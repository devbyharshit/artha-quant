"""Mean Reversion Strategy — built from scratch (not a Pine Script port), to
test a fundamentally different premise than the two trend-following
strategies already in this system: buy statistically-stretched dips rather
than breakouts, in names that are still in a structural uptrend.

Mechanics:
1. Bollinger Bands (SMA + N std-dev) on close.
2. Long-term EMA trend filter: only buy dips in names whose price is still
   above the long-term trend — this directly targets the failure mode seen
   in TrendBreakoutStrategy and the Pine ports, where buying into names with
   a declining underlying (e.g. HINDUNILVR) bled money on repeated false
   starts. A dip below the lower band in a structurally declining stock is
   not "oversold", it's a falling knife, and is deliberately not traded.
3. Entry: close breaks below the lower Bollinger Band AND close is above
   the long-term EMA.
4. Exit: close reverts to the middle band (SMA) — the reversion target —
   OR an ATR-based stop-loss is hit — OR the position has been held past
   max_holding_days (mean reversion that hasn't happened within a bounded
   window is itself a signal that the thesis for this trade failed; this
   caps the downside of a dip that just keeps dipping).

Unlike the two Pine-ported strategies, this one has a real technical stop
and target, so it plugs into BacktestEngine's existing risk-based position
sizing (position_sizing_mode="risk_based") without any sizing workaround.
"""
from typing import Dict, Any, Optional
import numpy as np
import pandas as pd
import pandas_ta as ta

from strategies.base_strategy import BaseStrategy


class MeanReversionStrategy(BaseStrategy):
    """Bollinger Band dip-buy with a long-term trend filter, ATR stop, and
    a reversion-to-mean target. Long-only."""

    DEFAULT_PARAMS = {
        "bb_period": 20,
        "bb_std": 2.0,
        "ema_trend_period": 200,
        "atr_period": 14,
        "atr_stop_multiplier": 2.0,
        "max_holding_days": 10,
    }

    def __init__(self, name: str = "MeanReversion_v1", params: Optional[Dict[str, Any]] = None):
        merged_params = self.DEFAULT_PARAMS.copy()
        if params:
            merged_params.update(params)
        super().__init__(name=name, params=merged_params)

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params

        sma = df["close"].rolling(window=p["bb_period"]).mean()
        std = df["close"].rolling(window=p["bb_period"]).std()
        df["bb_sma"] = sma
        df["bb_upper"] = sma + p["bb_std"] * std
        df["bb_lower"] = sma - p["bb_std"] * std

        ema = ta.ema(df["close"], length=p["ema_trend_period"])
        df["ema_trend"] = ema if ema is not None else pd.Series(np.nan, index=df.index)

        atr = ta.atr(df["high"], df["low"], df["close"], length=p["atr_period"])
        df["atr"] = atr if atr is not None else pd.Series(np.nan, index=df.index)

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        closes = df["close"].values
        bb_lower = df["bb_lower"].values
        bb_sma = df["bb_sma"].values
        ema_trend = df["ema_trend"].values
        atr = df["atr"].values

        n = len(df)
        signals = np.zeros(n, dtype=int)
        stop_losses = np.zeros(n, dtype=float)
        targets = np.zeros(n, dtype=float)

        in_position = False
        current_stop = 0.0
        current_target = 0.0
        entry_idx = 0

        for i in range(n):
            if (
                np.isnan(bb_lower[i]) or np.isnan(bb_sma[i]) or
                np.isnan(ema_trend[i]) or np.isnan(atr[i])
            ):
                signals[i] = 0
                continue

            close_p = closes[i]

            if not in_position:
                # BUY CONDITION: price is statistically stretched below its
                # own short-term mean, but the stock is still above its
                # long-term trend (buy the dip, not the falling knife).
                buy_condition = (close_p < bb_lower[i]) and (close_p > ema_trend[i])

                if buy_condition:
                    signals[i] = 1
                    current_stop = round(close_p - p["atr_stop_multiplier"] * atr[i], 2)
                    current_target = round(bb_sma[i], 2)
                    stop_losses[i] = current_stop
                    targets[i] = current_target
                    in_position = True
                    entry_idx = i
                else:
                    signals[i] = 0
                    stop_losses[i] = 0.0
                    targets[i] = 0.0

            else:
                held_days = i - entry_idx
                exit_condition = (
                    close_p <= current_stop or
                    close_p >= current_target or
                    held_days >= p["max_holding_days"]
                )

                if exit_condition:
                    signals[i] = -1
                    stop_losses[i] = current_stop
                    targets[i] = current_target
                    in_position = False
                    current_stop = 0.0
                    current_target = 0.0
                else:
                    signals[i] = 0
                    stop_losses[i] = current_stop
                    targets[i] = current_target

        df["signal"] = signals
        df["stop_loss"] = stop_losses
        df["target"] = targets
        return df
