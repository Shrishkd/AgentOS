# AgentOS

AgentOS is a runtime under development for long-running AI agents. Its design pins each task to an agent version, records worker progress in PostgreSQL and pauses protected actions for human approval.

**Phase 2 · Agent registry and run management.** Start the local stack from this directory.

```sh
docker compose up --build
```

Open the [system health dashboard](http://127.0.0.1:8080) or [API reference](http://127.0.0.1:8080/docs). The tenant API supports immutable agent configurations, publication, idempotent run submission, ordered history and cancellation. Runs remain queued until a worker claims them in Phase 3. [Register an agent and submit your first run](docs/operations/first-run.md) · [Local setup and troubleshooting](docs/operations/local-development.md)

| Start here | What to review |
| --- | --- |
| [Technical specification](docs/architecture/overview.md) | System boundaries, guarantees and the path of a run |
| [Execution lifecycle](docs/architecture/execution-lifecycle.md) | State transitions, scheduling and transaction boundaries |
| [Crash recovery](docs/architecture/crash-recovery.md) | What happens when a worker dies before or after an external call |
| [Database design](docs/architecture/database-design.md) | Tables, tenant isolation, constraints and indexes |
| [API contract](docs/api.md) | Registering an agent, submitting work and following its history |
| [Threat model](docs/architecture/security.md) | Who can authorize actions and where untrusted data enters |
| [Implementation backlog](docs/backlog.md) | Dependencies, acceptance tests and the phase handoffs |

The highest-impact decision to review is how to handle a tool that times out after accepting a request. The design preserves the operation identity and records an unresolved outcome when the tool cannot safely deduplicate or reconcile the request. [Execution guarantees](docs/adr/004-execution-guarantees.md)
