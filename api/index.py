"""Vercel serverless entry point.

Wraps the FastAPI app with a raw ASGI CORS handler so preflight OPTIONS
requests always succeed, even when inner middleware or route handlers crash.
"""

import json
import traceback

# Attempt to load the real app — capture any startup crash
_fastapi_app = None
_startup_error = None
_ALLOWED_ORIGINS = set()
_CORS_METHODS = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
_CORS_HEADERS = "Authorization, Content-Type, X-Org-Id, X-Requested-With, Accept"

try:
    from app.config import settings
    from app.main import app as _fastapi_app

    _ALLOWED_ORIGINS = set(settings.allowed_origins_list)
except Exception:
    _startup_error = traceback.format_exc()


async def app(scope, receive, send):
    """ASGI wrapper that injects CORS headers on every response."""

    if scope["type"] != "http":
        if _fastapi_app is not None:
            await _fastapi_app(scope, receive, send)
        return

    # Extract Origin header from the request
    origin = None
    for header_name, header_value in scope.get("headers", []):
        if header_name == b"origin":
            origin = header_value.decode("latin-1")
            break

    allowed_origin = origin if origin in _ALLOWED_ORIGINS else None

    # Handle OPTIONS preflight immediately — don't even enter FastAPI
    if scope["method"] == "OPTIONS":
        cors_origin = (allowed_origin or origin or "*").encode()
        await send({
            "type": "http.response.start",
            "status": 204,
            "headers": [
                (b"access-control-allow-origin", cors_origin),
                (b"access-control-allow-methods", _CORS_METHODS.encode()),
                (b"access-control-allow-headers", _CORS_HEADERS.encode()),
                (b"access-control-allow-credentials", b"true"),
                (b"access-control-max-age", b"86400"),
                (b"content-length", b"0"),
            ],
        })
        await send({"type": "http.response.body", "body": b""})
        return

    # If the app failed to start, return a diagnostic JSON response
    if _fastapi_app is None:
        body = json.dumps({
            "error": "App failed to start",
            "detail": _startup_error or "Unknown error",
        }).encode()
        resp_headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        if origin:
            resp_headers.extend([
                (b"access-control-allow-origin", origin.encode()),
                (b"access-control-allow-credentials", b"true"),
            ])
        await send({
            "type": "http.response.start",
            "status": 500,
            "headers": resp_headers,
        })
        await send({"type": "http.response.body", "body": body})
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
