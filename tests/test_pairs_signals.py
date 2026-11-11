"""Tests for pairs-trading spread/hedge-ratio/z-score math and the
entry/exit state machine in backtesting.pairs_signals."""
import numpy as np
import pandas as pd

from backtesting.pairs_signals import (
    PairSignalGenerator,
    PairSignalParams,
    compute_spread,
    compute_zscore,
    generate_positions,
)


def series(values, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(values), freq="D")
    return pd.Series(values, index=idx, dtype=float)


def test_hedge_ratio_and_spread_have_no_lookahead():
    rng = np.random.default_rng(0)
    log_a = series(np.cumsum(rng.normal(0, 0.01, 100)) + 5)
    log_b_base = np.cumsum(rng.normal(0, 0.01, 100)) + 5

    log_b_1 = series(log_b_base.copy())
    log_b_2 = series(log_b_base.copy())
    log_b_2.iloc[80:] += 10.0  # change only the future relative to bar 70

    beta_1, spread_1 = compute_spread(log_a, log_b_1, window=20)
    beta_2, spread_2 = compute_spread(log_a, log_b_2, window=20)

    assert beta_1.iloc[70] == beta_2.iloc[70]
    assert spread_1.iloc[70] == spread_2.iloc[70]


def test_zscore_is_nan_during_warmup_then_finite():
    spread = series(np.sin(np.linspace(0, 10, 100)))
    z = compute_zscore(spread, window=20)
    assert z.iloc[:19].isna().all()
    assert np.isfinite(z.iloc[25])


def test_generate_positions_enters_short_above_threshold_and_exits_at_zero_cross():
    # z climbs past +2 (short A / long B), then reverts through 0.
    z = series([0.0, 1.0, 2.5, 2.0, 0.5, -0.5])
    pos, reason = generate_positions(z, PairSignalParams())
    assert list(pos.values) == [0, 0, -1, -1, -1, 0]
    assert reason.iloc[5] == "SIGNAL_EXIT"


def test_generate_positions_enters_long_below_negative_threshold():
    z = series([0.0, -1.0, -2.1, -1.0, 0.1])
    pos, _ = generate_positions(z, PairSignalParams())
    assert list(pos.values) == [0, 0, 1, 1, 0]


def test_generate_positions_stop_exit_when_spread_keeps_diverging():
    z = series([0.0, 2.1, 2.8, 3.2, 3.6])
    pos, reason = generate_positions(z, PairSignalParams(stop_z=3.5))
    assert list(pos.values) == [0, -1, -1, -1, 0]
    assert reason.iloc[4] == "STOP"


def test_generate_positions_max_holding_days_exit():
    z = [2.5] + [1.5] * 10  # stays entered (never crosses 0 or the stop)
    pos, reason = generate_positions(series(z), PairSignalParams(max_holding_days=3, stop_z=99))
    pos = list(pos.values)
    assert pos[0] == -1
    assert pos[3] == 0  # held for 3 bars (indices 1,2,3) then forced flat
    assert reason.iloc[3] == "MAX_HOLD"


def test_generate_positions_holds_through_nan_without_reentering():
    z = series([np.nan, np.nan, 2.5, 1.0, np.nan, 0.5, -0.1])
    pos, _ = generate_positions(z, PairSignalParams())
    assert list(pos.values) == [0, 0, -1, -1, -1, -1, 0]


def test_pair_signal_generator_aligns_two_symbols_and_warms_up():
    idx = pd.date_range("2024-01-01", periods=120, freq="D")
    rng = np.random.default_rng(1)
    close_a = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, 120)))
    close_b = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, 120)))
    df_a = pd.DataFrame({"open": close_a, "close": close_a}, index=idx)
    df_b = pd.DataFrame({"open": close_b, "close": close_b}, index=idx)

    out = PairSignalGenerator().generate(df_a, df_b)

    assert len(out) == 120
    assert {"open_a", "close_a", "open_b", "close_b", "beta", "spread", "zscore", "position", "exit_reason"} <= set(out.columns)
    assert out["zscore"].iloc[:59].isna().all()
    assert np.isfinite(out["zscore"].iloc[-1])
