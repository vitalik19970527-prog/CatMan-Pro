import asyncio
import contextlib
import json
import os
import time
from datetime import datetime
from typing import Any, Optional

import flet as ft

import fx_rates
import i18n
from logger import logger
from views.async_view_build_helper import (
    create_orders_header_loading_indicator,
    ensure_orders_cache_fresh,
)
from views.number_format import (
    format_grouped,
    format_maybe_numeric_text,
    row_text_matches,
    safe_float,
)
from views.orders_view import _wrap_text_for_width
from views import product_naming

# ---------------------------------------------------------------------------
# Theme colour shims — module defaults, shadowed by live theme at render time
# ---------------------------------------------------------------------------
_BORDER = "#CBD5E1"
_BORDER2 = "#E5E7EB"
_TEXT = "#111827"
_TEXT_2 = "#1F2937"
_MUTED = "#94A3B8"
_PANEL = "#FFFFFF"
_ERR_BG = "#FEE2E2"
_HDR = "#E2E8F0"
_CHIP_T = "#1E3A8A"
_SKY = "#0EA5E9"
_OK_BG = "#DCFCE7"
_ALT = "#F1F5F9"
_ALT_2 = "#F8FAFC"
_ERR = "#DC2626"
_SOFT_P = "#EDE9FE"
_WARN_T = "#92400E"
_OK_T = "#166534"
_WARN = "#F59E0B"
_WARN_BG = "#FEF3C7"
_CHIP_BG = "#BFDBFE"
_OK_FG = "#059669"
_ERR_2 = "#FECACA"
_WARN_2 = "#FEF08A"
_OK_2 = "#BBF7D0"


def _th_from_view(view) -> dict:
    from views.themes import get_active_theme

    return get_active_theme(str(getattr(view, "manager_id", "") or ""))


def _safe_float(v, default: Optional[float] = None) -> Optional[float]:
    return safe_float(v, default)


def _safe_flet_update(ctrl) -> None:
    import contextlib as _cl

    with _cl.suppress(Exception):
        ctrl.update()


def build_orders_counters(view, rows: list) -> ft.Row:
    _th = _th_from_view(view)
    _BORDER = _BORDER2 = _th["border"]
    _TEXT = _TEXT_2 = _th["text"]
    _MUTED = _th["muted"]
    _PANEL = _th["panel"]
    _ERR_BG = _th["err_bg"]
    _HDR = _th["hdr"]
    _CHIP_T = _th["chip_text"]
    _OK_BG = _th["ok_bg"]
    _ALT = _ALT_2 = _th["alt"]
    _ERR = _th["err"]
    _SOFT_P = _th["soft"]
    _WARN_T = _OK_T = _th["warn"]
    _WARN = _th["warn"]
    _WARN_BG = _th["warn_bg"]
    _CHIP_BG = _th["chip_bg"]
    _OK_FG = _th["ok"]
    _SKY = _th["accent"]
    _ERR_2 = _th["err_bg"]
    _WARN_2 = _th["warn_bg"]
    _OK_2 = _th["ok_bg"]
    # Count unique orders from ALL rows (before status filter) for counter display.
    all_rows = view._orders_rows or []
    seen_all: set = set()
    statuses = {
        "production": 0,
        "ready_for_shipment": 0,
        "in_transit": 0,
        "at_warehouse": 0,
        "accepted": 0,
    }
    for row in all_rows:
        oid = str(row.get("order_id") or "").strip()
        if oid and oid in seen_all:
            continue
        if oid:
            seen_all.add(oid)
        status = str(row.get("status") or "").strip()
        if status in statuses:
            statuses[status] += 1
    order_count = len(seen_all) or len(all_rows)

    active = view._orders_filter_status

    def _chip(label, key, count, bg, fg):
        is_active = (key is None and active == "all") or (
            key is not None and active == key
        )
        border = ft.border.all(2, "#1E293B") if is_active else None

        def _click(_e, k=key):
            if k is None:
                view._orders_filter_status = "all"
                view._orders_filter_supplier = "all"
                view._orders_filter_category = "all"
                view._orders_filter_product = "all"
                view._orders_filter_payment = "all"
                view._render()
            else:
                new_val = "all" if active == k else k
                set_orders_filter(view, "status", new_val)

        return ft.Container(
            bgcolor=bg,
            border_radius=6,
            border=border,
            padding=ft.padding.symmetric(horizontal=6, vertical=2),
            ink=True,
            on_click=_click,
            content=ft.Row(
                [
                    ft.Text(
                        label, size=9, color=fg, weight="w600" if is_active else None
                    ),
                    ft.Container(
                        bgcolor=fg,
                        border_radius=10,
                        padding=ft.padding.symmetric(horizontal=5, vertical=1),
                        content=ft.Text(
                            str(count), size=9, color=_PANEL, weight="bold"
                        ),
                    ),
                ],
                spacing=4,
                tight=True,
            ),
        )

    controls = [
        _chip(
            view.t("orders.chip.total", "Замовлень"),
            None,
            order_count,
            _CHIP_BG,
            _CHIP_T,
        ),
        _chip(
            view.t("orders.status.production", "Вироб-во"),
            "production",
            statuses["production"],
            _ERR_BG,
            "#B91C1C",
        ),
        _chip(
            view.t("orders.status.ready_for_shipment", "Готовий"),
            "ready_for_shipment",
            statuses["ready_for_shipment"],
            _WARN_2,
            _WARN_T,
        ),
        _chip(
            view.t("orders.status.in_transit", "В дорозі"),
            "in_transit",
            statuses["in_transit"],
            _WARN_BG,
            _WARN_T,
        ),
        _chip(
            view.t("orders.status.at_warehouse", "На складі"),
            "at_warehouse",
            statuses["at_warehouse"],
            _OK_BG,
            _OK_T,
        ),
        _chip(
            view.t("orders.status.accepted", "Прийнятий"),
            "accepted",
            statuses["accepted"],
            _SOFT_P,
            "#5B21B6",
        ),
    ]
    return ft.Row(controls, spacing=6, wrap=True)


def _popup_filter(
    view,
    caption: str,
    selected_key: str,
    options: list[tuple[str, str]],
    on_pick,
    *,
    width: int = 90,
):
    _th = _th_from_view(view)
    _BORDER = _BORDER2 = _th["border"]
    _TEXT = _TEXT_2 = _th["text"]
    _MUTED = _th["muted"]
    _PANEL = _th["panel"]
    _HDR = _th["hdr"]
    _CHIP_T = _th["chip_text"]
    _CHIP_BG = _th["chip_bg"]
    _ALT = _ALT_2 = _th["alt"]
    selected_text = next(
        (txt for key, txt in options if key == selected_key),
        options[0][1] if options else "-",
    )
    items = [
        ft.PopupMenuItem(
            content=ft.Text(str(txt), size=8),
            on_click=view._guard_ui_action(
                "orders_filter_pick",
                lambda selected=key: on_pick(selected),
                view.t("cost_orders.err.filter", "Помилка фільтра"),
            ),
        )
        for key, txt in options
    ]
    return ft.PopupMenuButton(
        icon=None,
        items=items,
        menu_position=ft.PopupMenuPosition.UNDER,
        content=ft.Container(
            width=width,
            height=35,
            border=ft.border.all(1, _TEXT_2),
            border_radius=8,
            bgcolor=_ALT_2,
            padding=ft.padding.symmetric(horizontal=6, vertical=4),
            tooltip=ft.Tooltip(message=selected_text, wait_duration=0) if selected_text else None,
            content=ft.Column(
                [
                    ft.Text(
                        caption,
                        size=9,
                        color=_TEXT,
                        text_align=ft.TextAlign.CENTER,
                        no_wrap=True,
                    ),
                    ft.Text(
                        selected_text,
                        size=8,
                        color=_TEXT,
                        text_align=ft.TextAlign.CENTER,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                ],
                spacing=2,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                tight=True,
            ),
        ),
    )


def build_orders_currency_bar(view) -> ft.Container:
    _th = _th_from_view(view)
    _BORDER = _BORDER2 = _th["border"]
    _TEXT = _TEXT_2 = _th["text"]
    _MUTED = _th["muted"]
    _PANEL = _th["panel"]
    _HDR = _th["hdr"]
    _CHIP_T = _th["chip_text"]
    _CHIP_BG = _th["chip_bg"]
    _ALT = _ALT_2 = _th["alt"]
    defaults = view._column_defaults()
    logistics_ccy = str(defaults.get("log_calc_ccy") or "USD").upper().strip() or "USD"
    orders_unit_cost_ccy = (
        str(defaults.get("orders_unit_cost_ccy") or "USD").upper().strip() or "USD"
    )
    new_price_ccy = str(defaults.get("new_price_ccy") or "USD").upper().strip() or "USD"
    currency_opts = [
        (currency, f"{currency} {view._currency_symbol(currency)}")
        for currency in view.CURRENCIES
    ]
    controls = [
        _popup_filter(
            view,
            view.t("cost_orders.filter.logistics_ccy", "Логістика, валюта"),
            logistics_ccy,
            currency_opts,
            lambda value: view._set_global_logistics_currency(value),
            width=132,
        ),
        _popup_filter(
            view,
            view.t("cost_orders.filter.new_price_ccy", "Нова ціна, валюта"),
            new_price_ccy,
            currency_opts,
            lambda value: view._set_global_new_price_currency(value),
            width=138,
        ),
        _popup_filter(
            view,
            view.t("cost_orders.filter.unit_cost_ccy", "С/В замовлення, валюта"),
            orders_unit_cost_ccy,
            currency_opts,
            lambda value: view._set_global_order_unit_cost_currency(value),
            width=156,
        ),
    ]
    return ft.Container(
        bgcolor=_ALT_2,
        border_radius=6,
        padding=ft.padding.symmetric(horizontal=4, vertical=4),
        content=ft.Row(controls, spacing=8, wrap=True),
    )


def build_global_currency_bar(view) -> ft.Container:
    return build_orders_currency_bar(view)


def build_orders_filter_bar(view) -> ft.Container:
    _th = _th_from_view(view)
    _BORDER = _BORDER2 = _th["border"]
    _TEXT = _TEXT_2 = _th["text"]
    _MUTED = _th["muted"]
    _PANEL = _th["panel"]
    _HDR = _th["hdr"]
    _CHIP_T = _th["chip_text"]
    _CHIP_BG = _th["chip_bg"]
    _ALT = _ALT_2 = _th["alt"]
    supplier_opts = [
        ("all", view.t("orders.filter.all_suppliers", "Всі постачальники"))
    ] + [
        (sid, str((view._supplier_by_id.get(sid) or {}).get("name") or sid)[:42])
        for sid in sorted(
            {
                str(row.get("supplier_id") or "").strip()
                for row in view._orders_rows
                if row.get("supplier_id")
            }
        )
    ]
    category_opts = [
        ("all", view.t("orders.filter.all_categories", "Всі категорії"))
    ] + [
        (cid, str((view._category_by_id.get(cid) or {}).get("name") or cid)[:42])
        for cid in sorted(
            {
                str(row.get("category_id") or "").strip()
                for row in view._orders_rows
                if row.get("category_id")
            }
        )
    ]
    product_opts = [("all", view.t("orders.filter.all_products", "Всі товари"))] + [
        (pid, (view._product_display_name(view._product_by_id.get(pid) or {}) or pid)[:60])
        for pid in sorted(
            {
                str((row.get("item") or {}).get("product_id") or "").strip()
                for row in view._orders_rows
                if (row.get("item") or {}).get("product_id")
            }
        )
    ]

    controls = [
        _popup_filter(
            view,
            view.t("orders.field.status", "Статус"),
            view._orders_filter_status,
            [
                ("all", view.t("orders.filter.all_statuses", "Всі статуси")),
                ("production", view.t("orders.status.production", "Вироб-во")),
                (
                    "ready_for_shipment",
                    view.t("orders.status.ready_for_shipment", "Готовий"),
                ),
                ("in_transit", view.t("orders.status.in_transit", "В дорозі")),
                ("at_warehouse", view.t("orders.status.at_warehouse", "На складі")),
                ("accepted", view.t("orders.status.accepted", "Прийнятий")),
            ],
            lambda value: set_orders_filter(view, "status", value),
        ),
        _popup_filter(
            view,
            view.t("orders.filter.supplier", "Постачальник"),
            view._orders_filter_supplier,
            supplier_opts,
            lambda value: set_orders_filter(view, "supplier", value),
        ),
        _popup_filter(
            view,
            view.t("orders.filter.category", "Категорія"),
            view._orders_filter_category,
            category_opts,
            lambda value: set_orders_filter(view, "category", value),
        ),
        _popup_filter(
            view,
            view.t("orders.filter.product", "Товар"),
            view._orders_filter_product,
            product_opts,
            lambda value: set_orders_filter(view, "product", value),
        ),
        _popup_filter(
            view,
            view.t("orders.field.payment", "Оплата"),
            view._orders_filter_payment,
            [
                ("all", view.t("orders.filter.all_payments", "Всі оплати")),
                ("unpaid", view.t("orders.payment.unpaid", "Не сплачен.")),
                ("advance", view.t("orders.payment.advance", "Аванс")),
                ("paid", view.t("orders.payment.paid", "Сплач.")),
            ],
            lambda value: set_orders_filter(view, "payment", value),
        ),
    ]
    sel_count = len(view._orders_selected or set())
    merge_color = _CHIP_T if sel_count >= 2 else _MUTED
    merge_text_color = _PANEL
    merge_btn = ft.Container(
        width=120,
        height=35,
        border=ft.border.all(1, merge_color),
        border_radius=8,
        bgcolor=merge_color,
        padding=ft.padding.symmetric(horizontal=6, vertical=4),
        tooltip=(
            view.t(
                "cost_orders.merge_hint_select", "Вибери 2+ замовлення для об'єднання"
            )
            if sel_count < 2
            else view.t(
                "cost_orders.merge_hint_count", "Об'єднати {n} замовлень"
            ).format(n=sel_count)
        ),
        content=ft.Row(
            [
                ft.Icon(ft.Icons.MERGE_TYPE, size=12, color=merge_text_color),
                ft.Text(
                    (
                        view.t("cost_orders.merge_count", "Об'єднати ({n})").format(
                            n=sel_count
                        )
                        if sel_count >= 2
                        else view.t("cost_orders.merge", "Об'єднати")
                    ),
                    size=9,
                    color=merge_text_color,
                    no_wrap=True,
                ),
            ],
            spacing=3,
            tight=True,
        ),
        on_click=(
            view._guard_ui_action(
                "merge_orders",
                view._merge_selected_orders,
                view.t("cost_orders.err.merge", "Помилка об'єднання"),
            )
            if sel_count >= 2
            else None
        ),
    )
    controls.append(merge_btn)
    view._merge_btn_ref = merge_btn
    return ft.Container(
        bgcolor=_ALT,
        border_radius=6,
        padding=ft.padding.symmetric(horizontal=4, vertical=4),
        content=ft.Row(controls, spacing=4, wrap=False),
    )


def set_orders_filter(view, key: str, value: str) -> None:
    if key == "status":
        view._orders_filter_status = value
    elif key == "supplier":
        view._orders_filter_supplier = value
    elif key == "category":
        view._orders_filter_category = value
    elif key == "product":
        view._orders_filter_product = value
    elif key == "payment":
        view._orders_filter_payment = value
    view._render()


def build_orders_table(view, rows: list) -> dict:
    _th = _th_from_view(view)
    _BORDER = _BORDER2 = _th["border"]
    _TEXT = _TEXT_2 = _th["text"]
    _MUTED = _th["muted"]
    _PANEL = _th["panel"]
    _ERR_BG = _th["err_bg"]
    _HDR = _th["hdr"]
    _CHIP_T = _th["chip_text"]
    _OK_BG = _th["ok_bg"]
    _ALT = _ALT_2 = _th["alt"]
    _ERR = _th["err"]
    _SOFT_P = _th["soft"]
    _WARN_T = _OK_T = _th["warn"]
    _WARN = _th["warn"]
    _WARN_BG = _th["warn_bg"]
    _CHIP_BG = _th["chip_bg"]
    _OK_FG = _th["ok"]
    _SKY = _th["accent"]
    _ERR_2 = _th["err_bg"]
    _WARN_2 = _th["warn_bg"]
    _OK_2 = _th["ok_bg"]
    all_tiers = [tier for tier in (view._price_tiers or []) if isinstance(tier, dict)]
    visible_ids = set(view._visible_orders_tier_ids())
    visible_base_ids = set(view._visible_orders_base_column_ids())
    visible_tiers = [
        tier
        for tier in all_tiers
        if str((tier or {}).get("id") or "").strip() in visible_ids
    ]
    defaults = view._column_defaults()
    logistics_ccy = str(defaults.get("log_calc_ccy") or "USD").upper().strip() or "USD"
    orders_unit_cost_ccy = (
        str(defaults.get("orders_unit_cost_ccy") or "USD").upper().strip() or "USD"
    )
    new_price_ccy = str(defaults.get("new_price_ccy") or "USD").upper().strip() or "USD"
    width_defaults = defaults.get("orders_base_column_widths")
    if not isinstance(width_defaults, dict):
        width_defaults = {}
    # frozen: ☑ + ; решта колонок можуть змінювати порядок у шестерні.
    frozen_count = 2
    base_column_keys = [
        "select",
        "expand",
        "date",
        "invoice",
        "order1c",
        "product",
        "status",
        "delivery",
        "comment",
        "qty",
        "amount",
        "full_amount",
        "calc_weight",
        "fact_weight",
        "tariff",
        "log_calc",
        "log_fact",
        "unit_cost",
    ]
    ordered_base_raw = []
    try:
        ordered_base_raw = list(view._ordered_orders_base_column_ids())
    except Exception:
        ordered_base_raw = []
    non_tier_keys = list(base_column_keys) + ["new_price", "new_margin"]
    display_base_keys = ["select", "expand"] + [
        k
        for k in ordered_base_raw
        if k in non_tier_keys and k not in {"select", "expand"}
    ]
    display_base_keys += [k for k in non_tier_keys if k not in display_base_keys]
    _base_key_to_idx = {k: i for i, k in enumerate(base_column_keys)}
    _base_len = len(base_column_keys)

    def _reorder_base_segment(seq: list) -> list:
        if not isinstance(seq, list):
            return seq
        base_part = list(seq[:_base_len])
        tier_part = list(seq[_base_len:-2]) if len(seq) >= (_base_len + 2) else []
        tail_part = list(seq[-2:]) if len(seq) >= 2 else []
        key_to_cell = {
            k: base_part[_base_key_to_idx[k]]
            for k in base_column_keys
            if _base_key_to_idx[k] < len(base_part)
        }
        key_to_cell["new_price"] = (
            tail_part[0]
            if len(tail_part) >= 1
            else ft.Container(width=0, height=0, visible=False)
        )
        key_to_cell["new_margin"] = (
            tail_part[1]
            if len(tail_part) >= 2
            else ft.Container(width=0, height=0, visible=False)
        )
        reordered = [key_to_cell[k] for k in display_base_keys if k in key_to_cell]
        return reordered + tier_part

    view._last_reorder_base_cells = _reorder_base_segment

    headers = [
        "",  # 0  ☑
        "+",  # 1  expand
        view.t("orders.field.order_date", "Дата замовлення"),  # 2
        view.t("orders.field.invoice_num", "№ Інвойсу"),  # 3
        view.t("payments.col.order1c", "ЗП обліку"),  # 4
        view.t("orders.filter.product", "Товар"),  # 5
        view.t("orders.field.status", "Статус"),  # 6
        view.t("orders.field.delivery_type", "Тип доставки"),  # 7
        view.t("orders.field.comment", "Коментар"),  # 8
        view.t("orders.field.quantity", "К-сть"),  # 9
        view.t("payments.col.amount", "Вартість"),  # 10
        view.t("cost_orders.col.amount_full", "Вартість повна"),  # 11
        view.t("cost_orders.col.weight_calc", "Вага розр."),  # 12
        view.t("cost_orders.col.weight_actual", "Вага факт."),  # 13
        view.t("cost_orders.col.tariff", "Тариф"),  # 14
        view.t("cost_orders.col.log_calc", "Лог. розр. {ccy}").format(
            ccy=view._currency_symbol(logistics_ccy)
        ),  # 15
        view.t("cost_orders.col.log_actual", "Лог. факт {ccy}").format(
            ccy=view._currency_symbol(logistics_ccy)
        ),  # 16
        view.t("cost_orders.col.unit_cost", "С/В {ccy}").format(
            ccy=view._currency_symbol(orders_unit_cost_ccy)
        ),  # 17
    ]
    for tier in visible_tiers:
        nm = str(
            (tier or {}).get("name") or view.t("cost_orders.tier_fallback", "Рівень")
        )
        headers.append(nm)
        headers.append("")  # merged into price column
    headers += [
        view.t("cost_orders.col.new_price", "Нова ціна {ccy}").format(
            ccy=view._currency_symbol(new_price_ccy)
        ),
        "",
    ]  # new_margin merged

    default_widths = view._base_column_default_widths()
    tier_widths_cfg = defaults.get("orders_tier_widths") or {}
    tier_widths_flat: list[int] = []
    for _t in visible_tiers:
        _tid = str((_t or {}).get("id") or "")
        _tw = tier_widths_cfg.get(_tid) or {}
        tier_widths_flat.append(
            int(_tw.get("width") or 0)
            or (int(_tw.get("price_w") or 50) + int(_tw.get("margin_w") or 50))
        )
        tier_widths_flat.append(0)  # margin merged into price column
    # Full configured widths for ALL tiers (incl. invisible) — used by surgical tier toggle
    _all_tier_widths: dict[str, int] = {}
    for _t in all_tiers:
        _tid = str((_t or {}).get("id") or "")
        _tw = tier_widths_cfg.get(_tid) or {}
        _all_tier_widths[_tid] = int(_tw.get("width") or 0) or (
            int(_tw.get("price_w") or 50) + int(_tw.get("margin_w") or 50)
        )
    np_w = int(width_defaults.get("new_price") or default_widths.get("new_price") or 72)
    nm_w = int(
        width_defaults.get("new_margin") or default_widths.get("new_margin") or 72
    )
    combo_np_w = np_w + nm_w
    widths = (
        [
            int(width_defaults.get(key) or default_widths.get(key) or 0)
            for key in base_column_keys
        ]
        + tier_widths_flat
        + [combo_np_w, 0]
    )
    widths = (
        [
            (
                width
                if base_column_keys[idx] in {"select", "expand"}
                or base_column_keys[idx] in visible_base_ids
                else 0
            )
            for idx, width in enumerate(widths[: len(base_column_keys)])
        ]
        + list(widths[len(base_column_keys) : -2])
        + [
            combo_np_w if "new_price" in visible_base_ids else 0,
            0,  # new_margin merged into new_price column
        ]
    )
    frozen_width = sum(widths[:frozen_count])
    scroll_width = sum(widths[frozen_count:])
    row_height = 34
    tf_text_size = 8
    tf_vpad = max(0, int((row_height - (tf_text_size + 6)) / 2))

    def _need_lines_for_text(txt: str, w: int | None) -> int:
        """How many lines `txt` needs to fully fit a cell `w` px wide — same
        heuristic as orders_view.py._build_order_row, so long disambiguated
        product names ("Name | Кат: X | Постач: Y") grow the row instead of
        getting clipped."""
        try:
            width = int(w or 0)
        except Exception:
            width = 0
        if width <= 0:
            return 1
        s = "-" if txt is None else str(txt)
        wrapped = _wrap_text_for_width(s, width)
        return max(1, int(str(wrapped).count("\n")) + 1)

    def _row_height_for(*texts_and_widths: tuple[str, int | None], base_h: int = row_height) -> int:
        """Row height that fits the tallest of `texts_and_widths` — pass each
        (text, column_width) pair that appears in a given row."""
        line_counts = [_need_lines_for_text(t, w) for t, w in texts_and_widths]
        max_lines_row = max(line_counts) if line_counts else 1
        if max_lines_row <= 1:
            return base_h
        return int(min(220, max(base_h, 16 * max_lines_row + 4)))

    # Store build state so surgical merge/unmerge can use correct widths/helpers
    # without triggering a full rebuild.
    view._last_widths = widths[:]
    view._last_frozen_count = frozen_count
    view._last_row_height = row_height
    view._last_tf_vpad = tf_vpad
    view._last_visible_tiers = visible_tiers[:]
    view._last_all_tier_widths = dict(_all_tier_widths)

    # ── status / delivery color helpers ──────────────────────────────────────
    _STATUS_BG = {
        "production": _ERR_2,
        "ready_for_shipment": _WARN_2,
        "in_transit": _CHIP_BG,
        "at_warehouse": _OK_BG,
        "accepted": "#065F46",
    }
    _STATUS_FG = {
        "production": _ERR,  # was "#7F1D1D" — use theme err so light in dark themes
        "ready_for_shipment": _WARN_T,
        "in_transit": _CHIP_T,
        "at_warehouse": _OK_T,
        "accepted": "#FFFFFF",  # was _PANEL — always white; bg "#065F46" is always dark
    }
    _DELIVERY_PALETTE = [
        _WARN_BG,
        "#FDE68A",
        _ERR_2,
        "#FCA5A5",
        _OK_BG,
        _OK_2,
        _CHIP_BG,
        "#93C5FD",
        "#E9D5FF",
        "#DDD6FE",
    ]

    def _bg_contrast_text(bg_hex: str) -> str:
        h = bg_hex.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (
            "#111827" if (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.45 else "#FFFFFF"
        )

    def _status_cell_colors(st: str):
        s = str(st or "").strip()
        return _STATUS_BG.get(s, _PANEL), _STATUS_FG.get(s, _TEXT_2)

    def _delivery_cell_colors(name: str):
        raw = str(name or "").strip()
        if not raw:
            return _PANEL, _TEXT_2
        idx = sum(ord(ch) for ch in raw) % len(_DELIVERY_PALETTE)
        bg = _DELIVERY_PALETTE[idx]
        return bg, _bg_contrast_text(bg)

    # ── inner cell helpers ────────────────────────────────────────────────────
    def _cell_text(
        text: str,
        w: int,
        bg: str,
        align=ft.TextAlign.CENTER,
        bold: bool = False,
        max_lines: int = 1,
        wrap: bool = False,
        h: int | None = None,
    ) -> ft.Container:
        if int(w or 0) <= 0:
            return ft.Container(width=0, height=0, visible=False)
        _h = h if h is not None else row_height
        if wrap:
            return ft.Container(
                width=w,
                height=_h,
                bgcolor=bg,
                border=ft.border.all(1, _BORDER),
                alignment=ft.Alignment(0, 0),
                padding=ft.padding.symmetric(horizontal=4),
                content=ft.Text(
                    format_maybe_numeric_text(str(text or "")),
                    size=8,
                    color=_TEXT,
                    weight="bold" if bold else None,
                    text_align=align,
                    no_wrap=False,
                    max_lines=max(_h // 11, 2) if _h else None,
                    overflow=ft.TextOverflow.CLIP,
                ),
            )
        return ft.Container(
            width=w,
            height=_h,
            bgcolor=bg,
            border=ft.border.all(1, _BORDER),
            alignment=ft.Alignment(0, 0),
            content=ft.Text(
                format_maybe_numeric_text(str(text or "")),
                size=8,
                color=_TEXT,
                weight="bold" if bold else None,
                text_align=align,
                no_wrap=max_lines == 1,
                max_lines=max_lines,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
        )

    def _cell_edit(
        value: str,
        w: int,
        bg: str,
        on_save,
        align=ft.TextAlign.CENTER,
        max_lines: int = 1,
        h: int | None = None,
    ) -> ft.Container:
        if int(w or 0) <= 0:
            return ft.Container(width=0, height=0, visible=False)
        _h = h if h is not None else row_height
        tf = ft.TextField(
            value=str(value or ""),
            width=w,
            height=_h,
            max_lines=max_lines,
            text_size=tf_text_size,
            dense=True,
            filled=True,
            fill_color=bg,
            border=ft.InputBorder.NONE,
            border_width=0,
            border_radius=0,
            content_padding=ft.padding.symmetric(horizontal=4, vertical=tf_vpad),
            text_align=align,
            text_vertical_align=ft.VerticalAlignment.CENTER,
            read_only=view.is_read_only,
        )
        view._bind_commit_handlers(
            tf,
            lambda raw_value: on_save(raw_value),
            tag="orders_cell_edit",
            error_text="Помилка збереження",
        )
        return ft.Container(
            width=w,
            height=_h,
            bgcolor=bg,
            border=ft.border.all(1, _BORDER),
            alignment=ft.Alignment(0, 0),
            padding=ft.padding.all(0),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=tf,
        )

    def _mk_product_cell_cpo(
        value: str,
        w: int,
        bg: str,
        on_save,
        pid: str = "",
        h: int | None = None,
    ) -> ft.Container:
        _h = h if h is not None else row_height
        # max_lines scales with the row height so the field can actually use
        # the extra vertical space instead of clipping a taller container's
        # worth of text into a fixed 2 lines.
        _max_lines = max(2, _h // 16)
        cell = _cell_edit(
            value, w, bg, on_save, align=ft.TextAlign.LEFT, max_lines=_max_lines, h=_h
        )
        pid = str(pid or "").strip()
        if not pid or int(w or 0) <= 0:
            return cell
        from views.product_quick_view import product_icon_btn

        _btn = product_icon_btn(
            pid, view.content_area, view.db, view.user_data, size=10
        )
        cell.clip_behavior = ft.ClipBehavior.HARD_EDGE
        cell.content = ft.Stack(
            [
                cell.content,
                ft.Container(content=_btn, right=1, top=max(0, (_h - 22) // 2)),
            ],
            width=w,
            height=_h,
        )
        return cell

    def _mk_order_label_cell(
        label: str,
        w: int,
        bg: str,
        pid: str = "",
        h: int | None = None,
    ) -> ft.Container:
        _h = h if h is not None else row_height
        cell = _cell_text(
            label, w, bg, max_lines=max(2, _h // 16), wrap=True, h=_h
        )
        pid = str(pid or "").strip()
        if not pid or int(w or 0) <= 0:
            return cell
        from views.product_quick_view import product_icon_btn

        _btn = product_icon_btn(
            pid, view.content_area, view.db, view.user_data, size=10
        )
        cell.content = ft.Row(
            [cell.content, _btn],
            spacing=2,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        cell.alignment = ft.Alignment(-1, 0)
        cell.padding = ft.padding.only(left=4, right=2)
        return cell

    def _cell_tf(
        value: str,
        w: int,
        bg: str,
        on_save,
        tag: str = "orders_tf",
        normal_bg: str = None,
        h: int | None = None,
    ) -> ft.Container:
        if int(w or 0) <= 0:
            return ft.Container(width=0, height=0, visible=False)
        _nbg = normal_bg if normal_bg is not None else bg
        _tfh = h if h is not None else row_height
        tf = ft.TextField(
            value=str(value or ""),
            width=w,
            height=_tfh,
            text_size=tf_text_size,
            dense=True,
            filled=True,
            fill_color=bg,
            border=ft.InputBorder.NONE,
            border_width=0,
            border_radius=0,
            content_padding=ft.padding.symmetric(horizontal=4, vertical=tf_vpad),
            text_align=ft.TextAlign.CENTER,
            text_vertical_align=ft.VerticalAlignment.CENTER,
            read_only=view.is_read_only,
        )
        ct = ft.Container(
            width=w,
            height=_tfh,
            bgcolor=bg,
            border=ft.border.all(1, _BORDER),
            alignment=ft.Alignment(0, 0),
            padding=ft.padding.all(0),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=tf,
        )
        ct._normal_bg = (
            _nbg  # Store normal bg for external updates (distribution, clear)
        )

        def _do_save(raw_value: str) -> str:
            # Instant in-place: format to "00,00" and update cell color immediately,
            # BEFORE running on_save. on_save does disk I/O (ops-outbox write) and
            # recalculation that can take a noticeable moment — if it ran first, the
            # typed value would appear "stuck" until all of that finished.
            parsed = _safe_float(raw_value, None)
            formatted = (
                format_grouped(parsed, decimals=2, trim=False)
                if parsed is not None
                else ""
            )
            new_bg = _ERR_BG if parsed is None else _nbg
            tf.value = formatted
            tf.fill_color = new_bg
            ct.bgcolor = new_bg
            with contextlib.suppress(Exception):
                ct.update()
            on_save(raw_value)
            return formatted  # returned to _bind_commit_handlers for last_value sync

        view._bind_commit_handlers(
            tf, _do_save, tag=tag, error_text="Помилка збереження"
        )
        return ct

    def _cell_status(
        w: int, bg: str, current_status: str, order_obj: dict, h: int | None = None
    ) -> ft.Container:
        if int(w or 0) <= 0:
            return ft.Container(width=0, height=0, visible=False)
        _h = h if h is not None else row_height
        st_bg, st_fg = _status_cell_colors(current_status)
        label_text = ft.Text(
            view.STATUS_LABELS.get(current_status, current_status),
            size=8,
            color=st_fg,
            no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        status_container = ft.Container(
            width=w,
            height=_h,
            bgcolor=st_bg,
            border=ft.border.all(1, _BORDER),
            alignment=ft.Alignment(0, 0),
            padding=ft.padding.symmetric(horizontal=2, vertical=2),
            content=ft.PopupMenuButton(
                icon=None,
                menu_position=ft.PopupMenuPosition.UNDER,
                disabled=view.is_read_only,
                items=[
                    ft.PopupMenuItem(
                        content=ft.Text(lbl, size=8),
                        on_click=view._guard_ui_action(
                            "orders_status_pick",
                            lambda s=s, o=order_obj: view._save_and_update_status(o, s),
                            "Помилка зміни статусу",
                        ),
                    )
                    for s, lbl in view.STATUS_LABELS.items()
                ],
                content=ft.Container(
                    width=w - 2,
                    height=_h - 4,
                    alignment=ft.Alignment(0, 0),
                    content=label_text,
                ),
            ),
        )
        oid = str((order_obj or {}).get("id") or "").strip()
        if oid:
            view._order_status_cell_map[oid] = (status_container, label_text)
        return status_container

    def _cell_date(
        date_iso: str, w: int, bg: str, order: dict, h: int | None = None
    ) -> ft.Container:
        """Date cell: read-only for viewers, clickable DatePicker for owner."""
        if int(w or 0) <= 0:
            return ft.Container(width=0, height=0, visible=False)
        _h = h if h is not None else row_height
        lbl = ft.Text(
            str(date_iso or ""),
            size=8,
            color=_TEXT,
            text_align=ft.TextAlign.CENTER,
            no_wrap=True,
        )
        if view.is_read_only:
            return ft.Container(
                width=w,
                height=_h,
                bgcolor=bg,
                border=ft.border.all(1, _BORDER),
                alignment=ft.Alignment(0, 0),
                content=lbl,
            )
        from datetime import date as _date
        from datetime import datetime as _datetime

        def _open_picker(_e=None, _o=order):
            pg = view._pg()
            if not pg:
                return
            try:
                initial = _datetime.fromisoformat(
                    str(_o.get("order_date") or "")[:10]
                ).date()
            except Exception:
                initial = _date.today()

            def _picked(ev):
                val = getattr(ev.control, "value", None)
                if val is None:
                    return
                try:
                    iso = (
                        val.date().isoformat()
                        if hasattr(val, "date")
                        else str(val)[:10]
                    )
                except Exception:
                    return
                lbl.value = iso
                with contextlib.suppress(Exception):
                    lbl.update()
                view._save_order_fields(_o, 0, {}, {"order_date": iso})
                with contextlib.suppress(Exception):
                    pg.close_dialog()

            def _dismissed(_=None):
                with contextlib.suppress(Exception):
                    pg.close_dialog()

            dp = ft.DatePicker(
                value=_datetime.combine(initial, _datetime.min.time()),
                first_date=_date(2018, 1, 1),
                last_date=_date(2100, 12, 31),
                entry_mode=ft.DatePickerEntryMode.CALENDAR_ONLY,
                date_picker_mode=ft.DatePickerMode.DAY,
                locale=ft.Locale("uk", "UA"),
                cancel_text="Скасувати",
                on_change=_picked,
                on_dismiss=_dismissed,
            )
            pg.show_dialog(dp)

        return ft.Container(
            width=w,
            height=_h,
            bgcolor=bg,
            border=ft.border.all(1, _BORDER),
            alignment=ft.Alignment(0, 0),
            ink=True,
            on_click=_open_picker,
            tooltip="Клікніть для зміни дати",
            content=ft.Row(
                [lbl, ft.Icon(ft.Icons.EDIT_CALENDAR, size=10, color=_MUTED)],
                spacing=2,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _cell_status_cascade(
        w: int, bg: str, current_status: str, gid: str, h: int | None = None
    ) -> ft.Container:
        if int(w or 0) <= 0:
            return ft.Container(width=0, height=0, visible=False)
        st_bg, st_fg = _status_cell_colors(current_status)
        return ft.Container(
            width=w,
            height=h if h is not None else row_height,
            bgcolor=st_bg,
            border=ft.border.all(1, _BORDER),
            alignment=ft.Alignment(0, 0),
            padding=ft.padding.symmetric(horizontal=2, vertical=2),
            content=ft.PopupMenuButton(
                icon=None,
                menu_position=ft.PopupMenuPosition.UNDER,
                disabled=view.is_read_only,
                items=[
                    ft.PopupMenuItem(
                        content=ft.Text(lbl, size=8),
                        on_click=view._guard_ui_action(
                            "merge_status_pick",
                            lambda s=s, g=gid: view._cascade_merge_status(g, s),
                            "Помилка зміни статусу",
                        ),
                    )
                    for s, lbl in view.STATUS_LABELS.items()
                ],
                content=ft.Container(
                    width=w - 2,
                    height=(h if h is not None else row_height) - 4,
                    alignment=ft.Alignment(0, 0),
                    content=ft.Text(
                        view.STATUS_LABELS.get(current_status, current_status),
                        size=8,
                        color=st_fg,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                ),
            ),
        )

    def _cell_delivery(
        w: int, bg: str, order_obj: dict, h: int | None = None
    ) -> ft.Container:
        if int(w or 0) <= 0:
            return ft.Container(width=0, height=0, visible=False)
        _h = h if h is not None else row_height
        delivery_type_id = str((order_obj.get("delivery_type_id") or "")).strip()
        delivery_type_name = str(
            (
                order_obj.get("delivery_type_name")
                or order_obj.get("delivery_type")
                or ""
            )
        ).strip()
        selected_delivery_row = next(
            (
                m
                for m in (view._delivery_methods or [])
                if str((m or {}).get("id") or "").strip() == delivery_type_id
            ),
            None,
        )
        if selected_delivery_row is None and delivery_type_name:
            norm = delivery_type_name.lower().strip()
            selected_delivery_row = next(
                (
                    m
                    for m in (view._delivery_methods or [])
                    if str((m or {}).get("name") or "").lower().strip() == norm
                ),
                None,
            )
        current_label = str(
            (
                (selected_delivery_row or {}).get("name")
                if isinstance(selected_delivery_row, dict)
                else ""
            )
            or delivery_type_name
            or "-"
        ).strip()
        items = []
        for method_row in view._delivery_methods or []:
            if not isinstance(method_row, dict):
                continue
            method_id = str((method_row.get("id") or "")).strip()
            method_name = str((method_row.get("name") or "")).strip()
            price, ccy = view._delivery_method_tariff(method_row)
            order_patch = {
                "delivery_type_id": method_id,
                "delivery_type_name": method_name,
                "delivery_price_snapshot": price,
                "delivery_price_currency_snapshot": ccy,
            }
            items.append(
                ft.PopupMenuItem(
                    content=ft.Text(method_name or "-", size=9),
                    on_click=view._guard_ui_action(
                        "orders_delivery_pick",
                        lambda o=order_obj, op=dict(
                            order_patch
                        ): view._save_and_update_delivery(o, op),
                        "Помилка зміни доставки",
                    ),
                )
            )
        del_bg, del_fg = _delivery_cell_colors(current_label)
        del_label_text = ft.Text(
            current_label,
            size=8,
            color=del_fg,
            no_wrap=True,
            overflow=ft.TextOverflow.ELLIPSIS,
        )
        del_container = ft.Container(
            width=w,
            height=_h,
            bgcolor=del_bg,
            border=ft.border.all(1, _BORDER),
            alignment=ft.Alignment(0, 0),
            padding=ft.padding.symmetric(horizontal=2, vertical=2),
            content=ft.PopupMenuButton(
                icon=None,
                menu_position=ft.PopupMenuPosition.UNDER,
                disabled=view.is_read_only,
                items=items,
                content=ft.Container(
                    width=w - 2,
                    height=_h - 4,
                    alignment=ft.Alignment(0, 0),
                    content=del_label_text,
                ),
            ),
        )
        oid = str((order_obj or {}).get("id") or "").strip()
        if oid:
            view._order_delivery_cell_map[oid] = (del_container, del_label_text)
        return del_container

    def _cell_delivery_cascade(
        w: int, bg: str, order_obj: dict, gid: str, h: int | None = None
    ) -> ft.Container:
        if int(w or 0) <= 0:
            return ft.Container(width=0, height=0, visible=False)
        delivery_type_id = str((order_obj.get("delivery_type_id") or "")).strip()
        delivery_type_name = str(
            (
                order_obj.get("delivery_type_name")
                or order_obj.get("delivery_type")
                or ""
            )
        ).strip()
        selected_delivery_row = next(
            (
                m
                for m in (view._delivery_methods or [])
                if str((m or {}).get("id") or "").strip() == delivery_type_id
            ),
            None,
        )
        if selected_delivery_row is None and delivery_type_name:
            norm = delivery_type_name.lower().strip()
            selected_delivery_row = next(
                (
                    m
                    for m in (view._delivery_methods or [])
                    if str((m or {}).get("name") or "").lower().strip() == norm
                ),
                None,
            )
        current_label = str(
            (
                (selected_delivery_row or {}).get("name")
                if isinstance(selected_delivery_row, dict)
                else ""
            )
            or delivery_type_name
            or "-"
        ).strip()
        items = []
        for method_row in view._delivery_methods or []:
            if not isinstance(method_row, dict):
                continue
            method_name = str((method_row.get("name") or "")).strip()
            items.append(
                ft.PopupMenuItem(
                    content=ft.Text(method_name or "-", size=9),
                    on_click=view._guard_ui_action(
                        "merge_delivery_pick",
                        lambda mr=dict(method_row), g=gid: view._cascade_merge_delivery(
                            g, mr
                        ),
                        "Помилка зміни доставки",
                    ),
                )
            )
        del_bg2, del_fg2 = _delivery_cell_colors(current_label)
        _dh = h if h is not None else row_height
        return ft.Container(
            width=w,
            height=_dh,
            bgcolor=del_bg2,
            border=ft.border.all(1, _BORDER),
            alignment=ft.Alignment(0, 0),
            padding=ft.padding.symmetric(horizontal=2, vertical=2),
            content=ft.PopupMenuButton(
                icon=None,
                menu_position=ft.PopupMenuPosition.UNDER,
                disabled=view.is_read_only,
                items=items,
                content=ft.Container(
                    width=w - 2,
                    height=_dh - 4,
                    alignment=ft.Alignment(0, 0),
                    content=ft.Text(
                        current_label,
                        size=8,
                        color=del_fg2,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                ),
            ),
        )

    # ── header row ────────────────────────────────────────────────────────────
    def _make_hdr_cell(w: int, label: str) -> ft.Container:
        if int(w or 0) <= 0:
            return ft.Container(width=0, height=0, visible=False)
        return ft.Container(
            width=w,
            height=32,
            bgcolor=_HDR,
            border=ft.border.all(1, _BORDER),
            padding=ft.padding.symmetric(horizontal=4, vertical=2),
            content=ft.Column(
                [
                    ft.Text(
                        "" if label in ("", "+") else label,
                        size=9,
                        weight="bold",
                        color=_CHIP_T,
                        text_align=ft.TextAlign.CENTER,
                        width=max(0, w - 10),
                    )
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=0,
            ),
        )

    header_cells = []
    # Base columns 0-17
    for idx in range(min(18, len(headers))):
        header_cells.append(
            _make_hdr_cell(widths[idx] if idx < len(widths) else 0, headers[idx])
        )

    # Tier columns — ALL tiers (visible get proper width, invisible get zero-width placeholder)
    _hdr_tier_i = 18
    _header_tier_cell_refs: dict = {}  # tier_id → (container, full_width)
    for _ht in all_tiers:
        _htid = str((_ht or {}).get("id") or "").strip()
        _ht_name = str((_ht or {}).get("name") or "Рівень")
        _ht_visible = _htid in visible_ids
        if _ht_visible:
            _htw = widths[_hdr_tier_i] if _hdr_tier_i < len(widths) else 0
            _hdr_tier_i += 2
        else:
            _htw = _all_tier_widths.get(_htid, 0)
        if _ht_visible and int(_htw or 0) > 0:
            _hdr_c = ft.Container(
                width=_htw,
                height=32,
                bgcolor=_HDR,
                border=ft.border.all(1, _BORDER),
                padding=ft.padding.symmetric(horizontal=4, vertical=2),
                content=ft.Column(
                    [
                        ft.Text(
                            _ht_name,
                            size=9,
                            weight="bold",
                            color=_CHIP_T,
                            text_align=ft.TextAlign.CENTER,
                            width=max(0, _htw - 10),
                        )
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=0,
                ),
            )
        else:
            _hdr_c = ft.Container(width=0, height=0, visible=False)
        _header_tier_cell_refs[_htid] = (_hdr_c, _htw)
        header_cells.append(_hdr_c)
        header_cells.append(ft.Container(width=0, height=0, visible=False))

    # New price / new margin columns (last 2 of original headers/widths)
    _np_start = 18 + 2 * len(visible_tiers)
    for idx in range(_np_start, len(headers)):
        header_cells.append(
            _make_hdr_cell(widths[idx] if idx < len(widths) else 0, headers[idx])
        )

    view._header_tier_cell_refs = _header_tier_cell_refs
    header_cells = _reorder_base_segment(header_cells)
    _left_header_row = ft.Row(header_cells[:frozen_count], spacing=0)
    _right_header_row = ft.Row(header_cells[frozen_count:], spacing=0)
    left_rows = []
    right_rows = []

    # Per-row ref tracking: populated by _append_split_row / _append_detail_cols.
    # _tracking = [id_str, is_group_row]  — set before each order/group block.
    view._order_left_row_refs = {}
    view._order_right_row_refs = {}
    view._group_left_row_refs = {}
    view._group_right_row_refs = {}
    view._sub_logfact_ct_refs = {}  # gid → {order_id → log_fact cell Container}
    view._sub_cv_ct_refs = {}  # gid → {order_id → (cv_cell, ccy)}
    view._sub_fw_ct_refs = {}  # gid → {order_id → fact_weight cell Container}
    view._order_tier_all_cell_refs = (
        {}
    )  # "oid:line_idx" → {tier_id → (container, sale_txt, margin_txt, full_width)}
    view._merge_cv_cell_refs = {}  # gid → (cv_cell, ccy)
    _tracking: list = [None, False]

    # Overflow rows are pre-built (but hidden) so toggling is instant.
    _ovf: dict = {"active": False, "left": [], "right": []}

    def _append_split_row(cells: list) -> None:
        cells = _reorder_base_segment(cells)
        lr = ft.Row(
            cells[:frozen_count],
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        rr = ft.Row(
            cells[frozen_count:],
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        _tid, _isg = _tracking
        # Track refs for ALL orders (both main-column and overflow-zone).
        # Surgical merge/unmerge needs refs regardless of where the order lives.
        if _tid:
            if _isg:
                view._group_left_row_refs.setdefault(_tid, []).append(lr)
                view._group_right_row_refs.setdefault(_tid, []).append(rr)
            else:
                view._order_left_row_refs.setdefault(_tid, []).append(lr)
                view._order_right_row_refs.setdefault(_tid, []).append(rr)
        if _ovf["active"]:
            _ovf["left"].append(lr)
            _ovf["right"].append(rr)
        else:
            left_rows.append(lr)
            right_rows.append(rr)

    def _append_detail_cols(lc, rc) -> None:
        """Route expand/collapse sub-row Columns to the correct bucket.

        Sub-row columns for overflow-zone orders must live inside _ovf so they
        are shown/hidden together with their parent order header row when the
        user toggles the overflow section.
        """
        _tid, _isg = _tracking
        # Track sub-item columns for ALL orders (both main and overflow).
        if _tid:
            if _isg:
                view._group_left_row_refs.setdefault(_tid, []).append(lc)
                view._group_right_row_refs.setdefault(_tid, []).append(rc)
            else:
                view._order_left_row_refs.setdefault(_tid, []).append(lc)
                view._order_right_row_refs.setdefault(_tid, []).append(rc)
        if _ovf["active"]:
            _ovf["left"].append(lc)
            _ovf["right"].append(rc)
        else:
            left_rows.append(lc)
            right_rows.append(rc)

    # ── item sub-rows renderer (reused for multi-item orders and sub-sub-orders) ──
    def _render_item_rows(order_obj, items_list, bg1, bg2, left_col, right_col):
        inv_plain = str(order_obj.get("invoice_no") or "")
        zp_plain = str(order_obj.get("order_no_1c") or "")
        for si, row in enumerate(items_list):
            item_bg = bg1 if si % 2 == 0 else bg2
            target_margin = view._target_margin(row.get("category_id") or "")
            _row_h = _row_height_for(
                (row.get("product_name") or "", widths[5]),
                (row.get("item_comment") or "", widths[8]),
            )
            _fw_cell = _cell_tf(
                (
                    format_grouped(
                        _safe_float(row.get("fact_weight"), None),
                        decimals=2,
                        trim=False,
                    )
                    if row.get("fact_weight") is not None
                    else ""
                ),
                widths[13],
                _ERR_BG if row.get("fact_weight") is None else item_bg,
                lambda raw_value, cr=row: view._save_order_fields(
                    cr.get("order") or {},
                    int(cr.get("line_idx") or 0),
                    {"weight_actual_kg": _safe_float(raw_value, None)},
                    rerender=True,
                ),
                tag="orders_fact_weight",
                normal_bg=item_bg,
                h=_row_h,
            )
            ic = [
                _cell_text("", widths[0], item_bg, h=_row_h),
                _cell_text("", widths[1], item_bg, h=_row_h),
                _cell_text("", widths[2], item_bg, h=_row_h),
                _cell_text(
                    f"{inv_plain}/{si + 1:02d}" if inv_plain else f"/{si + 1:02d}",
                    widths[3],
                    item_bg,
                    h=_row_h,
                ),
                _cell_text(
                    f"{zp_plain}/{si + 1:02d}" if zp_plain else f"/{si + 1:02d}",
                    widths[4],
                    item_bg,
                    h=_row_h,
                ),
                _mk_product_cell_cpo(  # noqa: F821
                    row.get("product_name") or "",
                    widths[5],
                    item_bg,
                    lambda value, cr=row: view._save_order_fields(
                        cr.get("order") or {},
                        int(cr.get("line_idx") or 0),
                        {"product_name": value},
                    ),
                    str((row.get("item") or {}).get("product_id") or "").strip(),
                    h=_row_h,
                ),
                _cell_text("", widths[6], item_bg, h=_row_h),
                _cell_text("", widths[7], item_bg, h=_row_h),
                _cell_edit(
                    row.get("item_comment") or "",
                    widths[8],
                    item_bg,
                    lambda value, cr=row: view._save_order_fields(
                        cr.get("order") or {},
                        int(cr.get("line_idx") or 0),
                        {"comment": value},
                    ),
                    max_lines=max(2, _row_h // 16),
                    h=_row_h,
                ),
                _cell_tf(
                    view._fmt_money(_safe_float(row.get("qty"), 0.0)),
                    widths[9],
                    item_bg,
                    lambda raw_value, cr=row: view._save_qty_and_recalc(
                        cr.get("order") or {},
                        int(cr.get("line_idx") or 0),
                        cr,
                        raw_value,
                    ),
                    tag="orders_qty",
                    h=_row_h,
                ),
                _cell_tf(
                    (
                        view._fmt_money(row.get("amount"))
                        if row.get("amount") is not None
                        else ""
                    ),
                    widths[10],
                    item_bg,
                    lambda raw_value, cr=row: view._save_item_amount(
                        cr.get("order") or {},
                        int(cr.get("line_idx") or 0),
                        cr,
                        raw_value,
                    ),
                    tag="orders_amount",
                    h=_row_h,
                ),
                _cell_tf(
                    (
                        view._fmt_money(row.get("full_amount"))
                        if row.get("full_amount") is not None
                        else ""
                    ),
                    widths[11],
                    item_bg,
                    lambda raw_value, cr=row: view._save_item_full_amount(
                        cr.get("order") or {},
                        int(cr.get("line_idx") or 0),
                        cr,
                        raw_value,
                    ),
                    tag="orders_full_amount",
                    h=_row_h,
                ),
                _cell_text(
                    view._fmt_money(_safe_float(row.get("calc_weight"), 0.0), "kg"),
                    widths[12],
                    item_bg,
                    h=_row_h,
                ),
                _fw_cell,
                _cell_text(
                    view._fmt_money(
                        _safe_float(row.get("tariff"), None),
                        row.get("tariff_ccy") or "USD",
                    ),
                    widths[14],
                    item_bg,
                    h=_row_h,
                ),
            ]
            # Build ref-tracked CV cells for in-place update
            _lc_cell = _cell_text(
                view._fmt_money(_safe_float(row.get("log_calc"), None)),
                widths[15],
                item_bg,
                h=_row_h,
            )
            _lf_bg = _ERR_BG if row.get("log_fact") is None else item_bg
            _lf_cell = _cell_text(
                view._fmt_money(_safe_float(row.get("log_fact"), None)),
                widths[16],
                _lf_bg,
                h=_row_h,
            )
            _uc_cell = _cell_text(
                view._fmt_money(
                    view._convert_currency(
                        _safe_float(row.get("unit_cost_usd"), None),
                        str(row.get("unit_cost_currency") or "USD").upper().strip() or "USD",
                        orders_unit_cost_ccy,
                    ),
                    orders_unit_cost_ccy,
                ),
                widths[17],
                item_bg,
                h=_row_h,
            )
            ic.extend([_lc_cell, _lf_cell, _uc_cell])
            tier_i = 18
            pairs = row.get("tier_pairs") or []
            _tier_cell_refs = []
            _tier_all_refs: dict = {}
            for tier, pair in zip(all_tiers, pairs):
                tier_id = str((tier or {}).get("id") or "").strip()
                is_visible = tier_id in visible_ids
                sale = _safe_float(pair.get("sale_price"), None) if is_visible else None
                margin = pair.get("margin") if is_visible else None
                bg_m, fg_m = (
                    view._margin_colors(margin, target_margin)
                    if is_visible
                    else (_PANEL, _TEXT)
                )
                if is_visible:
                    configured_w = widths[tier_i] if tier_i < len(widths) else 0
                    tier_i += 2
                else:
                    configured_w = _all_tier_widths.get(tier_id, 0)
                combo_w = configured_w if is_visible else 0
                _sale_txt = ft.Text(
                    (
                        view._fmt_money(sale, pair.get("sale_ccy") or "USD")
                        if is_visible
                        else ""
                    ),
                    size=9,
                    color=_TEXT,
                    text_align=ft.TextAlign.CENTER,
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                )
                _margin_txt = ft.Text(
                    (
                        ("-" if margin is None else f"{margin * 100:.1f}%")
                        if is_visible
                        else ""
                    ),
                    size=8,
                    color=fg_m,
                    weight="bold",
                    text_align=ft.TextAlign.CENTER,
                    no_wrap=True,
                )
                _tier_c = ft.Container(
                    width=combo_w,
                    height=_row_h if is_visible else 0,
                    bgcolor=bg_m,
                    visible=is_visible,
                    border=ft.border.all(1, _BORDER) if is_visible else None,
                    padding=(
                        ft.padding.symmetric(horizontal=2, vertical=1)
                        if is_visible
                        else None
                    ),
                    content=ft.Column(
                        [_sale_txt, _margin_txt],
                        spacing=1,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        tight=True,
                    ),
                )
                # Store ref for ALL tiers (for surgical toggle)
                _tier_all_refs[tier_id] = (
                    _tier_c,
                    _sale_txt,
                    _margin_txt,
                    configured_w,
                )
                if is_visible:
                    _tier_cell_refs.append((_tier_c, _sale_txt, _margin_txt))
                ic.append(_tier_c)
                ic.append(ft.Container(width=0, height=0, visible=False))
            # Register CV cell refs for in-place update
            _cv_key = (
                f"{str(order_obj.get('id') or '')}:{int(row.get('line_idx') or 0)}"
            )
            if _cv_key.startswith(":"):
                _cv_key = None
            if _cv_key:
                view._order_cv_cell_map[_cv_key] = {
                    "log_calc_cell": _lc_cell,
                    "log_fact_cell": _lf_cell,
                    "unit_cost_cell": _uc_cell,
                    "tier_cells": _tier_cell_refs,
                    "item_bg": item_bg,
                    "fact_weight_cell": _fw_cell,
                }
                view._order_tier_all_cell_refs[_cv_key] = _tier_all_refs
            np_tf = ft.TextField(
                value=view._fmt_money(_safe_float(row.get("new_price"), None)),
                width=widths[-2] - 2,
                height=17,
                text_size=tf_text_size,
                dense=True,
                filled=True,
                fill_color="transparent",
                border=ft.InputBorder.NONE,
                border_width=0,
                border_radius=0,
                content_padding=ft.padding.symmetric(horizontal=4, vertical=0),
                text_align=ft.TextAlign.CENTER,
                text_vertical_align=ft.VerticalAlignment.CENTER,
            )
            nm_text = ft.Text(
                (
                    "-"
                    if row.get("new_margin") is None
                    else f"{float(row.get('new_margin')) * 100:.1f}%"
                ),
                size=8,
                color=_TEXT,
                weight="bold",
            )
            nm_half = ft.Container(
                width=widths[-2],
                height=17,
                bgcolor=_ALT,
                alignment=ft.Alignment(0, 0),
                content=nm_text,
            )
            view._update_margin_cell(
                nm_half, nm_text, row.get("new_margin"), target_margin
            )
            ic_np_outer = ft.Container(
                width=widths[-2],
                height=_row_h,
                bgcolor=nm_half.bgcolor,
                border=ft.border.all(1, _BORDER),
                padding=ft.padding.all(0),
                alignment=ft.Alignment(0, 0),
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
                content=ft.Column(
                    [
                        ft.Container(
                            content=np_tf,
                            bgcolor="transparent",
                            width=widths[-2],
                            height=17,
                            padding=ft.padding.all(0),
                        ),
                        nm_half,
                    ],
                    spacing=0,
                    tight=True,
                ),
            )
            view._bind_commit_handlers(
                np_tf,
                lambda raw_value, cr=row, mc=nm_half, mt=nm_text, tm=target_margin, oc=ic_np_outer: view._save_order_new_price_inline(
                    cr.get("order") or {},
                    int(cr.get("line_idx") or 0),
                    cr,
                    raw_value,
                    mc,
                    mt,
                    tm,
                    oc,
                ),
                tag="orders_new_price",
                error_text="Помилка збереження",
            )
            ic.append(ic_np_outer)
            ic.append(ft.Container(width=0, height=0, visible=False))
            ic = _reorder_base_segment(ic)
            left_col.controls.append(
                ft.Row(
                    ic[:frozen_count],
                    spacing=0,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
            )
            right_col.controls.append(
                ft.Row(
                    ic[frozen_count:],
                    spacing=0,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
            )

    # ── build order groups from flat rows ─────────────────────────────────────
    groups: list[tuple[dict, list]] = []
    idx_by_order: dict[str, int] = {}
    for row in rows:
        order_id = str(row.get("order_id") or "")
        if not order_id:
            order_id = str(id(row.get("order")))
        if order_id not in idx_by_order:
            idx_by_order[order_id] = len(groups)
            groups.append((row.get("order") or {}, []))
        groups[idx_by_order[order_id]][1].append(row)

    # merge group lookup: order_id → gid
    merge_group_by_order: dict[str, str] = {}
    for gid, gdata in (view._orders_merge_groups or {}).items():
        for oid in gdata.get("order_ids") or []:
            merge_group_by_order[oid] = gid

    order_bg = _ALT
    merge_bg = _CHIP_BG  # light blue for combined row
    sub_bg = "#EFF6FF"  # slightly lighter for sub-orders inside merge
    item_bg_1 = "#FEF9C3"
    item_bg_2 = "#FEFCE8"
    rendered_merge_groups: set = set()

    # Auto-expand merged groups and multi-item orders when any filter is active
    # so the user can see the matching products inside without manual expansion.
    _any_filter_active = (
        str(getattr(view, "_orders_filter_status", "all") or "all") != "all"
        or str(getattr(view, "_orders_filter_supplier", "all") or "all") != "all"
        or str(getattr(view, "_orders_filter_category", "all") or "all") != "all"
        or str(getattr(view, "_orders_filter_product", "all") or "all") != "all"
        or str(getattr(view, "_orders_filter_payment", "all") or "all") != "all"
        or bool(str(getattr(view, "_orders_text_query", "") or "").strip())
    )

    render_groups = [(order, items) for order, items in groups if items]
    # "Перший місяць відображення" — ховає лише РЯДКИ старих замовлень з рендеру.
    # rows/лічильники (build_orders_counters) обчислюються з ПОВНОГО rows вище/окремо —
    # цей фільтр торкається лише того, що будується в цю таблицю.
    try:
        _dfm = str((view._column_defaults() or {}).get("display_from_month") or "").strip()
    except Exception:
        _dfm = ""
    if _dfm:
        render_groups = [
            (order, items)
            for order, items in render_groups
            if str((order or {}).get("order_date") or "")[:7] >= _dfm
        ]
    if str(getattr(view, "_orders_text_query", "") or "").strip():
        # A row filter is active — reveal all matches, don't hide any behind
        # the manual "show more" pagination.
        visible_limit = max(1, len(render_groups))
    else:
        visible_limit = max(1, int(getattr(view, "_orders_visible_limit", 30) or 30))
    overflow_count = max(0, len(render_groups) - visible_limit)
    overflow_expanded = bool(getattr(view, "_orders_overflow_expanded", False))
    overflow_toggle_inserted = False

    # ── main render loop ──────────────────────────────────────────────────────
    for gi, (order, items_list) in enumerate(render_groups):
        if overflow_count > 0 and gi == visible_limit and not overflow_toggle_inserted:
            overflow_toggle_inserted = True
            overflow_bg = _ALT_2
            overflow_btn = ft.IconButton(
                icon=ft.Icons.REMOVE if overflow_expanded else ft.Icons.ADD,
                icon_size=14,
                tooltip=(
                    "Згорнути додаткові замовлення"
                    if overflow_expanded
                    else "Показати додаткові замовлення"
                ),
                style=ft.ButtonStyle(
                    padding=ft.padding.all(0), shape=ft.RoundedRectangleBorder(radius=4)
                ),
                width=widths[1],
                height=34,
                on_click=view._guard_ui_action(
                    "orders_overflow_toggle",
                    view._toggle_orders_overflow,
                    "Помилка оновлення списку",
                ),
            )
            overflow_cells = []
            for ci, w in enumerate(widths):
                if ci == 0:
                    overflow_cells.append(_cell_text("", w, overflow_bg))
                elif ci == 1:
                    overflow_cells.append(
                        ft.Container(
                            width=w,
                            height=34,
                            bgcolor=overflow_bg,
                            border=ft.border.all(1, _BORDER),
                            alignment=ft.Alignment(0, 0),
                            content=overflow_btn,
                        )
                    )
                elif ci == 5:
                    overflow_cells.append(
                        _cell_text(
                            f"Ще {overflow_count} замовлень",
                            w,
                            overflow_bg,
                            align=ft.TextAlign.LEFT,
                            bold=True,
                        )
                    )
                else:
                    overflow_cells.append(_cell_text("", w, overflow_bg))
            _append_split_row(overflow_cells)  # toggle row goes into main rows
            view._overflow_btn_ref = overflow_btn
            _ovf["active"] = (
                True  # only AFTER toggle row — subsequent rows go to hidden overflow Column
            )

        order_uid = str(order.get("id") or f"grp_{gi}")

        # ── MERGE GROUP ROW ────────────────────────────────────────────────────
        if order_uid in merge_group_by_order:
            gid = merge_group_by_order[order_uid]
            if gid in rendered_merge_groups:
                continue
            rendered_merge_groups.add(gid)
            _tracking[0] = gid
            _tracking[1] = True

            gdata = view._orders_merge_groups.get(gid) or {}
            group_order_ids = list(gdata.get("order_ids") or [])
            group_sub_orders = [
                groups[idx_by_order[oid]]
                for oid in group_order_ids
                if oid in idx_by_order
            ]
            all_rows = [r for (_, orows) in group_sub_orders for r in orows]
            all_orders = [o for (o, _) in group_sub_orders]

            grp_date = max(
                (
                    str(o.get("order_date") or "")[:10]
                    for o in all_orders
                    if o.get("order_date")
                ),
                default="",
            )
            grp_invoices = [
                str(o.get("invoice_no") or "").strip()
                for o in all_orders
                if o.get("invoice_no")
            ]
            grp_zp1c_list = [
                str(o.get("order_no_1c") or "").strip()
                for o in all_orders
                if o.get("order_no_1c")
            ]
            grp_qty = sum(_safe_float(r.get("qty"), 0.0) or 0.0 for r in all_rows)
            grp_amount = sum(
                (_safe_float(r.get("amount"), 0.0) or 0.0) for r in all_rows
            )
            grp_full = sum(
                (_safe_float(r.get("full_amount"), 0.0) or 0.0) for r in all_rows
            )
            grp_calc_w = sum(
                _safe_float(r.get("calc_weight"), 0.0) or 0.0 for r in all_rows
            )
            grp_has_fact = any(r.get("fact_weight") is not None for r in all_rows)
            grp_fact_w = sum(
                _safe_float(r.get("fact_weight"), 0.0) or 0.0
                for r in all_rows
                if r.get("fact_weight") is not None
            )
            grp_log_calc = sum(
                _safe_float(r.get("log_calc"), 0.0) or 0.0
                for r in all_rows
                if r.get("log_calc") is not None
            )
            grp_tariff = _safe_float(gdata.get("tariff"), None)
            grp_log_fact = _safe_float(gdata.get("log_fact"), None)
            grp_comment = str(gdata.get("comment") or "")
            # Aggregate C/В: weighted average unit cost by qty across all
            # sub-rows. Sub-rows can each be in a different native currency
            # now, so every value is converted into orders_unit_cost_ccy
            # first — averaging raw numbers across mixed currencies would be
            # meaningless.
            _grp_uc_parts = [
                (
                    view._convert_currency(
                        _safe_float(r.get("unit_cost_usd"), None),
                        str(r.get("unit_cost_currency") or "USD").upper().strip() or "USD",
                        orders_unit_cost_ccy,
                    ),
                    _safe_float(r.get("qty"), 0.0) or 0.0,
                )
                for r in all_rows
            ]
            _grp_uc_qty_total = sum(q for uc, q in _grp_uc_parts if uc is not None)
            grp_cv_usd = (
                sum(uc * q for uc, q in _grp_uc_parts if uc is not None)
                / _grp_uc_qty_total
                if _grp_uc_qty_total > 0
                else None
            )
            grp_status = str(
                (all_orders[0] if all_orders else {}).get("status") or "production"
            )
            grp_expanded = (gid in view._expanded_merge_groups) or _any_filter_active
            amount_ccy = (all_rows[0].get("amount_ccy") if all_rows else None) or "USD"

            # Estimate required row height so ALL cells match the tallest wrap cell.
            # Font size 8 ≈ 5px/char; 8px horizontal padding consumed per cell.
            _inv_str = "+".join(grp_invoices)
            _zp_str = "+".join(grp_zp1c_list)
            _cpl3 = max(1, (int(widths[3] or 0) - 8) // 5)
            _cpl4 = max(1, (int(widths[4] or 0) - 8) // 5)
            _lines = max(
                1,
                -(-len(_inv_str) // _cpl3) if _inv_str else 1,
                -(-len(_zp_str) // _cpl4) if _zp_str else 1,
            )
            _grp_h = max(row_height, 7 + _lines * 12 + 7)
            # Long disambiguated product-name labels ("Name | Кат: X | Постач: Y")
            # also need to fit in widths[5] — grow the row for those too.
            _grp_h = _row_height_for(
                (view._common_items_label(all_rows) or "", widths[5]),
                base_h=_grp_h,
            )

            merge_expand_btn = ft.IconButton(
                icon=ft.Icons.REMOVE if grp_expanded else ft.Icons.ADD,
                icon_size=14,
                tooltip=(
                    "Згорнути підзамовлення"
                    if grp_expanded
                    else "Розгорнути підзамовлення"
                ),
                style=ft.ButtonStyle(
                    padding=ft.padding.all(0), shape=ft.RoundedRectangleBorder(radius=4)
                ),
                width=widths[1],
                height=_grp_h,
            )
            _grp_label = (
                view._common_items_label(all_rows)
                or f"{len(group_sub_orders)} Замовлень"
            )
            _grp_label_bg = _SOFT_P if len(group_sub_orders) > 1 else merge_bg
            mc = [
                ft.Container(
                    width=widths[0],
                    height=_grp_h,
                    bgcolor=merge_bg,
                    border=ft.border.all(1, _BORDER),
                    alignment=ft.Alignment(0, 0),
                    content=ft.IconButton(
                        icon=ft.Icons.LINK_OFF,
                        icon_size=12,
                        tooltip="Роз'єднати",
                        style=ft.ButtonStyle(
                            padding=ft.padding.all(0),
                            shape=ft.RoundedRectangleBorder(radius=4),
                        ),
                        width=widths[0],
                        height=_grp_h,
                        on_click=view._guard_ui_action(
                            "merge_unlink",
                            lambda g=gid: view._unmerge_group(g),
                            "Помилка",
                        ),
                    ),
                ),
                ft.Container(
                    width=widths[1],
                    height=_grp_h,
                    bgcolor=merge_bg,
                    border=ft.border.all(1, _BORDER),
                    alignment=ft.Alignment(0, 0),
                    content=merge_expand_btn,
                ),
                _cell_text(grp_date, widths[2], merge_bg, h=_grp_h),
                _cell_text(
                    "+".join(grp_invoices),
                    widths[3],
                    merge_bg,
                    align=ft.TextAlign.CENTER,
                    wrap=True,
                    h=_grp_h,
                ),
                _cell_text(
                    "+".join(grp_zp1c_list),
                    widths[4],
                    merge_bg,
                    align=ft.TextAlign.CENTER,
                    wrap=True,
                    h=_grp_h,
                ),
                _cell_text(
                    _grp_label,
                    widths[5],
                    _grp_label_bg,
                    wrap=True,
                    h=_grp_h,
                ),
                _cell_status_cascade(widths[6], merge_bg, grp_status, gid, h=_grp_h),
                _cell_delivery_cascade(
                    widths[7],
                    merge_bg,
                    all_orders[0] if all_orders else {},
                    gid,
                    h=_grp_h,
                ),
                _cell_edit(
                    grp_comment,
                    widths[8],
                    merge_bg,
                    lambda value, g=gid: view._save_merge_group_field(
                        g, "comment", value
                    ),
                    h=_grp_h,
                ),
                _cell_text(view._fmt_money(grp_qty), widths[9], merge_bg, h=_grp_h),
                _cell_text(
                    view._fmt_money(grp_amount if grp_amount > 0 else None, amount_ccy),
                    widths[10],
                    merge_bg,
                    h=_grp_h,
                ),
                _cell_text(
                    view._fmt_money(grp_full if grp_full > 0 else None, amount_ccy),
                    widths[11],
                    merge_bg,
                    h=_grp_h,
                ),
                _cell_text(
                    view._fmt_money(grp_calc_w, "kg"), widths[12], merge_bg, h=_grp_h
                ),
                _cell_tf(
                    (
                        format_grouped(grp_fact_w, decimals=2, trim=False)
                        if grp_has_fact and grp_fact_w is not None
                        else ""
                    ),
                    widths[13],
                    merge_bg,
                    lambda raw_value, g=gid: view._distribute_merge_fact_weight(
                        g, _safe_float(raw_value, None)
                    ),
                    tag="merge_fact_weight",
                    h=_grp_h,
                ),
                _cell_tf(
                    view._fmt_money(grp_tariff) if grp_tariff is not None else "",
                    widths[14],
                    merge_bg,
                    lambda raw_value, g=gid: view._cascade_merge_tariff(
                        g, _safe_float(raw_value, None)
                    ),
                    tag="merge_tariff",
                    h=_grp_h,
                ),
                _cell_text(
                    view._fmt_money(grp_log_calc if all_rows else None),
                    widths[15],
                    merge_bg,
                    h=_grp_h,
                ),
                _cell_tf(
                    (
                        format_grouped(grp_log_fact, decimals=2, trim=False)
                        if grp_log_fact is not None
                        else ""
                    ),
                    widths[16],
                    merge_bg,
                    lambda raw_value, g=gid: view._distribute_merge_log_fact(
                        g, _safe_float(raw_value, None)
                    ),
                    tag="merge_log_fact",
                    h=_grp_h,
                ),
                _cell_text(
                    (
                        view._fmt_money(
                            # grp_cv_usd was already converted into
                            # orders_unit_cost_ccy while weight-averaging above.
                            grp_cv_usd,
                            orders_unit_cost_ccy,
                        )
                        if grp_cv_usd is not None
                        else "-"
                    ),
                    widths[17],
                    merge_bg,
                    h=_grp_h,
                ),
            ]
            # Store reference to the merged group C/В cell for instant in-place update.
            _grp_cv_cell = mc[-1]
            view._merge_cv_cell_refs[gid] = (_grp_cv_cell, orders_unit_cost_ccy)
            tier_i = 18
            _grp_hdr_tier_all_refs: dict = {}
            for _t in all_tiers:
                _tid = str((_t or {}).get("id") or "").strip()
                _is_vis = _tid in visible_ids
                _configured_w = (
                    widths[tier_i]
                    if (_is_vis and tier_i < len(widths))
                    else _all_tier_widths.get(_tid, 0)
                )
                if _is_vis:
                    tier_i += 2
                _grp_c = _cell_text(
                    "-" if _is_vis else "",
                    _configured_w if _is_vis else 0,
                    merge_bg,
                    h=_grp_h,
                )
                if not _is_vis:
                    _grp_c.height = 0
                    _grp_c.visible = False
                _grp_hdr_tier_all_refs[_tid] = (_grp_c, None, None, _configured_w)
                mc.append(_grp_c)
                mc.append(ft.Container(width=0, height=0, visible=False))
            view._order_tier_all_cell_refs[f"grp_{gid}:0"] = _grp_hdr_tier_all_refs
            mc.append(_cell_text("-", widths[-2], merge_bg, h=_grp_h))
            mc.append(ft.Container(width=0, height=0, visible=False))
            _append_split_row(mc)

            sub_left = ft.Column(spacing=0, visible=grp_expanded)
            sub_right = ft.Column(spacing=0, visible=grp_expanded)

            def _toggle_merge(
                _e=None, g=gid, lc=sub_left, rc=sub_right, btn=merge_expand_btn
            ):
                if g in view._expanded_merge_groups:
                    view._expanded_merge_groups.discard(g)
                else:
                    view._expanded_merge_groups.add(g)
                exp = g in view._expanded_merge_groups
                lc.visible = exp
                rc.visible = exp
                btn.icon = ft.Icons.REMOVE if exp else ft.Icons.ADD
                btn.tooltip = (
                    "Згорнути підзамовлення" if exp else "Розгорнути підзамовлення"
                )
                try:
                    lc.update()
                    rc.update()
                    btn.update()
                except Exception:
                    view._render()

            merge_expand_btn.on_click = view._guard_ui_action(
                "merge_toggle", _toggle_merge, "Помилка"
            )

            for sub_o, sub_items in group_sub_orders:
                sub_order_uid = str(sub_o.get("id") or id(sub_o))
                is_recent_order = gi < visible_limit
                if sub_order_uid in view._collapsed_order_groups:
                    sub_expanded = False
                elif _any_filter_active or sub_order_uid in view._expanded_order_groups:
                    sub_expanded = True
                else:
                    sub_expanded = False
                sub_first = sub_items[0]
                sub_has_items = len(sub_items) > 1
                sub_amount_ccy = sub_first.get("amount_ccy") or "USD"
                sub_tariff = _safe_float(sub_o.get("delivery_price_snapshot"), None)
                sub_fact_total = _safe_float(sub_o.get("cp_logistics_fact_total"), None)
                sub_qty = sum(_safe_float(r.get("qty"), 0.0) or 0.0 for r in sub_items)
                sub_amount = sum(
                    (_safe_float(r.get("amount"), 0.0) or 0.0) for r in sub_items
                )
                sub_full = sum(
                    (_safe_float(r.get("full_amount"), 0.0) or 0.0) for r in sub_items
                )
                sub_calc_w = sum(
                    _safe_float(r.get("calc_weight"), 0.0) or 0.0 for r in sub_items
                )
                sub_has_fact = any(r.get("fact_weight") is not None for r in sub_items)
                sub_fact_w = sum(
                    _safe_float(r.get("fact_weight"), 0.0) or 0.0
                    for r in sub_items
                    if r.get("fact_weight") is not None
                )
                sub_log_calc = sum(
                    _safe_float(r.get("log_calc"), 0.0) or 0.0
                    for r in sub_items
                    if r.get("log_calc") is not None
                )

                sub_expand_btn = None
                if sub_has_items:
                    sub_expand_btn = ft.IconButton(
                        icon=ft.Icons.REMOVE if sub_expanded else ft.Icons.ADD,
                        icon_size=14,
                        tooltip=(
                            "Згорнути товари" if sub_expanded else "Показати товари"
                        ),
                        style=ft.ButtonStyle(
                            padding=ft.padding.all(0),
                            shape=ft.RoundedRectangleBorder(radius=4),
                        ),
                        width=widths[1],
                        height=34,
                    )

                _sub_label = (
                    (view._common_items_label(sub_items) or f"{len(sub_items)} поз.")
                    if sub_has_items
                    else (sub_first.get("product_name") or "")
                )
                _sub_label_bg = _SOFT_P if sub_has_items else sub_bg
                _sub_h = _row_height_for(
                    (_sub_label, widths[5]),
                    (sub_o.get("comment") or "", widths[8]),
                )
                sc = [
                    _cell_text("", widths[0], sub_bg, h=_sub_h),
                    ft.Container(
                        width=widths[1],
                        height=_sub_h,
                        bgcolor=sub_bg,
                        border=ft.border.all(1, _BORDER),
                        alignment=ft.Alignment(0, 0),
                        content=(
                            sub_expand_btn if sub_expand_btn else ft.Text("", size=8)
                        ),
                    ),
                    _cell_date(
                        str(sub_o.get("order_date") or "")[:10],
                        widths[2],
                        sub_bg,
                        sub_o,
                        h=_sub_h,
                    ),
                    _cell_edit(
                        sub_o.get("invoice_no") or "",
                        widths[3],
                        sub_bg,
                        lambda value, o=sub_o: view._save_invoice_with_dup_check(
                            o, value
                        ),
                        h=_sub_h,
                    ),
                    _cell_edit(
                        sub_o.get("order_no_1c") or "",
                        widths[4],
                        sub_bg,
                        lambda value, o=sub_o: view._save_order_fields(
                            o, 0, {}, {"order_no_1c": value}
                        ),
                        h=_sub_h,
                    ),
                    _cell_text(
                        _sub_label,
                        widths[5],
                        _sub_label_bg,
                        max_lines=max(2, _sub_h // 16),
                        wrap=True,
                        h=_sub_h,
                    ),
                    _cell_status(
                        widths[6],
                        sub_bg,
                        str(sub_o.get("status") or "production"),
                        sub_o,
                        h=_sub_h,
                    ),
                    _cell_delivery(widths[7], sub_bg, sub_o, h=_sub_h),
                    _cell_edit(
                        sub_o.get("comment") or "",
                        widths[8],
                        sub_bg,
                        lambda value, o=sub_o: view._save_order_fields(
                            o, 0, {}, {"comment": value}
                        ),
                        max_lines=max(2, _sub_h // 16),
                        h=_sub_h,
                    ),
                    (
                        _cell_tf(
                            view._fmt_money(_safe_float(sub_first.get("qty"), None)),
                            widths[9],
                            sub_bg,
                            lambda raw_value, o=sub_o, r=sub_first: view._save_qty_and_recalc(
                                o, int(r.get("line_idx") or 0), r, raw_value
                            ),
                            tag="sub_qty",
                            h=_sub_h,
                        )
                        if not sub_has_items
                        else _cell_text(
                            view._fmt_money(sub_qty), widths[9], sub_bg, h=_sub_h
                        )
                    ),
                    (
                        _cell_tf(
                            view._fmt_money(sub_amount) if sub_amount > 0 else "",
                            widths[10],
                            sub_bg,
                            lambda raw_value, o=sub_o, r=sub_first: view._save_item_amount(
                                o, int(r.get("line_idx") or 0), r, raw_value
                            ),
                            tag="sub_amount",
                            h=_sub_h,
                        )
                        if not sub_has_items
                        else _cell_text(
                            view._fmt_money(
                                sub_amount if sub_amount > 0 else None, sub_amount_ccy
                            ),
                            widths[10],
                            sub_bg,
                            h=_sub_h,
                        )
                    ),
                    (
                        _cell_tf(
                            view._fmt_money(sub_full) if sub_full > 0 else "",
                            widths[11],
                            sub_bg,
                            lambda raw_value, o=sub_o, r=sub_first: view._save_item_full_amount(
                                o, int(r.get("line_idx") or 0), r, raw_value
                            ),
                            tag="sub_full_amount",
                            h=_sub_h,
                        )
                        if not sub_has_items
                        else _cell_text(
                            view._fmt_money(
                                sub_full if sub_full > 0 else None, sub_amount_ccy
                            ),
                            widths[11],
                            sub_bg,
                            h=_sub_h,
                        )
                    ),
                    _cell_text(
                        view._fmt_money(sub_calc_w, "kg"), widths[12], sub_bg, h=_sub_h
                    ),
                ]
                _sub_fw_cell = None
                if not sub_has_items:
                    _sub_fw_cell = _cell_tf(
                        view._fmt_money(
                            _safe_float(sub_first.get("fact_weight"), None)
                        ),
                        widths[13],
                        (_ERR_BG if sub_first.get("fact_weight") is None else sub_bg),
                        lambda raw_value, o=sub_o, r=sub_first: view._save_order_fields(
                            o,
                            int(r.get("line_idx") or 0),
                            {"weight_actual_kg": _safe_float(raw_value, None)},
                            rerender=True,
                        ),
                        tag="orders_fact_weight",
                        normal_bg=sub_bg,
                        h=_sub_h,
                    )
                    sc.append(_sub_fw_cell)
                    # Store ref for instant distribution update (used by _distribute_merge_fact_weight)
                    if gid and sub_order_uid:
                        view._sub_fw_ct_refs.setdefault(gid, {})[
                            sub_order_uid
                        ] = _sub_fw_cell
                else:
                    sc.append(
                        _cell_text(
                            view._fmt_money(sub_fact_w, "kg") if sub_has_fact else "",
                            widths[13],
                            _ERR_BG if not sub_has_fact else sub_bg,
                            h=_sub_h,
                        )
                    )
                sc.append(
                    _cell_edit(
                        view._fmt_money(sub_tariff) if sub_tariff is not None else "",
                        widths[14],
                        sub_bg,
                        lambda value, o=sub_o: view._save_order_fields(
                            o,
                            0,
                            {},
                            {"delivery_price_snapshot": _safe_float(value, None)},
                            rerender=True,
                        ),
                        align=ft.TextAlign.CENTER,
                        h=_sub_h,
                    )
                )
                _sub_lc_cell = _cell_text(
                    view._fmt_money(sub_log_calc if sub_items else None),
                    widths[15],
                    sub_bg,
                    h=_sub_h,
                )
                sc.append(_sub_lc_cell)
                _sub_lf_ct = _cell_tf(
                    (
                        view._fmt_money(sub_fact_total)
                        if sub_fact_total is not None
                        else ""
                    ),
                    widths[16],
                    _ERR_BG if sub_fact_total is None else sub_bg,
                    lambda raw_value, o=sub_o: view._save_order_fields(
                        o,
                        0,
                        {},
                        {"cp_logistics_fact_total": _safe_float(raw_value, None)},
                        rerender=True,
                    ),
                    tag="orders_fact_total",
                    normal_bg=sub_bg,
                    h=_sub_h,
                )
                sc.append(_sub_lf_ct)
                # Store ref for instant distribution update (used by _distribute_merge_log_fact)
                if gid and sub_order_uid:
                    view._sub_logfact_ct_refs.setdefault(gid, {})[
                        sub_order_uid
                    ] = _sub_lf_ct

                if sub_has_items:
                    _sub_hi_uc_cell = _cell_text("", widths[17], sub_bg, h=_sub_h)
                    sc.append(_sub_hi_uc_cell)
                    tier_i = 18
                    _sub_hi_tier_all_refs: dict = {}
                    for _t in all_tiers:
                        _tid = str((_t or {}).get("id") or "").strip()
                        _is_vis = _tid in visible_ids
                        _configured_w = (
                            widths[tier_i]
                            if (_is_vis and tier_i < len(widths))
                            else _all_tier_widths.get(_tid, 0)
                        )
                        if _is_vis:
                            tier_i += 2
                        _sub_hi_c = _cell_text(
                            "", _configured_w if _is_vis else 0, sub_bg, h=_sub_h
                        )
                        _sub_hi_c.visible = _is_vis
                        if not _is_vis:
                            _sub_hi_c.height = 0
                        _sub_hi_tier_all_refs[_tid] = (
                            _sub_hi_c,
                            None,
                            None,
                            _configured_w,
                        )
                        sc.append(_sub_hi_c)
                        sc.append(ft.Container(width=0, height=0, visible=False))
                    if sub_order_uid:
                        view._order_tier_all_cell_refs[f"{sub_order_uid}:0"] = (
                            _sub_hi_tier_all_refs
                        )
                        view._order_cv_cell_map[f"{sub_order_uid}:0"] = {
                            "log_calc_cell": _sub_lc_cell,
                            "log_fact_cell": _sub_lf_ct,
                            "unit_cost_cell": _sub_hi_uc_cell,
                            "tier_cells": [],
                            "item_bg": sub_bg,
                        }
                    sc.append(_cell_text("", widths[-2], sub_bg, h=_sub_h))
                    sc.append(ft.Container(width=0, height=0, visible=False))
                else:
                    sub_target = view._target_margin(sub_first.get("category_id") or "")
                    _sub_ni_uc_cell = _cell_text(
                        view._fmt_money(
                            view._convert_currency(
                                _safe_float(sub_first.get("unit_cost_usd"), None),
                                str(sub_first.get("unit_cost_currency") or "USD").upper().strip() or "USD",
                                orders_unit_cost_ccy,
                            ),
                            orders_unit_cost_ccy,
                        ),
                        widths[17],
                        sub_bg,
                        h=_sub_h,
                    )
                    sc.append(_sub_ni_uc_cell)
                    tier_i = 18
                    pairs = sub_first.get("tier_pairs") or []
                    _sub_ni_tier_all_refs: dict = {}
                    _sub_ni_vis_tier_refs: list = []
                    for tier, pair in zip(all_tiers, pairs):
                        tier_id = str((tier or {}).get("id") or "").strip()
                        is_visible = tier_id in visible_ids
                        sale = (
                            _safe_float(pair.get("sale_price"), None)
                            if is_visible
                            else None
                        )
                        margin = pair.get("margin") if is_visible else None
                        bg_m, fg_m = (
                            view._margin_colors(margin, sub_target)
                            if is_visible
                            else (_PANEL, _TEXT)
                        )
                        if is_visible:
                            configured_w = widths[tier_i] if tier_i < len(widths) else 0
                            tier_i += 2
                        else:
                            configured_w = _all_tier_widths.get(tier_id, 0)
                        combo_w = configured_w if is_visible else 0
                        _sub_sale_txt = ft.Text(
                            (
                                view._fmt_money(sale, pair.get("sale_ccy") or "USD")
                                if is_visible
                                else ""
                            ),
                            size=9,
                            color=_TEXT,
                            text_align=ft.TextAlign.CENTER,
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        )
                        _sub_margin_txt = ft.Text(
                            (
                                ("-" if margin is None else f"{margin * 100:.1f}%")
                                if is_visible
                                else ""
                            ),
                            size=8,
                            color=fg_m,
                            weight="bold",
                            text_align=ft.TextAlign.CENTER,
                            no_wrap=True,
                        )
                        _sub_tier_c = ft.Container(
                            width=combo_w,
                            height=_sub_h if is_visible else 0,
                            visible=is_visible,
                            bgcolor=bg_m,
                            border=ft.border.all(1, _BORDER) if is_visible else None,
                            padding=(
                                ft.padding.symmetric(horizontal=2, vertical=1)
                                if is_visible
                                else None
                            ),
                            content=ft.Column(
                                [_sub_sale_txt, _sub_margin_txt],
                                spacing=1,
                                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                tight=True,
                            ),
                        )
                        _sub_ni_tier_all_refs[tier_id] = (
                            _sub_tier_c,
                            _sub_sale_txt,
                            _sub_margin_txt,
                            configured_w,
                        )
                        if is_visible:
                            _sub_ni_vis_tier_refs.append(
                                (_sub_tier_c, _sub_sale_txt, _sub_margin_txt)
                            )
                        sc.append(_sub_tier_c)
                        sc.append(ft.Container(width=0, height=0, visible=False))
                    if sub_order_uid:
                        view._order_tier_all_cell_refs[f"{sub_order_uid}:0"] = (
                            _sub_ni_tier_all_refs
                        )
                        view._order_cv_cell_map[f"{sub_order_uid}:0"] = {
                            "log_calc_cell": _sub_lc_cell,
                            "log_fact_cell": _sub_lf_ct,
                            "unit_cost_cell": _sub_ni_uc_cell,
                            "tier_cells": _sub_ni_vis_tier_refs,
                            "item_bg": sub_bg,
                            "fact_weight_cell": _sub_fw_cell,
                        }
                        if gid and sub_order_uid:
                            view._sub_cv_ct_refs.setdefault(gid, {})[sub_order_uid] = (
                                _sub_ni_uc_cell,
                                orders_unit_cost_ccy,
                            )
                    sub_np_tf = ft.TextField(
                        value=view._fmt_money(
                            _safe_float(sub_first.get("new_price"), None)
                        ),
                        width=widths[-2] - 2,
                        height=17,
                        text_size=tf_text_size,
                        dense=True,
                        filled=True,
                        fill_color="transparent",
                        border=ft.InputBorder.NONE,
                        border_width=0,
                        border_radius=0,
                        content_padding=ft.padding.symmetric(horizontal=4, vertical=0),
                        text_align=ft.TextAlign.CENTER,
                        text_vertical_align=ft.VerticalAlignment.CENTER,
                    )
                    sub_nm_text = ft.Text(
                        (
                            "-"
                            if sub_first.get("new_margin") is None
                            else f"{float(sub_first.get('new_margin')) * 100:.1f}%"
                        ),
                        size=8,
                        color=_TEXT,
                        weight="bold",
                    )
                    sub_nm_half = ft.Container(
                        width=widths[-2],
                        height=17,
                        bgcolor=_ALT,
                        alignment=ft.Alignment(0, 0),
                        content=sub_nm_text,
                    )
                    view._update_margin_cell(
                        sub_nm_half,
                        sub_nm_text,
                        sub_first.get("new_margin"),
                        sub_target,
                    )
                    sc_np_outer = ft.Container(
                        width=widths[-2],
                        height=_sub_h,
                        bgcolor=sub_nm_half.bgcolor,
                        border=ft.border.all(1, _BORDER),
                        padding=ft.padding.all(0),
                        alignment=ft.Alignment(0, 0),
                        clip_behavior=ft.ClipBehavior.HARD_EDGE,
                        content=ft.Column(
                            [
                                ft.Container(
                                    content=sub_np_tf,
                                    bgcolor="transparent",
                                    width=widths[-2],
                                    height=17,
                                    padding=ft.padding.all(0),
                                ),
                                sub_nm_half,
                            ],
                            spacing=0,
                            tight=True,
                        ),
                    )
                    view._bind_commit_handlers(
                        sub_np_tf,
                        lambda raw_value, o=sub_o, r=sub_first, mc=sub_nm_half, mt=sub_nm_text, tm=sub_target, oc=sc_np_outer: view._save_order_new_price_inline(
                            o, int(r.get("line_idx") or 0), r, raw_value, mc, mt, tm, oc
                        ),
                        tag="orders_new_price_single",
                        error_text="Помилка збереження",
                    )
                    sc.append(sc_np_outer)
                    sc.append(ft.Container(width=0, height=0, visible=False))

                sc = _reorder_base_segment(sc)
                sub_left.controls.append(
                    ft.Row(
                        sc[:frozen_count],
                        spacing=0,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )
                sub_right.controls.append(
                    ft.Row(
                        sc[frozen_count:],
                        spacing=0,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )

                if sub_has_items and sub_expand_btn is not None:
                    sil = ft.Column(spacing=0, visible=sub_expanded)
                    sir = ft.Column(spacing=0, visible=sub_expanded)

                    def _toggle_sub_items(
                        _e=None,
                        oid=sub_order_uid,
                        lc=sil,
                        rc=sir,
                        btn=sub_expand_btn,
                        is_recent=is_recent_order,
                    ):
                        # Determine new state based on explicit sets
                        if oid in view._collapsed_order_groups:
                            # It was explicitly collapsed, now expand it
                            view._collapsed_order_groups.discard(oid)
                        elif oid in view._expanded_order_groups:
                            # It was explicitly expanded, collapse it
                            view._expanded_order_groups.discard(oid)
                            if is_recent:
                                # For recent orders, mark as explicitly collapsed
                                view._collapsed_order_groups.add(oid)
                        else:
                            # Default state - toggle based on recency
                            if is_recent:
                                # Was expanded by default, now collapse it explicitly
                                view._collapsed_order_groups.add(oid)
                            else:
                                # Was collapsed by default, now expand it explicitly
                                view._expanded_order_groups.add(oid)

                        # Recalculate expanded state
                        if oid in view._collapsed_order_groups:
                            exp = False
                        elif oid in view._expanded_order_groups:
                            exp = True
                        else:
                            exp = is_recent

                        lc.visible = exp
                        rc.visible = exp
                        btn.icon = ft.Icons.REMOVE if exp else ft.Icons.ADD
                        btn.tooltip = "Згорнути товари" if exp else "Показати товари"
                        try:
                            lc.update()
                            rc.update()
                            btn.update()
                        except Exception:
                            view._render()

                    sub_expand_btn.on_click = view._guard_ui_action(
                        "sub_toggle_items", _toggle_sub_items, "Помилка"
                    )
                    _render_item_rows(sub_o, sub_items, item_bg_1, item_bg_2, sil, sir)
                    sub_left.controls.append(sil)
                    sub_right.controls.append(sir)

            _append_detail_cols(sub_left, sub_right)
            if not grp_expanded:
                view._collapsed_cols_to_presync.append((sub_left, sub_right))
            _tracking[0] = None
            _tracking[1] = False

        else:
            # ── STANDALONE ORDER ───────────────────────────────────────────────
            _tracking[0] = order_uid
            _tracking[1] = False
            is_recent_order = gi < visible_limit
            if order_uid in view._collapsed_order_groups:
                expanded = False
            elif _any_filter_active or order_uid in view._expanded_order_groups:
                expanded = True
            else:
                expanded = False
            first = items_list[0]
            item_count = len(items_list)
            has_suborders = item_count > 1
            amount_ccy = first.get("amount_ccy") or "USD"
            tariff = _safe_float(order.get("delivery_price_snapshot"), None)
            fact_total = _safe_float(order.get("cp_logistics_fact_total"), None)
            total_qty = sum(_safe_float(r.get("qty"), 0.0) or 0.0 for r in items_list)
            total_amount = sum(
                (_safe_float(r.get("amount"), 0.0) or 0.0) for r in items_list
            )
            total_full = sum(
                (_safe_float(r.get("full_amount"), 0.0) or 0.0) for r in items_list
            )
            has_fact = any(r.get("fact_weight") is not None for r in items_list)
            total_calc_w = sum(
                _safe_float(r.get("calc_weight"), 0.0) or 0.0 for r in items_list
            )
            total_fact_w = sum(
                _safe_float(r.get("fact_weight"), 0.0) or 0.0
                for r in items_list
                if r.get("fact_weight") is not None
            )
            total_log_calc = sum(
                _safe_float(r.get("log_calc"), 0.0) or 0.0
                for r in items_list
                if r.get("log_calc") is not None
            )

            lines_expand_btn = None
            if has_suborders:
                lines_expand_btn = ft.IconButton(
                    icon=ft.Icons.REMOVE if expanded else ft.Icons.ADD,
                    icon_size=14,
                    tooltip=(
                        "Згорнути рядки товарів"
                        if expanded
                        else "Показати рядки товарів"
                    ),
                    style=ft.ButtonStyle(
                        padding=ft.padding.all(0),
                        shape=ft.RoundedRectangleBorder(radius=4),
                    ),
                    width=widths[1],
                    height=34,
                )

            is_sel = order_uid in (view._orders_selected or set())
            cb = ft.Checkbox(
                value=is_sel,
                on_change=view._guard_ui_action(
                    "orders_checkbox",
                    lambda _e=None, oid=order_uid: view._toggle_order_selection(oid),
                    "Помилка",
                ),
            )

            _order_label = (
                (view._common_items_label(items_list) or f"{item_count} поз.")
                if has_suborders
                else (first.get("product_name") or "")
            )
            _order_label_bg = _SOFT_P if has_suborders else order_bg
            _header_pid = (
                str((first.get("item") or {}).get("product_id") or "").strip()
                if not has_suborders
                else ""
            )
            _order_h = _row_height_for(
                (_order_label, widths[5]),
                (order.get("comment") or "", widths[8]),
            )
            order_cells = [
                ft.Container(
                    width=widths[0],
                    height=_order_h,
                    bgcolor=order_bg,
                    border=ft.border.all(1, _BORDER),
                    alignment=ft.Alignment(0, 0),
                    content=cb,
                ),
                ft.Container(
                    width=widths[1],
                    height=_order_h,
                    bgcolor=order_bg,
                    border=ft.border.all(1, _BORDER),
                    alignment=ft.Alignment(0, 0),
                    content=(
                        lines_expand_btn
                        if lines_expand_btn is not None
                        else ft.Text("", size=8)
                    ),
                ),
                _cell_date(
                    str(order.get("order_date") or "")[:10],
                    widths[2],
                    order_bg,
                    order,
                    h=_order_h,
                ),
                _cell_edit(
                    order.get("invoice_no") or "",
                    widths[3],
                    order_bg,
                    lambda value, o=order: view._save_invoice_with_dup_check(o, value),
                    h=_order_h,
                ),
                _cell_edit(
                    order.get("order_no_1c") or "",
                    widths[4],
                    order_bg,
                    lambda value, o=order: view._save_order_fields(
                        o, 0, {}, {"order_no_1c": value}
                    ),
                    h=_order_h,
                ),
                _mk_order_label_cell(
                    _order_label, widths[5], _order_label_bg, _header_pid, h=_order_h
                ),
                _cell_status(
                    widths[6],
                    order_bg,
                    str(order.get("status") or "production"),
                    order,
                    h=_order_h,
                ),
                _cell_delivery(widths[7], order_bg, order, h=_order_h),
                _cell_edit(
                    order.get("comment") or "",
                    widths[8],
                    order_bg,
                    lambda value, o=order: view._save_order_fields(
                        o, 0, {}, {"comment": value}
                    ),
                    max_lines=max(2, _order_h // 16),
                    h=_order_h,
                ),
                (
                    _cell_tf(
                        view._fmt_money(_safe_float(first.get("qty"), None)),
                        widths[9],
                        order_bg,
                        lambda raw_value, o=order, r=first: view._save_qty_and_recalc(
                            o, int(r.get("line_idx") or 0), r, raw_value
                        ),
                        tag="orders_qty",
                        h=_order_h,
                    )
                    if not has_suborders
                    else _cell_text(
                        view._fmt_money(total_qty), widths[9], order_bg, h=_order_h
                    )
                ),
                (
                    _cell_tf(
                        view._fmt_money(total_amount) if total_amount > 0 else "",
                        widths[10],
                        order_bg,
                        lambda raw_value, o=order, r=first: view._save_item_amount(
                            o, int(r.get("line_idx") or 0), r, raw_value
                        ),
                        tag="orders_amount",
                        h=_order_h,
                    )
                    if not has_suborders
                    else _cell_text(
                        view._fmt_money(
                            total_amount if total_amount > 0 else None, amount_ccy
                        ),
                        widths[10],
                        order_bg,
                        h=_order_h,
                    )
                ),
                (
                    _cell_tf(
                        view._fmt_money(total_full) if total_full > 0 else "",
                        widths[11],
                        order_bg,
                        lambda raw_value, o=order, r=first: view._save_item_full_amount(
                            o, int(r.get("line_idx") or 0), r, raw_value
                        ),
                        tag="orders_full_amount",
                        h=_order_h,
                    )
                    if not has_suborders
                    else _cell_text(
                        view._fmt_money(
                            total_full if total_full > 0 else None, amount_ccy
                        ),
                        widths[11],
                        order_bg,
                        h=_order_h,
                    )
                ),
                _cell_text(
                    view._fmt_money(total_calc_w, "kg"),
                    widths[12],
                    order_bg,
                    h=_order_h,
                ),  # always visible
            ]
            # fact weight: editable for single-item, aggregate text for multi-item
            _main_fw_cell = None
            if not has_suborders:
                _main_fw_cell = _cell_tf(
                    (
                        format_grouped(
                            _safe_float(first.get("fact_weight"), None),
                            decimals=2,
                            trim=False,
                        )
                        if first.get("fact_weight") is not None
                        else ""
                    ),
                    widths[13],
                    _ERR_BG if first.get("fact_weight") is None else order_bg,
                    lambda raw_value, o=order, r=first: view._save_order_fields(
                        o,
                        int(r.get("line_idx") or 0),
                        {"weight_actual_kg": _safe_float(raw_value, None)},
                        rerender=True,
                    ),
                    tag="orders_fact_weight",
                    normal_bg=order_bg,
                    h=_order_h,
                )
                order_cells.append(_main_fw_cell)
            else:
                order_cells.append(
                    _cell_text(
                        view._fmt_money(total_fact_w, "kg") if has_fact else "",
                        widths[13],
                        _ERR_BG if not has_fact else order_bg,
                        h=_order_h,
                    )
                )

            order_cells.append(
                _cell_edit(
                    view._fmt_money(tariff) if tariff is not None else "",
                    widths[14],
                    order_bg,
                    lambda value, o=order: view._save_order_fields(
                        o,
                        0,
                        {},
                        {"delivery_price_snapshot": _safe_float(value, None)},
                        rerender=True,
                    ),
                    align=ft.TextAlign.CENTER,
                    h=_order_h,
                )
            )
            _main_lc_cell = _cell_text(
                view._fmt_money(total_log_calc if items_list else None),
                widths[15],
                order_bg,
                h=_order_h,
            )
            order_cells.append(_main_lc_cell)
            _main_lf_cell = _cell_tf(
                (
                    format_grouped(fact_total, decimals=2, trim=False)
                    if fact_total is not None
                    else ""
                ),
                widths[16],
                _ERR_BG if fact_total is None else order_bg,
                lambda raw_value, o=order: view._save_order_fields(
                    o,
                    0,
                    {},
                    {"cp_logistics_fact_total": _safe_float(raw_value, None)},
                    rerender=True,
                ),
                tag="orders_fact_total",
                normal_bg=order_bg,
                h=_order_h,
            )
            order_cells.append(_main_lf_cell)

            if has_suborders:
                order_cells.append(_cell_text("", widths[17], order_bg, h=_order_h))
                tier_i = 18
                _has_sub_tier_all_refs: dict = {}
                for _t in all_tiers:
                    _tid = str((_t or {}).get("id") or "").strip()
                    _is_vis = _tid in visible_ids
                    _configured_w = (
                        widths[tier_i]
                        if (_is_vis and tier_i < len(widths))
                        else _all_tier_widths.get(_tid, 0)
                    )
                    if _is_vis:
                        tier_i += 2
                    _hs_c = _cell_text(
                        "", _configured_w if _is_vis else 0, order_bg, h=_order_h
                    )
                    if not _is_vis:
                        _hs_c.height = 0
                        _hs_c.visible = False
                    _has_sub_tier_all_refs[_tid] = (_hs_c, None, None, _configured_w)
                    order_cells.append(_hs_c)
                    order_cells.append(ft.Container(width=0, height=0, visible=False))
                _has_sub_oid = str(order.get("id") or "").strip()
                if _has_sub_oid:
                    view._order_tier_all_cell_refs[f"{_has_sub_oid}:0"] = (
                        _has_sub_tier_all_refs
                    )
                order_cells.append(_cell_text("", widths[-2], order_bg, h=_order_h))
                order_cells.append(ft.Container(width=0, height=0, visible=False))
            else:
                target_margin = view._target_margin(first.get("category_id") or "")
                _main_uc_cell = _cell_text(
                    view._fmt_money(
                        view._convert_currency(
                            _safe_float(first.get("unit_cost_usd"), None),
                            str(first.get("unit_cost_currency") or "USD").upper().strip() or "USD",
                            orders_unit_cost_ccy,
                        ),
                        orders_unit_cost_ccy,
                    ),
                    widths[17],
                    order_bg,
                    h=_order_h,
                )
                order_cells.append(_main_uc_cell)
                tier_i = 18
                pairs = first.get("tier_pairs") or []
                _main_tier_refs = []
                _main_tier_all_refs: dict = {}
                for tier, pair in zip(all_tiers, pairs):
                    tier_id = str((tier or {}).get("id") or "").strip()
                    is_visible = tier_id in visible_ids
                    sale = (
                        _safe_float(pair.get("sale_price"), None)
                        if is_visible
                        else None
                    )
                    margin = pair.get("margin") if is_visible else None
                    bg_m, fg_m = (
                        view._margin_colors(margin, target_margin)
                        if is_visible
                        else (_PANEL, _TEXT)
                    )
                    if is_visible:
                        configured_w = widths[tier_i] if tier_i < len(widths) else 0
                        tier_i += 2
                    else:
                        configured_w = _all_tier_widths.get(tier_id, 0)
                    combo_w = configured_w if is_visible else 0
                    _m_sale_txt = ft.Text(
                        (
                            view._fmt_money(sale, pair.get("sale_ccy") or "USD")
                            if is_visible
                            else ""
                        ),
                        size=9,
                        color=_TEXT,
                        text_align=ft.TextAlign.CENTER,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    )
                    _m_margin_txt = ft.Text(
                        (
                            ("-" if margin is None else f"{margin * 100:.1f}%")
                            if is_visible
                            else ""
                        ),
                        size=8,
                        color=fg_m,
                        weight="bold",
                        text_align=ft.TextAlign.CENTER,
                        no_wrap=True,
                    )
                    _m_tier_c = ft.Container(
                        width=combo_w,
                        height=_order_h if is_visible else 0,
                        visible=is_visible,
                        bgcolor=bg_m,
                        border=ft.border.all(1, _BORDER) if is_visible else None,
                        padding=(
                            ft.padding.symmetric(horizontal=2, vertical=1)
                            if is_visible
                            else None
                        ),
                        content=ft.Column(
                            [_m_sale_txt, _m_margin_txt],
                            spacing=1,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            tight=True,
                        ),
                    )
                    _main_tier_all_refs[tier_id] = (
                        _m_tier_c,
                        _m_sale_txt,
                        _m_margin_txt,
                        configured_w,
                    )
                    if is_visible:
                        _main_tier_refs.append((_m_tier_c, _m_sale_txt, _m_margin_txt))
                    order_cells.append(_m_tier_c)
                    order_cells.append(ft.Container(width=0, height=0, visible=False))
                # Register CV refs for single-item main order row
                _main_oid = str(order.get("id") or "").strip()
                if _main_oid:
                    _cv_key_main = f"{_main_oid}:0"
                    view._order_cv_cell_map[_cv_key_main] = {
                        "log_calc_cell": _main_lc_cell,
                        "log_fact_cell": None,
                        "unit_cost_cell": _main_uc_cell,
                        "tier_cells": _main_tier_refs,
                        "item_bg": order_bg,
                        "fact_weight_cell": _main_fw_cell,
                        "main_lf_bg_cell": _main_lf_cell,
                    }
                    view._order_tier_all_cell_refs[_cv_key_main] = _main_tier_all_refs
                new_price_tf = ft.TextField(
                    value=view._fmt_money(_safe_float(first.get("new_price"), None)),
                    width=widths[-2] - 2,
                    height=17,
                    text_size=tf_text_size,
                    dense=True,
                    filled=True,
                    fill_color="transparent",
                    border=ft.InputBorder.NONE,
                    border_width=0,
                    border_radius=0,
                    content_padding=ft.padding.symmetric(horizontal=4, vertical=0),
                    text_align=ft.TextAlign.CENTER,
                    text_vertical_align=ft.VerticalAlignment.CENTER,
                )
                new_margin_text = ft.Text(
                    (
                        "-"
                        if first.get("new_margin") is None
                        else f"{float(first.get('new_margin')) * 100:.1f}%"
                    ),
                    size=8,
                    color=_TEXT,
                    weight="bold",
                )
                new_margin_half = ft.Container(
                    width=widths[-2],
                    height=17,
                    bgcolor=_ALT,
                    alignment=ft.Alignment(0, 0),
                    content=new_margin_text,
                )
                view._update_margin_cell(
                    new_margin_half,
                    new_margin_text,
                    first.get("new_margin"),
                    target_margin,
                )
                order_np_outer = ft.Container(
                    width=widths[-2],
                    height=_order_h,
                    bgcolor=new_margin_half.bgcolor,
                    border=ft.border.all(1, _BORDER),
                    padding=ft.padding.all(0),
                    alignment=ft.Alignment(0, 0),
                    clip_behavior=ft.ClipBehavior.HARD_EDGE,
                    content=ft.Column(
                        [
                            ft.Container(
                                content=new_price_tf,
                                bgcolor="transparent",
                                width=widths[-2],
                                height=17,
                                padding=ft.padding.all(0),
                            ),
                            new_margin_half,
                        ],
                        spacing=0,
                        tight=True,
                    ),
                )
                view._bind_commit_handlers(
                    new_price_tf,
                    lambda raw_value, current_row=first, margin_cell=new_margin_half, margin_text=new_margin_text, tm=target_margin, oc=order_np_outer: view._save_order_new_price_inline(
                        current_row.get("order") or {},
                        int(current_row.get("line_idx") or 0),
                        current_row,
                        raw_value,
                        margin_cell,
                        margin_text,
                        tm,
                        oc,
                    ),
                    tag="orders_new_price_single",
                    error_text="Помилка збереження",
                )
                order_cells.append(order_np_outer)
                order_cells.append(ft.Container(width=0, height=0, visible=False))

            _append_split_row(order_cells)

            if lines_expand_btn is not None:
                detail_left_col = ft.Column(spacing=0, visible=expanded)
                detail_right_col = ft.Column(spacing=0, visible=expanded)

                def _toggle_multi_item_rows(
                    _e=None,
                    oid=order_uid,
                    left_col=detail_left_col,
                    right_col=detail_right_col,
                    btn=lines_expand_btn,
                    is_recent=is_recent_order,
                ):
                    # Determine new state based on explicit sets
                    if oid in view._collapsed_order_groups:
                        # It was explicitly collapsed, now expand it
                        view._collapsed_order_groups.discard(oid)
                    elif oid in view._expanded_order_groups:
                        # It was explicitly expanded, collapse it
                        view._expanded_order_groups.discard(oid)
                        if is_recent:
                            # For recent orders, mark as explicitly collapsed
                            view._collapsed_order_groups.add(oid)
                    else:
                        # Default state - toggle based on recency
                        if is_recent:
                            # Was expanded by default, now collapse it explicitly
                            view._collapsed_order_groups.add(oid)
                        else:
                            # Was collapsed by default, now expand it explicitly
                            view._expanded_order_groups.add(oid)

                    # Recalculate expanded state using same logic as rendering
                    if oid in view._collapsed_order_groups:
                        expanded_now = False
                    elif oid in view._expanded_order_groups:
                        expanded_now = True
                    else:
                        expanded_now = is_recent

                    left_col.visible = expanded_now
                    right_col.visible = expanded_now
                    btn.icon = ft.Icons.REMOVE if expanded_now else ft.Icons.ADD
                    btn.tooltip = (
                        "Згорнути рядки товарів"
                        if expanded_now
                        else "Показати рядки товарів"
                    )
                    try:
                        left_col.update()
                        right_col.update()
                        btn.update()
                    except Exception:
                        view._render()

                lines_expand_btn.on_click = view._guard_ui_action(
                    "orders_toggle_rows",
                    _toggle_multi_item_rows,
                    "Помилка оновлення таблиці",
                )
                _render_item_rows(
                    order,
                    items_list,
                    item_bg_1,
                    item_bg_2,
                    detail_left_col,
                    detail_right_col,
                )
                _append_detail_cols(detail_left_col, detail_right_col)
                if not expanded:
                    view._collapsed_cols_to_presync.append(
                        (detail_left_col, detail_right_col)
                    )
            _tracking[0] = None
            _tracking[1] = False

    if len(left_rows) == 1:
        left_rows.append(
            ft.Row(
                [
                    ft.Container(
                        width=frozen_width,
                        height=42,
                        border=ft.border.all(1, _BORDER),
                        alignment=ft.Alignment(0, 0),
                        bgcolor=_PANEL,
                        content=ft.Text(
                            view.t("cost_orders.no_rows", "Немає рядків"),
                            size=11,
                            color=_TEXT,
                        ),
                    )
                ],
                spacing=0,
            )
        )
        right_rows.append(
            ft.Row(
                [
                    ft.Container(
                        width=scroll_width,
                        height=42,
                        border=ft.border.all(1, _BORDER),
                        alignment=ft.Alignment(0, 0),
                        bgcolor=_PANEL,
                        content=ft.Text("", size=11, color=_TEXT),
                    )
                ],
                spacing=0,
            )
        )

    # Main data columns hold only visible order rows — kept small so lc.update()
    # during merge/unmerge traverses ~30 rows instead of 70k+ overflow controls.
    _left_main_col = ft.Column(left_rows, spacing=0)
    _right_main_col = ft.Column(right_rows, spacing=0)
    view._data_left_col_ref = _left_main_col
    view._data_right_col_ref = _right_main_col

    # Overflow column is a SIBLING of the main col, not a child inside it.
    # This isolates overflow from lc.update() diffs during merge/unmerge.
    if _ovf["active"] and _ovf["left"]:
        _ovf_lc = ft.Column(_ovf["left"], spacing=0, visible=overflow_expanded)
        _ovf_rc = ft.Column(_ovf["right"], spacing=0, visible=overflow_expanded)
        view._overflow_left_col_ref = _ovf_lc
        view._overflow_right_col_ref = _ovf_rc
        _left_outer_col = ft.Column([_left_main_col, _ovf_lc], spacing=0)
        _right_outer_col = ft.Column([_right_main_col, _ovf_rc], spacing=0)
    else:
        view._overflow_left_col_ref = None
        view._overflow_right_col_ref = None
        _left_outer_col = _left_main_col
        _right_outer_col = _right_main_col

    return {
        "frozen": ft.Container(width=frozen_width, content=_left_outer_col),
        "scrollable": ft.Container(width=scroll_width, content=_right_outer_col),
        "frozen_width": frozen_width,
        "scroll_width": scroll_width,
        "left_header_row": _left_header_row,
        "right_header_row": _right_header_row,
    }


class CostPriceView:
    CURRENCIES = ["USD", "CNY", "EUR", "UAH", "PLN"]
    STATUS_LABELS = {
        "production": "Виробництво",
        "ready_for_shipment": "Готовий",
        "in_transit": "В дорозі",
        "at_warehouse": "На складі",
        "accepted": "Прийнятий",
    }
    STATUS_BG = {
        "production": _ERR_2,
        "ready_for_shipment": _WARN_2,
        "in_transit": _CHIP_BG,
        "at_warehouse": _OK_BG,
        "accepted": "#065F46",
    }
    STATUS_FG = {
        "production": "#7F1D1D",
        "ready_for_shipment": _WARN_T,
        "in_transit": _CHIP_T,
        "at_warehouse": _OK_T,
        "accepted": _PANEL,
    }
    DELIVERY_PALETTE = [
        _WARN_BG,
        "#FDE68A",
        _ERR_2,
        "#FCA5A5",
        _OK_BG,
        _OK_2,
        _CHIP_BG,
        "#93C5FD",
        "#E9D5FF",
        "#DDD6FE",
    ]
    PAYMENT_LABELS = {
        "unpaid": "Не сплачен.",
        "advance": "Аванс",
        "transferred": "Передано",
        "paid": "Сплач.",
    }

    def __init__(
        self, content_area, user_data, db, active_region=None, quick_filter: str = ""
    ):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.active_region = active_region
        self.quick_filter = quick_filter

        self.company_id = str(self.user_data.get("company_id") or "").strip()
        self.manager_id = str(
            self.user_data.get("active_manager_id") or self.user_data.get("id") or ""
        ).strip()
        try:
            self.packaging_enabled = bool(
                self.db.get_effective_features(self.manager_id, self.company_id).get(
                    "packaging", True
                )
            )
        except Exception:
            self.packaging_enabled = True
        self.t = i18n.bind(str(self.user_data.get("id") or ""))
        self.STATUS_LABELS = {
            k: self.t(f"cost_orders.status.{k}", v)
            for k, v in CostPriceView.STATUS_LABELS.items()
        }
        self.PAYMENT_LABELS = {
            k: self.t(f"orders.payment.{k}", v)
            for k, v in CostPriceView.PAYMENT_LABELS.items()
        }
        self.manager_name = str(
            self.user_data.get("active_manager_name")
            or self.user_data.get("full_name")
            or self.t("cost_orders.default_manager", "Менеджер")
        ).strip()
        # Read-only when viewing a subordinate remotely (no shared ownership).
        self.is_read_only = bool(self.user_data.get("subordinate_view_readonly", False))
        self._general_mode: bool = bool(self.user_data.get("general_mode", False))
        self._general_manager_ids: list[str] = list(
            self.user_data.get("general_manager_ids") or []
        )
        from views.general_mode_mixin import build_general_manager_names

        self._general_manager_names: dict[str, str] = build_general_manager_names(
            self.user_data.get("all_subordinates_flat") or [], self.user_data
        )
        # Pre-build region name→id map once (same approach as orders_view/payments_view)
        # so _resolve_region_id() doesn't fail on a DB call inside _load().
        _raw_regions = (
            self.db.get_company_regions(self.company_id) if self.company_id else []
        ) or []
        self._region_id_by_name_map: dict[str, str] = {
            str(r.get("name") or "").strip(): str(r.get("id") or "").strip()
            for r in _raw_regions
            if isinstance(r, dict) and r.get("id") and r.get("name")
        }

        _som = self.user_data.get("shared_orders_map")
        self._shared_orders_map: dict[str, dict] = (
            _som if isinstance(_som, dict) else {}
        )

        self._region_id = None
        self._orders = []
        self._products = []
        self._packaging = []
        self._suppliers = []
        self._categories = []
        self._price_tiers = []
        self._delivery_methods = []
        self._order_price_inputs_by_key = {}

        self._product_by_id = {}
        self._packaging_by_id = {}
        self._supplier_by_id = {}
        self._category_by_id = {}
        self._node_name_by_id: dict = {}
        self._node_rows_by_id: dict = {}
        self._product_label_map: dict = {}

        self._uah_rates_cache = {}
        self._uah_rates_ts = 0.0

        self._stopped = False
        self._orders_rows = []
        self._orders_filter_status = "all"
        self._orders_filter_supplier = "all"
        self._orders_filter_category = "all"
        self._orders_filter_product = "all"
        self._orders_filter_payment = "all"
        self._quick_filter_mode = ""  # "missing_logistics_fact" | "missing_fact_weight"
        # Apply quick_filter pre-sets from home KPI cards.
        _qf = str(self.quick_filter or "").strip()
        if _qf == "non_production_missing_logistics_fact":
            self._quick_filter_mode = "missing_logistics_fact"
        elif _qf == "missing_fact_weight_non_production":
            self._quick_filter_mode = "missing_fact_weight"

        self._workspace_state = {}
        self._workspace_outbox = {}
        self._workspace_outbox_running = False
        self._workspace_sync_status = "synced"
        self._workspace_sync_error_count = 0
        self._workspace_sync_loop_started = False
        self._ref_data_pulse_ts: int = 0
        self._ops_outbox: dict[str, dict] = {}
        self._ops_flush_running = False
        self._sync_indicator_text = None
        self._sync_indicator_dot = None
        self._sync_indicator_container = None
        self._header_loading_indicator = None
        self._async_build_token = 0
        self._prebuilt_table_content = None
        self._prebuilt_table_rows: list | None = None
        self._orders_selected: set = set()
        self._orders_merge_groups: dict = {}
        self._expanded_merge_groups: set = set()
        self._expanded_order_groups = set()  # for old orders explicitly expanded
        self._collapsed_order_groups = set()  # for recent orders explicitly collapsed
        self._orders_visible_limit = 30
        self._orders_overflow_expanded = False
        self._orders_text_query: str = ""
        self._resize_refresh_state = {"last_w": 0, "pending": False}
        self._table_h_scroll_offset = 0.0
        self._orders_v_scroll_offset = 0.0
        self._render_pending = False
        self._h_scroll_slot_content = None
        self._cv_body_scroll = None
        self._h_scroll_needs = False
        self._h_scroll_step = 300
        self._v_body_col_ref = None
        # In-place cell ref maps: order_id → (bg_container, label_text)
        self._order_status_cell_map: dict[str, tuple] = {}
        self._order_delivery_cell_map: dict[str, tuple] = {}
        # CV cell refs: "order_id:line_idx" → {log_calc_cell, log_fact_cell, unit_cost_cell, tier_cells}
        self._order_cv_cell_map: dict[str, dict] = {}
        # Merged group C/В cell refs: gid → (cv_cell, ccy)
        self._merge_cv_cell_refs: dict[str, tuple] = {}
        self._merge_btn_ref: Optional[ft.Container] = None
        self._body_column_ref: Optional[ft.Column] = None
        self._cv_header_scroll = None
        self._cv_header_stack_child = None
        self._sticky_currency_bar_ref = None
        self._sticky_counters_ref = None
        self._sticky_filter_bar_ref = None
        self._sticky_table_header_ref = None
        self._collapsed_cols_to_presync: list = []
        self._data_left_col_ref: Optional[ft.Column] = None
        self._data_right_col_ref: Optional[ft.Column] = None
        self._order_left_row_refs: dict = {}
        self._order_right_row_refs: dict = {}
        self._group_left_row_refs: dict = {}
        self._group_right_row_refs: dict = {}

    def _app_data_dir(self) -> str:
        flet_storage = os.getenv("FLET_APP_STORAGE_DATA")
        if flet_storage:
            path = flet_storage
        else:
            root = os.environ.get("LOCALAPPDATA") or os.path.join(
                os.path.expanduser("~"), "AppData", "Local"
            )
            path = os.path.join(root, "CatManPro")
        os.makedirs(path, exist_ok=True)
        return path

    def _pg(self):
        if not self.content_area:
            return None
        try:
            return self.content_area.page
        except RuntimeError:
            return None

    def _snack(self, text: str, color: str = _OK_FG):
        pg = self._pg()
        if not pg:
            return
        pg.snack_bar = ft.SnackBar(
            content=ft.Text(text, color=_PANEL, weight="bold"),
            bgcolor=color,
            duration=2500,
        )
        pg.snack_bar.open = True
        pg.update()

    def _persist_and_render(self, success_text: str = "", *, rebuild_rows: bool = False) -> None:
        if success_text:
            self._snack(success_text, "#0EA5E9")
        self._schedule_render(rebuild_rows=rebuild_rows)

    def _show_help(self) -> None:
        page = self._pg()
        if not page:
            return
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(
                self.t(
                    "cost_orders.help.title", "Довідка — Собівартість / Рентабельність"
                )
            ),
            content=ft.Container(
                width=440,
                content=ft.Text(
                    self.t(
                        "cost_orders.help.body",
                        "Тут розраховується собівартість і рентабельність кожного"
                        " замовлення: логістика, вага, тариф і ціни за рівнями."
                        " Чіпи вгорі фільтрують за статусом замовлення. Кнопка"
                        " «Таблиця» — налаштування видимих стовпців; кнопка"
                        " «Калькулятор» — швидкий розрахунок собівартості без"
                        " прив'язки до конкретного замовлення. Можна обрати кілька"
                        " замовлень і об'єднати їх в одне.",
                    ),
                    size=13,
                ),
            ),
            actions=[
                ft.FilledButton(
                    self.t("common.help.close", "Зрозуміло"),
                    on_click=lambda _e: page.close(dlg),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        with contextlib.suppress(Exception):
            page.show_dialog(dlg)

    def _guard_ui_action(self, tag: str, action, error_text: str = "Помилка дії"):
        if self.is_read_only:

            def _ro_handler(_e=None):
                self._snack(
                    self.t(
                        "cost_orders.readonly_notice",
                        "Режим перегляду — редагування заборонено",
                    ),
                    "#64748B",
                )
                return None

            return _ro_handler

        def _handler(_e=None):
            try:
                return action()
            except Exception:
                logger.exception("[cost_price] ui_action_failed | tag=%s", tag)
                self._snack(error_text, _ERR)
                return None

        return _handler

    def _bind_commit_handlers(
        self,
        control: ft.Control,
        commit,
        *,
        tag: str,
        error_text: str = "Помилка збереження",
    ) -> None:
        if self.is_read_only:
            # In read-only mode: disable the control but don't bind save handlers.
            with contextlib.suppress(Exception):
                control.disabled = True
            return

        # Mutable state shared across all closures for this control.
        # Debounce: rapid edits cancel previous pending save; only the last one commits.
        state = {
            "last": str(getattr(control, "value", "") or ""),
            "task": None,
        }

        def _handle(_e=None):
            current_value = str(getattr(control, "value", "") or "")
            if current_value == state["last"]:
                return

            # Cancel any previously scheduled save for this control.
            prev = state["task"]
            if prev is not None:
                with contextlib.suppress(Exception):
                    prev.cancel()
                state["task"] = None

            async def _delayed_commit():
                await asyncio.sleep(0.06)
                live_value = str(getattr(control, "value", "") or "")
                if live_value == state["last"]:
                    state["task"] = None
                    return
                try:
                    result = commit(live_value)
                    # commit() from _cell_tf returns a formatted string for last-value sync.
                    state["last"] = result if isinstance(result, str) else live_value
                except Exception:
                    logger.exception("[cost_price] commit_failed | tag=%s", tag)
                    self._snack(error_text, _ERR)
                finally:
                    state["task"] = None

            try:
                loop = asyncio.get_running_loop()
                state["task"] = loop.create_task(_delayed_commit())
            except RuntimeError:
                # Synchronous fallback when called outside the event loop.
                try:
                    result = commit(current_value)
                    state["last"] = result if isinstance(result, str) else current_value
                except Exception:
                    logger.exception("[cost_price] commit_failed | tag=%s", tag)
                    self._snack(error_text, _ERR)

        control.on_submit = _handle
        control.on_blur = _handle
        # New price should persist even if user switches tabs before blur/Enter.
        if tag in {"orders_new_price", "orders_new_price_single"}:
            control.on_change = _handle

    def _resolve_region_id(self) -> Optional[str]:
        raw = str(self.active_region or "").strip()
        if not raw:
            return None
        # Already an ID — check against pre-built map values
        if raw in self._region_id_by_name_map.values():
            return raw
        # Name lookup via cached map (identical to orders_view._effective_region_id)
        resolved = self._region_id_by_name_map.get(raw)
        if resolved:
            return resolved
        # Fallback: looks like an Appwrite ID (alphanumeric, no dashes)
        if len(raw) >= 20 and "-" not in raw and raw.replace("_", "").isalnum():
            return raw
        return None

    def _workspace_user_id(self) -> str:
        ud = self.user_data or {}
        return str(
            ud.get("id") or ud.get("active_manager_id") or self.manager_id or ""
        ).strip()

    def _load_workspace_state(self):
        user_id = self._workspace_user_id()
        if not user_id:
            self._workspace_state = {}
            return
        local_path = self._workspace_state_file_path()
        if os.path.exists(local_path):
            try:
                import json as _json

                with open(local_path, "r", encoding="utf-8") as f:
                    loaded = _json.load(f)
                if isinstance(loaded, dict):
                    self._workspace_state = loaded
                    return
            except Exception:
                pass
        self._workspace_state = self.db.get_manager_workspace_state(user_id) or {}
        self._save_workspace_state_to_file()

    def _workspace_outbox_file_path(self) -> str:
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        return os.path.join(
            self._app_data_dir(),
            f"cost_price_workspace_outbox_{safe_cid}_{safe_mgr}.json",
        )

    def _workspace_state_file_path(self) -> str:
        uid = (
            str((self.user_data or {}).get("id") or "nouser")
            .replace("/", "_")
            .replace("\\", "_")
        )
        cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        rid = str(self._region_id or "all").replace("/", "_").replace("\\", "_")
        return os.path.join(
            self._app_data_dir(),
            f"cp_orders_workspace_state_{cid}_{uid}_{rid}.json",
        )

    def _save_workspace_state_to_file(self) -> None:
        if not self._workspace_state:
            return
        try:
            import json as _json

            with open(self._workspace_state_file_path(), "w", encoding="utf-8") as f:
                _json.dump(self._workspace_state, f, ensure_ascii=False, default=str)
        except Exception:
            pass

    def _load_workspace_outbox(self) -> None:
        path = self._workspace_outbox_file_path()
        if not os.path.exists(path):
            self._refresh_workspace_sync_indicator_state()
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            payload = raw.get("payload") if isinstance(raw, dict) else None
            if isinstance(payload, dict):
                self._workspace_outbox = dict(payload)
                self._workspace_state = dict(payload)
        except Exception:
            pass
        self._refresh_workspace_sync_indicator_state()

    def _save_workspace_outbox(self) -> None:
        try:
            payload = {
                "payload": (
                    self._workspace_state
                    if isinstance(self._workspace_state, dict)
                    else {}
                ),
                "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
            }
            path = self._workspace_outbox_file_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
            self._workspace_outbox = dict(payload["payload"])
        except Exception:
            self._set_workspace_sync_status("error")

    def _refresh_workspace_sync_indicator_state(self) -> None:
        if self._workspace_outbox_running or self._ops_flush_running:
            self._set_workspace_sync_status("syncing")
            return
        if self._workspace_outbox or self._ops_outbox:
            self._set_workspace_sync_status("pending")
            return
        self._set_workspace_sync_status("synced")

    def _ops_outbox_file_path(self) -> str:
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        return os.path.join(
            self._app_data_dir(),
            f"cost_price_ops_outbox_{safe_cid}_{safe_mgr}.json",
        )

    def _load_ops_outbox(self) -> None:
        path = self._ops_outbox_file_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            items = raw.get("items") if isinstance(raw, dict) else None
            if isinstance(items, dict):
                loaded = {
                    str(k): dict(v) for k, v in items.items() if isinstance(v, dict)
                }
                # Drop stale "order" (full-upsert) entries — they contained full order blobs
                # queued by the old _sync_unit_costs_to_db mechanism. Only keep "patch" and
                # "input" entries which represent real user-initiated changes.
                filtered = {
                    k: v
                    for k, v in loaded.items()
                    if str(v.get("kind") or "") in ("patch", "input")
                }
                dropped = len(loaded) - len(filtered)
                if dropped:
                    logger.warning(
                        "[OUTBOX-LOAD] dropped %d stale 'order' entries, kept %d patch/input entries",
                        dropped,
                        len(filtered),
                    )
                self._ops_outbox = filtered
        except Exception:
            pass

    def _save_ops_outbox(self) -> None:
        try:
            path = self._ops_outbox_file_path()
            if not self._ops_outbox:
                with contextlib.suppress(Exception):
                    if os.path.exists(path):
                        os.remove(path)
                return
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "items": self._ops_outbox,
                        "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
                    },
                    f,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            os.replace(tmp, path)
        except Exception:
            pass

    async def flush_pending_async(self) -> None:
        if self._ops_outbox:
            with contextlib.suppress(Exception):
                await self._flush_ops_async()
        if self._workspace_outbox:
            with contextlib.suppress(Exception):
                await self._flush_workspace_state_async()

    async def _flush_ops_async(self) -> None:
        if self._ops_flush_running or not self._ops_outbox:
            self._refresh_workspace_sync_indicator_state()
            return
        self._ops_flush_running = True
        self._refresh_workspace_sync_indicator_state()
        error_count = 0
        try:
            keys = list(self._ops_outbox.keys())

            async def _flush_one(key: str):
                op = self._ops_outbox.get(key)
                if not isinstance(op, dict):
                    self._ops_outbox.pop(key, None)
                    return True
                kind = str(op.get("kind") or "")
                try:
                    if kind == "input":
                        ok = await asyncio.to_thread(
                            self.db.upsert_cost_price_order_input,
                            op.get("company_id"),
                            op.get("order_id"),
                            op.get("item_idx"),
                            op["data"],
                            region_id=op.get("region_id"),
                            manager_id=op.get("manager_id"),
                        )
                    elif kind == "patch":
                        _oid = str(op.get("order_id") or "").strip()
                        _cid = str(op.get("company_id") or "").strip()
                        _fields = op.get("fields") or {}
                        _items = _fields.get("items") or []
                        _weights = [
                            (_i or {}).get("weight_actual_kg", "?") for _i in _items[:3]
                        ]
                        _fact_log = _fields.get("cp_logistics_fact_total", "?")
                        ok = await asyncio.to_thread(
                            self.db.patch_order_fields,
                            _oid,
                            _cid,
                            _fields,
                        )
                    elif kind == "order":
                        # Legacy full-upsert entries (from old outbox files). Still handle them.
                        _d = op.get("data") or {}
                        _oid = str(_d.get("id") or key).strip()
                        ok = await asyncio.to_thread(
                            self.db.upsert_order,
                            op["data"],
                            op.get("manager_id"),
                        )
                    else:
                        ok = True
                except Exception as _exc:
                    logger.debug("[FLUSH] exception for key=%s: %s", key, _exc)
                    ok = False
                if ok:
                    self._ops_outbox.pop(key, None)
                return ok

            # Sequential flush: one DB write at a time to avoid overwhelming the DB.
            # asyncio.gather (parallel) caused fan spin and UI hangs on large outboxes.
            results = []
            for _k in keys:
                results.append(await _flush_one(_k))
            error_count = sum(
                1 for r in results if r is not True and not (isinstance(r, bool) and r)
            )
            self._save_ops_outbox()
            # After a successful flush the server has incremented the DB pulse.
            # Re-save the local snapshot with the new pulse so the next app
            # startup sees snapshot_pulse == db_pulse and loads from snapshot
            # (preserving any null/cleared values) instead of re-fetching from DB.
            if error_count == 0:
                with contextlib.suppress(Exception):
                    from views.async_view_build_helper import (
                        _PULSE_KEY_PREFIX,
                        _get_orders_pulse,
                    )

                    _new_pulse = await asyncio.to_thread(
                        _get_orders_pulse, self.db, self.company_id
                    )
                    _snap_rid = self._resolve_region_id()
                    # Update in-memory pulse so ensure_orders_cache_fresh skips next reload.
                    _pkey = f"{_PULSE_KEY_PREFIX}{_snap_rid or 'all'}"
                    if _new_pulse:
                        self.user_data[_pkey] = _new_pulse
                        # Tell orders_view (and any other tab) that this pulse
                        # increase is our own session's write — skip full reload.
                        with contextlib.suppress(Exception):
                            self._shared_orders_map["__session_db_pulse__"] = int(
                                _new_pulse
                            )
                    # Log fact fields in in-memory orders after flush
                    for _o in self._orders:
                        if not isinstance(_o, dict):
                            continue
                        _oid2 = str(_o.get("id") or "")[:12]
                        _its = _o.get("items") or []
                        _ws = [
                            (_i or {}).get("weight_actual_kg", "-") for _i in _its[:3]
                        ]
                        _fl = _o.get("cp_logistics_fact_total", "-")
        except Exception:
            error_count += 1
        finally:
            self._ops_flush_running = False
            if error_count > 0:
                self._set_workspace_sync_status("error")
            else:
                self._refresh_workspace_sync_indicator_state()

    def _set_workspace_sync_status(self, status: str, error_count: int = 0) -> None:
        self._workspace_sync_status = str(status or "synced")
        if status == "error":
            self._workspace_sync_error_count = max(
                int(error_count or 0), self._workspace_sync_error_count
            )
        elif status == "synced":
            self._workspace_sync_error_count = 0

        txt = self._sync_indicator_text
        dot = self._sync_indicator_dot
        ctr = self._sync_indicator_container
        if txt is None or dot is None or ctr is None:
            return
        if status == "synced":
            txt.value = self.t("orders.sync.synced", "Збережено")
            txt.color = _OK_FG
            dot.bgcolor = _OK_FG
            ctr.tooltip = self.t(
                "cost_orders.sync.synced_tip",
                "Workspace state вже в БД. Натисніть для примусової синхронізації.",
            )
        elif status == "pending":
            txt.value = self.t("orders.sync.pending", "Локально")
            txt.color = "#B45309"
            dot.bgcolor = _WARN
            ctr.tooltip = self.t(
                "cost_orders.sync.pending_tip",
                "Workspace state збережено локально і ще не передано в БД. Натисніть для синхронізації.",
            )
        elif status == "syncing":
            txt.value = self.t("orders.sync.syncing", "Оновлення...")
            txt.color = _TEXT
            dot.bgcolor = "#3B82F6"
            ctr.tooltip = self.t(
                "cost_orders.sync.syncing_tip", "Йде передача workspace state в БД."
            )
        elif status == "error":
            txt.value = self.t("orders.sync.error", "Не збережено")
            txt.color = _ERR
            dot.bgcolor = _ERR
            ctr.tooltip = self.t(
                "cost_orders.sync.error_tip",
                "Workspace state не вдалося передати в БД. Натисніть для повторної спроби.",
            )
        with contextlib.suppress(Exception):
            ctr.update()

    def _build_workspace_sync_indicator(self) -> ft.Container:
        self._sync_indicator_dot = ft.Container(
            width=8, height=8, border_radius=4, bgcolor=_BORDER
        )
        self._sync_indicator_text = ft.Text("", size=10, color=_TEXT)

        def _on_force_sync(_):
            page = self._pg()
            if page and hasattr(page, "run_task"):
                with contextlib.suppress(Exception):
                    page.run_task(self._force_sync_workspace_state_async)

        self._sync_indicator_container = ft.Container(
            content=ft.Row(
                [self._sync_indicator_dot, self._sync_indicator_text],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                tight=True,
            ),
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            border_radius=6,
            bgcolor=_ALT_2,
            border=ft.border.all(1, _HDR),
            tooltip="Натисніть для збереження в БД",
            on_click=_on_force_sync,
            ink=True,
        )
        self._refresh_workspace_sync_indicator_state()
        return self._sync_indicator_container

    async def _flush_workspace_state_async(self) -> None:
        if self._workspace_outbox_running:
            return
        if not self._workspace_state:
            if self._workspace_outbox:
                self._workspace_outbox = {}
                with contextlib.suppress(Exception):
                    path = self._workspace_outbox_file_path()
                    if os.path.exists(path):
                        os.remove(path)
                self._refresh_workspace_sync_indicator_state()
            return
        self._workspace_outbox_running = True
        self._set_workspace_sync_status("syncing")
        error_count = 0
        try:
            user_id = self._workspace_user_id()
            if not user_id:
                self._set_workspace_sync_status("error")
                return
            snapshot = (
                dict(self._workspace_state)
                if isinstance(self._workspace_state, dict)
                else {}
            )
            ok = await asyncio.to_thread(
                self.db.save_manager_workspace_state, user_id, snapshot
            )
            if ok:
                # Don't overwrite _workspace_state with stale snapshot — keep live edits.
                # Just persist the current in-memory state and clear the outbox.
                self._save_workspace_state_to_file()
                self._workspace_outbox = {}
                with contextlib.suppress(Exception):
                    path = self._workspace_outbox_file_path()
                    if os.path.exists(path):
                        os.remove(path)
            else:
                error_count += 1
        except Exception:
            error_count += 1
        finally:
            self._workspace_outbox_running = False
            if error_count > 0:
                self._set_workspace_sync_status("error", error_count)
            else:
                self._refresh_workspace_sync_indicator_state()

    async def _force_sync_workspace_state_async(self) -> None:
        with contextlib.suppress(Exception):
            await self._flush_workspace_state_async()
        if self._ops_outbox:
            with contextlib.suppress(Exception):
                await self._flush_ops_async()

    async def _workspace_sync_loop(self) -> None:
        while not self._stopped:
            await asyncio.sleep(45)
            if self._stopped:
                return
            pg = self._pg()
            active = str(getattr(pg, "_active_view", "") if pg else "")
            if not pg or active not in ("show_cost_price", "show_cost_price_orders"):
                continue
            if self._workspace_outbox:
                await self._flush_workspace_state_async()
            if self._ops_outbox:
                await self._flush_ops_async()

    def _queue_workspace_state_save(self) -> None:
        self._save_workspace_outbox()
        self._save_workspace_state_to_file()
        self._refresh_workspace_sync_indicator_state()
        # DB sync via 45s loop or "Локально" button — no immediate network call

    def _save_workspace_state(self):
        self._queue_workspace_state_save()

    def _trigger_immediate_sync(self) -> None:
        """Fire-and-forget background DB flush — bypasses the 45s batch loop
        for actions (like currency switches) that should hit the DB right away."""
        page = self._pg()
        if page and hasattr(page, "run_task"):
            with contextlib.suppress(Exception):
                page.run_task(self._force_sync_workspace_state_async)

    def _state_bucket(self) -> dict:
        key = f"cost_profit:{self.company_id}:{self._region_id or 'all'}"
        if not isinstance(self._workspace_state.get("cost_profit"), dict):
            self._workspace_state["cost_profit"] = {}
        bucket = self._workspace_state["cost_profit"].get(key)
        if not isinstance(bucket, dict):
            bucket = {
                "warehouse": {},
                "column_defaults": {
                    "log_calc_ccy": "USD",
                    "log_fact_ccy": "USD",
                    "orders_unit_cost_ccy": "USD",
                    "new_price_ccy": "USD",
                    "orders_visible_tier_ids": [],
                },
            }
            self._workspace_state["cost_profit"][key] = bucket
        return bucket

    def _column_defaults(self) -> dict:
        bucket = self._state_bucket()
        defaults = (
            bucket.get("column_defaults")
            if isinstance(bucket.get("column_defaults"), dict)
            else {}
        )
        normalized = {
            "log_calc_ccy": str(defaults.get("log_calc_ccy") or "USD").upper().strip()
            or "USD",
            "log_fact_ccy": str(defaults.get("log_fact_ccy") or "USD").upper().strip()
            or "USD",
            "orders_unit_cost_ccy": str(defaults.get("orders_unit_cost_ccy") or "USD")
            .upper()
            .strip()
            or "USD",
            "new_price_ccy": str(defaults.get("new_price_ccy") or "USD").upper().strip()
            or "USD",
            "display_from_month": str(defaults.get("display_from_month") or "").strip()[:7],
        }
        visible_raw = defaults.get("orders_visible_tier_ids")
        if isinstance(visible_raw, list):
            normalized["orders_visible_tier_ids"] = [
                str(v).strip() for v in visible_raw if str(v).strip()
            ]
        elif "orders_visible_tier_ids" in defaults:
            normalized["orders_visible_tier_ids"] = []
        visible_base_raw = defaults.get("orders_visible_base_column_ids")
        if isinstance(visible_base_raw, list):
            normalized["orders_visible_base_column_ids"] = [
                str(v).strip() for v in visible_base_raw if str(v).strip()
            ]
        elif "orders_visible_base_column_ids" in defaults:
            normalized["orders_visible_base_column_ids"] = []
        order_raw = defaults.get("orders_base_column_order_ids")
        if isinstance(order_raw, list):
            normalized["orders_base_column_order_ids"] = [
                str(v).strip() for v in order_raw if str(v).strip()
            ]
        elif "orders_base_column_order_ids" in defaults:
            normalized["orders_base_column_order_ids"] = []
        widths_raw = defaults.get("orders_base_column_widths")
        if isinstance(widths_raw, dict):
            normalized_widths = {}
            for key, value in widths_raw.items():
                key_text = str(key).strip()
                if not key_text:
                    continue
                try:
                    normalized_widths[key_text] = max(0, int(float(value)))
                except Exception:
                    continue
            normalized["orders_base_column_widths"] = normalized_widths
        elif "orders_base_column_widths" in defaults:
            normalized["orders_base_column_widths"] = {}
        tier_widths_raw = defaults.get("orders_tier_widths")
        if isinstance(tier_widths_raw, dict):
            normalized["orders_tier_widths"] = tier_widths_raw
        elif "orders_tier_widths" in defaults:
            normalized["orders_tier_widths"] = {}
        if defaults != normalized:
            bucket["column_defaults"] = normalized
        return normalized

    def _base_column_default_widths(self) -> dict[str, int]:
        return {
            "select": 24,
            "expand": 30,
            "date": 70,
            "invoice": 72,
            "order1c": 72,
            "product": 130,
            "status": 72,
            "delivery": 90,
            "comment": 120,
            "qty": 56,
            "amount": 72,
            "full_amount": 84,
            "calc_weight": 62,
            "fact_weight": 62,
            "tariff": 66,
            "log_calc": 74,
            "log_fact": 74,
            "unit_cost": 70,
            "new_price": 50,
            "new_margin": 50,
        }

    def _patch_column_defaults(self, patch: dict) -> None:
        if not isinstance(patch, dict) or not patch:
            return
        bucket = self._state_bucket()
        defaults = (
            bucket.get("column_defaults")
            if isinstance(bucket.get("column_defaults"), dict)
            else {}
        )
        merged_defaults = dict(defaults)
        for key, value in patch.items():
            merged_defaults[key] = value
        bucket["column_defaults"] = merged_defaults
        self._save_workspace_state_to_file()
        self._queue_workspace_state_save()

    def _all_orders_base_column_defs(self) -> list[dict]:
        defaults = self._column_defaults()
        widths = defaults.get("orders_base_column_widths")
        if not isinstance(widths, dict):
            widths = {}
        default_widths = self._base_column_default_widths()
        logistics_ccy = (
            str(defaults.get("log_calc_ccy") or "USD").upper().strip() or "USD"
        )
        orders_unit_cost_ccy = (
            str(defaults.get("orders_unit_cost_ccy") or "USD").upper().strip() or "USD"
        )
        new_price_ccy = (
            str(defaults.get("new_price_ccy") or "USD").upper().strip() or "USD"
        )
        return [
            {
                "key": "date",
                "label": self.t("cost_orders.col.date", "Дата"),
                "width": int(widths.get("date") or default_widths["date"]),
            },
            {
                "key": "invoice",
                "label": self.t("cost_orders.col.invoice", "Інвойс"),
                "width": int(widths.get("invoice") or default_widths["invoice"]),
            },
            {
                "key": "order1c",
                "label": self.t("cost_orders.col.order1c", "ЗП обліку"),
                "width": int(widths.get("order1c") or default_widths["order1c"]),
            },
            {
                "key": "product",
                "label": self.t("cost_orders.col.product", "Товар"),
                "width": int(widths.get("product") or default_widths["product"]),
            },
            {
                "key": "status",
                "label": self.t("cost_orders.col.status", "Статус"),
                "width": int(widths.get("status") or default_widths["status"]),
            },
            {
                "key": "delivery",
                "label": self.t("cost_orders.col.delivery", "Тип доставки"),
                "width": int(widths.get("delivery") or default_widths["delivery"]),
            },
            {
                "key": "comment",
                "label": self.t("cost_orders.col.comment", "Коментар"),
                "width": int(widths.get("comment") or default_widths["comment"]),
            },
            {
                "key": "qty",
                "label": self.t("cost_orders.col.qty", "К-сть"),
                "width": int(widths.get("qty") or default_widths["qty"]),
            },
            {
                "key": "amount",
                "label": self.t("cost_orders.col.amount", "Вартість"),
                "width": int(widths.get("amount") or default_widths["amount"]),
            },
            {
                "key": "full_amount",
                "label": self.t("cost_orders.col.full_amount", "Вартість повна"),
                "width": int(
                    widths.get("full_amount") or default_widths["full_amount"]
                ),
            },
            {
                "key": "calc_weight",
                "label": self.t("cost_orders.col.calc_weight", "Вага розр."),
                "width": int(
                    widths.get("calc_weight") or default_widths["calc_weight"]
                ),
            },
            {
                "key": "fact_weight",
                "label": self.t("cost_orders.col.fact_weight", "Вага факт."),
                "width": int(
                    widths.get("fact_weight") or default_widths["fact_weight"]
                ),
            },
            {
                "key": "tariff",
                "label": self.t("cost_orders.col.tariff", "Тариф"),
                "width": int(widths.get("tariff") or default_widths["tariff"]),
            },
            {
                "key": "log_calc",
                "label": f"{self.t('cost_orders.col.log_calc', 'Лог. розр.')} {self._currency_symbol(logistics_ccy)}",
                "width": int(widths.get("log_calc") or default_widths["log_calc"]),
            },
            {
                "key": "log_fact",
                "label": f"{self.t('cost_orders.col.log_fact', 'Лог. факт')} {self._currency_symbol(logistics_ccy)}",
                "width": int(widths.get("log_fact") or default_widths["log_fact"]),
            },
            {
                "key": "unit_cost",
                "label": f"{self.t('cost_orders.col.unit_cost', 'С/В')} {self._currency_symbol(orders_unit_cost_ccy)}",
                "width": int(widths.get("unit_cost") or default_widths["unit_cost"]),
            },
            {
                "key": "new_price",
                "label": f"{self.t('cost_orders.col.new_price', 'Нова ціна')} {self._currency_symbol(new_price_ccy)}",
                "width": int(widths.get("new_price") or default_widths["new_price"]),
            },
            {
                "key": "new_margin",
                "label": self.t("cost_orders.col.new_margin", "Рентаб. нова"),
                "width": int(widths.get("new_margin") or default_widths["new_margin"]),
            },
        ]

    def _visible_orders_base_column_ids(self) -> set[str]:
        defaults = self._column_defaults()
        all_ids = [
            str(col.get("key") or "").strip()
            for col in self._all_orders_base_column_defs()
            if str(col.get("key") or "").strip()
        ]
        raw = defaults.get("orders_visible_base_column_ids")
        if not isinstance(raw, list):
            return set(all_ids)
        selected = {str(v).strip() for v in raw if str(v).strip()}
        filtered = {col_id for col_id in all_ids if col_id in selected}
        return filtered or set(all_ids)

    def _ordered_orders_base_column_ids(self) -> list[str]:
        defaults = self._column_defaults()
        all_ids = [
            str(col.get("key") or "").strip()
            for col in self._all_orders_base_column_defs()
            if str(col.get("key") or "").strip()
        ]
        raw = defaults.get("orders_base_column_order_ids")
        if not isinstance(raw, list):
            return list(all_ids)
        normalized = [str(v).strip() for v in raw if str(v).strip()]
        return [cid for cid in normalized if cid in all_ids] + [
            cid for cid in all_ids if cid not in set(normalized)
        ]

    def _set_visible_orders_base_column_ids(self, column_ids: list[str]) -> None:
        all_ids = [
            str(col.get("key") or "").strip()
            for col in self._all_orders_base_column_defs()
            if str(col.get("key") or "").strip()
        ]
        chosen = {str(v).strip() for v in (column_ids or []) if str(v).strip()}
        normalized = [col_id for col_id in all_ids if col_id in chosen]
        if not normalized:
            normalized = list(all_ids)
        self._patch_column_defaults({"orders_visible_base_column_ids": normalized})
        self._restart_tab()

    def _set_orders_base_column_order_ids(self, column_ids: list[str]) -> None:
        all_ids = [
            str(col.get("key") or "").strip()
            for col in self._all_orders_base_column_defs()
            if str(col.get("key") or "").strip()
        ]
        normalized_raw = [str(v).strip() for v in (column_ids or []) if str(v).strip()]
        normalized = [cid for cid in normalized_raw if cid in all_ids] + [
            cid for cid in all_ids if cid not in set(normalized_raw)
        ]
        self._patch_column_defaults({"orders_base_column_order_ids": normalized})

    def _open_orders_base_columns_dialog(self) -> None:
        page = self._pg()
        if not page:
            return
        selected = set(self._visible_orders_base_column_ids())
        ordered_ids = list(self._ordered_orders_base_column_ids())
        by_key = {
            str(col.get("key") or "").strip(): col
            for col in self._all_orders_base_column_defs()
        }
        width_inputs: dict[str, ft.TextField] = {}
        rows_host = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO)

        def _render_rows():
            controls = []
            total = len(ordered_ids)
            for idx, key in enumerate(ordered_ids):
                col = by_key.get(key)
                if not col:
                    continue
                label = str(col.get("label") or key).strip() or key
                cb = ft.Checkbox(label=label, value=(key in selected))
                width_value = str(int(col.get("width") or 0) or "")
                width_field = ft.TextField(
                    value=width_value,
                    width=72,
                    dense=True,
                    text_size=11,
                    content_padding=ft.padding.symmetric(horizontal=8, vertical=8),
                    keyboard_type=ft.KeyboardType.NUMBER,
                    hint_text="Шир.",
                )
                width_inputs[key] = width_field

                def _toggle(e, current_key=key):
                    if bool(getattr(e.control, "value", False)):
                        selected.add(current_key)
                    else:
                        selected.discard(current_key)

                def _move_up(_e, i=idx):
                    if i <= 0:
                        return
                    ordered_ids[i - 1], ordered_ids[i] = (
                        ordered_ids[i],
                        ordered_ids[i - 1],
                    )
                    _render_rows()

                def _move_down(_e, i=idx):
                    if i >= len(ordered_ids) - 1:
                        return
                    ordered_ids[i + 1], ordered_ids[i] = (
                        ordered_ids[i],
                        ordered_ids[i + 1],
                    )
                    _render_rows()

                cb.on_change = _toggle
                controls.append(
                    ft.Row(
                        [
                            ft.Container(width=210, content=cb),
                            ft.IconButton(
                                icon=ft.Icons.KEYBOARD_ARROW_UP,
                                icon_size=16,
                                tooltip="Вгору",
                                disabled=(idx == 0),
                                on_click=_move_up,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.KEYBOARD_ARROW_DOWN,
                                icon_size=16,
                                tooltip="Вниз",
                                disabled=(idx == total - 1),
                                on_click=_move_down,
                            ),
                            ft.Container(width=84, content=width_field),
                        ],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )
            rows_host.controls = controls
            try:
                rows_host.update()
            except Exception:
                pass

        _render_rows()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Показувати стовпці"),
            content=ft.Container(
                width=420,
                height=420,
                content=rows_host,
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _e: page.close(dialog)),
                ft.FilledButton(
                    "Зберегти",
                    on_click=lambda _e: self._save_orders_base_columns_dialog(
                        page,
                        dialog,
                        ordered_ids,
                        selected,
                        width_inputs,
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dialog)

    def _save_orders_base_columns_dialog(
        self,
        page,
        dialog,
        ordered_ids: list[str],
        selected: set[str],
        width_inputs: dict[str, ft.TextField],
    ) -> None:
        width_patch = {}
        for key, field in (width_inputs or {}).items():
            raw = str(getattr(field, "value", "") or "").strip()
            try:
                width = max(0, int(float(raw)))
            except Exception:
                continue
            width_patch[key] = width
        self._patch_column_defaults(
            {
                "orders_base_column_order_ids": list(ordered_ids),
                "orders_visible_base_column_ids": list(selected),
                "orders_base_column_widths": width_patch,
            }
        )
        page.close(dialog)
        self._restart_tab()

    def _all_orders_tier_ids(self) -> list[str]:
        return [
            str((tier or {}).get("id") or "").strip()
            for tier in (self._price_tiers or [])
            if isinstance(tier, dict) and str((tier or {}).get("id") or "").strip()
        ]

    def _visible_orders_tier_ids(self) -> set[str]:
        all_ids = self._all_orders_tier_ids()
        defaults = self._column_defaults()
        raw = defaults.get("orders_visible_tier_ids")
        if not isinstance(raw, list):
            return set(all_ids)
        selected = {str(v).strip() for v in raw if str(v).strip()}
        return {tid for tid in all_ids if tid in selected}

    def _restart_tab(self) -> None:
        """Full tab restart — like Ctrl+F5. Saves UI state locally then rebuilds from scratch."""
        import contextlib as _ctx

        pg = self._pg()
        if pg is None:
            return
        dash = getattr(pg, "_dashboard", None)
        if dash is None:
            self._force_full_bc_update = True
            self._schedule_render(0.05)
            return

        # Persist UI state in a separate bucket key (not column_defaults — it strips unknown keys)
        _bucket = self._state_bucket()
        _bucket["ui_restore"] = {
            "overflow_expanded": bool(self._orders_overflow_expanded),
            "h_scroll": float(self._table_h_scroll_offset or 0.0),
            "v_scroll": float(self._orders_v_scroll_offset or 0.0),
        }
        self._save_workspace_state_to_file()

        async def _do_restart():
            # Flush pending changes to DB before rebuilding so nothing is lost.
            with _ctx.suppress(Exception):
                await self._force_sync_workspace_state_async()
            with _ctx.suppress(Exception):
                await dash._handle_ctrl_f5()

        with _ctx.suppress(Exception):
            pg.run_task(_do_restart)

    def _set_visible_orders_tier_ids(self, tier_ids: list[str]) -> None:
        all_ids = self._all_orders_tier_ids()
        chosen = {str(v).strip() for v in (tier_ids or []) if str(v).strip()}
        self._patch_column_defaults(
            {"orders_visible_tier_ids": [tid for tid in all_ids if tid in chosen]}
        )
        self._restart_tab()

    def _apply_tier_visibility_surgical(self, new_visible_ids: set) -> bool:
        """Toggle tier column visibility in-place without rebuilding all rows.

        Returns True if all tier cells were updated successfully via direct refs.
        Returns False if refs are missing → caller falls back to full render.
        """
        all_tier_refs = getattr(self, "_order_tier_all_cell_refs", None)
        if not all_tier_refs:
            return False

        cells_toggled = 0

        for cv_key, tier_map in all_tier_refs.items():
            for tier_id, (ct, sale_txt, margin_txt, full_w) in tier_map.items():
                should_be_visible = tier_id in new_visible_ids
                currently_visible = getattr(ct, "visible", True)
                if bool(currently_visible) == should_be_visible:
                    continue
                if should_be_visible:
                    ct.width = full_w
                    ct.height = 34
                    ct.visible = True
                    if ct.border is None:
                        ct.border = ft.border.all(1, _BORDER)
                    if ct.padding is None:
                        ct.padding = ft.padding.symmetric(horizontal=2, vertical=1)
                else:
                    ct.width = 0
                    ct.height = 0
                    ct.visible = False
                cells_toggled += 1

        if cells_toggled == 0 and all_tier_refs:
            return True
        if cells_toggled == 0:
            return False

        # Toggle header tier cells (same pattern)
        hdr_tier_refs = getattr(self, "_header_tier_cell_refs", None) or {}
        for tier_id, (hdr_ct, full_w) in hdr_tier_refs.items():
            should_be_visible = tier_id in new_visible_ids
            currently_visible = getattr(hdr_ct, "visible", True)
            if bool(currently_visible) == should_be_visible:
                continue
            if should_be_visible:
                hdr_ct.width = full_w
                hdr_ct.height = 32
                hdr_ct.visible = True
                if hdr_ct.border is None:
                    hdr_ct.border = ft.border.all(1, _BORDER)
                if hdr_ct.padding is None:
                    hdr_ct.padding = ft.padding.symmetric(horizontal=4, vertical=2)
            else:
                hdr_ct.width = 0
                hdr_ct.height = 0
                hdr_ct.visible = False

        # Update data columns in one pass
        lc = getattr(self, "_data_left_col_ref", None)
        rc = getattr(self, "_data_right_col_ref", None)
        if lc is None or rc is None:
            return False

        _safe_flet_update(lc)
        _safe_flet_update(rc)

        # Update sticky header (includes header tier cells)
        pg = self._pg()
        _sticky = getattr(pg, "_view_sticky_header", None) if pg else None
        if _sticky is not None:
            with contextlib.suppress(Exception):
                _sticky.update()

        return True

    def _open_orders_price_columns_dialog(self) -> None:
        page = self._pg()
        if not page:
            return

        tiers = [
            tier
            for tier in (self._price_tiers or [])
            if isinstance(tier, dict) and str((tier or {}).get("id") or "").strip()
        ]
        if not tiers:
            self._snack("Немає цінових рівнів", _WARN)
            return

        selected = set(self._visible_orders_tier_ids())
        checks = []
        for tier in tiers:
            tier_id = str((tier or {}).get("id") or "").strip()
            tier_name = str((tier or {}).get("name") or "Рівень").strip() or "Рівень"
            cb = ft.Checkbox(label=tier_name, value=(tier_id in selected))

            def _toggle(e, current_id=tier_id):
                if bool(getattr(e.control, "value", False)):
                    selected.add(current_id)
                else:
                    selected.discard(current_id)

            cb.on_change = _toggle
            checks.append(cb)

        dlg = ft.AlertDialog(modal=True)

        def _close(_e=None):
            try:
                if hasattr(page, "close_dialog"):
                    page.close_dialog()
                else:
                    dlg.open = False
                    try:
                        dlg.update()
                    except Exception:
                        pass
            except Exception:
                dlg.open = False
                try:
                    dlg.update()
                except Exception:
                    pass

        def _apply(_e=None):
            self._set_visible_orders_tier_ids(list(selected))
            _close()

        dlg.title = ft.Text("Видимість цін")
        dlg.content = ft.Container(
            width=360,
            content=ft.Column(
                checks,
                spacing=4,
                tight=True,
                scroll=ft.ScrollMode.AUTO,
            ),
        )
        dlg.actions = [
            ft.TextButton("Скасувати", on_click=_close),
            ft.FilledButton("Застосувати", on_click=_apply),
        ]
        dlg.actions_alignment = ft.MainAxisAlignment.END

        try:
            if hasattr(page, "show_dialog"):
                page.show_dialog(dlg)
            else:
                page.dialog = dlg
                dlg.open = True
                try:
                    dlg.update()
                except Exception:
                    pass
        except Exception:
            logger.exception("[cost_price] open_orders_price_dialog_failed")
            self._snack("Не вдалося відкрити список цін", _ERR)

    def _open_orders_table_dialog(self) -> None:
        """Combined dialog: base columns + price tier visibility + widths."""
        page = self._pg()
        if not page:
            return

        defaults = self._column_defaults()

        _dfm_month_names = {
            1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень",
            5: "Травень", 6: "Червень", 7: "Липень", 8: "Серпень",
            9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень",
        }
        _dfm_months = sorted(
            {
                str(o.get("order_date") or "")[:7]
                for o in (self._orders or [])
                if isinstance(o, dict) and str(o.get("order_date") or "")[:7]
            }
        )

        def _dfm_label(m: str) -> str:
            try:
                mm = int(m[5:7])
            except Exception:
                return m
            return f"{_dfm_month_names.get(mm, m[5:7])} {m[:4]}"

        _dfm_current = str(defaults.get("display_from_month") or "").strip()
        display_from_dd = ft.Dropdown(
            label="Перший місяць відображення",
            value=(_dfm_current if _dfm_current in _dfm_months else ""),
            options=[ft.dropdown.Option("", "Показати всю історію")]
            + [ft.dropdown.Option(m, _dfm_label(m)) for m in _dfm_months],
            width=320,
            text_size=11,
        )

        # ── Columns section (excludes new_price / new_margin — those live in Ціни) ──
        _price_only_keys = {"new_price", "new_margin"}
        selected_cols = set(self._visible_orders_base_column_ids())
        ordered_ids = [
            k
            for k in self._ordered_orders_base_column_ids()
            if k not in _price_only_keys
        ]
        by_key = {
            str(col.get("key") or "").strip(): col
            for col in self._all_orders_base_column_defs()
        }
        width_inputs: dict[str, ft.TextField] = {}
        cols_host = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO)

        def _render_col_rows():
            controls = []
            total = len(ordered_ids)
            for idx, key in enumerate(ordered_ids):
                col = by_key.get(key)
                if not col:
                    continue
                label = str(col.get("label") or key).strip() or key
                cb = ft.Checkbox(label=label, value=(key in selected_cols))
                width_field = ft.TextField(
                    value=str(int(col.get("width") or 0) or ""),
                    width=68,
                    dense=True,
                    text_size=11,
                    content_padding=ft.padding.symmetric(horizontal=6, vertical=6),
                    keyboard_type=ft.KeyboardType.NUMBER,
                    hint_text="Шир.",
                )
                width_inputs[key] = width_field

                def _toggle_col(e, k=key):
                    if bool(getattr(e.control, "value", False)):
                        selected_cols.add(k)
                    else:
                        selected_cols.discard(k)

                def _move_up(_e, i=idx):
                    if i <= 0:
                        return
                    ordered_ids[i - 1], ordered_ids[i] = (
                        ordered_ids[i],
                        ordered_ids[i - 1],
                    )
                    _render_col_rows()

                def _move_down(_e, i=idx):
                    if i >= len(ordered_ids) - 1:
                        return
                    ordered_ids[i + 1], ordered_ids[i] = (
                        ordered_ids[i],
                        ordered_ids[i + 1],
                    )
                    _render_col_rows()

                cb.on_change = _toggle_col
                controls.append(
                    ft.Row(
                        [
                            ft.Container(width=200, content=cb),
                            ft.IconButton(
                                icon=ft.Icons.KEYBOARD_ARROW_UP,
                                icon_size=15,
                                tooltip="Вгору",
                                disabled=(idx == 0),
                                on_click=_move_up,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.KEYBOARD_ARROW_DOWN,
                                icon_size=15,
                                tooltip="Вниз",
                                disabled=(idx == total - 1),
                                on_click=_move_down,
                            ),
                            ft.Container(width=76, content=width_field),
                        ],
                        spacing=2,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )
            cols_host.controls = controls
            try:
                cols_host.update()
            except Exception:
                pass

        _render_col_rows()

        # ── Price tiers section ──────────────────────────────────────────────
        tiers = [
            tier
            for tier in (self._price_tiers or [])
            if isinstance(tier, dict) and str((tier or {}).get("id") or "").strip()
        ]
        selected_tiers = set(self._visible_orders_tier_ids())
        tier_widths_cfg = defaults.get("orders_tier_widths") or {}
        default_widths = self._base_column_default_widths()

        # tier_width_inputs: { tier_id: {"price_w": TextField, "margin_w": TextField} }
        tier_width_inputs: dict[str, dict] = {}
        tier_rows = []

        # Column header for tier section
        tier_rows.append(
            ft.Row(
                [
                    ft.Container(
                        width=160, content=ft.Text("Назва", size=9, color=_TEXT)
                    ),
                    ft.Container(
                        width=68, content=ft.Text("Ціна", size=9, color=_TEXT)
                    ),
                    ft.Container(
                        width=68, content=ft.Text("Рентаб.", size=9, color=_TEXT)
                    ),
                ],
                spacing=4,
            )
        )

        for tier in tiers:
            tier_id = str((tier or {}).get("id") or "").strip()
            tier_name = str((tier or {}).get("name") or "Рівень").strip() or "Рівень"
            display_name = tier_name if len(tier_name) <= 22 else tier_name[:20] + "…"
            cb_t = ft.Checkbox(
                label=display_name,
                value=(tier_id in selected_tiers),
                label_style=ft.TextStyle(size=11),
                tooltip=tier_name if len(tier_name) > 22 else None,
            )

            def _toggle_tier(e, tid=tier_id):
                if bool(getattr(e.control, "value", False)):
                    selected_tiers.add(tid)
                else:
                    selected_tiers.discard(tid)

            cb_t.on_change = _toggle_tier

            _tw = tier_widths_cfg.get(tier_id) or {}
            _tw_combined = int(_tw.get("width") or 0) or (
                int(_tw.get("price_w") or 50) + int(_tw.get("margin_w") or 50)
            )
            w_field = ft.TextField(
                value=str(_tw_combined),
                width=80,
                dense=True,
                text_size=11,
                content_padding=ft.padding.symmetric(horizontal=6, vertical=6),
                keyboard_type=ft.KeyboardType.NUMBER,
                hint_text="px",
            )
            tier_width_inputs[tier_id] = {"width": w_field}

            tier_rows.append(
                ft.Row(
                    [
                        ft.Container(
                            width=200,
                            content=cb_t,
                            clip_behavior=ft.ClipBehavior.HARD_EDGE,
                        ),
                        ft.Container(width=80, content=w_field),
                    ],
                    spacing=4,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
            )

        # ── New price / new margin rows in Ціни section ──────────────────────
        _wdef = defaults.get("orders_base_column_widths") or {}
        np_col = by_key.get("new_price") or {}
        nm_col = by_key.get("new_margin") or {}

        np_cb = ft.Checkbox(
            label=str(np_col.get("label") or "Нова ціна"),
            value=("new_price" in selected_cols),
            label_style=ft.TextStyle(size=11),
        )
        np_cb.on_change = lambda e: (
            selected_cols.add("new_price")
            if bool(getattr(e.control, "value", False))
            else selected_cols.discard("new_price")
        )
        np_w_input = ft.TextField(
            value=str(
                int(_wdef.get("new_price") or default_widths.get("new_price") or 72)
            ),
            width=60,
            dense=True,
            text_size=11,
            content_padding=ft.padding.symmetric(horizontal=6, vertical=6),
            keyboard_type=ft.KeyboardType.NUMBER,
            hint_text="px",
        )

        nm_cb = ft.Checkbox(
            label=str(nm_col.get("label") or "Рентаб. нова"),
            value=("new_margin" in selected_cols),
            label_style=ft.TextStyle(size=11),
        )
        nm_cb.on_change = lambda e: (
            selected_cols.add("new_margin")
            if bool(getattr(e.control, "value", False))
            else selected_cols.discard("new_margin")
        )
        nm_w_input = ft.TextField(
            value=str(
                int(_wdef.get("new_margin") or default_widths.get("new_margin") or 72)
            ),
            width=60,
            dense=True,
            text_size=11,
            content_padding=ft.padding.symmetric(horizontal=6, vertical=6),
            keyboard_type=ft.KeyboardType.NUMBER,
            hint_text="px",
        )

        if tiers:
            tier_rows.append(ft.Divider(height=8, color=_HDR))

        tier_rows.append(
            ft.Row(
                [
                    ft.Container(width=160, content=np_cb),
                    ft.Container(width=68, content=np_w_input),
                    ft.Container(width=68, content=ft.Text("", size=9)),
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )
        tier_rows.append(
            ft.Row(
                [
                    ft.Container(width=160, content=nm_cb),
                    ft.Container(width=68, content=nm_w_input),
                    ft.Container(width=68, content=ft.Text("", size=9)),
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )

        content_col = ft.Column(
            [
                display_from_dd,
                ft.Divider(height=8, color=_HDR),
                ft.Text("Стовпці", size=12, weight="bold", color=_CHIP_T),
                ft.Container(
                    height=260,
                    content=cols_host,
                ),
                ft.Divider(height=12, color=_HDR),
                ft.Text("Ціни", size=12, weight="bold", color=_CHIP_T),
                ft.Column(tier_rows, spacing=4, tight=True),
            ],
            spacing=6,
            scroll=ft.ScrollMode.AUTO,
        )

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Таблиця"),
            content=ft.Container(width=460, height=540, content=content_col),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _e: page.close(dialog)),
                ft.FilledButton(
                    "Зберегти",
                    on_click=lambda _e: self._save_orders_table_dialog(
                        page,
                        dialog,
                        ordered_ids,
                        selected_cols,
                        width_inputs,
                        selected_tiers,
                        tier_width_inputs,
                        np_cb,
                        np_w_input,
                        nm_cb,
                        nm_w_input,
                        display_from_dd,
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dialog)

    def _save_orders_table_dialog(
        self,
        page,
        dialog,
        ordered_ids: list,
        selected_cols: set,
        width_inputs: dict,
        selected_tiers: set,
        tier_width_inputs: dict | None = None,
        np_cb=None,
        np_w_input=None,
        nm_cb=None,
        nm_w_input=None,
        display_from_dd=None,
    ) -> None:
        width_patch = {}
        for key, field in (width_inputs or {}).items():
            raw = str(getattr(field, "value", "") or "").strip()
            try:
                width_patch[key] = max(0, int(float(raw)))
            except Exception:
                pass

        # new_price / new_margin visibility + widths
        if np_cb is not None:
            if bool(getattr(np_cb, "value", False)):
                selected_cols.add("new_price")
            else:
                selected_cols.discard("new_price")
        if np_w_input is not None:
            raw = str(getattr(np_w_input, "value", "") or "").strip()
            try:
                width_patch["new_price"] = max(0, int(float(raw)))
            except Exception:
                pass
        if nm_cb is not None:
            if bool(getattr(nm_cb, "value", False)):
                selected_cols.add("new_margin")
            else:
                selected_cols.discard("new_margin")
        if nm_w_input is not None:
            raw = str(getattr(nm_w_input, "value", "") or "").strip()
            try:
                width_patch["new_margin"] = max(0, int(float(raw)))
            except Exception:
                pass

        # tier widths
        tier_widths_patch: dict[str, dict] = {}
        for tier_id, fields in (tier_width_inputs or {}).items():
            f = fields.get("width")
            if f is None:
                continue
            raw = str(getattr(f, "value", "") or "").strip()
            try:
                tier_widths_patch[tier_id] = {"width": max(1, int(float(raw)))}
            except Exception:
                pass

        all_tier_ids = self._all_orders_tier_ids()
        self._patch_column_defaults(
            {
                "orders_base_column_order_ids": list(ordered_ids),
                "orders_visible_base_column_ids": list(selected_cols),
                "orders_base_column_widths": width_patch,
                "orders_visible_tier_ids": [
                    t for t in all_tier_ids if t in selected_tiers
                ],
                "orders_tier_widths": tier_widths_patch,
                "display_from_month": str(getattr(display_from_dd, "value", "") or "").strip(),
            }
        )
        page.close(dialog)
        self._restart_tab()

    def _open_calculator_dialog(self) -> None:
        page = self._pg()
        if not page:
            return

        # Pre-compute unit cost (converted to USD) from already-built rows
        # (has logistics baked in). Rows can each be in a different native
        # pivot currency now, so this modal — which mixes many products and
        # compares against a single sale-price currency — normalizes every
        # value to USD here rather than propagating per-row currencies
        # through the rest of its (already USD-flavoured) local variables.
        unit_cost_lookup: dict[tuple, float] = {}
        for _row in self._orders_rows or []:
            if not isinstance(_row, dict):
                continue
            _oid = str(_row.get("order_id") or "")
            _li = int(_row.get("line_idx") or 0)
            _uc_ccy = str(_row.get("unit_cost_currency") or "USD").upper().strip() or "USD"
            _usd = self._convert_currency(
                _safe_float(_row.get("unit_cost_usd"), None), _uc_ccy, "USD"
            )
            if _usd is not None:
                unit_cost_lookup[(_oid, _li)] = _usd

        # Aggregate avg unit_cost_usd per unique product — only items with a real product_id.
        prod_costs: dict[str, list[float]] = {}
        prod_names: dict[str, str] = {}
        prod_category: dict[str, str] = {}

        for order in self._orders or []:
            if not isinstance(order, dict):
                continue
            order_id = str(order.get("id") or id(order))
            items_raw = (
                order.get("items") if isinstance(order.get("items"), list) else []
            )
            if not items_raw:
                items_raw = [{"product_id": str(order.get("product_id") or "")}]
            for i_idx, item in enumerate(items_raw):
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("product_id") or "").strip()
                if not pid:
                    continue
                product = self._product_by_id.get(pid) or {}
                pname = str(
                    self._product_display_name(product) or item.get("product_name") or pid
                ).strip()
                if pid not in prod_names:
                    prod_names[pid] = pname
                    prod_category[pid] = str(product.get("category_id") or "").strip()
                usd = unit_cost_lookup.get((order_id, i_idx))
                if usd is not None:
                    prod_costs.setdefault(pid, []).append(usd)

        if not prod_names:
            self._snack("Немає товарів у поточному списку", _WARN)
            return

        tiers = [
            t
            for t in (self._price_tiers or [])
            if isinstance(t, dict) and str((t or {}).get("id") or "").strip()
        ]

        # ── Column widths (25% wider than the original 155/60/74/94 so the
        # table fits without horizontal scroll on typical tier counts) ────────
        C_NAME = 194
        C_COST = 75
        C_TIER = 93
        C_COMBO = 118  # combined: new price + margin
        ROW_H = 38
        HDR_H = 28

        # ── Helpers ───────────────────────────────────────────────────────────
        def _margin_bg_fg(m: Optional[float], target: Optional[float]):
            if m is None:
                return _ALT, "#9CA3AF"
            m_pct = float(m) * 100.0
            thr = float(target or 0.0)
            if m_pct >= thr:
                return _OK_BG, _OK_T
            if m_pct >= thr - 5.0:
                return "#FEF9C3", "#854D0E"
            return _ERR_BG, _ERR

        def _fmt_margin(m: Optional[float]) -> str:
            return f"{m * 100:.1f}%" if m is not None else "-"

        def _tier_price_for_pid(pid: str, tier: dict):
            product = self._product_by_id.get(pid) or {}
            return self._sales_price_for_tier(product, tier)

        def _recalc_margin(k: str):
            tf = np_fields.get(k)
            nm = nm_texts.get(k)
            ctr = np_containers.get(k)
            if tf is None or nm is None:
                return
            raw = str(tf.value or "").strip().replace(",", ".")
            try:
                sale_v = float(raw) if raw else None
            except Exception:
                sale_v = None
            c_list = prod_costs.get(k) or []
            c_usd = (sum(c_list) / len(c_list)) if c_list else None
            target_m = self._target_margin(prod_category.get(k, ""))
            if sale_v is not None and c_usd is not None and sale_v > 0:
                sale_in_usd = (
                    self._convert_currency(sale_v, state["ccy"], "USD") or sale_v
                )
                m = (sale_in_usd - c_usd) / sale_in_usd if sale_in_usd > 0 else None
            else:
                m = None
            if m is not None:
                cell_bg, cell_fg = _margin_bg_fg(m, target_m)
                nm.value = _fmt_margin(m)
                nm.color = cell_fg
                if ctr is not None:
                    ctr.bgcolor = cell_bg
            else:
                nm.value = "-"
                nm.color = _TEXT
                if ctr is not None:
                    ctr.bgcolor = _PANEL
            with contextlib.suppress(Exception):
                nm.update()
            with contextlib.suppress(Exception):
                if ctr is not None:
                    ctr.update()

        # ── Currency state (local only, NOT saved to DB) ───────────────────────
        state = {"ccy": "USD", "cost_ccy": "USD"}
        ccy_row_ref: list = []  # will hold the ft.Row with "Нова ціна" currency buttons
        cost_ccy_row_ref: list = []  # will hold the ft.Row with "С/В" currency buttons

        np_fields: dict[str, ft.TextField] = {}
        nm_texts: dict[str, ft.Text] = {}
        np_containers: dict[str, ft.Container] = {}
        data_col = ft.Column([], spacing=0, tight=True, scroll=ft.ScrollMode.AUTO)

        # ── Header builder ────────────────────────────────────────────────────
        def _hcell(label: str, w: int, align=ft.TextAlign.RIGHT):
            return ft.Container(
                width=w,
                height=HDR_H,
                bgcolor=_HDR,
                border=ft.border.all(1, _BORDER),
                alignment=ft.Alignment(
                    (
                        1
                        if align == ft.TextAlign.RIGHT
                        else (0 if align == ft.TextAlign.CENTER else -1)
                    ),
                    0,
                ),
                padding=ft.padding.symmetric(horizontal=4, vertical=2),
                content=ft.Text(
                    label, size=8, weight="bold", color=_CHIP_T, text_align=align
                ),
            )

        # ── Table builder (rebuilds data rows; header is static) ─────────────────
        def _build_table():
            data_rows: list[ft.Row] = []
            sorted_prods = sorted(prod_names.items(), key=lambda x: x[1].lower())
            for i, (key, pname) in enumerate(sorted_prods):
                costs = prod_costs.get(key) or []
                avg_cost_usd = (sum(costs) / len(costs)) if costs else None
                target_m = self._target_margin(prod_category.get(key, ""))
                row_bg = _PANEL if i % 2 == 0 else _ALT_2

                # Build/reuse margin Text widget
                if key not in nm_texts:
                    nm_texts[key] = ft.Text(
                        "-",
                        size=8,
                        weight="bold",
                        color=_TEXT,
                        text_align=ft.TextAlign.RIGHT,
                    )
                nm_t = nm_texts[key]

                # Build/reuse "Нова ціна" TextField (no DB save — local only)
                if key not in np_fields:

                    def _on_np_change(e, k=key):
                        _recalc_margin(k)

                    np_fields[key] = ft.TextField(
                        value="",
                        width=C_COMBO - 8,
                        height=20,
                        text_size=8,
                        dense=True,
                        filled=True,
                        fill_color=_PANEL,
                        border=ft.InputBorder.NONE,
                        border_width=0,
                        border_radius=0,
                        content_padding=ft.padding.symmetric(horizontal=4, vertical=1),
                        text_align=ft.TextAlign.CENTER,
                        text_vertical_align=ft.VerticalAlignment.CENTER,
                        on_change=_on_np_change,
                    )
                np_tf = np_fields[key]

                cells: list = [
                    ft.Container(
                        width=C_NAME,
                        bgcolor=row_bg,
                        border=ft.border.all(1, _BORDER2),
                        alignment=ft.Alignment(-1, 0),
                        padding=ft.padding.symmetric(horizontal=5, vertical=5),
                        tooltip=ft.Tooltip(message=pname, wait_duration=0),
                        content=ft.Text(
                            pname, size=8, no_wrap=False, overflow=ft.TextOverflow.CLIP
                        ),
                    ),
                    ft.Container(
                        width=C_COST,
                        height=ROW_H,
                        bgcolor=row_bg,
                        border=ft.border.all(1, _BORDER2),
                        alignment=ft.Alignment(1, 0),
                        padding=ft.padding.symmetric(horizontal=5, vertical=2),
                        content=ft.Text(
                            (
                                self._fmt_money(
                                    self._convert_currency(
                                        avg_cost_usd, "USD", state["cost_ccy"]
                                    )
                                    or avg_cost_usd,
                                    state["cost_ccy"],
                                )
                                if avg_cost_usd is not None
                                else "-"
                            ),
                            size=8,
                            text_align=ft.TextAlign.RIGHT,
                        ),
                    ),
                ]

                for tier in tiers:
                    sale_price, sale_ccy = _tier_price_for_pid(key, tier)
                    if (
                        sale_price is not None
                        and avg_cost_usd is not None
                        and sale_price > 0
                    ):
                        sale_usd = (
                            self._convert_currency(sale_price, sale_ccy, "USD")
                            or sale_price
                        )
                        m = (
                            (sale_usd - avg_cost_usd) / sale_usd
                            if sale_usd > 0
                            else None
                        )
                    else:
                        m = None
                        sale_price = None
                    cell_bg, cell_fg = _margin_bg_fg(m, target_m)
                    # Display tier prices in their native currency (not disp_ccy)
                    price_str = (
                        self._fmt_money(sale_price, sale_ccy)
                        if sale_price is not None
                        else "-"
                    )
                    marg_str = _fmt_margin(m)
                    cells.append(
                        ft.Container(
                            width=C_TIER,
                            height=ROW_H,
                            bgcolor=cell_bg,
                            border=ft.border.all(1, _BORDER2),
                            alignment=ft.Alignment(1, 0),
                            padding=ft.padding.symmetric(horizontal=4, vertical=3),
                            content=ft.Column(
                                [
                                    ft.Text(
                                        price_str,
                                        size=8,
                                        weight="bold",
                                        color=cell_fg,
                                        text_align=ft.TextAlign.RIGHT,
                                    ),
                                    ft.Text(
                                        marg_str,
                                        size=7,
                                        color=cell_fg,
                                        text_align=ft.TextAlign.RIGHT,
                                    ),
                                ],
                                spacing=0,
                                tight=True,
                                horizontal_alignment=ft.CrossAxisAlignment.END,
                            ),
                        )
                    )

                combo_cell = ft.Container(
                    width=C_COMBO,
                    height=ROW_H,
                    bgcolor=row_bg,
                    border=ft.border.all(1, _BORDER2),
                    alignment=ft.Alignment(0, 0),
                    padding=ft.padding.symmetric(horizontal=2, vertical=3),
                    content=ft.Column(
                        [
                            ft.Container(
                                width=C_COMBO - 4,
                                clip_behavior=ft.ClipBehavior.HARD_EDGE,
                                content=np_tf,
                            ),
                            ft.Container(
                                width=C_COMBO - 4,
                                alignment=ft.Alignment(1, 0),
                                padding=ft.padding.only(right=4),
                                content=nm_t,
                            ),
                        ],
                        spacing=1,
                        tight=True,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
                np_containers[key] = combo_cell
                cells.append(combo_cell)
                data_rows.append(
                    ft.Row(
                        controls=cells,
                        spacing=0,
                        tight=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )

            data_col.controls.clear()
            data_col.controls.extend(data_rows)
            with contextlib.suppress(Exception):
                data_col.update()

        # ── Currency selector rows (one for С/В display, one for Нова ціна) ────
        def _ccy_btn(ccy: str, sel_ccy: str, on_click):
            is_sel = sel_ccy == ccy
            return ft.OutlinedButton(
                ccy,
                height=28,
                style=ft.ButtonStyle(
                    text_style=ft.TextStyle(size=10, weight="bold" if is_sel else None),
                    color=_TEXT if is_sel else "#475569",
                    bgcolor=_CHIP_BG if is_sel else None,
                    padding=ft.padding.symmetric(horizontal=8, vertical=2),
                    side=ft.BorderSide(
                        2 if is_sel else 1, _CHIP_T if is_sel else _BORDER
                    ),
                ),
                on_click=lambda _e, c=ccy: on_click(c),
            )

        def _set_ccy(new_ccy: str):
            state["ccy"] = new_ccy
            np_header_text.value = f"Нова ({new_ccy})"
            with contextlib.suppress(Exception):
                np_header_text.update()
            for k in list(np_fields.keys()):
                if str(np_fields[k].value or "").strip():
                    _recalc_margin(k)
            if ccy_row_ref:
                ccy_row_ref[0].controls.clear()
                ccy_row_ref[0].controls.extend(
                    [ft.Text("Нова ціна:", size=9, color=_TEXT)]
                    + [_ccy_btn(c, state["ccy"], _set_ccy) for c in self.CURRENCIES]
                )
                with contextlib.suppress(Exception):
                    ccy_row_ref[0].update()

        def _set_cost_ccy(new_ccy: str):
            state["cost_ccy"] = new_ccy
            cost_header_text.value = f"С/В ({new_ccy})"
            with contextlib.suppress(Exception):
                cost_header_text.update()
            if cost_ccy_row_ref:
                cost_ccy_row_ref[0].controls.clear()
                cost_ccy_row_ref[0].controls.extend(
                    [ft.Text("С/В:", size=9, color=_TEXT)]
                    + [
                        _ccy_btn(c, state["cost_ccy"], _set_cost_ccy)
                        for c in self.CURRENCIES
                    ]
                )
                with contextlib.suppress(Exception):
                    cost_ccy_row_ref[0].update()
            _build_table()

        np_header_text = ft.Text(
            f"Нова ({state['ccy']})",
            size=8,
            weight="bold",
            color=_CHIP_T,
            text_align=ft.TextAlign.CENTER,
        )
        cost_header_text = ft.Text(
            f"С/В ({state['cost_ccy']})",
            size=8,
            weight="bold",
            color=_CHIP_T,
            text_align=ft.TextAlign.RIGHT,
        )

        ccy_row = ft.Row(
            [ft.Text("Нова ціна:", size=9, color=_TEXT)]
            + [_ccy_btn(c, state["ccy"], _set_ccy) for c in self.CURRENCIES],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        ccy_row_ref.append(ccy_row)

        cost_ccy_row = ft.Row(
            [ft.Text("С/В:", size=9, color=_TEXT)]
            + [_ccy_btn(c, state["cost_ccy"], _set_cost_ccy) for c in self.CURRENCIES],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        cost_ccy_row_ref.append(cost_ccy_row)

        _build_table()

        np_header_cell = ft.Container(
            width=C_COMBO,
            height=HDR_H,
            bgcolor=_HDR,
            border=ft.border.all(1, _BORDER),
            alignment=ft.Alignment(0, 0),
            padding=ft.padding.symmetric(horizontal=4, vertical=2),
            content=np_header_text,
        )
        cost_header_cell = ft.Container(
            width=C_COST,
            height=HDR_H,
            bgcolor=_HDR,
            border=ft.border.all(1, _BORDER),
            alignment=ft.Alignment(1, 0),
            padding=ft.padding.symmetric(horizontal=4, vertical=2),
            content=cost_header_text,
        )
        static_header = ft.Row(
            [_hcell("Товар", C_NAME, ft.TextAlign.LEFT), cost_header_cell]
            + [_hcell(str((t or {}).get("name") or ""), C_TIER) for t in tiers]
            + [np_header_cell],
            spacing=0,
            tight=True,
        )

        total_w = C_NAME + C_COST + len(tiers) * C_TIER + C_COMBO
        # Horizontal scroll wraps both header and data; vertical scroll is on data_col
        table_content = ft.Column(
            [
                static_header,
                ft.Container(
                    content=data_col,
                    height=440,
                ),
            ],
            spacing=0,
            tight=True,
        )
        scrollable_content = ft.Row(
            [ft.Container(width=total_w, content=table_content)],
            spacing=0,
            scroll=ft.ScrollMode.AUTO,
        )

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Калькулятор собівартості"),
            content=ft.Container(
                width=min(total_w + 24, 1150),
                height=520,
                content=ft.Column(
                    [cost_ccy_row, ccy_row, scrollable_content],
                    spacing=8,
                    tight=True,
                    horizontal_alignment=ft.CrossAxisAlignment.START,
                ),
            ),
            actions=[
                ft.TextButton("Закрити", on_click=lambda _e: page.close(dialog)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dialog)

    def _currency_symbol(self, ccy: str) -> str:
        c = str(ccy or "").upper().strip()
        return {
            "USD": "$",
            "UAH": "₴",
            "EUR": "€",
            "PLN": "zł",
            "CNY": "¥",
        }.get(c, c)

    def _fmt_money(self, val: Optional[float], ccy: str = "") -> str:
        if val is None:
            return ""
        amount = format_grouped(val, decimals=2, trim=True)
        if not ccy:
            return amount
        return f"{amount} {self._currency_symbol(ccy)}"

    def _fetch_uah_rates(self) -> dict:
        # Delegates to the shared fx_rates module (also used by
        # cost_price_stock_page.py, orders_view.py, products_view.py) so
        # every screen fetches/caches live rates identically.
        return fx_rates.fetch_uah_rates()

    def _get_uah_rates_cached(self) -> dict:
        return fx_rates.get_uah_rates()

    def _convert_currency(
        self, amount: Optional[float], from_ccy: str, to_ccy: str
    ) -> Optional[float]:
        return fx_rates.convert(amount, from_ccy, to_ccy)

    def _sales_price_for_tier(
        self, product: dict, tier: dict
    ) -> tuple[Optional[float], str]:
        if not isinstance(product, dict) or not isinstance(tier, dict):
            return None, str((tier or {}).get("currency") or "USD").upper()
        sales = product.get("sales_prices_json")
        rows = []
        if isinstance(sales, list):
            rows = sales
        elif isinstance(sales, str) and sales.strip():
            try:
                parsed = json.loads(sales)
                if isinstance(parsed, list):
                    rows = parsed
            except Exception:
                rows = []
        tid = str(tier.get("id") or "").strip()
        tname = str(tier.get("name") or "").strip().lower()
        tccy = str(tier.get("currency") or "USD").upper().strip() or "USD"
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("tier_id") or "").strip() == tid:
                return _safe_float(row.get("price"), None), tccy
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("name") or "").strip().lower() == tname:
                return _safe_float(row.get("price"), None), tccy
        return None, tccy

    def _packaging_price_for_product(self, product: dict) -> tuple[float, str]:
        if not isinstance(product, dict):
            return 0.0, "USD"
        if not self.packaging_enabled:
            return 0.0, str(product.get("purchase_currency") or "USD").upper()
        packaging_id = str(product.get("packaging_id") or "").strip()
        if not packaging_id:
            return 0.0, str(product.get("purchase_currency") or "USD").upper()
        packaging = self._packaging_by_id.get(packaging_id) or {}
        pack_amount = _safe_float(packaging.get("price"), 0.0) or 0.0
        pack_ccy = (
            str(
                (
                    (packaging.get("suppliers") or {}).get("currency")
                    if isinstance(packaging.get("suppliers"), dict)
                    else ""
                )
                or product.get("purchase_currency")
                or "USD"
            )
            .upper()
            .strip()
            or "USD"
        )
        return pack_amount, pack_ccy

    def _load_general_combined(self) -> None:
        """Load data from all selected managers and combine (general mode only)."""
        region_id = self._region_id
        all_products: list = []
        all_orders: list = []
        all_suppliers: list = []
        all_categories: list = []
        seen_product_ids: set = set()
        seen_supplier_ids: set = set()
        seen_category_ids: set = set()

        for mid in self._general_manager_ids:
            mgr_name = (self._general_manager_names or {}).get(mid, mid)
            try:
                prods = (
                    self.db.get_products(
                        self.company_id, region_id=region_id, manager_id=mid
                    )
                    or []
                )
                vis_ids = {
                    str((p or {}).get("id") or "").strip()
                    for p in prods
                    if isinstance(p, dict) and (p.get("id") or "")
                }
                for p in prods:
                    pid = str((p or {}).get("id") or "").strip()
                    if pid and pid not in seen_product_ids:
                        seen_product_ids.add(pid)
                        all_products.append(p)
                orders = (
                    self.db.get_orders(
                        self.company_id,
                        region_id,
                        manager_id=mid,
                        force_refresh=True,
                        visible_product_ids=list(vis_ids) if vis_ids else None,
                    )
                    or []
                )
                for o in orders:
                    if isinstance(o, dict):
                        o["_gm_manager_id"] = mid
                        o["_gm_manager_name"] = mgr_name
                all_orders.extend(o for o in orders if isinstance(o, dict))
            except Exception:
                pass
            try:
                sups = (
                    self.db.get_suppliers(self.company_id, region_id, manager_id=mid)
                    or []
                )
                for s in sups:
                    sid = str((s or {}).get("id") or "").strip()
                    if sid and sid not in seen_supplier_ids:
                        seen_supplier_ids.add(sid)
                        all_suppliers.append(s)
            except Exception:
                pass
            try:
                cats = (
                    self.db.get_company_categories(
                        self.company_id, region_id, manager_id=mid
                    )
                    or []
                )
                for c in cats:
                    cid = str((c or {}).get("id") or "").strip()
                    if cid and cid not in seen_category_ids:
                        seen_category_ids.add(cid)
                        all_categories.append(c)
            except Exception:
                pass

        self._products = all_products
        self._orders = all_orders
        self._suppliers = all_suppliers
        self._categories = all_categories
        # Catalog nodes for hierarchy-level labels (brand / category / series)
        try:
            _cnodes = self.db.get_company_catalog_nodes(self.company_id) or []
            self._node_name_by_id = {
                str(n.get("id") or "").strip(): str(n.get("name") or "").strip()
                for n in _cnodes
                if isinstance(n, dict) and n.get("id") and n.get("name")
            }
            self._node_rows_by_id = {
                str(n.get("id") or "").strip(): n
                for n in _cnodes
                if isinstance(n, dict) and n.get("id")
            }
        except Exception:
            self._node_name_by_id = {}
            self._node_rows_by_id = {}
        # Packaging and delivery: load for own manager (reference data)
        try:
            self._packaging = (
                self.db.get_packaging(
                    self.company_id, region_id=region_id, manager_id=self.manager_id
                )
                or []
            )
        except Exception:
            self._packaging = []
        try:
            all_methods = (
                self.db.get_effective_delivery_methods_for_manager(
                    self.company_id, self.manager_id, region_id=region_id
                )
                or []
            )
            self._delivery_methods = [
                m
                for m in all_methods
                if isinstance(m, dict) and m.get("enabled") is not False
            ]
        except Exception:
            self._delivery_methods = []
        self._price_tiers = (
            self.db.get_effective_region_price_tiers_for_manager(
                self.company_id, self.manager_id, region_id
            )
            if region_id
            else []
        )
        try:
            self._order_price_inputs_by_key = (
                self.db.get_cost_price_order_inputs(
                    self.company_id, region_id=region_id, manager_id=self.manager_id
                )
                or {}
            )
        except Exception:
            self._order_price_inputs_by_key = {}

        self._product_by_id = {
            str((p or {}).get("id") or "").strip(): p
            for p in self._products
            if isinstance(p, dict) and (p.get("id") or "")
        }
        self._packaging_by_id = {
            str((pk or {}).get("id") or "").strip(): pk
            for pk in self._packaging
            if isinstance(pk, dict) and (pk.get("id") or "")
        }
        self._supplier_by_id = {
            str((s or {}).get("id") or "").strip(): s
            for s in self._suppliers
            if isinstance(s, dict) and (s.get("id") or "")
        }
        self._category_by_id = {
            str((c or {}).get("id") or "").strip(): c
            for c in self._categories
            if isinstance(c, dict) and (c.get("id") or "")
        }
        self._hydrate_product_level_ids()
        self._refresh_product_label_map()

        self._build_orders_rows()
        self._sync_unit_costs_to_db()
        self._sync_to_shared_map()

    def _restore_merge_groups_from_state(self) -> None:
        saved = (self._workspace_state or {}).get("merge_groups")
        if not isinstance(saved, dict):
            return
        restored = {}
        for gid, gdata in saved.items():
            if isinstance(gdata, dict) and gdata.get("order_ids"):
                restored[str(gid)] = {
                    "order_ids": [str(x) for x in (gdata.get("order_ids") or [])],
                    "comment": gdata.get("comment") or "",
                    "tariff": gdata.get("tariff"),
                    "log_fact": gdata.get("log_fact"),
                }
        if restored:
            self._orders_merge_groups = restored

    def _save_merge_groups_to_state(self) -> None:
        groups: dict = {}
        for gid, gdata in (self._orders_merge_groups or {}).items():
            groups[str(gid)] = {
                "order_ids": [str(x) for x in (gdata.get("order_ids") or [])],
                "comment": gdata.get("comment") or "",
                "tariff": gdata.get("tariff"),
                "log_fact": gdata.get("log_fact"),
            }
        self._workspace_state["merge_groups"] = groups
        self._queue_workspace_state_save()

    def _set_loading_text(self, text: str) -> None:
        try:
            ind = self._header_loading_indicator
            if ind is None or not ind.visible:
                return
            ctrls = getattr(ind, "controls", None) or []
            txt = ctrls[1] if len(ctrls) > 1 and isinstance(ctrls[1], ft.Text) else None
            if txt is None:
                return
            txt.value = text
            with contextlib.suppress(Exception):
                txt.update()
        except Exception:
            pass

    def _load(self):
        self._region_id = self._resolve_region_id()
        self._load_workspace_state()
        # Restore UI state saved by _restart_tab (stored in a separate key, not column_defaults)
        _ui = self._state_bucket().get("ui_restore")
        if isinstance(_ui, dict):
            _ovf = _ui.get("overflow_expanded")
            if isinstance(_ovf, bool):
                self._orders_overflow_expanded = _ovf
            _h = _ui.get("h_scroll")
            if isinstance(_h, (int, float)) and _h > 0:
                self._table_h_scroll_offset = float(_h)
            _v = _ui.get("v_scroll")
            if isinstance(_v, (int, float)) and _v > 0:
                self._orders_v_scroll_offset = float(_v)
        self._load_workspace_outbox()
        self._load_ops_outbox()
        self._restore_merge_groups_from_state()

        if self._general_mode and self._general_manager_ids:
            self._load_general_combined()
            return

        # Products — no force_refresh so the DB layer can return its in-memory cache.
        # Products change infrequently; the cached result is always fresh enough.
        self._products = (
            self.db.get_products(
                self.company_id,
                region_id=self._region_id,
                manager_id=self.manager_id,
            )
            or []
        )
        self._set_loading_text(f"Товари: {len(self._products)}")
        # Build the product-id filter exactly like orders_view._visible_product_ids_cache.
        _visible_product_ids = [
            str((p or {}).get("id") or "").strip()
            for p in self._products
            if isinstance(p, dict) and (p.get("id") or "").strip()
        ] or None

        # ── FAST PATH: use orders already in the shared map ───────────────────
        # Sub-views (Загальні mode) must skip: shared map has the logged-in user's
        # orders, not the subordinate's. They must always use the slow path.
        _skip_fast = self.is_read_only
        _shared_order_list = (
            []
            if _skip_fast
            else [
                dict(v)
                for k, v in self._shared_orders_map.items()
                if not str(k).startswith("__") and isinstance(v, dict) and v.get("id")
            ]
        )
        # Apply region filter: shared map may contain all regions.
        if _shared_order_list and self._region_id:
            _shared_order_list = [
                o
                for o in _shared_order_list
                if str(o.get("region_id") or "").strip() == self._region_id
            ]
        if _shared_order_list:
            _mid_own = (self.manager_id or "").strip()
            # Mirror the SQL logic: manager-owned orders are always included;
            # other orders only if they contain a product in the visible set.
            if _visible_product_ids:
                _vpid_set = set(_visible_product_ids)
                self._orders = [
                    o
                    for o in _shared_order_list
                    if str((o.get("manager_id") or "")).strip() == _mid_own
                    or any(
                        str((it.get("product_id") or "")).strip() in _vpid_set
                        for it in (o.get("items") or [])
                        if isinstance(it, dict)
                    )
                ]
            else:
                # No product visibility → filter strictly by manager ownership.
                self._orders = [
                    o
                    for o in _shared_order_list
                    if not _mid_own
                    or str((o.get("manager_id") or "")).strip() == _mid_own
                ]
        else:
            # ── SLOW PATH: load from DB (first open, or shared map empty) ─────
            self._orders = (
                self.db.get_orders(
                    self.company_id,
                    self._region_id,
                    manager_id=self.manager_id,
                    force_refresh=True,
                    visible_product_ids=_visible_product_ids,
                )
                or []
            )

        self._set_loading_text(f"Замовлення: {len(self._orders)}")
        # Merge pending outbox changes so locally-saved edits survive app restarts
        # (outbox may not have flushed to DB yet when we load fresh data).
        # Outbox format: {"order:<id>": {"kind": "order", "data": {...order_dict...}}}
        if self._ops_outbox:
            _order_by_id: dict[str, dict] = {
                str((o or {}).get("id") or "").strip(): o
                for o in self._orders
                if isinstance(o, dict) and (o.get("id") or "")
            }
            for _op in self._ops_outbox.values():
                if not isinstance(_op, dict):
                    continue
                _kind = str(_op.get("kind") or "")
                if _kind != "order":
                    continue
                _order_data = _op.get("data")
                if not isinstance(_order_data, dict):
                    continue
                _oid = str(_order_data.get("id") or "").strip()
                if not _oid or _oid not in _order_by_id:
                    continue
                _target = _order_by_id[_oid]
                _SKIP = {"id", "company_id", "region_id", "manager_id", "created_at"}
                for _k, _v in _order_data.items():
                    if _k not in _SKIP:
                        # Explicit None must be applied (user cleared the field).
                        _target[_k] = _v

        self._order_price_inputs_by_key = (
            self.db.get_cost_price_order_inputs(
                self.company_id,
                region_id=self._region_id,
                manager_id=self.manager_id,
            )
            or {}
        )
        # Packaging, suppliers, categories: no force_refresh — use DB layer cache.
        self._packaging = (
            self.db.get_packaging(
                self.company_id,
                region_id=self._region_id,
                manager_id=self.manager_id,
            )
            or []
        )
        self._set_loading_text(f"Пакування: {len(self._packaging)}")
        self._suppliers = (
            self.db.get_suppliers(
                self.company_id,
                self._region_id,
                manager_id=self.manager_id,
            )
            or []
        )
        self._set_loading_text(f"Постачальники: {len(self._suppliers)}")
        self._categories = (
            self.db.get_company_categories(
                self.company_id,
                self._region_id,
                manager_id=self.manager_id,
            )
            or []
        )
        self._set_loading_text(f"Категорії: {len(self._categories)}")
        try:
            all_methods = (
                self.db.get_effective_delivery_methods_for_manager(
                    self.company_id,
                    self.manager_id,
                    region_id=self._region_id,
                )
                or []
            )
            self._delivery_methods = [
                method_row
                for method_row in all_methods
                if isinstance(method_row, dict)
                and (method_row.get("enabled") is not False)
            ]
        except Exception:
            self._delivery_methods = []
        self._set_loading_text("Довідники...")
        self._price_tiers = (
            self.db.get_effective_region_price_tiers_for_manager(
                self.company_id,
                self.manager_id,
                self._region_id,
            )
            if self._region_id
            else []
        )

        self._product_by_id = {
            str((product or {}).get("id") or "").strip(): product
            for product in self._products
            if isinstance(product, dict) and (product.get("id") or "")
        }
        self._packaging_by_id = {
            str((packaging or {}).get("id") or "").strip(): packaging
            for packaging in self._packaging
            if isinstance(packaging, dict) and (packaging.get("id") or "")
        }
        self._supplier_by_id = {
            str((supplier or {}).get("id") or "").strip(): supplier
            for supplier in self._suppliers
            if isinstance(supplier, dict) and (supplier.get("id") or "")
        }
        self._category_by_id = {
            str((category or {}).get("id") or "").strip(): category
            for category in self._categories
            if isinstance(category, dict) and (category.get("id") or "")
        }
        # Catalog nodes for hierarchy-level labels (brand / category / series)
        try:
            _cnodes = self.db.get_company_catalog_nodes(self.company_id) or []
            self._node_name_by_id = {
                str(n.get("id") or "").strip(): str(n.get("name") or "").strip()
                for n in _cnodes
                if isinstance(n, dict) and n.get("id") and n.get("name")
            }
            self._node_rows_by_id = {
                str(n.get("id") or "").strip(): n
                for n in _cnodes
                if isinstance(n, dict) and n.get("id")
            }
        except Exception:
            self._node_name_by_id = {}
            self._node_rows_by_id = {}
        self._hydrate_product_level_ids()
        self._refresh_product_label_map()
        try:
            self._ref_data_pulse_ts = self.db.get_reference_data_pulse(self.company_id)
        except Exception:
            pass

        self._build_orders_rows()
        self._sync_unit_costs_to_db()
        self._sync_to_shared_map()

    def _delivery_method_tariff(self, method_row: Optional[dict]):
        row = method_row if isinstance(method_row, dict) else {}
        price = _safe_float(
            (
                row.get("effective_price_per_kg")
                if row.get("effective_price_per_kg") is not None
                else row.get("base_price_per_kg")
            ),
            None,
        )
        ccy = (
            str(
                row.get("effective_price_currency")
                or row.get("base_price_currency")
                or "USD"
            )
            .upper()
            .strip()
        )
        return price, ccy or "USD"

    def _hydrate_product_level_ids(self) -> None:
        """Traverse catalog hierarchy from catalog_node_id to fill level_N_node_id fields.

        Products from the DB often have catalog_node_id (leaf) but not level_1..6_node_id.
        This walk-up fills those fields in-place so _common_items_label can use them.
        """
        node_rows = getattr(self, "_node_rows_by_id", {}) or {}
        if not node_rows:
            return
        for product in self._products or []:
            if not isinstance(product, dict):
                continue
            leaf_id = str(product.get("catalog_node_id") or "").strip()
            if not leaf_id:
                continue
            current = leaf_id
            hops = 0
            while current and hops < 40:
                row = node_rows.get(current)
                if not isinstance(row, dict):
                    break
                node_type = str(row.get("node_type") or "").strip().lower()
                if node_type.startswith("level_"):
                    try:
                        pos = int(node_type.split("_", 1)[1])
                    except Exception:
                        pos = 0
                    if 1 <= pos <= 6:
                        level_key = f"level_{pos}_node_id"
                        if not str(product.get(level_key) or "").strip():
                            product[level_key] = str(row.get("id") or "").strip()
                current = str(row.get("parent_id") or "").strip()
                hops += 1

    def _refresh_product_label_map(self) -> None:
        """Disambiguated names (name + " | Кат: ... | Постач: ...") for products
        that share a name — recompute once after (re)loading products/lookups
        instead of per row. See views/product_naming.py."""
        supplier_by_id = getattr(self, "_supplier_by_id", None) or {}
        category_by_id = getattr(self, "_category_by_id", None) or {}
        if not hasattr(self, "_node_path_cache"):
            self._node_path_cache = {}
        _node_path_of = product_naming.make_node_path_resolver(
            self.db, self._node_path_cache
        )

        def _category_of(p: dict) -> str:
            # node_name_by_id (whole catalog_nodes tree, leaf's OWN name) used
            # to be the last-resort fallback here — wrong, since a product's
            # catalog_node_id is its own LEAF node (often literally named the
            # same as the product), not the category. The real category is
            # the ROOT of that node's path.
            return product_naming.resolve_category_label(
                p, node_path_of=_node_path_of, category_by_id=category_by_id
            )

        def _supplier_of(p: dict) -> str:
            return product_naming.resolve_supplier_label(p, supplier_by_id=supplier_by_id)

        try:
            valid = [p for p in (self._products or []) if isinstance(p, dict) and (p.get("id") or "")]
            suffixes = product_naming.compute_name_suffixes(
                valid, category_of=_category_of, supplier_of=_supplier_of
            )
            self._product_label_map = {
                str(p.get("id")): str(p.get("name") or "").strip() + suffixes.get(str(p.get("id")), "")
                for p in valid
            }
        except Exception:
            self._product_label_map = {}

    def _product_display_name(self, product: dict) -> str:
        pid = str((product or {}).get("id") or "").strip()
        cached = (self._product_label_map or {}).get(pid)
        return cached if cached else str((product or {}).get("name") or "").strip()

    def _common_items_label(self, rows: list) -> str:
        """Returns deepest shared catalog-level name across all items in rows.

        Tries levels 6→1 (deepest first) then falls back to category.
        Returns '' when no common level is found (caller should show count).
        """
        if not rows or len(rows) < 2:
            return ""
        node_names = getattr(self, "_node_name_by_id", {}) or {}
        level_sets: dict[int, set] = {}
        cat_ids: set = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = str((row.get("item") or {}).get("product_id") or "").strip()
            product = (self._product_by_id or {}).get(pid) or {}
            cid = str(
                row.get("category_id") or product.get("category_id") or ""
            ).strip()
            if cid:
                cat_ids.add(cid)
            for pos in range(6, 0, -1):
                nid = str(product.get(f"level_{pos}_node_id") or "").strip()
                if nid:
                    level_sets.setdefault(pos, set()).add(nid)
        for pos in range(6, 0, -1):
            ids = level_sets.get(pos)
            if ids and len(ids) == 1:
                name = node_names.get(next(iter(ids)), "").strip()
                if name:
                    return name
        if len(cat_ids) == 1:
            cat = (self._category_by_id or {}).get(next(iter(cat_ids))) or {}
            cat_name = str(cat.get("name") or "").strip()
            if cat_name:
                return cat_name
        # Last fallback: if all rows have the same product, return the product name directly.
        pids = {
            str((row.get("item") or {}).get("product_id") or "").strip()
            for row in rows
            if str((row.get("item") or {}).get("product_id") or "").strip()
        }
        if len(pids) == 1:
            pid = next(iter(pids))
            product = (self._product_by_id or {}).get(pid) or {}
            pname = self._product_display_name(product)
            if pname:
                return pname
        return ""

    def _order_item_basis_weight(self, item: dict, product: dict) -> float:
        weight_fact = _safe_float((item or {}).get("weight_actual_kg"), None)
        if weight_fact is not None and weight_fact >= 0:
            return float(weight_fact)
        qty = (
            _safe_float((item or {}).get("qty") or (item or {}).get("quantity"), 0.0)
            or 0.0
        )
        weight_unit = _safe_float((item or {}).get("weight_per_unit_kg"), None)
        if weight_unit is None and isinstance(product, dict):
            weight_unit = _safe_float(product.get("weight_kg"), 0.0)
        return max(0.0, float(qty) * float(weight_unit or 0.0))

    def _cost_pivot_currency_for_product(self, product: dict, cache: dict) -> str:
        """Currency to compute this product's cost price in: its own
        currency if purchase price + every sale-tier price share one
        currency, otherwise USD. Re-evaluated fresh per call so a later
        currency change (e.g. a tier switched to a different currency)
        automatically flips the product back to USD next time."""
        pid = str((product or {}).get("id") or "").strip()
        if pid in cache:
            return cache[pid]
        purchase_ccy = (
            str((product or {}).get("purchase_currency") or "USD").upper().strip()
            or "USD"
        )
        tier_currencies = [
            str(t.get("currency") or "USD").upper().strip() or "USD"
            for t in (self._price_tiers or [])
            if isinstance(t, dict) and t.get("enabled", True)
        ]
        pivot = fx_rates.resolve_pivot_currency([purchase_ccy, *tier_currencies])
        cache[pid] = pivot
        return pivot

    def _build_orders_rows(self):
        rows = []
        defaults = self._column_defaults()
        default_calc_ccy = str(defaults.get("log_calc_ccy") or "USD").upper()
        default_fact_ccy = str(defaults.get("log_fact_ccy") or "USD").upper()
        default_new_ccy = str(defaults.get("new_price_ccy") or "USD").upper()
        _rows_pivot_cache: dict = {}

        for order in self._orders:
            if not isinstance(order, dict):
                continue
            items = order.get("items") if isinstance(order.get("items"), list) else []
            if not items:
                items = [
                    {
                        "product_id": str(order.get("product_id") or ""),
                        "product_name": str(order.get("product_name") or ""),
                        "qty": order.get("qty") or order.get("quantity") or 0,
                        "purchase_price": order.get("purchase_price"),
                        "purchase_currency": order.get("purchase_currency") or "USD",
                    }
                ]

            weights = []
            has_any_fact = False
            for item in items:
                product_id = str((item or {}).get("product_id") or "").strip()
                product = self._product_by_id.get(product_id) or {}
                weight_fact = _safe_float((item or {}).get("weight_actual_kg"), None)
                if weight_fact is not None:
                    has_any_fact = True
                weights.append(self._order_item_basis_weight(item, product))

            total_basis = sum(weights) or 0.0
            tariff = _safe_float(order.get("delivery_price_snapshot"), None)
            tariff_ccy = (
                str(order.get("delivery_price_currency_snapshot") or "USD")
                .upper()
                .strip()
                or "USD"
            )

            fact_total = _safe_float(order.get("cp_logistics_fact_total"), None)
            fact_ccy = (
                str(order.get("cp_logistics_fact_currency") or default_fact_ccy)
                .upper()
                .strip()
                or default_fact_ccy
            )
            calc_ccy = (
                str(order.get("cp_logistics_calc_currency") or default_calc_ccy)
                .upper()
                .strip()
                or default_calc_ccy
            )

            for idx, item in enumerate(items):
                product_id = str((item or {}).get("product_id") or "").strip()
                product = self._product_by_id.get(product_id) or {}
                qty = (
                    _safe_float(
                        (item or {}).get("qty") or (item or {}).get("quantity"), 0.0
                    )
                    or 0.0
                )

                purchase_price = _safe_float((item or {}).get("purchase_price"), None)
                purchase_ccy = (
                    str(
                        (item or {}).get("purchase_currency")
                        or product.get("purchase_currency")
                        or "USD"
                    )
                    .upper()
                    .strip()
                    or "USD"
                )
                if purchase_price is None and isinstance(product, dict):
                    purchase_price = _safe_float(product.get("purchase_price"), None)

                pack_price, pack_ccy = self._packaging_price_for_product(product)
                pack_in_purchase = self._convert_currency(
                    pack_price, pack_ccy, purchase_ccy
                )
                if pack_in_purchase is None:
                    pack_in_purchase = 0.0

                unit_full_price = None
                if purchase_price is not None:
                    unit_full_price = float(purchase_price) + float(pack_in_purchase)

                # Prefer explicitly stored amount/full_amount over computed values
                _stored_amount = _safe_float((item or {}).get("amount"), None)
                _stored_full = _safe_float((item or {}).get("full_amount"), None)
                if _stored_amount is not None:
                    computed_amount = _stored_amount
                elif purchase_price is not None:
                    computed_amount = float(purchase_price) * float(qty)
                else:
                    computed_amount = None
                if _stored_full is not None:
                    full_amount = _stored_full
                elif unit_full_price is not None:
                    full_amount = float(unit_full_price) * float(qty)
                else:
                    full_amount = None

                basis_weight = weights[idx] if idx < len(weights) else 0.0
                calc_log_total = None
                if tariff is not None:
                    calc_log_total = float(basis_weight) * float(tariff)
                calc_log_in_target = self._convert_currency(
                    calc_log_total, tariff_ccy, calc_ccy
                )

                fact_share = None
                if fact_total is not None and total_basis > 0:
                    fact_share = float(fact_total) * (
                        float(basis_weight) / float(total_basis)
                    )
                fact_in_target = fact_share

                pivot_ccy = self._cost_pivot_currency_for_product(product, _rows_pivot_cache)

                full_amount_pivot = self._convert_currency(
                    full_amount, purchase_ccy, pivot_ccy
                )
                calc_log_pivot = self._convert_currency(calc_log_total, tariff_ccy, pivot_ccy)
                fact_log_pivot = self._convert_currency(fact_share, fact_ccy, pivot_ccy)
                logistics_for_cv_pivot = (
                    fact_log_pivot if fact_log_pivot is not None else calc_log_pivot
                )

                unit_cost_usd = None
                unit_cost_ccy = pivot_ccy
                if qty > 0 and full_amount_pivot is not None:
                    unit_cost_usd = (
                        float(full_amount_pivot) + float(logistics_for_cv_pivot or 0.0)
                    ) / float(qty)

                order_input_key = f"{str(order.get('id') or id(order))}:{int(idx)}"
                order_input = (
                    self._order_price_inputs_by_key.get(order_input_key)
                    if isinstance(self._order_price_inputs_by_key, dict)
                    else None
                )
                # Read unit_cost_usd: item JSONB takes priority (new path),
                # then fall back to the separate cost_price_order_inputs table (legacy).
                item_unit_cost = _safe_float((item or {}).get("unit_cost_usd"), None)
                item_unit_cost_ccy = (
                    str((item or {}).get("unit_cost_currency") or "").upper().strip()
                )
                stored_unit_cost_usd = (
                    item_unit_cost
                    if item_unit_cost is not None
                    else _safe_float((order_input or {}).get("unit_cost_usd"), None)
                )
                stored_unit_cost_ccy = (
                    item_unit_cost_ccy
                    or str((order_input or {}).get("unit_cost_currency") or "").upper().strip()
                    or "USD"
                )
                if unit_cost_usd is None and stored_unit_cost_usd is not None:
                    unit_cost_usd = float(stored_unit_cost_usd)
                    unit_cost_ccy = stored_unit_cost_ccy

                tier_pairs = []
                for tier in self._price_tiers:
                    sale_price, sale_ccy = self._sales_price_for_tier(product, tier)
                    margin = None
                    if (
                        sale_price is not None
                        and sale_price > 0
                        and unit_cost_usd is not None
                    ):
                        cv_in_sale = self._convert_currency(
                            unit_cost_usd, unit_cost_ccy, sale_ccy
                        )
                        if cv_in_sale is not None and sale_price > 0:
                            margin = (float(sale_price) - float(cv_in_sale)) / float(
                                sale_price
                            )
                    tier_pairs.append(
                        {
                            "tier": tier,
                            "sale_price": sale_price,
                            "sale_ccy": sale_ccy,
                            "margin": margin,
                        }
                    )
                # Read new_price: item JSONB first, then legacy table, then cp_new_price field.
                item_new_price = _safe_float((item or {}).get("new_price"), None)
                if item_new_price is not None:
                    new_price = item_new_price
                elif isinstance(order_input, dict) and "new_price" in order_input:
                    new_price = _safe_float(order_input.get("new_price"), None)
                else:
                    new_price = _safe_float((item or {}).get("cp_new_price"), None)
                # Read new_ccy: same priority order.
                item_new_ccy = (
                    str((item or {}).get("new_price_ccy") or "").upper().strip()
                )
                if item_new_ccy:
                    new_ccy = item_new_ccy
                elif isinstance(order_input, dict) and order_input.get("new_price_ccy"):
                    new_ccy = (
                        str(order_input.get("new_price_ccy") or default_new_ccy)
                        .upper()
                        .strip()
                        or default_new_ccy
                    )
                else:
                    new_ccy = (
                        str(
                            (item or {}).get("cp_new_price_currency") or default_new_ccy
                        )
                        .upper()
                        .strip()
                        or default_new_ccy
                    )
                new_margin = None
                if (
                    new_price is not None
                    and new_price > 0
                    and unit_cost_usd is not None
                ):
                    cv_in_new = self._convert_currency(unit_cost_usd, unit_cost_ccy, new_ccy)
                    if cv_in_new is not None:
                        new_margin = (float(new_price) - float(cv_in_new)) / float(
                            new_price
                        )

                weight_unit_raw = _safe_float(
                    (item or {}).get("weight_per_unit_kg"), None
                )
                if weight_unit_raw is None and isinstance(product, dict):
                    weight_unit_raw = _safe_float(product.get("weight_kg"), 0.0)
                display_calc_weight = max(
                    0.0, float(qty) * float(weight_unit_raw or 0.0)
                )

                rows.append(
                    {
                        "order": order,
                        "order_id": str(order.get("id") or id(order)),
                        "item": item,
                        "line_idx": idx,
                        "date": str(order.get("order_date") or "")[:10],
                        "invoice": str(order.get("invoice_no") or ""),
                        "order_1c": str(order.get("order_no_1c") or ""),
                        "product_name": str(
                            self._product_display_name(product)
                            or (item or {}).get("product_name")
                            or "-"
                        ),
                        "status": str(order.get("status") or "production"),
                        "delivery_name": str(order.get("delivery_type_name") or ""),
                        "comment": str(order.get("comment") or ""),
                        "item_comment": str((item or {}).get("comment") or ""),
                        "qty": qty,
                        "amount": computed_amount,
                        "amount_ccy": purchase_ccy,
                        "full_amount": full_amount,
                        "full_amount_ccy": purchase_ccy,
                        "calc_weight": display_calc_weight,
                        "fact_weight": _safe_float(
                            (item or {}).get("weight_actual_kg"), None
                        ),
                        "tariff": tariff,
                        "tariff_ccy": tariff_ccy,
                        "log_calc": calc_log_in_target,
                        "log_calc_ccy": calc_ccy,
                        "log_fact": fact_in_target,
                        "log_fact_ccy": fact_ccy,
                        "log_fact_order_total": fact_total,
                        "unit_cost_usd": unit_cost_usd,
                        "unit_cost_currency": unit_cost_ccy,
                        "tier_pairs": tier_pairs,
                        "new_price": new_price,
                        "new_price_ccy": new_ccy,
                        "new_margin": new_margin,
                        "category_id": str(product.get("category_id") or "").strip(),
                        "supplier_id": str(
                            order.get("supplier_id") or product.get("supplier_id") or ""
                        ).strip(),
                        "payment": str(order.get("payment_status") or "unpaid"),
                        "row_is_fact_master": idx == 0,
                        "has_any_fact": has_any_fact,
                    }
                )

        self._orders_rows = rows

    def _replace_order_local(self, updated: dict) -> None:
        order_id = str((updated or {}).get("id") or "").strip()
        if not order_id:
            return
        for idx, existing in enumerate(self._orders):
            if not isinstance(existing, dict):
                continue
            if str(existing.get("id") or "").strip() == order_id:
                self._orders[idx] = dict(updated)
                return
        self._orders.append(dict(updated))

    def _refresh_local_rows(self) -> None:
        self._build_orders_rows()
        self._sync_unit_costs_to_db()

    # ──────────────────── shared orders map ──────────────────────────────────

    def _sync_order_to_shared_map(self, oid: str, order: dict) -> None:
        if oid and isinstance(order, dict):
            self._shared_orders_map[oid] = dict(order)
            self._bump_sync_seq()

    def _sync_to_shared_map(self) -> None:
        for i, order in enumerate(self._orders):
            if not isinstance(order, dict):
                continue
            oid = str((order.get("id") or "")).strip()
            if not oid:
                continue
            existing = self._shared_orders_map.get(oid)
            if existing is None:
                self._shared_orders_map[oid] = dict(order)
            else:
                self._orders[i] = dict(existing)

    def _sync_from_shared_map(self) -> list:
        if not self._shared_orders_map:
            return []
        updated: list = []
        # Set to True when items/qty changed — callers use this to decide whether
        # a status-only in-place update is enough or a full rebuild is needed.
        self._last_sync_needs_full_rebuild = False
        # Orders pending outbox flush are locally modified — never overwrite with
        # stale shared-map values (another tab may have re-populated it from DB).
        _pending_ids = {
            str(k)[len("order:") :].strip()
            for k in self._ops_outbox
            if str(k).startswith("order:")
        }
        for i, local in enumerate(self._orders):
            if not isinstance(local, dict):
                continue
            oid = str((local.get("id") or "")).strip()
            if not oid:
                continue
            if oid in _pending_ids:
                continue
            shared = self._shared_orders_map.get(oid)
            if shared is None:
                continue
            # Compare ALL keys from both dicts so changes to keys that exist only
            # in shared (e.g. "items" written by Packaging) are detected.
            all_keys = set(local.keys()) | (set(shared.keys()) - {"__sync_seq__"})
            if any(
                str(shared.get(k) or "") != str(local.get(k) or "") for k in all_keys
            ):
                # If items/qty or any CV-relevant field changed, a full rebuild is
                # required so unit costs and logistics-based calculations update.
                _cv_fields = (
                    "items",
                    "qty",
                    "weight_actual_kg",
                    "cp_logistics_fact_total",
                    "delivery_price_snapshot",
                )
                if any(
                    str(shared.get(f) or "") != str(local.get(f) or "")
                    for f in _cv_fields
                ):
                    self._last_sync_needs_full_rebuild = True
                self._orders[i] = dict(shared)
                updated.append(shared)
        return updated

    def _bump_sync_seq(self) -> None:
        try:
            seq = int(self._shared_orders_map.get("__sync_seq__", 0) or 0)
            self._shared_orders_map["__sync_seq__"] = seq + 1
            _cb = self._shared_orders_map.get("__on_change__")
            if callable(_cb):
                _cb()
        except Exception:
            pass

    def _purge_deleted_orders(self) -> bool:
        """Remove orders deleted in Orders tab. Returns True if any removed."""
        deleted_ids = self._shared_orders_map.get("__deleted_ids__")
        if not deleted_ids or not isinstance(deleted_ids, list):
            return False
        deleted_set = set(deleted_ids)
        before = len(self._orders)
        self._orders = [
            o
            for o in self._orders
            if not (
                isinstance(o, dict) and str((o.get("id") or "")).strip() in deleted_set
            )
        ]
        return len(self._orders) < before

    def _bg_apply_sync(self) -> None:
        if not getattr(self, "_workspace_sync_loop_started", False):
            return
        purged = self._purge_deleted_orders()
        changed = self._sync_from_shared_map()
        if purged:
            self._refresh_local_rows()
            # Use async incremental render instead of blocking _render() to avoid
            # freezing the event loop and racing with other render tasks.
            self._schedule_render(0.1, rebuild_rows=True)
            return
        if not changed:
            return
        # If items/qty changed we need a full rebuild (not just a status chip update).
        needs_full = getattr(self, "_last_sync_needs_full_rebuild", False)
        inplace_only = not needs_full and all(
            str((o.get("id") or "")).strip() in self._order_status_cell_map
            for o in changed
        )
        if inplace_only:
            for o in changed:
                oid = str((o.get("id") or "")).strip()
                self._apply_status_inplace(oid, str(o.get("status") or "production"))
                with contextlib.suppress(Exception):
                    del_name = str(
                        o.get("delivery_type_name") or o.get("delivery_type") or ""
                    )
                    self._apply_delivery_inplace(oid, del_name)
        else:
            self._refresh_local_rows()
            self._schedule_render(0.1, rebuild_rows=True)

    def stop(self) -> None:
        self._stopped = True
        self._async_build_token = int(self._async_build_token or 0) + 1
        self._workspace_outbox_running = False

    async def _bg_sync_loop(self) -> None:
        _last_pulse_check = 0.0
        while not self._stopped:
            try:
                await asyncio.sleep(1.5)
                if self._stopped:
                    return
                seq = int(self._shared_orders_map.get("__sync_seq__", 0) or 0)
                if seq != getattr(self, "_bg_sync_seq", 0):
                    self._bg_sync_seq = seq
                    self._bg_apply_sync()
                # Periodically poll for reference-data changes made from
                # another device/session, even while this tab stays open the
                # whole time (not just when the user switches away and back).
                _now = time.monotonic()
                if (_now - _last_pulse_check) >= 20.0:
                    _last_pulse_check = _now
                    await self._bg_ref_data_check()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def _inject_h_scroll_slot(self) -> None:
        """No-op: top scrollbar is now embedded in the table layout."""

    async def resume(self) -> None:
        # Restore sticky header whenever we return to this tab.
        pg = self._pg()
        _sticky = getattr(pg, "_view_sticky_header", None) if pg else None
        if (
            _sticky is not None
            and getattr(pg, "_active_view", None) == "show_cost_price_orders"
        ):
            _hr = getattr(self, "_sticky_header_ref", None)
            _cb = getattr(self, "_sticky_currency_bar_ref", None)
            _cn = getattr(self, "_sticky_counters_ref", None)
            _fb = getattr(self, "_sticky_filter_bar_ref", None)
            _th = getattr(self, "_sticky_table_header_ref", None)
            if _hr is not None:
                new_controls = [_hr]
                if _cb is not None:
                    new_controls.append(_cb)
                if _cn is not None:
                    new_controls.append(_cn)
                if _fb is not None:
                    new_controls.append(_fb)
                if _th is not None:
                    new_controls.append(_th)
                _sticky.controls.clear()
                _sticky.controls.extend(new_controls)
                with contextlib.suppress(Exception):
                    _sticky.update()
        # Re-render if viewport width changed (screen moved, zoom changed, window resized).
        _cur_w = int(self._get_viewport_width())
        if abs(_cur_w - int(getattr(self, "_last_rendered_width", 0) or 0)) > 10:
            self._last_rendered_width = _cur_w
            self._schedule_render(0.05)
            return

        _ref_pg = self._pg()
        if _ref_pg and hasattr(_ref_pg, "run_task"):
            with contextlib.suppress(Exception):
                _ref_pg.run_task(self._bg_ref_data_check)

        # Only sync when shared map changed since last visit.
        current_seq = int(self._shared_orders_map.get("__sync_seq__", 0) or 0)
        if current_seq == getattr(self, "_resume_last_seq", -1):
            self._inject_h_scroll_slot()
            return
        self._resume_last_seq = current_seq
        purged = self._purge_deleted_orders()
        if purged:
            self._refresh_local_rows()
            self._schedule_render(0.05, rebuild_rows=True)
            self._inject_h_scroll_slot()
            return
        changed = self._sync_from_shared_map()
        if not changed:
            self._inject_h_scroll_slot()
            return
        # If items/qty changed we must do a full rebuild (in-place only patches status).
        needs_full = getattr(self, "_last_sync_needs_full_rebuild", False)
        inplace_only = not needs_full and all(
            str((o.get("id") or "")).strip() in self._order_status_cell_map
            or str((o.get("id") or "")).strip() in self._order_delivery_cell_map
            for o in changed
        )
        if inplace_only:
            for o in changed:
                oid = str((o.get("id") or "")).strip()
                self._apply_status_inplace(oid, str(o.get("status") or "production"))
                del_name = str(
                    o.get("delivery_type_name") or o.get("delivery_type") or ""
                )
                self._apply_delivery_inplace(oid, del_name)
            self._inject_h_scroll_slot()
        else:
            self._refresh_local_rows()
            self._schedule_render(0.05, rebuild_rows=True)
            self._inject_h_scroll_slot()

    async def _bg_ref_data_check(self) -> None:
        """Check if reference data (suppliers/products/packaging/categories)
        changed. Called both right after resume() (tab switch) AND
        periodically from _bg_sync_loop (~every 20s) so a change made on
        another device shows up here even if we never switch away from this
        tab."""
        try:
            ref_pulse = await asyncio.to_thread(
                self.db.get_reference_data_pulse, self.company_id
            )
            if ref_pulse > self._ref_data_pulse_ts:
                await asyncio.to_thread(self._load)
                self._ref_data_pulse_ts = ref_pulse
                self._schedule_render(0.0, rebuild_rows=True)
        except Exception:
            pass

    def _save_and_update_status(self, order: dict, new_status: str) -> None:
        updated = self._save_order_fields(
            order, 0, {}, {"status": new_status}, rerender=False
        )
        if updated:
            oid = str(updated.get("id") or "").strip()
            self._apply_status_inplace(oid, new_status)

    def _apply_status_inplace(self, order_id: str, new_status: str) -> None:
        refs = self._order_status_cell_map.get(order_id)
        if not refs:
            return
        container, text_widget = refs
        st_bg = self.STATUS_BG.get(new_status, _PANEL)
        st_fg = self.STATUS_FG.get(new_status, _TEXT_2)
        container.bgcolor = st_bg
        text_widget.value = self.STATUS_LABELS.get(new_status, new_status)
        text_widget.color = st_fg
        with contextlib.suppress(Exception):
            container.update()

    def _save_and_update_delivery(self, order: dict, order_patch: dict) -> None:
        updated = self._save_order_fields(order, 0, {}, order_patch, rerender=False)
        if updated:
            oid = str(updated.get("id") or "").strip()
            del_name = str(order_patch.get("delivery_type_name") or "")
            self._apply_delivery_inplace(oid, del_name)

    def _apply_delivery_inplace(self, order_id: str, delivery_name: str) -> None:
        refs = self._order_delivery_cell_map.get(order_id)
        if not refs:
            return
        container, text_widget = refs
        raw = str(delivery_name or "").strip()
        if raw:
            idx = sum(ord(ch) for ch in raw) % len(self.DELIVERY_PALETTE)
            del_bg = self.DELIVERY_PALETTE[idx]
        else:
            del_bg = _PANEL
        container.bgcolor = del_bg
        text_widget.value = raw or "-"
        with contextlib.suppress(Exception):
            container.update()

    def _recalc_order_cv(self, order: dict) -> list[dict]:
        """Re-run CV calculations for a single order. Returns list of per-item dicts."""
        defaults = self._column_defaults()
        default_calc_ccy = str(defaults.get("log_calc_ccy") or "USD").upper()
        default_fact_ccy = str(defaults.get("log_fact_ccy") or "USD").upper()

        items = order.get("items") if isinstance(order.get("items"), list) else []
        if not items:
            items = [
                {
                    "product_id": str(order.get("product_id") or ""),
                    "qty": order.get("qty") or 0,
                }
            ]

        weights = []
        for item in items:
            product_id = str((item or {}).get("product_id") or "").strip()
            product = self._product_by_id.get(product_id) or {}
            weights.append(self._order_item_basis_weight(item, product))

        total_basis = sum(weights) or 0.0
        tariff = _safe_float(order.get("delivery_price_snapshot"), None)
        tariff_ccy = (
            str(order.get("delivery_price_currency_snapshot") or "USD").upper().strip()
            or "USD"
        )
        fact_total = _safe_float(order.get("cp_logistics_fact_total"), None)
        fact_ccy = (
            str(order.get("cp_logistics_fact_currency") or default_fact_ccy)
            .upper()
            .strip()
            or default_fact_ccy
        )
        calc_ccy = (
            str(order.get("cp_logistics_calc_currency") or default_calc_ccy)
            .upper()
            .strip()
            or default_calc_ccy
        )

        _pivot_cache: dict = {}
        results = []
        for idx, item in enumerate(items):
            product_id = str((item or {}).get("product_id") or "").strip()
            product = self._product_by_id.get(product_id) or {}
            qty = (
                _safe_float(
                    (item or {}).get("qty") or (item or {}).get("quantity"), 0.0
                )
                or 0.0
            )
            basis_weight = weights[idx] if idx < len(weights) else 0.0

            calc_log_total = (
                float(basis_weight) * float(tariff) if tariff is not None else None
            )
            calc_log_in_target = self._convert_currency(
                calc_log_total, tariff_ccy, calc_ccy
            )

            fact_share = None
            if fact_total is not None and total_basis > 0:
                fact_share = float(fact_total) * (
                    float(basis_weight) / float(total_basis)
                )

            purchase_ccy = (
                str(
                    (item or {}).get("purchase_currency")
                    or product.get("purchase_currency")
                    or "USD"
                )
                .upper()
                .strip()
                or "USD"
            )
            pack_price, pack_ccy = self._packaging_price_for_product(product)
            pack_in_purchase = (
                self._convert_currency(pack_price, pack_ccy, purchase_ccy) or 0.0
            )
            purchase_price = _safe_float((item or {}).get("purchase_price"), None)
            if purchase_price is None and isinstance(product, dict):
                purchase_price = _safe_float(product.get("purchase_price"), None)

            unit_full_price = None
            if purchase_price is not None:
                unit_full_price = float(purchase_price) + float(pack_in_purchase)

            _stored_full = _safe_float((item or {}).get("full_amount"), None)
            _stored_amount = _safe_float((item or {}).get("amount"), None)
            if _stored_full is not None:
                full_amount = _stored_full
            elif unit_full_price is not None:
                full_amount = float(unit_full_price) * float(qty)
            else:
                full_amount = None

            pivot_ccy = self._cost_pivot_currency_for_product(product, _pivot_cache)

            full_amount_pivot = self._convert_currency(full_amount, purchase_ccy, pivot_ccy)
            calc_log_pivot = self._convert_currency(calc_log_total, tariff_ccy, pivot_ccy)
            fact_log_pivot = self._convert_currency(fact_share, fact_ccy, pivot_ccy)
            logistics_for_cv_pivot = (
                fact_log_pivot if fact_log_pivot is not None else calc_log_pivot
            )

            unit_cost_usd = None
            unit_cost_ccy = pivot_ccy
            if qty > 0 and full_amount_pivot is not None:
                unit_cost_usd = (
                    float(full_amount_pivot) + float(logistics_for_cv_pivot or 0.0)
                ) / float(qty)

            item_unit_cost = _safe_float((item or {}).get("unit_cost_usd"), None)
            item_unit_cost_ccy = (
                str((item or {}).get("unit_cost_currency") or "").upper().strip()
            )
            order_input_key = f"{str(order.get('id') or id(order))}:{idx}"
            order_input = (
                self._order_price_inputs_by_key.get(order_input_key)
                if isinstance(self._order_price_inputs_by_key, dict)
                else None
            )
            stored_unit_cost_usd = (
                item_unit_cost
                if item_unit_cost is not None
                else _safe_float((order_input or {}).get("unit_cost_usd"), None)
            )
            stored_unit_cost_ccy = (
                item_unit_cost_ccy
                or str((order_input or {}).get("unit_cost_currency") or "").upper().strip()
                or "USD"
            )
            if unit_cost_usd is None and stored_unit_cost_usd is not None:
                unit_cost_usd = float(stored_unit_cost_usd)
                unit_cost_ccy = stored_unit_cost_ccy

            tier_pairs = []
            for tier in self._price_tiers:
                sale_price, sale_ccy = self._sales_price_for_tier(product, tier)
                margin = None
                if (
                    sale_price is not None
                    and sale_price > 0
                    and unit_cost_usd is not None
                ):
                    cv_in_sale = self._convert_currency(
                        unit_cost_usd, unit_cost_ccy, sale_ccy
                    )
                    if cv_in_sale is not None and sale_price > 0:
                        margin = (float(sale_price) - float(cv_in_sale)) / float(
                            sale_price
                        )
                tier_pairs.append(
                    {
                        "tier": tier,
                        "sale_price": sale_price,
                        "sale_ccy": sale_ccy,
                        "margin": margin,
                    }
                )

            results.append(
                {
                    "line_idx": idx,
                    "log_calc": calc_log_in_target,
                    "log_calc_ccy": calc_ccy,
                    "log_fact": fact_share,
                    "log_fact_ccy": fact_ccy,
                    "unit_cost_usd": unit_cost_usd,
                    "unit_cost_currency": unit_cost_ccy,
                    "tier_pairs": tier_pairs,
                }
            )
        return results

    def _apply_cv_inplace(self, order_id: str) -> bool:
        """Recalculate CV for one order and update only the affected cells. Returns True if applied."""
        order = next(
            (
                o
                for o in self._orders
                if isinstance(o, dict) and str(o.get("id") or "").strip() == order_id
            ),
            None,
        )
        if order is None:
            return False
        defaults = self._column_defaults()
        orders_unit_cost_ccy = (
            str(defaults.get("orders_unit_cost_ccy") or "USD").upper().strip() or "USD"
        )
        target_margin_cache: dict = {}

        try:
            item_results = self._recalc_order_cv(order)
        except Exception:
            return False

        # Update _orders_rows in-place so in-memory data stays consistent
        if isinstance(getattr(self, "_orders_rows", None), list):
            for row in self._orders_rows:
                if not isinstance(row, dict):
                    continue
                if str((row.get("order") or {}).get("id") or "").strip() != order_id:
                    continue
                idx = int(row.get("line_idx") or 0)
                result = next((r for r in item_results if r["line_idx"] == idx), None)
                if result:
                    row["log_calc"] = result["log_calc"]
                    row["log_fact"] = result["log_fact"]
                    row["unit_cost_usd"] = result["unit_cost_usd"]
                    row["unit_cost_currency"] = result.get("unit_cost_currency", "USD")
                    row["tier_pairs"] = result["tier_pairs"]

        cells_to_update = []
        for result in item_results:
            idx = result["line_idx"]
            key = f"{order_id}:{idx}"
            refs = self._order_cv_cell_map.get(key)
            if not refs:
                continue

            item_bg = refs.get("item_bg") or _PANEL

            lc_cell = refs.get("log_calc_cell")
            if (
                lc_cell
                and hasattr(lc_cell, "content")
                and hasattr(lc_cell.content, "value")
            ):
                lc_cell.content.value = self._fmt_money(result["log_calc"])
                cells_to_update.append(lc_cell)

            lf_cell = refs.get("log_fact_cell")
            if (
                lf_cell
                and hasattr(lf_cell, "content")
                and hasattr(lf_cell.content, "value")
            ):
                lf_val = result["log_fact"]
                lf_cell.content.value = self._fmt_money(lf_val)
                _new_lf_bg = _ERR_BG if lf_val is None else item_bg
                lf_cell.bgcolor = _new_lf_bg
                if hasattr(lf_cell.content, "fill_color"):
                    lf_cell.content.fill_color = _new_lf_bg
                cells_to_update.append(lf_cell)

            # Background-only update for main editable log_fact cell (no value overwrite)
            main_lf_bg_cell = refs.get("main_lf_bg_cell")
            if (
                main_lf_bg_cell
                and hasattr(main_lf_bg_cell, "content")
                and hasattr(main_lf_bg_cell.content, "fill_color")
            ):
                lf_val = result["log_fact"]
                _main_lf_new_bg = _ERR_BG if lf_val is None else item_bg
                main_lf_bg_cell.bgcolor = _main_lf_new_bg
                main_lf_bg_cell.content.fill_color = _main_lf_new_bg
                cells_to_update.append(main_lf_bg_cell)

            # Fact weight cell: update background color based on current item weight
            fw_cell = refs.get("fact_weight_cell")
            if (
                fw_cell
                and hasattr(fw_cell, "content")
                and hasattr(fw_cell.content, "fill_color")
            ):
                _fw_items = order.get("items") or []
                _fw_item = _fw_items[idx] if idx < len(_fw_items) else {}
                _fw_raw = (_fw_item or {}).get("weight_actual_kg")
                _fw_bg = _ERR_BG if _fw_raw is None else item_bg
                fw_cell.bgcolor = _fw_bg
                fw_cell.content.fill_color = _fw_bg
                cells_to_update.append(fw_cell)

            uc_cell = refs.get("unit_cost_cell")
            if (
                uc_cell
                and hasattr(uc_cell, "content")
                and hasattr(uc_cell.content, "value")
            ):
                uc_val = self._convert_currency(
                    result["unit_cost_usd"],
                    result.get("unit_cost_currency", "USD"),
                    orders_unit_cost_ccy,
                )
                uc_cell.content.value = self._fmt_money(uc_val, orders_unit_cost_ccy)
                cells_to_update.append(uc_cell)

            category_id = ""
            if isinstance(getattr(self, "_orders_rows", None), list):
                for row in self._orders_rows:
                    if (
                        str((row.get("order") or {}).get("id") or "").strip()
                        == order_id
                        and int(row.get("line_idx") or 0) == idx
                    ):
                        category_id = row.get("category_id") or ""
                        break
            if category_id not in target_margin_cache:
                target_margin_cache[category_id] = self._target_margin(category_id)
            target_margin = target_margin_cache[category_id]

            tier_cells = refs.get("tier_cells") or []
            visible_ids = set(self._visible_orders_tier_ids())
            all_tiers = [t for t in (self._price_tiers or []) if isinstance(t, dict)]
            tier_pair_map = {
                str((p.get("tier") or {}).get("id") or ""): p
                for p in (result["tier_pairs"] or [])
            }
            for _tier_c, _sale_txt, _margin_txt in tier_cells:
                # Match tier cell by position in visible tiers
                pass
            # Simpler: iterate visible tiers in order, match with tier_cells list
            tc_idx = 0
            for tier in all_tiers:
                tier_id = str((tier or {}).get("id") or "").strip()
                if tier_id not in visible_ids:
                    continue
                if tc_idx >= len(tier_cells):
                    break
                _tier_c, _sale_txt, _margin_txt = tier_cells[tc_idx]
                tc_idx += 1
                pair = tier_pair_map.get(tier_id) or {}
                sale = _safe_float(pair.get("sale_price"), None)
                margin = pair.get("margin")
                bg_m, fg_m = self._margin_colors(margin, target_margin)
                _sale_txt.value = self._fmt_money(sale, pair.get("sale_ccy") or "USD")
                _margin_txt.value = "-" if margin is None else f"{margin * 100:.1f}%"
                _margin_txt.color = fg_m
                _tier_c.bgcolor = bg_m
                cells_to_update.append(_tier_c)

        if not cells_to_update:
            return False

        pg = self._pg()
        if pg:
            with contextlib.suppress(Exception):
                # Include the sync indicator so its pending "Локально" update is
                # flushed in the same batch as the recalculated cells.
                _ind = self._sync_indicator_container
                if _ind is not None and _ind not in cells_to_update:
                    cells_to_update.append(_ind)
                pg.update(*cells_to_update)
        return True

    def _save_item_amount(
        self, order: dict, line_idx: int, row: dict, raw_value
    ) -> None:
        """Store amount explicitly; also derive purchase_price = amount / qty."""
        entered = _safe_float(raw_value, None)
        if entered is None:
            return
        qty = _safe_float(row.get("qty"), 0.0) or 0.0
        patch: dict = {"amount": entered}
        if qty > 0:
            patch["purchase_price"] = entered / qty
        self._save_order_fields(order, line_idx, patch)

    def _save_item_full_amount(
        self, order: dict, line_idx: int, row: dict, raw_value
    ) -> None:
        """Store full_amount explicitly; derive purchase_price = (full_amount/qty) - pack."""
        entered = _safe_float(raw_value, None)
        if entered is None:
            return
        qty = _safe_float(row.get("qty"), 0.0) or 0.0
        patch: dict = {"full_amount": entered}
        if qty > 0:
            pid = str((row.get("item") or {}).get("product_id") or "").strip()
            product = self._product_by_id.get(pid) or {}
            purchase_ccy = str(
                (row.get("item") or {}).get("purchase_currency")
                or product.get("purchase_currency")
                or "USD"
            ).upper()
            pack_price, pack_ccy = self._packaging_price_for_product(product)
            pack_in_purchase = (
                self._convert_currency(pack_price, pack_ccy, purchase_ccy) or 0.0
            )
            purchase_price = max(0.0, (entered / qty) - float(pack_in_purchase or 0.0))
            patch["purchase_price"] = purchase_price
        self._save_order_fields(order, line_idx, patch)

    def _save_qty_and_recalc(
        self, order: dict, line_idx: int, row: dict, raw_value
    ) -> None:
        """Save qty and recalculate amount/full_amount from stored purchase_price."""
        new_qty = _safe_float(raw_value, None)
        if new_qty is None:
            return
        patch: dict = {"qty": new_qty}
        item = row.get("item") or {}
        purchase_price = _safe_float(item.get("purchase_price"), None)
        if purchase_price is None:
            # fallback: derive from stored amount / old qty
            old_qty = _safe_float(row.get("qty"), 0.0) or 0.0
            stored_amt = _safe_float(item.get("amount"), None) or row.get("amount")
            if stored_amt is not None and old_qty > 0:
                purchase_price = stored_amt / old_qty
        if purchase_price is not None and new_qty >= 0:
            pid = str(item.get("product_id") or "").strip()
            product = self._product_by_id.get(pid) or {}
            purchase_ccy = str(
                item.get("purchase_currency")
                or product.get("purchase_currency")
                or "USD"
            ).upper()
            pack_price, pack_ccy = self._packaging_price_for_product(product)
            pack_in_purchase = (
                self._convert_currency(pack_price, pack_ccy, purchase_ccy) or 0.0
            )
            patch["amount"] = purchase_price * new_qty
            patch["full_amount"] = (
                purchase_price + float(pack_in_purchase or 0.0)
            ) * new_qty
        self._save_order_fields(order, line_idx, patch)

    # ─────────────────────────────────────────────────────────────────────────

    def _sync_unit_costs_to_db(self) -> None:
        """Update calculated unit_cost_usd in-memory only.

        unit_cost_usd is a derived field (recalculated from price inputs on every
        _build_orders_rows call). Writing it to DB/outbox would flood the outbox with
        all 200+ orders on every render. Instead we update self._orders and the shared
        map in-place — no outbox entry, no DB write.
        """
        if not isinstance(self._order_price_inputs_by_key, dict):
            self._order_price_inputs_by_key = {}
        for row in self._orders_rows or []:
            if not isinstance(row, dict):
                continue
            order_id = str(row.get("order_id") or "").strip()
            if not order_id:
                continue
            try:
                line_idx = int(row.get("line_idx") or 0)
            except Exception:
                line_idx = 0
            unit_cost_usd = _safe_float(row.get("unit_cost_usd"), None)
            if unit_cost_usd is None:
                continue
            unit_cost_usd = round(float(unit_cost_usd), 6)
            unit_cost_ccy = (
                str(row.get("unit_cost_currency") or "USD").upper().strip() or "USD"
            )

            key = f"{order_id}:{line_idx}"
            prev_state = self._order_price_inputs_by_key.get(key)
            prev_unit_cost = _safe_float((prev_state or {}).get("unit_cost_usd"), None)
            prev_unit_cost_ccy = str(
                (prev_state or {}).get("unit_cost_currency") or "USD"
            ).upper().strip() or "USD"
            if (
                prev_unit_cost is not None
                and round(float(prev_unit_cost), 6) == unit_cost_usd
                and prev_unit_cost_ccy == unit_cost_ccy
            ):
                continue

            merged_state = dict(prev_state) if isinstance(prev_state, dict) else {}
            merged_state["unit_cost_usd"] = unit_cost_usd
            merged_state["unit_cost_currency"] = unit_cost_ccy
            self._order_price_inputs_by_key[key] = merged_state

            # Update in-memory only — derived field, no DB/outbox needed.
            for idx, existing in enumerate(self._orders):
                if not isinstance(existing, dict):
                    continue
                if str(existing.get("id") or "").strip() != order_id:
                    continue
                items = list(existing.get("items") or [])
                while len(items) <= line_idx:
                    items.append({})
                item = dict(items[line_idx] or {})
                if (
                    _safe_float(item.get("unit_cost_usd"), None) == unit_cost_usd
                    and str(item.get("unit_cost_currency") or "USD").upper().strip() == unit_cost_ccy
                ):
                    break
                item["unit_cost_usd"] = unit_cost_usd
                item["unit_cost_currency"] = unit_cost_ccy
                # Also expose as calc override so orders_view.py reads this value
                # directly instead of falling back to delivery_price_snapshot tariff.
                item["unit_cost_calc_usd"] = unit_cost_usd
                item["unit_cost_calc_currency"] = unit_cost_ccy
                items[line_idx] = item
                updated_order = dict(existing)
                updated_order["items"] = items
                self._orders[idx] = updated_order
                self._sync_order_to_shared_map(order_id, updated_order)
                break

    def _toggle_order_selection(self, order_id: str) -> None:
        if order_id in self._orders_selected:
            self._orders_selected.discard(order_id)
        else:
            self._orders_selected.add(order_id)
        self._update_merge_btn_state()

    def _update_merge_btn_state(self) -> None:
        btn = self._merge_btn_ref
        if btn is None:
            return
        sel_count = len(self._orders_selected)
        active = sel_count >= 2
        merge_color = _CHIP_T if active else _MUTED
        btn.bgcolor = merge_color
        btn.border = ft.border.all(1, merge_color)
        btn.tooltip = (
            f"Об'єднати {sel_count} замовлень"
            if active
            else "Вибери 2+ замовлення для об'єднання"
        )
        btn.on_click = (
            self._guard_ui_action(
                "merge_orders", self._merge_selected_orders, "Помилка об'єднання"
            )
            if active
            else None
        )
        row_ctrl = btn.content
        if isinstance(row_ctrl, ft.Row) and len(row_ctrl.controls) >= 2:
            txt = row_ctrl.controls[1]
            if isinstance(txt, ft.Text):
                txt.value = f"Об'єднати ({sel_count})" if active else "Об'єднати"
        with contextlib.suppress(Exception):
            btn.update()

    def _toggle_orders_overflow(self) -> None:
        self._orders_overflow_expanded = not bool(self._orders_overflow_expanded)
        exp = bool(self._orders_overflow_expanded)
        lc = getattr(self, "_overflow_left_col_ref", None)
        rc = getattr(self, "_overflow_right_col_ref", None)
        btn = getattr(self, "_overflow_btn_ref", None)
        if lc is not None and rc is not None:
            lc.visible = exp
            rc.visible = exp
            if btn is not None:
                btn.icon = ft.Icons.REMOVE if exp else ft.Icons.ADD
                btn.tooltip = (
                    "Згорнути додаткові замовлення"
                    if exp
                    else "Показати додаткові замовлення"
                )
            try:
                lc.update()
                rc.update()
                if btn is not None:
                    btn.update()
                return
            except Exception:
                pass
        # Fallback: full re-render (refs not available — first render after restart)
        self._render()

    def _merge_selected_orders(self) -> None:
        if len(self._orders_selected) < 2:
            return
        import uuid as _uuid

        gid = str(_uuid.uuid4())
        order_ids = list(self._orders_selected)
        self._orders_merge_groups[gid] = {
            "order_ids": order_ids,
            "comment": "",
            "tariff": None,
            "log_fact": None,
        }
        self._orders_selected.clear()
        self._save_merge_groups_to_state()
        self._restart_tab()

    def _unmerge_group(self, gid: str) -> None:
        self._orders_merge_groups.pop(gid, None)
        self._expanded_merge_groups.discard(gid)
        self._save_merge_groups_to_state()
        self._restart_tab()

    # ── SURGICAL MERGE/UNMERGE (instant, no full rebuild) ──────────────────────

    def _apply_merge_surgical(self, gid: str, order_ids: list) -> bool:
        """Hide merged order rows, insert a functional group header. Returns True on success."""
        lc = self._data_left_col_ref
        rc = self._data_right_col_ref
        if lc is None or rc is None:
            return False

        widths = list(getattr(self, "_last_widths", None) or [42, 34] + [60] * 20)
        frozen_count = int(getattr(self, "_last_frozen_count", 2))
        row_height = int(getattr(self, "_last_row_height", 34))
        tf_vpad = int(getattr(self, "_last_tf_vpad", 10))
        reorder_fn = getattr(self, "_last_reorder_base_cells", None)
        visible_tiers = list(getattr(self, "_last_visible_tiers", []))
        merge_bg = _SOFT_P
        n = len(widths)

        order_id_strs = [str(oid) for oid in order_ids]
        order_id_set = set(order_id_strs)

        # Find insert position — search BOTH main column and overflow column.
        # _order_left_row_refs now tracks ALL rows regardless of visible_limit zone.
        def _find_min_pos(col):
            pos_min = None
            for oid in order_id_strs:
                for row in self._order_left_row_refs.get(oid) or []:
                    try:
                        p = col.controls.index(row)
                        if pos_min is None or p < pos_min:
                            pos_min = p
                    except ValueError:
                        pass
            return pos_min

        min_pos = _find_min_pos(lc)
        if min_pos is None:
            # Try the overflow column — only if Python and Flutter are in sync.
            # If not synced, _safe_flet_update would send 70k changes → 255s hang.
            if not getattr(self, "_overflow_synced_with_flutter", True):
                return False  # overflow stale → caller does page reload
            ovf_lc = getattr(self, "_overflow_left_col_ref", None)
            ovf_rc = getattr(self, "_overflow_right_col_ref", None)
            if ovf_lc is not None:
                min_pos = _find_min_pos(ovf_lc)
                if min_pos is not None:
                    lc = ovf_lc
                    rc = ovf_rc

        if min_pos is None:
            return False  # order rows not found in any column (refs not built yet)

        # Hide all order rows (both header rows and sub-item columns)
        for oid in order_id_strs:
            for row in self._order_left_row_refs.get(oid) or []:
                row.visible = False
            for row in self._order_right_row_refs.get(oid) or []:
                row.visible = False

        # Aggregate data for group header
        gdata = self._orders_merge_groups.get(gid) or {}
        all_rows = [
            r for r in self._orders_rows if str(r.get("order_id") or "") in order_id_set
        ]
        all_orders = [
            o
            for o in self._orders
            if isinstance(o, dict) and str(o.get("id") or "") in order_id_set
        ]
        grp_date = min(
            (str(o.get("order_date") or "")[:10] for o in all_orders), default=""
        )
        grp_invoices = list(
            dict.fromkeys(
                str(o.get("invoice_no") or "")
                for o in all_orders
                if o.get("invoice_no")
            )
        )
        grp_zp1c = list(
            dict.fromkeys(
                str(o.get("order_no_1c") or "")
                for o in all_orders
                if o.get("order_no_1c")
            )
        )
        grp_label = self._common_items_label(all_rows) or f"{len(order_ids)} Замовлень"
        grp_qty = sum(_safe_float(r.get("qty"), 0.0) or 0.0 for r in all_rows)
        grp_amount = sum((_safe_float(r.get("amount"), 0.0) or 0.0) for r in all_rows)
        grp_full = sum(
            (_safe_float(r.get("full_amount"), 0.0) or 0.0) for r in all_rows
        )
        grp_calc_w = sum(
            _safe_float(r.get("calc_weight"), 0.0) or 0.0 for r in all_rows
        )
        grp_has_fact = any(r.get("fact_weight") is not None for r in all_rows)
        grp_fact_w = sum(
            _safe_float(r.get("fact_weight"), 0.0) or 0.0
            for r in all_rows
            if r.get("fact_weight") is not None
        )
        grp_log_calc = sum(
            _safe_float(r.get("log_calc"), 0.0) or 0.0
            for r in all_rows
            if r.get("log_calc") is not None
        )
        grp_tariff = _safe_float(gdata.get("tariff"), None)
        grp_log_fact = _safe_float(gdata.get("log_fact"), None)
        amount_ccy = (all_rows[0].get("amount_ccy") if all_rows else None) or "USD"

        # Cell helper: simple text cell
        def _ct(text, w, bg=merge_bg, align=ft.TextAlign.RIGHT):
            if not w:
                return ft.Container(width=0, height=0, visible=False)
            return ft.Container(
                width=w,
                height=row_height,
                bgcolor=bg,
                border=ft.border.all(1, _BORDER),
                alignment=(
                    ft.alignment.Alignment(1, 0)
                    if align == ft.TextAlign.RIGHT
                    else (
                        ft.alignment.Alignment(0, 0)
                        if align == ft.TextAlign.CENTER
                        else ft.alignment.Alignment(-1, 0)
                    )
                ),
                padding=ft.padding.symmetric(horizontal=4),
                content=ft.Text(
                    str(text or ""),
                    size=8,
                    no_wrap=True,
                    text_align=align,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
            )

        # Cell helper: TextField input cell
        def _ctf(val, w, bg, on_value):
            if not w:
                return ft.Container(width=0, height=0, visible=False)
            tf = ft.TextField(
                value=str(val or ""),
                text_size=8,
                border=ft.InputBorder.NONE,
                text_align=ft.TextAlign.RIGHT,
                expand=True,
                disabled=self.is_read_only,
            )

            def _commit(e, _cb=on_value):
                with contextlib.suppress(Exception):
                    _cb(e.control.value)

            tf.on_submit = _commit
            tf.on_blur = _commit
            return ft.Container(
                width=w,
                height=row_height,
                bgcolor=bg,
                border=ft.border.all(1, _BORDER),
                padding=ft.padding.symmetric(horizontal=2, vertical=tf_vpad),
                content=tf,
            )

        # Expand/collapse button for sub-orders
        sub_left = ft.Column(spacing=0, visible=False)
        sub_right = ft.Column(spacing=0, visible=False)
        expand_btn = ft.IconButton(
            icon=ft.Icons.ADD,
            icon_size=14,
            tooltip="Розгорнути підзамовлення",
            style=ft.ButtonStyle(
                padding=ft.padding.all(0), shape=ft.RoundedRectangleBorder(radius=4)
            ),
            width=widths[1] if n > 1 else 34,
            height=row_height,
        )

        def _toggle_grp(_e=None, _g=gid, _sl=sub_left, _sr=sub_right, _btn=expand_btn):
            if _g in self._expanded_merge_groups:
                self._expanded_merge_groups.discard(_g)
            else:
                self._expanded_merge_groups.add(_g)
            exp = _g in self._expanded_merge_groups
            _sl.visible = exp
            _sr.visible = exp
            _btn.icon = ft.Icons.REMOVE if exp else ft.Icons.ADD
            _btn.tooltip = (
                "Згорнути підзамовлення" if exp else "Розгорнути підзамовлення"
            )
            _safe_flet_update(_sl)
            _safe_flet_update(_sr)
            _safe_flet_update(_btn)

        expand_btn.on_click = self._guard_ui_action(
            "merge_toggle", _toggle_grp, "Помилка"
        )

        # Build cells in base_column_keys order (matches how build_orders_table does it)
        mc = [
            # [0] select → unlink button
            ft.Container(
                width=widths[0] if n > 0 else 42,
                height=row_height,
                bgcolor=merge_bg,
                border=ft.border.all(1, _BORDER),
                alignment=ft.Alignment(0, 0),
                content=ft.IconButton(
                    icon=ft.Icons.LINK_OFF,
                    icon_size=12,
                    tooltip="Роз'єднати",
                    style=ft.ButtonStyle(
                        padding=ft.padding.all(0),
                        shape=ft.RoundedRectangleBorder(radius=4),
                    ),
                    width=widths[0] if n > 0 else 42,
                    height=row_height,
                    disabled=self.is_read_only,
                    on_click=self._guard_ui_action(
                        "merge_unlink",
                        lambda _g=gid: self._unmerge_group(_g),
                        "Помилка",
                    ),
                ),
            ),
            # [1] expand button
            ft.Container(
                width=widths[1] if n > 1 else 34,
                height=row_height,
                bgcolor=merge_bg,
                border=ft.border.all(1, _BORDER),
                alignment=ft.Alignment(0, 0),
                content=expand_btn,
            ),
            # [2] date
            _ct(grp_date, widths[2] if n > 2 else 0),
            # [3] invoice
            _ct(
                "+".join(grp_invoices),
                widths[3] if n > 3 else 0,
                align=ft.TextAlign.CENTER,
            ),
            # [4] order1c
            _ct(
                "+".join(grp_zp1c), widths[4] if n > 4 else 0, align=ft.TextAlign.CENTER
            ),
            # [5] product label
            _ct(
                grp_label,
                widths[5] if n > 5 else 0,
                bg=_SOFT_P if len(order_ids) > 1 else merge_bg,
                align=ft.TextAlign.CENTER,
            ),
            # [6] status (simplified text)
            _ct(
                (all_orders[0].get("status") or "—") if all_orders else "—",
                widths[6] if n > 6 else 0,
            ),
            # [7] delivery (simplified text)
            _ct(
                (all_orders[0].get("delivery_type_name") or "—") if all_orders else "—",
                widths[7] if n > 7 else 0,
                align=ft.TextAlign.LEFT,
            ),
            # [8] comment
            _ct(
                str(gdata.get("comment") or ""),
                widths[8] if n > 8 else 0,
                align=ft.TextAlign.LEFT,
            ),
            # [9] qty
            _ct(self._fmt_money(grp_qty), widths[9] if n > 9 else 0),
            # [10] amount
            _ct(
                self._fmt_money(grp_amount if grp_amount > 0 else None, amount_ccy),
                widths[10] if n > 10 else 0,
            ),
            # [11] full_amount
            _ct(
                self._fmt_money(grp_full if grp_full > 0 else None, amount_ccy),
                widths[11] if n > 11 else 0,
            ),
            # [12] calc_weight
            _ct(self._fmt_money(grp_calc_w, "kg"), widths[12] if n > 12 else 0),
            # [13] fact_weight TF
            _ctf(
                (
                    format_grouped(grp_fact_w, decimals=2, trim=False)
                    if grp_has_fact and grp_fact_w is not None
                    else ""
                ),
                widths[13] if n > 13 else 0,
                _ERR_BG if not grp_has_fact else merge_bg,
                lambda raw, _g=gid: self._distribute_merge_fact_weight(
                    _g, _safe_float(raw, None)
                ),
            ),
            # [14] tariff TF
            _ctf(
                self._fmt_money(grp_tariff) if grp_tariff is not None else "",
                widths[14] if n > 14 else 0,
                merge_bg,
                lambda raw, _g=gid: self._cascade_merge_tariff(
                    _g, _safe_float(raw, None)
                ),
            ),
            # [15] log_calc text
            _ct(
                self._fmt_money(grp_log_calc if all_rows else None),
                widths[15] if n > 15 else 0,
            ),
            # [16] log_fact TF
            _ctf(
                (
                    format_grouped(grp_log_fact, decimals=2, trim=False)
                    if grp_log_fact is not None
                    else ""
                ),
                widths[16] if n > 16 else 0,
                merge_bg,
                lambda raw, _g=gid: self._distribute_merge_log_fact(
                    _g, _safe_float(raw, None)
                ),
            ),
            # [17] unit_cost
            _ct("-", widths[17] if n > 17 else 0),
        ]
        # Tier columns — ALL tiers (visible get proper width, invisible get placeholder)
        _all_tier_widths_m = getattr(self, "_last_all_tier_widths", {}) or {}
        _all_tiers_m = [t for t in (self._price_tiers or []) if isinstance(t, dict)]
        _vis_ids_m = {str((t or {}).get("id") or "") for t in visible_tiers}
        tier_i = 18
        _merge_hdr_tier_refs: dict = {}
        for _tm in _all_tiers_m:
            _tmid = str((_tm or {}).get("id") or "").strip()
            _tm_vis = _tmid in _vis_ids_m
            _tm_w = (
                (widths[tier_i] if tier_i < n else 0)
                if _tm_vis
                else _all_tier_widths_m.get(_tmid, 0)
            )
            if _tm_vis:
                tier_i += 2
            _tm_c = _ct("-" if _tm_vis else "", _tm_w if _tm_vis else 0)
            if not _tm_vis:
                _tm_c.height = 0
                _tm_c.visible = False
            _merge_hdr_tier_refs[_tmid] = (_tm_c, None, None, _tm_w)
            mc.append(_tm_c)
            mc.append(ft.Container(width=0, height=0, visible=False))
        self._order_tier_all_cell_refs[f"grp_{gid}:0"] = _merge_hdr_tier_refs
        # new_price, new_margin (tail)
        mc.append(_ct("-", widths[-2] if n >= 2 else 0))
        mc.append(ft.Container(width=0, height=0, visible=False))

        # Reorder if the user has customized column order
        if callable(reorder_fn):
            mc = reorder_fn(mc)

        # Split frozen / scrollable and create row controls
        left_cells = mc[:frozen_count]
        right_cells = mc[frozen_count:]
        header_l = ft.Row(
            left_cells, spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER
        )
        header_r = ft.Row(
            right_cells, spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER
        )

        # Register as group row refs (used by unmerge)
        self._group_left_row_refs[gid] = [header_l, sub_left]
        self._group_right_row_refs[gid] = [header_r, sub_right]

        # Insert header + sub-column at the found position
        lc.controls.insert(min_pos, header_l)
        lc.controls.insert(min_pos + 1, sub_left)
        rc.controls.insert(min_pos, header_r)
        rc.controls.insert(min_pos + 1, sub_right)

        # Tiny diff → instant update
        _safe_flet_update(lc)
        _safe_flet_update(rc)
        return True

    def _apply_unmerge_surgical(self, gid: str, order_ids: list) -> bool:
        """Remove group header rows, re-show individual order rows. Returns True on success."""
        lc = self._data_left_col_ref
        rc = self._data_right_col_ref
        if lc is None or rc is None:
            return False

        order_id_strs = [str(oid) for oid in order_ids]
        target_lc = lc
        target_rc = rc

        group_header_rows = self._group_left_row_refs.get(gid) or []

        if group_header_rows:
            first_header = group_header_rows[0]
            if first_header not in lc.controls:
                # Group header is in overflow — only proceed if overflow is synced.
                if not getattr(self, "_overflow_synced_with_flutter", True):
                    return False  # overflow stale → caller does page reload
                ovf_lc = getattr(self, "_overflow_left_col_ref", None)
                ovf_rc = getattr(self, "_overflow_right_col_ref", None)
                if ovf_lc is not None and first_header in ovf_lc.controls:
                    target_lc = ovf_lc
                    target_rc = ovf_rc
                else:
                    return False  # can't locate group header in any column

        # Safety check: ensure order rows are DIRECT children of the target column,
        # not nested inside a sub-column (which happens after a page-reload build).
        # In the page-reload case, surgical unmerge would detach sub-rows → orders vanish.
        for oid in order_id_strs:
            order_rows = self._order_left_row_refs.get(oid) or []
            if order_rows and order_rows[0] not in target_lc.controls:
                # Order rows are nested inside a sub-column (page-reload state).
                return False  # caller will do page reload to show standalone orders

        # Remove group header rows from target column
        for row in list(self._group_left_row_refs.get(gid) or []):
            with contextlib.suppress(ValueError):
                target_lc.controls.remove(row)
        for row in list(self._group_right_row_refs.get(gid) or []):
            with contextlib.suppress(ValueError):
                target_rc.controls.remove(row)

        # Show individual order rows (they were hidden by _apply_merge_surgical)
        any_restored = False
        for oid in order_id_strs:
            for row in self._order_left_row_refs.get(oid) or []:
                row.visible = True
                any_restored = True
            for row in self._order_right_row_refs.get(oid) or []:
                row.visible = True

        # Clean up group refs
        self._group_left_row_refs.pop(gid, None)
        self._group_right_row_refs.pop(gid, None)

        if not any_restored:
            # Order rows not found (table was rebuilt since merge) — need full render
            return False

        _safe_flet_update(target_lc)
        _safe_flet_update(target_rc)
        return True

    def _schedule_table_section_render(self, *, rebuild_rows: bool = False) -> None:
        self._render_seq = getattr(self, "_render_seq", 0) + 1
        my_seq = self._render_seq
        pg = self._pg()
        if pg and hasattr(pg, "run_task") and self._body_column_ref is not None:

            async def _do(s=my_seq, rb=rebuild_rows):
                # Debounce: skip immediately cancelled requests
                await asyncio.sleep(0.02)
                if self._render_seq != s:
                    return
                # Serialize renders: only one at a time
                _lock = getattr(self, "_render_lock", None)
                if _lock is None:
                    self._render_lock = asyncio.Lock()
                    _lock = self._render_lock
                async with _lock:
                    # Check again after acquiring lock
                    if self._render_seq != s:
                        return
                    if rb:
                        await asyncio.to_thread(self._build_orders_rows)
                        self._sync_unit_costs_to_db()
                    await self._render_table_section_async(seq=s)

            pg.run_task(_do)
        else:
            if rebuild_rows:
                self._build_orders_rows()
                self._sync_unit_costs_to_db()
            self._render()

    async def _render_table_section_async(self, seq: int = -1) -> None:
        bc = self._body_column_ref
        if bc is None or not bc.controls:
            self._render()
            return

        self._order_status_cell_map.clear()
        self._order_delivery_cell_map.clear()
        self._order_cv_cell_map.clear()
        self._collapsed_cols_to_presync = []

        rows = self._orders_filtered()

        # Save old column refs BEFORE build — these objects are already known to Flutter.
        # After build, view._data_left_col_ref will point to NEW objects (unknown to Flutter).
        _old_left = self._data_left_col_ref
        _old_right = self._data_right_col_ref
        _old_ovf_left = getattr(self, "_overflow_left_col_ref", None)
        _old_ovf_right = getattr(self, "_overflow_right_col_ref", None)

        table_content = await asyncio.to_thread(build_orders_table, self, rows)

        # Cancellation point: if a newer render was scheduled while we were building, bail.
        if seq != -1 and self._render_seq != seq:
            return

        # New column refs set by build_orders_table
        _new_left = self._data_left_col_ref
        _new_right = self._data_right_col_ref
        _new_ovf_left = getattr(self, "_overflow_left_col_ref", None)
        _new_ovf_right = getattr(self, "_overflow_right_col_ref", None)

        _force_full = bool(getattr(self, "_force_full_bc_update", False))
        self._force_full_bc_update = False  # consume flag

        if _old_left is not None and _new_left is not None and not _force_full:
            # ── SURGICAL UPDATE: swap rows into Flutter-known column objects ──────
            # This sends only the diff for ~30 main rows, NOT the 70k+ overflow rows.
            _old_left.controls[:] = _new_left.controls
            _old_right.controls[:] = _new_right.controls
            # Restore refs so next render can find the Flutter-known objects again.
            self._data_left_col_ref = _old_left
            self._data_right_col_ref = _old_right

            await asyncio.to_thread(_safe_flet_update, _old_left)
            await asyncio.to_thread(_safe_flet_update, _old_right)

            # Overflow: update Python data only — NEVER send to Flutter.
            # Flutter overflow update with 70k+ controls takes 250+ seconds.
            # Surgical merge/unmerge sends its own tiny diff directly to Flutter
            # ONLY when overflow is synced (Python == Flutter).
            if _old_ovf_left is not None:
                if _new_ovf_left is not None:
                    _old_ovf_left.controls[:] = _new_ovf_left.controls
                    _old_ovf_right.controls[:] = _new_ovf_right.controls
                else:
                    _old_ovf_left.controls.clear()
                    _old_ovf_right.controls.clear()
                self._overflow_left_col_ref = _old_ovf_left
                self._overflow_right_col_ref = _old_ovf_right
                # After replacing Python overflow objects, Flutter's overflow is stale.
                # Surgical merge/unmerge must not call _safe_flet_update on overflow
                # until a fresh bc.update() re-syncs them.
                self._overflow_synced_with_flutter = False
        else:
            # First render or page-reload — full bc.update() syncs Python and Flutter.
            table_container = self._wrap_orders_table_with_scroll(table_content)
            bc.controls[0] = table_container
            await asyncio.to_thread(_safe_flet_update, bc)
            self._overflow_synced_with_flutter = True

        # ── Sticky header: currency bar + counters + filter bar + column header ──
        currency_bar_control = build_global_currency_bar(self)
        counters_control = build_orders_counters(self, rows)
        filter_bar_control = build_orders_filter_bar(self)
        table_header_widget = self._build_table_header_widget(table_content)
        self._sticky_currency_bar_ref = currency_bar_control
        self._sticky_counters_ref = counters_control
        self._sticky_filter_bar_ref = filter_bar_control
        self._sticky_table_header_ref = table_header_widget
        pg = self._pg()
        _sticky = getattr(pg, "_view_sticky_header", None) if pg else None
        if getattr(pg, "_active_view", "") != "show_cost_price_orders":
            _sticky = None
        if _sticky is not None and len(_sticky.controls) >= 5:
            _sticky.controls[1] = currency_bar_control
            _sticky.controls[2] = counters_control
            _sticky.controls[3] = filter_bar_control
            _sticky.controls[4] = table_header_widget
            await asyncio.to_thread(_safe_flet_update, _sticky)
        elif _sticky is not None and len(_sticky.controls) >= 3:
            _sticky.controls[-3] = counters_control
            _sticky.controls[-2] = filter_bar_control
            _sticky.controls[-1] = table_header_widget
            await asyncio.to_thread(_safe_flet_update, _sticky)

    def _save_merge_group_field(self, gid: str, field: str, value) -> None:
        gdata = self._orders_merge_groups.get(gid) or {}
        gdata[field] = value
        self._orders_merge_groups[gid] = gdata
        self._save_merge_groups_to_state()

    def _cascade_merge_status(self, gid: str, status: str) -> None:
        gdata = self._orders_merge_groups.get(gid) or {}
        for oid in gdata.get("order_ids") or []:
            for o in self._orders:
                if isinstance(o, dict) and str(o.get("id") or "") == str(oid):
                    self._save_order_fields(
                        o, 0, {}, {"status": status}, rerender=False, success_text=None
                    )
                    break
        self._snack("Статус оновлено — збережено локально", _SKY)
        # Data saved to outbox — no rebuild (rows are hidden inside merged group)

    def _cascade_merge_delivery(self, gid: str, method_row: dict) -> None:
        method_id = str((method_row.get("id") or "")).strip()
        method_name = str((method_row.get("name") or "")).strip()
        price, ccy = self._delivery_method_tariff(method_row)
        order_patch = {
            "delivery_type_id": method_id,
            "delivery_type_name": method_name,
            "delivery_price_snapshot": price,
            "delivery_price_currency_snapshot": ccy,
        }
        gdata = self._orders_merge_groups.get(gid) or {}
        for oid in gdata.get("order_ids") or []:
            for o in self._orders:
                if isinstance(o, dict) and str(o.get("id") or "") == str(oid):
                    self._save_order_fields(
                        o, 0, {}, order_patch, rerender=False, success_text=None
                    )
                    break
        self._snack("Тип доставки оновлено — збережено локально", _SKY)
        # Data saved to outbox — no rebuild

    def _cascade_merge_tariff(self, gid: str, tariff_value) -> None:
        gdata = self._orders_merge_groups.get(gid) or {}
        gdata["tariff"] = tariff_value
        self._orders_merge_groups[gid] = gdata
        for oid in gdata.get("order_ids") or []:
            for o in self._orders:
                if isinstance(o, dict) and str(o.get("id") or "") == str(oid):
                    self._save_order_fields(
                        o,
                        0,
                        {},
                        {"delivery_price_snapshot": tariff_value},
                        rerender=False,
                        success_text=None,
                    )
                    # Instant C/B recalculation via direct cell refs
                    with contextlib.suppress(Exception):
                        self._apply_cv_inplace(str(o.get("id") or ""))
                    break
        self._save_merge_groups_to_state()
        self._snack("Тариф оновлено — збережено локально", _SKY)

    def _distribute_merge_fact_weight(self, gid: str, total_weight) -> None:
        if total_weight is None:
            return
        gdata = self._orders_merge_groups.get(gid) or {}
        order_ids = set(str(x) for x in (gdata.get("order_ids") or []))
        item_rows = [
            r for r in self._orders_rows if str(r.get("order_id") or "") in order_ids
        ]
        total_calc = sum(
            _safe_float(r.get("calc_weight"), 0.0) or 0.0 for r in item_rows
        )
        if total_calc <= 0:
            self._snack("Немає розрахункової ваги для розподілу", _WARN)
            return

        sub_fw_refs = (getattr(self, "_sub_fw_ct_refs", None) or {}).get(gid) or {}
        sub_cv_refs = (getattr(self, "_sub_cv_ct_refs", None) or {}).get(gid) or {}
        all_updated_cells: list = []
        for row in item_rows:
            calc_w = _safe_float(row.get("calc_weight"), 0.0) or 0.0
            item_fact = total_weight * (calc_w / total_calc)
            self._save_order_fields(
                row.get("order") or {},
                int(row.get("line_idx") or 0),
                {"weight_actual_kg": item_fact},
                rerender=False,
                success_text=None,
            )
            row["fact_weight"] = item_fact
            oid = str(row.get("order_id") or "")

            # Instant fact_weight cell update: value + normal background (sub-order visible when expanded)
            fw_cell = sub_fw_refs.get(oid)
            if fw_cell is not None:
                tf = getattr(fw_cell, "content", None)
                if isinstance(tf, ft.TextField):
                    normal_bg = getattr(fw_cell, "_normal_bg", tf.fill_color)
                    tf.value = self._fmt_money(item_fact)
                    tf.fill_color = normal_bg
                    fw_cell.bgcolor = normal_bg
                    all_updated_cells.append(fw_cell)

            # Instant C/В recalculation via stored cell ref (weight affects per-kg logistics cost)
            cv_ref = sub_cv_refs.get(oid)
            if cv_ref is not None:
                cv_cell, ccy = cv_ref
                order_fresh = next(
                    (
                        o
                        for o in self._orders
                        if isinstance(o, dict) and str(o.get("id") or "") == oid
                    ),
                    None,
                )
                if (
                    order_fresh
                    and hasattr(cv_cell, "content")
                    and hasattr(cv_cell.content, "value")
                ):
                    with contextlib.suppress(Exception):
                        results = self._recalc_order_cv(order_fresh)
                        if results:
                            uc_usd = results[0].get("unit_cost_usd")
                            uc_ccy = results[0].get("unit_cost_currency", "USD")
                            uc_val = self._convert_currency(uc_usd, uc_ccy, ccy)
                            cv_cell.content.value = (
                                self._fmt_money(uc_val, ccy)
                                if uc_usd is not None
                                else "-"
                            )
                            all_updated_cells.append(cv_cell)
                            row["unit_cost_usd"] = uc_usd
                            row["unit_cost_currency"] = uc_ccy

        pg = self._pg()
        if pg and all_updated_cells:
            with contextlib.suppress(Exception):
                pg.update(*all_updated_cells)

        # Update the merged group header C/В cell to reflect new unit_cost aggregate.
        self._update_merge_group_cv_cell(gid, order_ids)
        self._save_merge_groups_to_state()
        self._snack("Вагу розподілено — збережено локально", _SKY)

    def _update_merge_group_cv_cell(self, gid: str, order_ids_set: set) -> None:
        """Recompute the merged group header C/В cell from current _orders_rows unit_cost_usd."""
        cv_ref = (getattr(self, "_merge_cv_cell_refs", None) or {}).get(gid)
        if cv_ref is None:
            return
        cv_cell, ccy = cv_ref
        if not (hasattr(cv_cell, "content") and hasattr(cv_cell.content, "value")):
            return
        parts = []
        for row in self._orders_rows:
            if str(row.get("order_id") or "") not in order_ids_set:
                continue
            uc = _safe_float(row.get("unit_cost_usd"), None)
            uc_ccy = str(row.get("unit_cost_currency") or "USD").upper().strip() or "USD"
            # Convert to the group's display currency before averaging —
            # sub-rows can each be in a different native currency now.
            uc_in_ccy = self._convert_currency(uc, uc_ccy, ccy) if uc is not None else None
            q = _safe_float(row.get("qty"), 0.0) or 0.0
            if uc_in_ccy is not None:
                parts.append((uc_in_ccy, q))
        total_q = sum(q for _, q in parts)
        grp_cv_usd = sum(uc * q for uc, q in parts) / total_q if total_q > 0 else None
        cv_cell.content.value = (
            self._fmt_money(grp_cv_usd, ccy)
            if grp_cv_usd is not None
            else "-"
        )
        with contextlib.suppress(Exception):
            cv_cell.update()

    def _distribute_merge_log_fact(self, gid: str, total_log_fact) -> None:
        gdata = self._orders_merge_groups.get(gid) or {}
        gdata["log_fact"] = total_log_fact
        self._orders_merge_groups[gid] = gdata
        order_ids = [str(x) for x in (gdata.get("order_ids") or [])]
        order_ids_set = set(order_ids)
        sub_refs = (getattr(self, "_sub_logfact_ct_refs", None) or {}).get(gid) or {}

        # ── Case 1: cleared (total_log_fact = None) — clear all sub-order log_fact ──
        sub_cv_refs = (getattr(self, "_sub_cv_ct_refs", None) or {}).get(gid) or {}
        if total_log_fact is None:
            lf_cells: list = []
            for oid in order_ids:
                order = next(
                    (
                        o
                        for o in self._orders
                        if isinstance(o, dict) and str(o.get("id") or "") == oid
                    ),
                    None,
                )
                if order:
                    self._save_order_fields(
                        order,
                        0,
                        {},
                        {"cp_logistics_fact_total": None},
                        rerender=False,
                        success_text=None,
                    )
                # Clear cell: empty value + red fill
                ct = sub_refs.get(oid)
                if ct is not None:
                    tf = getattr(ct, "content", None)
                    if isinstance(tf, ft.TextField):
                        tf.value = ""
                        tf.fill_color = _ERR_BG
                        ct.bgcolor = _ERR_BG
                        lf_cells.append(ct)
                # Recalculate C/В directly via stored cell ref
                cv_ref = sub_cv_refs.get(oid)
                if cv_ref is not None:
                    cv_cell, ccy = cv_ref
                    order_fresh = next(
                        (
                            o
                            for o in self._orders
                            if isinstance(o, dict) and str(o.get("id") or "") == oid
                        ),
                        None,
                    )
                    if (
                        order_fresh
                        and hasattr(cv_cell, "content")
                        and hasattr(cv_cell.content, "value")
                    ):
                        with contextlib.suppress(Exception):
                            results = self._recalc_order_cv(order_fresh)
                            if results:
                                uc_usd = results[0].get("unit_cost_usd")
                                uc_ccy = results[0].get("unit_cost_currency", "USD")
                                uc_val = self._convert_currency(uc_usd, uc_ccy, ccy)
                                cv_cell.content.value = (
                                    self._fmt_money(uc_val, ccy)
                                    if uc_usd is not None
                                    else "-"
                                )
                                lf_cells.append(cv_cell)
                                for row in self._orders_rows or []:
                                    if str(row.get("order_id") or "") == oid:
                                        row["unit_cost_usd"] = uc_usd
                                        row["unit_cost_currency"] = uc_ccy
            pg = self._pg()
            if pg and lf_cells:
                with contextlib.suppress(Exception):
                    pg.update(*lf_cells)
            # Update the merged group header C/В cell to reflect new unit_cost aggregate.
            self._update_merge_group_cv_cell(gid, order_ids_set)
            self._save_merge_groups_to_state()
            self._snack("Лог. факт очищено — збережено локально", _SKY)
            return

        # ── Case 2: distribute proportionally by weight ──────────────────────────
        order_weight_map: dict = {}
        order_obj_map: dict = {}
        for row in self._orders_rows:
            oid = str(row.get("order_id") or "")
            if oid not in order_ids_set:
                continue
            fw = _safe_float(row.get("fact_weight"), None)
            cw = _safe_float(row.get("calc_weight"), 0.0) or 0.0
            basis = fw if fw is not None else cw
            order_weight_map[oid] = order_weight_map.get(oid, 0.0) + basis
            if oid not in order_obj_map:
                order_obj_map[oid] = row.get("order")
        total_basis = sum(order_weight_map.values())
        if total_basis <= 0:
            self._snack("Немає ваги для розподілу логістики", _WARN)
            return

        all_updated_cells: list = []
        for oid in order_ids:
            sub_val = (
                total_log_fact * (order_weight_map[oid] / total_basis)
                if oid in order_weight_map
                else None
            )
            order = order_obj_map.get(oid)
            if order and sub_val is not None:
                self._save_order_fields(
                    order,
                    0,
                    {},
                    {"cp_logistics_fact_total": sub_val},
                    rerender=False,
                    success_text=None,
                )
            # Instant log_fact cell update: value + correct background
            ct = sub_refs.get(oid)
            if ct is not None:
                tf = getattr(ct, "content", None)
                if isinstance(tf, ft.TextField):
                    normal_bg = getattr(ct, "_normal_bg", "#EFF6FF")
                    new_bg = _ERR_BG if sub_val is None else normal_bg
                    tf.value = (
                        format_grouped(sub_val, decimals=2, trim=False)
                        if sub_val is not None
                        else ""
                    )
                    tf.fill_color = new_bg
                    ct.bgcolor = new_bg
                    all_updated_cells.append(ct)
            # Instant C/В recalculation via stored cell ref (no full rebuild needed)
            cv_ref = sub_cv_refs.get(oid)
            if cv_ref is not None:
                cv_cell, ccy = cv_ref
                order_fresh = next(
                    (
                        o
                        for o in self._orders
                        if isinstance(o, dict) and str(o.get("id") or "") == oid
                    ),
                    None,
                )
                if (
                    order_fresh
                    and hasattr(cv_cell, "content")
                    and hasattr(cv_cell.content, "value")
                ):
                    with contextlib.suppress(Exception):
                        results = self._recalc_order_cv(order_fresh)
                        if results:
                            uc_usd = results[0].get("unit_cost_usd")
                            uc_ccy = results[0].get("unit_cost_currency", "USD")
                            uc_val = self._convert_currency(uc_usd, uc_ccy, ccy)
                            cv_cell.content.value = (
                                self._fmt_money(uc_val, ccy)
                                if uc_usd is not None
                                else "-"
                            )
                            all_updated_cells.append(cv_cell)
                            for row in self._orders_rows or []:
                                if str(row.get("order_id") or "") == oid:
                                    row["unit_cost_usd"] = uc_usd
                                    row["unit_cost_currency"] = uc_ccy

        # Send one batch update to Flutter for all changed cells.
        pg = self._pg()
        if pg and all_updated_cells:
            with contextlib.suppress(Exception):
                pg.update(*all_updated_cells)

        # Update the merged group header C/В cell to reflect new unit_cost aggregate.
        self._update_merge_group_cv_cell(gid, order_ids_set)
        self._save_merge_groups_to_state()
        self._snack("Логістику розподілено — збережено локально", _SKY)

    def _schedule_render(
        self, delay_sec: float = 0.0, *, rebuild_rows: bool = False
    ) -> None:
        self._render_seq = getattr(self, "_render_seq", 0) + 1
        my_seq = self._render_seq
        self._render_pending = True
        pg = self._pg()
        if pg and hasattr(pg, "run_task"):

            async def _do_render(s=my_seq, d=delay_sec, rb=rebuild_rows):
                try:
                    if d and d > 0:
                        await asyncio.sleep(float(d))
                    if self._render_seq != s:
                        return
                    _lock = getattr(self, "_render_lock", None)
                    if _lock is None:
                        self._render_lock = asyncio.Lock()
                        _lock = self._render_lock
                    async with _lock:
                        if self._render_seq != s:
                            return
                        if rb:
                            await asyncio.to_thread(self._build_orders_rows)
                            self._sync_unit_costs_to_db()
                        if self._body_column_ref is not None:
                            await self._render_table_section_async(seq=s)
                        elif not getattr(self, "_async_build_in_progress", False):
                            self._render()
                finally:
                    self._render_pending = False

            try:
                pg.run_task(_do_render)
                return
            except Exception:
                self._render_pending = False
        if rebuild_rows:
            self._build_orders_rows()
            self._sync_unit_costs_to_db()
        try:
            self._render()
        finally:
            self._render_pending = False

    def _compute_order_row_new_margin(
        self, row: dict, new_price: Optional[float]
    ) -> Optional[float]:
        unit_cost_usd = _safe_float((row or {}).get("unit_cost_usd"), None)
        unit_cost_ccy = str((row or {}).get("unit_cost_currency") or "USD").upper().strip() or "USD"
        defaults = self._column_defaults()
        new_ccy = (
            str(
                (row or {}).get("new_price_ccy")
                or defaults.get("new_price_ccy")
                or "USD"
            )
            .upper()
            .strip()
            or "USD"
        )
        if new_price is None or new_price <= 0 or unit_cost_usd is None:
            return None
        cost_in_new_ccy = self._convert_currency(unit_cost_usd, unit_cost_ccy, new_ccy)
        if cost_in_new_ccy is None:
            return None
        return (float(new_price) - float(cost_in_new_ccy)) / float(new_price)

    def _update_margin_cell(
        self,
        container: ft.Container,
        label: ft.Text,
        margin: Optional[float],
        target_margin: Optional[float],
        outer_container: Optional[ft.Container] = None,
    ) -> None:
        bg, fg = self._margin_colors(margin, target_margin)
        container.bgcolor = bg
        if outer_container is not None:
            outer_container.bgcolor = bg
        label.value = "-" if margin is None else f"{float(margin) * 100:.1f}%"
        label.color = fg
        try:
            container.update()
        except Exception:
            pass
        if outer_container is not None:
            try:
                outer_container.update()
            except Exception:
                pass

    def _save_order_new_price_inline(
        self,
        order: dict,
        item_idx: int,
        row: dict,
        raw_value: str,
        margin_container: ft.Container,
        margin_label: ft.Text,
        target_margin: Optional[float],
        outer_container: Optional[ft.Container] = None,
    ) -> None:
        if self.is_read_only:
            return
        new_price = _safe_float(raw_value, None)
        order_id = str((order or {}).get("id") or "").strip()
        defaults = self._column_defaults()
        new_ccy = (
            str(
                (row or {}).get("new_price_ccy")
                or defaults.get("new_price_ccy")
                or "USD"
            )
            .upper()
            .strip()
            or "USD"
        )
        if not order_id:
            return

        cache_key = f"{order_id}:{int(item_idx or 0)}"
        # Update local display-row cache so margin cell refreshes immediately.
        merged_state = dict(self._order_price_inputs_by_key.get(cache_key) or {})
        merged_state["new_price"] = new_price
        merged_state["new_price_ccy"] = new_ccy
        self._order_price_inputs_by_key[cache_key] = merged_state
        row["new_price"] = new_price
        row["new_price_ccy"] = new_ccy
        row["new_margin"] = self._compute_order_row_new_margin(row, new_price)
        self._update_margin_cell(
            margin_container,
            margin_label,
            row.get("new_margin"),
            target_margin,
            outer_container,
        )
        # Save into the order item (orders JSONB) — goes to shared_map, orders
        # outbox, and eventually to the orders DB table.  No separate
        # cost_price_order_inputs write needed: the value lives in the order.
        self._save_order_fields(
            order,
            int(item_idx or 0),
            {"new_price": new_price, "new_price_ccy": new_ccy},
            rerender=False,
        )
        # Keep backward-compat outbox entry so old code paths still flush correctly.
        self._ops_outbox[f"input:{order_id}:{int(item_idx or 0)}"] = {
            "kind": "input",
            "company_id": self.company_id,
            "order_id": order_id,
            "item_idx": int(item_idx or 0),
            "data": merged_state,
            "region_id": self._region_id,
            "manager_id": self.manager_id,
        }
        self._save_ops_outbox()
        self._refresh_workspace_sync_indicator_state()

    def _is_invoice_duplicate(
        self,
        invoice: str,
        region_id: str | None = None,
        exclude_order_id: str | None = None,
    ) -> bool:
        inv = str(invoice or "").strip()
        if not inv:
            return False
        rid = str(region_id or "").strip() or None
        ex_id = str(exclude_order_id or "").strip()
        for o in self._orders or []:
            if not isinstance(o, dict):
                continue
            if (
                str(o.get("manager_id") or "").strip()
                != str(self.manager_id or "").strip()
            ):
                continue
            if rid and str(o.get("region_id") or "").strip() != rid:
                continue
            if ex_id and str(o.get("id") or "").strip() == ex_id:
                continue
            if str(o.get("invoice_no") or "").strip() == inv:
                return True
        return False

    def _save_invoice_with_dup_check(self, order: dict, value: str) -> None:
        new_inv = str(value or "").strip()
        old_inv = str(order.get("invoice_no") or "").strip()
        if new_inv and new_inv != old_inv:
            rid = str(order.get("region_id") or "").strip() or None
            oid = str(order.get("id") or "").strip()
            if self._is_invoice_duplicate(new_inv, rid, exclude_order_id=oid):
                self._snack(f"Інвойс {new_inv!r} вже існує в цьому регіоні", _ERR)
                return
        self._save_order_fields(order, 0, {}, {"invoice_no": value})

    def _save_order_fields(
        self,
        order: dict,
        item_idx: int,
        patch: dict,
        order_patch: Optional[dict] = None,
        *,
        rerender: bool = True,
        success_text: Optional[str] = None,
    ):
        if self.is_read_only:
            return None
        if not isinstance(order, dict):
            return None
        # Always work from the freshest cached version — closures capture order refs
        # that go stale as soon as _replace_order_local swaps in a new dict object.
        oid = str(order.get("id") or "").strip()
        if oid:
            live = next(
                (
                    o
                    for o in self._orders
                    if isinstance(o, dict) and str(o.get("id") or "").strip() == oid
                ),
                None,
            )
            base_order = live if live is not None else order
        else:
            base_order = order
        updated = dict(base_order)
        # Create a NEW list so mutations don't bleed back into base_order["items"].
        # dict(base_order) is a shallow copy — without list() the items list is shared,
        # causing updated == base_order to be True after the patch (no save triggered).
        items = list(updated.get("items") or [])
        while len(items) <= int(item_idx):
            items.append({})
        base = dict(items[item_idx] or {})
        base.update(patch or {})
        items[item_idx] = base
        updated["items"] = items
        if isinstance(order_patch, dict):
            updated.update(order_patch)
        if updated == base_order:
            return updated
        updated["updated_at"] = datetime.utcnow().isoformat(timespec="seconds")
        self._replace_order_local(updated)
        oid = str(updated.get("id") or "").strip()
        self._sync_order_to_shared_map(oid, updated)
        # Enqueue PATCH (only changed fields) for outbox — not the full order.
        if oid:
            # Build minimal patch: only what actually changed.
            minimal_patch: dict = {}
            # Order-level fields from order_patch
            if isinstance(order_patch, dict):
                for _k, _v in order_patch.items():
                    minimal_patch[_k] = _v
            # Item-level fields: send full items array (JSONB can't do nested patch).
            if patch:
                minimal_patch["items"] = list(updated.get("items") or [])
            if minimal_patch:
                # Merge into existing patch for this order (accumulate multiple edits).
                _pk = f"patch:{oid}"
                _existing_patch = self._ops_outbox.get(_pk) or {}
                _existing_fields = dict(_existing_patch.get("fields") or {})
                # Items: always replace with latest full array.
                if "items" in minimal_patch:
                    _existing_fields["items"] = minimal_patch["items"]
                # Other fields: merge.
                for _k, _v in minimal_patch.items():
                    if _k != "items":
                        _existing_fields[_k] = _v
                self._ops_outbox[_pk] = {
                    "kind": "patch",
                    "order_id": oid,
                    "company_id": str(updated.get("company_id") or "").strip(),
                    "fields": _existing_fields,
                    "manager_id": self.manager_id,
                }
            self._save_ops_outbox()
            self._refresh_workspace_sync_indicator_state()
        if rerender:
            if success_text:
                self._snack(success_text, _SKY)
            # CV fields: try instant in-place update first; fall back to full rebuild
            _cv_fields = {
                "cp_logistics_fact_total",
                "delivery_price_snapshot",
                "weight_actual_kg",
            }
            _changed_cv = bool(
                (isinstance(order_patch, dict) and _cv_fields & set(order_patch.keys()))
                or (isinstance(patch, dict) and _cv_fields & set(patch.keys()))
            )
            if _changed_cv and oid:
                _applied = self._apply_cv_inplace(oid)
                if _applied:
                    self._sync_unit_costs_to_db()
                    # If this order is part of a merged group, also refresh the group header C/В.
                    for _gid, _gdata in (self._orders_merge_groups or {}).items():
                        _goids = {str(x) for x in (_gdata.get("order_ids") or [])}
                        if oid in _goids:
                            self._update_merge_group_cv_cell(_gid, _goids)
                            break
                    return updated
            self._schedule_render(0.5, rebuild_rows=True)
        elif success_text:
            self._snack(success_text, _SKY)
        return updated

    def _set_global_logistics_currency(self, value: str):
        selected = str(value or "USD").upper().strip() or "USD"
        bucket = self._state_bucket()
        defaults = self._column_defaults()
        if (
            defaults.get("log_calc_ccy") == selected
            and defaults.get("log_fact_ccy") == selected
        ):
            return
        defaults["log_calc_ccy"] = selected
        defaults["log_fact_ccy"] = selected
        bucket["column_defaults"] = defaults
        for order in self._orders:
            if not isinstance(order, dict):
                continue
            updated = dict(order)
            if (
                str(updated.get("cp_logistics_calc_currency") or "").upper().strip()
                == selected
                and str(updated.get("cp_logistics_fact_currency") or "").upper().strip()
                == selected
            ):
                continue
            # Relabel only — the stored number is kept as-is, the currency tag
            # just changes which unit it's read as. Margin/downstream figures
            # recalculate against the (unchanged) number via rebuild_rows=True.
            updated["cp_logistics_calc_currency"] = selected
            updated["cp_logistics_fact_currency"] = selected
            updated["updated_at"] = datetime.utcnow().isoformat(timespec="seconds")
            self._replace_order_local(updated)
            oid = str(updated.get("id") or "").strip()
            if oid:
                self._ops_outbox[f"order:{oid}"] = {
                    "kind": "order",
                    "data": updated,
                    "manager_id": self.manager_id,
                }
        self._save_ops_outbox()
        self._refresh_workspace_sync_indicator_state()
        self._queue_workspace_state_save()
        self._trigger_immediate_sync()
        self._persist_and_render("Валюту оновлено", rebuild_rows=True)

    def _set_global_new_price_currency(self, value: str):
        selected = str(value or "USD").upper().strip() or "USD"
        bucket = self._state_bucket()
        defaults = self._column_defaults()
        prev_ccy = str(defaults.get("new_price_ccy") or "USD").upper().strip() or "USD"
        changed = prev_ccy != selected
        defaults["new_price_ccy"] = selected
        bucket["column_defaults"] = defaults

        # "Нова ціна" is relabeled only — the stored number stays exactly as
        # entered, only its currency tag switches. Margin/РРЦ recalculate
        # against that (unchanged) number via rebuild_rows=True below.
        for order in self._orders:
            if not isinstance(order, dict):
                continue
            order_id = str(order.get("id") or "").strip()
            items = order.get("items") if isinstance(order.get("items"), list) else []
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                cache_key = f"{order_id}:{idx}"
                cached = self._order_price_inputs_by_key.get(cache_key)
                item_price = _safe_float(item.get("new_price"), None)
                if item_price is not None:
                    price = item_price
                    ccy = str(item.get("new_price_ccy") or "").upper().strip() or prev_ccy
                elif isinstance(cached, dict) and cached.get("new_price") is not None:
                    price = _safe_float(cached.get("new_price"), None)
                    ccy = (
                        str(cached.get("new_price_ccy") or "").upper().strip()
                        or prev_ccy
                    )
                else:
                    price = _safe_float(item.get("cp_new_price"), None)
                    ccy = (
                        str(item.get("cp_new_price_currency") or "").upper().strip()
                        or prev_ccy
                    )
                if price is None or ccy == selected:
                    continue
                self._save_order_fields(
                    order,
                    idx,
                    {"new_price": price, "new_price_ccy": selected},
                    rerender=False,
                )
                merged_cache = dict(cached) if isinstance(cached, dict) else {}
                merged_cache["new_price"] = price
                merged_cache["new_price_ccy"] = selected
                self._order_price_inputs_by_key[cache_key] = merged_cache
                self._ops_outbox[f"input:{order_id}:{idx}"] = {
                    "kind": "input",
                    "company_id": self.company_id,
                    "order_id": order_id,
                    "item_idx": idx,
                    "data": merged_cache,
                    "region_id": self._region_id,
                    "manager_id": self.manager_id,
                }
                changed = True
        self._save_ops_outbox()
        self._refresh_workspace_sync_indicator_state()
        self._queue_workspace_state_save()
        if changed:
            self._trigger_immediate_sync()
            self._persist_and_render("Валюту оновлено", rebuild_rows=True)

    def _set_global_order_unit_cost_currency(self, value: str):
        selected = str(value or "USD").upper().strip() or "USD"
        bucket = self._state_bucket()
        defaults = self._column_defaults()
        if defaults.get("orders_unit_cost_ccy") == selected:
            return
        defaults["orders_unit_cost_ccy"] = selected
        bucket["column_defaults"] = defaults
        self._queue_workspace_state_save()
        self._trigger_immediate_sync()
        self._persist_and_render("Валюту оновлено", rebuild_rows=True)

    def apply_text_search(self, query: str) -> None:
        """Ctrl+F row filter: hide rows that don't match `query`."""
        self._orders_text_query = str(query or "").strip()
        self._render()

    def clear_text_search(self) -> None:
        self.apply_text_search("")

    def _orders_filtered(self) -> list:
        out = []
        for row in self._orders_rows:
            if (
                self._orders_filter_status != "all"
                and row.get("status") != self._orders_filter_status
            ):
                continue
            if (
                self._orders_filter_supplier != "all"
                and row.get("supplier_id") != self._orders_filter_supplier
            ):
                continue
            if (
                self._orders_filter_category != "all"
                and row.get("category_id") != self._orders_filter_category
            ):
                continue
            if (
                self._orders_filter_product != "all"
                and str((row.get("item") or {}).get("product_id") or "").strip()
                != self._orders_filter_product
            ):
                continue
            if (
                self._orders_filter_payment != "all"
                and row.get("payment") != self._orders_filter_payment
            ):
                continue
            if self._orders_text_query:
                visible_tiers = self._visible_orders_tier_ids()
                tier_prices = [
                    tp.get("sale_price")
                    for tp in (row.get("tier_pairs") or [])
                    if str((tp.get("tier") or {}).get("id") or "") in visible_tiers
                ]
                if not row_text_matches(
                    self._orders_text_query,
                    row.get("invoice"),
                    row.get("order_1c"),
                    row.get("qty"),
                    row.get("amount"),
                    row.get("full_amount"),
                    row.get("product_name"),
                    row.get("calc_weight"),
                    row.get("fact_weight"),
                    row.get("log_calc"),
                    row.get("log_fact"),
                    row.get("new_price"),
                    *tier_prices,
                ):
                    continue
            # Apply quick_filter_mode from home KPI cards.
            qfm = self._quick_filter_mode
            if qfm:
                status = str(row.get("status") or "production").strip().lower()
                # Only non-production statuses are relevant for logistics/weight filters.
                if status not in {
                    "ready_for_shipment",
                    "in_transit",
                    "at_warehouse",
                    "accepted",
                }:
                    continue
                if qfm == "missing_logistics_fact":
                    fact = row.get("log_fact_order_total")
                    if fact is None:
                        order = row.get("order") or {}
                        fact = _safe_float(order.get("cp_logistics_fact_total"), None)
                    if fact is not None and abs(float(fact or 0)) > 1e-9:
                        continue  # has fact → skip
                elif qfm == "missing_fact_weight":
                    w = row.get("fact_weight")
                    if w is None:
                        item = row.get("item") or {}
                        w = _safe_float(item.get("weight_actual_kg"), None)
                    if w is not None and float(w or 0) > 1e-9:
                        continue  # has weight → skip
            out.append(row)

        # Newest orders first; invalid/empty dates naturally go to the end.
        def _sort_date_key(row: dict) -> datetime:
            raw = str((row or {}).get("date") or "").strip()
            if len(raw) >= 10:
                raw = raw[:10]
            try:
                return datetime.strptime(raw, "%Y-%m-%d")
            except Exception:
                return datetime.min

        out.sort(key=_sort_date_key, reverse=True)
        return out

    def _target_margin(self, category_id: str) -> Optional[float]:
        cat = self._category_by_id.get(str(category_id or "").strip())
        return self.db.get_category_target_margin(cat)

    def _margin_colors(
        self, margin: Optional[float], target_margin_pct: Optional[float]
    ) -> tuple[str, str]:
        if margin is None:
            return (_ALT, "#64748B")
        margin_pct = float(margin) * 100.0
        pivot = float(target_margin_pct or 0.0)
        if margin_pct >= pivot:
            return (_OK_BG, _OK_T)
        if margin_pct >= pivot - 5.0:
            return (_WARN_BG, _WARN_T)
        return (_ERR_BG, _ERR)

    def _kpi_chip(self, label: str, value: Any, bg: str, fg: str) -> ft.Container:
        return ft.Container(
            bgcolor=bg,
            border_radius=6,
            padding=ft.padding.symmetric(horizontal=6, vertical=2),
            content=ft.Row(
                [
                    ft.Text(label, size=9, color=fg),
                    ft.Container(
                        bgcolor=fg,
                        border_radius=10,
                        padding=ft.padding.symmetric(horizontal=5, vertical=1),
                        content=ft.Text(
                            str(value), size=9, color=_PANEL, weight="bold"
                        ),
                    ),
                ],
                spacing=4,
                tight=True,
            ),
        )

    def _get_viewport_width(self, page=None) -> int:
        pg = page or (self.content_area.page if self.content_area else None)
        if not pg:
            return 1200
        # content_area.width is the most accurate (set by Flet layout engine).
        host_w = (
            int(getattr(self.content_area, "width", 0) or 0) if self.content_area else 0
        )
        if host_w > 0:
            return max(320, host_w)
        # Fallback: page/window width divided by zoom factor so the table fills
        # the scaled canvas (at 90% zoom the canvas is pw/0.9 logical pixels wide).
        zoom = float(getattr(pg, "_zoom_factor", 1.0) or 1.0)
        if zoom < 0.01:
            zoom = 1.0
        page_w = int(getattr(pg, "width", 0) or 0)
        if page_w <= 0:
            win = getattr(pg, "window", None)
            page_w = int(getattr(win, "width", 0) or 0) if win else 0
        if page_w > 0:
            return max(320, int(page_w / zoom))
        return 1200

    def _attach_resize_handler(self) -> None:
        page = self._pg()
        if not page:
            return

        def _on_resized(_e=None):
            try:
                active_view = str(getattr(page, "_active_view", "") or "").strip()
            except Exception:
                active_view = ""
            if active_view != "show_cost_price_orders":
                return

            try:
                new_w = int(self._get_viewport_width(page))
            except Exception:
                new_w = 0

            holder = self._resize_refresh_state
            if abs(int(holder.get("last_w") or 0) - int(new_w or 0)) < 4:
                return
            holder["last_w"] = int(new_w or 0)
            if holder.get("pending"):
                return
            holder["pending"] = True

            async def _rerender_later():
                try:
                    await asyncio.sleep(0.05)
                    self._render()
                finally:
                    holder["pending"] = False

            try:
                page.run_task(_rerender_later)
            except Exception:
                holder["pending"] = False

        try:
            if hasattr(page, "on_resized"):
                page.on_resized = _on_resized
            elif hasattr(page, "on_resize"):
                page.on_resize = _on_resized
        except Exception:
            pass

    def _wrap_orders_table_with_scroll(self, table: dict) -> ft.Control:
        viewport_width = max(320, int(self._get_viewport_width()))
        frozen_part = table.get("frozen") if isinstance(table, dict) else None
        scroll_part = table.get("scrollable") if isinstance(table, dict) else None
        frozen_width = int((table or {}).get("frozen_width") or 0)
        scroll_width = int((table or {}).get("scroll_width") or 0)
        shell_width = max(0, scroll_width)
        fixed_width = (
            min(max(0, frozen_width), max(0, viewport_width - 120))
            if viewport_width > 240
            else frozen_width
        )
        scroll_view_width = max(120, viewport_width - fixed_width)
        scroll_host_width = (
            min(scroll_view_width, shell_width)
            if shell_width > 0
            else scroll_view_width
        )

        frozen_shell = ft.Container(
            width=fixed_width,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            bgcolor=_PANEL,
            border_radius=8,
            border=ft.border.all(1, _BORDER2),
            content=ft.Container(
                width=frozen_width, alignment=ft.Alignment(-1, 0), content=frozen_part
            ),
        )
        table_shell = ft.Container(
            width=shell_width,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            bgcolor=_PANEL,
            border_radius=8,
            border=ft.border.all(1, _BORDER2),
            content=ft.Container(
                width=shell_width, alignment=ft.Alignment(-1, 0), content=scroll_part
            ),
        )

        def _on_body_scroll(e):
            """Track horizontal position and sync sticky header."""
            self._table_h_scroll_offset = max(
                0.0, float(getattr(e, "pixels", 0.0) or 0.0)
            )
            item = getattr(self, "_cv_header_stack_child", None)
            if item is not None:
                item.left = -self._table_h_scroll_offset
                with contextlib.suppress(Exception):
                    item.update()

        # Hidden scrollbar — h-scroll driven by overlay buttons instead.
        body_scroll = ft.Row(
            [table_shell],
            spacing=0,
            expand=True,
            scroll=ft.ScrollMode.HIDDEN,
            on_scroll=_on_body_scroll,
        )
        self._cv_body_scroll = body_scroll
        self._h_scroll_slot_content = None

        pg = self._pg()

        # Restore scroll position after rebuild.
        if pg and hasattr(pg, "run_task") and self._table_h_scroll_offset > 0:
            _restore_off = self._table_h_scroll_offset

            async def _restore_scroll():
                import asyncio as _aio

                await _aio.sleep(0.05)
                with contextlib.suppress(Exception):
                    await body_scroll.scroll_to(offset=_restore_off, duration=0)
                _sc = getattr(self, "_cv_header_stack_child", None)
                if _sc is not None:
                    _sc.left = -_restore_off
                    with contextlib.suppress(Exception):
                        _sc.update()

            try:
                pg.run_task(_restore_scroll)
            except Exception:
                pass

        # Clear page-level slot if another view left one behind.
        _scroll_slot = getattr(pg, "_view_h_scrollbar_slot", None) if pg else None
        if _scroll_slot is not None and _scroll_slot.controls:
            try:
                _scroll_slot.controls.clear()
                _scroll_slot.update()
            except Exception:
                pass

        # Store h-scroll params for _render() to create overlay buttons outside scroll.
        self._h_scroll_needs = shell_width > scroll_host_width
        self._h_scroll_step = max(300, shell_width // 3)
        self._h_scroll_width = shell_width

        return ft.Container(
            width=viewport_width,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            frozen_shell,
                            ft.Container(
                                width=scroll_host_width, content=body_scroll, padding=0
                            ),
                        ],
                        spacing=0,
                    ),
                ],
                spacing=0,
                tight=True,
            ),
            padding=0,
        )

    def _build_table_header_widget(self, table: dict) -> ft.Control:
        """Build the sticky column-header row that sits above the scrollable data area."""
        viewport_width = max(320, int(self._get_viewport_width()))
        frozen_width = int((table or {}).get("frozen_width") or 0)
        scroll_width = int((table or {}).get("scroll_width") or 0)
        shell_width = max(0, scroll_width)
        fixed_width = (
            min(max(0, frozen_width), max(0, viewport_width - 120))
            if viewport_width > 240
            else frozen_width
        )
        scroll_view_width = max(120, viewport_width - fixed_width)
        scroll_host_width = (
            min(scroll_view_width, shell_width)
            if shell_width > 0
            else scroll_view_width
        )

        left_hdr = table.get("left_header_row")
        right_hdr = table.get("right_header_row")

        frozen_header_shell = ft.Container(
            width=fixed_width,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            bgcolor=_HDR,
            border=ft.border.all(1, _BORDER),
            content=ft.Container(
                width=frozen_width, alignment=ft.Alignment(-1, 0), content=left_hdr
            ),
        )
        header_table_shell = ft.Container(
            width=shell_width,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            bgcolor=_HDR,
            border=ft.border.all(1, _BORDER),
            content=ft.Container(
                width=shell_width, alignment=ft.Alignment(-1, 0), content=right_hdr
            ),
        )
        header_table_shell.left = -int(self._table_h_scroll_offset or 0)
        header_table_shell.top = 0
        self._cv_header_stack_child = header_table_shell
        self._cv_header_scroll = None

        right_header_host = ft.Container(
            width=(
                min(scroll_host_width, shell_width)
                if shell_width > 0
                else scroll_host_width
            ),
            height=34,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.Stack([header_table_shell]),
            padding=0,
        )
        return ft.Container(
            width=viewport_width,
            padding=ft.padding.only(top=2),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            frozen_header_shell,
                            right_header_host,
                        ],
                        spacing=0,
                    )
                ],
                spacing=0,
                tight=True,
            ),
        )

    def _render(self):
        self._last_rendered_width = int(self._get_viewport_width())
        self._order_status_cell_map.clear()
        self._order_delivery_cell_map.clear()
        self._order_cv_cell_map.clear()
        self._collapsed_cols_to_presync = []
        region_label = str(
            self.user_data.get("active_region_label")
            or self.active_region
            or self.t("nav.all_regions", "Всі регіони")
        ).strip()
        manager_label = str(self.manager_name or "").strip()
        subtitle = (
            f"{region_label} · {manager_label}" if manager_label else region_label
        )
        # Build fresh sync indicator (new controls each render).
        _sync_indicator = self._build_workspace_sync_indicator()
        # Set correct initial text ("Збережено" / "Локально" / "Не збережено")
        # after building the indicator — controls start with empty "" text.
        with contextlib.suppress(Exception):
            self._refresh_workspace_sync_indicator_state()
        # Create a fresh inline loading indicator — keep it VISIBLE while Flutter
        # lays out the new table. We hide it after a short delay so the spinner
        # stays on screen until the content is actually rendered.
        _loading_indicator = create_orders_header_loading_indicator(visible=True)
        self._header_loading_indicator = _loading_indicator
        header_row = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(
                                ft.Icons.LOCAL_SHIPPING_OUTLINED,
                                size=26,
                                color=_TEXT,
                            ),
                            ft.Column(
                                [
                                    ft.Row(
                                        [
                                            ft.Text(
                                                self.t(
                                                    "cost_orders.title",
                                                    "Собівартість / Рентабельність",
                                                ),
                                                size=21,
                                                weight="bold",
                                                color=_TEXT,
                                            ),
                                            _loading_indicator,
                                            ft.IconButton(
                                                icon=ft.Icons.HELP_OUTLINE,
                                                icon_size=18,
                                                icon_color=_TEXT,
                                                tooltip=self.t(
                                                    "common.help.tooltip", "Довідка"
                                                ),
                                                on_click=lambda _e: self._show_help(),
                                            ),
                                        ],
                                        spacing=8,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        tight=True,
                                    ),
                                    ft.Text(
                                        subtitle,
                                        size=13,
                                        weight=ft.FontWeight.W_500,
                                        color=_TEXT,
                                    ),
                                ],
                                spacing=2,
                                tight=True,
                                expand=True,
                            ),
                            ft.Row(
                                [
                                    ft.OutlinedButton(
                                        self.t("payments.dlg.table_title", "Таблиця"),
                                        icon=ft.Icons.TABLE_CHART_OUTLINED,
                                        height=35,
                                        style=ft.ButtonStyle(
                                            text_style=ft.TextStyle(size=11),
                                            padding=ft.padding.symmetric(
                                                horizontal=10, vertical=2
                                            ),
                                        ),
                                        on_click=self._guard_ui_action(
                                            "orders_table_settings",
                                            self._open_orders_table_dialog,
                                            self.t(
                                                "cost_orders.err.table_settings",
                                                "Помилка налаштувань таблиці",
                                            ),
                                        ),
                                    ),
                                    ft.OutlinedButton(
                                        self.t("cost_orders.calculator", "Калькулятор"),
                                        icon=ft.Icons.CALCULATE_OUTLINED,
                                        height=35,
                                        style=ft.ButtonStyle(
                                            text_style=ft.TextStyle(size=11),
                                            padding=ft.padding.symmetric(
                                                horizontal=10, vertical=2
                                            ),
                                        ),
                                        on_click=self._guard_ui_action(
                                            "orders_calculator",
                                            self._open_calculator_dialog,
                                            "Помилка калькулятора",
                                        ),
                                    ),
                                    _sync_indicator,
                                ],
                                spacing=8,
                                wrap=True,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=6,
                tight=True,
            ),
            border=ft.border.only(bottom=ft.BorderSide(1, _HDR)),
            padding=ft.padding.only(bottom=12),
        )

        currency_bar = build_global_currency_bar(self)
        rows = self._orders_filtered()
        # Secondary safety-net: re-apply quick_filter_mode here in case
        # _orders_filtered() missed it due to async timing or state reset.
        _qfm = getattr(self, "_quick_filter_mode", "")
        if _qfm:
            _non_prod = {"ready_for_shipment", "in_transit", "at_warehouse", "accepted"}

            def _passes_quick(r: dict) -> bool:
                if str(r.get("status") or "production").lower() not in _non_prod:
                    return False
                if _qfm == "missing_logistics_fact":
                    # Check row-level field first, then raw order as fallback.
                    v = r.get("log_fact_order_total")
                    if v is None:
                        order = r.get("order") or {}
                        v = _safe_float(order.get("cp_logistics_fact_total"), None)
                    return v is None or abs(float(v or 0)) <= 1e-9
                if _qfm == "missing_fact_weight":
                    w = r.get("fact_weight")
                    if w is None:
                        item = r.get("item") or {}
                        w = _safe_float(item.get("weight_actual_kg"), None)
                    return w is None or float(w or 0) <= 1e-9
                return True

            rows = [r for r in rows if _passes_quick(r)]
        # Use pre-built table content from _build_async (avoids blocking event loop).
        _pre = getattr(self, "_prebuilt_table_content", None)
        if _pre is not None:
            table_content = _pre
            rows = getattr(self, "_prebuilt_table_rows", None) or rows
            self._prebuilt_table_content = None
            self._prebuilt_table_rows = None
        else:
            table_content = build_orders_table(self, rows)
        table_container = self._wrap_orders_table_with_scroll(table_content)
        counters_control = build_orders_counters(self, rows)
        filter_bar_control = build_orders_filter_bar(self)
        table_header_widget = self._build_table_header_widget(table_content)

        body_column = ft.Column([table_container], spacing=0, tight=True)

        self.content_area.controls.clear()
        pg = self._pg()
        _sticky = getattr(pg, "_view_sticky_header", None) if pg else None
        if getattr(pg, "_active_view", "") != "show_cost_price_orders":
            _sticky = None
        if _sticky is not None:
            _sticky.controls.clear()
            _sticky.controls.extend(
                [
                    header_row,
                    currency_bar,
                    counters_control,
                    filter_bar_control,
                    table_header_widget,
                ]
            )
            with contextlib.suppress(Exception):
                _sticky.update()
            _v_body_items = [body_column]
        else:
            _v_body_items = [
                header_row,
                currency_bar,
                counters_control,
                filter_bar_control,
                table_header_widget,
                body_column,
            ]

        def _on_v_scroll(e):
            self._orders_v_scroll_offset = max(
                0.0, float(getattr(e, "pixels", 0.0) or 0.0)
            )

        _v_body = ft.Column(_v_body_items, spacing=0, tight=True)
        self._v_body_col_ref = _v_body

        # H-scroll overlay buttons sit OUTSIDE the scroll column → always stay on screen.
        _h_needs = getattr(self, "_h_scroll_needs", False)
        _h_step = getattr(self, "_h_scroll_step", 300)
        _cpo_th = _th_from_view(self)
        _cpo_accent = _cpo_th["accent"]
        _cpo_panel = _cpo_th["panel"]

        def _do_cpo_hscroll(delta):
            async def _run():
                with contextlib.suppress(Exception):
                    if self._cv_body_scroll is not None:
                        await self._cv_body_scroll.scroll_to(delta=delta, duration=200)

            with contextlib.suppress(Exception):
                if pg:
                    pg.run_task(_run)

        _cpo_sh = ft.BoxShadow(
            spread_radius=1,
            blur_radius=8,
            color=ft.Colors.with_opacity(0.28, ft.Colors.BLACK),
            offset=ft.Offset(0, 2),
        )
        _cpo_bs = ft.ButtonStyle(padding=ft.padding.all(4))
        _cpo_left = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_LEFT,
                icon_size=20,
                icon_color=_cpo_accent,
                on_click=lambda _: _do_cpo_hscroll(-_h_step),
                style=_cpo_bs,
            ),
            left=4,
            top=300,
            bgcolor=_cpo_panel,
            border_radius=20,
            width=36,
            height=36,
            shadow=_cpo_sh,
            opacity=0.9,
            visible=_h_needs,
        )
        _cpo_right = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_RIGHT,
                icon_size=20,
                icon_color=_cpo_accent,
                on_click=lambda _: _do_cpo_hscroll(_h_step),
                style=_cpo_bs,
            ),
            right=4,
            top=300,
            bgcolor=_cpo_panel,
            border_radius=20,
            width=36,
            height=36,
            shadow=_cpo_sh,
            opacity=0.9,
            visible=_h_needs,
        )

        try:
            self.content_area.scroll = ft.ScrollMode.ADAPTIVE
        except Exception:
            pass
        self.content_area.controls.append(_v_body)
        self._sticky_header_ref = header_row
        self._sticky_currency_bar_ref = currency_bar
        self._sticky_counters_ref = counters_control
        self._sticky_filter_bar_ref = filter_bar_control
        self._sticky_table_header_ref = table_header_widget
        self._body_column_ref = body_column
        if pg:
            try:
                pg.update()
            except RuntimeError:
                return  # Session destroyed — tab was closed before render completed

            # Move h-scroll buttons to page overlay so they stay visible at all
            # vertical scroll positions.
            for _b in getattr(pg, "_catman_hscroll_btns", []):
                with contextlib.suppress(Exception):
                    pg.overlay.remove(_b)
            pg._catman_hscroll_btns = []
            if _h_needs:
                _btn_top = int((getattr(pg, "height", None) or 900) * 0.5)
                _cpo_left.top = _btn_top
                _cpo_right.top = _btn_top
                pg.overlay.append(_cpo_left)
                pg.overlay.append(_cpo_right)
                pg._catman_hscroll_btns = [_cpo_left, _cpo_right]
                with contextlib.suppress(Exception):
                    pg.update()
            self._cpo_left_btn = _cpo_left
            self._cpo_right_btn = _cpo_right

            # Lightweight resize handler — updates h-scroll button visibility only.
            _w_final_cpo = getattr(self, "_h_scroll_width", 0)

            def _on_cpo_resized(_e=None):
                if (
                    str(getattr(pg, "_active_view", "") or "")
                    != "show_cost_price_orders"
                ):
                    return
                try:
                    vp = int(getattr(self.content_area, "width", 0) or 0)
                    if vp <= 0:
                        vp = int(getattr(pg, "width", 0) or 0)
                    if vp <= 0:
                        return
                    _wf = getattr(self, "_h_scroll_width", _w_final_cpo)
                    new_needs = _wf > max(200, vp - 28)
                    bl = getattr(self, "_cpo_left_btn", None)
                    br = getattr(self, "_cpo_right_btn", None)
                    if bl is not None and bl.visible != new_needs:
                        bl.visible = new_needs
                        br.visible = new_needs
                        with contextlib.suppress(Exception):
                            bl.update()
                        with contextlib.suppress(Exception):
                            br.update()
                except Exception:
                    pass

            try:
                if hasattr(pg, "on_resized"):
                    pg.on_resized = _on_cpo_resized
                elif hasattr(pg, "on_resize"):
                    pg.on_resize = _on_cpo_resized
            except Exception:
                pass

            # Hide spinner after a short delay — gives Flutter time to finish
            # laying out the table before the indicator disappears.
            _render_token = int(self._async_build_token or 0)
            _ind_ref = self._header_loading_indicator

            async def _hide_spinner_after_render():
                import asyncio as _aio

                await _aio.sleep(0.8)
                if _ind_ref is not None:
                    _ind_ref.visible = False
                    with contextlib.suppress(Exception):
                        _ind_ref.update()

            try:
                if hasattr(pg, "run_task"):
                    pg.run_task(_hide_spinner_after_render)
            except Exception:
                if _ind_ref is not None:
                    _ind_ref.visible = False

            # First render: restore saved scroll position (or scroll to top if none).
            if not getattr(self, "_initial_render_done", False):
                self._initial_render_done = True
                _restore_v = float(self._orders_v_scroll_offset or 0.0)
                _v_col = getattr(self, "_v_body_col_ref", None)
                _scroll_target = _v_col if _v_col is not None else self.content_area
                try:
                    if hasattr(pg, "run_task"):

                        async def _restore_v_scroll(_v=_restore_v, _t=_scroll_target):
                            import asyncio

                            await asyncio.sleep(0.5)
                            with contextlib.suppress(Exception):
                                await _t.scroll_to(offset=_v, duration=0)

                        pg.run_task(_restore_v_scroll)
                except Exception:
                    pass

    def _render_loading_stub(self):
        self._header_loading_indicator = create_orders_header_loading_indicator(
            visible=True
        )
        self._sync_indicator_text = None
        self._sync_indicator_dot = None
        self._sync_indicator_container = None
        region_label = str(
            self.user_data.get("active_region_label")
            or self.active_region
            or self.t("nav.all_regions", "Всі регіони")
        ).strip()
        manager_label = str(self.manager_name or "").strip()
        subtitle = (
            f"{region_label} · {manager_label}" if manager_label else region_label
        )
        _stub_header = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(
                                ft.Icons.LOCAL_SHIPPING_OUTLINED,
                                size=26,
                                color=_TEXT,
                            ),
                            ft.Column(
                                [
                                    ft.Row(
                                        [
                                            ft.Text(
                                                "Собівартість / Рентабельність",
                                                size=21,
                                                weight="bold",
                                                color=_TEXT,
                                            ),
                                            self._header_loading_indicator,
                                        ],
                                        spacing=8,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        tight=True,
                                    ),
                                    ft.Text(
                                        subtitle,
                                        size=13,
                                        weight=ft.FontWeight.W_500,
                                        color=_TEXT,
                                    ),
                                ],
                                spacing=2,
                                tight=True,
                                expand=True,
                            ),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=6,
                tight=True,
            ),
            border=ft.border.only(bottom=ft.BorderSide(1, _HDR)),
            padding=ft.padding.only(bottom=12),
        )
        pg = self._pg()
        if pg:
            for _b in getattr(pg, "_catman_hscroll_btns", []):
                with contextlib.suppress(Exception):
                    pg.overlay.remove(_b)
            pg._catman_hscroll_btns = []
        _sticky = getattr(pg, "_view_sticky_header", None) if pg else None
        if getattr(pg, "_active_view", "") != "show_cost_price_orders":
            _sticky = None
        self.content_area.controls.clear()
        if _sticky is not None:
            _sticky.controls.clear()
            _sticky.controls.append(_stub_header)
            with contextlib.suppress(Exception):
                _sticky.update()
        else:
            self.content_area.controls.append(_stub_header)
        try:
            self.content_area.update()
        except Exception:
            pass

    async def _presync_collapsed_to_flutter(self, token: int) -> None:
        """Send already-built collapsed columns to Flutter while spinner is visible.

        Uses the same clear→batch-restore trick as Orders' _sync_body_to_flutter:
        each update() call forces Flutter to rebuild the widget subtree so that
        a later visibility toggle is instant.
        """
        pending = list(self._collapsed_cols_to_presync)
        self._collapsed_cols_to_presync = []
        if not pending:
            return

        indicator = getattr(self, "_header_loading_indicator", None)
        _ltext = None
        if indicator is not None:
            ctrls = getattr(indicator, "controls", [])
            _ltext = ctrls[1] if len(ctrls) > 1 else None
            if _ltext is not None:
                _ltext.value = "Підготовка..."
            indicator.visible = True
            with contextlib.suppress(Exception):
                indicator.update()

        try:
            B = 8  # rows per batch per column-pair
            for i, (lc, rc) in enumerate(pending):
                if token != int(self._async_build_token or 0):
                    return
                lc_rows = list(lc.controls)
                rc_rows = list(rc.controls)
                if not lc_rows:
                    continue
                lc.controls.clear()
                rc.controls.clear()
                with contextlib.suppress(Exception):
                    lc.update()
                with contextlib.suppress(Exception):
                    rc.update()
                await asyncio.sleep(0)
                for bi in range(0, max(1, len(lc_rows)), B):
                    lc.controls.extend(lc_rows[bi : bi + B])
                    rc.controls.extend(rc_rows[bi : bi + B])
                    with contextlib.suppress(Exception):
                        lc.update()
                    with contextlib.suppress(Exception):
                        rc.update()
                    await asyncio.sleep(0)
        finally:
            if indicator is not None and token == int(self._async_build_token or 0):
                indicator.visible = False
                with contextlib.suppress(Exception):
                    indicator.update()

    async def _build_async(self) -> None:
        token = int(self._async_build_token or 0)
        self._async_build_in_progress = True
        try:
            # Verify the shared orders snapshot is up-to-date with the DB BEFORE
            # calling _load().  This is the same check that orders_view does on its
            # initial load — whichever of the three tabs opens first will refresh
            # the snapshot if the DB has newer data (e.g. from another device).
            # Subsequent tabs see the verified flag and skip the round-trip.
            await ensure_orders_cache_fresh(
                user_data=self.user_data,
                db=self.db,
                app_data_dir=self._app_data_dir(),
                company_id=self.company_id,
                manager_id=self.manager_id,
                region_id=self._resolve_region_id(),
                shared_orders_map=self._shared_orders_map,
            )
            if token != int(self._async_build_token or 0):
                return
            await asyncio.to_thread(self._load)
            if token != int(self._async_build_token or 0):
                return
            # Pre-build the table controls in a background thread so _render() doesn't
            # block the event loop (build_orders_table creates hundreds of Flet controls
            # and can take several seconds on large order lists).
            _pre_rows = self._orders_filtered()
            _pre_content = await asyncio.to_thread(build_orders_table, self, _pre_rows)
            if token != int(self._async_build_token or 0):
                return
            self._prebuilt_table_content = _pre_content
            self._prebuilt_table_rows = _pre_rows
            self._render()
        finally:
            self._async_build_in_progress = False

    def build(self):
        global _TEXT, _TEXT_2, _MUTED, _BORDER, _BORDER2, _PANEL, _ALT, _ALT_2
        global _HDR, _CHIP_T, _ERR, _ERR_BG, _ERR_2, _SOFT_P, _WARN, _WARN_BG
        global _WARN_2, _WARN_T, _OK_BG, _OK_2, _OK_T, _OK_FG, _CHIP_BG, _SKY
        _th = _th_from_view(self)
        _TEXT = _TEXT_2 = _th["text"]
        _MUTED = _th["muted"]
        _BORDER = _BORDER2 = _th["border"]
        _PANEL = _th["panel"]
        _ALT = _ALT_2 = _th["alt"]
        _HDR = _th.get("hdr", _th["border"])
        _CHIP_T = _th["chip_text"]
        _ERR = _th["err"]
        _ERR_BG = _ERR_2 = _th["err_bg"]
        _SOFT_P = _th["soft"]
        _WARN = _WARN_T = _th["warn"]
        _WARN_BG = _WARN_2 = _th["warn_bg"]
        _OK_BG = _OK_2 = _th["ok_bg"]
        _OK_T = _OK_FG = _th["ok"]
        _CHIP_BG = _th["chip_bg"]
        _SKY = _th["accent"]
        if self._general_mode and self._general_manager_ids:
            import flet as _ft

            from views.general_wrapper import build_general_sections

            _cid, _db = self.company_id, self.db
            _ar = self.active_region

            def _rid_for(_ar=_ar, _db=_db, _cid=_cid):
                if not _ar:
                    return None
                for r in _db.get_company_regions(_cid) or []:
                    if isinstance(r, dict) and r.get("name") == _ar:
                        return r.get("id")
                return None

            _rid = _rid_for()
            # Use shared map for counts if available to avoid extra DB call
            _shared_g = [
                v
                for k, v in self._shared_orders_map.items()
                if not str(k).startswith("__") and isinstance(v, dict) and v.get("id")
            ]
            if _shared_g:
                _all_orders = [
                    o
                    for o in _shared_g
                    if not _rid or str(o.get("region_id") or "").strip() == _rid
                ]
            else:
                _all_orders = _db.get_orders(_cid, _rid) or []
            _mgr_counts: dict[str, int] = {}
            for _o in _all_orders:
                if not isinstance(_o, dict):
                    continue
                _m = str(_o.get("manager_id") or "").strip()
                if _m:
                    _mgr_counts[_m] = _mgr_counts.get(_m, 0) + 1

            def _count_orders(mid, _counts=_mgr_counts):
                return _counts.get(mid, 0)

            _table_btn = _ft.OutlinedButton(
                "Таблиця",
                icon=_ft.Icons.TABLE_CHART_OUTLINED,
                height=35,
                style=_ft.ButtonStyle(
                    text_style=_ft.TextStyle(size=11),
                    padding=_ft.padding.symmetric(horizontal=10, vertical=2),
                ),
                on_click=lambda _: self._open_orders_table_dialog(),
            )
            build_general_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={
                    "active_region": self.active_region,
                    "quick_filter": self.quick_filter,
                },
                count_getter=_count_orders,
                top_controls=[_ft.Row([_table_btn], spacing=8)],
            )
            return

        # All regions → region sub-groups (single-user mode)
        if not self.active_region:
            from views.general_wrapper import build_region_sections

            _cid2, _db2, _mid2 = self.company_id, self.db, self.manager_id
            # Use shared map for counts if available (avoids extra DB call)
            _shared2 = [
                v
                for k, v in self._shared_orders_map.items()
                if not str(k).startswith("__") and isinstance(v, dict) and v.get("id")
            ]
            _all2 = _shared2 if _shared2 else (_db2.get_orders(_cid2, None) or [])
            _rcounts2: dict[str, int] = {}
            for _o2 in _all2:
                if not isinstance(_o2, dict):
                    continue
                if _mid2 and str(_o2.get("manager_id") or "").strip() != _mid2:
                    continue
                _r2 = str(_o2.get("region_id") or "").strip()
                if _r2:
                    _rcounts2[_r2] = _rcounts2.get(_r2, 0) + 1

            def _count_rcpo(rid, _c=_rcounts2):
                return _c.get(rid, 0)

            build_region_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={"quick_filter": self.quick_filter},
                count_getter=_count_rcpo,
                region_kwarg_name="active_region",
            )
            return

        self._attach_resize_handler()
        self._initial_render_done = False  # reset so first _render() scrolls to top
        self._render_loading_stub()
        self._async_build_token = int(self._async_build_token or 0) + 1
        token = int(self._async_build_token or 0)
        page = self._pg()
        if page and hasattr(page, "run_task"):

            async def _runner():
                await asyncio.sleep(0.03)  # give Flutter time to paint the spinner
                if token != int(self._async_build_token or 0):
                    return
                await self._build_async()

            try:
                page.run_task(_runner)
                if not self._workspace_sync_loop_started:
                    self._workspace_sync_loop_started = True
                    with contextlib.suppress(Exception):
                        page.run_task(self._workspace_sync_loop)
                if not getattr(self, "_bg_sync_loop_started", False):
                    self._bg_sync_loop_started = True
                    with contextlib.suppress(Exception):
                        page.run_task(self._bg_sync_loop)
                return
            except Exception:
                pass
        self._load()
        self._render()
