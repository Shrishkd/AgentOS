"""Disposable Compose entry point. Stop the actual process at a selected durable boundary."""

import asyncio
import json
import os
import signal
import sys

import httpx

sys.path.insert(0, "/app")
from agentos import model_engine, worker
from agentos.config import Settings
from agentos.main import create_app
from agentos.providers import ChatAdapter
from agentos.publisher import publish_one
from agentos.runtime import serve


def crash():
    os.kill(os.getpid(), signal.SIGKILL)


async def interrupt_api():
    app = create_app(Settings())
    data = json.load(sys.stdin)

    async def interrupt_response(scope, receive, send):
        async def after_commit(message):
            assert message["type"] == "http.response.start" and message["status"] == 201, message
            crash()
        await app(scope, receive, after_commit)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=interrupt_response),
                                     base_url="http://localhost") as client:
            await client.post(data["path"], json=data["body"], headers=data["headers"])


async def interrupt_publisher(cfg, engine, nc, stop):
    class LoseAcknowledgement:
        async def publish(self, *args, **kwargs):
            await nc.jetstream().publish(*args, **kwargs)
            crash()

    await publish_one(engine, LoseAcknowledgement())
    raise AssertionError("Expected a pending outbox event")


async def interrupt_worker(mode):
    if mode == "database_renewal":
        run = model_engine.ModelEngine.run
        execute = worker.execute

        async def hold_committed_answer(*args):
            await run(*args)
            await asyncio.Event().wait()

        async def stop_on_storage_loss(*args):
            try:
                await execute(*args)
            finally:
                # The held operation cannot finish normally. Renewal failed or ownership expired.
                crash()
        model_engine.ModelEngine.run = hold_committed_answer
        worker.execute = stop_on_storage_loss
    elif mode == "before_dispatch":
        async def before_model(*args):
            crash()
        model_engine.ModelEngine.model_step = before_model
    elif mode == "after_provider_reply":
        complete = ChatAdapter.complete

        async def after_reply(*args):
            await complete(*args)
            crash()
        ChatAdapter.complete = after_reply
    elif mode == "after_final_checkpoint":
        async def before_finish(*args):
            crash()
        worker.Scheduler.finish = before_finish
    elif mode in {"after_tool_effect", "recover_tool_effect"}:
        async def keyed_length(operation_id, args):
            async with httpx.AsyncClient() as client:
                try:
                    response = await client.post("http://model-fixture:9000/test/length",
                        headers={"Idempotency-Key": str(operation_id)}, json={"text": args.text},
                        timeout=httpx.Timeout(5, read=0.2 if mode == "after_tool_effect" else 5))
                except httpx.ReadTimeout:
                    if mode == "after_tool_effect":
                        crash()
                    raise
            response.raise_for_status()
            reply = response.json()
            if mode == "after_tool_effect":
                assert reply["requests"] == 1
                crash()
            assert reply["requests"] == 2
            return reply["result"]
        model_engine.text_length = keyed_length
        if mode == "recover_tool_effect":
            execute = worker.execute

            async def one_run(*args):
                await execute(*args)
                os.kill(os.getpid(), signal.SIGTERM)
            worker.execute = one_run
    else:
        raise ValueError(mode)
    await serve(worker.worker_loop)


if __name__ == "__main__":
    signal.alarm(55)
    mode = sys.argv[1]
    if mode == "after_api_commit":
        asyncio.run(interrupt_api())
    elif mode == "after_publish":
        asyncio.run(serve(interrupt_publisher))
    else:
        asyncio.run(interrupt_worker(mode))
