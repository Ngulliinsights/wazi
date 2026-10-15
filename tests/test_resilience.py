"""Anticipated blockers, tested without a real model or the real portal:
model output formats, messy open-data shapes, and a misbehaving model."""
import asyncio
import gzip
import json
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bidbox.agent import run_agent
from bidbox.agent.llm import LLMError, OllamaLLM, StubLLM, extract_json
from bidbox.store import connect, load_ocds

from pathlib import Path

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_ocds.json"


@pytest.fixture(scope="module", autouse=True)
def sample_loaded():
    load_ocds(SAMPLE, connect())


OBJ = '{"checks":["history","benchmark"],"rationale":"r"}'


# ---------- model output formats (what small open models actually emit)
@pytest.mark.parametrize("text", [
    OBJ, "```json\n" + OBJ + "\n```", "<think>reasoning with {braces} inside</think>\n" + OBJ,
    "Here is the JSON you asked for:\n" + OBJ + "\nHope this helps!"])
def test_extract_json_handles_real_world_wrappers(text):
    assert extract_json(text)["checks"] == ["history", "benchmark"]


@pytest.mark.parametrize("text", [OBJ[:-10], "[1,2]", "", "no json at all", "<think>never finished " + OBJ])
def test_extract_json_refuses_unusable_output(text):
    with pytest.raises(LLMError):
        extract_json(text)


def test_ollama_client_over_http_with_qwen3_style_reply():
    reply = {"v": "<think>hmm</think>\n" + OBJ}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(json.dumps({"choices": [{"message": {"content": reply["v"]}}]}).encode())
        def log_message(self, *a): pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        llm = OllamaLLM("m", f"http://127.0.0.1:{srv.server_address[1]}", api="openai")
        assert llm.complete_json("plan", {"facts": "x"})["checks"]
        reply["v"] = "{broken"
        with pytest.raises(LLMError):
            llm.complete_json("plan", {"facts": "x"})
    finally:
        srv.shutdown()
    with pytest.raises(LLMError):  # nothing listening = LLMError, not a crash
        OllamaLLM("m", "http://127.0.0.1:1", timeout=2).complete_json("plan", {"facts": "x"})


# ---------- messy open-data shapes
MESSY = [
    {"ocid": "m1", "buyer": "just a string", "tender": {"title": "t", "mainProcurementCategory": "goods",
     "value": {"amount": "1,200,000", "currency": "KES"}, "tenderPeriod": {"endDate": "soon"}},
     "awards": [{"id": "a", "value": {"amount": "abc"}, "suppliers": None}, {"id": "b", "value": {"amount": 500}, "suppliers": ["x", {"id": "S1"}]}]},
    {"ocid": None, "tender": {}}, {"ocid": "m3", "tender": None, "awards": "oops"},
    {"ocid": "m4", "tender": {"value": {"amount": -5}}}, {"ocid": "m1", "tender": {"title": "dup"}}, "not a dict", 42,
]


def test_loader_survives_messy_records_and_counts_what_it_did():
    import tempfile, pathlib
    f = pathlib.Path(tempfile.mkdtemp()) / "m.json"; f.write_text(json.dumps({"releases": MESSY}))
    c = connect(); st = load_ocds(f, c)
    assert st["skipped"] == 3 and st["duplicates"] == 1 and st["invalid_amounts"] >= 2
    assert st["bad_dates"] == 1 and st["coerced_numbers"] >= 1
    m4 = c.execute("SELECT value FROM tenders WHERE ocid='m4'").fetchone()[0]
    assert m4 is None                                    # non-positive value becomes unknown, not -5
    assert c.execute("SELECT deadline FROM tenders WHERE ocid='m1'").fetchone()[0] is None  # unparseable date -> unknown


def test_loader_coerces_text_numbers():
    import tempfile, pathlib
    f = pathlib.Path(tempfile.mkdtemp()) / "n.json"
    f.write_text(json.dumps([{"ocid": "n1", "tender": {"value": {"amount": "1,200,000", "currency": "KES"}}}]))
    c = connect(); load_ocds(f, c)
    assert c.execute("SELECT value FROM tenders WHERE ocid='n1'").fetchone()[0] == 1_200_000


@pytest.mark.parametrize("kind", ["jsonl", "jsonl_gz", "records", "bare_list", "pretty_package"])
def test_loader_reads_each_common_open_data_shape(kind, tmp_path):
    rel = lambda i: {"ocid": f"s-{kind}-{i}", "tender": {"title": "x"}}
    rels = [rel(1), rel(2), rel(3)]
    if kind == "jsonl":
        f = tmp_path / "a.jsonl"; f.write_text("\n".join(json.dumps(r) for r in rels) + "\n{bad line\n")
    elif kind == "jsonl_gz":
        f = tmp_path / "a.jsonl.gz"; f.write_bytes(gzip.compress("\n".join(json.dumps(r) for r in rels).encode()))
    elif kind == "records":
        f = tmp_path / "a.json"; f.write_text(json.dumps({"records": [{"compiledRelease": r} for r in rels]}))
    elif kind == "bare_list":
        f = tmp_path / "a.json"; f.write_text(json.dumps(rels))
    else:
        f = tmp_path / "a.json"; f.write_text(json.dumps({"releases": rels}, indent=2))
    st = load_ocds(f, connect())
    assert st["tenders"] == 3 and st["skipped"] == (1 if kind == "jsonl" else 0)


# ---------- a misbehaving model must not crash the agent or get invented numbers filed
class Chaos(StubLLM):
    def __init__(self, seed):
        super().__init__(); self.r = random.Random(seed)

    def complete_json(self, task, payload):
        self.calls += 1; c = self.r.randrange(10)
        if c == 0: raise LLMError("boom")
        if c == 1: return {}
        if c == 2: return {"checks": "history", "summary": 5, "findings": "none"}
        if c == 3: return {"summary": None, "findings": [None, 5, "x", {"statement": 7}, {"statement": "ok text here", "evidence": [["E1"]]}]}
        if c == 9: return ["not", "an", "object"]
        if task == "plan": return {"checks": ["history", "benchmark", "drop_tables"], "rationale": "r"}
        evs = payload["evidence"]; evs = json.loads(evs) if isinstance(evs, str) else evs
        f = [{"kind": "price", "statement": "Average is 987654321 KES", "evidence": [evs[0]["id"]], "severity": "signal"}]
        if c in (4, 5): f += [{"kind": e["kind"], "statement": e["text"], "evidence": [e["id"]], "severity": "info"} for e in evs]
        if c == 6: f = [{"kind": "price", "statement": "ok text here", "evidence": "E1", "severity": "info"}]
        if c == 7: f = [{"kind": "nonsense", "statement": "ok text here", "evidence": ["E1"], "severity": "loud"}]
        return {"summary": "s", "findings": f}


@pytest.mark.parametrize("seed", [13, 24, 28, 0, 1, 2, 3, 4, 5, 6])  # first three crashed the agent before the fix
def test_chaotic_model_never_crashes_and_never_leaks_invented_numbers(seed):
    r = asyncio.run(run_agent("ocds-synth-0001", "Prepare a tender readiness brief", "2026-01-01", Chaos(seed)))
    assert r["status"] in ("awaiting_human_approval", "not_filed")
    assert not any("987654321" in json.dumps(f) for f in r["findings"])
    assert all(f["severity"] in ("info", "check", "signal") and f["source_ocid"] for f in r["findings"])


def test_non_object_findings_are_rejected_deterministically():
    """The exact crash found by probing: a model returning strings/None/numbers inside 'findings'."""
    class Junk(StubLLM):
        def complete_json(self, task, payload):
            if task == "plan":
                return super().complete_json(task, payload)
            return {"summary": "s", "findings": [None, 5, "free text finding", ["E1"], {"statement": "ok text here", "evidence": [["E1"]]}]}
    r = asyncio.run(run_agent("ocds-synth-0001", "Prepare a tender readiness brief", "2026-01-01", Junk()))
    vr = [e for e in r["events"] if e["event"] == "verify_result"]
    assert vr and vr[0]["accepted"] == 0 and vr[0]["rejected"] == 5
    assert r["used_fallback"] and r["status"] == "awaiting_human_approval"   # grounded template floor, flagged
