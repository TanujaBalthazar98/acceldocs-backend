"""Public docs — serve rendered HTML documentation directly from FastAPI.

Routes:
  GET /docs/{org_slug}                  — landing page (section cards)
  GET /external-docs/{org_slug}         — invite-only external docs landing
  GET /internal-docs/{org_slug}         — org-only internal docs landing
  GET /docs/{org_slug}/{page_slug}      — single page view
  GET /docs/{org_slug}/{section_slug}/{page_slug}  — page inside explicit section
"""

import logging
import re
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.auth.routes import DOCS_SESSION_COOKIE
from app.middleware.auth import get_current_user as _get_auth_user
from app.config import settings
from app.models import (
    Organization,
    Page,
    PageComment,
    PageFeedback,
    PageRedirect,
    Section,
    User,
)
from app.services.visibility import (
    ViewerScope,
    build_viewer_scope,
    can_view_visibility,
    resolve_effective_visibility,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["public"])

DEFAULT_PRIMARY_COLOR = "#6366f1"
VALID_AUDIENCES = {"all", "public", "internal", "external"}
_GOOGLE_DOC_URL_RE = re.compile(r"docs\.google\.com/document/d/([a-zA-Z0-9_-]+)")
_CANONICAL_DOCS_PATH_RE = re.compile(r"^/(docs|internal-docs|external-docs)/([^/]+)/p/(\d+)/([^/?#]+)$")
_LEGACY_DOCS_PATH_RE = re.compile(r"^/(docs|internal-docs|external-docs)/([^/]+)/([^/?#]+)$")
_HREF_ATTR_RE = re.compile(r'href=(["\'])(.*?)\1', re.IGNORECASE)

# Template engine — load from app/templates/
_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db() -> Session:
    db = SessionLocal()
    setattr(db, "_public_router_managed", True)
    return db


def _close_db(db: Session) -> None:
    """Close only sessions created by this module.

    Tests often monkeypatch `_get_db` to return a shared fixture session.
    Closing that shared session in route handlers detaches fixture instances.
    """
    if getattr(db, "_public_router_managed", False):
        db.close()


def _org_initials(name: str) -> str:
    words = name.strip().split()
    if len(words) >= 2:
        return (words[0][0] + words[-1][0]).upper()
    return name[:2].upper()


def _org_hierarchy_mode(org: Organization) -> str:
    return "flat" if getattr(org, "hierarchy_mode", None) == "flat" else "product"


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_last_updated(page: Page) -> str | None:
    parsed = (
        _parse_timestamp(page.last_synced_at)
        or _parse_timestamp(page.drive_modified_at)
        or _parse_timestamp(getattr(page, "last_published_at", None))
    )
    if not parsed:
        if page.updated_at:
            parsed = page.updated_at
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            return None
    return parsed.astimezone(timezone.utc).strftime("%b %d, %Y %H:%M UTC")


def _viewer_display_name(user: User | None) -> str | None:
    if user and user.name and user.name.strip():
        return user.name.strip()
    if user and user.email:
        local_part = user.email.split("@", 1)[0].strip()
        if local_part:
            return local_part
    return None


def _resolve_request_user(request: Request, db: Session) -> User | None:
    """Best-effort auth for public docs requests.

    Public docs stay accessible without auth.
    """
    auth_header = request.headers.get("Authorization")
    candidates: list[str] = []
    if auth_header:
        candidates.append(auth_header)

    cookie_token = (request.cookies.get(DOCS_SESSION_COOKIE) or "").strip()
    if cookie_token:
        candidates.append(f"Bearer {cookie_token}")

    auth_user = None
    for candidate in candidates:
        try:
            auth_user = _get_auth_user(candidate)
            if auth_user:
                break
        except HTTPException:
            continue

    if not auth_user:
        return None
    return db.get(User, auth_user.id)


def _resolve_request_user_with_optional_query_token(
    request: Request,
    db: Session,
    *,
    allow_query_token: bool = False,
) -> User | None:
    """Resolve user with optional one-time query token fallback.

    Query-token fallback is intended only for internal docs redirect bootstrap
    when cross-origin cookie setup fails in local development.
    """
    auth_header = request.headers.get("Authorization")
    candidates: list[str] = []
    if auth_header:
        candidates.append(auth_header)

    cookie_token = (request.cookies.get(DOCS_SESSION_COOKIE) or "").strip()
    if cookie_token:
        candidates.append(f"Bearer {cookie_token}")

    if allow_query_token:
        query_token = (request.query_params.get("auth_token") or "").strip()
        if query_token:
            candidates.append(f"Bearer {query_token}")

    auth_user = None
    for candidate in candidates:
        try:
            auth_user = _get_auth_user(candidate)
            if auth_user:
                break
        except HTTPException:
            continue

    if not auth_user:
        return None
    return db.get(User, auth_user.id)


def _is_secure_request(request: Request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return request.url.scheme == "https"


def _current_url_without_auth_token(request: Request) -> str:
    params = [(k, v) for (k, v) in request.query_params.multi_items() if k != "auth_token"]
    query = urlencode(params, doseq=True)
    return str(request.url.replace(query=query))


def _bootstrap_docs_cookie_redirect_if_needed(request: Request) -> RedirectResponse | None:
    """If auth_token query param is present, move it into HttpOnly cookie and strip URL token."""
    query_token = (request.query_params.get("auth_token") or "").strip()
    if not query_token:
        return None

    response = RedirectResponse(url=_current_url_without_auth_token(request), status_code=307)
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/")
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/docs")
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/internal-docs")
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/external-docs")
    response.set_cookie(
        key=DOCS_SESSION_COOKIE,
        value=query_token,
        max_age=24 * 3600,
        httponly=True,
        secure=_is_secure_request(request),
        samesite="lax",
        path="/",
    )
    return response


def _normalize_audience(audience: str | None) -> str | None:
    candidate = (audience or "").strip().lower()
    if not candidate:
        return None
    if candidate in VALID_AUDIENCES:
        return candidate
    return None


def _resolve_route_audience(
    audience: str | None,
    docs_root: str,
) -> tuple[str | None, str | None]:
    """Resolve (effective filter audience, template/link audience) for a route.

    Rules:
    - /internal-docs is always internal-only.
    - /external-docs is always external-only.
    - /docs is strict public by default.
    - /docs can opt into external audience via ?audience=external.
    """
    normalized_docs_root = docs_root.rstrip("/") or "/docs"
    if normalized_docs_root == "/internal-docs":
        return "internal", None
    if normalized_docs_root == "/external-docs":
        return "external", "external"

    normalized = _normalize_audience(audience)
    if normalized == "external":
        return "external", "external"

    # Never allow internal/all on public docs routes; keep /docs strictly public.
    return "public", None


def _audience_allows_visibility(audience: str | None, viewer_scope: ViewerScope, visibility: str) -> bool:
    normalized = _normalize_audience(audience)
    if normalized is None or normalized == "all":
        return can_view_visibility(viewer_scope, visibility)
    if normalized == "public":
        return visibility == "public"
    if normalized == "internal":
        return viewer_scope.is_org_member and visibility == "internal"
    if normalized == "external":
        return (viewer_scope.is_org_member or viewer_scope.is_external_allowed) and visibility == "external"
    return can_view_visibility(viewer_scope, visibility)


def _is_page_visible(
    page: Page,
    section_visibility: str | None,
    viewer_scope: ViewerScope,
    audience: str | None = None,
) -> bool:
    visibility = resolve_effective_visibility(section_visibility, page.visibility_override)
    return _audience_allows_visibility(audience, viewer_scope, visibility)


def _build_section_node(
    section: Section,
    org_id: int,
    db: Session,
    viewer_scope: ViewerScope,
    audience: str | None = None,
) -> dict | None:
    """Recursively build a nav node for a section. Returns None if the subtree has no published pages."""
    all_pages = (
        db.query(Page)
        .filter(Page.section_id == section.id, Page.is_published == True)
        .order_by(Page.display_order, Page.title)
        .all()
    )
    pages = [p for p in all_pages if _is_page_visible(p, section.visibility, viewer_scope, audience)]
    child_sections = (
        db.query(Section)
        .filter(Section.organization_id == org_id, Section.parent_id == section.id)
        .order_by(Section.display_order, Section.name)
        .all()
    )
    children = [
        n
        for s in child_sections
        for n in [_build_section_node(s, org_id, db, viewer_scope, audience)]
        if n is not None
    ]

    if not pages and not children:
        return None

    first_page_slug = None
    first_page_id = None
    if pages:
        first_page_id = pages[0].id
        first_page_slug = pages[0].slug
    else:
        first_page_id = next((child.get("first_page_id") for child in children if child.get("first_page_id")), None)
        first_page_slug = next((child.get("first_page_slug") for child in children if child.get("first_page_slug")), None)

    return {
        "id": section.id,
        "name": section.name,
        "slug": section.slug,
        "section_type": section.section_type or "section",
        "display_order": section.display_order,
        "pages": [{"id": p.id, "title": p.title, "slug": p.slug} for p in pages],
        "children": children,
        "first_page_id": first_page_id,
        "first_page_slug": first_page_slug,
    }


def _count_pages_recursive(node: dict) -> int:
    total = len(node.get("pages", []))
    for child in node.get("children", []):
        total += _count_pages_recursive(child)
    return total


def _build_top_nodes(
    org_id: int,
    db: Session,
    viewer_scope: ViewerScope,
    audience: str | None = None,
) -> list[dict]:
    """Build nav nodes for all top-level sections."""
    top_sections = (
        db.query(Section)
        .filter(Section.organization_id == org_id, Section.parent_id.is_(None))
        .order_by(Section.display_order, Section.name)
        .all()
    )
    nodes = [
        n
        for s in top_sections
        for n in [_build_section_node(s, org_id, db, viewer_scope, audience)]
        if n is not None
    ]
    return nodes


def _build_top_section_cards(
    org_id: int,
    db: Session,
    viewer_scope: ViewerScope,
    audience: str | None = None,
) -> list[dict]:
    """Landing page section cards with page count and first page slug."""
    top_sections = (
        db.query(Section)
        .filter(Section.organization_id == org_id, Section.parent_id.is_(None))
        .order_by(Section.display_order, Section.name)
        .all()
    )
    cards = []
    for s in top_sections:
        node = _build_section_node(s, org_id, db, viewer_scope, audience)
        if node is None:
            continue
        cards.append({
            "id": s.id,
            "name": s.name,
            "slug": s.slug,
            "page_count": _count_pages_recursive(node),
            "first_page_id": node.get("first_page_id"),
            "first_page_slug": node.get("first_page_slug"),
        })
    return cards


def _flatten_same_name_wrapper(node: dict | None) -> dict | None:
    """Flatten legacy wrapper: Product -> same-name child -> real content."""
    if not node:
        return None

    current = node
    while (
        len(current.get("pages", [])) == 0
        and len(current.get("children", [])) == 1
        and current.get("name")
    ):
        child = current["children"][0]
        if (child.get("name") or "").strip().lower() != current["name"].strip().lower():
            break
        current = child
    return current


def _collect_preview_pages(node: dict, limit: int = 4) -> list[dict]:
    """Collect up to `limit` descendant pages in display order."""
    pages: list[dict] = []

    def _walk(n: dict) -> None:
        if len(pages) >= limit:
            return
        for page in n.get("pages", []):
            pages.append({"id": page["id"], "title": page["title"], "slug": page["slug"]})
            if len(pages) >= limit:
                return
        for child in n.get("children", []):
            _walk(child)
            if len(pages) >= limit:
                return

    _walk(node)
    return pages


def _build_landing_groups(product_node: dict | None) -> list[dict]:
    """Build Monte Carlo-style topic groups for landing."""
    normalized = _flatten_same_name_wrapper(product_node)
    if not normalized:
        return []

    group_nodes = [
        child
        for child in normalized.get("children", [])
        if (child.get("section_type") or "section") != "version"
    ]
    if not group_nodes:
        group_nodes = [normalized]

    groups: list[dict] = []
    for group in group_nodes:
        preview_pages = _collect_preview_pages(group, limit=4)
        if not preview_pages:
            continue
        groups.append(
            {
                "name": group.get("name"),
                "pages": preview_pages,
                "more_id": group.get("first_page_id") or preview_pages[0]["id"],
                "more_slug": group.get("first_page_slug") or preview_pages[0]["slug"],
            }
        )
    return groups


def _build_flat_landing_groups(top_nodes: list[dict]) -> list[dict]:
    """Build landing groups when docs are configured in flat mode (no product rail)."""
    groups: list[dict] = []
    for node in top_nodes:
        normalized = _flatten_same_name_wrapper(node) or node
        preview_pages = _collect_preview_pages(normalized, limit=4)
        if not preview_pages:
            continue
        groups.append(
            {
                "name": normalized.get("name"),
                "pages": preview_pages,
                "more_id": normalized.get("first_page_id") or preview_pages[0]["id"],
                "more_slug": normalized.get("first_page_slug") or preview_pages[0]["slug"],
            }
        )
    return groups


def _build_search_index(nodes: list[dict]) -> list[dict]:
    """Build a lightweight page index for client-side search fallback."""
    index: list[dict] = []
    seen_page_ids: set[int] = set()

    def _walk(node: dict, path: list[str]) -> None:
        current_path = [*path, node.get("name", "")]
        section_name = " / ".join([part for part in current_path if part]).strip()

        for page in node.get("pages", []):
            page_id = page.get("id")
            slug = page.get("slug")
            if page_id is None or not slug or page_id in seen_page_ids:
                continue
            seen_page_ids.add(page_id)
            index.append(
                {
                    "title": page.get("title", ""),
                    "page_id": page_id,
                    "slug": slug,
                    "section_name": section_name,
                }
            )

        for child in node.get("children", []):
            _walk(child, current_path)

    for root in nodes:
        _walk(root, [])

    return index


def _find_page_path(
    node: dict,
    page_slug: str | None = None,
    page_id: int | None = None,
    path: list[dict] | None = None,
) -> list[dict] | None:
    """Return section path to a page id/slug, or None if not found in this node."""
    current_path = [*(path or []), node]
    if page_id is not None:
        if any(p.get("id") == page_id for p in node.get("pages", [])):
            return current_path
    elif page_slug is not None and any(p.get("slug") == page_slug for p in node.get("pages", [])):
        return current_path
    for child in node.get("children", []):
        found = _find_page_path(child, page_slug=page_slug, page_id=page_id, path=current_path)
        if found:
            return found
    return None


def _parse_version_parts(value: str | None) -> tuple[int, int, int] | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    text = text[1:] if text.startswith("v") else text
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?$", text)
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    patch = int(match.group(3) or 0)
    return (major, minor, patch)


def _sort_versions_desc(nodes: list[dict]) -> list[dict]:
    def key(node: dict) -> tuple[int, int, int, int, int, str]:
        parts = _parse_version_parts(node.get("name")) or _parse_version_parts(node.get("slug"))
        if parts:
            return (1, parts[0], parts[1], parts[2], node.get("display_order", 0), node.get("name", ""))
        return (0, 0, 0, 0, node.get("display_order", 0), node.get("name", ""))

    return sorted(nodes, key=key, reverse=True)


def _find_first_page_excluding_versions(node: dict | None) -> tuple[int | None, str | None]:
    if not node:
        return (None, None)
    pages = node.get("pages") or []
    if pages:
        first = pages[0]
        return first.get("id"), first.get("slug")
    for child in node.get("children", []):
        if (child.get("section_type") or "section") == "version":
            continue
        page_id, page_slug = _find_first_page_excluding_versions(child)
        if page_id and page_slug:
            return page_id, page_slug
    return (None, None)


def _build_product_version_nodes(
    *,
    org_id: int,
    product_section_id: int,
    db: Session,
    viewer_scope: ViewerScope,
    audience: str | None = None,
) -> list[dict]:
    """Build switchable version nodes under a product.

    Includes published version sections even when they have no visible pages yet,
    so the version selector remains stable.
    """
    version_sections = (
        db.query(Section)
        .filter(
            Section.organization_id == org_id,
            Section.parent_id == product_section_id,
            Section.section_type == "version",
        )
        .order_by(Section.display_order, Section.name)
        .all()
    )
    nodes: list[dict] = []
    for version_section in version_sections:
        # Do not pre-filter by section visibility only.
        # A version section can contain audience-visible pages via page-level overrides
        # (for example, internal pages under a public version section).
        # _build_section_node applies page-level visibility filtering correctly.
        existing_node = _build_section_node(version_section, org_id, db, viewer_scope, audience)
        if existing_node is not None:
            nodes.append(existing_node)
    return _sort_versions_desc(nodes)


def _resolve_page_navigation(
    all_top_nodes: list[dict],
    page_slug: str,
    page_id: int | None = None,
    hierarchy_mode: str = "product",
    version_slug: str | None = None,
) -> dict:
    """Resolve tabs, sidebar tree, and product header for a page view."""
    path: list[dict] = []
    for node in all_top_nodes:
        found = _find_page_path(node, page_slug=page_slug, page_id=page_id)
        if found:
            path = found
            break

    active_top_node = path[0] if path else None
    def _build_docs_node(node_id_prefix: str, pages: list[dict], children: list[dict]) -> dict | None:
        docs_first_id = None
        docs_first_slug = None
        if pages:
            docs_first_id = pages[0].get("id")
            docs_first_slug = pages[0].get("slug")
        if not docs_first_slug:
            docs_first_id = next(
                (child.get("first_page_id") for child in children if child.get("first_page_id")),
                None,
            )
            docs_first_slug = next(
                (child.get("first_page_slug") for child in children if child.get("first_page_slug")),
                None,
            )

        if not (pages or children):
            return None

        return {
            "id": f"{node_id_prefix}-docs",
            "name": "Docs",
            "slug": "__docs",
            "section_type": "tab",
            "pages": pages,
            "children": children,
            "first_page_id": docs_first_id,
            "first_page_slug": docs_first_slug,
        }

    if hierarchy_mode == "flat":
        if not active_top_node:
            return {
                "top_tabs": [],
                "nav_sections": all_top_nodes,
                "current_tab_slug": None,
                "product_header": None,
                "top_versions": [],
                "current_version_slug": None,
            }

        tab_roots = [
            node for node in all_top_nodes
            if (node.get("section_type") or "section") == "tab"
        ]
        non_tab_roots = [
            node for node in all_top_nodes
            if (node.get("section_type") or "section") != "tab"
        ]
        if tab_roots:
            active_tab = next(
                (tab for tab in tab_roots if _find_page_path(tab, page_slug=page_slug, page_id=page_id)),
                None,
            )
            docs_node = _build_docs_node("flat-root", [], non_tab_roots)
            top_tabs = [*tab_roots, *([docs_node] if docs_node else [])]
            if active_tab is not None:
                nav_sections = [active_tab]
                current_tab_slug = active_tab.get("slug")
            elif docs_node is not None:
                nav_sections = [docs_node]
                current_tab_slug = docs_node.get("slug")
            else:
                nav_sections = [tab_roots[0]]
                current_tab_slug = tab_roots[0].get("slug")
            return {
                "top_tabs": top_tabs,
                "nav_sections": nav_sections,
                "current_tab_slug": current_tab_slug,
                "product_header": None,
                "top_versions": [],
                "current_version_slug": None,
            }

        return {
            "top_tabs": [],
            "nav_sections": all_top_nodes,
            "current_tab_slug": None,
            "product_header": None,
            "top_versions": [],
            "current_version_slug": None,
        }

    # Product hierarchy mode (default).
    if not active_top_node:
        return {
            "top_tabs": [],
            "nav_sections": all_top_nodes,
            "current_tab_slug": None,
            "product_header": None,
            "top_versions": [],
            "current_version_slug": None,
        }

    # Legacy shape: single root node explicitly marked as tab.
    if (active_top_node.get("section_type") or "section") == "tab":
        return {
            "top_tabs": [active_top_node],
            "nav_sections": [active_top_node],
            "current_tab_slug": active_top_node.get("slug"),
            "product_header": active_top_node,
            "top_versions": [],
            "current_version_slug": None,
        }

    product_children = active_top_node.get("children", [])
    version_children = _sort_versions_desc(
        [
            child for child in product_children
            if (child.get("section_type") or "section") == "version"
        ]
    )
    active_content_root = active_top_node
    current_version_slug = None

    if version_children:
        active_version = next(
            (version for version in version_children if _find_page_path(version, page_slug=page_slug, page_id=page_id)),
            None,
        )
        if active_version is None and version_slug:
            active_version = next((version for version in version_children if version.get("slug") == version_slug), None)
        if active_version is not None:
            active_content_root = active_version
            current_version_slug = active_version.get("slug")
        else:
            # Base-content scope: keep version switcher available, but render sidebar
            # from product content excluding version branches.
            active_content_root = {
                **active_top_node,
                "children": [
                    child
                    for child in active_top_node.get("children", [])
                    if (child.get("section_type") or "section") != "version"
                ],
            }

    content_children = active_content_root.get("children", [])
    tab_children = [
        child for child in content_children
        if (child.get("section_type") or "section") == "tab"
    ]
    non_tab_children = [
        child for child in content_children
        if (child.get("section_type") or "section") != "tab"
    ]
    content_pages = active_content_root.get("pages", [])

    # If this product has explicit tab children, use strict product + tabs layout.
    if tab_children:
        active_tab = next(
            (tab for tab in tab_children if _find_page_path(tab, page_slug=page_slug, page_id=page_id)),
            None,
        )
        docs_node = _build_docs_node(
            f"product-{active_content_root.get('id')}",
            content_pages,
            non_tab_children,
        )
        top_tabs = [*tab_children, *([docs_node] if docs_node else [])]

        if active_tab is not None:
            nav_sections = [active_tab]
            current_tab_slug = active_tab.get("slug")
        elif docs_node is not None:
            nav_sections = [docs_node]
            current_tab_slug = docs_node.get("slug")
        else:
            nav_sections = [tab_children[0]]
            current_tab_slug = tab_children[0].get("slug")

        return {
            "top_tabs": top_tabs,
            "nav_sections": nav_sections,
            "current_tab_slug": current_tab_slug,
            "product_header": active_top_node,
            "top_versions": version_children,
            "current_version_slug": current_version_slug,
        }

    # Legacy fallback for non-typed structures.
    if len(all_top_nodes) == 1:
        only_node = active_content_root if active_content_root else all_top_nodes[0]
        normalized_only = _flatten_same_name_wrapper(only_node) or only_node
        return {
            "top_tabs": [],
            "nav_sections": [normalized_only],
            "current_tab_slug": None,
            "product_header": active_top_node,
            "top_versions": version_children,
            "current_version_slug": current_version_slug,
        }

    # Multiple top-level sections represent products, not tabs.
    normalized_active = _flatten_same_name_wrapper(active_content_root) or active_content_root
    return {
        "top_tabs": [],
        "nav_sections": [normalized_active] if normalized_active else all_top_nodes,
        "current_tab_slug": None,
        "product_header": active_top_node,
        "top_versions": version_children,
        "current_version_slug": current_version_slug,
    }


def _clean_gdoc_html(raw_html: str) -> str:
    """Strip Google Docs boilerplate and inline styles that break our design.

    Extracts just the <body> content and removes the worst style overrides.
    """
    # Extract body content
    body_match = re.search(r"<body[^>]*>(.*?)</body>", raw_html, re.DOTALL | re.IGNORECASE)
    if body_match:
        html = body_match.group(1)
    else:
        html = raw_html

    # Remove Google Docs style block (massive inline CSS)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)

    # Remove empty Google Docs spacer elements
    html = re.sub(r'<p[^>]*class="[^"]*c\d+[^"]*"[^>]*>\s*<span[^>]*>\s*</span>\s*</p>', "", html)

    # Strip specific Google font family / size inline styles (keep bold/italic)
    html = re.sub(r'font-family:[^;"]+"?;?', "", html)
    html = re.sub(r'font-size:\s*[\d.]+pt;?', "", html)
    html = re.sub(r'color:\s*#000000;?', "", html)
    html = re.sub(r'background-color:\s*#ffffff;?', "", html, flags=re.IGNORECASE)

    return html.strip()


def _audience_suffix_for_links(docs_root: str, audience_for_links: str | None) -> str:
    normalized_docs_root = docs_root.rstrip("/") or "/docs"
    normalized = _normalize_audience(audience_for_links)
    if normalized_docs_root == "/docs" and normalized:
        return f"?audience={normalized}"
    return ""


def _canonical_page_href(
    page: Page,
    *,
    org_slug: str,
    docs_root: str,
    audience_for_links: str | None = None,
) -> str:
    suffix = _audience_suffix_for_links(docs_root, audience_for_links)
    return f"{docs_root}/{org_slug}/{page.slug}{suffix}"


def _page_fallback_href(
    page: Page,
    *,
    org_slug: str,
    docs_root: str,
    audience_for_links: str | None = None,
) -> str:
    suffix = _audience_suffix_for_links(docs_root, audience_for_links)
    return f"{docs_root}/{org_slug}/p/{page.id}/{page.slug}{suffix}"


def _landing_href(*, org_slug: str, docs_root: str, audience_for_links: str | None = None) -> str:
    suffix = _audience_suffix_for_links(docs_root, audience_for_links)
    return f"{docs_root}/{org_slug}{suffix}"


def _docs_root_for_visibility(visibility: str) -> str:
    normalized = (visibility or "").strip().lower()
    if normalized == "internal":
        return "/internal-docs"
    if normalized == "external":
        return "/external-docs"
    return "/docs"


def _rewrite_page_links(
    html: str,
    *,
    org: Organization,
    db: Session,
    docs_root: str,
    audience_for_links: str | None = None,
) -> str:
    """Rewrite known internal links to canonical page URLs.

    Handles:
    - Google Docs links (docs.google.com/document/d/{id}/...)
    - Legacy docs slug links (/docs/{org}/{slug})
    - Canonical links in any docs root are normalized to current docs_root
    """
    org_slug = org.slug or str(org.id)
    org_aliases = {org_slug, str(org.id)}
    pages = (
        db.query(Page)
        .filter(Page.organization_id == org.id, Page.is_published == True)
        .order_by(Page.id.asc())
        .all()
    )
    section_ids = {p.section_id for p in pages if p.section_id is not None}
    sections_by_id = {
        s.id: s for s in db.query(Section).filter(Section.id.in_(section_ids)).all()
    } if section_ids else {}
    page_visibility: dict[int, str] = {}
    for p in pages:
        section = sections_by_id.get(p.section_id) if p.section_id else None
        page_visibility[p.id] = resolve_effective_visibility(
            section.visibility if section else "public",
            p.visibility_override,
        )
    by_doc_id = {p.google_doc_id: p for p in pages if p.google_doc_id}
    by_id = {p.id: p for p in pages}
    by_slug: dict[str, Page | None] = {}
    for p in pages:
        existing = by_slug.get(p.slug)
        if existing is None and p.slug not in by_slug:
            by_slug[p.slug] = p
        elif existing is not None:
            by_slug[p.slug] = None

    def replace_href(match: re.Match[str]) -> str:
        quote = match.group(1)
        href = (match.group(2) or "").strip()
        if not href:
            return match.group(0)
        lowered = href.lower()
        if lowered.startswith(("#", "mailto:", "tel:", "javascript:")):
            return match.group(0)

        parsed = urlparse(href)
        target_page: Page | None = None
        fragment = parsed.fragment

        gdoc_match = _GOOGLE_DOC_URL_RE.search(href)
        if gdoc_match:
            target_page = by_doc_id.get(gdoc_match.group(1))
        else:
            path = parsed.path or ""
            canonical_match = _CANONICAL_DOCS_PATH_RE.match(path)
            if canonical_match:
                _, path_org, page_id_text, _ = canonical_match.groups()
                if path_org in org_aliases:
                    try:
                        target_page = by_id.get(int(page_id_text))
                    except ValueError:
                        target_page = None
            if target_page is None:
                legacy_match = _LEGACY_DOCS_PATH_RE.match(path)
                if legacy_match:
                    _, path_org, slug = legacy_match.groups()
                    if path_org in org_aliases:
                        target_page = by_slug.get(slug) or None
            if target_page is None and not parsed.scheme and not path.startswith("/") and "/" not in path:
                target_page = by_slug.get(path) or None

        if target_page is None:
            return match.group(0)

        target_visibility = page_visibility.get(target_page.id, "public")
        target_docs_root = _docs_root_for_visibility(target_visibility)
        rewritten = _canonical_page_href(
            target_page,
            org_slug=org_slug,
            docs_root=target_docs_root,
            audience_for_links=audience_for_links if target_docs_root == docs_root else None,
        )
        if fragment:
            rewritten += f"#{fragment}"
        return f"href={quote}{rewritten}{quote}"

    return _HREF_ATTR_RE.sub(replace_href, html)


def _lookup_redirect_for_page_id(
    *,
    organization_id: int,
    page_id: int,
    page_slug: str,
    db: Session,
) -> PageRedirect | None:
    redirects = (
        db.query(PageRedirect)
        .filter(
            PageRedirect.organization_id == organization_id,
            PageRedirect.source_page_id == page_id,
            PageRedirect.is_active == True,
        )
        .order_by(PageRedirect.updated_at.desc())
        .all()
    )
    if not redirects:
        return None
    exact = next((r for r in redirects if r.source_slug == page_slug), None)
    return exact or redirects[0]


def _lookup_redirect_for_slug(
    *,
    organization_id: int,
    page_slug: str,
    db: Session,
) -> PageRedirect | None:
    return (
        db.query(PageRedirect)
        .filter(
            PageRedirect.organization_id == organization_id,
            PageRedirect.source_slug == page_slug,
            PageRedirect.is_active == True,
        )
        .order_by(PageRedirect.updated_at.desc())
        .first()
    )


def _resolve_redirect_target_url(
    *,
    redirect: PageRedirect,
    org: Organization,
    db: Session,
    viewer_scope: ViewerScope,
    audience: str | None,
    docs_root: str,
    audience_for_links: str | None,
) -> str:
    org_slug = org.slug or str(org.id)
    if redirect.target_page_id:
        target_page = (
            db.query(Page)
            .filter(
                Page.organization_id == org.id,
                Page.id == redirect.target_page_id,
                Page.is_published == True,
            )
            .first()
        )
        if target_page:
            section = db.get(Section, target_page.section_id) if target_page.section_id else None
            if _is_page_visible(
                target_page,
                section.visibility if section else "public",
                viewer_scope,
                audience,
            ):
                return _canonical_page_href(
                    target_page,
                    org_slug=org_slug,
                    docs_root=docs_root,
                    audience_for_links=audience_for_links,
                )

    if redirect.target_url:
        return redirect.target_url
    return _landing_href(
        org_slug=org_slug,
        docs_root=docs_root,
        audience_for_links=audience_for_links,
    )


def _render(template_name: str, **ctx) -> str:
    tpl = _jinja_env.get_template(template_name)
    return tpl.render(**ctx)


def _base_ctx(
    org: Organization,
    nav_sections: list,
    top_sections: list,
    primary_color: str,
    hierarchy_mode: str,
    audience: str | None = None,
    docs_root: str = "/docs",
) -> dict:
    normalized_audience = _normalize_audience(audience)
    normalized_docs_root = docs_root.rstrip("/") or "/docs"
    template_audience = normalized_audience if normalized_docs_root == "/docs" else None
    audience_suffix = f"?audience={template_audience}" if template_audience else ""
    if normalized_docs_root == "/internal-docs":
        docs_visibility_label = "Internal"
        docs_mode = "internal"
    elif normalized_docs_root == "/external-docs":
        docs_visibility_label = "External"
        docs_mode = "external"
    elif template_audience == "external":
        docs_visibility_label = "External"
        docs_mode = "public"
    elif template_audience == "all":
        docs_visibility_label = "All"
        docs_mode = "public"
    else:
        docs_visibility_label = "Public"
        docs_mode = "public"
    # Workspace display settings
    sidebar_position = getattr(org, "sidebar_position", None) or "left"
    show_toc = True if getattr(org, "show_toc", None) is None else bool(org.show_toc)
    code_theme = getattr(org, "code_theme", None) or "github-dark"
    max_content_width = getattr(org, "max_content_width", None) or "4xl"
    header_html = getattr(org, "header_html", None) or ""
    footer_html = getattr(org, "footer_html", None) or ""
    custom_css = getattr(org, "custom_css", None) or ""
    font_heading = getattr(org, "font_heading", None) or ""
    font_body = getattr(org, "font_body", None) or ""
    secondary_color = getattr(org, "secondary_color", None) or ""
    accent_color = getattr(org, "accent_color", None) or ""
    hero_title = getattr(org, "hero_title", None) or ""
    hero_description = getattr(org, "hero_description", None) or ""
    show_search_on_landing = getattr(org, "show_search_on_landing", True)
    if show_search_on_landing is None:
        show_search_on_landing = True
    show_featured_projects = getattr(org, "show_featured_projects", True)
    if show_featured_projects is None:
        show_featured_projects = True
    copyright_text = getattr(org, "copyright", None) or ""

    return {
        "org_name": org.name,
        "org_slug": org.slug or str(org.id),
        "org_logo": org.logo_url,
        "org_initials": _org_initials(org.name),
        "org_tagline": org.tagline,
        "primary_color": primary_color,
        # Workspace display settings
        "sidebar_position": sidebar_position,
        "show_toc": show_toc,
        "code_theme": code_theme,
        "max_content_width": max_content_width,
        "header_html": header_html,
        "footer_html": footer_html,
        "custom_css": custom_css,
        "font_heading": font_heading,
        "font_body": font_body,
        "secondary_color": secondary_color,
        "accent_color": accent_color,
        "hero_title": hero_title,
        "hero_description": hero_description,
        "show_search_on_landing": show_search_on_landing,
        "show_featured_projects": show_featured_projects,
        "copyright": copyright_text,
        # Navigation
        "nav_sections": nav_sections,
        "top_sections": top_sections,
        "top_tabs": [],
        "top_versions": [],
        "current_tab_slug": None,
        "current_version_slug": None,
        "base_version_label": "Original",
        "product_header": None,
        "landing_products": [],
        "landing_selected_product": None,
        "landing_versions": [],
        "landing_selected_version": None,
        "landing_base_label": "Original",
        "landing_groups": [],
        "landing_get_started_slug": None,
        "landing_get_started_id": None,
        "landing_search_index": [],
        "hierarchy_mode": hierarchy_mode,
        "audience": template_audience,
        "audience_suffix": audience_suffix,
        "docs_root": normalized_docs_root,
        "docs_mode": docs_mode,
        "docs_visibility_label": docs_visibility_label,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _lookup_org(org_slug: str, db: Session) -> Organization:
    """Find org by slug, or by numeric ID as fallback."""
    org = db.query(Organization).filter(Organization.slug == org_slug).first()
    if not org and org_slug.isdigit():
        org = db.get(Organization, int(org_slug))
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


def _ensure_internal_org_member(viewer_scope: ViewerScope) -> None:
    if viewer_scope.is_org_member:
        return
    raise HTTPException(status_code=403, detail="Internal docs require organization access")


def _ensure_external_docs_access(viewer_scope: ViewerScope) -> None:
    if viewer_scope.is_org_member or viewer_scope.is_external_allowed:
        return
    raise HTTPException(status_code=403, detail="External docs require invitation")


def _access_required_html(
    *,
    org: Organization,
    docs_root: str,
    required_scope: str,
    request: Request,
) -> HTMLResponse:
    """Render a lightweight access gate instead of JSON 403 for docs pages."""
    scope_label = "internal" if required_scope == "internal" else "external"
    scope_title = "Internal docs only" if required_scope == "internal" else "External docs only"
    message = (
        "Sign in with your organization account to access internal docs."
        if required_scope == "internal"
        else "This page is shared only with invited external users. Ask for an invitation to continue."
    )
    current_url = str(request.url)
    sign_in_url = f"/auth/docs-login?next={quote(current_url, safe='')}"
    public_url = _landing_href(org_slug=org.slug or str(org.id), docs_root="/docs")
    scope_label_safe = escape(scope_label)
    scope_title_safe = escape(scope_title)
    message_safe = escape(message)
    sign_in_url_safe = escape(sign_in_url, quote=True)
    public_url_safe = escape(public_url, quote=True)
    html = f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{scope_title}</title>
  <style>
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: #f8fafc;
      color: #0f172a;
    }}
    .wrap {{
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }}
    .card {{
      width: min(640px, 100%);
      background: #ffffff;
      border: 1px solid #e2e8f0;
      border-radius: 16px;
      padding: 28px;
      box-shadow: 0 8px 30px rgba(15, 23, 42, 0.06);
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 1px solid #cbd5e1;
      color: #334155;
      border-radius: 999px;
      padding: 6px 12px;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-bottom: 12px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 32px;
      line-height: 1.15;
    }}
    p {{
      margin: 0 0 18px;
      color: #475569;
      font-size: 16px;
      line-height: 1.5;
    }}
    .actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 10px;
      text-decoration: none;
      font-weight: 600;
      padding: 10px 14px;
      border: 1px solid #cbd5e1;
      color: #0f172a;
      background: #fff;
    }}
    .btn.primary {{
      border-color: #0f766e;
      background: #14b8a6;
      color: #ffffff;
    }}
  </style>
</head>
<body>
  <main class="wrap">
    <section class="card">
      <div class="badge">{scope_label_safe} access required</div>
      <h1>{scope_title_safe}</h1>
      <p>{message_safe}</p>
      <div class="actions">
        <a class="btn primary" href="{sign_in_url_safe}">Sign in</a>
        <a class="btn" href="{public_url_safe}">View public docs</a>
      </div>
    </section>
  </main>
</body>
</html>
"""
    return HTMLResponse(content=html, status_code=403)


def _docs_landing_impl(
    org_slug: str,
    request: Request,
    product: str | None,
    version: str | None,
    audience: str | None,
    docs_root: str,
    access_scope: str | None,
) -> HTMLResponse:
    db = _get_db()
    try:
        org = _lookup_org(org_slug, db)
        hierarchy_mode = _org_hierarchy_mode(org)
        viewer_scope = build_viewer_scope(
            db,
            org.id,
            _resolve_request_user_with_optional_query_token(
                request,
                db,
                allow_query_token=access_scope is not None,
            ),
        )
        if access_scope == "internal":
            if not viewer_scope.is_org_member:
                return _access_required_html(
                    org=org,
                    docs_root=docs_root,
                    required_scope="internal",
                    request=request,
                )
            cookie_redirect = _bootstrap_docs_cookie_redirect_if_needed(request)
            if cookie_redirect is not None:
                return cookie_redirect
        elif access_scope == "external":
            if not (viewer_scope.is_org_member or viewer_scope.is_external_allowed):
                return _access_required_html(
                    org=org,
                    docs_root=docs_root,
                    required_scope="external",
                    request=request,
                )
            cookie_redirect = _bootstrap_docs_cookie_redirect_if_needed(request)
            if cookie_redirect is not None:
                return cookie_redirect
        effective_audience, template_audience = _resolve_route_audience(audience, docs_root)

        primary = org.primary_color or DEFAULT_PRIMARY_COLOR
        all_top_nodes = _build_top_nodes(org.id, db, viewer_scope, effective_audience)
        cards = _build_top_section_cards(org.id, db, viewer_scope, effective_audience)
        selected_product_node = None
        landing_versions: list[dict] = []
        landing_selected_version: str | None = None
        if hierarchy_mode == "product":
            if product:
                selected_product_node = next((node for node in all_top_nodes if node.get("slug") == product), None)
            if selected_product_node is None and all_top_nodes:
                selected_product_node = all_top_nodes[0]
            version_nodes = (
                _build_product_version_nodes(
                    org_id=org.id,
                    product_section_id=selected_product_node.get("id"),
                    db=db,
                    viewer_scope=viewer_scope,
                    audience=effective_audience,
                )
                if selected_product_node and selected_product_node.get("id")
                else []
            )
            selected_version_node = None
            if version_nodes:
                landing_versions = [
                    {
                        "id": node.get("id"),
                        "name": node.get("name"),
                        "slug": node.get("slug"),
                    }
                    for node in version_nodes
                ]
                if version:
                    selected_version_node = next((node for node in version_nodes if node.get("slug") == version), None)
                if selected_version_node is None:
                    base_page_id, base_page_slug = _find_first_page_excluding_versions(selected_product_node)
                    if not (base_page_id and base_page_slug):
                        selected_version_node = version_nodes[0]
                landing_selected_version = selected_version_node.get("slug") if selected_version_node else None
            selected_content_node = selected_version_node or selected_product_node
            selected_content_node = _flatten_same_name_wrapper(selected_content_node)
            landing_groups = _build_landing_groups(selected_content_node)
            landing_get_started_slug = selected_content_node.get("first_page_slug") if selected_content_node else None
            landing_get_started_id = selected_content_node.get("first_page_id") if selected_content_node else None
        else:
            landing_groups = _build_flat_landing_groups(all_top_nodes)
            first_page = None
            for node in all_top_nodes:
                normalized = _flatten_same_name_wrapper(node) or node
                first_page = normalized.get("first_page_id"), normalized.get("first_page_slug")
                if first_page[0] and first_page[1]:
                    break
            landing_get_started_id = first_page[0] if first_page else None
            landing_get_started_slug = first_page[1] if first_page else None
        search_index = _build_search_index(all_top_nodes)

        ctx = _base_ctx(
            org,
            [],
            cards,
            primary,
            hierarchy_mode,
            template_audience,
            docs_root=docs_root,
        )
        ctx["landing_products"] = cards if hierarchy_mode == "product" else []
        ctx["landing_selected_product"] = selected_product_node.get("slug") if selected_product_node else None
        ctx["landing_versions"] = landing_versions
        ctx["landing_selected_version"] = landing_selected_version
        ctx["landing_base_label"] = (selected_product_node.get("name") if selected_product_node else "Original")
        ctx["landing_groups"] = landing_groups
        ctx["landing_get_started_slug"] = landing_get_started_slug
        ctx["landing_get_started_id"] = landing_get_started_id
        ctx["landing_search_index"] = search_index

        html = _render(
            "docs_home.html",
            **ctx,
            page_title=org.name,
            section_name=None,
            current_page=None,
            current_page_slug=None,
            page_html=None,
        )
        return HTMLResponse(content=html)
    finally:
        _close_db(db)


@router.get("/docs/{org_slug}", response_class=HTMLResponse)
def docs_landing(
    org_slug: str,
    request: Request,
    product: str | None = Query(default=None),
    version: str | None = Query(default=None),
    audience: str | None = Query(default=None),
) -> HTMLResponse:
    """Org documentation landing page — shows section cards."""
    return _docs_landing_impl(
        org_slug=org_slug,
        request=request,
        product=product,
        version=version,
        audience=audience,
        docs_root="/docs",
        access_scope=None,
    )


@router.get("/external-docs/{org_slug}", response_class=HTMLResponse)
def external_docs_landing(
    org_slug: str,
    request: Request,
    product: str | None = Query(default=None),
    version: str | None = Query(default=None),
) -> HTMLResponse:
    """External docs landing — invitation-only external visibility."""
    return _docs_landing_impl(
        org_slug=org_slug,
        request=request,
        product=product,
        version=version,
        audience="external",
        docs_root="/external-docs",
        access_scope="external",
    )


@router.get("/internal-docs/{org_slug}", response_class=HTMLResponse)
def internal_docs_landing(
    org_slug: str,
    request: Request,
    product: str | None = Query(default=None),
    version: str | None = Query(default=None),
) -> HTMLResponse:
    """Organization internal docs landing — internal-only visibility for org members."""
    return _docs_landing_impl(
        org_slug=org_slug,
        request=request,
        product=product,
        version=version,
        audience="internal",
        docs_root="/internal-docs",
        access_scope="internal",
    )


def _docs_search_impl(
    org_slug: str,
    request: Request,
    q: str,
    audience: str | None,
    docs_root: str,
    access_scope: str | None,
) -> JSONResponse:
    db = _get_db()
    try:
        org = _lookup_org(org_slug, db)
        request_user = _resolve_request_user_with_optional_query_token(
            request,
            db,
            allow_query_token=access_scope is not None,
        )
        viewer_scope = build_viewer_scope(
            db,
            org.id,
            request_user,
        )
        if access_scope == "internal":
            _ensure_internal_org_member(viewer_scope)
            cookie_redirect = _bootstrap_docs_cookie_redirect_if_needed(request)
            if cookie_redirect is not None:
                return cookie_redirect
        elif access_scope == "external":
            _ensure_external_docs_access(viewer_scope)
            cookie_redirect = _bootstrap_docs_cookie_redirect_if_needed(request)
            if cookie_redirect is not None:
                return cookie_redirect
        effective_audience, _ = _resolve_route_audience(audience, docs_root)
        q_stripped = q.strip().lower()
        if not q_stripped:
            return JSONResponse({"results": []})

        pages = (
            db.query(Page)
            .filter(Page.organization_id == org.id, Page.is_published == True)
            .all()
        )
        sections_by_id = {
            s.id: s
            for s in db.query(Section).filter(Section.organization_id == org.id).all()
        }

        results = []
        for page in pages:
            section = sections_by_id.get(page.section_id) if page.section_id else None
            if not _is_page_visible(
                page,
                section.visibility if section else "public",
                viewer_scope,
                effective_audience,
            ):
                continue

            title_lower = page.title.lower()
            if q_stripped in title_lower:
                score = 2 if title_lower.startswith(q_stripped) else 1
            elif page.published_html and q_stripped in page.published_html.lower():
                score = 0.5
            else:
                continue

            results.append({
                "page_id": page.id,
                "title": page.title,
                "slug": page.slug,
                "section_name": section.name if section else None,
                "score": score,
            })

        results.sort(key=lambda r: r["score"], reverse=True)
        return JSONResponse({
            "results": [
                {
                    "page_id": r["page_id"],
                    "title": r["title"],
                    "slug": r["slug"],
                    "section_name": r["section_name"],
                }
                for r in results[:12]
            ]
        })
    finally:
        _close_db(db)


@router.get("/docs/{org_slug}/search")
def docs_search(
    org_slug: str,
    request: Request,
    q: str = Query(default=""),
    audience: str | None = Query(default=None),
) -> JSONResponse:
    """Full-text search over published pages for an org."""
    return _docs_search_impl(
        org_slug=org_slug,
        request=request,
        q=q,
        audience=audience,
        docs_root="/docs",
        access_scope=None,
    )


@router.get("/external-docs/{org_slug}/search")
def external_docs_search(
    org_slug: str,
    request: Request,
    q: str = Query(default=""),
) -> JSONResponse:
    """Search endpoint for invitation-only external docs."""
    return _docs_search_impl(
        org_slug=org_slug,
        request=request,
        q=q,
        audience="external",
        docs_root="/external-docs",
        access_scope="external",
    )


@router.get("/internal-docs/{org_slug}/search")
def internal_docs_search(
    org_slug: str,
    request: Request,
    q: str = Query(default=""),
) -> JSONResponse:
    """Internal docs search — internal-only visibility for org members."""
    return _docs_search_impl(
        org_slug=org_slug,
        request=request,
        q=q,
        audience="internal",
        docs_root="/internal-docs",
        access_scope="internal",
    )


def _render_docs_page(
    org: Organization,
    page: Page,
    db: Session,
    viewer_scope: ViewerScope,
    request_user: User | None = None,
    audience: str | None = None,
    audience_for_links: str | None = None,
    docs_root: str = "/docs",
    version_slug: str | None = None,
) -> HTMLResponse:
    section = db.get(Section, page.section_id) if page.section_id else None
    primary = org.primary_color or DEFAULT_PRIMARY_COLOR
    hierarchy_mode = _org_hierarchy_mode(org)

    all_top_nodes = _build_top_nodes(org.id, db, viewer_scope, audience)
    cards = _build_top_section_cards(org.id, db, viewer_scope, audience)
    nav_meta = _resolve_page_navigation(
        all_top_nodes,
        page.slug,
        page_id=page.id,
        hierarchy_mode=hierarchy_mode,
        version_slug=version_slug,
    )
    if hierarchy_mode == "product" and nav_meta.get("product_header"):
        product_id = nav_meta["product_header"].get("id")
        if product_id:
            nav_meta["top_versions"] = _build_product_version_nodes(
                org_id=org.id,
                product_section_id=product_id,
                db=db,
                viewer_scope=viewer_scope,
                audience=audience,
            )
            if version_slug and any(v.get("slug") == version_slug for v in nav_meta["top_versions"]):
                nav_meta["current_version_slug"] = version_slug

    page_html = _clean_gdoc_html(page.published_html or "")
    page_html = _rewrite_page_links(
        page_html,
        org=org,
        db=db,
        docs_root=docs_root,
        audience_for_links=audience_for_links,
    )
    page_last_updated = _format_last_updated(page)

    feedback_rows = (
        db.query(PageFeedback.vote, func.count(PageFeedback.id))
        .filter(
            PageFeedback.organization_id == org.id,
            PageFeedback.page_id == page.id,
            PageFeedback.vote.in_(["up", "down"]),
        )
        .group_by(PageFeedback.vote)
        .all()
    )
    feedback_summary = {"up": 0, "down": 0}
    for vote, count in feedback_rows:
        if vote in feedback_summary:
            feedback_summary[vote] = int(count or 0)
    feedback_summary["total"] = feedback_summary["up"] + feedback_summary["down"]

    ctx = _base_ctx(
        org,
        nav_meta["nav_sections"],
        cards,
        primary,
        hierarchy_mode,
        audience_for_links,
        docs_root=docs_root,
    )
    ctx["top_tabs"] = nav_meta["top_tabs"]
    ctx["top_versions"] = nav_meta["top_versions"]
    ctx["current_tab_slug"] = nav_meta["current_tab_slug"]
    ctx["current_version_slug"] = nav_meta["current_version_slug"]
    ctx["product_header"] = nav_meta["product_header"]
    ctx["base_version_href"] = None
    ctx["base_version_label"] = None
    base_page_id = None
    base_page_slug = None
    if hierarchy_mode == "product" and nav_meta.get("product_header"):
        product_id = nav_meta["product_header"].get("id")
        if product_id:
            product_node = next((node for node in all_top_nodes if node.get("id") == product_id), None)
            base_page_id, base_page_slug = _find_first_page_excluding_versions(product_node)
    if base_page_id and base_page_slug:
        ctx["base_version_href"] = (
            f"{docs_root}/{org.slug or org.id}/{base_page_slug}"
            f"{_audience_suffix_for_links(docs_root, audience_for_links)}"
        )
        ctx["base_version_label"] = nav_meta["product_header"].get("name") or "Original"
    ctx["page_last_updated"] = page_last_updated
    ctx["feedback_summary"] = feedback_summary
    ctx["viewer_signed_in"] = bool(request_user)
    ctx["viewer_email"] = request_user.email if request_user and request_user.email else None
    ctx["viewer_name"] = _viewer_display_name(request_user)
    ctx["can_comment"] = bool(viewer_scope.is_org_member or viewer_scope.is_external_allowed)
    ctx["can_feedback"] = True

    html = _render(
        "docs.html",
        **ctx,
        page_title=page.title,
        section_name=section.name if section else None,
        current_page={
            "id": page.id,
            "title": page.title,
            "slug": page.slug,
            "section_name": section.name if section else None,
        },
        current_page_slug=page.slug,
        page_html=page_html,
    )
    return HTMLResponse(content=html)


def _docs_page_by_id_impl(
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
    version: str | None,
    audience: str | None,
    docs_root: str,
    access_scope: str | None,
) -> HTMLResponse:
    """Serve a single published page by id+slug (canonical, product-safe URL)."""
    db = _get_db()
    try:
        org = _lookup_org(org_slug, db)
        request_user = _resolve_request_user_with_optional_query_token(
            request,
            db,
            allow_query_token=access_scope is not None,
        )
        viewer_scope = build_viewer_scope(
            db,
            org.id,
            request_user,
        )
        if access_scope == "internal":
            if not viewer_scope.is_org_member:
                return _access_required_html(
                    org=org,
                    docs_root=docs_root,
                    required_scope="internal",
                    request=request,
                )
            cookie_redirect = _bootstrap_docs_cookie_redirect_if_needed(request)
            if cookie_redirect is not None:
                return cookie_redirect
        elif access_scope == "external":
            if not (viewer_scope.is_org_member or viewer_scope.is_external_allowed):
                return _access_required_html(
                    org=org,
                    docs_root=docs_root,
                    required_scope="external",
                    request=request,
                )
            cookie_redirect = _bootstrap_docs_cookie_redirect_if_needed(request)
            if cookie_redirect is not None:
                return cookie_redirect
        effective_audience, template_audience = _resolve_route_audience(audience, docs_root)
        page = (
            db.query(Page)
            .filter(
                Page.organization_id == org.id,
                Page.id == page_id,
                Page.is_published == True,
            )
            .first()
        )
        if not page:
            redirect = _lookup_redirect_for_page_id(
                organization_id=org.id,
                page_id=page_id,
                page_slug=page_slug,
                db=db,
            )
            if redirect:
                target_url = _resolve_redirect_target_url(
                    redirect=redirect,
                    org=org,
                    db=db,
                    viewer_scope=viewer_scope,
                    audience=effective_audience,
                    docs_root=docs_root,
                    audience_for_links=template_audience,
                )
                code = redirect.status_code if redirect.status_code in {301, 302, 307, 308} else 307
                return RedirectResponse(url=target_url, status_code=code)
            raise HTTPException(status_code=404, detail="Page not found or not published")
        section = db.get(Section, page.section_id) if page.section_id else None
        if not _is_page_visible(
            page,
            section.visibility if section else "public",
            viewer_scope,
            effective_audience,
        ):
            raise HTTPException(status_code=404, detail="Page not found or not published")
        if page.slug != page_slug:
            return RedirectResponse(
                url=_page_fallback_href(
                    page,
                    org_slug=org_slug,
                    docs_root=docs_root,
                    audience_for_links=template_audience,
                ),
                status_code=307,
            )
        visible_with_same_slug = (
            db.query(Page)
            .filter(
                Page.organization_id == org.id,
                Page.slug == page.slug,
                Page.is_published == True,
            )
            .all()
        )
        section_ids = {candidate.section_id for candidate in visible_with_same_slug if candidate.section_id is not None}
        candidate_sections_by_id = {
            section.id: section
            for section in db.query(Section).filter(Section.id.in_(section_ids)).all()
        } if section_ids else {}
        visible_same_slug_count = 0
        for candidate in visible_with_same_slug:
            candidate_section = candidate_sections_by_id.get(candidate.section_id) if candidate.section_id else None
            if _is_page_visible(
                candidate,
                candidate_section.visibility if candidate_section else "public",
                viewer_scope,
                effective_audience,
            ):
                visible_same_slug_count += 1
                if visible_same_slug_count > 1:
                    break
        if visible_same_slug_count <= 1:
            return RedirectResponse(
                url=_canonical_page_href(
                    page,
                    org_slug=org_slug,
                    docs_root=docs_root,
                    audience_for_links=template_audience,
                ),
                status_code=307,
            )
        return _render_docs_page(
            org,
            page,
            db,
            viewer_scope,
            request_user,
            effective_audience,
            audience_for_links=template_audience,
            docs_root=docs_root,
            version_slug=version,
        )
    finally:
        _close_db(db)


@router.get("/docs/{org_slug}/p/{page_id}/{page_slug}", response_class=HTMLResponse)
def docs_page_by_id(
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
    version: str | None = Query(default=None),
    audience: str | None = Query(default=None),
) -> HTMLResponse:
    return _docs_page_by_id_impl(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        version=version,
        audience=audience,
        docs_root="/docs",
        access_scope=None,
    )


@router.get("/external-docs/{org_slug}/p/{page_id}/{page_slug}", response_class=HTMLResponse)
def external_docs_page_by_id(
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
    version: str | None = Query(default=None),
) -> HTMLResponse:
    return _docs_page_by_id_impl(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        version=version,
        audience="external",
        docs_root="/external-docs",
        access_scope="external",
    )


@router.get("/internal-docs/{org_slug}/p/{page_id}/{page_slug}", response_class=HTMLResponse)
def internal_docs_page_by_id(
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
    version: str | None = Query(default=None),
) -> HTMLResponse:
    return _docs_page_by_id_impl(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        version=version,
        audience="internal",
        docs_root="/internal-docs",
        access_scope="internal",
    )


def _docs_page_impl(
    org_slug: str,
    page_slug: str,
    request: Request,
    version: str | None,
    audience: str | None,
    docs_root: str,
    access_scope: str | None,
) -> HTMLResponse:
    """Legacy slug route; redirects only when slug is ambiguous."""
    db = _get_db()
    try:
        org = _lookup_org(org_slug, db)
        request_user = _resolve_request_user_with_optional_query_token(
            request,
            db,
            allow_query_token=access_scope is not None,
        )
        viewer_scope = build_viewer_scope(
            db,
            org.id,
            request_user,
        )
        if access_scope == "internal":
            if not viewer_scope.is_org_member:
                return _access_required_html(
                    org=org,
                    docs_root=docs_root,
                    required_scope="internal",
                    request=request,
                )
            cookie_redirect = _bootstrap_docs_cookie_redirect_if_needed(request)
            if cookie_redirect is not None:
                return cookie_redirect
        elif access_scope == "external":
            if not (viewer_scope.is_org_member or viewer_scope.is_external_allowed):
                return _access_required_html(
                    org=org,
                    docs_root=docs_root,
                    required_scope="external",
                    request=request,
                )
            cookie_redirect = _bootstrap_docs_cookie_redirect_if_needed(request)
            if cookie_redirect is not None:
                return cookie_redirect
        effective_audience, template_audience = _resolve_route_audience(audience, docs_root)
        pages = (
            db.query(Page)
            .filter(
                Page.organization_id == org.id,
                Page.slug == page_slug,
                Page.is_published == True,
            )
            .order_by(Page.id.asc())
            .all()
        )
        if not pages:
            redirect = _lookup_redirect_for_slug(
                organization_id=org.id,
                page_slug=page_slug,
                db=db,
            )
            if redirect:
                target_url = _resolve_redirect_target_url(
                    redirect=redirect,
                    org=org,
                    db=db,
                    viewer_scope=viewer_scope,
                    audience=effective_audience,
                    docs_root=docs_root,
                    audience_for_links=template_audience,
                )
                code = redirect.status_code if redirect.status_code in {301, 302, 307, 308} else 307
                return RedirectResponse(url=target_url, status_code=code)
            raise HTTPException(status_code=404, detail="Page not found or not published")
        section_ids = {p.section_id for p in pages if p.section_id is not None}
        sections_by_id = {
            s.id: s for s in db.query(Section).filter(Section.id.in_(section_ids)).all()
        } if section_ids else {}
        visible_pages = [
            p
            for p in pages
            if _is_page_visible(
                p,
                sections_by_id.get(p.section_id).visibility if sections_by_id.get(p.section_id) else "public",
                viewer_scope,
                effective_audience,
            )
        ]
        if not visible_pages:
            raise HTTPException(status_code=404, detail="Page not found or not published")

        page = visible_pages[0]
        if len(visible_pages) > 1:
            return RedirectResponse(
                url=_page_fallback_href(
                    page,
                    org_slug=org_slug,
                    docs_root=docs_root,
                    audience_for_links=template_audience,
                ),
                status_code=307,
            )
        return _render_docs_page(
            org,
            page,
            db,
            viewer_scope,
            request_user,
            effective_audience,
            audience_for_links=template_audience,
            docs_root=docs_root,
            version_slug=version,
        )
    finally:
        _close_db(db)


@router.get("/docs/{org_slug}/{page_slug}", response_class=HTMLResponse)
def docs_page(
    org_slug: str,
    page_slug: str,
    request: Request,
    version: str | None = Query(default=None),
    audience: str | None = Query(default=None),
) -> HTMLResponse:
    return _docs_page_impl(
        org_slug=org_slug,
        page_slug=page_slug,
        request=request,
        version=version,
        audience=audience,
        docs_root="/docs",
        access_scope=None,
    )


@router.get("/external-docs/{org_slug}/{page_slug}", response_class=HTMLResponse)
def external_docs_page(
    org_slug: str,
    page_slug: str,
    request: Request,
    version: str | None = Query(default=None),
) -> HTMLResponse:
    return _docs_page_impl(
        org_slug=org_slug,
        page_slug=page_slug,
        request=request,
        version=version,
        audience="external",
        docs_root="/external-docs",
        access_scope="external",
    )


@router.get("/internal-docs/{org_slug}/{page_slug}", response_class=HTMLResponse)
def internal_docs_page(
    org_slug: str,
    page_slug: str,
    request: Request,
    version: str | None = Query(default=None),
) -> HTMLResponse:
    return _docs_page_impl(
        org_slug=org_slug,
        page_slug=page_slug,
        request=request,
        version=version,
        audience="internal",
        docs_root="/internal-docs",
        access_scope="internal",
    )


def _load_page_for_engagement(
    *,
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
    docs_root: str,
    access_scope: str | None,
    audience: str | None = None,
) -> tuple[Session, Organization, Page, ViewerScope, User | None]:
    """Resolve org/page and enforce same visibility rules as rendered docs pages."""
    db = _get_db()
    org = _lookup_org(org_slug, db)
    request_user = _resolve_request_user_with_optional_query_token(
        request,
        db,
        allow_query_token=access_scope is not None,
    )
    viewer_scope = build_viewer_scope(db, org.id, request_user)

    if access_scope == "internal":
        _ensure_internal_org_member(viewer_scope)
    elif access_scope == "external":
        _ensure_external_docs_access(viewer_scope)

    effective_audience, _ = _resolve_route_audience(audience, docs_root)
    page = (
        db.query(Page)
        .filter(
            Page.organization_id == org.id,
            Page.id == page_id,
            Page.is_published == True,
        )
        .first()
    )
    if not page or page.slug != page_slug:
        _close_db(db)
        raise HTTPException(status_code=404, detail="Page not found")

    section = db.get(Section, page.section_id) if page.section_id else None
    if not _is_page_visible(
        page,
        section.visibility if section else "public",
        viewer_scope,
        effective_audience,
    ):
        _close_db(db)
        raise HTTPException(status_code=404, detail="Page not found")
    return db, org, page, viewer_scope, request_user


def _feedback_summary(db: Session, organization_id: int, page_id: int) -> dict[str, int]:
    rows = (
        db.query(PageFeedback.vote, func.count(PageFeedback.id))
        .filter(
            PageFeedback.organization_id == organization_id,
            PageFeedback.page_id == page_id,
            PageFeedback.vote.in_(["up", "down"]),
        )
        .group_by(PageFeedback.vote)
        .all()
    )
    summary = {"up": 0, "down": 0}
    for vote, count in rows:
        if vote in summary:
            summary[vote] = int(count or 0)
    summary["total"] = summary["up"] + summary["down"]
    return summary


def _serialize_comment(comment: PageComment) -> dict:
    return {
        "id": comment.id,
        "display_name": comment.display_name or "User",
        "user_email": comment.user_email,
        "body": comment.body,
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
    }


def _engagement_impl(
    *,
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
    docs_root: str,
    access_scope: str | None,
    audience: str | None = None,
) -> JSONResponse:
    db, org, page, viewer_scope, request_user = _load_page_for_engagement(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        docs_root=docs_root,
        access_scope=access_scope,
        audience=audience,
    )
    try:
        comments = (
            db.query(PageComment)
            .filter(
                PageComment.organization_id == org.id,
                PageComment.page_id == page.id,
                PageComment.is_deleted == False,
            )
            .order_by(PageComment.created_at.desc())
            .limit(100)
            .all()
        )
        comments_payload = [_serialize_comment(c) for c in reversed(comments)]
        return JSONResponse(
            {
                "last_updated": _format_last_updated(page),
                "feedback": _feedback_summary(db, org.id, page.id),
                "comments": comments_payload,
                "permissions": {
                    "can_comment": bool(
                        request_user and (viewer_scope.is_org_member or viewer_scope.is_external_allowed)
                    ),
                    "signed_in": bool(request_user),
                },
            }
        )
    finally:
        _close_db(db)


async def _submit_feedback_impl(
    *,
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
    docs_root: str,
    access_scope: str | None,
    audience: str | None = None,
) -> JSONResponse:
    db, org, page, _, request_user = _load_page_for_engagement(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        docs_root=docs_root,
        access_scope=access_scope,
        audience=audience,
    )
    try:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        vote = str((payload or {}).get("vote") or "").strip().lower()
        if vote not in {"up", "down"}:
            raise HTTPException(status_code=400, detail="vote must be 'up' or 'down'")

        message = str((payload or {}).get("message") or "").strip()
        if len(message) > 2000:
            raise HTTPException(status_code=400, detail="Feedback message is too long")

        source = docs_root.strip("/").split("/", 1)[0] or "docs"
        db.add(
            PageFeedback(
                organization_id=org.id,
                page_id=page.id,
                user_id=request_user.id if request_user else None,
                user_email=request_user.email if request_user else None,
                vote=vote,
                message=message or None,
                source=source,
            )
        )
        db.commit()
        return JSONResponse(
            {
                "status": "ok",
                "feedback": _feedback_summary(db, org.id, page.id),
            },
            status_code=201,
        )
    finally:
        _close_db(db)


async def _submit_comment_impl(
    *,
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
    docs_root: str,
    access_scope: str | None,
    audience: str | None = None,
) -> JSONResponse:
    db, org, page, viewer_scope, request_user = _load_page_for_engagement(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        docs_root=docs_root,
        access_scope=access_scope,
        audience=audience,
    )
    try:
        if not request_user or not (viewer_scope.is_org_member or viewer_scope.is_external_allowed):
            raise HTTPException(status_code=401, detail="Sign in with access permissions to comment")

        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

        body = str((payload or {}).get("body") or "").strip()
        if not body:
            raise HTTPException(status_code=400, detail="Comment body is required")
        if len(body) > 5000:
            raise HTTPException(status_code=400, detail="Comment is too long")

        source = docs_root.strip("/").split("/", 1)[0] or "docs"
        comment = PageComment(
            organization_id=org.id,
            page_id=page.id,
            user_id=request_user.id,
            user_email=request_user.email,
            display_name=_viewer_display_name(request_user),
            body=body,
            source=source,
        )
        db.add(comment)
        db.commit()
        db.refresh(comment)
        return JSONResponse({"status": "ok", "comment": _serialize_comment(comment)}, status_code=201)
    finally:
        _close_db(db)


@router.get("/docs/{org_slug}/p/{page_id}/{page_slug}/engagement")
def docs_page_engagement(
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
    audience: str | None = Query(default=None),
) -> JSONResponse:
    return _engagement_impl(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        docs_root="/docs",
        access_scope=None,
        audience=audience,
    )


@router.post("/docs/{org_slug}/p/{page_id}/{page_slug}/feedback")
async def docs_page_feedback(
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
    audience: str | None = Query(default=None),
) -> JSONResponse:
    return await _submit_feedback_impl(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        docs_root="/docs",
        access_scope=None,
        audience=audience,
    )


@router.post("/docs/{org_slug}/p/{page_id}/{page_slug}/comments")
async def docs_page_comments(
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
    audience: str | None = Query(default=None),
) -> JSONResponse:
    return await _submit_comment_impl(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        docs_root="/docs",
        access_scope=None,
        audience=audience,
    )


@router.get("/internal-docs/{org_slug}/p/{page_id}/{page_slug}/engagement")
def internal_docs_page_engagement(
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
) -> JSONResponse:
    return _engagement_impl(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        docs_root="/internal-docs",
        access_scope="internal",
        audience="internal",
    )


@router.post("/internal-docs/{org_slug}/p/{page_id}/{page_slug}/feedback")
async def internal_docs_page_feedback(
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
) -> JSONResponse:
    return await _submit_feedback_impl(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        docs_root="/internal-docs",
        access_scope="internal",
        audience="internal",
    )


@router.post("/internal-docs/{org_slug}/p/{page_id}/{page_slug}/comments")
async def internal_docs_page_comments(
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
) -> JSONResponse:
    return await _submit_comment_impl(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        docs_root="/internal-docs",
        access_scope="internal",
        audience="internal",
    )


@router.get("/external-docs/{org_slug}/p/{page_id}/{page_slug}/engagement")
def external_docs_page_engagement(
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
) -> JSONResponse:
    return _engagement_impl(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        docs_root="/external-docs",
        access_scope="external",
        audience="external",
    )


@router.post("/external-docs/{org_slug}/p/{page_id}/{page_slug}/feedback")
async def external_docs_page_feedback(
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
) -> JSONResponse:
    return await _submit_feedback_impl(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        docs_root="/external-docs",
        access_scope="external",
        audience="external",
    )


@router.post("/external-docs/{org_slug}/p/{page_id}/{page_slug}/comments")
async def external_docs_page_comments(
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
) -> JSONResponse:
    return await _submit_comment_impl(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        docs_root="/external-docs",
        access_scope="external",
        audience="external",
    )


# ---------------------------------------------------------------------------
# XML Sitemap — public docs only
# ---------------------------------------------------------------------------

@router.get("/docs/{org_slug}/sitemap.xml")
async def public_docs_sitemap(org_slug: str, request: Request):
    """Return an XML sitemap for all published public pages in this org."""
    from fastapi.responses import Response as FastResponse
    from html import escape as _escape

    with SessionLocal() as db:
        org = db.query(Organization).filter(Organization.slug == org_slug).first()
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        pages = (
            db.query(Page)
            .filter(
                Page.organization_id == org.id,
                Page.is_published == True,  # noqa: E712
            )
            .order_by(Page.updated_at.desc())
            .all()
        )

    base = str(request.base_url).rstrip("/")
    docs_base = f"{base}/docs/{org_slug}"

    urls = [f"  <url><loc>{docs_base}</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>"]
    for page in pages:
        if not page.slug:
            continue
        loc = f"{docs_base}/{_escape(page.slug)}"
        lastmod = ""
        if page.updated_at:
            lastmod = f"<lastmod>{page.updated_at.strftime('%Y-%m-%d')}</lastmod>"
        urls.append(f"  <url><loc>{loc}</loc>{lastmod}<changefreq>weekly</changefreq><priority>0.8</priority></url>")

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    xml += "\n".join(urls)
    xml += "\n</urlset>"

    return FastResponse(content=xml, media_type="application/xml")
