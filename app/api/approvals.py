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
from app.publishing.git_publisher import promote_preview_to_production, publish_to_production, unpublish_from_production
from app.services.documents import _resolve_publish_path, _set_branding_from_doc, _auto_deploy_if_public

router = APIRouter()


import logging as _logging
_log = _logging.getLogger(__name__)


async def _get_drive_service_for_doc(
    doc: Document,
    db: Session,
    fallback_user: User | None = None,
    access_token: str | None = None,
):
    """Build a Drive API service using stored OAuth credentials.

    Tries in order:
    1. Browser-passed access token (freshest, no stored creds needed)
    2. Doc owner's stored refresh token
    3. Fallback user (approver) stored refresh token
    4. Global service account / ADC
    """
    from app.services.drive import GoogleDriveService
    from google.oauth2.credentials import Credentials

    # 1. Try the browser-passed access token directly — most current credentials
    if access_token:
        try:
            creds = Credentials(token=access_token)
            _log.info("Using browser-passed access token to export doc %s", doc.id)
            return _gdrive_build("drive", "v3", credentials=creds, cache_discovery=False)
        except Exception as e:
            _log.warning("Failed to use passed access token for doc %s: %s", doc.id, e)

    # 2 & 3. Try stored refresh tokens for doc owner then approver
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

    # 4. Last resort: service account / ADC
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
            project_slug, version_slug, section, doc_slug = _resolve_publish_path(doc)

            # Primary path: promote the preview-branch file (synced from Drive) to main.
            # This avoids needing Drive credentials in the web-server process.
            commit_sha, _ = promote_preview_to_production(project_slug, version_slug, section, doc_slug)

            # Fallback: build markdown from HTML if no preview file exists yet.
            if not commit_sha:
                html = None
                try:
                    service = await _get_drive_service_for_doc(doc, db, fallback_user=actor)
                    html = export_doc_as_html(service, doc.google_doc_id)
                except Exception as drive_err:
                    _log.warning("Drive fetch failed for doc %s: %s", doc.id, drive_err)
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

    # Rebuild and deploy the Zensical site for public projects
    _auto_deploy_if_public(doc, db)

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
    google_access_token = body.get("_google_access_token")

    doc = db.get(Document, doc_id)
    if not doc:
        return {"ok": False, "error": "Document not found"}

    actor = db.get(User, user.id)
    if not actor:
        return {"ok": False, "error": "User not found"}

    if action == "approve":
        try:
            _set_branding_from_doc(doc, db)
            project_slug, version_slug, section, doc_slug = _resolve_publish_path(doc)

            commit_sha = None

            # Primary path when browser token available: fetch latest content directly from Drive.
            # This guarantees we publish whatever is currently in the Google Doc, not stale preview.
            if google_access_token and doc.google_doc_id:
                try:
                    service = await _get_drive_service_for_doc(
                        doc, db, fallback_user=actor, access_token=google_access_token
                    )
                    html = export_doc_as_html(service, doc.google_doc_id)
                    if html:
                        markdown = convert_html_to_markdown(html)
                        commit_sha = publish_to_production(
                            project=project_slug,
                            version=version_slug,
                            section=section,
                            slug=doc_slug,
                            markdown=markdown,
                        )
                        _log.info("Published doc %s from Drive via browser token", doc.id)
                except Exception as drive_err:
                    _log.warning("Drive fetch with browser token failed for doc %s: %s", doc.id, drive_err)

            # Secondary path: promote the preview-branch file (synced from Drive) to main.
            if not commit_sha:
                commit_sha, _ = promote_preview_to_production(project_slug, version_slug, section, doc_slug)
                if commit_sha:
                    _log.info("Promoted preview branch content for doc %s", doc.id)

            # Tertiary fallback: stored HTML content.
            if not commit_sha:
                html = None
                try:
                    service = await _get_drive_service_for_doc(doc, db, fallback_user=actor)
                    html = export_doc_as_html(service, doc.google_doc_id)
                except Exception as drive_err:
                    _log.warning("Drive fetch failed for doc %s: %s", doc.id, drive_err)
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

    # Rebuild and deploy the Zensical site for public projects
    _auto_deploy_if_public(doc, db)

    return {"ok": True, "document_status": doc.status}
