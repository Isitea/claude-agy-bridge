"""비동기 job 레지스트리 — 프로젝트별 영속화 (§5).

agy 프로세스는 분리 실행(detached)하고 stdout/stderr를 파이프가 아니라 **파일**로
받는다. 브리지(MCP 서버)가 재시작되어도 진행 중이던 결과를 잃지 않기 위해서다:
재시작 후 발견된 고아 job은 pid 생존 여부를 확인하고, 프로세스가 끝나 있으면
출력 파일을 파싱해 종결한다.

상태 전이 (§5): queued → running → completed | failed | timeout | cancelled
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agy_bridge.config import Config
from agy_bridge.runner import (
    AgyError,
    AgyResult,
    build_command,
    ensure_prompt_within_argv_limit,
    parse_agy_output,
)

TERMINAL_STATES = ("completed", "failed", "timeout", "cancelled")

_JOB_FILE_RE = re.compile(r"^j-(\d+)\.json$")


@dataclass
class JobRecord:
    job_id: str
    state: str
    mode: str
    question_head: str          # 목록 표시용 앞부분만 — 전체 프롬프트는 저장하지 않는다
    session_id: str | None
    pid: int | None
    created_at: float
    finished_at: float | None = None
    error: str | None = None
    result: dict | None = None  # completed: response/conversation_id/usage/…
    reviewed: list = field(default_factory=list)

    def elapsed_s(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return round(end - self.created_at, 1)


class UnknownJob(KeyError):
    pass


class JobRegistry:
    """job의 생성·감시·영속화·회수. 모든 공개 메서드는 스레드 안전하다."""

    def __init__(
        self,
        config: Config,
        on_complete=None,  # (record, AgyResult) -> None — 세션 갱신 훅 (§6)
    ):
        self._config = config
        self._jobs_dir = config.state_dir / "jobs"
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._records: dict[str, JobRecord] = {}
        self._events: dict[str, threading.Event] = {}
        self._watched: set[str] = set()
        self._on_complete = on_complete

    # ── 경로 ─────────────────────────────────────────────

    def _record_path(self, job_id: str) -> Path:
        return self._jobs_dir / f"{job_id}.json"

    def _stdout_path(self, job_id: str) -> Path:
        return self._jobs_dir / f"{job_id}.stdout"

    def _stderr_path(self, job_id: str) -> Path:
        return self._jobs_dir / f"{job_id}.stderr"

    # ── 영속화 ───────────────────────────────────────────

    def _persist(self, record: JobRecord) -> None:
        path = self._record_path(record.job_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(asdict(record), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        tmp.replace(path)

    def _load_from_disk(self, job_id: str) -> JobRecord | None:
        path = self._record_path(job_id)
        if not path.is_file():
            return None
        return JobRecord(**json.loads(path.read_text(encoding="utf-8")))

    def _next_job_id(self) -> str:
        highest = 0
        for entry in self._jobs_dir.iterdir():
            match = _JOB_FILE_RE.match(entry.name)
            if match:
                highest = max(highest, int(match.group(1)))
        return f"j-{highest + 1}"

    # ── 시작 ─────────────────────────────────────────────

    def start(
        self,
        prompt: str,
        *,
        mode: str,
        question: str,
        session_id: str | None = None,
        conversation_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        json_schema: str | None = None,
        reviewed: list | None = None,
    ) -> JobRecord:
        ensure_prompt_within_argv_limit(prompt)
        cmd = build_command(
            prompt,
            config=self._config,
            model=model,
            effort=effort,
            conversation_id=conversation_id,
            json_schema=json_schema,
        )

        with self._lock:
            job_id = self._next_job_id()
            with (
                open(self._stdout_path(job_id), "wb") as stdout_file,
                open(self._stderr_path(job_id), "wb") as stderr_file,
            ):
                process = subprocess.Popen(
                    cmd,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    cwd=str(self._config.scratch_dir),
                    start_new_session=True,  # 프로세스 그룹 분리 — 하드 킬과 재시작 생존
                )

            record = JobRecord(
                job_id=job_id,
                state="running",
                mode=mode,
                question_head=question[:200],
                session_id=session_id,
                pid=process.pid,
                created_at=time.time(),
                reviewed=reviewed or [],
            )
            self._records[job_id] = record
            self._events[job_id] = threading.Event()
            self._watched.add(job_id)
            self._persist(record)

        watcher = threading.Thread(
            target=self._watch, args=(job_id, process), daemon=True, name=f"watch-{job_id}"
        )
        watcher.start()
        return record

    # ── 감시와 종결 ──────────────────────────────────────

    def _watch(self, job_id: str, process: subprocess.Popen) -> None:
        try:
            returncode: int | None = process.wait(timeout=self._config.hard_kill_seconds)
            timed_out = False
        except subprocess.TimeoutExpired:
            self._kill_group(process.pid)
            process.wait()
            returncode = None
            timed_out = True
        self._finalize(job_id, returncode=returncode, timed_out=timed_out)

    def _finalize(
        self, job_id: str, *, returncode: int | None, timed_out: bool
    ) -> None:
        stdout = self._read_output(self._stdout_path(job_id))
        stderr = self._read_output(self._stderr_path(job_id))

        with self._lock:
            record = self._records.get(job_id) or self._load_from_disk(job_id)
            if record is None or record.state in TERMINAL_STATES:
                return

            result: AgyResult | None = None
            if timed_out:
                record.state = "timeout"
                record.error = (
                    f"하드 킬: agy가 {self._config.hard_kill_seconds}s 안에 "
                    "종료하지 않아 프로세스 그룹을 종료했다 (§5 타임아웃 계층 3)."
                )
            else:
                try:
                    result = parse_agy_output(stdout, stderr, returncode=returncode)
                    record.state = "completed"
                    record.result = {
                        "response": result.response,
                        "conversation_id": result.conversation_id,
                        "structured_output": result.structured_output,
                        "usage": result.usage,
                        "duration_seconds": result.duration_seconds,
                    }
                except AgyError as exc:
                    record.state = "failed"
                    record.error = str(exc)

            record.finished_at = time.time()
            record.pid = None
            self._records[job_id] = record
            self._persist(record)
            self._watched.discard(job_id)
            event = self._events.setdefault(job_id, threading.Event())
        event.set()

        if result is not None and self._on_complete is not None:
            self._on_complete(record, result)

    @staticmethod
    def _read_output(path: Path) -> str:
        if not path.is_file():
            return ""
        return path.read_bytes().decode("utf-8", errors="replace")

    @staticmethod
    def _kill_group(pid: int, sig: int = signal.SIGKILL) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, sig)

    # ── 조회 ─────────────────────────────────────────────

    def get(self, job_id: str) -> JobRecord:
        """현재 상태를 반환한다. 재시작으로 고아가 된 running job은 회수를 시도한다."""
        with self._lock:
            record = self._records.get(job_id) or self._load_from_disk(job_id)
            if record is None:
                raise UnknownJob(job_id)
            self._records[job_id] = record
            orphan = (
                record.state == "running" and job_id not in self._watched
            )
            pid = record.pid

        if orphan:
            if pid is not None and _pid_alive(pid):
                return record  # 이전 서버가 띄운 프로세스가 아직 실행 중
            # 프로세스는 끝났는데 종결자가 없었다 → 출력 파일로 종결 (returncode 미상)
            self._finalize(job_id, returncode=None, timed_out=False)
            with self._lock:
                record = self._records[job_id]
        return record

    def wait(self, job_id: str, timeout: float) -> JobRecord:
        """timeout 초까지 종결을 기다린다. 종결되지 않아도 현재 레코드를 반환한다."""
        record = self.get(job_id)
        if record.state in TERMINAL_STATES or timeout <= 0:
            return record
        event = self._events.get(job_id)
        if event is not None:
            event.wait(timeout)
        else:
            # 고아 job(이전 서버 소유)은 이벤트가 없다 — 폴링으로 대기한다.
            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(min(1.0, max(0.0, deadline - time.time())))
                if self.get(job_id).state in TERMINAL_STATES:
                    break
        return self.get(job_id)

    def list_jobs(self) -> list[JobRecord]:
        with self._lock:
            known = dict(self._records)
        for entry in sorted(self._jobs_dir.glob("j-*.json")):
            job_id = entry.stem
            if job_id not in known:
                loaded = self._load_from_disk(job_id)
                if loaded:
                    known[job_id] = loaded
        return sorted(known.values(), key=lambda r: r.created_at)

    # ── 취소 ─────────────────────────────────────────────

    def cancel(self, job_id: str) -> JobRecord:
        record = self.get(job_id)
        if record.state in TERMINAL_STATES:
            return record

        with self._lock:
            record = self._records.get(job_id) or record
            if record.state in TERMINAL_STATES:
                return record
            event = self._events.setdefault(job_id, threading.Event())
            pid = record.pid
            # 프로세스를 죽이기 전에 먼저 cancelled로 표시한다 — 감시 스레드의
            # _finalize가 프로세스 사망을 failed로 오인해 덮어쓰는 경합을 막는다.
            record.state = "cancelled"
            record.error = "사용자 요청으로 중단됨 (agy_cancel)."
            record.finished_at = time.time()
            record.pid = None
            self._records[job_id] = record
            self._persist(record)
            self._watched.discard(job_id)
        event.set()

        if pid is not None:
            self._kill_group(pid, signal.SIGTERM)
            deadline = time.time() + 5
            while time.time() < deadline and _pid_alive(pid):
                time.sleep(0.1)
            if _pid_alive(pid):
                self._kill_group(pid, signal.SIGKILL)
        return record


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
