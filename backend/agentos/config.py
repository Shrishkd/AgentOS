from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTOS_", hide_input_in_errors=True)

    db_host: str = Field(min_length=1)
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = Field(min_length=1)
    db_user: str = Field(min_length=1)
    db_password_file: Path

    def database_url(self) -> URL:
        password = self.db_password_file.read_text(encoding="utf-8").strip()
        if not password:
            raise ValueError("Database password file is empty")
        return URL.create(
            "postgresql+asyncpg", self.db_user, password,
            self.db_host, self.db_port, self.db_name,
        )


class Settings(DatabaseSettings):
    token_issuer: str = "urn:agentos:local"
    token_audience: str = "agentos-api"
    token_public_key_file: Path = Path("/keys/public/issuer.pem")
    redis_url: SecretStr
    health_timeout_seconds: float = Field(default=2, gt=0, le=10)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
