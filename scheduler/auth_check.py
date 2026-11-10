"""Morning authentication health check.

Scheduled at 08:30 IST to guarantee Kite Connect session is valid
long before the market closes and the 16:00 EOD scan runs.
"""
import sys
from datetime import datetime
import pytz

from configs.logger import logger
from data.loaders.kite_client import get_kite_client
from data.loaders.exceptions import KiteSessionExpiredError
from alerts.telegram_bot import get_notifier

IST = pytz.timezone("Asia/Kolkata")


def check_auth_health() -> bool:
    """
    Checks session file and tests remote Kite profile.
    Sends Telegram SOS if invalid.
    """
    now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    notifier = get_notifier()

    try:
        logger.info(f"Running morning Kite Connect auth check at {now_ist}...")
        kite = get_kite_client(verify_remote=True, force_reload=True)
        profile = kite.profile()
        user_name = profile.get("user_name", "N/A")
        user_id = profile.get("user_id", "N/A")

        logger.info(f"Auth check PASSED for {user_name} ({user_id})")
        return True

    except KiteSessionExpiredError as exc:
        msg = (
            f"🚨 *ARTHA QUANT — AUTH ALERT*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Time: `{now_ist}`\n\n"
            f"⚠️ *Kite Connect session is EXPIRED or MISSING!*\n"
            f"Reason: `{str(exc)}`\n\n"
            f"👉 *Action Required:*\n"
            f"Run token refresh before 16:00 IST:\n"
            f"`python -m scheduler.token_refresh`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        logger.error(f"Auth check failed: {exc}")
        notifier.send_health_alert(msg)
        return False

    except Exception as exc:
        msg = (
            f"🚨 *ARTHA QUANT — AUTH CHECK FAILURE*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Time: `{now_ist}`\n\n"
            f"Unexpected error validating Kite session:\n"
            f"`{str(exc)}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        logger.exception("Unexpected error in auth check")
        notifier.send_health_alert(msg)
        return False


if __name__ == "__main__":
    success = check_auth_health()
    sys.exit(0 if success else 1)
