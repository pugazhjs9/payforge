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
    rabbitmq_url: str = Field(default="amqp://guest:guest@127.0.0.1:5673/", pattern=r"^amqps?://")

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

    # access tokens (JWT) for merchant servers
    jwt_private_key_file: str = ""  # PEM, EC P-256. Empty = a throwaway key (dev/test only)
    jwt_issuer: str = "payforge"
    jwt_audience: str = "payforge-api"
    access_token_ttl_seconds: int = Field(default=15 * 60, gt=0)

    # webhooks sent to merchants
    webhook_timeout_seconds: float = Field(default=10.0, gt=0)
    # LOCAL DEVELOPMENT ONLY: allow http:// and localhost/private-network webhook URLs
    allow_insecure_webhooks: bool = False

    # rate limits
    rate_limit_per_second: float = Field(default=25.0, gt=0)  # per merchant, on /v1
    rate_limit_burst: int = Field(default=100, ge=1)
    login_attempts_per_account: int = Field(default=10, ge=1)  # per email + IP, per 15 min
    login_attempts_per_ip: int = Field(default=100, ge=1)  # per 15 min
    signups_per_ip: int = Field(default=20, ge=1)  # per hour


settings = Settings()  # values come from the environment
