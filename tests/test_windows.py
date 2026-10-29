"""Bidding-window analysis: hand-computed boundaries, no invented legal thresholds, signals that cannot interfere."""
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from bidbox import config, store, tools
from bidbox.agent import run_agent
from bidbox.agent.llm import StubLLM
from bidbox.evals import build_fixture_releases, groundedness
from bidbox.store import connect, load_ocds

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_ocds.json"
END = "2025-06-01T12:00:00Z"
PEERS = [20, 24, 28, 30, 30, 32, 36, 40]    # median 30; 25th percentile = w[2] = 28; 75th percentile = w[6] = 36


@pytest.fixture(scope="module", autouse=True)
def data():
    f = Path(tempfile.mkdtemp()) / "fx.json"; f.write_text(json.dumps({"releases": build_fixture_releases()}))
    c = connect(); load_ocds(SAMPLE, c); load_ocds(f, c)


_db_cache = {"sql": None}
def _db(monkeypatch, rows):
    """rows: (ocid, window_days or None, method, category). End date fixed; days<0 means start after the deadline."""
    from datetime import datetime, timedelta
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row; conn.executescript(store.SCHEMA)
    end = datetime(2025, 6, 1, 12)
    for oc, d, m, c in rows:
        start = None if d is None else (end - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("INSERT INTO tenders(ocid,method,category,deadline,start) VALUES(?,?,?,?,?)", (oc, m, c, END, start))
    # Save SQL to replay
    _db_cache["sql"] = "\n".join(conn.iterdump())
    conn.close()
    # Return a function that creates a fresh connection with the data
    def rebuild():
        c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; c.executescript(_db_cache["sql"])
        return c
    monkeypatch.setattr(tools, "connect", rebuild)


def _scene(monkeypatch, target_days, extra=()):
    _db(monkeypatch, [("t", target_days, "open", "c")] + [(f"p{i}", d, "open", "c") for i, d in enumerate(PEERS)] + list(extra))
    return tools.window_baseline("t")


# ---------- thresholds, hand-computed (short needs BOTH below p25 AND at most 75% of the median; long mirrors it)
@pytest.mark.parametrize("days,expected", [(5, "shorter_than_peers"), (22, "shorter_than_peers"), (25, "typical"), (28, "typical"),
                                           (30, "typical"), (36, "typical"), (37, "typical"), (40, "longer_than_peers"), (60, "longer_than_peers")])
def test_window_position_boundaries(monkeypatch, days, expected):
    r = _scene(monkeypatch, days)
    assert r["position"] == expected and r["peer_median_days"] == 30 and r["peer_p25_days"] == 28 and r["peer_p75_days"] == 36, r


def test_zero_day_window_is_compared_not_crashed(monkeypatch):
    r = _scene(monkeypatch, 0)
    assert r["window_days"] == 0 and r["position"] == "shorter_than_peers"


def test_invalid_and_unknown_windows_are_reported_not_analysed(monkeypatch):
    assert _scene(monkeypatch, -5)["position"] == "window_invalid"
    _db(monkeypatch, [("t", None, "open", "c")])
    r = tools.window_baseline("t"); assert r["position"] == "window_unknown" and r["reason"] == "start date not recorded"
    assert tools.window_baseline("nope")["found"] is False


def test_invalid_peers_and_the_tender_itself_are_not_counted(monkeypatch):
    r = _scene(monkeypatch, 22, extra=[("bad", -5, "open", "c"), ("unk", None, "open", "c")])
    assert r["n_peers"] == 8                           # 8 valid peers: not the tender itself, the inverted one or the unknown one


def test_too_few_peers_means_no_judgement(monkeypatch):
    _db(monkeypatch, [("t", 3, "open", "c")] + [(f"p{i}", 30, "open", "c") for i in range(4)])
    assert tools.window_baseline("t")["position"] == "insufficient_peers"


def test_peer_group_falls_back_from_method_and_category_to_method_then_category(monkeypatch):
    # 3 peers share method+category; 6 more share only the method -> use 'same method', n = 9
    _db(monkeypatch, [("t", 30, "open", "c1")] + [(f"a{i}", 30, "open", "c1") for i in range(3)] + [(f"b{i}", 30, "open", "c2") for i in range(6)])
    r = tools.window_baseline("t"); assert r["peer_group"].startswith("same method (open)") and r["n_peers"] == 9
    # no method on the target -> compare within category
    _db(monkeypatch, [("t", 30, None, "c1")] + [(f"a{i}", 30, "open", "c1") for i in range(6)])
    assert tools.window_baseline("t")["peer_group"].startswith("same category")


def test_sources_start_with_the_tender_itself_and_all_exist(monkeypatch):
    r = _scene(monkeypatch, 5)
    assert r["sources"][0] == {"ocid": "t", "field_path": "tender.tenderPeriod.startDate"} and len(r["sources"]) == 2 + min(8, 20)


# ---------- loader and migration
def test_loader_counts_bad_and_inverted_windows(tmp_path):
    f = tmp_path / "w.json"; f.write_text(json.dumps([
        {"ocid": "w1", "tender": {"tenderPeriod": {"startDate": "garbage", "endDate": END}}},
        {"ocid": "w2", "tender": {"tenderPeriod": {"startDate": "2025-06-09T00:00:00Z", "endDate": END}}},
        {"ocid": "w3", "tender": {"tenderPeriod": {"startDate": "2025-05-01T00:00:00Z", "endDate": END}}}]))
    c = connect(); st = load_ocds(f, c)
    assert st["bad_dates"] == 1 and st["inverted_windows"] == 1
    assert c.execute("SELECT start FROM tenders WHERE ocid='w1'").fetchone()[0] is None
    assert c.execute("SELECT start FROM tenders WHERE ocid='w2'").fetchone()[0] == "2025-06-09T00:00:00Z"   # kept raw: flagged, never silently fixed


def test_old_databases_are_migrated_without_losing_rows(tmp_path):
    db = tmp_path / "old.db"; c = sqlite3.connect(db)
    c.execute("CREATE TABLE tenders(ocid TEXT PRIMARY KEY, title TEXT, description TEXT, status TEXT, category TEXT, method TEXT, deadline TEXT, "
              "value REAL, currency TEXT, eligibility TEXT, buyer_id TEXT, buyer_name TEXT, doc_urls TEXT)")
    c.execute("INSERT INTO tenders(ocid,title) VALUES('old1','kept')"); c.commit(); c.close()
    m = connect(db)
    assert m.execute("SELECT title, start FROM tenders WHERE ocid='old1'").fetchone()[:] == ("kept", None)


def test_the_sample_exercises_short_and_inverted_windows():
    c = connect()
    rows = c.execute("SELECT start, deadline FROM tenders WHERE ocid LIKE 'ocds-synth-%' AND start IS NOT NULL").fetchall()
    assert len(rows) >= 50 and any(r["start"] > r["deadline"] for r in rows) and any(tools.window_baseline(o["ocid"])["position"] == "shorter_than_peers"
        for o in c.execute("SELECT ocid FROM tenders WHERE ocid LIKE 'ocds-synth-%' AND start IS NOT NULL LIMIT 80"))


# ---------- through the agent and the brief
def run(ocid, audience="buyer", mode="loop", llm=None):
    r = asyncio.run(run_agent(ocid, "Prepare a tender readiness brief", "2025-05-01", llm or StubLLM("invest_all"), mode, audience=audience))
    return r, (config.OUTBOX / "pending" / f"{r['filing_id']}.md").read_text(encoding="utf-8")


def sev(r, word): return [f["severity"] for f in r["findings"] if word in f["statement"].lower()]


def test_short_window_is_a_labelled_signal_in_timing_with_the_legal_caveat():
    r, md = run("ocds-eval-win-short")
    timing = md.split("## Timing")[1].split("\n## ")[0]
    assert "Signal (worth a look, not proof)" in timing and "shorter than comparable tenders" in timing and "not with a legal minimum" in timing
    assert "| Bidding window | 5 day(s), opened 2025-05-27 |" in md
    assert groundedness(r["run_id"], r["findings"]) == 1.0


def test_typical_and_long_windows_are_not_alarms():
    r, md = run("ocds-eval-win-typical"); assert "signal" not in sev(r, "bidding window") and "within the range of comparable tenders" in md
    r, md = run("ocds-eval-win-long"); assert "signal" not in sev(r, "bidding window") and "longer than comparable tenders" in md


def test_inverted_window_is_a_data_error_in_the_brief():
    r, md = run("ocds-eval-win-inverted")
    assert "start date is after the deadline" in md and "not assessable" in md and "shorter than" not in md


def test_unknown_start_becomes_a_fix_for_the_buyer_and_a_question_for_the_supplier():
    r, md = run("ocds-eval-win-unknown")
    assert "Bidding window cannot be assessed: start date not recorded" in md and "Record the date the tender opened" in md
    assert "| Bidding window | not recorded |" in md
    assert "Confirm when the tender opened" in run("ocds-eval-win-unknown", "supplier")[1]


def test_window_signal_and_concentration_downgrade_do_not_interfere():
    r, md = run("ocds-eval-typ-short")
    assert "within the range of peers" in md                                  # concentration explained away by typical peers
    assert "signal" in sev(r, "shorter than comparable") and "signal" not in sev(r, "supplier")


def test_window_check_runs_in_both_modes_and_when_the_model_plan_fails():
    for mode in ("pipeline", "loop"):
        assert any(f["kind"] == "deadline" and "bidding window" in f["statement"].lower() for f in run("ocds-eval-win-short", mode=mode)[0]["findings"])
    assert "signal" in sev(run("ocds-eval-win-short", llm=StubLLM("plan_error"))[0], "shorter than comparable")


def test_a_missing_start_is_not_double_reported_as_a_generic_unknown_field():
    r, _ = run("ocds-eval-win-unknown")
    generic = [f for f in r["findings"] if "fields not recorded in the notice" in f["statement"]]
    assert not any("start" in f["statement"].split("recorded in the notice:")[1] for f in generic)
