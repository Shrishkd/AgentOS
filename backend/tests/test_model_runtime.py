import asyncio
from decimal import Decimal

import httpx
import pytest

from agentos.model_engine import allocation
from agentos.providers import ChatAdapter, ModelFailure, Prices, Reply, retry_delay


def test_budget_reserves_input_and_caps_output_at_remaining_cost():
    prices = Prices(Decimal("1"), Decimal("4"))
    limits = {"max_tokens": 1000, "max_cost_usd": "0.0002"}
    assert allocation(limits, 100, 0, Decimal("0.00002"), prices, 512) == 20
    with pytest.raises(ModelFailure, match="pricing_unavailable"):
        allocation(limits, 100, 0, 0, None, 512)
    with pytest.raises(ModelFailure, match="token_budget_exhausted"):
        allocation(limits, 100, 900, 0, prices, 512)


@pytest.mark.parametrize("message,reason", [
    ({"content": "\x00"}, "stop"),
    ({"tool_calls": []}, "tool_calls"),
    ({"tool_calls": [{"id": "ok", "type": "function", "function": {"name": "text_length", "arguments": "[]"}}]}, "tool_calls"),
    ({"tool_calls": [{"id": "ok", "type": "function", "function": {"name": "text_length", "arguments": "{"}}]}, "tool_calls"),
])
def test_rejects_unusable_model_actions(message, reason):
    reply = Reply({"choices": [{"message": message, "finish_reason": reason}]}, None)
    with pytest.raises(ModelFailure, match="invalid_model_response"):
        reply.action()


@pytest.mark.parametrize("reason,code", [("length", "output_limit_exceeded"), ("content_filter", "model_refused")])
def test_recognizes_nonfinal_stop_reasons(reason, code):
    with pytest.raises(ModelFailure, match=code):
        Reply({"choices": [{"message": {}, "finish_reason": reason}]}, None).action()


def test_missing_usage_is_unknown():
    with pytest.raises(ModelFailure, match="provider_usage_missing"):
        Reply({"usage": {"prompt_tokens": -1, "completion_tokens": 5}}, None).usage()


@pytest.mark.parametrize("status,code,retryable,rejected", [
    (401, "provider_request_rejected", False, True),
    (302, "provider_request_rejected", False, True),
    (429, "provider_rate_limited", True, True),
    (503, "provider_unavailable", True, False),
])
def test_normalized_http_failures(status, code, retryable, rejected):
    async def run():
        def handle(request):
            return httpx.Response(status, headers={"Retry-After": "2"}, content=b"private-sentinel")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter = ChatAdapter(client, "https://fixture.invalid/v1/chat/completions", 1)
            with pytest.raises(ModelFailure) as error:
                await adapter.complete("model", [], [], 1)
            assert str(error.value) == code
            assert (error.value.retryable, error.value.rejected) == (retryable, rejected)
            if status == 429:
                assert error.value.retry_after == 2
    asyncio.run(run())


def test_retry_after_is_finite_and_nonnegative():
    for value in (None, "negative", "NaN", "inf", "-1", "Sun, 06 Nov 1994 08:49:37 GMT"):
        assert retry_delay(value) == 0
