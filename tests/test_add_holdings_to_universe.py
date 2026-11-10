"""Tests for the Kite-holdings-to-universe merge logic (no live Kite calls —
fetch_holding_symbols() is Kite-dependent and intentionally untested here)."""
import yaml
import pytest

from data.loaders.add_holdings_to_universe import merge_holdings_into_universe


@pytest.fixture
def sample_universe(tmp_path):
    path = tmp_path / "universe.yaml"
    data = {
        "version": "1.0",
        "universe_name": "test",
        "description": "test universe",
        "assets": [
            {"symbol": "RELIANCE", "exchange": "NSE", "sector": "Energy"},
            {"symbol": "TCS", "exchange": "NSE", "sector": "Technology"},
        ]
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)
    return path


def test_adds_new_held_symbol(sample_universe):
    holdings = [{"tradingsymbol": "WIPRO", "exchange": "NSE", "quantity": 10}]
    added = merge_holdings_into_universe(holdings, universe_path=sample_universe)

    assert len(added) == 1
    assert added[0]["symbol"] == "WIPRO"
    assert added[0]["sector"] == "Personal Holding"

    with open(sample_universe, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    symbols = {a["symbol"] for a in data["assets"]}
    assert "WIPRO" in symbols
    assert len(data["assets"]) == 3


def test_skips_symbol_already_in_universe(sample_universe):
    holdings = [{"tradingsymbol": "RELIANCE", "exchange": "NSE", "quantity": 5}]
    added = merge_holdings_into_universe(holdings, universe_path=sample_universe)

    assert added == []
    with open(sample_universe, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert len(data["assets"]) == 2


def test_reuses_known_sector_if_symbol_already_tagged_elsewhere(sample_universe):
    """If a held symbol happens to already be tagged in the file under a
    different record somehow, prefer that sector over the generic fallback."""
    holdings = [{"tradingsymbol": "TCS", "exchange": "NSE", "quantity": 0}]  # qty 0 -> skipped (not a real holding)
    added = merge_holdings_into_universe(holdings, universe_path=sample_universe)
    assert added == []  # zero-quantity holdings (e.g. fully sold) are not added


def test_dry_run_does_not_write_file(sample_universe):
    holdings = [{"tradingsymbol": "INFY", "exchange": "NSE", "quantity": 3}]
    added = merge_holdings_into_universe(holdings, universe_path=sample_universe, dry_run=True)

    assert len(added) == 1
    with open(sample_universe, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    symbols = {a["symbol"] for a in data["assets"]}
    assert "INFY" not in symbols
