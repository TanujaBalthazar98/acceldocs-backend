"""Workspace and organization management functions."""

from sqlalchemy.orm import Session
from sqlalchemy import or_
from app.models import User, Organization, OrgRole


async def ensure_workspace(body: dict, db: Session, user: User | None) -> dict:
    """Auto-create organization for authenticated user if none exists.

    Returns:
        {"ok": True, "organization": {...}, "members": [...]}
    """
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        # Check if user already has an org role
        org_role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()

        if org_role:
            # User already has an organization
            org = db.query(Organization).filter(Organization.id == org_role.organization_id).first()
            if org:
                # Get all members
                members = db.query(OrgRole).filter(OrgRole.organization_id == org.id).all()
                member_list = [
                    {
                        "id": m.user_id,
                        "role": m.role,
                        "email": db.query(User).filter(User.id == m.user_id).first().email
                    }
                    for m in members
                ]

                return {
                    "ok": True,
                    "organization": {
                        "id": org.id,
                        "name": org.name,
                        "slug": org.slug,
                        "domain": org.domain,
                    },
                    "members": member_list
                }

        # Create new organization for this user
        org = Organization(
            name=f"{user.name or user.email}'s Workspace",
            slug=user.email.split("@")[0],
            owner_id=user.id
        )
        db.add(org)
        db.flush()  # Get org.id without committing

        # Create owner role
        org_role = OrgRole(
            organization_id=org.id,
            user_id=user.id,
            role="owner"
        )
        db.add(org_role)
        db.commit()

        return {
            "ok": True,
            "organization": {
                "id": org.id,
                "name": org.name,
                "slug": org.slug,
                "domain": org.domain,
            },
            "members": [
                {
                    "id": user.id,
                    "role": "owner",
                    "email": user.email
                }
            ]
        }

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def get_organization(body: dict, db: Session, user: User | None) -> dict:
    """Fetch organization with members list.

    Returns:
        {"ok": True, "id": int, "name": str, "members": [...], ...}
    """
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        # Find user's organization
        org_role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
        if not org_role:
            return {"ok": False, "error": "User is not a member of any organization"}

        org = db.query(Organization).filter(Organization.id == org_role.organization_id).first()
        if not org:
            return {"ok": False, "error": "Organization not found"}

        # Get all members
        members = db.query(OrgRole).filter(OrgRole.organization_id == org.id).all()
        member_list = []
        for m in members:
            member_user = db.query(User).filter(User.id == m.user_id).first()
            if member_user:
                member_list.append({
                    "id": member_user.id,
                    "role": m.role,
                    "email": member_user.email,
                    "name": member_user.name
                })

        return {
            "ok": True,
            "id": org.id,
            "name": org.name,
            "slug": org.slug,
            "domain": org.domain,
            "custom_docs_domain": org.custom_docs_domain,
            "subdomain": org.subdomain,
            "logo_url": org.logo_url,
            "tagline": org.tagline,
            "primary_color": org.primary_color,
            "secondary_color": org.secondary_color,
            "accent_color": org.accent_color,
            "font_heading": org.font_heading,
            "font_body": org.font_body,
            "custom_css": org.custom_css,
            "hero_title": org.hero_title,
            "hero_description": org.hero_description,
            "show_search_on_landing": org.show_search_on_landing,
            "show_featured_projects": org.show_featured_projects,
            "custom_links": org.custom_links,
            "mcp_enabled": org.mcp_enabled,
            "openapi_spec_json": org.openapi_spec_json,
            "openapi_spec_url": org.openapi_spec_url,
            "drive_folder_id": org.drive_folder_id,
            "owner_id": org.owner_id,
            "members": member_list
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}


async def update_organization(body: dict, db: Session, user: User | None) -> dict:
    """Update organization settings.

    Args:
        body: {"id": int, "name": str, "logo_url": str, ...}

    Returns:
        {"ok": True, "organization": {...}}
    """
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        org_id = body.get("id") or body.get("organizationId")
        if not org_id:
            return {"ok": False, "error": "Organization ID required"}

        # Cast to int for PostgreSQL type safety
        try:
            org_id = int(org_id)
        except (ValueError, TypeError):
            return {"ok": False, "error": "Invalid organization ID"}

        # Check if user is owner/admin
        org_role = db.query(OrgRole).filter(
            OrgRole.user_id == user.id,
            OrgRole.organization_id == org_id
        ).first()

        if not org_role or org_role.role not in ["owner", "admin"]:
            return {"ok": False, "error": "Insufficient permissions"}

        org = db.query(Organization).filter(Organization.id == org_id).first()
        if not org:
            return {"ok": False, "error": "Organization not found"}

        # Update fields — also check nested "data" dict that some callers use
        updatable_fields = [
            "name", "slug", "domain", "custom_docs_domain", "subdomain",
            "logo_url", "tagline", "primary_color", "secondary_color", "accent_color",
            "font_heading", "font_body", "custom_css", "hero_title", "hero_description",
            "show_search_on_landing", "show_featured_projects", "custom_links",
            "mcp_enabled", "openapi_spec_json", "openapi_spec_url", "drive_folder_id"
        ]

        # Merge nested "data" dict into body so callers can use either format
        merged = {**body}
        if isinstance(body.get("data"), dict):
            merged.update(body["data"])

        for field in updatable_fields:
            if field in merged:
                setattr(org, field, merged[field])

        db.commit()

        return {
            "ok": True,
            "organization": {
                "id": org.id,
                "name": org.name,
                "slug": org.slug,
                "domain": org.domain,
            }
        }

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def search_organizations(body: dict, db: Session, user: User | None = None) -> dict:
    """Search for organizations by name or domain.

    This is used during sign-up to find existing organizations.
    Does not require authentication to allow pre-signup search.

    Args:
        body: {"query": str} - search query

    Returns:
        {"ok": True, "organizations": [{id, name, domain, member_count}, ...]}
    """
    try:
        query = body.get("query", "").strip()
        if not query or len(query) < 2:
            return {"ok": False, "error": "Search query must be at least 2 characters"}

        # Search by name or domain (case-insensitive)
        orgs = db.query(Organization).filter(
            or_(
                Organization.name.ilike(f"%{query}%"),
                Organization.domain.ilike(f"%{query}%")
            )
        ).limit(10).all()

        results = []
        for org in orgs:
            # Count members
            member_count = db.query(OrgRole).filter(OrgRole.organization_id == org.id).count()

            results.append({
                "id": org.id,
                "name": org.name,
                "domain": org.domain,
                "member_count": member_count
            })

        return {
            "ok": True,
            "organizations": results
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}
