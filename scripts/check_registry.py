"""HTTP and PostgreSQL acceptance checks for the disposable Compose project."""

import json
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4


def request_json(url, method="GET", body=None, token=None, key=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, headers=headers, method=method)
    try:
        response = urlopen(request, timeout=15)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response), response.headers


def check_registry(base, compose, sql):
    def operator(*args):
        return compose("run", "--rm", "--no-deps", "-T", "operator", *args, capture=True)

    def grant(tenant, subject, role):
        return str(UUID(operator("grant", "--tenant", tenant, "--subject", subject, "--role", role)))

    tenant = grant("first", "ada", "developer")
    other = grant("second", "lin", "developer")
    grant("first", "reader", "viewer")
    token = operator("token", "--subject", "ada")
    other_token = operator("token", "--subject", "lin")
    viewer = operator("token", "--subject", "reader")
    root = f"/v1/tenants/{tenant}"
    url = base + root
    other_url = base + f"/v1/tenants/{other}"

    def call(path, method="GET", body=None, key=None, expected=200, auth=token):
        status, payload, headers = request_json(url + path, method, body, auth, key)
        assert status == expected, (path, status, payload)
        UUID(headers["X-Request-ID"])
        return payload

    call("/agents", auth=None, expected=401)
    call("/agents", auth=token + "bad", expected=401)
    call("/agents", auth=other_token, expected=404)
    call("/agents", "POST", {"name": "Denied"}, auth=viewer, expected=403)
    agent = call("/agents", "POST", {"name": "Reporter"}, expected=201)
    call("/agents", "POST", {"name": "Reporter"}, expected=409)
    call("/agents/" + agent["id"], auth=viewer)
    versions = "/agents/" + agent["id"] + "/versions"
    config = {"instructions": "Summarize input.", "provider": "mock", "model": "deterministic",
              "limits": {"max_steps": 5, "max_tokens": 1024, "timeout_seconds": 3600}}
    call(versions, "POST", {**config, "tool_version_ids": [str(uuid4())]}, expected=422)
    tool = str(uuid4())
    foreign_tool = str(uuid4())
    for tool_tenant, tool_id in [(tenant, tool), (other, foreign_tool)]:
        sql(f"""INSERT INTO tool_registry (id,tenant_id,name,version,definition)
            VALUES ('{tool_id}','{tool_tenant}','fixture',1,'{{}}')""")
    call(versions, "POST", {**config, "tool_version_ids": [foreign_tool]}, expected=422)
    config["tool_version_ids"] = [tool]
    with ThreadPoolExecutor(2) as pool:
        drafts = list(pool.map(lambda _: call(versions, "POST", config, expected=201), range(2)))
    assert sorted(v["number"] for v in drafts) == [1, 2]
    assert all(v["tool_version_ids"] == [tool] for v in drafts)
    version = drafts[0]["id"]
    body = {"agent_version_id": version, "input": {"topic": "durability"}}
    call("/runs", "POST", body, "draft", 409)
    publish = versions + "/" + version + "/publish"
    assert call(publish, "POST")["state"] == "PUBLISHED"
    call(publish, "POST")
    call("/runs", "POST", {**body, "deadline_at": "2020-01-01T00:00:00Z"}, "expired", 422)
    call("/runs", "POST", body, expected=422)
    with ThreadPoolExecutor(8) as pool:
        replies = list(pool.map(lambda _: request_json(url + "/runs", "POST", body, token, "same"), range(8)))
    assert sorted(item[0] for item in replies) == [200]*7 + [201], [(s, r) for s, r, _ in replies]
    run = replies[0][1]["id"]
    assert all(item[1]["id"] == run and item[2]["Location"] == root+"/runs/"+run for item in replies)
    with ThreadPoolExecutor(2) as pool:
        replies = list(pool.map(lambda n: request_json(url+"/runs", "POST", {**body, "priority": n},
                                                       token, "conflict"), [1, 2]))
    assert sorted(item[0] for item in replies) == [201, 409]
    assert [e["payload"]["status"] for e in call("/runs/"+run+"/events")] == ["CREATED", "QUEUED"]
    assert sql(f"SELECT count(*) FROM outbox_events WHERE run_id='{run}' AND published_at IS NULL") == "1"
    assert call("/runs/"+run, auth=viewer)["status"] == "QUEUED"
    assert request_json(other_url+"/runs/"+run, token=other_token)[0] == 404
    assert request_json(other_url+"/runs", token=other_token)[1]["items"] == []
    listing = call("/runs?limit=1&status=QUEUED")
    following = call("/runs?limit=1&status=QUEUED&cursor="+listing["next_cursor"])
    assert following["items"][0]["id"] != listing["items"][0]["id"]
    call("/runs?status=FAILED&cursor="+listing["next_cursor"], expected=422)
    assert len(call("/runs?agent_id="+agent["id"])["items"]) == 2

    # A failed outbox write must roll back the run and its two initial events.
    sql("ALTER TABLE outbox_events ADD CONSTRAINT test_outbox_unavailable CHECK (false) NOT VALID")
    try:
        status, _, _ = request_json(url+"/runs", "POST", body, token, "atomic")
        assert status >= 500
        assert sql("SELECT count(*) FROM runs WHERE submission_key='atomic'") == "0"
    finally:
        sql("ALTER TABLE outbox_events DROP CONSTRAINT test_outbox_unavailable")
    call("/runs", "POST", body, "atomic", 201)
    sql(f"UPDATE tenants SET submissions_per_minute=1 WHERE id='{tenant}'")
    status, _, headers = request_json(url+"/runs", "POST", body, token, "rate")
    assert status == 429 and int(headers["Retry-After"]) > 0
    sql(f"UPDATE tenants SET submissions_per_minute=120 WHERE id='{tenant}'")
    sql(f"UPDATE tenants SET pending_limit=3 WHERE id='{tenant}'")
    call("/runs", "POST", body, "over-quota", 429)
    call(versions+"/"+version+"/deactivate", "POST")
    call(publish, "POST", expected=409)
    call("/runs", "POST", body, "another", 409)
    assert call("/runs", "POST", {**body, "priority": 0}, "same")["id"] == run
    assert call("/runs/"+run+"/cancel", "POST")["status"] == "CANCELLED"
    assert call("/runs/"+run+"/cancel", "POST")["revision"] == 4
    assert call("/runs/"+run)["agent_version_id"] == version
    assert [event["seq"] for event in call("/runs/"+run+"/events?after=2")] == [3, 4]
    sql(f"UPDATE users SET enabled=false WHERE tenant_id='{tenant}' AND subject='reader'")
    call("/runs/"+run, auth=viewer, expected=404)

    # A SQL query omitting its tenant predicate is still constrained as the real API role.
    count = sql(f"""BEGIN; SET LOCAL ROLE agentos_api; SELECT set_config('agentos.tenant_id','{other}',true);
                   SELECT count(*) FROM runs; COMMIT""").splitlines()
    assert count[-2] == "0", count
    # The relational constraint also rejects a privileged writer mixing tenants.
    sql(f"""DO $$ BEGIN
        BEGIN INSERT INTO run_events(tenant_id,run_id,seq,kind,payload)
        VALUES ('{other}','{run}',99,'test','{{}}');
        RAISE EXCEPTION 'Expected cross-tenant FK rejection';
        EXCEPTION WHEN foreign_key_violation THEN NULL; END;
    END $$""")
    print("Registry checks passed, including authorization, tenant isolation, version pinning, admission races and rollback.")
    return root + "/runs/" + run, token, run
