"""Instrument token resolver and cache manager for Kite Connect."""
import json
from datetime import date
from pathlib import Path
from typing import Dict, Optional
from kiteconnect import KiteConnect

from configs.config import get_settings
from configs.logger import logger


def get_instruments_cache_path(exchange: str) -> Path:
    settings = get_settings()
    settings.DATA_STORE_DIR.mkdir(parents=True, exist_ok=True)
    return settings.DATA_STORE_DIR / f"instruments_{exchange.upper()}.json"


def fetch_and_cache_instruments(kite: KiteConnect, exchange: str = "NSE") -> Dict[str, int]:
    """
    Downloads active instruments from Kite for the given exchange,
    caches to disk, and returns symbol -> instrument_token map.
    """
    cache_path = get_instruments_cache_path(exchange)
    today_str = date.today().isoformat()

    # Check existing valid cache
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("cache_date") == today_str and "mapping" in data:
                    logger.debug(f"Loaded {exchange} instrument mapping from cache ({len(data['mapping'])} symbols)")
                    return data["mapping"]
        except Exception as exc:
            logger.warning(f"Failed to read instrument cache: {exc}")

    # Fetch from Kite API
    logger.info(f"Fetching active {exchange} instruments from Kite API...")
    instruments = kite.instruments(exchange)
    mapping: Dict[str, int] = {}

    for inst in instruments:
        symbol = inst.get("tradingsymbol")
        token = inst.get("instrument_token")
        if symbol and token:
            mapping[symbol] = int(token)

    # Save cache
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({
            "cache_date": today_str,
            "exchange": exchange,
            "total_count": len(mapping),
            "mapping": mapping
        }, f)

    logger.info(f"Cached {len(mapping)} {exchange} instruments to {cache_path}")
    return mapping


def get_instrument_token(kite: KiteConnect, symbol: str, exchange: str = "NSE") -> int:
    """Resolve symbol to its numeric instrument token."""
    mapping = fetch_and_cache_instruments(kite, exchange)
    token = mapping.get(symbol.upper())
    if not token:
        raise KeyError(f"Symbol '{symbol}' not found in active {exchange} instruments.")
    return token
