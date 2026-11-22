# ADR 001 · Modular control plane

**Decision.** Use a FastAPI modular monolith with a shared Python domain package and independently replicated worker processes. SQLAlchemy async sessions define transaction boundaries. API, publisher and recovery loop have separate entry points so restarts and scaling are independent.

An all-in-one server would tie in-flight agents and concurrency to HTTP deployments. Separate registry, approvals and execution microservices would distribute a state transition across multiple databases or network calls before scale requires it. The modular design can commit related writes together while allocating CPU and provider connections per worker pool.

**Consequence.** Shared schema changes need compatible migrations and coordinated rollouts. Consider extracting a module when profiling or a distinct trust boundary justifies its separate operation. [Component boundaries](../architecture/component-design.md)
