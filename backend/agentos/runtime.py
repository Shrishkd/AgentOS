import asyncio
import logging
import logging.config
import signal
from pathlib import Path

import nats
from pydantic import Field
from sqlalchemy.ext.asyncio import create_async_engine

from .config import DatabaseSettings
from .logging import logging_config

STREAM = "AGENTOS"
CONSUMER = "WORKERS"
SUBJECT = "agentos.wakeup"


class RuntimeSettings(DatabaseSettings):
    broker_url: str = "nats://nats:4222"
    broker_password_file: Path = Path("/secrets/runtime/broker")
    worker_capacity: int = Field(default=2, ge=1, le=32)
    lease_seconds: int = Field(default=15, ge=3, le=300)


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
        await service(cfg, engine, nc, stop)
    finally:
        if nc:
            await nc.close()
        await engine.dispose()
