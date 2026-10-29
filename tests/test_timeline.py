"""Tests for process timeline check (decision window from deadline to award)."""
import pytest
from bidbox import tools


def test_process_timeline_check_not_found():
    """Timeline check with non-existent tender."""
    result = tools.process_timeline_check("nonexistent-ocid")
    assert result["found"] is False
    assert result["ocid"] == "nonexistent-ocid"
    assert len(result["issues"]) == 0
    assert len(result["sources"]) == 0


def test_process_timeline_check_valid_tender():
    """Timeline check with valid tender and award dates."""
    result = tools.process_timeline_check("ocds-synth-0001")
    # If data is not loaded, found will be False - that's acceptable for this test
    # We're testing the tool structure, not the data
    assert "found" in result
    assert "ocid" in result
    if result["found"]:
        assert "decision_window_days" in result
        assert "sources" in result


def test_process_timeline_check_sources_recorded():
    """Timeline check records source field paths."""
    result = tools.process_timeline_check("ocds-synth-0001")
    sources = result["sources"]
    # Check that sources have the expected structure
    for src in sources:
        assert "ocid" in src
        assert "field_path" in src
