"""Security middleware — headers, CSRF checks, and optional rate limiting."""

import logging
from urllib.parse import urlparse

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Security headers middleware
# ---------------------------------------------------------------------------

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers to every response."""

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)

        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )

        proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if proto == "https" or request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        return response


# ---------------------------------------------------------------------------
# Rate limiting setup (slowapi, optional)
# ---------------------------------------------------------------------------

try:
    from slowapi import Limiter
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware
    from slowapi.util import get_remote_address

    SLOWAPI_AVAILABLE = True
except ModuleNotFoundError:
    SLOWAPI_AVAILABLE = False

    class RateLimitExceeded(Exception):
        """Fallback exception type used when slowapi is not installed."""

    class SlowAPIMiddleware:  # pragma: no cover - runtime fallback placeholder
        """Placeholder type so imports from main.py keep working."""

    def get_remote_address(request: Request) -> str:
        return request.client.host if request.client else "unknown"


class _NoOpLimiter:
    """No-op limiter so decorators remain valid even without slowapi."""

    enabled = False

    def limit(self, _rule: str):
        def decorator(func):
            return func

        return decorator


def _get_client_ip(request: Request) -> str:
    """Extract client IP, preferring X-Forwarded-For for proxied environments."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


RATE_LIMITING_ACTIVE = bool(settings.rate_limit_enabled and SLOWAPI_AVAILABLE)
if RATE_LIMITING_ACTIVE:
    try:
        limiter = Limiter(
            key_func=_get_client_ip,
            default_limits=[settings.rate_limit_default],
            storage_uri=settings.rate_limit_storage_uri,
        )
    except Exception as exc:  # pragma: no cover - defensive runtime fallback
        logger.error("Rate limiter init failed, disabling rate limiting: %s", exc)
        limiter = _NoOpLimiter()
        RATE_LIMITING_ACTIVE = False
else:
    limiter = _NoOpLimiter()
    if not SLOWAPI_AVAILABLE:
        logger.warning("slowapi not installed: rate limiting disabled.")
    elif not settings.rate_limit_enabled:
        logger.info("Rate limiting disabled by configuration.")


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Return a clean JSON response when rate limit is hit."""
    logger.warning(
        "Rate limit exceeded: %s %s from %s",
        request.method,
        request.url.path,
        _get_client_ip(request),
    )
    retry_after = "60 seconds"
    detail = getattr(exc, "detail", None)
    if detail:
        retry_after = str(detail).split("per ")[-1]
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Too many requests. Please try again later.",
            "retry_after": retry_after,
        },
    )


# ---------------------------------------------------------------------------
# CSRF protection middleware (Origin / Referer check)
# ---------------------------------------------------------------------------

class CSRFProtectionMiddleware(BaseHTTPMiddleware):
    """Reject state-changing requests whose Origin/Referer host is not allowed."""

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, app, allowed_origins: list[str] | None = None):
        super().__init__(app)
        self._allowed_hosts: set[str] = {"localhost", "127.0.0.1", "::1"}
        for origin in (allowed_origins or []):
            try:
                host = urlparse(origin).hostname
                if host:
                    self._allowed_hosts.add(host.lower())
            except Exception:
                continue

    async def dispatch(self, request: Request, call_next):
        if request.method in self.SAFE_METHODS:
            return await call_next(request)

        # Bearer-token requests are CSRF resistant by design in browser context.
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return await call_next(request)

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        source_host = None
        if origin:
            try:
                source_host = urlparse(origin).hostname
            except Exception:
                pass
        elif referer:
            try:
                source_host = urlparse(referer).hostname
            except Exception:
                pass

        current_host = request.url.hostname.lower() if request.url.hostname else None
        if source_host:
            normalized_source = source_host.lower()
            is_allowed = normalized_source in self._allowed_hosts
            is_same_host = current_host is not None and normalized_source == current_host
            if not is_allowed and not is_same_host:
                logger.warning(
                    "CSRF blocked: %s %s from origin=%s referer=%s",
                    request.method,
                    request.url.path,
                    origin,
                    referer,
                )
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Cross-origin request blocked."},
                )

        return await call_next(request)
