"""설정 해석과 경로 판정 (§7.4).

우선순위: 도구 호출 인자 > 대상 저장소 .agy-bridge.toml > 환경변수 > 내장 기본값.
agy 바이너리는 기동 시점에 찾지 못하면 즉시 실패한다 — 도구 호출 시점에 실패하면
원인 파악이 어렵다 (§7.4).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILENAME = ".agy-bridge.toml"

# 검증 독립성을 위해 기본 모델은 gemini 계열로 고정한다.
# agy를 통해 Claude 모델을 부르면 "다른 관점의 검증"이라는 목적이 훼손된다 (§2.1).
DEFAULT_MODEL = "gemini-3.1-pro-high"
DEFAULT_EFFORT = "high"
DEFAULT_MAX_INLINE_CHARS = 100_000  # argv 단일 인자 131,072 B 한계 대비 마진 (§2.3-D)
DEFAULT_WAIT_SECONDS = 45           # 동기 대기 창 — 넘으면 job 핸들 반환 (§5)
DEFAULT_PRINT_TIMEOUT = 600         # agy --print-timeout, 초 (§5)
DEFAULT_HARD_KILL_SECONDS = 900     # 브리지의 최종 안전망 (§5)
DEFAULT_DAILY_CALL_BUDGET = 60      # 초과 시 스폰 전에 거부 (§13, budget.py)
DEFAULT_DENY_GLOBS = (".env*", "*_key*", "*token*", "*.pem", "*.chk", "*.wfn")

VALID_EFFORTS = ("low", "medium", "high")


class StartupError(RuntimeError):
    """기동을 중단해야 하는 설정 오류. 메시지는 조치 가능한 문장으로 쓴다 (§9)."""


@dataclass(frozen=True)
class Config:
    project_root: Path
    state_dir: Path
    scratch_dir: Path  # agy 실행 CWD — 대상 저장소 AGENTS.md 상속 차단 (§8.2)
    agy_bin: str
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    max_inline_chars: int = DEFAULT_MAX_INLINE_CHARS
    wait_seconds: int = DEFAULT_WAIT_SECONDS
    print_timeout: int = DEFAULT_PRINT_TIMEOUT
    hard_kill_seconds: int = DEFAULT_HARD_KILL_SECONDS
    daily_call_budget: int = DEFAULT_DAILY_CALL_BUDGET
    deny_globs: tuple[str, ...] = DEFAULT_DENY_GLOBS
    # None이면 mode → 플레이북 정적 매핑을 쓴다. 값이 있으면 그것으로 덮어쓴다 (§8.5).
    playbooks_enabled: tuple[str, ...] | None = None
    overlay_dir: str = ".agy-bridge/playbooks"  # 프로젝트 오버레이 위치 (§8.6)


def find_project_root(cwd: Path | None = None) -> Path:
    """MCP 서버가 기동된 CWD에서 .git까지 거슬러 올라가 프로젝트 루트를 판정한다."""
    override = os.environ.get("AGY_BRIDGE_PROJECT_ROOT")
    if override:
        root = Path(override).expanduser().resolve()
        if not root.is_dir():
            raise StartupError(
                f"AGY_BRIDGE_PROJECT_ROOT가 디렉터리가 아니다: {root}"
            )
        return root
    current = (cwd or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    # .git이 없으면 CWD 자체를 루트로 삼는다. 상태 디렉터리가 경로 해시로
    # 분리되므로 안전하며, 경고는 doctor의 몫이다.
    return current


def find_agy_bin() -> str:
    """AGY_BIN → PATH 순으로 탐색, 없으면 기동 시점에 명확히 실패 (§7.4)."""
    env_bin = os.environ.get("AGY_BIN")
    if env_bin:
        path = Path(env_bin).expanduser()
        if not (path.is_file() and os.access(path, os.X_OK)):
            raise StartupError(f"AGY_BIN이 실행 파일을 가리키지 않는다: {env_bin}")
        return str(path)
    found = shutil.which("agy")
    if found:
        return found
    raise StartupError(
        "agy 바이너리를 찾을 수 없다. Antigravity CLI를 설치해 PATH에 넣거나, "
        "AGY_BIN 환경변수로 경로를 지정하라. 확인: `agy models`"
    )


def _cache_root() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "claude-agy-bridge"


def state_dir_for(project_root: Path) -> Path:
    """프로젝트별 상태 격리: 경로의 안정 해시로 디렉터리를 나눈다 (§7.4)."""
    digest = hashlib.sha256(str(project_root).encode("utf-8")).hexdigest()[:16]
    return _cache_root() / digest


def load_config(cwd: Path | None = None) -> Config:
    root = find_project_root(cwd)

    raw: dict = {}
    config_file = root / CONFIG_FILENAME
    if config_file.is_file():
        try:
            raw = tomllib.loads(config_file.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise StartupError(f"{config_file} 파싱 실패: {exc}") from exc

    limits = raw.get("limits", {})
    context = raw.get("context", {})
    playbooks = raw.get("playbooks", {})
    env = os.environ

    model = raw.get("model") or env.get("AGY_BRIDGE_MODEL") or DEFAULT_MODEL
    effort = raw.get("effort") or env.get("AGY_BRIDGE_EFFORT") or DEFAULT_EFFORT
    if effort not in VALID_EFFORTS:
        raise StartupError(f"effort는 {VALID_EFFORTS} 중 하나여야 한다: {effort!r}")

    deny = context.get("deny_globs")
    deny_globs = tuple(deny) if deny else DEFAULT_DENY_GLOBS

    state_dir = state_dir_for(root)
    scratch_dir = state_dir / "scratch"
    for directory in (state_dir, scratch_dir, state_dir / "jobs"):
        directory.mkdir(parents=True, exist_ok=True)

    return Config(
        project_root=root,
        state_dir=state_dir,
        scratch_dir=scratch_dir,
        agy_bin=find_agy_bin(),
        model=model,
        effort=effort,
        max_inline_chars=int(limits.get("max_inline_chars", DEFAULT_MAX_INLINE_CHARS)),
        wait_seconds=int(limits.get("wait_seconds", DEFAULT_WAIT_SECONDS)),
        print_timeout=int(limits.get("print_timeout", DEFAULT_PRINT_TIMEOUT)),
        hard_kill_seconds=int(limits.get("hard_kill_seconds", DEFAULT_HARD_KILL_SECONDS)),
        daily_call_budget=int(limits.get("daily_call_budget", DEFAULT_DAILY_CALL_BUDGET)),
        deny_globs=deny_globs,
        playbooks_enabled=(
            tuple(playbooks["enabled"]) if "enabled" in playbooks else None
        ),
        overlay_dir=playbooks.get("overlay_dir", ".agy-bridge/playbooks"),
    )
