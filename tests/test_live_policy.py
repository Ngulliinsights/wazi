"""Data conduct: never a live tender in progress. Real open tenders are refused (agent AND server); synthetic data and closed tenders are fine."""
import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from wazi import api, config, policy, tools
from wazi.agent import run_agent
from wazi.agent.llm import StubLLM
from wazi.store import connect, load_ocds

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
FUT, PAST = "2099-12-31T00:00:00Z", "2025-06-01T12:00:00Z"


def rel(oc, status=None, end=FUT, start="2025-05-02T12:00:00Z"):
    t = {"title": oc, "mainProcurementCategory": "goods", "procurementMethod": "open", "tenderPeriod": {"startDate": start, "endDate": end} if end else {},
         "value": {"amount": 1000, "currency": "KES"}}
    if status: t["status"] = status
    return {"ocid": oc, "buyer": {"id": "EV-LP", "name": "Policy Buyer"}, "tender": t}


@pytest.fixture(scope="module", autouse=True)
def data(tmp_path_factory):
    d = tmp_path_factory.mktemp("lp")
    real = [rel("lp-live", "active"), rel("lp-live-nostatus", None), rel("lp-closed-real", "complete", PAST), rel("lp-cancelled-future", "cancelled", FUT),
            rel("lp-past-nostatus", None, PAST), rel("lp-hist", "complete", "2025-06-01T12:00:00Z")]
    (d / "real.json").write_text(json.dumps(real), encoding="utf-8")
    (d / "synth.json").write_text(json.dumps({"publisher": {"name": "SYNTHETIC test"}, "releases": [rel("lp-synth-live", "active")]}), encoding="utf-8")
    c = connect(); load_ocds(d / "real.json", c); load_ocds(d / "synth.json", c)


def go(oc, as_of="2025-05-01"):
    return asyncio.run(run_agent(oc, "Prepare a tender readiness brief", as_of, StubLLM("invest_all")))


@pytest.mark.parametrize("status,deadline,synthetic,live", [
    ("active", FUT, 1, False), ("active", FUT, 0, True), (None, FUT, 0, True), ("complete", FUT, 0, False), ("cancelled", FUT, 0, False),
    ("unsuccessful", FUT, 0, False), ("withdrawn", FUT, 0, False), ("active", PAST, 0, False), (None, PAST, 0, False),
    ("active", None, 0, True), ("planning", None, 0, True), ("planned", None, 0, True), (None, None, 0, False), ("weird", None, 0, False),
    ("active", "garbage", 0, True), (None, "garbage", 0, False), (" ACTIVE ", None, 0, True), (None, "2099-12-31", 0, True)])
def test_the_live_tender_matrix(status, deadline, synthetic, live):
    assert bool(policy.live_reason(status, deadline, synthetic, NOW)) is live


def test_the_clock_is_the_wall_clock_and_can_be_pinned_for_tests(monkeypatch):
    monkeypatch.setenv("WAZI_TODAY", "2025-01-01")
    assert policy.live_reason(None, PAST, 0) is not None                 # the same tender is live on 2025-01-01 ...
    monkeypatch.delenv("WAZI_TODAY")
    assert policy.live_reason(None, PAST, 0) is None                     # ... and closed today


def test_the_agent_refuses_a_live_tender_before_doing_any_work():
    r = go("lp-live")
    assert r["status"] == "refused_live" and r["filing_id"] is None and "still live" in r["error"] and "deadline" in r["error"]
    assert [e["tool"] for e in r["events"] if e["event"] == "tool_call"] == ["get_tender_notice"]            # nothing else was read
    assert any(e["event"] == "refused_live" for e in r["events"])


@pytest.mark.parametrize("oc", ["lp-live-nostatus"])
def test_a_tender_with_a_future_deadline_and_no_status_is_still_live(oc):
    assert go(oc)["status"] == "refused_live"


@pytest.mark.parametrize("oc", ["lp-closed-real", "lp-cancelled-future", "lp-past-nostatus", "lp-synth-live"])
def test_closed_cancelled_past_and_synthetic_tenders_are_fine(oc):
    assert go(oc)["status"] == "awaiting_human_approval"


def test_replaying_a_historical_tender_as_of_an_earlier_date_is_the_intended_use():
    r = go("lp-hist", as_of="2025-05-01")                                  # open on the replay date, closed today
    assert r["status"] == "awaiting_human_approval"
    md = (config.OUTBOX / "pending" / f"{r['filing_id']}.md").read_text(encoding="utf-8")
    assert "open: 31 day(s) left as of 2025-05-01" in md


def test_the_server_refuses_to_file_a_live_tender_even_if_the_agent_is_bypassed():
    out = tools.file_brief("lp-live", "s", [{"kind": "deadline", "statement": "A plain fact here", "source_ocid": "lp-live", "field_path": "tender.tenderPeriod.endDate"}])
    assert out["status"] == "not_filed" and "live tender" in out["reason"]


def test_the_development_override_works_and_is_recorded_everywhere(monkeypatch):
    monkeypatch.setenv("WAZI_ALLOW_LIVE", "1")
    r = go("lp-live"); assert r["status"] == "awaiting_human_approval" and any(e["event"] == "live_override" for e in r["events"])
    md = (config.OUTBOX / "pending" / f"{r['filing_id']}.md").read_text(encoding="utf-8")
    assert "LIVE TENDER OVERRIDE" in md.splitlines()[-1]
    man = json.loads((config.OUTBOX / "pending" / f"{r['filing_id']}.manifest.json").read_text(encoding="utf-8"))
    assert man["run"]["live_override"] is True


def test_the_api_flags_live_tenders_and_reports_the_refusal_with_its_reason():
    with TestClient(api.app) as c:
        flags = {t["ocid"]: t["live"] for t in c.get("/api/tenders", params={"q": "lp-"}).json()}
        assert flags["lp-live"] is True and flags["lp-closed-real"] is False and flags["lp-synth-live"] is False and flags["lp-cancelled-future"] is False
        rid = c.post("/api/runs", json={"ocid": "lp-live", "as_of": "2025-05-01"}).json()["run_id"]
        for _ in range(100):
            r = c.get(f"/api/runs/{rid}").json()
            if r["done"]: break
            time.sleep(0.1)
    assert r["status"] == "refused_live" and "still live" in r["error"]


def test_databases_from_before_the_policy_are_migrated(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"; c = sqlite3.connect(db)
    c.execute("CREATE TABLE tenders(ocid TEXT PRIMARY KEY, title TEXT, description TEXT, status TEXT, category TEXT, method TEXT, deadline TEXT, value REAL, currency TEXT, eligibility TEXT, buyer_id TEXT, buyer_name TEXT, doc_urls TEXT)")
    c.execute("INSERT INTO tenders(ocid,title) VALUES('old1','kept')"); c.commit(); c.close()
    assert connect(db).execute("SELECT title, synthetic, start FROM tenders").fetchone()[:] == ("kept", 0, None)


def test_the_cli_can_mark_data_synthetic_or_force_it_real(tmp_path):
    f = tmp_path / "p.json"; f.write_text(json.dumps({"publisher": {"name": "SYNTHETIC x"}, "releases": [rel("cli-1", "active")]}), encoding="utf-8")
    env = {**__import__("os").environ, "WAZI_HOME": str(tmp_path / "home")}
    def synth(flag):
        subprocess.run([sys.executable, "-m", "wazi.cli", "load", str(f), *flag], capture_output=True, text=True, cwd=ROOT, env=env, check=True)
        return connect(tmp_path / "home" / "var" / "wazi.db").execute("SELECT synthetic FROM tenders WHERE ocid='cli-1'").fetchone()[0]
    assert synth([]) == 1 and synth(["--real"]) == 0 and synth(["--synthetic"]) == 1
