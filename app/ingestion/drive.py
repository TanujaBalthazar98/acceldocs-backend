"""Google Drive connector.

Supports three auth modes (in order):
1) Service account key JSON (if available)
2) User OAuth token file (recommended when key creation is blocked)
3) Application Default Credentials (for GCP runtime)
"""

import logging
from dataclasses import dataclass, field

import google.auth
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build

from app.config import settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
]

DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"

MAX_DEPTH = 6


@dataclass
class DriveFile:
    id: str
    name: str
    mime_type: str
    modified_time: str | None = None
    created_time: str | None = None
    parent_folder_id: str | None = None


@dataclass
class DriveFolder:
    id: str
    name: str
    parent_id: str | None
    path: str
    depth: int
    children: list["DriveFolder"] = field(default_factory=list)


@dataclass
class DriveTree:
    root: DriveFolder
    folders: list[DriveFolder]
    files: list[DriveFile]


def _get_credentials():
    """Resolve credentials in priority order: SA key -> OAuth token -> ADC."""
    sa_path = settings.service_account_path
    if sa_path.exists():
        logger.info("Using service account credentials: %s", sa_path)
        return ServiceAccountCredentials.from_service_account_file(str(sa_path), scopes=SCOPES)

    token_path = settings.oauth_token_path
    if token_path.exists():
        logger.info("Using OAuth user token file: %s", token_path)
        creds = UserCredentials.from_authorized_user_file(str(token_path), SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    # Final fallback: Application Default Credentials (GCP runtime)
    creds, _ = google.auth.default(scopes=SCOPES)
    if creds:
        logger.info("Using Application Default Credentials")
        return creds

    raise FileNotFoundError(
        "No Google credentials available. Provide one of:\n"
        "- GOOGLE_SERVICE_ACCOUNT_FILE (if allowed), or\n"
        "- GOOGLE_OAUTH_TOKEN_FILE (recommended for local), or\n"
        "- ADC in cloud runtime."
    )


def _get_service():
    """Build a Google Drive API service."""
    creds = _get_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_folder(service, folder_id: str) -> list[DriveFile]:
    """List all files and folders in a Drive folder."""
    items: list[DriveFile] = []
    page_token = None

    while True:
        resp = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, createdTime)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )

        for f in resp.get("files", []):
            items.append(
                DriveFile(
                    id=f["id"],
                    name=f["name"],
                    mime_type=f["mimeType"],
                    modified_time=f.get("modifiedTime"),
                    created_time=f.get("createdTime"),
                )
            )

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return items


def build_folder_tree(
    service, root_folder_id: str, root_name: str = "Root", max_depth: int = MAX_DEPTH
) -> DriveTree:
    """Recursively scan Drive folder structure — BFS traversal."""
    root = DriveFolder(
        id=root_folder_id, name=root_name, parent_id=None, path=root_name, depth=0
    )
    all_folders = [root]
    all_files: list[DriveFile] = []

    queue = [root]
    while queue:
        current = queue.pop(0)
        if current.depth >= max_depth:
            continue

        items = list_folder(service, current.id)
        for item in items:
            if item.mime_type == DRIVE_FOLDER_MIME:
                child = DriveFolder(
                    id=item.id,
                    name=item.name,
                    parent_id=current.id,
                    path=f"{current.path}/{item.name}",
                    depth=current.depth + 1,
                )
                current.children.append(child)
                all_folders.append(child)
                queue.append(child)
            else:
                item.parent_folder_id = current.id
                all_files.append(item)

    return DriveTree(root=root, folders=all_folders, files=all_files)


def export_doc_as_html(service, doc_id: str) -> str:
    """Export a Google Doc as HTML."""
    content = service.files().export(fileId=doc_id, mimeType="text/html").execute()
    if isinstance(content, bytes):
        return content.decode("utf-8")
    return str(content)


def classify_folder(folder: DriveFolder) -> dict[str, str | None]:
    """Classify a folder based on its depth in the tree."""
    parts = folder.path.split("/")
    return {
        "project": parts[1] if len(parts) > 1 else None,
        "version": parts[2] if len(parts) > 2 else None,
        "visibility": parts[3] if len(parts) > 3 else None,
        "section": "/".join(parts[4:]) if len(parts) > 4 else None,
    }


def scan_and_classify(
    root_folder_id: str | None = None,
) -> list[dict]:
    """Full scan: build tree, classify each Google Doc, return metadata list."""
    folder_id = root_folder_id or settings.google_drive_root_folder_id
    if not folder_id:
        raise ValueError("No root folder ID configured. Set GOOGLE_DRIVE_ROOT_FOLDER_ID in .env")

    service = _get_service()
    tree = build_folder_tree(service, folder_id, root_name="Documentation")

    results: list[dict] = []
    for file in tree.files:
        if file.mime_type != GOOGLE_DOC_MIME:
            continue

        parent_folder = _find_parent_folder(file, tree)
        if not parent_folder:
            logger.warning("Could not find parent folder for doc %s (%s)", file.name, file.id)
            continue

        classification = classify_folder(parent_folder)

        results.append(
            {
                "google_doc_id": file.id,
                "title": file.name,
                "project": classification["project"] or "unknown",
                "version": classification["version"] or "v1.0",
                "visibility": (classification["visibility"] or "public").lower(),
                "section": classification["section"],
                "modified_time": file.modified_time,
            }
        )

    logger.info("Scanned %d Google Docs from Drive", len(results))
    return results


def _find_parent_folder(file: DriveFile, tree: DriveTree) -> DriveFolder | None:
    """Find the parent folder for a file using the parent_folder_id set during scan."""
    if not file.parent_folder_id:
        return None
    for folder in tree.folders:
        if folder.id == file.parent_folder_id:
            return folder
    return None
