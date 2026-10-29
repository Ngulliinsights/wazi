"""Tests for independent middle-office validation module."""
import pytest

from bidbox import validation
from bidbox.store import connect, load_ocds
from pathlib import Path

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "sample_ocds.json"


@pytest.fixture(scope="module", autouse=True)
def loaded():
    conn = connect()
    load_ocds(SAMPLE, conn)
    return conn


def test_validate_buyer_consistency_both_provided(loaded):
    """Check passes when buyer ID and name are consistent."""
    r = validation.validate_buyer_consistency("KE-B-001", "Demo County Government A")
    assert r["valid"] is True
    assert len(r["issues"]) == 0


def test_validate_buyer_consistency_no_identifier():
    """Check fails when no identifier is provided."""
    r = validation.validate_buyer_consistency(None, None)
    assert r["valid"] is False
    assert len(r["issues"]) > 0


def test_validate_tender_integrity_valid(loaded):
    """Check passes for a valid tender."""
    r = validation.validate_tender_integrity("ocds-synth-0001")
    assert r["valid"] is True


def test_validate_tender_integrity_not_found():
    """Check fails for non-existent tender."""
    r = validation.validate_tender_integrity("nonexistent-ocid")
    assert r["valid"] is False
    assert any("not found" in issue.lower() for issue in r["issues"])


def test_validate_brief_findings_valid():
    """Check passes for well-structured findings."""
    findings = [
        {"kind": "deadline", "statement": "Deadline is recorded", "evidence": ["E1"], "severity": "info"},
        {"kind": "price", "statement": "Price is 1000 KES", "evidence": ["E2"], "severity": "check"}
    ]
    r = validation.validate_brief_findings(findings)
    assert r["valid"] is True


def test_validate_brief_findings_empty():
    """Check fails for empty findings."""
    r = validation.validate_brief_findings([])
    assert r["valid"] is False
    assert any("no findings" in issue.lower() for issue in r["issues"])


def test_validate_brief_findings_invalid_kind():
    """Check fails for invalid kind."""
    findings = [
        {"kind": "invalid_kind", "statement": "Test", "evidence": ["E1"], "severity": "info"}
    ]
    r = validation.validate_brief_findings(findings)
    assert r["valid"] is False
    assert any("invalid kind" in issue.lower() for issue in r["issues"])


def test_validate_brief_findings_missing_fields():
    """Check fails for missing required fields."""
    findings = [
        {"kind": "deadline", "statement": "Test"}  # missing evidence and severity
    ]
    r = validation.validate_brief_findings(findings)
    assert r["valid"] is False
    assert any("missing required fields" in issue.lower() for issue in r["issues"])


def test_validate_brief_findings_invalid_severity():
    """Check fails for invalid severity."""
    findings = [
        {"kind": "deadline", "statement": "Test", "evidence": ["E1"], "severity": "invalid"}
    ]
    r = validation.validate_brief_findings(findings)
    assert r["valid"] is False
    assert any("invalid severity" in issue.lower() for issue in r["issues"])


def test_validate_brief_findings_not_a_list():
    """Check fails when findings is not a list."""
    r = validation.validate_brief_findings("not a list")
    assert r["valid"] is False
    assert any("must be a list" in issue.lower() for issue in r["issues"])
