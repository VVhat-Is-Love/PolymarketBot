"""Tests for daily SQLite backup: sqlite3.backup(), gzip validity, rotation."""
import gzip
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.scripts.backup_db import run_backup, _rotate, _KEEP


def _make_db(path: Path) -> Path:
    """Create a minimal SQLite file with one row for verification."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# run_backup
# ---------------------------------------------------------------------------

class TestRunBackup:
    def test_creates_gz_file(self, tmp_path):
        src = _make_db(tmp_path / "source.db")
        backup_dir = tmp_path / "backups"
        with patch("src.scripts.backup_db._db_path", return_value=src):
            dest = run_backup(backup_dir=backup_dir)
        assert dest.exists()
        assert dest.name.endswith(".db.gz")

    def test_gz_is_valid_and_data_intact(self, tmp_path):
        src = _make_db(tmp_path / "source.db")
        backup_dir = tmp_path / "backups"
        with patch("src.scripts.backup_db._db_path", return_value=src):
            dest = run_backup(backup_dir=backup_dir)
        # Decompress and read back
        restored = tmp_path / "restored.db"
        with gzip.open(dest, "rb") as f_in, open(restored, "wb") as f_out:
            f_out.write(f_in.read())
        conn = sqlite3.connect(str(restored))
        row = conn.execute("SELECT x FROM t").fetchone()
        conn.close()
        assert row[0] == 42

    def test_no_uncompressed_db_left(self, tmp_path):
        src = _make_db(tmp_path / "source.db")
        backup_dir = tmp_path / "backups"
        with patch("src.scripts.backup_db._db_path", return_value=src):
            run_backup(backup_dir=backup_dir)
        assert list(backup_dir.glob("*.db")) == []

    def test_creates_backup_dir_if_missing(self, tmp_path):
        src = _make_db(tmp_path / "source.db")
        backup_dir = tmp_path / "deep" / "nested" / "backups"
        assert not backup_dir.exists()
        with patch("src.scripts.backup_db._db_path", return_value=src):
            run_backup(backup_dir=backup_dir)
        assert backup_dir.exists()

    def test_raises_on_missing_source_db(self, tmp_path):
        with patch("src.scripts.backup_db._db_path", return_value=tmp_path / "ghost.db"):
            with pytest.raises(FileNotFoundError):
                run_backup(backup_dir=tmp_path / "backups")

    def test_backup_filename_contains_timestamp(self, tmp_path):
        src = _make_db(tmp_path / "source.db")
        backup_dir = tmp_path / "backups"
        with patch("src.scripts.backup_db._db_path", return_value=src):
            dest = run_backup(backup_dir=backup_dir)
        # polymarket_YYYYMMDD_HHMMSS.db.gz
        assert dest.name.startswith("polymarket_")
        assert len(dest.stem) == len("polymarket_20240101_120000.db")

    def test_rotation_applied_after_backup(self, tmp_path):
        src = _make_db(tmp_path / "source.db")
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        # Pre-seed with _KEEP existing backups
        for i in range(_KEEP):
            (backup_dir / f"polymarket_202401{i+1:02d}_120000.db.gz").write_bytes(b"old")
        with patch("src.scripts.backup_db._db_path", return_value=src):
            run_backup(backup_dir=backup_dir)
        gz_files = list(backup_dir.glob("polymarket_*.db.gz"))
        assert len(gz_files) == _KEEP


# ---------------------------------------------------------------------------
# _rotate
# ---------------------------------------------------------------------------

class TestRotate:
    def test_keeps_exactly_keep_newest(self, tmp_path):
        for i in range(_KEEP + 2):
            f = tmp_path / f"polymarket_2024010{i}_120000.db.gz"
            f.write_bytes(b"x")
            time.sleep(0.01)  # distinct mtime
        _rotate(tmp_path)
        assert len(list(tmp_path.glob("polymarket_*.db.gz"))) == _KEEP

    def test_oldest_files_deleted(self, tmp_path):
        files = []
        for i in range(_KEEP + 1):
            f = tmp_path / f"polymarket_202401{i+10:02d}_120000.db.gz"
            f.write_bytes(b"x")
            files.append(f)
            time.sleep(0.01)
        _rotate(tmp_path)
        remaining = {p.name for p in tmp_path.glob("polymarket_*.db.gz")}
        assert files[0].name not in remaining   # oldest gone
        assert files[-1].name in remaining      # newest kept

    def test_no_delete_when_at_limit(self, tmp_path):
        for i in range(_KEEP):
            (tmp_path / f"polymarket_2024010{i}_120000.db.gz").write_bytes(b"x")
        _rotate(tmp_path)
        assert len(list(tmp_path.glob("polymarket_*.db.gz"))) == _KEEP

    def test_no_delete_when_below_limit(self, tmp_path):
        for i in range(_KEEP - 1):
            (tmp_path / f"polymarket_2024010{i}_120000.db.gz").write_bytes(b"x")
        _rotate(tmp_path)
        assert len(list(tmp_path.glob("polymarket_*.db.gz"))) == _KEEP - 1

    def test_no_raise_when_empty(self, tmp_path):
        _rotate(tmp_path)  # must not raise
