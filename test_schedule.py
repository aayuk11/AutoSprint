"""Test the 9 PM scheduled run on your own machine, at any time of day.

    python test_schedule.py              # decide + build the files, post NOTHING
    python test_schedule.py --post       # decide + build + really post to Cliq

It pretends the clock has just reached 9 PM today and then does exactly what
the scheduled job does: work out which projects are due, and run those. The
decision uses the app's real functions, so what you see here is what the server
does - the only difference is that this does not coordinate with other copies
of the app, which matters on the server and not on your laptop.
"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from modules import runner, scheduler, service

IST = ZoneInfo("Asia/Kolkata")
POST = "--post" in sys.argv

# The build-only path below calls run_project itself, so it cannot notice a
# scheduled run that dies before reaching it - which is exactly what happened
# when a helper named `runner` shadowed the module of the same name and every
# run crashed with AttributeError while this script passed. Check the real
# entry point is intact before reporting on anything.
if not hasattr(scheduler.runner, "run_project"):
    sys.exit("BROKEN: modules/scheduler.py has shadowed the `runner` module - "
             "the scheduled run would crash. Nothing else here is meaningful.")

now = datetime.now(IST)
hh, mm = (int(x) for x in service.RUN_TIME.split(":"))
pretend = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
today = pretend.strftime("%Y-%m-%d")
weekday = pretend.strftime("%a")

print("=" * 66)
print(f"  Pretending it is {pretend:%A %d %B %Y, %H:%M} IST")
print(f"  Mode: {'POSTING FOR REAL to Cliq' if POST else 'build only - nothing will be posted'}")
print("=" * 66)

schedules = service.load_store(service.SCHEDULE_STORE) or {}
runs = service.load_store(service.RUNS_STORE) or {}
if not schedules:
    print("\nNo project has a schedule saved. Nothing would ever run.")
    raise SystemExit(1)

due, skipped = [], []
print("\nWhich projects would the 9 PM job run?\n")
for project, raw in sorted(schedules.items()):
    sched = service.normalize_schedule(raw)
    prev = runs.get(project) or {}
    already = prev.get("date") == today and prev.get("trigger") == "scheduled"

    if not sched.get("enabled"):
        reason = "its schedule is switched off"
    elif weekday not in (sched.get("days") or []):
        reason = f"{weekday} is not one of its days ({', '.join(sched.get('days') or [])})"
    elif already:
        reason = "it already ran automatically today"
    else:
        reason = None

    if reason:
        skipped.append(project)
        print(f"  SKIP  {project:24} - {reason}")
    else:
        due.append(project)
        print(f"  RUN   {project:24} - due at {sched['time']} on {weekday}")

if not due:
    print("\nNothing is due, so the 9 PM job would do nothing tonight.")
    print("That is not a failure - check the reasons above.")
    raise SystemExit(0)

print(f"\n{'-' * 66}\nRunning {len(due)} project(s)...\n")
failed = 0

if POST:
    # Hand over to the real thing: this is the call the CronJob makes, claim
    # records and all, so a fault anywhere in it shows up here too.
    for result in scheduler.run_due(now=pretend):
        mark = "OK  " if result["ok"] else "FAIL"
        detail = (f"{result['issues']} issues, posted={result['posted']}"
                  if result["ok"] else result["error"])
        print(f"  {mark} {result['project']}: {detail}")
        for w in result.get("warnings") or []:
            print(f"       note: {w[:150]}")
        failed += 0 if result["ok"] else 1
    print("\n" + "-" * 66)
    print(f"{failed} project(s) failed - the message above says why." if failed
          else "All ran and posted. This is exactly what 9 PM will do.")
    raise SystemExit(1 if failed else 0)

for project in due:
    sched = service.normalize_schedule(schedules.get(project))
    print(f"  {project} ...")
    result = runner.run_project(project, formats=sched.get("formats"),
                                post_summary=sched.get("post_summary", True),
                                post=POST)
    if result["ok"]:
        built = ", ".join(result["formats"]) or "nothing"
        print(f"    OK   {result['issues']} issues, built {built}, "
              f"posted={result['posted']}")
    else:
        failed += 1
        print(f"    FAIL {result['error']}")
    for w in result.get("warnings") or []:
        print(f"    note: {w[:150]}")
    print()

print("-" * 66)
if failed:
    print(f"{failed} project(s) failed - the message above says why.")
elif not POST:
    print("All built successfully. Nothing was posted (add --post to post).")
else:
    print("All ran and posted. This is exactly what 9 PM will do.")
raise SystemExit(1 if failed else 0)
