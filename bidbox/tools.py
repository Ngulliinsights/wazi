"""Agent-facing tools. Three read, one acting. Every output carries its sources.
Nothing here can award a tender; nothing here can self-approve a filing."""
import json
import statistics
import uuid
from datetime import datetime, timezone

from . import config
from . import audit_log, policy, provenance as prov
from .audit_log import logged
from .findings import MAX_FINDINGS, verify_findings
from .textsafe import oneline
from .store import connect

DISCLAIMER = ("DRAFT prepared by an automated agent. It does not award, rank or recommend "
              "awarding any tender. Signals are prompts for review, not proof of wrongdoing.")


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now(as_of):
    d = _parse(as_of) if as_of else datetime.now(timezone.utc)
    if d is None:
        raise ValueError(f"as_of must be an ISO date such as 2025-06-01, got {as_of!r}")
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@logged
def get_tender_notice(ocid: str, as_of: str | None = None) -> dict:
    """Return the notice for one tender (title, buyer, method, deadline, eligibility, documents).
    Missing fields are listed under 'unknown' rather than guessed. 'as_of' (ISO date) sets the
    reference date for deadline status, which lets the demo replay closed tenders."""
    conn = connect()
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
    status = "unknown"
    if dl:
        dl = dl if dl.tzinfo else dl.replace(tzinfo=timezone.utc)
        status = "open" if dl >= _now(as_of) else "closed"
    return {"found": True, "ocid": ocid, "notice": notice, "currency": r["currency"], "synthetic": bool(r["synthetic"]),
            "buyer_id": r["buyer_id"], "documents": json.loads(r["doc_urls"] or "[]"),
            "deadline_status": status, "as_of": _now(as_of).date().isoformat(),
            "unknown": unknown,
            "sources": [{"ocid": ocid, "field_path": paths[k]} for k in notice]}


@logged
def buyer_award_history(buyer: str, category: str | None = None, limit: int = 20) -> dict:
    """Past awards by one buyer (match on buyer id or name, case-insensitive), optionally one
    category. Reports supplier concentration as a signal, never as proof."""
    conn = connect()
    q = ("SELECT t.ocid, t.title, t.category, a.award_id, a.supplier_id, a.supplier_name, a.amount, a.currency, a.date "
         "FROM awards a JOIN tenders t ON t.ocid=a.ocid "
         "WHERE (t.buyer_id=? OR lower(t.buyer_name)=lower(?))")
    args = [buyer, buyer]
    if category:
        q += " AND t.category=?"
        args.append(category)
    rows = conn.execute(q + " ORDER BY a.date DESC", args).fetchall()
    if not rows:
        return {"found": False, "buyer": buyer, "sources": []}
    # One row per (award, supplier): a joint venture appears as several rows. Count AWARDS, credit each member.
    keys = {(r["ocid"], r["award_id"]) for r in rows}
    wins: dict = {}
    for r in rows:
        who = r["supplier_name"] or r["supplier_id"]
        if who:
            wins.setdefault(who, set()).add((r["ocid"], r["award_id"]))
    with_sup = set().union(*wins.values()) if wins else set()
    top, top_n = max(((k, len(v)) for k, v in wins.items()), key=lambda kv: kv[1]) if wins else (None, 0)
    share = round(top_n / len(with_sup), 2) if with_sup else None
    flags = []
    if len(with_sup) < len(keys):
        flags.append(f"{len(keys) - len(with_sup)} award(s) have no supplier recorded")
    if share is not None and len(with_sup) >= 5 and share >= 0.5:
        flags.append(f"signal: one supplier holds {share:.0%} of {len(with_sup)} recorded awards")
    return {"found": True, "buyer": buyer, "category": category, "n_awards": len(keys),
            "distinct_suppliers": len(wins), "top_supplier": top, "top_supplier_share": share,
            "flags": flags,
            "awards": [dict(r) for r in rows[:limit]],
            "sources": [{"ocid": r["ocid"], "field_path": "awards[].suppliers[]"} for r in rows[:limit]]}


@logged
def price_benchmark(category: str, amount: float, currency: str = "KES",
                    exclude_ocid: str | None = None) -> dict:
    """Compare an amount with past award values in the same category and currency.
    Uses median and MAD so documented extreme outliers do not distort the benchmark;
    excluded outliers are listed, not hidden."""
    conn = connect()
    rows = conn.execute(
        "SELECT a.ocid, MAX(a.amount) AS amount FROM awards a JOIN tenders t ON t.ocid=a.ocid "
        "WHERE t.category=? AND a.currency=? AND a.amount>0 AND a.ocid IS NOT ? GROUP BY a.ocid, COALESCE(a.award_id, '')",
        (category, currency, exclude_ocid)).fetchall()
    vals = [(r["ocid"], r["amount"]) for r in rows]
    if len(vals) < 5:
        return {"category": category, "currency": currency, "n_used": len(vals),
                "low_confidence": True, "position": "insufficient_data", "sources": []}
    med = statistics.median(v for _, v in vals)
    mad = statistics.median(abs(v - med) for _, v in vals) * 1.4826
    def z(v): return (v - med) / mad if mad else (0 if v == med else float("inf") * (1 if v > med else -1))
    kept = [(o, v) for o, v in vals if abs(z(v)) <= 10]
    dropped = [{"ocid": o, "amount": v} for o, v in vals if abs(z(v)) > 10]
    kv = sorted(v for _, v in kept)
    kmed = statistics.median(kv)
    kmad = statistics.median(abs(v - kmed) for v in kv) * 1.4826 or 1.0
    rz = round((amount - kmed) / kmad, 2)
    pos = "within_range" if abs(rz) <= 2 else ("above_range" if rz > 0 else "below_range")
    return {"category": category, "currency": currency, "amount": amount, "n_used": len(kv),
            "n_excluded_outliers": len(dropped), "excluded_outliers": dropped[:10],
            "median": kmed, "p25": kv[len(kv) // 4], "p75": kv[(3 * len(kv)) // 4],
            "ratio_to_median": round(amount / kmed, 2), "robust_z": rz, "position": pos,
            "low_confidence": len(kv) < 10,
            "sources": [{"ocid": o, "field_path": "awards[].value.amount"} for o, _ in kept[:10]]}


def _utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@logged
def window_baseline(ocid: str, min_peers: int = 5) -> dict:
    """How long was this tender open for bids, compared with comparable tenders (same method and category,
    else same method, else same category)? No legal minimum is assumed or invented: short windows can be
    legitimate (urgent needs), so this reports where the window sits among its peers and nothing more."""
    conn = connect()
    t = conn.execute("SELECT ocid, start, deadline, method, category FROM tenders WHERE ocid=?", (ocid,)).fetchone()
    if t is None:
        return {"found": False, "ocid": ocid}
    base = {"found": True, "ocid": ocid, "method": t["method"], "category": t["category"]}
    s, e = _parse(t["start"]), _parse(t["deadline"])
    if not s or not e:
        return {**base, "position": "window_unknown", "sources": [],
                "reason": "start date not recorded" if not s else "deadline not recorded"}
    days = (_utc(e) - _utc(s)).days
    srcs = [{"ocid": ocid, "field_path": "tender.tenderPeriod.startDate"}, {"ocid": ocid, "field_path": "tender.tenderPeriod.endDate"}]
    if days < 0:
        return {**base, "window_days": days, "position": "window_invalid", "reason": "start date is after the deadline", "sources": srcs}
    pool = []
    for r in conn.execute("SELECT ocid, start, deadline, method, category FROM tenders WHERE ocid<>?", (ocid,)):
        a, b = _parse(r["start"]), _parse(r["deadline"])
        if a and b and (_utc(b) - _utc(a)).days >= 0:
            pool.append((r, (_utc(b) - _utc(a)).days))
    groups = []
    if t["method"] and t["category"]:
        groups.append((f"same method and category ({t['method']}, {t['category']})", lambda r: r["method"] == t["method"] and r["category"] == t["category"]))
    if t["method"]:
        groups.append((f"same method ({t['method']})", lambda r: r["method"] == t["method"]))
    if t["category"]:
        groups.append((f"same category ({t['category']})", lambda r: r["category"] == t["category"]))
    for label, ok in groups:
        peers = sorted((d, r["ocid"]) for r, d in pool if ok(r))
        if len(peers) >= min_peers:
            w = [d for d, _ in peers]
            med, p25, p75 = statistics.median(w), w[len(w) // 4], w[(3 * len(w)) // 4]
            short = days < p25 and days <= 0.75 * med
            long_ = days > p75 and days >= 1.25 * med
            return {**base, "window_days": days, "n_peers": len(w), "peer_group": label, "peer_median_days": med,
                    "peer_p25_days": p25, "peer_p75_days": p75, "low_confidence": len(w) < 10,
                    "position": "shorter_than_peers" if short else ("longer_than_peers" if long_ else "typical"),
                    "sources": srcs + [{"ocid": o, "field_path": "tender.tenderPeriod.endDate"} for _, o in peers[:5]]}
    return {**base, "window_days": days, "position": "insufficient_peers", "n_peers": 0, "low_confidence": True, "sources": srcs}


@logged
def peer_baseline(buyer: str, category: str, min_awards: int = 5) -> dict:
    """Test a supplier-concentration signal against comparable buyers: how concentrated are OTHER buyers
    (with at least min_awards recorded awards) in the same category? Small buyers and niche categories are
    naturally concentrated, so a signal that matches its peers is downgraded, not escalated."""
    conn = connect()
    rows = conn.execute(
        "SELECT t.buyer_id AS b, t.buyer_name AS n, a.ocid AS ocid, a.award_id AS aid, COALESCE(a.supplier_id, a.supplier_name) AS s "
        "FROM awards a JOIN tenders t ON t.ocid=a.ocid "
        "WHERE t.category=? AND t.buyer_id IS NOT NULL AND COALESCE(a.supplier_id, a.supplier_name) IS NOT NULL",
        (category,)).fetchall()
    by_buyer: dict = {}
    for r in rows:  # award-level: a joint venture credits every member but counts as ONE award
        d = by_buyer.setdefault(r["b"], {"name": (r["n"] or "").lower(), "sup": {}, "keys": set(), "ocids": []})
        k = (r["ocid"], r["aid"])
        d["sup"].setdefault(r["s"], set()).add(k)
        if k not in d["keys"]:
            d["keys"].add(k); d["ocids"].append(r["ocid"])
    key = next((b for b, d in by_buyer.items() if b == buyer or d["name"] == buyer.lower()), None)
    share = lambda d: max(len(v) for v in d["sup"].values()) / len(d["keys"])
    if key is None or len(by_buyer[key]["keys"]) < min_awards:
        return {"found": False, "buyer": buyer, "category": category, "position": "insufficient_target", "sources": []}
    tgt = by_buyer[key]
    peers = sorted(share(d) for b, d in by_buyer.items() if b != key and len(d["keys"]) >= min_awards)
    out = {"found": True, "buyer": buyer, "category": category, "buyer_share": round(share(tgt), 3),
           "buyer_awards": len(tgt["keys"]), "n_peers": len(peers),
           "sources": [{"ocid": o, "field_path": "awards[].suppliers[]"} for o in tgt["ocids"][:5]]}
    if len(peers) < 3:
        return {**out, "position": "insufficient_peers", "low_confidence": True}
    med, p75 = statistics.median(peers), peers[(3 * len(peers)) // 4]
    above = out["buyer_share"] > p75 and out["buyer_share"] - med >= 0.15
    return {**out, "peer_median_share": round(med, 3), "peer_p75_share": round(p75, 3),
            "position": "above_peers" if above else "typical_for_peers", "low_confidence": len(peers) < 5}


SECTIONS = [("deadline", "Timing"), ("eligibility", "Eligibility and paperwork"), ("price", "Price position"),
            ("buyer_history", "Buyer behaviour (read with care)"), ("other", "Tender details and documents"),
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


def _window_text(start, deadline):
    a, b = _parse(start), _parse(deadline)
    if not a or not b:
        return "not recorded"
    d = (_utc(b) - _utc(a)).days
    return f"{d} day(s), opened {a.date().isoformat()}" if d >= 0 else "not assessable (start date is after the deadline)"


def _timing(deadline, as_of):
    dl = _parse(deadline)
    if not dl:
        return "not recorded"
    dl = dl if dl.tzinfo else dl.replace(tzinfo=timezone.utc)
    ref = _now(as_of)
    days = (dl - ref).days
    when = f"{dl.date().isoformat()}"
    return (f"{when} (open: {days} day(s) left as of {ref.date().isoformat()})" if dl >= ref
            else f"{when} (closed {abs(days)} day(s) before the reference date {ref.date().isoformat()})")


def _render(ocid, summary, accepted, rejected_n, fid, row=None, as_of=None, audience="buyer", footer=""):
    A = AUDIENCE[audience]
    g = lambda k: (row[k] if row is not None and row[k] not in (None, "") else None)
    val = f"{g('value'):,.0f} {g('currency') or ''}".strip() if g("value") else "not recorded"
    glance = [("Buyer", g("buyer_name") or (f"{g('buyer_id')} (name not recorded)" if g("buyer_id") else "not recorded")),
              ("Category", g("category") or "not recorded"), ("Procurement method", g("method") or "not recorded"),
              ("Estimated value", val), ("Deadline", _timing(g("deadline"), as_of)), ("Bidding window", _window_text(g("start"), g("deadline")))]
    cell = lambda x: oneline(x).replace("|", "\\|")      # untrusted text can never break a table cell or start a new line/section
    lines = [f"# Tender readiness brief: {oneline(g('title') or ocid)}", f"`{oneline(ocid)}`  |  Filing `{fid}`  |  **pending human approval**", "",
             f"> {DISCLAIMER}", "", "## At a glance", "| | |", "|---|---|"] + [f"| {k} | {cell(v)} |" for k, v in glance]
    lines += ["", "## Summary", oneline(summary)[:1500]]
    for kind, title in SECTIONS:
        title = A["titles"].get(kind, title)
        items = sorted((f for f in accepted if f.kind == kind), key=lambda f: ORDER[f.severity])
        if items:
            lines += ["", f"## {title}"] + [f"- **{LABEL[f.severity]}:** {f.statement}  \n  _Source: {f.source_ocid} / {f.field_path}_" for f in items]
    verify = [A["missing"][k] for k, present in (("deadline", g("deadline")), ("start", g("start")), ("eligibility", g("eligibility")), ("buyer_name", g("buyer_name")),
                                            ("value", g("value")), ("category", g("category")), ("method", g("method"))) if not present]
    lines += ["", f"## {A['verify_title']}"] + [f"- {v}" for v in (verify or [A["verify_empty"]])]
    lines += ["", f"_{A['not_covered']}_"]
    if footer:
        lines += ["", f"_{footer}_"]
    if rejected_n:
        lines += ["", f"_{rejected_n} finding(s) were dropped for failing validation (missing source, invalid evidence, or verdict language)._"]
    return "\n".join(lines) + "\n"


@logged
def file_brief(ocid: str, summary: str, findings: list[dict], as_of: str | None = None, audience: str = "buyer",
               provenance: dict | None = None) -> dict:
    """Queue a sourced brief for human approval. Findings lacking a valid, real citation are dropped.
    With provenance (the agent's recorded tool calls, evidence, documents and model calls) every read-tool call is REPLAYED here
    and the brief is refused if any output differs: the agent cannot file work the data does not reproduce.
    A hash-linked manifest is stored with the brief. This does NOT file anything final: a named human must approve it."""
    conn = connect()
    if audience not in AUDIENCE:
        return {"status": "not_filed", "reason": f"unknown audience {audience!r}; use buyer or supplier", "rejected": []}
    trow = conn.execute("SELECT status, deadline, synthetic FROM tenders WHERE ocid=?", (ocid,)).fetchone()
    live = policy.live_reason(trow["status"], trow["deadline"], trow["synthetic"]) if trow else None
    if live and not policy.allowed():       # second line of defence: enforced where the brief is filed, whatever the agent did
        return {"status": "not_filed", "reason": f"live tender: {live}. Briefs are only prepared for closed tenders and synthetic data.", "rejected": []}
    if not isinstance(findings, list):
        return {"status": "not_filed", "reason": "findings must be a list", "rejected": []}
    overflow = max(0, len(findings) - MAX_FINDINGS)
    accepted, rejected = verify_findings(findings[:MAX_FINDINGS], conn)
    if overflow:
        rejected.append({"finding": f"{overflow} further finding(s)", "reason": f"too many findings (maximum {MAX_FINDINGS})"})
    if not accepted:
        return {"status": "not_filed", "reason": "no findings with a valid source", "rejected": rejected}
    if provenance:
        mism = prov.replay_mismatches(provenance.get("tool_calls", []))
        if mism:
            return {"status": "not_filed", "reason": "provenance does not replay: " + "; ".join(mism[:3]), "rejected": rejected}
    fid = uuid.uuid4().hex[:10]
    row = conn.execute("SELECT * FROM tenders WHERE ocid=?", (ocid,)).fetchone()
    ds, cv = prov.dataset(conn), prov.code_version()
    llm = ((provenance or {}).get("run") or {}).get("llm") or {}
    where = "REMOTE endpoint: prompts left this machine" if (llm.get("settings") or {}).get("remote") else "local"
    footer = (("LIVE TENDER OVERRIDE (BIDBOX_ALLOW_LIVE=1, development only) | " if live else "") + f"Run {audit_log.current_run_id()} | data {ds['fingerprint']} | code {cv} | " + (f"model {llm.get('model') or llm.get('name')} ({where}) | " if llm else "") +
              ("provenance recorded and replay-checked" if provenance else "NO provenance supplied: this brief cannot be independently reproduced"))
    md = _render(ocid, summary, accepted, len(rejected) + max(0, overflow - 1), fid, row, as_of, audience, footer)
    base = config.OUTBOX / "pending"
    (base / f"{fid}.md").write_text(md, encoding="utf-8")
    md_sha = prov.sha_file(base / f"{fid}.md")
    manifest = prov.make_manifest(fid=fid, run_id=audit_log.current_run_id(), ocid=ocid, audience=audience, as_of=as_of, md_sha256=md_sha, ds=ds,
                                  accepted=accepted, provenance=provenance, replay_ok=True)
    (base / f"{fid}.manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    man_sha = prov.sha_file(base / f"{fid}.manifest.json")
    (base / f"{fid}.json").write_text(json.dumps(
        {"filing_id": fid, "run_id": audit_log.current_run_id(), "ocid": ocid, "audience": audience, "as_of": as_of, "created": datetime.now(timezone.utc).isoformat(),
         "md_sha256": md_sha, "manifest_sha256": man_sha, "dataset": ds["fingerprint"], "code_version": cv,
         "findings": [f.model_dump() for f in accepted]}, indent=2), encoding="utf-8")
    return {"status": "pending_approval", "filing_id": fid, "accepted": len(accepted), "run_id": audit_log.current_run_id(),
            "md_sha256": md_sha, "manifest_sha256": man_sha, "rejected": rejected, "path": f"outbox/pending/{fid}.md"}
