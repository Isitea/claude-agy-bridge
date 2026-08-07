"""역할별 프롬프트 템플릿과 조립 (§4.5, §8).

조립 순서: _common.md(역할 규약) → [내장 플레이북·오버레이: Phase 4] → mode별 지시
→ 인라이닝된 파일 → 호출별 context → 질문.

브리지 안의 모든 선택은 테이블 조회다 (§3.1) — mode → 지시문 매핑은 정적 사전이다.
"""

from __future__ import annotations

from importlib import resources

MODES = ("review", "verify", "derive", "literature", "design")

MODE_INSTRUCTIONS: dict[str, str] = {
    "review": (
        "시뮬레이션·수치 코드의 과학적 타당성을 검토하라. 단위 정합성, 기준 상태 "
        "일관성, 근사의 적용 범위, 수치 안정성, 오차 전파를 우선 확인하라. "
        "지적마다 심각도(blocker / major / minor / nit)와 위치, 근거, 수정 제안을 붙여라."
    ),
    "verify": (
        "아래 주장·수식·구현이 옳은지 판정하라. 최종 판정은 correct / minor_issues / "
        "major_issues / incorrect / insufficient_context 중 하나로 명시하고, "
        "각 문제에 심각도, 위치, 물리적 근거, 수정 제안을 붙여라."
    ),
    "derive": (
        "유도 과정을 단계별로 점검하라. 차원 해석, 극한·점근 거동, 부호 규약, "
        "경계·초기 조건 정합을 확인하고, 필요하면 대안 정식화를 제시하라."
    ),
    "literature": (
        "이 문제에 대한 표준 기법, 관례, 선행연구를 확인하라. 확실히 아는 문헌 "
        "지식과 불확실한 기억을 구분해 표기하라."
    ),
    "design": (
        "제시된 알고리즘·이산화 선택지를 정확도, 안정성, 비용 관점에서 비교하고 "
        "근거와 함께 권고안을 제시하라."
    ),
}


def load_common() -> str:
    return (
        resources.files("agy_bridge")
        .joinpath("playbooks/_common.md")
        .read_text(encoding="utf-8")
    )


def assemble_prompt(
    *,
    mode: str,
    question: str,
    context: str = "",
    files_block: str = "",
) -> str:
    if mode not in MODES:
        raise ValueError(f"mode는 {MODES} 중 하나여야 한다: {mode!r}")

    parts = [load_common().strip()]
    parts.append(f"## 검토 지시 (mode={mode})\n\n{MODE_INSTRUCTIONS[mode]}")
    if files_block:
        parts.append(
            "## 검토 대상 파일\n\n"
            "각 행 앞의 숫자는 원본 파일의 절대 행 번호다. "
            "지적할 때 이 번호로 `경로:행` 인용하라.\n\n" + files_block
        )
    if context.strip():
        parts.append("## 호출 컨텍스트 (물리 설정·가정·단위계)\n\n" + context.strip())
    parts.append("## 질문\n\n" + question.strip())
    return "\n\n".join(parts)
