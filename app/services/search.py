"""BM25-based semantic search across org pages."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from sqlalchemy.orm import Session

from app.models import Page


# ---------------------------------------------------------------------------
# HTML → plain text (local, no external dep)
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str):
        self._parts.append(data)

    def text(self) -> str:
        return " ".join(self._parts).strip()


def _html_to_text(html: str) -> str:
    ext = _TextExtractor()
    ext.feed(html or "")
    return ext.text()


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


# ---------------------------------------------------------------------------
# BM25 search
# ---------------------------------------------------------------------------

def search_pages_bm25(
    org_id: int,
    query: str,
    db: Session,
    *,
    limit: int = 10,
    published_only: bool = True,
) -> list[dict]:
    """BM25-ranked search across org pages. Returns list of result dicts."""
    from rank_bm25 import BM25Okapi

    filters = [Page.organization_id == org_id]
    if published_only:
        filters.append(Page.is_published == True)  # noqa: E712

    pages = db.query(Page).filter(*filters).all()
    if not pages:
        return []

    # Build corpus from search_text (preferred) or html content
    corpus: list[list[str]] = []
    for p in pages:
        text = p.search_text or _html_to_text(p.published_html or p.html_content or "")
        corpus.append(_tokenize(text))

    bm25 = BM25Okapi(corpus)
    tokenized_query = _tokenize(query)
    if not tokenized_query:
        return []

    scores = bm25.get_scores(tokenized_query)
    ranked = sorted(zip(pages, scores), key=lambda x: x[1], reverse=True)

    results = []
    for page, score in ranked[:limit]:
        if score <= 0:
            break
        text = page.search_text or _html_to_text(
            page.published_html or page.html_content or ""
        )
        results.append({
            "id": page.id,
            "title": page.title,
            "slug": page.slug,
            "section_id": page.section_id,
            "score": round(float(score), 3),
            "snippet": text[:300],
        })
    return results
