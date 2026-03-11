"""Drive API — scan a Google Drive folder and import it as sections + pages.

POST /api/drive/scan   — recursively walk a folder, create Section + Page records
POST /api/drive/sync   — re-sync all pages for the current org from Drive
GET  /api/drive/status — check Drive connection for current user
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.config import settings
from app.database import get_db
from app.lib.slugify import to_slug as slugify
from app.models import GoogleToken, OrgRole, Organization, Page, Section, User
from app.services.encryption import get_encryption_service

logger = logging.getLogger(__name__)
router = APIRouter()

DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_TOKEN_REFRESH_URL = "https://oauth2.googleapis.com/token"
MAX_FOLDER_DEPTH = 5

_NUM_PREFIX = re.compile(r"^\d+[\.\s]+")


# ---------------------------------------------------------------------------
# Shared Drive credential helper
# ---------------------------------------------------------------------------

async def get_drive_credentials(user: User, org_id: int, db: Session) -> Credentials:
    """Return fresh Google Credentials for user by refreshing stored token."""
    google_token = db.query(GoogleToken).filter(
        GoogleToken.user_id == user.id,
        GoogleToken.organization_id == org_id,
    ).first()
    if not google_token:
        raise HTTPException(
            status_code=401,
            detail="No Google Drive credentials found. Please reconnect your account.",
        )

    enc = get_encryption_service()
    try:
        refresh_token = enc.decrypt(google_token.encrypted_refresh_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Failed to read stored Drive credentials")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(GOOGLE_TOKEN_REFRESH_URL, data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })

    if resp.status_code != 200:
        raise HTTPException(
            status_code=401,
            detail="Google token refresh failed. Please reconnect your Drive account.",
        )

    access_token = resp.json().get("access_token")
    google_token.last_refreshed_at = datetime.now(timezone.utc)
    db.commit()
    return Credentials(token=access_token)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ScanRequest(BaseModel):
    folder_id: str
    parent_section_id: int | None = None


# ---------------------------------------------------------------------------
# Internal scan logic
# ---------------------------------------------------------------------------

def _clean_name(raw: str) -> str:
    return _NUM_PREFIX.sub("", raw).strip() or raw


def _unique_section_slug(name: str, org_id: int, parent_id: int | None, db: Session) -> str:
    base = slugify(_clean_name(name))
    slug, n = base, 1
    while True:
        q = db.query(Section).filter(
            Section.organization_id == org_id,
            Section.parent_id == parent_id,
            Section.slug == slug,
        )
        if not q.first():
            return slug
        slug, n = f"{base}-{n}", n + 1


def _unique_page_slug(title: str, org_id: int, db: Session, exclude_id: int | None = None) -> str:
    base = slugify(_clean_name(title))
    slug, n = base, 1
    while True:
        q = db.query(Page).filter(Page.organization_id == org_id, Page.slug == slug)
        if exclude_id:
            q = q.filter(Page.id != exclude_id)
        if not q.first():
            return slug
        slug, n = f"{base}-{n}", n + 1


def _list_folder_items(service, folder_id: str) -> list[dict]:
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name,mimeType,modifiedTime)",
        pageSize=200,
        orderBy="folder,name",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return results.get("files", [])


def _scan_folder(
    service,
    folder_id: str,
    parent_section_id: int | None,
    org_id: int,
    user_id: int,
    db: Session,
    depth: int = 0,
) -> dict[str, int]:
    """Recursively scan a Drive folder, creating Sections and Pages."""
    counts: dict[str, int] = {"sections": 0, "pages": 0, "updated": 0}
    if depth > MAX_FOLDER_DEPTH:
        return counts

    items = _list_folder_items(service, folder_id)
    page_order = 0

    for i, item in enumerate(items):
        mime = item["mimeType"]
        name = item["name"]
        item_id = item["id"]
        modified = item.get("modifiedTime")

        if mime == DRIVE_FOLDER_MIME:
            existing_section = db.query(Section).filter(
                Section.organization_id == org_id,
                Section.drive_folder_id == item_id,
            ).first()

            if existing_section:
                existing_section.display_order = i
                existing_section.is_published = True
                section = existing_section
            else:
                slug = _unique_section_slug(name, org_id, parent_section_id, db)
                section = Section(
                    organization_id=org_id,
                    parent_id=parent_section_id,
                    name=_clean_name(name),
                    slug=slug,
                    drive_folder_id=item_id,
                    display_order=i,
                    is_published=True,
                )
                db.add(section)
                db.flush()
                counts["sections"] += 1

            sub = _scan_folder(service, item_id, section.id, org_id, user_id, db, depth + 1)
            counts["sections"] += sub["sections"]
            counts["pages"] += sub["pages"]
            counts["updated"] += sub["updated"]

        elif mime == GOOGLE_DOC_MIME:
            title = _clean_name(name)
            existing_page = db.query(Page).filter(
                Page.organization_id == org_id,
                Page.google_doc_id == item_id,
            ).first()

            if existing_page:
                if existing_page.drive_modified_at != modified:
                    existing_page.drive_modified_at = modified
                    counts["updated"] += 1
                existing_page.display_order = page_order
                if parent_section_id is not None:
                    existing_page.section_id = parent_section_id
            else:
                slug = _unique_page_slug(title, org_id, db)
                page = Page(
                    organization_id=org_id,
                    section_id=parent_section_id,
                    google_doc_id=item_id,
                    title=title,
                    slug=slug,
                    drive_modified_at=modified,
                    display_order=page_order,
                    owner_id=user_id,
                )
                db.add(page)
                counts["pages"] += 1

            page_order += 1

    db.commit()
    return counts


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/status")
def drive_status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
    if not org_role:
        return {"connected": False}

    org = db.get(Organization, org_role.organization_id)
    token = db.query(GoogleToken).filter(
        GoogleToken.user_id == user.id,
        GoogleToken.organization_id == org_role.organization_id,
    ).first()
    return {
        "connected": token is not None,
        "drive_folder_id": org.drive_folder_id if org else None,
        "last_refreshed_at": (
            token.last_refreshed_at.isoformat() if token and token.last_refreshed_at else None
        ),
    }


@router.post("/scan")
async def scan_folder(
    body: ScanRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Scan a Google Drive folder and create/update Sections and Pages.

    HTML content is NOT fetched here — call /sync after scan to pull content.
    """
    org_role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
    if not org_role or org_role.role not in ("owner", "admin", "editor"):
        raise HTTPException(status_code=403, detail="Editor role required")

    org_id = org_role.organization_id
    creds = await get_drive_credentials(user, org_id, db)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    try:
        folder_meta = service.files().get(
            fileId=body.folder_id,
            fields="id,name,mimeType",
            supportsAllDrives=True,
        ).execute()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not access folder: {e}")

    if folder_meta.get("mimeType") != DRIVE_FOLDER_MIME:
        raise HTTPException(status_code=400, detail="Provided ID is not a Drive folder")

    # Store root folder on org (first-time setup)
    org = db.get(Organization, org_id)
    if org and not org.drive_folder_id and body.parent_section_id is None:
        org.drive_folder_id = body.folder_id
        db.commit()

    # Create (or find) the scanned folder itself as a Section
    root_folder_name = folder_meta.get("name", "Untitled")
    parent_section_id = body.parent_section_id

    existing_root_section = db.query(Section).filter(
        Section.organization_id == org_id,
        Section.drive_folder_id == body.folder_id,
    ).first()

    if existing_root_section:
        existing_root_section.is_published = True
        root_section = existing_root_section
    else:
        root_slug = _unique_section_slug(root_folder_name, org_id, parent_section_id, db)
        root_section = Section(
            organization_id=org_id,
            parent_id=parent_section_id,
            name=_clean_name(root_folder_name),
            slug=root_slug,
            drive_folder_id=body.folder_id,
            display_order=0,
            is_published=True,
        )
        db.add(root_section)
        db.flush()

    counts = _scan_folder(
        service=service,
        folder_id=body.folder_id,
        parent_section_id=root_section.id,
        org_id=org_id,
        user_id=user.id,
        db=db,
    )
    # Root section itself counts as 1 created (if new)
    if not existing_root_section:
        counts["sections"] += 1

    logger.info(
        "Scan complete org=%d: +%d sections, +%d pages, ~%d updated",
        org_id, counts["sections"], counts["pages"], counts["updated"],
    )
    return {
        "ok": True,
        "folder_name": folder_meta.get("name"),
        "sections_created": counts["sections"],
        "pages_created": counts["pages"],
        "pages_updated": counts["updated"],
    }


@router.post("/sync")
async def sync_all_pages(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Fetch latest HTML from Google Drive for all pages in the org.

    Skips pages whose drive_modified_at matches the stored value (already up to date).
    """
    org_role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
    if not org_role or org_role.role not in ("owner", "admin", "editor"):
        raise HTTPException(status_code=403, detail="Editor role required")

    org_id = org_role.organization_id
    creds = await get_drive_credentials(user, org_id, db)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    pages = db.query(Page).filter(Page.organization_id == org_id).all()
    synced, skipped, errors = 0, 0, 0

    for page in pages:
        try:
            meta = service.files().get(
                fileId=page.google_doc_id,
                fields="modifiedTime",
                supportsAllDrives=True,
            ).execute()
            drive_mod = meta.get("modifiedTime")
        except Exception as e:
            logger.warning("Metadata fetch failed page %d (%s): %s", page.id, page.google_doc_id, e)
            errors += 1
            continue

        if drive_mod == page.drive_modified_at and page.html_content:
            skipped += 1
            continue

        try:
            raw = service.files().export(
                fileId=page.google_doc_id, mimeType="text/html"
            ).execute()
            html = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        except Exception as e:
            logger.warning("Export failed page %d (%s): %s", page.id, page.google_doc_id, e)
            errors += 1
            continue

        page.html_content = html
        page.drive_modified_at = drive_mod
        page.last_synced_at = datetime.now(timezone.utc).isoformat()
        synced += 1

    db.commit()
    logger.info("Sync complete org=%d: %d synced, %d skipped, %d errors", org_id, synced, skipped, errors)
    return {"ok": True, "synced": synced, "skipped": skipped, "errors": errors}
