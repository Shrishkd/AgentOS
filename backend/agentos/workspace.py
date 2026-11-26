"""Tenant-scoped dashboard read models. Aggregates cover all matching rows."""
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import text

from .access import Access
from .queries import one

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["Workspace"])


@router.get("/workspace")
async def workspace(access: Access):
    tenant = await one(access, """SELECT id,slug,concurrency_limit,pending_limit,submissions_per_minute
        FROM tenants WHERE id=:tenant""")
    return {**tenant, "role": access.role}


@router.get("/overview")
async def overview(access: Access):
    return await one(access, """SELECT count(*) AS total,
        count(*) FILTER (WHERE status='SUCCEEDED') AS succeeded,
        count(*) FILTER (WHERE status='FAILED') AS failed,
        count(*) FILTER (WHERE status='RUNNING') AS running,
        count(*) FILTER (WHERE status='QUEUED') AS queued,
        (SELECT count(*) FROM tenant_workers WHERE tenant_id=:tenant
            AND NOT draining AND stopped_at IS NULL AND healthy_until>now()) AS healthy_workers,
        (SELECT count(*) FROM approvals WHERE tenant_id=:tenant AND status='pending') AS pending_approvals
        FROM runs WHERE tenant_id=:tenant""")


@router.get("/analytics")
async def analytics(access: Access, days: Annotated[int, Query(ge=1, le=90)] = 7):
    # UTC calendar days, including today. Use one boundary for all breakdowns.
    start = await access.db.scalar(text("""SELECT (date_trunc('day', now() AT TIME ZONE 'UTC')
        - make_interval(days => :days - 1)) AT TIME ZONE 'UTC'"""), {"days": days})
    params = {"tenant": access.tenant, "start": start}

    async def rows(query):
        return (await access.db.execute(text(query), params)).mappings().all()

    return {
        "since": start,
        "days": await rows("""SELECT (created_at AT TIME ZONE 'UTC')::date AS day, count(*) AS runs,
            count(*) FILTER (WHERE status IN ('FAILED','TIMED_OUT')) AS failures,
            count(*) FILTER (WHERE status IN ('SUCCEEDED','FAILED','TIMED_OUT','CANCELLED')) AS finished,
            avg(extract(epoch FROM updated_at-created_at)) FILTER
                (WHERE status IN ('SUCCEEDED','FAILED','TIMED_OUT','CANCELLED')) AS duration
            FROM runs WHERE tenant_id=:tenant AND created_at>=:start GROUP BY 1 ORDER BY 1"""),
        "models": await rows("""SELECT provider,model, count(*) AS calls,
            coalesce(sum(input_tokens) FILTER (WHERE status='reported'),0) AS input_tokens,
            coalesce(sum(output_tokens) FILTER (WHERE status='reported'),0) AS output_tokens,
            count(*) FILTER (WHERE status IN ('unknown','reserved')) AS unresolved
            FROM usage_records WHERE tenant_id=:tenant AND created_at>=:start GROUP BY provider,model
            ORDER BY input_tokens DESC,provider,model"""),
        "agents": await rows("""SELECT a.id,a.name,coalesce(sum(u.estimated_cost) FILTER
            (WHERE u.status='reported'),0) AS estimated_cost,
            count(*) FILTER (WHERE u.status='reported' AND u.estimated_cost IS NULL) AS unpriced
            FROM usage_records u JOIN runs r ON r.id=u.run_id JOIN agent_versions v ON v.id=r.agent_version_id
            JOIN agents a ON a.id=v.agent_id WHERE u.tenant_id=:tenant AND u.created_at>=:start
            GROUP BY a.id,a.name ORDER BY estimated_cost DESC,a.name"""),
        "tools": await rows("""SELECT coalesce(t.name,'Unauthorized tool') AS name,
            count(*) AS steps,sum(s.dispatch_count) AS dispatches,count(*) FILTER (WHERE s.status='failed') AS failures
            FROM steps s LEFT JOIN tool_registry t ON t.id=s.tool_version_id
            WHERE s.tenant_id=:tenant AND s.kind='tool' AND s.created_at>=:start GROUP BY t.name ORDER BY steps DESC""")
    }
