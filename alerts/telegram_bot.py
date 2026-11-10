"""Telegram notification service for trade alerts and system health."""
import time
from typing import Optional
import requests

from configs.config import get_settings
from configs.logger import logger


class TelegramNotifier:
    """Dispatches notifications and trade cards to Telegram channels."""

    def __init__(self, bot_token: Optional[str] = None, default_chat_id: Optional[str] = None):
        settings = get_settings()
        self.bot_token = bot_token or settings.TELEGRAM_BOT_TOKEN
        self.default_chat_id = default_chat_id or settings.TELEGRAM_CHAT_ID
        health_id = settings.TELEGRAM_HEALTH_CHAT_ID
        if health_id and not str(health_id).startswith("your_"):
            self.health_chat_id = str(health_id)
        else:
            self.health_chat_id = self.default_chat_id
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

    def send_message(
        self,
        text: str,
        chat_id: Optional[str] = None,
        parse_mode: str = "Markdown",
        disable_web_page_preview: bool = True,
        max_retries: int = 3
    ) -> bool:
        """
        Send text message to Telegram with retry backoff.
        
        Returns:
            True if sent successfully, False otherwise.
        """
        target_chat = chat_id or self.default_chat_id
        endpoint = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": target_chat,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview
        }

        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(endpoint, json=payload, timeout=10)
                if response.status_code == 200:
                    logger.debug(f"Telegram message sent to {target_chat}")
                    return True
                else:
                    logger.warning(
                        f"Telegram API error (attempt {attempt}/{max_retries}): "
                        f"Status {response.status_code} - {response.text}"
                    )
            except Exception as exc:
                logger.warning(f"Telegram network failure (attempt {attempt}/{max_retries}): {exc}")

            if attempt < max_retries:
                time.sleep(2 ** attempt)  # 2s, 4s, etc.

        logger.error(f"Failed to send Telegram message after {max_retries} attempts.")
        return False

    def send_health_alert(self, message: str) -> bool:
        """Send message to dedicated health / alerts channel."""
        return self.send_message(text=message, chat_id=self.health_chat_id)

    def send_trade_card(self, card_markdown: str) -> bool:
        """Send trade alert card to primary alerts channel."""
        return self.send_message(text=card_markdown, chat_id=self.default_chat_id)


# Module-level helper instance
_notifier: Optional[TelegramNotifier] = None


def get_notifier() -> TelegramNotifier:
    """Return cached notifier instance."""
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier
