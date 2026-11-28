"""PostgreSQL transactions for session capacity, ownership and recovery."""

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .access import RunContext
from .domain import State, TERMINAL
from .lifecycle import transition

EXPIRY = "clock_timestamp()+make_interval(secs => :seconds)"
SUPPORTED = "(q.provider IN ('builtin','mock') OR (q.provider='openai-compatible' AND q.model=:remote))"


@dataclass(frozen=True)
class RunSpec:
    version: UUID
    provider: str
    model: str
    instructions: str
    limits: dict
    deadline: datetime


@dataclass(frozen=True)
class Claim:
    tenant: UUID
    run: UUID
    worker: UUID
    generation: int
    input: dict
    spec: RunSpec | None = None
    traceparent: str | None = None


class LeaseLost(Exception):
    """This attempt can no longer write progress."""


class RunInterrupted(Exception):
    """Cancellation or the database deadline has stopped model work."""


class RetryScheduled(Exception):
    """This claim released its slot and committed a retry time."""


class ApprovalNeeded(Exception):
    """Commit the proposal, park the run and release this attempt."""


class Scheduler:
    def __init__(self, engine: AsyncEngine, lease_seconds: int, remote_model: str | None = None):
        self.engine = engine
        self.seconds = lease_seconds
        self.remote_model = remote_model

    async def register(self, worker: UUID, capacity: int) -> None:
        async with self.engine.begin() as db:
            await db.execute(text(f"""INSERT INTO workers (id,capacity,healthy_until,remote_model)
                VALUES (:id,:capacity,{EXPIRY},:remote)"""), {"id": worker, "capacity": capacity,
                "seconds": self.seconds, "remote": self.remote_model})

    async def heartbeat(self, worker: UUID) -> None:
        async with self.engine.begin() as db:
            await db.execute(text(f"""UPDATE workers SET heartbeat_at=clock_timestamp(),healthy_until={EXPIRY}
                WHERE id=:id AND stopped_at IS NULL"""), {"id": worker, "seconds": self.seconds})

    async def drain(self, worker: UUID, stopped: bool = False) -> None:
        async with self.engine.begin() as db:
            await db.execute(text("""UPDATE workers SET draining=true,
                stopped_at=CASE WHEN :stopped THEN clock_timestamp() ELSE stopped_at END WHERE id=:id"""),
                {"id": worker, "stopped": stopped})

    async def claim(self, worker: UUID) -> Claim | None:
        async with self.engine.begin() as db:
            tenant = await db.scalar(text(f"""SELECT t.id FROM tenants t
                WHERE EXISTS (SELECT FROM execution_queue q WHERE q.tenant_id=t.id AND {SUPPORTED})
                AND (SELECT count(*) FROM worker_leases l WHERE l.tenant_id=t.id
                     AND l.expires_at>clock_timestamp()) < t.concurrency_limit
                ORDER BY t.last_admitted_at NULLS FIRST,t.id LIMIT 1 FOR NO KEY UPDATE SKIP LOCKED"""), {"remote": self.remote_model})
            if tenant is None:
                return None
            # Recheck capacity in a fresh READ COMMITTED snapshot after obtaining the tenant lock.
            params = {"tenant": tenant, "worker": worker, "seconds": self.seconds, "remote": self.remote_model}
            available = await db.scalar(text("""SELECT concurrency_limit > (SELECT count(*) FROM worker_leases
                WHERE tenant_id=:tenant AND expires_at>clock_timestamp()) FROM tenants WHERE id=:tenant"""), params)
            if not available:
                return None
            row = (await db.execute(text(f"""SELECT r.id,r.revision,r.input,r.deadline_at,r.traceparent,v.id AS version,
                v.provider,v.model,v.instructions,v.limits FROM runs r JOIN execution_queue q ON q.id=r.id
                JOIN agent_versions v ON v.id=r.agent_version_id WHERE q.tenant_id=:tenant AND r.status='QUEUED'
                AND {SUPPORTED} ORDER BY q.priority DESC,q.enqueued_at,q.id
                LIMIT 1 FOR UPDATE OF r SKIP LOCKED"""), params)).mappings().one_or_none()
            if row is None:
                return None
            capacity = await db.scalar(text("""SELECT capacity FROM workers WHERE id=:worker AND NOT draining
                AND healthy_until>clock_timestamp() FOR UPDATE"""), params)
            if capacity is None:
                return None
            used = await db.scalar(text("SELECT count(*) FROM worker_leases WHERE owner_id=:worker AND expires_at>clock_timestamp()"), params)
            if used >= capacity:
                return None
            run_id = row["id"]
            result = await transition(RunContext(db, tenant), run_id, State.QUEUED, State.RUNNING, row["revision"])
            if result["status"] != State.RUNNING:
                return None
            params["run"] = run_id
            generation = await db.scalar(text(f"""INSERT INTO worker_leases (tenant_id,run_id,owner_id,generation,expires_at)
                VALUES (:tenant,:run,:worker,1,{EXPIRY}) ON CONFLICT (run_id) DO UPDATE SET
                owner_id=excluded.owner_id,generation=worker_leases.generation+1,
                renewed_at=clock_timestamp(),expires_at=excluded.expires_at RETURNING generation"""), params)
            await db.execute(text("""INSERT INTO attempts (tenant_id,run_id,worker_id,number)
                VALUES (:tenant,:run,:worker,:generation)"""), {**params, "generation": generation})
            await db.execute(text("UPDATE tenants SET last_admitted_at=clock_timestamp() WHERE id=:tenant"), params)
            spec = RunSpec(row["version"], row["provider"], row["model"], row["instructions"], row["limits"], row["deadline_at"])
            return Claim(tenant, run_id, worker, generation, row["input"], spec, row["traceparent"])

    async def _locked(self, db: AsyncConnection, claim: Claim):
        params = {"run": claim.run, "worker": claim.worker, "generation": claim.generation}
        row = (await db.execute(text("""SELECT r.status,r.revision,r.deadline_at<=clock_timestamp() AS due
            FROM runs r WHERE r.id=:run FOR UPDATE"""), params)).mappings().one()
        owner = await db.scalar(text("""SELECT id FROM worker_leases WHERE run_id=:run AND owner_id=:worker
            AND generation=:generation AND expires_at>clock_timestamp() FOR UPDATE"""), params)
        if owner is None:
            raise LeaseLost()
        return row, params

    async def renew(self, claim: Claim) -> bool:
        async with self.engine.begin() as db:
            row, params = await self._locked(db, claim)
            if row["status"] != State.RUNNING or row["due"]:
                await self._finish(db, claim, row, params, None)
                return False
            renewed = await db.scalar(text(f"""UPDATE worker_leases SET renewed_at=clock_timestamp(),expires_at={EXPIRY}
                WHERE run_id=:run AND expires_at>clock_timestamp() RETURNING id"""), {**params, "seconds": self.seconds})
            if renewed is None:
                raise LeaseLost()
            return True

    async def finish(self, claim: Claim, output: dict) -> None:
        async with self.engine.begin() as db:
            row, params = await self._locked(db, claim)
            await self._finish(db, claim, row, params, output)

    async def fail(self, claim: Claim, code: str) -> None:
        async with self.engine.begin() as db:
            row, params = await self._locked(db, claim)
            await self._finish(db, claim, row, params, None, code)

    @asynccontextmanager
    async def progress(self, claim: Claim, retry_delay: float | None = None, *, retry_outcome="provider_retry"):
        """A short fenced transaction. No provider I/O is allowed while it is open."""
        async with self.engine.begin() as db:
            row, params = await self._locked(db, claim)
            if row["status"] != State.RUNNING or row["due"]:
                raise RunInterrupted()
            try:
                yield db
            except ApprovalNeeded:
                state = await transition(RunContext(db, claim.tenant), claim.run, State.RUNNING,
                                         State.WAITING_FOR_APPROVAL, row["revision"])
                await self._close_attempt(db, params, state["status"])
                await self._release(db, params)
            else:
                await self._end_progress(db, claim, row, params, retry_delay, retry_outcome)
                return
        raise RetryScheduled()  # The waiting state is durable before the execution loop exits.

    async def _end_progress(self, db, claim, row, params, retry_delay, retry_outcome):
        if retry_delay is not None:
            outcome = await self._wait(db, claim.tenant, claim.run, row["revision"], retry_delay)
            await self._close_attempt(db, params, retry_outcome if outcome == State.RETRY_WAIT else outcome)
            await self._release(db, params)
            return
        valid = await db.scalar(text("""SELECT expires_at>clock_timestamp() FROM worker_leases
            WHERE run_id=:run AND owner_id=:worker AND generation=:generation"""), params)
        if not valid:
            raise LeaseLost()

    async def _finish(self, db, claim, row, params, output, failure=None):
        current = State(row["status"])
        if current in TERMINAL:
            outcome = current
        else:
            target = State.FAILED if failure else State.SUCCEEDED
            if current == State.CANCEL_REQUESTED:
                target = State.CANCELLED
            result = await transition(RunContext(db, claim.tenant), claim.run, current, target, row["revision"])
            outcome = result["status"]
            if outcome == State.SUCCEEDED:
                await db.execute(text("UPDATE runs SET output=CAST(:payload AS jsonb) WHERE id=:run"),
                                 {**params, "payload": json.dumps(output)})
            elif outcome == State.FAILED:
                await db.execute(text("UPDATE runs SET failure_reason=:code WHERE id=:run"), {**params, "code": failure})
        if outcome != State.SUCCEEDED:
            await self._abandon(db, params, failure or str(outcome))
        await self._close_attempt(db, params, outcome)
        await self._release(db, params)

    @staticmethod
    async def _release(db, params):
        # Final fence in the same transaction as output, history and release.
        released = await db.scalar(text("""UPDATE worker_leases SET owner_id=NULL,expires_at=NULL
            WHERE run_id=:run AND owner_id=:worker AND generation=:generation AND expires_at>clock_timestamp()
            RETURNING id"""), params)
        if released is None:
            raise LeaseLost()

    @staticmethod
    async def _abandon(db, params, code):
        await db.execute(text("""UPDATE usage_records SET status='unknown',error_code=:code
            WHERE run_id=:run AND status='reserved'"""), {**params, "code": code})
        await db.execute(text("""UPDATE steps SET status='failed',ended_at=clock_timestamp(),error_code=:code
            WHERE run_id=:run AND status='pending'"""), {**params, "code": code})

    @staticmethod
    async def _close_attempt(db, params, outcome):
        await db.execute(text("""UPDATE attempts SET ended_at=clock_timestamp(),outcome=:outcome
            WHERE run_id=:run AND number=:generation AND ended_at IS NULL"""), {**params, "outcome": outcome})

    @staticmethod
    async def _wait(db, tenant, run, revision, delay):
        result = await transition(RunContext(db, tenant), run, State.RUNNING, State.RETRY_WAIT, revision)
        if result["status"] == State.RETRY_WAIT:
            await db.execute(text("""UPDATE runs SET retry_at=least(deadline_at,
                clock_timestamp()+make_interval(secs => :delay)) WHERE id=:run"""), {"run": run, "delay": delay})
        return result["status"]

    async def promote_one(self) -> bool:
        """Expire unowned work or promote a due retry and enqueue it atomically."""
        async with self.engine.begin() as db:
            row = (await db.execute(text("""SELECT id,tenant_id,status,revision,
                deadline_at<=clock_timestamp() AS due FROM runs
                WHERE (status='RETRY_WAIT' AND retry_at<=clock_timestamp()) OR
                (status IN ('QUEUED','RETRY_WAIT','WAITING_FOR_APPROVAL') AND deadline_at<=clock_timestamp()) OR
                (status='WAITING_FOR_APPROVAL' AND EXISTS (SELECT FROM approvals a WHERE a.run_id=runs.id
                    AND a.status='pending' AND a.expires_at<=clock_timestamp()))
                ORDER BY least(retry_at,deadline_at),id LIMIT 1 FOR UPDATE SKIP LOCKED"""))).mappings().one_or_none()
            if row is None:
                return False
            target = State.TIMED_OUT if row["due"] else State.QUEUED
            if row["status"] == State.WAITING_FOR_APPROVAL and not row["due"]:
                target = State.FAILED
                await db.execute(text("UPDATE runs SET failure_reason='approval_expired' WHERE id=:id"), {"id": row["id"]})
            await transition(RunContext(db, row["tenant_id"]), row["id"], State(row["status"]), target, row["revision"])
            return True

    async def recover_one(self) -> bool:
        async with self.engine.begin() as db:
            row = (await db.execute(text("""SELECT r.id,r.tenant_id,r.status,r.revision,l.generation
                FROM runs r JOIN worker_leases l ON l.run_id=r.id WHERE l.owner_id IS NOT NULL
                AND l.expires_at<=clock_timestamp() ORDER BY l.expires_at LIMIT 1 FOR UPDATE OF r SKIP LOCKED"""))).mappings().one_or_none()
            if row is None:
                return False
            params = {"run": row["id"], "generation": row["generation"]}
            released = await db.scalar(text("""UPDATE worker_leases SET owner_id=NULL,expires_at=NULL
                WHERE run_id=:run AND generation=:generation AND expires_at<=clock_timestamp() RETURNING id"""), params)
            if released is None:
                return False
            await self._close_attempt(db, params, "lease_expired")
            current = State(row["status"])
            if current in TERMINAL:
                await self._abandon(db, params, str(current))
                return True
            ctx = RunContext(db, row["tenant_id"])
            crashes = await db.scalar(text("""SELECT count(*) FROM attempts
                WHERE run_id=:run AND outcome='lease_expired'"""), params)
            if current == State.CANCEL_REQUESTED:
                target = State.CANCELLED
            elif crashes >= 3:
                target = State.FAILED
            else:
                await self._wait(db, row["tenant_id"], row["id"], row["revision"], 2 ** (crashes-1))
                return True
            result = await transition(ctx, row["id"], current, target, row["revision"])
            if result["status"] == State.FAILED:
                await db.execute(text("UPDATE runs SET failure_reason='lease_attempts_exhausted' WHERE id=:run"), params)
            if result["status"] in TERMINAL:
                await self._abandon(db, params, str(result["status"]))
            return True
