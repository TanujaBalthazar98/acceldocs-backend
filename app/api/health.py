"""Health check endpoint and utility routes."""

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "service": "acceldocs"}


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    """Robots.txt — allow all crawlers on public docs, block dashboard/api."""
    return (
        "User-agent: *\n"
        "Allow: /docs/\n"
        "Disallow: /api/\n"
        "Disallow: /dashboard\n"
        "Disallow: /admin/\n"
        "Disallow: /auth/\n"
        "\n"
        "Sitemap: https://acceldocs.vercel.app/sitemap.xml\n"
    )


@router.get("/llms.txt", response_class=PlainTextResponse)
async def llms_txt():
    """llms.txt — GEO (Generative Engine Optimization) for AI crawlers.

    Tells AI assistants and search engines how to understand this platform.
    See https://llmstxt.org for the spec.
    """
    return """# Knowledge Workspace

> Turn your Google Drive into a production documentation system.

Knowledge Workspace is a documentation platform that connects to Google Drive,
lets teams write in Google Docs as usual, and then runs structured review/approval
workflows before publishing to a versioned, public-facing docs portal.

## What it does

- Syncs pages from Google Drive folders automatically
- Enforces RBAC + approval gates (draft → review → published)
- Renders documentation at /docs/{org-slug}/{page-slug}
- Provides an AI documentation agent (backed by Claude) that can draft
  new pages from Jira tickets or natural language instructions
- Supports internal, external, and public visibility per page

## Key concepts

- **Organization**: A workspace (team/company) with its own docs portal
- **Section**: A top-level grouping of pages (like a chapter or product)
- **Page**: A single documentation page, backed by a Google Doc
- **Draft/Review/Published**: The three states a page moves through
- **Agent**: The AI assistant inside the dashboard that generates and explores docs

## For developers

The backend is a FastAPI app deployed on Vercel, connected to a Neon Postgres database.
The frontend is a React + Vite SPA.

## Contact

Product by AccelData — https://acceldata.io
"""
