from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from agy_bridge.config import Config


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
