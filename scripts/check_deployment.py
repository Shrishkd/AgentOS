"""Exercise a TLS release and point-in-time recovery on two disposable Compose projects."""
import ipaddress
import json
import os
import secrets
import ssl
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
ISSUER = "urn:agentos:deployment-drill"


def credentials(directory):
    for service in ("api", "admin", "publisher", "worker", "identity", "tls", "alertmanager"):
        (directory / service).mkdir()
    for role in ("api", "admin", "worker", "publisher"):
        (directory / role / "password").write_text(secrets.token_hex(32))
    for role in ("worker", "publisher"):
        (directory / role / "broker").write_text(secrets.token_hex(32))
    (directory / "alertmanager/alertmanager.yml").write_text(
        "route:\n  receiver: drill\n  group_wait: 0s\nreceivers:\n  - name: drill\n"
        "    webhook_configs:\n      - url: http://receiver:9099/alerts\n")
    identity = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (directory / "identity/issuer.pem").write_bytes(identity.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=2)).add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False).sign(key, hashes.SHA256()))
    (directory / "tls/fullchain.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "tls/privkey.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    # Test-only bind mounts must be readable by the distinct container UIDs.
    for path in directory.glob("*/*"):
        path.chmod(0o444)
    return jwt.encode({"iss": ISSUER, "aud": "agentos-drill", "sub": "drill", "exp": now + timedelta(hours=1)},
                      identity, algorithm="RS256")


def wait_for(predicate, message, seconds=120):
    deadline = monotonic() + seconds
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.5)
    raise AssertionError(message)


def drill(directory):
    tag = "agentos-drill-" + uuid4().hex
    source, target, latest = tag + "-primary", tag + "-restore", tag + "-latest"
    archive, backup = tag + "-wal", tag + "-backup"
    token = credentials(directory)
    context = ssl.create_default_context(cafile=str(directory / "tls/fullchain.pem"))
    env = {**os.environ, "AGENTOS_SECRET_DIR": str(directory), "AGENTOS_TOKEN_ISSUER": ISSUER,
           "AGENTOS_TOKEN_AUDIENCE": "agentos-drill", "AGENTOS_BIND_ADDRESS": "127.0.0.1", "AGENTOS_HTTPS_PORT": "0",
           "AGENTOS_BACKEND_IMAGE": tag + "-backend", "AGENTOS_DASHBOARD_IMAGE": tag + "-dashboard",
           "AGENTOS_WAL_VOLUME": archive, "AGENTOS_BACKUP_VOLUME": backup, "AGENTOS_REMOTE_MODEL": "",
           "AGENTOS_MODEL_BASE_URL": "", "AGENTOS_LEASE_SECONDS": "8", "AGENTOS_MODEL_API_KEY_FILE": ""}

    def docker(*args, check=True, capture=False):
        result = subprocess.run(["docker", *args], cwd=ROOT, env=env, check=check, text=True,
                                stdout=subprocess.PIPE if capture else None)
        return result.stdout.strip() if capture else result.returncode

    def compose(project, *args, **kwargs):
        return docker("compose", "-p", project, "-f", "compose.yaml", "-f", "compose.production.yaml",
                      "-f", "scripts/fixtures/deployment-compose.yaml", *args, **kwargs)

    def sql(project, statement):
        return compose(project, "exec", "-T", "postgres", "psql", "-XqAt", "-v", "ON_ERROR_STOP=1",
                       "-U", "postgres", "-d", "agentos", "-c", statement, capture=True)

    def gateway(project):
        return "https://" + compose(project, "port", "dashboard", "8443", capture=True)

    def call(url, method="GET", body=None):
        headers = {"Authorization": "Bearer " + token, "Idempotency-Key": str(uuid4()), "Content-Type": "application/json"}
        req = Request(url, method=method, headers=headers, data=json.dumps(body).encode() if body is not None else None)
        with urlopen(req, context=context, timeout=10) as reply:
            return json.load(reply)

    try:
        for name in (archive, backup):
            docker("volume", "create", name)
        docker("build", "-t", env["AGENTOS_BACKEND_IMAGE"], "backend")
        docker("build", "-t", env["AGENTOS_DASHBOARD_IMAGE"], "dashboard")
        compose(source, "up", "-d", "--no-build", "--wait", "--scale", "worker=2")
        compose(source, "exec", "-T", "prometheus", "promtool", "test", "rules", "/drill/alerts-test.yaml")
        base = gateway(source)
        assert call(base + "/health/ready")["status"] == "ready"
        tenant = str(UUID(compose(source, "run", "--rm", "--no-deps", "-T", "operator", "grant", "--tenant", "drill",
                                 "--issuer", ISSUER, "--role", "developer", "--subject", "drill", capture=True)))
        root = base + "/v1/tenants/" + tenant
        agent = call(root + "/agents", "POST", {"name": "Recovery drill"})["id"]
        versions = root + "/agents/" + agent + "/versions"
        version = call(versions, "POST", {"provider": "mock", "model": "deterministic", "instructions": "Return input",
            "limits": {"max_steps": 2, "max_tokens": 1000, "timeout_seconds": 900}})["id"]
        call(versions + "/" + version + "/publish", "POST")
        start = monotonic()
        compose(source, "run", "--rm", "--no-deps", "backup", "base")
        backup_seconds = monotonic() - start
        slow = call(versions, "POST", {"provider": "openai-compatible", "model": "fixture-model", "instructions": "Reply",
            "limits": {"max_steps": 2, "max_tokens": 1000, "timeout_seconds": 90}})["id"]
        call(versions + "/" + slow + "/publish", "POST")
        active = call(root + "/runs", "POST", {"agent_version_id": slow, "input": {"mode": "timeout"}})["id"]
        wait_for(lambda: call(root + "/runs/" + active)["status"] == "RUNNING", "Expected an active model call")
        start = monotonic()
        compose(source, "stop", "worker")
        drain_seconds = monotonic() - start
        assert sql(source, "SELECT count(*) FROM workers WHERE draining AND stopped_at IS NOT NULL") == "2"
        assert [a["outcome"] for a in call(root + "/runs/" + active + "/attempts")] == ["SUCCEEDED"]
        wait_for(lambda: "AgentOSNoWorkers" in compose(source, "logs", "--no-log-prefix", "receiver", capture=True),
                 "The worker outage did not reach the private drill webhook")
        run = call(root + "/runs", "POST", {"agent_version_id": version, "input": {"proof": "restored"}})["id"]
        # Kill this one disposable worker between a durable final checkpoint and completion.
        result = compose(source, "run", "--rm", "--no-deps", "-T", "worker", "python",
                         "/drill/crash_process.py", "after_final_checkpoint", check=False)
        assert result == 137 and sql(source, f"SELECT status FROM runs WHERE id='{run}'") == "RUNNING"
        saved = call(root + "/runs/" + run + "/checkpoints")
        assert saved and saved[-1]["next_action"] == "finish"
        sql(source, "SELECT pg_create_restore_point('agentos_drill')")
        segment = sql(source, "SELECT pg_walfile_name(pg_current_wal_insert_lsn())")
        sql(source, "SELECT pg_switch_wal()")
        wait_for(lambda: compose(source, "exec", "-T", "postgres", "test", "-f", "/archive/" + segment,
                                 check=False) == 0, "Restore point WAL was not archived")
        sql(source, "CREATE TABLE after_restore_point(id int)")
        last_segment = sql(source, "SELECT pg_walfile_name(pg_current_wal_insert_lsn())")
        sql(source, "SELECT pg_switch_wal()")
        wait_for(lambda: compose(source, "exec", "-T", "postgres", "test", "-f", "/archive/" + last_segment,
                                 check=False) == 0, "Latest committed write did not reach the archive")
        compose(source, "down")
        # Read the end of the original timeline in another empty project. Keep this
        # verification clone from writing a promoted timeline into the shared archive.
        compose(latest, "run", "--rm", "--no-deps", "restore", "base", "latest")
        reader = compose(latest, "run", "--rm", "-d", "--no-deps", "postgres", "postgres", "-c", "archive_mode=off",
                         capture=True)
        try:
            wait_for(lambda: docker("exec", reader, "pg_isready", "-U", "postgres", check=False) == 0,
                     "End-of-WAL verification clone did not start")
            wait_for(lambda: docker("exec", reader, "psql", "-U", "postgres", "-d", "agentos", "-Atc",
                        "SELECT NOT pg_is_in_recovery() AND to_regclass('public.after_restore_point') IS NOT NULL",
                        capture=True) == "t", "Latest restore omitted the committed post-marker write")
        finally:
            docker("stop", reader)
        # The recovery project starts with fresh PGDATA, broker and Redis volumes.
        start = monotonic()
        compose(target, "run", "--rm", "--no-deps", "restore", "base", "agentos_drill")
        compose(target, "up", "-d", "--no-build", "--wait", "--scale", "worker=2")
        restored = gateway(target) + "/v1/tenants/" + tenant + "/runs/" + run
        wait_for(lambda: call(restored)["status"] == "SUCCEEDED", "Restored run did not resume")
        recovery_seconds = monotonic() - start
        assert call(restored)["output"] == {"answer": '{"proof":"restored"}'}
        assert call(restored + "/checkpoints") == saved
        assert [a["outcome"] for a in call(restored + "/attempts")] == ["lease_expired", "SUCCEEDED"]
        assert sql(target, "SELECT to_regclass('public.after_restore_point') IS NULL") == "t"
        assert compose(target, "run", "--rm", "--no-deps", "restore", "base", "agentos_drill", check=False) != 0
        print(f"Verified TLS, alert delivery, latest WAL, backup ({backup_seconds:.2f}s), drain ({drain_seconds:.2f}s), "
              f"point-in-time restore through application recovery ({recovery_seconds:.2f}s).", flush=True)
    finally:
        for project in (latest, target, source):
            compose(project, "--profile", "ops", "--profile", "tools", "down", "--volumes", "--remove-orphans")
        docker("volume", "rm", archive, backup, check=False)
        docker("image", "rm", env["AGENTOS_BACKEND_IMAGE"], env["AGENTOS_DASHBOARD_IMAGE"], check=False)


if __name__ == "__main__":
    with TemporaryDirectory(prefix="agentos-deployment-") as temporary:
        drill(Path(temporary))
