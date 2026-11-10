"""Tests for GaussianChannelStrategy: lookahead safety, signal integrity,
and the N-pole filter math it's built on."""
import numpy as np
import pandas as pd
import pytest

from strategies.gaussian_channel import GaussianChannelStrategy
from backtesting.engine import BacktestEngine


def generate_synthetic_market(n_bars: int = 400) -> pd.DataFrame:
    """Deterministic synthetic OHLCV with an uptrend leg followed by a
    downtrend leg, long enough to clear the 148-bar sampling period."""
    dates = pd.date_range("2024-01-01", periods=n_bars, freq="D")
    np.random.seed(11)

    half = n_bars // 2
    up_leg = np.linspace(100, 240, half)
    down_leg = np.linspace(240, 150, n_bars - half)
    drift = np.concatenate([up_leg, down_leg])
    noise = np.random.normal(0, 2.0, n_bars)
    closes = drift + noise

    highs = closes + np.random.uniform(1.0, 3.0, n_bars)
    lows = closes - np.random.uniform(1.0, 3.0, n_bars)
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
    critical given the recursive N-pole filter."""
    df_original = generate_synthetic_market(400)
    strategy = GaussianChannelStrategy()

    evaluated_original = strategy.evaluate(df_original.copy())

    target_bar = 380
    signal_at_t = evaluated_original["signal"].iloc[target_bar]
    filt_at_t = evaluated_original["gc_filt"].iloc[target_bar]
    hband_at_t = evaluated_original["gc_hband"].iloc[target_bar]

    df_mutated = df_original.copy()
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("close")] *= 5.0
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("high")] *= 5.0
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("low")] *= 5.0

    evaluated_mutated = strategy.evaluate(df_mutated)

    assert evaluated_mutated["signal"].iloc[target_bar] == signal_at_t, "LOOKAHEAD DETECTED: Signal at T changed!"
    assert evaluated_mutated["gc_filt"].iloc[target_bar] == filt_at_t, "LOOKAHEAD DETECTED: Gaussian filter at T changed!"
    assert evaluated_mutated["gc_hband"].iloc[target_bar] == hband_at_t, "LOOKAHEAD DETECTED: Upper band at T changed!"


def test_strategy_state_machine_integrity():
    """No consecutive BUYs while in position, no EXIT without a prior BUY,
    and this strategy must never report a technical stop/target."""
    df = generate_synthetic_market(400)
    strategy = GaussianChannelStrategy()
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


def test_n_pole_filter_converges_on_constant_price():
    """A constant price series should converge to that constant."""
    prices = pd.Series([200.0] * 300)
    alpha = GaussianChannelStrategy._gaussian_alpha(sampling_period=148, poles=7)
    filtered = GaussianChannelStrategy._n_pole_filter(prices, alpha, poles=7)

    assert np.isclose(filtered.iloc[-1], 200.0, atol=0.01)


def test_n_pole_filter_order_one_matches_simple_ema_recurrence():
    """A 1-pole filter should reduce exactly to f[t] = alpha*s[t] + (1-alpha)*f[t-1]."""
    np.random.seed(3)
    prices = pd.Series(100 + np.random.normal(0, 2, 50))
    alpha = 0.1

    filtered = GaussianChannelStrategy._n_pole_filter(prices, alpha, poles=1)

    expected = 0.0
    for i in range(len(prices)):
        expected = alpha * prices.iloc[i] + (1 - alpha) * expected
        assert np.isclose(filtered.iloc[i], expected)


def test_true_range_first_bar_uses_high_low_only():
    df = generate_synthetic_market(50)
    tr = GaussianChannelStrategy._true_range(df["high"], df["low"], df["close"])
    assert np.isclose(tr.iloc[0], df["high"].iloc[0] - df["low"].iloc[0])


def test_full_equity_sizing_mode_allocates_full_capital_and_ignores_stop_hits():
    df = generate_synthetic_market(400)
    strategy = GaussianChannelStrategy()
    engine = BacktestEngine(strategy=strategy, initial_capital=100000.0, position_sizing_mode="full_equity")

    summary = engine.run(df, symbol="SYNTH")

    assert all(t.exit_reason != "STOP_LOSS" for t in summary.trades), "Stop-less strategy must never exit via STOP_LOSS"
    assert all(t.exit_reason != "TARGET" for t in summary.trades), "Stop-less strategy must never exit via TARGET"
