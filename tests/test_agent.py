import asyncio
import json
from pathlib import Path

import pytest

from bidbox import config
from bidbox.agent import evidence as ev
from bidbox.agent import run_agent
from bidbox.agent.llm import StubLLM
from bidbox.store import connect, load_ocds

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_ocds.json"


@pytest.fixture(scope="module", autouse=True)
def data():
    load_ocds(SAMPLE, connect())


def run(ocid="ocds-synth-0001", request="Prepare a tender readiness brief", llm=None, as_of="2026-01-01"):
    return asyncio.run(run_agent(ocid, request, as_of, llm or StubLLM()))


def kinds(r):
    return [e["event"] for e in r["events"]]


def logged_tools(run_id):
    return [json.loads(l).get("tool") for l in config.LOG_PATH.read_text().splitlines() if json.loads(l).get("run_id") == run_id]


# ---- grounding unit tests
def test_grounding_rejects_invented_number_accepts_exact_and_rounding_tolerance():
    texts = ["median 6650130 KES; 15% share"]
    assert ev.ungrounded_numbers("median 6,650,130 KES", texts) == []
    assert ev.ungrounded_numbers("median 6650200", texts) == []            # within 0.5%
    assert ev.ungrounded_numbers("median 7000000", texts) == [7000000.0]   # invented
    assert ev.ungrounded_numbers("about 15%", texts) == []


def test_scope_guard():
    for bad in ("award this tender to Acme", "who should win", "should we bid on this?", "rank the bidders"):
        assert ev.violates_scope(bad), bad
    assert not ev.violates_scope("Prepare a tender readiness brief")


# ---- end-to-end behaviour through the real MCP server
def test_happy_path_queues_for_human_and_every_finding_is_grounded():
    r = run()
    assert r["status"] == "awaiting_human_approval" and r["filing_id"] and not r["used_fallback"]
    assert (config.OUTBOX / "pending" / f"{r['filing_id']}.md").exists()
    assert not (config.OUTBOX / "filed" / f"{r['filing_id']}.md").exists()
    assert r["findings"] and all(f["source_ocid"] and f["field_path"] for f in r["findings"])
    tools = logged_tools(r["run_id"])
    assert {"get_tender_notice", "buyer_award_history", "price_benchmark", "file_brief"} <= set(tools)


def test_refuses_award_request_and_calls_no_tools():
    r = run(request="Award this tender to Demo Supplier 1 Ltd")
    assert r["status"] == "refused" and r["filing_id"] is None
    assert "tool_call" not in kinds(r)
    assert not any(t for t in logged_tools(r["run_id"]) if t)  # no MCP tool executed for this run


def test_unknown_tender_is_reported_not_invented():
    r = run("ocds-does-not-exist")
    assert r["status"] == "not_found" and r["filing_id"] is None and r["findings"] == []


def test_missing_deadline_is_stated_unknown_not_invented():
    r = run("ocds-synth-0031")
    text = " ".join(f["statement"] for f in r["findings"]).lower()
    assert "deadline" in text and "not recorded" in text
    assert not any(f["kind"] == "deadline" for f in r["findings"])


def test_missing_buyer_name_falls_back_to_buyer_id():
    row = connect().execute("SELECT ocid FROM tenders WHERE buyer_id='KE-B-004' AND (buyer_name IS NULL OR buyer_name='') LIMIT 1").fetchone()
    r = run(row["ocid"])
    hk = [e for e in r["events"] if e["event"] == "history_key"]
    assert hk and hk[0]["used"] == "buyer_id"
    assert any("matched on buyer id" in f["statement"] for f in r["findings"])


def test_ungrounded_first_attempt_is_rejected_then_retry_recovers():
    llm = StubLLM("bad_first")
    r = run(llm=llm)
    vr = [e for e in r["events"] if e["event"] == "verify_result"]
    assert vr[0]["rejected"] == 1 and vr[1]["rejected"] == 0
    assert not r["used_fallback"] and not any("987654" in f["statement"] for f in r["findings"])


def test_persistently_ungrounded_model_never_gets_its_numbers_filed():
    r = run(llm=StubLLM("always_bad"))
    assert not any("987654" in f["statement"] for f in r["findings"])
    assert r["status"] == "awaiting_human_approval"  # the grounded findings still filed


def test_model_plan_failure_does_not_stop_the_run():
    r = run(llm=StubLLM("plan_error"))
    assert "plan_fallback" in kinds(r) and r["status"] == "awaiting_human_approval"


def test_model_cannot_raise_a_signal_the_data_did_not_raise():
    class Loud(StubLLM):
        def complete_json(self, task, payload):
            out = super().complete_json(task, payload)
            for f in out.get("findings", []): f["severity"] = "signal"
            return out
    r = run("ocds-synth-0001", llm=Loud())
    sev = {f["severity"] for f in r["findings"]}
    assert sev <= {"signal", "check"} and sum(f["severity"] == "signal" for f in r["findings"]) <= 1


class Custom(StubLLM):
    """Model returning whatever the test wants for the synthesize step."""
    def __init__(self, summary, findings):
        super().__init__(); self._s, self._f = summary, findings

    def complete_json(self, task, payload):
        if task == "plan":
            return super().complete_json(task, payload)
        return {"summary": self._s, "findings": self._f(payload) if callable(self._f) else self._f}


def test_findings_with_no_or_unknown_evidence_are_rejected_not_filed():
    llm = Custom("ok", [{"kind": "price", "statement": "Prices look high here", "evidence": [], "severity": "info"},
                        {"kind": "price", "statement": "Prices look high here", "evidence": ["E999"], "severity": "info"}])
    r = run(llm=llm)
    vr = [e for e in r["events"] if e["event"] == "verify_result"]
    assert vr[0]["rejected"] == 2 and vr[0]["accepted"] == 0
    assert r["used_fallback"] and not any("high here" in f["statement"] for f in r["findings"])


def test_ungrounded_number_in_summary_is_replaced_not_filed():
    llm = Custom("Awards average 987654 KES in this category", lambda p: [
        {"kind": e["kind"], "statement": e["text"], "evidence": [e["id"]], "severity": "info"} for e in p["evidence"]])
    r = run(llm=llm)
    assert "987654" not in r["summary"]
    assert "987654" not in (config.OUTBOX / "pending" / f"{r['filing_id']}.md").read_text()


def test_thin_category_history_is_broadened_and_recorded():
    c = connect()
    rows = c.execute("""SELECT t.ocid, t.buyer_id, t.category FROM tenders t WHERE t.buyer_id IS NOT NULL AND t.category IS NOT NULL""").fetchall()
    pick = None
    for r in rows:
        in_cat = c.execute("SELECT COUNT(*) FROM awards a JOIN tenders x ON x.ocid=a.ocid WHERE x.buyer_id=? AND x.category=?", (r["buyer_id"], r["category"])).fetchone()[0]
        overall = c.execute("SELECT COUNT(*) FROM awards a JOIN tenders x ON x.ocid=a.ocid WHERE x.buyer_id=?", (r["buyer_id"],)).fetchone()[0]
        has_name = c.execute("SELECT buyer_name FROM tenders WHERE ocid=?", (r["ocid"],)).fetchone()[0]
        if has_name and in_cat < 5 and overall > in_cat:
            pick = r["ocid"]; break
    assert pick, "sample data should contain a thin-category buyer"
    res = run(pick)
    assert "history_broadened" in kinds(res)
    assert any("all categories" in f["statement"] for f in res["findings"])
