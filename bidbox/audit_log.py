"""Append-only JSONL log: one line per tool call (inputs, outputs, timestamp, run id)."""
import contextvars
import functools
import json
import os
import time
import uuid
from datetime import datetime, timezone

from .config import LOG_PATH

RUN_ID = os.environ.get("BIDBOX_RUN_ID") or uuid.uuid4().hex[:12]
run_id_var: contextvars.ContextVar = contextvars.ContextVar("bidbox_run_id", default=None)   # set by the in-process test session


def current_run_id() -> str:
    return run_id_var.get() or RUN_ID
MAX_OUT = 20000


def write(entry: dict) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "run_id": current_run_id(), **entry}
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str, ensure_ascii=False) + "\n")


def read_all(run_id: str | None = None) -> list[dict]:
    """Read the log tolerantly. A line being written right now, a blank line or a corrupt line is skipped,
    never allowed to break a reader. Explicit utf-8: Windows defaults to a legacy code page."""
    out = []
    if not LOG_PATH.exists():
        return out
    with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and (run_id is None or rec.get("run_id") == run_id):
                out.append(rec)
    return out


def logged(fn):
    """Decorator: every tool call is logged, including failures."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            out = fn(*args, **kwargs)
            blob = json.dumps(out, default=str)
            write({"tool": fn.__name__, "inputs": kwargs or list(args), "ok": True,
                   "ms": round((time.perf_counter() - t0) * 1000, 1),
                   "output": json.loads(blob) if len(blob) < MAX_OUT else "[truncated]"})
            return out
        except Exception as e:  # log, then re-raise so the agent can recover
            write({"tool": fn.__name__, "inputs": kwargs or list(args), "ok": False, "error": repr(e)})
            raise
    return wrapper
