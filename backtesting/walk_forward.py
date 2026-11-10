"""Walk-forward validation at the universe level.

Each symbol is one equal-capital sleeve of a portfolio. The history is split
by date into an in-sample period and an out-of-sample period; the OOS period
is then read as consecutive fixed-length folds. Strategies here have fixed
parameters (nothing is re-fitted per fold), so one continuous OOS run is the
same as running the folds back to back, without forced exits at fold edges.

Every run sees all earlier history for indicator warm-up. Tax is assessed per
financial year across the whole portfolio, so a loss in one symbol offsets a
gain in another. Gates follow plan.md's acceptance criteria and are judged on
the pooled portfolio, not on single symbols with a handful of trades each.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from strategies.base_strategy import BaseStrategy
from backtesting.engine import BacktestEngine, BacktestSummary, BacktestTrade, summarize_equity
from backtesting.cost_model import IndianCostModel
from backtesting.tax_model import AnnualTaxLedger, financial_year


@dataclass
class PeriodStats:
    start: pd.Timestamp
    end: pd.Timestamp
    cagr_pct: float
    sharpe: float
    max_drawdown_pct: float
    net_return_pct: float
    tax_paid: float = 0.0
    total_trades: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    expectancy_pct: float = 0.0
    exposure_pct: float = 0.0
    equity_curve: pd.Series = field(default=None, repr=False)


@dataclass
class FoldResult:
    start: pd.Timestamp
    end: pd.Timestamp
    return_pct: float
    sharpe: float
    benchmark_return_pct: Optional[float]
    trades: int


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str


@dataclass
class UniverseValidationReport:
    in_sample: PeriodStats
    out_of_sample: PeriodStats
    benchmark_oos: Optional[PeriodStats]
    folds: List[FoldResult]
    gates: List[GateResult]
    per_symbol: Dict[str, Tuple[BacktestSummary, BacktestSummary, float]] = field(default_factory=dict)

    @property
    def is_qualified(self) -> bool:
        return all(g.passed for g in self.gates)


def pool_trades(trades: List[BacktestTrade]) -> dict:
    n = len(trades)
    wins = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    losses = abs(sum(t.net_pnl for t in trades if t.net_pnl <= 0))
    return {
        "total_trades": n,
        "win_rate_pct": (sum(1 for t in trades if t.net_pnl > 0) / n * 100.0) if n else 0.0,
        "profit_factor": (wins / losses) if losses > 0 else (99.0 if wins > 0 else 0.0),
        "expectancy_pct": float(np.mean([t.return_pct for t in trades])) if n else 0.0,
    }


def apply_portfolio_tax(equity: pd.Series, trades: List[BacktestTrade]) -> Tuple[pd.Series, float]:
    """Deducts financial-year capital gains tax from a pre-tax equity curve.
    Each year's tax comes off from the first bar of the next FY (or the last
    bar, for the final year)."""
    ledger = AnnualTaxLedger()
    by_fy: Dict[int, List[BacktestTrade]] = {}
    for t in trades:
        by_fy.setdefault(financial_year(t.exit_time), []).append(t)

    deductions = pd.Series(0.0, index=equity.index)
    total = 0.0
    fys = sorted({financial_year(ts) for ts in equity.index})
    for fy in fys:
        for t in by_fy.get(fy, []):
            ledger.record(t.net_pnl, t.holding_days, t.exit_time)
        tax = ledger.settle()
        if tax <= 0:
            continue
        later = [ts for ts in equity.index if financial_year(ts) > fy]
        deductions.loc[later[0] if later else equity.index[-1]] += tax
        total += tax
    return equity - deductions.cumsum(), total


def buy_and_hold(df: pd.DataFrame, start, end, capital: float) -> Optional[PeriodStats]:
    """One round trip of the benchmark over the window, after costs and tax."""
    window = df.loc[start:end]
    if len(window) < 2:
        return None
    qty = capital / window["open"].iloc[0]
    equity = qty * window["close"]
    equity = pd.concat([pd.Series([capital], index=[window.index[0] - pd.Timedelta(days=1)]), equity])
    cost = IndianCostModel.calculate(capital, float(equity.iloc[-1]), is_delivery=True).total_friction
    ledger = AnnualTaxLedger()
    pnl = float(equity.iloc[-1]) - capital - cost
    ledger.record(pnl, (window.index[-1] - window.index[0]).days, window.index[-1])
    tax = ledger.settle()
    equity.iloc[-1] -= cost + tax
    cagr, sharpe, max_dd = summarize_equity(equity)
    return PeriodStats(window.index[0], window.index[-1], cagr, sharpe, max_dd,
                       (equity.iloc[-1] / capital - 1) * 100.0, tax_paid=tax, exposure_pct=100.0)


class WalkForwardValidator:
    """In-sample vs rolling out-of-sample validation of one strategy across a universe."""

    def __init__(
        self,
        strategy: BaseStrategy,
        position_sizing_mode: str = "risk_based",
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
        self.position_sizing_mode = position_sizing_mode
        self.train_ratio = train_ratio
        self.fold_months = fold_months
        self.capital_per_symbol = capital_per_symbol
        self.min_oos_trades = min_oos_trades
        self.min_profit_factor = min_profit_factor
        self.min_expectancy_pct = min_expectancy_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.min_sharpe_retention = min_sharpe_retention

    def split_date(self, data: Dict[str, pd.DataFrame]) -> Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
        first = min(df.index[0] for df in data.values())
        last = max(df.index[-1] for df in data.values())
        return first, first + (last - first) * self.train_ratio, last

    def _run_period(self, data, start, end) -> Tuple[PeriodStats, Dict[str, BacktestSummary], List[BacktestTrade], pd.Series]:
        engine = BacktestEngine(
            strategy=self.strategy,
            initial_capital=self.capital_per_symbol,
            apply_tax=False,
            position_sizing_mode=self.position_sizing_mode,
        )
        summaries: Dict[str, BacktestSummary] = {}
        curves = []
        for symbol, df in data.items():
            if df.loc[start:end].empty:
                continue
            res = engine.run(df, symbol=symbol, start=start, end=end)
            summaries[symbol] = res
            curves.append(res.equity_curve)

        dates = sorted(set().union(*[c.index for c in curves])) if curves else []
        aligned = [c.reindex(dates).ffill().fillna(self.capital_per_symbol) for c in curves]
        pre_tax = sum(aligned) if aligned else pd.Series(dtype=float)
        trades = [t for s in summaries.values() for t in s.trades]
        equity, tax = apply_portfolio_tax(pre_tax, trades) if len(pre_tax) else (pre_tax, 0.0)

        cagr, sharpe, max_dd = summarize_equity(equity)
        pooled = pool_trades(trades)
        capital = self.capital_per_symbol * len(curves)
        stats = PeriodStats(
            start=equity.index[1] if len(equity) > 1 else start,
            end=equity.index[-1] if len(equity) else end,
            cagr_pct=cagr, sharpe=sharpe, max_drawdown_pct=max_dd,
            net_return_pct=((equity.iloc[-1] / capital - 1) * 100.0) if capital else 0.0,
            tax_paid=tax,
            exposure_pct=float(np.mean([s.exposure_pct for s in summaries.values()])) if summaries else 0.0,
            equity_curve=equity,
            **pooled,
        )
        return stats, summaries, trades, equity

    def run(self, data: Dict[str, pd.DataFrame], benchmark: Optional[pd.DataFrame] = None) -> UniverseValidationReport:
        first, split, last = self.split_date(data)
        is_end = split - pd.Timedelta(seconds=1)

        is_stats, is_sums, _, _ = self._run_period(data, None, is_end)
        oos_stats, oos_sums, oos_trades, oos_equity = self._run_period(data, split, last)

        portfolio_capital = self.capital_per_symbol * len(oos_sums)
        bench = buy_and_hold(benchmark, split, last, portfolio_capital) if benchmark is not None else None

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
                folds.append(FoldResult(
                    start=window.index[0], end=window.index[-1],
                    return_pct=(curve.iloc[-1] / curve.iloc[0] - 1) * 100.0,
                    sharpe=f_sharpe, benchmark_return_pct=b,
                    trades=sum(1 for t in oos_trades if fold_start <= t.exit_time < fold_end),
                ))
            fold_start = fold_end

        per_symbol = {}
        for symbol, oos in oos_sums.items():
            bh = buy_and_hold(data[symbol], split, last, self.capital_per_symbol)
            per_symbol[symbol] = (is_sums.get(symbol), oos, bh.net_return_pct if bh else 0.0)

        gates = self._gates(is_stats, oos_stats, bench, folds)
        return UniverseValidationReport(is_stats, oos_stats, bench, folds, gates, per_symbol)

    def _gates(self, is_s: PeriodStats, oos: PeriodStats, bench: Optional[PeriodStats], folds: List[FoldResult]) -> List[GateResult]:
        gates = [
            GateResult("Sample size", oos.total_trades >= self.min_oos_trades,
                       f"{oos.total_trades} OOS trades (need {self.min_oos_trades})"),
        ]
        if bench is not None:
            gates.append(GateResult(
                "Beats Nifty buy-and-hold", oos.sharpe >= bench.sharpe,
                f"OOS Sharpe {oos.sharpe:.2f} vs Nifty {bench.sharpe:.2f} (after costs and tax; "
                f"CAGR {oos.cagr_pct:+.1f}% vs {bench.cagr_pct:+.1f}% at {oos.exposure_pct:.0f}% avg exposure)"))
        else:
            gates.append(GateResult("Beats Nifty buy-and-hold", False, "no benchmark data loaded"))

        retention_ok = is_s.sharpe > 0 and oos.sharpe >= is_s.sharpe * self.min_sharpe_retention
        gates.append(GateResult(
            "Walk-forward retention", retention_ok,
            f"OOS Sharpe {oos.sharpe:.2f} vs in-sample {is_s.sharpe:.2f} (need >= {self.min_sharpe_retention:.0%} of a positive IS)"))
        gates.append(GateResult("Profit factor", oos.profit_factor >= self.min_profit_factor,
                                f"{oos.profit_factor:.2f} after costs, before tax (need {self.min_profit_factor:.2f})"))
        gates.append(GateResult("Expectancy", oos.expectancy_pct >= self.min_expectancy_pct,
                                f"{oos.expectancy_pct:+.2f}% per trade (need {self.min_expectancy_pct:+.2f}%)"))
        gates.append(GateResult("Max drawdown", oos.max_drawdown_pct <= self.max_drawdown_pct,
                                f"{oos.max_drawdown_pct:.1f}% portfolio, mark-to-market (limit {self.max_drawdown_pct:.0f}%)"))
        positive = sum(1 for f in folds if f.sharpe > 0)
        gates.append(GateResult("Consistency across folds", bool(folds) and positive * 2 >= len(folds),
                                f"{positive}/{len(folds)} folds with positive Sharpe"))
        return gates
