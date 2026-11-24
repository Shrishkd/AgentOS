"""Tenant tools, immutable approval proposals and structured reports."""

from alembic import op

revision = "0006_tool_approvals"
down_revision = "0005_recovery"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE steps ADD dispatch_count integer NOT NULL DEFAULT 0, ADD last_dispatch_error text")
    op.execute("""CREATE TABLE approvals (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, run_id uuid NOT NULL,
        step_id uuid NOT NULL UNIQUE, tool_version_id uuid NOT NULL, tool_name text NOT NULL,
        arguments jsonb NOT NULL, action_digest text NOT NULL, risk text NOT NULL,
        status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','expired','cancelled')),
        expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        decided_by uuid, decided_at timestamptz, reason text, wait_seq integer NOT NULL, resolution_seq integer,
        FOREIGN KEY(tenant_id,run_id,step_id) REFERENCES steps(tenant_id,run_id,id),
        FOREIGN KEY(tenant_id,tool_version_id) REFERENCES tool_registry(tenant_id,id),
        FOREIGN KEY(tenant_id,decided_by) REFERENCES users(tenant_id,id),
        FOREIGN KEY(run_id,wait_seq) REFERENCES run_events(run_id,seq) DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY(run_id,resolution_seq) REFERENCES run_events(run_id,seq) DEFERRABLE INITIALLY DEFERRED)""")
    op.execute("CREATE INDEX approval_inbox ON approvals(tenant_id,status,created_at DESC,id DESC)")
    op.execute("CREATE INDEX approval_expiry ON approvals(expires_at,run_id) WHERE status='pending'")
    op.execute("""CREATE TABLE tool_reports (
        id uuid PRIMARY KEY, tenant_id uuid NOT NULL, run_id uuid NOT NULL, document jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY(tenant_id,run_id,id) REFERENCES steps(tenant_id,run_id,id))""")
    for table in ("approvals", "tool_reports"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY tenant_scope ON {table} TO agentos_api USING
            (tenant_id=nullif(current_setting('agentos.tenant_id',true),'')::uuid)""")
        op.execute(f"CREATE POLICY runtime ON {table} TO agentos_worker USING(true) WITH CHECK(true)")
        op.execute(f"GRANT SELECT ON {table} TO agentos_api,agentos_worker")
    op.execute("GRANT INSERT ON tool_reports TO agentos_worker")
    op.execute("""GRANT INSERT(tenant_id,run_id,step_id,tool_version_id,tool_name,arguments,action_digest,risk,
        expires_at,wait_seq) ON approvals TO agentos_worker""")
    op.execute("GRANT UPDATE(status,decided_by,decided_at,reason,resolution_seq) ON approvals TO agentos_api")
    op.execute("GRANT INSERT,UPDATE(enabled,updated_at) ON tool_registry TO agentos_api")
    op.execute("GRANT UPDATE(updated_at) ON tool_registry TO agentos_worker")
    op.execute("CREATE POLICY dispatch_lock ON tool_registry FOR UPDATE TO agentos_worker USING(true) WITH CHECK(false)")
    # Run transitions and proposal invalidation are visible together even on the deadline/cancel paths.
    op.execute("""CREATE FUNCTION end_approval() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
        SET search_path=pg_catalog,public AS $$
        BEGIN
            UPDATE public.approvals SET status=CASE WHEN NEW.status='CANCELLED' THEN 'cancelled' ELSE 'expired' END,
                resolution_seq=NEW.revision WHERE run_id=NEW.id AND status='pending';
            IF NEW.status='FAILED' THEN
                UPDATE public.steps s SET status='failed', ended_at=clock_timestamp(),
                    error_code=CASE WHEN a.status='rejected' THEN 'approval_rejected' ELSE 'approval_expired' END
                    FROM public.approvals a WHERE s.id=a.step_id AND a.run_id=NEW.id AND s.status='pending'
                    AND a.status IN ('expired','rejected');
            END IF;
            RETURN NULL;
        END $$""")
    op.execute("REVOKE ALL ON FUNCTION end_approval() FROM PUBLIC")
    op.execute("""CREATE TRIGGER end_approval AFTER UPDATE OF status ON runs FOR EACH ROW
        WHEN (NEW.status IN ('FAILED','CANCELLED','TIMED_OUT') AND OLD.status IS DISTINCT FROM NEW.status)
        EXECUTE FUNCTION end_approval()""")


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM runs WHERE status='WAITING_FOR_APPROVAL')
        THEN RAISE EXCEPTION 'Resolve or cancel waiting approvals before downgrade'; END IF; END $$""")
    op.execute("DROP TRIGGER end_approval ON runs")
    op.execute("DROP FUNCTION end_approval()")
    op.execute("REVOKE UPDATE(updated_at) ON tool_registry FROM agentos_worker")
    op.execute("DROP POLICY dispatch_lock ON tool_registry")
    op.execute("REVOKE INSERT,UPDATE(enabled,updated_at) ON tool_registry FROM agentos_api")
    op.execute("DROP TABLE tool_reports")
    op.execute("DROP TABLE approvals")
    op.execute("ALTER TABLE steps DROP dispatch_count, DROP last_dispatch_error")
