from pathlib import Path
from sqlalchemy import create_engine
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

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Session:
    return SessionLocal()
