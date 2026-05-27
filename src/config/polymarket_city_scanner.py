#!/usr/bin/env python3
"""
polymarket_city_scanner.py
==========================
Scans ALL active Polymarket temperature markets, parses resolution rules
with regex (no Claude API needed), geocodes stations, and outputs
a ready-to-use CITIES_WHITELIST.

Requirements:
    pip install requests airportsdata python-dotenv

    airportsdata  — coordinates for ICAO airport stations (optional but recommended)
    requests      — HTTP
    python-dotenv — reads .env

Usage:
    python polymarket_city_scanner.py

Outputs:
    city_scan_results.csv        — review before using, check ⚠️ warnings
    cities_whitelist_updated.py  — paste into src/config/cities.py
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests

# ── Regex patterns ─────────────────────────────────────────────────────────────

# Unit
_CELSIUS_RE    = re.compile(r'degrees?\s+Celsius|deg\.\s*C\b|°C|\bCelsius\b', re.I)
_FAHRENHEIT_RE = re.compile(r'degrees?\s+Fahrenheit|deg\.\s*F\b|°F|\bFahrenheit\b', re.I)

# Resolution URL — "available here: <url>"
_URL_RE = re.compile(r'available\s+here\s*:\s*(https?://\S+)', re.I)

# Any URL in text (fallback)
_ANY_URL_RE = re.compile(r'https?://[^\s"\'<>]+', re.I)

# Station name before "Station" keyword or comma: "for the Esenboğa Intl Airport Station"
_STATION_NAME_RE = re.compile(
    r'(?:recorded\s+(?:by|at|for)\s+the|for\s+the)\s+(.+?)\s*(?:Station\b|,\s*available|,\s*specifically)',
    re.I,
)

# City from title: "Highest temperature in Tokyo on ..."
_CITY_RE = re.compile(
    r'(?:highest|lowest)\s+temperature\s+in\s+(.+?)\s+on\b',
    re.IGNORECASE,
)

# Resolution source classification by URL domain / keyword
_SOURCE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'wunderground\.com', re.I),             'wunderground'),
    (re.compile(r'weather\.gov\.hk|hong\s*kong\s+obs',  re.I), 'hko'),
    (re.compile(r'weather\.gov(?!\.hk)',                 re.I), 'nws'),
    (re.compile(r'jma\.go\.jp|japan\s+met',              re.I), 'jma'),
    (re.compile(r'kma\.go\.kr|korea\s+met',              re.I), 'kma'),
    (re.compile(r'metoffice\.gov\.uk|met\s+office',      re.I), 'met_office'),
    (re.compile(r'bom\.gov\.au|bureau\s+of\s+met',       re.I), 'bom'),
    (re.compile(r'smhi\.se',                             re.I), 'smhi'),
    (re.compile(r'dmi\.dk',                              re.I), 'dmi'),
    (re.compile(r'cwb\.gov\.tw',                         re.I), 'cwb'),
    (re.compile(r'imd\.gov\.in',                         re.I), 'imd'),
    (re.compile(r'eccc|weather\.gc\.ca',                 re.I), 'eccc'),
]

# ── Known non-airport observatory coordinates ──────────────────────────────────

_OBSERVATORIES: dict[str, tuple[float, float]] = {
    # Key: lowercase keyword that appears in station_name or resolution_url
    "weather.gov.hk":          (22.3125,   114.1742),  # HK King's Park Observatory
    "hong kong observatory":   (22.3125,   114.1742),
    "king's park":             (22.3125,   114.1742),
    "jma.go.jp":               (35.6894,   139.6917),  # JMA Tokyo
    "kma.go.kr":               (37.5714,   126.9658),  # KMA Seoul
    "metoffice.gov.uk":        (51.4214,    -0.9540),  # UK Met Office Exeter
    "bom.gov.au":              (-37.8136,  144.9631),  # BoM Melbourne
    "dmi.dk":                  (55.6316,    12.5816),  # DMI Copenhagen
    "cwb.gov.tw":              (25.0330,   121.5654),  # CWB Taipei
    "imd.gov.in":              (28.6139,    77.2090),  # IMD Delhi
    "weather.gc.ca":           (45.4215,   -75.6972),  # ECCC Ottawa
}

# Fallback city-centre coords for cities not yet in the whitelist
_CITY_CENTRE_FALLBACK: dict[str, tuple[float, float]] = {
    "New York":      (40.7128,  -74.0060),
    "Miami":         (25.7617,  -80.1918),
    "Los Angeles":   (34.0522, -118.2437),
    "Austin":        (30.2672,  -97.7431),
    "Chicago":       (41.8781,  -87.6298),
    "London":        (51.5074,   -0.1278),
    "Tokyo":         (35.6762,  139.6503),
    "Seoul":         (37.5665,  126.9780),
    "Hong Kong":     (22.3193,  114.1694),
}

# ── Gamma API helpers ─────────────────────────────────────────────────────────

GAMMA_BASE = "https://gamma-api.polymarket.com"


def fetch_temperature_events() -> list[dict]:
    """Page through Gamma API events, return those with 'temperature' in title."""
    collected: list[dict] = []
    limit, offset = 100, 0
    print("Fetching events from Polymarket Gamma API ...")

    while True:
        try:
            r = requests.get(
                f"{GAMMA_BASE}/events",
                params={"limit": limit, "offset": offset,
                        "active": "true", "closed": "false"},
                timeout=20,
            )
            r.raise_for_status()
            batch: list[dict] = r.json()
        except Exception as exc:
            print(f"  ⚠️  API error at offset {offset}: {exc}")
            break

        if not batch:
            break

        for ev in batch:
            title = (ev.get("title") or ev.get("name") or ev.get("question") or "").lower()
            if "temperature" in title:
                collected.append(ev)

        print(f"  offset={offset:4d}  batch={len(batch):3d}  temperature_events={len(collected)}")
        if len(batch) < limit:
            break
        offset += limit
        time.sleep(0.2)

    print(f"\nTotal temperature events: {len(collected)}\n")
    return collected


def get_description(event: dict) -> str:
    """Return the longest available description text from an event."""
    candidates = [
        event.get("description"),
        event.get("longDescription"),
        event.get("resolutionSource"),
    ]
    for market in event.get("markets", []):
        candidates += [market.get("description"), market.get("longDescription")]
    texts = [t for t in candidates if t and len(t) > 40]
    return max(texts, key=len) if texts else ""


def deduplicate_by_city(events: list[dict]) -> dict[str, dict]:
    """One event per unique city — pick the most description-rich."""
    by_city: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        title = ev.get("title") or ev.get("name") or ev.get("question") or ""
        city = extract_city(title)
        if city:
            by_city[city].append(ev)
    result = {}
    for city, evs in sorted(by_city.items()):
        best = max(evs, key=lambda e: len(get_description(e)))
        result[city] = best
    print(f"Unique cities found ({len(result)}): {', '.join(sorted(result))}\n")
    return result


def extract_city(title: str) -> str | None:
    m = _CITY_RE.search(title)
    return m.group(1).strip().title() if m else None


# ── Rule parsing (pure regex — no Claude API) ──────────────────────────────────

def parse_rules(city: str, desc: str, title: str) -> dict:
    """
    Extract resolution metadata from a market description using regex.

    Handles: Wunderground (most markets), HKO, NWS, JMA, KMA, Met Office, BoM, etc.
    """
    # ── Unit ──────────────────────────────────────────────────────────────────
    if _CELSIUS_RE.search(desc):
        unit = "C"
    elif _FAHRENHEIT_RE.search(desc):
        unit = "F"
    else:
        # Heuristic: US markets (ICAO starts with K) use F
        unit = "C"

    # ── Metric ────────────────────────────────────────────────────────────────
    metric = "low_temp" if "lowest" in title.lower() else "high_temp"

    # ── Resolution URL ────────────────────────────────────────────────────────
    resolution_url: str | None = None
    url_match = _URL_RE.search(desc)
    if url_match:
        resolution_url = url_match.group(1).rstrip('.,)')
    else:
        # Fallback: any URL in description
        any_url = _ANY_URL_RE.search(desc)
        if any_url:
            resolution_url = any_url.group(0).rstrip('.,)')

    # ── Resolution source ─────────────────────────────────────────────────────
    resolution_source = "unknown"
    search_text = (desc + " " + (resolution_url or "")).lower()
    for pattern, src in _SOURCE_PATTERNS:
        if pattern.search(search_text):
            resolution_source = src
            break

    # ── Station code + Wunderground path ─────────────────────────────────────
    station_code: str | None = None
    wunderground_path: str | None = None

    if resolution_source == "wunderground" and resolution_url:
        parsed_url = urlparse(resolution_url)
        # path: /history/daily/tr/%C3%A7ubuk/LTAC
        segments = [s for s in parsed_url.path.split("/") if s]
        # segments: ['history', 'daily', 'tr', '%C3%A7ubuk', 'LTAC']
        if len(segments) >= 3:
            station_code = unquote(segments[-1]).upper()  # LTAC
            # wunderground_path = everything after "daily/"
            daily_idx = next((i for i, s in enumerate(segments) if s == "daily"), None)
            if daily_idx is not None:
                wunderground_path = "/".join(
                    unquote(s) for s in segments[daily_idx + 1:]
                )

    # ── Station name ──────────────────────────────────────────────────────────
    station_name: str | None = None
    name_match = _STATION_NAME_RE.search(desc)
    if name_match:
        station_name = name_match.group(1).strip().rstrip(',')

    return {
        "resolution_source": resolution_source,
        "station_name":      station_name,
        "station_code":      station_code,
        "wunderground_path": wunderground_path,
        "resolution_url":    resolution_url,
        "unit":              unit,
        "metric":            metric,
    }


# ── Geocoding ─────────────────────────────────────────────────────────────────

def get_station_coords(
    station_code: str | None,
    station_name: str | None,
    resolution_url: str | None,
    city: str,
) -> tuple[float | None, float | None, str]:
    """
    Return (lat, lon, method) for a station.

    Priority:
      1. airportsdata ICAO lookup
      2. Known observatory table
      3. City-centre fallback (triggers ⚠️ warning)
    """
    # 1. ICAO airport
    if station_code and re.match(r'^[A-Z]{3,4}$', station_code.upper()):
        try:
            import airportsdata
            airports = airportsdata.load("ICAO")
            ap = airports.get(station_code.upper())
            if ap:
                return float(ap["lat"]), float(ap["lon"]), "icao"
        except ImportError:
            pass  # airportsdata not installed — will fall through

    # 2. Known observatory
    search_keys = [
        (resolution_url or "").lower(),
        (station_name or "").lower(),
    ]
    for key in search_keys:
        for known, coords in _OBSERVATORIES.items():
            if known in key:
                return coords[0], coords[1], "observatory"

    # 3. City-centre fallback
    fallback = _CITY_CENTRE_FALLBACK.get(city)
    if fallback:
        return fallback[0], fallback[1], "city_centre_fallback"

    return None, None, "unknown"


# ── Build whitelist entry ─────────────────────────────────────────────────────

_ALIASES: dict[str, list[str]] = {
    "New York":    ["NYC", "New York City"],
    "Los Angeles": ["LA"],
    "Hong Kong":   ["HK"],
}


def build_entry(city: str, parsed: dict) -> dict:
    lat, lon, coord_method = get_station_coords(
        parsed["station_code"],
        parsed["station_name"],
        parsed["resolution_url"],
        city,
    )
    entry = {
        "aliases":           _ALIASES.get(city, []),
        "unit":              parsed["unit"],
        "lat":               round(lat, 6) if lat else None,
        "lon":               round(lon, 6) if lon else None,
        "station":           parsed["station_code"],
        "station_name":      parsed["station_name"],
        "resolution_source": parsed["resolution_source"],
        "resolution_url":    parsed["resolution_url"],
        "wunderground":      parsed["wunderground_path"],
        "_coord_method":     coord_method,
    }
    return entry


# ── Output ────────────────────────────────────────────────────────────────────

def write_csv(results: dict[str, dict], path: Path) -> None:
    fields = [
        "city", "unit", "lat", "lon", "station", "station_name",
        "resolution_source", "wunderground", "resolution_url", "_coord_method",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for city, entry in sorted(results.items()):
            w.writerow({"city": city, **entry})
    print(f"CSV → {path}")


def write_python(results: dict[str, dict], path: Path) -> None:
    lines = [
        "# Auto-generated by polymarket_city_scanner.py",
        "# ⚠️  Entries with _coord_method='city_centre_fallback' need manual coord check.",
        "# ⚠️  If airportsdata was NOT installed, ICAO entries also need verification.",
        "from __future__ import annotations",
        "",
        "CITIES_WHITELIST: dict[str, dict] = {",
    ]
    for city, e in sorted(results.items()):
        warn = ""
        if e.get("_coord_method") in ("city_centre_fallback", "unknown"):
            warn = "  # ⚠️  coords unverified — check station manually"
        elif e.get("_coord_method") == "icao" and not e.get("lat"):
            warn = "  # ⚠️  airportsdata not installed — coords may be city-centre"
        lines.append(f'    "{city}": {{{warn}')
        for k in ("aliases", "unit", "lat", "lon", "station",
                  "station_name", "resolution_source", "resolution_url", "wunderground"):
            v = e.get(k)
            lines.append(f'        "{k}": {v!r},')
        lines.append("    },")
    lines += ["}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Python whitelist → {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Check airportsdata
    try:
        import airportsdata
        print("✅ airportsdata available — ICAO stations will be geocoded automatically.\n")
    except ImportError:
        print("⚠️  airportsdata not installed. Run: pip install airportsdata")
        print("   ICAO stations will fall back to city-centre coords.\n")

    events = fetch_temperature_events()
    if not events:
        print("No events returned. Check internet connection and Gamma API.")
        sys.exit(1)

    by_city = deduplicate_by_city(events)
    results: dict[str, dict] = {}

    for city, event in sorted(by_city.items()):
        desc = get_description(event)
        title = event.get("title") or event.get("name") or ""
        if not desc:
            print(f"  SKIP {city}: no description")
            continue

        parsed = parse_rules(city, desc, title)
        entry  = build_entry(city, parsed)
        results[city] = entry

        method_icon = {"icao": "✅", "observatory": "✅", "city_centre_fallback": "⚠️ "}.get(
            entry["_coord_method"], "❓"
        )
        print(
            f"  {method_icon} {city:<20} "
            f"unit={entry['unit']}  "
            f"source={entry['resolution_source']:<12} "
            f"station={entry['station'] or '—':<6} "
            f"lat={entry['lat']}  lon={entry['lon']}"
        )

    out = Path(__file__).parent
    write_csv(results, out / "city_scan_results.csv")
    write_python(results, out / "cities_whitelist_updated.py")

    # Summary
    needs_review = [c for c, e in results.items() if e["_coord_method"] in ("city_centre_fallback", "unknown")]
    print(f"\n{'─'*60}")
    print(f"Total cities: {len(results)}")
    print(f"Auto-geocoded: {len(results) - len(needs_review)}")
    if needs_review:
        print(f"Need manual coord check ({len(needs_review)}): {', '.join(sorted(needs_review))}")
        print("  → Open city_scan_results.csv, find the resolution_url for each,")
        print("    look up station coordinates, update cities_whitelist_updated.py.")


if __name__ == "__main__":
    main()