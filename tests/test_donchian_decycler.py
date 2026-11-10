"""Tests for DonchianDecyclerStrategy: lookahead safety and signal integrity."""
import numpy as np
import pandas as pd

from strategies.donchian_decycler import DonchianDecyclerStrategy
from backtesting.engine import BacktestEngine


def generate_synthetic_market(n_bars: int = 400) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=n_bars, freq="D")
    np.random.seed(11)
    half = n_bars // 2
    drift = np.concatenate([np.linspace(100, 240, half), np.linspace(240, 150, n_bars - half)])
    closes = drift + np.random.normal(0, 2.0, n_bars)
    highs = closes + np.random.uniform(1.0, 3.0, n_bars)
    lows = closes - np.random.uniform(1.0, 3.0, n_bars)
    opens = (highs + lows) / 2.0
    volumes = np.random.randint(100000, 200000, n_bars)
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
                         index=pd.Index(dates, name="timestamp"))


def test_zero_lookahead_bias():
    df_original = generate_synthetic_market(400)
    strategy = DonchianDecyclerStrategy()
    evaluated_original = strategy.evaluate(df_original.copy())

    target_bar = 350
    signal_at_t = evaluated_original["signal"].iloc[target_bar]
    decycler_at_t = evaluated_original["decycler"].iloc[target_bar]

    df_mutated = df_original.copy()
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("close")] *= 5.0
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("high")] *= 5.0
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("low")] *= 5.0
    evaluated_mutated = strategy.evaluate(df_mutated)

    assert evaluated_mutated["signal"].iloc[target_bar] == signal_at_t
    assert np.isclose(evaluated_mutated["decycler"].iloc[target_bar], decycler_at_t)


def test_signal_state_machine_integrity():
    df = generate_synthetic_market(400)
    strategy = DonchianDecyclerStrategy()
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


def test_runs_on_backtest_engine():
    df = generate_synthetic_market(500)
    strategy = DonchianDecyclerStrategy()
    engine = BacktestEngine(strategy=strategy, initial_capital=100000.0, position_sizing_mode="risk_based")
    summary = engine.run(df, symbol="SYNTH")
    assert summary.total_trades >= 0
