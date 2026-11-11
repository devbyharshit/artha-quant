"""NSE F&O historical bhavcopy ingester (docs/12-futures-trading-plan.md §5).

**Why this exists:** `docs/12` §5 confirmed that Kite's NFO instrument
catalog (`data/loaders/futures_instruments.py`) only ever exposes
currently-listed-and-upcoming contracts, never a historical record --
there is no way to get genuinely historical NFO futures OHLCV through
Kite. NSE's own public bhavcopy archives are the standard workaround:
free, no API key, no Kite session, daily EOD settlement data going back
to at least 2011 (verified live during this module's build -- see
below), far deeper than the handful of months Kite retains.

**Two confirmed, currently-live formats, verified by live fetch during
this module's build (2026-11-10):**

1. "Legacy" per-day CSV-in-zip -- one file per trading day:

       https://archives.nseindia.com/content/historical/DERIVATIVES/
       <YYYY>/<MON>/fo<DD><MON><YYYY>bhav.csv.zip

   e.g. ``fo15JUN2023bhav.csv.zip``. Verified working for 2011-06-15,
   2020-06-15, 2023-06-15, and 2024-07-05 (the day before NSE's
   documented cutover, NSE circular 62424 dated 2024-06-12: legacy
   bhavcopy discontinued w.e.f. 2024-07-08). Requires a browser
   `User-Agent` *and* `Accept` *and* `Referer` header together --
   a bare User-Agent alone 403'd intermittently in testing, but
   UA+Accept+Referer was 100% reliable across every date tried, with
   no cookie warm-up needed. Columns: ``INSTRUMENT, SYMBOL, EXPIRY_DT,
   STRIKE_PR, OPTION_TYP, OPEN, HIGH, LOW, CLOSE, SETTLE_PR, CONTRACTS,
   VAL_INLAKH, OPEN_INT, CHG_IN_OI, TIMESTAMP``. ``INSTRUMENT`` is
   already ``FUTSTK``/``FUTIDX``/``OPTSTK``/``OPTIDX`` in this format.
   **No lot-size column at all** -- this format cannot close the
   lot-size-approximation gap documented in `futures_instruments.py`.

2. "UDiFF" (NSE's current Unified Distilled File Format), the
   replacement format since the 2024-07-08 cutover:

       https://nsearchives.nseindia.com/content/fo/
       BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip

   Verified working for 2024-07-05 (both formats were live briefly
   around the transition), 2024-07-08, and 2025-10-24. Only needs a
   browser `User-Agent` in testing (no cookie warm-up required), but
   this module still does a best-effort homepage warm-up first since
   NSE's bot-blocking is known to be inconsistent/IP- and load-
   dependent -- cheap insurance, not load-bearing in testing. Columns
   include ``FinInstrmTp`` (``STF`` = single-stock futures, ``STO`` =
   stock options, ``IDF``/``IDO`` = index futures/options -- ``STF`` is
   this format's FUTSTK equivalent), ``TckrSymb``, ``XpryDt``,
   ``OpnPric/HghPric/LwPric/ClsPric``, and -- importantly --
   **``NewBrdLotQty``: the real historical lot size for that exact
   contract on that exact date.** This is a genuine improvement over
   `futures_instruments.py`'s always-current-lot-size approximation,
   but it only covers dates from 2024-07-08 onward; before that, lot
   size still has to be approximated exactly as it is today.

This module tries the UDiFF URL first (today's live, supported format,
and also reachable slightly before the official cutover), falling back
to the legacy URL when UDiFF 404s. Every raw zip is cached to disk the
first time it is fetched so repeated backtests over wide date ranges
don't re-hit NSE for dates already pulled.

**NSE rate-limiting / blocking:** no hard 429s were observed in testing,
but NSE is known to rate-limit or soft-block tight request loops from a
single source without warning. This module therefore (a) caches
aggressively (raw zip + parsed frame on disk, keyed by date), (b) uses a
single persistent `requests.Session` with browser-like headers across
calls, (c) retries a transient failure a few times with exponential
backoff, and (d) sleeps briefly between *actual* network fetches (not
cache hits) in `get_historical_futures_bars` to avoid hammering NSE
across what could be years of daily requests.
"""
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Optional, Union

import pandas as pd
import requests

from configs.config import get_settings
from configs.logger import logger

DateLike = Union[str, date, datetime, pd.Timestamp]

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_UDIFF_URL_TMPL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyymmdd}_F_0000.csv.zip"
_LEGACY_URL_TMPL = "https://archives.nseindia.com/content/historical/DERIVATIVES/{yyyy}/{mon}/fo{dd}{mon}{yyyy}bhav.csv.zip"

# NSE UDiFF's FinInstrmTp code for single-stock futures. The unified
# output column ("INSTRUMENT") this module produces always uses the
# legacy-style label "FUTSTK" regardless of which source format a given
# date came from.
_UDIFF_STOCK_FUT_CODE = "STF"
_FUTSTK_LABEL = "FUTSTK"

# A day with genuinely no trading (weekend/holiday) or a date NSE simply
# doesn't have data for returns this empty, correctly-shaped frame rather
# than raising.
BHAVCOPY_COLUMNS = ["SYMBOL", "INSTRUMENT", "EXPIRY_DT", "OPEN", "HIGH", "LOW", "CLOSE", "LOT_SIZE"]

_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 2.0
# Minimum delay between *actual* network fetches (cache hits are exempt),
# to be respectful of NSE's servers across a long date-range backfill.
_MIN_FETCH_INTERVAL_SECONDS = 0.5

_last_fetch_ts: float = 0.0


def _bhavcopy_cache_dir() -> Path:
    settings = get_settings()
    cache_dir = settings.DATA_STORE_DIR / "nse_bhavcopy"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _raw_zip_cache_path(trading_date: date) -> Path:
    return _bhavcopy_cache_dir() / "raw" / f"{trading_date.isoformat()}.zip"


def _parsed_cache_path(trading_date: date) -> Path:
    return _bhavcopy_cache_dir() / "parsed" / f"{trading_date.isoformat()}.parquet"


def _throttle() -> None:
    """Sleeps just enough to keep actual network fetches spaced out."""
    global _last_fetch_ts
    elapsed = time.monotonic() - _last_fetch_ts
    remaining = _MIN_FETCH_INTERVAL_SECONDS - elapsed
    if remaining > 0:
        time.sleep(remaining)
    _last_fetch_ts = time.monotonic()


def _get_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/all-reports-derivatives",
    })
    try:
        # Best-effort cookie warm-up -- not required in testing, but NSE's
        # blocking behavior is known to vary by IP/load, so this is cheap
        # insurance against a future 403.
        session.get("https://www.nseindia.com/", timeout=10)
    except requests.RequestException as exc:
        logger.debug(f"NSE homepage warm-up request failed (continuing anyway): {exc}")
    return session


def _fetch_zip_bytes(url: str, session: requests.Session) -> Optional[bytes]:
    """Fetches `url`, retrying transient failures. Returns None on a 404
    (no file published for that date -- not an error) or after exhausting
    retries on other failures."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_RETRIES + 1):
        _throttle()
        try:
            resp = session.get(url, timeout=30)
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning(f"NSE fetch attempt {attempt}/{_MAX_RETRIES} failed for {url}: {exc}")
            time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
            continue

        if resp.status_code == 404:
            return None
        if resp.status_code == 200 and resp.content:
            return resp.content

        last_exc = RuntimeError(f"Unexpected status {resp.status_code} for {url}")
        logger.warning(f"NSE fetch attempt {attempt}/{_MAX_RETRIES} got status {resp.status_code} for {url}")
        time.sleep(_RETRY_BACKOFF_SECONDS * attempt)

    if last_exc is not None:
        logger.warning(f"Giving up on {url} after {_MAX_RETRIES} attempts: {last_exc}")
    return None


def _extract_single_csv(zip_bytes: bytes) -> Optional[str]:
    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            return None
        with zf.open(names[0]) as f:
            return f.read().decode("utf-8", errors="replace")


def _parse_legacy_csv(csv_text: str) -> pd.DataFrame:
    """Parses the legacy fo<DD><MON><YYYY>bhav.csv format into the unified schema."""
    raw = pd.read_csv(BytesIO(csv_text.encode("utf-8")))
    raw.columns = [c.strip() for c in raw.columns]

    futstk = raw[raw["INSTRUMENT"].str.strip() == _FUTSTK_LABEL].copy()
    if futstk.empty:
        return pd.DataFrame(columns=BHAVCOPY_COLUMNS)

    out = pd.DataFrame({
        "SYMBOL": futstk["SYMBOL"].str.strip(),
        "INSTRUMENT": _FUTSTK_LABEL,
        "EXPIRY_DT": pd.to_datetime(futstk["EXPIRY_DT"], format="%d-%b-%Y"),
        "OPEN": futstk["OPEN"].astype("float64"),
        "HIGH": futstk["HIGH"].astype("float64"),
        "LOW": futstk["LOW"].astype("float64"),
        "CLOSE": futstk["CLOSE"].astype("float64"),
        "LOT_SIZE": pd.NA,  # not available in this format -- see module docstring
    })
    return out.reset_index(drop=True)


def _parse_udiff_csv(csv_text: str) -> pd.DataFrame:
    """Parses the current UDiFF BhavCopy_NSE_FO_*.csv format into the unified schema."""
    raw = pd.read_csv(BytesIO(csv_text.encode("utf-8")))
    raw.columns = [c.strip() for c in raw.columns]

    futstk = raw[raw["FinInstrmTp"].str.strip() == _UDIFF_STOCK_FUT_CODE].copy()
    if futstk.empty:
        return pd.DataFrame(columns=BHAVCOPY_COLUMNS)

    out = pd.DataFrame({
        "SYMBOL": futstk["TckrSymb"].str.strip(),
        "INSTRUMENT": _FUTSTK_LABEL,
        "EXPIRY_DT": pd.to_datetime(futstk["XpryDt"]),
        "OPEN": futstk["OpnPric"].astype("float64"),
        "HIGH": futstk["HghPric"].astype("float64"),
        "LOW": futstk["LwPric"].astype("float64"),
        "CLOSE": futstk["ClsPric"].astype("float64"),
        "LOT_SIZE": futstk["NewBrdLotQty"].astype("Int64"),
    })
    return out.reset_index(drop=True)


def _download_and_parse(trading_date: date) -> pd.DataFrame:
    """Tries UDiFF first, falls back to the legacy URL. Returns an empty
    (correctly-shaped) frame if neither format has data for this date."""
    session = _get_session()
    yyyymmdd = trading_date.strftime("%Y%m%d")

    udiff_url = _UDIFF_URL_TMPL.format(yyyymmdd=yyyymmdd)
    zip_bytes = _fetch_zip_bytes(udiff_url, session)
    parser = _parse_udiff_csv
    source_url = udiff_url

    if zip_bytes is None:
        yyyy = trading_date.strftime("%Y")
        mon = trading_date.strftime("%b").upper()
        dd = trading_date.strftime("%d")
        legacy_url = _LEGACY_URL_TMPL.format(yyyy=yyyy, mon=mon, dd=dd)
        zip_bytes = _fetch_zip_bytes(legacy_url, session)
        parser = _parse_legacy_csv
        source_url = legacy_url

    if zip_bytes is None:
        logger.debug(f"No NSE F&O bhavcopy available for {trading_date.isoformat()} (holiday/weekend/no data).")
        return pd.DataFrame(columns=BHAVCOPY_COLUMNS)

    raw_path = _raw_zip_cache_path(trading_date)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(zip_bytes)

    try:
        csv_text = _extract_single_csv(zip_bytes)
    except zipfile.BadZipFile:
        logger.warning(f"NSE returned a non-zip response for {source_url}; treating as no data.")
        return pd.DataFrame(columns=BHAVCOPY_COLUMNS)

    if csv_text is None:
        logger.warning(f"No CSV found inside bhavcopy zip from {source_url}.")
        return pd.DataFrame(columns=BHAVCOPY_COLUMNS)

    try:
        return parser(csv_text)
    except (KeyError, ValueError) as exc:
        logger.warning(f"Failed to parse bhavcopy from {source_url}: {exc}")
        return pd.DataFrame(columns=BHAVCOPY_COLUMNS)


def fetch_fo_bhavcopy(trading_date: DateLike) -> pd.DataFrame:
    """
    Fetches one trading day's F&O bhavcopy from NSE (or the local disk
    cache, if already fetched), filtered to FUTSTK (single-stock futures)
    rows only -- options and index futures are dropped.

    Returns a DataFrame with columns `BHAVCOPY_COLUMNS`
    (SYMBOL, INSTRUMENT, EXPIRY_DT, OPEN, HIGH, LOW, CLOSE, LOT_SIZE).
    `LOT_SIZE` is the real exchange-published lot size for dates covered
    by the UDiFF format (2024-07-08 onward) and `pd.NA` for earlier dates,
    where NSE's own legacy bhavcopy simply never carried it (see module
    docstring) -- callers must not treat a missing LOT_SIZE as zero.

    Returns an empty (correctly-shaped) DataFrame, never raises, for a
    weekend/holiday/no-data date.
    """
    trading_date = pd.Timestamp(trading_date).date()

    parsed_path = _parsed_cache_path(trading_date)
    if parsed_path.exists():
        try:
            df = pd.read_parquet(parsed_path)
            # Parquet round-trips dtypes faithfully, but EXPIRY_DT needs
            # to come back as a proper datetime column, not a generic object.
            if not df.empty:
                df["EXPIRY_DT"] = pd.to_datetime(df["EXPIRY_DT"])
            return df
        except Exception as exc:
            logger.warning(f"Failed to read cached bhavcopy parquet {parsed_path} ({exc}); re-fetching.")

    df = _download_and_parse(trading_date)

    parsed_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(parsed_path)
    except Exception as exc:
        logger.warning(f"Failed to cache parsed bhavcopy to {parsed_path}: {exc}")

    return df


def _contract_tradingsymbol(symbol: str, expiry: pd.Timestamp) -> str:
    """Builds a Kite-style NFO futures tradingsymbol, e.g. RELIANCE26NOVFUT,
    matching the convention used by `futures_instruments._contract_tradingsymbol`."""
    yy = expiry.strftime("%y")
    mon = expiry.strftime("%b").upper()
    return f"{symbol.upper()}{yy}{mon}FUT"


def get_historical_futures_bars(
    symbol: str,
    start_date: DateLike,
    end_date: DateLike,
) -> pd.DataFrame:
    """
    Assembles daily FUTSTK rows for one underlying `symbol` across
    `start_date`..`end_date` (inclusive) from NSE's bhavcopy archives.

    Calls `fetch_fo_bhavcopy` once per calendar day in range (skipping
    Saturdays/Sundays; a weekday that turns out to be an exchange holiday
    or simply has no data is skipped silently, not treated as an error --
    NSE holiday-calendar awareness is deliberately not built in here).

    Returns one row **per live contract per day** -- a liquid stock
    typically has 2-3 contracts (near/next/far month) trading
    simultaneously, so this is *not* resolved to "the one active
    contract." That resolution (the rollover rule) is a separate concern
    owned by the futures-overlay-building layer
    (`data/loaders/sync_futures_history.py`), not this module.

    The returned DataFrame is indexed by trading date and has columns:
    `tradingsymbol`, `expiry` (pd.Timestamp), `open`, `high`, `low`,
    `close`, `lot_size` (real value where available, else NA -- see
    `fetch_fo_bhavcopy`).
    """
    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()
    if start > end:
        raise ValueError(f"start_date ({start}) must be <= end_date ({end})")

    symbol_upper = symbol.upper()
    rows = []

    current = start
    while current <= end:
        if current.weekday() < 5:  # Mon-Fri only; NSE holidays simply yield no data
            day_df = fetch_fo_bhavcopy(current)
            if not day_df.empty:
                day_matches = day_df[day_df["SYMBOL"].str.upper() == symbol_upper]
                for _, r in day_matches.iterrows():
                    expiry_ts = pd.Timestamp(r["EXPIRY_DT"])
                    rows.append({
                        "date": pd.Timestamp(current),
                        "tradingsymbol": _contract_tradingsymbol(symbol_upper, expiry_ts),
                        "expiry": expiry_ts,
                        "open": r["OPEN"],
                        "high": r["HIGH"],
                        "low": r["LOW"],
                        "close": r["CLOSE"],
                        "lot_size": r["LOT_SIZE"],
                    })
        current = current + timedelta(days=1)

    columns = ["date", "tradingsymbol", "expiry", "open", "high", "low", "close", "lot_size"]
    if not rows:
        return pd.DataFrame(columns=columns).set_index("date")

    result = pd.DataFrame(rows, columns=columns)
    result = result.set_index("date").sort_index()
    return result
