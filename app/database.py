"""SQLAlchemy database setup."""

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

connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}

engine = create_engine(
    database_url,
    connect_args=connect_args,
    echo=False,
)

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
