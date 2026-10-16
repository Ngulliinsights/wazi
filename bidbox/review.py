"""Practitioner review: the evidence behind the 'fit to the tender cycle' criterion, built so it cannot be faked or inflated.
A real person reviews real briefs; their feedback is recorded against the EXACT brief fingerprints they saw; the report FIT.md is
generated from the recorded reviews (never typed), is consent-aware, and refuses to exist without them.
    python -m bidbox.review template            blank review form (JSON)
    python -m bidbox.review packet --filing ID  [--filing ID ...] -o packet.md     what you send the practitioner
    python -m bidbox.review record review.json  validate and store a completed review
    python -m bidbox.review change "what we changed" --because REVIEW_ID [--commit HASH]
    python -m bidbox.review fit -o FIT.md       generate FIT.md from the recorded reviews"""
import argparse
import hashlib
import json
import re
import statistics
import sys
import uuid
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

from . import config, gate, provenance
from .store import connect
from .textsafe import oneline


def DIR() -> Path:
    return config.OUTBOX.parent / "reviews"


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

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,40}")
MAX_FREE_TEXT = 2000
MAX_MINUTES = 100_000


# --------------------------------------------------------------------------- small helpers

def _obj(x) -> dict:
    return x if isinstance(x, dict) else {}


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _clip(text: str, n: int) -> str:
    """Truncate visibly: a report that claims to show every entry must not cut one silently."""
    return text if len(text) <= n else text[:n].rstrip() + " [...]"


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def _warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def _brief_context(fid: str, base: Path, conn) -> dict:
    """What we can verify about how a brief was made, from its manifest and the tender table. Unknown stays unknown:
    a missing manifest or an unlisted tender must never read as 'real data produced by a local model'."""
    mp = base / f"{fid}.manifest.json"
    try:
        man = json.loads(mp.read_text(encoding="utf-8")) if mp.exists() else {}
    except (OSError, ValueError):
        man = {}
    man = _obj(man)
    run = _obj(man.get("run"))
    llm = _obj(run.get("llm"))
    ocid = man.get("ocid")
    row = conn.execute("SELECT synthetic FROM tenders WHERE ocid=?", (ocid,)).fetchone() if ocid else None
    live = bool(run.get("live_override"))
    if row and row[0]:
        data = "synthetic"
    elif row and live:
        data = "real (LIVE tender, override used)"
    elif row:
        data = "real (closed tender)"
    else:
        data = "unknown (tender not in the local database)"
    stub = llm.get("name") == "stub"
    remote = _obj(llm.get("settings")).get("remote")
    where = "remote" if remote is True else "local" if remote is False else "location unknown"
    model = llm.get("model") or llm.get("name") or "unknown model"
    fallback = bool(run.get("used_template_fallback"))
    produced = "deterministic stub (NOT an AI model)" if stub else f"{model} ({where})"
    if fallback and not stub:
        produced += "; findings are the deterministic fallback, not model output"
    return {"ocid": ocid, "audience": man.get("audience"), "data": data, "produced_by": produced,
            "stub": stub, "live": live, "model": model, "fallback": fallback}


def _find(fid) -> tuple[bool, Path | None]:
    if isinstance(fid, str) and gate.FILING_ID.match(fid):
        where, base = provenance.find_files(fid)
        return bool(where), base
    return False, None


# --------------------------------------------------------------------------- validation

def validate(rv: dict) -> tuple[dict | None, list[str]]:
    """Every rule that makes a review trustworthy. Returns (clean record, errors)."""
    if not isinstance(rv, dict):
        return None, ["a review must be a JSON object (start from `python -m bidbox.review template`)"]
    errs: list[str] = []
    who, con = _obj(rv.get("reviewer")), _obj(rv.get("consent"))
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
    method = oneline(rv.get("method"))
    if not method or method.startswith("in person |"):
        errs.append("method must say how the review happened")

    when = None
    raw_date = str(rv.get("date") or "")
    if DATE_RE.fullmatch(raw_date):          # fromisoformat alone also accepts forms like 20261016 and week dates
        try:
            when = date.fromisoformat(raw_date)
        except ValueError:
            pass
    if when is None:
        errs.append("date must be YYYY-MM-DD")
    elif when > date.today():
        errs.append("date is in the future")
    if rv.get("would_use_again") not in ("yes", "maybe", "no"):
        errs.append("would_use_again must be yes, maybe or no")

    briefs = rv.get("briefs")
    if not isinstance(briefs, list) or not briefs:
        return None, errs + ["at least one brief must be reviewed"]

    clean_briefs, seen = [], set()
    with closing(connect()) as conn:
        for i, item in enumerate(briefs):
            tag, b = f"briefs[{i}]", _obj(item)
            fid = b.get("filing_id")
            found, base = _find(fid)
            if not found:
                errs.append(f"{tag}: no such filing {fid!r}")
                continue
            if fid in seen:              # one brief counted twice would inflate every number in the report
                errs.append(f"{tag}: filing {fid} is listed more than once in this review")
                continue
            seen.add(fid)
            sha = str(b.get("md_sha256") or "").lower()
            try:
                cur = provenance.sha_file(base / f"{fid}.md")
            except OSError:
                errs.append(f"{tag}: the brief file for {fid} cannot be read")
                continue
            if len(sha) != 64 or sha != cur:
                errs.append(f"{tag}: md_sha256 does not match the brief on file (the review must name the exact brief the person saw)")
                continue
            rat = _obj(b.get("ratings"))
            if any(not _is_int(rat.get(k)) or not 1 <= rat[k] <= 5 for k in RATINGS):
                errs.append(f"{tag}: ratings.{'/'.join(RATINGS)} must each be a whole number from 1 to 5")
            mm, mw = b.get("minutes_manual"), b.get("minutes_with_brief")
            if (mm is None) != (mw is None) or any(x is not None and (not _is_num(x) or not 0 < x < MAX_MINUTES) for x in (mm, mw)):
                errs.append(f"{tag}: minutes_manual and minutes_with_brief must both be given (positive numbers) or both left empty")
            ctx = _brief_context(fid, base, conn)
            clean_briefs.append({
                "filing_id": fid, "md_sha256": sha, "ratings": {k: rat.get(k) for k in RATINGS},
                "minutes_manual": mm, "minutes_with_brief": mw,
                **{k: _clip(oneline(b.get(k)), MAX_FREE_TEXT) for k in ("errors_or_wrong", "missing", "remove")},
                "ocid": ctx["ocid"], "audience": ctx["audience"], "data": ctx["data"], "produced_by": ctx["produced_by"]})
    if errs:
        return None, errs

    years = who.get("years_in_procurement")
    return {
        "id": _new_id(), "recorded_at": datetime.now(timezone.utc).isoformat(), "date": when.isoformat(), "method": method,
        "reviewer": {"role": role, "org_type": oneline(who["org_type"]),
                     "years_in_procurement": years if _is_num(years) and 0 <= years <= 70 else None,
                     "name": oneline(who.get("name")) or None, "org": oneline(who.get("org")) or None},
        "consent": {k: con.get(k) is True for k in ("use_feedback", "name_person", "name_org", "quote")},
        "briefs": clean_briefs, "would_use_again": rv["would_use_again"],
        "one_quote": oneline(rv.get("one_quote"))[:600] or None}, []


def _fingerprint(rec: dict) -> str:
    """Identity of a review's CONTENT (not its id or timestamp): recording the same file twice must not count twice."""
    core = {k: v for k, v in rec.items() if k not in ("id", "recorded_at") and not k.startswith("_")}
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def record(rv: dict) -> dict:
    clean, errs = validate(rv)
    if errs:
        raise ValueError("review not recorded:\n- " + "\n- ".join(errs))
    fp = _fingerprint(clean)
    for r in load_reviews():
        if _fingerprint(r) == fp:
            raise ValueError(f"review not recorded: an identical review is already recorded as {r.get('id')}")
    DIR().mkdir(parents=True, exist_ok=True)
    text = json.dumps(clean, indent=2, ensure_ascii=False)
    for _ in range(5):
        try:
            with open(DIR() / f"{clean['id']}.json", "x", encoding="utf-8") as f:   # exclusive: a recorded review is never overwritten
                f.write(text)
            return clean
        except FileExistsError:
            clean["id"] = _new_id()
            text = json.dumps(clean, indent=2, ensure_ascii=False)
    raise ValueError("review not recorded: could not allocate a free review id")


def _well_formed(r) -> bool:
    try:
        return (isinstance(r, dict) and isinstance(r["id"], str) and isinstance(r["reviewer"], dict) and isinstance(r["consent"], dict)
                and bool(r["briefs"]) and all(isinstance(b, dict) and all(_is_int(b["ratings"][k]) for k in RATINGS) for b in r["briefs"]))
    except (KeyError, TypeError):
        return False


def load_reviews() -> list[dict]:
    out = []
    for p in sorted(DIR().glob("*.json")) if DIR().exists() else []:
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            r["_sha256"] = provenance.sha_file(p)
        except (OSError, ValueError, TypeError):
            _warn(f"skipped unreadable review file {p.name}")
            continue
        if not _well_formed(r):
            _warn(f"skipped malformed review file {p.name}")
            continue
        out.append(r)
    return sorted(out, key=lambda r: (r.get("recorded_at", ""), r.get("id", "")))     # deterministic: the same files always give the same report


# --------------------------------------------------------------------------- changes

def record_change(text: str, because: str, commit: str | None = None) -> dict:
    if because not in {r["id"] for r in load_reviews()}:
        raise ValueError(f"no recorded review {because!r}: a change can only be attributed to feedback that exists")
    change = oneline(text)
    if not change:
        raise ValueError("say what was changed")
    if commit is not None and not COMMIT_RE.fullmatch(commit):
        raise ValueError(f"commit {commit!r} does not look like a git commit hash (7-40 hex characters)")
    DIR().mkdir(parents=True, exist_ok=True)
    rec = {"at": datetime.now(timezone.utc).isoformat(), "change": _clip(change, 600), "because_of_review": because, "commit": commit}
    with open(DIR() / "changes.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _changes() -> list[dict]:
    p = DIR() / "changes.jsonl"
    out = []
    for line in (p.read_text(encoding="utf-8", errors="replace").splitlines() if p.exists() else []):
        try:
            c = json.loads(line)
        except ValueError:
            continue
        if isinstance(c, dict) and c.get("change") and c.get("because_of_review"):
            out.append(c)
    return out


# --------------------------------------------------------------------------- FIT.md

def _label(r: dict) -> str:
    who, con = r["reviewer"], r["consent"]
    person = f"{who['name']}, " if con.get("name_person") and who.get("name") else ""
    org = who["org"] if con.get("name_org") and who.get("org") else who.get("org_type")
    return f"{person}{who.get('role')} ({org})"


def _is_synthetic(b: dict) -> bool:
    return str(b.get("data", "")).startswith("synthetic")


def _is_stub(b: dict) -> bool:
    return str(b.get("produced_by", "")).startswith("deterministic stub")


def _is_unknown(b: dict) -> bool:
    return str(b.get("data", "")).startswith("unknown") or "unknown model" in str(b.get("produced_by", ""))


def _section_who(rs: list[dict], briefs: list) -> list[str]:
    out = ["", "## Who reviewed what"]
    for r in rs:
        years = r["reviewer"].get("years_in_procurement")
        out.append(f"- **{_label(r)}**, {r.get('method')}, {r.get('date')}; {len(r['briefs'])} brief(s); "
                   f"years in procurement: {'not stated' if years is None else years}; would use again: **{r.get('would_use_again')}**")
    kinds = sorted({f"{b.get('data', 'unknown')}, produced by {b.get('produced_by', 'unknown')}" for _, b in briefs})
    out += ["", "What they were shown: " + "; ".join(kinds) + "."]
    if any(_is_synthetic(b) or _is_stub(b) for _, b in briefs):
        out += ["", "> **Some briefs were synthetic or stub-produced: that feedback is about layout and usefulness, not about accuracy on real tenders or about any AI model.**"]
    if any(_is_unknown(b) for _, b in briefs):
        out += ["", "> **For some briefs the data origin or the model could not be verified from the record; treat their feedback accordingly.**"]
    return out


def _section_ratings(briefs: list) -> list[str]:
    out = ["", f"## Ratings (1 = poor, 5 = excellent; n = {len(briefs)} brief reviews)"]
    for k in RATINGS:
        v = [b["ratings"][k] for _, b in briefs]
        out.append(f"- {k.replace('_', ' ')}: mean {statistics.mean(v):.1f}; distribution " + ", ".join(f"{s}:{v.count(s)}" for s in range(1, 6)))
    return out


def _section_time(briefs: list) -> list[str]:
    out = ["", "## Time (the same task done their usual way, then with the brief; as reported by the reviewer)"]
    pairs = [(b["minutes_manual"], b["minutes_with_brief"]) for _, b in briefs
             if b.get("minutes_manual") is not None and b.get("minutes_with_brief") is not None]
    if not pairs:
        return out + ["- No timed comparison was recorded."]
    man, wb = [p[0] for p in pairs], [p[1] for p in pairs]
    out += [f"- n = {len(pairs)} timed task(s). Usual way: median {statistics.median(man):g} min. With the brief: median {statistics.median(wb):g} min. "
            f"Median saving per task: {statistics.median([m - w for m, w in pairs]):g} min.",
            "- Per task (usual -> with brief, minutes): " + "; ".join(f"{m:g} -> {w:g}" for m, w in pairs)]
    if len(pairs) < 5:
        out.append(f"- **Small sample (n = {len(pairs)}): these are anecdotes, not a statistic, and they say nothing about a whole tender evaluation.**")
    return out


def _section_feedback(briefs: list) -> list[str]:
    out = []
    for title, key in (("What was wrong", "errors_or_wrong"), ("What was missing", "missing"), ("What they would remove", "remove")):
        items = [f"- ({_label(r)}, {b.get('ocid')}) {b[key]}" for r, b in briefs if b.get(key)]
        out += ["", f"## {title} (every entry; whitespace normalised)"] + (items or ["- nothing recorded"])
    return out


def fit_report(path=None) -> str:
    """FIT.md, generated from recorded reviews only. Refuses to exist without them; names only with consent; hides nothing negative."""
    rs = load_reviews()
    if not rs:
        raise ValueError("no recorded reviews: FIT.md is evidence of a real conversation and cannot be written without one")
    briefs = [(r, b) for r in rs for b in r["briefs"]]
    out = ["# FIT: did a person who does this job review it?", "",
           f"Generated by `python -m bidbox.review fit` from {len(rs)} recorded review(s) (file hashes below). Numbers are computed, not typed. "
           "Each review names the exact brief fingerprints the person saw; names appear only with consent."]
    out += _section_who(rs, briefs) + _section_ratings(briefs) + _section_time(briefs) + _section_feedback(briefs)
    quotes = [f"- \"{r['one_quote']}\" ({_label(r)})" for r in rs if r.get("one_quote") and r["consent"].get("quote")]
    out += ["", "## Quotes (only where the person agreed)"] + (quotes or ["- none"])
    changes = [f"- {c['change']} (review {c['because_of_review']}" + (f", commit {c['commit']}" if c.get("commit") else "") + ")" for c in _changes()]
    out += ["", "## What we changed because of it"] + (changes or ["- nothing recorded yet"])
    out += ["", "## Limits", f"- {len(rs)} reviewer(s). No claim about how long a whole evaluation takes, or about any office beyond those named above.",
            "- Reviews are self-reported and attested by the team; the review files and their hashes are available on request.", "", "## Review files"]
    out += [f"- `{r['id']}.json` sha256 `{r['_sha256']}`" for r in rs]
    text = "\n".join(out) + "\n"
    if path:
        Path(path).write_text(text, encoding="utf-8")
    return text


# --------------------------------------------------------------------------- packet

def make_packet(filing_ids: list[str], path=None) -> str:
    rows, seen = [], set()
    with closing(connect()) as conn:
        for fid in filing_ids:
            if fid in seen:
                continue
            seen.add(fid)
            found, base = _find(fid)
            if not found:
                raise ValueError(f"no such filing {fid!r}")
            ctx = _brief_context(fid, base, conn)
            if ctx["live"]:
                raise ValueError(f"filing {fid} was made with the live-tender override; the packet promises 'nothing here is a live tender', so it cannot include it")
            md_path = base / f"{fid}.md"
            by = ("a deterministic stub, NOT an AI model: judge the content and layout, not the wording" if ctx["stub"]
                  else f"the model {ctx['model']}" + (" (its findings are the deterministic fallback, not model output)" if ctx["fallback"] else ""))
            data = {"synthetic": "SYNTHETIC (invented) data", "real (closed tender)": "a real, closed public tender"}.get(
                ctx["data"], "data of unverified origin (tender not in the local database)")
            rows.append((fid, provenance.sha_file(md_path), md_path.read_text(encoding="utf-8"), ctx["ocid"], by, data))
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


# --------------------------------------------------------------------------- CLI

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m bidbox.review", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    s = p.add_subparsers(dest="cmd", required=True)
    s.add_parser("template")
    k = s.add_parser("packet")
    k.add_argument("--filing", action="append", required=True)
    k.add_argument("-o", required=True)
    r = s.add_parser("record")
    r.add_argument("file")
    c = s.add_parser("change")
    c.add_argument("text")
    c.add_argument("--because", required=True)
    c.add_argument("--commit")
    f = s.add_parser("fit")
    f.add_argument("-o", required=True)
    return p


def main(argv=None):
    a = _parser().parse_args(argv)
    try:
        if a.cmd == "template":
            print(json.dumps(TEMPLATE, indent=2))
        elif a.cmd == "packet":
            make_packet(a.filing, a.o)
            print(f"wrote {a.o}")
        elif a.cmd == "record":
            rec = record(json.loads(Path(a.file).read_text(encoding="utf-8")))
            print(f"recorded review {rec['id']}")
        elif a.cmd == "change":
            record_change(a.text, a.because, a.commit)
            print("recorded")
        elif a.cmd == "fit":
            fit_report(a.o)
            print(f"wrote {a.o}")
    except (ValueError, OSError) as e:      # JSONDecodeError is a ValueError; FileNotFoundError an OSError
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())