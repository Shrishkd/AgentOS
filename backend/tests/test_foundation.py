import asyncio
import json
import logging
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agentos.config import Settings
from agentos.health import check_readiness
from agentos.logging import JsonFormatter
from agentos.main import create_app


@pytest.fixture
def settings(tmp_path):
    password = tmp_path / "password"
    password.write_text("test-only", encoding="utf-8")
    return Settings(db_host="localhost", db_name="agentos", db_user="test",
                    db_password_file=password, redis_url="redis://:private-sentinel@localhost")


def test_missing_configuration_fails(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(f"AGENTOS_{name.upper()}", raising=False)
    with pytest.raises(ValidationError) as error:
        Settings()
    assert "db_host" in str(error.value)


def test_settings_redact_credentials(settings):
    assert "private-sentinel" not in repr(settings)
    assert "test-only" not in str(settings.database_url())


@pytest.mark.parametrize("database_up,expected", [(True, 200), (False, 503)])
def test_health_routes_and_request_ids(settings, database_up, expected):
    app = create_app(settings)
    with TestClient(app) as client:
        db = AsyncMock(return_value=database_up)
        app.state.probes = {"postgresql": db, "redis": AsyncMock(return_value=True)}
        live = client.get("/health/live", headers={"X-Request-ID": "invalid"})
        assert live.json() == {"status": "alive"}
        UUID(live.headers["X-Request-ID"])
        db.assert_not_awaited()
        request_id = str(uuid4())
        response = client.get("/health/ready", headers={"X-Request-ID": request_id})
        assert response.status_code == expected
        assert response.headers["X-Request-ID"] == request_id
        assert response.json()["checks"]["postgresql"] == ("up" if database_up else "down")


def test_probe_error_and_timeout_are_bounded_and_redacted(caplog):
    async def slow():
        await asyncio.sleep(30)
        return True

    report = asyncio.run(check_readiness({
        "redis": AsyncMock(side_effect=OSError("private-sentinel")),
        "postgresql": slow,
    }, timeout=0.01))
    assert report.checks == {"redis": "down", "postgresql": "down"}
    assert len(caplog.records) == 2
    for record in caplog.records:
        entry = JsonFormatter().format(record)
        assert "private-sentinel" not in entry
        assert json.loads(entry)["level"] == logging.getLevelName(logging.WARNING)


def test_internal_error_is_correlated_and_redacted(settings, caplog):
    app = create_app(settings)

    @app.get("/test-error")
    def broken():
        raise RuntimeError("private-sentinel")

    with TestClient(app) as client:
        response = client.get("/test-error")
    assert response.status_code == 500
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
    assert "private-sentinel" not in response.text + caplog.text
