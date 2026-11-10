"""Atomic Parquet storage manager for local market data."""
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Union
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from configs.config import get_settings
from configs.logger import logger
from data.cleaners.validator import clean_ohlcv


class ParquetStore:
    """Manages atomic reads and writes of OHLCV bars in Apache Parquet format."""

    def __init__(self, base_dir: Optional[Path] = None):
        settings = get_settings()
        self.base_dir = base_dir or settings.DATA_STORE_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _get_file_path(self, symbol: str, timeframe: str = "1D") -> Path:
        tf_dir = self.base_dir / timeframe.upper()
        tf_dir.mkdir(parents=True, exist_ok=True)
        return tf_dir / f"{symbol.upper()}.parquet"

    def save_bars_atomic(self, symbol: str, df: pd.DataFrame, timeframe: str = "1D") -> Path:
        """
        Saves DataFrame to Parquet using an atomic temp-file rename
        to prevent partial writes during process interruptions.
        """
        target_path = self._get_file_path(symbol, timeframe)
        temp_path = target_path.with_suffix(".parquet.tmp")

        if df.empty:
            logger.warning(f"Attempted to save empty DataFrame for {symbol}. Skipped.")
            return target_path

        # Ensure index is datetime and named timestamp
        cleaned_df = clean_ohlcv(df)
        if cleaned_df.index.name != "timestamp":
            cleaned_df.index.name = "timestamp"

        table = pa.Table.from_pandas(cleaned_df)
        pq.write_table(table, str(temp_path), compression="snappy")

        # Atomic replace
        os.replace(temp_path, target_path)
        logger.debug(f"Saved {len(cleaned_df)} bars for {symbol} ({timeframe}) to {target_path}")
        return target_path

    def load_bars(
        self,
        symbol: str,
        timeframe: str = "1D",
        start_date: Optional[Union[datetime, str]] = None,
        end_date: Optional[Union[datetime, str]] = None
    ) -> pd.DataFrame:
        """Load OHLCV bars for symbol."""
        file_path = self._get_file_path(symbol, timeframe)
        if not file_path.exists():
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.read_parquet(file_path, engine="pyarrow")
        df.sort_index(inplace=True)

        if start_date is not None:
            df = df.loc[df.index >= pd.to_datetime(start_date)]
        if end_date is not None:
            df = df.loc[df.index <= pd.to_datetime(end_date)]

        return df

    def get_latest_bar_date(self, symbol: str, timeframe: str = "1D") -> Optional[datetime]:
        """Returns the timestamp of the most recent bar on disk, or None if no data."""
        file_path = self._get_file_path(symbol, timeframe)
        if not file_path.exists():
            return None

        try:
            # Read metadata without loading the entire dataset into memory
            metadata = pq.read_metadata(str(file_path))
            if metadata.num_rows == 0:
                return None
            df = pd.read_parquet(file_path, columns=["close"], engine="pyarrow")
            if df.empty:
                return None
            return df.index.max().to_pydatetime()
        except Exception as exc:
            logger.warning(f"Error reading latest bar date for {symbol}: {exc}")
            return None

    def get_earliest_bar_date(self, symbol: str, timeframe: str = "1D") -> Optional[datetime]:
        """Returns the timestamp of the oldest bar on disk, or None if no data."""
        file_path = self._get_file_path(symbol, timeframe)
        if not file_path.exists():
            return None
        df = pd.read_parquet(file_path, columns=["close"], engine="pyarrow")
        return None if df.empty else df.index.min().to_pydatetime()

    def sync_bars(self, symbol: str, new_bars_df: pd.DataFrame, timeframe: str = "1D") -> pd.DataFrame:
        """
        Merges new bars with existing bars on disk, deduplicates by index,
        and atomically saves the consolidated series.
        """
        existing_df = self.load_bars(symbol, timeframe=timeframe)

        if existing_df.empty:
            consolidated = new_bars_df.copy()
        elif new_bars_df.empty:
            return existing_df
        else:
            consolidated = pd.concat([existing_df, new_bars_df])

        # Deduplicate and sort
        consolidated = consolidated[~consolidated.index.duplicated(keep="last")].sort_index()
        self.save_bars_atomic(symbol, consolidated, timeframe=timeframe)
        return consolidated


# Global helper instance
_parquet_store: Optional[ParquetStore] = None


def get_parquet_store() -> ParquetStore:
    global _parquet_store
    if _parquet_store is None:
        _parquet_store = ParquetStore()
    return _parquet_store
