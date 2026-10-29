"""Thin HTTP layer for the one-page UI. Run: python -m bidbox.api   (BIDBOX_LLM=stub for no model)

The approval gate is reachable only here, by a person; the agent has no path to it.
There is no authentication: the server binds to 127.0.0.1 and that is the access control. Do not expose it.

Environment: BIDBOX_LLM (ollama|stub), BIDBOX_AS_OF, BIDBOX_MAX_RUNS (default 1), BIDBOX_RUN_TIMEOUT (seconds, default 900), PORT.
"""
import asyncio
import os
import uuid
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audit_log, gate, policy, provenance
from .agent import run_agent
from .agent.llm import OllamaLLM, QuotaExhausted, StubLLM
from .store import connect

app = FastAPI(title="WAZI")

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

PAGE = Path(__file__).parent / "static" / "index.html"
MODES = ("loop", "pipeline")
AUDIENCES = ("buyer", "supplier")
RESULT_KEYS = ("status", "filing_id", "summary", "used_fallback", "llm", "mode", "seconds", "llm_calls", "error",
               "md_sha256", "filing_error")
TENDER_KEYS = ("ocid", "title", "buyer_name", "buyer_id", "category", "deadline")
MAX_RUNS_KEPT = 500          # finished runs are evicted oldest-first beyond this, so the process doesn't grow forever

RUNS: dict[str, dict] = {}
TASKS: set[asyncio.Task] = set()
_SEM: tuple | None = None


# --------------------------------------------------------------------------- settings


def llm_name() -> str:
    return os.environ.get("BIDBOX_LLM", "ollama")


def default_as_of() -> str:
    return os.environ.get("BIDBOX_AS_OF", date.today().isoformat())


def make_llm():
    return StubLLM("invest_all") if llm_name() == "stub" else OllamaLLM()


def _sem() -> asyncio.Semaphore:
    """One model run at a time by default: parallel runs thrash a laptop GPU and make every run slower. Bound to the running loop."""
    global _SEM
    loop = asyncio.get_running_loop()
    if _SEM is None or _SEM[0] is not loop:
        _SEM = (loop, asyncio.Semaphore(int(os.environ.get("BIDBOX_MAX_RUNS", "1"))))
    return _SEM[1]


# --------------------------------------------------------------------------- request models


class RunReq(BaseModel):
    ocid: str
    mode: str = "loop"
    audience: str = "buyer"
    as_of: str | None = None
    request: str = "Prepare a tender readiness brief"


class Approval(BaseModel):
    approver: str
    reason: str = ""
    brief_sha256: str | None = None     # REQUIRED to decide: the fingerprint of the brief the person actually reviewed


# --------------------------------------------------------------------------- pages and lookups


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE.read_text(encoding="utf-8")


@app.get("/api/config")
def cfg():
    return {"llm": llm_name(), "as_of": default_as_of(), "replay": True}


def _like(q: str) -> str:
    """Contains-match pattern with LIKE wildcards in the user's text escaped (paired with ESCAPE '\\')."""
    return "%" + q.strip()[:100].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


@app.get("/api/tenders")
def tenders(q: str = ""):
    pat = _like(q)
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT ocid, title, buyer_name, buyer_id, category, deadline, status, synthetic FROM tenders "
            "WHERE ocid LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' ORDER BY ocid LIMIT 60", (pat, pat)).fetchall()
    return [{**{k: r[k] for k in TENDER_KEYS}, "live": bool(policy.live_reason(r["status"], r["deadline"], r["synthetic"]))}
            for r in rows]


# --------------------------------------------------------------------------- runs


def _finish(run_id: str, **fields) -> None:
    RUNS[run_id] = {"run_id": run_id, "done": True, **fields}


def _prune_runs() -> None:
    excess = len(RUNS) - MAX_RUNS_KEPT
    if excess > 0:
        for rid in [r for r, v in RUNS.items() if v.get("done")][:excess]:      # dicts keep insertion order: oldest first
            del RUNS[rid]


async def _go(run_id: str, req: RunReq) -> None:
    try:
        timeout = float(os.environ.get("BIDBOX_RUN_TIMEOUT", "900"))
        async with _sem():
            RUNS[run_id]["status"] = "running"
            res = await asyncio.wait_for(
                run_agent(req.ocid, req.request, req.as_of or default_as_of(), make_llm(), req.mode,
                          run_id=run_id, audience=req.audience), timeout)
        _finish(run_id, **{k: res.get(k) for k in RESULT_KEYS})
    except QuotaExhausted as e:
        _finish(run_id, status="quota_exhausted", error=str(e)[:200])
    except asyncio.TimeoutError:
        _finish(run_id, status="timeout", error=f"the model did not finish within {timeout:g} seconds")
    except Exception as e:  # noqa: BLE001 - surface, never hang the UI
        _finish(run_id, status="error", error=repr(e)[:200])


@app.post("/api/runs")
async def start(req: RunReq):
    if req.mode not in MODES:
        raise HTTPException(422, "mode must be loop or pipeline")
    if req.audience not in AUDIENCES:
        raise HTTPException(422, "audience must be buyer or supplier")
    req.ocid = req.ocid.strip()
    if not req.ocid or len(req.ocid) > 200:
        raise HTTPException(422, "ocid is required (at most 200 characters)")
    if req.as_of:
        try:
            datetime.fromisoformat(req.as_of.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(422, "as_of must be an ISO date such as 2025-06-01")
    _prune_runs()
    rid = uuid.uuid4().hex[:12]
    RUNS[rid] = {"run_id": rid, "done": False, "status": "queued"}
    task = asyncio.create_task(_go(rid, req))
    TASKS.add(task)                                  # hold a reference so the task isn't garbage-collected mid-run
    task.add_done_callback(TASKS.discard)
    return dict(RUNS[rid])


def _known_run(rid: str) -> dict:
    run = RUNS.get(rid)
    if run is None:
        raise HTTPException(404, "unknown run")
    return run


@app.get("/api/runs/{rid}")
def run(rid: str):
    return _known_run(rid)


@app.get("/api/runs/{rid}/events")
def events(rid: str, after: int = 0):
    _known_run(rid)
    out = [r for r in audit_log.read_all(rid) if "event" in r]
    return {"events": out[max(0, after):], "next": len(out)}


# --------------------------------------------------------------------------- filings and decisions


@app.get("/api/filings")
def filings():
    from . import config
    out = []
    for f in gate.list_pending():
        p = config.OUTBOX / "pending" / f"{f['filing_id']}.md"
        if p.exists():
            out.append({**f, "markdown": p.read_text(encoding="utf-8")})
    return out


def _decide(fn, fid, *args):
    """Run a gate decision, mapping its errors to HTTP status codes. The gate itself stays the authority."""
    try:
        return fn(fid, *args)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except gate.ConflictError as e:
        raise HTTPException(409, str(e))


def _need_hash(body: Approval) -> str:
    if not body.brief_sha256:
        raise HTTPException(422, "brief_sha256 is required: a decision must name the exact brief that was reviewed")
    return body.brief_sha256


@app.post("/api/filings/{fid}/approve")
def approve(fid: str, body: Approval):
    return _decide(gate.approve, fid, body.approver, _need_hash(body))


@app.post("/api/filings/{fid}/reject")
def reject(fid: str, body: Approval):
    return _decide(gate.reject, fid, body.approver, body.reason, _need_hash(body))


@app.get("/api/filings/{fid}/verify")
def verify(fid: str):
    rep = provenance.verify_filing(fid)
    if rep["verdict"] == "NOT FOUND":
        raise HTTPException(404, "no such filing")
    return rep


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))