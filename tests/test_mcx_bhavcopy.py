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


# ---------------------------------------------------------------------------
# get_historical_commodity_bars -- the brief's required test cases
# ---------------------------------------------------------------------------

def test_get_historical_commodity_bars_parses_real_fixture(isolated_data_store, monkeypatch):
    data = _load_fixture("futcom_2015-06-15.json")
    parsed = _parse_datewise_response(data)

    def fake_fetch(trading_date):
        trading_date = pd.Timestamp(trading_date).date()
        if trading_date == date(2015, 6, 15):
            return parsed
        return pd.DataFrame(columns=MCX_BHAVCOPY_COLUMNS)

    monkeypatch.setattr(mcx_bhavcopy, "fetch_mcx_bhavcopy", fake_fetch)

    df = get_historical_commodity_bars("GOLDM", "2015-06-15", "2015-06-15")

    assert not df.empty
    assert {"open", "high", "low", "close", "lot_size"}.issubset(df.columns)
    # 3 live GOLDM contracts that day -- one row per contract, same date.
    assert len(df) == 3
    assert (df.index == pd.Timestamp("2015-06-15")).all()
    assert all(df["lot_size"] == 100)  # GOLDM's static lot size (mcx_instruments.GOLDM_LOT_SIZE)


def test_get_historical_commodity_bars_empty_on_no_data_day(isolated_data_store, monkeypatch):
    # 17 Nov 2003 -- Task 1's confirmed real no-trading day (not a weekend:
    # 17 Nov 2003 was a Monday). Simulates MCX's documented
    # IsSuccess:false/"No data found." response for that exact date.
    no_data_response = {"IsSuccess": False, "Message": "No data found.", "Data": None, "Metadata": None}

    def fake_fetch(trading_date):
        return _parse_datewise_response(no_data_response)

    monkeypatch.setattr(mcx_bhavcopy, "fetch_mcx_bhavcopy", fake_fetch)

    df = get_historical_commodity_bars("GOLDM", "2003-11-17", "2003-11-17")

    assert df.empty


def test_get_historical_commodity_bars_rejects_inverted_range():
    with pytest.raises(ValueError):
        get_historical_commodity_bars("GOLDM", "2024-03-01", "2024-01-01")
