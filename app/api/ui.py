"""Server-rendered admin dashboard using Jinja2 templates."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Approval, Document, Project, SyncLog, User

logger = logging.getLogger(__name__)

router = APIRouter()

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _pending_count(db: Session) -> int:
    return db.query(func.count(Document.id)).filter(Document.status == "review").scalar() or 0


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    total = db.query(func.count(Document.id)).scalar() or 0
    approved = (
        db.query(func.count(Document.id)).filter(Document.status == "approved").scalar() or 0
    )
    review = _pending_count(db)
    draft = db.query(func.count(Document.id)).filter(Document.status == "draft").scalar() or 0

    recent = (
        db.query(Document).order_by(Document.updated_at.desc()).limit(10).all()
    )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "active": "dashboard",
            "pending_count": review,
            "total_docs": total,
            "approved_count": approved,
            "review_count": review,
            "draft_count": draft,
            "recent_docs": recent,
        },
    )


@router.get("/documents", response_class=HTMLResponse)
async def documents_page(
    request: Request,
    status: str | None = Query(None),
    project: str | None = Query(None),
    visibility: str | None = Query(None),
    version: str | None = Query(None),
    q: str | None = Query(None),
    sort: str | None = Query(None),
    order: str | None = Query("asc"),
    db: Session = Depends(get_db),
):
    """Documents page with search, filtering, and sorting."""
    query = db.query(Document)

    # Apply filters
    if status:
        query = query.filter(Document.status == status)
    if project:
        query = query.filter(Document.project == project)
    if visibility:
        query = query.filter(Document.visibility == visibility)
    if version:
        query = query.filter(Document.version == version)

    # Apply search
    if q:
        search_term = f"%{q}%"
        query = query.filter(
            (Document.title.ilike(search_term)) |
            (Document.description.ilike(search_term)) |
            (Document.tags.ilike(search_term)) |
            (Document.slug.ilike(search_term))
        )

    # Apply sorting
    sort_field = Document.title  # default
    if sort == "last_synced":
        sort_field = Document.last_synced_at
    elif sort == "last_published":
        sort_field = Document.last_published_at
    elif sort == "modified":
        sort_field = Document.drive_modified_at
    elif sort == "created":
        sort_field = Document.created_at

    if order == "desc":
        sort_field = sort_field.desc()
    else:
        # For null-safe sorting, put nulls last
        from sqlalchemy import nullslast
        sort_field = nullslast(sort_field)

    docs = query.order_by(sort_field).all()

    # Get distinct values for filter dropdowns
    projects = [
        r[0] for r in db.query(Document.project).distinct().order_by(Document.project).all()
        if r[0]
    ]

    versions = [
        r[0] for r in db.query(Document.version).distinct().order_by(Document.version).all()
        if r[0]
    ]

    return templates.TemplateResponse(
        "documents.html",
        {
            "request": request,
            "active": "documents",
            "pending_count": _pending_count(db),
            "documents": docs,
            "projects": projects,
            "versions": versions,
            "filter_status": status or "",
            "filter_project": project or "",
            "filter_visibility": visibility or "",
            "filter_version": version or "",
            "search_query": q or "",
            "sort_field": sort or "title",
            "sort_order": order or "asc",
        },
    )


@router.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request, db: Session = Depends(get_db)):
    """Projects page with list and document counts."""
    # Get all projects with document counts
    projects_raw = (
        db.query(Project, func.count(Document.id))
        .outerjoin(Document, Project.name == Document.project)
        .filter(Project.is_active == True)
        .group_by(Project.id)
        .order_by(Project.name)
        .all()
    )

    projects = [
        {
            "id": p.id,
            "name": p.name,
            "slug": p.slug,
            "description": p.description,
            "drive_folder_id": p.drive_folder_id,
            "default_visibility": p.default_visibility,
            "require_approval": p.require_approval,
            "is_active": p.is_active,
            "created_at": str(p.created_at) if p.created_at else "—",
            "document_count": count,
        }
        for p, count in projects_raw
    ]

    return templates.TemplateResponse(
        "projects.html",
        {
            "request": request,
            "active": "projects",
            "pending_count": _pending_count(db),
            "projects": projects,
        },
    )


@router.get("/approvals", response_class=HTMLResponse)
async def approvals_page(request: Request, db: Session = Depends(get_db)):
    pending = (
        db.query(Document)
        .filter(Document.status == "review")
        .order_by(Document.updated_at.desc())
        .all()
    )

    # Recent approval decisions (join with document and user for display)
    recent_approvals_raw = (
        db.query(Approval, Document.title, User.name)
        .join(Document, Approval.document_id == Document.id)
        .join(User, Approval.user_id == User.id)
        .order_by(Approval.created_at.desc())
        .limit(20)
        .all()
    )

    recent_approvals = [
        {
            "doc_title": title,
            "action": a.action,
            "user_name": user_name,
            "comment": a.comment,
            "created_at": str(a.created_at) if a.created_at else "—",
        }
        for a, title, user_name in recent_approvals_raw
    ]

    return templates.TemplateResponse(
        "approvals.html",
        {
            "request": request,
            "active": "approvals",
            "pending_count": len(pending),
            "pending": pending,
            "recent_approvals": recent_approvals,
        },
    )


@router.get("/sync", response_class=HTMLResponse)
async def sync_page(
    request: Request,
    action: str | None = Query(None),
    branch: str | None = Query(None),
    errors: bool = Query(False),
    limit: int = Query(100),
    db: Session = Depends(get_db),
):
    """Sync history page with filtering and statistics."""
    from datetime import datetime, timedelta

    # Build query with filters
    query = db.query(SyncLog, Document.title).join(Document, SyncLog.document_id == Document.id)

    if action:
        query = query.filter(SyncLog.action == action)
    if branch:
        query = query.filter(SyncLog.branch == branch)
    if errors:
        query = query.filter(SyncLog.error.isnot(None))

    logs_raw = query.order_by(SyncLog.created_at.desc()).limit(limit).all()

    raw_logs = [
        {
            "doc_title": title,
            "action": log.action,
            "branch": log.branch,
            "commit_sha": log.commit_sha or "",
            "error": log.error,
            "created_at": str(log.created_at) if log.created_at else "—",
            "created_at_obj": log.created_at,
        }
        for log, title in logs_raw
    ]

    # Collapse noisy pairs from one operation
    sync_logs = []
    skip_next = False
    for i, row in enumerate(raw_logs):
        if skip_next:
            skip_next = False
            continue

        sync_logs.append(row)

        if row["action"] not in {"publish", "publish_preview", "unpublish"}:
            continue
        if i + 1 >= len(raw_logs):
            continue

        next_row = raw_logs[i + 1]
        if next_row["action"] != "sync":
            continue
        if next_row["doc_title"] != row["doc_title"]:
            continue

        t1 = row["created_at_obj"]
        t2 = next_row["created_at_obj"]
        if t1 and t2 and abs((t1 - t2).total_seconds()) <= 2:
            skip_next = True

    # Calculate statistics
    total_ops = db.query(func.count(SyncLog.id)).scalar() or 0
    successful_ops = db.query(func.count(SyncLog.id)).filter(SyncLog.error.is_(None)).scalar() or 0
    error_ops = db.query(func.count(SyncLog.id)).filter(SyncLog.error.isnot(None)).scalar() or 0

    # Recent operations (last 24 hours)
    day_ago = datetime.now() - timedelta(days=1)
    recent_ops = (
        db.query(func.count(SyncLog.id))
        .filter(SyncLog.created_at >= day_ago)
        .scalar() or 0
    )

    stats = {
        "total": total_ops,
        "successful": successful_ops,
        "errors": error_ops,
        "recent": recent_ops,
    }

    # Remove datetime objects from logs
    for row in sync_logs:
        row.pop("created_at_obj", None)

    return templates.TemplateResponse(
        "sync.html",
        {
            "request": request,
            "active": "sync",
            "pending_count": _pending_count(db),
            "sync_logs": sync_logs,
            "stats": stats,
            "filter_action": action or "",
            "filter_branch": branch or "",
            "filter_errors": errors,
            "limit": limit,
        },
    )


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.email).all()

    # TODO: Get current user from session/JWT
    current_user_id = users[0].id if users else None

    return templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "active": "users",
            "pending_count": _pending_count(db),
            "users": users,
            "current_user_id": current_user_id,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "active": "settings",
            "pending_count": _pending_count(db),
        },
    )


@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, db: Session = Depends(get_db)):
    """Onboarding flow for new users."""
    return templates.TemplateResponse(
        "onboarding.html",
        {
            "request": request,
            "active": None,  # No sidebar item active during onboarding
            "pending_count": _pending_count(db),
        },
    )


@router.get("/drive", response_class=HTMLResponse)
async def drive_page(request: Request, db: Session = Depends(get_db)):
    """Google Drive browser page."""
    # Get all active projects
    projects = db.query(Project).filter(Project.is_active == True).order_by(Project.name).all()

    return templates.TemplateResponse(
        "drive.html",
        {
            "request": request,
            "active": "drive",
            "pending_count": _pending_count(db),
            "projects": projects,
        },
    )


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request, db: Session = Depends(get_db)):
    """Analytics dashboard page."""
    return templates.TemplateResponse(
        "analytics.html",
        {
            "request": request,
            "active": "analytics",
            "pending_count": _pending_count(db),
        },
    )


@router.get("/documents/{doc_id}/preview", response_class=HTMLResponse)
async def document_preview_page(doc_id: int, request: Request, db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    if not doc:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Document not found")

    return templates.TemplateResponse(
        "document_preview.html",
        {
            "request": request,
            "active": "documents",
            "pending_count": _pending_count(db),
            "document": doc,
        },
    )
