"""Worker sessions, fenced leases, attempts and restartable outbox publication."""

from alembic import op

revision = "0003_workers"
down_revision = "0002_registry_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE tenants ADD concurrency_limit integer NOT NULL DEFAULT 4 CHECK (concurrency_limit>0),
        ADD last_admitted_at timestamptz""")
    op.execute("""ALTER TABLE outbox_events ADD publish_token uuid, ADD claim_until timestamptz,
        ADD publish_attempts integer NOT NULL DEFAULT 0""")
    op.execute("""CREATE TABLE workers (
        id uuid PRIMARY KEY, capacity integer NOT NULL CHECK (capacity>0),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(), healthy_until timestamptz NOT NULL,
        draining boolean NOT NULL DEFAULT false, stopped_at timestamptz)""")
    op.execute("""CREATE TABLE worker_leases (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, run_id uuid NOT NULL UNIQUE,
        owner_id uuid REFERENCES workers, generation integer NOT NULL CHECK (generation>0),
        renewed_at timestamptz NOT NULL DEFAULT clock_timestamp(), expires_at timestamptz,
        CHECK ((owner_id IS NULL) = (expires_at IS NULL)),
        FOREIGN KEY (tenant_id,run_id) REFERENCES runs (tenant_id,id))""")
    op.execute("""CREATE TABLE attempts (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, run_id uuid NOT NULL,
        worker_id uuid NOT NULL REFERENCES workers, number integer NOT NULL CHECK (number>0),
        started_at timestamptz NOT NULL DEFAULT clock_timestamp(), ended_at timestamptz, outcome text,
        CHECK ((ended_at IS NULL) = (outcome IS NULL)), UNIQUE(run_id,number),
        FOREIGN KEY (tenant_id,run_id) REFERENCES runs (tenant_id,id))""")
    op.execute("CREATE UNIQUE INDEX one_open_attempt ON attempts(run_id) WHERE ended_at IS NULL")
    op.execute("CREATE INDEX active_leases ON worker_leases(owner_id,expires_at) WHERE owner_id IS NOT NULL")
    op.execute("CREATE INDEX tenant_leases ON worker_leases(tenant_id,expires_at) WHERE owner_id IS NOT NULL")
    op.execute("CREATE INDEX worker_activity ON attempts(tenant_id,worker_id,started_at DESC)")
    for table in ("worker_leases", "attempts"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY tenant_read ON {table} FOR SELECT TO agentos_api USING
            (tenant_id=nullif(current_setting('agentos.tenant_id',true),'')::uuid)""")
    for table in ("tenants", "agent_versions", "runs", "outbox_events", "run_events", "worker_leases", "attempts"):
        op.execute(f"CREATE POLICY runtime ON {table} TO agentos_worker USING (true) WITH CHECK (true)")
    op.execute("GRANT SELECT ON tenants,agent_versions,runs,workers,worker_leases,attempts TO agentos_worker")
    op.execute("GRANT UPDATE(last_admitted_at) ON tenants TO agentos_worker")
    op.execute("GRANT UPDATE(status,revision,updated_at,enqueued_at,output,failure_reason) ON runs TO agentos_worker")
    op.execute("GRANT INSERT ON run_events,outbox_events TO agentos_worker")
    op.execute("GRANT INSERT,UPDATE ON workers,worker_leases,attempts TO agentos_worker")
    op.execute("GRANT SELECT ON attempts,worker_leases TO agentos_api")
    op.execute("CREATE POLICY publish ON outbox_events TO agentos_publisher USING (true) WITH CHECK (true)")
    op.execute("GRANT SELECT,UPDATE(publish_token,claim_until,published_at,publish_attempts) ON outbox_events TO agentos_publisher")
    # This projection intentionally exposes only workers with activity inside the verified tenant.
    op.execute("""CREATE VIEW tenant_workers WITH (security_barrier=true) AS
        SELECT a.tenant_id,w.id,w.created_at,w.heartbeat_at,w.healthy_until,w.draining,w.stopped_at,
            count(*) FILTER (WHERE a.ended_at IS NULL) AS open_attempts
        FROM workers w JOIN attempts a ON w.id=a.worker_id
        WHERE a.tenant_id=nullif(current_setting('agentos.tenant_id',true),'')::uuid
        GROUP BY a.tenant_id,w.id""")
    op.execute("GRANT SELECT ON tenant_workers TO agentos_api")
    op.execute("""CREATE VIEW receipt_queue WITH (security_invoker=true) AS
        SELECT r.id,r.tenant_id,r.priority,r.enqueued_at,r.revision FROM runs r
        JOIN agent_versions v ON v.id=r.agent_version_id
        WHERE r.status='QUEUED' AND v.provider='builtin' AND v.model='receipt'""")
    op.execute("GRANT SELECT ON receipt_queue TO agentos_worker")
    op.execute("""CREATE VIEW wakeup_needed AS SELECT EXISTS (
        SELECT FROM runs q JOIN tenants t ON t.id=q.tenant_id JOIN agent_versions v ON v.id=q.agent_version_id
        WHERE q.status='QUEUED' AND v.provider='builtin' AND v.model='receipt' AND
        (SELECT count(*) FROM worker_leases l WHERE l.tenant_id=t.id AND l.expires_at>now())<t.concurrency_limit
    ) AND EXISTS (SELECT FROM workers w WHERE NOT w.draining AND w.healthy_until>now()
        AND (SELECT count(*) FROM worker_leases l WHERE l.owner_id=w.id AND l.expires_at>now())<w.capacity) AS needed""")
    op.execute("GRANT SELECT ON wakeup_needed TO agentos_publisher")


def downgrade() -> None:
    for view in ("wakeup_needed", "receipt_queue", "tenant_workers"):
        op.execute(f"DROP VIEW {view}")
    op.execute("DROP POLICY publish ON outbox_events")
    for table in ("tenants", "agent_versions", "runs", "outbox_events", "run_events"):
        op.execute(f"DROP POLICY runtime ON {table}")
        op.execute(f"REVOKE ALL ON {table} FROM agentos_worker")
    op.execute("REVOKE UPDATE(status,revision,updated_at,enqueued_at,output,failure_reason) ON runs FROM agentos_worker")
    op.execute("REVOKE ALL ON outbox_events FROM agentos_publisher")
    op.execute("REVOKE UPDATE(published_at) ON outbox_events FROM agentos_publisher")
    for table in ("attempts", "worker_leases", "workers"):
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE outbox_events DROP publish_token,DROP claim_until,DROP publish_attempts")
    op.execute("ALTER TABLE tenants DROP concurrency_limit,DROP last_admitted_at")
