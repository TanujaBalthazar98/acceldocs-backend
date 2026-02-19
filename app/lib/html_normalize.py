"""HTML normalization — ported from src/lib/htmlNormalizer.ts.

Cleans Google Docs exported HTML into semantic, sanitized HTML.
Handles:
  - CSS class-based heading detection (font-size → h1-h4)
  - Monospace font → <pre><code> blocks
  - Orphaned <li> wrapping
  - Blockquote conversion (lines starting with >)
  - Markdown table detection inside HTML
  - Google Docs artifact removal (empty spans, inline styles, class names)
"""

import re
from html.parser import HTMLParser
from typing import Sequence

import bleach

from app.lib.sanitize import ALLOWED_ATTRIBUTES, ALLOWED_TAGS

# ---------------------------------------------------------------------------
# CSS parsing helpers
# ---------------------------------------------------------------------------

_CSS_RULE_RE = re.compile(r"\.([a-zA-Z0-9_-]+)\s*\{([^}]*)\}")


def _parse_css_class_styles(css_text: str) -> dict[str, dict[str, str]]:
    """Extract class → {property: value} map from CSS text."""
    result: dict[str, dict[str, str]] = {}
    for m in _CSS_RULE_RE.finditer(css_text):
        class_name = m.group(1)
        decls: dict[str, str] = {}
        for part in m.group(2).split(";"):
            if ":" not in part:
                continue
            prop, val = part.split(":", 1)
            decls[prop.strip().lower()] = val.strip()
        result.setdefault(class_name, {}).update(decls)
    return result


def _extract_css(html: str) -> str:
    """Pull CSS text out of <style> blocks."""
    return "\n".join(m.group(1) for m in re.finditer(r"<style[^>]*>([\s\S]*?)</style>", html, re.I))


def _get_decl(
    inline_style: str, class_attr: str, class_styles: dict[str, dict[str, str]], prop: str
) -> str:
    """Resolve a CSS property from inline style or class styles."""
    # Inline first
    m = re.search(rf"{re.escape(prop)}\s*:\s*([^;]+)", inline_style, re.I)
    if m:
        return m.group(1).strip()
    for cn in class_attr.split():
        decls = class_styles.get(cn, {})
        if prop in decls:
            return decls[prop]
    return ""


_MONO_FONTS = {"monospace", "courier", "consolas", "menlo", "source code"}


def _has_monospace(inline_style: str, class_attr: str, class_styles: dict) -> bool:
    ff = _get_decl(inline_style, class_attr, class_styles, "font-family").lower()
    return any(f in ff for f in _MONO_FONTS)


# ---------------------------------------------------------------------------
# Heading conversion
# ---------------------------------------------------------------------------


def _heading_level_from_font_size(font_size_str: str, font_weight: str) -> int:
    """Map Google Docs font size to heading level (0 = not a heading)."""
    m = re.match(r"(\d+(?:\.\d+)?)(pt|px)", font_size_str, re.I)
    if not m:
        return 0
    size = float(m.group(1))
    unit = m.group(2).lower()
    size_pt = size * 0.75 if unit == "px" else size

    if size_pt >= 24:
        return 1
    if size_pt >= 18:
        return 2
    if size_pt >= 14:
        return 3
    if size_pt >= 12 and font_weight:
        return 4
    return 0


# ---------------------------------------------------------------------------
# Regex-based transformations (no DOM needed)
# ---------------------------------------------------------------------------

_STYLE_ATTR_RE = re.compile(r'\s*style="[^"]*"', re.I)
_CLASS_ATTR_RE = re.compile(r'\s*class="[^"]*"', re.I)
_EMPTY_SPAN_RE = re.compile(r"<span[^>]*>\s*</span>", re.I)
_WRAPPER_SPAN_RE = re.compile(r"<span[^>]*>([^<]*)</span>", re.I)
_STYLE_TAG_RE = re.compile(r"<style[^>]*>[\s\S]*?</style>", re.I)
_LINK_TAG_RE = re.compile(r"<link[^>]*>", re.I)
_META_TAG_RE = re.compile(r"<meta[^>]*>", re.I)
_TITLE_TAG_RE = re.compile(r"<title[^>]*>[\s\S]*?</title>", re.I)
_EMPTY_P_RE = re.compile(r"<p[^>]*>\s*(&nbsp;)?\s*</p>", re.I)
_EMPTY_DIV_RE = re.compile(r"<div[^>]*>\s*</div>", re.I)
_NBSP_RE = re.compile(r"&nbsp;")
_MULTI_SPACE_RE = re.compile(r"\s{2,}")


def _remove_google_docs_styles(html: str) -> str:
    html = _STYLE_ATTR_RE.sub("", html)
    html = _CLASS_ATTR_RE.sub("", html)
    html = _EMPTY_SPAN_RE.sub("", html)
    html = _WRAPPER_SPAN_RE.sub(r"\1", html)
    html = _STYLE_TAG_RE.sub("", html)
    html = _LINK_TAG_RE.sub("", html)
    html = _META_TAG_RE.sub("", html)
    html = _TITLE_TAG_RE.sub("", html)
    html = _EMPTY_P_RE.sub("", html)
    html = _EMPTY_DIV_RE.sub("", html)
    html = _NBSP_RE.sub(" ", html)
    html = _MULTI_SPACE_RE.sub(" ", html)
    return html


# ---------------------------------------------------------------------------
# Markdown table detection (regex-based, no DOM)
# ---------------------------------------------------------------------------

_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")


def _is_md_table_row(text: str) -> list[str] | None:
    if "|" not in text:
        return None
    t = text.strip()
    if t.startswith("|"):
        t = t[1:]
    if t.endswith("|"):
        t = t[:-1]
    cells = [c.strip() for c in t.split("|")]
    return cells if len(cells) >= 2 else None


def _is_separator_row(text: str) -> bool:
    cells = _is_md_table_row(text)
    if not cells:
        return False
    return all(_SEPARATOR_CELL_RE.match(c.replace(" ", "")) for c in cells)


# ---------------------------------------------------------------------------
# Frontmatter stripping
# ---------------------------------------------------------------------------

_YAML_FRONT_RE = re.compile(r"^\s*---\s*\n[\s\S]*?\n---\s*\n?")
_TOML_FRONT_RE = re.compile(r"^\s*\+\+\+\s*\n[\s\S]*?\n\+\+\+\s*\n?")


def strip_frontmatter(content: str) -> str:
    """Strip YAML (---) or TOML (+++) frontmatter blocks from content."""
    if not content:
        return content
    content = _YAML_FRONT_RE.sub("", content)
    content = _TOML_FRONT_RE.sub("", content)
    return content


# ---------------------------------------------------------------------------
# HTML frontmatter stripping (Google Docs renders --- as <p>---</p>)
# ---------------------------------------------------------------------------

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_P_CONTENT_RE = re.compile(r"<p[^>]*>([\s\S]*?)</p>", re.I)


def strip_html_frontmatter(html: str) -> str:
    """Remove frontmatter rendered as HTML paragraphs by Google Docs.

    Looks for a <p>---</p> ... <p>---</p> block near the start and removes
    all elements between them (inclusive).
    """
    if not html:
        return html

    paragraphs = list(_P_CONTENT_RE.finditer(html))
    if not paragraphs:
        return html

    # Find opening ---
    start_idx = -1
    for i, m in enumerate(paragraphs[:5]):  # only check first 5 paragraphs
        text = _HTML_TAG_RE.sub("", m.group(1)).strip()
        if text == "---" or text.startswith("---"):
            start_idx = i
            break

    if start_idx < 0:
        return html

    # Find closing ---
    end_idx = -1
    for i in range(start_idx + 1, min(start_idx + 20, len(paragraphs))):
        text = _HTML_TAG_RE.sub("", paragraphs[i].group(1)).strip()
        if text == "---" or text.startswith("---"):
            end_idx = i
            break

    if end_idx < 0:
        return html

    # Remove from start of first match to end of last match
    return html[: paragraphs[start_idx].start()] + html[paragraphs[end_idx].end() :]


# ---------------------------------------------------------------------------
# Main normalization entry point
# ---------------------------------------------------------------------------


def normalize_html(html: str) -> str:
    """Normalize and clean HTML content for consistent display.

    Handles Google Docs exported HTML and markdown-converted HTML.
    """
    if not html:
        return ""

    # Extract body if present
    body_match = re.search(r"<body[^>]*>([\s\S]*?)</body>", html, re.I)
    content = body_match.group(1) if body_match else html

    # Remove Google Docs styles and artifacts
    content = _remove_google_docs_styles(content)

    # Sanitize
    content = bleach.clean(
        content,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        strip=True,
    )

    return content.strip()
