"""UT Bot Strategy — ported from the open-source TradingView Pine Script v6
"UT Bot Strategy with Risk Management [TradeIQ]" by Tradeiqofficial,
MPL-2.0 (wrapping QuantNomad's open-source "UT Bot Alerts" signal engine).
Source saved at docs/pine_sources/ut_bot_risk_management.pine.

Mechanics kept from the source (the UT Bot signal engine itself is
timeframe/instrument-agnostic — defaults were tuned for 4H crypto, not
daily equities, so the ATR-multiple parameters are the ones worth tuning
here):
1. ATR trailing-stop line (`ts`): ratchets toward price, only moving in
   the trade's favour, by `key_value * ATR(atr_period)`.
2. Buy signal: close crosses over the trailing line. Sell signal: close
   crosses under it.
3. Optional trend filter: close vs EMA(ema_period).
4. Optional trend-strength filter: ADX(14) >= adx_minimum.
5. Long entry on a buy signal (filters permitting); long exit on a sell
   signal — mirrors the source's `oppRev` (close/reverse on the opposite
   UT signal), short side dropped (long-only system).
6. Stop-loss, fixed at entry (does not trail, matching the source's
   default `slMode = "ATR"`): entry_close - atr_stop_multiplier * ATR.
   No target (the source has an optional scale-out take-profit, off by
   default; not ported).

Dropped entirely as out of scope for this system (handled elsewhere, or
not applicable to NSE cash equities): volume filter, session/day-of-week
filter, all "Money Management" guards (daily loss lock, max trades/day,
loss-streak pause, drawdown kill switch, position sizing modes — this
engine's own cost/tax model and position sizing handle that), webhook
alerts, higher-timeframe filter (daily bars have no meaningful HTF here).
"""
from typing import Dict, Any, Optional
import numpy as np
import pandas as pd
import pandas_ta as ta

from strategies.base_strategy import BaseStrategy


class UtBotStrategy(BaseStrategy):
    """ATR trailing-stop crossover trend strategy, long-only, optional
    EMA/ADX filters, fixed ATR stop set at entry."""

    DEFAULT_PARAMS = {
        "key_value": 3.0,
        "atr_period": 10,
        "use_ema_filter": False,
        "ema_period": 200,
        "use_adx_filter": False,
        "adx_period": 14,
        "adx_minimum": 20.0,
        "stop_atr_period": 14,
        "atr_stop_multiplier": 2.0,
    }

    def __init__(self, name: str = "UtBot_v1", params: Optional[Dict[str, Any]] = None):
        merged_params = self.DEFAULT_PARAMS.copy()
        if params:
            merged_params.update(params)
        super().__init__(name=name, params=merged_params)

    @staticmethod
    def _trailing_stop(src: pd.Series, atr: pd.Series, key_value: float) -> pd.Series:
        """UT Bot's ATR trailing-stop line, ported 1:1 from QuantNomad's recurrence."""
        n_loss = key_value * atr
        values = src.to_numpy(dtype=float)
        loss = n_loss.to_numpy(dtype=float)
        n = len(values)
        ts = np.zeros(n)
        for i in range(n):
            prev_ts = ts[i - 1] if i >= 1 else 0.0
            prev_src = values[i - 1] if i >= 1 else np.nan
            if values[i] > prev_ts and (not np.isnan(prev_src)) and prev_src > prev_ts:
                ts[i] = max(prev_ts, values[i] - loss[i])
            elif values[i] < prev_ts and (not np.isnan(prev_src)) and prev_src < prev_ts:
                ts[i] = min(prev_ts, values[i] + loss[i])
            elif values[i] > prev_ts:
                ts[i] = values[i] - loss[i]
            else:
                ts[i] = values[i] + loss[i]
        return pd.Series(ts, index=src.index)

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params

        atr = ta.atr(df["high"], df["low"], df["close"], length=p["atr_period"])
        df["atr"] = atr if atr is not None else pd.Series(np.nan, index=df.index)
        df["trailing_stop"] = self._trailing_stop(df["close"], df["atr"], p["key_value"])

        ema = ta.ema(df["close"], length=p["ema_period"])
        df["ema"] = ema if ema is not None else pd.Series(np.nan, index=df.index)

        adx_df = ta.adx(df["high"], df["low"], df["close"], length=p["adx_period"])
        adx_col = f"ADX_{p['adx_period']}"
        df["adx"] = adx_df[adx_col] if adx_df is not None and adx_col in adx_df else pd.Series(np.nan, index=df.index)

        stop_atr = ta.atr(df["high"], df["low"], df["close"], length=p["stop_atr_period"])
        df["stop_atr"] = stop_atr if stop_atr is not None else pd.Series(np.nan, index=df.index)

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        closes = df["close"].values
        ts = df["trailing_stop"].values
        ema = df["ema"].values
        adx = df["adx"].values
        stop_atr = df["stop_atr"].values

        n = len(df)
        signals = np.zeros(n, dtype=int)
        stop_losses = np.zeros(n, dtype=float)
        targets = np.zeros(n, dtype=float)

        in_position = False
        current_stop = 0.0

        for i in range(n):
            warm = np.isnan(ts[i]) or np.isnan(stop_atr[i]) or (i < 1) or np.isnan(ts[i - 1])
            if warm:
                continue

            cross_up = closes[i] > ts[i] and closes[i - 1] <= ts[i - 1]
            cross_down = closes[i] < ts[i] and closes[i - 1] >= ts[i - 1]

            ema_ok = (not p["use_ema_filter"]) or np.isnan(ema[i]) is False and closes[i] > ema[i]
            adx_ok = (not p["use_adx_filter"]) or (not np.isnan(adx[i]) and adx[i] >= p["adx_minimum"])

            if not in_position and cross_up and ema_ok and adx_ok:
                signals[i] = 1
                current_stop = closes[i] - p["atr_stop_multiplier"] * stop_atr[i]
                stop_losses[i] = current_stop
                in_position = True
            elif in_position and cross_down:
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
