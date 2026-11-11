"""Runs universe-level walk-forward validation (FuturesWalkForwardValidator)
for one strategy on NSE single-stock futures and prints the same per-symbol
detail, rolling OOS folds, and gates as `run_walk_forward.py`'s cash-equity
version, for direct comparison.

Usage:
    python -m backtesting.run_futures_walk_forward --strategy range_filtered_trend
    python -m backtesting.run_futures_walk_forward --strategy gaussian_channel --symbols RELIANCE TCS

As of 2026-11-10, Kite's NFO instrument catalog only exposes currently-listed
contracts (docs/12-futures-trading-plan.md §3.1's "known risk" — confirmed,
not hypothetical). This means the overlay table only has real price history
for the last ~2-3 live contracts per symbol — a few weeks, not the years a
walk-forward split needs. This CLI runs correctly end-to-end today, but the
result is not yet a meaningful validation; see docs/12 §5 for what closes
that gap (an NSE historical bhavcopy ingester).
"""
import argparse

import pandas as pd

from backtesting.engine import STRATEGY_REGISTRY, colorize_signed
from backtesting.futures_walk_forward import FuturesUniverseValidationReport, FuturesWalkForwardValidator
from backtesting.market_data import load_backtest_data
from data.loaders.kite_client import get_kite_client
from data.loaders.sync_futures_history import build_futures_overlay


def print_report(report: FuturesUniverseValidationReport, strategy_name: str) -> None:
    is_s, oos = report.in_sample, report.out_of_sample
    width = 104
    print("\n" + "=" * width)
    print(f"  ARTHA QUANT — FUTURES WALK-FORWARD VALIDATION ({strategy_name})")
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
    print("Per-symbol rows are pre-tax and informational only (see docs/12-futures-trading-plan.md §2: no")
    print("tax is modeled anywhere in this validator). Gates below are judged on the pooled portfolio.\n")

    print(f"{'Portfolio':<22}{'Trades':>7}{'Win%':>7}{'PF':>6}{'Exp%':>7}{'CAGR%':>8}{'Sharpe':>8}{'MaxDD%':>8}")
    for label, s in (("In-sample", is_s), ("Out-of-sample", oos)):
        print(f"{label:<22}{s.total_trades:>7}{s.win_rate_pct:>7.1f}{s.profit_factor:>6.2f}{s.expectancy_pct:>+7.2f}"
              f"{s.cagr_pct:>+8.2f}{s.sharpe:>8.2f}{s.max_drawdown_pct:>8.1f}")
    if report.benchmark_oos:
        b = report.benchmark_oos
        print(f"{'Nifty buy-and-hold':<22}{'':>7}{'':>7}{'':>6}{'':>7}{b.cagr_pct:>+8.2f}{b.sharpe:>8.2f}{b.max_drawdown_pct:>8.1f}")

    print(f"\nFolds ({len(report.folds)}):")
    for f in report.folds:
        bench_str = f", benchmark {f.benchmark_return_pct:+.2f}%" if f.benchmark_return_pct is not None else ""
        print(f"  {f.start.date()} -> {f.end.date()}: return {f.return_pct:+.2f}%, "
              f"Sharpe {f.sharpe:.2f}, {f.trades} trades{bench_str}")

    print("\nGates:")
    for g in report.gates:
        status = "PASS" if g.passed else "FAIL"
        print(f"  [{status}] {g.name}: {g.detail}")

    verdict = "QUALIFIED" if report.is_qualified else "NOT QUALIFIED"
    print(colorize_signed(f"\n>>> {strategy_name}: {verdict}", 1.0 if report.is_qualified else -1.0))
    print(
        "\nReminder (docs/12-futures-trading-plan.md §3.1/§5): Kite's NFO catalog only exposes "
        "currently-listed contracts, so the overlay table's real price history is currently only a few\n"
        "weeks deep per symbol, not the years a walk-forward split needs. Treat this run as an end-to-end\n"
        "plumbing check, not a trustworthy verdict, until a deeper historical futures price source is wired in."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Artha Quant Futures Walk-Forward Validation")
    parser.add_argument("--strategy", default="range_filtered_trend", choices=list(STRATEGY_REGISTRY.keys()))
    parser.add_argument("--symbols", nargs="+", default=None, help="Restrict to specific symbols (default: full universe)")
    parser.add_argument("--fold-months", type=int, default=6)
    parser.add_argument("--allocation-pct", type=float, default=20.0)
    parser.add_argument("--margin-pct", type=float, default=20.0)
    args = parser.parse_args()

    strategy_cls, _ = STRATEGY_REGISTRY[args.strategy]
    cash_data, benchmark = load_backtest_data(args.symbols)

    kite = None
    try:
        kite = get_kite_client(verify_remote=True)
    except Exception as exc:
        print(f"[!] Could not reach Kite ({exc}); building overlays from local parquet cache only.")

    overlay_data = {}
    for symbol, df in cash_data.items():
        overlay = build_futures_overlay(symbol, df.index, kite=kite)
        if not overlay.empty:
            overlay_data[symbol] = overlay

    if not overlay_data:
        print("\n[!] No futures overlay data available for any requested symbol. Nothing to validate.")
    else:
        validator = FuturesWalkForwardValidator(
            strategy=strategy_cls(), fold_months=args.fold_months,
            allocation_pct=args.allocation_pct, margin_pct=args.margin_pct,
        )
        report = validator.run(cash_data, overlay_data, benchmark=benchmark)
        print_report(report, args.strategy)
