"""Git-based publishing — write Markdown files to git branches.

Preview workflow:
  - status=review → write to docs-preview branch
  - status=approved → write to main branch

Also regenerates zensical.toml after content changes.
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import git

from app.config import settings
from app.publishing.mkdocs_gen import write_zensical_toml

# Module-level dict: callers can set branding before publishing so the
# generated config reflects the organization's theme.
_current_branding: dict[str, Any] = {}

# Per-org repo path: set by _set_branding_from_doc in documents.py so that
# each organization's docs are isolated in their own subdirectory.
# Falls back to settings.docs_repo_path (legacy / single-tenant).
_current_repo_path: Path | None = None


def _get_repo_path() -> Path:
    """Return the active org-scoped repo path, falling back to the global setting."""
    return _current_repo_path or Path(settings.docs_repo_path)


logger = logging.getLogger(__name__)

PREVIEW_BRANCH = "docs-preview"
MAIN_BRANCH = "main"


def get_repo() -> git.Repo:
    """Get or clone the docs site repository."""
    repo_path = _get_repo_path()

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
    *,
    product: str | None = None,
) -> str | None:
    """Write a Markdown file to docs repo on the specified branch."""
    active_branch: str | None = None
    try:
        repo = get_repo()
        repo_path = _get_repo_path()
        active_branch = _current_branch_name(repo)

        _ensure_branch(repo, branch)
        rel_path = _document_rel_path(project, version, section, slug, product=product)
        full_path = repo_path / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(markdown_content, encoding="utf-8")
        index_paths = _ensure_parent_indexes(repo_path, full_path)

        cfg_path = write_zensical_toml(repo_path, **_current_branding)

        # Refresh homepage index.md if it still holds placeholder text
        index_md_path = repo_path / "docs" / "index.md"
        _PLACEHOLDER_HEADINGS = ("# AccelDocs", "# Documentation")
        if index_md_path.exists():
            current_heading = index_md_path.read_text(encoding="utf-8").splitlines()[0].strip()
            if current_heading in _PLACEHOLDER_HEADINGS:
                site_name = _current_branding.get("site_name") or "Documentation"
                index_md_path.write_text(f"# {site_name}\n\nWelcome to the documentation.\n", encoding="utf-8")

        # Track all generated files
        files_to_add = [
            rel_path,
            str(cfg_path.relative_to(repo_path)),
            *index_paths,
        ]
        # Also add custom CSS if it was generated
        css_path = repo_path / "docs" / "stylesheets" / "extra.css"
        if css_path.exists():
            files_to_add.append(str(css_path.relative_to(repo_path)))
        if index_md_path.exists():
            files_to_add.append(str(index_md_path.relative_to(repo_path)))

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
    project: str, version: str, section: str | None, slug: str, markdown: str,
    *, product: str | None = None,
) -> str | None:
    return publish_document(project, version, section, slug, markdown, branch=PREVIEW_BRANCH,
                            product=product)


def publish_to_production(
    project: str, version: str, section: str | None, slug: str, markdown: str,
    *, product: str | None = None,
) -> str | None:
    return publish_document(project, version, section, slug, markdown, branch=MAIN_BRANCH,
                            product=product)


def promote_preview_to_production(
    project: str, version: str, section: str | None, slug: str,
    *, product: str | None = None,
) -> tuple[str | None, str | None]:
    """Read the markdown from docs-preview branch and publish it to main.

    Returns (commit_sha, markdown) — markdown is None if preview file not found.
    This lets approval publish exactly what the reviewer saw in preview,
    without needing any Drive credentials.
    """
    active_branch: str | None = None
    try:
        repo = get_repo()
        repo_path = _get_repo_path()
        active_branch = _current_branch_name(repo)
        rel_path = _document_rel_path(project, version, section, slug, product=product)

        # Read markdown from preview branch
        _ensure_branch(repo, PREVIEW_BRANCH)
        preview_file = repo_path / rel_path
        if not preview_file.exists():
            logger.warning("No preview file found at %s on %s", rel_path, PREVIEW_BRANCH)
            return None, None

        markdown = preview_file.read_text(encoding="utf-8")
    except Exception:
        logger.exception("Failed to read preview file for %s/%s/%s", project, version, slug)
        return None, None
    finally:
        _restore_branch(repo if "repo" in locals() else None, active_branch)

    # Now publish that markdown to production
    commit_sha = publish_document(project, version, section, slug, markdown, branch=MAIN_BRANCH)
    return commit_sha, markdown


def unpublish_from_production(
    project: str, version: str, section: str | None, slug: str,
    *, product: str | None = None,
) -> str | None:
    """Remove a published document from production branch and regenerate nav."""
    active_branch: str | None = None
    try:
        repo = get_repo()
        repo_path = _get_repo_path()
        active_branch = _current_branch_name(repo)
        _ensure_branch(repo, MAIN_BRANCH)

        rel_path = _document_rel_path(project, version, section, slug, product=product)
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


def deploy_to_gh_pages(repo_path: Path, remote_url: str) -> bool | str:
    """Build the docs site with zensical and push the HTML to the gh-pages branch.

    Supports two modes:
    - **Single site** (no product directories): builds once → site/
    - **Multi-product** (docs/{product}/{project}/…): builds each product
      into site/{product}/ and generates a root product-picker index.html.

    Steps:
      1. Detect whether multi-product layout is in use
      2. Build each product (or the single site) with zensical
      3. Clone (or init) an isolated gh-pages worktree into a temp dir
      4. Replace its contents with the combined site output
      5. Commit and force-push to gh-pages on the remote
    """
    # Always build from the main branch so doc commits are included.
    try:
        _build_repo = git.Repo(repo_path)
        if MAIN_BRANCH in [b.name for b in _build_repo.branches]:
            _build_repo.heads[MAIN_BRANCH].checkout()
        elif _build_repo.branches:
            _build_repo.active_branch.rename(MAIN_BRANCH)
            _build_repo.heads[MAIN_BRANCH].checkout()
    except Exception as _br_err:
        logger.warning("Could not checkout %s before build: %s", MAIN_BRANCH, _br_err)

    docs_dir = repo_path / "docs"
    skip_dirs = {"assets", "static", "images", "img", "css", "js", "fonts", "stylesheets"}

    # Detect multi-product layout:
    # A product directory contains sub-project directories (which contain .md files),
    # rather than having .md files directly.
    product_dirs = _detect_product_dirs(docs_dir, skip_dirs)

    if product_dirs:
        return _deploy_multi_product(repo_path, remote_url, product_dirs)
    else:
        return _deploy_single_site(repo_path, remote_url)


def _detect_product_dirs(docs_dir: Path, skip_dirs: set[str]) -> list[Path]:
    """Detect product directories under docs/.

    A directory is a "product" if it contains subdirectories that themselves
    contain .md files (i.e. it's docs/{product}/{project}/…), NOT if it
    directly contains .md files (that's a project, not a product).
    """
    if not docs_dir.exists():
        return []

    product_dirs: list[Path] = []
    for child in sorted(docs_dir.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name.lower() in skip_dirs:
            continue
        # Check: does this dir have sub-dirs that contain .md files?
        has_subproject_dirs = False
        has_direct_md = any(child.glob("*.md"))
        for subdir in child.iterdir():
            if subdir.is_dir() and not subdir.name.startswith(".") and subdir.name.lower() not in skip_dirs:
                if any(subdir.rglob("*.md")):
                    has_subproject_dirs = True
                    break
        # It's a product dir if it has sub-project dirs but no direct content .md
        # (index.md is OK, but real content pages would mean it's a project)
        direct_content_pages = [
            f for f in child.glob("*.md") if f.name != "index.md"
        ]
        if has_subproject_dirs and not direct_content_pages:
            product_dirs.append(child)

    return product_dirs


def _deploy_single_site(repo_path: Path, remote_url: str) -> bool | str:
    """Build and deploy a single documentation site (no product subdivision)."""
    toml_path = repo_path / "zensical.toml"
    if not toml_path.exists():
        logger.warning("zensical.toml missing at %s — generating now", repo_path)
        try:
            write_zensical_toml(repo_path)
        except Exception as gen_exc:
            return f"zensical.toml not found and could not be generated: {gen_exc}"
        if not toml_path.exists():
            return f"zensical.toml not found at {repo_path} — docs repo may not be initialised"

    build_err = _run_zensical_build(repo_path, "zensical.toml")
    if build_err:
        return build_err

    site_dir = repo_path / "site"
    if not site_dir.exists() or not any(site_dir.iterdir()):
        return "site/ directory is empty after build — nothing to deploy"

    return _push_site_to_gh_pages(site_dir, remote_url)


def _deploy_multi_product(
    repo_path: Path, remote_url: str, product_dirs: list[Path],
) -> bool | str:
    """Build each product as a separate site and combine into site/{product}/."""
    from app.publishing.mkdocs_gen import generate_zensical_toml, _ensure_folder_indexes, _folder_title

    docs_dir = repo_path / "docs"
    combined_site = repo_path / "site"
    if combined_site.exists():
        shutil.rmtree(combined_site)
    combined_site.mkdir(parents=True, exist_ok=True)

    branding = dict(_current_branding) if _current_branding else {}

    for product_path in product_dirs:
        product_slug = product_path.name
        product_label = _folder_title(product_slug)
        logger.info("Building product site: %s (%s)", product_label, product_slug)

        # Create a temporary build directory for this product
        with tempfile.TemporaryDirectory(prefix=f"product-{product_slug}-") as tmp_build:
            tmp_build_path = Path(tmp_build)
            tmp_docs = tmp_build_path / "docs"

            # Copy only this product's docs (sub-projects) into tmp_docs/
            shutil.copytree(str(product_path), str(tmp_docs))

            # Generate indexes and config for this product
            _ensure_folder_indexes(tmp_docs)

            product_branding = dict(branding)
            product_branding["site_name"] = product_label

            toml_content = generate_zensical_toml(
                tmp_docs, **product_branding,
            )
            toml_path = tmp_build_path / "zensical.toml"
            toml_path.write_text(toml_content, encoding="utf-8")

            # Copy custom CSS if it exists
            css_src = docs_dir / "stylesheets"
            if css_src.exists():
                shutil.copytree(str(css_src), str(tmp_docs / "stylesheets"))

            # Build
            build_err = _run_zensical_build(tmp_build_path, "zensical.toml")
            if build_err:
                logger.error("Build failed for product %s: %s", product_slug, build_err)
                continue

            # Copy output to combined site/{product}/
            product_site = tmp_build_path / "site"
            if product_site.exists() and any(product_site.iterdir()):
                dest = combined_site / product_slug
                shutil.copytree(str(product_site), str(dest))
                logger.info("Built product %s → site/%s/", product_label, product_slug)

    # Generate root product picker index.html
    _generate_product_picker_html(combined_site, product_dirs, branding)

    if not any(combined_site.iterdir()):
        return "No product sites were built — nothing to deploy"

    return _push_site_to_gh_pages(combined_site, remote_url)


def _run_zensical_build(build_dir: Path, toml_filename: str) -> str | None:
    """Run zensical.build() in the given directory. Returns error string or None."""
    try:
        result = subprocess.run(
            [
                "python", "-c",
                "import sys, zensical; zensical.build(sys.argv[1], True)",
                toml_filename,
            ],
            cwd=str(build_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            full_err = (result.stderr or result.stdout or "no output")
            logger.error("zensical build failed (rc=%s):\n%s", result.returncode, full_err)
            return f"Zensical build failed (rc={result.returncode}): {full_err[-600:]}"
        logger.info("zensical build output: %s", result.stdout.strip())
        return None
    except Exception as build_exc:
        logger.exception("zensical build raised an exception")
        return f"Zensical build error: {build_exc}"


def _push_site_to_gh_pages(site_dir: Path, remote_url: str) -> bool | str:
    """Push the contents of site_dir to the gh-pages branch on the remote."""
    with tempfile.TemporaryDirectory(prefix="gh-pages-") as tmpdir:
        tmp_path = Path(tmpdir)
        try:
            gh_repo = git.Repo.clone_from(
                remote_url, str(tmp_path), branch="gh-pages", depth=1
            )
            logger.info("Cloned existing gh-pages branch into %s", tmpdir)
            for item in list(tmp_path.iterdir()):
                if item.name == ".git":
                    continue
                shutil.rmtree(item) if item.is_dir() else item.unlink()
        except git.GitCommandError as clone_err:
            err_str = str(clone_err).lower()
            if "authentication" in err_str or "403" in err_str or "401" in err_str or "could not read" in err_str:
                logger.error("GitHub auth failed during clone: %s", clone_err)
                return f"GitHub authentication failed. Please reconnect your GitHub account with a fresh token. ({clone_err})"
            logger.info("gh-pages branch not found remotely — initialising fresh repo")
            gh_repo = git.Repo.init(str(tmp_path))
            gh_repo.create_remote("origin", remote_url)

        # Copy site contents
        for item in site_dir.iterdir():
            dest = tmp_path / item.name
            if item.is_dir():
                shutil.copytree(str(item), str(dest))
            else:
                shutil.copy2(str(item), str(dest))

        (tmp_path / ".nojekyll").touch()

        gh_repo.git.add("--all")
        if not gh_repo.is_dirty(untracked_files=True):
            logger.info("deploy_to_gh_pages: nothing changed in gh-pages")
            return True
        gh_repo.index.commit("Deploy documentation site")
        try:
            gh_repo.remotes.origin.push("HEAD:refs/heads/gh-pages", force=True)
            logger.info("Pushed gh-pages to %s", remote_url.split("@")[-1])
            _configure_pages_source(remote_url)
            return True
        except Exception as push_err:
            logger.exception("Failed to push gh-pages")
            return f"Push to gh-pages failed: {push_err}"


def _generate_product_picker_html(
    site_dir: Path, product_dirs: list[Path], branding: dict,
) -> None:
    """Generate a root index.html that acts as a product picker / landing page."""
    from app.publishing.mkdocs_gen import _folder_title

    site_name = branding.get("site_name", "Documentation")
    primary_color = branding.get("primary_color", "#1a73e8")

    cards_html = ""
    for pdir in product_dirs:
        label = _folder_title(pdir.name)
        slug = pdir.name
        page_count = sum(1 for _ in pdir.rglob("*.md"))
        cards_html += f"""
        <a href="{slug}/" class="card">
            <h2>{label}</h2>
            <p>{page_count} page{"s" if page_count != 1 else ""}</p>
        </a>
"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{site_name}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
               background: #f5f5f5; min-height: 100vh; }}
        .header {{ background: {primary_color}; color: white; padding: 2rem; text-align: center; }}
        .header h1 {{ font-size: 2rem; margin-bottom: 0.5rem; }}
        .header p {{ opacity: 0.9; }}
        .container {{ max-width: 900px; margin: 2rem auto; padding: 0 1rem;
                     display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
                     gap: 1.5rem; }}
        .card {{ background: white; border-radius: 8px; padding: 1.5rem;
                text-decoration: none; color: inherit; box-shadow: 0 1px 3px rgba(0,0,0,0.12);
                transition: box-shadow 0.2s, transform 0.2s; }}
        .card:hover {{ box-shadow: 0 4px 12px rgba(0,0,0,0.15); transform: translateY(-2px); }}
        .card h2 {{ color: {primary_color}; margin-bottom: 0.5rem; font-size: 1.25rem; }}
        .card p {{ color: #666; font-size: 0.9rem; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>{site_name}</h1>
        <p>Select a product to view its documentation</p>
    </div>
    <div class="container">
{cards_html}
    </div>
</body>
</html>"""

    (site_dir / "index.html").write_text(html, encoding="utf-8")
    logger.info("Generated product picker landing page at site/index.html")


def _configure_pages_source(remote_url: str) -> None:
    """Ensure GitHub Pages is configured to serve from the gh-pages branch.

    After the first successful push to gh-pages, the Pages source may still
    point to 'main' (set during repo creation). This calls the GitHub API to
    update it so the built HTML is actually served.
    """
    try:
        import re
        import requests as _req

        # Extract token and repo from the remote URL
        # Format: https://oauth2:{token}@github.com/{owner}/{repo}.git
        m = re.match(r"https://oauth2:([^@]+)@github\.com/(.+?)(?:\.git)?$", remote_url)
        if not m:
            logger.debug("_configure_pages_source: could not parse remote_url")
            return
        token, full_name = m.group(1), m.group(2)

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        # Check current Pages config
        check = _req.get(f"https://api.github.com/repos/{full_name}/pages",
                         headers=headers, timeout=10)

        if check.status_code == 200:
            current_source = check.json().get("source", {})
            if current_source.get("branch") == "gh-pages":
                logger.debug("GitHub Pages already configured for gh-pages")
                return
            # Update existing Pages configuration
            resp = _req.put(
                f"https://api.github.com/repos/{full_name}/pages",
                headers=headers,
                json={"source": {"branch": "gh-pages", "path": "/"}},
                timeout=10,
            )
            if resp.status_code in (200, 204):
                logger.info("Updated GitHub Pages source to gh-pages for %s", full_name)
            else:
                logger.warning("Failed to update Pages source (status %s): %s",
                               resp.status_code, resp.text[:300])
        elif check.status_code == 404:
            # Pages not enabled yet — create it
            resp = _req.post(
                f"https://api.github.com/repos/{full_name}/pages",
                headers=headers,
                json={"source": {"branch": "gh-pages", "path": "/"}},
                timeout=10,
            )
            if resp.status_code in (200, 201):
                logger.info("Enabled GitHub Pages from gh-pages for %s", full_name)
            else:
                logger.warning("Failed to enable Pages (status %s): %s",
                               resp.status_code, resp.text[:300])
        else:
            logger.debug("Could not check Pages config (status %s)", check.status_code)
    except Exception:
        logger.exception("_configure_pages_source failed (non-fatal)")


def remove_stale_product_dir(old_product_slug: str) -> None:
    """Remove a stale product directory from the main branch.

    Called when a parent project's name-based slug ("adoc") differs from its
    stored slug ("new-project") so the old directory is cleaned up on first republish.
    """
    active_branch: str | None = None
    try:
        repo = get_repo()
        repo_path = _get_repo_path()
        active_branch = _current_branch_name(repo)
        _ensure_branch(repo, MAIN_BRANCH)

        old_dir = repo_path / "docs" / _safe_path(old_product_slug)
        if not old_dir.exists():
            return

        shutil.rmtree(old_dir)
        # Stage the deletions
        try:
            repo.git.rm("-r", "--cached", "--ignore-unmatch",
                        f"docs/{_safe_path(old_product_slug)}")
        except Exception:
            pass
        repo.git.add("-A")
        if repo.is_dirty(untracked_files=True):
            repo.index.commit(f"Remove stale product dir: {old_product_slug}")
            logger.info("Removed stale product dir docs/%s from main", _safe_path(old_product_slug))
    except Exception:
        logger.exception("Failed to remove stale product dir: %s", old_product_slug)
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


def _document_rel_path(
    project: str, version: str, section: str | None, slug: str,
    *, product: str | None = None,
) -> str:
    rel_parts = ["docs"]
    if product:
        rel_parts.append(_safe_path(product))
    rel_parts.append(_safe_path(project))
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
    """Always leave the repo on main after a publish operation.

    The old "restore to original branch" logic caused the repo to end up on
    'master' (git's legacy default) after publishing to 'main', making the
    subsequent Zensical build see only the seed commit instead of doc commits.
    """
    if repo is None:
        return
    try:
        if MAIN_BRANCH in [b.name for b in repo.branches]:
            repo.heads[MAIN_BRANCH].checkout()
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
        index_md.write_text("# Documentation\n\nWelcome to the documentation.\n")
    cfg_path = write_zensical_toml(repo_path, **_current_branding)
    repo.index.add(["docs/index.md", str(cfg_path.relative_to(repo_path))])
    repo.index.commit("Initial docs structure")
    # Ensure the branch is named "main", not "master" (git default varies by version)
    try:
        if repo.active_branch.name != MAIN_BRANCH:
            repo.active_branch.rename(MAIN_BRANCH)
    except Exception:
        pass
