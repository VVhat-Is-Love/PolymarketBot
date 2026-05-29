"""Tests for /mode command: state transitions and response text."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update_context(args=None, user_id=99999):
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args if args is not None else []
    return update, context


def _make_query_context(data, user_id=99999):
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.from_user.id = user_id
    update = MagicMock()
    update.effective_user.id = user_id
    update.callback_query = query
    context = MagicMock()
    return update, context, query


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# _apply_mode_switch: in-process settings update
# ---------------------------------------------------------------------------

class TestApplyModeSwitch:
    def _patched_switch(self, mode: str) -> None:
        with patch("src.db.session.get_session") as mock_gs:
            session = MagicMock()
            session.get.return_value = MagicMock()
            mock_gs.return_value = session

            from src.telegram.bot import _apply_mode_switch
            _apply_mode_switch(mode)

    def test_live_updates_settings(self):
        from src.config.settings import settings
        original = settings.trading_mode
        try:
            self._patched_switch("live")
            assert settings.trading_mode == "live"
        finally:
            settings.trading_mode = original

    def test_paper_updates_settings(self):
        from src.config.settings import settings
        original = settings.trading_mode
        try:
            self._patched_switch("paper")
            assert settings.trading_mode == "paper"
        finally:
            settings.trading_mode = original


# ---------------------------------------------------------------------------
# cmd_mode: /mode paper → switches to paper, response mentions PAPER
# ---------------------------------------------------------------------------

class TestCmdModePaper:
    def test_paper_arg_switches_and_responds_paper(self):
        """Start in live mode, switch to paper → _apply_mode_switch('paper'), response has PAPER."""
        update, context = _make_update_context(args=["paper"])
        mock_s = MagicMock()
        mock_s.trading_mode = "live"  # start in live so switch is not a no-op

        def fake_switch(mode):
            mock_s.trading_mode = mode  # simulate in-process settings update

        with patch("src.telegram.bot._get_admin_ids", return_value={99999}), \
             patch("src.telegram.bot._apply_mode_switch", side_effect=fake_switch) as mock_switch, \
             patch("src.config.settings.settings", mock_s):
            from src.telegram.bot import cmd_mode
            _run(cmd_mode(update, context))

        mock_switch.assert_called_once_with("paper")
        text = update.message.reply_text.call_args[0][0]
        assert "PAPER" in text.upper()

    def test_paper_does_not_prompt_confirmation(self):
        update, context = _make_update_context(args=["paper"])
        mock_s = MagicMock()
        mock_s.trading_mode = "live"

        with patch("src.telegram.bot._get_admin_ids", return_value={99999}), \
             patch("src.telegram.bot._apply_mode_switch"), \
             patch("src.config.settings.settings", mock_s):
            from src.telegram.bot import cmd_mode
            _run(cmd_mode(update, context))

        # Direct reply (no InlineKeyboard confirmation step)
        update.message.reply_text.assert_called_once()


# ---------------------------------------------------------------------------
# cmd_mode: /mode live → shows confirmation keyboard (no immediate switch)
# ---------------------------------------------------------------------------

class TestCmdModeLive:
    def test_live_arg_shows_confirmation_not_switch(self):
        """Start in paper mode, request live → confirmation keyboard, no immediate switch."""
        update, context = _make_update_context(args=["live"])
        mock_s = MagicMock()
        mock_s.trading_mode = "paper"  # start in paper so switch is not a no-op

        with patch("src.telegram.bot._get_admin_ids", return_value={99999}), \
             patch("src.telegram.bot._apply_mode_switch") as mock_switch, \
             patch("src.config.settings.settings", mock_s):
            from src.telegram.bot import cmd_mode
            _run(cmd_mode(update, context))

        # Switch must NOT happen until confirmation
        mock_switch.assert_not_called()
        # Confirmation keyboard should be shown
        update.message.reply_text.assert_called_once()
        kwargs = update.message.reply_text.call_args[1]
        assert "reply_markup" in kwargs


# ---------------------------------------------------------------------------
# cmd_mode: /mode (no args) → shows current mode, no switch
# ---------------------------------------------------------------------------

class TestCmdModeNoArgs:
    def test_no_args_shows_current_and_no_switch(self):
        update, context = _make_update_context(args=[])
        mock_s = MagicMock()
        mock_s.trading_mode = "paper"

        with patch("src.telegram.bot._get_admin_ids", return_value={99999}), \
             patch("src.telegram.bot._apply_mode_switch") as mock_switch, \
             patch("src.config.settings.settings", mock_s):
            from src.telegram.bot import cmd_mode
            _run(cmd_mode(update, context))

        mock_switch.assert_not_called()
        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "PAPER" in text.upper() or "paper" in text.lower()

    def test_no_args_live_mode_shows_live(self):
        update, context = _make_update_context(args=[])
        mock_s = MagicMock()
        mock_s.trading_mode = "live"

        with patch("src.telegram.bot._get_admin_ids", return_value={99999}), \
             patch("src.telegram.bot._apply_mode_switch") as mock_switch, \
             patch("src.config.settings.settings", mock_s):
            from src.telegram.bot import cmd_mode
            _run(cmd_mode(update, context))

        mock_switch.assert_not_called()
        text = update.message.reply_text.call_args[0][0]
        assert "LIVE" in text.upper() or "live" in text.lower()


# ---------------------------------------------------------------------------
# mode:live:confirm callback → switches to live, response mentions LIVE
# ---------------------------------------------------------------------------

class TestModeConfirmCallback:
    def test_confirm_live_calls_switch_and_responds_live(self):
        update, context, query = _make_query_context("mode:live:confirm")
        mock_s = MagicMock()
        mock_s.trading_mode = "paper"

        def fake_switch(mode):
            mock_s.trading_mode = mode

        with patch("src.telegram.bot._get_admin_ids", return_value={99999}), \
             patch("src.telegram.bot._apply_mode_switch", side_effect=fake_switch) as mock_switch, \
             patch("src.config.settings.settings", mock_s):
            from src.telegram.bot import _callback_router
            _run(_callback_router(update, context))

        mock_switch.assert_called_once_with("live")
        text = query.edit_message_text.call_args[0][0]
        assert "LIVE" in text.upper()
