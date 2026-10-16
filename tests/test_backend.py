import asyncio
import json
from pathlib import Path

import pytest

from bidbox import config, gate, tools
from bidbox.cli import audit
from bidbox.findings import verify_findings
from bidbox.store import connect, load_ocds

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_ocds.json"


@pytest.fixture(scope="module", autouse=True)
def loaded():
    conn = connect()
    load_ocds(SAMPLE, conn)
    return conn


def test_load_counts(loaded):
    a = audit(loaded)
    q = lambda sql: loaded.execute(sql).fetchone()[0]
    assert q("SELECT COUNT(*) FROM tenders WHERE ocid LIKE 'ocds-synth-%'") == 80      # other modules may load extra scenario data
    assert q("SELECT COUNT(*) FROM awards WHERE ocid LIKE 'ocds-synth-%'") > 50 and a["tenders"] >= 80
    assert a["pct_with_buyer_name"] < 100          # missing buyer names survive loading
    assert a["awards_missing_supplier"] >= 1


def test_notice_unknowns_not_guessed():
    r = tools.get_tender_notice("ocds-synth-0031")  # deadline blanked
    assert r["found"] and "deadline" in r["unknown"] and r["deadline_status"] == "unknown"


def test_notice_missing_ocid():
    assert tools.get_tender_notice("nope")["found"] is False


def test_deadline_replay_as_of():
    r1 = tools.get_tender_notice("ocds-synth-0001", as_of="2000-01-01")
    r2 = tools.get_tender_notice("ocds-synth-0001", as_of="2099-01-01")
    assert r1["deadline_status"] == "open" and r2["deadline_status"] == "closed"


def test_buyer_history_by_id_when_name_missing():
    r = tools.buyer_award_history("KE-B-004")
    assert r["found"] and r["n_awards"] > 0 and r["sources"]


def test_buyer_concentration_is_a_signal_not_a_verdict():
    r = tools.buyer_award_history("Demo County Government A")
    assert any(f.startswith("signal:") for f in r["flags"])
    assert not any("fraud" in f.lower() or "corrupt" in f.lower() for f in r["flags"])


def test_price_benchmark_excludes_extreme_outlier():
    r = tools.price_benchmark("goods", 1_200_000)
    assert r["median"] < 10_000_000 and r["sources"]
    all_cats = [tools.price_benchmark(c, 1) for c in ("goods", "works", "services")]
    assert sum(x["n_excluded_outliers"] for x in all_cats) >= 1  # the planted 9e12 value is surfaced


def test_price_benchmark_low_confidence():
    r = tools.price_benchmark("nonexistent-category", 100)
    assert r["low_confidence"] and r["position"] == "insufficient_data"


def test_verifier_rejects_unsourced_unknown_and_accusatory(loaded):
    good = {"kind": "deadline", "statement": "Deadline recorded in notice", "source_ocid": "ocds-synth-0001", "field_path": "tender.tenderPeriod.endDate"}
    bad = [{"kind": "price", "statement": "Price looks high", "source_ocid": "", "field_path": ""},
           {"kind": "price", "statement": "Price looks high", "source_ocid": "ocds-nope", "field_path": "awards[].value.amount"},
           {**good, "statement": "Supplier committed fraud"}]
    acc, rej = verify_findings([good] + bad, loaded)
    assert len(acc) == 1 and len(rej) == 3


def _finding(**kw):
    return {"kind": "deadline", "statement": "Deadline recorded in notice", "source_ocid": "ocds-synth-0001",
            "field_path": "tender.tenderPeriod.endDate", **kw}


def test_file_brief_only_queues_and_gate_requires_named_human():
    r = tools.file_brief("ocds-synth-0001", "Test summary", [_finding()])
    assert r["status"] == "pending_approval"
    fid = r["filing_id"]
    assert (config.OUTBOX / "pending" / f"{fid}.md").exists()
    assert not (config.OUTBOX / "filed" / f"{fid}.md").exists()
    for who in ("", "agent", "Claude", "x"):
        with pytest.raises(PermissionError):
            gate.approve(fid, who)
    rec = gate.approve(fid, "Wanjiku Mwangi")
    assert rec["approver"] == "Wanjiku Mwangi" and (config.OUTBOX / "filed" / f"{fid}.md").exists()


def test_file_brief_with_no_sourced_findings_files_nothing():
    r = tools.file_brief("ocds-synth-0001", "x", [{"kind": "price", "statement": "unsourced claim here", "source_ocid": "", "field_path": ""}])
    assert r["status"] == "not_filed"


def test_agent_has_no_award_or_approve_tool():
    from bidbox.server import mcp
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {"get_tender_notice", "buyer_award_history", "price_benchmark", "peer_baseline", "window_baseline", "file_brief"}


def test_every_call_is_logged_with_inputs_outputs_timestamps():
    lines = [json.loads(l) for l in config.LOG_PATH.read_text().splitlines()]
    tools_logged = {l.get("tool") for l in lines}
    assert {"get_tender_notice", "buyer_award_history", "price_benchmark", "file_brief", "human_approval"} <= tools_logged
    assert all("ts" in l and "run_id" in l for l in lines)
    assert all(("tool" in l) or ("event" in l) for l in lines)  # every line is a tool call or an agent event


def test_price_benchmark_matches_hand_computed_values(monkeypatch):
    """Hand-worked: goods awards 100,110,120,130,140,150 and 1e9 (planted outlier).
    Full median 130, MAD 20*1.4826 -> 1e9 has z ~ 3e7 -> excluded. Kept median = (120+130)/2 = 125.
    A value of 125 therefore has robust z of 0 and ratio 1.0."""
    import sqlite3
    from bidbox import store
    def fresh_conn():
        c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; c.executescript(store.SCHEMA)
        for i, amt in enumerate([100, 110, 120, 130, 140, 150, 1_000_000_000]):
            c.execute("INSERT INTO tenders(ocid,category) VALUES(?,?)", (f"o{i}", "goods"))
            c.execute("INSERT INTO awards VALUES(?,?,?,?,?,?,?)", (f"o{i}", "a", "s", "S", amt, "KES", None))
        return c
    monkeypatch.setattr(tools, "connect", fresh_conn)
    r = tools.price_benchmark("goods", 125)
    assert r["median"] == 125 and r["n_used"] == 6 and r["n_excluded_outliers"] == 1
    assert r["excluded_outliers"][0]["amount"] == 1_000_000_000
    assert r["robust_z"] == 0 and r["ratio_to_median"] == 1.0 and r["position"] == "within_range"
    assert tools.price_benchmark("goods", 10_000)["position"] == "above_range"


def _fixture_db(monkeypatch, rows, buyer="B1", category="goods"):
    import sqlite3
    from bidbox import store
    def fresh_conn():
        c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; c.executescript(store.SCHEMA)
        for i, (sup, amt) in enumerate(rows):
            c.execute("INSERT INTO tenders(ocid,category,buyer_id,buyer_name) VALUES(?,?,?,?)", (f"o{i}", category, buyer, "Buyer One"))
            c.execute("INSERT INTO awards VALUES(?,?,?,?,?,?,?)", (f"o{i}", "a", sup, sup, amt, "KES", f"2025-01-{i+1:02d}"))
        return c
    monkeypatch.setattr(tools, "connect", fresh_conn)
    # For backward compatibility, return a connection for the first call
    return fresh_conn()


def test_concentration_signal_boundaries(monkeypatch):
    sig = lambda r: any(f.startswith("signal") for f in r["flags"])
    _fixture_db(monkeypatch, [("S1", 1)] * 3 + [("S2", 1), ("S3", 1), ("S4", 1)])      # 3 of 6 = exactly 0.5 -> fires
    assert sig(tools.buyer_award_history("B1"))
    _fixture_db(monkeypatch, [("S1", 1)] * 2 + [("S2", 1), ("S3", 1), ("S4", 1), ("S5", 1)])  # 2 of 6 = 0.33 -> quiet
    assert not sig(tools.buyer_award_history("B1"))
    _fixture_db(monkeypatch, [("S1", 1)] * 4)                                           # 100% but only 4 awards -> too thin to call
    assert not sig(tools.buyer_award_history("B1"))


def test_price_position_thresholds_are_meaningful(monkeypatch):
    _fixture_db(monkeypatch, [("S", a) for a in (100, 110, 120, 130, 140, 150)])  # median 125, scaled MAD 22.239
    pos = lambda amt: tools.price_benchmark("goods", amt)["position"]
    assert pos(150) == "within_range"      # z ~ 1.1
    assert pos(200) == "above_range"       # z ~ 3.4
    assert pos(50) == "below_range"        # z ~ -3.4
