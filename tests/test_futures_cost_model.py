"""Unit tests for the NSE F&O futures cost model and lot-size position sizing."""
import pytest

from backtesting.futures_cost_model import (
    IndianFuturesCostModel,
    calculate_futures_position_size,
)


def test_futures_cost_model_zero_trade():
    result = IndianFuturesCostModel.calculate(0, 0)
    assert result.total_friction == 0


def test_futures_cost_model_symmetric_round_trip():
    """Buy and sell 1 lot of RELIANCEFUT at the same notional (₹7,00,000 each leg)."""
    buy_value = 700000.0
    sell_value = 700000.0
    result = IndianFuturesCostModel.calculate(buy_value, sell_value)

    expected_brokerage = min(20.0, 0.0003 * buy_value) + min(20.0, 0.0003 * sell_value)
    assert expected_brokerage == pytest.approx(20.0 + 20.0)  # both legs capped at ₹20
    assert result.brokerage == pytest.approx(expected_brokerage)

    expected_stt = sell_value * 0.0005
    assert result.stt == pytest.approx(expected_stt)
    assert result.stt == pytest.approx(350.0)

    total_turnover = buy_value + sell_value
    expected_exchange = total_turnover * 0.000019
    assert result.exchange_charges == pytest.approx(expected_exchange)

    expected_sebi = total_turnover * 0.000001
    assert result.sebi_charges == pytest.approx(expected_sebi)

    expected_stamp_duty = buy_value * 0.00002
    assert result.stamp_duty == pytest.approx(expected_stamp_duty)
    assert result.stamp_duty == pytest.approx(14.0)

    expected_gst = (expected_brokerage + expected_exchange + expected_sebi) * 0.18
    assert result.gst == pytest.approx(expected_gst)

    expected_total = (
        expected_brokerage + expected_stt + expected_exchange
        + expected_sebi + expected_stamp_duty + expected_gst
    )
    assert result.total_friction == pytest.approx(expected_total)


def test_futures_cost_model_small_trade_brokerage_below_cap():
    """When 0.03% of turnover is below ₹20, brokerage should use the percentage, not the cap."""
    buy_value = 10000.0   # 0.03% = ₹3, below the ₹20 cap
    sell_value = 12000.0  # 0.03% = ₹3.6, below the ₹20 cap
    result = IndianFuturesCostModel.calculate(buy_value, sell_value)

    expected_brokerage = (0.0003 * buy_value) + (0.0003 * sell_value)
    assert expected_brokerage < 40.0
    assert result.brokerage == pytest.approx(expected_brokerage)


def test_futures_cost_model_asymmetric_legs_capped_independently():
    """Large buy leg hits the ₹20 cap while a small sell leg does not."""
    buy_value = 1000000.0  # 0.03% = ₹300 -> capped at ₹20
    sell_value = 5000.0    # 0.03% = ₹1.5 -> uncapped
    result = IndianFuturesCostModel.calculate(buy_value, sell_value)

    expected_brokerage = 20.0 + (0.0003 * sell_value)
    assert result.brokerage == pytest.approx(expected_brokerage)


def test_futures_cost_model_no_dp_charges_field():
    """F&O futures have no demat holding, so no dp_charges line item should exist."""
    result = IndianFuturesCostModel.calculate(100000.0, 100000.0)
    assert not hasattr(result, "dp_charges")


def test_futures_cost_model_buy_only_leg():
    """Only a buy leg (position still open): STT and stamp duty computed accordingly."""
    result = IndianFuturesCostModel.calculate(500000.0, 0.0)
    assert result.stt == 0.0  # STT is sell-side only
    assert result.stamp_duty == pytest.approx(500000.0 * 0.00002)
    assert result.brokerage == pytest.approx(min(20.0, 0.0003 * 500000.0))


# --- Position sizing ---

def test_position_size_zero_lots_when_capital_too_small():
    result = calculate_futures_position_size(
        portfolio_capital=50000.0,
        allocation_pct=20.0,
        entry_price=2000.0,
        lot_size=500,
        margin_pct=20.0,
    )
    # capital_cap = 10,000; margin_per_lot = 500 * 2000 * 0.20 = 200,000 -> 0 lots
    assert result.num_lots == 0
    assert result.quantity == 0
    assert result.capital_required == 0.0


def test_position_size_affords_several_lots():
    result = calculate_futures_position_size(
        portfolio_capital=1000000.0,
        allocation_pct=20.0,
        entry_price=1000.0,
        lot_size=500,
        margin_pct=20.0,
    )
    # capital_cap = 200,000; margin_per_lot = 500 * 1000 * 0.20 = 100,000 -> 2 lots
    assert result.num_lots == 2
    assert result.quantity == 1000
    assert result.capital_required == pytest.approx(200000.0)


def test_position_size_quantity_is_multiple_of_lot_size():
    result = calculate_futures_position_size(
        portfolio_capital=375000.0,
        allocation_pct=25.0,
        entry_price=850.0,
        lot_size=300,
        margin_pct=18.0,
    )
    assert result.quantity % result.lot_size == 0


def test_position_size_invalid_inputs_do_not_error():
    result = calculate_futures_position_size(
        portfolio_capital=0.0,
        allocation_pct=20.0,
        entry_price=100.0,
        lot_size=500,
    )
    assert result.quantity == 0

    result_neg_price = calculate_futures_position_size(
        portfolio_capital=100000.0,
        allocation_pct=20.0,
        entry_price=-10.0,
        lot_size=500,
    )
    assert result_neg_price.quantity == 0
