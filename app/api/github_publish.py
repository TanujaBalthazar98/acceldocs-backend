"""GitHub publishing API — connect a GitHub account and publish docs to GitHub Pages.

Flow:
  1. POST /github/connect        — verify PAT, store encrypted token + username on org
  2. GET  /github/settings/{id}  — return connection status + repo + pages info
  3. POST /github/create-repo    — create a GitHub repo, enable GitHub Pages
  4. POST /github/custom-domain  — set a custom domain on the GitHub Pages site
  5. DELETE /github/disconnect   — clear stored GitHub credentials from the org
"""

import logging
import re
from datetime import datetime, timezone

import requests
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.auth import get_current_user_optional
from app.models import Organization, OrgRole, User
from app.services.encryption import get_encryption_service

logger = logging.getLogger(__name__)
router = APIRouter()

GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_org_and_role(db: Session, user: User, org_id: int):
    """Return (org, role_str) or raise ValueError."""
    org = db.get(Organization, org_id)
    if not org:
        raise ValueError("Organization not found")
    role_row = (
        db.query(OrgRole)
        .filter(OrgRole.user_id == user.id, OrgRole.organization_id == org_id)
        .first()
    )
    if not role_row:
        raise ValueError("You are not a member of this organization")
    return org, role_row.role


def _decrypt_token(org: Organization) -> str | None:
    if not org.github_token_encrypted:
        return None
    try:
        return get_encryption_service().decrypt(org.github_token_encrypted)
    except Exception:
        logger.warning("Failed to decrypt GitHub token for org %s", org.id)
        return None


def _safe_slug(name: str) -> str:
    """Turn an org name into a valid GitHub repo slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "docs"


# ---------------------------------------------------------------------------
# POST /github/connect
# ---------------------------------------------------------------------------

@router.post("/github/connect")
async def connect_github(
    body: dict,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Verify a GitHub Personal Access Token and store it on the org.

    Body: { organizationId, token, repoName? }
    Returns: { ok, username, avatarUrl }
    """
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        org_id = int(body.get("organizationId") or 0)
        token = (body.get("token") or "").strip()
        if not org_id:
            return {"ok": False, "error": "organizationId required"}
        if not token:
            return {"ok": False, "error": "GitHub token required"}

        org, role = _get_org_and_role(db, user, org_id)
        if role not in ("owner", "admin"):
            return {"ok": False, "error": "Only owners and admins can connect GitHub"}

        # Verify token with GitHub
        resp = requests.get(f"{GITHUB_API}/user", headers=_gh_headers(token), timeout=10)
        if resp.status_code == 401:
            return {"ok": False, "error": "Invalid token — please check it and try again"}
        if resp.status_code != 200:
            return {"ok": False, "error": f"GitHub returned {resp.status_code}. Try again later."}

        gh_user = resp.json()
        username = gh_user.get("login", "")
        avatar_url = gh_user.get("avatar_url", "")

        # Check required scopes
        scopes = resp.headers.get("X-OAuth-Scopes", "")
        if "repo" not in scopes and "public_repo" not in scopes:
            return {
                "ok": False,
                "error": (
                    "This token is missing the 'repo' scope. "
                    "Please create a new token with 'repo' (or 'public_repo') access."
                ),
            }

        # Encrypt and store
        encrypted = get_encryption_service().encrypt(token)
        org.github_token_encrypted = encrypted
        org.github_username = username
        db.commit()

        return {"ok": True, "username": username, "avatarUrl": avatar_url}

    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.exception("GitHub connect error")
        return {"ok": False, "error": "Connection failed. Please try again."}


# ---------------------------------------------------------------------------
# GET /github/settings/{org_id}
# ---------------------------------------------------------------------------

@router.get("/github/settings/{org_id}")
async def get_github_settings(
    org_id: int,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Return GitHub connection status, repo info, and Pages URL for an org."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        org, _ = _get_org_and_role(db, user, org_id)
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    connected = bool(org.github_username and org.github_token_encrypted)

    return {
        "ok": True,
        "connected": connected,
        "username": org.github_username,
        "repoName": org.github_repo_name,
        "repoFullName": org.github_repo_full_name,
        "pagesUrl": org.github_pages_url,
        "customDomain": org.github_custom_domain,
        "domainVerified": org.github_domain_verified or False,
        "lastPublishedAt": org.last_published_at,
    }


# ---------------------------------------------------------------------------
# POST /github/create-repo
# ---------------------------------------------------------------------------

@router.post("/github/create-repo")
async def create_github_repo(
    body: dict,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Create a GitHub repo for docs and enable GitHub Pages.

    Body: { organizationId, repoName?, private? }
    Returns: { ok, repo: { fullName, htmlUrl }, pagesUrl }
    """
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        org_id = int(body.get("organizationId") or 0)
        if not org_id:
            return {"ok": False, "error": "organizationId required"}

        org, role = _get_org_and_role(db, user, org_id)
        if role not in ("owner", "admin"):
            return {"ok": False, "error": "Only owners and admins can create repositories"}

        token = _decrypt_token(org)
        if not token:
            return {"ok": False, "error": "GitHub not connected. Please connect your account first."}

        # Determine repo name
        requested_name = (body.get("repoName") or "").strip()
        repo_name = requested_name or f"{_safe_slug(org.name or 'docs')}-docs"

        is_private = bool(body.get("private", False))

        # Create the repo
        create_resp = requests.post(
            f"{GITHUB_API}/user/repos",
            headers=_gh_headers(token),
            json={
                "name": repo_name,
                "description": f"Documentation for {org.name}",
                "private": is_private,
                "auto_init": True,  # creates default branch with README
            },
            timeout=15,
        )

        if create_resp.status_code == 422:
            # Repo already exists — use it
            username = org.github_username
            existing = requests.get(
                f"{GITHUB_API}/repos/{username}/{repo_name}",
                headers=_gh_headers(token),
                timeout=10,
            )
            if existing.status_code != 200:
                return {
                    "ok": False,
                    "error": f"Repository '{repo_name}' already exists but could not be accessed. "
                             "Please choose a different name or delete the existing repo.",
                }
            repo_data = existing.json()
        elif create_resp.status_code not in (200, 201):
            err = create_resp.json().get("message", "Unknown error")
            return {"ok": False, "error": f"GitHub error: {err}"}
        else:
            repo_data = create_resp.json()

        full_name = repo_data["full_name"]
        html_url = repo_data["html_url"]
        default_branch = repo_data.get("default_branch", "main")

        # Enable GitHub Pages from the default branch
        pages_resp = requests.post(
            f"{GITHUB_API}/repos/{full_name}/pages",
            headers=_gh_headers(token),
            json={"source": {"branch": default_branch, "path": "/"}},
            timeout=10,
        )

        # Pages might already be enabled (422) — that's fine
        if pages_resp.status_code in (200, 201):
            pages_data = pages_resp.json()
            pages_url = pages_data.get("html_url", "")
        else:
            # Derive the expected URL
            username = org.github_username or full_name.split("/")[0]
            pages_url = f"https://{username.lower()}.github.io/{repo_name}"

        # Set the remote URL on the docs-site local repo
        _set_docs_repo_remote(full_name, token)

        # Persist to org
        org.github_repo_name = repo_name
        org.github_repo_full_name = full_name
        org.github_pages_url = pages_url
        db.commit()

        return {
            "ok": True,
            "repo": {"fullName": full_name, "htmlUrl": html_url},
            "pagesUrl": pages_url,
        }

    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception:
        logger.exception("GitHub create-repo error")
        return {"ok": False, "error": "Failed to create repository. Please try again."}


def _set_docs_repo_remote(full_name: str, token: str) -> None:
    """Set the remote URL on the local docs git repo so push works."""
    try:
        from app.publishing.git_publisher import get_repo
        from app.config import settings

        remote_url = f"https://oauth2:{token}@github.com/{full_name}.git"
        repo = get_repo()
        if "origin" not in [r.name for r in repo.remotes]:
            repo.create_remote("origin", remote_url)
        else:
            repo.remotes.origin.set_url(remote_url)

        # Update config so future runs use this URL
        # (also store in settings for restart persistence)
        import os
        os.environ["DOCS_REPO_URL"] = f"https://github.com/{full_name}.git"
    except Exception:
        logger.warning("Could not set docs repo remote — push may fail", exc_info=True)


# ---------------------------------------------------------------------------
# POST /github/custom-domain
# ---------------------------------------------------------------------------

@router.post("/github/custom-domain")
async def set_custom_domain(
    body: dict,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Set a custom domain on the GitHub Pages site.

    Body: { organizationId, domain }
    Returns: { ok, domain, cname }
    """
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        org_id = int(body.get("organizationId") or 0)
        domain = (body.get("domain") or "").strip().lower()
        if not org_id:
            return {"ok": False, "error": "organizationId required"}
        if not domain:
            return {"ok": False, "error": "domain required"}

        org, role = _get_org_and_role(db, user, org_id)
        if role not in ("owner", "admin"):
            return {"ok": False, "error": "Only owners and admins can set custom domains"}

        if not org.github_repo_full_name:
            return {"ok": False, "error": "No repository connected yet"}

        token = _decrypt_token(org)
        if not token:
            return {"ok": False, "error": "GitHub not connected"}

        resp = requests.put(
            f"{GITHUB_API}/repos/{org.github_repo_full_name}/pages",
            headers=_gh_headers(token),
            json={"cname": domain},
            timeout=10,
        )

        if resp.status_code not in (200, 204):
            err = resp.json().get("message", "Unknown error") if resp.content else "Unknown error"
            return {"ok": False, "error": f"GitHub error: {err}"}

        org.github_custom_domain = domain
        org.github_domain_verified = False
        db.commit()

        username = org.github_username or org.github_repo_full_name.split("/")[0]
        cname_target = f"{username.lower()}.github.io"

        return {
            "ok": True,
            "domain": domain,
            "cname": cname_target,
        }

    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception:
        logger.exception("GitHub custom-domain error")
        return {"ok": False, "error": "Failed to set custom domain. Please try again."}


# ---------------------------------------------------------------------------
# DELETE /github/disconnect
# ---------------------------------------------------------------------------

@router.delete("/github/disconnect")
async def disconnect_github(
    body: dict,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Remove GitHub credentials from an org."""
    if not user:
        return {"ok": False, "error": "Authentication required"}

    try:
        org_id = int(body.get("organizationId") or 0)
        if not org_id:
            return {"ok": False, "error": "organizationId required"}

        org, role = _get_org_and_role(db, user, org_id)
        if role not in ("owner", "admin"):
            return {"ok": False, "error": "Only owners and admins can disconnect GitHub"}

        org.github_token_encrypted = None
        org.github_username = None
        db.commit()

        return {"ok": True}

    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception:
        logger.exception("GitHub disconnect error")
        return {"ok": False, "error": "Failed to disconnect. Please try again."}
