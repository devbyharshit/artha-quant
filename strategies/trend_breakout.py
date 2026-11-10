"""Trend Breakout Strategy with 200 EMA Filter and ATR Trailing Stop.

Strategy Mechanics:
1. Long-term Trend Filter: 200-period Exponential Moving Average (EMA).
   Trades are only taken if Price > 200 EMA.
2. Breakout Channel: 20-period Donchian Upper Band (Highest High of previous 20 bars).
3. Volume Confirmation: Current bar volume > 20-period Volume SMA.
4. Volatility-based Stop-Loss: Entry Price - (2.0 * ATR(14)).
5. Take-Profit Target: Entry Price + (2.5 * Risk Distance) -> Minimum 1:2.5 Risk:Reward.
6. Exit Condition: Close falls below 10-period Donchian Lower Band.
"""
from typing import Dict, Any, Optional
import numpy as np
import pandas as pd
import pandas_ta as ta

from strategies.base_strategy import BaseStrategy


class TrendBreakoutStrategy(BaseStrategy):
    """
    Directional daily trend-following breakout strategy engineered
    for liquid large-cap equities.
    """

    DEFAULT_PARAMS = {
        "donchian_entry_period": 20,
        "donchian_exit_period": 10,
        "ema_trend_period": 200,
        "atr_period": 14,
        "atr_stop_multiplier": 2.0,
        "risk_reward_target": 2.5,
        "volume_ma_period": 20
    }

    def __init__(self, name: str = "TrendBreakout_v1", params: Optional[Dict[str, Any]] = None):
        merged_params = self.DEFAULT_PARAMS.copy()
        if params:
            merged_params.update(params)
        super().__init__(name=name, params=merged_params)

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Appends 200 EMA, Donchian bands, ATR, and Volume SMA."""
        p = self.params

        # 1. 200-period EMA Trend Filter
        ema = ta.ema(df["close"], length=p["ema_trend_period"])
        df["ema_trend"] = ema if ema is not None else pd.Series(np.nan, index=df.index)

        # 2. Donchian Entry Channel (Highest High of previous N bars, shifted by 1)
        # Shift(1) guarantees zero lookahead into current bar
        df["donchian_high"] = df["high"].rolling(window=p["donchian_entry_period"]).max().shift(1)

        # 3. Donchian Exit Channel (Lowest Low of previous M bars, shifted by 1)
        df["donchian_low"] = df["low"].rolling(window=p["donchian_exit_period"]).min().shift(1)

        # 4. Volatility: ATR(14) with Wilder's RMA smoothing
        atr_series = ta.atr(df["high"], df["low"], df["close"], length=p["atr_period"])
        df["atr"] = atr_series if atr_series is not None else pd.Series(np.nan, index=df.index)

        # 5. Volume Confirmation: 20-period Volume Moving Average
        df["volume_ma"] = df["volume"].rolling(window=p["volume_ma_period"]).mean().shift(1)

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Generates Buy (1), Exit (-1), and Hold (0) signals.
        All conditions are evaluated on confirmed closed bars.
        """
        p = self.params
        signals = np.zeros(len(df), dtype=int)
        stop_losses = np.zeros(len(df), dtype=float)
        targets = np.zeros(len(df), dtype=float)

        closes = df["close"].values
        highs = df["high"].values
        volumes = df["volume"].values
        ema_trend = df["ema_trend"].values
        donchian_high = df["donchian_high"].values
        donchian_low = df["donchian_low"].values
        atr = df["atr"].values
        volume_ma = df["volume_ma"].values

        in_position = False
        current_stop = 0.0
        current_target = 0.0

        for i in range(len(df)):
            # Need indicator warm-up period
            if (
                np.isnan(ema_trend[i]) or
                np.isnan(donchian_high[i]) or
                np.isnan(donchian_low[i]) or
                np.isnan(atr[i]) or
                np.isnan(volume_ma[i])
            ):
                signals[i] = 0
                stop_losses[i] = 0.0
                targets[i] = 0.0
                continue

            close_p = closes[i]

            if not in_position:
                # BUY CONDITION:
                # 1. Close breaks above previous 20-day high
                # 2. Close is above 200 EMA (secular uptrend)
                # 3. Volume is above 20-day average
                buy_condition = (
                    close_p > donchian_high[i] and
                    close_p > ema_trend[i] and
                    volumes[i] > volume_ma[i]
                )

                if buy_condition:
                    signals[i] = 1
                    risk_dist = p["atr_stop_multiplier"] * atr[i]
                    current_stop = round(close_p - risk_dist, 2)
                    current_target = round(close_p + (p["risk_reward_target"] * risk_dist), 2)

                    stop_losses[i] = current_stop
                    targets[i] = current_target
                    in_position = True
                else:
                    signals[i] = 0
                    stop_losses[i] = 0.0
                    targets[i] = 0.0

            else:
                # EXIT CONDITION (in position):
                # 1. Close falls below stop loss
                # 2. Close hits or exceeds target
                # 3. Close drops below 10-day trailing Donchian low
                exit_condition = (
                    close_p <= current_stop or
                    close_p >= current_target or
                    close_p < donchian_low[i]
                )

                if exit_condition:
                    signals[i] = -1
                    stop_losses[i] = current_stop
                    targets[i] = current_target
                    in_position = False
                    current_stop = 0.0
                    current_target = 0.0
                else:
                    # Trailing stop update: ratchets stop up if Donchian exit floor rises
                    trailing_floor = round(donchian_low[i], 2)
                    if trailing_floor > current_stop:
                        current_stop = trailing_floor

                    signals[i] = 0
                    stop_losses[i] = current_stop
                    targets[i] = current_target

        df["signal"] = signals
        df["stop_loss"] = stop_losses
        df["target"] = targets
        return df
