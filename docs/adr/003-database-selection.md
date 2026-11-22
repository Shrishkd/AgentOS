# ADR 003 · PostgreSQL owns durable state

**Decision.** Store tenant data, execution state, worker ownership, approvals, events and outbox rows in PostgreSQL. Use relational constraints for identities and grants and JSONB for validated versioned payloads. Redis supplies expiring rate-limit counters and cache entries.

A Redis-only queue and state store would couple recovery to cache persistence and require a separate design for referential integrity and audit queries. Splitting event history from mutable state across databases would add a consistency boundary at every step. PostgreSQL lets a single commit make state, history and dispatch intent visible together.

**Consequence.** Database availability bounds write availability. Keep external calls outside transactions, monitor lock waits and outbox age, and test backups by restoring a live checkpoint chain. [Schema and tenant constraints](../architecture/database-design.md)
