"""Metadata extraction from Google Docs content.

Reads frontmatter from the document's first paragraph block.
Supports both raw YAML frontmatter and HTML-rendered frontmatter
(Google Docs renders --- as <p>---</p>).
"""

import re

import yaml

# Raw YAML frontmatter: ---\nkey: value\n---
_YAML_BLOCK_RE = re.compile(r"^\s*---\s*\n([\s\S]*?)\n---\s*\n?")

# HTML-rendered frontmatter: <p>---</p><p>key: value</p>...<p>---</p>
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_P_CONTENT_RE = re.compile(r"<p[^>]*>([\s\S]*?)</p>", re.I)


def extract_frontmatter(content: str) -> dict[str, str]:
    """Extract frontmatter metadata from document content.

    Tries raw YAML first, then HTML-rendered frontmatter.
    Returns dict of key-value pairs (all string values).
    """
    if not content:
        return {}

    # Try raw YAML frontmatter
    result = _try_yaml_frontmatter(content)
    if result:
        return result

    # Try HTML-rendered frontmatter
    result = _try_html_frontmatter(content)
    if result:
        return result

    return {}


def _try_yaml_frontmatter(content: str) -> dict[str, str] | None:
    """Parse raw YAML frontmatter (--- delimited)."""
    # Strip HTML tags first to get plain text
    plain = _HTML_TAG_RE.sub("\n", content).strip()
    m = _YAML_BLOCK_RE.match(plain)
    if not m:
        return None

    try:
        data = yaml.safe_load(m.group(1))
        if isinstance(data, dict):
            return {k: str(v) for k, v in data.items() if v is not None}
    except yaml.YAMLError:
        pass
    return None


def _try_html_frontmatter(html: str) -> dict[str, str] | None:
    """Parse frontmatter rendered as HTML paragraphs by Google Docs.

    Looks for <p>---</p> ... <p>key: value</p> ... <p>---</p>.
    """
    paragraphs = _P_CONTENT_RE.findall(html)
    if not paragraphs:
        return None

    # Find opening ---
    start_idx = -1
    for i, p_html in enumerate(paragraphs[:5]):
        text = _HTML_TAG_RE.sub("", p_html).strip()
        if text.startswith("---"):
            start_idx = i
            break

    if start_idx < 0:
        return None

    # Find closing ---
    end_idx = -1
    for i in range(start_idx + 1, min(start_idx + 20, len(paragraphs))):
        text = _HTML_TAG_RE.sub("", paragraphs[i]).strip()
        if text.startswith("---"):
            end_idx = i
            break

    if end_idx < 0:
        return None

    # Extract key-value pairs from paragraphs between delimiters
    yaml_lines = []
    for i in range(start_idx + 1, end_idx):
        text = _HTML_TAG_RE.sub("", paragraphs[i]).strip()
        if text:
            yaml_lines.append(text)

    if not yaml_lines:
        return None

    yaml_text = "\n".join(yaml_lines)
    try:
        data = yaml.safe_load(yaml_text)
        if isinstance(data, dict):
            return {k: str(v) for k, v in data.items() if v is not None}
    except yaml.YAMLError:
        pass

    return None
