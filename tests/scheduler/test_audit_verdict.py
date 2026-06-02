"""G4-22: three-way audit verdict — no stored≠activity line is ever marked OK."""
from src.scheduler import _audit_verdict


# ---------------------------------------------------------------------------
# OK — only when reconciled or honest pending
# ---------------------------------------------------------------------------

def test_ok_when_stored_matches_activity():
    # Seoul: redeemed win, stored == activity → OK
    assert _audit_verdict(filled=True, has_redeem=True, sold=False,
                          stored_pnl=0.74, activity_pnl=0.74) == "OK"


def test_ok_when_within_one_cent():
    assert _audit_verdict(filled=True, has_redeem=True, sold=False,
                          stored_pnl=0.745, activity_pnl=0.74) == "OK"


def test_ok_when_honest_pending_not_filled():
    # Lucknow: no BUY, nothing stored → honest pending, OK
    assert _audit_verdict(filled=False, has_redeem=False, sold=False,
                          stored_pnl=0.0, activity_pnl=0.0) == "OK"


# ---------------------------------------------------------------------------
# PENDING_REDEEM — filled winner, resolved but not yet redeemed/sold
# ---------------------------------------------------------------------------

def test_pending_redeem_seattle():
    # Seattle: filled, activity shows +$0.42, nothing redeemed/sold yet, stored stale
    v = _audit_verdict(filled=True, has_redeem=False, sold=False,
                       stored_pnl=0.0, activity_pnl=0.42)
    assert v == "PENDING_REDEEM"


def test_pending_redeem_not_marked_ok_even_with_stored_value():
    v = _audit_verdict(filled=True, has_redeem=False, sold=False,
                       stored_pnl=0.40, activity_pnl=0.42)
    assert v == "PENDING_REDEEM"


# ---------------------------------------------------------------------------
# DRIFT — filled AND finalized, but stored diverged from /activity
# ---------------------------------------------------------------------------

def test_drift_sold_position_milan():
    # Milan: sold via early-exit, stored $0.05 over /activity truth → DRIFT
    v = _audit_verdict(filled=True, has_redeem=False, sold=True,
                       stored_pnl=0.43, activity_pnl=0.38)
    assert v.startswith("DRIFT")
    assert "+0.05" in v


def test_drift_redeemed_position():
    # Atlanta/Chicago: redeemed but stored $0.03 over → DRIFT
    v = _audit_verdict(filled=True, has_redeem=True, sold=False,
                       stored_pnl=0.21, activity_pnl=0.18)
    assert v.startswith("DRIFT")
    assert "+0.03" in v


def test_drift_negative_delta_sign():
    v = _audit_verdict(filled=True, has_redeem=True, sold=False,
                       stored_pnl=0.10, activity_pnl=0.16)
    assert v.startswith("DRIFT")
    assert "-0.06" in v


def test_not_filled_but_stored_nonzero_is_drift_not_ok():
    # No BUY yet something stored → not honest pending, must surface as DRIFT
    v = _audit_verdict(filled=False, has_redeem=False, sold=False,
                       stored_pnl=-3.34, activity_pnl=0.0)
    assert v.startswith("DRIFT")


# ---------------------------------------------------------------------------
# Invariant: no stored≠activity line is ever OK
# ---------------------------------------------------------------------------

def test_no_drift_line_is_ever_ok():
    cases = [
        # (filled, redeem, sold, stored, activity)
        (True, False, False, 0.0, 0.42),   # Seattle  → PENDING_REDEEM
        (True, True, False, 0.21, 0.18),   # Atlanta  → DRIFT
        (True, False, True, 0.43, 0.38),   # Milan    → DRIFT
        (False, False, False, -3.34, 0.0), # phantom  → DRIFT
    ]
    for filled, redeem, sold, stored, activity in cases:
        v = _audit_verdict(filled, redeem, sold, stored, activity)
        assert v != "OK", f"stored≠activity wrongly OK for {(filled, redeem, sold, stored, activity)}"
