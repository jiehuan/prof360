"""BFF settings from environment variables (or a local .env)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Development: the SQLite application database the pipeline's publish stage writes.
    bff_database_url: str = f"sqlite:///{REPO / 'data' / 'app.db'}"
    # azure_sql: the app schema of config/azure.yaml, with the BFF's own service principal.
    bff_database_backend: Literal["url", "azure_sql"] = "url"
    azure_env: str | None = None
    bff_sql_client_id: str | None = None
    bff_sql_client_secret: SecretStr | None = None
    config_dir: Path = REPO / "config"
    # dev: the user is the X-User-Email header, or `bff_dev_user_email`. SSO comes later (D-048).
    bff_auth_mode: Literal["dev"] = "dev"
    bff_dev_user_email: str = "dev.user@example.com"
    bff_cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"]
    )
