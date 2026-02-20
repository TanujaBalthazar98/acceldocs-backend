"""User management API routes."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.database import get_db
from app.lib.rbac import get_assignable_roles, get_permissions_for_role, is_higher_role
from app.models import User

router = APIRouter()


class UserOut(BaseModel):
    id: int
    google_id: str
    email: str
    name: str | None
    role: str
    created_at: str

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    email: EmailStr
    name: str
    role: str


class UserUpdate(BaseModel):
    name: str | None = None
    role: str | None = None


@router.get("/", response_model=list[UserOut])
async def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all users. Requires viewer role or higher."""
    user_perms = get_permissions_for_role(current_user.role)
    if "users.view" not in user_perms:
        raise HTTPException(status_code=403, detail="Permission denied")

    return db.query(User).order_by(User.email).all()


@router.get("/{user_id}", response_model=UserOut)
async def get_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get user details."""
    user_perms = get_permissions_for_role(current_user.role)
    if "users.view" not in user_perms:
        raise HTTPException(status_code=403, detail="Permission denied")

    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("/", response_model=UserOut)
async def create_user(
    body: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create/invite a new user. Requires admin role or higher."""
    user_perms = get_permissions_for_role(current_user.role)
    if "users.create" not in user_perms:
        raise HTTPException(status_code=403, detail="Permission denied")

    # Validate role assignment
    assignable = get_assignable_roles(current_user.role)
    if body.role not in assignable:
        raise HTTPException(
            status_code=403,
            detail=f"You can only assign these roles: {', '.join(assignable)}",
        )

    # Check if user already exists
    existing = db.query(User).filter(User.email == body.email).first()
    if existing:
        raise HTTPException(status_code=409, detail="User already exists")

    # Create user with placeholder google_id (will be set on first login)
    user = User(
        google_id=f"pending-{body.email}",
        email=body.email,
        name=body.name,
        role=body.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # TODO: Send invitation email (integrate with SendGrid or SMTP)
    # send_invitation_email(user.email, user.name)

    return user


@router.put("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    body: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update user details. Role changes require admin or higher."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_perms = get_permissions_for_role(current_user.role)

    # Role change requires special permission
    if body.role and body.role != user.role:
        if "users.manage_roles" not in user_perms:
            raise HTTPException(status_code=403, detail="Cannot change user roles")

        # Can't demote yourself
        if user_id == current_user.id:
            raise HTTPException(status_code=403, detail="Cannot change your own role")

        # Can't assign higher role than yours
        if not is_higher_role(current_user.role, body.role):
            raise HTTPException(
                status_code=403, detail="Cannot assign a role equal to or higher than yours"
            )

        # Validate role is assignable by current user
        assignable = get_assignable_roles(current_user.role)
        if body.role not in assignable:
            raise HTTPException(
                status_code=403,
                detail=f"You can only assign these roles: {', '.join(assignable)}",
            )

        user.role = body.role

    # Name change requires at least users.edit permission
    if body.name is not None:
        if "users.edit" not in user_perms and user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Cannot edit other users")
        user.name = body.name

    db.commit()
    db.refresh(user)
    return user


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a user. Requires admin role or higher."""
    user_perms = get_permissions_for_role(current_user.role)
    if "users.delete" not in user_perms:
        raise HTTPException(status_code=403, detail="Permission denied")

    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Can't delete yourself
    if user_id == current_user.id:
        raise HTTPException(status_code=403, detail="Cannot delete yourself")

    # Can't delete higher-ranked users
    if not is_higher_role(current_user.role, user.role):
        raise HTTPException(
            status_code=403, detail="Cannot delete users with equal or higher role"
        )

    db.delete(user)
    db.commit()

    return {"status": "ok", "deleted_user_id": user_id}
