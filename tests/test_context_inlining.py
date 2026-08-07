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
    assert manifest == [{"file": "a.py", "lines": "1-3", "chars": len(text)}]


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
             "scf.chk", "orbital.wfn"]
)
def test_credential_like_paths_are_denied(tmp_path, name):
    (tmp_path / name).write_text("SECRET=1")
    with pytest.raises(ContextError, match="deny_globs"):
        _inline([name], tmp_path)


def test_size_cap_promotes_to_context_too_large(tmp_path):
    (tmp_path / "big.py").write_text("x" * 5000)
    with pytest.raises(ContextTooLarge, match="상한"):
        _inline(["big.py"], tmp_path, max_chars=1000)


def test_multiple_files_accumulate_toward_cap(tmp_path):
    (tmp_path / "f1.py").write_text("a" * 600)
    (tmp_path / "f2.py").write_text("b" * 600)
    with pytest.raises(ContextTooLarge):
        _inline(["f1.py", "f2.py"], tmp_path, max_chars=1000)


def test_absolute_path_outside_root_displays_absolute(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    target = outside / "ref.py"
    target.write_text("data\n")
    root = tmp_path / "repo"
    root.mkdir()
    _text, manifest = _inline([str(target)], root)
    assert manifest[0]["file"] == target.as_posix()
