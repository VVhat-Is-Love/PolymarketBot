from datetime import datetime
import requests
from loguru import logger
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from src.config.settings import settings
from src.config.cities import CITIES_WHITELIST
from src.db.models import WeatherSnapshot
from src.db.session import get_session
from src.db.repository import WeatherSnapshotRepository

OWM_BASE = "https://api.openweathermap.org/data/2.5"


class WeatherFetcher:
    def __init__(self) -> None:
        self._session = requests.Session()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
        reraise=True,
    )
    def _get(self, endpoint: str, lat: float, lon: float, extra: dict | None = None) -> dict:
        params: dict = {
            "lat": lat,
            "lon": lon,
            "appid": settings.openweather_api_key,
            "units": "imperial",  # Fahrenheit for US; international cities get Celsius via unit field
        }
        if extra:
            params.update(extra)
        resp = self._session.get(f"{OWM_BASE}/{endpoint}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def fetch_city(self, city: str, lat: float, lon: float) -> WeatherSnapshot | None:
        try:
            current = self._get("weather", lat, lon)
            forecast = self._get("forecast", lat, lon, extra={"cnt": 8})

            items = forecast.get("list", [])
            highs = [i["main"]["temp_max"] for i in items if "main" in i]
            lows = [i["main"]["temp_min"] for i in items if "main" in i]

            desc = None
            if current.get("weather"):
                desc = current["weather"][0].get("description")

            return WeatherSnapshot(
                city=city,
                temp_current=current["main"].get("temp"),
                temp_forecast_max=max(highs) if highs else None,
                temp_forecast_min=min(lows) if lows else None,
                humidity=current["main"].get("humidity"),
                description=desc,
                source="openweathermap",
                snapshot_time=datetime.utcnow(),
            )
        except Exception as e:
            logger.error(f"Failed to fetch weather for {city}: {e}")
            return None

    def fetch_all_cities(self) -> int:
        session = get_session()
        repo = WeatherSnapshotRepository(session)
        saved = 0

        for city, cfg in CITIES_WHITELIST.items():
            snapshot = self.fetch_city(city, cfg["lat"], cfg["lon"])
            if snapshot:
                repo.insert(snapshot)
                saved += 1
                logger.debug(
                    f"Weather: {city} → {snapshot.temp_current}°F, {snapshot.description}"
                )

        session.close()
        logger.info(f"Weather snapshots saved: {saved}/{len(CITIES_WHITELIST)} cities")
        return saved
