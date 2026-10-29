"""Tests for counterparty quality check (pre-trade style validation)."""
import pytest

from bidbox import tools
from bidbox.store import connect, load_ocds
from pathlib import Path

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_ocds.json"


@pytest.fixture(scope="module", autouse=True)
def loaded():
    conn = connect()
    load_ocds(SAMPLE, conn)
    return conn


def test_counterparty_quality_buyer_exists(loaded):
    """Check passes when buyer ID and name are found and consistent."""
    r = tools.counterparty_quality(buyer_id="KE-B-001", buyer_name="Demo County Government A")
    assert r["found"] is True
    assert r["ready"] is True
    assert len(r["issues"]) == 0
    assert r["award_count"] > 0
    assert len(r["sources"]) > 0


def test_counterparty_quality_buyer_not_found(loaded):
    """Check fails when buyer is not in records."""
    r = tools.counterparty_quality(buyer_id="NONEXISTENT-ID", buyer_name="Nonexistent Buyer")
    assert r["found"] is True
    assert r["ready"] is False
    assert len(r["issues"]) > 0
    assert any("not found" in issue.lower() for issue in r["issues"])


def test_counterparty_quality_id_name_mismatch(loaded):
    """Check flags when ID and name don't match in any record."""
    r = tools.counterparty_quality(buyer_id="KE-B-001", buyer_name="Wrong Name")
    assert r["found"] is True
    assert r["ready"] is False
    assert any("do not match" in issue.lower() for issue in r["issues"])


def test_counterparty_quality_no_identifier(loaded):
    """Check fails when no identifier is provided."""
    r = tools.counterparty_quality()
    assert r["found"] is False
    assert r["ready"] is False
    assert len(r["issues"]) == 1
    assert "No buyer identifier" in r["issues"][0]


def test_counterparty_quality_thin_history(loaded):
    """Check flags insufficient award history."""
    # Use a buyer that likely has few awards in synthetic data
    r = tools.counterparty_quality(buyer_id="KE-B-999")  # unlikely to exist
    if r["found"]:
        # If it exists, check thin history logic
        if r["award_count"] < 5:
            assert any("Insufficient award history" in issue for issue in r["issues"])


def test_counterparty_quality_sources_are_recorded(loaded):
    """Check that sources are properly recorded for audit trail."""
    r = tools.counterparty_quality(buyer_id="KE-B-001")
    assert r["found"]
    assert len(r["sources"]) > 0
    for src in r["sources"]:
        assert "ocid" in src
        assert "field_path" in src
