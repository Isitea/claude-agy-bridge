"""플레이북 로딩·매핑·오버레이 발견 검증 (§8, Phase 4)."""

from __future__ import annotations

import pytest

from agy_bridge.prompts import (
    BUILTIN_PLAYBOOKS,
    MODE_PLAYBOOKS,
    MODES,
    assemble_prompt,
    compose_playbooks_block,
    discover_overlays,
    load_builtin_playbook,
    playbook_names_for_mode,
)


def test_all_builtin_playbooks_load():
    for name in BUILTIN_PLAYBOOKS:
        text = load_builtin_playbook(name)
        assert text.strip().startswith("#"), name
        assert "확인하라" in text, name


def test_mode_mapping_covers_all_modes_with_known_names():
    assert set(MODE_PLAYBOOKS) == set(MODES)
    for mode, names in MODE_PLAYBOOKS.items():
        assert names, mode
        for name in names:
            assert name in BUILTIN_PLAYBOOKS, (mode, name)


def test_enabled_overrides_mode_mapping():
    assert playbook_names_for_mode("verify", ("numerics",)) == ("numerics",)
    assert playbook_names_for_mode("verify", None) == MODE_PLAYBOOKS["verify"]


def test_unknown_playbook_name_fails_loudly():
    with pytest.raises(ValueError, match="알 수 없는 플레이북"):
        load_builtin_playbook("orca-keywords")


def test_total_size_within_guideline():
    """§8.1 경험칙: 전 플레이북 합계가 8k자를 넘으면 스킬 설치 모드를 검토해야 한다."""
    total = sum(len(load_builtin_playbook(n)) for n in BUILTIN_PLAYBOOKS)
    assert total < 8_000, f"플레이북 합계 {total}자 — §8.1 탈출구 검토 필요"


def test_overlay_discovery_and_ordering(tmp_path):
    overlay_dir = tmp_path / ".agy-bridge" / "playbooks"
    overlay_dir.mkdir(parents=True)
    (overlay_dir / "b-team-rules.md").write_text("# 팀 규칙 B\n내부 단위계는 SI.")
    (overlay_dir / "a-units.md").write_text("# 팀 규칙 A\n압력 기준 1 bar.")

    overlays = discover_overlays(tmp_path, ".agy-bridge/playbooks")
    assert [name for name, _ in overlays] == ["a-units.md", "b-team-rules.md"]

    block = compose_playbooks_block(
        "verify", project_root=tmp_path, overlay_dir=".agy-bridge/playbooks"
    )
    # 오버레이는 내장 플레이북 뒤에 온다 (§8.6)
    builtin_pos = block.index("오차 전파")
    overlay_pos = block.index("팀 규칙 A")
    assert builtin_pos < overlay_pos
    assert "프로젝트 오버레이: a-units.md" in block


def test_no_overlay_dir_is_fine(tmp_path):
    block = compose_playbooks_block(
        "derive", project_root=tmp_path, overlay_dir=".agy-bridge/playbooks"
    )
    assert "유도 점검" in block


def test_assemble_order_common_playbooks_instruction_question(tmp_path):
    block = compose_playbooks_block(
        "verify", project_root=tmp_path, overlay_dir=".agy-bridge/playbooks"
    )
    prompt = assemble_prompt(
        mode="verify", question="타당한가?", playbooks_block=block, structured=True
    )
    positions = [
        prompt.index("공통 규약"),
        prompt.index("검증 플레이북"),
        prompt.index("검토 지시"),
        prompt.index("## 질문"),
        prompt.index("출력 형식"),
    ]
    assert positions == sorted(positions)


def test_config_playbooks_section_parsed(tmp_path, monkeypatch):
    from agy_bridge.config import load_config

    (tmp_path / ".git").mkdir()
    (tmp_path / ".agy-bridge.toml").write_text(
        '[playbooks]\nenabled = ["numerics"]\noverlay_dir = "custom/dir"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("AGY_BRIDGE_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("AGY_BIN", "/bin/true")

    config = load_config(tmp_path)
    assert config.playbooks_enabled == ("numerics",)
    assert config.overlay_dir == "custom/dir"
