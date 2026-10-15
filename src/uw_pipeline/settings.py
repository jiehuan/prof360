"""Runtime settings. Secrets come only from environment variables (or a local, git-ignored .env)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # SEC requires "<name> <contact email>"; requests without it get blocked.
    sec_user_agent: str
    # SEC fair-access limit is 10 req/s; keep headroom.
    sec_rate_limit_per_sec: float = Field(default=8.0, gt=0, le=10)
    sec_max_attempts: int = Field(default=5, ge=1)
    http_timeout_sec: float = 30.0
    # How far back to read filing XBRL instances for instance-only concepts (company overrides).
    xbrl_history_years: int = Field(default=6, ge=1)
    # 10-K/10-Q text (sections) and earnings releases are kept for this many days back.
    text_lookback_days: int = Field(default=400, ge=30)
    # 8-K events and governance signals look back this far.
    event_lookback_days: int = Field(default=730, ge=30)

    data_dir: Path = Path("data")
    config_dir: Path = Path("config")
    database_url: str = "sqlite:///data/uw.db"
    # Application database the `publish` stage writes (Phase 9); Azure SQL in production.
    app_database_url: str = "sqlite:///data/app.db"
    # Phase 12 (D-040): `url` uses the URLs above (SQLite off the network, or any SQLAlchemy URL);
    # `azure_sql` builds the URL from config/azure.yaml with a service principal.
    database_backend: Literal["url", "azure_sql"] = "url"  # anything else fails at startup
    # Schema of the pipeline / application tables (default: from config/azure.yaml for
    # azure_sql, none otherwise).
    database_schema: str | None = None
    app_database_schema: str | None = None
    azure_env: str | None = None  # dv | pp | pr; default from config/azure.yaml
    azure_sql_client_id: str | None = None
    azure_sql_client_secret: SecretStr | None = None
    # Raw landing zone: `local` (data/raw) or `adls` (config/azure.yaml).
    storage_backend: Literal["local", "adls"] = "local"
    adls_client_id: str | None = None
    adls_client_secret: SecretStr | None = None

    alphavantage_api_key: SecretStr | None = None
    # Free plan: 25 calls/day. Premium (75 calls/min): set the rate to 75 and the quota to 0.
    alphavantage_calls_per_minute: float = Field(default=5, gt=0)
    alphavantage_daily_quota: int = Field(default=25, ge=0)  # 0 = unlimited
    # LLM provider profile in config/llm.yaml (default: default_provider, securegpt).
    llm_provider: str | None = None
    # SecureGPT gateway (provider securegpt; D-039).
    securegpt_client_id: str | None = None
    securegpt_client_secret: SecretStr | None = None
    securegpt_subscription_key: SecretStr | None = None
    securegpt_env: str | None = None  # dv | pp | pr; default from config/securegpt.yaml
    # Turn LLM voting off (rules only), e.g. when no credentials are available.
    llm_enabled: bool = True
    # New LLM calls per UTC day, in the active profile's currency (D-043); 0 = unlimited.
    llm_daily_budget: float = Field(default=25.0, ge=0)

    @field_validator("sec_user_agent")
    @classmethod
    def _user_agent_has_contact(cls, v: str) -> str:
        v = v.strip()
        if not _EMAIL_RE.search(v):
            raise ValueError("SEC_USER_AGENT must contain a contact email: 'Jane Doe jane@x.com'")
        return v

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"
