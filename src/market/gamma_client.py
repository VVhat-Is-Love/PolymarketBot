import json
import time
from datetime import datetime, timedelta
import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
)
from loguru import logger

GAMMA_BASE = "https://gamma-api.polymarket.com"
REQUEST_DELAY = 0.3  # stay well within 500 req/10s limit for /events

# G4-24: classify failures. A read-timeout means Gamma is ALIVE but slow — it
# must not trip the breaker on its own. A connection error / 5xx means Gamma is
# DOWN — trip faster. Separate counters with separate thresholds.
_HARD_FAILURE_THRESHOLD = 2   # connection error / 5xx → endpoint dead
_TIMEOUT_THRESHOLD = 5        # consecutive read-timeouts → sustained slowness
# Exponential cooldown: 10→20→40→60 min per consecutive breaker open; resets on success.
_COOLDOWN_MINUTES_BASE = 10
_COOLDOWN_MINUTES_MAX = 60

# Prices fetch: (connect=5s, read=30s) + 2 retries → ≤62s worst-case per group.
# Connect failures fast-fail at 5s (not accumulated into read-timeout budget).
_PRICES_CONNECT_TIMEOUT_S = 5
_PRICES_READ_TIMEOUT_S = 30
_PRICES_MAX_ATTEMPTS = 2      # was 4; with breaker this is enough signal

# Discovery has a hard wall-clock budget so one slow page can't hang the whole
# scheduler. Better to skip a cycle than block every other job behind it.
_DISCOVERY_BUDGET_S = 90.0    # total time allowed for one get_all_weather_events
_DISCOVERY_CONNECT_TIMEOUT_S = 3   # fast connect-fail for discovery pages
_DISCOVERY_TIMEOUT_S = 20.0   # per-attempt HTTP read timeout for discovery pages
_DISCOVERY_PAGE_RETRIES = 3   # in-cycle retries (with backoff) per page on timeout

# DISCOVERY-FIX: Gamma's ?q= keyword search stopped filtering server-side ~2026-06-19.
# Switch to tag-based discovery via /events/keyset (cursor pagination, no offset 403).
# tag_id is resolved dynamically via /tags/slug/{slug} so it doesn't need hardcoding.
_WEATHER_TAG_SLUG = "daily-temperature"    # GET /tags/slug/daily-temperature → {"id": "103040", ...}
_WEATHER_TAG_ID_FALLBACK = 103040          # "Daily Temperature" — verified 2026-06-19


class GammaClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "polymarket-bot/0.1",
        })
        self._consec_hard: int = 0       # connection/5xx in a row
        self._consec_timeout: int = 0    # read-timeouts in a row
        self._circuit_open_until: datetime | None = None
        self._open_count: int = 0        # consecutive opens; drives exponential cooldown
        # Reason the last fetch returned nothing, for funnel attribution:
        # "ok" | "breaker_open" | "timeout_budget" | "endpoint_error"
        self._last_fetch_status: str = "ok"
        self._last_fail_kind: str = ""

    @property
    def last_fetch_status(self) -> str:
        return self._last_fetch_status

    def _is_circuit_open(self) -> bool:
        if self._circuit_open_until is None:
            return False
        if datetime.now() >= self._circuit_open_until:
            self._circuit_open_until = None
            self._consec_hard = 0
            self._consec_timeout = 0
            # _open_count deliberately NOT reset here — only a successful fetch
            # proves Gamma recovered; expiry alone just means "try again".
            logger.info("Gamma circuit breaker: cooldown finished, resuming")
            return False
        return True

    @staticmethod
    def _classify(exc: BaseException) -> str:
        """timeout = alive-but-slow (read timeout / 429); hard = dead (connect/5xx)."""
        # ReadTimeout is a Timeout but NOT a connection failure → soft.
        if isinstance(exc, requests.exceptions.ReadTimeout):
            return "timeout"
        # ConnectTimeout/ConnectionError → endpoint unreachable → hard.
        if isinstance(exc, (requests.exceptions.ConnectTimeout, requests.ConnectionError)):
            return "hard"
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            code = exc.response.status_code
            if code in (500, 502, 503, 504):
                return "hard"
            if code == 429:
                return "timeout"  # rate-limited = slow, not dead
            return "other"  # 400/403/404 — not a health signal
        if isinstance(exc, requests.Timeout):
            return "timeout"  # generic/unknown timeout — treat as soft
        return "other"

    def _open_circuit(self, reason: str) -> None:
        self._open_count += 1
        cooldown_min = min(
            _COOLDOWN_MINUTES_BASE * (2 ** (self._open_count - 1)),
            _COOLDOWN_MINUTES_MAX,
        )
        self._circuit_open_until = datetime.now() + timedelta(minutes=cooldown_min)
        logger.warning(
            f"Gamma circuit breaker OPEN ({reason}) — "
            f"hard={self._consec_hard} timeout={self._consec_timeout} "
            f"open_count={self._open_count} cooldown={cooldown_min}min. "
            f"Pausing until {self._circuit_open_until:%H:%M:%S}"
        )

    def _record_failure(self, kind: str = "hard") -> None:
        """kind: 'timeout' (soft) or 'hard'. Only sustained failure opens breaker."""
        if kind == "timeout":
            self._consec_timeout += 1
            if self._consec_timeout >= _TIMEOUT_THRESHOLD:
                self._open_circuit(f"{self._consec_timeout} consecutive read-timeouts")
        elif kind == "hard":
            self._consec_hard += 1
            if self._consec_hard >= _HARD_FAILURE_THRESHOLD:
                self._open_circuit(f"{self._consec_hard} consecutive connection/5xx errors")
        # "other" (4xx) is not a health signal — don't touch the breaker.

    def _record_success(self) -> None:
        self._consec_hard = 0
        self._consec_timeout = 0
        if self._open_count:
            logger.info(
                f"Gamma circuit: consecutive-open counter reset "
                f"({self._open_count} → 0) — Gamma recovered"
            )
            self._open_count = 0

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            # Retry on transient server errors and rate limiting
            return exc.response.status_code in (429, 500, 502, 503, 504)
        return False  # 400/403/404 — not retryable

    @retry(
        stop=stop_after_attempt(_PRICES_MAX_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable.__func__),
        reraise=True,
    )
    def _get(self, path: str, params: dict | None = None) -> list | dict:
        url = f"{GAMMA_BASE}{path}"
        try:
            # (connect, read): connect-fail fast-fails at 5s; read capped at 30s.
            # 2 retries + 2s backoff → ≤62s worst-case per call.
            resp = self._session.get(
                url, params=params,
                timeout=(_PRICES_CONNECT_TIMEOUT_S, _PRICES_READ_TIMEOUT_S),
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 5))
                logger.warning(f"Gamma 429 rate-limit — Retry-After: {retry_after}s")
                time.sleep(retry_after)
                resp.raise_for_status()
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            logger.error(f"HTTP {e.response.status_code} from {url}: {e}")
            raise
        finally:
            time.sleep(REQUEST_DELAY)

    def get_events(
        self,
        tag_id: int | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[dict]:
        params: dict = {"closed": "false", "active": "true", "limit": limit, "offset": offset}
        if tag_id is not None:
            params["tag_id"] = tag_id
        if q is not None:
            params["q"] = q
        result = self._get("/events", params=params)
        # Gamma API may return {"data": [...]} or just [...]
        if isinstance(result, dict):
            return result.get("data", [])
        return result

    def _get_once(self, path: str, params: dict | None = None,
                  timeout: float = _DISCOVERY_TIMEOUT_S) -> list | dict:
        """Single-attempt GET (no tenacity) for budget-bounded discovery paging."""
        url = f"{GAMMA_BASE}{path}"
        try:
            # connect=3s fast-fail; read=timeout (default 20s).
            resp = self._session.get(
                url, params=params,
                timeout=(_DISCOVERY_CONNECT_TIMEOUT_S, timeout),
            )
            resp.raise_for_status()
            return resp.json()
        finally:
            time.sleep(REQUEST_DELAY)

    def _fetch_events_page(self, offset: int, limit: int, deadline: float | None,
                           label: str, kwargs: dict) -> list[dict] | None:
        """
        Fetch ONE page with in-cycle backoff. Returns the page list ([] at end of
        feed), or None to stop paginating (hard failure or budget/retries spent).
        Records + classifies failures so the breaker only trips on real trouble.
        """
        params: dict = {"closed": "false", "active": "true", "limit": limit, "offset": offset}
        if kwargs.get("tag_id") is not None:
            params["tag_id"] = kwargs["tag_id"]
        if kwargs.get("q") is not None:
            params["q"] = kwargs["q"]

        backoff = 2.0
        last_kind = "timeout"
        for attempt in range(1, _DISCOVERY_PAGE_RETRIES + 1):
            if deadline is not None and time.monotonic() >= deadline:
                last_kind = "timeout"
                break
            try:
                result = self._get_once("/events", params=params)
                data = result.get("data", []) if isinstance(result, dict) else result
                return data or []
            except Exception as e:
                last_kind = self._classify(e)
                logger.warning(
                    f"Gamma /events {label} offset={offset} "
                    f"attempt {attempt}/{_DISCOVERY_PAGE_RETRIES} [{last_kind}]: {e}"
                )
                if last_kind == "hard":
                    break  # endpoint down — don't burn budget retrying
                # soft (read-timeout / 429): back off within the deadline, retry
                if attempt < _DISCOVERY_PAGE_RETRIES:
                    sleep_for = backoff
                    if deadline is not None:
                        sleep_for = min(sleep_for, max(0.0, deadline - time.monotonic()))
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                    backoff *= 2
        # One failure per page give-up (NOT per retry attempt) — a single slow
        # page must not, by itself, march the breaker toward open.
        self._last_fail_kind = last_kind
        self._record_failure(last_kind)
        return None

    def _paginate_events(self, deadline: float | None = None, **kwargs) -> list[dict]:
        """Fetch all pages for a given set of get_events kwargs, within a budget."""
        if self._is_circuit_open():
            logger.debug("Gamma circuit open — skipping paginated fetch")
            self._last_fetch_status = "breaker_open"
            return []

        all_events: list[dict] = []
        offset = 0
        limit = kwargs.pop("limit", 100)
        label = str(kwargs)
        pages = 0
        t0 = time.monotonic()
        status = "ok"
        self._last_fail_kind = ""

        while True:
            if deadline is not None and time.monotonic() >= deadline:
                logger.warning(
                    f"Gamma {label}: time budget exhausted at offset={offset} — "
                    f"skipping rest of cycle"
                )
                status = "timeout_budget"
                break
            page = self._fetch_events_page(offset, limit, deadline, label, kwargs)
            if page is None:
                # Failure already recorded + classified in _fetch_events_page.
                status = "endpoint_error" if self._last_fail_kind == "hard" else "timeout_budget"
                break
            if not page:
                break
            all_events.extend(page)
            pages += 1
            logger.debug(f"Gamma /events {label} offset={offset}: {len(page)} events")
            self._record_success()
            if len(page) < limit:
                break
            offset += limit

        elapsed = time.monotonic() - t0
        logger.info(
            f"Gamma {label}: {len(all_events)} events in {pages} pages "
            f"({elapsed:.1f}s, status={status})"
        )
        self._last_fetch_status = status
        return all_events

    def get_prices_for_group(self, event_id: str) -> dict[str, float]:
        """
        Fetch current YES prices for all markets in an event.
        Returns {market_id: price_yes} using embedded outcomePrices from Gamma.
        """
        if self._is_circuit_open():
            return {}
        try:
            resp = self._get(f"/events/{event_id}")
            self._record_success()
        except Exception as e:
            self._record_failure(self._classify(e))
            logger.warning(f"Gamma prices fetch failed for event {event_id}: {e}")
            return {}

        event = resp[0] if isinstance(resp, list) else resp
        if not isinstance(event, dict):
            return {}

        result: dict[str, float] = {}
        for market in event.get("markets") or []:
            mid = str(market.get("id") or "")
            raw_prices = market.get("outcomePrices")
            if not mid or not raw_prices:
                continue
            try:
                prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                result[mid] = float(prices[0])
            except Exception:
                pass
        return result

    def is_resolved_event(self, event_id: str) -> bool:
        """
        Return True if the Gamma event is resolved (any market has price_yes ≥ 0.99).
        Used by reconcile and paper-settle jobs to gate PnL attribution.
        """
        prices = self.get_prices_for_group(event_id)
        if not prices:
            return False
        return max(prices.values(), default=0.0) >= 0.99

    def winning_market_id(self, event_id: str) -> str | None:
        """Return the market_id whose YES price ≥ 0.99, or None if still live."""
        prices = self.get_prices_for_group(event_id)
        for mid, p in prices.items():
            if p >= 0.99:
                return mid
        return None

    def _get_tag_id_by_slug(self, slug: str) -> int | None:
        """Resolve a tag slug to its numeric id via /tags/slug/{slug}.

        Returns None on any failure — caller falls back to hardcoded _WEATHER_TAG_ID_FALLBACK.
        """
        try:
            result = self._get_once(f"/tags/slug/{slug}", timeout=10.0)
            if isinstance(result, dict) and result.get("id"):
                return int(result["id"])
            if isinstance(result, list) and result and result[0].get("id"):
                return int(result[0]["id"])
            logger.warning(f"[gamma] /tags/slug/{slug} returned unexpected shape: {result!r}")
        except Exception as exc:
            logger.warning(f"[gamma] tag slug lookup failed ({slug!r}): {exc}")
        return None

    def _fetch_keyset_page(
        self, tag_id: int, after_cursor: str | None, deadline: float | None
    ) -> tuple[list[dict], str | None] | None:
        """Fetch one page from /events/keyset with in-cycle backoff.

        Returns (events, next_cursor) on success.
        Returns None when all retries are spent or a hard failure occurs — caller stops.
        """
        params: dict = {"closed": "false", "limit": 100, "tag_id": tag_id}
        if after_cursor:
            params["after_cursor"] = after_cursor

        backoff = 2.0
        last_kind = "timeout"
        for attempt in range(1, _DISCOVERY_PAGE_RETRIES + 1):
            if deadline is not None and time.monotonic() >= deadline:
                last_kind = "timeout"
                break
            try:
                result = self._get_once("/events/keyset", params=params)
                if isinstance(result, dict):
                    # /events/keyset returns {"events": [...], "next_cursor": "..."}
                    # (different from /events which uses "data")
                    data = result.get("events") or result.get("data") or []
                    cursor = result.get("next_cursor")  # absent/None when feed exhausted
                else:
                    data = result or []
                    cursor = None
                return data, cursor
            except Exception as e:
                last_kind = self._classify(e)
                logger.warning(
                    f"Gamma /events/keyset tag_id={tag_id} "
                    f"attempt {attempt}/{_DISCOVERY_PAGE_RETRIES} [{last_kind}]: {e}"
                )
                if last_kind == "hard":
                    break
                if attempt < _DISCOVERY_PAGE_RETRIES:
                    remaining = (deadline - time.monotonic()) if deadline else backoff
                    sleep_for = min(backoff, max(0.0, remaining))
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                    backoff *= 2

        self._last_fail_kind = last_kind
        self._record_failure(last_kind)
        return None

    def _paginate_events_keyset(self, tag_id: int, deadline: float | None) -> list[dict]:
        """Fetch all events for tag_id via cursor-based /events/keyset pagination.

        Cursor pagination avoids the offset-403 limit and correctly handles
        feed growth between pages (no duplicates from shifted offsets).
        """
        if self._is_circuit_open():
            logger.debug("Gamma circuit open — skipping keyset fetch")
            self._last_fetch_status = "breaker_open"
            return []

        all_events: list[dict] = []
        after_cursor: str | None = None
        pages = 0
        t0 = time.monotonic()
        status = "ok"
        self._last_fail_kind = ""

        while True:
            if deadline is not None and time.monotonic() >= deadline:
                logger.warning(
                    f"[gamma] keyset tag_id={tag_id}: time budget exhausted "
                    f"after {pages} pages ({len(all_events)} events)"
                )
                status = "timeout_budget"
                break

            result = self._fetch_keyset_page(tag_id, after_cursor, deadline)
            if result is None:
                status = "endpoint_error" if self._last_fail_kind == "hard" else "timeout_budget"
                break

            page_data, next_cursor = result
            all_events.extend(page_data)
            pages += 1
            logger.debug(
                f"Gamma /events/keyset tag_id={tag_id} page {pages}: "
                f"{len(page_data)} events (cursor={after_cursor!r})"
            )
            self._record_success()

            if not next_cursor or not page_data:
                break  # feed exhausted
            after_cursor = next_cursor

        elapsed = time.monotonic() - t0
        logger.info(
            f"Gamma keyset tag_id={tag_id}: {len(all_events)} events "
            f"in {pages} pages ({elapsed:.1f}s, status={status})"
        )
        self._last_fetch_status = status
        return all_events

    def get_all_weather_events(self) -> list[dict]:
        """Fetch weather events via tag-based keyset pagination.

        DISCOVERY-FIX: Gamma's ?q= keyword search stopped filtering server-side
        ~2026-06-19, returning unrelated events for all queries. Replaced with
        /events/keyset?tag_id=103040 (slug "temperature" = "Daily Temperature"),
        which correctly returns Highest temperature in {city} events.

        tag_id is resolved dynamically via /tags/slug/{slug} so no hardcoded
        dependency. Falls back to _WEATHER_TAG_ID_FALLBACK on lookup failure.

        G4-24: bounded by _DISCOVERY_BUDGET_S wall-clock budget. last_fetch_status
        carries "ok" / "breaker_open" / "timeout_budget" / "endpoint_error".
        """
        deadline = time.monotonic() + _DISCOVERY_BUDGET_S

        tag_id = self._get_tag_id_by_slug(_WEATHER_TAG_SLUG) or _WEATHER_TAG_ID_FALLBACK
        logger.info(
            f"[gamma] weather discovery via keyset: "
            f"slug={_WEATHER_TAG_SLUG!r} tag_id={tag_id}"
        )

        events = self._paginate_events_keyset(tag_id=tag_id, deadline=deadline)
        logger.info(
            f"Gamma: {len(events)} weather events fetched "
            f"(status={self._last_fetch_status})"
        )
        return events
