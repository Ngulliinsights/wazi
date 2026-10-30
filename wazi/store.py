"""Normalise OCDS release packages into SQLite. Messy data is flagged, never silently fixed."""
import gzip
import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders(
  ocid TEXT PRIMARY KEY, title TEXT, description TEXT, status TEXT, category TEXT,
  method TEXT, deadline TEXT, start TEXT, value REAL, currency TEXT, eligibility TEXT,
  buyer_id TEXT, buyer_name TEXT, doc_urls TEXT, synthetic INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS awards(
  ocid TEXT, award_id TEXT, supplier_id TEXT, supplier_name TEXT,
  amount REAL, currency TEXT, date TEXT);
CREATE INDEX IF NOT EXISTS ix_aw_ocid ON awards(ocid);
CREATE TABLE IF NOT EXISTS loads(id INTEGER PRIMARY KEY AUTOINCREMENT, file TEXT, sha256 TEXT, bytes INTEGER, tenders INTEGER, awards INTEGER, loaded_at TEXT);
CREATE INDEX IF NOT EXISTS ix_t_buyer ON tenders(buyer_id, buyer_name);
CREATE INDEX IF NOT EXISTS ix_t_buyer_name ON tenders(buyer_name);
CREATE INDEX IF NOT EXISTS ix_t_category ON tenders(category);
"""

# columns added after the first release: databases created earlier get them on connect
MIGRATIONS = (("start", "TEXT"), ("synthetic", "INTEGER DEFAULT 0"))

TENDER_COLS = ("title", "description", "status", "category", "method", "deadline", "start", "value", "currency",
               "eligibility", "buyer_id", "buyer_name")
# A tender arrives as SEVERAL releases (tender stage, award stage, contract stage...). Later releases add or change fields;
# a field a later release does not mention is unchanged, never erased.
UPSERT_TENDER = (
    f"INSERT INTO tenders(ocid,{','.join(TENDER_COLS)},doc_urls,synthetic) VALUES({','.join('?' * (len(TENDER_COLS) + 3))}) "
    "ON CONFLICT(ocid) DO UPDATE SET " + ",".join(f"{c}=COALESCE(excluded.{c},tenders.{c})" for c in TENDER_COLS)
    + ",doc_urls=CASE WHEN excluded.doc_urls IS NOT NULL AND excluded.doc_urls<>'[]' THEN excluded.doc_urls ELSE tenders.doc_urls END,"
      "synthetic=excluded.synthetic")
DELETE_AWARD = "DELETE FROM awards WHERE ocid=? AND award_id IS ?"      # IS: matches NULL ids too
INSERT_AWARD = "INSERT INTO awards(ocid,award_id,supplier_id,supplier_name,amount,currency,date) VALUES(?,?,?,?,?,?,?)"

# Digits only, optionally grouped in thousands with commas: "1250000", "1,250,000.50". Anything else is NOT guessed at:
# "1.234,56" (European), "KES 1000", "1e6" or "1_000" would each be read as a different number than the publisher meant.
_AMOUNT_TEXT = re.compile(r"\d+(?:\.\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = str(path or DB_PATH)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(tenders)")}
    for name, decl in MIGRATIONS:
        if name not in have:
            conn.execute(f"ALTER TABLE tenders ADD COLUMN {name} {decl}")
    conn.commit()
    return conn


# --------------------------------------------------------------------------- reading

BAD = object()  # sentinel for an unparseable line


def _loads(raw):
    """bytes or str -> JSON value, or BAD. Bytes let the json module handle a BOM and report invalid UTF-8 as BAD, not a crash."""
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):      # JSONDecodeError and UnicodeDecodeError are both ValueErrors
        return BAD


def iter_releases(path, meta=None) -> Iterator:
    """Yield release dicts from: JSON Lines (streamed), a release package, a record package, a single release, or a bare list; .gz ok.
    Anything unreadable is yielded as BAD so the loader can count it."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as f:
        first = f.readline()
        while first and not first.strip():        # leading blank lines
            first = f.readline()
        obj = _loads(first)
        if isinstance(obj, dict) and "releases" not in obj and "records" not in obj:  # JSON Lines: stream it
            yield obj
            for line in f:
                if line.strip():
                    yield _loads(line)
            return
        rest = f.read()
        doc = obj if (obj is not BAD and not rest.strip()) else _loads(first + rest)   # a one-line document need not be parsed twice
    if doc is BAD:
        yield BAD
        return
    if meta is not None and isinstance(doc, dict):
        meta["publisher"] = doc.get("publisher")                  # lets the loader tell synthetic data from real
    if isinstance(doc, list):
        yield from doc
    elif isinstance(doc, dict) and "records" in doc:               # record package: use the compiled release
        for rec in _l(doc["records"]):
            rec = _d(rec)
            rels = _l(rec.get("releases"))
            yield rec.get("compiledRelease") or (rels[-1] if rels else BAD)
    elif isinstance(doc, dict) and "releases" in doc:
        yield from _l(doc["releases"])
    elif isinstance(doc, dict) and "ocid" in doc:                  # a single pretty-printed release
        yield doc
    else:
        yield BAD


def _d(x):
    return x if isinstance(x, dict) else {}


def _l(x):
    return x if isinstance(x, list) else []


def _s(x):
    """A non-empty scalar as trimmed text, else None."""
    return str(x).strip() if isinstance(x, (str, int, float)) and not isinstance(x, bool) and str(x).strip() else None


def _parse_dt(x):
    """ISO 8601 text -> aware datetime (naive values are taken as UTC), or None."""
    if not isinstance(x, str):
        return None
    try:
        dt = datetime.fromisoformat(x.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(x):
    """The text unchanged if it is a valid ISO date or datetime, else None. Values are validated, never rewritten."""
    return x if _parse_dt(x) else None


def _amount(x) -> tuple[float | None, bool, bool]:
    """(value or None, was coerced from text, is invalid). Absent is not invalid; non-positive, non-finite and non-numeric are."""
    if x is None:
        return None, False, False
    if isinstance(x, bool):
        return None, False, True
    coerced = False
    if isinstance(x, str):
        text = x.strip()
        if not _AMOUNT_TEXT.fullmatch(text):
            return None, False, True
        v, coerced = float(text.replace(",", "")), True
    elif isinstance(x, (int, float)):
        v = float(x)
    else:
        return None, False, True
    if not math.isfinite(v) or v <= 0:
        return None, coerced, True
    return v, coerced, False


# --------------------------------------------------------------------------- loading

def load_ocds(json_path: Path | str, conn: sqlite3.Connection | None = None, synthetic: bool | None = None) -> dict:
    """Load real-world-messy OCDS. Bad records are skipped and COUNTED, never silently fixed:
    numbers stored as text are coerced (counted); non-positive or non-numeric amounts become unknown.
    The whole file is one transaction: it is loaded completely or not at all.
    'duplicates' counts releases whose ocid was already seen in this file (normal for multi-stage tenders, so not an error);
    'synthetic' is taken from the argument, else from the package publisher, else False."""
    own = conn is None
    conn = conn or connect()
    st = {"tenders": 0, "awards": 0, "skipped": 0, "coerced_numbers": 0, "invalid_amounts": 0, "bad_dates": 0,
          "inverted_windows": 0, "duplicates": 0, "duplicate_award_ids": 0}
    seen: set[str] = set()

    def amount(x):
        v, coerced, invalid = _amount(x)
        st["coerced_numbers"] += coerced
        st["invalid_amounts"] += invalid
        return v

    def date_of(x):
        out = _iso(x)
        if x and not out:
            st["bad_dates"] += 1
        return out

    meta: dict = {}
    is_synth = None
    try:
        for r in iter_releases(json_path, meta):
            if is_synth is None:     # the publisher (if any) is known once the first release arrives
                is_synth = int(synthetic) if synthetic is not None else int("synthetic" in json.dumps(meta.get("publisher") or {}).lower())
            if r is BAD or not isinstance(r, dict) or not _s(r.get("ocid")):
                st["skipped"] += 1
                continue
            ocid = _s(r["ocid"])
            st["duplicates"] += ocid in seen
            seen.add(ocid)
            t, b = _d(r.get("tender")), _d(r.get("buyer"))
            period, tv = _d(t.get("tenderPeriod")), _d(t.get("value"))
            deadline, start = date_of(period.get("endDate")), date_of(period.get("startDate"))
            if start and deadline and _parse_dt(start) > _parse_dt(deadline):   # compared as instants, not as text; flagged, never silently fixed
                st["inverted_windows"] += 1
            docs = [d["url"] for d in map(_d, _l(t.get("documents"))) if isinstance(d.get("url"), str)]
            conn.execute(UPSERT_TENDER,
                         (ocid, _s(t.get("title")), _s(t.get("description")), _s(t.get("status")),
                          _s(t.get("mainProcurementCategory")), _s(t.get("procurementMethod")), deadline, start,
                          amount(tv.get("amount")), _s(tv.get("currency")), _s(t.get("eligibilityCriteria")),
                          _s(b.get("id")), _s(b.get("name")), json.dumps(docs), is_synth))
            st["tenders"] += 1
            _load_awards(conn, st, ocid, _l(r.get("awards")), amount, date_of)

        h, size = hashlib.sha256(), 0
        with open(json_path, "rb") as fh:                      # streamed: real exports are large
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
                size += len(chunk)
        conn.execute("INSERT INTO loads(file, sha256, bytes, tenders, awards, loaded_at) VALUES(?,?,?,?,?,?)",
                     (Path(json_path).name, h.hexdigest(), size, st["tenders"], st["awards"], datetime.now(timezone.utc).isoformat()))
        st["sha256"] = h.hexdigest()
        conn.commit()
    except BaseException:
        conn.rollback()          # never leave half a file in the database
        raise
    finally:
        if own:
            conn.close()
    return st


def _load_awards(conn, st: dict, ocid: str, raw_awards: list, amount, date_of) -> None:
    """Replace awards by id, never wipe them with their siblings. Awards without an id are replaced as a group by the same release.
    Within one release the last award with a given id wins (and is counted); awards that have no id are all kept."""
    by_id: dict[str | None, list[dict]] = {}
    keyless: list[dict] = []
    for a in map(_d, raw_awards):
        aid = _s(a.get("id"))
        if aid is None:
            keyless.append(a)
        else:
            if aid in by_id:
                st["duplicate_award_ids"] += 1
            by_id[aid] = [a]
    ordered = [(aid, a) for aid, group in by_id.items() for a in group] + [(None, a) for a in keyless]
    for aid in {aid for aid, _ in ordered}:
        conn.execute(DELETE_AWARD, (ocid, aid))
    rows = []
    for aid, a in ordered:
        av = _d(a.get("value"))
        amt, cur, when = amount(av.get("amount")), _s(av.get("currency")), date_of(a.get("date"))
        for sp in [_d(x) for x in _l(a.get("suppliers"))] or [{}]:      # empty supplier arrays are common
            rows.append((ocid, aid, _s(sp.get("id")), _s(sp.get("name")), amt, cur, when))
    conn.executemany(INSERT_AWARD, rows)
    st["awards"] += len(rows)