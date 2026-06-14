"""Tests for watchdog main(): stale detection and Telegram alert logic."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.scripts.watchdog import main, _check_threads


def _write_fresh(tmp_path: Path, *threads: str) -> None:
    """Write a just-now timestamp for each named thread."""
    now = datetime.now(timezone.utc).isoformat()
    for t in threads:
        (tmp_path / f"{t}.ts").write_text(now)


def _write_old(tmp_path: Path, thread: str, age_seconds: int) -> None:
    old = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    (tmp_path / f"{thread}.ts").write_text(old)


# ---------------------------------------------------------------------------
# _check_threads
# ---------------------------------------------------------------------------

class TestCheckThreads:
    def test_empty_when_all_healthy(self, tmp_path):
        _write_fresh(tmp_path, "scheduler", "tail_exit", "telegram")
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            assert _check_threads() == []

    def test_lists_missing_thread(self, tmp_path):
        _write_fresh(tmp_path, "tail_exit", "telegram")
        # scheduler file absent
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            stale = _check_threads()
        assert len(stale) == 1
        assert "scheduler" in stale[0].lower() or "scheduler" in stale[0]

    def test_lists_stale_tail_exit(self, tmp_path):
        _write_fresh(tmp_path, "scheduler", "telegram")
        _write_old(tmp_path, "tail_exit", 700)  # > 660 s threshold
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            stale = _check_threads()
        assert len(stale) == 1
        assert "tail-exit" in stale[0].lower() or "CRITICAL" in stale[0]

    def test_lists_multiple_stale(self, tmp_path):
        _write_old(tmp_path, "scheduler", 200)  # > 90 s
        _write_old(tmp_path, "tail_exit", 700)  # > 660 s
        _write_fresh(tmp_path, "telegram")
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            stale = _check_threads()
        assert len(stale) == 2


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class TestMain:
    def test_returns_0_when_all_healthy(self, tmp_path):
        _write_fresh(tmp_path, "scheduler", "tail_exit", "telegram")
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            assert main() == 0

    def test_returns_1_when_any_stale(self, tmp_path):
        _write_fresh(tmp_path, "tail_exit", "telegram")
        _write_old(tmp_path, "scheduler", 200)
        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path), \
             patch("src.notifications.telegram.get_notifier", return_value=mock_notifier):
            result = main()
        assert result == 1

    def test_sends_telegram_alert_on_stale(self, tmp_path):
        _write_fresh(tmp_path, "tail_exit", "telegram")
        _write_old(tmp_path, "scheduler", 200)
        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path), \
             patch("src.notifications.telegram.get_notifier", return_value=mock_notifier):
            main()
        mock_notifier.send.assert_called_once()

    def test_no_alert_when_all_healthy(self, tmp_path):
        _write_fresh(tmp_path, "scheduler", "tail_exit", "telegram")
        mock_notifier = MagicMock()
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path), \
             patch("src.notifications.telegram.get_notifier", return_value=mock_notifier):
            main()
        mock_notifier.send.assert_not_called()

    def test_alert_text_mentions_watchdog(self, tmp_path):
        _write_old(tmp_path, "tail_exit", 700)
        _write_fresh(tmp_path, "scheduler", "telegram")
        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path), \
             patch("src.notifications.telegram.get_notifier", return_value=mock_notifier):
            main()
        alert_text = mock_notifier.send.call_args[0][0]
        assert "watchdog" in alert_text.lower()

    def test_returns_1_when_no_heartbeat_files(self, tmp_path):
        mock_notifier = MagicMock()
        mock_notifier.send.return_value = True
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path), \
             patch("src.notifications.telegram.get_notifier", return_value=mock_notifier):
            result = main()
        assert result == 1

    def test_survives_telegram_send_failure(self, tmp_path):
        _write_old(tmp_path, "scheduler", 200)
        _write_fresh(tmp_path, "tail_exit", "telegram")
        mock_notifier = MagicMock()
        mock_notifier.send.side_effect = RuntimeError("network down")
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path), \
             patch("src.notifications.telegram.get_notifier", return_value=mock_notifier):
            result = main()  # must not raise
        assert result == 1
