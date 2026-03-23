"""Tests for health and readiness endpoints."""

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "acceldocs-backend"


def test_ready_development():
    response = client.get("/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in {"ready", "degraded"}
    assert data["environment"] == settings.environment
    assert isinstance(data["checks"], list)
    check_names = {check["name"] for check in data["checks"]}
    assert {"database", "google_oauth", "cors", "rate_limiting"}.issubset(check_names)


def test_ready_production_reports_degraded_when_required_values_missing(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "google_client_id", "")
    monkeypatch.setattr(settings, "google_client_secret", "")
    monkeypatch.setattr(settings, "allowed_origins", "")
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_storage_uri", "memory://")

    response = client.get("/ready")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "degraded"
    failing_checks = {check["name"] for check in data["checks"] if not check["ok"]}
    assert "google_oauth" in failing_checks
    assert "cors" in failing_checks
    assert "rate_limiting" in failing_checks
