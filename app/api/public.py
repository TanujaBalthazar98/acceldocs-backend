"""Public docs — serve rendered HTML documentation directly from FastAPI.

Routes:
  GET /docs/{org_slug}                  — landing page (section cards)
  GET /docs/{org_slug}/{page_slug}      — single page view
  GET /docs/{org_slug}/{section_slug}/{page_slug}  — page inside explicit section
"""

import logging
import re
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.auth.routes import DOCS_SESSION_COOKIE
from app.middleware.auth import get_current_user as _get_auth_user
from app.models import Organization, Page, Section, User
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
    return SessionLocal()


def _org_initials(name: str) -> str:
    words = name.strip().split()
    if len(words) >= 2:
        return (words[0][0] + words[-1][0]).upper()
    return name[:2].upper()


def _org_hierarchy_mode(org: Organization) -> str:
    return "flat" if getattr(org, "hierarchy_mode", None) == "flat" else "product"


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
    - /docs is strict public by default.
    - /docs can opt into external audience via ?audience=external.
    """
    normalized_docs_root = docs_root.rstrip("/") or "/docs"
    if normalized_docs_root == "/internal-docs":
        return "internal", None

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

    group_nodes = normalized.get("children", [])
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


def _resolve_page_navigation(
    all_top_nodes: list[dict],
    page_slug: str,
    page_id: int | None = None,
    hierarchy_mode: str = "product",
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
            }

        return {
            "top_tabs": [],
            "nav_sections": all_top_nodes,
            "current_tab_slug": None,
            "product_header": None,
        }

    # Product hierarchy mode (default).
    if not active_top_node:
        return {
            "top_tabs": [],
            "nav_sections": all_top_nodes,
            "current_tab_slug": None,
            "product_header": None,
        }

    # Legacy shape: single root node explicitly marked as tab.
    if (active_top_node.get("section_type") or "section") == "tab":
        return {
            "top_tabs": [active_top_node],
            "nav_sections": [active_top_node],
            "current_tab_slug": active_top_node.get("slug"),
            "product_header": active_top_node,
        }

    product_children = active_top_node.get("children", [])
    tab_children = [
        child for child in product_children
        if (child.get("section_type") or "section") == "tab"
    ]
    non_tab_children = [
        child for child in product_children
        if (child.get("section_type") or "section") != "tab"
    ]
    product_pages = active_top_node.get("pages", [])

    # If this product has explicit tab children, use strict product + tabs layout.
    if tab_children:
        active_tab = next(
            (tab for tab in tab_children if _find_page_path(tab, page_slug=page_slug, page_id=page_id)),
            None,
        )
        docs_node = _build_docs_node(f"product-{active_top_node.get('id')}", product_pages, non_tab_children)
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
        }

    # Legacy fallback for non-typed structures.
    if len(all_top_nodes) == 1:
        only_node = all_top_nodes[0]
        normalized_only = _flatten_same_name_wrapper(only_node) or only_node
        return {
            "top_tabs": [],
            "nav_sections": [normalized_only],
            "current_tab_slug": None,
            "product_header": normalized_only,
        }

    # Multiple top-level sections represent products, not tabs.
    normalized_active = _flatten_same_name_wrapper(active_top_node) or active_top_node
    return {
        "top_tabs": [],
        "nav_sections": [normalized_active] if normalized_active else all_top_nodes,
        "current_tab_slug": None,
        "product_header": normalized_active,
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
    elif template_audience == "external":
        docs_visibility_label = "External"
    elif template_audience == "all":
        docs_visibility_label = "All"
    else:
        docs_visibility_label = "Public"
    return {
        "org_name": org.name,
        "org_slug": org.slug or str(org.id),
        "org_logo": org.logo_url,
        "org_initials": _org_initials(org.name),
        "org_tagline": org.tagline,
        "primary_color": primary_color,
        "nav_sections": nav_sections,
        "top_sections": top_sections,
        "top_tabs": [],
        "current_tab_slug": None,
        "product_header": None,
        "landing_products": [],
        "landing_selected_product": None,
        "landing_groups": [],
        "landing_get_started_slug": None,
        "landing_get_started_id": None,
        "landing_search_index": [],
        "hierarchy_mode": hierarchy_mode,
        "audience": template_audience,
        "audience_suffix": audience_suffix,
        "docs_root": normalized_docs_root,
        "docs_mode": "internal" if normalized_docs_root == "/internal-docs" else "public",
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


def _docs_landing_impl(
    org_slug: str,
    request: Request,
    product: str | None,
    audience: str | None,
    docs_root: str,
    require_org_member: bool,
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
                allow_query_token=require_org_member,
            ),
        )
        if require_org_member:
            _ensure_internal_org_member(viewer_scope)
            cookie_redirect = _bootstrap_docs_cookie_redirect_if_needed(request)
            if cookie_redirect is not None:
                return cookie_redirect
        effective_audience, template_audience = _resolve_route_audience(audience, docs_root)

        primary = org.primary_color or DEFAULT_PRIMARY_COLOR
        all_top_nodes = _build_top_nodes(org.id, db, viewer_scope, effective_audience)
        cards = _build_top_section_cards(org.id, db, viewer_scope, effective_audience)
        selected_product_node = None
        if hierarchy_mode == "product":
            if product:
                selected_product_node = next((node for node in all_top_nodes if node.get("slug") == product), None)
            if selected_product_node is None and all_top_nodes:
                selected_product_node = all_top_nodes[0]
            selected_product_node = _flatten_same_name_wrapper(selected_product_node)
            landing_groups = _build_landing_groups(selected_product_node)
            landing_get_started_slug = selected_product_node.get("first_page_slug") if selected_product_node else None
            landing_get_started_id = selected_product_node.get("first_page_id") if selected_product_node else None
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
        db.close()


@router.get("/docs/{org_slug}", response_class=HTMLResponse)
def docs_landing(
    org_slug: str,
    request: Request,
    product: str | None = Query(default=None),
    audience: str | None = Query(default=None),
) -> HTMLResponse:
    """Org documentation landing page — shows section cards."""
    return _docs_landing_impl(
        org_slug=org_slug,
        request=request,
        product=product,
        audience=audience,
        docs_root="/docs",
        require_org_member=False,
    )


@router.get("/internal-docs/{org_slug}", response_class=HTMLResponse)
def internal_docs_landing(
    org_slug: str,
    request: Request,
    product: str | None = Query(default=None),
) -> HTMLResponse:
    """Organization internal docs landing — internal-only visibility for org members."""
    return _docs_landing_impl(
        org_slug=org_slug,
        request=request,
        product=product,
        audience="internal",
        docs_root="/internal-docs",
        require_org_member=True,
    )


def _docs_search_impl(
    org_slug: str,
    request: Request,
    q: str,
    audience: str | None,
    docs_root: str,
    require_org_member: bool,
) -> JSONResponse:
    db = _get_db()
    try:
        org = _lookup_org(org_slug, db)
        viewer_scope = build_viewer_scope(
            db,
            org.id,
            _resolve_request_user_with_optional_query_token(
                request,
                db,
                allow_query_token=require_org_member,
            ),
        )
        if require_org_member:
            _ensure_internal_org_member(viewer_scope)
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
        db.close()


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
        require_org_member=False,
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
        require_org_member=True,
    )


def _render_docs_page(
    org: Organization,
    page: Page,
    db: Session,
    viewer_scope: ViewerScope,
    audience: str | None = None,
    audience_for_links: str | None = None,
    docs_root: str = "/docs",
) -> HTMLResponse:
    section = db.get(Section, page.section_id) if page.section_id else None
    primary = org.primary_color or DEFAULT_PRIMARY_COLOR
    hierarchy_mode = _org_hierarchy_mode(org)

    all_top_nodes = _build_top_nodes(org.id, db, viewer_scope, audience)
    cards = _build_top_section_cards(org.id, db, viewer_scope, audience)
    nav_meta = _resolve_page_navigation(all_top_nodes, page.slug, page_id=page.id, hierarchy_mode=hierarchy_mode)

    page_html = _clean_gdoc_html(page.published_html or "")

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
    ctx["current_tab_slug"] = nav_meta["current_tab_slug"]
    ctx["product_header"] = nav_meta["product_header"]

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
    audience: str | None,
    docs_root: str,
    require_org_member: bool,
) -> HTMLResponse:
    """Serve a single published page by id+slug (canonical, product-safe URL)."""
    db = _get_db()
    try:
        org = _lookup_org(org_slug, db)
        viewer_scope = build_viewer_scope(
            db,
            org.id,
            _resolve_request_user_with_optional_query_token(
                request,
                db,
                allow_query_token=require_org_member,
            ),
        )
        if require_org_member:
            _ensure_internal_org_member(viewer_scope)
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
            canonical_url = f"{docs_root}/{org_slug}/p/{page.id}/{page.slug}"
            if template_audience and docs_root == "/docs":
                canonical_url += f"?audience={template_audience}"
            return RedirectResponse(url=canonical_url, status_code=307)
        return _render_docs_page(
            org,
            page,
            db,
            viewer_scope,
            effective_audience,
            audience_for_links=template_audience,
            docs_root=docs_root,
        )
    finally:
        db.close()


@router.get("/docs/{org_slug}/p/{page_id}/{page_slug}", response_class=HTMLResponse)
def docs_page_by_id(
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
    audience: str | None = Query(default=None),
) -> HTMLResponse:
    return _docs_page_by_id_impl(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        audience=audience,
        docs_root="/docs",
        require_org_member=False,
    )


@router.get("/internal-docs/{org_slug}/p/{page_id}/{page_slug}", response_class=HTMLResponse)
def internal_docs_page_by_id(
    org_slug: str,
    page_id: int,
    page_slug: str,
    request: Request,
) -> HTMLResponse:
    return _docs_page_by_id_impl(
        org_slug=org_slug,
        page_id=page_id,
        page_slug=page_slug,
        request=request,
        audience="internal",
        docs_root="/internal-docs",
        require_org_member=True,
    )


def _docs_page_impl(
    org_slug: str,
    page_slug: str,
    request: Request,
    audience: str | None,
    docs_root: str,
    require_org_member: bool,
) -> HTMLResponse:
    """Legacy slug route; redirects only when slug is ambiguous."""
    db = _get_db()
    try:
        org = _lookup_org(org_slug, db)
        viewer_scope = build_viewer_scope(
            db,
            org.id,
            _resolve_request_user_with_optional_query_token(
                request,
                db,
                allow_query_token=require_org_member,
            ),
        )
        if require_org_member:
            _ensure_internal_org_member(viewer_scope)
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
            canonical_url = f"{docs_root}/{org_slug}/p/{page.id}/{page.slug}"
            if template_audience and docs_root == "/docs":
                canonical_url += f"?audience={template_audience}"
            return RedirectResponse(url=canonical_url, status_code=307)
        return _render_docs_page(
            org,
            page,
            db,
            viewer_scope,
            effective_audience,
            audience_for_links=template_audience,
            docs_root=docs_root,
        )
    finally:
        db.close()


@router.get("/docs/{org_slug}/{page_slug}", response_class=HTMLResponse)
def docs_page(
    org_slug: str,
    page_slug: str,
    request: Request,
    audience: str | None = Query(default=None),
) -> HTMLResponse:
    return _docs_page_impl(
        org_slug=org_slug,
        page_slug=page_slug,
        request=request,
        audience=audience,
        docs_root="/docs",
        require_org_member=False,
    )


@router.get("/internal-docs/{org_slug}/{page_slug}", response_class=HTMLResponse)
def internal_docs_page(
    org_slug: str,
    page_slug: str,
    request: Request,
) -> HTMLResponse:
    return _docs_page_impl(
        org_slug=org_slug,
        page_slug=page_slug,
        request=request,
        audience="internal",
        docs_root="/internal-docs",
        require_org_member=True,
    )
