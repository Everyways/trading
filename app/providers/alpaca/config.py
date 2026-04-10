"""AlpacaConfig — environment-driven settings for the Alpaca broker provider.

Loaded from environment variables or .env file. Isolated to this package.
No other module in the codebase should import from app.providers.alpaca.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AlpacaConfig(BaseSettings):
    """Alpaca API credentials and endpoint configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    alpaca_api_key: str = Field(..., alias="ALPACA_API_KEY")
    alpaca_api_secret: str = Field(..., alias="ALPACA_API_SECRET")
    alpaca_base_url: str = Field(
        default="https://paper-api.alpaca.markets",
        alias="ALPACA_BASE_URL",
    )

    @property
    def is_paper(self) -> bool:
        """True when pointing at the paper trading endpoint."""
        return "paper" in self.alpaca_base_url.lower()
