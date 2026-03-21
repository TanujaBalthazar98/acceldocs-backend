"""Extract brand properties (colors, logo, fonts) from a website URL."""

import re
import logging
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.routes import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()

_TIMEOUT = 12.0
_MAX_BYTES = 2_000_000  # 2 MB cap on HTML


class BrandExtractRequest(BaseModel):
    url: str


class BrandExtractResponse(BaseModel):
    logo_url: str | None = None
    favicon_url: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    accent_color: str | None = None
    font_heading: str | None = None
    font_body: str | None = None
    tagline: str | None = None
    name: str | None = None


def _normalise_url(raw: str) -> str:
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_RGB_RE = re.compile(r"rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)")
_GOOGLE_FONT_RE = re.compile(r"fonts\.googleapis\.com/css2?\?family=([^&\"']+)")
_FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*['\"]?([^;'\"}{]+)")

# Colours to ignore when extracting primary brand colour
_BORING_COLORS = {
    "#000000", "#000", "#111111", "#111", "#222222", "#222",
    "#333333", "#333", "#444444", "#444", "#555555", "#555",
    "#666666", "#666", "#777777", "#777", "#888888", "#888",
    "#999999", "#999", "#aaaaaa", "#aaa", "#bbbbbb", "#bbb",
    "#cccccc", "#ccc", "#dddddd", "#ddd", "#eeeeee", "#eee",
    "#ffffff", "#fff", "#f5f5f5", "#f0f0f0", "#fafafa", "#e5e5e5",
    "#f8f8f8", "#e0e0e0", "#d0d0d0", "#f9fafb", "#f3f4f6",
    "#e5e7eb", "#d1d5db", "#9ca3af", "#6b7280", "#4b5563",
    "#374151", "#1f2937", "#111827", "#030712",
}
_SYSTEM_FONTS = {
    "arial", "helvetica", "verdana", "tahoma", "georgia", "times",
    "times new roman", "courier", "courier new", "sans-serif", "serif",
    "monospace", "system-ui", "-apple-system", "blinkmacsystemfont",
    "segoe ui", "inherit", "initial", "unset",
}


def _expand_hex(h: str) -> str:
    """Expand 3-char hex to 6-char. Lowercase."""
    h = h.lower()
    if len(h) == 4:
        return "#" + h[1]*2 + h[2]*2 + h[3]*2
    return h


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def _extract_colors(html: str) -> list[str]:
    """Return list of unique non-boring hex colours found in the page."""
    found: list[str] = []
    seen: set[str] = set()
    for m in _HEX_RE.finditer(html):
        c = _expand_hex(m.group())
        if c not in seen and c not in _BORING_COLORS:
            seen.add(c)
            found.append(c)
    for m in _RGB_RE.finditer(html):
        c = _rgb_to_hex(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if c not in seen and c not in _BORING_COLORS:
            seen.add(c)
            found.append(c)
    return found


def _extract_fonts(html: str) -> list[str]:
    """Return list of unique non-system font names from the page."""
    fonts: list[str] = []
    seen: set[str] = set()

    # Google Fonts links
    for m in _GOOGLE_FONT_RE.finditer(html):
        raw = m.group(1).replace("+", " ")
        for part in raw.split("|"):
            name = part.split(":")[0].strip()
            key = name.lower()
            if key not in seen and key not in _SYSTEM_FONTS:
                seen.add(key)
                fonts.append(name)

    # inline font-family declarations
    for m in _FONT_FAMILY_RE.finditer(html):
        for raw_name in m.group(1).split(","):
            name = raw_name.strip().strip("'\"")
            key = name.lower()
            if key not in seen and key not in _SYSTEM_FONTS and len(name) > 1:
                seen.add(key)
                fonts.append(name)

    return fonts


def _extract_logo(soup: BeautifulSoup, base_url: str) -> str | None:
    """Try to find a logo image."""
    # <link rel="icon" or rel="apple-touch-icon">
    for selector in [
        {"rel": "apple-touch-icon"},
        {"rel": "icon"},
    ]:
        tag = soup.find("link", attrs=selector)
        if tag and tag.get("href"):
            return urljoin(base_url, tag["href"])

    # Look for <img> with "logo" in class, id, alt, or src
    for img in soup.find_all("img", limit=50):
        attrs_str = " ".join([
            img.get("class", [""])[0] if isinstance(img.get("class"), list) else str(img.get("class", "")),
            str(img.get("id", "")),
            str(img.get("alt", "")),
            str(img.get("src", "")),
        ]).lower()
        if "logo" in attrs_str:
            src = img.get("src")
            if src:
                return urljoin(base_url, src)

    # SVG with logo in class/id
    for svg in soup.find_all("svg", limit=20):
        attrs_str = " ".join([
            str(svg.get("class", "")),
            str(svg.get("id", "")),
        ]).lower()
        if "logo" in attrs_str:
            # Can't easily extract SVG as URL
            pass

    return None


def _extract_favicon(soup: BeautifulSoup, base_url: str) -> str | None:
    for rel in ["icon", "shortcut icon"]:
        tag = soup.find("link", attrs={"rel": rel})
        if tag and tag.get("href"):
            return urljoin(base_url, tag["href"])
    return urljoin(base_url, "/favicon.ico")


def _extract_meta(soup: BeautifulSoup) -> tuple[str | None, str | None]:
    """Extract site name and description/tagline."""
    name = None
    tagline = None

    # og:site_name
    og = soup.find("meta", attrs={"property": "og:site_name"})
    if og and og.get("content"):
        name = og["content"].strip()

    # <title>
    if not name:
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            # Often "Page | Site Name" — take last part
            parts = title_tag.string.strip().split("|")
            if len(parts) > 1:
                name = parts[-1].strip()
            else:
                parts = title_tag.string.strip().split(" - ")
                if len(parts) > 1:
                    name = parts[-1].strip()
                else:
                    name = title_tag.string.strip()

    # description
    desc = soup.find("meta", attrs={"name": "description"})
    if desc and desc.get("content"):
        tagline = desc["content"].strip()
        if len(tagline) > 120:
            tagline = tagline[:117] + "..."

    return name, tagline


# -- Theme color from meta --
def _extract_theme_color(soup: BeautifulSoup) -> str | None:
    tag = soup.find("meta", attrs={"name": "theme-color"})
    if tag and tag.get("content"):
        c = tag["content"].strip()
        if _HEX_RE.match(c):
            return _expand_hex(c)
    return None


@router.post("/brand-extract", response_model=BrandExtractResponse)
async def extract_brand(body: BrandExtractRequest, _user=Depends(get_current_user)):
    url = _normalise_url(body.url)
    parsed = urlparse(url)
    if not parsed.hostname:
        raise HTTPException(400, "Invalid URL")

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=_TIMEOUT,
            headers={"User-Agent": "AccelDocs-BrandExtractor/1.0"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"Website returned {exc.response.status_code}")
    except Exception as exc:
        raise HTTPException(502, f"Could not fetch website: {exc}")

    html = resp.text[:_MAX_BYTES]
    soup = BeautifulSoup(html, "html.parser")
    base_url = f"{parsed.scheme}://{parsed.hostname}"

    colors = _extract_colors(html)
    fonts = _extract_fonts(html)
    logo_url = _extract_logo(soup, base_url)
    favicon_url = _extract_favicon(soup, base_url)
    name, tagline = _extract_meta(soup)
    theme_color = _extract_theme_color(soup)

    # Determine primary/secondary/accent
    primary = theme_color or (colors[0] if len(colors) >= 1 else None)
    secondary = colors[1] if len(colors) >= 2 else None
    accent = colors[2] if len(colors) >= 3 else None

    # If theme_color was found and it's also in the list, shift
    if theme_color and colors and _expand_hex(colors[0]) == theme_color:
        secondary = colors[1] if len(colors) >= 2 else secondary
        accent = colors[2] if len(colors) >= 3 else accent

    return BrandExtractResponse(
        logo_url=logo_url,
        favicon_url=favicon_url,
        primary_color=primary,
        secondary_color=secondary,
        accent_color=accent,
        font_heading=fonts[0] if len(fonts) >= 1 else None,
        font_body=fonts[1] if len(fonts) >= 2 else (fonts[0] if fonts else None),
        tagline=tagline,
        name=name,
    )
