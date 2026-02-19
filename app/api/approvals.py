"""Approval workflow API routes."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Approval, Document

router = APIRouter()


class ApprovalAction(BaseModel):
    document_id: int
    action: str  # "approve" or "reject"
    comment: str | None = None


class ApprovalOut(BaseModel):
    id: int
    document_id: int
    user_id: int
    action: str
    comment: str | None
    created_at: str | None

    model_config = {"from_attributes": True}


@router.get("/pending", response_model=list)
async def list_pending(db: Session = Depends(get_db)):
    """List documents awaiting approval (status=review)."""
    docs = (
        db.query(Document)
        .filter(Document.status == "review")
        .order_by(Document.updated_at.desc())
        .all()
    )
    return [
        {
            "id": d.id,
            "title": d.title,
            "project": d.project,
            "version": d.version,
            "slug": d.slug,
            "updated_at": str(d.updated_at) if d.updated_at else None,
        }
        for d in docs
    ]


@router.post("/action")
async def perform_action(body: ApprovalAction, db: Session = Depends(get_db)):
    """Approve or reject a document.

    Phase 7 will implement the actual git-based publishing.
    For now, updates status in the database.
    """
    doc = db.get(Document, body.document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if body.action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="Action must be 'approve' or 'reject'")

    if body.action == "approve":
        doc.status = "approved"
    else:
        doc.status = "draft"

    # TODO: Phase 7 — also commit to git branch here
    # TODO: Phase 8 — use authenticated user_id instead of hardcoded 0
    approval = Approval(
        document_id=body.document_id,
        user_id=0,
        action=body.action,
        comment=body.comment,
    )
    db.add(approval)
    db.commit()

    return {"status": "ok", "document_status": doc.status}
