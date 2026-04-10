"""Migration API — import documentation from external sources into AccelDocs.

POST /api/migration/discover — Discover hierarchy from a source URL
POST /api/migration/preview — Preview what will be imported
POST /api/migration/start — Start migration
GET  /api/migration/status — Get migration status
POST /api/migration/cancel — Cancel running migration
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import requests
from fastapi import APIRouter, Depends, HTTPException, Header, status
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.config import settings
from app.database import get_db
from app.models import Migration, MigrationPage, Section, User
from app.services.migration import (
    MigrationServiceError,
    initialize_migration_sections_and_pages,
    process_migration_page_task,
    resolve_all_migration_links_task,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["migration"])

# Use /tmp for state files (writable on Vercel, Heroku, etc.)
# Falls back to local directory if not in serverless environment
_migration_state_dir = Path("/tmp/migration_states")
try:
    _migration_state_dir.mkdir(exist_ok=True)
except OSError:
    # Fallback to local directory for local development
    _migration_state_dir = Path(__file__).parent.parent.parent / "migration_states"
    _migration_state_dir.mkdir(exist_ok=True)

MIGRATION_STATE_DIR = _migration_state_dir


class DiscoverRequest(BaseModel):
    source_url: str
    product: str = "auto"
    use_playwright: bool = False


class DiscoverResponse(BaseModel):
    source_url: str
    source_type: str
    products: list[dict]
    total_pages: int
    hierarchy: list[dict]


class StartRequest(BaseModel):
    source_url: str
    product: str
    backend_url: str
    api_token: str
    org_id: int
    product_id: int
    use_playwright: bool = False
    create_drive_docs: bool = False
    state_file: str | None = None
    max_pages: int = 0


class StatusResponse(BaseModel):
    status: str
    progress: dict
    errors: list[dict]
    started_at: str | None
    completed_at: str | None


_active_migrations: dict[str, dict] = {}
_migration_lock = threading.Lock()


def _get_migration_state_path(migration_id: str) -> Path:
    return MIGRATION_STATE_DIR / f"{migration_id}.json"


def _load_migration_state(migration_id: str) -> dict | None:
    path = _get_migration_state_path(migration_id)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def _save_migration_state(migration_id: str, state: dict) -> None:
    path = _get_migration_state_path(migration_id)
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _delete_migration_state(migration_id: str) -> None:
    path = _get_migration_state_path(migration_id)
    if path.exists():
        path.unlink()


def _run_migration_in_thread(migration_id: str, params: dict) -> None:
    """Run the migration in a background thread."""
    try:
        with _migration_lock:
            _active_migrations[migration_id]["status"] = "running"
            _active_migrations[migration_id]["started_at"] = datetime.now(timezone.utc).isoformat()

        state = _load_migration_state(migration_id) or {}
        state["status"] = "running"
        state["started_at"] = datetime.now(timezone.utc).isoformat()
        _save_migration_state(migration_id, state)

        state_file = params.get("state_file") or f"migration_{migration_id}.json"

        import sys

        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
        from migrate_developerhub import (
            AccelDocsClient,
            build_slug_map,
            fetch_and_convert_page,
            import_hierarchy,
            rewrite_html_internal_links,
        )
        from migrate_developerhub import (
            discover_structure as _discover_structure,
        )

        params["state_file"] = str(MIGRATION_STATE_DIR / state_file)

        params.setdefault("use_playwright", False)
        params.setdefault("create_drive_docs", False)

        source_url = params["source_url"]
        use_playwright = params["use_playwright"]

        token = params["api_token"]
        backend_url = params["backend_url"]
        org_id = params["org_id"]
        product_id = params["product_id"]
        create_drive_docs = params["create_drive_docs"]

        client = AccelDocsClient(backend_url=backend_url, token=token, org_id=org_id)

        state = _load_migration_state(migration_id) or {}
        state["source_url"] = source_url
        state["product"] = params["product"]
        state["params"] = params
        state["progress"] = {"phase": "discovering", "message": "Discovering structure..."}
        _save_migration_state(migration_id, state)

        tree, fallback_links = _discover_structure(source_url, use_playwright=use_playwright, apply_category_map=True, max_depth=0)

        # Save tree and fallback_links to state for debugging and resume
        state = _load_migration_state(migration_id)
        state["tree"] = tree
        state["fallback_links"] = fallback_links
        _save_migration_state(migration_id, state)

        max_pages = params.get("max_pages", 0)
        if max_pages > 0 and len(fallback_links) > max_pages:
            fallback_links = fallback_links[:max_pages]
            state = _load_migration_state(migration_id)
            state["progress"] = {"phase": "fetching", "message": f"Fetching {max_pages} pages (safety limit)..."}
            _save_migration_state(migration_id, state)

        state = _load_migration_state(migration_id)
        state["progress"] = {"phase": "fetching", "message": f"Fetching {len(fallback_links)} pages..."}
        _save_migration_state(migration_id, state)

        page_data: dict[str, Any] = {}

        if use_playwright:
            from playwright.sync_api import sync_playwright
            from migrate_developerhub import _fetch_html_playwright

            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(headless=True)
                    context = browser.new_context(
                        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                    )
                    for idx, url in enumerate(fallback_links):
                        result = fetch_and_convert_page(url, pw_browser=context)
                        page_data[url] = result
                        if idx % 10 == 0:
                            state = _load_migration_state(migration_id)
                            state["progress"] = {
                                "phase": "fetching",
                                "message": f"Fetching pages... {idx + 1}/{len(fallback_links)}",
                                "fetched": idx + 1,
                                "total": len(fallback_links),
                            }
                            state["page_data"] = page_data
                            _save_migration_state(migration_id, state)
                    browser.close()
            except Exception as exc:
                logger.warning("Playwright page fetch failed: %s — falling back to static", exc)
                for idx, url in enumerate(fallback_links):
                    page_data[url] = fetch_and_convert_page(url)
                    if idx % 50 == 0:
                        state = _load_migration_state(migration_id)
                        state["progress"] = {
                            "phase": "fetching",
                            "message": f"Fetching pages... {idx + 1}/{len(fallback_links)}",
                            "fetched": idx + 1,
                            "total": len(fallback_links),
                        }
                        state["page_data"] = page_data
                        _save_migration_state(migration_id, state)
        else:
            for idx, url in enumerate(fallback_links):
                page_data[url] = fetch_and_convert_page(url)
                if idx % 50 == 0:
                    state = _load_migration_state(migration_id)
                    state["progress"] = {
                        "phase": "fetching",
                        "message": f"Fetching pages... {idx + 1}/{len(fallback_links)}",
                        "fetched": idx + 1,
                        "total": len(fallback_links),
                    }
                    state["page_data"] = page_data
                    _save_migration_state(migration_id, state)

        # Save final page_data
        state = _load_migration_state(migration_id)
        state["page_data"] = page_data
        state["progress"] = {"phase": "rewriting_links", "message": "Rewriting internal links..."}
        _save_migration_state(migration_id, state)

        from urllib.parse import urlparse

        parsed_source = urlparse(source_url)
        source_domain = parsed_source.netloc
        slug_map = build_slug_map(list(page_data.values()))
        for url, data in page_data.items():
            if data:
                if data.get("raw_html"):
                    data["raw_html"] = rewrite_html_internal_links(
                        data["raw_html"], source_domain, slug_map
                    )

        state = _load_migration_state(migration_id)
        state["progress"] = {"phase": "importing", "message": "Importing into AccelDocs...", "imported": 0}
        _save_migration_state(migration_id, state)

        migration_state: dict[str, Any] = {
            "page_id_map": {},
            "section_map": {},
        }
        old_url_to_page_id = import_hierarchy(
            client=client,
            tree=tree,
            product_id=product_id,
            page_data=page_data,
            state=migration_state,
            create_drive_docs=create_drive_docs,
        )

        # Fallback: import any pages from page_data that weren't imported via tree
        state = _load_migration_state(migration_id)
        state["progress"] = {"phase": "importing_fallback", "message": "Importing remaining pages..."}
        _save_migration_state(migration_id, state)

        pages_imported_via_tree = len(old_url_to_page_id)
        pages_in_page_data = len(page_data)

        if pages_in_page_data > pages_imported_via_tree:
            logger.info("Tree import covered %d/%d pages, importing remaining %d pages directly",
                       pages_imported_via_tree, pages_in_page_data, pages_in_page_data - pages_imported_via_tree)

            import time
            
            # Import pages that weren't imported via the tree
            for idx, (url, data) in enumerate(page_data.items()):
                if url in old_url_to_page_id:
                    continue
                if not data or not data.get("raw_html"):
                    continue

                try:
                    title = data.get("title", url.split("/")[-1])
                    html_content = data.get("raw_html", "")
                    drive_html = data.get("raw_html", "")

                    result = client.import_page(
                        title=title,
                        html_content=html_content,
                        section_id=product_id,  # Import to product root if no section found
                        display_order=1000 + idx,
                        create_drive_doc=create_drive_docs,
                        drive_html_content=drive_html,
                    )
                    page_id = result.get("id")
                    if page_id:
                        old_url_to_page_id[url] = page_id
                        migration_state["page_id_map"][url] = page_id
                    
                    # Rate limiting: add small delay to avoid hitting API rate limits (200/min)
                    time.sleep(0.35)
                    
                    # Update progress every 10 pages
                    if idx % 10 == 0:
                        state = _load_migration_state(migration_id)
                        state["progress"] = {
                            "phase": "importing_fallback",
                            "message": f"Importing pages... {idx + 1}/{pages_in_page_data - pages_imported_via_tree}",
                            "imported": idx + 1,
                            "total": pages_in_page_data - pages_imported_via_tree,
                        }
                        _save_migration_state(migration_id, state)
                        
                except Exception as exc:
                    logger.warning("Failed to import page %s: %s", url, exc)
                    time.sleep(1)  # Longer delay on error

        state = _load_migration_state(migration_id)
        state["status"] = "completed"
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        state["progress"] = {
            "phase": "completed",
            "message": f"Imported {len(old_url_to_page_id)} pages",
            "imported": len(old_url_to_page_id),
        }
        # Get section count from migration_state
        sections_created = len(migration_state.get("section_map", {}))
        state["result"] = {
            "pages_imported": len(old_url_to_page_id),
            "sections_created": sections_created,
            "page_id_map": old_url_to_page_id,
        }
        _save_migration_state(migration_id, state)

        with _migration_lock:
            _active_migrations[migration_id]["status"] = "completed"
            _active_migrations[migration_id]["completed_at"] = datetime.now(timezone.utc).isoformat()

    except Exception as exc:
        logger.exception("Migration %s failed: %s", migration_id, exc)
        tb = traceback.format_exc()
        state = _load_migration_state(migration_id)
        state["status"] = "failed"
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        state["error"] = str(exc)
        state["traceback"] = tb
        _save_migration_state(migration_id, state)

        with _migration_lock:
            _active_migrations[migration_id]["status"] = "failed"
            _active_migrations[migration_id]["error"] = str(exc)


@router.post("/discover", response_model=DiscoverResponse)
async def discover(
    body: DiscoverRequest,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DiscoverResponse:
    """Discover the hierarchy from a source URL (dry-run, no import)."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

    from migrate_developerhub import (
        discover_structure,
    )

    use_playwright = body.use_playwright

    try:
        tree, fallback_links = await asyncio.wait_for(
            asyncio.to_thread(
                discover_structure,
                body.source_url,
                use_playwright=use_playwright,
                apply_category_map=True,
                max_depth=2,
            ),
            timeout=55.0,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail="Discovery timed out after 55 seconds. The source site may be slow or unreachable.",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Discovery failed: {exc!r}") from exc

    return DiscoverResponse(
        source_url=body.source_url,
        source_type="developerhub",
        products=[{"name": body.product, "versions": []}],
        total_pages=len(fallback_links),
        hierarchy=tree,
    )


@router.post("/start")
async def start_migration(
    body: StartRequest,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Start a migration. Runs asynchronously in the background."""
    migration_id = f"{user.id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    state = {
        "migration_id": migration_id,
        "user_id": user.id,
        "status": "pending",
        "source_url": body.source_url,
        "product": body.product,
        "progress": {"phase": "pending", "message": "Starting..."},
        "errors": [],
    }
    _save_migration_state(migration_id, state)

    with _migration_lock:
        _active_migrations[migration_id] = {
            "status": "pending",
            "started_at": None,
            "completed_at": None,
            "error": None,
        }

    thread = threading.Thread(
        target=_run_migration_in_thread,
        args=(migration_id, body.model_dump()),
        daemon=True,
    )
    thread.start()

    return {
        "migration_id": migration_id,
        "status": "started",
        "message": "Migration started in background. Use GET /api/migration/status/{id} to track progress.",
    }


@router.get("/status/{migration_id}", response_model=StatusResponse)
async def get_status(
    migration_id: str,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StatusResponse:
    """Get the status of a migration."""
    state = _load_migration_state(migration_id)
    if not state:
        raise HTTPException(status_code=404, detail="Migration not found")

    if state.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="Not authorized to view this migration")

    return StatusResponse(
        status=state.get("status", "unknown"),
        progress=state.get("progress", {}),
        errors=state.get("errors", []),
        started_at=state.get("started_at"),
        completed_at=state.get("completed_at"),
    )


@router.post("/cancel/{migration_id}")
async def cancel_migration(
    migration_id: str,
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Cancel a running migration."""
    state = _load_migration_state(migration_id)
    if not state:
        raise HTTPException(status_code=404, detail="Migration not found")

    if state.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    if state.get("status") not in ("pending", "running"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel migration with status: {state.get('status')}")

    state["status"] = "cancelled"
    state["completed_at"] = datetime.now(timezone.utc).isoformat()
    _save_migration_state(migration_id, state)

    with _migration_lock:
        if migration_id in _active_migrations:
            _active_migrations[migration_id]["status"] = "cancelled"

    return {"status": "cancelled", "message": "Migration cancelled"}


@router.get("/history")
async def migration_history(
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Get all migrations for the current user."""
    migrations = []
    for path in MIGRATION_STATE_DIR.glob("*.json"):
        try:
            with open(path) as f:
                state = json.load(f)
            if state.get("user_id") == user.id:
                migrations.append({
                    "migration_id": state.get("migration_id"),
                    "status": state.get("status"),
                    "source_url": state.get("source_url"),
                    "product": state.get("product"),
                    "progress": state.get("progress"),
                    "started_at": state.get("started_at"),
                    "completed_at": state.get("completed_at"),
                    "result": state.get("result"),
                    "error": state.get("error"),
                })
        except Exception:
            continue

    return sorted(migrations, key=lambda x: x.get("started_at") or "", reverse=True)


# ---------------------------------------------------------------------------
# DB-backed migration ingestion endpoints (used by external crawler)
# ---------------------------------------------------------------------------


class IngestPageContent(BaseModel):
    url: HttpUrl
    title: str
    markdown: Optional[str] = None
    raw_html: Optional[str] = None
    drive_html: Optional[str] = None


class IngestTreeNode(BaseModel):
    title: str
    url: Optional[HttpUrl] = None
    depth: int
    children: List["IngestTreeNode"] = Field(default_factory=list)
    _section_type: Literal["section", "tab", "version", "page"] = "section"


IngestTreeNode.model_rebuild()


class IngestRequest(BaseModel):
    source_url: HttpUrl
    tree: List[IngestTreeNode]
    page_data: Dict[HttpUrl, IngestPageContent]
    create_drive_docs: bool = False


class MigrationJobResponse(BaseModel):
    id: int
    organization_id: int
    source_url: HttpUrl
    target_section_id: int
    status: str
    start_time: datetime
    end_time: Optional[datetime] = None
    total_pages: int
    completed_pages: int
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MigrationJobPageResponse(BaseModel):
    id: int
    migration_id: int
    source_url: HttpUrl
    title: str
    target_page_id: Optional[int] = None
    status: str
    error_message: Optional[str] = None
    section_node_path: str
    display_order: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


@router.post(
    "/{target_section_id}/start",
    response_model=MigrationJobResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_migration_ingest(
    target_section_id: int,
    request: IngestRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_section = db.query(Section).filter(
        Section.id == target_section_id,
        Section.organization_id == current_user.organization_id,
    ).first()
    if not target_section:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target section not found or does not belong to your organization.",
        )

    existing = db.query(Migration).filter(
        Migration.organization_id == current_user.organization_id,
        Migration.source_url == str(request.source_url),
        Migration.target_section_id == target_section_id,
        Migration.status.in_(["PENDING", "IN_PROGRESS"]),
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A migration from {request.source_url} to section {target_section_id} "
                f"is already PENDING or IN_PROGRESS (ID: {existing.id})."
            ),
        )

    migration = Migration(
        organization_id=current_user.organization_id,
        source_url=str(request.source_url),
        target_section_id=target_section_id,
        status="PENDING",
        total_pages=len(request.page_data),
        tree_json=json.dumps([node.model_dump() for node in request.tree]),
    )
    db.add(migration)
    db.flush()

    queued_pages: list[MigrationPage] = []
    for page_url, page_content in request.page_data.items():
        queued_pages.append(
            MigrationPage(
                migration_id=migration.id,
                source_url=str(page_url),
                title=page_content.title,
                html_content=page_content.raw_html,
                markdown_content=page_content.markdown,
                drive_html_content=page_content.drive_html,
                section_node_path="",
                display_order=0,
                status="PENDING",
            )
        )
    db.add_all(queued_pages)
    db.commit()
    db.refresh(migration)

    try:
        initialize_migration_sections_and_pages(db, migration, current_user)
    except MigrationServiceError as exc:
        db.rollback()
        migration.status = "FAILED"
        migration.error_message = f"Failed to initialize migration sections: {exc}"
        db.add(migration)
        db.commit()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    db.refresh(migration)
    return migration


@router.get("/jobs", response_model=List[MigrationJobResponse])
def list_migration_jobs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return db.query(Migration).filter(
        Migration.organization_id == current_user.organization_id
    ).order_by(Migration.created_at.desc()).all()


@router.get("/jobs/{migration_id}", response_model=MigrationJobResponse)
def get_migration_job(
    migration_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    migration = db.query(Migration).filter(
        Migration.id == migration_id,
        Migration.organization_id == current_user.organization_id,
    ).first()
    if not migration:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Migration not found.")
    return migration


@router.post("/jobs/{migration_id}/process-next", response_model=Optional[MigrationJobPageResponse])
def process_next_job_page(
    migration_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    migration = db.query(Migration).filter(
        Migration.id == migration_id,
        Migration.organization_id == current_user.organization_id,
        Migration.status.in_(["PENDING", "IN_PROGRESS"]),
    ).first()
    if not migration:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Migration not found or not in PENDING/IN_PROGRESS status.",
        )

    next_page = db.query(MigrationPage).filter(
        MigrationPage.migration_id == migration_id,
        MigrationPage.status == "PENDING",
    ).order_by(MigrationPage.display_order, MigrationPage.created_at).with_for_update().first()

    if not next_page:
        remaining = db.query(MigrationPage).filter(
            MigrationPage.migration_id == migration_id,
            MigrationPage.status.in_(["PENDING", "IN_PROGRESS"]),
        ).count()
        if remaining > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No pending pages found at this moment, but others might exist. Try again.",
            )

        migration.status = "COMPLETED"
        migration.end_time = datetime.now(timezone.utc)
        db.add(migration)
        db.commit()
        db.refresh(migration)

        try:
            resolve_all_migration_links_task(db, migration.id, current_user)
        except Exception as exc:
            db.rollback()
            migration.status = "FAILED"
            migration.error_message = f"Link resolution failed: {exc}"
            db.add(migration)
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Migration completed but link resolution failed: {exc}",
            )
        return None

    if migration.status == "PENDING":
        migration.status = "IN_PROGRESS"
        db.add(migration)
        db.flush()

    next_page.status = "IN_PROGRESS"
    db.add(next_page)
    db.flush()

    try:
        return process_migration_page_task(
            db,
            migration_page_id=next_page.id,
            current_user=current_user,
            create_drive_docs=False,
        )
    except MigrationServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        )
