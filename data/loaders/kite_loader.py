"""Kite historical data loader with rate limiting and automated chunking."""
from datetime import datetime, timedelta, date
from typing import Union, List, Optional
import pandas as pd
from kiteconnect import KiteConnect

from configs.logger import logger
from data.loaders.rate_limiter import kite_rate_limiter
from data.loaders.exceptions import DataIntegrityError, KiteRateLimitError


class KiteDataLoader:
    """Fetches historical OHLCV data from Kite Connect."""

    def __init__(self, kite: KiteConnect):
        self.kite = kite

    @kite_rate_limiter
    def _fetch_chunk(
        self,
        instrument_token: int,
        from_date: Union[datetime, date],
        to_date: Union[datetime, date],
        interval: str = "day"
    ) -> List[dict]:
        """Fetch a single slice of historical data under rate limits."""
        try:
            records = self.kite.historical_data(
                instrument_token=instrument_token,
                from_date=from_date,
                to_date=to_date,
                interval=interval,
                continuous=False,
                oi=False
            )
            return records or []
        except Exception as exc:
            err_msg = str(exc).lower()
            if "429" in err_msg or "too many" in err_msg:
                raise KiteRateLimitError(f"Kite API rate limit exceeded: {exc}")
            raise

    def fetch_ohlcv(
        self,
        instrument_token: int,
        from_date: Union[datetime, date],
        to_date: Union[datetime, date],
        interval: str = "day",
        chunk_days: int = 365
    ) -> pd.DataFrame:
        """
        Fetches historical data across a potentially wide date range
        by automatically partitioning requests into safe time chunks.
        """
        if isinstance(from_date, date) and not isinstance(from_date, datetime):
            from_date = datetime.combine(from_date, datetime.min.time())
        if isinstance(to_date, date) and not isinstance(to_date, datetime):
            to_date = datetime.combine(to_date, datetime.max.time())

        if from_date > to_date:
            raise ValueError(f"from_date ({from_date}) must be <= to_date ({to_date})")

        all_records: List[dict] = []
        current_start = from_date

        logger.debug(
            f"Fetching {interval} data for token {instrument_token} "
            f"from {from_date.strftime('%Y-%m-%d')} to {to_date.strftime('%Y-%m-%d')}"
        )

        while current_start < to_date:
            current_end = min(current_start + timedelta(days=chunk_days), to_date)
            chunk = self._fetch_chunk(instrument_token, current_start, current_end, interval)
            all_records.extend(chunk)
            current_start = current_end + timedelta(days=1)

        if not all_records:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = pd.DataFrame(all_records)
        df.rename(columns={"date": "timestamp"}, inplace=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df.set_index("timestamp", inplace=True)

        # Standardize column names and types
        expected_cols = ["open", "high", "low", "close", "volume"]
        available_cols = [c for c in expected_cols if c in df.columns]
        df = df[available_cols].copy()

        df["open"] = df["open"].astype("float64")
        df["high"] = df["high"].astype("float64")
        df["low"] = df["low"].astype("float64")
        df["close"] = df["close"].astype("float64")
        df["volume"] = df["volume"].astype("int64")

        # Deduplicate index if any overlapping chunks
        df = df[~df.index.duplicated(keep="last")]
        df.sort_index(inplace=True)

        return df
