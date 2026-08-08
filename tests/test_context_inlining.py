"""전략 A 인라이닝 검증 (§4.3, §10): 행범위, deny_globs, 크기 상한."""

from __future__ import annotations

import pytest

from agy_bridge.config import DEFAULT_DENY_GLOBS
from agy_bridge.context import (
    ContextError,
    ContextTooLarge,
    inline_files,
    parse_spec,
)


def _inline(specs, root, **overrides):
    kwargs = {
        "project_root": root,
        "deny_globs": DEFAULT_DENY_GLOBS,
        "max_chars": 100_000,
    }
    kwargs.update(overrides)
    return inline_files(specs, **kwargs)


class TestParseSpec:
    def test_plain_path(self):
        assert parse_spec("src/solver.py") == ("src/solver.py", None, None)

    def test_range(self):
        assert parse_spec("src/solver.py:10-20") == ("src/solver.py", 10, 20)

    def test_single_line(self):
        assert parse_spec("src/solver.py:87") == ("src/solver.py", 87, 87)

    def test_colon_in_name_without_digits(self):
        assert parse_spec("weird:name.py") == ("weird:name.py", None, None)


def test_whole_file_inlining_has_line_numbers(tmp_path):
    (tmp_path / "a.py").write_text("alpha\nbeta\ngamma\n")
    text, manifest = _inline(["a.py"], tmp_path)
    assert "--- FILE a.py [1-3행 / 총 3행] ---" in text
    assert "1| alpha" in text and "3| gamma" in text
    assert manifest == [
        {"file": "a.py", "lines": "1-3", "chars": len(text),
         "bytes": len(text.encode("utf-8"))}
    ]


def test_line_range_slicing_keeps_absolute_numbers(tmp_path):
    (tmp_path / "b.py").write_text("\n".join(f"line{i}" for i in range(1, 11)))
    text, manifest = _inline(["b.py:4-6"], tmp_path)
    assert "4| line4" in text and "6| line6" in text
    assert "line3" not in text and "line7" not in text
    assert manifest[0]["lines"] == "4-6"


def test_end_beyond_eof_is_clamped(tmp_path):
    (tmp_path / "c.py").write_text("one\ntwo\n")
    _text, manifest = _inline(["c.py:2-999"], tmp_path)
    assert manifest[0]["lines"] == "2-2"


def test_start_beyond_eof_is_error(tmp_path):
    (tmp_path / "d.py").write_text("one\n")
    with pytest.raises(ContextError, match="범위"):
        _inline(["d.py:5-9"], tmp_path)


def test_missing_file_is_error(tmp_path):
    with pytest.raises(ContextError, match="파일이 없다"):
        _inline(["nope.py"], tmp_path)


@pytest.mark.parametrize(
    "name", [".env", ".env.local", "api_key.py", "auth_token.json", "cert.pem",
             "scf.chk", "orbital.wfn",
             # 후속 B: 흔한 자격증명 파일명 (홈-루트에서도 이름으로 막힌다)
             "id_rsa", "id_ed25519", "server.key", "keystore.p12",
             ".netrc", ".npmrc", ".git-credentials", "aws_credentials"]
)
def test_credential_like_paths_are_denied(tmp_path, name):
    (tmp_path / name).write_text("SECRET=1")
    with pytest.raises(ContextError, match="deny_globs"):
        _inline([name], tmp_path)


def test_credential_dir_paths_are_denied(tmp_path):
    """후속 B: 경로 매칭 — 루트가 홈이 되는 구성에서 ~/.ssh/id_rsa 유출 차단."""
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "known_hosts").write_text("host key")  # 이름만으론 안 걸리는 파일
    with pytest.raises(ContextError, match="deny_globs"):
        _inline([".ssh/known_hosts"], tmp_path)


def test_hardlink_to_outside_is_denied(tmp_path):
    """후속 B (§10): 루트 안 하드링크가 밖 inode를 가리키는 우회 — 링크 수>1이면
    거부한다. 경로 봉쇄는 하드링크를 감지하지 못한다."""
    import os

    root = tmp_path / "repo"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("OUTSIDE SECRET\n")
    os.link(secret, root / "alias.txt")  # 루트 안 경로, 밖 inode
    with pytest.raises(ContextError, match="하드링크"):
        _inline(["alias.txt"], root)


def test_size_cap_promotes_to_context_too_large(tmp_path):
    (tmp_path / "big.py").write_text("x" * 5000)
    with pytest.raises(ContextTooLarge, match="상한"):
        _inline(["big.py"], tmp_path, max_chars=1000)


def test_multiple_files_accumulate_toward_cap(tmp_path):
    (tmp_path / "f1.py").write_text("a" * 600)
    (tmp_path / "f2.py").write_text("b" * 600)
    with pytest.raises(ContextTooLarge):
        _inline(["f1.py", "f2.py"], tmp_path, max_chars=1000)


def test_multibyte_bytes_budget_enforced(tmp_path):
    """리뷰 #1 회귀 (§2.3-D): 한글(UTF-8 3 B/자) 자료는 문자 예산을 통과해도
    바이트 예산에 걸려야 한다 — 아니면 argv 한계에서 호출 전체가 죽는다."""
    line = "# 검토 대상: 상태방정식 선택이 임계점 근방에서 밀도 예측에 미치는 영향을 확인한다"
    (tmp_path / "ko.md").write_text("\n".join([line] * 1200), encoding="utf-8")
    # 파일: 약 5.8만 자 (문자 예산 10만 이내) / 약 14만 B (바이트 예산 12.2만 초과)
    with pytest.raises(ContextTooLarge, match="바이트"):
        _inline(["ko.md"], tmp_path)


class TestProjectRootConfinement:
    """리뷰 #3 (§10): files 인자는 프롬프트 인젝션으로 유도될 수 있는 입력이다.
    deny_globs는 자격증명 이름만 거르므로, 루트 밖 접근 자체를 봉쇄해야
    ~/.ssh/id_rsa 같은 (deny에 안 걸리는) 파일의 유출 경로가 닫힌다."""

    def _repo_and_secret(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir()
        secret = tmp_path / "id_rsa"
        secret.write_text("PRIVATE KEY\n")
        return root, secret

    def test_absolute_path_outside_root_is_rejected(self, tmp_path):
        root, secret = self._repo_and_secret(tmp_path)
        with pytest.raises(ContextError, match="밖의 파일"):
            _inline([str(secret)], root)

    def test_relative_escape_is_rejected(self, tmp_path):
        root, _secret = self._repo_and_secret(tmp_path)
        with pytest.raises(ContextError, match="밖의 파일"):
            _inline(["../id_rsa"], root)

    def test_symlink_escape_is_rejected(self, tmp_path):
        """저장소 안의 무해해 보이는 심링크가 밖을 가리키는 벡터 — resolve 후
        비교이므로 막혀야 한다."""
        root, secret = self._repo_and_secret(tmp_path)
        (root / "innocent.txt").symlink_to(secret)
        with pytest.raises(ContextError, match="밖의 파일"):
            _inline(["innocent.txt"], root)

    def test_confinement_error_does_not_reveal_existence(self, tmp_path):
        """루트 밖 경로는 존재하지 않아도 같은 오류 — 존재 오라클을 주지 않는다."""
        root, _secret = self._repo_and_secret(tmp_path)
        with pytest.raises(ContextError, match="밖의 파일"):
            _inline(["/no/such/file/anywhere"], root)

    def test_symlinked_project_root_still_accepts_inside_files(self, tmp_path):
        """루트 자체가 심링크여도 내부 파일은 정상 동작해야 한다 (resolve 비교)."""
        real = tmp_path / "real-repo"
        real.mkdir()
        (real / "a.py").write_text("data\n")
        link = tmp_path / "link-repo"
        link.symlink_to(real)
        _text, manifest = _inline(["a.py"], link)
        assert manifest[0]["file"] == "a.py"
