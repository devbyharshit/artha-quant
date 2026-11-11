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


M = IndianCommodityCostModel


@pytest.mark.parametrize("buy_value,sell_value", [
    (10_000.0, 10_500.0),        # small: brokerage below the cap on both legs
    (1_405_280.0, 1_420_000.0),  # one GOLDM lot at a 2026 quote: cap engaged
    (50_000.0, 2_000_000.0),     # mixed: one leg capped, the other not
])
def test_total_friction_is_exactly_the_sum_of_its_components(buy_value, sell_value):
    r = M.calculate(buy_value=buy_value, sell_value=sell_value)
    assert r.total_friction == pytest.approx(
        r.brokerage + r.ctt + r.exchange_charges + r.sebi_charges + r.stamp_duty + r.gst)


def test_brokerage_cap_engages_per_leg_on_large_turnover():
    # 0.03% of Rs 14 lakh = Rs 420 per leg, far above the Rs 20 cap.
    r = M.calculate(buy_value=1_400_000.0, sell_value=1_400_000.0)
    assert r.brokerage == pytest.approx(2 * M.BROKERAGE_CAP) == pytest.approx(40.0)


def test_brokerage_is_percentage_below_the_cap():
    # 0.03% of Rs 10,000 = Rs 3 per leg (< Rs 20 cap).
    r = M.calculate(buy_value=10_000.0, sell_value=10_000.0)
    assert r.brokerage == pytest.approx(2 * 10_000.0 * M.BROKERAGE_RATE) == pytest.approx(6.0)


def test_brokerage_cap_applies_independently_per_leg():
    # Buy leg Rs 50,000 -> Rs 15 (uncapped); sell leg Rs 20 lakh -> capped at Rs 20.
    r = M.calculate(buy_value=50_000.0, sell_value=2_000_000.0)
    assert r.brokerage == pytest.approx(50_000.0 * M.BROKERAGE_RATE + M.BROKERAGE_CAP) == pytest.approx(35.0)


@pytest.mark.parametrize("buy_value,sell_value", [(10_000.0, 10_500.0), (1_400_000.0, 1_450_000.0)])
def test_gst_is_18_percent_of_brokerage_exchange_and_sebi_only(buy_value, sell_value):
    r = M.calculate(buy_value=buy_value, sell_value=sell_value)
    assert M.GST_RATE == 0.18
    assert r.gst == pytest.approx(0.18 * (r.brokerage + r.exchange_charges + r.sebi_charges))
    # CTT and stamp duty are NOT in the GST base.
    assert r.gst != pytest.approx(0.18 * (r.brokerage + r.exchange_charges + r.sebi_charges + r.ctt))


def test_turnover_based_components_use_combined_and_one_sided_legs():
    r = M.calculate(buy_value=1_000_000.0, sell_value=1_100_000.0)
    assert r.exchange_charges == pytest.approx(2_100_000.0 * M.EXCHANGE_RATE)
    assert r.sebi_charges == pytest.approx(2_100_000.0 * M.SEBI_RATE)
    assert r.stamp_duty == pytest.approx(1_000_000.0 * M.STAMP_DUTY_BUY)  # buy leg only
    assert r.ctt == pytest.approx(1_100_000.0 * M.CTT_RATE)  # sell leg only
