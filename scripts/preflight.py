"""Submission preflight: is the repo submittable RIGHT NOW? Run nightly so an early cutoff is never a surprise.
Usage: python scripts/preflight.py [--fast]   (--fast skips the full test run)"""
import os, pathlib, shutil, subprocess, sys, tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAST = "--fast" in sys.argv
rd = lambda n: (ROOT / n).read_text(encoding="utf-8", errors="ignore") if (ROOT / n).exists() else ""
src = "\n".join(p.read_text(errors="ignore") for p in (ROOT / "wazi").rglob("*.py"))
checks = []


def check(name, ok, hint=""):
    checks.append((name, bool(ok), hint))


lic = rd("LICENSE")
check("python floor matches the code (>=3.11)", 'requires-python = ">=3.11"' in rd("pyproject.toml"), "set requires-python")
check("run.py (one command) and .gitignore present", (ROOT / "run.py").exists() and (ROOT / ".gitignore").exists(), "add them")
check("OSI licence file (MIT/Apache-2.0)", "MIT License" in lic or "Apache License" in lic, "add LICENSE")
check("README: points to docs/", "docs/" in rd("README.md"), "update README to point to docs/")
check("README: quick start", "python run.py" in rd("README.md"), "add quick start")
check("docs/README: run instructions", "## Run" in rd("docs/README.md"), "add a '## Run' section with one-command run")
check("docs/README: honest unfinished/known limits", "Known limits" in rd("docs/README.md"), "add '## Known limits'")
check("docs/README: data source and data half stated", "**Data:**" in rd("docs/README.md") and "data half" in rd("docs/README.md").lower(), "state which data and which half you build on")
check("docs/ARCHITECTURE.md (one page, includes why-borrowed-MCP line)", len(rd("docs/ARCHITECTURE.md").split()) > 150 and "fetch" in rd("docs/ARCHITECTURE.md").lower(), "write docs/ARCHITECTURE.md")
check("docs/FIT_PLAN.md references ADR 001", "ADR 001" in rd("docs/FIT_PLAN.md"), "update FIT_PLAN.md to reference ADR 001")
check("docs/EVALS.md (8+ tasks, pass/fail, one unfixed failure)", rd("docs/EVALS.md").lower().count("pass") >= 8 and "unfixed" in rd("docs/EVALS.md").lower(), "write docs/EVALS.md from real runs")
words = len(rd("docs/DESCRIPTION.md").split())
check("docs/DESCRIPTION.md (~300 words)", 220 <= words <= 380, f"currently {words} words")
check("public/ directory exists for brand assets", (ROOT / "public").exists(), "create public/ directory")
check("brand assets in public/ (not root)", len(list((ROOT / "public").glob("*.svg"))) > 0, "move logo files to public/")
check("own MCP server with 3+ tools and one acting tool", src.count("mcp.tool()(tools.") >= 3 and "file_brief" in src, "")
check("borrowed MCP server actually used", "mcp_server_fetch" in src and "fetcher.read_document" in src, "")
check("open-source orchestration (LangGraph)", "langgraph" in src, "")
check("open-weights client present", "OllamaLLM" in src, "")
check("no leftover mutation backups / secrets files", not list(ROOT.rglob("*.mutbak")) and not list(ROOT.rglob(".env")), "")
import re
data = rd("data/sample_ocds.json")
check("sample data is labelled synthetic", "SYNTHETIC" in data[:300], "label non-real data")
check("no emails or phone numbers in data (personal-data rule)", not re.search(r"[\w.]+@[\w.]+\.\w+|\+\d{1,3}[\s-]?\d{2,4}[\s-]?\d{3}[\s-]?\d{3,4}|\b0\d{2,3}[\s-]\d{3}[\s-]\d{3,4}\b", data), "remove personal contact details")

tmp = pathlib.Path(tempfile.mkdtemp(prefix="preflight_"))
shutil.copytree(ROOT, tmp / "repo", ignore=shutil.ignore_patterns("var", ".venv", "__pycache__", ".pytest_cache", "*.zip"))
env = {**os.environ, "WAZI_HOME": str(tmp / "home"), "PYTHONDONTWRITEBYTECODE": "1"}
(tmp / "home").mkdir()
for cmd in (["-m", "wazi.cli", "load"], ["-m", "wazi.cli", "audit"]):
    r = subprocess.run([sys.executable, *cmd], cwd=tmp / "repo", env=env, capture_output=True, text=True)
    check("clean copy runs: wazi " + cmd[-1], r.returncode == 0, r.stderr[-150:])
r = subprocess.run([sys.executable, "-m", "wazi.agent", "ocds-synth-0001", "--llm", "stub", "--as-of", "2026-01-01"],
                   cwd=tmp / "repo", env=env, capture_output=True, text=True)
check("clean copy runs: agent end to end (stub)", '"status": "awaiting_human_approval"' in r.stdout, r.stderr[-150:])
if not FAST:
    t = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    check("full test suite passes", t.returncode == 0, t.stdout[-200:])
if not FAST:   # the check that matters: a brand-new virtual environment, ONE command, no help from this machine's installed packages
    t2 = subprocess.run([sys.executable, "run.py", "--check"], cwd=tmp / "repo", capture_output=True, text=True, timeout=600)
    check("fresh machine: `python run.py --check` creates a venv, installs, runs", t2.returncode == 0 and "check: OK" in t2.stdout, (t2.stdout + t2.stderr)[-200:])
shutil.rmtree(tmp, ignore_errors=True)
bad = [c for c in checks if not c[1]]
for n, ok, h in checks:
    print(("PASS  " if ok else "FAIL  ") + n + ("" if ok or not h else f"   <- {h}"))
print(f"\n{len(checks) - len(bad)}/{len(checks)} checks pass; outstanding: {len(bad)}")
sys.exit(1 if bad else 0)
