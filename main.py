"""Main entry point for Artha Quant End-of-Day (EOD) Market Scanner.

Executes complete pipeline:
1. Authentication verification
2. Market data ingestion & atomic Parquet sync
3. Strategy indicator calculation & signal generation on closed bars
4. Dual-constraint position sizing & risk circuit breakers
5. Deterministic SQLite deduplication
6. Telegram trade alert dispatch (max 3 per session)
"""
import argparse
import sys
import time
from datetime import datetime, date, timedelta
from typing import List, Dict, Any
import pytz

from configs.config import get_settings
from configs.logger import logger
from data.loaders.kite_client import get_kite_client
from data.loaders.instrument_lookup import get_instrument_token
from data.loaders.kite_loader import KiteDataLoader
from data.loaders.sync_history import load_universe
from data.cleaners.validator import validate_ohlcv
from data.store.parquet_store import get_parquet_store
from strategies.trend_breakout import TrendBreakoutStrategy
from risk.position_sizer import calculate_position_size
from risk.circuit_breakers import PortfolioRiskGovernor
from signals.ledger import SignalLedger, generate_signal_hash
from signals.reconciler import PaperTradeReconciler
from alerts.telegram_bot import get_notifier
from alerts.formatter import format_trade_alert_card, format_eod_summary_card

IST = pytz.timezone("Asia/Kolkata")


def run_eod_scan(dry_run: bool = False, specific_symbols: List[str] = None) -> dict:
    """Executes the daily scanner pipeline."""
    start_time = time.time()
    try:
        settings = get_settings()
    except Exception as exc:
        logger.error(
            "Configuration Error: Missing or invalid .env file.\n"
            "Please copy .env.example to .env and configure your Kite and Telegram credentials."
        )
        return {"status": "CONFIG_ERROR", "error": str(exc)}

    notifier = get_notifier()
    store = get_parquet_store()
    ledger = SignalLedger()
    reconciler = PaperTradeReconciler()
    strategy = TrendBreakoutStrategy()
    risk_governor = PortfolioRiskGovernor(
        max_drawdown_limit_pct=settings.MAX_PORTFOLIO_DRAWDOWN_PCT,
        max_sector_positions=settings.MAX_SECTOR_POSITIONS,
        consecutive_losses_limit=settings.CONSECUTIVE_LOSS_LIMIT,
        loss_cooldown_days=settings.LOSS_COOLDOWN_DAYS
    )

    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    logger.info(f"Starting Artha Quant EOD Scan at {now_ist} (Dry Run: {dry_run})...")

    # 1. Authenticate with Kite (if not dry run)
    kite = None
    loader = None
    if not dry_run:
        try:
            kite = get_kite_client(verify_remote=True)
            loader = KiteDataLoader(kite)
        except Exception as exc:
            err_msg = f"Kite authentication failed during EOD scan: {exc}"
            logger.error(err_msg)
            notifier.send_health_alert(f"🚨 *EOD SCAN ABORTED*: {err_msg}")
            return {"status": "AUTH_FAILED", "error": str(exc)}

    # 2. Load Universe
    assets = load_universe()
    if specific_symbols:
        assets = [a for a in assets if a["symbol"].upper() in [s.upper() for s in specific_symbols]]

    symbols_scanned = 0
    candidate_signals: List[Dict[str, Any]] = []

    # 3. Process Each Symbol in Universe
    for asset in assets:
        symbol = asset["symbol"].upper()
        sector = asset.get("sector", "General")
        exchange = asset.get("exchange", "NSE")

        try:
            # Sync latest bar from Kite if live
            if not dry_run and kite and loader:
                token = get_instrument_token(kite, symbol, exchange)
                latest_date = store.get_latest_bar_date(symbol)
                start_fetch = (latest_date.date() + timedelta(days=1)) if latest_date else (date.today() - timedelta(days=365))
                if start_fetch <= date.today():
                    new_bars = loader.fetch_ohlcv(token, start_fetch, date.today(), interval="day")
                    if not new_bars.empty:
                        store.sync_bars(symbol, new_bars)

            # Load full history from Parquet store
            df = store.load_bars(symbol, timeframe="1D")
            if df.empty or len(df) < 50:
                logger.debug(f"Skipping {symbol}: insufficient history ({len(df)} bars).")
                continue

            symbols_scanned += 1

            # Run Strategy
            evaluated = strategy.evaluate(df)
            latest_bar = evaluated.iloc[-1]
            signal_val = int(latest_bar["signal"])
            bar_timestamp = str(evaluated.index[-1])

            if signal_val != 0:
                action = "BUY" if signal_val == 1 else "SELL"
                entry_ref = float(latest_bar["entry_ref"])
                stop_loss = float(latest_bar["stop_loss"])
                target = float(latest_bar["target"])
                rr_ratio = float(latest_bar.get("risk_reward_ratio", 2.5))

                candidate_signals.append({
                    "symbol": symbol,
                    "sector": sector,
                    "strategy": strategy.name,
                    "timeframe": "1D",
                    "bar_timestamp": bar_timestamp,
                    "action": action,
                    "entry_ref": entry_ref,
                    "stop_loss": stop_loss,
                    "target": target,
                    "risk_reward_ratio": rr_ratio,
                    "close_series": df["close"]
                })

        except Exception as exc:
            logger.warning(f"Error processing symbol {symbol}: {exc}")

    # 4. Filter, Size, and Deduplicate Candidate Signals
    emitted_count = 0
    vetoed_count = 0
    MAX_SESSION_ALERTS = 3

    active_positions = reconciler.get_open_positions()
    recent_closed = reconciler.get_closed_trades()

    for candidate in candidate_signals:
        symbol = candidate["symbol"]
        sector = candidate["sector"]
        action = candidate["action"]
        entry_ref = candidate["entry_ref"]
        stop_loss = candidate["stop_loss"]

        # Dual-Constraint Position Sizing
        sizing = calculate_position_size(
            portfolio_capital=settings.PORTFOLIO_CAPITAL,
            risk_pct=settings.RISK_PER_TRADE_PCT,
            max_position_pct=settings.MAX_POSITION_PCT,
            entry_price=entry_ref,
            stop_loss_price=stop_loss
        )

        candidate["calculated_qty"] = sizing.quantity
        candidate["capital_required"] = sizing.capital_required
        candidate["risk_budget"] = sizing.risk_amount
        candidate["capital_allocation_pct"] = sizing.capital_allocation_pct
        candidate["limiting_factor"] = sizing.limiting_factor

        # Risk Governor Audit
        risk_eval = risk_governor.evaluate_candidate(
            symbol=symbol,
            sector=sector,
            capital_required=sizing.capital_required,
            portfolio_peak_capital=settings.PORTFOLIO_CAPITAL,
            portfolio_current_capital=settings.PORTFOLIO_CAPITAL,
            available_cash=settings.PORTFOLIO_CAPITAL,
            active_positions=active_positions,
            candidate_entry_price=entry_ref,
            recent_closed_trades=recent_closed,
            eval_date=date.today()
        )

        signal_hash = generate_signal_hash(symbol, candidate["strategy"], candidate["bar_timestamp"])
        candidate["signal_hash"] = signal_hash

        if not risk_eval.approved:
            vetoed_count += 1
            veto_reason = "; ".join(risk_eval.veto_reasons)
            candidate["status"] = "VETOED"
            candidate["veto_reason"] = veto_reason
            ledger.record_if_unique(candidate)
            logger.info(f"Signal for {symbol} VETOED by Risk Governor: {veto_reason}")
            continue

        # Sessional alert cap check
        if emitted_count >= MAX_SESSION_ALERTS:
            logger.info(f"Signal for {symbol} skipped: Session cap reached ({MAX_SESSION_ALERTS} alerts).")
            continue

        # Check SQLite Idempotency Deduplication
        candidate["status"] = "PENDING"
        is_new = ledger.record_if_unique(candidate)

        if not is_new:
            logger.info(f"Duplicate signal for {symbol} already exists in SQLite ledger. Skipped alert dispatch.")
            continue

        # Dispatch Alert to Telegram
        card_markdown = format_trade_alert_card(candidate, available_cash=settings.PORTFOLIO_CAPITAL)
        sent = notifier.send_trade_card(card_markdown)
        if sent:
            ledger.mark_notified(signal_hash)
            emitted_count += 1
            logger.info(f"Dispatched actionable {action} alert card for {symbol} to Telegram.")
        else:
            logger.error(f"Failed to send Telegram alert for {symbol}.")

    elapsed = time.time() - start_time
    logger.info(
        f"EOD Scan completed in {elapsed:.2f}s. "
        f"Evaluated: {symbols_scanned}, Emitted: {emitted_count}, Vetoed: {vetoed_count}."
    )

    # 5. Dispatch Summary Heartbeat
    summary_card = format_eod_summary_card(
        symbols_scanned=symbols_scanned,
        signals_emitted=emitted_count,
        signals_vetoed=vetoed_count,
        elapsed_seconds=elapsed
    )
    notifier.send_health_alert(summary_card)

    return {
        "status": "COMPLETED",
        "symbols_scanned": symbols_scanned,
        "signals_emitted": emitted_count,
        "signals_vetoed": vetoed_count,
        "elapsed_seconds": elapsed
    }


def main():
    parser = argparse.ArgumentParser(description="Artha Quant EOD Market Scanner")
    parser.add_argument("--dry-run", action="store_true", help="Run scanner offline on local Parquet cache")
    parser.add_argument("--symbols", nargs="+", help="Specific symbols to scan")
    args = parser.parse_args()

    result = run_eod_scan(dry_run=args.dry_run, specific_symbols=args.symbols)
    if result.get("status") != "COMPLETED":
        sys.exit(1)


if __name__ == "__main__":
    main()
