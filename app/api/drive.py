"""Drive API — scan a Google Drive folder and import it as sections + pages.

POST /api/drive/scan   — recursively walk a folder, create Section + Page records
POST /api/drive/import/local — upload local files/folders into a target section
POST /api/drive/sync   — re-sync all pages for the current org from Drive
GET  /api/drive/status — check Drive connection for current user
"""

import logging
import json
import io
from pathlib import PurePosixPath
import re
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, File, Form, Header, UploadFile
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
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
MAX_LOCAL_FILE_BYTES = 20 * 1024 * 1024
ALLOWED_LOCAL_EXTENSIONS: dict[str, str] = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pdf": "application/pdf",
    ".rtf": "application/rtf",
}

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


def _resolve_org_role(user: User, db: Session, requested_org_id: int | None = None) -> OrgRole:
    query = db.query(OrgRole).filter(OrgRole.user_id == user.id)
    if requested_org_id is not None:
        explicit = query.filter(OrgRole.organization_id == requested_org_id).first()
        if explicit:
            return explicit
    role = query.first()
    if not role:
        raise HTTPException(status_code=403, detail="User has no organization")
    return role


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ScanRequest(BaseModel):
    folder_id: str
    parent_section_id: int | None = None
    target_type: Literal["product", "version", "tab", "section"] | None = None


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


def _infer_target_type(section: Section) -> Literal["product", "version", "tab", "section"]:
    if section.parent_id is None:
        return "product"
    if (section.section_type or "section") == "version":
        return "version"
    if (section.section_type or "section") == "tab":
        return "tab"
    return "section"


def _resolve_target_section(
    *,
    org_id: int,
    db: Session,
    section_id: int | None,
    expected_type: Literal["product", "version", "tab", "section"] | None,
) -> Section | None:
    if section_id is None:
        return None

    section = db.query(Section).filter(
        Section.id == section_id,
        Section.organization_id == org_id,
    ).first()
    if not section:
        raise HTTPException(status_code=404, detail="Target section not found")

    actual_type = _infer_target_type(section)
    if expected_type and expected_type != actual_type:
        raise HTTPException(
            status_code=400,
            detail=f"Target type mismatch: expected '{expected_type}', got '{actual_type}'",
        )
    return section


def _ensure_section_drive_folder(
    *,
    service,
    org: Organization | None,
    section: Section,
    db: Session,
) -> str:
    if section.drive_folder_id:
        return section.drive_folder_id

    parent_drive_id: str | None = None
    if section.parent_id:
        parent_section = db.get(Section, section.parent_id)
        parent_drive_id = parent_section.drive_folder_id if parent_section else None
    if not parent_drive_id and org:
        parent_drive_id = org.drive_folder_id
    if not parent_drive_id:
        raise HTTPException(
            status_code=400,
            detail="Target section has no Drive folder and workspace root is not configured",
        )

    folder_id = _create_drive_folder(service, section.name, parent_drive_id)
    section.drive_folder_id = folder_id
    db.commit()
    return folder_id


def _normalize_relative_path(path: str) -> str:
    candidate = (path or "").replace("\\", "/").strip().strip("/")
    if not candidate:
        raise HTTPException(status_code=400, detail="Folder import includes an empty file path")
    parts = [segment.strip() for segment in candidate.split("/") if segment.strip() and segment != "."]
    if not parts or any(segment == ".." for segment in parts):
        raise HTTPException(status_code=400, detail=f"Invalid folder path '{path}'")
    normalized = "/".join(parts)
    if normalized.startswith("../") or "/../" in normalized:
        raise HTTPException(status_code=400, detail=f"Invalid folder path '{path}'")
    return normalized


def _drive_import_mime_for_filename(filename: str) -> str:
    ext = PurePosixPath(filename or "").suffix.lower()
    mime = ALLOWED_LOCAL_EXTENSIONS.get(ext)
    if not mime:
        allowed = ", ".join(sorted(ALLOWED_LOCAL_EXTENSIONS.keys()))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext or 'unknown'}'. Allowed: {allowed}",
        )
    return mime


async def _upload_local_as_google_doc(
    *,
    service,
    upload: UploadFile,
    parent_drive_folder_id: str,
    destination_name: str | None = None,
) -> None:
    filename = (destination_name or upload.filename or "Untitled").strip() or "Untitled"
    mime = _drive_import_mime_for_filename(filename)
    content = await upload.read()
    if not content:
        raise HTTPException(status_code=400, detail=f"File '{filename}' is empty")
    if len(content) > MAX_LOCAL_FILE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File '{filename}' exceeds {MAX_LOCAL_FILE_BYTES // (1024 * 1024)}MB limit",
        )

    title = _clean_name(PurePosixPath(filename).stem) or "Untitled"
    media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime, resumable=False)
    service.files().create(
        body={
            "name": title,
            "mimeType": GOOGLE_DOC_MIME,
            "parents": [parent_drive_folder_id],
        },
        media_body=media,
        fields="id,name,mimeType",
        supportsAllDrives=True,
    ).execute()

def _trash_drive_item(service, file_id: str) -> None:
    """Move a Drive file/folder to trash."""
    service.files().update(
        fileId=file_id,
        body={"trashed": True},
        supportsAllDrives=True,
    ).execute()


def _move_drive_item(service, file_id: str, new_parent_id: str) -> None:
    """Move a Drive file/folder to a new parent folder."""
    meta = service.files().get(
        fileId=file_id,
        fields="parents",
        supportsAllDrives=True,
    ).execute()
    old_parents = ",".join(meta.get("parents", []))
    service.files().update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=old_parents,
        fields="id,parents",
        supportsAllDrives=True,
    ).execute()


def _create_drive_folder(service, name: str, parent_id: str | None) -> str:
    """Create a Drive folder and return its ID."""
    metadata: dict = {"name": name, "mimeType": DRIVE_FOLDER_MIME}
    if parent_id:
        metadata["parents"] = [parent_id]
    f = service.files().create(body=metadata, fields="id", supportsAllDrives=True).execute()
    return f["id"]


def _create_drive_doc(service, title: str, parent_id: str | None) -> str:
    """Create a blank Google Doc and return its ID."""
    metadata: dict = {"name": title, "mimeType": GOOGLE_DOC_MIME}
    if parent_id:
        metadata["parents"] = [parent_id]
    f = service.files().create(body=metadata, fields="id", supportsAllDrives=True).execute()
    return f["id"]


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
                    section_type="section",
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
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    org_role = _resolve_org_role(user, db, x_org_id)

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
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Scan a Google Drive folder and create/update Sections and Pages.

    HTML content is NOT fetched here — call /sync after scan to pull content.
    """
    org_role = _resolve_org_role(user, db, x_org_id)
    if not org_role or org_role.role not in ("owner", "admin", "editor"):
        raise HTTPException(status_code=403, detail="Editor role required")

    org_id = org_role.organization_id
    org = db.get(Organization, org_id)
    target_section = _resolve_target_section(
        org_id=org_id,
        db=db,
        section_id=body.parent_section_id,
        expected_type=body.target_type,
    )

    # Root folder is workspace-level configuration and must stay owner/admin only.
    if body.parent_section_id is None and (not org or not org.drive_folder_id):
        if org_role.role not in ("owner", "admin"):
            raise HTTPException(
                status_code=403,
                detail="Only workspace owner/admin can configure Drive root folder",
            )

    if target_section and org and org.drive_folder_id and body.folder_id == org.drive_folder_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot import the connected root into a nested destination.",
        )

    if body.parent_section_id is None and org and org.drive_folder_id and body.folder_id != org.drive_folder_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Import-from-drive only supports the connected root folder. "
                "Use the additional import flow for other folders."
            ),
        )

    creds = await get_drive_credentials(user, org_id, db)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    try:
        folder_meta = service.files().get(
            fileId=body.folder_id,
            fields="id,name,mimeType,parents",
            supportsAllDrives=True,
        ).execute()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not access folder: {e}")

    if folder_meta.get("mimeType") != DRIVE_FOLDER_MIME:
        raise HTTPException(status_code=400, detail="Provided ID is not a Drive folder")

    # Store root folder on org (first-time setup)
    if org and not org.drive_folder_id and body.parent_section_id is None:
        org.drive_folder_id = body.folder_id
        db.commit()

    if target_section is not None:
        target_drive_parent = _ensure_section_drive_folder(service=service, org=org, section=target_section, db=db)
        current_parents = folder_meta.get("parents") or []
        if body.folder_id != target_drive_parent and target_drive_parent not in current_parents:
            _move_drive_item(service, body.folder_id, target_drive_parent)
            logger.info(
                "Moved imported Drive folder %s under section %d (%s)",
                body.folder_id,
                target_section.id,
                target_drive_parent,
            )

    scan_parent_section_id = body.parent_section_id
    if target_section is not None:
        if body.folder_id == target_section.drive_folder_id:
            scan_parent_section_id = target_section.id
        else:
            folder_name = _clean_name(folder_meta.get("name", "Imported section"))
            imported_root_section = db.query(Section).filter(
                Section.organization_id == org_id,
                Section.drive_folder_id == body.folder_id,
            ).first()
            if imported_root_section:
                imported_root_section.parent_id = target_section.id
                imported_root_section.name = folder_name
                imported_root_section.display_order = 0
                imported_root_section.is_published = True
            else:
                imported_root_section = Section(
                    organization_id=org_id,
                    parent_id=target_section.id,
                    name=folder_name,
                    slug=_unique_section_slug(folder_name, org_id, target_section.id, db),
                    section_type="section",
                    drive_folder_id=body.folder_id,
                    display_order=0,
                    is_published=True,
                )
                db.add(imported_root_section)
                db.flush()
            db.commit()
            scan_parent_section_id = imported_root_section.id

    # Root onboarding scans treat the selected folder as an invisible workspace container.
    # Targeted scans first map the selected folder as a child section under the target.
    counts = _scan_folder(
        service=service,
        folder_id=body.folder_id,
        parent_section_id=scan_parent_section_id,
        org_id=org_id,
        user_id=user.id,
        db=db,
    )

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


@router.post("/import/local")
async def import_local(
    target_section_id: int = Form(...),
    target_type: Literal["product", "version", "tab", "section"] | None = Form(default=None),
    mode: Literal["files", "folder"] = Form(default="files"),
    relative_paths_json: str | None = Form(default=None),
    files: list[UploadFile] = File(...),
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Import local files/folders into a target section and sync DB from Drive."""
    org_role = _resolve_org_role(user, db, x_org_id)
    if not org_role or org_role.role not in ("owner", "admin", "editor"):
        raise HTTPException(status_code=403, detail="Editor role required")
    org_id = org_role.organization_id
    org = db.get(Organization, org_id)
    if not org or not org.drive_folder_id:
        raise HTTPException(status_code=400, detail="Connect Drive and configure a root folder first")

    target_section = _resolve_target_section(
        org_id=org_id,
        db=db,
        section_id=target_section_id,
        expected_type=target_type,
    )
    if target_section is None:
        raise HTTPException(status_code=400, detail="Target section is required")

    effective_target_type = _infer_target_type(target_section)
    if mode == "files" and effective_target_type != "section":
        raise HTTPException(
            status_code=400,
            detail="File import is supported only for section targets. Choose a section destination.",
        )

    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    creds = await get_drive_credentials(user, org_id, db)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    target_drive_folder_id = _ensure_section_drive_folder(service=service, org=org, section=target_section, db=db)

    relative_paths: list[str] = []
    if relative_paths_json:
        try:
            payload = json.loads(relative_paths_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid relative_paths_json payload") from exc
        if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
            raise HTTPException(status_code=400, detail="relative_paths_json must be a JSON string array")
        relative_paths = payload
        if len(relative_paths) != len(files):
            raise HTTPException(status_code=400, detail="relative_paths_json size must match uploaded files")

    folder_cache: dict[str, str] = {"": target_drive_folder_id}
    uploaded = 0

    for idx, upload in enumerate(files):
        raw_relative = (
            relative_paths[idx]
            if idx < len(relative_paths)
            else (upload.filename or "")
        )
        normalized_relative = _normalize_relative_path(raw_relative)
        relative_parts = normalized_relative.split("/")
        file_name = relative_parts[-1]
        folder_parts = relative_parts[:-1] if mode == "folder" else []

        parent_path = ""
        parent_drive = target_drive_folder_id
        for folder_name in folder_parts:
            parent_path = f"{parent_path}/{folder_name}" if parent_path else folder_name
            cached = folder_cache.get(parent_path)
            if cached:
                parent_drive = cached
                continue
            created_folder = _create_drive_folder(service, _clean_name(folder_name), parent_drive)
            folder_cache[parent_path] = created_folder
            parent_drive = created_folder

        await _upload_local_as_google_doc(
            service=service,
            upload=upload,
            parent_drive_folder_id=parent_drive,
            destination_name=file_name,
        )
        uploaded += 1

    counts = _scan_folder(
        service=service,
        folder_id=target_drive_folder_id,
        parent_section_id=target_section.id,
        org_id=org_id,
        user_id=user.id,
        db=db,
    )

    logger.info(
        "Local import complete org=%d target=%d mode=%s: %d files uploaded",
        org_id,
        target_section.id,
        mode,
        uploaded,
    )
    return {
        "ok": True,
        "target_section_id": target_section.id,
        "target_type": effective_target_type,
        "mode": mode,
        "uploaded_files": uploaded,
        "sections_created": counts["sections"],
        "pages_created": counts["pages"],
        "pages_updated": counts["updated"],
    }


@router.post("/sync")
async def sync_all_pages(
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Fetch latest HTML from Google Drive for all pages in the org.

    Skips pages whose drive_modified_at matches the stored value (already up to date).
    """
    org_role = _resolve_org_role(user, db, x_org_id)
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

        if page.is_published and html != page.published_html:
            page.status = "draft"
        page.html_content = html
        page.drive_modified_at = drive_mod
        page.last_synced_at = datetime.now(timezone.utc).isoformat()
        synced += 1

    db.commit()
    logger.info("Sync complete org=%d: %d synced, %d skipped, %d errors", org_id, synced, skipped, errors)
    return {"ok": True, "synced": synced, "skipped": skipped, "errors": errors}
