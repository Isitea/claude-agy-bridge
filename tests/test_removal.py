"""제거 프로세스 검증 (deinit / purge / uninstall.sh).

절대 규칙: 제거는 **브리지가 만든 것만** 지운다. 저장소·소스 체크아웃·사용자
저작물은 어떤 경로로도 삭제되지 않는다.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agy_bridge.cli import main as cli_main
from agy_bridge.config import read_state_meta, state_dir_for, write_state_meta


def _init_repo(target: Path) -> None:
    assert cli_main(
        ["init", "--target", str(target), "--no-smoke", "--claude-md"]
    ) == 0


class TestDeinit:
    def _repo(self, tmp_path, monkeypatch) -> Path:
        monkeypatch.setenv("AGY_BIN", "/bin/true")
        target = tmp_path / "repo"
        target.mkdir()
        _init_repo(target)
        return target

    def test_preview_does_not_touch_anything(self, tmp_path, monkeypatch, capsys):
        target = self._repo(tmp_path, monkeypatch)
        assert cli_main(["deinit", "--target", str(target)]) == 0
        assert "미리보기" in capsys.readouterr().out
        # 미리보기는 아무것도 바꾸지 않는다
        mcp = json.loads((target / ".mcp.json").read_text())
        assert "agy" in mcp["mcpServers"]
        assert (target / ".agy-bridge" / "playbooks" / "_TEMPLATE.md").is_file()

    def test_removes_only_bridge_artifacts(self, tmp_path, monkeypatch):
        target = self._repo(tmp_path, monkeypatch)
        # 사용자 저작물과 남의 MCP 서버를 함께 둔다
        overlay = target / ".agy-bridge" / "playbooks" / "team.md"
        overlay.write_text("# 팀 규칙\n")
        mcp_path = target / ".mcp.json"
        data = json.loads(mcp_path.read_text())
        data["mcpServers"]["other"] = {"command": "other-server"}
        mcp_path.write_text(json.dumps(data))
        (target / "CLAUDE.md").write_text(
            "# 기존 지침\n\n## 빌드\n내용\n\n"
            + (target / "CLAUDE.md").read_text()
        )

        assert cli_main(["deinit", "--target", str(target), "--yes"]) == 0

        mcp = json.loads(mcp_path.read_text())
        assert "agy" not in mcp["mcpServers"]
        assert "other" in mcp["mcpServers"]          # 남의 항목은 보존
        claude = (target / "CLAUDE.md").read_text()
        assert "## 과학 검증 (agy_consult)" not in claude
        assert "## 빌드" in claude                    # 파일도 다른 절도 보존
        assert overlay.is_file()                      # 사용자 오버레이 보존
        assert (target / ".agy-bridge.toml").is_file()  # 설정 보존
        assert not (target / ".agy-bridge" / "playbooks" / "_TEMPLATE.md").exists()

    def test_purge_config_removes_settings_and_overlays(self, tmp_path, monkeypatch):
        target = self._repo(tmp_path, monkeypatch)
        (target / ".agy-bridge" / "playbooks" / "team.md").write_text("# 팀\n")

        assert cli_main(
            ["deinit", "--target", str(target), "--purge-config", "--yes"]
        ) == 0
        assert not (target / ".agy-bridge.toml").exists()
        assert not (target / ".agy-bridge").exists()  # 빈 껍데기까지 정리

    def test_refuses_inside_bridge_checkout(self, tmp_path, monkeypatch, capsys):
        """소스 체크아웃에서 돌리면 개발·테스트 환경이 망가진다."""
        monkeypatch.setenv("AGY_BIN", "/bin/true")
        fake_checkout = tmp_path / "claude-agy-bridge"
        fake_checkout.mkdir()
        (fake_checkout / "pyproject.toml").write_text(
            '[project]\nname = "agy-bridge"\nversion = "0.0.0"\n'
        )
        assert cli_main(["deinit", "--target", str(fake_checkout), "--yes"]) == 1
        assert "브리지 소스 저장소" in capsys.readouterr().err

    def test_claude_md_created_by_init_is_removed_when_empty(
        self, tmp_path, monkeypatch
    ):
        """우리 절만 있던 CLAUDE.md(=init이 만든 파일)는 빈 껍데기로 남기지 않는다."""
        target = self._repo(tmp_path, monkeypatch)
        assert (target / "CLAUDE.md").is_file()
        assert cli_main(["deinit", "--target", str(target), "--yes"]) == 0
        assert not (target / "CLAUDE.md").exists()

    def test_noop_on_unregistered_repo(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("AGY_BIN", "/bin/true")
        target = tmp_path / "plain"
        target.mkdir()
        assert cli_main(["deinit", "--target", str(target), "--yes"]) == 0
        assert "제거할 항목이 없다" in capsys.readouterr().out


class TestPurge:
    def _state(self, tmp_path, monkeypatch, name="proj") -> Path:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        root = tmp_path / name
        root.mkdir()
        state_dir = state_dir_for(root)
        (state_dir / "jobs").mkdir(parents=True)
        write_state_meta(state_dir, root)
        return state_dir

    def test_meta_records_origin(self, tmp_path, monkeypatch):
        """해시 디렉터리는 역산이 안 된다 — 표식이 없으면 무엇을 지우는지 못 보여준다."""
        state_dir = self._state(tmp_path, monkeypatch)
        assert read_state_meta(state_dir)["project_root"].endswith("proj")

    def test_preview_lists_origin_without_deleting(
        self, tmp_path, monkeypatch, capsys
    ):
        state_dir = self._state(tmp_path, monkeypatch)
        assert cli_main(["purge", "--all"]) == 0
        out = capsys.readouterr().out
        assert "proj" in out and "미리보기" in out
        assert state_dir.is_dir()

    def test_deletes_with_yes(self, tmp_path, monkeypatch):
        state_dir = self._state(tmp_path, monkeypatch)
        assert cli_main(["purge", "--all", "--yes"]) == 0
        assert not state_dir.exists()

    def test_refuses_while_jobs_are_running(self, tmp_path, monkeypatch, capsys):
        """실행 중 job의 원장을 지우면 예산 계측이 깨지고 프로세스는 고아가 된다."""
        state_dir = self._state(tmp_path, monkeypatch)
        (state_dir / "jobs" / "j-1.json").write_text(
            json.dumps({"job_id": "j-1", "state": "running", "mode": "review",
                        "question_head": "q", "session_id": None, "pid": 1,
                        "created_at": 0.0})
        )
        assert cli_main(["purge", "--all", "--yes"]) == 1
        assert "실행 중" in capsys.readouterr().out
        assert state_dir.is_dir()      # 지우지 않았다


def test_uninstall_script_syntax_and_policy():
    """uninstall.sh는 저장소를 지우지 않고 전제 도구도 제거하지 않는다."""
    script = Path(__file__).parent.parent / "uninstall.sh"
    assert script.is_file()
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr

    text = script.read_text(encoding="utf-8")
    assert "rm -rf" not in text                       # 디렉터리를 통째로 지우지 않는다
    assert "uv tool uninstall agy-bridge" in text
    assert "uninstall uv" not in text and "uv self uninstall" not in text
