"""Tests for emergency-stop auto-clear logic on startup."""
from datetime import datetime, date, timedelta
from unittest.mock import MagicMock, patch, call


def _make_rm(stopped: bool, stop_type: str, triggered_at: datetime | None = None):
    rm = MagicMock()
    rm.is_emergency_stopped.return_value = stopped
    rm.get_stop_type.return_value = stop_type
    rm.get_stop_triggered_at.return_value = triggered_at
    return rm


# ---------------------------------------------------------------------------
# _check_emergency_stop_on_startup
# ---------------------------------------------------------------------------

class TestCheckEmergencyStopOnStartup:
    def _run(self, rm):
        with patch("src.risk.risk_manager.risk_manager", rm), \
             patch("src.scheduler.get_notifier") as mock_notif:
            mock_notif.return_value.send = MagicMock()
            from src.scheduler import _check_emergency_stop_on_startup
            _check_emergency_stop_on_startup()
        return mock_notif.return_value.send

    def test_no_stop_does_nothing(self):
        rm = _make_rm(stopped=False, stop_type="")
        send = self._run(rm)
        rm.clear_emergency_stop.assert_not_called()
        send.assert_not_called()

    def test_manual_stop_clears_on_restart(self):
        """reason=manual → auto-clear on restart."""
        rm = _make_rm(stopped=True, stop_type="manual")
        send = self._run(rm)
        rm.clear_emergency_stop.assert_called_once()
        send.assert_called_once()
        assert "manual" in send.call_args[0][0].lower() or "перезапуск" in send.call_args[0][0].lower()

    def test_total_loss_stop_persists(self):
        """reason=total_loss → must NOT be auto-cleared."""
        rm = _make_rm(stopped=True, stop_type="total_loss")
        send = self._run(rm)
        rm.clear_emergency_stop.assert_not_called()
        send.assert_not_called()

    def test_daily_loss_clears_on_new_day(self):
        """reason=daily_loss, triggered yesterday → clear."""
        yesterday = datetime.utcnow() - timedelta(days=1)
        rm = _make_rm(stopped=True, stop_type="daily_loss", triggered_at=yesterday)
        send = self._run(rm)
        rm.clear_emergency_stop.assert_called_once()
        send.assert_called_once()

    def test_daily_loss_persists_same_day(self):
        """reason=daily_loss, triggered today → do NOT clear."""
        today_ts = datetime.combine(date.today(), datetime.min.time())
        rm = _make_rm(stopped=True, stop_type="daily_loss", triggered_at=today_ts)
        send = self._run(rm)
        rm.clear_emergency_stop.assert_not_called()
        send.assert_not_called()

    def test_daily_loss_clears_when_triggered_at_none(self):
        """triggered_at=None treated as old → clear."""
        rm = _make_rm(stopped=True, stop_type="daily_loss", triggered_at=None)
        send = self._run(rm)
        rm.clear_emergency_stop.assert_called_once()


# ---------------------------------------------------------------------------
# risk_manager: stop_type propagation
# ---------------------------------------------------------------------------

class TestStopTypeTracking:
    def _make_rm_instance(self, **kwargs):
        defaults = dict(
            max_single_bet_usd=3.0,
            max_daily_bets=5,
            max_concurrent_bets=3,
            daily_loss_limit_usd=10.0,
            total_stop_loss_usd=20.0,
            trading_mode="live",
            max_basket_legs_open=12,
            max_tail_positions=3,
            total_deployed_cap_usd=40.0,
            basket_max_usd=26.0,
            tail_max_usd=18.0,
        )
        defaults.update(kwargs)

        import threading
        from src.risk.risk_manager import RiskManager
        with patch("src.risk.risk_manager.RiskManager._load_settings"):
            rm = RiskManager.__new__(RiskManager)
        rm._lock = threading.Lock()
        rm._max_single_bet_usd = defaults["max_single_bet_usd"]
        rm._max_daily_bets = defaults["max_daily_bets"]
        rm._max_concurrent_bets = defaults["max_concurrent_bets"]
        rm._daily_loss_limit_usd = defaults["daily_loss_limit_usd"]
        rm._total_stop_loss_usd = defaults["total_stop_loss_usd"]
        rm._max_basket_legs_open = defaults["max_basket_legs_open"]
        rm._max_tail_positions = defaults["max_tail_positions"]
        rm._total_deployed_cap_usd = defaults["total_deployed_cap_usd"]
        rm._basket_max_usd = defaults["basket_max_usd"]
        rm._tail_max_usd = defaults["tail_max_usd"]
        rm._reset_daily_state()
        rm._total_loss = 0.0
        rm._emergency_stop = False
        rm._emergency_stop_reason = ""
        rm._emergency_stop_type = ""
        rm._open_bets = {}
        rm._reserved_usd = 0.0
        rm._basket_legs_open = 0
        rm._load_live_stats = lambda: None
        rm._get_deployed_by_strategy = lambda: {}
        rm._persist_emergency_stop = MagicMock()
        return rm

    def test_total_stop_sets_type_total_loss(self):
        rm = self._make_rm_instance()
        rm._total_loss = 25.0
        with patch("src.config.settings.settings") as mock_s:
            mock_s.trading_mode = "live"
            with patch("src.notifications.telegram.get_notifier"):
                ok, _ = rm.can_place_order(1.0)
        assert ok is False
        assert rm._emergency_stop_type == "total_loss"
        rm._persist_emergency_stop.assert_called_with(True, mock_s.trading_mode and ANY, "total_loss")

    def test_daily_loss_stop_sets_type_daily_loss(self):
        rm = self._make_rm_instance()
        rm._daily_loss = 15.0
        with patch("src.config.settings.settings") as mock_s:
            mock_s.trading_mode = "live"
            with patch("src.notifications.telegram.get_notifier"):
                ok, _ = rm.can_place_order(1.0)
        assert ok is False
        assert rm._emergency_stop_type == "daily_loss"

    def test_manual_stop_sets_type_manual(self):
        rm = self._make_rm_instance()
        rm.set_emergency_stop("test", stop_type="manual")
        assert rm._emergency_stop_type == "manual"

    def test_set_emergency_stop_default_type_is_manual(self):
        rm = self._make_rm_instance()
        rm.set_emergency_stop("some reason")
        assert rm._emergency_stop_type == "manual"

    def test_clear_resets_type(self):
        rm = self._make_rm_instance()
        rm.set_emergency_stop("x")
        rm.clear_emergency_stop()
        assert rm._emergency_stop_type == ""


# sentinel for assert_called_with when we don't care about exact value
from unittest.mock import ANY
