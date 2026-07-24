from typing import Optional

import flet as ft
import i18n
from views.number_format import safe_float

# ── Theme colour shims ────────────────────────────────────────────────────────
_TEXT   = "#0F172A"
_MUTED  = "#475569"
_BORDER = "#CBD5E1"
_PANEL  = "#FFFFFF"
_ALT    = "#F8FAFC"
_CHIP_T = "#1E3A8A"
_SOFT_I  = "#EFF6FF"
_HDR_BG  = "#EFF6FF"
_ERR     = "#DC2626"
_WARN    = "#B45309"
_WARN_BG = "#FEF3C7"


def _th_from_view(view) -> dict:
    from views.themes import get_active_theme
    return get_active_theme(str(getattr(view, "manager_id", "") or ""))


_CALCULATORS_SESSION_CACHE: dict[str, dict] = {}


def _cache_key(view, calc_name: str) -> str:
    company_id = str(getattr(view, "company_id", "") or "").strip()
    manager_id = str(getattr(view, "manager_id", "") or "").strip()
    region_id = str(getattr(view, "_region_id", "") or "").strip()
    return f"{calc_name}:{company_id}:{manager_id}:{region_id}"


def _get_cache(view, calc_name: str) -> dict:
    key = _cache_key(view, calc_name)
    payload = _CALCULATORS_SESSION_CACHE.get(key)
    return dict(payload) if isinstance(payload, dict) else {}


def _set_cache(view, calc_name: str, payload: dict) -> None:
    key = _cache_key(view, calc_name)
    _CALCULATORS_SESSION_CACHE[key] = dict(payload or {})


def _pick_ccy(value: Optional[str], currencies: list[str], fallback: str) -> str:
    ccy = str(value or "").upper().strip()
    if ccy and ccy in set(currencies or []):
        return ccy
    return str(fallback or "USD").upper().strip() or "USD"


def _set_field_value(control, value) -> None:
    if value is None:
        return
    try:
        control.value = str(value)
    except Exception:
        pass


def _safe_float(v, default: Optional[float] = None) -> Optional[float]:
    return safe_float(v, default)


def _convert_currency(view, amount: Optional[float], from_ccy: str, to_ccy: str):
    if amount is None:
        return None
    src = str(from_ccy or "USD").upper().strip() or "USD"
    dst = str(to_ccy or "USD").upper().strip() or "USD"
    if src == dst:
        return float(amount)
    try:
        return view._convert_currency(amount, src, dst)
    except Exception:
        return None


def _currency_options_for_region(view) -> list[str]:
    opts = []
    seen = set()

    def _add(ccy):
        key = str(ccy or "").upper().strip()
        if not key or key in seen:
            return
        seen.add(key)
        opts.append(key)

    for ccy in getattr(view, "CURRENCIES", []) or []:
        _add(ccy)

    defaults = {}
    try:
        defaults = view._column_defaults() or {}
    except Exception:
        defaults = {}
    for key in [
        "stock_ccy",
        "stock_new_price_ccy",
        "orders_unit_cost_ccy",
        "log_calc_ccy",
        "log_fact_ccy",
        "new_price_ccy",
    ]:
        _add(defaults.get(key))

    for method in getattr(view, "_delivery_methods", []) or []:
        if not isinstance(method, dict):
            continue
        _add(
            method.get("effective_price_currency") or method.get("base_price_currency")
        )

    if not opts:
        opts = ["USD"]
    return opts


def _delivery_tariffs_for_region(view) -> list[dict]:
    rows = []
    for method in getattr(view, "_delivery_methods", []) or []:
        if not isinstance(method, dict):
            continue
        name = str(method.get("name") or "").strip() or "Доставка"
        tariff = _safe_float(
            (
                method.get("effective_price_per_kg")
                if method.get("effective_price_per_kg") is not None
                else method.get("base_price_per_kg")
            ),
            None,
        )
        ccy = (
            str(
                method.get("effective_price_currency")
                or method.get("base_price_currency")
                or "USD"
            )
            .upper()
            .strip()
            or "USD"
        )
        if tariff is None:
            continue
        rows.append({"name": name, "tariff": float(tariff), "currency": ccy})

    rows.sort(key=lambda x: str(x.get("name") or "").lower())
    return rows


def _format_money(view, value: Optional[float], ccy: str = "") -> str:
    if value is None:
        return "-"
    try:
        if hasattr(view, "_fmt_money"):
            out = view._fmt_money(value, ccy)
            return str(out or "-")
    except Exception:
        pass
    amount = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return f"{amount} {ccy}".strip()


def _show_dialog(page, dialog: ft.AlertDialog):
    if hasattr(page, "show_dialog"):
        page.show_dialog(dialog)
    else:
        page.dialog = dialog
        dialog.open = True
        page.update()


def _close_dialog(page, dialog: ft.AlertDialog):
    try:
        if hasattr(page, "close"):
            page.close(dialog)
            return
    except Exception:
        pass
    try:
        if hasattr(page, "close_dialog"):
            page.close_dialog()
            return
    except Exception:
        pass
    dialog.open = False
    try:
        dialog.update()
    except Exception:
        pass


def _number_field(label: str, compact: bool = False) -> ft.TextField:
    field_height = 42 if compact else 36
    return ft.TextField(
        label=label,
        dense=False,
        height=field_height,
        keyboard_type=ft.KeyboardType.NUMBER,
        border_radius=10,
        content_padding=ft.padding.symmetric(horizontal=12, vertical=8),
        filled=True,
        fill_color="#FFFFFF",
        border_color="#94A3B8",
        focused_border_color="#2563EB",
    )


def _currency_dropdown(
    label: str,
    currencies: list[str],
    default_ccy: str,
    width: int,
    compact: bool = False,
):
    # Keep all dropdowns ~20% narrower in both modes to avoid clipping.
    target_width = max(90, int(round(width * 0.8)))
    return ft.Dropdown(
        label=label,
        width=target_width,
        value=default_ccy,
        options=[ft.dropdown.Option(c) for c in currencies],
        border_radius=10,
        filled=True,
        fill_color="#FFFFFF",
        border_color="#94A3B8",
        focused_border_color="#2563EB",
        dense=False,
    )


def _section_card(title: str, content: ft.Control, icon: str) -> ft.Container:
    return ft.Container(
        bgcolor=_PANEL,
        border_radius=14,
        border=ft.border.all(1, "#CBD5E1"),
        padding=ft.padding.symmetric(horizontal=14, vertical=12),
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Icon(icon, size=16, color=_CHIP_T),
                        ft.Text(
                            title,
                            size=13,
                            color=_TEXT,
                            weight=ft.FontWeight.W_600,
                        ),
                    ],
                    spacing=6,
                ),
                content,
            ],
            spacing=10,
            tight=True,
        ),
    )


def _delivery_chip(view, row: dict) -> ft.Container:
    return ft.Container(
        padding=ft.padding.symmetric(horizontal=10, vertical=6),
        border_radius=999,
        bgcolor=_HDR_BG,
        border=ft.border.all(1, "#BFDBFE"),
        content=ft.Text(
            f"{str(row.get('name') or '-')}: {_format_money(view, row.get('tariff'), row.get('currency') or 'USD')}",
            size=11,
            color=_CHIP_T,
        ),
    )


def _viewport_width(page) -> int:
    if not page:
        return 1200
    page_w = int(getattr(page, "width", 0) or 0)
    if page_w <= 0:
        win = getattr(page, "window", None)
        page_w = int(getattr(win, "width", 0) or 0) if win else 0
    return page_w if page_w > 0 else 1200


def _cards_layout(
    compact: bool, left_card: ft.Control, right_card: ft.Control
) -> ft.Control:
    if compact:
        return ft.Column([left_card, right_card], spacing=10, tight=True)
    return ft.ResponsiveRow(
        [
            ft.Container(col={"xs": 12, "md": 6}, content=left_card),
            ft.Container(col={"xs": 12, "md": 6}, content=right_card),
        ],
        spacing=10,
        run_spacing=10,
    )


def _field_row(
    left_field: ft.Control, right_field: ft.Control, compact: bool
) -> ft.Row:
    left_w = 250 if compact else 180
    return ft.Row(
        [
            ft.Container(width=left_w, content=left_field),
            right_field,
        ],
        spacing=8,
        wrap=True,
    )


def _single_field_row(field: ft.Control, compact: bool) -> ft.Row:
    row_w = 250 if compact else 180
    return ft.Row([ft.Container(width=row_w, content=field)], spacing=8, wrap=True)


def _dialog_shell(
    title: str,
    subtitle: str,
    body: ft.Control,
    *,
    compact: bool,
    viewport_width: int,
) -> ft.Container:
    vw = max(320, int(viewport_width or 0))
    dialog_width = (
        max(340, min(620, vw - 24)) if compact else max(580, min(680, vw - 36))
    )
    dialog_height = 620 if compact else 530
    return ft.Container(
        width=dialog_width,
        height=dialog_height,
        padding=ft.padding.all(12),
        border_radius=18,
        bgcolor=_ALT,
        content=ft.Column(
            [
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=4, vertical=2),
                    content=ft.Column(
                        [
                            ft.Text(
                                title,
                                size=22,
                                weight=ft.FontWeight.W_700,
                                color=_TEXT,
                            ),
                            ft.Text(
                                subtitle,
                                size=12,
                                color=_MUTED,
                            ),
                        ],
                        spacing=2,
                        tight=True,
                    ),
                ),
                body,
            ],
            spacing=10,
            tight=True,
            scroll=ft.ScrollMode.AUTO,
        ),
    )


def open_profitability_calculator(view):
    global _TEXT, _MUTED, _BORDER, _PANEL, _ALT, _CHIP_T, _SOFT_I, _HDR_BG, _ERR, _WARN, _WARN_BG
    _th = _th_from_view(view)
    _TEXT   = _th["text"];  _MUTED  = _th["muted"];   _BORDER = _th["border"]
    _PANEL  = _th["panel"]; _ALT    = _th["alt"];      _CHIP_T = _th["chip_text"]
    _SOFT_I  = _th["soft"]; _HDR_BG  = _th["soft"];    _ERR     = _th["err"]
    _WARN    = _th["warn"]; _WARN_BG = _th["warn_bg"]
    _t = getattr(view, "t", None) or i18n.bind(str(getattr(view, "manager_id", "") or ""))
    page = view._pg()
    if not page:
        return

    currencies = _currency_options_for_region(view)
    deliveries = _delivery_tariffs_for_region(view)
    default_ccy = currencies[0]
    cached = _get_cache(view, "profitability")
    viewport_w = _viewport_width(page)
    compact_layout = viewport_w < 1180

    cost_tf = _number_field(_t("calc.cp.field.cost", "Вартість товару"), compact_layout)
    cost_ccy = _currency_dropdown(
        "Валюта", currencies, default_ccy, 150, compact_layout
    )

    pack_tf = _number_field("Вартість упаковки", compact_layout)
    pack_ccy = _currency_dropdown(
        "Валюта", currencies, default_ccy, 150, compact_layout
    )

    weight_tf = _number_field("Вага товару (кг)", compact_layout)

    sale_tf = _number_field(_t("calc.cp.field.sale_price", "Ціна продажу"), compact_layout)
    sale_ccy = _currency_dropdown(
        "Валюта продажу", currencies, default_ccy, 165, compact_layout
    )

    custom_tariff_tf = _number_field("Свій тариф (за кг)", compact_layout)
    custom_tariff_ccy = _currency_dropdown(
        "Валюта тарифу", currencies, default_ccy, 165, compact_layout
    )

    results_host = ft.Column([], spacing=6, visible=False)

    cost_ccy.value = _pick_ccy(cached.get("cost_ccy"), currencies, default_ccy)
    pack_ccy.value = _pick_ccy(cached.get("pack_ccy"), currencies, default_ccy)
    sale_ccy.value = _pick_ccy(cached.get("sale_ccy"), currencies, default_ccy)
    custom_tariff_ccy.value = _pick_ccy(
        cached.get("custom_tariff_ccy"), currencies, default_ccy
    )
    _set_field_value(cost_tf, cached.get("cost"))
    _set_field_value(pack_tf, cached.get("pack"))
    _set_field_value(weight_tf, cached.get("weight"))
    _set_field_value(sale_tf, cached.get("sale_price"))
    _set_field_value(custom_tariff_tf, cached.get("custom_tariff"))

    def _render_profitability_results(
        result_rows: list[dict], target_sale_ccy: str
    ) -> None:
        table_rows = []
        for row in result_rows or []:
            margin_pct = _safe_float(row.get("margin_pct"), 0.0) or 0.0
            margin_color = "#166534" if margin_pct >= 0 else "#B91C1C"
            table_rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(
                            ft.Text(
                                str(row.get("name") or "-"),
                                size=12,
                                weight=ft.FontWeight.W_500,
                                color=_TEXT,
                            )
                        ),
                        ft.DataCell(
                            ft.Text(
                                _format_money(
                                    view,
                                    _safe_float(row.get("tariff_sale"), None),
                                    target_sale_ccy,
                                ),
                                size=12,
                                color=_TEXT,
                            )
                        ),
                        ft.DataCell(
                            ft.Text(
                                _format_money(
                                    view,
                                    _safe_float(row.get("cost_total"), None),
                                    target_sale_ccy,
                                ),
                                size=12,
                                color=_TEXT,
                            )
                        ),
                        ft.DataCell(
                            ft.Text(
                                f"{margin_pct:.2f}%",
                                size=12,
                                weight=ft.FontWeight.W_600,
                                color=margin_color,
                            )
                        ),
                    ]
                )
            )
        if not table_rows:
            results_host.visible = False
            results_host.controls = []
            try:
                results_host.update()
            except Exception:
                pass
            return
        results_host.controls = [
            _section_card(
                _t("calc.results_title", "Результати розрахунку"),
                ft.Column(
                    [
                        ft.Text(
                            f"Валюта результату: {target_sale_ccy}",
                            size=11,
                            color=_MUTED,
                        ),
                        ft.DataTable(
                            columns=[
                                ft.DataColumn(ft.Text(_t("calc.col.delivery_type", "Тип доставки"))),
                                ft.DataColumn(ft.Text(f"{_t('calc.col.tariff', 'Тариф')} ({target_sale_ccy})")),
                                ft.DataColumn(ft.Text(f"{_t('calc.col.unit_cost', 'С/В')} ({target_sale_ccy})")),
                                ft.DataColumn(ft.Text(_t("calc.col.margin", "Рентабельність"))),
                            ],
                            rows=table_rows,
                            column_spacing=22,
                            data_row_min_height=38,
                            data_row_max_height=42,
                            heading_row_color=_HDR_BG,
                            heading_text_style=ft.TextStyle(
                                size=12,
                                weight=ft.FontWeight.W_600,
                                color=_CHIP_T,
                            ),
                        ),
                    ],
                    spacing=8,
                    tight=True,
                ),
                ft.Icons.TABLE_ROWS,
            )
        ]
        results_host.visible = True
        try:
            results_host.update()
        except Exception:
            pass

    def _calc(_e=None):
        cost = _safe_float(cost_tf.value, None)
        pack = _safe_float(pack_tf.value, 0.0) or 0.0
        weight = _safe_float(weight_tf.value, None)
        sale_price = _safe_float(sale_tf.value, None)
        target_sale_ccy = (
            str(sale_ccy.value or default_ccy).upper().strip() or default_ccy
        )

        if cost is None or weight is None or sale_price is None or sale_price <= 0:
            view._snack(_t("calc.cp.err.fill_cost_weight_price", "Заповніть вартість, вагу та ціну продажу"), "#DC2626")
            return
        if weight < 0:
            view._snack("Вага не може бути від'ємною", "#DC2626")
            return

        cost_sale = _convert_currency(
            view, cost, cost_ccy.value or default_ccy, target_sale_ccy
        )
        pack_sale = _convert_currency(
            view, pack, pack_ccy.value or default_ccy, target_sale_ccy
        )
        if cost_sale is None or pack_sale is None:
            view._snack("Не вдалося конвертувати валюту", "#DC2626")
            return

        calc_rows = [dict(row) for row in deliveries]
        custom_tariff = _safe_float(custom_tariff_tf.value, None)
        if custom_tariff is not None:
            calc_rows.append(
                {
                    "name": "Свій тариф",
                    "tariff": float(custom_tariff),
                    "currency": str(custom_tariff_ccy.value or default_ccy)
                    .upper()
                    .strip()
                    or default_ccy,
                }
            )

        if not calc_rows:
            view._snack("Для регіону немає тарифів доставки", "#F59E0B")
            return

        result_rows = []
        for row in calc_rows:
            tariff_sale = _convert_currency(
                view, row["tariff"], row["currency"], target_sale_ccy
            )
            if tariff_sale is None:
                continue
            total_cost = (
                float(cost_sale) + float(pack_sale) + float(tariff_sale) * float(weight)
            )
            margin = (float(sale_price) - float(total_cost)) / float(sale_price)
            margin_pct = float(margin) * 100.0
            result_rows.append(
                {
                    "name": str(row["name"]),
                    "tariff_sale": float(tariff_sale),
                    "cost_total": float(total_cost),
                    "margin_pct": float(margin_pct),
                }
            )

        if not result_rows:
            view._snack("Не вдалося порахувати через валютні обмеження", "#DC2626")
            return

        _render_profitability_results(result_rows, target_sale_ccy)
        _set_cache(
            view,
            "profitability",
            {
                "cost": cost_tf.value,
                "cost_ccy": cost_ccy.value,
                "pack": pack_tf.value,
                "pack_ccy": pack_ccy.value,
                "weight": weight_tf.value,
                "sale_price": sale_tf.value,
                "sale_ccy": sale_ccy.value,
                "custom_tariff": custom_tariff_tf.value,
                "custom_tariff_ccy": custom_tariff_ccy.value,
                "target_sale_ccy": target_sale_ccy,
                "results": result_rows,
            },
        )

    inputs_card = _section_card(
        _t("calc.cp.input_params", "Вхідні параметри"),
        ft.Column(
            [
                _field_row(cost_tf, cost_ccy, compact_layout),
                _field_row(pack_tf, pack_ccy, compact_layout),
                _single_field_row(weight_tf, compact_layout),
                _field_row(sale_tf, sale_ccy, compact_layout),
            ],
            spacing=10,
            tight=True,
        ),
        ft.Icons.TUNE,
    )

    delivery_chips = (
        [_delivery_chip(view, row) for row in deliveries]
        if deliveries
        else [
            ft.Container(
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
                border_radius=10,
                bgcolor=_WARN_BG,
                border=ft.border.all(1, "#FCD34D"),
                content=ft.Text(
                    "Для регіону не знайдено тарифів доставки",
                    size=11,
                    color=_WARN,
                ),
            )
        ]
    )
    delivery_card = _section_card(
        _t("calc.cp.region_deliveries", "Доставки регіону"),
        ft.Column(
            [
                ft.Row(delivery_chips, spacing=8, wrap=True),
                ft.Row(
                    [
                        ft.Container(
                            width=(250 if compact_layout else 180),
                            content=custom_tariff_tf,
                        ),
                        custom_tariff_ccy,
                    ],
                    spacing=8,
                    wrap=True,
                ),
                ft.Text(
                    "Формула: (ціна продажу - (вартість + упаковка + тариф*вага)) / ціна продажу",
                    size=11,
                    color=_MUTED,
                ),
            ],
            spacing=10,
            tight=True,
        ),
        ft.Icons.LOCAL_SHIPPING,
    )

    actions_bar = ft.Row(
        [
            ft.FilledButton(
                _t("calc.cp.calculate_btn", "Рахувати"),
                icon=ft.Icons.CALCULATE,
                height=(42 if compact_layout else 36),
                style=ft.ButtonStyle(
                    shape=ft.RoundedRectangleBorder(radius=10),
                    padding=ft.padding.symmetric(horizontal=18, vertical=0),
                ),
                on_click=_calc,
            ),
        ],
        alignment=ft.MainAxisAlignment.START,
    )

    dialog = ft.AlertDialog(
        modal=True,
        content=_dialog_shell(
            _t("calc.cp.profitability_title", "Калькулятор рентабельності"),
            "Оціни прибутковість для кожного тарифу доставки в обраній валюті продажу",
            ft.Column(
                [
                    _cards_layout(compact_layout, inputs_card, delivery_card),
                    actions_bar,
                    results_host,
                ],
                spacing=10,
                tight=True,
            ),
            compact=compact_layout,
            viewport_width=viewport_w,
        ),
        actions=[
            ft.TextButton(_t("packaging.dlg.close", "Закрити"), on_click=lambda _e: _close_dialog(page, dialog)),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )
    if isinstance(cached.get("results"), list) and cached.get("target_sale_ccy"):
        _render_profitability_results(
            cached.get("results") or [],
            _pick_ccy(cached.get("target_sale_ccy"), currencies, default_ccy),
        )
    _show_dialog(page, dialog)


def open_price_calculator(view):
    global _TEXT, _MUTED, _BORDER, _PANEL, _ALT, _CHIP_T, _SOFT_I, _HDR_BG, _ERR, _WARN, _WARN_BG
    _th = _th_from_view(view)
    _TEXT   = _th["text"];  _MUTED  = _th["muted"];   _BORDER = _th["border"]
    _PANEL  = _th["panel"]; _ALT    = _th["alt"];      _CHIP_T = _th["chip_text"]
    _SOFT_I  = _th["soft"]; _HDR_BG  = _th["soft"];    _ERR     = _th["err"]
    _WARN    = _th["warn"]; _WARN_BG = _th["warn_bg"]
    _t = getattr(view, "t", None) or i18n.bind(str(getattr(view, "manager_id", "") or ""))
    page = view._pg()
    if not page:
        return

    currencies = _currency_options_for_region(view)
    deliveries = _delivery_tariffs_for_region(view)
    default_ccy = currencies[0]
    cached = _get_cache(view, "price")
    viewport_w = _viewport_width(page)
    compact_layout = viewport_w < 1180

    cost_tf = _number_field(_t("calc.cp.field.cost", "Вартість товару"), compact_layout)
    cost_ccy = _currency_dropdown(
        "Валюта", currencies, default_ccy, 150, compact_layout
    )

    pack_tf = _number_field("Вартість упаковки", compact_layout)
    pack_ccy = _currency_dropdown(
        "Валюта", currencies, default_ccy, 150, compact_layout
    )

    weight_tf = _number_field("Вага товару (кг)", compact_layout)
    target_margin_tf = _number_field("Цільова рентабельність (%)", compact_layout)
    sale_ccy = _currency_dropdown(
        "Валюта ціни продажу", currencies, default_ccy, 165, compact_layout
    )

    custom_tariff_tf = _number_field("Свій тариф (за кг)", compact_layout)
    custom_tariff_ccy = _currency_dropdown(
        "Валюта тарифу", currencies, default_ccy, 165, compact_layout
    )

    results_host = ft.Column([], spacing=6, visible=False)

    cost_ccy.value = _pick_ccy(cached.get("cost_ccy"), currencies, default_ccy)
    pack_ccy.value = _pick_ccy(cached.get("pack_ccy"), currencies, default_ccy)
    sale_ccy.value = _pick_ccy(cached.get("sale_ccy"), currencies, default_ccy)
    custom_tariff_ccy.value = _pick_ccy(
        cached.get("custom_tariff_ccy"), currencies, default_ccy
    )
    _set_field_value(cost_tf, cached.get("cost"))
    _set_field_value(pack_tf, cached.get("pack"))
    _set_field_value(weight_tf, cached.get("weight"))
    _set_field_value(target_margin_tf, cached.get("target_margin"))
    _set_field_value(custom_tariff_tf, cached.get("custom_tariff"))

    def _render_price_results(result_rows: list[dict], target_sale_ccy: str) -> None:
        table_rows = []
        for row in result_rows or []:
            table_rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(
                            ft.Text(
                                str(row.get("name") or "-"),
                                size=12,
                                weight=ft.FontWeight.W_500,
                                color=_TEXT,
                            )
                        ),
                        ft.DataCell(
                            ft.Text(
                                _format_money(
                                    view,
                                    _safe_float(row.get("tariff_sale"), None),
                                    target_sale_ccy,
                                ),
                                size=12,
                                color=_TEXT,
                            )
                        ),
                        ft.DataCell(
                            ft.Text(
                                _format_money(
                                    view,
                                    _safe_float(row.get("cost_total"), None),
                                    target_sale_ccy,
                                ),
                                size=12,
                                color=_TEXT,
                            )
                        ),
                        ft.DataCell(
                            ft.Text(
                                _format_money(
                                    view,
                                    _safe_float(row.get("sale_price"), None),
                                    target_sale_ccy,
                                ),
                                size=12,
                                color=_TEXT,
                                weight=ft.FontWeight.W_600,
                            )
                        ),
                    ]
                )
            )
        if not table_rows:
            results_host.visible = False
            results_host.controls = []
            try:
                results_host.update()
            except Exception:
                pass
            return
        results_host.controls = [
            _section_card(
                _t("calc.results_title", "Результати розрахунку"),
                ft.Column(
                    [
                        ft.Text(
                            f"Валюта результату: {target_sale_ccy}",
                            size=11,
                            color=_MUTED,
                        ),
                        ft.DataTable(
                            columns=[
                                ft.DataColumn(ft.Text(_t("calc.col.delivery_type", "Тип доставки"))),
                                ft.DataColumn(ft.Text(f"{_t('calc.col.tariff', 'Тариф')} ({target_sale_ccy})")),
                                ft.DataColumn(ft.Text(f"{_t('calc.col.unit_cost', 'С/В')} ({target_sale_ccy})")),
                                ft.DataColumn(
                                    ft.Text(f"{_t('calc.col.sale_price', 'Ціна продажу')} ({target_sale_ccy})")
                                ),
                            ],
                            rows=table_rows,
                            column_spacing=22,
                            data_row_min_height=38,
                            data_row_max_height=42,
                            heading_row_color=_HDR_BG,
                            heading_text_style=ft.TextStyle(
                                size=12,
                                weight=ft.FontWeight.W_600,
                                color=_CHIP_T,
                            ),
                        ),
                    ],
                    spacing=8,
                    tight=True,
                ),
                ft.Icons.TABLE_ROWS,
            )
        ]
        results_host.visible = True
        try:
            results_host.update()
        except Exception:
            pass

    def _calc(_e=None):
        cost = _safe_float(cost_tf.value, None)
        pack = _safe_float(pack_tf.value, 0.0) or 0.0
        weight = _safe_float(weight_tf.value, None)
        target_margin = _safe_float(target_margin_tf.value, None)
        target_sale_ccy = (
            str(sale_ccy.value or default_ccy).upper().strip() or default_ccy
        )

        if cost is None or weight is None or target_margin is None:
            view._snack(_t("calc.cp.err.fill_cost_weight_margin", "Заповніть вартість, вагу та цільову рентабельність"), "#DC2626")
            return
        if weight < 0:
            view._snack("Вага не може бути від'ємною", "#DC2626")
            return

        denominator = 1.0 - (float(target_margin) / 100.0)
        if denominator <= 0:
            view._snack("Цільова рентабельність має бути меншою за 100%", "#DC2626")
            return

        cost_sale = _convert_currency(
            view, cost, cost_ccy.value or default_ccy, target_sale_ccy
        )
        pack_sale = _convert_currency(
            view, pack, pack_ccy.value or default_ccy, target_sale_ccy
        )
        if cost_sale is None or pack_sale is None:
            view._snack("Не вдалося конвертувати валюту", "#DC2626")
            return

        calc_rows = [dict(row) for row in deliveries]
        custom_tariff = _safe_float(custom_tariff_tf.value, None)
        if custom_tariff is not None:
            calc_rows.append(
                {
                    "name": "Свій тариф",
                    "tariff": float(custom_tariff),
                    "currency": str(custom_tariff_ccy.value or default_ccy)
                    .upper()
                    .strip()
                    or default_ccy,
                }
            )

        if not calc_rows:
            view._snack("Для регіону немає тарифів доставки", "#F59E0B")
            return

        result_rows = []
        for row in calc_rows:
            tariff_sale = _convert_currency(
                view, row["tariff"], row["currency"], target_sale_ccy
            )
            if tariff_sale is None:
                continue
            sale_price = (
                float(cost_sale)
                + float(pack_sale)
                + (float(weight) * float(tariff_sale))
            ) / float(denominator)
            total_cost = (
                float(cost_sale)
                + float(pack_sale)
                + (float(weight) * float(tariff_sale))
            )
            result_rows.append(
                {
                    "name": str(row["name"]),
                    "tariff_sale": float(tariff_sale),
                    "cost_total": float(total_cost),
                    "sale_price": float(sale_price),
                }
            )

        if not result_rows:
            view._snack("Не вдалося порахувати через валютні обмеження", "#DC2626")
            return

        _render_price_results(result_rows, target_sale_ccy)
        _set_cache(
            view,
            "price",
            {
                "cost": cost_tf.value,
                "cost_ccy": cost_ccy.value,
                "pack": pack_tf.value,
                "pack_ccy": pack_ccy.value,
                "weight": weight_tf.value,
                "target_margin": target_margin_tf.value,
                "sale_ccy": sale_ccy.value,
                "custom_tariff": custom_tariff_tf.value,
                "custom_tariff_ccy": custom_tariff_ccy.value,
                "target_sale_ccy": target_sale_ccy,
                "results": result_rows,
            },
        )

    inputs_card = _section_card(
        _t("calc.cp.input_params", "Вхідні параметри"),
        ft.Column(
            [
                _field_row(cost_tf, cost_ccy, compact_layout),
                _field_row(pack_tf, pack_ccy, compact_layout),
                _single_field_row(weight_tf, compact_layout),
                _field_row(target_margin_tf, sale_ccy, compact_layout),
            ],
            spacing=10,
            tight=True,
        ),
        ft.Icons.TUNE,
    )

    delivery_chips = (
        [_delivery_chip(view, row) for row in deliveries]
        if deliveries
        else [
            ft.Container(
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
                border_radius=10,
                bgcolor=_WARN_BG,
                border=ft.border.all(1, "#FCD34D"),
                content=ft.Text(
                    "Для регіону не знайдено тарифів доставки",
                    size=11,
                    color=_WARN,
                ),
            )
        ]
    )
    delivery_card = _section_card(
        _t("calc.cp.region_deliveries", "Доставки регіону"),
        ft.Column(
            [
                ft.Row(delivery_chips, spacing=8, wrap=True),
                ft.Row(
                    [
                        ft.Container(
                            width=(250 if compact_layout else 180),
                            content=custom_tariff_tf,
                        ),
                        custom_tariff_ccy,
                    ],
                    spacing=8,
                    wrap=True,
                ),
                ft.Text(
                    "Формула: (вартість + упаковка + вага*тариф) / (1 - рентабельність/100)",
                    size=11,
                    color=_MUTED,
                ),
            ],
            spacing=10,
            tight=True,
        ),
        ft.Icons.LOCAL_SHIPPING,
    )

    actions_bar = ft.Row(
        [
            ft.FilledButton(
                _t("calc.cp.calculate_btn", "Рахувати"),
                icon=ft.Icons.CALCULATE,
                height=(42 if compact_layout else 36),
                style=ft.ButtonStyle(
                    shape=ft.RoundedRectangleBorder(radius=10),
                    padding=ft.padding.symmetric(horizontal=18, vertical=0),
                ),
                on_click=_calc,
            ),
        ],
        alignment=ft.MainAxisAlignment.START,
    )

    dialog = ft.AlertDialog(
        modal=True,
        content=_dialog_shell(
            _t("calc.cp.price_title", "Калькулятор ціни"),
            "Розрахунок рекомендованої ціни продажу для цільової рентабельності",
            ft.Column(
                [
                    _cards_layout(compact_layout, inputs_card, delivery_card),
                    actions_bar,
                    results_host,
                ],
                spacing=10,
                tight=True,
            ),
            compact=compact_layout,
            viewport_width=viewport_w,
        ),
        actions=[
            ft.TextButton(_t("packaging.dlg.close", "Закрити"), on_click=lambda _e: _close_dialog(page, dialog)),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )
    if isinstance(cached.get("results"), list) and cached.get("target_sale_ccy"):
        _render_price_results(
            cached.get("results") or [],
            _pick_ccy(cached.get("target_sale_ccy"), currencies, default_ccy),
        )
    _show_dialog(page, dialog)


def build_calculator_buttons(view) -> ft.Container:
    _t = getattr(view, "t", None) or i18n.bind(str(getattr(view, "manager_id", "") or ""))
    def _safe_action(tag: str, action, err: str):
        try:
            return view._guard_ui_action(tag, action, err)
        except Exception:
            return lambda _e=None: action()

    return ft.Container(
        bgcolor=_ALT,
        border_radius=6,
        padding=ft.padding.symmetric(horizontal=6, vertical=6),
        content=ft.Row(
            [
                ft.FilledButton(
                    _t("calc.cp.profitability_btn", "Калькулятор Рентабельності"),
                    icon=ft.Icons.CALCULATE,
                    on_click=_safe_action(
                        "open_profitability_calculator",
                        lambda: open_profitability_calculator(view),
                        _t("calc.cp.err.open", "Не вдалося відкрити калькулятор"),
                    ),
                ),
                ft.OutlinedButton(
                    _t("calc.cp.price_btn", "Калькулятор Ціни"),
                    icon=ft.Icons.PRICE_CHANGE,
                    on_click=_safe_action(
                        "open_price_calculator",
                        lambda: open_price_calculator(view),
                        _t("calc.cp.err.open", "Не вдалося відкрити калькулятор"),
                    ),
                ),
            ],
            spacing=8,
            wrap=True,
        ),
    )
