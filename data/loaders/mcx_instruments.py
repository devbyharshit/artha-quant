"""MCX commodity futures contract resolver (docs/13-mcx-commodities-momentum-design.md §4).

Mirrors `data/loaders/futures_instruments.py` (NFO single-stock futures) in
structure: a caching Kite catalog fetch, an approximated contract calendar, and
an `instrument_token` that is only resolved when the contract is still listed.

**Live catalog limitation (same as NFO):** `kite.instruments("MCX")` only
exposes currently-listed and upcoming contracts, never expired ones. Historical
contracts therefore get `instrument_token=None`, and the calendar below is an
approximation rather than a recovered record.

**Expiry approximation.** Task 1's fixture captures of MCX's FUTCOM bhavcopy
(`tests/fixtures/mcx_bhavcopy/`) contain real expiry dates for each of the
four default commodities. Each rule below was chosen to match those observed
dates, and the observed date is cited next to it. Exchange holidays are NOT
modelled (only weekends), so a real expiry falling on a market holiday will
differ by a day or more. This is a deliberate, documented simplification.

- GOLDM: the 5th of the expiry month. If that is a Saturday or Sunday, the
  preceding Friday is used. Observed: 03JUL2015 (5 Jul 2015 was a Sunday),
  04SEP2015 (Saturday), 05AUG2015 (weekday).
- SILVERMIC: the last weekday of the expiry month. Observed: 28FEB2020 (29 Feb
  2020 was a Saturday), 30NOV2026.
- CRUDEOILM: the 19th of the expiry month, rolled back to the preceding Friday
  when the 19th is a weekend day. Observed: 19NOV2015, 18DEC2026 (19 Dec 2026
  was a Saturday). The exchange's real shift direction is not uniform (for
  example 20JUL2015 and 21SEP2015 roll forward), so the backward roll is a
  documented choice that may differ by a day or two.
- NATGASMINI: three weekdays before the last weekday of the expiry month.
  Observed: 28JAN2020 (last weekday 31 Jan 2020, minus 3 weekdays = 28 Jan),
  taken from the NATURALGAS row, since no NATGASMINI-specific row exists in
  the `futcom_2020-01-15.json` fixture. Ignoring Thanksgiving leaves Nov 2026
  one day late (observed 24NOV2026,
  rule gives 25NOV2026), which is the holiday gap noted above.

These rules replace the mid-month Gold/Silver and end-of-month Crude
expectations in the Task 2 brief, which the Task 1 fixtures contradict.
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

# Current lot sizes of the four default MCX instruments. Lot sizes are static
# exchange product specs (Task 1 found no lot-size field in MCX's historical
# endpoints), so these values are applied retroactively to every historical
# contract. This is a stated approximation, same posture as
# `futures_instruments.py`'s current-lot-size-applied-to-all rule.
GOLDM_LOT_SIZE = 100       # current lot size, applied retroactively (100 g)
SILVERMIC_LOT_SIZE = 1     # current lot size, applied retroactively (1 kg)
CRUDEOILM_LOT_SIZE = 10    # current lot size, applied retroactively (10 barrels)
NATGASMINI_LOT_SIZE = 250  # current lot size, applied retroactively (250 mmBtu)

_LOT_SIZES: Dict[str, int] = {
    "GOLDM": GOLDM_LOT_SIZE,
    "SILVERMIC": SILVERMIC_LOT_SIZE,
    "CRUDEOILM": CRUDEOILM_LOT_SIZE,
    "NATGASMINI": NATGASMINI_LOT_SIZE,
}

# PRICE QUOTE UNIT -- how many lot-size units one quoted price refers to.
#
# MCX quotes each contract's Open/High/Low/Close in rupees per a *quotation
# unit* that is not always the same unit the lot size is expressed in:
#
# - GOLDM: lot size 100 grams, but price quoted in Rs per **10 grams** -> 10.
# - SILVERMIC: lot size 1 kg, price quoted in Rs per kg -> 1.
# - CRUDEOILM: lot size 10 barrels, price quoted in Rs per barrel -> 1.
# - NATGASMINI: lot size 250 mmBtu, price quoted in Rs per mmBtu -> 1.
#
# CONVENTION (used everywhere a quoted price becomes rupees -- sizing in
# `backtesting.commodity_signals`, entry/exit value, mark-to-market and
# exposure in `backtesting.commodity_engine`, and therefore the cost-model
# inputs): prices stay as RAW EXCHANGE QUOTES throughout the pipeline (so
# trade logs match the bhavcopy), quantities stay in LOT-SIZE UNITS
# (num_lots * lot_size, e.g. grams for GOLDM), and the rupee value is
#
#     rupees = quantity_in_lot_units * quoted_price / price_quote_unit
#
# i.e. it is a DIVISOR. For GOLDM one lot (100 g) at a quote of Rs 1,40,528
# per 10 g is 100 * 140528 / 10 = Rs 14.05 lakh, not 100 * 140528.
#
# Ground truth: MCX's own bhavcopy reports traded `Value` (Rs lakh) and
# `Volume` (lots) per row, so `Value * 1e5 / Volume / Close` is the rupee
# value per lot per quoted point, which must equal lot_size / price_quote_unit
# (GOLDM ~10 = 100/10, SILVERMIC ~1, CRUDEOILM ~10, NATGASMINI ~250 in both the
# 2015 and 2026 fixtures). `tests/test_mcx_instruments.py` derives this from
# the real fixtures rather than asserting a hand-picked constant. Note the
# data pins down only the RATIO lot_size / price_quote_unit; the split into
# "100 g lot, quoted per 10 g" comes from the exchange's contract spec.
GOLDM_PRICE_QUOTE_UNIT = 10      # Rs per 10 g; lot is 100 g
SILVERMIC_PRICE_QUOTE_UNIT = 1   # Rs per kg; lot is 1 kg
CRUDEOILM_PRICE_QUOTE_UNIT = 1   # Rs per barrel; lot is 10 barrels
NATGASMINI_PRICE_QUOTE_UNIT = 1  # Rs per mmBtu; lot is 250 mmBtu

_PRICE_QUOTE_UNITS: Dict[str, int] = {
    "GOLDM": GOLDM_PRICE_QUOTE_UNIT,
    "SILVERMIC": SILVERMIC_PRICE_QUOTE_UNIT,
    "CRUDEOILM": CRUDEOILM_PRICE_QUOTE_UNIT,
    "NATGASMINI": NATGASMINI_PRICE_QUOTE_UNIT,
}


def get_static_lot_size(commodity: str) -> int:
    """Static current lot size (in lot-size units) for a supported commodity;
    raises ValueError for anything outside the core-4 table."""
    symbol = commodity.upper()
    if symbol not in _LOT_SIZES:
        raise ValueError(f"Unsupported MCX commodity {commodity!r}; expected one of {sorted(_LOT_SIZES)}")
    return _LOT_SIZES[symbol]


def get_price_quote_unit(commodity: str) -> int:
    """Lot-size units per quoted price for a supported commodity (see the
    PRICE QUOTE UNIT convention above); raises ValueError for anything outside
    the core-4 table -- never silently defaults to 1."""
    symbol = commodity.upper()
    if symbol not in _PRICE_QUOTE_UNITS:
        raise ValueError(f"Unsupported MCX commodity {commodity!r}; expected one of {sorted(_PRICE_QUOTE_UNITS)}")
    return _PRICE_QUOTE_UNITS[symbol]


def _validated_live_lot_size(commodity: str, kite_lot_size) -> int:
    """Sanity-checks a Kite-reported MCX lot size against the static table.

    docs/13 §3 warns that Kite's MCX instrument dump has been observed with
    wrong lot sizes (Zerodha forum reports; Kite also expresses MCX order
    quantity in *lots*, so its `lot_size` field may legitimately read 1 where
    this pipeline needs grams/kg/barrels/mmBtu). The static table, by
    contrast, is cross-checked against MCX's own traded Value/Volume in the
    real bhavcopy fixtures (see PRICE QUOTE UNIT above). So on any mismatch
    -- or an unparseable/non-positive Kite value -- this logs a warning and
    FALLS BACK TO THE STATIC TABLE rather than silently substituting a value
    that would rescale every notional, P&L and cost figure. A genuine
    exchange lot-size change (by circular) must be applied by updating the
    static table, deliberately, after checking it against bhavcopy data."""
    static = get_static_lot_size(commodity)
    try:
        live = int(kite_lot_size)
    except (TypeError, ValueError):
        logger.warning(
            f"{commodity.upper()}: Kite reported an unparseable lot size {kite_lot_size!r}; "
            f"using the static table's {static}."
        )
        return static
    if live != static:
        logger.warning(
            f"{commodity.upper()}: Kite reports lot size {live} but the static table "
            f"(cross-checked against MCX bhavcopy Value/Volume) has {static}; Kite's MCX lot "
            f"sizes are known-unreliable (docs/13 §3), so using the static {static}. If MCX "
            f"really changed the lot size, update mcx_instruments._LOT_SIZES deliberately."
        )
        return static
    return live


@dataclass
class CommodityContract:
    tradingsymbol: str
    instrument_token: Optional[int]
    lot_size: int
    expiry: pd.Timestamp
    name: str  # underlying commodity, e.g. "GOLDM", "SILVERMIC", "CRUDEOILM", "NATGASMINI"


def get_mcx_instruments_cache_path() -> Path:
    settings = get_settings()
    settings.DATA_STORE_DIR.mkdir(parents=True, exist_ok=True)
    return settings.DATA_STORE_DIR / "instruments_MCX.json"


def fetch_and_cache_mcx_instruments(kite: KiteConnect, force_refresh: bool = False) -> List[dict]:
    """
    Downloads the current MCX instrument catalog from Kite and caches it to
    disk for the day, mirroring `futures_instruments.fetch_and_cache_nfo_instruments`.
    Like the NFO catalog, this holds only currently-listed and upcoming contracts.
    """
    cache_path = get_mcx_instruments_cache_path()
    today_str = date.today().isoformat()

    if not force_refresh and cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("cache_date") == today_str and "instruments" in data:
                    logger.debug(f"Loaded MCX instrument catalog from cache ({len(data['instruments'])} rows)")
                    return data["instruments"]
        except Exception as exc:
            logger.warning(f"Failed to read MCX instrument cache: {exc}")

    logger.info("Fetching MCX instrument catalog from Kite API...")
    raw = kite.instruments("MCX")

    serializable = []
    for inst in raw:
        row = dict(inst)
        expiry = row.get("expiry")
        if isinstance(expiry, (date, datetime)):
            row["expiry"] = expiry.isoformat()
        serializable.append(row)

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"cache_date": today_str, "instruments": serializable}, f)

    logger.info(f"Cached {len(serializable)} MCX instrument rows to {cache_path}")
    return serializable


def _live_fut_contracts_for_commodity(kite: Optional[KiteConnect], commodity: str) -> Dict[str, dict]:
    """Returns {tradingsymbol: raw_instrument_dict} for commodity's currently-listed FUT contracts, or {} if unavailable."""
    if kite is None:
        return {}
    try:
        catalog = fetch_and_cache_mcx_instruments(kite)
    except Exception as exc:
        logger.warning(f"Could not fetch live MCX catalog ({exc}); falling back to approximated calendar only.")
        return {}

    out: Dict[str, dict] = {}
    for row in catalog:
        if row.get("name") == commodity.upper() and row.get("instrument_type") == "FUT":
            out[row["tradingsymbol"]] = row
    return out


def _nth_weekday_before(d: date, n: int) -> date:
    """Returns the date `n` weekdays before `d` (weekends skipped, holidays not)."""
    out = d
    while n > 0:
        out -= timedelta(days=1)
        if out.weekday() < 5:
            n -= 1
    return out


def _on_or_prev_weekday(year: int, month: int, day: int) -> date:
    """Returns the given calendar day, or the preceding Friday if it falls on a weekend."""
    d = date(year, month, day)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _last_weekday(year: int, month: int) -> date:
    """Returns the last Monday-to-Friday day of the month."""
    last_day = date(year, month, calendar.monthrange(year, month)[1])
    return _on_or_prev_weekday(year, month, last_day.day)


def _commodity_expiry(commodity: str, year: int, month: int) -> date:
    """
    Approximated expiry for `commodity`'s contract in the given calendar month.
    See module docstring for each rule and the Task 1 fixture dates it matches.
    Raises ValueError for commodities without a defined rule.
    """
    symbol = commodity.upper()
    if symbol == "GOLDM":
        return _on_or_prev_weekday(year, month, 5)
    if symbol == "SILVERMIC":
        return _last_weekday(year, month)
    if symbol == "CRUDEOILM":
        return _on_or_prev_weekday(year, month, 19)
    if symbol == "NATGASMINI":
        return _nth_weekday_before(_last_weekday(year, month), 3)
    raise ValueError(f"No MCX expiry rule defined for commodity {commodity!r}")


def _contract_tradingsymbol(commodity: str, expiry: date) -> str:
    """Builds Kite's MCX futures tradingsymbol convention, e.g. GOLDM26DECFUT."""
    yy = expiry.strftime("%y")
    mon = expiry.strftime("%b").upper()
    return f"{commodity.upper()}{yy}{mon}FUT"


def _month_range(start: date, end: date):
    """Yields (year, month) for every calendar month from start through end, inclusive."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def get_commodity_contracts(
    commodity: str,
    start_date: DateLike,
    end_date: DateLike,
    kite: Optional[KiteConnect] = None,
) -> List[CommodityContract]:
    """
    Returns the sequence of (approximated) MCX FUT contracts for `commodity`
    covering `start_date`..`end_date`, plus one extra lead month so the contract
    that was already active *at* `start_date` is included even if its own expiry
    falls before `start_date`.

    See module docstring for the expiry rules and the lot-size approximation.
    `instrument_token` is only resolvable for contracts still present in Kite's
    live MCX catalog.
    """
    symbol = commodity.upper()
    if symbol not in _LOT_SIZES:
        raise ValueError(f"Unsupported MCX commodity {commodity!r}; expected one of {sorted(_LOT_SIZES)}")

    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()
    if start > end:
        raise ValueError(f"start_date ({start}) must be <= end_date ({end})")

    lead_month = start.month - 1 or 12
    lead_year = start.year if start.month > 1 else start.year - 1
    range_start = date(lead_year, lead_month, 1)

    live_contracts = _live_fut_contracts_for_commodity(kite, symbol)
    # Kite's lot size is never trusted unchecked (see _validated_live_lot_size):
    # on a mismatch with the static table it warns and keeps the static value.
    current_lot_size = _LOT_SIZES[symbol]
    if live_contracts:
        current_lot_size = _validated_live_lot_size(symbol, next(iter(live_contracts.values())).get("lot_size"))

    contracts: List[CommodityContract] = []
    for year, month in _month_range(range_start, end):
        expiry = _commodity_expiry(symbol, year, month)
        tsym = _contract_tradingsymbol(symbol, expiry)
        live_row = live_contracts.get(tsym)
        contracts.append(CommodityContract(
            tradingsymbol=tsym,
            instrument_token=int(live_row["instrument_token"]) if live_row else None,
            lot_size=current_lot_size,
            expiry=pd.Timestamp(expiry),
            name=symbol,
        ))

    return contracts
