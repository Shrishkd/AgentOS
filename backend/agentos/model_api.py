from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel, JsonValue
from sqlalchemy import text

from .access import Access
from .contracts import Page
from .queries import Cursor, Limit, one, page

router = APIRouter(prefix="/v1/tenants/{tenant_id}/runs/{run_id}", tags=["Model execution"])


class Step(BaseModel):
    id: UUID
    ordinal: int
    kind: Literal["model", "tool"]
    status: str
    result: JsonValue
    error_code: str | None
    tool_version_id: UUID | None
    dispatch_count: int
    last_dispatch_error: str | None
    created_at: datetime
    ended_at: datetime | None


class Invocation(BaseModel):
    id: UUID
    step_id: UUID
    invocation: int
    provider: str
    model: str
    status: str
    reserved_input: int
    reserved_output: int
    reserved_cost: Decimal | None
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: Decimal | None
    latency_ms: int | None
    provider_request_id: str | None
    error_code: str | None
    created_at: datetime


class Checkpoint(BaseModel):
    id: UUID
    agent_version_id: UUID
    step_id: UUID
    ordinal: int
    schema_version: int
    next_action: str
    next_ordinal: int
    event_seq: int
    created_at: datetime


@router.get("/checkpoints", response_model=list[Checkpoint])
async def checkpoints(run_id: UUID, access: Access, after: Annotated[int, Query(ge=0)] = 0,
                      after_event: Annotated[int, Query(ge=0)] = 0, limit: Limit = 50):
    await one(access, "SELECT id FROM runs WHERE tenant_id=:tenant AND id=:id", id=run_id)
    return (await access.db.execute(text("""SELECT * FROM (
        SELECT id,agent_version_id,step_id,ordinal,schema_version,next_action,next_ordinal,event_seq,created_at
        FROM checkpoints WHERE tenant_id=:tenant AND run_id=:run UNION ALL
        SELECT e.id,r.agent_version_id,a.step_id,s.ordinal,2,
            CASE WHEN e.seq=a.wait_seq THEN 'await_decision' WHEN a.status='approved' THEN 'authorize_tool'
                 ELSE 'stop' END,s.ordinal,e.seq,e.created_at
        FROM approvals a JOIN runs r ON r.id=a.run_id JOIN steps s ON s.id=a.step_id
        JOIN run_events e ON e.run_id=a.run_id AND e.seq IN (a.wait_seq,a.resolution_seq)
        WHERE a.tenant_id=:tenant AND a.run_id=:run) AS boundaries
        WHERE ordinal>:after AND event_seq>:after_event ORDER BY event_seq LIMIT :limit"""),
        {"tenant": access.tenant, "run": run_id, "after": after, "after_event": after_event,
         "limit": limit})).mappings().all()


@router.get("/steps", response_model=list[Step])
async def steps(run_id: UUID, access: Access, after: Annotated[int, Query(ge=0)] = 0, limit: Limit = 50):
    await one(access, "SELECT id FROM runs WHERE tenant_id=:tenant AND id=:id", id=run_id)
    return (await access.db.execute(text("""SELECT id,ordinal,kind,status,result,error_code,tool_version_id,dispatch_count,last_dispatch_error,
        created_at,ended_at FROM steps WHERE tenant_id=:tenant AND run_id=:run AND ordinal>:after
        ORDER BY ordinal LIMIT :limit"""), {"tenant": access.tenant, "run": run_id, "after": after,
                                           "limit": limit})).mappings().all()


@router.get("/usage", response_model=Page[Invocation])
async def usage(run_id: UUID, access: Access, limit: Limit = 50, cursor: Cursor = None):
    await one(access, "SELECT id FROM runs WHERE tenant_id=:tenant AND id=:id", id=run_id)
    return await page(access, """SELECT t.* FROM usage_records t WHERE t.tenant_id=:tenant AND t.run_id=:run""",
                      "run_usage", limit, cursor, run=run_id)
