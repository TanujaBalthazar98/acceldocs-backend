"""Tests for RBAC module — validates parity with TypeScript rbac.ts."""

from app.lib.rbac import (
    get_assignable_roles,
    get_drive_role,
    get_permissions,
    is_higher_role,
)


def test_owner_has_all_permissions():
    p = get_permissions("owner")
    assert p.can_view is True
    assert p.can_edit is True
    assert p.can_publish is True
    assert p.can_manage_members is True
    assert p.can_delete_project is True
    assert p.can_edit_visibility is True


def test_admin_matches_owner():
    owner = get_permissions("owner")
    admin = get_permissions("admin")
    # Admin should have identical permissions to owner
    assert owner == admin


def test_editor_permissions():
    p = get_permissions("editor")
    assert p.can_edit is True
    assert p.can_publish is True
    assert p.can_edit_project_settings is True
    # Editor cannot manage members or delete project
    assert p.can_manage_members is False
    assert p.can_delete_project is False
    assert p.can_share_drive is False
    assert p.can_edit_visibility is False


def test_reviewer_permissions():
    p = get_permissions("reviewer")
    assert p.can_view is True
    assert p.can_view_draft is True
    assert p.can_comment_drive is True
    assert p.can_view_audit_logs is True
    # Cannot edit or publish
    assert p.can_edit is False
    assert p.can_publish is False


def test_viewer_permissions():
    p = get_permissions("viewer")
    assert p.can_view is True
    assert p.can_view_published is True
    assert p.can_view_draft is False
    assert p.can_edit is False
    assert p.can_publish is False


def test_org_owner_override():
    p = get_permissions("viewer", is_org_owner=True)
    assert p.can_edit is True
    assert p.can_publish is True
    assert p.can_manage_members is True


def test_unknown_role_gets_no_permissions():
    p = get_permissions("nonexistent")
    assert p.can_view is False
    assert p.can_edit is False

    p2 = get_permissions(None)
    assert p2.can_view is False


def test_is_higher_role():
    assert is_higher_role("admin", "editor") is True
    assert is_higher_role("viewer", "admin") is False
    assert is_higher_role("owner", "admin") is True
    assert is_higher_role(None, "viewer") is False


def test_assignable_roles():
    assert get_assignable_roles("admin") == ["editor", "reviewer", "viewer"]
    assert get_assignable_roles(None, is_org_owner=True) == [
        "admin", "editor", "reviewer", "viewer"
    ]
    assert get_assignable_roles("editor") == []
    assert get_assignable_roles("viewer") == []


def test_drive_role_mapping():
    assert get_drive_role("owner") == "writer"
    assert get_drive_role("admin") == "writer"
    assert get_drive_role("editor") == "writer"
    assert get_drive_role("reviewer") == "commenter"
    assert get_drive_role("viewer") == "reader"
    assert get_drive_role(None) is None
