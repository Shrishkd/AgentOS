import asyncio
import hashlib
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentos.contracts import VersionInput
from agentos.scheduler import Claim, LeaseLost
from agentos.worker import execute, receipt, slot


def test_receipt_identifies_attempt_and_hashes_canonical_utf8():
    claim = Claim(uuid4(), uuid4(), uuid4(), 2, {"z": 1, "a": "café"})
    expected = hashlib.sha256('{"a":"café","z":1}'.encode()).hexdigest()
    assert receipt(claim) == {"handler": "builtin/receipt", "input_sha256": expected,
                              "worker_id": str(claim.worker), "attempt": 2}


def test_builtin_version_requires_receipt_and_empty_grants():
    base = {"provider": "builtin", "model": "receipt", "instructions": "Make a receipt",
            "limits": {"max_steps": 1, "max_tokens": 1, "timeout_seconds": 10}}
    VersionInput(**base)
    for change in ({"model": "other"}, {"tool_version_ids": [uuid4()]}):
        with pytest.raises(ValidationError):
            VersionInput(**{**base, **change})


def test_one_wakeup_drains_backlog_and_stop_prevents_another_claim():
    async def check():
        stop = asyncio.Event()
        worker, tenant = uuid4(), uuid4()
        claims = [Claim(tenant, uuid4(), worker, 1, {}) for _ in range(2)]
        scheduler = AsyncMock()
        scheduler.seconds = 5
        scheduler.claim.side_effect = claims

        async def finish(claim, output):
            assert output["worker_id"] == str(worker)
            if claim == claims[-1]:
                stop.set()

        scheduler.finish.side_effect = finish
        nc, js, subscription, wakeup = Mock(is_connected=True), AsyncMock(), AsyncMock(), AsyncMock()
        nc.jetstream.return_value = js
        js.pull_subscribe_bind.return_value = subscription
        subscription.fetch.side_effect = [[wakeup]]
        await asyncio.wait_for(slot(nc, scheduler, worker, stop, None), timeout=2)
        subscription.fetch.assert_awaited_once()
        wakeup.ack.assert_awaited_once()
        assert scheduler.claim.await_count == scheduler.finish.await_count == 2
        subscription.unsubscribe.assert_awaited_once()

    asyncio.run(check())


@pytest.mark.parametrize("lost", [True, False])
def test_unfinished_work_stops_when_renewal_fails_or_cancellation_completes(monkeypatch, lost):
    async def blocked(*_):
        await asyncio.Event().wait()

    monkeypatch.setattr("agentos.worker.asyncio.to_thread", blocked)
    scheduler = AsyncMock()
    scheduler.seconds = 0.01
    scheduler.renew.return_value = False
    if lost:
        scheduler.renew.side_effect = LeaseLost()
        with pytest.raises(LeaseLost):
            asyncio.run(execute(scheduler, Claim(uuid4(), uuid4(), uuid4(), 1, {})))
    else:
        asyncio.run(execute(scheduler, Claim(uuid4(), uuid4(), uuid4(), 1, {})))
    scheduler.renew.assert_awaited_once()
    scheduler.finish.assert_not_awaited()
