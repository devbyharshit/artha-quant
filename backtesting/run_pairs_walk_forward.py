"""Runs pair selection (correlation and/or cointegration, in-sample only) and
walk-forward validates the resulting candidate list(s), reporting each
method's results side by side plus the fixed proxy disclosure
(docs/09-pairs-trading-plan.md §5).

Usage:
    python -m backtesting.run_pairs_walk_forward
    python -m backtesting.run_pairs_walk_forward --method cointegration
    python -m backtesting.run_pairs_walk_forward --method correlation --fold-months 3
"""
import argparse
from typing import Dict, List

import pandas as pd

from backtesting.engine import colorize_signed
from backtesting.market_data import load_backtest_data
from backtesting.pairs_selection import PairCandidate, select_correlation_pairs, select_cointegration_pairs
from backtesting.pairs_walk_forward import PairsValidationReport, PairsWalkForwardValidator
from data.loaders.sync_history import load_universe

DISCLOSURE = (
    "\nProxy disclosure (docs/09-pairs-trading-plan.md §3.4-§3.5) — this is NOT deployment-realistic:\n"
    "  - Short-leg costs are MIRRORED from the long leg's cash-equity delivery cost structure.\n"
    "    Real short exposure would be an F&O position with entirely different costs, margin, and tax.\n"
    "  - Leg sizing is equal-notional (dollar-neutral), not weighted by the signal's hedge ratio.\n"
    "  - No tax is modeled on either leg.\n"
)


def _sector_map() -> Dict[str, str]:
    return {a["symbol"]: a.get("sector") for a in load_universe()}


def _select(method: str, data: Dict[str, pd.DataFrame], sectors: Dict[str, str],
            start, end) -> List[PairCandidate]:
    if method == "correlation":
        return select_correlation_pairs(data, sectors, start=start, end=end)
    if method == "cointegration":
        return select_cointegration_pairs(data, sectors, start=start, end=end)
    raise ValueError(method)


def print_report(report: PairsValidationReport, method: str) -> None:
    is_s, oos = report.in_sample, report.out_of_sample
    width = 100
    print("\n" + "=" * width)
    print(f"  ARTHA QUANT — PAIRS WALK-FORWARD VALIDATION (selection method: {method})")
    if is_s.start and oos.end:
        print(f"  In-sample {is_s.start.date()} -> {is_s.end.date()}  |  Out-of-sample {oos.start.date()} -> {oos.end.date()}")
    print("=" * width)

    print(f"Candidate pairs selected ({len(report.candidates)}, in-sample only, de-duplicated by symbol):")
    for c in report.candidates:
        print(f"  {c.symbol_a:<12}/ {c.symbol_b:<12} [{c.sector:<20}] {c.method} metric = {c.metric:.4f}")
    if not report.candidates:
        print("  (none survived the selection threshold)")

    print(f"\n{'Pair':<26}{'IS Trd':>7}{'OOS Trd':>8}{'OOS Ret%':>10}{'OOS PF':>8}")
    print("-" * width)
    for name, (is_sum, oos_sum) in sorted(report.per_pair.items(), key=lambda kv: -kv[1][1].net_return_pct):
        is_trades = is_sum.total_trades if is_sum else 0
        print(f"{name:<26}{is_trades:>7}{oos_sum.total_trades:>8}"
              f"{colorize_signed(f'{oos_sum.net_return_pct:>10.2f}', oos_sum.net_return_pct)}"
              f"{oos_sum.profit_factor:>8.2f}")

    print("-" * width)
    print(f"{'Portfolio':<22}{'Trades':>7}{'Win%':>7}{'PF':>6}{'Exp%':>7}{'CAGR%':>8}{'Sharpe':>8}{'MaxDD%':>8}")
    for label, s in (("In-sample", is_s), ("Out-of-sample", oos)):
        print(f"{label:<22}{s.total_trades:>7}{s.win_rate_pct:>7.1f}{s.profit_factor:>6.2f}{s.expectancy_pct:>+7.2f}"
              f"{s.cagr_pct:>+8.2f}{s.sharpe:>8.2f}{s.max_drawdown_pct:>8.1f}")

    print(f"\nFolds ({len(report.folds)}):")
    for f in report.folds:
        print(f"  {f.start.date()} -> {f.end.date()}: return {f.return_pct:+.2f}%, "
              f"Sharpe {f.sharpe:.2f}, {f.trades} trades")

    print("\nGates:")
    for g in report.gates:
        status = "PASS" if g.passed else "FAIL"
        print(f"  [{status}] {g.name}: {g.detail}")

    verdict = "QUALIFIED" if report.is_qualified else "NOT QUALIFIED"
    print(colorize_signed(f"\n>>> {method.upper()}: {verdict}", 1.0 if report.is_qualified else -1.0))
    print(DISCLOSURE)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Artha Quant Pairs Walk-Forward Validation")
    parser.add_argument("--method", default="both", choices=["correlation", "cointegration", "both"])
    parser.add_argument("--fold-months", type=int, default=6)
    parser.add_argument("--capital-per-pair", type=float, default=100000.0)
    args = parser.parse_args()

    data, _benchmark = load_backtest_data()
    sectors = _sector_map()
    methods = ["correlation", "cointegration"] if args.method == "both" else [args.method]

    for method in methods:
        validator_probe = PairsWalkForwardValidator([], fold_months=args.fold_months,
                                                      capital_per_pair=args.capital_per_pair)
        # Selection must only see the in-sample window: derive the split from
        # the full data range first, exactly like the single-stock validator.
        first = min(df.index[0] for df in data.values())
        last = max(df.index[-1] for df in data.values())
        split = first + (last - first) * validator_probe.train_ratio
        is_end = split - pd.Timedelta(seconds=1)

        candidates = _select(method, data, sectors, start=None, end=is_end)
        validator = PairsWalkForwardValidator(candidates, fold_months=args.fold_months,
                                               capital_per_pair=args.capital_per_pair)
        report = validator.run(data)
        print_report(report, method)
