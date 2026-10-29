"""Tests for aged items and budget exposure checks."""
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


def test_aged_items_check_valid_tender(loaded):
    """Check handles a valid tender."""
    r = tools.aged_items_check("ocds-synth-0001")
    assert r["found"] is True
    assert "is_aged" in r
    assert "issues" in r


def test_aged_items_check_not_found():
    """Check handles non-existent tender."""
    r = tools.aged_items_check("nonexistent-ocid")
    assert r["found"] is False
    assert r["is_aged"] is False


def test_budget_exposure_valid_buyer(loaded):
    """Check handles a valid buyer."""
    r = tools.budget_exposure(buyer_id="KE-B-001")
    assert r["found"] is True
    assert "exposure_calculated" in r
    assert "total_awards" in r


def test_budget_exposure_no_identifier():
    """Check handles missing identifier."""
    r = tools.budget_exposure()
    assert r["found"] is False
    assert r["exposure_calculated"] is False
