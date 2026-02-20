"""RBAC (Role-Based Access Control) — ported from src/lib/rbac.ts.

Single source of truth for role permissions in the new backend.
"""

from dataclasses import dataclass, field
from typing import Literal

Role = Literal["owner", "admin", "editor", "reviewer", "viewer"]
DriveRole = Literal["writer", "commenter", "reader"] | None

ROLE_HIERARCHY: dict[str, int] = {
    "owner": 5,
    "admin": 4,
    "editor": 3,
    "reviewer": 2,
    "viewer": 1,
    "external": 0,
}


@dataclass(frozen=True)
class Permissions:
    # View
    can_view: bool = False
    can_view_published: bool = False
    can_view_draft: bool = False
    # Edit
    can_edit: bool = False
    can_edit_document: bool = False
    can_edit_metadata: bool = False
    # Create / Delete
    can_create_document: bool = False
    can_delete_document: bool = False
    can_create_topic: bool = False
    can_delete_topic: bool = False
    can_delete_project: bool = False
    # Publish
    can_publish: bool = False
    can_unpublish: bool = False
    # Structure
    can_move_topic: bool = False
    can_move_page: bool = False
    # Members
    can_manage_members: bool = False
    can_invite_members: bool = False
    can_remove_members: bool = False
    can_change_roles: bool = False
    # Drive
    can_edit_drive: bool = False
    can_download_drive: bool = False
    can_export_drive: bool = False
    can_share_drive: bool = False
    can_comment_drive: bool = False
    # Audit
    can_view_audit_logs: bool = False
    # Sync
    can_sync_content: bool = False
    # Settings
    can_edit_project_settings: bool = False
    can_edit_visibility: bool = False


@dataclass(frozen=True)
class RoleDefinition:
    name: str
    description: str
    permissions: Permissions
    drive_role: DriveRole


NO_PERMISSIONS = Permissions()

ROLE_DEFINITIONS: dict[str, RoleDefinition] = {
    "owner": RoleDefinition(
        name="Owner",
        description="Full control over workspace, domains, publishing, and access management.",
        permissions=Permissions(
            can_view=True, can_view_published=True, can_view_draft=True,
            can_edit=True, can_edit_document=True, can_edit_metadata=True,
            can_create_document=True, can_delete_document=True,
            can_create_topic=True, can_delete_topic=True, can_delete_project=True,
            can_publish=True, can_unpublish=True,
            can_move_topic=True, can_move_page=True,
            can_manage_members=True, can_invite_members=True,
            can_remove_members=True, can_change_roles=True,
            can_edit_drive=True, can_download_drive=True,
            can_export_drive=True, can_share_drive=True, can_comment_drive=True,
            can_view_audit_logs=True, can_sync_content=True,
            can_edit_project_settings=True, can_edit_visibility=True,
        ),
        drive_role="writer",
    ),
    "admin": RoleDefinition(
        name="Admin",
        description="Can create, edit, delete, and publish documentation. Can manage project members.",
        permissions=Permissions(
            can_view=True, can_view_published=True, can_view_draft=True,
            can_edit=True, can_edit_document=True, can_edit_metadata=True,
            can_create_document=True, can_delete_document=True,
            can_create_topic=True, can_delete_topic=True, can_delete_project=True,
            can_publish=True, can_unpublish=True,
            can_move_topic=True, can_move_page=True,
            can_manage_members=True, can_invite_members=True,
            can_remove_members=True, can_change_roles=True,
            can_edit_drive=True, can_download_drive=True,
            can_export_drive=True, can_share_drive=True, can_comment_drive=True,
            can_view_audit_logs=True, can_sync_content=True,
            can_edit_project_settings=True, can_edit_visibility=True,
        ),
        drive_role="writer",
    ),
    "editor": RoleDefinition(
        name="Editor",
        description="Can create and edit documentation content. Can publish content.",
        permissions=Permissions(
            can_view=True, can_view_published=True, can_view_draft=True,
            can_edit=True, can_edit_document=True, can_edit_metadata=True,
            can_create_document=True, can_delete_document=True,
            can_create_topic=True, can_delete_topic=True,
            can_publish=True, can_unpublish=True,
            can_move_topic=True, can_move_page=True,
            can_edit_drive=True, can_download_drive=True,
            can_export_drive=True, can_comment_drive=True,
            can_sync_content=True, can_edit_project_settings=True,
        ),
        drive_role="writer",
    ),
    "reviewer": RoleDefinition(
        name="Reviewer",
        description="Can comment and suggest changes. Cannot publish.",
        permissions=Permissions(
            can_view=True, can_view_published=True, can_view_draft=True,
            can_comment_drive=True, can_view_audit_logs=True,
        ),
        drive_role="commenter",
    ),
    "viewer": RoleDefinition(
        name="Viewer",
        description="Read-only access to published documentation.",
        permissions=Permissions(
            can_view=True, can_view_published=True,
        ),
        drive_role="reader",
    ),
}


def get_permissions(role: str | None, is_org_owner: bool = False) -> Permissions:
    """Get permissions for a given role."""
    if is_org_owner:
        return ROLE_DEFINITIONS["owner"].permissions
    if not role or role not in ROLE_DEFINITIONS:
        return NO_PERMISSIONS
    return ROLE_DEFINITIONS[role].permissions


def is_higher_role(role_a: str | None, role_b: str | None) -> bool:
    """Check if role_a has higher privileges than role_b."""
    level_a = ROLE_HIERARCHY.get(role_a or "", -1)
    level_b = ROLE_HIERARCHY.get(role_b or "", -1)
    return level_a > level_b


def get_assignable_roles(assigner_role: str | None, is_org_owner: bool = False) -> list[str]:
    """Get roles that can be assigned by the given role."""
    if is_org_owner:
        return ["admin", "editor", "reviewer", "viewer"]
    level = ROLE_HIERARCHY.get(assigner_role or "", -1)
    if level < ROLE_HIERARCHY["admin"]:
        return []
    return ["editor", "reviewer", "viewer"]


def get_drive_role(role: str | None) -> DriveRole:
    """Map application role to Google Drive permission role."""
    if not role or role not in ROLE_DEFINITIONS:
        return None
    return ROLE_DEFINITIONS[role].drive_role


def get_permissions_for_role(role: str | None) -> set[str]:
    """Get string-based permissions for a role (for API guards).

    Returns a set of permission strings like:
    - users.view, users.create, users.edit, users.delete, users.manage_roles
    - documents.view, documents.edit, documents.publish
    - etc.
    """
    perms = get_permissions(role)
    permission_strings = set()

    # User management permissions
    if perms.can_view:
        permission_strings.add("users.view")
    if perms.can_manage_members:
        permission_strings.update(["users.create", "users.edit", "users.delete", "users.manage_roles"])
    elif perms.can_invite_members:
        permission_strings.add("users.create")

    # Document permissions
    if perms.can_view_published or perms.can_view_draft:
        permission_strings.add("documents.view")
    if perms.can_edit_document:
        permission_strings.add("documents.edit")
    if perms.can_publish:
        permission_strings.add("documents.publish")
    if perms.can_delete_document:
        permission_strings.add("documents.delete")

    # Sync permissions
    if perms.can_sync_content:
        permission_strings.add("sync.trigger")

    # Settings permissions
    if perms.can_edit_project_settings:
        permission_strings.add("settings.edit")

    return permission_strings
