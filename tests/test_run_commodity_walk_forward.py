"""Direct unit tests for the MCX CLI's data-assembly layer
(backtesting/run_commodity_walk_forward.py): active-contract selection,
the ratio back-adjustment and its roll ratio (where the back-adjustment
zeroing bug lived), and the fetch-failure / lot-size plumbing.

Synthetic multi-contract roll scenarios, no network: the two data-layer
calls `assemble_commodity_data` makes are monkeypatched."""
import pandas as pd
import pytest

from backtesting import run_commodity_walk_forward as rcwf
from backtesting.run_commodity_walk_forward import (
    CONTINUOUS_COLUMNS,
    OVERLAY_COLUMNS,
    _active_contract_rows,
    _back_adjust,
    _roll_ratio,
    assemble_commodity_data,
    build_universe_data,
    data_quality_lines,
)
from data.loaders.mcx_bhavcopy import BhavcopyFetchReport
from data.loaders.mcx_instruments import CommodityContract

DATES = pd.bdate_range("2026-01-05", periods=9)  # d0..d8, Mon 5 Jan .. Thu 15 Jan


def raw_rows(quotes, lot_size=100, price_quote_unit=10):
    """quotes: {tradingsymbol: (expiry_date_index, {date_index: close})} ->
    a get_historical_commodity_bars-shaped frame (one row per live contract
    per day, several rows sharing a date)."""
    rows = []
    for tsym, (exp_i, closes) in quotes.items():
        for d_i, close in closes.items():
            rows.append({
                "date": DATES[d_i], "tradingsymbol": tsym, "expiry": DATES[exp_i],
                "open": close - 1.0, "high": close + 2.0, "low": close - 2.0, "close": float(close),
                "lot_size": lot_size, "price_quote_unit": price_quote_unit,
            })
    return pd.DataFrame(rows).set_index("date").sort_index()


# A (expires d3) -> B (expires d6) -> C (expires far). Each new contract is
# co-quoted with the old one on the old one's last active day, at a different
# price level (contango), so a wrong roll ratio shows up as a wrong return.
A = {0: 100.0, 1: 102.0, 2: 101.0, 3: 104.0}
B = {0: 110.0, 1: 112.0, 2: 111.0, 3: 115.0, 4: 117.0, 5: 116.0, 6: 119.0}
C = {5: 130.0, 6: 133.0, 7: 135.0, 8: 134.0}
QUOTES = {"GOLDMA": (3, A), "GOLDMB": (6, B), "GOLDMC": (8, C)}


@pytest.fixture
def raw():
    return raw_rows(QUOTES)


# --- _active_contract_rows ---------------------------------------------------

def test_active_contract_rows_picks_nearest_unexpired_and_holds_through_expiry(raw):
    active = _active_contract_rows(raw, None)
    assert list(active.index) == list(DATES)
    # Old contract stays listed through its OWN expiry day, switches the day after.
    assert list(active["tradingsymbol"]) == ["GOLDMA"] * 4 + ["GOLDMB"] * 3 + ["GOLDMC"] * 2
    assert active.loc[DATES[3], "close"] == A[3]
    assert active.loc[DATES[4], "close"] == B[4]
    assert active.loc[DATES[7], "close"] == C[7]
    assert list(active.columns) == ["tradingsymbol", "expiry", "open", "high", "low", "close",
                                    "lot_size", "price_quote_unit"]


def test_active_contract_rows_skips_rows_past_expiry():
    # A still printed (stale) a day after its expiry, with no successor yet:
    # that date has no unexpired contract and is dropped, not filled with A.
    raw = raw_rows({"GOLDMA": (1, {0: 100.0, 1: 101.0, 2: 99.0})})
    active = _active_contract_rows(raw, None)
    assert list(active.index) == [DATES[0], DATES[1]]


def test_active_contract_rows_carries_quote_unit_and_applies_lot_override(raw):
    active = _active_contract_rows(raw, 100)
    assert (active["price_quote_unit"] == 10).all()
    assert (active["lot_size"] == 100).all()
    assert (_active_contract_rows(raw_rows(QUOTES, lot_size=7), None)["lot_size"] == 7).all()


def test_active_contract_rows_empty_input_keeps_columns():
    empty = pd.DataFrame(columns=["tradingsymbol", "expiry", "open", "high", "low", "close", "lot_size",
                                  "price_quote_unit"])
    out = _active_contract_rows(empty, None)
    assert out.empty and "price_quote_unit" in out.columns


# --- _roll_ratio ---------------------------------------------------------------

def _closes(raw):
    return {(pd.Timestamp(ts), s): float(c) for ts, s, c in zip(raw.index, raw["tradingsymbol"], raw["close"])}


def test_roll_ratio_uses_both_contracts_on_old_contracts_last_day(raw):
    ratio = _roll_ratio(_closes(raw), list(DATES), "GOLDMA", "GOLDMB", DATES[3], fallback=-1.0)
    assert ratio == pytest.approx(B[3] / A[3])


def test_roll_ratio_falls_back_to_nearest_earlier_common_date(loguru_warnings):
    raw = raw_rows({"GOLDMA": (3, A), "GOLDMB": (6, {1: 112.0, 2: 111.0, 4: 117.0})})  # B missing on d3
    ratio = _roll_ratio(_closes(raw), list(DATES), "GOLDMA", "GOLDMB", DATES[3], fallback=-1.0)
    assert ratio == pytest.approx(111.0 / A[2])
    assert any("nearest common date" in m for m in loguru_warnings)


def test_roll_ratio_returns_fallback_when_never_co_quoted(loguru_warnings):
    raw = raw_rows({"GOLDMA": (3, A), "GOLDMB": (6, {4: 117.0, 5: 116.0})})
    assert _roll_ratio(_closes(raw), list(DATES), "GOLDMA", "GOLDMB", DATES[3], fallback=0.5) == 0.5
    assert any("no common quoted date" in m for m in loguru_warnings)


# --- _back_adjust --------------------------------------------------------------

def _rets(series):
    return (series / series.shift(1) - 1).iloc[1:]


def test_back_adjust_roll_day_return_equals_new_contracts_own_return(raw):
    active = _active_contract_rows(raw, None)
    cont = _back_adjust(active, raw)
    assert list(cont.columns) == CONTINUOUS_COLUMNS
    r = _rets(cont["close"])

    # Roll A->B between d3 and d4: return must be B's own d3->d4 return, not 0
    # (the pre-fix zeroing bug) and not the phantom A(d3)->B(d4) jump.
    assert r[DATES[4]] == pytest.approx(B[4] / B[3] - 1)
    assert r[DATES[4]] != pytest.approx(0.0)
    assert r[DATES[4]] != pytest.approx(B[4] / A[3] - 1)
    # Roll B->C between d6 and d7 likewise.
    assert r[DATES[7]] == pytest.approx(C[7] / C[6] - 1)


def test_back_adjust_preserves_every_intra_contract_return_and_latest_level(raw):
    active = _active_contract_rows(raw, None)
    cont = _back_adjust(active, raw)
    r = _rets(cont["close"])
    for i in (1, 2, 3):
        assert r[DATES[i]] == pytest.approx(A[i] / A[i - 1] - 1)
    for i in (5, 6):
        assert r[DATES[i]] == pytest.approx(B[i] / B[i - 1] - 1)
    assert r[DATES[8]] == pytest.approx(C[8] / C[7] - 1)
    # Most recent contract's absolute level is untouched.
    assert cont.loc[DATES[7]:, "close"].tolist() == [C[7], C[8]]
    # Earlier segments scaled by the COMPOUNDED ratios of every later roll.
    factor_b = C[6] / B[6]
    factor_a = factor_b * (B[3] / A[3])
    assert cont.loc[DATES[5], "close"] == pytest.approx(B[5] * factor_b)
    assert cont.loc[DATES[0], "close"] == pytest.approx(A[0] * factor_a)
    # OHLC scaled by the same factor as the close.
    assert cont.loc[DATES[0], "open"] == pytest.approx((A[0] - 1.0) * factor_a)
    assert cont.loc[DATES[0], "high"] == pytest.approx((A[0] + 2.0) * factor_a)


def test_back_adjust_compounded_total_return_matches_chained_contract_returns(raw):
    active = _active_contract_rows(raw, None)
    cont = _back_adjust(active, raw)["close"]
    chained = (A[3] / A[0]) * (B[6] / B[3]) * (C[8] / C[6])
    assert cont.iloc[-1] / cont.iloc[0] == pytest.approx(chained)


def test_back_adjust_without_raw_falls_back_to_cross_date_ratio(raw, loguru_warnings):
    active = _active_contract_rows(raw, None)
    r = _rets(_back_adjust(active, None)["close"])
    assert r[DATES[4]] == pytest.approx(0.0)  # documented fallback: zeroed roll-day return
    assert any("no common quoted date" in m for m in loguru_warnings)


def test_back_adjust_empty_input():
    assert _back_adjust(pd.DataFrame(columns=["tradingsymbol", "open", "high", "low", "close"])).empty


# --- assemble_commodity_data / build_universe_data -------------------------------

def _patch_data_layer(monkeypatch, raw, failed=()):
    def fake_contracts(symbol, start, end, kite=None):
        return [CommodityContract("GOLDMB", None, 100, pd.Timestamp(DATES[6]), symbol)]

    def fake_bars(symbol, start, end, fetch_report=None):
        if fetch_report is not None:
            fetch_report.weekdays_requested = len(DATES) + len(failed)
            fetch_report.dates_with_data = len(DATES)
            fetch_report.failed_dates.extend(failed)
        return raw

    monkeypatch.setattr(rcwf, "get_commodity_contracts", fake_contracts)
    monkeypatch.setattr(rcwf, "get_historical_commodity_bars", fake_bars)


def test_assemble_overlay_has_engine_columns_and_raw_quotes(monkeypatch, raw):
    _patch_data_layer(monkeypatch, raw)
    continuous_df, overlay_df = assemble_commodity_data("GOLDM", DATES[0], DATES[-1])
    assert list(overlay_df.columns) == OVERLAY_COLUMNS
    assert (overlay_df["price_quote_unit"] == 10).all()
    # Overlay keeps RAW exchange quotes (no unit conversion at this layer).
    assert overlay_df.loc[DATES[0], "fut_close"] == A[0]
    assert list(continuous_df.index) == list(overlay_df.index)


def test_build_universe_data_surfaces_fetch_failures(monkeypatch, raw):
    failed = [pd.Timestamp("2026-01-16").date(), pd.Timestamp("2026-01-19").date()]
    _patch_data_layer(monkeypatch, raw, failed=failed)
    reports = {}
    cont, ov = build_universe_data(["goldm"], DATES[0], DATES[-1], fetch_reports=reports)
    assert set(cont) == {"GOLDM"}
    assert reports["GOLDM"].failed_dates == failed
    lines = data_quality_lines(reports)
    assert len(lines) == 1 and "GOLDM" in lines[0] and "2 of 11" in lines[0] and "FETCH FAILURES" in lines[0]


def test_data_quality_lines_empty_when_no_failures():
    assert data_quality_lines({"GOLDM": BhavcopyFetchReport(weekdays_requested=5, dates_with_data=5)}) == []
    assert data_quality_lines({}) == []
