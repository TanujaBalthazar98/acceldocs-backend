"""API routes for managing invite-only external docs access."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.auth.routes import get_current_user
from app.database import get_db
from app.models import User
from app.services import external_access as external_access_service

router = APIRouter()


class ExternalAccessGrantCreate(BaseModel):
    email: EmailStr


class ExternalAccessRevokeByEmail(BaseModel):
    email: EmailStr


def _raise_for_result(result: dict) -> None:
    if result.get("ok"):
        return

    detail = str(result.get("error") or "Request failed")
    lowered = detail.lower()
    if "insufficient permissions" in lowered:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    if "no organization" in lowered:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)
    if "not found" in lowered:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


@router.get("")
async def list_external_access(
    include_inactive: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    result = await external_access_service.list_external_access(
        body={"include_inactive": include_inactive},
        db=db,
        user=user,
    )
    _raise_for_result(result)
    return result


@router.post("")
async def grant_external_access(
    body: ExternalAccessGrantCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    result = await external_access_service.grant_external_access(
        body={"email": body.email},
        db=db,
        user=user,
    )
    _raise_for_result(result)
    return result


@router.delete("/{grant_id}")
async def revoke_external_access(
    grant_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    result = await external_access_service.revoke_external_access(
        body={"grantId": grant_id},
        db=db,
        user=user,
    )
    _raise_for_result(result)
    return result


@router.post("/revoke")
async def revoke_external_access_by_email(
    body: ExternalAccessRevokeByEmail,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    result = await external_access_service.revoke_external_access(
        body={"email": body.email},
        db=db,
        user=user,
    )
    _raise_for_result(result)
    return result
