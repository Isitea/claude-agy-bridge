"""MCP 진입점 — 도구 등록 + 자기설명 도구 설명문 (§9-1).

도구 설명은 소비 세션이 반드시 읽는 유일한 텍스트다. 언제 쓰고 언제 쓰지 말아야
하는지, 비용, 그리고 "반환값은 자문 의견이지 사실이 아니다"를 설명문에 싣는다.

실행 모델 (§5): agy_consult는 wait_seconds(기본 45s)까지만 동기 대기하고,
넘어가면 job 핸들을 반환한다. 비동기가 예외가 아니라 정상 경로다.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from functools import partial
from typing import Literal
from urllib.parse import quote

import anyio
from mcp.server.mcpserver import MCPServer

from agy_bridge import __version__
from agy_bridge.budget import Ledger
from agy_bridge.config import Config, StartupError, load_config
from agy_bridge.context import PreparedContext, prepare_context
from agy_bridge.jobs import TERMINAL_STATES, JobRecord, JobRegistry, UnknownJob
from agy_bridge.prompts import assemble_prompt, compose_playbooks_block
from agy_bridge.schemas import (
    structured_default,
    validate_verdict,
    verdict_schema_json,
)
from agy_bridge.serve import ContextServer
from agy_bridge.sessions import SessionStore

Mode = Literal["review", "verify", "derive", "literature", "design"]
Effort = Literal["low", "medium", "high"]

CONSULT_DESCRIPTION = """\
독립 검증자 agy(Antigravity CLI, 기본 gemini-3.7-flash / effort high)에게 과학·수치 검증을 요청한다.

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
    프로젝트 루트 밖 경로(절대경로·상위 탈출·심링크)는 거부된다 — 밖의 자료가
    필요하면 저장소 안으로 복사한 뒤 지정하라.
    **순서가 우선순위다** — 앞에서부터 인라이닝 예산에 담고, 넘치는 파일부터는
    루프백 HTTP 서빙으로 자동 전환된다(auto). 예산은 문자(기본 100,000자)와
    바이트(argv 한계 기준, 한글 등 멀티바이트는 약 40,000자에서 먼저 걸릴 수
    있음)를 병행하며, 플레이북·question 길이도 예산을 잠식한다. 검토 대상을 앞에,
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

    registry = JobRegistry(
        config,
        on_complete=_on_complete,
        # 재시도 스폰도 실제 agy 기동이다 — start(retry)로 원장에 계산해야
        # daily_call_budget이 '시작된 프로세스 수'라는 의미를 지킨다 (리뷰 #5-2)
        on_retry=lambda record: ledger.record_retry(record.job_id, mode=record.mode),
    )

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
            structured_output = record.result.get("structured_output")
            if structured_output is not None:
                payload["verdict"] = structured_output
                problems = validate_verdict(structured_output)
                if problems:
                    # 스키마 위반을 조용히 통과시키면 소비 세션이 엉뚱한 값을
                    # 판정으로 읽는다 (§4.4).
                    payload["verdict_valid"] = False
                    payload["verdict_problems"] = problems
                    payload["verdict_note"] = (
                        "검증자가 반환한 판정이 스키마를 지키지 않았다 — "
                        "verdict를 통과로 해석하지 마라. response 본문을 직접 "
                        "읽고, 필요하면 질문을 좁혀 재시도하라."
                    )
            elif record.structured:
                # 구조화를 요구했는데 판정이 없다. 침묵을 통과로 오독하는 것이
                # 이 시스템 최악의 실패이므로(§2.3-A) 부재를 명시한다.
                payload["verdict_valid"] = False
                payload["verdict_missing"] = True
                payload["verdict_note"] = (
                    "구조화 판정을 요구했으나 검증자가 스키마를 무시하고 산문만 "
                    "반환했다. 판정 부재를 통과로 해석하지 마라 — response를 "
                    "직접 검토하거나 질문을 좁혀 재시도하라."
                )
            if record.attempts > 1:
                # 비용 인식·신뢰도 판단 재료 (리뷰 #5-3)
                payload["attempts"] = record.attempts
                payload["attempts_note"] = (
                    "agy 비정상 종료로 재시도되었다 — 토큰 비용이 그만큼 "
                    "중복 발생했다."
                )
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
        cost_note = ""
        if record.attempts > 1:
            # 재시도 후 최종 실패 — 토큰 비용이 중복 발생했음을 알린다 (자체 리뷰)
            cost_note = f" (재시도 {record.attempts}회, 토큰 비용 중복 발생)"
        raise RuntimeError(
            f"job {record.job_id} {record.state}{cost_note}: {record.error}"
        )

    def _dummy_url_for(name: str) -> str:
        """실제 서버를 띄우기 전에 서빙 블록 크기를 재기 위한 최대 길이 URL.
        포트 5자리 + token_urlsafe(16)의 22자로 실제보다 짧아지지 않는다."""
        return f"http://127.0.0.1:65535/{'x' * 22}/{quote(name)}"

    def _served_block(url_for, prepared: PreparedContext) -> str:
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
            f"목록: {url_for('INDEX')}",
        ]
        for item in prepared.served_manifest:
            names = item["serve_names"]
            if len(names) == 1:
                lines.append(
                    f"- {item['file']} [{item['lines']}행]: {url_for(names[0])}"
                )
            else:
                lines.append(
                    f"- {item['file']} [{item['lines']}행] — {len(names)}개 부분으로 "
                    "분할됨. 답하기 전에 아래 부분 URL을 **하나도 빠짐없이** 읽어라:"
                )
                lines.extend(f"  {url_for(name)}" for name in names)
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
        # 값싼 사전 확인 — 컨텍스트 준비 전에 조기 거부한다. 동시 호출에 대한
        # 원자적 판정은 스폰 직전의 check_and_record_start가 한다 (리뷰 #5-1).
        ledger.check_budget(config.daily_call_budget)

        # 파일 블록을 뺀 프롬프트 오버헤드(플레이북/오버레이·mode 지시문·question·
        # context·구조화 지시)를 미리 실측한다. 이 바이트를 인라이닝 예산에서
        # 빼야 큰 오버레이나 긴 question이 argv 한계를 넘길 때 auto가 초과분을
        # 서빙으로 전환한다 — 파일 블록만 보면 폴백이 발동하지 않는다 (자체 리뷰).
        playbooks_block = compose_playbooks_block(
            mode,
            project_root=config.project_root,
            overlay_dir=config.overlay_dir,
            enabled=config.playbooks_enabled,
        )
        def _overhead_bytes(files_block: str) -> int:
            return len(
                assemble_prompt(
                    mode=mode, question=question, context=context,
                    files_block=files_block, playbooks_block=playbooks_block,
                    structured=structured,
                ).encode("utf-8")
            ) - len(files_block.encode("utf-8"))

        # files_block이 있을 때만 붙는 "## 검토 대상 파일" 머리말까지 포함해서 잰다
        reserved_bytes = _overhead_bytes("x")

        prepared: PreparedContext | None = None
        if files:
            # 서빙 URL 블록도 argv 공간을 먹는데 prepare_context가 예산을 다 쓴
            # 뒤에 붙는다. 그래서 더미 URL로 그 크기를 재어 예산에 반영하고,
            # 반영 결과 서빙 대상이 늘면 한 번 더 수렴시킨다 — 아니면 argv 초과를
            # 막으려 존재하는 폴백이 도리어 초과의 원인이 된다.
            served_reserve = 0
            for _ in range(3):
                prepared = prepare_context(
                    files,
                    project_root=config.project_root,
                    deny_globs=config.deny_globs,
                    max_chars=config.max_inline_chars,
                    reserved_bytes=reserved_bytes + served_reserve,
                )
                if not prepared.to_serve:
                    break
                estimate = len(
                    _served_block(_dummy_url_for, prepared).encode("utf-8")
                )
                if estimate <= served_reserve:
                    break
                served_reserve = estimate

        context_server: ContextServer | None = None
        try:
            files_block = prepared.files_block if prepared else ""
            if prepared and prepared.to_serve:
                context_server = ContextServer(prepared.to_serve)
                block = _served_block(context_server.url_for, prepared)
                files_block = f"{files_block}\n\n{block}" if files_block else block

            prompt = assemble_prompt(
                mode=mode,
                question=question,
                context=context,
                files_block=files_block,
                playbooks_block=playbooks_block,
                structured=structured,
            )
            # 스폰 전 선기록 (§13): id를 선점하고, 확인+기록을 원자 구간에서
            # 수행한다 — 스폰 후 기록하면 동시 호출이 상한을 넘고, 기록 후
            # 스폰이 실패하면 보정 엔트리로 되돌린다.
            job_id = registry.claim_job_id()
            try:
                start_date = ledger.check_and_record_start(
                    job_id, mode=mode, model=model or config.model,
                    limit=config.daily_call_budget,
                )
            except Exception:
                # BudgetExceeded뿐 아니라 원장 I/O 오류(ENOSPC 등)에서도 선점 id를
                # 반납한다 — 아니면 빈 j-N.json이 영구히 남아 이후 id를 건너뛴다.
                registry.release_claim(job_id)
                raise
            try:
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
                    job_id=job_id,
                    structured=structured,
                )
            except Exception:
                # 선기록한 start를 같은 날 계수에서 정확히 상쇄한다 (자정 경계)
                ledger.record_spawn_failed(job_id, date=start_date)
                registry.release_claim(job_id)
                raise
        except Exception:
            # registry.start에 도달하지 못한 실패 경로에서도 서버를 남기지 않는다
            # (registry.start 내부의 close와 겹쳐도 close는 멱등이다)
            if context_server is not None:
                context_server.close()
            raise

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
            # 세션 조회는 flock(LOCK_EX)을 잡는다 — 이벤트 루프 스레드에서 직접
            # 부르면 이웃 프로세스 정지 시 서버 전체가 얼어붙는다. 스레드로 넘긴다.
            meta = await anyio.to_thread.run_sync(partial(sessions.resolve, session_id))
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
            recent = await anyio.to_thread.run_sync(registry.list_jobs)
            known = ", ".join(r.job_id for r in recent[-10:]) or "없음"
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
        meta = await anyio.to_thread.run_sync(partial(sessions.resolve, session_id))
        if meta is None:
            names = await anyio.to_thread.run_sync(sessions.list_sessions)
            known = ", ".join(names) or "없음"
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
            closed = await anyio.to_thread.run_sync(partial(sessions.close, session_id))
            return {"closed": closed, "session_id": session_id}

        # 세션/job 조회는 flock·파일 I/O를 수반한다 — 이벤트 루프에서 직접 돌리지
        # 않고 스레드로 넘긴다 (이웃 프로세스 정지 시 서버 동결 방지).
        def _snapshot() -> dict:
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

        return await anyio.to_thread.run_sync(_snapshot)

    # serve()가 종료 시 정리(§10.1)에 쓴다. MCPServer는 이 필드를 모르므로 동적 부착.
    setattr(server, "agy_registry", registry)  # noqa: B010
    return server


def _die_by_signal(signum: int) -> None:
    """정리를 마친 뒤 그 신호로 죽는다.

    여기서 SystemExit을 올리면 asyncio 루프의 정상 teardown을 건너뛴다. anyio의
    워커 스레드는 non-daemon이라 루프가 제대로 닫힐 때만 정지되므로, 남은 스레드를
    join하며 인터프리터가 threading._shutdown에서 매달린다 — 실측으로 tools/list
    이후 첫 SIGTERM이 무반응이고 두 번째 신호에야 종료됐다 (faulthandler 덤프:
    main=threading._shutdown, worker=anyio._backends._asyncio.WorkerThread.run).

    기본 처리로 되돌린 뒤 같은 신호를 자신에게 보내면 즉시 종료되고, 부모에게도
    '신호로 죽었다'는 올바른 상태가 전달된다. 이 시점엔 _cleanup()이 이미 끝났고
    agy 자식은 애초에 분리 실행(§5 재시작 생존)이라 잃을 정리가 없다.
    """
    sys.stderr.flush()
    with contextlib.suppress(ValueError, OSError):
        signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


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
    server = build_server(config)
    registry: JobRegistry = getattr(server, "agy_registry")  # noqa: B009

    def _cleanup() -> None:
        # 루프백 서버는 이 프로세스와 함께 죽는다. 서빙 자료에 의존하던 job을
        # 그대로 두면 자료를 못 읽은 답이 나중에 completed로 회수된다 (§10.1).
        stopped = registry.shutdown()
        if stopped:
            print(
                f"agy-bridge: 종료하며 서빙 의존 job {len(stopped)}건 중단 "
                f"({', '.join(stopped)})",
                file=sys.stderr,
            )

    def _on_signal(signum, _frame):
        # 기본 SIGTERM은 finally를 돌리지 않고 즉사시킨다 — 정리 후 나간다.
        _cleanup()
        _die_by_signal(signum)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(signum, _on_signal)

    try:
        server.run(transport="stdio")
    finally:
        _cleanup()  # stdio 종료·예외 경로 (shutdown은 멱등이다)
    return 0
