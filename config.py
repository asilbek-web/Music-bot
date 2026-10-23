"""Application settings loaded from environment variables and a local .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the bot."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    bot_token: str = Field(description="Token issued by @BotFather")
    admin_ids: Annotated[list[int], NoDecode] = Field(
        default_factory=list,
        description="Comma-separated Telegram user IDs allowed to use admin commands",
    )

    db_path: Path = Path("data/music_bot.db")
    audio_dir: Path = Path("data/audio")
    search_limit: int = Field(default=20, ge=1, le=25)
    itunes_country: str = Field(default="US", min_length=2, max_length=2)
    request_timeout: float = Field(default=20.0, gt=0, le=120)
    audio_cache_ttl: int = Field(default=86_400, ge=0, description="Seconds a downloaded preview stays cached")
    max_audio_size: int = Field(default=20 * 1024 * 1024, gt=0)
    port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        description="Port for the HTTP health server; Render sets PORT automatically",
    )
    log_level: str = "INFO"

    @field_validator("bot_token")
    @classmethod
    def _validate_token(cls, value: str) -> str:
        token = value.strip()
        if ":" not in token:
            raise ValueError("BOT_TOKEN must look like 123456789:AAExampleTokenFromBotFather")
        return token

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: Any) -> Any:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            ids: list[int] = []
            for chunk in value.replace(";", ",").split(","):
                chunk = chunk.strip()
                if not chunk:
                    continue
                try:
                    ids.append(int(chunk))
                except ValueError as exc:
                    raise ValueError(f"ADMIN_IDS contains a non-numeric value: {chunk!r}") from exc
            return ids
        return value

    @field_validator("itunes_country")
    @classmethod
    def _normalise_country(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        return value.strip().upper()

    def ensure_directories(self) -> None:
        """Create the directories needed for the database and cached audio."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the settings singleton (raises pydantic.ValidationError when misconfigured)."""
    return Settings()
