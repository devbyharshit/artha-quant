"""MCX commodity historical bhavcopy ingester
(docs/13-mcx-commodities-momentum-design.md §3.1).

**Why this exists:** `docs/13` §3.1's Phase 0 investigation found that MCX
publishes no NSE-style predictable `.csv.zip` archive file. Instead,
`www.mcxindia.com`'s "Bhavcopy" and "Historical Data" pages are UI-rendered
report builders: the page's own JS calls a JSON endpoint and renders the
result into an HTML table client-side (the page's own "Download CSV"/"XLS"
buttons just reformat that same in-memory JSON -- there is no server-side
archive file to fetch directly). This module therefore calls MCX's JSON
endpoint directly and builds its own on-disk cache from the JSON, the same
end goal as `nse_bhavcopy.py` but via different plumbing.

**Confirmed endpoint (verified live, by a real browser session, during the
Phase 0 investigation -- 2026-11-11):**

    GET https://www.mcxindia.com/market-data/bhavcopy/GetDateWiseBhavCopy
        ?InstrumentName=FUTCOM&fromDate=DD%2FMM%2FYYYY

Returns `{"IsSuccess": bool, "Message": str, "Data": [...] | None,
"Metadata": null}`. On success, each row of `Data` is one live contract's
bar for that single day: `Symbol` (space-padded -- must `.strip()`),
`ExpiryDate` (`DDMMMYYYY`), `Open`/`High`/`Low`/`Close`, plus volume/OI
fields this module does not need. `docs/13` §3.1 also confirmed a second
endpoint, `GetCommoditywiseBhavCopy` (one contract's full trading life in a
single call), which would be a more efficient way to backfill one contract
end-to-end -- deliberately not used here so this module's fetch/cache shape
mirrors `nse_bhavcopy.py`'s day-by-day, symbol-filtered-client-side pattern
exactly (one raw+parsed cache file per calendar date, filtered by symbol in
`get_historical_commodity_bars`), rather than introducing a second, per-
contract caching shape.

**The "HTTP 200 with an HTML 404 body" trap (§3.1):** without the
`X-Requested-With: XMLHttpRequest` header, MCX's server does not 403/404 in
the usual sense -- it returns HTTP 200 with the site's full HTML error page
as the body, for every URL and date tried. A bare `status_code == 200`
check is therefore not sufficient. This module always attempts to parse the
response as JSON and treats a non-JSON (or JSON missing the `IsSuccess`
key) response as a failed fetch, exactly like a transient error -- never as
"no data for that date." A true no-data day (confirmed real example: 17 Nov
2003, a weekday MCX genuinely had no trading on) comes back as valid JSON
with `IsSuccess: false` and `Message: "No data found."`, which this module
parses into a correctly-empty frame rather than retrying.

**Lot size is not in MCX's bhavcopy data at all** (confirmed by §3.1 --
checked every field on every row). Unlike NSE single-stock-futures lot
sizes (which can change with corporate actions and need a genuine per-date
historical value), MCX commodity lot sizes are fixed exchange product
specs that only change via occasional circulars. `get_historical_commodity_bars`
therefore joins in the same static current-lot-size table
`data/loaders/mcx_instruments.py` already maintains for the four default
commodities (GOLDM, SILVERMIC, CRUDEOILM, NATGASMINI), applied retroactively
-- same approximation posture that module's docstring already documents.
`get_historical_commodity_bars` validates `symbol` against that same core-
four lookup table and **raises `ValueError`** for anything outside it --
matching `mcx_instruments.get_commodity_contracts`'s existing posture for
the identical condition (both are the same kind of gap: a symbol-dependent
lookup-table miss, not a date-dependent data-availability gap like
`nse_bhavcopy.py`'s pre-UDiFF missing lot sizes, so that module's NA-
fallback posture does not apply here).

**Access-method caveat -- carried forward from `docs/13` §3.1, UNRESOLVED
as of this module's build:** Phase 0's investigation reached these MCX
endpoints successfully only through a real browser session; a direct
scripted `curl`/`requests` call from that investigation's own sandbox got a
flat Akamai-edge 403 ("Access Denied"/"AkamaiGHost"), for every URL tried,
including the plain page load. Whether that was sandbox-IP-specific (in
which case a plain `requests.Session`, as implemented below, should work
fine from a different host) or a browser-fingerprint-level block (in which
case this module's `requests`-based fetch would need to be replaced with a
headless-browser-driven one) was explicitly left as the one open risk for
this module's builder to check "from the real deployment host" before
trusting the live path.

**That live-host check was not performed while building this module.**
Running `curl`/`requests` against the real `mcxindia.com` endpoints, with or
without header spoofing, to try to prove or disprove the Akamai block is
explicitly out of scope for this build step (risks tripping the same bot-
check tooling that blocked a similar attempt at the harness level during
this task, independent of the Akamai question itself). This module is
therefore built and tested exactly as the brief specifies -- a plain
`requests.Session` mirroring `nse_bhavcopy.py`'s session/header/retry/cache
shape, fully validated offline against Task 1's real captured fixture
responses (`tests/fixtures/mcx_bhavcopy/`) -- but **whether live fetching
from this system's actual deployment host actually works through this
plain-session implementation remains unverified.** That is a fact to
surface, not a gap to quietly route around with browser automation (out of
scope for this task) or to paper over by asserting success.

**Rate limiting / politeness:** `docs/13` §3.1 saw no 429s or throttling
across ~15-20 sequential requests in its own testing, but recommends the
same conservative default posture as `nse_bhavcopy.py` until proven
unnecessary -- this module copies that module's persistent session,
retry-with-backoff, and minimum-fetch-interval throttle verbatim.
"""
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Union

import pandas as pd
import requests

from configs.config import get_settings
from configs.logger import logger
from data.loaders.mcx_instruments import (
    CRUDEOILM_LOT_SIZE,
    GOLDM_LOT_SIZE,
    NATGASMINI_LOT_SIZE,
    SILVERMIC_LOT_SIZE,
)

DateLike = Union[str, date, datetime, pd.Timestamp]

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_BASE_URL = "https://www.mcxindia.com"
_DATEWISE_URL = f"{_BASE_URL}/market-data/bhavcopy/GetDateWiseBhavCopy"
_WARMUP_URL = f"{_BASE_URL}/market-data/bhavcopy"

# Static current lot sizes for the four default commodities (docs/13 §4),
# mirroring mcx_instruments.py's own table -- see module docstring. A symbol
# outside this table is unsupported and get_historical_commodity_bars raises
# ValueError for it, matching mcx_instruments.get_commodity_contracts.
_LOT_SIZES = {
    "GOLDM": GOLDM_LOT_SIZE,
    "SILVERMIC": SILVERMIC_LOT_SIZE,
    "CRUDEOILM": CRUDEOILM_LOT_SIZE,
    "NATGASMINI": NATGASMINI_LOT_SIZE,
}

# A day with genuinely no trading (weekend/holiday, or MCX's documented
# IsSuccess:false/"No data found." response) returns this empty,
# correctly-shaped frame rather than raising.
MCX_BHAVCOPY_COLUMNS = ["SYMBOL", "EXPIRY_DT", "OPEN", "HIGH", "LOW", "CLOSE"]

_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 2.0
# Minimum delay between *actual* network fetches (cache hits are exempt).
_MIN_FETCH_INTERVAL_SECONDS = 0.5

_last_fetch_ts: float = 0.0

# Fail-fast guard: after this many *consecutive* dates whose fetch exhausted
# all retries without a single genuine MCX response, fetch_mcx_bhavcopy raises
# MCXAccessBlockedError instead of grinding on. A persistent Akamai-edge 403
# (module docstring's access-method caveat) is not a per-request fluke, and
# each failed date costs ~12s+ of retry backoff, so an 11-year x 4-symbol run
# would otherwise take hours to fail. 5 = one full trading week: genuine
# no-data days (holidays) are *successful* responses and never count, and a
# real transient outage rarely spans 5 back-to-back dates. Cache hits neither
# count nor reset; any successful network fetch resets the counter to 0.
MCX_MAX_CONSECUTIVE_FETCH_FAILURES = 5

_consecutive_fetch_failures: int = 0


class MCXAccessBlockedError(RuntimeError):
    """Raised when MCX fetches keep failing outright (see
    MCX_MAX_CONSECUTIVE_FETCH_FAILURES) -- most likely the Akamai block."""


def _bhavcopy_cache_dir() -> Path:
    settings = get_settings()
    cache_dir = settings.DATA_STORE_DIR / "mcx_bhavcopy"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _raw_json_cache_path(trading_date: date) -> Path:
    return _bhavcopy_cache_dir() / "raw" / f"{trading_date.isoformat()}.json"


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
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": _WARMUP_URL,
        # Required by MCX -- without this header the server returns HTTP 200
        # with its HTML error page as the body instead of 403/404ing. See
        # module docstring.
        "X-Requested-With": "XMLHttpRequest",
    })
    try:
        # Best-effort cookie warm-up, same posture as nse_bhavcopy.py.
        session.get(_WARMUP_URL, timeout=10)
    except requests.RequestException as exc:
        logger.debug(f"MCX bhavcopy-page warm-up request failed (continuing anyway): {exc}")
    return session


def _fetch_date_wise_json(trading_date: date, session: requests.Session) -> Optional[dict]:
    """Fetches the GetDateWiseBhavCopy JSON for one day, retrying transient
    failures. Returns the parsed dict on any genuine JSON response -- including
    a valid `IsSuccess: false` / "No data found." no-data response, which is
    not an error -- or None after exhausting retries on a true failure (a
    request exception, a non-200 status, or a response that isn't valid JSON
    at all, e.g. MCX's "200 with an HTML 404 body" trap -- see module
    docstring)."""
    params = {"InstrumentName": "FUTCOM", "fromDate": trading_date.strftime("%d/%m/%Y")}

    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_RETRIES + 1):
        _throttle()
        try:
            resp = session.get(_DATEWISE_URL, params=params, timeout=30)
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning(f"MCX fetch attempt {attempt}/{_MAX_RETRIES} failed for {trading_date}: {exc}")
            time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
            continue

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                last_exc = RuntimeError(
                    f"MCX returned non-JSON (likely the HTML-404 trap) for {trading_date}"
                )
                logger.warning(f"MCX fetch attempt {attempt}/{_MAX_RETRIES}: {last_exc}")
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
                continue
            if "IsSuccess" not in data:
                last_exc = RuntimeError(f"MCX JSON response missing IsSuccess for {trading_date}: {data!r}")
                logger.warning(f"MCX fetch attempt {attempt}/{_MAX_RETRIES}: {last_exc}")
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
                continue
            return data

        last_exc = RuntimeError(f"Unexpected status {resp.status_code} for {trading_date}")
        logger.warning(f"MCX fetch attempt {attempt}/{_MAX_RETRIES} got status {resp.status_code} for {trading_date}")
        time.sleep(_RETRY_BACKOFF_SECONDS * attempt)

    if last_exc is not None:
        logger.warning(f"Giving up on MCX bhavcopy for {trading_date} after {_MAX_RETRIES} attempts: {last_exc}")
    return None


def _parse_datewise_response(data: dict) -> pd.DataFrame:
    """Parses one GetDateWiseBhavCopy JSON response into the unified schema.
    A no-data response (`IsSuccess: false`, e.g. "No data found.") yields a
    correctly-empty frame, not an error."""
    if not data.get("IsSuccess"):
        return pd.DataFrame(columns=MCX_BHAVCOPY_COLUMNS)

    rows = data.get("Data") or []
    if not rows:
        return pd.DataFrame(columns=MCX_BHAVCOPY_COLUMNS)

    raw = pd.DataFrame(rows)
    futcom = raw[raw["InstrumentName"].str.strip() == "FUTCOM"].copy()
    if futcom.empty:
        return pd.DataFrame(columns=MCX_BHAVCOPY_COLUMNS)

    out = pd.DataFrame({
        "SYMBOL": futcom["Symbol"].str.strip(),
        "EXPIRY_DT": pd.to_datetime(futcom["ExpiryDate"], format="%d%b%Y"),
        "OPEN": futcom["Open"].astype("float64"),
        "HIGH": futcom["High"].astype("float64"),
        "LOW": futcom["Low"].astype("float64"),
        "CLOSE": futcom["Close"].astype("float64"),
    })
    return out.reset_index(drop=True)


def _download_and_parse(trading_date: date) -> Optional[pd.DataFrame]:
    """Returns the parsed frame for any genuine MCX JSON response (possibly
    empty, for a real no-data day), or **None** if no successful response was
    obtained after all retries -- callers must not cache a None result as
    "no data" (see `fetch_mcx_bhavcopy`)."""
    session = _get_session()
    data = _fetch_date_wise_json(trading_date, session)

    if data is None:
        return None

    raw_path = _raw_json_cache_path(trading_date)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    import json as _json
    raw_path.write_text(_json.dumps(data))

    try:
        return _parse_datewise_response(data)
    except (KeyError, ValueError) as exc:
        logger.warning(f"Failed to parse MCX bhavcopy for {trading_date.isoformat()}: {exc}")
        return pd.DataFrame(columns=MCX_BHAVCOPY_COLUMNS)


def fetch_mcx_bhavcopy(trading_date: DateLike) -> pd.DataFrame:
    """
    Fetches one trading day's MCX FUTCOM bhavcopy (all commodities, from the
    local disk cache if already fetched). Returns a DataFrame with columns
    `MCX_BHAVCOPY_COLUMNS` (SYMBOL, EXPIRY_DT, OPEN, HIGH, LOW, CLOSE).

    Returns an empty (correctly-shaped) DataFrame for a weekend/holiday/
    no-data date. Only a genuine MCX response (including a confirmed
    "No data found." one) is written to the parsed cache; a fetch that never
    got a successful response after all retries also returns an empty frame
    but is **not** cached, so a later call retries it instead of trusting a
    bad cached "no data".

    Raises `MCXAccessBlockedError` once `MCX_MAX_CONSECUTIVE_FETCH_FAILURES`
    network fetches in a row have failed outright (see that constant).
    """
    global _consecutive_fetch_failures
    trading_date = pd.Timestamp(trading_date).date()

    parsed_path = _parsed_cache_path(trading_date)
    if parsed_path.exists():
        try:
            df = pd.read_parquet(parsed_path)
            if not df.empty:
                df["EXPIRY_DT"] = pd.to_datetime(df["EXPIRY_DT"])
            return df
        except Exception as exc:
            logger.warning(f"Failed to read cached MCX bhavcopy parquet {parsed_path} ({exc}); re-fetching.")

    df = _download_and_parse(trading_date)

    if df is None:
        _consecutive_fetch_failures += 1
        logger.warning(
            f"Could not fetch MCX bhavcopy for {trading_date.isoformat()} "
            f"(consecutive failures: {_consecutive_fetch_failures}); not caching, will retry on a later call."
        )
        if _consecutive_fetch_failures >= MCX_MAX_CONSECUTIVE_FETCH_FAILURES:
            raise MCXAccessBlockedError(
                f"MCX access appears blocked ({_consecutive_fetch_failures} consecutive failures) -- "
                "aborting fetch, see data/loaders/mcx_bhavcopy.py docstring for the known access-method risk."
            )
        return pd.DataFrame(columns=MCX_BHAVCOPY_COLUMNS)

    _consecutive_fetch_failures = 0

    parsed_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(parsed_path)
    except Exception as exc:
        logger.warning(f"Failed to cache parsed MCX bhavcopy to {parsed_path}: {exc}")

    return df


def _contract_tradingsymbol(symbol: str, expiry: pd.Timestamp) -> str:
    """Builds the Kite-style MCX futures tradingsymbol, e.g. GOLDM15JULFUT,
    matching the convention used by `mcx_instruments._contract_tradingsymbol`."""
    yy = expiry.strftime("%y")
    mon = expiry.strftime("%b").upper()
    return f"{symbol.upper()}{yy}{mon}FUT"


def get_historical_commodity_bars(
    symbol: str,
    start_date: DateLike,
    end_date: DateLike,
) -> pd.DataFrame:
    """
    Assembles daily FUTCOM rows for one underlying commodity `symbol` across
    `start_date`..`end_date` (inclusive) from MCX's bhavcopy JSON endpoint.

    Calls `fetch_mcx_bhavcopy` once per calendar day in range (skipping
    Saturdays/Sundays; a weekday that turns out to have no data -- holiday or
    otherwise -- is skipped silently, not treated as an error, same posture
    as `nse_bhavcopy.get_historical_futures_bars`).

    Returns one row **per live contract per day** -- a commodity typically
    has 2-3 contracts (near/next/far month) trading simultaneously, so this
    is *not* resolved to "the one active contract"; that resolution is a
    separate concern for the continuous-contract/roll layer.

    The returned DataFrame is indexed by trading date and has columns:
    `tradingsymbol`, `expiry` (pd.Timestamp), `open`, `high`, `low`, `close`,
    `lot_size` (the static current lot size for `symbol` from the table this
    module shares with `mcx_instruments.py`).

    Raises `ValueError` if `symbol` is not one of the four commodities in
    that lookup table (GOLDM, SILVERMIC, CRUDEOILM, NATGASMINI) -- matching
    `mcx_instruments.get_commodity_contracts`'s posture for the same
    unsupported-symbol condition, rather than silently returning
    `lot_size = pd.NA`; see module docstring.

    Propagates `MCXAccessBlockedError` from `fetch_mcx_bhavcopy` when MCX
    fetches keep failing outright, rather than walking the whole range.
    """
    start = pd.Timestamp(start_date).date()
    end = pd.Timestamp(end_date).date()
    if start > end:
        raise ValueError(f"start_date ({start}) must be <= end_date ({end})")

    symbol_upper = symbol.upper()
    if symbol_upper not in _LOT_SIZES:
        raise ValueError(
            f"Unsupported MCX commodity {symbol!r}; expected one of {sorted(_LOT_SIZES)}"
        )
    lot_size = _LOT_SIZES[symbol_upper]
    rows = []

    current = start
    while current <= end:
        if current.weekday() < 5:  # Mon-Fri only; holidays simply yield no data
            day_df = fetch_mcx_bhavcopy(current)
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
                        "lot_size": lot_size,
                    })
        current = current + timedelta(days=1)

    columns = ["date", "tradingsymbol", "expiry", "open", "high", "low", "close", "lot_size"]
    if not rows:
        return pd.DataFrame(columns=columns).set_index("date")

    result = pd.DataFrame(rows, columns=columns)
    result = result.set_index("date").sort_index()
    return result
