"""Exercise a disposable Compose project. Run from any directory with Python 3.12+."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from uuid import uuid4

from check_registry import check_registry, request_json
from check_workers import check_workers
from check_models import check_models

ROOT = Path(__file__).resolve().parents[1]


def await_json(base: str, path: str, status: int, expected: dict) -> None:
    deadline = monotonic() + 90
    last = None
    while monotonic() < deadline:
        try:
            try:
                response = urlopen(base + path, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                last = (response.status, json.load(response))
            if last[0] == status and all(last[1].get(k) == v for k, v in expected.items()):
                return
        except (URLError, TimeoutError, ValueError, ConnectionError) as error:
            last = str(error)
        sleep(1)
    raise AssertionError(f"{path} expected {status} {expected}, last response {last}")


def main(browser: bool) -> None:
    project = f"agentos-test-{uuid4().hex}"
    env = {**os.environ, "AGENTOS_PORT": "0"}

    def compose(*args: str, capture: bool = False, stdin: str | None = None) -> str:
        result = subprocess.run(
            ["docker", "compose", "-p", project, "-f", str(ROOT / "compose.yaml"),
             "-f", str(ROOT / "scripts/fixtures/model-compose.yaml"), *args],
            cwd=ROOT, env=env, check=True, input=stdin, stdout=subprocess.PIPE if capture else None, text=True,
        )
        return (result.stdout or "").strip()

    def start(*options: str) -> str:
        compose("up", "-d", *options)
        base = "http://" + compose("port", "dashboard", "8080", capture=True)
        await_json(base, "/health/ready", 200, {"status": "ready"})
        await_json(base, "/health/jetstream", 200, {"status": "ok"})
        return base

    def sql(query: str) -> str:
        return compose("exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "agentos",
                       "-v", "ON_ERROR_STOP=1", "-Atc", query, capture=True)

    try:
        # Exercise admission and destructive migration fixtures before enabling dispatch.
        base = start("--build", "--scale", "worker=0", "--scale", "publisher=0")
        await_json(base, "/health/live", 200, {"status": "alive"})
        with urlopen(base + "/", timeout=5) as response:
            assert "System health" in response.read().decode()
        if browser:
            subprocess.run([shutil.which("npm.cmd" if os.name == "nt" else "npm"), "run", "test:browser"],
                           cwd=ROOT / "dashboard", env={**env, "AGENTOS_TEST_URL": base}, check=True)
        assert sql("SELECT version_num FROM alembic_version") == "0004_model_runtime"
        sql("CREATE TABLE persistence_probe AS SELECT 42 AS value")
        compose("run", "--rm", "--no-deps", "migrate")
        compose("run", "--rm", "--no-deps", "migrate", "alembic", "downgrade", "base")
        await_json(base, "/health/ready", 503, {"checks": {"postgresql": "down", "redis": "up"}})
        compose("run", "--rm", "--no-deps", "migrate")
        await_json(base, "/health/ready", 200, {"status": "ready"})
        compose("build", "operator")
        run_path, token, run_id = check_registry(base, compose, sql)
        # Previous phases left two model runs queued. Exercise that backlog before lease-order fixtures.
        check_models(base, run_path, token, compose, sql)
        check_workers(base, run_path, token, compose, sql)
        for service in ("redis", "postgres"):
            compose("stop", service)
            await_json(base, "/health/live", 200, {"status": "alive"})
            await_json(base, "/health/ready", 503, {
                "checks": {"postgresql": "down" if service == "postgres" else "up",
                           "redis": "down" if service == "redis" else "up"},
            })
            compose("start", service)
            await_json(base, "/health/ready", 200, {"status": "ready"})
        compose("stop", "nats")
        await_json(base, "/health/ready", 200, {"status": "ready"})
        try:
            with urlopen(base + "/health/jetstream", timeout=10) as response:
                raise AssertionError(f"Stopped broker returned {response.status}")
        except HTTPError as error:
            assert error.code in {502, 503, 504}
        compose("down")
        base = start()
        assert sql("SELECT value FROM persistence_probe") == "42"
        assert request_json(base + run_path, token=token)[1]["id"] == run_id
        print("Foundation checks passed, including migration replay, dependency outages and volume reuse.")
    except Exception:
        compose("ps", "-a")
        compose("logs", "--tail", "30", "api", "migrate", "dashboard", "worker", "publisher", "nats")
        raise
    finally:
        # The random project owns these volumes. Never use the developer's project name.
        compose("--profile", "tools", "down", "--volumes", "--remove-orphans", "--rmi", "local")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", action="store_true", help="also run the installed Playwright suite")
    main(parser.parse_args().browser)
