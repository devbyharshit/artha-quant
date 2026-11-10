"""Value Area Trend Strategy — ported from the open-source TradingView Pine
Script v6 "Value Trend Strategy" (VTS) by blitz_algo. Source saved at
docs/pine_sources/aurora_value_trend.pine (listed on TradingView under the
title "Aurora Value Trend | Value Area Retest Strategy"). Long-only side
only — this port drops the short side, matching every other strategy in
this system.

Mechanics kept from the source (fully daily-bar-native: "last week" is
computed from daily bars grouped by ISO calendar week, no intraday session
logic anywhere in the original):
1. Weekly value area: at the start of each new ISO week, build a volume
   histogram (48 rows) over the *previous* completed week's daily
   high/low/volume, find the point of control (POC, the highest-volume
   row) and expand outward until 70% of the week's volume is enclosed —
   that range's edges are the value area high/low (VAH/VAL). This value
   area is held constant through the following week, exactly like the
   source (recomputed only once per week, from the prior week's bars).
2. Bilateral-filter trend: an edge-preserving smoother on close (weight =
   a Gaussian time-decay times a Gaussian value-distance-from-the-median
   term, scaled by 1.5x ATR), with a sticky trend state that flips when
   the filter moves `trend_reversal` * sigma away from the running
   peak/trough.
3. Flow: fast/slow ATR-normalized EMA-of-deviation terms, blended into a
   single -1..+1 `hue`; a long needs `hue > 0`.
4. Long entry: trend is up, hue > 0, AND price retests the value area
   high from below within `retest_tolerance_atr` * ATR and closes back
   above it on a green candle ("retest and hold" at the level, not a
   breakout).
5. Stop/target, fixed at entry: stop = entry_close - `stop_atr_multiplier`
   * ATR; target = entry + `target_r` * risk.
6. Exit: the trend flips down (mirrors the source's "trend flip" close),
   or the stop/target bracket, whichever comes first.

Dropped: the source's `direction` input (Both/Long only/Short only) — this
system is long-only by convention, so it is equivalent to "Long only" and
not exposed as a parameter.
"""
from typing import Dict, Any, Optional, Tuple
import numpy as np
import pandas as pd
import pandas_ta as ta

from strategies.base_strategy import BaseStrategy

_VALUE_AREA_ROWS = 48
_VALUE_AREA_FRACTION = 0.70


def _weekly_profile(highs, lows, vols) -> Tuple[float, float, float]:
    """Volume-weighted value area (VAH, VAL, POC) for one week's daily bars,
    ported 1:1 from the Pine `f_profile()` function."""
    hi_p = max(highs)
    lo_p = min(lows)
    if hi_p <= lo_p:
        return np.nan, np.nan, np.nan

    row_size = (hi_p - lo_p) / _VALUE_AREA_ROWS
    totals = np.zeros(_VALUE_AREA_ROWS)
    for h, l, v in zip(highs, lows, vols):
        r0 = max(0, min(_VALUE_AREA_ROWS - 1, int((l - lo_p) / row_size)))
        r1 = max(r0, min(_VALUE_AREA_ROWS - 1, int((h - lo_p) / row_size)))
        if h > l:
            for r in range(r0, r1 + 1):
                bot = lo_p + r * row_size
                overlap = min(h, bot + row_size) - max(l, bot)
                if overlap > 0:
                    totals[r] += v * overlap / (h - l)
        else:
            totals[r0] += v

    total = totals.sum()
    if total <= 0:
        return np.nan, np.nan, np.nan

    poc_idx = int(np.argmax(totals))
    lo, hi = poc_idx, poc_idx
    acc = totals[poc_idx]
    target_acc = total * _VALUE_AREA_FRACTION
    while acc < target_acc and (lo > 0 or hi < _VALUE_AREA_ROWS - 1):
        up_v = totals[hi + 1] if hi < _VALUE_AREA_ROWS - 1 else -1.0
        dn_v = totals[lo - 1] if lo > 0 else -1.0
        if up_v >= dn_v:
            hi += 1
            acc += up_v
        else:
            lo -= 1
            acc += dn_v

    vah = lo_p + (hi + 1) * row_size
    val = lo_p + lo * row_size
    poc = lo_p + (poc_idx + 0.5) * row_size
    return vah, val, poc


class ValueAreaTrendStrategy(BaseStrategy):
    """Weekly value-area retest in a bilateral-filter trend, long-only,
    fixed ATR stop/target set at entry."""

    DEFAULT_PARAMS = {
        "atr_period": 14,
        "trend_window": 40,
        "trend_reversal": 1.0,
        "stop_atr_multiplier": 1.5,
        "target_r": 2.0,
        "retest_tolerance_atr": 0.25,
    }

    def __init__(self, name: str = "ValueAreaTrend_v1", params: Optional[Dict[str, Any]] = None):
        merged_params = self.DEFAULT_PARAMS.copy()
        if params:
            merged_params.update(params)
        super().__init__(name=name, params=merged_params)

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params

        atr = ta.atr(df["high"], df["low"], df["close"], length=p["atr_period"])
        df["atr"] = atr if atr is not None else pd.Series(np.nan, index=df.index)

        iso_week = df.index.to_series().dt.isocalendar().week.to_numpy()
        iso_year = df.index.to_series().dt.isocalendar().year.to_numpy()
        new_week = np.zeros(len(df), dtype=bool)
        new_week[1:] = (iso_week[1:] != iso_week[:-1]) | (iso_year[1:] != iso_year[:-1])
        df["_new_week"] = new_week

        aatr = df["atr"].clip(lower=1e-6)
        ema9 = ta.ema(df["close"], length=9)
        ema34 = ta.ema(df["close"], length=34)
        diff_fast = (df["close"] - ema9) / aatr if ema9 is not None else pd.Series(np.nan, index=df.index)
        diff_slow = (df["close"] - ema34) / aatr if ema34 is not None else pd.Series(np.nan, index=df.index)
        flow_fast = ta.ema(diff_fast, length=3)
        flow_slow = ta.ema(diff_slow, length=5)
        df["flow_fast"] = flow_fast if flow_fast is not None else pd.Series(np.nan, index=df.index)
        df["flow_slow"] = flow_slow if flow_slow is not None else pd.Series(np.nan, index=df.index)

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        n = len(df)
        close = df["close"].to_numpy(dtype=float)
        open_ = df["open"].to_numpy(dtype=float)
        high = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        volume = df["volume"].to_numpy(dtype=float)
        atr = df["atr"].to_numpy(dtype=float)
        new_week = df["_new_week"].to_numpy(dtype=bool)
        flow_fast = df["flow_fast"].to_numpy(dtype=float)
        flow_slow = df["flow_slow"].to_numpy(dtype=float)

        signals = np.zeros(n, dtype=int)
        stop_losses = np.zeros(n, dtype=float)
        targets = np.zeros(n, dtype=float)

        wk_high, wk_low, wk_vol = [], [], []
        w_vah, w_val = np.nan, np.nan

        trend = 0
        peak, trough = np.nan, np.nan
        bf_len = p["trend_window"]

        in_position = False
        current_stop = 0.0
        current_target = 0.0

        for i in range(n):
            if new_week[i] and wk_high:
                vah, val, _poc = _weekly_profile(wk_high, wk_low, wk_vol)
                if not np.isnan(vah):
                    w_vah, w_val = vah, val
                wk_high, wk_low, wk_vol = [], [], []
            wk_high.append(high[i])
            wk_low.append(low[i])
            wk_vol.append(volume[i])

            if np.isnan(atr[i]) or np.isnan(flow_fast[i]) or np.isnan(flow_slow[i]):
                continue

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

            sigma = 1.5 * atr[i]
            prev1 = close[i - 1] if i >= 1 else close[i]
            prev2 = close[i - 2] if i >= 2 else prev1
            ref = max(min(close[i], prev1), min(max(close[i], prev1), prev2))

            w_sum = v_sum = 0.0
            lookback = min(bf_len, i + 1)
            if sigma > 0:
                for j in range(lookback):
                    smp = close[i - j]
                    tw = np.exp(-0.5 * j * j / 100.0)
                    dist = (smp - ref) / sigma
                    wt = tw * np.exp(-0.5 * dist * dist)
                    w_sum += wt
                    v_sum += wt * smp
            filt = v_sum / w_sum if w_sum > 0 else np.nan
            rev_dist = p["trend_reversal"] * sigma

            if not np.isnan(filt):
                if trend == 0:
                    peak = filt if np.isnan(peak) else max(peak, filt)
                    trough = filt if np.isnan(trough) else min(trough, filt)
                    if filt >= trough + rev_dist:
                        trend, peak = 1, filt
                    elif filt <= peak - rev_dist:
                        trend, trough = -1, filt
                elif trend == 1:
                    peak = max(peak, filt)
                    if filt <= peak - rev_dist:
                        trend, trough = -1, filt
                else:
                    trough = min(trough, filt)
                    if filt >= trough + rev_dist:
                        trend, peak = 1, filt

            if in_position and trend == -1:
                signals[i] = -1
                stop_losses[i] = current_stop
                targets[i] = current_target
                in_position = False
                current_stop = 0.0
                current_target = 0.0
                continue

            hue = max(-1.0, min(1.0, 0.65 * flow_fast[i] / 1.5 + 0.35 * flow_slow[i] / 2.5))
            has_map = not np.isnan(w_vah) and not np.isnan(w_val) and w_vah > w_val
            tol = p["retest_tolerance_atr"] * atr[i]
            long_retest = has_map and low[i] <= w_vah + tol and close[i] > w_vah and close[i] > open_[i]
            long_signal = trend == 1 and hue > 0 and long_retest

            if not in_position and long_signal:
                stop_dist = p["stop_atr_multiplier"] * atr[i]
                current_stop = close[i] - stop_dist
                current_target = close[i] + stop_dist * p["target_r"]
                signals[i] = 1
                stop_losses[i] = current_stop
                targets[i] = current_target
                in_position = True

        df["signal"] = signals
        df["stop_loss"] = stop_losses
        df["target"] = targets
        return df
