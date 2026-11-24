"""Persist a continuation alongside its step and restore the pinned conversation."""

from dataclasses import dataclass

from sqlalchemy import text

from .providers import ACTION, Final, ModelFailure, ToolCall, compact


@dataclass
class Continuation:
    messages: list[dict]
    ordinal: int = 1
    action: Final | ToolCall | None = None

    @property
    def next_action(self):
        if isinstance(self.action, Final):
            return "finish"
        return "tool" if self.action else "model"

    def applied_tool(self, result):
        call = self.action
        self.messages.extend([{"role": "assistant", "content": None, "tool_calls": [
            {"id": call.call_id, "type": "function", "function": {
                "name": call.name, "arguments": compact(call.arguments)}}]},
            {"role": "tool", "tool_call_id": call.call_id, "content": compact(result)}])
        self.action = None


async def save(db, claim, row, next_action):
    params = {"tenant": claim.tenant, "run": claim.run, "version": claim.spec.version,
              "step": row["id"], "ordinal": row["ordinal"], "next": next_action}
    params["seq"] = await db.scalar(text("""UPDATE runs SET revision=revision+1,updated_at=clock_timestamp()
        WHERE id=:run RETURNING revision"""), params)
    params["payload"] = compact({"step_id": str(row["id"]), "next_action": next_action})
    await db.execute(text("""INSERT INTO run_events(tenant_id,run_id,seq,kind,payload)
        VALUES (:tenant,:run,:seq,'run.checkpoint',CAST(:payload AS jsonb))"""), params)
    await db.execute(text("""INSERT INTO checkpoints(tenant_id,run_id,agent_version_id,step_id,ordinal,
        schema_version,next_action,next_ordinal,event_seq)
        VALUES (:tenant,:run,:version,:step,:ordinal,1,:next,:ordinal+1,:seq)"""), params)


def initial(claim):
    return Continuation([{"role": "system", "content": claim.spec.instructions},
                         {"role": "user", "content": compact(claim.input)}])


def apply_result(cursor, row):
    if row["ordinal"] != cursor.ordinal or row["kind"] != cursor.next_action or row["status"] != "completed":
        raise ValueError("Inconsistent step order")
    if row["kind"] == "model":
        cursor.action = ACTION.validate_python(row["result"])
    else:
        result = row["result"]
        if (row["proposal"] != cursor.action.model_dump() or not isinstance(result, dict)
                or set(result) != {"length"} or type(result["length"]) is not int or result["length"] < 0):
            raise ValueError("Inconsistent tool result")
        cursor.applied_tool(row["result"])
    cursor.ordinal += 1


def resume_boundary(claim, boundary):
    if boundary["schema_version"] != 1:
        raise ModelFailure("unsupported_checkpoint_version")
    cursor = initial(claim)
    model_ordinal = boundary["model_ordinal"]
    messages = boundary["messages"]
    if (boundary["agent_version_id"] != claim.spec.version or boundary["model_status"] != "completed"
            or boundary["ordinal"] != boundary["step_ordinal"] or not isinstance(messages, list)
            or messages[:2] != cursor.messages or len(messages) != model_ordinal + 1
            or model_ordinal % 2 != 1 or boundary["event_kind"] != "run.checkpoint"
            or boundary["event_payload"] != {"step_id": str(boundary["step_id"]), "next_action": boundary["next_action"]}):
        raise ValueError("Invalid checkpoint references")
    cursor.messages, cursor.ordinal = list(messages), model_ordinal
    apply_result(cursor, {"ordinal": model_ordinal, "kind": "model", "status": "completed",
                          "result": boundary["model_result"]})
    if boundary["kind"] == "tool":
        apply_result(cursor, boundary)
    if cursor.ordinal != boundary["next_ordinal"] or cursor.next_action != boundary["next_action"]:
        raise ValueError("Invalid continuation")
    return cursor


async def restore(db, claim):
    params = {"run": claim.run, "tenant": claim.tenant}
    # Fetch only the most recent prompt and its referenced action. Large transcripts stay in steps.
    boundary = (await db.execute(text("""SELECT c.*,s.ordinal AS step_ordinal,s.kind,s.status,s.result,
        CASE WHEN s.kind='tool' THEN s.request END AS proposal, m.ordinal AS model_ordinal,
        m.status AS model_status,m.request->'messages' AS messages,m.result AS model_result,
        e.kind AS event_kind,e.payload AS event_payload FROM checkpoints c
        JOIN steps s ON s.id=c.step_id JOIN run_events e ON e.run_id=c.run_id AND e.seq=c.event_seq
        LEFT JOIN steps m ON m.run_id=c.run_id AND m.kind='model'
          AND m.ordinal=CASE WHEN s.kind='tool' THEN s.ordinal-1 ELSE s.ordinal END
        WHERE c.run_id=:run AND c.tenant_id=:tenant ORDER BY c.ordinal DESC LIMIT 1"""), params)).mappings().first()
    cursor = initial(claim)
    if boundary:
        try:
            cursor = resume_boundary(claim, boundary)
        except (KeyError, TypeError, ValueError, AttributeError):
            # A supported but damaged cursor can be rebuilt only from a fully accounted ledger.
            cursor = initial(claim)
    rows = (await db.execute(text("""SELECT s.ordinal,s.kind,s.status,s.result,
        CASE WHEN s.kind='tool' THEN s.request END AS proposal,
        EXISTS (SELECT FROM usage_records u WHERE u.step_id=s.id AND u.status='reported'
                AND u.error_code IS NULL) AS accounted
        FROM steps s WHERE s.run_id=:run AND s.tenant_id=:tenant AND s.ordinal>=:start ORDER BY s.ordinal"""),
        {**params, "start": cursor.ordinal})).mappings().all()
    try:
        for i, row in enumerate(rows):
            if row["ordinal"] != cursor.ordinal or row["kind"] != cursor.next_action:
                raise ValueError("Unexpected step")
            if row["status"] != "completed":
                if i != len(rows)-1:
                    raise ValueError("Incomplete step in history")
                break
            if row["kind"] == "model" and not row["accounted"]:
                raise ValueError("Missing model usage")
            apply_result(cursor, row)
        if boundary and cursor.ordinal <= boundary["ordinal"]:
            raise ValueError("Missing checkpoint step")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ModelFailure("inconsistent_step_history") from None
    return cursor
