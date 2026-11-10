"""Tests for SweepFvgReversalStrategy: lookahead safety, signal integrity,
and the sweep/FVG detection it's built on."""
import numpy as np
import pandas as pd

from strategies.sweep_fvg_reversal import SweepFvgReversalStrategy
from backtesting.engine import BacktestEngine


def generate_synthetic_market(n_bars: int = 600) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=n_bars, freq="D")
    np.random.seed(7)
    half = n_bars // 2
    drift = np.concatenate([np.linspace(100, 240, half), np.linspace(240, 150, n_bars - half)])
    closes = drift + np.random.normal(0, 3.0, n_bars)
    highs = closes + np.random.uniform(1.0, 4.0, n_bars)
    lows = closes - np.random.uniform(1.0, 4.0, n_bars)
    opens = (highs + lows) / 2.0
    volumes = np.random.randint(100000, 200000, n_bars)
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
                         index=pd.Index(dates, name="timestamp"))


def test_zero_lookahead_bias():
    df_original = generate_synthetic_market(600)
    strategy = SweepFvgReversalStrategy()
    evaluated_original = strategy.evaluate(df_original.copy())

    target_bar = 500
    signal_at_t = evaluated_original["signal"].iloc[target_bar]
    pivot_low_at_t = evaluated_original["pivot_low"].iloc[target_bar]

    df_mutated = df_original.copy()
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("close")] *= 5.0
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("high")] *= 5.0
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("low")] *= 5.0
    evaluated_mutated = strategy.evaluate(df_mutated)

    assert evaluated_mutated["signal"].iloc[target_bar] == signal_at_t
    if not np.isnan(pivot_low_at_t):
        assert np.isclose(evaluated_mutated["pivot_low"].iloc[target_bar], pivot_low_at_t)


def test_signal_state_machine_integrity():
    df = generate_synthetic_market(600)
    strategy = SweepFvgReversalStrategy()
    evaluated = strategy.evaluate(df)

    in_pos = False
    for i, row in evaluated.iterrows():
        sig = row["signal"]
        if sig == 1:
            assert not in_pos, f"Received BUY while already in position at {i}"
            in_pos = True
        elif sig == -1:
            assert in_pos, f"Received EXIT while NOT in position at {i}"
            in_pos = False


def test_signal_state_machine_integrity_no_resting_stop_cap():
    """With max_stop_atr disabled, every exit (stop/target/flip) must be a
    signal=-1 before the next BUY — no silent position resets."""
    df = generate_synthetic_market(600)
    strategy = SweepFvgReversalStrategy(params={"max_stop_atr": 0})
    evaluated = strategy.evaluate(df)

    in_pos = False
    for i, row in evaluated.iterrows():
        sig = row["signal"]
        if sig == 1:
            assert not in_pos, f"Received BUY while already in position at {i}"
            in_pos = True
        elif sig == -1:
            assert in_pos, f"Received EXIT while NOT in position at {i}"
            in_pos = False


def test_entry_always_carries_a_real_stop():
    """This strategy has no signal-only entries — every BUY must have a stop
    below the entry close and (when risk is positive) a target above it."""
    df = generate_synthetic_market(600)
    strategy = SweepFvgReversalStrategy(params={"max_stop_atr": 0})
    evaluated = strategy.evaluate(df)

    buys = evaluated[evaluated["signal"] == 1]
    assert (buys["stop_loss"] < buys["close"]).all()
    assert (buys["target"] > buys["close"]).all()


def test_runs_on_backtest_engine():
    df = generate_synthetic_market(600)
    strategy = SweepFvgReversalStrategy(params={"max_stop_atr": 0})
    engine = BacktestEngine(strategy=strategy, initial_capital=100000.0, position_sizing_mode="risk_based")
    summary = engine.run(df, symbol="SYNTH")
    assert summary.total_trades >= 0
