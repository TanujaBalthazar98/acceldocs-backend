#!/usr/bin/env python3
"""Backfill projects and versions from existing documents.

Use this when migrated documents exist but project/project_version tables are empty.
"""

from collections import defaultdict

from slugify import slugify
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Document, Organization, OrgRole, Project, ProjectVersion


def main() -> None:
    db = SessionLocal()
    try:
        docs = db.scalars(select(Document)).all()
        if not docs:
            print("No documents found. Nothing to backfill.")
            return

        # Find a default organization id for imported data.
        org = db.scalars(select(Organization).order_by(Organization.id.asc())).first()
        org_id = org.id if org else None

        # Create projects by name if missing.
        projects_by_name: dict[str, Project] = {}
        existing_projects = db.scalars(select(Project)).all()
        for p in existing_projects:
            projects_by_name[p.name] = p

        created_projects = 0
        for doc in docs:
            project_name = (doc.project or "").strip()
            if not project_name:
                continue
            if project_name in projects_by_name:
                continue
            project = Project(
                name=project_name,
                slug=slugify(project_name),
                organization_id=org_id,
                is_active=True,
                visibility="public" if (doc.visibility or "public") == "public" else "internal",
                default_visibility=doc.visibility or "public",
                is_published=doc.status == "approved",
                show_version_switcher=True,
            )
            db.add(project)
            db.flush()
            projects_by_name[project_name] = project
            created_projects += 1

        # Create versions by (project_id, version)
        versions_by_key: dict[tuple[int, str], ProjectVersion] = {}
        existing_versions = db.scalars(select(ProjectVersion)).all()
        for v in existing_versions:
            versions_by_key[(v.project_id, v.name)] = v

        created_versions = 0
        per_project_version_counts = defaultdict(int)
        for doc in docs:
            project_name = (doc.project or "").strip()
            if not project_name:
                continue
            project = projects_by_name.get(project_name)
            if not project:
                continue
            version_name = (doc.version or "v1.0").strip() or "v1.0"
            key = (project.id, version_name)
            per_project_version_counts[project.id] += 1
            if key in versions_by_key:
                continue
            version = ProjectVersion(
                project_id=project.id,
                name=version_name,
                slug=slugify(version_name),
                is_default=False,
                is_published=True,
            )
            db.add(version)
            db.flush()
            versions_by_key[key] = version
            created_versions += 1

        # Ensure one default version per project.
        for (project_id, _), version in list(versions_by_key.items()):
            if not db.scalars(
                select(ProjectVersion).where(
                    ProjectVersion.project_id == project_id,
                    ProjectVersion.is_default == True,
                )
            ).first():
                version.is_default = True

        # Backfill document foreign keys and publish flags.
        updated_docs = 0
        for doc in docs:
            project_name = (doc.project or "").strip()
            if not project_name:
                continue
            project = projects_by_name.get(project_name)
            if not project:
                continue
            version_name = (doc.version or "v1.0").strip() or "v1.0"
            version = versions_by_key.get((project.id, version_name))
            changed = False
            if doc.project_id != project.id:
                doc.project_id = project.id
                changed = True
            if version and doc.project_version_id != version.id:
                doc.project_version_id = version.id
                changed = True
            desired_published = doc.status == "approved"
            if doc.is_published != desired_published:
                doc.is_published = desired_published
                changed = True
            if changed:
                updated_docs += 1

        # Ensure imported org owner has roles for created projects.
        if org_id:
            owner_role = db.scalars(
                select(OrgRole).where(
                    OrgRole.organization_id == org_id,
                    OrgRole.role == "owner",
                )
            ).first()
            if owner_role:
                for p in projects_by_name.values():
                    if p.owner_id is None:
                        p.owner_id = owner_role.user_id

        db.commit()
        print(
            f"Backfill complete: created_projects={created_projects}, "
            f"created_versions={created_versions}, updated_documents={updated_docs}"
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
