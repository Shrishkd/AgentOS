from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic, sleep
from uuid import UUID, uuid4

from check_registry import request_json


def wait_for(check, description):
    deadline = monotonic() + 65
    while monotonic() < deadline:
        if check():
            return
        sleep(0.25)
    raise AssertionError(description)


def check_workers(base, run_path, token, compose, sql):
    root = run_path.split("/runs/")[0]
    tenant = str(UUID(root.rsplit("/", 1)[1]))
    other = str(UUID(sql("SELECT id FROM tenants WHERE slug='second'")))
    other_token = compose("run", "--rm", "--no-deps", "-T", "operator", "token", "--subject", "lin", capture=True)
    other_root = "/v1/tenants/" + other

    def call(path, method="GET", body=None, auth=token):
        status, result, _ = request_json(base+path, method, body, auth, str(uuid4()))
        assert status in (200, 201), (path, status, result)
        return result

    def publish(path, auth):
        agent = call(path+"/agents", "POST", {"name": "Receipt worker"}, auth)
        versions = path+"/agents/"+agent["id"]+"/versions"
        version = call(versions, "POST", {"provider": "builtin", "model": "receipt", "instructions": "Issue a receipt",
            "limits": {"max_steps": 1, "max_tokens": 1, "timeout_seconds": 600}}, auth)
        call(versions+"/"+version["id"]+"/publish", "POST", auth=auth)
        return version["id"]

    sql(f"UPDATE tenants SET pending_limit=100,submissions_per_minute=1000,concurrency_limit=2 WHERE id='{tenant}'")
    sql(f"UPDATE tenants SET last_admitted_at=now() WHERE id='{other}'")
    version, peer_version = publish(root, token), publish(other_root, other_token)

    def submit(priority):
        return call(root+"/runs", "POST", {"agent_version_id": version, "input": {"priority": priority},
                                          "priority": priority})["id"]

    low, middle, high = submit(0), submit(5), submit(9)
    peer = call(other_root+"/runs", "POST", {"agent_version_id": peer_version, "input": {}}, other_token)["id"]
    compose("build", "worker", "publisher")
    compose("run", "--rm", "--no-deps", "-T", "worker", "python", "-", high, middle, peer,
            stdin=Path(__file__).with_name("check_leases.py").read_text())
    attempts = call(root+f"/runs/{high}/attempts")
    assert [a["outcome"] for a in attempts] == ["lease_expired", "SUCCEEDED"]
    assert call(root+f"/runs/{high}")["output"]["worker_id"] == attempts[1]["worker_id"]
    peer_workers = call(other_root+"/workers", auth=other_token)["items"]
    assert len(peer_workers) == 1 and peer_workers[0]["id"] not in {a["worker_id"] for a in attempts}

    # Lose all queued hints. The publisher's scan must rediscover the eligible low-priority run.
    sql("UPDATE outbox_events SET published_at=now()")
    compose("up", "-d", "--scale", "worker=2", "worker", "publisher")
    wait_for(lambda: call(root+f"/runs/{low}")["status"] == "SUCCEEDED", "Missing wakeup was not repaired")
    wait_for(lambda: sql("SELECT count(*) FROM workers WHERE NOT draining AND healthy_until>now()") == "2",
             "Expected two healthy worker processes")
    sql(f"UPDATE tenants SET concurrency_limit=4 WHERE id='{tenant}'")
    compose("stop", "nats")
    with ThreadPoolExecutor(6) as pool:
        batch = list(pool.map(submit, [2]*20))
    assert sql(f"SELECT count(*) FROM outbox_events WHERE run_id='{batch[0]}' AND published_at IS NULL") == "1"
    compose("start", "nats")
    wait_for(lambda: sql(f"SELECT count(*) FROM runs WHERE agent_version_id='{version}' AND status='SUCCEEDED'")
             == str(len(batch)+3), "Workers failed to drain the broker outage backlog")
    results = [call(root+f"/runs/{run}")["output"] for run in batch]
    owners = Counter(result["worker_id"] for result in results)
    assert len(owners) >= 2 and all(result["attempt"] == 1 for result in results), owners
    assert len({result["input_sha256"] for result in results}) == 1
    # Send another distinct wakeup for completed work; it cannot create another attempt.
    event = sql(f"INSERT INTO outbox_events(tenant_id,run_id,kind) VALUES ('{tenant}','{low}','run.queued') RETURNING id").splitlines()[0]
    wait_for(lambda: sql(f"SELECT published_at IS NOT NULL FROM outbox_events WHERE id='{UUID(event)}'") == "t",
             "Duplicate wakeup did not receive a publish acknowledgement")
    compose("stop", "worker")
    assert sql("SELECT count(*) FROM attempts WHERE ended_at IS NULL") == "0"
    assert len(call(root+f"/runs/{low}/attempts")) == 1
    assert all(w["status"] == "stopped" for w in call(root+"/workers")["items"])
    print(f"Worker checks passed: {dict(owners)}, fenced recovery, quotas, wakeup repair and graceful shutdown.")
