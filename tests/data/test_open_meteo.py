from datetime import date
from unittest.mock import patch, MagicMock

import pytest

from src.data.open_meteo import _parse_daily, fetch_actual_temperature
from src.db.models import WeatherSnapshot


def _make_fake_response(temp_max_vals: list[float], temp_min_vals: list[float], start_ts: int = 1746057600):
    """Build a mock Open-Meteo FlatBuffers-style response."""
    daily = MagicMock()
    daily.Time.return_value = start_ts
    daily.Interval.return_value = 86400

    max_var = MagicMock()
    max_var.ValuesLength.return_value = len(temp_max_vals)
    max_var.Values.side_effect = lambda i: temp_max_vals[i]

    min_var = MagicMock()
    min_var.ValuesLength.return_value = len(temp_min_vals)
    min_var.Values.side_effect = lambda i: temp_min_vals[i]

    daily.Variables.side_effect = lambda i: max_var if i == 0 else min_var

    response = MagicMock()
    response.Daily.return_value = daily
    return response


def test_parse_daily_returns_correct_count():
    response = _make_fake_response([20.0, 21.0, 19.5], [15.0, 16.0, 14.5])
    snapshots = _parse_daily(response, model="gfs_global", city="New York", days=3)
    assert len(snapshots) == 3
    assert all(isinstance(s, WeatherSnapshot) for s in snapshots)


def test_parse_daily_source_format():
    response = _make_fake_response([20.0], [15.0])
    snapshots = _parse_daily(response, model="ecmwf_ifs025", city="London", days=1)
    assert snapshots[0].source == "open_meteo:ecmwf_ifs025"
    assert snapshots[0].city == "London"
    assert isinstance(snapshots[0].forecast_date, date)
