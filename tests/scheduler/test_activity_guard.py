"""G4-28: an empty/failed /activity must NOT demote real positions to not_filled.

Empty == data-api unavailable (timeout/error returns []), NOT proof that no BUY
exists. The reconcile/audit/import jobs must defer on empty and change no status.
"""
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Base, LiveTrade


def _session_factory():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)


def _add_open_position(Session, status="filled"):
    s = Session()
    s.add(LiveTrade(
        market_id="m1", bin_label="80-81F", status=status, order_id="0xabc",
        token_id="tok1", size_shares=6.0, stake_usd=6.0, group_id="g1",
        city="Seattle", strategy_name="tail_no",
        reconciled_with_polymarket=False,
        placed_at=datetime.utcnow() - timedelta(hours=5),  # past any grace window
    ))
    s.commit(); s.close()


def _run_reconcile_pnl(Session, activity):
    with patch("src.scheduler.get_session", Session), \
         patch("src.market.polymarket_data.get_activity", return_value=activity), \
         patch("src.scheduler.get_notifier", return_value=MagicMock()), \
         patch("src.scheduler._gamma") as gamma, \
         patch("src.config.settings.settings") as st:
        st.proxy_wallet_address = "0xwallet"
        st.order_timeout_minutes = 5
        gamma.is_resolved_event.return_value = False  # not resolved → would stay open
        from src.scheduler import _job_reconcile_pnl
        _job_reconcile_pnl()


def test_empty_activity_does_not_demote_to_not_filled():
    Session = _session_factory()
    _add_open_position(Session, status="filled")

    _run_reconcile_pnl(Session, [])           # /activity unavailable

    s = Session(); row = s.query(LiveTrade).first()
    assert row.status == "filled"              # UNCHANGED — not wiped
    assert row.reconciled_with_polymarket is False


def test_real_data_without_buy_still_marks_not_filled():
    """Control: /activity returned data, but no BUY for this token → genuine not_filled."""
    Session = _session_factory()
    _add_open_position(Session, status="filled")

    # Non-empty activity, but a BUY for a DIFFERENT token only
    other = [{"type": "TRADE", "side": "BUY", "asset": "OTHER",
              "size": "6.0", "usdcSize": "6.0", "conditionId": "c"}]
    _run_reconcile_pnl(Session, other)

    s = Session(); row = s.query(LiveTrade).first()
    assert row.status == "not_filled"          # correctly demoted (real phantom)


def test_audit_empty_activity_no_status_change():
    Session = _session_factory()
    # audit scans resolved/pending_redeem/not_filled
    s = Session()
    s.add(LiveTrade(
        market_id="m", bin_label="b", status="pending_redeem", order_id="0x",
        token_id="tokA", size_shares=6.0, stake_usd=6.0, group_id="g",
        condition_id="cidA", city="Dallas", strategy_name="tail_no",
        placed_at=datetime.utcnow(),
    ))
    s.commit(); s.close()

    with patch("src.scheduler.get_session", Session), \
         patch("src.market.polymarket_data.get_activity", return_value=[]), \
         patch("src.config.settings.settings") as st:
        st.proxy_wallet_address = "0xwallet"
        from src.scheduler import _job_audit_activity
        _job_audit_activity()

    s = Session(); row = s.query(LiveTrade).first()
    assert row.status == "pending_redeem"      # untouched
