from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from agy_bridge.config import Config


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path: Path, monkeypatch):
    """테스트가 실제 ~/.cache/claude-agy-bridge에 상태 디렉터리를 만들지 않게 한다.

    init·doctor 등은 load_config를 부르고, 그러면 프로젝트 경로 해시로 실제
    캐시에 디렉터리가 생긴다 — 실행할 때마다 해시가 달라 쓰레기가 쌓였다.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))


@pytest.fixture
def bridge_config(tmp_path: Path):
    """가짜 agy 바이너리를 주입할 수 있는 테스트용 Config 팩토리."""

    def _make(agy_bin: Path | str = "/bin/false", **overrides) -> Config:
        state_dir = tmp_path / "state"
        scratch_dir = state_dir / "scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        return Config(
            project_root=tmp_path,
            state_dir=state_dir,
            scratch_dir=scratch_dir,
            agy_bin=str(agy_bin),
            **overrides,
        )

    return _make


@pytest.fixture
def fake_agy(tmp_path: Path):
    """지정한 stdout/stderr/종료 코드를 재생하는 가짜 agy 실행 파일을 만든다."""

    def _make(
        payload: dict | str,
        *,
        returncode: int = 0,
        stderr: str = "",
        sleep_seconds: float = 0,
        name: str = "fake-agy",
    ) -> Path:
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        script = tmp_path / name
        script.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/sh
                sleep {sleep_seconds}
                printf '%s' {_sh_quote(stderr)} >&2
                printf '%s' {_sh_quote(stdout)}
                exit {returncode}
                """
            ),
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    return _make


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"
