"""
CLOB order-book helpers.

Endpoint contract (confirmed against Polymarket CLOB API):
  GET  /book?token_id=<id>          — single token, always works
  POST /books   body=[{"token_id": "..."}]  — batch, one request for N tokens
  GET  /books?token_id=...          — 400 "Invalid payload" (wrong method)

We use:
  • POST /books for batch YES-ask fetches (one HTTP round-trip per group)
  • GET  /book  for single-token fallback + legacy callers
"""
import time

import requests
from loguru import logger

_CLOB_HOST = "https://clob.polymarket.com"

# Rate-limiting: ~6–7 req/s (well under CLOB limit of ~50/s)
_REQUEST_DELAY = 0.15

# Connect + read timeouts (seconds)
_TIMEOUT = (5, 15)  # (connect, read)
_RETRIES = 3        # attempts before giving up on a single token
_RETRY_SLEEP = 2    # seconds between retries on timeout/connect errors

_CLOB_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; polymarket-bot/0.1)",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_book_response(book: dict) -> dict:
    """Parse a single order-book dict into our canonical snapshot format.

    Polymarket CLOB returns asks sorted HIGH→LOW (resting orders at 0.99+ come
    first).  Best ask = MINIMUM of the asks array; best bid = MAXIMUM of bids.
    Using min/max is robust regardless of sort direction.
    """
    bids: list[dict] = book.get("bids") or []
    asks: list[dict] = book.get("asks") or []

    # --- best ask: lowest ask price (cheapest for a buyer) ---
    if asks:
        best_ask_entry = min(asks, key=lambda a: float(a["price"]))
        best_ask = float(best_ask_entry["price"])
        best_ask_size = float(best_ask_entry.get("size", 0) or 0)
    else:
        best_ask = None
        best_ask_size = None

    # --- best bid: highest bid price (most a seller can get) ---
    if bids:
        best_bid_entry = max(bids, key=lambda b: float(b["price"]))
        best_bid = float(best_bid_entry["price"])
        best_bid_size = float(best_bid_entry.get("size", 0) or 0)
    else:
        best_bid = None
        best_bid_size = None

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


class BookUnavailable(Exception):
    """Raised when CLOB says the market exists but has no active order book."""


def _get_single_book(token_id: str) -> dict | None:
    """
    GET /book?token_id=<id>  — single token, with timeout retries.
    Returns parsed snapshot dict, or None on failure.
    Raises BookUnavailable when the market has no active book (HTTP 400).
    """
    for attempt in range(_RETRIES):
        try:
            resp = requests.get(
                f"{_CLOB_HOST}/book",
                params={"token_id": token_id},
                headers=_CLOB_HEADERS,
                timeout=_TIMEOUT,
            )
            if resp.status_code == 400:
                raise BookUnavailable(token_id)
            if resp.status_code == 403:
                logger.warning(
                    f"[clob] GET /book 403 token={token_id[:16]}… — IP-blocked or rate-limited"
                )
                return None
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                logger.warning(f"[clob] GET /book 429 — sleeping {retry_after}s")
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            parsed = _parse_book_response(resp.json())
            ask = parsed.get("best_ask")
            if ask is not None and ask > 0.95:
                raw = resp.json()
                logger.warning(
                    f"[clob_parse] GET /book suspicious best_ask={ask:.4f} "
                    f"token={token_id[:20]}… "
                    f"asks[:4]={[(a.get('price'), a.get('size')) for a in (raw.get('asks') or [])[:4]]}"
                )
            return parsed

        except BookUnavailable:
            raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < _RETRIES - 1:
                logger.warning(
                    f"[clob] GET /book timeout/connect token={token_id[:16]}… "
                    f"attempt {attempt + 1}/{_RETRIES}: {exc} — retrying in {_RETRY_SLEEP}s"
                )
                time.sleep(_RETRY_SLEEP)
                continue
            logger.warning(
                f"[clob] GET /book gave up after {_RETRIES} attempts "
                f"token={token_id[:16]}…: {exc}"
            )
            return None
        except Exception as exc:
            logger.error(f"[clob] GET /book unexpected error token={token_id[:16]}…: {exc}")
            return None

    return None


def _post_batch_books(
    token_ids: list[str],
    *,
    log_raw: bool = False,
) -> list[dict | None]:
    """
    POST /books  body=[{"token_id": ...}, ...]
    Returns list of parsed snapshots in the same order as token_ids.
    None entries for tokens whose book could not be fetched.

    log_raw=True logs the raw HTTP response at INFO level (diagnostics).
    """
    if not token_ids:
        return []

    body = [{"token_id": tid} for tid in token_ids]
    for attempt in range(_RETRIES):
        try:
            resp = requests.post(
                f"{_CLOB_HOST}/books",
                json=body,
                headers={**_CLOB_HEADERS, "Content-Type": "application/json"},
                timeout=(5, 30),
            )
            if log_raw:
                logger.info(
                    f"[clob_diag] POST /books n={len(token_ids)} "
                    f"HTTP={resp.status_code} "
                    f"body={resp.text[:800]}"
                )
            if resp.status_code != 200:
                logger.warning(
                    f"[clob] POST /books HTTP {resp.status_code}: {resp.text[:300]}"
                )
                return [None] * len(token_ids)

            data = resp.json()
            if not isinstance(data, list):
                logger.warning(
                    f"[clob] POST /books unexpected response type {type(data).__name__}: "
                    f"{str(data)[:200]}"
                )
                return [None] * len(token_ids)

            result: list[dict | None] = []
            for i, book in enumerate(data):
                if isinstance(book, dict) and book:
                    try:
                        parsed = _parse_book_response(book)
                        # Diagnostic: log raw asks when best_ask looks wrong (>0.5 is odd
                        # for most YES tokens; 0.99 for all bins is the known bad pattern)
                        if log_raw or (parsed.get("best_ask") or 0) > 0.95:
                            raw_asks = [
                                {"p": a.get("price"), "s": a.get("size")}
                                for a in (book.get("asks") or [])[:6]
                            ]
                            raw_bids = [
                                {"p": b.get("price"), "s": b.get("size")}
                                for b in (book.get("bids") or [])[:3]
                            ]
                            tok = token_ids[i] if i < len(token_ids) else "?"
                            logger.info(
                                f"[clob_parse] token={tok[:20]}… "
                                f"best_ask={parsed.get('best_ask')} "
                                f"best_bid={parsed.get('best_bid')} "
                                f"asks[:6]={raw_asks} "
                                f"bids[:3]={raw_bids}"
                            )
                        result.append(parsed)
                    except Exception as _exc:
                        logger.warning(f"[clob] _parse_book_response failed idx={i}: {_exc}")
                        result.append(None)
                else:
                    result.append(None)

            # Pad if server returned fewer items than requested
            while len(result) < len(token_ids):
                result.append(None)

            return result

        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < _RETRIES - 1:
                logger.warning(
                    f"[clob] POST /books timeout/connect attempt {attempt + 1}/{_RETRIES}: "
                    f"{exc} — retrying in {_RETRY_SLEEP}s"
                )
                time.sleep(_RETRY_SLEEP)
                continue
            logger.warning(f"[clob] POST /books gave up after {_RETRIES} attempts: {exc}")
            return [None] * len(token_ids)
        except Exception as exc:
            logger.error(f"[clob] POST /books unexpected error: {exc}")
            return [None] * len(token_ids)

    return [None] * len(token_ids)


# ---------------------------------------------------------------------------
# Legacy single-token helper (used by get_book_snapshot + get_no_ask_prices)
# ---------------------------------------------------------------------------

def _fallback_http(token_id: str) -> dict | None:
    """Single-token book fetch via GET /book (fixed endpoint, with retries)."""
    return _get_single_book(token_id)


def _try_py_clob_client(token_id: str) -> dict | None:
    """Attempt the official py-clob-client SDK (read-only). Falls back on any error."""
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
    except Exception as exc:
        logger.debug(
            f"[clob] py-clob-client failed for {token_id[:16]}…: {exc} — using HTTP"
        )
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_book_snapshot(token_id: str) -> dict | None:
    """
    Order-book snapshot for a single YES token.
    Tries py-clob-client, falls back to GET /book.
    Raises BookUnavailable if the market has no active book.
    """
    result = _try_py_clob_client(token_id)
    if result is None:
        result = _get_single_book(token_id)
    return result


def get_no_ask_prices(token_ids: list[str]) -> dict[str, float | None]:
    """
    Best ask for each NO token via GET /book (sequential with rate-limit delay).
    Returns {token_id: best_ask} — None when unavailable.
    """
    result: dict[str, float | None] = {}
    for token_id in token_ids:
        if not token_id:
            continue
        try:
            snap = _get_single_book(token_id)
            result[token_id] = snap.get("best_ask") if snap else None
        except BookUnavailable:
            result[token_id] = None
        except Exception as exc:
            logger.debug(f"[clob] NO ask fetch {token_id[:16]}…: {exc}")
            result[token_id] = None
        time.sleep(_REQUEST_DELAY)
    return result


def get_yes_ask_prices(
    token_ids: list[str],
    *,
    log_raw_for: str | None = None,
) -> dict[str, float | None]:
    """
    Best ask for YES tokens — single POST /books round-trip for all tokens.

    log_raw_for: unused (kept for API compat); raw response is always logged
    at INFO when the batch call is the first call for a group (controlled by
    the caller passing the parameter).

    Falls back to individual GET /book calls if the batch fails completely.
    Returns {token_id: best_ask | None}.
    """
    if not token_ids:
        return {}

    clean = [tid for tid in token_ids if tid]
    # Log raw response when diagnostics are requested (log_raw_for!=None triggers it)
    do_log_raw = log_raw_for is not None
    books = _post_batch_books(clean, log_raw=do_log_raw)

    result: dict[str, float | None] = {}
    all_failed = all(b is None for b in books)

    if all_failed and clean:
        # Batch totally failed — fall back to sequential GET /book
        logger.warning(
            "[clob] POST /books returned all-None — falling back to GET /book per token"
        )
        for token_id in clean:
            try:
                snap = _get_single_book(token_id)
                ask = snap.get("best_ask") if snap else None
                logger.info(
                    f"[clob] YES ask fallback GET /book "
                    f"token={token_id[:16]}… ask={ask}"
                )
                result[token_id] = ask
            except BookUnavailable:
                result[token_id] = None
            except Exception as exc:
                logger.warning(f"[clob] YES ask fallback failed {token_id[:16]}…: {exc}")
                result[token_id] = None
            time.sleep(_REQUEST_DELAY)
    else:
        for token_id, snap in zip(clean, books):
            ask = snap.get("best_ask") if snap else None
            logger.debug(f"[clob] YES ask batch token={token_id[:16]}… ask={ask}")
            result[token_id] = ask

    # Tokens that were empty strings → None
    for tid in token_ids:
        if not tid:
            result[tid] = None

    return result
