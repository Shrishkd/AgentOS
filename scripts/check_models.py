from decimal import Decimal
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from check_registry import request_json
from check_workers import wait_for


def check_models(base, run_path, token, compose, sql):
    root = run_path.split("/runs/")[0]

    def call(path, method="GET", body=None):
        status, data, _ = request_json(base+root+path, method, body, token, str(uuid4()))
        assert status in (200, 201), (path, status, data)
        return data

    company = root.rsplit("/", 1)[1]
    sql(f"UPDATE tenants SET pending_limit=100 WHERE id='{company}'")
    tool = compose("run", "--rm", "--no-deps", "-T", "operator", "install-text-length", "--tenant", "first", capture=True)
    agent = call("/agents", "POST", {"name": "Model integration"})
    versions = "/agents/"+agent["id"]+"/versions"

    def publish(provider, grant=True, **limits):
        version = call(versions, "POST", {"instructions": "Count the requested text.", "provider": provider,
            "model": "deterministic" if provider == "mock" else "fixture-model",
            "tool_version_ids": [tool] if grant else [],
            "limits": {"max_tokens": 8000, "max_steps": 5, "timeout_seconds": 90, **limits}})
        call(versions+"/"+version["id"]+"/publish", "POST")
        return version["id"]

    def run(version, data, status="SUCCEEDED", reason=None):
        run_id = call("/runs", "POST", {"agent_version_id": version, "input": data})["id"]
        path = "/runs/"+run_id
        wait_for(lambda: call(path)["status"] in {"SUCCEEDED", "FAILED", "TIMED_OUT"}, f"Model run stalled: {run_id}")
        record = call(path)
        assert (record["status"], record["failure_reason"]) == (status, reason), record
        return record, call(path+"/steps"), call(path+"/usage")["items"]

    compose("up", "-d", "--build", "--scale", "worker=1", "publisher", "worker")
    mock = publish("mock")
    final, steps, usage = run(mock, {"hello": "world"})
    assert final["output"]["answer"] == '{"hello":"world"}' and len(steps) == len(usage) == 1
    final, steps, usage = run(mock, {"count_text": "café"})
    assert final["output"] == {"answer": "4"} and [s["kind"] for s in steps] == ["model", "tool", "model"]
    assert steps[1]["tool_version_id"] == tool and steps[1]["result"] == {"length": 4}
    assert all(u["status"] == "reported" and u["input_tokens"] > 0 for u in usage)
    other_tenant = sql("SELECT id FROM tenants WHERE slug='second'")
    other_token = compose("run", "--rm", "--no-deps", "-T", "operator", "token", "--subject", "lin", capture=True)
    for resource in ("steps", "usage", "checkpoints"):
        status, _, _ = request_json(f"{base}/v1/tenants/{other_tenant}/runs/{final['id']}/{resource}", token=other_token)
        assert status == 404
    _, invalid_steps, _ = run(mock, {"count_text": 4}, "FAILED", "invalid_tool_arguments")
    assert invalid_steps[-1]["status"] == "failed"
    run(publish("mock", grant=False), {"count_text": "abc"}, "FAILED", "tool_not_permitted")
    sql(f"UPDATE tool_registry SET enabled=false WHERE id='{tool}'")
    run(mock, {"count_text": "abc"}, "FAILED", "tool_not_permitted")
    sql(f"UPDATE tool_registry SET enabled=true WHERE id='{tool}'")
    run(publish("mock", max_steps=1), {"count_text": "abc"}, "FAILED", "step_limit_exceeded")
    _, _, denied = run(publish("mock", max_tokens=1), {}, "FAILED", "token_budget_exhausted")
    assert denied == []

    remote = publish("openai-compatible", max_cost_usd=1)
    final, _, usage = run(remote, {"mode": "tool"})
    assert final["output"] == {"answer": "five"} and len(usage) == 2
    assert all(u["provider_request_id"] and Decimal(u["estimated_cost"]) == Decimal("0.000020") for u in usage)
    _, _, usage = run(remote, {"mode": "retry", "key": str(uuid4())})
    assert [(u["status"], u["error_code"]) for u in usage] == [("reported", None), ("rejected", "provider_rate_limited")]
    _, _, usage = run(remote, {"mode": "timeout"}, "FAILED", "provider_timeout")
    assert len(usage) == 3 and all(u["status"] == "unknown" and u["input_tokens"] is None for u in usage)
    _, _, usage = run(publish("openai-compatible", max_cost_usd=0.000001), {"mode": "unused"}, "FAILED", "cost_budget_exhausted")
    assert usage == []
    for cancel in (True, False):
        submit = {"agent_version_id": remote, "input": {"mode": "backoff"}}
        if not cancel:
            submit["deadline_at"] = (datetime.now(UTC) + timedelta(seconds=8)).isoformat()
        path = "/runs/" + call("/runs", "POST", submit)["id"]
        wait_for(lambda: call(path)["status"] == "RETRY_WAIT", "Missing durable retry timer")
        assert call(path)["retry_at"] and call(path+"/attempts")[-1]["outcome"] == "provider_retry"
        if cancel:
            assert call(path+"/cancel", "POST")["status"] == "CANCELLED"
        else:
            wait_for(lambda: call(path)["status"] == "TIMED_OUT", "Waiting run exceeded its deadline")
        assert call(path)["retry_at"] is None and call(path+"/steps")[-1]["status"] == "failed"
        assert len(call(path+"/usage")["items"]) == len(call(path+"/attempts")) == 1
    wait_for(lambda: sql("SELECT count(*) FROM runs WHERE status IN ('QUEUED','RUNNING')") == "0", "Old model backlog stalled")
    compose("stop", "worker", "publisher")
    # A former owner committed dispatch intent and lost its reply. Do not resend on the next claim.
    pending = call("/runs", "POST", {"agent_version_id": mock, "input": {"text": "uncertain"}})["id"]
    sql(f"""WITH s AS (INSERT INTO steps(tenant_id,run_id,ordinal,kind,request)
        VALUES ('{company}','{pending}',1,'model','{{"messages":[],"tools":[]}}') RETURNING id)
        INSERT INTO usage_records(tenant_id,run_id,step_id,invocation,provider,model,reserved_input,reserved_output)
        SELECT '{company}','{pending}',s.id,1,'mock','deterministic',128,32 FROM s""")
    compose("start", "worker", "publisher")
    wait_for(lambda: call("/runs/"+pending)["status"] == "FAILED", "Outstanding invocation was not handled")
    assert call("/runs/"+pending)["failure_reason"] == "model_outcome_unknown"
    uncertain = call("/runs/"+pending+"/usage")["items"]
    assert len(uncertain) == 1 and uncertain[0]["status"] == "unknown"
    compose("stop", "worker", "publisher")
    print("Model checks passed: single and multi-step execution, tool grants, budgets, 429 retry, uncertain timeout usage and HTTP adapter.")
    return remote
