"""The filed brief is the one document a supply chain manager reads: structure, honesty about gaps, no verdicts."""
import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from bidbox import config, tools
from bidbox.agent import run_agent
from bidbox.agent.llm import StubLLM
from bidbox.evals import build_fixture_releases
from bidbox.store import connect, load_ocds

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_ocds.json"


@pytest.fixture(scope="module", autouse=True)
def data():
    f = Path(tempfile.mkdtemp()) / "fx.json"; f.write_text(json.dumps({"releases": build_fixture_releases()}))
    c = connect(); load_ocds(SAMPLE, c); load_ocds(f, c)


def brief(ocid, as_of="2025-05-01", audience="supplier"):
    r = asyncio.run(run_agent(ocid, "Prepare a tender readiness brief", as_of, StubLLM("invest_all"), audience=audience))
    return (config.OUTBOX / "pending" / f"{r['filing_id']}.md").read_text(encoding="utf-8")


def test_brief_has_the_sections_a_supplier_needs_in_order():
    md = brief("ocds-eval-abv-target")
    heads = [l for l in md.splitlines() if l.startswith("## ")]
    assert heads[0] == "## At a glance" and heads[-1] == "## Before you commit: what to verify"
    assert "## Buyer behaviour (read with care)" in heads and heads.index("## Summary") == 1
    assert "What this brief does not cover" in md and "payment terms" in md


def test_glance_shows_buyer_value_and_days_left_from_the_record():
    md = brief("ocds-eval-abv-target", as_of="2025-05-01")
    assert "| Buyer | Buyer EV-E0 |" in md and "| Estimated value | 1,000,000 KES |" in md
    assert "open: 31 day(s) left as of 2025-05-01" in md          # deadline 2025-06-01


def test_closed_tender_is_shown_as_closed_not_as_a_live_opportunity():
    md = brief("ocds-eval-abv-target", as_of="2099-01-01")
    assert "closed" in md.split("## Summary")[0] and "open:" not in md.split("## Summary")[0]


def test_missing_fields_become_questions_to_ask_not_silence():
    md = brief("ocds-synth-0031")                                     # deadline blanked in the sample
    verify = md.split("## Before you commit: what to verify")[1]
    assert "Confirm the submission deadline" in verify and "| Deadline | not recorded |" in md
    md2 = brief("ocds-eval-noelig")
    assert "eligibility and mandatory-document list" in md2.split("## Before you commit")[1]


def test_nameless_buyer_is_labelled_not_invented():
    c = connect()
    ocid = c.execute("SELECT ocid FROM tenders WHERE buyer_id='KE-B-004' AND (buyer_name IS NULL OR buyer_name='') LIMIT 1").fetchone()[0]
    assert "(name not recorded)" in brief(ocid)


def test_signals_are_labelled_as_prompts_for_review_and_listed_first():
    md = brief("ocds-eval-abv-target")
    assert "Signal (worth a look, not proof)" in md
    r = tools.file_brief("ocds-eval-abv-target", "s", [
        {"kind": "buyer_history", "statement": "A calm note here", "source_ocid": "ocds-eval-abv-target", "field_path": "tender.title", "severity": "info"},
        {"kind": "buyer_history", "statement": "A loud signal here", "source_ocid": "ocds-eval-abv-target", "field_path": "tender.title", "severity": "signal"}])
    out = (config.OUTBOX / "pending" / f"{r['filing_id']}.md").read_text(encoding="utf-8")
    assert out.index("A loud signal here") < out.index("A calm note here")


def test_brief_never_gives_a_verdict_and_every_finding_carries_its_source():
    for ocid in ("ocds-eval-abv-target", "ocds-eval-typ-target", "ocds-synth-0001"):
        md = "\n".join(l for l in brief(ocid).splitlines() if not l.startswith("> "))   # the disclaimer itself says 'recommend'
        assert not any(w in md.lower() for w in ("you should bid", "recommend", "we advise", "should be awarded", "best bidder"))
        findings = [l for l in md.splitlines() if l.startswith("- **")]
        assert findings and all("_Source:" in md.split(l)[1].split("\n")[1] for l in findings)


@pytest.mark.parametrize("verdict", ["You should bid on this tender", "We recommend this supplier apply", "This is the best bidder pool",
                                     "Don't bid, the buyer deserves a rival", "It is worth bidding for this one"])
def test_verdict_language_never_reaches_the_brief_even_with_a_valid_source(verdict):
    class Pushy(StubLLM):
        def complete_json(self, task, payload):
            out = super().complete_json(task, payload)
            if task == "synthesize":
                out["findings"].insert(0, {"kind": "other", "statement": verdict, "evidence": [out["findings"][0]["evidence"][0]], "severity": "info"})
            return out
    r = asyncio.run(run_agent("ocds-eval-abv-target", "Prepare a tender readiness brief", "2025-05-01", Pushy("invest_all")))
    md = (config.OUTBOX / "pending" / f"{r['filing_id']}.md").read_text(encoding="utf-8")
    assert verdict not in md and not any(verdict == f["statement"] for f in r["findings"])
    # and the server enforces it too, independent of the agent's own check
    out = tools.file_brief("ocds-eval-abv-target", "s", [{"kind": "other", "statement": verdict, "source_ocid": "ocds-eval-abv-target", "field_path": "tender.title"}])
    assert out["status"] == "not_filed"


def test_facts_that_merely_contain_similar_words_are_not_blocked():
    ok = {"kind": "other", "statement": "Bidders must submit a bid security of 50000 KES", "source_ocid": "ocds-eval-doc-ok", "field_path": "tender.documents[].url"}
    out = tools.file_brief("ocds-eval-abv-target", "s", [ok])
    assert out["status"] == "pending_approval"


@pytest.mark.parametrize("fact", ["Bidders must submit a bid security of 50000 KES", "A site visit is recommended before submitting bids",
                                  "Applicants must apply through the portal before the deadline"])
def test_requirements_quoted_from_the_tender_are_facts_not_advice(fact):
    out = tools.file_brief("ocds-eval-abv-target", "s", [{"kind": "other", "statement": fact, "source_ocid": "ocds-eval-doc-ok", "field_path": "tender.documents[].url"}])
    assert out["status"] == "pending_approval", fact
