"""Persist run trace context and expose bounded system-wide scrape aggregates."""

from alembic import op

revision = "0007_observability"
down_revision = "0006_tool_approvals"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE runs ADD traceparent text")
    # The API role can read only these computed cross-tenant statistics without tenant context.
    op.execute("""CREATE VIEW execution_metrics WITH (security_barrier=true) AS
        SELECT 'runs' AS metric, status AS category, count(*)::numeric AS value FROM runs GROUP BY status
        UNION ALL SELECT 'workers','healthy',count(*) FROM workers
            WHERE NOT draining AND stopped_at IS NULL AND healthy_until > now()
        UNION ALL SELECT 'retries',outcome,count(*) FROM attempts
            WHERE outcome IN ('provider_retry','tool_retry','lease_expired') GROUP BY outcome
        UNION ALL SELECT 'invocations',status,count(*) FROM usage_records GROUP BY status
        UNION ALL SELECT 'tokens','input',coalesce(sum(input_tokens),0) FROM usage_records WHERE status='reported'
        UNION ALL SELECT 'tokens','output',coalesce(sum(output_tokens),0) FROM usage_records WHERE status='reported'
        UNION ALL SELECT 'estimated_cost_usd','',coalesce(sum(estimated_cost),0) FROM usage_records WHERE status='reported'
        UNION ALL SELECT 'unpriced_invocations','',count(*) FROM usage_records
            WHERE status='reported' AND estimated_cost IS NULL""")
    op.execute("GRANT SELECT ON execution_metrics TO agentos_api")


def downgrade():
    op.execute("DROP VIEW execution_metrics")
    op.execute("ALTER TABLE runs DROP COLUMN traceparent")
