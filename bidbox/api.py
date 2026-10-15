"""Thin HTTP layer for the one-page UI. Run: python -m bidbox.api   (BIDBOX_LLM=stub for no model)
The approval gate is reachable only here, by a person; the agent has no path to it."""
import asyncio
import os
import uuid
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import audit_log, gate, policy
from .agent import run_agent
from .agent.llm import OllamaLLM, QuotaExhausted, StubLLM
from . import provenance
from .store import connect

app = FastAPI(title="Bid Box")
RUNS: dict[str, dict] = {}
TASKS: set = set()
PAGE = Path(__file__).parent / "static" / "index.html"
_SEM: tuple | None = None


def _sem() -> asyncio.Semaphore:
    """One model run at a time by default: parallel runs thrash a laptop GPU and make every run slower. Bound to the running loop."""
    global _SEM
    loop = asyncio.get_running_loop()
    if _SEM is None or _SEM[0] is not loop:
        _SEM = (loop, asyncio.Semaphore(int(os.environ.get("BIDBOX_MAX_RUNS", "1"))))
    return _SEM[1]


def make_llm():
    return StubLLM("invest_all") if os.environ.get("BIDBOX_LLM", "ollama") == "stub" else OllamaLLM()


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


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE.read_text(encoding="utf-8")


@app.get("/api/config")
def cfg():
    return {"llm": os.environ.get("BIDBOX_LLM", "ollama"), "as_of": os.environ.get("BIDBOX_AS_OF", date.today().isoformat()),
            "replay": True}


@app.get("/api/tenders")
def tenders(q: str = ""):
    rows = connect().execute(
        "SELECT ocid, title, buyer_name, buyer_id, category, deadline, status, synthetic FROM tenders WHERE ocid LIKE ? OR title LIKE ? ORDER BY ocid LIMIT 60",
        (f"%{q}%", f"%{q}%")).fetchall()
    return [{**{k: r[k] for k in ("ocid", "title", "buyer_name", "buyer_id", "category", "deadline")},
             "live": bool(policy.live_reason(r["status"], r["deadline"], r["synthetic"]))} for r in rows]


async def _go(run_id: str, req: RunReq):
    timeout = float(os.environ.get("BIDBOX_RUN_TIMEOUT", "900"))
    try:
        async with _sem():
            RUNS[run_id]["status"] = "running"
            res = await asyncio.wait_for(run_agent(req.ocid, req.request, req.as_of or cfg()["as_of"], make_llm(), req.mode,
                                                   run_id=run_id, audience=req.audience), timeout)
        RUNS[run_id] = {"run_id": run_id, "done": True, **{k: res[k] for k in ("status", "filing_id", "summary", "used_fallback", "llm", "mode", "seconds", "llm_calls", "error", "md_sha256", "filing_error")}}
    except QuotaExhausted as e:
        RUNS[run_id] = {"run_id": run_id, "done": True, "status": "quota_exhausted", "error": str(e)[:200]}
    except asyncio.TimeoutError:
        RUNS[run_id] = {"run_id": run_id, "done": True, "status": "timeout", "error": f"the model did not finish within {timeout:g} seconds"}
    except Exception as e:  # noqa: BLE001 - surface, never hang the UI
        RUNS[run_id] = {"run_id": run_id, "done": True, "status": "error", "error": repr(e)[:200]}


@app.post("/api/runs")
async def start(req: RunReq):
    if req.mode not in ("loop", "pipeline"):
        raise HTTPException(422, "mode must be loop or pipeline")
    if req.audience not in ("buyer", "supplier"):
        raise HTTPException(422, "audience must be buyer or supplier")
    if not req.ocid.strip() or len(req.ocid) > 200:
        raise HTTPException(422, "ocid is required (at most 200 characters)")
    if req.as_of:
        try:
            datetime.fromisoformat(req.as_of.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(422, "as_of must be an ISO date such as 2025-06-01")
    rid = uuid.uuid4().hex[:12]
    RUNS[rid] = {"run_id": rid, "done": False, "status": "queued"}
    t = asyncio.create_task(_go(rid, req)); TASKS.add(t); t.add_done_callback(TASKS.discard)
    return RUNS[rid]


@app.get("/api/runs/{rid}")
def run(rid: str):
    if rid not in RUNS:
        raise HTTPException(404, "unknown run")
    return RUNS[rid]


@app.get("/api/runs/{rid}/events")
def events(rid: str, after: int = 0):
    if rid not in RUNS:
        raise HTTPException(404, "unknown run")
    out = [r for r in audit_log.read_all(rid) if "event" in r]
    return {"events": out[max(0, after):], "next": len(out)}


@app.get("/api/filings")
def filings():
    from . import config
    out = []
    for f in gate.list_pending():
        p = config.OUTBOX / "pending" / f"{f['filing_id']}.md"
        if p.exists():
            out.append({**f, "markdown": p.read_text(encoding="utf-8")})
    return out


def _decide(fn, fid, *a):
    try:
        return fn(fid, *a)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except gate.ConflictError as e:
        raise HTTPException(409, str(e))


def _need_hash(body: Approval):
    if not body.brief_sha256:
        raise HTTPException(422, "brief_sha256 is required: a decision must name the exact brief that was reviewed")


@app.post("/api/filings/{fid}/approve")
def approve(fid: str, body: Approval):
    _need_hash(body)
    return _decide(gate.approve, fid, body.approver, body.brief_sha256)


@app.post("/api/filings/{fid}/reject")
def reject(fid: str, body: Approval):
    _need_hash(body)
    return _decide(gate.reject, fid, body.approver, body.reason, body.brief_sha256)


@app.get("/api/filings/{fid}/verify")
def verify(fid: str):
    rep = provenance.verify_filing(fid)
    if rep["verdict"] == "NOT FOUND":
        raise HTTPException(404, "no such filing")
    return rep


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
