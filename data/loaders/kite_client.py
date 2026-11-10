"""KiteConnect client factory and session manager."""
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
import pytz
from kiteconnect import KiteConnect

from configs.config import get_settings
from configs.logger import logger
from data.loaders.exceptions import KiteSessionExpiredError

IST = pytz.timezone("Asia/Kolkata")


_kite_instance: Optional[KiteConnect] = None


def load_session(session_path: Path) -> dict:
    """Load and validate session json structure."""
    if not session_path.exists():
        raise KiteSessionExpiredError(
            f"Kite session file missing at {session_path}. "
            "Please run: python -m scheduler.token_refresh"
        )

    try:
        with open(session_path, "r", encoding="utf-8") as f:
            session_data = json.load(f)
    except Exception as exc:
        raise KiteSessionExpiredError(f"Corrupted session file: {exc}")

    if "access_token" not in session_data:
        raise KiteSessionExpiredError("Session file missing 'access_token'.")

    # Check exact expiration timestamp against IST clock
    now_ist = datetime.now(IST)
    expires_at_str = session_data.get("expires_at")

    if not expires_at_str and "saved_at" in session_data:
        saved_dt = datetime.fromisoformat(session_data["saved_at"])
        if saved_dt.tzinfo is None:
            saved_dt = IST.localize(saved_dt)
        today_6am = saved_dt.replace(hour=6, minute=0, second=0, microsecond=0)
        derived_expiry = today_6am if saved_dt < today_6am else (today_6am + timedelta(days=1))
        expires_at_str = derived_expiry.isoformat()

    if expires_at_str:
        expires_at = datetime.fromisoformat(expires_at_str)
        if expires_at.tzinfo is None:
            expires_at = IST.localize(expires_at)
        if now_ist >= expires_at:
            raise KiteSessionExpiredError(
                f"Session expired at {expires_at.strftime('%Y-%m-%d %H:%M:%S IST')}. "
                "Kite tokens expire daily at 06:00 AM IST. Please run: python -m scheduler.token_refresh"
            )
    else:
        # Fallback to date check
        saved_expiry = session_data.get("expiry_date")
        today_str = date.today().isoformat()
        if saved_expiry != today_str:
            raise KiteSessionExpiredError(
                f"Session expired! Session date: {saved_expiry}, Today: {today_str}. "
                "Kite tokens expire daily at 06:00 IST. Please run: python -m scheduler.token_refresh"
            )

    return session_data


def get_kite_client(verify_remote: bool = False, force_reload: bool = False) -> KiteConnect:
    """
    Returns an authenticated KiteConnect client.
    
    Args:
        verify_remote: If True, executes kite.profile() to verify API connectivity.
        force_reload: If True, re-reads session file from disk.
    """
    global _kite_instance

    if _kite_instance is not None and not force_reload:
        return _kite_instance

    settings = get_settings()
    session_data = load_session(settings.SESSION_FILE_PATH)

    kite = KiteConnect(api_key=settings.KITE_API_KEY)
    kite.set_access_token(session_data["access_token"])

    if verify_remote:
        try:
            profile = kite.profile()
            logger.info(f"Connected to Kite Connect as {profile.get('user_name')} ({profile.get('user_id')})")
        except Exception as exc:
            _kite_instance = None
            raise KiteSessionExpiredError(f"Remote token verification failed: {exc}")

    _kite_instance = kite
    return _kite_instance


def reset_kite_client() -> None:
    """Reset cached Kite client instance."""
    global _kite_instance
    _kite_instance = None
