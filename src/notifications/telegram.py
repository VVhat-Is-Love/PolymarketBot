"""
Thin Telegram notifier — fire-and-forget push messages to admin chats.
Silently disabled when TELEGRAM_BOT_TOKEN or ADMIN_TELEGRAM_ID are not set.
ADMIN_TELEGRAM_ID may contain multiple comma-separated user IDs.
No polling / webhook; suitable for one-way alerts only.

G2-9: Added 1-retry with 5s backoff + pending queue so daily summary
       is not lost on a single timeout. Failed messages are re-sent on the
       next successful call.
"""
import threading
import time
from collections import deque

import requests
from loguru import logger

_API_BASE = "https://api.telegram.org/bot{token}/sendMessage"
_RETRY_DELAY = 5   # seconds before single retry attempt
_MAX_PENDING = 20  # max queued failed messages


class TelegramNotifier:
    def __init__(self, token: str, chat_ids: str) -> None:
        self._token = (token or "").strip()
        raw = (chat_ids or "").strip()
        self._chat_ids: list[str] = [x.strip() for x in raw.split(",") if x.strip()]
        self._enabled = bool(self._token and self._chat_ids)
        self._pending: deque[str] = deque(maxlen=_MAX_PENDING)
        self._lock = threading.Lock()
        if self._enabled:
            logger.info(
                f"Telegram notifications enabled "
                f"(admins={self._chat_ids}, count={len(self._chat_ids)})"
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _post_once(self, chat_id: str, text: str) -> bool:
        """Single HTTP POST attempt. Returns True on success."""
        url = _API_BASE.format(token=self._token)
        try:
            resp = requests.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning(f"Telegram send failed (chat_id={chat_id}): {e}")
            return False

    def _send_to(self, chat_id: str, text: str) -> bool:
        """Send with one retry on failure (G2-9)."""
        if self._post_once(chat_id, text):
            return True
        # Single retry after short delay
        time.sleep(_RETRY_DELAY)
        return self._post_once(chat_id, text)

    def send(self, text: str) -> bool:
        """
        Send message to every configured admin. Returns True if at least one succeeded.
        On success: also flushes any previously queued failed messages.
        On failure: queues message so it's re-sent next time (G2-9).
        """
        if not self._enabled:
            return False

        any_ok = False
        for chat_id in self._chat_ids:
            ok = self._send_to(chat_id, text)
            if ok:
                any_ok = True

        if any_ok:
            # Flush pending queue
            with self._lock:
                while self._pending:
                    queued = self._pending.popleft()
                    for chat_id in self._chat_ids:
                        self._send_to(chat_id, f"[отложено] {queued}")
        else:
            # Queue for retry on next successful send
            with self._lock:
                self._pending.append(text)
            logger.warning(
                f"[telegram] Message queued after all retries failed "
                f"({len(self._pending)} pending)"
            )

        return any_ok


def get_notifier() -> TelegramNotifier:
    from src.config.settings import settings
    # Master kill-switch: a disabled notifier (empty token) never sends. Lets dev
    # and tests run with NOTIFICATIONS_ENABLED=false without touching prod Telegram.
    if not getattr(settings, "notifications_enabled", True):
        return TelegramNotifier("", "")
    return TelegramNotifier(settings.telegram_bot_token, settings.admin_telegram_id)
