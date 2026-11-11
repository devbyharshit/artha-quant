"""Pairs-trading signal generation: rolling hedge ratio, spread z-score, and
an entry/exit state machine.

Not a BaseStrategy — a pair's signal depends on two symbols' aligned price
series, which doesn't fit that single-DataFrame contract, so this is its own
small class rather than forced into that interface (see docs/09-pairs-trading-plan.md §4).
"""
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class PairSignalParams:
    hedge_ratio_window: int = 60
    zscore_window: int = 60
    entry_z: float = 2.0
    exit_z: float = 0.0
    stop_z: float = 3.5
    max_holding_days: int = 20


def compute_spread(log_a: pd.Series, log_b: pd.Series, window: int) -> Tuple[pd.Series, pd.Series]:
    """Rolling-OLS hedge ratio (beta of log_a on log_b over a trailing
    `window`-bar lookback ending at the current bar's close) and the
    resulting log-price spread. Uses only data through the current bar's
    close, the same no-lookahead convention as the rest of this system's
    indicators (e.g. `mean_reversion`'s Bollinger/EMA/ATR) — the engine
    fills orders at the *next* bar's open, so nothing here sees the future."""
    cov = log_a.rolling(window).cov(log_b)
    var = log_b.rolling(window).var()
    beta = cov / var
    spread = log_a - beta * log_b
    return beta, spread


def compute_zscore(spread: pd.Series, window: int) -> pd.Series:
    mean = spread.rolling(window).mean()
    std = spread.rolling(window).std()
    return (spread - mean) / std


def generate_positions(zscore: pd.Series, params: PairSignalParams) -> Tuple[pd.Series, pd.Series]:
    """Stateful entry/exit over a z-score series.

    position: 1 = long leg A / short leg B, -1 = short leg A / long leg B, 0 = flat.
    exit_reason: non-empty exactly on the bar a position closes, naming why
    (SIGNAL_EXIT, STOP, or MAX_HOLD).
    """
    z = zscore.to_numpy()
    n = len(z)
    pos = np.zeros(n, dtype=int)
    reason = np.full(n, "", dtype=object)

    current = 0
    held_days = 0
    for i in range(n):
        if np.isnan(z[i]):
            pos[i] = current
            continue

        if current == 0:
            if z[i] <= -params.entry_z:
                current, held_days = 1, 0
            elif z[i] >= params.entry_z:
                current, held_days = -1, 0
        else:
            held_days += 1
            if current == 1 and z[i] >= params.exit_z:
                current, reason[i] = 0, "SIGNAL_EXIT"
            elif current == -1 and z[i] <= params.exit_z:
                current, reason[i] = 0, "SIGNAL_EXIT"
            elif abs(z[i]) >= params.stop_z:
                current, reason[i] = 0, "STOP"
            elif held_days >= params.max_holding_days:
                current, reason[i] = 0, "MAX_HOLD"

        pos[i] = current

    return pd.Series(pos, index=zscore.index), pd.Series(reason, index=zscore.index)


class PairSignalGenerator:
    """Produces the aligned spread/z-score/position series for one pair."""

    def __init__(self, params: Optional[PairSignalParams] = None):
        self.params = params or PairSignalParams()

    def generate(self, df_a: pd.DataFrame, df_b: pd.DataFrame) -> pd.DataFrame:
        a = df_a[["open", "close"]].add_suffix("_a")
        b = df_b[["open", "close"]].add_suffix("_b")
        aligned = a.join(b, how="inner").dropna()

        log_a = np.log(aligned["close_a"])
        log_b = np.log(aligned["close_b"])
        beta, spread = compute_spread(log_a, log_b, self.params.hedge_ratio_window)
        zscore = compute_zscore(spread, self.params.zscore_window)
        position, exit_reason = generate_positions(zscore, self.params)

        out = aligned.copy()
        out["beta"] = beta
        out["spread"] = spread
        out["zscore"] = zscore
        out["position"] = position
        out["exit_reason"] = exit_reason
        return out
