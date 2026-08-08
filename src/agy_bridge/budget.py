"""호출 예산과 비용 계측 (§13, Phase 5).

원장은 프로젝트별 상태 디렉터리의 ledger.jsonl에 추가 전용으로 쌓인다.
예산은 시작된 호출 수 기준(로컬 날짜)이며, 초과 시 도구가 스폰 전에 사유와
함께 거부한다 — agy 프로세스가 뜬 뒤 거부하면 이미 비용이 나간 뒤다.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
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

    @contextlib.contextmanager
    def _exclusive(self):
        """확인·기록 임계구역 — 스레드 락 + fcntl.flock. 상태 디렉터리를 공유하는
        브리지 프로세스(같은 저장소의 다중 세션)와도 원자성이 성립해야 상한
        직전의 동시 호출이 각자 확인만 통과해 예산을 넘는 경합이 닫힌다 (§13)."""
        lock_path = self._path.with_suffix(".lock")
        with self._lock, open(lock_path, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _append(self, entry: dict) -> None:
        # 호출자는 _exclusive() 안에서 부른다.
        with open(self._path, "a", encoding="utf-8") as handle:
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
        with self._exclusive():
            self._append(
                {"event": "start", "date": _today(), "job_id": job_id,
                 "mode": mode, "model": model}
            )

    def record_retry(self, job_id: str, *, mode: str) -> None:
        """재시도 스폰도 실제 agy 프로세스 기동이다 — start로 계산해야 예산이
        '시작된 agy 프로세스 수'라는 의미를 지킨다 (리뷰 #5-2)."""
        with self._exclusive():
            self._append(
                {"event": "start", "date": _today(), "job_id": job_id,
                 "mode": mode, "retry": True}
            )

    def record_spawn_failed(self, job_id: str, *, date: str | None = None) -> None:
        """선기록된 start가 스폰에 실패했다 — 보정 엔트리로 계수에서 뺀다.

        date는 상쇄 대상 start가 기록된 날짜다. 자정 직전 start가 자정 직후
        실패해도 보정이 원래 날짜의 계수에서 빠지도록, 호출 시점이 아니라 start의
        날짜를 쓴다 (자체 리뷰). 없으면 job_id로 원장에서 start를 찾고, 그래도
        못 찾으면 오늘로 폴백한다."""
        with self._exclusive():
            if date is None:
                date = next(
                    (e.get("date") for e in reversed(self._entries())
                     if e.get("event") == "start" and e.get("job_id") == job_id),
                    _today(),
                )
            self._append(
                {"event": "spawn_failed", "date": date, "job_id": job_id}
            )

    def record_finish(
        self,
        job_id: str,
        *,
        state: str,
        usage: dict | None,
        duration_seconds: float | None,
    ) -> None:
        with self._exclusive():
            self._append(
                {"event": "finish", "date": _today(), "job_id": job_id,
                 "state": state, "usage": usage or {},
                 "duration_seconds": duration_seconds}
            )

    # ── 예산 ─────────────────────────────────────────────

    def calls_today(self) -> int:
        today = _today()
        entries = [e for e in self._entries() if e.get("date") == today]
        starts = sum(1 for e in entries if e.get("event") == "start")
        failed_spawns = sum(1 for e in entries if e.get("event") == "spawn_failed")
        return max(0, starts - failed_spawns)

    def _raise_if_exceeded(self, used: int, limit: int) -> None:
        if used >= limit:
            raise BudgetExceeded(
                f"일일 호출 예산 초과: 오늘 {used}회 시작 / 상한 {limit}회 "
                "(.agy-bridge.toml [limits] daily_call_budget). "
                "자정(로컬)에 초기화된다. 정말 필요하면 상한을 올려라."
            )

    def check_budget(self, limit: int) -> None:
        """비원자적 사전 확인 — 값싼 조기 거부용. 스폰 직전에는
        check_and_record_start가 원자적으로 다시 판정한다."""
        self._raise_if_exceeded(self.calls_today(), limit)

    def check_and_record_start(
        self, job_id: str, *, mode: str, model: str, limit: int
    ) -> str:
        """확인과 기록을 한 임계구역에서 수행한다 (리뷰 #5-1). 스폰 전에 기록하므로
        초과 스폰이 원천 차단된다 — 스폰이 실패하면 반환된 날짜로
        record_spawn_failed를 불러 같은 날 계수에서 정확히 뺀다."""
        with self._exclusive():
            self._raise_if_exceeded(self.calls_today(), limit)
            date = _today()
            self._append(
                {"event": "start", "date": date, "job_id": job_id,
                 "mode": mode, "model": model}
            )
        return date

    # ── 리포트 ───────────────────────────────────────────

    def report(self, limit: int) -> dict:
        today = _today()
        entries = [e for e in self._entries() if e.get("date") == today]
        starts = [e for e in entries if e.get("event") == "start"]
        retries = [e for e in starts if e.get("retry")]
        failed_spawns = [e for e in entries if e.get("event") == "spawn_failed"]
        finishes = [e for e in entries if e.get("event") == "finish"]
        # check_budget과 같은 보정(순계)을 써야 리포트와 거부 판정이 일치한다
        calls = max(0, len(starts) - len(failed_spawns))
        tokens = sum(
            int((e.get("usage") or {}).get("total_tokens") or 0) for e in finishes
        )
        by_state: dict[str, int] = {}
        for entry in finishes:
            by_state[entry.get("state", "?")] = by_state.get(entry.get("state", "?"), 0) + 1
        payload = {
            "date": today,
            "calls_started": calls,
            "daily_call_budget": limit,
            "remaining": max(0, limit - calls),
            "total_tokens": tokens,
            "finished_by_state": by_state,
        }
        if retries:
            payload["retries"] = len(retries)
        if failed_spawns:
            payload["spawn_failed"] = len(failed_spawns)
        return payload
