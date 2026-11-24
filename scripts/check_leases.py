"""Executed inside a disposable worker container by check_workers.py."""

import asyncio
import sys
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import create_async_engine

from agentos.runtime import RuntimeSettings
from agentos.scheduler import LeaseLost, Scheduler
from agentos.worker import receipt


async def main(high: UUID, middle: UUID, other: UUID):
    engine = create_async_engine(RuntimeSettings().database_url())
    scheduler = Scheduler(engine, lease_seconds=3)
    first, second, observer = uuid4(), uuid4(), uuid4()
    try:
        await scheduler.register(first, 1)
        await scheduler.register(second, 2)
        await scheduler.register(observer, 1)
        claim = await scheduler.claim(first)
        assert claim and claim.run == high
        assert await scheduler.claim(first) is None  # worker slot full
        peer = await scheduler.claim(second)
        assert peer and peer.run == other  # both tenants eligible; least recently served wins
        another = await scheduler.claim(second)
        assert another and another.run == middle
        assert await scheduler.claim(observer) is None  # spare worker slot, first tenant at quota
        await scheduler.drain(second)
        await scheduler.finish(peer, receipt(peer))
        await scheduler.finish(another, receipt(another))
        await asyncio.sleep(1.6)
        await scheduler.renew(claim)
        await asyncio.sleep(1.6)
        assert not await scheduler.recover_one()  # renewal extended the original deadline
        await asyncio.sleep(1.7)
        try:
            await scheduler.renew(claim)
        except LeaseLost:
            pass
        else:
            raise AssertionError("Expired lease was renewed")
        assert await scheduler.recover_one()
        assert not await scheduler.promote_one()
        await asyncio.sleep(1.1)
        assert await scheduler.promote_one()
        await scheduler.heartbeat(second)
        assert await scheduler.claim(second) is None  # draining session rejects new work
        third = uuid4()
        await scheduler.register(third, 1)
        replacement = await scheduler.claim(third)
        assert replacement and replacement.run == high and replacement.generation == claim.generation + 1
        try:
            await scheduler.finish(claim, {"stale": True})
        except LeaseLost:
            pass
        else:
            raise AssertionError("Expired generation wrote a result")
        await scheduler.finish(replacement, receipt(replacement))
        for worker in (first, second, third, observer):
            await scheduler.drain(worker, stopped=True)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(*(UUID(arg) for arg in sys.argv[1:])))
