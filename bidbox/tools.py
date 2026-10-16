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
            "sources": [{"ocid": r["ocid"], "field_path": "awards[].suppliers[]"} for r in rows[:limit]]}


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
            "sources": [{"ocid": o, "field_path": "awards[].value.amount"} for o, _ in kept[:10]]}


@logged
def window_baseline(ocid: str, min_peers: int = 5) -> dict:
    """How long was this tender open for bids, compared with comparable tenders (same method and category,
    else same method, else same category)? No legal minimum is assumed or invented: short windows can be
    legitimate (urgent needs), so this reports where the window sits among its peers and nothing more."""
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
                    "sources": srcs + [{"ocid": o, "field_path": "tender.tenderPeriod.endDate"} for _, o in peers[:5]]}
    return {**base, "window_days": days, "position": "insufficient_peers", "n_peers": 0, "low_confidence": True, "sources": srcs}


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
           "sources": [{"ocid": o, "field_path": "awards[].suppliers[]"} for o in sorted(tgt["ocids"])[:5]]}
    if len(peers) < MIN_PEER_BUYERS:
        return {**out, "position": "insufficient_peers", "low_confidence": True}
    med = statistics.median(peers)
    _, p75 = _quartiles(peers)
    above = out["buyer_share"] > p75 and out["buyer_share"] - med >= PEER_ABOVE_MARGIN
    return {**out, "peer_median_share": round(med, 3), "peer_p75_share": round(p75, 3),
            "position": "above_peers" if above else "typical_for_peers", "low_confidence": len(peers) < 5}


# --------------------------------------------------------------------------- rendering

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
    footer = ((f"LIVE TENDER OVERRIDE (BIDBOX_ALLOW_LIVE=1, development only) | " if live else "") + f"Run {run_id} | data {ds['fingerprint']} | code {cv} | "
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