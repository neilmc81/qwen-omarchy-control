"""Pollable background jobs owned by the controller, not by a voice agent.

Why this exists
---------------
Some Tier 2 powers run *after* the tool call returns: watching a window for a
condition, or any future long task. The voice frontend must be able to ask
"is it done yet?" without keeping a blocking call open, and without the
controller depending on that frontend's notification mechanism.

So a job is just a JSON state file plus a daemon thread:

    start(kind, worker) -> job id
    update(job_id, **fields)  (workers call this)
    finish(job_id, status, **fields)
    read(job_id) -> the current record, or None
    stop(job_id) -> asks the worker to stop; the worker observes `stopped`

The file lives at `$XDG_STATE_HOME/qwen-omarchy-control/jobs/<id>.json` (0600).
The frontend polls `read`; a desktop notification is an optional courtesy the
caller may add, never the transport. This is what keeps watch-and-narrate
portable across voice agents.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

STATE_HOME = Path(
    os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
)
JOBS_DIR = Path(os.environ.get(
    "QWEN_JOBS_DIR", STATE_HOME / "qwen-omarchy-control" / "jobs"
))

TERMINAL = ("met", "timeout", "stopped", "failed")

_LOCK = threading.Lock()
_STOP: dict[str, threading.Event] = {}


def _path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _write(record: dict) -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".job.", dir=JOBS_DIR)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, ensure_ascii=False)
            stream.write("\n")
        os.chmod(name, 0o600)
        os.replace(name, _path(record["id"]))
    finally:
        Path(name).unlink(missing_ok=True)


def start(kind: str, worker: Callable[[str, threading.Event], None],
          **fields: Any) -> str:
    """Start `worker(job_id, stop_event)` on a daemon thread. Returns the id.

    The worker is responsible for calling `finish` (or `update`) as it goes;
    exceptions are caught and recorded as a `failed` terminal so a crashing
    worker never leaves a job stuck in `running`.
    """
    job_id = f"{kind}-{uuid.uuid4().hex[:10]}"
    stop = threading.Event()
    record = {
        "id": job_id,
        "kind": kind,
        "status": "running",
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        **fields,
    }
    with _LOCK:
        _STOP[job_id] = stop
        _write(record)

    def run() -> None:
        try:
            worker(job_id, stop)
        except Exception as exc:  # noqa: BLE001 - a job must never crash the server
            finish(job_id, "failed", error=f"{type(exc).__name__}: {exc}")

    threading.Thread(target=run, name=f"qwen-{job_id}", daemon=True).start()
    return job_id


def update(job_id: str, **fields: Any) -> dict | None:
    with _LOCK:
        record = read(job_id)
        if record is None:
            return None
        record.update(fields)
        record["updated"] = dt.datetime.now().isoformat(timespec="seconds")
        _write(record)
        return record


def finish(job_id: str, status: str, **fields: Any) -> dict | None:
    if status not in TERMINAL:
        status = "failed"
    with _LOCK:
        record = read(job_id)
        if record is None:
            return None
        record.update(fields)
        record["status"] = status
        record["finished"] = dt.datetime.now().isoformat(timespec="seconds")
        _write(record)
        _STOP.pop(job_id, None)
        return record


def read(job_id: str) -> dict | None:
    try:
        return json.loads(_path(job_id).read_text())
    except (OSError, ValueError):
        return None


def stop(job_id: str) -> bool:
    """Ask a running job to stop. The worker ends with `finish(..., 'stopped')`."""
    stop_event = _STOP.get(job_id)
    if stop_event is None:
        return False
    stop_event.set()
    return True


def stop_requested(job_id: str, stop: threading.Event) -> bool:
    return stop.is_set()


def list_jobs() -> list[dict]:
    if not JOBS_DIR.exists():
        return []
    out: list[dict] = []
    for path in sorted(JOBS_DIR.glob("*.json")):
        try:
            out.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return out


__all__ = ["start", "update", "finish", "read", "stop", "list_jobs",
           "JOBS_DIR", "TERMINAL"]
