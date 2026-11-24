"""PostgreSQL transactions for session capacity, ownership and recovery."""

import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .access import RunContext
from .domain import State, TERMINAL
from .lifecycle import transition

EXPIRY = "clock_timestamp()+make_interval(secs => :seconds)"


@dataclass(frozen=True)
class Claim:
    tenant: UUID
    run: UUID
    worker: UUID
    generation: int
    input: dict


class LeaseLost(Exception):
    """This attempt can no longer write progress."""


class Scheduler:
    def __init__(self, engine: AsyncEngine, lease_seconds: int):
        self.engine = engine
        self.seconds = lease_seconds

    async def register(self, worker: UUID, capacity: int) -> None:
        async with self.engine.begin() as db:
            await db.execute(text(f"""INSERT INTO workers (id,capacity,healthy_until)
                VALUES (:id,:capacity,{EXPIRY})"""), {"id": worker, "capacity": capacity, "seconds": self.seconds})

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
            tenant = await db.scalar(text("""SELECT t.id FROM tenants t
                WHERE EXISTS (SELECT FROM receipt_queue q WHERE q.tenant_id=t.id)
                AND (SELECT count(*) FROM worker_leases l WHERE l.tenant_id=t.id
                     AND l.expires_at>clock_timestamp()) < t.concurrency_limit
                ORDER BY t.last_admitted_at NULLS FIRST,t.id LIMIT 1 FOR UPDATE SKIP LOCKED"""))
            if tenant is None:
                return None
            # Recheck capacity in a fresh READ COMMITTED snapshot after obtaining the tenant lock.
            params = {"tenant": tenant, "worker": worker, "seconds": self.seconds}
            available = await db.scalar(text("""SELECT concurrency_limit > (SELECT count(*) FROM worker_leases
                WHERE tenant_id=:tenant AND expires_at>clock_timestamp()) FROM tenants WHERE id=:tenant"""), params)
            if not available:
                return None
            row = (await db.execute(text("""SELECT r.id,r.revision,r.input FROM runs r JOIN receipt_queue q ON q.id=r.id
                WHERE q.tenant_id=:tenant AND r.status='QUEUED' ORDER BY q.priority DESC,q.enqueued_at,q.id
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
            return Claim(tenant, run_id, worker, generation, row["input"])

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

    async def _finish(self, db, claim, row, params, output):
        current = State(row["status"])
        if current in TERMINAL:
            outcome = current
        else:
            target = State.CANCELLED if current == State.CANCEL_REQUESTED else State.SUCCEEDED
            result = await transition(RunContext(db, claim.tenant), claim.run, current, target, row["revision"])
            outcome = result["status"]
            if outcome == State.SUCCEEDED:
                await db.execute(text("UPDATE runs SET output=CAST(:payload AS jsonb) WHERE id=:run"),
                                 {**params, "payload": json.dumps(output)})
        await self._close_attempt(db, params, outcome)
        # Final fence in the same transaction as output, history and release.
        released = await db.scalar(text("""UPDATE worker_leases SET owner_id=NULL,expires_at=NULL
            WHERE run_id=:run AND owner_id=:worker AND generation=:generation AND expires_at>clock_timestamp()
            RETURNING id"""), params)
        if released is None:
            raise LeaseLost()

    @staticmethod
    async def _close_attempt(db, params, outcome):
        await db.execute(text("""UPDATE attempts SET ended_at=clock_timestamp(),outcome=:outcome
            WHERE run_id=:run AND number=:generation AND ended_at IS NULL"""), {**params, "outcome": outcome})

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
                return True
            ctx = RunContext(db, row["tenant_id"])
            if current == State.CANCEL_REQUESTED:
                target = State.CANCELLED
            elif row["generation"] >= 3:
                target = State.FAILED
            else:
                target = State.RETRY_WAIT
            result = await transition(ctx, row["id"], current, target, row["revision"])
            if result["status"] == State.RETRY_WAIT:
                await transition(ctx, row["id"], State.RETRY_WAIT, State.QUEUED, result["revision"])
            elif result["status"] == State.FAILED:
                await db.execute(text("UPDATE runs SET failure_reason='lease_attempts_exhausted' WHERE id=:run"), params)
            return True
