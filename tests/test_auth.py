"""Test suite for Kite authentication and session handling."""
import json
from datetime import datetime, date, timedelta
from pathlib import Path
import pytest

from data.loaders.exceptions import KiteSessionExpiredError
from data.loaders.kite_client import load_session, get_kite_client, reset_kite_client
from scheduler.auth_check import check_auth_health
from scheduler.token_refresh import save_session


@pytest.fixture(autouse=True)
def clean_kite_state():
    reset_kite_client()
    yield
    reset_kite_client()


def test_save_session_handles_datetime_objects(tmp_path):
    """Verify save_session serializes native datetime objects without error."""
    session_file = tmp_path / "kite_session.json"
    session_data = {
        "user_id": "MOCK_USER_ID",
        "access_token": "mock_token_abc",
        "login_time": datetime.now(),
        "public_token": "mock_public_token"
    }
    save_session(session_data, str(session_file))

    assert session_file.exists()
    loaded = load_session(session_file)
    assert loaded["user_id"] == "MOCK_USER_ID"
    assert loaded["access_token"] == "mock_token_abc"
    assert "login_time" in loaded


def test_load_session_missing_file(tmp_path):
    """Verify error raised when session file does not exist."""
    missing_file = tmp_path / "kite_session.json"
    with pytest.raises(KiteSessionExpiredError) as exc:
        load_session(missing_file)
    assert "missing" in str(exc.value).lower()


def test_load_session_expired_date(tmp_path):
    """Verify error raised when session is from a previous date."""
    session_file = tmp_path / "kite_session.json"
    yesterday_str = (date.today() - timedelta(days=1)).isoformat()
    session_data = {
        "access_token": "mock_token_123",
        "expiry_date": yesterday_str
    }
    with open(session_file, "w") as f:
        json.dump(session_data, f)

    with pytest.raises(KiteSessionExpiredError) as exc:
        load_session(session_file)
    assert "session expired" in str(exc.value).lower()


def test_load_session_valid(tmp_path):
    """Verify successful session load when dated today."""
    session_file = tmp_path / "kite_session.json"
    today_str = date.today().isoformat()
    session_data = {
        "access_token": "valid_token_abc",
        "expiry_date": today_str
    }
    with open(session_file, "w") as f:
        json.dump(session_data, f)

    loaded = load_session(session_file)
    assert loaded["access_token"] == "valid_token_abc"
    assert loaded["expiry_date"] == today_str


def test_auth_check_sends_telegram_alert_on_expired_session(tmp_path, monkeypatch, mocker):
    """Verify check_auth_health notifies Telegram on expired session."""
    monkeypatch.setenv("KITE_API_KEY", "test_key")
    monkeypatch.setenv("KITE_API_SECRET", "test_secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")

    # Mock load_session to raise expired error
    mocker.patch(
        "data.loaders.kite_client.load_session",
        side_effect=KiteSessionExpiredError("Kite session file missing at /path/kite_session.json")
    )

    mock_send = mocker.patch("alerts.telegram_bot.TelegramNotifier.send_health_alert", return_value=True)

    success = check_auth_health()
    assert success is False
    assert mock_send.called
    assert "EXPIRED or MISSING" in mock_send.call_args[0][0]
