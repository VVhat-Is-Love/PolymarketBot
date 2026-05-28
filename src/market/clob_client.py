import time
import requests
from loguru import logger

_CLOB_HOST = "https://clob.polymarket.com"
_REQUEST_DELAY = 0.1  # 10 req/s — conservative, CLOB allows ~50 req/s

_CLOB_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; polymarket-bot/0.1)",
}


def _try_py_clob_client(token_id: str) -> dict | None:
    """Attempt to use the official py-clob-client SDK (read-only, no credentials)."""
    try:
        from py_clob_client.client import ClobClient as _PyClob  # type: ignore[import]

        clob = _PyClob(host=_CLOB_HOST)
        book = clob.get_order_book(token_id)

        bids = book.bids or []
        asks = book.asks or []

        best_bid = float(bids[0].price) if bids else None
        best_bid_size = float(bids[0].size) if bids else None
        best_ask = float(asks[0].price) if asks else None
        best_ask_size = float(asks[0].size) if asks else None

        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else None
        spread = (best_ask - best_bid) if best_bid and best_ask else None

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "best_bid_size": best_bid_size,
            "best_ask_size": best_ask_size,
            "mid": mid,
            "spread": spread,
            "price_yes": best_ask,
            "price_no": (1.0 - best_bid) if best_bid is not None else None,
        }
    except Exception as e:
        logger.debug(f"py-clob-client failed for {token_id[:12]}…: {e} — falling back to HTTP")
        return None


class BookUnavailable(Exception):
    """Raised when CLOB returns 400 — market exists but has no active book."""


def _parse_book_response(book: dict) -> dict:
    bids: list[dict] = book.get("bids") or []
    asks: list[dict] = book.get("asks") or []

    best_bid = float(bids[0]["price"]) if bids else None
    best_bid_size = float(bids[0]["size"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None
    best_ask_size = float(asks[0]["size"]) if asks else None

    mid = (best_bid + best_ask) / 2 if best_bid and best_ask else None
    spread = (best_ask - best_bid) if best_bid and best_ask else None

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "best_bid_size": best_bid_size,
        "best_ask_size": best_ask_size,
        "mid": mid,
        "spread": spread,
        "price_yes": best_ask,
        "price_no": (1.0 - best_bid) if best_bid is not None else None,
    }


def _fallback_http(token_id: str) -> dict | None:
    """Plain HTTP fallback in case py-clob-client has issues."""
    for attempt in range(2):
        try:
            resp = requests.get(
                f"{_CLOB_HOST}/books",
                params={"token_id": token_id},
                headers=_CLOB_HEADERS,
                timeout=20,
            )
            if resp.status_code == 400:
                raise BookUnavailable(token_id)
            if resp.status_code == 403:
                logger.warning(f"CLOB 403 for {token_id[:12]}… — rate-limited or IP-blocked, skipping")
                return None
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                logger.warning(f"CLOB 429 — sleeping {retry_after}s before retry")
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return _parse_book_response(resp.json())
        except BookUnavailable:
            raise
        except Exception as e:
            logger.error(f"HTTP request failed for token {token_id[:12]}…: {e}")
            return None
    return None


def get_book_snapshot(token_id: str) -> dict | None:
    """
    Returns order book snapshot for a YES token.
    Tries py-clob-client first, falls back to plain HTTP.
    Raises BookUnavailable if CLOB returned 400 (closed/invalid market).
    Caller is responsible for rate-limiting (sleep after each call).
    """
    result = _try_py_clob_client(token_id)
    if result is None:
        result = _fallback_http(token_id)
    return result


def get_no_ask_prices(token_ids: list[str]) -> dict[str, float | None]:
    """
    Fetch the best ask for each NO token from the CLOB order book.
    Returns {token_id: best_ask} — None when book is empty or unavailable.
    Sequential with _REQUEST_DELAY between calls.
    """
    result: dict[str, float | None] = {}
    for token_id in token_ids:
        if not token_id:
            continue
        try:
            snap = _fallback_http(token_id)
            result[token_id] = snap.get("best_ask") if snap else None
        except BookUnavailable:
            result[token_id] = None
        except Exception as exc:
            logger.debug(f"[clob_client] NO ask fetch {token_id[:12]}…: {exc}")
            result[token_id] = None
        time.sleep(_REQUEST_DELAY)
    return result
