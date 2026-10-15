"""Provenance: everything needed to defend, reproduce and audit a filed brief.
brief (md) <- hash in manifest <- hash in decision. The manifest records the run, data version, code version, model and
settings, every read-tool call (with output hashes that must REPLAY), the evidence, the findings and the cost."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

READ_TOOLS = ("get_tender_notice", "buyer_award_history", "price_benchmark", "peer_baseline", "window_baseline")
MAX_TEXT = 20000


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha(x) -> str:
    return hashlib.sha256((x if isinstance(x, str) else canonical(x)).encode("utf-8")).hexdigest()


def sha_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def roundtrip(obj):
    return json.loads(json.dumps(obj, default=str))


_CODE = None


def code_version() -> str:
    """Hash of the package source: a result is only 'the same' if the code that made it is the same."""
    global _CODE
    if _CODE is None:
        h, root = hashlib.sha256(), Path(__file__).parent
        for p in sorted(root.rglob("*.py")):
            h.update(str(p.relative_to(root)).encode()); h.update(p.read_bytes())
        _CODE = h.hexdigest()[:16]
    return _CODE


def dataset(conn) -> dict:
    """Identity of the data: ordered distinct file hashes of everything loaded (re-loading the same file changes nothing)."""
    seen, loads = set(), []
    for r in conn.execute("SELECT file, sha256, tenders, awards FROM loads ORDER BY id"):
        if r["sha256"] not in seen:
            seen.add(r["sha256"]); loads.append({"file": r["file"], "sha256": r["sha256"], "tenders": r["tenders"], "awards": r["awards"]})
    return {"fingerprint": sha([x["sha256"] for x in loads])[:16] if loads else "unrecorded", "loads": loads}


def replay(tool: str, args: dict):
    from . import tools
    fn = getattr(tools, tool)
    return roundtrip(getattr(fn, "__wrapped__", fn)(**args))      # unlogged call: replaying must not pollute the audit log


def replay_mismatches(calls: list[dict]) -> list[str]:
    out = []
    for c in calls:
        if c.get("tool") not in READ_TOOLS:
            out.append(f"{c.get('tool')}: not a replayable read tool"); continue
        try:
            got = sha(replay(c["tool"], c.get("args") or {}))
        except Exception as e:  # noqa: BLE001
            out.append(f"{c['tool']}: replay raised {e!r}"); continue
        if got != c.get("output_sha256"):
            out.append(f"{c['tool']}({canonical(c.get('args'))}): output differs from what the agent recorded")
    return out


def _cap(t) -> dict:
    t = t if isinstance(t, str) else canonical(t)
    return {"sha256": sha(t), "text": t[:MAX_TEXT], "truncated": len(t) > MAX_TEXT}


def make_manifest(*, fid, run_id, ocid, audience, as_of, md_sha256, ds, accepted, provenance, replay_ok) -> dict:
    p = provenance or {}
    return {"schema": 1, "filing_id": fid, "run_id": run_id, "ocid": ocid, "audience": audience, "as_of": as_of,
            "created": datetime.now(timezone.utc).isoformat(), "md_sha256": md_sha256, "dataset": ds, "code_version": code_version(),
            "provenance_supplied": bool(provenance), "replay": {"checked": len(p.get("tool_calls", [])), "ok": replay_ok},
            "run": p.get("run"), "tool_calls": p.get("tool_calls", []), "documents": p.get("documents", []), "notes": p.get("notes", []),
            "evidence": p.get("evidence", []),
            "model_calls": [{"task": m.get("task"), "prompt": _cap(m.get("prompt", "")), "response": _cap(m.get("response", "")),
                             "tokens_in": m.get("tokens_in"), "tokens_out": m.get("tokens_out"), "cached": bool(m.get("cached"))} for m in p.get("model_calls", [])],
            "findings": [f.model_dump() for f in accepted]}


def find_files(fid: str):
    from . import config
    for d in ("pending", "filed", "rejected"):
        base = config.OUTBOX / d
        if (base / f"{fid}.json").exists():
            return d, base
    return None, None


def verify_filing(fid: str) -> dict:
    """Independent audit of a filing: integrity, replay, evidence re-derivation, grounding, model trace. Never trusts the stored claims."""
    from . import gate, tools
    from .agent import evidence as ev
    from .findings import has_verdict, validate_citation
    from .store import connect
    checks = []
    def add(name, ok, detail="", kind="integrity"): checks.append({"check": name, "ok": bool(ok), "detail": detail, "kind": kind})
    if not gate.FILING_ID.match(fid or ""):
        return {"filing_id": fid, "verdict": "NOT FOUND", "checks": []}
    where, base = find_files(fid)
    if not where:
        return {"filing_id": fid, "verdict": "NOT FOUND", "checks": []}
    mpath, mdpath = base / f"{fid}.manifest.json", base / f"{fid}.md"
    if not mpath.exists():
        return {"filing_id": fid, "state": where, "verdict": "NOT REPRODUCIBLE (no manifest)", "checks": [{"check": "manifest present", "ok": False, "detail": "filed without provenance", "kind": "integrity"}]}
    m = json.loads(mpath.read_text(encoding="utf-8"))
    add("brief file matches the fingerprint in the manifest", sha_file(mdpath) == m["md_sha256"], "the brief was edited after filing" if sha_file(mdpath) != m["md_sha256"] else "")
    dec = base / f"{fid}.decision.json"
    if dec.exists():
        d = json.loads(dec.read_text(encoding="utf-8"))
        add("decision names the same brief and manifest", d.get("md_sha256") == sha_file(mdpath) and d.get("manifest_sha256") == sha_file(mpath), "decision/brief/manifest hashes disagree")
        add("approver is a named person", bool(d.get("approver")) and d.get("approver").casefold() not in gate.AGENT_NAMES)
    conn = connect(); ds = dataset(conn)
    same_data = ds["fingerprint"] == m["dataset"]["fingerprint"]
    add("data version unchanged since filing", same_data, f"filed on {m['dataset']['fingerprint']}, now {ds['fingerprint']}", "data")
    add("code version unchanged since filing", code_version() == m["code_version"], f"filed on {m['code_version']}, now {code_version()}", "data")
    if not m.get("provenance_supplied"):
        return {"filing_id": fid, "state": where, "verdict": "NOT REPRODUCIBLE (no provenance supplied)", "checks": checks}
    mism = replay_mismatches(m["tool_calls"])
    add(f"all {len(m['tool_calls'])} tool calls replay to identical output", not mism, "; ".join(mism[:3]), "replay")
    # re-derive the evidence from replayed data + the recorded documents/notes, never from stored evidence
    try:
        by_role = {}
        for c in m["tool_calls"]:
            by_role.setdefault(c["role"], replay(c["tool"], c["args"]))
        if "notice" in by_role:
            fresh = ev.build_evidence(by_role["notice"], by_role.get("history"), by_role.get("benchmark"), m["notes"], m["documents"],
                                      by_role.get("peers"), by_role.get("history_wide"), by_role.get("window"))
            same = [(e["id"], e["kind"], e["source_ocid"], e["field_path"], e["text"], e["signal"]) for e in fresh] == \
                   [(e["id"], e["kind"], e["source_ocid"], e["field_path"], e["text"], e["signal"]) for e in m["evidence"]]
            add("evidence is exactly what the data and documents produce", same, "recorded evidence differs from re-derived evidence", "replay")
        else:
            add("evidence is exactly what the data and documents produce", False, "no notice call recorded")
    except Exception as e:  # noqa: BLE001
        add("evidence is exactly what the data and documents produce", False, f"re-derivation failed: {e!r}", "replay")
    by_id = {e["id"]: e for e in m["evidence"]}
    bad = []
    for f in m["findings"]:
        ids = f.get("evidence") or []
        if not ids or any(i not in by_id for i in ids):
            bad.append(f"finding without valid evidence: {f['statement'][:50]}"); continue
        if ev.ungrounded_numbers(f["statement"], [by_id[i]["text"] for i in ids]):
            bad.append(f"ungrounded numbers: {f['statement'][:50]}")
        if has_verdict(f["statement"]):
            bad.append(f"verdict language: {f['statement'][:50]}")
        if f["severity"] == "signal" and not any(by_id[i]["signal"] for i in ids):
            bad.append(f"signal not raised by the data: {f['statement'][:50]}")
        why = validate_citation(conn, f["kind"], f["source_ocid"], f["field_path"])
        if why and same_data:
            bad.append(f"citation invalid ({why}): {f['statement'][:40]}")
    add(f"all {len(m['findings'])} findings are grounded, sourced and free of verdicts", not bad, "; ".join(bad[:3]), "grounding")
    mc_bad = [x["task"] for x in m["model_calls"] if not x["prompt"]["truncated"] and sha(x["prompt"]["text"]) != x["prompt"]["sha256"]]
    add("model prompts and responses are intact", not mc_bad, f"tampered: {mc_bad}")
    integrity_ok = all(c["ok"] for c in checks if c["kind"] in ("integrity", "grounding"))
    replay_ok = all(c["ok"] for c in checks if c["kind"] == "replay")
    if integrity_ok and replay_ok and same_data:
        verdict = "REPRODUCED"
    elif integrity_ok and not same_data:
        verdict = "INTEGRITY OK; DATA CHANGED SINCE FILING (replay not expected to match)"
    else:
        verdict = "FAILED"
    return {"filing_id": fid, "state": where, "verdict": verdict, "checks": checks, "run_id": m["run_id"], "dataset": m["dataset"]["fingerprint"], "code_version": m["code_version"]}
