"""
DISCOVERY-FIX diagnostic: dump Gamma /events titles from discovery funnel.

Гипотезы:
  (a) Polymarket изменил формат title — _WEATHER_KEYWORDS требует точную фразу
      "highest temperature" / "lowest temperature", но теперь это "high temperature"
      или "temperature in <city>" -> ВСЕ 1727 события отсеиваются на первом этапе.
  (b) timeout_budget отсекает пагинацию до погодных рынков (они на поздних страницах
      среди 2100+ событий по q=temperature).

Запуск на сервере:
    su - bot -c "cd /opt/polymarket-bot/app && \
        /opt/polymarket-bot/venv/bin/python scripts/diag_discovery.py"

Или локально (тест реального Gamma API — нужен интернет):
    python scripts/diag_discovery.py
"""
import sys
import os
import time
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.market.markets import _WEATHER_KEYWORDS, _EXCLUDED_WEATHER_KEYWORDS, MIN_VOLUME
from src.market.parsers import parse_city_from_title, parse_metric_from_title
from src.config.cities import CITIES_WHITELIST

GAMMA_BASE = "https://gamma-api.polymarket.com"
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 25
PAGE_LIMIT = 100
DUMP_TITLES = 30   # первые N raw titles печатаются без фильтрации
MAX_PAGES = 5      # сколько страниц проверять (диагностика, не полный прогон)


def _fetch_page(q: str, offset: int) -> list[dict]:
    params = {
        "closed": "false",
        "active": "true",
        "limit": PAGE_LIMIT,
        "offset": offset,
        "q": q,
    }
    resp = requests.get(
        f"{GAMMA_BASE}/events",
        params=params,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    resp.raise_for_status()
    result = resp.json()
    data = result.get("data", []) if isinstance(result, dict) else result
    time.sleep(0.3)
    return data or []


def _funnel_label(title: str) -> str:
    """Return the funnel stage this title fails at, or 'PASS' if it gets through."""
    t = title.lower()
    if not any(kw in t for kw in _WEATHER_KEYWORDS):
        # Show which keywords almost match (substring without "highest"/"lowest" prefix)
        hints = []
        if "temperature" in t:
            hints.append("has 'temperature'")
        if "high temp" in t:
            hints.append("has 'high temp'")
        if "maximum" in t or "max temp" in t:
            hints.append("has 'maximum'")
        hint = f"  [{', '.join(hints)}]" if hints else ""
        return f"FAIL:keyword{hint}"
    if any(kw in t for kw in _EXCLUDED_WEATHER_KEYWORDS):
        return "FAIL:excluded(lowest_temp)"
    city = parse_city_from_title(title, CITIES_WHITELIST)
    if not city:
        return "FAIL:city_not_in_whitelist"
    metric = parse_metric_from_title(title)
    if not metric:
        return "FAIL:metric_unrecognised"
    return f"PASS  city={city!r} metric={metric!r}"


def _keyword_variants(title: str) -> dict:
    """Check presence of alternative weather phrasings in a title."""
    t = title.lower()
    return {
        "highest temperature": "highest temperature" in t,
        "high temperature":    "high temperature" in t,
        "max temperature":     "max temperature" in t or "maximum temperature" in t,
        "temperature":         "temperature" in t,
        "lowest temperature":  "lowest temperature" in t,
        "low temperature":     "low temperature" in t,
        "precipitation":       "precipitation" in t,
    }


def main() -> int:
    print("=" * 72)
    print("DISCOVERY-FIX: Gamma /events title diagnostic")
    print(f"  WEATHER_KEYWORDS = {_WEATHER_KEYWORDS}")
    print(f"  EXCLUDED         = {_EXCLUDED_WEATHER_KEYWORDS}")
    print(f"  MIN_VOLUME       = {MIN_VOLUME}")
    print("=" * 72)

    for keyword in ("temperature", "precipitation"):
        print(f"\n{'-' * 72}")
        print(f"  Keyword: q={keyword!r}")
        print(f"{'-' * 72}")

        all_events: list[dict] = []
        for page_num in range(MAX_PAGES):
            offset = page_num * PAGE_LIMIT
            try:
                page = _fetch_page(keyword, offset)
            except Exception as exc:
                print(f"  [ERROR] Page {page_num+1} (offset={offset}): {exc}")
                break
            if not page:
                print(f"  [END]   Page {page_num+1} empty — feed exhausted")
                break
            all_events.extend(page)
            print(f"  [PAGE]  {page_num+1}: offset={offset} -> {len(page)} events (total={len(all_events)})")
            if len(page) < PAGE_LIMIT:
                break

        if not all_events:
            print("  No events fetched.")
            continue

        # ── гипотеза (a): дамп первых DUMP_TITLES raw titles ─────────────────
        print(f"\n  --- Raw titles (first {min(DUMP_TITLES, len(all_events))}) ---")
        for i, ev in enumerate(all_events[:DUMP_TITLES]):
            title = ev.get("title") or "(no title)"
            vol = float(ev.get("volume") or 0)
            stage = _funnel_label(title)
            print(f"  {i+1:3d}. [{stage}]")
            print(f"       vol=${vol:,.0f}  title={title!r}")

        # ── гипотеза (a): подсчёт вариантов фразировки ────────────────────────
        print(f"\n  --- Keyword-variant counts (in all {len(all_events)} fetched events) ---")
        variant_counts: dict[str, int] = {}
        for ev in all_events:
            t = (ev.get("title") or "").lower()
            for phrase in (
                "highest temperature", "high temperature",
                "max temperature", "maximum temperature",
                "lowest temperature", "low temperature",
                "temperature",
                "precipitation",
            ):
                variant_counts[phrase] = variant_counts.get(phrase, 0) + (1 if phrase in t else 0)

        for phrase, count in variant_counts.items():
            flag = " <-- MATCHED by _WEATHER_KEYWORDS" if phrase in _WEATHER_KEYWORDS else ""
            print(f"    {count:4d}  '{phrase}'{flag}")

        # ── гипотеза (b): погодные рынки на поздних страницах? ────────────────
        weather_on_page: dict[int, int] = {}
        for i, ev in enumerate(all_events):
            title = (ev.get("title") or "").lower()
            if "temperature" in title or "precipitation" in title:
                page_num = i // PAGE_LIMIT + 1
                weather_on_page[page_num] = weather_on_page.get(page_num, 0) + 1

        if weather_on_page:
            print(f"\n  --- Events containing 'temperature'/'precipitation' by page ---")
            for p in sorted(weather_on_page):
                print(f"    page {p}: {weather_on_page[p]} events with temperature/precipitation in title")
        else:
            print(f"\n  [NOTE] No events at all contain 'temperature' or 'precipitation' in title!")
            print(f"         q={keyword!r} search is returning entirely unrelated events.")

        # ── финальный вердикт ──────────────────────────────────────────────────
        passes_keyword = sum(
            1 for ev in all_events
            if any(kw in (ev.get("title") or "").lower() for kw in _WEATHER_KEYWORDS)
        )
        print(f"\n  VERDICT: {passes_keyword}/{len(all_events)} events pass _WEATHER_KEYWORDS filter")
        if passes_keyword == 0:
            pct_has_temp = variant_counts.get("temperature", 0)
            pct_has_high = variant_counts.get("high temperature", 0)
            print(f"  => ALL FILTERED at keyword stage.")
            if pct_has_temp > 0 and pct_has_high > 0:
                print(f"     HYPOTHESIS (a) likely: {pct_has_temp} events have 'temperature',")
                print(f"     {pct_has_high} have 'high temperature' (not 'highest temperature').")
                print(f"     FIX: add 'high temperature' to _WEATHER_KEYWORDS in markets.py")
            elif pct_has_temp == 0:
                print(f"     HYPOTHESIS (b) likely: q={keyword!r} returns unrelated events.")
                print(f"     Weather events may be on pages > {MAX_PAGES}.")
                print(f"     FIX: increase MAX_PAGES here to 25 and re-run, or check page ordering.")

    print("\n" + "=" * 72)
    print("Re-run with MAX_PAGES=25 to check if weather events exist beyond page 5.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
