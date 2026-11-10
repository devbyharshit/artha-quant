"""Backtest-time data fixes the broker's history doesn't apply.

Kite's daily history is adjusted for splits and bonuses but not for
demergers, and it contains bad opening prints. These fixes are applied when
backtests load data; the Parquet store keeps the raw download.

- Demergers: bars before the ex-date are scaled by (ex-date open / previous
  close), so the value that moved into the new company doesn't look like a
  crash, while the ex-date's own open-to-close move stays real.
- Symbols with several demergers start after the last one instead of
  stacking estimated adjustments.
- Bad opens: a stock that opens >8% away from the previous close but closes
  within 3% of it, on a day when the market as a whole didn't gap, gets its
  open (and the low it dragged down) reset to the previous close.
- ETFs, gold and liquid funds are excluded from strategy universes; NIFTYBEES
  stays available as the benchmark.
"""
from typing import Dict, Iterable, Set

import pandas as pd

NON_EQUITY = {"NIFTYBEES", "JUNIORBEES", "GOLDBEES", "GOLDCASE", "LIQUIDBEES", "LIQUIDCASE", "MAFANG"}

# symbol -> ex-date (verified against exchange-reported record dates)
DEMERGERS = {
    "GRASIM": ["2017-07-19"],   # Aditya Birla Capital
    "TMPV": ["2025-10-14"],     # Tata Motors commercial vehicles (TMLCV)
    "VEDL": ["2026-04-30"],     # Vedanta split into separate businesses
}

# symbol -> first usable date (after its last demerger)
START_AFTER = {
    "ADANIENT": "2018-09-06",   # demergers on 2015-06-03, 2018-04-05 (Adani Green), 2018-09-06 (Adani Gas)
}

BAD_OPEN_GAP = 0.08
BAD_OPEN_CLOSE_TOLERANCE = 0.03
MARKET_GAP = 0.05
MARKET_GAP_MIN_SYMBOLS = 5


def _ts(df: pd.DataFrame, date: str) -> pd.Timestamp:
    return pd.Timestamp(date, tz=df.index.tz)


def apply_start_after(symbol: str, df: pd.DataFrame) -> pd.DataFrame:
    if symbol in START_AFTER and not df.empty:
        return df.loc[_ts(df, START_AFTER[symbol]):]
    return df


def adjust_demergers(symbol: str, df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for ex in DEMERGERS.get(symbol, []):
        ex_ts = _ts(df, ex)
        if ex_ts not in df.index:
            continue
        i = df.index.get_loc(ex_ts)
        if i == 0:
            continue
        factor = df["open"].iloc[i] / df["close"].iloc[i - 1]
        before = df.index < ex_ts
        df.loc[before, ["open", "high", "low", "close"]] *= factor
    return df


def market_gap_days(data: Dict[str, pd.DataFrame]) -> Set[pd.Timestamp]:
    """Dates when at least MARKET_GAP_MIN_SYMBOLS stocks opened >MARKET_GAP away from their previous close."""
    counts: Dict[pd.Timestamp, int] = {}
    for df in data.values():
        gap = (df["open"] / df["close"].shift(1) - 1).abs()
        for ts in gap.index[gap > MARKET_GAP]:
            counts[ts.normalize()] = counts.get(ts.normalize(), 0) + 1
    return {ts for ts, n in counts.items() if n >= MARKET_GAP_MIN_SYMBOLS}


def fix_bad_opens(df: pd.DataFrame, market_days: Iterable[pd.Timestamp]) -> pd.DataFrame:
    df = df.copy()
    prev_close = df["close"].shift(1)
    bad = (
        ((df["open"] / prev_close - 1).abs() > BAD_OPEN_GAP)
        & ((df["close"] / prev_close - 1).abs() < BAD_OPEN_CLOSE_TOLERANCE)
        & ~df.index.normalize().isin(list(market_days))
    )
    # The bad print also set the day's low (or high); the true extreme is
    # unknown, so fall back to the nearest real prices: the fixed open and the close.
    down = bad & (df["open"] < prev_close)
    up = bad & (df["open"] > prev_close)
    df.loc[bad, "open"] = prev_close[bad]
    df.loc[down, "low"] = df.loc[down, ["open", "close"]].min(axis=1)
    df.loc[up, "high"] = df.loc[up, ["open", "close"]].max(axis=1)
    return df


def prepare(raw: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """Applies every fix to a dict of raw symbol frames. Market-wide gap days are
    detected across all equity symbols passed in, so pass the full universe."""
    staged = {s: adjust_demergers(s, apply_start_after(s, df)) for s, df in raw.items() if not df.empty}
    days = market_gap_days({s: df for s, df in staged.items() if s not in NON_EQUITY})
    return {s: fix_bad_opens(df, days) for s, df in staged.items() if not df.empty}
