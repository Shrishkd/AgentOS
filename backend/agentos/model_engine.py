import asyncio
import random
from decimal import Decimal
from time import monotonic

from pydantic import ValidationError
from sqlalchemy import text

from .providers import (
    ACTION, TEXT_TOOL, TOOL_DEFINITION, Adapter, Final, ModelFailure, Prices,
    TextArgs, ToolCall, Usage, compact,
)
from .scheduler import Claim, Scheduler

MAX_INVOCATIONS = 3


def allocation(limits, prompt, used_tokens, spent, prices, cap):
    remaining = limits["max_tokens"] - used_tokens - prompt
    output = min(cap, remaining)
    if output < 1:
        raise ModelFailure("token_budget_exhausted")
    if limits.get("max_cost_usd") is not None:
        if prices is None:
            raise ModelFailure("pricing_unavailable")
        money = Decimal(str(limits["max_cost_usd"])) - spent - prices.cost(prompt, 0)
        if money < 0:
            raise ModelFailure("cost_budget_exhausted")
        if prices.output:
            output = min(output, int(money * 1_000_000 / prices.output))
            if output < 1:
                raise ModelFailure("cost_budget_exhausted")
    return output


async def granted_tool(db, claim):
    return await db.scalar(text("""SELECT t.id FROM version_tools g JOIN tool_registry t
        ON t.tenant_id=g.tenant_id AND t.id=g.tool_version_id WHERE g.tenant_id=:tenant
        AND g.agent_version_id=:version AND t.enabled AND t.name='text_length' AND t.version=1
        AND t.definition=CAST(:definition AS jsonb)"""),
        {"tenant": claim.tenant, "version": claim.spec.version, "definition": compact(TOOL_DEFINITION)})


async def step(db, claim, ordinal, kind, request, tool=None):
    if ordinal > claim.spec.limits["max_steps"]:
        raise ModelFailure("step_limit_exceeded")
    params = {"tenant": claim.tenant, "run": claim.run, "n": ordinal, "kind": kind,
              "request": compact(request), "tool": tool}
    await db.execute(text("""INSERT INTO steps (tenant_id,run_id,ordinal,kind,request,tool_version_id)
        VALUES (:tenant,:run,:n,:kind,CAST(:request AS jsonb),:tool)
        ON CONFLICT (run_id,ordinal) DO NOTHING"""), params)
    row = (await db.execute(text("SELECT * FROM steps WHERE run_id=:run AND ordinal=:n"), params)).mappings().one()
    if row["kind"] != kind:
        raise ModelFailure("inconsistent_step_history")
    if row["status"] == "failed":
        raise ModelFailure(row["error_code"])
    return row


async def close_step(db, step_id, result=None, error=None):
    await db.execute(text("""UPDATE steps SET status=:status,result=CAST(:result AS jsonb),
        error_code=:error,ended_at=clock_timestamp() WHERE id=:id AND status='pending'"""),
        {"id": step_id, "status": "failed" if error else "completed", "result": compact(result), "error": error})


class ModelEngine:
    def __init__(self, scheduler: Scheduler, adapters: dict[str, Adapter], remote_prices: Prices | None,
                 output_cap: int):
        self.scheduler, self.adapters = scheduler, adapters
        self.remote_prices, self.output_cap = remote_prices, output_cap

    async def run(self, claim: Claim) -> dict:
        messages = [{"role": "system", "content": claim.spec.instructions},
                    {"role": "user", "content": compact(claim.input)}]
        ordinal = 1
        while True:
            action = await self.model_step(claim, ordinal, messages)
            if isinstance(action, Final):
                return {"answer": action.answer}
            ordinal += 1
            result = await self.tool_step(claim, ordinal, action)
            messages.extend([{"role": "assistant", "content": None, "tool_calls": [
                {"id": action.call_id, "type": "function", "function": {
                    "name": action.name, "arguments": compact(action.arguments)}}]},
                {"role": "tool", "tool_call_id": action.call_id, "content": compact(result)}])
            ordinal += 1

    async def tool_step(self, claim, ordinal, action: ToolCall):
        error = None
        async with self.scheduler.progress(claim) as db:
            tool = await granted_tool(db, claim) if action.name == "text_length" else None
            row = await step(db, claim, ordinal, "tool", action.model_dump(), tool)
            if row["status"] == "completed":
                return row["result"]
            result = None
            if tool is None:
                error = "tool_not_permitted"
            else:
                try:
                    args = TextArgs.model_validate(action.arguments)
                    result = {"length": len(args.text)}
                except ValidationError:
                    error = "invalid_tool_arguments"
            await close_step(db, row["id"], result, error)
        if error:
            raise ModelFailure(error)
        return result

    async def reserve(self, db, claim, step_id, prompt, prices):
        totals = (await db.execute(text("""SELECT coalesce(sum(CASE status
            WHEN 'reported' THEN input_tokens+output_tokens WHEN 'rejected' THEN 0
            ELSE reserved_input+reserved_output END),0) AS tokens,
            coalesce(sum(CASE status WHEN 'reported' THEN estimated_cost WHEN 'rejected' THEN 0
            ELSE reserved_cost END),0) AS cost FROM usage_records WHERE run_id=:run"""),
            {"run": claim.run})).mappings().one()
        prior = (await db.execute(text("""SELECT invocation,status,error_code FROM usage_records
            WHERE step_id=:step ORDER BY invocation DESC LIMIT 1"""), {"step": step_id})).mappings().first()
        if prior and prior["status"] == "reserved":
            raise ModelFailure("model_outcome_unknown")
        number = prior["invocation"] + 1 if prior else 1
        if number > MAX_INVOCATIONS:
            raise ModelFailure(prior["error_code"] or "provider_retries_exhausted")
        cap = allocation(claim.spec.limits, prompt, totals["tokens"], totals["cost"], prices, self.output_cap)
        invocation = await db.scalar(text("""INSERT INTO usage_records(tenant_id,run_id,step_id,invocation,
            provider,model,reserved_input,reserved_output,input_rate,output_rate,reserved_cost)
            VALUES (:tenant,:run,:step,:number,:provider,:model,:prompt,:cap,:ir,:orate,:cost) RETURNING id"""),
            {"tenant": claim.tenant, "run": claim.run, "step": step_id, "number": number,
             "provider": claim.spec.provider, "model": claim.spec.model, "prompt": prompt, "cap": cap,
             "ir": prices.input if prices else None, "orate": prices.output if prices else None,
             "cost": prices.cost(prompt, cap) if prices else None})
        return invocation, number, cap

    async def model_step(self, claim, ordinal, messages):
        spec = claim.spec
        prices = Prices(Decimal(0), Decimal(0)) if spec.provider == "mock" else self.remote_prices
        while True:
            async with self.scheduler.progress(claim) as db:
                tools = [TEXT_TOOL] if await granted_tool(db, claim) else []
                row = await step(db, claim, ordinal, "model", {"messages": messages, "tools": tools})
                if row["status"] == "completed":
                    return ACTION.validate_python(row["result"])
                # A retry uses the persisted prompt. Tool permissions are checked again at dispatch.
                messages, tools = row["request"]["messages"], row["request"]["tools"]
                # UTF-8 bytes plus framing allowance conservatively estimate the prompt.
                prompt = len(compact([messages, tools]).encode()) + 128
                invocation, number, cap = await self.reserve(db, claim, row["id"], prompt, prices)
                remaining = await db.scalar(text("""SELECT extract(epoch FROM
                    (CAST(:deadline AS timestamptz)-clock_timestamp()))"""), {"deadline": spec.deadline})
                deadline = monotonic() + float(remaining)

            action = usage = error = request_id = None
            start = monotonic()
            try:
                if deadline <= monotonic():
                    raise ModelFailure("deadline_elapsed", rejected=True)
                async with asyncio.timeout(deadline - monotonic()):
                    reply = await self.adapters[spec.provider].complete(spec.model, messages, tools, cap)
                request_id = reply.request_id
                usage = reply.usage()
                action = reply.action()
                if usage.prompt_tokens > prompt or usage.completion_tokens > cap:
                    raise ModelFailure("provider_usage_overrun")
            except TimeoutError:
                error = ModelFailure("deadline_elapsed")
            except ModelFailure as exc:
                error = exc
            latency = round(1000 * (monotonic() - start))
            retry = error and error.retryable and number < MAX_INVOCATIONS
            async with self.scheduler.progress(claim) as db:
                await self.settle(db, invocation, usage, prices, request_id, latency, error)
                if not retry:
                    await close_step(db, row["id"], action.model_dump() if action and not error else None,
                                     error.code if error else None)
            if not error:
                return action
            if not retry:
                raise error
            delay = max(error.retry_after, random.uniform(0.1, 0.25) * 2 ** (number-1))
            if delay >= deadline - monotonic():
                raise error
            await asyncio.sleep(delay)

    @staticmethod
    async def settle(db, invocation, usage: Usage | None, prices, request_id, latency, error):
        status = "reported" if usage else "rejected" if error and error.rejected else "unknown"
        await db.execute(text("""UPDATE usage_records SET status=:status,input_tokens=:prompt,output_tokens=:output,
            estimated_cost=:cost,latency_ms=:latency,provider_request_id=:rid,error_code=:error WHERE id=:id"""),
            {"status": status, "prompt": usage.prompt_tokens if usage else None,
             "output": usage.completion_tokens if usage else None,
             "cost": prices.cost(usage.prompt_tokens, usage.completion_tokens) if prices and usage else None,
             "latency": latency, "rid": request_id, "error": error.code if error else None, "id": invocation})
