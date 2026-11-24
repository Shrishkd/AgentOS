"""Kill disposable service processes and inspect the same run through the tenant API."""

import json
from subprocess import CalledProcessError
from time import sleep
from uuid import uuid4

from check_registry import request_json
from check_workers import wait_for


def check_recovery(base, run_path, token, version, compose, sql):
    root = run_path.split("/runs/")[0]
    submissions = base + root + "/runs"

    def get(run, suffix=""):
        status, data, _ = request_json(f"{submissions}/{run}{suffix}", token=token)
        assert status == 200, (status, data)
        return data

    def process(service, mode, stdin=None, killed=True):
        try:
            compose("run", "--rm", "--no-deps", "-T", "-e", "AGENTOS_LEASE_SECONDS=3",
                    service, "python", "/crash_process.py", mode, stdin=stdin)
        except CalledProcessError as exc:
            if not killed or exc.returncode != 137:
                raise
        else:
            assert not killed, f"{mode} did not kill its process"

    wait_for(lambda: sql("SELECT count(*) FROM outbox_events WHERE published_at IS NULL") == "0",
             "Previous events still pending")
    compose("stop", "publisher")
    body = {"agent_version_id": version, "input": {"mode": "tool"}}
    key = str(uuid4())
    process("api", "after_api_commit", json.dumps({"path": root + "/runs", "body": body,
        "headers": {"Authorization": "Bearer " + token, "Idempotency-Key": key}}))
    status, original, _ = request_json(submissions, "POST", body, token, key)
    assert status == 200
    run = original["id"]
    process("publisher", "after_publish")
    event = f"FROM outbox_events WHERE run_id='{run}'"
    assert sql("SELECT count(*) " + event + " AND publish_attempts=1 AND published_at IS NULL") == "1"
    compose("start", "publisher")
    wait_for(lambda: sql("SELECT count(*) " + event + " AND publish_attempts=2 AND published_at IS NOT NULL") == "1",
             "Lost publish acknowledgement was not recovered")

    for fault, boundary in [("before_dispatch", 0), ("after_tool_effect", 1), ("database_renewal", 3),
                            ("after_final_checkpoint", 3), ("after_provider_reply", 0)]:
        if fault != "before_dispatch":
            status, record, _ = request_json(submissions, "POST", body, token, str(uuid4()))
            assert status == 201
            run = record["id"]
        if fault == "database_renewal":
            compose("run", "--rm", "-d", "--no-deps", "-e", "AGENTOS_LEASE_SECONDS=3",
                    "worker", "python", "/crash_process.py", fault)
            wait_for(lambda: len(get(run, "/checkpoints")) == 3, "Worker did not reach the final boundary")
        else:
            process("worker", fault)
        saved = get(run, "/checkpoints")
        assert len(saved) == boundary, (fault, saved)
        if fault == "after_tool_effect":
            pending = get(run, "/steps")[-1]
            assert pending["kind"] == "tool" and pending["status"] == "pending"
            process("worker", "recover_tool_effect", killed=False)
            assert get(run, "/steps")[1]["id"] == pending["id"]
        else:
            if fault == "database_renewal":
                compose("stop", "postgres")
                try:
                    assert request_json(base+"/health/ready")[0] == 503
                    sleep(4)  # The 3-second lease must expire while writes and renewals are unavailable.
                finally:
                    compose("start", "postgres")
                wait_for(lambda: request_json(base+"/health/ready")[0] == 200, "Database did not recover")
            compose("start", "worker")
        wait_for(lambda: get(run)["status"] in {"SUCCEEDED", "FAILED"}, f"Recovery stalled at {fault}")
        attempts = get(run, "/attempts")
        assert len(attempts) == 2 and attempts[0]["outcome"] == "lease_expired", (fault, attempts, get(run))
        assert attempts[0]["worker_id"] != attempts[1]["worker_id"]
        history = get(run, "/checkpoints")
        assert history[:boundary] == saved
        usage = get(run, "/usage")["items"]
        if fault == "after_provider_reply":
            assert get(run)["failure_reason"] == "model_outcome_unknown"
            assert len(usage) == 1 and usage[0]["status"] == "unknown" and not history
        else:
            assert get(run)["output"] == {"answer": "five"}
            assert [c["next_action"] for c in history] == ["tool", "model", "finish"]
            assert [c["next_ordinal"] for c in history] == [2, 3, 4]
            assert len(usage) == 2 and all(u["status"] == "reported" for u in usage)
            events = {e["seq"]: e for e in get(run, "/events")}
            for c in history:
                e = events[c["event_seq"]]
                assert e["kind"] == "run.checkpoint" and e["payload"]["step_id"] == c["step_id"]
        compose("stop", "worker")
        print(f"Recovery {fault}: {run}, {attempts[0]['outcome']} -> {attempts[1]['outcome']}.", flush=True)
    print("Recovery checks passed, including API response loss, lost publish acknowledgement and keyed tool replay.")
