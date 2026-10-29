"""Tests for amendment check (detects proper handling vs overwrites)."""
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


def test_amendment_check_valid_tender(loaded):
    """Check handles a valid tender without amendments."""
    r = tools.amendment_check("ocds-synth-0001")
    assert r["found"] is True
    # The synthetic data may or may not have amendments - just check it doesn't crash
    assert "has_amendments" in r
    assert "issues" in r


def test_amendment_check_not_found():
    """Check handles non-existent tender."""
    r = tools.amendment_check("nonexistent-ocid")
    assert r["found"] is False
    assert r["has_amendments"] is False


def test_amendment_check_sources_recorded(loaded):
    """Check that sources are properly recorded for audit trail."""
    r = tools.amendment_check("ocds-synth-0001")
    assert r["found"]
    assert len(r["sources"]) > 0
    for src in r["sources"]:
        assert "ocid" in src
        assert "field_path" in src
