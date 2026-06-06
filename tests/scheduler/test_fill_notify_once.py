"""G4-27: the "✅ Исполнен" alert fires once per position (on the fill transition),
never on reconcile's per-cycle re-verification."""
from datetime import datetime
from unittest.mock import patch, MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Base, LiveTrade


def _session_factory():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)


def _activity(token, shares):
    return [{
        "type": "TRADE", "side": "BUY", "asset": token,
        "size": str(shares), "usdcSize": "6.0", "conditionId": "c",
    }]


def _run_reconcile(Session, activity):
    notifier = MagicMock()
    with patch("src.scheduler.get_session", Session), \
         patch("src.market.polymarket_data.get_activity_checked", return_value=(True, activity)), \
         patch("src.notifications.telegram.get_notifier", return_value=notifier), \
         patch("src.market.order_executor.get_order_status", return_value="open"), \
         patch("src.market.order_executor.cancel_order", return_value=True), \
         patch("src.config.settings.settings") as st:
        st.proxy_wallet_address = "0xwallet"
        st.order_timeout_minutes = 5
        from src.scheduler import _job_reconcile_orders
        _job_reconcile_orders()
    return notifier


def test_fills_and_notifies_once_on_transition():
    Session = _session_factory()
    s = Session()
    s.add(LiveTrade(
        market_id="m", bin_label="80-81F", status="pending", order_id="0xabc",
        token_id="tok1", size_shares=6.0, target_price=0.9,
        strategy_name="tail_no", city="Seattle", placed_at=datetime.utcnow(),
    ))
    s.commit(); s.close()

    notifier = _run_reconcile(Session, _activity("tok1", 6.0))

    assert notifier.send.call_count == 1
    s = Session(); row = s.query(LiveTrade).first()
    assert row.status == "filled"
    assert row.fill_notified is True


def test_already_notified_row_fills_without_realert():
    """A row reset to open after a prior fill (fill_notified=True) must NOT re-announce."""
    Session = _session_factory()
    s = Session()
    s.add(LiveTrade(
        market_id="m", bin_label="80-81F", status="open", order_id="0xabc",
        token_id="tok1", size_shares=6.0, target_price=0.9,
        strategy_name="tail_no", city="Dallas", placed_at=datetime.utcnow(),
        fill_notified=True,
    ))
    s.commit(); s.close()

    notifier = _run_reconcile(Session, _activity("tok1", 6.0))

    assert notifier.send.call_count == 0          # no re-announce
    s = Session(); row = s.query(LiveTrade).first()
    assert row.status == "filled"                 # still reconciled to filled


def test_two_cycles_only_one_alert():
    """Acceptance: reconcile runs every cycle; the position alerts exactly once."""
    Session = _session_factory()
    s = Session()
    s.add(LiveTrade(
        market_id="m", bin_label="92-93F", status="pending", order_id="0xdef",
        token_id="tok2", size_shares=6.0, target_price=0.9,
        strategy_name="tail_no", city="Dallas", placed_at=datetime.utcnow(),
    ))
    s.commit(); s.close()

    n1 = _run_reconcile(Session, _activity("tok2", 6.0))
    # Simulate the row being re-opened (e.g. a transient empty-/activity demotion)
    s = Session(); row = s.query(LiveTrade).first(); row.status = "open"; s.commit(); s.close()
    n2 = _run_reconcile(Session, _activity("tok2", 6.0))

    assert n1.send.call_count == 1
    assert n2.send.call_count == 0                # second cycle: silent
