"""Borrowed MCP server: the official `fetch` server (mcp-server-fetch), reached over stdio.

We do not write HTTP, robots.txt, redirect or truncation handling ourselves: the maintained server does it.
`raw` mode is used so no Node/npm step is needed. Document text is UNTRUSTED data.
"""
import asyncio
import html
import ipaddress
import os
import re
import socket
import sys
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

try:
    from mcp.client.stdio import get_default_environment
except ImportError:  # older SDKs: fall back to a minimal allowlist
    def get_default_environment() -> dict[str, str]:
        keys = ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER", "LANG", "SYSTEMROOT", "TEMP", "TMP")
        return {k: os.environ[k] for k in keys if k in os.environ}

TIMEOUT_S = 45
RAW_FETCH_CHARS = 20_000   # raw HTML is mostly markup; 4,000 raw characters were often all <head>/script and cleaned to nothing
MAX_TEXT_CHARS = 600       # what the agent keeps of a document

# The borrowed server gets a minimal environment, not ours (which may hold API keys), plus what it needs to reach the web.
_PASSTHROUGH_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
                    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE")
_LOCAL_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".lan", ".home.arpa")

_SCRIPT_STYLE = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>")
_COMMENT = re.compile(r"(?s)<!--.*?-->")
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def _allow_private() -> bool:
    return os.environ.get("WAZI_ALLOW_PRIVATE_FETCH") == "1"


def _server_env() -> dict[str, str]:
    env = get_default_environment()
    env.update({k: os.environ[k] for k in _PASSTHROUGH_ENV if k in os.environ})
    return env


@asynccontextmanager
async def fetch_session():
    args = ["-m", "mcp_server_fetch"]
    if _allow_private():
        args.append("--allow-private-ips")
    params = StdioServerParameters(command=sys.executable, args=args, env=_server_env())
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            yield s


def _clean(raw: str, url: str) -> str:
    body = raw.split(f"Contents of {url}:", 1)[-1]
    body = _COMMENT.sub(" ", _SCRIPT_STYLE.sub(" ", body))
    body = _TAG.sub(" ", body)
    return _SPACE.sub(" ", html.unescape(body)).strip()


def _as_ip(host: str):
    """Parse a host as an IP address, including the legacy IPv4 spellings resolvers still accept
    (2130706433, 0x7f.1, 0177.0.0.1, 127.1). Returns None for an ordinary host name."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
        if host[:1].isdigit():
            try:
                ip = ipaddress.IPv4Address(socket.inet_aton(host))
            except (OSError, ValueError):
                ip = None
    if ip is not None and ip.version == 6 and ip.ipv4_mapped:   # ::ffff:127.0.0.1
        ip = ip.ipv4_mapped
    return ip


def url_allowed(url: str) -> tuple[bool, str]:
    """Tender data is untrusted: do not let it point the agent at this machine or its network.

    Refused: non-http(s) schemes, obvious local host names, and literal private, loopback, link-local,
    reserved, multicast, unspecified or otherwise non-global IPs (including IPv4-mapped IPv6 and
    legacy numeric spellings). Set WAZI_ALLOW_PRIVATE_FETCH=1 for tests.
    Not covered here (the fetch server applies its own check): a public name that resolves to a private
    address, or a redirect to one.
    """
    if _allow_private():
        return True, ""
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower().rstrip(".")
    except ValueError:
        return False, "invalid URL"
    if u.scheme not in ("http", "https") or not host:
        return False, "only http(s) web addresses are fetched"
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return False, "local or internal host name"
    ip = _as_ip(host)
    if ip is not None and (not ip.is_global or ip.is_multicast):
        return False, "private or reserved address"
    return True, ""


async def read_document(session: ClientSession, url: str, max_length: int = RAW_FETCH_CHARS) -> dict:
    """Never raises: a dead link, timeout or empty page becomes ok=False so the agent can recover.

    On success `text` is the cleaned text cut to MAX_TEXT_CHARS and `chars` is its length before that cut.
    """
    allowed, why = url_allowed(url)
    if not allowed:
        return {"url": url, "ok": False, "error": f"URL not allowed ({why})"}
    try:
        res = await asyncio.wait_for(
            session.call_tool("fetch", {"url": url, "max_length": max_length, "raw": True}), TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        return {"url": url, "ok": False, "error": repr(e)[:150]}
    txt = (getattr(res.content[0], "text", "") or "") if res.content else ""
    if res.isError:
        return {"url": url, "ok": False, "error": txt[:150]}
    text = _clean(txt, url)
    if not text:
        return {"url": url, "ok": False, "error": "document was empty"}
    return {"url": url, "ok": True, "text": text[:MAX_TEXT_CHARS], "chars": len(text)}