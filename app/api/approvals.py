"""Approval workflow API routes and function handlers."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from googleapiclient.discovery import build as _gdrive_build

from app.conversion.html_to_md import convert_html_to_markdown
from app.database import get_db
from app.ingestion.drive import _get_service, export_doc_as_html
from app.middleware.auth import AuthUser, require_auth, require_role
from app.models import Approval, Document, OrgRole, Project, User
from app.publishing.git_publisher import publish_to_production, unpublish_from_production
from app.services.documents import _resolve_publish_path, _set_branding_from_doc

router = APIRouter()


import logging as _logging
_log = _logging.getLogger(__name__)


async def _get_drive_service_for_doc(doc: Document, db: Session, fallback_user: User | None = None):
    """Build a Drive API service using stored OAuth credentials.

    Tries in order: doc owner → fallback_user (approver) → global service account / ADC.
    """
    from app.services.drive import GoogleDriveService

    candidates: list[User] = []
    if doc.owner_id:
        owner = db.get(User, doc.owner_id)
        if owner:
            candidates.append(owner)
    if fallback_user and (not candidates or candidates[0].id != fallback_user.id):
        candidates.append(fallback_user)

    for user in candidates:
        try:
            svc = GoogleDriveService(db, user)
            creds = await svc.get_credentials(None)
            if creds:
                _log.info("Using OAuth credentials for user %s to export doc %s", user.email, doc.id)
                return _gdrive_build("drive", "v3", credentials=creds, cache_discovery=False)
        except Exception as e:
            _log.warning("Failed to get credentials for user %s: %s", user.email, e)

    _log.warning("No per-user credentials found for doc %s, falling back to ADC/_get_service()", doc.id)
    return _get_service()


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


def _get_user_project_ids(db: Session, user_id: int) -> list[int]:
    """Return all project IDs belonging to orgs the user is a member of."""
    org_ids = [
        r.organization_id
        for r in db.query(OrgRole).filter(OrgRole.user_id == user_id).all()
    ]
    if not org_ids:
        return []
    return [
        p.id
        for p in db.query(Project).filter(Project.organization_id.in_(org_ids)).all()
    ]


@router.get("/pending", response_model=list)
async def list_pending(
    current_user: AuthUser = Depends(require_auth),
    db: Session = Depends(get_db)
):
    """List documents awaiting approval (status=review), scoped to user's org."""
    project_ids = _get_user_project_ids(db, current_user.id)
    docs = (
        db.query(Document)
        .options(joinedload(Document.owner), joinedload(Document.project_rel))
        .filter(
            Document.status == "review",
            Document.project_id.in_(project_ids),
        )
        .order_by(Document.updated_at.desc())
        .all()
    )
    return [_serialize_pending_doc(d) for d in docs]


@router.get("/count")
async def get_count(
    current_user: AuthUser = Depends(require_auth),
    db: Session = Depends(get_db)
):
    """Count documents awaiting approval, scoped to user's org."""
    project_ids = _get_user_project_ids(db, current_user.id)
    count = (
        db.query(Document)
        .filter(Document.status == "review", Document.project_id.in_(project_ids))
        .count()
    )
    return {"count": count}


@router.get("/history", response_model=list)
async def list_history(
    current_user: AuthUser = Depends(require_auth),
    db: Session = Depends(get_db)
):
    """List 50 most recent approval actions, scoped to user's org."""
    project_ids = _get_user_project_ids(db, current_user.id)
    rows = (
        db.query(Approval)
        .options(
            joinedload(Approval.document),
            joinedload(Approval.user),
        )
        .join(Document, Approval.document_id == Document.id)
        .filter(Document.project_id.in_(project_ids))
        .order_by(Approval.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": a.id,
            "document_id": a.document_id,
            "document_title": a.document.title if a.document else None,
            "user_name": a.user.name if a.user else "Unknown",
            "action": a.action,
            "comment": a.comment,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]


@router.get("/my-submissions", response_model=list)
async def list_my_submissions(
    current_user: AuthUser = Depends(require_auth),
    db: Session = Depends(get_db)
):
    """List docs owned by the current user that are in review/approved/rejected."""
    docs = (
        db.query(Document)
        .options(joinedload(Document.project_rel))
        .filter(
            Document.owner_id == current_user.id,
            Document.status.in_(["review", "approved", "rejected"]),
        )
        .order_by(Document.updated_at.desc())
        .all()
    )
    return [
        {
            "id": d.id,
            "title": d.title,
            "status": d.status,
            "project_name": d.project_rel.name if d.project_rel else d.project,
            "project_id": d.project_id,
            "updated_at": d.updated_at.isoformat() if d.updated_at else None,
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


def _serialize_pending_doc(d: Document) -> dict:
    return {
        "id": d.id,
        "title": d.title,
        "project": d.project,
        "project_id": d.project_id,
        "project_name": d.project_rel.name if d.project_rel else d.project,
        "version": d.version,
        "slug": d.slug,
        "owner_id": d.owner_id,
        "owner_name": d.owner.name if d.owner else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


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

    actor = db.get(User, current_user.id)
    if not actor:
        actor = _get_system_user(db)

    if body.action == "approve":
        try:
            _set_branding_from_doc(doc, db)
            html = None
            try:
                service = await _get_drive_service_for_doc(doc, db, fallback_user=actor)
                html = export_doc_as_html(service, doc.google_doc_id)
            except Exception as drive_err:
                _log.warning("Drive fetch failed for doc %s, falling back to cache: %s", doc.id, drive_err)
            if not html:
                html = doc.content_html or doc.published_content_html
            if not html:
                from app.models import DocumentCache
                cache = db.query(DocumentCache).filter(DocumentCache.document_id == doc.id).first()
                if cache:
                    html = cache.published_content_html_encrypted or cache.content_html_encrypted
            if not html:
                raise ValueError("No content available to publish")
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


# ---------------------------------------------------------------------------
# Function handlers — called via /api/functions/{name} (body, db, user)
# ---------------------------------------------------------------------------

async def approvals_pending_fn(body: dict, db: Session, user: User | None) -> dict:
    """List pending review docs for the user's org."""
    if not user:
        return {"ok": False, "error": "Authentication required"}
    project_ids = _get_user_project_ids(db, user.id)
    docs = (
        db.query(Document)
        .options(joinedload(Document.owner), joinedload(Document.project_rel))
        .filter(
            Document.status == "review",
            Document.project_id.in_(project_ids),
        )
        .order_by(Document.updated_at.desc())
        .all()
    )
    return {"ok": True, "pending": [_serialize_pending_doc(d) for d in docs]}


async def approvals_count_fn(body: dict, db: Session, user: User | None) -> dict:
    """Count pending review docs for the user's org."""
    if not user:
        return {"ok": False, "error": "Authentication required"}
    project_ids = _get_user_project_ids(db, user.id)
    count = (
        db.query(Document)
        .filter(Document.status == "review", Document.project_id.in_(project_ids))
        .count()
    )
    return {"ok": True, "count": count}


async def approvals_history_fn(body: dict, db: Session, user: User | None) -> dict:
    """List recent approval history for the user's org."""
    if not user:
        return {"ok": False, "error": "Authentication required"}
    project_ids = _get_user_project_ids(db, user.id)
    rows = (
        db.query(Approval)
        .options(joinedload(Approval.document), joinedload(Approval.user))
        .join(Document, Approval.document_id == Document.id)
        .filter(Document.project_id.in_(project_ids))
        .order_by(Approval.created_at.desc())
        .limit(50)
        .all()
    )
    return {
        "ok": True,
        "history": [
            {
                "id": a.id,
                "document_id": a.document_id,
                "document_title": a.document.title if a.document else None,
                "user_name": a.user.name if a.user else "Unknown",
                "action": a.action,
                "comment": a.comment,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in rows
        ],
    }


async def approvals_my_submissions_fn(body: dict, db: Session, user: User | None) -> dict:
    """List the current user's submitted docs with approval status."""
    if not user:
        return {"ok": False, "error": "Authentication required"}
    docs = (
        db.query(Document)
        .options(joinedload(Document.project_rel))
        .filter(
            Document.owner_id == user.id,
            Document.status.in_(["review", "approved", "rejected"]),
        )
        .order_by(Document.updated_at.desc())
        .all()
    )
    return {
        "ok": True,
        "submissions": [
            {
                "id": d.id,
                "title": d.title,
                "status": d.status,
                "project_name": d.project_rel.name if d.project_rel else d.project,
                "project_id": d.project_id,
                "updated_at": d.updated_at.isoformat() if d.updated_at else None,
            }
            for d in docs
        ],
    }


async def approvals_action_fn(body: dict, db: Session, user: User | None) -> dict:
    """Approve or reject a document (function handler variant)."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    doc_id_raw = body.get("document_id") or body.get("documentId")
    try:
        doc_id = int(doc_id_raw)
    except (TypeError, ValueError):
        return {"ok": False, "error": "document_id required"}

    action = body.get("action", "")
    if action not in ("approve", "reject"):
        return {"ok": False, "error": "action must be 'approve' or 'reject'"}

    comment = body.get("comment")

    doc = db.get(Document, doc_id)
    if not doc:
        return {"ok": False, "error": "Document not found"}

    actor = db.get(User, user.id)
    if not actor:
        return {"ok": False, "error": "User not found"}

    if action == "approve":
        try:
            _set_branding_from_doc(doc, db)
            html = None
            try:
                service = await _get_drive_service_for_doc(doc, db, fallback_user=actor)
                html = export_doc_as_html(service, doc.google_doc_id)
            except Exception as drive_err:
                _log.warning("Drive fetch failed for doc %s, falling back to cache: %s", doc.id, drive_err)
            if not html:
                html = doc.content_html or doc.published_content_html
            if not html:
                from app.models import DocumentCache
                cache = db.query(DocumentCache).filter(DocumentCache.document_id == doc.id).first()
                if cache:
                    html = cache.published_content_html_encrypted or cache.content_html_encrypted
            if not html:
                return {"ok": False, "error": "No content available to publish"}
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
                        document_id=doc_id,
                        user_id=actor.id,
                        action="publish",
                        comment=f"Published commit {commit_sha[:8]}",
                    )
                )
        except Exception as exc:
            return {"ok": False, "error": f"Publish failed: {exc}"}
    else:
        doc.status = "draft"
        doc.is_published = False
        try:
            p_slug, v_slug, sec, d_slug = _resolve_publish_path(doc)
            unpublish_from_production(project=p_slug, version=v_slug, section=sec, slug=d_slug)
        except Exception:
            pass
        doc.last_published_at = None

    db.add(Approval(document_id=doc_id, user_id=actor.id, action=action, comment=comment))
    db.commit()

    return {"ok": True, "document_status": doc.status}
