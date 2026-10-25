"""
excel_generator.py
Builds the complete Sprint Report Excel file:
  - Sprint Summary block at the top (sprint meta, KPI %, bucket counts, goals)
  - Full Epic -> Story/Task -> Subtask hierarchy table below

Summary-block columns and their colors come entirely from the project's
configured buckets/KPIs (see modules/parser.py / modules/store.py) - any
number of them lays out correctly, nothing is a fixed set of columns.
"""

import io
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from . import columns as task_columns_def
from . import config

# Issue links follow JIRA_BASE_URL so the report works against any Jira.
# Falls back to no link rather than a wrong one when Jira is not configured.
def _jira_base() -> str:
    cfg = config.section("jira") or {}
    base = str(cfg.get("base_url") or "").rstrip("/")
    return f"{base}/browse" if base else ""

WHITE = 'FFFFFF'
BLACK = '000000'

C_SPRINT_NO = '000000'
C_DAYS      = '595959'
C_KPI_AI    = '1F3864'
C_GOAL      = '7030A0'
C_MAJOR     = '1F3864'

EPIC_BG  = 'C39BD3'
DEFAULT_STATUS_FILL = 'FCE4D6'


def _hex(color: str) -> str:
    return color.strip("#").upper()


def _luminance(hex_color: str) -> float:
    v = _hex(hex_color)
    r, g, b = int(v[0:2], 16) / 255, int(v[2:4], 16) / 255, int(v[4:6], 16) / 255
    return 0.299 * r + 0.587 * g + 0.114 * b


def _contrast_fg(hex_color: str) -> str:
    return BLACK if _luminance(hex_color) > 0.55 else WHITE


def _hdr(bg, fg=WHITE, bold=True, sz=9):
    return dict(
        font      = Font(bold=bold, color=fg, name='Arial', size=sz),
        fill      = PatternFill('solid', start_color=_hex(bg)),
        alignment = Alignment(horizontal='center', vertical='center', wrap_text=True),
        border    = Border(
            left=Side(style='thin', color='FFFFFF'),
            right=Side(style='thin', color='FFFFFF'),
            top=Side(style='thin', color='FFFFFF'),
            bottom=Side(style='thin', color='FFFFFF'),
        )
    )


def _val(bg=WHITE, fg=BLACK, bold=False, sz=10, h='center'):
    thin = Side(style='thin', color='CCCCCC')
    return dict(
        font      = Font(bold=bold, color=fg, name='Arial', size=sz),
        fill      = PatternFill('solid', start_color=_hex(bg)),
        alignment = Alignment(horizontal=h, vertical='center', wrap_text=True),
        border    = Border(left=thin, right=thin, top=thin, bottom=thin),
    )


def _apply(cell, style: dict):
    for k, v in style.items():
        setattr(cell, k, v)


def _blank_row(ws, row, n_cols, bg='F2F2F2', height=6):
    ws.row_dimensions[row].height = height
    for col_idx in range(1, n_cols + 1):
        ws.cell(row, col_idx).fill = PatternFill('solid', start_color=bg)


def _write_item_grid(ws, start_row, items, n_cols_min=10):
    """items: list of (label, value, color). Two rows per grid (header+value),
    wraps into another header/value pair if there are more items than columns.
    Returns the row number after the last grid written."""
    row = start_row
    n_cols = min(n_cols_min, len(items)) if items else 0
    idx = 0
    while idx < len(items):
        chunk = items[idx:idx + n_cols_min] if len(items) > n_cols_min else items
        for col_idx, (label, value, color) in enumerate(chunk, 1):
            hc = ws.cell(row, col_idx)
            hc.value = label
            _apply(hc, _hdr(color))
        ws.row_dimensions[row].height = 28
        row += 1
        for col_idx, (label, value, color) in enumerate(chunk, 1):
            vc = ws.cell(row, col_idx)
            vc.value = value
            _apply(vc, _val(WHITE, BLACK, True, 11))
        ws.row_dimensions[row].height = 22
        row += 1
        idx += len(chunk)
        if idx < len(items):
            _blank_row(ws, row, n_cols)
            row += 1
    return row, n_cols


def build_excel(form_data: dict, parsed: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = f"Sprint {form_data['sprint_number']} - Report"

    kpis      = parsed['kpis']
    buckets   = parsed['buckets']
    kpi_defs  = parsed['kpi_defs']
    status_map = parsed['status_map']
    bucket_colors = {b['key']: b['color'] for b in buckets}
    bucket_counts = kpis['bucket_counts']
    kpi_values = kpis['kpi_values']
    hierarchy = parsed['hierarchy']
    fd        = form_data

    daily_task = round(kpis['action_items'] / fd['total_days'], 2) if fd['total_days'] > 0 else 0

    # Meta fields differ by sprint type (Product has Dev/QA/Prod releases,
    # Design has a single Release Date) - app.py builds the right set.
    meta_items = fd.get('meta_items') or []
    row, n_cols = _write_item_grid(ws, 1, meta_items)
    _blank_row(ws, row, n_cols)
    row += 1

    kpi_items = [('No of Days Left in Sprint', fd['days_left'], C_SPRINT_NO),
                 ('Action Items', kpis['action_items'], C_KPI_AI)]
    for k in kpi_defs:
        v = kpi_values.get(k['key'], {'pct_display': '0%'})
        kpi_items.append((k['label'], v['pct_display'], k['color']))
    row, n_cols_kpi = _write_item_grid(ws, row, kpi_items)
    _blank_row(ws, row, max(n_cols, n_cols_kpi))
    row += 1

    stat_items = [('Daily Task Count', daily_task, C_DAYS)]
    for b in buckets:
        stat_items.append((b['label'], bucket_counts.get(b['key'], 0), b['color']))
    row, n_cols_stat = _write_item_grid(ws, row, stat_items)
    _blank_row(ws, row, max(n_cols, n_cols_kpi, n_cols_stat))
    row += 1

    total_cols = max(n_cols, n_cols_kpi, n_cols_stat, 2)
    c = ws.cell(row, 1); c.value = 'Sprint Goal'; _apply(c, _hdr(C_GOAL))
    c = ws.cell(row, 2); c.value = 'Major Sprint Items'; _apply(c, _hdr(C_MAJOR))
    for col_idx in range(3, total_cols + 1):
        ws.cell(row, col_idx).fill = PatternFill('solid', start_color='F2F2F2')
    ws.row_dimensions[row].height = 24
    goal_hdr_row = row
    row += 1

    for i, (val_a, val_b) in enumerate([
        (fd['sprint_goal'], fd['major_item_1']),
        ('', fd['major_item_2']),
        ('', fd['major_item_3']),
    ]):
        ca = ws.cell(row, 1); ca.value = val_a
        _apply(ca, _val('FAE5D3' if i == 0 else 'F2F2F2', BLACK, False, 9, 'left'))
        cb = ws.cell(row, 2); cb.value = val_b
        _apply(cb, _val('FFF2CC', BLACK, False, 9, 'left'))
        for col_idx in range(3, total_cols + 1):
            ws.cell(row, col_idx).fill = PatternFill('solid', start_color='F2F2F2')
        ws.row_dimensions[row].height = 20
        row += 1

    ws.row_dimensions[row].height = 14
    ws.cell(row, 1).value = 'Yellow = Manual Input | Auto-calculated fields derived from Jira CSV'
    ws.cell(row, 1).font = Font(name='Arial', size=8, italic=True, color='595959')
    row += 1

    task_start_row = row + 1
    # Column order/visibility comes from the project config (see modules/columns.py).
    active_cols = task_columns_def.active(
        form_data.get('task_columns'), form_data.get('label_header', 'Label')
    )
    for col_idx, col in enumerate(active_cols, 1):
        c = ws.cell(task_start_row, col_idx)
        c.value = col['label']
        _apply(c, _hdr('D1BBF0', fg='4A235A'))
    ws.row_dimensions[task_start_row].height = 28

    thin_g = Side(style='thin', color='CCCCCC')
    border_epic = Border(
        left=Side(style='medium', color='C39BD3'), right=Side(style='medium', color='C39BD3'),
        top=Side(style='medium', color='C39BD3'), bottom=Side(style='medium', color='C39BD3'),
    )
    border_sub = Border(left=thin_g, right=thin_g, top=thin_g, bottom=thin_g)

    rows_with_spacers = []
    for i, item in enumerate(hierarchy):
        rows_with_spacers.append(item)
        if i + 1 < len(hierarchy) and hierarchy[i + 1]['level'] == 0:
            rows_with_spacers.append(None)

    epic_counter = 0
    story_counter = 0
    sub_counters = {}
    last_epic_num = 0
    last_story_num = 0
    last_story_key = None

    for item in rows_with_spacers:
        if item is None:
            continue
        elif item['level'] == 0:
            epic_counter += 1
            story_counter = 0
            last_story_key = None
            item['sno'] = str(epic_counter)
            last_epic_num = epic_counter
        elif item['level'] == 1:
            story_counter += 1
            last_story_key = item['issue_key']
            sub_counters[last_story_key] = 0
            item['sno'] = f"{last_epic_num}.{story_counter}"
            last_story_num = story_counter
        else:
            if last_story_key and last_story_key in sub_counters:
                sub_counters[last_story_key] += 1
                sub_num = sub_counters[last_story_key]
            else:
                sub_num = 1
            item['sno'] = f"{last_epic_num}.{last_story_num}.{sub_num}"

    current_row = task_start_row + 1
    for item in rows_with_spacers:
        if item is None:
            ws.row_dimensions[current_row].height = 8
            for col in range(1, len(active_cols) + 1):
                ws.cell(current_row, col).fill = PatternFill('solid', start_color='FFFFFF')
            current_row += 1
            continue

        level = item['level']
        ik = item['issue_key']
        jira_base = _jira_base()
        url = f"{jira_base}/{ik}" if jira_base else ""

        if level == 0:
            summary_disp = item['summary'].upper()
            bg, fg, bold, sz = EPIC_BG, '4A235A', True, 10
            row_h = 28
        elif level == 1:
            summary_disp = '    >  ' + item['summary']
            bg, fg, bold, sz = WHITE, '1F3864', True, 10
            row_h = 22
        else:
            summary_disp = '         -  ' + item['summary']
            bg, fg, bold, sz = WHITE, '1F3864', False, 10
            row_h = 18

        ws.row_dimensions[current_row].height = row_h

        values_by_key = {
            'sno':        item.get('sno', ''),
            'issue_key':  ik,
            'jira_link':  url,
            'issue_type': item['issue_type'],
            'summary':    summary_disp,
            'status':     item['status'],
            'priority':   item['priority'],
            'assignee':   item['assignee'],
            'start_date': item['target_start'],
            'end_date':   item['target_end'],
            'rev_start':  '',
            'rev_end':    '',
            'comment':    item.get('latest_comment', ''),
            'labels':     item.get('labels', ''),
        }

        bucket_key = status_map.get(str(item['status']).lower().strip())
        status_fill = _hex(bucket_colors.get(bucket_key, DEFAULT_STATUS_FILL))
        status_fg = _contrast_fg(status_fill)

        for col_idx, col in enumerate(active_cols, 1):
            val = values_by_key.get(col['key'], '')
            cell = ws.cell(current_row, col_idx)
            cell.alignment = Alignment(vertical='center', wrap_text=bool(col.get('wrap')))
            cell.border = border_epic if level == 0 else border_sub
            role = col.get('role')

            if role == 'url':
                cell.value = val
                cell.hyperlink = val
                cell.font = Font(bold=False, color='4472C4', name='Arial', size=sz, underline='single')
                cell.fill = PatternFill('solid', start_color=bg)
            elif role == 'status':
                cell.value = val
                cell.fill = PatternFill('solid', start_color=status_fill)
                cell.font = Font(bold=True, name='Arial', size=sz, color=status_fg)
            else:
                cell.value = val
                cell.font = Font(bold=bold, color=fg, name='Arial', size=sz)
                cell.fill = PatternFill('solid', start_color=bg)

        current_row += 1

    # Widths follow the configured column order, not fixed sheet letters.
    for col_idx, col in enumerate(active_cols, 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = col['excel_w']

    ws.freeze_panes = f'A{task_start_row + 1}'

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()
