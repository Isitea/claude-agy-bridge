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
