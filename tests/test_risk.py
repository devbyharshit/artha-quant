"""Unit tests for Sprint 5: Dual-constraint position sizing and portfolio circuit breakers."""
import numpy as np
import pandas as pd
import pytest

from risk.position_sizer import calculate_position_size
from risk.circuit_breakers import PortfolioRiskGovernor


def test_position_sizer_standard_case():
    """Verify standard sizing when risk budget is the binding constraint."""
    portfolio = 100000.0   # ₹1 Lakh
    risk_pct = 1.0         # ₹1,000 risk budget
    cap_pct = 10.0         # ₹10,000 max capital
    entry = 500.0
    stop = 450.0           # ₹50 stop distance

    res = calculate_position_size(portfolio, risk_pct, cap_pct, entry, stop)

    # qty_by_risk = 1000 / 50 = 20 shares
    # qty_by_cap = 10000 / 500 = 20 shares
    assert res.quantity == 20
    assert res.capital_required == 10000.0
    assert res.risk_amount == 1000.0


def test_position_sizer_tight_stop_triggers_cap():
    """
    CRITICAL TEST:
    Verify that an ultra-tight stop does NOT allow the system to allocate 60%+ of capital.
    """
    portfolio = 100000.0   # ₹1 Lakh
    risk_pct = 1.5         # ₹1,500 risk budget
    cap_pct = 10.0         # ₹10,000 max capital (10%)
    entry = 2000.0
    stop = 1975.0          # Stop distance is only ₹25!

    res = calculate_position_size(portfolio, risk_pct, cap_pct, entry, stop)

    # Naive sizing would buy: 1500 / 25 = 60 shares (₹1,20,000 = 120% of capital!)
    # Dual cap must limit to: 10000 / 2000 = 5 shares (₹10,000 = 10% of capital)
    assert res.qty_by_risk == 60
    assert res.qty_by_cap == 5
    assert res.quantity == 5
    assert res.limiting_factor == "CAPITAL_CAP"
    assert res.capital_required <= 10000.0
    assert res.capital_allocation_pct <= 10.0


def test_position_sizer_handles_zero_stop():
    """Verify graceful handling when stop equals entry."""
    res = calculate_position_size(100000.0, 1.0, 10.0, 100.0, 100.0)
    assert res.quantity == 0


def test_circuit_breaker_drawdown_halt():
    """Verify 15% drawdown halts all candidate signals."""
    governor = PortfolioRiskGovernor(max_drawdown_limit_pct=15.0)

    # 1. 10% Drawdown (₹100k -> ₹90k) -> Should Approve
    eval_pass = governor.evaluate_candidate(
        symbol="RELIANCE",
        sector="Energy",
        capital_required=10000.0,
        portfolio_peak_capital=100000.0,
        portfolio_current_capital=90000.0,
        available_cash=50000.0,
        active_positions=[]
    )
    assert eval_pass.approved is True

    # 2. 16% Drawdown (₹100k -> ₹84k) -> Should VETO
    eval_fail = governor.evaluate_candidate(
        symbol="RELIANCE",
        sector="Energy",
        capital_required=10000.0,
        portfolio_peak_capital=100000.0,
        portfolio_current_capital=84000.0,
        available_cash=50000.0,
        active_positions=[]
    )
    assert eval_fail.approved is False
    assert any("drawdown" in r.lower() for r in eval_fail.veto_reasons)


def test_circuit_breaker_sector_cap():
    """Verify max 3 positions per sector."""
    governor = PortfolioRiskGovernor(max_sector_positions=3)
    active_banking = [
        {"symbol": "HDFCBANK", "sector": "Banking"},
        {"symbol": "ICICIBANK", "sector": "Banking"},
        {"symbol": "KOTAKBANK", "sector": "Banking"}
    ]

    # 4th Banking stock should be VETOED
    eval_veto = governor.evaluate_candidate(
        symbol="SBIN",
        sector="Banking",
        capital_required=10000.0,
        portfolio_peak_capital=100000.0,
        portfolio_current_capital=100000.0,
        available_cash=50000.0,
        active_positions=active_banking
    )
    assert eval_veto.approved is False
    assert any("sector" in r.lower() for r in eval_veto.veto_reasons)

    # Different sector (e.g. IT) should be APPROVED
    eval_approve = governor.evaluate_candidate(
        symbol="INFY",
        sector="Technology",
        capital_required=10000.0,
        portfolio_peak_capital=100000.0,
        portfolio_current_capital=100000.0,
        available_cash=50000.0,
        active_positions=active_banking
    )
    assert eval_approve.approved is True


def test_circuit_breaker_correlation_filter():
    """Verify assets with > 0.75 correlation are blocked."""
    governor = PortfolioRiskGovernor(max_pairwise_correlation=0.75)

    dates = pd.date_range("2026-01-01", periods=60, freq="D")
    base_returns = pd.Series(np.random.normal(0, 0.02, 60), index=dates)
    # Highly correlated series (corr > 0.9)
    correlated_returns = base_returns + np.random.normal(0, 0.005, 60)

    eval_res = governor.evaluate_candidate(
        symbol="ICICIBANK",
        sector="Banking",
        capital_required=10000.0,
        portfolio_peak_capital=100000.0,
        portfolio_current_capital=100000.0,
        available_cash=50000.0,
        active_positions=[{"symbol": "HDFCBANK", "sector": "Banking"}],
        symbol_returns_60d=correlated_returns,
        active_returns_60d={"HDFCBANK": base_returns}
    )
    assert eval_res.approved is False
    assert any("correlation" in r.lower() for r in eval_res.veto_reasons)


def test_circuit_breaker_cash_buffer():
    """Verify trade is blocked if cash buffer is breached."""
    governor = PortfolioRiskGovernor(min_cash_buffer_pct=10.0)
    # Capital is 100k, so 10k buffer needed. Available cash is only 15k.
    # Usable cash is 5k. Trade requires 8k.
    eval_res = governor.evaluate_candidate(
        symbol="TCS",
        sector="Technology",
        capital_required=8000.0,
        portfolio_peak_capital=100000.0,
        portfolio_current_capital=100000.0,
        available_cash=15000.0,
        active_positions=[]
    )
    assert eval_res.approved is False
    assert any("insufficient usable cash" in r.lower() for r in eval_res.veto_reasons)


def test_circuit_breaker_never_add_to_losing_trade():
    """Verify Gate 5: Veto adding to an active losing position."""
    governor = PortfolioRiskGovernor()

    # 1. Active position has negative unrealized PnL -> VETO
    eval_veto_pnl = governor.evaluate_candidate(
        symbol="TATAMOTORS",
        sector="Auto",
        capital_required=10000.0,
        portfolio_peak_capital=100000.0,
        portfolio_current_capital=100000.0,
        available_cash=50000.0,
        active_positions=[{"symbol": "TATAMOTORS", "sector": "Auto", "unrealized_pnl": -1200.0}],
        candidate_entry_price=950.0
    )
    assert eval_veto_pnl.approved is False
    assert any("never add to a losing trade" in r.lower() for r in eval_veto_pnl.veto_reasons)

    # 2. Candidate entry is lower than current entry price (averaging down) -> VETO
    eval_veto_avg_down = governor.evaluate_candidate(
        symbol="TATAMOTORS",
        sector="Auto",
        capital_required=10000.0,
        portfolio_peak_capital=100000.0,
        portfolio_current_capital=100000.0,
        available_cash=50000.0,
        active_positions=[{"symbol": "TATAMOTORS", "sector": "Auto", "entry_price": 1000.0}],
        candidate_entry_price=920.0
    )
    assert eval_veto_avg_down.approved is False
    assert any("never add to a losing trade" in r.lower() for r in eval_veto_avg_down.veto_reasons)

    # 3. Active position is in profit and candidate is higher (pyramiding in profit) -> APPROVED
    eval_approve = governor.evaluate_candidate(
        symbol="TATAMOTORS",
        sector="Auto",
        capital_required=10000.0,
        portfolio_peak_capital=100000.0,
        portfolio_current_capital=100000.0,
        available_cash=50000.0,
        active_positions=[{"symbol": "TATAMOTORS", "sector": "Auto", "entry_price": 900.0, "unrealized_pnl": 5000.0}],
        candidate_entry_price=950.0
    )
    assert eval_approve.approved is True


def test_circuit_breaker_consecutive_losses_cooldown():
    """Verify Gate 6: 3 consecutive losses enforce a 3-day cooldown."""
    governor = PortfolioRiskGovernor(consecutive_losses_limit=3, loss_cooldown_days=3)

    # 1. Only 2 consecutive losses -> APPROVED
    eval_2_losses = governor.evaluate_candidate(
        symbol="INFY",
        sector="IT",
        capital_required=10000.0,
        portfolio_peak_capital=100000.0,
        portfolio_current_capital=100000.0,
        available_cash=50000.0,
        active_positions=[],
        consecutive_losses=2,
        days_since_last_loss=0
    )
    assert eval_2_losses.approved is True

    # 2. 3 consecutive losses on Day 0 -> VETOED with 3 days off
    eval_3_losses_day0 = governor.evaluate_candidate(
        symbol="INFY",
        sector="IT",
        capital_required=10000.0,
        portfolio_peak_capital=100000.0,
        portfolio_current_capital=100000.0,
        available_cash=50000.0,
        active_positions=[],
        consecutive_losses=3,
        days_since_last_loss=0
    )
    assert eval_3_losses_day0.approved is False
    assert any("consecutive loss circuit breaker active" in r.lower() for r in eval_3_losses_day0.veto_reasons)
    assert any("3 more day(s)" in r for r in eval_3_losses_day0.veto_reasons)

    # 3. 3 consecutive losses on Day 1 -> VETOED with 2 days off remaining
    eval_3_losses_day1 = governor.evaluate_candidate(
        symbol="INFY",
        sector="IT",
        capital_required=10000.0,
        portfolio_peak_capital=100000.0,
        portfolio_current_capital=100000.0,
        available_cash=50000.0,
        active_positions=[],
        consecutive_losses=3,
        days_since_last_loss=1
    )
    assert eval_3_losses_day1.approved is False
    assert any("2 more day(s)" in r for r in eval_3_losses_day1.veto_reasons)

    # 4. 3 consecutive losses on Day 3 (cooldown elapsed) -> APPROVED
    eval_3_losses_day3 = governor.evaluate_candidate(
        symbol="INFY",
        sector="IT",
        capital_required=10000.0,
        portfolio_peak_capital=100000.0,
        portfolio_current_capital=100000.0,
        available_cash=50000.0,
        active_positions=[],
        consecutive_losses=3,
        days_since_last_loss=3
    )
    assert eval_3_losses_day3.approved is True


def test_circuit_breaker_consecutive_losses_from_trade_list():
    """Verify streak computation directly from closed trade records."""
    governor = PortfolioRiskGovernor(consecutive_losses_limit=3, loss_cooldown_days=3)

    closed_trades = [
        {"symbol": "TCS", "net_pnl": -500.0, "exit_date": "2026-11-06"},
        {"symbol": "INFY", "net_pnl": -450.0, "exit_date": "2026-11-05"},
        {"symbol": "WIPRO", "net_pnl": -300.0, "exit_date": "2026-11-04"},
        {"symbol": "RELIANCE", "net_pnl": 1200.0, "exit_date": "2026-11-01"},
    ]

    streak, last_date = governor.calculate_consecutive_losses(closed_trades)
    assert streak == 3
    assert last_date == "2026-11-06"

    # Evaluated on 2026-11-07 (1 day passed < 3 days cooldown) -> VETOED
    eval_res = governor.evaluate_candidate(
        symbol="HDFCBANK",
        sector="Banking",
        capital_required=10000.0,
        portfolio_peak_capital=100000.0,
        portfolio_current_capital=100000.0,
        available_cash=50000.0,
        active_positions=[],
        recent_closed_trades=closed_trades,
        eval_date="2026-11-07"
    )
    assert eval_res.approved is False
    assert any("2 more day(s)" in r for r in eval_res.veto_reasons)

    # If the most recent trade was a winner, streak is 0 -> APPROVED
    winning_streak_trades = [
        {"symbol": "HDFCBANK", "net_pnl": 1500.0, "exit_date": "2026-11-06"},
        {"symbol": "TCS", "net_pnl": -500.0, "exit_date": "2026-11-05"},
        {"symbol": "INFY", "net_pnl": -450.0, "exit_date": "2026-11-04"},
        {"symbol": "WIPRO", "net_pnl": -300.0, "exit_date": "2026-11-03"},
    ]
    eval_win_res = governor.evaluate_candidate(
        symbol="LT",
        sector="Capital Goods",
        capital_required=10000.0,
        portfolio_peak_capital=100000.0,
        portfolio_current_capital=100000.0,
        available_cash=50000.0,
        active_positions=[],
        recent_closed_trades=winning_streak_trades,
        eval_date="2026-11-07"
    )
    assert eval_win_res.approved is True

