"""Backtesting engine simulating realistic Indian market execution, frictions, and taxes."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any
import numpy as np
import pandas as pd

from configs.logger import logger
from strategies.base_strategy import BaseStrategy
from strategies.trend_breakout import TrendBreakoutStrategy
from strategies.range_filtered_trend import RangeFilteredTrendStrategy
from strategies.gaussian_channel import GaussianChannelStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.ema_vwap_cross import EmaVwapCrossStrategy
from backtesting.cost_model import IndianCostModel
from backtesting.slippage import SlippageModel
from backtesting.tax_model import AnnualTaxLedger

_SIGNAL_COLS = {"signal", "stop_loss", "target", "entry_ref", "risk_distance", "risk_reward_ratio"}


def first_tradable_index(evaluated: pd.DataFrame, source_cols) -> int:
    """First row where every indicator the strategy added has a value."""
    idx = 0
    for col in evaluated.columns:
        if col in source_cols or col in _SIGNAL_COLS:
            continue
        first = evaluated[col].first_valid_index()
        if first is not None:
            idx = max(idx, evaluated.index.get_loc(first))
    return idx


def summarize_equity(equity: pd.Series):
    """(cagr_pct, sharpe, max_drawdown_pct) from a daily mark-to-market equity curve."""
    if len(equity) < 2 or equity.iloc[0] <= 0:
        return 0.0, 0.0, 0.0
    years = max((equity.index[-1] - equity.index[0]).days, 1) / 365.25
    cagr = ((equity.iloc[-1] / equity.iloc[0]) ** (1.0 / years) - 1.0) * 100.0 if equity.iloc[-1] > 0 else -100.0
    rets = equity.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0
    max_dd = float(((equity.cummax() - equity) / equity.cummax()).max() * 100.0)
    return cagr, sharpe, max_dd


# ANSI color codes for signed terminal output — green for profit, red for
# loss, yellow for breakeven. Shared by BacktestSummary and run_universe.py
# so profit/loss is visible at a glance without reading the numbers.
_ANSI_GREEN = "\033[92m"
_ANSI_RED = "\033[91m"
_ANSI_YELLOW = "\033[93m"
_ANSI_BOLD = "\033[1m"
_ANSI_RESET = "\033[0m"


def colorize_signed(text: str, value: float) -> str:
    """Wraps `text` in green/red/yellow ANSI codes based on the sign of `value`."""
    color = _ANSI_GREEN if value > 0 else _ANSI_RED if value < 0 else _ANSI_YELLOW
    return f"{color}{text}{_ANSI_RESET}"

# Maps CLI/script strategy name -> (class, recommended default sizing mode)
STRATEGY_REGISTRY = {
    "trend_breakout": (TrendBreakoutStrategy, "risk_based"),
    "range_filtered_trend": (RangeFilteredTrendStrategy, "full_equity"),
    "gaussian_channel": (GaussianChannelStrategy, "full_equity"),
    "mean_reversion": (MeanReversionStrategy, "risk_based"),
    "ema_vwap_cross": (EmaVwapCrossStrategy, "full_equity"),
}


@dataclass
class BacktestTrade:
    """Record of a closed trade."""
    symbol: str
    entry_time: Any
    exit_time: Any
    holding_days: int
    entry_price: float
    exit_price: float
    quantity: int
    entry_value: float
    exit_value: float
    gross_pnl: float
    friction_costs: float
    tax_paid: float   # always 0.0: tax is assessed per financial year, see BacktestSummary.total_tax_paid
    net_pnl: float    # after costs and slippage, before tax
    return_pct: float
    exit_reason: str


@dataclass
class BacktestSummary:
    """Consolidated performance metrics net of all costs and tax."""
    symbol: str
    initial_capital: float
    ending_capital: float
    gross_return_pct: float
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
    total_tax_paid: float
    trades: List[BacktestTrade] = field(default_factory=list)
    sharpe: float = 0.0
    exposure_pct: float = 0.0  # average share of equity held in the position, per bar
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))

    def print_tearsheet(self) -> None:
        """Prints formatted summary tear sheet with an unmissable profit/loss verdict."""
        net_pnl = self.ending_capital - self.initial_capital

        if self.net_return_pct > 0:
            verdict = "PROFIT"
        elif self.net_return_pct < 0:
            verdict = "LOSS"
        else:
            verdict = "BREAKEVEN"

        print("\n" + "=" * 65)
        print(f"     ARTHA QUANT — BACKTEST PERFORMANCE SUMMARY ({self.symbol})")
        print("=" * 65)
        print(colorize_signed(
            f"{_ANSI_BOLD}>>> NET RESULT: {verdict}  ({self.net_return_pct:+.2f}%  |  ₹{net_pnl:+,.2f}){_ANSI_RESET}",
            self.net_return_pct
        ))
        print("=" * 65)
        print(f"Initial Capital          : ₹{self.initial_capital:,.2f}")
        print(f"Ending Capital (Net)     : ₹{self.ending_capital:,.2f}")
        print(f"Gross Portfolio Return   : {self.gross_return_pct:+.2f}%")
        print(f"Net Return (Post Tax/Fee): {colorize_signed(f'{self.net_return_pct:+.2f}%', self.net_return_pct)}")
        print(f"Annualized CAGR          : {colorize_signed(f'{self.cagr_pct:+.2f}%', self.cagr_pct)}")
        print(f"Max Portfolio Drawdown   : {self.max_drawdown_pct:.2f}%")
        print("-" * 65)
        print(f"Total Closed Trades      : {self.total_trades}")
        print(f"Win Rate                 : {self.win_rate_pct:.1f}% ({self.winning_trades}W / {self.losing_trades}L)")
        print(f"Profit Factor            : {self.profit_factor:.2f}")
        print(f"Expectancy per Trade     : {colorize_signed(f'{self.expectancy_pct:+.2f}%', self.expectancy_pct)}")
        print("-" * 65)
        print(f"Total Fees & Frictions   : ₹{self.total_frictions_paid:,.2f}")
        print(f"Total Capital Gains Tax  : ₹{self.total_tax_paid:,.2f}")
        print("=" * 65 + "\n")


class BacktestEngine:
    """Event-driven bar-by-bar backtesting engine."""

    def __init__(
        self,
        strategy: BaseStrategy,
        initial_capital: float = 100000.0,
        risk_per_trade_pct: float = 1.0,
        max_position_pct: float = 10.0,
        slippage_pct: float = 0.0005,
        apply_tax: bool = True,
        position_sizing_mode: str = "risk_based",
        full_equity_allocation_pct: float = 100.0
    ):
        if position_sizing_mode not in ("risk_based", "full_equity"):
            raise ValueError(f"Unknown position_sizing_mode: {position_sizing_mode!r}")

        self.strategy = strategy
        self.initial_capital = initial_capital
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_position_pct = max_position_pct
        self.slippage_pct = slippage_pct
        self.apply_tax = apply_tax
        self.position_sizing_mode = position_sizing_mode
        self.full_equity_allocation_pct = full_equity_allocation_pct

    def run(self, df: pd.DataFrame, symbol: str = "ASSET", start=None, end=None) -> BacktestSummary:
        """Runs a backtest on one symbol's OHLCV series.

        Signals are read at a bar's close and filled at the next bar's open.
        With `start`/`end`, the strategy still sees all history before
        `start` (indicator warm-up) but trading and metrics cover only the
        window; nothing after `end` is passed to the strategy.
        """
        src = df.loc[:end] if end is not None else df
        evaluated = self.strategy.evaluate(src)

        n = len(evaluated)
        timestamps = evaluated.index
        sim_start = first_tradable_index(evaluated, df.columns)
        if start is not None:
            sim_start = max(sim_start, int(timestamps.searchsorted(start)))

        signals = evaluated["signal"].values
        closes = evaluated["close"].values
        opens = evaluated["open"].values
        highs = evaluated["high"].values
        lows = evaluated["low"].values
        stops = evaluated["stop_loss"].values
        targets = evaluated["target"].values

        capital = self.initial_capital
        ledger = AnnualTaxLedger()
        trades: List[BacktestTrade] = []
        equity_points = []
        exposure_sum = 0.0
        total_friction = 0.0
        total_tax = 0.0

        in_pos = False
        entry_time = None
        entry_price = 0.0
        qty = 0
        stop_loss = 0.0
        target = 0.0
        pending_entry = None   # (stop, target) from the signal bar
        pending_exit = False

        def close_position(i, raw_exit, reason):
            nonlocal capital, in_pos, qty, total_friction
            exit_price = SlippageModel.apply_slippage(raw_exit, is_buy=False, slippage_pct=self.slippage_pct)
            entry_val = qty * entry_price
            exit_val = qty * exit_price
            gross_pnl = exit_val - entry_val
            friction = IndianCostModel.calculate(entry_val, exit_val, is_delivery=True).total_friction
            total_friction += friction
            net_pnl = gross_pnl - friction
            holding_days = (timestamps[i] - entry_time).days
            if self.apply_tax:
                ledger.record(net_pnl, holding_days, timestamps[i])
            capital += net_pnl
            trades.append(BacktestTrade(
                symbol=symbol, entry_time=entry_time, exit_time=timestamps[i],
                holding_days=holding_days, entry_price=entry_price, exit_price=exit_price,
                quantity=qty, entry_value=entry_val, exit_value=exit_val, gross_pnl=gross_pnl,
                friction_costs=friction, tax_paid=0.0, net_pnl=net_pnl,
                return_pct=(net_pnl / entry_val) * 100.0 if entry_val > 0 else 0.0,
                exit_reason=reason
            ))
            in_pos = False
            qty = 0

        for i in range(sim_start, n):
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]

            if self.apply_tax:
                tax = ledger.due_on_rollover(timestamps[i])
                capital -= tax
                total_tax += tax

            # 1. Orders queued at the previous close fill at this open.
            if pending_exit and in_pos:
                close_position(i, o, "SIGNAL_EXIT")
            pending_exit = False

            if pending_entry is not None and not in_pos:
                stop_p, target_p = pending_entry
                fill_price = SlippageModel.apply_slippage(o, is_buy=True, slippage_pct=self.slippage_pct)
                gapped_through = (stop_p > 0 and fill_price <= stop_p) or (target_p > 0 and fill_price >= target_p)
                final_qty = 0
                if not gapped_through:
                    if self.position_sizing_mode == "full_equity":
                        # Signal-only strategies with no technical stop: allocate a
                        # fixed fraction of equity per trade (mirrors TradingView's
                        # default_qty_type=strategy.percent_of_equity).
                        alloc_budget = capital * (self.full_equity_allocation_pct / 100.0)
                        final_qty = int(alloc_budget / fill_price) if fill_price > 0 else 0
                    else:
                        # Dual-Constraint Position Sizing (requires a real stop distance)
                        stop_dist = fill_price - stop_p
                        if stop_dist > 0:
                            qty_risk = int(capital * (self.risk_per_trade_pct / 100.0) / stop_dist)
                            qty_cap = int(capital * (self.max_position_pct / 100.0) / fill_price)
                            final_qty = min(qty_risk, qty_cap)
                if final_qty > 0:
                    in_pos = True
                    entry_time = timestamps[i]
                    entry_price = fill_price
                    qty = final_qty
                    stop_loss = stop_p
                    target = target_p
            pending_entry = None

            # 2. Intrabar stop/target. A gap through the level fills at the open.
            #    If both are touched in one bar, assume the stop came first.
            if in_pos:
                stop_hit = stop_loss > 0 and l <= stop_loss
                target_hit = target > 0 and h >= target
                if target_hit and (not stop_hit or o >= target):
                    close_position(i, max(o, target), "TARGET")
                elif stop_hit:
                    close_position(i, min(o, stop_loss), "STOP_LOSS")

            # 3. End of the window, or act on this bar's closing signal.
            if i == n - 1:
                if in_pos:
                    close_position(i, c, "END_OF_DATA")
            elif in_pos:
                if signals[i] == -1:
                    pending_exit = True
                elif stops[i] > stop_loss:
                    stop_loss = stops[i]  # ratchet a trailing stop; applies from the next bar
            elif signals[i] == 1:
                pending_entry = (stops[i], targets[i])

            position_value = qty * c if in_pos else 0.0
            equity = capital + (qty * (c - entry_price) if in_pos else 0.0)
            exposure_sum += position_value / equity if equity > 0 else 0.0
            equity_points.append(equity)

        if self.apply_tax:
            tax = ledger.settle()
            capital -= tax
            total_tax += tax
            if equity_points:
                equity_points[-1] = capital

        sim_index = timestamps[sim_start:]
        equity_curve = pd.Series(equity_points, index=sim_index, dtype=float)
        if len(equity_curve):
            equity_curve = pd.concat([pd.Series([self.initial_capital], index=[sim_index[0] - pd.Timedelta(days=1)]), equity_curve])
        cagr, sharpe, max_drawdown = summarize_equity(equity_curve)

        total_trades = len(trades)
        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl <= 0]
        gross_profit = sum(t.net_pnl for t in wins)
        gross_loss = abs(sum(t.net_pnl for t in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

        total_net_pnl = capital - self.initial_capital
        return BacktestSummary(
            symbol=symbol,
            initial_capital=self.initial_capital,
            ending_capital=capital,
            gross_return_pct=((total_net_pnl + total_friction + total_tax) / self.initial_capital) * 100.0,
            net_return_pct=(total_net_pnl / self.initial_capital) * 100.0,
            cagr_pct=cagr,
            total_trades=total_trades,
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate_pct=(len(wins) / total_trades * 100.0) if total_trades else 0.0,
            profit_factor=profit_factor,
            expectancy_pct=float(np.mean([t.return_pct for t in trades])) if trades else 0.0,
            max_drawdown_pct=max_drawdown,
            total_frictions_paid=total_friction,
            total_tax_paid=total_tax,
            trades=trades,
            sharpe=sharpe,
            exposure_pct=(exposure_sum / len(equity_points) * 100.0) if equity_points else 0.0,
            equity_curve=equity_curve,
        )


if __name__ == "__main__":
    import argparse
    from data.store.parquet_store import get_parquet_store

    parser = argparse.ArgumentParser(description="Artha Quant Backtest Runner")
    parser.add_argument("--symbol", default="RELIANCE", help="Symbol to backtest")
    parser.add_argument("--capital", type=float, default=100000.0, help="Starting capital in INR")
    parser.add_argument("--strategy", default="trend_breakout", choices=list(STRATEGY_REGISTRY.keys()), help="Strategy to backtest")
    parser.add_argument("--sizing", default=None, choices=["risk_based", "full_equity"], help="Position sizing mode (defaults to the strategy's recommended mode)")
    args = parser.parse_args()

    store = get_parquet_store()
    data = store.load_bars(args.symbol, timeframe="1D")

    if data.empty:
        print(f"\n[!] No local Parquet data found for {args.symbol} in data/store/1D/.")
        print("    Run 'python -m data.loaders.sync_history' to ingest market data.\n")
    else:
        strategy_cls, default_sizing = STRATEGY_REGISTRY[args.strategy]
        sizing_mode = args.sizing or default_sizing
        strat = strategy_cls()
        backtester = BacktestEngine(strategy=strat, initial_capital=args.capital, position_sizing_mode=sizing_mode)
        result = backtester.run(data, symbol=args.symbol)
        print(f"\nStrategy: {args.strategy} | Sizing mode: {sizing_mode}")
        result.print_tearsheet()
