"""Startup PnL report: numerator and denominator from the SAME executed set,
realized PnL from /activity, |loss| never exceeds stake (fixes −$9.19 @ $6.02)."""
from types import SimpleNamespace
from unittest.mock import patch

from src.main import _executed_realized_pnl


def _t(token, stake, pnl=None, status="resolved"):
    return SimpleNamespace(token_id=token, stake_usd=stake, pnl_usd=pnl, status=status)


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
