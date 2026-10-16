"""API + page: a person can run the agent, watch it, and approve; the agent cannot approve itself."""
import json
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bidbox import config
from bidbox.api import app
from bidbox.evals import build_fixture_releases
from bidbox.store import connect, load_ocds

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_ocds.json"


@pytest.fixture(scope="module")
def c():
    f = Path(tempfile.mkdtemp()) / "fx.json"; f.write_text(json.dumps({"releases": build_fixture_releases()}))
    cn = connect(); load_ocds(SAMPLE, cn); load_ocds(f, cn)
    with TestClient(app) as client:
        yield client


def finish(c, rid, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(f"/api/runs/{rid}").json()
        if r["done"]:
            return r
        time.sleep(0.5)
    raise AssertionError("run did not finish")


def start(c, **kw):
    r = c.post("/api/runs", json={"ocid": "ocds-eval-abv-target", "as_of": "2025-05-01", **kw}); assert r.status_code == 200, r.text
    return finish(c, r.json()["run_id"])


def sha_of(c, fid):
    """What a real client does: take the fingerprint of the brief it is deciding on from the listing."""
    return next(f["md_sha256"] for f in c.get("/api/filings").json() if f["filing_id"] == fid)


def test_page_carries_the_safety_banners_and_no_award_button(c):
    html = c.get("/").text
    assert "DRAFT: not an award decision" in html and "Closed tenders and synthetic data only" in html
    assert "never awards" in html and "Approve filing" in html and "Reject" in html
    assert "Award" not in html.replace("never awards", "").replace("not an award decision", "")   # no award control anywhere on the page


def test_config_and_tender_search(c):
    assert c.get("/api/config").json()["llm"] == "stub"
    hits = c.get("/api/tenders", params={"q": "eval-abv"}).json()
    assert hits and all("abv" in h["ocid"] for h in hits)


def test_run_streams_agent_events_and_queues_a_brief(c):
    r = start(c)
    assert r["status"] == "awaiting_human_approval" and r["filing_id"]
    ev = c.get(f"/api/runs/{r['run_id']}/events").json()
    names = [e["event"] for e in ev["events"]]
    assert names[0] == "intake" and "filed" in names and "investigate_step" in names
    assert c.get(f"/api/runs/{r['run_id']}/events", params={"after": ev["next"]}).json()["events"] == []   # polling cursor works
    pend = {f["filing_id"]: f for f in c.get("/api/filings").json()}
    assert "At a glance" in pend[r["filing_id"]]["markdown"]


def test_gate_over_http(c):
    r = start(c)
    fid = r["filing_id"]; h = sha_of(c, fid)
    for who in ("agent", "Claude", "", "x"):
        resp = c.post(f"/api/filings/{fid}/approve", json={"approver": who, "brief_sha256": h})
        assert resp.status_code == 403, who
    assert fid in [f["filing_id"] for f in c.get("/api/filings").json()]          # still pending
    ok = c.post(f"/api/filings/{fid}/approve", json={"approver": "Wanjiku Mwangi", "brief_sha256": h})
    assert ok.status_code == 200 and ok.json()["approver"] == "Wanjiku Mwangi"
    assert fid not in [f["filing_id"] for f in c.get("/api/filings").json()]
    assert c.post(f"/api/filings/{fid}/approve", json={"approver": "Wanjiku Mwangi", "brief_sha256": h}).status_code == 404   # cannot decide twice


def test_reject_records_a_reason(c):
    fid = start(c)["filing_id"]
    resp = c.post(f"/api/filings/{fid}/reject", json={"approver": "Otieno Achieng", "reason": "notice incomplete", "brief_sha256": sha_of(c, fid)})
    assert resp.status_code == 200 and resp.json()["reason"] == "notice incomplete"
    assert (config.OUTBOX / "rejected" / f"{fid}.md").exists()


def test_bad_requests_are_handled_not_crashed(c):
    assert c.post("/api/runs", json={"ocid": "x", "mode": "turbo"}).status_code == 422
    assert c.get("/api/runs/nope").status_code == 404 and c.get("/api/runs/nope/events").status_code == 404
    assert c.post("/api/filings/nope/approve", json={"approver": "Real Person", "brief_sha256": "a" * 12}).status_code == 404


def test_award_request_over_http_is_refused_and_files_nothing(c):
    r = start(c, request="Award this tender to Demo Supplier 1 Ltd")
    assert r["status"] == "refused" and not r.get("filing_id")


def test_unknown_tender_ends_cleanly(c):
    resp = c.post("/api/runs", json={"ocid": "ocds-nope", "as_of": "2025-05-01"})
    assert finish(c, resp.json()["run_id"])["status"] == "not_found"
