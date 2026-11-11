"""Tests for PairsBacktestEngine's two-leg execution, dollar-neutral sizing,
and cost accounting, using a scripted signal generator (mirrors the
`Scripted` strategy pattern in tests/test_walk_forward.py)."""
import numpy as np
import pandas as pd
import pytest

from backtesting.cost_model import IndianCostModel
from backtesting.pairs_engine import PairsBacktestEngine
from backtesting.slippage import SlippageModel

SLIP = 0.0005


def bars(opens, closes, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(opens), freq="D", name="timestamp")
    return pd.DataFrame({"open": opens, "close": closes}, index=idx)


class ScriptedPairSignal:
    """Returns a fixed open/close/position schedule, bypassing the real
    spread/z-score math so engine mechanics can be tested in isolation."""

    def __init__(self, df_a, df_b, positions, exit_reasons=None):
        self.df_a, self.df_b, self.positions = df_a, df_b, positions
        self.exit_reasons = exit_reasons or [""] * len(positions)

    def generate(self, df_a, df_b):
        out = pd.DataFrame(index=self.df_a.index)
        out["open_a"], out["close_a"] = self.df_a["open"], self.df_a["close"]
        out["open_b"], out["close_b"] = self.df_b["open"], self.df_b["close"]
        out["beta"] = 1.0
        out["spread"] = 0.0
        out["zscore"] = 0.0
        out["position"] = self.positions
        out["exit_reason"] = self.exit_reasons
        return out


def test_entry_and_exit_fill_at_next_bar_open_both_legs():
    df_a = bars(opens=[100, 101, 102, 103, 104], closes=[100, 101, 102, 103, 104])
    df_b = bars(opens=[50, 50.5, 51, 51.5, 52], closes=[50, 50.5, 51, 51.5, 52])
    gen = ScriptedPairSignal(df_a, df_b, positions=[0, 1, 1, 0, 0],
                              exit_reasons=["", "", "", "SIGNAL_EXIT", ""])

    res = PairsBacktestEngine(gen, capital_per_pair=100000.0).run(df_a, df_b, "A", "B")

    assert res.total_trades == 1
    trade = res.trades[0]
    assert trade.direction == 1
    # position[1]=1 is the signal read at bar 1's close -> filled at bar 2's open.
    assert trade.entry_time == df_a.index[2]
    assert trade.entry_price_a == SlippageModel.apply_slippage(102, is_buy=True, slippage_pct=SLIP)
    assert trade.entry_price_b == SlippageModel.apply_slippage(51, is_buy=False, slippage_pct=SLIP)
    # position[3]=0 (SIGNAL_EXIT) read at bar 3's close -> filled at bar 4's open.
    assert trade.exit_time == df_a.index[4]
    assert trade.exit_price_a == SlippageModel.apply_slippage(104, is_buy=False, slippage_pct=SLIP)
    assert trade.exit_price_b == SlippageModel.apply_slippage(52, is_buy=True, slippage_pct=SLIP)
    assert trade.exit_reason == "SIGNAL_EXIT"


def test_legs_are_dollar_neutral_at_entry():
    df_a = bars(opens=[100, 100, 100], closes=[100, 100, 100])
    df_b = bars(opens=[20, 20, 20], closes=[20, 20, 20])
    gen = ScriptedPairSignal(df_a, df_b, positions=[0, 1, 1])

    res = PairsBacktestEngine(gen, capital_per_pair=100000.0, slippage_pct=0.0).run(df_a, df_b, "A", "B")
    trade = res.trades[0] if res.trades else None
    # Not closed yet (still open at end of data -> forced close at last bar).
    assert res.total_trades == 1
    t = res.trades[0]
    assert t.qty_a * 100 == pytest.approx(50000, rel=0.01)
    assert t.qty_b * 20 == pytest.approx(50000, rel=0.01)


def test_short_direction_pnl_sign():
    # direction=-1 (short A / long B): A falls, B rises -> both legs profit.
    df_a = bars(opens=[100, 100, 90, 90], closes=[100, 100, 90, 85])
    df_b = bars(opens=[50, 50, 55, 55], closes=[50, 50, 55, 60])
    gen = ScriptedPairSignal(df_a, df_b, positions=[0, -1, -1, 0])

    res = PairsBacktestEngine(gen, capital_per_pair=100000.0, slippage_pct=0.0).run(df_a, df_b, "A", "B")
    t = res.trades[0]
    assert t.direction == -1
    assert t.gross_pnl > 0


def test_costs_are_mirrored_onto_both_legs_symmetrically():
    df_a = bars(opens=[100, 100, 100, 100], closes=[100, 100, 100, 100])
    df_b = bars(opens=[50, 50, 50, 50], closes=[50, 50, 50, 50])
    gen = ScriptedPairSignal(df_a, df_b, positions=[0, 1, 0, 0], exit_reasons=["", "", "SIGNAL_EXIT", ""])

    res = PairsBacktestEngine(gen, capital_per_pair=100000.0, slippage_pct=0.0).run(df_a, df_b, "A", "B")
    t = res.trades[0]
    expected_cost_a = IndianCostModel.calculate(t.qty_a * t.entry_price_a, t.qty_a * t.exit_price_a).total_friction
    expected_cost_b = IndianCostModel.calculate(t.qty_b * t.entry_price_b, t.qty_b * t.exit_price_b).total_friction
    assert t.friction_costs == pytest.approx(expected_cost_a + expected_cost_b)
    assert t.friction_costs > 0


def test_open_position_forced_closed_at_end_of_data():
    df_a = bars(opens=[100, 100, 100], closes=[100, 100, 110])
    df_b = bars(opens=[50, 50, 50], closes=[50, 50, 50])
    gen = ScriptedPairSignal(df_a, df_b, positions=[0, 1, 1])

    res = PairsBacktestEngine(gen, capital_per_pair=100000.0).run(df_a, df_b, "A", "B")
    assert res.total_trades == 1
    assert res.trades[0].exit_reason == "END_OF_DATA"
    assert res.trades[0].exit_time == df_a.index[-1]


def test_no_trades_when_never_entered():
    df_a = bars(opens=[100, 100, 100], closes=[100, 100, 100])
    df_b = bars(opens=[50, 50, 50], closes=[50, 50, 50])
    gen = ScriptedPairSignal(df_a, df_b, positions=[0, 0, 0])

    res = PairsBacktestEngine(gen, capital_per_pair=100000.0).run(df_a, df_b, "A", "B")
    assert res.total_trades == 0
    assert res.ending_capital == 100000.0
