from uuid import UUID

from fastapi import APIRouter
from sqlalchemy import text

from .access import Access, TenantAccess
from .contracts import Agent, AgentInput, Page, Version, VersionInput
from .domain import DomainError
from .queries import Cursor, Limit, one, page

router = APIRouter(prefix="/v1/tenants/{tenant_id}/agents", tags=["Agent registry"])
AGENTS = "SELECT t.id,t.name,t.description,t.created_at FROM agents t WHERE t.tenant_id=:tenant"
VERSIONS = """SELECT t.id,t.agent_id,t.number,t.state,t.instructions,t.provider,t.model,t.limits,t.created_at,
    ARRAY(SELECT g.tool_version_id FROM version_tools g WHERE g.agent_version_id=t.id ORDER BY g.tool_version_id)
    AS tool_version_ids FROM agent_versions t WHERE t.tenant_id=:tenant AND t.agent_id=:agent"""


@router.post("", response_model=Agent, status_code=201)
async def create_agent(body: AgentInput, access: Access):
    access.require_developer()
    result = await access.db.execute(text("""INSERT INTO agents (tenant_id,name,description)
        VALUES (:tenant,:name,:description) ON CONFLICT (tenant_id,name) DO NOTHING
        RETURNING id,name,description,created_at"""), {"tenant": access.tenant, **body.model_dump()})
    row = result.mappings().one_or_none()
    if row is None:
        raise DomainError(409, "agent_name_conflict", "An agent with this name already exists.")
    await access.audit("agent.created", row["id"])
    return row


@router.get("", response_model=Page[Agent])
async def list_agents(access: Access, limit: Limit = 50, cursor: Cursor = None):
    return await page(access, AGENTS, "agents", limit, cursor)


@router.get("/{agent_id}", response_model=Agent)
async def get_agent(agent_id: UUID, access: Access):
    return await one(access, AGENTS + " AND t.id=:id", id=agent_id)


@router.post("/{agent_id}/versions", response_model=Version, status_code=201)
async def create_version(agent_id: UUID, body: VersionInput, access: Access):
    access.require_developer()
    # Updating the parent serializes version number allocation across processes.
    await one(access, "UPDATE agents SET updated_at=clock_timestamp() WHERE tenant_id=:tenant AND id=:id RETURNING id", id=agent_id)
    if body.tool_version_ids:
        count = await access.db.scalar(text("""SELECT count(*) FROM tool_registry
            WHERE tenant_id=:tenant AND enabled AND id=ANY(CAST(:tools AS uuid[]))"""),
            {"tenant": access.tenant, "tools": body.tool_version_ids})
        if count != len(body.tool_version_ids):
            raise DomainError(422, "invalid_tool_grant", "Each grant must reference an enabled tenant tool version.")
    version = await access.db.scalar(text("""INSERT INTO agent_versions
        (tenant_id,agent_id,number,instructions,provider,model,limits)
        SELECT :tenant,:agent,coalesce(max(number),0)+1,:instructions,:provider,:model,CAST(:limits AS jsonb)
        FROM agent_versions WHERE agent_id=:agent RETURNING id"""),
        {"tenant": access.tenant, "agent": agent_id, "instructions": body.instructions, "provider": body.provider,
         "model": body.model, "limits": body.limits.model_dump_json(exclude_none=True)})
    if body.tool_version_ids:
        await access.db.execute(text("""INSERT INTO version_tools (tenant_id,agent_version_id,tool_version_id)
            VALUES (:tenant,:version,:tool)"""), [{"tenant": access.tenant, "version": version, "tool": tool}
                                                 for tool in body.tool_version_ids])
    await access.audit("version.created", version)
    return await one(access, VERSIONS + " AND t.id=:id", agent=agent_id, id=version)


@router.get("/{agent_id}/versions", response_model=Page[Version])
async def list_versions(agent_id: UUID, access: Access, limit: Limit = 50, cursor: Cursor = None):
    await one(access, AGENTS + " AND t.id=:id", id=agent_id)
    return await page(access, VERSIONS, "versions", limit, cursor, agent=agent_id)


@router.get("/{agent_id}/versions/{version_id}", response_model=Version)
async def get_version(agent_id: UUID, version_id: UUID, access: Access):
    return await one(access, VERSIONS + " AND t.id=:id", agent=agent_id, id=version_id)


async def change_state(access: TenantAccess, agent_id: UUID, version_id: UUID, target: str):
    access.require_developer()
    row = await one(access, VERSIONS + " AND t.id=:id FOR UPDATE OF t", agent=agent_id, id=version_id)
    if row["state"] == target:
        return row
    previous = "DRAFT" if target == "PUBLISHED" else "PUBLISHED"
    if row["state"] != previous:
        raise DomainError(409, "version_state_conflict", f"This operation requires a {previous} version.")
    await access.db.execute(text("UPDATE agent_versions SET state=:target,updated_at=clock_timestamp() WHERE id=:id"),
                            {"target": target, "id": version_id})
    await access.audit("version." + target.lower(), version_id)
    return {**row, "state": target}


@router.post("/{agent_id}/versions/{version_id}/publish", response_model=Version)
async def publish(agent_id: UUID, version_id: UUID, access: Access):
    return await change_state(access, agent_id, version_id, "PUBLISHED")


@router.post("/{agent_id}/versions/{version_id}/deactivate", response_model=Version)
async def deactivate(agent_id: UUID, version_id: UUID, access: Access):
    return await change_state(access, agent_id, version_id, "INACTIVE")
