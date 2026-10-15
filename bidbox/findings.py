"""A finding without a source cannot exist. Enforced here, not in a prompt."""
import json
import re
import sqlite3
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from .textsafe import normalize, oneline

MAX_STATEMENT, MAX_FINDINGS = 1000, 60
PATH_OK = re.compile(r"^(tender|buyer|awards\[\])(\.[A-Za-z]+(\[\])?)*$")      # OCDS-style paths only
PATH_COLUMN = {"tender.tenderPeriod.endDate": "deadline", "tender.tenderPeriod.startDate": "start", "tender.eligibilityCriteria": "eligibility",
               "tender.value.amount": "value", "tender.procurementMethod": "method", "tender.mainProcurementCategory": "category",
               "buyer.name": "buyer_name", "buyer.id": "buyer_id", "tender.title": "title", "tender.description": "description"}


def validate_citation(conn, kind: str, ocid: str, path: str) -> str | None:
    """Return why a citation is not valid, or None. A citation must point at something the cited record really contains
    (a finding that REPORTS a gap, kind data_quality, may cite the empty field)."""
    if not PATH_OK.match(path or ""):
        return "not a recognised OCDS field path"
    if kind == "data_quality" or path == "tender":
        return None
    if path in PATH_COLUMN:
        row = conn.execute(f"SELECT {PATH_COLUMN[path]} AS v FROM tenders WHERE ocid=?", (ocid,)).fetchone()
        return None if row and row["v"] not in (None, "") else "the cited record has no value at that path"
    if path.startswith("awards[]"):
        col = "amount" if path.endswith("value.amount") else ("COALESCE(supplier_id, supplier_name)" if "suppliers" in path else "award_id")
        row = conn.execute(f"SELECT 1 FROM awards WHERE ocid=? AND {col} IS NOT NULL LIMIT 1", (ocid,)).fetchone()
        return None if row else "the cited record has no award data at that path"
    if path.startswith("tender.documents"):
        row = conn.execute("SELECT doc_urls FROM tenders WHERE ocid=?", (ocid,)).fetchone()
        return None if row and json.loads(row["doc_urls"] or "[]") else "the cited record has no documents"
    return None


VERDICT = re.compile(
    r"\b(you|we|i)\s+(should|must|ought to|need to|can)\s+(not\s+)?(bid|apply|submit|tender|go for|win)\b"   # advice to the reader
    r"|\bshould\s+(not\s+)?(bid|apply)\b|\b(we|i)\s+(recommend|advise|suggest)\b"
    r"|\brecommend(s|ed|ing)?\s+(that\s+)?(you|your|the supplier|suppliers|smes?|bidding|a bid)\b"
    r"|\b(this|the)\s+(tender|notice|award|process|procurement|buyer)\s+(is|was|appears|seems)\s+(not\s+|fully\s+)?(compliant|legal|lawful|irregular|improper|unlawful|fair|rigged|biased)\b"
    r"|\bbest bidder\b|\bshould be awarded\b|\bdeserves?\b|\bgo for it\b|\bdon'?t bid\b|\bworth bidding\b", re.I)
# Not blocked on purpose: "Bidders must submit a bid security..." or "a site visit is recommended" are facts from the tender, not advice.


def has_verdict(text: str) -> bool:
    """Briefs state facts and signals; they never tell a supplier to bid or who should win."""
    return bool(VERDICT.search(normalize(text)))


class Finding(BaseModel):
    kind: Literal["deadline", "eligibility", "price", "buyer_history", "data_quality", "other"]
    statement: str = Field(min_length=5, max_length=MAX_STATEMENT)
    source_ocid: str = Field(min_length=3)
    field_path: str = Field(min_length=3, description="e.g. tender.tenderPeriod.endDate")
    evidence: list[str] = Field(default_factory=list, description="ids of the evidence items this finding rests on")
    severity: Literal["info", "check", "signal"] = "info"  # 'signal' = worth a look, never proof

    @field_validator("statement", "source_ocid", "field_path", mode="before")
    @classmethod
    def _one_line(cls, v):
        return oneline(v) if isinstance(v, str) else v      # no newline can ever start a forged section in a brief

    @field_validator("statement")
    @classmethod
    def _no_accusations(cls, v: str) -> str:
        if any(w in v.lower() for w in ("fraud", "corrupt", "bribe", "stole", "illegal")):
            raise ValueError("findings describe signals, not wrongdoing")
        if has_verdict(v):
            raise ValueError("findings describe facts, not verdicts or advice to bid")
        return v


def verify_findings(raw: list[dict], conn: sqlite3.Connection):
    """Return (accepted, rejected). Rejects bad schema or a source ocid not in the store."""
    accepted, rejected = [], []
    for item in raw:
        try:
            f = Finding(**item)
        except (ValidationError, TypeError) as e:
            rejected.append({"finding": item, "reason": str(e).splitlines()[0][:200]})
            continue
        if conn.execute("SELECT 1 FROM tenders WHERE ocid=?", (f.source_ocid,)).fetchone() is None:
            rejected.append({"finding": item, "reason": f"source_ocid {f.source_ocid} not found"})
            continue
        why = validate_citation(conn, f.kind, f.source_ocid, f.field_path)
        if why:
            rejected.append({"finding": item, "reason": f"invalid citation: {why}"})
        else:
            accepted.append(f)
    return accepted, rejected
