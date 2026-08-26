"""Shared runtime config, loaded from env once at process start."""

from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ------------------------------------------------------------------ TG
    tg_api_id: int = Field(..., description="my.telegram.org/apps → App api_id")
    tg_api_hash: str = Field(..., description="my.telegram.org/apps → App api_hash")
    tg_bot_token: str = Field(default="", description="BotFather token (empty for ingest-only mode)")
    tg_session_name: str = Field(default="openrelay_ingest", description="Telethon session name")

    # ------------------------------------------------------------------ DB
    postgres_dsn: str = Field(
        default="postgresql+asyncpg://openrelay:openrelay@localhost:5432/openrelay_tg",
    )
    redis_url: str = Field(default="redis://localhost:6379/0")

    # ------------------------------------------------------------------ Inference
    openrelay_base_url: str = Field(default="https://openrelay.hggfffdfy687.workers.dev")
    # Custodial master seed — derives per-channel subaccounts. Loaded from a
    # separate secrets store in prod; env var is beta-only.
    treasury_master_seed_hex: str = Field(default="", description="64-hex-char seed")

    # ------------------------------------------------------------------ Feature flags
    setup_fee_enabled: bool = Field(default=False, description="Off during beta")
    setup_fee_simple_usd: float = Field(default=75.0)
    setup_fee_docker_usd: float = Field(default=25.0)

    # Per-request hard caps
    max_usd_per_request: float = Field(default=0.05)

    # Model routing
    model_large: str = Field(default="deepseek-ai/DeepSeek-V4-Flash-0731")
    model_small: str = Field(default="moonshotai/Kimi-K2.6")

    # Embeddings
    embed_model: str = Field(default="sentence-transformers/all-MiniLM-L6-v2")
    embed_dim: int = Field(default=384)


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
