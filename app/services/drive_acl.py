"""Drive permission synchronization for organization members.

This module keeps Google Drive folder ACLs aligned with workspace RBAC so
invited users can open source Google Docs without manual access requests.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

import httpx
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import GoogleToken, OrgRole, Organization, User
from app.services.encryption import get_encryption_service

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_REFRESH_URL = "https://oauth2.googleapis.com/token"

RBAC_TO_DRIVE_ROLE = {
    "owner": "writer",
    "admin": "writer",
    "editor": "writer",
    "reviewer": "commenter",
    "viewer": "reader",
}


def _normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


async def _refresh_access_token(encrypted_refresh_token: str) -> str | None:
    encryption_service = get_encryption_service()
    try:
        refresh_token = encryption_service.decrypt(encrypted_refresh_token)
    except Exception as exc:
        logger.warning("Could not decrypt stored Google refresh token: %s", exc)
        return None

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(
                GOOGLE_TOKEN_REFRESH_URL,
                data={
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
    except Exception as exc:
        logger.warning("Google token refresh request failed: %s", exc)
        return None

    if resp.status_code != 200:
        logger.warning("Google token refresh failed (%s): %s", resp.status_code, resp.text)
        return None

    access_token = resp.json().get("access_token")
    if not access_token:
        logger.warning("Google token refresh response missing access_token")
        return None
    return access_token


def _dedupe_user_ids(user_ids: Iterable[int | None]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for raw in user_ids:
        if raw is None:
            continue
        value = int(raw)
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _candidate_actor_tokens(
    db: Session,
    organization_id: int,
    preferred_user_ids: Iterable[int | None] = (),
) -> list[GoogleToken]:
    member_roles = (
        db.query(OrgRole)
        .filter(OrgRole.organization_id == organization_id)
        .order_by(OrgRole.created_at.asc(), OrgRole.id.asc())
        .all()
    )
    member_user_ids = [role.user_id for role in member_roles]
    if not member_user_ids:
        return []

    all_member_tokens = (
        db.query(GoogleToken)
        .filter(GoogleToken.user_id.in_(member_user_ids))
        .order_by(GoogleToken.updated_at.desc(), GoogleToken.id.desc())
        .all()
    )

    # Prefer workspace-scoped token for each member, but fall back to any token
    # held by that same member (legacy/multi-workspace login paths).
    org_token_by_user: dict[int, GoogleToken] = {}
    any_token_by_user: dict[int, GoogleToken] = {}
    for token in all_member_tokens:
        if token.user_id not in any_token_by_user:
            any_token_by_user[token.user_id] = token
        if token.organization_id == organization_id and token.user_id not in org_token_by_user:
            org_token_by_user[token.user_id] = token

    def _resolve_token_for_user(user_id: int) -> GoogleToken | None:
        return org_token_by_user.get(user_id) or any_token_by_user.get(user_id)

    ordered: list[GoogleToken] = []
    used_token_ids: set[int] = set()

    for user_id in _dedupe_user_ids(preferred_user_ids):
        token = _resolve_token_for_user(user_id)
        if token and token.id not in used_token_ids:
            ordered.append(token)
            used_token_ids.add(token.id)

    privileged_roles = (
        role for role in member_roles if role.role in ("owner", "admin")
    )
    for role in privileged_roles:
        token = _resolve_token_for_user(role.user_id)
        if token and token.id not in used_token_ids:
            ordered.append(token)
            used_token_ids.add(token.id)

    for role in member_roles:
        token = _resolve_token_for_user(role.user_id)
        if not token or token.id in used_token_ids:
            continue
        ordered.append(token)
        used_token_ids.add(token.id)

    return ordered


def _find_user_permission(service, folder_id: str, email: str) -> tuple[str | None, str | None]:
    page_token: str | None = None
    target_email = _normalize_email(email)
    while True:
        payload = service.permissions().list(
            fileId=folder_id,
            fields="nextPageToken,permissions(id,emailAddress,role,type)",
            pageToken=page_token,
            supportsAllDrives=True,
        ).execute()
        for permission in payload.get("permissions", []):
            if permission.get("type") != "user":
                continue
            if _normalize_email(permission.get("emailAddress")) != target_email:
                continue
            return permission.get("id"), permission.get("role")
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return None, None


def _is_retryable_folder_access_error(exc: HttpError) -> bool:
    status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
    if status in (403, 404):
        return True
    message = str(exc).lower()
    return "insufficient" in message or "not found" in message or "forbidden" in message


async def _build_drive_service_for_token(db: Session, token: GoogleToken):
    access_token = await _refresh_access_token(token.encrypted_refresh_token)
    if not access_token:
        return None
    token.last_refreshed_at = datetime.now(timezone.utc)
    db.commit()
    credentials = Credentials(token=access_token)
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


async def sync_member_drive_permission(
    *,
    db: Session,
    org: Organization | None,
    member_email: str | None,
    org_role: str | None,
    preferred_user_ids: Iterable[int | None] = (),
) -> dict:
    """Ensure a member has the correct permission on the workspace root folder."""
    if not org or not org.id:
        return {"ok": False, "reason": "organization_missing"}
    if not org.drive_folder_id:
        return {"ok": True, "status": "skipped", "reason": "drive_root_not_configured"}

    email = _normalize_email(member_email)
    if not email:
        return {"ok": False, "reason": "member_email_missing"}

    target_role = RBAC_TO_DRIVE_ROLE.get((org_role or "").strip().lower())
    if not target_role:
        return {"ok": False, "reason": "unsupported_role", "role": org_role}

    candidate_tokens = _candidate_actor_tokens(
        db=db,
        organization_id=org.id,
        preferred_user_ids=preferred_user_ids,
    )
    if not candidate_tokens:
        return {"ok": False, "reason": "no_google_token"}

    failures: list[dict] = []
    for actor_token in candidate_tokens:
        service = await _build_drive_service_for_token(db, actor_token)
        if not service:
            failures.append({"user_id": actor_token.user_id, "reason": "token_refresh_failed"})
            continue

        try:
            permission_id, current_role = _find_user_permission(service, org.drive_folder_id, email)
            if permission_id:
                if current_role == target_role:
                    return {
                        "ok": True,
                        "status": "unchanged",
                        "drive_role": current_role,
                        "actor_user_id": actor_token.user_id,
                    }
                service.permissions().update(
                    fileId=org.drive_folder_id,
                    permissionId=permission_id,
                    body={"role": target_role},
                    fields="id,role",
                    supportsAllDrives=True,
                ).execute()
                return {
                    "ok": True,
                    "status": "updated",
                    "drive_role": target_role,
                    "actor_user_id": actor_token.user_id,
                }

            service.permissions().create(
                fileId=org.drive_folder_id,
                body={
                    "type": "user",
                    "role": target_role,
                    "emailAddress": email,
                },
                sendNotificationEmail=False,
                fields="id,role",
                supportsAllDrives=True,
            ).execute()
            return {
                "ok": True,
                "status": "created",
                "drive_role": target_role,
                "actor_user_id": actor_token.user_id,
            }
        except HttpError as exc:
            failures.append(
                {
                    "user_id": actor_token.user_id,
                    "reason": "drive_api_error",
                    "status": int(getattr(getattr(exc, "resp", None), "status", 0) or 0),
                    "message": str(exc),
                }
            )
            if _is_retryable_folder_access_error(exc):
                continue
            break

    logger.warning(
        "Drive ACL sync failed for org=%s email=%s role=%s after %s actor attempts",
        org.id,
        email,
        target_role,
        len(candidate_tokens),
    )
    return {"ok": False, "reason": "drive_acl_sync_failed", "attempts": failures}


async def sync_member_drive_file_permission(
    *,
    db: Session,
    org: Organization | None,
    member_email: str | None,
    org_role: str | None,
    drive_file_id: str | None,
    preferred_user_ids: Iterable[int | None] = (),
) -> dict:
    """Ensure a member has the correct permission on a specific Drive file."""
    if not org or not org.id:
        return {"ok": False, "reason": "organization_missing"}

    file_id = (drive_file_id or "").strip()
    if not file_id:
        return {"ok": False, "reason": "drive_file_id_missing"}

    email = _normalize_email(member_email)
    if not email:
        return {"ok": False, "reason": "member_email_missing"}

    target_role = RBAC_TO_DRIVE_ROLE.get((org_role or "").strip().lower())
    if not target_role:
        return {"ok": False, "reason": "unsupported_role", "role": org_role}

    candidate_tokens = _candidate_actor_tokens(
        db=db,
        organization_id=org.id,
        preferred_user_ids=preferred_user_ids,
    )
    if not candidate_tokens:
        return {"ok": False, "reason": "no_google_token"}

    failures: list[dict] = []
    for actor_token in candidate_tokens:
        service = await _build_drive_service_for_token(db, actor_token)
        if not service:
            failures.append({"user_id": actor_token.user_id, "reason": "token_refresh_failed"})
            continue

        try:
            permission_id, current_role = _find_user_permission(service, file_id, email)
            if permission_id:
                if current_role == target_role:
                    return {
                        "ok": True,
                        "status": "unchanged",
                        "drive_role": current_role,
                        "actor_user_id": actor_token.user_id,
                        "file_id": file_id,
                    }
                service.permissions().update(
                    fileId=file_id,
                    permissionId=permission_id,
                    body={"role": target_role},
                    fields="id,role",
                    supportsAllDrives=True,
                ).execute()
                return {
                    "ok": True,
                    "status": "updated",
                    "drive_role": target_role,
                    "actor_user_id": actor_token.user_id,
                    "file_id": file_id,
                }

            service.permissions().create(
                fileId=file_id,
                body={
                    "type": "user",
                    "role": target_role,
                    "emailAddress": email,
                },
                sendNotificationEmail=False,
                fields="id,role",
                supportsAllDrives=True,
            ).execute()
            return {
                "ok": True,
                "status": "created",
                "drive_role": target_role,
                "actor_user_id": actor_token.user_id,
                "file_id": file_id,
            }
        except HttpError as exc:
            failures.append(
                {
                    "user_id": actor_token.user_id,
                    "reason": "drive_api_error",
                    "status": int(getattr(getattr(exc, "resp", None), "status", 0) or 0),
                    "message": str(exc),
                }
            )
            if _is_retryable_folder_access_error(exc):
                continue
            break

    logger.warning(
        "Drive file ACL sync failed for org=%s email=%s role=%s file=%s after %s actor attempts",
        org.id,
        email,
        target_role,
        file_id,
        len(candidate_tokens),
    )
    return {"ok": False, "reason": "drive_file_acl_sync_failed", "attempts": failures}


async def revoke_member_drive_permission(
    *,
    db: Session,
    org: Organization | None,
    member_email: str | None,
    preferred_user_ids: Iterable[int | None] = (),
) -> dict:
    """Remove a member's explicit permission from the workspace root folder."""
    if not org or not org.id:
        return {"ok": False, "reason": "organization_missing"}
    if not org.drive_folder_id:
        return {"ok": True, "status": "skipped", "reason": "drive_root_not_configured"}

    email = _normalize_email(member_email)
    if not email:
        return {"ok": False, "reason": "member_email_missing"}

    candidate_tokens = _candidate_actor_tokens(
        db=db,
        organization_id=org.id,
        preferred_user_ids=preferred_user_ids,
    )
    if not candidate_tokens:
        return {"ok": False, "reason": "no_google_token"}

    failures: list[dict] = []
    for actor_token in candidate_tokens:
        service = await _build_drive_service_for_token(db, actor_token)
        if not service:
            failures.append({"user_id": actor_token.user_id, "reason": "token_refresh_failed"})
            continue

        try:
            permission_id, _ = _find_user_permission(service, org.drive_folder_id, email)
            if not permission_id:
                return {"ok": True, "status": "not_found", "actor_user_id": actor_token.user_id}
            service.permissions().delete(
                fileId=org.drive_folder_id,
                permissionId=permission_id,
                supportsAllDrives=True,
            ).execute()
            return {"ok": True, "status": "removed", "actor_user_id": actor_token.user_id}
        except HttpError as exc:
            failures.append(
                {
                    "user_id": actor_token.user_id,
                    "reason": "drive_api_error",
                    "status": int(getattr(getattr(exc, "resp", None), "status", 0) or 0),
                    "message": str(exc),
                }
            )
            if _is_retryable_folder_access_error(exc):
                continue
            break

    logger.warning("Drive ACL removal failed for org=%s email=%s after retries", org.id, email)
    return {"ok": False, "reason": "drive_acl_revoke_failed", "attempts": failures}


async def sync_org_drive_permissions(
    *,
    db: Session,
    org: Organization | None,
    preferred_user_ids: Iterable[int | None] = (),
) -> dict:
    """Backfill Drive ACLs for all members of an organization."""
    if not org or not org.id:
        return {"ok": False, "reason": "organization_missing"}
    if not org.drive_folder_id:
        return {"ok": True, "status": "skipped", "reason": "drive_root_not_configured"}

    roles = db.query(OrgRole).filter(OrgRole.organization_id == org.id).all()
    success = 0
    failed = 0
    details: list[dict] = []

    for role in roles:
        member = db.get(User, role.user_id)
        result = await sync_member_drive_permission(
            db=db,
            org=org,
            member_email=member.email if member else None,
            org_role=role.role,
            preferred_user_ids=preferred_user_ids,
        )
        details.append(
            {
                "user_id": role.user_id,
                "email": member.email if member else None,
                "role": role.role,
                **result,
            }
        )
        if result.get("ok"):
            success += 1
        else:
            failed += 1

    org.drive_permissions_last_synced_at = datetime.now(timezone.utc).isoformat()
    db.commit()
    return {"ok": failed == 0, "synced": success, "failed": failed, "details": details}
