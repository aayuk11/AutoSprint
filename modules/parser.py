"""
parser.py
Reads the Jira CSV export (or Jira API fetch, same schema), builds the
Epic -> Story/Task/Bug -> Subtask hierarchy, and calculates KPI values for the
sprint summary block.

Unlike the original tool this was forked from, buckets (status groupings) and
% KPI cards are NOT a fixed set - they come entirely from each project's saved
config (see modules/store.py, store "project_configs"). A project config is:

    {
      "buckets": [ {"key": "not_started", "label": "Not Started", "color": "#ED7D31"}, ... ],
      "kpis":    [ {"key": "not_started_pct", "label": "Not Started %", "color": "#ED7D31",
                    "bucket_keys": ["not_started"]}, ... ],
      "status_map": { "to do": "not_started", ... },   # jira status (lowercase) -> bucket key
      "known_statuses": ["To Do", ...],                # display-cased, persists across CSVs
    }

Every bucket count and every KPI percentage is computed generically from this
config, so a project can have any number of buckets/KPIs with any labels and
colors - nothing is hardcoded to a specific team's workflow.
"""

import pandas as pd
from datetime import date

STORY_LEVEL_TYPES = ['Story', 'Task', 'Bug', 'Improvement', 'New Feature']
REQUIRED_COLUMNS = ['Issue key', 'Issue Type', 'Summary', 'Status']

UNMAPPED_KEY = '__unmapped__'
UNMAPPED_LABEL = 'Unmapped'


def _ensure_text_column(df: pd.DataFrame, column: str, default: str = '') -> None:
    if column not in df.columns:
        df[column] = default
    df[column] = df[column].fillna(default).astype(str).str.strip()


def _ensure_datetime_column(df: pd.DataFrame, source_column: str, target_column: str | None = None) -> None:
    target = target_column or source_column
    if source_column in df.columns:
        df[target] = pd.to_datetime(df[source_column], errors='coerce')
    else:
        df[target] = pd.NaT


def _coalesce_datetime(df: pd.DataFrame, target: str, sources: list) -> None:
    """Fill `target` with the first source column that has a value per row."""
    result = pd.Series(pd.NaT, index=df.index, dtype='datetime64[ns]')
    for col in sources:
        if col not in df.columns:
            continue
        parsed = pd.to_datetime(df[col], errors='coerce')
        result = result.fillna(parsed)
    df[target] = result


def distinct_statuses(df: pd.DataFrame) -> list:
    """Distinct, non-empty Status values for non-Epic rows, display-cased."""
    if 'Status' not in df.columns:
        return []
    non_epic = df[df['Issue Type'] != 'Epic'] if 'Issue Type' in df.columns else df
    seen = {}
    for s in non_epic['Status'].dropna():
        s = str(s).strip()
        if s:
            seen.setdefault(s.lower(), s)
    return [seen[k] for k in seen]


def parse_jira_csv(df: pd.DataFrame, project_name: str, config: dict) -> dict:
    """
    Main entry point. Takes the raw Jira DataFrame and a project config
    (buckets/kpis/status_map) and returns:
      - hierarchy: ordered list of dicts for the task table
      - kpis: bucket counts + KPI percentages + structural counts
      - buckets/kpis: the resolved config echoed back (for renderers)
    """
    df = df.copy()
    missing_required = [column for column in REQUIRED_COLUMNS if column not in df.columns]
    if missing_required:
        raise ValueError(f"Missing required columns: {', '.join(missing_required)}")

    _ensure_text_column(df, 'Issue key')
    _ensure_text_column(df, 'Issue Type')
    _ensure_text_column(df, 'Summary')
    _ensure_text_column(df, 'Status')
    _ensure_text_column(df, 'Priority')
    _ensure_text_column(df, 'Assignee', 'Unassigned')
    _ensure_text_column(df, 'Parent key')
    _ensure_datetime_column(df, 'Due date')

    # Teams fill different Jira date fields, and a CSV export names them
    # differently again. Take the first column that actually has a value for a
    # row, so "Target start/end" and "Start date"/"Due date" both work.
    _coalesce_datetime(df, 'Target Start', [
        'Custom field (Target start)', 'Custom field (Start date)', 'Start date',
    ])
    _coalesce_datetime(df, 'Target End', [
        'Custom field (Target end)', 'Custom field (Due date)', 'Due date',
    ])
    _ensure_datetime_column(df, 'Created')
    _ensure_datetime_column(df, 'Updated')

    epics_df    = df[df['Issue Type'] == 'Epic']
    stories_df  = df[df['Issue Type'].isin(STORY_LEVEL_TYPES)]
    subtasks_df = df[df['Issue Type'] == 'Sub-task']

    hierarchy = []
    processed_stories = set()

    # PASS 1: epics present in the CSV
    for _, epic in epics_df.iterrows():
        ek = epic['Issue key']
        hierarchy.append(_make_row(epic, level=0))

        children = stories_df[stories_df['Parent key'] == ek]
        for _, child in children.iterrows():
            ck = child['Issue key']
            hierarchy.append(_make_row(child, level=1))
            processed_stories.add(ck)

            subs = subtasks_df[subtasks_df['Parent key'] == ck]
            for _, sub in subs.iterrows():
                hierarchy.append(_make_row(sub, level=2))

    # PASS 2: stories whose parent Epic is NOT in the CSV (external epics)
    unresolved_stories = stories_df[~stories_df['Issue key'].isin(processed_stories)]
    external_stories = unresolved_stories[unresolved_stories['Parent key'] != '']

    external_epic_groups = {}
    for _, row in external_stories.iterrows():
        pk = row['Parent key']
        external_epic_groups.setdefault(pk, []).append(row)

    for ext_epic_key, children in external_epic_groups.items():
        synthetic_epic = {
            'level': 0, 'issue_key': ext_epic_key, 'issue_type': 'Epic',
            'summary': f'[EXTERNAL EPIC] {ext_epic_key}', 'status': '',
            'priority': '', 'assignee': '', 'target_start': '-', 'target_end': '-',
            'latest_comment': '', 'labels': '',
        }
        hierarchy.append(synthetic_epic)

        for child_row in children:
            ck = child_row['Issue key']
            hierarchy.append(_make_row(child_row, level=1))
            processed_stories.add(ck)

            subs = subtasks_df[subtasks_df['Parent key'] == ck]
            for _, sub in subs.iterrows():
                hierarchy.append(_make_row(sub, level=2))

    # PASS 3: standalone items with no parent key, plus orphan sub-tasks
    standalone_stories = unresolved_stories[~unresolved_stories['Issue key'].isin(processed_stories)]
    orphan_subtasks = subtasks_df[~subtasks_df['Parent key'].isin(processed_stories)]

    if len(standalone_stories) > 0 or len(orphan_subtasks) > 0:
        hierarchy.append({
            'level': 0, 'issue_key': '-', 'issue_type': 'Group',
            'summary': 'UNLINKED / STANDALONE ITEMS', 'status': '',
            'priority': '', 'assignee': '', 'target_start': '-', 'target_end': '-',
            'latest_comment': '', 'labels': '',
        })
        for _, row in standalone_stories.iterrows():
            hierarchy.append(_make_row(row, level=1))
        for _, row in orphan_subtasks.iterrows():
            hierarchy.append(_make_row(row, level=2))

    # ---- KPI / bucket calculation (exclude Epics) ----
    non_epic = df[df['Issue Type'] != 'Epic']
    total = len(non_epic)

    buckets_cfg = list(config.get('buckets') or [])
    kpis_cfg = list(config.get('kpis') or [])
    status_map = {str(k).lower().strip(): v for k, v in (config.get('status_map') or {}).items()}

    bucket_keys = {b['key'] for b in buckets_cfg}
    counts = {b['key']: 0 for b in buckets_cfg}
    unmapped_count = 0
    for status_val in non_epic['Status']:
        key = str(status_val).lower().strip()
        bucket = status_map.get(key)
        if bucket in bucket_keys:
            counts[bucket] += 1
        else:
            unmapped_count += 1

    kpi_values = {}
    for k in kpis_cfg:
        bucket_sum = sum(counts.get(bk, 0) for bk in k.get('bucket_keys', []))
        pct = round(bucket_sum / total * 100, 2) if total else 0
        kpi_values[k['key']] = {'count': bucket_sum, 'pct': pct, 'pct_display': f"{pct}%"}

    action_items = total
    daily_task_count = None  # filled in by caller (needs total_days from form)

    kpis = {
        'action_items': action_items,
        'total': total,
        'unmapped_count': unmapped_count,
        'bucket_counts': counts,
        'kpi_values': kpi_values,
    }

    return {
        'hierarchy': hierarchy,
        'kpis': kpis,
        'buckets': buckets_cfg,
        'kpi_defs': kpis_cfg,
        'status_map': status_map,
        'raw_df': df,
    }


def _extract_latest_comment(row) -> str:
    """
    Looks through Comment, Comment.1 ... Comment.6 columns, finds the last
    non-empty one, and returns only the text after the second semicolon
    (format: "DD/Mon/YY HH:MM AM/PM ; AUTHOR_UUID ; COMMENT_TEXT").
    """
    comment_cols = ['Comment', 'Comment.1', 'Comment.2', 'Comment.3',
                    'Comment.4', 'Comment.5', 'Comment.6']
    latest = ''
    for col in comment_cols:
        val = row.get(col, '')
        if pd.notna(val) and str(val).strip():
            latest = str(val).strip()
    if not latest:
        return ''
    parts = latest.split(';', 2)
    if len(parts) == 3:
        return parts[2].strip()
    return latest


def _join_labels(row) -> str:
    """Combine Jira 'Labels' data into one comma-separated, de-duplicated string.
    Handles both the API's single joined 'Labels' column and a CSV export's
    repeated 'Labels'/'Labels.1'/... columns."""
    try:
        cols = list(row.index)
    except AttributeError:
        cols = list(row.keys())
    out = []
    for col in cols:
        name = str(col)
        if name == 'Labels' or name.startswith('Labels.'):
            val = row.get(col)
            if pd.notna(val) and str(val).strip():
                for part in str(val).split(','):
                    p = part.strip()
                    if p and p not in out:
                        out.append(p)
    return ', '.join(out)


def _make_row(row, level: int) -> dict:
    # Only real planning dates are shown. A ticket with none renders '-' rather
    # than falling back to Created/Updated, which looked like a real date but
    # was just when the issue was raised / last touched.
    ts = row.get('Target Start')
    te = row.get('Target End')
    return {
        'level':          level,
        'issue_key':      row['Issue key'],
        'issue_type':     row['Issue Type'],
        'summary':        row['Summary'],
        'status':         row['Status'],
        'priority':       row['Priority'],
        'assignee':       row['Assignee'],
        'target_start':   ts.strftime('%d %b %Y') if pd.notna(ts) else '-',
        'target_end':     te.strftime('%d %b %Y') if pd.notna(te) else '-',
        'latest_comment': _extract_latest_comment(row),
        'labels':         _join_labels(row),
    }


def calculate_daily_task_count(total_action_items: int, total_days: int) -> str:
    """Daily Task Count = Total Action Items / Total Sprint Days"""
    if total_days <= 0:
        return '0'
    return str(round(total_action_items / total_days, 2))
