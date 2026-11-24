"""Bounded HTTPS reads to fixed public origins, with DNS pinning and hostname verification."""
import asyncio
import ipaddress
import json
import socket
from urllib.parse import urlencode, urlsplit

import httpx

from .providers import ModelFailure


def destination(url, host):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc not in {host, host + ":443"}
                or parsed.fragment or not url.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in url)
                or "\\" in url):
            raise ValueError()
        return httpx.URL(url)
    except (ValueError, httpx.InvalidURL):
        raise ModelFailure("tool_destination_denied") from None


def public_ip(records):
    addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    if not addresses or any(not ip.is_global or ip.is_multicast or ip.is_reserved
                            or getattr(ip, "ipv4_mapped", None) or getattr(ip, "sixtofour", None)
                            or getattr(ip, "teredo", None) for ip in addresses):
        raise ModelFailure("tool_destination_denied")
    return str(addresses[0])


async def fetch(url, host, limit):
    target = destination(url, host)
    try:
        records = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        pinned = target.copy_with(host=public_ip(records))
        # One connection per invocation prevents reuse across TLS hostnames sharing an IP.
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
            async with client.stream("GET", pinned, headers={"Host": host, "Accept-Encoding": "identity",
                    "User-Agent": "AgentOS/0.1 (bounded research tool)"}, extensions={"sni_hostname": host}) as reply:
                if reply.status_code in (502, 503, 504):
                    raise ModelFailure("tool_http_busy")
                if reply.status_code != 200 or reply.headers.get("content-encoding", "identity") != "identity":
                    raise ModelFailure("tool_http_response")
                data = bytearray()
                async for chunk in reply.aiter_raw(chunk_size=4096):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ModelFailure("tool_output_too_large")
                return data.decode("utf-8")
    except (TimeoutError, httpx.TimeoutException):
        raise ModelFailure("tool_timeout") from None
    except (OSError, httpx.RequestError, UnicodeError):
        raise ModelFailure("tool_http_unavailable") from None


async def search(query):
    params = urlencode({"action": "query", "list": "search", "srsearch": query, "srlimit": 5,
                        "srprop": "", "format": "json"})
    raw = await fetch("https://en.wikipedia.org/w/api.php?" + params, "en.wikipedia.org", 32768)
    try:
        items = json.loads(raw)["query"]["search"]
        return {"titles": [str(item["title"]) for item in items[:5]]}
    except (TypeError, ValueError, KeyError, RecursionError):
        raise ModelFailure("tool_http_response") from None
