"""§2.3(A) 조용한 실패 회귀 테스트 — Phase 1의 최우선 테스트 (§11).

status=SUCCESS + 빈 response를 오류로 승격하지 못하면 브리지가 "검증 통과"로
오독되는 침묵을 반환한다. 이 파일이 그것을 영구히 막는다.
"""

from __future__ import annotations

import pytest

from agy_bridge.runner import ARGV_PROMPT_LIMIT_BYTES, AgyError, run_agy

SUCCESS_PAYLOAD = {
    "conversation_id": "conv-123",
    "status": "SUCCESS",
    "response": "리뷰 의견: src/solver.py:87 에서 단위 불일치.",
    "duration_seconds": 3.2,
    "num_turns": 1,
    "usage": {"input_tokens": 17000, "output_tokens": 300, "total_tokens": 17300},
}

HEADLESS_DENIAL_STDERR = (
    'jetski: no output produced — a tool required the "command" permission that '
    "headless mode cannot prompt for, so it was auto-denied."
)


def test_success_with_empty_response_is_promoted_to_error(bridge_config, fake_agy):
    """가장 위험한 실패 모드: SUCCESS인데 response가 빈 문자열 (§2.3-A)."""
    payload = {**SUCCESS_PAYLOAD, "response": ""}
    agy = fake_agy(payload, stderr=HEADLESS_DENIAL_STDERR)
    config = bridge_config(agy)

    with pytest.raises(AgyError) as excinfo:
        run_agy("아무 질문", config=config)

    message = str(excinfo.value)
    assert "response가 비어" in message
    # stderr는 그대로 호출자에게 전달되어야 한다 (§2.3-A)
    assert "auto-denied" in message


def test_success_with_whitespace_response_is_promoted(bridge_config, fake_agy):
    payload = {**SUCCESS_PAYLOAD, "response": "  \n\t "}
    config = bridge_config(fake_agy(payload))
    with pytest.raises(AgyError, match="비어"):
        run_agy("질문", config=config)


def test_empty_response_with_structured_output_is_valid(bridge_config, fake_agy):
    """--json-schema 사용 시 response 대신 structured_output만 올 수 있다 (Phase 3)."""
    payload = {
        **SUCCESS_PAYLOAD,
        "response": "",
        "structured_output": {"verdict": "correct", "issues": []},
    }
    config = bridge_config(fake_agy(payload))
    result = run_agy("질문", config=config)
    assert result.structured_output == {"verdict": "correct", "issues": []}


def test_nonzero_exit_code(bridge_config, fake_agy):
    agy = fake_agy("", returncode=3, stderr="auth expired")
    config = bridge_config(agy)
    with pytest.raises(AgyError) as excinfo:
        run_agy("질문", config=config)
    assert excinfo.value.returncode == 3
    assert "auth expired" in str(excinfo.value)


def test_non_json_stdout(bridge_config, fake_agy):
    config = bridge_config(fake_agy("Segmentation fault (core dumped)"))
    with pytest.raises(AgyError, match="JSON이 아니다"):
        run_agy("질문", config=config)


def test_non_success_status(bridge_config, fake_agy):
    payload = {**SUCCESS_PAYLOAD, "status": "FAILED"}
    config = bridge_config(fake_agy(payload))
    with pytest.raises(AgyError, match="FAILED"):
        run_agy("질문", config=config)


def test_normal_success_roundtrip(bridge_config, fake_agy):
    config = bridge_config(fake_agy(SUCCESS_PAYLOAD))
    result = run_agy("질문", config=config)
    assert result.response.startswith("리뷰 의견")
    assert result.conversation_id == "conv-123"
    assert result.usage["total_tokens"] == 17300
    assert result.duration_seconds == 3.2


def test_argv_limit_guard_fails_before_spawn(bridge_config):
    """131,072 B 단일 인자 한계 (§2.3-D). /bin/false가 실행되면 다른 오류가 났을 것."""
    config = bridge_config("/bin/false")
    huge_prompt = "x" * (ARGV_PROMPT_LIMIT_BYTES + 1)
    with pytest.raises(AgyError, match="argv"):
        run_agy(huge_prompt, config=config)


def test_command_safety_flags(bridge_config):
    """§10: --mode plan 고정, slash 확장 차단, skip-permissions 부재."""
    from agy_bridge.runner import build_command

    config = bridge_config("/bin/true")
    cmd = build_command("질문", config=config)
    assert "--mode" in cmd and cmd[cmd.index("--mode") + 1] == "plan"
    assert "--disable-slash-commands" in cmd
    assert "--dangerously-skip-permissions" not in cmd


def test_family_model_carries_effort_flag(bridge_config):
    """접미사 없는 패밀리 ID에는 --effort를 넘긴다 (정상 형태)."""
    from agy_bridge.runner import build_command

    config = bridge_config("/bin/true", model="gemini-3.1-pro", effort="high")
    cmd = build_command("질문", config=config)
    assert cmd[cmd.index("--model") + 1] == "gemini-3.1-pro"
    assert cmd[cmd.index("--effort") + 1] == "high"


def test_level_suffixed_model_omits_effort_flag(bridge_config):
    """수준이 박힌 ID에 --effort를 함께 넘기면 agy가 충돌로 거부한다 — 빼야 한다."""
    from agy_bridge.runner import build_command

    config = bridge_config("/bin/true", model="gemini-3.1-pro-high", effort="high")
    cmd = build_command("질문", config=config)
    assert cmd[cmd.index("--model") + 1] == "gemini-3.1-pro-high"
    assert "--effort" not in cmd

    # 호출 인자로 모델만 바꾸는 경로도 같다 (config.effort와 충돌시키지 않는다).
    config = bridge_config("/bin/true", model="gemini-3.1-pro", effort="high")
    cmd = build_command("질문", config=config, model="gemini-3.7-flash-medium")
    assert "--effort" not in cmd


def test_explicit_effort_conflicting_with_model_is_rejected(bridge_config):
    """조용히 무시하지 않는다 — 낮췄다고 믿으며 high로 도는 게 오류보다 나쁘다."""
    from agy_bridge.runner import build_command

    config = bridge_config("/bin/true", model="gemini-3.1-pro-high", effort="high")
    with pytest.raises(AgyError, match="충돌"):
        build_command("질문", config=config, effort="low")


def test_smoke_model_and_effort_agree(bridge_config):
    """doctor·init 스모크(수준이 박힌 모델 + 일치하는 effort)는 거부되지 않는다."""
    from agy_bridge.cli import SMOKE_MODEL
    from agy_bridge.runner import build_command

    config = bridge_config("/bin/true")
    cmd = build_command("질문", config=config, model=SMOKE_MODEL, effort="low")
    assert cmd[cmd.index("--model") + 1] == SMOKE_MODEL
    assert "--effort" not in cmd


def test_conversation_resume_flag(bridge_config):
    from agy_bridge.runner import build_command

    config = bridge_config("/bin/true")
    cmd = build_command("질문", config=config, conversation_id="conv-9")
    assert cmd[cmd.index("--conversation") + 1] == "conv-9"
