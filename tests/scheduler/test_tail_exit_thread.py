"""G4-30: tail-exit runs on its own thread, decoupled from schedule.run_pending().

This fix is about tick DELIVERY, not decision logic — these tests assert the
ticker is NOT in the blocking schedule loop, that it ticks on its own timer, and
that a slow job in the main schedule loop cannot starve it. decide_tail_exit and
the hot/normal cadence gate are deliberately NOT exercised here.
"""
import threading
import time

import schedule

from src import scheduler as sch


def test_tail_exit_not_registered_in_schedule():
    """The ticker must NOT live in the single-threaded run_pending() loop."""
    schedule.clear()
    try:
        sch.setup_scheduler()
        funcs = {getattr(j.job_func, "func", j.job_func) for j in schedule.jobs}
        assert sch._job_tail_early_exit not in funcs, \
            "tail-exit must not be registered on the blocking scheduler"
    finally:
        schedule.clear()


def test_ticker_ticks_on_its_own_timer_and_stops(monkeypatch):
    """Loop fires the job repeatedly on its base interval and halts on stop()."""
    calls: list[float] = []
    monkeypatch.setattr(sch, "_job_tail_early_exit", lambda: calls.append(time.monotonic()))

    t = sch.start_tail_exit_thread(base_seconds=0.05)
    try:
        time.sleep(0.35)          # ~6 intervals
        assert len(calls) >= 3, f"expected several ticks, got {len(calls)}"
    finally:
        sch.stop_tail_exit_thread(timeout=2.0)

    assert not t.is_alive(), "ticker thread must stop after stop_tail_exit_thread()"
    settled = len(calls)
    time.sleep(0.2)
    assert len(calls) == settled, "no ticks must fire after stop"


def test_slow_main_loop_job_does_not_delay_ticker(monkeypatch):
    """A blocking job (simulating discover ~90s) on the main thread must not
    push out the ticker — it runs on a separate thread."""
    ticks: list[float] = []
    monkeypatch.setattr(sch, "_job_tail_early_exit", lambda: ticks.append(time.monotonic()))

    sch.start_tail_exit_thread(base_seconds=0.05)
    try:
        # Emulate the main scheduler thread stuck inside a heavy job.
        blocked_until = time.monotonic() + 0.3
        while time.monotonic() < blocked_until:
            time.sleep(0.01)      # main thread busy; ticker is independent
        assert len(ticks) >= 3, \
            f"ticker should keep firing while main loop blocks, got {len(ticks)}"
    finally:
        sch.stop_tail_exit_thread(timeout=2.0)


def test_tick_seconds_floored(monkeypatch):
    """A misconfigured 0-minute hot poll cannot busy-spin the ticker."""
    from src.strategy import config as cfg
    monkeypatch.setattr(cfg.strategy_settings, "tail_poll_hot_minutes", 0, raising=False)
    assert sch._compute_tail_tick_seconds() == sch._TAIL_TICK_FLOOR_SECONDS


def test_tick_seconds_uses_hot_interval(monkeypatch):
    """Base tick equals the hot poll interval (so the gate delivers 5/10 min)."""
    from src.strategy import config as cfg
    monkeypatch.setattr(cfg.strategy_settings, "tail_poll_hot_minutes", 5, raising=False)
    assert sch._compute_tail_tick_seconds() == 5 * 60
