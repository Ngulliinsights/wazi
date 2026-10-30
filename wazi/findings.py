"""A finding without a source cannot exist. Enforced here, not in a prompt."""
import json
import re
import sqlite3
from typing import Literal, get_args

from pydantic import BaseModel, Field, ValidationError, field_validator

from .textsafe import normalize, oneline

MAX_STATEMENT, MAX_FINDINGS = 1000, 60
MAX_REF = 200                      # longest accepted source_ocid / field_path (the API caps an ocid at the same length)
FindingKind = Literal["deadline", "eligibility", "price", "buyer_history", "data_quality", "other"]
FindingSeverity = Literal["info", "check", "signal"]
FINDING_KINDS = get_args(FindingKind)
FINDING_SEVERITIES = get_args(FindingSeverity)

# --------------------------------------------------------------------------- citations

# OCDS-style paths only. \Z, not $: "$" would also accept a path followed by a trailing newline.
PATH_OK = re.compile(r"^(tender|buyer|awards\[\])(\.[A-Za-z]+(\[\])?)*\Z")
PATH_COLUMN = {"tender.tenderPeriod.endDate": "deadline", "tender.tenderPeriod.startDate": "start", "tender.eligibilityCriteria": "eligibility",
               "tender.value.amount": "value", "tender.procurementMethod": "method", "tender.mainProcurementCategory": "category",
               "buyer.name": "buyer_name", "buyer.id": "buyer_id", "tender.buyer_id": "buyer_id", "tender.buyer_name": "buyer_name",
               "tender.title": "title", "tender.description": "description"}


def _award_column(path: str) -> str:
    """The awards column a path points at. Only ever one of these fixed expressions, so it is safe to put in SQL."""
    if path.endswith("value.amount"):
        return "amount"
    return "COALESCE(supplier_id, supplier_name)" if "suppliers" in path else "award_id"


def _has_documents(raw) -> bool:
    try:
        return bool(json.loads(raw or "[]"))
    except (TypeError, ValueError):            # an unreadable doc_urls cell means no usable documents, not a crash
        return False


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
        row = conn.execute(f"SELECT 1 FROM awards WHERE ocid=? AND {_award_column(path)} IS NOT NULL LIMIT 1", (ocid,)).fetchone()
        return None if row else "the cited record has no award data at that path"
    if path.startswith("tender.documents"):
        row = conn.execute("SELECT doc_urls FROM tenders WHERE ocid=?", (ocid,)).fetchone()
        return None if row and _has_documents(row["doc_urls"]) else "the cited record has no documents"
    return None


# --------------------------------------------------------------------------- language rules

_VERDICT_ALTERNATIVES = (
    # advice to the reader
    r"\b(you|we|i)\s+(should|must|ought to|need to|can)\s+(not\s+)?(bid|apply|submit|tender|go for|win)\b",
    r"\bshould\s+(not\s+)?(bid|apply)\b",
    r"\b(we|i)\s+(recommend|advise|suggest)\b",
    r"\brecommend(s|ed|ing)?\s+(that\s+)?(you|your|the supplier|suppliers|smes?|bidding|a bid)\b",
    # a ruling on the tender, the process or the buyer
    r"\b(this|the)\s+(tender|notice|award|process|procurement|buyer)\s+(is|was|appears|seems)\s+(not\s+|fully\s+)?(compliant|legal|lawful|irregular|improper|unlawful|fair|rigged|biased)\b",
    # who should win, whether to bid
    r"\bbest bidder\b", r"\bshould be awarded\b", r"\bdeserves?\b", r"\bgo for it\b", r"\bdon'?t bid\b", r"\bworth bidding\b",
)
VERDICT = re.compile("|".join(_VERDICT_ALTERNATIVES), re.I)
# Not blocked on purpose: "Bidders must submit a bid security..." or "a site visit is recommended" are facts from the tender, not advice.

ACCUSATION_WORDS = ("fraud", "corrupt", "bribe", "stole", "illegal")


def has_verdict(text: str) -> bool:
    """Briefs state facts and signals; they never tell a supplier to bid or who should win."""
    return bool(VERDICT.search(normalize(text)))


def has_accusation(text: str) -> bool:
    """Findings describe signals, not wrongdoing. Normalised first, so look-alike characters cannot slip a word through."""
    t = normalize(text).lower()
    return any(w in t for w in ACCUSATION_WORDS)


# --------------------------------------------------------------------------- the finding


class Finding(BaseModel):
    kind: FindingKind
    statement: str = Field(min_length=5, max_length=MAX_STATEMENT)
    source_ocid: str = Field(min_length=3, max_length=MAX_REF)
    field_path: str = Field(min_length=3, max_length=MAX_REF, description="e.g. tender.tenderPeriod.endDate")
    evidence: list[str] = Field(min_length=1, description="ids of the evidence items this finding rests on")
    severity: FindingSeverity = "info"  # 'signal' = worth a look, never proof

    @field_validator("statement", "source_ocid", "field_path", mode="before")
    @classmethod
    def _one_line(cls, v):
        return oneline(v) if isinstance(v, str) else v      # no newline can ever start a forged section in a brief

    @field_validator("statement")
    @classmethod
    def _no_accusations(cls, v: str) -> str:
        if has_accusation(v):
            raise ValueError("findings describe signals, not wrongdoing")
        if has_verdict(v):
            raise ValueError("findings describe facts, not verdicts or advice to bid")
        return v


# --------------------------------------------------------------------------- verification


def _why_invalid(e: ValidationError) -> str:
    """'statement: findings describe facts...' per failing field. The first line of str(e) is only '1 validation error
    for Finding', which tells whoever reads the rejection (a person, or the agent trying again) nothing."""
    parts = []
    for err in e.errors()[:3]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "finding"
        parts.append(f"{loc}: {str(err.get('msg', '')).removeprefix('Value error, ')}")
    return "; ".join(parts)[:200]


def verify_findings(raw: list[dict], conn: sqlite3.Connection) -> tuple[list[Finding], list[dict]]:
    """Return (accepted, rejected). Rejects bad schema or a source ocid not in the store."""
    accepted, rejected = [], []
    known: dict[str, bool] = {}                      # ocid -> exists, so a record is looked up once however many findings cite it

    def reject(item, reason: str) -> None:
        rejected.append({"finding": item, "reason": reason})

    for item in raw or []:
        if not isinstance(item, dict):
            reject(item, "a finding must be an object")
            continue
        try:
            f = Finding.model_validate(item)
        except ValidationError as e:
            reject(item, _why_invalid(e))
            continue
        except TypeError as e:
            reject(item, str(e)[:200])
            continue
        if not f.evidence or any(not item_id.strip() for item_id in f.evidence):
            reject(item, "a finding must cite at least one evidence item")
            continue
        if f.source_ocid not in known:
            known[f.source_ocid] = conn.execute("SELECT 1 FROM tenders WHERE ocid=?", (f.source_ocid,)).fetchone() is not None
        if not known[f.source_ocid]:
            reject(item, f"source_ocid {f.source_ocid} not found")
            continue
        why = validate_citation(conn, f.kind, f.source_ocid, f.field_path)
        if why:
            reject(item, f"invalid citation: {why}")
        else:
            accepted.append(f)
    return accepted, rejected