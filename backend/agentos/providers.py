"""Model adapters and the bounded action protocol understood by the execution engine."""

import asyncio
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Annotated, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from .contracts import InputModel


def compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ModelFailure(Exception):
    def __init__(self, code: str, *, retryable: bool = False, rejected: bool = False, retry_after: float = 0):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.rejected = rejected
        self.retry_after = retry_after


class Final(InputModel):
    kind: Literal["final"] = "final"
    answer: str = Field(min_length=1, max_length=65536)


class ToolCall(InputModel):
    kind: Literal["tool"] = "tool"
    call_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    arguments: dict


ACTION = TypeAdapter(Annotated[Final | ToolCall, Field(discriminator="kind")])


class TextArgs(InputModel):
    text: str = Field(max_length=8192, strict=True)


TEXT_TOOL = {"type": "function", "function": {"name": "text_length",
             "description": "Count Unicode code points in a string.", "parameters": TextArgs.model_json_schema()}}
TOOL_DEFINITION = {"implementation": "text_length_v1", "risk": "low", "input_schema": TextArgs.model_json_schema()}


class Usage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    prompt_tokens: int = Field(ge=0, le=1_000_000_000)
    completion_tokens: int = Field(ge=0, le=1_000_000_000)


@dataclass
class Reply:
    envelope: dict
    request_id: str | None

    def usage(self) -> Usage:
        try:
            return Usage.model_validate(self.envelope.get("usage"))
        except ValidationError:
            raise ModelFailure("provider_usage_missing") from None

    def action(self) -> Final | ToolCall:
        try:
            choice, = self.envelope["choices"]
            msg = choice["message"]
            if choice["finish_reason"] == "length":
                raise ModelFailure("output_limit_exceeded")
            if msg.get("refusal") or choice["finish_reason"] == "content_filter":
                raise ModelFailure("model_refused")
            if choice["finish_reason"] == "stop" and not msg.get("tool_calls"):
                return Final.model_validate({"answer": msg["content"]})
            if choice["finish_reason"] == "tool_calls":
                call, = msg["tool_calls"]
                if call["type"] == "function":
                    fn = call["function"]
                    arguments = json.loads(fn["arguments"], parse_constant=_invalid_number)
                    compact(arguments)
                    return ToolCall.model_validate({"call_id": call["id"], "name": fn["name"],
                                                    "arguments": arguments})
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
            pass
        raise ModelFailure("invalid_model_response")


def _invalid_number(value):
    raise ValueError("Non-finite JSON number")


class Adapter(Protocol):
    async def complete(self, model: str, messages: list[dict], tools: list[dict], cap: int) -> Reply: ...


class MockAdapter:
    async def complete(self, model, messages, tools, cap):
        last = messages[-1]
        data = json.loads(last["content"])
        if last["role"] == "user" and isinstance(data.get("tool_call"), dict):
            proposal = data["tool_call"]
            call = {"id": "mock_1", "type": "function", "function": {"name": proposal.get("name"),
                    "arguments": compact(proposal.get("arguments"))}}
            message, reason = {"tool_calls": [call]}, "tool_calls"
        elif last["role"] == "user" and "count_text" in data:
            call = {"id": "count_1", "type": "function", "function": {"name": "text_length",
                    "arguments": compact({"text": data["count_text"]})}}
            message, reason = {"tool_calls": [call]}, "tool_calls"
        else:
            answer = str(data["length"]) if last["role"] == "tool" and "length" in data else compact(data)
            message, reason = {"content": answer}, "stop"
        # The mock's reproducible token unit is one UTF-8 byte.
        output = len(compact(message).encode())
        return Reply({"choices": [{"message": message, "finish_reason": reason if output <= cap else "length"}],
                      "usage": {"prompt_tokens": len(compact([messages, tools]).encode()),
                                "completion_tokens": min(cap, output)}}, None)


def retry_delay(value: str | None) -> float:
    if not value:
        return 0
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, delay) if math.isfinite(delay) else 0


class ChatAdapter:
    def __init__(self, client: httpx.AsyncClient, endpoint: str, timeout: float):
        self.client, self.endpoint, self.timeout = client, endpoint, timeout

    async def complete(self, model, messages, tools, cap):
        payload = {"model": model, "messages": messages, "max_completion_tokens": cap}
        if tools:
            payload["tools"] = tools
            payload["parallel_tool_calls"] = False
        try:
            async with asyncio.timeout(self.timeout):
                async with self.client.stream("POST", self.endpoint, json=payload,
                                              headers={"Accept-Encoding": "identity"}) as response:
                    if response.status_code == 429:
                        raise ModelFailure("provider_rate_limited", retryable=True, rejected=True,
                                           retry_after=retry_delay(response.headers.get("retry-after")))
                    if response.status_code >= 500:
                        raise ModelFailure("provider_unavailable", retryable=True)
                    if response.status_code == 408:
                        raise ModelFailure("provider_timeout", retryable=True)
                    if response.status_code != 200:
                        raise ModelFailure("provider_request_rejected", rejected=True)
                    body = bytearray()
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise ModelFailure("unsupported_response_encoding")
                    async for chunk in response.aiter_raw(chunk_size=16384):
                        body.extend(chunk)
                        if len(body) > 1_048_576:
                            raise ModelFailure("provider_response_too_large")
                    envelope = json.loads(body, parse_constant=_invalid_number)
                    if not isinstance(envelope, dict):
                        raise ModelFailure("invalid_model_response")
                    ident = response.headers.get("x-request-id") or envelope.get("id")
                    return Reply(envelope, ident[:200] if isinstance(ident, str) and ident.isascii() and ident.isprintable() else None)
        except (TimeoutError, httpx.TimeoutException):
            raise ModelFailure("provider_timeout", retryable=True) from None
        except httpx.RequestError:
            raise ModelFailure("provider_unavailable", retryable=True) from None
        except (ValueError, RecursionError):
            raise ModelFailure("invalid_model_response") from None


@dataclass(frozen=True)
class Prices:
    input: Decimal
    output: Decimal

    def cost(self, prompt: int, completion: int) -> Decimal:
        return (self.input * prompt + self.output * completion) / 1_000_000
