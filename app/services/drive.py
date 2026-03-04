"""Google Drive operations with OAuth token management and auto-refresh.

This service implements all 8 Drive operations with proper token management:
- list_folder: List files/folders in a Drive folder
- create_folder: Create a new folder
- create_doc: Create a new Google Doc
- get_doc_content: Export Google Doc as HTML
- sync_doc_content: Sync Doc content to database
- move_file: Move file to a folder
- trash_file: Delete a file
- upload_file: Upload file with MIME conversion
"""

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from sqlalchemy.orm import Session

from app.models import Document, GoogleToken, OrgRole, Organization, User
from app.services.encryption import get_encryption_service

logger = logging.getLogger(__name__)

# Google API endpoints
GOOGLE_TOKEN_INFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_TOKEN_REFRESH_URL = "https://oauth2.googleapis.com/token"


class GoogleDriveService:
    """Service for Google Drive API operations with automatic token management."""

    def __init__(self, db: Session, user: User):
        """Initialize Drive service for a user.

        Args:
            db: Database session
            user: Authenticated user
        """
        self.db = db
        self.user = user
        self.encryption_service = get_encryption_service()

    async def get_credentials(self, access_token: str | None) -> Credentials | None:
        """Get valid Google credentials, refreshing if necessary.

        Args:
            access_token: Access token from frontend (may be expired)

        Returns:
            Valid Credentials object or None if unable to authenticate
        """
        # If access token provided and valid, use it
        if access_token and await self._is_token_valid(access_token):
            return Credentials(token=access_token)

        # Token expired or missing - try to refresh using stored refresh token
        logger.info(f"Access token missing or expired for user {self.user.email}, attempting refresh")
        return await self._refresh_credentials()

    async def _is_token_valid(self, token: str) -> bool:
        """Check if access token is still valid.

        Args:
            token: Access token to validate

        Returns:
            True if valid, False otherwise
        """
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{GOOGLE_TOKEN_INFO_URL}?access_token={token}",
                    timeout=5.0
                )
                return resp.status_code == 200
        except Exception as e:
            logger.warning(f"Token validation failed: {e}")
            return False

    async def _refresh_credentials(self) -> Credentials | None:
        """Refresh access token using stored refresh token.

        Returns:
            New Credentials with fresh access token, or None if refresh fails
        """
        from app.config import settings

        # Get organization ID for user
        org_role = self.db.query(OrgRole).filter(OrgRole.user_id == self.user.id).first()
        if not org_role:
            logger.error(f"User {self.user.email} has no organization")
            return None

        # Get stored refresh token
        google_token = self.db.query(GoogleToken).filter(
            GoogleToken.user_id == self.user.id,
            GoogleToken.organization_id == org_role.organization_id
        ).first()

        if not google_token:
            logger.error(f"No refresh token stored for user {self.user.email}")
            return None

        # Decrypt refresh token
        try:
            refresh_token = self.encryption_service.decrypt(google_token.encrypted_refresh_token)
        except Exception as e:
            logger.error(f"Failed to decrypt refresh token: {e}")
            return None

        # Refresh the access token
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    GOOGLE_TOKEN_REFRESH_URL,
                    data={
                        "client_id": settings.google_client_id,
                        "client_secret": settings.google_client_secret,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                    timeout=10.0
                )

            if resp.status_code != 200:
                logger.error(f"Token refresh failed: {resp.text}")
                return None

            token_data = resp.json()
            new_access_token = token_data.get("access_token")

            if not new_access_token:
                logger.error("No access token in refresh response")
                return None

            # Update last_refreshed_at
            google_token.last_refreshed_at = datetime.now(timezone.utc)
            self.db.commit()

            logger.info(f"Successfully refreshed token for user {self.user.email}")
            return Credentials(token=new_access_token)

        except Exception as e:
            logger.error(f"Token refresh exception: {e}")
            return None

    def _handle_api_error(self, error: HttpError) -> dict:
        """Convert Google API errors to standardized response format.

        Args:
            error: Google API error

        Returns:
            Error response dict
        """
        status_code = error.resp.status

        if status_code == 401:
            return {
                "ok": False,
                "needsReauth": True,
                "error": "Authentication expired. Please reconnect your Google account.",
                "errorCode": "UNAUTHORIZED"
            }
        elif status_code == 403:
            return {
                "ok": False,
                "needsReauth": True,
                "error": "Insufficient permissions. Please grant Drive access.",
                "errorCode": "FORBIDDEN"
            }
        elif status_code == 404:
            return {
                "ok": False,
                "error": "File or folder not found",
                "errorCode": "NOT_FOUND"
            }
        elif status_code == 429:
            return {
                "ok": False,
                "error": "Rate limit exceeded. Please try again later.",
                "errorCode": "RATE_LIMIT"
            }
        else:
            return {
                "ok": False,
                "error": f"Drive API error: {str(error)}",
                "errorCode": "API_ERROR"
            }

    async def list_folder(self, folder_id: str = "root") -> dict:
        """List files and folders in a Drive folder.

        Args:
            folder_id: Drive folder ID (default: root)

        Returns:
            {"ok": True, "files": [...]} or error response
        """
        creds = await self.get_credentials(None)
        if not creds:
            return {"ok": False, "needsReauth": True, "error": "Unable to authenticate"}

        try:
            service = build("drive", "v3", credentials=creds, cache_discovery=False)

            results = service.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="files(id, name, mimeType, modifiedTime, size, webViewLink)",
                pageSize=100,
                orderBy="folder,name"
            ).execute()

            files = results.get("files", [])

            return {
                "ok": True,
                "files": [
                    {
                        "id": f["id"],
                        "name": f["name"],
                        "mimeType": f["mimeType"],
                        "modifiedTime": f.get("modifiedTime"),
                        "size": f.get("size"),
                        "webViewLink": f.get("webViewLink"),
                        "isFolder": f["mimeType"] == "application/vnd.google-apps.folder"
                    }
                    for f in files
                ]
            }

        except HttpError as e:
            return self._handle_api_error(e)
        except Exception as e:
            logger.error(f"list_folder error: {e}")
            return {"ok": False, "error": str(e)}

    async def create_folder(self, name: str, parent_id: str = "root") -> dict:
        """Create a new folder in Google Drive.

        Args:
            name: Folder name
            parent_id: Parent folder ID (default: root)

        Returns:
            {"ok": True, "folder": {...}} or error response
        """
        creds = await self.get_credentials(None)
        if not creds:
            return {"ok": False, "needsReauth": True, "error": "Unable to authenticate"}

        try:
            service = build("drive", "v3", credentials=creds, cache_discovery=False)

            file_metadata = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id]
            }

            folder = service.files().create(
                body=file_metadata,
                fields="id, name, webViewLink"
            ).execute()

            return {
                "ok": True,
                "folder": {
                    "id": folder["id"],
                    "name": folder["name"],
                    "webViewLink": folder.get("webViewLink")
                }
            }

        except HttpError as e:
            return self._handle_api_error(e)
        except Exception as e:
            logger.error(f"create_folder error: {e}")
            return {"ok": False, "error": str(e)}

    async def create_doc(self, name: str, parent_id: str = "root") -> dict:
        """Create a new Google Doc.

        Args:
            name: Document name
            parent_id: Parent folder ID (default: root)

        Returns:
            {"ok": True, "document": {...}} or error response
        """
        creds = await self.get_credentials(None)
        if not creds:
            return {"ok": False, "needsReauth": True, "error": "Unable to authenticate"}

        try:
            service = build("drive", "v3", credentials=creds, cache_discovery=False)

            file_metadata = {
                "name": name,
                "mimeType": "application/vnd.google-apps.document",
                "parents": [parent_id]
            }

            doc = service.files().create(
                body=file_metadata,
                fields="id, name, webViewLink"
            ).execute()

            return {
                "ok": True,
                "document": {
                    "id": doc["id"],
                    "name": doc["name"],
                    "webViewLink": doc.get("webViewLink")
                }
            }

        except HttpError as e:
            return self._handle_api_error(e)
        except Exception as e:
            logger.error(f"create_doc error: {e}")
            return {"ok": False, "error": str(e)}

    async def get_doc_content(self, doc_id: str) -> dict:
        """Export Google Doc content as HTML.

        Args:
            doc_id: Google Doc ID

        Returns:
            {"ok": True, "content": "...", "title": "..."} or error response
        """
        creds = await self.get_credentials(None)
        if not creds:
            return {"ok": False, "needsReauth": True, "error": "Unable to authenticate"}

        try:
            service = build("drive", "v3", credentials=creds, cache_discovery=False)

            # Get document metadata
            metadata = service.files().get(
                fileId=doc_id,
                fields="name, modifiedTime"
            ).execute()

            # Export as HTML
            content = service.files().export(
                fileId=doc_id,
                mimeType="text/html"
            ).execute()

            return {
                "ok": True,
                "content": content.decode("utf-8") if isinstance(content, bytes) else content,
                "title": metadata.get("name"),
                "modifiedTime": metadata.get("modifiedTime")
            }

        except HttpError as e:
            return self._handle_api_error(e)
        except Exception as e:
            logger.error(f"get_doc_content error: {e}")
            return {"ok": False, "error": str(e)}

    async def sync_doc_content(self, doc_id: str, document_db_id: int | None = None) -> dict:
        """Sync Google Doc content to database.

        Args:
            doc_id: Google Doc ID
            document_db_id: Database document ID (optional)

        Returns:
            {"ok": True, "document": {...}} or error response
        """
        # Get doc content
        result = await self.get_doc_content(doc_id)
        if not result.get("ok"):
            return result

        content = result["content"]
        title = result.get("title", "Untitled")
        modified_time = result.get("modifiedTime")

        try:
            # Find or create document in database
            if document_db_id:
                doc = self.db.query(Document).filter(Document.id == document_db_id).first()
                if not doc:
                    return {"ok": False, "error": f"Document {document_db_id} not found"}
            else:
                # Find by google_doc_id or content_id
                doc = self.db.query(Document).filter(
                    (Document.google_doc_id == doc_id) | (Document.content_id == doc_id)
                ).first()

            if doc:
                # Update existing document
                doc.content_html = content
                doc.google_modified_at = modified_time
                doc.title = title
                logger.info(f"Updated document {doc.id} from Google Doc {doc_id}")
            else:
                # Create new document
                doc = Document(
                    title=title,
                    content_html=content,
                    content_id=doc_id,
                    google_modified_at=modified_time,
                    owner_id=self.user.id
                )
                self.db.add(doc)
                logger.info(f"Created new document from Google Doc {doc_id}")

            self.db.commit()
            self.db.refresh(doc)

            return {
                "ok": True,
                "html": doc.content_html,
                "document": {
                    "id": doc.id,
                    "title": doc.title,
                    "contentId": doc.content_id,
                    "modifiedTime": modified_time
                }
            }

        except Exception as e:
            self.db.rollback()
            logger.error(f"sync_doc_content database error: {e}")
            return {"ok": False, "error": str(e)}

    async def move_file(self, file_id: str, new_parent_id: str, old_parent_id: str | None = None) -> dict:
        """Move a file to a different folder.

        Args:
            file_id: File ID to move
            new_parent_id: Destination folder ID
            old_parent_id: Source folder ID (optional, will be fetched if not provided)

        Returns:
            {"ok": True} or error response
        """
        creds = await self.get_credentials(None)
        if not creds:
            return {"ok": False, "needsReauth": True, "error": "Unable to authenticate"}

        try:
            service = build("drive", "v3", credentials=creds, cache_discovery=False)

            # If old_parent_id not provided, fetch it
            if not old_parent_id:
                file_metadata = service.files().get(
                    fileId=file_id,
                    fields="parents"
                ).execute()
                old_parents = file_metadata.get("parents", [])
                old_parent_id = old_parents[0] if old_parents else None

            # Move file
            service.files().update(
                fileId=file_id,
                addParents=new_parent_id,
                removeParents=old_parent_id if old_parent_id else "",
                fields="id, parents"
            ).execute()

            return {"ok": True}

        except HttpError as e:
            return self._handle_api_error(e)
        except Exception as e:
            logger.error(f"move_file error: {e}")
            return {"ok": False, "error": str(e)}

    async def trash_file(self, file_id: str) -> dict:
        """Move a file to trash (soft delete).

        Args:
            file_id: File ID to trash

        Returns:
            {"ok": True} or error response
        """
        creds = await self.get_credentials(None)
        if not creds:
            return {"ok": False, "needsReauth": True, "error": "Unable to authenticate"}

        try:
            service = build("drive", "v3", credentials=creds, cache_discovery=False)

            service.files().update(
                fileId=file_id,
                body={"trashed": True}
            ).execute()

            return {"ok": True}

        except HttpError as e:
            return self._handle_api_error(e)
        except Exception as e:
            logger.error(f"trash_file error: {e}")
            return {"ok": False, "error": str(e)}

    async def upload_file(
        self,
        file_path: str,
        name: str,
        parent_id: str = "root",
        mime_type: str | None = None
    ) -> dict:
        """Upload a file to Google Drive.

        Args:
            file_path: Local file path
            name: File name in Drive
            parent_id: Parent folder ID (default: root)
            mime_type: MIME type (auto-detected if not provided)

        Returns:
            {"ok": True, "file": {...}} or error response
        """
        creds = await self.get_credentials(None)
        if not creds:
            return {"ok": False, "needsReauth": True, "error": "Unable to authenticate"}

        try:
            service = build("drive", "v3", credentials=creds, cache_discovery=False)

            file_metadata = {
                "name": name,
                "parents": [parent_id]
            }

            media = MediaFileUpload(file_path, mimetype=mime_type, resumable=True)

            uploaded_file = service.files().create(
                body=file_metadata,
                media_body=media,
                fields="id, name, webViewLink, mimeType"
            ).execute()

            return {
                "ok": True,
                "file": {
                    "id": uploaded_file["id"],
                    "name": uploaded_file["name"],
                    "webViewLink": uploaded_file.get("webViewLink"),
                    "mimeType": uploaded_file.get("mimeType")
                }
            }

        except HttpError as e:
            return self._handle_api_error(e)
        except Exception as e:
            logger.error(f"upload_file error: {e}")
            return {"ok": False, "error": str(e)}


async def google_drive_handler(body: dict, db: Session, user: User | None) -> dict:
    """Multi-action handler for Google Drive operations.

    Actions: list_folder, create_folder, create_doc, get_doc_content,
             sync_doc_content, move_file, trash_file, upload_file

    The frontend passes the Google access token via x-google-token header,
    which gets injected into body["_google_access_token"] by the functions router.
    """
    if not user:
        return {"ok": False, "error": "Authentication required"}

    action = body.get("action")
    if not action:
        return {"ok": False, "error": "Action parameter required"}

    # Extract Google access token (passed from x-google-token header)
    access_token = body.get("_google_access_token")

    # Initialize service
    service = GoogleDriveService(db, user)

    # Save the original get_credentials before monkey-patching so the lambda
    # delegates to the real implementation instead of recursing into itself.
    _original_get_credentials = service.get_credentials

    async def _get_creds_with_frontend_token(access_token_value):
        return await _original_get_credentials(access_token_value)

    # Patch service to always pass the frontend-supplied access token
    service.get_credentials = lambda _ignored: _get_creds_with_frontend_token(access_token)

    # Dispatch to appropriate handler
    if action == "list_folder":
        folder_id = body.get("folderId", "root")
        return await service.list_folder(folder_id)

    elif action == "create_folder":
        name = body.get("name")
        parent_id = body.get("parentId", "root")
        if not name:
            return {"ok": False, "error": "Folder name required"}
        return await service.create_folder(name, parent_id)

    elif action == "create_doc":
        name = body.get("name")
        parent_id = body.get("parentId", "root")
        if not name:
            return {"ok": False, "error": "Document name required"}
        return await service.create_doc(name, parent_id)

    elif action == "get_doc_content":
        doc_id = body.get("docId")
        if not doc_id:
            return {"ok": False, "error": "Document ID required"}
        return await service.get_doc_content(doc_id)

    elif action == "sync_doc_content":
        doc_id = body.get("docId") or body.get("googleDocId") or body.get("google_doc_id")
        document_db_id = body.get("documentId") or body.get("document_id")
        if not doc_id:
            return {"ok": False, "error": "Document ID required"}
        return await service.sync_doc_content(doc_id, document_db_id)

    elif action == "move_file":
        file_id = body.get("fileId")
        new_parent_id = body.get("newParentId")
        old_parent_id = body.get("oldParentId")
        if not file_id or not new_parent_id:
            return {"ok": False, "error": "File ID and new parent ID required"}
        return await service.move_file(file_id, new_parent_id, old_parent_id)

    elif action == "trash_file":
        file_id = body.get("fileId")
        if not file_id:
            return {"ok": False, "error": "File ID required"}
        return await service.trash_file(file_id)

    elif action == "upload_file":
        file_path = body.get("filePath")
        name = body.get("name")
        parent_id = body.get("parentId", "root")
        mime_type = body.get("mimeType")
        if not file_path or not name:
            return {"ok": False, "error": "File path and name required"}
        return await service.upload_file(file_path, name, parent_id, mime_type)

    else:
        return {"ok": False, "error": f"Unknown action: {action}"}


# Keep other existing functions for backward compatibility
async def convert_markdown_to_gdoc(body: dict, db: Session, user: User | None) -> dict:
    """Convert Markdown to Google Doc."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    # TODO: Implement markdown conversion using Drive API
    return {
        "ok": False,
        "error": "Not yet implemented"
    }


async def discover_drive_structure(body: dict, db: Session, user: User | None) -> dict:
    """Recursively explore Drive folders and return hierarchy.

    Maps the Drive folder tree to acceldocs structure:
      Level 0 (root):     docs here → "General" project
      Level 1 folders:    → Projects
      Level 2+ folders:   → Topics (with parent_id for nesting)
      Google Docs:        → Documents (linked to their parent project/topic)

    Returns a flat list of items with depth + parentDriveId so the
    frontend can build and display the tree.
    """
    if not user:
        return {"ok": False, "error": "Authentication required"}

    folder_id = body.get("folderId") or body.get("folder_id")
    if not folder_id:
        return {"ok": False, "error": "Folder ID required"}

    access_token = body.get("_google_access_token")
    max_depth = int(body.get("maxDepth", 4))

    service = GoogleDriveService(db, user)
    _original_get_credentials = service.get_credentials
    service.get_credentials = lambda _: _original_get_credentials(access_token)

    FOLDER_MIME = "application/vnd.google-apps.folder"
    DOC_MIME = "application/vnd.google-apps.document"

    # BFS to walk the tree
    all_items = []  # {id, name, mimeType, parentDriveId, depth, isFolder, type}
    queue = [(folder_id, 0, None)]  # (drive_folder_id, depth, parentDriveId)

    try:
        while queue:
            current_id, depth, parent_drive_id = queue.pop(0)

            result = await service.list_folder(current_id)
            if not result.get("ok"):
                # If auth error on first call, propagate it
                if depth == 0:
                    return result
                logger.warning("Failed to list folder %s: %s", current_id, result.get("error"))
                continue

            for f in result.get("files", []):
                is_folder = f["mimeType"] == FOLDER_MIME
                is_doc = f["mimeType"] == DOC_MIME

                # Determine what this item maps to in acceldocs
                if is_folder:
                    if depth == 0:
                        item_type = "project"
                    else:
                        item_type = "topic"
                elif is_doc:
                    item_type = "document"
                else:
                    # Skip non-Google-Doc files (Sheets, PDFs, etc.)
                    continue

                all_items.append({
                    "id": f["id"],
                    "name": f["name"],
                    "mimeType": f["mimeType"],
                    "parentDriveId": current_id,
                    "depth": depth,
                    "isFolder": is_folder,
                    "type": item_type,  # "project", "topic", or "document"
                    "modifiedTime": f.get("modifiedTime"),
                })

                # Queue subfolders for scanning (respect depth limit)
                if is_folder and depth < max_depth:
                    queue.append((f["id"], depth + 1, current_id))

        # Separate into categories for easier frontend consumption
        projects = [i for i in all_items if i["type"] == "project"]
        topics = [i for i in all_items if i["type"] == "topic"]
        documents = [i for i in all_items if i["type"] == "document"]

        return {
            "ok": True,
            "rootFolderId": folder_id,
            "items": all_items,
            "summary": {
                "projects": len(projects),
                "topics": len(topics),
                "documents": len(documents),
                "total": len(all_items),
            }
        }

    except Exception as e:
        logger.exception("discover_drive_structure error: %s", e)
        return {"ok": False, "error": str(e)}


async def import_markdown(body: dict, db: Session, user: User | None) -> dict:
    """Batch import markdown files."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    # TODO: Implement batch markdown import
    return {
        "ok": False,
        "error": "Not yet implemented"
    }


async def store_refresh_token(body: dict, db: Session, user: User | None) -> dict:
    """Persist Google refresh token.

    Note: This is now handled by the OAuth callback in auth/routes.py
    """
    return {
        "ok": False,
        "error": "Deprecated - use OAuth flow instead"
    }


async def sync_drive_permissions(body: dict, db: Session, user: User | None) -> dict:
    """Sync Drive folder permissions."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    # TODO: Implement permissions sync
    return {
        "ok": False,
        "error": "Not yet implemented"
    }
