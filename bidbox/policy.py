"""Data-conduct policy from the challenge rules: never a live tender in progress. Real tenders that are still open are refused;
synthetic data and closed tenders are fine. Judged against the WALL CLOCK (not the replay date): replaying a closed tender as of an
earlier date is the intended use, working on a tender that is open today is not."""
import os
from datetime import datetime, timezone

CLOSED = {"complete", "cancelled", "unsuccessful", "withdrawn"}
OPEN = {"planned", "planning", "active"}


def today() -> datetime:
    t = os.environ.get("BIDBOX_TODAY")                       # determinism for tests only
    d = datetime.fromisoformat(t.replace("Z", "+00:00")) if t else datetime.now(timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def allowed() -> bool:
    return os.environ.get("BIDBOX_ALLOW_LIVE") == "1"          # development override; recorded in the brief and the manifest


def live_reason(status, deadline, synthetic, now=None) -> str | None:
    """Why this tender counts as live, or None. Unknown status and no deadline = cannot tell = allowed."""
    if synthetic:
        return None
    s = (status or "").strip().lower()
    if s in CLOSED:
        return None
    if deadline:
        try:
            d = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
            d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            return f"its deadline ({str(deadline)[:10]}) has not passed yet" if d > (now or today()) else None
        except ValueError:
            pass
    return f"its status is '{s}' and no usable deadline is recorded" if s in OPEN else None
