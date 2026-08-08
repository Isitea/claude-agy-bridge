"""전체 코드 리뷰(2026-08-08)에서 확인된 16건에 대한 회귀 테스트.

각 테스트는 수정 전 실제로 재현됐던 실패를 고정한다 — 번호는 리뷰 항목 번호다.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agy_bridge.config import DEFAULT_DENY_GLOBS, StartupError, load_config
from agy_bridge.context import ContextError, _render_spec, prepare_context
from agy_bridge.jobs import JobRegistry
from agy_bridge.runner import AgyError, parse_agy_output
from agy_bridge.schemas import validate_verdict
from agy_bridge.serve import ContextServer

PAYLOAD = {
    "conversation_id": "c",
    "status": "SUCCESS",
    "response": "ok",
    "usage": {"total_tokens": 1},
}


def _status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _wait_terminal(registry, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = registry.wait(job_id, 0.2)
        if record.state in ("completed", "failed", "timeout", "cancelled"):
            return record
    raise AssertionError(f"{job_id}가 종결되지 않음")


class TestServedPaths:
    def test_encoded_and_query_requests_are_served(self):
        """#1: 비ASCII·공백 이름과 쿼리스트링이 붙은 요청도 받아야 한다.
        원문 비교하던 시절 한글 저장소의 서빙 자료는 전부 404였다."""
        server = ContextServer({"문서__설계.md": b"korean", "a b.txt": b"space"})
        try:
            assert _status(server.url_for("문서__설계.md")) == 200
            assert _status(server.url_for("a b.txt")) == 200
            assert _status(server.url_for("a b.txt") + "?v=1") == 200
        finally:
            server.close()

    def test_url_for_is_a_valid_request_target(self):
        """광고하는 URL 자체가 인코딩돼 있어야 클라이언트가 보낼 수 있다."""
        server = ContextServer({"한글.md": b"x"})
        try:
            assert "한글" not in server.url_for("한글.md")
        finally:
            server.close()


class TestContextRendering:
    def test_empty_file_is_not_an_error(self, tmp_path):
        """#13: 빈 __init__.py 하나가 호출 전체를 죽이면 안 된다."""
        (tmp_path / "__init__.py").write_text("")
        rendered = _render_spec("__init__.py", project_root=tmp_path, deny_globs=())
        assert "빈 파일" in rendered["block"]
        assert rendered["lines"] == "1-0"

    def test_line_numbers_follow_newlines_only(self, tmp_path):
        """#8: splitlines()는 폼피드 등에서도 쪼개 절대 행 번호를 어긋나게 한다."""
        (tmp_path / "ff.py").write_text("a=1\nb=2\x0cc=3\nd=4\n")
        block = _render_spec("ff.py", project_root=tmp_path, deny_globs=())["block"]
        assert "총 3행" in block           # wc -l과 일치
        assert block.splitlines()[-1] == "3| d=4"
        # 행범위 선택도 같은 기준이어야 한다
        picked = _render_spec("ff.py:3", project_root=tmp_path, deny_globs=())["block"]
        assert picked.splitlines()[-1] == "3| d=4"

    @pytest.mark.parametrize("project", ["token-service", "secretary-app", "my_keys"])
    def test_substring_globs_do_not_match_directories(self, tmp_path, project):
        """#7: 경로에 token·secret이 든 저장소가 통째로 막히면 안 된다."""
        root = tmp_path / project
        root.mkdir()
        (root / "solver.py").write_text("import math\n")
        rendered = _render_spec(
            "solver.py", project_root=root, deny_globs=DEFAULT_DENY_GLOBS
        )
        assert rendered["display"] == "solver.py"

    def test_credential_dirs_are_still_denied(self, tmp_path):
        """#7 수정이 */.ssh/* 류 경로 패턴까지 풀어 버리면 안 된다."""
        (tmp_path / ".ssh").mkdir()
        (tmp_path / ".ssh" / "known_hosts").write_text("k")
        with pytest.raises(ContextError, match="deny_globs"):
            _render_spec(
                ".ssh/known_hosts", project_root=tmp_path, deny_globs=DEFAULT_DENY_GLOBS
            )

    def test_serve_names_do_not_collide(self, tmp_path):
        """#15: '/'→'__' 치환이 단사가 아니라 한 파일이 조용히 사라졌다."""
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "b.txt").write_text("FIRST\n")
        (tmp_path / "a__b.txt").write_text("SECOND\n")
        prepared = prepare_context(
            ["a/b.txt", "a__b.txt"], project_root=tmp_path, deny_globs=(), max_chars=1
        )
        assert len(prepared.served_manifest) == 2
        assert len(prepared.to_serve) == 2  # manifest가 보고한 만큼 실제로 서빙된다
        bodies = {bytes(v) for v in prepared.to_serve.values()}
        assert len(bodies) == 2


class TestOverlayConfinement:
    def test_escaping_overlay_dir_is_rejected(self, tmp_path, monkeypatch):
        """#3: overlay_dir 내용은 전부 외부 모델로 나간다 — 루트 밖은 거부."""
        root = tmp_path / "repo"
        (root / ".git").mkdir(parents=True)
        (root / ".agy-bridge.toml").write_text(
            '[playbooks]\noverlay_dir = "../secrets"\n'
        )
        monkeypatch.setenv("AGY_BIN", "/bin/true")
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        monkeypatch.setenv("AGY_BRIDGE_PROJECT_ROOT", str(root))
        with pytest.raises(StartupError, match="밖을 가리킨다"):
            load_config()

    def test_discover_overlays_defends_independently(self, tmp_path):
        """설정 검사를 우회한 호출 경로에서도 루트 밖을 읽지 않아야 한다."""
        from agy_bridge.prompts import discover_overlays

        outside = tmp_path / "secrets"
        outside.mkdir()
        (outside / "k.md").write_text("SECRET")
        root = tmp_path / "repo"
        root.mkdir()
        assert discover_overlays(root, "../secrets") == []


class TestRunnerContract:
    @pytest.mark.parametrize(
        "stdout",
        ['[1, 2, 3]', '"문자열"', '{"status": "SUCCESS", "response": 42}'],
    )
    def test_every_failure_is_promoted_to_agy_error(self, stdout):
        """#6: 계약대로 AgyError만 나가야 한다 — AttributeError가 새면 감시
        스레드가 죽어 job이 영구 running으로 고착된다."""
        with pytest.raises(AgyError):
            parse_agy_output(stdout, "", returncode=0)


class TestVerdictValidation:
    @pytest.mark.parametrize(
        "value",
        ["그냥 문자열", 42, {"verdict": "완전히 틀렸음"}, {"summary": "x"},
         {"verdict": "correct", "summary": "s", "issues": [{}], "confidence": "high"}],
    )
    def test_invalid_verdicts_are_reported(self, value):
        """#4: 스키마 위반을 통과시키면 소비 세션이 엉뚱한 값을 판정으로 읽는다."""
        assert validate_verdict(value)

    def test_valid_verdict_passes(self):
        assert validate_verdict(
            {"verdict": "minor_issues", "summary": "s", "confidence": "low",
             "issues": [{"severity": "minor", "location": "a.py:1",
                         "problem": "p", "evidence": "e", "suggestion": "s"}]}
        ) == []


class TestJobRobustness:
    def _flaky_agy(self, tmp_path, name="flaky"):
        marker = tmp_path / f"{name}-marker"
        script = tmp_path / name
        script.write_text(
            f"#!/bin/sh\n"
            f"if [ ! -f {marker} ]; then touch {marker}; echo err >&2; exit 1; fi\n"
            f"printf '%s' '{json.dumps(PAYLOAD)}'\n"
        )
        script.chmod(0o755)
        return script

    def test_retry_is_refused_when_hook_vetoes(self, tmp_path, bridge_config):
        """#12: 재시도도 스폰 전에 예산 확인을 거쳐야 한다."""
        def veto(record):
            raise RuntimeError("일일 호출 예산 초과")

        registry = JobRegistry(
            bridge_config(self._flaky_agy(tmp_path)), on_retry=veto
        )
        record = _wait_terminal(
            registry, registry.start("p", mode="review", question="q").job_id
        )
        assert record.state == "failed"
        assert record.attempts == 1          # 두 번째 프로세스는 뜨지 않았다
        assert "예산 초과" in record.error

    def test_on_complete_exception_does_not_kill_watcher(self, bridge_config, fake_agy):
        """#11: 원장·세션 I/O 오류가 성공한 job을 실패로 뒤집으면 안 된다."""
        def boom(record, result):
            raise OSError(28, "No space left on device")

        registry = JobRegistry(bridge_config(fake_agy(PAYLOAD)), on_complete=boom)
        record = _wait_terminal(
            registry, registry.start("p", mode="review", question="q").job_id
        )
        assert record.state == "completed"

    def test_cancel_kills_even_if_hook_raises(self, bridge_config, fake_agy):
        """#10: 훅이 터져도 프로세스는 죽어야 한다 — pid는 이미 null로 영속화된다."""
        def boom(record, result):
            raise OSError(28, "No space left on device")

        registry = JobRegistry(
            bridge_config(fake_agy(PAYLOAD, sleep_seconds=60)), on_complete=boom
        )
        record = registry.start("p", mode="review", question="q")
        pid = record.pid
        registry.cancel(record.job_id)
        deadline = time.time() + 5
        while time.time() < deadline and Path(f"/proc/{pid}").exists():
            time.sleep(0.1)
        assert not Path(f"/proc/{pid}").exists()

    def test_recycled_pid_is_not_signalled(self, bridge_config, fake_agy):
        """#9: 재부팅을 넘긴 레코드의 pid는 남의 것일 수 있다 — 죽이면 안 된다."""
        registry = JobRegistry(bridge_config(fake_agy(PAYLOAD, sleep_seconds=30)))
        record = registry.start("p", mode="review", question="q")
        pid = record.pid
        with registry._lock:
            registry._records[record.job_id].pid_starttime = 999_999_999
        registry.cancel(record.job_id)
        time.sleep(0.5)
        try:
            assert Path(f"/proc/{pid}").exists()  # 무관한 프로세스는 살아남는다
        finally:
            os.kill(pid, 9)

    def test_shutdown_stops_serving_dependent_jobs(self, bridge_config, fake_agy):
        """#2: 브리지가 죽으면 서빙 URL도 죽는다 — 그 job을 완주시키면 자료를
        못 읽은 답이 나중에 completed로 회수된다."""
        registry = JobRegistry(bridge_config(fake_agy(PAYLOAD, sleep_seconds=60)))
        server = ContextServer({"doc.txt": b"data"})
        record = registry.start(
            "p", mode="review", question="q", context_server=server
        )
        stopped = registry.shutdown()
        assert stopped == [record.job_id]
        assert registry.get(record.job_id).state == "cancelled"
        assert not server.is_serving()

    def test_unknown_fields_on_disk_do_not_break_loading(self, bridge_config, fake_agy):
        """#6(b): 공유 상태 디렉터리에 신버전이 필드를 더 써도 죽지 않아야 한다."""
        config = bridge_config(fake_agy(PAYLOAD))
        registry = JobRegistry(config)
        record = _wait_terminal(
            registry, registry.start("p", mode="review", question="q").job_id
        )
        path = config.state_dir / "jobs" / f"{record.job_id}.json"
        data = json.loads(path.read_text())
        data["미래_필드"] = "신버전이 추가함"
        path.write_text(json.dumps(data, ensure_ascii=False))

        fresh = JobRegistry(config)
        assert fresh.get(record.job_id).state == "completed"

    def test_retry_updates_pid_for_other_processes(self, tmp_path, bridge_config):
        """#5: 재시도로 pid가 바뀌면 다른 브리지 프로세스도 그것을 봐야 한다."""
        marker = tmp_path / "slow-marker"
        script = tmp_path / "slow-flaky"
        script.write_text(
            f"#!/bin/sh\n"
            f"if [ ! -f {marker} ]; then touch {marker}; echo err >&2; exit 1; fi\n"
            f"sleep 30\n"
        )
        script.chmod(0o755)
        config = bridge_config(script)
        owner = JobRegistry(config)
        other = JobRegistry(config)

        record = owner.start("p", mode="review", question="q")
        other.get(record.job_id)          # 1회차 pid를 캐시한다
        time.sleep(1.5)                   # owner가 재시도 스폰
        observed = other.get(record.job_id)
        assert observed.state == "running"  # 죽은 옛 pid로 오종결하지 않는다
        assert observed.attempts == 2
        owner.cancel(record.job_id)
