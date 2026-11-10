"""Tests for EmaVwapCrossStrategy: lookahead safety, edge-triggered signal
integrity, and the fixed stop behaving sensibly under risk-based sizing."""
import numpy as np
import pandas as pd
import pytest

from strategies.ema_vwap_cross import EmaVwapCrossStrategy
from backtesting.engine import BacktestEngine


def generate_trending_market(n_bars: int = 300, seed: int = 11) -> pd.DataFrame:
    """A stock that trends up then down, noisy enough to force several
    9/20 EMA and VWAP alignment flips in both directions."""
    dates = pd.date_range("2023-01-01", periods=n_bars, freq="D")
    rng = np.random.default_rng(seed)

    up = np.linspace(100, 180, n_bars // 2)
    down = np.linspace(180, 110, n_bars - n_bars // 2)
    drift = np.concatenate([up, down])
    noise = rng.normal(0, 1.2, n_bars)
    closes = drift + noise

    highs = closes + rng.uniform(0.5, 1.5, n_bars)
    lows = closes - rng.uniform(0.5, 1.5, n_bars)
    opens = (highs + lows) / 2.0
    volumes = rng.integers(100000, 200000, n_bars)

    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes
    }, index=pd.Index(dates, name="timestamp"))


def test_zero_lookahead_bias():
    df_original = generate_trending_market(300)
    strategy = EmaVwapCrossStrategy()

    evaluated_original = strategy.evaluate(df_original.copy())

    target_bar = 280
    signal_at_t = evaluated_original["signal"].iloc[target_bar]
    ema_fast_at_t = evaluated_original["ema_fast"].iloc[target_bar]
    vwap_at_t = evaluated_original["rolling_vwap"].iloc[target_bar]

    df_mutated = df_original.copy()
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("close")] *= 5.0
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("high")] *= 5.0
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("low")] *= 5.0

    evaluated_mutated = strategy.evaluate(df_mutated)

    assert evaluated_mutated["signal"].iloc[target_bar] == signal_at_t, "LOOKAHEAD DETECTED: Signal at T changed!"
    assert evaluated_mutated["ema_fast"].iloc[target_bar] == ema_fast_at_t, "LOOKAHEAD DETECTED: EMA at T changed!"
    assert evaluated_mutated["rolling_vwap"].iloc[target_bar] == vwap_at_t, "LOOKAHEAD DETECTED: VWAP at T changed!"


def test_strategy_state_machine_integrity():
    df = generate_trending_market(300)
    strategy = EmaVwapCrossStrategy()
    evaluated = strategy.evaluate(df)

    in_pos = False
    for i, row in evaluated.iterrows():
        sig = row["signal"]
        if sig == 1:
            assert not in_pos, f"Received BUY signal while already in position at {i}!"
            assert row["stop_loss"] > 0, "Stop loss must be positive on Buy"
            assert row["stop_loss"] < row["close"], "Long stop must sit below entry close"
            in_pos = True
        elif sig == -1:
            assert in_pos, f"Received EXIT signal while NOT in position at {i}!"
            in_pos = False


def test_signal_is_edge_triggered_not_held():
    """The source script fires once on the bar alignment first becomes
    true, not on every bar it stays true — so there must be no run of
    consecutive +1 (or consecutive -1) bars."""
    df = generate_trending_market(300)
    strategy = EmaVwapCrossStrategy()
    evaluated = strategy.evaluate(df)

    signals = evaluated["signal"].values
    for i in range(1, len(signals)):
        if signals[i] != 0:
            assert signals[i - 1] != signals[i], f"Signal {signals[i]} repeated on consecutive bars at index {i}"


def test_candle_stop_loss_mode_uses_entry_low():
    df = generate_trending_market(300, seed=7)
    strategy = EmaVwapCrossStrategy(params={"stop_loss_mode": "candle"})
    evaluated = strategy.evaluate(df)

    entries = evaluated.index[evaluated["signal"] == 1]
    if len(entries) == 0:
        pytest.skip("Synthetic setup didn't trigger an entry")

    entry_row = evaluated.loc[entries[0]]
    assert entry_row["stop_loss"] == round(entry_row["low"], 2)


def test_invalid_stop_loss_mode_raises():
    with pytest.raises(ValueError):
        EmaVwapCrossStrategy(params={"stop_loss_mode": "trailing"})


def test_risk_based_sizing_works_out_of_the_box():
    """The source script sizes at 100% of equity (full_equity, the
    registry default for this strategy), but it does have a real fixed
    stop at entry, so risk_based sizing should also work with no
    workaround needed if someone chooses to run it that way."""
    df = generate_trending_market(300)
    strategy = EmaVwapCrossStrategy()
    engine = BacktestEngine(strategy=strategy, initial_capital=100000.0, position_sizing_mode="risk_based")

    summary = engine.run(df, symbol="SYNTH")

    if summary.trades:
        trade = summary.trades[0]
        assert trade.quantity > 0
