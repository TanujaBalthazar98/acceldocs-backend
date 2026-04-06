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
from typing import Any

import requests
from fastapi import APIRouter, Depends, HTTPException, Header, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.config import settings
from app.database import get_db
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["migration"])

MIGRATION_STATE_DIR = Path(__file__).parent.parent.parent / "migration_states"
MIGRATION_STATE_DIR.mkdir(exist_ok=True)


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
                except Exception as exc:
                    logger.warning("Failed to import page %s: %s", url, exc)

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
        tree, fallback_links = await asyncio.to_thread(
            discover_structure,
            body.source_url,
            use_playwright=use_playwright,
            apply_category_map=True,
            max_depth=2,
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Discovery timed out. Try disabling Playwright or using a smaller subset.") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Discovery failed: {exc}") from exc

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
