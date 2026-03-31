"""Pages API — CRUD, Drive sync, and publish for individual documentation pages."""

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Header
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.api.drive import _create_drive_doc, _trash_drive_item, _move_drive_item, get_drive_credentials as _get_drive_creds_drive
from app.auth.routes import get_current_user
from app.database import get_db
from app.models import (
    Approval,
    Organization,
    OrgRole,
    Page,
    PageComment,
    PageFeedback,
    PageRedirect,
    Section,
    User,
)
from app.lib.markdown_import import normalize_imported_markdown
from app.services.pages import (
    apply_publish,
    apply_sync_result,
    create_duplicate_record,
    create_page_record,
    unique_slug as _unique_slug_svc,
)

logger = logging.getLogger(__name__)
router = APIRouter()

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"

# Strip leading numeric sort-prefixes from Drive doc names ("01 ", "2. ")
_NUM_PREFIX = re.compile(r"^\d+[\.\s]+")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class PageCreate(BaseModel):
    google_doc_id: str | None = None
    section_id: int | None = None
    title: str | None = None
    display_order: int = 0


class PageUpdate(BaseModel):
    section_id: int | None = None
    title: str | None = None
    slug: str | None = None
    visibility_override: Literal["public", "internal", "external"] | None = None
    display_order: int | None = None
    hide_toc: bool | None = None
    full_width: bool | None = None
    page_custom_css: str | None = None
    featured_image_url: str | None = None


class PageImport(BaseModel):
    title: str
    markdown_content: str = ""
    section_id: int | None = None
    display_order: int = 0
    create_drive_doc: bool = False  # If True, also create a Google Doc in the section's Drive folder


def _page_dict(p: Page, include_html: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": p.id,
        "organization_id": p.organization_id,
        "section_id": p.section_id,
        "google_doc_id": p.google_doc_id,
        "title": p.title,
        "slug": p.slug,
        "slug_locked": p.slug_locked,
        "visibility_override": p.visibility_override,
        "is_published": p.is_published,
        "status": p.status,
        "display_order": p.display_order,
        "drive_modified_at": p.drive_modified_at,
        "last_synced_at": p.last_synced_at,
        "owner_id": p.owner_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        "hide_toc": getattr(p, "hide_toc", False) or False,
        "full_width": getattr(p, "full_width", False) or False,
        "page_custom_css": getattr(p, "page_custom_css", None),
        "featured_image_url": getattr(p, "featured_image_url", None),
    }
    if include_html:
        d["html_content"] = p.html_content
        d["published_html"] = p.published_html
    return d


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------

async def _get_drive_credentials(user: User, db: Session, org_id: int, *, require_write: bool = True) -> Credentials:
    """Shared credential lookup with workspace fallback logic."""
    return await _get_drive_creds_drive(user, org_id, db, require_write=require_write)


async def _get_drive_credentials_compat(user: User, db: Session, org_id: int, *, require_write: bool = True) -> Credentials:
    """Compatibility layer for tests that monkeypatch the older 2-arg helper."""
    try:
        return await _get_drive_credentials(user, db, org_id, require_write=require_write)
    except TypeError:
        return await _get_drive_credentials(user, db)  # type: ignore[misc]


async def _export_html(google_doc_id: str, creds: Credentials) -> tuple[str, str | None, str | None]:
    """Return (html_content, modified_at) for a Google Doc."""
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    # Get metadata (title + modified time)
    meta = service.files().get(
        fileId=google_doc_id,
        fields="name,modifiedTime",
        supportsAllDrives=True,
    ).execute()

    # Export as HTML
    raw = service.files().export(fileId=google_doc_id, mimeType="text/html").execute()
    html = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    return html, meta.get("modifiedTime"), meta.get("name")


def _rename_drive_doc(service, file_id: str, title: str) -> str:
    """Rename a Google Doc in Drive and return resulting title."""
    result = (
        service.files()
        .update(
            fileId=file_id,
            body={"name": title},
            fields="id,name",
            supportsAllDrives=True,
        )
        .execute()
    )
    return result.get("name") or title


def _copy_drive_doc(service, source_file_id: str, copy_title: str, parent_id: str | None) -> tuple[str, str | None]:
    """Copy a Google Doc in Drive and return (new_file_id, modified_time)."""
    body: dict[str, Any] = {"name": copy_title}
    if parent_id:
        body["parents"] = [parent_id]
    result = (
        service.files()
        .copy(
            fileId=source_file_id,
            body=body,
            fields="id,modifiedTime",
            supportsAllDrives=True,
        )
        .execute()
    )
    new_id = result.get("id")
    if not new_id:
        raise HTTPException(status_code=502, detail="Drive did not return copied document ID")
    return new_id, result.get("modifiedTime")


# ---------------------------------------------------------------------------
# Org / auth helpers
# ---------------------------------------------------------------------------

def _resolve_org_role(user: User, db: Session, requested_org_id: int | None = None) -> OrgRole:
    query = db.query(OrgRole).filter(OrgRole.user_id == user.id)
    if requested_org_id is not None:
        query = query.filter(OrgRole.organization_id == requested_org_id)
    role = query.first()
    if not role:
        raise HTTPException(status_code=403, detail="User has no organization")
    return role


def _get_org_id(user: User, db: Session, requested_org_id: int | None = None) -> int:
    return _resolve_org_role(user, db, requested_org_id).organization_id


def _require_editor(user: User, db: Session, requested_org_id: int | None = None) -> int:
    role = _resolve_org_role(user, db, requested_org_id)
    if not role or role.role not in ("owner", "admin", "editor"):
        raise HTTPException(status_code=403, detail="Editor role required")
    return role.organization_id


def _require_reviewer(user: User, db: Session, requested_org_id: int | None = None) -> int:
    role = _resolve_org_role(user, db, requested_org_id)
    if not role or role.role not in ("owner", "admin", "reviewer"):
        raise HTTPException(status_code=403, detail="Reviewer role required")
    return role.organization_id


def _unique_slug(base_value: str, org_id: int, db: Session, exclude_id: int | None = None, *, strip_numeric_prefix: bool = True) -> str:
    return _unique_slug_svc(base_value, org_id, db, exclude_id, strip_numeric_prefix=strip_numeric_prefix)


def _upsert_page_redirect(
    db: Session,
    *,
    organization_id: int,
    source_page_id: int | None,
    source_slug: str,
    target_page_id: int | None = None,
    target_url: str | None = None,
    status_code: int = 307,
) -> None:
    source_slug_clean = (source_slug or "").strip()
    if not source_slug_clean:
        return

    query = db.query(PageRedirect).filter(
        PageRedirect.organization_id == organization_id,
        PageRedirect.source_slug == source_slug_clean,
    )
    if source_page_id is None:
        query = query.filter(PageRedirect.source_page_id.is_(None))
    else:
        query = query.filter(PageRedirect.source_page_id == source_page_id)

    existing = query.first()
    if existing:
        existing.target_page_id = target_page_id
        existing.target_url = target_url
        existing.status_code = status_code
        existing.is_active = True
        return

    db.add(
        PageRedirect(
            organization_id=organization_id,
            source_page_id=source_page_id,
            source_slug=source_slug_clean,
            target_page_id=target_page_id,
            target_url=target_url,
            status_code=status_code,
            is_active=True,
        )
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/engagement/overview")
def engagement_overview(
    limit: int = 10,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Organization-level feedback/comment insights for dashboard analytics."""
    role = _resolve_org_role(user, db, x_org_id)
    org_id = role.organization_id
    safe_limit = max(1, min(limit, 50))

    feedback_agg = (
        db.query(
            PageFeedback.page_id.label("page_id"),
            func.sum(case((PageFeedback.vote == "up", 1), else_=0)).label("up"),
            func.sum(case((PageFeedback.vote == "down", 1), else_=0)).label("down"),
            func.max(PageFeedback.created_at).label("last_feedback_at"),
        )
        .filter(PageFeedback.organization_id == org_id)
        .group_by(PageFeedback.page_id)
        .subquery()
    )

    comments_agg = (
        db.query(
            PageComment.page_id.label("page_id"),
            func.count(PageComment.id).label("comments"),
            func.max(PageComment.created_at).label("last_comment_at"),
        )
        .filter(
            PageComment.organization_id == org_id,
            PageComment.is_deleted == False,  # noqa: E712
        )
        .group_by(PageComment.page_id)
        .subquery()
    )

    page_rows = (
        db.query(
            Page.id.label("page_id"),
            Page.title.label("page_title"),
            Page.slug.label("page_slug"),
            func.coalesce(feedback_agg.c.up, 0).label("up"),
            func.coalesce(feedback_agg.c.down, 0).label("down"),
            func.coalesce(comments_agg.c.comments, 0).label("comments"),
            feedback_agg.c.last_feedback_at.label("last_feedback_at"),
            comments_agg.c.last_comment_at.label("last_comment_at"),
        )
        .outerjoin(feedback_agg, feedback_agg.c.page_id == Page.id)
        .outerjoin(comments_agg, comments_agg.c.page_id == Page.id)
        .filter(Page.organization_id == org_id)
        .filter(
            (func.coalesce(feedback_agg.c.up, 0) + func.coalesce(feedback_agg.c.down, 0) + func.coalesce(comments_agg.c.comments, 0))
            > 0
        )
        .order_by(
            (
                func.coalesce(feedback_agg.c.up, 0)
                + func.coalesce(feedback_agg.c.down, 0)
                + func.coalesce(comments_agg.c.comments, 0)
            ).desc(),
            func.coalesce(comments_agg.c.last_comment_at, feedback_agg.c.last_feedback_at).desc(),
        )
        .limit(safe_limit)
        .all()
    )

    pages_payload: list[dict[str, Any]] = []
    for row in page_rows:
        up = int(row.up or 0)
        down = int(row.down or 0)
        total_feedback = up + down
        total_comments = int(row.comments or 0)
        helpful_ratio = round((up / total_feedback) * 100, 1) if total_feedback > 0 else None
        last_feedback_at = row.last_feedback_at.isoformat() if row.last_feedback_at else None
        last_comment_at = row.last_comment_at.isoformat() if row.last_comment_at else None
        last_activity_at = max(filter(None, [last_feedback_at, last_comment_at]), default=None)
        pages_payload.append(
            {
                "page_id": int(row.page_id),
                "page_title": row.page_title,
                "page_slug": row.page_slug,
                "up": up,
                "down": down,
                "total_feedback": total_feedback,
                "total_comments": total_comments,
                "helpful_ratio": helpful_ratio,
                "last_feedback_at": last_feedback_at,
                "last_comment_at": last_comment_at,
                "last_activity_at": last_activity_at,
            }
        )

    recent_comments_rows = (
        db.query(PageComment, Page.title)
        .join(Page, Page.id == PageComment.page_id)
        .filter(
            PageComment.organization_id == org_id,
            Page.organization_id == org_id,
            PageComment.is_deleted == False,  # noqa: E712
        )
        .order_by(PageComment.created_at.desc())
        .limit(safe_limit)
        .all()
    )
    recent_comments = [
        {
            "id": comment.id,
            "page_id": comment.page_id,
            "page_title": page_title,
            "display_name": comment.display_name or "User",
            "user_email": comment.user_email,
            "body": comment.body,
            "source": comment.source,
            "created_at": comment.created_at.isoformat() if comment.created_at else None,
        }
        for comment, page_title in recent_comments_rows
    ]

    feedback_totals = (
        db.query(
            func.sum(case((PageFeedback.vote == "up", 1), else_=0)).label("up"),
            func.sum(case((PageFeedback.vote == "down", 1), else_=0)).label("down"),
            func.count(PageFeedback.id).label("total"),
        )
        .filter(PageFeedback.organization_id == org_id)
        .one()
    )
    total_comments = (
        db.query(func.count(PageComment.id))
        .filter(
            PageComment.organization_id == org_id,
            PageComment.is_deleted == False,  # noqa: E712
        )
        .scalar()
        or 0
    )

    return {
        "summary": {
            "total_feedback": int(feedback_totals.total or 0),
            "helpful": int(feedback_totals.up or 0),
            "not_helpful": int(feedback_totals.down or 0),
            "total_comments": int(total_comments),
            "pages_with_feedback": int(
                db.query(func.count(func.distinct(PageFeedback.page_id)))
                .filter(PageFeedback.organization_id == org_id)
                .scalar()
                or 0
            ),
            "commented_pages": int(
                db.query(func.count(func.distinct(PageComment.page_id)))
                .filter(
                    PageComment.organization_id == org_id,
                    PageComment.is_deleted == False,  # noqa: E712
                )
                .scalar()
                or 0
            ),
        },
        "pages": pages_payload,
        "recent_comments": recent_comments,
    }


@router.get("/{page_id}/engagement")
def page_engagement_detail(
    page_id: int,
    limit: int = 20,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Page-level feedback/comment feed for dashboard moderation."""
    org_id = _get_org_id(user, db, x_org_id)
    safe_limit = max(1, min(limit, 100))
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    # Aggregate totals from ALL feedback (not limited)
    totals = (
        db.query(
            func.sum(case((PageFeedback.vote == "up", 1), else_=0)).label("up"),
            func.sum(case((PageFeedback.vote == "down", 1), else_=0)).label("down"),
        )
        .filter(PageFeedback.organization_id == org_id, PageFeedback.page_id == page_id)
        .one()
    )
    up_count = int(totals.up or 0)
    down_count = int(totals.down or 0)

    feedback_rows = (
        db.query(PageFeedback)
        .filter(PageFeedback.organization_id == org_id, PageFeedback.page_id == page_id)
        .order_by(PageFeedback.created_at.desc())
        .limit(safe_limit)
        .all()
    )
    comments_rows = (
        db.query(PageComment)
        .filter(
            PageComment.organization_id == org_id,
            PageComment.page_id == page_id,
            PageComment.is_deleted == False,  # noqa: E712
        )
        .order_by(PageComment.created_at.desc())
        .limit(safe_limit)
        .all()
    )

    return {
        "page": {"id": page.id, "title": page.title, "slug": page.slug},
        "feedback": {
            "up": up_count,
            "down": down_count,
            "total": up_count + down_count,
            "items": [
                {
                    "id": row.id,
                    "vote": row.vote,
                    "message": row.message,
                    "user_email": row.user_email,
                    "source": row.source,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in feedback_rows
            ],
        },
        "comments": [
            {
                "id": row.id,
                "display_name": row.display_name or "User",
                "user_email": row.user_email,
                "body": row.body,
                "source": row.source,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in comments_rows
        ],
    }


@router.get("")
def list_pages(
    section_id: int | None = None,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """List all pages for the current org, optionally filtered by section."""
    role = _resolve_org_role(user, db, x_org_id)
    org_id = role.organization_id
    q = db.query(Page).filter(Page.organization_id == org_id)
    if section_id is not None:
        q = q.filter(Page.section_id == section_id)
    # Viewers can only see published pages
    if role.role == "viewer":
        q = q.filter(Page.is_published == True)  # noqa: E712
    pages = q.order_by(Page.section_id.nulls_last(), Page.display_order, Page.title).all()
    return {"pages": [_page_dict(p) for p in pages]}


@router.post("/import", status_code=201)
async def import_page(
    body: PageImport,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Create a page directly from Markdown content.

    Intended for bulk migration tooling. Converts ``markdown_content`` to HTML
    using the same normalization pipeline used by the Drive import flow.

    By default stores a synthetic ``imported-<uuid>`` in ``google_doc_id`` so
    the unique constraint is satisfied without Drive access.

    If ``create_drive_doc=True``, also creates a real Google Doc in the
    section's Drive folder so the page can be edited in Google Docs later.
    Drive must be connected to the org for this to work.
    """
    org_id = _require_editor(user, db, x_org_id)

    title = (body.title or "Untitled").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")

    section: Section | None = None
    if body.section_id is not None:
        section = db.query(Section).filter(
            Section.id == body.section_id,
            Section.organization_id == org_id,
        ).first()
        if not section:
            raise HTTPException(status_code=404, detail="Section not found")

    # Convert markdown → HTML using the shared normalization pipeline.
    try:
        import markdown as _md
        normalized = normalize_imported_markdown(body.markdown_content or "")
        html_content = _md.markdown(
            normalized,
            extensions=[
                "tables",
                "fenced_code",
                "codehilite",
                "toc",
                "nl2br",
                "sane_lists",
                "admonition",
                "attr_list",
            ],
        )
    except Exception as exc:
        logger.warning("Markdown conversion failed for import page '%s': %s", title, exc)
        html_content = f"<p>{body.markdown_content or ''}</p>"

    # Optionally create a real Google Drive doc so the page can be edited later.
    google_doc_id: str
    if body.create_drive_doc:
        try:
            creds = await _get_drive_creds_drive(user, org_id, db)
            service = build("drive", "v3", credentials=creds)
            # Use the section's Drive folder if available, else fall back to org folder
            parent_folder_id: str | None = None
            if section and section.drive_folder_id:
                parent_folder_id = section.drive_folder_id
            else:
                from app.models import Organization
                org = db.query(Organization).filter_by(id=org_id).first()
                parent_folder_id = org.drive_folder_id if org else None
            google_doc_id = _create_drive_doc(service, title, parent_folder_id)
            logger.info(
                "Created Drive doc '%s' → %s (section folder: %s)",
                title,
                google_doc_id,
                parent_folder_id,
            )
        except Exception as exc:
            logger.warning(
                "Drive doc creation failed for '%s': %s — falling back to synthetic ID",
                title,
                exc,
            )
            google_doc_id = f"imported-{uuid.uuid4().hex}"
    else:
        # Synthetic google_doc_id: satisfies NOT NULL + unique constraint without Drive.
        google_doc_id = f"imported-{uuid.uuid4().hex}"

    page = create_page_record(
        db,
        org_id=org_id,
        section_id=body.section_id,
        google_doc_id=google_doc_id,
        title=title,
        html_content=html_content,
        modified_at=None,
        display_order=body.display_order,
        owner_id=user.id,
    )
    db.commit()
    db.refresh(page)
    drive_note = "with Drive doc" if body.create_drive_doc else "no Drive"
    logger.info(
        "Imported page %d '%s' (%s) for org %d",
        page.id,
        page.title,
        drive_note,
        org_id,
    )
    return _page_dict(page, include_html=True)


@router.post("", status_code=201)
async def create_page(
    body: PageCreate,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Create a page from a Google Doc ID. Fetches title + HTML immediately."""
    org_id = _require_editor(user, db, x_org_id)

    creds = await _get_drive_credentials_compat(user, db, org_id)

    google_doc_id = body.google_doc_id

    if not google_doc_id:
        # No doc ID supplied — create a blank Google Doc in Drive
        title_for_doc = (body.title or "Untitled").strip()
        try:
            drive_creds = await _get_drive_creds_drive(user, org_id, db)
            service = build("drive", "v3", credentials=drive_creds, cache_discovery=False)

            parent_drive_id: str | None = None
            if body.section_id:
                sec = db.get(Section, body.section_id)
                parent_drive_id = sec.drive_folder_id if sec else None
            if not parent_drive_id:
                org = db.get(Organization, org_id)
                parent_drive_id = org.drive_folder_id if org else None

            google_doc_id = _create_drive_doc(service, title_for_doc, parent_drive_id)
            logger.info("Created Drive doc %s for new page '%s'", google_doc_id, title_for_doc)
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"No Google Doc ID provided and Drive doc creation failed: {exc}. "
                       "Provide a Google Doc ID or reconnect Drive.",
            ) from exc

    # Check duplicate
    existing = db.query(Page).filter(
        Page.organization_id == org_id,
        Page.google_doc_id == google_doc_id,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Page with this Google Doc ID already exists")

    # Fetch from Drive to get real title and initial HTML
    html, modified_at, drive_title = await _export_html(google_doc_id, creds)

    title = body.title or drive_title or google_doc_id
    title = _NUM_PREFIX.sub("", title).strip() or title

    page = create_page_record(
        db,
        org_id=org_id,
        section_id=body.section_id,
        google_doc_id=google_doc_id,
        title=title,
        html_content=html,
        modified_at=modified_at,
        display_order=body.display_order,
        owner_id=user.id,
    )
    db.commit()
    db.refresh(page)
    logger.info("Created page %d '%s' for org %d", page.id, page.title, org_id)
    return _page_dict(page, include_html=True)


@router.get("/{page_id}")
def get_page(
    page_id: int,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    role = _resolve_org_role(user, db, x_org_id)
    org_id = role.organization_id
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    # Viewers can only access published pages; hide draft HTML content
    if role.role == "viewer":
        if not page.is_published:
            raise HTTPException(status_code=403, detail="Page not published")
        d = _page_dict(page, include_html=True)
        d["html_content"] = d.get("published_html") or d.get("html_content")
        return d
    return _page_dict(page, include_html=True)


@router.patch("/{page_id}")
async def update_page(
    page_id: int,
    body: PageUpdate,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_id = _require_editor(user, db, x_org_id)
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    old_section_id = page.section_id
    old_slug = page.slug
    section_id_changed = False
    title_changed = body.title is not None and body.title.strip() != page.title
    slug_changed = body.slug is not None
    drive_service = None

    if title_changed and not body.title.strip():
        raise HTTPException(status_code=400, detail="Title cannot be empty")

    if page.google_doc_id and (title_changed or (body.section_id is not None and body.section_id != old_section_id)):
        creds = await _get_drive_credentials_compat(user, db, org_id)
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)

    if body.section_id is not None:
        section_id_changed = body.section_id != old_section_id
        page.section_id = body.section_id

    if title_changed:
        new_title = body.title.strip()
        if drive_service and page.google_doc_id:
            try:
                _rename_drive_doc(drive_service, page.google_doc_id, new_title)
                logger.info("Renamed Drive doc %s to '%s'", page.google_doc_id, new_title)
            except Exception as exc:
                logger.warning("Could not rename Drive doc for page %d: %s", page_id, exc)
                raise HTTPException(status_code=502, detail="Failed to rename Google Doc title") from exc
        page.title = new_title
        if not page.slug_locked and not page.is_published and not slug_changed:
            page.slug = _unique_slug(page.title, org_id, db, exclude_id=page_id)

    if slug_changed:
        raw_slug = (body.slug or "").strip()
        if not raw_slug:
            raise HTTPException(status_code=400, detail="Slug cannot be empty")
        page.slug = _unique_slug(raw_slug, org_id, db, exclude_id=page_id, strip_numeric_prefix=False)
        page.slug_locked = True
        if old_slug != page.slug:
            _upsert_page_redirect(
                db,
                organization_id=org_id,
                source_page_id=page.id,
                source_slug=old_slug,
                target_page_id=page.id,
                status_code=307,
            )

    if body.display_order is not None:
        page.display_order = body.display_order
    if "visibility_override" in body.model_fields_set:
        page.visibility_override = body.visibility_override
    if body.hide_toc is not None:
        page.hide_toc = body.hide_toc
    if body.full_width is not None:
        page.full_width = body.full_width
    if "page_custom_css" in body.model_fields_set:
        page.page_custom_css = body.page_custom_css
    if "featured_image_url" in body.model_fields_set:
        page.featured_image_url = body.featured_image_url

    db.commit()
    db.refresh(page)

    # Mirror section change in Drive
    if section_id_changed and page.google_doc_id:
        try:
            new_drive_parent: str | None = None
            if page.section_id:
                sec = db.get(Section, page.section_id)
                new_drive_parent = sec.drive_folder_id if sec else None
            if not new_drive_parent:
                org = db.get(Organization, org_id)
                new_drive_parent = org.drive_folder_id if org else None
            if new_drive_parent:
                if not drive_service:
                    creds = await _get_drive_credentials_compat(user, db, org_id)
                    drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
                _move_drive_item(drive_service, page.google_doc_id, new_drive_parent)
                logger.info("Moved Drive doc %s to folder %s", page.google_doc_id, new_drive_parent)
        except Exception as exc:
            logger.warning("Could not move Drive doc for page %d: %s", page_id, exc)

    return _page_dict(page)


@router.post("/{page_id}/duplicate", status_code=201)
async def duplicate_page(
    page_id: int,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Duplicate a page in Drive and create a sibling page right below the source."""
    org_id = _require_editor(user, db, x_org_id)
    source = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Page not found")
    if not source.google_doc_id:
        raise HTTPException(status_code=400, detail="Source page has no Google Doc ID")

    creds = await _get_drive_credentials_compat(user, db, org_id)
    drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)

    copy_title = f"{source.title} Copy"

    target_parent: str | None = None
    if source.section_id:
        section = db.get(Section, source.section_id)
        target_parent = section.drive_folder_id if section else None
    if not target_parent:
        org = db.get(Organization, org_id)
        target_parent = org.drive_folder_id if org else None

    try:
        copied_doc_id, copied_modified_at = _copy_drive_doc(
            drive_service,
            source.google_doc_id,
            copy_title,
            target_parent,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Could not duplicate Drive doc for page %d: %s", page_id, exc)
        raise HTTPException(status_code=502, detail="Failed to duplicate Google Doc") from exc

    try:
        html, modified_at, drive_title = await _export_html(copied_doc_id, creds)
    except Exception as exc:
        logger.warning("Drive copy created (%s) but export failed: %s", copied_doc_id, exc)
        html, modified_at, drive_title = source.html_content or "", copied_modified_at, copy_title

    duplicate = create_duplicate_record(
        db,
        source=source,
        org_id=org_id,
        copied_doc_id=copied_doc_id,
        html=html,
        modified_at=modified_at,
        drive_title=drive_title,
        copy_title=copy_title,
        owner_id=user.id,
    )
    db.commit()
    db.refresh(duplicate)
    logger.info("Duplicated page %d -> %d (Drive doc %s)", source.id, duplicate.id, copied_doc_id)
    return _page_dict(duplicate, include_html=True)


@router.delete("/{page_id}")
async def delete_page(
    page_id: int,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_id = _require_editor(user, db, x_org_id)
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    google_doc_id = page.google_doc_id

    # Preserve old links for deleted pages: route historical page IDs/slugs to landing.
    existing_redirects = db.query(PageRedirect).filter(
        PageRedirect.organization_id == org_id,
        PageRedirect.source_page_id == page.id,
        PageRedirect.is_active == True,
    ).all()
    for redirect in existing_redirects:
        redirect.target_page_id = None
        redirect.target_url = None
        redirect.status_code = 307
        redirect.is_active = True

    _upsert_page_redirect(
        db,
        organization_id=org_id,
        source_page_id=page.id,
        source_slug=page.slug,
        target_page_id=None,
        target_url=None,
        status_code=307,
    )

    db.delete(page)
    db.commit()

    # Trash Google Doc in Drive
    drive_trashed = False
    drive_error = None
    if google_doc_id:
        try:
            creds = await _get_drive_credentials_compat(user, db, org_id)
            svc = build("drive", "v3", credentials=creds, cache_discovery=False)
            _trash_drive_item(svc, google_doc_id)
            drive_trashed = True
            logger.info("Trashed Drive doc %s for page %d", google_doc_id, page_id)
        except Exception as exc:
            drive_error = str(exc)
            logger.exception("Could not trash Drive doc %s for page %d: %s", google_doc_id, page_id, exc)

    return {
        "ok": True,
        "drive_trashed": drive_trashed,
        "drive_error": drive_error,
    }


@router.post("/{page_id}/sync")
async def sync_page(
    page_id: int,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Pull latest content from Google Drive for this page."""
    org_id = _require_editor(user, db, x_org_id)
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    creds = await _get_drive_credentials_compat(user, db, org_id, require_write=False)
    html, modified_at, drive_title = await _export_html(page.google_doc_id, creds)

    apply_sync_result(db, page, html, modified_at, drive_title, org_id)

    db.commit()
    db.refresh(page)
    logger.info("Synced page %d '%s'", page.id, page.title)
    return {"ok": True, "page": _page_dict(page, include_html=True)}


@router.post("/{page_id}/publish")
def publish_page(
    page_id: int,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Publish page — snapshot html_content into published_html."""
    org_id = _require_editor(user, db, x_org_id)
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    if page.section_id is None:
        raise HTTPException(
            status_code=400,
            detail="Assign this page to a section before publishing.",
        )

    if not page.html_content:
        raise HTTPException(status_code=400, detail="Page has no content. Sync first.")

    apply_publish(db, page)

    db.commit()
    db.refresh(page)
    logger.info("Published page %d '%s'", page.id, page.title)
    return {"ok": True, "page": _page_dict(page, include_html=True)}


@router.post("/{page_id}/unpublish")
def unpublish_page(
    page_id: int,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_id = _require_editor(user, db, x_org_id)
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    page.is_published = False
    page.status = "draft"
    db.commit()
    return {"ok": True}


@router.post("/{page_id}/submit-review")
def submit_page_for_review(
    page_id: int,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Submit page for approval review."""
    org_id = _require_editor(user, db, x_org_id)
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    if page.section_id is None:
        raise HTTPException(status_code=400, detail="Assign this page to a section before submitting for review.")
    if not page.html_content:
        raise HTTPException(status_code=400, detail="Page has no content. Sync first.")
    if page.status == "review":
        return {"ok": True, "page": _page_dict(page, include_html=True), "status": "already_in_review"}

    page.status = "review"
    # Record submission event so approval history and notifications can reflect
    # the review request lifecycle (not just approve/reject decisions).
    db.add(
        Approval(
            page_id=page.id,
            entity_type="page",
            user_id=user.id,
            action="submit",
            comment=None,
        )
    )
    db.commit()
    db.refresh(page)
    logger.info("Submitted page %d '%s' for review", page.id, page.title)
    return {"ok": True, "page": _page_dict(page, include_html=True), "status": "in_review"}


@router.post("/{page_id}/approve")
def approve_page(
    page_id: int,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Approve a page in review and publish current draft snapshot."""
    org_id = _require_reviewer(user, db, x_org_id)
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    if page.status != "review":
        raise HTTPException(status_code=409, detail=f"Page is not in review (status={page.status})")
    if not page.html_content:
        raise HTTPException(status_code=400, detail="Page has no content. Sync first.")

    apply_publish(db, page)

    db.add(Approval(page_id=page.id, entity_type="page", user_id=user.id, action="approve", comment=None))
    db.commit()
    db.refresh(page)
    logger.info("Approved page %d '%s'", page.id, page.title)
    return {"ok": True, "page": _page_dict(page, include_html=True)}


@router.post("/{page_id}/reject")
def reject_page(
    page_id: int,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Reject a page in review and return it to draft."""
    org_id = _require_reviewer(user, db, x_org_id)
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    if page.status != "review":
        raise HTTPException(status_code=409, detail=f"Page is not in review (status={page.status})")

    page.status = "draft"
    db.add(
        Approval(
            page_id=page.id,
            entity_type="page",
            user_id=user.id,
            action="reject",
            comment=None,
        )
    )
    db.commit()
    db.refresh(page)
    logger.info("Rejected page %d '%s'", page.id, page.title)
    return {"ok": True, "page": _page_dict(page, include_html=True)}
