"""
Thin Telegram notifier — fire-and-forget push messages to admin chats.
Silently disabled when TELEGRAM_BOT_TOKEN or ADMIN_TELEGRAM_ID are not set.
ADMIN_TELEGRAM_ID may contain multiple comma-separated user IDs.
No polling / webhook; suitable for one-way alerts only.
"""
import requests
from loguru import logger

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, token: str, chat_ids: str) -> None:
        self._token = (token or "").strip()
        # Accept comma-separated IDs: "516577448,321141407"
        raw = (chat_ids or "").strip()
        self._chat_ids: list[str] = [x.strip() for x in raw.split(",") if x.strip()]
        self._enabled = bool(self._token and self._chat_ids)
        if self._enabled:
            logger.info(
                f"Telegram notifications enabled "
                f"(admins={self._chat_ids}, count={len(self._chat_ids)})"
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send(self, text: str) -> bool:
        """Send message to every configured admin. Returns True if at least one succeeded."""
        if not self._enabled:
            return False
        url = _API_BASE.format(token=self._token)
        any_ok = False
        for chat_id in self._chat_ids:
            try:
                resp = requests.post(
                    url,
                    json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                    timeout=10,
                )
                resp.raise_for_status()
                any_ok = True
            except Exception as e:
                logger.warning(f"Telegram send failed (chat_id={chat_id}): {e}")
        return any_ok


def get_notifier() -> TelegramNotifier:
    from src.config.settings import settings
    return TelegramNotifier(settings.telegram_bot_token, settings.admin_telegram_id)
