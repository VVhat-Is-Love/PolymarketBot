import json
import time
from datetime import datetime, timedelta
import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)
from loguru import logger

GAMMA_BASE = "https://gamma-api.polymarket.com"
REQUEST_DELAY = 0.3  # stay well within 500 req/10s limit for /events

_FAILURE_THRESHOLD = 5
_COOLDOWN_MINUTES = 10


class GammaClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "polymarket-bot/0.1",
        })
        self._consecutive_failures: int = 0
        self._circuit_open_until: datetime | None = None

    def _is_circuit_open(self) -> bool:
        if self._circuit_open_until is None:
            return False
        if datetime.now() >= self._circuit_open_until:
            self._circuit_open_until = None
            self._consecutive_failures = 0
            logger.info("Gamma circuit breaker: cooldown finished, resuming")
            return False
        return True

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= _FAILURE_THRESHOLD:
            self._circuit_open_until = datetime.now() + timedelta(minutes=_COOLDOWN_MINUTES)
            logger.warning(
                f"Gamma circuit breaker OPEN — {self._consecutive_failures} consecutive failures. "
                f"Pausing until {self._circuit_open_until:%H:%M:%S}"
            )

    def _record_success(self) -> None:
        self._consecutive_failures = 0

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
        reraise=True,
    )
    def _get(self, path: str, params: dict | None = None) -> list | dict:
        url = f"{GAMMA_BASE}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=30)
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

    def _paginate_events(self, **kwargs) -> list[dict]:
        """Fetch all pages for a given set of get_events kwargs."""
        if self._is_circuit_open():
            logger.debug("Gamma circuit open — skipping paginated fetch")
            return []

        all_events: list[dict] = []
        offset = 0
        limit = kwargs.pop("limit", 100)
        label = str(kwargs)
        pages = 0
        t0 = time.monotonic()

        while True:
            try:
                page = self.get_events(offset=offset, limit=limit, **kwargs)
                if not page:
                    break
                all_events.extend(page)
                pages += 1
                logger.debug(f"Gamma /events {label} offset={offset}: {len(page)} events")
                if len(page) < limit:
                    break
                offset += limit
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.error(f"Gamma /events {label} offset={offset}: {e}")
                break

        elapsed = time.monotonic() - t0
        logger.info(f"Gamma {label}: {len(all_events)} events in {pages} pages ({elapsed:.1f}s)")
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
            self._record_failure()
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

    def get_all_weather_events(self) -> list[dict]:
        """
        Fetch weather events via targeted keyword search to avoid the 403 that
        Gamma returns when paginating beyond offset=900 on the full /events feed.
        """
        seen_ids: set = set()
        all_events: list[dict] = []

        for keyword in ("temperature", "precipitation"):
            page_events = self._paginate_events(q=keyword)
            new = [e for e in page_events if e.get("id") not in seen_ids]
            for e in new:
                seen_ids.add(e["id"])
            all_events.extend(new)
            logger.info(f"Gamma keyword={keyword!r}: {len(page_events)} events ({len(new)} new)")

        logger.info(f"Gamma: {len(all_events)} unique weather events fetched")
        return all_events
