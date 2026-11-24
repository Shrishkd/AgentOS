import asyncio
import logging
import logging.config
import signal
from pathlib import Path
from decimal import Decimal
from urllib.parse import urlsplit

import nats
from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict
from sqlalchemy.ext.asyncio import create_async_engine

from .config import DatabaseSettings
from .logging import logging_config

STREAM = "AGENTOS"
CONSUMER = "WORKERS"
SUBJECT = "agentos.wakeup"


class RuntimeSettings(DatabaseSettings):
    model_config = SettingsConfigDict(env_ignore_empty=True)
    broker_url: str = "nats://nats:4222"
    broker_password_file: Path = Path("/secrets/runtime/broker")
    worker_capacity: int = Field(default=2, ge=1, le=32)
    lease_seconds: int = Field(default=15, ge=3, le=300)
    model_base_url: str = ""
    remote_model: str = ""
    model_api_key_file: Path | None = None
    model_timeout_seconds: float = Field(default=20, gt=0, le=300)
    model_output_cap: int = Field(default=512, ge=1, le=16384)
    model_input_usd_per_million: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    model_output_usd_per_million: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_provider(self):
        if bool(self.remote_model) != bool(self.model_base_url):
            raise ValueError("Set the remote model and base URL together")
        if self.model_base_url:
            url = urlsplit(self.model_base_url)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("Invalid model base URL")
            if self.model_api_key_file and url.scheme != "https":
                raise ValueError("A credentialed model endpoint requires HTTPS")
        if (self.model_input_usd_per_million is None) != (self.model_output_usd_per_million is None):
            raise ValueError("Set both model token prices together")
        return self


async def pause(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass


def log_error(event: str, exc: Exception) -> None:
    logging.getLogger("agentos.runtime").warning(event, extra={"error_type": type(exc).__name__})


async def serve(service):
    logging.config.dictConfig(logging_config("INFO"))
    cfg = RuntimeSettings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    engine = create_async_engine(cfg.database_url(), pool_pre_ping=True,
                                 connect_args={"timeout": 3, "command_timeout": 5})

    async def broker_error(exc):
        log_error("broker_error", exc)

    nc = None
    try:
        nc = await nats.connect(cfg.broker_url, user=cfg.db_user,
                                inbox_prefix=f"_INBOX.{cfg.db_user}".encode(),
                                password=cfg.broker_password_file.read_text().strip(), error_cb=broker_error,
                                connect_timeout=2, reconnect_time_wait=1, max_reconnect_attempts=5)
        from .telemetry import tracing
        with tracing(cfg.db_user.replace("_", "-")):
            await service(cfg, engine, nc, stop)
    finally:
        if nc:
            await nc.close()
        await engine.dispose()
