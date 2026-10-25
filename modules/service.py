"""
service.py
All report logic that is independent of the web layer: project defaults, config
resolution, data ingestion, and building the Excel/PDF/PNG artifacts.

Keeping this UI-free means the same code powers the Flask app today and a cron /
CLI runner later, with no duplication.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from . import columns, store
from .excel_generator import build_excel
from .image_generator import build_summary_image
from .parser import STORY_LEVEL_TYPES, distinct_statuses, parse_jira_csv
from .pdf_generator import build_pdf

DESIGN_PROJECT = "Product Design"
DESIGN_SPRINT = "Design Sprint"
PRODUCT_SPRINT = "Product Sprint"

# Starter list only. Any name typed on step 1 creates a project, so this is
# just what the dropdown offers on a fresh install.
PROJECTS = [
    "Product Design",
    "Sample Project",
]

REQUIRED_COLUMNS = ["Issue key", "Issue Type", "Summary", "Status"]

PALETTE = ['#ED7D31', '#00B0F0', '#BF8F00', '#FFC000', '#70AD47', '#00B050',
           '#375623', '#A6A6A6', '#7030A0', '#2E75B6', '#C00000', '#1F4E79']

# ---------------------------------------------------------------- defaults ---

_PRODUCT_DEFAULT_STATUS_MAP = {
    "to do": "not_initiated", "not initiated": "not_initiated", "open": "not_initiated",
    "in progress": "in_progress",
    "staging deployed": "staging", "staging": "staging", "stage deployed": "staging",
    "qa review": "qa_review", "in review": "qa_review",
    "qa deployed": "qa_deployed",
    "qa approved": "qa_approved", "qa": "qa_approved",
    "done": "production", "production": "production", "released": "production", "closed": "production",
    "on hold": "on_hold", "blocked": "on_hold",
    "to be picked in another sprint": "to_be_picked", "deferred": "to_be_picked",
}

_LEGACY_BUCKETS = [
    ("not_initiated", "Not Initiated", "#ED7D31"),
    ("in_progress", "In Progress", "#00B0F0"),
    ("staging", "Staging", "#BF8F00"),
    ("qa_review", "QA Review", "#FFC000"),
    ("qa_deployed", "QA Deployed", "#70AD47"),
    ("qa_approved", "QA Approved (Completed-QA)", "#00B050"),
    ("production", "Production", "#375623"),
    ("on_hold", "On Hold", "#A6A6A6"),
    ("to_be_picked", "To Be Picked (Another Sprint)", "#7030A0"),
]
_LEGACY_PCTS = [
    ("not_initiated_pct", "Not Initiated %", "#ED7D31", ["not_initiated"]),
    ("pending_pct", "Pending %", "#FFC000", ["in_progress", "staging"]),
    ("completion_qa_pct", "Completion - QA %", "#00B050", ["qa_review", "qa_deployed", "qa_approved"]),
    ("production_release_pct", "Production Release %", "#375623", ["production"]),
]

# Per-project overrides can be added here, but every project is fully
# configurable from the Settings page, so this ships empty.
_BUILTIN_PROJECT_MAPS: dict = {}

_DESIGN_DEFAULT = {
    "buckets": [
        {"key": "bucket_0", "label": "Not Initiated", "color": "#ED7D31"},
        {"key": "bucket_1", "label": "In Progress", "color": "#f0e600"},
        {"key": "bucket_2", "label": "Completed", "color": "#3bbf00"},
        {"key": "bucket_3", "label": "Document Pending", "color": "#bdbbbb"},
        {"key": "bucket_4", "label": "On Hold", "color": "#515251"},
        {"key": "bucket_5", "label": "Rework", "color": "#00b0a6"},
        {"key": "bucket_6", "label": "Wireframe Ready", "color": "#5edecf"},
        {"key": "bucket_7", "label": "In Review", "color": "#3386d8"},
    ],
    "kpis": [
        {"key": "kpi_0", "label": "Completion %", "color": "#3bbf00", "bucket_keys": ["bucket_2"]},
        {"key": "kpi_1", "label": "Pending %", "color": "#e6e200",
         "bucket_keys": ["bucket_1", "bucket_3", "bucket_4", "bucket_5", "bucket_6"]},
        {"key": "kpi_2", "label": "Not Initiated %", "color": "#ed7d31", "bucket_keys": ["bucket_0"]},
        {"key": "kpi_3", "label": "In Review %", "color": "#3386d8", "bucket_keys": ["bucket_7"]},
    ],
    "status_map": {
        "completed": "bucket_2", "document pending": "bucket_3", "documentation pending": "bucket_3",
        "in progress": "bucket_1", "in review": "bucket_7", "not initiated": "bucket_0",
        "on hold": "bucket_4", "rework": "bucket_5", "wireframe ready": "bucket_6",
    },
    "known_statuses": ["Completed", "Document Pending", "Documentation Pending", "In Progress",
                       "In Review", "Not Initiated", "On Hold", "Rework", "Wireframe Ready"],
}


def today_ist() -> date:
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def palette_color(idx: int) -> str:
    return PALETTE[idx % len(PALETTE)]


def _from_legacy(old: dict) -> dict:
    pct_buckets = old.get("pct_buckets") or {}
    return {
        "buckets": [{"key": k, "label": l, "color": c} for (k, l, c) in _LEGACY_BUCKETS],
        "kpis": [{"key": pk, "label": l, "color": c, "bucket_keys": list(pct_buckets.get(pk, dflt))}
                 for (pk, l, c, dflt) in _LEGACY_PCTS],
        "status_map": dict(old.get("status_map") or {}),
        "known_statuses": list(old.get("known_statuses") or []),
    }


def default_config_for(project: str) -> dict:
    """Config for a project with nothing saved yet - never an empty shell, so
    existing projects keep working untouched until someone edits Settings."""
    if project == DESIGN_PROJECT:
        cfg = {
            "buckets": [dict(b) for b in _DESIGN_DEFAULT["buckets"]],
            "kpis": [dict(k) for k in _DESIGN_DEFAULT["kpis"]],
            "status_map": dict(_DESIGN_DEFAULT["status_map"]),
            "known_statuses": list(_DESIGN_DEFAULT["known_statuses"]),
        }
    elif project in _BUILTIN_PROJECT_MAPS:
        builtin = _BUILTIN_PROJECT_MAPS[project]
        known = [s.title() for s in builtin["status_map"]]
        cfg = _from_legacy({**builtin, "known_statuses": known})
    else:
        known = [s.title() for s in dict.fromkeys(_PRODUCT_DEFAULT_STATUS_MAP)]
        cfg = _from_legacy({"status_map": dict(_PRODUCT_DEFAULT_STATUS_MAP),
                            "pct_buckets": {}, "known_statuses": known})
    cfg["task_columns"] = None
    return cfg


# ------------------------------------------------------------- store access ---

# Set when a storage read fails, so the UI can say so instead of rendering an
# empty page. A swallowed error looked exactly like "no data saved yet", which
# sent people hunting for lost settings that were never actually lost.
LAST_STORE_ERROR = ""


def load_store(name: str) -> dict:
    global LAST_STORE_ERROR
    try:
        data = store.get_backend().load_all(name)
        LAST_STORE_ERROR = ""
        return data
    except Exception as exc:
        LAST_STORE_ERROR = f"Could not read '{name}' from storage - {type(exc).__name__}: {exc}"[:300]
        return {}


def save_to_store(name: str, project: str, obj) -> None:
    store.get_backend().save_one(name, project, obj)


def delete_from_store(name: str, project: str) -> None:
    store.get_backend().delete_one(name, project)


def project_config(project: str) -> dict:
    """Saved config for a project, else the built-in default."""
    cfg = load_store("project_configs").get(project)
    if cfg:
        return {
            "buckets": [dict(b) for b in cfg.get("buckets", [])],
            "kpis": [dict(k) for k in cfg.get("kpis", [])],
            "status_map": dict(cfg.get("status_map", {})),
            "known_statuses": list(cfg.get("known_statuses", [])),
            "task_columns": [dict(c) for c in cfg.get("task_columns", [])] or None,
        }
    return default_config_for(project)


def project_options() -> list:
    options = list(PROJECTS)
    for name in list(load_store("sprint_details")) + list(load_store("project_configs")):
        if name not in options:
            options.append(name)
    return options


MAX_PROJECT_NAME = 60


def clean_project_name(name: str) -> tuple[str, str]:
    """Tidy a user-typed project name. Returns (name, error).

    Collapses whitespace, caps the length, and reuses the existing spelling when
    the name already exists in a different case - otherwise 'Acme' and 'acme'
    would become two projects with separate settings.
    """
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        return "", "Enter a project name."
    if len(cleaned) > MAX_PROJECT_NAME:
        return "", f"Project names must be {MAX_PROJECT_NAME} characters or fewer."
    for existing in project_options():
        if existing.lower() == cleaned.lower():
            return existing, ""
    return cleaned, ""


def sprint_type_for_project(project: str) -> str:
    saved = load_store("sprint_details").get(project, {})
    default_type = DESIGN_SPRINT if project == DESIGN_PROJECT else PRODUCT_SPRINT
    return saved.get("sprint_type", default_type)


def migrate_legacy_status_mappings() -> int:
    """One-time: convert the old fixed-bucket 'status_mappings' rows into the
    configurable 'project_configs' format. Additive - old rows are left alone."""
    legacy = load_store("status_mappings")
    if not legacy:
        return 0
    configs = load_store("project_configs")
    migrated = 0
    for project, old in legacy.items():
        if project in configs or not isinstance(old, dict) or not old.get("status_map"):
            continue
        new_cfg = _from_legacy(old)
        save_to_store("project_configs", project, new_cfg)
        migrated += 1
    return migrated


# -------------------------------------------------------------- sprint form ---

def parse_saved_date(value, fallback: date) -> date:
    if not value:
        return fallback
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return fallback


def project_defaults(project: str) -> dict:
    """Form values for a project: saved sprint details, else sensible defaults."""
    today = today_ist()
    saved = load_store("sprint_details").get(project, {})
    return {
        "sprint_type": saved.get("sprint_type", sprint_type_for_project(project)),
        "sprint_number": int(saved.get("sprint_number", 1) or 1),
        "sprint_start": parse_saved_date(saved.get("sprint_start"), today),
        "sprint_end": parse_saved_date(saved.get("sprint_end"), today),
        "sprint_release": parse_saved_date(saved.get("sprint_release"), today),
        "dev_release": parse_saved_date(saved.get("dev_release"), today),
        "qa_release": parse_saved_date(saved.get("qa_release"), today),
        "prod_release": parse_saved_date(saved.get("prod_release"), today),
        "scrum_master": saved.get("scrum_master", ""),
        "sprint_goal": saved.get("sprint_goal", ""),
        "major_item_1": saved.get("major_item_1", ""),
        "major_item_2": saved.get("major_item_2", ""),
        "major_item_3": saved.get("major_item_3", ""),
    }


def validate_form(fd: dict) -> list:
    """Same rules as the previous app - returns a list of error strings."""
    errors = []
    if not str(fd.get("scrum_master", "")).strip():
        errors.append("Scrum Master name is required.")
    if fd.get("sprint_type") == DESIGN_SPRINT:
        if fd["sprint_end"] < fd["sprint_start"]:
            errors.append("Sprint End Date cannot be before Sprint Start.")
        if fd["sprint_release"] < fd["sprint_start"]:
            errors.append("Sprint Release Date cannot be before Sprint Start.")
    else:
        if fd["dev_release"] < fd["sprint_start"]:
            errors.append("Dev Release cannot be before Sprint Start.")
        if fd["qa_release"] < fd["dev_release"]:
            errors.append("QA Release cannot be before Dev Release.")
        if fd["prod_release"] < fd["qa_release"]:
            errors.append("Production Release cannot be before QA Release.")
        if fd["sprint_end"] < fd["sprint_start"]:
            errors.append("Sprint End Date cannot be before Sprint Start.")
    return errors


def finalize_form(fd: dict) -> dict:
    """Add the derived fields the generators expect (dates stay as date objects)."""
    fd = dict(fd)
    is_design = fd.get("sprint_type") == DESIGN_SPRINT
    end_ref = fd["sprint_release"] if is_design else fd["sprint_end"]
    fd["total_days"] = (end_ref - fd["sprint_start"]).days + 1
    fd["days_left"] = max((end_ref - today_ist()).days + 1, 0)
    fd["label_header"] = "Requirement Type" if is_design else "Label"
    fd["meta_items"] = build_meta_items(fd)
    fd["task_columns"] = columns.resolve(
        project_config(fd["project_name"]).get("task_columns"), fd.get("sprint_type")
    )
    return fd


def serialize_form(fd: dict) -> dict:
    out = {}
    for k, v in fd.items():
        if k in ("meta_items", "label_header", "task_columns"):
            continue
        out[k] = v.isoformat() if isinstance(v, date) else v
    return out


def save_sprint_details(project: str, fd: dict) -> None:
    save_to_store("sprint_details", project, serialize_form(fd))


def build_meta_items(fd: dict) -> list:
    if fd.get("sprint_type") == DESIGN_SPRINT:
        return [
            ("Project", fd["project_name"], "#000000"),
            ("Sprint Number", fd["sprint_number"], "#1F4E79"),
            ("Sprint Start Date", fd["sprint_start"].strftime("%d %b %Y"), "#2E75B6"),
            ("Sprint End Date", fd["sprint_end"].strftime("%d %b %Y"), "#375623"),
            ("Sprint Release Date", fd["sprint_release"].strftime("%d %b %Y"), "#C00000"),
            ("Total No. of Days", fd["total_days"], "#595959"),
            ("Scrum Master", fd["scrum_master"], "#7030A0"),
        ]
    return [
        ("Project", fd["project_name"], "#000000"),
        ("Sprint Number", fd["sprint_number"], "#1F4E79"),
        ("Sprint Start Date", fd["sprint_start"].strftime("%d %b %Y"), "#2E75B6"),
        ("Sprint Development Release", fd["dev_release"].strftime("%d %b %Y"), "#00B0F0"),
        ("Sprint QA Release", fd["qa_release"].strftime("%d %b %Y"), "#BF8F00"),
        ("Production Release", fd["prod_release"].strftime("%d %b %Y"), "#C00000"),
        ("Sprint End Date", fd["sprint_end"].strftime("%d %b %Y"), "#375623"),
        ("Total No. of Days", fd["total_days"], "#595959"),
        ("Scrum Master", fd["scrum_master"], "#7030A0"),
    ]


def report_base_name(fd: dict) -> str:
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d%b%Y")
    project = str(fd["project_name"]).replace("_", " ")
    return f"{project}_Sprint{fd['sprint_number']}_{today}"


# ------------------------------------------------------------ data + report ---

def validate_dataframe(df: pd.DataFrame) -> list:
    return [c for c in REQUIRED_COLUMNS if c not in df.columns]


def data_health(df: pd.DataFrame, project: str) -> dict:
    """Counts and warnings shown after a fetch/upload, so a misconfiguration
    (e.g. the wrong Jira date fields) can never be silent."""
    non_epic = df[df["Issue Type"] != "Epic"] if "Issue Type" in df.columns else df
    cfg = project_config(project)
    status_map = {str(k).lower().strip(): v for k, v in cfg.get("status_map", {}).items()}

    def dates(col):
        return pd.to_datetime(df[col], errors="coerce") if col in df.columns \
            else pd.Series(pd.NaT, index=df.index)

    both = int((dates("Custom field (Target start)").notna()
                & dates("Custom field (Target end)").notna()).sum())
    known_types = set(STORY_LEVEL_TYPES) | {"Epic", "Sub-task"}
    keys = set(df["Issue key"].astype(str)) if "Issue key" in df.columns else set()
    story_rows = df[df["Issue Type"].isin(STORY_LEVEL_TYPES)] if "Issue Type" in df.columns else df.iloc[0:0]
    missing_epics = sorted({str(p) for p in story_rows.get("Parent key", pd.Series(dtype=str)).dropna()
                            if str(p).strip() and str(p) not in keys})
    return {
        "rows": len(df),
        "epics": int((df["Issue Type"] == "Epic").sum()) if "Issue Type" in df.columns else 0,
        "stories": int(df["Issue Type"].isin(["Story", "Task"]).sum()) if "Issue Type" in df.columns else 0,
        "subtasks": int((df["Issue Type"] == "Sub-task").sum()) if "Issue Type" in df.columns else 0,
        "status_counts": non_epic["Status"].value_counts().to_dict() if "Status" in non_epic.columns else {},
        "unmapped_statuses": sorted({s for s in distinct_statuses(non_epic)
                                     if s.lower().strip() not in status_map}),
        "with_dates": both,
        "dropped_types": sorted({str(t) for t in df["Issue Type"].dropna().unique()
                                 if str(t) not in known_types}) if "Issue Type" in df.columns else [],
        "missing_epics": missing_epics,
        "preview": df[[c for c in ["Issue key", "Issue Type", "Summary", "Status",
                                   "Priority", "Assignee", "Parent key"] if c in df.columns]].head(5),
    }


def build_report(fd: dict, df: pd.DataFrame) -> dict:
    """Parse + build all three artifacts. Returns parsed data and file bytes."""
    parsed = parse_jira_csv(df, fd["project_name"], project_config(fd["project_name"]))
    base = report_base_name(fd)
    return {
        "parsed": parsed,
        "files": {
            "Excel": (f"{base}.xlsx", build_excel(fd, parsed),
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "PDF": (f"{base}.pdf", build_pdf(fd, parsed), "application/pdf"),
            "Image": (f"{base}.png", build_summary_image(fd, parsed), "image/png"),
        },
    }


def cliq_summary_message(fd: dict, parsed: dict) -> str:
    kpis = parsed["kpis"]
    bits = " | ".join(
        f"{k['label']}: {kpis['kpi_values'].get(k['key'], {}).get('pct_display', '0%')}"
        for k in parsed["kpi_defs"]
    )
    return (f"*Sprint {fd['sprint_number']} report — {fd['project_name']}*\n"
            f"Action Items: {kpis['action_items']}" + (f" | {bits}" if bits else "") +
            f"\nScrum Master: {fd['scrum_master']}")


# ----------------------------------------------------------------- schedule ---
# Per-project automation: when to run, what to post. Stored like every other
# setting so it survives restarts and can be edited without a redeploy.

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SCHEDULE_STORE = "schedules"
RUNS_STORE = "schedule_runs"


# The CronJob decides when reports run; the app no longer keeps its own clock.
# The field is still carried on every schedule because due_now() compares
# against it, but it is fixed rather than per-project - a project set to any
# other time would either duplicate the job or never fire at all.
RUN_TIME = "21:00"
RUN_TIME_LABEL = "9:00 PM IST"


def default_schedule() -> dict:
    return {"enabled": False, "time": RUN_TIME, "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
            "formats": ["Excel", "PDF", "Image"], "post_summary": True}


def normalize_schedule(raw: dict | None) -> dict:
    """A stored schedule merged onto the defaults, with the run time pinned.

    Rows saved before the CronJob still carry their old per-project time, so
    pin it here rather than trusting what was written.
    """
    sched = default_schedule()
    sched.update({k: v for k, v in (raw or {}).items() if k in sched})
    sched["time"] = RUN_TIME
    return sched


def schedule_for(project: str) -> dict:
    return normalize_schedule(load_store(SCHEDULE_STORE).get(project))


def save_schedule(project: str, sched: dict) -> None:
    save_to_store(SCHEDULE_STORE, project, sched)


# A run claims its slot by recording status="running" before the work starts,
# and only a finished run overwrites that row. So a process killed in between -
# an evicted pod, a crashed container - leaves "running" behind with nothing
# ever to replace it, and the page reports a run still in progress hours later.
# Past this many minutes, treat "running" as a run that died.
RUN_STALE_MINUTES = 30


def last_run(project: str) -> dict:
    return load_store(RUNS_STORE).get(project) or {}


def run_interrupted(record: dict, now: datetime | None = None) -> bool:
    """True when a record claims to be running but plainly is not any more."""
    if (record or {}).get("status") != "running":
        return False
    started = str(record.get("at") or "").strip()
    if not started:
        return False
    try:
        began = datetime.strptime(started, "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    now = now or datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    return (now - began).total_seconds() > RUN_STALE_MINUTES * 60


def record_run(project: str, result: dict) -> None:
    save_to_store(RUNS_STORE, project, result)


def schedule_blockers(project: str) -> list:
    """What's still missing before this project can run unattended."""
    missing = []
    if not (load_store("jira_config").get(project) or {}).get("value"):
        missing.append("a saved Jira query")
    if not load_store("zoho_channels").get(project):
        missing.append("a Cliq channel")
    if not load_store("sprint_details").get(project):
        missing.append("saved sprint details")
    return missing


def jql_targets_fixed_sprint(jql: str) -> bool:
    """True when the query names a specific sprint instead of the open one, so a
    scheduled run would keep reporting that same sprint forever."""
    text = str(jql or "")
    if not text.strip():
        return False
    if re.search(r"openSprints\s*\(", text, re.IGNORECASE):
        return False
    return bool(re.search(r"\bsprint\b", text, re.IGNORECASE))


def due_now(sched: dict, now, already_ran_today: bool) -> bool:
    """Has this project's slot passed today, on an allowed day, un-run so far?"""
    if not sched.get("enabled") or already_ran_today:
        return False
    if WEEKDAYS[now.weekday()] not in (sched.get("days") or WEEKDAYS):
        return False
    try:
        hh, mm = str(sched.get("time", "09:30")).split(":")
        slot = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except (ValueError, TypeError):
        return False
    return now >= slot
