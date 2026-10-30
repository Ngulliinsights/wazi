"""Mutation check: break trust-critical code on purpose; every mutant must be caught by the tests.
A surviving mutant means a test is too weak. Each mutant runs in a THROWAWAY COPY of the repo, in parallel,
so your source is never modified. Usage: python scripts/mutate.py [lo:hi] [--jobs N]"""
import json, os, pathlib, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ["tests/test_backend.py", "tests/test_review.py", "tests/test_stages.py", "tests/test_live_policy.py", "tests/test_packaging.py", "tests/test_joint_ventures.py", "tests/test_windows.py", "tests/test_defensible.py", "tests/test_freetier.py",   # fast + most discriminating first
         "tests/test_ollama_native.py", "tests/test_resilience.py", "tests/test_investigate.py", "tests/test_brief.py", "tests/test_personas.py",
         "tests/test_bughunt.py", "tests/test_evals_quota.py", "tests/test_api.py", "tests/test_agent.py", "tests/test_fetch_mcp.py", "tests/test_mcp_contract.py",
         "tests/test_counterparty.py", "tests/test_validation.py", "tests/test_amendment.py", "tests/test_lifecycle.py"]
M = [  # (file, original, mutant, what it breaks)
 ("wazi/gate.py", "or n.casefold() in AGENT_NAMES", "or False", "agent names accepted as approvers"),
 ("wazi/gate.py", "len(n) < 3", "len(n) < 0", "empty/short approver accepted"),
 ("wazi/findings.py", '("fraud", "corrupt", "bribe", "stole", "illegal")', '("zzzz",)', "accusatory wording allowed"),
 ("wazi/findings.py", ") is None:", ") is not None:", "unknown source ocid accepted"),
 ("wazi/tools.py", "<= 10]", "<= 1e18]", "outliers no longer excluded"),
 ("wazi/tools.py", "share >= 0.5", "share >= 0.99", "concentration signal never fires"),
 ("wazi/tools.py", "len(vals) < 5", "len(vals) < 0", "thin data not flagged"),
 ("wazi/tools.py", "if not accepted:", "if False:", "unsourced brief gets filed"),
 ("wazi/tools.py", "abs(rz) <= 2", "abs(rz) <= 200", "price position always within range"),
 ("wazi/tools.py", "dl >= _now(as_of)", "dl <= _now(as_of)", "deadline status inverted"),
 ("wazi/tools.py", '"open" if', '"closed" if', "deadline label swapped"),
 ("wazi/tools.py", "if award_count < THIN_HISTORY:", "if False:", "counterparty thin history not flagged"),
 ("wazi/tools.py", "len(issues) == 0", "True", "counterparty issues ignored"),
 ("wazi/tools.py", "len(issues) > 0", "False", "amendments never detected"),
 ("wazi/tools.py", "days_open and days_open > 90", "False", "aged items never flagged"),
 ("wazi/validation.py", 'len(issues) == 0', "True", "validation issues ignored"),
 ("wazi/validation.py", '"no findings present"', '""', "empty findings allowed"),
]
M += [tuple(x) for x in json.loads((ROOT / "scripts" / "mutants_agent.json").read_text())]
T = lambda *n: [f"tests/test_{x}.py" for x in n]
FOCUS = {  # tests most likely to catch a mutation in each file, tried FIRST. If they do not catch it, the FULL suite runs, so no mutant is
           # ever called a survivor (or caught) on thinner evidence than before; this only makes the common 'caught' case fast.
    "wazi/gate.py": T("defensible", "bughunt", "backend"), "wazi/findings.py": T("defensible", "backend", "bughunt", "brief"),
    "wazi/tools.py": T("backend", "live_policy", "joint_ventures", "windows", "defensible", "brief", "counterparty", "amendment", "lifecycle"), "wazi/textsafe.py": T("bughunt"),
    "wazi/provenance.py": T("defensible"), "wazi/audit_log.py": T("bughunt"), "wazi/store.py": T("stages", "resilience", "windows", "defensible", "live_policy"),
    "wazi/api.py": T("api", "live_policy", "bughunt", "freetier", "defensible"), "wazi/policy.py": T("live_policy"), "wazi/review.py": T("review"), "wazi/evals.py": T("evals_quota"),
    "wazi/agent/llm.py": T("freetier", "ollama_native", "resilience"), "wazi/agent/evidence.py": T("bughunt", "resilience", "agent", "investigate", "windows"),
    "wazi/agent/graph.py": T("live_policy", "agent", "investigate", "resilience", "freetier", "bughunt", "mcp_contract"), "wazi/agent/fetcher.py": T("bughunt", "fetch_mcp"),
    "wazi/validation.py": T("validation")}
SKIP_DIRS = shutil.ignore_patterns("var", ".venv", "__pycache__", ".pytest_cache", "*.zip", "*.mutbak")
ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def pytest(cwd, extra=(), files=None):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *extra, *(files or TESTS)], cwd=cwd,
                          capture_output=True, text=True, env=ENV)


def run_one(args):
    i, (f, old, new, what) = args
    src = (ROOT / f).read_text()
    if src.count(old) != 1:
        return i, what, "SKIP", f"pattern found {src.count(old)}x in {f}"
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mut_"))
    try:
        shutil.copytree(ROOT, tmp / "r", ignore=SKIP_DIRS)
        (tmp / "r" / f).write_text(src.replace(old, new))
        r = pytest(tmp / "r", ["-x"], FOCUS.get(f))                 # fast path: the most relevant tests
        if r.returncode == 0 and FOCUS.get(f):
            r = pytest(tmp / "r", ["-x"])                            # not caught there: the FULL suite decides
        res = (i, what, ("CAUGHT" if r.returncode != 0 else "SURVIVED"), "")
        print(f"{res[2]:<9} {what}", flush=True)
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    jobs = int(sys.argv[sys.argv.index("--jobs") + 1]) if "--jobs" in sys.argv else max(2, (os.cpu_count() or 2))
    if "--jobs" in sys.argv:
        args = [a for a in args if a != sys.argv[sys.argv.index("--jobs") + 1]]
    lo, hi = map(int, args[0].split(":")) if args else (0, len(M))
    base = pytest(ROOT) if "--skip-baseline" not in sys.argv else None  # must be green, otherwise every mutant looks "caught"
    if base is not None and base.returncode != 0:
        print("BASELINE FAILING - fix tests before mutating\n" + base.stdout[-800:]); sys.exit(2)
    work = list(enumerate(M))[lo:hi]
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        results = sorted(ex.map(run_one, work))
    for i, what, status, note in results:
        if status == "SKIP":
            print(f"{status:<9} {what} ({note})")
    caught = sum(r[2] == "CAUGHT" for r in results); skipped = [r[1] for r in results if r[2] == "SKIP"]
    surv = [r[1] for r in results if r[2] == "SURVIVED"]
    ran = len(results) - len(skipped)
    print(f"\n{caught}/{ran} mutants caught; survivors: {surv or 'none'}; skipped (stale patterns, fix the list): {skipped or 'none'}")
    sys.exit(1 if surv or skipped else 0)
