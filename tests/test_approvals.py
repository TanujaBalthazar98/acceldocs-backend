from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Document

client = TestClient(app)


def setup_function():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_reject_moves_document_to_draft():
    db = SessionLocal()
    try:
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
      doc_id = doc.id
    finally:
      db.close()

    response = client.post("/api/approvals/action", json={"document_id": doc_id, "action": "reject"})
    assert response.status_code == 200
    assert response.json()["document_status"] == "draft"
