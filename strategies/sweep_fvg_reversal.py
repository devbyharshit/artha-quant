"""Sweep + FVG Reversal Strategy — ported from the open-source TradingView
Pine Script v6 "Sweep + FVG Reversal [Strategy]" by DoruGavrila, MPL-2.0
(an ICT-style liquidity-sweep + fair-value-gap reversal, strategy version
of the author's open-source "Displacement Sweep Reversal with FVG"
indicator). Source saved at docs/pine_sources/sweep_fvg_reversal.pine.
Long-only side only — this port drops the short side, matching every
other strategy in this system.

Mechanics kept from the source (fully daily-bar-native, no intraday
session logic to strip; the source's optional session filter was off by
default and is not ported):
1. Swing pivots: a confirmed pivot low/high needs `pivot_length` bars on
   each side (so, same as Pine's `ta.pivotlow`/`ta.pivothigh`, a pivot is
   only known `pivot_length` bars after it happened).
2. Liquidity sweep of a swing low: price pierces below the last confirmed
   swing low by at least `min_pierce_atr` * ATR, then closes back above
   it within `reclaim_bars` (default 1 — the piercing bar itself must
   close back above). A pierce that goes deeper than `max_pierce_atr` *
   ATR without reclaiming, or isn't reclaimed in time, retires the level
   as a breakout instead of a sweep. Each swing level can only be swept
   once. The reversal structure required is a "lower high" (the last
   confirmed swing high is below the one before it) — the setup is a
   stop-hunt of a low inside a still-bearish-looking structure.
3. Fair value gap (FVG) confirmation: a 3-bar gap where
   `low[bar] > high[bar-2]` by at least `min_fvg_atr` * ATR, found within
   `max_reversal_bars` of the sweep.
4. Reversal confirmation: close crosses back above the swept swing high
   (the "structure break" level) with a significant candle body
   (>= `body_atr_multiple` * ATR) and the FVG already found.

Deliberate simplification from the source (documented, not a 1:1 port):
the source places a resting LIMIT order at the FVG edge after
confirmation, cancelled if not filled within `entry_window` bars or
invalidated by price trading back through the FVG. This engine's fill
model is next-bar-market-open only (see `backtesting/engine.py`), so this
port enters at market on the confirmation bar itself instead of waiting
for a retracement into the FVG — trading the FVG-retest edge for
simplicity and a guaranteed fill. Stop = the sweep's extreme low minus
`stop_buffer_atr` * ATR (the source's stop); target = entry + `target_r` *
risk. `max_stop_atr` (0 disables) skips entries whose stop is unusually
wide, matching the source's "stop too wide" skip reason.
"""
from typing import Dict, Any, Optional
import numpy as np
import pandas as pd
import pandas_ta as ta

from strategies.base_strategy import BaseStrategy


class SweepFvgReversalStrategy(BaseStrategy):
    """Liquidity-sweep-of-a-low + fair-value-gap bullish reversal, long-only,
    fixed ATR-based stop/target set at entry."""

    DEFAULT_PARAMS = {
        "pivot_length": 3,
        "max_reversal_bars": 10,
        "atr_period": 14,
        "body_atr_multiple": 0.8,
        "min_fvg_atr": 0.15,
        "reclaim_bars": 1,
        "min_pierce_atr": 0.05,
        "max_pierce_atr": 1.5,
        "require_reject": True,
        "stop_buffer_atr": 0.1,
        "target_r": 2.0,
        "max_stop_atr": 3.0,
    }

    def __init__(self, name: str = "SweepFvgReversal_v1", params: Optional[Dict[str, Any]] = None):
        merged_params = self.DEFAULT_PARAMS.copy()
        if params:
            merged_params.update(params)
        super().__init__(name=name, params=merged_params)

    @staticmethod
    def _pivot_lows(low: np.ndarray, length: int) -> np.ndarray:
        """1:1 with Pine's `ta.pivotlow(low, length, length)`: non-nan at bar
        i+length, holding low[i], when low[i] is the strict min of the
        2*length+1 window centered on it."""
        n = len(low)
        out = np.full(n, np.nan)
        for i in range(length, n - length):
            window = low[i - length:i + length + 1]
            if low[i] == window.min():
                out[i + length] = low[i]
        return out

    @staticmethod
    def _pivot_highs(high: np.ndarray, length: int) -> np.ndarray:
        n = len(high)
        out = np.full(n, np.nan)
        for i in range(length, n - length):
            window = high[i - length:i + length + 1]
            if high[i] == window.max():
                out[i + length] = high[i]
        return out

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        atr = ta.atr(df["high"], df["low"], df["close"], length=p["atr_period"])
        df["atr"] = atr if atr is not None else pd.Series(np.nan, index=df.index)
        df["pivot_low"] = self._pivot_lows(df["low"].to_numpy(dtype=float), p["pivot_length"])
        df["pivot_high"] = self._pivot_highs(df["high"].to_numpy(dtype=float), p["pivot_length"])
        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        n = len(df)
        high = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        close = df["close"].to_numpy(dtype=float)
        open_ = df["open"].to_numpy(dtype=float)
        atr = df["atr"].to_numpy(dtype=float)
        pivot_low = df["pivot_low"].to_numpy(dtype=float)
        pivot_high = df["pivot_high"].to_numpy(dtype=float)

        signals = np.zeros(n, dtype=int)
        stop_losses = np.zeros(n, dtype=float)
        targets = np.zeros(n, dtype=float)

        last_pivot_low = np.nan
        prior_pivot_low = np.nan
        last_pivot_low_bar = -1
        last_pivot_high = np.nan
        prior_pivot_high = np.nan
        last_pivot_high_bar = -1
        low_swept = False

        lo_pierce_start = -1
        lo_pierce_min = np.nan

        bull_active = False
        bull_sweep_bar = -1
        bull_break_level = np.nan
        bull_extreme = np.nan
        bull_fvg_found = False

        in_position = False
        current_stop = 0.0
        current_target = 0.0

        pivot_len = p["pivot_length"]
        min_pierce_mult = p["min_pierce_atr"]
        max_pierce_mult = p["max_pierce_atr"]

        for i in range(n):
            if i < 2 or np.isnan(atr[i]):
                continue

            # This strategy's only exit is the stop/target bracket (no
            # discretionary flip signal), so `in_position` must be mirrored
            # here from the same stop/target levels the engine checks
            # intrabar — otherwise it would latch "in position" forever
            # after the first trade and never scan for a new setup again.
            if in_position:
                if low[i] <= current_stop or high[i] >= current_target:
                    signals[i] = -1
                    stop_losses[i] = current_stop
                    targets[i] = current_target
                    in_position = False
                    current_stop = 0.0
                    current_target = 0.0
                    continue
                stop_losses[i] = current_stop
                targets[i] = current_target

            if not np.isnan(pivot_high[i]):
                prior_pivot_high = last_pivot_high
                last_pivot_high = pivot_high[i]
                last_pivot_high_bar = i - pivot_len
            if not np.isnan(pivot_low[i]):
                prior_pivot_low = last_pivot_low
                last_pivot_low = pivot_low[i]
                last_pivot_low_bar = i - pivot_len
                low_swept = False
                lo_pierce_start = -1

            min_pierce = min_pierce_mult * atr[i]
            max_pierce = max_pierce_mult * atr[i]

            liq_low_sweep = False
            lo_sweep_ext = np.nan
            if not np.isnan(last_pivot_low) and not low_swept and last_pivot_low_bar < i:
                if lo_pierce_start < 0 and low[i] < last_pivot_low:
                    lo_pierce_start = i
                    lo_pierce_min = low[i]
                elif lo_pierce_start >= 0:
                    lo_pierce_min = min(lo_pierce_min, low[i])

                if lo_pierce_start >= 0:
                    depth = last_pivot_low - lo_pierce_min
                    if close[i] > last_pivot_low:
                        mid = (high[i] + low[i]) / 2.0
                        quality_ok = depth >= min_pierce and (not p["require_reject"] or close[i] > mid)
                        if quality_ok:
                            liq_low_sweep = True
                            lo_sweep_ext = lo_pierce_min
                            low_swept = True
                        lo_pierce_start = -1
                    elif (i - lo_pierce_start + 1) >= p["reclaim_bars"] or (max_pierce_mult > 0 and depth > max_pierce):
                        low_swept = True
                        lo_pierce_start = -1

            bullish_sweep = (
                not np.isnan(last_pivot_low) and not np.isnan(last_pivot_high) and not np.isnan(prior_pivot_high) and
                last_pivot_high < prior_pivot_high and last_pivot_low_bar < i and liq_low_sweep
            )

            if bullish_sweep:
                low_swept = True
                bull_active = True
                bull_sweep_bar = i
                bull_break_level = last_pivot_high
                bull_extreme = lo_sweep_ext
                bull_fvg_found = (low[i] > high[i - 2]) and ((low[i] - high[i - 2]) >= p["min_fvg_atr"] * atr[i])

            bullish_signal = False
            if bull_active and not bullish_sweep:
                bars_from_sweep = i - bull_sweep_bar
                bull_extreme = min(bull_extreme, low[i])
                if (not bull_fvg_found) and bars_from_sweep <= p["max_reversal_bars"]:
                    bull_fvg_found = (low[i] > high[i - 2]) and ((low[i] - high[i - 2]) >= p["min_fvg_atr"] * atr[i])
                if bars_from_sweep > p["max_reversal_bars"]:
                    bull_active = False
                else:
                    candle_body = abs(close[i] - open_[i])
                    sig_bull_body = close[i] > open_[i] and candle_body >= p["body_atr_multiple"] * atr[i]
                    crossed_over = close[i] > bull_break_level and close[i - 1] <= bull_break_level
                    if bars_from_sweep > 0 and crossed_over and sig_bull_body and bull_fvg_found:
                        bullish_signal = True
                        bull_active = False

            if not in_position and bullish_signal:
                stop = bull_extreme - p["stop_buffer_atr"] * atr[i]
                risk = close[i] - stop
                size_ok = p["max_stop_atr"] <= 0 or risk <= p["max_stop_atr"] * atr[i]
                if risk > 0 and size_ok:
                    signals[i] = 1
                    current_stop = stop
                    current_target = close[i] + p["target_r"] * risk
                    stop_losses[i] = current_stop
                    targets[i] = current_target
                    in_position = True

        df["signal"] = signals
        df["stop_loss"] = stop_losses
        df["target"] = targets
        return df
