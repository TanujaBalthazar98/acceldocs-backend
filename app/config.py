"""Application configuration via environment variables."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database
    database_url: str = "sqlite:///./acceldocs.db"

    # Google OAuth (interactive/user auth)
    google_client_id: str = ""
    google_client_secret: str = ""
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
    allowed_origins: str = "http://localhost:3000,http://localhost:5173"

    # Netlify
    netlify_site_id: str = ""
    netlify_auth_token: str = ""

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
