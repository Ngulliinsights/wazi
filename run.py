#!/usr/bin/env python3
"""One command for a stranger:   python run.py        (Windows: py run.py)
Creates .venv, installs, loads the SAMPLE data (synthetic), starts the UI and opens it. Uses a local Ollama model if one is
running; otherwise the deterministic STUB, clearly labelled (demo only, not a model).
Options: --stub (force the stub), --check (bootstrap + one stub run, then exit 0/1), --port N."""
import hashlib
import os
import subprocess
import sys
import threading
import time
import urllib.request
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(*cmd, **kw):
    return subprocess.run([str(c) for c in cmd], cwd=ROOT, check=True, **kw)


def ollama_up() -> bool:
    try:
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1.5)
        return True
    except Exception:  # noqa: BLE001
        return False


def main(argv: list[str]) -> int:
    if sys.version_info < (3, 11):
        sys.exit("Python 3.11 or newer is required")
    if not PY.exists():
        print("creating .venv ...", flush=True)
        venv.EnvBuilder(with_pip=True).create(VENV)
    stamp, mark = hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest()[:16], VENV / ".wazi-installed"
    if not mark.exists() or mark.read_text() != stamp:
        print("installing dependencies (first run only, a minute or two) ...", flush=True)
        run(PY, "-m", "pip", "install", "-q", "-e", ".")
        mark.write_text(stamp)
    stub = "--stub" in argv or "--check" in argv or not ollama_up()
    env = {**os.environ, "WAZI_LLM": "stub" if stub else "ollama"}
    if stub:
        print("No local model found (or --stub): using the deterministic STUB. Demo only, not a model.", flush=True)
    if not (ROOT / "var" / "wazi.db").exists():
        run(PY, "-m", "wazi.cli", "load", env=env, stdout=subprocess.DEVNULL)
    if "--check" in argv:
        out = run(PY, "-m", "wazi.agent", "ocds-synth-0001", "--llm", "stub", "--as-of", "2026-01-01", env=env, capture_output=True, text=True)
        ok = '"status": "awaiting_human_approval"' in out.stdout
        print("check:", "OK" if ok else "FAILED")
        return 0 if ok else 1
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 8000
    url = f"http://127.0.0.1:{port}"
    print(f"Starting {url}  (Ctrl+C to stop)", flush=True)
    threading.Timer(2.5, lambda: webbrowser.open(url)).start()
    run(PY, "-m", "wazi.api", env={**env, "PORT": str(port)})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except subprocess.CalledProcessError as e:
        sys.exit(f"setup failed running: {' '.join(map(str, e.cmd))}")
    except KeyboardInterrupt:
        sys.exit(0)
