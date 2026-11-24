import asyncio
import random
from decimal import Decimal
from time import monotonic

from sqlalchemy import text

from . import checkpoints, tool_gateway
from .tool_catalog import CATALOG, granted
from .providers import (
    ACTION, Adapter, Final, ModelFailure, Prices,
    ToolCall, Usage, compact,
)
from .scheduler import Claim, RetryScheduled, Scheduler

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
        async with self.scheduler.progress(claim) as db:
            cursor = await checkpoints.restore(db, claim)
        while not isinstance(cursor.action, Final):
            if cursor.action is None:
                cursor.action = await self.model_step(claim, cursor.ordinal, cursor.messages)
            else:
                try:
                    result = await self.tool_step(claim, cursor.ordinal, cursor.action)
                except TimeoutError:
                    # Record the failure after the interrupted transaction has rolled back.
                    raise ModelFailure("tool_timeout") from None
                cursor.applied_tool(result)
            cursor.ordinal += 1
        return {"answer": cursor.action.answer}

    async def tool_step(self, claim, ordinal, action: ToolCall):
        error = None
        async with self.scheduler.progress(claim) as db:
            versions = await granted(db, claim, action.name)
            version = versions.get(action.name)
            row = await step(db, claim, ordinal, "tool", action.model_dump(), version)
            if row["status"] == "completed":
                return row["result"]
            try:
                if version is None or row["tool_version_id"] != version:
                    raise ModelFailure("tool_not_permitted")
                tool = CATALOG[action.name]
                args = await tool_gateway.authorize(db, claim, row, tool, action)
                dispatch = await db.scalar(text("""UPDATE steps SET dispatch_count=dispatch_count+1
                    WHERE id=:id RETURNING dispatch_count"""), {"id": row["id"]})
                if tool.name == "save_report":
                    # The protected write, result and checkpoint commit under one run and registry lock.
                    async with asyncio.timeout(tool.timeout):
                        result = await tool_gateway.save_report(db, claim, row["id"], args)
                    await close_step(db, row["id"], result)
                    await checkpoints.save(db, claim, row, "model")
                    return result
            except ModelFailure as exc:
                error = exc
                await close_step(db, row["id"], error=exc.code)
        if error:
            raise error
        try:
            result = await tool_gateway.read_tool(tool, row["id"], args)
        except ModelFailure as exc:
            retry = tool.name in {"web_search", "http_get"} and dispatch < 2 and exc.code in {
                "tool_timeout", "tool_http_unavailable", "tool_http_busy"}
            async with self.scheduler.progress(claim, retry_delay=1 if retry else None, retry_outcome="tool_retry") as db:
                await db.execute(text("UPDATE steps SET last_dispatch_error=:code WHERE id=:id"),
                                 {"code": exc.code, "id": row["id"]})
                if not retry:
                    await close_step(db, row["id"], error=exc.code)
            if retry:
                raise RetryScheduled() from None
            raise
        async with self.scheduler.progress(claim) as db:
            await close_step(db, row["id"], result)
            await checkpoints.save(db, claim, row, "model")
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
        async with self.scheduler.progress(claim) as db:
            tools = [CATALOG[name].function() for name in sorted(await granted(db, claim))]
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
        delay = None
        if error and error.retryable and number < MAX_INVOCATIONS:
            backoff = random.uniform(0.1, 0.25) * 2 ** (number - 1)
            delay = min(max(error.retry_after, backoff), max(0, deadline - monotonic()))
        async with self.scheduler.progress(claim, retry_delay=delay) as db:
            await self.settle(db, invocation, usage, prices, request_id, latency, error)
            if delay is None:
                await close_step(db, row["id"], action.model_dump() if action and not error else None,
                                 error.code if error else None)
                if not error:
                    await checkpoints.save(db, claim, row, "finish" if isinstance(action, Final) else "tool")
        if delay is not None:
            raise RetryScheduled()
        if error:
            raise error
        return action

    @staticmethod
    async def settle(db, invocation, usage: Usage | None, prices, request_id, latency, error):
        status = "reported" if usage else "rejected" if error and error.rejected else "unknown"
        await db.execute(text("""UPDATE usage_records SET status=:status,input_tokens=:prompt,output_tokens=:output,
            estimated_cost=:cost,latency_ms=:latency,provider_request_id=:rid,error_code=:error WHERE id=:id"""),
            {"status": status, "prompt": usage.prompt_tokens if usage else None,
             "output": usage.completion_tokens if usage else None,
             "cost": prices.cost(usage.prompt_tokens, usage.completion_tokens) if prices and usage else None,
             "latency": latency, "rid": request_id, "error": error.code if error else None, "id": invocation})
