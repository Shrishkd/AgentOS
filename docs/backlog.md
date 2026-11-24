# Implementation backlog

Each row is a reviewable increment. Phase numbers identify handoff points. Acceptance criteria describe evidence to collect as implementation lands.

Phases 0–5 cover design, foundation, registry, distributed workers, model execution and checkpoint recovery. The Compose suite interrupts live workers before dispatch, after an external tool effect and after a final checkpoint. It also tests lost API replies and lost publish acknowledgements. Phase 6 adds the registered tool gateway and human approvals. [Run a model-backed agent](operations/model-runtime.md) · [Reproduce worker recovery](operations/worker-failure-runbook.md#verify-checkpoint-recovery)

| Phase / increment | Depends on | Work and acceptance criteria |
| --- | --- | --- |
| 0 · Architecture | Empty repository and brief | Review the technical specification, six diagrams, schema, state machine, threat model, HTTP contract and five ADRs. Check cross-document links and example payloads. |
| 1a · Backend skeleton | 0 | Pin Python and dependencies, define FastAPI settings, structured logging, request IDs and health endpoints. Test missing config, redacted secrets and readiness failure. |
| 1b · Compose and migrations | 1a | Add PostgreSQL, Redis and persistent JetStream, Alembic job and an API container. From a clean volume, start with one command. Verify readiness, restart with existing volumes, re-run migrations and test dependency outage. |
| 1c · Dashboard shell and CI | 1b | React/TypeScript/Vite app, Tailwind, accessible component library and TanStack Query read API health. CI builds both apps and tests Compose startup. Publish local setup, env reference and troubleshooting steps. |
| 2a · Identity and registry | 1 | Implement tenant-scoped auth, database roles, RLS, agent versions, permission grants and publication rules. Test creation, immutable publication, deactivation and attempted cross-tenant access. |
| 2b · Submit and inspect | 2a | Implement transition service, run reads/lists, idempotent create, run events and enqueue outbox write in one transaction. Race two equal submissions and two different payloads sharing a key. Verify original version survives deactivation. |
| 3a · Publish and claim | 2b | Add restartable outbox publisher, durable pull consumer, fair admission and worker sessions. Run two worker processes with limited slots and competing tenants. Verify quota, priority, fairness and one live claim per run. |
| 3b · Maintain ownership | 3a | Renew leases using DB time, enforce fencing, drain workers and expose health. Reject an expired owner's writes and show a graceful stop completes or releases work. |
| 4a · Model step | 3 | Implement mock and configurable OpenAI-compatible adapters. Reserve budgets, persist intent and response, validate outputs and normalize errors. Test timeout, rate limit, budget exhaustion and recorded token usage without external keys. |
| 4b · Execution loop | 4a | Handle typed final answers and tool proposals with a safe deterministic tool fixture, bounded steps and time limits. Test single-step and multi-step runs and invalid tool schema. |
| 5a · Restore durable boundaries | 4 | Implement versioned checkpoints, recovery reaper, retry timers, deadlines and cancellation. Kill a worker before dispatch, after remote completion and after result commit. Verify the replacement reads the correct cursor. |
| 5b · Outage and duplicate tests | 5a | Exercise every row in the recovery matrix with PostgreSQL and JetStream containers. Demonstrate an API crash after commit, broker outage, database loss, lost acknowledgements and worker restart. Verify deduplicating test tool commits one external effect and unknown outcomes stop. |
| 6a · Registered tool gateway | 5 | Implement versioned built-ins, JSON schemas, permissions, protected egress, output limits and audit. Test unauthorized calls, SSRF cases, oversized responses, timeouts and read-only query isolation. |
| 6b · Human decision | 6a | Implement immutable proposals, approval wait/resume and UI. Test approval, denial, expiry, changed arguments, revoked access and duplicate/conflicting decisions. No protected dispatch occurs without matching authorization. |
| 7 · Observability | 5, 6 | Add OTel spans, Prometheus metrics, Grafana dashboards, ordered SSE and usage summaries. A reconnect resumes at the next stored event. Trace one retry and reconcile dashboard totals against usage rows. |
| 8 · Dashboard | 7 | Ship overview, agents, runs, run timeline, approvals, workers, analytics and settings with real APIs, light/dark themes and accessible navigation. Exercise a complete approval journey and empty/error/loading states. |
| 9 · Reliability and performance | 8 | Run deterministic pytest unit, Testcontainers integration, browser end-to-end and k6 load suites. Record environment and raw results for 100 short runs, 100 concurrent submissions, ten long runs, competing workers, crash, duplicate delivery and provider timeout. Report throughput, latency percentiles, resource use, recovery, errors and costs. |
| 10 · Deployment | 9 | Build production images, secret handling, health checks, backup/restore, alerting and incident runbooks. Restore a backup, recover a running task, drain a release and document observed recovery time before introducing Kubernetes. |

The outbox row starts in Phase 2 and its publisher in Phase 3 because a runnable distributed worker needs a reliable enqueue path. Phase 5 proves that path under failure. Authentication and tenant constraints arrive alongside the first tenant-owned data. Keep database migrations with the features that introduce each table.

Phase 1's handoff is one documented Compose command that starts all foundation services on a clean machine with Docker, plus CI evidence of a healthy stack. [Deployment design](architecture/deployment.md)
