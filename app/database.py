"""SQLAlchemy engine and session wiring."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        # check_same_thread is a sqlite-only concern; TestClient drives the app
        # from a different thread than the one that opened the connection.
        return {"connect_args": {"check_same_thread": False}}
    return {"pool_pre_ping": True}


def _build_engine():
    """Turn a missing driver into an actionable message.

    A DATABASE_URL naming a dialect whose driver is not installed fails inside
    SQLAlchemy with a bare ModuleNotFoundError, which in a container reads as
    an application crash rather than a configuration mistake.
    """
    try:
        return create_engine(
            settings.database_url, **_engine_kwargs(settings.database_url)
        )
    except ModuleNotFoundError as exc:
        driver = exc.name or "the database driver"
        raise RuntimeError(
            f"DATABASE_URL requires the '{driver}' package, which is not "
            f"installed. For PostgreSQL: pip install 'psycopg[binary]'. "
            f"Current URL dialect: {settings.database_url.split('://', 1)[0]}"
        ) from exc


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
