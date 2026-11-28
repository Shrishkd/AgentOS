"""Concurrent tenant admission and deployment health signals."""
from alembic import op

revision = "0008_operations"
down_revision = "0007_observability"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE INDEX pending_runs_by_tenant ON runs(tenant_id)
        WHERE status NOT IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT')""")
    op.execute("""CREATE FUNCTION scoped_member(scope uuid, token_issuer text, token_subject text)
        RETURNS TABLE(id uuid, role text)
        LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = pg_catalog,public AS $$
        BEGIN
            PERFORM set_config('agentos.tenant_id',scope::text,true);
            RETURN QUERY SELECT u.id,u.role FROM public.users u WHERE u.tenant_id=scope AND u.enabled
                AND u.issuer=token_issuer AND u.subject=token_subject;
        END $$""")
    # The fast path reserves headroom for every potentially uncommitted admission
    # (one per client connection). Near the limit the exclusive gate waits them out.
    op.execute("""CREATE FUNCTION pending_slots(scope uuid) RETURNS bigint
        LANGUAGE sql VOLATILE SECURITY INVOKER SET search_path = pg_catalog,public AS $$
            SELECT pending_limit - (SELECT count(*) FROM public.runs WHERE tenant_id=scope
                AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT'))
                FROM public.tenants WHERE id=scope
        $$""")
    op.execute("""CREATE FUNCTION reserve_admission(scope uuid) RETURNS boolean
        LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = pg_catalog,public AS $$
        DECLARE gate bigint := hashtextextended('admission:' || scope::text, 0);
        BEGIN
            BEGIN
                PERFORM pg_advisory_xact_lock_shared(gate);
                IF public.pending_slots(scope) >= current_setting('max_connections')::integer THEN RETURN true; END IF;
                -- Release the shared lock before competing for the exclusive gate.
                RAISE SQLSTATE 'A0001';
            EXCEPTION WHEN SQLSTATE 'A0001' THEN NULL;
            END;
            PERFORM pg_advisory_xact_lock(gate);
            RETURN public.pending_slots(scope) > 0;
        END $$""")
    op.execute("""CREATE FUNCTION prepare_submission(scope uuid, submission text, version_id uuid)
        RETURNS TABLE(existing_id uuid, digest text, state text, timeout_seconds int,
                      db_now timestamptz, rate_limit int, has_room boolean)
        LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path = pg_catalog,public AS $$
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtextextended('submission:' || scope::text || ':' || submission,0));
            SELECT r.id,r.request_hash INTO existing_id,digest FROM public.runs r
                WHERE r.tenant_id=scope AND r.submission_key=submission;
            IF existing_id IS NULL THEN
                SELECT v.state,(v.limits->>'timeout_seconds')::int,t.submissions_per_minute
                    INTO state,timeout_seconds,rate_limit FROM public.agent_versions v
                    JOIN public.tenants t ON t.id=v.tenant_id WHERE v.tenant_id=scope AND v.id=version_id FOR SHARE OF v;
                db_now := clock_timestamp();
                IF state='PUBLISHED' THEN has_room := public.reserve_admission(scope); END IF;
            END IF;
            RETURN NEXT;
        END $$""")
    for signature in ("scoped_member(uuid,text,text)", "pending_slots(uuid)", "reserve_admission(uuid)",
                      "prepare_submission(uuid,text,uuid)"):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO agentos_api")
    op.execute("""CREATE VIEW operational_metrics WITH (security_barrier=true) AS
        SELECT 'oldest_queued_seconds' AS metric,
            coalesce(extract(epoch FROM now()-min(enqueued_at)),0) AS value FROM runs WHERE status='QUEUED'
        UNION ALL SELECT 'oldest_unpublished_seconds',coalesce(extract(epoch FROM now()-min(created_at)),0)
            FROM outbox_events WHERE published_at IS NULL
        UNION ALL SELECT 'oldest_expired_lease_seconds',coalesce(extract(epoch FROM now()-min(expires_at)),0)
            FROM worker_leases WHERE owner_id IS NOT NULL AND expires_at < now()
        UNION ALL SELECT 'wal_archive_failed', CASE WHEN last_failed_time IS NOT NULL AND
            (last_archived_time IS NULL OR last_failed_time > last_archived_time) THEN 1 ELSE 0 END
            FROM pg_stat_archiver""")
    op.execute("GRANT SELECT ON operational_metrics TO agentos_api")


def downgrade():
    op.execute("DROP INDEX pending_runs_by_tenant")
    op.execute("DROP FUNCTION scoped_member(uuid,text,text)")
    op.execute("DROP VIEW operational_metrics")
    op.execute("DROP FUNCTION prepare_submission(uuid,text,uuid), reserve_admission(uuid), pending_slots(uuid)")
