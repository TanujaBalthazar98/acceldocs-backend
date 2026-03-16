"""Pages API — CRUD, Drive sync, and publish for individual documentation pages."""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Header
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.drive import _create_drive_doc, _trash_drive_item, _move_drive_item, get_drive_credentials as _get_drive_creds_drive
from app.auth.routes import get_current_user
from app.database import get_db
from app.lib.slugify import to_slug as slugify
from app.models import Organization, OrgRole, Page, PageRedirect, Section, User

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
    }
    if include_html:
        d["html_content"] = p.html_content
        d["published_html"] = p.published_html
    return d


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------

async def _get_drive_credentials(user: User, db: Session, org_id: int) -> Credentials:
    """Shared credential lookup with workspace fallback logic."""
    return await _get_drive_creds_drive(user, org_id, db)


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


def _unique_slug(base_value: str, org_id: int, db: Session, exclude_id: int | None = None, *, strip_numeric_prefix: bool = True) -> str:
    seed = _NUM_PREFIX.sub("", base_value).strip() if strip_numeric_prefix else base_value.strip()
    base = slugify(seed or base_value) or "page"
    slug, n = base, 1
    while True:
        q = db.query(Page).filter(Page.organization_id == org_id, Page.slug == slug)
        if exclude_id:
            q = q.filter(Page.id != exclude_id)
        if not q.first():
            return slug
        slug, n = f"{base}-{n}", n + 1


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

@router.get("")
def list_pages(
    section_id: int | None = None,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """List all pages for the current org, optionally filtered by section."""
    org_id = _get_org_id(user, db, x_org_id)
    q = db.query(Page).filter(Page.organization_id == org_id)
    if section_id is not None:
        q = q.filter(Page.section_id == section_id)
    pages = q.order_by(Page.section_id.nulls_last(), Page.display_order, Page.title).all()
    return {"pages": [_page_dict(p) for p in pages]}


@router.post("", status_code=201)
async def create_page(
    body: PageCreate,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Create a page from a Google Doc ID. Fetches title + HTML immediately."""
    org_id = _require_editor(user, db, x_org_id)

    creds = await _get_drive_credentials(user, db, org_id)

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
    slug = _unique_slug(title, org_id, db)

    page = Page(
        organization_id=org_id,
        section_id=body.section_id,
        google_doc_id=google_doc_id,
        title=title,
        slug=slug,
        slug_locked=False,
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
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_id = _get_org_id(user, db, x_org_id)
    page = db.query(Page).filter(Page.id == page_id, Page.organization_id == org_id).first()
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
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
        creds = await _get_drive_credentials(user, db, org_id)
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
                    creds = await _get_drive_credentials(user, db, org_id)
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

    creds = await _get_drive_credentials(user, db, org_id)
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

    new_title = _NUM_PREFIX.sub("", (drive_title or copy_title)).strip() or copy_title
    new_slug = _unique_slug(new_title, org_id, db)

    insert_order = (source.display_order or 0) + 1
    following_pages = (
        db.query(Page)
        .filter(
            Page.organization_id == org_id,
            Page.section_id == source.section_id,
            Page.display_order >= insert_order,
            Page.id != source.id,
        )
        .order_by(Page.display_order.desc(), Page.id.desc())
        .all()
    )
    for page in following_pages:
        page.display_order += 1

    duplicate = Page(
        organization_id=org_id,
        section_id=source.section_id,
        google_doc_id=copied_doc_id,
        title=new_title,
        slug=new_slug,
        slug_locked=False,
        html_content=html,
        published_html=None,
        is_published=False,
        status="draft",
        display_order=insert_order,
        drive_modified_at=modified_at,
        last_synced_at=datetime.now(timezone.utc).isoformat(),
        owner_id=user.id,
    )
    db.add(duplicate)
    db.commit()
    db.refresh(duplicate)
    logger.info("Duplicated page %d -> %d (Drive doc %s)", source.id, duplicate.id, copied_doc_id)
    return _page_dict(duplicate, include_html=True)


@router.delete("/{page_id}", status_code=204)
async def delete_page(
    page_id: int,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
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
    if google_doc_id:
        try:
            creds = await _get_drive_credentials(user, db, org_id)
            svc = build("drive", "v3", credentials=creds, cache_discovery=False)
            _trash_drive_item(svc, google_doc_id)
            logger.info("Trashed Drive doc %s for page %d", google_doc_id, page_id)
        except Exception as exc:
            logger.warning("Could not trash Drive doc %s: %s", google_doc_id, exc)


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

    creds = await _get_drive_credentials(user, db, org_id)
    html, modified_at, drive_title = await _export_html(page.google_doc_id, creds)

    page.html_content = html
    page.drive_modified_at = modified_at
    page.last_synced_at = datetime.now(timezone.utc).isoformat()
    # Update title from Drive if it hasn't been manually overridden
    if drive_title:
        clean = _NUM_PREFIX.sub("", drive_title).strip()
        if clean:
            page.title = clean
            if not page.slug_locked and not page.is_published:
                page.slug = _unique_slug(clean, org_id, db, exclude_id=page_id)

    # If page was published and content has changed, flag it as having unpublished changes.
    # The old published_html remains live until the user re-publishes.
    if page.is_published and html != page.published_html:
        page.status = "draft"

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

    page.published_html = page.html_content
    page.is_published = True
    page.status = "published"

    # Auto-publish parent section so it appears in the public docs sidebar
    if page.section_id:
        section = db.get(Section, page.section_id)
        if section and not section.is_published:
            section.is_published = True

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
