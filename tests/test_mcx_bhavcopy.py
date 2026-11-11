"""Tests for the MCX commodity bhavcopy ingester (data/loaders/mcx_bhavcopy.py),
docs/13-mcx-commodities-momentum-design.md §3.1 follow-on.

No real network calls -- the network-hitting parts (`_fetch_date_wise_json` /
`fetch_mcx_bhavcopy`) are monkeypatched out, exactly like
tests/test_nse_bhavcopy.py does for the NSE ingester. Format-parsing
correctness is tested directly against the real (unmodified or documented-
trim) MCX `GetDateWiseBhavCopy` JSON responses captured during Task 1's
Phase 0 investigation and saved under tests/fixtures/mcx_bhavcopy/."""
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from configs.config import reset_settings
import data.store.parquet_store as parquet_store_module
from data.loaders import mcx_bhavcopy
from data.loaders.mcx_bhavcopy import (
    MCX_BHAVCOPY_COLUMNS,
    _parse_datewise_response,
    fetch_mcx_bhavcopy,
    get_historical_commodity_bars,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "mcx_bhavcopy"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Hard guard: no test in this module may reach mcxindia.com. Any real
    `requests` call fails loudly instead of silently retrying against the
    live (Akamai-blocked) endpoint."""
    def refuse(*args, **kwargs):
        raise AssertionError("test attempted a real network request")

    monkeypatch.setattr("requests.Session.request", refuse)


@pytest.fixture
def isolated_data_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_STORE_DIR", str(tmp_path))
    reset_settings()
    parquet_store_module._parquet_store = None
    yield tmp_path
    parquet_store_module._parquet_store = None
    reset_settings()


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


# ---------------------------------------------------------------------------
# _parse_datewise_response -- real fixture parsing
# ---------------------------------------------------------------------------

def test_parse_datewise_response_extracts_known_symbol_columns():
    data = _load_fixture("futcom_2015-06-15.json")
    df = _parse_datewise_response(data)

    assert list(df.columns) == MCX_BHAVCOPY_COLUMNS
    # GOLDM had 3 live contracts (near/next/far month) trading that day.
    goldm = df[df["SYMBOL"] == "GOLDM"]
    assert len(goldm) == 3
    assert set(goldm["EXPIRY_DT"]) == {
        pd.Timestamp("2015-07-03"),
        pd.Timestamp("2015-08-05"),
        pd.Timestamp("2015-09-04"),
    }

    near = goldm[goldm["EXPIRY_DT"] == pd.Timestamp("2015-07-03")].iloc[0]
    assert near["OPEN"] == pytest.approx(26795.0)
    assert near["HIGH"] == pytest.approx(26997.0)
    assert near["LOW"] == pytest.approx(26625.0)
    assert near["CLOSE"] == pytest.approx(26915.0)

    # Symbol must be stripped -- the raw JSON pads it with trailing spaces.
    assert "GOLDM        " not in set(df["SYMBOL"])


def test_parse_datewise_response_handles_documented_no_data_response():
    # Task 1's investigation confirmed 17 Nov 2003 is a real no-trading day
    # and that MCX signals this as IsSuccess:false + "No data found.", not
    # a silent empty array -- this is the exact shape that response takes.
    data = {"IsSuccess": False, "Message": "No data found.", "Data": None, "Metadata": None}
    df = _parse_datewise_response(data)

    assert df.empty
    assert list(df.columns) == MCX_BHAVCOPY_COLUMNS


# ---------------------------------------------------------------------------
# fetch_mcx_bhavcopy -- caching behavior (mirrors nse_bhavcopy's test)
# ---------------------------------------------------------------------------

def test_fetch_mcx_bhavcopy_caches_and_does_not_refetch(isolated_data_store, monkeypatch):
    data = _load_fixture("futcom_2015-06-15.json")

    call_count = {"n": 0}

    def fake_download_and_parse(trading_date):
        call_count["n"] += 1
        return _parse_datewise_response(data)

    monkeypatch.setattr(mcx_bhavcopy, "_download_and_parse", fake_download_and_parse)

    trading_date = date(2015, 6, 15)
    first = fetch_mcx_bhavcopy(trading_date)
    second = fetch_mcx_bhavcopy(trading_date)

    assert call_count["n"] == 1  # second call served from the on-disk parquet cache
    assert not first.empty
    pd.testing.assert_frame_equal(
        first.reset_index(drop=True), second.reset_index(drop=True), check_dtype=False
    )


@pytest.fixture
def reset_failure_counter(monkeypatch):
    monkeypatch.setattr(mcx_bhavcopy, "_consecutive_fetch_failures", 0)


def test_fetch_mcx_bhavcopy_does_not_cache_failed_fetch(isolated_data_store, monkeypatch, reset_failure_counter):
    # A fetch that exhausted all retries (_download_and_parse -> None, e.g. a
    # persistent 403) must NOT be cached as "no data": the next call retries.
    call_count = {"n": 0}
    data = _load_fixture("futcom_2015-06-15.json")

    def fake_download_and_parse(trading_date):
        call_count["n"] += 1
        return None if call_count["n"] == 1 else _parse_datewise_response(data)

    monkeypatch.setattr(mcx_bhavcopy, "_download_and_parse", fake_download_and_parse)

    trading_date = date(2015, 6, 15)
    first = fetch_mcx_bhavcopy(trading_date)
    assert first.empty
    assert list(first.columns) == MCX_BHAVCOPY_COLUMNS
    assert not mcx_bhavcopy._parsed_cache_path(trading_date).exists()

    second = fetch_mcx_bhavcopy(trading_date)
    assert call_count["n"] == 2  # retried, not served a bad cached empty result
    assert not second.empty
    assert mcx_bhavcopy._parsed_cache_path(trading_date).exists()


def test_fetch_mcx_bhavcopy_caches_genuine_no_data_response(isolated_data_store, monkeypatch, reset_failure_counter):
    no_data_response = {"IsSuccess": False, "Message": "No data found.", "Data": None, "Metadata": None}
    call_count = {"n": 0}

    def fake_download_and_parse(trading_date):
        call_count["n"] += 1
        return _parse_datewise_response(no_data_response)

    monkeypatch.setattr(mcx_bhavcopy, "_download_and_parse", fake_download_and_parse)

    trading_date = date(2003, 11, 17)
    assert fetch_mcx_bhavcopy(trading_date).empty
    assert mcx_bhavcopy._parsed_cache_path(trading_date).exists()
    assert fetch_mcx_bhavcopy(trading_date).empty
    assert call_count["n"] == 1  # confirmed-empty day served from cache


def test_fetch_mcx_bhavcopy_aborts_after_consecutive_failures(isolated_data_store, monkeypatch, reset_failure_counter):
    monkeypatch.setattr(mcx_bhavcopy, "_download_and_parse", lambda d: None)
    limit = mcx_bhavcopy.MCX_MAX_CONSECUTIVE_FETCH_FAILURES

    for day in range(1, limit):
        assert fetch_mcx_bhavcopy(date(2015, 6, day)).empty
    with pytest.raises(mcx_bhavcopy.MCXAccessBlockedError):
        fetch_mcx_bhavcopy(date(2015, 6, limit))


def test_fetch_mcx_bhavcopy_success_resets_failure_counter(isolated_data_store, monkeypatch, reset_failure_counter):
    # Isolated failures interleaved with successes (and genuine no-data days)
    # never trip the guard.
    no_data = _parse_datewise_response({"IsSuccess": False, "Message": "No data found.", "Data": None})
    limit = mcx_bhavcopy.MCX_MAX_CONSECUTIVE_FETCH_FAILURES

    def fake_download_and_parse(trading_date):
        return None if trading_date.day % limit else no_data

    monkeypatch.setattr(mcx_bhavcopy, "_download_and_parse", fake_download_and_parse)
    for day in range(1, 29):
        fetch_mcx_bhavcopy(date(2015, 6, day))  # must not raise
    assert mcx_bhavcopy._consecutive_fetch_failures < limit


# ---------------------------------------------------------------------------
# get_historical_commodity_bars -- the brief's required test cases
# ---------------------------------------------------------------------------

def test_get_historical_commodity_bars_parses_real_fixture(isolated_data_store, monkeypatch):
    data = _load_fixture("futcom_2015-06-15.json")
    parsed = _parse_datewise_response(data)

    def fake_fetch(trading_date):
        trading_date = pd.Timestamp(trading_date).date()
        if trading_date == date(2015, 6, 15):
            return parsed, True
        return pd.DataFrame(columns=MCX_BHAVCOPY_COLUMNS), True

    monkeypatch.setattr(mcx_bhavcopy, "fetch_mcx_bhavcopy_with_status", fake_fetch)

    df = get_historical_commodity_bars("GOLDM", "2015-06-15", "2015-06-15")

    assert not df.empty
    assert {"open", "high", "low", "close", "lot_size"}.issubset(df.columns)
    # 3 live GOLDM contracts that day -- one row per contract, same date.
    assert len(df) == 3
    assert (df.index == pd.Timestamp("2015-06-15")).all()
    assert all(df["lot_size"] == 100)  # GOLDM's static lot size (mcx_instruments.GOLDM_LOT_SIZE)
    assert all(df["price_quote_unit"] == 10)  # GOLDM quoted per 10 g (mcx_instruments.GOLDM_PRICE_QUOTE_UNIT)


def test_get_historical_commodity_bars_empty_on_no_data_day(isolated_data_store, monkeypatch):
    # 17 Nov 2003 -- Task 1's confirmed real no-trading day (not a weekend:
    # 17 Nov 2003 was a Monday). Simulates MCX's documented
    # IsSuccess:false/"No data found." response for that exact date.
    no_data_response = {"IsSuccess": False, "Message": "No data found.", "Data": None, "Metadata": None}

    def fake_fetch(trading_date):
        return _parse_datewise_response(no_data_response), True

    monkeypatch.setattr(mcx_bhavcopy, "fetch_mcx_bhavcopy_with_status", fake_fetch)

    df = get_historical_commodity_bars("GOLDM", "2003-11-17", "2003-11-17")

    assert df.empty


def test_get_historical_commodity_bars_rejects_inverted_range():
    with pytest.raises(ValueError):
        get_historical_commodity_bars("GOLDM", "2024-03-01", "2024-01-01")


def test_get_historical_commodity_bars_rejects_unsupported_symbol(isolated_data_store, monkeypatch):
    # ALUMINI is a real symbol present in the futcom_2015-06-15.json fixture
    # (confirmed live MCX data), but it is not one of the four commodities in
    # _LOT_SIZES. get_historical_commodity_bars must raise rather than
    # silently return lot_size = pd.NA, matching
    # mcx_instruments.get_commodity_contracts's posture for the same
    # unsupported-symbol condition.
    data = _load_fixture("futcom_2015-06-15.json")
    parsed = _parse_datewise_response(data)

    def fake_fetch(trading_date):
        trading_date = pd.Timestamp(trading_date).date()
        if trading_date == date(2015, 6, 15):
            return parsed, True
        return pd.DataFrame(columns=MCX_BHAVCOPY_COLUMNS), True

    monkeypatch.setattr(mcx_bhavcopy, "fetch_mcx_bhavcopy_with_status", fake_fetch)

    with pytest.raises(ValueError):
        get_historical_commodity_bars("ALUMINI", "2015-06-15", "2015-06-15")


# ---------------------------------------------------------------------------
# _fetch_date_wise_json -- tested DIRECTLY (HTML-200 trap, retries, failures)
# ---------------------------------------------------------------------------

import requests  # noqa: E402

HTML_404_BODY = "<!DOCTYPE html><html><head><title>404</title></head><body>Page not found</body></html>"


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            # What requests does for a non-JSON body (e.g. MCX's HTML 404 page).
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


class FakeSession:
    """Replays a scripted sequence of responses (or exceptions) and records
    every request made."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


GENUINE = {"IsSuccess": True, "Message": "", "Data": [], "Metadata": None}
NO_DATA = {"IsSuccess": False, "Message": "No data found.", "Data": None, "Metadata": None}
HTML_TRAP = FakeResponse(200, body=None, text=HTML_404_BODY)


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(mcx_bhavcopy.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(mcx_bhavcopy, "_throttle", lambda: None)
    return sleeps


def test_fetch_json_sends_futcom_and_dd_mm_yyyy_date(no_sleep):
    session = FakeSession([FakeResponse(200, GENUINE)])
    assert mcx_bhavcopy._fetch_date_wise_json(date(2015, 6, 5), session) == GENUINE
    url, params = session.calls[0]
    assert url == mcx_bhavcopy._DATEWISE_URL
    assert params == {"InstrumentName": "FUTCOM", "fromDate": "05/06/2015"}


def test_fetch_json_html_200_trap_is_a_failure_not_data(no_sleep):
    # HTTP 200 with MCX's HTML 404 page as the body: must NOT be trusted.
    session = FakeSession([HTML_TRAP] * mcx_bhavcopy._MAX_RETRIES)
    assert mcx_bhavcopy._fetch_date_wise_json(date(2015, 6, 15), session) is None
    assert len(session.calls) == mcx_bhavcopy._MAX_RETRIES  # retried every attempt


def test_fetch_json_recovers_after_html_trap(no_sleep):
    session = FakeSession([HTML_TRAP, FakeResponse(200, GENUINE)])
    assert mcx_bhavcopy._fetch_date_wise_json(date(2015, 6, 15), session) == GENUINE
    assert len(session.calls) == 2


def test_fetch_json_without_is_success_key_is_a_failure(no_sleep):
    session = FakeSession([FakeResponse(200, {"error": "something else"})] * mcx_bhavcopy._MAX_RETRIES)
    assert mcx_bhavcopy._fetch_date_wise_json(date(2015, 6, 15), session) is None
    assert len(session.calls) == mcx_bhavcopy._MAX_RETRIES


def test_fetch_json_genuine_no_data_is_returned_not_retried(no_sleep):
    session = FakeSession([FakeResponse(200, NO_DATA)])
    assert mcx_bhavcopy._fetch_date_wise_json(date(2003, 11, 17), session) == NO_DATA
    assert len(session.calls) == 1
    assert no_sleep == []


def test_fetch_json_403_retries_with_linear_backoff_then_gives_up(no_sleep, loguru_warnings):
    session = FakeSession([FakeResponse(403, body=None, text="Access Denied")] * mcx_bhavcopy._MAX_RETRIES)
    assert mcx_bhavcopy._fetch_date_wise_json(date(2015, 11, 12), session) is None
    assert len(session.calls) == mcx_bhavcopy._MAX_RETRIES
    backoff = mcx_bhavcopy._RETRY_BACKOFF_SECONDS
    assert no_sleep == [backoff * a for a in range(1, mcx_bhavcopy._MAX_RETRIES + 1)]
    assert any("Giving up" in m and "403" in m for m in loguru_warnings)


def test_fetch_json_request_exception_then_success(no_sleep):
    session = FakeSession([requests.ConnectionError("reset"), FakeResponse(200, GENUINE)])
    assert mcx_bhavcopy._fetch_date_wise_json(date(2015, 6, 15), session) == GENUINE
    assert len(session.calls) == 2


def test_persistent_403_through_real_fetch_path_counts_failures_and_trips_breaker(
        isolated_data_store, monkeypatch, no_sleep, reset_failure_counter):
    """Exercises _fetch_date_wise_json -> _download_and_parse ->
    fetch_mcx_bhavcopy_with_status unpatched (only the session is faked):
    each 403'd date is a failure (not cached, not "no data"), and the
    circuit-breaker trips at the configured limit."""
    limit = mcx_bhavcopy.MCX_MAX_CONSECUTIVE_FETCH_FAILURES
    monkeypatch.setattr(mcx_bhavcopy, "_get_session",
                        lambda: FakeSession([FakeResponse(403, text="Access Denied")] * mcx_bhavcopy._MAX_RETRIES))
    days = [date(2015, 6, d) for d in (1, 2, 3, 4, 5)]
    for d in days[:limit - 1]:
        df, ok = mcx_bhavcopy.fetch_mcx_bhavcopy_with_status(d)
        assert not ok and df.empty
        assert not mcx_bhavcopy._parsed_cache_path(d).exists()
    assert mcx_bhavcopy._consecutive_fetch_failures == limit - 1
    with pytest.raises(mcx_bhavcopy.MCXAccessBlockedError):
        mcx_bhavcopy.fetch_mcx_bhavcopy_with_status(days[limit - 1])


def test_html_trap_through_real_fetch_path_is_not_cached_as_no_data(
        isolated_data_store, monkeypatch, no_sleep, reset_failure_counter):
    monkeypatch.setattr(mcx_bhavcopy, "_get_session", lambda: FakeSession([HTML_TRAP] * mcx_bhavcopy._MAX_RETRIES))
    df, ok = mcx_bhavcopy.fetch_mcx_bhavcopy_with_status(date(2015, 6, 15))
    assert not ok and df.empty
    assert not mcx_bhavcopy._parsed_cache_path(date(2015, 6, 15)).exists()
    assert not mcx_bhavcopy._raw_json_cache_path(date(2015, 6, 15)).exists()


def test_unparseable_genuine_json_is_a_failure_not_a_holiday(
        isolated_data_store, monkeypatch, no_sleep, reset_failure_counter):
    # Valid JSON with IsSuccess, but rows missing required fields.
    bad = {"IsSuccess": True, "Message": "", "Data": [{"Symbol": "GOLDM", "InstrumentName": "FUTCOM"}]}
    monkeypatch.setattr(mcx_bhavcopy, "_get_session", lambda: FakeSession([FakeResponse(200, bad)]))
    df, ok = mcx_bhavcopy.fetch_mcx_bhavcopy_with_status(date(2015, 6, 15))
    assert not ok and df.empty
    assert not mcx_bhavcopy._parsed_cache_path(date(2015, 6, 15)).exists()


def test_genuine_no_data_through_real_fetch_path_is_ok_and_cached(
        isolated_data_store, monkeypatch, no_sleep, reset_failure_counter):
    monkeypatch.setattr(mcx_bhavcopy, "_get_session", lambda: FakeSession([FakeResponse(200, NO_DATA)]))
    df, ok = mcx_bhavcopy.fetch_mcx_bhavcopy_with_status(date(2003, 11, 17))
    assert ok and df.empty
    assert mcx_bhavcopy._parsed_cache_path(date(2003, 11, 17)).exists()


# ---------------------------------------------------------------------------
# get_historical_commodity_bars -- failed vs. genuinely-empty dates
# ---------------------------------------------------------------------------

def test_historical_bars_distinguishes_failed_from_genuine_no_data(isolated_data_store, monkeypatch,
                                                                  loguru_warnings):
    parsed = _parse_datewise_response(_load_fixture("futcom_2015-06-15.json"))
    empty = pd.DataFrame(columns=MCX_BHAVCOPY_COLUMNS)
    # Mon 15 Jun 2015: data. Tue 16: genuine holiday. Wed 17 + Thu 18: FAILED.
    # Fri 19: data. Sat/Sun 20-21: never requested.
    script = {
        date(2015, 6, 15): (parsed, True), date(2015, 6, 16): (empty, True),
        date(2015, 6, 17): (empty, False), date(2015, 6, 18): (empty, False),
        date(2015, 6, 19): (parsed, True),
    }
    requested = []

    def fake_fetch(d):
        d = pd.Timestamp(d).date()
        requested.append(d)
        return script[d]

    monkeypatch.setattr(mcx_bhavcopy, "fetch_mcx_bhavcopy_with_status", fake_fetch)
    report = mcx_bhavcopy.BhavcopyFetchReport()
    df = get_historical_commodity_bars("GOLDM", "2015-06-15", "2015-06-21", fetch_report=report)

    assert requested == sorted(script)  # weekends skipped
    assert report.weekdays_requested == 5
    assert report.dates_with_data == 2
    assert report.genuine_no_data_dates == [date(2015, 6, 16)]
    assert report.failed_dates == [date(2015, 6, 17), date(2015, 6, 18)]
    assert report.has_failures and report.failed_count == 2
    assert set(df.index.date) == {date(2015, 6, 15), date(2015, 6, 19)}
    assert any("2 FAILED fetch(es)" in m and "2015-06-17" in m for m in loguru_warnings)


def test_historical_bars_clean_range_logs_no_failure_warning(isolated_data_store, monkeypatch, loguru_warnings):
    parsed = _parse_datewise_response(_load_fixture("futcom_2015-06-15.json"))
    monkeypatch.setattr(mcx_bhavcopy, "fetch_mcx_bhavcopy_with_status", lambda d: (parsed, True))
    report = mcx_bhavcopy.BhavcopyFetchReport()
    get_historical_commodity_bars("GOLDM", "2015-06-15", "2015-06-15", fetch_report=report)
    assert not report.has_failures
    assert not any("FAILED" in m for m in loguru_warnings)


def test_historical_bars_report_is_filled_even_when_breaker_trips(isolated_data_store, monkeypatch):
    calls = {"n": 0}

    def fake_fetch(d):
        calls["n"] += 1
        if calls["n"] == 3:
            raise mcx_bhavcopy.MCXAccessBlockedError("blocked")
        return pd.DataFrame(columns=MCX_BHAVCOPY_COLUMNS), False

    monkeypatch.setattr(mcx_bhavcopy, "fetch_mcx_bhavcopy_with_status", fake_fetch)
    report = mcx_bhavcopy.BhavcopyFetchReport()
    with pytest.raises(mcx_bhavcopy.MCXAccessBlockedError):
        get_historical_commodity_bars("GOLDM", "2015-06-15", "2015-06-19", fetch_report=report)
    assert report.failed_dates == [date(2015, 6, 15), date(2015, 6, 16)]


def test_fetch_mcx_bhavcopy_wrapper_drops_status(isolated_data_store, monkeypatch):
    monkeypatch.setattr(mcx_bhavcopy, "fetch_mcx_bhavcopy_with_status",
                        lambda d: (pd.DataFrame(columns=MCX_BHAVCOPY_COLUMNS), False))
    assert fetch_mcx_bhavcopy(date(2015, 6, 15)).empty
