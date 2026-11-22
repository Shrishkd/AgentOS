"""Local Compose operator commands for provisioning membership and issuing development tokens."""

import argparse
import asyncio
import fcntl
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .config import DatabaseSettings

LOCAL_ISSUER = "urn:agentos:local"


def write_atomic(path: Path, data: bytes, mode: int) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as file:
        os.fchmod(file.fileno(), mode)
        file.write(data)
    temporary.replace(path)


def issue_token(subject: str) -> str:
    private = Path("/keys/private/issuer.pem")
    public = Path("/keys/public/issuer.pem")
    with private.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if private.exists():
            key = serialization.load_pem_private_key(private.read_bytes(), password=None)
        else:
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            write_atomic(private, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.NoEncryption()), 0o600)
        write_atomic(public, key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo), 0o644)
    now = datetime.now(UTC)
    return jwt.encode({"iss": LOCAL_ISSUER, "aud": "agentos-api", "sub": subject,
                       "iat": now, "exp": now + timedelta(minutes=30), "jti": str(uuid4())}, key, algorithm="RS256")


async def grant(args: argparse.Namespace) -> None:
    engine = create_async_engine(DatabaseSettings().database_url())
    try:
        async with engine.begin() as db:
            tenant = await db.scalar(text("""INSERT INTO tenants (slug) VALUES (:slug)
                ON CONFLICT (slug) DO UPDATE SET slug=excluded.slug RETURNING id"""), {"slug": args.tenant})
            await db.execute(text("""INSERT INTO users (tenant_id, issuer, subject, role)
                VALUES (:tenant, :issuer, :subject, :role) ON CONFLICT (tenant_id,issuer,subject)
                DO UPDATE SET role=excluded.role, enabled=true, updated_at=now()"""),
                {"tenant": tenant, "issuer": args.issuer, "subject": args.subject, "role": args.role})
        print(tenant)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    member = commands.add_parser("grant")
    member.add_argument("--tenant", required=True, help="Tenant slug")
    member.add_argument("--issuer", default=LOCAL_ISSUER)
    member.add_argument("--role", required=True, choices=["viewer", "developer", "approver", "admin"])
    member.add_argument("--subject", required=True)
    token = commands.add_parser("token")
    token.add_argument("--subject", required=True)
    args = parser.parse_args()
    if args.command == "grant":
        asyncio.run(grant(args))
    else:
        print(issue_token(args.subject))
