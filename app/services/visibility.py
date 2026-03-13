"""Central visibility policy for clean-arch docs rendering.

This module is intentionally framework-agnostic so API layers can reuse it
without duplicating authorization rules.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import ExternalAccessGrant, Organization, OrgRole, User

VISIBILITY_PUBLIC = "public"
VISIBILITY_INTERNAL = "internal"
VISIBILITY_EXTERNAL = "external"

VALID_VISIBILITIES = {
    VISIBILITY_PUBLIC,
    VISIBILITY_INTERNAL,
    VISIBILITY_EXTERNAL,
}


def normalize_visibility(value: str | None, default: str = VISIBILITY_PUBLIC) -> str:
    candidate = (value or "").strip().lower()
    if candidate in VALID_VISIBILITIES:
        return candidate
    return default


@dataclass(frozen=True)
class ViewerScope:
    """Resolved viewer access context for one organization."""

    user_id: int | None
    email: str | None
    is_authenticated: bool
    is_org_member: bool
    is_external_allowed: bool


def build_viewer_scope(db: Session, organization_id: int, user: User | None) -> ViewerScope:
    if not user:
        return ViewerScope(
            user_id=None,
            email=None,
            is_authenticated=False,
            is_org_member=False,
            is_external_allowed=False,
        )

    has_org_role = (
        db.query(OrgRole)
        .filter(
            OrgRole.organization_id == organization_id,
            OrgRole.user_id == user.id,
        )
        .first()
        is not None
    )
    # Ownership is authoritative even if org_roles is missing/drifted.
    is_org_owner = (
        db.query(Organization)
        .filter(
            Organization.id == organization_id,
            Organization.owner_id == user.id,
        )
        .first()
        is not None
    )
    is_org_member = has_org_role or is_org_owner

    normalized_email = (user.email or "").strip().lower()
    is_external_allowed = False
    if normalized_email:
        is_external_allowed = (
            db.query(ExternalAccessGrant)
            .filter(
                ExternalAccessGrant.organization_id == organization_id,
                ExternalAccessGrant.email == normalized_email,
                ExternalAccessGrant.is_active == True,  # noqa: E712 - SQLAlchemy boolean expression
            )
            .first()
            is not None
        )

    return ViewerScope(
        user_id=user.id,
        email=normalized_email or None,
        is_authenticated=True,
        is_org_member=is_org_member,
        is_external_allowed=is_external_allowed,
    )


def can_view_visibility(scope: ViewerScope, visibility: str | None) -> bool:
    resolved = normalize_visibility(visibility)
    if resolved == VISIBILITY_PUBLIC:
        return True
    if resolved == VISIBILITY_INTERNAL:
        return scope.is_org_member
    if resolved == VISIBILITY_EXTERNAL:
        return scope.is_org_member or scope.is_external_allowed
    return False


def resolve_effective_visibility(
    section_visibility: str | None,
    page_visibility_override: str | None,
) -> str:
    """Resolve final visibility for a page, preferring page override."""
    if page_visibility_override:
        return normalize_visibility(page_visibility_override)
    return normalize_visibility(section_visibility)
