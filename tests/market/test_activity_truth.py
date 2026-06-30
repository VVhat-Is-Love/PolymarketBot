"""G4-19: /activity as the single source of truth for fill + PnL."""
from src.market.polymarket_data import (
    realized_pnl_by_token,
    buy_shares_by_token,
    redeemed_condition_ids,
    redeem_payout_by_condition,
    open_cost_by_token,
)


def _seoul_win():
    # Bought NO for $6.00, redeemed for $6.74 → +$0.74 win
    return [
        {"type": "TRADE", "side": "BUY", "asset": "seoul_no",
         "size": "6.45", "usdcSize": "6.00", "conditionId": "cid_seoul"},
        {"type": "REDEEM", "asset": "", "usdcSize": "6.74", "conditionId": "cid_seoul"},
    ]


def _real_loss():
    # Bought $3.00, never redeemed (lost) → −$3.00
    return [
        {"type": "TRADE", "side": "BUY", "asset": "tok_loss",
         "size": "3.2", "usdcSize": "3.00", "conditionId": "cid_loss"},
    ]


# ---------------------------------------------------------------------------
# buy_shares_by_token — proof of fill
# ---------------------------------------------------------------------------

def test_buy_shares_counts_only_buys():
    act = _seoul_win()
    assert buy_shares_by_token(act) == {"seoul_no": 6.45}


def test_buy_shares_phantom_has_no_entry():
    # Lucknow: no BUY in /activity → token absent → not filled
    act = _seoul_win()
    assert buy_shares_by_token(act).get("lucknow_no", 0.0) == 0.0


# ---------------------------------------------------------------------------
# redeemed_condition_ids — win signal
# ---------------------------------------------------------------------------

def test_redeemed_condition_ids():
    assert redeemed_condition_ids(_seoul_win()) == {"cid_seoul"}
    assert redeemed_condition_ids(_real_loss()) == set()


# ---------------------------------------------------------------------------
# realized_pnl_by_token — sign correctness
# ---------------------------------------------------------------------------

def test_seoul_win_is_positive():
    pnl = realized_pnl_by_token(_seoul_win())
    assert round(pnl["seoul_no"], 2) == 0.74   # +(payout − cost), NOT −6.03


def test_real_loss_is_negative_cost():
    pnl = realized_pnl_by_token(_real_loss())
    assert round(pnl["tok_loss"], 2) == -3.00  # BUY debit, no redeem credit


def test_phantom_token_absent_from_pnl():
    # No BUY → token never appears → caller treats as not_filled (excluded)
    pnl = realized_pnl_by_token(_seoul_win())
    assert "lucknow_no" not in pnl


# ---------------------------------------------------------------------------
# G4-23: NegRisk twin-index REDEEM rows must not double-count the payout
# ---------------------------------------------------------------------------

def _chicago_negrisk_double_redeem():
    # Bought NO @0.913 ($6.02 / 6.59 sh); resolution emits TWO REDEEM rows for
    # the SAME on-chain tx (yes-index + no-index) each carrying the gross $6.59.
    # Net truth = 6.59 − 6.02 = +$0.57, NOT 2×6.59 − 6.02 = +$7.16.
    return [
        {"type": "TRADE", "side": "BUY", "asset": "chi_no",
         "size": "6.59", "usdcSize": "6.02", "conditionId": "cid_chi"},
        {"type": "REDEEM", "asset": "", "usdcSize": "6.59",
         "conditionId": "cid_chi", "transactionHash": "0xabc"},
        {"type": "REDEEM", "asset": "", "usdcSize": "6.59",
         "conditionId": "cid_chi", "transactionHash": "0xabc"},
    ]


def test_negrisk_double_redeem_not_double_counted():
    pnl = realized_pnl_by_token(_chicago_negrisk_double_redeem())
    assert round(pnl["chi_no"], 2) == 0.57   # net, NOT +7.16


def test_negrisk_roi_within_yield_ceiling():
    # ROI must never exceed (1/entry − 1). entry ≈ 6.02/6.59 = 0.9135 → ceil ≈ 9.5%
    pnl = realized_pnl_by_token(_chicago_negrisk_double_redeem())
    cost, shares = 6.02, 6.59
    entry = cost / shares
    ceiling = (1.0 / entry - 1.0) * cost  # max $ profit
    assert pnl["chi_no"] <= ceiling + 1e-6


def test_redeem_payout_dedup_by_condition():
    # Two twin rows of one tx → single $6.59 payout, not $13.18
    payouts = redeem_payout_by_condition(_chicago_negrisk_double_redeem())
    assert round(payouts["cid_chi"], 2) == 6.59


def test_distinct_redeems_same_condition_both_counted():
    # Two SEPARATE redemptions (different tx) of the same cid are both real
    act = [
        {"type": "TRADE", "side": "BUY", "asset": "tok", "size": "10",
         "usdcSize": "5.00", "conditionId": "cid_x"},
        {"type": "REDEEM", "asset": "", "usdcSize": "3.00",
         "conditionId": "cid_x", "transactionHash": "0x1"},
        {"type": "REDEEM", "asset": "", "usdcSize": "3.00",
         "conditionId": "cid_x", "transactionHash": "0x2"},
    ]
    assert round(redeem_payout_by_condition(act)["cid_x"], 2) == 6.00


def test_single_redeem_unchanged():
    # Regression: the ordinary single-redeem win still nets +0.74
    pnl = realized_pnl_by_token(_seoul_win())
    assert round(pnl["seoul_no"], 2) == 0.74


# ---------------------------------------------------------------------------
# G4-25: open_cost_by_token — on-chain exposure for the token-cap double-guard
# ---------------------------------------------------------------------------

def test_open_cost_counts_buy():
    act = [{"type": "TRADE", "side": "BUY", "asset": "tok", "usdcSize": "6.00"}]
    assert round(open_cost_by_token(act)["tok"], 2) == 6.00


def test_open_cost_nets_sell_to_zero():
    # Fully sold position → no longer counts toward the cap (token absent)
    act = [
        {"type": "TRADE", "side": "BUY", "asset": "tok", "usdcSize": "6.00"},
        {"type": "TRADE", "side": "SELL", "asset": "tok", "usdcSize": "6.10"},
    ]
    assert "tok" not in open_cost_by_token(act)


def test_open_cost_partial_sell():
    act = [
        {"type": "TRADE", "side": "BUY", "asset": "tok", "usdcSize": "6.00"},
        {"type": "TRADE", "side": "SELL", "asset": "tok", "usdcSize": "2.00"},
    ]
    assert round(open_cost_by_token(act)["tok"], 2) == 4.00


def test_open_cost_failed_but_executed_order_is_counted():
    # The Dallas case: an order that errored on the client but executed on-chain
    # still shows a BUY → cap must see it even though the DB recorded nothing.
    act = [{"type": "TRADE", "side": "BUY", "asset": "dallas_no", "usdcSize": "6.00"}]
    assert open_cost_by_token(act).get("dallas_no", 0.0) == 6.00
