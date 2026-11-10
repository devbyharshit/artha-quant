"""Tests for RangeFilteredTrendStrategy: lookahead safety, signal integrity,
and the full-equity backtest sizing mode it's designed to run under."""
import numpy as np
import pandas as pd
import pytest

from strategies.range_filtered_trend import RangeFilteredTrendStrategy
from backtesting.engine import BacktestEngine


def generate_synthetic_market(n_bars: int = 420) -> pd.DataFrame:
    """Deterministic synthetic OHLCV with an uptrend leg followed by a
    downtrend leg, long enough to clear the 200-bar WMA warmup with room
    for both an entry and an exit."""
    dates = pd.date_range("2024-01-01", periods=n_bars, freq="D")
    np.random.seed(7)

    half = n_bars // 2
    up_leg = np.linspace(100, 220, half)
    down_leg = np.linspace(220, 140, n_bars - half)
    drift = np.concatenate([up_leg, down_leg])
    noise = np.random.normal(0, 1.5, n_bars)
    closes = drift + noise

    highs = closes + np.random.uniform(1.0, 2.5, n_bars)
    lows = closes - np.random.uniform(1.0, 2.5, n_bars)
    opens = (highs + lows) / 2.0
    volumes = np.random.randint(100000, 200000, n_bars)

    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes
    }, index=pd.Index(dates, name="timestamp"))


def test_zero_lookahead_bias():
    """Mutating bars after T must never change the signal/indicators at T —
    critical here since kalman/supertrend are recursive filters."""
    df_original = generate_synthetic_market(420)
    strategy = RangeFilteredTrendStrategy()

    evaluated_original = strategy.evaluate(df_original.copy())

    target_bar = 400
    signal_at_t = evaluated_original["signal"].iloc[target_bar]
    kalman_at_t = evaluated_original["kalman"].iloc[target_bar]
    direction_at_t = evaluated_original["supertrend_direction"].iloc[target_bar]

    df_mutated = df_original.copy()
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("close")] *= 5.0
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("high")] *= 5.0
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("low")] *= 5.0

    evaluated_mutated = strategy.evaluate(df_mutated)

    assert evaluated_mutated["signal"].iloc[target_bar] == signal_at_t, "LOOKAHEAD DETECTED: Signal at T changed!"
    assert evaluated_mutated["kalman"].iloc[target_bar] == kalman_at_t, "LOOKAHEAD DETECTED: Kalman at T changed!"
    assert evaluated_mutated["supertrend_direction"].iloc[target_bar] == direction_at_t, "LOOKAHEAD DETECTED: Supertrend direction at T changed!"


def test_strategy_state_machine_integrity():
    """No consecutive BUYs while in position, no EXIT without a prior BUY,
    and this strategy must never report a technical stop/target."""
    df = generate_synthetic_market(420)
    strategy = RangeFilteredTrendStrategy()
    evaluated = strategy.evaluate(df)

    in_pos = False
    for i, row in evaluated.iterrows():
        sig = row["signal"]
        if sig == 1:
            assert not in_pos, f"Received BUY signal while already in position at {i}!"
            in_pos = True
        elif sig == -1:
            assert in_pos, f"Received EXIT signal while NOT in position at {i}!"
            in_pos = False

        assert row["stop_loss"] == 0.0, "This strategy must never report a technical stop-loss"
        assert row["target"] == 0.0, "This strategy must never report a technical target"


def test_kalman_filter_converges_on_constant_price():
    """A constant price series should converge to that constant with no
    overshoot, and the first bar (no prior bar to seed from) is NaN."""
    prices = pd.Series([150.0] * 100)
    smoothed = RangeFilteredTrendStrategy._kalman_filter(prices, period=77, alpha=0.01, beta=0.1)

    assert np.isnan(smoothed.iloc[0])
    assert np.isclose(smoothed.iloc[-1], 150.0)


def test_supertrend_direction_is_always_pm_one_or_nan():
    df = generate_synthetic_market(420)
    strategy = RangeFilteredTrendStrategy()
    evaluated = strategy.evaluate(df)

    directions = evaluated["supertrend_direction"].dropna().unique()
    assert set(directions).issubset({1.0, -1.0})


def test_full_equity_sizing_mode_allocates_full_capital_and_ignores_stop_hits():
    """This strategy has no stop-loss, so BacktestEngine must size on equity,
    not risk distance, and must never fire a phantom stop-loss exit."""
    df = generate_synthetic_market(420)
    strategy = RangeFilteredTrendStrategy()
    engine = BacktestEngine(strategy=strategy, initial_capital=100000.0, position_sizing_mode="full_equity")

    summary = engine.run(df, symbol="SYNTH")

    assert all(t.exit_reason != "STOP_LOSS" for t in summary.trades), "Stop-less strategy must never exit via STOP_LOSS"
    assert all(t.exit_reason != "TARGET" for t in summary.trades), "Stop-less strategy must never exit via TARGET"
    if summary.trades:
        first_trade = summary.trades[0]
        approx_full_alloc = 100000.0 / first_trade.entry_price
        assert first_trade.quantity == int(approx_full_alloc)


def test_risk_based_mode_undersizes_stop_less_signals():
    """Sanity check: risk_based sizing treats a 0.0 stop_loss as "risk the
    full entry price" (stop_dist == fill_price), drastically undersizing the
    trade rather than crashing. This is exactly why full_equity mode exists
    for this strategy."""
    df = generate_synthetic_market(420)
    strategy = RangeFilteredTrendStrategy()
    engine = BacktestEngine(strategy=strategy, initial_capital=100000.0, position_sizing_mode="risk_based")

    summary = engine.run(df, symbol="SYNTH")

    if summary.trades:
        trade = summary.trades[0]
        risk_budget = 100000.0 * 0.01
        expected_qty = int(risk_budget / trade.entry_price)
        assert trade.quantity == expected_qty


def test_invalid_sizing_mode_rejected():
    df = generate_synthetic_market(50)
    strategy = RangeFilteredTrendStrategy()
    with pytest.raises(ValueError):
        BacktestEngine(strategy=strategy, position_sizing_mode="not_a_real_mode")
