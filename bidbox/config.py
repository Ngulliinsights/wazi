import os
from pathlib import Path

ROOT = Path(os.environ.get("BIDBOX_HOME", Path(__file__).resolve().parent.parent))
DB_PATH = Path(os.environ.get("BIDBOX_DB", ROOT / "var" / "bidbox.db"))
LOG_PATH = Path(os.environ.get("BIDBOX_LOG", ROOT / "var" / "audit.jsonl"))
OUTBOX = Path(os.environ.get("BIDBOX_OUTBOX", ROOT / "var" / "outbox"))
SAMPLE_DATA = Path(__file__).resolve().parent.parent / "data" / "sample_ocds.json"  # ships with the repo
for p in (DB_PATH.parent, LOG_PATH.parent, OUTBOX / "pending", OUTBOX / "filed", OUTBOX / "rejected"):
    p.mkdir(parents=True, exist_ok=True)
