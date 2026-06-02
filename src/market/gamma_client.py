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
_COOLDOWN_MINUTES = 10

# Discovery has a hard wall-clock budget so one slow page can't hang the whole
# scheduler. Better to skip a cycle than block every other job behind it.
_DISCOVERY_BUDGET_S = 90.0    # total time allowed for one get_all_weather_events
_DISCOVERY_TIMEOUT_S = 20.0   # per-attempt HTTP read timeout for discovery pages
_DISCOVERY_PAGE_RETRIES = 3   # in-cycle retries (with backoff) per page on timeout


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
        self._circuit_open_until = datetime.now() + timedelta(minutes=_COOLDOWN_MINUTES)
        logger.warning(
            f"Gamma circuit breaker OPEN ({reason}) — "
            f"hard={self._consec_hard} timeout={self._consec_timeout}. "
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

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            # Retry on transient server errors and rate limiting
            return exc.response.status_code in (429, 500, 502, 503, 504)
        return False  # 400/403/404 — not retryable

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable.__func__),
        reraise=True,
    )
    def _get(self, path: str, params: dict | None = None) -> list | dict:
        url = f"{GAMMA_BASE}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=60)  # G2-8: 30→60s
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
            resp = self._session.get(url, params=params, timeout=timeout)
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

    def get_all_weather_events(self) -> list[dict]:
        """
        Fetch weather events via targeted keyword search to avoid the 403 that
        Gamma returns when paginating beyond offset=900 on the full /events feed.

        G4-24: bounded by a hard wall-clock budget so one slow page can't hang the
        scheduler. last_fetch_status carries the reason a cycle came back short
        ("ok" / "breaker_open" / "timeout_budget" / "endpoint_error").
        """
        seen_ids: set = set()
        all_events: list[dict] = []
        deadline = time.monotonic() + _DISCOVERY_BUDGET_S
        statuses: list[str] = []

        for keyword in ("temperature", "precipitation"):
            page_events = self._paginate_events(deadline=deadline, q=keyword)
            statuses.append(self._last_fetch_status)
            new = [e for e in page_events if e.get("id") not in seen_ids]
            for e in new:
                seen_ids.add(e["id"])
            all_events.extend(new)
            logger.info(
                f"Gamma keyword={keyword!r}: {len(page_events)} events ({len(new)} new) "
                f"[status={self._last_fetch_status}]"
            )

        # Surface the worst reason (any non-ok) so discovery can attribute a 0-cycle.
        final = next((s for s in statuses if s != "ok"), "ok")
        self._last_fetch_status = final
        logger.info(
            f"Gamma: {len(all_events)} unique weather events fetched (status={final})"
        )
        return all_events
