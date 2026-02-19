"""Tests for document API routes."""

from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app
from app.models import Document

client = TestClient(app)


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_list_documents_empty():
    response = client.get("/api/documents/")
    assert response.status_code == 200
    assert response.json() == []


def test_get_document_not_found():
    response = client.get("/api/documents/999")
    assert response.status_code == 404
