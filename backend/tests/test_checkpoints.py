import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from agentos.checkpoints import restore, resume_boundary
from agentos.providers import ModelFailure, ToolCall


@pytest.fixture
def checkpoint():
    claim = SimpleNamespace(spec=SimpleNamespace(instructions="Count.", version=uuid4()), input={"text": "abc"})
    call = ToolCall(call_id="c1", name="text_length", arguments={"text": "abc"}).model_dump()
    step = uuid4()
    row = {"schema_version": 1, "agent_version_id": claim.spec.version, "step_id": step,
           "step_ordinal": 2, "ordinal": 2, "kind": "tool", "status": "completed", "next_ordinal": 3,
           "next_action": "model", "model_status": "completed", "model_ordinal": 1, "model_result": call,
           "proposal": call, "result": {"length": 3}, "event_kind": "run.checkpoint",
           "event_payload": {"step_id": str(step), "next_action": "model"},
           "messages": [{"role": "system", "content": "Count."}, {"role": "user", "content": '{"text":"abc"}'}]}
    return claim, row


def test_tool_boundary_restores_original_call_and_result(checkpoint):
    cursor = resume_boundary(*checkpoint)
    assert (cursor.ordinal, cursor.next_action) == (3, "model")
    assert cursor.messages[-2]["tool_calls"][0]["id"] == "c1"
    assert cursor.messages[-1] == {"role": "tool", "tool_call_id": "c1", "content": '{"length":3}'}


@pytest.mark.parametrize("change", [{"step_ordinal": 1}, {"next_ordinal": 2}, {"agent_version_id": uuid4()},
                                    {"event_kind": "run.state"}, {"proposal": {}}, {"model_status": "pending"}])
def test_damaged_boundary_requires_ledger_reconstruction(checkpoint, change):
    claim, row = checkpoint
    with pytest.raises(ValueError):
        resume_boundary(claim, {**row, **change})


def test_future_checkpoint_requires_compatible_worker(checkpoint):
    claim, row = checkpoint
    with pytest.raises(ModelFailure, match="unsupported_checkpoint_version"):
        resume_boundary(claim, {**row, "schema_version": 2})


@pytest.mark.parametrize("damage", ["legacy", "invalid_cursor", "missing_usage", "missing_step"])
def test_ledger_reconstruction_requires_contiguous_accounted_results(checkpoint, damage):
    claim, saved = checkpoint
    claim.tenant, claim.run = uuid4(), uuid4()
    boundary, ledger = Mock(), Mock()
    boundary.mappings.return_value.first.return_value = None if damage == "legacy" else {**saved, "next_ordinal": 9}
    model = {"ordinal": 1, "kind": "model", "status": "completed", "result": saved["model_result"],
             "accounted": damage != "missing_usage"}
    ledger.mappings.return_value.all.return_value = [saved] if damage == "missing_step" else [model, saved]
    db = AsyncMock()
    db.execute.side_effect = [boundary, ledger]
    if damage in {"missing_usage", "missing_step"}:
        with pytest.raises(ModelFailure, match="inconsistent_step_history"):
            asyncio.run(restore(db, claim))
    else:
        cursor = asyncio.run(restore(db, claim))
        assert cursor.ordinal == 3 and cursor.messages[-1]["tool_call_id"] == "c1"
