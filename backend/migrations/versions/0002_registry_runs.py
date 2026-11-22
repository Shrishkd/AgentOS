"""Tenant registry, immutable versions and durable run admission."""

from alembic import op

revision = "0002_registry_runs"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None

TABLES = ("tenants", "users", "agents", "tool_registry", "agent_versions",
          "version_tools", "runs", "run_events", "outbox_events", "audit_events")


def upgrade() -> None:
    op.execute("""CREATE TABLE tenants (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), slug text NOT NULL UNIQUE,
        pending_limit integer NOT NULL DEFAULT 1000 CHECK (pending_limit > 0),
        submissions_per_minute integer NOT NULL DEFAULT 120 CHECK (submissions_per_minute > 0),
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("""CREATE TABLE users (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
        issuer text NOT NULL, subject text NOT NULL, role text NOT NULL
            CHECK (role IN ('viewer','developer','approver','admin')), enabled boolean NOT NULL DEFAULT true,
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, id), UNIQUE (tenant_id, issuer, subject)
    )""")
    op.execute("""CREATE TABLE agents (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
        name text NOT NULL, description text NOT NULL DEFAULT '',
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (tenant_id, id), UNIQUE (tenant_id, name)
    )""")
    op.execute("""CREATE TABLE tool_registry (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
        name text NOT NULL, version integer NOT NULL CHECK (version > 0), definition jsonb NOT NULL,
        enabled boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE (tenant_id,id), UNIQUE (tenant_id,name,version)
    )""")
    op.execute("""CREATE TABLE agent_versions (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
        agent_id uuid NOT NULL, number integer NOT NULL CHECK (number > 0),
        state text NOT NULL DEFAULT 'DRAFT' CHECK (state IN ('DRAFT','PUBLISHED','INACTIVE')),
        instructions text NOT NULL, provider text NOT NULL, model text NOT NULL, limits jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY (tenant_id,agent_id) REFERENCES agents (tenant_id,id),
        UNIQUE (tenant_id,id), UNIQUE (agent_id,number)
    )""")
    op.execute("""CREATE TABLE version_tools (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
        agent_version_id uuid NOT NULL, tool_version_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY (tenant_id,agent_version_id) REFERENCES agent_versions (tenant_id,id),
        FOREIGN KEY (tenant_id,tool_version_id) REFERENCES tool_registry (tenant_id,id),
        UNIQUE (agent_version_id,tool_version_id)
    )""")
    op.execute("""CREATE FUNCTION require_draft_grant() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM 1 FROM agent_versions WHERE tenant_id=NEW.tenant_id AND id=NEW.agent_version_id
                AND state='DRAFT' FOR SHARE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Tool grants require a draft version'; END IF;
            RETURN NEW;
        END $$""")
    op.execute("""CREATE TRIGGER draft_grant BEFORE INSERT ON version_tools
        FOR EACH ROW EXECUTE FUNCTION require_draft_grant()""")
    op.execute("""CREATE TABLE runs (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
        agent_version_id uuid NOT NULL, submitter_id uuid NOT NULL, submission_key text NOT NULL,
        request_hash text NOT NULL, input jsonb NOT NULL, output jsonb, failure_reason text,
        status text NOT NULL DEFAULT 'CREATED' CHECK (status IN ('CREATED','QUEUED','RUNNING',
            'WAITING_FOR_APPROVAL','RETRY_WAIT','CANCEL_REQUESTED','SUCCEEDED','FAILED','CANCELLED','TIMED_OUT')),
        revision integer NOT NULL DEFAULT 1, priority smallint NOT NULL CHECK (priority BETWEEN 0 AND 9),
        deadline_at timestamptz NOT NULL, enqueued_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY (tenant_id,agent_version_id) REFERENCES agent_versions (tenant_id,id),
        FOREIGN KEY (tenant_id,submitter_id) REFERENCES users (tenant_id,id),
        UNIQUE (tenant_id,id), UNIQUE (tenant_id,submission_key)
    )""")
    op.execute("""CREATE TABLE run_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
        run_id uuid NOT NULL, seq integer NOT NULL, kind text NOT NULL, payload jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY (tenant_id,run_id) REFERENCES runs (tenant_id,id), UNIQUE (run_id,seq)
    )""")
    op.execute("""CREATE TABLE outbox_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
        run_id uuid NOT NULL, kind text NOT NULL, available_at timestamptz NOT NULL DEFAULT now(),
        published_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY (tenant_id,run_id) REFERENCES runs (tenant_id,id)
    )""")
    op.execute("""CREATE TABLE audit_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
        actor_id uuid NOT NULL, action text NOT NULL, object_id uuid NOT NULL, request_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY (tenant_id,actor_id) REFERENCES users (tenant_id,id)
    )""")
    for name in ("agents", "runs", "agent_versions"):
        op.execute(f"CREATE INDEX {name}_listing ON {name} (tenant_id,created_at DESC,id DESC)")
    op.execute("CREATE INDEX queue_selection ON runs (tenant_id, priority DESC, enqueued_at,id) WHERE status='QUEUED'")
    op.execute("CREATE INDEX pending_outbox ON outbox_events (available_at,id) WHERE published_at IS NULL")
    for name in TABLES:
        column = "id" if name == "tenants" else "tenant_id"
        policy = f"{column} = nullif(current_setting('agentos.tenant_id', true),'')::uuid"
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_scope ON {name} TO agentos_api USING ({policy}) WITH CHECK ({policy})")
    op.execute("GRANT SELECT ON tenants,users,agents,agent_versions,version_tools,tool_registry,runs,run_events TO agentos_api")
    op.execute("GRANT INSERT ON agents,agent_versions,version_tools,runs,run_events,outbox_events,audit_events TO agentos_api")
    op.execute("GRANT UPDATE (updated_at) ON agents TO agentos_api")
    op.execute("GRANT UPDATE (state,updated_at) ON agent_versions TO agentos_api")
    op.execute("GRANT UPDATE (status,revision,updated_at,enqueued_at,failure_reason) ON runs TO agentos_api")


def downgrade() -> None:
    for name in reversed(TABLES):
        op.execute(f"DROP TABLE {name}")
    op.execute("DROP FUNCTION require_draft_grant()")
