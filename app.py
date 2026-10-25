"""
app.py - Sprint Report Generator (Flask)

Routes
  /                      Step 1 - sprint details
  /data                  Step 2 - fetch from Jira or upload a CSV, with health checks
  /report                Step 3 - KPIs, preview, downloads, post to Cliq
  /download/<fmt>        Serve a generated file
  /post-cliq             Post selected files to a Zoho Cliq channel
  /settings              Per-project buckets, KPI cards, status mapping, column order
  /healthz               Liveness + configuration status

All report logic lives in modules/service.py; this file is only the web layer.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta

import pandas as pd
from flask import (Flask, Response, flash, redirect, render_template, request,
                   send_file, session, url_for)
from io import BytesIO

from modules import (auth, cliq_client, columns, config, jira_client, jobstore,
                     runner, scheduler, service, store)

app = Flask(__name__)

_secret, _secret_warning = config.secret_key(auth.is_enabled())
app.secret_key = _secret
if _secret_warning:
    logging.warning("SECURITY: %s", _secret_warning)

app.config.update(
    MAX_CONTENT_LENGTH=32 * 1024 * 1024,                       # 32 MB CSV cap
    PERMANENT_SESSION_LIFETIME=timedelta(days=auth.session_days()),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=auth.cookie_secure(),
)

# Reachable without signing in: the login page itself, the stylesheet it needs,
# and the health endpoint so monitoring doesn't need credentials.
PUBLIC_ENDPOINTS = {"login", "static", "healthz"}

# Background timer for scheduled reports. Starts once per process and is a
# no-op when SCHEDULER_ENABLED=false, so it can be left off in stage.
if scheduler.start():
    logging.info("Scheduled reports are active on this instance")

_MIGRATED = False


@app.before_request
def _gate():
    """Require a signed-in user (when the gate is enabled), then do the one-time
    legacy migration and temp-job cleanup."""
    if auth.is_enabled() and not session.get("user") and request.endpoint not in PUBLIC_ENDPOINTS:
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))

    global _MIGRATED
    if not _MIGRATED:
        _MIGRATED = True
        try:
            service.migrate_legacy_status_mappings()
        except Exception:
            pass
        jobstore.prune()


def _safe_next(target: str | None) -> str:
    """Only follow relative paths, so ?next= can't bounce users off-site."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("index")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth.is_enabled():
        return redirect(url_for("index"))
    if session.get("user"):
        return redirect(_safe_next(request.args.get("next")))

    error = None
    email = ""
    if request.method == "POST":
        ip = request.remote_addr or "unknown"
        wait = auth.locked_out(ip)
        if wait:
            error = f"Too many failed attempts. Try again in {wait} seconds."
        else:
            email = request.form.get("email", "")
            ok, error = auth.check(email, request.form.get("password", ""))
            if ok:
                auth.clear_failures(ip)
                session.clear()
                session["user"] = auth.normalize_email(email)
                session.permanent = True
                return redirect(_safe_next(request.form.get("next")))
            auth.record_failure(ip)

    return render_template("login.html", error=error, email=email,
                           domain=auth.allowed_domain(),
                           next=request.values.get("next", "")), (401 if error else 200)


@app.route("/logout")
def logout():
    session.clear()
    if auth.is_enabled():
        return redirect(url_for("login"))
    return redirect(url_for("index"))


@app.route("/access", methods=["GET", "POST"])
def access():
    """Who may sign in. Editable here (stored in the Zoho Sheet) so people can be
    added or removed without a redeploy - the point of the allowlist is being
    able to revoke access when someone leaves."""
    if not auth.is_enabled():
        flash("The login gate is off (APP_PASSWORD isn't set), so there's nothing to manage.", "warn")
        return redirect(url_for("index"))

    if request.method == "POST":
        emails = auth.stored_users()
        action = request.form.get("action")
        if action == "add":
            for raw in re.split(r"[,;\n]+", request.form.get("new_emails", "")):
                candidate = raw.strip().lower()
                if not candidate:
                    continue
                ok, err = auth.validate_email(candidate)
                if not ok:
                    flash(err, "error")
                elif candidate not in emails:
                    emails.append(candidate)
        elif action == "remove":
            target = (request.form.get("email") or "").strip().lower()
            # Don't let someone lock themselves out mid-session.
            if target == session.get("user"):
                flash("You can't remove your own access while signed in.", "error")
            else:
                emails = [e for e in emails if e != target]
        try:
            auth.set_allowed_users(emails)
            flash("Access list updated.", "ok")
        except Exception as exc:
            flash(f"Could not save the access list: {exc}", "error")
        return redirect(url_for("access"))

    return render_template("access.html", stored=auth.stored_users(),
                           env_users=auth.env_users(), domain=auth.allowed_domain())


@app.context_processor
def _globals():
    return {
        "backend_kind": store.backend_kind(),
        "jira_ready": jira_client.is_configured(),
        "cliq_ready": cliq_client.is_configured(),
        "active_project": session.get("project") or service.PROJECTS[0],
        "current_user": session.get("user"),
        "auth_enabled": auth.is_enabled(),
        "store_error": service.LAST_STORE_ERROR,
    }


# ------------------------------------------------------------------ helpers ---

def _current_project() -> str:
    return session.get("project") or service.PROJECTS[0]


def _form_date(name: str, fallback: date) -> date:
    raw = (request.form.get(name) or "").strip()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return fallback


def _job() -> str | None:
    job_id = session.get("job")
    return job_id if job_id and jobstore.exists(job_id) else None


def _require_form_data():
    """The finalized sprint form for the active project, or None."""
    return jobstore.load_object(_job(), "form") if _job() else None


# ------------------------------------------------------------- step 1: form ---

@app.route("/", methods=["GET", "POST"])
def index():
    project = _current_project()

    if request.method == "POST":
        # A typed-in name wins over the dropdown, so a new project can be added
        # without touching the sheet or redeploying.
        typed = (request.form.get("new_project") or "").strip()
        if typed:
            project, err = service.clean_project_name(typed)
            if err:
                flash(err, "error")
                return render_template("index.html", project=_current_project(),
                                       fd=dict(service.project_defaults(_current_project()),
                                               project_name=_current_project()),
                                       projects=service.project_options())
        else:
            project = (request.form.get("project") or project).strip() or project
        session["project"] = project

        # Just switching project - reload that project's saved details.
        if request.form.get("action") == "switch":
            session.pop("job", None)
            return redirect(url_for("index"))

        defaults = service.project_defaults(project)
        sprint_type = request.form.get("sprint_type", defaults["sprint_type"])
        fd = {
            "project_name": project,
            "sprint_type": sprint_type,
            "sprint_number": int(request.form.get("sprint_number") or 1),
            "sprint_start": _form_date("sprint_start", defaults["sprint_start"]),
            "sprint_end": _form_date("sprint_end", defaults["sprint_end"]),
            "sprint_release": _form_date("sprint_release", defaults["sprint_release"]),
            "dev_release": _form_date("dev_release", defaults["dev_release"]),
            "qa_release": _form_date("qa_release", defaults["qa_release"]),
            "prod_release": _form_date("prod_release", defaults["prod_release"]),
            "scrum_master": (request.form.get("scrum_master") or "").strip(),
            "sprint_goal": (request.form.get("sprint_goal") or "").strip(),
            "major_item_1": (request.form.get("major_item_1") or "").strip(),
            "major_item_2": (request.form.get("major_item_2") or "").strip(),
            "major_item_3": (request.form.get("major_item_3") or "").strip(),
        }
        errors = service.validate_form(fd)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("index.html", project=project, fd=fd,
                                   projects=service.project_options())

        fd = service.finalize_form(fd)
        service.save_sprint_details(project, fd)
        job_id = jobstore.new_job()
        session["job"] = job_id
        jobstore.save_object(job_id, "form", fd)
        return redirect(url_for("data"))

    defaults = service.project_defaults(project)
    fd = dict(defaults, project_name=project)
    return render_template("index.html", project=project, fd=fd,
                           projects=service.project_options())


# -------------------------------------------------------------- step 2: data ---

@app.route("/data", methods=["GET", "POST"])
def data():
    fd = _require_form_data()
    if not fd:
        flash("Fill in the sprint details first.", "error")
        return redirect(url_for("index"))
    project = fd["project_name"]
    job_id = _job()
    saved_query = service.load_store("jira_config").get(project, {})

    if request.method == "POST":
        action = request.form.get("action")
        query = {"mode": request.form.get("mode", "jql"),
                 "value": (request.form.get("query") or "").strip()}

        if action in ("fetch", "save_query") and query["value"]:
            service.save_to_store("jira_config", project, query)
            saved_query = query

        if action == "save_query":
            flash("Query saved for this project.", "ok")
            return redirect(url_for("data"))

        if action == "fetch":
            if not query["value"]:
                flash("Enter a JQL query or saved-filter ID first.", "error")
                return redirect(url_for("data"))
            try:
                df = jira_client.fetch_issues(query)
            except Exception as exc:
                flash(f"Jira fetch failed: {exc}", "error")
                return redirect(url_for("data"))
            missing = service.validate_dataframe(df)
            if missing:
                flash(f"Missing required columns: {', '.join(missing)}", "error")
                return redirect(url_for("data"))
            jobstore.save_dataframe(job_id, df, f"Jira • {len(df)} issues")
            jobstore.clear_files(job_id)
            flash(f"Fetched {len(df)} issues from Jira.", "ok")
            return redirect(url_for("data"))

        if action == "upload":
            file = request.files.get("csv")
            if not file or not file.filename:
                flash("Choose a CSV file to upload.", "error")
                return redirect(url_for("data"))
            try:
                df = pd.read_csv(file)
            except Exception as exc:
                flash(f"Could not read CSV: {exc}", "error")
                return redirect(url_for("data"))
            missing = service.validate_dataframe(df)
            if missing:
                flash(f"Missing required columns: {', '.join(missing)}", "error")
                return redirect(url_for("data"))
            jobstore.save_dataframe(job_id, df, file.filename)
            jobstore.clear_files(job_id)
            flash(f"Loaded {len(df)} rows from {file.filename}.", "ok")
            return redirect(url_for("data"))

    df = jobstore.load_dataframe(job_id)
    health = service.data_health(df, project) if df is not None else None
    return render_template(
        "data.html", fd=fd, project=project, saved_query=saved_query,
        health=health, source=jobstore.source_label(job_id) if df is not None else None,
        date_fields=jira_client.last_date_fields(),
    )


# ------------------------------------------------------------ step 3: report ---

@app.route("/report")
def report():
    fd = _require_form_data()
    if not fd:
        flash("Fill in the sprint details first.", "error")
        return redirect(url_for("index"))
    job_id = _job()
    df = jobstore.load_dataframe(job_id)
    if df is None:
        flash("Fetch from Jira or upload a CSV first.", "error")
        return redirect(url_for("data"))

    parsed = jobstore.load_object(job_id, "parsed")
    if parsed is None or not jobstore.file_meta(job_id):
        try:
            result = service.build_report(fd, df)
        except Exception as exc:
            flash(f"Could not build the report: {exc}", "error")
            return redirect(url_for("data"))
        parsed = result["parsed"]
        jobstore.save_files(job_id, result["files"])
        jobstore.save_object(job_id, "parsed", parsed)

    kpis = parsed["kpis"]
    daily_task = round(kpis["action_items"] / fd["total_days"], 2) if fd["total_days"] else 0
    channel = service.load_store("zoho_channels").get(fd["project_name"], "")
    return render_template(
        "report.html", fd=fd, parsed=parsed, kpis=kpis, daily_task=daily_task,
        files=jobstore.file_meta(job_id), channel=channel,
    )


@app.route("/download/<fmt>")
def download(fmt):
    job_id = _job()
    item = jobstore.load_file(job_id, fmt) if job_id else None
    if not item:
        flash("That file is no longer available - generate the report again.", "error")
        return redirect(url_for("report"))
    filename, data, mime = item
    return send_file(BytesIO(data), mimetype=mime, as_attachment=True, download_name=filename)


@app.route("/post-cliq", methods=["POST"])
def post_cliq():
    fd = _require_form_data()
    job_id = _job()
    if not fd or not job_id:
        return redirect(url_for("index"))
    channel = (request.form.get("channel") or "").strip()
    if not channel:
        flash("Enter the Cliq channel name.", "error")
        return redirect(url_for("report"))
    service.save_to_store("zoho_channels", fd["project_name"], channel)

    if request.form.get("action") == "save_channel":
        flash("Channel saved.", "ok")
        return redirect(url_for("report"))

    formats = request.form.getlist("formats")
    if not formats:
        flash("Select at least one file to post.", "error")
        return redirect(url_for("report"))
    files = [jobstore.load_file(job_id, f) for f in formats]
    files = [f for f in files if f]
    message = ""
    if request.form.get("include_summary"):
        parsed = jobstore.load_object(job_id, "parsed")
        message = service.cliq_summary_message(fd, parsed) if parsed else ""
    try:
        warnings = cliq_client.post_report(channel, files, message)
    except Exception as exc:
        flash(f"Could not post to Cliq: {exc}", "error")
        return redirect(url_for("report"))
    if warnings:
        flash("Posted, but some files failed: " + " | ".join(warnings), "warn")
    else:
        flash(f"Posted {len(files)} file(s) to #{channel}.", "ok")
    return redirect(url_for("report"))


@app.route("/reset")
def reset():
    job_id = session.pop("job", None)
    if job_id:
        jobstore.clear_files(job_id)
    return redirect(url_for("index"))


# ---------------------------------------------------------------- settings ---

@app.route("/settings", methods=["GET", "POST"])
def settings():
    project = request.values.get("project") or _current_project()
    if project:
        cleaned, err = service.clean_project_name(project)
        if err:
            flash(err, "error")
            return redirect(url_for("settings", project=_current_project()))
        project = cleaned

    if request.method == "POST":
        action = request.form.get("action")
        cfg = service.project_config(project)
        buckets = cfg["buckets"]
        kpis = cfg["kpis"]

        if action == "select_project":
            session["project"] = project
            return redirect(url_for("settings", project=project))

        if action == "reset":
            service.delete_from_store("project_configs", project)
            flash(f"Reset {project} to defaults.", "ok")
            return redirect(url_for("settings", project=project))

        # Rebuild buckets/KPIs/columns from the submitted form.
        buckets = []
        for key in request.form.getlist("bucket_key"):
            label = (request.form.get(f"bucket_label_{key}") or "").strip()
            if not label:
                continue
            buckets.append({"key": key, "label": label,
                            "color": request.form.get(f"bucket_color_{key}") or "#888888"})
        new_label = (request.form.get("new_bucket_label") or "").strip()
        if new_label:
            idx = len(buckets)
            buckets.append({"key": f"bucket_{_next_seq(cfg['buckets'], 'bucket')}",
                            "label": new_label,
                            "color": request.form.get("new_bucket_color") or service.palette_color(idx)})

        valid_bucket_keys = {b["key"] for b in buckets}
        kpis = []
        for key in request.form.getlist("kpi_key"):
            label = (request.form.get(f"kpi_label_{key}") or "").strip()
            if not label:
                continue
            kpis.append({
                "key": key, "label": label,
                "color": request.form.get(f"kpi_color_{key}") or "#888888",
                "bucket_keys": [b for b in request.form.getlist(f"kpi_buckets_{key}") if b in valid_bucket_keys],
            })
        new_kpi = (request.form.get("new_kpi_label") or "").strip()
        if new_kpi:
            kpis.append({"key": f"kpi_{_next_seq(cfg['kpis'], 'kpi')}", "label": new_kpi,
                         "color": request.form.get("new_kpi_color") or "#2E75B6",
                         "bucket_keys": []})

        status_map = {}
        for lower_key in request.form.getlist("status_key"):
            bucket = request.form.get(f"status_map_{lower_key}")
            if bucket and bucket in valid_bucket_keys:
                status_map[lower_key] = bucket

        known = [s.strip() for s in (request.form.get("known_statuses") or "").splitlines() if s.strip()]
        for extra in (request.form.get("new_statuses") or "").splitlines():
            if extra.strip() and extra.strip() not in known:
                known.append(extra.strip())

        order = request.form.getlist("column_key")
        visible = set(request.form.getlist("column_visible"))
        task_columns = [{"key": k, "visible": k in visible} for k in order if k in columns.BY_KEY]

        service.save_to_store("project_configs", project, {
            "buckets": buckets, "kpis": kpis, "status_map": status_map,
            "known_statuses": known, "task_columns": task_columns,
        })
        flash(f"Saved: {len(buckets)} bucket(s), {len(kpis)} KPI card(s), "
              f"{len(status_map)} status(es) mapped.", "ok")
        return redirect(url_for("settings", project=project))

    cfg = service.project_config(project)
    task_columns = columns.resolve(cfg.get("task_columns"), service.sprint_type_for_project(project))
    label_header = ("Requirement Type"
                    if service.sprint_type_for_project(project) == service.DESIGN_SPRINT else "Label")
    statuses = sorted({str(s).lower().strip(): str(s) for s in cfg.get("known_statuses", [])}.items(),
                      key=lambda kv: kv[1].lower())
    saved_query = (service.load_store("jira_config").get(project) or {}).get("value", "")
    _last_run = service.last_run(project)
    return render_template(
        "settings.html", project=project, projects=service.project_options(), cfg=cfg,
        statuses=statuses, task_columns=task_columns, all_columns=columns.BY_KEY,
        label_header=label_header, labels_key=columns.LABELS_KEY,
        schedule=service.schedule_for(project), weekdays=service.WEEKDAYS,
        run_time_label=service.RUN_TIME_LABEL,
        blockers=service.schedule_blockers(project), last_run=_last_run,
        last_run_interrupted=service.run_interrupted(_last_run),
        stale_sprint=service.jql_targets_fixed_sprint(saved_query),
        automation=scheduler.automation_mode(),
    )


def _next_seq(rows: list, prefix: str) -> int:
    nums = []
    for r in rows:
        parts = str(r.get("key", "")).rsplit("_", 1)
        if len(parts) == 2 and parts[0] == prefix and parts[1].isdigit():
            nums.append(int(parts[1]))
    return (max(nums) + 1) if nums else 0


@app.route("/schedule", methods=["POST"])
def schedule():
    """Save a project's automation settings, or run it immediately."""
    project = (request.form.get("project") or _current_project()).strip()
    action = request.form.get("action")

    if action == "run_now":
        # Use what is ticked right now - the Run now button sits inside the
        # schedule form, so reading the saved settings instead would silently
        # ignore the user's selection.
        formats = request.form.getlist("formats")
        if not formats:
            flash("Select at least one file to post, then try Run now again.", "error")
            return redirect(url_for("settings", project=project))
        result = runner.run_project(project, formats=formats,
                                    post_summary=bool(request.form.get("post_summary")))
        result["status"] = "ok" if result["ok"] else "failed"
        result["trigger"] = "manual"      # doesn't consume today's scheduled slot
        service.record_run(project, result)
        if result["ok"]:
            flash(f"Ran {project}: {result['issues']} issues, "
                  f"posted {', '.join(result['formats'])}.", "ok")
        else:
            flash(f"Run failed for {project}: {result['error']}", "error")
        for warning in result.get("warnings", []):
            flash(warning, "warn")
        return redirect(url_for("settings", project=project))

    enabled = bool(request.form.get("enabled"))
    blockers = service.schedule_blockers(project)
    if enabled and blockers:
        flash("Can't enable the schedule yet - this project still needs "
              + ", ".join(blockers) + ".", "error")
        enabled = False

    service.save_schedule(project, {
        "enabled": enabled,
        "time": service.RUN_TIME,          # fixed by the CronJob, not the form
        "days": request.form.getlist("days") or list(service.WEEKDAYS),
        "formats": request.form.getlist("formats") or ["Excel", "PDF", "Image"],
        "post_summary": bool(request.form.get("post_summary")),
    })
    flash("Schedule saved." if enabled else "Schedule saved (currently disabled).", "ok")
    return redirect(url_for("settings", project=project))


@app.route("/healthz")
def healthz():
    """Liveness plus a real check that storage works.

    Reporting the backend name alone was misleading: it only reflects that the
    ZOHO_* variables exist, so a pod pointed at the wrong sheet - or one whose
    reads fail - still claimed "ZohoSheetBackend" while every page came up empty.
    This actually performs a read and reports what came back.
    """
    st = config.status()
    storage = {"backend": store.backend_kind()}
    try:
        details = store.get_backend().load_all("sprint_details")
        configs = store.get_backend().load_all("project_configs")
        storage["readable"] = True
        storage["projects"] = sorted(set(details) | set(configs))
        storage["project_count"] = len(storage["projects"])
        if storage["backend"] == "ZohoSheetBackend":
            zoho = config.section("zoho") or {}
            storage["worksheet"] = zoho.get("worksheet_name", "app_data")
            rid = zoho.get("resource_id", "")
            # Enough to compare against the intended sheet without exposing it.
            storage["resource_id_hint"] = f"{rid[:6]}...{rid[-4:]}" if len(rid) > 10 else "(short/unset)"
    except Exception as exc:
        storage["readable"] = False
        storage["error"] = f"{type(exc).__name__}: {exc}"[:300]

    ok = all(v[0] for v in st.values()) and storage.get("readable")
    body = {"status": "ok" if ok else "partial",
            "storage": storage,
            "scheduler": {"enabled": scheduler.is_enabled(), "runner": scheduler.automation_mode()},
            "config": {k: {"configured": v[0], "missing": v[1]} for k, v in st.items()}}
    return body, 200


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", "8501")),
            debug=os.environ.get("FLASK_DEBUG") == "1")
