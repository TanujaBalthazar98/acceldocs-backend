"""Tests for document API routes."""

def test_list_documents_empty(client):
    response = client.get("/api/documents/")
    assert response.status_code == 200
    assert response.json() == []


def test_get_document_not_found(client):
    response = client.get("/api/documents/999")
    assert response.status_code == 404
