"""
Daily SQLite backup: sqlite3.connect().backup() + gzip, rotate to keep 3 copies.

sqlite3.backup() is WAL-safe: it reads a consistent snapshot page-by-page
without blocking the main bot (unlike cp, which can capture a torn WAL state).

Usage:
    python -m src.scripts.backup_db [--backup-dir /path/to/backups]

Intended to run once daily via systemd timer as user 'bot'.
"""
from __future__ import annotations

import argparse
import gzip
import logging
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [backup] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_DEFAULT_BACKUP_DIR = Path("/opt/polymarket-bot/backups")
_KEEP = 3          # number of .db.gz files to retain after rotation
_PAGES = 256       # pages per backup step — limits I/O pressure on live DB


def _db_path() -> Path:
    """Resolve SQLite DB path from application settings."""
    from src.config.settings import settings
    url = settings.database_url
    if not url.startswith("sqlite"):
        raise RuntimeError(f"backup_db: not a SQLite database ({url!r})")
    raw = url.split("sqlite:///")[1]
    return Path(raw).resolve()


def run_backup(backup_dir: Path | None = None) -> Path:
    """
    Backup the live SQLite DB to backup_dir, compress with gzip, rotate old files.
    Returns path of the newly created .db.gz file.
    """
    if backup_dir is None:
        backup_dir = _DEFAULT_BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)

    src_db = _db_path()
    if not src_db.exists():
        raise FileNotFoundError(f"Source DB not found: {src_db}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest_db = backup_dir / f"polymarket_{stamp}.db"
    dest_gz = backup_dir / f"polymarket_{stamp}.db.gz"

    # Step 1: consistent online backup via sqlite3.backup()
    # pages=_PAGES yields the lock in between steps so writers aren't starved.
    logger.info(f"Backing up {src_db} → {dest_db} ...")
    src_conn = sqlite3.connect(str(src_db))
    dst_conn = sqlite3.connect(str(dest_db))
    try:
        src_conn.backup(dst_conn, pages=_PAGES)
    finally:
        dst_conn.close()
        src_conn.close()
    logger.info(f"sqlite3.backup() done — {dest_db.stat().st_size / 1e6:.1f} MB")

    # Step 2: compress the raw backup
    logger.info(f"Compressing → {dest_gz} ...")
    with open(dest_db, "rb") as f_in, gzip.open(dest_gz, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    dest_db.unlink()  # remove uncompressed copy
    src_size = src_db.stat().st_size
    gz_size = dest_gz.stat().st_size
    logger.info(
        f"Compressed: {gz_size / 1e6:.1f} MB "
        f"({gz_size / src_size * 100:.0f}% of original {src_size / 1e6:.0f} MB)"
    )

    # Step 3: rotate — delete oldest beyond _KEEP
    _rotate(backup_dir)

    return dest_gz


def _rotate(backup_dir: Path) -> None:
    """Delete oldest backups, keeping the _KEEP most recent .db.gz files."""
    files = sorted(
        backup_dir.glob("polymarket_*.db.gz"),
        key=lambda p: p.stat().st_mtime,
    )
    excess = files[:-_KEEP] if len(files) > _KEEP else []
    for old in excess:
        old.unlink()
        logger.info(f"Rotated out (deleted): {old.name}")
    kept = min(len(files), _KEEP)
    logger.info(f"Rotation complete — {kept} backup(s) retained in {backup_dir}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backup SQLite DB with gzip compression and rotation."
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help=f"Backup directory (default: {_DEFAULT_BACKUP_DIR})",
    )
    args = parser.parse_args()
    try:
        dest = run_backup(backup_dir=args.backup_dir)
        logger.info(f"Backup complete: {dest}")
        return 0
    except Exception:
        logger.exception("Backup failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
