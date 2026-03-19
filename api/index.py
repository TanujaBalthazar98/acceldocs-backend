"""Vercel serverless entry point.

Wraps the FastAPI app with a raw ASGI CORS handler so preflight OPTIONS
requests always succeed, even when inner middleware or route handlers crash.
Vercel's serverless Python runtime can sometimes swallow CORS headers on
errors, so this ensures they're always present.
"""

from app.config import settings
from app.main import app as _fastapi_app

_ALLOWED_ORIGINS = set(settings.allowed_origins_list)
_CORS_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
_CORS_HEADERS = "Authorization, Content-Type, X-Org-Id, X-Requested-With, Accept"


async def app(scope, receive, send):
    """ASGI wrapper that injects CORS headers on every response."""
    if scope["type"] != "http":
        await _fastapi_app(scope, receive, send)
        return

    # Extract Origin header from the request
    origin = None
    for header_name, header_value in scope.get("headers", []):
        if header_name == b"origin":
            origin = header_value.decode("latin-1")
            break

    # Check if origin is allowed
    allowed_origin = origin if origin in _ALLOWED_ORIGINS else None

    # Handle OPTIONS preflight immediately — don't even enter FastAPI
    if scope["method"] == "OPTIONS" and allowed_origin:
        await send({
            "type": "http.response.start",
            "status": 204,
            "headers": [
                (b"access-control-allow-origin", allowed_origin.encode()),
                (b"access-control-allow-methods", _CORS_METHODS.encode()),
                (b"access-control-allow-headers", _CORS_HEADERS.encode()),
                (b"access-control-allow-credentials", b"true"),
                (b"access-control-max-age", b"86400"),
                (b"content-length", b"0"),
            ],
        })
        await send({"type": "http.response.body", "body": b""})
        return

    # For non-OPTIONS requests, wrap send to inject CORS headers
    if allowed_origin:
        cors_headers = [
            (b"access-control-allow-origin", allowed_origin.encode()),
            (b"access-control-allow-credentials", b"true"),
        ]

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # Remove any existing CORS origin header to avoid duplicates
                headers = [
                    (k, v) for k, v in headers
                    if k.lower() != b"access-control-allow-origin"
                ]
                headers.extend(cors_headers)
                message = {**message, "headers": headers}
            await send(message)

        await _fastapi_app(scope, receive, send_with_cors)
    else:
        await _fastapi_app(scope, receive, send)
