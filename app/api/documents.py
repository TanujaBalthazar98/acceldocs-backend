"""Document CRUD API routes."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Document

router = APIRouter()


class DocumentOut(BaseModel):
    id: int
    google_doc_id: str
    title: str
    slug: str
    project: str
    version: str
    section: str | None
    visibility: str
    status: str
    description: str | None
    tags: str | None
    drive_modified_at: str | None
    last_synced_at: str | None
    last_published_at: str | None

    model_config = {"from_attributes": True}


@router.get("/", response_model=list[DocumentOut])
async def list_documents(
    project: str | None = Query(None),
    status: str | None = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(Document)
    if project:
        query = query.filter(Document.project == project)
    if status:
        query = query.filter(Document.status == status)
    return query.order_by(Document.title).all()


@router.get("/{doc_id}", response_model=DocumentOut)
async def get_document(doc_id: int, db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc
