"""비동기 job 레지스트리 — 프로젝트별 영속화 (§5).

agy 프로세스는 분리 실행(detached)하고 stdout/stderr를 파이프가 아니라 **파일**로
받는다. 브리지(MCP 서버)가 재시작되어도 진행 중이던 결과를 잃지 않기 위해서다:
재시작 후 발견된 고아 job은 pid 생존 여부를 확인하고, 프로세스가 끝나 있으면
출력 파일을 파싱해 종결한다.

상태 전이 (§5): queued → running → completed | failed | timeout | cancelled
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Protocol

from agy_bridge.config import Config
from agy_bridge.runner import (
    AgyError,
    AgyResult,
    build_command,
    ensure_prompt_within_argv_limit,
    parse_agy_output,
)

TERMINAL_STATES = ("completed", "failed", "timeout", "cancelled")


class _SupportsClose(Protocol):
    """ContextServer의 수명 연동에 필요한 최소 표면 (§10.1) — serve.py를
    직접 import하지 않아 모듈 결합을 피한다."""

    def close(self) -> None: ...

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
    structured: bool = False                        # 구조화 판정을 요구한 호출인가
    # pid 재사용 판별용 시작 시각(/proc의 starttime, 클럭틱). 재부팅 뒤 낮은 pid는
    # 재할당되므로 이것 없이는 남의 프로세스를 죽일 수 있다.
    pid_starttime: int | None = None

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
        self._servers: dict[str, _SupportsClose] = {}  # job_id → ContextServer (§10.1)
        self._commands: dict[str, list[str]] = {}  # job_id → argv (재시도용, 메모리 전용)

    # ── 경로 ─────────────────────────────────────────────

    def _record_path(self, job_id: str) -> Path:
        return self._jobs_dir / f"{job_id}.json"

    def _stdout_path(self, job_id: str) -> Path:
        return self._jobs_dir / f"{job_id}.stdout"

    def _stderr_path(self, job_id: str) -> Path:
        return self._jobs_dir / f"{job_id}.stderr"

    @contextlib.contextmanager
    def _job_lock(self, job_id: str):
        """한 job의 read-modify-write(_finalize·cancel)를 스레드 락 + fcntl.flock으로
        감싼다. 같은 상태 디렉터리를 공유하는 다른 브리지 프로세스(같은 저장소의
        다중 세션, §7.4)가 동시에 종결·취소를 시도해도 레코드 전이가 원자적이어야
        한다 — 아니면 취소가 재시도로 되살아나거나(cancel resurrection) 고아 회수가
        이중 종결된다. job id 선점(O_EXCL)은 배정만, 이 락은 전이를 지킨다."""
        lock_path = self._jobs_dir / f"{job_id}.lock"
        # flock을 self._lock보다 먼저 잡는다 — 이웃 프로세스가 flock을 쥔 채
        # 정지해도 이 프로세스의 self._lock(다른 job 연산 공용)까지 붙잡히지
        # 않는다. 락 순서는 어디서나 flock→self._lock으로 일관된다.
        with open(lock_path, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                with self._lock:
                    yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # ── 영속화 ───────────────────────────────────────────

    def _persist(self, record: JobRecord) -> None:
        path = self._record_path(record.job_id)
        # tmp 이름에 pid+난수를 넣어 다른 프로세스의 동시 _persist와 충돌하지
        # 않게 한다 — 고정 이름이면 한쪽 tmp.replace가 FileNotFoundError로 크래시.
        tmp = path.with_suffix(f".json.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            tmp.write_text(
                json.dumps(asdict(record), ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            raise

    def _load_from_disk(self, job_id: str) -> JobRecord | None:
        path = self._record_path(job_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            # 다른 프로세스가 선점만 하고 아직 _persist하지 않은 빈 파일 —
            # 그 프로세스만 의미를 아는 상태이므로 여기서는 없는 것으로 본다.
            return None
        if not isinstance(data, dict):
            return None
        # 공유 상태 디렉터리(§7.4)에 신버전 브리지가 필드를 더 쓸 수 있다.
        # 모르는 키로 TypeError를 내면 감시 스레드가 죽으므로 걸러 낸다.
        known = {f.name for f in fields(JobRecord)}
        try:
            return JobRecord(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return None

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
        context_server: _SupportsClose | None = None,  # 서버 수명 = job 수명 (§10.1)
        job_id: str | None = None,  # claim_job_id()로 미리 선점한 id (§13)
        structured: bool = False,   # 구조화 판정 요구 여부 — 결과 검증에 쓴다
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
        process: subprocess.Popen | None = None
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
                    pid_starttime=_pid_starttime(process.pid),
                    created_at=time.time(),
                    reviewed=reviewed or [],
                    served=served or [],
                    strategy=strategy,
                    strategy_reason=strategy_reason,
                    structured=structured,
                )
                self._records[job_id] = record
                self._events[job_id] = threading.Event()
                self._watched.add(job_id)
                self._commands[job_id] = cmd
                if context_server is not None:
                    self._servers[job_id] = context_server
                self._persist(record)
            # 워처 기동을 try 안에 둔다 — 스폰 성공 뒤 _persist·Thread.start()가
            # 실패하면 이미 뜬 프로세스가 무감시로 남고 원장엔 spawn_failed로
            # 오기록된다. 그 경우 except에서 프로세스를 죽여 정합을 지킨다.
            self._start_watcher(job_id, process)
        except Exception:
            if process is not None and process.poll() is None:
                self._kill_group(process.pid)
                with contextlib.suppress(Exception):
                    process.wait(timeout=5)
            if job_id is not None:
                with self._lock:
                    self._records.pop(job_id, None)
                    self._events.pop(job_id, None)
                    self._watched.discard(job_id)
                    self._commands.pop(job_id, None)
                    self._servers.pop(job_id, None)
                    with contextlib.suppress(FileNotFoundError):
                        self._record_path(job_id).unlink()  # 실패한 start는 흔적 없음
                # 여기서 선점한 id만 반납한다 — 밖에서 선점한 id는 호출자 몫이다.
                if claimed_here:
                    self.release_claim(job_id)
            if context_server is not None:
                context_server.close()
            raise

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
        try:
            self._finalize(job_id, returncode=returncode, timed_out=timed_out)
        except Exception as exc:  # noqa: BLE001 — 감시 스레드는 절대 조용히 죽으면 안 된다
            # 여기서 죽으면 _watched에 job_id가 남아 고아 회수가 영구히 막히고
            # job이 running에 고착된다. 최선을 다해 failed로 종결시킨다.
            self._force_fail(job_id, f"종결 처리 중 예외: {exc!r}")

    def _force_fail(self, job_id: str, reason: str) -> None:
        """종결 경로 자체가 실패했을 때의 마지막 안전망. 무슨 일이 있어도
        예외를 내지 않고, 대기자와 서버를 반드시 정리한다."""
        server = None
        event = None
        with contextlib.suppress(Exception), self._lock:
            record = self._records.get(job_id)
            if record is not None and record.state not in TERMINAL_STATES:
                record.state = "failed"
                record.error = reason
                record.finished_at = time.time()
                record.pid = None
                with contextlib.suppress(Exception):
                    self._persist(record)
            self._watched.discard(job_id)
            self._commands.pop(job_id, None)
            server = self._servers.pop(job_id, None)
            event = self._events.get(job_id)
        if server is not None:
            with contextlib.suppress(Exception):
                server.close()
        if event is not None:
            event.set()

    def _finalize(
        self, job_id: str, *, returncode: int | None, timed_out: bool
    ) -> None:
        stdout = self._read_output(self._stdout_path(job_id))
        stderr = self._read_output(self._stderr_path(job_id))

        # 락 블록 밖에서 읽는 값들 — 조기 return 경로에서도 정의되도록 선대입
        # (리뷰 #8: possibly-unbound, 조기 return 제거 리팩터링에 대한 보험)
        retry_process: subprocess.Popen | None = None
        server: _SupportsClose | None = None
        event: threading.Event | None = None
        with self._job_lock(job_id):
            disk = self._load_from_disk(job_id)
            record = self._records.get(job_id) or disk
            if record is None:
                return
            # 디스크가 권위다: 다른 브리지 프로세스가 이미 종결·취소했으면 메모리의
            # 낡은 running 레코드로 덮어쓰지 않는다 (cancel resurrection·이중 종결
            # 방지). 자기 메모리가 이미 종결이면 재진입도 차단한다.
            if disk is not None and disk.state in TERMINAL_STATES:
                self._records[job_id] = disk
                self._watched.discard(job_id)
                self._commands.pop(job_id, None)
                server = self._servers.pop(job_id, None)
                # 로컬 대기자(wait())도 즉시 깨운다 — 종결은 다른 프로세스가 했다.
                event = self._events.get(job_id)
                if server is not None:
                    server.close()
                if event is not None:
                    event.set()
                return
            if record.state in TERMINAL_STATES:
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
                        record.attempts += 1  # 훅은 "이제 시작할 회차"를 본다
                        try:
                            # 훅이 예산을 확인하고 선기록한다 — 스폰 전에 부르므로
                            # 토큰을 두 번 쓰는 이 경로에서도 "스폰 전 거부"가
                            # 성립한다. 거부(BudgetExceeded)나 I/O 오류는 예외로
                            # 올라와 재시도를 취소시킨다.
                            if self._on_retry is not None:
                                self._on_retry(record)
                            retry_process = self._spawn(job_id, self._commands[job_id])
                        except Exception as retry_exc:  # noqa: BLE001
                            # 재시도 거부·스폰 실패(fork EAGAIN, 바이너리 소실 등).
                            # 보호 없이 두면 감시 스레드가 죽어 job이 영구 running.
                            retry_process = None
                            record.attempts -= 1  # 실제로 뜨지 않았다
                            record.state = "failed"
                            record.error = (
                                f"재시도 스폰 실패: {retry_exc}. 원 오류: {exc}"
                            )
                        else:
                            record.pid = retry_process.pid
                            record.pid_starttime = _pid_starttime(retry_process.pid)
                            self._records[job_id] = record
                            self._persist(record)
                    else:
                        record.state = "failed"
                        record.error = str(exc)

            if retry_process is None:
                record.finished_at = time.time()
                record.pid = None
                record.pid_starttime = None
                self._watched.discard(job_id)
                self._commands.pop(job_id, None)
                server = self._servers.pop(job_id, None)
                # 서버는 종결 상태를 공개하기 **전에** 닫는다 (§10.1 서버 수명 =
                # job 수명). 락을 푼 뒤에 닫으면 그 창에서 관측자가 completed를
                # 보는데 포트는 아직 열려 있다.
                if server is not None:
                    with contextlib.suppress(Exception):
                        server.close()
                    server = None
                self._records[job_id] = record
                self._persist(record)
                event = self._events.setdefault(job_id, threading.Event())

        if retry_process is not None:
            self._start_watcher(job_id, retry_process)
            return

        if server is not None:
            with contextlib.suppress(Exception):
                server.close()  # 서버 수명 = job 수명 (§10.1)
        if event is not None:
            event.set()

        if self._on_complete is not None:
            # 원장·세션 I/O 오류가 감시 스레드를 죽이거나, 이미 완료된 job에
            # 엉뚱한 오류를 표면화하면 안 된다 (_on_retry와 동일한 취급).
            with contextlib.suppress(Exception):
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
            cached = self._records.get(job_id)
            watched = job_id in self._watched
            # 이 프로세스가 감시 중인 job만 메모리가 최신이다. 그 밖에는 디스크가
            # 권위 — 캐시된 레코드는 항상 truthy라 `cached or _load_from_disk()`가
            # 디스크를 영영 다시 읽지 않아, 재시도로 바뀐 pid를 놓치고 남의 job을
            # 죽은 pid 기준으로 오종결했다.
            record = cached if watched else (self._load_from_disk(job_id) or cached)
            if record is None:
                raise UnknownJob(job_id)
            self._records[job_id] = record
            orphan = record.state == "running" and not watched
            pid = record.pid
            starttime = record.pid_starttime

        if orphan:
            if _is_our_process(pid, starttime):
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

    def shutdown(self) -> list[str]:
        """브리지 종료 시 정리 (§10.1). 서빙 자료에 의존하는 job은 중단시킨다.

        루프백 서버는 이 프로세스와 함께 죽는데 agy는 분리 실행이라 계속 돈다.
        그대로 두면 검증자가 자료를 하나도 못 읽은 채 낸 답이 다음 기동 때
        completed로 회수된다 — §2.3-A가 막으려는 침묵 성공 그대로다.
        인라이닝만 쓴 job은 프롬프트에 자료가 이미 들어 있으므로 계속 두어
        재시작 생존(§5)을 유지한다.
        """
        with self._lock:
            served_jobs = list(self._servers)
        stopped = []
        for job_id in served_jobs:
            with contextlib.suppress(Exception):
                record = self.cancel(job_id)
                if record.state == "cancelled":
                    record.error = (
                        "브리지가 종료되어 서빙 자료(루프백 URL)가 끊겼다. "
                        "검증자가 자료를 읽지 못한 답을 내지 않도록 중단했다 "
                        "(§10.1). 브리지 재기동 후 다시 요청하라."
                    )
                    with contextlib.suppress(Exception), self._job_lock(job_id):
                        self._persist(record)
                stopped.append(job_id)
        return stopped

    def cancel(self, job_id: str) -> JobRecord:
        record = self.get(job_id)
        if record.state in TERMINAL_STATES:
            return record

        with self._job_lock(job_id):
            # 디스크 권위 재확인 — 다른 프로세스가 이미 종결·취소했을 수 있다.
            disk = self._load_from_disk(job_id)
            if disk is not None and disk.state in TERMINAL_STATES:
                self._records[job_id] = disk
                return disk
            record = self._records.get(job_id) or record
            if record.state in TERMINAL_STATES:
                return record
            event = self._events.setdefault(job_id, threading.Event())
            # 우리 자식임이 확인될 때만 신호를 보낸다 — 재부팅을 넘긴 레코드의
            # pid는 재할당됐을 수 있고, 그러면 무관한 프로세스 그룹을 죽인다.
            pid = record.pid if _is_our_process(record.pid, record.pid_starttime) else None
            # 프로세스를 죽이기 전에 먼저 cancelled를 (같은 per-job 락 아래) 디스크에
            # 영속화한다 — 소유 프로세스의 _finalize가 디스크를 재로드해 이 상태를
            # 존중하므로, 프로세스 사망을 failed·retry로 오인해 덮어쓰지 않는다.
            record.state = "cancelled"
            record.error = "사용자 요청으로 중단됨 (agy_cancel)."
            record.finished_at = time.time()
            record.pid = None
            record.pid_starttime = None
            self._watched.discard(job_id)
            self._commands.pop(job_id, None)
            server = self._servers.pop(job_id, None)
            if server is not None:
                # 종결 공개 전에 닫는다 (§10.1) — _finalize와 동일한 순서
                with contextlib.suppress(Exception):
                    server.close()
            self._records[job_id] = record
            self._persist(record)
        event.set()

        # 종료 신호가 먼저다 — 훅(원장·세션 I/O)이 예외를 던지면 pid는 이미
        # null로 영속화돼 있어 누구도 이 프로세스에 다시 도달할 수 없다.
        if pid is not None:
            self._kill_group(pid, signal.SIGTERM)
            deadline = time.time() + 5
            while time.time() < deadline and _pid_alive(pid):
                time.sleep(0.1)
            if _pid_alive(pid):
                self._kill_group(pid, signal.SIGKILL)

        if self._on_complete is not None:
            with contextlib.suppress(Exception):
                self._on_complete(record, None)
        return record


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_starttime(pid: int) -> int | None:
    """/proc/<pid>/stat의 starttime(22번 필드). pid 재사용 판별용 — 재부팅을 넘겨
    살아남은 레코드의 pid는 다른 프로세스에 재할당됐을 수 있다."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        # comm 필드에 공백·괄호가 들어갈 수 있으므로 마지막 ')' 뒤부터 자른다
        tail = stat[stat.rindex(")") + 2 :].split()
        return int(tail[19])
    except (ValueError, IndexError):
        return None


def _is_our_process(pid: int | None, starttime: int | None) -> bool:
    """기록해 둔 시작 시각과 일치할 때만 우리 자식으로 인정한다. 시작 시각을
    모르는 옛 레코드는 생존 여부만으로 판정한다(하위 호환)."""
    if pid is None:
        return False
    if not _pid_alive(pid):
        return False
    if starttime is None:
        return True
    return _pid_starttime(pid) == starttime
