"""Every setting the API needs, read from the environment (and a local .env file).

A missing or invalid value stops the app at start-up, which is much better than
failing on the first payment.
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["debug", "info", "warning", "error", "critical", "silent"] = "info"

    database_url: str = Field(pattern=r"^mysql\+asyncmy://")
    simbank_url: str = Field(pattern=r"^https?://")
    simbank_timeout_seconds: float = Field(default=3.0, gt=0)
    redis_url: str = Field(pattern=r"^rediss?://")

    # dashboard sessions
    dashboard_origin: str = "http://localhost:3000"
    session_idle_seconds: int = Field(default=60 * 60 * 24, gt=0)  # 1 day without activity
    session_max_seconds: int = Field(default=60 * 60 * 24 * 7, gt=0)  # 7 days at most

    # "Sign in with GitHub" (disabled while the client ID/secret are empty)
    github_client_id: str = ""
    github_client_secret: str = ""
    github_callback_url: str = "http://localhost:3000/auth/github/callback"
    github_oauth_base: str = "https://github.com"  # overridable so tests can use a fake GitHub
    github_api_base: str = "https://api.github.com"


settings = Settings()  # values come from the environment
