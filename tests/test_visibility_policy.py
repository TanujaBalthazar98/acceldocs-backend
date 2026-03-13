"""Tests for clean-arch visibility policy service."""

from app.models import ExternalAccessGrant, OrgRole, Organization, User
from app.services.visibility import (
    build_viewer_scope,
    can_view_visibility,
    normalize_visibility,
    resolve_effective_visibility,
)


def test_visibility_policy_anonymous_access(db):
    org = Organization(name="Vis Org", slug="vis-org")
    db.add(org)
    db.commit()

    scope = build_viewer_scope(db, org.id, None)
    assert can_view_visibility(scope, "public") is True
    assert can_view_visibility(scope, "internal") is False
    assert can_view_visibility(scope, "external") is False


def test_visibility_policy_org_member_access(db):
    user = User(google_id="vis-user-1", email="member@example.com", name="Member")
    org = Organization(name="Vis Org 2", slug="vis-org-2")
    db.add_all([user, org])
    db.flush()
    db.add(OrgRole(organization_id=org.id, user_id=user.id, role="editor"))
    db.commit()

    scope = build_viewer_scope(db, org.id, user)
    assert scope.is_org_member is True
    assert can_view_visibility(scope, "public") is True
    assert can_view_visibility(scope, "internal") is True
    assert can_view_visibility(scope, "external") is True


def test_visibility_policy_external_grant_access(db):
    user = User(google_id="vis-user-2", email="external@example.com", name="External")
    org = Organization(name="Vis Org 3", slug="vis-org-3")
    db.add_all([user, org])
    db.flush()
    db.add(
        ExternalAccessGrant(
            organization_id=org.id,
            email="external@example.com",
            is_active=True,
        )
    )
    db.commit()

    scope = build_viewer_scope(db, org.id, user)
    assert scope.is_org_member is False
    assert scope.is_external_allowed is True
    assert can_view_visibility(scope, "public") is True
    assert can_view_visibility(scope, "internal") is False
    assert can_view_visibility(scope, "external") is True


def test_visibility_policy_org_owner_is_internal_member(db):
    user = User(google_id="vis-owner-1", email="owner@example.com", name="Owner")
    org = Organization(name="Owner Org", slug="owner-org", owner_id=1)
    db.add(user)
    db.flush()
    org.owner_id = user.id
    db.add(org)
    db.commit()

    scope = build_viewer_scope(db, org.id, user)
    assert scope.is_org_member is True
    assert can_view_visibility(scope, "internal") is True


def test_visibility_normalization_and_resolution():
    assert normalize_visibility("PUBLIC") == "public"
    assert normalize_visibility("unknown") == "public"
    assert resolve_effective_visibility("internal", None) == "internal"
    assert resolve_effective_visibility("internal", "external") == "external"
