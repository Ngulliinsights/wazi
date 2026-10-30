import argparse
import json

from .graph import run_agent_sync
from .llm import OllamaLLM, StubLLM

p = argparse.ArgumentParser(prog="python -m wazi.agent")
p.add_argument("ocid"); p.add_argument("--as-of"); p.add_argument("--request", default="Prepare a tender readiness brief")
p.add_argument("--llm", choices=["ollama", "stub"], default="ollama"); p.add_argument("--model")
p.add_argument("--mode", choices=["loop", "pipeline"], default="loop")
a = p.parse_args()
r = run_agent_sync(a.ocid, a.request, a.as_of, StubLLM() if a.llm == "stub" else OllamaLLM(a.model), a.mode)
for e in r["events"]:
    print(f"  {e['event']:<20}", {k: v for k, v in e.items() if k not in ('event', 'ts', 'run_id')})
print(json.dumps({k: v for k, v in r.items() if k != "events"}, indent=2))
