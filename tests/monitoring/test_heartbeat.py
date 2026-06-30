"""Tests for per-thread heartbeat read/write/staleness logic."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from src.monitoring.heartbeat import (
    write_heartbeat,
    read_heartbeat,
    seconds_since,
    is_stale,
    STALE_THRESHOLDS,
)


class TestWriteRead:
    def test_write_creates_file(self, tmp_path):
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            write_heartbeat("scheduler")
        assert (tmp_path / "scheduler.ts").exists()

    def test_write_then_read_returns_utc_datetime(self, tmp_path):
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            write_heartbeat("scheduler")
            result = read_heartbeat("scheduler")
        assert isinstance(result, datetime)
        assert result.tzinfo is not None

    def test_read_missing_returns_none(self, tmp_path):
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            assert read_heartbeat("scheduler") is None

    def test_write_never_raises_on_unwritable_path(self):
        # Should silently swallow any I/O error
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", Path("/no/such/dir/ever")):
            write_heartbeat("scheduler")  # must not raise

    def test_multiple_threads_independent_files(self, tmp_path):
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            write_heartbeat("scheduler")
            write_heartbeat("tail_exit")
        assert (tmp_path / "scheduler.ts").exists()
        assert (tmp_path / "tail_exit.ts").exists()


class TestSecondsSince:
    def test_returns_none_when_no_file(self, tmp_path):
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            assert seconds_since("scheduler") is None

    def test_returns_small_value_just_written(self, tmp_path):
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            write_heartbeat("scheduler")
            elapsed = seconds_since("scheduler")
        assert elapsed is not None
        assert elapsed < 2.0

    def test_returns_correct_elapsed_for_old_timestamp(self, tmp_path):
        old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        (tmp_path / "scheduler.ts").write_text(old)
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            elapsed = seconds_since("scheduler")
        assert elapsed is not None
        assert 118 < elapsed < 125  # allow 5 s drift


class TestIsStale:
    def test_stale_when_no_file(self, tmp_path):
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            assert is_stale("scheduler") is True

    def test_not_stale_just_written(self, tmp_path):
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            write_heartbeat("scheduler")
            assert is_stale("scheduler") is False

    def test_stale_when_older_than_threshold(self, tmp_path):
        # scheduler threshold = 90 s; write a 200 s old timestamp
        old = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
        (tmp_path / "scheduler.ts").write_text(old)
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            assert is_stale("scheduler") is True

    def test_not_stale_just_within_threshold(self, tmp_path):
        # scheduler threshold = 90 s; write an 80 s old timestamp
        recent = (datetime.now(timezone.utc) - timedelta(seconds=80)).isoformat()
        (tmp_path / "scheduler.ts").write_text(recent)
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            assert is_stale("scheduler") is False

    def test_tail_exit_threshold_greater_than_scheduler(self):
        # tail_exit ticks slower so its threshold must be higher
        assert STALE_THRESHOLDS["tail_exit"] > STALE_THRESHOLDS["scheduler"]

    def test_unknown_thread_uses_default_300s(self, tmp_path):
        # A thread not in STALE_THRESHOLDS falls back to 300 s
        old = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
        (tmp_path / "unknown_thread.ts").write_text(old)
        with patch("src.monitoring.heartbeat._HEARTBEAT_DIR", tmp_path):
            assert is_stale("unknown_thread") is True
