"""
image_generator.py
Builds a PNG image of the sprint summary block for sharing in chat.

Rows are laid out generically from lists of (label, value, color) tuples, so
a project's configured buckets/KPIs (any count, any labels/colors) render
correctly - nothing here is tied to a fixed set of statuses.
"""

import io
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WHITE = "#FFFFFF"
BLACK = "#000000"
SCALE = 2
CELL_W = 150
MAX_PER_ROW = 10

_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"


def _font(size: int, bold: bool = False):
    candidates = [
        str(_FONT_DIR / ("Arimo-Bold.ttf" if bold else "Arimo-Regular.ttf")),
        str(_FONT_DIR / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")),
        "arialbd.ttf" if bold else "arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _draw_cell(draw, xy, text, fill, fg=BLACK, font=None, align="center", border="#C9CED6"):
    x, y, w, h = xy
    draw.rectangle([x, y, x + w, y + h], fill=fill, outline=border, width=1)
    font = font or _font(18)
    text = "" if text is None else str(text)
    avail = max(w - 8, 4)

    base_size = getattr(font, "size", 14)
    min_size = max(int(base_size * 0.6), 9)
    if text and hasattr(font, "font_variant"):
        size = base_size
        while size > min_size and draw.textlength(text, font=font) > avail:
            size -= 1
            font = font.font_variant(size=size)

    if text and draw.textlength(text, font=font) > avail:
        fs = getattr(font, "size", base_size)
        max_chars = max(int(avail / max(fs * 0.55, 1)), 4)
        lines = textwrap.wrap(text, width=max_chars) or [text]
    else:
        lines = [text] if text else [""]

    line_h = getattr(font, "size", 14) + 3
    total_h = len(lines) * line_h
    ty = y + max((h - total_h) / 2, 3)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        tx = x + 5
        if align == "center":
            tx = x + max((w - tw) / 2, 5)
        draw.text((tx, ty), line, fill=fg, font=font)
        ty += line_h


def _row(draw, y, widths, values, fills, height, font, text_colors=None, aligns=None):
    x = 0
    text_colors = text_colors or [BLACK] * len(values)
    aligns = aligns or ["center"] * len(values)
    for width, value, fill, fg, align in zip(widths, values, fills, text_colors, aligns):
        _draw_cell(draw, (x, y, width, height), value, fill, fg=fg, font=font, align=align)
        x += width


def _chunk(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _draw_item_grid(draw, y, items, row_h, header_font, value_font, cell_w=CELL_W, max_per_row=MAX_PER_ROW):
    """items: list of (label, value, color). Wraps into multiple grid rows."""
    for chunk in _chunk(items, max_per_row):
        widths = [cell_w * SCALE] * len(chunk)
        labels = [it[0] for it in chunk]
        values = [it[1] for it in chunk]
        colors = [it[2] for it in chunk]
        _row(draw, y, widths, labels, colors, row_h, header_font, text_colors=[WHITE] * len(labels))
        y += row_h
        _row(draw, y, widths, values, [WHITE] * len(values), row_h, value_font)
        y += row_h + (6 * SCALE)
    return y


def build_summary_image(form_data: dict, parsed: dict) -> bytes:
    kpis = parsed["kpis"]
    buckets = parsed["buckets"]
    kpi_defs = parsed["kpi_defs"]
    bucket_counts = kpis["bucket_counts"]
    kpi_values = kpis["kpi_values"]

    daily_task = round(kpis["action_items"] / form_data["total_days"], 2) if form_data["total_days"] > 0 else 0

    # Meta fields differ by sprint type (Product has Dev/QA/Prod releases,
    # Design has a single Release Date) - app.py builds the right set.
    meta_items = form_data.get("meta_items") or []
    kpi_items = [("No of Days Left in Sprint", form_data["days_left"], "#000000"),
                 ("Action Items", kpis["action_items"], "#1F3864")]
    for k in kpi_defs:
        v = kpi_values.get(k["key"], {"pct_display": "0%"})
        kpi_items.append((k["label"], v["pct_display"], k["color"]))

    stat_items = [("Daily Task Count", daily_task, "#595959")]
    for b in buckets:
        stat_items.append((b["label"], bucket_counts.get(b["key"], 0), b["color"]))

    n_meta_rows = -(-len(meta_items) // MAX_PER_ROW)
    n_kpi_rows = -(-len(kpi_items) // MAX_PER_ROW)
    n_stat_rows = -(-len(stat_items) // MAX_PER_ROW)
    row_h = 30 * SCALE
    major_h = 38 * SCALE
    block_gap = 6 * SCALE
    height = (
        (n_meta_rows + n_kpi_rows + n_stat_rows) * (2 * row_h + block_gap)
        + row_h + major_h * 3 + 40 * SCALE
    )
    width = CELL_W * MAX_PER_ROW * SCALE

    image = Image.new("RGB", (width, int(height)), "#F2F2F2")
    draw = ImageDraw.Draw(image)

    header_font = _font(12 * SCALE, bold=True)
    value_font = _font(13 * SCALE, bold=True)
    body_font = _font(11 * SCALE)

    y = 0
    y = _draw_item_grid(draw, y, meta_items, row_h, header_font, value_font)
    y = _draw_item_grid(draw, y, kpi_items, row_h, header_font, value_font)
    y = _draw_item_grid(draw, y, stat_items, row_h, header_font, value_font)

    goal_w = [CELL_W * SCALE, CELL_W * (MAX_PER_ROW - 1) * SCALE]
    _row(draw, y, goal_w, ["Sprint Goal", "Major Sprint Items"], ["#7030A0", "#1F3864"], row_h, header_font, text_colors=[WHITE, WHITE])
    y += row_h
    major_rows = [
        [form_data.get("sprint_goal", ""), form_data.get("major_item_1", "")],
        ["", form_data.get("major_item_2", "")],
        ["", form_data.get("major_item_3", "")],
    ]
    for row_values in major_rows:
        _row(draw, y, goal_w, row_values, ["#FAE5D3", "#FFF2CC"], major_h, body_font, aligns=["left", "left"])
        y += major_h

    image = image.crop((0, 0, width, int(y) + 8))
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
