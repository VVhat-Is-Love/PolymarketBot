"""Tests for order_executor — all network calls mocked."""
import pytest
from unittest.mock import MagicMock, patch


def _mock_risk_ok():
    rm = MagicMock()
    rm.can_place_order.return_value = (True, "ok")
    return rm


def _mock_risk_blocked(reason="blocked"):
    rm = MagicMock()
    rm.can_place_order.return_value = (False, reason)
    return rm


# ---------------------------------------------------------------------------
# place_limit_order
# ---------------------------------------------------------------------------

def test_place_limit_order_returns_order_id_on_success():
    mock_client = MagicMock()
    mock_client.create_and_post_order.return_value = {"orderID": "ord-123"}

    with patch("src.market.order_executor._get_client", return_value=mock_client), \
         patch("src.market.order_executor.risk_manager", _mock_risk_ok()):
        from src.market.order_executor import place_limit_order
        result = place_limit_order("m1", "tok1", "BUY", 0.40, 5.0)

    assert result == "ord-123"


def test_place_limit_order_blocked_by_risk():
    with patch("src.market.order_executor.risk_manager", _mock_risk_blocked("Daily loss limit")):
        from src.market.order_executor import place_limit_order
        result = place_limit_order("m1", "tok1", "BUY", 0.40, 5.0)

    assert result is None


def test_place_limit_order_returns_none_on_api_error():
    mock_client = MagicMock()
    mock_client.create_and_post_order.side_effect = Exception("network error")

    with patch("src.market.order_executor._get_client", return_value=mock_client), \
         patch("src.market.order_executor.risk_manager", _mock_risk_ok()), \
         patch("src.market.order_executor.time.sleep"):  # skip backoff delays
        from src.market.order_executor import place_limit_order
        result = place_limit_order("m1", "tok1", "BUY", 0.40, 5.0)

    assert result is None


# ---------------------------------------------------------------------------
# G4-25: error classification + verify-before-retry on ambiguous placement
# ---------------------------------------------------------------------------

def _exc_with_status(code):
    e = Exception("boom")
    e.status_code = code
    return e


@pytest.mark.parametrize("code,expected", [
    (None, "ambiguous"),   # request never completed (the Dallas case)
    (400, "clean"),        # explicit reject — definitely not placed
    (401, "clean"),
    (403, "clean"),
    (422, "clean"),
    (408, "ambiguous"),    # request timeout
    (429, "ambiguous"),    # rate-limited
    (500, "ambiguous"),
    (503, "ambiguous"),
])
def test_place_error_kind_by_status(code, expected):
    from src.market.order_executor import _place_error_kind
    assert _place_error_kind(_exc_with_status(code)) == expected


def test_place_error_kind_connection_and_timeout_are_ambiguous():
    import requests
    from src.market.order_executor import _place_error_kind
    assert _place_error_kind(requests.ConnectionError()) == "ambiguous"
    assert _place_error_kind(requests.Timeout()) == "ambiguous"


def test_ambiguous_error_landed_records_without_retry():
    """status=None but the order executed on-chain → record, DO NOT retry."""
    mock_client = MagicMock()
    mock_client.create_and_post_order.side_effect = _exc_with_status(None)

    with patch("src.market.order_executor._get_client", return_value=mock_client), \
         patch("src.market.order_executor.risk_manager", _mock_risk_ok()), \
         patch("src.market.order_executor._verify_order_placed",
               return_value=("landed", "real-ord-7")) as vrf, \
         patch("src.market.order_executor.time.sleep") as slp:
        from src.market.order_executor import place_limit_order
        result = place_limit_order("m1", "tok1", "BUY", 0.40, 6.0)

    assert result == "real-ord-7"
    vrf.assert_called_once()                       # verified via /activity
    assert mock_client.create_and_post_order.call_count == 1   # NO retry
    slp.assert_not_called()


def test_ambiguous_error_unknown_skips_retry_to_avoid_double():
    """Verification couldn't run → do not blindly retry (avoid double-fill)."""
    mock_client = MagicMock()
    mock_client.create_and_post_order.side_effect = _exc_with_status(None)

    with patch("src.market.order_executor._get_client", return_value=mock_client), \
         patch("src.market.order_executor.risk_manager", _mock_risk_ok()), \
         patch("src.market.order_executor._verify_order_placed",
               return_value=("unknown", None)), \
         patch("src.market.order_executor.time.sleep"):
        from src.market.order_executor import place_limit_order
        result = place_limit_order("m1", "tok1", "BUY", 0.40, 6.0)

    assert result is None
    assert mock_client.create_and_post_order.call_count == 1   # NO retry


def test_ambiguous_error_absent_retries_safely():
    """/activity readable, no BUY → safe to retry; still ends None if all fail."""
    mock_client = MagicMock()
    mock_client.create_and_post_order.side_effect = _exc_with_status(None)

    with patch("src.market.order_executor._get_client", return_value=mock_client), \
         patch("src.market.order_executor.risk_manager", _mock_risk_ok()), \
         patch("src.market.order_executor._verify_order_placed",
               return_value=("absent", None)), \
         patch("src.market.order_executor.time.sleep"):
        from src.market.order_executor import place_limit_order, _BACKOFF
        result = place_limit_order("m1", "tok1", "BUY", 0.40, 6.0)

    assert result is None
    assert mock_client.create_and_post_order.call_count == len(_BACKOFF)  # retried


def test_clean_reject_retries_without_verifying():
    """A 4xx reject is unambiguous (not placed): retry, never call /activity."""
    mock_client = MagicMock()
    mock_client.create_and_post_order.side_effect = _exc_with_status(400)

    with patch("src.market.order_executor._get_client", return_value=mock_client), \
         patch("src.market.order_executor.risk_manager", _mock_risk_ok()), \
         patch("src.market.order_executor._verify_order_placed") as vrf, \
         patch("src.market.order_executor.time.sleep"):
        from src.market.order_executor import place_limit_order, _BACKOFF
        result = place_limit_order("m1", "tok1", "BUY", 0.40, 6.0)

    assert result is None
    vrf.assert_not_called()                                  # no verification needed
    assert mock_client.create_and_post_order.call_count == len(_BACKOFF)


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------

def test_cancel_order_returns_true_on_success():
    mock_client = MagicMock()
    mock_client.cancel.return_value = {"success": True}

    with patch("src.market.order_executor._get_client", return_value=mock_client):
        from src.market.order_executor import cancel_order
        assert cancel_order("ord-123") is True


def test_cancel_order_returns_false_on_error():
    mock_client = MagicMock()
    mock_client.cancel_order.side_effect = Exception("api down")  # V2 method name

    with patch("src.market.order_executor._get_client", return_value=mock_client), \
         patch("src.market.order_executor.time.sleep"):
        from src.market.order_executor import cancel_order
        assert cancel_order("ord-123") is False


# ---------------------------------------------------------------------------
# get_order_status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("OPEN", "open"),
    ("LIVE", "open"),
    ("FILLED", "filled"),
    ("MATCHED", "filled"),
    ("CANCELLED", "cancelled"),
    ("CANCELED", "cancelled"),
    ("UNKNOWN_THING", "unknown"),
])
def test_get_order_status_maps_correctly(raw, expected):
    mock_client = MagicMock()
    mock_client.get_order.return_value = {"status": raw}

    with patch("src.market.order_executor._get_client", return_value=mock_client):
        from src.market.order_executor import get_order_status
        assert get_order_status("ord-x") == expected


def test_get_order_status_returns_unknown_on_error():
    mock_client = MagicMock()
    mock_client.get_order.side_effect = Exception("timeout")

    with patch("src.market.order_executor._get_client", return_value=mock_client), \
         patch("src.market.order_executor.time.sleep"):
        from src.market.order_executor import get_order_status
        assert get_order_status("ord-x") == "unknown"


# ---------------------------------------------------------------------------
# get_open_orders
# ---------------------------------------------------------------------------

def test_get_open_orders_parses_list():
    raw = [
        {"id": "a1", "asset_id": "m1", "side": "BUY", "price": "0.40", "original_size": "5.0"},
        {"id": "a2", "asset_id": "m2", "side": "BUY", "price": "0.30", "original_size": "3.0"},
    ]
    mock_client = MagicMock()
    mock_client.get_open_orders.return_value = raw  # V2 method name

    with patch("src.market.order_executor._get_client", return_value=mock_client):
        from src.market.order_executor import get_open_orders
        orders = get_open_orders()

    assert len(orders) == 2
    assert orders[0].order_id == "a1"
    assert orders[1].price == pytest.approx(0.30)


def test_get_open_orders_returns_empty_on_error():
    mock_client = MagicMock()
    mock_client.get_open_orders.side_effect = Exception("network")  # V2 method name

    with patch("src.market.order_executor._get_client", return_value=mock_client), \
         patch("src.market.order_executor.time.sleep"):
        from src.market.order_executor import get_open_orders
        assert get_open_orders() == []
