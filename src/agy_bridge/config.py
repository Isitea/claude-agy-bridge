"""설정 해석과 경로 판정 (§7.4).

우선순위: 도구 호출 인자 > 대상 저장소 .agy-bridge.toml > 환경변수 > 내장 기본값.
agy 바이너리는 기동 시점에 찾지 못하면 즉시 실패한다 — 도구 호출 시점에 실패하면
원인 파악이 어렵다 (§7.4).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILENAME = ".agy-bridge.toml"

# 검증 독립성을 위해 기본 모델은 gemini 계열로 고정한다.
# agy를 통해 Claude 모델을 부르면 "다른 관점의 검증"이라는 목적이 훼손된다 (§2.1).
#
# 모델 ID는 접미사 없는 패밀리 ID로 둔다. agy는 `gemini-3.1-pro-high`처럼 사고
# 수준이 박힌 ID와 `--effort`를 동시에 받으면 충돌로 거부한다 (실측):
#   --model gemini-3.1-pro-high --effort low
#     → invalid model selection: ... conflicts with --effort=low
# 패밀리 ID를 기본값으로 두어야 effort가 실제로 조절 가능한 노브가 된다.
# 패밀리가 지원하지 않는 값(gemini-3.1-pro의 medium 등)은 agy가 available 목록과
# 함께 거부하므로, 브리지가 패밀리별 표를 들고 있지 않는다 — 썩을 표다.
# flash 패밀리는 low·medium·high 셋을 모두 지원해 effort 조절 폭이 가장 넓다.
DEFAULT_MODEL = "gemini-3.7-flash"
DEFAULT_EFFORT = "high"
DEFAULT_MAX_INLINE_CHARS = 100_000  # argv 단일 인자 131,072 B 한계 대비 마진 (§2.3-D)
DEFAULT_WAIT_SECONDS = 45           # 동기 대기 창 — 넘으면 job 핸들 반환 (§5)
DEFAULT_PRINT_TIMEOUT = 600         # agy --print-timeout, 초 (§5)
DEFAULT_HARD_KILL_SECONDS = 900     # 브리지의 최종 안전망 (§5)
DEFAULT_DAILY_CALL_BUDGET = 60      # 초과 시 스폰 전에 거부 (§13, budget.py)
# 인라이닝·서빙 양쪽에 적용되는 자격증명 차단 목록 (§10). project_root 봉쇄가
# 1차 방어선이고 이 목록이 '루트 안쪽'의 자격증명을 거르는 2차 방어선이다.
# find_project_root가 .git 부재 시 CWD/홈을 루트로 삼는 구성에서는 봉쇄가
# 무력해질 수 있으므로, 흔한 키·자격증명 파일명을 이름·경로로 폭넓게 막는다.
# _is_denied는 파일명과 전체 경로 양쪽에 fnmatch를 적용한다.
DEFAULT_DENY_GLOBS = (
    ".env*",
    "*.pem", "*.key", "*.p12", "*.pfx",       # 개인키·인증서 번들
    "*_key*", "*token*", "*secret*", "*credential*",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",  # SSH 개인키
    ".netrc", ".npmrc", ".pgpass", ".htpasswd", ".git-credentials",
    "*/.ssh/*", "*/.aws/*", "*/.gnupg/*",     # 자격증명 디렉터리 (경로 매칭)
    "*/.config/gh/*", "*/.docker/config.json",
    "*.chk", "*.wfn",                          # 대형 계산 산출물 (§4.3 실측)
)

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


STATE_META_FILENAME = "meta.json"


def write_state_meta(state_dir: Path, project_root: Path) -> None:
    """상태 디렉터리에 어느 프로젝트 것인지 남긴다.

    디렉터리 이름은 경로의 sha256이라 역산이 안 된다. 이 표식이 없으면
    `agy-bridge purge`가 무엇을 지우는지 보여줄 수 없고, 사용자는 캐시에 쌓인
    해시 디렉터리들의 정체를 영영 알 수 없다.
    """
    meta_path = state_dir / STATE_META_FILENAME
    payload = {"project_root": str(project_root), "version": 1}
    # JSONDecodeError(ValueError)까지 삼켜야 한다. OSError만 막으면 손상된
    # meta.json 하나가 load_config를 raw traceback으로 죽이고, 그 경로에 있는
    # serve·doctor·budget·init이 전부 기동조차 못 한다 — 정작 이 파일은 purge
    # 표시용 정보 파일이라 다시 쓰면 그만이다 (_int_limit과 같은 교훈).
    # dict가 아닌 JSON(배열 등)이면 .get에서 AttributeError가 나므로 함께 막는다.
    with contextlib.suppress(OSError, json.JSONDecodeError):
        if meta_path.is_file():
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
            if (
                isinstance(existing, dict)
                and existing.get("project_root") == payload["project_root"]
            ):
                return
    with contextlib.suppress(OSError):
        tmp = meta_path.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(meta_path)


def read_state_meta(state_dir: Path) -> dict:
    """write_state_meta가 남긴 표식. 없거나 손상이면 빈 dict."""
    try:
        data = json.loads(
            (state_dir / STATE_META_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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

    if not isinstance(model, str) or not isinstance(effort, str):
        raise StartupError("model·effort는 문자열이어야 한다.")

    deny_globs = _string_list(context.get("deny_globs"), "[context] deny_globs") \
        or DEFAULT_DENY_GLOBS
    playbooks_enabled = _validated_playbooks(
        _string_list(playbooks.get("enabled"), "[playbooks] enabled")
    )

    state_dir = state_dir_for(root)
    scratch_dir = state_dir / "scratch"
    for directory in (state_dir, scratch_dir, state_dir / "jobs"):
        directory.mkdir(parents=True, exist_ok=True)
    write_state_meta(state_dir, root)

    return Config(
        project_root=root,
        state_dir=state_dir,
        scratch_dir=scratch_dir,
        agy_bin=find_agy_bin(),
        model=model,
        effort=effort,
        max_inline_chars=_int_limit(
            limits, "max_inline_chars", DEFAULT_MAX_INLINE_CHARS, minimum=1
        ),
        wait_seconds=_int_limit(limits, "wait_seconds", DEFAULT_WAIT_SECONDS, minimum=0),
        print_timeout=_int_limit(
            limits, "print_timeout", DEFAULT_PRINT_TIMEOUT, minimum=1
        ),
        # 0이면 스폰 직후 하드 킬돼 어떤 호출도 성공할 수 없다
        hard_kill_seconds=_int_limit(
            limits, "hard_kill_seconds", DEFAULT_HARD_KILL_SECONDS, minimum=1
        ),
        daily_call_budget=_int_limit(
            limits, "daily_call_budget", DEFAULT_DAILY_CALL_BUDGET, minimum=0
        ),
        deny_globs=deny_globs,
        playbooks_enabled=playbooks_enabled,
        overlay_dir=_validated_overlay_dir(
            playbooks.get("overlay_dir", ".agy-bridge/playbooks"), root
        ),
    )


def _int_limit(limits: dict, key: str, default: int, *, minimum: int) -> int:
    """[limits] 값은 설정 오류로 다뤄야 한다 — int()를 그대로 쓰면 ValueError가
    StartupError 처리기를 지나쳐 raw traceback으로 터진다. 범위도 함께 막는다."""
    value = limits.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise StartupError(
            f"[limits] {key}는 정수여야 한다: {value!r} "
            f"({CONFIG_FILENAME}을 확인하라)"
        )
    if value < minimum:
        raise StartupError(f"[limits] {key}는 {minimum} 이상이어야 한다: {value}")
    return value


def _string_list(value: object, label: str) -> tuple[str, ...] | None:
    """문자열 하나를 주면 tuple()이 문자 단위로 쪼개 ('.', 'e', …)가 된다.
    deny_globs에서 이러면 '*'가 섞여 들어가 모든 파일이 차단된다."""
    if value is None:
        return None
    if isinstance(value, str):
        raise StartupError(
            f"{label}은 문자열이 아니라 목록이어야 한다 (예: [\"{value}\"]). "
            "문자열 하나를 주면 문자 단위로 쪼개진다."
        )
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise StartupError(f"{label}은 문자열 목록이어야 한다: {value!r}")
    return tuple(value)


def _validated_playbooks(names: tuple[str, ...] | None) -> tuple[str, ...] | None:
    """[playbooks] enabled의 이름이 실제로 존재하는지 기동 시점에 확인한다.

    이름 검사가 없으면 오타 하나가 기동을 통과하고, 그 뒤 **모든** 호출이
    load_builtin_playbook의 ValueError로 죽는다. 설정 오류는 기동에서 잡는다는
    §7.4 원칙 그대로다 — 도구 호출 시점에 터지면 원인 파악이 어렵고, doctor는
    내장 목록만 확인하므로 "전 항목 통과"라는 거짓 안심까지 준다.
    """
    if names is None:
        return None
    # 지역 import — prompts는 config를 쓰지 않으므로 순환은 없지만, 설정 해석이
    # 플레이북 로딩 모듈에 기동 시점부터 묶이지는 않게 둔다.
    from agy_bridge.prompts import BUILTIN_PLAYBOOKS

    unknown = [name for name in names if name not in BUILTIN_PLAYBOOKS]
    if unknown:
        raise StartupError(
            f"[playbooks] enabled에 알 수 없는 플레이북이 있다: {unknown}. "
            f"내장 목록: {list(BUILTIN_PLAYBOOKS)} ({CONFIG_FILENAME}을 확인하라)"
        )
    return names


def _validated_overlay_dir(overlay_dir: str, root: Path) -> str:
    """오버레이 디렉터리는 저장소 안이어야 한다 (§10).

    이 값은 대상 저장소의 설정 파일에서 오고, 여기 담긴 *.md는 전부 프롬프트에
    실려 외부 모델로 나간다. 절대경로나 ../ 를 허용하면 files 인자에 넣어 둔
    봉쇄를 우회하는 두 번째 유출 경로가 된다.
    """
    if not isinstance(overlay_dir, str):
        raise StartupError(f"[playbooks] overlay_dir은 문자열이어야 한다: {overlay_dir!r}")
    resolved = (root / overlay_dir).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise StartupError(
            f"[playbooks] overlay_dir이 프로젝트 루트({root}) 밖을 가리킨다: "
            f"{overlay_dir!r}. 오버레이 내용은 전부 검증자에게 전송되므로 "
            "저장소 안쪽 상대 경로만 허용한다 (§10)."
        )
    return overlay_dir
