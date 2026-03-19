"""Google OAuth authentication routes with JWT session tokens."""

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.lib.slugify import to_slug as slugify
from app.models import GoogleToken, JoinRequest, Organization, OrgRole, User
from app.services.drive_acl import sync_member_drive_permission
from app.services.encryption import get_encryption_service
from app.middleware.security import limiter

logger = logging.getLogger(__name__)

router = APIRouter()

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24
DOCS_SESSION_COOKIE = "acceldocs_docs_session"
PUBLIC_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "icloud.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
}


def _allowed_redirect_hosts() -> set[str]:
    hosts = {"localhost", "127.0.0.1", "::1"}

    try:
        frontend_host = (urlparse(settings.frontend_url).hostname or "").lower()
        if frontend_host:
            hosts.add(frontend_host)
    except Exception:
        pass

    try:
        oauth_host = (urlparse(settings.google_oauth_redirect_uri).hostname or "").lower()
        if oauth_host:
            hosts.add(oauth_host)
    except Exception:
        pass

    for origin in settings.allowed_origins_list:
        try:
            origin_host = (urlparse(origin).hostname or "").lower()
            if origin_host:
                hosts.add(origin_host)
        except Exception:
            continue

    return hosts


def _extract_docs_next_from_state(state: str | None) -> str | None:
    """Decode a ``next`` URL from the OAuth state parameter.

    Returns the URL only when it points to an allowed host.
    """
    if not state:
        return None
    import base64 as _b64
    import json as _json

    try:
        payload = _json.loads(_b64.urlsafe_b64decode(state + "=="))
        next_url = payload.get("next")
        if not next_url or not isinstance(next_url, str):
            return None
        from urllib.parse import urlparse as _urlparse

        parsed = _urlparse(next_url)
        host = (parsed.hostname or "").lower()
        if host in _allowed_redirect_hosts():
            return next_url
    except Exception:
        pass
    return None


def _resolve_default_redirect_from_request(request: Request | None) -> str:
    """Build a default redirect URI from trusted app hosts only."""
    trusted_hosts = _allowed_redirect_hosts()

    if request:
        for header_name in ("origin", "referer"):
            raw = (request.headers.get(header_name) or "").strip()
            if not raw:
                continue
            parsed = urlparse(raw)
            host = (parsed.hostname or "").lower()
            if parsed.scheme in {"http", "https"} and parsed.netloc and host in trusted_hosts:
                return f"{parsed.scheme}://{parsed.netloc}/auth/callback"

    return settings.google_oauth_redirect_uri


def _resolve_oauth_redirect_uri_for_request(
    redirect_uri: str | None,
    request: Request | None,
) -> str:
    """Resolve redirect URI while allowing app domains from request + config."""
    configured_default = _resolve_default_redirect_from_request(request)

    candidate = (redirect_uri or "").strip()
    if not candidate:
        return configured_default

    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        logger.warning("Ignoring invalid redirect_uri format: %s", candidate)
        return configured_default

    host = (parsed.hostname or "").lower()
    if host not in _allowed_redirect_hosts():
        logger.warning("Ignoring non-allowed redirect_uri host: %s", candidate)
        return configured_default

    return candidate


def _extract_org_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[1].strip().lower()
    if not domain or domain in PUBLIC_EMAIL_DOMAINS:
        return None
    return domain


def _unique_org_slug(db: Session, name: str | None) -> str:
    """Generate an organization slug that is unique across organizations."""
    base = slugify(name or "workspace") or "workspace"
    slug = base
    suffix = 2
    while db.query(Organization.id).filter(Organization.slug == slug).first():
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


def _create_jwt(user_id: int, email: str) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)


def _is_secure_request(request: Request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return request.url.scheme == "https"


def _check_drive_folder_owner(access_token: str, folder_id: str, user_email: str) -> bool:
    """Check if user owns the Google Drive root folder."""
    try:
        from google.oauth2.credentials import Credentials as UserCredentials
        from googleapiclient.discovery import build

        creds = UserCredentials(token=access_token)
        service = build("drive", "v3", credentials=creds, cache_discovery=False)

        # Get folder metadata
        folder = service.files().get(
            fileId=folder_id,
            fields="owners",
            supportsAllDrives=True,
        ).execute()

        owners = folder.get("owners", [])
        for owner in owners:
            if owner.get("emailAddress") == user_email:
                logger.info(f"User {user_email} owns Drive folder {folder_id}")
                return True

        logger.info(f"User {user_email} does NOT own Drive folder {folder_id}")
        return False
    except Exception as e:
        logger.warning(f"Failed to check folder ownership: {e}")
        return False


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: extract and validate JWT from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = auth_header[7:]
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user_id = int(payload["sub"])
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


@router.post("/prepare-signup")
@limiter.limit("10/minute")
async def prepare_signup(request: Request, db: Session = Depends(get_db)):
    """Prepare signup with organization selection.

    Body:
        - action: "join" or "create"
        - org_id: (if action=join) Organization ID to join
        - org_name: (if action=create) New organization name
        - drive_folder_id: (if action=create) Google Drive root folder ID

    Returns:
        - signup_token: JWT token with org info to pass in OAuth state
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")

    action = body.get("action")
    if action not in ["join", "create"]:
        raise HTTPException(status_code=400, detail="Action must be 'join' or 'create'")

    # Create signup token with org info
    payload = {
        "action": action,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        "iat": datetime.now(timezone.utc),
    }

    if action == "join":
        org_id = body.get("org_id")
        if not org_id:
            raise HTTPException(status_code=400, detail="org_id required for join action")
        payload["org_id"] = org_id
    else:  # create
        org_name = body.get("org_name")
        drive_folder_id = body.get("drive_folder_id")
        if not org_name:
            raise HTTPException(status_code=400, detail="org_name required for create action")
        if not drive_folder_id:
            raise HTTPException(status_code=400, detail="drive_folder_id required for create action")
        existing = (
            db.query(Organization)
            .filter(Organization.name.ilike(org_name.strip()))
            .first()
        )
        if existing:
            raise HTTPException(status_code=409, detail="Organization already exists")
        payload["org_name"] = org_name
        payload["drive_folder_id"] = drive_folder_id

    signup_token = jwt.encode(payload, settings.secret_key, algorithm=JWT_ALGORITHM)

    return {
        "signup_token": signup_token
    }


@router.post("/search-organizations")
@limiter.limit("20/minute")
async def search_organizations(request: Request, db: Session = Depends(get_db)):
    """Public org search endpoint used by sign-up UI."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    query = (body.get("query") or "").strip()
    if len(query) < 2:
        return {"ok": True, "organizations": []}

    q = f"%{query.lower()}%"
    matching_member_org_ids = (
        db.query(OrgRole.organization_id)
        .join(User, User.id == OrgRole.user_id)
        .filter(User.email.ilike(q))
        .distinct()
        .subquery()
    )
    orgs = (
        db.query(Organization)
        .filter(
            or_(
                Organization.name.ilike(q),
                Organization.domain.ilike(q),
                Organization.slug.ilike(q),
                Organization.id.in_(matching_member_org_ids),
            )
        )
        .order_by(Organization.name.asc())
        .limit(20)
        .all()
    )

    results = []
    for org in orgs:
        member_count = db.query(OrgRole).filter(OrgRole.organization_id == org.id).count()
        results.append({
            "id": org.id,
            "name": org.name,
            "domain": org.domain,
            "member_count": member_count,
        })

    return {"ok": True, "organizations": results}


@router.get("/login")
@limiter.limit("15/minute")
async def login(
    request: Request,
    state: str | None = None,
    redirect_uri: str | None = None,
    org_id: int | None = None,
):
    """Return the Google OAuth consent URL.

    Args:
        state: Optional signup token from /prepare-signup
    """
    if not settings.google_client_id:
        raise HTTPException(status_code=501, detail="Google OAuth not configured")

    # Full Drive access — required to move/modify any file, including imported ones
    scopes = [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/drive",
    ]
    scope_param = "%20".join(scopes)
    resolved_redirect_uri = _resolve_oauth_redirect_uri_for_request(redirect_uri, request)

    resolved_state = state
    if not resolved_state and org_id is not None:
        resolved_state = jwt.encode(
            {
                "action": "connect",
                "org_id": org_id,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
                "iat": datetime.now(timezone.utc),
            },
            settings.secret_key,
            algorithm=JWT_ALGORITHM,
        )

    # Include state parameter if provided
    state_param = f"&state={resolved_state}" if resolved_state else ""

    return {
        "url": (
            "https://accounts.google.com/o/oauth2/v2/auth"
            f"?client_id={settings.google_client_id}"
            "&response_type=code"
            f"&scope={scope_param}"
            f"&redirect_uri={resolved_redirect_uri}"
            "&access_type=offline"
            "&prompt=consent"  # Force consent to ensure refresh_token is returned
            f"{state_param}"
        ),
        "redirect_uri": resolved_redirect_uri,
    }


@router.get("/docs-login")
@limiter.limit("15/minute")
async def docs_login(request: Request, next: str | None = None):
    """Browser-redirect login for docs gate pages.

    Redirects the browser straight to Google OAuth.  The ``next`` URL
    (the docs page the user was trying to access) is encoded inside the
    OAuth ``state`` parameter so the callback can redirect back.
    """
    if not settings.google_client_id:
        raise HTTPException(status_code=501, detail="Google OAuth not configured")

    import base64 as _b64
    import json as _json

    scopes = [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/drive",
    ]
    scope_param = "%20".join(scopes)
    # Always use the configured redirect URI — this request originates from
    # the backend gate page so the request Origin/Referer is localhost:8000,
    # which is NOT registered with Google.  The frontend callback URI IS.
    resolved_redirect_uri = settings.google_oauth_redirect_uri

    # Encode the next URL in the state so the callback can redirect back
    state_payload: dict = {}
    if next:
        # Validate the next URL points to our backend (prevent open redirect)
        from urllib.parse import urlparse as _urlparse

        parsed_next = _urlparse(next)
        next_host = (parsed_next.hostname or "").lower()
        if next_host in _allowed_redirect_hosts() or not parsed_next.scheme:
            state_payload["next"] = next
    state_str = _b64.urlsafe_b64encode(_json.dumps(state_payload).encode()).decode()

    google_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={settings.google_client_id}"
        "&response_type=code"
        f"&scope={scope_param}"
        f"&redirect_uri={resolved_redirect_uri}"
        "&access_type=offline"
        "&prompt=consent"
        f"&state={state_str}"
    )
    return RedirectResponse(url=google_url, status_code=302)


@router.get("/callback")
@limiter.limit("10/minute")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    api: bool = False,
    redirect_uri: str | None = None,
    db: Session = Depends(get_db),
):
    """Handle OAuth callback — exchange code for tokens, upsert user, store refresh token, return HTML popup.

    Args:
        code: OAuth authorization code from Google
        state: Optional signup token from /prepare-signup
    """
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=501, detail="Google OAuth not configured")

    # Decode signup state if provided
    signup_info = None
    if state and state.count(".") == 2:
        try:
            signup_info = jwt.decode(state, settings.secret_key, algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=400, detail="Signup token expired")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=400, detail="Invalid signup token")

    # Exchange authorization code for tokens
    resolved_redirect_uri = _resolve_oauth_redirect_uri_for_request(redirect_uri, request)
    logger.info("OAuth callback: exchanging code, redirect_uri=%s", resolved_redirect_uri)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": resolved_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
    except Exception as e:
        logger.exception("Token exchange HTTP request failed: %s", e)
        raise HTTPException(status_code=500, detail="Token exchange request failed. Please try again.")

    if token_resp.status_code != 200:
        logger.error("Token exchange failed (status %s)", token_resp.status_code)
        raise HTTPException(
            status_code=400,
            detail="Failed to exchange authorization code. Please try signing in again.",
        )

    tokens = token_resp.json()
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    scope = tokens.get("scope", "")

    if not access_token:
        raise HTTPException(status_code=400, detail="No access token in response")

    # Fetch user info from Google
    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if userinfo_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch user info")

    userinfo = userinfo_resp.json()
    google_id = userinfo.get("sub")
    email = userinfo.get("email")
    name = userinfo.get("name")

    if not google_id or not email:
        raise HTTPException(status_code=400, detail="Incomplete user info from Google")

    # Determine role: owner if they own the root Drive folder, else viewer
    is_folder_owner = False
    if settings.google_drive_root_folder_id:
        is_folder_owner = _check_drive_folder_owner(
            access_token, settings.google_drive_root_folder_id, email
        )

    # Upsert user in DB
    is_new_user = False
    user = db.query(User).filter(User.google_id == google_id).first()
    if user:
        user.email = email
        user.name = name
        if is_folder_owner and user.role != "owner":
            user.role = "owner"
            logger.info(f"Promoted {email} to owner (owns Drive root folder)")
    else:
        is_new_user = True
        role = "owner" if is_folder_owner else "viewer"
        user = User(google_id=google_id, email=email, name=name, role=role)
        db.add(user)
        db.flush()
        logger.info(f"Created new user {email} with role {role}")

    # Handle organization assignment based on signup info
    org_role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
    organization_id: int | None = None
    pending_join_result: dict | None = None

    if signup_info:
        action = signup_info.get("action")

        if action == "connect":
            target_org_id = signup_info.get("org_id")
            if not target_org_id:
                raise HTTPException(status_code=400, detail="org_id required for connect action")
            target_role = (
                db.query(OrgRole)
                .filter(OrgRole.user_id == user.id, OrgRole.organization_id == target_org_id)
                .first()
            )
            if not target_role:
                raise HTTPException(status_code=403, detail="You are not a member of the selected workspace")
            if target_role.role not in ("owner", "admin"):
                raise HTTPException(status_code=403, detail="Only workspace owner/admin can connect Drive")
            organization_id = target_role.organization_id
            org_role = target_role

        elif action == "join":
            org_id = signup_info.get("org_id")
            organization = db.query(Organization).filter(Organization.id == org_id).first()
            if not organization:
                raise HTTPException(status_code=400, detail="Organization not found")

            existing_target_role = (
                db.query(OrgRole)
                .filter(OrgRole.user_id == user.id, OrgRole.organization_id == organization.id)
                .first()
            )
            if existing_target_role:
                organization_id = organization.id
                org_role = existing_target_role
            else:
                org_domain = (organization.domain or "").strip().lower()
                if org_domain:
                    user_domain = (email.split("@", 1)[1] if "@" in (email or "") else "").strip().lower()
                    if user_domain != org_domain:
                        raise HTTPException(
                            status_code=403,
                            detail=f"Only @{org_domain} accounts can request access to this workspace",
                        )

                pending_request = (
                    db.query(JoinRequest)
                    .filter(
                        JoinRequest.organization_id == organization.id,
                        JoinRequest.user_id == user.id,
                        JoinRequest.status == "pending",
                    )
                    .first()
                )
                if not pending_request:
                    pending_request = JoinRequest(
                        organization_id=organization.id,
                        user_id=user.id,
                        message=None,
                        status="pending",
                    )
                    db.add(pending_request)
                    db.flush()
                    logger.info(
                        "Created join request for %s into organization %s",
                        user.email,
                        organization.name,
                    )

                pending_join_result = {
                    "organization": {
                        "id": organization.id,
                        "name": organization.name,
                        "slug": organization.slug,
                    },
                    "request_id": pending_request.id,
                    "requested_at": pending_request.created_at.isoformat() if pending_request.created_at else None,
                }

        elif action == "create":
            org_name = signup_info.get("org_name")
            drive_folder_id = signup_info.get("drive_folder_id")
            inferred_domain = _extract_org_domain(email)
            claimed_domain = None
            if inferred_domain:
                domain_in_use = db.query(Organization).filter(Organization.domain == inferred_domain).first()
                if not domain_in_use:
                    claimed_domain = inferred_domain

            organization = Organization(
                name=org_name,
                slug=_unique_org_slug(db, org_name),
                drive_folder_id=drive_folder_id,
                domain=claimed_domain,
                owner_id=user.id
            )
            db.add(organization)
            db.flush()

            org_role = OrgRole(organization_id=organization.id, user_id=user.id, role="owner")
            db.add(org_role)
            db.flush()
            organization_id = organization.id
            logger.info(f"Created organization {org_name} with root folder {drive_folder_id} for user {user.email}")

        else:
            raise HTTPException(status_code=400, detail=f"Invalid signup action: {action}")
    elif org_role:
        # User already has an organization
        organization_id = org_role.organization_id

    else:
        # No signup info — auto-join by email domain, or create default workspace
        # Self-heal orphan owners from earlier partial signup states.
        owned_org = (
            db.query(Organization)
            .filter(Organization.owner_id == user.id)
            .order_by(Organization.id.asc())
            .first()
        )
        if owned_org:
            existing_owned_role = (
                db.query(OrgRole)
                .filter(OrgRole.organization_id == owned_org.id, OrgRole.user_id == user.id)
                .first()
            )
            if not existing_owned_role:
                db.add(OrgRole(organization_id=owned_org.id, user_id=user.id, role="owner"))
                db.flush()
                logger.info(
                    "Restored missing owner membership for user %s in organization %s",
                    user.email,
                    owned_org.name,
                )
            organization_id = owned_org.id
            org_role = (
                db.query(OrgRole)
                .filter(OrgRole.organization_id == owned_org.id, OrgRole.user_id == user.id)
                .first()
            )
            logger.info("Using owned organization %s for user %s", owned_org.name, user.email)
        else:
            inferred_domain = _extract_org_domain(email)
            existing_org = None
            if inferred_domain:
                existing_org = db.query(Organization).filter(Organization.domain == inferred_domain).first()

            if existing_org:
                # First member of an ownerless org becomes owner;
                # otherwise auto-joined teammates get editor role.
                existing_count = db.query(OrgRole).filter(OrgRole.organization_id == existing_org.id).count()
                auto_role = "owner" if existing_count == 0 else "editor"
                org_role = OrgRole(organization_id=existing_org.id, user_id=user.id, role=auto_role)
                db.add(org_role)
                db.flush()
                organization_id = existing_org.id
                logger.info(
                    f"Auto-joined {user.email} to organization {existing_org.name} via domain {inferred_domain} as {auto_role}"
                )
            else:
                # Create default workspace — dashboard onboarding will guide setup
                default_name = f"{user.name}'s Workspace" if user.name else f"{user.email}'s Workspace"
                claimed_domain = None
                if inferred_domain:
                    domain_in_use = db.query(Organization).filter(Organization.domain == inferred_domain).first()
                    if not domain_in_use:
                        claimed_domain = inferred_domain

                organization = Organization(
                    name=default_name,
                    slug=_unique_org_slug(db, default_name),
                    domain=claimed_domain,
                    owner_id=user.id,
                )
                db.add(organization)
                db.flush()

                org_role = OrgRole(organization_id=organization.id, user_id=user.id, role="owner")
                db.add(org_role)
                db.flush()
                organization_id = organization.id
                logger.info(f"Created default workspace '{default_name}' for user {user.email}")

    # Persist user + org membership atomically before optional token storage.
    # This avoids partial "user-only" records when org assignment fails.
    db.commit()
    db.refresh(user)

    if pending_join_result:
        accept_header = (request.headers.get("accept") or "").lower()
        wants_json = api or ("application/json" in accept_header) or (request.headers.get("x-requested-with") == "XMLHttpRequest")
        redirect_target = f"{settings.frontend_url.rstrip('/')}/signup?requested=1&org={pending_join_result['organization']['id']}"
        if wants_json:
            return {
                "error": "join_request_pending",
                "message": "Join request submitted for owner approval",
                "redirect": redirect_target,
                **pending_join_result,
            }
        return RedirectResponse(url=redirect_target, status_code=302)

    # Store encrypted refresh token if provided
    if refresh_token and organization_id:
        try:
            encryption_service = get_encryption_service()
            encrypted_refresh_token = encryption_service.encrypt(refresh_token)

            # Upsert GoogleToken record
            existing_token = db.query(GoogleToken).filter(
                GoogleToken.user_id == user.id,
                GoogleToken.organization_id == organization_id
            ).first()

            now = datetime.now(timezone.utc)
            if existing_token:
                existing_token.encrypted_refresh_token = encrypted_refresh_token
                existing_token.scope = scope
                existing_token.token_created_at = now
                existing_token.last_refreshed_at = now
                existing_token.updated_at = now
                logger.info(f"Updated refresh token for user {user.email}")
            else:
                google_token = GoogleToken(
                    user_id=user.id,
                    organization_id=organization_id,
                    encrypted_refresh_token=encrypted_refresh_token,
                    scope=scope,
                    token_created_at=now,
                    last_refreshed_at=now
                )
                db.add(google_token)
                logger.info(f"Stored new refresh token for user {user.email}")

            db.commit()
        except Exception as e:
            logger.error(f"Failed to store refresh token: {e}")
            # Don't fail the auth flow if token storage fails
            db.rollback()

    # Keep Drive ACL aligned with RBAC across all memberships so invited users
    # can open docs even when they log into a different default workspace.
    try:
        memberships = db.query(OrgRole).filter(OrgRole.user_id == user.id).all()
        for membership in memberships:
            member_org = db.get(Organization, membership.organization_id)
            if not member_org or not member_org.drive_folder_id:
                continue
            sync_result = await sync_member_drive_permission(
                db=db,
                org=member_org,
                member_email=user.email,
                org_role=membership.role,
                preferred_user_ids=[member_org.owner_id, user.id],
            )
            if not sync_result.get("ok"):
                logger.warning(
                    "Drive ACL sync after login was not fully successful for %s in org %s: %s",
                    user.email,
                    membership.organization_id,
                    sync_result,
                )
    except Exception as exc:
        logger.warning("Drive ACL sync after login failed for %s: %s", user.email, exc)

    # Create JWT session token
    jwt_token = _create_jwt(user.id, user.email)

    accept_header = (request.headers.get("accept") or "").lower()
    wants_json = api or ("application/json" in accept_header) or (request.headers.get("x-requested-with") == "XMLHttpRequest")

    if wants_json:
        return {
            "access_token": jwt_token,
            "token_type": "bearer",
            "google_access_token": access_token,
            "user": {
                "id": user.id,
                "email": user.email,
                "name": user.name,
                "role": user.role,
                "google_id": user.google_id,
                "created_at": user.created_at.isoformat() if user.created_at else None,
            },
        }

    # If the OAuth state contains a ``next`` URL (from /auth/docs-login),
    # redirect back to that docs page with auth_token so the cookie
    # bootstrap flow kicks in.
    docs_next_url = _extract_docs_next_from_state(state)
    if docs_next_url:
        from urllib.parse import urlencode as _urlencode, urlparse as _urlparse, parse_qs as _parse_qs, urlunparse as _urlunparse

        parsed = _urlparse(docs_next_url)
        qs = _parse_qs(parsed.query, keep_blank_values=True)
        qs["auth_token"] = [jwt_token]
        new_query = _urlencode(qs, doseq=True)
        target = _urlunparse(parsed._replace(query=new_query))
        return RedirectResponse(url=target, status_code=302)

    # Redirect to frontend /auth/callback with token in URL params.
    # The frontend AuthCallback page reads the token and stores it in localStorage.
    import urllib.parse
    frontend_url = settings.frontend_url.rstrip("/")
    params = urllib.parse.urlencode({
        "token": jwt_token,
        "google_access_token": access_token,
    })
    return RedirectResponse(url=f"{frontend_url}/auth/callback?{params}")


@router.post("/logout")
async def logout(request: Request):
    """Stateless JWT logout endpoint for frontend compatibility."""
    response = JSONResponse({"ok": True})
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/")
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/docs")
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/internal-docs")
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/external-docs")
    return response


@router.post("/docs-session")
async def set_docs_session_cookie(request: Request, user: User = Depends(get_current_user)):
    """Set docs-only HttpOnly session cookie for rendered /docs pages.

    Frontend calls this once before redirecting browser to backend-rendered docs.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    token = auth_header[7:]
    expires_in = JWT_EXPIRY_HOURS * 3600

    response = JSONResponse(
        {
            "ok": True,
            "user_id": user.id,
            "expires_in": expires_in,
        }
    )
    # Remove any legacy path-scoped cookies first to avoid duplicate cookie-name
    # collisions across /docs and /internal-docs.
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/")
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/docs")
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/internal-docs")
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/external-docs")
    response.set_cookie(
        key=DOCS_SESSION_COOKIE,
        value=token,
        max_age=expires_in,
        httponly=True,
        secure=_is_secure_request(request),
        samesite="lax",
        path="/",
    )
    return response


@router.delete("/docs-session")
async def clear_docs_session_cookie():
    """Clear docs session cookie."""
    response = JSONResponse({"ok": True})
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/")
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/docs")
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/internal-docs")
    response.delete_cookie(DOCS_SESSION_COOKIE, path="/external-docs")
    return response


@router.get("/me")
async def me(request: Request, user: User = Depends(get_current_user)):
    """Return the current authenticated user with token expiry info."""
    # Extract expiry from the JWT so the frontend can schedule refresh
    auth_header = request.headers.get("Authorization", "")
    expires_at = None
    if auth_header.startswith("Bearer "):
        try:
            payload = jwt.decode(
                auth_header[7:], settings.secret_key, algorithms=[JWT_ALGORITHM]
            )
            expires_at = payload.get("exp")
        except Exception:
            pass

    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "expires_at": expires_at,
    }


@router.post("/refresh")
@limiter.limit("10/minute")
async def refresh_token(request: Request, db: Session = Depends(get_db)):
    """Issue a fresh JWT if the current token is valid or recently expired (within 7-day grace).

    Also returns a fresh Google access token if a refresh token is stored.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    token = auth_header[7:]
    user_id = None
    email = None

    # Try to decode — allow recently expired tokens (7-day grace period)
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[JWT_ALGORITHM])
        user_id = int(payload["sub"])
        email = payload.get("email")
    except jwt.ExpiredSignatureError:
        # Decode without verification to get claims from expired token
        try:
            payload = jwt.decode(
                token, settings.secret_key, algorithms=[JWT_ALGORITHM],
                options={"verify_exp": False}
            )
            user_id = int(payload["sub"])
            email = payload.get("email")
            # Check grace period: only allow refresh within 7 days of expiry
            exp = payload.get("exp", 0)
            now = datetime.now(timezone.utc).timestamp()
            if now - exp > 7 * 24 * 3600:
                raise HTTPException(status_code=401, detail="Token expired beyond grace period")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token claims")

    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # Issue fresh JWT
    new_jwt = _create_jwt(user.id, user.email)

    # Try to get a fresh Google access token using stored refresh token
    google_access_token = None
    org_role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()
    if org_role:
        google_token = db.query(GoogleToken).filter(
            GoogleToken.user_id == user.id,
            GoogleToken.organization_id == org_role.organization_id,
        ).first()

        if google_token and google_token.encrypted_refresh_token:
            try:
                encryption_service = get_encryption_service()
                refresh_tok = encryption_service.decrypt(google_token.encrypted_refresh_token)

                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(GOOGLE_TOKEN_URL, data={
                        "client_id": settings.google_client_id,
                        "client_secret": settings.google_client_secret,
                        "refresh_token": refresh_tok,
                        "grant_type": "refresh_token",
                    })

                if resp.status_code == 200:
                    token_data = resp.json()
                    google_access_token = token_data.get("access_token")
                    google_token.last_refreshed_at = datetime.now(timezone.utc)
                    db.commit()
                    logger.info(f"Refreshed Google access token for user {user.email}")
                else:
                    logger.warning(f"Google token refresh failed: {resp.status_code}")
            except Exception as e:
                logger.error(f"Error refreshing Google token: {e}")

    # Decode expiry from the new JWT
    new_payload = jwt.decode(new_jwt, settings.secret_key, algorithms=[JWT_ALGORITHM])

    return {
        "access_token": new_jwt,
        "token_type": "bearer",
        "expires_at": new_payload.get("exp"),
        "google_access_token": google_access_token,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        },
    }
