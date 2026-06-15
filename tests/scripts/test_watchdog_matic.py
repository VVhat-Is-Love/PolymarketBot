"""Tests for MATIC balance watchdog (_check_matic and main() integration)."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.scripts.watchdog import _check_matic, main

_WALLET = "0xDEADBEEFCAFEBABE1234567890ABCDEF12345678"


def _fresh(tmp_path: Path, *threads: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for t in threads:
        (tmp_path / f"{t}.ts").write_text(now)


def _old(tmp_path: Path, thread: str, age_s: int) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).isoformat()
    (tmp_path / f"{thread}.ts").write_text(ts)


# ---------------------------------------------------------------------------
# _check_matic
# ---------------------------------------------------------------------------

class TestCheckMatic:
    def test_low_balance_returns_alert(self):
        with patch("src.config.settings.settings") as s, \
             patch("src.blockchain.balance.get_matic_balance", return_value=0.05):
            s.eoa_wallet_address = _WALLET
            s.matic_alert_threshold = 0.10
            result = _check_matic()
        assert len(result) == 1
        assert "MATIC" in result[0]
        assert "0.0500" in result[0]

    def test_sufficient_balance_silent(self):
        with patch("src.config.settings.settings") as s, \
             patch("src.blockchain.balance.get_matic_balance", return_value=0.50):
            s.eoa_wallet_address = _WALLET
            s.matic_alert_threshold = 0.10
            result = _check_matic()
        assert result == []

    def test_exactly_at_threshold_is_ok(self):
        with patch("src.config.settings.settings") as s, \
             patch("src.blockchain.balance.get_matic_balance", return_value=0.10):
            s.eoa_wallet_address = _WALLET
            s.matic_alert_threshold = 0.10
            result = _check_matic()
        assert result == []

    def test_no_wallet_skips_balance_call(self):
        with patch("src.config.settings.settings") as s, \
             patch("src.blockchain.balance.get_matic_balance") as mock_bal:
            s.eoa_wallet_address = ""
            result = _check_matic()
        mock_bal.assert_not_called()
        assert result == []

    def test_rpc_failure_silent(self):
        """Balance RPC error must not raise — return empty list."""
        with patch("src.config.settings.settings") as s, \
             patch("src.blockchain.balance.get_matic_balance", side_effect=Exception("timeout")):
            s.eoa_wallet_address = _WALLET
            s.matic_alert_threshold = 0.10
            result = _check_matic()
        assert result == []

    def test_alert_mentions_pending_redeem(self):
        with patch("src.config.settings.settings") as s, \
             patch("src.blockchain.balance.get_matic_balance", return_value=0.01):
            s.eoa_wallet_address = _WALLET
            s.matic_alert_threshold = 0.10
            result = _check_matic()
        assert result
        assert "pending_redeem" in result[0]

    def test_alert_shows_eoa_prefix(self):
        with patch("src.config.settings.settings") as s, \
             patch("src.blockchain.balance.get_matic_balance", return_value=0.01):
            s.eoa_wallet_address = _WALLET
            s.matic_alert_threshold = 0.10
            result = _check_matic()
        assert _WALLET[:10] in result[0]


# ---------------------------------------------------------------------------
# main() integration — MATIC path
# ---------------------------------------------------------------------------

class TestMainWithMatic:
    def test_matic_low_returns_1(self, tmp_path):
        _fresh(tmp_path, "scheduler", "tail_exit", "telegram")
        notifier = MagicMock()
        notifier.send.return_value = True
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path), \
             patch("src.config.settings.settings") as s, \
             patch("src.blockchain.balance.get_matic_balance", return_value=0.01), \
             patch("src.notifications.telegram.get_notifier", return_value=notifier):
            s.eoa_wallet_address = _WALLET
            s.matic_alert_threshold = 0.10
            assert main() == 1

    def test_matic_low_sends_telegram(self, tmp_path):
        _fresh(tmp_path, "scheduler", "tail_exit", "telegram")
        notifier = MagicMock()
        notifier.send.return_value = True
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path), \
             patch("src.config.settings.settings") as s, \
             patch("src.blockchain.balance.get_matic_balance", return_value=0.01), \
             patch("src.notifications.telegram.get_notifier", return_value=notifier):
            s.eoa_wallet_address = _WALLET
            s.matic_alert_threshold = 0.10
            main()
        notifier.send.assert_called_once()
        assert "MATIC" in notifier.send.call_args[0][0]

    def test_both_ok_returns_0(self, tmp_path):
        _fresh(tmp_path, "scheduler", "tail_exit", "telegram")
        notifier = MagicMock()
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path), \
             patch("src.config.settings.settings") as s, \
             patch("src.blockchain.balance.get_matic_balance", return_value=0.50), \
             patch("src.notifications.telegram.get_notifier", return_value=notifier):
            s.eoa_wallet_address = _WALLET
            s.matic_alert_threshold = 0.10
            result = main()
        assert result == 0
        notifier.send.assert_not_called()

    def test_combined_message_has_both_sections(self, tmp_path):
        """Stale thread + low MATIC → single message with both topics."""
        _fresh(tmp_path, "tail_exit", "telegram")
        _old(tmp_path, "scheduler", 200)
        notifier = MagicMock()
        notifier.send.return_value = True
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path), \
             patch("src.config.settings.settings") as s, \
             patch("src.blockchain.balance.get_matic_balance", return_value=0.01), \
             patch("src.notifications.telegram.get_notifier", return_value=notifier):
            s.eoa_wallet_address = _WALLET
            s.matic_alert_threshold = 0.10
            main()
        text = notifier.send.call_args[0][0]
        assert "watchdog" in text.lower()
        assert "MATIC" in text

    def test_no_wallet_does_not_block_healthy_exit(self, tmp_path):
        """No wallet configured → MATIC check skips → healthy threads = 0."""
        _fresh(tmp_path, "scheduler", "tail_exit", "telegram")
        notifier = MagicMock()
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path), \
             patch("src.config.settings.settings") as s, \
             patch("src.notifications.telegram.get_notifier", return_value=notifier):
            s.eoa_wallet_address = ""
            result = main()
        assert result == 0
        notifier.send.assert_not_called()
