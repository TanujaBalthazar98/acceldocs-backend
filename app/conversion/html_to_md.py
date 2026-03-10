"""HTML → Markdown conversion using markdownify.

Handles Google Docs exported HTML with custom converters for:
  - Preserving headings, tables, code blocks, images, links
  - Image extraction and path rewriting
  - Stripping Google Docs artifacts
"""

import logging
import re
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


def convert_html_to_markdown(
    html: str,
    strip_front: bool = True,
    download_images: bool = False,
    images_dir: Path | None = None,
    image_base_path: str = "assets",
) -> str:
    """Convert HTML (typically from Google Docs export) to Markdown.

    Args:
        html: The HTML content to convert.
        strip_front: Strip YAML/HTML frontmatter before conversion.
        download_images: If True, download Google Docs images locally.
        images_dir: Directory to save downloaded images.
        image_base_path: Base path for image references in Markdown.

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

    # Convert to Markdown
    converter = DocsMarkdownConverter(
        heading_style="atx",
        bullets="-",
        strip=["style", "script", "meta", "link", "title"],
    )
    md = converter.convert(content)

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
    md = re.sub(r"^(#{1,2}\s+)\d+[-_\s]+", r"\1", md, count=1)

    return md


# Frontmatter keys commonly found in documentation tools
_FM_KEYS = {
    "type", "title", "listed", "slug", "description", "index_title",
    "hidden", "keywords", "tags", "published", "date", "weight",
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

    Handles two cases:
    1. Proper YAML frontmatter: ---\\n...\\n---
    2. Loose frontmatter: lines of "key: value" at the start (from Google Docs
       rendering frontmatter as regular text without --- delimiters)
    """
    if not md:
        return md

    # Case 1: standard YAML frontmatter (---...---)
    fm_match = re.match(r"^---\s*\n([\s\S]*?\n)---\s*\n?", md)
    if fm_match:
        md = md[fm_match.end():]

    # Case 2: loose frontmatter — key:value lines at the very start
    # Also handles single-line "type: page title: Version ... ---published"
    lines = md.split("\n")
    strip_until = 0
    found_fm = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            # Blank line: if we already found frontmatter, this ends the block
            if found_fm:
                strip_until = i + 1
                break
            continue
        # Also match concatenated frontmatter on a single line
        # e.g. "type: page title: Version 4.7.0 listed: true slug: ..."
        fm_key_count = sum(1 for k in _FM_KEYS if re.search(rf"\b{k}\s*:", stripped, re.I))
        if fm_key_count >= 2:
            strip_until = i + 1
            found_fm = True
            continue
        # Check if line contains a single frontmatter key:value pair
        if _FM_LINE_RE.match(stripped) and not found_fm:
            # Only match single-key lines if they're at the very start
            strip_until = i + 1
            found_fm = True
            continue
        # Check for "---published" or "--- published" (closing frontmatter marker)
        if stripped.startswith("---") and found_fm:
            strip_until = i + 1
            break
        break

    if strip_until > 0:
        remaining = "\n".join(lines[strip_until:]).strip()
        if remaining:
            md = remaining

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
