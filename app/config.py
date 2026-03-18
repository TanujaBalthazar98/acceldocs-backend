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
    def service_account_path(self) -> Path:
        return Path(self.google_service_account_file)

    @property
    def oauth_token_path(self) -> Path:
        return Path(self.google_oauth_token_file)


settings = Settings()

# Fail loudly if running with the default secret key in a production-like environment
# (i.e. when DATABASE_URL points to a real database, not local SQLite)
if settings.secret_key == "change-me-in-production" and not settings.is_sqlite:
    import warnings
    warnings.warn(
        "CRITICAL: secret_key is still set to the default value. "
        "Set the SECRET_KEY environment variable to a secure random string.",
        stacklevel=1,
    )
