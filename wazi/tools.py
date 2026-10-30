"""Agent-facing tools. Three read, one acting. Every output carries its sources.
Nothing here can award a tender; nothing here can self-approve a filing.

Read tools are deterministic: the same data and arguments give byte-identical output, because file_brief replays them
and refuses a brief whose outputs differ. Every query that feeds an output list therefore has a total ordering."""
import json
import math
import re
import statistics
import uuid
from contextlib import closing
from datetime import datetime, timezone

from . import audit_log, config, policy, provenance as prov
from .audit_log import logged
from .findings import MAX_FINDINGS, verify_findings
from .store import connect
from .textsafe import oneline

DISCLAIMER = ("DRAFT prepared by an automated agent. It does not award, rank or recommend "
              "awarding any tender. Signals are prompts for review, not proof of wrongdoing.")

# --- thresholds (named so they can be reviewed and tested) ---
MIN_BENCH_AWARDS = 5          # fewer comparable awards than this: no price benchmark
BENCH_LOW_CONFIDENCE_N = 10
OUTLIER_Z = 10                # robust z beyond this is an extreme outlier and is excluded (and listed)
RANGE_Z = 2                   # within +-2 robust z of the median counts as within range
MAD_SCALE = 1.4826            # MAD -> standard-deviation equivalent for normal data
MEAN_AD_SCALE = 1.2533        # mean absolute deviation -> standard-deviation equivalent
FALLBACK_REL_SCALE = 0.05     # last-resort spread: 5% of the median
CONCENTRATION_MIN_AWARDS = 5
CONCENTRATION_SHARE = 0.5
MIN_PEER_BUYERS = 3
PEER_ABOVE_MARGIN = 0.15      # must beat the peer median by this much as well as the peer 75th percentile
THIN_HISTORY = 5              # fewer in-category awards than this triggers recovery


# --------------------------------------------------------------------------- small helpers

def _parse(ts):
    """ISO 8601 text -> timezone-aware datetime (naive values are taken as UTC), or None."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        d = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now(as_of):
    d = _parse(as_of) if as_of else datetime.now(timezone.utc)
    if d is None:
        raise ValueError(f"as_of must be an ISO date such as 2025-06-01, got {as_of!r}")
    return d


def _obj(x) -> dict:
    return x if isinstance(x, dict) else {}


def _quartiles(sorted_vals: list):
    """(p25, p75) by position in an already sorted, non-empty list. One definition for every tool."""
    n = len(sorted_vals)
    return sorted_vals[n // 4], sorted_vals[(3 * n) // 4]


def _ascii_lower(s: str) -> str:
    """Lowercases A-Z only, exactly like SQLite's lower(), so Python and SQL resolve a buyer name identically."""
    return s.encode("utf-8").lower().decode("utf-8")


def _supplier_key(sid, name):
    """One identity for a supplier in every tool: its id if recorded, else its name ignoring case and spacing."""
    sid = str(sid).strip() if sid is not None else ""
    if sid:
        return "id:" + sid
    n = " ".join(str(name or "").split()).casefold()
    return "name:" + n if n else None


def _supplier_label(sid, name):
    n = " ".join(str(name or "").split())
    return n or (str(sid).strip() if sid is not None else None) or None


# --------------------------------------------------------------------------- read tools

@logged
def get_tender_notice(ocid: str, as_of: str | None = None) -> dict:
    """Return the notice for one tender (title, buyer, method, deadline, eligibility, documents).
    Missing fields are listed under 'unknown' rather than guessed. 'as_of' (ISO date) sets the
    reference date for deadline status, which lets the demo replay closed tenders."""
    ref = _now(as_of)                              # validated up front and read once: status and as_of cannot disagree
    with closing(connect()) as conn:
        r = conn.execute("SELECT * FROM tenders WHERE ocid=?", (ocid,)).fetchone()
    if r is None:
        return {"found": False, "ocid": ocid}
    paths = {"title": "tender.title", "category": "tender.mainProcurementCategory",
             "method": "tender.procurementMethod", "deadline": "tender.tenderPeriod.endDate", "start": "tender.tenderPeriod.startDate",
             "eligibility": "tender.eligibilityCriteria", "buyer_name": "buyer.name",
             "value": "tender.value.amount", "status": "tender.status"}
    notice = {k: r[k] for k in paths if r[k] not in (None, "")}
    unknown = [k for k in paths if k not in notice]
    dl = _parse(r["deadline"])
    status = ("open" if dl >= ref else "closed") if dl else "unknown"
    try:
        docs = json.loads(r["doc_urls"] or "[]")
        docs = [u for u in docs if isinstance(u, str)] if isinstance(docs, list) else []
    except ValueError:
        docs = []
        unknown.append("documents")                # a corrupt document list is reported, not hidden
    return {"found": True, "ocid": ocid, "notice": notice, "currency": r["currency"], "synthetic": bool(r["synthetic"]),
            "buyer_id": r["buyer_id"], "documents": docs,
            "deadline_status": status, "as_of": ref.date().isoformat(),
            "unknown": unknown,
            "sources": [{"ocid": ocid, "field_path": paths[k]} for k in notice]}


@logged
def buyer_award_history(buyer: str, category: str | None = None, limit: int = 20) -> dict:
    """Past awards by one buyer (match on buyer id or name, case-insensitive), optionally one
    category. Reports supplier concentration as a signal, never as proof."""
    limit = max(1, int(limit))
    q = ("SELECT t.ocid, t.title, t.category, a.award_id, a.supplier_id, a.supplier_name, a.amount, a.currency, a.date "
         "FROM awards a JOIN tenders t ON t.ocid=a.ocid "
         "WHERE (t.buyer_id=? OR lower(t.buyer_name)=lower(?))")
    args = [buyer, buyer]
    if category:
        q += " AND t.category=?"
        args.append(category)
    # total ordering: ties on date must not make the output (and its hash) depend on storage order
    q += " ORDER BY a.date DESC, a.ocid, a.award_id, a.supplier_id, a.supplier_name"
    with closing(connect()) as conn:
        rows = conn.execute(q, args).fetchall()
    if not rows:
        return {"found": False, "buyer": buyer, "sources": []}
    # One row per (award, supplier): a joint venture appears as several rows. Count AWARDS, credit each member.
    keys = {(r["ocid"], r["award_id"]) for r in rows}
    wins: dict[str, set] = {}
    labels: dict[str, str] = {}
    for r in rows:
        k = _supplier_key(r["supplier_id"], r["supplier_name"])
        if k:
            wins.setdefault(k, set()).add((r["ocid"], r["award_id"]))
            labels.setdefault(k, _supplier_label(r["supplier_id"], r["supplier_name"]))
    with_sup = set().union(*wins.values()) if wins else set()
    top_key, top_n = min(((k, len(v)) for k, v in wins.items()), key=lambda kv: (-kv[1], kv[0])) if wins else (None, 0)
    share = round(top_n / len(with_sup), 2) if with_sup else None
    flags = []
    if len(with_sup) < len(keys):
        flags.append(f"{len(keys) - len(with_sup)} award(s) have no supplier recorded")
    if with_sup and len(with_sup) >= CONCENTRATION_MIN_AWARDS and top_n / len(with_sup) >= CONCENTRATION_SHARE:   # the real ratio, not the rounded one
        flags.append(f"signal: one supplier holds {share:.0%} of {len(with_sup)} recorded awards")
    return {"found": True, "buyer": buyer, "category": category, "n_awards": len(keys),
            "distinct_suppliers": len(wins), "top_supplier": labels.get(top_key), "top_supplier_share": share,
            "flags": flags,
            "awards": [dict(r) for r in rows[:limit]],
            "sources": [{"ocid": r["ocid"], "field_path": "awards[].suppliers[]"} for r in rows[:50]]}


@logged
def price_benchmark(category: str, amount: float, currency: str = "KES",
                    exclude_ocid: str | None = None) -> dict:
    """Compare an amount with past award values in the same category and currency.
    Uses median and MAD so documented extreme outliers do not distort the benchmark;
    excluded outliers are listed, not hidden. If more than half of the awards share one value there is
    no spread to judge outliers by: nothing is excluded and the spread falls back to the mean absolute
    deviation, then to 5% of the median (see 'spread_basis')."""
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
        raise ValueError(f"amount must be a positive number, got {amount!r}")
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT a.ocid, MAX(a.amount) AS amount FROM awards a JOIN tenders t ON t.ocid=a.ocid "
            "WHERE t.category=? AND a.currency=? AND a.amount>0 AND a.ocid IS NOT ? "
            "GROUP BY a.ocid, COALESCE(a.award_id, '') ORDER BY a.ocid, COALESCE(a.award_id, '')",
            (category, currency, exclude_ocid)).fetchall()
    vals = [(r["ocid"], r["amount"]) for r in rows]
    if len(vals) < MIN_BENCH_AWARDS:
        return {"category": category, "currency": currency, "n_used": len(vals),
                "low_confidence": True, "position": "insufficient_data", "sources": []}
    med = statistics.median(v for _, v in vals)
    mad = statistics.median(abs(v - med) for _, v in vals) * MAD_SCALE
    if mad > 0:
        kept = [(o, v) for o, v in vals if abs((v - med) / mad) <= OUTLIER_Z]
        dropped = [{"ocid": o, "amount": v} for o, v in vals if abs((v - med) / mad) > OUTLIER_Z]
    else:
        kept, dropped = list(vals), []
    kv = sorted(v for _, v in kept)
    kmed = statistics.median(kv)
    kmad = statistics.median(abs(v - kmed) for v in kv) * MAD_SCALE
    if kmad > 0:
        scale, basis = kmad, "mad"
    else:
        mean_ad = statistics.fmean(abs(v - kmed) for v in kv) * MEAN_AD_SCALE
        scale, basis = (mean_ad, "mean_abs_dev") if mean_ad > 0 else (kmed * FALLBACK_REL_SCALE, "relative_5pct")
    rz = round((amount - kmed) / scale, 2)
    pos = "within_range" if abs(rz) <= RANGE_Z else ("above_range" if rz > 0 else "below_range")
    p25, p75 = _quartiles(kv)
    return {"category": category, "currency": currency, "amount": amount, "n_used": len(kv),
            "n_excluded_outliers": len(dropped), "excluded_outliers": dropped[:10],
            "median": kmed, "p25": p25, "p75": p75,
            "ratio_to_median": round(amount / kmed, 2), "robust_z": rz, "spread_basis": basis, "position": pos,
            "low_confidence": len(kv) < BENCH_LOW_CONFIDENCE_N,
            "sources": [{"ocid": o, "field_path": "awards[].value.amount"} for o, _ in kept[:50]]}


@logged
def window_baseline(ocid: str, min_peers: int = 5) -> dict:
    """How long was this tender open for bids, compared with comparable tenders (same method and category,
    else same method, else same category)? Per ADR 001, we cite the Act's prescribed minimum (s.97) before peer comparison.
    PPADA Act Section 97: minimum tender periods are prescribed in regulations (7 days for open tenders per regs 86, 87, 89(7), 114, 119).
    NOTE: 2020 Regulations are under legal challenge (*RCECA v AG*), so the 7-day minimum is regulation-dependent."""
    min_peers = max(1, int(min_peers))
    with closing(connect()) as conn:
        t = conn.execute("SELECT ocid, start, deadline, method, category FROM tenders WHERE ocid=?", (ocid,)).fetchone()
        if t is None:
            return {"found": False, "ocid": ocid}
        base = {"found": True, "ocid": ocid, "method": t["method"], "category": t["category"]}
        s, e = _parse(t["start"]), _parse(t["deadline"])
        if not s or not e:
            return {**base, "position": "window_unknown", "sources": [],
                    "reason": "start date not recorded" if not s else "deadline not recorded"}
        days = (e - s).days
        srcs = [{"ocid": ocid, "field_path": "tender.tenderPeriod.startDate"}, {"ocid": ocid, "field_path": "tender.tenderPeriod.endDate"}]
        if days < 0:
            return {**base, "window_days": days, "position": "window_invalid", "reason": "start date is after the deadline", "sources": srcs}

        # Per ADR 001: cite Act's s.97 prescribed minimum (regulation-dependent, validity contested)
        # Regulations 86, 87, 89(7), 114, 119 set 7-day minimum for open tenders
        legal_minimum_note = " (PPADA Act Section 97 prescribes minimum tender periods in regulations; 2020 Regulations under legal challenge, so 7-day minimum is regulation-dependent per ADR 001)"
        if days < 7:
            base["legal_minimum_note"] = f"Bidding window is {days} days - below 7-day minimum per regulations (regulation-dependent, validity contested per ADR 001)"
        else:
            base["legal_minimum_note"] = f"Bidding window is {days} days (PPADA Act Section 97 prescribes minimum periods in regulations; 7-day minimum for open tenders per regulations 86, 87, 89(7), 114, 119 - regulation-dependent, validity contested per ADR 001)"

        groups = []
        if t["method"] and t["category"]:
            groups.append((f"same method and category ({t['method']}, {t['category']})", lambda r: r["method"] == t["method"] and r["category"] == t["category"]))
        if t["method"]:
            groups.append((f"same method ({t['method']})", lambda r: r["method"] == t["method"]))
        if t["category"]:
            groups.append((f"same category ({t['category']})", lambda r: r["category"] == t["category"]))
        pool = []
        if groups:    # only tenders that could be peers in some group, with a usable window: not the whole table
            cond, args = [], [ocid]
            for col in ("method", "category"):
                if t[col]:
                    cond.append(f"{col}=?")
                    args.append(t[col])
            for r in conn.execute("SELECT ocid, start, deadline, method, category FROM tenders "
                                  "WHERE ocid<>? AND start IS NOT NULL AND deadline IS NOT NULL AND (" + " OR ".join(cond) + ")", args):
                a, b = _parse(r["start"]), _parse(r["deadline"])
                if a and b and (b - a).days >= 0:
                    pool.append((r, (b - a).days))
    for label, ok in groups:
        peers = sorted((d, r["ocid"]) for r, d in pool if ok(r))
        if len(peers) >= min_peers:
            w = [d for d, _ in peers]
            med = statistics.median(w)
            p25, p75 = _quartiles(w)
            short = days < p25 and days <= 0.75 * med
            long_ = days > p75 and days >= 1.25 * med
            return {**base, "window_days": days, "n_peers": len(w), "peer_group": label, "peer_median_days": med,
                    "peer_p25_days": p25, "peer_p75_days": p75, "low_confidence": len(w) < 10,
                    "position": "shorter_than_peers" if short else ("longer_than_peers" if long_ else "typical"),
                    "sources": srcs + [{"ocid": o, "field_path": "tender.tenderPeriod.endDate"} for _, o in peers[:20]]}
    return {**base, "window_days": days, "position": "insufficient_peers", "n_peers": 0, "low_confidence": True, "sources": srcs}


@logged
def amendment_check(ocid: str) -> dict:
    """Check if a tender has multiple releases (amendments/variations) rather than being overwritten.
    In OCDS, proper amendment handling means publishing new releases, not overwriting old ones.
    This check detects whether the data shows evidence of proper amendment publication.
    NOTE: Per ADR 001, PPIP OCDS has zero amendments, so "no amendments" must not be read as "no variations happened"."""
    with closing(connect()) as conn:
        # Check if we have any indication of multiple releases
        # Since we flatten to one row per ocid, we can't directly count releases
        # But we can check for status changes and date patterns that suggest amendments
        tender = conn.execute("SELECT * FROM tenders WHERE ocid=?", (ocid,)).fetchone()
        if not tender:
            return {"found": False, "ocid": ocid, "has_amendments": False, "issues": [], "sources": []}

        issues = []
        # Per ADR 001: PPIP OCDS has zero amendments - report this transparently
        issues.append("Cannot be assessed - amendments not published in data source (PPIP OCDS has zero amendments across all tenders; this does not mean no variations occurred)")

        # Check for status changes that might indicate amendments
        # OCDS statuses: planned, planning, planned, active, cancelled, unsuccessful, withdrawn, complete
        statuses_considered_final = ("cancelled", "unsuccessful", "withdrawn", "complete")
        if tender["status"] not in statuses_considered_final:
            # Still active - could have amendments pending
            # We flag this for the human to verify if amendments exist
            issues.append(f"Tender status is '{tender['status']}' - verify if any amendments have been published (check tender document)")

        # Check award count for variations (multiple awards to same supplier = possible variation)
        awards = conn.execute("SELECT supplier_id, supplier_name, COUNT(*) as c FROM awards WHERE ocid=? GROUP BY supplier_id, supplier_name", (ocid,)).fetchall()
        for a in awards:
            if a["c"] > 1:
                issues.append(f"Multiple awards ({a['c']}) to supplier {a['supplier_name'] or a['supplier_id']} - possible contract variation")

        return {"found": True, "ocid": ocid, "has_amendments": len(issues) > 0, "issues": issues,
                "sources": [{"ocid": ocid, "field_path": "tender.status"}]}


@logged
def ppada_date_order_check(ocid: str) -> dict:
    """Check PPADA Act date order: tender close → award (14 days minimum) → contract signing.
    Section 135(3): award notification must be at least 14 days before contract signing.
    Also validates tender validity period (contract must be within validity period).
    NOTE: Per ADR 001, rules dependent on 2020 Regulations carry validity_contested flag."""
    from datetime import datetime, timezone, timedelta

    with closing(connect()) as conn:
        tender = conn.execute("SELECT * FROM tenders WHERE ocid=?", (ocid,)).fetchone()
        if not tender:
            return {"found": False, "ocid": ocid, "order_valid": False, "issues": [], "sources": [], "validity_contested": False}

        issues = []
        deadline = _parse(tender["deadline"])
        start = _parse(tender["start"])

        # Get awards for this tender
        awards = conn.execute("SELECT date FROM awards WHERE ocid=?", (ocid,)).fetchall()

        if not awards:
            issues.append("No award recorded - cannot validate date order")
            return {"found": True, "ocid": ocid, "order_valid": False, "issues": issues,
                    "sources": [{"ocid": ocid, "field_path": "awards[].date"}], "validity_contested": False}

        # Find the first award date
        award_dates = sorted([_parse(a["date"]) for a in awards if _parse(a["date"])])
        if not award_dates:
            issues.append("Award dates are invalid or missing")
            return {"found": True, "ocid": ocid, "order_valid": False, "issues": issues,
                    "sources": [{"ocid": ocid, "field_path": "awards[].date"}], "validity_contested": False}

        first_award = award_dates[0]

        # Check: tender must close before award (Act-based, not contested)
        if deadline and first_award:
            if first_award < deadline:
                issues.append(f"Award date {first_award.date()} is before tender deadline {deadline.date()} - invalid order")

        # Check: contract signing should be at least 14 days after award (Act section 135(3) - not contested)
        # We don't have contract signing date in OCDS, so we flag this as data gap
        issues.append("Contract signing date not in OCDS data - cannot validate 14-day PPADA requirement (section 135(3))")

        # Check: minimum tender periods (Regulation-dependent - validity_contested per ADR 001)
        if start and deadline:
            validity_days = (deadline - start).days
            if validity_days and validity_days < 30:
                # Regulation 86, 87, 89(7), 114, 119 set 7-day minimum for open tenders
                # But 2020 Regulations are under legal challenge (*RCECA v AG*)
                issues.append(f"Bidding window is only {validity_days} days - check if this meets PPADA minimum periods (regulation-dependent, validity contested per ADR 001)")

        return {"found": True, "ocid": ocid, "order_valid": len(issues) == 0, "issues": issues,
                "sources": [{"ocid": ocid, "field_path": "tender.tenderPeriod.endDate"}],
                "validity_contested": True}  # True because it references regulation-dependent minimum periods


@logged
def single_bidder_check(ocid: str) -> dict:
    """Check for single-bidder tenders (PPADA Act flagging requirement).
    Section 135: single-bidder situations require special procedures.
    NOTE: Per ADR 001, Act-based rules are firm; this check uses Act section 135.
    Per ADR 001: PPIP OCDS has zero tenderers, so this check may never fire on real data."""
    with closing(connect()) as conn:
        awards = conn.execute("SELECT award_id, supplier_id, supplier_name FROM awards WHERE ocid=?", (ocid,)).fetchall()
        if not awards:
            return {"found": False, "ocid": ocid, "is_single_bidder": False, "issues": [], "sources": [], "validity_contested": False}

        # Per ADR 001: If no supplier data, report transparently
        if not any(a["supplier_id"] or a["supplier_name"] for a in awards):
            return {"found": True, "ocid": ocid, "is_single_bidder": False, "n_suppliers": 0,
                    "issues": ["Cannot be assessed - tenderers (bidders) not published in data source (PPIP OCDS has zero tenderers across all tenders)"],
                    "sources": [{"ocid": ocid, "field_path": "awards[].suppliers[]"}], "validity_contested": False}

        # Count distinct suppliers
        suppliers = set()
        for a in awards:
            key = _supplier_key(a["supplier_id"], a["supplier_name"])
            if key:
                suppliers.add(key)

        issues = []
        if len(suppliers) == 1:
            supplier_name = awards[0]["supplier_name"] or awards[0]["supplier_id"]
            issues.append(f"Single-bidder tender: {supplier_name} (requires special PPADA procedures per section 135)")

        return {"found": True, "ocid": ocid, "is_single_bidder": len(suppliers) == 1, "n_suppliers": len(suppliers),
                "issues": issues, "sources": [{"ocid": ocid, "field_path": "awards[].suppliers[]"}], "validity_contested": False}


@logged
def termination_check(ocid: str) -> dict:
    """Check if contract is terminated with reason and date.
    PPADA Act requires terminations to record reason and date.
    NOTE: Per ADR 001, Act-based rules are firm; this check uses Act requirements.
    Per ADR 001: termination reason and date are not in OCDS data, so we report this as a data gap."""
    with closing(connect()) as conn:
        tender = conn.execute("SELECT status, deadline FROM tenders WHERE ocid=?", (ocid,)).fetchone()
        if not tender:
            return {"found": False, "ocid": ocid, "is_terminated": False, "issues": [], "sources": [], "validity_contested": False}

        issues = []
        status = tender["status"]

        # Check for terminated/withdrawn/cancelled status
        if status in ("terminated", "withdrawn", "cancelled"):
            issues.append(f"Contract status is '{status}' - verify that termination reason and date are recorded (PPADA Act requirement)")
            # Per ADR 001: termination reason and date are not in OCDS data - report transparently
            issues.append("Termination reason and date not in OCDS data - cannot verify PPADA compliance (ask buyer to check tender document)")

        return {"found": True, "ocid": ocid, "is_terminated": status in ("terminated", "withdrawn", "cancelled"),
                "issues": issues, "sources": [{"ocid": ocid, "field_path": "tender.status"}], "validity_contested": False}


@logged
def publication_quality_check(ocid: str) -> dict:
    """Check publication quality: missing fields per OCDS stage.
    PPIP data shows zero documents, milestones, transactions, amendments."""
    with closing(connect()) as conn:
        tender = conn.execute("SELECT * FROM tenders WHERE ocid=?", (ocid,)).fetchone()
        if not tender:
            return {"found": False, "ocid": ocid, "issues": [], "sources": []}
        
        issues = []
        
        # Check for missing documents
        try:
            docs = json.loads(tender["doc_urls"] or "[]")
            if not docs or len(docs) == 0:
                issues.append("No tender documents attached (PPIP data shows zero documents across all tenders)")
        except (ValueError, TypeError):
            issues.append("Document list is malformed")
        
        # Check for missing eligibility criteria
        if not tender["eligibility"]:
            issues.append("Eligibility criteria not recorded")
        
        # Check for missing buyer information
        if not tender["buyer_name"] and not tender["buyer_id"]:
            issues.append("Buyer information not recorded")
        
        # Check award completion
        awards = conn.execute("SELECT COUNT(*) as c FROM awards WHERE ocid=?", (ocid,)).fetchone()
        if awards["c"] == 0:
            issues.append("No award recorded")
        
        return {"found": True, "ocid": ocid, "issues": issues, "sources": [{"ocid": ocid, "field_path": "tender"}]}


@logged
def aged_items_check(ocid: str, as_of: str | None = None) -> dict:
    """Check for unusually long open tenders (aged items that may indicate process delays)."""
    from datetime import datetime, timezone, timedelta
    
    ref = _now(as_of)
    with closing(connect()) as conn:
        tender = conn.execute("SELECT * FROM tenders WHERE ocid=?", (ocid,)).fetchone()
        if not tender:
            return {"found": False, "ocid": ocid, "is_aged": False, "issues": [], "sources": []}
        
        issues = []
        deadline = _parse(tender["deadline"])
        status = tender["status"]
        
        # Check if tender is open but deadline is far in the past (aged open tender)
        if deadline and status not in ("cancelled", "unsuccessful", "withdrawn", "complete"):
            days_open = (ref - deadline).days if deadline < ref else None
            if days_open and days_open > 90:  # more than 90 days past deadline
                issues.append(f"Tender is {days_open} days past deadline but still marked as '{status}' - possible aged item")
        
        # Check if tender has been in planning/planned status for very long
        start = _parse(tender["start"])
        if start and status in ("planning", "planned"):
            days_planning = (ref - start).days
            if days_planning > 365:  # more than a year in planning
                issues.append(f"Tender has been in '{status}' status for {days_planning} days - possible aged item")
        
        return {"found": True, "ocid": ocid, "is_aged": len(issues) > 0, "issues": issues,
                "sources": [{"ocid": ocid, "field_path": "tender.status"}]}


@logged
def budget_exposure(buyer_id: str | None = None, buyer_name: str | None = None) -> dict:
    """Estimate budget exposure based on active/unpaid awards (accruals gap)."""
    issues = []
    has_id = buyer_id is not None and str(buyer_id).strip()
    has_name = buyer_name is not None and str(buyer_name).strip()
    
    if not has_id and not has_name:
        return {"found": False, "buyer_id": buyer_id, "buyer_name": buyer_name,
                "exposure_calculated": False, "issues": ["No buyer identifier provided"], "sources": []}
    
    with closing(connect()) as conn:
        key = buyer_id if has_id else buyer_name
        
        # Count awards without payment data (this is a proxy - real payment data not in OCDS)
        # We look for awards that exist but have no contract/delivery status
        awards = conn.execute(
            "SELECT COUNT(DISTINCT a.ocid) as c FROM awards a JOIN tenders t ON t.ocid=a.ocid "
            "WHERE (t.buyer_id=? OR lower(t.buyer_name)=lower(?))",
            (key, key)).fetchone()
        
        total_awards = awards["c"]
        
        # In OCDS, we don't have payment data, so we estimate exposure based on award amounts
        # for awards that don't have contract completion status
        amounts = conn.execute(
            "SELECT a.amount, a.currency FROM awards a JOIN tenders t ON t.ocid=a.ocid "
            "WHERE (t.buyer_id=? OR lower(t.buyer_name)=lower(?)) AND a.amount IS NOT NULL AND a.amount > 0",
            (key, key)).fetchall()
        
        total_value = sum(a["amount"] for a in amounts)
        
        if total_awards > 0:
            issues.append(f"Buyer has {total_awards} recorded awards with total value {total_value:,.0f} KES (payment status unknown in OCDS)")
        
        return {"found": True, "buyer_id": buyer_id, "buyer_name": buyer_name,
                "exposure_calculated": True, "total_awards": total_awards, "total_value": total_value,
                "issues": issues, "sources": [{"ocid": "*", "field_path": "awards[].value.amount"}]}


@logged
def counterparty_quality(buyer_id: str | None = None, buyer_name: str | None = None) -> dict:
    """Check buyer data quality before analysis.
    Checks: buyer ID/name consistency, whether buyer has sufficient award history,
    and flags data quality issues that would affect reliability of downstream analysis."""
    issues = []
    has_id = buyer_id is not None and str(buyer_id).strip()
    has_name = buyer_name is not None and str(buyer_name).strip()
    
    if not has_id and not has_name:
        return {"found": False, "buyer_id": buyer_id, "buyer_name": buyer_name,
                "ready": False, "issues": ["No buyer identifier (ID or name) provided"],
                "sources": []}
    
    with closing(connect()) as conn:
        # Check if buyer exists in tenders table
        if has_id:
            by_id = conn.execute("SELECT COUNT(*) as c FROM tenders WHERE buyer_id=?", (buyer_id,)).fetchone()
            if by_id["c"] == 0:
                issues.append(f"Buyer ID {buyer_id} not found in any tender record")
        if has_name:
            by_name = conn.execute("SELECT COUNT(*) as c FROM tenders WHERE lower(buyer_name)=lower(?)",
                                    (buyer_name,)).fetchone()
            if by_name["c"] == 0:
                issues.append(f"Buyer name '{buyer_name}' not found in any tender record")
        
        # Check consistency: if both provided, do they point to same records?
        if has_id and has_name:
            both = conn.execute(
                "SELECT COUNT(*) as c FROM tenders WHERE buyer_id=? AND lower(buyer_name)=lower(?)",
                (buyer_id, buyer_name)).fetchone()
            if both["c"] == 0:
                issues.append(f"Buyer ID {buyer_id} and name '{buyer_name}' do not match in any record")
        
        # Check award history depth
        key = buyer_id if has_id else buyer_name
        history_rows = conn.execute(
            "SELECT COUNT(DISTINCT a.ocid) as c FROM awards a JOIN tenders t ON t.ocid=a.ocid "
            "WHERE (t.buyer_id=? OR lower(t.buyer_name)=lower(?))",
            (key, key)).fetchone()
        award_count = history_rows["c"]
        
        if award_count < THIN_HISTORY:
            issues.append(f"Insufficient award history ({award_count} awards) for reliable analysis")
        
        # Check for supplier ID/name inconsistencies in this buyer's awards
        supp_issues = conn.execute(
            "SELECT COUNT(DISTINCT a.supplier_id) as ids, COUNT(DISTINCT a.supplier_name) as names "
            "FROM awards a JOIN tenders t ON t.ocid=a.ocid "
            "WHERE (t.buyer_id=? OR lower(t.buyer_name)=lower(?)) AND a.supplier_id IS NOT NULL AND a.supplier_name IS NOT NULL",
            (key, key)).fetchone()
        if supp_issues["ids"] != supp_issues["names"]:
            issues.append(f"Supplier data inconsistency: {supp_issues['ids']} distinct IDs but {supp_issues['names']} distinct names")
        
        ready = len(issues) == 0
        sources = [{"ocid": "*", "field_path": "buyer.id"}, {"ocid": "*", "field_path": "buyer.name"},
                    {"ocid": "*", "field_path": "awards[].suppliers[]"}]

        return {"found": True, "buyer_id": buyer_id, "buyer_name": buyer_name,
                "ready": ready, "issues": issues, "award_count": award_count,
                "sources": sources}


@logged
def process_timeline_check(ocid: str) -> dict:
    """Check for process timeline anomalies using available dates.
    This is a proxy for evaluation time limit checks - we check the gap between
    tender deadline and award date (decision window). Per ADR 001, we cite the
    Act's 30-day evaluation limit (s.80(6)) explicitly before peer comparison.
    PPADA Act Section 80(6): evaluation must not exceed 30 days."""
    from datetime import datetime, timezone, timedelta

    with closing(connect()) as conn:
        tender = conn.execute("SELECT ocid, start, deadline FROM tenders WHERE ocid=?", (ocid,)).fetchone()
        if not tender:
            return {"found": False, "ocid": ocid, "issues": [], "sources": []}

        issues = []
        deadline = _parse(tender["deadline"])
        start = _parse(tender["start"])

        # Get award dates
        awards = conn.execute("SELECT date FROM awards WHERE ocid=?", (ocid,)).fetchall()
        if not awards:
            issues.append("No award recorded - cannot assess decision timeline")
            return {"found": True, "ocid": ocid, "decision_window_days": None,
                    "issues": issues, "sources": [{"ocid": ocid, "field_path": "awards[].date"}]}

        # Find the first award date
        award_dates = sorted([_parse(a["date"]) for a in awards if _parse(a["date"])])
        if not award_dates:
            issues.append("Award dates are invalid or missing")
            return {"found": True, "ocid": ocid, "decision_window_days": None,
                    "issues": issues, "sources": [{"ocid": ocid, "field_path": "awards[].date"}]}

        first_award = award_dates[0]

        # Calculate decision window (deadline to first award)
        decision_window = None
        if deadline and first_award:
            if first_award >= deadline:
                decision_window = (first_award - deadline).days
            else:
                # Award before deadline is already caught by ppada_date_order_check
                decision_window = None

        # Per ADR 001: cite Act's 30-day evaluation limit (s.80(6)) explicitly
        if decision_window:
            if decision_window > 30:
                issues.append(f"Decision window is {decision_window} days from deadline to award - exceeds PPADA Act Section 80(6) 30-day evaluation limit")
            else:
                # Still report as fact, even if within limit
                issues.append(f"Decision window is {decision_window} days from deadline to award (PPADA Act Section 80(6) specifies 30-day evaluation limit)")

        sources = [{"ocid": ocid, "field_path": "tender.tenderPeriod.endDate"},
                   {"ocid": ocid, "field_path": "awards[].date"}]

        return {"found": True, "ocid": ocid, "decision_window_days": decision_window,
                "issues": issues, "sources": sources}


@logged
def peer_baseline(buyer: str, category: str, min_awards: int = 5) -> dict:
    """Test a supplier-concentration signal against comparable buyers: how concentrated are OTHER buyers
    (with at least min_awards recorded awards) in the same category? Small buyers and niche categories are
    naturally concentrated, so a signal that matches its peers is downgraded, not escalated.
    The buyer is resolved exactly as in buyer_award_history (id or case-insensitive name, all matching ids together),
    and suppliers are identified the same way, so the two tools describe the same buyer."""
    min_awards = max(1, int(min_awards))
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT t.buyer_id AS b, t.buyer_name AS n, a.ocid AS ocid, a.award_id AS aid, a.supplier_id AS sid, a.supplier_name AS sname "
            "FROM awards a JOIN tenders t ON t.ocid=a.ocid "
            "WHERE t.category=? AND (t.buyer_id IS NOT NULL OR t.buyer_name IS NOT NULL) "
            "ORDER BY a.ocid, a.award_id, a.supplier_id, a.supplier_name",
            (category,)).fetchall()
    by_buyer: dict = {}
    for r in rows:  # award-level: a joint venture credits every member but counts as ONE award
        sk = _supplier_key(r["sid"], r["sname"])
        if sk is None:
            continue
        name = _ascii_lower(r["n"]) if r["n"] else None
        group = r["b"] if r["b"] is not None else f"name:{name}"
        d = by_buyer.setdefault(group, {"names": set(), "sup": {}, "keys": set(), "ocids": set()})
        if name:
            d["names"].add(name)
        k = (r["ocid"], r["aid"])
        d["sup"].setdefault(sk, set()).add(k)
        d["keys"].add(k)
        d["ocids"].add(r["ocid"])
    want = _ascii_lower(buyer)
    target_groups = [g for g, d in by_buyer.items() if g == buyer or want in d["names"]]
    tgt = {"sup": {}, "keys": set(), "ocids": set()}
    for g in target_groups:
        d = by_buyer[g]
        for sk, ks in d["sup"].items():
            tgt["sup"].setdefault(sk, set()).update(ks)
        tgt["keys"] |= d["keys"]
        tgt["ocids"] |= d["ocids"]
    share = lambda d: max(len(v) for v in d["sup"].values()) / len(d["keys"])
    if not target_groups or len(tgt["keys"]) < min_awards:
        return {"found": False, "buyer": buyer, "category": category, "position": "insufficient_target", "sources": []}
    peers = sorted(share(d) for g, d in by_buyer.items() if g not in target_groups and len(d["keys"]) >= min_awards)
    out = {"found": True, "buyer": buyer, "category": category, "buyer_share": round(share(tgt), 3),
           "buyer_awards": len(tgt["keys"]), "n_peers": len(peers),
           "sources": [{"ocid": o, "field_path": "awards[].suppliers[]"} for o in sorted(tgt["ocids"])[:20]]}
    if len(peers) < MIN_PEER_BUYERS:
        return {**out, "position": "insufficient_peers", "low_confidence": True}
    med = statistics.median(peers)
    _, p75 = _quartiles(peers)
    above = out["buyer_share"] > p75 and out["buyer_share"] - med >= PEER_ABOVE_MARGIN
    return {**out, "peer_median_share": round(med, 3), "peer_p75_share": round(p75, 3),
            "position": "above_peers" if above else "typical_for_peers", "low_confidence": len(peers) < 5}


# --------------------------------------------------------------------------- rendering

# Reordered for buyer-side workflow per ADR 001: paperwork and process readiness first
SECTIONS = [("process_readiness", "Paperwork and process readiness"),
            ("deadline", "Timing"),
            ("eligibility", "Eligibility and paperwork"),
            ("price", "Price position"),
            ("buyer_history", "Buyer behaviour (read with care)"),
            ("other", "Tender details and documents"),
            ("data_quality", "Gaps and data limits")]
LABEL = {"info": "Note", "check": "Check", "signal": "Signal (worth a look, not proof)"}
ORDER = {"signal": 0, "check": 1, "info": 2}
MISSING = {"deadline": "Confirm the submission deadline with the buyer (not recorded in the notice).",
           "eligibility": "Get the eligibility and mandatory-document list from the tender document (not recorded in the notice).",
           "buyer_name": "Confirm who the buyer is (name not recorded).",
           "value": "Ask for the budget or estimated value (not recorded).",
           "category": "Confirm whether this is goods, works or services (not recorded).",
           "method": "Confirm the procurement method (not recorded)."}
NOT_COVERED = ("What this brief does not cover (check your own records or ask the buyer): delivery lead time and location, "
               "payment terms and the buyer's payment record, how many bidders to expect, and any bid security or tender fee not stated above.")
VERIFY_ORDER = ("deadline", "start", "eligibility", "buyer_name", "value", "category", "method")


AUDIENCE = {  # same facts, different reader. 'buyer' = the procurement manager who signs off; 'supplier' = a small firm deciding whether to bid
 "buyer": {"titles": {"eligibility": "Notice and eligibility criteria as published", "price": "Your estimate against past awards",
                      "buyer_history": "Your award history and exposure (read with care)"},
           "verify_title": "Before you sign off: what to fix or confirm",
           "verify_empty": "Nothing is missing from the published record; still confirm it against the tender file.",
           "missing": {"deadline": "Add the closing date to the published notice (it is not recorded).",
                       "start": "Record the date the tender opened so the bidding window can be assessed (not recorded).",
                       "eligibility": "Publish the eligibility criteria and mandatory documents in the notice (not recorded).",
                       "buyer_name": "Record your entity's name on the notice (not recorded).",
                       "value": "Record the estimated value or budget range (not recorded).",
                       "category": "Record whether this is goods, works or services (not recorded).",
                       "method": "Record the procurement method (not recorded)."},
           "not_covered": "What this brief does not cover (check separately): legal and regulatory compliance, quality of the evaluation criteria, "
                          "conflicts of interest, how many bids to expect, budget availability, and contract performance."},
 "supplier": {"titles": {}, "verify_title": "Before you commit: what to verify",
              "verify_empty": "Nothing in the tender record is missing, but confirm the details against the tender document.",
              "missing": {**MISSING, "start": "Confirm when the tender opened (not recorded), so you know how long the buyer allowed for bids."},
              "not_covered": NOT_COVERED}}

_MD_LINK = re.compile(r"\]\(")


def _md(x) -> str:
    """Untrusted text as one line that cannot open an HTML tag, an autolink or an inline link in the rendered brief."""
    return _MD_LINK.sub("]\\(", oneline(x).replace("<", "&lt;").replace(">", "&gt;"))


def _window_text(start, deadline):
    a, b = _parse(start), _parse(deadline)
    if not a or not b:
        return "not recorded"
    d = (b - a).days
    return f"{d} day(s), opened {a.date().isoformat()}" if d >= 0 else "not assessable (start date is after the deadline)"


def _timing(deadline, as_of):
    dl = _parse(deadline)
    if not dl:
        return "not recorded"
    ref = _now(as_of)
    when = dl.date().isoformat()
    if dl >= ref:
        return f"{when} (open: {(dl - ref).days} day(s) left as of {ref.date().isoformat()})"
    return f"{when} (closed {(ref - dl).days} day(s) before the reference date {ref.date().isoformat()})"   # (ref - dl), not abs of a floored negative


def _render(ocid, summary, accepted, rejected_n, fid, row=None, as_of=None, audience="buyer", footer=""):
    A = AUDIENCE[audience]
    g = lambda k: (row[k] if row is not None and row[k] not in (None, "") else None)
    val = f"{g('value'):,.0f} {g('currency') or ''}".strip() if g("value") else "not recorded"
    glance = [("Buyer", g("buyer_name") or (f"{g('buyer_id')} (name not recorded)" if g("buyer_id") else "not recorded")),
              ("Category", g("category") or "not recorded"), ("Procurement method", g("method") or "not recorded"),
              ("Estimated value", val), ("Deadline", _timing(g("deadline"), as_of)), ("Bidding window", _window_text(g("start"), g("deadline")))]
    cell = lambda x: _md(x).replace("|", "\\|")      # untrusted text can never break a table cell or start a new line/section
    lines = [f"# Tender readiness brief: {_md(g('title') or ocid)}", f"`{oneline(ocid).replace('`', chr(39))}`  |  Filing `{fid}`  |  **pending human approval**", "",
             f"> {DISCLAIMER}", "", "## At a glance", "| | |", "|---|---|"] + [f"| {k} | {cell(v)} |" for k, v in glance]

    # Three-tier gap classification per ADR 001
    blockers = []  # Missing deadline, eligibility, required documents, tender security, buyer identity
    must_confirm = []  # Missing start date, payment terms
    known_gaps = []  # Thin history, low-confidence benchmark

    # Count data_quality findings as gaps
    for f in accepted:
        if f.kind == "data_quality":
            stmt = f.statement.lower()
            if any(x in stmt for x in ["deadline", "deadline not recorded", "missing deadline"]):
                blockers.append(f)
            elif any(x in stmt for x in ["start date", "start not recorded"]):
                must_confirm.append(f)
            elif any(x in stmt for x in ["thin history", "low confidence", "few awards"]):
                known_gaps.append(f)
            elif any(x in stmt for x in ["buyer name", "buyer identity", "buyer not recorded"]):
                blockers.append(f)
            elif any(x in stmt for x in ["eligibility", "required documents"]):
                blockers.append(f)

    # Add confidence banner based on gap counts
    if blockers:
        lines += ["", "### ⚠️ LOW CONFIDENCE - CRITICAL DATA MISSING",
                  f"_This brief has {len(blockers)} critical gap(s) that must be addressed before proceeding._"]
    elif must_confirm and len(must_confirm) > 2:
        lines += ["", "### ⚠️ MEDIUM CONFIDENCE - REQUIRES VERIFICATION",
                  f"_This brief has {len(must_confirm)} item(s) that must be confirmed with the buyer._"]
    elif known_gaps and len(known_gaps) > 5:
        lines += ["", "### ⚠️ MEDIUM CONFIDENCE - LIMITED HISTORY",
                  f"_This brief has {len(known_gaps)} gap(s) due to limited historical data._"]

    lines += ["", "## Summary", _md(oneline(summary)[:1500])]
    for kind, title in SECTIONS:
        title = A["titles"].get(kind, title)
        items = sorted((f for f in accepted if f.kind == kind), key=lambda f: ORDER.get(f.severity, len(ORDER)))
        if items:
            lines += ["", f"## {title}"] + [f"- **{LABEL.get(f.severity, 'Note')}:** {_md(f.statement)}  \n  _Source: {_md(f.source_ocid)} / {_md(f.field_path)}_" for f in items]
    verify = [A["missing"][k] for k in VERIFY_ORDER if not g(k)]
    lines += ["", f"## {A['verify_title']}"] + [f"- {v}" for v in (verify or [A["verify_empty"]])]
    lines += ["", f"_{A['not_covered']}_"]
    if footer:
        lines += ["", f"_{footer}_"]
    if rejected_n:
        lines += ["", f"_{rejected_n} finding(s) were dropped for failing validation (missing source, invalid evidence, or verdict language)._"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- the one acting tool

def _write_new(path, text: str) -> None:
    with open(path, "x", encoding="utf-8") as f:     # exclusive: a filed brief is never overwritten
        f.write(text)


@logged
def file_brief(ocid: str, summary: str, findings: list[dict], as_of: str | None = None, audience: str = "buyer",
               provenance: dict | None = None) -> dict:
    """Queue a sourced brief for human approval. Findings lacking a valid, real citation are dropped.
    With provenance (the agent's recorded tool calls, evidence, documents and model calls) every read-tool call is REPLAYED here
    and the brief is refused if any output differs: the agent cannot file work the data does not reproduce.
    A hash-linked manifest is stored with the brief. This does NOT file anything final: a named human must approve it."""
    _now(as_of)                                      # an invalid as_of is an error before anything else happens
    with closing(connect()) as conn:
        return _file_brief(conn, ocid, summary, findings, as_of, audience, provenance)


def _file_brief(conn, ocid, summary, findings, as_of, audience, provenance) -> dict:
    def refuse(reason, rejected=None):
        return {"status": "not_filed", "reason": reason, "rejected": rejected or []}

    if audience not in AUDIENCE:
        return refuse(f"unknown audience {audience!r}; use buyer or supplier")
    row = conn.execute("SELECT * FROM tenders WHERE ocid=?", (ocid,)).fetchone()
    if row is None:
        return refuse(f"no such tender {ocid!r}")
    live = policy.live_reason(row["status"], row["deadline"], row["synthetic"])
    if live and not policy.allowed():       # second line of defence: enforced where the brief is filed, whatever the agent did
        return refuse(f"live tender: {live}. Briefs are only prepared for closed tenders and synthetic data.")
    if not isinstance(findings, list):
        return refuse("findings must be a list")
    if provenance is not None and not isinstance(provenance, dict):
        return refuse("provenance must be an object")
    overflow = max(0, len(findings) - MAX_FINDINGS)
    accepted, rejected = verify_findings(findings[:MAX_FINDINGS], conn)
    dropped = len(rejected) + overflow               # findings dropped, counting every overflowing one
    if overflow:
        rejected.append({"finding": f"{overflow} further finding(s)", "reason": f"too many findings (maximum {MAX_FINDINGS})"})
    if not accepted:
        return refuse("no findings with a valid source", rejected)
    if provenance:
        mism = prov.replay_mismatches(provenance.get("tool_calls", []))
        if mism:
            return refuse("provenance does not replay: " + "; ".join(mism[:3]), rejected)

    ds, cv, run_id = prov.dataset(conn), prov.code_version(), audit_log.current_run_id()
    llm = _obj(_obj((provenance or {}).get("run")).get("llm"))
    where = "REMOTE endpoint: prompts left this machine" if _obj(llm.get("settings")).get("remote") else "local"
    footer = ((f"LIVE TENDER OVERRIDE (WAZI_ALLOW_LIVE=1, development only) | " if live else "") + f"Run {run_id} | data {ds['fingerprint']} | code {cv} | "
              + (f"model {llm.get('model') or llm.get('name')} ({where}) | " if llm else "")
              + ("provenance recorded and replay-checked" if provenance else "NO provenance supplied: this brief cannot be independently reproduced"))

    base = config.OUTBOX / "pending"
    base.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        for _ in range(5):                           # a filing id is never reused, even on a (very unlikely) collision
            fid = uuid.uuid4().hex[:10]
            md = _render(ocid, summary, accepted, dropped, fid, row, as_of, audience, footer)
            try:
                _write_new(base / f"{fid}.md", md)
                created.append(base / f"{fid}.md")
                break
            except FileExistsError:
                continue
        else:
            return refuse("could not allocate a free filing id")
        md_sha = prov.sha_file(base / f"{fid}.md")
        manifest = prov.make_manifest(fid=fid, run_id=run_id, ocid=ocid, audience=audience, as_of=as_of, md_sha256=md_sha, ds=ds,
                                      accepted=accepted, provenance=provenance, replay_ok=True)
        _write_new(base / f"{fid}.manifest.json", json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False))
        created.append(base / f"{fid}.manifest.json")
        man_sha = prov.sha_file(base / f"{fid}.manifest.json")
        _write_new(base / f"{fid}.json", json.dumps(
            {"filing_id": fid, "run_id": run_id, "ocid": ocid, "audience": audience, "as_of": as_of, "created": datetime.now(timezone.utc).isoformat(),
             "md_sha256": md_sha, "manifest_sha256": man_sha, "dataset": ds["fingerprint"], "code_version": cv,
             "findings": [f.model_dump() for f in accepted]}, indent=2))
        created.append(base / f"{fid}.json")
    except BaseException:
        for p in created:                            # never leave half a filing in the pending folder
            p.unlink(missing_ok=True)
        raise
    return {"status": "pending_approval", "filing_id": fid, "accepted": len(accepted), "run_id": run_id,
            "md_sha256": md_sha, "manifest_sha256": man_sha, "rejected": rejected, "path": f"outbox/pending/{fid}.md"}