"""Test fixtures and configuration."""

import jwt
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import User, Document, DocumentView
from app.config import settings

# Create in-memory test database
SQLALCHEMY_TEST_DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    SQLALCHEMY_TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    """Override the database dependency for tests."""
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()


# Override the database dependency
app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(scope="function")
def db():
    """Create a fresh database for each test."""
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    yield db
    db.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def client(db):
    """Create a test client."""
    return TestClient(app)


@pytest.fixture
def test_users(db):
    """Create test users with different roles."""
    users = {
        "admin": User(
            google_id="admin-123",
            email="admin@example.com",
            name="Admin User",
            role="admin",
        ),
        "editor": User(
            google_id="editor-123",
            email="editor@example.com",
            name="Editor User",
            role="editor",
        ),
        "reviewer": User(
            google_id="reviewer-123",
            email="reviewer@example.com",
            name="Reviewer User",
            role="reviewer",
        ),
        "viewer": User(
            google_id="viewer-123",
            email="viewer@example.com",
            name="Viewer User",
            role="viewer",
        ),
    }

    for user in users.values():
        db.add(user)
    db.commit()

    # Refresh to get IDs
    for user in users.values():
        db.refresh(user)

    return users


@pytest.fixture
def auth_headers(test_users):
    """Generate auth headers for different users."""
    def _make_header(role: str) -> dict:
        user = test_users[role]
        token = jwt.encode(
            {
                "user_id": user.id,
                "email": user.email,
                "role": user.role,
            },
            settings.secret_key,
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}

    return _make_header


@pytest.fixture
def test_documents(db, test_users):
    """Create test documents with various statuses and visibilities."""
    docs = [
        Document(
            google_doc_id="doc-1",
            title="Public Draft Document",
            slug="public-draft",
            project="Test Project",
            version="v1.0",
            section="Getting Started",
            visibility="public",
            status="draft",
            description="A public draft document",
            tags="test,draft",
            drive_modified_at=datetime.now(timezone.utc).isoformat(),
            last_synced_at=datetime.now(timezone.utc).isoformat(),
        ),
        Document(
            google_doc_id="doc-2",
            title="Public Approved Document",
            slug="public-approved",
            project="Test Project",
            version="v1.0",
            section="Getting Started",
            visibility="public",
            status="approved",
            description="A public approved document",
            tags="test,approved",
            drive_modified_at=datetime.now(timezone.utc).isoformat(),
            last_synced_at=datetime.now(timezone.utc).isoformat(),
            last_published_at=datetime.now(timezone.utc).isoformat(),
        ),
        Document(
            google_doc_id="doc-3",
            title="Internal Approved Document",
            slug="internal-approved",
            project="Test Project",
            version="v1.0",
            section="Internal",
            visibility="internal",
            status="approved",
            description="An internal approved document",
            tags="test,internal",
            drive_modified_at=datetime.now(timezone.utc).isoformat(),
            last_synced_at=datetime.now(timezone.utc).isoformat(),
            last_published_at=datetime.now(timezone.utc).isoformat(),
        ),
        Document(
            google_doc_id="doc-4",
            title="Review Document",
            slug="review-doc",
            project="Test Project",
            version="v1.0",
            section="Features",
            visibility="public",
            status="review",
            description="A document pending review",
            tags="test,review",
            drive_modified_at=datetime.now(timezone.utc).isoformat(),
            last_synced_at=datetime.now(timezone.utc).isoformat(),
        ),
    ]

    for doc in docs:
        db.add(doc)
    db.commit()

    # Refresh to get IDs
    for doc in docs:
        db.refresh(doc)

    return docs


@pytest.fixture
def test_views(db, test_documents, test_users):
    """Create test document views for analytics."""
    views = []

    # Create views for doc-2 (most viewed)
    for i in range(5):
        view = DocumentView(
            document_id=test_documents[1].id,  # Public Approved
            user_id=test_users["viewer"].id,
            user_email=test_users["viewer"].email,
            ip_address=f"192.168.1.{i}",
            user_agent="Mozilla/5.0",
        )
        views.append(view)
        db.add(view)

    # Create views for doc-3 (internal)
    for i in range(3):
        view = DocumentView(
            document_id=test_documents[2].id,  # Internal Approved
            user_id=test_users["editor"].id,
            user_email=test_users["editor"].email,
            ip_address=f"192.168.2.{i}",
            user_agent="Mozilla/5.0",
        )
        views.append(view)
        db.add(view)

    db.commit()
    return views
