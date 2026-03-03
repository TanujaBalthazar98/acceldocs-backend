"""RPC-style function router matching Strapi's function dispatch pattern."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.auth import get_current_user_optional
from app.models import User

router = APIRouter()


@router.post("/api/functions/{function_name}")
async def invoke_function(
    function_name: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """RPC-style function invocation matching Strapi's pattern.

    Accepts POST requests with JSON body and dispatches to service functions.
    Returns: {"ok": bool, ...data, "error": str | None}
    """

    # Parse request body
    try:
        body = await request.json()
    except Exception:
        body = {}

    # Extract Google access token from header (for Drive operations)
    google_token = request.headers.get("x-google-token")
    if function_name == "google-drive" and google_token:
        body["_google_access_token"] = google_token

    # Import service modules (lazy import to avoid circular dependencies)
    from app.services import workspace, projects, documents, drive, members

    # Function dispatch table
    handlers = {
        # Workspace (4 functions)
        "ensure-workspace": workspace.ensure_workspace,
        "get-organization": workspace.get_organization,
        "update-organization": workspace.update_organization,
        "search-organizations": workspace.search_organizations,

        # Projects (5 functions)
        "list-projects": projects.list_projects,
        "create-project": projects.create_project,
        "update-project-settings": projects.update_project_settings,
        "get-project-settings": projects.get_project_settings,
        "delete-project": projects.delete_project,

        # Versions (2 functions)
        "list-project-versions": projects.list_project_versions,
        "create-project-version": projects.create_project_version,

        # Topics (3 functions)
        "list-topics": projects.list_topics,
        "create-topic": projects.create_topic,
        "delete-topic": projects.delete_topic,

        # Documents (6 functions)
        "list-documents": documents.list_documents,
        "create-document": documents.create_document,
        "get-document": documents.get_document,
        "update-document": documents.update_document,
        "delete-document": documents.delete_document,
        "document-cache": documents.document_cache,

        # Drive (6 functions - note: google-drive is multi-action)
        "google-drive": drive.google_drive_handler,
        "convert-markdown-to-gdoc": drive.convert_markdown_to_gdoc,
        "discover-drive-structure": drive.discover_drive_structure,
        "import-markdown": drive.import_markdown,
        "store-refresh-token": drive.store_refresh_token,

        # Members (8 functions)
        "create-invitation": members.create_invitation,
        "create-project-invitation": members.create_project_invitation,
        "remove-project-invitation": members.remove_project_invitation,
        "update-member-role": members.update_member_role,
        "update-project-member-role": members.update_project_member_role,
        "remove-project-member": members.remove_project_member,
        "list-join-requests": members.list_join_requests,
        "get-project-share": members.get_project_share,

        # Maintenance (3 functions)
        "sync-drive-permissions": drive.sync_drive_permissions,
        "normalize-structure": projects.normalize_structure,
        "repair-hierarchy": projects.repair_hierarchy,

        # AI (1 function)
        "docs-ai-assistant": documents.docs_ai_assistant,
    }

    handler = handlers.get(function_name)
    if not handler:
        raise HTTPException(
            status_code=404,
            detail=f"Function '{function_name}' not found"
        )

    try:
        # Call handler with standard signature
        result = await handler(body=body, db=db, user=user)
        return result
    except Exception as e:
        # Return error in Strapi format
        return {
            "ok": False,
            "error": str(e)
        }
