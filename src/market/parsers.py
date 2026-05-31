import re
from datetime import date, datetime

# ---------------------------------------------------------------------------
# Canonical city name (G3-5)
# ---------------------------------------------------------------------------
# Maps lowercase raw names / aliases → canonical name used in CITIES_WHITELIST.
# Applied at three points: weather write, market-title parse, forecast read.
CITY_CANONICAL: dict[str, str] = {
    "nyc": "New York",
    "new york city": "New York",
    "new york": "New York",
    "ny": "New York",
    "sao paulo": "Sao Paulo",
    "são paulo": "Sao Paulo",
    "kuala lumpur": "Kuala Lumpur",
    "buenos aires": "Buenos Aires",
    "hong kong": "Hong Kong",
    "cape town": "Cape Town",
    "los angeles": "Los Angeles",
    "san francisco": "San Francisco",
    "mexico city": "Mexico City",
    "panama city": "Panama City",
}


def canonical_city(raw: str) -> str:
    """Return the canonical city name for whitelist matching.

    Strips whitespace, lower-cases, looks up in CITY_CANONICAL; falls back to
    title-casing the raw value if not found (keeps existing whitelist entries
    like 'Amsterdam' unchanged).
    """
    cleaned = raw.strip()
    return CITY_CANONICAL.get(cleaned.lower(), cleaned)

# ---------------------------------------------------------------------------
# Month name → number mapping
# ---------------------------------------------------------------------------
_MONTHS: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Date patterns: group 1 = month name, group 2 = day, group 3 = year (optional)
_DATE_WITH_YEAR = re.compile(
    r"\b(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.IGNORECASE
)
_DATE_NO_YEAR = re.compile(
    r"\b(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.IGNORECASE
)

# ICAO station codes: 4 uppercase letters not in common-word skip list
_ICAO_RE = re.compile(r"\b([A-Z]{4})\b")
_SKIP_WORDS = {
    "WHEN", "WILL", "THIS", "THAT", "WHAT", "HIGH", "WITH", "FROM",
    "THAN", "YORK", "CITY", "TEMP", "OVER", "RAIN", "SNOW", "WEEK",
    "THAN", "EACH", "ALSO", "BEEN", "WERE", "HAVE", "YEAR",
}

# Bin range patterns
_BIN_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)")
# keyword-before-number: "above 90°F", "below 60°F"
_BIN_ABOVE = re.compile(r"(?:above|over|at\s+least)\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
_BIN_BELOW = re.compile(r"(?:below|under|less\s+than)\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
# keyword-after-number (international format): "15°C or higher", "5°C or below"
_BIN_ABOVE_SUFFIX = re.compile(
    r"(\d+(?:\.\d+)?)\s*°?[CFcf]?\s+(?:or\s+(?:higher|above|more)|and\s+(?:above|higher|more))",
    re.IGNORECASE,
)
_BIN_BELOW_SUFFIX = re.compile(
    r"(\d+(?:\.\d+)?)\s*°?[CFcf]?\s+(?:or\s+(?:below|under|less)|and\s+(?:below|under|less))",
    re.IGNORECASE,
)
# single point value: "6°C", "72°F" → treated as half-degree intervals
_BIN_POINT = re.compile(r"^(\d+(?:\.\d+)?)\s*°?[CFcf]?\s*$")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _word_in(term: str, text: str) -> bool:
    """
    True if `term` appears in `text` as a standalone word (both lower-cased).

    Uses letter-boundary lookarounds rather than plain substring so short
    aliases like 'la' do NOT match inside other city names:
        'la' in 'milan'  → substring match (WRONG, caused Milan→Los Angeles)
        _word_in('la', 'milan') → False  (preceded by a letter)
        _word_in('la', 'temp in la on may') → True
    """
    if not term:
        return False
    return re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", text) is not None


def parse_city_from_title(title: str, whitelist: dict) -> str | None:
    """'Highest temperature in Seoul on May 5?' → 'Seoul' (canonical).

    Matching priority (CRITICAL — prevents wrong-market trades):
      1. Exact canonical city name as a whole word, across the WHOLE whitelist.
         This guarantees 'Milan' wins over the 'LA' alias substring inside it.
      2. City aliases as whole words (e.g. 'NYC', 'HK').
      3. CITY_CANONICAL fallback table.
    """
    t = title.lower()

    # Pass 1: exact canonical city name (whole-word) — highest priority
    for city in whitelist:
        if _word_in(city.lower(), t):
            return city

    # Pass 2: aliases (whole-word)
    for city, cfg in whitelist.items():
        for alias in cfg.get("aliases", []):
            if _word_in(alias.lower(), t):
                return city

    # Pass 3: CITY_CANONICAL table (whole-word) for titles like "highest temp in NYC"
    for raw, canonical in CITY_CANONICAL.items():
        if _word_in(raw, t) and canonical in whitelist:
            return canonical
    return None


def parse_date_from_title(title: str) -> date | None:
    """
    'Highest temperature in NYC on May 5, 2026?' → date(2026, 5, 5)
    'on May 5' → date(current_or_next_year, 5, 5)
    """
    today = date.today()

    m = _DATE_WITH_YEAR.search(title)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                pass

    m = _DATE_NO_YEAR.search(title)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            try:
                d = date(today.year, month, int(m.group(2)))
                if d < today:
                    d = date(today.year + 1, month, int(m.group(2)))
                return d
            except ValueError:
                pass

    return None


def parse_metric_from_title(title: str) -> str | None:
    """
    'Highest temperature in Seoul' → 'high_temp'
    'Lowest temperature in Chicago' → 'low_temp'
    'Total precipitation in NYC' → 'precipitation'
    """
    t = title.lower()
    if any(k in t for k in ("highest", "high temp", "maximum", "max temp")):
        return "high_temp"
    if any(k in t for k in ("lowest", "low temp", "minimum", "min temp")):
        return "low_temp"
    if any(k in t for k in ("precipitation", "rainfall", "snowfall")):
        return "precipitation"
    return None


def parse_bin_range(label: str) -> tuple[float | None, float | None]:
    """
    '82-83°F'        → (82.0, 83.0)
    'above 90°F'     → (90.0, None)
    'below 60°F'     → (None, 60.0)
    '15°C or higher' → (15.0, None)
    '5°C or below'   → (None, 5.0)
    '6°C'            → (5.5, 6.5)   # point bin: ±0.5 interval
    ''               → (None, None)
    """
    # Range: "40-41°F"
    m = _BIN_RANGE.search(label)
    if m:
        return float(m.group(1)), float(m.group(2))

    # Prefix above: "above 90°F"
    m = _BIN_ABOVE.search(label)
    if m:
        return float(m.group(1)), None

    # Suffix above: "15°C or higher"
    m = _BIN_ABOVE_SUFFIX.search(label)
    if m:
        return float(m.group(1)), None

    # Prefix below: "below 60°F"
    m = _BIN_BELOW.search(label)
    if m:
        return None, float(m.group(1))

    # Suffix below: "5°C or below"
    m = _BIN_BELOW_SUFFIX.search(label)
    if m:
        return None, float(m.group(1))

    # Single point value: "6°C" → half-degree interval [5.5, 6.5]
    m = _BIN_POINT.match(label)
    if m:
        v = float(m.group(1))
        return v - 0.5, v + 0.5

    return None, None


def parse_station(description: str) -> str | None:
    """
    'Miami Intl Airport (KMIA) is used.' → 'KMIA'
    'Data sourced from KORD in Chicago.' → 'KORD'
    """
    if not description:
        return None
    for token in _ICAO_RE.findall(description):
        if token not in _SKIP_WORDS:
            return token
    return None
