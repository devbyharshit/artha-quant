"""Walk-forward validation for a list of pair candidates.

Reuses `WalkForwardValidator`'s train/test split, fold structure, and most
gates, but (per docs/09-pairs-trading-plan.md §3.6):
- drops "beats Nifty buy-and-hold" — a market-neutral strategy isn't fairly
  judged against a long-only index;
- adds "OOS Sharpe > 0" in its place — a market-neutral strategy with
  non-positive risk-adjusted return has no case for existing, independent of
  the index;
- lowers the minimum OOS trade count from 100 to 30 pooled trades, since a
  same-sector-only, de-duplicated candidate pool with a 20-day max hold
  produces far fewer round trips than the 53-symbol single-stock universe did
  — keeping the single-stock figure would make the gate measure sample size
  rather than the hypothesis.

No tax is applied anywhere in this validator; see `backtesting.pairs_engine`'s
module docstring for why.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from backtesting.engine import summarize_equity
from backtesting.pairs_engine import PairBacktestSummary, PairsBacktestEngine, PairTrade
from backtesting.pairs_selection import PairCandidate
from backtesting.pairs_signals import PairSignalGenerator, PairSignalParams
from backtesting.walk_forward import GateResult, pool_trades


@dataclass
class PairPeriodStats:
    start: pd.Timestamp
    end: pd.Timestamp
    cagr_pct: float
    sharpe: float
    max_drawdown_pct: float
    net_return_pct: float
    total_trades: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    expectancy_pct: float = 0.0
    equity_curve: pd.Series = field(default=None, repr=False)


@dataclass
class PairFoldResult:
    start: pd.Timestamp
    end: pd.Timestamp
    return_pct: float
    sharpe: float
    trades: int


@dataclass
class PairsValidationReport:
    in_sample: PairPeriodStats
    out_of_sample: PairPeriodStats
    folds: List[PairFoldResult]
    gates: List[GateResult]
    per_pair: Dict[str, Tuple[Optional[PairBacktestSummary], PairBacktestSummary]] = field(default_factory=dict)
    candidates: List[PairCandidate] = field(default_factory=list)

    @property
    def is_qualified(self) -> bool:
        return all(g.passed for g in self.gates)


class PairsWalkForwardValidator:
    """In-sample vs rolling out-of-sample validation of a frozen pair candidate list."""

    def __init__(
        self,
        candidates: List[PairCandidate],
        signal_params: Optional[PairSignalParams] = None,
        capital_per_pair: float = 100000.0,
        train_ratio: float = 0.50,
        fold_months: int = 6,
        min_oos_trades: int = 30,
        min_profit_factor: float = 1.30,
        min_expectancy_pct: float = 0.20,
        max_drawdown_pct: float = 20.0,
        min_sharpe_retention: float = 0.60,
    ):
        self.candidates = candidates
        self.signal_params = signal_params or PairSignalParams()
        self.capital_per_pair = capital_per_pair
        self.train_ratio = train_ratio
        self.fold_months = fold_months
        self.min_oos_trades = min_oos_trades
        self.min_profit_factor = min_profit_factor
        self.min_expectancy_pct = min_expectancy_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.min_sharpe_retention = min_sharpe_retention

    def split_date(self, data: Dict[str, pd.DataFrame]) -> Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
        symbols = {s for c in self.candidates for s in (c.symbol_a, c.symbol_b)}
        relevant = {s: data[s] for s in symbols if s in data}
        first = min(df.index[0] for df in relevant.values())
        last = max(df.index[-1] for df in relevant.values())
        return first, first + (last - first) * self.train_ratio, last

    def _run_period(self, data, start, end) -> Tuple[PairPeriodStats, Dict[str, PairBacktestSummary], List[PairTrade], pd.Series]:
        summaries: Dict[str, PairBacktestSummary] = {}
        curves = []
        for c in self.candidates:
            if c.symbol_a not in data or c.symbol_b not in data:
                continue
            engine = PairsBacktestEngine(
                signal_generator=PairSignalGenerator(self.signal_params),
                capital_per_pair=self.capital_per_pair,
            )
            res = engine.run(data[c.symbol_a], data[c.symbol_b], c.symbol_a, c.symbol_b, start=start, end=end)
            summaries[res.pair] = res
            curves.append(res.equity_curve)

        dates = sorted(set().union(*[c.index for c in curves])) if curves else []
        aligned = [c.reindex(dates).ffill().fillna(self.capital_per_pair) for c in curves]
        equity = sum(aligned) if aligned else pd.Series(dtype=float)
        trades = [t for s in summaries.values() for t in s.trades]

        cagr, sharpe, max_dd = summarize_equity(equity)
        pooled = pool_trades(trades)
        capital = self.capital_per_pair * len(curves)
        stats = PairPeriodStats(
            start=equity.index[1] if len(equity) > 1 else start,
            end=equity.index[-1] if len(equity) else end,
            cagr_pct=cagr, sharpe=sharpe, max_drawdown_pct=max_dd,
            net_return_pct=((equity.iloc[-1] / capital - 1) * 100.0) if capital else 0.0,
            equity_curve=equity,
            **pooled,
        )
        return stats, summaries, trades, equity

    def run(self, data: Dict[str, pd.DataFrame]) -> PairsValidationReport:
        first, split, last = self.split_date(data)
        is_end = split - pd.Timedelta(seconds=1)

        is_stats, is_sums, _, _ = self._run_period(data, None, is_end)
        oos_stats, oos_sums, oos_trades, oos_equity = self._run_period(data, split, last)

        folds = []
        fold_start = split
        while fold_start < last:
            fold_end = min(fold_start + pd.DateOffset(months=self.fold_months), last + pd.Timedelta(seconds=1))
            prior = oos_equity.loc[:fold_start - pd.Timedelta(seconds=1)]
            window = oos_equity.loc[fold_start:fold_end - pd.Timedelta(seconds=1)]
            if len(window) and len(prior):
                curve = pd.concat([prior.iloc[-1:], window])
                _, f_sharpe, _ = summarize_equity(curve)
                folds.append(PairFoldResult(
                    start=window.index[0], end=window.index[-1],
                    return_pct=(curve.iloc[-1] / curve.iloc[0] - 1) * 100.0,
                    sharpe=f_sharpe,
                    trades=sum(1 for t in oos_trades if fold_start <= t.exit_time < fold_end),
                ))
            fold_start = fold_end

        per_pair = {name: (is_sums.get(name), oos) for name, oos in oos_sums.items()}
        gates = self._gates(is_stats, oos_stats, folds)
        return PairsValidationReport(is_stats, oos_stats, folds, gates, per_pair, self.candidates)

    def _gates(self, is_s: PairPeriodStats, oos: PairPeriodStats, folds: List[PairFoldResult]) -> List[GateResult]:
        gates = [
            GateResult("Sample size", oos.total_trades >= self.min_oos_trades,
                       f"{oos.total_trades} OOS trades (need {self.min_oos_trades})"),
            GateResult("OOS Sharpe > 0", oos.sharpe > 0,
                       f"OOS Sharpe {oos.sharpe:.2f} (market-neutral: no index benchmark, just needs to be positive)"),
        ]
        retention_ok = is_s.sharpe > 0 and oos.sharpe >= is_s.sharpe * self.min_sharpe_retention
        gates.append(GateResult(
            "Walk-forward retention", retention_ok,
            f"OOS Sharpe {oos.sharpe:.2f} vs in-sample {is_s.sharpe:.2f} (need >= {self.min_sharpe_retention:.0%} of a positive IS)"))
        gates.append(GateResult("Profit factor", oos.profit_factor >= self.min_profit_factor,
                                f"{oos.profit_factor:.2f} after costs (need {self.min_profit_factor:.2f})"))
        gates.append(GateResult("Expectancy", oos.expectancy_pct >= self.min_expectancy_pct,
                                f"{oos.expectancy_pct:+.2f}% per trade (need {self.min_expectancy_pct:+.2f}%)"))
        gates.append(GateResult("Max drawdown", oos.max_drawdown_pct <= self.max_drawdown_pct,
                                f"{oos.max_drawdown_pct:.1f}% portfolio, mark-to-market (limit {self.max_drawdown_pct:.0f}%)"))
        positive = sum(1 for f in folds if f.sharpe > 0)
        gates.append(GateResult("Consistency across folds", bool(folds) and positive * 2 >= len(folds),
                                f"{positive}/{len(folds)} folds with positive Sharpe"))
        return gates
