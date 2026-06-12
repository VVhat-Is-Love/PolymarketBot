"""Tests for _job_calibration_log: model↔market calibration logging."""
from datetime import datetime, date
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.db.models import Base, CalibrationLog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


def _make_group(city="Miami", resolution_date=None, unit="F"):
    g = MagicMock()
    g.group_id = "grp-1"
    g.city = city
    g.resolution_date = resolution_date or date(2026, 6, 15)
    g.unit = unit
    return g


def _make_market(market_id="mkt-1", bin_label="88-89°F", bin_min=88.0, bin_max=89.0, volume=5000.0):
    m = MagicMock()
    m.market_id = market_id
    m.bin_label = bin_label
    m.bin_min = bin_min
    m.bin_max = bin_max
    m.is_active = True
    m.volume = volume
    return m


def _make_wx(source="open_meteo:ecmwf_ifs025", temp_max=90.0, city="Miami", fdate=None):
    wx = MagicMock()
    wx.source = source
    wx.temp_forecast_max = temp_max
    wx.city = city
    wx.forecast_date = fdate or date(2026, 6, 15)
    wx.snapshot_time = datetime(2026, 6, 12, 10, 0)
    return wx


def _make_snap(market_id="mkt-1", price_yes=0.12, price_no=0.88):
    s = MagicMock()
    s.market_id = market_id
    s.price_yes = price_yes
    s.price_no = price_no
    s.snapshot_time = datetime(2026, 6, 12, 10, 5)
    return s


# ---------------------------------------------------------------------------
# ORM model smoke test
# ---------------------------------------------------------------------------

class TestCalibrationLogModel:
    def test_table_created_and_insertable(self):
        session = _make_session()
        row = CalibrationLog(
            timestamp=datetime(2026, 6, 12, 10, 0),
            city="Miami",
            forecast_date=date(2026, 6, 15),
            bin_label="88-89°F",
            model_p_no=0.87,
            market_no_ask=0.88,
            market_yes_price=0.12,
            volume_usd=5000.0,
            models_count=3,
        )
        session.add(row)
        session.commit()

        fetched = session.execute(select(CalibrationLog)).scalars().all()
        assert len(fetched) == 1
        r = fetched[0]
        assert r.city == "Miami"
        assert r.bin_label == "88-89°F"
        assert abs(r.model_p_no - 0.87) < 1e-6
        assert r.models_count == 3
        session.close()

    def test_nullable_fields_accepted(self):
        session = _make_session()
        row = CalibrationLog(
            timestamp=datetime(2026, 6, 12, 10, 0),
            city="Chicago",
            forecast_date=date(2026, 6, 15),
            bin_label="82-83°F",
            model_p_no=None,
            market_no_ask=None,
            market_yes_price=None,
            volume_usd=None,
            models_count=None,
        )
        session.add(row)
        session.commit()
        fetched = session.execute(select(CalibrationLog)).scalars().all()
        assert fetched[0].model_p_no is None
        session.close()


# ---------------------------------------------------------------------------
# _job_calibration_log integration test
# ---------------------------------------------------------------------------

class TestJobCalibrationLog:
    def _run(self, groups, wx_rows_by_group, markets_by_group, snaps_by_group):
        """
        Run _job_calibration_log() with mocked DB queries.
        Returns the list of CalibrationLog rows passed to session.add_all().
        """
        from src.scheduler import _job_calibration_log

        captured: list[CalibrationLog] = []

        def fake_execute(stmt, *a, **kw):
            result = MagicMock()
            # Determine which query this is by inspecting the statement
            stmt_str = str(stmt)
            if "market_groups" in stmt_str:
                result.scalars.return_value.all.return_value = groups
            elif "weather_snapshots" in stmt_str:
                # Return wx for the current group (all groups share same wx here)
                result.scalars.return_value.all.return_value = list(wx_rows_by_group.values())[0]
            elif "markets" in stmt_str and "calibration" not in stmt_str:
                result.scalars.return_value.all.return_value = list(markets_by_group.values())[0]
            elif "market_snapshots" in stmt_str:
                result.scalars.return_value.all.return_value = list(snaps_by_group.values())[0]
            else:
                result.scalars.return_value.all.return_value = []
            return result

        mock_session = MagicMock()
        mock_session.execute.side_effect = fake_execute
        mock_session.add_all.side_effect = lambda rows: captured.extend(rows)

        with patch("src.scheduler.get_session", return_value=mock_session):
            _job_calibration_log()

        return captured

    def test_writes_rows_per_bin(self):
        group = _make_group()
        markets = [
            _make_market("m1", "88-89°F", 88.0, 89.0),
            _make_market("m2", "89-90°F", 89.0, 90.0),
            _make_market("m3", "90-91°F", 90.0, 91.0),
        ]
        wx = [
            _make_wx("open_meteo:ecmwf_ifs025", 90.0),
            _make_wx("open_meteo:gfs_global", 91.0),
            _make_wx("open_meteo:icon_global", 89.5),
        ]
        snaps = [_make_snap("m1", 0.40, 0.60), _make_snap("m2", 0.20, 0.80), _make_snap("m3", 0.10, 0.90)]

        rows = self._run(
            groups=[group],
            wx_rows_by_group={"grp-1": wx},
            markets_by_group={"grp-1": markets},
            snaps_by_group={"grp-1": snaps},
        )

        assert len(rows) == 3
        cities = {r.city for r in rows}
        assert cities == {"Miami"}
        # model_p_no should be populated for all rows
        assert all(r.model_p_no is not None for r in rows)
        # models_count = 3 (three wx sources)
        assert all(r.models_count == 3 for r in rows)

    def test_skips_group_with_no_weather(self):
        group = _make_group()
        markets = [_make_market()]
        rows = self._run(
            groups=[group],
            wx_rows_by_group={"grp-1": []},   # no weather data
            markets_by_group={"grp-1": markets},
            snaps_by_group={"grp-1": []},
        )
        assert rows == []

    def test_market_no_snap_writes_none_prices(self):
        group = _make_group()
        markets = [_make_market("m1", "88-89°F", 88.0, 89.0)]
        wx = [_make_wx("open_meteo:ecmwf_ifs025", 92.0)]
        # No snapshot for this market
        rows = self._run(
            groups=[group],
            wx_rows_by_group={"grp-1": wx},
            markets_by_group={"grp-1": markets},
            snaps_by_group={"grp-1": []},
        )
        assert len(rows) == 1
        assert rows[0].market_yes_price is None
        assert rows[0].market_no_ask is None
        # model_p_no still computed from wx
        assert rows[0].model_p_no is not None
