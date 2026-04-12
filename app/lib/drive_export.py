"""Google Drive export helpers for docs HTML + embedded image assets."""

from __future__ import annotations

import base64
import mimetypes
import re
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import unquote
import zipfile


_IMG_SRC_RE = re.compile(
    r"""(<img\b[^>]*?\bsrc\s*=\s*)(['"])([^'"]+)\2""",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class EmbeddedImageStats:
    """Image extraction/inlining stats for exported HTML."""

    embedded_images: int
    inlined_images: int


def _is_external_or_data_url(src: str) -> bool:
    value = src.strip().lower()
    return (
        value.startswith("http://")
        or value.startswith("https://")
        or value.startswith("//")
        or value.startswith("data:")
    )


def _src_candidates(src: str) -> list[str]:
    cleaned = unquote(src).split("?", 1)[0].split("#", 1)[0].strip().lstrip("/")
    if not cleaned:
        return []

    basename = cleaned.rsplit("/", 1)[-1]
    candidates: list[str] = [cleaned]
    if cleaned.startswith("./"):
        candidates.append(cleaned[2:])
    if not cleaned.startswith("images/"):
        candidates.append(f"images/{basename}")
    candidates.append(basename)

    # Preserve order but dedupe.
    seen: set[str] = set()
    deduped: list[str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    return deduped


def export_html_with_inlined_images(service, file_id: str) -> tuple[str, EmbeddedImageStats]:
    """Export a Google Doc as zipped HTML and inline image assets as data URLs.

    Returns:
        (html, EmbeddedImageStats)
    Raises:
        ValueError: if zip payload is missing index.html
        Exception: passthrough from Google API/export failures
    """
    raw_zip = service.files().export(fileId=file_id, mimeType="application/zip").execute()
    if isinstance(raw_zip, str):
        raw_zip = raw_zip.encode("utf-8")

    with zipfile.ZipFile(BytesIO(raw_zip)) as archive:
        if "index.html" not in archive.namelist():
            raise ValueError("Zip export missing index.html")

        html = archive.read("index.html").decode("utf-8", errors="replace")
        image_map: dict[str, str] = {}
        embedded_images = 0

        for name in archive.namelist():
            lower_name = name.lower()
            if not lower_name.startswith("images/") or lower_name.endswith("/"):
                continue

            image_bytes = archive.read(name)
            embedded_images += 1
            mime, _ = mimetypes.guess_type(name)
            if not mime or not mime.startswith("image/"):
                mime = "image/png"

            data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
            basename = name.rsplit("/", 1)[-1]
            for key in (name, name.lstrip("./"), basename, f"images/{basename}"):
                image_map[key] = data_url

    if not image_map:
        return html, EmbeddedImageStats(embedded_images=0, inlined_images=0)

    inlined_images = 0

    def _replace_src(match: re.Match[str]) -> str:
        nonlocal inlined_images

        prefix, quote, src = match.groups()
        if _is_external_or_data_url(src):
            return match.group(0)

        for candidate in _src_candidates(src):
            data_url = image_map.get(candidate)
            if data_url:
                inlined_images += 1
                return f"{prefix}{quote}{data_url}{quote}"

        return match.group(0)

    rendered = _IMG_SRC_RE.sub(_replace_src, html)
    return rendered, EmbeddedImageStats(
        embedded_images=embedded_images,
        inlined_images=inlined_images,
    )
