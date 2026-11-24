import json
from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, StringConstraints, field_validator, model_validator

from .domain import State

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def postgres_text(cls, value):
        def check(item):
            if isinstance(item, str):
                if "\x00" in item:
                    raise ValueError("Null characters are unsupported")
                item.encode("utf-8")
            elif isinstance(item, dict):
                for key, val in item.items():
                    check(key)
                    check(val)
            elif isinstance(item, list):
                for val in item:
                    check(val)
        check(value)
        return value


class AgentInput(InputModel):
    name: Name
    description: str = Field(default="", max_length=4096)


class Limits(InputModel):
    max_steps: int = Field(ge=1, le=1000, strict=True)
    max_tokens: int = Field(ge=1, le=10_000_000, strict=True)
    timeout_seconds: int = Field(ge=1, le=604800, strict=True)
    max_cost_usd: Annotated[float, Field(gt=0, le=100000)] | None = None


class VersionInput(InputModel):
    instructions: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32000)]
    provider: Literal["builtin", "mock", "openai-compatible"]
    model: Name
    limits: Limits
    tool_version_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid_configuration(self):
        if self.provider == "builtin" and (self.model != "receipt" or self.tool_version_ids):
            raise ValueError("The built-in receipt handler accepts no tool grants")
        if self.provider == "mock" and self.model != "deterministic":
            raise ValueError("The mock provider uses the deterministic model")
        if len(self.tool_version_ids) != len(set(self.tool_version_ids)):
            raise ValueError("Tool version IDs must be unique")
        return self


class RunInput(InputModel):
    agent_version_id: UUID
    input: dict[str, JsonValue]
    priority: int = Field(default=0, ge=0, le=9, strict=True)
    deadline_at: AwareDatetime | None = None

    @field_validator("input")
    @classmethod
    def bounded_input(cls, value):
        if len(json.dumps(value, allow_nan=False, ensure_ascii=False).encode()) > 262144:
            raise ValueError("Run input exceeds 256 KiB")
        return value


class Agent(BaseModel):
    id: UUID
    name: str
    description: str
    created_at: datetime


class Version(VersionInput):
    id: UUID
    agent_id: UUID
    number: int
    state: Literal["DRAFT", "PUBLISHED", "INACTIVE"]
    created_at: datetime


class Run(BaseModel):
    id: UUID
    agent_version_id: UUID
    submitter_id: UUID
    status: State
    revision: int
    priority: int
    deadline_at: datetime
    enqueued_at: datetime | None
    created_at: datetime
    updated_at: datetime
    input: dict[str, JsonValue]
    output: JsonValue = None
    failure_reason: str | None
    retry_at: datetime | None


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None
