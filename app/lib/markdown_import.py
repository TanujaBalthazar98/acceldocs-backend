"""Utilities for normalizing imported markdown before rendering/storage."""

from __future__ import annotations

import json
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

_MINTLIFY_TYPE_MAP = {
    "Note": "note",
    "Info": "note",
    "Warning": "warning",
    "Danger": "danger",
    "Error": "danger",
    "Tip": "tip",
    "Check": "tip",
    "Success": "tip",
}

_GITBOOK_TYPE_MAP = {
    "info": "note",
    "warning": "warning",
    "danger": "danger",
    "success": "tip",
}

_EMOJI_TYPE_MAP = {
    "📘": "note", "ℹ️": "note", "💡": "note", "📌": "note",
    "⚠️": "warning", "🚧": "warning", "🔔": "warning",
    "🚨": "danger", "❌": "danger", "🛑": "danger",
    "✅": "tip", "💚": "tip", "🟢": "tip", "👍": "tip",
}


def _convert_mintlify_jsx(text: str) -> str:
    """Convert Mintlify JSX callout components to admonition syntax."""
    result = text
    for tag, admonition_type in _MINTLIFY_TYPE_MAP.items():
        def make_replacer(atype: str):
            def replace_component(m: re.Match) -> str:
                attrs = m.group(1) or ""
                body = (m.group(2) or "").strip()
                title_match = re.search(r'title=["\']([^"\']+)["\']', attrs)
                title = title_match.group(1) if title_match else atype.capitalize()
                if not body:
                    return f'!!! {atype} "{title}"\n\n'
                indented = "\n".join(f"    {line}" for line in body.splitlines())
                return f'!!! {atype} "{title}"\n{indented}\n'
            return replace_component

        pattern = re.compile(rf'<{tag}([^>]*)>(.*?)</{tag}>', re.DOTALL)
        result = pattern.sub(make_replacer(admonition_type), result)

        self_closing = re.compile(rf'<{tag}([^/]*)/>', re.DOTALL)
        result = self_closing.sub(f'!!! {admonition_type} "{admonition_type.capitalize()}"\n    \n', result)

    # <Card> → blockquote
    result = re.sub(
        r'<Card[^>]*title=["\']([^"\']+)["\'][^>]*>(.*?)</Card>',
        lambda m: f'> **{m.group(1)}**\n>\n> {m.group(2).strip()}\n',
        result,
        flags=re.DOTALL,
    )
    result = re.sub(r'<CardGroup[^>]*>(.*?)</CardGroup>', r'\1', result, flags=re.DOTALL)

    # <Steps>/<Step> → numbered list
    def replace_steps(m: re.Match) -> str:
        steps_body = m.group(1)
        step_items = re.findall(r'<Step[^>]*>(.*?)</Step>', steps_body, re.DOTALL)
        if not step_items:
            return steps_body
        lines = [f"{i}. {item.strip()}" for i, item in enumerate(step_items, 1)]
        return "\n".join(lines) + "\n"

    result = re.sub(r'<Steps[^>]*>(.*?)</Steps>', replace_steps, result, flags=re.DOTALL)

    return result


def _convert_gitbook_hints(text: str) -> str:
    """Convert GitBook {% hint style="X" %}...{% endhint %} to admonition syntax."""
    def replace_hint(m: re.Match) -> str:
        style = m.group(1).lower()
        body = m.group(2).strip()
        admonition_type = _GITBOOK_TYPE_MAP.get(style, "note")
        title = admonition_type.capitalize()
        indented = "\n".join(f"    {line}" for line in body.splitlines())
        return f'!!! {admonition_type} "{title}"\n{indented}\n'

    pattern = re.compile(
        r'\{%\s*hint\s+style=["\'](\w+)["\']\s*%\}(.*?)\{%\s*endhint\s*%\}',
        re.DOTALL | re.IGNORECASE,
    )
    return pattern.sub(replace_hint, text)


def _convert_notion_callouts(text: str) -> str:
    """Convert Notion-style emoji blockquotes and ReadMe-style callouts."""
    lines = text.split("\n")
    output = []
    i = 0
    while i < len(lines):
        line = lines[i]
        bq_match = re.match(r'^>\s*([\U00010000-\U0010ffff]|[^\w\s>])\s*(.*)', line)
        if bq_match:
            emoji = bq_match.group(1)
            first_line = bq_match.group(2)
            atype = _EMOJI_TYPE_MAP.get(emoji)
            if atype is not None:
                body_lines = [first_line] if first_line else []
                i += 1
                while i < len(lines) and lines[i].startswith(">"):
                    body_lines.append(lines[i][1:].strip())
                    i += 1
                body = "\n".join(body_lines).strip()
                title = atype.capitalize()
                indented = "\n".join(f"    {bl}" for bl in body.splitlines()) if body else "    "
                output.append(f'!!! {atype} "{title}"\n{indented}')
                continue
        output.append(line)
        i += 1
    return "\n".join(output)


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


def normalize_import_json_callouts(text: str) -> str:
    """Convert ReadMe/DeveloperHub JSON callout blocks to admonitions.

    Example:
      [block:callout]
      { "type": "info", "title": "What's New", "body": "..." }
      [/block]
    """
    if not text:
        return text

    block_re = re.compile(r"\[block:callout\]\s*([\s\S]*?)\s*\[/block\]", re.I)

    def _to_admonition(match: re.Match) -> str:
        raw = match.group(1).strip()
        try:
            payload = json.loads(raw)
        except Exception:
            return match.group(0)

        kind_raw = str(payload.get("type", "info")).strip().lower()
        kind = _CALLOUT_KIND_MAP.get(kind_raw, "info")
        title = str(payload.get("title", "")).strip()
        body = str(payload.get("body", "")).strip()

        header = f'!!! {kind} "{title}"' if title else f"!!! {kind}"
        if not body:
            return f"{header}\n"
        indented = "\n".join(f"    {ln}" for ln in body.splitlines())
        return f"{header}\n{indented}\n"

    return block_re.sub(_to_admonition, text)


_FM_TITLE_RE = re.compile(r"^\s*(?:title|index_title)\s*:\s*(.+?)\s*$", re.I)


def _extract_frontmatter_title(text: str) -> str | None:
    if not text:
        return None
    for ln in text.splitlines()[:50]:
        m = _FM_TITLE_RE.match(ln)
        if m:
            title = m.group(1).strip().strip('"').strip("'")
            if title:
                return title
    return None


def _has_top_heading(text: str) -> bool:
    if not text:
        return False
    for ln in text.splitlines()[:20]:
        s = ln.strip()
        if not s:
            continue
        return s.startswith("#")
    return False


def normalize_imported_markdown(text: str) -> str:
    """Apply all normalization steps for imported markdown content."""
    original = text or ""
    extracted_title = _extract_frontmatter_title(original)
    cleaned = strip_import_frontmatter(original)
    # Normalize unicode bullets emitted by some HTML->Markdown conversions.
    cleaned = "\n".join(_UNICODE_BULLET_RE.sub(r"\1- ", ln) for ln in cleaned.splitlines())
    cleaned = _convert_mintlify_jsx(cleaned)
    cleaned = _convert_gitbook_hints(cleaned)
    cleaned = _convert_notion_callouts(cleaned)
    cleaned = normalize_import_json_callouts(cleaned)
    cleaned = normalize_import_callouts(cleaned)
    cleaned = cleaned.strip()
    if extracted_title and not _has_top_heading(cleaned):
        cleaned = f"# {extracted_title}\n\n{cleaned}" if cleaned else f"# {extracted_title}"
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


def clean_google_docs_html(content_html: str) -> str:
    """Strip Google Docs bloat (inline styles, wrapper tags, custom CSS) from
    exported HTML while preserving semantic structure.

    This produces clean HTML that renders with the site's own stylesheet instead
    of Google's forced Arial/inline-style soup.
    """
    if not content_html:
        return content_html

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        # Fallback: regex-based cleanup if bs4 is not available
        return _regex_clean_google_html(content_html)

    soup = BeautifulSoup(content_html, "html.parser")

    # Remove <style> and <meta> tags entirely
    for tag in soup.find_all(["style", "meta", "link", "title"]):
        tag.decompose()

    # Strip all style attributes and Google-specific classes
    for tag in soup.find_all(True):
        if tag.get("style"):
            del tag["style"]
        # Remove class except for semantically meaningful ones (admonition, callout, etc.)
        classes = tag.get("class", [])
        if classes:
            keep = [c for c in classes if c.startswith(("admonition", "callout", "highlight", "codehilite"))]
            if keep:
                tag["class"] = keep
            else:
                del tag["class"]

    # Unwrap <html>, <head>, <body> wrappers — keep just body content
    body = soup.find("body")
    if body:
        soup = body

    # Remove empty spans that Google Docs inserts everywhere
    for span in soup.find_all("span"):
        # Span with no attributes is just a wrapper — unwrap it
        if not span.attrs:
            span.unwrap()

    # Clean up empty paragraphs (Google inserts <p><span></span></p> as spacers).
    # Keep paragraphs that contain media nodes (e.g. <img>) even if text is empty.
    media_tags = {"img", "picture", "svg", "video", "iframe", "object", "embed", "canvas", "audio"}
    for p in soup.find_all("p"):
        has_media = p.find(media_tags) is not None
        if not has_media and not p.get_text(strip=True):
            p.decompose()

    result = str(soup)
    # Collapse excessive whitespace from decomposed elements
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


_STYLE_TAG_CLEAN_RE = re.compile(r"<style[^>]*>[\s\S]*?</style>", re.I)
_META_TAG_CLEAN_RE = re.compile(r"<meta[^>]*>", re.I)
_LINK_TAG_CLEAN_RE = re.compile(r"<link[^>]*>", re.I)
_TITLE_TAG_CLEAN_RE = re.compile(r"<title[^>]*>[\s\S]*?</title>", re.I)
_STYLE_ATTR_CLEAN_RE = re.compile(r'\s+style="[^"]*"', re.I)
_CLASS_ATTR_CLEAN_RE = re.compile(r'\s+class="(?!admonition|callout|highlight|codehilite)[^"]*"', re.I)
_HTML_WRAPPER_RE = re.compile(r"</?(?:html|head|body)[^>]*>", re.I)


def _regex_clean_google_html(content_html: str) -> str:
    """Regex-only fallback for cleaning Google Docs HTML when bs4 is unavailable."""
    html = content_html
    html = _STYLE_TAG_CLEAN_RE.sub("", html)
    html = _META_TAG_CLEAN_RE.sub("", html)
    html = _LINK_TAG_CLEAN_RE.sub("", html)
    html = _TITLE_TAG_CLEAN_RE.sub("", html)
    html = _STYLE_ATTR_CLEAN_RE.sub("", html)
    html = _CLASS_ATTR_CLEAN_RE.sub("", html)
    html = _HTML_WRAPPER_RE.sub("", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


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
