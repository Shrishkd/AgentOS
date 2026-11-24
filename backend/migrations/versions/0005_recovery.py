"""Versioned step boundaries and durable retry timers."""

from alembic import op

revision = "0005_recovery"
down_revision = "0004_model_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE runs ADD retry_at timestamptz")
    op.execute("GRANT UPDATE(retry_at) ON runs TO agentos_worker")
    op.execute("CREATE INDEX due_retries ON runs(retry_at) WHERE status='RETRY_WAIT'")
    op.execute("""CREATE INDEX inactive_deadlines ON runs(deadline_at)
        WHERE status IN ('QUEUED','RETRY_WAIT','WAITING_FOR_APPROVAL')""")
    op.execute("""CREATE TABLE checkpoints (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL, run_id uuid NOT NULL,
        agent_version_id uuid NOT NULL, step_id uuid NOT NULL, ordinal integer NOT NULL CHECK (ordinal>0),
        schema_version integer NOT NULL CHECK (schema_version>0), next_action text NOT NULL,
        next_ordinal integer NOT NULL CHECK (next_ordinal=ordinal+1), event_seq integer NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(run_id,ordinal), UNIQUE(run_id,event_seq),
        FOREIGN KEY(tenant_id,agent_version_id) REFERENCES agent_versions(tenant_id,id),
        FOREIGN KEY(tenant_id,run_id,step_id) REFERENCES steps(tenant_id,run_id,id),
        FOREIGN KEY(run_id,event_seq) REFERENCES run_events(run_id,seq))""")
    op.execute("ALTER TABLE checkpoints ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE checkpoints FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_read ON checkpoints FOR SELECT TO agentos_api USING
        (tenant_id=nullif(current_setting('agentos.tenant_id',true),'')::uuid)""")
    op.execute("CREATE POLICY runtime ON checkpoints TO agentos_worker USING(true) WITH CHECK(true)")
    op.execute("GRANT SELECT ON checkpoints TO agentos_api")
    op.execute("GRANT SELECT,INSERT ON checkpoints TO agentos_worker")
    op.execute("GRANT SELECT(run_id,seq,kind,payload) ON run_events TO agentos_worker")
    # API cancellation can finish a waiting run while its worker slot is already free.
    op.execute("""CREATE FUNCTION close_interrupted_steps() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
        BEGIN
            UPDATE public.steps SET status='failed', ended_at=clock_timestamp(), error_code=NEW.status
            WHERE tenant_id=NEW.tenant_id AND run_id=NEW.id AND status='pending';
            UPDATE public.usage_records SET status='unknown',error_code=NEW.status
            WHERE tenant_id=NEW.tenant_id AND run_id=NEW.id AND status='reserved';
            RETURN NULL;
        END $$""")
    op.execute("REVOKE ALL ON FUNCTION close_interrupted_steps() FROM PUBLIC")
    op.execute("""CREATE TRIGGER close_interrupted_steps AFTER UPDATE OF status ON runs FOR EACH ROW
        WHEN (NEW.status IN ('CANCELLED','TIMED_OUT') AND OLD.status IS DISTINCT FROM NEW.status)
        EXECUTE FUNCTION close_interrupted_steps()""")


def downgrade() -> None:
    # Older workers do not promote timers. Preserve active retries and their enqueue notifications.
    op.execute("""WITH promoted AS (UPDATE runs SET status='QUEUED', revision=revision+1,
        updated_at=clock_timestamp(), enqueued_at=clock_timestamp() WHERE status='RETRY_WAIT'
        RETURNING tenant_id,id,revision), events AS (INSERT INTO run_events(tenant_id,run_id,seq,kind,payload)
        SELECT tenant_id,id,revision,'run.state','{"status":"QUEUED"}'::jsonb FROM promoted)
        INSERT INTO outbox_events(tenant_id,run_id,kind) SELECT tenant_id,id,'run.queued' FROM promoted""")
    op.execute("DROP TRIGGER close_interrupted_steps ON runs")
    op.execute("DROP FUNCTION close_interrupted_steps()")
    op.execute("DROP TABLE checkpoints")
    op.execute("REVOKE SELECT(run_id,seq,kind,payload) ON run_events FROM agentos_worker")
    op.execute("DROP INDEX inactive_deadlines")
    op.execute("ALTER TABLE runs DROP COLUMN retry_at")
