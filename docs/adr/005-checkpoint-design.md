# ADR 005 · Checkpoint at committed boundaries

**Decision.** Take a versioned checkpoint when persisting a model response, a tool result, an approval wait and an approval resolution. Include the next instruction, next step ordinal, referenced event sequence and minimal continuation state. Keep large results in their step rows and read the pinned agent version through the run.

Snapshotting only at run end loses intermediate results. Serializing an entire live interpreter introduces deployment-version coupling and can capture secrets and sockets. Explicit checkpoints let a replacement worker reconstruct only the application state it needs.

**Consequence.** A release must retain decoders for active checkpoint versions or supply a tested migration. Before recovery, validate references and consult the dispatch ledger for any later in-flight operation. A checkpoint cannot establish the outcome of a remote call on its own. [Recovery protocol](../architecture/crash-recovery.md)
