"""G4-26: per-token classification for the /activity → DB importer.

Strict invariant: a resolved position must NEVER be classified `open`. The safe
default when neither /positions nor Gamma can tell is `pending_redeem`.
"""
from src.scheduler import _classify_imported_token

REDEEMED = {"cid_done"}


def _pos(redeemable=False, cur=0.5):
    return {"redeemable": redeemable, "curPrice": cur}


# ---------------------------------------------------------------------------
# skip
# ---------------------------------------------------------------------------

def test_redeemed_condition_skips():
    assert _classify_imported_token(6.0, "cid_done", REDEEMED, _pos(), False) == "skip"


def test_below_min_usd_skips():
    # Sold/flat: net below the noise floor
    assert _classify_imported_token(0.4, "cid_x", REDEEMED, _pos(), False) == "skip"


# ---------------------------------------------------------------------------
# pending_redeem — won / resolved
# ---------------------------------------------------------------------------

def test_redeemable_true_is_pending_redeem():
    assert _classify_imported_token(6.0, "c", REDEEMED, _pos(redeemable=True, cur=1.0), None) == "pending_redeem"


def test_curprice_won_is_pending_redeem():
    assert _classify_imported_token(6.0, "c", REDEEMED, _pos(redeemable=False, cur=0.995), False) == "pending_redeem"


def test_live_price_but_gamma_resolved_is_pending_redeem():
    # Price still mid but Gamma says the event resolved → not open
    assert _classify_imported_token(6.0, "c", REDEEMED, _pos(cur=0.4), True) == "pending_redeem"


# ---------------------------------------------------------------------------
# open — genuinely live
# ---------------------------------------------------------------------------

def test_live_position_is_open():
    assert _classify_imported_token(6.0, "c", REDEEMED, _pos(redeemable=False, cur=0.45), False) == "open"


def test_not_in_positions_but_gamma_live_is_open():
    assert _classify_imported_token(6.0, "c", REDEEMED, None, False) == "open"


def test_deep_otm_live_is_open():
    # curPrice≈0 but Gamma explicitly says live → still a real open position
    assert _classify_imported_token(6.0, "c", REDEEMED, _pos(cur=0.0), False) == "open"


# ---------------------------------------------------------------------------
# SAFE DEFAULT — resolved must never leak into open
# ---------------------------------------------------------------------------

def test_no_positions_no_gamma_defaults_pending_redeem():
    # Neither signal available → pending_redeem (never open)
    assert _classify_imported_token(6.0, "c", REDEEMED, None, None) == "pending_redeem"


def test_curprice_zero_unknown_gamma_defaults_pending_redeem():
    assert _classify_imported_token(6.0, "c", REDEEMED, _pos(cur=0.0), None) == "pending_redeem"


def test_invariant_no_resolved_signal_ever_open():
    """Sweep: any input where resolution is true/unknown must not be 'open'."""
    cases = [
        (_pos(redeemable=True, cur=1.0), None),
        (_pos(redeemable=True, cur=1.0), True),
        (_pos(cur=0.995), False),
        (_pos(cur=0.4), True),
        (_pos(cur=0.0), True),
        (_pos(cur=0.0), None),
        (None, True),
        (None, None),
    ]
    for pos, gres in cases:
        v = _classify_imported_token(6.0, "c", REDEEMED, pos, gres)
        assert v != "open", f"resolved/unknown wrongly 'open' for pos={pos} gamma={gres}"
