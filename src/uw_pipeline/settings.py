"""Runtime settings. Secrets come only from environment variables (or a local, git-ignored .env)."""

from __future__ import annotations

import re
from pathlib import Path

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

    alphavantage_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None

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
