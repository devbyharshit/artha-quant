"""Two-leg pairs backtesting engine: one combined capital account per pair,
dollar-neutral equal-notional legs, signals read at close and filled at the
next bar's open (same convention as `backtesting.engine.BacktestEngine`).

Deliberate research-phase simplifications (see docs/09-pairs-trading-plan.md
§3.4-§3.5), named here so results can never be misread as deployment-realistic:
- Both legs are costed through `IndianCostModel` exactly as a normal cash-equity
  delivery round trip, including the short leg — mirroring the long leg's cost
  structure onto a leg that, in reality, would be an F&O short with different
  costs entirely (real delivery shorting isn't available in Indian cash equity).
- Leg sizing is equal-notional (dollar-neutral), not weighted by the signal's
  hedge ratio.
- No tax is applied. `IndianTaxEngine`'s capital-gains model has no defined
  meaning for a leg that isn't actually executable as cash-equity delivery.
"""
from dataclasses import dataclass, field
from typing import Any, List, Optional

import numpy as np
import pandas as pd

from backtesting.cost_model import IndianCostModel
from backtesting.engine import summarize_equity
from backtesting.pairs_signals import PairSignalGenerator
from backtesting.slippage import SlippageModel


@dataclass
class PairTrade:
    """Record of one closed pair position (both legs)."""
    pair: str
    direction: int  # 1 = long A / short B, -1 = short A / long B
    entry_time: Any
    exit_time: Any
    holding_days: int
    entry_price_a: float
    entry_price_b: float
    exit_price_a: float
    exit_price_b: float
    qty_a: int
    qty_b: int
    gross_pnl: float
    friction_costs: float
    net_pnl: float
    return_pct: float
    exit_reason: str


@dataclass
class PairBacktestSummary:
    """Consolidated performance for one pair, net of costs (pre-tax — see module docstring)."""
    pair: str
    initial_capital: float
    ending_capital: float
    net_return_pct: float
    cagr_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    profit_factor: float
    expectancy_pct: float
    max_drawdown_pct: float
    total_frictions_paid: float
    sharpe: float = 0.0
    trades: List[PairTrade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


def _entry_fills(open_a: float, open_b: float, direction: int, slippage_pct: float):
    """direction=1: buy A (long), sell B (short-to-open). direction=-1: reverse."""
    if direction == 1:
        fa = SlippageModel.apply_slippage(open_a, is_buy=True, slippage_pct=slippage_pct)
        fb = SlippageModel.apply_slippage(open_b, is_buy=False, slippage_pct=slippage_pct)
    else:
        fa = SlippageModel.apply_slippage(open_a, is_buy=False, slippage_pct=slippage_pct)
        fb = SlippageModel.apply_slippage(open_b, is_buy=True, slippage_pct=slippage_pct)
    return fa, fb


def _exit_fills(open_a: float, open_b: float, direction: int, slippage_pct: float):
    """Reverse of _entry_fills: sell what was bought, buy back what was shorted."""
    if direction == 1:
        xa = SlippageModel.apply_slippage(open_a, is_buy=False, slippage_pct=slippage_pct)
        xb = SlippageModel.apply_slippage(open_b, is_buy=True, slippage_pct=slippage_pct)
    else:
        xa = SlippageModel.apply_slippage(open_a, is_buy=True, slippage_pct=slippage_pct)
        xb = SlippageModel.apply_slippage(open_b, is_buy=False, slippage_pct=slippage_pct)
    return xa, xb


class PairsBacktestEngine:
    """Event-driven bar-by-bar backtest of one pair's long/short spread."""

    def __init__(
        self,
        signal_generator: PairSignalGenerator,
        capital_per_pair: float = 100000.0,
        slippage_pct: float = 0.0005,
    ):
        self.signal_generator = signal_generator
        self.capital_per_pair = capital_per_pair
        self.slippage_pct = slippage_pct

    def run(
        self, df_a: pd.DataFrame, df_b: pd.DataFrame,
        symbol_a: str, symbol_b: str, start=None, end=None,
    ) -> PairBacktestSummary:
        src_a = df_a.loc[:end] if end is not None else df_a
        src_b = df_b.loc[:end] if end is not None else df_b
        sig = self.signal_generator.generate(src_a, src_b)

        n = len(sig)
        timestamps = sig.index
        warm = sig["zscore"].first_valid_index()
        sim_start = 0 if warm is None else sig.index.get_loc(warm)
        if start is not None:
            sim_start = max(sim_start, int(timestamps.searchsorted(start)))

        desired = sig["position"].to_numpy()
        reasons = sig["exit_reason"].to_numpy()
        open_a, open_b = sig["open_a"].to_numpy(), sig["open_b"].to_numpy()
        close_a, close_b = sig["close_a"].to_numpy(), sig["close_b"].to_numpy()

        pair_name = f"{symbol_a}/{symbol_b}"
        capital = self.capital_per_pair
        trades: List[PairTrade] = []
        equity_points = []
        total_friction = 0.0

        current_dir = 0
        entry_time = None
        entry_price_a = entry_price_b = 0.0
        qty_a = qty_b = 0
        pending_dir: Optional[int] = None
        pending_reason = ""

        def open_position(i, direction):
            nonlocal entry_time, entry_price_a, entry_price_b, qty_a, qty_b, current_dir
            fa, fb = _entry_fills(open_a[i], open_b[i], direction, self.slippage_pct)
            leg_notional = capital / 2.0
            qty_a = int(leg_notional / fa) if fa > 0 else 0
            qty_b = int(leg_notional / fb) if fb > 0 else 0
            if qty_a <= 0 or qty_b <= 0:
                current_dir = 0
                return
            entry_time, entry_price_a, entry_price_b = timestamps[i], fa, fb
            current_dir = direction

        def close_position(i, raw_a, raw_b, reason):
            nonlocal capital, current_dir, total_friction
            xa, xb = _exit_fills(raw_a, raw_b, current_dir, self.slippage_pct)
            cost_a = IndianCostModel.calculate(qty_a * entry_price_a, qty_a * xa, is_delivery=True).total_friction
            cost_b = IndianCostModel.calculate(qty_b * entry_price_b, qty_b * xb, is_delivery=True).total_friction
            friction = cost_a + cost_b
            total_friction += friction
            gross_pnl = current_dir * (qty_a * (xa - entry_price_a) - qty_b * (xb - entry_price_b))
            net_pnl = gross_pnl - friction
            capital += net_pnl
            entry_notional = qty_a * entry_price_a + qty_b * entry_price_b
            trades.append(PairTrade(
                pair=pair_name, direction=current_dir, entry_time=entry_time, exit_time=timestamps[i],
                holding_days=(timestamps[i] - entry_time).days,
                entry_price_a=entry_price_a, entry_price_b=entry_price_b,
                exit_price_a=xa, exit_price_b=xb, qty_a=qty_a, qty_b=qty_b,
                gross_pnl=gross_pnl, friction_costs=friction, net_pnl=net_pnl,
                return_pct=(net_pnl / entry_notional * 100.0) if entry_notional > 0 else 0.0,
                exit_reason=reason,
            ))
            current_dir = 0

        for i in range(sim_start, n):
            if pending_dir is not None:
                if current_dir != 0:
                    close_position(i, open_a[i], open_b[i], pending_reason or "SIGNAL_EXIT")
                if pending_dir != 0:
                    open_position(i, pending_dir)
                pending_dir, pending_reason = None, ""

            if i == n - 1:
                if current_dir != 0:
                    close_position(i, close_a[i], close_b[i], "END_OF_DATA")
            else:
                if desired[i] != current_dir:
                    pending_dir = desired[i]
                    pending_reason = str(reasons[i])

            equity = capital
            if current_dir != 0:
                equity += current_dir * (
                    qty_a * (close_a[i] - entry_price_a) - qty_b * (close_b[i] - entry_price_b)
                )
            equity_points.append(equity)

        sim_index = timestamps[sim_start:]
        equity_curve = pd.Series(equity_points, index=sim_index, dtype=float)
        if len(equity_curve):
            equity_curve = pd.concat([
                pd.Series([self.capital_per_pair], index=[sim_index[0] - pd.Timedelta(days=1)]),
                equity_curve,
            ])
        cagr, sharpe, max_dd = summarize_equity(equity_curve)

        total_trades = len(trades)
        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl <= 0]
        gross_profit = sum(t.net_pnl for t in wins)
        gross_loss = abs(sum(t.net_pnl for t in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

        return PairBacktestSummary(
            pair=pair_name,
            initial_capital=self.capital_per_pair,
            ending_capital=capital,
            net_return_pct=((capital - self.capital_per_pair) / self.capital_per_pair) * 100.0,
            cagr_pct=cagr,
            total_trades=total_trades,
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate_pct=(len(wins) / total_trades * 100.0) if total_trades else 0.0,
            profit_factor=profit_factor,
            expectancy_pct=float(np.mean([t.return_pct for t in trades])) if trades else 0.0,
            max_drawdown_pct=max_dd,
            total_frictions_paid=total_friction,
            sharpe=sharpe,
            trades=trades,
            equity_curve=equity_curve,
        )
