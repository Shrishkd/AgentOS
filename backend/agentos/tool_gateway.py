"""Authorize a pinned tool at dispatch and execute only its deployed implementation."""
import ast
import asyncio
import hashlib
import json
import math
import operator
import sqlite3

from pydantic import ValidationError
from sqlalchemy import text

from . import sample_database, tool_egress
from .providers import ModelFailure, compact
from .scheduler import ApprovalNeeded


def digest(tenant, run, step, version, arguments):
    # Protected schemas have fixed ASCII keys and string/list values. Compact sorted UTF-8
    # JSON is RFC 8785 canonical JSON for this restricted domain.
    document = {"tenant_id": str(tenant), "run_id": str(run), "step_id": str(step),
                "tool_version_id": str(version), "arguments": arguments}
    return hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


async def authorize(db, claim, row, tool, action):
    if row["request"] != action.model_dump():
        raise ModelFailure("inconsistent_step_history")
    try:
        args = tool.inputs.model_validate(action.arguments)
    except ValidationError:
        raise ModelFailure("invalid_tool_arguments") from None
    if tool.name == "http_get":
        tool_egress.destination(args.url, "www.rfc-editor.org")
    if tool.risk != "high":
        return args
    values = args.model_dump()
    key = digest(claim.tenant, claim.run, row["id"], row["tool_version_id"], values)
    params = {"tenant": claim.tenant, "run": claim.run, "step": row["id"], "tool": row["tool_version_id"],
              "name": tool.name, "args": compact(values), "digest": key, "risk": tool.risk}
    approval = (await db.execute(text("""SELECT status,arguments,action_digest,tool_version_id,
        expires_at<=clock_timestamp() AS expired FROM approvals WHERE tenant_id=:tenant AND run_id=:run
        AND step_id=:step"""), params)).mappings().one_or_none()
    if approval is None:
        await db.execute(text("""INSERT INTO approvals(tenant_id,run_id,step_id,tool_version_id,tool_name,
            arguments,action_digest,risk,expires_at,wait_seq) SELECT :tenant,:run,:step,:tool,:name,
            CAST(:args AS jsonb),:digest,:risk,least(deadline_at,clock_timestamp()+interval '15 minutes'),revision+1
            FROM runs WHERE id=:run"""), params)
        raise ApprovalNeeded()
    if approval["expired"]:
        raise ModelFailure("approval_expired")
    if (approval["status"] != "approved" or approval["action_digest"] != key
            or approval["arguments"] != values or approval["tool_version_id"] != row["tool_version_id"]):
        raise ModelFailure("approval_mismatch")
    return args


OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def calculate(expression):
    def value(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            number = node.value
        elif isinstance(node, ast.UnaryOp) and type(node.op) in (ast.USub, ast.UAdd):
            number = value(node.operand)
            if isinstance(node.op, ast.USub):
                number = -number
        elif isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            number = OPERATORS[type(node.op)](value(node.left), value(node.right))
        else:
            raise ValueError()
        if not math.isfinite(number) or abs(number) > 10**15:
            raise ValueError()
        return number
    try:
        tree = ast.parse(expression, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 64:
            raise ValueError()
        return {"value": value(tree.body)}
    except (SyntaxError, ValueError, ArithmeticError, RecursionError):
        raise ModelFailure("invalid_expression") from None


async def text_length(operation_id, args):
    return {"length": len(args.text)}


def bounded(tool, result):
    if len(compact(result).encode()) > tool.output_bytes:
        raise ModelFailure("tool_output_too_large")
    return result


async def save_report(db, claim, step_id, args):
    await db.execute(text("""INSERT INTO tool_reports(id,tenant_id,run_id,document)
        VALUES (:id,:tenant,:run,CAST(:doc AS jsonb))"""), {"id": step_id, "tenant": claim.tenant,
                                                            "run": claim.run, "doc": args.model_dump_json()})
    return {"report_id": str(step_id), "title": args.title}


async def read_tool(tool, step_id, args):
    try:
        async with asyncio.timeout(tool.timeout):
            if tool.name == "text_length":
                result = await text_length(step_id, args)
            elif tool.name == "calculator":
                result = calculate(args.expression)
            elif tool.name == "inventory_lookup":
                result = await asyncio.to_thread(sample_database.lookup, args)
            elif tool.name == "web_search":
                result = await tool_egress.search(args.query)
            elif tool.name == "http_get":
                result = {"text": await tool_egress.fetch(args.url, "www.rfc-editor.org", tool.output_bytes)}
            else:
                raise ModelFailure("tool_not_permitted")
            return bounded(tool, result)
    except TimeoutError:
        raise ModelFailure("tool_timeout") from None
    except sqlite3.Error:
        raise ModelFailure("tool_storage_unavailable") from None
