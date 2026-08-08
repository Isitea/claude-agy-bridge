"""비동기 job 레지스트리 검증 (§5): non-blocking 회수, 재시작 복구, 취소, 실패 승격."""

from __future__ import annotations

import json
import time

import pytest

from agy_bridge.jobs import JobRegistry

PAYLOAD = {
    "conversation_id": "conv-async",
    "status": "SUCCESS",
    "response": "느린 리뷰 결과입니다.",
    "duration_seconds": 1.0,
    "usage": {"total_tokens": 100},
}


def _start(registry, **kwargs):
    defaults = {"mode": "review", "question": "질문"}
    defaults.update(kwargs)
    return registry.start("프롬프트", **defaults)


def _wait_terminal(registry, job_id, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = registry.wait(job_id, 0.2)
        if record.state in ("completed", "failed", "timeout", "cancelled"):
            return record
    raise AssertionError(f"job {job_id}가 {timeout}s 안에 종결되지 않음")


def test_slow_job_returns_running_then_completes(bridge_config, fake_agy):
    """Phase 2 완료 기준의 축소판: 대기 창을 넘긴 리뷰를 non-blocking으로 회수."""
    agy = fake_agy(PAYLOAD, sleep_seconds=2)
    registry = JobRegistry(bridge_config(agy))

    record = _start(registry)
    record = registry.wait(record.job_id, 0.3)  # 동기 창을 짧게 → running
    assert record.state == "running"

    record = _wait_terminal(registry, record.job_id)
    assert record.state == "completed"
    assert record.result["response"] == "느린 리뷰 결과입니다."
    assert record.result["conversation_id"] == "conv-async"


def test_fast_job_completes_within_window(bridge_config, fake_agy):
    registry = JobRegistry(bridge_config(fake_agy(PAYLOAD)))
    record = _start(registry)
    record = registry.wait(record.job_id, 10)
    assert record.state == "completed"


def test_job_persisted_and_recovered_by_new_registry(bridge_config, fake_agy):
    """서버 재시작 시나리오 (§5): 새 레지스트리가 디스크에서 job을 회수한다."""
    config = bridge_config(fake_agy(PAYLOAD, sleep_seconds=1.2))
    first = JobRegistry(config)
    record = _start(first)
    job_id = record.job_id

    # "재시작": 같은 상태 디렉터리를 보는 새 레지스트리 (감시 스레드 없음)
    second = JobRegistry(config)
    recovered = second.get(job_id)
    assert recovered.state == "running"  # 프로세스가 아직 살아 있다

    deadline = time.time() + 15
    while time.time() < deadline:
        recovered = second.get(job_id)
        if recovered.state != "running":
            break
        time.sleep(0.2)
    # 프로세스 종료 후: 고아 회수 경로가 출력 파일을 파싱해 종결
    assert recovered.state == "completed"
    assert recovered.result["response"] == "느린 리뷰 결과입니다."


def test_silent_failure_is_promoted_in_async_path(bridge_config, fake_agy):
    """§2.3-A 승격이 비동기 경로에서도 동일하게 동작해야 한다."""
    payload = {**PAYLOAD, "response": ""}
    registry = JobRegistry(bridge_config(fake_agy(payload, stderr="auto-denied")))
    record = _start(registry)
    record = _wait_terminal(registry, record.job_id)
    assert record.state == "failed"
    assert "비어" in record.error
    assert "auto-denied" in record.error


def test_cancel_running_job(bridge_config, fake_agy):
    registry = JobRegistry(bridge_config(fake_agy(PAYLOAD, sleep_seconds=30)))
    record = _start(registry)
    assert registry.wait(record.job_id, 0.2).state == "running"

    cancelled = registry.cancel(record.job_id)
    assert cancelled.state == "cancelled"
    # 감시 스레드가 나중에 종결해도 cancelled를 덮어쓰면 안 된다
    time.sleep(0.5)
    assert registry.get(record.job_id).state == "cancelled"


def test_hard_kill_timeout(bridge_config, fake_agy):
    config = bridge_config(
        fake_agy(PAYLOAD, sleep_seconds=30), hard_kill_seconds=1
    )
    registry = JobRegistry(config)
    record = _start(registry)
    record = _wait_terminal(registry, record.job_id, timeout=10)
    assert record.state == "timeout"
    assert "하드 킬" in record.error


def test_on_complete_hook_receives_result(bridge_config, fake_agy):
    calls = []
    registry = JobRegistry(
        bridge_config(fake_agy(PAYLOAD)),
        on_complete=lambda record, result: calls.append((record.job_id, result)),
    )
    record = _start(registry, session_id="sess-1")
    _wait_terminal(registry, record.job_id)
    assert len(calls) == 1
    assert calls[0][1].conversation_id == "conv-async"


def test_unknown_job_raises(bridge_config, fake_agy):
    from agy_bridge.jobs import UnknownJob

    registry = JobRegistry(bridge_config(fake_agy(PAYLOAD)))
    with pytest.raises(UnknownJob):
        registry.get("j-999")


def test_job_ids_are_monotonic_across_restarts(bridge_config, fake_agy):
    config = bridge_config(fake_agy(PAYLOAD))
    first = JobRegistry(config)
    r1 = _start(first)
    _wait_terminal(first, r1.job_id)

    second = JobRegistry(config)
    r2 = _start(second)
    assert int(r2.job_id.split("-")[1]) > int(r1.job_id.split("-")[1])


def test_job_ids_unique_across_concurrent_registries(bridge_config, fake_agy):
    """리뷰 #4: 같은 jobs/를 공유하는 두 레지스트리(다중 브리지 프로세스 모사)가
    동시에 배정해도 같은 id가 나오면 안 된다 — O_EXCL 선점이 경합을 판정한다."""
    import threading

    config = bridge_config(fake_agy(PAYLOAD))
    registries = [JobRegistry(config), JobRegistry(config)]
    barrier = threading.Barrier(2)
    claimed: list[str] = []

    def claim(registry: JobRegistry) -> None:
        barrier.wait()  # 두 스캔을 최대한 동시에 강제
        claimed.append(registry.claim_job_id())

    threads = [threading.Thread(target=claim, args=(r,)) for r in registries]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(claimed)) == 2, claimed


def test_empty_claim_file_does_not_break_readers(bridge_config, fake_agy):
    """리뷰 #4: 다른 프로세스의 선점 창(빈 j-N.json)이 이쪽의 get/list_jobs를
    JSONDecodeError로 통째로 죽이면 안 된다."""
    from agy_bridge.jobs import UnknownJob

    config = bridge_config(fake_agy(PAYLOAD))
    registry = JobRegistry(config)
    record = _start(registry)
    _wait_terminal(registry, record.job_id)

    (config.state_dir / "jobs" / "j-99.json").touch()  # 다른 프로세스의 선점 모사
    assert [r.job_id for r in registry.list_jobs()] == [record.job_id]
    with pytest.raises(UnknownJob):
        registry.get("j-99")


def test_spawn_failure_releases_claim(bridge_config):
    """스폰 실패가 빈 선점 파일을 남기면 list_jobs가 영구 고장난다 — 반납 확인."""
    registry = JobRegistry(bridge_config("/nonexistent/agy"))
    with pytest.raises(OSError):
        registry.start("p", mode="review", question="q")
    assert registry.list_jobs() == []
    assert registry.claim_job_id() == "j-1"  # 반납된 id가 재사용된다


def test_cross_process_cancel_survives_owner_retry(bridge_config):
    """후속 A: 다른 브리지 프로세스가 취소한 job을 소유 프로세스의 재시도 로직이
    되살리면 안 된다 — cancelled를 디스크 권위로 존중해야 한다."""
    # 잠깐 살아 있다가 비정상 종료(재시도 대상)하는 가짜 agy
    slow_fail = bridge_config()  # config만 필요; 스크립트는 아래에서 만든다
    import textwrap

    script = slow_fail.state_dir.parent / "slow-fail-agy"
    script.write_text(
        textwrap.dedent("""\
            #!/bin/sh
            sleep 30
            echo 'infra error' >&2
            exit 1
        """),
        encoding="utf-8",
    )
    script.chmod(0o755)
    config = bridge_config(script)

    owner = JobRegistry(config)  # 감시 스레드가 있는 소유 프로세스
    canceller = JobRegistry(config)  # 같은 상태 디렉터리를 보는 다른 프로세스

    record = owner.start("p", mode="review", question="q")
    assert owner.wait(record.job_id, 0.3).state == "running"

    cancelled = canceller.cancel(record.job_id)  # 다른 프로세스가 취소 → SIGTERM
    assert cancelled.state == "cancelled"

    # 소유 프로세스의 감시 스레드가 프로세스 사망을 보고 종결을 시도한다 —
    # 재시도로 되살리지 않고 cancelled를 유지해야 한다.
    time.sleep(1.5)
    final = owner.get(record.job_id)
    assert final.state == "cancelled", final.state
    assert final.attempts == 1  # 재스폰되지 않았다


def test_retry_spawn_failure_marks_failed_not_stuck(bridge_config, fake_agy, monkeypatch):
    """후속 A: 재시도 스폰이 OSError로 실패해도 감시 스레드가 죽어 job이 영구
    running으로 고착되면 안 된다 — failed로 종결해야 한다."""
    registry = JobRegistry(
        bridge_config(fake_agy(PAYLOAD, returncode=1, stderr="transient"))
    )
    real_spawn = registry._spawn
    n = {"count": 0}

    def flaky_spawn(job_id, cmd):
        n["count"] += 1
        if n["count"] >= 2:  # 재시도 스폰에서만 실패
            raise OSError("Cannot allocate memory")
        return real_spawn(job_id, cmd)

    monkeypatch.setattr(registry, "_spawn", flaky_spawn)
    record = registry.start("p", mode="review", question="q")
    record = _wait_terminal(registry, record.job_id)
    assert record.state == "failed"
    assert "재시도 스폰 실패" in record.error


def test_concurrent_orphan_recovery_is_single_finalize(bridge_config):
    """후속 A: 감시 없는 두 레지스트리가 같은 고아 job을 동시에 회수해도
    tmp 충돌로 크래시하거나 종결 훅이 이중 발화하면 안 된다."""
    import subprocess as sp
    import threading
    from dataclasses import asdict

    from agy_bridge.jobs import JobRecord

    config = bridge_config("/bin/true")
    jobs_dir = config.state_dir / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    dead = sp.Popen(["/bin/true"])
    dead.wait()  # 회수되어 죽은 pid — _pid_alive False

    rec = JobRecord(
        job_id="j-7", state="running", mode="review", question_head="q",
        session_id="s", pid=dead.pid, created_at=time.time(),
    )
    (jobs_dir / "j-7.json").write_text(json.dumps(asdict(rec)), encoding="utf-8")
    (jobs_dir / "j-7.stdout").write_text(json.dumps(PAYLOAD), encoding="utf-8")
    (jobs_dir / "j-7.stderr").write_text("", encoding="utf-8")

    completions: list[str] = []
    lock = threading.Lock()

    def on_complete(record, result):
        with lock:
            completions.append(record.job_id)

    errors: list[Exception] = []
    results: list[str] = []

    def recover() -> None:
        try:
            registry = JobRegistry(config, on_complete=on_complete)
            results.append(registry.get("j-7").state)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=recover) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []  # tmp 충돌 크래시 없음
    assert results == ["completed", "completed"]
    assert completions == ["j-7"]  # 종결 훅은 정확히 한 번
