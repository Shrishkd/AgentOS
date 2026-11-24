import json
from uuid import UUID

from sqlalchemy import text

from .access import RunContext
from .domain import DomainError, State, TERMINAL, validate_transition
from .queries import one

RUNS = """SELECT t.id,t.agent_version_id,t.submitter_id,t.status,t.revision,t.priority,t.deadline_at,
    t.enqueued_at,t.created_at,t.updated_at,t.input,t.output,t.failure_reason FROM runs t WHERE t.tenant_id=:tenant"""


async def record_event(access: RunContext, run_id: UUID, seq: int, state: State) -> None:
    await access.db.execute(text("""INSERT INTO run_events (tenant_id,run_id,seq,kind,payload)
        VALUES (:tenant,:run,:seq,'run.state',CAST(:payload AS jsonb))"""),
        {"tenant": access.tenant, "run": run_id, "seq": seq, "payload": json.dumps({"status": state})})


async def transition(access: RunContext, run_id: UUID, expected: State, target: State,
                     expected_revision: int) -> dict:
    row = await one(access, RUNS + " AND t.id=:id FOR UPDATE", id=run_id)
    current = State(row["status"])
    if current != expected or row["revision"] != expected_revision:
        raise DomainError(409, "stale_run", "Run state has changed.")
    now = await access.db.scalar(text("SELECT clock_timestamp()"))
    if current not in TERMINAL and row["deadline_at"] <= now:
        target = State.TIMED_OUT
    elif target == State.TIMED_OUT:
        raise DomainError(409, "deadline_pending", "The run deadline has not elapsed.")
    validate_transition(current, target)
    revision = expected_revision + 1
    await access.db.execute(text("""UPDATE runs SET status=:status,revision=:revision,updated_at=:now,
        enqueued_at=CASE WHEN :status='QUEUED' THEN :now ELSE enqueued_at END WHERE id=:run"""),
        {"status": target.value, "revision": revision, "now": now, "run": run_id})
    await record_event(access, run_id, revision, target)
    if target == State.QUEUED:
        await access.db.execute(text("""INSERT INTO outbox_events (tenant_id,run_id,kind)
            VALUES (:tenant,:run,'run.queued')"""), {"tenant": access.tenant, "run": run_id})
    return await one(access, RUNS + " AND t.id=:id", id=run_id)
