import asyncio
import hashlib
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentos.contracts import VersionInput
from agentos.scheduler import Claim, LeaseLost
from agentos.worker import execute, receipt


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
