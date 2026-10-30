"""Consortium (joint venture) awards list several suppliers on ONE award. Metrics must count awards, not supplier rows."""
import json
import sqlite3

from wazi import store, tools


def _db(monkeypatch, awards, buyer="B", category="goods"):
    """awards: list of (supplier_ids tuple, amount). One tender+award per entry."""
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row; conn.executescript(store.SCHEMA)
    for i, (sups, amt) in enumerate(awards):
        conn.execute("INSERT INTO tenders(ocid,category,buyer_id,buyer_name) VALUES(?,?,?,?)", (f"o{i}", category, buyer, buyer))
        for s in sups:
            conn.execute("INSERT INTO awards VALUES(?,?,?,?,?,?,?)", (f"o{i}", "a", s, s, amt, "KES", f"2025-01-{i+1:02d}"))
    monkeypatch.setattr(tools, "connect", lambda: conn)


def test_jv_awards_are_counted_once_and_each_member_is_credited(monkeypatch):
    # 6 real awards: three are a JV of A+B, three are single suppliers
    _db(monkeypatch, [(("JV-A", "JV-B"), 100)] * 3 + [(("S1",), 100), (("S2",), 100), (("S3",), 100)])
    r = tools.buyer_award_history("B")
    assert r["n_awards"] == 6                                   # not 9 supplier rows
    assert r["top_supplier_share"] == 0.5                       # JV-A took part in 3 of 6 awards
    assert any(f.startswith("signal") for f in r["flags"])      # the old row-count maths gave 0.33 and missed this


def test_jv_does_not_inflate_price_observations(monkeypatch):
    _db(monkeypatch, [(("A", "B"), 100), (("A", "B"), 110), (("A", "B"), 120), (("C",), 130), (("D",), 140)])
    r = tools.price_benchmark("goods", 120)
    assert r["n_used"] == 5 and r["median"] == 120               # 5 awards, not 8 rows


def test_jv_counts_once_in_peer_comparison(monkeypatch):
    # target buyer: 5 awards, all won by a JV (A+B) -> share 1.0 over 5 awards; five diverse peers
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row; conn.executescript(store.SCHEMA)
    def add(buyer, k, sups):
        conn.execute("INSERT INTO tenders(ocid,category,buyer_id,buyer_name) VALUES(?,?,?,?)", (f"{buyer}-{k}", "c", buyer, buyer))
        for s in sups:
            conn.execute("INSERT INTO awards VALUES(?,?,?,?,?,?,?)", (f"{buyer}-{k}", "a", s, s, 1.0, "KES", None))
    for k in range(5): add("T", k, ("A", "B"))
    for p in range(5):
        for k in range(5): add(f"P{p}", k, (f"P{p}-{k}",))
    monkeypatch.setattr(tools, "connect", lambda: conn)
    r = tools.peer_baseline("T", "c")
    assert r["buyer_awards"] == 5 and r["buyer_share"] == 1.0 and r["position"] == "above_peers"
