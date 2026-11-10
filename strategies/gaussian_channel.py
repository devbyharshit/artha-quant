"""Gaussian Channel Strategy — ported from TradingView Pine Script v5
("Gaussian Channel Strategy || GPT o1").

Mechanics:
1. An N-pole recursive Gaussian/Ehlers filter (default N=7) applied to
   hlc3 price -> the channel center line `filt`.
2. The same N-pole filter applied to True Range -> the channel width
   `filt_tr`, scaled by a multiplier to form hband/lband around `filt`.
3. A Stochastic RSI (raw %K, smoothed) used as a momentum-extreme gate.
4. Long entry: the channel is rising (filt > filt[1]), close is above the
   upper band, AND Stoch RSI %K is in an extreme zone (>80 or <20).
5. Long exit: close crosses back down under the upper band.

Like range_filtered_trend, this has NO technical stop-loss or target —
exits are purely signal-driven, matching the source Pine strategy's
percent_of_equity=100 sizing with no fixed stop. stop_loss/target are
always 0.0. Backtest with BacktestEngine(position_sizing_mode="full_equity").
"""
import math
from typing import Dict, Any, Optional
import numpy as np
import pandas as pd
import pandas_ta as ta

from strategies.base_strategy import BaseStrategy


class GaussianChannelStrategy(BaseStrategy):
    """N-pole Gaussian channel breakout gated by a Stochastic RSI extreme, long-only, signal-exit."""

    DEFAULT_PARAMS = {
        "poles": 7,
        "sampling_period": 148,
        "tr_multiplier": 1.43,
        "rsi_length": 14,
        "stoch_length": 14,
        "smooth_k": 3,
        "stoch_upper": 80.0,
        "stoch_lower": 20.0,
    }

    def __init__(self, name: str = "GaussianChannel_v1", params: Optional[Dict[str, Any]] = None):
        merged_params = self.DEFAULT_PARAMS.copy()
        if params:
            merged_params.update(params)
        super().__init__(name=name, params=merged_params)

    @staticmethod
    def _gaussian_alpha(sampling_period: int, poles: int) -> float:
        """Ported 1:1 from the Pine `beta`/`alpha` calculation (the 1.414
        literal is the Pine author's own hardcoded approximation of sqrt(2),
        kept as-is rather than "corrected" to 2**0.5)."""
        beta = (1 - math.cos(2 * math.pi / sampling_period)) / (1.414 ** (2.0 / poles) - 1)
        return -beta + math.sqrt(beta ** 2 + 2 * beta)

    @staticmethod
    def _n_pole_filter(series: pd.Series, alpha: float, poles: int) -> pd.Series:
        """Direct-form N-pole recursive filter, ported 1:1 from the Pine
        `f_filt9x()` binomial expansion: f[t] = alpha^N*s[t] + sum over
        k=1..N of (-1)^(k+1) * C(N,k) * (1-alpha)^k * f[t-k], with missing
        history (t-k < 0) treated as 0 (Pine's nz() fallback). Equivalent to
        cascading N one-pole low-pass filters, expressed in closed form.
        """
        n = len(series)
        vals = series.to_numpy(dtype=float)
        out = np.zeros(n)

        x = 1 - alpha
        a_pow_n = alpha ** poles
        coeffs = [((-1) ** (k + 1)) * math.comb(poles, k) * (x ** k) for k in range(1, poles + 1)]

        for t in range(n):
            value = a_pow_n * vals[t]
            for k in range(1, poles + 1):
                lag_idx = t - k
                f_lag = out[lag_idx] if lag_idx >= 0 else 0.0
                value += coeffs[k - 1] * f_lag
            out[t] = value

        return pd.Series(out, index=series.index)

    @staticmethod
    def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
        """Ported from Pine's `ta.tr(true)`: on the first bar (no prior
        close), falls back to plain high-low rather than NaN."""
        prev_close = close.shift(1)
        ranges = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1)
        return ranges.max(axis=1)

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        alpha = self._gaussian_alpha(p["sampling_period"], p["poles"])

        hlc3 = (df["high"] + df["low"] + df["close"]) / 3.0
        df["gc_filt"] = self._n_pole_filter(hlc3, alpha, p["poles"])

        tr = self._true_range(df["high"], df["low"], df["close"])
        df["gc_filt_tr"] = self._n_pole_filter(tr, alpha, p["poles"])

        df["gc_hband"] = df["gc_filt"] + df["gc_filt_tr"] * p["tr_multiplier"]
        df["gc_lband"] = df["gc_filt"] - df["gc_filt_tr"] * p["tr_multiplier"]

        rsi = ta.rsi(df["close"], length=p["rsi_length"])
        df["rsi"] = rsi if rsi is not None else pd.Series(np.nan, index=df.index)

        roll_min = df["rsi"].rolling(window=p["stoch_length"]).min()
        roll_max = df["rsi"].rolling(window=p["stoch_length"]).max()
        raw_k = 100.0 * (df["rsi"] - roll_min) / (roll_max - roll_min)
        df["stoch_k"] = raw_k.rolling(window=p["smooth_k"]).mean()

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        closes = df["close"].values
        filt = df["gc_filt"].values
        hband = df["gc_hband"].values
        stoch_k = df["stoch_k"].values

        n = len(df)
        signals = np.zeros(n, dtype=int)
        stop_losses = np.zeros(n, dtype=float)
        targets = np.zeros(n, dtype=float)

        in_position = False

        for i in range(1, n):
            # NaN comparisons evaluate False (Python/IEEE-754), same
            # fall-through-to-false behavior Pine's na comparisons have —
            # so stoch_extreme naturally stays False through RSI/Stoch
            # warmup without needing an explicit warmup guard.
            is_rising = filt[i] > filt[i - 1]
            above_hband = closes[i] > hband[i]
            stoch_extreme = (stoch_k[i] > p["stoch_upper"]) or (stoch_k[i] < p["stoch_lower"])
            long_condition = is_rising and above_hband and stoch_extreme

            crossunder = (closes[i - 1] >= hband[i - 1]) and (closes[i] < hband[i])

            if not in_position and long_condition:
                signals[i] = 1
                in_position = True
            elif in_position and crossunder:
                signals[i] = -1
                in_position = False
            else:
                signals[i] = 0

        df["signal"] = signals
        df["stop_loss"] = stop_losses
        df["target"] = targets
        return df
