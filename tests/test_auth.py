"""Tests for auth JWT creation and validation."""

from fastapi.testclient import TestClient

from app.auth.routes import _create_jwt, JWT_ALGORITHM
from app.config import settings
from app.database import Base, engine
from app.main import app
from app.models import User

import jwt

client = TestClient(app)


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_create_jwt_and_decode():
    token = _create_jwt(user_id=42, email="test@example.com")
    payload = jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
    assert payload["sub"] == "42"
    assert payload["email"] == "test@example.com"
    assert "exp" in payload


def test_me_without_token():
    response = client.get("/auth/me")
    assert response.status_code == 401


def test_me_with_valid_token():
    from app.database import SessionLocal

    db = SessionLocal()
    user = User(google_id="g123", email="test@example.com", name="Test", role="admin")
    db.add(user)
    db.commit()
    db.refresh(user)
    db.close()

    token = _create_jwt(user.id, user.email)
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "test@example.com"
    assert data["role"] == "admin"


def test_me_with_invalid_token():
    response = client.get("/auth/me", headers={"Authorization": "Bearer invalid.token.here"})
    assert response.status_code == 401
