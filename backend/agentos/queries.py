import base64
import binascii
import json
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Query
from sqlalchemy import text

from .access import RunContext, TenantAccess
from .domain import DomainError, missing

Limit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=2048)]


async def one(access: RunContext, sql: str, **params):
    row = (await access.db.execute(text(sql), {"tenant": access.tenant, **params})).mappings().one_or_none()
    if row is None:
        raise missing()
    return dict(row)


async def page(access: TenantAccess, sql: str, scope: str, limit: int, cursor: str | None, **params):
    """All internal SELECTs use the alias t and end in a tenant-scoped WHERE clause."""
    binding = [str(access.tenant), scope, {key: str(value) for key, value in params.items()}]
    if cursor is not None:
        try:
            saved, timestamp, identifier = json.loads(base64.urlsafe_b64decode(cursor))
            after = datetime.fromisoformat(timestamp)
            if saved != binding or after.utcoffset() is None:
                raise ValueError
            params.update(after=after, after_id=UUID(identifier))
        except (ValueError, TypeError, AttributeError, binascii.Error):
            raise DomainError(422, "invalid_cursor", "Invalid cursor for this list.") from None
        sql += " AND (t.created_at,t.id) < (:after,:after_id)"
    sql += " ORDER BY t.created_at DESC,t.id DESC LIMIT :count"
    rows = (await access.db.execute(text(sql), {"tenant": access.tenant, "count": limit+1, **params})).mappings().all()
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit-1]
        token = json.dumps([binding, last["created_at"].isoformat(), str(last["id"])])
        next_cursor = base64.urlsafe_b64encode(token.encode()).decode()
    return {"items": rows[:limit], "next_cursor": next_cursor}
