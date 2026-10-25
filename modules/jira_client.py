"""
jira_client.py
Fetches issues from Jira Cloud and returns them as a pandas DataFrame using the
SAME column names as the Jira CSV export, so the existing parser/report pipeline
consumes them unchanged (see modules/parser.py for the expected schema).

Configured via environment (see modules/config.py):
    JIRA_BASE_URL=https://<yourco>.atlassian.net
    JIRA_EMAIL=<you>@example.com
    JIRA_API_TOKEN=...
    JIRA_TARGET_START_FIELD=customfield_XXXXX   # optional; else auto-discovered
    JIRA_TARGET_END_FIELD=customfield_YYYYY     # optional
"""

from __future__ import annotations

import pandas as pd

from . import config

# Standard Jira fields we always request. Custom Target start/end fields are
# appended once resolved.
_BASE_FIELDS = [
    "summary", "issuetype", "status", "priority",
    "assignee", "parent", "created", "updated", "comment", "labels", "duedate",
]


def _cfg():
    return config.section("jira")


def is_configured() -> bool:
    return _cfg() is not None


def _auth(cfg):
    return (cfg["email"], cfg["api_token"])


def _base(cfg) -> str:
    return cfg["base_url"].rstrip("/")


def build_jql(query: dict) -> str:
    """query = {"mode": "filter"|"jql", "value": "<filter id or JQL>"}."""
    mode = (query or {}).get("mode", "jql")
    value = str((query or {}).get("value", "")).strip()
    if not value:
        raise ValueError("No Jira saved-filter ID or JQL provided.")
    return f"filter={value}" if mode == "filter" else value


# Date fields, in the order they are tried for each issue. Teams differ: some
# fill "Target start"/"Target end", others "Start date"/"Due date". We resolve
# every candidate that exists and pick the first one actually set on an issue,
# so a mix across projects works without per-project configuration.
_START_FIELD_NAMES = ["target start", "start date"]
_END_FIELD_NAMES = ["target end", "due date"]


def discover_date_fields(cfg) -> tuple:
    """Return (start_candidates, end_candidates) as ordered lists of field ids.

    Explicit ids from secrets win; the rest are resolved by exact field name via
    /rest/api/3/field. 'duedate' is a system field and is always a last resort
    for the end date.
    """
    start_pref = cfg.get("target_start_field")
    end_pref = cfg.get("target_end_field")

    fields = []
    try:
        import requests

        resp = requests.get(f"{_base(cfg)}/rest/api/3/field", auth=_auth(cfg), timeout=30)
        resp.raise_for_status()
        fields = resp.json() or []
    except Exception:
        fields = []

    by_name = {}
    for f in fields:
        name = str(f.get("name", "")).strip().lower()
        if name and name not in by_name:
            by_name[name] = f.get("id")

    def build(preferred, names, extra=()):
        out = []
        for candidate in [preferred] + [by_name.get(n) for n in names] + list(extra):
            if candidate and candidate not in out:
                out.append(candidate)
        return out

    start = build(start_pref, _START_FIELD_NAMES)
    end = build(end_pref, _END_FIELD_NAMES, extra=("duedate",))
    return start, end


def field_labels(cfg, start_ids, end_ids) -> tuple:
    """Human-readable names for the resolved ids, for the UI diagnostic."""
    names = {"duedate": "Due date"}
    try:
        import requests

        resp = requests.get(f"{_base(cfg)}/rest/api/3/field", auth=_auth(cfg), timeout=30)
        resp.raise_for_status()
        for f in resp.json() or []:
            names[f.get("id")] = f.get("name")
    except Exception:
        pass
    fmt = lambda ids: ", ".join(names.get(i, i) for i in ids) or "none found"
    return fmt(start_ids), fmt(end_ids)


def _first_set(fields: dict, candidates) -> object:
    """First candidate field that actually has a value on this issue."""
    for fid in candidates or []:
        val = fields.get(fid)
        if val not in (None, "", [], {}):
            return val
    return None


def _adf_to_text(node) -> str:
    """Flatten an Atlassian Document Format node (comment body) to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_to_text(c) for c in node)
    if isinstance(node, dict):
        text = node.get("text", "") if node.get("type") == "text" else ""
        return text + "".join(_adf_to_text(c) for c in (node.get("content") or []))
    return ""


def _latest_comment_text(fields: dict) -> str:
    comments = ((fields.get("comment") or {}).get("comments")) or []
    if not comments:
        return ""
    body = comments[-1].get("body")
    if isinstance(body, (dict, list)):
        return _adf_to_text(body).strip()
    return str(body or "").strip()


def _issue_to_row(issue: dict, start_fields, end_fields) -> dict:
    """start_fields/end_fields are ordered candidate ids; the first one set on
    this issue wins (so Target start/end is preferred, else Start date/Due date).
    Accepts a single id for backwards compatibility."""
    if isinstance(start_fields, str) or start_fields is None:
        start_fields = [start_fields] if start_fields else []
    if isinstance(end_fields, str) or end_fields is None:
        end_fields = [end_fields] if end_fields else []
    f = issue.get("fields", {}) or {}
    assignee = f.get("assignee") or {}
    return {
        "Issue key": issue.get("key", ""),
        "Issue Type": (f.get("issuetype") or {}).get("name", ""),
        "Summary": f.get("summary", "") or "",
        "Status": (f.get("status") or {}).get("name", ""),
        "Priority": (f.get("priority") or {}).get("name", "") if f.get("priority") else "",
        "Assignee": assignee.get("displayName", "Unassigned") if assignee else "Unassigned",
        "Parent key": (f.get("parent") or {}).get("key", "") if f.get("parent") else "",
        "Custom field (Target start)": _first_set(f, start_fields),
        "Custom field (Target end)": _first_set(f, end_fields),
        "Created": f.get("created"),
        "Updated": f.get("updated"),
        "Comment": _latest_comment_text(f),
        "Labels": ", ".join(str(x).strip() for x in (f.get("labels") or []) if str(x).strip()),
    }


def fetch_issues(query: dict, cfg: dict | None = None) -> pd.DataFrame:
    """Run the saved filter / JQL and return a DataFrame in the CSV-export schema."""
    import requests

    cfg = cfg or _cfg()
    if not cfg:
        raise RuntimeError(
            "Jira is not configured. Set JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN."
        )
    jql = build_jql(query)
    start_fields, end_fields = discover_date_fields(cfg)
    fields = list(_BASE_FIELDS)
    for f in list(start_fields) + list(end_fields):
        if f and f not in fields:
            fields.append(f)

    rows = []
    next_token = None
    for _ in range(1000):  # safety cap on pages
        payload = {"jql": jql, "fields": fields, "maxResults": 100}
        if next_token:
            payload["nextPageToken"] = next_token
        resp = requests.post(
            f"{_base(cfg)}/rest/api/3/search/jql",
            json=payload, auth=_auth(cfg), timeout=60,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Jira search failed (HTTP {resp.status_code}): {(resp.text or '')[:400]}")
        body = resp.json()
        for issue in body.get("issues", []) or []:
            rows.append(_issue_to_row(issue, start_fields, end_fields))
        next_token = body.get("nextPageToken")
        if body.get("isLast") or not next_token:
            break

    # Which fields supplied the dates - surfaced in the UI so a field-name
    # mismatch can never silently fall back to wrong dates again.
    global _LAST_DATE_FIELDS
    _LAST_DATE_FIELDS = field_labels(cfg, start_fields, end_fields)

    return pd.DataFrame(rows)


_LAST_DATE_FIELDS = (None, None)


def last_date_fields() -> tuple:
    """(start_label, end_label) from the most recent fetch, for diagnostics."""
    return _LAST_DATE_FIELDS
