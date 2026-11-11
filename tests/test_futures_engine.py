"""Tests for FuturesBacktestEngine execution realism: next-bar-open fills
against the futures overlay (not cash price), lot-size-rounded sizing,
forced rollover exits, re-entry on the new contract, and clean handling of
overlay gaps. Entirely synthetic fixtures — no dependency on the real
futures data loader or cost model (see backtesting/futures_engine.py's
import-fallback stub, used here exactly as in production when those modules
are absent)."""
import numpy as np
import pandas as pd

from strategies.base_strategy import BaseStrategy
from backtesting.futures_engine import FuturesBacktestEngine


class Scripted(BaseStrategy):
    """Emits given signals by bar position, after `warmup` NaN bars (same
    idiom as tests/test_walk_forward.py's Scripted fake strategy)."""

    def __init__(self, signals=None, warmup=0):
        super().__init__("Scripted", {})
        self.sig, self.warmup = signals or {}, warmup

    def calculate_indicators(self, df):
        ind = np.arange(len(df), dtype=float)
        ind[: self.warmup] = np.nan
        df["ind"] = ind
        return df

    def generate_signals(self, df):
        n = len(df)
        df["signal"] = [self.sig.get(i, 0) for i in range(n)]
        df["stop_loss"] = 0.0
        df["target"] = 0.0
        return df


def cash_bars(closes, start="2024-01-01"):
    closes = np.asarray(closes, dtype=float)
    idx = pd.date_range(start, periods=len(closes), freq="D", name="timestamp")
    return pd.DataFrame({
        "open": closes, "high": closes + 1, "low": closes - 1, "close": closes,
        "volume": np.full(len(closes), 100000),
    }, index=idx)


def overlay(dates, fut_opens, fut_closes=None, lot_size=100, expiry=None, tradingsymbol="TESTFUT",
            skip=()):
    """Builds a futures-overlay table. `skip` is a set of positions (into
    `dates`) to leave OUT of the overlay entirely, simulating a date with no
    futures data. `expiry`/`tradingsymbol` may each be a single value (same
    contract throughout) or a per-position list (simulating a real contract
    rollover: the active contract's expiry/tradingsymbol changes partway
    through, exactly as the real data layer's overlay table would)."""
    fut_opens = np.asarray(fut_opens, dtype=float)
    fut_closes = fut_opens.copy() if fut_closes is None else np.asarray(fut_closes, dtype=float)
    rows = []
    kept_dates = []
    for i, d in enumerate(dates):
        if i in skip:
            continue
        kept_dates.append(d)
        row_expiry = expiry[i] if isinstance(expiry, (list, tuple)) else (expiry if expiry is not None else dates[-1])
        row_ts = tradingsymbol[i] if isinstance(tradingsymbol, (list, tuple)) else tradingsymbol
        rows.append({
            "fut_open": fut_opens[i], "fut_high": fut_opens[i] + 1, "fut_low": fut_opens[i] - 1,
            "fut_close": fut_closes[i], "lot_size": lot_size,
            "expiry": row_expiry,
            "tradingsymbol": row_ts,
        })
    return pd.DataFrame(rows, index=pd.DatetimeIndex(kept_dates, name="timestamp"))


def run_engine(strategy, cash_df, overlay_df, end=None, **kw):
    kw.setdefault("allocation_pct", 100.0)
    kw.setdefault("margin_pct", 20.0)
    kw.setdefault("rollover_buffer_days", 3)
    engine = FuturesBacktestEngine(strategy=strategy, initial_capital=100000.0, **kw)
    return engine.run(cash_df, overlay_df, symbol="T", end=end)


def test_entry_fills_at_next_bar_overlay_open_not_cash_price():
    # Cash and futures opens deliberately diverge so a cash-price fill would
    # be unmistakably wrong.
    dates = pd.date_range("2024-01-01", periods=6, freq="D")
    cash_df = cash_bars([100, 101, 102, 103, 104, 105], start=dates[0])
    ov = overlay(dates, fut_opens=[200, 201, 500, 203, 204, 205])

    res = run_engine(Scripted(signals={1: 1}), cash_df, ov)

    assert res.total_trades >= 1
    trade = res.trades[0]
    assert trade.entry_time == dates[2]
    # entry fills at the overlay's fut_open for bar 2 (500), not cash open/close.
    assert trade.entry_price == round(500 * 1.0005, 2)


def test_lot_size_rounding_and_zero_quantity_when_unaffordable():
    dates = pd.date_range("2024-01-01", periods=5, freq="D")
    cash_df = cash_bars([100, 100, 100, 100, 100], start=dates[0])

    # Affordable: capital 100,000, allocation 100% -> budget 100,000.
    # margin_per_lot = lot_size(50) * price(200) * 20% = 2000 -> 50 lots affordable (100,000/2000).
    ov = overlay(dates, fut_opens=[200, 200, 200, 200, 200], lot_size=50)
    res = run_engine(Scripted(signals={1: 1}), cash_df, ov)
    trade = res.trades[0] if res.trades else None
    assert trade is not None
    assert trade.quantity % 50 == 0
    assert trade.quantity > 0

    # Unaffordable: a single lot costs more than the whole allocation budget.
    ov_expensive = overlay(dates, fut_opens=[200, 200, 200, 200, 200], lot_size=100000)
    res2 = run_engine(Scripted(signals={1: 1}), cash_df, ov_expensive)
    assert res2.total_trades == 0  # 0 qty -> no position opened, no crash


def test_forced_exit_before_expiry_with_rollover_reason_and_reentry():
    # Strategy goes long on bar 1 and NEVER emits an exit signal — the only
    # thing that should close the position is the rollover buffer.
    n = 20
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    cash_df = cash_bars([100] * n, start=dates[0])
    old_expiry = dates[6]    # old contract expires on bar 6
    new_expiry = dates[19]   # next front-month contract, expiring well past
                              # the window below so it doesn't roll again here
    # The data layer's rollover rule switches the "front-month active"
    # contract to the new one right as the old one enters its expiry
    # buffer window (bar 4 onward) — matching when our forced exit fills.
    expiries = [old_expiry if i < 4 else new_expiry for i in range(n)]
    tradingsymbols = ["OLDFUT" if i < 4 else "NEWFUT" for i in range(n)]
    fut_opens = [200 + i for i in range(n)]
    ov = overlay(dates, fut_opens=fut_opens, lot_size=10, expiry=expiries, tradingsymbol=tradingsymbols)

    # Bounded to bar 10, well short of the new contract's own expiry buffer,
    # so only the one, deliberately-engineered rollover should fire.
    res = run_engine(Scripted(signals={1: 1}), cash_df, ov, rollover_buffer_days=3, end=dates[10])

    rollover_trades = [t for t in res.trades if t.exit_reason == "ROLLOVER"]
    assert len(rollover_trades) == 1
    rt = rollover_trades[0]
    # buffer=3, expiry at bar 6 -> forced exit decided once days-to-expiry <= 3
    # (bar 3), filled at the NEXT bar's open (bar 4).
    assert rt.exit_time == dates[4]
    assert rt.exit_price == round(fut_opens[4] * (1 - 0.0005), 2)

    # Signal is still "long" (never emitted -1) -> re-entry happens on the
    # bar after the rollover exit, as a fresh trade.
    assert res.total_trades >= 2
    reentry = res.trades[1]
    assert reentry.exit_reason != "ROLLOVER" or reentry.entry_time > rt.exit_time
    assert reentry.entry_time == dates[5]
    assert reentry.tradingsymbol == "NEWFUT"  # entered fresh on the new front-month contract


def test_missing_overlay_date_is_skipped_cleanly_no_phantom_fill():
    n = 8
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    cash_df = cash_bars([100] * n, start=dates[0])
    fut_opens = [200 + i for i in range(n)]
    # Bar 2 (the date the entry would normally fill on) has NO overlay row.
    ov = overlay(dates, fut_opens=fut_opens, lot_size=10, skip={2})

    res = run_engine(Scripted(signals={1: 1}), cash_df, ov)

    # No exception, and no fill dated on the missing day.
    assert all(t.entry_time != dates[2] for t in res.trades)
    if res.trades:
        # The order waits for the next date that actually has overlay data.
        assert res.trades[0].entry_time == dates[3]
        assert res.trades[0].entry_price == round(fut_opens[3] * 1.0005, 2)
