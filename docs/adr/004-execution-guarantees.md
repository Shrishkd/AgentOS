# ADR 004 · At-least-once delivery and fenced commits

**Decision.** Expect duplicate delivery. A database lease with a monotonically increasing generation guards every worker commit. Record an operation identity before external dispatch and reuse it on recovery. A unique step plus transactionally recorded results ensure a single committed logical completion.

Holding a database lock across network I/O would consume connections and still leave uncertainty if the remote service succeeds before the local process crashes. A broker acknowledgement alone cannot establish whether an external tool applied a change.

**Consequence.** Effectively-once external effects depend on a tool honoring a stable key over the full retry window or offering authoritative outcome reconciliation. Otherwise an ambiguous side effect stops automatic execution and is surfaced for review. Provider retries may incur additional cost. [Failure matrix](../architecture/crash-recovery.md)
