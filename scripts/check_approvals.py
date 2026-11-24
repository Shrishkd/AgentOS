"""Exercise review and dispatch through the real API, worker pool and optional browser."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from check_registry import request_json
from check_workers import wait_for


def check_approvals(base, run_path, developer, compose, sql, browser):
    root = run_path.split("/runs/")[0]
    tenant = root.rsplit("/", 1)[1]

    def identity(subject, role, slug="first"):
        compose("run", "--rm", "--no-deps", "-T", "operator", "grant", "--tenant", slug, "--subject", subject,
                "--role", role, capture=True)
        return compose("run", "--rm", "--no-deps", "-T", "operator", "token", "--subject", subject, capture=True)

    admin = identity("tool-admin", "admin")
    reviewer = identity("tool-reviewer", "approver")
    outsider = identity("other-reviewer", "approver", "second")
    other = sql("SELECT id FROM tenants WHERE slug='second'")

    def call(path, method="GET", body=None, token=developer, expected=200):
        status, data, _ = request_json(base + root + path, method, body, token, str(uuid4()))
        assert status == expected, (path, status, data)
        return data

    def published(tool_ids):
        version = call(versions, "POST", {"instructions": "Use the requested tool.", "model": "deterministic",
            "provider": "mock", "tool_version_ids": tool_ids,
            "limits": {"max_steps": 3, "max_tokens": 20000, "timeout_seconds": 300}}, expected=201)
        call(versions + "/" + version["id"] + "/publish", "POST")
        return version["id"]

    def submit(version, name, args):
        body = {"agent_version_id": version, "input": {"tool_call": {"name": name, "arguments": args}}}
        return "/runs/" + call("/runs", "POST", body, expected=201)["id"]

    def wait(path, state):
        wait_for(lambda: call(path)["status"] == state, f"Expected {state} for {path}")

    call("/tools", "POST", {"name": "save_report"}, expected=403)
    call("/tools", "POST", {"name": "unregistered"}, token=admin, expected=422)
    tools = {name: call("/tools", "POST", {"name": name}, token=admin)["id"]
             for name in ("calculator", "inventory_lookup", "save_report")}
    agent = call("/agents", "POST", {"name": "Review tools"}, expected=201)
    versions = "/agents/" + agent["id"] + "/versions"
    version = published(list(tools.values()))
    ungranted = published([])
    compose("start", "worker", "publisher")
    for name, args, expected in [("calculator", {"expression": "3*(4+2)"}, {"value": 18}),
            ("inventory_lookup", {"category": "hardware", "limit": 1}, {"items": [{"name": "Keyboard", "quantity": 12}]})]:
        path = submit(version, name, args)
        wait(path, "SUCCEEDED")
        assert call(path + "/steps")[1]["result"] == expected
    args = {"title": "Review sample", "sections": ["Queue drained."]}
    path = submit(ungranted, "save_report", args)
    wait(path, "FAILED")
    assert call(path)["failure_reason"] == "tool_not_permitted"
    path = submit(version, "save_report", {"title": "incomplete"})
    wait(path, "FAILED")
    assert call(path)["failure_reason"] == "invalid_tool_arguments"
    for case in ("approve", "reject", "expire", "cancel", "revoked", "changed"):
        path = submit(version, "save_report", args)
        wait(path, "WAITING_FOR_APPROVAL")
        run = call(path)
        step = call(path + "/steps")[1]
        proposal, = [a for a in call("/approvals?status=pending")["items"] if a["run_id"] == run["id"]]
        item = "/approvals/" + proposal["id"]
        decision = item + "/decision"
        body = {"decision": "reject" if case == "reject" else "approve", "action_digest": proposal["action_digest"]}
        assert step["dispatch_count"] == 0 and proposal["arguments"] == args
        assert call(path+"/attempts")[-1]["outcome"] == "WAITING_FOR_APPROVAL"
        assert [c["next_action"] for c in call(path+"/checkpoints")] == ["tool", "await_decision"]
        report = "/reports/" + step["id"]
        call(report, expected=404)
        call(decision, "POST", body, expected=403)
        assert request_json(base + "/v1/tenants/" + other + item, token=outsider)[0] == 404
        call(decision, "POST", {**body, "action_digest": "0"*64}, reviewer, 409)
        if case == "expire":
            sql(f"UPDATE approvals SET expires_at=now() WHERE id='{proposal['id']}'")
            wait(path, "FAILED")
            assert call(path)["failure_reason"] == "approval_expired"
            call(decision, "POST", body, reviewer, 409)
        elif case == "cancel":
            call(path+"/cancel", "POST")
            assert call(item)["status"] == "cancelled"
            call(decision, "POST", body, reviewer, 409)
        else:
            if case == "revoked":
                call("/tools/"+tools["save_report"], "PATCH", {"enabled": False}, admin)
            if case == "changed":
                sql(f"""UPDATE steps SET request=jsonb_set(request,'{{arguments,title}}','"changed"')
                         WHERE id='{step["id"]}'""")
            if case == "approve" and browser:
                subprocess.run([shutil.which("npm.cmd" if os.name == "nt" else "npm"), "run", "test:browser", "--",
                    "approvals.spec.ts"], cwd=Path(__file__).resolve().parents[1] / "dashboard", check=True,
                    env={**os.environ, "AGENTOS_TEST_URL": base, "AGENTOS_TEST_TENANT": tenant,
                         "AGENTOS_TEST_REVIEWER": reviewer})
            with ThreadPoolExecutor(2) as pool:
                receipts = list(pool.map(lambda _: call(decision, "POST", body, reviewer), range(2)))
            assert receipts[0]["decided_at"] == receipts[1]["decided_at"]
            call(decision, "POST", {**body, "decision": "reject" if case != "reject" else "approve"}, reviewer, 409)
            wait(path, "SUCCEEDED" if case == "approve" else "FAILED")
        if case == "approve":
            assert call(report)["document"] == args and call(path+"/steps")[1]["dispatch_count"] == 1
            assert [c["next_action"] for c in call(path+"/checkpoints")] == [
                "tool", "await_decision", "authorize_tool", "model", "finish"]
        else:
            call(report, expected=404)
            assert call(path+"/steps")[1]["dispatch_count"] == 0
        if case == "revoked":
            call("/tools/"+tools["save_report"], "PATCH", {"enabled": True}, admin)
    compose("stop", "worker", "publisher")
    print("Approval checks passed: role and tenant isolation, browser/API decision, replay, refusal, expiry, cancellation, changed arguments and revocation.", flush=True)
