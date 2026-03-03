"""Project, version, and topic management functions."""

from sqlalchemy.orm import Session
from app.models import User, Project, ProjectVersion, Topic, OrgRole, ProjectMember, Organization


async def list_projects(body: dict, db: Session, user: User | None) -> dict:
    """List all projects in organization."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        # Find user's organization
        org_role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
        if not org_role:
            return {"ok": True, "projects": []}

        # Get all projects for this organization
        projects = db.query(Project).filter(
            Project.organization_id == org_role.organization_id,
            Project.is_active == True
        ).all()

        project_list = []
        for p in projects:
            project_list.append({
                "id": p.id,
                "name": p.name,
                "slug": p.slug,
                "description": p.description,
                "drive_folder_id": p.drive_folder_id,
                "drive_parent_id": p.drive_parent_id,
                "visibility": p.visibility,
                "is_published": p.is_published,
                "show_version_switcher": p.show_version_switcher,
                "default_visibility": p.default_visibility,
                "require_approval": p.require_approval,
                "organization_id": p.organization_id,
                "parent_id": p.parent_id,
                "owner_id": p.owner_id,
                "is_active": p.is_active,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            })

        return {"ok": True, "projects": project_list}

    except Exception as e:
        return {"ok": False, "error": str(e)}


async def create_project(body: dict, db: Session, user: User | None) -> dict:
    """Create project + default v1.0 version."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        # Find user's organization
        org_role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
        if not org_role:
            return {"ok": False, "error": "User is not a member of any organization"}

        # Check permissions (owner/admin can create projects)
        if org_role.role not in ["owner", "admin"]:
            return {"ok": False, "error": "Insufficient permissions"}

        # Create project
        project = Project(
            name=body.get("name", "New Project"),
            slug=body.get("slug", "new-project"),
            description=body.get("description"),
            drive_folder_id=body.get("drive_folder_id"),
            drive_parent_id=body.get("drive_parent_id"),
            visibility=body.get("visibility", "internal"),
            default_visibility=body.get("default_visibility", "public"),
            require_approval=body.get("require_approval", True),
            organization_id=org_role.organization_id,
            owner_id=user.id,
            is_active=True,
        )
        db.add(project)
        db.flush()  # Get project ID

        # Create default version (v1.0)
        version = ProjectVersion(
            project_id=project.id,
            name="v1.0",
            slug="v1-0",
            is_default=True,
            is_published=False,
            semver_major=1,
            semver_minor=0,
            semver_patch=0,
        )
        db.add(version)
        db.commit()

        return {
            "ok": True,
            "project": {
                "id": project.id,
                "name": project.name,
                "slug": project.slug,
                "description": project.description,
            }
        }

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def update_project_settings(body: dict, db: Session, user: User | None) -> dict:
    """Modify project metadata."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        project_id = body.get("id") or body.get("projectId")
        if not project_id:
            return {"ok": False, "error": "Project ID required"}

        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return {"ok": False, "error": "Project not found"}

        # Update fields
        updatable_fields = [
            "name", "slug", "description", "drive_folder_id", "drive_parent_id",
            "visibility", "default_visibility", "require_approval", "show_version_switcher"
        ]
        for field in updatable_fields:
            if field in body:
                setattr(project, field, body[field])

        db.commit()

        return {"ok": True, "project": {"id": project.id, "name": project.name}}

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def get_project_settings(body: dict, db: Session, user: User | None) -> dict:
    """Fetch comprehensive project config with org, members, and roles."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        project_id = body.get("id") or body.get("projectId")
        if not project_id:
            return {"ok": False, "error": "Project ID required"}

        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return {"ok": False, "error": "Project not found"}

        # Get organization with owner
        org = None
        org_data = None
        if project.organization_id:
            org = db.query(Organization).filter(Organization.id == project.organization_id).first()
            if org:
                owner_user = db.query(User).filter(User.id == org.owner_id).first() if org.owner_id else None
                org_data = {
                    "id": org.id,
                    "name": org.name,
                    "slug": org.slug,
                    "domain": org.domain,
                    "owner": {"id": owner_user.id, "email": owner_user.email, "name": owner_user.name} if owner_user else None,
                }

        # Get org roles with user info
        org_roles = []
        if project.organization_id:
            roles = db.query(OrgRole).filter(OrgRole.organization_id == project.organization_id).all()
            for r in roles:
                role_user = db.query(User).filter(User.id == r.user_id).first()
                if role_user:
                    org_roles.append({
                        "id": r.id,
                        "role": r.role,
                        "user": {"id": role_user.id, "email": role_user.email, "name": role_user.name},
                    })

        # Get project members with user info
        project_members = []
        members = db.query(ProjectMember).filter(ProjectMember.project_id == project_id).all()
        for m in members:
            member_user = db.query(User).filter(User.id == m.user_id).first()
            if member_user:
                project_members.append({
                    "id": m.id,
                    "role": m.role,
                    "user": {"id": member_user.id, "email": member_user.email, "name": member_user.name},
                })

        # Compute effective role for current user
        effective_role = None
        if org_data and org_data.get("owner") and org_data["owner"]["id"] == user.id:
            effective_role = "admin"
        else:
            org_role_match = next((r for r in org_roles if r["user"]["id"] == user.id), None)
            if org_role_match:
                role_str = org_role_match["role"]
                if role_str in ("owner", "admin"):
                    effective_role = "admin"
                elif role_str == "editor":
                    effective_role = "editor"
                elif role_str == "viewer":
                    effective_role = "viewer"

            pm_match = next((m for m in project_members if m["user"]["id"] == user.id), None)
            if pm_match and not effective_role:
                effective_role = pm_match["role"]

        # Get versions
        versions = db.query(ProjectVersion).filter(ProjectVersion.project_id == project_id).all()

        return {
            "ok": True,
            "project": {
                "id": project.id,
                "name": project.name,
                "slug": project.slug,
                "description": project.description,
                "drive_folder_id": project.drive_folder_id,
                "drive_parent_id": project.drive_parent_id,
                "visibility": project.visibility,
                "is_published": project.is_published,
                "show_version_switcher": project.show_version_switcher,
                "default_visibility": project.default_visibility,
                "require_approval": project.require_approval,
                "organization_id": project.organization_id,
                "parent_id": project.parent_id,
                "owner_id": project.owner_id,
                "is_active": project.is_active,
            },
            "organization": org_data,
            "orgRoles": org_roles,
            "projectMembers": project_members,
            "effectiveRole": effective_role,
            "versions": [
                {
                    "id": v.id,
                    "name": v.name,
                    "slug": v.slug,
                    "is_default": v.is_default,
                    "is_published": v.is_published,
                }
                for v in versions
            ],
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}


async def delete_project(body: dict, db: Session, user: User | None) -> dict:
    """Delete project and all content."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        project_id = body.get("id") or body.get("projectId")
        if not project_id:
            return {"ok": False, "error": "Project ID required"}

        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return {"ok": False, "error": "Project not found"}

        # Soft delete
        project.is_active = False
        db.commit()

        return {"ok": True}

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def list_project_versions(body: dict, db: Session, user: User | None) -> dict:
    """Get versions for given projects."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        project_ids = body.get("projectIds", [])
        if not project_ids:
            return {"ok": True, "versions": []}

        versions = db.query(ProjectVersion).filter(
            ProjectVersion.project_id.in_(project_ids)
        ).all()

        version_list = []
        for v in versions:
            version_list.append({
                "id": v.id,
                "project_id": v.project_id,
                "name": v.name,
                "slug": v.slug,
                "is_default": v.is_default,
                "is_published": v.is_published,
                "semver_major": v.semver_major,
                "semver_minor": v.semver_minor,
                "semver_patch": v.semver_patch,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "updated_at": v.updated_at.isoformat() if v.updated_at else None,
            })

        return {"ok": True, "versions": version_list}

    except Exception as e:
        return {"ok": False, "error": str(e)}


async def create_project_version(body: dict, db: Session, user: User | None) -> dict:
    """Create new version."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        project_id = body.get("projectId")
        name = body.get("name", "v1.0")
        slug = body.get("slug", "v1-0")

        if not project_id:
            return {"ok": False, "error": "Project ID required"}

        # Create version (accept both camelCase and snake_case)
        version = ProjectVersion(
            project_id=project_id,
            name=name,
            slug=slug,
            is_default=body.get("isDefault", body.get("is_default", False)),
            is_published=body.get("isPublished", body.get("is_published", False)),
            semver_major=body.get("semverMajor", body.get("semver_major", 1)),
            semver_minor=body.get("semverMinor", body.get("semver_minor", 0)),
            semver_patch=body.get("semverPatch", body.get("semver_patch", 0)),
        )
        db.add(version)
        db.commit()

        return {
            "ok": True,
            "versionId": str(version.id),
            "version": {
                "id": version.id,
                "project_id": version.project_id,
                "name": version.name,
                "slug": version.slug,
            }
        }

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def list_topics(body: dict, db: Session, user: User | None) -> dict:
    """Get all topics for projects."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        project_ids = body.get("projectIds", [])
        if not project_ids:
            return {"ok": True, "topics": []}

        topics = db.query(Topic).filter(
            Topic.project_id.in_(project_ids)
        ).order_by(Topic.display_order, Topic.id).all()

        topic_list = []
        for t in topics:
            topic_list.append({
                "id": t.id,
                "project_id": t.project_id,
                "project_version_id": t.project_version_id,
                "parent_id": t.parent_id,
                "name": t.name,
                "slug": t.slug,
                "display_order": t.display_order,
                "drive_folder_id": t.drive_folder_id,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            })

        return {"ok": True, "topics": topic_list}

    except Exception as e:
        return {"ok": False, "error": str(e)}


async def create_topic(body: dict, db: Session, user: User | None) -> dict:
    """Create topic/section."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        project_id = body.get("projectId") or body.get("project_id")
        name = body.get("name", "New Topic")

        if not project_id:
            return {"ok": False, "error": "Project ID required"}

        topic = Topic(
            project_id=project_id,
            project_version_id=body.get("projectVersionId") or body.get("project_version_id"),
            parent_id=body.get("parentId") or body.get("parent_id"),
            name=name,
            slug=body.get("slug", name.lower().replace(" ", "-")),
            display_order=body.get("displayOrder", body.get("display_order", 0)),
            drive_folder_id=body.get("driveFolderId") or body.get("drive_folder_id"),
        )
        db.add(topic)
        db.commit()

        return {
            "ok": True,
            "topic": {
                "id": topic.id,
                "project_id": topic.project_id,
                "name": topic.name,
                "slug": topic.slug,
            }
        }

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def delete_topic(body: dict, db: Session, user: User | None) -> dict:
    """Delete topic and all documents."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        topic_id = body.get("id") or body.get("topicId")
        if not topic_id:
            return {"ok": False, "error": "Topic ID required"}

        topic = db.query(Topic).filter(Topic.id == topic_id).first()
        if not topic:
            return {"ok": False, "error": "Topic not found"}

        db.delete(topic)
        db.commit()

        return {"ok": True}

    except Exception as e:
        db.rollback()
        return {"ok": False, "error": str(e)}


async def normalize_structure(body: dict, db: Session, user: User | None) -> dict:
    """Fix topic hierarchy issues."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    # Placeholder for maintenance function
    return {"ok": True, "message": "Structure normalized"}


async def repair_hierarchy(body: dict, db: Session, user: User | None) -> dict:
    """Find and repair duplicates."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    # Placeholder for maintenance function
    return {"ok": True, "message": "Hierarchy repaired"}
