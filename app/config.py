"""Application configuration via environment variables."""

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Only load .env file if it exists (not present on Vercel/production)
_env_file = ".env" if os.path.isfile(".env") else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_file,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = "sqlite:///./acceldocs.db"

    # Google OAuth (interactive/user auth)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_oauth_redirect_uri: str = "https://acceldocs.vercel.app/auth/callback"
    google_oauth_token_file: str = "oauth-token.json"

    # Google Drive service account (optional, if org policy allows keys)
    google_service_account_file: str = "service-account.json"
    google_drive_root_folder_id: str = ""

    # Zensical docs site repo
    docs_repo_path: str = "./docs-site"
    docs_repo_url: str = ""

    # Server
    environment: str = "development"  # development | staging | production
    host: str = "0.0.0.0"
    port: int = 8000
    secret_key: str = "change-me-in-production"
    allowed_origins: str = "http://localhost:3000,http://localhost:5173,http://localhost:8081,https://localhost:8081,https://acceldocs.vercel.app"
    frontend_url: str = "https://acceldocs.vercel.app"
    auto_create_schema: bool = False

    # Security / rate limiting
    rate_limit_enabled: bool = True
    rate_limit_default: str = "200/minute"
    # Use Redis in multi-instance production, e.g. "redis://host:6379/0"
    rate_limit_storage_uri: str = "memory://"

    # Email (Resend)
    resend_api_key: str = ""
    resend_from_email: str = "AccelDocs <noreply@docspeare.com>"

    # Netlify
    netlify_site_id: str = ""
    netlify_auth_token: str = ""

    # AI Agent
    # Providers: "gemini" (free tier), "groq" (free tier), "anthropic" (paid),
    #            "openai_compat" (any OpenAI-compatible endpoint — Ollama, vLLM, etc.)
    agent_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5-20250514"
    # Generic OpenAI-compatible endpoint (Ollama, vLLM, LiteLLM, etc.)
    openai_compat_base_url: str = "http://localhost:11434"
    openai_compat_model: str = "qwen2.5"
    openai_compat_api_key: str = ""  # Optional — some endpoints need it
    agent_rate_limit_per_org: int = 50  # messages per day per org (0 = unlimited)

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"prod", "production"}

    @property
    def service_account_path(self) -> Path:
        return Path(self.google_service_account_file)

    @property
    def oauth_token_path(self) -> Path:
        return Path(self.google_oauth_token_file)


settings = Settings()


def _normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/")


def _validate_runtime_settings() -> None:
    """Fail fast on unsafe production configuration."""
    if not settings.is_production:
        return

    errors: list[str] = []

    if settings.is_sqlite:
        errors.append("DATABASE_URL cannot use sqlite in production.")

    if not settings.secret_key or settings.secret_key == "change-me-in-production":
        errors.append("SECRET_KEY must be set to a secure random value in production.")
    elif len(settings.secret_key) < 32:
        errors.append("SECRET_KEY must be at least 32 characters in production.")

    if settings.rate_limit_enabled and settings.rate_limit_storage_uri.strip().lower() == "memory://":
        errors.append("RATE_LIMIT_STORAGE_URI must use Redis in production (memory:// is single-instance only).")

    allowed_origins = settings.allowed_origins_list
    if not allowed_origins:
        errors.append("ALLOWED_ORIGINS cannot be empty in production.")
    else:
        local_origins = [
            origin
            for origin in allowed_origins
            if "localhost" in origin.lower() or "127.0.0.1" in origin
        ]
        if local_origins:
            errors.append(f"ALLOWED_ORIGINS contains local origins in production: {', '.join(local_origins)}")

        non_https_origins = [origin for origin in allowed_origins if not origin.lower().startswith("https://")]
        if non_https_origins:
            errors.append(f"ALLOWED_ORIGINS must use https in production: {', '.join(non_https_origins)}")

        normalized_allowed = {_normalize_origin(origin) for origin in allowed_origins}
        normalized_frontend = _normalize_origin(settings.frontend_url)
        if normalized_frontend not in normalized_allowed:
            errors.append("FRONTEND_URL must be present in ALLOWED_ORIGINS for production CORS.")

    if not settings.frontend_url.startswith("https://"):
        errors.append("FRONTEND_URL must use https in production.")

    if not settings.google_client_id.strip() or not settings.google_client_secret.strip():
        errors.append("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be configured in production.")

    if settings.google_oauth_redirect_uri and not settings.google_oauth_redirect_uri.startswith("https://"):
        errors.append("GOOGLE_OAUTH_REDIRECT_URI must use https in production.")

    if errors:
        raise RuntimeError(
            "Invalid production configuration:\n- " + "\n- ".join(errors)
        )


_validate_runtime_settings()
