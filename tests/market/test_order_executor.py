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
    mock_client.cancel.side_effect = Exception("api down")

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
    mock_client.get_orders.return_value = raw

    with patch("src.market.order_executor._get_client", return_value=mock_client):
        from src.market.order_executor import get_open_orders
        orders = get_open_orders()

    assert len(orders) == 2
    assert orders[0].order_id == "a1"
    assert orders[1].price == pytest.approx(0.30)


def test_get_open_orders_returns_empty_on_error():
    mock_client = MagicMock()
    mock_client.get_orders.side_effect = Exception("network")

    with patch("src.market.order_executor._get_client", return_value=mock_client), \
         patch("src.market.order_executor.time.sleep"):
        from src.market.order_executor import get_open_orders
        assert get_open_orders() == []
