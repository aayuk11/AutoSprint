"""
run_scheduled.py
Run scheduled sprint reports from the command line - no browser involved.

    python run_scheduled.py --due                 # whatever is due right now
    python run_scheduled.py --project "Sample Project" --force
    python run_scheduled.py --all --dry-run       # build but don't post

Useful for testing the unattended path, and it is exactly what a Kubernetes
CronJob would call if the in-app timer is ever swapped out.
"""

from __future__ import annotations

import argparse
import logging
import sys

from modules import runner, scheduler, service


def main() -> int:
    ap = argparse.ArgumentParser(description="Run sprint reports unattended.")
    ap.add_argument("--project", help="run this project")
    ap.add_argument("--all", action="store_true", help="run every project that has a schedule")
    ap.add_argument("--due", action="store_true", help="run only what is due now (what the timer does)")
    ap.add_argument("--force", action="store_true", help="ignore the once-a-day guard")
    ap.add_argument("--dry-run", action="store_true", help="build the files but don't post to Cliq")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.due and not (args.project or args.all):
        results = scheduler.run_due()
    else:
        if args.project:
            projects = [args.project]
        elif args.all:
            projects = list(service.load_store(service.SCHEDULE_STORE) or {})
        else:
            ap.error("give --project NAME, --all, or --due")
        results = []
        for project in projects:
            sched = service.schedule_for(project)
            if not args.force and (service.last_run(project) or {}).get("date") == service.today_ist().isoformat():
                print(f"SKIP  {project} - already ran today (use --force)")
                continue
            result = runner.run_project(project, formats=sched.get("formats"),
                                        post_summary=sched.get("post_summary", True),
                                        post=not args.dry_run)
            result["status"] = "ok" if result["ok"] else "failed"
            result["trigger"] = "scheduled" if args.due else "manual"
            if not args.dry_run:
                service.record_run(project, result)
            results.append(result)

    failed = 0
    for r in results:
        mark = "OK  " if r["ok"] else "FAIL"
        detail = f"{r['issues']} issues, posted={r['posted']}" if r["ok"] else r["error"]
        print(f"{mark}  {r['project']}: {detail}")
        for w in r.get("warnings", []):
            print(f"      warning: {w}")
        failed += 0 if r["ok"] else 1

    if not results:
        print("Nothing to run.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
