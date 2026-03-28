"""Slug generation utility."""

from typing import Callable

from slugify import slugify as _slugify


def to_slug(text: str) -> str:
    """Generate a URL-safe slug from text."""
    return _slugify(text, lowercase=True, separator="-")


def unique_slug(base: str, exists_fn: Callable[[str], bool]) -> str:
    """Return a unique slug by appending incrementing numeric suffixes.

    Calls exists_fn(candidate) until it returns False, then returns candidate.
    """
    slug, n = base, 1
    while exists_fn(slug):
        slug, n = f"{base}-{n}", n + 1
    return slug
