"""NFO single-stock futures contract resolver (docs/12-futures-trading-plan.md §3.1/§4).

**Live investigation finding (2026-11-10):** `kite.instruments("NFO")` was
verified live against a real Kite session and, for RELIANCE, returned exactly
3 FUT contracts -- the current near/next/far month (NOV/DEC/JAN) -- and
nothing older. This confirms the "known risk" flagged in the design doc:
Kite's instrument catalog only ever exposes currently-listed-and-upcoming
contracts, never a historical record of expired contracts' tokens or
lot sizes. There is no live-data path to recover what contracts existed,
e.g., 2 years ago.

This module therefore builds its contract calendar from an **approximated**
rule rather than pretending a deep historical catalog exists:
- Expiry: NSE monthly F&O contracts expire on the last Thursday of the
  month. This ignores the exchange-holiday shift (the real rule falls back
  to the previous trading day when the last Thursday is a market holiday) --
  a documented, deliberate simplification.
- Lot size: the **current** lot size (from Kite's live catalog, when
  reachable) is applied to every historical contract. Real lot sizes are
  periodically revised by the exchange, so this will not be exact for older
  periods -- also documented and deliberate (see docs/12 §3.2).

When a generated contract happens to still appear in Kite's live NFO catalog
(i.e. it's a currently-listed/near-dated contract), its real
`instrument_token` is filled in from that catalog. For contracts further in
the past there is no way to resolve a token (the contract has already expired
and dropped out of Kite's catalog), so `instrument_token` is left as `None`;
callers that need historical bars for such a contract should expect an
empty/unavailable result rather than treat `None` as an error.
"""
import calendar
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Union

import pandas as pd
from kiteconnect import KiteConnect

from configs.config import get_settings
from configs.logger import logger

DateLike = Union[str, date, datetime, pd.Timestamp]

# Used only when no live NFO catalog is reachable at all (no kite client given,
# or the fetch failed) -- a conservative placeholder, not a real lot size.
DEFAULT_LOT_SIZE_FALLBACK = 1


@dataclass
class FuturesContract:
    tradingsymbol: str
    instrument_token: Optional[int]
    lot_size: int
    expiry: pd.Timestamp
    name: str  # underlying equity symbol, e.g. "RELIANCE"


def get_nfo_instruments_cache_path() -> Path:
    settings = get_settings()
    settings.DATA_STORE_DIR.mkdir(parents=True, exist_ok=True)
    return settings.DATA_STORE_DIR / "instruments_NFO.json"


def fetch_and_cache_nfo_instruments(kite: KiteConnect, force_refresh: bool = False) -> List[dict]:
    """
    Downloads the current NFO instrument catalog from Kite and caches it to
    disk for the day, mirroring `instrument_lookup.fetch_and_cache_instruments`'s
    caching style. Note this catalog only ever holds currently-listed and
    upcoming contracts (see module docstring) -- it is not a historical record.
    """
    cache_path = get_nfo_instruments_cache_path()
    today_str = date.today().isoformat()

    if not force_refresh and cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("cache_date") == today_str and "instruments" in data:
                    logger.debug(f"Loaded NFO instrument catalog from cache ({len(data['instruments'])} rows)")
                    return data["instruments"]
        except Exception as exc:
            logger.warning(f"Failed to read NFO instrument cache: {exc}")

    logger.info("Fetching NFO instrument catalog from Kite API...")
    raw = kite.instruments("NFO")

    serializable = []
    for inst in raw:
        row = dict(inst)
        expiry = row.get("expiry")
        if isinstance(expiry, (date, datetime)):
            row["expiry"] = expiry.isoformat()
        serializable.append(row)

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"cache_date": today_str, "instruments": serializable}, f)

    logger.info(f"Cached {len(serializable)} NFO instrument rows to {cache_path}")
    return serializable


def _live_fut_contracts_for_symbol(kite: Optional[KiteConnect], symbol: str) -> Dict[str, dict]:
    """Returns {tradingsymbol: raw_instrument_dict} for symbol's currently-listed FUT contracts, or {} if unavailable."""
    if kite is None:
        return {}
    try:
        catalog = fetch_and_cache_nfo_instruments(kite)
    except Exception as exc:
        logger.warning(f"Could not fetch live NFO catalog ({exc}); falling back to approximated calendar only.")
        return {}

    out: Dict[str, dict] = {}
    for row in catalog:
        if row.get("name") == symbol.upper() and row.get("instrument_type") == "FUT":
            out[row["tradingsymbol"]] = row
    return out


def _last_thursday(year: int, month: int) -> date:
    """
    Approximates NSE's monthly F&O expiry as the last Thursday of the month.
    Deliberately ignores exchange-holiday shifts -- see module docstring.
    """
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    offset = (d.weekday() - 3) % 7  # Thursday == weekday() 3
    return d - timedelta(days=offset)


def _contract_tradingsymbol(symbol: str, expiry: date) -> str:
    """Builds Kite's NFO futures tradingsymbol convention, e.g. RELIANCE26NOVFUT."""
    yy = expiry.strftime("%y")
    mon = expiry.strftime("%b").upper()
    return f"{symbol.upper()}{yy}{mon}FUT"


def _month_range(start: date, end: date):
    """Yields (year, month) for every calendar month from start through end, inclusive."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def get_futures_contracts(
    symbol: str,
    start_date: DateLike,
    end_date: DateLike,
    kite: Optional[KiteConnect] = None,
) -> List[FuturesContract]:
    """
    Returns the sequence of (approximated) NFO FUT contracts for `symbol`
    covering `start_date`..`end_date`, plus one extra lead month so the
    contract that was already active *at* `start_date` is included even if
    its own expiry falls before `start_date`.

    See module docstring for the approximation rules (last-Thursday expiry,
    current lot size applied to all historical contracts). `instrument_token`
    is only resolvable for contracts still present in Kite's live NFO catalog.
    """
    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()
    if start > end:
        raise ValueError(f"start_date ({start}) must be <= end_date ({end})")

    lead_month = start.month - 1 or 12
    lead_year = start.year if start.month > 1 else start.year - 1
    range_start = date(lead_year, lead_month, 1)

    live_contracts = _live_fut_contracts_for_symbol(kite, symbol)
    current_lot_size = DEFAULT_LOT_SIZE_FALLBACK
    if live_contracts:
        current_lot_size = int(next(iter(live_contracts.values()))["lot_size"])

    contracts: List[FuturesContract] = []
    for year, month in _month_range(range_start, end):
        expiry = _last_thursday(year, month)
        tsym = _contract_tradingsymbol(symbol, expiry)
        live_row = live_contracts.get(tsym)
        contracts.append(FuturesContract(
            tradingsymbol=tsym,
            instrument_token=int(live_row["instrument_token"]) if live_row else None,
            lot_size=int(live_row["lot_size"]) if live_row else current_lot_size,
            expiry=pd.Timestamp(expiry),
            name=symbol.upper(),
        ))

    return contracts
