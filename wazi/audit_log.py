"""Append-only JSONL log: one line per tool call (inputs, outputs, timestamp, run id).

Each entry goes out as a single append, under a lock, so concurrent writers (the API's threadpool, async tasks,
a second process) cannot interleave inside a line. Readers skip anything that is not a complete JSON object.
Fields passed to `write` after the defaults win, so a caller may set its own `run_id` or `ts` explicitly.
"""
import contextvars
import functools
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from .config import LOG_PATH

MAX_OUT = 20000
RUN_ID = os.environ.get("WAZI_RUN_ID") or uuid.uuid4().hex[:12]
run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("wazi_run_id", default=None)   # set by the in-process test session

_LOCK = threading.Lock()
_APPEND = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)      # O_BINARY: no \n -> \r\n rewriting on Windows; RDWR: to peek at the last byte


def current_run_id() -> str:
    return run_id_var.get() or RUN_ID


# --------------------------------------------------------------------------- writing


def _dumps(entry: dict) -> str:
    """One JSON line. An entry that cannot be serialised is still recorded, as a note, rather than lost."""
    try:
        return json.dumps(entry, default=str, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        fallback = {k: entry.get(k) for k in ("ts", "run_id", "tool") if isinstance(entry.get(k), str)}
        fallback.update(ok=False, error=f"log entry not serialisable: {e!r}", entry_repr=repr(entry)[:MAX_OUT])
        return json.dumps(fallback, default=str, ensure_ascii=False)


def _open_append() -> int:
    try:
        return os.open(LOG_PATH, _APPEND, 0o644)
    except FileNotFoundError:                                  # first write: the log's folder does not exist yet
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        return os.open(LOG_PATH, _APPEND, 0o644)


def _ends_mid_line(fd: int) -> bool:
    """True when the file's last line is unfinished (a crash or full disk cut an earlier write short). Appending straight
    after it would fuse the fragment and the new entry into one corrupt line and lose a good record."""
    size = os.fstat(fd).st_size
    if not size:
        return False
    os.lseek(fd, size - 1, os.SEEK_SET)                        # harmless: O_APPEND writes always go to the end
    return os.read(fd, 1) != b"\n"


def write(entry: dict) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "run_id": current_run_id(), **entry}
    data = (_dumps(entry) + "\n").encode("utf-8", "replace")   # "replace": a lone surrogate must not abort the write
    with _LOCK:
        fd = _open_append()
        try:
            if _ends_mid_line(fd):
                data = b"\n" + data                            # the fragment stays a skipped corrupt line of its own
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)


# --------------------------------------------------------------------------- reading


def read_all(run_id: str | None = None) -> list[dict]:
    """Read the log tolerantly. A line being written right now, a blank line or a corrupt line is skipped,
    never allowed to break a reader. Explicit utf-8: Windows defaults to a legacy code page."""
    out = []
    # Cheap pre-filter: a line for this run must contain its id exactly as the writer encoded it, so other runs' lines
    # are skipped without being parsed. The exact run_id comparison below still decides.
    needle = json.dumps(run_id, ensure_ascii=False)[1:-1] if run_id is not None else None
    try:
        f = open(LOG_PATH, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return out
    with f:
        for line in f:
            if needle is not None and needle not in line:
                continue
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


# --------------------------------------------------------------------------- decorator


def _snapshot(obj):
    """What the value looks like as JSON right now, so a tool that mutates its arguments cannot rewrite the record of its inputs."""
    try:
        return json.loads(json.dumps(obj, default=str))
    except (TypeError, ValueError):
        return repr(obj)[:MAX_OUT]


def _inputs(args: tuple, kwargs: dict):
    raw = {"_args": list(args), **kwargs} if args and kwargs else (kwargs or list(args))
    return _snapshot(raw)


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def logged(fn):
    """Decorator: every tool call is logged, including failures. The original stays reachable as `__wrapped__`
    (provenance replays through it so that auditing does not add to the log)."""
    name = fn.__name__

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        inputs = _inputs(args, kwargs)
        t0 = time.perf_counter()
        try:
            out = fn(*args, **kwargs)
        except Exception as e:  # log, then re-raise so the agent can recover
            write({"tool": name, "inputs": inputs, "ok": False, "ms": _ms(t0), "error": repr(e)})
            raise
        ms = _ms(t0)
        # Logging happens outside the try above: a problem recording a result that succeeded must never be logged as a tool failure.
        extra = {}
        try:
            blob = json.dumps(out, default=str)
        except (TypeError, ValueError):
            output = "[unserializable]"
        else:
            if len(blob) < MAX_OUT:
                output = json.loads(blob)
            else:
                output, extra = "[truncated]", {"output_chars": len(blob)}
        write({"tool": name, "inputs": inputs, "ok": True, "ms": ms, "output": output, **extra})
        return out
    return wrapper