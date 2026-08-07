"""agy-bridge CLI — serve / init / doctor / budget 서브커맨드 (§7.5).

agy 권한 정책 (Phase 0 확정, §9·§10):
전략 A(인라이닝)와 C(루프백 서빙) 모두 헤드리스에서 권한 부여가 불필요하므로
agy 권한 설정을 만들지 않으며, `--dangerously-skip-permissions`는 어떤 경로로도
사용하지 않는다. 헤드리스 권한 자동 거부(§2.3-A)는 runner가 빈 response를
오류로 승격하는 방식으로 감지한다.
"""

from __future__ import annotations

import argparse
import sys

from agy_bridge import __version__

_PHASE_HINT = "아직 구현되지 않았습니다. 로드맵은 docs/plan.md §11 참조."


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

    sub.add_parser("serve", help="MCP stdio 서버 기동 (Phase 1)")

    p_init = sub.add_parser(
        "init", help="대상 저장소에 .mcp.json 등록 + .agy-bridge.toml 생성 (Phase 6)"
    )
    p_init.add_argument("--target", required=True, help="대상 저장소 경로")
    p_init.add_argument("--profile", help="도메인 프로파일 이름 (예: quantum-chemistry)")

    sub.add_parser("doctor", help="agy 바이너리·인증·유휴 서버 잔존 점검 (Phase 6)")

    sub.add_parser("budget", help="일일 호출 예산 사용량 조회 (Phase 5)")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    print(f"agy-bridge {args.command}: {_PHASE_HINT}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
