"""Exercise live SSE, retry traces and the same Prometheus queries used by Grafana."""

import json
from decimal import Decimal
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from check_registry import request_json
from check_workers import wait_for


def read_event(response):
    fields = {}
    for raw in response:
        line = raw.decode().rstrip("\r\n")
        if not line and fields:
            data = json.loads(fields["data"])
            assert int(fields["id"]) == data["seq"] and fields["event"] == data["kind"]
            return data
        if line and not line.startswith(":"):
            key, value = line.split(": ", 1)
            fields[key] = value
    return None


def check_observability(base, run_path, token, remote, compose, sql):
    root = run_path.split("/runs/")[0]
    status, run, _ = request_json(base + root + "/runs", "POST", {
        "agent_version_id": remote, "input": {"mode": "retry", "key": str(uuid4())}}, token, str(uuid4()))
    assert status == 201
    run_url = base + root + "/runs/" + run["id"]
    headers = {"Authorization": "Bearer " + token, "Accept": "text/event-stream"}

    def stream(cursor=None):
        extra = {} if cursor is None else {"Last-Event-ID": str(cursor)}
        return urlopen(Request(run_url + "/events?limit=1", headers={**headers, **extra}), timeout=35)

    with stream() as response:
        assert response.headers.get_content_type() == "text/event-stream"
        first = read_event(response)
        assert first["seq"] == 1 and first["payload"]["status"] == "CREATED"
    for cursor in ("malformed", "9999999999"):
        try:
            stream(cursor).close()
        except HTTPError as exc:
            assert exc.code == 422
            exc.close()
        else:
            raise AssertionError("Invalid event cursor was accepted")
    with stream(first["seq"]) as response:
        events = [first, read_event(response)]
        assert events[-1]["payload"]["status"] == "QUEUED"
        compose("start", "publisher", "worker")
        while event := read_event(response):
            events.append(event)
    assert events == request_json(run_url + "/events", token=token)[1]
    assert events[-1]["payload"]["status"] == "SUCCEEDED"
    assert any(e["payload"].get("status") == "RETRY_WAIT" for e in events)
    assert list(range(1, len(events) + 1)) == [e["seq"] for e in events]
    diag = request_json(run_url + "/diagnostics", token=token)[1]
    assert diag["retry_count"] == 1 and diag["last_attempt"]["number"] == 2
    assert diag["last_step"]["status"] == "completed" and diag["duration_seconds"] > 0
    status, usage, _ = request_json(base + root + "/usage?run_id=" + run["id"], token=token)
    rows = request_json(run_url + "/usage", token=token)[1]["items"]
    assert status == 200 and usage["invocations"] == len(rows) == 2
    assert usage["reported"] == usage["rejected"] == 1
    reported = next(r for r in rows if r["status"] == "reported")
    assert (usage["input_tokens"], usage["output_tokens"], Decimal(usage["estimated_cost_usd"])) == (
        reported["input_tokens"], reported["output_tokens"], Decimal(reported["estimated_cost"]))
    other = sql("SELECT id FROM tenants WHERE slug='second'")
    other_token = compose("run", "--rm", "--no-deps", "-T", "operator", "token", "--subject", "lin", capture=True)
    foreign = base + "/v1/tenants/" + other
    assert request_json(foreign + "/usage?run_id=" + run["id"], token=other_token)[0] == 404
    assert request_json(foreign + "/runs/" + run["id"] + "/diagnostics", token=other_token)[0] == 404

    grafana = "http://" + compose("port", "grafana", "3000", capture=True)
    jaeger = "http://" + compose("port", "jaeger", "16686", capture=True)
    dashboards = [request_json(grafana + "/api/dashboards/uid/agentos-" + uid)[1]["dashboard"]
                  for uid in ("execution", "usage")]
    assert all(dash["panels"] for dash in dashboards)
    expected = sql("""SELECT sum(input_tokens),sum(output_tokens),sum(estimated_cost)
        FROM usage_records WHERE status='reported'""").split("|")
    expected = [Decimal(v) for v in expected]

    def metric_values(expr):
        api = "/api/datasources/proxy/uid/agentos-prometheus/api/v1/query?"
        status, body, _ = request_json(grafana + api + urlencode({"query": expr}))
        assert status == 200, status
        return {r["metric"].get("direction", ""): Decimal(r["value"][1]) for r in body["data"]["result"]}

    tokens, cost = [p["targets"][0]["expr"] for p in dashboards[1]["panels"][:2]]
    wait_for(lambda: metric_values(tokens) == dict(zip(("input", "output"), expected[:2]))
             and metric_values(cost) == {"": expected[2]}, "Grafana usage differs from settled database records")

    def trace_complete():
        status, body, _ = request_json(jaeger + "/api/traces/" + diag["trace_id"])
        if status == 404 or not body.get("data"):
            return False
        spans = body["data"][0]["spans"]
        attempts = [s for s in spans if s["operationName"] == "run.attempt"]
        steps = [s for s in spans if s["operationName"] == "step.model"]
        roots = [s for s in spans if s["operationName"] == "run.submit"]
        if (len(roots), len(attempts), len(steps)) != (1, 2, 2):
            return False
        assert all(s["references"][0]["spanID"] == roots[0]["spanID"] for s in attempts)
        assert {s["references"][0]["spanID"] for s in steps} == {s["spanID"] for s in attempts}
        assert any(t["key"] == "error.type" and t["value"] == "provider_rate_limited"
                   for s in steps for t in s["tags"])
        return True

    wait_for(trace_complete, "Missing retry spans in Jaeger")
    compose("stop", "worker", "publisher")
    print("Observability checks passed for SSE reconnect and tail, tenant isolation, retry traces and Grafana totals.")
