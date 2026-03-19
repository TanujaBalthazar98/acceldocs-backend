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
from app.models import Approval, Document, OrgRole, Page, Project, Section, User
from app.publishing.git_publisher import promote_preview_to_production, publish_to_production, unpublish_from_production
from app.services.documents import _resolve_publish_path, _set_branding_from_doc, _auto_deploy_if_public

router = APIRouter()


import logging as _logging
_log = _logging.getLogger(__name__)


def _iso_utc(dt: datetime | None) -> str | None:
    """Serialize datetimes with explicit UTC offset.

    SQLite often returns naive datetimes even when timezone=True. Treat those
    as UTC to avoid client-side relative-time skew.
    """
    if not dt:
        return None
    normalized = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat()


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


def _get_user_org_ids(db: Session, user_id: int) -> list[int]:
    """Return all organization IDs the user is a member of."""
    return [
        r.organization_id
        for r in db.query(OrgRole).filter(OrgRole.user_id == user_id).all()
    ]


def _get_user_org_role_map(db: Session, user_id: int, org_ids: list[int]) -> dict[int, str]:
    """Return organization -> role mapping for a user within scoped org ids."""
    if not org_ids:
        return {}
    rows = (
        db.query(OrgRole)
        .filter(OrgRole.user_id == user_id, OrgRole.organization_id.in_(org_ids))
        .all()
    )
    return {row.organization_id: (row.role or "").lower() for row in rows}


def _get_scoped_org_ids(db: Session, user_id: int, body: dict | None) -> list[int]:
    """Resolve org scope from selected workspace header/body, with membership safety."""
    user_org_ids = _get_user_org_ids(db, user_id)
    if not user_org_ids:
        return []

    selected_org_raw = (body or {}).get("_x_org_id")
    if selected_org_raw in (None, "", 0):
        return user_org_ids

    try:
        selected_org_id = int(selected_org_raw)
    except (TypeError, ValueError):
        return []

    return [selected_org_id] if selected_org_id in user_org_ids else []


def _get_project_ids_for_org_ids(db: Session, org_ids: list[int]) -> list[int]:
    """Return project ids for provided org ids."""
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
    query = (
        db.query(Document)
        .options(joinedload(Document.owner), joinedload(Document.project_rel))
        .filter(Document.status == "review")
    )
    # Legacy compatibility: when org/project membership rows are absent
    # (older tests/flows), return all review docs instead of none.
    if project_ids:
        query = query.filter(Document.project_id.in_(project_ids))
    docs = query.order_by(Document.updated_at.desc()).all()
    return [_serialize_pending_doc(d) for d in docs]


@router.get("/count")
async def get_count(
    current_user: AuthUser = Depends(require_auth),
    db: Session = Depends(get_db)
):
    """Count documents awaiting approval, scoped to user's org."""
    project_ids = _get_user_project_ids(db, current_user.id)
    query = db.query(Document).filter(Document.status == "review")
    if project_ids:
        query = query.filter(Document.project_id.in_(project_ids))
    count = query.count()
    return {"count": count}


@router.get("/history", response_model=list)
async def list_history(
    current_user: AuthUser = Depends(require_auth),
    db: Session = Depends(get_db)
):
    """List 50 most recent approval actions, scoped to user's org."""
    project_ids = _get_user_project_ids(db, current_user.id)
    query = (
        db.query(Approval)
        .options(
            joinedload(Approval.document),
            joinedload(Approval.user),
        )
        .join(Document, Approval.document_id == Document.id)
        .order_by(Approval.created_at.desc())
    )
    if project_ids:
        query = query.filter(Document.project_id.in_(project_ids))
    rows = query.limit(50).all()
    return [
        {
            "id": a.id,
            "document_id": a.document_id,
            "document_title": a.document.title if a.document else None,
            "document_owner_id": a.document.owner_id if a.document else None,
            "user_id": a.user_id,
            "user_name": a.user.name if a.user else "Unknown",
            "action": a.action,
            "comment": a.comment,
            "created_at": _iso_utc(a.created_at),
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
            "updated_at": _iso_utc(d.updated_at),
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
        "updated_at": _iso_utc(d.updated_at),
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
            project_slug, version_slug, section, doc_slug, product_slug = _resolve_publish_path(doc)

            # Primary path: promote the preview-branch file (synced from Drive) to main.
            # This avoids needing Drive credentials in the web-server process.
            commit_sha, _ = promote_preview_to_production(project_slug, version_slug, section, doc_slug,
                                                           product=product_slug)

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
                    product=product_slug,
                )

            doc.status = "approved"
            doc.is_published = True
            doc.last_published_at = datetime.now(timezone.utc).isoformat()
            if commit_sha:
                db.add(
                    Approval(
                        document_id=body.document_id,
                        entity_type="document",
                        user_id=actor.id,
                        action="publish",
                        comment=f"Published commit {commit_sha[:8]}",
                    )
                )
        except HTTPException:
            raise
        except Exception as exc:
            _log.error("Publish failed: %s", exc)
            raise HTTPException(status_code=500, detail="Publish failed. Please try again.") from exc
    else:
        doc.status = "draft"
        doc.is_published = False
        p_slug, v_slug, sec, d_slug, prod_slug = _resolve_publish_path(doc)
        unpublish_from_production(
            project=p_slug,
            version=v_slug,
            section=sec,
            slug=d_slug,
            product=prod_slug,
        )
        doc.last_published_at = None

    approval = Approval(
        document_id=body.document_id,
        entity_type="document",
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
    org_ids = _get_scoped_org_ids(db, user.id, body)
    if not org_ids:
        return {"ok": True, "pending": []}
    role_map = _get_user_org_role_map(db, user.id, org_ids)
    allowed_review_roles = {"owner", "admin", "reviewer"}
    pages = (
        db.query(Page)
        .options(joinedload(Page.owner), joinedload(Page.section))
        .filter(
            Page.status == "review",
            Page.organization_id.in_(org_ids),
        )
        .order_by(Page.updated_at.desc())
        .all()
    )
    pending = [
        {
            "id": p.id,
            "entity_type": "page",
            "title": p.title,
            "project": p.section.name if p.section else "General",
            "project_id": p.section_id,
            "project_name": p.section.name if p.section else None,
            "version": "default",
            "slug": p.slug,
            "owner_id": p.owner_id,
            "owner_name": p.owner.name if p.owner else None,
            "updated_at": _iso_utc(p.updated_at),
            "can_review": role_map.get(p.organization_id, "") in allowed_review_roles,
        }
        for p in pages
    ]
    return {"ok": True, "pending": pending}


async def approvals_count_fn(body: dict, db: Session, user: User | None) -> dict:
    """Count pending review docs for the user's org."""
    if not user:
        return {"ok": False, "error": "Authentication required"}
    org_ids = _get_scoped_org_ids(db, user.id, body)
    if not org_ids:
        return {"ok": True, "count": 0}
    count = (
        db.query(Page)
        .filter(Page.status == "review", Page.organization_id.in_(org_ids))
        .count()
    )
    return {"ok": True, "count": count}


async def approvals_history_fn(body: dict, db: Session, user: User | None) -> dict:
    """List recent approval history for the user's org."""
    if not user:
        return {"ok": False, "error": "Authentication required"}
    org_ids = _get_scoped_org_ids(db, user.id, body)
    if not org_ids:
        return {"ok": True, "history": []}
    project_ids = _get_project_ids_for_org_ids(db, org_ids)

    rows = (
        db.query(Approval)
        .options(joinedload(Approval.user))
        .order_by(Approval.created_at.desc())
        .limit(200)
        .all()
    )
    if not rows:
        return {"ok": True, "history": []}

    # Keep entity type explicit to avoid page/document ID collision mismatches.
    page_ids = {
        a.document_id
        for a in rows
        if str(getattr(a, "entity_type", "") or "").lower() == "page"
    }
    doc_ids = {
        a.document_id
        for a in rows
        if str(getattr(a, "entity_type", "") or "").lower() == "document"
    }
    unknown_ids = {
        a.document_id
        for a in rows
        if str(getattr(a, "entity_type", "") or "").lower() not in {"page", "document"}
    }
    # Unknown legacy rows are resolved with page-first fallback below.
    doc_lookup_ids = sorted(doc_ids | unknown_ids)
    page_lookup_ids = sorted(page_ids | unknown_ids)

    docs = []
    if doc_lookup_ids:
        docs = (
            db.query(Document)
            .options(joinedload(Document.owner))
            .filter(Document.id.in_(doc_lookup_ids), Document.project_id.in_(project_ids))
            .all()
        )
    pages = []
    if page_lookup_ids:
        pages = (
            db.query(Page)
            .options(joinedload(Page.owner), joinedload(Page.section))
            .filter(Page.id.in_(page_lookup_ids), Page.organization_id.in_(org_ids))
            .all()
        )
    docs_by_id = {d.id: d for d in docs}
    pages_by_id = {p.id: p for p in pages}

    history: list[dict] = []
    for a in rows:
        entity_type = str(getattr(a, "entity_type", "") or "").lower()
        doc = None
        page = None
        if entity_type == "page":
            page = pages_by_id.get(a.document_id)
            if page is None:
                doc = docs_by_id.get(a.document_id)
        elif entity_type == "document":
            doc = docs_by_id.get(a.document_id)
            if doc is None:
                page = pages_by_id.get(a.document_id)
        else:
            # Legacy rows without entity_type: prefer page resolution for current flow.
            page = pages_by_id.get(a.document_id)
            if page is None:
                doc = docs_by_id.get(a.document_id)

        if doc is None and page is None:
            continue

        title = doc.title if doc else page.title
        owner_id = doc.owner_id if doc else page.owner_id
        history.append(
            {
                "id": a.id,
                "document_id": a.document_id,
                "entity_type": "document" if doc else "page",
                "document_title": title,
                "document_owner_id": owner_id,
                "user_id": a.user_id,
                "user_name": a.user.name if a.user else "Unknown",
                "action": a.action,
                "comment": a.comment,
                "created_at": _iso_utc(a.created_at),
            }
        )
        if len(history) >= 50:
            break

    return {
        "ok": True,
        "history": history,
    }


async def approvals_my_submissions_fn(body: dict, db: Session, user: User | None) -> dict:
    """List the current user's submitted docs with approval status."""
    if not user:
        return {"ok": False, "error": "Authentication required"}
    org_ids = _get_scoped_org_ids(db, user.id, body)
    if not org_ids:
        return {"ok": True, "submissions": []}
    pages = (
        db.query(Page)
        .options(joinedload(Page.section))
        .filter(
            Page.owner_id == user.id,
            Page.organization_id.in_(org_ids),
            Page.status.in_(["review", "published"]),
        )
        .order_by(Page.updated_at.desc())
        .all()
    )
    return {
        "ok": True,
        "submissions": [
            {
                "id": p.id,
                "entity_type": "page",
                "title": p.title,
                "status": p.status,
                "project_name": p.section.name if p.section else None,
                "project_id": p.section_id,
                "updated_at": _iso_utc(p.updated_at),
            }
            for p in pages
        ],
    }


def _resolve_entity_org_role(
    db: Session,
    user_id: int,
    entity_type: str,
    entity_id: int,
) -> str | None:
    """Resolve the actor's org-scoped role for a page/document entity."""
    org_id: int | None = None

    if entity_type == "page":
        page = db.get(Page, entity_id)
        org_id = page.organization_id if page else None
    else:
        doc = db.get(Document, entity_id)
        if doc and doc.project_id:
            proj = db.get(Project, doc.project_id)
            org_id = proj.organization_id if proj else None

    if not org_id:
        return None

    org_role = (
        db.query(OrgRole)
        .filter(OrgRole.user_id == user_id, OrgRole.organization_id == org_id)
        .first()
    )
    if not org_role or not org_role.role:
        return None
    return org_role.role.lower()


def _can_review_entity(
    db: Session,
    actor: User,
    entity_type: str,
    entity_id: int,
) -> bool:
    """Authorize review action with org-scoped role; fallback to legacy global role."""
    allowed = {"owner", "admin", "reviewer"}
    scoped_role = _resolve_entity_org_role(db, actor.id, entity_type, entity_id)
    if scoped_role:
        return scoped_role in allowed
    return (actor.role or "").lower() in allowed


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

    actor = db.get(User, user.id)
    if not actor:
        return {"ok": False, "error": "User not found"}

    entity_type = str(body.get("entity_type") or body.get("entityType") or "document").strip().lower()
    if entity_type not in {"document", "page"}:
        entity_type = "document"

    scoped_org_ids = _get_scoped_org_ids(db, actor.id, body)
    if not scoped_org_ids:
        return {"ok": False, "error": "No workspace access for approval action."}

    target_org_id: int | None = None
    if entity_type == "page":
        target_page = db.get(Page, doc_id)
        target_org_id = target_page.organization_id if target_page else None
    else:
        target_doc = db.get(Document, doc_id)
        if target_doc and target_doc.project_id:
            target_project = db.get(Project, target_doc.project_id)
            target_org_id = target_project.organization_id if target_project else None

    if target_org_id and target_org_id not in scoped_org_ids:
        return {"ok": False, "error": "Item is not in the selected workspace."}

    if not _can_review_entity(db, actor, entity_type, doc_id):
        return {"ok": False, "error": "Insufficient permissions. Reviewer/Admin/Owner required."}

    page = None
    doc = None
    if entity_type == "page":
        page = db.get(Page, doc_id)
        if not page:
            return {"ok": False, "error": "Page not found"}
    else:
        doc = db.get(Document, doc_id)
        if not doc:
            # Backward-compatible fallback: if no document exists, try page with same id.
            page = db.get(Page, doc_id)
            if page:
                entity_type = "page"
            else:
                return {"ok": False, "error": "Document not found"}

    if entity_type == "page":
        if action == "approve":
            if not page.html_content:
                return {"ok": False, "error": "Page has no content. Sync first."}
            page.published_html = page.html_content
            page.is_published = True
            page.status = "published"
            if page.section_id:
                section = db.get(Section, page.section_id)
                if section and not section.is_published:
                    section.is_published = True
        else:
            page.status = "draft"
            page.is_published = False

        db.add(
            Approval(
                document_id=doc_id,
                entity_type="page",
                user_id=actor.id,
                action=action,
                comment=comment,
            )
        )
        db.commit()
        return {"ok": True, "document_status": page.status, "entity_type": "page"}

    if action == "approve":
        try:
            _set_branding_from_doc(doc, db)
            project_slug, version_slug, section, doc_slug, product_slug = _resolve_publish_path(doc)

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
                            product=product_slug,
                        )
                        _log.info("Published doc %s from Drive via browser token", doc.id)
                except Exception as drive_err:
                    _log.warning("Drive fetch with browser token failed for doc %s: %s", doc.id, drive_err)

            # Secondary path: promote the preview-branch file (synced from Drive) to main.
            if not commit_sha:
                commit_sha, _ = promote_preview_to_production(project_slug, version_slug, section, doc_slug,
                                                               product=product_slug)
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
                    product=product_slug,
                )
            doc.status = "approved"
            doc.is_published = True
            doc.last_published_at = datetime.now(timezone.utc).isoformat()
            if commit_sha:
                db.add(
                    Approval(
                        document_id=doc_id,
                        entity_type="document",
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
            p_slug, v_slug, sec, d_slug, prod_slug = _resolve_publish_path(doc)
            unpublish_from_production(project=p_slug, version=v_slug, section=sec, slug=d_slug,
                                      product=prod_slug)
        except Exception:
            pass
        doc.last_published_at = None

    db.add(
        Approval(
            document_id=doc_id,
            entity_type="document",
            user_id=actor.id,
            action=action,
            comment=comment,
        )
    )
    db.commit()

    # Rebuild and deploy the Zensical site for public projects
    _auto_deploy_if_public(doc, db)

    return {"ok": True, "document_status": doc.status}
