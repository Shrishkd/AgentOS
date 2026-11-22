# AgentOS

AgentOS is a proposed runtime for long-running AI agents. A developer publishes an agent version and submits a task. A worker executes it in steps, records progress in PostgreSQL, and hands control back to a human when an action needs approval.

**Project status.** Phase 0 defines the engineering design. Phase 1 will introduce the first runnable services and local startup command.

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
