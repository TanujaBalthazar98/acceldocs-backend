"""AccelDocs backend — FastAPI application entry point."""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import Base, engine
from app.api.health import router as health_router
from app.api.documents import router as documents_router
from app.api.sync import router as sync_router
from app.api.approvals import router as approvals_router
from app.auth.routes import router as auth_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AccelDocs Backend",
    description="Google Docs → MkDocs publishing pipeline",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(health_router)
app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(documents_router, prefix="/api/documents", tags=["documents"])
app.include_router(sync_router, prefix="/api/sync", tags=["sync"])
app.include_router(approvals_router, prefix="/api/approvals", tags=["approvals"])


@app.on_event("startup")
async def startup():
    logger.info("Creating database tables...")
    Base.metadata.create_all(bind=engine)
    logger.info("AccelDocs backend started on %s:%s", settings.host, settings.port)
