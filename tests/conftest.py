"""Pytest configuration and global fixtures."""
import os
import pytest
from configs.config import reset_settings


@pytest.fixture(autouse=True)
def set_test_environment(monkeypatch):
    """Ensure all tests have valid mock environment variables by default."""
    monkeypatch.setenv("KITE_API_KEY", "mock_kite_api_key_123")
    monkeypatch.setenv("KITE_API_SECRET", "mock_kite_api_secret_456")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:mock_telegram_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123456789")
    monkeypatch.setenv("TELEGRAM_HEALTH_CHAT_ID", "-100123456789")
    monkeypatch.setenv("PORTFOLIO_CAPITAL", "100000.0")

    reset_settings()
    yield
    reset_settings()
