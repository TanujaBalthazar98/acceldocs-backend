"""HTML → Markdown conversion using Pandoc (preferred) or markdownify.

Handles Google Docs exported HTML with custom converters for:
  - Preserving headings, tables, code blocks, images, links
  - Image extraction and path rewriting
  - Stripping Google Docs artifacts
"""

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import markdownify
import requests

from app.lib.html_normalize import (
    _remove_google_docs_styles,
    strip_frontmatter,
    strip_html_frontmatter,
)

logger = logging.getLogger(__name__)

# Google Docs image URL pattern
_GDOCS_IMAGE_RE = re.compile(
    r'src="(https://lh[0-9]\.googleusercontent\.com/[^"]+)"', re.I
)


class DocsMarkdownConverter(markdownify.MarkdownConverter):
    """Custom markdownify converter tuned for Google Docs HTML."""

    def convert_pre(self, el, text, convert_as_inline):
        """Preserve code blocks with fenced syntax."""
        code = el.find("code")
        if code:
            lang = ""
            class_attr = code.get("class", "")
            if class_attr:
                # Extract language from class like "language-python"
                for cls in class_attr.split():
                    if cls.startswith("language-"):
                        lang = cls[9:]
                        break
            return f"\n```{lang}\n{code.get_text()}\n```\n"
        return f"\n```\n{text}\n```\n"

    def convert_table(self, el, text, convert_as_inline):
        """Let markdownify handle tables naturally."""
        return super().convert_table(el, text, convert_as_inline)


def _convert_with_pandoc(html: str) -> str | None:
    """Convert HTML to markdown via local pandoc binary.

    Returns markdown when successful, otherwise None.
    """
    pandoc_bin = os.getenv("PANDOC_PATH", "pandoc").strip() or "pandoc"
    if shutil.which(pandoc_bin) is None:
        return None

    try:
        result = subprocess.run(
            [
                pandoc_bin,
                "--from=html",
                "--to=gfm+pipe_tables+task_lists+fenced_code_blocks",
                "--wrap=none",
            ],
            input=html,
            text=True,
            capture_output=True,
            check=True,
            timeout=20,
        )
        return (result.stdout or "").strip()
    except Exception:
        logger.exception("Pandoc HTML->Markdown conversion failed")
        return None


def _convert_with_pypandoc(html: str) -> str | None:
    """Convert HTML to markdown via pypandoc (bundled binary-friendly)."""
    try:
        import pypandoc  # type: ignore
    except Exception:
        return None

    try:
        md = pypandoc.convert_text(
            html,
            "gfm+pipe_tables+task_lists+fenced_code_blocks",
            format="html",
            extra_args=["--wrap=none"],
        )
        return (md or "").strip()
    except Exception:
        logger.exception("pypandoc HTML->Markdown conversion failed")
        return None


def _convert_with_markdownify(content: str) -> str:
    """Convert HTML to markdown using markdownify."""
    converter = DocsMarkdownConverter(
        heading_style="atx",
        bullets="-",
        strip=["style", "script", "meta", "link", "title"],
    )
    return converter.convert(content)


def convert_html_to_markdown(
    html: str,
    strip_front: bool = True,
    download_images: bool = False,
    images_dir: Path | None = None,
    image_base_path: str = "assets",
    engine: str | None = None,
) -> str:
    """Convert HTML (typically from Google Docs export) to Markdown.

    Args:
        html: The HTML content to convert.
        strip_front: Strip YAML/HTML frontmatter before conversion.
        download_images: If True, download Google Docs images locally.
        images_dir: Directory to save downloaded images.
        image_base_path: Base path for image references in Markdown.
        engine: Conversion engine: "auto" (default), "pandoc", "markdownify".

    Returns:
        Clean Markdown string.
    """
    if not html:
        return ""

    content = html

    # Strip frontmatter (both raw and HTML-rendered)
    if strip_front:
        content = strip_frontmatter(content)
        content = strip_html_frontmatter(content)

    # Clean Google Docs artifacts
    content = _remove_google_docs_styles(content)

    # Download and rewrite images if requested
    if download_images and images_dir:
        content = _download_and_rewrite_images(content, images_dir, image_base_path)

    requested_engine = (engine or os.getenv("HTML_TO_MD_ENGINE", "auto")).strip().lower()
    if requested_engine not in {"auto", "pandoc", "markdownify"}:
        requested_engine = "auto"

    md = ""
    if requested_engine in {"auto", "pandoc"}:
        md = _convert_with_pypandoc(content) or _convert_with_pandoc(content) or ""
        if not md and requested_engine == "pandoc":
            logger.warning(
                "HTML_TO_MD_ENGINE=pandoc requested, but Pandoc unavailable/failed; falling back to markdownify"
            )

    if not md:
        md = _convert_with_markdownify(content)

    # Clean up excessive whitespace
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = md.strip()

    # Strip any remaining frontmatter in the markdown output.
    # Google Docs sometimes renders YAML frontmatter as body text,
    # producing lines like "type: page title: Version 4.7.0 listed: true ..."
    # or proper --- delimited blocks that survived HTML conversion.
    md = _strip_md_frontmatter(md)

    # Strip leading number prefixes from the first H1 heading.
    # Doc tools often prefix titles with ordering numbers like "5 version 4.7.0"
    # which should display as "Version 4.7.0".
    def _strip_num_prefix(m: re.Match) -> str:
        hashes = m.group(1)  # "# " or "## "
        rest = m.group(2)
        # Capitalize first letter after stripping number
        return hashes + (rest[0].upper() + rest[1:] if rest else rest)

    md = re.sub(r"^(#{1,2}\s+)\d+[-_\s]+(.)", _strip_num_prefix, md, count=1)

    return md


# Frontmatter keys commonly found in documentation tools
_FM_KEYS = {
    "type", "title", "listed", "slug", "description", "index_title",
    "hidden", "keywords", "tags", "weight",
    "draft", "layout", "permalink", "categories", "author", "order",
    "sidebar_position", "sidebar_label", "page_title", "nav_title",
}

# Matches a line like "key: value" or "key:" for known frontmatter keys
_FM_LINE_RE = re.compile(
    r"^(" + "|".join(re.escape(k) for k in _FM_KEYS) + r")\s*:\s*.*$",
    re.IGNORECASE,
)


def _strip_md_frontmatter(md: str) -> str:
    """Strip frontmatter that leaked into markdown as body text.

    Handles three cases:
    1. Proper YAML frontmatter: ---\\n...\\n---
    2. Loose frontmatter at the very start of the document
    3. Loose frontmatter appearing right after the first heading
       (Google Docs renders the doc title as H1, then frontmatter as body text)
    """
    if not md:
        return md

    # Case 1: standard YAML frontmatter (---...---)
    fm_match = re.match(r"^---\s*\n([\s\S]*?\n)---\s*\n?", md)
    if fm_match:
        md = md[fm_match.end():]

    # Case 2 & 3: loose frontmatter — scan ALL lines, removing any that
    # look like frontmatter key:value pairs or concatenated frontmatter.
    # Skip headings and blank lines, only strip matching fm lines.
    lines = md.split("\n")
    keep: list[int] = []  # indices of lines to remove
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Skip markdown headings
        if stripped.startswith("#"):
            continue
        # Concatenated frontmatter on a single line
        # e.g. "type: page title: Version 4.7.0 listed: true slug: ..."
        fm_key_count = sum(1 for k in _FM_KEYS if re.search(rf"\b{k}\s*:", stripped, re.I))
        if fm_key_count >= 2:
            keep.append(i)
            continue
        # Single frontmatter key:value that starts the non-heading content
        if _FM_LINE_RE.match(stripped):
            keep.append(i)
            continue
        # "---published" or similar closing marker
        if stripped.startswith("---") and keep:
            keep.append(i)
            continue
        # Stop scanning once we hit real content (not a heading, blank, or fm)
        break

    if keep:
        result_lines = [lines[i] for i in range(len(lines)) if i not in set(keep)]
        cleaned = "\n".join(result_lines).strip()
        if cleaned:
            md = cleaned

    return md


def _download_and_rewrite_images(
    html: str, images_dir: Path, base_path: str
) -> str:
    """Download Google Docs images and rewrite src attributes to local paths."""
    images_dir.mkdir(parents=True, exist_ok=True)
    counter = 0

    def _replace_image(match: re.Match) -> str:
        nonlocal counter
        url = match.group(1)
        counter += 1

        # Determine extension from URL or default to .png
        parsed = urlparse(url)
        ext = ".png"
        path = parsed.path.lower()
        for candidate in [".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp"]:
            if path.endswith(candidate):
                ext = candidate
                break

        filename = f"image_{counter:03d}{ext}"
        local_path = images_dir / filename

        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            local_path.write_bytes(resp.content)
            logger.debug("Downloaded image: %s → %s", url, local_path)
        except Exception:
            logger.warning("Failed to download image: %s", url)
            return match.group(0)  # Keep original URL on failure

        return f'src="{base_path}/{filename}"'

    return _GDOCS_IMAGE_RE.sub(_replace_image, html)
