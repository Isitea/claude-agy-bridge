"""--json-schema 페이로드 (§4.4).

`insufficient_context`와 `assumptions_made`가 있는 이유: 검증자가 맥락 부족을
환각으로 메우는 것이 이 구조에서 가장 흔한 실패다. 부족하면 부족하다고 말할
출구를 스키마에 명시적으로 만들어 둔다.
"""

from __future__ import annotations

import json

VERDICT_SCHEMA: dict = {
    "type": "object",
    "required": ["verdict", "summary", "issues", "confidence"],
    "properties": {
        "verdict": {
            "enum": [
                "correct",
                "minor_issues",
                "major_issues",
                "incorrect",
                "insufficient_context",
            ]
        },
        "summary": {"type": "string"},
        "confidence": {"enum": ["low", "medium", "high"]},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "severity",
                    "location",
                    "problem",
                    "evidence",
                    "suggestion",
                ],
                "properties": {
                    "severity": {"enum": ["blocker", "major", "minor", "nit"]},
                    # "src/solver.py:87" 형식. 코드가 아닌 주장 검증이면 대상 서술.
                    "location": {"type": "string"},
                    "problem": {"type": "string"},
                    # 물리 법칙 / 수식 / 문헌 근거
                    "evidence": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
            },
        },
        "assumptions_made": {"type": "array", "items": {"type": "string"}},
    },
}


def verdict_schema_json() -> str:
    """agy --json-schema 인자로 넘길 직렬화 문자열."""
    return json.dumps(VERDICT_SCHEMA, ensure_ascii=False)


def structured_default(mode: str) -> bool:
    """mode=verify는 기본으로 구조화 판정을 요구한다 (§4.2)."""
    return mode == "verify"


def validate_verdict(value: object) -> list[str]:
    """VERDICT_SCHEMA 위반 목록을 반환한다 (빈 목록이면 적합).

    agy의 --json-schema 강제는 프롬프트 수준이라 무시될 수 있다(§4.4 실측).
    검증 없이 통과시키면 열거형 밖 판정이나 필수 키가 빠진 객체가 그대로
    verdict로 실려, 소비 세션이 판정 부재를 통과로 오독한다. 외부 의존성을
    늘리지 않으려고(§7.1) 이 스키마에 필요한 만큼만 검사한다.
    """
    problems: list[str] = []
    if not isinstance(value, dict):
        return [f"판정이 JSON 객체가 아니다 ({type(value).__name__})"]

    for key in VERDICT_SCHEMA["required"]:
        if key not in value:
            problems.append(f"필수 키 누락: {key}")

    props: dict = VERDICT_SCHEMA["properties"]
    for key in ("verdict", "confidence"):
        allowed = props[key]["enum"]
        if key in value and value[key] not in allowed:
            problems.append(f"{key}={value[key]!r}는 허용값 {allowed} 밖이다")
    if "summary" in value and not isinstance(value["summary"], str):
        problems.append("summary가 문자열이 아니다")

    issues = value.get("issues")
    if "issues" in value and not isinstance(issues, list):
        problems.append("issues가 배열이 아니다")
    elif isinstance(issues, list):
        item_schema = props["issues"]["items"]
        severities = item_schema["properties"]["severity"]["enum"]
        for index, issue in enumerate(issues):
            if not isinstance(issue, dict):
                problems.append(f"issues[{index}]가 객체가 아니다")
                continue
            missing = [k for k in item_schema["required"] if k not in issue]
            if missing:
                problems.append(f"issues[{index}] 필수 키 누락: {', '.join(missing)}")
            if "severity" in issue and issue["severity"] not in severities:
                problems.append(
                    f"issues[{index}].severity={issue['severity']!r}가 허용값 밖이다"
                )
    return problems
