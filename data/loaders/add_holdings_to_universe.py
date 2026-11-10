"""One-off utility: pulls your real equity holdings from Kite Connect and
merges any symbols not already in configs/universe.yaml, so you can sync
and backtest strategies specifically against what you actually hold.

Does NOT fetch price history itself — run `make sync` afterwards to pull
OHLCV for the newly-added symbols.

Usage:
    python -m data.loaders.add_holdings_to_universe
    python -m data.loaders.add_holdings_to_universe --dry-run
"""
import argparse
from pathlib import Path
from typing import Optional
import yaml

from configs.config import get_settings
from configs.logger import logger
from data.loaders.kite_client import get_kite_client
from data.loaders.sync_history import load_universe


def fetch_holding_symbols() -> list:
    """Returns Kite's raw holdings records (tradingsymbol, exchange, quantity, ...)."""
    kite = get_kite_client(verify_remote=True)
    holdings = kite.holdings() or []
    logger.info(f"Fetched {len(holdings)} equity holdings from Kite.")
    return holdings


def merge_holdings_into_universe(
    holdings: list,
    universe_path: Optional[Path] = None,
    dry_run: bool = False
) -> list:
    """Adds any held symbol missing from universe.yaml. Reuses an existing
    sector tag if the symbol is already known elsewhere in the file;
    otherwise tags it "Personal Holding" so the YAML schema stays valid."""
    settings = get_settings()
    path = universe_path or (settings.BASE_DIR / "configs" / "universe.yaml")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    existing_assets = data.get("assets", [])
    existing_symbols = {a["symbol"] for a in existing_assets}
    known_sectors = {a["symbol"]: a["sector"] for a in existing_assets}

    added = []
    for h in holdings:
        symbol = h.get("tradingsymbol")
        exchange = h.get("exchange", "NSE")
        qty = h.get("quantity", 0)

        if not symbol or qty <= 0 or symbol in existing_symbols:
            continue

        new_asset = {
            "symbol": symbol,
            "exchange": exchange,
            "sector": known_sectors.get(symbol, "Personal Holding")
        }
        existing_assets.append(new_asset)
        existing_symbols.add(symbol)
        added.append(new_asset)

    if added and not dry_run:
        data["assets"] = existing_assets
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        logger.info(f"Added {len(added)} held symbol(s) to {path}")
    elif added and dry_run:
        logger.info(f"[DRY RUN] Would add {len(added)} held symbol(s): {[a['symbol'] for a in added]}")
    else:
        logger.info("No new symbols to add — every held symbol is already in the universe.")

    return added


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Merge Kite holdings into configs/universe.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be added without writing the file")
    args = parser.parse_args()

    holdings = fetch_holding_symbols()
    added = merge_holdings_into_universe(holdings, dry_run=args.dry_run)

    if added:
        print(f"\n{'Would add' if args.dry_run else 'Added'} {len(added)} symbol(s):")
        for a in added:
            print(f"  {a['symbol']} ({a['exchange']}, sector={a['sector']})")
        if not args.dry_run:
            print("\nRun 'make sync' to pull price history for these before backtesting them.")
    else:
        print("No new symbols — your holdings are already fully covered by the universe.")
