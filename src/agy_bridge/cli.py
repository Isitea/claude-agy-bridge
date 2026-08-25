"""agy-bridge CLI — serve / init / doctor / budget 서브커맨드 (§7.5).

agy 권한 정책 (Phase 0 확정, §9·§10):
전략 A(인라이닝)와 C(루프백 서빙) 모두 헤드리스에서 권한 부여가 불필요하므로
agy 권한 설정을 만들지 않으며, `--dangerously-skip-permissions`는 어떤 경로로도
사용하지 않는다. 헤드리스 권한 자동 거부(§2.3-A)는 runner가 빈 response를
오류로 승격하는 방식으로 감지한다.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

from agy_bridge import __version__

SMOKE_MODEL = "gemini-3.6-flash-low"  # 스모크는 인증·왕복 확인용 — 최저 비용 모델

CONFIG_TEMPLATE = """\
# claude-agy-bridge 설정. 없는 키는 내장 기본값을 쓴다.
# 우선순위: 도구 호출 인자 > 이 파일 > 환경변수 > 내장 기본값.

# model  = "gemini-3.7-flash"      # 검증 독립성을 위해 Claude 계열 모델은 피하라
# effort = "high"                  # low | medium | high (모델 패밀리별로 지원 범위가
#                                  # 다르다 — gemini-3.1-pro는 low·high만)
#
# model에 `gemini-3.1-pro-high`처럼 사고 수준이 박힌 ID를 쓰면 effort는 그 ID가
# 정하며, 어긋나는 effort는 agy가 거부한다. effort로 조절하려면 위처럼 접미사
# 없는 패밀리 ID를 써라. 사용 가능한 ID는 `agy models`.

# [playbooks]
# enabled     = ["units-and-scales", "assumption-validity", "uncertainty-propagation"]
#                                  # 생략하면 mode별 기본 매핑을 쓴다
# overlay_dir = ".agy-bridge/playbooks"

# [limits]
# max_inline_chars  = 100000       # 인라이닝→서빙 자동 전환 임계값
# wait_seconds      = 45           # 동기 대기 창. 넘으면 job 핸들 반환
# print_timeout     = 600          # agy 자체 타임아웃 (초)
# daily_call_budget = 60           # 초과 시 도구가 사유와 함께 거부

# [context]
# 기본 deny_globs는 흔한 키·자격증명(.env*, *.pem, *.key, id_rsa*, .netrc,
# */.ssh/*, */.aws/* 등)과 대형 계산 산출물(*.chk, *.wfn)을 이름·경로로 막는다.
# 아래처럼 지정하면 기본값을 완전히 대체한다 (보강이 아니라 교체다).
# deny_globs = [".env*", "*.pem", "*.key", "id_rsa*", "*.chk", "*.wfn"]
"""

OVERLAY_TEMPLATE = """\
# 프로젝트 오버레이 작성 지침 (이 파일은 주입되지 않는다)

이 디렉터리의 `_` 접두가 아닌 `*.md` 파일은 자동 발견되어 모든 검증 호출에서
내장 플레이북 **뒤에** 주입된다. 브리지를 고치지 않고 이 저장소 고유의 검증
항목을 추가하는 자리다.

무엇을 적는가 — 이 프로젝트에서만 참인 것:
- 이 저장소의 단위계·기준 상태 규약 (예: "내부 에너지는 전부 kJ/mol")
- 자체 자료구조의 불변식
- 팀 내부 검증 기준, 과거 사고에서 얻은 점검 항목

무엇을 적지 않는가:
- 일반 물리·화학 지식 (검증자 모델이 이미 안다)
- 패키지 사용법 (마찬가지)
- 도메인 불변식 (내장 플레이북이 담당)

작성 예 — `units.md`:

    # 이 저장소의 단위 규약
    확인하라:
    - 열역학량은 모듈 경계에서 항상 kJ/mol이어야 한다.
    - 압력 기준 상태는 1 bar다. 1 atm이 보이면 지적하라.
"""

CLAUDE_MD_SNIPPET = """\
## 과학 검증 (agy_consult)

이 저장소에는 독립 검증자 MCP 도구 `agy_consult`가 등록되어 있다.
- 새 수치 기법을 커밋하기 전, 열역학량이 다른 모델로 넘어가는 경계에서
  `mode="verify"`로 검증받아라.
- 검증자는 저장소를 스스로 읽지 못한다 (셸·파일 접근이 헤드리스 자동 거부됨).
  검토 대상은 반드시 files 인자로 전달하고, 질문에 "grep해 봐" 같은
  셸 유도 표현을 쓰지 마라. 웹 검색·URL fetch는 가능하다.
- 같은 주제의 후속 질문은 같은 session_id로 `agy_followup`을 써라 (저렴·정확).
- 반환값은 자문 의견이다 — evidence를 검토한 뒤 반영하라.
- `{"status": "running"}`이 오면 기다리지 말고 다른 작업을 계속하다가
  `agy_result`로 회수하라.
"""

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agy-bridge",
        description=(
            "Claude ↔ Antigravity(agy) 과학 검증 MCP 브리지. "
            "브리지에는 LLM이 없다 — agy 서브프로세스를 실행하고 결과를 중계한다."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="MCP stdio 서버 기동")

    p_init = sub.add_parser(
        "init", help="대상 저장소에 .mcp.json 등록 + .agy-bridge.toml 생성"
    )
    p_init.add_argument(
        "--target",
        help="대상 저장소 경로 (생략 시 현재 디렉터리의 git 루트)",
    )
    p_init.add_argument(
        "--no-smoke", action="store_true", help="스모크 호출(실제 agy 1회) 생략"
    )
    p_init.add_argument(
        "--claude-md",
        action="store_true",
        help="묻지 않고 CLAUDE.md에 사용 지침 스니펫을 반영",
    )

    p_deinit = sub.add_parser(
        "deinit", help="대상 저장소에서 브리지 등록을 해제 (init의 역연산)"
    )
    p_deinit.add_argument(
        "--target", help="대상 저장소 경로 (생략 시 현재 디렉터리의 git 루트)"
    )
    p_deinit.add_argument(
        "--purge-config",
        action="store_true",
        help=".agy-bridge.toml과 오버레이까지 삭제 (기본은 보존)",
    )
    p_deinit.add_argument(
        "--yes", action="store_true", help="확인 없이 실제로 삭제 (기본은 미리보기)"
    )

    p_purge = sub.add_parser(
        "purge", help="런타임 상태(job·세션·원장) 삭제 — 저장소 파일은 손대지 않는다"
    )
    p_purge.add_argument(
        "--all", action="store_true", help="이 머신의 모든 프로젝트 상태를 대상으로"
    )
    p_purge.add_argument(
        "--yes", action="store_true", help="확인 없이 실제로 삭제 (기본은 미리보기)"
    )

    sub.add_parser(
        "update", help="설치된 브리지를 최신으로 갱신 (uv tool upgrade 위임)"
    )

    p_doctor = sub.add_parser(
        "doctor", help="agy 바이너리·인증·상태·예산 자가 진단"
    )
    p_doctor.add_argument(
        "--no-smoke", action="store_true", help="스모크 호출(실제 agy 1회) 생략"
    )

    sub.add_parser("budget", help="일일 호출 예산 사용량 조회")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "serve":
        from agy_bridge.server import serve

        return serve()
    if args.command == "budget":
        return _budget()
    if args.command == "init":
        return _init(args)
    if args.command == "deinit":
        return _deinit(args)
    if args.command == "purge":
        return _purge(args)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "update":
        return _update()
    raise AssertionError(f"등록되지 않은 서브커맨드: {args.command}")


# ── update ──────────────────────────────────────────────


def _update() -> int:
    """uv tool upgrade에 위임한다 — uv가 설치 receipt(git/경로 소스)를 기억한다."""
    import shutil
    import subprocess

    if shutil.which("uv") is None:
        print(
            "agy-bridge update: uv를 찾을 수 없다. 설치 스크립트로 복구하라:\n"
            "  curl -fsSL https://raw.githubusercontent.com/Isitea/"
            "claude-agy-bridge/main/install.sh | bash",
            file=sys.stderr,
        )
        return 1
    proc = subprocess.run(["uv", "tool", "upgrade", "agy-bridge"], check=False)
    if proc.returncode != 0:
        print(
            "agy-bridge update: 갱신 실패. 설치 스크립트로 재설치하면 해결된다:\n"
            "  curl -fsSL https://raw.githubusercontent.com/Isitea/"
            "claude-agy-bridge/main/install.sh | bash",
            file=sys.stderr,
        )
    return proc.returncode


# ── budget ──────────────────────────────────────────────


def _budget() -> int:
    from agy_bridge.budget import Ledger
    from agy_bridge.config import StartupError, load_config

    try:
        config = load_config()
    except StartupError as exc:
        print(f"agy-bridge: {exc}", file=sys.stderr)
        return 1
    report = Ledger(config).report(config.daily_call_budget)
    report["project_root"] = str(config.project_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


# ── init ────────────────────────────────────────────────


def _init(args) -> int:
    from agy_bridge.config import (
        StartupError,
        find_agy_bin,
        find_project_root,
        load_config,
    )

    if args.target:
        target = Path(args.target).expanduser().resolve()
    else:
        target = find_project_root()
        print(f"대상: {target} (현재 디렉터리 기준 자동 판정 — 다른 곳이면 --target 지정)")
    if not target.is_dir():
        print(f"agy-bridge init: 대상이 디렉터리가 아니다: {target}", file=sys.stderr)
        return 1

    try:
        agy_bin = find_agy_bin()
    except StartupError as exc:
        print(f"agy-bridge init: {exc}", file=sys.stderr)
        return 1
    print(f"[1/6] agy 바이너리: {agy_bin}")

    # .mcp.json 병합 — 기존 서버 항목은 보존한다
    mcp_path = target / ".mcp.json"
    mcp_data: dict = {}
    if mcp_path.is_file():
        try:
            mcp_data = json.loads(mcp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(
                f"agy-bridge init: {mcp_path} 파싱 실패 — 손대지 않는다: {exc}",
                file=sys.stderr,
            )
            return 1
    entry: dict = {"command": "agy-bridge", "args": ["serve"]}
    mcp_data.setdefault("mcpServers", {})["agy"] = entry
    mcp_path.write_text(
        json.dumps(mcp_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[2/6] {mcp_path} 에 mcpServers.agy 등록")

    # 설정 템플릿 — 있으면 덮어쓰지 않는다
    config_path = target / ".agy-bridge.toml"
    if config_path.exists():
        print(f"[3/6] {config_path} 이미 존재 — 유지")
    else:
        config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
        print(f"[3/6] {config_path} 생성 (전 항목 주석 = 내장 기본값)")

    # 오버레이 자리 (§8.6)
    overlay_dir = target / ".agy-bridge" / "playbooks"
    overlay_dir.mkdir(parents=True, exist_ok=True)
    template_path = overlay_dir / "_TEMPLATE.md"
    if not template_path.exists():
        template_path.write_text(OVERLAY_TEMPLATE, encoding="utf-8")
    print(f"[4/6] {overlay_dir} 오버레이 자리 생성 (_TEMPLATE.md는 주입되지 않음)")

    # 스모크 — 인증과 왕복을 실제로 확인 (§7.3). 판정은 response 비어있지 않음 (§9)
    if args.no_smoke:
        print("[5/6] 스모크 생략 (--no-smoke)")
    else:
        try:
            config = load_config(target)
        except StartupError as exc:
            # 여기서 터지면 파일은 이미 만들어진 뒤라 저장소가 어중간하게 남는다.
            # 무엇을 고쳐야 하는지 알려 주고 나머지 단계를 건너뛴다.
            print(f"[5/6] 설정을 읽지 못해 스모크를 건너뛴다: {exc}", file=sys.stderr)
            print(
                "      .agy-bridge.toml을 고친 뒤 `agy-bridge doctor`로 확인하라.",
                file=sys.stderr,
            )
            return 1
        error = _smoke(config)
        if error:
            print(f"[5/6] 스모크 실패: {error}", file=sys.stderr)
            return 1
        print(f"[5/6] 스모크 통과 ({SMOKE_MODEL} 왕복, 응답 비어있지 않음)")

    # CLAUDE.md — 승인 없이는 쓰지 않는다 (§9-4). --claude-md 플래그 또는
    # 대화형 y 응답이 승인이다. 비대화형(파이프·CI)에서는 종전대로 제안만 한다.
    wrote = None
    if args.claude_md:
        wrote = _apply_claude_md(target)
    elif sys.stdin.isatty():
        answer = input("CLAUDE.md에 사용 지침 스니펫을 반영할까요? [y/N] ")
        if answer.strip().lower() in ("y", "yes"):
            wrote = _apply_claude_md(target)
    if wrote:
        print(f"[6/6] CLAUDE.md 스니펫 {wrote}")
    else:
        print("[6/6] CLAUDE.md 미변경 — 아래 스니펫 추가를 검토하라:\n")
        print(CLAUDE_MD_SNIPPET)
    return 0


# ── deinit / purge ──────────────────────────────────────
#
# 제거의 원칙 (설치의 대칭):
#   1. 우리가 만든 것만, 만들었을 때만 지운다. 저장소·소스·사용자 저작물은
#      어떤 경로로도 rm -rf 하지 않는다.
#   2. 전제 도구(uv·agy)는 우리가 설치하지 않았으므로 제거도 하지 않는다.
#   3. 기본은 미리보기다. --yes가 있어야 실제로 지운다.
#   4. 브리지 소스 체크아웃에서는 거부한다 — 로컬 테스트 환경을 지키기 위해서다.


def _is_bridge_checkout(target: Path) -> bool:
    """대상이 브리지 자신의 소스 저장소인가. 여기서 deinit을 돌리면 개발·테스트
    환경이 망가지므로 막는다."""
    pyproject = target / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return False
    return 'name = "agy-bridge"' in text


def _resolve_target(args) -> Path | None:
    from agy_bridge.config import find_project_root

    if args.target:
        target = Path(args.target).expanduser().resolve()
    else:
        target = find_project_root()
        print(f"대상: {target} (자동 판정 — 다른 곳이면 --target 지정)")
    if not target.is_dir():
        print(f"agy-bridge: 대상이 디렉터리가 아니다: {target}", file=sys.stderr)
        return None
    return target


def _deinit(args) -> int:
    """init이 만든 것을 되돌린다. 설정·오버레이는 사용자 저작물이라 기본 보존."""
    target = _resolve_target(args)
    if target is None:
        return 1
    if _is_bridge_checkout(target):
        print(
            f"agy-bridge deinit: {target}는 브리지 소스 저장소다 — 거부한다.\n"
            "  여기서 실행하면 개발·테스트 환경이 망가진다. 대상 저장소에서 "
            "실행하거나 --target으로 지정하라.",
            file=sys.stderr,
        )
        return 1

    planned: list[str] = []

    # 1) .mcp.json의 agy 항목만 제거 — 다른 서버는 보존, 파일도 남긴다
    mcp_path = target / ".mcp.json"
    mcp_data: dict = {}
    remove_agy_entry = False
    if mcp_path.is_file():
        try:
            mcp_data = json.loads(mcp_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(
                f"agy-bridge deinit: {mcp_path} 파싱 실패 — 손대지 않는다: {exc}",
                file=sys.stderr,
            )
            return 1
        if "agy" in (mcp_data.get("mcpServers") or {}):
            remove_agy_entry = True
            planned.append(f"{mcp_path}: mcpServers.agy 항목 제거")

    # 2) CLAUDE.md의 우리 절만 제거 — 파일 자체는 지우지 않는다
    claude_md = target / "CLAUDE.md"
    remove_snippet = claude_md.is_file() and _SNIPPET_HEADER in claude_md.read_text(
        encoding="utf-8"
    )
    if remove_snippet:
        planned.append(f"{claude_md}: '{_SNIPPET_HEADER}' 절 제거")

    # 3) init이 만든 템플릿만 제거 (사용자가 쓴 오버레이는 남긴다)
    overlay_dir = target / ".agy-bridge" / "playbooks"
    template = overlay_dir / "_TEMPLATE.md"
    if template.is_file():
        planned.append(f"{template} 삭제 (init이 만든 템플릿)")

    # 4) --purge-config일 때만 설정·오버레이까지
    config_path = target / ".agy-bridge.toml"
    extra: list[Path] = []
    if args.purge_config:
        if config_path.is_file():
            extra.append(config_path)
            planned.append(f"{config_path} 삭제 (--purge-config)")
        overlays = sorted(overlay_dir.glob("*.md")) if overlay_dir.is_dir() else []
        for path in overlays:
            if path.name != "_TEMPLATE.md":
                extra.append(path)
                planned.append(f"{path} 삭제 (--purge-config, 사용자 오버레이)")
    else:
        kept = []
        if config_path.is_file():
            kept.append(config_path.name)
        if overlay_dir.is_dir():
            kept += [
                p.name for p in sorted(overlay_dir.glob("*.md"))
                if p.name != "_TEMPLATE.md"
            ]
        if kept:
            print(f"보존: {', '.join(kept)} (지우려면 --purge-config)")

    if not planned:
        print("제거할 항목이 없다 — 이 저장소에는 브리지 등록이 없다.")
        return 0

    print("\n제거 대상:")
    for line in planned:
        print(f"  - {line}")
    if not args.yes:
        print("\n미리보기다. 실제로 지우려면 --yes를 붙여라.")
        return 0

    if remove_agy_entry:
        del mcp_data["mcpServers"]["agy"]
        mcp_path.write_text(
            json.dumps(mcp_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if remove_snippet:
        import re

        text = claude_md.read_text(encoding="utf-8")
        pattern = re.compile(
            rf"{re.escape(_SNIPPET_HEADER)}.*?(?=^## |\Z)", re.DOTALL | re.MULTILINE
        )
        remaining = pattern.sub("", text).strip()
        if remaining:
            claude_md.write_text(remaining + "\n", encoding="utf-8")
        else:
            # 우리 절만 있던 파일 = init이 만든 파일. 빈 껍데기를 남기지 않는다.
            claude_md.unlink()
            print(f"  ({claude_md.name}은 우리 절만 있어 파일째 정리)")
    if template.is_file():
        template.unlink()
    for path in extra:
        with contextlib.suppress(OSError):
            path.unlink()
    # 빈 껍데기 디렉터리만 정리 (내용이 남아 있으면 그대로 둔다)
    for directory in (overlay_dir, overlay_dir.parent):
        with contextlib.suppress(OSError):
            directory.rmdir()

    print("\n완료. 런타임 상태는 `agy-bridge purge`로, 전역 바이너리는 "
          "`uv tool uninstall agy-bridge`로 각각 제거한다.")
    return 0


def _purge(args) -> int:
    """런타임 상태(job·세션·원장)를 지운다. 저장소 파일은 건드리지 않는다."""
    from agy_bridge.config import (
        _cache_root,
        find_project_root,
        read_state_meta,
        state_dir_for,
    )
    from agy_bridge.jobs import TERMINAL_STATES, JobRegistry

    cache_root = _cache_root()
    if args.all:
        targets = sorted(p for p in cache_root.glob("*") if p.is_dir())
        if not targets:
            print(f"정리할 상태 디렉터리가 없다 ({cache_root}).")
            return 0
    else:
        targets = [state_dir_for(find_project_root())]
        if not targets[0].is_dir():
            print("이 프로젝트의 상태 디렉터리가 없다 — 지울 것이 없다.")
            return 0

    print(f"상태 디렉터리 ({cache_root}):\n")
    removable: list[Path] = []
    for state_dir in targets:
        meta = read_state_meta(state_dir)
        origin = meta.get("project_root", "(기록 없음 — 옛 버전이 만든 디렉터리)")
        running = _running_jobs(state_dir, JobRegistry, TERMINAL_STATES)
        size_kb = sum(
            f.stat().st_size for f in state_dir.rglob("*") if f.is_file()
        ) // 1024
        flag = f"  ★실행 중 job {len(running)}건★" if running else ""
        print(f"  {state_dir.name}  {size_kb:,} KB  ← {origin}{flag}")
        if running:
            print(f"      {', '.join(running)} — 먼저 agy_cancel로 정리하라")
        else:
            removable.append(state_dir)

    if not removable:
        print("\n지울 수 있는 디렉터리가 없다 (실행 중 job이 있는 곳은 건너뛴다).")
        return 1 if targets else 0
    if not args.yes:
        print(f"\n미리보기다. {len(removable)}개를 실제로 지우려면 --yes를 붙여라.")
        return 0

    import shutil

    for state_dir in removable:
        shutil.rmtree(state_dir, ignore_errors=True)
    print(f"\n{len(removable)}개 상태 디렉터리를 삭제했다. "
          "저장소 파일과 agy·uv는 그대로다.")
    return 0


def _running_jobs(state_dir: Path, registry_cls, terminal_states) -> list[str]:
    """해당 상태 디렉터리에서 아직 종결되지 않은 job id 목록."""
    jobs_dir = state_dir / "jobs"
    if not jobs_dir.is_dir():
        return []
    running = []
    for entry in sorted(jobs_dir.glob("j-*.json")):
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict) and record.get("state") not in terminal_states:
            running.append(entry.stem)
    return running


_SNIPPET_HEADER = "## 과학 검증 (agy_consult)"


def _apply_claude_md(target: Path) -> str:
    """스니펫을 CLAUDE.md에 반영한다. 기존 절이 있으면 교체해 중복을 막는다."""
    import re

    path = target / "CLAUDE.md"
    if not path.is_file():
        path.write_text(CLAUDE_MD_SNIPPET, encoding="utf-8")
        return "생성 (CLAUDE.md 새로 만듦)"
    text = path.read_text(encoding="utf-8")
    if _SNIPPET_HEADER in text:
        pattern = re.compile(
            rf"{re.escape(_SNIPPET_HEADER)}.*?(?=^## |\Z)", re.DOTALL | re.MULTILINE
        )
        path.write_text(pattern.sub(CLAUDE_MD_SNIPPET + "\n", text), encoding="utf-8")
        return "교체 (기존 절 갱신)"
    path.write_text(text.rstrip() + "\n\n" + CLAUDE_MD_SNIPPET, encoding="utf-8")
    return "추가"


def _smoke(config) -> str | None:
    """실제 agy 왕복 1회. 성공이면 None, 실패면 조치 가능한 메시지."""
    from agy_bridge.runner import AgyError, run_agy

    try:
        result = run_agy(
            "스모크 테스트다. 'OK'라고만 답하라.",
            config=config,
            model=SMOKE_MODEL,
            effort="low",
        )
    except AgyError as exc:
        return (
            f"{exc}\n조치: `agy models`로 인증 상태를 확인하라. "
            "OAuth가 만료됐으면 agy를 대화형으로 한 번 실행해 재인증하라."
        )
    if not result.response.strip():
        return "응답이 비어 있다 — §2.3-A 침묵 실패. stderr를 확인하라."
    return None


# ── doctor ──────────────────────────────────────────────


def _doctor(args) -> int:
    """자가 진단 (§9-3). 실패 항목은 조치 가능한 문장으로 출력한다."""
    from agy_bridge.budget import Ledger
    from agy_bridge.config import StartupError, find_agy_bin, load_config
    from agy_bridge.prompts import (
        BUILTIN_PLAYBOOKS,
        discover_overlays,
        load_builtin_playbook,
    )

    failures = 0

    def check(name: str, ok: bool, detail: str):
        nonlocal failures
        print(f"  [{'OK' if ok else 'FAIL'}] {name}: {detail}")
        if not ok:
            failures += 1

    print(f"agy-bridge {__version__} doctor\n")

    try:
        agy_bin = find_agy_bin()
        check("agy 바이너리", True, agy_bin)
    except StartupError as exc:
        check("agy 바이너리", False, str(exc))
        print(f"\n진단 결과: 실패 {failures}건")
        return 1

    try:
        config = load_config()
        check("프로젝트 루트", True, str(config.project_root))
    except StartupError as exc:
        check("설정 로드", False, str(exc))
        print(f"\n진단 결과: 실패 {failures}건")
        return 1

    # 루트가 홈·파일시스템 루트면 project_root 봉쇄(§10)가 사실상 무력해진다 —
    # 그 안의 ~/.ssh 등이 '루트 안쪽'으로 취급되기 때문. FAIL은 아니지만 경고한다.
    root = config.project_root.resolve()
    if root == Path.home().resolve() or root == Path(root.anchor):
        print(
            f"  [WARN] 프로젝트 루트가 {root}다 — files 봉쇄가 약해진다. "
            ".git이 있는 실제 저장소에서 실행하거나 AGY_BRIDGE_PROJECT_ROOT로 "
            "좁은 루트를 지정하라 (deny_globs가 흔한 자격증명은 계속 막는다)."
        )

    probe = config.state_dir / ".doctor-probe"
    try:
        probe.write_text("ok")
        probe.unlink()
        check("상태 디렉터리 쓰기", True, str(config.state_dir))
    except OSError as exc:
        check("상태 디렉터리 쓰기", False, f"{config.state_dir}: {exc}")

    # 이 설정에서 **실제로 실릴** 목록을 검사한다. 내장 7종만 확인하면
    # [playbooks] enabled의 오타를 못 보고 "통과"를 찍는다 — 그 상태로는 모든
    # 호출이 실패하므로 doctor가 거짓 안심을 주는 셈이다(자체 리뷰).
    selected = config.playbooks_enabled or BUILTIN_PLAYBOOKS
    scope = (
        f"설정 지정 {len(selected)}종"
        if config.playbooks_enabled
        else f"내장 {len(BUILTIN_PLAYBOOKS)}종"
    )
    try:
        for name in selected:
            load_builtin_playbook(name)
        overlays = discover_overlays(config.project_root, config.overlay_dir)
        check(
            "플레이북",
            True,
            f"{scope} + 오버레이 {len(overlays)}건"
            + (f" ({', '.join(n for n, _ in overlays)})" if overlays else ""),
        )
    except (OSError, ValueError) as exc:
        check("플레이북", False, str(exc))

    report = Ledger(config).report(config.daily_call_budget)
    check(
        "호출 예산",
        report["remaining"] > 0,
        f"오늘 {report['calls_started']}회 사용 / 상한 {report['daily_call_budget']}회"
        + ("" if report["remaining"] > 0 else " — 소진. 자정 초기화 또는 상한 조정"),
    )

    # 유휴 서버는 브리지 프로세스와 함께 소멸하므로 잔존할 수 없다 (§10.1 —
    # 회귀 테스트가 보증). 여기서는 회수 안 된 고아 job만 점검한다.
    from agy_bridge.jobs import JobRegistry

    registry = JobRegistry(config)
    stale = [r.job_id for r in registry.list_jobs() if r.state == "running"]
    check(
        "미회수 job",
        True,
        f"running {len(stale)}건"
        + (f" ({', '.join(stale)}) — agy_result 호출 시 자동 회수됨" if stale else ""),
    )

    if args.no_smoke:
        print("  [SKIP] 스모크 (--no-smoke)")
    else:
        error = _smoke(config)
        check("스모크 (인증·왕복)", error is None, error or f"{SMOKE_MODEL} 응답 정상")

    print(f"\n진단 결과: {'전 항목 통과' if failures == 0 else f'실패 {failures}건'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
