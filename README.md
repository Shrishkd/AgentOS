# AgentOS

AgentOS is a runtime under development for long-running AI agents. Its design pins each task to an agent version, records worker progress in PostgreSQL and pauses protected actions for human approval.

**Phase 10 · Deployment.** The production Compose release serves HTTPS, drains workers during upgrades, routes operational alerts and recovers interrupted runs from PostgreSQL base backups and archived WAL. [Deployment and recovery runbook](docs/operations/production.md) · [Performance measurements](benchmarks/results.md)

Start the local stack with Grafana, Prometheus and Jaeger from this directory.

```sh
docker compose -f compose.yaml -f compose.observability.yaml up -d --build
```

Open the workspace and connect with a tenant ID and bearer token. In **Agents**, create an agent, save and publish a version, then start a run. Follow its live timeline, inspect step results, review protected actions, and compare model and tool usage. **System health** is available before sign-in. [Dashboard](http://127.0.0.1:8080) · [Get local credentials](docs/operations/first-run.md) · [API reference](http://127.0.0.1:8080/docs)

Workers resume interrupted model runs from committed checkpoints. The recovery suite kills service processes at known boundaries and verifies the replacement worker's output, attempts and usage. [Recovery walkthrough](docs/operations/worker-failure-runbook.md#verify-checkpoint-recovery)

| Start here | What to review |
| --- | --- |
| [Inspect an execution](docs/operations/observability.md) | Resume a live event stream, trace a retry across workers and reconcile token and cost totals |
| [Review a protected action](docs/operations/tool-approvals.md) | Install a tenant tool, inspect a pending report in the approval inbox and verify the resulting effect |
| [Technical specification](docs/architecture/overview.md) | System boundaries, guarantees and the path of a run |
| [Execution lifecycle](docs/architecture/execution-lifecycle.md) | State transitions, scheduling and transaction boundaries |
| [Crash recovery](docs/architecture/crash-recovery.md) | What happens when a worker dies before or after an external call |
| [Database design](docs/architecture/database-design.md) | Tables, tenant isolation, constraints and indexes |
| [API contract](docs/api.md) | Registering an agent, submitting work and following its history |
| [Threat model](docs/architecture/security.md) | Who can authorize actions and where untrusted data enters |
| [Implementation backlog](docs/backlog.md) | Dependencies, acceptance tests and the phase handoffs |

The highest-impact decision to review is how to handle a tool that times out after accepting a request. The design preserves the operation identity and records an unresolved outcome when the tool cannot safely deduplicate or reconcile the request. [Execution guarantees](docs/adr/004-execution-guarantees.md)
