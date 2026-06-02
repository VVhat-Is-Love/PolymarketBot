import json
from datetime import datetime, timezone, timedelta
from loguru import logger

from src.config.cities import CITIES_WHITELIST
from src.db.models import Market, MarketGroup
from src.db.session import get_session
from src.db.repository import MarketGroupRepository, MarketRepository
from src.market.gamma_client import GammaClient
from src.market.parsers import (
    parse_city_from_title,
    parse_date_from_title,
    parse_metric_from_title,
    parse_bin_range,
    parse_station,
    canonical_city,
)

MIN_VOLUME = 2_000.0
_MIN_TTL = timedelta(hours=1)  # ignore events resolving within the next hour

_WEATHER_KEYWORDS = ("highest temperature", "lowest temperature", "precipitation")


def _is_event_active(event: dict) -> bool:
    """Return True only if event is open and resolves more than 1 h from now."""
    if event.get("closed", False):
        return False
    raw_end = event.get("endDate")
    if not raw_end:
        return True  # no end date — assume still open
    try:
        end_dt = datetime.fromisoformat(str(raw_end).rstrip("Z")).replace(tzinfo=timezone.utc)
        return end_dt > datetime.now(timezone.utc) + _MIN_TTL
    except Exception:
        return True  # unparseable date — don't drop it


def _parse_token_ids(raw: str | list | None) -> tuple[str | None, str | None]:
    """clobTokenIds arrives as JSON string '["yes_id","no_id"]' or already a list."""
    if raw is None:
        return None, None
    if isinstance(raw, str):
        try:
            ids = json.loads(raw)
        except Exception:
            return None, None
    else:
        ids = raw
    return (ids[0] if len(ids) > 0 else None), (ids[1] if len(ids) > 1 else None)


def discover_and_save_weather_markets(gamma: GammaClient) -> int:
    logger.info("Starting weather market discovery via Gamma API...")
    all_events = gamma.get_all_weather_events()

    active_events = [e for e in all_events if _is_event_active(e)]
    expired_count = len(all_events) - len(active_events)
    logger.info(
        f"Found {len(active_events)} active events "
        f"(filtered out {expired_count} expired/closed)"
    )

    session = get_session()
    group_repo = MarketGroupRepository(session)
    market_repo = MarketRepository(session)

    saved_groups = 0
    saved_markets = 0
    # G4-23 diagnostic: per-stage discovery funnel so a 0-result cycle is
    # attributable in one line — distinguishes "endpoint returned nothing"
    # from "keyword/city/metric filter cut everything" (e.g. Polymarket retitled).
    funnel = {"keyword": 0, "volume": 0, "city": 0, "metric": 0, "date": 0}

    for event in active_events:
        title: str = event.get("title") or ""
        title_lower = title.lower()

        # 1. Only temperature / precipitation events
        if not any(kw in title_lower for kw in _WEATHER_KEYWORDS):
            funnel["keyword"] += 1
            continue

        # 2. Volume filter at event level
        event_volume = float(event.get("volume") or 0)
        if event_volume < MIN_VOLUME:
            funnel["volume"] += 1
            continue

        # 3. City must be in our whitelist
        city = parse_city_from_title(title, CITIES_WHITELIST)
        if not city:
            funnel["city"] += 1
            continue
        city = canonical_city(city)  # G3-5: ensure canonical form (e.g. Nyc → New York)

        # 4. Metric and resolution date
        metric = parse_metric_from_title(title)
        if not metric:
            funnel["metric"] += 1
            continue

        res_date = parse_date_from_title(title)
        if not res_date:
            funnel["date"] += 1
            continue

        # 5. Weather station (description fallback to whitelist default)
        description = event.get("description") or ""
        station = parse_station(description) or CITIES_WHITELIST[city]["station"]
        unit = CITIES_WHITELIST[city]["unit"]

        group_id = str(event["id"])

        group = MarketGroup(
            group_id=group_id,
            city=city,
            metric=metric,
            resolution_date=res_date,
            weather_station=station,
            unit=unit,
            event_volume=event_volume,
            is_active=True,
        )
        group_repo.upsert(group)
        saved_groups += 1

        markets_in_event: list[dict] = event.get("markets") or []
        logger.info(
            f"Found event: {title[:70]} "
            f"(vol=${event_volume:,.0f}, {len(markets_in_event)} bins)"
        )

        # 6. Upsert each bin (market)
        for mkt in markets_in_event:
            bin_label: str = mkt.get("groupItemTitle") or mkt.get("question") or ""
            bin_min, bin_max = parse_bin_range(bin_label)

            token_id_yes, token_id_no = _parse_token_ids(mkt.get("clobTokenIds"))

            end_date: datetime | None = None
            raw_end = event.get("endDate")
            if raw_end:
                try:
                    end_date = datetime.fromisoformat(str(raw_end).rstrip("Z"))
                except Exception:
                    pass

            market = Market(
                market_id=str(mkt["id"]),
                condition_id=mkt.get("conditionId"),
                group_id=group_id,
                question=mkt.get("question") or bin_label,
                bin_label=bin_label,
                bin_min=bin_min,
                bin_max=bin_max,
                token_id_yes=token_id_yes,
                token_id_no=token_id_no,
                volume=float(mkt.get("volume") or 0),
                end_date=end_date,
                is_active=mkt.get("active", True) and not mkt.get("closed", False),
            )
            market_repo.upsert(market)
            saved_markets += 1

    session.close()
    logger.info(
        f"Discovery funnel: {len(active_events)} active → "
        f"−{funnel['keyword']} non-weather title → −{funnel['volume']} low-vol → "
        f"−{funnel['city']} city-miss → −{funnel['metric']} metric-miss → "
        f"−{funnel['date']} date-miss → {saved_groups} groups kept"
    )
    logger.info(f"Discovery complete: {saved_groups} groups, {saved_markets} markets saved")
    return saved_markets
