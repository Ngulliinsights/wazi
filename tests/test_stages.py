"""Real OCDS feeds publish one release PER STAGE for the same tender. Later releases must add to earlier ones, never wipe them."""
import json

import pytest

from bidbox.store import connect, load_ocds

TENDER = {"ocid": "ocds-st-1", "date": "2025-03-01", "tag": ["tender"], "buyer": {"id": "B1", "name": "Stage Buyer"},
          "tender": {"title": "Supply of pens", "status": "active", "mainProcurementCategory": "goods", "procurementMethod": "open",
                     "tenderPeriod": {"startDate": "2025-03-01T00:00:00Z", "endDate": "2025-04-01T00:00:00Z"}, "value": {"amount": 5000, "currency": "KES"},
                     "eligibilityCriteria": "Tax certificate", "documents": [{"url": "https://example.org/a.pdf"}]}}
AWARD = {"ocid": "ocds-st-1", "date": "2025-04-20", "tag": ["award"], "awards": [{"id": "a1", "value": {"amount": 4800, "currency": "KES"}, "suppliers": [{"id": "S1", "name": "Pen Co"}]}]}
CONTRACT = {"ocid": "ocds-st-1", "date": "2025-05-01", "tag": ["contract"], "tender": {"status": "complete"}, "contracts": [{"id": "c1", "awardID": "a1"}]}
FIELDS = ("title", "buyer_name", "deadline", "start", "value", "eligibility", "category", "method", "currency", "doc_urls")


def load(tmp_path, releases, name="p.json", **kw):
    f = tmp_path / name; f.write_text(json.dumps({"releases": releases}), encoding="utf-8")
    c = connect(tmp_path / "t.db"); load_ocds(f, c, **kw); return c       # a private database per test: no leakage between tests


def row(c, oc="ocds-st-1"): return c.execute("SELECT * FROM tenders WHERE ocid=?", (oc,)).fetchone()
def awards(c, oc="ocds-st-1"): return c.execute("SELECT award_id, supplier_id, amount FROM awards WHERE ocid=? ORDER BY award_id, supplier_id", (oc,)).fetchall()


@pytest.mark.parametrize("order", [(TENDER, AWARD), (AWARD, TENDER), (TENDER, AWARD, CONTRACT), (CONTRACT, AWARD, TENDER)])
def test_stage_releases_in_any_order_leave_a_complete_tender(tmp_path, order):
    c = load(tmp_path, [dict(r) for r in order], f"o{len(order)}{order[0]['tag'][0]}.json"); r = row(c)
    assert [k for k in FIELDS if r[k] in (None, "", "[]")] == [], "no field may be lost"
    assert r["title"] == "Supply of pens" and r["value"] == 5000 and r["buyer_name"] == "Stage Buyer" and r["deadline"].startswith("2025-04-01")
    assert [(a["award_id"], a["supplier_id"], a["amount"]) for a in awards(c)] == [("a1", "S1", 4800)]


def test_a_later_release_overrides_what_it_changes_and_keeps_what_it_does_not_mention(tmp_path):
    amended = {"ocid": "ocds-st-1", "tag": ["tenderAmendment"], "tender": {"tenderPeriod": {"endDate": "2025-04-15T00:00:00Z"}, "value": {"amount": 6000}}}
    r = row(load(tmp_path, [TENDER, amended]))
    assert r["deadline"].startswith("2025-04-15") and r["value"] == 6000                     # amended
    assert r["start"].startswith("2025-03-01") and r["title"] == "Supply of pens" and r["currency"] == "KES" and r["eligibility"] == "Tax certificate"   # untouched


def test_a_null_or_empty_field_in_a_later_release_never_erases_earlier_data(tmp_path):
    later = {"ocid": "ocds-st-1", "tender": {"title": None, "value": {"amount": None}, "eligibilityCriteria": "", "documents": [], "tenderPeriod": {}}, "buyer": {}}
    r = row(load(tmp_path, [TENDER, later]))
    assert r["title"] == "Supply of pens" and r["value"] == 5000 and r["eligibility"] == "Tax certificate" and r["buyer_name"] == "Stage Buyer" and "a.pdf" in r["doc_urls"]


def test_awards_accumulate_across_releases_and_the_same_award_is_replaced_not_duplicated(tmp_path):
    a2 = {"ocid": "ocds-st-1", "awards": [{"id": "a2", "value": {"amount": 100}, "suppliers": [{"id": "S2"}]}]}
    a1b = {"ocid": "ocds-st-1", "awards": [{"id": "a1", "value": {"amount": 4700}, "suppliers": [{"id": "S1"}]}]}   # a1 corrected
    c = load(tmp_path, [TENDER, AWARD, a2, a1b])
    assert [(a["award_id"], a["amount"]) for a in awards(c)] == [("a1", 4700), ("a2", 100)]


def test_joint_venture_awards_survive_across_stages(tmp_path):
    jv = {"ocid": "ocds-st-1", "awards": [{"id": "a1", "value": {"amount": 1}, "suppliers": [{"id": "S1"}, {"id": "S2"}]}]}
    assert [(a["award_id"], a["supplier_id"]) for a in awards(load(tmp_path, [TENDER, jv]))] == [("a1", "S1"), ("a1", "S2")]


def test_reloading_the_same_package_is_idempotent(tmp_path):
    c = load(tmp_path, [TENDER, AWARD]); before = ([tuple(row(c)) for _ in [0]], [tuple(a) for a in awards(c)])
    load_ocds(tmp_path / "p.json", c)
    assert ([tuple(row(c))], [tuple(a) for a in awards(c)]) == before and c.execute("SELECT COUNT(*) FROM tenders WHERE ocid='ocds-st-1'").fetchone()[0] == 1


def test_synthetic_data_is_recognised_from_the_publisher_and_can_be_forced(tmp_path):
    def pkg(name, publisher): f = tmp_path / name; f.write_text(json.dumps({"publisher": publisher, "releases": [{**TENDER, "ocid": name}]}), encoding="utf-8"); return f
    c = connect(tmp_path / "s.db")
    load_ocds(pkg("s1", {"name": "SYNTHETIC - not real data"}), c); load_ocds(pkg("r1", {"name": "Public Procurement Regulatory Authority"}), c)
    load_ocds(pkg("f1", {"name": "Public Procurement Regulatory Authority"}), c, synthetic=True); load_ocds(pkg("f2", {"name": "synthetic eval fixture"}), c, synthetic=False)
    assert [row(c, o)["synthetic"] for o in ("s1", "r1", "f1", "f2")] == [1, 0, 1, 0]
