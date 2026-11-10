"""Interactive Kite Connect token exchange service.

Supports:
1. Automated local HTTP redirect listener (intercepts ?request_token=XYZ).
2. Manual CLI fallback for headless or remote environments.
"""
import json
import os
import platform
import subprocess
import sys
import webbrowser
from datetime import datetime, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from typing import Optional, Tuple
import pytz

from kiteconnect import KiteConnect
from configs.config import get_settings
from configs.logger import logger

IST = pytz.timezone("Asia/Kolkata")


def calculate_token_expiry(now_dt: datetime) -> datetime:
    """
    Zerodha tokens expire daily at 06:00 AM IST.
    If logged in after 06:00 AM, expires tomorrow at 06:00 AM.
    If logged in before 06:00 AM, expires today at 06:00 AM.
    """
    today_6am = now_dt.replace(hour=6, minute=0, second=0, microsecond=0)
    if now_dt < today_6am:
        return today_6am
    else:
        return today_6am + timedelta(days=1)


def open_in_chrome(url: str) -> None:
    """Attempts to open URL specifically in Google Chrome, with fallback to default browser."""
    # 1. macOS: launch Google Chrome directly via open -a
    if platform.system() == "Darwin":
        chrome_app = Path("/Applications/Google Chrome.app")
        if chrome_app.exists():
            try:
                subprocess.run(["open", "-a", "Google Chrome", url], check=True)
                logger.info("Successfully opened login page in Google Chrome.")
                return
            except Exception as exc:
                logger.warning(f"Could not open Google Chrome via macOS 'open': {exc}")

    # 2. Try webbrowser registered chrome controllers
    for browser_name in ("google-chrome", "chrome"):
        try:
            browser = webbrowser.get(browser_name)
            browser.open(url)
            logger.info(f"Opened login page using {browser_name}.")
            return
        except Exception:
            continue

    # 3. Fallback to system default browser
    try:
        webbrowser.open(url)
        logger.info("Opened login page in system default browser.")
    except Exception as exc:
        logger.warning(f"Failed to auto-open browser: {exc}")


class TokenCaptureHandler(BaseHTTPRequestHandler):
    """HTTP handler capturing the request_token redirect from Zerodha."""
    captured_token: Optional[str] = None

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "request_token" in params:
            TokenCaptureHandler.captured_token = params["request_token"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"""
                <html>
                <body style="font-family: system-ui, sans-serif; text-align: center; padding-top: 50px;">
                    <h2 style="color: #10b981;">Authentication Successful</h2>
                    <p>Request token captured. You can close this tab and return to the terminal.</p>
                </body>
                </html>
            """)
        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<h3>Error: No request_token found in URL.</h3>")

    def log_message(self, format, *args):
        # Silence default HTTP server console logging
        return


def capture_token_via_server(port: int = 8080, timeout_seconds: int = 180) -> Optional[str]:
    """Start local server and wait for Zerodha redirect callback."""
    server_address = ("127.0.0.1", port)
    httpd = HTTPServer(server_address, TokenCaptureHandler)
    httpd.timeout = timeout_seconds

    logger.info(f"Local auth receiver listening on http://127.0.0.1:{port}")
    httpd.handle_request()  # Wait for a single GET request
    return TokenCaptureHandler.captured_token


def save_session(session_data: dict, session_path: str) -> None:
    """Save session JSON securely."""
    now_ist = datetime.now(IST)
    expiry_dt = calculate_token_expiry(now_ist)

    # Add date & exact expiration metadata
    session_data["saved_at"] = now_ist.isoformat()
    session_data["expires_at"] = expiry_dt.isoformat()
    session_data["expiry_date"] = expiry_dt.date().isoformat()

    temp_path = f"{session_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2, default=str)

    # Restrict permissions (0600 - user read/write only)
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, session_path)
    logger.info(f"Kite session saved securely to {session_path}")


def refresh_token(manual_token: Optional[str] = None) -> dict:
    """Execute token refresh flow."""
    settings = get_settings()
    kite = KiteConnect(api_key=settings.KITE_API_KEY)
    login_url = kite.login_url()

    request_token = manual_token

    if not request_token:
        logger.info(f"Opening browser for Zerodha login: {login_url}")
        print("\n" + "=" * 70)
        print("ZERODHA AUTHENTICATION")
        print("=" * 70)
        print(f"Login URL:\n{login_url}\n")
        print("Waiting for redirect callback at http://127.0.0.1:8080 ...")
        print("If redirect does not work, you can copy 'request_token' from the URL bar.")
        print("=" * 70 + "\n")

        open_in_chrome(login_url)

        # Attempt to capture via local server
        request_token = capture_token_via_server(port=8080)

        # Fallback to manual entry if server timed out or failed
        if not request_token:
            request_token = input("\nEnter request_token (or full redirected URL) manually: ").strip()
            if "request_token=" in request_token:
                parsed = urlparse(request_token)
                request_token = parse_qs(parsed.query).get("request_token", [None])[0]

    if not request_token:
        raise ValueError("No request_token obtained. Authentication aborted.")

    logger.info("Exchanging request_token for access_token with Kite API...")
    data = kite.generate_session(request_token, api_secret=settings.KITE_API_SECRET)

    # Verify session with a lightweight profile ping
    kite.set_access_token(data["access_token"])
    profile = kite.profile()
    logger.info(f"Authentication verified for user: {profile.get('user_name', 'N/A')} ({profile.get('user_id')})")

    # Save session
    save_session(data, str(settings.SESSION_FILE_PATH))
    return data


if __name__ == "__main__":
    try:
        token_arg = sys.argv[1] if len(sys.argv) > 1 else None
        refresh_token(manual_token=token_arg)
        print("\nSession refresh complete. Ready for market scans.\n")
    except Exception as exc:
        logger.error(f"Token refresh failed: {exc}")
        sys.exit(1)
