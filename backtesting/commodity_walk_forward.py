"""Walk-forward validation of the MCX long/short time-series momentum strategy
across a commodity universe.

Modeled directly on `FuturesWalkForwardValidator` (same train/test split,
same `_run_period` aggregation of per-symbol equity curves into one
equal-capital portfolio, same fold slicing of the OOS portfolio curve) and
drives `CommodityBacktestEngine` per symbol. Three deliberate differences:

1. **Benchmark is per-symbol, not a shared Nifty frame.** A long/short
   commodity momentum book is not an Indian-equity strategy, so Nifty is the
   wrong comparison. Each commodity is compared against its *own* passive
   buy-and-hold over the OOS window, computed by the reused
   `backtesting.walk_forward.buy_and_hold` on that commodity's own
   `continuous_data[symbol]`. Reported as `benchmarks_oos[symbol]`. Each
   fold's `benchmark_return_pct` is the equal-weighted mean of the traded
   symbols' own continuous-price returns over that fold (the gross return of
   an equal-capital passive basket of the same commodities).

2. **Gate set.** Dropped: `"Sample size"` (a trade-count floor -- a
   slow 12-month momentum signal on a handful of commodities trades rarely
   by design, so a trade count says little about statistical sufficiency)
   and `"Beats Nifty buy-and-hold"`. Added:
   - `"Sufficient OOS history"`: OOS calendar span >= `min_oos_years` years.
   - `"Beats own buy-and-hold"`: one gate, evaluated per symbol -- every
     traded symbol's OOS Sharpe must be >= its own buy-and-hold OOS Sharpe;
     the detail lists each symbol's comparison.
   - `"Calmar ratio"`: OOS CAGR / OOS max drawdown >= `min_calmar`
     (default `DEFAULT_MIN_CALMAR` = 0.5: at the 20% drawdown limit that
     demands >= 10% CAGR, i.e. the book must earn back at least half of its
     worst peak-to-trough loss per year -- the threshold the plan brief
     suggests, and a conventional floor for CTA-style trend books).
   Kept unchanged in spirit: `"Walk-forward retention"` and `"Max drawdown"`.
   Profit factor and expectancy are still computed into `CommodityPeriodStats`
   and reported in the `"Sufficient OOS history"` gate's detail text, but do
   **not** gate `is_qualified` (a trend-following book is expected to win
   rarely and big; pooled per-trade stats are informational). The futures
   validator's `"Consistency across folds"` gate is not carried over either
   (the brief's enumerated gate set omits it); folds are still reported.

3. **`calmar` on `CommodityPeriodStats`.** `cagr_pct / max_drawdown_pct`
   when the drawdown is positive. With zero drawdown the ratio is undefined;
   `calmar_ratio` returns 99.0 if CAGR is positive (same "unbounded but
   finite" sentinel `pool_trades` uses for a loss-free profit factor) and
   0.0 otherwise, so a never-down positive curve passes the Calmar gate and
   a flat/negative one fails it.

**Known benchmark caveat.** `buy_and_hold` is reused as-is, so the
benchmark's round-trip friction and tax come from the *equity delivery* cost
model and capital-gains ledger, not commodity futures costs, and it buys the
back-adjusted continuous series rather than rolling real contracts. Over a
5+ year single round trip that friction is small relative to the price move,
but the benchmark is an approximation, not a modelled commodity position.

No tax on the strategy side, same posture as `backtesting.commodity_engine`.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

from backtesting.commodity_engine import (
    CommodityBacktestEngine,
    CommodityBacktestSummary,
    CommodityBacktestTrade,
)
from backtesting.engine import summarize_equity
from backtesting.futures_walk_forward import FuturesFoldResult
from backtesting.walk_forward import GateResult, buy_and_hold, pool_trades

DEFAULT_MIN_CALMAR = 0.5
# Calmar for a curve that never drew down but grew (see module docstring).
CALMAR_NO_DRAWDOWN = 99.0


def calmar_ratio(cagr_pct: float, max_drawdown_pct: float) -> float:
    if max_drawdown_pct > 0:
        return cagr_pct / max_drawdown_pct
    return CALMAR_NO_DRAWDOWN if cagr_pct > 0 else 0.0


@dataclass
class CommodityPeriodStats:
    start: pd.Timestamp
    end: pd.Timestamp
    cagr_pct: float
    sharpe: float
    calmar: float
    max_drawdown_pct: float
    net_return_pct: float
    total_trades: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    expectancy_pct: float = 0.0
    equity_curve: pd.Series = field(default=None, repr=False)


@dataclass
class CommodityUniverseValidationReport:
    in_sample: CommodityPeriodStats
    out_of_sample: CommodityPeriodStats
    benchmarks_oos: Dict[str, CommodityPeriodStats]  # per-commodity own buy-and-hold, spec §8
    folds: List[FuturesFoldResult]  # reused dataclass shape, no changes needed
    gates: List[GateResult]
    per_symbol: Dict[str, Tuple[Optional[CommodityBacktestSummary], CommodityBacktestSummary]] = field(default_factory=dict)

    @property
    def is_qualified(self) -> bool:
        return all(g.passed for g in self.gates)


class CommodityWalkForwardValidator:
    """In-sample vs rolling out-of-sample validation of the commodity
    momentum strategy across a universe of MCX commodities, each benchmarked
    against its own buy-and-hold."""

    def __init__(
        self, vol_target_pct: float = 35.0, margin_pct: float = 25.0,
        rollover_buffer_days: int = 3, train_ratio: float = 0.50, fold_months: int = 6,
        capital_per_symbol: float = 100000.0, min_oos_years: float = 5.0,
        min_sharpe_retention: float = 0.60, max_drawdown_pct: float = 20.0,
        min_calmar: float = DEFAULT_MIN_CALMAR,
    ):
        self.vol_target_pct = vol_target_pct
        self.margin_pct = margin_pct
        self.rollover_buffer_days = rollover_buffer_days
        self.train_ratio = train_ratio
        self.fold_months = fold_months
        self.capital_per_symbol = capital_per_symbol
        self.min_oos_years = min_oos_years
        self.min_sharpe_retention = min_sharpe_retention
        self.max_drawdown_pct = max_drawdown_pct
        self.min_calmar = min_calmar

    def split_date(self, continuous_data: Dict[str, pd.DataFrame]) -> Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
        first = min(df.index[0] for df in continuous_data.values())
        last = max(df.index[-1] for df in continuous_data.values())
        return first, first + (last - first) * self.train_ratio, last

    def _tradable_symbols(self, continuous_data, overlay_data) -> List[str]:
        # A commodity with no futures overlay at all is skipped, not an error.
        return [s for s in continuous_data if s in overlay_data and not overlay_data[s].empty]

    def _run_period(
        self, continuous_data, overlay_data, start, end,
    ) -> Tuple[CommodityPeriodStats, Dict[str, CommodityBacktestSummary], List[CommodityBacktestTrade], pd.Series]:
        summaries: Dict[str, CommodityBacktestSummary] = {}
        curves = []
        for symbol in self._tradable_symbols(continuous_data, overlay_data):
            cont_df = continuous_data[symbol]
            if cont_df.loc[start:end].empty:
                continue
            engine = CommodityBacktestEngine(
                vol_target_pct=self.vol_target_pct,
                margin_pct=self.margin_pct,
                rollover_buffer_days=self.rollover_buffer_days,
                initial_capital=self.capital_per_symbol,
            )
            res = engine.run(cont_df, overlay_data[symbol], symbol=symbol, start=start, end=end)
            summaries[symbol] = res
            curves.append(res.equity_curve)

        dates = sorted(set().union(*[c.index for c in curves])) if curves else []
        aligned = [c.reindex(dates).ffill().fillna(self.capital_per_symbol) for c in curves]
        equity = sum(aligned) if aligned else pd.Series(dtype=float)
        trades = [t for s in summaries.values() for t in s.trades]

        cagr, sharpe, max_dd = summarize_equity(equity)
        pooled = pool_trades(trades)
        capital = self.capital_per_symbol * len(curves)
        stats = CommodityPeriodStats(
            start=equity.index[1] if len(equity) > 1 else start,
            end=equity.index[-1] if len(equity) else end,
            cagr_pct=cagr, sharpe=sharpe, calmar=calmar_ratio(cagr, max_dd), max_drawdown_pct=max_dd,
            net_return_pct=((equity.iloc[-1] / capital - 1) * 100.0) if capital else 0.0,
            equity_curve=equity,
            **pooled,
        )
        return stats, summaries, trades, equity

    def run(
        self, continuous_data: Dict[str, pd.DataFrame], overlay_data: Dict[str, pd.DataFrame],
    ) -> CommodityUniverseValidationReport:
        first, split, last = self.split_date(continuous_data)
        is_end = split - pd.Timedelta(seconds=1)

        is_stats, is_sums, _, _ = self._run_period(continuous_data, overlay_data, None, is_end)
        oos_stats, oos_sums, oos_trades, oos_equity = self._run_period(continuous_data, overlay_data, split, last)

        # Each commodity's own passive buy-and-hold over the OOS window.
        benchmarks: Dict[str, CommodityPeriodStats] = {}
        for symbol, df in continuous_data.items():
            bh = buy_and_hold(df, split, last, self.capital_per_symbol)
            if bh is None:
                continue
            benchmarks[symbol] = CommodityPeriodStats(
                start=bh.start, end=bh.end, cagr_pct=bh.cagr_pct, sharpe=bh.sharpe,
                calmar=calmar_ratio(bh.cagr_pct, bh.max_drawdown_pct),
                max_drawdown_pct=bh.max_drawdown_pct, net_return_pct=bh.net_return_pct,
                equity_curve=bh.equity_curve,
            )

        folds = []
        fold_start = split
        while fold_start < last:
            fold_end = min(fold_start + pd.DateOffset(months=self.fold_months), last + pd.Timedelta(seconds=1))
            prior = oos_equity.loc[:fold_start - pd.Timedelta(seconds=1)]
            window = oos_equity.loc[fold_start:fold_end - pd.Timedelta(seconds=1)]
            # Base point for the fold's return: the last OOS mark before the
            # fold. For the first fold that can be empty when the engine's
            # seed point (initial capital, stamped one day before the first
            # OOS bar) lands exactly on `split` -- then the seed is already
            # window[0] and serves as the base. (FuturesWalkForwardValidator
            # silently drops the first fold in that case.)
            curve = pd.concat([prior.iloc[-1:], window]) if len(prior) else window
            if len(curve) >= 2:
                _, f_sharpe, _ = summarize_equity(curve)
                sym_rets = []
                for symbol in oos_sums:
                    close = continuous_data[symbol]["close"]
                    bw = close.loc[fold_start:fold_end - pd.Timedelta(seconds=1)]
                    bp = close.loc[:fold_start - pd.Timedelta(seconds=1)]
                    if len(bw) and len(bp):
                        sym_rets.append((bw.iloc[-1] / bp.iloc[-1] - 1) * 100.0)
                folds.append(FuturesFoldResult(
                    start=curve.index[1], end=curve.index[-1],
                    return_pct=(curve.iloc[-1] / curve.iloc[0] - 1) * 100.0,
                    sharpe=f_sharpe,
                    benchmark_return_pct=float(sum(sym_rets) / len(sym_rets)) if sym_rets else None,
                    trades=sum(1 for t in oos_trades if fold_start <= t.exit_time < fold_end),
                ))
            fold_start = fold_end

        per_symbol = {symbol: (is_sums.get(symbol), oos) for symbol, oos in oos_sums.items()}
        symbol_sharpes = {symbol: oos.sharpe for symbol, oos in oos_sums.items()}

        gates = self._gates(is_stats, oos_stats, benchmarks, symbol_sharpes)
        return CommodityUniverseValidationReport(is_stats, oos_stats, benchmarks, folds, gates, per_symbol)

    def _gates(
        self, is_s: CommodityPeriodStats, oos: CommodityPeriodStats,
        benchmarks: Dict[str, CommodityPeriodStats], symbol_sharpes: Dict[str, float],
    ) -> List[GateResult]:
        """`symbol_sharpes` = OOS strategy Sharpe per traded symbol; each is
        compared with `benchmarks[symbol].sharpe`."""
        years = (oos.end - oos.start).days / 365.25
        gates = [GateResult(
            "Sufficient OOS history", (oos.end - oos.start).days >= self.min_oos_years * 365.25,
            f"{years:.1f} years OOS (need {self.min_oos_years:.1f}); informational, not gated: "
            f"{oos.total_trades} trades, profit factor {oos.profit_factor:.2f}, "
            f"expectancy {oos.expectancy_pct:+.2f}% per trade, win rate {oos.win_rate_pct:.0f}% "
            f"(after costs, no tax)")]

        parts, all_beat = [], bool(symbol_sharpes)
        for symbol, s_sharpe in symbol_sharpes.items():
            bench = benchmarks.get(symbol)
            if bench is None:
                all_beat = False
                parts.append(f"{symbol} {s_sharpe:.2f} vs no buy-and-hold data")
                continue
            beat = s_sharpe >= bench.sharpe
            all_beat = all_beat and beat
            parts.append(f"{symbol} {s_sharpe:.2f} vs {bench.sharpe:.2f} {'ok' if beat else 'FAIL'}")
        gates.append(GateResult(
            "Beats own buy-and-hold", all_beat,
            "OOS Sharpe vs own buy-and-hold Sharpe, every symbol must beat its own: "
            + ("; ".join(parts) if parts else "no symbols traded")))

        retention_ok = is_s.sharpe > 0 and oos.sharpe >= is_s.sharpe * self.min_sharpe_retention
        gates.append(GateResult(
            "Walk-forward retention", retention_ok,
            f"OOS Sharpe {oos.sharpe:.2f} vs in-sample {is_s.sharpe:.2f} (need >= {self.min_sharpe_retention:.0%} of a positive IS)"))
        gates.append(GateResult(
            "Calmar ratio", oos.calmar >= self.min_calmar,
            f"{oos.calmar:.2f} (CAGR {oos.cagr_pct:+.1f}% / max DD {oos.max_drawdown_pct:.1f}%; need {self.min_calmar:.2f})"))
        gates.append(GateResult(
            "Max drawdown", oos.max_drawdown_pct <= self.max_drawdown_pct,
            f"{oos.max_drawdown_pct:.1f}% portfolio, mark-to-market (limit {self.max_drawdown_pct:.0f}%)"))
        return gates
