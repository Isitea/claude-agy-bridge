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
    served: list = field(default_factory=list)      # 전략 C로 서빙된 파일 (§4.3)
    strategy: str = "inline"                        # inline | mixed | serve
    strategy_reason: str | None = None              # auto 전환 사유 — 도구 결과에 명시
    attempts: int = 1                               # 인프라 실패 재시도 횟수 포함

    def elapsed_s(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return round(end - self.created_at, 1)


class UnknownJob(KeyError):
    pass


class JobRegistry:
    """job의 생성·감시·영속화·회수. 모든 공개 메서드는 스레드 안전하다."""

    # 인프라 실패(agy 비정상 종료)만 재시도한다. §2.3-A(빈 response)와 타임아웃은
    # 재시도해도 같은 결과가 나올 가능성이 높으므로 즉시 실패로 승격한다.
    MAX_ATTEMPTS = 2

    def __init__(
        self,
        config: Config,
        on_complete=None,  # (record, AgyResult | None) -> None — 종결 훅 (§6, 원장)
        on_retry=None,     # (record) -> None — 재시도 스폰 훅 (§13, 원장 계측)
    ):
        self._config = config
        self._jobs_dir = config.state_dir / "jobs"
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._records: dict[str, JobRecord] = {}
        self._events: dict[str, threading.Event] = {}
        self._watched: set[str] = set()
        self._on_complete = on_complete
        self._on_retry = on_retry
        self._servers: dict[str, object] = {}   # job_id → ContextServer (수명 연동 §10.1)
        self._commands: dict[str, list[str]] = {}  # job_id → argv (재시도용, 메모리 전용)

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
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # 다른 프로세스가 선점만 하고 아직 _persist하지 않은 빈 파일 —
            # 그 프로세스만 의미를 아는 상태이므로 여기서는 없는 것으로 본다.
            return None
        return JobRecord(**data)

    def _next_job_id(self) -> str:
        """O_CREAT|O_EXCL로 레코드 파일을 선점해 id를 원자적으로 배정한다.
        같은 상태 디렉터리를 공유하는 다른 브리지 프로세스(같은 저장소의 다중
        세션)와의 충돌은 이 선점이, 스레드 간 충돌은 _lock이 막는다. 선점된
        빈 파일은 곧 _persist가 채우며, 그 사이 다른 프로세스의 읽기는
        _load_from_disk가 None으로 처리한다."""
        highest = 0
        for entry in self._jobs_dir.iterdir():
            match = _JOB_FILE_RE.match(entry.name)
            if match:
                highest = max(highest, int(match.group(1)))
        candidate = highest + 1
        while True:
            path = self._record_path(f"j-{candidate}")
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                candidate += 1
                continue
            os.close(fd)
            return f"j-{candidate}"

    def claim_job_id(self) -> str:
        """스폰 전에 id가 필요한 호출자(원장 선기록 §13)를 위한 사전 선점."""
        with self._lock:
            return self._next_job_id()

    def release_claim(self, job_id: str) -> None:
        """선점만 되고 레코드가 영속화되지 않은 id를 반납한다.
        빈 파일일 때만 지운다 — 실제 레코드는 건드리지 않는다."""
        with self._lock:
            path = self._record_path(job_id)
            with contextlib.suppress(FileNotFoundError):
                if path.stat().st_size == 0:
                    path.unlink()

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
        served: list | None = None,
        strategy: str = "inline",
        strategy_reason: str | None = None,
        context_server=None,  # ContextServer — 서버 수명 = job 수명 (§10.1)
        job_id: str | None = None,  # claim_job_id()로 미리 선점한 id (§13)
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

        claimed_here = job_id is None
        try:
            with self._lock:
                if job_id is None:
                    job_id = self._next_job_id()
                process = self._spawn(job_id, cmd)
                record = JobRecord(
                    job_id=job_id,
                    state="running",
                    mode=mode,
                    question_head=question[:200],
                    session_id=session_id,
                    pid=process.pid,
                    created_at=time.time(),
                    reviewed=reviewed or [],
                    served=served or [],
                    strategy=strategy,
                    strategy_reason=strategy_reason,
                )
                self._records[job_id] = record
                self._events[job_id] = threading.Event()
                self._watched.add(job_id)
                self._commands[job_id] = cmd
                if context_server is not None:
                    self._servers[job_id] = context_server
                self._persist(record)
        except Exception:
            # 스폰 실패 시에도 서버를 유휴 상태로 남기지 않는다 (§10.1).
            # 여기서 선점한 id는 반납한다 — 밖에서 선점한 id는 호출자 몫이다.
            if claimed_here and job_id is not None:
                self.release_claim(job_id)
            if context_server is not None:
                context_server.close()
            raise

        self._start_watcher(job_id, process)
        return record

    def _spawn(self, job_id: str, cmd: list[str]) -> subprocess.Popen:
        with (
            open(self._stdout_path(job_id), "wb") as stdout_file,
            open(self._stderr_path(job_id), "wb") as stderr_file,
        ):
            return subprocess.Popen(
                cmd,
                stdout=stdout_file,
                stderr=stderr_file,
                cwd=str(self._config.scratch_dir),
                start_new_session=True,  # 프로세스 그룹 분리 — 하드 킬과 재시작 생존
            )

    def _start_watcher(self, job_id: str, process: subprocess.Popen) -> None:
        threading.Thread(
            target=self._watch, args=(job_id, process), daemon=True,
            name=f"watch-{job_id}",
        ).start()

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

        retry_process: subprocess.Popen | None = None
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
                    if self._is_retryable(exc) and record.attempts < self.MAX_ATTEMPTS \
                            and job_id in self._commands:
                        record.attempts += 1
                        retry_process = self._spawn(job_id, self._commands[job_id])
                        record.pid = retry_process.pid
                        self._records[job_id] = record
                        self._persist(record)
                    else:
                        record.state = "failed"
                        record.error = str(exc)

            if retry_process is None:
                record.finished_at = time.time()
                record.pid = None
                self._records[job_id] = record
                self._persist(record)
                self._watched.discard(job_id)
                server = self._servers.pop(job_id, None)
                self._commands.pop(job_id, None)
                event = self._events.setdefault(job_id, threading.Event())

        if retry_process is not None:
            if self._on_retry is not None:
                self._on_retry(record)
            self._start_watcher(job_id, retry_process)
            return

        if server is not None:
            server.close()  # 서버 수명 = job 수명, 모든 종결 경로에서 보장 (§10.1)
        event.set()

        if self._on_complete is not None:
            self._on_complete(record, result)

    @staticmethod
    def _is_retryable(exc: AgyError) -> bool:
        """agy 비정상 종료(인프라 실패)만 재시도. §2.3-A 빈 응답은 재시도 금지 —
        권한 거부는 다시 실행해도 같은 결과이고, 재시도가 침묵을 두 배로 만든다."""
        return exc.returncode is not None and exc.returncode != 0

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
            server = self._servers.pop(job_id, None)
            self._commands.pop(job_id, None)
        if server is not None:
            server.close()  # §10.1 — 취소 경로에서도 서버를 남기지 않는다
        event.set()
        if self._on_complete is not None:
            self._on_complete(record, None)

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
