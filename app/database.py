"""SQLAlchemy database setup — supports both PostgreSQL (production) and SQLite (local dev)."""

import os
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

is_pytest = "PYTEST_CURRENT_TEST" in os.environ

# Use a writable temp DB during tests to avoid locking/readonly issues.
if is_pytest and settings.is_sqlite:
    test_db_path = os.path.join(tempfile.gettempdir(), "acceldocs_test.db")
    database_url = f"sqlite:///{test_db_path}"
else:
    database_url = settings.database_url

# SQLite needs check_same_thread=False; PostgreSQL doesn't need special args
connect_args = {"check_same_thread": False} if settings.is_sqlite else {}

# PostgreSQL benefits from connection pooling; SQLite doesn't support it
engine_kwargs: dict = {
    "connect_args": connect_args,
    "echo": False,
}
if not settings.is_sqlite:
    engine_kwargs["pool_size"] = 5
    engine_kwargs["max_overflow"] = 10
    engine_kwargs["pool_pre_ping"] = True  # detect stale connections

engine = create_engine(database_url, **engine_kwargs)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency that yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
