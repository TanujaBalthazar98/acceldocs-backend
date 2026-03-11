"""Public docs — serve rendered HTML documentation directly from FastAPI.

Routes:
  GET /docs/{org_slug}                  — landing page (section cards)
  GET /docs/{org_slug}/{page_slug}      — single page view
  GET /docs/{org_slug}/{section_slug}/{page_slug}  — page inside explicit section
"""

import logging
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Organization, OrgRole, Page, Section

logger = logging.getLogger(__name__)
router = APIRouter(tags=["public"])

DEFAULT_PRIMARY_COLOR = "#6366f1"

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


def _build_nav_sections(org_id: int, db: Session) -> list[dict]:
    """Build the sidebar navigation tree (top-level sections + their pages)."""
    top_sections = (
        db.query(Section)
        .filter(Section.organization_id == org_id, Section.parent_id.is_(None), Section.is_published == True)
        .order_by(Section.display_order, Section.name)
        .all()
    )

    nav = []
    for s in top_sections:
        pages = (
            db.query(Page)
            .filter(Page.section_id == s.id, Page.is_published == True)
            .order_by(Page.display_order, Page.title)
            .all()
        )
        nav.append({
            "id": s.id,
            "name": s.name,
            "slug": s.slug,
            "pages": [{"id": p.id, "title": p.title, "slug": p.slug} for p in pages],
        })
    return nav


def _build_top_section_cards(org_id: int, db: Session) -> list[dict]:
    """Landing page section cards with page count and first page slug."""
    top_sections = (
        db.query(Section)
        .filter(Section.organization_id == org_id, Section.parent_id.is_(None), Section.is_published == True)
        .order_by(Section.display_order, Section.name)
        .all()
    )
    cards = []
    for s in top_sections:
        pages = (
            db.query(Page)
            .filter(Page.section_id == s.id, Page.is_published == True)
            .order_by(Page.display_order, Page.title)
            .all()
        )
        cards.append({
            "id": s.id,
            "name": s.name,
            "slug": s.slug,
            "page_count": len(pages),
            "first_page_slug": pages[0].slug if pages else None,
        })
    return cards


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


def _base_ctx(org: Organization, nav_sections: list, top_sections: list, primary_color: str) -> dict:
    return {
        "org_name": org.name,
        "org_slug": org.slug or str(org.id),
        "org_logo": org.logo_url,
        "org_initials": _org_initials(org.name),
        "org_tagline": org.tagline,
        "primary_color": primary_color,
        "nav_sections": nav_sections,
        "top_sections": top_sections,
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


@router.get("/docs/{org_slug}", response_class=HTMLResponse)
def docs_landing(org_slug: str) -> HTMLResponse:
    """Org documentation landing page — shows section cards."""
    db = _get_db()
    try:
        org = _lookup_org(org_slug, db)

        primary = org.primary_color or DEFAULT_PRIMARY_COLOR
        nav = _build_nav_sections(org.id, db)
        cards = _build_top_section_cards(org.id, db)

        html = _render(
            "docs.html",
            **_base_ctx(org, nav, cards, primary),
            page_title=org.name,
            section_name=None,
            current_page=None,
            current_page_slug=None,
            page_html=None,
        )
        return HTMLResponse(content=html)
    finally:
        db.close()


@router.get("/docs/{org_slug}/search")
def docs_search(org_slug: str, q: str = Query(default="")) -> JSONResponse:
    """Full-text search over published pages for an org."""
    db = _get_db()
    try:
        org = _lookup_org(org_slug, db)
        q_stripped = q.strip().lower()
        if not q_stripped:
            return JSONResponse({"results": []})

        pages = (
            db.query(Page)
            .filter(Page.organization_id == org.id, Page.is_published == True)
            .all()
        )

        results = []
        for page in pages:
            title_lower = page.title.lower()
            if q_stripped in title_lower:
                score = 2 if title_lower.startswith(q_stripped) else 1
            elif page.published_html and q_stripped in page.published_html.lower():
                score = 0.5
            else:
                continue

            section = db.get(Section, page.section_id) if page.section_id else None
            results.append({
                "title": page.title,
                "slug": page.slug,
                "section_name": section.name if section else None,
                "score": score,
            })

        results.sort(key=lambda r: r["score"], reverse=True)
        return JSONResponse({
            "results": [{"title": r["title"], "slug": r["slug"], "section_name": r["section_name"]} for r in results[:12]]
        })
    finally:
        db.close()


@router.get("/docs/{org_slug}/{page_slug}", response_class=HTMLResponse)
def docs_page(org_slug: str, page_slug: str) -> HTMLResponse:
    """Serve a single published page by slug."""
    db = _get_db()
    try:
        org = _lookup_org(org_slug, db)

        page = db.query(Page).filter(
            Page.organization_id == org.id,
            Page.slug == page_slug,
            Page.is_published == True,
        ).first()
        if not page:
            raise HTTPException(status_code=404, detail="Page not found or not published")

        section = db.get(Section, page.section_id) if page.section_id else None
        primary = org.primary_color or DEFAULT_PRIMARY_COLOR
        nav = _build_nav_sections(org.id, db)
        cards = _build_top_section_cards(org.id, db)

        page_html = _clean_gdoc_html(page.published_html or "")

        html = _render(
            "docs.html",
            **_base_ctx(org, nav, cards, primary),
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
    finally:
        db.close()
