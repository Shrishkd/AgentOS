"""Bounded benchmark for the randomly named Compose project in check_foundation."""
import json
import platform
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from uuid import UUID, uuid4

from check_registry import request_json

CASES = {"sequential": 100, "burst": 100, "long": 10, "timeout": 10}
TERMINAL = "('SUCCEEDED','FAILED','TIMED_OUT','CANCELLED')"


def distribution(values):
    ordered = sorted(values)
    quantiles = [50, 95] + ([99] if len(ordered) >= 100 else [])
    return {f"p{q}": round(ordered[ceil(q * len(ordered) / 100) - 1], 3) for q in quantiles}


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, timeout=40).strip()


def sample_resources(ids, stop):
    snapshots = []
    while True:
        raw = docker("stats", "--no-stream", "--format", "{{json .}}", *ids)
        containers = {}
        for line in raw.splitlines():
            row = json.loads(line)
            # check_foundation names each project agentos-test-<uuid>.
            name = row["Name"].split("-", 3)[3]
            containers[name] = {"cpu_percent": float(row["CPUPerc"].rstrip("%")),
                                "memory": row["MemUsage"].split("/")[0].strip()}
        if len(containers) != len(ids):
            raise RuntimeError("Incomplete Docker resource sample")
        snapshots.append({"utc": datetime.now(UTC).isoformat(), "containers": containers})
        if stop.wait(0.5):
            return snapshots


def db_rows(sql, statement):
    return json.loads(sql("SELECT coalesce(json_agg(sample),'[]') FROM (" + statement + ") AS sample"))


def workload(compose, sql, token, tenant, version, name, ids, slots):
    count = CASES[name]
    scope = f"r.agent_version_id='{version}'"
    sql("SELECT pg_stat_statements_reset()")
    stop = Event()
    with ThreadPoolExecutor(1) as pool:
        sampling = pool.submit(sample_resources, ids, stop)
        try:
            output = compose("run", "--rm", "-T", "--no-deps", "-e", "WORKLOAD=" + name,
                             "-e", "TENANT=" + tenant, "-e", "VERSION=" + version, "k6",
                             stdin=token + "\n", capture=True)
            prefix = "AGENTOS_SAMPLE "
            submissions = [json.loads(line.removeprefix(prefix)) for line in output.splitlines() if line.startswith(prefix)]
            assert sorted(x["index"] for x in submissions) == list(range(count)), name
            codes = Counter(row["status"] for row in submissions)
            assert codes == {201: count}, (name, codes, Counter(row["error_code"] for row in submissions))
            deadline = monotonic() + 300
            while int(sql(f"SELECT count(*) FROM runs r WHERE {scope} AND status IN {TERMINAL}")) < count:
                if monotonic() > deadline:
                    raise TimeoutError(f"{name} did not finish within five minutes")
                sleep(1)
        finally:
            stop.set()
        samples = sampling.result()

    profile = db_rows(sql, """SELECT queryid::text,round(total_exec_time::numeric,2) AS total_ms,calls,
        left(regexp_replace(query,'[[:space:]]+',' ','g'),140) AS statement
        FROM pg_stat_statements WHERE userid IN (SELECT oid FROM pg_roles WHERE rolname IN
        ('agentos_api','agentos_worker','agentos_publisher')) ORDER BY total_exec_time DESC LIMIT 5""")
    runs = db_rows(sql, f"""SELECT (r.input->>'index')::int AS index,r.status,r.failure_reason,
        extract(epoch FROM r.created_at) AS submitted,extract(epoch FROM r.updated_at) AS finished,
        1000*extract(epoch FROM r.updated_at-r.created_at) AS completion_ms,
        1000*extract(epoch FROM a.started_at-(SELECT min(e.created_at) FROM run_events e WHERE
            e.run_id=r.id AND e.kind='run.state' AND e.payload->>'status'='QUEUED')) AS dispatch_ms,
        a.worker_id::text AS first_worker,
        (SELECT sum(extract(epoch FROM ended_at-started_at)) FROM attempts WHERE run_id=r.id) AS busy_seconds,
        (SELECT count(*) FROM attempts WHERE run_id=r.id) AS attempts,
        (SELECT count(*) FROM steps WHERE run_id=r.id) AS steps
        FROM runs r JOIN attempts a ON a.run_id=r.id AND a.number=1 WHERE {scope} ORDER BY 1""")
    usage = db_rows(sql, f"""SELECT u.status,count(*) AS calls,sum(u.input_tokens) AS input_tokens,
        sum(u.output_tokens) AS output_tokens,sum(u.estimated_cost)::text AS estimated_usd
        FROM usage_records u JOIN runs r ON r.id=u.run_id WHERE {scope} GROUP BY u.status ORDER BY u.status""")
    assert [r["index"] for r in runs] == list(range(count))
    expected = "FAILED" if name == "timeout" else "SUCCEEDED"
    assert all(r["status"] == expected for r in runs), (name, Counter(r["failure_reason"] for r in runs))
    assert all(r["dispatch_ms"] >= 0 and r["completion_ms"] > 0 for r in runs)
    assert all(r["attempts"] == (3 if name == "timeout" else 1) for r in runs)
    owners = Counter(r["first_worker"] for r in runs)
    if name == "burst":
        assert len(owners) >= 2, "The competing-worker workload used fewer than two workers"
    if name == "timeout":
        assert all(r["failure_reason"] == "provider_timeout" for r in runs)
        assert len(usage) == 1 and usage[0]["status"] == "unknown" and usage[0]["calls"] == count * 3
    elif name == "long":
        assert all(r["steps"] == 49 for r in runs)
        assert len(usage) == 1 and usage[0]["status"] == "reported" and usage[0]["calls"] == count * 25
    else:
        assert not usage

    seconds = max(r["finished"] for r in runs) - min(r["submitted"] for r in runs)
    return {"submissions": sorted(submissions, key=lambda x: x["index"]), "runs": runs,
            "usage": usage, "resources": samples, "sql_profile": profile, "workers": owners,
            "summary": {"runs": count, "elapsed_seconds": round(seconds, 3),
                        "terminal_runs_per_second": round(count / seconds, 3),
                        "failed_runs": sum(r["status"] != "SUCCEEDED" for r in runs),
                        "slot_occupancy_percent": round(100 * sum(r["busy_seconds"] for r in runs) / (slots * seconds), 2),
                        "submit_ms": distribution([x["ms"] for x in submissions]),
                        "first_dispatch_ms": distribution([x["dispatch_ms"] for x in runs]),
                        "completion_ms": distribution([x["completion_ms"] for x in runs])}}


def run_benchmark(base, compose, sql, recovery, output_path, tracing):
    tenant = str(UUID(compose("run", "--rm", "--no-deps", "-T", "operator", "grant", "--tenant", "benchmark",
                              "--subject", "benchmark", "--role", "developer", capture=True)))
    token = compose("run", "--rm", "--no-deps", "-T", "operator", "token", "--subject", "benchmark", capture=True)
    tool = str(UUID(compose("run", "--rm", "--no-deps", "-T", "operator", "install-text-length", "--tenant",
                            "benchmark", capture=True)))
    sql(f"UPDATE tenants SET submissions_per_minute=1000, pending_limit=300, concurrency_limit=4 WHERE id='{tenant}'")
    sql("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")

    def post(path, body=None):
        status, payload, _ = request_json(base + "/v1/tenants/" + tenant + path, "POST", body, token, str(uuid4()))
        assert status in (200, 201), (path, status, payload)
        return payload

    agent = post("/agents", {"name": "Performance fixtures"})["id"]
    versions = "/agents/" + agent + "/versions"
    compose("up", "-d", "--scale", "worker=2", "worker", "publisher")
    ids = compose("ps", "-q", "api", "dashboard", "worker", "publisher", "postgres", "redis", "nats",
                  "model-fixture", capture=True).splitlines()
    deadline = monotonic() + 45
    while True:
        slots = sql("SELECT count(*),coalesce(sum(capacity),0) FROM workers WHERE NOT draining AND healthy_until>now()")
        if slots.startswith("2|"):
            break
        if monotonic() > deadline:
            raise RuntimeError("Expected two healthy workers")
        sleep(0.5)
    env = json.loads(docker("info", "--format", '{"engine":"{{.ServerVersion}}","os":"{{.OperatingSystem}}",'
                            '"arch":"{{.Architecture}}","cpus":{{.NCPU}},"memory_bytes":{{.MemTotal}}}'))
    cpu_model = compose("exec", "-T", "api", "python", "-c",
        "from pathlib import Path; print(next((s.partition(':')[2].strip() for s in "
        "Path('/proc/cpuinfo').read_text().splitlines() if s.startswith('model name')), 'unreported'))", capture=True)
    env.update(host=platform.platform(), cpu_model=cpu_model, postgres=sql("SHOW server_version"),
               python=platform.python_version(), k6="1.3.0",
               workers=2, slots=int(slots.split("|")[1]), observability=tracing)
    env["api_processes"] = int(compose("exec", "-T", "api", "printenv", "AGENTOS_API_PROCESSES", capture=True))
    report = {"started_utc": datetime.now(UTC).isoformat(), "environment": env, "recovery": recovery, "workloads": {}}
    for name in CASES:
        remote = name in {"long", "timeout"}
        version = str(UUID(post(versions, {"instructions": "Complete the fixture task.",
            "provider": "openai-compatible" if remote else "builtin", "model": "fixture-model" if remote else "receipt",
            "limits": {"max_steps": 64, "max_tokens": 200000, "timeout_seconds": 240},
            "tool_version_ids": [tool] if name == "long" else []})["id"]))
        post(versions + "/" + version + "/publish")
        if name == "sequential":
            receipt_version = version
        if name == "long":
            tool_version = version
        result = workload(compose, sql, token, tenant, version, name, ids, env["slots"])
        report["workloads"][name] = result
        print(f"{name}: {result['summary']}", flush=True)
    compose("stop", "worker")
    run = str(UUID(post("/runs", {"agent_version_id": receipt_version, "input": {"redelivery": True}})["id"]))
    script = Path(__file__).with_name("fixtures").joinpath("redelivery.py").read_text()
    report["redelivery"] = json.loads(compose("run", "--rm", "--no-deps", "-T", "worker",
        "python", "-", run, capture=True, stdin=script))
    assert sql(f"SELECT status,(SELECT count(*) FROM attempts WHERE run_id=r.id) FROM runs r WHERE id='{run}'") == "SUCCEEDED|1"
    if recovery is None:
        from check_recovery import check_recovery
        report["recovery"] = check_recovery(base, f"/v1/tenants/{tenant}/runs/{run}", token, tool_version, compose, sql)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Benchmark verified and saved to {output_path}", flush=True)
