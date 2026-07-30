"""
bulk_product_template.py
Утиліти для функції «Товар по шаблону»:
  - build_hierarchy_config_from_levels — формує hierarchy_config для update_category
  - collect_spec                        — збирає ExcelSpec зі списку рівнів і ярусів
  - write_template                      — записує .xlsx шаблон (2 аркуші)
  - import_products_from_xlsx           — зчитує заповнений файл і додає товари до БД
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from logger import logger

ALLOWED_PURCHASE_CURRENCIES = {"USD", "CNY", "EUR", "UAH", "PLN"}
PURCHASE_CURRENCY_ALIASES = {
    "$": "USD",
    "USDT": "USD",
    "RMB": "CNY",
    "YUAN": "CNY",
    "EURO": "EUR",
    "ZL": "PLN",
    "ZLOTY": "PLN",
    "GRN": "UAH",
    "HRN": "UAH",
    "ГРН": "UAH",
}


def _normalize_purchase_currency(raw: Any, default: str = "USD") -> str:
    token = str(raw or "").strip().upper().replace(" ", "")
    token = token.replace(".", "").replace("_", "")
    if token in ALLOWED_PURCHASE_CURRENCIES:
        return token
    if token in PURCHASE_CURRENCY_ALIASES:
        return PURCHASE_CURRENCY_ALIASES[token]
    fallback = str(default or "USD").strip().upper()
    return fallback if fallback in ALLOWED_PURCHASE_CURRENCIES else "USD"


# ---------------------------------------------------------------------------
# Public: hierarchy config builder
# ---------------------------------------------------------------------------


def build_hierarchy_config_from_levels(
    level_rows: list[dict], use_products: bool
) -> dict:
    """
    Формує dict hierarchy_config для db.update_category.

    level_rows: [{position: int, title: str, enabled: bool}, ...]
    """
    levels = []
    for r in sorted(level_rows, key=lambda x: int(x.get("position") or 0)):
        pos = int(r.get("position") or 0)
        if pos < 1 or pos > 6:
            continue
        levels.append(
            {
                "position": pos,
                "title": str(r.get("title") or "").strip() or f"Рівень {pos}",
                "enabled": bool(r.get("enabled")),
            }
        )
    return {"levels": levels, "use_products": bool(use_products)}


# ---------------------------------------------------------------------------
# ExcelSpec dataclass
# ---------------------------------------------------------------------------


@dataclass
class ExcelSpec:
    company_id: str = ""
    company_name: str = ""
    manager_id: str = ""
    manager_name: str = ""
    region_id: str = ""
    region_name: str = ""
    category_id: str = ""
    category_name: str = ""
    # enabled levels only, in position order
    level_headers: list[str] = field(default_factory=list)
    level_positions: list[int] = field(default_factory=list)
    use_products: bool = True
    # [{tier_id, name, currency}]
    price_tiers: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public: collect_spec
# ---------------------------------------------------------------------------


def collect_spec(
    company_name: str = "",
    manager_name: str = "",
    region_name: str = "",
    category_name: str = "",
    level_rows: list[dict] | tuple = (),
    use_products: bool = True,
    price_tiers: list[dict] | tuple = (),
    company_id: str = "",
    manager_id: str = "",
    region_id: str = "",
    category_id: str = "",
) -> ExcelSpec:
    """
    Збирає ExcelSpec із параметрів UI.
    Фільтрує лише увімкнені рівні.
    """
    enabled = [
        r
        for r in (level_rows or [])
        if isinstance(r, dict) and r.get("enabled") and int(r.get("position") or 0) > 0
    ]
    enabled.sort(key=lambda x: int(x.get("position") or 0))
    return ExcelSpec(
        company_id=str(company_id or "").strip(),
        company_name=str(company_name or "").strip(),
        manager_id=str(manager_id or "").strip(),
        manager_name=str(manager_name or "").strip(),
        region_id=str(region_id or "").strip(),
        region_name=str(region_name or "").strip(),
        category_id=str(category_id or "").strip(),
        category_name=str(category_name or "").strip(),
        level_headers=[
            str(r.get("title") or f"Рівень {r['position']}") for r in enabled
        ],
        level_positions=[int(r["position"]) for r in enabled],
        use_products=bool(use_products),
        price_tiers=[t for t in (price_tiers or []) if isinstance(t, dict)],
    )


# ---------------------------------------------------------------------------
# Public: write_template
# ---------------------------------------------------------------------------


def write_template(path: str, spec: ExcelSpec) -> tuple[bool, str]:
    """
    Записує Excel-файл із двома аркушами:
      «Шаблон»    — таблиця для заповнення
      «Інструкція» — опис колонок
    Повертає (ok: bool, error_msg: str).
    """
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        return False, f"openpyxl не встановлено: {exc}"

    try:
        wb = openpyxl.Workbook()

        # ── Аркуш «Шаблон» ──────────────────────────────────────────────────
        ws = wb.active
        ws.title = "Шаблон"

        # Стилі
        hdr_font = Font(bold=True, color="FFFFFF", size=11)
        hdr_fill_ctx = PatternFill("solid", fgColor="2D6A9F")  # синій — контекст
        hdr_fill_lvl = PatternFill("solid", fgColor="1A7A4A")  # зелений — рівні
        hdr_fill_name = PatternFill("solid", fgColor="7B3F9E")  # фіолетовий — назва
        hdr_fill_meta = PatternFill("solid", fgColor="0F766E")  # бірюзовий — закупівля
        hdr_fill_tier = PatternFill("solid", fgColor="C05621")  # помаранчевий — ціни
        ctx_fill = PatternFill("solid", fgColor="D6E4F0")
        thin = Border(
            left=Side(style="thin"),
            right=Side(style="thin"),
            top=Side(style="thin"),
            bottom=Side(style="thin"),
        )

        def _cell(r, c, value, font=None, fill=None, align=None):
            cell = ws.cell(row=r, column=c, value=value)
            if font:
                cell.font = font
            if fill:
                cell.fill = fill
            if align:
                cell.alignment = align
            cell.border = thin
            return cell

        center = Alignment(horizontal="center", vertical="center", wrap_text=True)
        left = Alignment(horizontal="left", vertical="center", wrap_text=False)

        # Заголовки
        col = 1
        context_cols = ["Компанія", "Менеджер", "Регіон", "Категорія"]
        context_col_indices = []
        for hdr in context_cols:
            _cell(1, col, hdr, font=hdr_font, fill=hdr_fill_ctx, align=center)
            context_col_indices.append(col)
            col += 1

        level_col_start = col
        for hdr in spec.level_headers:
            _cell(1, col, hdr, font=hdr_font, fill=hdr_fill_lvl, align=center)
            col += 1

        name_col = col
        _cell(1, col, "Назва", font=hdr_font, fill=hdr_fill_name, align=center)
        col += 1

        supplier_col = col
        _cell(1, col, "Постачальник", font=hdr_font, fill=hdr_fill_meta, align=center)
        col += 1
        purchase_price_col = col
        _cell(1, col, "Ціна закупівлі", font=hdr_font, fill=hdr_fill_meta, align=center)
        col += 1
        purchase_currency_col = col
        _cell(
            1, col, "Валюта закупівлі", font=hdr_font, fill=hdr_fill_meta, align=center
        )
        col += 1
        weight_col = col
        _cell(1, col, "Вага", font=hdr_font, fill=hdr_fill_meta, align=center)
        col += 1
        production_term_col = col
        _cell(
            1,
            col,
            "Термін виробництва",
            font=hdr_font,
            fill=hdr_fill_meta,
            align=center,
        )
        col += 1

        tier_col_start = col
        for tier in spec.price_tiers:
            t_name = str(tier.get("name") or "")
            t_cur = str(tier.get("currency") or "")
            hdr_txt = f"{t_name} ({t_cur})" if t_name else (t_cur or "Ціна")
            _cell(1, col, hdr_txt, font=hdr_font, fill=hdr_fill_tier, align=center)
            col += 1

        total_cols = col - 1

        # Рядок контексту (рядок 2 — попередньо заповнений)
        ctx_label_font = Font(italic=True, size=10, color="1F497D")
        ctx_values = [
            spec.company_id or spec.company_name,
            spec.manager_id or spec.manager_name,
            spec.region_id or spec.region_name,
            spec.category_id or spec.category_name,
        ]
        for i, val in enumerate(ctx_values):
            _cell(2, i + 1, val or "", font=ctx_label_font, fill=ctx_fill, align=left)
        for c in range(5, total_cols + 1):
            _cell(2, c, "", fill=ctx_fill)

        # Порожні рядки для заповнення (10 рядків)
        data_font = Font(size=11)
        for row_n in range(3, 53):
            for c in range(1, total_cols + 1):
                _cell(row_n, c, "", font=data_font)

        # Ширина колонок
        for c in range(1, 5):
            ws.column_dimensions[get_column_letter(c)].width = 22
        for c in range(5, level_col_start + len(spec.level_headers)):
            ws.column_dimensions[get_column_letter(c)].width = 20
        if name_col:
            ws.column_dimensions[get_column_letter(name_col)].width = 28
        ws.column_dimensions[get_column_letter(supplier_col)].width = 24
        ws.column_dimensions[get_column_letter(purchase_price_col)].width = 16
        ws.column_dimensions[get_column_letter(purchase_currency_col)].width = 16
        ws.column_dimensions[get_column_letter(weight_col)].width = 12
        ws.column_dimensions[get_column_letter(production_term_col)].width = 18
        for i in range(len(spec.price_tiers)):
            ws.column_dimensions[get_column_letter(tier_col_start + i)].width = 16

        ws.row_dimensions[1].height = 32
        ws.row_dimensions[2].height = 20

        # Закріпити першу рядок
        ws.freeze_panes = "A3"

        # ── Аркуш «Інструкція» ────────────────────────────────────────────
        wi = wb.create_sheet("Інструкція")
        wi.column_dimensions["A"].width = 18
        wi.column_dimensions["B"].width = 60

        head_font = Font(bold=True, size=12)
        lbl_font = Font(bold=True, size=10)
        txt_font = Font(size=10)

        wi["A1"] = "Товар по шаблону"
        wi["A1"].font = head_font

        instructions = [
            ("", ""),
            ("Контекст (A–D)", ""),
            (
                "Компанія",
                "Назва або ID компанії. Якщо порожньо в рядку — береться зі рядка 2.",
            ),
            ("Менеджер", "Ім'я або ID менеджера (власника товару)."),
            ("Регіон", "Назва або ID регіону."),
            ("Категорія", "Назва або ID категорії."),
            ("", ""),
            ("Рівні (E+)", ""),
        ]
        for i, lv in enumerate(spec.level_headers):
            instructions.append((lv, f"Рівень {spec.level_positions[i]} ієрархії."))
        instructions.append(
            (
                "Назва",
                "Якщо заповнено — рядок імпортується як товар з деталізацією. Якщо порожньо — Товар = Серія (назва береться з останнього непорожнього рівня).",
            )
        )
        instructions += [
            (
                "Постачальник",
                "Обов'язково. Якщо такого постачальника немає, він буде створений автоматично.",
            ),
            (
                "Ціна закупівлі",
                "Обов'язково. Число, можна з крапкою або комою (напр. 12.5 або 12,5).",
            ),
            (
                "Валюта закупівлі",
                "Обов'язково. Дозволені: USD, CNY, EUR, UAH, PLN. Можна псевдоніми: $, USDT, RMB, YUAN, EURO, ZL, ZLOTY, GRN, HRN, ГРН.",
            ),
            ("Вага", "Обов'язково. Вага товару в кг (напр. 0.085)."),
            (
                "Термін виробництва",
                "Обов'язково. Кількість днів виробництва (ціле число).",
            ),
        ]
        for tier in spec.price_tiers:
            t_name = str(tier.get("name") or "")
            t_cur = str(tier.get("currency") or "")
            instructions.append(
                (
                    f"{t_name} ({t_cur})",
                    f"Ціна в {t_cur}. Залиш порожнім, якщо не потрібна.",
                )
            )
        instructions += [
            ("", ""),
            ("Примітки", ""),
            (
                "Рядок 2",
                "Попередньо заповнений контекст. Не видаляй його — він використовується при імпорті.",
            ),
            (
                "Дублікати",
                "Рядки з однаковою назвою товару у тому ж регіоні не будуть додані повторно.",
            ),
            (
                "Імпорт",
                "Під час імпорту заголовки рівнів можуть бути синхронізовані у категорію.",
            ),
        ]
        for row_i, (label, text) in enumerate(instructions, start=2):
            c_a = wi.cell(row=row_i, column=1, value=label)
            c_b = wi.cell(row=row_i, column=2, value=text)
            if label and not text:
                c_a.font = lbl_font
            else:
                c_a.font = lbl_font if label else txt_font
                c_b.font = txt_font

        wb.save(path)
        return True, ""

    except Exception as exc:
        logger.exception("write_template failed: %s", exc)
        return False, str(exc)


# ---------------------------------------------------------------------------
# Public: import_products_from_xlsx
# ---------------------------------------------------------------------------


def import_products_from_xlsx(
    db: Any,
    path: str,
    sync_category_headers: bool = True,
    row_problem: Callable[[int, str], None] | None = None,
) -> tuple[int, int, list[str], list[str]]:
    """
    Зчитує заповнений Excel-файл і додає товари до БД.

    Повертає (added, duplicates, warnings, dup_lines).
    """
    try:
        import openpyxl
    except ImportError:
        return 0, 0, ["openpyxl не встановлено"], []

    def _warn(msg: str):
        logger.warning("bulk_import: %s", msg)

    def _row_cb(rno: int, msg: str):
        _warn(f"row {rno}: {msg}")
        if callable(row_problem):
            with contextlib.suppress(Exception):
                row_problem(rno, msg)

    added = 0
    dup_count = 0
    warns: list[str] = []
    dup_lines: list[str] = []

    # ------------------------------------------------------------------
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:
        return 0, 0, [f"Не вдалося відкрити файл: {exc}"], []

    ws = wb.active
    # Prefer "Шаблон" sheet if exists
    if "Шаблон" in wb.sheetnames:
        ws = wb["Шаблон"]

    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return 0, 0, ["Файл порожній або не містить рядків даних"], []

    header_row = [str(c or "").strip() for c in rows[0]]
    total_cols = len(header_row)

    # ------------------------------------------------------------------
    # Identify column indices
    # A=0, B=1, C=2, D=3 (0-based)
    COL_COMPANY = 0
    COL_MANAGER = 1
    COL_REGION = 2
    COL_CATEGORY = 3

    # Level columns: after D until service columns begin.
    # Service columns: Назва, Постачальник, Ціна закупівлі, Валюта закупівлі, Вага, Термін виробництва
    # Price tier columns: usually have pattern "Name (CCY)".
    level_col_indices: list[int] = []
    level_col_names: list[str] = []
    name_col: int | None = None
    supplier_col: int | None = None
    purchase_price_col: int | None = None
    purchase_currency_col: int | None = None
    weight_col: int | None = None
    production_term_col: int | None = None
    tier_col_indices: list[int] = []
    tier_col_headers: list[str] = []

    service_headers = {
        "назва": "name",
        "постачальник": "supplier",
        "ціназакупівлі": "purchase_price",
        "валютазакупівлі": "purchase_currency",
        "вага": "weight",
        "термінвиробництва": "production_term",
    }

    for ci, hdr in enumerate(header_row):
        if ci < 4:
            continue
        normalized_hdr = "".join(
            ch for ch in str(hdr or "").strip().lower() if not ch.isspace()
        )
        hdr_type = service_headers.get(normalized_hdr)
        if hdr_type == "name":
            name_col = ci
            continue
        if hdr_type == "supplier":
            supplier_col = ci
            continue
        if hdr_type == "purchase_price":
            purchase_price_col = ci
            continue
        if hdr_type == "purchase_currency":
            purchase_currency_col = ci
            continue
        if hdr_type == "weight":
            weight_col = ci
            continue
        if hdr_type == "production_term":
            production_term_col = ci
            continue

        if " (" in hdr or hdr.lower().startswith("ціна"):
            tier_col_indices.append(ci)
            tier_col_headers.append(hdr)
        elif hdr:
            # Level columns are expected before service columns.
            if (
                name_col is None
                and supplier_col is None
                and purchase_price_col is None
                and purchase_currency_col is None
                and weight_col is None
                and production_term_col is None
            ):
                level_col_indices.append(ci)
                level_col_names.append(hdr)

    required_missing = []
    if supplier_col is None:
        required_missing.append("Постачальник")
    if purchase_price_col is None:
        required_missing.append("Ціна закупівлі")
    if purchase_currency_col is None:
        required_missing.append("Валюта закупівлі")
    if weight_col is None:
        required_missing.append("Вага")
    if production_term_col is None:
        required_missing.append("Термін виробництва")
    if required_missing:
        return (
            0,
            0,
            [f"У шаблоні відсутні обов'язкові колонки: {', '.join(required_missing)}"],
            [],
        )

    if not level_col_indices:
        return 0, 0, ["Не знайдено жодної колонки рівнів (E і далі)"], []

    # ------------------------------------------------------------------
    # Helper: resolve context (company, manager, region, category) from DB

    _company_cache: dict[str, dict | None] = {}
    _manager_cache: dict[str, dict | None] = {}
    _region_cache: dict[str, dict | None] = {}
    _category_cache: dict[str, dict | None] = {}
    _supplier_cache: dict[str, dict | None] = {}

    def _to_float_or_none(raw: Any) -> float | None:
        try:
            txt = str(raw or "").strip().replace(",", ".")
            if not txt:
                return None
            return float(txt)
        except Exception:
            return None

    def _to_int_or_none(raw: Any) -> int | None:
        try:
            txt = str(raw or "").strip().replace(",", ".")
            if not txt:
                return None
            return int(float(txt))
        except Exception:
            return None

    def _find_company(key: str) -> dict | None:
        if not key:
            return None
        if key in _company_cache:
            return _company_cache[key]
        result = None
        try:
            for c in (
                db.get_all_companies() if hasattr(db, "get_all_companies") else []
            ):
                if not isinstance(c, dict):
                    continue
                if (
                    str(c.get("id") or "").strip() == key
                    or str(c.get("name") or "").strip().lower() == key.lower()
                ):
                    result = c
                    break
        except Exception:
            pass
        _company_cache[key] = result
        return result

    def _find_manager(company_id: str, key: str) -> dict | None:
        cache_key = f"{company_id}|{key}"
        if cache_key in _manager_cache:
            return _manager_cache[cache_key]
        result = None
        try:
            for m in db.get_company_managers(company_id) or []:
                if not isinstance(m, dict):
                    continue
                if (
                    str(m.get("id") or "").strip() == key
                    or str(m.get("full_name") or "").strip().lower() == key.lower()
                ):
                    result = m
                    break
        except Exception:
            pass
        _manager_cache[cache_key] = result
        return result

    def _find_region(company_id: str, key: str) -> dict | None:
        cache_key = f"{company_id}|{key}"
        if cache_key in _region_cache:
            return _region_cache[cache_key]
        result = None
        try:
            for r in db.get_company_regions(company_id) or []:
                if not isinstance(r, dict):
                    continue
                if (
                    str(r.get("id") or "").strip() == key
                    or str(r.get("name") or "").strip().lower() == key.lower()
                ):
                    result = r
                    break
        except Exception:
            pass
        _region_cache[cache_key] = result
        return result

    def _find_category(company_id: str, region_id: str, key: str) -> dict | None:
        cache_key = f"{company_id}|{region_id}|{key}"
        if cache_key in _category_cache:
            return _category_cache[cache_key]
        result = None
        try:
            cats = []
            if hasattr(db, "get_company_categories"):
                cats = db.get_company_categories(company_id, region_id) or []
            for c in cats:
                if not isinstance(c, dict):
                    continue
                if (
                    str(c.get("id") or "").strip() == key
                    or str(c.get("name") or "").strip().lower() == key.lower()
                ):
                    result = c
                    break
        except Exception:
            pass
        _category_cache[cache_key] = result
        return result

    def _find_or_create_supplier(
        company_id: str,
        region_id: str,
        manager_id: str,
        supplier_name: str,
        purchase_currency: str,
    ) -> tuple[str | None, str | None]:
        s_name = str(supplier_name or "").strip()
        if not s_name:
            return None, "Не вказано постачальника"
        cache_key = f"{company_id}|{region_id}|{manager_id}|{s_name.lower()}"
        cached = _supplier_cache.get(cache_key)
        if isinstance(cached, dict) and cached.get("id"):
            return str(cached.get("id") or ""), None

        suppliers = (
            db.get_suppliers(company_id, region_id=region_id, manager_id=manager_id)
            or []
        )
        for s in suppliers:
            if not isinstance(s, dict):
                continue
            if str(s.get("name") or "").strip().lower() == s_name.lower():
                _supplier_cache[cache_key] = s
                return str(s.get("id") or ""), None

        payload = {
            "company_id": company_id,
            "region_id": region_id,
            "manager_id": manager_id,
            "name": s_name,
            "currency": _normalize_purchase_currency(purchase_currency, "USD"),
        }
        sid = str(db.add_supplier(payload) or "").strip()
        if not sid:
            return None, f"Не вдалося створити постачальника '{s_name}'"
        _supplier_cache[cache_key] = {"id": sid, "name": s_name}
        return sid, None

    _category_levels_cfg_cache: dict[str, dict] = {}
    _node_owner_granted_cache: set[tuple[str, str, str]] = set()

    def _ensure_category_levels_config(cat_row: dict | None) -> dict:
        """Ensure category has enough enabled levels for template columns.

        If category has fewer enabled levels than in the template, missing ones are
        auto-created and enabled. Existing levels can also be renamed from template
        headers when sync_category_headers=True.
        """
        cat_id = str((cat_row or {}).get("id") or "").strip()
        if not cat_id:
            return {"levels": [], "use_products": True}
        cached = _category_levels_cfg_cache.get(cat_id)
        if isinstance(cached, dict):
            return cached

        cfg = db.get_category_levels_config(
            cat_row if isinstance(cat_row, dict) else {}
        )
        levels_raw = cfg.get("levels") if isinstance(cfg.get("levels"), list) else []
        levels: list[dict] = []
        for row in levels_raw:
            if not isinstance(row, dict):
                continue
            try:
                pos = int(row.get("position") or 0)
            except Exception:
                continue
            if pos < 1 or pos > 6:
                continue
            levels.append(
                {
                    "position": pos,
                    "title": str(row.get("title") or f"Рівень {pos}").strip()
                    or f"Рівень {pos}",
                    "enabled": bool(row.get("enabled")),
                }
            )

        by_pos = {int(r.get("position") or 0): dict(r) for r in levels}
        changed = False
        max_pos = min(6, len(level_col_names))
        for pos in range(1, max_pos + 1):
            template_title = (
                str(level_col_names[pos - 1] or "").strip() or f"Рівень {pos}"
            )
            existing = by_pos.get(pos)
            if not isinstance(existing, dict):
                by_pos[pos] = {
                    "position": pos,
                    "title": template_title,
                    "enabled": True,
                }
                changed = True
                continue
            if not bool(existing.get("enabled")):
                existing["enabled"] = True
                changed = True
            if (
                sync_category_headers
                and str(existing.get("title") or "").strip() != template_title
            ):
                existing["title"] = template_title
                changed = True

        updated_levels = [
            by_pos[p]
            for p in sorted(
                k for k in by_pos.keys() if isinstance(k, int) and 1 <= k <= 6
            )
        ]
        updated_cfg = {
            "levels": updated_levels,
            "use_products": bool(cfg.get("use_products", True)),
        }
        if changed:
            with contextlib.suppress(Exception):
                db.update_category(cat_id, hierarchy_config=updated_cfg)
        _category_levels_cfg_cache[cat_id] = updated_cfg
        return updated_cfg

    # ------------------------------------------------------------------
    # Find the "context row" — first row (index 1 in rows list = excel row 2) with non-empty A
    context_row = rows[1] if len(rows) > 1 else None
    ctx_company_raw = (
        str(
            context_row[COL_COMPANY]
            if context_row and len(context_row) > COL_COMPANY
            else ""
        )
        or ""
    )
    ctx_manager_raw = (
        str(
            context_row[COL_MANAGER]
            if context_row and len(context_row) > COL_MANAGER
            else ""
        )
        or ""
    )
    ctx_region_raw = (
        str(
            context_row[COL_REGION]
            if context_row and len(context_row) > COL_REGION
            else ""
        )
        or ""
    )
    ctx_category_raw = (
        str(
            context_row[COL_CATEGORY]
            if context_row and len(context_row) > COL_CATEGORY
            else ""
        )
        or ""
    )

    # ------------------------------------------------------------------
    # sync_category_headers: update category level titles in DB
    if sync_category_headers and level_col_names:
        try:
            # Resolve context to find the category
            comp = _find_company(ctx_company_raw)
            cid = str((comp or {}).get("id") or "").strip()
            reg = _find_region(cid, ctx_region_raw) if cid else None
            rid = str((reg or {}).get("id") or "").strip()
            cat = _find_category(cid, rid, ctx_category_raw) if cid else None
            if cat and cat.get("id"):
                cfg = db.get_category_levels_config(cat)
                existing_levels = cfg.get("levels") or []
                updated = list(existing_levels)
                # Map level_col_names (ordered) to enabled level positions
                enabled_positions = [
                    int(lv.get("position") or 0)
                    for lv in sorted(
                        existing_levels, key=lambda z: int(z.get("position") or 0)
                    )
                    if lv.get("enabled")
                ]
                for idx, new_title in enumerate(level_col_names):
                    if idx >= len(enabled_positions):
                        break
                    pos = enabled_positions[idx]
                    for lv in updated:
                        if int(lv.get("position") or 0) == pos:
                            lv["title"] = new_title.strip() or lv["title"]
                            break
                hc = {"levels": updated, "use_products": cfg.get("use_products", True)}
                db.update_category(cat["id"], hierarchy_config=hc)
        except Exception as exc:
            _warn(f"sync_category_headers failed: {exc}")

    # ------------------------------------------------------------------
    # Duplicate detection cache:
    # strict duplicate scope = manager + region + category + supplier + product name
    _scope_name_cache: dict[str, set[str]] = {}
    _region_scope_loaded: set[str] = set()
    _name_contexts_cache: dict[str, list[dict[str, str]]] = {}
    _company_contexts_loaded: set[str] = set()

    def _scope_key(
        cid: str,
        rid: str,
        mid: str,
        cat_id: str,
        supplier_id: str,
    ) -> str:
        return "|".join(
            [
                str(cid or "").strip(),
                str(rid or "").strip(),
                str(mid or "").strip(),
                str(cat_id or "").strip(),
                str(supplier_id or "").strip(),
            ]
        )

    def _load_region_scope_names(cid: str, rid: str):
        region_key = f"{cid}|{rid}"
        if region_key in _region_scope_loaded:
            return
        _region_scope_loaded.add(region_key)
        try:
            prods = db.get_products(cid, region_id=rid) or []
        except Exception:
            prods = []
        for p in prods:
            if not isinstance(p, dict):
                continue
            nm = str(p.get("name") or "").strip().lower()
            if not nm:
                continue
            pmid = str(p.get("manager_id") or "").strip()
            pcat = str(p.get("category_id") or "").strip()
            psup = str(p.get("supplier_id") or "").strip()
            sk = _scope_key(cid, rid, pmid, pcat, psup)
            _scope_name_cache.setdefault(sk, set()).add(nm)

    def _load_company_name_contexts(cid: str):
        if cid in _company_contexts_loaded:
            return
        _company_contexts_loaded.add(cid)
        try:
            prods = db.get_products(cid, region_id=None) or []
        except Exception:
            prods = []
        for p in prods:
            if not isinstance(p, dict):
                continue
            nm = str(p.get("name") or "").strip().lower()
            if not nm:
                continue
            key = f"{cid}|{nm}"
            _name_contexts_cache.setdefault(key, []).append(
                {
                    "manager_id": str(p.get("manager_id") or "").strip(),
                    "region_id": str(p.get("region_id") or "").strip(),
                    "category_id": str(p.get("category_id") or "").strip(),
                    "supplier_id": str(p.get("supplier_id") or "").strip(),
                }
            )

    def _build_disambiguated_name(
        base_name: str,
        *,
        cid: str,
        rid: str,
        mid: str,
        cat_id: str,
        cat_name: str,
        supplier_id: str,
        supplier_name: str,
        manager_name: str,
        region_name: str,
        used_in_scope: set[str],
    ) -> str:
        clean_base = str(base_name or "").strip()
        if not clean_base:
            return ""

        _load_company_name_contexts(cid)
        name_key = f"{cid}|{clean_base.lower()}"
        contexts = _name_contexts_cache.get(name_key) or []
        if not contexts:
            return clean_base

        diff_manager = any(str(c.get("manager_id") or "") != mid for c in contexts)
        diff_region = any(str(c.get("region_id") or "") != rid for c in contexts)
        diff_category = any(str(c.get("category_id") or "") != cat_id for c in contexts)
        diff_supplier = any(
            str(c.get("supplier_id") or "") != supplier_id for c in contexts
        )

        suffix_parts: list[str] = []
        if diff_manager:
            suffix_parts.append(f"Мгр: {manager_name or mid}")
        if diff_region:
            suffix_parts.append(f"Регіон: {region_name or rid}")
        if diff_category:
            suffix_parts.append(f"Кат: {cat_name or cat_id}")
        if diff_supplier:
            suffix_parts.append(f"Пост: {supplier_name or supplier_id}")

        if not suffix_parts:
            return clean_base

        candidate = f"{clean_base} | {' | '.join(suffix_parts)}"
        low = candidate.lower()
        if low not in used_in_scope:
            return candidate

        i = 2
        while True:
            numbered = f"{candidate} #{i}"
            if numbered.lower() not in used_in_scope:
                return numbered
            i += 1

    # ------------------------------------------------------------------
    # Process data rows (skip header row 0, context row 1)
    # Context (A-D) is carried forward from previous non-empty rows to support
    # mixed blocks (including multiple categories) in a single file.
    current_company_raw = str(ctx_company_raw or "").strip()
    current_manager_raw = str(ctx_manager_raw or "").strip()
    current_region_raw = str(ctx_region_raw or "").strip()
    current_category_raw = str(ctx_category_raw or "").strip()

    for row_excel_idx, raw_row in enumerate(rows[2:], start=3):
        if not raw_row:
            continue

        def _cell_val(ci: int) -> str:
            if ci < len(raw_row) and raw_row[ci] is not None:
                return str(raw_row[ci]).strip()
            return ""

        # Skip completely empty rows
        all_vals = [_cell_val(ci) for ci in range(total_cols)]
        if not any(all_vals):
            continue

        # Context for this row: explicit cell value has priority;
        # otherwise use last non-empty value from previous rows.
        cell_company = _cell_val(COL_COMPANY)
        cell_manager = _cell_val(COL_MANAGER)
        cell_region = _cell_val(COL_REGION)
        cell_category = _cell_val(COL_CATEGORY)

        if cell_company:
            current_company_raw = cell_company
        if cell_manager:
            current_manager_raw = cell_manager
        if cell_region:
            current_region_raw = cell_region
        if cell_category:
            current_category_raw = cell_category

        company_raw = current_company_raw
        manager_raw = current_manager_raw
        region_raw = current_region_raw
        category_raw = current_category_raw

        if not company_raw and not manager_raw:
            _row_cb(row_excel_idx, "Порожній контекст (A–D), рядок пропущено")
            warns.append(f"Рядок {row_excel_idx}: порожній контекст")
            continue

        # Resolve IDs
        comp = _find_company(company_raw)
        if not comp:
            msg = f"Компанію '{company_raw}' не знайдено"
            _row_cb(row_excel_idx, msg)
            warns.append(f"Рядок {row_excel_idx}: {msg}")
            continue
        cid = str(comp.get("id") or "").strip()

        mgr = _find_manager(cid, manager_raw)
        if not mgr:
            msg = f"Менеджера '{manager_raw}' не знайдено в компанії"
            _row_cb(row_excel_idx, msg)
            warns.append(f"Рядок {row_excel_idx}: {msg}")
            continue
        mid = str(mgr.get("id") or "").strip()

        reg = _find_region(cid, region_raw)
        if not reg:
            msg = f"Регіон '{region_raw}' не знайдено"
            _row_cb(row_excel_idx, msg)
            warns.append(f"Рядок {row_excel_idx}: {msg}")
            continue
        rid = str(reg.get("id") or "").strip()

        cat = _find_category(cid, rid, category_raw)
        if not cat:
            msg = f"Категорію '{category_raw}' не знайдено"
            _row_cb(row_excel_idx, msg)
            warns.append(f"Рядок {row_excel_idx}: {msg}")
            continue
        cat_id = str(cat.get("id") or "").strip()

        # Level values
        level_values = [_cell_val(ci) for ci in level_col_indices]
        # Filter empty trailing levels
        last_non_empty = -1
        for li, lv in enumerate(level_values):
            if lv:
                last_non_empty = li
        if last_non_empty < 0:
            _row_cb(row_excel_idx, "Всі рівні порожні, рядок пропущено")
            continue

        # Product name
        product_name_col = _cell_val(name_col) if name_col is not None else ""
        # Determine final product name and catalog_node_id
        if product_name_col:
            final_name = product_name_col
        else:
            # use deepest non-empty level value as product name
            final_name = level_values[last_non_empty] if last_non_empty >= 0 else ""

        if not final_name:
            _row_cb(row_excel_idx, "Не вдалося визначити назву товару, рядок пропущено")
            continue

        # Build level_nodes path to get catalog_node_id
        # Ensure category levels are auto-created from template headers when missing.
        cfg = _ensure_category_levels_config(cat)
        enabled_db_levels = [
            lv
            for lv in sorted(
                (cfg.get("levels") or []),
                key=lambda z: int(z.get("position") or 0),
            )
            if lv.get("enabled")
        ]

        parent_node_id = None
        leaf_node_id: str | None = None

        base_last_pos = 0
        if enabled_db_levels:
            try:
                base_last_pos = int(enabled_db_levels[-1].get("position") or 0)
            except Exception:
                base_last_pos = 0

        path_node_ids: list[str] = []
        for lvl_idx, lvl_val in enumerate(level_values[: last_non_empty + 1]):
            if not lvl_val:
                continue
            if lvl_idx < len(enabled_db_levels):
                pos = int(enabled_db_levels[lvl_idx].get("position") or 0)
            else:
                extra_offset = lvl_idx - len(enabled_db_levels) + 1
                pos = min(6, max(1, base_last_pos + extra_offset))
            node_id = None
            try:
                node_id = db.find_or_create_level_node(
                    cat_id, pos, lvl_val, parent_node_id=parent_node_id
                )
            except Exception as exc:
                _row_cb(row_excel_idx, f"Помилка створення вузла рівня {pos}: {exc}")
                warns.append(f"Рядок {row_excel_idx}: вузол рівня {pos} — {exc}")
                break
            if not node_id:
                _row_cb(row_excel_idx, f"Не вдалося знайти/створити вузол рівня {pos}")
                break
            parent_node_id = node_id
            leaf_node_id = node_id
            path_node_ids.append(node_id)

        if not leaf_node_id:
            _row_cb(row_excel_idx, "Не вдалося побудувати ієрархію, рядок пропущено")
            warns.append(f"Рядок {row_excel_idx}: не вдалося побудувати ієрархію")
            continue

        # find_or_create_level_node() only creates/finds the level_nodes row —
        # unlike the manual "Створити Підкатегорія" UI dialog, it never grants
        # the manager ownership of the node. Without an owner row, category-
        # manager accounts (non-bypass roles) have their category tree filtered
        # to their owned scope and see nothing at all, even though the nodes
        # and products both exist — mirror the UI dialog's add_node_owner call
        # here so imported hierarchy is actually visible to the manager it
        # belongs to.
        for node_id in path_node_ids:
            owner_cache_key = (cid, mid, node_id)
            if owner_cache_key in _node_owner_granted_cache:
                continue
            _node_owner_granted_cache.add(owner_cache_key)
            with contextlib.suppress(Exception):
                db.add_node_owner(mid, cid, node_id)

        supplier_name = _cell_val(supplier_col)
        purchase_price = _to_float_or_none(_cell_val(purchase_price_col))
        purchase_currency = _normalize_purchase_currency(
            _cell_val(purchase_currency_col), "USD"
        )
        weight_kg = _to_float_or_none(_cell_val(weight_col))
        production_term_days = _to_int_or_none(_cell_val(production_term_col))

        if not supplier_name:
            _row_cb(row_excel_idx, "Не вказано постачальника")
            warns.append(f"Рядок {row_excel_idx}: не вказано постачальника")
            continue
        if purchase_price is None:
            _row_cb(row_excel_idx, "Не вказано або некоректна ціна закупівлі")
            warns.append(
                f"Рядок {row_excel_idx}: не вказано або некоректна ціна закупівлі"
            )
            continue
        if weight_kg is None:
            _row_cb(row_excel_idx, "Не вказано або некоректна вага")
            warns.append(f"Рядок {row_excel_idx}: не вказано або некоректна вага")
            continue
        if production_term_days is None:
            _row_cb(row_excel_idx, "Не вказано або некоректний термін виробництва")
            warns.append(
                f"Рядок {row_excel_idx}: не вказано або некоректний термін виробництва"
            )
            continue

        supplier_id, supplier_err = _find_or_create_supplier(
            cid,
            rid,
            mid,
            supplier_name,
            purchase_currency,
        )
        if supplier_err or not supplier_id:
            _row_cb(row_excel_idx, supplier_err or "Помилка постачальника")
            warns.append(
                f"Рядок {row_excel_idx}: {supplier_err or 'помилка постачальника'}"
            )
            continue

        # Strict duplicate check: same manager + region + category + supplier + name
        _load_region_scope_names(cid, rid)
        scope_k = _scope_key(cid, rid, mid, cat_id, supplier_id)
        used_names_in_scope = _scope_name_cache.setdefault(scope_k, set())
        if final_name.lower() in used_names_in_scope:
            dup_count += 1
            dup_lines.append(f"Рядок {row_excel_idx}: '{final_name}'")
            continue

        # If same base name exists in other scope, disambiguate display name
        manager_name_disp = str(mgr.get("full_name") or manager_raw or "").strip()
        region_name_disp = str(reg.get("name") or region_raw or "").strip()
        category_name_disp = str(cat.get("name") or category_raw or "").strip()
        final_name = _build_disambiguated_name(
            final_name,
            cid=cid,
            rid=rid,
            mid=mid,
            cat_id=cat_id,
            cat_name=category_name_disp,
            supplier_id=supplier_id,
            supplier_name=supplier_name,
            manager_name=manager_name_disp,
            region_name=region_name_disp,
            used_in_scope=used_names_in_scope,
        )

        # Price tiers
        sales_rows: list[dict] = []
        for ti, ci in enumerate(tier_col_indices):
            if ti >= len(tier_col_headers):
                break
            raw_price = _cell_val(ci)
            if not raw_price:
                continue
            price_val = None
            try:
                price_val = float(str(raw_price).replace(",", "."))
            except Exception:
                continue
            if price_val is None or price_val < 0:
                continue
            # Parse tier name/currency from header
            tier_hdr = tier_col_headers[ti]
            t_name = tier_hdr
            t_cur = "USD"
            if " (" in tier_hdr and tier_hdr.endswith(")"):
                t_name = tier_hdr[: tier_hdr.rfind(" (")].strip()
                t_cur = tier_hdr[tier_hdr.rfind("(") + 1 : -1].strip()
            sales_rows.append({"name": t_name, "currency": t_cur, "price": price_val})

        # Build payload
        use_products_flag = name_col is not None and bool(product_name_col)
        payload = {
            "company_id": cid,
            "region_id": rid,
            "manager_id": mid,
            "category_id": cat_id,
            "catalog_node_id": leaf_node_id,
            "name": final_name,
            "is_series_mode": not use_products_flag,
            "supplier_id": supplier_id,
            "purchase_price": purchase_price,
            "purchase_currency": purchase_currency,
            "weight_kg": weight_kg,
            "production_term_days": production_term_days,
            "sales_prices_json": json.dumps(sales_rows, ensure_ascii=False),
        }

        try:
            ok = db.add_product(payload)
        except Exception as exc:
            _row_cb(row_excel_idx, f"db.add_product failed: {exc}")
            warns.append(f"Рядок {row_excel_idx}: помилка збереження — {exc}")
            continue

        if ok:
            added += 1
            # Add to strict-scope set to prevent intra-import duplicates.
            used_names_in_scope.add(final_name.lower())
            # Refresh same-name contexts cache so next rows in this import can disambiguate.
            _name_contexts_cache.setdefault(f"{cid}|{final_name.lower()}", []).append(
                {
                    "manager_id": mid,
                    "region_id": rid,
                    "category_id": cat_id,
                    "supplier_id": supplier_id,
                }
            )
        else:
            dup_count += 1
            dup_lines.append(f"Рядок {row_excel_idx}: '{final_name}' (не збережено)")

    return added, dup_count, warns, dup_lines
