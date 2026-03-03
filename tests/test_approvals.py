from app.models import Document

def test_reject_moves_document_to_draft(client, db, auth_headers):
    doc = Document(
        google_doc_id="doc-1",
        title="Doc 1",
        slug="doc-1",
        project="release-notes",
        version="v1.0",
        visibility="public",
        status="review",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    response = client.post(
        "/api/approvals/action",
        json={"document_id": doc.id, "action": "reject"},
        headers=auth_headers("reviewer"),
    )
    assert response.status_code == 200
    assert response.json()["document_status"] == "draft"
