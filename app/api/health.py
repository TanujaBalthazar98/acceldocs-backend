"""Health check endpoint and utility routes."""

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from app.config import settings
from app.database import engine

router = APIRouter()


def _normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/")


def _database_check() -> tuple[bool, str]:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "Database connection is healthy."
    except Exception as exc:  # pragma: no cover - defensive runtime check
        return False, f"Database check failed: {exc}"


@router.get("/health")
async def health():
    return {"status": "ok", "service": "acceldocs-backend"}


@router.get("/ready")
async def ready():
    """Readiness check for operators and load balancers.

    Returns 200 when the service is ready to serve traffic.
    Returns 503 when required production guarantees are not met.
    """
    checks: list[dict[str, str | bool]] = []

    db_ok, db_message = _database_check()
    checks.append({"name": "database", "ok": db_ok, "message": db_message})

    oauth_ok = bool(settings.google_client_id.strip() and settings.google_client_secret.strip())
    oauth_message = "Google OAuth credentials are configured."
    if not oauth_ok:
        oauth_message = "Google OAuth credentials are missing."
    checks.append({"name": "google_oauth", "ok": oauth_ok, "message": oauth_message})

    cors_origins = settings.allowed_origins_list
    cors_ok = bool(cors_origins)
    cors_message = "CORS origins are configured."
    if not cors_ok:
        cors_message = "CORS origins are not configured."

    if settings.is_production and cors_ok:
        local_origins = [
            origin for origin in cors_origins if "localhost" in origin.lower() or "127.0.0.1" in origin
        ]
        if local_origins:
            cors_ok = False
            cors_message = f"CORS contains local origins in production: {', '.join(local_origins)}"
        elif any(not origin.lower().startswith("https://") for origin in cors_origins):
            cors_ok = False
            cors_message = "All CORS origins must be https in production."
        else:
            normalized_allowed = {_normalize_origin(origin) for origin in cors_origins}
            if _normalize_origin(settings.frontend_url) not in normalized_allowed:
                cors_ok = False
                cors_message = "FRONTEND_URL must be present in ALLOWED_ORIGINS."

    checks.append({"name": "cors", "ok": cors_ok, "message": cors_message})

    rate_limit_ok = True
    rate_limit_message = "Rate limiting storage is configured."
    if settings.rate_limit_enabled:
        if settings.rate_limit_storage_uri.strip().lower() == "memory://":
            if settings.is_production:
                rate_limit_ok = False
                rate_limit_message = "Rate limiting uses memory:// in production (use Redis)."
            else:
                rate_limit_message = "Rate limiting uses memory:// (acceptable for development only)."
    else:
        rate_limit_message = "Rate limiting is disabled by configuration."

    checks.append({"name": "rate_limiting", "ok": rate_limit_ok, "message": rate_limit_message})

    if settings.is_production:
        ready_ok = all(bool(c["ok"]) for c in checks)
    else:
        # In non-prod, only DB check is mandatory.
        ready_ok = db_ok

    payload = {
        "status": "ready" if ready_ok else "degraded",
        "environment": settings.environment,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }

    if settings.is_production and not ready_ok:
        return JSONResponse(status_code=503, content=payload)

    return payload


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt(request: Request):
    """Robots.txt — allow all crawlers on public docs, block dashboard/api."""
    base = str(request.base_url).rstrip("/")
    return (
        "User-agent: *\n"
        "Allow: /docs/\n"
        "Disallow: /api/\n"
        "Disallow: /dashboard\n"
        "Disallow: /admin/\n"
        "Disallow: /auth/\n"
        "\n"
        "User-agent: Google-Extended\n"
        "Allow: /docs/\n"
        "Disallow: /api/\n"
        "Disallow: /dashboard\n"
        "Disallow: /admin/\n"
        "Disallow: /auth/\n"
        "\n"
        f"Sitemap: {base}/sitemap.xml\n"
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
