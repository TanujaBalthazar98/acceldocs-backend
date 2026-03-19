"""Settings management API routes."""

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from pydantic import BaseModel

from app.auth.routes import get_current_user
from app.config import settings
from app.lib.env_manager import EnvManager
from app.lib.rbac import get_permissions_for_role
from app.models import User

router = APIRouter()


class SettingsResponse(BaseModel):
    google_drive_root_folder_id: str
    google_service_account_file: str
    google_client_id: str
    google_client_secret: str
    docs_repo_path: str
    docs_repo_url: str
    netlify_site_id: str
    netlify_auth_token: str
    secret_key: str
    allowed_origins: str


class SettingsUpdate(BaseModel):
    google_drive_root_folder_id: str | None = None
    google_service_account_file: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    docs_repo_path: str | None = None
    docs_repo_url: str | None = None
    netlify_site_id: str | None = None
    netlify_auth_token: str | None = None
    secret_key: str | None = None
    allowed_origins: str | None = None


class TestDriveResponse(BaseModel):
    success: bool
    message: str
    folder_name: str | None = None


class DeployResponse(BaseModel):
    success: bool
    message: str
    deploy_id: str | None = None


def _require_admin(current_user: User) -> None:
    """Check if current user has admin permissions."""
    perms = get_permissions_for_role(current_user.role)
    if "settings.edit" not in perms:
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("/", response_model=SettingsResponse)
async def get_settings(current_user: User = Depends(get_current_user)):
    """Get all settings (secrets are redacted)."""
    _require_admin(current_user)

    env = EnvManager()
    env_vars = env.read_all(redact_secrets=True)

    return SettingsResponse(
        google_drive_root_folder_id=env_vars.get("GOOGLE_DRIVE_ROOT_FOLDER_ID", ""),
        google_service_account_file=env_vars.get("GOOGLE_SERVICE_ACCOUNT_FILE", ""),
        google_client_id=env_vars.get("GOOGLE_CLIENT_ID", ""),
        google_client_secret=env_vars.get("GOOGLE_CLIENT_SECRET", ""),
        docs_repo_path=env_vars.get("DOCS_REPO_PATH", ""),
        docs_repo_url=env_vars.get("DOCS_REPO_URL", ""),
        netlify_site_id=env_vars.get("NETLIFY_SITE_ID", ""),
        netlify_auth_token=env_vars.get("NETLIFY_AUTH_TOKEN", ""),
        secret_key=env_vars.get("SECRET_KEY", ""),
        allowed_origins=env_vars.get("ALLOWED_ORIGINS", ""),
    )


@router.put("/")
async def update_settings(
    body: SettingsUpdate,
    current_user: User = Depends(get_current_user),
):
    """Update settings and write to .env file."""
    _require_admin(current_user)

    env = EnvManager()
    updates = {}

    # Build update dict (only include non-None values)
    if body.google_drive_root_folder_id is not None:
        updates["GOOGLE_DRIVE_ROOT_FOLDER_ID"] = body.google_drive_root_folder_id
    if body.google_service_account_file is not None:
        updates["GOOGLE_SERVICE_ACCOUNT_FILE"] = body.google_service_account_file
    if body.google_client_id is not None:
        updates["GOOGLE_CLIENT_ID"] = body.google_client_id
    if body.google_client_secret is not None:
        # Don't update if it's the redacted placeholder
        if body.google_client_secret != "***":
            updates["GOOGLE_CLIENT_SECRET"] = body.google_client_secret
    if body.docs_repo_path is not None:
        updates["DOCS_REPO_PATH"] = body.docs_repo_path
    if body.docs_repo_url is not None:
        updates["DOCS_REPO_URL"] = body.docs_repo_url
    if body.netlify_site_id is not None:
        updates["NETLIFY_SITE_ID"] = body.netlify_site_id
    if body.netlify_auth_token is not None:
        if body.netlify_auth_token != "***":
            updates["NETLIFY_AUTH_TOKEN"] = body.netlify_auth_token
    if body.secret_key is not None:
        if body.secret_key != "***":
            updates["SECRET_KEY"] = body.secret_key
    if body.allowed_origins is not None:
        updates["ALLOWED_ORIGINS"] = body.allowed_origins

    if updates:
        env.update(updates)

    return {"status": "ok", "updated_count": len(updates)}


@router.post("/test-drive", response_model=TestDriveResponse)
async def test_drive_connection(current_user: User = Depends(get_current_user)):
    """Test Google Drive connection using service account."""
    _require_admin(current_user)

    try:
        # Load service account credentials
        service_account_path = Path(settings.google_service_account_file)
        if not service_account_path.exists():
            return TestDriveResponse(
                success=False,
                message=f"Service account file not found: {settings.google_service_account_file}",
            )

        creds = Credentials.from_service_account_file(
            settings.google_service_account_file,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        service = build("drive", "v3", credentials=creds, cache_discovery=False)

        # Try to get root folder info
        folder = service.files().get(
            fileId=settings.google_drive_root_folder_id,
            fields="id,name",
            supportsAllDrives=True,
        ).execute()

        return TestDriveResponse(
            success=True,
            message="Successfully connected to Google Drive",
            folder_name=folder.get("name"),
        )

    except FileNotFoundError as e:
        return TestDriveResponse(success=False, message=f"File not found: {e}")
    except Exception as e:
        return TestDriveResponse(success=False, message=f"Connection failed: {e}")


@router.post("/backup-db")
async def backup_database(current_user: User = Depends(get_current_user)):
    """Create a backup of the SQLite database."""
    _require_admin(current_user)

    try:
        from datetime import datetime
        import shutil

        db_path = Path("acceldocs.db")
        if not db_path.exists():
            raise HTTPException(status_code=404, detail="Database file not found")

        # Create backup with timestamp
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup_path = Path(f"backups/acceldocs_{timestamp}.db")
        backup_path.parent.mkdir(exist_ok=True)

        shutil.copy2(db_path, backup_path)

        return {
            "status": "ok",
            "backup_file": str(backup_path),
            "size_bytes": backup_path.stat().st_size,
        }

    except Exception as e:
        logger.error("Backup failed: %s", e)
        raise HTTPException(status_code=500, detail="Backup failed. Please try again.")


@router.post("/deploy-netlify", response_model=DeployResponse)
async def trigger_netlify_deploy(current_user: User = Depends(get_current_user)):
    """Trigger a manual Netlify deployment."""
    _require_admin(current_user)

    try:
        if not settings.netlify_site_id or not settings.netlify_auth_token:
            return DeployResponse(
                success=False,
                message="Netlify site ID or auth token not configured",
            )

        # Trigger deploy via Netlify API
        import requests

        response = requests.post(
            f"https://api.netlify.com/api/v1/sites/{settings.netlify_site_id}/builds",
            headers={"Authorization": f"Bearer {settings.netlify_auth_token}"},
            timeout=10,
        )

        if response.status_code == 200:
            deploy_data = response.json()
            return DeployResponse(
                success=True,
                message="Deployment triggered successfully",
                deploy_id=deploy_data.get("id"),
            )
        else:
            return DeployResponse(
                success=False,
                message=f"Netlify API error: {response.status_code} {response.text}",
            )

    except Exception as e:
        return DeployResponse(success=False, message=f"Deploy failed: {e}")
