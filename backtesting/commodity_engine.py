"""MCX commodity backtesting engine: long/short time-series momentum.

Two series, same split as `backtesting.futures_engine` (docs/13 §7):

- **Signal series** (`continuous_df`): a back-adjusted continuous price,
  used only for `compute_momentum_signal` and `compute_realized_vol`. Never
  used for a fill.
- **Execution series** (`overlay_df`): the active front-month contract's own
  OHLC per date (`fut_open/fut_high/fut_low/fut_close/lot_size/expiry/
  tradingsymbol`), identical in shape to `FuturesBacktestEngine`'s overlay.
  Every fill, mark, P&L, sizing and cost figure comes from here. A date
  absent from the overlay is untradeable: skipped, never an error, and any
  queued order (including a forced rollover exit) waits for the next date
  that does have overlay data.

Event loop mirrors `FuturesBacktestEngine.run`: decisions are made at bar
i's close and fill at the next bar with overlay data, at its `fut_open`;
pending-entry / pending-exit flags are mutually exclusive (an entry is only
queued while flat), so a bar sees at most one fill. A long -> short flip is
therefore always close (bar i+1 open) then reopen (bar i+2 open) through
`close_position`/`open_position`, never a direct swap.

Differences from the long-only futures engine:
- Desired direction comes straight from the momentum signal each bar
  (1 long, -1 short, 0 flat); the signal is a state, not an entry/exit
  event, so no latch is needed.
- Sizing is `calculate_vol_target_position_size`, fed the realized vol of
  the bar *before* the fill bar (the latest value fully known at the fill
  bar's open — the fill bar's own vol uses its close, which is lookahead).
- Costs: `IndianCommodityCostModel.calculate(buy_value, sell_value)` with
  legs assigned by direction — long: buy=entry, sell=exit; short:
  sell=entry, buy=exit.
- Trading days to expiry are counted on the signal series' calendar, not
  the overlay's, so a forced rollover is still *decided* on a date the
  overlay is missing and then fills on the first date it resumes.
- A pending entry never opens a contract that is itself already inside the
  rollover buffer (it would be force-closed the next bar); it waits for the
  data layer to list the next front-month.
- If the run ends during an overlay gap with a position still open, it is
  closed at the last available overlay `fut_close` (END_OF_DATA) instead of
  being silently dropped from the trade log.

No tax is applied (same posture as docs/12 §2: F&O/commodity derivative P&L
is business income at the trader's slab rate, which this system does not
model).
"""
from dataclasses import dataclass, field
from typing import Any, List

import numpy as np
import pandas as pd

from backtesting.commodity_cost_model import IndianCommodityCostModel
from backtesting.commodity_signals import (
    calculate_vol_target_position_size,
    compute_momentum_signal,
    compute_realized_vol,
)
from backtesting.engine import first_tradable_index, summarize_equity
from backtesting.slippage import SlippageModel


@dataclass
class CommodityBacktestTrade:
    """Record of a closed MCX commodity futures trade (one contract)."""
    symbol: str
    tradingsymbol: str
    direction: str  # "LONG" or "SHORT"
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
    exit_reason: str  # "SIGNAL_FLIP" | "ROLLOVER" | "END_OF_DATA"


@dataclass
class CommodityBacktestSummary:
    """Consolidated performance metrics for one commodity's backtest, net of
    costs. No tax is applied (see module docstring)."""
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
    trades: List[CommodityBacktestTrade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


class CommodityBacktestEngine:
    """Event-driven bar-by-bar long/short momentum backtest of one MCX
    commodity, executed on its front-month futures contract."""

    def __init__(
        self, vol_target_pct: float = 35.0, margin_pct: float = 25.0,
        slippage_pct: float = 0.0005, rollover_buffer_days: int = 3,
        initial_capital: float = 100000.0,
        lookback_days: int = 252, vol_lookback_days: int = 60,
    ):
        self.vol_target_pct = vol_target_pct
        self.margin_pct = margin_pct
        self.slippage_pct = slippage_pct
        self.rollover_buffer_days = rollover_buffer_days
        self.initial_capital = initial_capital
        self.lookback_days = lookback_days
        self.vol_lookback_days = vol_lookback_days

    def run(
        self, continuous_df: pd.DataFrame, overlay_df: pd.DataFrame, symbol: str,
        start=None, end=None,
    ) -> CommodityBacktestSummary:
        src = continuous_df.loc[:end] if end is not None else continuous_df
        close = src["close"]
        signal = compute_momentum_signal(close, lookback_days=self.lookback_days)
        realized_vol = compute_realized_vol(close, vol_lookback_days=self.vol_lookback_days)

        evaluated = src.copy()
        evaluated["momentum_signal"] = signal
        evaluated["realized_vol_pct"] = realized_vol

        n = len(evaluated)
        timestamps = evaluated.index
        sim_start = first_tradable_index(evaluated, continuous_df.columns)
        if start is not None:
            sim_start = max(sim_start, int(timestamps.searchsorted(start)))

        desired = signal.values  # 1 long, -1 short, 0 flat
        vol_values = realized_vol.values

        # Trading calendar for days-to-expiry: the full (untruncated) signal
        # series, so an expiry just past `end` is still counted correctly.
        calendar = continuous_df.index
        overlay_idx = overlay_df.index

        capital = self.initial_capital
        trades: List[CommodityBacktestTrade] = []
        equity_points = []
        equity_index = []
        exposure_sum = 0.0
        exposure_n = 0
        total_friction = 0.0

        in_pos = False
        pos_dir = 0  # +1 long, -1 short
        entry_time = None
        entry_price = 0.0
        qty = 0
        lot_size = 0
        tradingsymbol = ""
        expiry = None
        # Decided at bar i's close, consumed at the first later bar that has
        # overlay data. Mutually exclusive: an entry is only queued while
        # flat, an exit only while in a position.
        pending_entry = False
        pending_entry_dir = 0
        pending_exit = False
        pending_exit_reason = "SIGNAL_FLIP"

        def days_to_expiry(date, exp):
            """Trading days from `date` to `exp` on the signal calendar, or
            None when `exp` is beyond the known calendar (can't be inside the
            buffer of any bar we will simulate)."""
            if exp is None or pd.isna(exp):
                return None
            exp_ts = pd.Timestamp(exp)
            if exp_ts > calendar[-1]:
                return None
            pos_date = calendar.searchsorted(date, side="left")
            pos_expiry = calendar.searchsorted(exp_ts, side="right") - 1
            return int(pos_expiry - pos_date)

        def inside_buffer(date, exp):
            d = days_to_expiry(date, exp)
            return d is not None and d <= self.rollover_buffer_days

        def close_position(date, raw_exit_price, reason):
            nonlocal capital, in_pos, pos_dir, qty, total_friction
            is_long = pos_dir == 1
            # Closing a long sells; closing a short buys.
            exit_price = SlippageModel.apply_slippage(raw_exit_price, is_buy=not is_long, slippage_pct=self.slippage_pct)
            entry_val = qty * entry_price
            exit_val = qty * exit_price
            gross_pnl = (exit_val - entry_val) if is_long else (entry_val - exit_val)
            if is_long:
                costs = IndianCommodityCostModel.calculate(buy_value=entry_val, sell_value=exit_val)
            else:
                costs = IndianCommodityCostModel.calculate(buy_value=exit_val, sell_value=entry_val)
            friction = costs.total_friction
            total_friction += friction
            net_pnl = gross_pnl - friction
            capital += net_pnl
            trades.append(CommodityBacktestTrade(
                symbol=symbol, tradingsymbol=tradingsymbol, direction="LONG" if is_long else "SHORT",
                entry_time=entry_time, exit_time=date, holding_days=(date - entry_time).days,
                entry_price=entry_price, exit_price=exit_price, quantity=qty, lot_size=lot_size,
                expiry=expiry, entry_value=entry_val, exit_value=exit_val, gross_pnl=gross_pnl,
                friction_costs=friction, net_pnl=net_pnl,
                return_pct=(net_pnl / entry_val) * 100.0 if entry_val > 0 else 0.0,
                exit_reason=reason,
            ))
            in_pos = False
            pos_dir = 0
            qty = 0

        def open_position(date, direction, raw_entry_open, row_lot_size, row_tradingsymbol, row_expiry, vol_pct):
            nonlocal in_pos, pos_dir, entry_time, entry_price, qty, lot_size, tradingsymbol, expiry
            # Opening a long buys; opening a short sells.
            fill_price = SlippageModel.apply_slippage(raw_entry_open, is_buy=(direction == 1), slippage_pct=self.slippage_pct)
            sizing = calculate_vol_target_position_size(
                capital, self.vol_target_pct, fill_price, int(row_lot_size),
                float(vol_pct), margin_pct=self.margin_pct,
            )
            if sizing.quantity > 0:
                in_pos = True
                pos_dir = direction
                entry_time = date
                entry_price = fill_price
                qty = sizing.quantity
                lot_size = int(row_lot_size)
                tradingsymbol = row_tradingsymbol
                expiry = row_expiry

        for i in range(sim_start, n):
            date = timestamps[i]
            has_overlay = date in overlay_idx
            row = overlay_df.loc[date] if has_overlay else None
            is_last = (i == n - 1)

            # 1. Orders queued at an earlier bar fill at this bar's fut_open,
            #    once a date with overlay data arrives. At most one of these
            #    two branches can act (pending flags are mutually exclusive).
            if has_overlay:
                if pending_exit and in_pos:
                    close_position(date, float(row["fut_open"]), pending_exit_reason)
                    pending_exit = False
                elif pending_entry and not in_pos:
                    if inside_buffer(date, row["expiry"]):
                        pass  # listed contract is itself about to roll: keep waiting
                    else:
                        # i > sim_start here (an entry is only queued at an
                        # earlier bar), so vol_values[i - 1] is in range.
                        open_position(date, pending_entry_dir, float(row["fut_open"]), row["lot_size"],
                                      row["tradingsymbol"], row["expiry"], vol_values[i - 1])
                        pending_entry = False

            # 2. End of window: force-close at this bar's fut_close if possible.
            if is_last:
                if in_pos and has_overlay:
                    close_position(date, float(row["fut_close"]), "END_OF_DATA")
            else:
                # 3. Decide this bar's close-of-day action for the NEXT bar.
                if in_pos:
                    forced = inside_buffer(date, expiry)
                    if forced and not pending_exit:
                        pending_exit = True
                        pending_exit_reason = "ROLLOVER"
                    elif desired[i] != pos_dir and not pending_exit:
                        pending_exit = True
                        pending_exit_reason = "SIGNAL_FLIP"
                elif desired[i] != 0:
                    # Re-decided every bar while flat, so a queued entry
                    # always carries the latest signal direction.
                    pending_entry = True
                    pending_entry_dir = int(desired[i])
                else:
                    pending_entry = False

            if in_pos and has_overlay:
                mark = float(row["fut_close"])
                equity = capital + pos_dir * qty * (mark - entry_price)
                exposure_sum += (qty * mark) / equity if equity > 0 else 0.0
            elif in_pos:
                equity = equity_points[-1] if equity_points else capital
            else:
                equity = capital
            exposure_n += 1
            equity_points.append(equity)
            equity_index.append(date)

        # Run ended inside an overlay gap with a position still open: close it
        # at the last available overlay close rather than dropping it.
        if in_pos:
            last_date = timestamps[-1] if n else None
            avail = overlay_df.loc[:last_date]
            last_ov_date = avail.index[-1]
            close_position(last_ov_date, float(avail.iloc[-1]["fut_close"]), "END_OF_DATA")
            if equity_points:
                equity_points[-1] = capital

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
        return CommodityBacktestSummary(
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
