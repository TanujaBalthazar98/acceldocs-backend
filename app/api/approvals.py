"""Approval workflow API routes."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.conversion.html_to_md import convert_html_to_markdown
from app.database import get_db
from app.ingestion.drive import _get_service, export_doc_as_html
from app.middleware.auth import AuthUser, require_auth, require_role
from app.models import Approval, Document, User
from app.publishing.git_publisher import publish_to_production, unpublish_from_production
from app.services.documents import _resolve_publish_path, _set_branding_from_doc

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
async def list_pending(
    current_user: AuthUser = Depends(require_auth),
    db: Session = Depends(get_db)
):
    """List documents awaiting approval (status=review). Requires authentication."""
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


def _get_system_user(db: Session) -> User:
    user = db.query(User).filter(User.google_id == "system").first()
    if user:
        return user
    user = User(
        google_id="system",
        email="system@local",
        name="System",
        role="owner",
    )
    db.add(user)
    db.flush()
    return user


@router.post("/action")
async def perform_action(
    body: ApprovalAction,
    current_user: AuthUser = Depends(require_role("reviewer")),
    db: Session = Depends(get_db)
):
    """Approve or reject a document. Requires reviewer role or higher.

    - approve: publishes markdown to production branch
    - reject: moves back to draft
    """
    doc = db.get(Document, body.document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if body.action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="Action must be 'approve' or 'reject'")

    # Use the authenticated user instead of system user
    actor = db.get(User, current_user.id)
    if not actor:
        # Fallback to system user if authenticated user not found in DB
        actor = _get_system_user(db)

    if body.action == "approve":
        try:
            _set_branding_from_doc(doc, db)
            service = _get_service()
            html = export_doc_as_html(service, doc.google_doc_id)
            markdown = convert_html_to_markdown(html)
            project_slug, version_slug, section, doc_slug = _resolve_publish_path(doc)
            commit_sha = publish_to_production(
                project=project_slug,
                version=version_slug,
                section=section,
                slug=doc_slug,
                markdown=markdown,
            )

            doc.status = "approved"
            doc.is_published = True
            doc.last_published_at = datetime.now(timezone.utc).isoformat()
            if commit_sha:
                db.add(
                    Approval(
                        document_id=body.document_id,
                        user_id=actor.id,
                        action="publish",
                        comment=f"Published commit {commit_sha[:8]}",
                    )
                )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Publish failed: {exc}") from exc
    else:
        doc.status = "draft"
        doc.is_published = False
        p_slug, v_slug, sec, d_slug = _resolve_publish_path(doc)
        unpublish_from_production(
            project=p_slug,
            version=v_slug,
            section=sec,
            slug=d_slug,
        )
        doc.last_published_at = None

    approval = Approval(
        document_id=body.document_id,
        user_id=actor.id,
        action=body.action,
        comment=body.comment,
    )
    db.add(approval)
    db.commit()

    return {"status": "ok", "document_status": doc.status}
