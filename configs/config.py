"""Application configuration schema using Pydantic Settings."""
from pathlib import Path
from typing import Optional
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Strongly typed application configuration."""
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Kite Connect
    KITE_API_KEY: str = Field(..., description="Zerodha Kite Connect Developer API Key")
    KITE_API_SECRET: str = Field(..., description="Zerodha Kite Connect Developer API Secret")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = Field(..., description="Telegram Bot API Token from @BotFather")
    TELEGRAM_CHAT_ID: str = Field(..., description="Target Telegram chat/channel ID for trade alerts")
    TELEGRAM_HEALTH_CHAT_ID: Optional[str] = Field(
        default=None,
        description="Target Telegram chat/channel ID for system health and exception notifications"
    )

    # Risk Engine Defaults
    PORTFOLIO_CAPITAL: float = Field(default=100000.0, gt=0, description="Total trading capital in INR")
    RISK_PER_TRADE_PCT: float = Field(default=1.0, gt=0, le=5.0, description="Risk budget per trade (e.g. 1.0%)")
    MAX_POSITION_PCT: float = Field(default=10.0, gt=0, le=25.0, description="Maximum allocation per position (e.g. 10.0%)")
    MAX_PORTFOLIO_DRAWDOWN_PCT: float = Field(default=15.0, gt=0, description="Portfolio drawdown halt threshold")
    MAX_SECTOR_POSITIONS: int = Field(default=3, ge=1, description="Max open positions per sector")
    CONSECUTIVE_LOSS_LIMIT: int = Field(default=3, ge=1, description="Max consecutive losses before cooldown pause")
    LOSS_COOLDOWN_DAYS: int = Field(default=3, ge=1, description="Trading days off following consecutive loss limit hit")

    # Environment & Logging
    APP_ENV: str = Field(default="development", description="Environment: development, paper, production")
    LOG_LEVEL: str = Field(default="INFO", description="Log level: DEBUG, INFO, WARNING, ERROR")

    # Paths
    BASE_DIR: Path = BASE_DIR
    DATA_STORE_DIR: Path = BASE_DIR / "data" / "store"
    LOGS_DIR: Path = BASE_DIR / "logs"
    SIGNALS_DB_PATH: Path = BASE_DIR / "signals" / "signals.db"
    SESSION_FILE_PATH: Path = BASE_DIR / "kite_session.json"

    @field_validator("TELEGRAM_HEALTH_CHAT_ID", mode="before")
    @classmethod
    def fallback_health_chat(cls, v, values):
        if not v and "TELEGRAM_CHAT_ID" in values.data:
            return values.data["TELEGRAM_CHAT_ID"]
        return v


_settings_instance: Optional[Settings] = None


def get_settings() -> Settings:
    """Instantiate and return settings (cached singleton)."""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = Settings()
    return _settings_instance


def reset_settings() -> None:
    """Reset cached settings instance (primarily for testing)."""
    global _settings_instance
    _settings_instance = None
