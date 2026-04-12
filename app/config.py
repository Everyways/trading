"""Global application settings — loaded from environment variables / .env file.

All secrets come from the environment. No hardcoded values.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GlobalSettings(BaseSettings):
    """Application-wide configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Database ---
    database_url: str = Field(
        ...,
        description="Async PostgreSQL DSN (asyncpg driver)",
    )
    database_url_sync: str = Field(
        ...,
        description="Sync PostgreSQL DSN (psycopg2 driver, used by Alembic)",
    )

    # --- Security ---
    secret_key: str = Field(..., min_length=32)
    dashboard_user: str = Field(default="admin")
    dashboard_password: str = Field(...)

    # --- Telegram ---
    telegram_bot_token: str | None = Field(default=None)
    telegram_chat_id_global: str | None = Field(default=None)

    # --- Trading safety ---
    allow_live_on_laptop: bool = Field(
        default=False,
        description="Allow live trading on a laptop. Never set True in production.",
    )
    accept_monthly_stop_override: bool = Field(
        default=False,
        alias="TRADING_BOT_ACCEPT_MONTHLY_STOP_OVERRIDE",
        description="Override the permanent monthly stop. Creates CRITICAL audit log.",
    )
    panic: bool = Field(
        default=False,
        alias="TRADING_BOT_PANIC",
        description="Engage global kill switch immediately on boot.",
    )

    # --- Paths ---
    data_dir: str = Field(default="./data")
    config_dir: str = Field(default="./config")

    # --- Environment ---
    environment: str = Field(default="development")

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        allowed = {"development", "production"}
        if v not in allowed:
            raise ValueError(f"environment must be one of {allowed}, got '{v}'")
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def live_approval_env_var(self, strategy_name: str) -> str:
        """Return the env var name for per-strategy live approval (§12)."""
        return f"TRADING_BOT_LIVE_APPROVAL_{strategy_name.upper()}"


# Lazily instantiated — callers import get_settings() to avoid import-time side effects.
_settings: GlobalSettings | None = None


def get_settings() -> GlobalSettings:
    """Return the singleton settings instance, creating it on first call."""
    global _settings
    if _settings is None:
        _settings = GlobalSettings()  # type: ignore[call-arg]
    return _settings
