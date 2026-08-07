"""MCP 진입점 — 도구 등록 + 자기설명 도구 설명문 (§9-1).

도구 설명은 소비 세션이 반드시 읽는 유일한 텍스트다. 언제 쓰고 언제 쓰지 말아야
하는지, 비용, 그리고 "반환값은 자문 의견이지 사실이 아니다"를 설명문에 싣는다.

Phase 1: agy_consult 동기 전용. 비동기 job과 세션 재개는 Phase 2에서 추가된다.
"""

from __future__ import annotations

import sys
import time
from functools import partial
from typing import Literal

import anyio
from mcp.server.mcpserver import MCPServer

from agy_bridge import __version__
from agy_bridge.config import Config, StartupError, load_config
from agy_bridge.context import inline_files
from agy_bridge.prompts import assemble_prompt
from agy_bridge.runner import run_agy

CONSULT_DESCRIPTION = """\
독립 검증자 agy(Antigravity CLI, 기본 gemini-3.1-pro-high)에게 과학·수치 검증을 요청한다.

언제 쓰는가: 수치 기법, 열역학 처리, 단위계·기준 상태, 근사의 타당성처럼 독립적인
과학적 재검토가 가치 있는 시점. 새 수치 기법을 커밋하기 전, 물리량이 다른 모델로
넘어가는 경계가 대표적이다.

언제 쓰지 않는가: 구현 중의 사소한 질문. 호출당 고정비가 프로세스 기동 ~10초 +
입력 17k 토큰이고 review/verify는 수 분이 걸린다. "자주 잘게"가 아니라
"충분한 맥락을 담아 드물게" 묻는 도구다. 이 호출은 완료까지 블로킹된다(Phase 1).

반환값은 자문 의견이지 사실이 아니다. 판정을 코드에 반영하기 전에 evidence를
직접 검토하라.

files: 검토 대상을 "경로" 또는 "경로:시작-끝" 행범위로 지정한다 (프로젝트 루트 기준).
    전체가 프롬프트에 인라이닝되며 합계 상한은 100,000자다. 넘으면 행범위를 좁혀라.
context: 물리 설정, 단위계, 가정, 경계조건 등 코드만으로 알 수 없는 정보.
    검증 품질은 여기서 갈린다 — 이론 수준, 온도·압력 조건, 반응계 성격을 담아라.
mode: review(수치 코드 검토) | verify(주장 판정) | derive(유도 점검) |
    literature(표준 기법 확인) | design(선택지 비교).
"""


def build_server(config: Config) -> MCPServer:
    server = MCPServer(
        name="agy-bridge",
        version=__version__,
        instructions=(
            "과학 검증 브리지. 브리지 자체에는 LLM이 없고, agy 서브프로세스를 "
            "실행해 독립 검증 의견을 중계한다. 반환값은 자문이지 사실이 아니다."
        ),
    )

    @server.tool(name="agy_consult", description=CONSULT_DESCRIPTION)
    async def agy_consult(
        question: str,
        mode: Literal["review", "verify", "derive", "literature", "design"] = "review",
        files: list[str] | None = None,
        context: str = "",
        model: str | None = None,
        effort: Literal["low", "medium", "high"] | None = None,
    ) -> dict:
        started = time.monotonic()

        files_block, manifest = "", []
        if files:
            files_block, manifest = inline_files(
                files,
                project_root=config.project_root,
                deny_globs=config.deny_globs,
                max_chars=config.max_inline_chars,
            )

        prompt = assemble_prompt(
            mode=mode, question=question, context=context, files_block=files_block
        )

        # 서브프로세스 대기가 이벤트 루프를 막지 않도록 워커 스레드에서 실행한다.
        result = await anyio.to_thread.run_sync(
            partial(run_agy, prompt, config=config, model=model, effort=effort),
            abandon_on_cancel=True,
        )

        return {
            "status": "completed",
            "response": result.response,
            "conversation_id": result.conversation_id,
            "usage": result.usage,
            "elapsed_s": round(time.monotonic() - started, 1),
            "reviewed": manifest,  # 무엇이 검토됐는지 확정 (§4.3 전략 A의 강점)
        }

    return server


def serve() -> int:
    """`agy-bridge serve` 진입점. stdout은 MCP 채널이므로 로그는 stderr로만."""
    try:
        config = load_config()
    except StartupError as exc:
        print(f"agy-bridge: {exc}", file=sys.stderr)
        return 1

    print(
        f"agy-bridge {__version__}: project_root={config.project_root} "
        f"model={config.model} (stdio)",
        file=sys.stderr,
    )
    build_server(config).run(transport="stdio")
    return 0
