"""Parameter tuning that never looks at the out-of-sample period.

The in-sample period (before the walk-forward split date) gets its own inner
walk-forward: each grid combination is scored on the inner train and inner
test halves, and the score is the worse of the two Sharpes, so a setting has
to hold up in both. The pick is the combination whose grid neighbourhood
scores best (a plateau, not a lone spike). The best inner-test Sharpe is then
checked with a deflated Sharpe ratio that accounts for every variant ever
tried for this strategy (logged in logs/tuning_trials_<strategy>.jsonl).

Only `--final` touches the real out-of-sample period, once, with the chosen
parameters. Re-tuning after looking at that result contaminates the holdout.

Usage:
    python -m backtesting.tune --strategy range_filtered_trend
    python -m backtesting.tune --strategy range_filtered_trend --final kalman_period=120 deviation=1.8
"""
import argparse
import itertools
import json
import math
from datetime import datetime
from pathlib import Path
from statistics import NormalDist
from typing import Dict, List

import numpy as np
import pandas as pd

from backtesting.engine import STRATEGY_REGISTRY
from backtesting.walk_forward import WalkForwardValidator
from backtesting.run_walk_forward import print_report
from backtesting.market_data import load_backtest_data

GRIDS: Dict[str, Dict[str, list]] = {
    # kalman_alpha/beta/period only act through one smoothing ratio, so
    # kalman_period alone spans the filter's smoothness.
    "range_filtered_trend": {
        "kalman_period": [40, 77, 120, 180],
        "deviation": [0.6, 1.2, 1.8, 2.4],
        "supertrend_factor": [0.7, 1.5, 2.5, 3.5],
    },
    # start_len is held fixed at the default (31); end_len alone spans the
    # displaced Donchian window size (end_len - start_len).
    "donchian_decycler": {
        "end_len": [33, 36, 41, 51],
        "decycler_period": [30, 60, 90, 120],
        "atr_stop_multiplier": [1.0, 2.0, 3.0, 4.0],
    },
    "ut_bot": {
        "key_value": [1.5, 2.0, 3.0, 4.0],
        "atr_period": [7, 10, 14, 21],
        "atr_stop_multiplier": [1.0, 2.0, 3.0, 4.0],
    },
    "triple_ema_rsi": {
        "slow_ema_period": [50, 100, 150, 200],
        "exit_long_rsi": [60, 70, 80, 90],
        "stop_loss_pct": [0.5, 1.0, 2.0, 3.0],
    },
    # max_stop_atr=0 disables the "stop too wide" skip entirely; this setup
    # is rare by construction (liquidity sweep + FVG + structure confirm all
    # on one symbol), so the grid spans from disabled to tight.
    "sweep_fvg_reversal": {
        "max_stop_atr": [0, 2.0, 3.0, 5.0],
        "target_r": [1.5, 2.0, 3.0, 4.0],
        "pivot_length": [2, 3, 5, 8],
    },
    "value_area_trend": {
        "trend_window": [20, 40, 60, 90],
        "trend_reversal": [0.5, 1.0, 1.5, 2.0],
        "stop_atr_multiplier": [1.0, 1.5, 2.5, 3.5],
    },
}

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


def deflated_sharpe(daily_returns: pd.Series, trial_sharpes_daily: List[float], n_trials: int) -> float:
    """Probability that the selected strategy's true Sharpe is above zero
    after correcting for selection among `n_trials` (Bailey & Lopez de Prado)."""
    r = daily_returns.dropna()
    if len(r) < 30 or r.std() == 0 or n_trials < 2:
        return float("nan")
    nd = NormalDist()
    sr = r.mean() / r.std()
    var_sr = float(np.var(trial_sharpes_daily, ddof=1)) if len(trial_sharpes_daily) > 1 else 0.0
    euler = 0.5772156649
    sr0 = math.sqrt(var_sr) * ((1 - euler) * nd.inv_cdf(1 - 1 / n_trials)
                               + euler * nd.inv_cdf(1 - 1 / (n_trials * math.e)))
    skew, kurt = r.skew(), r.kurt() + 3.0
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr ** 2))
    return nd.cdf((sr - sr0) * math.sqrt(len(r) - 1) / denom)


def prior_trials(strategy: str) -> int:
    """Variants tried so far. Repeat runs of the same walk-forward variant count once."""
    path = LOG_DIR / f"tuning_trials_{strategy}.jsonl"
    if not path.exists():
        return 0
    total, seen = 0, set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if "variant" in rec:
            key = json.dumps(rec["variant"], sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
        total += rec.get("trials", 0)
    return total


def log_run(strategy: str, record: dict) -> None:
    record = {"time": datetime.now().isoformat(), **record}
    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / f"tuning_trials_{strategy}.jsonl", "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def tune(strategy_name: str, sizing: str, train_ratio: float, min_inner_trades: int) -> dict:
    strategy_cls, _ = STRATEGY_REGISTRY[strategy_name]
    grid = GRIDS[strategy_name]
    keys = list(grid)

    data, bench = load_backtest_data()
    _, split, _ = WalkForwardValidator(strategy=strategy_cls(), train_ratio=train_ratio).split_date(data)
    cutoff = split - pd.Timedelta(seconds=1)
    is_data = {s: df.loc[:cutoff] for s, df in data.items() if len(df.loc[:cutoff]) > 0}
    is_bench = bench.loc[:cutoff] if not bench.empty else None

    print(f"Tuning {strategy_name} on in-sample data only: up to {cutoff.date()}")
    results = []
    combos = list(itertools.product(*(grid[k] for k in keys)))
    for n, values in enumerate(combos, 1):
        params = dict(zip(keys, values))
        inner = WalkForwardValidator(strategy=strategy_cls(params=params), position_sizing_mode=sizing,
                                     train_ratio=0.5, fold_months=3, min_oos_trades=min_inner_trades)
        rep = inner.run(is_data, benchmark=is_bench)
        tr, te = rep.in_sample, rep.out_of_sample
        enough = te.total_trades >= min_inner_trades
        results.append({
            "params": params, "idx": tuple(grid[k].index(v) for k, v in params.items()),
            "train_sharpe": tr.sharpe, "test_sharpe": te.sharpe, "test_trades": te.total_trades,
            "test_pf": te.profit_factor, "score": min(tr.sharpe, te.sharpe) if enough else float("-inf"),
            "returns": te.equity_curve.pct_change().dropna(),
        })
        print(f"  [{n:>3}/{len(combos)}] {params}  train {tr.sharpe:+.2f}  test {te.sharpe:+.2f}  trades {te.total_trades}")

    by_idx = {r["idx"]: r for r in results}
    for r in results:
        neigh = [by_idx[tuple(a + d for a, d in zip(r["idx"], delta))]["score"]
                 for delta in itertools.product((-1, 0, 1), repeat=len(keys))
                 if tuple(a + d for a, d in zip(r["idx"], delta)) in by_idx]
        finite = [s for s in neigh if s != float("-inf")]
        r["plateau"] = float(np.mean(finite)) if len(finite) == len(neigh) else float("-inf")

    ranked = sorted(results, key=lambda r: r["plateau"], reverse=True)
    best = ranked[0]

    n_trials = prior_trials(strategy_name) + len(results)
    trial_sr_daily = [r["test_sharpe"] / math.sqrt(252) for r in results]
    dsr = deflated_sharpe(best["returns"], trial_sr_daily, n_trials)

    print(f"\nTop 10 by plateau score (mean of min(train, test) Sharpe over grid neighbours):")
    print(f"  {'params':<64}{'train':>7}{'test':>7}{'PF':>6}{'trades':>8}{'plateau':>9}")
    for r in ranked[:10]:
        print(f"  {str(r['params']):<64}{r['train_sharpe']:>+7.2f}{r['test_sharpe']:>+7.2f}"
              f"{r['test_pf']:>6.2f}{r['test_trades']:>8}{r['plateau']:>+9.2f}")
    default = strategy_cls().params
    base = next((r for r in results if all(default[k] == v for k, v in r["params"].items())), None)
    if base:
        print(f"  default {base['params']}: train {base['train_sharpe']:+.2f}  test {base['test_sharpe']:+.2f}  plateau {base['plateau']:+.2f}")
    print(f"\nSelected: {best['params']}")
    print(f"Deflated Sharpe probability (true inner-test Sharpe > 0, after {n_trials} trials): {dsr:.1%}")

    log_run(strategy_name, {"time": datetime.now().isoformat(), "kind": "grid", "trials": len(results),
                            "grid": grid, "selected": best["params"], "deflated_sharpe_prob": dsr})
    return best["params"]


def parse_params(pairs: List[str], defaults: dict) -> dict:
    out = {}
    for pair in pairs:
        key, raw = pair.split("=", 1)
        out[key] = type(defaults[key])(raw)
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="In-sample-only parameter tuning")
    parser.add_argument("--strategy", default="range_filtered_trend", choices=list(GRIDS))
    parser.add_argument("--sizing", default=None, choices=["risk_based", "full_equity"])
    parser.add_argument("--train-ratio", type=float, default=0.50)
    parser.add_argument("--min-inner-trades", type=int, default=100)
    parser.add_argument("--final", nargs="*", metavar="KEY=VALUE",
                        help="Run the one-time out-of-sample check with these parameters")
    args = parser.parse_args()

    strategy_cls, default_sizing = STRATEGY_REGISTRY[args.strategy]
    sizing = args.sizing or default_sizing

    if args.final is None:
        tune(args.strategy, sizing, args.train_ratio, args.min_inner_trades)
    else:
        params = parse_params(args.final, strategy_cls().params)
        data, bench = load_backtest_data()
        report = WalkForwardValidator(strategy=strategy_cls(params=params), position_sizing_mode=sizing,
                                      train_ratio=args.train_ratio).run(data, benchmark=None if bench.empty else bench)
        log_run(args.strategy, {"time": datetime.now().isoformat(), "kind": "final_oos", "trials": 0,
                                "params": params, "qualified": report.is_qualified})
        print(f"Final out-of-sample check with {params}")
        print_report(report, args.strategy, sizing)
