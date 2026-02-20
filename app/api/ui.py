"""Server-rendered admin dashboard using Jinja2 templates."""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Approval, Document, SyncLog, User

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
    db: Session = Depends(get_db),
):
    query = db.query(Document)
    if status:
        query = query.filter(Document.status == status)
    if project:
        query = query.filter(Document.project == project)

    docs = query.order_by(Document.project, Document.version, Document.title).all()

    # Get distinct projects for the filter dropdown
    projects = [
        r[0] for r in db.query(Document.project).distinct().order_by(Document.project).all()
    ]

    return templates.TemplateResponse(
        "documents.html",
        {
            "request": request,
            "active": "documents",
            "pending_count": _pending_count(db),
            "documents": docs,
            "projects": projects,
            "filter_status": status or "",
            "filter_project": project or "",
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
async def sync_page(request: Request, db: Session = Depends(get_db)):
    # Recent sync logs with document titles
    logs_raw = (
        db.query(SyncLog, Document.title)
        .join(Document, SyncLog.document_id == Document.id)
        .order_by(SyncLog.created_at.desc())
        .limit(50)
        .all()
    )

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

    # Collapse noisy pairs from one operation:
    # sync -> (publish|unpublish) for the same doc within the same moment.
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

    # template doesn't need raw datetime object
    for row in sync_logs:
        row.pop("created_at_obj", None)

    return templates.TemplateResponse(
        "sync.html",
        {
            "request": request,
            "active": "sync",
            "pending_count": _pending_count(db),
            "sync_logs": sync_logs,
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
