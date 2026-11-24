"""Persist model and tool steps, invocation reservations and worker capabilities."""

from alembic import op

revision = "0004_model_runtime"
down_revision = "0003_workers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE workers ADD remote_model text")
    op.execute("""CREATE TABLE steps (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, run_id uuid NOT NULL,
        ordinal integer NOT NULL CHECK (ordinal>0), kind text NOT NULL CHECK (kind IN ('model','tool')),
        status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','completed','failed')),
        request jsonb NOT NULL, result jsonb, error_code text, tool_version_id uuid,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(), ended_at timestamptz,
        UNIQUE(run_id,ordinal), UNIQUE(tenant_id,run_id,id),
        FOREIGN KEY(tenant_id,run_id) REFERENCES runs(tenant_id,id),
        FOREIGN KEY(tenant_id,tool_version_id) REFERENCES tool_registry(tenant_id,id))""")
    op.execute("""CREATE TABLE usage_records (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, run_id uuid NOT NULL,
        step_id uuid NOT NULL, invocation integer NOT NULL CHECK (invocation>0),
        provider text NOT NULL, model text NOT NULL, status text NOT NULL DEFAULT 'reserved'
            CHECK (status IN ('reserved','reported','unknown','rejected')),
        reserved_input integer NOT NULL, reserved_output integer NOT NULL,
        input_rate numeric, output_rate numeric, reserved_cost numeric,
        input_tokens integer, output_tokens integer, estimated_cost numeric,
        latency_ms integer, provider_request_id text, error_code text,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(step_id,invocation),
        FOREIGN KEY(tenant_id,run_id,step_id) REFERENCES steps(tenant_id,run_id,id))""")
    op.execute("CREATE INDEX usage_by_run ON usage_records(tenant_id,run_id,created_at)")
    for table in ("steps", "usage_records"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY tenant_read ON {table} FOR SELECT TO agentos_api USING
            (tenant_id=nullif(current_setting('agentos.tenant_id',true),'')::uuid)""")
        op.execute(f"CREATE POLICY runtime ON {table} TO agentos_worker USING(true) WITH CHECK(true)")
        op.execute(f"GRANT SELECT ON {table} TO agentos_api")
        op.execute(f"GRANT SELECT,INSERT,UPDATE ON {table} TO agentos_worker")
    for table in ("version_tools", "tool_registry"):
        op.execute(f"CREATE POLICY worker_read ON {table} FOR SELECT TO agentos_worker USING(true)")
        op.execute(f"GRANT SELECT ON {table} TO agentos_worker")
    op.execute("""CREATE VIEW execution_queue WITH (security_invoker=true) AS
        SELECT r.id,r.tenant_id,r.priority,r.enqueued_at,v.provider,v.model FROM runs r JOIN agent_versions v
        ON v.id=r.agent_version_id WHERE r.status='QUEUED'""")
    op.execute("GRANT SELECT ON execution_queue TO agentos_worker")
    op.execute("""CREATE OR REPLACE VIEW wakeup_needed AS SELECT EXISTS (
        SELECT FROM runs r JOIN agent_versions v ON v.id=r.agent_version_id JOIN tenants t ON t.id=r.tenant_id
        WHERE r.status='QUEUED' AND (SELECT count(*) FROM worker_leases l WHERE l.tenant_id=t.id
            AND l.expires_at>now())<t.concurrency_limit AND EXISTS (
            SELECT FROM workers w WHERE NOT w.draining AND w.healthy_until>now()
            AND (v.provider IN ('builtin','mock') OR (v.provider='openai-compatible' AND v.model=w.remote_model))
            AND (SELECT count(*) FROM worker_leases l WHERE l.owner_id=w.id AND l.expires_at>now())<w.capacity)
        ) AS needed""")


def downgrade() -> None:
    op.execute("""CREATE OR REPLACE VIEW wakeup_needed AS SELECT EXISTS (
        SELECT FROM runs q JOIN tenants t ON t.id=q.tenant_id JOIN agent_versions v ON v.id=q.agent_version_id
        WHERE q.status='QUEUED' AND v.provider='builtin' AND v.model='receipt' AND
        (SELECT count(*) FROM worker_leases l WHERE l.tenant_id=t.id AND l.expires_at>now())<t.concurrency_limit
    ) AND EXISTS (SELECT FROM workers w WHERE NOT w.draining AND w.healthy_until>now()
        AND (SELECT count(*) FROM worker_leases l WHERE l.owner_id=w.id AND l.expires_at>now())<w.capacity) AS needed""")
    op.execute("DROP VIEW execution_queue")
    for table in ("version_tools", "tool_registry"):
        op.execute(f"DROP POLICY worker_read ON {table}")
        op.execute(f"REVOKE SELECT ON {table} FROM agentos_worker")
    op.execute("DROP TABLE usage_records")
    op.execute("DROP TABLE steps")
    op.execute("ALTER TABLE workers DROP remote_model")
