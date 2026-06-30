"""Verify the test-session Telegram mute (conftest) and the kill-switch."""
from src.notifications.telegram import get_notifier
from src.config.settings import settings


def test_notifier_is_disabled_during_tests():
    # conftest autouse fixture blanks token/admin + flips the kill-switch.
    assert settings.notifications_enabled is False
    n = get_notifier()
    assert n.enabled is False
    # send() on a disabled notifier is a no-op that returns False (never POSTs).
    assert n.send("should never reach prod") is False


def test_kill_switch_disables_even_with_token():
    # Even if a token/admin were present, NOTIFICATIONS_ENABLED=false wins.
    settings.telegram_bot_token = "fake-token"
    settings.admin_telegram_id = "123"
    settings.notifications_enabled = False
    try:
        assert get_notifier().enabled is False
    finally:
        settings.telegram_bot_token = ""
        settings.admin_telegram_id = ""
