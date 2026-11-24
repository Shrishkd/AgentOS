import asyncio
import socket
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from agentos import sample_database, tool_egress
from agentos.providers import ModelFailure
from agentos.tool_catalog import CATALOG, Inventory
from agentos.tool_gateway import bounded, calculate, digest


def test_calculator_supports_only_bounded_arithmetic():
    assert calculate("2 * -(1 + 3) / 4") == {"value": -2}
    for expression in ("some_function(2)", "True", "10 ** 10", "1 / 0", "1e100", "+".join(["1"] * 40)):
        with pytest.raises(ModelFailure, match="invalid_expression"):
            calculate(expression)


def test_inventory_uses_the_separate_sample(tmp_path):
    path = tmp_path / "inventory.sqlite3"
    sample_database.build(path)
    result = sample_database.lookup(Inventory(category="software", limit=1), path)
    assert result == {"items": [{"name": "Editor license", "quantity": 5}]}


def test_digest_binds_normalized_content_and_operation():
    ids = [uuid4() for _ in range(4)]
    args = {"title": "Report", "sections": ["sample"]}
    key = digest(*ids, args)
    assert key == digest(*ids, dict(reversed(list(args.items()))))
    assert key != digest(*ids, {**args, "title": "Another"})
    for i in range(4):
        other = ids.copy()
        other[i] = uuid4()
        assert key != digest(*other, args)


def test_output_limit_counts_serialized_bytes():
    with pytest.raises(ModelFailure, match="tool_output_too_large"):
        bounded(CATALOG["web_search"], {"titles": ["x" * 17000]})


@pytest.mark.parametrize("url", ["http://www.rfc-editor.org/", "https://example.org/",
                                 "https://www.rfc-editor.org:444/", "https://user@www.rfc-editor.org/"])
def test_get_rejects_non_allowlisted_destinations(url):
    with pytest.raises(ModelFailure, match="tool_destination_denied"):
        tool_egress.destination(url, "www.rfc-editor.org")


def records(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 443)) for ip in ips]


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "169.254.0.1", "224.0.0.1"])
def test_all_addresses_must_be_public(ip):
    with pytest.raises(ModelFailure, match="tool_destination_denied"):
        tool_egress.public_ip(records("8.8.8.8", ip))


@pytest.mark.parametrize("mode", ["success", "redirect", "size", "timeout"])
def test_pinned_https_request(monkeypatch, mode):
    client_type = httpx.AsyncClient

    def handle(request):
        assert request.url.host == "8.8.8.8"
        assert request.headers["host"] == request.extensions["sni_hostname"] == "www.rfc-editor.org"
        if mode == "timeout":
            raise httpx.ReadTimeout("fixture")
        return httpx.Response(302 if mode == "redirect" else 200, headers={"location": "https://example.org/"},
                              stream=httpx.ByteStream(b"sample"))

    async def run():
        resolve = AsyncMock(return_value=records("8.8.8.8"))
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw))
        result = await tool_egress.fetch("https://www.rfc-editor.org/", "www.rfc-editor.org", 2 if mode == "size" else 25)
        resolve.assert_awaited_once()
        return result

    if mode == "success":
        assert asyncio.run(run()) == "sample"
    else:
        expected = {"size": "tool_output_too_large", "redirect": "tool_http_response", "timeout": "tool_timeout"}
        with pytest.raises(ModelFailure, match=expected[mode]):
            asyncio.run(run())


def test_search_returns_bounded_titles(monkeypatch):
    fetch = AsyncMock(return_value='{"query":{"search":[{"title":"One"},{"title":"Two"}]}}')
    monkeypatch.setattr(tool_egress, "fetch", fetch)
    assert asyncio.run(tool_egress.search("project plan")) == {"titles": ["One", "Two"]}
    url, host, limit = fetch.await_args.args
    assert host == "en.wikipedia.org" and "srsearch=project+plan" in url and limit == 32768
