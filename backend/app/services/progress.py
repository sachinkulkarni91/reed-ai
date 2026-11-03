"""In-memory processing progress, keyed by meeting id.

Transient by design: progress only matters while the work is running, and the
worker that produces it lives in this same process. Survives nothing - a server
restart simply shows plain "processing" until the next stage reports in.
Written from worker threads and read from the event loop; single-key dict
assignment is atomic under the GIL, so no lock is needed.
"""
from __future__ import annotations

import time

_progress: dict[int, dict] = {}


def report(meeting_id: int | None, stage: str, current: int | None = None,
           total: int | None = None, detail: str = "") -> None:
    if meeting_id is None:
        return
    _progress[meeting_id] = {
        "stage": stage,
        "current": current,
        "total": total,
        "detail": detail,
        "updated_at": time.time(),
    }


def clear(meeting_id: int | None) -> None:
    if meeting_id is not None:
        _progress.pop(meeting_id, None)


def get(meeting_id: int) -> dict | None:
    entry = _progress.get(meeting_id)
    if entry is None:
        return None
    # Stale guard: a crashed worker must not leave a permanent "43/97" ghost.
    if time.time() - entry["updated_at"] > 1800:
        _progress.pop(meeting_id, None)
        return None
    return {k: entry[k] for k in ("stage", "current", "total", "detail")}
