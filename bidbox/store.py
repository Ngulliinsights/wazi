"""Normalise OCDS release packages into SQLite. Messy data is flagged, never silently fixed."""
import json
import sqlite3
from pathlib import Path

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
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if "start" not in {r[1] for r in conn.execute("PRAGMA table_info(tenders)")}:   # databases created before windows existed
        conn.execute("ALTER TABLE tenders ADD COLUMN start TEXT")
        conn.commit()
    if "synthetic" not in {r[1] for r in conn.execute("PRAGMA table_info(tenders)")}:
        conn.execute("ALTER TABLE tenders ADD COLUMN synthetic INTEGER DEFAULT 0")
        conn.commit()
    return conn


BAD = object()  # sentinel for an unparseable line


def iter_releases(path, meta=None):
    """Yield release dicts from: JSON Lines (streamed), a release package, a record package, or a bare list; .gz ok."""
    import gzip
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        first = f.readline()
        try:
            obj = json.loads(first)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and "releases" not in obj and "records" not in obj:  # JSON Lines: stream it
            yield obj
            for line in f:
                if line.strip():
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        yield BAD
            return
        try:
            doc = json.loads(first + f.read())
        except json.JSONDecodeError:
            yield BAD
            return
    if meta is not None and isinstance(doc, dict):
        meta["publisher"] = doc.get("publisher")                  # lets the loader tell synthetic data from real
    items = doc if isinstance(doc, list) else (doc.get("releases") or []) if isinstance(doc, dict) and "records" not in doc else []
    if isinstance(doc, dict) and "records" in doc:  # record package: use the compiled release
        for rec in doc["records"] or []:
            rec = rec if isinstance(rec, dict) else {}
            rels = rec.get("releases") if isinstance(rec.get("releases"), list) else []
            yield rec.get("compiledRelease") or (rels[-1] if rels else BAD)
        return
    yield from items


def _d(x): return x if isinstance(x, dict) else {}
def _l(x): return x if isinstance(x, list) else []
def _s(x): return str(x) if isinstance(x, (str, int, float)) and not isinstance(x, bool) and str(x).strip() else None


def _iso(x):
    from datetime import datetime
    if not isinstance(x, str):
        return None
    try:
        datetime.fromisoformat(x.replace("Z", "+00:00"))
        return x
    except ValueError:
        return None


def load_ocds(json_path: Path | str, conn: sqlite3.Connection | None = None, synthetic: bool | None = None) -> dict:
    """Load real-world-messy OCDS. Bad records are skipped and COUNTED, never silently fixed:
    numbers stored as text are coerced (counted); non-positive or non-numeric amounts become unknown."""
    import math
    conn = conn or connect()
    st = {"tenders": 0, "awards": 0, "skipped": 0, "coerced_numbers": 0, "invalid_amounts": 0, "bad_dates": 0, "inverted_windows": 0, "duplicates": 0}
    seen = set()

    def amount(x):
        if isinstance(x, bool) or x is None:
            return None
        if isinstance(x, str):
            try:
                v = float(x.replace(",", "").strip())
            except ValueError:
                st["invalid_amounts"] += 1; return None
            st["coerced_numbers"] += 1
        elif isinstance(x, (int, float)):
            v = float(x)
        else:
            st["invalid_amounts"] += 1; return None
        if not math.isfinite(v) or v <= 0:
            st["invalid_amounts"] += 1; return None
        return v

    meta: dict = {}
    for r in iter_releases(json_path, meta):
        is_synth = int(synthetic) if synthetic is not None else int("synthetic" in json.dumps(meta.get("publisher") or {}).lower())
        if r is BAD or not isinstance(r, dict) or not _s(r.get("ocid")):
            st["skipped"] += 1; continue
        ocid = _s(r["ocid"])
        st["duplicates"] += ocid in seen; seen.add(ocid)
        t, b = _d(r.get("tender")), _d(r.get("buyer"))
        deadline = _iso(_d(t.get("tenderPeriod")).get("endDate"))
        if _d(t.get("tenderPeriod")).get("endDate") and not deadline:
            st["bad_dates"] += 1
        docs = [d["url"] for d in map(_d, _l(t.get("documents"))) if isinstance(d.get("url"), str)]
        tv = _d(t.get("value"))
        raw_start = _d(t.get("tenderPeriod")).get("startDate")
        start = _iso(raw_start)
        if raw_start and not start:
            st["bad_dates"] += 1
        if start and deadline and start > deadline:   # ISO strings in one format sort correctly; flag, never silently fix
            st["inverted_windows"] += 1
        # A tender arrives as SEVERAL releases (tender stage, award stage, contract stage...). Later releases add or change fields;
        # a field a later release does not mention is unchanged, never erased.
        conn.execute("INSERT INTO tenders(ocid,title,description,status,category,method,deadline,start,value,currency,eligibility,buyer_id,buyer_name,doc_urls,synthetic) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(ocid) DO UPDATE SET "
                     + ",".join(f"{c}=COALESCE(excluded.{c},tenders.{c})" for c in ("title", "description", "status", "category", "method", "deadline", "start", "value", "currency", "eligibility", "buyer_id", "buyer_name"))
                     + ",doc_urls=CASE WHEN excluded.doc_urls IS NOT NULL AND excluded.doc_urls<>'[]' THEN excluded.doc_urls ELSE tenders.doc_urls END,synthetic=excluded.synthetic",
                     (ocid, _s(t.get("title")), _s(t.get("description")), _s(t.get("status")),
                      _s(t.get("mainProcurementCategory")), _s(t.get("procurementMethod")), deadline, start,
                      amount(tv.get("amount")), _s(tv.get("currency")), _s(t.get("eligibilityCriteria")),
                      _s(b.get("id")), _s(b.get("name")), json.dumps(docs), is_synth))
        st["tenders"] += 1
        for a in map(_d, _l(r.get("awards"))):
            conn.execute("DELETE FROM awards WHERE ocid=? AND award_id IS ?", (ocid, _s(a.get("id"))))      # an award is replaced by id, never wiped with its siblings
            sups = [x for x in map(_d, _l(a.get("suppliers")))] or [{}]  # empty supplier arrays are common
            av = _d(a.get("value"))
            for sp in sups:
                conn.execute("INSERT INTO awards VALUES(?,?,?,?,?,?,?)",
                             (ocid, _s(a.get("id")), _s(sp.get("id")), _s(sp.get("name")),
                              amount(av.get("amount")), _s(av.get("currency")), _iso(a.get("date"))))
                st["awards"] += 1
    import hashlib
    from datetime import datetime, timezone
    h, size = hashlib.sha256(), 0
    with open(json_path, "rb") as fh:                      # streamed: real exports are large
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk); size += len(chunk)
    conn.execute("INSERT INTO loads(file, sha256, bytes, tenders, awards, loaded_at) VALUES(?,?,?,?,?,?)",
                 (Path(json_path).name, h.hexdigest(), size, st["tenders"], st["awards"], datetime.now(timezone.utc).isoformat()))
    st["sha256"] = h.hexdigest()
    conn.commit()
    return st
