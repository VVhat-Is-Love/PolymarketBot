"""
Thin Telegram notifier — fire-and-forget push messages to the admin chat.
Silently disabled when TELEGRAM_BOT_TOKEN or ADMIN_TELEGRAM_ID are not set.
No polling / webhook; suitable for one-way alerts only.
"""
import requests
from loguru import logger

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self._token = (token or "").strip()
        self._chat_id = (chat_id or "").strip()
        self._enabled = bool(self._token and self._chat_id)
        if self._enabled:
            logger.info(f"Telegram notifications enabled (chat_id={self._chat_id})")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send(self, text: str) -> bool:
        if not self._enabled:
            return False
        try:
            url = _API_BASE.format(token=self._token)
            resp = requests.post(
                url,
                json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")
            return False


def get_notifier() -> TelegramNotifier:
    from src.config.settings import settings
    return TelegramNotifier(settings.telegram_bot_token, settings.admin_telegram_id)
