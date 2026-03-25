"""AccelDocs backend — clean architecture entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.services.encryption import init_encryption_service
from app.middleware.security import (
    SecurityHeadersMiddleware,
    CSRFProtectionMiddleware,
    RATE_LIMITING_ACTIVE,
    limiter,
    rate_limit_exceeded_handler,
    RateLimitExceeded,
    SlowAPIMiddleware,
)

# New clean-arch routers
from app.api.health import router as health_router
from app.api.org import router as org_router
from app.api.sections import router as sections_router
from app.api.pages import router as pages_router
from app.api.drive import router as drive_router
from app.api.public import router as public_router
from app.api.external_access import router as external_access_router
from app.api.functions import router as functions_router
# Agent chat is optional. Do not let optional AI provider deps block core auth/docs API.
try:
    from app.api.agent_chat import router as agent_chat_router
    _agent_chat_import_error: Exception | None = None
except Exception as _err:  # pragma: no cover - defensive for production startup
    agent_chat_router = None
    _agent_chat_import_error = _err
from app.api.documents import router as documents_router
from app.api.approvals import router as approvals_router
from app.api.analytics import router as analytics_router
from app.api.projects import router as projects_router
from app.api.search import router as search_router
from app.api.users import router as users_router
from app.api.ui import router as ui_router
from app.api.brand_extract import router as brand_extract_router
from app.auth.routes import router as auth_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
if _agent_chat_import_error is not None:
    logger.warning("Agent chat router disabled during startup: %s", _agent_chat_import_error)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing AccelDocs (clean-arch)…")
    init_encryption_service(settings.secret_key)

    if settings.auto_create_schema:
        logger.info("Creating / updating database schema…")
        Base.metadata.create_all(bind=engine)
        _add_missing_columns()

    _backfill_org_owner_ids()
    logger.info("AccelDocs backend ready on %s:%s", settings.host, settings.port)
    yield


def _add_missing_columns() -> None:
    """Best-effort: ALTER TABLE for any ORM column not yet in the live DB."""
    from sqlalchemy import inspect as sa_inspect, text

    try:
        inspector = sa_inspect(engine)
        with engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                try:
                    if not inspector.has_table(table.name):
                        continue
                    existing = {c["name"] for c in inspector.get_columns(table.name)}
                except Exception:
                    continue
                for col in table.columns:
                    if col.name in existing:
                        continue
                    try:
                        col_type = col.type.compile(dialect=engine.dialect)
                        stmt = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'
                        logger.info("Adding missing column: %s", stmt)
                        conn.execute(text(stmt))
                    except Exception as err:
                        logger.warning("Could not add %s.%s: %s", table.name, col.name, err)
    except Exception as e:
        logger.error("_add_missing_columns failed (non-fatal): %s", e)

    # Make approvals.document_id nullable (was NOT NULL, now pages use page_id instead)
    try:
        from sqlalchemy import text as _text
        with engine.begin() as conn:
            dialect_name = engine.dialect.name
            if dialect_name == "postgresql":
                conn.execute(_text(
                    'ALTER TABLE "approvals" ALTER COLUMN "document_id" DROP NOT NULL'
                ))
                logger.info("Made approvals.document_id nullable")
            # SQLite doesn't support ALTER COLUMN, but also doesn't enforce NOT NULL
            # on existing rows, so it's fine.
    except Exception as e:
        # Column might already be nullable, or table might not exist yet.
        logger.debug("approvals.document_id nullable migration (non-fatal): %s", e)


def _backfill_org_owner_ids() -> None:
    """Fix orgs where owner_id doesn't match the actual OrgRole owner."""
    from app.models import OrgRole, Organization
    try:
        db = SessionLocal()
        orgs = db.query(Organization).all()
        for org in orgs:
            owner_role = (
                db.query(OrgRole)
                .filter(OrgRole.organization_id == org.id, OrgRole.role == "owner")
                .first()
            )
            if owner_role and org.owner_id != owner_role.user_id:
                logger.info(
                    "Fixing org %s owner_id: %s -> %s",
                    org.id, org.owner_id, owner_role.user_id,
                )
                org.owner_id = owner_role.user_id
        db.commit()
        db.close()
    except Exception as e:
        logger.debug("_backfill_org_owner_ids (non-fatal): %s", e)


app = FastAPI(
    title="AccelDocs",
    description="Google Docs → Professional documentation, served directly.",
    version="2.0.0",
    lifespan=lifespan,
)

logger.info("CORS allowed origins: %s", settings.allowed_origins_list)

# Security middleware — headers, CSRF, rate limiting
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CSRFProtectionMiddleware, allowed_origins=settings.allowed_origins_list)
if RATE_LIMITING_ACTIVE:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

# CORS must be the outermost middleware so all responses (including preflight,
# 4xx/5xx, and rate-limit responses) include CORS headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Org-Id", "X-Requested-With", "Accept"],
)

# Public docs site — no auth prefix, matched first
app.include_router(public_router)

# API routes
app.include_router(health_router)
app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(org_router, prefix="/api/org", tags=["org"])
app.include_router(brand_extract_router, prefix="/api/org", tags=["org"])
app.include_router(sections_router, prefix="/api/sections", tags=["sections"])
app.include_router(pages_router, prefix="/api/pages", tags=["pages"])
app.include_router(documents_router, prefix="/api/documents", tags=["documents"])
app.include_router(approvals_router, prefix="/api/approvals", tags=["approvals"])
app.include_router(analytics_router, prefix="/api/analytics", tags=["analytics"])
app.include_router(projects_router, prefix="/api/projects", tags=["projects"])
app.include_router(search_router, prefix="/api/docs", tags=["search"])
app.include_router(users_router, prefix="/api/users", tags=["users"])
app.include_router(drive_router, prefix="/api/drive", tags=["drive"])
app.include_router(external_access_router, prefix="/api/external-access", tags=["external-access"])
app.include_router(functions_router, tags=["functions"])
if agent_chat_router is not None:
    app.include_router(agent_chat_router, tags=["agent-chat"])
from app.api.agent_inline import router as agent_inline_router
app.include_router(agent_inline_router, tags=["agent-inline"])
from app.api.agent_history import router as agent_history_router
app.include_router(agent_history_router, tags=["agent-history"])
app.include_router(ui_router, tags=["ui"])
