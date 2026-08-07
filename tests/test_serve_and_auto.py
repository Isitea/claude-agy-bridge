"""전략 C 서빙 안전 요건(§10.1)과 auto 전환(§4.3) 검증 (Phase 5)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

from agy_bridge.context import prepare_context
from agy_bridge.jobs import JobRegistry
from agy_bridge.serve import ContextServer


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class TestContextServer:
    def test_serves_whitelisted_content(self):
        server = ContextServer({"a.py": b"hello"})
        try:
            status, body = _get(server.url_for("a.py"))
            assert (status, body) == (200, b"hello")
            assert server.base_url.startswith("http://127.0.0.1:")
        finally:
            server.close()

    def test_index_lists_files_without_directory_listing(self):
        server = ContextServer({"a.py": b"x", "b.py": b"y"})
        try:
            status, body = _get(server.url_for("INDEX"))
            assert status == 200
            assert b"a.py" in body and b"b.py" in body
            # 토큰 루트/파일 직접 접근은 404 — 목록은 INDEX로만
            status, _ = _get(server.base_url.rsplit("/", 1)[0] + "/a.py")
            assert status == 404
        finally:
            server.close()

    def test_wrong_token_is_404(self):
        server = ContextServer({"a.py": b"x"})
        try:
            port = server.port
            status, _ = _get(f"http://127.0.0.1:{port}/wrongtoken/a.py")
            assert status == 404
        finally:
            server.close()

    def test_non_get_is_405(self):
        server = ContextServer({"a.py": b"x"})
        try:
            request = urllib.request.Request(
                server.url_for("a.py"), data=b"body", method="POST"
            )
            try:
                urllib.request.urlopen(request, timeout=5)
                status = 200
            except urllib.error.HTTPError as exc:
                status = exc.code
            assert status == 405
        finally:
            server.close()

    def test_head_then_get_on_keepalive_connection(self):
        """회귀 (Phase 5 실측): HEAD 응답에 본문이 실리면 keep-alive 연결의
        다음 GET 상태줄이 오염된다 — agy READ_URL이 정확히 이 순서로 요청한다."""
        import http.client
        from urllib.parse import urlparse

        server = ContextServer({"a.py": b"hello"})
        try:
            parsed = urlparse(server.url_for("a.py"))
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
            conn.request("HEAD", parsed.path)
            head = conn.getresponse()
            head.read()
            assert head.status == 200
            conn.request("GET", parsed.path)  # 같은 연결 재사용
            get = conn.getresponse()
            assert get.status == 200
            assert get.read() == b"hello"
            conn.close()
        finally:
            server.close()

    def test_close_is_idempotent_and_frees_port(self):
        server = ContextServer({"a.py": b"x"})
        url = server.url_for("a.py")
        server.close()
        server.close()
        assert not server.is_serving()
        with pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(url, timeout=2)


class TestAutoStrategy:
    def _prepare(self, specs, root, max_chars=100_000):
        return prepare_context(
            specs,
            project_root=root,
            deny_globs=(".env*",),
            max_chars=max_chars,
        )

    def test_small_files_stay_inline(self, tmp_path):
        (tmp_path / "a.py").write_text("small\n")
        prepared = self._prepare(["a.py"], tmp_path)
        assert prepared.strategy == "inline"
        assert not prepared.to_serve
        assert prepared.reason is None

    def test_overflow_switches_to_mixed_in_order(self, tmp_path):
        (tmp_path / "target.py").write_text("t\n" * 100)
        (tmp_path / "big_ref.txt").write_text("x" * 5000)
        prepared = self._prepare(["target.py", "big_ref.txt"], tmp_path, max_chars=1000)
        assert prepared.strategy == "mixed"
        assert [m["file"] for m in prepared.inline_manifest] == ["target.py"]
        assert [m["file"] for m in prepared.served_manifest] == ["big_ref.txt"]
        assert "auto" in prepared.reason

    def test_first_file_too_big_switches_to_serve(self, tmp_path):
        (tmp_path / "huge.txt").write_text("x" * 5000)
        prepared = self._prepare(["huge.txt"], tmp_path, max_chars=1000)
        assert prepared.strategy == "serve"
        assert not prepared.inline_manifest

    def test_no_reordering_after_overflow(self, tmp_path):
        """한 파일이 넘친 뒤에는 작은 파일도 순서를 건너뛰어 인라이닝되지 않는다."""
        (tmp_path / "a.txt").write_text("x" * 550)
        (tmp_path / "b.txt").write_text("y" * 550)   # 합계가 상한 초과 → 서빙
        (tmp_path / "c.py").write_text("s\n")        # 작지만 b 뒤이므로 함께 서빙
        prepared = self._prepare(["a.txt", "b.txt", "c.py"], tmp_path, max_chars=1000)
        assert [m["file"] for m in prepared.inline_manifest] == ["a.txt"]
        assert [m["file"] for m in prepared.served_manifest] == ["b.txt", "c.py"]

    def test_served_content_preserves_line_numbers(self, tmp_path):
        (tmp_path / "doc.txt").write_text("alpha\nbeta\n")
        prepared = self._prepare(["doc.txt"], tmp_path, max_chars=10)
        content = next(iter(prepared.to_serve.values())).decode()
        assert "1| alpha" in content and "2| beta" in content

    def test_large_served_file_is_chunked_on_line_boundaries(self, tmp_path, monkeypatch):
        import agy_bridge.context as ctx

        monkeypatch.setattr(ctx, "SERVE_CHUNK_BYTES", 120)
        (tmp_path / "big.txt").write_text("\n".join(f"line-{i:04d}" for i in range(30)))
        prepared = self._prepare(["big.txt"], tmp_path, max_chars=10)

        names = prepared.served_manifest[0]["serve_names"]
        assert len(names) > 1
        assert names[0].endswith(f"of{len(names):02d}")
        # 부분을 이어 붙이면 원본 블록과 동일하고, 행 번호가 보존된다
        joined = "\n".join(
            prepared.to_serve[name].decode() for name in names
        )
        assert "line-0000" in joined and "line-0029" in joined
        assert "30| line-0029" in joined

    def test_deny_globs_apply_to_served_paths_too(self, tmp_path):
        """§10.1: 인라이닝에만 걸고 서빙에서 빠지면 우회로가 된다."""
        from agy_bridge.context import ContextError

        (tmp_path / ".env.prod").write_text("SECRET=1")
        with pytest.raises(ContextError, match="deny_globs"):
            self._prepare([".env.prod"], tmp_path, max_chars=1)


PAYLOAD = {
    "conversation_id": "conv-s",
    "status": "SUCCESS",
    "response": "ok",
    "usage": {"total_tokens": 5},
}


def _wait_terminal(registry, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = registry.wait(job_id, 0.2)
        if record.state in ("completed", "failed", "timeout", "cancelled"):
            return record
    raise AssertionError("종결되지 않음")


class TestServerLifetimeEqualsJobLifetime:
    def test_server_closed_on_completion(self, bridge_config, fake_agy):
        registry = JobRegistry(bridge_config(fake_agy(PAYLOAD)))
        server = ContextServer({"doc.txt": b"data"})
        record = registry.start(
            "p", mode="review", question="q", context_server=server
        )
        _wait_terminal(registry, record.job_id)
        assert not server.is_serving()

    def test_server_closed_on_failure(self, bridge_config, fake_agy):
        registry = JobRegistry(
            bridge_config(fake_agy({**PAYLOAD, "response": ""}))
        )
        server = ContextServer({"doc.txt": b"data"})
        record = registry.start(
            "p", mode="review", question="q", context_server=server
        )
        record = _wait_terminal(registry, record.job_id)
        assert record.state == "failed"
        assert not server.is_serving()

    def test_server_closed_on_cancel(self, bridge_config, fake_agy):
        registry = JobRegistry(
            bridge_config(fake_agy(PAYLOAD, sleep_seconds=30))
        )
        server = ContextServer({"doc.txt": b"data"})
        record = registry.start(
            "p", mode="review", question="q", context_server=server
        )
        registry.cancel(record.job_id)
        assert not server.is_serving()

    def test_server_closed_on_timeout(self, bridge_config, fake_agy):
        registry = JobRegistry(
            bridge_config(fake_agy(PAYLOAD, sleep_seconds=30), hard_kill_seconds=1)
        )
        server = ContextServer({"doc.txt": b"data"})
        record = registry.start(
            "p", mode="review", question="q", context_server=server
        )
        record = _wait_terminal(registry, record.job_id, timeout=10)
        assert record.state == "timeout"
        assert not server.is_serving()


class TestRetry:
    def test_infra_failure_retried_once(self, tmp_path, bridge_config):
        """첫 실행은 exit 1, 두 번째는 성공하는 가짜 agy — attempts=2로 완료."""
        marker = tmp_path / "attempted"
        script = tmp_path / "flaky-agy"
        script.write_text(
            "#!/bin/sh\n"
            f"if [ ! -f {marker} ]; then\n"
            f"  touch {marker}\n"
            "  echo 'transient network error' >&2\n"
            "  exit 1\n"
            "fi\n"
            f"printf '%s' '{json.dumps(PAYLOAD)}'\n",
            encoding="utf-8",
        )
        script.chmod(0o755)

        registry = JobRegistry(bridge_config(script))
        record = registry.start("p", mode="review", question="q")
        record = _wait_terminal(registry, record.job_id)
        assert record.state == "completed"
        assert record.attempts == 2

    def test_empty_response_is_not_retried(self, bridge_config, fake_agy):
        """§2.3-A 권한 거부 침묵은 재시도 금지 — 즉시 실패로 승격."""
        registry = JobRegistry(
            bridge_config(fake_agy({**PAYLOAD, "response": ""}))
        )
        record = registry.start("p", mode="review", question="q")
        record = _wait_terminal(registry, record.job_id)
        assert record.state == "failed"
        assert record.attempts == 1
