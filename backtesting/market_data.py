"""Single entry point for loading cleaned daily data into backtests."""
from typing import Dict, List, Optional, Tuple

import pandas as pd

from configs.logger import logger
from data.cleaners.corporate_actions import NON_EQUITY, prepare
from data.loaders.sync_history import load_universe
from data.store.parquet_store import get_parquet_store

BENCHMARK_SYMBOL = "NIFTYBEES"


def load_backtest_data(symbols: Optional[List[str]] = None) -> Tuple[Dict[str, pd.DataFrame], pd.DataFrame]:
    """Returns (symbol -> cleaned bars, cleaned benchmark bars).

    Without `symbols`, the universe minus ETFs and liquid/gold funds. Cleaning
    always sees the whole universe, so market-wide gap days are recognised
    even when a single symbol is requested.
    """
    store = get_parquet_store()
    universe = [a["symbol"] for a in load_universe()]
    names = set(universe) | {BENCHMARK_SYMBOL} | set(symbols or [])

    raw = {}
    for symbol in names:
        df = store.load_bars(symbol, timeframe="1D")
        if df.empty:
            logger.warning(f"No local Parquet data for {symbol}, skipping. Run 'make sync' first.")
            continue
        raw[symbol] = df
    clean = prepare(raw)

    wanted = symbols or [s for s in universe if s not in NON_EQUITY]
    data = {s: clean[s] for s in wanted if s in clean}
    benchmark = clean.get(BENCHMARK_SYMBOL, pd.DataFrame(columns=["open", "high", "low", "close", "volume"]))
    return data, benchmark
