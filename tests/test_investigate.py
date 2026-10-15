"""peer_baseline thresholds, and the bounded investigation step (allowlist, step budget, deterministic downgrade)."""
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from bidbox import config, store, tools
from bidbox.agent import run_agent
from bidbox.agent.llm import StubLLM
from bidbox.evals import build_fixture_releases
from bidbox.store import connect, load_ocds

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_ocds.json"


@pytest.fixture(scope="module", autouse=True)
def data():
    f = Path(tempfile.mkdtemp()) / "fx.json"; f.write_text(json.dumps({"releases": build_fixture_releases()}))
    c = connect(); load_ocds(SAMPLE, c); load_ocds(f, c)


def run(ocid, llm=None, mode="loop"):
    return asyncio.run(run_agent(ocid, "Prepare a tender readiness brief", "2026-01-01", llm or StubLLM("invest_all"), mode))


def ev(r, name): return [e for e in r["events"] if e["event"] == name]
def tool_calls(r): return [e["tool"] for e in r["events"] if e["event"] == "tool_call"]
def sevs(r): return [f["severity"] for f in r["findings"]]


# ---------- peer_baseline thresholds (hand-built fixtures, n=25 awards per buyer)
def _buyers(monkeypatch, shares, n=25, category="c"):
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row; conn.executescript(store.SCHEMA)
    for b, share in shares.items():
        top = round(share * n)
        for k in range(n):
            oc = f"{b}-{k}"
            conn.execute("INSERT INTO tenders(ocid,category,buyer_id,buyer_name) VALUES(?,?,?,?)", (oc, category, b, b))
            conn.execute("INSERT INTO awards VALUES(?,?,?,?,?,?,?)", (oc, "a", f"{b}-top" if k < top else f"{b}-o{k}", None, 1.0, "KES", None))
    monkeypatch.setattr(tools, "connect", lambda: conn)


PEERS = {"p1": .28, "p2": .28, "p3": .40, "p4": .48, "p5": .56}   # sorted: median .40, p75 .48


def test_peer_baseline_above_needs_both_p75_and_a_margin_over_median(monkeypatch):
    _buyers(monkeypatch, {**PEERS, "t": .56})        # .56 > .48 and .56-.40 >= .15
    assert tools.peer_baseline("t", "c")["position"] == "above_peers"
    _buyers(monkeypatch, {**PEERS, "t": .52})        # > p75 but margin only .12 -> still typical
    assert tools.peer_baseline("t", "c")["position"] == "typical_for_peers"
    _buyers(monkeypatch, {**PEERS, "t": .44})        # inside the peer range
    assert tools.peer_baseline("t", "c")["position"] == "typical_for_peers"


def test_peer_baseline_refuses_to_judge_on_thin_evidence(monkeypatch):
    _buyers(monkeypatch, {"p1": .3, "p2": .3, "t": .9})              # only 2 peers
    r = tools.peer_baseline("t", "c"); assert r["position"] == "insufficient_peers" and r["low_confidence"]
    _buyers(monkeypatch, {**PEERS, "t": .9}, n=4)                    # target has too few awards
    assert tools.peer_baseline("t", "c")["position"] == "insufficient_target"
    assert tools.peer_baseline("nobody", "c")["found"] is False


# ---------- the bounded investigation step
def test_typical_concentration_is_downgraded_by_data_not_by_model():
    class Loud(StubLLM):  # model insists everything is a signal
        def complete_json(self, task, payload):
            out = super().complete_json(task, payload)
            for f in out.get("findings", []) if isinstance(out, dict) else []: f["severity"] = "signal"
            return out
    r = run("ocds-eval-typ-target", Loud("invest_all"))
    assert "peer_baseline" in tool_calls(r) and ev(r, "investigate_step")
    assert "signal" not in sevs(r)
    assert any("within the range of peers" in f["statement"] for f in r["findings"])


def test_concentration_above_peers_keeps_its_signal():
    r = run("ocds-eval-abv-target")
    assert "signal" in sevs(r) and any("above the range of peers" in f["statement"] for f in r["findings"])


def test_pipeline_mode_never_investigates():
    r = run("ocds-eval-abv-target", mode="pipeline")
    assert not ev(r, "investigate_step") and "peer_baseline" not in tool_calls(r)


def test_no_signal_means_no_investigation_and_no_model_call_for_it():
    llm = StubLLM("invest_all")
    r = run("ocds-eval-nohist", llm)
    assert ev(r, "investigate_skipped") and getattr(llm, "invest_calls", 0) == 0


def test_history_without_a_signal_is_not_investigated():
    """A buyer WITH history but no concentration signal: the skip must come from 'no signal', not 'no history'."""
    c = connect(); pick = None
    for r in c.execute("SELECT ocid, buyer_id, category FROM tenders WHERE buyer_id IN ('KE-B-002','KE-B-003') AND category IS NOT NULL"):
        h = tools.buyer_award_history(r["buyer_id"], r["category"])
        if h["found"] and h["n_awards"] >= 1 and not any(f.startswith("signal") for f in h["flags"]):
            pick = r["ocid"]; break
    assert pick, "sample data should contain a buyer with history and no signal"
    llm = StubLLM("invest_all")
    r = run(pick, llm)
    assert ev(r, "investigate_skipped") and getattr(llm, "invest_calls", 0) == 0 and "peer_baseline" not in tool_calls(r)
    assert "buyer_award_history" in tool_calls(r)   # history WAS fetched, so the skip was about the missing signal


def test_model_choosing_a_tool_outside_the_allowlist_is_rejected():
    r = run("ocds-eval-abv-target", StubLLM("invest_junk"))
    stop = ev(r, "investigate_stop")
    assert stop and stop[0]["rejected_tool"] == "drop_database" and "peer_baseline" not in tool_calls(r)
    assert r["status"] == "awaiting_human_approval"


def test_repeating_a_check_is_refused_so_the_loop_is_bounded():
    r = run("ocds-eval-abv-target", StubLLM("invest_repeat"))
    assert tool_calls(r).count("peer_baseline") == 1 and len(ev(r, "investigate_step")) == 1


def test_two_allowed_steps_then_stop():
    r = run("ocds-eval-abv-target", StubLLM("invest_all"))
    assert [e["tool"] for e in ev(r, "investigate_step")] == ["peer_baseline", "history_all_categories"]
    assert any("Across all categories" in f["statement"] for f in r["findings"])


def test_model_failure_inside_the_investigation_does_not_stop_the_run():
    class Boom(StubLLM):
        def complete_json(self, task, payload):
            if task == "investigate": raise KeyError("buggy client")
            return super().complete_json(task, payload)
    r = run("ocds-eval-abv-target", Boom())
    assert ev(r, "investigate_error") and r["status"] == "awaiting_human_approval"
    assert "signal" in sevs(r)   # untested signal stays a signal, but the brief says it was not tested
    assert any("not tested against peer buyers" in f["statement"] for f in r["findings"])


def test_pipeline_brief_says_the_signal_was_not_peer_tested():
    r = run("ocds-eval-abv-target", mode="pipeline")
    assert "signal" in sevs(r) and any("not tested against peer buyers" in f["statement"] for f in r["findings"])
