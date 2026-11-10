"""Unit tests for Sprint 4: Indian cost model, slippage, tax engine, and backtest harness."""
import pytest
import numpy as np
import pandas as pd

from backtesting.cost_model import IndianCostModel
from backtesting.slippage import SlippageModel
from backtesting.tax_model import IndianTaxEngine
from backtesting.engine import BacktestEngine
from backtesting.walk_forward import WalkForwardValidator
from strategies.trend_breakout import TrendBreakoutStrategy


def test_indian_cost_model_delivery():
    """Verify exact calculation of Zerodha equity delivery charges."""
    buy_val = 100000.0  # ₹1 Lakh buy
    sell_val = 110000.0 # ₹1.1 Lakh sell (₹10k gross profit)

    cost = IndianCostModel.calculate(buy_val, sell_val, is_delivery=True)

    # 1. Brokerage is ₹0 on delivery
    assert cost.brokerage == 0.0

    # 2. STT: 0.1% on buy + sell = 0.001 * 210,000 = ₹210.00
    assert np.isclose(cost.stt, 210.0, atol=0.01)

    # 3. Stamp Duty: 0.015% on buy only = 0.00015 * 100,000 = ₹15.00
    assert np.isclose(cost.stamp_duty, 15.0, atol=0.01)

    # 4. DP Charges: ₹15.93 flat on sell
    assert cost.dp_charges == 15.93

    # 5. Total friction must be greater than STT + Stamp + DP
    assert cost.total_friction > (210.0 + 15.0 + 15.93)


def test_indian_cost_model_intraday():
    """Verify intraday brokerage cap and zero DP charges."""
    buy_val = 500000.0
    sell_val = 505000.0
    cost = IndianCostModel.calculate(buy_val, sell_val, is_delivery=False)

    # Brokerage capped at ₹40 (₹20 buy + ₹20 sell)
    assert cost.brokerage <= 40.0
    # No DP charges on intraday
    assert cost.dp_charges == 0.0


def test_slippage_model():
    """Verify slippage price adjustment."""
    raw_price = 1000.0
    # Large-cap 0.05% slippage
    buy_fill = SlippageModel.apply_slippage(raw_price, is_buy=True, slippage_pct=0.0005)
    sell_fill = SlippageModel.apply_slippage(raw_price, is_buy=False, slippage_pct=0.0005)

    assert buy_fill == 1000.50
    assert sell_fill == 999.50


def test_tax_engine_stcg():
    """Verify 20% STCG on short-term profitable trades."""
    profit = 10000.0
    res = IndianTaxEngine.evaluate_trade(profit, holding_days=45)

    assert res.is_stcg is True
    assert res.tax_rate == 0.20
    assert res.tax_payable == 2000.0
    assert res.net_pnl_after_tax == 8000.0


def test_tax_engine_losses_pay_zero_tax():
    """Verify losing trades incur zero tax."""
    loss = -5000.0
    res = IndianTaxEngine.evaluate_trade(loss, holding_days=10)
    assert res.tax_payable == 0.0
    assert res.net_pnl_after_tax == -5000.0


def test_backtest_engine_execution():
    """Run full backtest simulation and assert accurate accounting."""
    # Generate 400 bars of trending market
    dates = pd.date_range("2024-01-01", periods=400, freq="D")
    np.random.seed(42)
    closes = np.linspace(500, 1500, 400) + np.random.normal(0, 5, 400)
    highs = closes + 10.0
    lows = closes - 10.0
    opens = (highs + lows) / 2.0
    volumes = np.random.randint(100000, 300000, 400)

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes
    }, index=pd.Index(dates, name="timestamp"))

    strategy = TrendBreakoutStrategy(params={"ema_trend_period": 50, "donchian_entry_period": 20})
    engine = BacktestEngine(
        strategy=strategy,
        initial_capital=100000.0,
        risk_per_trade_pct=1.0,
        max_position_pct=10.0,
        apply_tax=True
    )

    summary = engine.run(df, symbol="TEST_ASSET")

    assert summary.initial_capital == 100000.0
    assert summary.total_trades > 0
    assert summary.total_frictions_paid > 0.0
    assert summary.total_tax_paid >= 0.0
    assert summary.max_drawdown_pct >= 0.0


def test_walk_forward_validator_passes_through_position_sizing_mode():
    """WalkForwardValidator must honor position_sizing_mode, not silently
    fall back to the engine's risk_based default, for stop-less strategies."""
    np.random.seed(5)
    dates = pd.date_range("2022-01-01", periods=400, freq="D")
    closes = np.linspace(500, 1500, 400) + np.random.normal(0, 5, 400)
    highs = closes + 10.0
    lows = closes - 10.0
    opens = (highs + lows) / 2.0
    volumes = np.random.randint(100000, 300000, 400)

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes
    }, index=pd.Index(dates, name="timestamp"))

    strategy = TrendBreakoutStrategy(params={"ema_trend_period": 50, "donchian_entry_period": 20})
    validator = WalkForwardValidator(strategy=strategy, position_sizing_mode="full_equity")

    report = validator.run({"TEST_ASSET": df})

    is_sum, oos_sum, _ = report.per_symbol["TEST_ASSET"]
    assert is_sum.total_trades + oos_sum.total_trades > 0
    trades = is_sum.trades or oos_sum.trades
    if trades:
        trade = trades[0]
        approx_full_alloc = int(100000.0 / trade.entry_price)
        assert trade.quantity == approx_full_alloc
