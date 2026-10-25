"""
runner.py
Runs the whole report pipeline for one project without a browser: rebuild the
sprint form from saved settings, fetch from Jira, build the files, post them to
the project's Cliq channel.

This is deliberately free of any web-framework import so the same function backs
the in-app scheduler, the `run_scheduled.py` CLI, and (if the app is ever scaled
to more than one pod) a Kubernetes CronJob - without duplicating logic.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from . import cliq_client, jira_client, service

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def _now_ist() -> datetime:
    return datetime.now(IST)


def form_data_for(project: str) -> dict:
    """Rebuild the sprint form from saved details. total_days / days_left are
    recomputed for today - they're stored, but they're relative to the run date
    so a stale copy would be wrong."""
    saved = service.project_defaults(project)
    fd = dict(saved, project_name=project)
    return service.finalize_form(fd)


def run_project(project: str, formats=None, post_summary: bool = True,
                post: bool = True) -> dict:
    """Fetch -> build -> post for one project.

    Returns a result dict (never raises) so callers can log it and show it in
    the UI: {ok, project, at, issues, posted, formats, warnings, error}.
    """
    started = _now_ist()
    result = {"project": project, "at": started.strftime("%Y-%m-%d %H:%M"),
              "date": started.strftime("%Y-%m-%d"), "ok": False,
              "issues": 0, "posted": False, "formats": [], "warnings": [], "error": ""}
    try:
        blockers = service.schedule_blockers(project)
        if blockers:
            result["error"] = "Missing " + ", ".join(blockers)
            return result

        query = service.load_store("jira_config").get(project) or {}
        channel = service.load_store("zoho_channels").get(project) or ""

        if service.jql_targets_fixed_sprint(query.get("value", "")):
            result["warnings"].append(
                "The saved query targets a specific sprint rather than the open one, "
                "so this report may repeat a past sprint.")

        df = jira_client.fetch_issues(query)
        missing = service.validate_dataframe(df)
        if missing:
            result["error"] = "Jira data missing columns: " + ", ".join(missing)
            return result
        result["issues"] = len(df)

        fd = form_data_for(project)
        built = service.build_report(fd, df)
        files_by_fmt = built["files"]

        # None means "whatever the report produced"; an empty list is an explicit
        # choice of nothing and must not quietly turn into everything.
        if formats is None:
            wanted = list(files_by_fmt)
        else:
            wanted = [f for f in formats if f in files_by_fmt]
        result["formats"] = wanted
        if post and not wanted:
            result["error"] = "No file formats were selected to post."
            return result

        if post:
            message = service.cliq_summary_message(fd, built["parsed"]) if post_summary else ""
            if result["warnings"] and message:
                message += "\n_" + result["warnings"][0] + "_"
            problems = cliq_client.post_report(channel, [files_by_fmt[f] for f in wanted], message)
            if problems:
                result["warnings"].extend(problems)
            result["posted"] = True

        result["ok"] = True
        return result
    except Exception as exc:                      # a scheduled run must never crash the app
        log.exception("Scheduled run failed for %s", project)
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
