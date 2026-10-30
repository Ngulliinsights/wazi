"""The wiring contract: these run the REAL servers as subprocesses (the other tests use the fast in-process session)."""
import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wazi import api, audit_log, config, provenance
from wazi.agent import run_agent
from wazi.agent.graph import mcp_session
from wazi.agent.inproc import TOOLS
from wazi.agent.llm import StubLLM
from wazi.evals import build_fixture_releases
from wazi.store import connect, load_ocds

pytestmark = pytest.mark.mcp
SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_ocds.json"


@pytest.fixture(scope="module", autouse=True)
def data(tmp_path_factory):
    f = tmp_path_factory.mktemp("d") / "fx.json"; f.write_text(json.dumps({"releases": build_fixture_releases()}), encoding="utf-8")
    c = connect(); load_ocds(SAMPLE, c); load_ocds(f, c)


def test_the_fast_in_process_session_exposes_exactly_what_the_real_server_exposes():
    from wazi.server import mcp
    assert set(TOOLS) == {t.name for t in asyncio.run(mcp.list_tools())}


def test_real_mcp_run_links_logs_filing_and_manifest_by_run_id_and_replays():
    r = asyncio.run(run_agent("ocds-eval-abv-target", "x", "2025-05-01", StubLLM("invest_all"), audience="buyer"))
    assert r["status"] == "awaiting_human_approval"
    server_side = [l for l in audit_log.read_all(r["run_id"]) if l.get("tool") == "file_brief" and "output" in l]
    assert len(server_side) == 1                                       # logged by the SERVER PROCESS under the agent's run id
    man = json.loads((config.OUTBOX / "pending" / f"{r['filing_id']}.manifest.json").read_text(encoding="utf-8"))
    assert man["run_id"] == r["run_id"] and man["md_sha256"] == r["md_sha256"]
    rep = provenance.verify_filing(r["filing_id"])                      # output hashes survive REAL serialisation and replay identically
    assert rep["verdict"] == "REPRODUCED", rep


def test_a_forged_provenance_is_refused_by_the_real_server():
    async def go():
        async with mcp_session("forge-test") as s:
            res = await s.call_tool("file_brief", {"ocid": "ocds-eval-abv-target", "summary": "s", "as_of": "2025-05-01", "audience": "buyer",
                "findings": [{"kind": "other", "statement": "A plain fact here", "source_ocid": "ocds-eval-abv-target", "field_path": "tender.title", "evidence": ["E1"]}],
                "provenance": {"tool_calls": [{"tool": "get_tender_notice", "args": {"ocid": "ocds-eval-abv-target", "as_of": "2025-05-01"}, "output_sha256": "0" * 64, "role": "notice"}]}})
            return json.loads(res.content[0].text)
    out = asyncio.run(go())
    assert out["status"] == "not_filed" and "does not replay" in out["reason"]


def test_full_stack_api_run_through_real_servers_then_verify_and_approve():
    with TestClient(api.app) as c:
        rid = c.post("/api/runs", json={"ocid": "ocds-eval-win-short", "as_of": "2025-05-01"}).json()["run_id"]
        t0 = time.time()
        while time.time() - t0 < 90 and not c.get(f"/api/runs/{rid}").json()["done"]:
            time.sleep(0.5)
        r = c.get(f"/api/runs/{rid}").json(); assert r["status"] == "awaiting_human_approval", r
        fid = r["filing_id"]
        assert c.get(f"/api/filings/{fid}/verify").json()["verdict"] == "REPRODUCED"
        h = next(f["md_sha256"] for f in c.get("/api/filings").json() if f["filing_id"] == fid)
        assert c.post(f"/api/filings/{fid}/approve", json={"approver": "Wanjiku Mwangi", "brief_sha256": h,
                                                           "role": "Procurement Officer", "date": "2026-10-30"}).status_code == 200
        assert c.get(f"/api/filings/{fid}/verify").json()["verdict"] == "REPRODUCED"      # still, after the decision


def test_a_quota_stop_surfaces_as_itself_through_the_real_servers_task_groups():
    """Real MCP runs inside anyio task groups, which wrap errors in ExceptionGroups: a quota stop must still arrive as QuotaExhausted."""
    from wazi.agent.llm import QuotaExhausted
    class Q(StubLLM):
        def complete_json(self, t, p):
            if t == "synthesize": raise QuotaExhausted("daily quota used up")
            return super().complete_json(t, p)
    before = sorted(p.name for p in (config.OUTBOX / "pending").glob("*.json"))
    with pytest.raises(QuotaExhausted, match="daily quota"):
        asyncio.run(run_agent("ocds-eval-abv-target", "x", "2025-05-01", Q("invest_all")))
    assert sorted(p.name for p in (config.OUTBOX / "pending").glob("*.json")) == before
