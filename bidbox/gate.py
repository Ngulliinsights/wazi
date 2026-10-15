"""Human approval gate. Deliberately NOT exposed as an MCP tool: the agent cannot approve itself.
A decision is bound to CONTENT: the brief must still be byte-identical to what was filed, and (when the caller supplies it)
to the fingerprint the human actually reviewed."""
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone

from . import audit_log, config
from .textsafe import oneline

AGENT_NAMES = {"agent", "ai", "claude", "gpt", "llm", "bot", "assistant", "system", "auto", "qwen", "llama"}
FILING_ID = re.compile(r"^[0-9a-f]{10}$")      # ids are minted by us; anything else is never turned into a path
MIN_HASH_PREFIX = 12


class ConflictError(Exception):
    """The brief is not what was filed / not what the approver reviewed."""


def _check_approver(name: str) -> str:
    n = oneline(name)                            # NFKC: full-width / zero-width spellings of 'agent' no longer slip through
    if len(n) < 3 or not any(c.isalpha() for c in n) or n.casefold() in AGENT_NAMES:
        raise PermissionError("a named human approver is required")
    return n


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def brief_sha256(filing_id: str) -> str:
    if not isinstance(filing_id, str) or not FILING_ID.match(filing_id):
        raise FileNotFoundError(f"no filing {filing_id!r}")
    for d in ("pending", "filed", "rejected"):
        p = config.OUTBOX / d / f"{filing_id}.md"
        if p.exists():
            return _sha(p)
    raise FileNotFoundError(f"no filing {filing_id}")


def list_pending() -> list[dict]:
    out = []
    for p in sorted((config.OUTBOX / "pending").glob("*.json")):
        if p.name.endswith((".manifest.json", ".decision.json")):
            continue
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            rec["md_sha256"] = _sha(p.with_suffix(".md"))      # always the CURRENT content, never a stored claim
            out.append(rec)
        except (OSError, ValueError):
            continue                              # a half-written filing must not break the list
    return out


def _move(fid: str, dest: str, who: str, action: str, reason: str | None = None, confirmed: str | None = None) -> dict:
    if not isinstance(fid, str) or not FILING_ID.match(fid):
        raise FileNotFoundError(f"no pending filing {fid!r}")
    src = config.OUTBOX / "pending"
    if not (src / f"{fid}.json").exists():
        raise FileNotFoundError(f"no pending filing {fid}")
    cur = _sha(src / f"{fid}.md")
    man = src / f"{fid}.manifest.json"
    if man.exists() and json.loads(man.read_text(encoding="utf-8")).get("md_sha256") != cur:
        raise ConflictError("the brief file changed after it was filed; refusing to decide on altered content")
    if confirmed is not None:
        c = str(confirmed).strip().lower()
        if len(c) < MIN_HASH_PREFIX or not cur.startswith(c):
            raise ConflictError("the brief on file is not the brief you reviewed (content fingerprint mismatch); reload and review it again")
    out = config.OUTBOX / dest
    for ext in ("md", "json", "manifest.json"):
        if (src / f"{fid}.{ext}").exists():
            shutil.move(str(src / f"{fid}.{ext}"), str(out / f"{fid}.{ext}"))
    rec = {"tool": "human_approval", "action": action, "filing_id": fid, "approver": who, "md_sha256": cur,
           "manifest_sha256": _sha(out / f"{fid}.manifest.json") if (out / f"{fid}.manifest.json").exists() else None,
           "confirmed_sha256": confirmed, "reason": oneline(reason) if reason else None, "decided_at": datetime.now(timezone.utc).isoformat()}
    (out / f"{fid}.decision.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
    audit_log.write(rec)
    return rec


def approve(filing_id: str, approver: str, brief_sha256: str | None = None) -> dict:
    return _move(filing_id, "filed", _check_approver(approver), "approved", None, brief_sha256)


def reject(filing_id: str, approver: str, reason: str = "", brief_sha256: str | None = None) -> dict:
    return _move(filing_id, "rejected", _check_approver(approver), "rejected", reason, brief_sha256)
