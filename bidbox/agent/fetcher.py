"""Borrowed MCP server: the official `fetch` server (mcp-server-fetch), reached over stdio.
We do not write HTTP, robots.txt, redirect or truncation handling ourselves: the maintained server does it.
`raw` mode is used so no Node/npm step is needed. Document text is UNTRUSTED data."""
import asyncio
import html
import ipaddress
import os
import re
import sys
from urllib.parse import urlparse
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TIMEOUT_S = 45


@asynccontextmanager
async def fetch_session():
    args = ["-m", "mcp_server_fetch"]
    if os.environ.get("BIDBOX_ALLOW_PRIVATE_FETCH") == "1":
        args.append("--allow-private-ips")
    params = StdioServerParameters(command=sys.executable, args=args, env={**os.environ})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            yield s


def _clean(raw: str, url: str) -> str:
    body = raw.split(f"Contents of {url}:", 1)[-1]
    body = re.sub(r"(?is)<(script|style).*?</\1>", " ", body)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", html.unescape(body)).strip()


def url_allowed(url: str) -> tuple[bool, str]:
    """Tender data is untrusted: do not let it point the agent at this machine or its network.
    Literal private/loopback/link-local IPs and obvious local host names are refused (set BIDBOX_ALLOW_PRIVATE_FETCH=1 for tests).
    Not covered: a public name that resolves to a private address, or a redirect to one."""
    if os.environ.get("BIDBOX_ALLOW_PRIVATE_FETCH") == "1":
        return True, ""
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
    except ValueError:
        return False, "invalid URL"
    if u.scheme not in ("http", "https") or not host:
        return False, "only http(s) web addresses are fetched"
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return False, "local or internal host name"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True, ""
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return False, "private or reserved address"
    return True, ""


async def read_document(session: ClientSession, url: str, max_length: int = 4000) -> dict:
    """Never raises: a dead link, timeout or empty page becomes ok=False so the agent can recover."""
    allowed, why = url_allowed(url)
    if not allowed:
        return {"url": url, "ok": False, "error": f"URL not allowed ({why})"}
    try:
        res = await asyncio.wait_for(session.call_tool("fetch", {"url": url, "max_length": max_length, "raw": True}), TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        return {"url": url, "ok": False, "error": repr(e)[:150]}
    txt = res.content[0].text if res.content else ""
    if res.isError:
        return {"url": url, "ok": False, "error": txt[:150]}
    text = _clean(txt, url)
    if not text:
        return {"url": url, "ok": False, "error": "document was empty"}
    return {"url": url, "ok": True, "text": text[:600], "chars": len(text)}
