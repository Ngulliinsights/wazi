"""Measure YOUR hardware before committing to a model. One command, a few minutes.
  python -m wazi.bench --model qwen3:4b --runs 2
Reports load time, seconds per agent run, tokens and tokens/sec, whether the template fallback fired (a quality red flag),
and a projection of how long the full eval harness would take. Use --llm stub to test the script itself."""
import argparse
import asyncio
import statistics
import time

from . import evals
from .agent import run_agent
from .agent.llm import LLMError, OllamaLLM, StubLLM

TENDERS = ["ocds-synth-0001", "ocds-eval-abv-target", "ocds-eval-win-short"]


def bench(make_llm, runs: int, warm: bool = True) -> dict:
    load = None
    if warm:
        t0 = time.perf_counter()
        try:
            make_llm().complete_json("plan", {"facts": "warm-up"})
        except LLMError:
            pass                                   # a bad reply still means the model is now loaded
        load = round(time.perf_counter() - t0, 1)
    rows = []
    for i in range(runs):
        for oc in TENDERS:
            llm = make_llm(); t0 = time.perf_counter()
            r = asyncio.run(run_agent(oc, "Prepare a tender readiness brief", "2026-01-01", llm, "loop"))
            rows.append({"ocid": oc, "seconds": time.perf_counter() - t0, "calls": llm.calls, "tok_in": getattr(llm, "tokens_in", 0), "tok_out": getattr(llm, "tokens_out", 0),
                         "model_s": getattr(llm, "model_seconds", 0.0), "fallback": r["used_fallback"], "status": r["status"]})
    mean = lambda k: statistics.mean(r[k] for r in rows)
    per_run = mean("seconds")
    n_cases, n_modes = len(evals.CASES), 2
    return {"load_seconds": load, "runs": len(rows), "seconds_per_run": round(per_run, 3), "tok_in": round(mean("tok_in")), "tok_out": round(mean("tok_out")),
            "tokens_per_sec": round(sum(r["tok_out"] for r in rows) / max(1e-9, sum(r["model_s"] for r in rows)), 1) if any(r["model_s"] for r in rows) else None,
            "fallbacks": sum(r["fallback"] for r in rows), "not_filed": sum(r["status"] != "awaiting_human_approval" for r in rows),
            "projected_minutes": {f"{k} run(s) per case": round(per_run * n_cases * n_modes * k / 60, 2) for k in (1, 3, 5)}}


def main():
    p = argparse.ArgumentParser(prog="python -m wazi.bench")
    p.add_argument("--llm", choices=["ollama", "stub"], default="ollama"); p.add_argument("--model"); p.add_argument("--runs", type=int, default=2)
    p.add_argument("--api", choices=["native", "openai"])
    a = p.parse_args()
    srv = evals.setup_data()
    make = (lambda: StubLLM("invest_all")) if a.llm == "stub" else (lambda: OllamaLLM(a.model, api=a.api))
    res = bench(make, a.runs, warm=a.llm != "stub"); srv.shutdown()
    print(f"model: {a.model or 'default'} | first load: {res['load_seconds']}s (excluded below)")
    print(f"per agent run: {res['seconds_per_run']}s | prompt tokens {res['tok_in']} | output tokens {res['tok_out']} | speed: {res['tokens_per_sec']} tok/s")
    print(f"template fallbacks: {res['fallbacks']}/{res['runs']} | runs not filed: {res['not_filed']}/{res['runs']}   (anything above 0 = model struggles with the JSON task)")
    print("projected full eval (18 cases x 2 modes):", ", ".join(f"{k}: {v} min" for k, v in res["projected_minutes"].items()))


if __name__ == "__main__":
    main()
