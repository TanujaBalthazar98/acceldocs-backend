"""Public-facing API endpoints for docs viewer (no auth required)."""

import logging
import re

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.auth import get_current_user_optional
from app.models import (
    Organization, Project, ProjectMember, ProjectVersion, Topic, Document, User,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["public"])


# ---------------------------------------------------------------------------
# Helper: serialise models to dicts
# ---------------------------------------------------------------------------

def _org_dict(org: Organization) -> dict:
    return {
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
        "drive_folder_id": org.drive_folder_id,
    }


def _project_dict(p: Project) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "slug": p.slug,
        "description": p.description,
        "visibility": p.visibility,
        "is_published": p.is_published,
        "drive_folder_id": p.drive_folder_id,
        "drive_parent_id": p.drive_parent_id,
        "organization_id": p.organization_id,
        "parent_id": p.parent_id,
        "show_version_switcher": p.show_version_switcher,
        "default_visibility": p.default_visibility,
    }


def _version_dict(v: ProjectVersion) -> dict:
    return {
        "id": v.id,
        "project_id": v.project_id,
        "name": v.name,
        "slug": v.slug,
        "is_default": v.is_default,
        "is_published": v.is_published,
        "semver_major": v.semver_major,
        "semver_minor": v.semver_minor,
        "semver_patch": v.semver_patch,
    }


def _topic_dict(t: Topic) -> dict:
    return {
        "id": t.id,
        "project_id": t.project_id,
        "project_version_id": t.project_version_id,
        "parent_id": t.parent_id,
        "name": t.name,
        "slug": t.slug,
        "display_order": t.display_order,
    }


def _doc_dict(d: Document) -> dict:
    return {
        "id": d.id,
        "title": d.title,
        "slug": d.slug,
        "project": d.project,
        "version": d.version,
        "section": d.section,
        "visibility": d.visibility,
        "status": d.status,
        "description": d.description,
        "tags": d.tags,
        "project_id": d.project_id,
        "project_version_id": d.project_version_id,
        "topic_id": d.topic_id,
        "is_published": d.is_published,
        "content_html": d.content_html,
        "published_content_html": d.published_content_html,
        "content_id": d.content_id,
        "published_content_id": d.published_content_id,
        "video_url": d.video_url,
        "video_title": d.video_title,
        "display_order": d.display_order,
        "last_published_at": d.last_published_at,
        "created_at": str(d.created_at) if d.created_at else None,
        "updated_at": str(d.updated_at) if d.updated_at else None,
    }


# ---------------------------------------------------------------------------
# GET /api/organizations  — Strapi-style filtered lookup
# ---------------------------------------------------------------------------
# The frontend sends queries like:
#   /api/organizations?filters[$or][0][slug][$eq]=foo&filters[$or][1][domain][$eq]=foo
#   /api/organizations?filters[custom_docs_domain][$eq]=example.com
#   /api/organizations?filters[domain][$eq]=example.com
# We parse the most common filter patterns and return {data: [...]}.

def _parse_strapi_filters(params: dict) -> dict:
    """Extract simple Strapi filters into {field: value} and $or groups."""
    simple: dict[str, str] = {}
    or_groups: list[dict[str, str]] = []

    for key, val in params.items():
        if not key.startswith("filters"):
            continue
        # Simple: filters[slug][$eq] = value
        m = re.match(r"filters\[(\w+)\]\[\$eq\]", key)
        if m:
            simple[m.group(1)] = val
            continue
        # Nested relation: filters[organization][id][$eq] -> organization_id
        m = re.match(r"filters\[(\w+)\]\[id\]\[\$eq\]", key)
        if m:
            simple[m.group(1) + "_id"] = val
            continue
        # Nested relation shorthand: filters[organization][$eq] -> organization_id
        m = re.match(r"filters\[(\w+)\]\[\$eq\]", key)
        if m:
            field = m.group(1)
            if field not in simple:
                simple[field + "_id"] = val
            continue
        # $or: filters[$or][0][slug][$eq] = value
        m = re.match(r"filters\[\$or\]\[(\d+)\]\[(\w+)\]\[\$eq\]", key)
        if m:
            idx, field = int(m.group(1)), m.group(2)
            while len(or_groups) <= idx:
                or_groups.append({})
            or_groups[idx][field] = val

    return {"simple": simple, "or_groups": or_groups}


@router.get("/api/organizations")
async def list_organizations(request: Request, db: Session = Depends(get_db)):
    params = dict(request.query_params)
    parsed = _parse_strapi_filters(params)

    query = db.query(Organization)

    # Apply simple equality filters
    for field, value in parsed["simple"].items():
        col = getattr(Organization, field, None)
        if col is not None:
            query = query.filter(col == value)

    # Apply $or groups
    if parsed["or_groups"]:
        from sqlalchemy import or_
        conditions = []
        for group in parsed["or_groups"]:
            for field, value in group.items():
                col = getattr(Organization, field, None)
                if col is not None:
                    conditions.append(col == value)
        if conditions:
            query = query.filter(or_(*conditions))

    # Pagination
    limit = int(params.get("pagination[limit]", "25"))
    orgs = query.limit(limit).all()

    return {"data": [_org_dict(o) for o in orgs]}


# ---------------------------------------------------------------------------
# GET /api/organizations/{org_id}  — single org by ID
# ---------------------------------------------------------------------------

@router.get("/api/organizations/{org_id}")
async def get_organization_by_id(org_id: int, db: Session = Depends(get_db)):
    org = db.get(Organization, org_id)
    if not org:
        return {"data": None}
    return {"data": _org_dict(org)}


# ---------------------------------------------------------------------------
# GET /api/projects  — Strapi-style filtered lookup for project-based org resolution
# ---------------------------------------------------------------------------

@router.get("/api/projects")
async def list_projects_public(request: Request, db: Session = Depends(get_db)):
    params = dict(request.query_params)
    parsed = _parse_strapi_filters(params)

    query = db.query(Project)

    for field, value in parsed["simple"].items():
        if field == "is_published":
            query = query.filter(Project.is_published == (value.lower() == "true"))
        elif field == "visibility":
            query = query.filter(Project.visibility == value)
        elif field == "slug":
            query = query.filter(Project.slug == value)
        elif field == "organization_id":
            query = query.filter(Project.organization_id == int(value))

    limit = int(params.get("pagination[limit]", "25"))
    projects = query.limit(limit).all()

    # Return in Strapi-like shape with nested organization
    result = []
    for p in projects:
        d = _project_dict(p)
        # Populate organization if requested
        if "populate[organization]" in str(params) and p.organization_id:
            d["organization"] = {"data": {"id": p.organization_id}}
        result.append(d)

    return {"data": result}


# ---------------------------------------------------------------------------
# GET /api/public-content  — all published content for an org
# ---------------------------------------------------------------------------

def _content_payload(project_ids: list[int], db: Session) -> dict:
    """Build the topics/versions/documents payload for a set of project IDs."""
    if not project_ids:
        return {"projects": [], "versions": [], "topics": [], "documents": []}

    # All versions for the project are shown — no separate version-level published gate
    versions = (
        db.query(ProjectVersion)
        .filter(ProjectVersion.project_id.in_(project_ids))
        .all()
    )
    topics = db.query(Topic).filter(Topic.project_id.in_(project_ids)).all()
    # Show all documents that have content — project-level is_published is the gate
    documents = (
        db.query(Document)
        .filter(
            Document.project_id.in_(project_ids),
            Document.content_html.isnot(None),
            Document.content_html != "",
        )
        .all()
    )
    return {
        "versions": [_version_dict(v) for v in versions],
        "topics": [_topic_dict(t) for t in topics],
        "documents": [_doc_dict(d) for d in documents],
    }


@router.get("/api/public-content")
async def public_content(
    organizationId: str = Query(...),
    db: Session = Depends(get_db),
):
    """Return published public-visibility content for unauthenticated viewers."""
    try:
        org_id = int(organizationId)
    except (ValueError, TypeError):
        return {"ok": False, "error": "Invalid organizationId"}

    org = db.get(Organization, org_id)
    if not org:
        return {"ok": False, "error": "Organization not found"}

    # Only projects with visibility=public AND is_published
    projects = (
        db.query(Project)
        .filter(
            Project.organization_id == org_id,
            Project.visibility == "public",
            Project.is_published == True,
        )
        .all()
    )
    project_ids = [p.id for p in projects]
    payload = _content_payload(project_ids, db)
    return {"ok": True, "projects": [_project_dict(p) for p in projects], **payload}


@router.get("/api/external-content")
async def external_content(
    organizationId: str = Query(...),
    request: Request = None,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Return external-visibility content for authenticated invited guests."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        org_id = int(organizationId)
    except (ValueError, TypeError):
        return {"ok": False, "error": "Invalid organizationId"}

    # Find external projects this user has been explicitly invited to
    memberships = (
        db.query(ProjectMember)
        .filter(ProjectMember.user_id == user.id)
        .all()
    )
    invited_project_ids = {m.project_id for m in memberships}

    projects = (
        db.query(Project)
        .filter(
            Project.organization_id == org_id,
            Project.visibility == "external",
            Project.is_published == True,
            Project.id.in_(invited_project_ids),
        )
        .all()
    )
    project_ids = [p.id for p in projects]
    payload = _content_payload(project_ids, db)
    return {"ok": True, "projects": [_project_dict(p) for p in projects], **payload}


# ---------------------------------------------------------------------------
# Helper: parse $in filter  (filters[project][id][$in] = "1,2,3")
# ---------------------------------------------------------------------------

def _parse_in_filter(params: dict, relation: str) -> list[int] | None:
    """Parse filters[relation][id][$in] = '1,2,3' into list of ints."""
    key = f"filters[{relation}][id][$in]"
    raw = params.get(key)
    if not raw:
        return None
    try:
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# GET /api/project-versions  — Strapi-compatible version listing
# ---------------------------------------------------------------------------

@router.get("/api/project-versions")
async def list_project_versions(request: Request, db: Session = Depends(get_db)):
    params = dict(request.query_params)
    parsed = _parse_strapi_filters(params)

    query = db.query(ProjectVersion)

    # $in filter for project IDs
    project_ids = _parse_in_filter(params, "project")
    if project_ids:
        query = query.filter(ProjectVersion.project_id.in_(project_ids))

    for field, value in parsed["simple"].items():
        if field == "is_published":
            query = query.filter(ProjectVersion.is_published == (value.lower() == "true"))
        elif field == "project_id":
            query = query.filter(ProjectVersion.project_id == int(value))

    limit = int(params.get("pagination[limit]", "1000"))
    versions = query.limit(limit).all()

    result = []
    for v in versions:
        d = _version_dict(v)
        d["project"] = {"data": {"id": v.project_id}}
        result.append(d)

    return {"data": result}


# ---------------------------------------------------------------------------
# GET /api/topics  — Strapi-compatible topic listing
# ---------------------------------------------------------------------------

@router.get("/api/topics")
async def list_topics(request: Request, db: Session = Depends(get_db)):
    params = dict(request.query_params)
    parsed = _parse_strapi_filters(params)

    query = db.query(Topic)

    project_ids = _parse_in_filter(params, "project")
    if project_ids:
        query = query.filter(Topic.project_id.in_(project_ids))

    for field, value in parsed["simple"].items():
        if field == "project_id":
            query = query.filter(Topic.project_id == int(value))

    # Sort
    sort_param = params.get("sort", "display_order:asc")
    if "display_order" in sort_param:
        if "desc" in sort_param:
            query = query.order_by(Topic.display_order.desc())
        else:
            query = query.order_by(Topic.display_order.asc())

    limit = int(params.get("pagination[limit]", "1000"))
    topics = query.limit(limit).all()

    result = []
    for t in topics:
        d = _topic_dict(t)
        d["project"] = {"data": {"id": t.project_id}}
        if t.project_version_id:
            d["project_version"] = {"data": {"id": t.project_version_id}}
        if t.parent_id:
            d["parent"] = {"data": {"id": t.parent_id}}
        result.append(d)

    return {"data": result}


# ---------------------------------------------------------------------------
# GET /api/documents  — Strapi-compatible document listing
# ---------------------------------------------------------------------------

@router.get("/api/documents")
async def list_documents_public(request: Request, db: Session = Depends(get_db)):
    params = dict(request.query_params)
    parsed = _parse_strapi_filters(params)

    query = db.query(Document)

    project_ids = _parse_in_filter(params, "project")
    if project_ids:
        query = query.filter(Document.project_id.in_(project_ids))

    for field, value in parsed["simple"].items():
        if field == "is_published":
            query = query.filter(Document.is_published == (value.lower() == "true"))
        elif field == "project_id":
            query = query.filter(Document.project_id == int(value))
        elif field == "visibility":
            query = query.filter(Document.visibility == value)

    # Sort
    sort_param = params.get("sort", "display_order:asc")
    if "display_order" in sort_param:
        if "desc" in sort_param:
            query = query.order_by(Document.display_order.desc())
        else:
            query = query.order_by(Document.display_order.asc())

    limit = int(params.get("pagination[limit]", "1000"))
    docs = query.limit(limit).all()

    result = []
    for d in docs:
        dd = _doc_dict(d)
        if d.project_id:
            dd["project"] = {"data": {"id": d.project_id}}
        if d.project_version_id:
            dd["project_version"] = {"data": {"id": d.project_version_id}}
        if d.topic_id:
            dd["topic"] = {"data": {"id": d.topic_id}}
        if d.owner_id:
            dd["owner"] = {"data": {"id": d.owner_id}}
        result.append(dd)

    return {"data": result}


# ---------------------------------------------------------------------------
# GET /api/documents/{doc_id}  — single document by ID (Strapi-compatible)
# ---------------------------------------------------------------------------

@router.get("/api/documents/{doc_id}")
async def get_document_by_id(doc_id: int, request: Request, db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    if not doc:
        return {"data": None}

    dd = _doc_dict(doc)
    # Populate relations
    if doc.project_id:
        proj = db.get(Project, doc.project_id)
        if proj:
            dd["project"] = {"data": {"id": proj.id, "name": proj.name, "slug": proj.slug,
                                       "organization": {"data": {"id": proj.organization_id}} if proj.organization_id else None}}
    if doc.project_version_id:
        ver = db.get(ProjectVersion, doc.project_version_id)
        if ver:
            dd["project_version"] = {"data": {"id": ver.id, "name": ver.name, "slug": ver.slug}}
    if doc.topic_id:
        topic = db.get(Topic, doc.topic_id)
        if topic:
            dd["topic"] = {"data": {"id": topic.id, "name": topic.name, "slug": topic.slug}}
    if doc.owner_id:
        from app.models import User
        owner = db.get(User, doc.owner_id)
        if owner:
            dd["owner"] = {"data": {"id": owner.id, "email": owner.email,
                                     "username": owner.name, "full_name": owner.name}}

    return {"data": dd}


# ---------------------------------------------------------------------------
# GET /api/project-members  — membership lookup for user
# ---------------------------------------------------------------------------

@router.get("/api/project-members")
async def list_project_members(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Return project memberships for the current user."""
    if not user:
        return {"data": []}

    params = dict(request.query_params)
    parsed = _parse_strapi_filters(params)

    # Filter by requesting user (ignore any user filter from query — always scope to auth user)
    memberships = (
        db.query(ProjectMember)
        .filter(ProjectMember.user_id == user.id)
        .all()
    )

    result = []
    for m in memberships:
        result.append({
            "id": m.id,
            "project_id": m.project_id,
            "user_id": m.user_id,
            "role": m.role,
            "project": {"data": {"id": m.project_id}},
        })
    return {"data": result}
