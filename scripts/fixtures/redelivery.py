"""Run using the disposable worker role with other consumers stopped."""
import asyncio
import json
import sys
from uuid import UUID, uuid4

from agentos.runtime import CONSUMER, STREAM, serve
from agentos.scheduler import Scheduler
from agentos.worker import execute


async def redeliver(cfg, engine, nc, stop):
    scheduler = Scheduler(engine, cfg.lease_seconds)
    worker = uuid4()
    await scheduler.register(worker, 1)
    sub = await nc.jetstream().pull_subscribe_bind(stream=STREAM, consumer=CONSUMER)
    try:
        async with asyncio.timeout(40):
            msg, = await sub.fetch(1, timeout=15)
            claim = await scheduler.claim(worker)
            assert claim and claim.run == UUID(sys.argv[1])
            sequence = msg.metadata.sequence.stream
            await execute(scheduler, claim)
            # Ask the broker to redeliver the same stored message after the result committed.
            await msg.nak()
            while True:
                again, = await sub.fetch(1, timeout=10)
                if again.metadata.sequence.stream == sequence:
                    break
                await again.ack_sync()
            assert again.metadata.num_delivered > msg.metadata.num_delivered
            assert await scheduler.claim(worker) is None
            await again.ack_sync()
            print(json.dumps({"stream_sequence": sequence, "deliveries": again.metadata.num_delivered}))
    finally:
        await sub.unsubscribe()
        await scheduler.drain(worker, stopped=True)


asyncio.run(serve(redeliver))
