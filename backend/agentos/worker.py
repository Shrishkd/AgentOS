import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

from nats.errors import TimeoutError as FetchTimeout
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .runtime import CONSUMER, STREAM, RuntimeSettings, log_error, pause, serve
from .scheduler import Claim, LeaseLost, Scheduler

SESSION_FILE = Path("/tmp/agentos-worker-id")


def receipt(claim: Claim) -> dict:
    payload = json.dumps(claim.input, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {"handler": "builtin/receipt", "input_sha256": hashlib.sha256(payload).hexdigest(),
            "worker_id": str(claim.worker), "attempt": claim.generation}


async def execute(scheduler: Scheduler, claim: Claim):
    work = asyncio.create_task(asyncio.to_thread(receipt, claim))
    try:
        while not work.done():
            done, _ = await asyncio.wait({work}, timeout=scheduler.seconds / 4)
            if not done and not await scheduler.renew(claim):
                return
        await scheduler.finish(claim, work.result())
    finally:
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


async def slot(js, scheduler, worker, stop):
    sub = await js.pull_subscribe_bind(stream=STREAM, consumer=CONSUMER)
    try:
        while not stop.is_set():
            try:
                try:
                    messages = await sub.fetch(1, timeout=1)
                except FetchTimeout:
                    continue
                msg = messages[0]
                if stop.is_set():
                    await msg.nak()
                    break
                claim = await scheduler.claim(worker)
                # A failed database transaction leaves this delivery unacknowledged.
                await msg.ack()
                if claim:
                    await execute(scheduler, claim)
            except LeaseLost as exc:
                log_error("worker_fenced", exc)
            except Exception as exc:
                log_error("worker_slot_retry", exc)
                await pause(stop, 1)
    finally:
        await sub.unsubscribe()


async def worker_loop(cfg, engine, nc, stop):
    scheduler = Scheduler(engine, cfg.lease_seconds)
    worker = uuid4()
    await scheduler.register(worker, cfg.worker_capacity)
    SESSION_FILE.write_text(str(worker))
    tasks = [asyncio.create_task(slot(nc.jetstream(), scheduler, worker, stop)) for _ in range(cfg.worker_capacity)]
    try:
        while not stop.is_set():
            if nc.is_closed or any(task.done() for task in tasks):
                raise RuntimeError("Worker consumer unavailable")
            try:
                await scheduler.heartbeat(worker)
                for _ in range(8):
                    if not await scheduler.recover_one():
                        break
            except Exception as exc:
                log_error("worker_maintenance_retry", exc)
            await pause(stop, min(2, cfg.lease_seconds / 4))
    finally:
        stop.set()
        try:
            await scheduler.drain(worker)
            await asyncio.wait(tasks, timeout=8)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await scheduler.drain(worker, stopped=True)


async def healthy():
    cfg = RuntimeSettings()
    engine = create_async_engine(cfg.database_url())
    try:
        async with engine.connect() as db:
            return await db.scalar(text("""SELECT NOT draining AND healthy_until>clock_timestamp()
                FROM workers WHERE id=:id"""), {"id": UUID(SESSION_FILE.read_text())})
    finally:
        await engine.dispose()


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["health"]:
        raise SystemExit(0 if asyncio.run(healthy()) else 1)
    asyncio.run(serve(worker_loop))
