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
4. Every two seconds the publisher checks for eligible work and spare capacity and can emit one repair wakeup. Workers renew long-running claims every quarter lease interval. A live worker scans for expired ownership, closes its attempt and requeues retriable receipts. Three expired attempts fail the run. A pending cancellation or elapsed deadline takes precedence.

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

The replay policy here applies to the side-effect-free receipt handler. Recovery for model calls and external tools needs the checkpoint and reconciliation work scheduled for Phase 5.
