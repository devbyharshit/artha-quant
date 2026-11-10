"""Tests for backtest-time demerger adjustment, bad-open repair and universe filtering."""
import numpy as np
import pandas as pd
import pytest

from data.cleaners import corporate_actions as ca


def frame(closes, opens=None, start="2025-10-08", tz="Asia/Kolkata"):
    closes = np.asarray(closes, dtype=float)
    opens = closes.copy() if opens is None else np.asarray(opens, dtype=float)
    idx = pd.date_range(start, periods=len(closes), freq="D", tz=tz, name="timestamp")
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) + 1,
                         "low": np.minimum(opens, closes) - 1, "close": closes,
                         "volume": np.full(len(closes), 1000)}, index=idx)


def test_demerger_scales_history_and_removes_gap(monkeypatch):
    monkeypatch.setitem(ca.DEMERGERS, "XYZ", ["2025-10-11"])
    df = frame([100, 100, 100, 70, 72], opens=[100, 100, 100, 75, 70])
    out = ca.adjust_demergers("XYZ", df)

    factor = 75 / 100
    assert out["close"].iloc[2] == pytest.approx(100 * factor)
    assert out["open"].iloc[3] == 75                     # ex-date bar untouched
    assert out["close"].iloc[3] == 70                    # its own open-to-close move stays real
    assert out["volume"].iloc[0] == 1000                 # share count unchanged by a demerger
    assert out["open"].iloc[3] / out["close"].iloc[2] == pytest.approx(1.0)


def test_symbols_without_demergers_are_unchanged():
    df = frame([100, 90, 80])
    pd.testing.assert_frame_equal(ca.adjust_demergers("RELIANCE", df), df)


def test_start_after_drops_history_before_last_demerger(monkeypatch):
    monkeypatch.setitem(ca.START_AFTER, "XYZ", "2025-10-10")
    out = ca.apply_start_after("XYZ", frame([1, 2, 3, 4]))
    assert out.index[0] == pd.Timestamp("2025-10-10", tz="Asia/Kolkata")


def test_isolated_bad_open_is_repaired():
    df = frame([100, 100, 101, 102], opens=[100, 100, 87, 102])
    out = ca.fix_bad_opens(df, market_days=set())

    assert out["open"].iloc[2] == 100                    # previous close
    assert out["low"].iloc[2] == 100                     # min of fixed open and close
    assert out["close"].iloc[2] == 101


def test_bad_open_upward_repairs_high():
    df = frame([100, 100, 101], opens=[100, 100, 115])
    out = ca.fix_bad_opens(df, market_days=set())
    assert out["open"].iloc[2] == 100
    assert out["high"].iloc[2] == 101


def test_market_wide_gap_day_is_left_alone():
    df = frame([100, 100, 101], opens=[100, 100, 87])
    day = df.index[2].normalize()
    out = ca.fix_bad_opens(df, market_days={day})
    assert out["open"].iloc[2] == 87


def test_real_gap_that_holds_is_left_alone():
    df = frame([100, 100, 85], opens=[100, 100, 87])
    out = ca.fix_bad_opens(df, market_days=set())
    assert out["open"].iloc[2] == 87


def test_market_gap_days_needs_several_symbols():
    gapped = frame([100, 100, 99], opens=[100, 100, 90])
    calm = frame([100, 100, 100])
    few = ca.market_gap_days({f"S{i}": gapped for i in range(ca.MARKET_GAP_MIN_SYMBOLS - 1)} | {"C": calm})
    many = ca.market_gap_days({f"S{i}": gapped for i in range(ca.MARKET_GAP_MIN_SYMBOLS)})
    assert few == set()
    assert many == {gapped.index[2].normalize()}


def test_prepare_ignores_etf_gaps_when_detecting_market_days():
    etf_gap = frame([100, 100, 99], opens=[100, 100, 87])
    raw = {s: etf_gap for s in list(ca.NON_EQUITY)[:ca.MARKET_GAP_MIN_SYMBOLS]}
    raw["STOCK"] = etf_gap
    out = ca.prepare(raw)
    assert out["STOCK"]["open"].iloc[2] == 100           # ETF prints don't make it a market day
