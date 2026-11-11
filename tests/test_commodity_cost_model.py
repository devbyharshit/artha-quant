"""Unit tests for the MCX commodity cost model (long and short trades)."""
import pytest

from backtesting.commodity_cost_model import IndianCommodityCostModel


def test_calculate_long_trade_charges_ctt_on_sell_value():
    # buy_value is the entry (long), sell_value is the exit
    result = IndianCommodityCostModel.calculate(buy_value=100000.0, sell_value=105000.0)
    assert result.ctt == pytest.approx(105000.0 * IndianCommodityCostModel.CTT_RATE)


def test_calculate_short_trade_charges_ctt_on_opening_sell_leg():
    # sell_value is the ENTRY here (short opens by selling), buy_value is the exit
    result = IndianCommodityCostModel.calculate(buy_value=95000.0, sell_value=100000.0)
    assert result.ctt == pytest.approx(100000.0 * IndianCommodityCostModel.CTT_RATE)
    # the model must not care which leg came first chronologically


def test_calculate_zero_values_returns_zero_breakdown():
    result = IndianCommodityCostModel.calculate(0.0, 0.0)
    assert result.total_friction == 0.0
