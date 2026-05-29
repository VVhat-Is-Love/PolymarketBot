from datetime import datetime, timezone, timedelta, date
from pathlib import Path

from loguru import logger

from src.config.cities import CITIES_WHITELIST
from src.db.models import WeatherSnapshot
from src.db.session import get_session
from src.db.repository import WeatherSnapshotRepository

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

MODELS = ["ecmwf_ifs025", "gfs_global", "icon_global"]
CACHE_PATH = str(Path("data") / ".openmeteo_cache")
_MODEL_LABEL = {"ecmwf_ifs025": "ECMWF", "gfs_global": "GFS", "icon_global": "ICON"}


def _build_client():
    import openmeteo_requests
    from requests_cache import CachedSession
    from retry_requests import retry

    cache = CachedSession(CACHE_PATH, expire_after=600)
    session = retry(cache, retries=5, backoff_factor=0.2)
    return openmeteo_requests.Client(session=session)


def _parse_daily(response, model: str, city: str, days: int) -> list[WeatherSnapshot]:
    """Parse one Open-Meteo FlatBuffers response into WeatherSnapshot objects."""
    daily = response.Daily()
    start_ts = daily.Time()
    interval = daily.Interval()  # 86400 s for daily
    n_days = daily.Variables(0).ValuesLength()

    snapshots: list[WeatherSnapshot] = []
    now = datetime.utcnow()

    for i in range(n_days):
        forecast_dt = datetime.fromtimestamp(start_ts + i * interval, tz=timezone.utc)
        forecast_day = forecast_dt.date()

        try:
            temp_max = daily.Variables(0).Values(i)  # temperature_2m_max
            temp_min = daily.Variables(1).Values(i)  # temperature_2m_min
        except Exception:
            continue

        snapshots.append(
            WeatherSnapshot(
                city=city,
                temp_forecast_max=float(temp_max) if temp_max == temp_max else None,  # NaN guard
                temp_forecast_min=float(temp_min) if temp_min == temp_min else None,
                source=f"open_meteo:{model}",
                forecast_date=forecast_day,
                snapshot_time=now,
            )
        )
    return snapshots


def fetch_multi_model_forecast(city: str, lat: float, lon: float, days: int = 3) -> list[WeatherSnapshot]:
    """Fetch daily high/low from 3 global models for the given city.

    timezone="UTC" ensures forecast_date stored in DB equals the UTC calendar date,
    matching Polymarket's resolution_date (which is always a UTC day). With "auto"
    the start-of-day timestamp is midnight local time, shifting stored dates by ±1
    for cities in UTC±N timezones.
    """
    om = _build_client()
    snapshots: list[WeatherSnapshot] = []

    for model in MODELS:
        try:
            params = {
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min",
                "models": model,
                "temperature_unit": "celsius",
                "forecast_days": days,
                "timezone": "UTC",   # was "auto" — see docstring
            }
            responses = om.weather_api(FORECAST_URL, params=params)
            
            if not responses:
                logger.warning(
                    f"[{city}] model={model}: Open-Meteo returned empty response "
                    f"(forecast_days={days}), skipping"
                )
                continue

            
            model_snaps = _parse_daily(responses[0], model, city, days)
            snapshots.extend(model_snaps)
            logger.debug(f"Open-Meteo {model} → {city}: {len(model_snaps)} days")
        except Exception as e:
            logger.error(f"Open-Meteo failed for {city} model={model}: {e}")

    return snapshots


def fetch_actual_temperature(
    lat: float,
    lon: float,
    target_date: date,
    metric: str,  # 'high_temp' | 'low_temp'
) -> float | None:
    """
    Fetch actual temperature for a past date in Celsius.

    Strategy:
    1. Forecast API with past_days=3  — updates in near-realtime, covers yesterday
    2. Archive API                    — lags 1-5 days but more accurate long-term
    """
    days_ago = (date.today() - target_date).days
    val = None

    if days_ago <= 3:
        val = _fetch_past_from_forecast(lat, lon, target_date, metric)
        if val is not None:
            return val

    return _fetch_from_archive(lat, lon, target_date, metric)


def _fetch_past_from_forecast(
    lat: float, lon: float, target_date: date, metric: str
) -> float | None:
    """Use forecast API with past_days to get recent actual temperatures."""
    om = _build_client()
    days_ago = (date.today() - target_date).days
    past_days = max(days_ago + 1, 2)
    try:
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min",
            "past_days": past_days,
            "forecast_days": 1,
            "temperature_unit": "celsius",
            "timezone": "auto",
        }
        responses = om.weather_api(FORECAST_URL, params=params)
        daily = responses[0].Daily()
        n = daily.Variables(0).ValuesLength()
        start_ts = daily.Time()
        interval = daily.Interval()

        for i in range(n):
            row_date = datetime.fromtimestamp(start_ts + i * interval, tz=timezone.utc).date()
            if row_date == target_date:
                if metric == "high_temp":
                    val = daily.Variables(0).Values(i)
                else:
                    val = daily.Variables(1).Values(i)
                if val == val:  # NaN guard
                    return float(val)
    except Exception as e:
        logger.debug(f"Open-Meteo forecast/past_days failed for {target_date}: {e}")
    return None


def _fetch_from_archive(
    lat: float, lon: float, target_date: date, metric: str
) -> float | None:
    """Use archive API for older dates."""
    om = _build_client()
    date_str = target_date.isoformat()
    try:
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": date_str,
            "end_date": date_str,
            "daily": "temperature_2m_max,temperature_2m_min",
            "temperature_unit": "celsius",
            "timezone": "auto",
        }
        responses = om.weather_api(ARCHIVE_URL, params=params)
        daily = responses[0].Daily()
        if daily.Variables(0).ValuesLength() == 0:
            return None
        if metric == "high_temp":
            val = daily.Variables(0).Values(0)
        else:
            val = daily.Variables(1).Values(0)
        return float(val) if val == val else None
    except Exception as e:
        logger.error(f"Open-Meteo archive failed for {target_date}: {e}")
    return None


def fetch_and_save_all_cities() -> int:
    """Fetch multi-model forecasts for all cities in the whitelist and save to DB."""
    session = get_session()
    repo = WeatherSnapshotRepository(session)
    saved = 0
    no_data_cities: list[str] = []

    for city, cfg in CITIES_WHITELIST.items():
        snaps = fetch_multi_model_forecast(city, cfg["lat"], cfg["lon"])
        for snap in snaps:
            repo.insert(snap)
            saved += 1

        _log_city_forecast(city, cfg.get("unit", "F"), snaps)

        if not snaps:
            no_data_cities.append(city)

    session.close()
    logger.info(f"Open-Meteo total snapshots saved: {saved}")

    if no_data_cities:
        logger.warning(
            f"[open_meteo] No data from any model for {len(no_data_cities)} cities: "
            f"{', '.join(no_data_cities[:10])}"
            + (f" (+{len(no_data_cities)-10} more)" if len(no_data_cities) > 10 else "")
        )

    return saved


def _log_city_forecast(city: str, unit: str, snaps: list[WeatherSnapshot]) -> None:
    """Log a one-line consensus forecast per forecast_date for a city."""
    by_date: dict[date, dict[str, float]] = {}
    for snap in snaps:
        if snap.forecast_date is None or snap.temp_forecast_max is None:
            continue
        model = snap.source.replace("open_meteo:", "")
        label = _MODEL_LABEL.get(model, model)
        by_date.setdefault(snap.forecast_date, {})[label] = snap.temp_forecast_max

    for fdate in sorted(by_date):
        model_temps = by_date[fdate]
        if not model_temps:
            continue
        vals = list(model_temps.values())
        consensus_c = sum(vals) / len(vals)
        spread_c = max(vals) - min(vals)
        parts = " ".join(f"{k}={v:.1f}" for k, v in model_temps.items())
        if unit == "F":
            consensus_f = consensus_c * 9 / 5 + 32
            logger.info(
                f"Forecast {city} {fdate}: {parts}°C "
                f"consensus={consensus_c:.1f}°C ({consensus_f:.1f}°F) spread={spread_c:.1f}°C"
            )
        else:
            logger.info(
                f"Forecast {city} {fdate}: {parts}°C "
                f"consensus={consensus_c:.1f}°C spread={spread_c:.1f}°C"
            )
