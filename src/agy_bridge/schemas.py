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
