"""Runs universe-level walk-forward validation (WalkForwardValidator) for one
strategy and prints per-symbol detail, the rolling OOS folds, and the
plan.md acceptance gates judged on the pooled portfolio.

Usage:
    python -m backtesting.run_walk_forward --strategy range_filtered_trend
    python -m backtesting.run_walk_forward --strategy trend_breakout --fold-months 3
"""
import argparse
from typing import Dict, List, Optional

import pandas as pd

from configs.logger import logger
from data.loaders.sync_history import load_universe
from data.store.parquet_store import get_parquet_store
from backtesting.engine import STRATEGY_REGISTRY, colorize_signed
from backtesting.walk_forward import WalkForwardValidator, UniverseValidationReport
from strategies.regime_filter import RegimeFilteredStrategy, index_regime

BENCHMARK_SYMBOL = "NIFTYBEES"


def load_data(symbols: Optional[List[str]] = None) -> Dict[str, pd.DataFrame]:
    store = get_parquet_store()
    data = {}
    for symbol in symbols or [a["symbol"] for a in load_universe()]:
        df = store.load_bars(symbol, timeframe="1D")
        if df.empty:
            logger.warning(f"No local Parquet data for {symbol}, skipping. Run 'make sync' first.")
            continue
        data[symbol] = df
    return data


def print_report(report: UniverseValidationReport, strategy_name: str, sizing_mode: str) -> None:
    is_s, oos = report.in_sample, report.out_of_sample
    width = 104
    print("\n" + "=" * width)
    print(f"  ARTHA QUANT — WALK-FORWARD VALIDATION ({strategy_name}, sizing={sizing_mode})")
    print(f"  In-sample {is_s.start.date()} -> {is_s.end.date()}  |  Out-of-sample {oos.start.date()} -> {oos.end.date()}")
    print("=" * width)
    print(f"{'Symbol':<12}{'IS Trd':>7}{'OOS Trd':>8}{'OOS Ret%':>10}{'B&H Ret%':>10}{'OOS PF':>8}{'Exposure%':>11}")
    print("-" * width)
    for symbol, (is_sum, oos_sum, bh_ret) in sorted(report.per_symbol.items(), key=lambda kv: -kv[1][1].net_return_pct):
        is_trades = is_sum.total_trades if is_sum else 0
        print(f"{symbol:<12}{is_trades:>7}{oos_sum.total_trades:>8}"
              f"{colorize_signed(f'{oos_sum.net_return_pct:>10.2f}', oos_sum.net_return_pct)}"
              f"{bh_ret:>10.2f}{oos_sum.profit_factor:>8.2f}{oos_sum.exposure_pct:>11.1f}")
    print("-" * width)
    print("Per-symbol rows are pre-tax and informational only: a few trades per symbol is noise.")
    print("Gates below are judged on the pooled, equal-weight portfolio of all symbols.\n")

    print(f"{'Portfolio':<22}{'Trades':>7}{'Win%':>7}{'PF':>6}{'Exp%':>7}{'CAGR%':>8}{'Sharpe':>8}{'MaxDD%':>8}{'Tax ₹':>12}")
    rows = [("In-sample", is_s), ("Out-of-sample", oos)]
    for label, s in rows:
        print(f"{label:<22}{s.total_trades:>7}{s.win_rate_pct:>7.1f}{s.profit_factor:>6.2f}{s.expectancy_pct:>+7.2f}"
              f"{s.cagr_pct:>+8.2f}{s.sharpe:>8.2f}{s.max_drawdown_pct:>8.1f}{s.tax_paid:>12,.0f}")
    if report.benchmark_oos:
        b = report.benchmark_oos
        print(f"{'Nifty B&H (OOS)':<22}{'':>7}{'':>7}{'':>6}{'':>7}{b.cagr_pct:>+8.2f}{b.sharpe:>8.2f}{b.max_drawdown_pct:>8.1f}{b.tax_paid:>12,.0f}")

    print(f"\nRolling OOS folds:")
    for f in report.folds:
        bench = f"{f.benchmark_return_pct:+.2f}%" if f.benchmark_return_pct is not None else "n/a"
        print(f"  {f.start.date()} -> {f.end.date()}  return {colorize_signed(f'{f.return_pct:+6.2f}%', f.return_pct)}"
              f"  Sharpe {f.sharpe:+5.2f}  trades {f.trades:>3}  Nifty {bench}")

    print("\nAcceptance gates:")
    for g in report.gates:
        mark = colorize_signed("PASS", 1) if g.passed else colorize_signed("FAIL", -1)
        print(f"  {mark}  {g.name:<26} {g.detail}")
    verdict = colorize_signed("QUALIFIED", 1) if report.is_qualified else colorize_signed("NOT QUALIFIED", -1)
    print(f"\n>>> {verdict}")
    print("=" * width + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Artha Quant Walk-Forward Validation Runner")
    parser.add_argument("--strategy", default="range_filtered_trend", choices=list(STRATEGY_REGISTRY.keys()))
    parser.add_argument("--sizing", default=None, choices=["risk_based", "full_equity"], help="Defaults to the strategy's recommended mode")
    parser.add_argument("--symbol", default=None, help="Run a single symbol instead of the full universe")
    parser.add_argument("--train-ratio", type=float, default=0.50)
    parser.add_argument("--fold-months", type=int, default=6)
    parser.add_argument("--capital", type=float, default=100000.0, help="Capital per symbol sleeve")
    parser.add_argument("--regime-filter", action="store_true",
                        help=f"Only allow new entries while {BENCHMARK_SYMBOL} closes above its moving average")
    parser.add_argument("--regime-ma", type=int, default=200)
    parser.add_argument("--in-sample-only", action="store_true",
                        help="Drop everything from the out-of-sample split onward and validate on an inner split, "
                             "so the holdout stays untouched")
    args = parser.parse_args()

    strategy_cls, default_sizing = STRATEGY_REGISTRY[args.strategy]
    sizing = args.sizing or default_sizing

    data = load_data([args.symbol] if args.symbol else None)
    benchmark = get_parquet_store().load_bars(BENCHMARK_SYMBOL, timeframe="1D")
    train_ratio = args.train_ratio

    if args.in_sample_only:
        _, split, _ = WalkForwardValidator(strategy=strategy_cls(), train_ratio=train_ratio).split_date(data)
        cutoff = split - pd.Timedelta(seconds=1)
        data = {s: df.loc[:cutoff] for s, df in data.items() if len(df.loc[:cutoff])}
        benchmark = benchmark.loc[:cutoff]
        train_ratio = 0.5
        print(f"IN-SAMPLE ONLY: data cut at {cutoff.date()}; 'Out-of-sample' below is the inner test half.")

    strategy = strategy_cls()
    label = args.strategy
    if args.regime_filter:
        if benchmark.empty:
            raise SystemExit(f"--regime-filter needs {BENCHMARK_SYMBOL} data. Run 'make sync' first.")
        strategy = RegimeFilteredStrategy(strategy, index_regime(benchmark, args.regime_ma))
        label = f"{args.strategy} + {BENCHMARK_SYMBOL}>{args.regime_ma}d MA filter"

    validator = WalkForwardValidator(
        strategy=strategy,
        position_sizing_mode=sizing,
        train_ratio=train_ratio,
        fold_months=args.fold_months,
        capital_per_symbol=args.capital,
    )
    report = validator.run(data, benchmark=None if benchmark.empty else benchmark)
    print_report(report, label, sizing)

    from backtesting.tune import log_run
    log_run(args.strategy, {
        "kind": "walk_forward", "trials": 1,
        "variant": {"params": strategy.params, "regime_filter": args.regime_filter,
                    "regime_ma": args.regime_ma if args.regime_filter else None, "sizing": sizing},
        "in_sample_only": args.in_sample_only, "qualified": report.is_qualified,
    })
