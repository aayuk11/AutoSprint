"""
columns.py
Single source of truth for the task-table columns (the Epic -> Story -> Sub-task
detail table) shared by the Excel and PDF renderers and the Settings editor.

Each column carries everything the renderers need, so neither generator relies on
a column's *position* any more:

    key      - stable identifier, also the lookup key for a row's value
    label    - default header text (the 'labels' column uses the dynamic
               "Requirement Type" / "Label" header from form_data instead)
    excel_w  - Excel column width
    pdf_w    - relative width in the PDF (renormalized over visible columns)
    wrap     - wrap text in the cell
    role     - "url" (render as a hyperlink) or "status" (fill with the bucket
               color); replaces the old hardcoded URL_COL / STATUS_COL indexes

Column order/visibility is stored per project (project config key
"task_columns") as a list of {"key": ..., "visible": bool}. When a project has
no saved order, the default depends on the sprint type: Design sprints show the
label column right after Status; Product sprints keep it last (the historical
layout, so existing reports are unchanged).
"""

from __future__ import annotations

LABELS_KEY = "labels"

# Canonical definitions. Widths/proportions match the previous hardcoded values.
TASK_COLUMNS = [
    {"key": "sno",        "label": "S.No",                                 "excel_w": 8,  "pdf_w": 0.035},
    {"key": "issue_key",  "label": "Issue Key",                            "excel_w": 14, "pdf_w": 0.055},
    {"key": "jira_link",  "label": "Jira Link / Confluence Document Link", "excel_w": 50, "pdf_w": 0.130, "role": "url"},
    {"key": "issue_type", "label": "Issue Type",                           "excel_w": 14, "pdf_w": 0.055},
    {"key": "summary",    "label": "Summary / Title",                      "excel_w": 58, "pdf_w": 0.150, "wrap": True},
    {"key": "status",     "label": "Status",                               "excel_w": 16, "pdf_w": 0.065, "role": "status"},
    {"key": "priority",   "label": "Priority",                             "excel_w": 13, "pdf_w": 0.045},
    {"key": "assignee",   "label": "Assignee",                             "excel_w": 24, "pdf_w": 0.075},
    {"key": "start_date", "label": "Start Date",                           "excel_w": 16, "pdf_w": 0.055},
    {"key": "end_date",   "label": "End Date",                             "excel_w": 16, "pdf_w": 0.055},
    {"key": "rev_start",  "label": "Revised Start Date",                   "excel_w": 18, "pdf_w": 0.060},
    {"key": "rev_end",    "label": "Revised End Date",                     "excel_w": 18, "pdf_w": 0.060},
    {"key": "comment",    "label": "Comment",                              "excel_w": 60, "pdf_w": 0.100, "wrap": True},
    {"key": LABELS_KEY,   "label": "Label",                                "excel_w": 24, "pdf_w": 0.060, "wrap": True},
]

BY_KEY = {c["key"]: c for c in TASK_COLUMNS}
ALL_KEYS = [c["key"] for c in TASK_COLUMNS]

# Historical order - label column last. Keeps existing Product reports identical.
DEFAULT_ORDER_PRODUCT = list(ALL_KEYS)

# Design sprints: the label column ("Requirement Type") sits next to Status.
DEFAULT_ORDER_DESIGN = [k for k in ALL_KEYS if k != LABELS_KEY]
DEFAULT_ORDER_DESIGN.insert(DEFAULT_ORDER_DESIGN.index("status") + 1, LABELS_KEY)

DESIGN_SPRINT = "Design Sprint"


def default_columns(sprint_type: str | None = None) -> list:
    """Default order/visibility for a sprint type (all columns visible)."""
    order = DEFAULT_ORDER_DESIGN if sprint_type == DESIGN_SPRINT else DEFAULT_ORDER_PRODUCT
    return [{"key": k, "visible": True} for k in order]


def resolve(saved, sprint_type: str | None = None) -> list:
    """Normalize a saved task_columns list into a usable one.

    Keeps the saved order, drops keys that no longer exist, and appends any
    known column missing from the saved list (so columns added in future
    versions show up automatically instead of silently disappearing).
    Falls back to the sprint-type default when nothing is saved.
    """
    if not saved:
        return default_columns(sprint_type)

    resolved, seen = [], set()
    for entry in saved:
        if isinstance(entry, str):
            key, visible = entry, True
        elif isinstance(entry, dict):
            key, visible = entry.get("key"), bool(entry.get("visible", True))
        else:
            continue
        if key in BY_KEY and key not in seen:
            resolved.append({"key": key, "visible": visible})
            seen.add(key)

    for key in ALL_KEYS:
        if key not in seen:
            resolved.append({"key": key, "visible": True})

    return resolved or default_columns(sprint_type)


def active(task_columns, label_header: str | None = None) -> list:
    """The visible columns, in order, as full definitions ready to render.

    `label_header` overrides the label column's header ("Requirement Type" for
    Design sprints, "Label" for Product sprints).
    """
    cols = []
    for entry in resolve(task_columns):
        if not entry.get("visible", True):
            continue
        col = dict(BY_KEY[entry["key"]])
        if col["key"] == LABELS_KEY and label_header:
            col["label"] = label_header
        cols.append(col)
    return cols
