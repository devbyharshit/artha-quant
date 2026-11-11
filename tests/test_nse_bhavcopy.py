"""Tests for the NSE F&O bhavcopy ingester (data/loaders/nse_bhavcopy.py),
docs/12-futures-trading-plan.md §5 follow-on.

No real network calls -- `_download_and_parse` / the network-hitting parts
of `fetch_fo_bhavcopy` are monkeypatched out. Format-parsing correctness is
tested directly against real (trimmed) NSE bhavcopy fixtures saved under
tests/fixtures/nse_bhavcopy/ -- one legacy-format file (2023-06-15) and one
UDiFF-format file (2025-10-24), both downloaded live from NSE's archives
during this module's build."""
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from configs.config import reset_settings
import data.store.parquet_store as parquet_store_module
from data.loaders import nse_bhavcopy
from data.loaders.nse_bhavcopy import (
    BHAVCOPY_COLUMNS,
    _parse_legacy_csv,
    _parse_udiff_csv,
    fetch_fo_bhavcopy,
    get_historical_futures_bars,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "nse_bhavcopy"


@pytest.fixture
def isolated_data_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_STORE_DIR", str(tmp_path))
    reset_settings()
    parquet_store_module._parquet_store = None
    yield tmp_path
    parquet_store_module._parquet_store = None
    reset_settings()


# ---------------------------------------------------------------------------
# Legacy format parsing (fo<DD><MON><YYYY>bhav.csv, pre-2024-07-08)
# ---------------------------------------------------------------------------

def test_parse_legacy_csv_filters_to_futstk_only():
    csv_text = (FIXTURES_DIR / "fo15JUN2023bhav.csv").read_text()
    df = _parse_legacy_csv(csv_text)

    assert list(df.columns) == BHAVCOPY_COLUMNS
    assert set(df["INSTRUMENT"]) == {"FUTSTK"}
    # FUTIDX (NIFTY) and OPTSTK (RELIANCE options) rows must be dropped.
    assert "NIFTY" not in set(df["SYMBOL"])
    assert len(df) == 9  # HDFCBANK x2, INFY x2, RELIANCE x3, TCS x2 -- see fixture
    assert len(df) == len(df[df["INSTRUMENT"] == "FUTSTK"])


def test_parse_legacy_csv_extracts_known_symbol_columns():
    csv_text = (FIXTURES_DIR / "fo15JUN2023bhav.csv").read_text()
    df = _parse_legacy_csv(csv_text)

    reliance_near = df[(df["SYMBOL"] == "RELIANCE") & (df["EXPIRY_DT"] == pd.Timestamp("2023-06-29"))].iloc[0]
    assert reliance_near["OPEN"] == pytest.approx(2552.0)
    assert reliance_near["HIGH"] == pytest.approx(2568.0)
    assert reliance_near["LOW"] == pytest.approx(2542.35)
    assert reliance_near["CLOSE"] == pytest.approx(2557.1)
    # Legacy format never carries lot size -- must be NA, not fabricated.
    assert pd.isna(reliance_near["LOT_SIZE"])


# ---------------------------------------------------------------------------
# UDiFF format parsing (BhavCopy_NSE_FO_0_0_0_<date>_F_0000.csv, 2024-07-08+)
# ---------------------------------------------------------------------------

def test_parse_udiff_csv_filters_to_stock_futures_only():
    csv_text = (FIXTURES_DIR / "BhavCopy_NSE_FO_0_0_0_20251024_F_0000.csv").read_text()
    df = _parse_udiff_csv(csv_text)

    assert list(df.columns) == BHAVCOPY_COLUMNS
    assert set(df["INSTRUMENT"]) == {"FUTSTK"}
    # STO (options), IDF (index futures) rows must be dropped.
    assert "NIFTY" not in set(df["SYMBOL"])
    assert len(df) == 8  # TCS x3, RELIANCE x3, INFY x2 -- see fixture


def test_parse_udiff_csv_extracts_known_symbol_columns_and_real_lot_size():
    csv_text = (FIXTURES_DIR / "BhavCopy_NSE_FO_0_0_0_20251024_F_0000.csv").read_text()
    df = _parse_udiff_csv(csv_text)

    reliance_oct = df[(df["SYMBOL"] == "RELIANCE") & (df["EXPIRY_DT"] == pd.Timestamp("2025-10-28"))].iloc[0]
    assert reliance_oct["OPEN"] == pytest.approx(1447.10)
    assert reliance_oct["HIGH"] == pytest.approx(1459.00)
    assert reliance_oct["LOW"] == pytest.approx(1441.00)
    assert reliance_oct["CLOSE"] == pytest.approx(1450.50)
    # UDiFF carries the real, contract-specific historical lot size.
    assert int(reliance_oct["LOT_SIZE"]) == 500

    tcs_nov = df[(df["SYMBOL"] == "TCS") & (df["EXPIRY_DT"] == pd.Timestamp("2025-11-25"))].iloc[0]
    assert int(tcs_nov["LOT_SIZE"]) == 175


# ---------------------------------------------------------------------------
# fetch_fo_bhavcopy -- empty-result handling, caching
# ---------------------------------------------------------------------------

def test_fetch_fo_bhavcopy_returns_empty_frame_for_no_data_date(isolated_data_store, monkeypatch):
    # Simulate NSE having nothing for this date (weekend/holiday) by making
    # the network-hitting helper return None for both URL attempts.
    monkeypatch.setattr(nse_bhavcopy, "_fetch_zip_bytes", lambda url, session: None)
    monkeypatch.setattr(nse_bhavcopy, "_get_session", lambda: object())

    df = fetch_fo_bhavcopy(date(2023, 6, 18))  # a Sunday

    assert df.empty
    assert list(df.columns) == BHAVCOPY_COLUMNS


def test_fetch_fo_bhavcopy_caches_and_does_not_refetch(isolated_data_store, monkeypatch):
    csv_text = (FIXTURES_DIR / "BhavCopy_NSE_FO_0_0_0_20251024_F_0000.csv").read_text()

    call_count = {"n": 0}

    def fake_download_and_parse(trading_date):
        call_count["n"] += 1
        return _parse_udiff_csv(csv_text)

    monkeypatch.setattr(nse_bhavcopy, "_download_and_parse", fake_download_and_parse)

    trading_date = date(2025, 10, 24)
    first = fetch_fo_bhavcopy(trading_date)
    second = fetch_fo_bhavcopy(trading_date)

    assert call_count["n"] == 1  # second call served from the on-disk parquet cache
    assert not first.empty
    pd.testing.assert_frame_equal(
        first.reset_index(drop=True), second.reset_index(drop=True), check_dtype=False
    )


# ---------------------------------------------------------------------------
# get_historical_futures_bars
# ---------------------------------------------------------------------------

def test_get_historical_futures_bars_assembles_multi_contract_rows(isolated_data_store, monkeypatch):
    csv_text = (FIXTURES_DIR / "BhavCopy_NSE_FO_0_0_0_20251024_F_0000.csv").read_text()
    parsed = _parse_udiff_csv(csv_text)

    def fake_fetch(trading_date):
        trading_date = pd.Timestamp(trading_date).date()
        if trading_date == date(2025, 10, 24):
            return parsed
        return pd.DataFrame(columns=BHAVCOPY_COLUMNS)

    monkeypatch.setattr(nse_bhavcopy, "fetch_fo_bhavcopy", fake_fetch)

    bars = get_historical_futures_bars("RELIANCE", date(2025, 10, 24), date(2025, 10, 24))

    assert list(bars.columns) == ["tradingsymbol", "expiry", "open", "high", "low", "close", "lot_size"]
    # RELIANCE had 3 live contracts that day -- one row per contract, same date.
    assert len(bars) == 3
    assert set(bars["tradingsymbol"]) == {"RELIANCE25OCTFUT", "RELIANCE25NOVFUT", "RELIANCE25DECFUT"}
    assert (bars.index == pd.Timestamp(2025, 10, 24)).all()
    assert all(bars["lot_size"] == 500)


def test_get_historical_futures_bars_skips_weekends_and_no_data_days(isolated_data_store, monkeypatch):
    monkeypatch.setattr(
        nse_bhavcopy, "fetch_fo_bhavcopy", lambda d: pd.DataFrame(columns=BHAVCOPY_COLUMNS)
    )

    bars = get_historical_futures_bars("RELIANCE", date(2025, 10, 20), date(2025, 10, 26))

    assert bars.empty
    assert list(bars.columns) == ["tradingsymbol", "expiry", "open", "high", "low", "close", "lot_size"]


def test_get_historical_futures_bars_rejects_inverted_range():
    with pytest.raises(ValueError):
        get_historical_futures_bars("RELIANCE", date(2025, 10, 26), date(2025, 10, 20))
