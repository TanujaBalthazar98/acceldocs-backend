"""Pages API — CRUD, Drive sync, and publish for individual documentation pages."""

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Header
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.config import settings
from app.database import get_db
from app.lib.slugify import to_slug as slugify
from app.models import GoogleToken, OrgRole, Page, Section, User
from app.services.encryption import get_encryption_service

logger = logging.getLogger(__name__)
router = APIRouter()

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_TOKEN_REFRESH_URL = "https://oauth2.googleapis.com/token"

# Strip leading numeric sort-prefixes from Drive doc names ("01 ", "2. ")
_NUM_PREFIX = re.compile(r"^\d+[\.\s]+")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class PageCreate(BaseModel):
    google_doc_id: str
    section_id: int | None = None
    title: str | None = None
    display_order: int = 0


class PageUpdate(BaseModel):
    section_id: int | None = None
    title: str | None = None
    display_order: int | None = None


def _page_dict(p: Page, include_html: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": p.id,
        "organization_id": p.organization_id,
        "section_id": p.section_id,
        "google_doc_id": p.google_doc_id,
        "title": p.title,
        "slug": p.slug,
        "is_published": p.is_published,
        "status": p.status,
        "display_order": p.display_order,
        "drive_modified_at": p.drive_modified_at,
        "last_synced_at": p.last_synced_at,
        "owner_id": p.owner_id,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }
    if include_html:
        d["html_content"] = p.html_content
        d["published_html"] = p.published_html
    return d


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------

async def _get_drive_credentials(user: User, db: Session) -> Credentials:
    """Return valid Google Credentials for user, refreshing stored token if needed."""
    org_role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
    if not org_role:
        raise HTTPException(status_code=403, detail="User has no organization")

    google_token = db.query(GoogleToken).filter(
        GoogleToken.user_id == user.id,
        GoogleToken.organization_id == org_role.organization_id,
    ).first()
    if not google_token:
        raise HTTPException(status_code=401, detail="No Google Drive credentials. Please reconnect.")

    enc = get_encryption_service()
    try:
        refresh_token = enc.decrypt(google_token.encrypted_refresh_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Failed to decrypt Drive credentials")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(GOOGLE_TOKEN_REFRESH_URL, data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Failed to refresh Google token. Please reconnect.")

    access_token = resp.json().get("access_token")
    google_token.last_refreshed_at = datetime.now(timezone.utc)
    db.commit()
    return Credentials(token=access_token)


async def _export_html(google_doc_id: str, creds: Credentials) -> tuple[str, str | None]:
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


# ---------------------------------------------------------------------------
# Org / auth helpers
# ---------------------------------------------------------------------------

def _get_org_id(user: User, db: Session) -> int:
    role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
    if not role:
        raise HTTPException(status_code=403, detail="User has no organization")
    return role.organization_id


def _require_editor(user: User, db: Session) -> int:
    role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
    if not role or role.role not in ("owner", "admin", "editor"):
        raise HTTPException(status_code=403, detail="Editor role required")
    return role.organization_id


def _unique_slug(title: str, org_id: int, db: Session, exclude_id: int | None = None) -> str:
    base = slugify(_NUM_PREFIX.sub("", title).strip() or title)
    slug, n = base, 1
    while True:
        q = db.query(Page).filter(Page.organization_id == org_id, Page.slug == slug)
        if exclude_id:
            q = q.filter(Page.id != exclude_id)
        if not q.first():
            return slug
        slug, n = f"{base}-{n}", n + 1


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("")
def list_pages(
    section_id: int | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """List all pages for the current org, optionally filtered by section."""
    org_id = _get_org_id(user, db)
    q = db.query(Page).filter(Page.organization_id == org_id)
    if section_id is not None:
        q = q.filter(Page.section_id == section_id)
    pages = q.order_by(Page.section_id.nulls_last(), Page.display_order, Page.title).all()
    return {"pages": [_page_dict(p) for p in pages]}


@router.post("", status_code=201)
async def create_page(
    body: PageCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Create a page from a Google Doc ID. Fetches title + HTML immediately."""
    org_id = _require_editor(user, db)

    # Check duplicate
    existing = db.query(Page).filter(
        Page.organization_id == org_id,
        Page.google_doc_id == body.google_doc_id,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Page with this Google Doc ID already exists")

    # Fetch from Drive to get real title and initial HTML
    creds = await _get_drive_credentials(user, db)
    html, modified_at, drive_title = await _export_html(body.google_doc_id, creds)

    title = body.title or drive_title or body.google_doc_id
    title = _NUM_PREFIX.sub("", title).strip() or title
    slug = _unique_slug(title, org_id, db)

    page = Page(
        organization_id=org_id,
        section_id=body.section_id,
        google_doc_id=body.google_doc_id,
        title=title,
        slug=slug,
        html_content=html,
        display_order=body.display_order,
        drive_modified_at=modified_at,
        last_synced_at=datetime.now(timezone.utc).isoformat(),
        owner_id=user.id,
    )
    db.add(page)
    db.commit()
    db.refresh(page)
    logger.info("Created page %d '%s' for org %d", page.id, page.title, org_id)
    return _page_dict(page, include_html=True)


@router.get("/{page_id}")
def get_page(
    page_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_id = _get_org_id(user, db)
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    return _page_dict(page, include_html=True)


@router.patch("/{page_id}")
def update_page(
    page_id: int,
    body: PageUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_id = _require_editor(user, db)
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    if body.section_id is not None:
        page.section_id = body.section_id
    if body.title is not None:
        page.title = body.title.strip()
        page.slug = _unique_slug(page.title, org_id, db, exclude_id=page_id)
    if body.display_order is not None:
        page.display_order = body.display_order

    db.commit()
    db.refresh(page)
    return _page_dict(page)


@router.delete("/{page_id}", status_code=204)
def delete_page(
    page_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    org_id = _require_editor(user, db)
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    db.delete(page)
    db.commit()


@router.post("/{page_id}/sync")
async def sync_page(
    page_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Pull latest content from Google Drive for this page."""
    org_id = _require_editor(user, db)
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    creds = await _get_drive_credentials(user, db)
    html, modified_at, drive_title = await _export_html(page.google_doc_id, creds)

    page.html_content = html
    page.drive_modified_at = modified_at
    page.last_synced_at = datetime.now(timezone.utc).isoformat()
    # Update title from Drive if it hasn't been manually overridden
    if drive_title:
        clean = _NUM_PREFIX.sub("", drive_title).strip()
        if clean:
            page.title = clean
            page.slug = _unique_slug(clean, org_id, db, exclude_id=page_id)

    db.commit()
    db.refresh(page)
    logger.info("Synced page %d '%s'", page.id, page.title)
    return {"ok": True, "page": _page_dict(page, include_html=True)}


@router.post("/{page_id}/publish")
def publish_page(
    page_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Publish page — snapshot html_content into published_html."""
    org_id = _require_editor(user, db)
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    if not page.html_content:
        raise HTTPException(status_code=400, detail="Page has no content. Sync first.")

    page.published_html = page.html_content
    page.is_published = True
    page.status = "published"

    db.commit()
    db.refresh(page)
    logger.info("Published page %d '%s'", page.id, page.title)
    return {"ok": True, "page": _page_dict(page, include_html=True)}


@router.post("/{page_id}/unpublish")
def unpublish_page(
    page_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_id = _require_editor(user, db)
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")

    page.is_published = False
    page.status = "draft"
    db.commit()
    return {"ok": True}
