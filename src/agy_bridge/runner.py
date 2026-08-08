"""agy 서브프로세스 실행 + JSON 파싱 + 실패 승격 (§2.3-A, §5, §10).

이 모듈의 존재 이유는 기능이 아니라 실패 모드다. agy는 헤드리스에서 권한이
필요한 도구를 자동 거부당해도 status=SUCCESS에 빈 response를 반환한다 (§2.3-A).
브리지가 이것을 오류로 승격하지 않으면 "검증 통과"로 오독되는 침묵이 전달된다 —
이 시스템에서 가능한 최악의 버그다.

안전 정책 (§10): --mode plan 고정(편집 의도 차단), --disable-slash-commands
(조립된 프롬프트는 지시가 아니라 데이터다), --dangerously-skip-permissions 절대 미사용.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
from dataclasses import dataclass, field
from typing import Any

from agy_bridge.config import Config

# argv 단일 인자 하드 리밋 131,072 B (§2.3-D). 여기 걸리면 프로세스가 뜨지도 못하고
# E2BIG이 나므로, 스폰 전에 명확한 메시지로 실패시킨다.
ARGV_PROMPT_LIMIT_BYTES = 130_000


class AgyError(RuntimeError):
    """agy 호출이 신뢰 가능한 응답을 반환하지 못했다. stderr를 그대로 실어 전달한다."""

    def __init__(
        self,
        reason: str,
        *,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ):
        self.reason = reason
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        parts = [reason]
        if returncode is not None:
            parts.append(f"exit code: {returncode}")
        if stderr.strip():
            parts.append("--- agy stderr ---\n" + stderr.strip()[-2000:])
        if stdout.strip():
            parts.append("--- agy stdout (head) ---\n" + stdout.strip()[:2000])
        super().__init__("\n".join(parts))


@dataclass(frozen=True)
class AgyResult:
    response: str
    conversation_id: str
    structured_output: Any | None
    usage: dict
    duration_seconds: float | None
    raw: dict = field(repr=False)


def ensure_prompt_within_argv_limit(prompt: str) -> None:
    """E2BIG 사전 차단 (§2.3-D). 동기 경로와 job 스폰 경로가 공유한다."""
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > ARGV_PROMPT_LIMIT_BYTES:
        raise AgyError(
            f"조립된 프롬프트가 {prompt_bytes:,} B로 argv 단일 인자 한계"
            f"({ARGV_PROMPT_LIMIT_BYTES:,} B)를 넘는다 (§2.3-D). "
            "files 예산은 문자·바이트 양쪽으로 관리되므로, 보통 question/context가 "
            "지나치게 길 때 생긴다 — question/context를 줄이고 자료는 files로 넘겨라."
        )


def build_command(
    prompt: str,
    *,
    config: Config,
    model: str | None = None,
    effort: str | None = None,
    conversation_id: str | None = None,
    json_schema: str | None = None,
) -> list[str]:
    cmd = [
        config.agy_bin,
        "-p", prompt,
        "--output-format", "json",
        "--model", model or config.model,
        "--effort", effort or config.effort,
        "--mode", "plan",
        "--disable-slash-commands",
        "--print-timeout", f"{config.print_timeout}s",
    ]
    if conversation_id:
        cmd += ["--conversation", conversation_id]
    if json_schema:
        cmd += ["--json-schema", json_schema]
    return cmd


def run_agy(
    prompt: str,
    *,
    config: Config,
    model: str | None = None,
    effort: str | None = None,
    conversation_id: str | None = None,
    json_schema: str | None = None,
) -> AgyResult:
    """agy를 동기 실행하고 결과를 파싱한다. 모든 실패는 AgyError로 승격된다."""
    ensure_prompt_within_argv_limit(prompt)

    cmd = build_command(
        prompt,
        config=config,
        model=model,
        effort=effort,
        conversation_id=conversation_id,
        json_schema=json_schema,
    )

    # CWD는 브리지가 관리하는 중립 스크래치 디렉터리 (§8.2) — 대상 저장소의
    # AGENTS.md/GEMINI.md(구현자용 지시)를 검토자가 상속하지 않게 한다.
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(config.scratch_dir),
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=config.hard_kill_seconds)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise AgyError(
            f"하드 킬: agy가 {config.hard_kill_seconds}s 안에 종료하지 않아 "
            "프로세스 그룹을 종료했다 (§5 타임아웃 계층 3)."
        ) from None

    return parse_agy_output(stdout, stderr, returncode=process.returncode)


def parse_agy_output(
    stdout: str,
    stderr: str,
    returncode: int | None = None,
) -> AgyResult:
    """agy 출력을 AgyResult로 파싱한다. 모든 실패는 AgyError로 승격된다.

    동기 경로(run_agy)와 비동기 job 종결자(jobs.py)가 공유한다 — 실패 승격
    규칙(§2.3-A)이 두 경로에서 갈라지면 안 되기 때문이다. returncode=None은
    브리지 재시작 후 고아 job 회수처럼 종료 코드를 알 수 없는 경우다.
    """
    if returncode is not None and returncode != 0:
        raise AgyError(
            "agy가 0이 아닌 코드로 종료했다.",
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AgyError(
            f"agy stdout이 JSON이 아니다: {exc}",
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ) from exc

    if not isinstance(payload, dict):
        # 계약: 이 함수의 모든 실패는 AgyError다. dict가 아니면 .get에서
        # AttributeError가 새어 나가 감시 스레드를 죽인다.
        raise AgyError(
            f"agy stdout이 JSON 객체가 아니다 ({type(payload).__name__}).",
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    status = payload.get("status")
    if status != "SUCCESS":
        raise AgyError(
            f"agy status={status!r} (SUCCESS 아님).",
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    response = payload.get("response") or ""
    if not isinstance(response, str):
        raise AgyError(
            f"agy response가 문자열이 아니다 ({type(response).__name__}).",
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
    structured = payload.get("structured_output")
    if not response.strip() and structured in (None, "", {}, []):
        # 가장 위험한 실패 모드의 승격 지점 (§2.3-A). status만 믿으면 안 된다.
        raise AgyError(
            "agy가 status=SUCCESS를 반환했지만 response가 비어 있다. "
            "헤드리스 권한 자동 거부(§2.3-A)일 가능성이 높다 — stderr를 확인하라.",
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    return AgyResult(
        response=response,
        conversation_id=payload.get("conversation_id") or "",
        structured_output=structured,
        usage=payload.get("usage") or {},
        duration_seconds=payload.get("duration_seconds"),
        raw=payload,
    )
