import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy import text

from .access import Access, Credentials, scoped_access
from .contracts import Page, Run, RunInput
from .domain import DomainError, State, TERMINAL, missing
from .lifecycle import RUNS, transition
from .queries import Cursor, Limit, one, page
from .telemetry import execution_span, traceparent

router = APIRouter(prefix="/v1/tenants/{tenant_id}/runs", tags=["Runs"])


def fingerprint(body: RunInput) -> str:
    normalized = body.model_dump(mode="json")
    if body.deadline_at is not None:
        normalized["deadline_at"] = body.deadline_at.astimezone(UTC).isoformat()
    canonical = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def check_rate(request: Request, tenant: UUID, limit: int) -> None:
    # Expire the counter and read its retry interval in the same Redis operation.
    script = """
        local n = redis.call('INCR', KEYS[1])
        if n == 1 then redis.call('EXPIRE', KEYS[1], 60) end
        return {n,redis.call('TTL', KEYS[1])}
    """
    try:
        count, ttl = await request.app.state.redis.eval(script, 1, f"admission:{tenant}")
    except RedisError:
        raise DomainError(503, "limiter_unavailable", "Submission rate checks are unavailable.") from None
    if count > limit:
        raise DomainError(429, "rate_limited", "Submission rate exceeded.", {"Retry-After": str(max(1, ttl))})


@router.post("", response_model=Run, status_code=201, responses={200: {"model": Run}})
async def submit(body: RunInput, request: Request, response: Response, access: Access,
                 key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128,
                                            pattern=r"^[!-~]+$")]):
    access.require_developer()
    db = access.db
    digest = fingerprint(body)
    admission = await one(access, "SELECT * FROM prepare_submission(:tenant,:key,:id)", key=key, id=body.agent_version_id)
    if admission["existing_id"]:
        if admission["digest"] != digest:
            raise DomainError(409, "idempotency_conflict", "The key belongs to a different submission.")
        response.status_code = 200
        row = await one(access, RUNS + " AND t.id=:id", id=admission["existing_id"])
    else:
        if admission["state"] is None:
            raise missing()
        if admission["state"] != "PUBLISHED":
            raise DomainError(409, "version_unavailable", "New runs require a published version.")
        now = admission["db_now"]
        deadline = now + timedelta(seconds=admission["timeout_seconds"])
        if body.deadline_at is not None:
            deadline = min(deadline, body.deadline_at)
        if deadline <= now:
            raise DomainError(422, "deadline_elapsed", "The requested deadline has elapsed.")
        if not admission["has_room"]:
            raise DomainError(429, "quota_exceeded", "Tenant pending run quota reached.", {"Retry-After": "1"})
        run_id = uuid4()
        await check_rate(request, access.tenant, admission["rate_limit"])
        with execution_span("run.submit", root=True, run_id=str(run_id), request_id=access.request_id):
            # A single statement creates the initial state, ordered history and wakeup.
            # Returning through the CTE avoids reading our own insert with an older snapshot.
            row = (await db.execute(text("""WITH admitted AS (
                SELECT clock_timestamp() AS ts
            ), inserted AS (INSERT INTO runs (id,tenant_id,agent_version_id,submitter_id,
                submission_key,request_hash,input,priority,deadline_at,traceparent,status,revision,enqueued_at,updated_at)
                SELECT :run,:tenant,:version,:user,:key,:digest,CAST(:input AS jsonb),:priority,:deadline,:parent,
                    CASE WHEN :deadline <= ts THEN 'TIMED_OUT' ELSE 'QUEUED' END,2,
                    CASE WHEN :deadline > ts THEN ts END,ts FROM admitted RETURNING *
            ), history AS (INSERT INTO run_events(tenant_id,run_id,seq,kind,payload)
                SELECT tenant_id,id,seq,'run.state',jsonb_build_object('status',
                    CASE seq WHEN 1 THEN 'CREATED' ELSE status END)
                FROM inserted CROSS JOIN generate_series(1,2) seq
            ), wakeup AS (INSERT INTO outbox_events(tenant_id,run_id,kind)
                SELECT tenant_id,id,'run.queued' FROM inserted WHERE status='QUEUED'
            ) SELECT id,agent_version_id,submitter_id,status,revision,priority,deadline_at,
                enqueued_at,created_at,updated_at,input,output,failure_reason,retry_at FROM inserted"""),
                {"run": run_id, "tenant": access.tenant, "version": body.agent_version_id, "user": access.user,
                 "key": key, "digest": digest, "input": json.dumps(body.input), "priority": body.priority,
                 "deadline": deadline, "parent": traceparent()})).mappings().one()
    response.headers["Location"] = f"/v1/tenants/{access.tenant}/runs/{row['id']}"
    return row


@router.get("", response_model=Page[Run])
async def list_runs(access: Access, status: State | None = None, agent_id: UUID | None = None,
                    agent_version_id: UUID | None = None, limit: Limit = 50, cursor: Cursor = None,
                    search: Annotated[str, Query(min_length=1, max_length=128)] | None = None):
    sql, params = RUNS, {}
    if search:
        sql += """ AND (starts_with(t.id::text,lower(:search)) OR EXISTS (SELECT FROM agent_versions v
            JOIN agents a ON a.id=v.agent_id WHERE v.id=t.agent_version_id AND strpos(lower(a.name),lower(:search))>0))"""
        params["search"] = search
    if status is not None:
        sql += " AND t.status=:status"
        params["status"] = status.value
    if agent_version_id is not None:
        sql += " AND t.agent_version_id=:version"
        params["version"] = agent_version_id
    if agent_id is not None:
        sql += " AND t.agent_version_id IN (SELECT id FROM agent_versions WHERE agent_id=:agent)"
        params["agent"] = agent_id
    return await page(access, sql, "runs", limit, cursor, **params)


@router.get("/{run_id}", response_model=Run)
async def get_run(run_id: UUID, access: Access):
    return await one(access, RUNS + " AND t.id=:id", id=run_id)


@router.post("/{run_id}/cancel", response_model=Run)
async def cancel(run_id: UUID, access: Access, response: Response):
    access.require_developer()
    row = await one(access, RUNS + " AND t.id=:id FOR UPDATE", id=run_id)
    original = State(row["status"])
    if original == State.CANCELLED:
        return row
    if original == State.CANCEL_REQUESTED:
        response.status_code = 202
        return row
    row = await transition(access, run_id, original, State.CANCEL_REQUESTED, row["revision"])
    if row["status"] == State.CANCEL_REQUESTED:
        if original == State.RUNNING:
            response.status_code = 202
        else:
            row = await transition(access, run_id, State.CANCEL_REQUESTED, State.CANCELLED, row["revision"])
    return row


class Event(BaseModel):
    seq: int
    kind: str
    created_at: datetime
    payload: dict[str, str]


async def event_page(access, run_id, after, limit):
    return (await access.db.execute(text("""SELECT seq,kind,created_at,payload FROM run_events
        WHERE tenant_id=:tenant AND run_id=:run AND seq>:after ORDER BY seq LIMIT :limit"""),
        {"tenant": access.tenant, "run": run_id, "after": after, "limit": limit})).mappings().all()


@router.get("/{run_id}/events", response_model=list[Event],
            responses={200: {"content": {"text/event-stream": {}}}})
async def history(run_id: UUID, request: Request, access: Access, credentials: Credentials,
                  after: Annotated[int, Query(ge=0, le=2147483647)] = 0, limit: Limit = 50,
                  last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None):
    state_query = "SELECT status,revision FROM runs WHERE tenant_id=:tenant AND id=:id"
    state = await one(access, state_query, id=run_id)
    if "text/event-stream" not in request.headers.get("accept", ""):
        return await event_page(access, run_id, after, limit)
    cursor = after
    if last_event_id is not None:
        if not (last_event_id.isascii() and last_event_id.isdecimal() and len(last_event_id) <= 10):
            raise DomainError(422, "invalid_cursor", "Expected a nonnegative event ID.")
        cursor = int(last_event_id)
    if cursor > state["revision"]:
        raise DomainError(422, "invalid_cursor", "Event ID exceeds the latest revision.")
    first = await access.db.scalar(text("SELECT min(seq) FROM run_events WHERE tenant_id=:tenant AND run_id=:run"),
                                   {"tenant": access.tenant, "run": run_id})
    if first is not None and cursor < first - 1:
        raise DomainError(410, "history_expired", "The requested event history has expired.")
    tenant = access.tenant

    async def stream():
        position = cursor
        heartbeat = monotonic()
        while not await request.is_disconnected():
            try:
                async with scoped_access(request, tenant, credentials) as current:
                    state = await one(current, state_query, id=run_id)
                    rows = await event_page(current, run_id, position, limit)
            except DomainError:
                return  # Reconnect goes through normal HTTP authentication again.
            for row in rows:
                event = Event.model_validate(dict(row))
                yield f"id: {event.seq}\nevent: {event.kind}\ndata: {event.model_dump_json()}\n\n"
                position = event.seq
                heartbeat = monotonic()
            if state["status"] in TERMINAL and position >= state["revision"]:
                return
            if len(rows) < limit:
                if monotonic() - heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    heartbeat = monotonic()
                await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
