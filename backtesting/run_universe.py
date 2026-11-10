"""Runs a backtest strategy across every symbol in configs/universe.yaml and
prints a per-symbol + aggregate performance summary.

Usage:
    python -m backtesting.run_universe --strategy range_filtered_trend
    python -m backtesting.run_universe --strategy trend_breakout --sizing risk_based
    python -m backtesting.run_universe --strategy range_filtered_trend --csv out.csv
"""
import argparse
import csv
from dataclasses import dataclass
from typing import List, Optional

from configs.logger import logger
from data.loaders.sync_history import load_universe
from data.store.parquet_store import get_parquet_store
from backtesting.engine import BacktestEngine, STRATEGY_REGISTRY, colorize_signed


@dataclass
class SymbolResult:
    symbol: str
    total_trades: int
    win_rate_pct: float
    net_return_pct: float
    cagr_pct: float
    max_drawdown_pct: float
    profit_factor: float
    total_tax_paid: float
    total_frictions_paid: float


def run_universe_backtest(
    strategy_name: str,
    sizing_mode: Optional[str] = None,
    initial_capital: float = 100000.0
) -> List[SymbolResult]:
    strategy_cls, default_sizing = STRATEGY_REGISTRY[strategy_name]
    resolved_sizing = sizing_mode or default_sizing

    store = get_parquet_store()
    results: List[SymbolResult] = []

    for asset in load_universe():
        symbol = asset["symbol"]
        data = store.load_bars(symbol, timeframe="1D")

        if data.empty:
            logger.warning(f"No local Parquet data for {symbol}, skipping. Run 'make sync' first.")
            continue

        strat = strategy_cls()
        engine = BacktestEngine(strategy=strat, initial_capital=initial_capital, position_sizing_mode=resolved_sizing)
        summary = engine.run(data, symbol=symbol)

        results.append(SymbolResult(
            symbol=symbol,
            total_trades=summary.total_trades,
            win_rate_pct=summary.win_rate_pct,
            net_return_pct=summary.net_return_pct,
            cagr_pct=summary.cagr_pct,
            max_drawdown_pct=summary.max_drawdown_pct,
            profit_factor=summary.profit_factor,
            total_tax_paid=summary.total_tax_paid,
            total_frictions_paid=summary.total_frictions_paid
        ))

    return results


def print_results_table(results: List[SymbolResult], strategy_name: str, sizing_mode: str) -> None:
    print("\n" + "=" * 95)
    print(f"  ARTHA QUANT — UNIVERSE BACKTEST ({strategy_name}, sizing={sizing_mode})")
    print("=" * 95)
    header = f"{'Symbol':<12}{'Trades':>8}{'WinRate%':>10}{'NetRet%':>10}{'CAGR%':>9}{'MaxDD%':>9}{'ProfitF':>9}"
    print(header)
    print("-" * 95)

    for r in sorted(results, key=lambda x: x.net_return_pct, reverse=True):
        net_ret_str = colorize_signed(f"{r.net_return_pct:>9.2f}", r.net_return_pct)
        print(
            f"{r.symbol:<12}{r.total_trades:>8}{r.win_rate_pct:>10.1f}"
            f"{net_ret_str}{r.cagr_pct:>9.2f}{r.max_drawdown_pct:>9.2f}{r.profit_factor:>9.2f}"
        )

    print("-" * 95)
    if results:
        n = len(results)
        avg_net_ret = sum(r.net_return_pct for r in results) / n
        avg_cagr = sum(r.cagr_pct for r in results) / n
        avg_win_rate = sum(r.win_rate_pct for r in results) / n
        total_trades = sum(r.total_trades for r in results)
        profitable_symbols = sum(1 for r in results if r.net_return_pct > 0)

        overall_verdict = "PROFITABLE" if avg_net_ret > 0 else "UNPROFITABLE" if avg_net_ret < 0 else "BREAKEVEN"
        print(colorize_signed(f">>> OVERALL: {overall_verdict}  (avg net return {avg_net_ret:+.2f}%)", avg_net_ret))
        print(f"Symbols tested           : {n}")
        print(f"Profitable symbols       : {profitable_symbols}/{n}")
        print(f"Average Net Return       : {colorize_signed(f'{avg_net_ret:+.2f}%', avg_net_ret)}")
        print(f"Average CAGR             : {colorize_signed(f'{avg_cagr:+.2f}%', avg_cagr)}")
        print(f"Average Win Rate         : {avg_win_rate:.1f}%")
        print(f"Total Trades (all syms)  : {total_trades}")
    print("=" * 95 + "\n")


def write_csv(results: List[SymbolResult], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "symbol", "total_trades", "win_rate_pct", "net_return_pct",
            "cagr_pct", "max_drawdown_pct", "profit_factor", "total_tax_paid", "total_frictions_paid"
        ])
        for r in results:
            writer.writerow([
                r.symbol, r.total_trades, r.win_rate_pct, r.net_return_pct,
                r.cagr_pct, r.max_drawdown_pct, r.profit_factor, r.total_tax_paid, r.total_frictions_paid
            ])
    print(f"Wrote {len(results)} rows to {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Artha Quant Universe Backtest Runner")
    parser.add_argument("--strategy", default="range_filtered_trend", choices=list(STRATEGY_REGISTRY.keys()))
    parser.add_argument("--sizing", default=None, choices=["risk_based", "full_equity"], help="Defaults to the strategy's recommended mode")
    parser.add_argument("--capital", type=float, default=100000.0)
    parser.add_argument("--csv", default=None, help="Optional path to write per-symbol results as CSV")
    args = parser.parse_args()

    strategy_cls, default_sizing = STRATEGY_REGISTRY[args.strategy]
    resolved_sizing = args.sizing or default_sizing

    all_results = run_universe_backtest(args.strategy, sizing_mode=resolved_sizing, initial_capital=args.capital)
    print_results_table(all_results, args.strategy, resolved_sizing)

    if args.csv:
        write_csv(all_results, args.csv)
