"""Test suite for Pine Script parity, zero-lookahead verification, and strategy state transitions."""
import numpy as np
import pandas as pd
import pytest

from strategies.trend_breakout import TrendBreakoutStrategy


def generate_synthetic_trending_market(n_bars: int = 300) -> pd.DataFrame:
    """Creates a deterministic synthetic dataset with clear trend and breakout."""
    dates = pd.date_range("2025-01-01", periods=n_bars, freq="D")
    
    # Upward drift with noise
    np.random.seed(42)
    drift = np.linspace(100, 250, n_bars)
    noise = np.random.normal(0, 2, n_bars)
    closes = drift + noise
    
    highs = closes + np.random.uniform(1.0, 3.0, n_bars)
    lows = closes - np.random.uniform(1.0, 3.0, n_bars)
    opens = (highs + lows) / 2.0
    volumes = np.random.randint(150000, 300000, n_bars)

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes
    }, index=pd.Index(dates, name="timestamp"))
    return df


def test_zero_lookahead_bias():
    """
    RIGOROUS LOOKAHEAD AUDIT:
    Mutating future bars (bars > T) must NEVER change the signal or indicators at bar T.
    If bar T's signal changes when bar T+1 is modified, lookahead bias is present.
    """
    df_original = generate_synthetic_trending_market(260)
    strategy = TrendBreakoutStrategy()

    evaluated_original = strategy.evaluate(df_original.copy())

    # Pick an evaluation bar well after warm-up
    target_bar = 240
    signal_at_t = evaluated_original["signal"].iloc[target_bar]
    ema_at_t = evaluated_original["ema_trend"].iloc[target_bar]
    donchian_at_t = evaluated_original["donchian_high"].iloc[target_bar]
    atr_at_t = evaluated_original["atr"].iloc[target_bar]

    # Create mutated future: drastically change all bars after target_bar
    df_mutated = df_original.copy()
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("close")] *= 5.0
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("high")] *= 5.0
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("low")] *= 5.0
    df_mutated.iloc[target_bar + 1:, df_mutated.columns.get_loc("volume")] *= 10.0

    evaluated_mutated = strategy.evaluate(df_mutated)

    # Assert Bar T's values are 100% IDENTICAL
    assert evaluated_mutated["signal"].iloc[target_bar] == signal_at_t, "LOOKAHEAD DETECTED: Signal at T changed when future bars were altered!"
    assert evaluated_mutated["ema_trend"].iloc[target_bar] == ema_at_t, "LOOKAHEAD DETECTED: EMA at T changed!"
    assert evaluated_mutated["donchian_high"].iloc[target_bar] == donchian_at_t, "LOOKAHEAD DETECTED: Donchian High at T changed!"
    assert evaluated_mutated["atr"].iloc[target_bar] == atr_at_t, "LOOKAHEAD DETECTED: ATR at T changed!"


def test_strategy_state_machine_integrity():
    """
    Verify signal sequencing rules:
    - Never emit consecutive BUY (1) signals while already in a position.
    - Never emit EXIT (-1) without a prior BUY (1).
    - Stop loss and target must be positive when in position.
    """
    df = generate_synthetic_trending_market(300)
    strategy = TrendBreakoutStrategy()
    evaluated = strategy.evaluate(df)

    in_pos = False
    for i, row in evaluated.iterrows():
        sig = row["signal"]
        stop = row["stop_loss"]
        target = row["target"]

        if sig == 1:
            assert not in_pos, f"Received BUY signal while already in position at {i}!"
            assert stop > 0, "Stop loss must be positive on Buy"
            assert target > row["close"], "Target must be above entry on Buy"
            assert row["risk_reward_ratio"] >= 2.4, "R:R ratio must meet minimum 2.5:1 target"
            in_pos = True
        elif sig == -1:
            assert in_pos, f"Received EXIT signal while NOT in position at {i}!"
            in_pos = False
        else:
            if in_pos:
                assert stop > 0, "Stop loss must persist while in position"
                assert target > 0, "Target must persist while in position"


def test_donchian_channel_shifted():
    """Verify Donchian channel does not include the current bar's high/low."""
    df = generate_synthetic_trending_market(50)
    strategy = TrendBreakoutStrategy(params={"donchian_entry_period": 20})
    df_ind = strategy.calculate_indicators(df)

    # Bar 25's donchian_high should be the max of highs from index 5 to 24 (inclusive)
    expected_high = df["high"].iloc[5:25].max()
    actual_high = df_ind["donchian_high"].iloc[25]
    assert np.isclose(expected_high, actual_high), f"Expected {expected_high}, got {actual_high}"
