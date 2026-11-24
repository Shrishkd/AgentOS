import asyncio
import json
from time import monotonic
from uuid import uuid4

from nats.js.api import AckPolicy, ConsumerConfig, RetentionPolicy, StorageType, StreamConfig
from sqlalchemy import text

from .runtime import CONSUMER, STREAM, SUBJECT, log_error, pause, serve


async def publish_one(engine, js) -> bool:
    token = uuid4()
    async with engine.begin() as db:
        event = await db.scalar(text("""WITH pending AS (
            SELECT id FROM outbox_events WHERE published_at IS NULL AND available_at<=clock_timestamp()
            AND (claim_until IS NULL OR claim_until<=clock_timestamp()) ORDER BY available_at,id
            LIMIT 1 FOR UPDATE SKIP LOCKED)
            UPDATE outbox_events e SET publish_token=:token,claim_until=clock_timestamp()+interval '5 seconds',
            publish_attempts=publish_attempts+1 FROM pending WHERE e.id=pending.id RETURNING e.id"""), {"token": token})
    if event is None:
        return False
    await js.publish(SUBJECT, json.dumps({"schema": 1, "event_id": str(event)}).encode(),
                     headers={"Nats-Msg-Id": str(event)}, timeout=2)
    async with engine.begin() as db:
        await db.execute(text("""UPDATE outbox_events SET published_at=clock_timestamp(),claim_until=NULL,publish_token=NULL
            WHERE id=:event AND publish_token=:token"""), {"event": event, "token": token})
    return True


async def publish_loop(cfg, engine, nc, stop):
    js = nc.jetstream(timeout=2)
    configured = False
    next_scan = 0
    while not stop.is_set():
        if nc.is_closed:
            raise ConnectionError("Broker reconnect attempts exhausted")
        try:
            if not configured:
                await js.add_stream(config=StreamConfig(name=STREAM, subjects=[SUBJECT], storage=StorageType.FILE,
                    retention=RetentionPolicy.WORK_QUEUE, max_age=86400, max_msgs=100000))
                await js.add_consumer(STREAM, config=ConsumerConfig(durable_name=CONSUMER,
                    ack_policy=AckPolicy.EXPLICIT, ack_wait=10, filter_subject=SUBJECT))
                configured = True
            published = await publish_one(engine, js)
            if monotonic() >= next_scan:
                async with engine.connect() as db:
                    needed = await db.scalar(text("SELECT needed FROM wakeup_needed"))
                if needed:
                    await js.publish(SUBJECT, b'{"schema":1,"repair":true}', timeout=2)
                next_scan = monotonic() + 2
            if not published:
                await pause(stop, 0.2)
        except Exception as exc:
            log_error("publish_retry", exc)
            configured = False
            await pause(stop, 1)


if __name__ == "__main__":
    asyncio.run(serve(publish_loop))
