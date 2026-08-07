"""MCP 진입점 — 도구 등록 + 자기설명 도구 설명문 (§9-1).

도구 설명은 소비 세션이 반드시 읽는 유일한 텍스트다. 언제 쓰고 언제 쓰지 말아야
하는지, 비용, 그리고 "반환값은 자문 의견이지 사실이 아니다"를 설명문에 싣는다.

실행 모델 (§5): agy_consult는 wait_seconds(기본 45s)까지만 동기 대기하고,
넘어가면 job 핸들을 반환한다. 비동기가 예외가 아니라 정상 경로다.
"""

from __future__ import annotations

import sys
from functools import partial
from typing import Literal

import anyio
from mcp.server.mcpserver import MCPServer

from agy_bridge import __version__
from agy_bridge.budget import Ledger
from agy_bridge.config import Config, StartupError, load_config
from agy_bridge.context import PreparedContext, prepare_context
from agy_bridge.jobs import TERMINAL_STATES, JobRecord, JobRegistry, UnknownJob
from agy_bridge.prompts import assemble_prompt, compose_playbooks_block
from agy_bridge.schemas import structured_default, verdict_schema_json
from agy_bridge.serve import ContextServer
from agy_bridge.sessions import SessionStore

Mode = Literal["review", "verify", "derive", "literature", "design"]
Effort = Literal["low", "medium", "high"]

CONSULT_DESCRIPTION = """\
독립 검증자 agy(Antigravity CLI, 기본 gemini-3.1-pro-high)에게 과학·수치 검증을 요청한다.

언제 쓰는가: 수치 기법, 열역학 처리, 단위계·기준 상태, 근사의 타당성처럼 독립적인
과학적 재검토가 가치 있는 시점. 새 수치 기법을 커밋하기 전, 물리량이 다른 모델로
넘어가는 경계가 대표적이다.

언제 쓰지 않는가: 구현 중의 사소한 질문. 호출당 고정비가 프로세스 기동 ~10초 +
입력 17k 토큰이고 review/verify는 수 분이 걸린다. "자주 잘게"가 아니라
"충분한 맥락을 담아 드물게" 묻는 도구다.

wait_seconds(기본 45초) 안에 끝나면 결과를 바로 받고, 넘어가면
{status:"running", job_id}가 반환된다. 그 경우 기다리지 말고 다른 작업(테스트
작성, 다른 모듈 구현)을 계속하다가 agy_result로 회수하라.

반환값은 자문 의견이지 사실이 아니다. 판정을 코드에 반영하기 전에 evidence를
직접 검토하라.

검증자의 능력 경계 (실측): 웹 검색과 URL fetch는 할 수 있다. 그러나 셸 명령
실행과 파일시스템 읽기는 헤드리스 자동 거부되므로 **저장소를 스스로 탐색하지
못한다** — 검토 대상은 반드시 files 인자로 전달하라. 질문에 "grep해 봐",
"찾아 봐" 같은 셸 유도 표현을 쓰지 마라 (검증자가 명령을 시도하다 침묵
실패한다). literature 모드는 웹 검색으로 최신 문헌·표준 기법을 확인할 수 있다.

files: 검토 대상을 "경로" 또는 "경로:시작-끝" 행범위로 지정한다 (프로젝트 루트 기준).
    **순서가 우선순위다** — 앞에서부터 100,000자 인라이닝 예산에 담고, 넘치는
    파일부터는 루프백 HTTP 서빙으로 자동 전환된다(auto). 검토 대상을 앞에,
    주변 자료(문헌, 로그, 큰 모듈)를 뒤에 두라. 전환 시 결과에 context_strategy와
    사유가 명시된다. 서빙은 지연이 크기에 비례한다(600 KB ≈ 46 s, 2 MB ≈ 100 s).
context: 물리 설정, 단위계, 가정, 경계조건 등 코드만으로 알 수 없는 정보.
    검증 품질은 여기서 갈린다 — 이론 수준, 온도·압력 조건, 반응계 성격을 담아라.
session_id: 같은 주제의 연속 질문에는 같은 session_id를 재사용하라 — 캐시 히트로
    저렴해지고 검증자가 앞선 논의를 기억한다. 처음 쓰는 id면 새 세션이 생긴다.
mode: review(수치 코드 검토) | verify(주장 판정) | derive(유도 점검) |
    literature(표준 기법 확인) | design(선택지 비교).
structured: mode=verify는 기본으로 구조화 판정을 강제하고 결과의 verdict 필드
    (verdict/summary/issues/confidence/assumptions_made)로 반환한다.
    verdict가 insufficient_context면 검증자가 맥락 부족을 선언한 것이다 —
    context를 보강해 재시도하라.
"""

RESULT_DESCRIPTION = """\
agy_consult가 반환한 job의 결과를 회수한다 (폴링).

status가 여전히 "running"이면 기다리지 말고 다른 작업을 계속한 뒤 나중에 다시
호출하라. wait_seconds를 주면 그 시간까지는 브리지가 대신 기다린다.
실패한 job은 오류로 반환된다 — 원인(stderr 포함)을 읽고 대응하라.
"""

FOLLOWUP_DESCRIPTION = """\
기존 세션의 conversation을 이어서 재질문한다.

같은 주제의 후속 질문은 새 agy_consult보다 이 도구가 낫다: 프롬프트 캐시가
적중해 저렴하고, 검증자가 앞선 지적·코드·논의를 기억한 채 답한다.
(예: 지적받은 두 곳을 수정한 뒤 재검증을 요청)
mode를 생략하면 그 세션의 직전 mode를 쓴다. 실행 모델은 agy_consult와 같다
(wait_seconds 안에 끝나면 결과, 넘으면 job 핸들).
"""

CANCEL_DESCRIPTION = "실행 중인 job을 중단한다. 이미 끝난 job이면 그 상태를 그대로 반환한다."

SESSIONS_DESCRIPTION = """\
세션 목록과 진행 중인 job을 조회하거나(action="list"), 세션 매핑을 닫는다(action="close").

"이전에 이 모듈에 대해 물어본 세션"을 찾아 session_id를 재사용할 때 쓴다.
close는 브리지의 매핑만 제거한다 — agy 쪽 대화 기록 자체는 남는다.
"""


def _attach_strategy(payload: dict, record: JobRecord) -> None:
    """auto 전환이 일어났으면 전환 사실과 사유를 도구 결과에 명시한다 (§4.3)."""
    if record.strategy != "inline":
        payload["context_strategy"] = record.strategy
        payload["context_note"] = record.strategy_reason
        payload["served"] = record.served


def build_server(config: Config) -> MCPServer:
    server = MCPServer(
        name="agy-bridge",
        version=__version__,
        instructions=(
            "과학 검증 브리지. 브리지 자체에는 LLM이 없고, agy 서브프로세스를 "
            "실행해 독립 검증 의견을 중계한다. 반환값은 자문이지 사실이 아니다. "
            "running이 반환되면 기다리지 말고 다른 작업을 계속하라."
        ),
    )

    sessions = SessionStore(config)
    ledger = Ledger(config)

    def _on_complete(record: JobRecord, result) -> None:
        ledger.record_finish(
            record.job_id,
            state=record.state,
            usage=result.usage if result is not None else None,
            duration_seconds=result.duration_seconds if result is not None else None,
        )
        if result is not None and record.session_id:
            sessions.record_use(
                record.session_id,
                conversation_id=result.conversation_id,
                mode=record.mode,
                usage=result.usage,
            )

    registry = JobRegistry(config, on_complete=_on_complete)

    # ── 공통 헬퍼 ────────────────────────────────────────

    def _job_payload(record: JobRecord) -> dict:
        if record.state == "completed":
            assert record.result is not None
            payload = {
                "status": "completed",
                "job_id": record.job_id,
                "response": record.result["response"],
                "conversation_id": record.result["conversation_id"],
                "usage": record.result["usage"],
                "elapsed_s": record.elapsed_s(),
                "reviewed": record.reviewed,
            }
            if record.session_id:
                payload["session_id"] = record.session_id
            if record.result.get("structured_output") is not None:
                payload["verdict"] = record.result["structured_output"]
            _attach_strategy(payload, record)
            return payload
        if record.state == "running":
            payload = {
                "status": "running",
                "job_id": record.job_id,
                "elapsed_s": record.elapsed_s(),
                "hint": (
                    f'agy_result(job_id="{record.job_id}")로 회수하라. '
                    "review/verify는 수 분 걸릴 수 있다 — 기다리지 말고 "
                    "다른 작업을 계속하라."
                ),
            }
            _attach_strategy(payload, record)
            return payload
        if record.state == "cancelled":
            return {
                "status": "cancelled",
                "job_id": record.job_id,
                "elapsed_s": record.elapsed_s(),
            }
        # failed | timeout — 실패는 조용히 넘어가지 않는다 (§2.3-A)
        raise RuntimeError(f"job {record.job_id} {record.state}: {record.error}")

    def _served_block(server: ContextServer, prepared: PreparedContext) -> str:
        lines = [
            "--- 추가 자료 (루프백 HTTP 서빙) ---",
            "아래 파일은 크기 때문에 프롬프트에 싣지 않고 로컬 URL로 제공한다.",
            (
                "이 환경에서 셸 명령 실행(curl, grep 등)은 권한이 없어 조용히 "
                "실패한다 — 절대 시도하지 마라. 반드시 내장 URL fetch(웹 읽기) "
                "도구로 아래 URL을 직접 읽어라. 필요하면 여러 번 나눠 읽어도 된다."
            ),
            (
                "인라이닝과 동일하게 각 행 앞에 원본 절대 행 번호가 붙어 있다."
            ),
            f"목록: {server.url_for('INDEX')}",
        ]
        for item in prepared.served_manifest:
            names = item["serve_names"]
            if len(names) == 1:
                lines.append(
                    f"- {item['file']} [{item['lines']}행]: "
                    f"{server.url_for(names[0])}"
                )
            else:
                lines.append(
                    f"- {item['file']} [{item['lines']}행] — {len(names)}개 부분으로 "
                    "분할됨. 답하기 전에 아래 부분 URL을 **하나도 빠짐없이** 읽어라:"
                )
                lines.extend(f"  {server.url_for(name)}" for name in names)
        return "\n".join(lines)

    def _start_and_wait(
        *,
        mode: str,
        question: str,
        files: list[str] | None,
        context: str,
        model: str | None,
        effort: str | None,
        session_id: str | None,
        conversation_id: str | None,
        wait_seconds: float | None,
        structured: bool,
    ) -> dict:
        # 예산 확인은 스폰 전에 한다 — 프로세스가 뜬 뒤 거부하면 이미 비용이 나갔다
        ledger.check_budget(config.daily_call_budget)

        prepared: PreparedContext | None = None
        if files:
            prepared = prepare_context(
                files,
                project_root=config.project_root,
                deny_globs=config.deny_globs,
                max_chars=config.max_inline_chars,
            )

        context_server: ContextServer | None = None
        try:
            files_block = prepared.files_block if prepared else ""
            if prepared and prepared.to_serve:
                context_server = ContextServer(prepared.to_serve)
                block = _served_block(context_server, prepared)
                files_block = f"{files_block}\n\n{block}" if files_block else block

            prompt = assemble_prompt(
                mode=mode,
                question=question,
                context=context,
                files_block=files_block,
                playbooks_block=compose_playbooks_block(
                    mode,
                    project_root=config.project_root,
                    overlay_dir=config.overlay_dir,
                    enabled=config.playbooks_enabled,
                ),
                structured=structured,
            )
            record = registry.start(
                prompt,
                mode=mode,
                question=question,
                session_id=session_id,
                conversation_id=conversation_id,
                model=model,
                effort=effort,
                json_schema=verdict_schema_json() if structured else None,
                reviewed=prepared.inline_manifest if prepared else [],
                served=prepared.served_manifest if prepared else [],
                strategy=prepared.strategy if prepared else "inline",
                strategy_reason=prepared.reason if prepared else None,
                context_server=context_server,
            )
        except Exception:
            # registry.start에 도달하지 못한 실패 경로에서도 서버를 남기지 않는다
            if context_server is not None:
                context_server.close()
            raise

        ledger.record_start(
            record.job_id, mode=mode, model=model or config.model
        )
        window = config.wait_seconds if wait_seconds is None else wait_seconds
        record = registry.wait(record.job_id, window)
        return _job_payload(record)

    # ── 도구 ─────────────────────────────────────────────

    @server.tool(name="agy_consult", description=CONSULT_DESCRIPTION)
    async def agy_consult(
        question: str,
        mode: Mode = "review",
        files: list[str] | None = None,
        context: str = "",
        model: str | None = None,
        effort: Effort | None = None,
        session_id: str | None = None,
        wait_seconds: float | None = None,
        structured: bool | None = None,
    ) -> dict:
        conversation_id = None
        if session_id:
            meta = sessions.resolve(session_id)
            if meta:
                conversation_id = meta.get("conversation_id")
        return await anyio.to_thread.run_sync(
            partial(
                _start_and_wait,
                mode=mode,
                question=question,
                files=files,
                context=context,
                model=model,
                effort=effort,
                session_id=session_id,
                conversation_id=conversation_id,
                wait_seconds=wait_seconds,
                structured=structured_default(mode) if structured is None else structured,
            ),
            abandon_on_cancel=True,
        )

    @server.tool(name="agy_result", description=RESULT_DESCRIPTION)
    async def agy_result(job_id: str, wait_seconds: float = 0) -> dict:
        try:
            record = await anyio.to_thread.run_sync(
                partial(registry.wait, job_id, wait_seconds),
                abandon_on_cancel=True,
            )
        except UnknownJob:
            known = ", ".join(r.job_id for r in registry.list_jobs()[-10:]) or "없음"
            raise RuntimeError(
                f"job {job_id!r}를 모른다. 최근 job: {known}"
            ) from None
        return _job_payload(record)

    @server.tool(name="agy_followup", description=FOLLOWUP_DESCRIPTION)
    async def agy_followup(
        session_id: str,
        question: str,
        files: list[str] | None = None,
        context: str = "",
        mode: Mode | None = None,
        model: str | None = None,
        effort: Effort | None = None,
        wait_seconds: float | None = None,
        structured: bool | None = None,
    ) -> dict:
        meta = sessions.resolve(session_id)
        if meta is None:
            known = ", ".join(sessions.list_sessions()) or "없음"
            raise RuntimeError(
                f"세션 {session_id!r}를 모른다. 알려진 세션: {known}. "
                "새 주제라면 agy_consult에 session_id를 주어 시작하라."
            )
        resolved_mode = mode or meta.get("last_mode") or "review"
        return await anyio.to_thread.run_sync(
            partial(
                _start_and_wait,
                mode=resolved_mode,
                question=question,
                files=files,
                context=context,
                model=model,
                effort=effort,
                session_id=session_id,
                conversation_id=meta.get("conversation_id"),
                wait_seconds=wait_seconds,
                structured=structured_default(resolved_mode)
                if structured is None
                else structured,
            ),
            abandon_on_cancel=True,
        )

    @server.tool(name="agy_cancel", description=CANCEL_DESCRIPTION)
    async def agy_cancel(job_id: str) -> dict:
        try:
            record = await anyio.to_thread.run_sync(partial(registry.cancel, job_id))
        except UnknownJob:
            raise RuntimeError(f"job {job_id!r}를 모른다.") from None
        return {
            "status": record.state,
            "job_id": record.job_id,
            "elapsed_s": record.elapsed_s(),
        }

    @server.tool(name="agy_sessions", description=SESSIONS_DESCRIPTION)
    async def agy_sessions(
        action: Literal["list", "close"] = "list",
        session_id: str | None = None,
    ) -> dict:
        if action == "close":
            if not session_id:
                raise RuntimeError('action="close"에는 session_id가 필요하다.')
            return {"closed": sessions.close(session_id), "session_id": session_id}
        active = [
            {
                "job_id": r.job_id,
                "state": r.state,
                "mode": r.mode,
                "question_head": r.question_head,
                "session_id": r.session_id,
                "elapsed_s": r.elapsed_s(),
            }
            for r in registry.list_jobs()
            if r.state not in TERMINAL_STATES
        ]
        return {"sessions": sessions.list_sessions(), "active_jobs": active}

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
