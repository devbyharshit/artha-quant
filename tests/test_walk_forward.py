"""Tests for execution realism in BacktestEngine, financial-year tax netting,
and the universe-level WalkForwardValidator."""
import numpy as np
import pandas as pd
import pytest

from strategies.base_strategy import BaseStrategy
from backtesting.engine import BacktestEngine
from backtesting.slippage import SlippageModel
from backtesting.tax_model import AnnualTaxLedger, financial_year
from backtesting.walk_forward import WalkForwardValidator, apply_portfolio_tax

SLIP = 0.0005


def buy(p):
    return SlippageModel.apply_slippage(np.float64(p), is_buy=True, slippage_pct=SLIP)


def sell(p):
    return SlippageModel.apply_slippage(np.float64(p), is_buy=False, slippage_pct=SLIP)


class Scripted(BaseStrategy):
    """Emits given signals/stops/targets by bar position, after `warmup` NaN bars."""

    def __init__(self, signals=None, stops=None, targets=None, warmup=0):
        super().__init__("Scripted", {})
        self.sig, self.stp, self.tgt, self.warmup = signals or {}, stops or {}, targets or {}, warmup

    def calculate_indicators(self, df):
        ind = np.arange(len(df), dtype=float)
        ind[: self.warmup] = np.nan
        df["ind"] = ind
        return df

    def generate_signals(self, df):
        n = len(df)
        df["signal"] = [self.sig.get(i, 0) for i in range(n)]
        df["stop_loss"] = [float(self.stp.get(i, 0.0)) for i in range(n)]
        df["target"] = [float(self.tgt.get(i, 0.0)) for i in range(n)]
        return df


def bars(closes, opens=None, highs=None, lows=None, start="2024-01-01"):
    closes = np.asarray(closes, dtype=float)
    opens = closes.copy() if opens is None else np.asarray(opens, dtype=float)
    highs = np.maximum(opens, closes) + 1 if highs is None else np.asarray(highs, dtype=float)
    lows = np.minimum(opens, closes) - 1 if lows is None else np.asarray(lows, dtype=float)
    idx = pd.date_range(start, periods=len(closes), freq="D", name="timestamp")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes,
                         "volume": np.full(len(closes), 100000)}, index=idx)


def run(strategy, df, **kw):
    kw.setdefault("position_sizing_mode", "full_equity")
    kw.setdefault("apply_tax", False)
    return BacktestEngine(strategy=strategy, initial_capital=100000.0, **kw).run(df, symbol="T")


def test_entry_and_signal_exit_fill_at_next_bar_open():
    df = bars(closes=[100, 101, 102, 103, 104, 105], opens=[100, 100.5, 110, 103, 120, 105])
    res = run(Scripted(signals={1: 1, 3: -1}), df)

    trade = res.trades[0]
    assert trade.entry_time == df.index[2]
    assert trade.entry_price == buy(110)
    assert trade.exit_time == df.index[4]
    assert trade.exit_price == sell(120)
    assert trade.exit_reason == "SIGNAL_EXIT"


def test_gap_down_through_stop_fills_at_open_not_stop():
    df = bars(closes=[100, 100, 100, 80, 80],
              opens=[100, 100, 100, 85, 80],
              lows=[99, 99, 99, 79, 79])
    res = run(Scripted(signals={1: 1}, stops={1: 95.0}), df)

    trade = res.trades[0]
    assert trade.exit_reason == "STOP_LOSS"
    assert trade.exit_price == sell(85)


def test_entry_skipped_when_open_gaps_below_stop():
    df = bars(closes=[100, 100, 90, 90], opens=[100, 100, 90, 90])
    res = run(Scripted(signals={1: 1}, stops={1: 95.0}), df)
    assert res.total_trades == 0


def test_trailing_stop_from_strategy_is_honored():
    # Initial stop 90 at entry; the strategy raises it to 104 on bar 3.
    df = bars(closes=[100, 100, 105, 106, 103, 103],
              opens=[100, 100, 100, 105, 105, 103],
              lows=[99, 99, 99, 104.5, 102, 102])
    res = run(Scripted(signals={1: 1}, stops={1: 90.0, 3: 104.0}), df)

    trade = res.trades[0]
    assert trade.exit_reason == "STOP_LOSS"
    assert trade.exit_time == df.index[4]
    assert trade.exit_price == sell(104)


def test_drawdown_is_mark_to_market():
    # Bought at 100, falls to 60 while open, recovers to 100 before the exit.
    df = bars(closes=[100, 100, 100, 60, 100, 100], opens=[100, 100, 100, 60, 100, 100])
    res = run(Scripted(signals={1: 1, 4: -1}), df)
    assert res.max_drawdown_pct > 35.0


def test_window_uses_prior_history_for_warmup():
    df = bars(closes=np.linspace(100, 150, 100))
    strat = Scripted(signals={60: 1, 70: -1}, warmup=50)
    res = BacktestEngine(strategy=strat, position_sizing_mode="full_equity", apply_tax=False).run(
        df, start=df.index[55], end=df.index[80])

    assert res.equity_curve.index[1] == df.index[55]
    assert res.total_trades == 1
    assert res.trades[0].entry_time == df.index[61]


def test_window_end_hides_future_bars_from_strategy():
    seen = {}

    class Spy(Scripted):
        def calculate_indicators(self, df):
            seen["last"] = df.index[-1]
            return super().calculate_indicators(df)

    df = bars(closes=np.linspace(100, 150, 50))
    BacktestEngine(strategy=Spy(), apply_tax=False).run(df, end=df.index[30])
    assert seen["last"] == df.index[30]


def test_financial_year_boundary():
    assert financial_year(pd.Timestamp("2025-03-31")) == 2025
    assert financial_year(pd.Timestamp("2025-04-01")) == 2026


def test_ledger_nets_losses_within_year_and_carries_forward():
    ledger = AnnualTaxLedger()
    t = pd.Timestamp("2025-06-01")
    ledger.record(10000.0, 30, t)
    ledger.record(-4000.0, 30, t)
    assert ledger.settle() == pytest.approx(1200.0)  # 20% of the 6,000 net

    ledger.record(-5000.0, 30, pd.Timestamp("2026-06-01"))
    assert ledger.settle() == 0.0
    ledger.record(8000.0, 30, pd.Timestamp("2027-06-01"))
    assert ledger.settle() == pytest.approx(600.0)  # 20% of 8,000 - 5,000 carried forward


def test_ledger_short_term_loss_offsets_long_term_gain_and_exemption_applies():
    ledger = AnnualTaxLedger()
    t = pd.Timestamp("2025-06-01")
    ledger.record(200000.0, 400, t)   # long-term gain
    ledger.record(-50000.0, 30, t)    # short-term loss
    # LTCG 150,000 after set-off, minus 125,000 exemption, at 12.5%
    assert ledger.settle() == pytest.approx(3125.0)


def test_ledger_long_term_loss_does_not_offset_short_term_gain():
    ledger = AnnualTaxLedger()
    t = pd.Timestamp("2025-06-01")
    ledger.record(10000.0, 30, t)
    ledger.record(-10000.0, 400, t)
    assert ledger.settle() == pytest.approx(2000.0)


def test_engine_tax_is_annual_not_per_trade():
    # One winner and one equal-size loser in the same FY: per-trade tax would
    # charge the winner; annual netting should charge nothing.
    closes = [100, 100, 110, 110, 110, 100, 100]
    df = bars(closes=closes, opens=closes, start="2024-05-01")
    res = run(Scripted(signals={0: 1, 2: -1, 3: 1, 5: -1}), df, apply_tax=True)
    assert res.total_trades == 2
    assert sum(t.net_pnl for t in res.trades) < 0
    assert res.total_tax_paid == 0.0


def test_portfolio_tax_deducted_after_year_end():
    idx = pd.date_range("2025-03-01", "2025-05-01", freq="D", tz="Asia/Kolkata")
    equity = pd.Series(110000.0, index=idx)

    class T:
        net_pnl, holding_days = 10000.0, 30
        exit_time = pd.Timestamp("2025-03-15", tz="Asia/Kolkata")

    after, tax = apply_portfolio_tax(equity, [T()])
    assert tax == pytest.approx(2000.0)
    assert after.loc["2025-03-31"] == 110000.0
    assert after.loc["2025-04-01"] == 108000.0


def trending(n=800, seed=0, drift=0.0005):
    rng = np.random.default_rng(seed)
    closes = 100 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    return bars(closes=closes, start="2021-01-01")


def test_walk_forward_report_structure_and_gates():
    data = {f"S{i}": trending(seed=i) for i in range(3)}
    strat = Scripted(signals={i: (1 if i % 20 == 0 else -1 if i % 20 == 10 else 0) for i in range(800)}, warmup=30)
    report = WalkForwardValidator(strategy=strat, position_sizing_mode="full_equity",
                                  min_oos_trades=10).run(data, benchmark=trending(seed=99))

    assert report.out_of_sample.start > report.in_sample.end
    assert report.out_of_sample.total_trades >= 10
    assert len(report.folds) >= 3
    assert {g.name for g in report.gates} >= {
        "Sample size", "Beats Nifty buy-and-hold", "Walk-forward retention",
        "Profit factor", "Expectancy", "Max drawdown", "Consistency across folds"}
    assert set(report.per_symbol) == set(data)


def test_out_of_sample_trading_starts_at_split_not_after_warmup():
    # A 200-bar warm-up in the OOS slice was the old bug; with prior history
    # available, the first OOS bar must already be tradable.
    data = {"S": trending(seed=1)}
    strat = Scripted(signals={i: (1 if i % 20 == 0 else -1 if i % 20 == 10 else 0) for i in range(800)}, warmup=200)
    validator = WalkForwardValidator(strategy=strat, position_sizing_mode="full_equity")
    _, split, _ = validator.split_date(data)
    report = validator.run(data)

    first_oos_trade = min(t.entry_time for t in report.per_symbol["S"][1].trades)
    assert (first_oos_trade - split).days < 25
