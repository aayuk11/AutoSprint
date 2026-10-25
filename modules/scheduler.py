"""
scheduler.py
Background timer that runs each project's report at its configured time.

It lives inside the web process, which keeps deployment simple - no extra
container, nothing for DevOps to schedule. The trade-off is that a timer in the
app fires in *every* worker and every replica, so the same report could be
posted more than once. Three things guard against that:

  1. A once-per-day check against the `schedule_runs` store - the main defence,
     and the reason a duplicate needs two instances to collide inside the same
     few seconds rather than merely both being alive.
  2. Claim-then-verify: an instance writes a claim, waits, then re-reads. The
     store is append-only with newest-wins, so all instances agree on a single
     winner and the losers back off.
  3. Only the first process to acquire a file lock starts a thread at all, so
     multiple gunicorn workers in one container schedule once between them.

If the app is ever scaled to several pods, prefer moving this to a Kubernetes
CronJob calling `run_scheduled.py` - the logic is already shared.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from tempfile import gettempdir
from zoneinfo import ZoneInfo

from . import runner, service   # NB: `runner` is a module - do not shadow it

log = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
# A slot is "due" any time after it passes, not only at the instant it does, so
# a coarse tick delays a report by minutes at most - and costs a fraction of the
# API calls. Irrelevant where the CronJob runs the reports instead.
TICK_SECONDS = 300
CLAIM_SETTLE_SECONDS = 5
_LOCK_PATH = Path(gettempdir()) / "sprint-report-scheduler.lock"

INSTANCE_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"

_started = False
_lock_handle = None


def is_enabled() -> bool:
    return os.environ.get("SCHEDULER_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def automation_mode() -> str:
    """What actually runs due reports: "app", "cron" or "off".

    SCHEDULER_ENABLED=false alone used to mean nothing would run, and the UI
    said so. That stopped being true once a CronJob started calling
    run_scheduled.py: the timer is off precisely *because* something else runs
    the reports, and warning that automation is dead would be wrong.
    """
    if os.environ.get("REPORTS_RUNNER", "").strip().lower() == "cron":
        return "cron"
    return "app" if is_enabled() else "off"


def _acquire_process_lock() -> bool:
    """Only one process per host runs the timer. Best-effort: an exclusive
    create that is released when the process exits."""
    global _lock_handle
    try:
        fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        # Stale lock from a crashed process? Take it over if it's old.
        try:
            if time.time() - _LOCK_PATH.stat().st_mtime > 10 * 60:
                _LOCK_PATH.unlink(missing_ok=True)
                fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            else:
                return False
        except OSError:
            return False
    except OSError:
        return False
    os.write(fd, INSTANCE_ID.encode())
    _lock_handle = fd
    return True


def _claim(project: str, today: str) -> bool:
    """Write a claim, let other instances write theirs, then check who won."""
    service.record_run(project, {"date": today, "by": INSTANCE_ID, "trigger": "scheduled",
                                 "status": "running", "at": datetime.now(IST).strftime("%Y-%m-%d %H:%M")})
    time.sleep(CLAIM_SETTLE_SECONDS)
    winner = (service.last_run(project) or {}).get("by")
    if winner != INSTANCE_ID:
        log.info("Skipping %s - claimed by %s", project, winner)
        return False
    return True


def run_due(now=None) -> list:
    """Run every project whose slot has passed today. Returns the results."""
    now = now or datetime.now(IST)
    today = now.strftime("%Y-%m-%d")
    done = []
    # Read the run history once, not once per project: last_run() fetches the
    # whole worksheet, so calling it inside the loop cost an API request per
    # project per tick.
    runs = service.load_store(service.RUNS_STORE) or {}
    for project, raw in (service.load_store(service.SCHEDULE_STORE) or {}).items():
        sched = service.normalize_schedule(raw)
        # Only a previous *scheduled* run uses up today's slot. Pressing
        # "Run now" is a separate, explicit action and must not stop the
        # timer firing later the same day. Records written before this field
        # existed count as manual, so they don't block a first scheduled run.
        prev = runs.get(project) or {}
        already = prev.get("date") == today and prev.get("trigger") == "scheduled"
        if not service.due_now(sched, now, already):
            continue
        if not _claim(project, today):
            continue
        log.info("Running scheduled report for %s", project)
        result = runner.run_project(project, formats=sched.get("formats"),
                                    post_summary=sched.get("post_summary", True))
        # Stamp with the tick's date, not the runner's own clock: the once-a-day
        # guard compares against this, and the two must not drift apart.
        result["date"] = today
        result["by"] = INSTANCE_ID
        result["trigger"] = "scheduled"
        result["status"] = "ok" if result["ok"] else "failed"
        service.record_run(project, result)
        done.append(result)
    return done


def _loop():
    log.info("Scheduler started (%s)", INSTANCE_ID)
    while True:
        try:
            run_due()
        except Exception:                      # never let the thread die
            log.exception("Scheduler tick failed")
        time.sleep(TICK_SECONDS)


def start() -> bool:
    """Start the timer once per process. Returns True if this process owns it."""
    global _started
    if _started or not is_enabled():
        return False
    if not _acquire_process_lock():
        log.info("Scheduler already running in another process; not starting here")
        return False
    _started = True
    threading.Thread(target=_loop, name="sprint-scheduler", daemon=True).start()
    return True
