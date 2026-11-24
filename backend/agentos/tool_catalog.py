"""Only code-deployed tool versions can be enabled in the tenant registry."""
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field
from sqlalchemy import text

from .contracts import InputModel
from .providers import TEXT_TOOL, TOOL_DEFINITION, TextArgs


class Calculation(InputModel):
    expression: str = Field(min_length=1, max_length=128)


class Search(InputModel):
    query: str = Field(min_length=1, max_length=160)


class GetPage(InputModel):
    url: str = Field(min_length=1, max_length=2048)


class Inventory(InputModel):
    category: Literal["hardware", "software"]
    limit: int = Field(default=10, ge=1, le=20, strict=True)


class Report(InputModel):
    title: str = Field(min_length=1, max_length=160)
    sections: list[Annotated[str, Field(min_length=1, max_length=2000)]] = Field(min_length=1, max_length=12)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    inputs: type[InputModel]
    risk: str = "low"
    timeout: int = 2
    output_bytes: int = 16384

    def metadata(self):
        return {"description": self.description, "input_schema": self.inputs.model_json_schema(),
                "implementation": self.name + "_v1", "risk": self.risk, "timeout_seconds": self.timeout,
                "max_output_bytes": self.output_bytes, "permission": self.name + ":execute",
                "idempotency": "step_key" if self.name == "save_report" else "read_only"}

    def matches(self, definition):
        return definition == self.metadata() or (self.name == "text_length" and definition == TOOL_DEFINITION)

    def function(self):
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                  "parameters": self.inputs.model_json_schema()}}


CATALOG = {tool.name: tool for tool in (
    Tool("text_length", TEXT_TOOL["function"]["description"], TextArgs),
    Tool("calculator", "Evaluate a bounded arithmetic expression with +, -, *, / and parentheses.", Calculation),
    Tool("inventory_lookup", "List items by category in the read-only sample inventory.", Inventory),
    Tool("web_search", "Search English Wikipedia for matching page titles.", Search, "high", 8),
    Tool("http_get", "Read a text resource from www.rfc-editor.org over HTTPS.", GetPage, "high", 8, 65536),
    Tool("save_report", "Save a structured report to the tenant workspace.", Report, "high"),
)}


async def granted(db, claim, name=None):
    query = """SELECT t.id,t.name,t.definition FROM tool_registry t JOIN version_tools g
        ON g.tenant_id=t.tenant_id AND g.tool_version_id=t.id WHERE t.tenant_id=:tenant AND
        g.agent_version_id=:version AND t.enabled AND t.version=1"""
    if name is not None:
        query += " AND t.name=:name FOR SHARE OF t"
    rows = (await db.execute(text(query), {"tenant": claim.tenant, "version": claim.spec.version, "name": name})).mappings()
    return {r["name"]: r["id"] for r in rows if r["name"] in CATALOG and CATALOG[r["name"]].matches(r["definition"])}
