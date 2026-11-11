"""Builds the futures-overlay execution table (docs/12-futures-trading-plan.md §4).

Indicator/signal computation stays on cash-equity history, completely
unchanged (docs/12 §3.1) -- this module only supplies the execution-side
OHLC for each cash-history date, sourced from whichever NFO contract is
"front-month-active" that day under the rollover rule.

**Data source (updated 2026-11-10, docs/12 §5):** real historical prices
come from NSE's own public F&O bhavcopy archives
(`data.loaders.nse_bhavcopy.get_historical_futures_bars`), not Kite --
Kite's live NFO catalog only ever exposes ~3 currently-listed contracts per
symbol (confirmed, see `futures_instruments.py`'s module docstring), nowhere
near enough history to walk-forward validate anything. `get_historical_futures_bars`
returns one row per live contract per day for the whole requested window in
one call (it caches per trading day internally), which this module reshapes
in-memory into the `List[FuturesContract]`-like candidates
`select_active_contract` expects for each date, then applies the existing
rollover rule unchanged.

**Lot-size approximation:** NSE's bhavcopy only carries the *real,
contract-specific* historical lot size from 2024-07-08 onward (its current
UDiFF format); every date before that has no lot-size column in NSE's own
archive at all (`nse_bhavcopy.py`'s "legacy" format), not just here. For
those earlier dates this module falls back to the **current** lot size for
the symbol, resolved via `futures_instruments.get_futures_contracts` (the
same "current lot size applied retroactively" approximation
`futures_instruments.py` already uses for its own, now-superseded,
Kite-only contract calendar -- reused rather than reimplemented). This is a
documented approximation, not a real historical value, for any pre-2024-07
row.

**Kite's role now:** Kite is used *only* to resolve the above lot-size
fallback (via `get_futures_contracts`, which calls Kite's live NFO catalog
for the current lot size when a `kite` client is supplied; with `kite=None`
it falls back further to `futures_instruments.DEFAULT_LOT_SIZE_FALLBACK`).
Kite is **no longer used to source any OHLC price** -- empirically (see
docs/12 §5's 2026-11-10 follow-up), NSE's bhavcopy alone covers the full
cash-equity history (2011-present) including very recent dates (there is a
1-trading-day publication lag: "today's" bhavcopy isn't up yet on the same
day, which just means that date is skipped like any other day with no
contract data -- not an error).
"""
from datetime import date as _date
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from kiteconnect import KiteConnect

from configs.logger import logger
from data.loaders.futures_instruments import (
    DEFAULT_LOT_SIZE_FALLBACK,
    FuturesContract,
    get_futures_contracts,
)
from data.loaders.nse_bhavcopy import get_historical_futures_bars

OVERLAY_COLUMNS = ["fut_open", "fut_high", "fut_low", "fut_close", "lot_size", "expiry", "tradingsymbol"]


def select_active_contract(
    as_of: pd.Timestamp,
    contracts: List[FuturesContract],
    rollover_buffer_days: int = 3,
) -> Optional[FuturesContract]:
    """
    Picks the "active" (front-month) contract for `as_of` per the rollover
    rule (docs/12 §3.1): the nearest-expiry contract whose expiry is still
    more than `rollover_buffer_days` *trading* days away; once the nearest
    contract's expiry is within that buffer (inclusive of the boundary day
    itself), the active contract becomes the next one in sequence.

    Returns None if no contract in `contracts` has an expiry on or after
    `as_of` (i.e. futures don't exist yet / have stopped existing for this
    symbol at this date) -- callers should skip the date, not treat it as
    an error.
    """
    as_of = pd.Timestamp(as_of)
    if as_of.tzinfo is not None:
        as_of = as_of.tz_localize(None)
    as_of = as_of.normalize()
    candidates = sorted(
        (c for c in contracts if c.expiry.normalize() >= as_of),
        key=lambda c: c.expiry,
    )
    if not candidates:
        return None

    for contract in candidates:
        trading_days_to_expiry = int(np.busday_count(as_of.date(), contract.expiry.normalize().date()))
        if trading_days_to_expiry > rollover_buffer_days:
            return contract

    # Every remaining candidate (including the furthest-dated one) is already
    # inside the rollover buffer -- nothing better to roll into, so hold the
    # furthest one available.
    return candidates[-1]


def _resolve_current_lot_size(symbol: str, kite: Optional[KiteConnect]) -> int:
    """Resolves "the current lot size" for `symbol`, reusing
    `futures_instruments.get_futures_contracts`'s own existing resolution path
    (live Kite NFO catalog when reachable, else `DEFAULT_LOT_SIZE_FALLBACK`)
    rather than reimplementing it here. Used only to fill the lot-size gap in
    bhavcopy rows older than 2024-07-08 (see module docstring)."""
    today = pd.Timestamp(_date.today())
    try:
        contracts = get_futures_contracts(symbol, today, today, kite=kite)
    except Exception as exc:
        logger.warning(f"Could not resolve a current lot size for {symbol} ({exc}); using fallback.")
        return DEFAULT_LOT_SIZE_FALLBACK
    if contracts:
        return contracts[0].lot_size
    return DEFAULT_LOT_SIZE_FALLBACK


def _bars_by_date(bhav: pd.DataFrame, symbol: str, current_lot_size: int) -> Dict[pd.Timestamp, Dict[str, dict]]:
    """Reshapes `get_historical_futures_bars`'s per-contract-per-day rows into
    {date: {tradingsymbol: {"contract": FuturesContract, "open"/"high"/"low"/"close": float}}}
    -- the per-day candidate list `select_active_contract` expects, plus the
    OHLC each candidate actually carries that day."""
    by_date: Dict[pd.Timestamp, Dict[str, dict]] = {}
    for ts, group in bhav.groupby(bhav.index):
        day_map: Dict[str, dict] = {}
        for _, r in group.iterrows():
            lot_raw = r["lot_size"]
            lot_size = int(lot_raw) if pd.notna(lot_raw) else current_lot_size
            contract = FuturesContract(
                tradingsymbol=r["tradingsymbol"],
                instrument_token=None,
                lot_size=lot_size,
                expiry=pd.Timestamp(r["expiry"]),
                name=symbol.upper(),
            )
            day_map[r["tradingsymbol"]] = {
                "contract": contract,
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
            }
        by_date[pd.Timestamp(ts)] = day_map
    return by_date


def build_futures_overlay(symbol: str, cash_index: pd.DatetimeIndex, kite: Optional[KiteConnect] = None) -> pd.DataFrame:
    """
    Builds the futures-overlay table described in docs/12-futures-trading-plan.md
    §4: a DataFrame indexed by date (a subset of `cash_index` -- dates before
    futures existed for `symbol`, dates NSE's bhavcopy has no data for (e.g.
    the most recent 1-trading-day publication lag), are simply absent, not an
    error) with columns `fut_open, fut_high, fut_low, fut_close, lot_size,
    expiry, tradingsymbol`.

    Prices come from NSE's own historical F&O bhavcopy archives
    (`nse_bhavcopy.get_historical_futures_bars`), fetched once per symbol for
    the whole `cash_index` span -- not Kite, which only exposes a handful of
    currently-listed contracts (see module docstring). The rollover rule
    (`select_active_contract`, unchanged) is then applied in-memory across
    each day's live contracts to pick the front-month one.
    """
    if len(cash_index) == 0:
        return pd.DataFrame(columns=OVERLAY_COLUMNS)

    start_date = pd.Timestamp(cash_index.min())
    end_date = pd.Timestamp(cash_index.max())

    bhav = get_historical_futures_bars(symbol, start_date.date(), end_date.date())
    if bhav.empty:
        logger.warning(f"No NSE bhavcopy futures data found for {symbol} in {start_date.date()}..{end_date.date()}.")
        return pd.DataFrame(columns=OVERLAY_COLUMNS)

    current_lot_size = _resolve_current_lot_size(symbol, kite)
    by_date = _bars_by_date(bhav, symbol, current_lot_size)

    rows: Dict[pd.Timestamp, dict] = {}
    for current_date in cash_index:
        ts = pd.Timestamp(current_date)
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        ts = ts.normalize()

        day_map = by_date.get(ts)
        if not day_map:
            continue

        candidates = [v["contract"] for v in day_map.values()]
        active = select_active_contract(ts, candidates)
        if active is None:
            continue

        bar = day_map[active.tradingsymbol]
        rows[ts] = {
            "fut_open": bar["open"],
            "fut_high": bar["high"],
            "fut_low": bar["low"],
            "fut_close": bar["close"],
            "lot_size": active.lot_size,
            "expiry": active.expiry,
            "tradingsymbol": active.tradingsymbol,
        }

    overlay = pd.DataFrame.from_dict(rows, orient="index", columns=OVERLAY_COLUMNS)
    overlay.index.name = cash_index.name or "timestamp"
    overlay.sort_index(inplace=True)
    return overlay
