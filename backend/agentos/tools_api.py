from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import text

from .access import Access
from .contracts import InputModel
from .domain import DomainError, State
from .lifecycle import RUNS, transition
from .providers import compact
from .queries import Cursor, Limit, one, page
from .tool_catalog import CATALOG

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["Tools and approvals"])
APPROVALS = """SELECT t.id,t.run_id,t.step_id,t.tool_version_id,t.tool_name,t.arguments,t.action_digest,t.risk,
    t.status,t.expires_at,t.created_at,t.decided_by,t.decided_at,t.reason,t.wait_seq,t.resolution_seq
    FROM approvals t WHERE t.tenant_id=:tenant"""


class Registration(InputModel):
    name: str = Field(min_length=1, max_length=64)


class Availability(InputModel):
    enabled: bool


class Decision(InputModel):
    decision: Literal["approve", "reject"]
    action_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, max_length=1000)


def administrator(access):
    if access.role != "admin":
        raise DomainError(403, "forbidden", "Tenant administrator access is required.")


@router.get("/tools/catalog")
async def catalog(access: Access):
    return [{"name": tool.name, "version": 1, **tool.metadata()} for tool in CATALOG.values()]


@router.get("/tools")
async def list_tools(access: Access, limit: Limit = 50, cursor: Cursor = None):
    return await page(access, """SELECT t.id,t.name,t.version,t.enabled,t.definition,t.created_at
        FROM tool_registry t WHERE t.tenant_id=:tenant""", "tools", limit, cursor)


@router.post("/tools")
async def register(body: Registration, access: Access):
    administrator(access)
    tool = CATALOG.get(body.name)
    if tool is None:
        raise DomainError(422, "unknown_tool", "Select a deployed tool from the catalog.")
    inserted = await access.db.scalar(text("""INSERT INTO tool_registry(tenant_id,name,version,definition)
        VALUES (:tenant,:name,1,CAST(:definition AS jsonb)) ON CONFLICT(tenant_id,name,version) DO NOTHING
        RETURNING id"""), {"tenant": access.tenant, "name": tool.name, "definition": compact(tool.metadata())})
    row = await one(access, """SELECT id,name,version,enabled,definition FROM tool_registry
        WHERE tenant_id=:tenant AND name=:name AND version=1""", name=tool.name)
    if not tool.matches(row["definition"]):
        raise DomainError(409, "version_conflict", "The registered tool version has a different definition.")
    if inserted:
        await access.audit("tool.registered", inserted)
    return row


@router.patch("/tools/{tool_id}")
async def availability(tool_id: UUID, body: Availability, access: Access):
    administrator(access)
    row = await one(access, """UPDATE tool_registry SET enabled=:enabled,updated_at=clock_timestamp()
        WHERE tenant_id=:tenant AND id=:id RETURNING id,name,version,enabled""", id=tool_id, enabled=body.enabled)
    await access.audit("tool.enabled" if body.enabled else "tool.disabled", tool_id)
    return row


@router.get("/reports/{report_id}")
async def report(report_id: UUID, access: Access):
    return await one(access, """SELECT id,run_id,document,created_at FROM tool_reports
        WHERE tenant_id=:tenant AND id=:id""", id=report_id)


@router.get("/approvals")
async def approvals(access: Access, status: Literal["pending","approved","rejected","expired","cancelled"] | None = None,
                    limit: Limit = 50, cursor: Cursor = None):
    sql = APPROVALS + (" AND t.status=:status" if status else "")
    return await page(access, sql, "approvals", limit, cursor, **({"status": status} if status else {}))


@router.get("/approvals/{approval_id}")
async def approval(approval_id: UUID, access: Access):
    return await one(access, APPROVALS + " AND t.id=:id", id=approval_id)


@router.post("/approvals/{approval_id}/decision")
async def decide(approval_id: UUID, body: Decision, access: Access):
    if access.role not in {"approver", "admin"}:
        raise DomainError(403, "forbidden", "An approver or tenant administrator must decide.")
    proposal = await approval(approval_id, access)
    run = await one(access, RUNS + " AND t.id=:id FOR UPDATE", id=proposal["run_id"])
    proposal = await approval(approval_id, access)
    if proposal["action_digest"] != body.action_digest:
        raise DomainError(409, "action_mismatch", "The decision does not match the proposed action.")
    state = "approved" if body.decision == "approve" else "rejected"
    if proposal["status"] == state and proposal["reason"] == body.reason:
        return proposal
    if proposal["status"] != "pending" or run["status"] != State.WAITING_FOR_APPROVAL:
        raise DomainError(409, "approval_closed", "The proposal is no longer pending.")
    now = await access.db.scalar(text("SELECT clock_timestamp()"))
    expired = now >= proposal["expires_at"]
    target = State.FAILED if expired or state == "rejected" else State.QUEUED
    params = {"tenant": access.tenant, "id": approval_id, "user": access.user, "state": state,
              "reason": body.reason, "seq": run["revision"] + 1}
    if not expired:
        await access.db.execute(text("""UPDATE approvals SET status=:state,decided_by=:user,reason=:reason,
            decided_at=clock_timestamp(),resolution_seq=:seq WHERE tenant_id=:tenant AND id=:id"""), params)
    result = await transition(access, run["id"], State.WAITING_FOR_APPROVAL, target, run["revision"])
    if result["status"] == State.TIMED_OUT or expired:
        if result["status"] == State.TIMED_OUT and not expired:
            await access.db.execute(text("""UPDATE approvals SET status='expired',decided_by=NULL,reason=NULL,
                decided_at=NULL WHERE tenant_id=:tenant AND id=:id"""), params)
        if result["status"] == State.FAILED:
            await access.db.execute(text("UPDATE runs SET failure_reason='approval_expired' WHERE id=:id"), {"id": run["id"]})
        return JSONResponse({"error": {"code": "approval_expired", "message": "The approval window has closed.",
                                       "request_id": access.request_id}}, status_code=409)
    if state == "rejected":
        await access.db.execute(text("UPDATE runs SET failure_reason='approval_rejected' WHERE id=:id"), {"id": run["id"]})
    await access.audit("approval." + state, approval_id)
    return await approval(approval_id, access)
