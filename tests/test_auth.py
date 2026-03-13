"""Tests for auth JWT creation and validation."""

from app.auth.routes import _create_jwt, JWT_ALGORITHM
from app.config import settings
from app.models import User

import jwt


def test_create_jwt_and_decode():
    token = _create_jwt(user_id=42, email="test@example.com")
    payload = jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
    assert payload["sub"] == "42"
    assert payload["email"] == "test@example.com"
    assert "exp" in payload


def test_me_without_token(client):
    response = client.get("/auth/me")
    assert response.status_code == 401


def test_me_with_valid_token(client, db):
    user = User(google_id="g123", email="test@example.com", name="Test", role="admin")
    db.add(user)
    db.commit()
    db.refresh(user)

    token = _create_jwt(user.id, user.email)
    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "test@example.com"
    assert data["role"] == "admin"


def test_me_with_invalid_token(client):
    response = client.get("/auth/me", headers={"Authorization": "Bearer invalid.token.here"})
    assert response.status_code == 401


def test_docs_session_sets_http_only_cookie(client, db):
    user = User(google_id="g-docs-cookie", email="cookie@example.com", name="Cookie User", role="editor")
    db.add(user)
    db.commit()
    db.refresh(user)

    token = _create_jwt(user.id, user.email)
    response = client.post("/auth/docs-session", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    cookie_header = response.headers.get("set-cookie", "")
    assert "acceldocs_docs_session=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "Path=/docs" in cookie_header


def test_logout_clears_docs_session_cookie(client):
    response = client.post("/auth/logout")
    assert response.status_code == 200
    cookie_header = response.headers.get("set-cookie", "")
    assert "acceldocs_docs_session=" in cookie_header
