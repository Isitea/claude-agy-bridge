"""session_id ↔ conversation_id 매핑 (§6).

동일 주제의 연속 질문은 같은 conversation으로 재개해야 한다 — 캐시 히트로
비용이 크게 줄고(§2.2 실측 12.2k 캐시 히트), 검증자가 앞선 논의를 기억한다.
세션 메타는 프로젝트별 상태 디렉터리의 sessions.json에 영속화한다.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import threading
import time
from pathlib import Path

from agy_bridge.config import Config


class SessionStore:
    def __init__(self, config: Config):
        self._path: Path = config.state_dir / "sessions.json"
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def _exclusive(self):
        """read-modify-write 전 구간을 스레드 락 + fcntl.flock으로 감싼다.
        상태 디렉터리는 같은 저장소의 브리지 프로세스들이 공유하므로(§7.4),
        프로세스 내부 락만으로는 서로의 갱신을 덮어쓴다 (lost update, 리뷰 #4).
        conversation_id가 유실되면 세션 연속성(캐시 히트)이 끊어진다."""
        lock_path = self._path.with_suffix(".lock")
        with self._lock, open(lock_path, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load(self) -> dict:
        if not self._path.is_file():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # 세션 파일 손상은 치명적이지 않다 — 새로 시작하되 원본은 남겨 둔다.
            self._path.rename(self._path.with_suffix(".json.corrupt"))
            return {}

    def _save(self, sessions: dict) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(sessions, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(self._path)

    def resolve(self, session_id: str) -> dict | None:
        """세션 메타를 반환한다. 없으면 None (호출자가 새 세션으로 시작)."""
        with self._exclusive():
            return self._load().get(session_id)

    def record_use(
        self,
        session_id: str,
        *,
        conversation_id: str,
        mode: str,
        usage: dict | None = None,
    ) -> None:
        """호출 완료 시 매핑을 갱신한다. 처음 보는 session_id면 생성한다."""
        now = time.time()
        tokens = int((usage or {}).get("total_tokens") or 0)
        with self._exclusive():
            sessions = self._load()
            meta = sessions.get(session_id) or {
                "conversation_id": conversation_id,
                "created_at": now,
                "turns": 0,
                "total_tokens": 0,
                "last_mode": mode,
            }
            meta["conversation_id"] = conversation_id or meta["conversation_id"]
            meta["turns"] += 1
            meta["total_tokens"] += tokens
            meta["last_mode"] = mode
            meta["last_used_at"] = now
            sessions[session_id] = meta
            self._save(sessions)

    def list_sessions(self) -> dict:
        with self._exclusive():
            return self._load()

    def close(self, session_id: str) -> bool:
        """매핑을 제거한다. agy 쪽 conversation은 남지만 더는 재개되지 않는다."""
        with self._exclusive():
            sessions = self._load()
            if session_id not in sessions:
                return False
            del sessions[session_id]
            self._save(sessions)
            return True
