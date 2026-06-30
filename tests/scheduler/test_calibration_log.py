"""Tests for _job_calibration_log and _job_calibration_resolve."""
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
    def _run(self, groups, wx_rows_by_group, markets_by_group, snaps_by_group,
             active_tails=None):
        """
        Run _job_calibration_log() with mocked DB queries.
        Returns the list of CalibrationLog rows passed to session.add_all().

        active_tails: list of (city, bin_label) tuples representing open tail positions.
        """
        from src.scheduler import _job_calibration_log

        captured: list[CalibrationLog] = []
        _active_tails = active_tails or []

        def fake_execute(stmt, *a, **kw):
            result = MagicMock()
            stmt_str = str(stmt)
            if "market_groups" in stmt_str:
                result.scalars.return_value.all.return_value = groups
            elif "weather_snapshots" in stmt_str:
                result.scalars.return_value.all.return_value = list(wx_rows_by_group.values())[0]
            elif "live_trades" in stmt_str:
                # Returns (city, bin_label) tuples for the pre-load query.
                result.all.return_value = _active_tails
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

    # ------------------------------------------------------------------
    # bin_distance_sigma
    # ------------------------------------------------------------------

    def test_bin_distance_sigma_above_consensus(self):
        """Bin entirely above consensus: near boundary = bin_min."""
        # consensus = 90°F, sigma ≈ tail_sigma_c(2.0) * 9/5 = 3.6°F
        # bin 94-95°F → near = 94, dist = 4, dist/sigma ≈ 1.11
        group = _make_group(unit="F")
        markets = [_make_market("m1", "94-95°F", 94.0, 95.0)]
        wx = [_make_wx("open_meteo:ecmwf_ifs025", 90.0)]
        rows = self._run(
            groups=[group],
            wx_rows_by_group={"grp-1": wx},
            markets_by_group={"grp-1": markets},
            snaps_by_group={"grp-1": []},
        )
        assert len(rows) == 1
        ds = rows[0].bin_distance_sigma
        assert ds is not None
        # dist = 94 - 90 = 4, sigma = 2.0 * 1.8 = 3.6 → 4/3.6 ≈ 1.111
        assert abs(ds - 4.0 / 3.6) < 0.01

    def test_bin_distance_sigma_below_consensus(self):
        """Bin entirely below consensus: near boundary = bin_max."""
        # consensus = 90°F, bin 84-85°F → near = 85, dist = 5, dist/sigma ≈ 1.39
        group = _make_group(unit="F")
        markets = [_make_market("m1", "84-85°F", 84.0, 85.0)]
        wx = [_make_wx("open_meteo:ecmwf_ifs025", 90.0)]
        rows = self._run(
            groups=[group],
            wx_rows_by_group={"grp-1": wx},
            markets_by_group={"grp-1": markets},
            snaps_by_group={"grp-1": []},
        )
        assert len(rows) == 1
        ds = rows[0].bin_distance_sigma
        assert ds is not None
        # near = bin_max = 85, dist = 90-85 = 5, sigma = 3.6 → 5/3.6 ≈ 1.389
        assert abs(ds - 5.0 / 3.6) < 0.01

    def test_bin_distance_sigma_none_when_no_bin_bounds(self):
        """If market has no bin_min, bin_distance_sigma stays None."""
        group = _make_group()
        m = _make_market("m1", "88-89°F")
        m.bin_min = None
        m.bin_max = None
        wx = [_make_wx("open_meteo:ecmwf_ifs025", 90.0)]
        rows = self._run(
            groups=[group],
            wx_rows_by_group={"grp-1": wx},
            markets_by_group={"grp-1": [m]},
            snaps_by_group={"grp-1": []},
        )
        assert rows[0].bin_distance_sigma is None

    # ------------------------------------------------------------------
    # bot_traded
    # ------------------------------------------------------------------

    def test_bot_traded_true_when_active_position(self):
        group = _make_group(city="Miami")
        markets = [_make_market("m1", "94-95°F", 94.0, 95.0)]
        wx = [_make_wx("open_meteo:ecmwf_ifs025", 90.0)]
        rows = self._run(
            groups=[group],
            wx_rows_by_group={"grp-1": wx},
            markets_by_group={"grp-1": markets},
            snaps_by_group={"grp-1": []},
            active_tails=[("Miami", "94-95°F")],
        )
        assert rows[0].bot_traded is True

    def test_bot_traded_false_when_no_active_position(self):
        group = _make_group(city="Miami")
        markets = [_make_market("m1", "94-95°F", 94.0, 95.0)]
        wx = [_make_wx("open_meteo:ecmwf_ifs025", 90.0)]
        rows = self._run(
            groups=[group],
            wx_rows_by_group={"grp-1": wx},
            markets_by_group={"grp-1": markets},
            snaps_by_group={"grp-1": []},
            active_tails=[],
        )
        assert rows[0].bot_traded is False

    def test_bot_traded_only_matches_own_city_and_bin(self):
        """Active tail in a different city must not set bot_traded=True."""
        group = _make_group(city="Miami")
        markets = [_make_market("m1", "94-95°F", 94.0, 95.0)]
        wx = [_make_wx("open_meteo:ecmwf_ifs025", 90.0)]
        rows = self._run(
            groups=[group],
            wx_rows_by_group={"grp-1": wx},
            markets_by_group={"grp-1": markets},
            snaps_by_group={"grp-1": []},
            active_tails=[("Dallas", "94-95°F")],   # different city
        )
        assert rows[0].bot_traded is False


# ---------------------------------------------------------------------------
# _job_calibration_resolve
# ---------------------------------------------------------------------------

class TestJobCalibrationResolve:
    """Tests for _job_calibration_resolve: backfills CalibrationLog with outcomes."""

    def _make_cal_row(self, city="Miami", forecast_date=None, bin_label="94-95°F",
                      model_p_no=0.90, resolved_at=None):
        row = MagicMock(spec=CalibrationLog)
        row.city = city
        row.forecast_date = forecast_date or date(2026, 6, 10)
        row.bin_label = bin_label
        row.model_p_no = model_p_no
        row.resolved_at = resolved_at
        row.resolved_outcome = None
        row.actual_temp_resolve = None
        row.bot_traded = False
        return row

    def _make_market_orm(self, market_id, bin_label, group_id="grp-1"):
        m = MagicMock()
        m.market_id = market_id
        m.bin_label = bin_label
        m.group_id = group_id
        return m

    def _run_resolve(self, unresolved_pairs, group, markets_in_group,
                     cal_rows, is_resolved=True, winning_mid="mkt-winner",
                     actual_temp_c=None, traded_bins=None):
        """Run _job_calibration_resolve with mocked dependencies."""
        from src.scheduler import _job_calibration_resolve

        def fake_execute(stmt, *a, **kw):
            result = MagicMock()
            stmt_str = str(stmt)
            if "calibration_log" in stmt_str and "distinct" in stmt_str.lower():
                result.all.return_value = unresolved_pairs
            elif "calibration_log" in stmt_str:
                result.scalars.return_value.all.return_value = cal_rows
            elif "market_groups" in stmt_str:
                result.scalar_one_or_none.return_value = group
            elif "markets" in stmt_str:
                result.scalars.return_value.all.return_value = markets_in_group
            elif "live_trades" in stmt_str:
                result.scalars.return_value.all.return_value = list(traded_bins or [])
            else:
                result.scalars.return_value.all.return_value = []
            return result

        mock_session = MagicMock()
        mock_session.execute.side_effect = fake_execute

        mock_gamma = MagicMock()
        mock_gamma.is_resolved_event.return_value = is_resolved
        mock_gamma.winning_market_id.return_value = winning_mid

        with patch("src.scheduler.get_session", return_value=mock_session), \
             patch("src.scheduler._gamma", mock_gamma), \
             patch("src.scheduler.fetch_actual_temperature" if False else
                   "src.data.open_meteo.fetch_actual_temperature",
                   return_value=actual_temp_c):
            _job_calibration_resolve()

        return cal_rows

    def test_stamps_resolved_outcome_true_for_winning_bin(self):
        """Bin whose market_id == winning_mid gets resolved_outcome=True."""
        pair = ("Miami", date(2026, 6, 10))
        group = _make_group(city="Miami", resolution_date=date(2026, 6, 10), unit="F")
        group.group_id = "grp-1"
        group.metric = "high_temp"
        markets = [
            self._make_market_orm("mkt-winner", "94-95°F"),
            self._make_market_orm("mkt-other", "93-94°F"),
        ]
        rows = [
            self._make_cal_row(bin_label="94-95°F"),
            self._make_cal_row(bin_label="93-94°F"),
        ]

        from src.scheduler import _job_calibration_resolve
        from unittest.mock import patch, MagicMock

        def fake_execute(stmt, *a, **kw):
            result = MagicMock()
            stmt_str = str(stmt)
            if "calibration_log" in stmt_str and "distinct" in stmt_str.lower():
                result.all.return_value = [pair]
            elif "calibration_log" in stmt_str:
                result.scalars.return_value.all.return_value = rows
            elif "market_groups" in stmt_str:
                result.scalar_one_or_none.return_value = group
            elif "markets" in stmt_str:
                result.scalars.return_value.all.return_value = markets
            elif "live_trades" in stmt_str:
                result.scalars.return_value.all.return_value = []
            else:
                result.scalars.return_value.all.return_value = []
            return result

        mock_session = MagicMock()
        mock_session.execute.side_effect = fake_execute
        mock_gamma = MagicMock()
        mock_gamma.is_resolved_event.return_value = True
        mock_gamma.winning_market_id.return_value = "mkt-winner"

        with patch("src.scheduler.get_session", return_value=mock_session), \
             patch("src.scheduler._gamma", mock_gamma), \
             patch("src.data.open_meteo.fetch_actual_temperature", return_value=37.8):
            _job_calibration_resolve()

        winning_row = next(r for r in rows if r.bin_label == "94-95°F")
        losing_row = next(r for r in rows if r.bin_label == "93-94°F")
        assert winning_row.resolved_outcome is True
        assert losing_row.resolved_outcome is False

    def test_resolved_at_and_temp_stamped(self):
        """resolved_at is set and actual_temp_resolve is in native unit (°F)."""
        pair = ("Miami", date(2026, 6, 10))
        group = _make_group(city="Miami", resolution_date=date(2026, 6, 10), unit="F")
        group.group_id = "grp-1"
        group.metric = "high_temp"
        markets = [self._make_market_orm("mkt-winner", "94-95°F")]
        row = self._make_cal_row(bin_label="94-95°F")

        from src.scheduler import _job_calibration_resolve

        def fake_execute(stmt, *a, **kw):
            result = MagicMock()
            stmt_str = str(stmt)
            if "calibration_log" in stmt_str and "distinct" in stmt_str.lower():
                result.all.return_value = [pair]
            elif "calibration_log" in stmt_str:
                result.scalars.return_value.all.return_value = [row]
            elif "market_groups" in stmt_str:
                result.scalar_one_or_none.return_value = group
            elif "markets" in stmt_str:
                result.scalars.return_value.all.return_value = markets
            elif "live_trades" in stmt_str:
                result.scalars.return_value.all.return_value = []
            else:
                result.scalars.return_value.all.return_value = []
            return result

        mock_session = MagicMock()
        mock_session.execute.side_effect = fake_execute
        mock_gamma = MagicMock()
        mock_gamma.is_resolved_event.return_value = True
        mock_gamma.winning_market_id.return_value = "mkt-winner"

        # Open-Meteo returns °C; unit=F → should convert to °F
        with patch("src.scheduler.get_session", return_value=mock_session), \
             patch("src.scheduler._gamma", mock_gamma), \
             patch("src.data.open_meteo.fetch_actual_temperature", return_value=35.0):
            _job_calibration_resolve()

        assert row.resolved_at is not None
        # 35°C → 95°F
        assert row.actual_temp_resolve is not None
        assert abs(row.actual_temp_resolve - 95.0) < 0.01

    def test_skips_when_not_yet_resolved(self):
        """If Gamma says market not resolved, rows stay untouched."""
        pair = ("Miami", date(2026, 6, 10))
        group = _make_group(city="Miami", resolution_date=date(2026, 6, 10))
        group.group_id = "grp-1"
        row = self._make_cal_row()

        from src.scheduler import _job_calibration_resolve

        def fake_execute(stmt, *a, **kw):
            result = MagicMock()
            stmt_str = str(stmt)
            if "calibration_log" in stmt_str and "distinct" in stmt_str.lower():
                result.all.return_value = [pair]
            elif "market_groups" in stmt_str:
                result.scalar_one_or_none.return_value = group
            else:
                result.scalars.return_value.all.return_value = []
            return result

        mock_session = MagicMock()
        mock_session.execute.side_effect = fake_execute
        mock_gamma = MagicMock()
        mock_gamma.is_resolved_event.return_value = False  # not resolved yet

        with patch("src.scheduler.get_session", return_value=mock_session), \
             patch("src.scheduler._gamma", mock_gamma):
            _job_calibration_resolve()

        assert row.resolved_at is None
        assert row.resolved_outcome is None

    def test_bot_traded_upgraded_at_resolution(self):
        """If a LiveTrade exists for this group/bin, bot_traded is promoted to True."""
        pair = ("Miami", date(2026, 6, 10))
        group = _make_group(city="Miami", resolution_date=date(2026, 6, 10), unit="F")
        group.group_id = "grp-1"
        group.metric = "high_temp"
        markets = [self._make_market_orm("mkt-winner", "94-95°F")]
        row = self._make_cal_row(bin_label="94-95°F")
        row.bot_traded = False  # was False at snapshot time (trade placed later)

        from src.scheduler import _job_calibration_resolve

        def fake_execute(stmt, *a, **kw):
            result = MagicMock()
            stmt_str = str(stmt)
            if "calibration_log" in stmt_str and "distinct" in stmt_str.lower():
                result.all.return_value = [pair]
            elif "calibration_log" in stmt_str:
                result.scalars.return_value.all.return_value = [row]
            elif "market_groups" in stmt_str:
                result.scalar_one_or_none.return_value = group
            elif "markets" in stmt_str:
                result.scalars.return_value.all.return_value = markets
            elif "live_trades" in stmt_str:
                # Bot placed a trade on this bin after the snapshot was written
                result.scalars.return_value.all.return_value = ["94-95°F"]
            else:
                result.scalars.return_value.all.return_value = []
            return result

        mock_session = MagicMock()
        mock_session.execute.side_effect = fake_execute
        mock_gamma = MagicMock()
        mock_gamma.is_resolved_event.return_value = True
        mock_gamma.winning_market_id.return_value = "mkt-winner"

        with patch("src.scheduler.get_session", return_value=mock_session), \
             patch("src.scheduler._gamma", mock_gamma), \
             patch("src.data.open_meteo.fetch_actual_temperature", return_value=35.0):
            _job_calibration_resolve()

        assert row.bot_traded is True

    def test_skips_when_no_unresolved_pairs(self):
        """If all rows are already resolved, job exits early without Gamma calls."""
        from src.scheduler import _job_calibration_resolve

        def fake_execute(stmt, *a, **kw):
            result = MagicMock()
            result.all.return_value = []
            return result

        mock_session = MagicMock()
        mock_session.execute.side_effect = fake_execute
        mock_gamma = MagicMock()

        with patch("src.scheduler.get_session", return_value=mock_session), \
             patch("src.scheduler._gamma", mock_gamma):
            _job_calibration_resolve()

        mock_gamma.is_resolved_event.assert_not_called()
