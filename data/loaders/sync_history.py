"""CLI utility to sync historical data for all symbols in the universe."""
import sys
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional
import yaml
import pytz

from configs.config import get_settings
from configs.logger import logger
from data.loaders.kite_client import get_kite_client
from data.loaders.instrument_lookup import get_instrument_token
from data.loaders.kite_loader import KiteDataLoader
from data.cleaners.validator import validate_ohlcv
from data.store.parquet_store import get_parquet_store

IST = pytz.timezone("Asia/Kolkata")


def load_universe(universe_path: Optional[Path] = None) -> list:
    """Load universe YAML specification."""
    settings = get_settings()
    path = universe_path or (settings.BASE_DIR / "configs" / "universe.yaml")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("assets", [])


def sync_universe_history(
    days_back: int = 1825,  # 5 years by default
    timeframe: str = "1D",
    universe_path: Optional[Path] = None,
    backfill: bool = False
) -> dict:
    """
    Downloads and updates Parquet data for all symbols in the universe.

    With backfill=True, symbols that already have data also get the missing
    older bars, back to `days_back` days ago (or listing, if later).

    Returns:
        Summary dict of processed symbols and bar counts.
    """
    logger.info("Starting historical data sync for universe...")
    kite = get_kite_client(verify_remote=True)
    loader = KiteDataLoader(kite)
    store = get_parquet_store()
    assets = load_universe(universe_path)

    today = date.today()
    results = {}

    for asset in assets:
        symbol = asset["symbol"]
        exchange = asset.get("exchange", "NSE")

        try:
            token = get_instrument_token(kite, symbol, exchange)
            latest_date = store.get_latest_bar_date(symbol, timeframe=timeframe)

            if backfill and latest_date is not None:
                earliest = store.get_earliest_bar_date(symbol, timeframe=timeframe).date()
                backfill_start = today - timedelta(days=days_back)
                if backfill_start < earliest:
                    logger.info(f"Backfilling {symbol} from {backfill_start} to {earliest - timedelta(days=1)}...")
                    old_df = loader.fetch_ohlcv(
                        instrument_token=token,
                        from_date=backfill_start,
                        to_date=earliest - timedelta(days=1),
                        interval="day"
                    )
                    if not old_df.empty:
                        report = validate_ohlcv(old_df, symbol=symbol)
                        if not report.is_valid:
                            logger.warning(f"Data quality warning for {symbol} backfill: {report.errors}")
                        store.sync_bars(symbol, old_df, timeframe=timeframe)
                    results.setdefault(symbol, {})["backfilled_bars"] = len(old_df)

            if latest_date is None:
                start_date = today - timedelta(days=days_back)
                logger.info(f"Syncing full history for {symbol} ({days_back} days)...")
            else:
                start_date = latest_date.date() + timedelta(days=1)
                if start_date > today:
                    logger.info(f"Symbol {symbol} is already up to date ({latest_date.date()}).")
                    results.setdefault(symbol, {}).update({"status": "UP_TO_DATE", "bars_added": 0})
                    continue
                logger.info(f"Syncing incremental bars for {symbol} from {start_date} to {today}...")

            new_df = loader.fetch_ohlcv(
                instrument_token=token,
                from_date=start_date,
                to_date=today,
                interval="day"
            )

            if not new_df.empty:
                # Validate bars
                report = validate_ohlcv(new_df, symbol=symbol)
                if not report.is_valid:
                    logger.warning(f"Data quality warning for {symbol}: {report.errors}")

                # Save and merge
                consolidated = store.sync_bars(symbol, new_df, timeframe=timeframe)
                logger.info(f"Updated {symbol}: added {len(new_df)} bars (total {len(consolidated)} bars).")
                results.setdefault(symbol, {}).update({"status": "SUCCESS", "bars_added": len(new_df), "total_bars": len(consolidated)})
            else:
                logger.info(f"No new bars returned for {symbol}.")
                results.setdefault(symbol, {}).update({"status": "NO_NEW_DATA", "bars_added": 0})

        except Exception as exc:
            logger.error(f"Failed to sync {symbol}: {exc}")
            results[symbol] = {"status": "FAILED", "error": str(exc)}

    logger.info("Historical data sync completed.")
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Sync daily history for the universe")
    parser.add_argument("--years", type=float, default=5.0)
    parser.add_argument("--backfill", action="store_true", help="Also fetch missing older history for symbols that already have data")
    args = parser.parse_args()
    try:
        results = sync_universe_history(days_back=int(args.years * 365.25), backfill=args.backfill)
        failed = {s: r for s, r in results.items() if r.get("status") == "FAILED"}
        print(f"Synced {len(results)} symbols, {len(failed)} failed.")
        for s, r in failed.items():
            print(f"  FAILED {s}: {r.get('error')}")
    except Exception as exc:
        logger.exception("Fatal crash during historical sync")
        sys.exit(1)
