"""Provenance: everything needed to defend, reproduce and audit a filed brief.

    brief (md) <- hash in manifest <- hash in decision

The manifest records the run, data version, code version, model and settings, every read-tool call
(with output hashes that must REPLAY), the evidence, the findings and the cost.

Stability contract: `canonical`, `sha`, the dataset fingerprint and the manifest schema are stored in
filed manifests, so their formats must not change without bumping `schema`.
"""
import hashlib
import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

READ_TOOLS = ("get_tender_notice", "buyer_award_history", "price_benchmark", "peer_baseline", "window_baseline")
MAX_TEXT = 20000
SCHEMA = 1

# --------------------------------------------------------------------------- hashing


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha(x) -> str:
    return hashlib.sha256((x if isinstance(x, str) else canonical(x)).encode("utf-8")).hexdigest()


def sha_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def roundtrip(obj):
    """Normalise to what JSON storage would return, so replayed output hashes like the recorded one."""
    return json.loads(json.dumps(obj, default=str))


# --------------------------------------------------------------------------- code / data identity

_CODE = None


def code_version() -> str:
    """Hash of the package source: a result is only 'the same' if the code that made it is the same."""
    global _CODE
    if _CODE is None:
        h, root = hashlib.sha256(), Path(__file__).parent
        for p in sorted(root.rglob("*.py")):
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
        _CODE = h.hexdigest()[:16]
    return _CODE


def dataset(conn) -> dict:
    """Identity of the data: ordered distinct file hashes of everything loaded (re-loading the same file changes nothing)."""
    seen, loads = set(), []
    for r in conn.execute("SELECT file, sha256, tenders, awards FROM loads ORDER BY id"):
        if r["sha256"] not in seen:
            seen.add(r["sha256"])
            loads.append({"file": r["file"], "sha256": r["sha256"], "tenders": r["tenders"], "awards": r["awards"]})
    return {"fingerprint": sha([x["sha256"] for x in loads])[:16] if loads else "unrecorded", "loads": loads}


# --------------------------------------------------------------------------- replay


def replay(tool: str, args: dict):
    """Re-run a read tool without logging. Only whitelisted read tools can be replayed, so a doctored
    manifest cannot make the auditor call arbitrary functions."""
    if tool not in READ_TOOLS:
        raise ValueError(f"{tool!r} is not a replayable read tool")
    from . import tools
    fn = getattr(tools, tool)
    return roundtrip(getattr(fn, "__wrapped__", fn)(**args))      # unlogged call: replaying must not pollute the audit log


def _replay_all(calls: list[dict]) -> list[tuple[bool, object]]:
    """Replay every call exactly once -> [(ok, output_or_error_message)], aligned with `calls`."""
    out = []
    for c in calls:
        tool = c.get("tool")
        if tool not in READ_TOOLS:
            out.append((False, f"{tool}: not a replayable read tool"))
            continue
        try:
            out.append((True, replay(tool, c.get("args") or {})))
        except Exception as e:  # noqa: BLE001
            out.append((False, f"{tool}: replay raised {e!r}"))
    return out


def replay_mismatches(calls: list[dict], results: list[tuple[bool, object]] | None = None) -> list[str]:
    """Descriptions of every call whose replay fails or hashes differently. Pass `results` to reuse a prior replay."""
    results = _replay_all(calls) if results is None else results
    out = []
    for c, (ok, val) in zip(calls, results):
        if not ok:
            out.append(str(val))
        elif sha(val) != c.get("output_sha256"):
            out.append(f"{c.get('tool')}({canonical(c.get('args'))}): output differs from what the agent recorded")
    return out


# --------------------------------------------------------------------------- manifest


def _cap(t) -> dict:
    t = t if isinstance(t, str) else canonical(t)
    return {"sha256": sha(t), "text": t[:MAX_TEXT], "truncated": len(t) > MAX_TEXT}


def make_manifest(*, fid, run_id, ocid, audience, as_of, md_sha256, ds, accepted, provenance, replay_ok) -> dict:
    p = provenance or {}
    tool_calls = p.get("tool_calls") or []
    return {
        "schema": SCHEMA, "filing_id": fid, "run_id": run_id, "ocid": ocid, "audience": audience, "as_of": as_of,
        "created": datetime.now(timezone.utc).isoformat(), "md_sha256": md_sha256, "dataset": ds, "code_version": code_version(),
        "provenance_supplied": bool(provenance), "replay": {"checked": len(tool_calls), "ok": replay_ok},
        "run": p.get("run"), "tool_calls": tool_calls, "documents": p.get("documents") or [], "notes": p.get("notes") or [],
        "evidence": p.get("evidence") or [],
        "model_calls": [
            {"task": m.get("task"), "prompt": _cap(m.get("prompt", "")), "response": _cap(m.get("response", "")),
             "tokens_in": m.get("tokens_in"), "tokens_out": m.get("tokens_out"), "cached": bool(m.get("cached"))}
            for m in p.get("model_calls") or []
        ],
        "findings": [f.model_dump() for f in accepted],
    }


def find_files(fid: str):
    """Locate a filing's folder -> (state, path), or (None, None). The id is validated before touching the filesystem."""
    from . import config, gate
    if not gate.FILING_ID.match(fid or ""):
        return None, None
    for d in ("pending", "filed", "rejected"):
        base = config.OUTBOX / d
        if (base / f"{fid}.json").exists():
            return d, base
    return None, None


# --------------------------------------------------------------------------- audit


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as e:
        return None, repr(e)


def _ev_key(e: dict) -> tuple:
    return (e["id"], e["kind"], e["source_ocid"], e["field_path"], e["text"], e["signal"])


def verify_filing(fid: str) -> dict:
    """Independent audit of a filing: integrity, replay, evidence re-derivation, grounding, model trace.
    Never trusts the stored claims."""
    from . import gate
    from .agent import evidence as ev
    from .findings import has_verdict, validate_citation
    from .store import connect

    checks = []

    def add(name, ok, detail="", kind="integrity"):
        checks.append({"check": name, "ok": bool(ok), "detail": detail, "kind": kind})

    where, base = find_files(fid)
    if not where:
        return {"filing_id": fid, "verdict": "NOT FOUND", "checks": []}

    mpath, mdpath = base / f"{fid}.manifest.json", base / f"{fid}.md"
    if not mpath.exists():
        return {"filing_id": fid, "state": where, "verdict": "NOT REPRODUCIBLE (no manifest)",
                "checks": [{"check": "manifest present", "ok": False, "detail": "filed without provenance", "kind": "integrity"}]}

    m, err = _load_json(mpath)
    if not isinstance(m, dict):
        return {"filing_id": fid, "state": where, "verdict": "FAILED",
                "checks": [{"check": "manifest is readable", "ok": False, "detail": err or "not a JSON object", "kind": "integrity"}]}

    md_hash = sha_file(mdpath) if mdpath.exists() else None
    manifest_hash = sha_file(mpath)
    add("brief file matches the fingerprint in the manifest", md_hash is not None and md_hash == m.get("md_sha256"),
        "brief file is missing" if md_hash is None else
        "the brief was edited after filing" if md_hash != m.get("md_sha256") else "")

    dec = base / f"{fid}.decision.json"
    if dec.exists():
        d, err = _load_json(dec)
        if isinstance(d, dict):
            add("decision names the same brief and manifest",
                md_hash is not None and d.get("md_sha256") == md_hash and d.get("manifest_sha256") == manifest_hash,
                "decision/brief/manifest hashes disagree")
            approver = d.get("approver")
            add("approver is a named person",
                isinstance(approver, str) and bool(approver.strip()) and approver.strip().casefold() not in gate.AGENT_NAMES)
        else:
            add("decision file is readable", False, err or "not a JSON object")

    with closing(connect()) as conn:
        ds = dataset(conn)
        filed_fp = (m.get("dataset") or {}).get("fingerprint")
        same_data = ds["fingerprint"] == filed_fp
        add("data version unchanged since filing", same_data, f"filed on {filed_fp}, now {ds['fingerprint']}", "data")
        add("code version unchanged since filing", code_version() == m.get("code_version"),
            f"filed on {m.get('code_version')}, now {code_version()}", "data")

        if not m.get("provenance_supplied"):
            return {"filing_id": fid, "state": where, "verdict": "NOT REPRODUCIBLE (no provenance supplied)", "checks": checks}

        calls = m.get("tool_calls") or []
        results = _replay_all(calls)                                   # each call replays exactly once
        mism = replay_mismatches(calls, results)
        add(f"all {len(calls)} tool calls replay to identical output", not mism, "; ".join(mism[:3]), "replay")

        # Re-derive the evidence from replayed data + the recorded documents/notes, never from stored evidence.
        try:
            by_role, seen = {}, set()
            for c, (ok, val) in zip(calls, results):
                role = c.get("role")
                if role in seen:
                    continue
                seen.add(role)
                if ok:
                    by_role[role] = val
            if "notice" in by_role:
                fresh = ev.build_evidence(by_role["notice"], by_role.get("history"), by_role.get("benchmark"),
                                          m.get("notes") or [], m.get("documents") or [], by_role.get("peers"),
                                          by_role.get("history_wide"), by_role.get("window"))
                same = [_ev_key(e) for e in fresh] == [_ev_key(e) for e in m.get("evidence") or []]
                add("evidence is exactly what the data and documents produce", same,
                    "" if same else "recorded evidence differs from re-derived evidence", "replay")
            else:
                add("evidence is exactly what the data and documents produce", False, "no replayable notice call recorded")
        except Exception as e:  # noqa: BLE001
            add("evidence is exactly what the data and documents produce", False, f"re-derivation failed: {e!r}", "replay")

        by_id = {e["id"]: e for e in m.get("evidence") or []}
        bad = []
        findings = m.get("findings") or []
        for f in findings:
            label = (f.get("statement") or "")[:50]
            ids = f.get("evidence") or []
            if not ids or any(i not in by_id for i in ids):
                bad.append(f"finding without valid evidence: {label}")
                continue
            if ev.ungrounded_numbers(f["statement"], [by_id[i]["text"] for i in ids]):
                bad.append(f"ungrounded numbers: {label}")
            if has_verdict(f["statement"]):
                bad.append(f"verdict language: {label}")
            if f.get("severity") == "signal" and not any(by_id[i]["signal"] for i in ids):
                bad.append(f"signal not raised by the data: {label}")
            if same_data:
                why = validate_citation(conn, f["kind"], f["source_ocid"], f["field_path"])
                if why:
                    bad.append(f"citation invalid ({why}): {label[:40]}")
        add(f"all {len(findings)} findings are grounded, sourced and free of verdicts", not bad, "; ".join(bad[:3]), "grounding")

    # A capped (truncated) text cannot be re-hashed; everything else must match its recorded hash.
    mc_bad = [x.get("task") for x in m.get("model_calls") or []
              if any(not x[k]["truncated"] and sha(x[k]["text"]) != x[k]["sha256"] for k in ("prompt", "response"))]
    add("model prompts and responses are intact", not mc_bad, f"tampered: {mc_bad}" if mc_bad else "")

    integrity_ok = all(c["ok"] for c in checks if c["kind"] in ("integrity", "grounding"))
    replay_ok = all(c["ok"] for c in checks if c["kind"] == "replay")
    if integrity_ok and replay_ok and same_data:
        verdict = "REPRODUCED"
    elif integrity_ok and not same_data:
        verdict = "INTEGRITY OK; DATA CHANGED SINCE FILING (replay not expected to match)"
    else:
        verdict = "FAILED"
    return {"filing_id": fid, "state": where, "verdict": verdict, "checks": checks, "run_id": m.get("run_id"),
            "dataset": filed_fp, "code_version": m.get("code_version")}