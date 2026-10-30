"""In-process stand-in for the MCP tool session, for FAST tests of agent logic (about 20x faster than spawning servers).
It keeps the interface the graph uses, call_tool(name, args), and skips ONLY the process boundary and JSON-RPC: the tool
functions, their audit logging and their errors are the real ones. A small set of true-MCP contract tests still covers the wiring."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from .. import audit_log, tools

# exactly what wazi.server exposes (tests/test_mcp_contract.py asserts this equals the real server's tool list):
TOOLS = ("get_tender_notice", "buyer_award_history", "price_benchmark", "peer_baseline", "window_baseline",
         "counterparty_quality", "amendment_check", "aged_items_check", "budget_exposure", "ppada_date_order_check",
         "single_bidder_check", "termination_check", "publication_quality_check", "process_timeline_check", "file_brief")


def _result(text: str, is_error: bool = False):
    """Shaped like an MCP CallToolResult as far as the graph and fetcher read it: isError, content[i].type/.text."""
    return SimpleNamespace(isError=is_error, content=[SimpleNamespace(type="text", text=text)])


class InProcSession:
    def __init__(self, run_id: str):
        self.run_id = run_id

    async def call_tool(self, name: str, args: dict | None = None):
        if name not in TOOLS:
            return _result(f"unknown tool {name!r}", True)
        tok = audit_log.run_id_var.set(self.run_id)         # server-side logs carry the AGENT's run id, as they do across processes
        try:
            out = getattr(tools, name)(**(args or {}))
            return _result(json.dumps(out, default=str))
        except Exception as e:  # noqa: BLE001 - the real server reports tool errors the same way
            return _result(str(e) or repr(e), True)
        finally:
            audit_log.run_id_var.reset(tok)


@asynccontextmanager
async def inproc_session(run_id: str):
    yield InProcSession(run_id)