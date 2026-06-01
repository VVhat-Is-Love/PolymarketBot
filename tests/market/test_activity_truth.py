"""G4-19: /activity as the single source of truth for fill + PnL."""
from src.market.polymarket_data import (
    realized_pnl_by_token,
    buy_shares_by_token,
    redeemed_condition_ids,
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
