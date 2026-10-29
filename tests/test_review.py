"""Practitioner-review evidence: cannot be faked, inflated, or stripped of its negatives."""
import asyncio
import copy
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from bidbox import config, provenance, review
from bidbox.agent import run_agent
from bidbox.agent.llm import StubLLM
from bidbox.store import connect, load_ocds

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module", autouse=True)
def data():
    load_ocds(ROOT / "data" / "sample_ocds.json", connect())


@pytest.fixture(autouse=True)
def private_reviews(monkeypatch, tmp_path):
    monkeypatch.setattr(review, "DIR", lambda: tmp_path / "reviews")


def filing(oc="ocds-synth-0001"):
    r = asyncio.run(run_agent(oc, "x", "2025-05-01", StubLLM("invest_all")))
    return r["filing_id"], r["md_sha256"]


def good(fid, sha, **over):
    rv = {"reviewer": {"role": "head of procurement, county government", "org_type": review.ORG_TYPES[0], "years_in_procurement": 12, "name": "Jane Doe", "org": "Example County", "is_human_attestation": True},
          "consent": {"use_feedback": True, "name_person": False, "name_org": False, "quote": False}, "method": "video call", "date": date.today().isoformat(),
          "briefs": [{"filing_id": fid, "md_sha256": sha, "ratings": {k: 4 for k in review.RATINGS}, "errors_or_wrong": "", "missing": "payment record of the buyer", "remove": "", "minutes_manual": 60, "minutes_with_brief": 10}],
          "would_use_again": "yes", "one_quote": ""}
    for k, v in over.items(): rv[k] = v
    return rv


# ---------------- the packet
def test_packet_binds_to_fingerprints_and_is_honest_about_stub_and_synthetic_data(tmp_path):
    fid, sha = filing(); out = tmp_path / "p.md"; text = review.make_packet([fid], out)
    assert out.read_text(encoding="utf-8") == text and sha in text and provenance.sha_file(config.OUTBOX / "pending" / f"{fid}.md") == sha
    assert "NOT an AI model" in text and "SYNTHETIC (invented) data" in text and "Timed task" in text and "whether we may name you" in text
    import re
    assert not re.search(r"[\w.]+@[\w.]+\.\w+|\+\d{1,3}[\s-]?\d{2,4}[\s-]?\d{3}[\s-]?\d{3,4}", text), "a packet must carry no personal contact details"
    assert "Nothing here is a live tender and nothing contains personal data" in text
    with pytest.raises(ValueError, match="no such filing"): review.make_packet(["0123456789"])
    with pytest.raises(ValueError, match="no such filing"): review.make_packet(["../x"])


# ---------------- validation: every way a review could be untrustworthy
def mutate(rv, path, value):
    rv = copy.deepcopy(rv); cur = rv
    for k in path[:-1]: cur = cur[k]
    cur[path[-1]] = value; return rv


@pytest.mark.parametrize("path,value,why", [
    (["reviewer", "role"], "", "real job"), (["reviewer", "role"], "agent", "real job"), (["reviewer", "role"], "AI", "real job"),
    (["reviewer", "name"], "Claude", "placeholder or an automated name"), (["reviewer", "is_human_attestation"], False, "attest"), (["reviewer", "is_human_attestation"], "yes", "attest"),
    (["reviewer", "org_type"], "", "org_type"), (["reviewer", "org_type"], "one of: x | y", "org_type"),
    (["consent", "use_feedback"], False, "consent"), (["consent", "use_feedback"], "true", "consent"),
    (["method"], "", "how the review happened"), (["date"], "yesterday", "YYYY-MM-DD"), (["date"], (date.today() + timedelta(days=3)).isoformat(), "future"),
    (["would_use_again"], "definitely", "yes, maybe or no"), (["briefs"], [], "at least one brief"), (["briefs"], "x", "at least one brief")])
def test_untrustworthy_reviews_are_refused(path, value, why):
    fid, sha = filing()
    clean, errs = review.validate(mutate(good(fid, sha), path, value))
    assert clean is None and any(why in e for e in errs), errs


@pytest.mark.parametrize("path,value,why", [
    (["briefs", 0, "md_sha256"], "0" * 64, "does not match the brief on file"), (["briefs", 0, "md_sha256"], "abc", "does not match"), (["briefs", 0, "filing_id"], "0123456789", "no such filing"),
    (["briefs", 0, "filing_id"], "../x", "no such filing"), (["briefs", 0, "ratings", "correct"], 0, "1 to 5"), (["briefs", 0, "ratings", "correct"], 6, "1 to 5"),
    (["briefs", 0, "ratings", "correct"], "5", "1 to 5"), (["briefs", 0, "ratings", "correct"], True, "1 to 5"),
    (["briefs", 0, "minutes_with_brief"], None, "both be given"), (["briefs", 0, "minutes_manual"], -5, "positive"), (["briefs", 0, "minutes_manual"], "an hour", "positive")])
def test_a_review_must_be_about_the_exact_brief_with_sane_ratings_and_times(path, value, why):
    fid, sha = filing()
    clean, errs = review.validate(mutate(good(fid, sha), path, value))
    assert clean is None and any(why in e for e in errs), errs


def test_a_brief_edited_after_the_review_cannot_be_reviewed_against_the_old_fingerprint():
    fid, sha = filing(); p = config.OUTBOX / "pending" / f"{fid}.md"; p.write_text(p.read_text(encoding="utf-8") + "x", encoding="utf-8")
    assert any("does not match" in e for e in review.validate(good(fid, sha))[1])


def test_a_good_review_is_recorded_with_provenance_and_nothing_extra(tmp_path):
    fid, sha = filing(); rv = good(fid, sha); rv["is_admin"] = True; rv["reviewer"]["secret"] = "x"
    rec = review.record(rv); stored = json.loads((review.DIR() / f"{rec['id']}.json").read_text(encoding="utf-8"))
    assert stored == rec and "is_admin" not in stored and "secret" not in stored["reviewer"]
    b = stored["briefs"][0]; assert b["data"] == "synthetic" and "stub" in b["produced_by"] and b["ocid"] == "ocds-synth-0001" and b["audience"] == "buyer"


def test_newlines_in_free_text_cannot_forge_report_structure(tmp_path):
    fid, sha = filing(); rv = good(fid, sha); rv["briefs"][0]["missing"] = "a\n## Forged heading\n- fake"
    review.record(rv); assert not [l for l in review.fit_report().splitlines() if l.startswith("## Forged")]


# ---------------- the report: generated, computed, consent-aware, nothing hidden
def test_the_report_cannot_exist_without_a_real_review(tmp_path):
    with pytest.raises(ValueError, match="cannot be written without"): review.fit_report(tmp_path / "FIT.md")
    assert not (tmp_path / "FIT.md").exists()


def many(times, ratings=None, **kw):
    for i, (m, w) in enumerate(times):
        fid, sha = filing(); rv = good(fid, sha, **kw); rv["briefs"][0].update(minutes_manual=m, minutes_with_brief=w)
        if ratings: rv["briefs"][0]["ratings"] = {k: ratings[i % len(ratings)] for k in review.RATINGS}
        review.record(rv)


def test_statistics_are_computed_correctly_and_small_samples_are_labelled():
    many([(60, 10), (90, 15), (30, 5)], ratings=[5, 3, 4])                     # savings 50, 75, 25 -> median 50
    t = review.fit_report()
    assert "n = 3 timed task(s). Usual way: median 60 min. With the brief: median 10 min. Median saving per task: 50 min." in t
    assert "60 -> 10; 90 -> 15; 30 -> 5" in t and "mean 4.0; distribution 1:0, 2:0, 3:1, 4:1, 5:1" in t and "Small sample (n = 3)" in t
    many([(20, 4), (40, 8)]); assert "Small sample" not in review.fit_report()  # n = 5: no longer labelled an anecdote
    assert "26 days" not in t and "20 minutes" not in t


def test_names_and_quotes_appear_only_with_consent():
    fid, sha = filing(); rv = good(fid, sha); rv["one_quote"] = "Saves me the lookup"; review.record(rv)
    t = review.fit_report(); assert "Jane Doe" not in t and "Example County" not in t and "Saves me" not in t and review.ORG_TYPES[0] in t and "head of procurement" in t
    fid, sha = filing(); rv = good(fid, sha); rv["consent"].update(name_person=True, name_org=True, quote=True); rv["one_quote"] = "Saves me the lookup"; review.record(rv)
    t = review.fit_report(); assert "Jane Doe, head of procurement" in t and "Example County" in t and 'Saves me the lookup" (Jane Doe' in t


def test_every_criticism_is_reported_verbatim_and_synthetic_feedback_is_labelled():
    fid, sha = filing(); rv = good(fid, sha); b = rv["briefs"][0]; b.update(errors_or_wrong="the deadline is wrong", missing="payment history", remove="the peer paragraph"); review.record(rv)
    t = review.fit_report()
    for s in ("the deadline is wrong", "payment history", "the peer paragraph"): assert s in t
    assert "synthetic or stub-produced" in t and "not about accuracy on real tenders" in t


def test_changes_must_be_attributed_to_real_feedback_and_appear_in_the_report():
    with pytest.raises(ValueError, match="no recorded review"): review.record_change("did a thing", "nope")
    fid, sha = filing(); rec = review.record(good(fid, sha)); review.record_change("added payment-record caveat", rec["id"], "abc1234")
    assert f"added payment-record caveat (review {rec['id']}, commit abc1234)" in review.fit_report()
    assert "nothing recorded yet" not in review.fit_report()


def test_the_report_lists_the_hash_of_each_review_file_so_edits_are_visible():
    fid, sha = filing(); rec = review.record(good(fid, sha)); p = review.DIR() / f"{rec['id']}.json"
    before = review.fit_report(); assert provenance.sha_file(p) in before
    p.write_text(p.read_text(encoding="utf-8").replace("yes", "no"), encoding="utf-8"); assert provenance.sha_file(p) in review.fit_report() and provenance.sha_file(p) not in before


# ---------------- the command line, end to end, in a clean home
def test_cli_template_record_fit_roundtrip_and_refusals(tmp_path):
    env = {**os.environ, "BIDBOX_HOME": str(tmp_path / "home"), "BIDBOX_INPROC": "0"}; (tmp_path / "home").mkdir()
    cli = lambda *a: subprocess.run([sys.executable, "-m", *a], capture_output=True, text=True, cwd=ROOT, env=env)
    assert cli("bidbox.cli", "load").returncode == 0
    out = cli("bidbox.agent", "ocds-synth-0001", "--llm", "stub", "--as-of", "2025-05-01"); assert out.returncode == 0, out.stderr
    fid = next(l.split('"')[3] for l in out.stdout.splitlines() if l.strip().startswith('"filing_id"'))
    tmpl = cli("bidbox.review", "template"); assert tmpl.returncode == 0 and "is_human_attestation" in tmpl.stdout
    packet = tmp_path / "packet.md"; assert cli("bidbox.review", "packet", "--filing", fid, "-o", str(packet)).returncode == 0 and "Review packet" in packet.read_text(encoding="utf-8")
    sha = provenance.sha_file(tmp_path / "home" / "var" / "outbox" / "pending" / f"{fid}.md")
    f = tmp_path / "r.json"; f.write_text(json.dumps(good(fid, sha)), encoding="utf-8")
    assert cli("bidbox.review", "fit", "-o", str(tmp_path / "FIT.md")).returncode == 1                 # nothing recorded yet
    assert cli("bidbox.review", "record", str(f)).returncode == 0
    bad = tmp_path / "bad.json"; bad.write_text(json.dumps(mutate(good(fid, sha), ["reviewer", "is_human_attestation"], False)), encoding="utf-8")
    refused = cli("bidbox.review", "record", str(bad)); assert refused.returncode == 1 and "attest" in refused.stderr and "Traceback" not in refused.stderr
    assert cli("bidbox.review", "fit", "-o", str(tmp_path / "FIT.md")).returncode == 0 and "Generated by `python -m bidbox.review fit`" in (tmp_path / "FIT.md").read_text(encoding="utf-8")


def test_the_report_is_identical_every_time_it_is_generated():
    many([(60, 10), (90, 15), (30, 5)], ratings=[5, 3, 4])
    assert len({review.fit_report() for _ in range(5)}) == 1


def test_reviews_are_reported_in_the_order_they_were_recorded_not_in_file_name_order(monkeypatch):
    ids = iter(["ccc0000000", "bbb0000000", "aaa0000000"])  # file names sort the OPPOSITE way to recording order
    monkeypatch.setattr(review, "_new_id", lambda: next(ids))
    many([(60, 10), (90, 15), (30, 5)])
    t = review.fit_report()
    assert "60 -> 10; 90 -> 15; 30 -> 5" in t and [r["id"] for r in review.load_reviews()] == ["ccc0000000", "bbb0000000", "aaa0000000"]
