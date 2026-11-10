"""Range Filtered Trend Strategy — ported from TradingView Pine Script v6
("Range Filtered Trend Strategy || GPT").

Mechanics:
1. Kalman-style recursive smoother applied to close price.
2. Supertrend regime filter computed on the Kalman line (not hl2), using
   standard ATR for band width.
3. Volatility-scaled range band around the Kalman line (Kalman +/- WMA(200)
   of the high-low range, scaled by a deviation multiplier).
4. Long entry: the Supertrend regime AND the range-band breakout both turn
   bullish on the same bar (the "green" state), edge-triggered.
5. Long exit: the moment that combined bullish state breaks — there is no
   technical stop-loss or target, exits are purely signal-driven, matching
   the source Pine strategy exactly. stop_loss/target are always 0.0.

Because there's no fixed stop distance, backtest this with
BacktestEngine(position_sizing_mode="full_equity") rather than the default
risk-based sizing mode, which requires a real stop distance to size a trade.
"""
from typing import Dict, Any, Optional
import numpy as np
import pandas as pd
import pandas_ta as ta

from strategies.base_strategy import BaseStrategy


class RangeFilteredTrendStrategy(BaseStrategy):
    """Kalman-filtered trend + Supertrend regime combo, long-only, signal-exit."""

    DEFAULT_PARAMS = {
        "kalman_alpha": 0.01,
        "kalman_beta": 0.1,
        "kalman_period": 77,
        "deviation": 1.2,
        "supertrend_factor": 0.7,
        "supertrend_atr_period": 7,
        "vola_period": 200,
    }

    def __init__(self, name: str = "RangeFilteredTrend_v1", params: Optional[Dict[str, Any]] = None):
        merged_params = self.DEFAULT_PARAMS.copy()
        if params:
            merged_params.update(params)
        super().__init__(name=name, params=merged_params)

    @staticmethod
    def _kalman_filter(prices: pd.Series, period: int, alpha: float, beta: float) -> pd.Series:
        """Recursive smoother, ported 1:1 from the Pine `kalman()` function.

        v1 = smoothed estimate, v2 = error covariance, v4 = kalman gain.
        v1[0] is NaN (there is no prior bar to seed from, same as Pine's a[1]
        on the first bar); NaN naturally propagates through the recurrence
        exactly as Pine's na-arithmetic does.
        """
        n = len(prices)
        values = prices.to_numpy(dtype=float)
        out = np.full(n, np.nan)

        v1 = np.nan
        v2 = 1.0
        v3 = alpha * period

        for i in range(n):
            if np.isnan(v1) and i >= 1:
                v1 = values[i - 1]
            v5 = v1
            v4 = v2 / (v2 + v3)
            v1 = v5 + v4 * (values[i] - v5)
            v2 = (1 - v4) * v2 + beta / period
            out[i] = v1

        return pd.Series(out, index=prices.index)

    @staticmethod
    def _supertrend_direction(k: pd.Series, atr: pd.Series, factor: float) -> pd.Series:
        """Supertrend direction on an arbitrary source `k`, ported 1:1 from the
        Pine `pine_supertrend()` function. Returns -1 (bullish, using the
        lower band) or 1 (bearish, using the upper band); NaN during warmup.
        """
        n = len(k)
        k_vals = k.to_numpy(dtype=float)
        atr_vals = atr.to_numpy(dtype=float)

        direction = np.full(n, np.nan)
        prev_upper = np.nan
        prev_lower = np.nan
        prev_super = np.nan

        for i in range(n):
            raw_upper = k_vals[i] + factor * atr_vals[i]
            raw_lower = k_vals[i] - factor * atr_vals[i]

            prev_upper_nz = 0.0 if np.isnan(prev_upper) else prev_upper
            prev_lower_nz = 0.0 if np.isnan(prev_lower) else prev_lower
            k_prev = k_vals[i - 1] if i >= 1 else np.nan

            cur_lower = raw_lower if (raw_lower > prev_lower_nz or k_prev < prev_lower_nz) else prev_lower_nz
            cur_upper = raw_upper if (raw_upper < prev_upper_nz or k_prev > prev_upper_nz) else prev_upper_nz

            atr_prev = atr_vals[i - 1] if i >= 1 else np.nan
            if np.isnan(atr_prev):
                dirn = 1.0
            elif prev_super == prev_upper:
                dirn = -1.0 if k_vals[i] > cur_upper else 1.0
            else:
                dirn = 1.0 if k_vals[i] < cur_lower else -1.0

            st = cur_lower if dirn == -1.0 else cur_upper

            direction[i] = dirn
            prev_upper, prev_lower, prev_super = cur_upper, cur_lower, st

        return pd.Series(direction, index=k.index)

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params

        df["kalman"] = self._kalman_filter(df["close"], p["kalman_period"], p["kalman_alpha"], p["kalman_beta"])

        atr = ta.atr(df["high"], df["low"], df["close"], length=p["supertrend_atr_period"])
        df["atr"] = atr if atr is not None else pd.Series(np.nan, index=df.index)

        df["supertrend_direction"] = self._supertrend_direction(df["kalman"], df["atr"], p["supertrend_factor"])

        vola = ta.wma(df["high"] - df["low"], length=p["vola_period"])
        df["vola"] = vola if vola is not None else pd.Series(np.nan, index=df.index)

        df["range_upper"] = df["kalman"] + df["vola"] * p["deviation"]
        df["range_lower"] = df["kalman"] - df["vola"] * p["deviation"]

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        closes = df["close"].values
        kalman = df["kalman"].values
        atr = df["atr"].values
        direction = df["supertrend_direction"].values
        range_upper = df["range_upper"].values
        range_lower = df["range_lower"].values

        n = len(df)
        signals = np.zeros(n, dtype=int)
        stop_losses = np.zeros(n, dtype=float)
        targets = np.zeros(n, dtype=float)

        trend = 0       # sticky range-breakout trend: 1 bullish, -1 bearish, 0 neutral
        ktrend = 0       # sticky Supertrend-derived trend: 1 bullish, -1 bearish
        in_position = False
        was_green = False

        for i in range(n):
            warm = (
                np.isnan(kalman[i]) or np.isnan(atr[i]) or
                np.isnan(direction[i]) or np.isnan(range_upper[i]) or np.isnan(range_lower[i])
            )
            if warm:
                signals[i] = 0
                continue

            if closes[i] > range_upper[i]:
                trend = 1
            elif closes[i] < range_lower[i]:
                trend = -1
            # else: retain previous trend (sticky)

            if direction[i] < 0:
                ktrend = 1
            elif direction[i] > 0:
                ktrend = -1
            # else: retain previous ktrend (sticky; direction is never exactly 0)

            is_green = (ktrend == 1) and (trend != -1)

            if not in_position and is_green and not was_green:
                signals[i] = 1
                in_position = True
            elif in_position and not is_green:
                signals[i] = -1
                in_position = False
            else:
                signals[i] = 0

            was_green = is_green

        df["signal"] = signals
        df["stop_loss"] = stop_losses
        df["target"] = targets
        return df
