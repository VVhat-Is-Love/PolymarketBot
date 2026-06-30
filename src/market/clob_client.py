"""
CLOB order-book helpers.

Endpoint contract (confirmed against Polymarket CLOB API):
  GET  /book?token_id=<id>          — single token, always works
  POST /books   body=[{"token_id": "..."}]  — batch; response keyed by asset_id
  GET  /books?token_id=...          — 400 "Invalid payload" (wrong method)

Ask/bid conventions:
  • Polymarket returns asks sorted HIGH→LOW (resting orders first)
  • Best ask = MINIMUM price with real size (< _NO_LIQ_THRESHOLD)
  • Best bid = MAXIMUM price with real size
  • Asks ≥ _NO_LIQ_THRESHOLD (0.97) are market-maker stubs with no real liquidity
"""
import time
from typing import Optional

import requests
from loguru import logger

_CLOB_HOST = "https://clob.polymarket.com"

_REQUEST_DELAY = 0.15           # seconds between sequential GET /book calls
_TIMEOUT = (5, 15)              # (connect, read) for single-token requests
_BATCH_TIMEOUT = (5, 30)        # longer read timeout for batch POST
_RETRIES = 3
_RETRY_SLEEP = 2

# Asks at or above this price have no real liquidity (stub/resting orders).
# Return None (no_liquidity) instead of reporting them as best_ask.
_NO_LIQ_THRESHOLD = 0.97

_CLOB_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (compatible; polymarket-bot/0.1)",
}


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

def _parse_book_response(
    book: dict,
    *,
    token_id: str = "",
    log_asks: bool = False,
) -> dict:
    """
    Parse one CLOB order-book dict → canonical snapshot.

    Filtering rules:
      • Only asks/bids with nonzero size count as real orders.
      • Asks ≥ _NO_LIQ_THRESHOLD (0.97) are stub/resting orders — ignored.
        If no real asks remain, best_ask = None (caller sees no_liquidity).
      • Best ask  = min real ask price (cheapest for a buyer).
      • Best bid  = max real bid price (highest a seller can achieve).
    """
    bids: list[dict] = book.get("bids") or []
    asks: list[dict] = book.get("asks") or []
    asset_id = book.get("asset_id") or token_id

    # Real asks: nonzero size AND below the no-liquidity threshold
    real_asks = []
    for a in asks:
        try:
            price = float(a["price"])
            size = float(a.get("size") or 0)
            if size > 0 and price < _NO_LIQ_THRESHOLD:
                real_asks.append((price, size))
        except (KeyError, ValueError):
            pass

    # Real bids: nonzero size
    real_bids = []
    for b in bids:
        try:
            price = float(b["price"])
            size = float(b.get("size") or 0)
            if size > 0:
                real_bids.append((price, size))
        except (KeyError, ValueError):
            pass

    best_ask: Optional[float] = min(real_asks, key=lambda t: t[0])[0] if real_asks else None
    best_ask_size: Optional[float] = min(real_asks, key=lambda t: t[0])[1] if real_asks else None

    best_bid: Optional[float] = max(real_bids, key=lambda t: t[0])[0] if real_bids else None
    best_bid_size: Optional[float] = max(real_bids, key=lambda t: t[0])[1] if real_bids else None

    mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
    spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else None

    # Diagnostic logging when ask looks wrong or explicitly requested
    stub_count = len([a for a in asks if float(a.get("price", 0)) >= _NO_LIQ_THRESHOLD])
    if log_asks or best_ask is None:
        raw_sample = [
            {"p": a.get("price"), "s": a.get("size")}
            for a in asks[:8]
        ]
        logger.info(
            f"[clob_parse] asset={asset_id[:20]}… "
            f"best_ask={best_ask} best_bid={best_bid} "
            f"real_asks={len(real_asks)} stub_asks={stub_count} "
            f"asks[:8]={raw_sample}"
        )

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "best_bid_size": best_bid_size,
        "best_ask_size": best_ask_size,
        "mid": mid,
        "spread": spread,
        "price_yes": best_ask,
        "price_no": (1.0 - best_bid) if best_bid is not None else None,
        "no_liquidity": best_ask is None,
        "asset_id": asset_id,
        "ask_levels": sorted(real_asks, key=lambda t: t[0]),  # [(price, size)] cheapest first
    }


class BookUnavailable(Exception):
    """Raised when CLOB says the market has no active order book (HTTP 400)."""


# ---------------------------------------------------------------------------
# Single-token fetch  (GET /book)
# ---------------------------------------------------------------------------

def _get_single_book(token_id: str, *, log_asks: bool = False) -> dict | None:
    """
    GET /book?token_id=<id> — with retries on timeout/connect errors.
    Returns parsed snapshot or None. Raises BookUnavailable on HTTP 400.
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
                logger.warning(f"[clob] GET /book 403 token={token_id[:16]}… — IP-blocked")
                return None
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                logger.warning(f"[clob] GET /book 429 — sleeping {retry_after}s")
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            parsed = _parse_book_response(
                resp.json(),
                token_id=token_id,
                log_asks=log_asks,
            )
            logger.debug(
                f"[clob] GET /book token={token_id[:16]}… "
                f"ask={parsed['best_ask']} no_liq={parsed['no_liquidity']}"
            )
            return parsed

        except BookUnavailable:
            raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < _RETRIES - 1:
                logger.warning(
                    f"[clob] GET /book timeout token={token_id[:16]}… "
                    f"attempt {attempt + 1}/{_RETRIES}: {exc} — retry in {_RETRY_SLEEP}s"
                )
                time.sleep(_RETRY_SLEEP)
                continue
            logger.warning(f"[clob] GET /book gave up token={token_id[:16]}…: {exc}")
            return None
        except Exception as exc:
            logger.error(f"[clob] GET /book error token={token_id[:16]}…: {exc}")
            return None

    return None


# ---------------------------------------------------------------------------
# Batch fetch  (POST /books)
# ---------------------------------------------------------------------------

def _post_batch_books(
    token_ids: list[str],
    *,
    log_raw: bool = False,
) -> dict[str, dict | None]:
    """
    POST /books  body=[{"token_id": ...}, ...]
    Returns {token_id: parsed_snapshot | None}.

    Mapping is done by asset_id in each response book (NOT by position),
    because the server is NOT guaranteed to return books in request order.
    Falls back to positional mapping if asset_id is absent.
    """
    if not token_ids:
        return {}

    body = [{"token_id": tid} for tid in token_ids]
    for attempt in range(_RETRIES):
        try:
            resp = requests.post(
                f"{_CLOB_HOST}/books",
                json=body,
                headers={**_CLOB_HEADERS, "Content-Type": "application/json"},
                timeout=_BATCH_TIMEOUT,
            )
            if log_raw:
                logger.info(
                    f"[clob_diag] POST /books n={len(token_ids)} "
                    f"HTTP={resp.status_code} body={resp.text[:1000]}"
                )
            if resp.status_code != 200:
                logger.warning(
                    f"[clob] POST /books HTTP {resp.status_code}: {resp.text[:300]}"
                )
                return {tid: None for tid in token_ids}

            data = resp.json()
            if not isinstance(data, list):
                logger.warning(
                    f"[clob] POST /books unexpected type {type(data).__name__}: "
                    f"{str(data)[:200]}"
                )
                return {tid: None for tid in token_ids}

            # ── Map by asset_id (canonical) ───────────────────────────────
            # Each book has asset_id == the YES token_id we requested.
            # This avoids index-shift bugs when the server reorders books.
            books_by_asset: dict[str, dict] = {}
            for raw_book in data:
                if not isinstance(raw_book, dict):
                    continue
                aid = str(raw_book.get("asset_id") or "").strip()
                if aid:
                    books_by_asset[aid] = raw_book

            result: dict[str, dict | None] = {}
            positional = list(data)  # fallback

            for i, tid in enumerate(token_ids):
                raw_book = books_by_asset.get(tid)
                if raw_book is None and i < len(positional):
                    # asset_id absent or mismatch → positional fallback with a warning
                    fb = positional[i]
                    if isinstance(fb, dict):
                        fb_aid = str(fb.get("asset_id") or "")
                        if fb_aid and fb_aid != tid:
                            logger.warning(
                                f"[clob] asset_id mismatch: requested={tid[:16]}… "
                                f"got={fb_aid[:16]}… — using None"
                            )
                            result[tid] = None
                            continue
                        raw_book = fb

                if raw_book is not None:
                    try:
                        parsed = _parse_book_response(
                            raw_book,
                            token_id=tid,
                            log_asks=log_raw,
                        )
                        result[tid] = parsed
                        logger.info(
                            f"[clob] batch YES ask "
                            f"token={tid[:16]}… "
                            f"ask={parsed['best_ask']} "
                            f"no_liq={parsed['no_liquidity']}"
                        )
                    except Exception as exc:
                        logger.warning(f"[clob] parse failed token={tid[:16]}…: {exc}")
                        result[tid] = None
                else:
                    result[tid] = None

            # Fill any token_ids not in result
            for tid in token_ids:
                result.setdefault(tid, None)

            return result

        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt < _RETRIES - 1:
                logger.warning(
                    f"[clob] POST /books timeout attempt {attempt + 1}/{_RETRIES}: "
                    f"{exc} — retry in {_RETRY_SLEEP}s"
                )
                time.sleep(_RETRY_SLEEP)
                continue
            logger.warning(f"[clob] POST /books gave up after {_RETRIES} attempts: {exc}")
            return {tid: None for tid in token_ids}
        except Exception as exc:
            logger.error(f"[clob] POST /books error: {exc}")
            return {tid: None for tid in token_ids}

    return {tid: None for tid in token_ids}


# ---------------------------------------------------------------------------
# Legacy wrappers
# ---------------------------------------------------------------------------

def _fallback_http(token_id: str) -> dict | None:
    """Single-token book via GET /book (backward-compat)."""
    return _get_single_book(token_id)


def _try_py_clob_client(token_id: str) -> dict | None:
    """Attempt the official py-clob-client SDK. Falls back on any error."""
    try:
        from py_clob_client.client import ClobClient as _PyClob  # type: ignore[import]

        clob = _PyClob(host=_CLOB_HOST)
        book = clob.get_order_book(token_id)

        raw_bids = [{"price": str(b.price), "size": str(b.size)} for b in (book.bids or [])]
        raw_asks = [{"price": str(a.price), "size": str(a.size)} for a in (book.asks or [])]

        return _parse_book_response(
            {"bids": raw_bids, "asks": raw_asks, "asset_id": token_id},
            token_id=token_id,
        )
    except Exception as exc:
        logger.debug(f"[clob] py-clob-client failed {token_id[:16]}…: {exc} — using HTTP")
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_book_snapshot(token_id: str) -> dict | None:
    """
    Order-book snapshot for a single YES token.
    Tries py-clob-client first, falls back to GET /book.
    Raises BookUnavailable if market has no active book.
    Returns dict with best_ask=None (no_liquidity=True) when book exists but
    has no real asks below _NO_LIQ_THRESHOLD.
    """
    result = _try_py_clob_client(token_id)
    if result is None:
        result = _get_single_book(token_id)
    return result


def get_no_ask_prices(token_ids: list[str]) -> dict[str, float | None]:
    """
    Best real ask for each NO token via GET /book (sequential).
    Returns {token_id: best_ask | None}.
    None means either no book or no_liquidity (all asks are stubs ≥ 0.97).
    """
    result: dict[str, float | None] = {}
    for token_id in token_ids:
        if not token_id:
            continue
        try:
            snap = _get_single_book(token_id)
            if snap and not snap.get("no_liquidity"):
                result[token_id] = snap.get("best_ask")
            else:
                result[token_id] = None
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
    Best real ask for YES tokens via POST /books (one round-trip).
    Mapping is by asset_id in the response — immune to server reordering.
    Returns {token_id: best_ask | None}.
    None means no book, no_liquidity, or failed fetch.

    Falls back to sequential GET /book if the batch returns all-None.
    """
    if not token_ids:
        return {}

    clean = [tid for tid in token_ids if tid]
    do_log_raw = log_raw_for is not None
    books_by_token = _post_batch_books(clean, log_raw=do_log_raw)

    all_failed = all(v is None for v in books_by_token.values())

    result: dict[str, float | None] = {}

    if all_failed and clean:
        logger.warning("[clob] POST /books all-None — falling back to GET /book per token")
        for token_id in clean:
            try:
                snap = _get_single_book(token_id, log_asks=(token_id == log_raw_for))
                if snap and not snap.get("no_liquidity"):
                    result[token_id] = snap.get("best_ask")
                else:
                    result[token_id] = None
                    if snap:
                        logger.info(f"[clob] YES ask no_liquidity token={token_id[:16]}…")
            except BookUnavailable:
                result[token_id] = None
            except Exception as exc:
                logger.warning(f"[clob] YES ask fallback failed {token_id[:16]}…: {exc}")
                result[token_id] = None
            time.sleep(_REQUEST_DELAY)
    else:
        for token_id, snap in books_by_token.items():
            if snap is None:
                result[token_id] = None
            elif snap.get("no_liquidity"):
                logger.info(
                    f"[clob] YES ask no_liquidity token={token_id[:16]}… "
                    f"(all asks ≥ {_NO_LIQ_THRESHOLD})"
                )
                result[token_id] = None
            else:
                result[token_id] = snap.get("best_ask")

    # Ensure every requested token_id (including empty-string) is in result
    for tid in token_ids:
        result.setdefault(tid, None)

    return result


def get_no_book_depth(token_id: str) -> dict | None:
    """
    Full order-book snapshot for one NO token, including all real ask levels.

    Returns a dict with:
      ask_levels : [(price, size), ...] sorted cheapest-first (stubs ≥ 0.97 excluded)
      best_ask   : cheapest ask price (or None if no real asks)
      best_bid   : highest bid price (or None)
      spread     : best_ask − best_bid (or None)
    Returns None when the book is unavailable or the call fails.
    Used for depth/capacity logging only — never gates order placement.
    """
    try:
        return _get_single_book(token_id)
    except BookUnavailable:
        return None
    except Exception as exc:
        logger.debug(f"[clob] depth fetch failed {token_id[:16]}…: {exc}")
        return None
