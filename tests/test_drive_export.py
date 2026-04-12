from __future__ import annotations

import base64
from io import BytesIO
import zipfile

import pytest

from app.lib.drive_export import export_html_with_inlined_images


def _build_zip(index_html: str, images: dict[str, bytes] | None = None) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", index_html)
        for path, payload in (images or {}).items():
            archive.writestr(path, payload)
    return buffer.getvalue()


class _FakeRequest:
    def __init__(self, payload: bytes):
        self.payload = payload

    def execute(self):
        return self.payload


class _FakeFilesAPI:
    def __init__(self, payload: bytes):
        self.payload = payload

    def export(self, *, fileId: str, mimeType: str):
        assert fileId
        assert mimeType == "application/zip"
        return _FakeRequest(self.payload)


class _FakeDriveService:
    def __init__(self, payload: bytes):
        self._files = _FakeFilesAPI(payload)

    def files(self):
        return self._files


def test_export_html_with_inlined_images_rewrites_relative_sources():
    png_data = b"\x89PNG\r\n\x1a\nfake-png"
    jpg_data = b"\xff\xd8\xff\xe0fake-jpg"
    html = '<p><img src="images/image1.png" alt="one"></p><p><img src="image2.jpg?size=large" alt="two"></p>'
    zip_payload = _build_zip(
        html,
        images={
            "images/image1.png": png_data,
            "images/image2.jpg": jpg_data,
        },
    )
    service = _FakeDriveService(zip_payload)

    rendered, stats = export_html_with_inlined_images(service, "doc-123")

    expected_png = f"data:image/png;base64,{base64.b64encode(png_data).decode('ascii')}"
    expected_jpg = f"data:image/jpeg;base64,{base64.b64encode(jpg_data).decode('ascii')}"
    assert expected_png in rendered
    assert expected_jpg in rendered
    assert "images/image1.png" not in rendered
    assert "image2.jpg?size=large" not in rendered
    assert stats.embedded_images == 2
    assert stats.inlined_images == 2


def test_export_html_with_inlined_images_keeps_external_sources():
    html = '<p><img src="https://lh3.googleusercontent.com/abc123" alt="ext"></p>'
    zip_payload = _build_zip(html)
    service = _FakeDriveService(zip_payload)

    rendered, stats = export_html_with_inlined_images(service, "doc-456")

    assert rendered == html
    assert stats.embedded_images == 0
    assert stats.inlined_images == 0


def test_export_html_with_inlined_images_requires_index_html():
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("images/image.png", b"image")
    service = _FakeDriveService(buffer.getvalue())

    with pytest.raises(ValueError, match="missing index.html"):
        export_html_with_inlined_images(service, "doc-789")
