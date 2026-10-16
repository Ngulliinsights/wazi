"""LangGraph agent. Tools are reached only through the MCP server. The model plans and writes;
code gathers evidence, enforces grounding and scope, and stops at the human gate."""
import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import date
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .. import audit_log, policy, provenance as prov
from ..findings import MAX_STATEMENT, has_verdict
from . import evidence as ev
from . import fetcher
from .inproc import inproc_session
from .llm import LLMError, OllamaLLM, QuotaExhausted, StubLLM  # noqa: F401  (StubLLM re-exported for callers)

MAX_ATTEMPTS = 2
MAX_INVEST_STEPS = 3
MAX_DOCS = 2
THIN_HISTORY = 5                                   # fewer in-category awards than this triggers the all-categories recovery
DEFAULT_CHECKS = ["history", "benchmark"]
INVEST_TOOLS = ["peer_baseline", "history_all_categories"]   # the model picks a NAME from this list; code supplies the arguments
SEVERITIES = ("info", "check", "signal")
KINDS = ("deadline", "eligibility", "price", "buyer_history", "data_quality", "other")


class State(TypedDict, total=False):
    # request
    ocid: str
    request: str
    as_of: str | None
    run_id: str
    mode: str
    audience: str
    live_override: bool
    # gathered by code
    plan: list[str]
    notice: dict
    history: dict | None
    hkey: str | None
    hcat: str | None
    hbroad: bool
    bench: dict | None
    window: dict | None
    peers: dict | None
    wide: dict | None
    docs: list[dict]
    notes: list[str]
    evidence: list[dict]
    # model output and verification
    summary: str
    raw: list
    attempt: int
    feedback: str
    accepted: list[dict]
    rejected: list[dict]
    best: list[dict]
    # outcome
    used_fallback: bool
    status: str
    result: dict
    error: str


@asynccontextmanager
async def mcp_session(run_id: str):
    env = {**os.environ, "BIDBOX_RUN_ID": run_id}
    params = StdioServerParameters(command=sys.executable, args=["-m", "bidbox.server"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            yield s


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def build_graph(session: ClientSession, llm, run_id: str, events: list, fetch: ClientSession | None = None, t0_run: float | None = None):
    calls: list[dict] = []   # every read-tool call with the hash of its exact output: the filing must be able to replay them

    def emit(event: str, **data):
        rec = {"event": event, "run_id": run_id, **data}
        events.append({"ts": time.time(), **rec})
        audit_log.write(rec)

    async def call(tool: str, **args) -> dict:
        t0 = time.perf_counter()
        res = await session.call_tool(tool, args)
        txt = (getattr(res.content[0], "text", "") or "") if res.content else ""
        if res.isError:
            out = {"error": txt[:200], "found": False}
        else:
            try:
                out = json.loads(txt or "{}")
            except json.JSONDecodeError:
                out = {"error": "tool returned non-JSON", "found": False}
            if not isinstance(out, dict):
                out = {"error": "tool returned a non-object", "found": False}
        emit("tool_call", tool=tool, args=args, ms=_ms(t0), ok=not res.isError)
        if tool in prov.READ_TOOLS and not res.isError:
            calls.append({"tool": tool, "args": dict(args), "output_sha256": prov.sha(out)})
        return out

    # ------------------------------------------------------------------ nodes: gather

    async def intake(s: State):
        emit("intake", ocid=s["ocid"], request=s.get("request"), as_of=s.get("as_of"), llm=getattr(llm, "name", "?"))
        if ev.violates_scope(s.get("request", "")):
            emit("refused", reason="out of scope: this tool never awards, ranks bidders or recommends a bid")
            return {"status": "refused"}
        return {"notes": [], "attempt": 0, "feedback": "", "best": []}

    async def plan(s: State):
        try:
            out = await asyncio.to_thread(llm.complete_json, "plan", {"facts": f"ocid {s['ocid']}; optional checks: history, benchmark"})
            if not isinstance(out, dict):
                raise LLMError("plan was not a JSON object")
            asked = out.get("checks") if isinstance(out.get("checks"), list) else []
            checks = list(dict.fromkeys(c for c in asked if c in DEFAULT_CHECKS))
            if not checks:
                raise LLMError("plan had no valid checks")
            emit("plan", checks=checks, rationale=str(out.get("rationale", ""))[:300])
            return {"plan": checks}
        except QuotaExhausted:
            raise                                    # a used-up quota is not a bad model answer: stop, loudly
        except Exception as e:  # any model/client failure, not only LLMError, must not stop the run
            emit("plan_fallback", reason=repr(e)[:200])
            return {"plan": list(DEFAULT_CHECKS)}

    async def notice(s: State):
        n = await call("get_tender_notice", ocid=s["ocid"], as_of=s.get("as_of"))
        if n.get("error"):
            emit("tool_error", tool="get_tender_notice", error=n["error"])
            return {"status": "tool_error", "error": n["error"]}
        if not n.get("found"):
            emit("not_found", ocid=s["ocid"])
            return {"status": "not_found"}
        nn = n["notice"]
        reason = policy.live_reason(nn.get("status"), nn.get("deadline"), n.get("synthetic"))
        if reason:                      # data conduct: never a live tender in progress
            if not policy.allowed():
                emit("refused_live", reason=reason)
                return {"status": "refused_live", "error": f"This tender is still live ({reason}). Bid Box only prepares briefs for closed tenders and synthetic data."}
            emit("live_override", reason=reason)
            return {"notice": n, "live_override": True}
        return {"notice": n}

    async def history(s: State):
        if "history" not in s["plan"]:
            return {"history": None}
        n, notes = s["notice"], list(s["notes"])
        key = n["notice"].get("buyer_name")
        if not key and n.get("buyer_id"):
            key = n["buyer_id"]
            notes.append("buyer name not recorded; matched on buyer id")
            emit("history_key", used="buyer_id", reason="buyer name missing")
        if not key:
            notes.append("no buyer recorded; buyer history skipped")
            emit("history_skipped", reason="no buyer name or id")
            return {"history": None, "notes": notes}
        cat = n["notice"].get("category")
        h = await call("buyer_award_history", buyer=key, **({"category": cat} if cat else {}))
        broadened = False
        if cat and (not h.get("found") or h["n_awards"] < THIN_HISTORY):  # recovery: thin in-category history
            wide = await call("buyer_award_history", buyer=key)
            if wide.get("found") and wide["n_awards"] > (h.get("n_awards") or 0):
                emit("history_broadened", reason="thin in-category history; used all categories")
                notes.append("buyer history covers all categories because the category history was thin")
                h, broadened = wide, True
        if not h.get("found"):   # absence is a finding too: say so instead of silently omitting history
            emit("history_not_found", buyer=key, error=h.get("error"))
            notes.append("buyer history unavailable" if h.get("error") else "no recorded award history found for this buyer")
            return {"history": None, "notes": notes, "hkey": key, "hcat": cat, "hbroad": False}
        return {"history": h, "notes": notes, "hkey": key, "hcat": cat, "hbroad": broadened}

    async def benchmark(s: State):
        if "benchmark" not in s["plan"]:
            return {"bench": None}
        n, notes = s["notice"], list(s["notes"])
        cat, val = n["notice"].get("category"), n["notice"].get("value")
        try:
            amount = float(val) if (cat and val is not None) else None
        except (TypeError, ValueError):
            amount = None
        if amount is None:
            notes.append("price benchmark skipped: category or estimated value not recorded")
            emit("benchmark_skipped", reason="category or value missing")
            return {"bench": None, "notes": notes}
        b = await call("price_benchmark", category=cat, amount=amount,
                       currency=n.get("currency") or "KES", exclude_ocid=s["ocid"])
        return {"bench": b, "notes": notes}

    async def window(s: State):
        """Always runs (cheap, deterministic): how long was the tender open, against comparable tenders."""
        return {"window": await call("window_baseline", ocid=s["ocid"])}

    async def documents(s: State):
        urls = (s["notice"].get("documents") or [])[:MAX_DOCS]
        if not urls:
            return {"docs": []}
        if fetch is None:
            for u in urls:
                emit("fetch_unavailable", url=u)
            return {"docs": [{"url": u, "ok": False, "error": "fetch server unavailable"} for u in urls]}

        async def one(u: str):
            t0 = time.perf_counter()
            return await fetcher.read_document(fetch, u), _ms(t0)

        done = await asyncio.gather(*(one(u) for u in urls))   # read_document never raises; the fetches overlap
        docs = []
        for u, (d, ms) in zip(urls, done):
            emit("tool_call", server="fetch (borrowed)", tool="fetch", args={"url": u, "raw": True},
                 ms=ms, ok=d["ok"], chars=d.get("chars"), error=d.get("error"), text=d.get("text"))
            docs.append(d)
        return {"docs": docs}

    async def investigate(s: State):
        """Bounded autonomy: only when the data raised a signal, the model may choose which allowed check
        to run next (max MAX_INVEST_STEPS). Names are validated against an allowlist; arguments come from code."""
        if s.get("mode") != "loop":
            return {}
        h = s.get("history")
        flags = (h or {}).get("flags") or []
        if not h or not any(f.startswith("signal") for f in flags) or not s.get("hkey"):
            emit("investigate_skipped", reason="no signal to test")
            return {}
        # offer only checks that can actually run: peers need a category; the all-categories view is moot if history already is
        remaining = [t for t in INVEST_TOOLS
                     if not (t == "peer_baseline" and not s.get("hcat"))
                     and not (t == "history_all_categories" and s.get("hbroad"))]
        if not remaining:
            emit("investigate_skipped", reason="no applicable check")
            return {}
        done, peers, wide = [], None, None
        situation = f"Buyer {s['hkey']}: {'; '.join(flags)}. Category: {s.get('hcat')}."
        for i in range(MAX_INVEST_STEPS):
            if not remaining:
                break
            try:
                out = await asyncio.to_thread(llm.complete_json, "investigate",
                                              {"situation": situation, "allowed": remaining + ["stop"], "done": done})
            except QuotaExhausted:
                raise
            except Exception as e:
                emit("investigate_error", error=repr(e)[:200])
                break
            is_obj = isinstance(out, dict)
            tool = out.get("tool") if is_obj else None
            if not is_obj or out.get("action") != "call" or tool not in remaining:
                emit("investigate_stop", step=i + 1, reason=str(out.get("reason", ""))[:160] if is_obj else "invalid",
                     rejected_tool=tool if tool not in (None, "stop") and tool not in remaining else None)
                break
            remaining.remove(tool)
            done.append(tool)
            emit("investigate_step", step=i + 1, tool=tool, why=str(out.get("reason", ""))[:200])
            if tool == "peer_baseline":
                peers = await call("peer_baseline", buyer=s["hkey"], category=s["hcat"])
            elif tool == "history_all_categories":
                wide = await call("buyer_award_history", buyer=s["hkey"])
        return {"peers": peers, "wide": wide}

    async def evidence_node(s: State):
        items = ev.build_evidence(s["notice"], s.get("history"), s.get("bench"), s["notes"], s.get("docs"),
                                  s.get("peers"), s.get("wide"), s.get("window"))
        emit("evidence_built", n=len(items), kinds=sorted({e["kind"] for e in items}))
        return {"evidence": items}

    # ------------------------------------------------------------------ nodes: write and verify

    async def synthesize(s: State):
        attempt = s["attempt"] + 1
        emit("synthesize_attempt", attempt=attempt)
        try:
            out = await asyncio.to_thread(llm.complete_json, "synthesize", {"evidence": s["evidence"], "feedback": s["feedback"]})
            if not isinstance(out, dict):
                raise LLMError("synthesis was not a JSON object")
            raw = out.get("findings") if isinstance(out.get("findings"), list) else []
            summ = out.get("summary")
            return {"attempt": attempt, "summary": summ if isinstance(summ, str) else "", "raw": raw}
        except QuotaExhausted:
            raise
        except Exception as e:
            emit("synthesize_error", error=repr(e)[:200])
            return {"attempt": attempt, "summary": "", "raw": []}

    async def verify(s: State):
        by_id = {e["id"]: e for e in s["evidence"]}
        accepted, rejected, seen = [], [], set()

        def reject(finding, reason):
            rejected.append({"finding": finding, "reason": reason})

        for f in s["raw"]:
            if not isinstance(f, dict):
                reject(str(f)[:80], "finding is not an object")
                continue
            ids = f.get("evidence")
            ids = [ids] if isinstance(ids, str) else ids
            if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i in by_id for i in ids):
                reject(f, "cites missing or unknown evidence")
                continue
            ids = list(dict.fromkeys(ids))
            stmt = f.get("statement")
            if not isinstance(stmt, str) or len(stmt.strip()) < 5:
                reject(f, "statement missing or too short")
                continue
            if len(stmt) > MAX_STATEMENT:
                reject(f, f"statement longer than {MAX_STATEMENT} characters")
                continue
            if has_verdict(stmt):
                reject(f, "verdict or advice language; state facts and signals only")
                continue
            bad = ev.ungrounded_numbers(stmt, [by_id[i]["text"] for i in ids])
            if bad:
                reject(f, f"numbers not in cited evidence: {bad}")
                continue
            if stmt in seen:                       # the model repeated itself
                continue
            seen.add(stmt)
            first = by_id[ids[0]]
            sev = f.get("severity") if f.get("severity") in SEVERITIES else "info"
            if sev == "signal" and not any(by_id[i]["signal"] for i in ids):
                sev = "check"  # a model cannot raise a signal the data did not raise
            kind = f.get("kind") if f.get("kind") in KINDS else first["kind"]
            accepted.append({"kind": kind, "statement": stmt, "source_ocid": first["source_ocid"],
                             "field_path": first["field_path"], "severity": sev, "evidence": ids})
        best = accepted if len(accepted) >= len(s["best"]) else s["best"]
        emit("verify_result", attempt=s["attempt"], accepted=len(accepted), rejected=len(rejected),
             reasons=[r["reason"] for r in rejected][:5])
        fb = ""
        if rejected:
            fb = "Previous attempt had problems: " + "; ".join(r["reason"] for r in rejected[:3]) + ". Quote numbers exactly."
        return {"accepted": accepted, "rejected": rejected, "best": best, "feedback": fb}

    def after_verify(s: State):
        if (s["rejected"] or not s["accepted"]) and s["attempt"] < MAX_ATTEMPTS:
            return "synthesize"
        return "file"

    async def file_node(s: State):
        evidence = s["evidence"]
        findings, used_fb = s["best"], False
        if not findings:  # deterministic floor, flagged loudly
            by_id = {e["id"]: e for e in evidence}
            findings = [{"kind": f["kind"], "statement": f["statement"], "source_ocid": by_id[f["evidence"][0]]["source_ocid"],
                         "field_path": by_id[f["evidence"][0]]["field_path"], "severity": f["severity"], "evidence": list(f["evidence"])}
                        for f in ev.template_findings(evidence)]
            used_fb = True
            emit("fallback_findings", n=len(findings), reason="model produced no grounded findings")

        # The summary is model text too: it gets the same number and verdict checks as the findings.
        summary = s.get("summary") or ""
        if summary:
            if ev.ungrounded_numbers(summary, [e["text"] for e in evidence]):
                emit("summary_replaced", reason="numbers not in evidence")
                summary = ""
            elif has_verdict(summary):
                emit("summary_replaced", reason="verdict or advice language")
                summary = ""
        if not summary:
            summary = f"Readiness brief for {s['ocid']} built from {len(evidence)} sourced evidence items."

        roles: dict[str, str] = {}
        for key, role in (("notice", "notice"), ("history", "history"), ("bench", "benchmark"),
                          ("window", "window"), ("peers", "peers"), ("wide", "history_wide")):
            if s.get(key):
                roles.setdefault(prov.sha(s[key]), role)
        provenance = {
            "run": {
                "run_id": run_id, "mode": s.get("mode"), "audience": s.get("audience"), "as_of": s.get("as_of"), "request": s.get("request"),
                "llm": {"name": getattr(llm, "name", "?"), "model": getattr(llm, "model", None), "settings": getattr(llm, "settings", None)},
                "seconds_until_filing": round(time.perf_counter() - (t0_run or time.perf_counter()), 2),
                "llm_calls": getattr(llm, "calls", None), "tokens_in": getattr(llm, "tokens_in", None),
                "tokens_out": getattr(llm, "tokens_out", None), "model_seconds": getattr(llm, "model_seconds", None),
                "synthesis_attempts": s.get("attempt"), "used_template_fallback": used_fb, "live_override": bool(s.get("live_override")),
            },
            "tool_calls": [{**c, "role": roles.get(c["output_sha256"], "probe")} for c in calls],
            "documents": [{"url": d["url"], "ok": d["ok"], "error": d.get("error"), "text": d.get("text"),
                           "text_sha256": prov.sha(d.get("text") or "")} for d in (s.get("docs") or [])],
            "notes": list(s.get("notes") or []),
            "evidence": evidence,
            "model_calls": list(getattr(llm, "trace", [])),
        }
        filed = await call("file_brief", ocid=s["ocid"], summary=summary, findings=findings, as_of=s.get("as_of"),
                           audience=s.get("audience", "buyer"), provenance=provenance)
        emit("filed", status=filed.get("status"), filing_id=filed.get("filing_id"), awaiting="named human approval")
        status = "awaiting_human_approval" if filed.get("status") == "pending_approval" else "not_filed"
        return {"status": status, "used_fallback": used_fb,
                "result": {"filing": filed, "findings": findings, "rejected_last": s["rejected"], "summary": summary}}

    # ------------------------------------------------------------------ wiring

    g = StateGraph(State)
    for name, fn in [("intake", intake), ("plan", plan), ("notice", notice), ("history", history),
                     ("benchmark", benchmark), ("window", window), ("documents", documents),
                     ("investigate", investigate), ("evidence", evidence_node),
                     ("synthesize", synthesize), ("verify", verify), ("file", file_node)]:
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", lambda s: END if s.get("status") == "refused" else "plan")
    g.add_edge("plan", "notice")
    g.add_conditional_edges("notice", lambda s: END if s.get("status") in ("not_found", "tool_error", "refused_live") else "history")
    for a, b in [("history", "benchmark"), ("benchmark", "window"), ("window", "documents"), ("documents", "investigate"),
                 ("investigate", "evidence"), ("evidence", "synthesize"), ("synthesize", "verify")]:
        g.add_edge(a, b)
    g.add_conditional_edges("verify", after_verify, {"synthesize": "synthesize", "file": "file"})
    g.add_edge("file", END)
    return g.compile()


def _has_documents(ocid: str, as_of: str | None) -> bool:
    from .. import tools
    try:
        return bool(tools.get_tender_notice.__wrapped__(ocid, as_of).get("documents"))
    except Exception:  # noqa: BLE001
        return False


def _leaves(eg: BaseExceptionGroup) -> list[BaseException]:
    return [x for sub in eg.exceptions for x in (_leaves(sub) if isinstance(sub, BaseExceptionGroup) else [sub])]


async def run_agent(ocid: str, request: str = "Prepare a tender readiness brief", as_of: str | None = None,
                    llm=None, mode: str = "loop", run_id: str | None = None, audience: str = "buyer", inproc: bool | None = None) -> dict:
    llm = llm or OllamaLLM()
    as_of = as_of or date.today().isoformat()   # a replayable run needs a concrete reference date, recorded with it
    run_id, events, t0 = run_id or uuid.uuid4().hex[:12], [], time.perf_counter()
    try:
        inproc = (os.environ.get("BIDBOX_INPROC") == "1") if inproc is None else inproc   # tests only: real runs always use real MCP servers
        async with (inproc_session(run_id) if inproc else mcp_session(run_id)) as session:
            async with AsyncExitStack() as stack:
                fetch = None
                # The borrowed MCP server is started here, up front, and not lazily inside a graph node: anyio task groups
                # must be exited in the task that entered them. If it cannot START, say so and run once without it.
                try:
                    if not inproc or _has_documents(ocid, as_of):   # in-process test mode: spawn the fetch server only if the tender has documents
                        fetch = await stack.enter_async_context(fetcher.fetch_session())
                except Exception as e:  # noqa: BLE001
                    rec = {"event": "fetch_server_failed", "run_id": run_id, "error": repr(e)[:200]}
                    events.append({"ts": time.time(), **rec})
                    audit_log.write(rec)
                app = build_graph(session, llm, run_id, events, fetch, t0)
                final = await app.ainvoke({"ocid": ocid, "request": request, "as_of": as_of, "run_id": run_id, "mode": mode, "audience": audience},
                                          {"recursion_limit": 40})
    except BaseExceptionGroup as eg:  # real MCP servers run in anyio task groups, which wrap errors: surface a quota stop as itself
        quota = [x for x in _leaves(eg) if isinstance(x, QuotaExhausted)]
        if quota:
            raise quota[0] from None
        raise
    res = final.get("result", {}) or {}
    filing = res.get("filing") or {}
    return {"run_id": run_id, "mode": mode, "status": final.get("status", "unknown"), "ocid": ocid,
            "filing_id": filing.get("filing_id"), "findings": res.get("findings", []),
            "md_sha256": filing.get("md_sha256"), "manifest_sha256": filing.get("manifest_sha256"),
            "filing_error": filing.get("reason") or filing.get("error"),
            "summary": res.get("summary"), "error": final.get("error"), "used_fallback": final.get("used_fallback", False),
            "llm": getattr(llm, "name", "?"), "llm_calls": getattr(llm, "calls", None),
            "seconds": round(time.perf_counter() - t0, 2), "events": events}


def run_agent_sync(*a, **k) -> dict:
    return asyncio.run(run_agent(*a, **k))