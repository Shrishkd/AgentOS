# Worker operations

Start two independent processes against the same durable consumer.

```sh
docker compose up -d --build --scale worker=2 --wait
docker compose ps worker publisher
```

Submit a receipt run using the [first-run walkthrough](first-run.md). Inspect `GET /v1/tenants/{tenant_id}/runs/{id}/attempts` to see the session ID, attempt number, timestamps and outcome. `GET /v1/tenants/{tenant_id}/workers` shows sessions that have served the tenant, their heartbeat, health and the tenant's open attempts. Operators can inspect idle and historical sessions with this local command.

```sh
docker compose exec postgres psql -U postgres -d agentos -c "SELECT id, capacity, heartbeat_at, draining, stopped_at FROM workers ORDER BY created_at DESC LIMIT 20"
```

## Dispatch and ownership

1. The publisher reserves a due outbox row for five seconds, commits the reservation, then publishes its UUID to the file-backed AGENTOS stream. Only a JetStream publish acknowledgement permits the reservation owner to mark the row sent. Duplicate messages and expired reservations are safe to retry.
2. Each free slot pulls one wakeup from the shared WORKERS consumer. PostgreSQL picks the least recently served eligible tenant, then the highest priority run, breaking ties by queue time and ID. Locks and fresh capacity checks serialize competing claims. The worker acknowledges the wakeup after the database transaction finishes.
3. A claim records RUNNING, a new attempt and its lease in one transaction. A worker boot receives a new session UUID. The run retains its generation counter across ownership changes. A conditional release checks the owner, generation and database clock in the same transaction as the result and event.
4. Every two seconds the publisher checks for eligible work and spare capacity and can emit one repair wakeup. Workers renew long-running claims every quarter lease interval. A live worker scans for expired ownership, closes the expired attempt and schedules recovery after one second for the first crash and two seconds for the second. Three expired attempts fail the run. A pending cancellation or elapsed deadline takes precedence.

## Configuration and incidents

| Setting or symptom | Behavior and action |
| --- | --- |
| AGENTOS_WORKER_CAPACITY | Default 2 slots per process, range 1–32. Set it in the root .env and recreate workers. |
| AGENTOS_LEASE_SECONDS | Default 15 seconds, range 3–300. Session health and lease validity use database time. |
| Tenant concurrency | tenants.concurrency_limit defaults to 4. The scheduler checks both tenant and worker limits before every claim. |
| Broker unavailable | Inspect NATS and publisher logs. Submissions accumulate in outbox_events. Restore the broker and the publisher retries with the original event ID. A consumer also checks current run state on every wakeup. |
| Worker stale | Inspect its last heartbeat and open attempts. Start a healthy replica. After lease expiry it can reclaim the receipt with a higher generation. Late writes from the former owner roll back. |
| Run stays queued | Check its pinned provider/model, tenant quota, worker health and unpublished outbox age. Workers accept builtin/receipt and mock/deterministic. A remote model requires a worker advertising its exact name. [Model settings](model-runtime.md) |
| Planned shutdown | Send SIGTERM with docker compose stop worker. Each process stops pulling, marks its session draining, and gives active slots up to eight seconds to finish. Unfinished leases expire and can be reclaimed. |

The Compose credentials job generates separate database and NATS passwords for the publisher and workers. The runtime-logins job provisions restricted PostgreSQL logins before Alembic runs, including on an existing installation. Standalone deployments must supply these two logins, compatible NATS permissions, the `AGENTOS_DB_*` settings, `AGENTOS_BROKER_URL` and `AGENTOS_BROKER_PASSWORD_FILE`. Compose supplies the private broker URL and mounts the password at `/secrets/runtime/broker`.

## Verify checkpoint recovery

From the repository root, run the integration check with Docker running. It creates a separate Compose project, exercises the real API, publisher and worker code, and removes its test containers and volumes afterward.

```sh
python scripts/check_foundation.py
```

Each `Recovery` line identifies a run, the crash location and both attempt outcomes. The test checks the recorded step IDs, checkpoints, provider invocations and output. For a run in your own tenant, use the same bearer token and run URL as in the [model walkthrough](model-runtime.md).

```powershell
Invoke-RestMethod -Headers $headers -Uri "$url/attempts"
Invoke-RestMethod -Headers $headers -Uri "$url/checkpoints"
Invoke-RestMethod -Headers $headers -Uri "$url/steps"
(Invoke-RestMethod -Headers $headers -Uri "$url/usage").items
```

| Interruption | What to expect |
| --- | --- |
| Worker dies before model dispatch | The replacement starts the pending instruction under a higher lease generation. |
| Worker dies after a model or tool result commits | The step, checkpoint and history event share a transaction. The replacement restores the pinned conversation. A final checkpoint lets it publish the saved answer directly. |
| Provider replied but the worker died before saving the reply | The outstanding reservation becomes unknown. The run fails with `model_outcome_unknown` and retains the reservation for accounting. |
| Tool reply is lost after a keyed effect | The step UUID survives the handoff. The test service returns the original result on the second request with that UUID. The production text-length tool is safe to recompute. |
| Provider requests backoff | Inspect `retry_at` on the run. A worker promotes the due retry and writes its outbox event atomically. A waiting run can be cancelled or reach its deadline with its slot free. |
| A publisher loses its acknowledgement | The outbox reservation expires. Publication retries with the same event UUID. A duplicate wakeup cannot reopen a completed run. |
| PostgreSQL is unavailable during renewal | The worker loses the ability to commit progress. After storage returns, an expired lease can be reclaimed and a saved answer reused. |

Checkpoints use schema version 1. On recovery the worker verifies the pinned version, step, event and continuation. It can rebuild a damaged version-1 cursor or a pre-Phase-5 conversation from contiguous completed steps with recorded model usage. An inconsistent history fails with `inconsistent_step_history`. An unknown schema version fails with `unsupported_checkpoint_version`. Checkpoint history begins with results written under migration `0005_recovery`; historical Phase 4 runs retain their original step and usage records.
