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


def _safe_int(val) -> int | None:
    """Safely cast a value to int, returning None on failure."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None
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
                orderBy="folder,name",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
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
                fields="id, name, webViewLink",
                supportsAllDrives=True,
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
                fields="id, name, webViewLink",
                supportsAllDrives=True,
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
                fields="name, modifiedTime",
                supportsAllDrives=True,
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
                    fields="parents",
                    supportsAllDrives=True,
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
        parent_id = body.get("parentId") or body.get("parentFolderId") or "root"
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
    """Convert Markdown content to a new Google Doc in the specified Drive folder.

    Accepts markdown, converts it to HTML, then uploads via the Drive API
    using multipart upload with mimeType conversion (text/html → Google Docs).

    Returns the created Google Doc's ID so the caller can link it to a
    Document record.
    """
    if not user:
        return {"ok": False, "error": "Authentication required"}

    markdown_content = body.get("markdownContent", "")
    title = body.get("title", "Untitled")
    folder_id = body.get("folderId")
    access_token = body.get("accessToken") or body.get("_google_access_token")

    if not access_token:
        return {"ok": False, "error": "Google access token required"}

    if not markdown_content.strip():
        return {"ok": False, "error": "No markdown content provided"}

    try:
        import markdown as _md
        import json as _json

        # Convert markdown → HTML
        html_body = _md.markdown(
            markdown_content,
            extensions=["tables", "fenced_code", "codehilite", "toc", "attr_list"],
        )
        # Wrap in a minimal HTML document so Drive renders it properly
        html_content = (
            f"<!DOCTYPE html><html><head>"
            f"<meta charset='utf-8'><title>{title}</title></head>"
            f"<body>{html_body}</body></html>"
        )

        # Build multipart upload request for Drive API
        # Metadata part
        metadata = {
            "name": title,
            "mimeType": "application/vnd.google-apps.document",
        }
        if folder_id:
            metadata["parents"] = [folder_id]

        import requests

        boundary = "----DocspeareBoundary"
        body_parts = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{_json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: text/html; charset=UTF-8\r\n\r\n"
            f"{html_content}\r\n"
            f"--{boundary}--"
        )

        resp = requests.post(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            data=body_parts.encode("utf-8"),
            timeout=30,
        )

        if resp.status_code not in (200, 201):
            logger.error("Drive API error creating doc: %s %s", resp.status_code, resp.text[:500])
            return {"ok": False, "error": f"Drive API error: {resp.status_code}"}

        created = resp.json()
        doc_id = created.get("id")
        if not doc_id:
            return {"ok": False, "error": "Drive returned no file ID"}

        logger.info("Created Google Doc '%s' (id=%s) in folder %s", title, doc_id, folder_id)
        return {"ok": True, "documentId": doc_id, "googleDocId": doc_id}

    except Exception as exc:
        logger.exception("convert_markdown_to_gdoc failed for '%s'", title)
        return {"ok": False, "error": str(exc)}


async def discover_drive_structure(body: dict, db: Session, user: User | None) -> dict:
    """Recursively explore Drive folders and return hierarchy.

    Maps the Drive folder tree to acceldocs structure:
      Level 0 (root):     docs here → "General" project
      Level 1 folders:    → Projects
      Level 2 folders:    → Sub-projects (projects with parent_id)
      Level 3+ folders:   → Topics (with parent_id for nesting)
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
                    elif depth == 1:
                        item_type = "subproject"
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
        subprojects_raw = [i for i in all_items if i["type"] == "subproject"]
        topics_raw = [i for i in all_items if i["type"] == "topic"]
        documents_raw = [i for i in all_items if i["type"] == "document"]

        # Build lookup: folder_id → count of direct child docs
        doc_count_by_parent: dict[str, int] = {}
        for d in documents_raw:
            pid = d["parentDriveId"]
            doc_count_by_parent[pid] = doc_count_by_parent.get(pid, 0) + 1

        # Shape subprojects and documents in the format DriveDiscoveryDialog expects
        subprojects = [
            {
                "id": s["id"],
                "name": s["name"],
                "docCount": doc_count_by_parent.get(s["id"], 0),
            }
            for s in subprojects_raw
        ]
        documents = [
            {
                "id": d["id"],
                "name": d["name"],
                "folderId": d["parentDriveId"],
            }
            for d in documents_raw
        ]
        topics = [
            {
                "id": t["id"],
                "name": t["name"],
                "parentId": t.get("parentDriveId"),
                "driveParentId": t["parentDriveId"],
                "docCount": doc_count_by_parent.get(t["id"], 0),
            }
            for t in topics_raw
        ]

        return {
            "ok": True,
            "rootFolderId": folder_id,
            "subprojects": subprojects,
            "documents": documents,
            "topics": topics,
            "items": all_items,
            "summary": {
                "projects": len(projects),
                "subprojects": len(subprojects),
                "topics": len(topics),
                "documents": len(documents),
                "total": len(all_items),
            }
        }

    except Exception as e:
        logger.exception("discover_drive_structure error: %s", e)
        return {"ok": False, "error": str(e)}


async def import_markdown(body: dict, db: Session, user: User | None) -> dict:
    """Batch import markdown files.

    Accepts a list of files (each with path and content) and creates
    a Document record for each one. If a Google access token is
    available, also creates a Google Doc for each file.

    Body keys:
      files: list of { path: str, content: str, targetTopicId?: int }
      projectId: int
      projectVersionId: int | None
      organizationId: int | None
      parentTopicId: int | None
      jobId: str | None  (for batched imports)
    """
    if not user:
        return {"ok": False, "error": "Authentication required"}

    import uuid as _uuid
    from app.models import Document, Project, ProjectVersion, Topic

    files = body.get("files", [])
    if not files:
        return {"ok": False, "error": "No files to import"}

    project_id = _safe_int(body.get("projectId"))
    project_version_id = _safe_int(body.get("projectVersionId"))
    parent_topic_id = _safe_int(body.get("parentTopicId"))
    google_token = body.get("_google_access_token")
    job_id = body.get("jobId") or str(_uuid.uuid4())

    if not project_id:
        return {"ok": False, "error": "projectId required"}

    # Verify project exists
    project = db.get(Project, project_id)
    if not project:
        return {"ok": False, "error": "Project not found"}

    # Resolve project slug chain
    resolved_project = ""
    proj = project
    parts: list[str] = []
    while proj is not None:
        parts.append(proj.slug or proj.name.lower().replace(" ", "-"))
        proj = proj.parent
    parts.reverse()
    resolved_project = "/".join(parts)

    # Resolve version
    resolved_version = ""
    if project_version_id:
        pv = db.get(ProjectVersion, project_version_id)
        if pv:
            resolved_version = pv.slug or pv.name

    # Resolve parent topic section path
    resolved_section = ""
    if parent_topic_id:
        topic = db.get(Topic, parent_topic_id)
        if topic:
            topic_parts: list[str] = []
            t = topic
            while t is not None:
                topic_parts.append(t.slug or t.name.lower().replace(" ", "-"))
                t = t.parent
            topic_parts.reverse()
            resolved_section = "/".join(topic_parts)

    # Optional: create Google Docs via Drive API
    drive_service = None
    if google_token:
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            creds = Credentials(token=google_token)
            drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
        except Exception as e:
            logger.warning("Could not init Drive service for import: %s", e)

    imported = 0
    errors_list: list[str] = []

    for f in files:
        file_path = f.get("path", "untitled")
        content = f.get("content", "")
        target_topic_id = _safe_int(f.get("targetTopicId")) or parent_topic_id

        # Derive title from file path
        file_name = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
        title = file_name.replace(".md", "").replace(".markdown", "").replace("-", " ").replace("_", " ")
        slug = file_name.replace(".md", "").replace(".markdown", "").lower().replace(" ", "-")

        google_doc_id = None

        # Try creating a Google Doc if we have a Drive service
        if drive_service and content:
            try:
                # Create a Google Doc
                file_metadata = {"name": title, "mimeType": "application/vnd.google-apps.document"}
                # If there's a parent folder, put the doc there
                if project and hasattr(project, "drive_folder_id") and project.drive_folder_id:
                    file_metadata["parents"] = [project.drive_folder_id]

                created_file = drive_service.files().create(
                    body=file_metadata, fields="id"
                ).execute()
                google_doc_id = created_file.get("id")

                if google_doc_id:
                    # Update the doc content via Docs API
                    try:
                        from googleapiclient.discovery import build as build_api
                        docs_service = build_api("docs", "v1", credentials=creds, cache_discovery=False)
                        # Insert the markdown as plain text (Drive will format it)
                        docs_service.documents().batchUpdate(
                            documentId=google_doc_id,
                            body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]}
                        ).execute()
                    except Exception as e:
                        logger.warning("Could not insert content into Google Doc %s: %s", google_doc_id, e)

            except Exception as e:
                logger.warning("Could not create Google Doc for %s: %s", title, e)

        try:
            # Convert markdown to HTML for storage
            content_html = None
            if content:
                try:
                    import markdown as _md
                    content_html = _md.markdown(content, extensions=["tables", "fenced_code"])
                except ImportError:
                    # Wrap raw markdown in pre tags as fallback
                    content_html = f"<pre>{content}</pre>"

            document = Document(
                google_doc_id=google_doc_id,
                title=title,
                slug=slug,
                project=resolved_project,
                version=resolved_version,
                section=resolved_section,
                visibility="internal",
                status="draft",
                project_id=project_id,
                project_version_id=project_version_id,
                topic_id=target_topic_id,
                owner_id=user.id,
                content_html=content_html,
            )
            db.add(document)
            db.flush()
            imported += 1
        except Exception as e:
            logger.error("Failed to create document for %s: %s", title, e)
            errors_list.append(f"{title}: {e}")

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        return {"ok": False, "error": f"Database commit failed: {e}"}

    return {
        "ok": True,
        "jobId": job_id,
        "imported": imported,
        "errors": errors_list,
        "total": len(files),
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
