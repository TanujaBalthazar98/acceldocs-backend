"""Utilities for normalizing imported markdown before rendering/storage."""

from __future__ import annotations

import re

_YAML_FRONT_RE = re.compile(r"^\s*---\s*\n[\s\S]*?\n---\s*\n?")
_TOML_FRONT_RE = re.compile(r"^\s*\+\+\+\s*\n[\s\S]*?\n\+\+\+\s*\n?")
_META_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*.*$")
_META_BARE_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*$")

# Frontmatter keys commonly seen in imported docs platforms (DeveloperHub/Docusaurus)
_FM_KEYS = {
    "type",
    "title",
    "listed",
    "slug",
    "description",
    "index_title",
    "hidden",
    "keywords",
    "tags",
    "published",
    "weight",
    "draft",
    "layout",
    "permalink",
    "categories",
    "author",
    "order",
    "sidebar_position",
    "sidebar_label",
    "page_title",
    "nav_title",
}

_CALLOUT_KIND_MAP = {
    "note": "note",
    "info": "info",
    "tip": "tip",
    "warning": "warning",
    "danger": "danger",
    "caution": "warning",
    "success": "success",
}

_UNICODE_BULLET_RE = re.compile(r"^(\s*)[•●▪◦]\s+")


def strip_import_frontmatter(text: str) -> str:
    """Strip frontmatter/meta blocks from imported markdown body content."""
    if not text:
        return text

    # Standard fenced frontmatter
    text = _YAML_FRONT_RE.sub("", text)
    text = _TOML_FRONT_RE.sub("", text)

    lines = text.splitlines()
    if not lines:
        return text

    # Remove loose key:value preamble near top.
    # Supports documents where frontmatter was exported without --- fences.
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1

    j = i
    meta_count = 0
    while j < len(lines):
        stripped = lines[j].strip()
        if not stripped:
            if meta_count > 0:
                j += 1
                break
            j += 1
            i = j
            continue

        # Allow headings before leaked metadata block
        if stripped.startswith("#") and meta_count == 0:
            j += 1
            i = j
            continue

        match = _META_LINE_RE.match(stripped)
        if not match:
            bare = _META_BARE_KEY_RE.match(stripped)
            if bare:
                bare_key = bare.group(1).lower()
                if bare_key in _FM_KEYS and meta_count > 0:
                    meta_count += 1
                    j += 1
                    continue
            # Handle collapsed marker like ---published
            if meta_count > 0 and stripped.startswith("---"):
                meta_count += 1
                j += 1
                continue
            break

        key = match.group(1).lower()
        if key in _FM_KEYS:
            meta_count += 1
            j += 1
            continue

        # Unknown key:value lines are accepted once a metadata block has begun
        if meta_count > 0:
            meta_count += 1
            j += 1
            continue
        break

    if meta_count >= 3:
        lines = lines[:i] + lines[j:]

    # Remove any remaining ---published-like top markers
    while lines and re.match(r"^\s*---\s*[A-Za-z0-9_-]*\s*$", lines[0]):
        lines.pop(0)
    # Remove lone frontmatter status markers leaked as first body line
    while lines and lines[0].strip().lower() in {"published", "draft"}:
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)

    return "\n".join(lines).strip() + ("\n" if lines else "")


def normalize_import_callouts(text: str) -> str:
    """Convert imported callout syntax into Python-Markdown admonitions."""
    if not text:
        return text

    lines = text.splitlines()
    out: list[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Docusaurus syntax: :::info Optional Title
        m = re.match(r"^:::\s*([A-Za-z]+)\s*(.*)$", stripped)
        if m:
            raw_kind = m.group(1).lower()
            title = m.group(2).strip()
            kind = _CALLOUT_KIND_MAP.get(raw_kind)
            if kind:
                i += 1
                block: list[str] = []
                while i < len(lines) and lines[i].strip() != ":::":  # closing fence
                    block.append(lines[i].rstrip())
                    i += 1
                if i < len(lines) and lines[i].strip() == ":::":
                    i += 1

                out.append(f'!!! {kind} "{title}"' if title else f"!!! {kind}")
                for b in block:
                    out.append(f"    {b}" if b else "")
                out.append("")
                continue

        # GitHub alerts:
        # > [!NOTE]
        # > text
        a = re.match(r"^>\s*\[!([A-Za-z]+)\]\s*(.*)$", stripped)
        if a:
            raw_kind = a.group(1).lower()
            first_line = a.group(2).strip()
            kind = _CALLOUT_KIND_MAP.get(raw_kind)
            if kind:
                block: list[str] = []
                if first_line:
                    block.append(first_line)
                i += 1
                while i < len(lines):
                    nxt = lines[i].strip()
                    if not nxt.startswith(">"):
                        break
                    block.append(re.sub(r"^>\s?", "", lines[i]).rstrip())
                    i += 1
                out.append(f"!!! {kind}")
                for b in block:
                    out.append(f"    {b}" if b else "")
                out.append("")
                continue

        out.append(line)
        i += 1

    return "\n".join(out).rstrip() + "\n"


def normalize_imported_markdown(text: str) -> str:
    """Apply all normalization steps for imported markdown content."""
    cleaned = strip_import_frontmatter(text or "")
    # Normalize unicode bullets emitted by some HTML->Markdown conversions.
    cleaned = "\n".join(_UNICODE_BULLET_RE.sub(r"\1- ", ln) for ln in cleaned.splitlines())
    cleaned = normalize_import_callouts(cleaned)
    return cleaned.strip() + ("\n" if cleaned.strip() else "")


_SYNC_LEAK_MARKERS = (
    "type:",
    "listed:",
    "slug:",
    "description:",
    "index_title:",
    "keywords:",
    "tags:",
    "published",
    "---published",
)


def _should_rehydrate_synced_html(content_html: str) -> bool:
    if not content_html:
        return False
    text = re.sub(r"<[^>]+>", "\n", content_html)
    lines = [ln.strip().lower() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False

    # Focus on top-of-document metadata leaks.
    head = lines[:40]

    # Common broken case: lone "published" marker at the top.
    if any(ln in {"published", "---published"} for ln in head[:8]):
        return True

    # Detect multiple key:value metadata lines near the top.
    kv_hits = 0
    for ln in head:
        if re.match(r"^[a-z_][a-z0-9_-]*\s*:\s*", ln):
            key = ln.split(":", 1)[0].strip()
            if key in _FM_KEYS:
                kv_hits += 1
    if kv_hits >= 2:
        return True

    marker_hits = sum(1 for marker in _SYNC_LEAK_MARKERS if marker in "\n".join(head))
    return marker_hits >= 3


def normalize_synced_html(content_html: str) -> str:
    """Best-effort cleanup for legacy synced HTML with leaked markdown metadata."""
    if not _should_rehydrate_synced_html(content_html):
        return content_html

    try:
        import markdown as _md
        from app.conversion.html_to_md import convert_html_to_markdown

        md_content = convert_html_to_markdown(
            content_html or "",
            strip_front=True,
            download_images=False,
        )
        cleaned_md = normalize_imported_markdown(md_content)
        if not cleaned_md:
            return content_html

        return _md.markdown(
            cleaned_md,
            extensions=[
                "tables",
                "fenced_code",
                "codehilite",
                "toc",
                "nl2br",
                "sane_lists",
                "admonition",
                "attr_list",
            ],
        )
    except Exception:
        return content_html
