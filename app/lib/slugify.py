"""Slug generation utility."""

from slugify import slugify as _slugify


def to_slug(text: str) -> str:
    """Generate a URL-safe slug from text."""
    return _slugify(text, lowercase=True, separator="-")
