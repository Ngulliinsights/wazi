"""Independent validation: code-based checks separate from the agent.

These checks run after the agent produces a brief but before filing, catching issues
that require separate validation logic beyond the agent's scope.
"""
from contextlib import closing
from typing import Any
from .findings import FINDING_KINDS, FINDING_SEVERITIES
from .store import connect


def validate_buyer_consistency(buyer_id: str | None, buyer_name: str | None, conn=None) -> dict:
    """Code-based check: verify buyer ID and name are consistent across all records.
    This is a separate validation from the agent's counterparty check - it's about
    data integrity, not about whether the counterparty is 'ready' for analysis."""
    issues = []
    
    if not buyer_id and not buyer_name:
        return {"valid": False, "issues": ["No buyer identifier provided"]}

    if conn is None:
        with closing(connect()) as db:
            return validate_buyer_consistency(buyer_id, buyer_name, db)

    # Check if ID/name combinations are consistent.
    if buyer_id and buyer_name:
        names_for_id = conn.execute(
            "SELECT DISTINCT buyer_name FROM tenders WHERE buyer_id=?",
            (buyer_id,)
        ).fetchall()
        if len(names_for_id) > 1:
            issues.append(f"Buyer ID {buyer_id} is associated with {len(names_for_id)} different names")

        ids_for_name = conn.execute(
            "SELECT DISTINCT buyer_id FROM tenders WHERE lower(buyer_name)=lower(?)",
            (buyer_name,)
        ).fetchall()
        if len(ids_for_name) > 1:
            issues.append(f"Buyer name '{buyer_name}' is associated with {len(ids_for_name)} different IDs")
    
    return {"valid": len(issues) == 0, "issues": issues}


def validate_tender_integrity(ocid: str, conn=None) -> dict:
    """Code-based check: verify tender data integrity (no contradictions between fields).
    Checks for data anomalies that could indicate corruption or loading errors."""
    issues = []

    if conn is None:
        with closing(connect()) as db:
            return validate_tender_integrity(ocid, db)

    tender = conn.execute("SELECT * FROM tenders WHERE ocid=?", (ocid,)).fetchone()
    if not tender:
        return {"valid": False, "issues": [f"Tender {ocid} not found"]}

    if tender["start"] and tender["deadline"]:
        from datetime import datetime
        try:
            start = datetime.fromisoformat(tender["start"].replace("Z", "+00:00"))
            deadline = datetime.fromisoformat(tender["deadline"].replace("Z", "+00:00"))
            if start > deadline:
                issues.append("Start date is after deadline (data anomaly)")
        except ValueError:
            issues.append("Date format is invalid")

    if tender["value"] is not None and tender["value"] <= 0:
        issues.append("Estimated value is non-positive (should be positive or null)")

    if not tender["buyer_id"] and not tender["buyer_name"]:
        issues.append("No buyer information recorded")
    
    return {"valid": len(issues) == 0, "issues": issues}


def validate_brief_findings(findings: list[dict[str, Any]]) -> dict:
    """Code-based check: validate findings structure and content independently.
    This is a separate pass from the agent's verify step - it checks for
    structural issues and logical consistency."""
    issues = []
    
    if not isinstance(findings, list):
        return {"valid": False, "issues": ["Findings must be a list"]}
    
    seen_kinds = set()
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            issues.append(f"Finding {i} is not a dictionary")
            continue
        
        # Check required fields
        required = ["kind", "statement", "source_ocid", "field_path", "evidence", "severity"]
        missing = [field for field in required if field not in f]
        if missing:
            issues.append(f"Finding {i} missing required fields: {', '.join(missing)}")
        
        # Check kind validity
        if "kind" in f:
            if f["kind"] not in FINDING_KINDS:
                issues.append(f"Finding {i} has invalid kind: {f['kind']}")
            seen_kinds.add(f["kind"])
        
        # Check severity validity
        if "severity" in f:
            if f["severity"] not in FINDING_SEVERITIES:
                issues.append(f"Finding {i} has invalid severity: {f['severity']}")
        if "evidence" in f and (
            not isinstance(f["evidence"], list)
            or not f["evidence"]
            or any(not isinstance(item, str) or not item.strip() for item in f["evidence"])
        ):
            issues.append(f"Finding {i} must cite at least one evidence item")
    
    # Check that we have at least some findings
    if len(findings) == 0:
        issues.append("No findings present")
    
    return {"valid": len(issues) == 0, "issues": issues}


def validate_filing(conn, ocid: str, findings: list[dict[str, Any]]) -> dict:
    """Run independent checks against the same connection and accepted findings that will be filed."""
    tender = conn.execute("SELECT buyer_id, buyer_name FROM tenders WHERE ocid=?", (ocid,)).fetchone()
    buyer_id, buyer_name = (tender["buyer_id"], tender["buyer_name"]) if tender else (None, None)
    return {
        "tender_integrity": validate_tender_integrity(ocid, conn),
        "buyer_consistency": validate_buyer_consistency(buyer_id, buyer_name, conn),
        "brief_findings": validate_brief_findings(findings),
    }
