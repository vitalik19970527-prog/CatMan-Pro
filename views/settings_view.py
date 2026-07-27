import asyncio
import contextlib
import os
import uuid

import flet as ft
import i18n

from logger import logger
from views.number_format import format_grouped, safe_float
from views.themes import (
    THEMES, THEME_GROUPS, DEFAULT_THEME, build_theme_card,
    NAV_ICON_PACKS, NAV_ICON_PACK_GROUPS, DEFAULT_NAV_PACK, build_nav_pack_card,
)


def _parse_float_or_none(value):
    return safe_float(value, None)


def _format_price(value):
    parsed = _parse_float_or_none(value)
    if parsed is None:
        return ""
    return format_grouped(parsed)


def _normalize_role(role):
    return (role or "").strip().lower().replace(" ", "")


def _can_manage_company_settings(role):
    return _normalize_role(role) in {
        "суперадмін",
        "суперадмин",
        "superadmin",
        "адмін",
        "админ",
        "admin",
    }


# Persists which collapsible blocks the user has expanded across rebuilds of
# this view (SettingsView is re-instantiated on every tab switch/sync-pulse
# refresh, so per-instance state would reset to `initially_open` every time —
# see build_collapsible_block below). Keyed by (company_id, title); resets on
# app restart, which is fine since the goal is just "stay open until the user
# collapses it themselves" within a session.
_collapsible_open_state: dict = {}


class SettingsView:
    def __init__(self, content_area, user_data, db):
        self.content_area = content_area
        self.user_data = user_data
        self.db = db
        self.t = i18n.bind(str(user_data.get("id") or user_data.get("active_manager_id") or ""))
        self.company_id = user_data.get("company_id")
        self.active_region_id = user_data.get("active_region_id") or user_data.get(
            "region_id"
        )
        self.user_role = user_data.get("role")
        self.can_manage_company_settings = _can_manage_company_settings(self.user_role)
        self.read_only_subordinate = bool(user_data.get("subordinate_view_readonly"))
        # Live read (not the value cached in user_data at login) so the
        # Локал->VPS upgrade button disappears immediately after migrating,
        # without requiring the user to log out/in again.
        self.storage_mode = (
            self.db.get_company_storage_mode(self.company_id) if self.company_id else "vps"
        )
        # Де реально живуть дані компанії зараз (host/port/db_name, без
        # пароля) — з урахуванням особистого Компанія-Локал підключення
        # цього конкретного співробітника, якщо воно є.
        _own_manager_id = str(
            user_data.get("id") or user_data.get("active_manager_id") or ""
        ).strip()
        self.db_location = (
            self.db.get_company_db_summary(self.company_id, _own_manager_id)
            if self.company_id
            else {}
        )
        # Цінові налаштування: власні ціни/сегментація — доступні всім, окрім режиму віддаленого перегляду.
        self.can_edit_pricing_settings = not self.read_only_subordinate
        # Обмежений список з 5 основних валют
        self.currencies_list = ["USD", "UAH", "EUR", "PLN", "CNY"]
        # Called after a successful save (handle_update) — wired by dashboard.py
        # to invalidate OTHER cached tabs (Товари/Замовлення/С/В/...) whose data
        # (delivery methods, payment fields, price tiers) this screen edits, now
        # that this tab itself is cached and no longer rebuilds on every visit.
        self._on_settings_saved = None

    def _dialog_content_height(self, desired=560, min_height=260):
        try:
            page = self.content_area.page if self.content_area else None
            page_h = getattr(page, "height", None) if page else None
            if not page_h and page:
                win = getattr(page, "window", None)
                page_h = getattr(win, "height", None) if win else None
            if isinstance(page_h, (int, float)) and page_h > 0:
                return max(min_height, min(desired, int(page_h * 0.72)))
        except Exception:
            pass
        return desired

    def _dialog_width(self, desired=560, min_width=320):
        try:
            page = self.content_area.page if self.content_area else None
            page_w = getattr(page, "width", None) if page else None
            if not page_w and page:
                win = getattr(page, "window", None)
                page_w = getattr(win, "width", None) if win else None
            if isinstance(page_w, (int, float)) and page_w > 0:
                return max(min_width, min(desired, int(page_w * 0.9)))
        except Exception:
            pass
        return desired

    def _pick_local_image_file(self):
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askopenfilename(
                title="Оберіть зображення",
                filetypes=[("Image files", "*.png *.jpg *.jpeg *.webp")],
            )
            root.destroy()
            return path or ""
        except Exception:
            return ""

    def _build_deferred(self) -> None:
        """Re-run build() on the next event-loop tick instead of immediately.

        Region create/rename/delete used to call self.build() synchronously
        right after closing an AlertDialog — clearing and rebuilding this
        whole (large, deeply nested, many wrap=True Rows) tree while Flutter
        was still mid-animation on the dialog's close crashed its layout
        ("WrapParentData is not a subtype of FlexParentData"), which visually
        looked like the tab bouncing back to Головна. Deferring by one tick
        (same pattern as dashboard.py's resize-triggered rerenders) lets the
        dialog-close settle first.
        """
        page = self.content_area.page if self.content_area else None
        if not page or not hasattr(page, "run_task"):
            self.build()
            return

        async def _do():
            import asyncio

            await asyncio.sleep(0.05)
            self.build()

        with contextlib.suppress(Exception):
            page.run_task(_do)

    def build(self):
        try:
            self._build_inner()
        except Exception:
            logger.exception("[settings] build() failed, showing error state")
            self.content_area.controls.clear()
            self.content_area.controls.append(
                ft.Column(
                    [
                        ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=30, color="#EF4444"),
                        ft.Text(
                            self.t("settings.err.build", "Помилка завантаження налаштувань"),
                            size=14,
                            weight="bold",
                            color="#EF4444",
                        ),
                        ft.ElevatedButton(
                            self.t("settings.retry", "Повторити"),
                            on_click=lambda _e: self.build(),
                        ),
                    ],
                    spacing=8,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                )
            )
            with contextlib.suppress(Exception):
                self.content_area.update()

    def _build_inner(self):
        self.content_area.controls.clear()

        from views.themes import get_active_theme as _get_th
        _mid = str(
            self.user_data.get("id") or self.user_data.get("active_manager_id") or ""
        ).strip()
        _th = _get_th(_mid)
        _th_text   = _th["text"]
        _th_muted  = _th["muted"]
        _th_icon   = _th["icon_color"]
        _th_panel  = _th["panel"]
        _th_border = _th["border"]
        _th_ok     = _th["ok"]
        _th_err    = _th["err"]

        def build_collapsible_block(
            title, controls, initially_open=False, visible=True, key_prefix=""
        ):
            # key_prefix disambiguates the persisted open/closed state when the
            # same visible title (e.g. a bare region name) is reused for
            # unrelated nested blocks under different parent sections.
            state_key = (self.company_id, key_prefix, title)
            is_open = _collapsible_open_state.get(state_key, initially_open)
            body = ft.Column(controls, spacing=8, visible=is_open)
            toggle_btn = ft.TextButton(
                title,
                icon=(
                    ft.Icons.KEYBOARD_ARROW_DOWN
                    if is_open
                    else ft.Icons.KEYBOARD_ARROW_RIGHT
                ),
                style=ft.ButtonStyle(
                    color=_th_icon,
                    padding=ft.padding.symmetric(horizontal=0, vertical=0),
                ),
            )

            def toggle(_):
                body.visible = not body.visible
                _collapsible_open_state[state_key] = body.visible
                toggle_btn.icon = (
                    ft.Icons.KEYBOARD_ARROW_DOWN
                    if body.visible
                    else ft.Icons.KEYBOARD_ARROW_RIGHT
                )
                self.content_area.update()

            toggle_btn.on_click = toggle
            return ft.Container(
                visible=visible,
                padding=14,
                bgcolor=_th_panel,
                border=ft.border.all(1, _th_border),
                border_radius=8,
                content=ft.Column(
                    [toggle_btn, body],
                    spacing=6,
                ),
            )

        settings_layout = {
            "delivery_name_w": 320,
            "delivery_price_w": 180,
            "delivery_ccy_w": 120,
            "delivery_term_w": 170,
            "delivery_row_h": 42,
        }
        settings_visible = {
            "delivery_name": True,
            "delivery_price": True,
            "delivery_ccy": True,
            "delivery_term": True,
        }

        def _settings_layout_state_key():
            region = str(self.active_region_id or "all").strip() or "all"
            company = str(self.company_id or "no_company").strip() or "no_company"
            return f"{company}:{region}"

        def _workspace_user_id():
            return str(
                self.user_data.get("id")
                or self.user_data.get("active_manager_id")
                or self.user_data.get("manager_id")
                or ""
            ).strip()

        def _load_settings_layout_state():
            manager_id = _workspace_user_id()
            if not manager_id:
                return
            try:
                payload = self.db.get_manager_workspace_state(manager_id) or {}
                if not isinstance(payload, dict):
                    return
                store = (
                    payload.get("settings_table_layout")
                    if isinstance(payload.get("settings_table_layout"), dict)
                    else {}
                )
                row = (
                    store.get(_settings_layout_state_key())
                    if isinstance(store, dict)
                    else None
                )
                if not isinstance(row, dict):
                    return
                for key in list(settings_layout.keys()):
                    try:
                        settings_layout[key] = int(
                            row.get(key) or settings_layout.get(key) or 0
                        )
                    except Exception:
                        pass
            except Exception:
                pass

        def _save_settings_layout_state():
            manager_id = _workspace_user_id()
            if not manager_id:
                return
            try:
                payload = self.db.get_manager_workspace_state(manager_id) or {}
                if not isinstance(payload, dict):
                    payload = {}
                store = (
                    payload.get("settings_table_layout")
                    if isinstance(payload.get("settings_table_layout"), dict)
                    else {}
                )
                store[_settings_layout_state_key()] = dict(settings_layout)
                payload["settings_table_layout"] = store
                self.db.save_manager_workspace_state(manager_id, payload)
            except Exception:
                pass

        def _load_settings_visible_state():
            manager_id = _workspace_user_id()
            if not manager_id:
                return
            try:
                payload = self.db.get_manager_workspace_state(manager_id) or {}
                store = (
                    payload.get("settings_visible_columns")
                    if isinstance(payload, dict)
                    else {}
                )
                row = (
                    store.get(_settings_layout_state_key())
                    if isinstance(store, dict)
                    else None
                )
                if isinstance(row, dict):
                    for key in list(settings_visible.keys()):
                        if key in row:
                            settings_visible[key] = bool(row.get(key))
            except Exception:
                pass

        def _load_manager_production_term_state():
            manager_id = _workspace_user_id()
            if not manager_id:
                return {}
            try:
                payload = self.db.get_manager_workspace_state(manager_id) or {}
                store = (
                    payload.get("manager_production_term")
                    if isinstance(payload.get("manager_production_term"), dict)
                    else {}
                )
                row = (
                    store.get(_settings_layout_state_key())
                    if isinstance(store, dict)
                    else None
                )
                if isinstance(row, dict):
                    return row
            except Exception:
                pass
            return {}

        def _save_manager_production_term_state(days_val):
            manager_id = _workspace_user_id()
            if not manager_id:
                return
            try:
                payload = self.db.get_manager_workspace_state(manager_id) or {}
                if not isinstance(payload, dict):
                    payload = {}
                store = (
                    payload.get("manager_production_term")
                    if isinstance(payload.get("manager_production_term"), dict)
                    else {}
                )
                store[_settings_layout_state_key()] = {
                    "days": (
                        int(days_val)
                        if days_val is not None and str(days_val).strip() != ""
                        else None
                    ),
                }
                payload["manager_production_term"] = store
                self.db.save_manager_workspace_state(manager_id, payload)
            except Exception:
                pass

        def _save_settings_visible_state():
            manager_id = _workspace_user_id()
            if not manager_id:
                return
            try:
                payload = self.db.get_manager_workspace_state(manager_id) or {}
                if not isinstance(payload, dict):
                    payload = {}
                store = (
                    payload.get("settings_visible_columns")
                    if isinstance(payload.get("settings_visible_columns"), dict)
                    else {}
                )
                store[_settings_layout_state_key()] = dict(settings_visible)
                payload["settings_visible_columns"] = store
                self.db.save_manager_workspace_state(manager_id, payload)
            except Exception:
                pass

        def _open_settings_columns_dialog(_=None):
            page = self.content_area.page if self.content_area else None
            if not page:
                return
            c_name = ft.Checkbox(
                label="Назва способу", value=bool(settings_visible["delivery_name"])
            )
            c_price = ft.Checkbox(
                label="Базова ціна", value=bool(settings_visible["delivery_price"])
            )
            c_ccy = ft.Checkbox(
                label="Валюта", value=bool(settings_visible["delivery_ccy"])
            )
            c_term = ft.Checkbox(
                label="Термін доставки", value=bool(settings_visible["delivery_term"])
            )
            dlg = ft.AlertDialog(modal=True)

            def _close(_e=None):
                page.pop_dialog()

            def _save(_e=None):
                try:
                    settings_visible["delivery_name"] = bool(c_name.value)
                    settings_visible["delivery_price"] = bool(c_price.value)
                    settings_visible["delivery_ccy"] = bool(c_ccy.value)
                    settings_visible["delivery_term"] = bool(c_term.value)
                    if not any(settings_visible.values()):
                        settings_visible["delivery_name"] = True
                    _save_settings_visible_state()
                    page.pop_dialog()
                    self._build_deferred()
                except Exception:
                    logger.exception("[settings] _open_settings_columns_dialog save failed")
                    self._show_snack(self.t("settings.err.save_failed", "Не вдалося зберегти"), "red")

            def _cols_help(_):
                from views.help_utils import show_modal_help
                show_modal_help(page, "Показувати стовпці", [
                    self.t("settings.cols_help.step1", "1. Вмикайте/вимикайте стовпці для відображення у таблиці доставок."),
                    self.t("settings.cols_help.step2", "2. Натисніть «Зберегти»."),
                ])

            dlg.title = ft.Row(
                [
                    ft.Text("Показувати стовпці", expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_cols_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            )
            dlg.content = ft.Container(
                width=320,
                content=ft.Column([c_name, c_price, c_ccy, c_term], spacing=6),
            )
            dlg.actions = [
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_close),
                ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save),
            ]
            dlg.actions_alignment = ft.MainAxisAlignment.END
            page.show_dialog(dlg)

        def _open_settings_layout_dialog(_=None):
            page = self.content_area.page if self.content_area else None
            if not page:
                return
            name_w_f = ft.TextField(
                label="Назва способу",
                value=str(int(settings_layout["delivery_name_w"])),
                width=180,
                border_radius=8,
            )
            price_w_f = ft.TextField(
                label="Базова ціна",
                value=str(int(settings_layout["delivery_price_w"])),
                width=180,
                border_radius=8,
            )
            ccy_w_f = ft.TextField(
                label="Валюта",
                value=str(int(settings_layout["delivery_ccy_w"])),
                width=180,
                border_radius=8,
            )
            term_w_f = ft.TextField(
                label="Термін доставки",
                value=str(int(settings_layout["delivery_term_w"])),
                width=180,
                border_radius=8,
            )
            row_h_f = ft.TextField(
                label="Висота",
                value=str(int(settings_layout["delivery_row_h"])),
                width=180,
                border_radius=8,
            )

            dlg = ft.AlertDialog(modal=True)

            def _to_int(txt, default):
                try:
                    s = str(txt or "").strip().replace(",", ".")
                    if not s:
                        return int(default)
                    return int(float(s))
                except Exception:
                    return int(default)

            def _close(_e=None):
                page.pop_dialog()

            def _save(_e=None):
                try:
                    settings_layout["delivery_name_w"] = min(
                        720,
                        _to_int(name_w_f.value, settings_layout["delivery_name_w"]),
                    )
                    settings_layout["delivery_price_w"] = min(
                        360,
                        _to_int(price_w_f.value, settings_layout["delivery_price_w"]),
                    )
                    settings_layout["delivery_ccy_w"] = min(
                        220,
                        _to_int(ccy_w_f.value, settings_layout["delivery_ccy_w"]),
                    )
                    settings_layout["delivery_term_w"] = min(
                        360,
                        _to_int(term_w_f.value, settings_layout["delivery_term_w"]),
                    )
                    settings_layout["delivery_row_h"] = min(
                        88,
                        _to_int(row_h_f.value, settings_layout["delivery_row_h"]),
                    )
                    _save_settings_layout_state()
                    page.pop_dialog()
                    self._build_deferred()
                except Exception:
                    logger.exception("[settings] _open_settings_layout_dialog save failed")
                    self._show_snack(self.t("settings.err.save_failed", "Не вдалося зберегти"), "red")

            def _sizes_help(_):
                from views.help_utils import show_modal_help
                show_modal_help(page, "Стовпчик / Ширина / Висота", [
                    self.t("settings.sizes_help.step1", "1. Введіть ширину (px) для кожного стовпчика таблиці доставок."),
                    self.t("settings.sizes_help.step2", "2. Вкажіть висоту рядка."),
                    self.t("settings.sizes_help.step3", "3. Натисніть «Зберегти»."),
                ])

            dlg.title = ft.Row(
                [
                    ft.Text("Стовпчик / Ширина / Висота", expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_sizes_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            )
            dlg.content = ft.Container(
                width=420,
                content=ft.Column(
                    [
                        ft.Row([name_w_f, price_w_f], wrap=True, spacing=8),
                        ft.Row([ccy_w_f, term_w_f], wrap=True, spacing=8),
                        ft.Divider(height=8),
                        row_h_f,
                    ],
                    spacing=8,
                ),
            )
            dlg.actions = [
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_close),
                ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save),
            ]
            dlg.actions_alignment = ft.MainAxisAlignment.END
            page.show_dialog(dlg)

        _load_settings_layout_state()
        _load_settings_visible_state()

        # --- WORK ORDERS MODE PREFERENCE ---
        _work_orders_mode_current = bool(self.user_data.get("work_orders_mode", True))
        work_orders_cb = ft.Checkbox(
            label="Робота з замовленнями",
            value=_work_orders_mode_current,
        )

        def _on_work_orders_mode_toggle(_e=None):
            new_val = bool(work_orders_cb.value)
            mid = _workspace_user_id()
            if mid:
                try:
                    _payload = self.db.get_manager_workspace_state(mid) or {}
                    if not isinstance(_payload, dict):
                        _payload = {}
                    _payload["work_orders_mode"] = new_val
                    self.db.save_manager_workspace_state(mid, _payload)
                except Exception:
                    pass
            _cb = getattr(self, "_on_work_orders_mode_changed", None)
            if callable(_cb):
                _cb(new_val)

        work_orders_cb.on_change = _on_work_orders_mode_toggle

        # --- ПОЛЯ ВВОДУ ОСОБИСТИХ ДАНИХ ---
        manager_name = ft.TextField(
            label=self.t("auth.common.name_label", "Ваше Ім'я"), value=self.user_data.get("full_name"), border_radius=10
        )
        manager_email = ft.TextField(
            label=self.t("auth.common.email_label", "E-mail"), value=self.user_data.get("email"), border_radius=10
        )
        manager_phone = ft.TextField(
            label=self.t("auth.common.phone_label", "Номер телефону +380"), value=self.user_data.get("phone"), border_radius=10
        )
        old_password = ft.TextField(
            label=self.t("settings.field.current_password", "Поточний пароль"),
            password=True,
            can_reveal_password=True,
            border_radius=10,
        )
        new_password = ft.TextField(
            label=self.t("settings.field.new_password", "Новий пароль"),
            password=True,
            can_reveal_password=True,
            border_radius=10,
        )

        regions_list_container = ft.Column(spacing=15)

        # --- МЕТОДИ ДОСТАВКИ (КОМПАНІЯ + ПЕРСОНАЛЬНІ ОВЕРРАЙДИ) ---
        # `active_region_id` from Dashboard is a *region name* (UI buttons), not DB region_id.
        # Convert to real `region_id` so Postgres queries return data.
        region_id_for_delivery = self.active_region_id
        if isinstance(region_id_for_delivery, str):
            if region_id_for_delivery.strip().lower() in {
                "__all_regions__",
                "all",
                "всі",
                "всі_регіони",
            }:
                region_id_for_delivery = None
            else:
                try:
                    regions_map = self.db.get_company_regions(self.company_id) or []
                    name_to_id = {
                        str(r.get("name") or "")
                        .strip()
                        .lower(): str(r.get("id") or "")
                        .strip()
                        for r in regions_map
                        if isinstance(r, dict) and r.get("name") and r.get("id")
                    }
                    normalized = str(region_id_for_delivery).strip().lower()
                    if normalized in name_to_id and name_to_id[normalized]:
                        region_id_for_delivery = name_to_id[normalized]
                except Exception:
                    pass
        company_delivery_methods = self.db.get_company_delivery_methods(
            self.company_id, region_id_for_delivery
        )
        manager_delivery_overrides = self.db.get_manager_delivery_overrides(
            self.user_data.get("id"),
            region_id=region_id_for_delivery,
        )
        # --- Payment fields (company templates + manager overrides) ---
        company_payment_templates = (
            self.db.get_company_payment_field_templates(
                self.company_id, region_id=region_id_for_delivery
            )
            if self.company_id
            else []
        )
        manager_payment_overrides = (
            self.db.get_manager_payment_field_overrides(
                self.user_data.get("id"),
                self.company_id,
                region_id=region_id_for_delivery,
            )
            if self.company_id
            else {}
        )
        payment_override_rows = []
        manager_delivery_rows = []
        # Company-wide delivery methods are configured separately PER REGION —
        # each region gets its own row list/container, all shown at once (see
        # delivery_region_blocks below), instead of one shared list scoped to
        # whichever region the editing admin currently has active. Keyed by
        # region_id.
        company_delivery_rows_by_region: dict = {}
        company_delivery_containers_by_region: dict = {}

        def build_company_delivery_row(row):
            controls = []
            if settings_visible.get("delivery_name", True):
                controls.append(row["name_f"])
            controls.append(row["enabled_cb"])
            if settings_visible.get("delivery_price", True):
                controls.append(row["base_price_f"])
            if settings_visible.get("delivery_ccy", True):
                controls.append(row["currency_f"])
            if settings_visible.get("delivery_term", True):
                controls.append(row["delivery_term_f"])
            if row.get("remove_btn") is not None:
                controls.append(row["remove_btn"])
            return ft.Row(controls, spacing=10, wrap=True)

        def refresh_company_delivery_rows_for(rid):
            rows = company_delivery_rows_by_region.get(rid) or []
            container = company_delivery_containers_by_region.get(rid)
            if container is None:
                return
            if rows:
                container.controls = [build_company_delivery_row(row) for row in rows]
            else:
                container.controls = [
                    ft.Text("Способи доставки не налаштовані", color=_th_muted, size=12)
                ]
            if self.content_area and self.content_area.page:
                self.content_area.update()

        def make_company_delivery_row(
            mid,
            name,
            is_custom,
            enabled,
            base_price,
            price_currency="USD",
            delivery_term_days=None,
            region_id=None,
        ):
            name_f = ft.TextField(
                label="Назва способу" if is_custom else "Спосіб",
                value=name or "",
                border_radius=10,
                width=int(settings_layout.get("delivery_name_w") or 320),
                height=int(settings_layout.get("delivery_row_h") or 42),
                # Базові способи редагує тільки адмін/суперадмін; свої користувач не міняє.
                disabled=(not is_custom)
                or (not self.can_manage_company_settings)
                or self.read_only_subordinate,
            )
            enabled_cb = ft.Checkbox(
                label="Увімкнено",
                value=bool(enabled),
                disabled=(not self.can_manage_company_settings)
                or self.read_only_subordinate,
            )
            base_price_f = ft.TextField(
                label="Базова ціна за кг",
                value=_format_price(base_price),
                width=int(settings_layout.get("delivery_price_w") or 180),
                height=int(settings_layout.get("delivery_row_h") or 42),
                border_radius=10,
                disabled=(not self.can_manage_company_settings)
                or self.read_only_subordinate,
            )
            currency_f = ft.Dropdown(
                label="Валюта",
                options=[ft.dropdown.Option(c) for c in self.currencies_list],
                value=(
                    price_currency if price_currency in self.currencies_list else "USD"
                ),
                width=int(settings_layout.get("delivery_ccy_w") or 120),
                height=int(settings_layout.get("delivery_row_h") or 42),
                border_radius=10,
                disabled=(not self.can_manage_company_settings)
                or self.read_only_subordinate,
            )
            delivery_term_f = ft.TextField(
                label="Термін доставки (дні)",
                value=(
                    "" if delivery_term_days is None else str(int(delivery_term_days))
                ),
                width=int(settings_layout.get("delivery_term_w") or 170),
                height=int(settings_layout.get("delivery_row_h") or 42),
                border_radius=10,
                disabled=(not self.can_manage_company_settings)
                or self.read_only_subordinate,
            )

            row = {
                "id": mid,
                "name_f": name_f,
                "enabled_cb": enabled_cb,
                "base_price_f": base_price_f,
                "currency_f": currency_f,
                "delivery_term_f": delivery_term_f,
                "is_custom": bool(is_custom),
            }

            if bool(is_custom) and self.can_manage_company_settings:
                remove_btn = ft.IconButton(
                    ft.Icons.DELETE_OUTLINE,
                    icon_color=ft.Colors.RED_600,
                    tooltip="Видалити спосіб",
                    disabled=self.read_only_subordinate,
                    visible=False,  # requested: no delete in this pricing UI
                )

                def remove_custom_method(_, _rid=region_id):
                    rows = company_delivery_rows_by_region.get(_rid) or []
                    if row in rows:
                        rows.remove(row)
                        refresh_company_delivery_rows_for(_rid)

                remove_btn.on_click = remove_custom_method
                row["remove_btn"] = remove_btn
            return row

        def add_custom_delivery_method_for(rid):
            def _add(_):
                company_delivery_rows_by_region.setdefault(rid, []).append(
                    make_company_delivery_row(
                        mid=f"custom_{uuid.uuid4().hex[:10]}",
                        name="",
                        is_custom=True,
                        enabled=False,
                        base_price=None,
                        price_currency="USD",
                        region_id=rid,
                    )
                )
                refresh_company_delivery_rows_for(rid)

            return _add

        for method in company_delivery_methods:
            if not isinstance(method, dict) or not method.get("id"):
                continue
            mid = method.get("id")
            is_custom = bool(method.get("is_custom"))
            name_val = (method.get("name") or "").strip()
            enabled_val = bool(method.get("enabled"))
            base_price_val = _parse_float_or_none(method.get("base_price_per_kg"))
            delivery_term_val = _parse_float_or_none(method.get("delivery_term_days"))
            price_currency_val = (method.get("price_currency") or "USD").upper()
            # Hide fully empty custom placeholders; custom rows are added manually via button.
            if (
                is_custom
                and not name_val
                and not enabled_val
                and base_price_val is None
                and delivery_term_val is None
            ):
                continue

            if enabled_val and name_val:
                override_entry = manager_delivery_overrides.get(mid)
                if isinstance(override_entry, dict):
                    mgr_price_val = override_entry.get("price")
                    mgr_ccy_val = str(
                        override_entry.get("currency") or price_currency_val
                    ).upper()
                    mgr_term_val = _parse_float_or_none(
                        override_entry.get("delivery_term_days")
                    )
                    mgr_enabled_val = bool(override_entry.get("enabled", True))
                else:
                    mgr_price_val = override_entry
                    mgr_ccy_val = price_currency_val
                    mgr_term_val = _parse_float_or_none(
                        method.get("delivery_term_days")
                    )
                    mgr_enabled_val = True

                manager_enabled_cb = ft.Checkbox(
                    label=name_val,
                    value=mgr_enabled_val,
                    disabled=self.read_only_subordinate,
                )
                manager_price_f = ft.TextField(
                    label=f"Ваша ціна/кг ({price_currency_val if price_currency_val in self.currencies_list else 'USD'})",
                    value=_format_price(
                        mgr_price_val
                        if mgr_price_val is not None
                        else method.get("base_price_per_kg")
                    ),
                    border_radius=10,
                    width=210,
                    disabled=self.read_only_subordinate,
                )
                manager_currency_f = ft.Dropdown(
                    options=[ft.dropdown.Option(c) for c in self.currencies_list],
                    value=mgr_ccy_val if mgr_ccy_val in self.currencies_list else "USD",
                    width=110,
                    border_radius=10,
                    disabled=self.read_only_subordinate,
                )
                manager_term_f = ft.TextField(
                    label="Ваш термін (дні)",
                    value=("" if mgr_term_val is None else str(int(mgr_term_val))),
                    width=150,
                    border_radius=10,
                    disabled=self.read_only_subordinate,
                )
                manager_delivery_rows.append(
                    {
                        "id": mid,
                        "enabled_cb": manager_enabled_cb,
                        "price_f": manager_price_f,
                        "currency_f": manager_currency_f,
                        "term_f": manager_term_f,
                    }
                )

        # --- Per-manager custom production term ---
        _mgr_prod_row = _load_manager_production_term_state() or {}
        mgr_prod_days_val = _mgr_prod_row.get("days")
        manager_prod_days_f = ft.TextField(
            label="Кількість днів",
            value=("" if mgr_prod_days_val is None else str(int(mgr_prod_days_val))),
            border_radius=10,
            width=150,
            disabled=self.read_only_subordinate,
        )
        manager_prod_label = ft.Text(
            'Термін виробництва "Своя Назва"',
            size=12,
            weight="bold",
        )

        def _build_payment_field_rows_block(region_id, region_name):
            """Inline (non-dialog) payment-field-templates editor for ONE region.

            Payment field templates are configured per region (same reasoning
            as delivery methods below) — each region gets its own block, all
            shown at once, instead of one shared dialog scoped to whichever
            region the editing admin currently has active.
            """
            cid = self.company_id
            templates = (
                self.db.get_company_payment_field_templates(cid, region_id=region_id)
                or []
                if cid
                else []
            )
            rows_col = ft.Column(spacing=8)

            def _add_row(name_val="", has_text=True, has_image=False):
                name_f = ft.TextField(
                    label="Назва", border_radius=10, value=name_val, width=320
                )
                text_cb = ft.Checkbox(label="Текст", value=bool(has_text))
                img_cb = ft.Checkbox(label="Картинка", value=bool(has_image))
                del_btn = ft.IconButton(
                    ft.Icons.DELETE_OUTLINE,
                    icon_color=ft.Colors.RED_600,
                    tooltip=self.t("common.delete", "Видалити"),
                    disabled=self.read_only_subordinate,
                )
                row = ft.Row(
                    [
                        name_f,
                        ft.Row([text_cb, img_cb], spacing=6, wrap=True),
                        del_btn,
                    ],
                    spacing=10,
                    wrap=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
                row.data = {"name": name_f, "text": text_cb, "img": img_cb}

                def _del(_):
                    if row in rows_col.controls:
                        rows_col.controls.remove(row)
                        with contextlib.suppress(Exception):
                            rows_col.update()

                del_btn.on_click = _del
                rows_col.controls.append(row)

            for t in templates:
                if isinstance(t, dict):
                    _add_row(
                        t.get("name", ""),
                        bool(t.get("has_text", True)),
                        bool(t.get("has_image", False)),
                    )

            if not rows_col.controls:
                _add_row("SWIFT", True, False)
                _add_row("AliPay ID", True, True)
                _add_row("WeChat ID", True, True)

            def _save(_):
                if not cid:
                    self._show_snack("Не визначено компанію", "orange")
                    return
                try:
                    payload = []
                    seen = set()
                    for idx, r in enumerate(rows_col.controls):
                        if not isinstance(r, ft.Row) or not getattr(r, "data", None):
                            continue
                        name_f = r.data.get("name")
                        n = (name_f.value or "").strip()
                        name_f.error_text = None
                        if not n:
                            continue
                        key = n.lower()
                        if key in seen:
                            name_f.error_text = "Повтор назви"
                            self._show_snack(
                                "Назви платіжних полів не повинні повторюватися", "orange"
                            )
                            with contextlib.suppress(Exception):
                                name_f.update()
                            return
                        seen.add(key)
                        payload.append(
                            {
                                "name": n,
                                "has_text": bool(r.data.get("text").value),
                                "has_image": bool(r.data.get("img").value),
                                "order_index": idx,
                            }
                        )
                    ok = self.db.save_company_payment_field_templates(
                        cid, payload, region_id=region_id
                    )
                    if ok:
                        self._show_snack("Платіжні поля збережено", "green")
                    else:
                        self._show_snack(
                            self.t(
                                "settings.err.save_payment_fields",
                                "Не вдалося зберегти платіжні поля",
                            ),
                            "red",
                        )
                except Exception:
                    logger.exception("[settings] payment field templates save failed")
                    self._show_snack(
                        self.t("settings.err.save_failed", "Не вдалося зберегти"), "red"
                    )

            return build_collapsible_block(
                region_name,
                [
                    rows_col,
                    ft.Row(
                        [
                            ft.TextButton(
                                self.t("settings.add_field_btn", "Додати поле"),
                                icon=ft.Icons.ADD,
                                on_click=lambda _: (_add_row(), rows_col.update()),
                                disabled=self.read_only_subordinate,
                            ),
                            ft.FilledButton(
                                self.t("common.save", "Зберегти"),
                                icon=ft.Icons.SAVE,
                                on_click=_save,
                                disabled=self.read_only_subordinate,
                            ),
                        ],
                        spacing=10,
                        wrap=True,
                    ),
                ],
                initially_open=False,
                key_prefix="payment_region",
            )

        # --- Company-wide delivery methods, configured per region ---
        company_regions_for_settings = [
            r
            for r in (self.db.get_company_regions(self.company_id) or [])
            if isinstance(r, dict) and r.get("id")
        ]
        for _reg in company_regions_for_settings:
            _rid = _reg.get("id")
            _rows = company_delivery_rows_by_region.setdefault(_rid, [])
            company_delivery_containers_by_region[_rid] = ft.Column(spacing=8)
            for method in self.db.get_company_delivery_methods(self.company_id, _rid) or []:
                if not isinstance(method, dict) or not method.get("id"):
                    continue
                r_is_custom = bool(method.get("is_custom"))
                r_name_val = (method.get("name") or "").strip()
                r_enabled_val = bool(method.get("enabled"))
                r_base_price_val = _parse_float_or_none(method.get("base_price_per_kg"))
                r_delivery_term_val = _parse_float_or_none(
                    method.get("delivery_term_days")
                )
                r_price_currency_val = (method.get("price_currency") or "USD").upper()
                if (
                    r_is_custom
                    and not r_name_val
                    and not r_enabled_val
                    and r_base_price_val is None
                    and r_delivery_term_val is None
                ):
                    continue
                _rows.append(
                    make_company_delivery_row(
                        mid=method.get("id"),
                        name=r_name_val,
                        is_custom=r_is_custom,
                        enabled=r_enabled_val,
                        base_price=r_base_price_val,
                        price_currency=r_price_currency_val,
                        delivery_term_days=r_delivery_term_val,
                        region_id=_rid,
                    )
                )
            refresh_company_delivery_rows_for(_rid)

        delivery_region_blocks = [
            build_collapsible_block(
                str(reg.get("name") or reg.get("id")),
                [
                    company_delivery_containers_by_region[reg.get("id")],
                    ft.OutlinedButton(
                        "Додати спосіб доставки",
                        icon=ft.Icons.ADD,
                        on_click=add_custom_delivery_method_for(reg.get("id")),
                        visible=self.can_manage_company_settings
                        and not self.read_only_subordinate,
                    ),
                ],
                initially_open=(len(company_regions_for_settings) <= 1),
                key_prefix="delivery_region",
            )
            for reg in company_regions_for_settings
        ] or [ft.Text("У компанії немає регіонів", size=12, color=_th_muted)]

        payment_templates_region_blocks = [
            _build_payment_field_rows_block(
                reg.get("id"), str(reg.get("name") or reg.get("id"))
            )
            for reg in company_regions_for_settings
        ] or [ft.Text("У компанії немає регіонів", size=12, color=_th_muted)]

        # Build per-manager payment overrides UI (based on company templates).
        payment_fields_container = ft.Column(spacing=8)
        for t in company_payment_templates or []:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            tid = str(t.get("id"))
            base_name = (t.get("name") or "").strip()
            ov = (
                manager_payment_overrides.get(tid)
                if isinstance(manager_payment_overrides, dict)
                else None
            )
            ov = ov if isinstance(ov, dict) else {}
            visible_val = bool(ov.get("visible", True))
            custom_name_val = (ov.get("custom_name") or "").strip()
            eff_name = custom_name_val or base_name

            use_cb = ft.Checkbox(
                label="Використовувати",
                value=visible_val,
                disabled=self.read_only_subordinate,
            )
            name_f = ft.TextField(
                label="Назва поля",
                value=eff_name,
                border_radius=10,
                width=260,
                disabled=self.read_only_subordinate,
            )
            text_cb = ft.Checkbox(
                label="Текст",
                value=(
                    bool(ov.get("has_text"))
                    if ov.get("has_text") is not None
                    else bool(t.get("has_text", True))
                ),
                disabled=self.read_only_subordinate,
            )
            img_cb = ft.Checkbox(
                label="Картинка",
                value=(
                    bool(ov.get("has_image"))
                    if ov.get("has_image") is not None
                    else bool(t.get("has_image", False))
                ),
                disabled=self.read_only_subordinate,
            )

            def _sync_row_visibility(
                _=None, cb=use_cb, namef=name_f, tcb=text_cb, icb=img_cb
            ):
                enabled = bool(cb.value)
                namef.disabled = self.read_only_subordinate or not enabled
                tcb.disabled = self.read_only_subordinate or not enabled
                icb.disabled = self.read_only_subordinate or not enabled

            use_cb.on_change = _sync_row_visibility
            _sync_row_visibility()

            payment_override_rows.append(
                {
                    "id": tid,
                    "base_name": base_name,
                    "use_cb": use_cb,
                    "name_f": name_f,
                    "text_cb": text_cb,
                    "img_cb": img_cb,
                }
            )
            payment_fields_container.controls.append(
                ft.Container(
                    padding=10,
                    border=ft.border.all(1, "#E5E7EB"),
                    border_radius=10,
                    content=ft.Column(
                        [
                            ft.Row(
                                [ft.Text(base_name, weight="bold", size=12), use_cb],
                                spacing=10,
                                wrap=True,
                            ),
                            ft.Row(
                                [
                                    name_f,
                                    ft.Container(
                                        width=120,
                                        content=ft.Row(
                                            [text_cb, img_cb], spacing=6, wrap=True
                                        ),
                                    ),
                                ],
                                spacing=10,
                                wrap=True,
                            ),
                        ],
                        spacing=8,
                    ),
                )
            )

        def create_region_card(name="", prices=None, is_first=False):
            """Створює картку регіону з вибором валюти окремо для кожної ціни"""
            if prices is None:
                prices = []
            prices_col = ft.Column(spacing=6)

            def add_price_field(price_name="", price_curr="UAH"):
                p_name_input = ft.TextField(
                    label=f"Рівень ціни {len(prices_col.controls)+1}",
                    value=price_name,
                    border_radius=10,
                    height=42,
                    text_size=13,
                    expand=True,
                )
                p_curr_dropdown = ft.Dropdown(
                    options=[ft.dropdown.Option(c) for c in self.currencies_list],
                    value=price_curr if price_curr in self.currencies_list else "UAH",
                    width=100,
                    border_radius=10,
                    height=42,
                    text_size=13,
                )

                price_row = ft.Row([p_name_input, p_curr_dropdown], spacing=10)
                price_row.data = {
                    "name_input": p_name_input,
                    "curr_input": p_curr_dropdown,
                }

                prices_col.controls.append(price_row)
                self.content_area.update()

            reg_name_input = ft.TextField(
                label="Назва регіону",
                value=name,
                border_radius=10,
                height=42,
                text_size=13,
                expand=True,
                disabled=True,
            )

            # Заповнюємо існуючими даними з бази
            if prices:
                for p in prices:
                    add_price_field(p.get("name", ""), p.get("currency", "UAH"))
            # No default tiers. User adds levels explicitly.

            card = ft.Card(
                content=ft.Container(
                    padding=14,
                    content=ft.Column(
                        [
                            ft.Row(
                                [
                                    reg_name_input,
                                    ft.IconButton(
                                        ft.Icons.DELETE_OUTLINE,
                                        icon_color="red",
                                        visible=False,
                                        on_click=lambda _: remove_region_card(card),
                                    ),
                                ],
                                spacing=10,
                            ),
                            ft.Text(
                                "Рівні цін для цього регіону:",
                                size=11,
                                weight="bold",
                                color=_th_text,
                            ),
                            prices_col,
                            ft.TextButton(
                                self.t("settings.add_price_tier", "Додати рівень ціни"),
                                icon="add",
                                on_click=lambda _: add_price_field(),
                                visible=self.can_manage_company_settings
                                and not self.read_only_subordinate,
                            ),
                        ]
                    ),
                )
            )
            card.data = {"name": reg_name_input, "prices_container": prices_col}
            return card

        def remove_region_card(card):
            regions_list_container.controls.remove(card)
            self.content_area.update()

        # --- ЗАВАНТАЖЕННЯ ДАНИХ З SUPABASE ---
        # Отримуємо всі регіони з їх айдішниками
        regions_raw = self.db.get_company_regions(self.company_id)

        def _settings_list_button(title, subtitle, on_click, disabled=False):
            return ft.OutlinedButton(
                on_click=None if disabled else on_click,
                disabled=disabled,
                style=ft.ButtonStyle(
                    padding=ft.padding.symmetric(horizontal=12, vertical=10),
                    shape=ft.RoundedRectangleBorder(radius=8),
                    side=ft.BorderSide(1, "#E5E7EB"),
                ),
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Icon(
                                    ft.Icons.EDIT, size=16, color=ft.Colors.BLUE_700
                                ),
                                ft.Text(
                                    title or "Регіон", weight="bold", color=_th_text
                                ),
                            ],
                            spacing=6,
                        ),
                        ft.Text(subtitle or "", size=12, color=_th_muted),
                    ],
                    spacing=4,
                ),
            )

        def _add_region_from_settings(_):
            try:
                base = "Новий регіон"
                names = {
                    r.get("name")
                    for r in self.db.get_company_regions(self.company_id)
                    if isinstance(r, dict) and r.get("name")
                }
                candidate = base
                idx = 2
                while candidate in names:
                    candidate = f"{base} {idx}"
                    idx += 1
                new_id = self.db.create_region_with_defaults(self.company_id, candidate)
                if not new_id:
                    self._show_snack(self.t("settings.err.create_region", "Не вдалося створити регіон"), "red")
                    return
                self._show_snack(self.t("settings.region_created", "Регіон створено"), "green")
                self._build_deferred()
            except Exception:
                logger.exception("[settings] _add_region_from_settings failed")
                self._show_snack(self.t("settings.err.create_region", "Не вдалося створити регіон"), "red")

        def _open_rename_region_dialog(region):
            region = region if isinstance(region, dict) else {}
            region_id = region.get("id")
            if not region_id:
                return

            name_f = ft.TextField(
                label="Назва регіону",
                value=region.get("name", ""),
                border_radius=10,
                autofocus=True,
            )

            def _save(_):
                try:
                    name_f.error_text = None
                    new_name = (name_f.value or "").strip()
                    if not new_name:
                        name_f.error_text = "Введіть назву регіону"
                        self._show_snack(self.t("settings.err.enter_region_name", "Введіть назву регіону"), "orange")
                        with contextlib.suppress(Exception):
                            name_f.update()
                        return
                    if self.db.region_name_exists(
                        self.company_id, new_name, exclude_region_id=region_id
                    ):
                        name_f.error_text = "Такий регіон вже існує"
                        self._show_snack(self.t("settings.err.region_exists", "Такий регіон вже існує"), "orange")
                        with contextlib.suppress(Exception):
                            name_f.update()
                        return
                    ok = self.db.update_region_name(region_id, new_name)
                    if not ok:
                        self._show_snack("Не вдалося перейменувати регіон", "red")
                        return
                    self._close_dialog(dlg)
                    self._show_snack(self.t("settings.region_updated", "Регіон оновлено"), "green")
                    self._build_deferred()
                except Exception:
                    logger.exception("[settings] region rename failed")
                    self._show_snack(self.t("settings.err.save_failed", "Не вдалося зберегти"), "red")

            def _rename_region_help(_):
                from views.help_utils import show_modal_help
                show_modal_help(self.content_area.page, self.t("settings.dlg.rename_region", "Перейменувати регіон"), [
                    self.t("settings.rename_region_help.step1", "1. Введіть нову назву регіону."),
                    self.t("settings.rename_region_help.step2", "2. Натисніть «Зберегти»."),
                ])

            dlg = ft.AlertDialog(
                title=ft.Row(
                    [
                        ft.Text(self.t("settings.dlg.rename_region", "Перейменувати регіон"), expand=True),
                        ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_rename_region_help),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
                ),
                content=ft.Column([name_f], tight=True, width=420),
                actions=[
                    ft.TextButton(
                        self.t("common.cancel", "Скасувати"), on_click=lambda _: self._close_dialog(dlg)
                    ),
                    ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save),
                ],
            )
            self.content_area.page.show_dialog(dlg)

        def _delete_region_from_settings(region):
            region = region if isinstance(region, dict) else {}
            region_id = region.get("id")
            if not region_id:
                return

            def _confirm(_):
                try:
                    ok = self.db.delete_region_with_prices(region_id)
                    if not ok:
                        self._show_snack("Не вдалося видалити регіон", "red")
                        return
                    self._close_dialog(dlg)
                    self._show_snack(self.t("settings.region_deleted", "Регіон видалено"), "blue")
                    self._build_deferred()
                except Exception:
                    logger.exception("[settings] region delete failed")
                    self._show_snack(self.t("settings.err.save_failed", "Не вдалося зберегти"), "red")

            dlg = ft.AlertDialog(
                title=ft.Text(self.t("settings.dlg.delete_region", "Видалити регіон")),
                content=ft.Text(f"Видалити регіон: {region.get('name', '')}?"),
                actions=[
                    ft.TextButton(
                        self.t("common.cancel", "Скасувати"), on_click=lambda _: self._close_dialog(dlg)
                    ),
                    ft.FilledButton(
                        self.t("common.delete", "Видалити"),
                        on_click=_confirm,
                        style=ft.ButtonStyle(
                            bgcolor=ft.Colors.RED_600, color=ft.Colors.WHITE
                        ),
                    ),
                ],
            )
            self.content_area.page.show_dialog(dlg)

        def _open_edit_region_prices_dialog(region):
            region = region if isinstance(region, dict) else {}
            region_id = region.get("id")
            if not region_id:
                return

            editable = self.can_edit_pricing_settings
            # Match superadmin modal layout: fixed-width row cells, no internal scroll jitter.
            dlg_w = 560
            rows_container = ft.Column(spacing=8)
            empty_hint = ft.Text(
                "Немає рівнів цін. Натисніть “Додати рівень ціни”.",
                size=12,
                color=_th_muted,
            )
            empty_hint.data = {"kind": "empty_hint"}

            def _sync_empty_hint():
                has_rows = any(isinstance(x, ft.Row) for x in rows_container.controls)
                if has_rows:
                    rows_container.controls = [
                        c
                        for c in rows_container.controls
                        if not (
                            isinstance(getattr(c, "data", None), dict)
                            and c.data.get("kind") == "empty_hint"
                        )
                    ]
                else:
                    if not any(
                        isinstance(getattr(c, "data", None), dict)
                        and c.data.get("kind") == "empty_hint"
                        for c in rows_container.controls
                    ):
                        rows_container.controls.insert(0, empty_hint)

            def _append_price_row(name="", curr="UAH"):
                _sync_empty_hint()
                # Keep the same proportions as superadmin:
                # name ~= dlg_w - dropdown(120) - delete cell(36) - spacing/padding(24)
                name_w = max(260, int(dlg_w) - 120 - 36 - 24)
                name_f = ft.TextField(
                    label="Назва",
                    value=name,
                    border_radius=10,
                    height=42,
                    text_size=13,
                    width=name_w,
                    disabled=not editable,
                )
                curr_dd = ft.Dropdown(
                    options=[ft.dropdown.Option(c) for c in self.currencies_list],
                    value=curr if curr in self.currencies_list else "UAH",
                    width=120,
                    border_radius=10,
                    height=42,
                    text_size=13,
                    disabled=not editable,
                )
                remove_btn = ft.IconButton(
                    ft.Icons.DELETE_OUTLINE,
                    icon_color=ft.Colors.RED_600,
                    # requested: segmentation UI without delete action
                    visible=editable,
                    disabled=not editable,
                    style=ft.ButtonStyle(padding=ft.padding.all(0)),
                )
                # Dedicated delete cell spacer to keep alignment stable.
                row = ft.Row(
                    [
                        name_f,
                        curr_dd,
                        ft.Container(
                            width=36, alignment=ft.Alignment.CENTER, content=remove_btn
                        ),
                    ],
                    spacing=8,
                    width=dlg_w,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
                row.data = {"name_input": name_f, "curr_input": curr_dd}

                def _remove(_):
                    if not editable:
                        return
                    if row in rows_container.controls:
                        rows_container.controls.remove(row)
                        _sync_empty_hint()
                        with contextlib.suppress(Exception):
                            rows_container.update()

                remove_btn.on_click = _remove
                rows_container.controls.append(row)
                _sync_empty_hint()
                with contextlib.suppress(Exception):
                    rows_container.update()

            # Personal (per-user) tiers for this region.
            tiers = self.db.get_effective_region_price_tiers_for_manager(
                self.company_id,
                self.user_data.get("id"),
                region_id,
            )
            if tiers:
                for t in tiers:
                    if isinstance(t, dict):
                        _append_price_row(t.get("name", ""), t.get("currency", "UAH"))
            # No default tiers. User adds levels explicitly.
            _sync_empty_hint()

            def _add_row(_):
                _append_price_row("", "UAH")
                with contextlib.suppress(Exception):
                    rows_container.update()

            def _save(_):
                if not editable:
                    self._close_dialog(dlg)
                    return

                try:
                    payload = []
                    seen_names = set()
                    for p_row in rows_container.controls:
                        if not isinstance(p_row, ft.Row) or not getattr(
                            p_row, "data", None
                        ):
                            continue
                        name_input = p_row.data.get("name_input")
                        p_name = (name_input.value or "").strip()
                        p_curr = p_row.data.get("curr_input").value or "UAH"
                        if name_input is not None:
                            name_input.error_text = None
                        if p_name:
                            key = p_name.lower()
                            if key in seen_names:
                                if name_input is not None:
                                    name_input.error_text = "Такий рівень вже додано"
                                    with contextlib.suppress(Exception):
                                        name_input.update()
                                self._show_snack(
                                    "Назви рівнів цін не повинні повторюватися", "orange"
                                )
                                return
                            seen_names.add(key)
                            payload.append({"name": p_name, "currency": p_curr})

                    # Allow saving empty list to clear all tiers.

                    ok = self.db.save_manager_region_price_tiers_overrides(
                        self.user_data.get("id"),
                        region_id,
                        payload,
                    )
                    if not ok:
                        self._show_snack("Не вдалося зберегти рівні цін", "red")
                        return

                    self._close_dialog(dlg)
                    self._show_snack(self.t("settings.price_tiers_saved", "Рівні цін збережено"), "green")
                    self._build_deferred()
                except Exception:
                    logger.exception("[settings] price tiers save failed")
                    self._show_snack(self.t("settings.err.save_failed", "Не вдалося зберегти"), "red")

            def _price_tiers_help(_):
                from views.help_utils import show_modal_help
                show_modal_help(self.content_area.page, "Рівні цін", [
                    self.t("settings.price_tiers_help.step1", "1. Введіть назву рівня ціни та оберіть валюту."),
                    self.t("settings.price_tiers_help.step2", "2. Додайте рівні кнопкою «Додати рівень ціни»."),
                    self.t("settings.price_tiers_help.step3", "3. Видаліть непотрібний рівень іконкою кошика."),
                    self.t("settings.price_tiers_help.step4", "4. Натисніть «Зберегти»."),
                ])

            dlg = ft.AlertDialog(
                title=ft.Row(
                    [
                        ft.Text(self.t("settings.dlg.price_tiers", "Рівні цін: {name}").format(name=region.get('name', 'Регіон')), expand=True),
                        ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_price_tiers_help),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
                ),
                content=ft.Container(
                    width=dlg_w,
                    content=ft.Column(
                        [
                            rows_container,
                            ft.TextButton(
                                self.t("settings.add_price_tier", "Додати рівень ціни"),
                                icon=ft.Icons.ADD,
                                on_click=_add_row,
                                visible=editable,
                                disabled=not editable,
                            ),
                        ],
                        tight=True,
                        spacing=8,
                    ),
                ),
                actions=[
                    ft.TextButton(
                        self.t("packaging.dlg.close", "Закрити"), on_click=lambda _: self._close_dialog(dlg)
                    ),
                    ft.FilledButton(
                        "Зберегти",
                        on_click=_save,
                        visible=editable,
                        disabled=not editable,
                    ),
                ],
            )
            self.content_area.page.show_dialog(dlg)

        def _open_edit_region_hierarchy_dialog(region):
            region = region if isinstance(region, dict) else {}
            region_id = region.get("id")
            if not region_id:
                return

            editable = (
                self.can_manage_company_settings and not self.read_only_subordinate
            )
            cfg = self.db.get_company_catalog_config(self.company_id, region_id)
            level_rows = []
            rows_container = ft.Column(spacing=8)

            for lvl in cfg.get("levels") or []:
                pos = int(lvl.get("position", 0))
                if pos < 1 or pos > 6:
                    continue
                title_f = ft.TextField(
                    label=f"Назва рівня {pos}",
                    value=(lvl.get("title") or f"Рівень {pos}"),
                    border_radius=10,
                    # expand=True on a Row child inside this dialog's scrolling
                    # column renders as an empty grey block in this Flet
                    # version (same issue already worked around elsewhere in
                    # this file, e.g. _wide_checkbox_row) — fixed width instead.
                    width=380,
                    disabled=not editable,
                )
                enabled_f = ft.Checkbox(
                    label="Увімкнено",
                    value=bool(lvl.get("enabled")),
                    disabled=not editable,
                )
                row = {"position": pos, "title": title_f, "enabled": enabled_f}
                level_rows.append(row)
                rows_container.controls.append(
                    ft.Row([title_f, enabled_f], spacing=10, wrap=True)
                )

            def _save(_):
                try:
                    catalog_config = {
                        "levels": [
                            {
                                "position": int(row.get("position", 0)),
                                "title": (
                                    row.get("title").value
                                    or f"Рівень {int(row.get('position', 0))}"
                                ).strip(),
                                "enabled": bool(row.get("enabled").value),
                            }
                            for row in level_rows
                            if int(row.get("position", 0)) in [1, 2, 3, 4, 5, 6]
                        ],
                        "use_products": False,
                    }
                    saved_id = self.db.save_region_price_setup(
                        self.company_id,
                        region_id,
                        region.get("name") or "",
                        self.db.get_region_price_tiers(region_id),
                        catalog_config=catalog_config,
                    )
                    if not saved_id:
                        self._show_snack(
                            "Не вдалося зберегти дефолтну ієрархію регіону", "red"
                        )
                        return
                    self._close_dialog(dlg)
                    self._show_snack("Ієрархію регіону збережено", "green")
                    self._build_deferred()
                except Exception:
                    logger.exception("[settings] region hierarchy save failed")
                    self._show_snack(self.t("settings.err.save_failed", "Не вдалося зберегти"), "red")

            def _hierarchy_help(_):
                from views.help_utils import show_modal_help
                show_modal_help(self.content_area.page, "Дефолтна ієрархія", [
                    self.t("settings.hierarchy_help.step1", "1. Вкажіть назви рівнів ієрархії (до 6 рівнів)."),
                    self.t("settings.hierarchy_help.step2", "2. Ці рівні підставляються автоматично при створенні нової категорії."),
                    self.t("settings.hierarchy_help.step3", "3. Натисніть «Зберегти»."),
                ])

            dlg = ft.AlertDialog(
                title=ft.Row(
                    [
                        ft.Text(self.t("settings.dlg.default_hierarchy", "Дефолтна ієрархія: {name}").format(name=region.get('name', 'Регіон')), expand=True),
                        ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_hierarchy_help),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
                ),
                content=ft.Column(
                    [
                        ft.Text(
                            "Ці рівні підставляються автоматично при створенні нової категорії в цьому регіоні.",
                            size=12,
                            color=_th_muted,
                        ),
                        rows_container,
                    ],
                    tight=True,
                    spacing=8,
                    width=560,
                ),
                actions=[
                    ft.TextButton(
                        self.t("packaging.dlg.close", "Закрити"), on_click=lambda _: self._close_dialog(dlg)
                    ),
                    ft.FilledButton(
                        "Зберегти",
                        on_click=_save,
                        visible=editable,
                        disabled=not editable,
                    ),
                ],
            )
            self.content_area.page.show_dialog(dlg)

        regions_manage_controls = []
        for reg in regions_raw:
            if not isinstance(reg, dict) or not reg.get("id"):
                continue
            regions_manage_controls.append(
                ft.Row(
                    [
                        # No expand=True here — inside this page's scrolling
                        # Column, expand=True on a Row child renders as an
                        # empty gray block in this Flet version (same bug
                        # already worked around in _wide_checkbox_row below).
                        # Fixed width instead; the Row's own wrap=True still
                        # reflows everything on narrow windows.
                        ft.Container(
                            content=_settings_list_button(
                                reg.get("name", "Регіон"),
                                "Натисніть для перейменування регіону",
                                lambda _, r=reg: _open_rename_region_dialog(r),
                                disabled=not (
                                    self.can_manage_company_settings
                                    and not self.read_only_subordinate
                                ),
                            ),
                            width=420,
                        ),
                        ft.OutlinedButton(
                            "Ієрархія",
                            icon=ft.Icons.ACCOUNT_TREE,
                            on_click=lambda _, r=reg: _open_edit_region_hierarchy_dialog(
                                r
                            ),
                            visible=self.can_manage_company_settings,
                        ),
                        ft.OutlinedButton(
                            self.t("common.delete", "Видалити"),
                            icon=ft.Icons.DELETE_OUTLINE,
                            style=ft.ButtonStyle(color=ft.Colors.RED_600),
                            on_click=lambda _, r=reg: _delete_region_from_settings(r),
                            visible=self.can_manage_company_settings
                            and not self.read_only_subordinate,
                        ),
                    ],
                    spacing=8,
                    wrap=True,
                )
            )
        if not regions_manage_controls:
            regions_manage_controls = [
                ft.Text(self.t("settings.no_regions", "У компанії поки немає регіонів"), color=_th_muted)
            ]

        regions_prices_controls = []
        # When a specific region is active, show only that region's price tiers
        if region_id_for_delivery is not None:
            _rp_src = [
                r for r in regions_raw
                if isinstance(r, dict) and (
                    str(r.get("id", "")) == str(region_id_for_delivery)
                    or str(r.get("name", "")).strip().lower()
                    == str(region_id_for_delivery).strip().lower()
                )
            ]
        else:
            _rp_src = regions_raw
        for reg in _rp_src:
            if not isinstance(reg, dict) or not reg.get("id"):
                continue
            tiers = self.db.get_effective_region_price_tiers_for_manager(
                self.company_id,
                self.user_data.get("id"),
                reg.get("id"),
            )
            if tiers:
                tier_text = ", ".join(
                    [
                        f"{t.get('name', '')} ({t.get('currency', 'UAH')})"
                        for t in tiers
                        if isinstance(t, dict)
                    ]
                )
            else:
                tier_text = "Немає рівнів цін"
            regions_prices_controls.append(
                _settings_list_button(
                    reg.get("name", "Регіон"),
                    tier_text,
                    lambda _, r=reg: _open_edit_region_prices_dialog(r),
                    disabled=False,
                )
            )
        if not regions_prices_controls:
            regions_prices_controls = [
                ft.Text(self.t("settings.no_regions", "У компанії поки немає регіонів"), color=_th_muted)
            ]

        # Старий inline-редактор по картках замінено на кнопки-рядки з модальним редагуванням.
        regions_list_container.controls = []

        # --- КОРИСТУВАЧІ КОМПАНІЇ (ДЛЯ АДМІНА/СУПЕРАДМІНА) ---
        user_full_name_f = ft.TextField(label="Ім'я", border_radius=10)
        user_email_f = ft.TextField(label="Email", border_radius=10)
        user_phone_f = ft.TextField(label="Телефон", border_radius=10)
        user_password_f = ft.TextField(
            label="Пароль", border_radius=10, password=True, can_reveal_password=True
        )
        user_role_f = ft.Dropdown(
            label="Роль",
            border_radius=10,
            options=[
                ft.dropdown.Option(x)
                for x in [
                    "Закупівельник",
                    "Категорійний менеджер",
                    "Керівник направлення",
                    "Керівник",
                    "Адмін",
                ]
            ],
            value="Категорійний менеджер",
        )
        user_parent_f = ft.Dropdown(label="Керівник", border_radius=10, options=[])
        # A Column with its own height=/scroll=ADAPTIVE, nested inside this
        # page's already-scrolling outer Column (content_area), renders as a
        # blank/grey block — Flutter doesn't support two nested scrollables
        # in the same axis and Flet surfaces the failure as a grey box
        # instead of a Python exception (see gray-dialog-nested-scroll memory;
        # same fix already applied to admin_dashboard.py/superadmin_dashboard.py
        # checkbox lists). Only content_area itself should own scroll here —
        # these stay plain, unbounded Columns.
        user_regions_checks_block = ft.Column(spacing=4)
        user_regions_checks_host = user_regions_checks_block
        user_regions_checks = {}
        user_catalog_checks_block = ft.Column(spacing=4)
        user_catalog_checks_host = user_catalog_checks_block
        user_catalog_checks = {}

        def _wide_checkbox_row(label, value=False, on_change=None):
            cb = ft.Checkbox(value=bool(value))
            # В діалогах з прокруткою `expand=True` інколи ламає layout (порожні/сірі блоки).
            # Тримаймо рядок максимально простим і стабільним.
            txt = ft.Text(label or "", expand=False)

            def _toggle(_):
                cb.value = not bool(cb.value)
                if callable(on_change):
                    with contextlib.suppress(Exception):
                        on_change(cb)
                with contextlib.suppress(Exception):
                    cb.update()

            if callable(on_change):

                def _cb_changed(_):
                    with contextlib.suppress(Exception):
                        on_change(cb)

                cb.on_change = _cb_changed

            row = ft.Container(
                content=ft.Row(
                    [cb, txt],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    wrap=True,
                ),
                padding=ft.padding.symmetric(horizontal=8, vertical=8),
                border_radius=8,
                ink=True,
                on_click=_toggle,
            )
            return cb, row

        def _catalog_label(node, regions_by_id):
            node_type = node.get("node_type") or ""
            name = node.get("name") or ""
            if node_type == "category":
                prefix = "Категорія"
            elif node_type.startswith("level_"):
                try:
                    pos = int(node_type.split("_")[-1])
                except Exception:
                    pos = 0
                prefix = f"Рівень {pos}" if pos else node_type
            else:
                prefix = node_type
            if node_type == "category":
                rid = node.get("region_id")
                rname = regions_by_id.get(rid, "") if rid else ""
                if rname:
                    return f"{prefix}: {name} ({rname})"
            return f"{prefix}: {name}"

        def _build_catalog_tree(nodes, regions_by_id, selected_ids=None):
            selected_ids = set(selected_ids or [])
            checks = {}
            by_id = {
                n.get("id"): n for n in nodes if isinstance(n, dict) and n.get("id")
            }
            children_of = {}
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                pid = n.get("parent_id")
                nid = n.get("id")
                if pid and nid and pid in by_id:
                    children_of.setdefault(pid, []).append(n)

            def _subtree_ids(root_id):
                out = set()
                stack = [root_id]
                while stack:
                    cur = stack.pop()
                    if not cur or cur in out:
                        continue
                    out.add(cur)
                    for ch in children_of.get(cur, []):
                        ch_id = ch.get("id")
                        if ch_id and ch_id not in out:
                            stack.append(ch_id)
                return out

            def _make_cb(node):
                nid = node.get("id")

                def _cascade(current_cb):
                    value = bool(getattr(current_cb, "value", False))
                    for did in _subtree_ids(nid):
                        dcb = checks.get(did)
                        if dcb is not None:
                            dcb.value = value

                cb, row = _wide_checkbox_row(
                    _catalog_label(node, regions_by_id),
                    nid in selected_ids,
                    on_change=_cascade,
                )
                checks[nid] = cb
                return row

            def _build_leaf_row(level_node):
                return ft.Container(
                    padding=ft.padding.only(left=12, top=2, bottom=2),
                    content=_make_cb(level_node),
                )

            def _build_level_tile(level_node):
                node_id = level_node.get("id")
                child_nodes = children_of.get(node_id, [])
                child_controls = [_build_level_tile(child) for child in child_nodes]
                header_row = ft.Row([_make_cb(level_node)], expand=True)
                if child_controls:
                    return ft.ExpansionTile(
                        title=header_row,
                        expanded=False,
                        controls=child_controls,
                        controls_padding=ft.padding.only(left=16),
                    )
                return ft.Container(padding=ft.padding.only(left=8), content=header_row)

            def _build_cat_tile(cat_node):
                cat_id = cat_node.get("id")
                child_nodes = children_of.get(cat_id, [])
                child_controls = [_build_level_tile(child) for child in child_nodes]
                header_row = ft.Row([_make_cb(cat_node)], expand=True)
                if child_controls:
                    return ft.ExpansionTile(
                        title=header_row,
                        expanded=False,
                        controls=child_controls,
                        controls_padding=ft.padding.only(left=16),
                    )
                return ft.Container(padding=ft.padding.only(left=4), content=header_row)

            categories = [n for n in nodes if n.get("node_type") == "category"]
            categories.sort(
                key=lambda n: (
                    str(regions_by_id.get(n.get("region_id"), "") or "").lower(),
                    (n.get("name") or "").lower(),
                )
            )
            grouped = {}
            for c in categories:
                rid = str(c.get("region_id") or "").strip()
                grouped.setdefault(rid, []).append(c)

            controls = []
            for rid in sorted(
                grouped.keys(),
                key=lambda x: str(regions_by_id.get(x, x or "Без регіону")).lower(),
            ):
                rname = str(regions_by_id.get(rid) or "").strip() or "Без регіону"
                controls.append(
                    ft.ExpansionTile(
                        title=ft.Row(
                            [
                                ft.Text(
                                    rname,
                                    size=12,
                                    weight=ft.FontWeight.W_600,
                                    color=_th_muted,
                                )
                            ]
                        ),
                        expanded=False,
                        controls=[_build_cat_tile(c) for c in grouped.get(rid, [])],
                        controls_padding=ft.padding.only(left=8),
                    )
                )
            return controls, checks

        def _reload_user_management_sources():
            managers = self.db.get_company_managers(self.company_id)
            user_parent_f.options = [
                ft.dropdown.Option(key=m.get("id"), text=m.get("full_name"))
                for m in managers
                if isinstance(m, dict) and m.get("id")
            ]
            user_parent_f.value = None

            user_regions_checks.clear()
            user_regions_checks_block.controls.clear()
            region_rows = self.db.get_company_regions(self.company_id)
            regions_by_id = {
                r.get("id"): r.get("name", "")
                for r in region_rows
                if isinstance(r, dict) and r.get("id")
            }

            self.db.sync_company_catalog_nodes(self.company_id)
            nodes_all = self.db.get_company_catalog_nodes(self.company_id)
            allowed_types = {
                "category",
                "level_1",
                "level_2",
                "level_3",
                "level_4",
                "level_5",
                "level_6",
            }
            nodes_all = [
                n
                for n in nodes_all
                if isinstance(n, dict)
                and n.get("id")
                and n.get("node_type") in allowed_types
            ]

            def _rebuild_creation_catalog_tree(_=None):
                # Only offer categories from region(s) checked above — a new
                # user restricted to one region must not be assignable
                # categories from a region they don't have access to.
                checked_region_ids = {
                    rid for rid, cb in user_regions_checks.items() if cb.value
                }
                user_catalog_checks.clear()
                if not checked_region_ids:
                    user_catalog_checks_block.controls = [
                        ft.Text(
                            "Спочатку оберіть регіон(и) вище",
                            size=11,
                            color=_th_muted,
                        )
                    ]
                else:
                    nodes = [
                        n
                        for n in nodes_all
                        if n.get("node_type") != "category"
                        or n.get("region_id") in checked_region_ids
                    ]
                    tree_controls, tree_checks = _build_catalog_tree(
                        nodes, regions_by_id, selected_ids=[]
                    )
                    user_catalog_checks.update(tree_checks)
                    user_catalog_checks_block.controls = tree_controls or [
                        ft.Text(
                            "У компанії немає елементів каталогу",
                            size=11,
                            color=_th_muted,
                        )
                    ]
                with contextlib.suppress(Exception):
                    user_catalog_checks_block.update()

            for reg in region_rows:
                if not isinstance(reg, dict) or not reg.get("id"):
                    continue
                rid = reg.get("id")
                cb, cb_row = _wide_checkbox_row(
                    reg.get("name", "Регіон"),
                    False,
                    on_change=_rebuild_creation_catalog_tree,
                )
                user_regions_checks[rid] = cb
                user_regions_checks_block.controls.append(cb_row)
            if not user_regions_checks_block.controls:
                user_regions_checks_block.controls.append(
                    ft.Text(self.t("settings.no_regions", "У компанії поки немає регіонів"), size=11, color=_th_muted)
                )

            _rebuild_creation_catalog_tree()

        def _open_edit_user_regions_dialog(manager):
            manager = manager if isinstance(manager, dict) else {}
            manager_id = manager.get("id")
            if not manager_id:
                self._show_snack("Не вдалося визначити користувача", "orange")
                return

            regions = self.db.get_company_regions(self.company_id)
            selected_ids = set(self.db.get_manager_allowed_region_ids(manager_id))
            checks = {}
            # Plain Column, no height/scroll of its own — it's already inside
            # a scrolling Column of a fixed-height Container below. A bare
            # Column carrying both a fixed height AND its own scroll while
            # nested inside another scrolling ancestor renders as an empty
            # grey block in this Flet version (same bug class fixed for
            # user_regions_checks_block above).
            checks_block = ft.Column(spacing=4)
            for reg in regions:
                if not isinstance(reg, dict) or not reg.get("id"):
                    continue
                rid = reg.get("id")
                cb, cb_row = _wide_checkbox_row(
                    reg.get("name", "Регіон"), rid in selected_ids
                )
                checks[rid] = cb
                checks_block.controls.append(cb_row)
            if not checks_block.controls:
                checks_block.controls.append(
                    ft.Text("Немає регіонів у компанії", color=_th_muted)
                )

            def save_regions(_):
                selected = [rid for rid, cb in checks.items() if cb.value]
                ok = self.db.save_manager_allowed_region_ids(manager_id, selected)
                if ok:
                    self._close_dialog(dlg)
                    self.content_area.page.show_dialog(success_dlg)
                else:
                    self._show_snack("Не вдалося зберегти доступні регіони", "red")

            def _regions_access_help(_):
                from views.help_utils import show_modal_help
                show_modal_help(self.content_area.page, "Регіони доступу", [
                    self.t("settings.regions_access_help.step1", "1. Відмітьте регіони, до яких матиме доступ користувач."),
                    self.t("settings.regions_access_help.step2", "2. Натисніть «Зберегти»."),
                ])

            dlg = ft.AlertDialog(
                title=ft.Row(
                    [
                        ft.Text(f"Регіони доступу: {manager.get('full_name', '')}", expand=True),
                        ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_regions_access_help),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
                ),
                content=ft.Container(
                    width=520,
                    height=self._dialog_content_height(500),
                    content=ft.Column(
                        [checks_block],
                        tight=True,
                        spacing=10,
                        scroll=ft.ScrollMode.ADAPTIVE,
                    ),
                ),
                actions=[
                    ft.TextButton(
                        self.t("common.cancel", "Скасувати"), on_click=lambda _: self._close_dialog(dlg)
                    ),
                    ft.FilledButton("Зберегти", on_click=save_regions),
                ],
            )
            self.content_area.page.show_dialog(dlg)

        def _open_edit_user_catalog_dialog(manager):
            manager = manager if isinstance(manager, dict) else {}
            manager_id = manager.get("id")
            if not manager_id:
                self._show_snack("Не вдалося визначити користувача", "orange")
                return

            self.db.sync_company_catalog_nodes(self.company_id)
            all_nodes = self.db.get_company_catalog_nodes(self.company_id)
            regions_raw = self.db.get_company_regions(self.company_id)
            regions = [r for r in regions_raw if isinstance(r, dict) and r.get("id")]
            regions_by_id = {r.get("id"): r.get("name", "") for r in regions}
            selected_ids = set(
                self.db.get_manager_scope_node_ids(
                    manager_id, company_id=self.company_id, permission="view"
                )
            )
            checks = {}
            # Plain Column — already inside a scrolling Column of a fixed-
            # height Container below (see checks_block above for the same fix).
            checks_col = ft.Column(spacing=4)

            manager_allowed_region_ids = set(
                self.db.get_manager_allowed_region_ids(manager_id)
            )
            allowed_types = {
                "category",
                "level_1",
                "level_2",
                "level_3",
                "level_4",
                "level_5",
                "level_6",
            }
            nodes = [
                n
                for n in all_nodes
                if isinstance(n, dict)
                and n.get("id")
                and n.get("node_type") in allowed_types
            ]
            if manager_allowed_region_ids:
                nodes = [
                    n
                    for n in nodes
                    if n.get("node_type") != "category"
                    or n.get("region_id") in manager_allowed_region_ids
                ]
            tree_controls, tree_checks = _build_catalog_tree(
                nodes, regions_by_id, selected_ids=selected_ids
            )
            checks.update(tree_checks)
            checks_col.controls = tree_controls

            if not tree_controls:
                checks_col.controls.append(
                    ft.Text("У компанії немає елементів каталогу", color=_th_muted)
                )

            def save_access(_):
                selected = [nid for nid, cb in checks.items() if cb.value]
                ok = self.db.replace_manager_access_scopes(
                    manager_id,
                    self.company_id,
                    selected,
                    permission="view",
                    inherit_to_children=True,
                )
                if ok:
                    self._close_dialog(dlg)
                    self.content_area.page.show_dialog(success_dlg)
                else:
                    self._show_snack("Не вдалося оновити доступи", "red")

            def _catalog_access_help(_):
                from views.help_utils import show_modal_help
                show_modal_help(self.content_area.page, "Доступи каталогу", [
                    self.t("settings.catalog_access_help.step1", "1. Відмітьте категорії/рівні каталогу, до яких матиме доступ користувач."),
                    self.t("settings.catalog_access_help.step2", "2. Дочірні вузли успадковують доступ автоматично."),
                    self.t("settings.catalog_access_help.step3", "3. Натисніть «Зберегти»."),
                ])

            dlg = ft.AlertDialog(
                title=ft.Row(
                    [
                        ft.Text(f"Доступи каталогу: {manager.get('full_name', '')}", expand=True),
                        ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_catalog_access_help),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
                ),
                content=ft.Container(
                    width=620,
                    height=self._dialog_content_height(640),
                    content=ft.Column(
                        [checks_col], tight=True, scroll=ft.ScrollMode.ADAPTIVE
                    ),
                ),
                actions=[
                    ft.TextButton(
                        self.t("common.cancel", "Скасувати"), on_click=lambda _: self._close_dialog(dlg)
                    ),
                    ft.FilledButton("Зберегти", on_click=save_access),
                ],
            )
            self.content_area.page.show_dialog(dlg)

        def _build_users_table():
            managers = self.db.get_company_managers(self.company_id)
            rows = []
            for m in managers:
                if not isinstance(m, dict):
                    continue
                rows.append(
                    ft.DataRow(
                        cells=[
                            ft.DataCell(ft.Text(m.get("full_name", ""))),
                            ft.DataCell(ft.Text(m.get("role", ""))),
                            ft.DataCell(
                                ft.Text("Так" if m.get("is_approved") else "Ні")
                            ),
                            ft.DataCell(
                                ft.Row(
                                    [
                                        ft.IconButton(
                                            ft.Icons.MAP,
                                            tooltip="Регіони",
                                            on_click=lambda _, manager=m: _open_edit_user_regions_dialog(
                                                manager
                                            ),
                                        ),
                                        ft.IconButton(
                                            ft.Icons.CATEGORY,
                                            tooltip="Каталог",
                                            on_click=lambda _, manager=m: _open_edit_user_catalog_dialog(
                                                manager
                                            ),
                                        ),
                                    ],
                                    spacing=0,
                                )
                            ),
                        ]
                    )
                )

            return ft.DataTable(
                columns=[
                    ft.DataColumn(ft.Text(self.t("settings.users_table.col.name", "Ім'я"))),
                    ft.DataColumn(ft.Text(self.t("settings.users_table.col.role", "Роль"))),
                    ft.DataColumn(ft.Text(self.t("settings.users_table.col.approved", "Підтв."))),
                    ft.DataColumn(ft.Text(self.t("settings.users_table.col.access", "Доступи"))),
                ],
                rows=rows,
            )

        _reload_user_management_sources()

        def create_user_click(_):
            user_full_name_f.error_text = None
            user_email_f.error_text = None
            user_phone_f.error_text = None
            user_password_f.error_text = None
            payload = {
                "company_id": self.company_id,
                "full_name": (user_full_name_f.value or "").strip(),
                "email": (user_email_f.value or "").strip(),
                "phone": (user_phone_f.value or "").strip(),
                "role": user_role_f.value,
                "password": user_password_f.value,
                "parent_manager_id": user_parent_f.value,
                "is_approved": True,
                "allowed_region_ids_json": [
                    rid for rid, cb in user_regions_checks.items() if cb.value
                ],
            }
            if (
                not payload["full_name"]
                or not payload["email"]
                or not payload["phone"]
                or not payload["password"]
            ):
                if not payload["full_name"]:
                    user_full_name_f.error_text = "Введіть ім'я"
                if not payload["email"]:
                    user_email_f.error_text = "Введіть email"
                if not payload["phone"]:
                    user_phone_f.error_text = "Введіть телефон"
                if not payload["password"]:
                    user_password_f.error_text = "Введіть пароль"
                self._show_snack("Заповніть обов'язкові поля користувача", "orange")
                for _f in (user_full_name_f, user_email_f, user_phone_f, user_password_f):
                    with contextlib.suppress(Exception):
                        _f.update()
                return

            try:
                if self.db.manager_contact_exists(
                    self.company_id, payload["email"], payload["phone"]
                ):
                    user_email_f.error_text = "Email або телефон вже існує"
                    user_phone_f.error_text = "Email або телефон вже існує"
                    self._show_snack(
                        "Користувач з таким email або телефоном вже існує", "orange"
                    )
                    with contextlib.suppress(Exception):
                        user_email_f.update()
                    with contextlib.suppress(Exception):
                        user_phone_f.update()
                    return

                new_id = self.db.create_manager(payload)
                if not new_id:
                    self._show_snack("Не вдалося створити користувача", "red")
                    return

                selected_nodes = [
                    nid for nid, cb in user_catalog_checks.items() if cb.value
                ]
                if selected_nodes:
                    self.db.replace_manager_access_scopes(
                        new_id,
                        self.company_id,
                        selected_nodes,
                        permission="view",
                        inherit_to_children=True,
                    )

                self.content_area.page.show_dialog(success_dlg)
                self._build_deferred()
            except Exception:
                logger.exception("[settings] create_user_click failed")
                self._show_snack(self.t("settings.err.save_failed", "Не вдалося зберегти"), "red")

        # --- ЛОГІКА ЗБЕРЕЖЕННЯ ---
        def close_dlg(e):
            self.content_area.page.pop_dialog()

        success_dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Успіх"),
            content=ft.Text("Дані успішно оновлені і збережені."),
            actions=[ft.TextButton("ОК", on_click=close_dlg)],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        current_personal_bg = self.db.get_manager_local_background(
            self.user_data.get("id")
        )

        def upload_personal_background(_):
            if self.read_only_subordinate:
                self._show_snack(
                    "У вкладці підлеглого доступний лише перегляд", "orange"
                )
                return
            file_path = self._pick_local_image_file()
            if not file_path:
                return
            try:
                saved = self.db.set_manager_local_background(
                    self.user_data.get("id"), file_path
                )
                if saved:
                    self._show_snack("Персональний фон збережено", "green")
                    # Apply immediately (no restart).
                    try:
                        cb = getattr(
                            self.content_area.page, "_refresh_background_now", None
                        )
                        if callable(cb):
                            cb()
                    except Exception:
                        pass
                    self._build_deferred()
                else:
                    self._show_snack("Не вдалося зберегти персональний фон", "red")
            except Exception:
                logger.exception("[settings] upload_personal_background failed")
                self._show_snack(self.t("settings.err.save_failed", "Не вдалося зберегти"), "red")

        def clear_personal_background(_):
            if self.read_only_subordinate:
                self._show_snack(
                    "У вкладці підлеглого доступний лише перегляд", "orange"
                )
                return
            try:
                ok = self.db.clear_manager_local_background(self.user_data.get("id"))
                if ok:
                    self._show_snack("Персональний фон очищено", "blue")
                    # Apply immediately (no restart).
                    try:
                        cb = getattr(
                            self.content_area.page, "_refresh_background_now", None
                        )
                        if callable(cb):
                            cb()
                    except Exception:
                        pass
                    self._build_deferred()
                else:
                    self._show_snack("Не вдалося очистити персональний фон", "red")
            except Exception:
                logger.exception("[settings] clear_personal_background failed")
                self._show_snack(self.t("settings.err.save_failed", "Не вдалося зберегти"), "red")

        def _save_profile(_):
            if self.read_only_subordinate:
                self._show_snack(
                    "У вкладці підлеглого доступний лише перегляд", "orange"
                )
                return
            try:
                payload = {
                    "full_name": manager_name.value,
                    "email": manager_email.value,
                    "phone": manager_phone.value,
                }
                ok = self.db.update_manager_data(self.user_data.get("id"), payload)
                if ok:
                    self.user_data["full_name"] = payload["full_name"]
                    self.user_data["email"] = payload["email"]
                    self.user_data["phone"] = payload["phone"]
                    self._show_snack("Профіль збережено", "green")
                    with contextlib.suppress(Exception):
                        self.content_area.update()
                else:
                    self._show_snack("Не вдалося зберегти профіль", "red")
            except Exception:
                logger.exception("[settings] _save_profile failed")
                self._show_snack(self.t("settings.err.save_failed", "Не вдалося зберегти"), "red")

        def _save_password(_):
            if self.read_only_subordinate:
                self._show_snack(
                    "У вкладці підлеглого доступний лише перегляд", "orange"
                )
                return
            if not old_password.value:
                self._show_snack(self.t("settings.err.enter_current_password", "Введіть поточний пароль"), "orange")
                return
            if str(old_password.value) != str(self.user_data.get("password")):
                self._show_snack("Поточний пароль невірно!", "red")
                return
            if not new_password.value or len(new_password.value) < 4:
                self._show_snack("Новий пароль від 4 символів!", "orange")
                return
            try:
                ok = self.db.update_manager_data(
                    self.user_data.get("id"), {"password": new_password.value}
                )
                if ok:
                    self.user_data["password"] = new_password.value
                    old_password.value = ""
                    new_password.value = ""
                    self._show_snack("Пароль змінено", "green")
                    with contextlib.suppress(Exception):
                        self.content_area.update()
                else:
                    self._show_snack("Не вдалося змінити пароль", "red")
            except Exception:
                logger.exception("[settings] _save_password failed")
                self._show_snack(self.t("settings.err.save_failed", "Не вдалося зберегти"), "red")

        def handle_update(e):
            if self.read_only_subordinate:
                self._show_snack(
                    "У вкладці підлеглого доступний лише перегляд", "orange"
                )
                return

            try:
                success_manager = True

                success_regions = True
                success_company_profile = True
                success_company_delivery = True
                success_manager_delivery = True
                success_manager_payment_fields = True
                if self.can_manage_company_settings:
                    for _rid, _region_rows in company_delivery_rows_by_region.items():
                        delivery_methods_payload = []
                        for row in _region_rows:
                            name_val = (row["name_f"].value or "").strip()
                            enabled_val = bool(row["enabled_cb"].value)
                            base_price_val = _parse_float_or_none(
                                row["base_price_f"].value
                            )
                            delivery_term_val = _parse_float_or_none(
                                row["delivery_term_f"].value
                            )
                            price_currency_val = (
                                row["currency_f"].value or "USD"
                            ).upper()
                            if row["is_custom"]:
                                has_data = (
                                    bool(name_val)
                                    or enabled_val
                                    or base_price_val is not None
                                    or delivery_term_val is not None
                                )
                                if not has_data:
                                    continue
                                if not name_val:
                                    self._show_snack(
                                        "Для нового способу доставки вкажіть назву",
                                        "orange",
                                    )
                                    return
                            delivery_methods_payload.append(
                                {
                                    "id": row["id"],
                                    "name": name_val,
                                    "enabled": enabled_val,
                                    "base_price_per_kg": base_price_val,
                                    "delivery_term_days": (
                                        int(delivery_term_val)
                                        if delivery_term_val is not None
                                        else None
                                    ),
                                    "price_currency": price_currency_val,
                                    "is_custom": bool(row["is_custom"]),
                                }
                            )
                        if not self.db.save_company_delivery_methods(
                            self.company_id,
                            delivery_methods_payload,
                            region_id=_rid,
                        ):
                            success_company_delivery = False

                manager_delivery_payload = {}
                for row in manager_delivery_rows:
                    val = _parse_float_or_none(row["price_f"].value)
                    term_val = _parse_float_or_none(row["term_f"].value)
                    enabled_val_save = bool(row["enabled_cb"].value)
                    # Save if user has a price/term override OR has disabled this method
                    if val is not None or term_val is not None or not enabled_val_save:
                        ccy = (row["currency_f"].value or "USD").upper()
                        manager_delivery_payload[row["id"]] = {
                            "price": val,
                            "currency": ccy,
                            "delivery_term_days": (
                                int(term_val) if term_val is not None else None
                            ),
                            "enabled": enabled_val_save,
                        }
                success_manager_delivery = self.db.save_manager_delivery_overrides(
                    self.user_data.get("id"),
                    manager_delivery_payload,
                    region_id=region_id_for_delivery,
                )

                # Save per-manager custom production term to workspace state (DB-backed)
                try:
                    _save_manager_production_term_state(
                        manager_prod_days_f.value
                        if "manager_prod_days_f" in locals()
                        else None,
                    )
                except Exception:
                    pass

                # Save per-manager payment fields overrides (company-scoped).
                payment_overrides_payload = {}
                for row in payment_override_rows:
                    tid = row.get("id")
                    if not tid:
                        continue
                    payment_overrides_payload[tid] = {
                        "visible": bool(row["use_cb"].value),
                        "custom_name": (row["name_f"].value or "").strip(),
                        "has_text": bool(row["text_cb"].value),
                        "has_image": bool(row["img_cb"].value),
                    }
                if self.company_id:
                    success_manager_payment_fields = (
                        self.db.save_manager_payment_field_overrides(
                            self.user_data.get("id"),
                            self.company_id,
                            payment_overrides_payload,
                            region_id=region_id_for_delivery,
                        )
                    )

                success = (
                    success_manager
                    and success_regions
                    and success_company_profile
                    and success_company_delivery
                    and success_manager_delivery
                    and success_manager_payment_fields
                )

                if success:
                    with contextlib.suppress(Exception):
                        if callable(self._on_settings_saved):
                            self._on_settings_saved()
                    self.content_area.page.show_dialog(success_dlg)
                else:
                    self._show_snack("Помилка при збереженні", "red")
            except Exception:
                logger.exception("[settings] handle_update failed")
                self._show_snack(self.t("settings.err.save_failed", "Не вдалося зберегти"), "red")

        # ── Theme + Symbol picker ────────────────────────────────────────────────
        def _load_current_theme() -> str:
            manager_id = _workspace_user_id()
            if not manager_id:
                return DEFAULT_THEME
            try:
                payload = self.db.get_manager_workspace_state(manager_id) or {}
                return str(payload.get("ui_theme") or DEFAULT_THEME)
            except Exception:
                return DEFAULT_THEME

        def _load_current_nav_pack() -> str:
            manager_id = _workspace_user_id()
            if not manager_id:
                return DEFAULT_NAV_PACK
            try:
                payload = self.db.get_manager_workspace_state(manager_id) or {}
                return str(payload.get("ui_nav_pack") or DEFAULT_NAV_PACK)
            except Exception:
                return DEFAULT_NAV_PACK

        def _save_theme(theme_key: str) -> None:
            manager_id = _workspace_user_id()
            if not manager_id:
                return
            try:
                payload = self.db.get_manager_workspace_state(manager_id) or {}
                if not isinstance(payload, dict):
                    payload = {}
                payload["ui_theme"] = theme_key
                self.db.save_manager_workspace_state(manager_id, payload)
            except Exception:
                pass

        def _save_nav_pack(pack_key: str) -> None:
            manager_id = _workspace_user_id()
            if not manager_id:
                return
            try:
                payload = self.db.get_manager_workspace_state(manager_id) or {}
                if not isinstance(payload, dict):
                    payload = {}
                payload["ui_nav_pack"] = pack_key
                self.db.save_manager_workspace_state(manager_id, payload)
            except Exception:
                pass

        def _build_theme_picker_controls():
            current_theme = _load_current_theme()
            current_nav = _load_current_nav_pack()
            cards_col = ft.Column(spacing=0)
            nav_cards_col = ft.Column(spacing=0)

            def _refresh_grid(selected_key: str, _notify: bool = True):
                _save_theme(selected_key)
                from views import themes as _t
                _mid = _workspace_user_id()
                if _mid:
                    _t.set_active_theme(_mid, selected_key)
                cards_col.controls.clear()
                for group_name, keys in THEME_GROUPS:
                    cards_col.controls.append(
                        ft.Container(
                            padding=ft.padding.only(top=12, bottom=4),
                            content=ft.Text(group_name, size=11, weight=ft.FontWeight.W_600,
                                            color=_th_muted),
                        )
                    )
                    row_items = []
                    for k in keys:
                        t = THEMES.get(k)
                        if not t:
                            continue
                        row_items.append(
                            build_theme_card(k, t, k == selected_key, _refresh_grid, current_theme=_th)
                        )
                    cards_col.controls.append(
                        ft.Row(row_items, spacing=8, wrap=True)
                    )
                try:
                    cards_col.update()
                except Exception:
                    pass
                if not _notify:
                    return
                theme_name = THEMES.get(selected_key, {}).get("name", selected_key)
                cb = getattr(self, "_on_theme_changed", None)
                if callable(cb):
                    try:
                        cb(selected_key)
                    except Exception:
                        pass
                self._show_snack(f"Тему «{theme_name}» застосовано!", "#0F172A")

            def _refresh_nav_grid(selected_key: str, _notify: bool = True):
                _save_nav_pack(selected_key)
                from views import themes as _t
                _mid = _workspace_user_id()
                if _mid:
                    _t.set_active_nav_pack(_mid, selected_key)
                nav_cards_col.controls.clear()
                for group_name, keys in NAV_ICON_PACK_GROUPS:
                    nav_cards_col.controls.append(
                        ft.Container(
                            padding=ft.padding.only(top=12, bottom=4),
                            content=ft.Text(group_name, size=11, weight=ft.FontWeight.W_600,
                                            color=_th_muted),
                        )
                    )
                    row_items = []
                    for k in keys:
                        p = NAV_ICON_PACKS.get(k)
                        if not p:
                            continue
                        row_items.append(
                            build_nav_pack_card(k, p, k == selected_key, _refresh_nav_grid, current_theme=_th)
                        )
                    nav_cards_col.controls.append(
                        ft.Row(row_items, spacing=8, wrap=True)
                    )
                try:
                    nav_cards_col.update()
                except Exception:
                    pass
                if not _notify:
                    return
                pack_name = NAV_ICON_PACKS.get(selected_key, {}).get("name", selected_key)
                cb_nav = getattr(self, "_on_nav_pack_changed", None)
                if callable(cb_nav):
                    try:
                        cb_nav(selected_key)
                    except Exception:
                        pass
                self._show_snack(f"Пакет іконок «{pack_name}» застосовано!", "#0F172A")

            _refresh_grid(current_theme, _notify=False)
            _refresh_nav_grid(current_nav, _notify=False)
            return ft.Container(
                content=ft.Column(
                    [
                        ft.Text(
                            "Оберіть тему. Застосовується одразу до всіх вкладок.",
                            size=12, color=_th_muted,
                        ),
                        cards_col,
                        ft.Divider(height=1, color=_th_border),
                        ft.Text(
                            "Пакет символів бічного меню — замінює іконки навігації.",
                            size=12, color=_th_muted,
                        ),
                        nav_cards_col,
                    ],
                    spacing=8,
                ),
                padding=ft.padding.only(top=4),
            )

        # ── GSM Parser per-user settings block ─────────────────────────────
        def _build_gsm_parser_block():
            if not self.db.is_company_gsm_parser_enabled(self.company_id):
                return []
            _mid = _workspace_user_id()
            prefs = self.db.get_gsm_parser_user_prefs(_mid) if _mid else {"enabled": True, "brands": "all", "device_types": "all"}

            enabled_sw = ft.Switch(
                value=bool(prefs.get("enabled", True)),
                label=self.t("settings.gsm.enable_label", "Увімкнути GSM Parser для мене"),
                label_text_style=ft.TextStyle(size=12, color=_th_text),
                active_color="#1D4ED8",
            )

            available_brands = self.db.get_gsm_parser_available_brands()
            available_types = self.db.get_gsm_parser_available_device_types()

            pref_brands = prefs.get("brands", "all")
            pref_types = prefs.get("device_types", "all")

            brand_chips_row = ft.Row([], spacing=6, wrap=True)
            dtype_chips_row = ft.Row([], spacing=6, wrap=True)

            def _make_chip(label, is_selected, on_toggle):
                chip = ft.Chip(
                    label=ft.Text(label, size=11),
                    selected=is_selected,
                    on_select=on_toggle,
                    selected_color="#DBEAFE",
                    show_checkmark=True,
                )
                return chip

            # "Всі" chip for brands
            all_brands_sel = (pref_brands == "all" or pref_brands == [])
            brand_selected: list = [] if all_brands_sel else list(pref_brands or [])

            def _refresh_brand_chips(_=None):
                all_sel = not brand_selected
                chips = [_make_chip(
                    self.t("common.all", "Всі"),
                    all_sel,
                    lambda e: _toggle_all_brands(e),
                )]
                for b in available_brands:
                    sel = b in brand_selected
                    chips.append(_make_chip(b, sel, lambda e, brand=b: _toggle_brand(e, brand)))
                brand_chips_row.controls = chips
                with contextlib.suppress(Exception):
                    brand_chips_row.update()

            def _toggle_all_brands(e):
                brand_selected.clear()
                _refresh_brand_chips()
                _save_gsm_prefs()

            def _toggle_brand(e, brand: str):
                if brand in brand_selected:
                    brand_selected.remove(brand)
                else:
                    brand_selected.append(brand)
                _refresh_brand_chips()
                _save_gsm_prefs()

            # "Всі" chip for device types
            all_types_sel = (pref_types == "all" or pref_types == [])
            type_selected: list = [] if all_types_sel else list(pref_types or [])

            def _refresh_type_chips(_=None):
                all_sel = not type_selected
                chips = [_make_chip(
                    self.t("common.all", "Всі"),
                    all_sel,
                    lambda e: _toggle_all_types(e),
                )]
                for dt in available_types:
                    sel = dt in type_selected
                    chips.append(_make_chip(dt, sel, lambda e, dtype=dt: _toggle_type(e, dtype)))
                dtype_chips_row.controls = chips
                with contextlib.suppress(Exception):
                    dtype_chips_row.update()

            def _toggle_all_types(e):
                type_selected.clear()
                _refresh_type_chips()
                _save_gsm_prefs()

            def _toggle_type(e, dtype: str):
                if dtype in type_selected:
                    type_selected.remove(dtype)
                else:
                    type_selected.append(dtype)
                _refresh_type_chips()
                _save_gsm_prefs()

            def _save_gsm_prefs(_=None):
                if not _mid:
                    return
                new_prefs = {
                    "enabled": bool(enabled_sw.value),
                    "brands": "all" if not brand_selected else list(brand_selected),
                    "device_types": "all" if not type_selected else list(type_selected),
                }
                self.db.set_gsm_parser_user_prefs(_mid, new_prefs)
                getattr(self, "_on_feature_toggle_changed", lambda: None)()

            enabled_sw.on_change = _save_gsm_prefs

            _refresh_brand_chips()
            _refresh_type_chips()

            controls = [
                ft.Text(
                    self.t("settings.gsm.description", "Налаштуйте персональне відображення GSM Parser — фільтри застосовуються автоматично у бічній панелі та при відкритті сторінки."),
                    size=12,
                    color=_th_muted,
                ),
                enabled_sw,
                ft.Text(self.t("settings.gsm.brands_label", "Бренди"), size=11, weight=ft.FontWeight.W_600, color=_th_text),
                brand_chips_row,
                ft.Text(self.t("settings.gsm.types_label", "Типи пристроїв"), size=11, weight=ft.FontWeight.W_600, color=_th_text),
                dtype_chips_row,
            ]

            return [
                build_collapsible_block(
                    self.t("settings.gsm.title", "GSM Parser"),
                    controls,
                    initially_open=False,
                )
            ]

        # ── Локал -> VPS upgrade (Фаза E) ───────────────────────────────────
        migrate_status_text = ft.Text("", size=12, color=_th_muted)
        migrate_progress = ft.ProgressRing(width=16, height=16, stroke_width=2, visible=False)
        migrate_btn = ft.FilledButton(
            "Перейти на VPS (хмарне сховище)",
            icon=ft.Icons.CLOUD_UPLOAD,
        )

        def _do_migrate_click(_):
            page = self.content_area.page if self.content_area else None
            if not page:
                return

            async def _run_async():
                migrate_btn.disabled = True
                migrate_progress.visible = True
                migrate_status_text.value = (
                    "Переносимо дані на хмарний сервер — це може зайняти кілька хвилин, "
                    "не закривайте програму..."
                )
                migrate_status_text.color = _th_muted
                page.update()
                try:
                    ok, err = await asyncio.to_thread(
                        self.db.migrate_company_local_to_vps, self.company_id
                    )
                except Exception as exc:
                    ok, err = False, str(exc)
                if ok:
                    self._show_snack("Дані успішно перенесено на VPS!", "green")
                    self._build_deferred()
                else:
                    migrate_btn.disabled = False
                    migrate_progress.visible = False
                    migrate_status_text.value = f"Не вдалося перенести: {err}"
                    migrate_status_text.color = ft.Colors.RED_700
                    page.update()

            page.run_task(_run_async)

        migrate_btn.on_click = _do_migrate_click

        # ── Ручний бекап / відновлення (Фаза G) ─────────────────────────────
        # Доступний незалежно від storage_mode (Локал чи VPS) — на відміну
        # від migrate_btn вище. Гейт той самий (can_manage_company_settings),
        # тож для звичайного співробітника компанії блок не показується.
        backup_status_text = ft.Text("", size=12, color=_th_muted)
        backup_progress = ft.ProgressRing(width=16, height=16, stroke_width=2, visible=False)

        def _pick_backup_dest_dir():
            try:
                import tkinter as tk
                from tkinter import filedialog

                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                path = filedialog.askdirectory(title="Оберіть теку для збереження бекапу")
                root.destroy()
                return path or ""
            except Exception:
                return ""

        def _pick_backup_file():
            try:
                import tkinter as tk
                from tkinter import filedialog

                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                path = filedialog.askopenfilename(
                    title="Оберіть файл бекапу",
                    filetypes=[("Backup files", "*.dump")],
                )
                root.destroy()
                return path or ""
            except Exception:
                return ""

        def _set_backup_controls_disabled(disabled: bool):
            export_btn.disabled = disabled
            import_btn.disabled = disabled
            backup_progress.visible = disabled

        def _do_export_click(_):
            page = self.content_area.page if self.content_area else None
            if not page:
                return
            dest_dir = _pick_backup_dest_dir()
            if not dest_dir:
                return

            async def _run_async():
                _set_backup_controls_disabled(True)
                backup_status_text.value = "Зберігаємо бекап..."
                backup_status_text.color = _th_muted
                page.update()
                try:
                    ok, result = await asyncio.to_thread(
                        self.db.export_company_backup,
                        self.company_id,
                        dest_dir,
                        self.user_data.get("active_manager_id") or self.user_data.get("id") or "",
                    )
                except Exception as exc:
                    ok, result = False, str(exc)
                _set_backup_controls_disabled(False)
                if ok:
                    backup_status_text.value = f"Бекап збережено: {result}"
                    backup_status_text.color = ft.Colors.GREEN_700
                    self._show_snack("Бекап успішно збережено!", "green")
                else:
                    backup_status_text.value = f"Не вдалося зберегти бекап: {result}"
                    backup_status_text.color = ft.Colors.RED_700
                page.update()

            page.run_task(_run_async)

        def _do_import_click(_):
            page = self.content_area.page if self.content_area else None
            if not page:
                return
            input_file = _pick_backup_file()
            if not input_file:
                return

            manifest = self.db.read_backup_manifest(input_file)

            def _run_restore():
                async def _run_async():
                    _set_backup_controls_disabled(True)
                    backup_status_text.value = (
                        "Відновлюємо дані з бекапу — не закривайте програму..."
                    )
                    backup_status_text.color = _th_muted
                    page.update()
                    try:
                        ok, err = await asyncio.to_thread(
                            self.db.import_company_backup,
                            self.company_id,
                            input_file,
                            self.user_data.get("active_manager_id") or self.user_data.get("id") or "",
                        )
                    except Exception as exc:
                        ok, err = False, str(exc)
                    if ok:
                        self._show_snack("Дані успішно відновлено з бекапу!", "green")
                        self._build_deferred()
                    else:
                        _set_backup_controls_disabled(False)
                        backup_status_text.value = f"Не вдалося відновити: {err}"
                        backup_status_text.color = ft.Colors.RED_700
                        page.update()

                page.run_task(_run_async)

            def _confirmed(_e=None):
                page.pop_dialog()
                _run_restore()

            warn_lines = [
                "Це ПЕРЕЗАПИШЕ всі поточні дані компанії даними з цього файлу. "
                "Дію неможливо скасувати."
            ]
            if manifest:
                warn_lines.append(
                    f"Бекап: {manifest.get('company_name', '—')} "
                    f"({manifest.get('created_at', '—')})"
                )
                if str(manifest.get("company_id") or "") != str(self.company_id or ""):
                    warn_lines.append("Увага: цей бекап належить ІНШОМУ акаунту!")

            confirm_dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text("Відновити з бекапу?"),
                content=ft.Container(width=380, content=ft.Text("\n".join(warn_lines))),
                actions=[
                    ft.TextButton(
                        self.t("common.cancel", "Скасувати"),
                        on_click=lambda _e: page.pop_dialog(),
                    ),
                    ft.FilledButton(
                        "Відновити",
                        icon=ft.Icons.RESTORE,
                        bgcolor=ft.Colors.RED_700,
                        color=ft.Colors.WHITE,
                        on_click=_confirmed,
                    ),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
            page.show_dialog(confirm_dlg)

        export_btn = ft.FilledButton(
            "Зберегти бекап", icon=ft.Icons.SAVE_ALT, on_click=_do_export_click
        )
        import_btn = ft.OutlinedButton(
            "Відновити з бекапу", icon=ft.Icons.RESTORE, on_click=_do_import_click
        )

        # --- ІНТЕРФЕЙС ---
        settings_content = ft.Column(
            controls=[
                ft.Row(
                    [
                        ft.Icon(ft.Icons.MANAGE_ACCOUNTS, size=26, color=_th_icon),
                        ft.Column(
                            [
                                ft.Text(
                                    "Налаштування профілю та компанії",
                                    size=22,
                                    weight="bold",
                                    color=_th_text,
                                ),
                                ft.Text(
                                    str(self.user_data.get("full_name") or self.user_data.get("email") or ""),
                                    size=12,
                                    color=_th_muted,
                                ),
                            ],
                            spacing=1,
                        ),
                        ft.IconButton(
                            icon=ft.Icons.HELP_OUTLINE,
                            icon_size=18,
                            icon_color=_th_icon,
                            tooltip=self.t("common.help.tooltip", "Довідка"),
                            on_click=lambda _e: self._show_help(),
                        ),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Text(
                    "Режим перегляду підлеглого: створення і редагування вимкнено",
                    size=12,
                    color=_th_muted,
                    visible=self.read_only_subordinate,
                ),
                build_collapsible_block(
                    "Розташування бази даних",
                    [
                        ft.Text(
                            (
                                f"{self.db_location.get('host')}:{self.db_location.get('port')}"
                                f"/{self.db_location.get('db_name')} — "
                                + (
                                    "VPS (хмара)"
                                    if self.db_location.get("storage_mode") == "vps"
                                    else "Локально"
                                )
                            )
                            if self.db_location
                            else "Не вдалося визначити адресу бази даних",
                            size=13,
                            weight="bold",
                            color=_th_text,
                            selectable=True,
                        ),
                        ft.Text(
                            "Це адреса саме цього пристрою. У локальному режимі кожен "
                            "співробітник має власну базу на своєму комп'ютері — на "
                            "інших пристроях адреса буде іншою.",
                            size=12,
                            color=_th_muted,
                            visible=self.storage_mode == "local",
                        ),
                    ],
                    initially_open=False,
                    visible=(
                        self.can_manage_company_settings and not self.read_only_subordinate
                    ),
                ),
                build_collapsible_block(
                    "Зберігання даних",
                    [
                        ft.Text(
                            "Дані зараз зберігаються локально на цьому пристрої. "
                            "Перенесіть їх у хмару (VPS), щоб мати доступ з будь-якого "
                            "пристрою й автоматичну синхронізацію. Локальні дані "
                            "залишаться на цьому комп'ютері як резервна копія.",
                            size=12,
                            color=_th_muted,
                        ),
                        ft.Row(
                            [migrate_btn, migrate_progress],
                            spacing=10,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        migrate_status_text,
                    ],
                    initially_open=True,
                    visible=(
                        self.can_manage_company_settings
                        and not self.read_only_subordinate
                        and self.storage_mode == "local"
                    ),
                ),
                build_collapsible_block(
                    "Резервна копія",
                    [
                        ft.Text(
                            "Збережіть копію даних на комп'ютер, флешку чи інший пристрій — "
                            "і зможете відновити їх звідти, якщо щось піде не так. Робиться "
                            "вручну, займає кілька секунд.",
                            size=12,
                            color=_th_muted,
                        ),
                        ft.Row(
                            [export_btn, import_btn, backup_progress],
                            spacing=10,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        backup_status_text,
                    ],
                    initially_open=False,
                    visible=(
                        self.can_manage_company_settings and not self.read_only_subordinate
                    ),
                ),
                # ── Company setup — in the order a first-time admin needs to
                # do them: regions are the foundation everything else below
                # is scoped to (price tiers, delivery methods, payment
                # templates, and the region-access checkboxes when creating
                # users all reference a region that must already exist).
                build_collapsible_block(
                    "Регіони",
                    [
                        ft.Text(
                            "Керування регіонами компанії. Блок не залежить від поточного вибору контексту.",
                            size=12,
                            color=_th_muted,
                        ),
                        ft.OutlinedButton(
                            "Створити регіон",
                            icon=ft.Icons.ADD_LOCATION,
                            on_click=_add_region_from_settings,
                            visible=self.can_manage_company_settings
                            and not self.read_only_subordinate,
                        ),
                        *regions_manage_controls,
                    ],
                    initially_open=False,
                    visible=self.can_manage_company_settings
                    and not self.read_only_subordinate,
                ),
                build_collapsible_block(
                    "Дефолтні рівні по регіонах",
                    [
                        ft.Text(
                            "Натисніть на регіон для редагування дефолтних рівнів:",
                            size=12,
                            color=_th_muted,
                        ),
                        *(
                            [
                                ft.TextButton(
                                    content=ft.Column(
                                        [
                                            ft.Text(
                                                reg.get("name", "Регіон"),
                                                weight="bold",
                                                size=12,
                                            ),
                                            ft.Text(
                                                ", ".join(
                                                    [
                                                        f"{(lvl.get('title') or ('Рівень ' + str(lvl.get('position') or '')))} ({'ON' if lvl.get('enabled') else 'OFF'})"
                                                        for lvl in (
                                                            self.db.get_company_catalog_config(
                                                                self.company_id,
                                                                reg.get("id"),
                                                            ).get(
                                                                "levels"
                                                            )
                                                            or []
                                                        )
                                                        if isinstance(lvl, dict)
                                                    ]
                                                )
                                                or "Немає рівнів",
                                                size=11,
                                                color=_th_muted,
                                            ),
                                        ],
                                        spacing=2,
                                    ),
                                    on_click=lambda _, r=reg: _open_edit_region_hierarchy_dialog(
                                        r
                                    ),
                                )
                                for reg in regions_raw
                                if isinstance(reg, dict) and reg.get("id")
                            ]
                            if regions_raw
                            else [ft.Text("Немає регіонів", size=12, color=_th_muted)]
                        ),
                    ],
                    initially_open=False,
                    visible=self.can_manage_company_settings
                    and not self.read_only_subordinate,
                ),
                build_collapsible_block(
                    "Сегментація цін",
                    [
                        ft.Text(
                            "Натисніть на регіон нижче, щоб відкрити редагування рівнів цін у модальному вікні.",
                            size=12,
                            color=_th_muted,
                        ),
                        ft.Text(
                            (
                                "Редагування недоступне в режимі віддаленого перегляду"
                                if not self.can_edit_pricing_settings
                                else ""
                            ),
                            size=12,
                            color=_th_muted,
                            visible=not self.can_edit_pricing_settings,
                        ),
                        *regions_prices_controls,
                    ],
                    initially_open=False,
                ),
                build_collapsible_block(
                    "Способи доставки компанії",
                    [
                        ft.Text(
                            "Налаштовуються окремо для кожного регіону. Доступні для "
                            "всіх користувачів цього регіону; користувач може "
                            "перевизначити свою ціну за кг.",
                            size=12,
                            color=_th_muted,
                        ),
                        *delivery_region_blocks,
                    ],
                    initially_open=False,
                    visible=True,
                ),
                build_collapsible_block(
                    "Платіжні поля постачальників (компанія)",
                    [
                        ft.Text(
                            "Налаштовуються окремо для кожного регіону — визначте, "
                            "які платіжні поля можна використовувати у постачальниках.",
                            size=12,
                            color=_th_muted,
                        ),
                        *payment_templates_region_blocks,
                    ],
                    initially_open=False,
                    visible=self.can_manage_company_settings
                    and not self.read_only_subordinate,
                ),
                build_collapsible_block(
                    "Користувачі компанії",
                    [
                        ft.Text("Створити користувача", weight="bold", size=13),
                        ft.Row([user_full_name_f, user_role_f], spacing=10, wrap=True),
                        ft.Row([user_email_f, user_phone_f], spacing=10, wrap=True),
                        ft.Row([user_password_f, user_parent_f], spacing=10, wrap=True),
                        ft.Text(
                            "Доступні регіони (мультивибір)", size=12, weight="bold"
                        ),
                        user_regions_checks_host,
                        ft.Text(
                            "Доступи до каталогу (мультивибір)", size=12, weight="bold"
                        ),
                        user_catalog_checks_host,
                        ft.FilledButton(
                            "Створити користувача",
                            icon=ft.Icons.PERSON_ADD,
                            on_click=create_user_click,
                        ),
                        ft.Divider(),
                        ft.Text(
                            "Список користувачів і редагування доступів",
                            weight="bold",
                            size=13,
                        ),
                        _build_users_table(),
                    ],
                    initially_open=False,
                    visible=self.can_manage_company_settings
                    and not self.read_only_subordinate,
                ),
                # ── Personal preferences — same for every role, come after
                # company setup since they don't block anything else.
                build_collapsible_block(
                    "Особисті дані",
                    [
                        manager_name,
                        manager_email,
                        manager_phone,
                        ft.FilledButton(
                            "Зберегти профіль",
                            icon=ft.Icons.SAVE,
                            on_click=_save_profile,
                            disabled=self.read_only_subordinate,
                        ),
                        ft.Divider(),
                        ft.Text("Зміна пароля", size=12, color=_th_muted),
                        ft.Row([old_password, new_password], expand=True),
                        ft.FilledButton(
                            "Змінити пароль",
                            icon=ft.Icons.LOCK_RESET,
                            on_click=_save_password,
                            disabled=self.read_only_subordinate,
                        ),
                    ]
                    + (
                        []
                        if self.can_manage_company_settings
                        else [
                            ft.Divider(),
                            work_orders_cb,
                            ft.Text(
                                "Якщо вимкнено — у всіх вкладках завантажуються дані підлеглих, "
                                "обраних на Головній (а не ваші власні замовлення).",
                                size=11,
                                color=_th_muted,
                            ),
                        ]
                    ),
                    initially_open=False,
                ),
                build_collapsible_block(
                    "Мої способи доставки",
                    [
                        ft.Text(
                            "Вмикайте або вимикайте способи доставки для себе. Вимкнуті не з'являтимуться у замовленнях та розрахунках. Ціну залиште порожньою — використовується базова ціна компанії.",
                            size=12,
                            color=_th_muted,
                        ),
                        (
                            ft.Column(
                                [
                                    ft.Column(
                                        [
                                            row["enabled_cb"],
                                            ft.Row(
                                                [
                                                    row["price_f"],
                                                    row["currency_f"],
                                                    row["term_f"],
                                                ],
                                                spacing=8,
                                                wrap=True,
                                            ),
                                        ],
                                        spacing=4,
                                    )
                                    for row in manager_delivery_rows
                                ],
                                spacing=8,
                            )
                            if manager_delivery_rows
                            else ft.Text(
                                "Немає увімкнених способів доставки",
                                color=_th_muted,
                                size=12,
                            )
                        ),
                        ft.Row(
                            [manager_prod_label, manager_prod_days_f],
                            spacing=8,
                            wrap=True,
                        ),
                    ],
                    initially_open=False,
                ),
                build_collapsible_block(
                    "Платіжні поля постачальників",
                    [
                        ft.Text(
                            "Оберіть, які платіжні поля вам потрібні у постачальниках, та як вони мають називатися.",
                            size=12,
                            color=_th_muted,
                        ),
                        (
                            payment_fields_container
                            if payment_override_rows
                            else ft.Text(
                                "У компанії не налаштовано платіжні поля",
                                color=_th_muted,
                                size=12,
                            )
                        ),
                    ],
                    initially_open=False,
                    visible=True,
                ),
                *_build_gsm_parser_block(),
                build_collapsible_block(
                    "Тема оформлення",
                    [_build_theme_picker_controls()],
                    initially_open=False,
                ),
                build_collapsible_block(
                    "Персональний фон",
                    [
                        ft.Text(
                            "Зберігається локально у папці media/user_backgrounds",
                            size=12,
                            color=_th_muted,
                        ),
                        ft.Text(
                            (
                                f"Поточний файл: {os.path.basename(current_personal_bg)}"
                                if current_personal_bg
                                else "Поточний файл: не встановлено"
                            ),
                            size=12,
                            color=_th_muted,
                        ),
                        ft.Row(
                            [
                                ft.FilledButton(
                                    "Завантажити",
                                    icon=ft.Icons.UPLOAD_FILE,
                                    on_click=upload_personal_background,
                                    disabled=self.read_only_subordinate,
                                ),
                                ft.OutlinedButton(
                                    "Очистити",
                                    icon=ft.Icons.DELETE_OUTLINE,
                                    on_click=clear_personal_background,
                                    disabled=self.read_only_subordinate
                                    or not bool(current_personal_bg),
                                ),
                            ],
                            spacing=8,
                            wrap=True,
                        ),
                        ft.Text(
                            "Фон встановлюється індивідуально для поточного користувача.",
                            size=12,
                            color=_th_muted,
                        ),
                    ],
                    initially_open=False,
                ),
                ft.Divider(height=10, color="transparent"),
                ft.Container(
                    content=ft.FilledButton(
                        "ЗБЕРЕГТИ ВСІ ЗМІНИ",
                        icon=ft.Icons.SAVE,
                        on_click=handle_update,
                        height=50,
                        width=400,
                        disabled=self.read_only_subordinate,
                    ),
                    alignment=ft.Alignment(0, 0),
                ),
                ft.Divider(height=20, color="transparent"),
            ],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
            spacing=20,
        )

        self.content_area.controls.append(settings_content)
        if self.read_only_subordinate:
            manager_name.disabled = True
            manager_email.disabled = True
            manager_phone.disabled = True
            old_password.disabled = True
            new_password.disabled = True
        if not self.can_manage_company_settings:
            for card in regions_list_container.controls:
                if not isinstance(card, ft.Card) or not getattr(card, "data", None):
                    continue
                name_input = card.data.get("name")
                if isinstance(name_input, ft.TextField):
                    name_input.disabled = True
                prices_container = card.data.get("prices_container")
                if isinstance(prices_container, ft.Column):
                    for p_row in prices_container.controls:
                        if not isinstance(p_row, ft.Row) or not getattr(
                            p_row, "data", None
                        ):
                            continue
                        p_name = p_row.data.get("name_input")
                        p_curr = p_row.data.get("curr_input")
                        if isinstance(p_name, ft.TextField):
                            p_name.disabled = True
                        if isinstance(p_curr, ft.Dropdown):
                            p_curr.disabled = True
        self.content_area.update()

    def _show_help(self) -> None:
        from views.help_utils import show_modal_help
        page = getattr(self.content_area, "page", None)
        show_modal_help(page, self.t("settings.help.title", "Налаштування"), [
            self.t(
                "settings.help.desc",
                "Блоки нижче йдуть у тому порядку, в якому їх варто заповнювати "
                "під час першого запуску: спершу — основа компанії (регіони, "
                "ціни, доставка, оплата, користувачі), потім — особисті "
                "налаштування, які не блокують нічого іншого.",
            ),
            (self.t("settings.help.steps_title", "Налаштування компанії (спочатку)"), None),
            self.t("settings.help.step1", "1. «Регіони» — створіть регіони, в яких працює компанія. Це основа: усе нижче прив'язане до регіону."),
            self.t("settings.help.step2", "2. «Дефолтні рівні по регіонах» — налаштуйте ієрархію категорій для кожного регіону."),
            self.t("settings.help.step3", "3. «Сегментація цін» — задайте рівні цін (наприклад, роздріб/опт) для кожного регіону."),
            self.t("settings.help.step4", "4. «Способи доставки компанії» — додайте способи доставки та базові тарифи."),
            self.t("settings.help.step5", "5. «Платіжні поля постачальників (компанія)» — визначте, які платіжні поля можна використовувати у постачальниках."),
            self.t("settings.help.step6", "6. «Користувачі компанії» — створюйте акаунти співробітників і одразу призначайте їм доступні регіони та розділи каталогу."),
            (self.t("settings.help.personal_title", "Особисті налаштування (будь-коли)"), None),
            self.t("settings.help.step7", "7. «Особисті дані», «Мої способи доставки», «Платіжні поля постачальників», GSM Parser, «Тема оформлення», «Персональний фон» — стосуються лише вашого акаунта, налаштуйте у будь-який момент."),
            self.t("settings.help.step8", "8. Натисніть «ЗБЕРЕГТИ ВСІ ЗМІНИ» внизу сторінки, щоб застосувати зміни в цих блоках."),
        ])

    def _show_snack(self, text, color):
        self.content_area.page.snack_bar = ft.SnackBar(
            ft.Text(text, color=ft.Colors.WHITE, weight="bold"),
            bgcolor=color,
            duration=3000,
        )
        self.content_area.page.snack_bar.open = True
        self.content_area.page.update()

    def _close_dialog(self, dlg):
        page = self.content_area.page if self.content_area else None
        if not page:
            logger.warning("[modal:settings] close skipped: page is None")
            return
        logger.info(
            "[modal:settings] close:start dlg=%s@%s open=%s overlay_before=%s",
            type(dlg).__name__,
            id(dlg),
            getattr(dlg, "open", None),
            len(getattr(page, "overlay", []) or []),
        )
        page.pop_dialog()
        logger.info(
            "[modal:settings] close:finish dlg=%s@%s open=%s overlay_after=%s",
            type(dlg).__name__,
            id(dlg),
            getattr(dlg, "open", None),
            len(getattr(page, "overlay", []) or []),
        )
