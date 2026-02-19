"""Markdown processing — ported from src/lib/markdown.ts.

Frontmatter stripping, heading detection, markdown rendering.
"""

import re

import markdown as md

_MD_EXTENSIONS = ["tables", "fenced_code", "codehilite", "toc", "nl2br"]

# Heading pattern: # to ###### followed by a space and text
_HEADING_RE = re.compile(r"^#{1,6}\s+\S", re.M)
# List item: leading whitespace then -, *, +, or digit followed by . and space
_LIST_RE = re.compile(r"^\s*([-*+]|\d+\.)\s+\S", re.M)
# Fenced code block
_CODE_FENCE_RE = re.compile(r"^```", re.M)
# Markdown table (header row + separator row)
_TABLE_RE = re.compile(r"\|.+\|")
_TABLE_SEP_RE = re.compile(r"\|[-: ]+\|")
# HTML tag
_HTML_TAG_RE = re.compile(r"</?[a-z][\s\S]*>", re.I)


def is_likely_markdown(content: str | None) -> bool:
    """Heuristic check: does the content look like Markdown rather than HTML?"""
    if not content:
        return False
    if _HTML_TAG_RE.search(content):
        return False
    if _HEADING_RE.search(content):
        return True
    if _LIST_RE.search(content):
        return True
    if _CODE_FENCE_RE.search(content):
        return True
    if _TABLE_RE.search(content) and _TABLE_SEP_RE.search(content):
        return True
    return False


_YAML_FRONT_RE = re.compile(r"^\s*---\s*\n[\s\S]*?\n---\s*\n?")
_TOML_FRONT_RE = re.compile(r"^\s*\+\+\+\s*\n[\s\S]*?\n\+\+\+\s*\n?")


def strip_frontmatter(content: str) -> str:
    """Strip YAML (---) or TOML (+++) frontmatter blocks."""
    if not content:
        return content
    content = _YAML_FRONT_RE.sub("", content)
    content = _TOML_FRONT_RE.sub("", content)
    return content


_FIRST_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*(\r?\n|$)")


def strip_first_heading(markdown_text: str, title: str) -> str:
    """Remove the first markdown heading if it matches the given title."""
    if not markdown_text or not title:
        return markdown_text
    normalized_title = " ".join(title.strip().lower().split())
    m = _FIRST_HEADING_RE.match(markdown_text)
    if not m:
        return markdown_text
    heading_text = " ".join(m.group(1).strip().lower().split())
    if heading_text == normalized_title:
        return markdown_text[m.end() :]
    return markdown_text


def render_markdown(markdown_text: str) -> str:
    """Render Markdown to HTML using Python-Markdown."""
    return md.markdown(markdown_text, extensions=_MD_EXTENSIONS).strip()
