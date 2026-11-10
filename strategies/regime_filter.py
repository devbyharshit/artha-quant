"""Market regime filter: blocks new long entries while the market index is
below its long-term moving average. Exits are never blocked.

The filter wraps any strategy. A blocked entry is treated as a trade never
taken: the inner strategy's own state machine still "enters", so for
edge-triggered strategies (e.g. RangeFilteredTrend) the next entry waits for
a fresh signal, exactly as if the filter lived inside the strategy. For
level-triggered strategies a trade blocked by the regime is likewise not
re-attempted until the inner strategy exits and re-signals.

Where the moving average isn't available yet (not enough index history),
entries are allowed, so missing history never masquerades as a filter.
"""
import pandas as pd

from strategies.base_strategy import BaseStrategy


def index_regime(index_df: pd.DataFrame, ma_period: int = 200) -> pd.Series:
    """1.0 when the index closes above its simple moving average, 0.0 below,
    NaN while the average is warming up. Uses only data up to each bar."""
    close = index_df["close"]
    sma = close.rolling(window=ma_period).mean()
    return (close > sma).astype(float).where(sma.notna())


class RegimeFilteredStrategy(BaseStrategy):
    def __init__(self, inner: BaseStrategy, regime: pd.Series):
        super().__init__(name=f"{inner.name}+Regime", params=dict(inner.params))
        self.inner = inner
        self.regime = regime.astype(float).sort_index()

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self.inner.calculate_indicators(df)
        aligned = self.regime.reindex(self.regime.index.union(df.index)).ffill().reindex(df.index)
        df["regime_ok"] = aligned.fillna(1.0) > 0
        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self.inner.generate_signals(df)
        blocked = (df["signal"] == 1) & ~df["regime_ok"]
        df.loc[blocked, ["signal", "stop_loss", "target"]] = [0, 0.0, 0.0]
        return df
