"""Excel import/export for the «Дефіцит/Закупівля» (stock-out & replenishment)
calculator — see views/calculators_view.py's "replenishment" calc type.

Same shape as bulk_product_template.py (header-name matching, not fixed
column order) so a category manager pasting data from 1С into the
downloaded template doesn't need to worry about column order, only that
the header text stays recognizable. Kept as its own module (not inlined
into calculators_view.py) because this file is imported by all three apps
(CatMan Pro, the standalone calculator, and the Pyodide web build) and
openpyxl-based parsing/writing needs to stay independent of any Flet
control construction so it works the same way regardless of which app
calls it.
"""

from __future__ import annotations

import io


def write_replenishment_template(path: str) -> tuple[bool, str]:
    """Writes a blank .xlsx with the three expected columns, styled like
    bulk_product_template.py's own template (bold white-on-color header
    row) so category managers have a ready file to paste 1С data into.
    Returns (ok, error_msg)."""
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError as exc:
        return False, f"openpyxl не встановлено: {exc}"

    try:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Шаблон"

        hdr_font = Font(bold=True, color="FFFFFF", size=11)
        hdr_fill = PatternFill("solid", fgColor="2D6A9F")
        center = Alignment(horizontal="center", vertical="center", wrap_text=True)

        headers = ["Назва товару", "Продано за період", "Поточний залишок"]
        for col, hdr in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col, value=hdr)
            cell.font = hdr_font
            cell.fill = hdr_fill
            cell.alignment = center

        ws.column_dimensions["A"].width = 40
        ws.column_dimensions["B"].width = 20
        ws.column_dimensions["C"].width = 20

        wb.save(path)
        return True, ""
    except Exception as exc:
        return False, f"Не вдалося створити шаблон: {exc}"


def replenishment_template_bytes() -> tuple[bytes | None, str]:
    """Same template as write_replenishment_template, but returned as raw
    bytes instead of written to a local path — for the web build, which
    can't write to a filesystem path a browser download expects."""
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError as exc:
        return None, f"openpyxl не встановлено: {exc}"

    try:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Шаблон"

        hdr_font = Font(bold=True, color="FFFFFF", size=11)
        hdr_fill = PatternFill("solid", fgColor="2D6A9F")
        center = Alignment(horizontal="center", vertical="center", wrap_text=True)

        headers = ["Назва товару", "Продано за період", "Поточний залишок"]
        for col, hdr in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col, value=hdr)
            cell.font = hdr_font
            cell.fill = hdr_fill
            cell.alignment = center

        ws.column_dimensions["A"].width = 40
        ws.column_dimensions["B"].width = 20
        ws.column_dimensions["C"].width = 20

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), ""
    except Exception as exc:
        return None, f"Не вдалося створити шаблон: {exc}"


# Normalized header text (stripped, lowercased, spaces removed) -> field
# name — same normalization convention as bulk_product_template.py's own
# service_headers dict, with a few synonym variants since this file gets
# pasted from different 1С report exports.
_HEADER_MAP: dict[str, str] = {
    "назватовару": "name",
    "товар": "name",
    "номенклатура": "name",
    "продано": "sold",
    "проданозаперіод": "sold",
    "кількістьпродано": "sold",
    "кількістьпродажів": "sold",
    "залишок": "stock",
    "поточнийзалишок": "stock",
    "кількістьзалишок": "stock",
    "наскладі": "stock",
}


def _normalize_header(hdr) -> str:
    return "".join(ch for ch in str(hdr or "").strip().lower() if not ch.isspace())


def parse_replenishment_xlsx(source: str | bytes) -> tuple[list[dict], list[str]]:
    """Parses an uploaded/selected .xlsx into rows of
    {"name": str, "sold": float, "stock": float}. `source` is either a
    local file path (desktop apps) or raw file bytes (web build, which
    has no real filesystem path for a picked file).

    Returns (rows, warnings) — warnings list rows that were skipped
    (missing name, non-numeric sold/stock) rather than silently dropping
    them, since a category manager needs to know if some products didn't
    make it into the calculation."""
    try:
        import openpyxl
    except ImportError as exc:
        return [], [f"openpyxl не встановлено: {exc}"]

    try:
        wb = openpyxl.load_workbook(
            io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source,
            data_only=True,
        )
    except Exception as exc:
        return [], [f"Не вдалося відкрити файл: {exc}"]

    ws = wb.active
    if "Шаблон" in wb.sheetnames:
        ws = wb["Шаблон"]

    all_rows = list(ws.iter_rows(values_only=True))
    if len(all_rows) < 2:
        return [], ["Файл порожній або не містить рядків даних"]

    header_row = all_rows[0]
    name_col = sold_col = stock_col = None
    for ci, hdr in enumerate(header_row):
        field = _HEADER_MAP.get(_normalize_header(hdr))
        if field == "name" and name_col is None:
            name_col = ci
        elif field == "sold" and sold_col is None:
            sold_col = ci
        elif field == "stock" and stock_col is None:
            stock_col = ci

    missing = []
    if name_col is None:
        missing.append("Назва товару")
    if sold_col is None:
        missing.append("Продано за період")
    if stock_col is None:
        missing.append("Поточний залишок")
    if missing:
        return [], [
            "Не знайдено колонки: " + ", ".join(missing)
            + " — використайте шаблон і не змінюйте заголовки стовпців."
        ]

    rows: list[dict] = []
    warnings: list[str] = []
    for i, r in enumerate(all_rows[1:], start=2):
        name = str(r[name_col] or "").strip() if name_col < len(r) else ""
        if not name:
            continue  # blank trailing rows are normal, not worth a warning
        try:
            sold = float(r[sold_col]) if sold_col < len(r) and r[sold_col] not in (None, "") else None
        except (TypeError, ValueError):
            sold = None
        try:
            stock = float(r[stock_col]) if stock_col < len(r) and r[stock_col] not in (None, "") else None
        except (TypeError, ValueError):
            stock = None
        if sold is None or stock is None:
            warnings.append(f"Рядок {i} ({name}): пропущено — «Продано»/«Залишок» не числові")
            continue
        rows.append({"name": name, "sold": sold, "stock": stock})

    if not rows:
        warnings.append("Жодного коректного рядка не знайдено")
    return rows, warnings
