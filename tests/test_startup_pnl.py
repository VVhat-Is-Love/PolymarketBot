"""Startup PnL report: numerator and denominator from the SAME executed set,
realized PnL from /activity, |loss| never exceeds stake (fixes −$9.19 @ $6.02)."""
from types import SimpleNamespace
from unittest.mock import patch

from src.main import _executed_realized_pnl, _bucket_executed, _pending_expected_gain


def _t(token, stake, pnl=None, status="resolved", shares=0.0):
    return SimpleNamespace(token_id=token, stake_usd=stake, pnl_usd=pnl,
                           status=status, size_shares=shares)


def _buy(token, cost):
    return {"type": "TRADE", "side": "BUY", "asset": token,
            "size": "6.0", "usdcSize": str(cost), "conditionId": "c"}


def test_loss_from_activity_cannot_exceed_stake():
    executed = [_t("tokA", 6.02)]
    with patch("src.config.settings.settings") as st, \
         patch("src.market.polymarket_data.get_activity", return_value=[_buy("tokA", 6.02)]):
        st.proxy_wallet_address = "0xw"
        pnl, source = _executed_realized_pnl(executed)

    assert source == "activity"
    assert pnl == -6.02                                  # realized = −cost (lost)
    assert abs(pnl) <= sum(t.stake_usd for t in executed) + 1e-9


def test_dedups_by_token_dallas_double():
    # Two executed rows share a token (the Dallas double). realized_pnl_by_token
    # already sums both buys → count the token once, matching summed stake.
    executed = [_t("tokA", 5.6), _t("tokA", 5.6)]
    act = [_buy("tokA", 5.6), _buy("tokA", 5.6)]
    with patch("src.config.settings.settings") as st, \
         patch("src.market.polymarket_data.get_activity", return_value=act):
        st.proxy_wallet_address = "0xw"
        pnl, _ = _executed_realized_pnl(executed)

    staked = sum(t.stake_usd for t in executed)          # 11.2
    assert round(pnl, 2) == -11.2                        # full token cost once
    assert abs(pnl) <= staked + 1e-9


def test_win_is_positive_and_under_no_loss_constraint():
    # BUY $6 then REDEEM $6.74 → realized +0.74
    act = [_buy("tokA", 6.0),
           {"type": "REDEEM", "asset": "", "usdcSize": "6.74", "conditionId": "c"}]
    with patch("src.config.settings.settings") as st, \
         patch("src.market.polymarket_data.get_activity", return_value=act):
        st.proxy_wallet_address = "0xw"
        pnl, source = _executed_realized_pnl([_t("tokA", 6.0)])
    assert source == "activity"
    assert pnl == 0.74


def test_falls_back_to_stored_when_activity_unavailable():
    executed = [_t("tokA", 6.0, pnl=-3.0)]
    with patch("src.config.settings.settings") as st, \
         patch("src.market.polymarket_data.get_activity", return_value=[]):
        st.proxy_wallet_address = "0xw"
        pnl, source = _executed_realized_pnl(executed)
    assert source == "stored"
    assert pnl == -3.0


def test_aggregate_invariant_loss_le_stake_across_positions():
    executed = [_t("tokA", 6.0), _t("tokB", 6.0), _t("tokC", 6.0)]
    # all three lost (BUY only)
    act = [_buy("tokA", 6.0), _buy("tokB", 6.0), _buy("tokC", 6.0)]
    with patch("src.config.settings.settings") as st, \
         patch("src.market.polymarket_data.get_activity", return_value=act):
        st.proxy_wallet_address = "0xw"
        pnl, _ = _executed_realized_pnl(executed)
    staked = sum(t.stake_usd for t in executed)
    assert pnl == -18.0
    assert abs(pnl) <= staked + 1e-9                     # |Y| ≤ X


# ---------------------------------------------------------------------------
# G4-29b: pending_redeem wins must NOT be booked at −cost
# ---------------------------------------------------------------------------

def test_bucket_split_separates_pending_from_closed():
    trades = [
        _t("c1", 6.0, status="resolved"),
        _t("c2", 6.0, status="sold"),
        _t("p1", 6.0, status="pending_redeem", shares=6.6),
        _t("o1", 6.0, status="filled"),
        _t("x1", 6.0, status="not_filled"),
    ]
    closed, pending, open_filled = _bucket_executed(trades)
    assert {t.token_id for t in closed} == {"c1", "c2"}
    assert [t.token_id for t in pending] == ["p1"]
    assert [t.token_id for t in open_filled] == ["o1"]


def test_pending_expected_gain_is_positive_never_negative():
    pending = [_t("p1", 6.0, status="pending_redeem", shares=6.6),
               _t("p2", 6.0, status="pending_redeem", shares=6.5)]
    # (6.6−6.0) + (6.5−6.0) = 1.1, shown as an expected gain
    assert _pending_expected_gain(pending) == 1.1
    # a (mis)priced pending where shares<stake is floored at 0, never negative
    assert _pending_expected_gain([_t("p3", 6.0, status="pending_redeem", shares=5.0)]) == 0.0


def test_six_pending_wins_do_not_make_realized_negative():
    """Acceptance: 6 winning pending_redeem legs must not drag the realized PnL to
    −$26. Realized is computed over CLOSED only; pending is excluded."""
    # 6 winning pending legs (would be −36 at −cost) + 2 closed wins
    pending = [_t(f"p{i}", 6.0, status="pending_redeem", shares=6.6) for i in range(6)]
    closed = [_t("c1", 6.0, status="resolved"), _t("c2", 6.0, status="resolved")]
    trades = pending + closed
    _closed, _pending, _open = _bucket_executed(trades)

    # Realized only over the 2 closed wins → positive, pending excluded entirely
    act = [_buy("c1", 6.0), {"type": "REDEEM", "asset": "", "usdcSize": "6.7", "conditionId": "c"},
           _buy("c2", 6.0), {"type": "REDEEM", "asset": "", "usdcSize": "6.7", "conditionId": "d"}]
    # give each redeem its own condition/token link
    act = [
        {"type": "TRADE", "side": "BUY", "asset": "c1", "size": "6", "usdcSize": "6.0", "conditionId": "cc1"},
        {"type": "REDEEM", "asset": "", "usdcSize": "6.6", "conditionId": "cc1"},
        {"type": "TRADE", "side": "BUY", "asset": "c2", "size": "6", "usdcSize": "6.0", "conditionId": "cc2"},
        {"type": "REDEEM", "asset": "", "usdcSize": "6.6", "conditionId": "cc2"},
    ]
    with patch("src.config.settings.settings") as st, \
         patch("src.market.polymarket_data.get_activity", return_value=act):
        st.proxy_wallet_address = "0xw"
        realized, _ = _executed_realized_pnl(_closed)

    assert realized > 0                       # NOT −$26
    assert _pending_expected_gain(_pending) > 0   # pending shown as expected plus
