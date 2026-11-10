"""Unit tests for Sprint 6: SQLite ledger, card formatting, and end-to-end scanner execution."""
import pytest
from datetime import date
import pandas as pd
import numpy as np

from signals.ledger import SignalLedger, generate_signal_hash
from signals.reconciler import PaperTradeReconciler
from alerts.formatter import format_trade_alert_card, format_eod_summary_card
from main import run_eod_scan
from data.store.parquet_store import ParquetStore


def test_sqlite_ledger_deduplication(tmp_path):
    """Verify that identical signals are deduplicated by SHA-256 hash."""
    db_file = tmp_path / "test_signals.db"
    ledger = SignalLedger(db_path=db_file)

    signal = {
        "symbol": "RELIANCE",
        "strategy": "TrendBreakout_v1",
        "timeframe": "1D",
        "bar_timestamp": "2026-11-06T15:30:00",
        "action": "BUY",
        "entry_ref": 2850.0,
        "stop_loss": 2780.0,
        "target": 3025.0,
        "risk_reward_ratio": 2.5,
        "calculated_qty": 14,
        "capital_required": 39900.0,
        "risk_budget": 980.0
    }

    # First insert -> True
    assert ledger.record_if_unique(signal) is True

    # Immediate duplicate insert -> False
    assert ledger.record_if_unique(signal) is False

    # Mark notified
    signal_hash = signal["signal_hash"]
    ledger.mark_notified(signal_hash)
    record = ledger.get_signal(signal_hash)
    assert record["status"] == "NOTIFIED"
    assert record["telegram_sent_at"] is not None


def test_trade_alert_card_formatter():
    """Verify Markdown card formatting contains all required parameters."""
    signal = {
        "symbol": "RELIANCE",
        "action": "BUY",
        "strategy": "TrendBreakout_v1",
        "timeframe": "1D",
        "bar_timestamp": "2026-11-06",
        "entry_ref": 2850.0,
        "stop_loss": 2780.0,
        "target": 3025.0,
        "risk_reward_ratio": 2.5,
        "calculated_qty": 14,
        "capital_required": 39900.0,
        "risk_budget": 980.0,
        "capital_allocation_pct": 9.9,
        "limiting_factor": "CAPITAL_CAP"
    }

    card = format_trade_alert_card(signal, available_cash=100000.0)

    assert "BUY SIGNAL — RELIANCE" in card
    assert "₹2,850.00" in card
    assert "₹2,780.00" in card
    assert "₹3,025.00" in card
    assert "14 shares" in card
    assert "CAPITAL_CAP" in card
    assert "Available Cash : `₹100,000.00`" in card


def test_eod_scanner_dry_run_idempotency(tmp_path, mocker):
    """
    Verify complete EOD scan pipeline:
    - First run processes symbols, records signals, and sends alert.
    - Second immediate run identifies signals as duplicate and sends 0 duplicate alerts.
    """
    # 1. Setup mock store with trending data for RELIANCE
    store = ParquetStore(base_dir=tmp_path / "store")
    mocker.patch("main.get_parquet_store", return_value=store)

    dates = pd.date_range("2025-01-01", periods=250, freq="D")
    np.random.seed(42)
    closes = np.linspace(1000, 2500, 250)
    highs = closes + 10.0
    lows = closes - 10.0
    opens = (highs + lows) / 2.0
    volumes = np.random.randint(200000, 500000, 250)
    # Ensure last bar is breakout with high volume
    highs[-1] = 2600.0
    closes[-1] = 2580.0
    volumes[-1] = 900000

    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes
    }, index=pd.Index(dates, name="timestamp"))
    store.save_bars_atomic("RELIANCE", df, timeframe="1D")

    # 2. Setup mock SQLite ledger
    test_db = tmp_path / "signals.db"
    mocker.patch("main.SignalLedger", return_value=SignalLedger(db_path=test_db))

    # 3. Mock Telegram Notifier
    mock_notifier = mocker.MagicMock()
    mock_notifier.send_trade_card.return_value = True
    mock_notifier.send_health_alert.return_value = True
    mocker.patch("main.get_notifier", return_value=mock_notifier)

    # 4. First Scan Execution
    res1 = run_eod_scan(dry_run=True, specific_symbols=["RELIANCE"])
    assert res1["status"] == "COMPLETED"
    assert res1["symbols_scanned"] == 1
    assert res1["signals_emitted"] == 1
    assert mock_notifier.send_trade_card.call_count == 1

    # 5. Second Immediate Scan Execution (Idempotency test)
    mock_notifier.send_trade_card.reset_mock()
    res2 = run_eod_scan(dry_run=True, specific_symbols=["RELIANCE"])
    assert res2["status"] == "COMPLETED"
    # Should emit 0 new alerts because signal was deduplicated!
    assert res2["signals_emitted"] == 0
    assert mock_notifier.send_trade_card.call_count == 0


def test_paper_trade_reconciler_lifecycle(tmp_path):
    """Verify paper trading position opening and settled bar exit reconciliation."""
    test_db = tmp_path / "paper_signals.db"
    store = ParquetStore(base_dir=tmp_path / "store")
    reconciler = PaperTradeReconciler(db_path=test_db, store=store)

    signal = {
        "signal_hash": "mock_hash_123",
        "symbol": "INFY",
        "entry_ref": 1500.0,
        "stop_loss": 1450.0,
        "target": 1625.0,
        "calculated_qty": 20
    }

    # 1. Open trade
    opened = reconciler.create_paper_trade_from_signal(signal)
    assert opened is True
    assert len(reconciler.get_open_positions()) == 1

    # 2. Add market bars where bar 3 hits the target
    dates = pd.date_range(date.today().isoformat(), periods=3, freq="D")
    bars_df = pd.DataFrame({
        "open": [1500.0, 1520.0, 1630.0],
        "high": [1510.0, 1550.0, 1640.0], # Bar 3 hits target (1625)
        "low": [1490.0, 1500.0, 1610.0],
        "close": [1505.0, 1540.0, 1635.0],
        "volume": [200000, 250000, 400000]
    }, index=pd.Index(dates, name="timestamp"))
    store.save_bars_atomic("INFY", bars_df, timeframe="1D")

    # 3. Reconcile
    closed = reconciler.reconcile_open_positions()
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "TARGET"
    assert closed[0]["net_pnl"] > 0
    assert len(reconciler.get_open_positions()) == 0

    # 4. Verify closed trades query and loss streak calculation
    closed_trades = reconciler.get_closed_trades()
    assert len(closed_trades) == 1
    assert closed_trades[0]["symbol"] == "INFY"
    # Target was hit (winner) -> consecutive losses streak should be 0
    assert reconciler.get_consecutive_losses() == 0
    assert reconciler.get_days_since_last_loss() is None
