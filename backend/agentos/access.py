from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from .domain import DomainError, missing

bearer = HTTPBearer(auto_error=False)


@dataclass
class TenantAccess:
    db: AsyncConnection
    tenant: UUID
    user: UUID
    role: str
    request_id: str

    def require_developer(self) -> None:
        if self.role not in {"admin", "developer"}:
            raise DomainError(403, "forbidden", "Developer access is required.")

    async def audit(self, action: str, object_id: UUID) -> None:
        await self.db.execute(text("""INSERT INTO audit_events
            (tenant_id, actor_id, action, object_id, request_id)
            VALUES (:tenant,:actor,:action,:object,:request)"""),
            {"tenant": self.tenant, "actor": self.user, "action": action,
             "object": object_id, "request": self.request_id})


async def tenant_access(request: Request, tenant_id: UUID,
                        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
                        ) -> AsyncIterator[TenantAccess]:
    settings = request.app.state.settings
    unauthorized = DomainError(401, "unauthorized", "A valid access token is required.",
                               {"WWW-Authenticate": "Bearer"})
    if not credentials:
        raise unauthorized
    try:
        key = settings.token_public_key_file.read_bytes()
        claims = jwt.decode(credentials.credentials, key, algorithms=["RS256"], issuer=settings.token_issuer,
                            audience=settings.token_audience, options={"require": ["exp", "sub", "iss", "aud"]})
    except jwt.InvalidTokenError:
        raise unauthorized from None
    except (OSError, jwt.InvalidKeyError):
        raise DomainError(503, "identity_unavailable", "Identity verification is unavailable.") from None
    async with request.app.state.engine.begin() as db:
        await db.execute(text("SELECT set_config('agentos.tenant_id', :tenant, true)"), {"tenant": str(tenant_id)})
        member = (await db.execute(text("""SELECT id, role FROM users
            WHERE tenant_id=:tenant AND issuer=:issuer AND subject=:subject AND enabled"""),
            {"tenant": tenant_id, "issuer": claims["iss"], "subject": claims["sub"]})).mappings().one_or_none()
        if member is None:
            raise missing()
        yield TenantAccess(db, tenant_id, member["id"], member["role"], request.state.request_id)


Access = Annotated[TenantAccess, Depends(tenant_access, scope="function")]
