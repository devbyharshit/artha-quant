"""Tests for the futures data layer: the approximated-calendar contract
resolver (data/loaders/futures_instruments.py) and the rollover-selection /
overlay-building logic (data/loaders/sync_futures_history.py).

No real network calls -- Kite is always a small stub object or absent (None),
per docs/12-futures-trading-plan.md's "no historical NFO catalog" finding."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from configs.config import reset_settings
from data.loaders.futures_instruments import (
    FuturesContract,
    _contract_tradingsymbol,
    _last_thursday,
    get_futures_contracts,
)
from data.loaders.sync_futures_history import (
    OVERLAY_COLUMNS,
    build_futures_overlay,
    select_active_contract,
)
import data.store.parquet_store as parquet_store_module
from data.store.parquet_store import ParquetStore


# ---------------------------------------------------------------------------
# _last_thursday / tradingsymbol convention
# ---------------------------------------------------------------------------

def test_last_thursday_of_month_matches_known_nse_expiries():
    # NSE's real Nov/Dec 2026 F&O expiries (verified live against Kite's
    # catalog, see module docstring) are 2026-11-26 and 2026-12-31 -- both
    # happen to actually be the plain last-Thursday-of-month, no holiday shift.
    assert _last_thursday(2026, 11) == date(2026, 11, 26)
    assert _last_thursday(2026, 12) == date(2026, 12, 31)


def test_last_thursday_handles_month_ending_on_thursday():
    # September 2027's last day (30th) is itself a Thursday.
    assert _last_thursday(2027, 9) == date(2027, 9, 30)


def test_contract_tradingsymbol_matches_kite_convention():
    assert _contract_tradingsymbol("RELIANCE", date(2026, 11, 26)) == "RELIANCE26NOVFUT"
    assert _contract_tradingsymbol("reliance", date(2027, 1, 25)) == "RELIANCE27JANFUT"


# ---------------------------------------------------------------------------
# get_futures_contracts (approximated calendar, no live catalog)
# ---------------------------------------------------------------------------

def test_get_futures_contracts_without_kite_uses_fallback_lot_size():
    contracts = get_futures_contracts("RELIANCE", "2026-11-01", "2026-12-15", kite=None)

    assert len(contracts) >= 2
    for c in contracts:
        assert c.name == "RELIANCE"
        assert c.instrument_token is None  # no live catalog reachable at all
        assert c.lot_size == 1  # DEFAULT_LOT_SIZE_FALLBACK, documented approximation
        assert isinstance(c.expiry, pd.Timestamp)


def test_get_futures_contracts_includes_lead_month_contract():
    # Requesting a window starting mid-November should still include the
    # October-expiry contract, since that's the one active at the start date.
    contracts = get_futures_contracts("RELIANCE", "2026-11-15", "2026-11-20", kite=None)
    tradingsymbols = {c.tradingsymbol for c in contracts}
    assert "RELIANCE26OCTFUT" in tradingsymbols
    assert "RELIANCE26NOVFUT" in tradingsymbols


def test_get_futures_contracts_rejects_inverted_range():
    with pytest.raises(ValueError):
        get_futures_contracts("RELIANCE", "2026-12-01", "2026-11-01", kite=None)


class FakeKite:
    """Stub standing in for KiteConnect -- only `.instruments()` is used by this module."""

    def __init__(self, nfo_rows):
        self._nfo_rows = nfo_rows
        self.call_count = 0

    def instruments(self, exchange):
        self.call_count += 1
        assert exchange == "NFO"
        return self._nfo_rows


def _sample_nfo_rows():
    return [
        {
            "tradingsymbol": "RELIANCE26NOVFUT",
            "instrument_token": 111,
            "name": "RELIANCE",
            "expiry": date(2026, 11, 26),
            "lot_size": 500,
            "instrument_type": "FUT",
            "segment": "NFO-FUT",
            "exchange": "NFO",
        },
        {
            "tradingsymbol": "RELIANCE26DECFUT",
            "instrument_token": 112,
            "name": "RELIANCE",
            "expiry": date(2026, 12, 31),
            "lot_size": 500,
            "instrument_type": "FUT",
            "segment": "NFO-FUT",
            "exchange": "NFO",
        },
        {
            "tradingsymbol": "RELIANCE26NOV2900CE",
            "instrument_token": 999,
            "name": "RELIANCE",
            "expiry": date(2026, 11, 26),
            "lot_size": 500,
            "instrument_type": "CE",
            "segment": "NFO-OPT",
            "exchange": "NFO",
        },
    ]


@pytest.fixture
def isolated_data_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_STORE_DIR", str(tmp_path))
    reset_settings()
    # get_parquet_store() is a process-wide singleton bound to whatever
    # DATA_STORE_DIR was active the first time it was constructed -- reset it
    # so this test's ParquetStore actually points at this test's tmp_path.
    parquet_store_module._parquet_store = None
    yield tmp_path
    parquet_store_module._parquet_store = None
    reset_settings()


def test_get_futures_contracts_resolves_token_for_live_listed_contract(isolated_data_store):
    kite = FakeKite(_sample_nfo_rows())
    contracts = get_futures_contracts("RELIANCE", "2026-11-01", "2026-12-15", kite=kite)

    by_symbol = {c.tradingsymbol: c for c in contracts}
    assert by_symbol["RELIANCE26NOVFUT"].instrument_token == 111
    assert by_symbol["RELIANCE26NOVFUT"].lot_size == 500
    # Options rows (CE) must never leak in as a FUT contract.
    assert "RELIANCE26NOV2900CE" not in by_symbol

    # Older, non-live-listed contract in the window still appears (approximated),
    # just without a resolvable token, and using the current lot size.
    assert by_symbol["RELIANCE26OCTFUT"].instrument_token is None
    assert by_symbol["RELIANCE26OCTFUT"].lot_size == 500


def test_get_futures_contracts_caches_nfo_catalog(isolated_data_store):
    kite = FakeKite(_sample_nfo_rows())
    get_futures_contracts("RELIANCE", "2026-11-01", "2026-11-05", kite=kite)
    get_futures_contracts("RELIANCE", "2026-11-01", "2026-11-05", kite=kite)

    assert kite.call_count == 1  # second call served from disk cache
    cache_file = isolated_data_store / "instruments_NFO.json"
    assert cache_file.exists()


def test_get_futures_contracts_degrades_gracefully_on_kite_failure(isolated_data_store):
    class BrokenKite:
        def instruments(self, exchange):
            raise RuntimeError("session expired")

    contracts = get_futures_contracts("RELIANCE", "2026-11-01", "2026-11-05", kite=BrokenKite())
    assert len(contracts) >= 1
    assert all(c.instrument_token is None for c in contracts)
    assert all(c.lot_size == 1 for c in contracts)


# ---------------------------------------------------------------------------
# select_active_contract -- the rollover rule (docs/12 §3.1)
# ---------------------------------------------------------------------------

def _contract(tradingsymbol, expiry_str, lot_size=500, token=1):
    return FuturesContract(
        tradingsymbol=tradingsymbol,
        instrument_token=token,
        lot_size=lot_size,
        expiry=pd.Timestamp(expiry_str),
        name="RELIANCE",
    )


@pytest.fixture
def three_monthly_contracts():
    # Three contracts roughly a month apart, all expiring on a Thursday.
    return [
        _contract("RELIANCE26OCTFUT", "2026-10-29", token=1),
        _contract("RELIANCE26NOVFUT", "2026-11-26", token=2),
        _contract("RELIANCE26DECFUT", "2026-12-31", token=3),
    ]


def test_select_active_contract_picks_near_month_well_before_expiry(three_monthly_contracts):
    contract = select_active_contract(pd.Timestamp("2026-10-01"), three_monthly_contracts)
    assert contract.tradingsymbol == "RELIANCE26OCTFUT"


def test_select_active_contract_rolls_over_once_inside_buffer(three_monthly_contracts):
    # 2026-10-29 is a Thursday; 3 trading days before it is 2026-10-26 (Monday).
    expiry = pd.Timestamp("2026-10-29").date()
    boundary_date = pd.Timestamp(np.busday_offset(expiry, -3, roll="backward"))

    # One trading day further out (still > 3 trading days away): stay on near month.
    still_near = pd.Timestamp(np.busday_offset(boundary_date.date(), -1, roll="backward"))
    assert select_active_contract(still_near, three_monthly_contracts).tradingsymbol == "RELIANCE26OCTFUT"

    # Exactly at the 3-trading-day boundary: per the rule ("within 3 trading
    # days" triggers rollover), this must already be on the next contract.
    assert select_active_contract(boundary_date, three_monthly_contracts).tradingsymbol == "RELIANCE26NOVFUT"

    # The day after the boundary (even closer to expiry): still next contract.
    closer = pd.Timestamp(np.busday_offset(boundary_date.date(), 1, roll="forward"))
    assert select_active_contract(closer, three_monthly_contracts).tradingsymbol == "RELIANCE26NOVFUT"


def test_select_active_contract_on_expiry_day_itself_rolls_over(three_monthly_contracts):
    assert select_active_contract(pd.Timestamp("2026-10-29"), three_monthly_contracts).tradingsymbol == "RELIANCE26NOVFUT"


def test_select_active_contract_rolls_through_multiple_months(three_monthly_contracts):
    contract = select_active_contract(pd.Timestamp("2026-11-01"), three_monthly_contracts)
    assert contract.tradingsymbol == "RELIANCE26NOVFUT"


def test_select_active_contract_holds_furthest_when_all_inside_buffer(three_monthly_contracts):
    # Past every contract's rollover point (even the far month) -- nothing
    # better to roll into, so the furthest-dated contract is held.
    contract = select_active_contract(pd.Timestamp("2026-12-30"), three_monthly_contracts)
    assert contract.tradingsymbol == "RELIANCE26DECFUT"


def test_select_active_contract_accepts_tz_aware_as_of(three_monthly_contracts):
    # Real cash-equity bar indices are tz-aware (Asia/Kolkata); contract
    # expiries built from a plain calendar date are tz-naive. Comparing the
    # two directly used to raise TypeError -- this must just work.
    as_of = pd.Timestamp("2026-10-01", tz="Asia/Kolkata")
    contract = select_active_contract(as_of, three_monthly_contracts)
    assert contract.tradingsymbol == "RELIANCE26OCTFUT"


def test_select_active_contract_returns_none_when_no_contract_covers_date():
    contracts = [_contract("RELIANCE26OCTFUT", "2026-10-29")]
    assert select_active_contract(pd.Timestamp("2026-11-15"), contracts) is None


def test_select_active_contract_respects_custom_buffer(three_monthly_contracts):
    # With a 0-day buffer, only expiry day itself forces rollover.
    day_before_expiry = pd.Timestamp("2026-10-28")
    contract = select_active_contract(day_before_expiry, three_monthly_contracts, rollover_buffer_days=0)
    assert contract.tradingsymbol == "RELIANCE26OCTFUT"


# ---------------------------------------------------------------------------
# build_futures_overlay (NSE-bhavcopy-sourced, docs/12 §5 2026-11-10 follow-up)
# ---------------------------------------------------------------------------
# `get_historical_futures_bars` is monkeypatched out everywhere below -- no
# real network calls. Kite, when supplied, is only ever consulted for the
# current-lot-size fallback (via `get_futures_contracts`), never for prices.

import data.loaders.sync_futures_history as sync_futures_history_module


def _bhav_bars(rows):
    """rows: list of (date_str, tradingsymbol, expiry_str, open, high, low, close, lot_size_or_None)."""
    records = []
    for d, tsym, expiry, o, h, l, c, lot in rows:
        records.append({
            "date": pd.Timestamp(d),
            "tradingsymbol": tsym,
            "expiry": pd.Timestamp(expiry),
            "open": o, "high": h, "low": l, "close": c,
            "lot_size": pd.NA if lot is None else lot,
        })
    df = pd.DataFrame(records, columns=["date", "tradingsymbol", "expiry", "open", "high", "low", "close", "lot_size"])
    return df.set_index("date").sort_index()


def test_build_futures_overlay_assembles_table_across_rollover(isolated_data_store, monkeypatch):
    cash_index = pd.bdate_range("2026-11-01", "2026-12-02", name="timestamp")

    rows = []
    for d in pd.bdate_range("2026-11-01", "2026-12-02"):
        rows.append((d, "RELIANCE26NOVFUT", "2026-11-26", 2500.0, 2510.0, 2490.0, 2505.0, 500))
        rows.append((d, "RELIANCE26DECFUT", "2026-12-31", 2600.0, 2610.0, 2590.0, 2605.0, 500))
    bhav = _bhav_bars(rows)

    monkeypatch.setattr(
        sync_futures_history_module,
        "get_historical_futures_bars",
        lambda symbol, start_date, end_date: bhav,
    )

    overlay = build_futures_overlay("RELIANCE", cash_index, kite=None)

    assert list(overlay.columns) == OVERLAY_COLUMNS
    assert overlay.index.isin(cash_index).all()
    assert not overlay.empty

    # Rollover should actually have switched contracts somewhere in the window.
    assert overlay["tradingsymbol"].nunique() >= 2
    assert set(overlay["tradingsymbol"].unique()) <= {"RELIANCE26NOVFUT", "RELIANCE26DECFUT"}

    # Post-rollover dates must carry the December contract, not November.
    rollover_boundary = pd.Timestamp(np.busday_offset(date(2026, 11, 26), -3, roll="backward"))
    on_or_after = overlay.index[overlay.index >= rollover_boundary]
    assert (overlay.loc[on_or_after, "tradingsymbol"] == "RELIANCE26DECFUT").all()
    # Real lot size carried through from the bhavcopy row, not an approximation.
    assert (overlay["lot_size"] == 500).all()


def test_build_futures_overlay_skips_dates_with_no_contract_data(isolated_data_store, monkeypatch):
    cash_index = pd.bdate_range("2026-11-01", "2026-11-10", name="timestamp")
    monkeypatch.setattr(
        sync_futures_history_module,
        "get_historical_futures_bars",
        lambda symbol, start_date, end_date: pd.DataFrame(
            columns=["tradingsymbol", "expiry", "open", "high", "low", "close", "lot_size"]
        ),
    )

    overlay = build_futures_overlay("RELIANCE", cash_index, kite=None)
    assert overlay.empty
    assert list(overlay.columns) == OVERLAY_COLUMNS


def test_build_futures_overlay_empty_cash_index_returns_empty_frame(isolated_data_store):
    overlay = build_futures_overlay("RELIANCE", pd.DatetimeIndex([]), kite=None)
    assert overlay.empty
    assert list(overlay.columns) == OVERLAY_COLUMNS


def test_build_futures_overlay_fills_missing_lot_size_with_current_lot_size(isolated_data_store, monkeypatch):
    # Pre-2024-07-08-style bhavcopy row: no lot size at all (pd.NA), as NSE's
    # legacy archive format never published one (nse_bhavcopy.py docstring).
    cash_index = pd.bdate_range("2023-06-01", "2023-06-05", name="timestamp")
    rows = [(d, "RELIANCE23JUNFUT", "2023-06-29", 2500.0, 2510.0, 2490.0, 2505.0, None)
            for d in pd.bdate_range("2023-06-01", "2023-06-05")]
    bhav = _bhav_bars(rows)

    monkeypatch.setattr(
        sync_futures_history_module,
        "get_historical_futures_bars",
        lambda symbol, start_date, end_date: bhav,
    )
    kite = FakeKite(_sample_nfo_rows())  # live catalog's current lot size is 500

    overlay = build_futures_overlay("RELIANCE", cash_index, kite=kite)
    assert not overlay.empty
    assert (overlay["lot_size"] == 500).all()


def test_build_futures_overlay_falls_back_to_default_lot_size_without_kite(isolated_data_store, monkeypatch):
    cash_index = pd.bdate_range("2023-06-01", "2023-06-05", name="timestamp")
    rows = [(d, "RELIANCE23JUNFUT", "2023-06-29", 2500.0, 2510.0, 2490.0, 2505.0, None)
            for d in pd.bdate_range("2023-06-01", "2023-06-05")]
    bhav = _bhav_bars(rows)

    monkeypatch.setattr(
        sync_futures_history_module,
        "get_historical_futures_bars",
        lambda symbol, start_date, end_date: bhav,
    )

    overlay = build_futures_overlay("RELIANCE", cash_index, kite=None)
    assert not overlay.empty
    assert (overlay["lot_size"] == 1).all()  # DEFAULT_LOT_SIZE_FALLBACK, no live catalog reachable
