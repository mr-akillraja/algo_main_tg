"""Mechanical broker checks remain journalled but never become strategy evidence."""
import csv

import pytest

from agents.agent_asset_selector import _load_win_rates_from_journal


def write_journal(path, rows, *, state_column=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["asset", "signal", "result", "pnl"]
    if state_column:
        fields.append("state_key")
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def row(result, state_key, *, asset="EUR/USD OTC", signal="BUY"):
    return {"asset": asset, "signal": signal, "result": result,
            "pnl": 170 if result == "WIN" else -200,
            "state_key": state_key}


@pytest.mark.parametrize("relative_path", ["trade_journal.csv", "archive/trade_journal_2026-09-29.csv"])
def test_asset_rates_exclude_test_orders_and_leave_journal_intact(tmp_path, relative_path):
    path = write_journal(tmp_path / relative_path, [
        row("WIN", "DEMO_EXECUTION_TEST"),
        row("LOSS", "30SH|60SH"),
        row("WIN", "30BH|60BH"),
        row("WIN", "DEMO_EXECUTION_TEST", asset="GBP/USD OTC"),
        row("PENDING", "30BH|60BH"),
    ])
    original = path.read_bytes()
    assert _load_win_rates_from_journal(path) == {
        "EUR/USD OTC": {"wins": 1, "losses": 1, "total": 2, "win_rate": 0.5}}
    assert path.read_bytes() == original


def test_asset_rates_keep_legacy_rows_without_test_marker(tmp_path):
    path = write_journal(tmp_path / "legacy.csv", [
        {"asset": "EUR/USD OTC", "signal": "BUY", "result": "WIN", "pnl": 170}
    ], state_column=False)
    assert _load_win_rates_from_journal(path)["EUR/USD OTC"]["total"] == 1




def test_only_test_orders_provide_no_performance_or_learning_history(tmp_path):
    path = write_journal(tmp_path / "trade_journal.csv", [
        row("WIN", "DEMO_EXECUTION_TEST"), row("WIN", "DEMO_EXECUTION_TEST", signal="SELL")])
    assert _load_win_rates_from_journal(path) == {}
