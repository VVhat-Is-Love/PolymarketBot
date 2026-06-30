"""Global test safety: tests must NEVER reach the production Telegram.

A dev machine often has a real .env with the prod bot token + admin ids. Without
this guard, any test (or git hook) that exercises a code path calling
get_notifier().send(...) would message the live admin chat. get_notifier() reads
settings live on every call, so blanking the token/admin id and flipping the
kill-switch disables every notifier the code constructs — no per-test patching
needed.
"""
import pytest


@pytest.fixture(autouse=True)
def _mute_telegram_in_tests():
    from src.config.settings import settings

    saved_token = settings.telegram_bot_token
    saved_admin = settings.admin_telegram_id
    saved_enabled = getattr(settings, "notifications_enabled", True)

    settings.telegram_bot_token = ""
    settings.admin_telegram_id = ""
    settings.notifications_enabled = False
    try:
        yield
    finally:
        settings.telegram_bot_token = saved_token
        settings.admin_telegram_id = saved_admin
        settings.notifications_enabled = saved_enabled
