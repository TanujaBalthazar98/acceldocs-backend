"""Google Drive browser API routes."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.ingestion.drive import _get_service as get_drive_service

router = APIRouter()


class SyncFileRequest(BaseModel):
    project_id: int


@router.get("/browse")
async def browse_drive_folder(folder_id: str):
    """Browse contents of a Google Drive folder."""
    try:
        service = get_drive_service()

        # Get folder metadata
        folder = service.files().get(
            fileId=folder_id,
            fields="id, name"
        ).execute()

        # List files in folder
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id, name, mimeType, modifiedTime, owners, webViewLink)",
            orderBy="folder,name"
        ).execute()

        files = results.get('files', [])

        return {
            "folderId": folder_id,
            "folderName": folder.get('name'),
            "files": files
        }

    except FileNotFoundError as e:
        # No credentials configured
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Google Drive credentials not configured",
                "message": str(e),
                "help": "Configure GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_OAUTH_TOKEN_FILE environment variable"
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to browse Drive folder: {str(e)}")


@router.get("/file/{file_id}")
async def get_file_details(file_id: str):
    """Get detailed metadata for a specific file."""
    try:
        service = get_drive_service()

        file = service.files().get(
            fileId=file_id,
            fields="id, name, mimeType, modifiedTime, createdTime, owners, webViewLink, size, description"
        ).execute()

        return file

    except Exception as e:
        raise HTTPException(status_code=404, detail=f"File not found: {str(e)}")


@router.post("/sync/{file_id}")
async def sync_single_file(
    file_id: str,
    body: SyncFileRequest,
    db: Session = Depends(get_db),
):
    """Manually sync a single file from Google Drive."""
    # TODO: Implement full sync logic
    # For now, just validate the file exists
    try:
        service = get_drive_service()

        # Get file metadata
        file = service.files().get(
            fileId=file_id,
            fields="id, name, mimeType, modifiedTime"
        ).execute()

        # Only sync Google Docs
        if file['mimeType'] != 'application/vnd.google-apps.document':
            raise HTTPException(
                status_code=400,
                detail="Only Google Docs can be synced"
            )

        # For now, return success without actually syncing
        # Full implementation would:
        # 1. Export HTML from Drive
        # 2. Convert to Markdown
        # 3. Publish to git branches
        # 4. Update database record

        return {
            "status": "acknowledged",
            "message": f"Manual sync of '{file['name']}' will be implemented in full sync workflow",
            "file_name": file['name'],
            "file_id": file_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to access file: {str(e)}")
