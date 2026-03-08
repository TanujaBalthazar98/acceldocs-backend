"""Git-based publishing — write Markdown files to git branches.

Preview workflow:
  - status=review → write to docs-preview branch
  - status=approved → write to main branch

Also regenerates zensical.toml after content changes.
"""

import logging
from pathlib import Path
from typing import Any

import git

from app.config import settings
from app.publishing.mkdocs_gen import write_zensical_toml

# Module-level dict: callers can set branding before publishing so the
# generated config reflects the organization's theme.
_current_branding: dict[str, Any] = {}

logger = logging.getLogger(__name__)

PREVIEW_BRANCH = "docs-preview"
MAIN_BRANCH = "main"


def get_repo() -> git.Repo:
    """Get or clone the docs site repository."""
    repo_path = Path(settings.docs_repo_path)

    if repo_path.exists() and (repo_path / ".git").exists():
        repo = git.Repo(repo_path)
        _ensure_seed_commit(repo, repo_path)
        try:
            if repo.remotes:
                repo.remotes.origin.fetch()
        except Exception:
            logger.warning("Could not fetch from origin")
        return repo

    # Clone only for an empty destination and a real remote URL.
    remote_url = (settings.docs_repo_url or "").strip()
    has_local_files = repo_path.exists() and any(repo_path.iterdir())
    looks_like_placeholder = "your-org" in remote_url or remote_url.endswith("example.com")

    if remote_url and not has_local_files and not looks_like_placeholder:
        logger.info("Cloning docs repo from %s", settings.docs_repo_url)
        return git.Repo.clone_from(settings.docs_repo_url, repo_path)

    logger.info("Initializing new docs repo at %s", repo_path)
    repo_path.mkdir(parents=True, exist_ok=True)
    repo = git.Repo.init(repo_path)
    _ensure_seed_commit(repo, repo_path)

    return repo


def publish_document(
    project: str,
    version: str,
    section: str | None,
    slug: str,
    markdown_content: str,
    branch: str = MAIN_BRANCH,
) -> str | None:
    """Write a Markdown file to docs repo on the specified branch."""
    active_branch: str | None = None
    try:
        repo = get_repo()
        repo_path = Path(settings.docs_repo_path)
        active_branch = _current_branch_name(repo)

        _ensure_branch(repo, branch)
        rel_path = _document_rel_path(project, version, section, slug)
        full_path = repo_path / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(markdown_content, encoding="utf-8")
        index_paths = _ensure_parent_indexes(repo_path, full_path)

        cfg_path = write_zensical_toml(repo_path, **_current_branding)

        # Track all generated files
        files_to_add = [rel_path, str(cfg_path.relative_to(repo_path)), *index_paths]
        # Also add custom CSS if it was generated
        css_path = repo_path / "docs" / "stylesheets" / "extra.css"
        if css_path.exists():
            files_to_add.append(str(css_path.relative_to(repo_path)))

        repo.index.add(files_to_add)

        if not repo.is_dirty(untracked_files=True):
            logger.info("No changes to commit for %s", rel_path)
            return None

        commit = repo.index.commit(f"Update {project}/{version}/{slug}")
        logger.info("Published %s to %s (commit %s)", rel_path, branch, commit.hexsha[:8])
        return commit.hexsha

    except Exception:
        logger.exception("Failed to publish %s/%s/%s", project, version, slug)
        return None
    finally:
        _restore_branch(repo if "repo" in locals() else None, active_branch)


def publish_to_preview(
    project: str, version: str, section: str | None, slug: str, markdown: str
) -> str | None:
    return publish_document(project, version, section, slug, markdown, branch=PREVIEW_BRANCH)


def publish_to_production(
    project: str, version: str, section: str | None, slug: str, markdown: str
) -> str | None:
    return publish_document(project, version, section, slug, markdown, branch=MAIN_BRANCH)


def unpublish_from_production(
    project: str, version: str, section: str | None, slug: str
) -> str | None:
    """Remove a published document from production branch and regenerate nav."""
    active_branch: str | None = None
    try:
        repo = get_repo()
        repo_path = Path(settings.docs_repo_path)
        active_branch = _current_branch_name(repo)
        _ensure_branch(repo, MAIN_BRANCH)

        rel_path = _document_rel_path(project, version, section, slug)
        full_path = repo_path / rel_path
        if full_path.exists():
            full_path.unlink()

        cfg_path = write_zensical_toml(repo_path, **_current_branding)
        repo.index.add([str(cfg_path.relative_to(repo_path))])
        if full_path.exists():
            repo.index.add([rel_path])
        else:
            try:
                repo.index.remove([rel_path], working_tree=True)
            except Exception:
                # Already not tracked
                pass

        if not repo.is_dirty(untracked_files=True):
            logger.info("No changes to unpublish for %s", rel_path)
            return None

        commit = repo.index.commit(f"Unpublish {project}/{version}/{slug}")
        logger.info("Unpublished %s from %s (commit %s)", rel_path, MAIN_BRANCH, commit.hexsha[:8])
        return commit.hexsha
    except Exception:
        logger.exception("Failed to unpublish %s/%s/%s", project, version, slug)
        return None
    finally:
        _restore_branch(repo if "repo" in locals() else None, active_branch)


def push_branch(branch: str = MAIN_BRANCH) -> bool:
    try:
        repo = get_repo()
        if "origin" not in [r.name for r in repo.remotes]:
            logger.warning("No origin remote configured — skipping push")
            return False
        repo.remotes.origin.push(branch)
        logger.info("Pushed %s to origin", branch)
        return True
    except Exception:
        logger.exception("Failed to push %s", branch)
        return False


def _ensure_branch(repo: git.Repo, branch_name: str) -> None:
    if branch_name in [b.name for b in repo.branches]:
        repo.heads[branch_name].checkout()
    else:
        repo.create_head(branch_name)
        repo.heads[branch_name].checkout()


def _safe_path(name: str) -> str:
    return name.replace(" ", "-").replace("/", "-").lower().strip("-")


def _document_rel_path(project: str, version: str, section: str | None, slug: str) -> str:
    rel_parts = ["docs", _safe_path(project)]
    if version:
        rel_parts.append(_safe_path(version))
    if section:
        for part in section.split("/"):
            if part.strip():
                rel_parts.append(_safe_path(part))
    rel_parts.append(f"{_safe_path(slug)}.md")
    return "/".join(rel_parts)


def _current_branch_name(repo: git.Repo) -> str | None:
    try:
        if repo.head.is_detached:
            return None
        return repo.active_branch.name
    except Exception:
        return None


def _restore_branch(repo: git.Repo | None, branch_name: str | None) -> None:
    if repo is None:
        return
    try:
        target = MAIN_BRANCH
        if branch_name and branch_name in [b.name for b in repo.branches]:
            target = branch_name
        elif MAIN_BRANCH not in [b.name for b in repo.branches] and branch_name:
            target = branch_name
        if target in [b.name for b in repo.branches]:
            repo.heads[target].checkout()
    except Exception:
        logger.warning("Failed to restore branch after publish operation")


def _ensure_parent_indexes(repo_path: Path, full_doc_path: Path) -> list[str]:
    """Create index.md files for parent folders (project/version/sections)."""
    created: list[str] = []
    docs_root = repo_path / "docs"
    current = full_doc_path.parent

    while current != docs_root and docs_root in current.parents:
        index_md = current / "index.md"
        if not index_md.exists():
            title = current.name.replace("-", " ").replace("_", " ").title()
            index_md.write_text(
                f"# {title}\n\nAuto-generated index page for {title}.\n",
                encoding="utf-8",
            )
            created.append(str(index_md.relative_to(repo_path)))
        current = current.parent

    return created


def _ensure_seed_commit(repo: git.Repo, repo_path: Path) -> None:
    """Ensure repository has at least one commit and starter docs."""
    try:
        _ = repo.head.commit
        return
    except Exception:
        pass

    docs_dir = repo_path / "docs"
    docs_dir.mkdir(exist_ok=True)
    index_md = docs_dir / "index.md"
    if not index_md.exists():
        index_md.write_text("# AccelDocs\n\nWelcome to the documentation.\n")
    cfg_path = write_zensical_toml(repo_path, **_current_branding)
    repo.index.add(["docs/index.md", str(cfg_path.relative_to(repo_path))])
    repo.index.commit("Initial docs structure")
