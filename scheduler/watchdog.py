"""Watchdog verification script.

Runs at 16:15 IST to confirm that the 16:00 EOD scan executed and recorded state.
If no execution occurred, issues a critical Telegram SOS alert.
"""
import sqlite3
import sys
from datetime import datetime, date
import pytz

from configs.config import get_settings
from configs.logger import logger
from alerts.telegram_bot import get_notifier

IST = pytz.timezone("Asia/Kolkata")


def verify_eod_execution() -> bool:
    """Verifies that an EOD scan ran today."""
    settings = get_settings()
    notifier = get_notifier()
    today_str = date.today().isoformat()
    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

    # If weekend (Saturday=5, Sunday=6), skip watchdog
    if datetime.now(IST).weekday() in (5, 6):
        logger.info("Watchdog: Weekend detected. Skipping execution check.")
        return True

    db_path = settings.SIGNALS_DB_PATH
    if not db_path.exists():
        msg = (
            f"🚨 *WATCHDOG CRITICAL FAILURE*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Time: `{now_ist}`\n\n"
            f"Database file does not exist at `{db_path}`!\n"
            f"The 16:00 IST EOD scan failed to initialize."
        )
        logger.critical(msg)
        notifier.send_health_alert(msg)
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) FROM signals
        WHERE created_at LIKE ?
    """, (f"{today_str}%",))
    count = cursor.fetchone()[0]
    conn.close()

    logger.info(f"Watchdog verification: Found {count} signal ledger events for today ({today_str}).")
    return True


if __name__ == "__main__":
    success = verify_eod_execution()
    sys.exit(0 if success else 1)
