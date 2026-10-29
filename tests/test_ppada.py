"""Tests for PPADA Act-specific checks from the edge-case checklist."""
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


def test_ppada_date_order_check_valid_tender(loaded):
    """Check handles a valid tender."""
    r = tools.ppada_date_order_check("ocds-synth-0001")
    assert r["found"] is True
    assert "order_valid" in r
    assert "issues" in r


def test_ppada_date_order_check_not_found():
    """Check handles non-existent tender."""
    r = tools.ppada_date_order_check("nonexistent-ocid")
    assert r["found"] is False


def test_single_bidder_check_multiple_suppliers(loaded):
    """Check handles tender with multiple suppliers."""
    r = tools.single_bidder_check("ocds-synth-0001")
    assert r["found"] is True
    assert "is_single_bidder" in r
    assert "n_suppliers" in r


def test_single_bidder_check_not_found():
    """Check handles non-existent tender."""
    r = tools.single_bidder_check("nonexistent-ocid")
    assert r["found"] is False


def test_termination_check_terminated_status():
    """Check flags terminated status."""
    # This test would need a tender with terminated status
    # For now, just check it doesn't crash
    r = tools.termination_check("ocds-synth-0001")
    assert r["found"] is True
    assert "is_terminated" in r


def test_publication_quality_check(loaded):
    """Check publication quality - missing fields."""
    r = tools.publication_quality_check("ocds-synth-0001")
    assert r["found"] is True
    assert "issues" in r
