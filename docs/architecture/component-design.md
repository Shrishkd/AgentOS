# Component boundaries

| Module | Owns | Contract with other modules |
| --- | --- | --- |
| API and identity | HTTP validation, verified identity, pagination and SSE | Resolves tenant membership before invoking an application command. Returns stable domain error codes. |
| Registry | Agents, immutable version configurations and publication | Resolves an explicit version for submission under a database lock. Deactivation prevents new submissions while existing runs retain the snapshot. |
| Execution manager | Run state, events, cancellation and deadlines | Sole entry point for transitions. Locks the run and applies its guards inside the caller's transaction. |
| Scheduler and leases | Fair selection, reservations, worker sessions and fencing | Claims one due run for a free worker slot. Returns run ID and generation. Releases capacity transactionally at each exit from execution. |
| Outbox and timers | Wakeup publication, retry promotion and lease recovery | Polls due rows in bounded batches. Emits identifier-only messages. Safe to restart. |
| Worker runtime | Pull loop, local concurrency, renewal and draining | Gets a reservation, drives the engine and stops progress on ownership loss. |
| Engine and checkpoints | Step identity, next instruction and schema-versioned cursor | Reads durable input and delegates model/tool I/O outside database transactions. Commits results under the lease guard. |
| Provider adapter | OpenAI-compatible HTTP and deterministic mock implementations | Receives bounded input and timeout. Returns validated structured action or final result, usage, latency and provider request ID. Normalizes transient and permanent errors. |
| Tool gateway | Registered implementations, schemas, allowlists, risk and execution | Resolves a pinned tool version, authorizes the operation, applies time/output limits and returns a typed result. |
| Approval manager | Pending action and human decision | Locks the run and proposal, verifies digest and approver role, consumes a decision once and records an event. |
| Read models and telemetry | Timeline, usage summaries, worker health, traces and metrics | Uses tenant-scoped projections for the dashboard. Prometheus series use bounded labels. Per-run identifiers go into traces and structured logs. |

FastAPI handlers and worker entry points both call typed application services. SQLAlchemy repositories participate in the same async unit of work for a transition. Alembic owns schema evolution. Infrastructure adapters implement the database, JetStream, Redis, provider and registered-tool interfaces. The engine receives those interfaces so deterministic tests can control individual failures.

The initial tools are a bounded arithmetic parser, a web search adapter, an egress-filtered HTTP GET adapter, parameterized read-only queries against a controlled database, and a structured report renderer. A tool version declares its JSON schema, required permission, risk, timeout, output limit and idempotency capability. Only deployed implementations can be bound to registry entries. [Threat model](security.md)
