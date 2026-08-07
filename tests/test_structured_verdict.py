"""구조화 판정 검증 (§4.4): 스키마 전달과 structured_output 회수 (Phase 3)."""

from __future__ import annotations

import json
import time

from agy_bridge.jobs import JobRegistry
from agy_bridge.runner import build_command
from agy_bridge.schemas import structured_default, verdict_schema_json


def test_verdict_schema_is_valid_json():
    parsed = json.loads(verdict_schema_json())
    assert parsed["required"] == ["verdict", "summary", "issues", "confidence"]
    assert "insufficient_context" in parsed["properties"]["verdict"]["enum"]
    issue_schema = parsed["properties"]["issues"]["items"]
    assert issue_schema["required"] == [
        "severity", "location", "problem", "evidence", "suggestion",
    ]


def test_structured_prompt_demands_single_json_object():
    """산문 유도와 스키마 강제가 충돌하면 agy가 스키마를 무시한다 (실측)."""
    from agy_bridge.prompts import assemble_prompt

    prompt = assemble_prompt(mode="verify", question="q", structured=True)
    assert "단일 JSON 객체" in prompt

    prose = assemble_prompt(mode="verify", question="q", structured=False)
    assert "단일 JSON 객체" not in prose


def test_structured_default_only_for_verify():
    assert structured_default("verify") is True
    for mode in ("review", "derive", "literature", "design"):
        assert structured_default(mode) is False


def test_build_command_includes_json_schema(bridge_config):
    config = bridge_config("/bin/true")
    schema = verdict_schema_json()
    cmd = build_command("질문", config=config, json_schema=schema)
    assert cmd[cmd.index("--json-schema") + 1] == schema

    cmd_without = build_command("질문", config=config)
    assert "--json-schema" not in cmd_without


def test_job_passes_schema_and_returns_verdict(tmp_path, bridge_config):
    """job 경로에서 --json-schema가 argv로 전달되고 structured_output이 회수되는지."""
    argv_log = tmp_path / "argv.log"
    payload = {
        "conversation_id": "conv-v",
        "status": "SUCCESS",
        "response": "판정 완료",
        "structured_output": {
            "verdict": "major_issues",
            "summary": "열화학 보정 누락",
            "confidence": "high",
            "issues": [],
        },
        "usage": {"total_tokens": 10},
    }
    script = tmp_path / "argv-echo-agy"
    script.write_text(
        "#!/bin/sh\n"
        f'printf \'%s\\n\' "$@" > {argv_log}\n'
        f"printf '%s' '{json.dumps(payload)}'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    registry = JobRegistry(bridge_config(script))
    record = registry.start(
        "프롬프트", mode="verify", question="q", json_schema=verdict_schema_json()
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        record = registry.wait(record.job_id, 0.2)
        if record.state != "running":
            break
    assert record.state == "completed"
    assert record.result["structured_output"]["verdict"] == "major_issues"

    logged = argv_log.read_text(encoding="utf-8")
    assert "--json-schema" in logged
    assert '"insufficient_context"' in logged
