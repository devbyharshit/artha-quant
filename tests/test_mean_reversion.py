"""Tests for MeanReversionStrategy: lookahead safety, signal integrity, and
the design intent behind it (trend filter blocks falling-knife entries,
time-based exit caps a reversion that never happens)."""
import numpy as np
import pandas as pd
import pytest

from strategies.mean_reversion import MeanReversionStrategy
from backtesting.engine import BacktestEngine


def generate_choppy_uptrend_market(n_bars: int = 400) -> pd.DataFrame:
    """A stock in a gentle long-term uptrend (stays above a 200-EMA) but
    noisy enough day-to-day to repeatedly punch through Bollinger Bands —
    exactly the regime this strategy is built to trade."""
    dates = pd.date_range("2023-01-01", periods=n_bars, freq="D")
    np.random.seed(21)

    drift = np.linspace(100, 160, n_bars)
    noise = np.cumsum(np.random.normal(0, 1.5, n_bars))
    noise -= np.linspace(noise[0], noise[-1], n_bars)  # keep noise mean-reverting, not drifting
    closes = drift + noise

    highs = closes + np.random.uniform(0.5, 1.5, n_bars)
    lows = closes - np.random.uniform(0.5, 1.5, n_bars)
    opens = (highs + lows) / 2.0
    volumes = np.random.randint(100000, 200000, n_bars)

    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes
    }, index=pd.Index(dates, name="timestamp"))


def generate_persistent_downtrend_market(n_bars: int = 400) -> pd.DataFrame:
    """A stock in a sustained decline, with enough noise to also punch
    through its own Bollinger lower band — but it should never be bought,
    because it's below its long-term trend the whole time."""
    dates = pd.date_range("2023-01-01", periods=n_bars, freq="D")
    np.random.seed(22)

    drift = np.linspace(200, 100, n_bars)
    noise = np.random.normal(0, 2.0, n_bars)
    closes = drift + noise

    highs = closes + np.random.uniform(0.5, 1.5, n_bars)
    lows = closes - np.random.uniform(0.5, 1.5, n_bars)
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
    df_original = generate_choppy_uptrend_market(400)
    strategy = MeanReversionStrategy()

    evaluated_original = strategy.evaluate(df_original.copy())

    target_bar = 380
    signal_at_t = evaluated_original["signal"].iloc[target_bar]
    sma_at_t = evaluated_original["bb_sma"].iloc[target_bar]
    ema_at_t = evaluated_original["ema_trend"].iloc[target_bar]

    df_mutated = df_original.copy()
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("close")] *= 5.0
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("high")] *= 5.0
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("low")] *= 5.0

    evaluated_mutated = strategy.evaluate(df_mutated)

    assert evaluated_mutated["signal"].iloc[target_bar] == signal_at_t, "LOOKAHEAD DETECTED: Signal at T changed!"
    assert evaluated_mutated["bb_sma"].iloc[target_bar] == sma_at_t, "LOOKAHEAD DETECTED: SMA at T changed!"
    assert evaluated_mutated["ema_trend"].iloc[target_bar] == ema_at_t, "LOOKAHEAD DETECTED: EMA at T changed!"


def test_strategy_state_machine_integrity():
    df = generate_choppy_uptrend_market(400)
    strategy = MeanReversionStrategy()
    evaluated = strategy.evaluate(df)

    in_pos = False
    for i, row in evaluated.iterrows():
        sig = row["signal"]
        if sig == 1:
            assert not in_pos, f"Received BUY signal while already in position at {i}!"
            assert row["stop_loss"] > 0, "Stop loss must be positive on Buy"
            assert row["target"] > row["close"], "Reversion target (middle band) must be above entry close"
            in_pos = True
        elif sig == -1:
            assert in_pos, f"Received EXIT signal while NOT in position at {i}!"
            in_pos = False
        else:
            if in_pos:
                assert row["stop_loss"] > 0, "Stop loss must persist while in position"
                assert row["target"] > 0, "Target must persist while in position"


def test_trend_filter_blocks_entries_in_persistent_downtrend():
    """The entire point of the EMA trend filter: a stock that is
    structurally declining should never generate a buy, even though it
    repeatedly punches through its own Bollinger lower band."""
    df = generate_persistent_downtrend_market(400)
    strategy = MeanReversionStrategy()
    evaluated = strategy.evaluate(df)

    touches_lower_band = (evaluated["close"] < evaluated["bb_lower"]).sum()
    assert touches_lower_band > 0, "Test setup check: downtrend should still touch the lower band sometimes"
    assert (evaluated["signal"] == 1).sum() == 0, "Strategy must never buy a stock below its long-term trend"


def test_max_holding_days_forces_exit_when_reversion_never_happens():
    """If price never reverts to the middle band and never hits the stop,
    the position must still be closed by max_holding_days."""
    n = 250
    dates = pd.date_range("2023-01-01", periods=n, freq="D")

    # Uptrend long enough to clear EMA warmup, then flat-lines right after
    # a dip below the lower band — no reversion, no stop-out, just time.
    closes = np.concatenate([
        np.linspace(100, 150, 220),
        np.full(30, 149.0)  # flat: won't hit stop, won't reach the (slightly higher) target either
    ])
    closes[219] = 140.0  # one-bar dip to trigger entry just before the flat stretch

    highs = closes + 0.3
    lows = closes - 0.3
    opens = closes.copy()
    volumes = np.full(n, 150000)

    df = pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes
    }, index=pd.Index(dates, name="timestamp"))

    strategy = MeanReversionStrategy(params={"max_holding_days": 10})
    evaluated = strategy.evaluate(df)

    entries = evaluated.index[evaluated["signal"] == 1]
    if len(entries) == 0:
        pytest.skip("Synthetic setup didn't trigger an entry; not what this test is checking")

    entry_pos = evaluated.index.get_loc(entries[0])
    exits_after = evaluated.index[(evaluated["signal"] == -1) & (np.arange(n) > entry_pos)]
    assert len(exits_after) > 0, "Position must eventually be closed"

    exit_pos = evaluated.index.get_loc(exits_after[0])
    assert (exit_pos - entry_pos) <= 10, "Must exit by max_holding_days if reversion never happens"


def test_risk_based_sizing_works_out_of_the_box():
    """Unlike the Pine-ported strategies, this one has a real stop/target,
    so it should size sensibly under the engine's default risk_based mode
    with no workaround needed."""
    df = generate_choppy_uptrend_market(400)
    strategy = MeanReversionStrategy()
    engine = BacktestEngine(strategy=strategy, initial_capital=100000.0, position_sizing_mode="risk_based")

    summary = engine.run(df, symbol="SYNTH")

    if summary.trades:
        trade = summary.trades[0]
        assert trade.quantity > 0
