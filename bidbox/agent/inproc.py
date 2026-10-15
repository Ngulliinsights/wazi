"""In-process stand-in for the MCP tool session, for FAST tests of agent logic (about 20x faster than spawning servers).
It keeps the interface the graph uses, call_tool(name, args), and skips ONLY the process boundary and JSON-RPC: the tool
functions, their audit logging and their errors are the real ones. A small set of true-MCP contract tests still covers the wiring."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from .. import audit_log, tools

# exactly what bidbox.server exposes (tests/test_mcp_contract.py asserts this equals the real server's tool list):
TOOLS = ("get_tender_notice", "buyer_award_history", "price_benchmark", "peer_baseline", "window_baseline", "file_brief")


class InProcSession:
    def __init__(self, run_id: str):
        self.run_id = run_id

    async def call_tool(self, name: str, args: dict):
        tok = audit_log.run_id_var.set(self.run_id)         # server-side logs carry the AGENT's run id, as they do across processes
        try:
            if name not in TOOLS:
                raise ValueError(f"unknown tool {name!r}")
            out = getattr(tools, name)(**args)
            return SimpleNamespace(isError=False, content=[SimpleNamespace(text=json.dumps(out, default=str))])
        except Exception as e:  # noqa: BLE001 - the real server reports tool errors the same way
            return SimpleNamespace(isError=True, content=[SimpleNamespace(text=str(e))])
        finally:
            audit_log.run_id_var.reset(tok)


@asynccontextmanager
async def inproc_session(run_id: str):
    yield InProcSession(run_id)
