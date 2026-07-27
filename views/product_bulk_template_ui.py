"""
Діалог «Товар по шаблону» для SuperAdminDashboard / AdminDashboard.
"""

from __future__ import annotations

import contextlib
import os
from tkinter import Tk, filedialog
from typing import Any

import flet as ft
import i18n

from bulk_product_template import (
    build_hierarchy_config_from_levels,
    collect_spec,
    import_products_from_xlsx,
    write_template,
)
from logger import logger

ALL_COMPANIES_KEY = "__all_companies__"


def _preferred_user_folder() -> str:
    """Звичні папки користувача (Windows локалізації для «Downloads» тощо)."""
    home = os.path.expanduser("~") or ""
    for sub in ("Downloads", "Загрузки", "Завантаження", "Documents", "Документи"):
        p = os.path.join(home, sub)
        if os.path.isdir(p):
            return p
    return home or os.getcwd()


def _pick_save_path(
    title: str, initialfile: str = "CatMan_tovar_shablon.xlsx"
) -> str | None:
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.asksaveasfilename(
        title=title,
        initialdir=_preferred_user_folder(),
        initialfile=initialfile,
        defaultextension=".xlsx",
        filetypes=[("Excel", "*.xlsx"), ("Усі файли", "*.*")],
    )
    root.destroy()
    return path or None


def _pick_open_path(title: str) -> str | None:
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title=title,
        initialdir=_preferred_user_folder(),
        filetypes=[("Excel", "*.xlsx"), ("Усі файли", "*.*")],
    )
    root.destroy()
    return path or None


def _category_levels_from_db(
    db: Any, category_row: dict | None
) -> tuple[list[dict], bool]:
    if not hasattr(db, "get_category_levels_config"):
        return [], True
    cfg = db.get_category_levels_config(
        category_row if isinstance(category_row, dict) else {}
    )
    levels = sorted(
        ([dict(x) for x in cfg.get("levels", []) if isinstance(x, dict)]),
        key=lambda z: int(z.get("position") or 0),
    )
    rows = []
    for x in levels:
        pos = int(x.get("position") or 1)
        if pos < 1 or pos > 6:
            continue
        rows.append(
            {
                "position": pos,
                "title": str(x.get("title") or f"Рівень {pos}"),
                "enabled": bool(x.get("enabled")),
            }
        )
    use_p = bool(cfg.get("use_products", True))
    return rows, use_p


def show_bulk_product_template_dialog(dashboard: Any, on_after_import=None) -> None:
    page = getattr(dashboard, "page", None)
    db = getattr(dashboard, "db", None)
    if page is None or db is None:
        return
    _t = i18n.bind(str(getattr(dashboard, "manager_id", "") or ""))

    locked_cid = str(getattr(dashboard, "locked_company_id", None) or "").strip()
    selected_company_global = getattr(dashboard, "selected_company_id", None)
    # У SuperAdmin завжди allow_all_companies=True, але locked_company_id теж задається з профілю
    # (user_data.company_id) — його не можна плутати з «областю однієї компанії» для цього діалогу.
    allow_multi_company_ui = bool(getattr(dashboard, "allow_all_companies", False))

    _field_w = 380
    company_f = ft.Dropdown(
        label="Компанія",
        border_radius=12,
        visible=allow_multi_company_ui,
        width=_field_w,
    )
    manager_f = ft.Dropdown(
        label="Користувач (власник товару)", border_radius=12, width=_field_w
    )
    region_f = ft.Dropdown(label="Регіон", border_radius=12, width=_field_w)
    category_f = ft.Dropdown(label="Категорія", border_radius=12, width=_field_w)

    use_prod_cb = ft.Checkbox(
        label=_t("bulk_template.product_separate", "Товар окремо: є колонка «Назва» (ряд нижче рівнів)"), value=False
    )
    levels_holder = ft.Column(spacing=10)
    # Прокрутка зовнішньою Column (як раніше) — scroll на самому levels_holder дає сірий блок з expand у рядках
    levels_viewport = ft.Container(
        height=280,
        padding=ft.padding.only(right=6),
        content=ft.Column([levels_holder], scroll=ft.ScrollMode.AUTO, spacing=0),
    )
    tier_preview = ft.Text("", size=11, color="grey700")

    sync_hdr_cb = ft.Checkbox(
        label="Підлаштувати назви рівнів категорії за заголовками файлу після першого рядка",
        value=True,
    )

    log_box = ft.Text("", size=11, selectable=True)

    lvl_edit_rows: list[dict[str, Any]] = []

    def _work_company_id() -> str:
        if allow_multi_company_ui:
            cid = str(company_f.value or "").strip()
            if not cid or cid == ALL_COMPANIES_KEY:
                return ""
            return cid
        if locked_cid:
            return locked_cid
        return str(company_f.value or "").strip()

    def _fill_companies():
        if not allow_multi_company_ui:
            company_f.visible = False
            return
        companies = db.get_all_companies() if hasattr(db, "get_all_companies") else []
        company_f.options = [
            ft.dropdown.Option(key=c.get("id"), text=c.get("name") or "?")
            for c in companies
            if isinstance(c, dict) and c.get("id")
        ]
        valid_keys = {getattr(opt, "key", None) for opt in company_f.options or []}
        pick = selected_company_global
        if pick in {None, "", ALL_COMPANIES_KEY}:
            pick = None
            for opt in company_f.options or []:
                k = getattr(opt, "key", None)
                if k not in {None, "", ALL_COMPANIES_KEY}:
                    pick = k
                    break
            if pick is None and company_f.options:
                pick = company_f.options[0].key
        elif pick not in valid_keys:
            pick = next(
                (
                    getattr(opt, "key", None)
                    for opt in company_f.options or []
                    if getattr(opt, "key", None) not in {None, "", ALL_COMPANIES_KEY}
                ),
                None,
            )
        company_f.value = pick

    def _fill_managers():
        cid = _work_company_id()
        mgrs = []
        if cid:
            mgrs = [
                m
                for m in (db.get_company_managers(cid) or [])
                if isinstance(m, dict) and str(m.get("id") or "").strip()
            ]
        manager_f.options = []
        seen: set[str] = set()
        for m in sorted(
            mgrs,
            key=lambda z: (
                (z.get("full_name") or "").lower(),
                str(z.get("role") or ""),
            ),
        ):
            mid = str(m.get("id") or "").strip()
            if mid in seen:
                continue
            seen.add(mid)
            role = str(m.get("role") or "").strip()
            fname = str(m.get("full_name") or "").strip() or mid[:10]
            txt = f"{fname} ({role})" if role else fname
            manager_f.options.append(ft.dropdown.Option(key=mid, text=txt))
        prev = getattr(manager_f, "value", None)
        keys = [getattr(opt, "key", None) for opt in manager_f.options]
        manager_f.value = (
            prev
            if prev in keys
            else (manager_f.options[0].key if manager_f.options else None)
        )

    def _allowed_region_ids_for_manager(mid: str) -> set[str] | None:
        """Якщо для менеджера задано обмеження регіонів — повертаємо set id; якщо список порожній у БД — None (усі регіони компанії)."""
        if not mid or not hasattr(db, "get_manager_allowed_region_ids"):
            return None
        raw = db.get_manager_allowed_region_ids(mid) or []
        allowed = {str(x).strip() for x in raw if str(x).strip()}
        # Порожній список = не налаштовували звуження → показуємо всі регіони компанії (як у формі товару).
        if not allowed:
            return None
        return allowed

    def _fill_regions():
        cid = _work_company_id()
        mid = str(manager_f.value or "").strip()
        prev = getattr(region_f, "value", None)
        regions = [
            r
            for r in (db.get_company_regions(cid) if cid else [])
            if isinstance(r, dict) and r.get("id")
        ]
        allowed = _allowed_region_ids_for_manager(mid)
        if allowed is not None:
            regions = [r for r in regions if str(r.get("id") or "") in allowed]
        regions.sort(
            key=lambda z: (((z.get("name") or "") + str(z.get("id") or "")).lower())
        )
        region_f.options = [
            ft.dropdown.Option(key=r.get("id"), text=r.get("name") or "?")
            for r in regions
        ]
        valid = {getattr(opt, "key", None) for opt in region_f.options}
        region_f.value = (
            prev
            if prev in valid
            else (region_f.options[0].key if region_f.options else None)
        )

    def _fill_categories():
        cid = _work_company_id()
        rid = str(region_f.value or "").strip()
        mid = str(manager_f.value or "").strip()
        prev = getattr(category_f, "value", None)
        cats: list = []
        if cid and rid and mid and hasattr(db, "get_accessible_categories"):
            cats = [
                c
                for c in (db.get_accessible_categories(cid, mid, rid) or [])
                if isinstance(c, dict) and c.get("id")
            ]
        elif cid and rid:
            cats = [
                c
                for c in (db.get_company_categories(cid, rid) or [])
                if isinstance(c, dict) and c.get("id")
            ]
        cats.sort(key=lambda z: ((z.get("name") or "") + str(z.get("id"))).lower())
        category_f.options = [
            ft.dropdown.Option(key=c.get("id"), text=str(c.get("name") or c.get("id")))
            for c in cats
        ]
        valid_cat = {getattr(opt, "key", None) for opt in category_f.options}
        category_f.value = (
            prev
            if prev in valid_cat
            else (category_f.options[0].key if category_f.options else None)
        )

    def _reload_tier_preview():
        cid = _work_company_id()
        mid = manager_f.value or ""
        rid = region_f.value or ""
        if not cid or not mid or not rid:
            tier_preview.value = ""
            return
        tiers = db.get_effective_region_price_tiers_for_manager(cid, mid, rid) or []
        names = []
        for t in tiers:
            if isinstance(t, dict):
                nm = str(t.get("name") or "")
                cu = str(t.get("currency") or "")
                names.append(f"{nm} ({cu})" if nm else "?")
        tier_preview.value = (
            "Цінові яруси: " + "; ".join(names)
            if names
            else "Яруси не налаштовані — будуть підставлені з регіону після першого запису або порожній список."
        )

    def _render_level_fields(
        cat_row: dict | None, *, sync_use_products_from_category: bool = True
    ):
        levels_holder.controls.clear()
        lvl_edit_rows.clear()
        lvl_cfg, use_p = _category_levels_from_db(db, cat_row)
        # Якщо оновити з категорії під час зміни цього ж чекбокса — on_change підставить старе з БД і гальмує перемикач.
        if sync_use_products_from_category:
            use_prod_cb.value = use_p
        for row in sorted(lvl_cfg, key=lambda r: int(r.get("position") or 0)):
            ti = ft.TextField(
                label=f"Рівень {row.get('position')}",
                value=row.get("title") or "",
                border_radius=10,
                width=336,
                height=52,
                text_size=14,
                dense=False,
                content_padding=ft.padding.symmetric(horizontal=12, vertical=14),
            )
            en = ft.Checkbox(label="увімкн.", value=bool(row.get("enabled")))
            lvl_row = ft.Row([ti, en], wrap=True, spacing=12, run_spacing=8)
            lvl_edit_rows.append(
                {
                    "position": int(row["position"]),
                    "title": ti,
                    "enabled": en,
                    "wrap": lvl_row,
                }
            )
            levels_holder.controls.append(lvl_row)
        with contextlib.suppress(Exception):
            levels_holder.update()

    def _current_category_row() -> dict | None:
        cid = _work_company_id()
        rid = str(region_f.value or "").strip()
        mid = str(manager_f.value or "").strip()
        cat_id = category_f.value
        if not cat_id or not cid:
            return None
        if rid and mid and hasattr(db, "get_accessible_categories"):
            cats = db.get_accessible_categories(cid, mid, rid) or []
        else:
            cats = db.get_company_categories(cid, rid) if rid else []
        return next(
            (c for c in cats if isinstance(c, dict) and c.get("id") == cat_id), None
        )

    def _gather_level_payload() -> list[dict]:
        out = []
        for r in sorted(lvl_edit_rows, key=lambda z: z["position"]):
            out.append(
                {
                    "position": r["position"],
                    "title": (r["title"].value or "").strip()
                    or f"Рівень {r['position']}",
                    "enabled": bool(r["enabled"].value),
                }
            )
        return out

    def _apply_levels_to_category(_):
        cat_id = category_f.value
        if not cat_id:
            dashboard._show_snack("Оберіть категорію", "orange")
            return
        hc = build_hierarchy_config_from_levels(
            _gather_level_payload(), use_prod_cb.value
        )
        if db.update_category(cat_id, hierarchy_config=hc):
            dashboard._show_snack("Рівні категорії оновлено", "green")
            _reload_tier_preview()
            _render_level_fields(_current_category_row())

    def _on_company(_):
        # Компанія → лише її користувачі; далі все залежить від обраного користувача/регіону.
        _fill_managers()
        _fill_regions()
        _fill_categories()
        _render_level_fields(_current_category_row())
        _reload_tier_preview()
        with contextlib.suppress(Exception):
            manager_f.update()
        with contextlib.suppress(Exception):
            region_f.update()
        with contextlib.suppress(Exception):
            category_f.update()
        with contextlib.suppress(Exception):
            tier_preview.update()

    def _on_manager_selected(_):
        # Користувач → лише його регіони; категорії — лише доступні йому у вибраному регіоні.
        _fill_regions()
        _fill_categories()
        _reload_tier_preview()
        _render_level_fields(_current_category_row())
        with contextlib.suppress(Exception):
            region_f.update()
        with contextlib.suppress(Exception):
            category_f.update()
        with contextlib.suppress(Exception):
            tier_preview.update()

    def _on_region_selected(_):
        # Регіон → лише категорії, доступні цьому користувачу в цьому регіоні.
        _fill_categories()
        _reload_tier_preview()
        _render_level_fields(_current_category_row())
        with contextlib.suppress(Exception):
            category_f.update()
        with contextlib.suppress(Exception):
            tier_preview.update()

    def _on_cat(_):
        _render_level_fields(_current_category_row())
        # levels_holder already updated inside _render_level_fields

    def _gather_spec():
        cid = _work_company_id()
        rid = str(region_f.value or "")
        cat_id = str(category_f.value or "")
        mid = str(manager_f.value or "")
        if cid == ALL_COMPANIES_KEY or not cid:
            return None, "Оберіть конкретну компанію"
        if not rid or not cat_id or not mid:
            return None, "Заповніть користувача, регіон і категорію"
        comp_name = ""
        companies = db.get_all_companies() if hasattr(db, "get_all_companies") else []
        for c in companies:
            if isinstance(c, dict) and c.get("id") == cid:
                comp_name = str(c.get("name") or "")
                break
        mp = db.get_manager_profile(mid) if hasattr(db, "get_manager_profile") else {}
        mname = str((mp or {}).get("full_name") or "").strip()
        rn = ""
        for r in db.get_company_regions(cid) or []:
            if isinstance(r, dict) and r.get("id") == rid:
                rn = str(r.get("name") or "")
                break
        prow = _current_category_row()
        cn = str((prow or {}).get("name") or "")
        tiers = db.get_effective_region_price_tiers_for_manager(cid, mid, rid) or []
        spec = collect_spec(
            company_name=comp_name or cid[:8],
            manager_name=mname or mid[:8],
            region_name=rn or rid[:8],
            category_name=cn or cat_id[:8],
            level_rows=_gather_level_payload(),
            use_products=bool(use_prod_cb.value),
            price_tiers=[t for t in tiers if isinstance(t, dict)],
        )
        if not spec.level_headers:
            return None, "Увімкніть принаймні один рівень із назвою"
        return spec, None

    def _do_download(_):
        es, err = _gather_spec()
        if es is None:
            dashboard._show_snack(err or "Не вдалося сформувати шаблон", "orange")
            return
        path = _pick_save_path("Збережіть шаблон Excel")
        if not path:
            return
        ok, msg = write_template(path, es)
        if ok:
            log_box.value = f"Збережено: {path}"
            dashboard._show_snack("Шаблон збережено", "green")
        else:
            log_box.value = msg or ""
            dashboard._show_snack(msg or "Не вдалося записати файл", "red")
        with contextlib.suppress(Exception):
            log_box.update()

    def _do_import_action(_):
        path = _pick_open_path("Оберіть заповнений файл")
        if not path or not os.path.isfile(path):
            dashboard._show_snack("Файл не обрано", "orange")
            return

        def _row_pb(rno: int, msg: str):
            logger.info("bulk import row=%s msg=%s", rno, msg)

        n, dup_n, warns, dup_lines = import_products_from_xlsx(
            db,
            path=path,
            sync_category_headers=bool(sync_hdr_cb.value),
            row_problem=_row_pb,
        )
        lines = [f"Імпорт завершено. Додано: {n}. Дублів (не додано): {dup_n}."]
        if dup_lines:
            lines.append("Дублікати:")
            disp = dup_lines[:120]
            lines.extend([f"- {x}" for x in disp])
            if len(dup_lines) > 120:
                lines.append(f"… та ще {len(dup_lines) - 120}")
        if warns:
            lines.append("Попередження:")
            lines.extend([f"- {w}" for w in warns[:40]])
            if len(warns) > 40:
                lines.append("…")
        log_box.value = "\n".join(lines)
        msg = f"Готово — додано {n}, дублів {dup_n}"
        snack_col = (
            "green" if n else ("blue" if dup_n else ("orange" if warns else "blue"))
        )
        dashboard._show_snack(msg, snack_col)
        with contextlib.suppress(Exception):
            log_box.update()
        if n and callable(on_after_import):
            with contextlib.suppress(Exception):
                on_after_import()

    def _refresh_all(_=None):
        _fill_companies()
        _fill_managers()
        _fill_regions()
        _fill_categories()
        _render_level_fields(_current_category_row())
        _reload_tier_preview()
        with contextlib.suppress(Exception):
            company_f.update()
        with contextlib.suppress(Exception):
            manager_f.update()
        with contextlib.suppress(Exception):
            region_f.update()
        with contextlib.suppress(Exception):
            category_f.update()
        with contextlib.suppress(Exception):
            tier_preview.update()

    company_f.on_select = _on_company
    with contextlib.suppress(Exception):
        company_f.on_change = _on_company

    manager_f.on_select = _on_manager_selected
    with contextlib.suppress(Exception):
        manager_f.on_change = _on_manager_selected

    region_f.on_select = _on_region_selected
    with contextlib.suppress(Exception):
        region_f.on_change = _on_region_selected

    category_f.on_select = _on_cat
    with contextlib.suppress(Exception):
        category_f.on_change = _on_cat

    def _on_use_product_checkbox(_):
        pass  # Visual update not needed — levels rebuilt on next field change

    use_prod_cb.on_change = _on_use_product_checkbox

    btn_apply_levels = ft.OutlinedButton(
        "Записати рівні в категорію в БД",
        icon=ft.Icons.SAVE,
        style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=16, vertical=12)),
        on_click=_apply_levels_to_category,
    )
    btn_dl = ft.FilledButton(
        "Завантажити шаблон", icon=ft.Icons.DOWNLOAD, on_click=_do_download
    )
    btn_imp = ft.FilledButton(
        "Імпорт з Excel", icon=ft.Icons.UPLOAD_FILE, on_click=_do_import_action
    )

    intro_card = ft.Container(
        padding=ft.padding.all(14),
        border_radius=12,
        bgcolor=ft.Colors.BLUE_50,
        content=ft.Text(
            "Завантаження шаблону — із полів нижче (компанія → користувач → регіон → категорія). "
            "Імпорт із Excel використовує тільки вміст файлу: контекст кожного рядка з колонок A–D "
            "(або перший рядок, якщо вони порожні); поля вікна на імпорт не впливають. Пакування та фото = порожньо.",
            size=13,
            color=ft.Colors.BLUE_GREY_900,
        ),
    )

    scope_card = ft.Container(
        padding=ft.padding.all(14),
        border_radius=14,
        bgcolor=ft.Colors.GREY_50,
        border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
        content=ft.Column(
            [
                ft.Text(
                    "Контекст категорії",
                    size=13,
                    weight=ft.FontWeight.W_600,
                    color=ft.Colors.BLUE_GREY_800,
                ),
                ft.Container(height=6),
                company_f,
                manager_f,
                region_f,
                category_f,
            ],
            spacing=12,
        ),
    )

    levels_outer = ft.Container(
        padding=ft.padding.all(14),
        border_radius=14,
        bgcolor=ft.Colors.BLUE_GREY_50,
        border=ft.border.all(1, ft.Colors.BLUE_100),
        content=ft.Column(
            [
                ft.Text(
                    "Рівні категорії",
                    size=14,
                    weight=ft.FontWeight.W_600,
                    color=ft.Colors.BLUE_GREY_900,
                ),
                ft.Container(height=6),
                levels_viewport,
                ft.Container(
                    padding=ft.padding.only(top=8),
                    content=tier_preview,
                ),
            ],
            spacing=0,
        ),
    )

    tier_preview.size = 12

    excel_hint = ft.Container(
        padding=ft.padding.symmetric(horizontal=12, vertical=10),
        border_radius=10,
        bgcolor=ft.Colors.AMBER_50,
        content=ft.Text(
            "У файлі два аркуші: «Шаблон» — рядки товарів, «Інструкція» — як заповнювати колонки. Імпорт зчитує лише «Шаблон».",
            size=11,
            color=ft.Colors.BROWN_900,
        ),
    )

    log_outer = ft.Container(
        padding=ft.padding.all(10),
        border_radius=10,
        bgcolor=ft.Colors.GREY_100,
        border=ft.border.all(1, ft.Colors.GREY_300),
        content=log_box,
    )

    body = ft.Container(
        width=dashboard._dialog_width(800),
        height=min(
            820,
            (
                getattr(page.window, "height", 920) - 120
                if getattr(page.window, "height", None)
                else 720
            ),
        ),
        padding=14,
        border_radius=18,
        bgcolor=ft.Colors.WHITE,
        content=ft.Column(
            [
                intro_card,
                ft.Container(height=10),
                scope_card,
                ft.Container(height=10),
                ft.Row([use_prod_cb, btn_apply_levels], wrap=True, spacing=12),
                ft.Container(height=6),
                levels_outer,
                ft.Container(height=8),
                sync_hdr_cb,
                ft.Container(height=10),
                excel_hint,
                ft.Container(height=10),
                ft.Row([btn_dl, btn_imp], wrap=True, spacing=12),
                ft.Text(
                    "Лог",
                    weight=ft.FontWeight.W_600,
                    size=12,
                    color=ft.Colors.BLUE_GREY_700,
                ),
                log_outer,
            ],
            spacing=0,
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    dlg_title = ft.Column(
        [
            ft.Text(_t("nav.bulk_template", "Товар по шаблону"), size=20, weight=ft.FontWeight.W_600),
            ft.Text(
                "Excel для масового створення номенклатури та цін",
                size=12,
                color=ft.Colors.BLUE_GREY_700,
            ),
        ],
        spacing=4,
        tight=True,
    )

    dlg = ft.AlertDialog(
        title=dlg_title,
        bgcolor=ft.Colors.WHITE,
        elevation=6,
        title_padding=ft.padding.only(left=24, top=22, right=24, bottom=12),
        content_padding=ft.padding.only(left=12, right=12, bottom=12),
        modal=True,
        content=body,
        actions=[
            ft.TextButton(_t("packaging.dlg.close", "Закрити"), on_click=lambda _: _close(dlg)),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    def _close(d):
        dashboard._close_dialog(d)

    _refresh_all()
    page.show_dialog(dlg)


def show_bulk_product_template_dialog_user(dashboard: Any, on_after_import=None) -> None:
    """Спрощений діалог «Товар по шаблону» для звичайного користувача.

    Компанія та менеджер фіксовані (беруться з профілю).
    Користувач обирає регіон → категорію і налаштовує рівні.
    """
    page = getattr(dashboard, "page", None)
    db = getattr(dashboard, "db", None)
    if page is None or db is None:
        return

    def _snack(text: str, color: str = "blue"):
        fn = getattr(dashboard, "_show_snack", None)
        if callable(fn):
            fn(text, color)
            return
        try:
            page.snack_bar = ft.SnackBar(
                content=ft.Text(str(text or "")),
                bgcolor=str(color or "blue"),
                behavior=ft.SnackBarBehavior.FLOATING,
            )
            page.snack_bar.open = True
            page.update()
        except Exception:
            pass

    def _dialog_width(desired: int = 800, min_width: int = 320) -> int:
        fn = getattr(dashboard, "_dialog_width", None)
        if callable(fn):
            try:
                return int(fn(desired, min_width))
            except Exception:
                pass
        try:
            w = int(getattr(page.window, "width", 1200) or 1200)
        except Exception:
            w = 1200
        return max(int(min_width), min(int(desired), w - 60))

    def _close_dialog_safe(dlg_obj):
        fn = getattr(dashboard, "_close_dialog", None)
        if callable(fn):
            fn(dlg_obj)
            return
        try:
            page.pop_dialog()
        except Exception:
            pass

    # Фіксовані ідентифікатори поточного користувача
    company_id = str(getattr(dashboard, "company_id", None) or "").strip()
    manager_id = str(
        getattr(dashboard, "manager_id", None)
        or (getattr(dashboard, "user_data", None) or {}).get("active_manager_id")
        or (getattr(dashboard, "user_data", None) or {}).get("id")
        or ""
    ).strip()
    _t = i18n.bind(manager_id)

    _field_w = 380
    region_f = ft.Dropdown(label=_t("orders.field.region", "Регіон"), border_radius=12, width=_field_w)
    category_f = ft.Dropdown(label=_t("orders.filter.category", "Категорія"), border_radius=12, width=_field_w)

    use_prod_cb = ft.Checkbox(
        label=_t("bulk_template.product_separate", "Товар окремо: є колонка «Назва» (ряд нижче рівнів)"), value=False
    )
    levels_holder = ft.Column(spacing=10)
    levels_viewport = ft.Container(
        height=280,
        padding=ft.padding.only(right=6),
        content=ft.Column([levels_holder], scroll=ft.ScrollMode.AUTO, spacing=0),
    )
    tier_preview = ft.Text("", size=12, color="grey700")

    sync_hdr_cb = ft.Checkbox(
        label="Підлаштувати назви рівнів категорії за заголовками файлу після першого рядка",
        value=True,
    )
    log_box = ft.Text("", size=11, selectable=True)

    lvl_edit_rows: list[dict[str, Any]] = []

    def _fill_regions():
        prev = getattr(region_f, "value", None)
        regions = [
            r
            for r in (db.get_company_regions(company_id) if company_id else [])
            if isinstance(r, dict) and r.get("id")
        ]
        if manager_id and hasattr(db, "get_manager_allowed_region_ids"):
            allowed = {
                str(x).strip()
                for x in (db.get_manager_allowed_region_ids(manager_id) or [])
                if str(x).strip()
            }
            if allowed:
                regions = [r for r in regions if str(r.get("id") or "") in allowed]
        regions.sort(
            key=lambda z: ((z.get("name") or "") + str(z.get("id") or "")).lower()
        )
        region_f.options = [
            ft.dropdown.Option(key=r["id"], text=r.get("name") or "?") for r in regions
        ]
        valid = {getattr(o, "key", None) for o in region_f.options}
        region_f.value = (
            prev
            if prev in valid
            else (region_f.options[0].key if region_f.options else None)
        )

    def _fill_categories():
        rid = str(region_f.value or "").strip()
        prev = getattr(category_f, "value", None)
        cats: list = []
        if (
            company_id
            and rid
            and manager_id
            and hasattr(db, "get_accessible_categories")
        ):
            cats = [
                c
                for c in (
                    db.get_accessible_categories(company_id, manager_id, rid) or []
                )
                if isinstance(c, dict) and c.get("id")
            ]
        elif company_id and rid:
            cats = [
                c
                for c in (db.get_company_categories(company_id, rid) or [])
                if isinstance(c, dict) and c.get("id")
            ]
        cats.sort(key=lambda z: ((z.get("name") or "") + str(z.get("id"))).lower())
        category_f.options = [
            ft.dropdown.Option(key=c["id"], text=str(c.get("name") or c["id"]))
            for c in cats
        ]
        valid = {getattr(o, "key", None) for o in category_f.options}
        category_f.value = (
            prev
            if prev in valid
            else (category_f.options[0].key if category_f.options else None)
        )

    def _reload_tier_preview():
        rid = str(region_f.value or "").strip()
        if not company_id or not manager_id or not rid:
            tier_preview.value = ""
            return
        tiers = (
            db.get_effective_region_price_tiers_for_manager(company_id, manager_id, rid)
            or []
        )
        names = [
            f"{t.get('name', '')} ({t.get('currency', '')})"
            for t in tiers
            if isinstance(t, dict)
        ]
        tier_preview.value = (
            "Цінові яруси: " + "; ".join(names) if names else "Яруси не налаштовані."
        )

    def _current_category_row() -> dict | None:
        rid = str(region_f.value or "").strip()
        cat_id = category_f.value
        if not cat_id or not company_id:
            return None
        if rid and manager_id and hasattr(db, "get_accessible_categories"):
            cats = db.get_accessible_categories(company_id, manager_id, rid) or []
        else:
            cats = db.get_company_categories(company_id, rid) if rid else []
        return next(
            (c for c in cats if isinstance(c, dict) and c.get("id") == cat_id), None
        )

    def _render_level_fields(
        cat_row: dict | None, *, sync_use_products_from_category: bool = True
    ):
        levels_holder.controls.clear()
        lvl_edit_rows.clear()
        lvl_cfg, use_p = _category_levels_from_db(db, cat_row)
        if sync_use_products_from_category:
            use_prod_cb.value = use_p
        for row in sorted(lvl_cfg, key=lambda r: int(r.get("position") or 0)):
            ti = ft.TextField(
                label=f"Рівень {row.get('position')}",
                value=row.get("title") or "",
                border_radius=10,
                width=336,
                height=52,
                text_size=14,
                dense=False,
                content_padding=ft.padding.symmetric(horizontal=12, vertical=14),
            )
            en = ft.Checkbox(label="увімкн.", value=bool(row.get("enabled")))
            lvl_row = ft.Row([ti, en], wrap=True, spacing=12, run_spacing=8)
            lvl_edit_rows.append(
                {"position": int(row["position"]), "title": ti, "enabled": en}
            )
            levels_holder.controls.append(lvl_row)
        with contextlib.suppress(Exception):
            levels_holder.update()

    def _gather_level_payload() -> list[dict]:
        return [
            {
                "position": r["position"],
                "title": (r["title"].value or "").strip() or f"Рівень {r['position']}",
                "enabled": bool(r["enabled"].value),
            }
            for r in sorted(lvl_edit_rows, key=lambda z: z["position"])
        ]

    def _on_region_selected(_):
        _fill_categories()
        _reload_tier_preview()
        _render_level_fields(_current_category_row())
        with contextlib.suppress(Exception):
            category_f.update()
        with contextlib.suppress(Exception):
            tier_preview.update()

    def _on_cat(_):
        _render_level_fields(_current_category_row())
        # levels_holder already updated inside _render_level_fields

    def _apply_levels_to_category(_):
        cat_id = category_f.value
        if not cat_id:
            _snack("Оберіть категорію", "orange")
            return
        hc = build_hierarchy_config_from_levels(
            _gather_level_payload(), use_prod_cb.value
        )
        if db.update_category(cat_id, hierarchy_config=hc):
            _snack("Рівні категорії оновлено", "green")
            _render_level_fields(_current_category_row())

    def _gather_spec():
        rid = str(region_f.value or "").strip()
        cat_id = str(category_f.value or "").strip()
        if not company_id or not manager_id:
            return None, "Не вдалося визначити компанію або користувача"
        if not rid or not cat_id:
            return None, "Оберіть регіон і категорію"
        mp = (
            db.get_manager_profile(manager_id)
            if hasattr(db, "get_manager_profile")
            else {}
        )
        mname = str((mp or {}).get("full_name") or "").strip()
        rn = next(
            (
                str(r.get("name") or "")
                for r in (db.get_company_regions(company_id) or [])
                if isinstance(r, dict) and r.get("id") == rid
            ),
            rid[:8],
        )
        prow = _current_category_row()
        cn = str((prow or {}).get("name") or "")
        tiers = (
            db.get_effective_region_price_tiers_for_manager(company_id, manager_id, rid)
            or []
        )
        spec = collect_spec(
            company_name="",
            manager_name=mname or manager_id[:8],
            region_name=rn,
            category_name=cn or cat_id[:8],
            level_rows=_gather_level_payload(),
            use_products=bool(use_prod_cb.value),
            price_tiers=[t for t in tiers if isinstance(t, dict)],
        )
        if not spec.level_headers:
            return None, "Увімкніть принаймні один рівень із назвою"
        return spec, None

    def _do_download(_):
        es, err = _gather_spec()
        if es is None:
            _snack(err or "Не вдалося сформувати шаблон", "orange")
            return
        path = _pick_save_path("Збережіть шаблон Excel")
        if not path:
            return
        ok, msg = write_template(path, es)
        if ok:
            log_box.value = f"Збережено: {path}"
            _snack("Шаблон збережено", "green")
        else:
            log_box.value = msg or ""
            _snack(msg or "Не вдалося записати файл", "red")
        with contextlib.suppress(Exception):
            log_box.update()

    def _do_import_action(_):
        path = _pick_open_path("Оберіть заповнений файл")
        if not path or not os.path.isfile(path):
            _snack("Файл не обрано", "orange")
            return

        def _row_pb(rno: int, msg: str):
            logger.info("bulk import row=%s msg=%s", rno, msg)

        n, dup_n, warns, dup_lines = import_products_from_xlsx(
            db,
            path=path,
            sync_category_headers=bool(sync_hdr_cb.value),
            row_problem=_row_pb,
        )
        lines = [f"Імпорт завершено. Додано: {n}. Дублів (не додано): {dup_n}."]
        if dup_lines:
            lines.append("Дублікати:")
            lines.extend([f"- {x}" for x in dup_lines[:120]])
            if len(dup_lines) > 120:
                lines.append(f"… та ще {len(dup_lines) - 120}")
        if warns:
            lines.append("Попередження:")
            lines.extend([f"- {w}" for w in warns[:40]])
            if len(warns) > 40:
                lines.append("…")
        log_box.value = "\n".join(lines)
        snack_col = (
            "green" if n else ("blue" if dup_n else ("orange" if warns else "blue"))
        )
        _snack(f"Готово — додано {n}, дублів {dup_n}", snack_col)
        with contextlib.suppress(Exception):
            log_box.update()
        if n and callable(on_after_import):
            with contextlib.suppress(Exception):
                on_after_import()

    region_f.on_select = _on_region_selected
    with contextlib.suppress(Exception):
        region_f.on_change = _on_region_selected

    category_f.on_select = _on_cat
    with contextlib.suppress(Exception):
        category_f.on_change = _on_cat

    use_prod_cb.on_change = None  # visual update happens on next category/region change

    btn_apply_levels = ft.OutlinedButton(
        "Записати рівні в категорію",
        icon=ft.Icons.SAVE,
        style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=16, vertical=12)),
        on_click=_apply_levels_to_category,
    )
    btn_dl = ft.FilledButton(
        "Завантажити шаблон", icon=ft.Icons.DOWNLOAD, on_click=_do_download
    )
    btn_imp = ft.FilledButton(
        "Імпорт з Excel", icon=ft.Icons.UPLOAD_FILE, on_click=_do_import_action
    )

    scope_card = ft.Container(
        padding=ft.padding.all(14),
        border_radius=14,
        bgcolor=ft.Colors.GREY_50,
        border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
        content=ft.Column(
            [
                ft.Text(
                    "Контекст категорії",
                    size=13,
                    weight=ft.FontWeight.W_600,
                    color=ft.Colors.BLUE_GREY_800,
                ),
                ft.Container(height=6),
                region_f,
                category_f,
            ],
            spacing=12,
        ),
    )

    levels_outer = ft.Container(
        padding=ft.padding.all(14),
        border_radius=14,
        bgcolor=ft.Colors.BLUE_GREY_50,
        border=ft.border.all(1, ft.Colors.BLUE_100),
        content=ft.Column(
            [
                ft.Text(
                    "Рівні категорії",
                    size=14,
                    weight=ft.FontWeight.W_600,
                    color=ft.Colors.BLUE_GREY_900,
                ),
                ft.Container(height=6),
                levels_viewport,
                ft.Container(padding=ft.padding.only(top=8), content=tier_preview),
            ],
            spacing=0,
        ),
    )

    log_outer = ft.Container(
        padding=ft.padding.all(10),
        border_radius=10,
        bgcolor=ft.Colors.GREY_100,
        border=ft.border.all(1, ft.Colors.GREY_300),
        content=log_box,
    )

    body = ft.Container(
        width=_dialog_width(800),
        height=min(
            820,
            (
                getattr(page.window, "height", 920) - 120
                if getattr(page.window, "height", None)
                else 720
            ),
        ),
        padding=14,
        border_radius=18,
        bgcolor=ft.Colors.WHITE,
        content=ft.Column(
            [
                ft.Container(
                    padding=ft.padding.all(14),
                    border_radius=12,
                    bgcolor=ft.Colors.BLUE_50,
                    content=ft.Text(
                        "Оберіть регіон і категорію → налаштуйте рівні → завантажте шаблон Excel → заповніть товари → імпортуйте файл.",
                        size=13,
                        color=ft.Colors.BLUE_GREY_900,
                    ),
                ),
                ft.Container(height=10),
                scope_card,
                ft.Container(height=10),
                ft.Row([use_prod_cb, btn_apply_levels], wrap=True, spacing=12),
                ft.Container(height=6),
                levels_outer,
                ft.Container(height=8),
                sync_hdr_cb,
                ft.Container(height=10),
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=12, vertical=10),
                    border_radius=10,
                    bgcolor=ft.Colors.AMBER_50,
                    content=ft.Text(
                        "У файлі два аркуші: «Шаблон» — рядки товарів, «Інструкція» — як заповнювати колонки.",
                        size=11,
                        color=ft.Colors.BROWN_900,
                    ),
                ),
                ft.Container(height=10),
                ft.Row([btn_dl, btn_imp], wrap=True, spacing=12),
                ft.Text(
                    "Лог",
                    weight=ft.FontWeight.W_600,
                    size=12,
                    color=ft.Colors.BLUE_GREY_700,
                ),
                log_outer,
            ],
            spacing=0,
            scroll=ft.ScrollMode.AUTO,
        ),
    )

    dlg = ft.AlertDialog(
        title=ft.Column(
            [
                ft.Text(_t("nav.bulk_template", "Товар по шаблону"), size=20, weight=ft.FontWeight.W_600),
                ft.Text(
                    "Excel для масового створення номенклатури",
                    size=12,
                    color=ft.Colors.BLUE_GREY_700,
                ),
            ],
            spacing=4,
            tight=True,
        ),
        bgcolor=ft.Colors.WHITE,
        elevation=6,
        title_padding=ft.padding.only(left=24, top=22, right=24, bottom=12),
        content_padding=ft.padding.only(left=12, right=12, bottom=12),
        modal=True,
        content=body,
        actions=[ft.TextButton(_t("packaging.dlg.close", "Закрити"), on_click=lambda _: _close_dialog_safe(dlg))],
        actions_alignment=ft.MainAxisAlignment.END,
    )

    # Initial load
    _fill_regions()
    _fill_categories()
    _reload_tier_preview()
    _render_level_fields(_current_category_row())

    page.show_dialog(dlg)
