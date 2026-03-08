"""AccelDocs backend — FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import Base, engine
from app.services.encryption import init_encryption_service
from app.api.health import router as health_router
from app.api.documents import router as documents_router
from app.api.sync import router as sync_router
from app.api.approvals import router as approvals_router
from app.api.users import router as users_router
from app.api.settings import router as settings_router
from app.api.projects import router as projects_router
from app.api.drive import router as drive_router
from app.api.analytics import router as analytics_router
from app.api.ui import router as ui_router
from app.api.functions import router as functions_router
from app.api.publish import router as publish_router
from app.api.public import router as public_router
from app.api.github_publish import router as github_router
from app.auth.routes import router as auth_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _ensure_missing_columns(eng):
    """Add any columns present in ORM models but missing from the live DB.

    SQLAlchemy's create_all() only creates new tables; it never alters existing
    ones.  This helper inspects every mapped table and issues ALTER TABLE ADD
    COLUMN for anything that doesn't exist yet — safe to run on every startup.
    """
    from sqlalchemy import inspect as sa_inspect, text

    try:
        inspector = sa_inspect(eng)
        with eng.begin() as conn:
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
                        col_type = col.type.compile(dialect=eng.dialect)
                        # Always add as nullable to avoid issues with existing rows
                        stmt = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col_type}'
                        logger.info("Adding missing column: %s", stmt)
                        conn.execute(text(stmt))
                    except Exception as col_err:
                        logger.warning("Could not add column %s.%s: %s",
                                       table.name, col.name, col_err)
    except Exception as e:
        logger.error("_ensure_missing_columns failed (non-fatal): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize encryption service for Google token storage
    logger.info("Initializing encryption service...")
    logger.info("SECRET_KEY length: %d, AUTO_CREATE_SCHEMA: %s, ALLOWED_ORIGINS: %s",
                len(settings.secret_key), settings.auto_create_schema, settings.allowed_origins)
    init_encryption_service(settings.secret_key)

    if settings.auto_create_schema:
        logger.info("AUTO_CREATE_SCHEMA enabled: creating database tables...")
        Base.metadata.create_all(bind=engine)
        # create_all only creates NEW tables — it won't add columns to existing
        # tables.  Run a lightweight migration to add any missing columns.
        _ensure_missing_columns(engine)
    else:
        logger.info("AUTO_CREATE_SCHEMA disabled: expecting schema to be managed by migrations")
    logger.info("AccelDocs backend started on %s:%s", settings.host, settings.port)
    yield


app = FastAPI(
    title="AccelDocs Backend",
    description="Google Docs → Zensical publishing pipeline",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes — public endpoints first (Strapi-compatible queries for docs viewer)
app.include_router(public_router)
app.include_router(ui_router)
app.include_router(health_router)
app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(functions_router, tags=["functions"])  # RPC-style functions (no prefix, handles /api/functions/* internally)
app.include_router(documents_router, prefix="/api/documents", tags=["documents"])
app.include_router(sync_router, prefix="/api/sync", tags=["sync"])
app.include_router(approvals_router, prefix="/api/approvals", tags=["approvals"])
app.include_router(users_router, prefix="/api/users", tags=["users"])
app.include_router(settings_router, prefix="/api/settings", tags=["settings"])
app.include_router(projects_router, prefix="/api/projects", tags=["projects"])
app.include_router(drive_router, prefix="/api/drive", tags=["drive"])
app.include_router(analytics_router, prefix="/api/analytics", tags=["analytics"])
app.include_router(publish_router, tags=["publish"])
app.include_router(github_router, prefix="/api", tags=["github"])
