"""Document CRUD API routes."""

import markdown
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.middleware.auth import AuthUser, can_access_document, get_current_user, require_role
from app.models import Document, DocumentView
from app.publishing.git_publisher import unpublish_from_production

router = APIRouter()


def _track_view(doc_id: int, user: Optional[AuthUser], request: Request, db: Session):
    """Helper function to track document views."""
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    referer = request.headers.get("referer")

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


class DocumentStatusUpdate(BaseModel):
    status: str


class BulkActionRequest(BaseModel):
    document_ids: list[int]
    action: str
    value: str | None = None  # For actions like set_status or set_visibility


class SearchStats(BaseModel):
    total: int
    projects: list[str]
    versions: list[str]
    statuses: list[str]
    visibilities: list[str]


@router.get("/", response_model=list[DocumentOut])
async def list_documents(
    project: str | None = Query(None),
    status: str | None = Query(None),
    visibility: str | None = Query(None),
    version: str | None = Query(None),
    q: str | None = Query(None),  # Search query
    sort: str | None = Query(None),  # Sort field
    order: str | None = Query("asc"),  # Sort order
    limit: int | None = Query(None),
    offset: int | None = Query(0),
    user: Optional[AuthUser] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List documents with optional filtering, search, and sorting."""
    query = db.query(Document)

    # Apply filters
    if project:
        query = query.filter(Document.project == project)
    if status:
        query = query.filter(Document.status == status)
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

    query = query.order_by(sort_field)

    # Apply pagination
    if limit:
        query = query.limit(limit)
    if offset:
        query = query.offset(offset)

    # Filter by visibility - only return documents user can access
    all_docs = query.all()
    return [doc for doc in all_docs if can_access_document(user, doc.visibility)]


@router.get("/search/stats", response_model=SearchStats)
async def get_search_stats(
    user: Optional[AuthUser] = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get statistics for search filters (distinct values and counts)."""
    from sqlalchemy import func, distinct

    # Get all documents and filter by visibility
    all_docs = db.query(Document).all()
    accessible_docs = [doc for doc in all_docs if can_access_document(user, doc.visibility)]

    total = len(accessible_docs)

    # Extract distinct values from accessible documents
    projects = sorted(list(set(doc.project for doc in accessible_docs if doc.project)))
    versions = sorted(list(set(doc.version for doc in accessible_docs if doc.version)))
    statuses = sorted(list(set(doc.status for doc in accessible_docs if doc.status)))
    visibilities = sorted(list(set(doc.visibility for doc in accessible_docs if doc.visibility)))

    return SearchStats(
        total=total,
        projects=projects,
        versions=versions,
        statuses=statuses,
        visibilities=visibilities,
    )


@router.get("/{doc_id}", response_model=DocumentOut)
async def get_document(
    doc_id: int,
    user: Optional[AuthUser] = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Check if user can access this document based on visibility
    if not can_access_document(user, doc.visibility):
        raise HTTPException(
            status_code=403,
            detail="This document is internal-only and requires authentication"
        )

    return doc


@router.post("/{doc_id}/status")
async def update_document_status(
    doc_id: int,
    body: DocumentStatusUpdate,
    user: AuthUser = Depends(require_role("editor")),
    db: Session = Depends(get_db),
):
    """Update document status. Requires editor role or higher."""
    allowed = {"draft", "review", "approved", "rejected"}
    if body.status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Allowed: {', '.join(sorted(allowed))}",
        )

    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    doc.status = body.status
    doc.is_published = body.status == "approved"
    if body.status in {"draft", "rejected"}:
        unpublish_from_production(
            project=doc.project,
            version=doc.version,
            section=doc.section,
            slug=doc.slug,
        )
        doc.last_published_at = None
    db.commit()
    return {"status": "ok", "document_id": doc_id, "document_status": doc.status}


@router.post("/bulk")
async def bulk_action(
    body: BulkActionRequest,
    user: AuthUser = Depends(require_role("editor")),
    db: Session = Depends(get_db),
):
    """Perform bulk actions on multiple documents. Requires editor role or higher."""
    if not body.document_ids:
        raise HTTPException(status_code=400, detail="No documents selected")

    if len(body.document_ids) > 100:
        raise HTTPException(status_code=400, detail="Cannot process more than 100 documents at once")

    # Validate documents exist
    docs = db.query(Document).filter(Document.id.in_(body.document_ids)).all()
    if len(docs) != len(body.document_ids):
        raise HTTPException(status_code=404, detail="Some documents not found")

    success_count = 0
    error_count = 0
    errors = []

    try:
        if body.action == "set_status":
            if not body.value or body.value not in {"draft", "review", "approved", "rejected"}:
                raise HTTPException(status_code=400, detail="Invalid status value")

            for doc in docs:
                try:
                    doc.status = body.value
                    doc.is_published = body.value == "approved"
                    if body.value in {"draft", "rejected"}:
                        unpublish_from_production(
                            project=doc.project,
                            version=doc.version,
                            section=doc.section,
                            slug=doc.slug,
                        )
                        doc.last_published_at = None
                    success_count += 1
                except Exception as e:
                    error_count += 1
                    errors.append(f"Doc {doc.id}: {str(e)}")

        elif body.action == "set_visibility":
            if not body.value or body.value not in {"public", "internal"}:
                raise HTTPException(status_code=400, detail="Invalid visibility value")

            for doc in docs:
                try:
                    doc.visibility = body.value
                    success_count += 1
                except Exception as e:
                    error_count += 1
                    errors.append(f"Doc {doc.id}: {str(e)}")

        elif body.action == "delete":
            for doc in docs:
                try:
                    # Unpublish first
                    unpublish_from_production(
                        project=doc.project,
                        version=doc.version,
                        section=doc.section,
                        slug=doc.slug,
                    )
                    db.delete(doc)
                    success_count += 1
                except Exception as e:
                    error_count += 1
                    errors.append(f"Doc {doc.id}: {str(e)}")

        elif body.action == "approve":
            for doc in docs:
                try:
                    doc.status = "approved"
                    success_count += 1
                except Exception as e:
                    error_count += 1
                    errors.append(f"Doc {doc.id}: {str(e)}")

        elif body.action == "reject":
            for doc in docs:
                try:
                    doc.status = "rejected"
                    unpublish_from_production(
                        project=doc.project,
                        version=doc.version,
                        section=doc.section,
                        slug=doc.slug,
                    )
                    doc.last_published_at = None
                    success_count += 1
                except Exception as e:
                    error_count += 1
                    errors.append(f"Doc {doc.id}: {str(e)}")

        elif body.action == "resync":
            # Re-sync from Google Drive - trigger sync for specific docs
            # This would require implementing selective sync logic
            raise HTTPException(status_code=501, detail="Resync action not yet implemented")

        else:
            raise HTTPException(status_code=400, detail=f"Unknown action: {body.action}")

        db.commit()

        return {
            "status": "ok",
            "action": body.action,
            "total": len(body.document_ids),
            "success": success_count,
            "errors": error_count,
            "error_details": errors if errors else None,
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Bulk operation failed: {str(e)}")


@router.get("/{doc_id}/raw")
async def get_document_raw_markdown(
    doc_id: int,
    request: Request,
    user: Optional[AuthUser] = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get raw markdown source for a document."""
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Check if user can access this document based on visibility
    if not can_access_document(user, doc.visibility):
        raise HTTPException(
            status_code=403,
            detail="This document is internal-only and requires authentication"
        )

    # Track view
    _track_view(doc_id, user, request, db)

    # Build path to markdown file using same normalization as publishing code
    repo_path = Path(settings.docs_repo_path)
    parts = ["docs", _safe_path(doc.project)]
    if doc.version:
        parts.append(_safe_path(doc.version))
    if doc.section:
        for part in doc.section.split("/"):
            if part.strip():
                parts.append(_safe_path(part))
    parts.append(f"{_safe_path(doc.slug)}.md")

    md_path = repo_path / Path(*parts)

    if not md_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Markdown file not found at {md_path.relative_to(repo_path)}",
        )

    content = md_path.read_text(encoding="utf-8")
    return {"content": content, "path": str(md_path.relative_to(repo_path))}


def _safe_path(name: str) -> str:
    """Normalize folder/file names to filesystem-safe lowercase hyphenated form."""
    return name.replace(" ", "-").replace("/", "-").lower().strip("-")


@router.get("/{doc_id}/preview")
async def get_document_preview(
    doc_id: int,
    request: Request,
    user: Optional[AuthUser] = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get rendered HTML preview of a document."""
    doc = db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # Check if user can access this document based on visibility
    if not can_access_document(user, doc.visibility):
        raise HTTPException(
            status_code=403,
            detail="This document is internal-only and requires authentication"
        )

    # Track view
    _track_view(doc_id, user, request, db)

    # Build path to markdown file
    repo_path = Path(settings.docs_repo_path)

    # Check if repo exists
    if not repo_path.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Docs repository not found at {settings.docs_repo_path}. Please configure DOCS_REPO_PATH in settings.",
        )

    # Use same path normalization as publishing code
    parts = ["docs", _safe_path(doc.project)]
    if doc.version:
        parts.append(_safe_path(doc.version))
    if doc.section:
        for part in doc.section.split("/"):
            if part.strip():
                parts.append(_safe_path(part))
    parts.append(f"{_safe_path(doc.slug)}.md")

    md_path = repo_path / Path(*parts)

    if not md_path.exists():
        # Show expected path for debugging
        expected_path = str(md_path.relative_to(repo_path))
        raise HTTPException(
            status_code=404,
            detail=f"Markdown file not found. Expected: {expected_path}",
        )

    md_content = md_path.read_text(encoding="utf-8")

    # Render markdown to HTML with extensions
    html = markdown.markdown(
        md_content,
        extensions=[
            "fenced_code",
            "codehilite",
            "tables",
            "toc",
            "nl2br",
            "sane_lists",
        ],
        extension_configs={
            "codehilite": {
                "css_class": "highlight",
                "linenums": False,
            }
        },
    )

    return {
        "html": html,
        "markdown": md_content,
        "path": str(md_path.relative_to(repo_path)),
    }
