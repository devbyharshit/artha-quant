"""Test application configuration and validation."""
import pytest
from pydantic import ValidationError
from configs.config import Settings


def test_settings_fails_without_required_keys(monkeypatch):
    """Verify that settings raise ValidationError if required keys are missing."""
    monkeypatch.delenv("KITE_API_KEY", raising=False)
    monkeypatch.delenv("KITE_API_SECRET", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_succeeds_with_valid_keys(monkeypatch):
    """Verify that settings load and apply correct defaults when valid keys are provided."""
    monkeypatch.setenv("KITE_API_KEY", "test_key")
    monkeypatch.setenv("KITE_API_SECRET", "test_secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123456")
    monkeypatch.delenv("TELEGRAM_HEALTH_CHAT_ID", raising=False)

    s = Settings(_env_file=None)
    assert s.KITE_API_KEY == "test_key"
    assert s.KITE_API_SECRET == "test_secret"
    assert s.PORTFOLIO_CAPITAL == 100000.0
    assert s.RISK_PER_TRADE_PCT == 1.0
    assert s.MAX_POSITION_PCT == 10.0
    # Fallback for health chat ID should default to main chat ID if not set
    assert s.TELEGRAM_HEALTH_CHAT_ID == "-100123456"


def test_settings_risk_validation_bounds(monkeypatch):
    """Verify that unrealistic risk parameters fail validation."""
    monkeypatch.setenv("KITE_API_KEY", "test_key")
    monkeypatch.setenv("KITE_API_SECRET", "test_secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123456")
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "10.0")  # Exceeds max 5.0%

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
