"""Tests for the MCX commodity contract resolver (data/loaders/mcx_instruments.py).

No real network calls -- Kite is always absent (None). The expiry-rule tests
are anchored to real MCX expiry dates present in tests/fixtures/mcx_bhavcopy/
(Task 1's captured FUTCOM responses), not to hand-typed dates."""
from datetime import date

import pandas as pd
import pytest

from data.loaders.mcx_instruments import (
    CommodityContract,
    _commodity_expiry,
    get_commodity_contracts,
)


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
