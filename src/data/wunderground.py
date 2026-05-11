"""
Wunderground historical temperature scraper.

Used to resolve paper trades with same-day actual temperatures before the
Open-Meteo archive API updates (which can lag 24-48 h).

⚠️  Wunderground HTML structure changes periodically.  If parsing returns None,
    check the selector log lines and update _parse_high_temp() accordingly.
"""
import re
import requests
from bs4 import BeautifulSoup
from datetime import date
from loguru import logger

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_WU_BASE = "https://www.wunderground.com/history/daily"


def fetch_actual_high(city: str, target_date: date) -> float | None:
    """
    Scrape the daily high temperature from Wunderground for the given city/date.
    Returns the temperature in the city's market unit (F for US, C for international),
    or None if scraping fails (resolution will be retried next cycle).
    """
    from src.config.cities import CITIES_WHITELIST

    cfg = CITIES_WHITELIST.get(city)
    if not cfg or "wunderground" not in cfg:
        return None

    # Wunderground date format has no leading zeros: 2026-5-4
    date_str = f"{target_date.year}-{target_date.month}-{target_date.day}"
    url = f"{_WU_BASE}/{cfg['wunderground']}/date/{date_str}"

    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Wunderground fetch failed for {city} {target_date}: {e}")
        return None

    result = _parse_high_temp(resp.text, cfg.get("unit", "F"), city, target_date)
    if result is not None:
        logger.info(f"Wunderground: {city} {target_date} high={result}°{cfg.get('unit','F')}")
    return result


def _extract_number(text: str) -> float | None:
    """Pull the first float-looking substring out of a text node."""
    text = text.strip()
    m = re.search(r"-?\d+\.?\d*", text)
    if not m:
        return None
    try:
        val = float(m.group())
        if -60 < val < 60:   # Celsius sanity range
            return val
        if -76 < val < 140:  # Fahrenheit sanity range
            return val
    except ValueError:
        pass
    return None


def _parse_high_temp(
    html: str, unit: str, city: str, target_date: date
) -> float | None:
    # Wunderground is an Angular SPA — the initial server response is a JS bootstrap
    # with "Please enable JavaScript to continue using this application."
    # Real temperature data requires JavaScript rendering.  Until we add a headless
    # browser, this function will return None and the caller falls back to Open-Meteo.
    if "enable javascript" in html.lower() or "enable-javascript" in html.lower():
        logger.debug(
            f"Wunderground: page for {city} {target_date} requires JavaScript — "
            "falling back to Open-Meteo archive"
        )
        return None

    soup = BeautifulSoup(html, "lxml")

    # ── Strategy 1 ──────────────────────────────────────────────────────────
    # Daily summary table — look for a <th> or header row containing "High Temp",
    # then read the first numeric cell in that column.
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not any("high" in h for h in headers):
            continue
        high_idx = next((i for i, h in enumerate(headers) if "high" in h), None)
        if high_idx is None:
            continue
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) > high_idx:
                val = _extract_number(cells[high_idx].get_text())
                if val is not None:
                    logger.debug(f"Wunderground strategy-1 table hit: {val}")
                    return val

    # ── Strategy 2 ──────────────────────────────────────────────────────────
    # JSON embedded in <script type="application/json"> tags.
    import json as _json

    def _find_high_in_json(obj, depth=0):
        if depth > 8:
            return None
        if isinstance(obj, dict):
            for k, v in obj.items():
                if "high" in str(k).lower() and isinstance(v, (int, float)):
                    return float(v)
                result = _find_high_in_json(v, depth + 1)
                if result is not None:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = _find_high_in_json(item, depth + 1)
                if result is not None:
                    return result
        return None

    for script in soup.find_all("script", type="application/json"):
        try:
            val = _find_high_in_json(_json.loads(script.string or ""))
            if val is not None:
                logger.debug(f"Wunderground strategy-2 JSON hit: {val}")
                return val
        except Exception:
            continue

    logger.warning(
        f"Wunderground: could not parse high temp for {city} {target_date} — "
        "selectors need updating (JS rendering may be required)"
    )
    return None
