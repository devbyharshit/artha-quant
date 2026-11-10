"""EMA/VWAP Trend-Cross Strategy — ported from the published Pine Script v6
source "9EMA + VWAP x 20EMA (Daily)" by jaspreet1998 (TradingView strategy
page: "EMA (9,20) & VWAP [stock screener]").

Mechanics (verified against the actual Pine source, not just the listing
page's prose, which described a 1%-risk position sizer that is NOT in the
actual code):
1. 9 EMA and 20 EMA on close.
2. A rolling N-bar VWAP: `sum(hlc3 * volume, N) / sum(volume, N)`. Not a
   session VWAP — the source script's own tooltip notes a session VWAP
   resets every bar on a daily chart and is meaningless there.
3. Long entry: on the bar where ALL THREE first become true simultaneously
   (edge-triggered off the previous bar's state, i.e. `buyState and not
   buyState[1]` — not re-fired every bar they stay true) —
       - 9 EMA > 20 EMA
       - rolling VWAP > 20 EMA
       - close > 20 EMA
4. Long exit: the mirrored bearish alignment first becomes true —
       - 9 EMA < 20 EMA
       - rolling VWAP < 20 EMA
       - close < 20 EMA
   The source script defaults to treating this as a reversal into a short
   (`allowShort = true`). This port is long-only: BaseStrategy's signal
   contract only has {1: BUY, -1: EXIT, 0: HOLD}, every other strategy in
   this system is long-only, and NSE cash equities can't carry a naked
   overnight short anyway — so the bearish alignment is used purely as the
   exit for the long, matching the source's own `allowShort = false`
   branch (`strategy.close("Long")`).
5. Stop-loss, fixed at the entry bar (does not trail), chosen via
   `stop_loss_mode`:
       - "atr" (default): entry close - atr_stop_multiplier * ATR(14)
       - "candle": the entry candle's low
6. No take-profit in the source script — exits are either the stop or the
   bearish-alignment signal. target is always 0.0.
7. Sizing: the source strategy() declaration uses
   default_qty_type=percent_of_equity, default_qty_value=100 — i.e. 100% of
   equity per trade, not a risk-based quantity calc. Register/run this with
   BacktestEngine(position_sizing_mode="full_equity") to match the source's
   actual behavior; the fixed stop still applies and can end a trade early,
   it just isn't used to size the position the way trend_breakout's or
   mean_reversion's risk_based stop is. "risk_based" also works (there is a
   real stop distance), it just isn't what the original backtest did.
"""
from typing import Dict, Any, Optional
import numpy as np
import pandas as pd
import pandas_ta as ta

from strategies.base_strategy import BaseStrategy


class EmaVwapCrossStrategy(BaseStrategy):
    """9/20 EMA + rolling VWAP trend-alignment strategy, long-only,
    edge-triggered entries and exits, fixed ATR or candle-low stop."""

    DEFAULT_PARAMS = {
        "fast_ema_period": 9,
        "slow_ema_period": 20,
        "vwap_period": 20,
        "atr_period": 14,
        "atr_stop_multiplier": 2.0,
        "stop_loss_mode": "atr",  # "atr" or "candle"
    }

    def __init__(self, name: str = "EmaVwapCross_v1", params: Optional[Dict[str, Any]] = None):
        merged_params = self.DEFAULT_PARAMS.copy()
        if params:
            merged_params.update(params)
        if merged_params["stop_loss_mode"] not in ("atr", "candle"):
            raise ValueError(f"Unknown stop_loss_mode: {merged_params['stop_loss_mode']!r}")
        super().__init__(name=name, params=merged_params)

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params

        ema_fast = ta.ema(df["close"], length=p["fast_ema_period"])
        df["ema_fast"] = ema_fast if ema_fast is not None else pd.Series(np.nan, index=df.index)

        ema_slow = ta.ema(df["close"], length=p["slow_ema_period"])
        df["ema_slow"] = ema_slow if ema_slow is not None else pd.Series(np.nan, index=df.index)

        # Rolling N-bar VWAP: a session VWAP would reset every bar on a
        # daily series, so this mirrors the source script's rolling window.
        typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
        pv = typical_price * df["volume"]
        rolling_pv = pv.rolling(window=p["vwap_period"]).sum()
        rolling_vol = df["volume"].rolling(window=p["vwap_period"]).sum()
        df["rolling_vwap"] = rolling_pv / rolling_vol.replace(0, np.nan)

        atr_series = ta.atr(df["high"], df["low"], df["close"], length=p["atr_period"])
        df["atr"] = atr_series if atr_series is not None else pd.Series(np.nan, index=df.index)

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        closes = df["close"].values
        lows = df["low"].values
        ema_fast = df["ema_fast"].values
        ema_slow = df["ema_slow"].values
        vwap = df["rolling_vwap"].values
        atr = df["atr"].values

        n = len(df)
        signals = np.zeros(n, dtype=int)
        stop_losses = np.zeros(n, dtype=float)
        targets = np.zeros(n, dtype=float)

        in_position = False
        prev_bullish = False
        prev_bearish = False

        for i in range(n):
            warm = (
                np.isnan(ema_fast[i]) or np.isnan(ema_slow[i]) or
                np.isnan(vwap[i]) or np.isnan(atr[i])
            )
            if warm:
                # NaN comparisons fall through to False, same as Pine's na
                # handling, so bullish/bearish naturally stay False through
                # warmup without an explicit guard beyond skipping the signal.
                signals[i] = 0
                continue

            bullish_aligned = (
                ema_fast[i] > ema_slow[i] and
                vwap[i] > ema_slow[i] and
                closes[i] > ema_slow[i]
            )
            bearish_aligned = (
                ema_fast[i] < ema_slow[i] and
                vwap[i] < ema_slow[i] and
                closes[i] < ema_slow[i]
            )

            if not in_position:
                if bullish_aligned and not prev_bullish:
                    signals[i] = 1
                    if p["stop_loss_mode"] == "atr":
                        stop_losses[i] = round(closes[i] - p["atr_stop_multiplier"] * atr[i], 2)
                    else:
                        stop_losses[i] = round(lows[i], 2)
                    targets[i] = 0.0
                    in_position = True
            else:
                if bearish_aligned and not prev_bearish:
                    signals[i] = -1
                    in_position = False

            prev_bullish = bullish_aligned
            prev_bearish = bearish_aligned

        df["signal"] = signals
        df["stop_loss"] = stop_losses
        df["target"] = targets
        return df
