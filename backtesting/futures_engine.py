"""Futures backtesting engine: strategy logic runs on deep cash-equity
history (unchanged, same `BaseStrategy.evaluate` every other engine calls),
execution runs against a parallel NSE single-stock-futures "overlay" table.

Why this split exists (see docs/12-futures-trading-plan.md §3.1): a single
NFO futures contract only lives ~2-3 months, nowhere near enough bars for
these strategies' warm-up (e.g. `gaussian_channel`'s 200-bar WMA). So
`strategy.evaluate()` is run on the same cash OHLCV `BacktestEngine` uses,
and only fills, P&L, lot-size sizing, costs, and the forced rollover exit
are computed against the active futures contract's own OHLC for that date
(the "futures-overlay table", docs/12 §4: `fut_open/fut_high/fut_low/
fut_close/lot_size/expiry/tradingsymbol`, indexed by date). A date absent
from the overlay (no contract listed yet, or gap in sync) is simply
untradeable — skipped, never an error, and never carries a pending order
forward across the gap.

This is a standalone engine, not a `BacktestEngine` subclass: the lot-size
rounding, margin-based sizing, and expiry-driven forced-exit logic have no
equivalent in the cash-equity engine's intrabar stop/target logic, and
forcing a shared base class would obscure both (same design note as
`backtesting.pairs_engine`).

No tax is applied anywhere in this engine — see docs/12 §2 ("Non-goals"):
F&O P&L is business income taxed at the trader's personal slab rate, which
this system has no model of, so fabricating one would be a guess dressed
up as a number. Same posture, same labeling, as the pairs engine.
"""
from dataclasses import dataclass, field
from typing import Any, List, Optional

import numpy as np
import pandas as pd

from strategies.base_strategy import BaseStrategy
from backtesting.engine import first_tradable_index, summarize_equity
from backtesting.slippage import SlippageModel

from backtesting.futures_cost_model import IndianFuturesCostModel, calculate_futures_position_size


_OVERLAY_COLS = ["fut_open", "fut_high", "fut_low", "fut_close", "lot_size", "expiry", "tradingsymbol"]


@dataclass
class FuturesBacktestTrade:
    """Record of a closed futures trade (one contract, long-only)."""
    symbol: str
    tradingsymbol: str
    entry_time: Any
    exit_time: Any
    holding_days: int
    entry_price: float
    exit_price: float
    quantity: int
    lot_size: int
    expiry: Any
    entry_value: float
    exit_value: float
    gross_pnl: float
    friction_costs: float
    net_pnl: float
    return_pct: float
    exit_reason: str


@dataclass
class FuturesBacktestSummary:
    """Consolidated performance metrics for one symbol's futures backtest,
    net of costs. No tax is applied anywhere (see module docstring)."""
    symbol: str
    initial_capital: float
    ending_capital: float
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
    sharpe: float = 0.0
    exposure_pct: float = 0.0
    trades: List[FuturesBacktestTrade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


class FuturesBacktestEngine:
    """Event-driven bar-by-bar backtest of one symbol's long-only signal,
    executed on NSE single-stock futures."""

    def __init__(
        self,
        strategy: BaseStrategy,
        initial_capital: float = 100000.0,
        allocation_pct: float = 20.0,
        margin_pct: float = 20.0,
        slippage_pct: float = 0.0005,
        rollover_buffer_days: int = 3,
    ):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.allocation_pct = allocation_pct
        self.margin_pct = margin_pct
        self.slippage_pct = slippage_pct
        self.rollover_buffer_days = rollover_buffer_days

    def run(
        self, cash_df: pd.DataFrame, overlay_df: pd.DataFrame, symbol: str,
        start=None, end=None,
    ) -> FuturesBacktestSummary:
        """Signals are computed on `cash_df` (unchanged strategy pipeline) and
        filled against `overlay_df`'s futures OHLC, one bar later, exactly
        as `BacktestEngine.run` fills against the cash open. Any date absent
        from `overlay_df` is untradeable: skipped, no error — a queued order
        simply waits for the next date that does have overlay data (see
        design note on `pending_entry`/`pending_exit` below)."""
        src = cash_df.loc[:end] if end is not None else cash_df
        evaluated = self.strategy.evaluate(src)

        n = len(evaluated)
        timestamps = evaluated.index
        sim_start = first_tradable_index(evaluated, cash_df.columns)
        if start is not None:
            sim_start = max(sim_start, int(timestamps.searchsorted(start)))

        signals = evaluated["signal"].values

        # The cash-computed "wants to be long" state, independent of whatever
        # our actual futures position is doing (a forced rollover exit does
        # not reset this — the strategy itself never saw signal -1, so it
        # still "thinks" it's long, which is exactly what triggers
        # re-entry below once we're flat again post-rollover).
        wants_long = np.zeros(n, dtype=bool)
        _state = False
        for i in range(n):
            if signals[i] == 1 and not _state:
                _state = True
            elif signals[i] == -1 and _state:
                _state = False
            wants_long[i] = _state

        overlay_idx = overlay_df.index

        capital = self.initial_capital
        trades: List[FuturesBacktestTrade] = []
        equity_points = []
        equity_index = []
        exposure_sum = 0.0
        exposure_n = 0
        total_friction = 0.0

        in_pos = False
        entry_time = None
        entry_price = 0.0
        qty = 0
        lot_size = 0
        tradingsymbol = ""
        expiry = None
        # Decided at the end of bar i, consumed at the first later bar that
        # has overlay data — this is what lets a rollover re-entry land on
        # "the next available date" rather than dying with the gap, while a
        # plain missing-data gap elsewhere behaves identically (the order
        # just waits for liquidity, same as a real resting order would).
        pending_entry = False
        pending_exit = False
        pending_exit_reason = "SIGNAL_EXIT"

        def close_position(date, raw_exit_open, reason):
            nonlocal capital, in_pos, qty, total_friction
            exit_price = SlippageModel.apply_slippage(raw_exit_open, is_buy=False, slippage_pct=self.slippage_pct)
            entry_val = qty * entry_price
            exit_val = qty * exit_price
            gross_pnl = exit_val - entry_val
            friction = IndianFuturesCostModel.calculate(entry_val, exit_val).total_friction
            total_friction += friction
            net_pnl = gross_pnl - friction
            holding_days = (date - entry_time).days
            capital += net_pnl
            trades.append(FuturesBacktestTrade(
                symbol=symbol, tradingsymbol=tradingsymbol, entry_time=entry_time, exit_time=date,
                holding_days=holding_days, entry_price=entry_price, exit_price=exit_price,
                quantity=qty, lot_size=lot_size, expiry=expiry,
                entry_value=entry_val, exit_value=exit_val, gross_pnl=gross_pnl,
                friction_costs=friction, net_pnl=net_pnl,
                return_pct=(net_pnl / entry_val) * 100.0 if entry_val > 0 else 0.0,
                exit_reason=reason,
            ))
            in_pos = False
            qty = 0

        def open_position(date, raw_entry_open, row_lot_size, row_tradingsymbol, row_expiry):
            nonlocal in_pos, entry_time, entry_price, qty, lot_size, tradingsymbol, expiry
            fill_price = SlippageModel.apply_slippage(raw_entry_open, is_buy=True, slippage_pct=self.slippage_pct)
            sizing = calculate_futures_position_size(
                capital, self.allocation_pct, fill_price, int(row_lot_size), margin_pct=self.margin_pct,
            )
            final_qty = sizing.quantity
            if final_qty > 0:
                in_pos = True
                entry_time = date
                entry_price = fill_price
                qty = final_qty
                lot_size = int(row_lot_size)
                tradingsymbol = row_tradingsymbol
                expiry = row_expiry

        for i in range(sim_start, n):
            date = timestamps[i]
            has_overlay = date in overlay_idx
            row = overlay_df.loc[date] if has_overlay else None
            is_last = (i == n - 1)

            # 1. Orders queued at an earlier bar fill at this bar's fut_open —
            #    but only once a date with overlay data actually arrives.
            if has_overlay:
                if pending_exit and in_pos:
                    close_position(date, float(row["fut_open"]), pending_exit_reason)
                    pending_exit = False
                if pending_entry and not in_pos:
                    open_position(date, float(row["fut_open"]), row["lot_size"], row["tradingsymbol"], row["expiry"])
                    pending_entry = False

            # 2. End of window: force-close at this bar's fut_close if possible.
            if is_last:
                if in_pos and has_overlay:
                    close_position(date, float(row["fut_close"]), "END_OF_DATA")
            else:
                # 3. Decide this bar's close-of-day action for the NEXT bar.
                if in_pos:
                    forced = False
                    if has_overlay and expiry is not None:
                        expiry_ts = pd.Timestamp(expiry)
                        pos_date = overlay_idx.get_loc(date)
                        pos_expiry = overlay_idx.searchsorted(expiry_ts, side="right") - 1
                        forced = (pos_expiry - pos_date) <= self.rollover_buffer_days
                    if forced and not pending_exit:
                        pending_exit = True
                        pending_exit_reason = "ROLLOVER"
                    elif not wants_long[i] and not pending_exit:
                        pending_exit = True
                        pending_exit_reason = "SIGNAL_EXIT"
                elif wants_long[i] and not pending_entry:
                    pending_entry = True

            if in_pos and has_overlay:
                mark = float(row["fut_close"])
                equity = capital + qty * (mark - entry_price)
                position_value = qty * mark
                exposure_sum += position_value / equity if equity > 0 else 0.0
            else:
                equity = equity_points[-1] if equity_points else capital
            exposure_n += 1
            equity_points.append(equity)
            equity_index.append(date)

        equity_curve = pd.Series(equity_points, index=pd.DatetimeIndex(equity_index), dtype=float)
        if len(equity_curve):
            equity_curve = pd.concat([
                pd.Series([self.initial_capital], index=[equity_curve.index[0] - pd.Timedelta(days=1)]),
                equity_curve,
            ])
        cagr, sharpe, max_dd = summarize_equity(equity_curve)

        total_trades = len(trades)
        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl <= 0]
        gross_profit = sum(t.net_pnl for t in wins)
        gross_loss = abs(sum(t.net_pnl for t in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0)

        total_net_pnl = capital - self.initial_capital
        return FuturesBacktestSummary(
            symbol=symbol,
            initial_capital=self.initial_capital,
            ending_capital=capital,
            net_return_pct=(total_net_pnl / self.initial_capital) * 100.0,
            cagr_pct=cagr,
            total_trades=total_trades,
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate_pct=(len(wins) / total_trades * 100.0) if total_trades else 0.0,
            profit_factor=profit_factor,
            expectancy_pct=float(np.mean([t.return_pct for t in trades])) if trades else 0.0,
            max_drawdown_pct=max_dd,
            total_frictions_paid=total_friction,
            sharpe=sharpe,
            exposure_pct=(exposure_sum / exposure_n * 100.0) if exposure_n else 0.0,
            trades=trades,
            equity_curve=equity_curve,
        )
