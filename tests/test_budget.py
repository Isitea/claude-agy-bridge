"""호출 예산·비용 계측 검증 (§13, Phase 5)."""

from __future__ import annotations

import pytest

from agy_bridge.budget import BudgetExceeded, Ledger


def test_calls_counted_and_budget_enforced(bridge_config):
    ledger = Ledger(bridge_config())
    for index in range(3):
        ledger.check_budget(3)
        ledger.record_start(f"j-{index}", mode="review", model="m")
    with pytest.raises(BudgetExceeded, match="예산 초과"):
        ledger.check_budget(3)


def test_old_entries_do_not_count(bridge_config):
    config = bridge_config()
    ledger = Ledger(config)
    with open(config.state_dir / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write(
            '{"event": "start", "date": "2001-01-01", "job_id": "j-old", '
            '"mode": "review", "model": "m"}\n'
        )
    assert ledger.calls_today() == 0
    ledger.check_budget(1)  # 옛 기록만 있으므로 통과해야 한다


def test_report_totals_tokens(bridge_config):
    ledger = Ledger(bridge_config())
    ledger.record_start("j-1", mode="verify", model="m")
    ledger.record_finish(
        "j-1", state="completed", usage={"total_tokens": 1500}, duration_seconds=10
    )
    ledger.record_start("j-2", mode="review", model="m")
    ledger.record_finish(
        "j-2", state="failed", usage=None, duration_seconds=None
    )

    report = ledger.report(60)
    assert report["calls_started"] == 2
    assert report["remaining"] == 58
    assert report["total_tokens"] == 1500
    assert report["finished_by_state"] == {"completed": 1, "failed": 1}


def test_corrupt_ledger_lines_are_skipped(bridge_config):
    config = bridge_config()
    ledger = Ledger(config)
    ledger.record_start("j-1", mode="review", model="m")
    with open(config.state_dir / "ledger.jsonl", "a", encoding="utf-8") as handle:
        handle.write("not json at all\n")
    assert ledger.calls_today() == 1


def test_check_and_record_is_atomic_across_instances(bridge_config):
    """리뷰 #5-1: 상한 직전의 동시 호출(별개 브리지 프로세스 모사 — 락을 공유하지
    않는 두 인스턴스)이 각자 확인만 통과해 상한을 넘으면 안 된다 (flock)."""
    import threading

    config = bridge_config()
    ledgers = [Ledger(config), Ledger(config)]
    barrier = threading.Barrier(2)
    granted: list[str] = []
    denied: list[str] = []

    def attempt(ledger: Ledger, job_id: str) -> None:
        barrier.wait()
        try:
            ledger.check_and_record_start(job_id, mode="review", model="m", limit=1)
            granted.append(job_id)
        except BudgetExceeded:
            denied.append(job_id)

    threads = [
        threading.Thread(target=attempt, args=(ledger, f"j-{i}"))
        for i, ledger in enumerate(ledgers, start=1)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(granted) == 1 and len(denied) == 1, (granted, denied)


def test_spawn_failure_is_compensated(bridge_config):
    """리뷰 #5-1: 선기록 후 스폰이 실패하면 보정 엔트리가 계수에서 뺀다."""
    ledger = Ledger(bridge_config())
    ledger.check_and_record_start("j-1", mode="review", model="m", limit=1)
    with pytest.raises(BudgetExceeded):
        ledger.check_and_record_start("j-2", mode="review", model="m", limit=1)

    ledger.record_spawn_failed("j-1")
    assert ledger.calls_today() == 0
    ledger.check_and_record_start("j-2", mode="review", model="m", limit=1)

    report = ledger.report(1)
    assert report["calls_started"] == 1
    assert report["spawn_failed"] == 1


def test_retry_counts_toward_budget(bridge_config):
    """리뷰 #5-2: 재시도 스폰도 start로 계산 — 예산이 '시작된 agy 프로세스 수'."""
    ledger = Ledger(bridge_config())
    ledger.record_start("j-1", mode="review", model="m")
    ledger.record_retry("j-1", mode="review")
    assert ledger.calls_today() == 2
    report = ledger.report(60)
    assert report["calls_started"] == 2
    assert report["retries"] == 1
