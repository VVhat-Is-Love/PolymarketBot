"""
FIX-1 acceptance verification — dry-run, zero production side-effects.

Run on the server (from the project root):
    python scripts/verify_drawdown_stop.py

What it does:
  1. Creates an isolated RiskManager (NOT the production singleton).
  2. Patches equity fetch and HWM update — no CLOB calls, no DB writes.
  3. Runs evaluate_drawdown_stop() for three scenarios.
  4. Prints PASS / FAIL with the exact log lines that would appear in prod.

Acceptance criteria (from CODER_TASKS_v2_18062026.md FIX-1):
  - dd=16% → EMERGENCY STOP SET [drawdown] + entry blocked
  - dd=14% → no stop, no halving (above hard, below soft... wait, soft=8%): no stop
  - dd=9%  → no stop, size×0.5 (soft threshold)
"""
import sys
import os
import logging
from unittest.mock import patch, MagicMock

# Project root on PATH so imports resolve without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-8s %(name)s: %(message)s",
    stream=sys.stdout,
)

from src.risk.risk_manager import RiskManager  # noqa: E402


def _make_isolated_rm() -> RiskManager:
    """Fresh RiskManager — shares no state with the production singleton."""
    return RiskManager()


def _run_case(label: str, equity: float, hwm: float, reset_flag: bool = False) -> bool:
    """
    Returns True if the scenario produced the expected outcome.
    Prints a PASS/FAIL line plus the key log output.
    """
    rm = _make_isolated_rm()
    rm._drawdown_hard_reset = reset_flag

    dd_expected = (hwm - equity) / hwm * 100
    hard_pct = rm._drawdown_hard_stop_pct * 100
    soft_pct = rm._drawdown_soft_pct * 100

    expected_stop = (dd_expected >= hard_pct) and not reset_flag
    expected_mult = 0.5 if dd_expected >= soft_pct else 1.0

    fake_equity_snap = {"ok": True, "total_bid": equity}

    with (
        patch("src.telegram.stats.get_equity_bid", return_value=fake_equity_snap),
        patch.object(rm, "_update_equity_hwm", return_value=hwm),
        patch.object(rm, "_persist_emergency_stop"),
        patch("src.config.settings.settings") as mock_settings,
    ):
        mock_settings.trading_mode = "live"
        mock_settings.drawdown_soft_pct = rm._drawdown_soft_pct
        mock_settings.drawdown_hard_stop_pct = rm._drawdown_hard_stop_pct

        # Disable Telegram notification (imported lazily inside _trigger_drawdown_stop)
        with patch("src.notifications.telegram.get_notifier", return_value=MagicMock()):
            rm.evaluate_drawdown_stop()

    got_stop = rm._emergency_stop and rm._emergency_stop_type == "drawdown"
    got_mult = rm._dd_size_multiplier

    stop_ok = got_stop == expected_stop
    mult_ok = got_mult == expected_mult
    passed = stop_ok and mult_ok

    status = "PASS" if passed else "FAIL"
    print(
        f"\n[{status}] {label}"
        f"\n  equity={equity:.2f}  hwm={hwm:.2f}  dd={dd_expected:.1f}%"
        f"  (hard>={hard_pct:.0f}%  soft>={soft_pct:.0f}%)"
        f"\n  stop_fired={got_stop} (expected={expected_stop})"
        f"  size_mult={got_mult:.1f} (expected={expected_mult:.1f})"
    )
    if got_stop:
        reason_ascii = rm._emergency_stop_reason.encode("ascii", errors="replace").decode("ascii")
        print(f"  log: EMERGENCY STOP SET [drawdown] - {reason_ascii}")
    if not passed:
        if not stop_ok:
            print(f"  !! stop_fired mismatch")
        if not mult_ok:
            print(f"  !! size_mult mismatch")
    return passed


def main() -> int:
    print("=" * 60)
    print("FIX-1 dry-run: auto-drawdown stop verification")
    print("=" * 60)

    results = [
        _run_case("dd=16% ABOVE hard (must FIRE stop)",        equity=84.0,  hwm=100.0),
        _run_case("dd=14% BELOW hard (must NOT fire)",         equity=86.0,  hwm=100.0),
        _run_case("dd=9%  ABOVE soft (no stop, size x0.5)",   equity=91.0,  hwm=100.0),
        _run_case("dd=5%  below soft (no stop, size x1.0)",   equity=95.0,  hwm=100.0),
        _run_case("dd=16% + reset_flag=True (must NOT fire)",  equity=84.0,  hwm=100.0,
                  reset_flag=True),
    ]

    print("\n" + "=" * 60)
    passed = sum(results)
    total = len(results)
    overall = "ALL PASS" if passed == total else f"{total - passed} FAILED"
    print(f"Result: {passed}/{total} — {overall}")
    print("=" * 60)

    if passed < total:
        print("\nFIX-1 NOT accepted — drawdown stop logic needs review.")
        return 1
    print("\nFIX-1 accepted by dry-run. Still need live-log confirmation on server:")
    print("  journalctl -u polymarket-bot -f | grep -i 'drawdown\\|EMERGENCY STOP'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
