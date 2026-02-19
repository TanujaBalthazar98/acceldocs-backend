"""Content sanitization — ported from DOMPurify config in htmlNormalizer.ts.

Uses bleach with the same allowlisted tags/attributes.
"""

ALLOWED_TAGS: list[str] = [
    "p", "br", "strong", "b", "em", "i", "u", "s", "strike", "del",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li",
    "a", "img",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th",
    "caption", "colgroup", "col",
    "div", "span", "section", "article", "aside",
    "header", "footer", "nav", "main",
    "blockquote", "pre", "code", "kbd", "samp", "var",
    "hr", "sup", "sub", "mark", "small", "abbr", "time", "address",
    "dl", "dt", "dd", "figure", "figcaption", "details", "summary",
]

ALLOWED_ATTRIBUTES: dict[str, list[str]] = {
    "*": ["class", "id", "name", "lang", "dir"],
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title"],
    "td": ["colspan", "rowspan"],
    "th": ["colspan", "rowspan"],
    "time": ["datetime"],
}


def sanitize_html(html: str) -> str:
    """Sanitize HTML using bleach with the project's allowlist."""
    import bleach

    return bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        strip=True,
    )
