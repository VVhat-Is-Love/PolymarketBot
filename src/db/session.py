from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session

from src.config.settings import settings


def _ensure_db_dir() -> None:
    if settings.database_url.startswith("sqlite"):
        # sqlite:///./data/polymarket.db  →  ./data/polymarket.db
        db_path = settings.database_url.split("sqlite:///")[1]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)


_ensure_db_dir()

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
    echo=False,
)

if "sqlite" in settings.database_url:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        """WAL mode: readers never block writers; busy_timeout retries instead of
        raising 'database is locked' immediately under the 3-thread scheduler."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Session:
    return SessionLocal()
