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

    selected_org_id = request.headers.get("x-org-id")
    if selected_org_id and isinstance(body, dict):
        body["_x_org_id"] = selected_org_id

    # Extract Google access token from header (for Drive operations)
    google_token = request.headers.get("x-google-token")
    if google_token and function_name in (
        "google-drive", "discover-drive-structure",
        "import-markdown", "convert-markdown-to-gdoc",
        "create-document", "create-topic", "create-project",
        "approvals-action",
    ):
        body["_google_access_token"] = google_token

    # Import service modules (lazy import to avoid circular dependencies)
    from app.services import workspace, projects, documents, drive, members, external_access
    from app.api import approvals as approvals_mod

    # Function dispatch table
    handlers = {
        # Workspace (4 functions)
        "ensure-workspace": workspace.ensure_workspace,
        "get-organization": workspace.get_organization,
        "update-organization": workspace.update_organization,
        "search-organizations": workspace.search_organizations,

        # Projects
        "list-projects": projects.list_projects,
        "create-project": projects.create_project,
        "update-project-settings": projects.update_project_settings,
        "get-project-settings": projects.get_project_settings,
        "delete-project": projects.delete_project,
        # Project member / external invite
        "invite-to-project": projects.invite_to_project,
        "list-project-members": projects.list_project_members_for_project,
        "remove-project-member": projects.remove_project_member,
        "list-external-access": external_access.list_external_access,
        "grant-external-access": external_access.grant_external_access,
        "revoke-external-access": external_access.revoke_external_access,

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

        # Members (11 functions)
        "create-invitation": members.create_invitation,
        "create-project-invitation": members.create_project_invitation,
        "remove-project-invitation": members.remove_project_invitation,
        "update-member-role": members.update_member_role,
        "update-project-member-role": members.update_project_member_role,
        "remove-project-member": members.remove_project_member,
        "list-join-requests": members.list_join_requests,
        "create-join-request": members.create_join_request,
        "approve-join-request": members.approve_join_request,
        "reject-join-request": members.reject_join_request,
        "get-project-share": members.get_project_share,

        # Maintenance (3 functions)
        "sync-drive-permissions": drive.sync_drive_permissions,
        "normalize-structure": projects.normalize_structure,
        "repair-hierarchy": projects.repair_hierarchy,

        # AI (1 function)
        "docs-ai-assistant": documents.docs_ai_assistant,

        # Approvals (5 functions)
        "approvals-pending": approvals_mod.approvals_pending_fn,
        "approvals-count": approvals_mod.approvals_count_fn,
        "approvals-history": approvals_mod.approvals_history_fn,
        "approvals-my-submissions": approvals_mod.approvals_my_submissions_fn,
        "approvals-action": approvals_mod.approvals_action_fn,
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


@router.post("/api/rpc/{rpc_name}")
async def invoke_rpc(
    rpc_name: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """RPC endpoint used by the frontend for Supabase-style calls.

    Translates underscore names to hyphenated function names and strips
    leading underscores from parameter keys (e.g. _request_id → requestId).
    """
    try:
        raw = await request.json()
    except Exception:
        raw = {}

    # Convert _snake_case params to camelCase: _request_id → requestId
    body: dict = {}
    for key, value in raw.items():
        clean = key.lstrip("_")
        # Convert snake_case to camelCase
        parts = clean.split("_")
        camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
        body[camel] = value

    # Map underscore rpc name to hyphenated function name
    function_name = rpc_name.replace("_", "-")

    # Re-use the same dispatch by calling invoke_function logic
    from app.services import workspace, projects, documents, drive, members

    handlers = {
        "approve-join-request": members.approve_join_request,
        "reject-join-request": members.reject_join_request,
        "create-join-request": members.create_join_request,
        "list-join-requests": members.list_join_requests,
    }

    handler = handlers.get(function_name)
    if not handler:
        raise HTTPException(status_code=404, detail=f"RPC '{rpc_name}' not found")

    try:
        return await handler(body=body, db=db, user=user)
    except Exception as e:
        return {"ok": False, "error": str(e)}
