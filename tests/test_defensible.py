"""Defensibility: every claim traces to a real citation; every filing carries a manifest that REPLAYS; a decision is bound to the
exact content reviewed; any tampering or data drift is detected. Fast in-process tests; the real-MCP contract is in test_mcp_contract.py."""
import asyncio
import json
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wazi import api, config, gate, provenance, store, tools
from wazi.agent import run_agent
from wazi.agent.llm import StubLLM
from wazi.evals import build_fixture_releases
from wazi.store import connect, load_ocds

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample_ocds.json"


def _rel(oc, sup=None, cat="defcat"):
    t = {"title": oc, "mainProcurementCategory": cat, "procurementMethod": "open", "tenderPeriod": {"startDate": "2025-05-02T12:00:00Z", "endDate": "2025-06-01T12:00:00Z"},
         "value": {"amount": 1000, "currency": "KES"}}
    r = {"ocid": oc, "buyer": {"id": "EV-DEF", "name": "Defensible Buyer"}, "tender": t, "awards": []}
    if sup:
        r["awards"].append({"id": "a", "value": {"amount": 1000, "currency": "KES"}, "suppliers": [{"id": sup, "name": sup}]})
    return r


@pytest.fixture(scope="module", autouse=True)
def data():
    d = Path(tempfile.mkdtemp()); (d / "fx.json").write_text(json.dumps({"releases": build_fixture_releases()}), encoding="utf-8")
    (d / "def.json").write_text(json.dumps([_rel(f"def-{k}", "S1") for k in "abcdef"] + [_rel("def-target")]), encoding="utf-8")
    c = connect(); load_ocds(SAMPLE, c); load_ocds(d / "fx.json", c); load_ocds(d / "def.json", c)


def go(ocid="ocds-eval-abv-target", as_of="2025-05-01", llm=None, aud="buyer"):
    r = asyncio.run(run_agent(ocid, "Prepare a tender readiness brief", as_of, llm or StubLLM("invest_all"), audience=aud))
    return r


def md_path(fid, d="pending"): return config.OUTBOX / d / f"{fid}.md"
def man_path(fid, d="pending"): return config.OUTBOX / d / f"{fid}.manifest.json"
def manifest(fid, d="pending"): return json.loads(man_path(fid, d).read_text(encoding="utf-8"))
def fid_ok(**kw): return go(**kw)["filing_id"]
def failed(rep): return [c["check"] for c in rep["checks"] if not c["ok"]]


# ---------------- citations must be real
CITE = lambda kind, oc, path, st="A plain fact here": [{"kind": kind, "statement": st, "source_ocid": oc, "field_path": path, "evidence": ["E1"]}]
ROLE, APPROVAL_DATE = "Procurement Officer", "2026-10-30"


@pytest.mark.parametrize("kind,oc,path,ok", [
    ("price", "ocds-synth-0001", "totally.made.up[]", False),                         # invented path
    ("other", "ocds-synth-0001", "tender.", False), ("other", "ocds-synth-0001", "../etc/passwd", False),
    ("deadline", "ocds-synth-0031", "tender.tenderPeriod.endDate", False),            # record has NO deadline
    ("data_quality", "ocds-synth-0031", "tender.tenderPeriod.endDate", True),         # ...but a finding that REPORTS the gap may cite it
    ("buyer_history", "ocds-eval-nohist", "awards[].suppliers[]", False),             # no awards on that record
    ("other", "ocds-eval-abv-target", "tender.documents[].url", False),               # no documents there
    ("other", "ocds-eval-doc-ok", "tender.documents[].url", True),
    ("deadline", "ocds-synth-0001", "tender.tenderPeriod.endDate", True),
    ("eligibility", "ocds-eval-noelig", "tender.eligibilityCriteria", False),         # eligibility absent on that record
    ("price", "ocds-eval-sup-1-0", "awards[].value.amount", True)])
def test_citations_must_point_at_something_the_cited_record_really_has(kind, oc, path, ok):
    out = tools.file_brief("ocds-eval-abv-target", "s", CITE(kind, oc, path))
    assert (out["status"] == "pending_approval") is ok, out


def test_agent_findings_are_never_silently_dropped_by_citation_checks():
    for oc in ("ocds-synth-0001", "ocds-eval-abv-target", "ocds-eval-typ-short", "ocds-eval-win-unknown", "ocds-eval-doc-ok", "ocds-synth-0031", "ocds-eval-noelig"):
        r = go(oc); assert len(manifest(r["filing_id"])["findings"]) == len(r["findings"]) > 0, oc


# ---------------- the manifest
def test_manifest_records_run_data_code_model_calls_evidence_and_cost():
    r = go(); m = manifest(r["filing_id"])
    assert m["run_id"] == r["run_id"] and m["ocid"] == "ocds-eval-abv-target" and m["audience"] == "buyer" and m["as_of"] == "2025-05-01"
    assert m["md_sha256"] == provenance.sha_file(md_path(r["filing_id"])) == r["md_sha256"]
    assert m["dataset"]["fingerprint"] == provenance.dataset(connect())["fingerprint"] and m["code_version"] == provenance.code_version()
    assert {"notice", "history", "benchmark", "window", "peers"} <= {c["role"] for c in m["tool_calls"]}
    assert all(len(c["output_sha256"]) == 64 for c in m["tool_calls"]) and m["evidence"] and all(f["evidence"] for f in m["findings"])
    tasks = [x["task"] for x in m["model_calls"]]
    assert tasks[0] == "plan" and tasks.index("investigate") < tasks.index("synthesize")            # loop mode: plan, investigate, synthesize
    syn = next(x for x in m["model_calls"] if x["task"] == "synthesize"); assert syn["prompt"]["text"] and len(syn["response"]["sha256"]) == 64
    run = m["run"]; assert run["llm"]["name"] == "stub" and run["llm"]["settings"] and run["llm_calls"] >= 2 and run["seconds_until_filing"] >= 0 and "tokens_out" in run
    footer = md_path(r["filing_id"]).read_text(encoding="utf-8").splitlines()[-1]
    assert r["run_id"] in footer and m["dataset"]["fingerprint"] in footer and m["code_version"] in footer and "replay-checked" in footer


def test_a_missing_reference_date_is_made_concrete_so_the_run_can_be_replayed_tomorrow():
    r = asyncio.run(run_agent("ocds-synth-0001", "x", None, StubLLM("invest_all"))); m = manifest(r["filing_id"])
    assert m["as_of"] == date.today().isoformat() and m["tool_calls"][0]["args"]["as_of"] == date.today().isoformat()
    assert provenance.verify_filing(r["filing_id"])["verdict"] == "REPRODUCED"


def test_same_inputs_give_identical_evidence_findings_prompts_and_tool_hashes():
    a, b = manifest(go()["filing_id"]), manifest(go()["filing_id"])
    for k in ("tool_calls", "evidence", "notes", "documents"): assert a[k] == b[k], k
    assert [(f["statement"], f["severity"], f["evidence"]) for f in a["findings"]] == [(f["statement"], f["severity"], f["evidence"]) for f in b["findings"]]
    assert [m["prompt"]["sha256"] for m in a["model_calls"]] == [m["prompt"]["sha256"] for m in b["model_calls"]]


def test_dataset_fingerprint_ignores_reloads_of_the_same_file_and_changes_with_new_data(tmp_path):
    import sqlite3
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row; conn.executescript(store.SCHEMA)
    f1 = tmp_path / "a.json"; f1.write_text(json.dumps([{"ocid": "x1"}]), encoding="utf-8"); f2 = tmp_path / "b.json"; f2.write_text(json.dumps([{"ocid": "x2"}]), encoding="utf-8")
    assert provenance.dataset(conn)["fingerprint"] == "unrecorded"
    load_ocds(f1, conn); fp1 = provenance.dataset(conn)["fingerprint"]
    load_ocds(f1, conn); assert provenance.dataset(conn)["fingerprint"] == fp1
    load_ocds(f2, conn); assert provenance.dataset(conn)["fingerprint"] != fp1


# ---------------- approval is bound to CONTENT
def test_editing_the_brief_after_filing_blocks_any_decision_even_without_a_hash():
    fid = fid_ok(); p = md_path(fid); p.write_text(p.read_text(encoding="utf-8").replace("pending human approval", "APPROVED BY REGULATOR"), encoding="utf-8")
    with pytest.raises(gate.ConflictError, match="changed after it was filed"):
        gate.approve(fid, "Real Person", gate.brief_sha256(fid), ROLE, APPROVAL_DATE)
    assert p.exists() and not md_path(fid, "filed").exists()


@pytest.mark.parametrize("wrong", ["0" * 64, "abc", "ab" * 6, " "])
def test_a_decision_must_name_the_exact_brief_that_was_reviewed(wrong):
    fid = fid_ok()
    with pytest.raises(gate.ConflictError, match="not the brief you reviewed"):
        gate.approve(fid, "Real Person", wrong, ROLE, APPROVAL_DATE)
    assert md_path(fid).exists()


def test_a_decision_requires_a_fingerprint():
    fid = fid_ok()
    with pytest.raises(gate.ConflictError, match="brief_sha256 is required"):
        gate.approve(fid, "Real Person", "", ROLE, APPROVAL_DATE)
    assert md_path(fid).exists()


def test_correct_fingerprint_full_or_prefix_is_accepted_and_the_decision_records_the_chain():
    fid = fid_ok(); h = gate.brief_sha256(fid)
    rec = gate.approve(fid, "Wanjiku Mwangi", h[:12].upper(), ROLE, APPROVAL_DATE)
    man_sha = provenance.sha_file(man_path(fid, "filed"))
    assert rec["md_sha256"] == h and rec["manifest_sha256"] == man_sha and rec["confirmed_sha256"] == h[:12].upper()
    assert man_path(fid, "filed").exists() and not man_path(fid).exists()
    fid2 = fid_ok(); assert gate.reject(fid2, "Otieno Achieng", "no", gate.brief_sha256(fid2), ROLE, APPROVAL_DATE)["action"] == "rejected"


def test_the_pending_list_always_shows_the_current_content_fingerprint():
    fid = fid_ok(); before = next(f for f in gate.list_pending() if f["filing_id"] == fid)["md_sha256"]
    p = md_path(fid); p.write_text(p.read_text(encoding="utf-8") + "\nedited", encoding="utf-8")
    assert next(f for f in gate.list_pending() if f["filing_id"] == fid)["md_sha256"] != before


# ---------------- verify: an auditor's independent check
def test_a_fresh_filing_and_an_approved_one_both_verify():
    fid = fid_ok(); rep = provenance.verify_filing(fid)
    assert rep["verdict"] == "REPRODUCED" and not failed(rep) and len(rep["checks"]) >= 7
    man = json.loads(man_path(fid).read_text(encoding="utf-8"))
    assert man["independent_validation"]["brief_findings"]["valid"]
    assert "Independent data checks" in md_path(fid).read_text(encoding="utf-8")
    assert any(c["check"] == "independent filing checks match the manifest" and c["ok"] for c in rep["checks"])
    gate.approve(fid, "Wanjiku Mwangi", gate.brief_sha256(fid), ROLE, APPROVAL_DATE)
    rep2 = provenance.verify_filing(fid); assert rep2["verdict"] == "REPRODUCED" and rep2["state"] == "filed"
    assert any(c["check"].startswith("approver is a named person") and c["ok"] for c in rep2["checks"])
    assert any(c["check"] == "approver role and date are complete and valid" and c["ok"] for c in rep2["checks"])


def test_tampering_with_independent_validation_is_detected():
    fid = fid_ok()
    path = man_path(fid)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["independent_validation"]["tender_integrity"]["valid"] = False
    path.write_text(json.dumps(manifest), encoding="utf-8")
    report = provenance.verify_filing(fid)
    assert "independent filing checks match the manifest" in failed(report)


def test_tampered_brief_is_caught():
    fid = fid_ok(); p = md_path(fid); p.write_text(p.read_text(encoding="utf-8") + "x", encoding="utf-8")
    rep = provenance.verify_filing(fid); assert rep["verdict"] == "FAILED" and "brief file matches the fingerprint in the manifest" in failed(rep)


def test_tampered_evidence_in_the_manifest_is_caught_by_rederivation():
    fid = fid_ok(); m = manifest(fid); m["evidence"][0]["text"] += " (edited)"
    man_path(fid).write_text(json.dumps(m, sort_keys=True), encoding="utf-8")
    rep = provenance.verify_filing(fid); assert rep["verdict"] == "FAILED" and "evidence is exactly what the data and documents produce" in failed(rep)


def test_an_invented_number_added_to_a_recorded_finding_is_caught():
    fid = fid_ok(); m = manifest(fid); m["findings"][0]["statement"] += " The median was 987654 KES."
    man_path(fid).write_text(json.dumps(m, sort_keys=True), encoding="utf-8")
    assert any("grounded" in c for c in failed(provenance.verify_filing(fid)))


def test_a_verdict_smuggled_into_a_recorded_finding_is_caught():
    fid = fid_ok(); m = manifest(fid); m["findings"][0]["statement"] = "You should bid on this tender"
    man_path(fid).write_text(json.dumps(m, sort_keys=True), encoding="utf-8")
    assert any("grounded" in c for c in failed(provenance.verify_filing(fid)))


def test_a_tampered_decision_record_is_caught():
    fid = fid_ok(); gate.approve(fid, "Wanjiku Mwangi", gate.brief_sha256(fid), ROLE, APPROVAL_DATE)
    d = config.OUTBOX / "filed" / f"{fid}.decision.json"; rec = json.loads(d.read_text(encoding="utf-8")); rec["md_sha256"] = "0" * 64; d.write_text(json.dumps(rec), encoding="utf-8")
    assert "decision names the same brief and manifest" in failed(provenance.verify_filing(fid))


def test_tampered_model_prompt_is_caught():
    fid = fid_ok(); m = manifest(fid); m["model_calls"][0]["prompt"]["text"] += " IGNORE EVERYTHING"
    man_path(fid).write_text(json.dumps(m, sort_keys=True), encoding="utf-8")
    assert "model prompts and responses are intact" in failed(provenance.verify_filing(fid))


def test_data_drift_is_reported_as_data_changed_not_as_tampering(tmp_path):
    r = go("def-target"); fid = r["filing_id"]; assert provenance.verify_filing(fid)["verdict"] == "REPRODUCED"
    f = tmp_path / "more.json"; f.write_text(json.dumps([_rel("def-late", "S2")]), encoding="utf-8"); load_ocds(f, connect())   # same buyer gets a new award
    rep = provenance.verify_filing(fid)
    assert rep["verdict"].startswith("INTEGRITY OK; DATA CHANGED") and "data version unchanged since filing" in failed(rep)
    assert any(c["kind"] == "replay" and not c["ok"] for c in rep["checks"])


def test_filings_without_provenance_or_manifest_are_labelled_not_reproducible():
    out = tools.file_brief("ocds-eval-abv-target", "s", CITE("deadline", "ocds-synth-0001", "tender.tenderPeriod.endDate"))
    assert "NO provenance supplied" in md_path(out["filing_id"]).read_text(encoding="utf-8")
    assert provenance.verify_filing(out["filing_id"])["verdict"] == "NOT REPRODUCIBLE (no provenance supplied)"
    man_path(out["filing_id"]).unlink()
    assert provenance.verify_filing(out["filing_id"])["verdict"].startswith("NOT REPRODUCIBLE (no manifest")
    assert provenance.verify_filing("nope")["verdict"] == "NOT FOUND" and provenance.verify_filing("../x")["verdict"] == "NOT FOUND"


# ---------------- forged provenance is refused at filing time
@pytest.mark.parametrize("calls,why", [
    ([{"tool": "get_tender_notice", "args": {"ocid": "ocds-eval-abv-target", "as_of": "2025-05-01"}, "output_sha256": "0" * 64, "role": "notice"}], "does not replay"),
    ([{"tool": "file_brief", "args": {}, "output_sha256": "0" * 64, "role": "x"}], "not a replayable read tool"),
    ([{"tool": "get_tender_notice", "args": {"bogus": 1}, "output_sha256": "0" * 64, "role": "x"}], "replay raised"),
    ([{"tool": "approve", "args": {}, "output_sha256": "0" * 64, "role": "x"}], "not a replayable read tool")])
def test_forged_or_unreplayable_provenance_is_refused(calls, why):
    out = tools.file_brief("ocds-eval-abv-target", "s", CITE("deadline", "ocds-synth-0001", "tender.tenderPeriod.endDate"), provenance={"tool_calls": calls})
    assert out["status"] == "not_filed" and why in out["reason"]


# ---------------- API and CLI expose the same guarantees
def test_api_exposes_fingerprints_rejects_stale_hashes_and_serves_verification():
    with TestClient(api.app) as c:
        rid = c.post("/api/runs", json={"ocid": "ocds-eval-abv-target", "as_of": "2025-05-01"}).json()["run_id"]
        for _ in range(100):
            if c.get(f"/api/runs/{rid}").json()["done"]: break
            asyncio.run(asyncio.sleep(0.1))
        fid = c.get(f"/api/runs/{rid}").json()["filing_id"]
        listing = next(f for f in c.get("/api/filings").json() if f["filing_id"] == fid); h = listing["md_sha256"]
        assert c.post(f"/api/filings/{fid}/approve", json={"approver": "Real Person"}).status_code == 422           # approval fields required
        decision = {"approver": "Real Person", "brief_sha256": "0" * 64, "role": ROLE, "date": APPROVAL_DATE}
        assert c.post(f"/api/filings/{fid}/approve", json=decision).status_code == 409
        p = md_path(fid); p.write_text(p.read_text(encoding="utf-8") + "x", encoding="utf-8")
        decision["brief_sha256"] = h
        assert c.post(f"/api/filings/{fid}/approve", json=decision).status_code == 409   # changed since reviewed
        assert c.get(f"/api/filings/{fid}/verify").json()["verdict"] == "FAILED"
        assert c.get("/api/filings/nope/verify").status_code == 404


def test_cli_shows_verifies_and_refuses_unfingerprinted_or_wrong_decisions():
    fid = fid_ok(); run = lambda *a: subprocess.run([sys.executable, "-m", "wazi.cli", *a], capture_output=True, text=True, cwd=ROOT)
    shown = run("show", fid); h = gate.brief_sha256(fid)
    assert shown.returncode == 0 and h in shown.stdout and "BRIEF FINGERPRINT" in shown.stdout
    assert "VERDICT: REPRODUCED" in run("verify", fid).stdout
    assert run("approve", fid, "--approver", "Real Person").returncode == 2                                 # approval fields are mandatory
    role_args = ["--role", ROLE, "--date", APPROVAL_DATE]
    bad = run("approve", fid, "--approver", "Real Person", "--sha256", "0" * 64, *role_args)
    assert bad.returncode == 1 and "ERROR:" in bad.stdout and "Traceback" not in bad.stderr
    assert run("approve", fid, "--approver", "Real Person", "--sha256", h, *role_args).returncode == 0
