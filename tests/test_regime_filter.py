"""Tests for the market regime filter wrapper."""
import numpy as np
import pandas as pd

from strategies.regime_filter import RegimeFilteredStrategy, index_regime
from tests.test_walk_forward import Scripted, bars


def test_regime_uses_only_past_data():
    idx = bars(closes=np.linspace(100, 200, 300))
    base = index_regime(idx, ma_period=50)

    mutated = idx.copy()
    mutated.iloc[200:, mutated.columns.get_loc("close")] *= 0.1
    after = index_regime(mutated, ma_period=50)

    pd.testing.assert_series_equal(base.iloc[:200], after.iloc[:200])


def test_regime_is_unknown_during_warmup():
    regime = index_regime(bars(closes=np.linspace(100, 200, 60)), ma_period=50)
    assert regime.iloc[:49].isna().all()
    assert regime.iloc[49:].notna().all()


def test_entries_blocked_below_average_and_allowed_above():
    df = bars(closes=np.full(10, 100.0))
    regime = pd.Series([None, None, False, False, True, True, True, False, False, False], index=df.index, dtype=object)
    inner = Scripted(signals={1: 1, 2: -1, 3: 1, 4: -1, 5: 1, 7: -1, 8: 1})
    out = RegimeFilteredStrategy(inner, regime).evaluate(df)

    assert out["signal"].iloc[1] == 1      # unknown regime: allowed
    assert out["signal"].iloc[3] == 0      # below average: blocked
    assert out["signal"].iloc[5] == 1      # above average: allowed
    assert out["signal"].iloc[7] == -1     # exits are never blocked
    assert out["signal"].iloc[8] == 0
    assert out.loc[out.index[3], "stop_loss"] == 0.0


def test_regime_forward_fills_onto_symbol_dates_missing_from_index():
    df = bars(closes=np.full(6, 100.0))
    regime = pd.Series([True, False], index=[df.index[0], df.index[2]], dtype=object)
    out = RegimeFilteredStrategy(Scripted(signals={1: 1, 4: 1}), regime).evaluate(df)

    assert out["signal"].iloc[1] == 1      # day 1 inherits day 0's True
    assert out["signal"].iloc[4] == 0      # day 4 inherits day 2's False
