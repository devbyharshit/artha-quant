"""Tests for the MCX commodity contract resolver (data/loaders/mcx_instruments.py).

No real network calls -- Kite is always absent (None). The expiry-rule tests
are anchored to real MCX expiry dates present in tests/fixtures/mcx_bhavcopy/
(Task 1's captured FUTCOM responses), not to hand-typed dates."""
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from data.loaders import mcx_instruments
from data.loaders.mcx_instruments import (
    CommodityContract,
    _commodity_expiry,
    _validated_live_lot_size,
    get_commodity_contracts,
    get_price_quote_unit,
    get_static_lot_size,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "mcx_bhavcopy"


def test_get_commodity_contracts_gold_mini_covers_range():
    contracts = get_commodity_contracts("GOLDM", "2024-01-01", "2024-03-31")
    assert all(c.name == "GOLDM" for c in contracts)
    assert all(isinstance(c, CommodityContract) for c in contracts)
    assert contracts[0].expiry <= pd.Timestamp("2024-02-29")  # lead month included


def test_get_commodity_contracts_lot_size_fallback_without_kite():
    contracts = get_commodity_contracts("SILVERMIC", "2024-01-01", "2024-01-31", kite=None)
    assert all(c.instrument_token is None for c in contracts)
    assert all(c.lot_size > 0 for c in contracts)  # named per-commodity default, not 0/negative


def test_get_commodity_contracts_rejects_inverted_range():
    with pytest.raises(ValueError):
        get_commodity_contracts("CRUDEOILM", "2024-03-01", "2024-01-01")


def test_get_commodity_contracts_rejects_unknown_commodity():
    with pytest.raises(ValueError):
        get_commodity_contracts("NOTACOMMODITY", "2024-01-01", "2024-03-31")


def test_tradingsymbol_follows_kite_futures_convention():
    contracts = get_commodity_contracts("NATGASMINI", "2024-01-01", "2024-01-31")
    assert contracts[0].tradingsymbol.startswith("NATGASMINI")
    assert contracts[0].tradingsymbol.endswith("FUT")


# ---------------------------------------------------------------------------
# Expiry approximation rules, anchored to real MCX expiries in Task 1's fixtures
# ---------------------------------------------------------------------------

def test_gold_mini_expires_on_5th_rolled_back_from_weekend():
    # Fixture: GOLDM 03JUL2015 (5 Jul 2015 was a Sunday, rolled back to Fri 3rd)
    assert _commodity_expiry("GOLDM", 2015, 7) == date(2015, 7, 3)
    # Fixture: GOLDM 04SEP2015 (5 Sep 2015 was a Saturday)
    assert _commodity_expiry("GOLDM", 2015, 9) == date(2015, 9, 4)
    # Fixture: GOLDM 05AUG2015 (weekday, no shift)
    assert _commodity_expiry("GOLDM", 2015, 8) == date(2015, 8, 5)


def test_silver_micro_expires_on_last_weekday_of_month():
    # Fixture: SILVERMIC 28FEB2020 (29 Feb 2020 was a Saturday)
    assert _commodity_expiry("SILVERMIC", 2020, 2) == date(2020, 2, 28)
    # Fixture: SILVERMIC 30NOV2026 (last weekday of Nov 2026)
    assert _commodity_expiry("SILVERMIC", 2026, 11) == date(2026, 11, 30)


def test_crude_oil_mini_expires_on_19th_rolled_back_from_weekend():
    # Fixture: CRUDEOILM 19NOV2015 (weekday, no shift)
    assert _commodity_expiry("CRUDEOILM", 2015, 11) == date(2015, 11, 19)
    # Fixture: CRUDEOILM 18DEC2026 (19 Dec 2026 was a Saturday, rolled back to Fri 18th)
    assert _commodity_expiry("CRUDEOILM", 2026, 12) == date(2026, 12, 18)


def test_natural_gas_mini_expires_three_weekdays_before_last_weekday():
    # Fixture: NATURALGAS 28JAN2020 (last weekday of Jan 2020 is Fri 31st; 3 weekdays earlier is Tue 28th)
    assert _commodity_expiry("NATGASMINI", 2020, 1) == date(2020, 1, 28)


def test_get_commodity_contracts_expiry_matches_rule():
    contracts = get_commodity_contracts("CRUDEOILM", "2015-11-01", "2015-11-30")
    expiries = {c.expiry.date() for c in contracts}
    assert date(2015, 11, 19) in expiries


# ---------------------------------------------------------------------------
# Price quote unit -- DERIVED from real MCX bhavcopy fixtures, not hand-picked
# ---------------------------------------------------------------------------

CORE4 = ("GOLDM", "SILVERMIC", "CRUDEOILM", "NATGASMINI")
# Real full-day captures that contain core-4 rows (2020 has SILVERMIC only).
RATIO_FIXTURES = ("futcom_2015-06-15.json", "futcom_2020-01-15.json", "futcom_2026-11-10_core4.json")
MIN_LOTS = 100  # skip thinly-traded far months, where Close vs. the day's VWAP can diverge


def _rupees_per_lot_per_quoted_point():
    """{(fixture, symbol): [ratio, ...]} where ratio = Value(lakh)*1e5 /
    Volume(lots) / Close. MCX's own traded turnover divided by lots traded is
    the rupee value of ONE LOT; dividing by the quoted price gives rupees per
    lot per quoted point -- the exchange's ground truth for
    lot_size / price_quote_unit. (Close is not the day's VWAP, hence the
    tolerance below.)"""
    out = {}
    for name in RATIO_FIXTURES:
        for row in json.loads((FIXTURES_DIR / name).read_text())["Data"]:
            sym = row["Symbol"].strip()
            if sym not in CORE4 or row["InstrumentName"].strip() != "FUTCOM":
                continue
            volume, close = float(row["Volume"]), float(row["Close"])
            if volume < MIN_LOTS or close <= 0:
                continue
            out.setdefault((name, sym), []).append(float(row["Value"]) * 1e5 / volume / close)
    return out


def test_price_quote_unit_matches_mcx_traded_value_in_real_fixtures():
    ratios = _rupees_per_lot_per_quoted_point()
    # Every core-4 commodity is checked against real data at least once, and
    # GOLDM in two different years.
    assert {sym for _, sym in ratios} == set(CORE4)
    assert sum(1 for _, sym in ratios if sym == "GOLDM") >= 2

    for (name, sym), values in ratios.items():
        expected = get_static_lot_size(sym) / get_price_quote_unit(sym)
        for r in values:
            assert r == pytest.approx(expected, rel=0.03), (name, sym, r, expected)


def test_derived_quote_unit_for_goldm_is_ten_not_one():
    """Derive each quote unit purely from fixture data (lot_size / ratio).
    The pre-fix pipeline implicitly used 1 for GOLDM (rupees = quantity *
    price), which this data rules out -- that assumption overstated every
    GOLDM rupee figure 10x."""
    ratios = _rupees_per_lot_per_quoted_point()
    goldm = [r for (_, sym), vs in ratios.items() if sym == "GOLDM" for r in vs]
    derived = [get_static_lot_size("GOLDM") / r for r in goldm]
    assert all(round(d) == 10 for d in derived), derived
    assert get_price_quote_unit("GOLDM") == round(sum(derived) / len(derived))
    # The other three are quoted per lot-size unit already.
    for sym in ("SILVERMIC", "CRUDEOILM", "NATGASMINI"):
        vs = [r for (_, s), v in ratios.items() if s == sym for r in v]
        assert vs and all(round(get_static_lot_size(sym) / r) == 1 for r in vs), (sym, vs)
        assert get_price_quote_unit(sym) == 1


def test_sizer_lot_notional_matches_mcx_turnover_per_lot_for_goldm():
    """End-to-end on the real 2026 GOLDM row: the sizer's per-lot margin
    (25% of notional) must imply the rupees-per-lot MCX itself reported."""
    from backtesting.commodity_signals import calculate_vol_target_position_size

    row = next(r for r in json.loads((FIXTURES_DIR / "futcom_2026-11-10_core4.json").read_text())["Data"]
               if r["Symbol"].strip() == "GOLDM" and float(r["Volume"]) > 0)
    mcx_rupees_per_lot = float(row["Value"]) * 1e5 / float(row["Volume"])
    sizing = calculate_vol_target_position_size(
        10_000_000.0, 35.0, float(row["Close"]), get_static_lot_size("GOLDM"), 15.0,
        margin_pct=25.0, price_quote_unit=get_price_quote_unit("GOLDM"),
    )
    assert sizing.margin_per_lot / 0.25 == pytest.approx(mcx_rupees_per_lot, rel=0.03)


def test_get_price_quote_unit_rejects_unknown_commodity():
    with pytest.raises(ValueError):
        get_price_quote_unit("ALUMINI")


# ---------------------------------------------------------------------------
# Kite lot-size override is sanity-checked, not trusted blindly (docs/13 §3)
# ---------------------------------------------------------------------------

def test_validated_live_lot_size_accepts_matching_kite_value(loguru_warnings):
    assert _validated_live_lot_size("GOLDM", 100) == 100
    assert loguru_warnings == []


@pytest.mark.parametrize("bad", [1, 10, 1000])
def test_validated_live_lot_size_falls_back_to_static_on_mismatch(bad, loguru_warnings):
    assert _validated_live_lot_size("GOLDM", bad) == 100
    assert any("Kite reports lot size" in m and "GOLDM" in m for m in loguru_warnings)


@pytest.mark.parametrize("bad", [None, "abc"])
def test_validated_live_lot_size_falls_back_on_unparseable(bad, loguru_warnings):
    assert _validated_live_lot_size("CRUDEOILM", bad) == 10
    assert any("unparseable" in m for m in loguru_warnings)


def test_get_commodity_contracts_does_not_adopt_wrong_kite_lot_size(monkeypatch, loguru_warnings):
    """A Kite catalog row reporting lot_size=1 for GOLDM (Kite expresses MCX
    quantity in lots) must not rescale every contract's lot size."""
    live_tsym = get_commodity_contracts("GOLDM", "2026-11-01", "2026-12-31")[-1].tradingsymbol
    monkeypatch.setattr(mcx_instruments, "_live_fut_contracts_for_commodity", lambda kite, c: {
        live_tsym: {"tradingsymbol": live_tsym, "instrument_token": 12345, "lot_size": 1},
    })
    contracts = get_commodity_contracts("GOLDM", "2026-11-01", "2026-12-31", kite=object())
    assert all(c.lot_size == 100 for c in contracts)
    assert contracts[-1].instrument_token == 12345  # the live token is still resolved
    assert any("Kite reports lot size 1" in m for m in loguru_warnings)
