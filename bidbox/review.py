"""Practitioner review: the evidence behind the 'fit to the tender cycle' criterion, built so it cannot be faked or inflated.
A real person reviews real briefs; their feedback is recorded against the EXACT brief fingerprints they saw; the report FIT.md is
generated from the recorded reviews (never typed), is consent-aware, and refuses to exist without them.
    python -m bidbox.review template            blank review form (JSON)
    python -m bidbox.review packet --filing ID  [--filing ID ...] -o packet.md     what you send the practitioner
    python -m bidbox.review record review.json  validate and store a completed review
    python -m bidbox.review change "what we changed" --because REVIEW_ID [--commit HASH]
    python -m bidbox.review fit -o FIT.md       generate FIT.md from the recorded reviews"""
import argparse
import json
import statistics
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from . import config, gate, provenance
from .store import connect
from .textsafe import oneline

DIR = lambda: (config.OUTBOX.parent / "reviews")
RATINGS = ("found_what_i_need", "correct", "would_act_on_it", "trust_sources")
ORG_TYPES = ("county or national government procurement unit", "state corporation or public entity procurement", "university or school procurement",
             "SME support desk or chamber of commerce", "supplier (small business)", "civil society or watchdog", "audit or oversight body", "other")
TEMPLATE = {
    "reviewer": {"role": "", "org_type": "one of: " + " | ".join(ORG_TYPES), "years_in_procurement": None, "name": "", "org": "", "is_human_attestation": False},
    "consent": {"use_feedback": False, "name_person": False, "name_org": False, "quote": False},
    "method": "in person | video call | phone | email", "date": "YYYY-MM-DD",
    "briefs": [{"filing_id": "", "md_sha256": "", "ratings": {k: 0 for k in RATINGS}, "errors_or_wrong": "", "missing": "", "remove": "", "minutes_manual": None, "minutes_with_brief": None}],
    "would_use_again": "yes | maybe | no", "one_quote": ""}
NOT_HUMAN = {"agent", "ai", "claude", "gpt", "llm", "bot", "assistant", "system", "auto", "qwen", "llama", "test", "n/a", "none", "unknown"}


def validate(rv: dict) -> tuple[dict | None, list[str]]:
    """Every rule that makes a review trustworthy. Returns (clean record, errors)."""
    errs, rv = [], rv or {}
    who, con = rv.get("reviewer") or {}, rv.get("consent") or {}
    role = oneline(who.get("role"))
    if len(role) < 3 or role.casefold() in NOT_HUMAN:
        errs.append("reviewer.role must describe a real job (for example 'head of procurement, county government')")
    if who.get("name") and oneline(who["name"]).casefold() in NOT_HUMAN:
        errs.append("reviewer.name looks like a placeholder or an automated name")
    if who.get("is_human_attestation") is not True:
        errs.append("reviewer.is_human_attestation must be true: a real person did this review, and you attest to it")
    if not oneline(who.get("org_type")) or str(who.get("org_type")).startswith("one of:"):
        errs.append("reviewer.org_type must be filled in (type of organisation; the name is optional)")
    if con.get("use_feedback") is not True:
        errs.append("consent.use_feedback must be true: no review is recorded without the person's consent")
    if not oneline(rv.get("method")) or str(rv.get("method")).startswith(("in person |",)):
        errs.append("method must say how the review happened")
    try:
        d = date.fromisoformat(str(rv.get("date")))
        if d > date.today():
            errs.append("date is in the future")
    except ValueError:
        errs.append("date must be YYYY-MM-DD")
    if rv.get("would_use_again") not in ("yes", "maybe", "no"):
        errs.append("would_use_again must be yes, maybe or no")
    briefs = rv.get("briefs")
    if not isinstance(briefs, list) or not briefs:
        return None, errs + ["at least one brief must be reviewed"]
    conn, clean_briefs = connect(), []
    for i, b in enumerate(briefs):
        tag = f"briefs[{i}]"
        fid, sha = (b or {}).get("filing_id"), str((b or {}).get("md_sha256") or "").lower()
        where, base = provenance.find_files(fid) if isinstance(fid, str) and gate.FILING_ID.match(fid) else (None, None)
        if not where:
            errs.append(f"{tag}: no such filing {fid!r}"); continue
        cur = provenance.sha_file(base / f"{fid}.md")
        if len(sha) != 64 or sha != cur:
            errs.append(f"{tag}: md_sha256 does not match the brief on file (the review must name the exact brief the person saw)"); continue
        rat = (b.get("ratings") or {})
        if any(not isinstance(rat.get(k), int) or isinstance(rat.get(k), bool) or not 1 <= rat[k] <= 5 for k in RATINGS):
            errs.append(f"{tag}: ratings.{'/'.join(RATINGS)} must each be a whole number from 1 to 5")
        mm, mw = b.get("minutes_manual"), b.get("minutes_with_brief")
        if (mm is None) != (mw is None) or any(x is not None and (not isinstance(x, (int, float)) or isinstance(x, bool) or not 0 < x < 100000) for x in (mm, mw)):
            errs.append(f"{tag}: minutes_manual and minutes_with_brief must both be given (positive numbers) or both left empty")
        man = json.loads((base / f"{fid}.manifest.json").read_text(encoding="utf-8")) if (base / f"{fid}.manifest.json").exists() else {}
        llm = ((man.get("run") or {}).get("llm") or {})
        synth = conn.execute("SELECT synthetic FROM tenders WHERE ocid=?", (man.get("ocid"),)).fetchone()
        clean_briefs.append({"filing_id": fid, "md_sha256": sha, "ratings": {k: rat.get(k) for k in RATINGS}, "minutes_manual": mm, "minutes_with_brief": mw,
                             **{k: oneline(b.get(k))[:2000] for k in ("errors_or_wrong", "missing", "remove")},
                             "ocid": man.get("ocid"), "audience": man.get("audience"), "data": "synthetic" if (synth and synth[0]) else "real (closed tender)",
                             "produced_by": "deterministic stub (NOT an AI model)" if llm.get("name") == "stub" else f"{llm.get('model') or llm.get('name') or 'unknown model'} ({'remote' if (llm.get('settings') or {}).get('remote') else 'local'})"})
    if errs:
        return None, errs
    return {"id": _new_id(), "recorded_at": datetime.now(timezone.utc).isoformat(), "date": str(rv["date"]), "method": oneline(rv["method"]),
            "reviewer": {"role": role, "org_type": oneline(who["org_type"]), "years_in_procurement": who.get("years_in_procurement") if isinstance(who.get("years_in_procurement"), (int, float)) else None,
                         "name": oneline(who.get("name")) or None, "org": oneline(who.get("org")) or None},
            "consent": {k: con.get(k) is True for k in ("use_feedback", "name_person", "name_org", "quote")},
            "briefs": clean_briefs, "would_use_again": rv["would_use_again"], "one_quote": oneline(rv.get("one_quote"))[:600] or None}, []


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def record(rv: dict) -> dict:
    clean, errs = validate(rv)
    if errs:
        raise ValueError("review not recorded:\n- " + "\n- ".join(errs))
    DIR().mkdir(parents=True, exist_ok=True)
    (DIR() / f"{clean['id']}.json").write_text(json.dumps(clean, indent=2, ensure_ascii=False), encoding="utf-8")
    return clean


def load_reviews() -> list[dict]:
    out = []
    for p in sorted(DIR().glob("*.json")) if DIR().exists() else []:
        if p.name == "changes.jsonl":
            continue
        try:
            r = json.loads(p.read_text(encoding="utf-8")); r["_sha256"] = provenance.sha_file(p); out.append(r)
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: (r.get("recorded_at", ""), r.get("id", "")))     # deterministic: the same files always give the same report


def record_change(text: str, because: str, commit: str | None = None) -> dict:
    ids = {r["id"] for r in load_reviews()}
    if because not in ids:
        raise ValueError(f"no recorded review {because!r}: a change can only be attributed to feedback that exists")
    DIR().mkdir(parents=True, exist_ok=True)
    rec = {"at": datetime.now(timezone.utc).isoformat(), "change": oneline(text)[:600], "because_of_review": because, "commit": commit}
    with open(DIR() / "changes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _changes() -> list[dict]:
    p = DIR() / "changes.jsonl"
    out = []
    for line in (p.read_text(encoding="utf-8", errors="replace").splitlines() if p.exists() else []):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def fit_report(path=None) -> str:
    """FIT.md, generated from recorded reviews only. Refuses to exist without them; names only with consent; hides nothing negative."""
    rs = load_reviews()
    if not rs:
        raise ValueError("no recorded reviews: FIT.md is evidence of a real conversation and cannot be written without one")
    label = lambda r: ((r["reviewer"]["name"] + ", ") if r["consent"]["name_person"] and r["reviewer"]["name"] else "") + r["reviewer"]["role"] + \
                      (f" ({r['reviewer']['org']})" if r["consent"]["name_org"] and r["reviewer"]["org"] else f" ({r['reviewer']['org_type']})")
    briefs = [(r, b) for r in rs for b in r["briefs"]]
    out = ["# FIT: did a person who does this job review it?", "",
           f"Generated by `python -m bidbox.review fit` from {len(rs)} recorded review(s) (file hashes below). Numbers are computed, not typed. "
           "Each review names the exact brief fingerprints the person saw; names appear only with consent.", "", "## Who reviewed what"]
    for r in rs:
        out.append(f"- **{label(r)}**, {r['method']}, {r['date']}; {len(r['briefs'])} brief(s); years in procurement: {r['reviewer']['years_in_procurement'] or 'not stated'}; "
                   f"would use again: **{r['would_use_again']}**")
    kinds = sorted({f"{b['data']}, produced by {b['produced_by']}" for _, b in briefs})
    out += ["", "What they were shown: " + "; ".join(kinds) + ".",
            *(["", "> **Some briefs were synthetic or stub-produced: that feedback is about layout and usefulness, not about accuracy on real tenders or about any AI model.**"]
              if any(b["data"] == "synthetic" or "stub" in b["produced_by"] for _, b in briefs) else [])]
    out += ["", f"## Ratings (1 = poor, 5 = excellent; n = {len(briefs)} brief reviews)"]
    for k in RATINGS:
        v = [b["ratings"][k] for _, b in briefs]
        out.append(f"- {k.replace('_', ' ')}: mean {statistics.mean(v):.1f}; distribution " + ", ".join(f"{s}:{v.count(s)}" for s in range(1, 6)))
    pairs = [(b["minutes_manual"], b["minutes_with_brief"]) for _, b in briefs if b["minutes_manual"] is not None]
    out += ["", "## Time (the same task done their usual way, then with the brief; as reported by the reviewer)"]
    if pairs:
        man, wb = [p[0] for p in pairs], [p[1] for p in pairs]
        out += [f"- n = {len(pairs)} timed task(s). Usual way: median {statistics.median(man):g} min. With the brief: median {statistics.median(wb):g} min. "
                f"Median saving per task: {statistics.median([m - w for m, w in pairs]):g} min.",
                "- Per task (usual -> with brief, minutes): " + "; ".join(f"{m:g} -> {w:g}" for m, w in pairs)]
        if len(pairs) < 5:
            out.append(f"- **Small sample (n = {len(pairs)}): these are anecdotes, not a statistic, and they say nothing about a whole tender evaluation.**")
    else:
        out.append("- No timed comparison was recorded.")
    for title, key in (("What was wrong", "errors_or_wrong"), ("What was missing", "missing"), ("What they would remove", "remove")):
        items = [f"- ({label(r)}, {b['ocid']}) {b[key]}" for r, b in briefs if b[key]]
        out += ["", f"## {title} (every entry, verbatim)"] + (items or ["- nothing recorded"])
    quotes = [f"- \"{r['one_quote']}\" ({label(r)})" for r in rs if r["one_quote"] and r["consent"]["quote"]]
    out += ["", "## Quotes (only where the person agreed)"] + (quotes or ["- none"])
    ch = _changes()
    out += ["", "## What we changed because of it"] + ([f"- {c['change']} (review {c['because_of_review']}" + (f", commit {c['commit']}" if c.get("commit") else "") + ")" for c in ch] or ["- nothing recorded yet"])
    out += ["", "## Limits", f"- {len(rs)} reviewer(s). No claim about how long a whole evaluation takes, or about any office beyond those named above.",
            "- Reviews are self-reported and attested by the team; the review files and their hashes are available on request.", "", "## Review files"] + \
           [f"- `{r['id']}.json` sha256 `{r['_sha256']}`" for r in rs]
    text = "\n".join(out) + "\n"
    if path:
        Path(path).write_text(text, encoding="utf-8")
    return text


def make_packet(filing_ids: list[str], path=None) -> str:
    rows = []
    for fid in filing_ids:
        where, base = provenance.find_files(fid) if gate.FILING_ID.match(fid or "") else (None, None)
        if not where:
            raise ValueError(f"no such filing {fid!r}")
        man = json.loads((base / f"{fid}.manifest.json").read_text(encoding="utf-8")) if (base / f"{fid}.manifest.json").exists() else {}
        llm = ((man.get("run") or {}).get("llm") or {}); synth = connect().execute("SELECT synthetic FROM tenders WHERE ocid=?", (man.get("ocid"),)).fetchone()
        rows.append((fid, provenance.sha_file(base / f"{fid}.md"), (base / f"{fid}.md").read_text(encoding="utf-8"), man.get("ocid"),
                     "a deterministic stub, NOT an AI model: judge the content and layout, not the wording" if llm.get("name") == "stub" else f"the model {llm.get('model') or llm.get('name') or 'unknown'}",
                     "SYNTHETIC (invented) data" if (synth and synth[0]) else "a real, closed public tender"))
    out = ["# Review packet", "", f"Thank you. This takes about {10 + 8 * len(rows)} minutes. We are testing whether a short, sourced brief on a public tender helps someone who does your kind of work. "
           "We want honest criticism: what is wrong, missing or useless matters more to us than praise.", "",
           "**Before you start.** Nothing here is a live tender and nothing contains personal data. You can stop at any time. We will record your answers against the exact briefs below "
           "(each has a fingerprint). We will only use your name, your organisation's name or a quote if you agree.", "",
           "**Please tell us** (we may ask you aloud and write it down): your role and type of organisation; years in procurement; and whether we may name you, your organisation, or quote you.", "",
           "**For each brief:** (1) Did it contain what you would need? (2) Did you spot anything wrong? (3) What is missing? (4) What would you remove? "
           "(5) Rate 1-5: found what I need / correct / I would act on it / I trust the sources. "
           "(6) **Timed task:** note how many minutes the same questions take you your usual way (deadline, eligibility, buyer's recent awards in this category, whether the estimate is in line with past awards), "
           "then how many minutes with this brief. Do them in the order you prefer and tell us which. (7) Would you use this again: yes, maybe, no?", ""]
    for i, (fid, sha, md, ocid, by, data) in enumerate(rows, 1):
        out += ["---", f"## Brief {i}: `{ocid}`", f"- Produced by {by}. Data: {data}. Fingerprint (sha256): `{sha}`", "", md, ""]
    text = "\n".join(out) + "\n"
    if path:
        Path(path).write_text(text, encoding="utf-8")
    return text


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m bidbox.review", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("template")
    k = s.add_parser("packet"); k.add_argument("--filing", action="append", required=True); k.add_argument("-o", required=True)
    r = s.add_parser("record"); r.add_argument("file")
    c = s.add_parser("change"); c.add_argument("text"); c.add_argument("--because", required=True); c.add_argument("--commit")
    f = s.add_parser("fit"); f.add_argument("-o", required=True)
    a = p.parse_args(argv)
    try:
        if a.cmd == "template":
            print(json.dumps(TEMPLATE, indent=2))
        elif a.cmd == "packet":
            make_packet(a.filing, a.o); print(f"wrote {a.o}")
        elif a.cmd == "record":
            rec = record(json.loads(Path(a.file).read_text(encoding="utf-8"))); print(f"recorded review {rec['id']}")
        elif a.cmd == "change":
            record_change(a.text, a.because, a.commit); print("recorded")
        elif a.cmd == "fit":
            fit_report(a.o); print(f"wrote {a.o}")
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}"); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
