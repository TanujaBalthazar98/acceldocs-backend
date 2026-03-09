"""Health check endpoint."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok", "service": "acceldocs-backend"}


@router.get("/admin/debug-docs")
async def debug_docs(db: Session = Depends(get_db)):
    """TEMPORARY: Dump document and project state for debugging."""
    from app.models import Document, Project, Organization, OrgRole, User

    total_docs = db.query(Document).count()
    docs_with_pid = db.query(Document).filter(Document.project_id.isnot(None)).count()
    docs_without_pid = db.query(Document).filter(Document.project_id.is_(None)).count()
    docs_with_owner = db.query(Document).filter(Document.owner_id.isnot(None)).count()
    docs_without_owner = db.query(Document).filter(Document.owner_id.is_(None)).count()

    all_docs = db.query(Document).all()
    doc_summaries = [
        {
            "id": d.id,
            "title": d.title[:50],
            "project_id": d.project_id,
            "project_legacy": d.project[:50] if d.project else None,
            "owner_id": d.owner_id,
            "visibility": d.visibility,
            "status": d.status,
        }
        for d in all_docs[:50]
    ]

    all_projects = db.query(Project).all()
    project_summaries = [
        {
            "id": p.id,
            "name": p.name,
            "slug": p.slug,
            "org_id": p.organization_id,
            "parent_id": p.parent_id,
            "is_active": p.is_active,
        }
        for p in all_projects[:30]
    ]

    all_orgs = db.query(Organization).all()
    org_summaries = [{"id": o.id, "name": o.name, "slug": o.slug} for o in all_orgs[:10]]

    org_roles = db.query(OrgRole).all()
    role_summaries = [
        {"user_id": r.user_id, "org_id": r.organization_id, "role": r.role}
        for r in org_roles[:20]
    ]

    users = db.query(User).all()
    user_summaries = [{"id": u.id, "email": u.email, "name": u.name} for u in users[:20]]

    return {
        "total_docs": total_docs,
        "docs_with_project_id": docs_with_pid,
        "docs_without_project_id": docs_without_pid,
        "docs_with_owner_id": docs_with_owner,
        "docs_without_owner_id": docs_without_owner,
        "documents": doc_summaries,
        "projects": project_summaries,
        "organizations": org_summaries,
        "org_roles": role_summaries,
        "users": user_summaries,
    }


@router.post("/admin/clear-drive-folder")
async def clear_drive_folder(db: Session = Depends(get_db)):
    """TEMPORARY: Clear drive_folder_id from all orgs to re-trigger onboarding."""
    from app.models import Organization
    count = db.query(Organization).update({Organization.drive_folder_id: None})
    db.commit()
    return {"status": "ok", "orgs_cleared": count}
