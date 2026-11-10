"""Triple EMA + RSI Strategy — ported from the open-source TradingView Pine
Script v6 "3 EMA + RSI Filtered" by TomQuixote (MPL-2.0, building on
Free990's open-source "v.5 - 3 EMA + RSI with Trail Stop"). Source saved at
docs/pine_sources/3_ema_rsi_filtered.pine.

Mechanics kept from the source's core signal (defaults there target 1 MNQ
futures contract on 5-minute bars — irrelevant to daily NSE equities, so
only the EMA/RSI/stop parameters are kept, with daily-bar-appropriate
defaults substituted):
1. Three EMAs (fast/mid/slow). Long entry: fast EMA crosses over mid EMA,
   AND fast EMA is above the slow EMA (trend filter), AND the entry
   candle closes green (close > open).
2. Long exit (RSI-based): RSI crosses above `exit_long_rsi` (overbought).
3. Stop-loss: fixed at entry, `stop_loss_points` below entry close (the
   source's "Points" mode, its new default over the original "Percent"
   mode) — kept as a percentage here instead of a fixed point count,
   since a fixed point distance on, say, a Rs. 50 stock vs a Rs. 5000
   stock is not comparable the way it is for one instrument's futures
   contract; `stop_loss_pct` defaults to roughly the source's own
   150-point/typical-MNQ-price ratio.
4. No separate take-profit — target is always 0.0, matching the source.

Dropped entirely as intraday-futures-specific, with no daily-bar
equivalent (this port is for NSE cash equities, long-only, no session
concept on an EOD chart): the source's daily CME-session flatten,
day-of-week/session filters for new entries, holiday early-close
calendar, daily loss lock / loss-streak pause, webhook alerts, and the
Pine `trail_points`/`trail_offset` true trailing-stop order (TradingView's
resting trailing-stop semantics don't map onto this engine's next-open
fill model; the fixed stop above is the faithful substitute, same
simplification the source's own "Percent" stop mode already offers).
"""
from typing import Dict, Any, Optional
import numpy as np
import pandas as pd
import pandas_ta as ta

from strategies.base_strategy import BaseStrategy


class TripleEmaRsiStrategy(BaseStrategy):
    """Triple-EMA crossover entry with RSI-overbought exit, long-only,
    fixed percent stop set at entry."""

    DEFAULT_PARAMS = {
        "fast_ema_period": 10,
        "mid_ema_period": 20,
        "slow_ema_period": 111,
        "rsi_period": 13,
        "exit_long_rsi": 70,
        "stop_loss_pct": 1.1,
    }

    def __init__(self, name: str = "TripleEmaRsi_v1", params: Optional[Dict[str, Any]] = None):
        merged_params = self.DEFAULT_PARAMS.copy()
        if params:
            merged_params.update(params)
        super().__init__(name=name, params=merged_params)

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params

        ema_fast = ta.ema(df["close"], length=p["fast_ema_period"])
        df["ema_fast"] = ema_fast if ema_fast is not None else pd.Series(np.nan, index=df.index)

        ema_mid = ta.ema(df["close"], length=p["mid_ema_period"])
        df["ema_mid"] = ema_mid if ema_mid is not None else pd.Series(np.nan, index=df.index)

        ema_slow = ta.ema(df["close"], length=p["slow_ema_period"])
        df["ema_slow"] = ema_slow if ema_slow is not None else pd.Series(np.nan, index=df.index)

        rsi = ta.rsi(df["close"], length=p["rsi_period"])
        df["rsi"] = rsi if rsi is not None else pd.Series(np.nan, index=df.index)

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        closes = df["close"].values
        opens = df["open"].values
        ema_fast = df["ema_fast"].values
        ema_mid = df["ema_mid"].values
        ema_slow = df["ema_slow"].values
        rsi = df["rsi"].values

        n = len(df)
        signals = np.zeros(n, dtype=int)
        stop_losses = np.zeros(n, dtype=float)
        targets = np.zeros(n, dtype=float)

        in_position = False
        current_stop = 0.0

        for i in range(n):
            warm = (
                np.isnan(ema_fast[i]) or np.isnan(ema_mid[i]) or np.isnan(ema_slow[i]) or
                np.isnan(rsi[i]) or i < 1 or
                np.isnan(ema_fast[i - 1]) or np.isnan(ema_mid[i - 1]) or np.isnan(rsi[i - 1])
            )
            if warm:
                if in_position:
                    stop_losses[i] = current_stop
                continue

            entry_long = (
                ema_fast[i] > ema_mid[i] and ema_fast[i - 1] <= ema_mid[i - 1] and
                ema_fast[i] > ema_slow[i] and closes[i] > opens[i]
            )
            exit_long = rsi[i] > p["exit_long_rsi"] and rsi[i - 1] <= p["exit_long_rsi"]

            if not in_position and entry_long:
                signals[i] = 1
                current_stop = closes[i] * (1.0 - p["stop_loss_pct"] / 100.0)
                stop_losses[i] = current_stop
                in_position = True
            elif in_position and exit_long:
                signals[i] = -1
                stop_losses[i] = current_stop
                in_position = False
                current_stop = 0.0
            elif in_position:
                stop_losses[i] = current_stop

        df["signal"] = signals
        df["stop_loss"] = stop_losses
        df["target"] = targets
        return df
