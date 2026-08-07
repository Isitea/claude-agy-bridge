"""이식성 검증 (§7, Phase 6): 임의 CWD/저장소 기동, 경로 격리, init 부트스트랩."""

from __future__ import annotations

import json
from pathlib import Path

from agy_bridge.cli import main as cli_main
from agy_bridge.config import find_project_root, load_config, state_dir_for


def test_state_dirs_isolated_per_project(tmp_path):
    root_a = tmp_path / "proj-a"
    root_b = tmp_path / "proj-b"
    assert state_dir_for(root_a) != state_dir_for(root_b)


def test_project_root_walks_up_to_git(tmp_path, monkeypatch):
    monkeypatch.delenv("AGY_BRIDGE_PROJECT_ROOT", raising=False)
    repo = tmp_path / "repo"
    nested = repo / "src" / "deep"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    assert find_project_root(nested) == repo


def test_project_root_env_override(tmp_path, monkeypatch):
    override = tmp_path / "elsewhere"
    override.mkdir()
    monkeypatch.setenv("AGY_BRIDGE_PROJECT_ROOT", str(override))
    assert find_project_root(tmp_path) == override


def test_no_git_falls_back_to_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("AGY_BRIDGE_PROJECT_ROOT", raising=False)
    bare = tmp_path / "bare"
    bare.mkdir()
    assert find_project_root(bare) == bare


def test_load_config_from_arbitrary_cwd(tmp_path, monkeypatch):
    """대상 저장소 정보 없이 어떤 CWD에서든 기동해야 한다."""
    monkeypatch.delenv("AGY_BRIDGE_PROJECT_ROOT", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("AGY_BIN", "/bin/true")
    anywhere = tmp_path / "random" / "place"
    anywhere.mkdir(parents=True)
    config = load_config(anywhere)
    assert config.project_root == anywhere
    assert config.scratch_dir.is_dir()


def test_source_has_no_hardcoded_home_paths():
    """절대 규칙 (§7.2): 설치 경로 밖 어떤 경로도 하드코딩하지 않는다."""
    src = Path(__file__).parent.parent / "src"
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "/home/" not in text, f"{path}에 하드코딩 경로"
        assert "dev-space" not in text, f"{path}에 하드코딩 경로"


class TestInit:
    def _run_init(self, target: Path) -> int:
        return cli_main(["init", "--target", str(target), "--no-smoke"])

    def test_creates_bootstrap_files(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("AGY_BIN", "/bin/true")
        target = tmp_path / "target"
        target.mkdir()

        assert self._run_init(target) == 0

        mcp = json.loads((target / ".mcp.json").read_text())
        assert mcp["mcpServers"]["agy"] == {
            "command": "agy-bridge", "args": ["serve"],
        }
        toml_text = (target / ".agy-bridge.toml").read_text()
        assert "daily_call_budget" in toml_text
        assert (target / ".agy-bridge" / "playbooks" / "_TEMPLATE.md").is_file()
        # CLAUDE.md는 자동으로 쓰지 않고 제안만 한다 (§9-4)
        assert not (target / "CLAUDE.md").exists()
        assert "CLAUDE.md" in capsys.readouterr().out

    def test_merges_existing_mcp_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGY_BIN", "/bin/true")
        target = tmp_path / "target"
        target.mkdir()
        (target / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"other": {"command": "other-server"}}})
        )

        assert self._run_init(target) == 0
        mcp = json.loads((target / ".mcp.json").read_text())
        assert "other" in mcp["mcpServers"]  # 기존 항목 보존
        assert "agy" in mcp["mcpServers"]

    def test_does_not_overwrite_existing_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGY_BIN", "/bin/true")
        target = tmp_path / "target"
        target.mkdir()
        (target / ".agy-bridge.toml").write_text('model = "custom"\n')

        assert self._run_init(target) == 0
        assert (target / ".agy-bridge.toml").read_text() == 'model = "custom"\n'

    def test_missing_agy_fails_actionably(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("AGY_BIN", raising=False)
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
        target = tmp_path / "target"
        target.mkdir()
        assert self._run_init(target) == 1
        assert "agy" in capsys.readouterr().err


class TestInitErgonomics:
    """Phase 6 이후 개선: --target 기본값 CWD, CLAUDE.md 반영, update 위임."""

    def test_target_defaults_to_cwd_git_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGY_BIN", "/bin/true")
        monkeypatch.delenv("AGY_BRIDGE_PROJECT_ROOT", raising=False)
        target = tmp_path / "repo"
        (target / "src").mkdir(parents=True)
        (target / ".git").mkdir()
        monkeypatch.chdir(target / "src")  # 하위 디렉터리에서 실행해도 루트를 찾는다

        assert cli_main(["init", "--no-smoke"]) == 0
        assert (target / ".mcp.json").is_file()
        assert not (target / "src" / ".mcp.json").exists()

    def test_claude_md_flag_writes_and_rerun_replaces(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGY_BIN", "/bin/true")
        target = tmp_path / "repo"
        target.mkdir()
        (target / "CLAUDE.md").write_text("# 기존 지침\n\n## 빌드\n내용\n")

        args = ["init", "--target", str(target), "--no-smoke", "--claude-md"]
        assert cli_main(args) == 0
        text = (target / "CLAUDE.md").read_text()
        assert "# 기존 지침" in text and "## 빌드" in text  # 기존 내용 보존
        assert text.count("## 과학 검증 (agy_consult)") == 1

        assert cli_main(args) == 0  # 재실행 → 교체, 중복 없음
        text = (target / "CLAUDE.md").read_text()
        assert text.count("## 과학 검증 (agy_consult)") == 1

    def test_claude_md_created_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGY_BIN", "/bin/true")
        target = tmp_path / "repo"
        target.mkdir()
        assert cli_main(
            ["init", "--target", str(target), "--no-smoke", "--claude-md"]
        ) == 0
        assert "agy_consult" in (target / "CLAUDE.md").read_text()

    def test_update_delegates_to_uv_tool_upgrade(self, monkeypatch):
        import subprocess

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd

            class Proc:
                returncode = 0

            return Proc()

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert cli_main(["update"]) == 0
        assert captured["cmd"] == ["uv", "tool", "upgrade", "agy-bridge"]

    def test_update_without_uv_is_actionable(self, monkeypatch, capsys):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert cli_main(["update"]) == 1
        assert "install.sh" in capsys.readouterr().err


def test_install_script_syntax():
    import subprocess

    script = Path(__file__).parent.parent / "install.sh"
    assert script.is_file()
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_template_overlay_not_injected(tmp_path, monkeypatch):
    """init이 만든 _TEMPLATE.md가 프롬프트에 주입되면 안 된다."""
    from agy_bridge.cli import OVERLAY_TEMPLATE
    from agy_bridge.prompts import discover_overlays

    overlay_dir = tmp_path / ".agy-bridge" / "playbooks"
    overlay_dir.mkdir(parents=True)
    (overlay_dir / "_TEMPLATE.md").write_text(OVERLAY_TEMPLATE)
    (overlay_dir / "real.md").write_text("# 진짜 오버레이")

    names = [n for n, _ in discover_overlays(tmp_path, ".agy-bridge/playbooks")]
    assert names == ["real.md"]


def test_doctor_no_smoke_passes_in_clean_project(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGY_BIN", "/bin/true")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("AGY_BRIDGE_PROJECT_ROOT", str(tmp_path))
    assert cli_main(["doctor", "--no-smoke"]) == 0
    out = capsys.readouterr().out
    assert "전 항목 통과" in out
