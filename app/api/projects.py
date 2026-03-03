"""Project CRUD API routes."""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from slugify import slugify
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.auth import AuthUser, require_role
from app.models import Project, Document
from sqlalchemy import func

router = APIRouter()


class ProjectOut(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None
    drive_folder_id: str | None
    default_visibility: str
    require_approval: bool
    owner_id: int | None
    is_active: bool
    created_at: str
    updated_at: str
    document_count: int | None = None

    model_config = {"from_attributes": True}


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None
    drive_folder_id: str | None = None
    default_visibility: str = "public"
    require_approval: bool = True


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    drive_folder_id: str | None = None
    default_visibility: str | None = None
    require_approval: bool | None = None
    is_active: bool | None = None


@router.get("/", response_model=list[ProjectOut])
async def list_projects(
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
):
    """List all projects with document counts."""
    query = db.query(Project)

    if not include_inactive:
        query = query.filter(Project.is_active == True)

    projects = query.order_by(Project.name).all()

    # Add document counts
    result = []
    for project in projects:
        doc_count = (
            db.query(func.count(Document.id))
            .filter(Document.project == project.name)
            .scalar() or 0
        )

        project_dict = {
            "id": project.id,
            "name": project.name,
            "slug": project.slug,
            "description": project.description,
            "drive_folder_id": project.drive_folder_id,
            "default_visibility": project.default_visibility,
            "require_approval": project.require_approval,
            "owner_id": project.owner_id,
            "is_active": project.is_active,
            "created_at": str(project.created_at),
            "updated_at": str(project.updated_at),
            "document_count": doc_count,
        }
        result.append(ProjectOut(**project_dict))

    return result


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: int, db: Session = Depends(get_db)):
    """Get a single project by ID."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    doc_count = (
        db.query(func.count(Document.id))
        .filter(Document.project == project.name)
        .scalar() or 0
    )

    project_dict = {
        "id": project.id,
        "name": project.name,
        "slug": project.slug,
        "description": project.description,
        "drive_folder_id": project.drive_folder_id,
        "default_visibility": project.default_visibility,
        "require_approval": project.require_approval,
        "owner_id": project.owner_id,
        "is_active": project.is_active,
        "created_at": str(project.created_at),
        "updated_at": str(project.updated_at),
        "document_count": doc_count,
    }

    return ProjectOut(**project_dict)


@router.post("/", response_model=ProjectOut)
async def create_project(
    body: ProjectCreate,
    current_user: AuthUser = Depends(require_role("editor")),
    db: Session = Depends(get_db),
):
    """Create a new project. Requires editor role or higher."""
    # Generate slug from name
    slug = slugify(body.name)

    # Check if project with same name or slug exists
    existing = db.query(Project).filter(
        (Project.name == body.name) | (Project.slug == slug)
    ).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail="Project with this name already exists"
        )

    # Validate visibility
    if body.default_visibility not in {"public", "internal"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid visibility. Must be 'public' or 'internal'"
        )

    project = Project(
        name=body.name,
        slug=slug,
        description=body.description,
        drive_folder_id=body.drive_folder_id,
        default_visibility=body.default_visibility,
        require_approval=body.require_approval,
        is_active=True,
    )

    db.add(project)
    db.commit()
    db.refresh(project)

    project_dict = {
        "id": project.id,
        "name": project.name,
        "slug": project.slug,
        "description": project.description,
        "drive_folder_id": project.drive_folder_id,
        "default_visibility": project.default_visibility,
        "require_approval": project.require_approval,
        "owner_id": project.owner_id,
        "is_active": project.is_active,
        "created_at": str(project.created_at),
        "updated_at": str(project.updated_at),
        "document_count": 0,
    }

    return ProjectOut(**project_dict)


@router.put("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: int,
    body: ProjectUpdate,
    current_user: AuthUser = Depends(require_role("editor")),
    db: Session = Depends(get_db),
):
    """Update a project. Requires editor role or higher."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Update fields if provided
    if body.name is not None:
        # Check for name conflicts
        existing = db.query(Project).filter(
            Project.name == body.name,
            Project.id != project_id
        ).first()
        if existing:
            raise HTTPException(
                status_code=400,
                detail="Project with this name already exists"
            )
        project.name = body.name
        project.slug = slugify(body.name)

    if body.description is not None:
        project.description = body.description

    if body.drive_folder_id is not None:
        project.drive_folder_id = body.drive_folder_id

    if body.default_visibility is not None:
        if body.default_visibility not in {"public", "internal"}:
            raise HTTPException(
                status_code=400,
                detail="Invalid visibility. Must be 'public' or 'internal'"
            )
        project.default_visibility = body.default_visibility

    if body.require_approval is not None:
        project.require_approval = body.require_approval

    if body.is_active is not None:
        project.is_active = body.is_active

    db.commit()
    db.refresh(project)

    doc_count = (
        db.query(func.count(Document.id))
        .filter(Document.project == project.name)
        .scalar() or 0
    )

    project_dict = {
        "id": project.id,
        "name": project.name,
        "slug": project.slug,
        "description": project.description,
        "drive_folder_id": project.drive_folder_id,
        "default_visibility": project.default_visibility,
        "require_approval": project.require_approval,
        "owner_id": project.owner_id,
        "is_active": project.is_active,
        "created_at": str(project.created_at),
        "updated_at": str(project.updated_at),
        "document_count": doc_count,
    }

    return ProjectOut(**project_dict)


@router.delete("/{project_id}")
async def delete_project(
    project_id: int,
    current_user: AuthUser = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Delete a project (or mark as inactive). Requires admin role."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Check if project has documents
    doc_count = (
        db.query(func.count(Document.id))
        .filter(Document.project == project.name)
        .scalar() or 0
    )

    if doc_count > 0:
        # Don't delete, just mark as inactive
        project.is_active = False
        db.commit()
        return {
            "status": "deactivated",
            "message": f"Project has {doc_count} documents. Marked as inactive instead of deleting."
        }

    # No documents, safe to delete
    db.delete(project)
    db.commit()

    return {"status": "deleted", "message": "Project deleted successfully"}
