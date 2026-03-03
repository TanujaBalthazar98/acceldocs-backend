"""Analytics API routes for tracking and reporting."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.auth import AuthUser, get_current_user, require_auth
from app.models import Document, DocumentView, User, SyncLog

router = APIRouter()


class ViewStats(BaseModel):
    """Document view statistics."""
    document_id: int
    document_title: str
    project: str
    total_views: int
    unique_users: int
    last_viewed: str | None


class TrendingDoc(BaseModel):
    """Trending document data."""
    document_id: int
    title: str
    project: str
    views_last_7_days: int
    views_last_30_days: int


class UserActivity(BaseModel):
    """User activity summary."""
    user_id: int
    user_email: str
    user_name: str | None
    total_views: int
    last_active: str | None


class AnalyticsSummary(BaseModel):
    """Overall analytics summary."""
    total_documents: int
    total_views: int
    total_views_last_7_days: int
    total_views_last_30_days: int
    unique_viewers: int
    most_viewed_document: ViewStats | None
    trending_documents: list[TrendingDoc]


@router.post("/track/view/{doc_id}")
async def track_document_view(
    doc_id: int,
    request: Request,
    user: Optional[AuthUser] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Track a document view."""
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Get client IP and user agent
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    referer = request.headers.get("referer")

    # Create view record
    view = DocumentView(
        document_id=doc_id,
        user_id=user.id if user else None,
        user_email=user.email if user else None,
        ip_address=client_ip,
        user_agent=user_agent,
        referer=referer,
    )
    db.add(view)
    db.commit()

    return {"status": "ok", "view_tracked": True}


@router.get("/documents/trending", response_model=list[TrendingDoc])
async def get_trending_documents(
    limit: int = 10,
    current_user: AuthUser = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Get trending documents based on recent view counts."""
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    thirty_days_ago = now - timedelta(days=30)

    # Get view counts for last 7 and 30 days
    docs_with_views = (
        db.query(
            Document.id,
            Document.title,
            Document.project,
            func.count(DocumentView.id).filter(
                DocumentView.viewed_at >= seven_days_ago
            ).label("views_7d"),
            func.count(DocumentView.id).filter(
                DocumentView.viewed_at >= thirty_days_ago
            ).label("views_30d"),
        )
        .outerjoin(DocumentView, Document.id == DocumentView.document_id)
        .group_by(Document.id)
        .order_by(func.count(DocumentView.id).filter(
            DocumentView.viewed_at >= seven_days_ago
        ).desc())
        .limit(limit)
        .all()
    )

    return [
        TrendingDoc(
            document_id=doc_id,
            title=title,
            project=project,
            views_last_7_days=views_7d or 0,
            views_last_30_days=views_30d or 0,
        )
        for doc_id, title, project, views_7d, views_30d in docs_with_views
    ]


@router.get("/documents/stats", response_model=list[ViewStats])
async def get_document_stats(
    project: str | None = None,
    limit: int = 20,
    current_user: AuthUser = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Get view statistics for documents."""
    query = (
        db.query(
            Document.id,
            Document.title,
            Document.project,
            func.count(DocumentView.id).label("total_views"),
            func.count(func.distinct(DocumentView.user_id)).label("unique_users"),
            func.max(DocumentView.viewed_at).label("last_viewed"),
        )
        .outerjoin(DocumentView, Document.id == DocumentView.document_id)
        .group_by(Document.id)
    )

    if project:
        query = query.filter(Document.project == project)

    query = query.order_by(func.count(DocumentView.id).desc()).limit(limit)

    results = query.all()

    return [
        ViewStats(
            document_id=doc_id,
            document_title=title,
            project=project_name,
            total_views=total_views or 0,
            unique_users=unique_users or 0,
            last_viewed=str(last_viewed) if last_viewed else None,
        )
        for doc_id, title, project_name, total_views, unique_users, last_viewed in results
    ]


@router.get("/users/activity", response_model=list[UserActivity])
async def get_user_activity(
    limit: int = 20,
    current_user: AuthUser = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Get user activity statistics."""
    results = (
        db.query(
            User.id,
            User.email,
            User.name,
            func.count(DocumentView.id).label("total_views"),
            func.max(DocumentView.viewed_at).label("last_active"),
        )
        .outerjoin(DocumentView, User.id == DocumentView.user_id)
        .group_by(User.id)
        .order_by(func.count(DocumentView.id).desc())
        .limit(limit)
        .all()
    )

    return [
        UserActivity(
            user_id=user_id,
            user_email=email,
            user_name=name,
            total_views=total_views or 0,
            last_active=str(last_active) if last_active else None,
        )
        for user_id, email, name, total_views, last_active in results
    ]


@router.get("/summary", response_model=AnalyticsSummary)
async def get_analytics_summary(
    current_user: AuthUser = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Get overall analytics summary."""
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)
    thirty_days_ago = now - timedelta(days=30)

    # Total documents
    total_docs = db.query(func.count(Document.id)).scalar() or 0

    # Total views
    total_views = db.query(func.count(DocumentView.id)).scalar() or 0

    # Views last 7 days
    views_7d = (
        db.query(func.count(DocumentView.id))
        .filter(DocumentView.viewed_at >= seven_days_ago)
        .scalar() or 0
    )

    # Views last 30 days
    views_30d = (
        db.query(func.count(DocumentView.id))
        .filter(DocumentView.viewed_at >= thirty_days_ago)
        .scalar() or 0
    )

    # Unique viewers
    unique_viewers = db.query(func.count(func.distinct(DocumentView.user_id))).scalar() or 0

    # Most viewed document
    most_viewed = (
        db.query(
            Document.id,
            Document.title,
            Document.project,
            func.count(DocumentView.id).label("total_views"),
            func.count(func.distinct(DocumentView.user_id)).label("unique_users"),
            func.max(DocumentView.viewed_at).label("last_viewed"),
        )
        .outerjoin(DocumentView, Document.id == DocumentView.document_id)
        .group_by(Document.id)
        .order_by(func.count(DocumentView.id).desc())
        .first()
    )

    most_viewed_doc = None
    if most_viewed:
        doc_id, title, project, total, unique, last = most_viewed
        most_viewed_doc = ViewStats(
            document_id=doc_id,
            document_title=title,
            project=project,
            total_views=total or 0,
            unique_users=unique or 0,
            last_viewed=str(last) if last else None,
        )

    # Trending documents (top 5)
    trending_results = (
        db.query(
            Document.id,
            Document.title,
            Document.project,
            func.count(DocumentView.id).filter(
                DocumentView.viewed_at >= seven_days_ago
            ).label("views_7d"),
            func.count(DocumentView.id).filter(
                DocumentView.viewed_at >= thirty_days_ago
            ).label("views_30d"),
        )
        .outerjoin(DocumentView, Document.id == DocumentView.document_id)
        .group_by(Document.id)
        .order_by(func.count(DocumentView.id).filter(
            DocumentView.viewed_at >= seven_days_ago
        ).desc())
        .limit(5)
        .all()
    )

    trending = [
        TrendingDoc(
            document_id=doc_id,
            title=title,
            project=project,
            views_last_7_days=views_7d or 0,
            views_last_30_days=views_30d or 0,
        )
        for doc_id, title, project, views_7d, views_30d in trending_results
    ]

    return AnalyticsSummary(
        total_documents=total_docs,
        total_views=total_views,
        total_views_last_7_days=views_7d,
        total_views_last_30_days=views_30d,
        unique_viewers=unique_viewers,
        most_viewed_document=most_viewed_doc,
        trending_documents=trending,
    )
