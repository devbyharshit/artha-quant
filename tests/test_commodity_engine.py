"""Tests for CommodityBacktestEngine: long/short momentum execution on an MCX
futures overlay, next-bar-open fills, direction-aware costs and slippage,
forced rollover exits, and the two plan Review Focus items this task owns:
(1) an overlay gap on/after the forced-rollover date, (2) a signal flip on
the same bar the forced rollover fires.

Entirely synthetic fixtures. Lookbacks are shrunk (momentum 5 bars, vol 3
bars) so every signal flip and fill bar can be located by hand; the
production defaults (252/60) are exercised only by the constructor-defaults
test."""
import numpy as np
import pandas as pd
import pytest

from backtesting import commodity_engine as ce
from backtesting.commodity_cost_model import IndianCommodityCostModel
from backtesting.commodity_engine import CommodityBacktestEngine
from backtesting.commodity_signals import (
    calculate_vol_target_position_size,
    compute_momentum_signal,
    compute_realized_vol,
)

LOOKBACK = 5
VOL_LOOKBACK = 3
SLIP = 0.0005


def make_engine(**kw):
    kw.setdefault("initial_capital", 100000.0)
    kw.setdefault("lookback_days", LOOKBACK)
    kw.setdefault("vol_lookback_days", VOL_LOOKBACK)
    return CommodityBacktestEngine(**kw)


def continuous(closes, start="2024-01-01"):
    closes = np.asarray(closes, dtype=float)
    idx = pd.date_range(start, periods=len(closes), freq="D", name="timestamp")
    return pd.DataFrame({
        "open": closes, "high": closes + 1, "low": closes - 1, "close": closes,
    }, index=idx)


def overlay(dates, fut_opens, fut_closes=None, lot_size=10, expiry=None, tradingsymbol="GOLDMFUT",
            skip=()):
    """Futures-overlay table, same shape/idiom as tests/test_futures_engine.py.
    `skip` = positions (into `dates`) left OUT entirely (no contract listed).
    `expiry`/`tradingsymbol` may be per-position lists to simulate the data
    layer switching the active front-month contract. Default expiry is far
    beyond the data so no rollover fires unless a test engineers one."""
    fut_opens = np.asarray(fut_opens, dtype=float)
    fut_closes = fut_opens.copy() if fut_closes is None else np.asarray(fut_closes, dtype=float)
    far = dates[-1] + pd.Timedelta(days=365)
    rows, kept = [], []
    for i, d in enumerate(dates):
        if i in skip:
            continue
        kept.append(d)
        row_expiry = expiry[i] if isinstance(expiry, (list, tuple)) else (expiry if expiry is not None else far)
        row_ts = tradingsymbol[i] if isinstance(tradingsymbol, (list, tuple)) else tradingsymbol
        rows.append({
            "fut_open": fut_opens[i], "fut_high": fut_opens[i] + 1, "fut_low": fut_opens[i] - 1,
            "fut_close": fut_closes[i], "lot_size": lot_size, "expiry": row_expiry,
            "tradingsymbol": row_ts,
        })
    return pd.DataFrame(rows, index=pd.DatetimeIndex(kept, name="timestamp"))


def zig_up(n, start=100.0, slope=2.0):
    """Steady uptrend with a +1 zigzag so realized vol is strictly positive."""
    return [start + slope * i + (i % 2) for i in range(n)]


def assert_no_same_bar_double_fill(trades):
    """Trades are strictly sequential: one position at a time, and a fresh
    entry never lands on the same bar as the previous close."""
    for prev, nxt in zip(trades, trades[1:]):
        assert nxt.entry_time > prev.exit_time
    entry_times = [t.entry_time for t in trades]
    assert len(entry_times) == len(set(entry_times))


# --- Rollover scenario shared by several tests -------------------------------
# n=30 daily bars, run bounded to end=dates[20]. Signal (5-bar momentum) turns
# long at bar 5 -> entry queued at bar 5 close, filled at bar 6 open.
# OLD contract expires on bar 14; the data layer switches the active contract
# to NEW (expiry bar 29) from bar 12 onward. Buffer=3: at bar 11, 14-11=3 <= 3
# -> forced exit decided at bar 11 close, filled at bar 12 open.
N_ROLL = 30
ROLL_DATES = pd.date_range("2024-01-01", periods=N_ROLL, freq="D", name="timestamp")
OLD_EXPIRY = ROLL_DATES[14]
NEW_EXPIRY = ROLL_DATES[29]
SWITCH_BAR = 12


def roll_overlay(closes, skip=(), switch_bar=SWITCH_BAR):
    expiries = [OLD_EXPIRY if i < switch_bar else NEW_EXPIRY for i in range(N_ROLL)]
    tss = ["GOLDMOLD" if i < switch_bar else "GOLDMNEW" for i in range(N_ROLL)]
    fut = np.asarray(closes, dtype=float) + 100.0
    return overlay(ROLL_DATES, fut_opens=fut, expiry=expiries, tradingsymbol=tss, skip=skip)


# --- Basic long/short behaviour ----------------------------------------------

def test_engine_opens_long_and_short_positions():
    up = zig_up(15)
    down = [up[-1] - 2.0 * (j + 1) + ((j + 1) % 2) for j in range(20)]
    closes = up + down
    cdf = continuous(closes)
    fut = np.asarray(closes) + 100.0
    ov = overlay(cdf.index, fut_opens=fut)

    summary = make_engine().run(cdf, ov, symbol="GOLDM")

    directions = [t.direction for t in summary.trades]
    assert "LONG" in directions and "SHORT" in directions
    assert directions.index("LONG") < directions.index("SHORT")
    assert_no_same_bar_double_fill(summary.trades)

    long_t = next(t for t in summary.trades if t.direction == "LONG")
    short_t = next(t for t in summary.trades if t.direction == "SHORT")
    assert long_t.exit_reason == "SIGNAL_FLIP"
    assert short_t.exit_reason == "END_OF_DATA"
    # Short profits from a falling price.
    assert short_t.exit_price < short_t.entry_price
    assert short_t.gross_pnl > 0
    assert short_t.gross_pnl == pytest.approx(short_t.entry_value - short_t.exit_value)
    # Ending capital reconciles with the trade log and the equity curve.
    assert summary.ending_capital == pytest.approx(100000.0 + sum(t.net_pnl for t in summary.trades))
    assert summary.equity_curve.iloc[-1] == pytest.approx(summary.ending_capital)
    assert summary.total_trades == len(summary.trades)


def test_signal_flip_is_close_then_reopen_on_next_bar():
    up = zig_up(15)
    down = [up[-1] - 2.0 * (j + 1) + ((j + 1) % 2) for j in range(20)]
    closes = up + down
    cdf = continuous(closes)
    ov = overlay(cdf.index, fut_opens=np.asarray(closes) + 100.0)
    sig = compute_momentum_signal(cdf["close"], lookback_days=LOOKBACK)
    flip_bar = next(i for i in range(1, len(sig)) if sig.iloc[i - 1] == 1 and sig.iloc[i] == -1)

    summary = make_engine().run(cdf, ov, symbol="GOLDM")
    long_t, short_t = summary.trades[0], summary.trades[1]
    # Flip seen at flip_bar close -> LONG closed at flip_bar+1 open, SHORT
    # opened one bar later (flat for one bar, never a same-bar swap).
    assert long_t.exit_time == cdf.index[flip_bar + 1]
    assert short_t.entry_time == cdf.index[flip_bar + 2]


def test_short_trade_fills_slippage_against_us_and_costs_use_reversed_legs(monkeypatch):
    up = zig_up(15)
    down = [up[-1] - 2.0 * (j + 1) + ((j + 1) % 2) for j in range(20)]
    closes = up + down
    cdf = continuous(closes)
    fut = np.asarray(closes) + 100.0
    ov = overlay(cdf.index, fut_opens=fut)

    calls = []
    real_calc = IndianCommodityCostModel.calculate

    def spy(buy_value, sell_value):
        calls.append((buy_value, sell_value))
        return real_calc(buy_value, sell_value)

    monkeypatch.setattr(ce.IndianCommodityCostModel, "calculate", staticmethod(spy))
    summary = make_engine().run(cdf, ov, symbol="GOLDM")

    long_t = next(t for t in summary.trades if t.direction == "LONG")
    short_t = next(t for t in summary.trades if t.direction == "SHORT")

    # Slippage: long buys high / sells low; short sells high... i.e. sells
    # BELOW the open on entry and buys ABOVE the open on exit.
    li = cdf.index.get_loc(long_t.entry_time)
    si = cdf.index.get_loc(short_t.entry_time)
    assert long_t.entry_price == round(fut[li] * (1 + SLIP), 2)
    assert short_t.entry_price == round(fut[si] * (1 - SLIP), 2)
    # Short closes END_OF_DATA at the last bar's fut_close, buying.
    assert short_t.exit_price == round(fut[-1] * (1 + SLIP), 2)

    # Costs: long -> buy=entry, sell=exit. Short -> sell=entry, buy=exit.
    assert (long_t.entry_value, long_t.exit_value) in calls
    assert (short_t.exit_value, short_t.entry_value) in calls
    assert short_t.friction_costs == pytest.approx(
        real_calc(buy_value=short_t.exit_value, sell_value=short_t.entry_value).total_friction)
    assert long_t.friction_costs == pytest.approx(
        real_calc(buy_value=long_t.entry_value, sell_value=long_t.exit_value).total_friction)
    assert short_t.net_pnl == pytest.approx(short_t.gross_pnl - short_t.friction_costs)


def test_sizing_uses_realized_vol_known_before_the_fill_bar():
    closes = zig_up(12)
    closes[6] = closes[6] * 1.5  # violent move ON the fill bar (bar 6) -> vol[6] >> vol[5]
    cdf = continuous(closes)
    fut = np.asarray(closes) + 100.0
    ov = overlay(cdf.index, fut_opens=fut, lot_size=10)
    vol = compute_realized_vol(cdf["close"], vol_lookback_days=VOL_LOOKBACK)
    assert compute_momentum_signal(cdf["close"], lookback_days=LOOKBACK).iloc[5] == 1

    summary = make_engine().run(cdf, ov, symbol="GOLDM")
    t = summary.trades[0]
    assert t.entry_time == cdf.index[6]
    fill = round(fut[6] * (1 + SLIP), 2)
    expected = calculate_vol_target_position_size(100000.0, 35.0, fill, 10, float(vol.iloc[5]), margin_pct=25.0)
    lookahead = calculate_vol_target_position_size(100000.0, 35.0, fill, 10, float(vol.iloc[6]), margin_pct=25.0)
    assert expected.quantity != lookahead.quantity  # the test can tell them apart
    assert t.quantity == expected.quantity
    assert t.quantity % 10 == 0 and t.lot_size == 10


def test_no_trades_during_warmup_and_zero_quantity_is_not_an_error():
    cdf = continuous(zig_up(20))
    # A lot so large one lot's risk exceeds the whole vol budget -> qty 0.
    ov = overlay(cdf.index, fut_opens=np.asarray(zig_up(20)) + 100.0, lot_size=10_000_000)
    summary = make_engine().run(cdf, ov, symbol="GOLDM")
    assert summary.total_trades == 0
    assert summary.ending_capital == 100000.0


def test_constructor_defaults_match_global_constraints():
    e = CommodityBacktestEngine()
    assert e.vol_target_pct == 35.0
    assert e.margin_pct == 25.0
    assert e.slippage_pct == 0.0005
    assert e.rollover_buffer_days == 3
    assert e.initial_capital == 100000.0
    assert e.lookback_days == 252
    assert e.vol_lookback_days == 60


# --- Forced rollover ---------------------------------------------------------

def test_engine_forces_rollover_exit_before_expiry():
    closes = zig_up(N_ROLL)
    cdf = continuous(closes)
    ov = roll_overlay(closes)

    summary = make_engine().run(cdf, ov, symbol="GOLDM", end=ROLL_DATES[20])

    rollover = [t for t in summary.trades if t.exit_reason == "ROLLOVER"]
    assert len(rollover) == 1
    rt = rollover[0]
    assert rt.direction == "LONG"
    assert rt.tradingsymbol == "GOLDMOLD"
    assert rt.entry_time == ROLL_DATES[6]
    assert rt.exit_time == ROLL_DATES[SWITCH_BAR]
    assert rt.exit_price == round((closes[SWITCH_BAR] + 100.0) * (1 - SLIP), 2)
    # Signal still long -> fresh re-entry on the NEW contract, the bar after.
    reentry = summary.trades[1]
    assert reentry.direction == "LONG"
    assert reentry.entry_time == ROLL_DATES[SWITCH_BAR + 1]
    assert reentry.tradingsymbol == "GOLDMNEW"
    assert reentry.exit_reason == "END_OF_DATA"
    assert_no_same_bar_double_fill(summary.trades)


def test_short_position_is_also_force_rolled():
    closes = [200.0 - 2.0 * i + (i % 2) for i in range(N_ROLL)]
    cdf = continuous(closes)
    ov = roll_overlay(closes)
    summary = make_engine().run(cdf, ov, symbol="GOLDM", end=ROLL_DATES[20])
    assert summary.trades[0].direction == "SHORT"
    assert summary.trades[0].exit_reason == "ROLLOVER"
    assert summary.trades[0].exit_time == ROLL_DATES[SWITCH_BAR]
    assert summary.trades[1].direction == "SHORT"
    assert summary.trades[1].tradingsymbol == "GOLDMNEW"


def test_reentry_waits_while_overlay_still_lists_contract_inside_buffer():
    # Data layer keeps listing the expiring OLD contract until after its
    # expiry (switch at bar 15). Re-entering OLD inside its own buffer would
    # just be force-closed again next bar, so the entry waits for NEW.
    closes = zig_up(N_ROLL)
    cdf = continuous(closes)
    ov = roll_overlay(closes, switch_bar=15)
    summary = make_engine().run(cdf, ov, symbol="GOLDM", end=ROLL_DATES[20])

    assert summary.trades[0].exit_reason == "ROLLOVER"
    assert summary.trades[0].exit_time == ROLL_DATES[12]
    assert len(summary.trades) == 2
    assert summary.trades[1].entry_time == ROLL_DATES[15]
    assert summary.trades[1].tradingsymbol == "GOLDMNEW"


# --- Review Focus 1: overlay gap at the forced-rollover date -----------------

@pytest.mark.parametrize("skip, resume_bar", [
    ({12, 13}, 14),                     # gap on the fill bar(s) right after the decision
    ({11, 12, 13}, 14),                 # gap starts ON the decision bar itself
    ({11, 12, 13, 14, 15, 16}, 17),     # gap spans past the old contract's expiry
])
def test_engine_waits_when_overlay_missing_on_forced_rollover_date(skip, resume_bar):
    closes = zig_up(N_ROLL)
    cdf = continuous(closes)
    gap_ov = roll_overlay(closes, skip=skip)

    summary = make_engine().run(cdf, gap_ov, symbol="GOLDM", end=ROLL_DATES[20])

    # Never crashes, never fills on a date with no overlay row.
    assert all(t.exit_time in gap_ov.index for t in summary.trades)
    assert all(t.entry_time in gap_ov.index for t in summary.trades)
    # The forced exit is NOT cancelled: it fires as ROLLOVER on the first
    # date overlay data resumes, at that date's open.
    rt = summary.trades[0]
    assert rt.exit_reason == "ROLLOVER"
    assert rt.tradingsymbol == "GOLDMOLD"
    assert rt.exit_time == ROLL_DATES[resume_bar]
    assert rt.exit_price == round((closes[resume_bar] + 100.0) * (1 - SLIP), 2)
    # Then exactly one re-entry, on the new contract, the following bar.
    assert summary.trades[1].entry_time == ROLL_DATES[resume_bar + 1]
    assert summary.trades[1].tradingsymbol == "GOLDMNEW"
    assert len(summary.trades) == 2
    assert_no_same_bar_double_fill(summary.trades)


def test_open_position_is_closed_not_dropped_when_overlay_never_resumes():
    closes = zig_up(N_ROLL)
    cdf = continuous(closes)
    # Overlay vanishes from bar 12 to the end of the run window (bar 20).
    gap_ov = roll_overlay(closes, skip=set(range(12, N_ROLL)))
    summary = make_engine().run(cdf, gap_ov, symbol="GOLDM", end=ROLL_DATES[20])

    assert len(summary.trades) == 1
    t = summary.trades[0]
    assert t.exit_time in gap_ov.index
    assert t.exit_time == ROLL_DATES[11]  # last date with any overlay row
    assert t.exit_reason == "END_OF_DATA"
    assert t.exit_price == round((closes[11] + 100.0) * (1 - SLIP), 2)
    assert summary.ending_capital == pytest.approx(100000.0 + t.net_pnl)
    assert summary.equity_curve.iloc[-1] == pytest.approx(summary.ending_capital)


# --- Review Focus 2: signal flip inside the rollover buffer ------------------

def _flip_closes(flip_bar):
    """Uptrend, then a crash on `flip_bar` so 5-bar momentum goes 1 -> -1
    exactly there, then a downtrend that keeps it -1."""
    closes = zig_up(flip_bar)
    base = closes[flip_bar - LOOKBACK] - 6.0
    closes += [base - 2.0 * j + (j % 2) for j in range(N_ROLL - flip_bar)]
    return closes


def test_engine_handles_signal_flip_inside_rollover_buffer():
    # Flip lands on bar 11 — the same bar the forced rollover is decided.
    closes = _flip_closes(11)
    cdf = continuous(closes)
    sig = compute_momentum_signal(cdf["close"], lookback_days=LOOKBACK)
    assert sig.iloc[10] == 1 and sig.iloc[11] == -1 and (sig.iloc[11:21] == -1).all()
    ov = roll_overlay(closes)

    summary = make_engine().run(cdf, ov, symbol="GOLDM", end=ROLL_DATES[20])

    # Exactly one close then exactly one fresh entry — no skipped exit, no
    # second simultaneous position, no direct swap.
    assert len(summary.trades) == 2
    first, second = summary.trades
    assert first.direction == "LONG"
    assert first.exit_time == ROLL_DATES[12]
    assert first.exit_reason == "ROLLOVER"  # forced exit takes priority over the flip
    assert second.direction == "SHORT"
    assert second.entry_time == ROLL_DATES[13]
    assert second.tradingsymbol == "GOLDMNEW"
    assert second.exit_reason == "END_OF_DATA"
    assert_no_same_bar_double_fill(summary.trades)


def test_signal_flip_on_the_rollover_fill_bar_is_one_close_one_entry():
    # Flip lands on bar 12 — the bar the rollover exit actually fills.
    closes = _flip_closes(12)
    cdf = continuous(closes)
    sig = compute_momentum_signal(cdf["close"], lookback_days=LOOKBACK)
    assert sig.iloc[11] == 1 and sig.iloc[12] == -1
    ov = roll_overlay(closes)

    summary = make_engine().run(cdf, ov, symbol="GOLDM", end=ROLL_DATES[20])
    assert [t.direction for t in summary.trades] == ["LONG", "SHORT"]
    assert summary.trades[0].exit_reason == "ROLLOVER"
    assert summary.trades[0].exit_time == ROLL_DATES[12]
    assert summary.trades[1].entry_time == ROLL_DATES[13]
    assert_no_same_bar_double_fill(summary.trades)


def test_signal_flip_one_bar_before_rollover_exits_as_signal_flip_once():
    # Flip on bar 10 (one bar before the rollover decision): the SIGNAL_FLIP
    # exit is already pending when the buffer is entered — it must not be
    # double-processed into a second exit.
    closes = _flip_closes(10)
    cdf = continuous(closes)
    ov = roll_overlay(closes)
    summary = make_engine().run(cdf, ov, symbol="GOLDM", end=ROLL_DATES[20])

    assert [t.direction for t in summary.trades] == ["LONG", "SHORT"]
    assert summary.trades[0].exit_reason == "SIGNAL_FLIP"
    assert summary.trades[0].exit_time == ROLL_DATES[11]
    assert summary.trades[1].entry_time == ROLL_DATES[12]
    assert summary.trades[1].tradingsymbol == "GOLDMNEW"
    assert_no_same_bar_double_fill(summary.trades)


def test_flip_inside_rollover_buffer_combined_with_overlay_gap():
    # Both Review Focus items at once: flip on the decision bar AND the
    # overlay is missing on the fill bars.
    closes = _flip_closes(11)
    cdf = continuous(closes)
    ov = roll_overlay(closes, skip={12, 13})
    summary = make_engine().run(cdf, ov, symbol="GOLDM", end=ROLL_DATES[20])

    assert [t.direction for t in summary.trades] == ["LONG", "SHORT"]
    assert summary.trades[0].exit_reason == "ROLLOVER"
    assert summary.trades[0].exit_time == ROLL_DATES[14]
    assert summary.trades[1].entry_time == ROLL_DATES[15]
    assert all(t.exit_time in ov.index and t.entry_time in ov.index for t in summary.trades)
    assert_no_same_bar_double_fill(summary.trades)
