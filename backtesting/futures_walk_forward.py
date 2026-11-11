"""Walk-forward validation of one strategy across a universe, executed on
NSE single-stock futures instead of cash delivery.

Mirrors `WalkForwardValidator` closely (same train/test split, fold
structure, and gate philosophy, docs/12-futures-trading-plan.md §3.4) so
results are directly comparable to the cash-equity findings in `docs/10` —
these are still long-only, directional, single-stock strategies, same
comparison basis, unlike the market-neutral pairs case (`pairs_walk_forward.py`
drops the Nifty gate for exactly that reason; this validator keeps it).

The one structural difference: `run()` takes both a cash-data dict (for
indicator/signal warm-up, same as `WalkForwardValidator`) and a parallel
overlay-data dict (symbol -> futures-overlay table, docs/12 §4), and drives
`FuturesBacktestEngine` per symbol instead of `BacktestEngine`. A symbol with
no futures history at all (missing from the overlay dict) is simply skipped
from the run, not an error — not every symbol in the cash universe has NFO
single-stock futures listed.

No tax anywhere, same posture as `backtesting.futures_engine` (see its module
docstring) and `backtesting.pairs_walk_forward`.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from strategies.base_strategy import BaseStrategy
from backtesting.engine import summarize_equity
from backtesting.futures_engine import FuturesBacktestEngine, FuturesBacktestSummary, FuturesBacktestTrade
from backtesting.walk_forward import GateResult, buy_and_hold, pool_trades


@dataclass
class FuturesPeriodStats:
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
    exposure_pct: float = 0.0
    equity_curve: pd.Series = field(default=None, repr=False)


@dataclass
class FuturesFoldResult:
    start: pd.Timestamp
    end: pd.Timestamp
    return_pct: float
    sharpe: float
    benchmark_return_pct: Optional[float]
    trades: int


@dataclass
class FuturesUniverseValidationReport:
    in_sample: FuturesPeriodStats
    out_of_sample: FuturesPeriodStats
    benchmark_oos: Optional[FuturesPeriodStats]
    folds: List[FuturesFoldResult]
    gates: List[GateResult]
    per_symbol: Dict[str, Tuple[Optional[FuturesBacktestSummary], FuturesBacktestSummary, float]] = field(default_factory=dict)

    @property
    def is_qualified(self) -> bool:
        return all(g.passed for g in self.gates)


class FuturesWalkForwardValidator:
    """In-sample vs rolling out-of-sample validation of one strategy across
    a universe of NSE single-stock futures. Same gates/thresholds as
    `WalkForwardValidator` (including the Nifty buy-and-hold benchmark)."""

    def __init__(
        self,
        strategy: BaseStrategy,
        allocation_pct: float = 20.0,
        margin_pct: float = 20.0,
        rollover_buffer_days: int = 3,
        train_ratio: float = 0.50,
        fold_months: int = 6,
        capital_per_symbol: float = 100000.0,
        min_oos_trades: int = 100,
        min_profit_factor: float = 1.30,
        min_expectancy_pct: float = 0.20,
        max_drawdown_pct: float = 20.0,
        min_sharpe_retention: float = 0.60,
    ):
        self.strategy = strategy
        self.allocation_pct = allocation_pct
        self.margin_pct = margin_pct
        self.rollover_buffer_days = rollover_buffer_days
        self.train_ratio = train_ratio
        self.fold_months = fold_months
        self.capital_per_symbol = capital_per_symbol
        self.min_oos_trades = min_oos_trades
        self.min_profit_factor = min_profit_factor
        self.min_expectancy_pct = min_expectancy_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.min_sharpe_retention = min_sharpe_retention

    def split_date(self, cash_data: Dict[str, pd.DataFrame]) -> Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
        first = min(df.index[0] for df in cash_data.values())
        last = max(df.index[-1] for df in cash_data.values())
        return first, first + (last - first) * self.train_ratio, last

    def _tradable_symbols(self, cash_data: Dict[str, pd.DataFrame], overlay_data: Dict[str, pd.DataFrame]) -> List[str]:
        # A symbol with no futures history at all is skipped entirely — not
        # every cash-universe symbol has NFO single-stock futures listed.
        return [s for s in cash_data if s in overlay_data and not overlay_data[s].empty]

    def _run_period(
        self, cash_data, overlay_data, start, end,
    ) -> Tuple[FuturesPeriodStats, Dict[str, FuturesBacktestSummary], List[FuturesBacktestTrade], pd.Series]:
        summaries: Dict[str, FuturesBacktestSummary] = {}
        curves = []
        for symbol in self._tradable_symbols(cash_data, overlay_data):
            cash_df = cash_data[symbol]
            if cash_df.loc[start:end].empty:
                continue
            engine = FuturesBacktestEngine(
                strategy=self.strategy,
                initial_capital=self.capital_per_symbol,
                allocation_pct=self.allocation_pct,
                margin_pct=self.margin_pct,
                rollover_buffer_days=self.rollover_buffer_days,
            )
            res = engine.run(cash_df, overlay_data[symbol], symbol=symbol, start=start, end=end)
            summaries[symbol] = res
            curves.append(res.equity_curve)

        dates = sorted(set().union(*[c.index for c in curves])) if curves else []
        aligned = [c.reindex(dates).ffill().fillna(self.capital_per_symbol) for c in curves]
        equity = sum(aligned) if aligned else pd.Series(dtype=float)
        trades = [t for s in summaries.values() for t in s.trades]

        cagr, sharpe, max_dd = summarize_equity(equity)
        pooled = pool_trades(trades)
        capital = self.capital_per_symbol * len(curves)
        stats = FuturesPeriodStats(
            start=equity.index[1] if len(equity) > 1 else start,
            end=equity.index[-1] if len(equity) else end,
            cagr_pct=cagr, sharpe=sharpe, max_drawdown_pct=max_dd,
            net_return_pct=((equity.iloc[-1] / capital - 1) * 100.0) if capital else 0.0,
            exposure_pct=float(np.mean([s.exposure_pct for s in summaries.values()])) if summaries else 0.0,
            equity_curve=equity,
            **pooled,
        )
        return stats, summaries, trades, equity

    def run(
        self, cash_data: Dict[str, pd.DataFrame], overlay_data: Dict[str, pd.DataFrame],
        benchmark: Optional[pd.DataFrame] = None,
    ) -> FuturesUniverseValidationReport:
        first, split, last = self.split_date(cash_data)
        is_end = split - pd.Timedelta(seconds=1)

        is_stats, is_sums, _, _ = self._run_period(cash_data, overlay_data, None, is_end)
        oos_stats, oos_sums, oos_trades, oos_equity = self._run_period(cash_data, overlay_data, split, last)

        portfolio_capital = self.capital_per_symbol * len(oos_sums)
        bench = buy_and_hold(benchmark, split, last, portfolio_capital) if benchmark is not None else None
        bench_stats = None
        if bench is not None:
            bench_stats = FuturesPeriodStats(
                start=bench.start, end=bench.end, cagr_pct=bench.cagr_pct, sharpe=bench.sharpe,
                max_drawdown_pct=bench.max_drawdown_pct, net_return_pct=bench.net_return_pct,
                exposure_pct=bench.exposure_pct, equity_curve=bench.equity_curve,
            )

        folds = []
        fold_start = split
        while fold_start < last:
            fold_end = min(fold_start + pd.DateOffset(months=self.fold_months), last + pd.Timedelta(seconds=1))
            prior = oos_equity.loc[:fold_start - pd.Timedelta(seconds=1)]
            window = oos_equity.loc[fold_start:fold_end - pd.Timedelta(seconds=1)]
            if len(window) and len(prior):
                curve = pd.concat([prior.iloc[-1:], window])
                _, f_sharpe, _ = summarize_equity(curve)
                b = None
                if benchmark is not None:
                    bw = benchmark.loc[fold_start:fold_end - pd.Timedelta(seconds=1), "close"]
                    bp = benchmark.loc[:fold_start - pd.Timedelta(seconds=1), "close"]
                    if len(bw) and len(bp):
                        b = (bw.iloc[-1] / bp.iloc[-1] - 1) * 100.0
                folds.append(FuturesFoldResult(
                    start=window.index[0], end=window.index[-1],
                    return_pct=(curve.iloc[-1] / curve.iloc[0] - 1) * 100.0,
                    sharpe=f_sharpe, benchmark_return_pct=b,
                    trades=sum(1 for t in oos_trades if fold_start <= t.exit_time < fold_end),
                ))
            fold_start = fold_end

        per_symbol = {}
        for symbol, oos in oos_sums.items():
            bh = buy_and_hold(cash_data[symbol], split, last, self.capital_per_symbol)
            per_symbol[symbol] = (is_sums.get(symbol), oos, bh.net_return_pct if bh else 0.0)

        gates = self._gates(is_stats, oos_stats, bench_stats, folds)
        return FuturesUniverseValidationReport(is_stats, oos_stats, bench_stats, folds, gates, per_symbol)

    def _gates(
        self, is_s: FuturesPeriodStats, oos: FuturesPeriodStats, bench: Optional[FuturesPeriodStats],
        folds: List[FuturesFoldResult],
    ) -> List[GateResult]:
        gates = [
            GateResult("Sample size", oos.total_trades >= self.min_oos_trades,
                       f"{oos.total_trades} OOS trades (need {self.min_oos_trades})"),
        ]
        if bench is not None:
            gates.append(GateResult(
                "Beats Nifty buy-and-hold", oos.sharpe >= bench.sharpe,
                f"OOS Sharpe {oos.sharpe:.2f} vs Nifty {bench.sharpe:.2f} (after costs, no tax; "
                f"CAGR {oos.cagr_pct:+.1f}% vs {bench.cagr_pct:+.1f}% at {oos.exposure_pct:.0f}% avg exposure)"))
        else:
            gates.append(GateResult("Beats Nifty buy-and-hold", False, "no benchmark data loaded"))

        retention_ok = is_s.sharpe > 0 and oos.sharpe >= is_s.sharpe * self.min_sharpe_retention
        gates.append(GateResult(
            "Walk-forward retention", retention_ok,
            f"OOS Sharpe {oos.sharpe:.2f} vs in-sample {is_s.sharpe:.2f} (need >= {self.min_sharpe_retention:.0%} of a positive IS)"))
        gates.append(GateResult("Profit factor", oos.profit_factor >= self.min_profit_factor,
                                f"{oos.profit_factor:.2f} after costs, no tax (need {self.min_profit_factor:.2f})"))
        gates.append(GateResult("Expectancy", oos.expectancy_pct >= self.min_expectancy_pct,
                                f"{oos.expectancy_pct:+.2f}% per trade (need {self.min_expectancy_pct:+.2f}%)"))
        gates.append(GateResult("Max drawdown", oos.max_drawdown_pct <= self.max_drawdown_pct,
                                f"{oos.max_drawdown_pct:.1f}% portfolio, mark-to-market (limit {self.max_drawdown_pct:.0f}%)"))
        positive = sum(1 for f in folds if f.sharpe > 0)
        gates.append(GateResult("Consistency across folds", bool(folds) and positive * 2 >= len(folds),
                                f"{positive}/{len(folds)} folds with positive Sharpe"))
        return gates
