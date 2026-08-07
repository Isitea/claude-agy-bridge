"""호출 예산과 비용 계측 (§13, Phase 5).

원장은 프로젝트별 상태 디렉터리의 ledger.jsonl에 추가 전용으로 쌓인다.
예산은 시작된 호출 수 기준(로컬 날짜)이며, 초과 시 도구가 스폰 전에 사유와
함께 거부한다 — agy 프로세스가 뜬 뒤 거부하면 이미 비용이 나간 뒤다.
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
from pathlib import Path

from agy_bridge.config import Config


class BudgetExceeded(RuntimeError):
    pass


def _today() -> str:
    """예산 초기화 기준은 로컬 날짜다 (§13: 자정 초기화)."""
    return _dt.datetime.now().astimezone().date().isoformat()


class Ledger:
    def __init__(self, config: Config):
        self._path: Path = config.state_dir / "ledger.jsonl"
        self._lock = threading.Lock()

    def _append(self, entry: dict) -> None:
        with self._lock, open(self._path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _entries(self) -> list[dict]:
        if not self._path.is_file():
            return []
        entries = []
        with open(self._path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # 손상 행은 건너뛴다 — 원장은 감사 보조 수단이다
        return entries

    # ── 기록 ─────────────────────────────────────────────

    def record_start(self, job_id: str, *, mode: str, model: str) -> None:
        self._append(
            {"event": "start", "date": _today(), "job_id": job_id,
             "mode": mode, "model": model}
        )

    def record_finish(
        self,
        job_id: str,
        *,
        state: str,
        usage: dict | None,
        duration_seconds: float | None,
    ) -> None:
        self._append(
            {"event": "finish", "date": _today(), "job_id": job_id,
             "state": state, "usage": usage or {},
             "duration_seconds": duration_seconds}
        )

    # ── 예산 ─────────────────────────────────────────────

    def calls_today(self) -> int:
        today = _today()
        return sum(
            1 for e in self._entries()
            if e.get("event") == "start" and e.get("date") == today
        )

    def check_budget(self, limit: int) -> None:
        used = self.calls_today()
        if used >= limit:
            raise BudgetExceeded(
                f"일일 호출 예산 초과: 오늘 {used}회 시작 / 상한 {limit}회 "
                "(.agy-bridge.toml [limits] daily_call_budget). "
                "자정(로컬)에 초기화된다. 정말 필요하면 상한을 올려라."
            )

    # ── 리포트 ───────────────────────────────────────────

    def report(self, limit: int) -> dict:
        today = _today()
        entries = [e for e in self._entries() if e.get("date") == today]
        starts = [e for e in entries if e.get("event") == "start"]
        finishes = [e for e in entries if e.get("event") == "finish"]
        tokens = sum(
            int((e.get("usage") or {}).get("total_tokens") or 0) for e in finishes
        )
        by_state: dict[str, int] = {}
        for entry in finishes:
            by_state[entry.get("state", "?")] = by_state.get(entry.get("state", "?"), 0) + 1
        return {
            "date": today,
            "calls_started": len(starts),
            "daily_call_budget": limit,
            "remaining": max(0, limit - len(starts)),
            "total_tokens": tokens,
            "finished_by_state": by_state,
        }
