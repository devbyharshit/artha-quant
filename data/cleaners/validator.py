"""Data quality validation engine for market bars."""
from dataclasses import dataclass, field
from typing import List, Optional
import numpy as np
import pandas as pd

from configs.logger import logger
from data.loaders.exceptions import DataIntegrityError


@dataclass
class ValidationReport:
    """Summary of data sanity and integrity checks."""
    symbol: str
    is_valid: bool
    total_bars: int
    corrupted_bars_count: int = 0
    zero_volume_bars_count: int = 0
    outlier_bars_count: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def validate_ohlcv(
    df: pd.DataFrame,
    symbol: str = "UNKNOWN",
    allow_zero_volume: bool = False,
    atr_outlier_threshold: float = 4.0
) -> ValidationReport:
    """
    Validates OHLCV DataFrame integrity:
    1. High >= Open, High >= Close, Low <= Open, Low <= Close
    2. Prices strictly positive (> 0)
    3. Volume >= 0
    4. Outlier ATR spike check (possible split / bad tick)
    """
    report = ValidationReport(
        symbol=symbol,
        is_valid=True,
        total_bars=len(df)
    )

    if df.empty:
        report.is_valid = False
        report.errors.append("DataFrame is empty.")
        return report

    required_cols = {"open", "high", "low", "close", "volume"}
    missing = required_cols - set(df.columns)
    if missing:
        report.is_valid = False
        report.errors.append(f"Missing required columns: {missing}")
        return report

    # 1. Price positivity
    negative_prices = (
        (df["open"] <= 0) | (df["high"] <= 0) |
        (df["low"] <= 0) | (df["close"] <= 0)
    )
    if negative_prices.any():
        count = int(negative_prices.sum())
        report.corrupted_bars_count += count
        report.is_valid = False
        report.errors.append(f"Found {count} bars with non-positive prices (<= 0).")

    # 2. OHLC Logical Integrity
    ohlc_violations = (
        (df["high"] < df["open"]) |
        (df["high"] < df["close"]) |
        (df["low"] > df["open"]) |
        (df["low"] > df["close"])
    )
    if ohlc_violations.any():
        count = int(ohlc_violations.sum())
        report.corrupted_bars_count += count
        report.is_valid = False
        report.errors.append(
            f"Found {count} bars violating basic OHLC bounds (High >= Open/Close, Low <= Open/Close)."
        )

    # 3. Volume Check
    negative_volume = (df["volume"] < 0)
    if negative_volume.any():
        count = int(negative_volume.sum())
        report.corrupted_bars_count += count
        report.is_valid = False
        report.errors.append(f"Found {count} bars with negative volume.")

    zero_volume = (df["volume"] == 0)
    if zero_volume.any():
        count = int(zero_volume.sum())
        report.zero_volume_bars_count = count
        if not allow_zero_volume:
            report.warnings.append(f"Found {count} zero-volume trading bars.")

    # 4. Outlier Spike / Unadjusted Corporate Action Check
    if len(df) >= 20:
        # Approximate 14-period True Range
        high_low = df["high"] - df["low"]
        high_close = (df["high"] - df["close"].shift(1)).abs()
        low_close = (df["low"] - df["close"].shift(1)).abs()
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr14 = true_range.rolling(window=14).mean()

        outliers = (true_range > (atr_outlier_threshold * atr14)) & (atr14 > 0)
        if outliers.any():
            count = int(outliers.sum())
            report.outlier_bars_count = count
            report.warnings.append(
                f"Found {count} outlier bars where bar range exceeds {atr_outlier_threshold}x ATR(14). "
                "Possible unadjusted stock split, bonus, or extreme volatility."
            )

    return report


def clean_ohlcv(df: pd.DataFrame, drop_corrupted: bool = True) -> pd.DataFrame:
    """Removes invalid bars and duplicates, returning a clean dataframe."""
    cleaned = df.copy()

    # Deduplicate index
    cleaned = cleaned[~cleaned.index.duplicated(keep="last")]
    cleaned.sort_index(inplace=True)

    if drop_corrupted:
        valid_mask = (
            (cleaned["open"] > 0) &
            (cleaned["high"] >= cleaned["open"]) &
            (cleaned["high"] >= cleaned["close"]) &
            (cleaned["low"] <= cleaned["open"]) &
            (cleaned["low"] <= cleaned["close"]) &
            (cleaned["volume"] >= 0)
        )
        cleaned = cleaned[valid_mask]

    return cleaned
