from typing import Optional

import flet as ft
from views.mobile_layout_helpers import is_mobile, make_scrollable_chip_row
from views.number_format import format_maybe_numeric_text, safe_float
from views.orders_view import _wrap_text_for_width

# ---------------------------------------------------------------------------
# Theme colour shims — module defaults, shadowed by live theme at render time
# ---------------------------------------------------------------------------
_BORDER  = '#CBD5E1'
_TEXT    = '#111827'
_PANEL   = '#FFFFFF'
_ERR_BG  = '#FEE2E2'
_ALT     = '#F1F5F9'
_TEXT2   = '#1E293B'
_CHIP_BG = '#DBEAFE'
_ACCENT  = '#1D4ED8'
_OK_BG   = '#DCFCE7'
_OK_T    = '#166534'
_ERR_DK  = '#991B1B'
_WARN_BG = '#FEF3C7'
_WARN_T  = '#92400E'
_SOFT_O  = '#FDE8D8'
_ERR_O   = '#C2410C'
_MUTED   = '#475569'
_HDR     = '#E2E8F0'
_CHIP_T  = '#1E3A8A'
_ALT2    = '#F8FAFC'
_SOFT_P  = '#EDE9FE'


def _th_from_view(view) -> dict:
    from views.themes import get_active_theme
    return get_active_theme(str(getattr(view, 'manager_id', '') or ''))


def _safe_float(v, default: Optional[float] = None) -> Optional[float]:
    return safe_float(v, default)


def build_stock_counters(view, rows: list) -> ft.Row:
    _th = _th_from_view(view)
    _BORDER = _HDR = _th["border"]; _TEXT = _TEXT2 = _th["text"]; _MUTED = _th["muted"]
    _PANEL = _th["panel"]; _ERR_BG = _th["err_bg"]; _ALT = _ALT2 = _th["alt"]
    _CHIP_BG = _th["chip_bg"]; _ACCENT = _th["icon_color"]; _OK_BG = _th["ok_bg"]
    _OK_T = _th["ok"]; _ERR_DK = _th["err"]; _WARN_BG = _th["warn_bg"]; _WARN_T = _th["warn"]
    _SOFT_O = _SOFT_P = _th["soft"]; _ERR_O = _th["err"]; _CHIP_T = _th["chip_text"]
    # Count from all warehouse rows (before presence filter) for correct totals.
    all_rows = view._warehouse_rows or []
    with_qty = without_qty = with_cost = without_cost = without_both = 0
    for row in all_rows:
        has_q = (_safe_float(row.get("qty"), None) or 0) > 0
        has_c = (_safe_float(row.get("amount"), None) or 0) > 0
        if has_q:
            with_qty += 1
        else:
            without_qty += 1
        if has_c:
            with_cost += 1
        else:
            without_cost += 1
        if not has_q and not has_c:
            without_both += 1

    active = getattr(view, "_stock_presence_filter", "all")

    def _chip(label, key, count, bg, fg):
        is_active = active == key
        border = ft.border.all(2, _TEXT2) if is_active else None

        def _click(_e, k=key):
            view._stock_presence_filter = "all" if active == k else k
            view._render()

        return ft.Container(
            bgcolor=bg,
            border_radius=6,
            border=border,
            padding=ft.padding.symmetric(horizontal=6, vertical=2),
            ink=True,
            on_click=_click,
            content=ft.Row(
                [
                    ft.Text(label, size=9, color=fg, weight="w600" if is_active else None),
                    ft.Container(
                        bgcolor=fg,
                        border_radius=10,
                        padding=ft.padding.symmetric(horizontal=5, vertical=1),
                        content=ft.Text(str(count), size=9, color=_PANEL, weight="bold"),
                    ),
                ],
                spacing=4,
                tight=True,
            ),
        )

    _chips = [
        _chip(view.t("stock.chip.total", "Товарів"), "all", len(all_rows), _CHIP_BG, _ACCENT),
        _chip(view.t("stock.chip.with_qty", "З кількістю"), "with_qty", with_qty, _OK_BG, _OK_T),
        _chip(view.t("stock.chip.without_qty", "Без кількості"), "without_qty", without_qty, _ERR_BG, _ERR_DK),
        _chip(view.t("stock.chip.with_cost", "З вартістю"), "with_cost", with_cost, _WARN_BG, _WARN_T),
        _chip(view.t("stock.chip.without_cost", "Без вартості"), "without_cost", without_cost, _SOFT_O, _ERR_O),
        _chip(view.t("stock.chip.without_both", "Без обох"), "without_both", without_both, _ALT, _MUTED),
    ]
    # On mobile, wrap=True breaks this counter row onto 2-3 lines the moment
    # width runs out, stealing vertical space from the stock table below it —
    # the same layout bug already fixed in the sibling cost-price views. Keep
    # it a single horizontally-scrollable line on mobile; desktop is unchanged.
    if is_mobile(getattr(view.content_area, "page", None)):
        return make_scrollable_chip_row(_chips, spacing=6)
    return ft.Row(_chips, spacing=6, wrap=True)


def build_stock_totals(view, rows: list) -> ft.Row:
    """Summary stat tiles for the currently filtered rows — total quantity and
    total warehouse value, recomputed on every search/filter change."""
    _th = _th_from_view(view)
    _BORDER = _th["border"]; _TEXT = _th["text"]; _MUTED = _th["muted"]
    _PANEL = _th["panel"]; _ACCENT = _th["icon_color"]

    total_qty = sum((_safe_float(r.get("qty"), None) or 0) for r in rows)
    total_amount = sum((_safe_float(r.get("amount_stock_ccy"), None) or 0) for r in rows)
    stock_ccy = str((rows[0].get("stock_ccy") if rows else None) or "USD").upper()

    qty_value_text = ft.Text(view._fmt_money(total_qty), size=16, weight="bold", color=_TEXT)
    sum_value_text = ft.Text(view._fmt_money(total_amount, stock_ccy), size=16, weight="bold", color=_TEXT)
    # Refs kept so _update_stock_totals_inplace() can patch these two numbers
    # live on every qty/amount edit, without a full _render() (no flicker/scroll-jump).
    view._stock_totals_refs = {"qty_text": qty_value_text, "sum_text": sum_value_text}

    def _tile(icon, label, value_control):
        return ft.Container(
            bgcolor=_PANEL,
            border=ft.border.all(1, _BORDER),
            border_radius=8,
            padding=ft.padding.symmetric(horizontal=14, vertical=8),
            content=ft.Row(
                [
                    ft.Icon(icon, size=18, color=_ACCENT),
                    ft.Column(
                        [
                            ft.Text(label, size=10, color=_MUTED),
                            value_control,
                        ],
                        spacing=0,
                        tight=True,
                    ),
                ],
                spacing=8,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    _tiles = [
        _tile(
            ft.Icons.INVENTORY_2_OUTLINED,
            view.t("stock.total_qty", "Загальна кількість"),
            qty_value_text,
        ),
        _tile(
            ft.Icons.PAYMENTS_OUTLINED,
            view.t("stock.total_sum", "Загальна сума"),
            sum_value_text,
        ),
    ]
    # Same mobile-only fix as build_stock_counters() above: keep these two
    # summary tiles on one horizontally-scrollable line on mobile instead of
    # letting wrap=True stack them and push the table down.
    if is_mobile(getattr(view.content_area, "page", None)):
        return make_scrollable_chip_row(_tiles, spacing=10)
    return ft.Row(_tiles, spacing=10, wrap=True)


def build_stock_filter_bar(view) -> ft.Container:
    _th = _th_from_view(view)
    _BORDER = _HDR = _th["border"]; _TEXT = _TEXT2 = _th["text"]; _MUTED = _th["muted"]
    _PANEL = _th["panel"]; _ALT = _ALT2 = _th["alt"]
    supplier_opts = [("all", view.t("orders.filter.all_suppliers", "Всі постачальники"))] + [
        (str(supplier.get("id")), str(supplier.get("name") or "")[:45])
        for supplier in view._suppliers
        if isinstance(supplier, dict) and supplier.get("id")
    ]
    category_opts = [("all", view.t("orders.filter.all_categories", "Всі категорії"))] + [
        (str(category.get("id")), str(category.get("name") or "")[:45])
        for category in view._categories
        if isinstance(category, dict) and category.get("id")
    ]

    query_tf = ft.TextField(
        hint_text=view.t("stock.filter.search_hint", "Пошук товару..."),
        value=view._stock_filter_query,
        width=220,
        height=32,
        text_size=11,
        content_padding=ft.padding.symmetric(horizontal=8, vertical=6),
        on_change=lambda e: set_stock_query(view, getattr(e.control, "value", "")),
    )

    selected_supplier = view._stock_filter_supplier or "all"
    supplier_label = next(
        (label for key, label in supplier_opts if key == selected_supplier),
        view.t("orders.filter.all_suppliers", "Всі постачальники"),
    )
    sup_dd = ft.PopupMenuButton(
        icon=None,
        menu_position=ft.PopupMenuPosition.UNDER,
        items=[
            ft.PopupMenuItem(
                content=ft.Text(label, size=10),
                on_click=view._guard_ui_action(
                    "stock_supplier_filter",
                    lambda key=key: set_stock_filter(view, "supplier", key),
                    view.t("cost_orders.err.filter", "Помилка фільтра"),
                ),
            )
            for key, label in supplier_opts
        ],
        content=ft.Container(
            width=180,
            height=32,
            border=ft.border.all(1, _BORDER),
            border_radius=6,
            bgcolor=_PANEL,
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            content=ft.Text(
                supplier_label,
                size=11,
                no_wrap=True,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
        ),
    )

    selected_category = view._stock_filter_category or "all"
    category_label = next(
        (label for key, label in category_opts if key == selected_category),
        view.t("orders.filter.all_categories", "Всі категорії"),
    )
    cat_dd = ft.PopupMenuButton(
        icon=None,
        menu_position=ft.PopupMenuPosition.UNDER,
        items=[
            ft.PopupMenuItem(
                content=ft.Text(label, size=10),
                on_click=view._guard_ui_action(
                    "stock_category_filter",
                    lambda key=key: set_stock_filter(view, "category", key),
                    view.t("cost_orders.err.filter", "Помилка фільтра"),
                ),
            )
            for key, label in category_opts
        ],
        content=ft.Container(
            width=160,
            height=32,
            border=ft.border.all(1, _BORDER),
            border_radius=6,
            bgcolor=_PANEL,
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            content=ft.Text(
                category_label,
                size=11,
                no_wrap=True,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
        ),
    )

    return ft.Container(
        bgcolor=_ALT,
        border_radius=6,
        padding=ft.padding.symmetric(horizontal=6, vertical=4),
        content=ft.Row([query_tf, sup_dd, cat_dd], spacing=8, wrap=True),
    )


def set_stock_query(view, value: str) -> None:
    view._stock_filter_query = str(value or "")
    view._render()


def set_stock_filter(view, key: str, value: str) -> None:
    if key == "supplier":
        view._stock_filter_supplier = value
    elif key == "category":
        view._stock_filter_category = value
    view._render()


def build_stock_table(
    view,
    rows: list,
    stock_ccy: str = "USD",
    orders_unit_cost_ccy: str = "USD",
    new_price_ccy: str = "USD",
    include_header_row: bool = True,
) -> dict:
    _th = _th_from_view(view)
    _BORDER = _HDR = _th["border"]; _TEXT = _TEXT2 = _th["text"]; _MUTED = _th["muted"]
    _PANEL = _th["panel"]; _ERR_BG = _th["err_bg"]; _ALT = _ALT2 = _th["alt"]
    _CHIP_BG = _th["chip_bg"]; _ACCENT = _th["icon_color"]; _OK_BG = _th["ok_bg"]
    _OK_T = _th["ok"]; _ERR_DK = _th["err"]; _WARN_BG = _th["warn_bg"]; _WARN_T = _th["warn"]
    _SOFT_O = _SOFT_P = _th["soft"]; _ERR_O = _th["err"]; _CHIP_T = _th["chip_text"]
    defaults = view._column_defaults() if hasattr(view, "_column_defaults") else {}
    col_px_map = (
        defaults.get("stock_col_width_px")
        if isinstance(defaults.get("stock_col_width_px"), dict)
        else {}
    )
    _stock_tier_widths = (
        defaults.get("stock_tier_widths")
        if isinstance(defaults.get("stock_tier_widths"), dict)
        else {}
    )
    all_tiers = [tier for tier in (view._price_tiers or []) if isinstance(tier, dict)]
    visible_ids = set(view._visible_stock_tier_ids())
    visible_base_ids = set(view._visible_stock_base_column_ids())
    visible_tiers = [
        tier
        for tier in all_tiers
        if str((tier or {}).get("id") or "").strip() in visible_ids
    ]

    stock_ccy = str(stock_ccy or "USD").upper().strip() or "USD"
    orders_unit_cost_ccy = str(orders_unit_cost_ccy or "USD").upper().strip() or "USD"
    new_price_ccy = str(new_price_ccy or "USD").upper().strip() or "USD"
    # Accumulate across multiple build_stock_table() calls within one _render()
    # (main table + the archive section's own mini-table) instead of clobbering
    # whichever was built first — reset once per full render, not per call.
    if not isinstance(getattr(view, "_new_price_margin_refs", None), dict):
        view._new_price_margin_refs = {}
    row_height = int(
        max(1, min(120, int(_safe_float(defaults.get("stock_row_h"), 50) or 50)))
    )
    text_size = int(
        max(6, min(32, int(_safe_float(defaults.get("stock_text_size"), 9) or 9)))
    )
    h_align = str(defaults.get("stock_text_h_align") or "center").strip().lower()
    if h_align not in {"left", "center", "right"}:
        h_align = "center"
    v_align = str(defaults.get("stock_text_v_align") or "center").strip().lower()
    if v_align not in {"top", "center", "bottom"}:
        v_align = "center"
    wrap = bool(defaults.get("stock_text_wrap", True))

    text_align = (
        ft.TextAlign.LEFT
        if h_align == "left"
        else (ft.TextAlign.RIGHT if h_align == "right" else ft.TextAlign.CENTER)
    )
    vertical_align = (
        ft.VerticalAlignment.START
        if v_align == "top"
        else (
            ft.VerticalAlignment.END
            if v_align == "bottom"
            else ft.VerticalAlignment.CENTER
        )
    )
    ax = -1.0 if h_align == "left" else (1.0 if h_align == "right" else 0.0)
    ay = -1.0 if v_align == "top" else (1.0 if v_align == "bottom" else 0.0)
    cell_align = ft.Alignment(ax, ay)
    tf_text_size = text_size
    tf_vpad = max(0, int((row_height - (tf_text_size + 6)) / 2))
    base_map = {
        "product": ("product", view.t("orders.filter.product", "Товар"), 160),
        "qty": ("qty", view.t("stock.col.qty", "К-сть склад"), 70),
        "amount_stock": (
            "amount_stock",
            view.t("stock.col.amount_stock", "Вартість склад {ccy}").format(ccy=view._currency_symbol(stock_ccy)),
            80,
        ),
        "unit_cost_stock": (
            "unit_cost_stock",
            view.t("stock.col.unit_cost_stock", "С/В склад {ccy}").format(ccy=view._currency_symbol(stock_ccy)),
            70,
        ),
        "orders_unit_cost": (
            "orders_unit_cost",
            view.t("stock.col.orders_unit_cost", "С/В замовл. {ccy}").format(ccy=view._currency_symbol(orders_unit_cost_ccy)),
            80,
        ),
        "unit_cost_blended": (
            "unit_cost_blended",
            view.t("stock.col.unit_cost_blended", "С/В {ccy}").format(ccy=view._currency_symbol(stock_ccy)),
            70,
        ),
    }
    ordered_base_ids = []
    try:
        ordered_base_ids = list(view._ordered_stock_base_column_ids())
    except Exception:
        ordered_base_ids = list(base_map.keys())
    base_columns = [
        base_map[col_id] for col_id in ordered_base_ids if col_id in base_map
    ]
    non_tier_keys = [k for k, _l, _w in base_columns] + ["new_price", "new_margin"]
    ordered_non_tier = [k for k in ordered_base_ids if k in non_tier_keys] + [
        k for k in non_tier_keys if k not in set(ordered_base_ids)
    ]

    _base_key_to_idx = {k: i for i, (k, _l, _w) in enumerate(base_columns)}
    _base_len = len(base_columns)

    def _reorder_non_tier_segment(seq: list):
        if not isinstance(seq, list) or len(seq) < (_base_len + 2):
            return seq
        base_part = list(seq[:_base_len])
        tier_part = list(seq[_base_len:-2])
        tail_part = list(seq[-2:])
        key_to_cell = {
            k: base_part[i] for k, i in _base_key_to_idx.items() if i < len(base_part)
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
        reordered = [key_to_cell[k] for k in ordered_non_tier if k in key_to_cell]
        return reordered + tier_part

    base_headers = [label for _, label, _ in base_columns]
    for tier in visible_tiers:
        nm = str((tier or {}).get("name") or view.t("cost_orders.tier_fallback", "Рівень"))
        base_headers.append(nm)
        base_headers.append("")  # merged into price column
    base_headers += [
        view.t("cost_orders.col.new_price", "Нова ціна {ccy}").format(ccy=view._currency_symbol(new_price_ccy)),
        "",  # new_margin merged
    ]

    widths = (
        [
            width if key == "product" or key in visible_base_ids else 0
            for key, _label, width in base_columns
        ]
        + [
            w
            for _vt in visible_tiers
            for w in [
                int((_stock_tier_widths.get(str((_vt or {}).get("id") or "").strip()) or {}).get("price_w") or 50)
                + int((_stock_tier_widths.get(str((_vt or {}).get("id") or "").strip()) or {}).get("margin_w") or 50),
                0,
            ]
        ]
        + [
            100 if "new_price" in visible_base_ids else 0,
            0,  # new_margin merged into new_price column
        ]
    )
    base_len = len(base_columns)
    base_scaled = []
    for idx, (key, _label, _w) in enumerate(base_columns):
        w = int(widths[idx] or 0)
        if w <= 0:
            base_scaled.append(0)
            continue
        px = _safe_float(col_px_map.get(key), None)
        if px is None:
            base_scaled.append(w)
        else:
            base_scaled.append(int(max(35, min(1200, int(px)))))
    widths = base_scaled + list(widths[base_len:])
    if len(widths) >= 2:
        w = int(widths[-2] or 0)
        if w > 0:
            px = _safe_float(col_px_map.get("new_price"), None)
            if px is not None:
                widths[-2] = int(max(70, min(1200, int(px))))
    base_widths = widths[: len(base_columns)]
    width_by_key = {
        key: base_widths[idx] for idx, (key, _label, _w) in enumerate(base_columns)
    }

    frozen_count = 1
    left_rows = []
    right_rows = []

    header_cells = []
    for idx, header in enumerate(base_headers):
        if int(widths[idx] or 0) <= 0:
            header_cells.append(ft.Container(width=0, height=0, visible=False))
            continue
        header_cells.append(
            ft.Container(
                width=widths[idx],
                height=max(26, int(row_height) - 2),
                bgcolor=_HDR,
                border=ft.border.all(1, _BORDER),
                alignment=ft.Alignment(0, 0),
                content=ft.Text(
                    header,
                    size=text_size,
                    weight="bold",
                    color=_CHIP_T,
                    text_align=text_align,
                    no_wrap=(not wrap),
                    max_lines=(None if wrap else 1),
                    overflow=(
                        ft.TextOverflow.VISIBLE if wrap else ft.TextOverflow.ELLIPSIS
                    ),
                ),
            )
        )
    header_cells = _reorder_non_tier_segment(header_cells)
    left_header_row = ft.Row(header_cells[:frozen_count], spacing=0)
    right_header_row = ft.Row(header_cells[frozen_count:], spacing=0)
    if include_header_row:
        left_rows.append(left_header_row)
        right_rows.append(right_header_row)

    _default_row_height = row_height
    for idx, row in enumerate(rows):
        bg = _PANEL if idx % 2 == 0 else _ALT2
        cells = []

        # Grow this row's height (only this iteration — every cell builder
        # below reads the `row_height` closure variable directly) if the
        # product name needs more than one line at the product column's
        # width. Long disambiguated names ("Name | Кат: X | Постач: Y") would
        # otherwise clip inside the fixed default height.
        row_height = _default_row_height
        _name_for_h = str(row.get("name") or "")
        _prod_w_for_h = int(width_by_key.get("product", 0) or 0)
        if wrap and _name_for_h and _prod_w_for_h > 0:
            _wrapped_for_h = _wrap_text_for_width(_name_for_h, _prod_w_for_h)
            _needed_lines = max(1, _wrapped_for_h.count("\n") + 1)
            if _needed_lines > 1:
                row_height = int(
                    min(220, max(_default_row_height, 16 * _needed_lines + 6))
                )

        def _cell_text(
            text: str,
            w: int,
            align=ft.TextAlign.CENTER,
            cell_bg=None,
            color=_TEXT,
        ):
            if int(w or 0) <= 0:
                cells.append(ft.Container(width=0, height=0, visible=False))
                return
            cells.append(
                ft.Container(
                    width=w,
                    height=row_height,
                    bgcolor=cell_bg or bg,
                    border=ft.border.all(1, _BORDER),
                    alignment=cell_align,
                    content=ft.Text(
                        format_maybe_numeric_text(str(text or "")),
                        size=text_size,
                        text_align=text_align,
                        color=color,
                        no_wrap=(not wrap),
                        max_lines=(None if wrap else 1),
                        overflow=(
                            ft.TextOverflow.VISIBLE
                            if wrap
                            else ft.TextOverflow.ELLIPSIS
                        ),
                    ),
                )
            )

        _qty_val = _safe_float(row.get("qty"), None)
        _amt_val = _safe_float(row.get("amount_stock_ccy"), None)
        _qty_empty = _qty_val is None
        _amt_empty = _amt_val is None
        _qty_bg = _ERR_BG if _qty_empty else bg
        _amt_bg = _ERR_BG if _amt_empty else bg

        qty_tf = ft.TextField(
            value=view._fmt_money(_qty_val),
            width=width_by_key.get("qty", 0),
            height=row_height,
            text_size=tf_text_size,
            dense=True,
            filled=True,
            fill_color=_qty_bg,
            border=ft.InputBorder.NONE,
            border_width=0,
            border_radius=0,
            content_padding=ft.padding.symmetric(horizontal=0, vertical=tf_vpad),
            text_align=text_align,
            text_vertical_align=vertical_align,
            read_only=view.is_read_only,
        )
        view._bind_commit_handlers(
            qty_tf,
            lambda raw_value, current_row=row: view._save_warehouse_row(
                current_row.get("product_id") or "",
                {"qty": _safe_float(raw_value, None)},
            ),
            tag="stock_qty",
            error_text="Помилка збереження",
        )
        qty_cell = ft.Container(
            width=width_by_key.get("qty", 0),
            height=row_height,
            visible=bool(int(width_by_key.get("qty", 0) or 0) > 0),
            bgcolor=_qty_bg,
            border=ft.border.all(1, _BORDER),
            alignment=cell_align,
            padding=ft.padding.all(0),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=qty_tf,
        )

        amount_tf = ft.TextField(
            value=view._fmt_money(_amt_val),
            width=width_by_key.get("amount_stock", 0),
            height=row_height,
            text_size=tf_text_size,
            dense=True,
            filled=True,
            fill_color=_amt_bg,
            border=ft.InputBorder.NONE,
            border_width=0,
            border_radius=0,
            content_padding=ft.padding.symmetric(horizontal=0, vertical=tf_vpad),
            text_align=text_align,
            text_vertical_align=vertical_align,
            read_only=view.is_read_only,
        )
        view._bind_commit_handlers(
            amount_tf,
            lambda raw_value, current_row=row: view._save_warehouse_row(
                current_row.get("product_id") or "",
                {
                    "amount": _safe_float(raw_value, None),
                    "ccy": stock_ccy,
                },
            ),
            tag="stock_amount",
            error_text="Помилка збереження",
        )
        amount_cell = ft.Container(
            width=width_by_key.get("amount_stock", 0),
            height=row_height,
            visible=bool(int(width_by_key.get("amount_stock", 0) or 0) > 0),
            bgcolor=_amt_bg,
            border=ft.border.all(1, _BORDER),
            alignment=cell_align,
            padding=ft.padding.all(0),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=amount_tf,
        )

        base_cells_by_key: dict[str, ft.Control] = {}
        _prod_cell = ft.Container(
            width=width_by_key.get("product", 0),
            height=row_height,
            bgcolor=bg,
            border=ft.border.all(1, _BORDER),
            alignment=ft.Alignment(-1.0, float(cell_align.y if hasattr(cell_align, "y") else 0)),
            tooltip=ft.Tooltip(message=str(row.get("name") or ""), wait_duration=0) if row.get("name") else None,
            content=ft.Text(
                str(row.get("name") or "-"),
                size=text_size,
                text_align=ft.TextAlign.LEFT,
                color=_TEXT,
                no_wrap=(not wrap),
                max_lines=(None if wrap else 1),
                overflow=(
                    ft.TextOverflow.VISIBLE if wrap else ft.TextOverflow.ELLIPSIS
                ),
            ),
        )
        _stock_pid = str(row.get("product_id") or "").strip()
        if _stock_pid:
            from views.product_quick_view import product_icon_btn
            _spid_btn = product_icon_btn(
                _stock_pid, view.content_area, view.db, view.user_data, size=10
            )
            _row_btns = [_spid_btn]
            if (
                hasattr(view, "archive_stock_row")
                and hasattr(view, "restore_stock_row")
                and not getattr(view, "is_read_only", False)
            ):
                _is_archived_row = bool((row.get("product") or {}).get("is_archived"))
                _row_btns.append(
                    ft.IconButton(
                        icon=(
                            ft.Icons.UNARCHIVE_OUTLINED
                            if _is_archived_row
                            else ft.Icons.ARCHIVE_OUTLINED
                        ),
                        icon_size=10,
                        icon_color=_ACCENT if _is_archived_row else "#94A3B8",
                        tooltip=(
                            view.t("stock.restore", "Відновити з архіву")
                            if _is_archived_row
                            else view.t("stock.archive", "Архівувати")
                        ),
                        on_click=(
                            (lambda _e, pid=_stock_pid: view.restore_stock_row(pid))
                            if _is_archived_row
                            else (lambda _e, pid=_stock_pid: view.archive_stock_row(pid))
                        ),
                        style=ft.ButtonStyle(padding=ft.padding.all(0)),
                        width=18,
                        height=18,
                    )
                )
            _prod_cell.content = ft.Row(
                [_prod_cell.content, *_row_btns],
                spacing=2,
                tight=True,
                wrap=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            _prod_cell.alignment = ft.Alignment(-1, 0)
            _prod_cell.padding = ft.padding.only(left=4, right=2)
        base_cells_by_key["product"] = _prod_cell
        base_cells_by_key["qty"] = qty_cell
        base_cells_by_key["amount_stock"] = amount_cell

        # unit_cost_stock — зберігаємо ref для in-place оновлення
        _ucs_w = width_by_key.get("unit_cost_stock", 0)
        _ucs_txt = ft.Text(
            view._fmt_money(_safe_float(row.get("unit_cost_stock"), None), stock_ccy),
            size=text_size,
            text_align=text_align,
            color=_TEXT,
            no_wrap=(not wrap),
            max_lines=(None if wrap else 1),
            overflow=ft.TextOverflow.VISIBLE if wrap else ft.TextOverflow.ELLIPSIS,
        )
        _ucs_cell = ft.Container(
            width=_ucs_w,
            height=row_height,
            bgcolor=bg,
            border=ft.border.all(1, _BORDER),
            alignment=cell_align,
            content=_ucs_txt,
            visible=bool(int(_ucs_w or 0) > 0),
        )
        base_cells_by_key["unit_cost_stock"] = _ucs_cell

        # orders_unit_cost — зберігаємо ref для in-place оновлення при зміні С/В Замовлення
        _ouc_w = width_by_key.get("orders_unit_cost", 0)
        _ouc_bg = _SOFT_P if not row.get("has_active_orders", True) else bg
        _ouc_txt = ft.Text(
            view._fmt_money(
                _safe_float(row.get("orders_unit_cost"), None), orders_unit_cost_ccy
            ),
            size=text_size,
            text_align=text_align,
            color=_TEXT,
            no_wrap=(not wrap),
            max_lines=(None if wrap else 1),
            overflow=ft.TextOverflow.VISIBLE if wrap else ft.TextOverflow.ELLIPSIS,
        )
        _ouc_cell = ft.Container(
            width=_ouc_w,
            height=row_height,
            bgcolor=_ouc_bg,
            border=ft.border.all(1, _BORDER),
            alignment=cell_align,
            content=_ouc_txt,
            visible=bool(int(_ouc_w or 0) > 0),
        )
        base_cells_by_key["orders_unit_cost"] = _ouc_cell

        # unit_cost_blended — зберігаємо ref для in-place оновлення
        _ucb_w = width_by_key.get("unit_cost_blended", 0)
        _ucb_txt = ft.Text(
            view._fmt_money(_safe_float(row.get("unit_cost_blended"), None), stock_ccy),
            size=text_size,
            text_align=text_align,
            color=_TEXT,
            no_wrap=(not wrap),
            max_lines=(None if wrap else 1),
            overflow=ft.TextOverflow.VISIBLE if wrap else ft.TextOverflow.ELLIPSIS,
        )
        _ucb_cell = ft.Container(
            width=_ucb_w,
            height=row_height,
            bgcolor=bg,
            border=ft.border.all(1, _BORDER),
            alignment=cell_align,
            content=_ucb_txt,
            visible=bool(int(_ucb_w or 0) > 0),
        )
        base_cells_by_key["unit_cost_blended"] = _ucb_cell

        # Зберігаємо refs для in-place оновлення без ре-рендеру
        pid_str = str(row.get("product_id") or "")
        if pid_str and hasattr(view, "_stock_computed_refs"):
            view._stock_computed_refs[pid_str] = {
                "unit_cost_stock": _ucs_txt,
                "unit_cost_blended": _ucb_txt,
                "qty_cell": qty_cell,
                "qty_tf": qty_tf,
                "amount_cell": amount_cell,
                "amount_tf": amount_tf,
                "row_bg": bg,
                "orders_unit_cost_txt": _ouc_txt,
                "orders_unit_cost_ccy": orders_unit_cost_ccy,
            }

        for key, _label, _w in base_columns:
            ctrl = base_cells_by_key.get(key)
            if ctrl is not None:
                cells.append(ctrl)

        target = view._target_margin(row.get("category_id") or "")
        tier_col_idx = len(base_columns)
        pairs = row.get("tier_pairs") or []
        _tier_cell_refs = []
        for tier, pair in zip(all_tiers, pairs):
            tier_id = str((tier or {}).get("id") or "").strip()
            if tier_id not in visible_ids:
                continue
            sale = _safe_float(pair.get("sale_price"), None)
            sale_ccy = pair.get("sale_ccy") or "USD"
            margin = pair.get("margin")
            bg_m, fg_m = view._margin_colors(margin, target)
            combo_tw = widths[tier_col_idx]
            tier_col_idx += 2
            _st_sale = ft.Text(
                view._fmt_money(sale, sale_ccy),
                size=text_size,
                color=_TEXT,
                text_align=text_align,
                no_wrap=True,
                overflow=ft.TextOverflow.ELLIPSIS,
            )
            _st_margin = ft.Text(
                "-" if margin is None else f"{margin * 100:.1f}%",
                size=text_size,
                color=fg_m,
                weight="bold",
                text_align=text_align,
                no_wrap=True,
            )
            _st_c = ft.Container(
                width=combo_tw,
                height=row_height,
                bgcolor=bg_m,
                border=ft.border.all(1, _BORDER),
                padding=ft.padding.symmetric(horizontal=2, vertical=1),
                content=ft.Column(
                    [_st_sale, _st_margin],
                    spacing=1,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    tight=True,
                ),
            )
            _tier_cell_refs.append((_st_c, _st_sale, _st_margin))
            cells.append(_st_c)
            cells.append(ft.Container(width=0, height=0, visible=False))
        # Зберігаємо tier refs для in-place оновлення
        if pid_str and hasattr(view, "_stock_computed_refs"):
            _sc_refs = view._stock_computed_refs.get(pid_str)
            if isinstance(_sc_refs, dict):
                _sc_refs["tier_cells"] = _tier_cell_refs

        half_h = max(17, row_height // 2)
        new_tf = ft.TextField(
            value=view._fmt_money(_safe_float(row.get("new_price"), None)),
            width=widths[-2] - 2,
            height=half_h,
            text_size=tf_text_size,
            dense=True,
            filled=True,
            fill_color="transparent",
            border=ft.InputBorder.NONE,
            border_width=0,
            border_radius=0,
            content_padding=ft.padding.symmetric(horizontal=4, vertical=0),
            text_align=text_align,
            text_vertical_align=vertical_align,
        )
        view._bind_commit_handlers(
            new_tf,
            lambda raw_value, current_row=row: view._save_warehouse_row(
                current_row.get("product_id") or "",
                {
                    "new_price": _safe_float(raw_value, None),
                    "new_ccy": str(current_row.get("new_ccy") or "USD").upper(),
                },
            ),
            tag="stock_new_price",
            error_text="Помилка збереження",
        )
        m_bg, m_fg = view._margin_colors(row.get("new_margin"), target)
        nm_half = ft.Container(
            width=widths[-2],
            height=half_h,
            bgcolor=m_bg,
            alignment=cell_align,
            content=ft.Text(
                (
                    "-"
                    if row.get("new_margin") is None
                    else f"{float(row.get('new_margin')) * 100:.1f}%"
                ),
                size=text_size,
                color=m_fg,
                weight="bold",
                text_align=text_align,
                no_wrap=True,
            ),
        )
        _outer_new = ft.Container(
            width=widths[-2],
            height=row_height,
            bgcolor=m_bg,
            visible=bool(int(widths[-2] or 0) > 0),
            border=ft.border.all(1, _BORDER),
            padding=ft.padding.all(0),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.Column(
                [
                    ft.Container(
                        content=new_tf,
                        bgcolor="transparent",
                        width=widths[-2],
                        height=half_h,
                        padding=ft.padding.all(0),
                    ),
                    nm_half,
                ],
                spacing=0,
                tight=True,
            ),
        )
        cells.append(_outer_new)
        _pid_ref = str(row.get("product_id") or "")
        if _pid_ref:
            view._new_price_margin_refs[_pid_ref] = {
                "nm_half": nm_half,
                "outer": _outer_new,
                "category_id": str(row.get("category_id") or ""),
                "unit_cost_blended": row.get("unit_cost_blended"),
                "stock_ccy": str(row.get("stock_ccy") or "USD").upper(),
                "new_ccy": str(row.get("new_ccy") or "USD").upper(),
            }
        cells.append(ft.Container(width=0, height=0, visible=False))

        cells = _reorder_non_tier_segment(cells)
        left_rows.append(ft.Row(cells[:frozen_count], spacing=0))
        right_rows.append(ft.Row(cells[frozen_count:], spacing=0))

    # Restore the table-wide default — the loop above may have left this
    # bumped up to fit the last row's wrapped product name, which must not
    # leak into the empty-state rows or the returned header_height below.
    row_height = _default_row_height

    if len(left_rows) == 1:
        left_rows.append(
            ft.Row(
                [
                    ft.Container(
                        width=widths[0],
                        height=row_height,
                        border=ft.border.all(1, _BORDER),
                        alignment=ft.Alignment(0, 0),
                        content=ft.Text(view.t("stock.no_products", "Немає товарів"), size=11, color=_TEXT),
                    )
                ],
                spacing=0,
            )
        )
        right_rows.append(
            ft.Row(
                [
                    ft.Container(
                        width=sum(widths[frozen_count:]),
                        height=row_height,
                        border=ft.border.all(1, _BORDER),
                        alignment=ft.Alignment(0, 0),
                        content=ft.Text("", size=11, color=_TEXT),
                    )
                ],
                spacing=0,
            )
        )

    frozen_width = sum(widths[:frozen_count])
    scroll_width = sum(widths[frozen_count:])
    return {
        "frozen": ft.Container(
            width=frozen_width,
            content=ft.Column(left_rows, spacing=0),
        ),
        "scrollable": ft.Container(
            width=scroll_width,
            content=ft.Column(right_rows, spacing=0),
        ),
        "frozen_width": frozen_width,
        "scroll_width": scroll_width,
        "left_header_row": left_header_row,
        "right_header_row": right_header_row,
        "header_height": max(26, int(row_height) - 2) + 2,
    }
