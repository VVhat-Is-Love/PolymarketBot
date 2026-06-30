from datetime import date
import pytest

from src.market.parsers import (
    parse_city_from_title,
    parse_date_from_title,
    parse_metric_from_title,
    parse_bin_range,
    parse_station,
)
from src.config.cities import CITIES_WHITELIST


# ---------------------------------------------------------------------------
# parse_city_from_title
# ---------------------------------------------------------------------------

class TestParseCityFromTitle:
    def test_new_york_full(self):
        assert parse_city_from_title(
            "Highest temperature in New York on May 5?", CITIES_WHITELIST
        ) == "New York"

    def test_alias_nyc(self):
        assert parse_city_from_title(
            "Will NYC high temp exceed 80°F?", CITIES_WHITELIST
        ) == "New York"

    def test_alias_la(self):
        assert parse_city_from_title(
            "Highest temperature in LA on May 5?", CITIES_WHITELIST
        ) == "Los Angeles"

    def test_los_angeles_full(self):
        assert parse_city_from_title(
            "Highest temperature in Los Angeles on May 10?", CITIES_WHITELIST
        ) == "Los Angeles"

    def test_seoul(self):
        assert parse_city_from_title(
            "Highest temperature in Seoul on May 5?", CITIES_WHITELIST
        ) == "Seoul"

    def test_hong_kong_alias(self):
        assert parse_city_from_title(
            "Will HK reach 35°C on June 1?", CITIES_WHITELIST
        ) == "Hong Kong"

    def test_no_match(self):
        assert parse_city_from_title("Will Bitcoin hit 100k?", CITIES_WHITELIST) is None

    def test_case_insensitive(self):
        assert parse_city_from_title(
            "highest temp in CHICAGO on may 5", CITIES_WHITELIST
        ) == "Chicago"

    # ── G4-14 regression: 'LA' alias must NOT match as a substring ──────────
    # These titles previously returned "Los Angeles" because "la" is a
    # substring of miLAn / daLLas / atLAnta / kuaLA / maniLA, routing real
    # orders to the wrong market.
    def test_milan_not_los_angeles(self):
        assert parse_city_from_title(
            "Will the highest temperature in Milan be 33°C on May 31?",
            CITIES_WHITELIST,
        ) == "Milan"

    def test_dallas_not_los_angeles(self):
        assert parse_city_from_title(
            "Will the highest temperature in Dallas be 55°F or below?",
            CITIES_WHITELIST,
        ) == "Dallas"

    def test_atlanta_not_los_angeles(self):
        assert parse_city_from_title(
            "Will the highest temperature in Atlanta be 59°F?",
            CITIES_WHITELIST,
        ) == "Atlanta"

    def test_manila_not_los_angeles(self):
        assert parse_city_from_title(
            "Will the highest temperature in Manila be 30°C?",
            CITIES_WHITELIST,
        ) == "Manila"

    def test_kuala_lumpur_not_los_angeles(self):
        assert parse_city_from_title(
            "Will the highest temperature in Kuala Lumpur be 26°C?",
            CITIES_WHITELIST,
        ) == "Kuala Lumpur"

    def test_exact_name_beats_alias_substring(self):
        # 'Milan' (exact whole word) must win over 'LA' alias even though
        # whitelist iteration order is arbitrary.
        for title, expect in [
            ("highest temperature in milan", "Milan"),
            ("highest temperature in la", "Los Angeles"),
        ]:
            assert parse_city_from_title(title, CITIES_WHITELIST) == expect


# ---------------------------------------------------------------------------
# parse_date_from_title
# ---------------------------------------------------------------------------

class TestParseDateFromTitle:
    def test_with_full_year(self):
        assert parse_date_from_title("on May 5, 2026") == date(2026, 5, 5)

    def test_with_year_no_comma(self):
        assert parse_date_from_title("on May 5 2026") == date(2026, 5, 5)

    def test_without_year_returns_date(self):
        result = parse_date_from_title("Highest temperature in NYC on May 5?")
        assert result is not None
        assert result.month == 5
        assert result.day == 5

    def test_ordinal_suffix(self):
        result = parse_date_from_title("temperature on May 5th, 2026")
        assert result == date(2026, 5, 5)

    def test_no_date_returns_none(self):
        assert parse_date_from_title("Will it rain tomorrow?") is None

    def test_december(self):
        assert parse_date_from_title("on December 31, 2026") == date(2026, 12, 31)


# ---------------------------------------------------------------------------
# parse_metric_from_title
# ---------------------------------------------------------------------------

class TestParseMetricFromTitle:
    def test_highest(self):
        assert parse_metric_from_title("Highest temperature in Seoul") == "high_temp"

    def test_high_temp(self):
        assert parse_metric_from_title("Will the high temp be above 80?") == "high_temp"

    def test_lowest(self):
        assert parse_metric_from_title("Lowest temperature in Chicago") == "low_temp"

    def test_low_temp(self):
        assert parse_metric_from_title("Will the low temp drop below 40?") == "low_temp"

    def test_precipitation(self):
        assert parse_metric_from_title("Total precipitation in New York") == "precipitation"

    def test_rainfall(self):
        assert parse_metric_from_title("Will rainfall exceed 1 inch?") == "precipitation"

    def test_unknown_returns_none(self):
        assert parse_metric_from_title("Will Bitcoin reach 100k?") is None


# ---------------------------------------------------------------------------
# parse_bin_range
# ---------------------------------------------------------------------------

class TestParseBinRange:
    def test_range(self):
        assert parse_bin_range("82-83°F") == (82.0, 83.0)

    def test_range_with_en_dash(self):
        assert parse_bin_range("82–83°F") == (82.0, 83.0)

    def test_above(self):
        assert parse_bin_range("above 90°F") == (90.0, None)

    def test_over(self):
        assert parse_bin_range("over 90°F") == (90.0, None)

    def test_at_least(self):
        assert parse_bin_range("at least 85°F") == (85.0, None)

    def test_below(self):
        assert parse_bin_range("below 60°F") == (None, 60.0)

    def test_under(self):
        assert parse_bin_range("under 32°F") == (None, 32.0)

    def test_no_range(self):
        assert parse_bin_range("some weather text") == (None, None)

    def test_decimal(self):
        assert parse_bin_range("2.5-3.0 inches") == (2.5, 3.0)

    # International point-estimate formats
    def test_point_celsius(self):
        assert parse_bin_range("6°C") == (5.5, 6.5)

    def test_point_celsius_no_symbol(self):
        lo, hi = parse_bin_range("15C")
        assert lo == pytest.approx(14.5) and hi == pytest.approx(15.5)

    def test_or_higher_suffix(self):
        assert parse_bin_range("15°C or higher") == (15.0, None)

    def test_or_above_suffix(self):
        assert parse_bin_range("20°C or above") == (20.0, None)

    def test_or_below_suffix(self):
        assert parse_bin_range("5°C or below") == (None, 5.0)

    def test_or_under_suffix(self):
        assert parse_bin_range("3°C or under") == (None, 3.0)


# ---------------------------------------------------------------------------
# parse_station
# ---------------------------------------------------------------------------

class TestParseStation:
    def test_in_parentheses(self):
        assert parse_station("Miami Intl Airport (KMIA) is the source.") == "KMIA"

    def test_standalone(self):
        assert parse_station("Data from KORD station in Chicago.") == "KORD"

    def test_heathrow(self):
        assert parse_station("Measured at EGLL (London Heathrow).") == "EGLL"

    def test_no_station(self):
        assert parse_station("No station mentioned here.") is None

    def test_empty_string(self):
        assert parse_station("") is None

    def test_none(self):
        assert parse_station(None) is None  # type: ignore[arg-type]
