"""Verify the migrated PostgreSQL policies on a reused API-role connection."""
import asyncio
from pathlib import Path
from time import monotonic
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from testcontainers.core.container import DockerContainer


async def initialize(port, password):
    deadline = monotonic() + 60
    while True:
        try:
            db = await asyncpg.connect(host="127.0.0.1", port=port, database="postgres",
                                       user="postgres", password=password, timeout=2)
            break
        except (OSError, asyncpg.PostgresConnectionError):
            if monotonic() >= deadline:
                raise
            await asyncio.sleep(0.25)
    try:
        await db.execute("CREATE ROLE agentos_api; CREATE ROLE agentos_worker; CREATE ROLE agentos_publisher")
    finally:
        await db.close()


async def check_scope(port, password):
    db = await asyncpg.connect(host="127.0.0.1", port=port, database="postgres", user="postgres", password=password)
    try:
        first, second = uuid4(), uuid4()
        for tenant, name in ((first, "alpha"), (second, "beta")):
            await db.execute("INSERT INTO tenants(id,slug) VALUES($1,$2)", tenant, name)
            await db.execute("INSERT INTO agents(tenant_id,name) VALUES($1,$2)", tenant, name)
        await db.execute("SET ROLE agentos_api")
        assert not await db.fetch("SELECT name FROM agents")
        for tenant, expected in ((first, "alpha"), (second, "beta")):
            async with db.transaction():
                await db.execute("SELECT set_config('agentos.tenant_id',$1,true)", str(tenant))
                assert await db.fetchval("SELECT array_agg(name) FROM agents") == [expected]
            assert not await db.fetch("SELECT name FROM agents")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            async with db.transaction():
                await db.execute("SELECT set_config('agentos.tenant_id',$1,true)", str(first))
                await db.execute("INSERT INTO agents(tenant_id,name) VALUES($1,'denied')", second)
        assert not await db.fetch("SELECT name FROM agents")
    finally:
        await db.close()


def test_transaction_local_tenant_isolation(monkeypatch, tmp_path):
    password = uuid4().hex
    container = DockerContainer("postgres:17-alpine").with_env("POSTGRES_PASSWORD", password)
    # Docker SDK host-address form requests an ephemeral loopback-only port.
    container.ports["5432/tcp"] = ("127.0.0.1", None)
    with container:
        port = container.get_exposed_port(5432)
        asyncio.run(initialize(port, password))
        secret = tmp_path / "pg-password"
        secret.write_text(password, encoding="utf-8")
        for name, value in {"HOST": "127.0.0.1", "PORT": str(port), "NAME": "postgres",
                            "USER": "postgres", "PASSWORD_FILE": str(secret)}.items():
            monkeypatch.setenv("AGENTOS_DB_" + name, value)
        backend = Path(__file__).resolve().parents[1]
        cfg = Config(str(backend / "alembic.ini"))
        cfg.set_main_option("script_location", str(backend / "migrations"))
        command.upgrade(cfg, "head")
        asyncio.run(check_scope(port, password))
