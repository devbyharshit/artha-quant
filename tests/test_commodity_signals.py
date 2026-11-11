"""Unit tests for MCX commodity time-series momentum signal and vol-target sizing."""
import numpy as np
import pandas as pd
import pytest

from backtesting.commodity_signals import (
    CommoditySizingResult,
    calculate_vol_target_position_size,
    compute_momentum_signal,
    compute_realized_vol,
)


def test_compute_momentum_signal_long_on_positive_trailing_return():
    closes = pd.Series(np.linspace(100, 150, 300))
    signal = compute_momentum_signal(closes, lookback_days=252)
    assert signal.iloc[-1] == 1


def test_compute_momentum_signal_short_on_negative_trailing_return():
    closes = pd.Series(np.linspace(150, 100, 300))
    signal = compute_momentum_signal(closes, lookback_days=252)
    assert signal.iloc[-1] == -1


def test_compute_momentum_signal_zero_during_warmup():
    closes = pd.Series(np.linspace(100, 110, 100))  # < 252 bars
    signal = compute_momentum_signal(closes, lookback_days=252)
    assert (signal == 0).all()
    assert not signal.isna().any()


def test_compute_momentum_signal_never_nan():
    closes = pd.Series(np.linspace(100, 150, 300))
    signal = compute_momentum_signal(closes, lookback_days=252)
    assert not signal.isna().any()


def test_compute_realized_vol_nan_during_warmup():
    closes = pd.Series(np.linspace(100, 110, 30))  # < 60 bars
    vol = compute_realized_vol(closes, vol_lookback_days=60)
    assert vol.isna().all()


def test_compute_realized_vol_not_nan_after_warmup():
    rng = np.random.default_rng(42)
    closes = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 200)))
    vol = compute_realized_vol(closes, vol_lookback_days=60)
    assert not vol.iloc[-1] != vol.iloc[-1]  # not NaN
    assert vol.iloc[-1] > 0


def test_vol_target_sizing_handles_zero_realized_vol():
    result = calculate_vol_target_position_size(
        portfolio_capital=100000.0, vol_target_pct=35.0, entry_price=5000.0,
        lot_size=10, realized_vol_pct=0.0, price_quote_unit=1,
    )
    assert result.quantity == 0
    assert result.num_lots == 0
    assert np.isfinite(result.quantity)
    assert np.isfinite(result.capital_required)


def test_vol_target_sizing_handles_negative_realized_vol():
    result = calculate_vol_target_position_size(
        portfolio_capital=100000.0, vol_target_pct=35.0, entry_price=5000.0,
        lot_size=10, realized_vol_pct=-5.0, price_quote_unit=1,
    )
    assert result.quantity == 0
    assert result.num_lots == 0


def test_vol_target_sizing_handles_nan_realized_vol():
    result = calculate_vol_target_position_size(
        portfolio_capital=100000.0, vol_target_pct=35.0, entry_price=5000.0,
        lot_size=10, realized_vol_pct=float("nan"), price_quote_unit=1,
    )
    assert result.quantity == 0
    assert result.num_lots == 0
    assert np.isfinite(result.quantity)
    assert np.isfinite(result.capital_required)


def test_vol_target_sizing_handles_nonpositive_other_inputs():
    assert calculate_vol_target_position_size(0.0, 35.0, 5000.0, 10, 20.0, price_quote_unit=1).quantity == 0
    assert calculate_vol_target_position_size(100000.0, 35.0, 0.0, 10, 20.0, price_quote_unit=1).quantity == 0
    assert calculate_vol_target_position_size(100000.0, 35.0, 5000.0, 0, 20.0, price_quote_unit=1).quantity == 0
    assert calculate_vol_target_position_size(100000.0, 35.0, 5000.0, -10, 20.0, price_quote_unit=1).quantity == 0


def test_vol_target_sizing_scales_down_for_higher_volatility():
    low_vol = calculate_vol_target_position_size(100000.0, 35.0, 5000.0, 10, realized_vol_pct=10.0, price_quote_unit=1)
    high_vol = calculate_vol_target_position_size(100000.0, 35.0, 5000.0, 10, realized_vol_pct=40.0, price_quote_unit=1)
    assert high_vol.num_lots <= low_vol.num_lots


def test_vol_target_sizing_returns_commodity_sizing_result():
    result = calculate_vol_target_position_size(100000.0, 35.0, 5000.0, 10, realized_vol_pct=20.0, price_quote_unit=1)
    assert isinstance(result, CommoditySizingResult)
    assert result.quantity == result.num_lots * result.lot_size


def test_vol_target_sizing_caps_leverage_at_low_vol():
    # At 1.6% realized vol, the uncapped vol-target formula produces ~295x
    # leverage (reviewer-reproduced bug). The margin cap must keep
    # capital_required within portfolio_capital.
    result = calculate_vol_target_position_size(
        portfolio_capital=100000.0, vol_target_pct=35.0, entry_price=5000.0,
        lot_size=10, realized_vol_pct=1.6, price_quote_unit=1,
    )
    assert result.capital_required <= 100000.0
    assert result.quantity == result.num_lots * result.lot_size
    assert result.capital_allocation_pct <= 100.0


def test_vol_target_sizing_cap_inactive_when_vol_target_already_affordable():
    # At realized_vol_pct == vol_target_pct, the uncapped vol-target notional
    # is ~1x portfolio_capital, well within margin affordability at 25%
    # margin. The cap should not reduce num_lots below the uncapped formula.
    portfolio_capital = 100000.0
    vol_target_pct = 35.0
    entry_price = 5000.0
    lot_size = 10
    realized_vol_pct = 35.0

    lot_notional = lot_size * entry_price
    target_capital_at_risk = portfolio_capital * (vol_target_pct / 100.0)
    risk_per_lot = lot_notional * (realized_vol_pct / 100.0)
    expected_uncapped_num_lots = max(0, int(target_capital_at_risk // risk_per_lot))

    result = calculate_vol_target_position_size(
        portfolio_capital=portfolio_capital, vol_target_pct=vol_target_pct,
        entry_price=entry_price, lot_size=lot_size, realized_vol_pct=realized_vol_pct, price_quote_unit=1,
    )
    assert result.num_lots == expected_uncapped_num_lots
    assert result.capital_required <= portfolio_capital


def test_vol_target_sizing_price_quote_unit_is_a_divisor_on_notional():
    # GOLDM-shaped: 100 g lot quoted per 10 g. One lot = 100 * 140000 / 10 =
    # Rs 14 lakh, so 25% margin per lot = Rs 3.5 lakh (not Rs 35 lakh).
    r = calculate_vol_target_position_size(
        10_000_000.0, 35.0, 140000.0, 100, 15.0, margin_pct=25.0, price_quote_unit=10)
    assert r.margin_per_lot == pytest.approx(0.25 * 100 * 140000.0 / 10)
    assert r.price_quote_unit == 10
    # Same rupee economics expressed per-unit: identical lot count.
    same = calculate_vol_target_position_size(
        10_000_000.0, 35.0, 14000.0, 100, 15.0, margin_pct=25.0, price_quote_unit=1)
    assert r.num_lots == same.num_lots > 0
    assert r.capital_required == pytest.approx(same.capital_required)


def test_vol_target_sizing_requires_explicit_price_quote_unit():
    with pytest.raises(TypeError):
        calculate_vol_target_position_size(100000.0, 35.0, 5000.0, 10, 20.0)  # no silent default
    assert calculate_vol_target_position_size(
        100000.0, 35.0, 5000.0, 10, 20.0, price_quote_unit=0).quantity == 0


def test_vol_target_sizing_exposes_why_a_size_is_zero():
    # Vol-bound: Rs 2.5 lakh notional, 40% vol -> risk Rs 1 lakh > Rs 35k budget,
    # while margin (Rs 62.5k) is affordable.
    vol_bound = calculate_vol_target_position_size(
        100000.0, 35.0, 250000.0, 1, 40.0, margin_pct=25.0, price_quote_unit=1)
    assert vol_bound.num_lots == 0
    assert vol_bound.vol_target_lots == 0 and vol_bound.max_lots_by_margin == 1
    # Margin-bound: Rs 5 lakh notional at 25% = Rs 1.25 lakh margin > capital,
    # but at 5% vol the budget alone would allow 1 lot.
    margin_bound = calculate_vol_target_position_size(
        100000.0, 35.0, 500000.0, 1, 5.0, margin_pct=25.0, price_quote_unit=1)
    assert margin_bound.num_lots == 0
    assert margin_bound.vol_target_lots == 1 and margin_bound.max_lots_by_margin == 0
