import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

from alembic.script import ScriptDirectory
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)
Probe = Callable[[], Awaitable[bool]]
CheckStatus = Literal["up", "down"]


class Readiness(BaseModel):
    status: Literal["ready", "unavailable"]
    checks: dict[str, CheckStatus]


def dependency_checks(engine: AsyncEngine, redis: Redis) -> dict[str, Probe]:
    migrations = Path(__file__).resolve().parents[1] / "migrations"
    expected_revision = ScriptDirectory(str(migrations)).get_current_head()

    async def database_ready() -> bool:
        async with engine.connect() as connection:
            revisions = await connection.scalars(text("SELECT version_num FROM alembic_version"))
            return list(revisions) == [expected_revision]

    return {"postgresql": database_ready, "redis": redis.ping}


async def check_readiness(probes: dict[str, Probe], timeout: float) -> Readiness:
    async def check(name: str, probe: Probe) -> CheckStatus:
        try:
            async with asyncio.timeout(timeout):
                if await probe():
                    return "up"
        except Exception as exc:
            # Driver messages can contain credentials, connection URLs or submitted SQL.
            logger.warning("dependency_probe_failed", extra={"dependency": name, "error_type": type(exc).__name__})
        return "down"

    results = await asyncio.gather(*(check(name, probe) for name, probe in probes.items()))
    return Readiness(
        status="ready" if all(result == "up" for result in results) else "unavailable",
        checks=dict(zip(probes, results, strict=True)),
    )
