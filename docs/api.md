# Initial HTTP contract

All product routes below are relative to `/v1/tenants/{tenant_id}`. Accept a bearer access token, verify the caller's membership in the path tenant, and apply the role check before accessing a resource. IDs are UUIDs and times are UTC RFC 3339 strings. JSON request models reject unknown fields. Responses include a request ID. Cursor lists return `{"items":[],"next_cursor":null}`, sorted by creation time then ID descending, with a default limit of 50 and maximum of 100.

| Method and path | Request | Success |
| --- | --- | --- |
| POST /agents | Developer sends name (1–128 characters) and optional description | 201 agent with ID, name, description and creation time. Duplicate name returns 409. |
| GET /agents, GET /agents/{id} | Viewer, optional list cursor | 200 list or agent with links to its versions and runs. |
| POST /agents/{id}/versions | Developer sends instructions, provider, model, limits and tool version IDs | 201 version with server-assigned number and DRAFT state. Validate provider config, budgets and tenant tool ownership. |
| GET /agents/{id}/versions, GET /agents/{id}/versions/{version_id} | Viewer | 200 version list or configuration. |
| POST /agents/{id}/versions/{version_id}/publish or /deactivate | Developer, empty body | 200 with resulting publication state. Repeat of same action returns current state. Invalid lifecycle transition returns 409. |
| POST /runs | Developer sends payload below and required Idempotency-Key header | 201 with Location header after durable commit. Identical replay returns 200 and same run ID. |
| GET /runs | Viewer, optional status, agent_id, agent_version_id, cursor, limit | 200 scoped list. Bind cursor to filters and tenant. |
| GET /runs/{id} | Viewer | 200 with version, state, current step, attempt, timestamps, output or failure, usage summary and authorized history links. |
| POST /runs/{id}/cancel | Developer, empty body | 202 while cancellation is pending, 200 if already cancelled, 409 for another terminal state. |
| GET /runs/{id}/events | Viewer, Accept: text/event-stream, optional Last-Event-ID | 200 SSE with persisted per-run sequence as event ID. Read strictly after cursor, then tail the same ordered store. Heartbeats carry no cursor. |
| GET /approvals, GET /approvals/{id} | Viewer, optional state filter on list | 200 with validated action, digest, risk, expiry and decision status. Redact sensitive fields according to role. |
| POST /approvals/{id}/decision | Approver sends decision, action_digest and optional reason | 200 decision receipt. Identical replay returns original receipt. Changed digest or conflicting decision returns 409. |
| GET /workers, GET /usage | Viewer, scoped to tenant | 200 tenant-visible worker activity or usage aggregates. Cross-tenant capacity and workload details are operator-only. |

Phase 1 implements unscoped `GET /health/live` (200, `{"status":"alive"}`) and `GET /health/ready` (200 ready or 503 unavailable). Readiness includes a `checks` object with `postgresql` and `redis` set to `up` or `down`. PostgreSQL must match the application's Alembic head. The gateway also exposes `GET /health/jetstream` as a separate broker probe. API requests receive an `X-Request-ID` UUID. The current API specification is served at `/openapi.json`.

The registry supports version creation, publication and deactivation. Runs support idempotent submission, reads, filters and cancellation. Run history returns an ordered JSON array with `after` (exclusive sequence) and `limit` parameters. A queued cancellation completes immediately. Cancellation of a running receipt returns 202 until the current owner or recovery loop finalizes it. Streaming, approvals and usage projections arrive in later phases. [PowerShell walkthrough](operations/first-run.md)

Phase 3 implements `GET /workers` as a cursor list of tenant-visible sessions. `GET /runs/{id}/attempts` returns attempts in generation order with worker ID, start/end time and outcome. Both routes require verified membership. A published version with provider `builtin` and model `receipt` executes on the worker pool. [Worker operations](operations/worker-failure-runbook.md)

## Publish and submit

After creating the agent, post a version configuration and publish the returned version ID. Example configuration for the deterministic provider:

```json
{
  "instructions": "Summarize the supplied text.",
  "provider": "mock",
  "model": "deterministic",
  "tool_version_ids": [],
  "limits": {"max_steps": 5, "max_tokens": 2048, "timeout_seconds": 60}
}
```

Submit a run with `Idempotency-Key: summarize-42`. The key is an opaque string of 1–128 characters. Priority defaults to 0 and ranges from 0 to 9. The effective deadline is the earlier of the supplied deadline and the version's maximum duration from submission.

```json
{
  "agent_version_id": "00000000-0000-4000-8000-000000000001",
  "input": {"text": "The worker recovered from its last checkpoint."},
  "priority": 0
}
```

```json
{
  "id": "00000000-0000-4000-8000-000000000002",
  "agent_version_id": "00000000-0000-4000-8000-000000000001",
  "status": "QUEUED",
  "created_at": "2026-11-22T10:00:00Z"
}
```

Hash the canonical, validated submission including version, input, priority and caller-supplied deadline after applying stable defaults. Exclude server-generated timestamps. Look up the tenant/key before checking publication state or recomputing a relative deadline for a replay. A unique constraint resolves concurrent inserts. A reused key with a different request yields 409 `idempotency_conflict`.

## Errors and event resumption

```json
{"error":{"code":"idempotency_conflict","message":"The key belongs to a different submission.","request_id":"req-123"}}
```

Use 401 for invalid credentials, 403 for insufficient role, 404 for missing or other-tenant objects, 409 for conflicts, 422 for invalid input, 413 for excess payload, 429 with Retry-After for rate limits, and 503 for a required unavailable dependency. A malformed or future event cursor is invalid. An expired history cursor returns 410 before opening the stream. Clients reconnect with the last processed event ID and ignore previously processed sequences. Event data includes type, timestamp and redacted payload.
