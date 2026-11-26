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
    active_runs: list[UUID]
    last_error: str | None
    last_error_at: datetime | None


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
        WHEN t.draining THEN 'draining' ELSE 'healthy' END AS status,
        ARRAY(SELECT a.run_id FROM attempts a WHERE a.worker_id=t.id AND a.tenant_id=:tenant
            AND a.ended_at IS NULL ORDER BY a.started_at) AS active_runs,
        e.reason AS last_error,e.ended_at AS last_error_at
        FROM tenant_workers t LEFT JOIN LATERAL (SELECT CASE WHEN a.outcome='FAILED'
            THEN coalesce(r.failure_reason,a.outcome) ELSE a.outcome END AS reason,a.ended_at
            FROM attempts a JOIN runs r ON r.id=a.run_id WHERE a.worker_id=t.id AND a.tenant_id=:tenant
            AND a.outcome IN ('FAILED','TIMED_OUT','lease_expired','tool_retry','provider_retry')
            ORDER BY a.ended_at DESC LIMIT 1) e ON true WHERE t.tenant_id=:tenant""", "workers", limit, cursor)


@router.get("/runs/{run_id}/attempts", response_model=list[Attempt])
async def attempts(run_id: UUID, access: Access):
    await one(access, "SELECT id FROM runs WHERE tenant_id=:tenant AND id=:id", id=run_id)
    return (await access.db.execute(text("""SELECT number,worker_id,started_at,ended_at,outcome FROM attempts
        WHERE tenant_id=:tenant AND run_id=:run ORDER BY number"""),
        {"tenant": access.tenant, "run": run_id})).mappings().all()
