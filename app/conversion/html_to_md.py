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
