"""Displaced Donchian Decycler Strategy — ported from the open-source
TradingView Pine Script v6 "Displaced Donchian Decycler Strategy || Trend
Breakout with MFI" (DDDS) by blitz_algo, MPL-2.0. Source saved at
docs/pine_sources/displaced_donchian_decycler.pine.

Mechanics (faithful to the source; the source is already daily-bar-native,
no intraday session logic to strip):
1. Displaced Donchian trend state: look at the window of bars
   [startLen, endLen-1] back from the current bar (NOT the most recent
   bars — the window is itself lagged/displaced). The sticky `state` flips
   to bullish the first time the current high exceeds the *previous bar's*
   rolling high of that displaced window, and to bearish the mirror way on
   lows.
2. Two-pole decycler high-pass filter subtracted from close, used as a
   trend filter: price must be above the decycler line and the decycler
   itself must be rising over `slope_bars`.
3. MFI filter: long only confirmed when MFI > 50. This port uses
   pandas_ta's standard high/low/close/volume MFI as a stand-in for Pine's
   `ta.mfi(hlc3, length)` single-series builtin, which computes money flow
   differently (classifies by change in the given source, not full
   typical-price ranges) — a documented approximation, not a 1:1 formula
   match.
4. Long entry: state flips bullish AND the decycler filter AND the MFI
   filter are all true on the same bar, edge-triggered on the flip.
5. Long exit: state flips bearish (mirrors the source's "flip against the
   position" close, with the short branch dropped — this system is
   long-only, matching every other strategy here).
6. Stop/target: fixed at entry using that bar's ATR (the source's
   `entryATR` var is set once per entry and held, not re-evaluated or
   trailed) — stop = entry_close - atr_stop_multiplier * ATR, target =
   entry_close + atr_target_multiplier * ATR.

Allowed shorts in the source (`allowShorts`) are dropped for the same
long-only reason as every other port in this system.
"""
from typing import Dict, Any, Optional
import numpy as np
import pandas as pd
import pandas_ta as ta

from strategies.base_strategy import BaseStrategy


class DonchianDecyclerStrategy(BaseStrategy):
    """Displaced Donchian breakout + decycler trend filter + MFI, long-only,
    fixed ATR stop/target set at entry."""

    DEFAULT_PARAMS = {
        "start_len": 31,
        "end_len": 36,
        "use_decycler": True,
        "decycler_period": 60,
        "slope_bars": 3,
        "use_mfi": True,
        "mfi_period": 14,
        "mfi_smooth": 4,
        "atr_period": 14,
        "atr_stop_multiplier": 2.0,
        "atr_target_multiplier": 3.0,
    }

    def __init__(self, name: str = "DonchianDecycler_v1", params: Optional[Dict[str, Any]] = None):
        merged_params = self.DEFAULT_PARAMS.copy()
        if params:
            merged_params.update(params)
        super().__init__(name=name, params=merged_params)

    @staticmethod
    def _decycler(src: pd.Series, period: int) -> pd.Series:
        """Two-pole high-pass decycler, ported 1:1 from the Pine `decycler()` function."""
        ang = 0.707 * 2.0 * np.pi / period
        alpha = (np.cos(ang) + np.sin(ang) - 1.0) / np.cos(ang)
        decay = 1.0 - alpha
        gain = (1.0 - alpha * 0.5) ** 2.0

        values = src.to_numpy(dtype=float)
        n = len(values)
        hp = np.zeros(n)
        for i in range(n):
            if i >= 2:
                hp[i] = gain * (values[i] - 2.0 * values[i - 1] + values[i - 2]) + 2.0 * decay * hp[i - 1] - decay * decay * hp[i - 2]
            else:
                hp[i] = 0.0
        return pd.Series(values - hp, index=src.index)

    @staticmethod
    def _displaced_donchian_state(high: pd.Series, low: pd.Series, start_len: int, end_len: int) -> pd.Series:
        """Sticky trend state (1 bullish, -1 bearish, 0 before the first flip),
        ported 1:1 from the Pine for-loop + `var int state` block."""
        window = end_len - start_len
        hh = high.shift(start_len).rolling(window=window).max()
        ll = low.shift(start_len).rolling(window=window).min()
        hh_prev = hh.shift(1).to_numpy(dtype=float)
        ll_prev = ll.shift(1).to_numpy(dtype=float)

        h = high.to_numpy(dtype=float)
        l = low.to_numpy(dtype=float)
        n = len(high)
        state = np.zeros(n, dtype=int)
        cur = 0
        for i in range(n):
            if not np.isnan(hh_prev[i]) and h[i] > hh_prev[i]:
                cur = 1
            if not np.isnan(ll_prev[i]) and l[i] < ll_prev[i]:
                cur = -1
            state[i] = cur
        return pd.Series(state, index=high.index)

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params

        df["state"] = self._displaced_donchian_state(df["high"], df["low"], p["start_len"], p["end_len"])
        df["decycler"] = self._decycler(df["close"], p["decycler_period"])

        mfi = ta.mfi(df["high"], df["low"], df["close"], df["volume"], length=p["mfi_period"])
        mfi = ta.sma(mfi, length=p["mfi_smooth"]) if mfi is not None else None
        df["mfi"] = mfi if mfi is not None else pd.Series(np.nan, index=df.index)

        atr = ta.atr(df["high"], df["low"], df["close"], length=p["atr_period"])
        df["atr"] = atr if atr is not None else pd.Series(np.nan, index=df.index)

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        closes = df["close"].values
        state = df["state"].values
        decycler = df["decycler"].values
        mfi = df["mfi"].values
        atr = df["atr"].values

        n = len(df)
        signals = np.zeros(n, dtype=int)
        stop_losses = np.zeros(n, dtype=float)
        targets = np.zeros(n, dtype=float)

        in_position = False
        prev_state = 0
        current_stop = 0.0
        current_target = 0.0

        for i in range(n):
            warm = np.isnan(decycler[i]) or np.isnan(mfi[i]) or np.isnan(atr[i]) or (i < p["slope_bars"])
            if warm:
                prev_state = state[i]
                continue

            dec_long = (not p["use_decycler"]) or (closes[i] > decycler[i] and decycler[i] > decycler[i - p["slope_bars"]])
            mfi_long = (not p["use_mfi"]) or mfi[i] > 50
            flip_long = state[i] == 1 and prev_state != 1
            flip_short = state[i] == -1 and prev_state != -1

            if not in_position and flip_long and dec_long and mfi_long:
                signals[i] = 1
                current_stop = closes[i] - p["atr_stop_multiplier"] * atr[i]
                current_target = closes[i] + p["atr_target_multiplier"] * atr[i]
                stop_losses[i] = current_stop
                targets[i] = current_target
                in_position = True
            elif in_position and flip_short:
                signals[i] = -1
                stop_losses[i] = current_stop
                targets[i] = current_target
                in_position = False
                current_stop = 0.0
                current_target = 0.0
            elif in_position:
                stop_losses[i] = current_stop
                targets[i] = current_target

            prev_state = state[i]

        df["signal"] = signals
        df["stop_loss"] = stop_losses
        df["target"] = targets
        return df
