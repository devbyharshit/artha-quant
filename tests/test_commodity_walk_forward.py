"""Tests for CommodityWalkForwardValidator: per-symbol own buy-and-hold
benchmarks, the history-length (not trade-count) sample gate, the Calmar
ratio, and that profit factor / expectancy are reported but never gate.

Synthetic fixture (built once per module, the engine loop is per-bar Python):
- Two commodities, ~11 calendar years of business-day bars (2013-01-01 ..
  2023-12-29), so with train_ratio=0.50 the OOS window is ~5.5 years -- just
  over the default `min_oos_years=5.0`, and the production momentum/vol
  lookbacks (252/60 bars) have a full warm-up inside the in-sample half.
- GOLDM: seeded geometric random walk with a positive drift (trending).
- CRUDEOILM: seeded random walk (~29% annualized vol) around a slow ~4-year
  sine cycle in log price (trend reversals, so the momentum signal goes both
  long and short).
Realistic daily vols matter: vol-target sizing leverage is
vol_target / realized_vol and is not capped by margin, so an unrealistically
quiet synthetic series would lever up without bound.
- Overlay: one contract per calendar month, expiring on the month's last
  business day; the overlay keeps listing the held contract through its own
  expiry and only switches to the next month afterwards (the Task 8 data
  layer convention from the commodity_engine docstring), so the rollover
  buffer exit always fills on the held contract. fut_* = continuous close.
"""
import numpy as np
import pandas as pd
import pytest

from backtesting.commodity_walk_forward import (
    DEFAULT_MIN_CALMAR,
    CommodityPeriodStats,
    CommodityUniverseValidationReport,
    CommodityWalkForwardValidator,
    calmar_ratio,
)
from backtesting.futures_walk_forward import FuturesFoldResult

pytestmark = pytest.mark.filterwarnings("ignore::backtesting.commodity_engine.ContractMismatchWarning")

START, END = "2013-01-01", "2023-12-29"


def _frame(closes, idx):
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame({
        "open": closes, "high": closes * 1.005, "low": closes * 0.995, "close": closes,
    }, index=pd.DatetimeIndex(idx, name="timestamp"))


def _monthly_overlay(cont: pd.DataFrame, root: str, lot_size: int) -> pd.DataFrame:
    idx = cont.index
    month_key = idx.to_period("M")
    expiry_by_month = pd.Series(idx, index=idx).groupby(month_key).max()
    expiries = expiry_by_month.reindex(month_key).values
    syms = [f"{root}{p.strftime('%y%b').upper()}FUT" for p in month_key]
    c = cont["close"].values
    return pd.DataFrame({
        "fut_open": c, "fut_high": c * 1.005, "fut_low": c * 0.995, "fut_close": c,
        "lot_size": lot_size, "price_quote_unit": 1, "expiry": pd.to_datetime(expiries), "tradingsymbol": syms,
    }, index=idx)


def build_universe(start=START, end=END):
    idx = pd.bdate_range(start, end, name="timestamp")
    n = len(idx)
    rng = np.random.default_rng(7)
    gold = 30000.0 * np.exp(np.cumsum(0.0004 + 0.008 * rng.standard_normal(n)))
    t = np.arange(n)
    crude = 5000.0 * np.exp(0.35 * np.sin(2 * np.pi * t / 1000.0) + np.cumsum(0.018 * rng.standard_normal(n)))
    continuous = {"GOLDM": _frame(gold, idx), "CRUDEOILM": _frame(crude, idx)}
    overlays = {
        "GOLDM": _monthly_overlay(continuous["GOLDM"], "GOLDM", 10),
        "CRUDEOILM": _monthly_overlay(continuous["CRUDEOILM"], "CRUDEOILM", 10),
    }
    return continuous, overlays


@pytest.fixture(scope="module")
def universe():
    return build_universe()


@pytest.fixture(scope="module")
def continuous_data(universe):
    return universe[0]


@pytest.fixture(scope="module")
def overlay_data(universe):
    return universe[1]


@pytest.fixture(scope="module")
def validator():
    return CommodityWalkForwardValidator()


@pytest.fixture(scope="module")
def report(validator, continuous_data, overlay_data) -> CommodityUniverseValidationReport:
    return validator.run(continuous_data, overlay_data)


# --- the three brief tests -------------------------------------------------

def test_validator_computes_calmar_ratio(report):
    assert report.out_of_sample.calmar == pytest.approx(
        report.out_of_sample.cagr_pct / report.out_of_sample.max_drawdown_pct, rel=0.01
    ) if report.out_of_sample.max_drawdown_pct > 0 else True
    # Guard against the "or True" pass-through hiding a degenerate fixture.
    assert report.out_of_sample.max_drawdown_pct > 0


def test_validator_benchmarks_against_own_buy_and_hold_not_nifty(report, continuous_data):
    assert set(report.benchmarks_oos.keys()) == set(continuous_data.keys())


def test_validator_sample_size_gate_uses_history_length_not_trade_count(report):
    gate_names = [g.name for g in report.gates]
    assert "Sufficient OOS history" in gate_names
    assert "Sample size" not in gate_names  # the old trade-count floor must not carry over unexamined


# --- supporting tests ------------------------------------------------------

def test_gate_set_is_exactly_the_commodity_set(report):
    assert [g.name for g in report.gates] == [
        "Sufficient OOS history", "Beats own buy-and-hold", "Walk-forward retention",
        "Calmar ratio", "Max drawdown",
    ]
    names = {g.name for g in report.gates}
    assert not names & {"Beats Nifty buy-and-hold", "Profit factor", "Expectancy", "Sample size"}


def test_each_benchmark_is_that_symbols_own_series(report, continuous_data, validator):
    split = report.out_of_sample.start
    for sym, bench in report.benchmarks_oos.items():
        assert isinstance(bench, CommodityPeriodStats)
        window = continuous_data[sym].loc[bench.start:bench.end]
        gross = (window["close"].iloc[-1] / window["open"].iloc[0] - 1) * 100.0
        # After costs/tax the benchmark return is at or below the gross move.
        assert bench.net_return_pct <= gross + 1e-9
        assert bench.net_return_pct == pytest.approx(gross, abs=abs(gross) * 0.5 + 5.0)
        assert bench.start >= split - pd.Timedelta(days=3)
    a, b = (report.benchmarks_oos[s] for s in ("GOLDM", "CRUDEOILM"))
    assert a.net_return_pct != pytest.approx(b.net_return_pct)


def test_sufficient_oos_history_passes_on_5_5_year_oos(report):
    gate = next(g for g in report.gates if g.name == "Sufficient OOS history")
    span = (report.out_of_sample.end - report.out_of_sample.start).days
    assert span >= 5.0 * 365.25
    assert gate.passed


def test_sufficient_oos_history_fails_on_short_history(universe):
    continuous, overlays = universe
    short_c = {s: df.loc["2016-01-01":] for s, df in continuous.items()}  # 8y -> ~4y OOS
    short_o = {s: df.loc["2016-01-01":] for s, df in overlays.items()}
    rep = CommodityWalkForwardValidator().run(short_c, short_o)
    gate = next(g for g in rep.gates if g.name == "Sufficient OOS history")
    assert not gate.passed
    assert not rep.is_qualified


def test_profit_factor_and_expectancy_reported_but_not_gating(report):
    oos = report.out_of_sample
    assert oos.total_trades > 0
    detail = " ".join(g.detail for g in report.gates)
    assert f"{oos.profit_factor:.2f}" in detail
    assert f"{oos.expectancy_pct:+.2f}%" in detail


def test_is_qualified_ignores_profit_factor_and_expectancy(validator):
    def stats(**kw):
        base = dict(start=pd.Timestamp("2018-01-01"), end=pd.Timestamp("2024-01-01"),
                    cagr_pct=15.0, sharpe=1.2, calmar=1.5, max_drawdown_pct=10.0,
                    net_return_pct=120.0, total_trades=40)
        base.update(kw)
        return CommodityPeriodStats(**base)
    is_s = stats(sharpe=1.3)
    bad_pf = stats(profit_factor=0.4, expectancy_pct=-3.0)  # awful, but must not gate
    benches = {"X": stats(sharpe=0.5)}
    gates = validator._gates(is_s, bad_pf, benches, {"X": 1.2})
    assert all(g.passed for g in gates)
    rep = CommodityUniverseValidationReport(is_s, bad_pf, benches, [], gates)
    assert rep.is_qualified


def test_beats_own_buy_and_hold_is_checked_per_symbol(validator):
    s = dict(start=pd.Timestamp("2018-01-01"), end=pd.Timestamp("2024-01-01"), cagr_pct=15.0,
             calmar=1.5, max_drawdown_pct=10.0, net_return_pct=100.0)
    is_s = CommodityPeriodStats(sharpe=1.0, **s)
    oos = CommodityPeriodStats(sharpe=1.0, **s)
    benches = {"A": CommodityPeriodStats(sharpe=0.5, **s), "B": CommodityPeriodStats(sharpe=0.9, **s)}
    gates = {g.name: g for g in validator._gates(is_s, oos, benches, {"A": 0.8, "B": 0.6})}
    gate = gates["Beats own buy-and-hold"]
    assert not gate.passed  # B's strategy Sharpe 0.6 < B's own B&H 0.9
    assert "A" in gate.detail and "B" in gate.detail
    gates = {g.name: g for g in validator._gates(is_s, oos, benches, {"A": 0.8, "B": 1.1})}
    assert gates["Beats own buy-and-hold"].passed


def test_calmar_gate_uses_documented_default_threshold(validator):
    assert DEFAULT_MIN_CALMAR == 0.5
    assert validator.min_calmar == DEFAULT_MIN_CALMAR
    s = dict(start=pd.Timestamp("2018-01-01"), end=pd.Timestamp("2024-01-01"), sharpe=1.0,
             max_drawdown_pct=10.0, net_return_pct=50.0)
    is_s = CommodityPeriodStats(cagr_pct=8.0, calmar=0.8, **s)
    low = CommodityPeriodStats(cagr_pct=4.0, calmar=0.4, **s)
    ok = CommodityPeriodStats(cagr_pct=5.0, calmar=0.5, **s)
    g_low = {g.name: g for g in validator._gates(is_s, low, {}, {})}["Calmar ratio"]
    g_ok = {g.name: g for g in validator._gates(is_s, ok, {}, {})}["Calmar ratio"]
    assert not g_low.passed and g_ok.passed


def test_calmar_ratio_zero_drawdown_fallback():
    assert calmar_ratio(12.0, 6.0) == pytest.approx(2.0)
    assert calmar_ratio(12.0, 0.0) == 99.0  # positive CAGR, never drew down
    assert calmar_ratio(0.0, 0.0) == 0.0
    assert calmar_ratio(-5.0, 0.0) == 0.0


def test_folds_reuse_futures_fold_result_with_basket_benchmark(report, continuous_data):
    assert report.folds and all(isinstance(f, FuturesFoldResult) for f in report.folds)
    assert len(report.folds) in (11, 12)  # ~5.5y OOS / 6-month folds
    f = report.folds[0]
    # split lands exactly on the engine's seed-point date (2018-07-01) in this
    # fixture; the first fold must still be kept, starting at the first OOS bar.
    assert f.start == report.out_of_sample.start
    rets = []
    for df in continuous_data.values():
        prior = df.loc[:f.start - pd.Timedelta(seconds=1), "close"].iloc[-1]
        last = df.loc[f.start:f.end, "close"].iloc[-1]
        rets.append((last / prior - 1) * 100.0)
    assert f.benchmark_return_pct == pytest.approx(np.mean(rets))
    assert sum(f.trades for f in report.folds) <= report.out_of_sample.total_trades


def test_per_symbol_holds_is_and_oos_summaries(report, continuous_data):
    assert set(report.per_symbol) == set(continuous_data)
    for sym, (is_sum, oos_sum) in report.per_symbol.items():
        assert is_sum is not None and is_sum.symbol == sym
        assert oos_sum.symbol == sym


def test_symbol_without_overlay_is_skipped_from_trading(universe):
    continuous, overlays = universe
    rep = CommodityWalkForwardValidator().run(continuous, {"GOLDM": overlays["GOLDM"]})
    assert set(rep.per_symbol) == {"GOLDM"}
    gate = next(g for g in rep.gates if g.name == "Beats own buy-and-hold")
    assert "CRUDEOILM" not in gate.detail


def test_never_tradeable_symbol_is_labelled_not_silently_failed(universe):
    """GOLDM with a lot no Rs 1 lakh book can size (every attempt -> 0 lots):
    the "Beats own buy-and-hold" gate still FAILS (pass/fail logic unchanged),
    but its detail says why, so it can't be read as "no edge"."""
    continuous, overlays = universe
    huge = overlays["GOLDM"].copy()
    huge["lot_size"] = 10_000_000
    rep = CommodityWalkForwardValidator().run(continuous, {"GOLDM": huge, "CRUDEOILM": overlays["CRUDEOILM"]})

    _, goldm_oos = rep.per_symbol["GOLDM"]
    assert goldm_oos.total_trades == 0 and goldm_oos.never_sized
    assert not rep.per_symbol["CRUDEOILM"][1].never_sized
    gate = next(g for g in rep.gates if g.name == "Beats own buy-and-hold")
    assert not gate.passed
    goldm_part = next(p for p in gate.detail.split("; ") if "GOLDM" in p)
    crude_part = next(p for p in gate.detail.split("; ") if "CRUDEOILM" in p)
    assert "NEVER TRADEABLE AT THIS CAPITAL" in goldm_part
    assert f"{goldm_oos.zero_size_entry_count}/{goldm_oos.entry_attempt_count}" in goldm_part
    assert "NEVER TRADEABLE" not in crude_part


def test_partially_untradeable_symbol_gets_a_zero_size_note(validator):
    s = dict(start=pd.Timestamp("2018-01-01"), end=pd.Timestamp("2024-01-01"), cagr_pct=15.0,
             calmar=1.5, max_drawdown_pct=10.0, net_return_pct=100.0)
    is_s = CommodityPeriodStats(sharpe=1.0, **s)
    benches = {"A": CommodityPeriodStats(sharpe=0.5, **s), "B": CommodityPeriodStats(sharpe=0.5, **s)}
    gates = {g.name: g for g in validator._gates(is_s, is_s, benches, {"A": 0.1, "B": 0.8},
                                                 zero_size_counts={"A": (1120, 1124)})}
    gate = gates["Beats own buy-and-hold"]
    assert not gate.passed  # unchanged pass/fail logic
    a_part = next(p for p in gate.detail.split("; ") if "A 0.10 vs" in p)
    b_part = next(p for p in gate.detail.split("; ") if "B 0.80 vs" in p)
    assert "1120/1124 entry attempts sized to 0 lots" in a_part
    assert "sized to 0 lots" not in b_part


def test_constructor_defaults():
    v = CommodityWalkForwardValidator()
    assert (v.vol_target_pct, v.margin_pct, v.rollover_buffer_days, v.train_ratio, v.fold_months,
            v.capital_per_symbol, v.min_oos_years, v.min_sharpe_retention, v.max_drawdown_pct) == (
        35.0, 25.0, 3, 0.50, 6, 100000.0, 5.0, 0.60, 20.0)
