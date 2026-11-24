from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from .access import Access
from .contracts import Page
from .queries import Cursor, Limit, one, page

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["Workers"])


class WorkerActivity(BaseModel):
    id: UUID
    created_at: datetime
    heartbeat_at: datetime
    status: Literal["healthy", "stale", "draining", "stopped"]
    open_attempts: int


class Attempt(BaseModel):
    number: int
    worker_id: UUID
    started_at: datetime
    ended_at: datetime | None
    outcome: str | None


@router.get("/workers", response_model=Page[WorkerActivity])
async def list_workers(access: Access, limit: Limit = 50, cursor: Cursor = None):
    return await page(access, """SELECT t.id,t.created_at,t.heartbeat_at,t.open_attempts,CASE
        WHEN t.stopped_at IS NOT NULL THEN 'stopped' WHEN t.healthy_until<=clock_timestamp() THEN 'stale'
        WHEN t.draining THEN 'draining' ELSE 'healthy' END AS status
        FROM tenant_workers t WHERE t.tenant_id=:tenant""", "workers", limit, cursor)


@router.get("/runs/{run_id}/attempts", response_model=list[Attempt])
async def attempts(run_id: UUID, access: Access):
    await one(access, "SELECT id FROM runs WHERE tenant_id=:tenant AND id=:id", id=run_id)
    return (await access.db.execute(text("""SELECT number,worker_id,started_at,ended_at,outcome FROM attempts
        WHERE tenant_id=:tenant AND run_id=:run ORDER BY number"""),
        {"tenant": access.tenant, "run": run_id})).mappings().all()
