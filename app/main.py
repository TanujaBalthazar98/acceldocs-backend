"""AccelDocs backend — clean architecture entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import Base, engine
from app.services.encryption import init_encryption_service

# New clean-arch routers
from app.api.health import router as health_router
from app.api.org import router as org_router
from app.api.sections import router as sections_router
from app.api.pages import router as pages_router
from app.api.drive import router as drive_router
from app.api.public import router as public_router
from app.api.external_access import router as external_access_router
from app.auth.routes import router as auth_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing AccelDocs (clean-arch)…")
    init_encryption_service(settings.secret_key)

    if settings.auto_create_schema:
        logger.info("Creating / updating database schema…")
        Base.metadata.create_all(bind=engine)
        _add_missing_columns()

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


app = FastAPI(
    title="AccelDocs",
    description="Google Docs → Professional documentation, served directly.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Public docs site — no auth prefix, matched first
app.include_router(public_router)

# API routes
app.include_router(health_router)
app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(org_router, prefix="/api/org", tags=["org"])
app.include_router(sections_router, prefix="/api/sections", tags=["sections"])
app.include_router(pages_router, prefix="/api/pages", tags=["pages"])
app.include_router(drive_router, prefix="/api/drive", tags=["drive"])
app.include_router(external_access_router, prefix="/api/external-access", tags=["external-access"])
