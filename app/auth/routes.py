"""Google OAuth authentication routes with JWT session tokens."""

import logging
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import GoogleToken, Organization, OrgRole, User
from app.services.encryption import get_encryption_service

logger = logging.getLogger(__name__)

router = APIRouter()

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 24
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


def _extract_org_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[1].strip().lower()
    if not domain or domain in PUBLIC_EMAIL_DOMAINS:
        return None
    return domain


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
async def login(state: str | None = None):
    """Return the Google OAuth consent URL.

    Args:
        state: Optional signup token from /prepare-signup
    """
    if not settings.google_client_id:
        raise HTTPException(status_code=501, detail="Google OAuth not configured")

    # Request both drive.readonly (browse) and drive.file (create/edit owned files)
    scopes = [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.file"
    ]
    scope_param = "%20".join(scopes)

    # Include state parameter if provided
    state_param = f"&state={state}" if state else ""

    return {
        "url": (
            "https://accounts.google.com/o/oauth2/v2/auth"
            f"?client_id={settings.google_client_id}"
            "&response_type=code"
            f"&scope={scope_param}"
            f"&redirect_uri={settings.google_oauth_redirect_uri}"
            "&access_type=offline"
            "&prompt=consent"  # Force consent to ensure refresh_token is returned
            f"{state_param}"
        )
    }


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    api: bool = False,
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
    if state:
        try:
            signup_info = jwt.decode(state, settings.secret_key, algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=400, detail="Signup token expired")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=400, detail="Invalid signup token")

    # Exchange authorization code for tokens
    logger.info("OAuth callback: exchanging code, redirect_uri=%s", settings.google_oauth_redirect_uri)
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.google_client_id,
                    "client_secret": settings.google_client_secret,
                    "redirect_uri": settings.google_oauth_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
    except Exception as e:
        logger.exception("Token exchange HTTP request failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Token exchange request failed: {e}")

    if token_resp.status_code != 200:
        logger.error("Token exchange failed (status %s): %s", token_resp.status_code, token_resp.text)
        raise HTTPException(status_code=400, detail=f"Failed to exchange authorization code: {token_resp.text}")

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

    db.commit()
    db.refresh(user)

    # Handle organization assignment based on signup info
    org_role = db.query(OrgRole).filter(OrgRole.user_id == user.id).first()

    if org_role:
        # User already has an organization
        organization_id = org_role.organization_id
    elif signup_info:
        # Process signup with org selection
        action = signup_info.get("action")

        if action == "join":
            org_id = signup_info.get("org_id")
            organization = db.query(Organization).filter(Organization.id == org_id).first()

            if not organization:
                raise HTTPException(status_code=400, detail="Organization not found")

            # First member to join an ownerless org becomes the owner;
            # otherwise default to editor so teammates can contribute.
            existing_members = db.query(OrgRole).filter(OrgRole.organization_id == organization.id).count()
            if existing_members == 0:
                join_role = "owner"
            else:
                join_role = "editor"
            org_role = OrgRole(organization_id=organization.id, user_id=user.id, role=join_role)
            db.add(org_role)
            db.flush()
            organization_id = organization.id
            logger.info(f"Added user {user.email} to organization {organization.name} as {join_role}")

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

    else:
        # No signup info — auto-join by email domain, or create default workspace
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
            logger.info(f"Auto-joined {user.email} to organization {existing_org.name} via domain {inferred_domain} as {auto_role}")
        else:
            # Create default workspace — dashboard onboarding will guide setup
            default_name = f"{user.name}'s Workspace" if user.name else f"{user.email}'s Workspace"
            claimed_domain = None
            if inferred_domain:
                domain_in_use = db.query(Organization).filter(Organization.domain == inferred_domain).first()
                if not domain_in_use:
                    claimed_domain = inferred_domain

            organization = Organization(name=default_name, domain=claimed_domain, owner_id=user.id)
            db.add(organization)
            db.flush()

            org_role = OrgRole(organization_id=organization.id, user_id=user.id, role="owner")
            db.add(org_role)
            db.flush()
            organization_id = organization.id
            logger.info(f"Created default workspace '{default_name}' for user {user.email}")

    # Store encrypted refresh token if provided
    if refresh_token:
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

    # Return HTML that handles both popup flow (postMessage) and redirect flow (store + redirect)
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Authentication Success</title>
    </head>
    <body>
        <script>
            var authData = {{
                type: 'GOOGLE_AUTH_SUCCESS',
                accessToken: '{access_token}',
                jwt: '{jwt_token}',
                isNewUser: {'true' if is_new_user else 'false'},
                user: {{
                    id: {user.id},
                    email: '{user.email}',
                    name: '{user.name or ""}',
                    role: '{user.role}'
                }}
            }};

            if (window.opener) {{
                // Popup flow: send message to parent and close
                window.opener.postMessage(authData, '*');
                window.close();
            }} else {{
                // Redirect flow: store token in localStorage and redirect
                try {{
                    localStorage.setItem('acceldocs_auth_token', authData.jwt);
                    localStorage.setItem('google_access_token', authData.accessToken);
                }} catch(e) {{}}
                // New users go to dashboard (onboarding will trigger automatically)
                window.location.href = '/dashboard';
            }}
        </script>
        <p>Authentication successful! Redirecting...</p>
    </body>
    </html>
    """

    return HTMLResponse(content=html_content)


@router.post("/logout")
async def logout():
    """Stateless JWT logout endpoint for frontend compatibility."""
    return {"ok": True}


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
