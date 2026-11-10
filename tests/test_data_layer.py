"""Unit tests for Sprint 2 data layer: rate limiting, validation, and Parquet store."""
import time
from datetime import datetime, timedelta, date
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

from data.loaders.rate_limiter import RateLimiter
from data.cleaners.validator import validate_ohlcv, clean_ohlcv
from data.store.parquet_store import ParquetStore
from data.loaders.sync_history import load_universe


def test_rate_limiter_enforces_interval():
    """Verify that RateLimiter enforces minimum delay between successive calls."""
    interval = 0.1  # 100 ms for fast test
    limiter = RateLimiter(min_interval_seconds=interval)

    start = time.time()
    limiter.wait()
    limiter.wait()
    limiter.wait()
    elapsed = time.time() - start

    # 3 calls require at least 2 full intervals (0.2s)
    assert elapsed >= 0.18


def test_universe_yaml_structure():
    """Verify universe.yaml contains valid symbols and sectors."""
    assets = load_universe()
    assert len(assets) >= 20
    for asset in assets:
        assert "symbol" in asset
        assert "exchange" in asset
        assert asset["exchange"] in ("NSE", "BSE")
        assert "sector" in asset


def create_sample_ohlcv(bars: int = 30) -> pd.DataFrame:
    """Helper creating valid sample OHLCV data."""
    dates = pd.date_range("2026-01-01", periods=bars, freq="D")
    base_price = 1000.0
    opens = base_price + np.random.uniform(-10, 10, bars)
    highs = opens + np.random.uniform(5, 20, bars)
    lows = opens - np.random.uniform(5, 20, bars)
    closes = lows + (highs - lows) * np.random.uniform(0.1, 0.9, bars)
    volumes = np.random.randint(100000, 500000, bars)

    df = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes
    }, index=pd.Index(dates, name="timestamp"))
    return df


def test_validator_clean_data():
    """Verify that standard clean data passes all validation checks."""
    df = create_sample_ohlcv(30)
    report = validate_ohlcv(df, symbol="RELIANCE")
    assert report.is_valid is True
    assert report.corrupted_bars_count == 0
    assert len(report.errors) == 0


def test_validator_detects_corrupted_ohlc():
    """Verify that invalid OHLC relationships (e.g. High < Open) are caught."""
    df = create_sample_ohlcv(10)
    # Corrupt bar 3: set high lower than open
    df.iloc[3, df.columns.get_loc("high")] = df.iloc[3, df.columns.get_loc("open")] - 10.0

    report = validate_ohlcv(df, symbol="CORRUPTED")
    assert report.is_valid is False
    assert report.corrupted_bars_count >= 1
    assert any("violating basic OHLC bounds" in err for err in report.errors)


def test_validator_detects_negative_prices():
    """Verify that negative prices fail validation."""
    df = create_sample_ohlcv(10)
    df.iloc[2, df.columns.get_loc("close")] = -50.0

    report = validate_ohlcv(df, symbol="NEGATIVE")
    assert report.is_valid is False
    assert any("non-positive prices" in err for err in report.errors)


def test_clean_ohlcv_removes_corrupted_rows():
    """Verify clean_ohlcv drops corrupted rows while keeping valid rows."""
    df = create_sample_ohlcv(10)
    df.iloc[3, df.columns.get_loc("high")] = df.iloc[3, df.columns.get_loc("open")] - 10.0

    cleaned = clean_ohlcv(df)
    assert len(cleaned) == 9
    assert validate_ohlcv(cleaned).is_valid is True


def test_parquet_store_lifecycle(tmp_path):
    """Test full atomic write, read, latest date, and sync merge in ParquetStore."""
    store = ParquetStore(base_dir=tmp_path)
    symbol = "TCS"

    # 1. Save initial batch
    df1 = create_sample_ohlcv(10)
    store.save_bars_atomic(symbol, df1, timeframe="1D")

    # Verify no tmp files remain
    tmp_files = list(tmp_path.glob("**/*.tmp"))
    assert len(tmp_files) == 0

    # 2. Load and verify contents
    loaded = store.load_bars(symbol, timeframe="1D")
    assert len(loaded) == 10
    pd.testing.assert_series_equal(loaded["close"], df1["close"], check_freq=False)

    # 3. Check latest bar date
    latest_date = store.get_latest_bar_date(symbol, timeframe="1D")
    assert latest_date == df1.index.max().to_pydatetime()

    # 4. Incremental Sync with 5 overlapping + 5 new bars
    overlap_dates = pd.date_range(df1.index[-5], periods=10, freq="D")
    df2 = pd.DataFrame({
        "open": 2000.0,
        "high": 2050.0,
        "low": 1950.0,
        "close": 2020.0,
        "volume": 250000
    }, index=pd.Index(overlap_dates, name="timestamp"))

    synced = store.sync_bars(symbol, df2, timeframe="1D")
    # Total bars should be 10 + 5 new = 15 bars (duplicates replaced)
    assert len(synced) == 15
    assert synced.index.is_monotonic_increasing
