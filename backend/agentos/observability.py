from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Request, Response
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from pydantic import AwareDatetime, BaseModel
from sqlalchemy import text

from .access import Access
from .domain import DomainError
from .queries import one

router = APIRouter(tags=["Observability"])
TENANT = "/v1/tenants/{tenant_id}"
METRICS = {
    "runs": ("Stored executions by current state.", "state"),
    "workers": ("Worker sessions currently accepting work.", "state"),
    "retries": ("Stored attempts ending in retry or lease expiry.", "outcome"),
    "invocations": ("Stored model invocations by settlement state.", "status"),
    "tokens": ("Tokens reported by providers.", "direction"),
    "estimated_cost_usd": ("Estimated USD cost of priced, reported usage.", None),
    "unpriced_invocations": ("Reported invocations without a configured price.", None),
}


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request):
    async with request.app.state.engine.connect() as db:
        rows = (await db.execute(text("SELECT metric,category,value FROM execution_metrics"))).all()
    registry = CollectorRegistry()
    families = {key: Gauge("agentos_" + key, description, [label] if label else [], registry=registry)
                for key, (description, label) in METRICS.items()}
    for key, category, value in rows:
        gauge = families[key].labels(category) if METRICS[key][1] else families[key]
        gauge.set(value)
    return Response(generate_latest(registry), media_type="text/plain; version=0.0.4; charset=utf-8")


class UsageSummary(BaseModel):
    invocations: int
    reported: int
    rejected: int
    unknown: int
    reserved: int
    unpriced: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Decimal


@router.get(TENANT + "/usage", response_model=UsageSummary)
async def usage_summary(access: Access, run_id: UUID | None = None, since: AwareDatetime | None = None,
                        until: AwareDatetime | None = None):
    if since and until and since >= until:
        raise DomainError(422, "invalid_interval", "The end must be later than the start.")
    where = ["tenant_id=:tenant"]
    params = {}
    if run_id is not None:
        await one(access, "SELECT id FROM runs WHERE tenant_id=:tenant AND id=:run", run=run_id)
        where.append("run_id=:run")
        params["run"] = run_id
    if since:
        where.append("created_at >= :since")
        params["since"] = since
    if until:
        where.append("created_at < :until")
        params["until"] = until
    return await one(access, """SELECT count(*) AS invocations,
        count(*) FILTER (WHERE status='reported') AS reported, count(*) FILTER (WHERE status='rejected') AS rejected,
        count(*) FILTER (WHERE status='unknown') AS unknown, count(*) FILTER (WHERE status='reserved') AS reserved,
        count(*) FILTER (WHERE status='reported' AND estimated_cost IS NULL) AS unpriced,
        coalesce(sum(input_tokens) FILTER (WHERE status='reported'),0) AS input_tokens,
        coalesce(sum(output_tokens) FILTER (WHERE status='reported'),0) AS output_tokens,
        coalesce(sum(estimated_cost) FILTER (WHERE status='reported'),0) AS estimated_cost_usd
        FROM usage_records WHERE """ + " AND ".join(where), **params)


@router.get(TENANT + "/runs/{run_id}/diagnostics")
async def diagnostics(run_id: UUID, access: Access):
    return await one(access, """SELECT r.id AS run_id,r.status,r.failure_reason,
        nullif(split_part(r.traceparent,'-',2),'') AS trace_id,
        extract(epoch FROM (CASE WHEN r.status IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT')
            THEN r.updated_at ELSE now() END - r.created_at)) AS duration_seconds,
        (SELECT count(*) FROM attempts WHERE run_id=r.id AND outcome IN
            ('provider_retry','tool_retry','lease_expired')) AS retry_count,
        (SELECT jsonb_build_object('number',number,'outcome',outcome,'worker_id',worker_id)
            FROM attempts WHERE run_id=r.id ORDER BY number DESC LIMIT 1) AS last_attempt,
        (SELECT jsonb_build_object('ordinal',ordinal,'kind',kind,'status',status,'error_code',error_code)
            FROM steps WHERE run_id=r.id ORDER BY ordinal DESC LIMIT 1) AS last_step
        FROM runs r WHERE r.tenant_id=:tenant AND r.id=:run""", run=run_id)
