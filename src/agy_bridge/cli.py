"""agy-bridge CLI — serve / init / doctor / budget 서브커맨드 (§7.5).

agy 권한 정책 (Phase 0 확정, §9·§10):
전략 A(인라이닝)와 C(루프백 서빙) 모두 헤드리스에서 권한 부여가 불필요하므로
agy 권한 설정을 만들지 않으며, `--dangerously-skip-permissions`는 어떤 경로로도
사용하지 않는다. 헤드리스 권한 자동 거부(§2.3-A)는 runner가 빈 response를
오류로 승격하는 방식으로 감지한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agy_bridge import __version__

SMOKE_MODEL = "gemini-3.6-flash-low"  # 스모크는 인증·왕복 확인용 — 최저 비용 모델

CONFIG_TEMPLATE = """\
# claude-agy-bridge 설정. 없는 키는 내장 기본값을 쓴다.
# 우선순위: 도구 호출 인자 > 이 파일 > 환경변수 > 내장 기본값.

# model  = "gemini-3.1-pro-high"   # 검증 독립성을 위해 Claude 계열 모델은 피하라
# effort = "high"                  # low | medium | high

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
    p_init.add_argument("--profile", help="AGY_BRIDGE_PROFILE 환경변수로 기록할 이름")
    p_init.add_argument(
        "--no-smoke", action="store_true", help="스모크 호출(실제 agy 1회) 생략"
    )
    p_init.add_argument(
        "--claude-md",
        action="store_true",
        help="묻지 않고 CLAUDE.md에 사용 지침 스니펫을 반영",
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
    if args.profile:
        entry["env"] = {"AGY_BRIDGE_PROFILE": args.profile}
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
        error = _smoke(load_config(target))
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

    try:
        for name in BUILTIN_PLAYBOOKS:
            load_builtin_playbook(name)
        overlays = discover_overlays(config.project_root, config.overlay_dir)
        check(
            "플레이북",
            True,
            f"내장 {len(BUILTIN_PLAYBOOKS)}종 + 오버레이 {len(overlays)}건"
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
