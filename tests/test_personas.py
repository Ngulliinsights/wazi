"""Persona acceptance tests. Procurement manager (buyer side, signs off) is the primary persona;
the supplier's supply chain manager is the second lens. Each test is a thing that person would challenge."""
import asyncio
import json
import re
import tempfile
from pathlib import Path

import pytest

from wazi import config, gate, tools
from wazi.agent import run_agent
from wazi.agent.llm import StubLLM
from wazi.evals import build_fixture_releases
from wazi.store import connect, load_ocds

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_ocds.json"


@pytest.fixture(scope="module", autouse=True)
def data():
    f = Path(tempfile.mkdtemp()) / "fx.json"; f.write_text(json.dumps({"releases": build_fixture_releases()}))
    c = connect(); load_ocds(SAMPLE, c); load_ocds(f, c)


def go(ocid, audience="buyer", as_of="2025-05-01"):
    r = asyncio.run(run_agent(ocid, "Prepare a tender readiness brief", as_of, StubLLM("invest_all"), audience=audience))
    return r, (config.OUTBOX / "pending" / f"{r['filing_id']}.md").read_text(encoding="utf-8")


# ---------------- procurement manager
def test_pm_missing_notice_fields_are_framed_as_fixes_in_my_own_notice():
    _, md = go("ocds-synth-0031")
    v = md.split("## Before you sign off: what to fix or confirm")[1]
    assert "Add the closing date to the published notice" in v
    assert "with the buyer" not in v                      # I AM the buyer: do not tell me to ask myself


def test_pm_sees_my_estimate_against_the_market_and_my_own_exposure():
    _, md = go("ocds-eval-abv-target")
    assert "## Your estimate against past awards" in md and "times the median" in md
    assert "## Your award history and exposure (read with care)" in md and "above the range of peers" in md


def test_pm_is_not_shown_a_signal_that_typical_peers_explain_away():
    r, md = go("ocds-eval-typ-target")
    assert "Signal (worth a look, not proof)" not in md and "within the range of peers" in md


def test_pm_gets_an_accountability_trail_that_names_a_human():
    r, md = go("ocds-eval-abv-target")
    assert r["filing_id"] in md
    rec = gate.approve(r["filing_id"], "Wanjiku Mwangi")
    dec = json.loads((config.OUTBOX / "filed" / f"{r['filing_id']}.decision.json").read_text(encoding="utf-8"))
    assert dec["approver"] == "Wanjiku Mwangi" and dec["decided_at"] and dec["action"] == "approved"
    log = [json.loads(l) for l in config.LOG_PATH.read_text(encoding="utf-8").splitlines()]
    mine = [l for l in log if l.get("run_id") == r["run_id"]]
    assert {"get_tender_notice", "buyer_award_history", "price_benchmark", "peer_baseline", "file_brief"} <= {l.get("tool") for l in mine}
    assert any(l.get("tool") == "human_approval" and l.get("filing_id") == r["filing_id"] and l["approver"] == "Wanjiku Mwangi" for l in log)


def test_pm_can_reproduce_a_brief_for_an_auditor():
    (a, _), (b, _) = go("ocds-eval-abv-target"), go("ocds-eval-abv-target")
    assert [f["statement"] for f in a["findings"]] == [f["statement"] for f in b["findings"]]
    assert [f["severity"] for f in a["findings"]] == [f["severity"] for f in b["findings"]]


@pytest.mark.parametrize("claim", ["This tender is compliant with the regulations", "The process appears irregular", "The buyer seems biased",
                                   "This award was unlawful", "The procurement is fully legal"])
def test_pm_is_never_told_by_a_machine_that_a_process_is_legal_or_illegal(claim):
    out = tools.file_brief("ocds-eval-abv-target", "s", [{"kind": "other", "statement": claim, "source_ocid": "ocds-eval-abv-target", "field_path": "tender.title"}])
    assert out["status"] == "not_filed", claim


@pytest.mark.parametrize("fact", ["Bidders must provide a tax compliance certificate", "The notice requires proof of legal registration",
                                  "Eligibility: a fair price schedule must accompany the bid"])
def test_pm_legitimate_requirements_are_not_mistaken_for_verdicts(fact):
    out = tools.file_brief("ocds-eval-abv-target", "s", [{"kind": "eligibility", "statement": fact, "source_ocid": "ocds-eval-abv-target", "field_path": "tender.eligibilityCriteria"}])
    assert out["status"] == "pending_approval", fact


def test_pm_brief_states_what_it_cannot_check():
    _, md = go("ocds-eval-abv-target")
    tail = md.lower()
    for need in ("legal and regulatory compliance", "conflicts of interest", "evaluation criteria", "contract performance"):
        assert need in tail, need


def test_both_audiences_stay_free_of_personal_data_and_verdicts():
    pii = re.compile(r"[\w.]+@[\w.]+\.\w+|\+\d{1,3}[\s-]?\d{2,4}[\s-]?\d{3}[\s-]?\d{3,4}")
    for aud in ("buyer", "supplier"):
        for oc in ("ocds-synth-0001", "ocds-eval-abv-target", "ocds-eval-doc-ok"):
            _, md = go(oc, aud)
            body = "\n".join(l for l in md.splitlines() if not l.startswith("> "))
            assert not pii.search(body) and not any(w in body.lower() for w in ("you should bid", "we recommend", "best bidder", "should be awarded"))


# ---------------- supplier's supply chain manager (second lens)
def test_supplier_view_still_asks_questions_of_the_buyer():
    _, md = go("ocds-synth-0031", "supplier")
    assert "Confirm the submission deadline with the buyer" in md and "## Before you commit: what to verify" in md


def test_unknown_audience_is_refused_not_guessed():
    out = tools.file_brief("ocds-eval-abv-target", "s", [{"kind": "other", "statement": "A plain fact here", "source_ocid": "ocds-eval-abv-target", "field_path": "tender.title"}], audience="auditor")
    assert out["status"] == "not_filed" and "unknown audience" in out["reason"]
