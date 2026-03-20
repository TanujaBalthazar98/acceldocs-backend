"""Full-text search endpoint for public and internal documentation."""

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.config import settings
from app.database import get_db
from app.models import Organization, OrgRole, Page, Section, User

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_RESULTS = 50


class SearchRequest(BaseModel):
    q: str
    org_slug: str
    audience: str = "public"  # public | internal | all
    limit: int = 20


class SearchResultItem(BaseModel):
    page_id: int
    title: str
    slug: str
    section_name: str | None = None
    snippet: str = ""
    score: float = 0.0


def _strip_tags(html: str) -> str:
    """Minimal HTML tag stripper for snippet generation."""
    return re.sub(r"<[^>]+>", "", html or "")


def _generate_snippet(text_content: str, query: str, max_words: int = 30) -> str:
    """Extract a snippet around the first occurrence of the query."""
    if not text_content or not query:
        return text_content[:200] if text_content else ""

    lower_text = text_content.lower()
    lower_query = query.lower().strip()
    pos = lower_text.find(lower_query)

    if pos == -1:
        # No exact match — return beginning
        words = text_content.split()
        return " ".join(words[:max_words]) + ("..." if len(words) > max_words else "")

    # Find word boundaries around the match
    start = max(0, pos - 100)
    end = min(len(text_content), pos + len(lower_query) + 200)
    fragment = text_content[start:end].strip()

    # Highlight the match
    match_start = pos - start
    match_end = match_start + len(lower_query)
    highlighted = (
        fragment[:match_start]
        + "<mark>"
        + fragment[match_start:match_end]
        + "</mark>"
        + fragment[match_end:]
    )

    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text_content) else ""
    return prefix + highlighted + suffix


@router.post("/search")
def search_docs(
    body: SearchRequest,
    db: Session = Depends(get_db),
    x_org_id: int | None = Header(default=None, alias="X-Org-Id"),
    user: Optional[User] = Depends(lambda: None),  # placeholder — see below
):
    """Search published documentation by title and content."""
    query_text = (body.q or "").strip()
    if not query_text or len(query_text) < 2:
        return {"results": []}

    limit = min(body.limit or 20, MAX_RESULTS)

    # Resolve organization
    org = db.query(Organization).filter(Organization.slug == body.org_slug).first()
    if not org:
        return {"results": []}

    # Base query: only published pages in this org
    q = (
        db.query(Page, Section.name.label("section_name"))
        .outerjoin(Section, Page.section_id == Section.id)
        .filter(Page.organization_id == org.id, Page.is_published == True)  # noqa: E712
    )

    # Visibility filter
    if body.audience == "public":
        # Public: only pages with no visibility override or explicit "public"
        q = q.filter(
            (Page.visibility_override == None) | (Page.visibility_override == "public")  # noqa: E711
        )

    results: list[SearchResultItem] = []

    if settings.is_sqlite:
        # SQLite fallback: simple LIKE matching
        like_pattern = f"%{query_text}%"
        q = q.filter(
            func.lower(Page.title + " " + func.coalesce(Page.search_text, "")).like(
                f"%{query_text.lower()}%"
            )
        )
        rows = q.order_by(Page.title).limit(limit).all()

        for page, section_name in rows:
            snippet = _generate_snippet(
                page.search_text or _strip_tags(page.published_html or ""),
                query_text,
            )
            # Basic score: title match > content match
            score = 2.0 if query_text.lower() in (page.title or "").lower() else 1.0
            results.append(
                SearchResultItem(
                    page_id=page.id,
                    title=page.title,
                    slug=page.slug,
                    section_name=section_name,
                    snippet=snippet,
                    score=score,
                )
            )
    else:
        # PostgreSQL: full-text search with ranking
        tsquery = func.plainto_tsquery("english", query_text)
        tsvector = func.to_tsvector(
            "english",
            Page.title + text("' '") + func.coalesce(Page.search_text, text("''")),
        )
        rank = func.ts_rank_cd(tsvector, tsquery)

        q = q.filter(tsvector.op("@@")(tsquery))
        rows = q.add_columns(rank.label("rank")).order_by(rank.desc()).limit(limit).all()

        for page, section_name, rank_score in rows:
            # Use ts_headline for snippets if search_text exists
            try:
                snippet_result = db.execute(
                    text(
                        "SELECT ts_headline('english', :content, plainto_tsquery('english', :query), "
                        "'MaxWords=30, MinWords=15, StartSel=<mark>, StopSel=</mark>')"
                    ),
                    {
                        "content": page.search_text or page.title,
                        "query": query_text,
                    },
                ).scalar()
                snippet = snippet_result or ""
            except Exception:
                snippet = _generate_snippet(
                    page.search_text or _strip_tags(page.published_html or ""),
                    query_text,
                )

            results.append(
                SearchResultItem(
                    page_id=page.id,
                    title=page.title,
                    slug=page.slug,
                    section_name=section_name,
                    snippet=snippet,
                    score=float(rank_score or 0),
                )
            )

    return {"results": results}
