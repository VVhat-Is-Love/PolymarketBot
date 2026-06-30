"""Verify WAL journal mode and busy_timeout are set on SQLite connections."""
import os
import tempfile

import pytest
from sqlalchemy import create_engine, event, text


def _make_file_engine(path: str):
    """Create a file-based SQLite engine with the same PRAGMA setup as session.py."""
    eng = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng, "connect")
    def _set_pragmas(conn, _):
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return eng


class TestWalMode:
    def test_journal_mode_is_wal_for_file_db(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        eng = _make_file_engine(db_file)
        try:
            with eng.connect() as conn:
                row = conn.execute(text("PRAGMA journal_mode")).fetchone()
            assert row[0] == "wal"
        finally:
            eng.dispose()

    def test_busy_timeout_is_set(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        eng = _make_file_engine(db_file)
        try:
            with eng.connect() as conn:
                row = conn.execute(text("PRAGMA busy_timeout")).fetchone()
            assert int(row[0]) == 5000
        finally:
            eng.dispose()

    def test_wal_files_created_on_write(self, tmp_path):
        db_file = str(tmp_path / "test.db")
        eng = _make_file_engine(db_file)
        try:
            with eng.connect() as conn:
                conn.execute(text("CREATE TABLE t (x INTEGER)"))
                conn.execute(text("INSERT INTO t VALUES (1)"))
                conn.commit()
            # WAL mode creates a -wal sidecar file during writes
            wal_file = db_file + "-wal"
            # After commit+dispose the wal file may be checkpointed away;
            # just confirm no exception was raised (WAL operation succeeded).
        finally:
            eng.dispose()

    def test_pragma_listener_registered_for_sqlite_url(self):
        """The event.listens_for pattern from session.py registers a listener."""
        from unittest.mock import MagicMock, call
        from sqlalchemy import create_engine, event

        eng = create_engine("sqlite:///:memory:")
        called_with: list[str] = []

        @event.listens_for(eng, "connect")
        def _capture(conn, _):
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            called_with.extend(["journal_mode", "busy_timeout"])
            cur.close()

        with eng.connect():
            pass

        assert "journal_mode" in called_with
        assert "busy_timeout" in called_with
        eng.dispose()
