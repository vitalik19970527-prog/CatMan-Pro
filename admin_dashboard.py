import asyncio
import contextlib
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime

import flet as ft

import i18n
from config import Config
from dashboard import MainDashboard
from database import DataManager
from logger import logger
from views import themes as _themes
from views.calculators_view import open_calculators_modal
from views.categories_brands_view import CategoriesBrandsView
from views.copy_setup_view import CopySetupView
from views.extensions_view import ExtensionsView
from views.home_view import HomeView
from views.notebook_view import get_notebook_badge_counts, open_notebook_modal
from views.packaging_view import PackagingView
from views.settings_view import SettingsView
from views.suppliers_view import SuppliersView

try:
    import tkinter as tk
    from tkinter import filedialog

    TK_AVAILABLE = True
except Exception:
    TK_AVAILABLE = False


ALL_COMPANIES_KEY = "__all_companies__"
ALL_REGIONS_KEY = "__all_regions__"


def _delete_company_local_files(company_id: str) -> None:
    """Delete all local JSON cache/outbox files that belong to this company.

    Views store snapshots and outbox files named like:
      orders_snapshot_{cid}_*.json, settlements_{cid}_*.json,
      payments_outbox_{cid}_*.json, cost_price_workspace_outbox_{cid}_*.json, …
    Several base dirs are used inconsistently across the codebase (FLET_APP_STORAGE_DATA,
    %APPDATA%\\CatManPro, %LOCALAPPDATA%\\CatManPro, ~/.catmanpro) — check them all.
    """
    import glob as _glob

    safe_cid = (company_id or "").replace("/", "_").replace("\\", "_")
    if not safe_cid:
        return
    base_dirs = []
    flet_dir = os.getenv("FLET_APP_STORAGE_DATA")
    if flet_dir:
        base_dirs.append(flet_dir)
    appdata = os.getenv("APPDATA")
    if appdata:
        base_dirs.append(os.path.join(appdata, "CatManPro"))
    # cost_price_orders_table.py uses %LOCALAPPDATA%\CatManPro (not %APPDATA%) —
    # its outbox/snapshot files would otherwise never get cleaned up.
    localappdata = os.getenv("LOCALAPPDATA")
    if localappdata:
        base_dirs.append(os.path.join(localappdata, "CatManPro"))
    home_dir = os.path.join(os.path.expanduser("~"), ".catmanpro")
    base_dirs.append(home_dir)

    deleted = 0
    for base in base_dirs:
        if not os.path.isdir(base):
            continue
        pattern = os.path.join(base, f"*_{safe_cid}_*.json")
        for fpath in _glob.glob(pattern):
            with contextlib.suppress(Exception):
                os.remove(fpath)
                deleted += 1
    logger.info("Deleted %d local cache files for company %s", deleted, company_id)


class AdminDashboard:
    def __init__(self, page, user_data, on_logout=None, on_switch_account=None):
        user_data = user_data if isinstance(user_data, dict) else {}
        company_id = user_data.get("company_id")
        self.page = page
        self.db = DataManager()
        self.user_data = user_data
        self.t = i18n.bind((self.user_data.get("id") or "").strip())
        self.on_logout = on_logout
        self.on_switch_account = on_switch_account
        self.allow_all_companies = False
        self.locked_company_id = company_id
        if self.locked_company_id:
            self.db.set_company(
                self.locked_company_id,
                user_data.get("company_name", ""),
                manager_id=user_data.get("id", ""),
            )
        self.dashboard_title = "Адмін"
        self.current_tab = "overview"
        self.selected_company_id = company_id
        self.selected_region_name = None
        self._admin_create_user_open = False
        self._catalog_sync_warmup_done = set()
        self._company_sections_state = {}
        self.currencies_list = ["USD", "UAH", "EUR", "PLN", "CNY"]
        self.content_area = ft.Column(expand=True, scroll=ft.ScrollMode.ADAPTIVE)
        self._sync_poll_started = False
        self._sync_last_pulses = {}
        self._sync_dirty_scopes = set()
        self._last_sa_sync_pulse_reload_mono = 0.0
        self._pending_sa_sync_pulse_reload = False
        self._logout_in_progress = False
        self._admin_header_ref = None
        self._admin_modules_row_ref = None

    # ── nav-icon helper ───────────────────────────────────────────────────────
    def _admin_nav_btn(self, text: str, nav_key: str, default_icon, on_click, height: int = 36):
        mid = (self.user_data.get("id") or "").strip()
        pack = _themes.get_active_nav_pack(mid)
        pack_type = pack.get("type", "material")
        raw_icon = pack.get(nav_key, default_icon) if nav_key else default_icon
        if pack_type == "text":
            glyph = raw_icon if isinstance(raw_icon, str) else "•"
            return ft.FilledButton(
                content=ft.Row(
                    [ft.Text(glyph, size=14), ft.Text(text, size=12)],
                    spacing=4, tight=True,
                ),
                on_click=on_click,
                height=height,
            )
        return ft.FilledButton(text, icon=raw_icon, on_click=on_click, height=height)

    def _rebuild_admin_nav_buttons(self) -> None:
        row = self._admin_modules_row_ref
        if row is None:
            return
        h = 36
        row.controls = [
            ft.FilledButton(self.t("nav.approvals", "Підтвердити заявки"), icon=ft.Icons.VERIFIED_USER, on_click=self.show_approvals, height=h),
            ft.FilledButton(self.t("nav.users", "Користувачі"), icon=ft.Icons.GROUP, on_click=self.show_users, height=h),
            self._admin_nav_btn(self.t("nav.company", "Компанія"), "company", ft.Icons.BUSINESS, self.show_company_settings, h),
            self._admin_nav_btn(self.t("nav.categories", "Категорії"), "categories", ft.Icons.CATEGORY, self.show_categories, h),
            self._admin_nav_btn(self.t("nav.suppliers", "Постачальники"), "suppliers", ft.Icons.LOCAL_SHIPPING, self.show_suppliers, h),
            self._admin_nav_btn(self.t("nav.packaging", "Пакування"), "packaging", ft.Icons.INVENTORY_2, self.show_packaging, h),
            self._admin_nav_btn(self.t("nav.copy", "Копіювання"), "copy", ft.Icons.CONTENT_COPY, self.show_copy_setup, h),
            self._admin_nav_btn(self.t("nav.extensions", "Розширення"), "extensions", ft.Icons.EXTENSION, self.show_extensions, h),
            self._admin_nav_btn(self.t("nav.settings_tooltip", "Налаштування"), "settings", ft.Icons.SETTINGS, self.show_infrastructure_settings, h),
            ft.FilledButton(self.t("nav.bulk_template", "Товар по шаблону"), icon=ft.Icons.TABLE_CHART, on_click=self.open_bulk_product_template, height=h),
        ]
        with contextlib.suppress(Exception):
            row.update()

    def _show_nav_help(self) -> None:
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(self.t("nav.help_title", "Довідка — навігація")),
            content=ft.Container(
                width=420,
                content=ft.Text(
                    self.t(
                        "nav.help_body_admin",
                        "Вгорі — кнопки розділів адміністрування (заявки, користувачі,"
                        " категорії, постачальники, пакування, копіювання, розширення,"
                        " налаштування, товар по шаблону). Праворуч — вибір компанії/"
                        " регіону, кнопка «Огляд» повертає на головний екран, є"
                        " перемикач мови та кнопка виходу.",
                    ),
                    size=13,
                ),
            ),
            actions=[
                ft.FilledButton(
                    self.t("common.help.close", "Зрозуміло"),
                    on_click=lambda _e: self.page.pop_dialog(),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _build_admin_lang_switcher_box(self, manager_id: str, bg_color: str) -> ft.Container:
        box = i18n.build_language_switcher(
            self.db,
            lambda code: self._apply_admin_language_live(code),
            manager_id=manager_id,
            bg_color=bg_color,
        )
        self._admin_lang_switcher_box_ref = box
        return box

    def _apply_admin_language_live(self, lang_code: str) -> None:
        mid = (self.user_data.get("id") or "").strip()
        i18n.save_manager_language(self.db, mid, lang_code)
        try:
            _live_th = _themes.get_active_theme(mid)
            box = getattr(self, "_admin_lang_switcher_box_ref", None)
            if box is not None:
                fresh = i18n.build_language_switcher(
                    self.db,
                    lambda code: self._apply_admin_language_live(code),
                    manager_id=mid,
                    bg_color=_live_th.get("chip_bg"),
                )
                box.content = fresh.content
                box.update()
        except Exception:
            pass
        self.build()

    def _apply_admin_theme_live(self, theme_key: str) -> None:
        mid = (self.user_data.get("id") or "").strip()
        _themes.set_active_theme(mid, theme_key)
        _live_th = _themes.get_active_theme(mid)
        bar_color = _themes.get_bar_color(_live_th)
        if self._admin_header_ref is not None:
            self._admin_header_ref.bgcolor = bar_color
            with contextlib.suppress(Exception):
                self._admin_header_ref.update()
        try:
            self.page.bgcolor = _live_th["bg"]
            _live_dark = _themes._is_dark(_live_th["bg"])
            self.page.theme_mode = (
                ft.ThemeMode.DARK if _live_dark else ft.ThemeMode.LIGHT
            )
            if _live_dark:
                self.page.dark_theme = ft.Theme(
                    color_scheme=ft.ColorScheme(
                        on_surface=_live_th["text"],
                        on_background=_live_th["text"],
                        surface=_live_th["panel"],
                    ),
                )
            self.page.update()
        except Exception:
            pass

    def _apply_admin_nav_live(self, pack_key: str) -> None:
        mid = (self.user_data.get("id") or "").strip()
        _themes.set_active_nav_pack(mid, pack_key)
        self._rebuild_admin_nav_buttons()

    def build(self):
        self.page.clean()
        self.page.bgcolor = ft.Colors.WHITE
        self.page.padding = ft.padding.all(0)

        _admin_mgr_id = (self.user_data.get("id") or "").strip()
        try:
            _ws = self.db.get_manager_workspace_state(_admin_mgr_id) or {}
            _themes.set_active_theme(_admin_mgr_id, _ws.get("ui_theme"))
            _themes.set_active_nav_pack(_admin_mgr_id, _ws.get("ui_nav_pack"))
            i18n.set_active_language(_admin_mgr_id, _ws.get("ui_language"))
        except Exception:
            pass
        self.dashboard_title = self.t("nav.admin_title", "Адмін")
        _active_theme = _themes.get_active_theme(_admin_mgr_id)
        _bar_color = _themes.get_bar_color(_active_theme)
        _th_muted = _active_theme["muted"]
        _th_text = _active_theme["text"]
        _th_bg = _active_theme["bg"]
        _th_panel = _active_theme["panel"]
        self.page.bgcolor = _th_bg
        _is_dark_mode = _themes._is_dark(_th_bg)
        self.page.theme_mode = (
            ft.ThemeMode.DARK if _is_dark_mode else ft.ThemeMode.LIGHT
        )
        if _is_dark_mode:
            try:
                self.page.dark_theme = ft.Theme(
                    color_scheme=ft.ColorScheme(
                        on_surface=_th_text,
                        on_background=_th_text,
                        surface=_th_panel,
                    ),
                )
            except Exception:
                pass

        current_email = (self.user_data.get("email") or "").strip()
        self._upsert_saved_account(
            "",
            self.user_data.get("full_name", ""),
            current_email,
            self.user_data.get("phone", ""),
            role=self.user_data.get("role", ""),
            manager_id=self.user_data.get("id", ""),
            company_id=self.user_data.get("company_id", ""),
        )

        companies = self._visible_companies()
        if self._is_company_scoped():
            self.selected_company_id = self._scope_company_id()
        else:
            if self.selected_company_id not in {None, ALL_COMPANIES_KEY}:
                exists = any(
                    isinstance(c, dict) and c.get("id") == self.selected_company_id
                    for c in companies
                )
                if not exists:
                    self.selected_company_id = None
            if self.selected_company_id is None:
                self.selected_company_id = ALL_COMPANIES_KEY

        company_options = []
        if self.allow_all_companies:
            company_options.append(
                ft.dropdown.Option(key=ALL_COMPANIES_KEY, text=self.t("nav.all_companies", "Всі компанії"))
            )
        company_options.extend(
            [
                ft.dropdown.Option(key=c.get("id"), text=c.get("name"))
                for c in companies
                if isinstance(c, dict)
            ]
        )

        def _region_options(company_id):
            if company_id == ALL_COMPANIES_KEY and self.allow_all_companies:
                return []
            rows = self.db.get_company_regions(company_id)
            return [
                r for r in rows if isinstance(r, dict) and r.get("id") and r.get("name")
            ]

        def _sync_selected_region(company_id):
            if company_id == ALL_COMPANIES_KEY and self.allow_all_companies:
                self.selected_region_name = ALL_REGIONS_KEY
                return []
            region_rows = _region_options(company_id)
            region_names = [r.get("name") for r in region_rows]
            allowed_regions = (
                ([ALL_REGIONS_KEY] + region_names)
                if len(region_names) > 1
                else region_names
            )
            if allowed_regions and self.selected_region_name not in allowed_regions:
                self.selected_region_name = allowed_regions[0]
            if not region_names:
                self.selected_region_name = None
            return region_rows

        region_rows_initial = _sync_selected_region(self.selected_company_id)

        def _refresh_region_dropdown():
            rows = _sync_selected_region(self.selected_company_id)
            if (
                self.selected_company_id == ALL_COMPANIES_KEY
                and self.allow_all_companies
            ):
                region_dd.options = [
                    ft.dropdown.Option(key=ALL_REGIONS_KEY, text="Всі регіони")
                ]
                region_dd.value = ALL_REGIONS_KEY
                region_dd.visible = False
                return
            region_options = [
                ft.dropdown.Option(key=r.get("name"), text=r.get("name")) for r in rows
            ]
            if len(region_options) > 1:
                region_dd.options = [
                    ft.dropdown.Option(key=ALL_REGIONS_KEY, text="Всі регіони")
                ]
                region_dd.options.extend(region_options)
            else:
                region_dd.options = region_options
            region_dd.value = self.selected_region_name
            region_dd.visible = len(region_options) >= 1

        def on_company_change(e):
            if self._is_company_scoped():
                self.selected_company_id = self._scope_company_id()
                _refresh_region_dropdown()
                self._show_current_tab()
                return
            self.selected_company_id = (
                getattr(getattr(e, "control", None), "value", None) or company_dd.value
            )
            if (
                self.selected_company_id == ALL_COMPANIES_KEY
                and self.allow_all_companies
            ):
                self.selected_region_name = ALL_REGIONS_KEY
                self.db.set_company("")
            elif (
                self.selected_company_id
                and self.selected_company_id != ALL_COMPANIES_KEY
            ):
                # Find company name from the list to build a readable schema name
                _cname = next(
                    (
                        c.get("name", "")
                        for c in companies
                        if isinstance(c, dict)
                        and c.get("id") == self.selected_company_id
                    ),
                    "",
                )
                self.db.set_company(self.selected_company_id, _cname)
            _refresh_region_dropdown()
            self._show_current_tab()

        def on_region_change(e):
            self.selected_region_name = (
                getattr(getattr(e, "control", None), "value", None) or region_dd.value
            )
            self._show_current_tab()

        def _bind_dropdown_handler(dropdown, handler):
            # Compatibility for different Flet versions: some expose on_select, others on_change.
            try:
                setattr(dropdown, "on_change", handler)
            except Exception:
                pass
            try:
                setattr(dropdown, "on_select", handler)
            except Exception:
                logger.exception("Failed to bind dropdown handler")

        _dd_accent = _active_theme.get("accent", "#3B82F6")
        _dd_border = _active_theme.get("border", "#CBD5E1")
        _dd_icon_color = _active_theme.get("icon_color", _dd_accent)

        company_dd = ft.Dropdown(
            label=self.t("nav.company_label", "Компанія"),
            width=320,
            border_radius=10,
            color=_th_text,
            filled=True,
            fill_color=_th_panel,
            border_color=_dd_border,
            focused_border_color=_dd_accent,
            border_width=1,
            focused_border_width=2,
            content_padding=ft.padding.symmetric(horizontal=14, vertical=8),
            label_style=ft.TextStyle(color=_th_muted, size=12),
            text_style=ft.TextStyle(color=_th_text, weight="w500"),
            leading_icon=ft.Icon(ft.Icons.APARTMENT_ROUNDED, color=_dd_icon_color, size=18),
            options=company_options,
            value=self.selected_company_id,
            visible=not self._is_company_scoped(),
        )
        _bind_dropdown_handler(company_dd, on_company_change)

        region_dd = ft.Dropdown(
            label=self.t("nav.region_label", "Регіон"),
            width=260,
            border_radius=10,
            color=_th_text,
            filled=True,
            fill_color=_th_panel,
            border_color=_dd_border,
            focused_border_color=_dd_accent,
            border_width=1,
            focused_border_width=2,
            content_padding=ft.padding.symmetric(horizontal=14, vertical=8),
            label_style=ft.TextStyle(color=_th_muted, size=12),
            text_style=ft.TextStyle(color=_th_text, weight="w500"),
            leading_icon=ft.Icon(ft.Icons.PLACE_ROUNDED, color=_dd_icon_color, size=18),
            options=(
                (
                    [ft.dropdown.Option(key=ALL_REGIONS_KEY, text=self.t("nav.all_regions", "Всі регіони"))]
                    + [
                        ft.dropdown.Option(key=r.get("name"), text=r.get("name"))
                        for r in region_rows_initial
                    ]
                )
                if len(region_rows_initial) > 1
                else [
                    ft.dropdown.Option(key=r.get("name"), text=r.get("name"))
                    for r in region_rows_initial
                ]
            ),
            value=self.selected_region_name,
            visible=len(region_rows_initial) >= 1,
        )
        _bind_dropdown_handler(region_dd, on_region_change)

        # IMPORTANT: keep UI stable; only refresh options from DB on open/focus.
        # This ensures newly created regions appear immediately without page navigation.
        def _refresh_region_on_open(_e=None):
            try:
                _refresh_region_dropdown()
                region_dd.update()
            except Exception:
                pass

        # Compatibility across Flet versions: some expose on_focus, some on_click.
        try:
            setattr(region_dd, "on_focus", _refresh_region_on_open)
        except Exception:
            pass
        try:
            setattr(region_dd, "on_click", _refresh_region_on_open)
        except Exception:
            pass

        def _mark_manual_logout_intent(reason: str):
            try:
                import time as _time

                self.page._manual_logout_intent_reason = str(reason or "").strip()
                self.page._manual_logout_intent_ts = float(_time.time())
            except Exception:
                pass

        def do_logout(_, reason="unknown"):
            if self._logout_in_progress:
                return
            allowed_reasons = {"logout_button_click", "account_switch_confirmed"}
            if reason not in allowed_reasons:
                logger.error(
                    "[sa-logout] blocked unexpected request | reason=%s | user_id=%s | tab=%s",
                    reason,
                    self.user_data.get("id"),
                    self.current_tab,
                )
                return
            self._logout_in_progress = True
            logger.warning(
                "[sa-logout] requested | reason=%s | user_id=%s | company_id=%s | tab=%s",
                reason,
                self.user_data.get("id"),
                self.user_data.get("company_id"),
                self.current_tab,
            )
            try:
                auth_token = str(uuid.uuid4())
                try:
                    self.page._auth_transition_token = auth_token
                except Exception:
                    pass
                if callable(self.on_logout):
                    try:
                        self.on_logout(reason=reason, transition_token=auth_token)
                    except TypeError:
                        self.on_logout()
                else:
                    self.page.clean()
                    self.page.add(
                        ft.Text(
                            self.t(
                                "nav.logout_done",
                                "Вихід виконано. Перезапустіть застосунок для входу.",
                            ),
                            color="blue",
                        )
                    )
                    self.page.update()
            except Exception:
                logger.exception("SuperAdmin logout failed")
            finally:
                self._logout_in_progress = False

        def _close_dialog(dialog):
            self.page.pop_dialog()

        def _confirm_switch_account(_=None):
            dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text(self.t("nav.switch_account_confirm_title", "Змінити акаунт?")),
                content=ft.Text(
                    self.t(
                        "nav.switch_account_confirm_body",
                        "Ви перейдете на екран авторизації. Поточну сесію буде завершено.",
                    )
                ),
                actions_alignment=ft.MainAxisAlignment.END,
                actions=[
                    ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=lambda __: _close_dialog(dlg)),
                    ft.FilledButton(
                        self.t("nav.go_to_login", "Перейти до входу"),
                        icon=ft.Icons.SWAP_HORIZ,
                        on_click=lambda __: (
                            _close_dialog(dlg),
                            _mark_manual_logout_intent("account_switch_confirmed"),
                            do_logout(__, reason="account_switch_confirmed"),
                        ),
                    ),
                ],
            )
            self.page.show_dialog(dlg)

        def open_account_chooser_dialog(_=None):
            logger.info(
                "[sa-acct-switch] redirecting to login screen instead of opening dashboard modal"
            )
            _confirm_switch_account(_)

        page_w = int(getattr(self.page, "width", 0) or 0)
        if page_w <= 0:
            win = getattr(self.page, "window", None)
            page_w = int(getattr(win, "width", 0) or 0) if win else 0
        is_small_header = bool(page_w > 0 and page_w < 980)

        title_size = 20 if is_small_header else 24
        subtitle_size = 11 if is_small_header else 12
        clock_size = 15 if is_small_header else 18
        date_size = 9 if is_small_header else 11
        button_h = 32 if is_small_header else 36

        try:
            company_dd.width = 220 if is_small_header else 320
        except Exception:
            pass
        try:
            region_dd.width = 170 if is_small_header else 260
        except Exception:
            pass

        top_left = ft.Column(
            [
                ft.Row(
                    [
                        ft.Text(self.dashboard_title, size=title_size, weight="bold", color=_th_text),
                        ft.IconButton(
                            icon=ft.Icons.HELP_OUTLINE,
                            icon_size=18,
                            icon_color=_th_text,
                            tooltip=self.t("nav.help_tooltip", "Довідка"),
                            on_click=lambda _e: self._show_nav_help(),
                        ),
                    ],
                    spacing=2,
                    tight=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Text(
                    self.user_data.get("full_name", ""),
                    size=subtitle_size,
                    color=_th_muted,
                ),
            ],
            spacing=2,
            tight=True,
        )

        _clock_text = ft.Text(
            datetime.now().strftime("%H:%M"), size=clock_size, weight="bold", color=_th_text
        )
        self._clock_text = _clock_text
        if not getattr(self, "_clock_loop_started", False):
            self._clock_loop_started = True
            self.page.run_task(self._clock_loop)

        top_center = ft.Column(
            [
                _clock_text,
                ft.Text(datetime.now().strftime("%d.%m.%Y"), size=date_size, color=_th_muted),
            ],
            spacing=1,
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )

        top_right = ft.Row(
            [
                ft.FilledButton(
                    self.t("nav.switch_account", "Змінити акаунт"),
                    icon=ft.Icons.SWAP_HORIZ,
                    on_click=open_account_chooser_dialog,
                    tooltip=self.t("nav.switch_account_tooltip", "Повернутись до екрана входу"),
                    height=button_h,
                ),
                company_dd,
                region_dd,
                ft.OutlinedButton(
                    "",
                    icon=ft.Icons.HOME,
                    icon_color=_th_text,
                    tooltip=self.t("nav.overview", "Огляд"),
                    on_click=self.show_overview,
                    height=button_h,
                    style=ft.ButtonStyle(color=_th_text),
                ),
                ft.OutlinedButton(
                    "",
                    icon=ft.Icons.CALCULATE,
                    icon_color=_th_text,
                    tooltip=self.t("nav.calculators", "Калькулятори"),
                    on_click=self._open_calculator,
                    height=button_h,
                    style=ft.ButtonStyle(color=_th_text),
                ),
                self._make_notebook_toolbar_btn(_th_text, button_h),
                self._build_admin_lang_switcher_box(_admin_mgr_id, _active_theme.get("chip_bg")),
                ft.OutlinedButton(
                    self.t("nav.logout_short", "Вийти"),
                    icon=ft.Icons.LOGOUT,
                    icon_color=_th_text,
                    on_click=lambda _: (
                        _mark_manual_logout_intent("logout_button_click"),
                        do_logout(_, reason="logout_button_click"),
                    ),
                    height=button_h,
                    style=ft.ButtonStyle(color=_th_text),
                ),
            ],
            spacing=8,
            run_spacing=8,
            wrap=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.END,
        )

        modules_row = ft.Row(
            [],
            spacing=8,
            wrap=True,
            run_spacing=8,
            alignment=ft.MainAxisAlignment.START,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        header = ft.Container(
            content=ft.Column(
                [
                    ft.ResponsiveRow(
                        [
                            ft.Container(col={"xs": 12, "md": 4}, content=top_left),
                            ft.Container(col={"xs": 12, "md": 2}, content=top_center),
                            ft.Container(col={"xs": 12, "md": 6}, content=top_right),
                        ],
                        columns=12,
                        spacing=8,
                        run_spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    modules_row,
                ],
                spacing=10,
                tight=True,
            ),
            padding=ft.padding.symmetric(
                horizontal=12,
                vertical=(8 if is_small_header else 10),
            ),
            bgcolor=_bar_color,
            border_radius=12,
            margin=ft.margin.only(bottom=10),
        )
        self._admin_header_ref = header
        self._admin_modules_row_ref = modules_row
        self._rebuild_admin_nav_buttons()

        self.page.add(
            self._wrap_with_background(
                ft.Column(
                    [
                        header,
                        ft.Container(
                            content=self.content_area,
                            expand=True,
                            bgcolor=_th_panel,
                            border_radius=12,
                            padding=15,
                        ),
                    ],
                    expand=True,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                )
            )
        )
        if not self._sync_poll_started:
            # Background cross-client polling removed — 1 менеджер = 1
            # категорія means nobody else can change what this admin is
            # looking at. Fresh data now only comes from Ctrl+F5
            # (_handle_ctrl_f5) or a normal tab switch.
            self._sync_last_pulses = self._load_sync_snapshot()
            self._sync_poll_started = True
        with contextlib.suppress(Exception):
            self.page.on_keyboard_event = self._on_keyboard_event
        self._show_current_tab()
        self.page.run_task(self._background_warmup)
        # Expose callback so Settings can refresh background without restart.
        try:
            setattr(self.page, "_refresh_background_now", self._refresh_background_now)
        except Exception:
            pass

    def _show_current_tab(self, *, from_sync: bool = False):
        if self.current_tab == "approvals":
            self.show_approvals(None)
            return
        if self.current_tab == "users":
            if from_sync and self._admin_create_user_open:
                return
            self.show_users(None)
            return
        if self.current_tab == "categories":
            self.show_categories(None)
            return
        if self.current_tab == "suppliers":
            self.show_suppliers(None)
            return
        if self.current_tab == "packaging":
            self.show_packaging(None)
            return
        if self.current_tab == "copy_setup":
            self.show_copy_setup(None)
            return
        if self.current_tab == "extensions":
            self.show_extensions(None)
            return
        if self.current_tab == "infrastructure":
            self.show_infrastructure_settings(None)
            return
        if self.current_tab == "company":
            self.show_company_settings(None)
            return
        if self.current_tab == "hierarchy":
            self.show_hierarchy(None)
            return
        if from_sync and self.current_tab == "overview":
            return
        self.show_overview(None)

    def _on_keyboard_event(self, e: ft.KeyboardEvent) -> None:
        try:
            if e.ctrl and str(e.key or "").strip() == "F5":
                self.page.run_task(self._handle_ctrl_f5)
        except Exception:
            pass

    async def _handle_ctrl_f5(self) -> None:
        def _snack(text: str, color: str = "#1D4ED8") -> None:
            with contextlib.suppress(Exception):
                self.page.snack_bar = ft.SnackBar(
                    content=ft.Text(text, color="#FFFFFF", weight="bold"),
                    bgcolor=color,
                    duration=2500,
                )
                self.page.snack_bar.open = True
                self.page.update()

        _snack("Перевірка оновлень з БД…")
        company_ids = self._sync_company_ids()
        all_scopes = self._sync_scopes()
        for cid in company_ids:
            for scope in all_scopes:
                with contextlib.suppress(Exception):
                    self.db.invalidate_scope_cache(cid, scope)
        with contextlib.suppress(Exception):
            self._show_current_tab()
        _snack("Дані оновлено з БД.", "#15803D")

    def _is_company_scoped(self):
        return not self.allow_all_companies and bool(self.locked_company_id)

    def _scope_company_id(self):
        return self.locked_company_id if self._is_company_scoped() else None

    def _visible_companies(self, force_refresh=False):
        companies = [
            c
            for c in self.db.get_all_companies(force_refresh=force_refresh)
            if isinstance(c, dict) and c.get("id")
        ]
        scoped_company_id = self._scope_company_id()
        if scoped_company_id:
            companies = [c for c in companies if c.get("id") == scoped_company_id]
        return companies

    def _filter_by_visible_companies(self, rows, company_key="company_id"):
        scoped_company_id = self._scope_company_id()
        if not scoped_company_id:
            return rows
        filtered = []
        for row in rows or []:
            if isinstance(row, dict) and row.get(company_key) == scoped_company_id:
                filtered.append(row)
        return filtered

    def _dialog_width(self, desired=620, min_width=320):
        try:
            page_w = getattr(self.page, "width", None)
            if not page_w:
                win = getattr(self.page, "window", None)
                page_w = getattr(win, "width", None) if win else None
            if isinstance(page_w, (int, float)) and page_w > 0:
                return max(min_width, min(desired, int(page_w * 0.92)))
        except Exception:
            pass
        return desired

    def _dialog_content_height(self, desired=560, min_height=260):
        try:
            page_h = getattr(self.page, "height", None)
            if not page_h:
                win = getattr(self.page, "window", None)
                page_h = getattr(win, "height", None) if win else None
            if isinstance(page_h, (int, float)) and page_h > 0:
                return max(min_height, min(desired, int(page_h * 0.72)))
        except Exception:
            pass
        return desired

    def _wide_checkbox_row(self, label, value=False, on_change=None):
        cb = ft.Checkbox(value=bool(value))
        text = ft.Text(label or "")

        def _toggle(_):
            cb.value = not bool(cb.value)
            if callable(on_change):
                with contextlib.suppress(Exception):
                    on_change(cb)
            if self.page:
                row.update()

        if callable(on_change):

            def _cb_changed(_):
                with contextlib.suppress(Exception):
                    on_change(cb)

            cb.on_change = _cb_changed

        row = ft.Container(
            content=ft.Row(
                [cb, text],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                tight=True,
            ),
            padding=ft.padding.symmetric(horizontal=8, vertical=8),
            border_radius=8,
            ink=True,
            on_click=_toggle,
        )
        return cb, row

    def _clear_field_errors(self, *controls):
        for control in controls:
            if control is not None and hasattr(control, "error_text"):
                control.error_text = None

    def _mark_required(self, control, message="Поле обов'язкове"):
        if control is not None and hasattr(control, "error_text"):
            control.error_text = message

    async def _background_warmup(self):
        await asyncio.sleep(0.05)
        try:
            companies = self._visible_companies()
            self.db.get_all_companies()
            target_company_id = (
                self.selected_company_id
                if self.selected_company_id not in {None, ALL_COMPANIES_KEY}
                else None
            )
            if not target_company_id and companies:
                target_company_id = companies[0].get("id")
            if target_company_id:
                self.db.set_company(target_company_id)
                self.db.get_company_managers(target_company_id)
                self.db.get_company_regions(target_company_id)
                self.db.get_company_categories(target_company_id, None)
                self.db.get_suppliers(target_company_id, None, manager_id=None)
                self.db.get_packaging(target_company_id, None, manager_id=None)
                self.db.get_products(target_company_id, None, manager_id=None)
                self.db.get_orders(target_company_id, None, limit=40, offset=0)
        except Exception:
            logger.exception("Superadmin warmup failed")

    def _sync_scopes(self):
        return [
            "catalog",
            "suppliers",
            "packaging",
            "products",
            "pricing",
            "users",
            "extensions",
            "company",
        ]

    def _sync_company_ids(self):
        visible = self._visible_companies()
        ids = [c.get("id") for c in visible if isinstance(c, dict) and c.get("id")]
        return ids

    def _sync_snapshot_file(self):
        user_id = (self.user_data.get("id") or "").strip()
        mode = "sa" if self.allow_all_companies else "admin"
        locked = (self.locked_company_id or "").strip()
        seed = f"{user_id}:{mode}:{locked}".encode("utf-8")
        suffix = hashlib.sha1(seed).hexdigest()[:12]
        return os.path.join(self._app_data_dir(), f"sync_pulses_{mode}_{suffix}.json")

    def _load_sync_snapshot(self):
        try:
            path = self._sync_snapshot_file()
            if not os.path.exists(path):
                return {}
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                return {}
            out = {}
            for k, v in payload.items():
                try:
                    out[str(k)] = int(v)
                except Exception:
                    continue
            return out
        except Exception:
            return {}

    def _save_sync_snapshot(self):
        try:
            path = self._sync_snapshot_file()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._sync_last_pulses, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _consume_scope_dirty(self, scope):
        if scope in self._sync_dirty_scopes:
            self._sync_dirty_scopes.discard(scope)
            return True
        return False

    def _sa_sync_reload_min_interval_s(self) -> float:
        return max(
            2.5,
            float(getattr(Config, "SYNC_VIEW_RELOAD_MIN_INTERVAL_SECONDS", 5.0) or 5.0),
        )

    def _on_sa_sync_pulse_needs_tab_reload(self) -> None:
        now = time.monotonic()
        min_iv = self._sa_sync_reload_min_interval_s()
        if now - self._last_sa_sync_pulse_reload_mono >= min_iv:
            self._last_sa_sync_pulse_reload_mono = now
            self._pending_sa_sync_pulse_reload = False
            self._show_current_tab(from_sync=True)
            return
        self._pending_sa_sync_pulse_reload = True

    def _flush_pending_sa_sync_pulse_reload_if_due(self) -> None:
        if not self._pending_sa_sync_pulse_reload:
            return
        now = time.monotonic()
        if (
            now - self._last_sa_sync_pulse_reload_mono
            < self._sa_sync_reload_min_interval_s()
        ):
            return
        self._pending_sa_sync_pulse_reload = False
        self._last_sa_sync_pulse_reload_mono = now
        self._show_current_tab(from_sync=True)

    def _invalidate_overview_cache(self):
        # Mark core scopes as dirty so dashboards refresh without navigation.
        # Used after approve/reject/delete operations.
        self._sync_dirty_scopes.update({"users", "company", "catalog", "pricing"})
        with contextlib.suppress(Exception):
            self._save_sync_snapshot()

    async def _clock_loop(self):
        while True:
            await asyncio.sleep(30)
            try:
                ct = getattr(self, "_clock_text", None)
                if ct is not None:
                    ct.value = datetime.now().strftime("%H:%M")
                    ct.update()
            except Exception:
                pass

    async def _sync_poll_loop(self):
        mode = str(getattr(Config, "SYNC_POLL_MODE", "eco") or "eco").strip().lower()
        post_change = max(
            2.0, float(getattr(Config, "SYNC_POST_CHANGE_POLL_SECONDS", 3.0) or 3.0)
        )
        min_live = max(
            1.25,
            float(getattr(Config, "SHARED_LIVE_SYNC_MIN_INTERVAL_SECONDS", 2.0) or 2.0),
        )
        if mode == "normal":
            fast_interval, idle_1, idle_2, idle_3 = 2.5, 4.0, 9.0, 16.0
        elif mode == "shared_live":
            live_interval = max(
                min_live,
                float(getattr(Config, "SHARED_LIVE_SYNC_INTERVAL_SECONDS", 8.0) or 8.0),
            )
            fast_interval = idle_1 = idle_2 = idle_3 = live_interval
        elif mode == "cache_only":
            fast_interval, idle_1, idle_2, idle_3 = 3600.0, 3600.0, 3600.0, 3600.0
        else:
            fast_interval = float(getattr(Config, "SYNC_ECO_FAST_SECONDS", 5.0) or 5.0)
            idle_1 = float(getattr(Config, "SYNC_ECO_IDLE1_SECONDS", 8.0) or 8.0)
            idle_2 = float(getattr(Config, "SYNC_ECO_IDLE2_SECONDS", 18.0) or 18.0)
            idle_3 = float(getattr(Config, "SYNC_ECO_IDLE3_SECONDS", 35.0) or 35.0)

        interval = fast_interval
        idle_cycles = 0
        error_streak = 0
        while True:
            await asyncio.sleep(interval)
            if mode == "cache_only":
                continue
            self._flush_pending_sa_sync_pulse_reload_if_due()
            try:
                company_ids = self._sync_company_ids()
                if not company_ids:
                    interval = min(30.0, interval + 5.0)
                    continue

                # Threaded — this used to run directly on the event loop
                # (blocking every button/click handler for the round-trip
                # duration); over a remote VPS connection that's no longer
                # free the way it was on localhost.
                pulses = await asyncio.to_thread(
                    self.db.get_companies_sync_pulses,
                    company_ids,
                    self._sync_scopes(),
                    True,
                )
                changed = False
                changed_scopes = set()
                changed_company_scopes = {}
                snapshot_changed = False

                for company_id, scope_map in (pulses or {}).items():
                    if not isinstance(scope_map, dict):
                        continue
                    for scope, ts in scope_map.items():
                        key = f"{company_id}:{scope}"
                        prev = int(self._sync_last_pulses.get(key, 0) or 0)
                        now_ts = int(ts or 0)
                        if now_ts <= 0:
                            continue
                        if prev > 0 and now_ts > prev:
                            self.db.invalidate_scope_cache(company_id, scope)
                            changed = True
                            changed_scopes.add(scope)
                            changed_company_scopes.setdefault(company_id, set()).add(
                                scope
                            )
                        if now_ts != prev:
                            snapshot_changed = True
                        self._sync_last_pulses[key] = now_ts

                if snapshot_changed:
                    self._save_sync_snapshot()

                if changed:
                    self._sync_dirty_scopes.update(changed_scopes)
                    self._on_sa_sync_pulse_needs_tab_reload()
                    interval = post_change
                    idle_cycles = 0
                    error_streak = 0
                else:
                    idle_cycles += 1
                    if idle_cycles <= 3:
                        interval = idle_1
                    elif idle_cycles <= 10:
                        interval = idle_2
                    else:
                        interval = idle_3
            except Exception:
                logger.exception("Superadmin dashboard sync poll failed")
                error_streak += 1
                interval = min(
                    60.0, max(5.0, interval * (1.6 + min(error_streak, 5) * 0.1))
                )

    def _app_data_dir(self):
        flet_storage = os.getenv("FLET_APP_STORAGE_DATA")
        if flet_storage:
            target = flet_storage
        else:
            base = os.getenv("APPDATA") or os.path.expanduser("~")
            target = os.path.join(base, "CatManPro")
        os.makedirs(target, exist_ok=True)
        return target

    def _remove_from_quick_accounts(self, email_to_remove):
        """Remove an account from quick_accounts list by email."""
        if not email_to_remove:
            return
        try:
            email_norm = (email_to_remove or "").strip().lower()
            accounts = self._read_saved_accounts()
            filtered = [
                a
                for a in accounts
                if (a.get("email") or "").strip().lower() != email_norm
            ]
            if len(filtered) < len(accounts):
                self._save_saved_accounts(filtered)
        except Exception:
            pass

    def _quick_accounts_file(self):
        return os.path.join(self._app_data_dir(), "quick_accounts.json")

    def _read_saved_accounts(self):
        path = self._quick_accounts_file()
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            rows = payload.get("accounts", []) if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                return []
            out = []
            seen = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                email = (row.get("email") or "").strip()
                if not email:
                    continue
                key = email.lower()
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "company": (row.get("company") or "").strip(),
                        "manager": (row.get("manager") or "").strip(),
                        "email": email,
                        "phone": (row.get("phone") or "").strip(),
                        "role": (row.get("role") or "").strip(),
                        "manager_id": (row.get("manager_id") or "").strip(),
                        "company_id": (row.get("company_id") or "").strip(),
                    }
                )
            return out
        except Exception:
            logger.exception("Read saved accounts failed")
            return []

    def _save_saved_accounts(self, accounts):
        try:
            path = self._quick_accounts_file()
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"accounts": accounts}, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            logger.exception("Save saved accounts failed")
            return False

    def _upsert_saved_account(
        self, company, manager, email, phone, role="", manager_id="", company_id=""
    ):
        email_norm = (email or "").strip().lower()
        if not email_norm:
            return
        accounts = self._read_saved_accounts()
        updated = False
        for item in accounts:
            if (item.get("email") or "").strip().lower() == email_norm:
                item["company"] = company or ""
                item["manager"] = manager or ""
                item["email"] = email or ""
                item["phone"] = phone or ""
                item["role"] = role or item.get("role") or ""
                item["manager_id"] = manager_id or item.get("manager_id") or ""
                item["company_id"] = company_id or item.get("company_id") or ""
                updated = True
                break
        if not updated:
            accounts.append(
                {
                    "company": company or "",
                    "manager": manager or "",
                    "email": email or "",
                    "phone": phone or "",
                    "role": role or "",
                    "manager_id": manager_id or "",
                    "company_id": company_id or "",
                }
            )
        self._save_saved_accounts(accounts)

    def _account_option_label(self, account):
        manager = (account.get("manager") or "").strip() or "Користувач"
        company = (account.get("company") or "").strip()
        email = (account.get("email") or "").strip()
        role = (account.get("role") or "").strip()
        left = f"{manager} ({company})" if company else manager
        right = f"{email} · {role}" if role else email
        return f"{left} — {right}" if right else left

    def _pick_local_image_file(self):
        if not TK_AVAILABLE:
            raise RuntimeError("Локальний діалог файлів недоступний")
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Оберіть фон застосунку",
            filetypes=[
                ("Зображення", "*.jpg *.jpeg *.png *.gif *.bmp *.webp"),
                ("Усі файли", "*.*"),
            ],
        )
        root.destroy()
        return path

    def _wrap_with_background(self, content):
        # In aggregated modes a textured background looks like gray overlays on large list sections.
        if (
            self.selected_company_id in {None, ALL_COMPANIES_KEY}
            or self.selected_region_name == ALL_REGIONS_KEY
        ):
            return content
        bg_src = self.db.get_effective_background_source(
            self.selected_company_id, self.user_data.get("id")
        )
        bg_src = self.db.resolve_image_source(bg_src) if bg_src else ""
        ct = ft.Container(
            expand=True,
            image=(
                ft.DecorationImage(src=bg_src, fit="cover", opacity=0.22)
                if bg_src
                else None
            ),
            content=content,
        )
        self._bg_container = ct
        return ct

    def _refresh_background_now(self):
        try:
            if not getattr(self, "_bg_container", None):
                return
            bg_src = self.db.get_effective_background_source(
                self.selected_company_id, self.user_data.get("id")
            )
            bg_src = self.db.resolve_image_source(bg_src) if bg_src else ""
            self._bg_container.image = (
                ft.DecorationImage(src=bg_src, fit="cover", opacity=0.22)
                if bg_src
                else None
            )
            self.page.update()
        except Exception:
            logger.exception("Failed to refresh background")

    def _show_snack(self, text, color):
        self.page.snack_bar = ft.SnackBar(
            ft.Text(text, color=ft.Colors.WHITE, weight="bold"),
            bgcolor=color,
            duration=3000,
        )
        self.page.snack_bar.open = True
        self.page.update()

    def _show_saved_dialog(self, text="Дані успішно оновлені і збережені."):
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Успіх"),
            content=ft.Text(text),
            actions=[ft.FilledButton("OK", on_click=lambda _: self._close_dialog(dlg))],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _get_region_name(self, company_id, region_id):
        if not company_id or not region_id:
            return None
        regions = self.db.get_company_regions(company_id)
        return next(
            (
                region.get("name")
                for region in regions
                if isinstance(region, dict) and region.get("id") == region_id
            ),
            None,
        )

    def _format_category_label(self, category, company_id=None):
        category = category if isinstance(category, dict) else {}
        category_name = category.get("name", "Категорія")
        region_name = self._get_region_name(
            company_id or category.get("company_id"), category.get("region_id")
        )
        if region_name:
            return f"{category_name} ({region_name})"
        return category_name

    def _get_category(self, company_id, category_id):
        if not company_id or not category_id:
            return None
        categories = self.db.get_company_categories(company_id, None)
        return next(
            (
                category
                for category in categories
                if isinstance(category, dict) and category.get("id") == category_id
            ),
            None,
        )

    def _format_subcategory_label(self, subcategory, company_id=None, category_id=None):
        subcategory = subcategory if isinstance(subcategory, dict) else {}
        subcategory_name = subcategory.get("name", "Підкатегорія")
        category = self._get_category(
            company_id, category_id or subcategory.get("category_id")
        )
        region_name = self._get_region_name(
            company_id or (category or {}).get("company_id"),
            (category or {}).get("region_id"),
        )
        if region_name:
            return f"{subcategory_name} ({region_name})"
        return subcategory_name

    def _open_as_user(self, manager_id):
        try:
            profile = self.db.get_manager_profile(manager_id)
            if not profile:
                self._show_snack("Не вдалося отримати профіль користувача", "red")
                return

            # Режим імперсонації: відкриваємо звичайний дашборд від імені обраного користувача.
            profile["impersonated_by_superadmin"] = True
            profile["impersonator"] = {
                "id": self.user_data.get("id"),
                "full_name": self.user_data.get("full_name"),
                "role": self.user_data.get("role"),
                "email": self.user_data.get("email"),
                "phone": self.user_data.get("phone"),
                "company_id": self.user_data.get("company_id"),
            }

            dash = MainDashboard(self.page, profile, on_logout=self.on_logout)
            dash.build()
            self.page.update()
        except Exception:
            logger.exception("Impersonation open failed")
            self._show_snack("Не вдалося відкрити режим користувача", "red")

    def _open_notebook(self, _=None) -> None:
        # Pure personal task diary — no company_id, same as superadmin's.
        # Passing one triggers _sync_order_schedule_tasks (see notebook_
        # view.py), which pulls in "Розклад Замовлень" tasks scoped to
        # this company — an admin isn't a category manager who owns any
        # of those tasks personally (admin/superadmin have no Розклад
        # Замовлень at all), so that sync surfaced OTHER users' order-
        # schedule tasks in the admin's own notebook (confirmed live: an
        # admin opening their notebook saw a regular manager's "Замовити
        # ..." task list). Admin's notebook only ever shows admin's own
        # manually-typed notes/tasks.
        open_notebook_modal(
            self.page, self.db,
            manager_id=str(self.user_data.get("id") or ""),
            on_close=self._refresh_notebook_badge,
        )

    def _make_notebook_toolbar_btn(self, th_text, btn_h) -> ft.Stack:
        btn = ft.OutlinedButton(
            "",
            icon=ft.Icons.MENU_BOOK,
            icon_color=th_text,
            tooltip=self.t("nav.notebook_tooltip", "Блокнот"),
            on_click=self._open_notebook,
            height=btn_h,
            style=ft.ButtonStyle(color=th_text),
        )
        today_n, overdue_n = get_notebook_badge_counts(
            self.db, str(self.user_data.get("id") or "")
        )
        today_badge = ft.Container(
            content=ft.Text(
                str(today_n) if today_n else "",
                size=8, color="#000000", weight=ft.FontWeight.W_700,
                text_align=ft.TextAlign.CENTER,
            ),
            bgcolor="#FFD600", border_radius=8, height=16,
            padding=ft.padding.symmetric(horizontal=3),
            alignment=ft.alignment.Alignment(0, 0),
            top=-6, right=-6, visible=today_n > 0,
        )
        overdue_badge = ft.Container(
            content=ft.Text(
                str(overdue_n) if overdue_n else "",
                size=8, color="#FFFFFF", weight=ft.FontWeight.W_700,
                text_align=ft.TextAlign.CENTER,
            ),
            bgcolor="#E53935", border_radius=8, height=16,
            padding=ft.padding.symmetric(horizontal=3),
            alignment=ft.alignment.Alignment(0, 0),
            bottom=-6, right=-6, visible=overdue_n > 0,
        )
        self._notebook_badge_today_cont = today_badge
        self._notebook_badge_overdue_cont = overdue_badge
        return ft.Stack([btn, today_badge, overdue_badge])

    def _refresh_notebook_badge(self) -> None:
        if getattr(self, "_notebook_badge_today_cont", None) is None:
            return
        today_n, overdue_n = get_notebook_badge_counts(
            self.db, str(self.user_data.get("id") or "")
        )
        tc = self._notebook_badge_today_cont
        oc = self._notebook_badge_overdue_cont
        tc.content.value = str(today_n) if today_n else ""
        tc.visible = today_n > 0
        oc.content.value = str(overdue_n) if overdue_n else ""
        oc.visible = overdue_n > 0
        with contextlib.suppress(Exception):
            tc.update()
        with contextlib.suppress(Exception):
            oc.update()

    def _open_calculator(self, _=None) -> None:
        if not self.selected_company_id or self.selected_company_id == ALL_COMPANIES_KEY:
            self._show_snack("Оберіть компанію", "orange")
            return
        open_calculators_modal(
            self.page, self.db,
            manager_id=str(self.user_data.get("id") or ""),
            company_id=str(self.selected_company_id or ""),
            region_id=str(getattr(self, "selected_region_name", "") or ""),
        )

    def show_overview(self, e):
        self.current_tab = "overview"
        self.content_area.controls.clear()
        visible_company_ids = {c.get("id") for c in self._visible_companies()}

        pending_rows = self.db.get_pending_managers()
        pending_rows = [
            p
            for p in pending_rows
            if isinstance(p, dict) and p.get("company_id") in visible_company_ids
        ]
        pending_count = len(pending_rows)

        all_managers = [
            m
            for m in self.db._list("managers")
            if isinstance(m, dict) and m.get("company_id") in visible_company_ids
        ]
        users_count = len(
            [
                m
                for m in all_managers
                if isinstance(m, dict)
                and (m.get("role") or "").strip().lower().replace(" ", "")
                not in {"суперадмін", "суперадмин", "superadmin"}
            ]
        )

        categories_count = 0
        products_count = 0
        packagings_count = 0
        regions_set = set()

        for company_id in visible_company_ids:
            regions = self.db.get_company_regions(company_id) or []
            for region in regions:
                if isinstance(region, dict) and region.get("id"):
                    regions_set.add(region.get("id"))

            categories = self.db.get_company_categories(company_id) or []
            categories_count += len(categories)

            products = self.db.get_products(company_id) or []
            products_count += len(products)

            packagings = self.db.get_packaging(company_id) or []
            packagings_count += len(packagings)

        regions_count = len(regions_set)

        admin_id = str(self.user_data.get("id") or "").strip()

        _admin_skip_roles = {"суперадмін", "суперадмин", "superadmin", "адмін", "admin"}

        company_managers = [
            m
            for m in (self.db.get_company_managers(self.selected_company_id) or [])
            if isinstance(m, dict)
            and m.get("id")
            and str(m.get("id") or "") != admin_id
            and (m.get("role") or "").strip().lower().replace(" ", "")
            not in _admin_skip_roles
        ]

        home_view_container = ft.Column(spacing=0)
        _hv_ref = {"current": None}

        def _on_general_selection_changed(new_ids):
            current_hv = _hv_ref.get("current")
            if current_hv is None:
                return
            current_hv.general_manager_ids = list(new_ids or [])
            current_hv._async_build_token = int(
                getattr(current_hv, "_async_build_token", 0) or 0
            ) + 1
            _tok = current_hv._async_build_token

            async def _rebuild():
                await asyncio.sleep(0.01)
                if _tok != int(getattr(current_hv, "_async_build_token", 0) or 0):
                    return
                try:
                    current_hv._prefetch_cache = {}
                    current_hv._live_orders_snapshot = None
                    current_hv._build_sync()
                    with contextlib.suppress(Exception):
                        home_view_container.update()
                except Exception:
                    logger.exception("Admin overview rebuild after apply failed")

            with contextlib.suppress(Exception):
                self.page.run_task(_rebuild)

        def _render_manager_home():
            if _hv_ref["current"] is not None:
                with contextlib.suppress(Exception):
                    _hv_ref["current"].stop()
            home_view_container.controls.clear()
            ctx_user = dict(self.user_data)
            ctx_user["id"] = ""
            ctx_user["full_name"] = ""
            ctx_user["company_id"] = self.selected_company_id
            ctx_user["bypass_category_access"] = True
            ctx_user["general_mode"] = True
            ctx_user["general_manager_ids"] = [
                m.get("id") for m in company_managers if m.get("id")
            ]
            ctx_user["all_subordinates_flat"] = list(company_managers)
            ctx_user["active_manager_id"] = None
            ctx_user["active_manager_name"] = "Всі менеджери"
            region_label = self.selected_region_name or "Всі регіони"
            hv = HomeView(
                home_view_container,
                ctx_user,
                self.db,
                region_label,
                active_region=None,
                on_general_selection_changed=_on_general_selection_changed,
            )
            _hv_ref["current"] = hv
            hv._force_fresh_from_db = True
            with contextlib.suppress(Exception):
                hv.build()

        self.content_area.controls.extend(
            [
                ft.Text("Огляд", size=20, weight="bold"),
                ft.Row(
                    [
                        self._metric_card(
                            "Заявки",
                            str(pending_count),
                            "orange",
                            on_click=lambda _: self.show_approvals(None),
                        ),
                        self._metric_card(
                            "Користувачів",
                            str(users_count),
                            "green",
                            on_click=lambda _: self.show_users(None),
                        ),
                        self._metric_card(
                            "Категорій",
                            str(categories_count),
                            "blue",
                            on_click=lambda _: self.show_categories(None),
                        ),
                        self._metric_card(
                            "Пакувань",
                            str(packagings_count),
                            "purple",
                            on_click=lambda _: self.show_packaging(None),
                        ),
                        self._metric_card(
                            "Товарів", str(products_count), "cyan", on_click=None
                        ),
                        self._metric_card(
                            "Регіони", str(regions_count), "teal", on_click=None
                        ),
                    ],
                    wrap=True,
                    spacing=8,
                    run_spacing=6,
                ),
                ft.Divider(height=12),
                home_view_container,
            ]
        )

        self.page.update()
        _render_manager_home()
        with contextlib.suppress(Exception):
            self.page.update()

    def _metric_card(self, title, value, color, on_click=None):
        return ft.Container(
            width=140,
            padding=ft.padding.symmetric(horizontal=10, vertical=8),
            border=ft.border.all(1, "#E5E7EB"),
            border_radius=8,
            ink=True,
            on_click=on_click,
            tooltip=f"Відкрити: {title}" if on_click else None,
            content=ft.Column(
                [
                    ft.Text(title, size=11, color="grey700"),
                    ft.Text(value, size=20, weight="bold", color=color),
                ],
                spacing=2,
            ),
        )

    def show_approvals(self, e):
        self.current_tab = "approvals"
        self.content_area.controls.clear()
        refresh_users = self._consume_scope_dirty("users")
        refresh_company = self._consume_scope_dirty("company")
        refresh_catalog = self._consume_scope_dirty("catalog")
        pending = self.db.get_pending_managers(force_refresh=refresh_users)
        company_rows = self._visible_companies(force_refresh=refresh_company)
        visible_company_ids = {c.get("id") for c in company_rows}
        pending = [
            p
            for p in pending
            if isinstance(p, dict) and p.get("company_id") in visible_company_ids
        ]
        companies = {c.get("id"): c.get("name") for c in company_rows}

        def _is_unrestricted_role(role_value):
            normalized = (role_value or "").strip().lower().replace(" ", "")
            return normalized in {
                "адмін",
                "админ",
                "admin",
                "суперадмін",
                "суперадмин",
                "superadmin",
            }

        def open_pending_supervisors_dialog(_, manager):
            manager = manager if isinstance(manager, dict) else {}
            manager_id = manager.get("id")
            company_id = manager.get("company_id")
            if not manager_id or not company_id:
                self._show_snack("Не вдалося визначити користувача/компанію", "orange")
                return

            company_managers = self.db.get_company_managers(
                company_id, force_refresh=refresh_users
            )
            selected_ids = set(self.db.get_manager_supervisor_ids(manager_id))

            checks = {}
            checks_block = ft.Column(spacing=4)
            for sup in company_managers:
                if not isinstance(sup, dict) or not sup.get("id"):
                    continue
                sup_id = sup.get("id")
                if sup_id == manager_id:
                    continue
                label = f"{sup.get('full_name', '')} ({sup.get('role', '')})"
                cb = ft.Checkbox(label=label, value=bool(sup_id in selected_ids))
                checks[sup_id] = cb
                checks_block.controls.append(cb)

            if not checks_block.controls:
                checks_block.controls.append(
                    ft.Text("Немає доступних керівників", color="grey600")
                )

            def save_supervisors(_):
                selected = [sid for sid, cb in checks.items() if cb.value]
                ok = self.db.replace_manager_supervisors(
                    manager_id, selected, company_id=company_id
                )
                if ok:
                    self.page.pop_dialog()
                    self._show_saved_dialog()
                    self.show_approvals(None)
                else:
                    self._show_snack("Не вдалося оновити керівників", "red")

            dlg = ft.AlertDialog(
                title=ft.Text(f"Керівники: {manager.get('full_name', '')}"),
                content=ft.Container(
                    width=self._dialog_width(520),
                    height=self._dialog_content_height(460),
                    content=ft.Column(
                        [checks_block], tight=True, scroll=ft.ScrollMode.ADAPTIVE
                    ),
                ),
                actions=[
                    ft.TextButton(
                        "Скасувати", on_click=lambda _: self._close_dialog(dlg)
                    ),
                    ft.FilledButton("Зберегти", on_click=save_supervisors),
                ],
            )
            self.page.show_dialog(dlg)

        def open_pending_access_dialog(_, manager):
            manager = manager if isinstance(manager, dict) else {}
            manager_id = manager.get("id")
            company_id = manager.get("company_id")
            if not manager_id or not company_id:
                self._show_snack("Не вдалося визначити користувача/компанію", "orange")
                return

            all_nodes = self.db.get_company_catalog_nodes(
                company_id, force_refresh=refresh_catalog
            )
            regions_raw = self.db.get_company_regions(
                company_id, force_refresh=refresh_company
            )
            regions = [r for r in regions_raw if isinstance(r, dict) and r.get("id")]
            regions_by_id = {r.get("id"): r.get("name", "") for r in regions}
            selected_ids = set(
                self.db.get_manager_scope_node_ids(
                    manager_id, company_id=company_id, permission="view"
                )
            )

            if not selected_ids:
                legacy_cat_ids = set(self.db.get_manager_category_ids(manager_id))
                for n in all_nodes:
                    if not isinstance(n, dict):
                        continue
                    legacy = (n.get("legacy_id") or "").strip()
                    if (
                        legacy.startswith("cat:")
                        and legacy.split(":", 1)[1] in legacy_cat_ids
                    ):
                        selected_ids.add(n.get("id"))

            manager_allowed_region_ids = set(
                self.db.get_manager_allowed_region_ids(manager_id)
            )
            all_checks = {}
            # If this manager is restricted to specific region(s), only offer
            # those regions in the picker — an admin should never be able to
            # pick a region here that the manager isn't allowed to see.
            selectable_regions = (
                [r for r in regions if r.get("id") in manager_allowed_region_ids]
                if manager_allowed_region_ids
                else regions
            )
            region_options = [
                ft.dropdown.Option(key=r.get("id"), text=r.get("name"))
                for r in selectable_regions
            ]
            if len(region_options) > 1:
                region_options = [
                    ft.dropdown.Option(key=ALL_REGIONS_KEY, text="Всі регіони")
                ] + region_options
            region_f = ft.Dropdown(
                label="Регіон",
                border_radius=10,
                options=region_options,
                value=(
                    ALL_REGIONS_KEY
                    if len(selectable_regions) > 1
                    else (
                        selectable_regions[0].get("id") if selectable_regions else None
                    )
                ),
                width=320,
                visible=len(selectable_regions) > 1,
            )
            tree_col = ft.Column([], spacing=2)

            def rebuild_tree(region_id=None):
                all_checks.clear()
                nodes = [n for n in all_nodes if isinstance(n, dict)]
                effective_region_ids = None
                if region_id and region_id != ALL_REGIONS_KEY:
                    effective_region_ids = {region_id}
                    if manager_allowed_region_ids:
                        effective_region_ids &= manager_allowed_region_ids
                elif manager_allowed_region_ids:
                    effective_region_ids = manager_allowed_region_ids
                if effective_region_ids is not None:
                    nodes = [
                        n
                        for n in nodes
                        if n.get("node_type") != "category"
                        or n.get("region_id") in effective_region_ids
                    ]

                children_of = {}
                for n in nodes:
                    pid = n.get("parent_id")
                    nid = n.get("id")
                    if pid and nid:
                        children_of.setdefault(pid, []).append(n)

                for pid in list(children_of.keys()):
                    children_of[pid] = sorted(
                        children_of.get(pid, []),
                        key=lambda x: (str(x.get("name") or "")).lower(),
                    )

                for pid in list(children_of.keys()):
                    children_of[pid] = sorted(
                        children_of.get(pid, []),
                        key=lambda x: (str(x.get("name") or "")).lower(),
                    )

                def _make_cb(node):
                    nid = node.get("id")
                    label = node.get("name", nid)
                    if node.get("node_type") == "category":
                        rid = node.get("region_id")
                        if rid and rid in regions_by_id:
                            label = f"{label} ({regions_by_id[rid]})"
                    cb = ft.Checkbox(label=label, value=bool(nid in selected_ids))
                    all_checks[nid] = cb
                    return cb

                def _has_selected_descendant(node_id, visited=None):
                    if not node_id:
                        return False
                    if visited is None:
                        visited = set()
                    if node_id in visited:
                        return False
                    visited.add(node_id)
                    for ch in children_of.get(node_id, []):
                        ch_id = ch.get("id")
                        if ch_id in selected_ids:
                            return True
                        if _has_selected_descendant(ch_id, visited):
                            return True
                    return False

                def _build_node_tile(node, indent_left=4):
                    nid = node.get("id")
                    header_row = ft.Row([_make_cb(node)])
                    child_nodes = children_of.get(nid, [])
                    if child_nodes:
                        return ft.ExpansionTile(
                            title=header_row,
                            expanded=bool(
                                nid in selected_ids or _has_selected_descendant(nid)
                            ),
                            controls=[
                                _build_node_tile(ch, indent_left=indent_left + 10)
                                for ch in child_nodes
                            ],
                            controls_padding=ft.padding.only(left=12),
                        )
                    return ft.Container(
                        padding=ft.padding.only(left=indent_left, top=2, bottom=2),
                        content=header_row,
                    )

                categories = [n for n in nodes if n.get("node_type") == "category"]
                categories.sort(key=lambda n: (n.get("name") or "").lower())
                tree_controls = [_build_node_tile(c) for c in categories]
                if not tree_controls:
                    tree_controls = [
                        ft.Text("У компанії немає елементів каталогу", color="grey600")
                    ]
                tree_col.controls = tree_controls

            def on_region_change(_):
                rebuild_tree(region_f.value)
                self.page.update()

            region_f.on_select = on_region_change
            rebuild_tree(region_f.value)

            def save_access(_):
                selected = [nid for nid, cb in all_checks.items() if cb.value]
                ok = self.db.replace_manager_access_scopes(
                    manager_id,
                    company_id,
                    selected,
                    permission="view",
                    inherit_to_children=True,
                )
                if ok:
                    self.page.pop_dialog()
                    self._show_saved_dialog()
                    self.show_approvals(None)
                else:
                    self._show_snack("Не вдалося оновити доступи", "red")

            dlg = ft.AlertDialog(
                title=ft.Text(f"Доступи: {manager.get('full_name', '')}"),
                content=ft.Container(
                    content=ft.Column(
                        [region_f, tree_col],
                        tight=True,
                        spacing=8,
                        scroll=ft.ScrollMode.ADAPTIVE,
                    ),
                    width=self._dialog_width(620),
                    height=self._dialog_content_height(620),
                ),
                actions=[
                    ft.TextButton(
                        "Скасувати", on_click=lambda _: self._close_dialog(dlg)
                    ),
                    ft.FilledButton("Зберегти", on_click=save_access),
                ],
            )
            self.page.show_dialog(dlg)

        def open_check_dialog(_, manager):
            manager_id = manager.get("id")
            if not manager_id:
                self._show_snack("Не вдалося відкрити заявку", "red")
                return

            company_f = ft.Dropdown(
                label="Компанія",
                border_radius=10,
                options=[
                    ft.dropdown.Option(key=c.get("id"), text=c.get("name"))
                    for c in company_rows
                ],
                value=(
                    self._scope_company_id()
                    if self._is_company_scoped()
                    else manager.get("company_id")
                ),
                disabled=self._is_company_scoped(),
            )
            full_name_f = ft.TextField(
                label="Ім'я", border_radius=10, value=manager.get("full_name", "")
            )
            email_f = ft.TextField(
                label="Email",
                border_radius=10,
                value=manager.get("email", ""),
                width=280,
            )
            phone_f = ft.TextField(
                label="Телефон",
                border_radius=10,
                value=manager.get("phone", ""),
                width=280,
            )
            role_f = ft.Dropdown(
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
                value=manager.get("role", "Категорійний менеджер"),
            )

            regions_checks = {}
            regions_checks_block = ft.Column(spacing=4)
            regions_section = ft.Column(
                [
                    ft.Text("Регіони", weight="bold", size=12),
                    regions_checks_block,
                ],
                spacing=6,
            )

            def _load_regions_for_company(company_id):
                regions_checks.clear()
                regions_checks_block.controls.clear()
                if not company_id:
                    regions_checks_block.controls.append(
                        ft.Text("Оберіть компанію", color="grey600", size=11)
                    )
                    return

                selected_ids = set(self.db.get_manager_allowed_region_ids(manager_id))
                rows = self.db.get_company_regions(
                    company_id, force_refresh=refresh_company
                )
                for reg in rows:
                    if not isinstance(reg, dict) or not reg.get("id"):
                        continue
                    rid = reg.get("id")
                    cb = ft.Checkbox(
                        label=reg.get("name", "Регіон"), value=bool(rid in selected_ids)
                    )
                    regions_checks[rid] = cb
                    regions_checks_block.controls.append(cb)
                if not regions_checks_block.controls:
                    regions_checks_block.controls.append(
                        ft.Text("У компанії немає регіонів", color="grey600", size=11)
                    )

            def _sync_regions_visibility():
                regions_section.visible = not _is_unrestricted_role(role_f.value)

            def _on_company_change(_e=None):
                _load_regions_for_company(company_f.value)
                _sync_regions_visibility()
                self.page.update()

            def _on_role_change(_e=None):
                _sync_regions_visibility()
                self.page.update()

            company_f.on_select = _on_company_change
            role_f.on_select = _on_role_change
            _on_company_change()
            _on_role_change()

            def _save_changes(_=None):
                self._clear_field_errors(company_f, full_name_f, email_f, phone_f)
                if (
                    not company_f.value
                    or not (full_name_f.value or "").strip()
                    or not (email_f.value or "").strip()
                    or not (phone_f.value or "").strip()
                ):
                    if not company_f.value:
                        self._mark_required(company_f, "Оберіть компанію")
                    if not (full_name_f.value or "").strip():
                        self._mark_required(full_name_f, "Введіть ім'я")
                    if not (email_f.value or "").strip():
                        self._mark_required(email_f, "Введіть email")
                    if not (phone_f.value or "").strip():
                        self._mark_required(phone_f, "Введіть телефон")
                    self._show_snack("Заповніть обов'язкові поля", "orange")
                    self.page.update()
                    return

                if self.db.manager_contact_exists(
                    company_f.value,
                    (email_f.value or "").strip(),
                    (phone_f.value or "").strip(),
                    exclude_manager_id=manager_id,
                ):
                    self._mark_required(email_f, "Email або телефон вже існує")
                    self._mark_required(phone_f, "Email або телефон вже існує")
                    self._show_snack(
                        "Користувач з таким email або телефоном вже існує", "orange"
                    )
                    self.page.update()
                    return

                payload = {
                    "company_id": (
                        self._scope_company_id()
                        if self._is_company_scoped()
                        else company_f.value
                    ),
                    "full_name": (full_name_f.value or "").strip(),
                    "email": (email_f.value or "").strip(),
                    "phone": (phone_f.value or "").strip(),
                    "role": role_f.value,
                }
                ok = self.db.update_manager_data(manager_id, payload)
                if not ok:
                    self._show_snack("Не вдалося зберегти зміни", "red")
                    return

                if _is_unrestricted_role(role_f.value):
                    self.db.save_manager_allowed_region_ids(manager_id, [])
                else:
                    selected_regions = [
                        rid for rid, cb in regions_checks.items() if cb.value
                    ]
                    self.db.save_manager_allowed_region_ids(
                        manager_id, selected_regions
                    )

                self.page.pop_dialog()
                self.show_approvals(None)
                self._show_snack("Зміни збережено", "green")

            dlg = ft.AlertDialog(
                title=ft.Text("Check заявки"),
                content=ft.Container(
                    width=self._dialog_width(620),
                    height=self._dialog_content_height(640),
                    content=ft.Column(
                        [
                            company_f,
                            full_name_f,
                            role_f,
                            ft.Row(
                                [email_f, phone_f], spacing=10, wrap=True, run_spacing=8
                            ),
                            regions_section,
                        ],
                        tight=True,
                        spacing=10,
                        scroll=ft.ScrollMode.ADAPTIVE,
                    ),
                ),
                actions=[
                    ft.TextButton(
                        "Скасувати", on_click=lambda _: self._close_dialog(dlg)
                    ),
                    ft.FilledButton("Зберегти", on_click=_save_changes),
                ],
            )
            self.page.show_dialog(dlg)

            async def _warm_regions():
                await asyncio.sleep(0.06)
                _load_regions_for_company(company_f.value)
                _sync_regions_visibility()
                if self.page:
                    self.page.update()

            with contextlib.suppress(Exception):
                asyncio.create_task(_warm_regions())

        controls = [ft.Text("Підтвердження реєстрацій", size=24, weight="bold")]
        if not pending:
            controls.append(ft.Text("Немає заявок", color="grey600"))
        else:
            for m in pending:
                company_name = companies.get(m.get("company_id"), "-")

                def approve_click(_, manager=m):
                    ok = self.db.set_manager_approval(manager.get("id"), True)
                    if ok:
                        self._invalidate_overview_cache()
                        self.show_approvals(None)
                        self._show_snack("Підтверджено", "green")
                    else:
                        self._show_snack(
                            "Ліміт користувачів компанії вичерпано, зверніться до СуперАдміна",
                            "red",
                        )

                def reject_click(_, manager=m):
                    def run_reject(__):
                        self._close_dialog(dlgrej)
                        manager_email = (
                            manager.get("email") if isinstance(manager, dict) else None
                        )
                        ok = self.db.delete_manager_profile(manager.get("id"))
                        if ok:
                            if manager_email:
                                self._remove_from_quick_accounts(manager_email)
                            self._invalidate_overview_cache()
                            self.show_approvals(None)
                            self._show_snack("Видалено", "blue")
                        else:
                            self._show_snack("Не вдалося відхилити заявку", "red")

                    dlgrej = ft.AlertDialog(
                        modal=True,
                        title=ft.Text("Відхилити заявку"),
                        content=ft.Text(
                            "Запис користувача буде безповоротно видалено з бази (як відмова реєстрації).\n\n"
                            f"{manager.get('full_name', '')}\n{manager.get('email', '')}"
                        ),
                        actions=[
                            ft.TextButton(
                                "Скасувати",
                                on_click=lambda _: self._close_dialog(dlgrej),
                            ),
                            ft.FilledButton(
                                "Видалити заявку",
                                bgcolor=ft.Colors.RED_700,
                                on_click=run_reject,
                            ),
                        ],
                    )
                    self.page.show_dialog(dlgrej)

                controls.append(
                    ft.Card(
                        content=ft.Container(
                            padding=12,
                            content=ft.Row(
                                [
                                    ft.Column(
                                        [
                                            ft.Text(
                                                m.get("full_name", ""), weight="bold"
                                            ),
                                            ft.Text(
                                                f"{m.get('role', '')} | {company_name}",
                                                size=12,
                                                color="grey700",
                                            ),
                                            ft.Text(
                                                f"{m.get('email', '')} | {m.get('phone', '')}",
                                                size=12,
                                                color="grey700",
                                            ),
                                        ],
                                        expand=True,
                                        spacing=2,
                                    ),
                                    ft.Container(
                                        width=430,
                                        content=ft.Row(
                                            [
                                                ft.OutlinedButton(
                                                    "Перевірити",
                                                    on_click=lambda _, manager=m: open_check_dialog(
                                                        _, manager
                                                    ),
                                                ),
                                                ft.IconButton(
                                                    ft.Icons.CATEGORY,
                                                    tooltip="Призначити доступи",
                                                    visible=not _is_unrestricted_role(
                                                        m.get("role")
                                                    ),
                                                    on_click=lambda _, manager=m: open_pending_access_dialog(
                                                        _, manager
                                                    ),
                                                ),
                                                ft.IconButton(
                                                    ft.Icons.GROUP,
                                                    tooltip="Призначити керівників",
                                                    visible=not _is_unrestricted_role(
                                                        m.get("role")
                                                    ),
                                                    on_click=lambda _, manager=m: open_pending_supervisors_dialog(
                                                        _, manager
                                                    ),
                                                ),
                                                ft.FilledButton(
                                                    "Прийняти", on_click=approve_click
                                                ),
                                                ft.OutlinedButton(
                                                    "Відхилити", on_click=reject_click
                                                ),
                                            ],
                                            wrap=True,
                                            run_spacing=8,
                                            spacing=8,
                                        ),
                                    ),
                                ],
                                spacing=10,
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                vertical_alignment=ft.CrossAxisAlignment.START,
                            ),
                        )
                    )
                )

        self.content_area.controls.extend(controls)
        self.page.update()

    def show_users(self, e):
        self.current_tab = "users"
        self.content_area.controls.clear()

        refresh_users = self._consume_scope_dirty("users")
        refresh_company = self._consume_scope_dirty("company")
        refresh_catalog = self._consume_scope_dirty("catalog")
        companies = self._visible_companies(force_refresh=refresh_company)
        create_company_options = [
            ft.dropdown.Option(key=c.get("id"), text=c.get("name"))
            for c in companies
            if isinstance(c, dict) and c.get("id")
        ]
        if not self._is_company_scoped():
            create_company_options.append(
                ft.dropdown.Option(key="__new__", text="Нова компанія")
            )

        default_create_company_id = (
            self._scope_company_id()
            if self._is_company_scoped()
            else (
                self.selected_company_id
                if any(
                    isinstance(c, dict) and c.get("id") == self.selected_company_id
                    for c in companies
                )
                else (
                    create_company_options[0].key
                    if create_company_options
                    else "__new__"
                )
            )
        )

        create_company_f = ft.Dropdown(
            label="Компанія",
            border_radius=10,
            options=create_company_options,
            value=default_create_company_id,
            disabled=self._is_company_scoped(),
        )
        company_name_f = ft.TextField(
            label="Назва нової компанії",
            border_radius=10,
            visible=(
                default_create_company_id == "__new__" and not self._is_company_scoped()
            ),
        )
        full_name_f = ft.TextField(label="Ім'я", border_radius=10)
        email_f = ft.TextField(label="Email", border_radius=10)
        phone_f = ft.TextField(label="Телефон", border_radius=10)
        # "Керівник"/"Керівник направлення" (supervisor tiers) вимагають спільної
        # БД компанії для агрегації підлеглих — у Компанія-Локал кожен користувач
        # має власну ізольовану локальну БД, тому супервізор-ролі там безглузді.
        _ALL_ROLES = [
            "Закупівельник",
            "Категорійний менеджер",
            "Керівник направлення",
            "Керівник",
            "Адмін",
        ]
        _company_storage_by_id = {
            c.get("id"): c.get("storage_mode")
            for c in companies
            if isinstance(c, dict) and c.get("id")
        }

        def _role_options_for_company(cid):
            is_local = _company_storage_by_id.get(cid) == "local"
            roles = (
                [r for r in _ALL_ROLES if r not in ("Керівник направлення", "Керівник")]
                if is_local
                else _ALL_ROLES
            )
            return [ft.dropdown.Option(x) for x in roles]

        role_f = ft.Dropdown(
            label="Роль",
            border_radius=10,
            options=_role_options_for_company(default_create_company_id),
            value="Категорійний менеджер",
        )
        password_f = ft.TextField(
            label="Пароль", border_radius=10, password=True, can_reveal_password=True
        )

        managers = (
            self.db.get_company_managers(
                default_create_company_id, force_refresh=refresh_users
            )
            if default_create_company_id != "__new__"
            else []
        )
        manager_options = [
            ft.dropdown.Option(key=m.get("id"), text=m.get("full_name"))
            for m in managers
            if isinstance(m, dict)
        ]
        parent_f = ft.Dropdown(
            label="Керівник", border_radius=10, options=manager_options
        )
        regions_checks_block = ft.Column(spacing=4)
        regions_checks = {}
        catalog_checks_block = ft.Column(spacing=4)
        catalog_checks = {}

        selected_region_id = None
        # In "Всі компанії" mode we cannot resolve a single region list reliably.
        if self.selected_company_id == ALL_COMPANIES_KEY:
            selected_region_id = None
        elif (
            self.selected_company_id
            and self.selected_region_name
            and self.selected_region_name != ALL_REGIONS_KEY
        ):
            for _reg in self.db.get_company_regions(self.selected_company_id):
                if (
                    isinstance(_reg, dict)
                    and (_reg.get("name") or "") == self.selected_region_name
                ):
                    selected_region_id = _reg.get("id")
                    break

        def _manager_visible_for_selected_region(manager_row):
            if self.selected_company_id == ALL_COMPANIES_KEY:
                return True
            if not selected_region_id:
                return True
            manager_row = manager_row if isinstance(manager_row, dict) else {}
            manager_id = manager_row.get("id")
            if not manager_id or not self.selected_company_id:
                return True

            allowed_region_ids = set(self.db.get_manager_allowed_region_ids(manager_id))
            if allowed_region_ids:
                return selected_region_id in allowed_region_ids

            category_rows = [
                c
                for c in self.db.get_company_categories(self.selected_company_id)
                if isinstance(c, dict) and c.get("id")
            ]
            category_region_by_id = {
                c.get("id"): c.get("region_id") for c in category_rows
            }
            manager_cat_ids = set(self.db.get_manager_category_ids(manager_id))
            if manager_cat_ids:
                manager_region_ids = {
                    category_region_by_id.get(cid)
                    for cid in manager_cat_ids
                    if category_region_by_id.get(cid)
                }
                return selected_region_id in manager_region_ids

            scope_node_ids = set(
                self.db.get_manager_scope_node_ids(
                    manager_id, company_id=self.selected_company_id, permission="view"
                )
            )
            if scope_node_ids:
                nodes = [
                    n
                    for n in self.db.get_company_catalog_nodes(self.selected_company_id)
                    if isinstance(n, dict) and n.get("id")
                ]
                by_id = {n.get("id"): n for n in nodes}
                scope_region_ids = set()
                for node_id in scope_node_ids:
                    cur = by_id.get(node_id)
                    hops = 0
                    while cur and hops < 20:
                        if (cur.get("node_type") or "") == "category":
                            if cur.get("region_id"):
                                scope_region_ids.add(cur.get("region_id"))
                            break
                        cur = by_id.get(cur.get("parent_id"))
                        hops += 1
                if scope_region_ids:
                    return selected_region_id in scope_region_ids

            # Compatibility mode: if nothing is explicitly restricted, show user in all regions.
            return True

        def _catalog_label(node, regions_by_id):
            node_type = node.get("node_type") or ""
            name = node.get("name") or ""
            m = re.match(r"^level_(\d+)$", str(node_type))
            if m:
                prefix = f"Рівень {m.group(1)}"
            else:
                prefix = {
                    "category": "Категорія",
                    "subcategory": "Підкатегорія",
                    "series": "Серія",
                    "brand": "Бренд",
                }.get(node_type, node_type)
            if node_type == "category":
                rid = node.get("region_id")
                rname = regions_by_id.get(rid, "") if rid else ""
                if rname:
                    return f"{prefix}: {name} ({rname})"
            return f"{prefix}: {name}"

        def _build_catalog_tree(nodes, regions_by_id):
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

            for pid in list(children_of.keys()):
                children_of[pid] = sorted(
                    children_of.get(pid, []),
                    key=lambda x: (str(x.get("name") or "")).lower(),
                )

            def _make_cb(node):
                nid = node.get("id")
                cb, cb_row = self._wide_checkbox_row(
                    _catalog_label(node, regions_by_id), False
                )
                checks[nid] = cb
                return cb_row

            def _build_node_tile(node, indent_left=4):
                nid = node.get("id")
                header_row = ft.Row([_make_cb(node)])
                child_nodes = children_of.get(nid, [])
                if child_nodes:
                    return ft.ExpansionTile(
                        title=header_row,
                        controls=[
                            _build_node_tile(ch, indent_left=indent_left + 10)
                            for ch in child_nodes
                        ],
                        controls_padding=ft.padding.only(left=12),
                    )
                return ft.Container(
                    padding=ft.padding.only(left=indent_left, top=2, bottom=2),
                    content=header_row,
                )

            categories = [n for n in nodes if n.get("node_type") == "category"]
            categories.sort(key=lambda n: (n.get("name") or "").lower())
            controls = [_build_node_tile(c) for c in categories]
            return controls, checks

        def _reload_creation_access_blocks(company_id):
            regions_checks.clear()
            regions_checks_block.controls.clear()
            catalog_checks.clear()
            catalog_checks_block.controls.clear()

            if not company_id or company_id == "__new__":
                regions_checks_block.controls.append(
                    ft.Text(
                        "Спочатку оберіть існуючу компанію", size=11, color="grey600"
                    )
                )
                catalog_checks_block.controls.append(
                    ft.Text(
                        "Спочатку оберіть існуючу компанію", size=11, color="grey600"
                    )
                )
                return

            region_rows = self.db.get_company_regions(company_id)
            regions_by_id = {
                r.get("id"): r.get("name", "")
                for r in region_rows
                if isinstance(r, dict) and r.get("id")
            }

            nodes_all = self.db.get_company_catalog_nodes(company_id)
            # Keep hierarchy correct: perform one warm-up sync per company per session.
            if company_id not in self._catalog_sync_warmup_done:
                self.db.sync_company_catalog_nodes(company_id)
                self._catalog_sync_warmup_done.add(company_id)
                nodes_all = self.db.get_company_catalog_nodes(company_id)
            allowed_types = {"category", "subcategory", "series", "brand"}
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
                    rid for rid, cb in regions_checks.items() if cb.value
                }
                catalog_checks.clear()
                if not checked_region_ids:
                    catalog_checks_block.controls = [
                        ft.Text(
                            "Спочатку оберіть регіон(и) вище",
                            size=11,
                            color="grey600",
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
                        nodes, regions_by_id
                    )
                    catalog_checks.update(tree_checks)
                    catalog_checks_block.controls = tree_controls or [
                        ft.Text(
                            "У компанії немає елементів каталогу",
                            size=11,
                            color="grey600",
                        )
                    ]
                with contextlib.suppress(Exception):
                    catalog_checks_block.update()

            for reg in region_rows:
                if not isinstance(reg, dict) or not reg.get("id"):
                    continue
                rid = reg.get("id")
                cb, cb_row = self._wide_checkbox_row(
                    reg.get("name", "Регіон"),
                    False,
                    on_change=_rebuild_creation_catalog_tree,
                )
                regions_checks[rid] = cb
                regions_checks_block.controls.append(cb_row)
            if not regions_checks_block.controls:
                regions_checks_block.controls.append(
                    ft.Text("У компанії немає регіонів", size=11, color="grey600")
                )

            _rebuild_creation_catalog_tree()

        def on_create_company_change(_):
            company_name_f.visible = (
                create_company_f.value == "__new__" and not self._is_company_scoped()
            )
            current_managers = (
                self.db.get_company_managers(
                    create_company_f.value, force_refresh=refresh_users
                )
                if create_company_f.value
                else []
            )
            parent_f.options = [
                ft.dropdown.Option(key=m.get("id"), text=m.get("full_name"))
                for m in current_managers
                if isinstance(m, dict)
            ]
            parent_f.value = None
            role_f.options = _role_options_for_company(create_company_f.value)
            if role_f.value not in [opt.key for opt in role_f.options]:
                role_f.value = (
                    role_f.options[0].key if role_f.options else None
                )
            _reload_creation_access_blocks(create_company_f.value)
            if self.page:
                self.page.update()

        create_company_f.on_select = on_create_company_change
        on_create_company_change(None)

        def create_click(_):
            company_id = (
                self._scope_company_id()
                if self._is_company_scoped()
                else create_company_f.value
            )
            if company_id == "__new__":
                company_name = (company_name_f.value or "").strip()
                if not company_name:
                    self._show_snack("Вкажіть назву нової компанії", "orange")
                    return
                company_id = self.db.create_company(company_name)
                if company_id:
                    self.selected_company_id = company_id
            payload = {
                "company_id": company_id,
                "full_name": full_name_f.value.strip(),
                "email": email_f.value.strip(),
                "phone": phone_f.value.strip(),
                "role": role_f.value,
                "password": password_f.value,
                "parent_manager_id": parent_f.value,
                "is_approved": True,
                "allowed_region_ids_json": [
                    rid for rid, cb in regions_checks.items() if cb.value
                ],
            }
            if (
                not payload["company_id"]
                or not payload["full_name"]
                or not payload["email"]
                or not payload["phone"]
                or not payload["password"]
            ):
                self._show_snack("Заповніть обов'язкові поля", "orange")
                return
            can_add, _reason = self.db.check_can_add_approved_manager(
                payload["company_id"]
            )
            if not can_add:
                self._show_snack(
                    "Ліміт користувачів компанії вичерпано, зверніться до СуперАдміна",
                    "red",
                )
                return
            new_id = self.db.create_manager(payload)
            if new_id:
                selected_nodes = [nid for nid, cb in catalog_checks.items() if cb.value]
                if selected_nodes:
                    self.db.replace_manager_access_scopes(
                        new_id,
                        company_id,
                        selected_nodes,
                        permission="view",
                        inherit_to_children=True,
                    )
                self._show_snack("Користувача створено", "green")
                self.show_users(None)
            else:
                self._show_snack("Помилка створення", "red")

        def open_manager_remove_access_dialog(manager):
            """Dialog for removing access from a manager."""
            manager = manager if isinstance(manager, dict) else {}
            manager_id = manager.get("id")
            company_id = manager.get("company_id") or self.selected_company_id
            if not manager_id or not company_id:
                self._show_snack("Не вдалося визначити користувача/компанію", "orange")
                return

            all_nodes = self.db.get_company_catalog_nodes(
                company_id, force_refresh=refresh_catalog
            )
            regions_raw = self.db.get_company_regions(
                company_id, force_refresh=refresh_company
            )
            regions = [r for r in regions_raw if isinstance(r, dict) and r.get("id")]
            regions_by_id = {r.get("id"): r.get("name", "") for r in regions}

            # Get current access node IDs
            current_access_ids = set(
                self.db.get_manager_scope_node_ids(
                    manager_id, company_id=company_id, permission="view"
                )
            )

            if not current_access_ids:
                self._show_snack("Користувач не має доступів для видалення", "orange")
                return

            # Build tree: all accessible nodes
            accessible_nodes = [
                n
                for n in all_nodes
                if isinstance(n, dict) and n.get("id") in current_access_ids
            ]

            # Build parent-child relationships
            children_of = {}
            for n in accessible_nodes:
                pid = n.get("parent_id")
                nid = n.get("id")
                if nid and pid:
                    children_of.setdefault(pid, []).append(n)

            # Sort children by name
            for pid in children_of:
                children_of[pid] = sorted(
                    children_of[pid], key=lambda x: (x.get("name") or "").lower()
                )

            all_checks = {}  # node_id → ft.Checkbox

            def _make_node_label(node):
                """Create label for a node, showing type and name."""
                node_type = node.get("node_type") or ""
                name = node.get("name", node.get("id"))

                # Determine prefix based on node type
                if node_type == "category":
                    prefix = "Категорія"
                    rid = node.get("region_id")
                    rname = regions_by_id.get(rid, "") if rid else ""
                    if rname:
                        return f"{prefix}: {name} ({rname})"
                else:
                    import re

                    m = re.match(r"^level_(\d+)$", str(node_type))
                    if m:
                        prefix = f"Рівень {m.group(1)}"
                    else:
                        prefix = {
                            "subcategory": "Підкатегорія",
                            "series": "Серія",
                            "brand": "Бренд",
                        }.get(node_type, node_type)

                return f"{prefix}: {name}"

            def _build_tree_controls(parent_id=None, indent=0):
                """Recursively build tree controls."""
                controls = []
                children = children_of.get(parent_id, [])

                for child in children:
                    nid = child.get("id")
                    label = _make_node_label(child)

                    # Create checkbox
                    cb = ft.Checkbox(label=label, value=False)
                    all_checks[nid] = cb

                    # Check if has children
                    grandchildren = children_of.get(nid, [])
                    if grandchildren:
                        # Create expandable tile
                        sub_controls = _build_tree_controls(nid, indent + 1)
                        tile = ft.ExpansionTile(
                            title=ft.Row([cb]),
                            expanded=False,
                            controls=sub_controls,
                            controls_padding=ft.padding.only(left=12),
                        )
                        controls.append(tile)
                    else:
                        # Just add checkbox
                        controls.append(
                            ft.Container(
                                padding=ft.padding.only(
                                    left=indent * 12, top=2, bottom=2
                                ),
                                content=ft.Row([cb]),
                            )
                        )

                return controls

            # Get root nodes (categories that user has access to)
            root_categories = [
                n for n in accessible_nodes if n.get("node_type") == "category"
            ]
            root_categories.sort(
                key=lambda n: (
                    str(regions_by_id.get(n.get("region_id"), "") or "").lower(),
                    (n.get("name") or "").lower(),
                )
            )

            # Group by region for display
            grouped = {}
            for c in root_categories:
                rid = str(c.get("region_id") or "").strip()
                grouped.setdefault(rid, []).append(c)

            tree_controls = []
            if grouped:
                ordered_rids = sorted(
                    grouped.keys(),
                    key=lambda rid: str(
                        regions_by_id.get(rid, rid or "Без регіону")
                    ).lower(),
                )
                for rid in ordered_rids:
                    region_name = (
                        str(regions_by_id.get(rid) or "").strip() or "Без регіону"
                    )
                    tree_controls.append(
                        ft.Text(
                            region_name,
                            size=12,
                            weight=ft.FontWeight.W_600,
                            color="#334155",
                        )
                    )

                    for cat in grouped.get(rid, []):
                        cat_id = cat.get("id")
                        cat_label = _make_node_label(cat)
                        cat_cb = ft.Checkbox(label=cat_label, value=False)
                        all_checks[cat_id] = cat_cb

                        # Get children of this category
                        cat_children = children_of.get(cat_id, [])
                        if cat_children:
                            sub_tiles = _build_tree_controls(cat_id, 1)
                            tile = ft.ExpansionTile(
                                title=ft.Row([cat_cb]),
                                expanded=False,
                                controls=sub_tiles,
                                controls_padding=ft.padding.only(left=12),
                            )
                            tree_controls.append(tile)
                        else:
                            tree_controls.append(
                                ft.Container(
                                    padding=ft.padding.only(top=2, bottom=2),
                                    content=ft.Row([cat_cb]),
                                )
                            )

            if not tree_controls:
                tree_controls = [
                    ft.Text("Немає доступів для видалення", color="grey600")
                ]

            access_col = ft.Column(tree_controls, spacing=2)

            def do_remove(_):
                selected_to_remove = [nid for nid, cb in all_checks.items() if cb.value]
                if not selected_to_remove:
                    self._show_snack(
                        "Виберіть категорії або рівні для видалення", "orange"
                    )
                    return

                # For each selected node, expand to include descendants
                all_node_ids_to_remove = []
                for node_id in selected_to_remove:
                    # Include the node itself
                    all_node_ids_to_remove.append(node_id)
                    # Add all descendants
                    stack = [node_id]
                    visited = set([node_id])
                    while stack:
                        cur = stack.pop()
                        for child in children_of.get(cur, []):
                            child_id = child.get("id")
                            if child_id and child_id not in visited:
                                visited.add(child_id)
                                all_node_ids_to_remove.append(child_id)
                                stack.append(child_id)

                # Remove ownership
                removed = self.db._remove_owner_from_nodes(
                    manager_id, company_id, all_node_ids_to_remove
                )

                if removed > 0:
                    self.page.pop_dialog()
                    self._show_saved_dialog(f"Видалено доступ до {removed} вузлів")
                    self.show_users(None)
                else:
                    self._show_snack("Не вдалося видалити доступ", "red")

            dlg = ft.AlertDialog(
                title=ft.Text(f"Прибрати доступи: {manager.get('full_name', '')}"),
                content=ft.Container(
                    content=ft.Column(
                        [
                            ft.Text(
                                "Виберіть категорії або рівні для видалення:",
                                size=11,
                                color="grey700",
                            ),
                            access_col,
                        ],
                        tight=True,
                        spacing=8,
                        scroll=ft.ScrollMode.ADAPTIVE,
                    ),
                    width=self._dialog_width(620),
                    height=self._dialog_content_height(620),
                ),
                actions=[
                    ft.TextButton(
                        "Скасувати", on_click=lambda _: self._close_dialog(dlg)
                    ),
                    ft.FilledButton("Видалити", on_click=do_remove),
                ],
            )
            self.page.show_dialog(dlg)

        def open_manager_access_scopes_dialog(manager):
            manager = manager if isinstance(manager, dict) else {}
            manager_id = manager.get("id")
            company_id = manager.get("company_id") or self.selected_company_id
            if not manager_id or not company_id:
                self._show_snack("Не вдалося визначити користувача/компанію", "orange")
                return

            all_nodes = self.db.get_company_catalog_nodes(
                company_id, force_refresh=refresh_catalog
            )
            regions_raw = self.db.get_company_regions(
                company_id, force_refresh=refresh_company
            )
            regions = [r for r in regions_raw if isinstance(r, dict) and r.get("id")]
            regions_by_id = {r.get("id"): r.get("name", "") for r in regions}

            # Get categories already assigned to anyone in this company
            assigned_category_ids = set(self.db.get_assigned_category_ids(company_id))

            # If this manager is restricted to specific region(s), only offer
            # categories from those regions — a manager with access to one
            # region should never be able to be assigned categories from
            # another region.
            manager_allowed_region_ids = set(
                self.db.get_manager_allowed_region_ids(manager_id)
            )

            # Filter to show only unassigned categories (first owner assignment)
            categories = [
                n
                for n in all_nodes
                if isinstance(n, dict)
                and n.get("node_type") == "category"
                and n.get("id") not in assigned_category_ids
                and (
                    not manager_allowed_region_ids
                    or n.get("region_id") in manager_allowed_region_ids
                )
            ]

            selected_ids = set(
                self.db.get_manager_scope_node_ids(
                    manager_id, company_id=company_id, permission="view"
                )
            )

            if not selected_ids:
                legacy_cat_ids = set(self.db.get_manager_category_ids(manager_id))
                for n in all_nodes:
                    if not isinstance(n, dict):
                        continue
                    legacy = (n.get("legacy_id") or "").strip()
                    if (
                        legacy.startswith("cat:")
                        and legacy.split(":", 1)[1] in legacy_cat_ids
                    ):
                        selected_ids.add(n.get("id"))

            all_checks = {}  # category_id → ft.Checkbox

            # Build simple category list (full assignment mode - no partial node selection)
            categories_col = ft.Column([], spacing=4)

            # Sort categories by region and name
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

            category_rows = []
            if grouped:
                ordered_rids = sorted(
                    grouped.keys(),
                    key=lambda rid: str(
                        regions_by_id.get(rid, rid or "Без регіону")
                    ).lower(),
                )
                for rid in ordered_rids:
                    region_name = (
                        str(regions_by_id.get(rid) or "").strip() or "Без регіону"
                    )
                    category_rows.append(
                        ft.Text(
                            region_name,
                            size=12,
                            weight=ft.FontWeight.W_600,
                            color="#334155",
                        )
                    )
                    for c in grouped.get(rid, []):
                        cat_id = c.get("id")
                        cat_name = c.get("name", cat_id)
                        cb = ft.Checkbox(
                            label=cat_name,
                            value=bool(cat_id in selected_ids),
                        )
                        all_checks[cat_id] = cb
                        category_rows.append(cb)

            if not category_rows:
                category_rows = [
                    ft.Text(
                        "Усі категорії вже призначені або немає категорій",
                        color="grey600",
                    )
                ]

            categories_col.controls = category_rows

            def save_access(_):
                # Collect selected categories
                selected_categories = [
                    cid for cid, cb in all_checks.items() if cb.value
                ]

                # Expand each category to include all its descendants
                all_selected_node_ids = []
                for cat_id in selected_categories:
                    node_ids = self.db.get_category_and_descendants_ids(
                        company_id, cat_id
                    )
                    all_selected_node_ids.extend(node_ids)

                if not all_selected_node_ids:
                    self._show_snack("Виберіть хоча б одну категорію", "orange")
                    return

                # ASSIGN (add) to existing access, don't replace
                ok, msg = self.db.assign_scope_owner(
                    manager_id,
                    company_id,
                    all_selected_node_ids,
                )
                if ok:
                    self.page.pop_dialog()
                    self._show_saved_dialog(msg)
                    self.page.update()
                else:
                    self._show_snack(f"Не вдалося оновити доступи: {msg}", "red")

            dlg = ft.AlertDialog(
                title=ft.Text(
                    f"Призначити доступи (перший власник): {manager.get('full_name', '')}"
                ),
                content=ft.Container(
                    content=ft.Column(
                        [
                            ft.Text(
                                "Виберіть категорії (тільки неприсвоєні):",
                                size=11,
                                color="grey700",
                            ),
                            categories_col,
                        ],
                        tight=True,
                        spacing=8,
                        scroll=ft.ScrollMode.ADAPTIVE,
                    ),
                    width=self._dialog_width(620),
                    height=self._dialog_content_height(620),
                ),
                actions=[
                    ft.TextButton(
                        "Скасувати", on_click=lambda _: self._close_dialog(dlg)
                    ),
                    ft.FilledButton("Зберегти", on_click=save_access),
                ],
            )
            self.page.show_dialog(dlg)

        def open_manager_supervisors_dialog(manager):
            manager = manager if isinstance(manager, dict) else {}
            manager_id = manager.get("id")
            company_id = manager.get("company_id") or self.selected_company_id
            if not manager_id or not company_id:
                self._show_snack("Не вдалося визначити користувача/компанію", "orange")
                return

            company_managers = self.db.get_company_managers(
                company_id, force_refresh=refresh_users
            )
            selected_ids = set(self.db.get_manager_supervisor_ids(manager_id))

            checks = {}
            checks_block = ft.Column(spacing=4)
            for sup in company_managers:
                if not isinstance(sup, dict) or not sup.get("id"):
                    continue
                sup_id = sup.get("id")
                if sup_id == manager_id:
                    continue
                label = f"{sup.get('full_name', '')} ({sup.get('role', '')})"
                cb = ft.Checkbox(label=label, value=bool(sup_id in selected_ids))
                checks[sup_id] = cb
                checks_block.controls.append(cb)

            if not checks_block.controls:
                checks_block.controls.append(
                    ft.Text("Немає доступних керівників", color="grey600")
                )

            def save_supervisors(_):
                selected = [sid for sid, cb in checks.items() if cb.value]
                ok = self.db.replace_manager_supervisors(
                    manager_id, selected, company_id=company_id
                )
                if ok:
                    self.page.pop_dialog()
                    self._show_saved_dialog()
                    self.page.update()
                else:
                    self._show_snack("Не вдалося оновити керівників", "red")

            dlg = ft.AlertDialog(
                title=ft.Text(f"Керівники: {manager.get('full_name', '')}"),
                content=ft.Container(
                    width=self._dialog_width(520),
                    height=self._dialog_content_height(460),
                    content=ft.Column(
                        [checks_block], tight=True, scroll=ft.ScrollMode.ADAPTIVE
                    ),
                ),
                actions=[
                    ft.TextButton(
                        "Скасувати", on_click=lambda _: self._close_dialog(dlg)
                    ),
                    ft.FilledButton("Зберегти", on_click=save_supervisors),
                ],
            )
            self.page.show_dialog(dlg)

        def open_manager_regions_dialog(manager):
            manager = manager if isinstance(manager, dict) else {}
            manager_id = manager.get("id")
            company_id = manager.get("company_id") or self.selected_company_id
            if not manager_id or not company_id:
                self._show_snack("Не вдалося визначити користувача/компанію", "orange")
                return

            regions = self.db.get_company_regions(
                company_id, force_refresh=refresh_company
            )
            selected_ids = set(self.db.get_manager_allowed_region_ids(manager_id))

            checks = {}
            checks_block = ft.Column(spacing=4)
            for reg in regions:
                if not isinstance(reg, dict) or not reg.get("id"):
                    continue
                rid = reg.get("id")
                cb = ft.Checkbox(
                    label=reg.get("name", "Регіон"), value=bool(rid in selected_ids)
                )
                checks[rid] = cb
                checks_block.controls.append(cb)

            if not checks_block.controls:
                checks_block.controls.append(
                    ft.Text("Немає регіонів у компанії", color="grey600")
                )

            hint = ft.Text(
                "Якщо не вибрано жодного регіону - користувач бачить усі (режим сумісності).",
                size=11,
                color="grey700",
            )

            def save_regions(_):
                selected = [rid for rid, cb in checks.items() if cb.value]
                ok = self.db.save_manager_allowed_region_ids(manager_id, selected)
                if ok:
                    self.page.pop_dialog()
                    self._show_saved_dialog()
                    self.page.update()
                else:
                    self._show_snack("Не вдалося зберегти доступні регіони", "red")

            dlg = ft.AlertDialog(
                title=ft.Text(f"Регіони доступу: {manager.get('full_name', '')}"),
                content=ft.Container(
                    width=self._dialog_width(520),
                    height=self._dialog_content_height(520),
                    content=ft.Column(
                        [hint, checks_block],
                        tight=True,
                        spacing=10,
                        scroll=ft.ScrollMode.ADAPTIVE,
                    ),
                ),
                actions=[
                    ft.TextButton(
                        "Скасувати", on_click=lambda _: self._close_dialog(dlg)
                    ),
                    ft.FilledButton("Зберегти", on_click=save_regions),
                ],
            )
            self.page.show_dialog(dlg)

        managers_for_table = (
            self.db._list("managers")
            if self.selected_company_id == ALL_COMPANIES_KEY
            else self.db.get_company_managers(
                self.selected_company_id, force_refresh=refresh_users
            )
        )

        # Cache catalog nodes per company for access display.
        catalog_nodes_by_company = {}
        if self.selected_company_id != ALL_COMPANIES_KEY:
            catalog_nodes_by_company[self.selected_company_id] = (
                self.db.get_company_catalog_nodes(
                    self.selected_company_id, force_refresh=refresh_catalog
                )
                or []
            )

        def _build_password_cell(mid: str) -> ft.Control:
            pw_field = ft.TextField(
                value="••••••",
                password=True,
                can_reveal_password=False,
                width=100,
                height=30,
                text_size=11,
                content_padding=ft.padding.symmetric(horizontal=6, vertical=2),
                border_radius=6,
            )
            status = ft.Text("", size=10, color="green")

            def _save_pw(_):
                new_pw = (pw_field.value or "").strip()
                if not new_pw or new_pw == "••••••":
                    status.value = "Введіть пароль"
                    status.color = "red"
                    if self.page:
                        self.page.update()
                    return
                ok = self.db.reset_manager_password(mid, new_pw)
                if ok:
                    status.value = "Збережено"
                    status.color = "green"
                    pw_field.value = "••••••"
                else:
                    status.value = "Помилка"
                    status.color = "red"
                if self.page:
                    self.page.update()

            return ft.Column(
                [
                    pw_field,
                    ft.Row(
                        [
                            ft.OutlinedButton(
                                "Зберегти",
                                height=24,
                                style=ft.ButtonStyle(
                                    text_style=ft.TextStyle(size=10),
                                    padding=ft.padding.symmetric(
                                        horizontal=6, vertical=0
                                    ),
                                ),
                                on_click=_save_pw,
                            ),
                            status,
                        ],
                        spacing=4,
                        tight=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=4,
                tight=True,
            )

        table_rows = []
        for m in managers_for_table:
            if not isinstance(m, dict):
                continue
            if not _manager_visible_for_selected_region(m):
                continue
            # Superadmin should never appear in lists and cannot be deleted.
            normalized_role = (m.get("role") or "").strip().lower().replace(" ", "")
            if normalized_role in {"суперадмін", "суперадмин", "superadmin"}:
                continue
            # Admin here is often also a category-managing "керівник" (not a
            # pure overseer role), so their own row stays visible/manageable
            # (e.g. as a source/target for "Передати доступи") - only the
            # self-destructive actions (block/delete) get disabled below.
            is_self_row = m.get("id") == self.user_data.get("id")

            manager_id = m.get("id")

            # Load access for this specific manager.
            manager_company_id = (
                self.selected_company_id
                if self.selected_company_id != ALL_COMPANIES_KEY
                else m.get("company_id")
            )
            manager_company_id = str(manager_company_id or "").strip()
            company_nodes = catalog_nodes_by_company.get(manager_company_id)
            if company_nodes is None:
                company_nodes = (
                    self.db.get_company_catalog_nodes(
                        manager_company_id, force_refresh=refresh_catalog
                    )
                    or []
                )
                catalog_nodes_by_company[manager_company_id] = company_nodes

            node_by_id = {
                str(n.get("id") or "").strip(): n
                for n in company_nodes
                if isinstance(n, dict) and n.get("id")
            }

            scope_ids = set(
                self.db.get_manager_scope_node_ids(
                    manager_id,
                    company_id=manager_company_id,
                    permission="view",
                )
            )

            if not scope_ids:
                legacy_cat_ids = set(
                    self.db.get_manager_category_ids(
                        manager_id,
                        company_id=manager_company_id,
                    )
                )
                scope_ids = {
                    str(cat_id).strip()
                    for cat_id in legacy_cat_ids
                    if str(cat_id).strip()
                }

            access_paths_by_category = {}
            uncategorized_paths = set()
            for scope_id in scope_ids:
                node = node_by_id.get(str(scope_id or "").strip())
                if not isinstance(node, dict):
                    continue

                leaf_name = str(node.get("name") or "").strip()
                leaf_type = str(node.get("node_type") or "").strip().lower()
                is_level_like = leaf_type in {
                    "subcategory",
                    "brand",
                    "series",
                } or leaf_type.startswith("level_")
                if leaf_type != "category" and not is_level_like:
                    continue

                current = node
                visited = set()
                category_name = ""
                chain_from_leaf = []
                while isinstance(current, dict):
                    current_id = str(current.get("id") or "").strip()
                    if not current_id or current_id in visited:
                        break
                    visited.add(current_id)
                    current_name = str(current.get("name") or "").strip()
                    if current_name:
                        chain_from_leaf.append(current_name)
                    if (
                        str(current.get("node_type") or "").strip().lower()
                        == "category"
                    ):
                        category_name = str(current.get("name") or "").strip()
                        break
                    parent_id = str(current.get("parent_id") or "").strip()
                    if not parent_id:
                        break
                    current = node_by_id.get(parent_id)

                full_path = tuple(reversed(chain_from_leaf))
                if category_name:
                    access_paths_by_category.setdefault(category_name, set()).add(
                        full_path if full_path else (category_name,)
                    )
                elif leaf_name:
                    uncategorized_paths.add(full_path if full_path else (leaf_name,))

            access_preview_controls = []
            tooltip_parts = []
            sorted_categories = sorted(
                access_paths_by_category.keys(), key=lambda x: (x or "").lower()
            )

            def _path_sort_key(path_tuple):
                path_list = [str(x or "") for x in (path_tuple or ())]
                return (len(path_list), " - ".join(path_list).lower())

            preview_limit = 2
            for category_name in sorted_categories:
                category_paths = sorted(
                    access_paths_by_category.get(category_name, set()),
                    key=_path_sort_key,
                )
                block_lines = [category_name]

                # Build a nested tree so children are rendered directly under their parent.
                levels_tree = {}
                for path in category_paths:
                    if len(path) <= 1:
                        continue
                    levels = [
                        str(x or "").strip() for x in path[1:] if str(x or "").strip()
                    ]
                    if not levels:
                        continue
                    cursor = levels_tree
                    for level_name in levels:
                        cursor = cursor.setdefault(level_name, {})

                def _append_tree_lines(tree_node, depth):
                    for level_name in sorted(
                        tree_node.keys(), key=lambda x: str(x).lower()
                    ):
                        arrows = " ".join(["->"] * depth)
                        block_lines.append(f"{arrows} {level_name}")
                        _append_tree_lines(tree_node[level_name], depth + 1)

                _append_tree_lines(levels_tree, 1)
                tooltip_parts.append("\n".join(block_lines))

            for category_name in sorted_categories[:preview_limit]:
                access_preview_controls.append(
                    ft.Text(
                        category_name,
                        size=10,
                        weight=ft.FontWeight.W_600,
                        color="#334155",
                        no_wrap=False,
                    )
                )

            remaining_categories = len(sorted_categories) - preview_limit
            if remaining_categories > 0:
                access_preview_controls.append(
                    ft.Text(
                        f"+{remaining_categories} ще категорій",
                        size=9,
                        color="grey600",
                        italic=True,
                    )
                )

            if uncategorized_paths:
                misc_lines = ["Без категорії"]
                for path in sorted(uncategorized_paths, key=_path_sort_key):
                    misc_lines.append(" - ".join(path))
                tooltip_parts.append("\n".join(misc_lines))
                if not access_preview_controls:
                    access_preview_controls.append(
                        ft.Text(
                            "Без категорії",
                            size=10,
                            weight=ft.FontWeight.W_600,
                            color="#334155",
                        )
                    )

            access_text = "\n\n".join(tooltip_parts) if tooltip_parts else "немає"
            if not access_preview_controls:
                access_preview_controls = [ft.Text("немає", size=10, color="grey600")]

            def del_click(_, manager=m):
                dlgdel = ft.AlertDialog(
                    modal=True,
                    title=ft.Text("Видалити користувача"),
                    content=ft.Text(
                        "Дію неможливо скасувати. Будуть прибрані лише запис менеджера та налаштування його доступів; "
                        "компанія, товари та замовлення не зникають.\n\n"
                        f"Ім'я: {manager.get('full_name', '')}\n"
                        f"Роль: {manager.get('role', '')}\n"
                        f"Email: {manager.get('email', '')}"
                    ),
                    actions=[
                        ft.TextButton(
                            "Скасувати", on_click=lambda _: self._close_dialog(dlgdel)
                        ),
                        ft.FilledButton(
                            "Так, видалити назавжди",
                            bgcolor=ft.Colors.RED_700,
                            on_click=lambda _e: _run_confirmed_delete(),
                        ),
                    ],
                )

                def _run_confirmed_delete():
                    self._close_dialog(dlgdel)
                    manager_email = (
                        manager.get("email") if isinstance(manager, dict) else None
                    )
                    ok = self.db.delete_manager_profile(manager.get("id"))
                    if ok:
                        if manager_email:
                            self._remove_from_quick_accounts(manager_email)
                        self._show_snack("Користувача видалено", "blue")
                        self.show_users(None)
                    else:
                        self._show_snack("Не вдалося видалити", "red")

                self.page.show_dialog(dlgdel)

            def block_toggle_click(_, manager=m):
                is_blocked = bool(manager.get("is_blocked"))
                action = "розблокувати" if is_blocked else "заблокувати"
                dlg_blk = ft.AlertDialog(
                    modal=True,
                    title=ft.Text("Блокування користувача"),
                    content=ft.Text(
                        f"Ви хочете {action} користувача?\n\n"
                        f"{manager.get('full_name', '')} ({manager.get('role', '')})\n"
                        + (
                            "Заблокований користувач не зможе входити в систему."
                            if not is_blocked
                            else ""
                        )
                    ),
                    actions=[
                        ft.TextButton(
                            "Скасувати", on_click=lambda _: self._close_dialog(dlg_blk)
                        ),
                        ft.FilledButton(
                            "Підтвердити",
                            bgcolor=(
                                ft.Colors.ORANGE_700
                                if not is_blocked
                                else ft.Colors.GREEN_700
                            ),
                            on_click=lambda _: _do_block_toggle(),
                        ),
                    ],
                )

                def _do_block_toggle():
                    self._close_dialog(dlg_blk)
                    ok = self.db.set_manager_blocked(manager.get("id"), not is_blocked)
                    if ok:
                        self._show_snack(
                            "Розблоковано" if is_blocked else "Заблоковано",
                            "green" if is_blocked else "orange",
                        )
                        self.show_users(None)
                    else:
                        self._show_snack("Не вдалося змінити статус", "red")

                self.page.show_dialog(dlg_blk)

            def transfer_access_click(_, manager=m):
                """Open dialog to pick target manager and partially transfer scopes."""
                company_id = manager.get("company_id") or self.selected_company_id
                other_managers = [
                    om
                    for om in managers_for_table
                    if isinstance(om, dict)
                    and om.get("id") != manager.get("id")
                    and (
                        om.get("company_id") == company_id
                        or self.selected_company_id == ALL_COMPANIES_KEY
                    )
                ]
                if not other_managers:
                    self._show_snack("Немає інших користувачів для передачі", "orange")
                    return

                source_manager_id = manager.get("id")
                all_nodes = self.db.get_company_catalog_nodes(
                    company_id, force_refresh=refresh_catalog
                )
                regions_raw = self.db.get_company_regions(
                    company_id, force_refresh=refresh_company
                )
                regions = [
                    r for r in regions_raw if isinstance(r, dict) and r.get("id")
                ]
                regions_by_id = {r.get("id"): r.get("name", "") for r in regions}
                selected_ids = set(
                    self.db.get_manager_scope_node_ids(
                        source_manager_id, company_id=company_id, permission="view"
                    )
                )
                if not selected_ids:
                    legacy_cat_ids = set(
                        self.db.get_manager_category_ids(source_manager_id)
                    )
                    for node in all_nodes:
                        if not isinstance(node, dict):
                            continue
                        legacy = str(node.get("legacy_id") or "").strip()
                        if (
                            legacy.startswith("cat:")
                            and legacy.split(":", 1)[1] in legacy_cat_ids
                        ):
                            selected_ids.add(node.get("id"))

                nodes_by_id = {
                    str(node.get("id")): node
                    for node in all_nodes
                    if isinstance(node, dict) and node.get("id")
                }
                children_of_all = {}
                for node in all_nodes:
                    if not isinstance(node, dict):
                        continue
                    parent_id = str(node.get("parent_id") or "").strip()
                    node_id = str(node.get("id") or "").strip()
                    if parent_id and node_id:
                        children_of_all.setdefault(parent_id, []).append(node_id)

                allowed_transfer_ids = set()
                stack = [
                    str(node_id) for node_id in selected_ids if str(node_id).strip()
                ]
                while stack:
                    current_id = str(stack.pop() or "").strip()
                    if not current_id or current_id in allowed_transfer_ids:
                        continue
                    allowed_transfer_ids.add(current_id)
                    stack.extend(children_of_all.get(current_id, []))

                visible_node_ids = set(allowed_transfer_ids)
                for node_id in list(allowed_transfer_ids):
                    current_node = nodes_by_id.get(node_id)
                    visited = set()
                    while current_node and current_node.get("id"):
                        current_node_id = str(current_node.get("id") or "").strip()
                        if not current_node_id or current_node_id in visited:
                            break
                        visited.add(current_node_id)
                        visible_node_ids.add(current_node_id)
                        current_node = nodes_by_id.get(
                            str(current_node.get("parent_id") or "").strip()
                        )

                target_dd = ft.Dropdown(
                    label="Передати доступи до",
                    border_radius=10,
                    width=360,
                    options=[
                        ft.dropdown.Option(
                            key=om.get("id"),
                            text=f"{om.get('full_name', '')} ({om.get('role', '')})",
                        )
                        for om in other_managers
                    ],
                )
                region_f = ft.Dropdown(
                    label="Регіон",
                    border_radius=10,
                    options=(
                        (
                            [
                                ft.dropdown.Option(
                                    key=ALL_REGIONS_KEY, text="Всі регіони"
                                )
                            ]
                            + [
                                ft.dropdown.Option(key=r.get("id"), text=r.get("name"))
                                for r in regions
                            ]
                        )
                        if len(regions) > 1
                        else [
                            ft.dropdown.Option(key=r.get("id"), text=r.get("name"))
                            for r in regions
                        ]
                    ),
                    value=(
                        ALL_REGIONS_KEY
                        if len(regions) > 1
                        else next((r.get("id") for r in regions), None)
                    ),
                    width=320,
                    visible=len(regions) > 1,
                )
                tree_col = ft.Column([], spacing=2)
                current_tree_children = {}
                current_transferable_ids = set()
                mode_dd = ft.Dropdown(
                    label="Режим",
                    border_radius=10,
                    width=360,
                    value="move",
                    options=[
                        ft.dropdown.Option(
                            key="move", text="Передача (видалити попереднього)"
                        ),
                        ft.dropdown.Option(key="share", text="Додати співвласника"),
                    ],
                )
                all_checks = {}
                transfer_status = ft.Text(
                    "",
                    size=11,
                    color=ft.Colors.RED_700,
                    visible=False,
                )
                keep_hint = ft.Text(
                    "Передача: видалить попереднього та назначить нового власника. "
                    "Додати співвласника: залишить попереднього та додасть нового (до 5 на вузол). "
                    "Дані не копіюються.",
                    size=11,
                    color="grey700",
                )

                def rebuild_transfer_tree(region_id=None):
                    nonlocal current_tree_children, current_transferable_ids
                    all_checks.clear()
                    nodes = [
                        node
                        for node in all_nodes
                        if isinstance(node, dict)
                        and str(node.get("id") or "").strip() in visible_node_ids
                    ]
                    if region_id and region_id != ALL_REGIONS_KEY:
                        nodes = [
                            node
                            for node in nodes
                            if node.get("node_type") != "category"
                            or node.get("region_id") == region_id
                        ]

                    current_transferable_ids = {
                        str(node.get("id") or "").strip()
                        for node in nodes
                        if isinstance(node, dict)
                        and str(node.get("id") or "").strip() in allowed_transfer_ids
                    }

                    children_of = {}
                    for node in nodes:
                        parent_id = str(node.get("parent_id") or "").strip()
                        node_id = str(node.get("id") or "").strip()
                        if parent_id and node_id:
                            children_of.setdefault(parent_id, []).append(node)
                    current_tree_children = {
                        parent_id: [
                            str(child.get("id") or "").strip()
                            for child in (child_nodes or [])
                            if isinstance(child, dict)
                            and str(child.get("id") or "").strip()
                        ]
                        for parent_id, child_nodes in children_of.items()
                    }

                    def _subtree_ids(root_id):
                        collected = set()
                        stack_ids = [root_id]
                        while stack_ids:
                            current_id = stack_ids.pop()
                            if not current_id or current_id in collected:
                                continue
                            if current_id in allowed_transfer_ids:
                                collected.add(current_id)
                            for child in children_of.get(current_id, []):
                                child_id = str(child.get("id") or "").strip()
                                if child_id:
                                    stack_ids.append(child_id)
                        return collected

                    def _make_cb(node):
                        node_id = str(node.get("id") or "").strip()
                        label = node.get("name", node_id)
                        if node.get("node_type") == "category":
                            region_id_local = node.get("region_id")
                            if region_id_local and region_id_local in regions_by_id:
                                label = f"{label} ({regions_by_id[region_id_local]})"
                        is_transferable = node_id in allowed_transfer_ids

                        def _cascade(current_cb):
                            if not is_transferable:
                                return
                            value = bool(getattr(current_cb, "value", False))
                            for descendant_id in _subtree_ids(node_id):
                                descendant_cb = all_checks.get(descendant_id)
                                if descendant_cb is not None and not getattr(
                                    descendant_cb, "disabled", False
                                ):
                                    descendant_cb.value = value

                        cb, cb_row = self._wide_checkbox_row(
                            label,
                            node_id in selected_ids,
                            on_change=_cascade,
                        )
                        cb.disabled = not is_transferable
                        all_checks[node_id] = cb
                        return cb_row

                    def _build_node_tile(node, indent_left=4):
                        node_id = str(node.get("id") or "").strip()
                        header_row = ft.Row([_make_cb(node)])
                        child_nodes = children_of.get(node_id, [])
                        if child_nodes:
                            return ft.ExpansionTile(
                                title=header_row,
                                expanded=False,
                                controls=[
                                    _build_node_tile(
                                        child, indent_left=indent_left + 10
                                    )
                                    for child in child_nodes
                                ],
                                controls_padding=ft.padding.only(left=12),
                            )
                        return ft.Container(
                            padding=ft.padding.only(left=indent_left, top=2, bottom=2),
                            content=header_row,
                        )

                    categories = [
                        node for node in nodes if node.get("node_type") == "category"
                    ]
                    categories.sort(
                        key=lambda node: (
                            str(
                                regions_by_id.get(node.get("region_id"), "") or ""
                            ).lower(),
                            str(node.get("name") or "").lower(),
                        )
                    )
                    grouped = {}
                    for category in categories:
                        region_key = str(category.get("region_id") or "").strip()
                        grouped.setdefault(region_key, []).append(category)

                    tree_controls = []
                    if grouped:
                        ordered_region_ids = sorted(
                            grouped.keys(),
                            key=lambda rid: str(
                                regions_by_id.get(rid, rid or "Без регіону")
                            ).lower(),
                        )
                        for rid in ordered_region_ids:
                            region_name = (
                                str(regions_by_id.get(rid) or "").strip()
                                or "Без регіону"
                            )
                            region_children = [
                                _build_node_tile(category)
                                for category in grouped.get(rid, [])
                            ]
                            tree_controls.append(
                                ft.ExpansionTile(
                                    title=ft.Row(
                                        [
                                            ft.Text(
                                                region_name,
                                                size=12,
                                                weight=ft.FontWeight.W_600,
                                                color="#334155",
                                            )
                                        ]
                                    ),
                                    expanded=False,
                                    controls=region_children,
                                    controls_padding=ft.padding.only(left=8),
                                )
                            )
                    if not tree_controls:
                        tree_controls = [
                            ft.Text(
                                "У джерельного менеджера немає доступних для передачі вузлів",
                                color="grey600",
                            )
                        ]
                    tree_col.controls = tree_controls

                def on_transfer_region_change(_):
                    rebuild_transfer_tree(region_f.value)
                    if self.page:
                        self.page.update()

                region_f.on_select = on_transfer_region_change
                rebuild_transfer_tree(region_f.value)

                def _norm_supplier_name(raw_name):
                    return " ".join(str(raw_name or "").strip().split()).lower()

                def _execute_transfer(effective_node_ids, is_move, merge_groups):
                    transfer_btn.disabled = True
                    if self.page:
                        self.page.update()
                    try:
                        ok, msg = self.db.transfer_manager_access(
                            manager.get("id"),
                            target_dd.value,
                            effective_node_ids,
                            remove_from_source=is_move,
                        )
                        if not ok:
                            transfer_status.value = f"Помилка передачі: {msg}"
                            transfer_status.visible = True
                            self._show_snack(f"Помилка передачі: {msg}", "red")
                            return
                        merge_summary = ""
                        if merge_groups:
                            mres = self.db.apply_supplier_merge_groups(
                                company_id, merge_groups, node_ids=effective_node_ids
                            )
                            merged = int(mres.get("merged_groups", 0) or 0)
                            if merged:
                                merge_summary = (
                                    f" Об'єднано груп постачальників: {merged}."
                                )
                        self._close_dialog(dlg_tr)
                        self._show_saved_dialog(f"Доступи передано.\n{msg}{merge_summary}")
                    except Exception as exc:
                        logger.exception("Manager access transfer click failed")
                        transfer_status.value = f"Помилка передачі: {exc}"
                        transfer_status.visible = True
                        self._show_snack(f"Помилка передачі: {exc}", "red")
                    finally:
                        transfer_btn.disabled = False
                        if self.page:
                            self.page.update()

                def _open_supplier_merge_review(effective_node_ids, is_move):
                    preview = self.db.get_suppliers_for_transfer_preview(
                        manager.get("id"), target_dd.value, effective_node_ids
                    )
                    source_items = [
                        s for s in (preview.get("source") or []) if isinstance(s, dict)
                    ]
                    target_items = [
                        t for t in (preview.get("target") or []) if isinstance(t, dict)
                    ]

                    if not source_items or not target_items:
                        # Nothing on one side to possibly collide with - nothing
                        # for the admin to decide, proceed straight away.
                        _execute_transfer(effective_node_ids, is_move, [])
                        return

                    target_manager_name = next(
                        (
                            om.get("full_name", "")
                            for om in other_managers
                            if om.get("id") == target_dd.value
                        ),
                        "цільового менеджера",
                    )

                    target_names = {
                        _norm_supplier_name(t.get("name")) for t in target_items
                    }
                    source_names = {
                        _norm_supplier_name(s.get("name")) for s in source_items
                    }
                    names_by_id = {
                        str(i.get("id")): i.get("name") or ""
                        for i in (source_items + target_items)
                        if str(i.get("id") or "").strip()
                    }
                    # A supplier can already be co-owned by both managers (same
                    # DB row shows up on both sides of the transfer) — that's
                    # not a duplicate to merge, it's already one record, so
                    # exclude it from the checklist entirely instead of letting
                    # the admin try and fail to "merge" a row with itself.
                    already_shared_ids = {
                        str(s.get("id")) for s in source_items if s.get("id")
                    } & {str(t.get("id")) for t in target_items if t.get("id")}

                    groups: list[dict] = []
                    # Keyed by a unique per-row key, not the bare supplier id:
                    # the same supplier id can legitimately appear in both the
                    # source and target lists (a supplier can be owned by more
                    # than one manager), so id-only keys would collide and
                    # silently drop one side's checkbox.
                    item_checks: dict[str, ft.Checkbox] = {}
                    checkbox_sid: dict[str, str] = {}
                    groups_view = ft.Column([], spacing=4)
                    group_name_f = ft.TextField(
                        label="Фінальна назва для об'єднаних", width=460
                    )
                    merge_status = ft.Text(
                        "", size=11, color=ft.Colors.RED_700, visible=False
                    )

                    def _refresh_groups_view():
                        rows = [
                            ft.Text(
                                f"• {g.get('final_name')}: "
                                + ", ".join(g.get("names") or []),
                                size=12,
                            )
                            for g in groups
                        ]
                        groups_view.controls = rows or [
                            ft.Text("Груп ще не додано", size=12, color="grey600")
                        ]

                    def _build_supplier_row(item, is_source):
                        sid = str(item.get("id") or "").strip()
                        label = item.get("name") or sid
                        if is_source:
                            label = (
                                f"{label} ({int(item.get('products_count') or 0)} тов.)"
                            )
                        if sid and sid in already_shared_ids:
                            return ft.Container(
                                content=ft.Row(
                                    [
                                        ft.Icon(
                                            ft.Icons.CHECK_CIRCLE,
                                            size=16,
                                            color="green600",
                                        ),
                                        ft.Text(label, size=13),
                                        ft.Text(
                                            "вже спільний — дії не потрібні",
                                            size=10,
                                            color="green700",
                                        ),
                                    ],
                                    spacing=6,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                                padding=ft.padding.symmetric(horizontal=8, vertical=8),
                                border_radius=8,
                                bgcolor="#E8F5E9",
                            )
                        cb, row = self._wide_checkbox_row(label, False)
                        nname = _norm_supplier_name(item.get("name"))
                        other_names = target_names if is_source else source_names
                        if nname and nname in other_names:
                            row.bgcolor = "#FEF3C7"
                        key = f"{'s' if is_source else 't'}:{len(item_checks)}:{sid}"
                        item_checks[key] = cb
                        checkbox_sid[key] = sid
                        return row

                    def _add_group(_):
                        checked_keys = [
                            key for key, cb in item_checks.items() if cb.value
                        ]
                        checked: list[str] = []
                        seen_sids: set[str] = set()
                        for key in checked_keys:
                            sid = checkbox_sid[key]
                            if sid not in seen_sids:
                                seen_sids.add(sid)
                                checked.append(sid)
                        if len(checked) < 2:
                            merge_status.value = (
                                "Позначте щонайменше два постачальники для об'єднання"
                            )
                            merge_status.visible = True
                            if self.page:
                                self.page.update()
                            return
                        final_name = str(group_name_f.value or "").strip()
                        if not final_name:
                            checked_names = {
                                str(names_by_id.get(sid) or "").strip()
                                for sid in checked
                                if str(names_by_id.get(sid) or "").strip()
                            }
                            if len(checked_names) == 1:
                                final_name = next(iter(checked_names))
                            else:
                                merge_status.value = "Введіть загальну назву постачальника"
                                merge_status.visible = True
                                if self.page:
                                    self.page.update()
                                return
                        merge_status.visible = False
                        groups.append(
                            {
                                "member_ids": checked,
                                "final_name": final_name,
                                "names": [
                                    str(names_by_id.get(sid) or sid)
                                    for sid in checked
                                ],
                            }
                        )
                        for key in checked_keys:
                            item_checks[key].value = False
                        group_name_f.value = ""
                        _refresh_groups_view()
                        if self.page:
                            self.page.update()

                    def _continue_after_merge(_):
                        payload = [
                            {
                                "member_ids": g["member_ids"],
                                "final_name": g["final_name"],
                            }
                            for g in groups
                        ]
                        self._close_dialog(dlg_merge)
                        _execute_transfer(effective_node_ids, is_move, payload)

                    def _cancel_merge(_):
                        self._close_dialog(dlg_merge)

                    source_rows = [
                        _build_supplier_row(s, True) for s in source_items
                    ]
                    target_rows = [
                        _build_supplier_row(t, False) for t in target_items
                    ]
                    _refresh_groups_view()

                    dlg_merge = ft.AlertDialog(
                        modal=True,
                        title=ft.Text("Звірка постачальників перед передачею"),
                        content=ft.Container(
                            width=self._dialog_width(780),
                            height=self._dialog_content_height(680),
                            content=ft.Column(
                                [
                                    ft.Text(
                                        "Жовтим підсвічені постачальники з однаковою назвою в "
                                        "обох списках — це лише підказка, об'єднання завжди "
                                        "вирішуєте ви. Позначте потрібні постачальники з обох "
                                        "колонок (навіть якщо назви геть різні, але це той самий "
                                        "постачальник), впишіть фінальну (стандартизовану) назву "
                                        "і натисніть «Додати групу». Непозначені постачальники "
                                        "передадуться/залишаться без змін.",
                                        size=11,
                                        color="grey700",
                                    ),
                                    ft.Row(
                                        [
                                            ft.Column(
                                                [
                                                    ft.Text(
                                                        "Постачальники, що передаються",
                                                        weight=ft.FontWeight.W_600,
                                                        size=12,
                                                    )
                                                ]
                                                + source_rows,
                                                spacing=2,
                                                width=350,
                                                scroll=ft.ScrollMode.ADAPTIVE,
                                            ),
                                            ft.Column(
                                                [
                                                    ft.Text(
                                                        f"Постачальники: {target_manager_name}",
                                                        weight=ft.FontWeight.W_600,
                                                        size=12,
                                                    )
                                                ]
                                                + target_rows,
                                                spacing=2,
                                                width=350,
                                                scroll=ft.ScrollMode.ADAPTIVE,
                                            ),
                                        ],
                                        spacing=16,
                                        vertical_alignment=ft.CrossAxisAlignment.START,
                                    ),
                                    merge_status,
                                    ft.Row(
                                        [
                                            group_name_f,
                                            ft.FilledButton(
                                                "Додати групу", on_click=_add_group
                                            ),
                                        ]
                                    ),
                                    ft.Text(
                                        "Групи об'єднання:",
                                        size=12,
                                        weight=ft.FontWeight.W_600,
                                    ),
                                    groups_view,
                                ],
                                spacing=10,
                                tight=True,
                                scroll=ft.ScrollMode.ADAPTIVE,
                            ),
                        ),
                        actions=[
                            ft.TextButton("Скасувати", on_click=_cancel_merge),
                            ft.FilledButton(
                                "Продовжити передачу", on_click=_continue_after_merge
                            ),
                        ],
                    )
                    self.page.show_dialog(dlg_merge)

                def _do_transfer(_):
                    transfer_status.visible = False
                    transfer_status.value = ""
                    if not target_dd.value:
                        transfer_status.value = (
                            "Оберіть користувача, якому передати доступи"
                        )
                        transfer_status.visible = True
                        self._show_snack("Оберіть користувача", "orange")
                        return
                    selected_node_ids = [
                        node_id
                        for node_id, cb in all_checks.items()
                        if cb.value and not getattr(cb, "disabled", False)
                    ]
                    selected_node_ids_set = {
                        str(node_id).strip()
                        for node_id in selected_node_ids
                        if str(node_id).strip()
                    }

                    def _has_unselected_transferable_descendant(node_id):
                        stack = list(current_tree_children.get(node_id, []))
                        visited = set()
                        while stack:
                            child_id = str(stack.pop() or "").strip()
                            if not child_id or child_id in visited:
                                continue
                            visited.add(child_id)
                            if (
                                child_id in current_transferable_ids
                                and child_id not in selected_node_ids_set
                            ):
                                return True
                            stack.extend(current_tree_children.get(child_id, []))
                        return False

                    effective_selected_node_ids = [
                        node_id
                        for node_id in selected_node_ids
                        if not _has_unselected_transferable_descendant(
                            str(node_id).strip()
                        )
                    ]
                    if not effective_selected_node_ids:
                        transfer_status.value = (
                            "Оберіть категорії або рівні для передачі"
                        )
                        transfer_status.visible = True
                        self._show_snack(
                            "Оберіть категорії або рівні для передачі", "orange"
                        )
                        return
                    is_move = str(mode_dd.value or "move").strip().lower() != "share"
                    _open_supplier_merge_review(effective_selected_node_ids, is_move)

                transfer_btn = ft.FilledButton("Передати", on_click=_do_transfer)

                dlg_tr = ft.AlertDialog(
                    modal=True,
                    title=ft.Text(
                        f"Передати доступи від: {manager.get('full_name', '')}"
                    ),
                    content=ft.Container(
                        width=self._dialog_width(620),
                        height=self._dialog_content_height(620),
                        content=ft.Column(
                            [
                                target_dd,
                                mode_dd,
                                region_f,
                                transfer_status,
                                keep_hint,
                                tree_col,
                            ],
                            spacing=10,
                            tight=True,
                            scroll=ft.ScrollMode.ADAPTIVE,
                        ),
                    ),
                    actions=[
                        ft.TextButton(
                            "Скасувати", on_click=lambda _: self._close_dialog(dlg_tr)
                        ),
                        transfer_btn,
                    ],
                )
                self.page.show_dialog(dlg_tr)

                async def _warm_transfer_rebuild():
                    await asyncio.sleep(0.06)
                    rebuild_transfer_tree(region_f.value)
                    if self.page:
                        self.page.update()

                with contextlib.suppress(Exception):
                    asyncio.create_task(_warm_transfer_rebuild())

            is_blocked = bool(m.get("is_blocked"))

            def show_access_details_click(_, manager=m, full_text=access_text):
                if (
                    not str(full_text or "").strip()
                    or str(full_text).strip() == "немає"
                ):
                    self._show_snack("Немає доступів для перегляду", "orange")
                    return
                dlg_access = ft.AlertDialog(
                    modal=True,
                    title=ft.Text(f"Доступи: {manager.get('full_name', '')}"),
                    content=ft.Container(
                        width=self._dialog_width(680),
                        height=self._dialog_content_height(620),
                        content=ft.Column(
                            [
                                ft.Text(
                                    full_text,
                                    size=12,
                                    selectable=True,
                                    no_wrap=False,
                                )
                            ],
                            scroll=ft.ScrollMode.ADAPTIVE,
                            tight=True,
                        ),
                    ),
                    actions=[
                        ft.TextButton(
                            "Закрити", on_click=lambda _: self._close_dialog(dlg_access)
                        )
                    ],
                )
                self.page.show_dialog(dlg_access)

            table_rows.append(
                ft.DataRow(
                    color="#FFF3E0" if is_blocked else None,
                    cells=[
                        ft.DataCell(
                            ft.Row(
                                [
                                    ft.Text(
                                        m.get("full_name", ""),
                                        color="grey600" if is_blocked else None,
                                    ),
                                    ft.Container(
                                        visible=is_blocked,
                                        padding=ft.padding.only(left=4),
                                        content=ft.Text("🚫", size=11),
                                    ),
                                ],
                                spacing=4,
                            )
                        ),
                        ft.DataCell(ft.Text(m.get("role", ""))),
                        ft.DataCell(ft.Text("Так" if m.get("is_approved") else "Ні")),
                        ft.DataCell(
                            ft.Text(
                                m.get("email") or "—",
                                size=12,
                                selectable=True,
                            )
                        ),
                        ft.DataCell(
                            ft.Text(
                                m.get("phone") or "—",
                                size=12,
                                selectable=True,
                            )
                        ),
                        ft.DataCell(
                            ft.Row(
                                [
                                    ft.Container(
                                        content=ft.Column(
                                            access_preview_controls,
                                            spacing=1,
                                            tight=True,
                                        ),
                                        width=150,
                                        tooltip=access_text,
                                    ),
                                    ft.IconButton(
                                        ft.Icons.VISIBILITY,
                                        tooltip="Показати всі доступи",
                                        icon_size=14,
                                        on_click=show_access_details_click,
                                    ),
                                ],
                                spacing=2,
                                vertical_alignment=ft.CrossAxisAlignment.START,
                            )
                        ),
                        ft.DataCell(
                            ft.Row(
                                [
                                    ft.IconButton(
                                        ft.Icons.DESKTOP_WINDOWS,
                                        tooltip="Відкрити як користувач",
                                        icon_size=16,
                                        on_click=lambda _, mid=m.get(
                                            "id"
                                        ): self._open_as_user(mid),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.CATEGORY,
                                        tooltip="Призначити доступи",
                                        icon_size=16,
                                        on_click=lambda _, manager=m: open_manager_access_scopes_dialog(
                                            manager
                                        ),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.REMOVE_CIRCLE_OUTLINE,
                                        tooltip="Прибрати доступи",
                                        icon_color="orange",
                                        icon_size=16,
                                        on_click=lambda _, manager=m: open_manager_remove_access_dialog(
                                            manager
                                        ),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.SWAP_HORIZ,
                                        tooltip="Передати доступи іншому",
                                        icon_size=16,
                                        on_click=lambda _, manager=m: transfer_access_click(
                                            None, manager
                                        ),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.GROUP,
                                        tooltip="Призначити керівників",
                                        icon_size=16,
                                        on_click=lambda _, manager=m: open_manager_supervisors_dialog(
                                            manager
                                        ),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.MAP,
                                        tooltip="Обмежити регіони",
                                        icon_size=16,
                                        on_click=lambda _, manager=m: open_manager_regions_dialog(
                                            manager
                                        ),
                                    ),
                                    ft.IconButton(
                                        (
                                            ft.Icons.BLOCK
                                            if not is_blocked
                                            else ft.Icons.LOCK_OPEN
                                        ),
                                        icon_color=(
                                            "orange" if not is_blocked else "green"
                                        ),
                                        tooltip=(
                                            "Не можна заблокувати самого себе"
                                            if is_self_row
                                            else (
                                                "Заблокувати"
                                                if not is_blocked
                                                else "Розблокувати"
                                            )
                                        ),
                                        icon_size=16,
                                        disabled=is_self_row,
                                        on_click=lambda _, manager=m: block_toggle_click(
                                            None, manager
                                        ),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.DELETE,
                                        icon_color="red",
                                        icon_size=16,
                                        tooltip=(
                                            "Не можна видалити самого себе"
                                            if is_self_row
                                            else None
                                        ),
                                        disabled=is_self_row,
                                        on_click=del_click,
                                    ),
                                ],
                                spacing=0,
                            )
                        ),
                        ft.DataCell(_build_password_cell(m.get("id", ""))),
                    ],
                )
            )

        def _make_collapsible_section(
            title, controls, initially_open=False, on_toggle=None
        ):
            body = ft.Column(controls, spacing=8, visible=initially_open)
            toggle_btn = ft.TextButton(
                title,
                icon=(
                    ft.Icons.KEYBOARD_ARROW_DOWN
                    if initially_open
                    else ft.Icons.KEYBOARD_ARROW_RIGHT
                ),
                style=ft.ButtonStyle(
                    padding=ft.padding.symmetric(horizontal=0, vertical=0)
                ),
            )

            def _toggle(_):
                body.visible = not body.visible
                toggle_btn.icon = (
                    ft.Icons.KEYBOARD_ARROW_DOWN
                    if body.visible
                    else ft.Icons.KEYBOARD_ARROW_RIGHT
                )
                if callable(on_toggle):
                    on_toggle(body.visible)
                body.update()
                toggle_btn.update()

            toggle_btn.on_click = _toggle
            return ft.Card(
                content=ft.Container(
                    padding=15,
                    content=ft.Column([toggle_btn, body], spacing=6),
                )
            )

        def _on_create_user_toggle(is_open):
            self._admin_create_user_open = is_open

        self.content_area.controls.extend(
            [
                ft.Text("Користувачі", size=24, weight="bold"),
                _make_collapsible_section(
                    "Створення користувача",
                    [
                        create_company_f,
                        company_name_f,
                        ft.Row(
                            [full_name_f, role_f], spacing=10, wrap=True, run_spacing=8
                        ),
                        ft.Row(
                            [email_f, phone_f], spacing=10, wrap=True, run_spacing=8
                        ),
                        ft.Row(
                            [password_f, parent_f], spacing=10, wrap=True, run_spacing=8
                        ),
                        ft.Text(
                            "Доступні регіони (мультивибір)", size=12, weight="bold"
                        ),
                        regions_checks_block,
                        ft.Text(
                            "Доступи до каталогу (мультивибір)", size=12, weight="bold"
                        ),
                        catalog_checks_block,
                        ft.FilledButton("Створити", on_click=create_click),
                    ],
                    initially_open=self._admin_create_user_open,
                    on_toggle=_on_create_user_toggle,
                ),
                ft.Divider(),
                ft.Text("Список компанії", weight="bold"),
                ft.DataTable(
                    columns=[
                        ft.DataColumn(ft.Text("Ім'я", size=11)),
                        ft.DataColumn(ft.Text("Роль", size=11)),
                        ft.DataColumn(ft.Text("Підтв.", size=11)),
                        ft.DataColumn(ft.Text("Email", size=11)),
                        ft.DataColumn(ft.Text("Телефон", size=11)),
                        ft.DataColumn(ft.Text("Доступи", size=11)),
                        ft.DataColumn(ft.Text("Дія", size=11)),
                        ft.DataColumn(ft.Text("Пароль", size=11)),
                    ],
                    column_spacing=8,
                    horizontal_margin=8,
                    heading_row_height=34,
                    data_row_min_height=36,
                    data_row_max_height=110,
                    rows=table_rows,
                ),
            ]
        )
        self.page.update()

    def show_hierarchy(self, e):
        self.current_tab = "hierarchy"
        self.content_area.controls.clear()
        managers = self.db.get_company_managers(self.selected_company_id)
        by_parent = {}
        by_id = {}
        for m in managers:
            if not isinstance(m, dict):
                continue
            by_id[m.get("id")] = m
            by_parent.setdefault(m.get("parent_manager_id"), []).append(m)

        for parent_id in by_parent:
            by_parent[parent_id].sort(key=lambda x: (x.get("full_name") or "").lower())

        def is_descendant(node_id, target_parent_id):
            stack = [target_parent_id]
            visited = set()
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                if current == node_id:
                    return True
                for child in by_parent.get(current, []):
                    cid = child.get("id")
                    if cid and cid not in visited:
                        stack.append(cid)
            return False

        def change_parent(moved_id, new_parent_id):
            if moved_id == new_parent_id:
                self._show_snack("Неможливо призначити себе собі", "orange")
                return
            if new_parent_id and is_descendant(moved_id, new_parent_id):
                self._show_snack("Неможливо: цикл у ієрархії", "red")
                return
            ok = self.db.set_manager_parent(moved_id, new_parent_id)
            if ok:
                self._show_snack("Ієрархію оновлено", "green")
                self.show_hierarchy(None)

        def render_node(manager, level=0):
            manager_id = manager.get("id")
            if not manager_id:
                return []

            row = ft.DragTarget(
                group="hierarchy",
                on_accept=lambda e, target_id=manager_id: change_parent(
                    e.src_id, target_id
                ),
                content=ft.Container(
                    margin=ft.margin.only(left=level * 24, top=4, bottom=4),
                    padding=10,
                    border=ft.border.all(1, "#E5E7EB"),
                    border_radius=8,
                    content=ft.Row(
                        [
                            ft.Draggable(
                                group="hierarchy",
                                data=manager_id,
                                content=ft.Row(
                                    [
                                        ft.Icon(
                                            ft.Icons.DRAG_INDICATOR,
                                            size=16,
                                            color="grey600",
                                        ),
                                        ft.Text(
                                            f"{manager.get('full_name', '')} ({manager.get('role', '')})",
                                            weight="bold",
                                        ),
                                    ],
                                    spacing=6,
                                ),
                                content_when_dragging=ft.Text(
                                    f"{manager.get('full_name', '')}"
                                ),
                            ),
                            ft.OutlinedButton(
                                "В корінь",
                                on_click=lambda _, mid=manager_id: change_parent(
                                    mid, None
                                ),
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                ),
            )

            nodes = [row]
            for child in by_parent.get(manager_id, []):
                nodes.extend(render_node(child, level + 1))
            return nodes

        roots = by_parent.get(None, []) + by_parent.get("", [])
        controls = [ft.Text("Ієрархія (drag & drop)", size=24, weight="bold")]
        if not roots:
            controls.append(ft.Text("Немає даних", color="grey600"))
        else:
            for root in roots:
                controls.extend(render_node(root, 0))

        self.content_area.controls.extend(controls)
        self.page.update()

    def _open_sa_subcategory_dialog(self):
        companies = self.db.get_all_companies()
        company_options = [
            ft.dropdown.Option(key=c.get("id"), text=c.get("name"))
            for c in companies
            if isinstance(c, dict) and c.get("id")
        ]
        default_company_id = self.selected_company_id or next(
            (c.get("id") for c in companies if isinstance(c, dict) and c.get("id")),
            None,
        )

        company_f = ft.Dropdown(
            label="Компанія",
            border_radius=10,
            options=company_options,
            value=default_company_id,
        )
        region_f = ft.Dropdown(label="Регіон", border_radius=10, options=[])
        category_f = ft.Dropdown(label="Категорія", border_radius=10, options=[])
        no_categories_hint = ft.Text(
            "У вибраному регіоні немає категорій",
            size=12,
            color="grey600",
            visible=False,
        )
        name_f = ft.TextField(label="Назва підкатегорії", border_radius=10)

        def load_regions(company_id):
            regions = self.db.get_company_regions(company_id)
            region_options = [
                ft.dropdown.Option(key=r.get("id"), text=r.get("name"))
                for r in regions
                if isinstance(r, dict) and r.get("id")
            ]
            if len(region_options) > 1:
                region_f.options = [
                    ft.dropdown.Option(key=ALL_REGIONS_KEY, text="Всі регіони")
                ]
                region_f.options.extend(region_options)
                region_f.value = ALL_REGIONS_KEY
            else:
                region_f.options = region_options
                region_f.value = region_options[0].key if region_options else None
            region_f.visible = len(region_options) > 1

        def load_categories(company_id, region_id):
            target_region_id = None if region_id == ALL_REGIONS_KEY else region_id
            categories = self.db.get_company_categories(company_id, target_region_id)
            category_f.options = [
                ft.dropdown.Option(
                    key=c.get("id"), text=self._format_category_label(c, company_id)
                )
                for c in categories
                if isinstance(c, dict) and c.get("id")
            ]
            category_f.value = next(
                (
                    c.get("id")
                    for c in categories
                    if isinstance(c, dict) and c.get("id")
                ),
                None,
            )
            has_categories = bool(category_f.options)
            category_f.disabled = not has_categories
            no_categories_hint.visible = not has_categories

        def on_company_change(_):
            load_regions(company_f.value)
            load_categories(company_f.value, region_f.value)
            if self.page:
                self.page.update()

        def on_region_change(_):
            load_categories(company_f.value, region_f.value)
            if self.page:
                self.page.update()

        company_f.on_select = on_company_change
        region_f.on_select = on_region_change
        if default_company_id:
            load_regions(default_company_id)
            load_categories(default_company_id, region_f.value)

        def save_click(_):
            self._clear_field_errors(company_f, region_f, category_f, name_f)
            category_id = category_f.value
            sub_name = (name_f.value or "").strip()
            if not company_f.value or not category_id or not sub_name:
                if not company_f.value:
                    self._mark_required(company_f, "Оберіть компанію")
                if not region_f.value:
                    self._mark_required(region_f, "Оберіть регіон")
                if not category_id:
                    self._mark_required(category_f, "Оберіть категорію")
                if not sub_name:
                    self._mark_required(name_f, "Введіть назву")
                self._show_snack(
                    "Оберіть компанію, регіон, категорію та введіть назву", "orange"
                )
                if self.page:
                    self.page.update()
                return
            if self.db.subcategory_name_exists(category_id, sub_name):
                self._mark_required(name_f, "Така підкатегорія вже існує")
                self._show_snack("Така підкатегорія вже існує", "orange")
                if self.page:
                    self.page.update()
                return
            if self.db.add_subcategory(category_id, sub_name):
                self._show_snack("Підкатегорію створено", "green")
                self.page.pop_dialog()
                if company_f.value == self.selected_company_id:
                    self.show_categories(None)
                else:
                    self.page.update()
            else:
                self._show_snack("Не вдалося створити підкатегорію", "red")

        dlg = ft.AlertDialog(
            title=ft.Text("Створити підкатегорію"),
            content=ft.Column(
                [company_f, region_f, category_f, no_categories_hint, name_f],
                tight=True,
                spacing=10,
                width=460,
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: self._close_dialog(dlg)),
                ft.FilledButton("Зберегти", on_click=save_click),
            ],
        )
        self.page.show_dialog(dlg)

    def _open_sa_brand_dialog(self):
        tm_types = [
            "Загальна торгова марка",
            "Ексклюзивна торгова марка",
            "Власна торгова марка",
        ]
        companies = self.db.get_all_companies()
        company_options = [
            ft.dropdown.Option(key=c.get("id"), text=c.get("name"))
            for c in companies
            if isinstance(c, dict) and c.get("id")
        ]
        default_company_id = self.selected_company_id or next(
            (c.get("id") for c in companies if isinstance(c, dict) and c.get("id")),
            None,
        )

        company_f = ft.Dropdown(
            label="Компанія",
            border_radius=10,
            options=company_options,
            value=default_company_id,
        )
        region_f = ft.Dropdown(label="Регіон", border_radius=10, options=[])
        category_f = ft.Dropdown(label="Категорія", border_radius=10, options=[])
        no_categories_hint = ft.Text(
            "У вибраному регіоні немає категорій",
            size=12,
            color="grey600",
            visible=False,
        )
        subcategory_f = ft.Dropdown(label="Підкатегорія", border_radius=10, options=[])
        series_f = ft.Dropdown(label="Серія", border_radius=10, options=[])
        name_f = ft.TextField(label="Назва бренду", value="No_Name", border_radius=10)
        tm_type_f = ft.Dropdown(
            label="Тип ТМ",
            border_radius=10,
            options=[ft.dropdown.Option(x) for x in tm_types],
            value=tm_types[0],
        )

        def load_regions(company_id):
            regions = self.db.get_company_regions(company_id)
            region_options = [
                ft.dropdown.Option(key=r.get("id"), text=r.get("name"))
                for r in regions
                if isinstance(r, dict) and r.get("id")
            ]
            if len(region_options) > 1:
                region_f.options = [
                    ft.dropdown.Option(key=ALL_REGIONS_KEY, text="Всі регіони")
                ]
                region_f.options.extend(region_options)
                region_f.value = ALL_REGIONS_KEY
            else:
                region_f.options = region_options
                region_f.value = region_options[0].key if region_options else None
            region_f.visible = len(region_options) > 1

        def load_categories(company_id, region_id):
            target_region_id = None if region_id == ALL_REGIONS_KEY else region_id
            categories = self.db.get_company_categories(company_id, target_region_id)
            category_f.options = [
                ft.dropdown.Option(
                    key=c.get("id"), text=self._format_category_label(c, company_id)
                )
                for c in categories
                if isinstance(c, dict) and c.get("id")
            ]
            category_f.value = next(
                (
                    c.get("id")
                    for c in categories
                    if isinstance(c, dict) and c.get("id")
                ),
                None,
            )
            has_categories = bool(category_f.options)
            category_f.disabled = not has_categories
            no_categories_hint.visible = not has_categories

        def load_subcategories(category_id):
            subs = self.db.get_subcategories(category_id) if category_id else []
            subcategory_f.options = [
                ft.dropdown.Option(key="", text="Без підкатегорії")
            ]
            for s in subs:
                if isinstance(s, dict) and s.get("id"):
                    subcategory_f.options.append(
                        ft.dropdown.Option(
                            key=s.get("id"),
                            text=self._format_subcategory_label(
                                s, company_f.value, category_id
                            ),
                        )
                    )
            subcategory_f.value = ""

        def load_series(category_id, subcategory_id):
            rows = (
                self.db.get_series(category_id, subcategory_id or None)
                if category_id
                else []
            )
            series_f.options = [ft.dropdown.Option(key="", text="Без серії")]
            for row in rows:
                if isinstance(row, dict) and row.get("id"):
                    series_f.options.append(
                        ft.dropdown.Option(key=row.get("id"), text=row.get("name"))
                    )
            series_f.value = ""

        def on_company_change(_):
            load_regions(company_f.value)
            load_categories(company_f.value, region_f.value)
            load_subcategories(category_f.value)
            load_series(category_f.value, subcategory_f.value)
            if self.page:
                self.page.update()

        def on_region_change(_):
            load_categories(company_f.value, region_f.value)
            load_subcategories(category_f.value)
            load_series(category_f.value, subcategory_f.value)
            if self.page:
                self.page.update()

        def on_category_change(_):
            load_subcategories(category_f.value)
            load_series(category_f.value, subcategory_f.value)
            if self.page:
                self.page.update()

        def on_subcategory_change(_):
            load_series(category_f.value, subcategory_f.value)
            if self.page:
                self.page.update()

        company_f.on_select = on_company_change
        region_f.on_select = on_region_change
        category_f.on_select = on_category_change
        subcategory_f.on_select = on_subcategory_change

        if default_company_id:
            load_regions(default_company_id)
            load_categories(default_company_id, region_f.value)
            load_subcategories(category_f.value)
            load_series(category_f.value, subcategory_f.value)

        def save_click(_):
            self._clear_field_errors(company_f, region_f, category_f, name_f)
            company_id = company_f.value
            category_id = category_f.value
            brand_name = (name_f.value or "").strip()
            tm_type = tm_type_f.value or tm_types[0]
            sub_id = subcategory_f.value or None
            ser_id = series_f.value or None
            if not company_id or not category_id or not brand_name:
                if not company_id:
                    self._mark_required(company_f, "Оберіть компанію")
                if not region_f.value:
                    self._mark_required(region_f, "Оберіть регіон")
                if not category_id:
                    self._mark_required(category_f, "Оберіть категорію")
                if not brand_name:
                    self._mark_required(name_f, "Введіть назву")
                self._show_snack(
                    "Оберіть компанію/регіон/категорію і введіть назву бренду", "orange"
                )
                if self.page:
                    self.page.update()
                return
            if self.db.brand_name_exists(category_id, brand_name, sub_id, ser_id):
                self._mark_required(name_f, "Такий бренд вже існує")
                self._show_snack("Такий бренд вже існує", "orange")
                if self.page:
                    self.page.update()
                return
            if self.db.add_brand(category_id, brand_name, tm_type, sub_id, ser_id):
                self._show_snack("Бренд створено", "green")
                self.page.pop_dialog()
                if company_id == self.selected_company_id:
                    self.show_categories(None)
                else:
                    self.page.update()
            else:
                self._show_snack("Не вдалося створити бренд", "red")

        dlg = ft.AlertDialog(
            title=ft.Text("Створити бренд"),
            content=ft.Column(
                [
                    company_f,
                    region_f,
                    category_f,
                    no_categories_hint,
                    subcategory_f,
                    series_f,
                    name_f,
                    tm_type_f,
                ],
                tight=True,
                spacing=10,
                width=500,
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: self._close_dialog(dlg)),
                ft.FilledButton("Зберегти", on_click=save_click),
            ],
        )
        self.page.show_dialog(dlg)

    def _open_sa_series_dialog(self):
        companies = self.db.get_all_companies()
        company_options = [
            ft.dropdown.Option(key=c.get("id"), text=c.get("name"))
            for c in companies
            if isinstance(c, dict) and c.get("id")
        ]
        default_company_id = self.selected_company_id or next(
            (c.get("id") for c in companies if isinstance(c, dict) and c.get("id")),
            None,
        )

        company_f = ft.Dropdown(
            label="Компанія",
            border_radius=10,
            options=company_options,
            value=default_company_id,
        )
        region_f = ft.Dropdown(label="Регіон", border_radius=10, options=[])
        category_f = ft.Dropdown(label="Категорія", border_radius=10, options=[])
        no_categories_hint = ft.Text(
            "У вибраному регіоні немає категорій",
            size=12,
            color="grey600",
            visible=False,
        )
        subcategory_f = ft.Dropdown(label="Підкатегорія", border_radius=10, options=[])
        name_f = ft.TextField(label="Назва серії", border_radius=10)

        def load_regions(company_id):
            regions = self.db.get_company_regions(company_id)
            region_options = [
                ft.dropdown.Option(key=r.get("id"), text=r.get("name"))
                for r in regions
                if isinstance(r, dict) and r.get("id")
            ]
            if len(region_options) > 1:
                region_f.options = [
                    ft.dropdown.Option(key=ALL_REGIONS_KEY, text="Всі регіони")
                ]
                region_f.options.extend(region_options)
                region_f.value = ALL_REGIONS_KEY
            else:
                region_f.options = region_options
                region_f.value = region_options[0].key if region_options else None
            region_f.visible = len(region_options) > 1

        def load_categories(company_id, region_id):
            target_region_id = None if region_id == ALL_REGIONS_KEY else region_id
            categories = self.db.get_company_categories(company_id, target_region_id)
            category_f.options = [
                ft.dropdown.Option(
                    key=c.get("id"), text=self._format_category_label(c, company_id)
                )
                for c in categories
                if isinstance(c, dict) and c.get("id")
            ]
            category_f.value = next(
                (
                    c.get("id")
                    for c in categories
                    if isinstance(c, dict) and c.get("id")
                ),
                None,
            )
            has_categories = bool(category_f.options)
            category_f.disabled = not has_categories
            no_categories_hint.visible = not has_categories

        def load_subcategories(category_id):
            subs = self.db.get_subcategories(category_id) if category_id else []
            subcategory_f.options = [
                ft.dropdown.Option(key="", text="Без підкатегорії")
            ]
            for s in subs:
                if isinstance(s, dict) and s.get("id"):
                    subcategory_f.options.append(
                        ft.dropdown.Option(
                            key=s.get("id"),
                            text=self._format_subcategory_label(
                                s, company_f.value, category_id
                            ),
                        )
                    )
            subcategory_f.value = ""

        def on_company_change(_):
            load_regions(company_f.value)
            load_categories(company_f.value, region_f.value)
            load_subcategories(category_f.value)
            if self.page:
                self.page.update()

        def on_region_change(_):
            load_categories(company_f.value, region_f.value)
            load_subcategories(category_f.value)
            if self.page:
                self.page.update()

        def on_category_change(_):
            load_subcategories(category_f.value)
            if self.page:
                self.page.update()

        company_f.on_select = on_company_change
        region_f.on_select = on_region_change
        category_f.on_select = on_category_change

        if default_company_id:
            load_regions(default_company_id)
            load_categories(default_company_id, region_f.value)
            load_subcategories(category_f.value)

        def save_click(_):
            self._clear_field_errors(company_f, region_f, category_f, name_f)
            company_id = company_f.value
            category_id = category_f.value
            series_name = (name_f.value or "").strip()
            sub_id = subcategory_f.value or None
            if not company_id or not category_id or not series_name:
                if not company_id:
                    self._mark_required(company_f, "Оберіть компанію")
                if not region_f.value:
                    self._mark_required(region_f, "Оберіть регіон")
                if not category_id:
                    self._mark_required(category_f, "Оберіть категорію")
                if not series_name:
                    self._mark_required(name_f, "Введіть назву")
                self._show_snack(
                    "Оберіть компанію/регіон/категорію і введіть назву серії", "orange"
                )
                if self.page:
                    self.page.update()
                return
            if self.db.series_name_exists(category_id, series_name, sub_id):
                self._mark_required(name_f, "Така серія вже існує")
                self._show_snack("Така серія вже існує", "orange")
                if self.page:
                    self.page.update()
                return
            if self.db.add_series(category_id, series_name, sub_id):
                self._show_snack("Серію створено", "green")
                self.page.pop_dialog()
                if company_id == self.selected_company_id:
                    self.show_categories(None)
                else:
                    self.page.update()
            else:
                self._show_snack("Не вдалося створити серію", "red")

        dlg = ft.AlertDialog(
            title=ft.Text("Створити серію"),
            content=ft.Column(
                [
                    company_f,
                    region_f,
                    category_f,
                    no_categories_hint,
                    subcategory_f,
                    name_f,
                ],
                tight=True,
                spacing=10,
                width=500,
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: self._close_dialog(dlg)),
                ft.FilledButton("Зберегти", on_click=save_click),
            ],
        )
        self.page.show_dialog(dlg)

    def _resolve_region_name_for_company(self, company_id):
        if (
            company_id == ALL_COMPANIES_KEY
            or self.selected_region_name == ALL_REGIONS_KEY
        ):
            return None
        regions = self.db.get_company_regions(company_id)
        names = [
            r.get("name") for r in regions if isinstance(r, dict) and r.get("name")
        ]
        if names and self.selected_region_name not in names:
            self.selected_region_name = names[0]
        return self.selected_region_name

    def show_suppliers(self, e):
        self.current_tab = "suppliers"
        self.content_area.controls.clear()
        current_region = self._resolve_region_name_for_company(self.selected_company_id)
        SuppliersView(
            self.content_area, self._build_superadmin_context(), self.db, current_region
        ).build()
        self.page.update()

    def show_packaging(self, e):
        self.current_tab = "packaging"
        self.content_area.controls.clear()
        current_region = self._resolve_region_name_for_company(self.selected_company_id)
        PackagingView(
            self.content_area, self._build_superadmin_context(), self.db, current_region
        ).build()
        self.page.update()

    def show_infrastructure_settings(self, e):
        self.current_tab = "infrastructure"
        self.content_area.controls.clear()

        # ── credentials section (all roles) ──────────────────────────────────
        uid = str(self.user_data.get("id") or "").strip()
        name_f = ft.TextField(
            label="Ім'я",
            value=str(self.user_data.get("full_name") or ""),
            border_radius=10,
            width=320,
        )
        email_f = ft.TextField(
            label="E-mail",
            value=str(self.user_data.get("email") or ""),
            border_radius=10,
            width=320,
        )
        phone_f = ft.TextField(
            label="Телефон",
            value=str(self.user_data.get("phone") or ""),
            border_radius=10,
            width=320,
        )
        old_pw_f = ft.TextField(
            label="Поточний пароль",
            password=True,
            can_reveal_password=True,
            border_radius=10,
            width=320,
        )
        new_pw_f = ft.TextField(
            label="Новий пароль",
            password=True,
            can_reveal_password=True,
            border_radius=10,
            width=320,
        )

        def _save_profile(_):
            try:
                payload = {
                    "full_name": str(name_f.value or "").strip(),
                    "email": str(email_f.value or "").strip(),
                    "phone": str(phone_f.value or "").strip(),
                }
                ok = self.db.update_manager_data(uid, payload)
                if ok:
                    self.user_data["full_name"] = payload["full_name"]
                    self.user_data["email"] = payload["email"]
                    self.user_data["phone"] = payload["phone"]
                    self._show_snack("Профіль збережено", "green")
                    with contextlib.suppress(Exception):
                        self.page.update()
                else:
                    self._show_snack("Не вдалося зберегти профіль", "red")
            except Exception:
                logger.exception("[admin] _save_profile failed")
                self._show_snack("Не вдалося зберегти профіль", "red")

        def _save_password(_):
            if not old_pw_f.value:
                self._show_snack("Введіть поточний пароль", "orange")
                return
            if str(old_pw_f.value) != str(self.user_data.get("password") or ""):
                self._show_snack("Поточний пароль невірний!", "red")
                return
            if not new_pw_f.value or len(new_pw_f.value) < 4:
                self._show_snack("Новий пароль — мінімум 4 символи!", "orange")
                return
            try:
                ok = self.db.update_manager_data(uid, {"password": new_pw_f.value})
                if ok:
                    self.user_data["password"] = new_pw_f.value
                    old_pw_f.value = ""
                    new_pw_f.value = ""
                    self._show_snack("Пароль змінено", "green")
                    with contextlib.suppress(Exception):
                        self.page.update()
                else:
                    self._show_snack("Не вдалося змінити пароль", "red")
            except Exception:
                logger.exception("[admin] _save_password failed")
                self._show_snack("Не вдалося змінити пароль", "red")

        creds_card = ft.Card(
            ft.Container(
                padding=15,
                content=ft.Column(
                    [
                        ft.Text("Профіль", size=15, weight=ft.FontWeight.W_700),
                        name_f,
                        email_f,
                        phone_f,
                        ft.FilledButton(
                            "Зберегти профіль",
                            icon=ft.Icons.SAVE,
                            on_click=_save_profile,
                        ),
                        ft.Divider(height=8),
                        ft.Text("Зміна пароля", size=13, weight=ft.FontWeight.W_600),
                        old_pw_f,
                        new_pw_f,
                        ft.FilledButton(
                            "Змінити пароль",
                            icon=ft.Icons.LOCK_RESET,
                            on_click=_save_password,
                        ),
                    ],
                    spacing=10,
                ),
            )
        )

        # ── appearance section ────────────────────────────────────────────────
        _admin_mid = str(self.user_data.get("id") or "").strip()
        from views.themes import build_admin_appearance_section as _build_appearance
        appearance_card = ft.Card(
            ft.Container(
                padding=15,
                content=_build_appearance(
                    self.db,
                    _admin_mid,
                    on_theme_changed=self._apply_admin_theme_live,
                    on_nav_changed=self._apply_admin_nav_live,
                ),
            )
        )

        self.content_area.controls.extend(
            [
                ft.Text("Налаштування", size=22, weight="bold"),
                creds_card,
                appearance_card,
            ]
        )
        with contextlib.suppress(Exception):
            self.content_area.update()

        # ── infrastructure section (SuperAdmin only) ─────────────────────────
        if not self._is_company_scoped():
            cfg = self.db.get_infrastructure_settings()

            mode_f = ft.Dropdown(
                label="Режим активної БД",
                value=str(cfg.get("mode") or "local"),
                border_radius=10,
                width=220,
                options=[
                    ft.dropdown.Option("local", "Локальна"),
                    ft.dropdown.Option("vps", "VPS"),
                ],
            )
            local_dsn_f = ft.TextField(
                label="Локальна БД (DSN)",
                value=str(cfg.get("local_db_dsn") or ""),
                border_radius=10,
                multiline=True,
                min_lines=2,
                max_lines=4,
            )
            vps_dsn_f = ft.TextField(
                label="VPS БД (DSN)",
                value=str(cfg.get("vps_db_dsn") or ""),
                border_radius=10,
                multiline=True,
                min_lines=2,
                max_lines=4,
            )
            auto_migrate_cb = ft.Checkbox(
                label="Автоперенесення при зміні активної БД",
                value=bool(cfg.get("auto_migrate_on_switch", True)),
            )

            # ── manual copy section ─────────────────────
            copy_src_f = ft.TextField(
                label="Джерело (DSN) для копіювання",
                hint_text="Залиш пустим — використовується поточна активна БД",
                border_radius=10,
                multiline=True,
                min_lines=2,
                max_lines=3,
            )
            copy_tgt_f = ft.TextField(
                label="Призначення (DSN) — куди копіювати",
                hint_text="postgresql://user:pass@host:5432/dbname",
                border_radius=10,
                multiline=True,
                min_lines=2,
                max_lines=3,
            )

            def _manual_copy_click(_):
                try:
                    src = str(copy_src_f.value or "").strip()
                    tgt = str(copy_tgt_f.value or "").strip()
                    if not tgt:
                        self._show_snack("Вкажіть DSN призначення!", "orange")
                        return
                    if not src:
                        new_mode = str(mode_f.value or "local").strip().lower()
                        src = (
                            str(cfg.get("vps_db_dsn") or "").strip()
                            if new_mode == "vps"
                            else str(cfg.get("local_db_dsn") or "").strip()
                        )
                    if not src:
                        self._show_snack("Не вдалося визначити джерело!", "orange")
                        return
                    self._run_migration_dialog(source_dsn=src, target_dsn=tgt)
                except Exception:
                    logger.exception("[admin] _manual_copy_click failed")
                    self._show_snack("Не вдалося виконати копіювання", "red")

            def save_click(_):
                try:
                    old_mode = str(cfg.get("mode") or "local").strip().lower()
                    old_active_dsn = (
                        str(cfg.get("vps_db_dsn") or "").strip()
                        if old_mode == "vps"
                        else str(cfg.get("local_db_dsn") or "").strip()
                    )
                    new_mode = str(mode_f.value or "local").strip().lower()
                    payload = {
                        "mode": new_mode,
                        "local_db_dsn": str(local_dsn_f.value or "").strip(),
                        "vps_db_dsn": str(vps_dsn_f.value or "").strip(),
                        "auto_migrate_on_switch": bool(auto_migrate_cb.value),
                    }
                    new_active_dsn = (
                        str(payload.get("vps_db_dsn") or "").strip()
                        if new_mode == "vps"
                        else str(payload.get("local_db_dsn") or "").strip()
                    )
                    ok = self.db.set_infrastructure_settings(payload)
                    if not ok:
                        self._show_snack(
                            "Не вдалося зберегти інфраструктурні налаштування", "red"
                        )
                        return
                    want_migrate = (
                        bool(auto_migrate_cb.value)
                        and bool(old_active_dsn)
                        and bool(new_active_dsn)
                        and old_active_dsn != new_active_dsn
                    )
                    if want_migrate:
                        src = str(old_active_dsn or self.db._pg_dsn or "").strip()
                        tgt = str(new_active_dsn or "").strip()

                        def _after_migrate():
                            try:
                                sw_ok, sw_msg = self.db.apply_infrastructure_switch()
                                status = (
                                    "Підключення оновлено."
                                    if sw_ok
                                    else f"Не вдалося перемкнути: {sw_msg}"
                                )
                                self._show_snack(status, "green" if sw_ok else "red")
                            except Exception:
                                logger.exception("[admin] _after_migrate failed")
                                self._show_snack("Не вдалося перемкнути підключення", "red")

                        self._run_migration_dialog(
                            source_dsn=src, target_dsn=tgt, on_success=_after_migrate
                        )
                    else:
                        switch_ok, switch_msg = self.db.apply_infrastructure_switch()
                        if switch_ok:
                            self._show_saved_dialog(
                                f"Налаштування збережено.\n\n{switch_msg}"
                            )
                        else:
                            self._show_saved_dialog(
                                f"Налаштування збережено, але підключення не змінено:\n{switch_msg}"
                            )
                except Exception:
                    logger.exception("[admin] save_click (infrastructure) failed")
                    self._show_snack("Не вдалося зберегти інфраструктурні налаштування", "red")

            def _show_infra_help():
                from views.help_utils import show_modal_help, vps_migration_help_items

                show_modal_help(
                    self.page,
                    "Перенесення бази даних на VPS / власний сервер",
                    vps_migration_help_items(),
                )

            dsn_help = ft.Container(
                padding=10,
                bgcolor="#F8FBFA",
                border=ft.border.all(1, "#D7E5EA"),
                border_radius=10,
                content=ft.Column(
                    [
                        ft.Text("Що вказувати для БД", weight="bold"),
                        ft.Text(
                            "Формат DSN: postgresql://USER:PASSWORD@HOST:5432/DBNAME",
                            size=12,
                            color="grey700",
                        ),
                        ft.Text(
                            "Локальна: ваш локальний PostgreSQL. VPS: база на сервері. "
                            "Копіювання — тільки копіює дані, нічого не видаляє з джерела.",
                            size=12,
                            color="grey700",
                        ),
                    ],
                    spacing=4,
                ),
            )

            self.content_area.controls.extend(
                [
                    ft.Card(
                        ft.Container(
                            padding=15,
                            content=ft.Column(
                                [
                                    ft.Row(
                                        [
                                            ft.Text(
                                                "Налаштування інфраструктури",
                                                size=15,
                                                weight=ft.FontWeight.W_700,
                                            ),
                                            ft.IconButton(
                                                icon=ft.Icons.HELP_OUTLINE,
                                                icon_size=16,
                                                tooltip="Як перенести базу даних на VPS або власний сервер",
                                                on_click=lambda _: _show_infra_help(),
                                            ),
                                        ],
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        tight=True,
                                    ),
                                    ft.Text(
                                        "Перемикання Local/VPS без зміни коду. Доступи по компаніях — у вкладці Розширення.",
                                        size=12,
                                        color="grey700",
                                    ),
                                    mode_f,
                                    local_dsn_f,
                                    vps_dsn_f,
                                    auto_migrate_cb,
                                    dsn_help,
                                    ft.Text(
                                        "Автоперенесення виконується окремим кроком міграції (без ризику втрати даних).",
                                        size=12,
                                        color="grey700",
                                    ),
                                    ft.FilledButton(
                                        "Зберегти налаштування",
                                        icon=ft.Icons.SAVE,
                                        on_click=save_click,
                                    ),
                                ],
                                spacing=10,
                            ),
                        )
                    ),
                    ft.Card(
                        ft.Container(
                            padding=15,
                            content=ft.Column(
                                [
                                    ft.Text(
                                        "Копіювання БД (ручне)",
                                        size=15,
                                        weight=ft.FontWeight.W_700,
                                    ),
                                    ft.Text(
                                        "Скопіює всі дані з джерела в призначення. Дані у джерелі НЕ видаляються. "
                                        "Для VPS→VPS вкажіть обидва DSN вручну.",
                                        size=12,
                                        color="grey700",
                                    ),
                                    copy_src_f,
                                    copy_tgt_f,
                                    ft.FilledButton(
                                        "Копіювати",
                                        icon=ft.Icons.COPY_ALL,
                                        on_click=_manual_copy_click,
                                    ),
                                ],
                                spacing=10,
                            ),
                        )
                    ),
                ]
            )

        # ── company DB section (Admin with locked company) ───────────────────
        if self._is_company_scoped() and self.locked_company_id:
            _company_id = str(self.locked_company_id)
            _company_name = str(self.user_data.get("company_name") or _company_id)

            _global_cfg = self.db.get_infrastructure_settings()
            _global_mode = str(_global_cfg.get("mode") or "local")
            _global_auth_dsn = (
                str(_global_cfg.get("vps_db_dsn") or "").strip()
                if _global_mode == "vps"
                else str(_global_cfg.get("local_db_dsn") or "").strip()
            ) or str(self.db._pg_dsn or "").strip()

            _companies_db = _global_cfg.get("companies_db") or {}
            _co_cfg = _companies_db.get(_company_id) or {}
            _co_init_mode = str(_co_cfg.get("mode") or "local")
            _co_init_local = str(_co_cfg.get("local_dsn") or _global_auth_dsn)
            _co_init_vps = str(_co_cfg.get("vps_dsn") or "")

            if _co_init_mode == "vps":
                _co_cur_dsn = _co_init_vps or _global_auth_dsn
            else:
                _co_cur_dsn = _co_init_local or _global_auth_dsn

            _co_state = {"old_dsn": ""}

            co_cur_label = ft.Text(
                f"Поточна БД: {_co_cur_dsn or '—'}",
                size=11,
                color="grey600",
                selectable=True,
            )
            co_mode_f = ft.Dropdown(
                label="Активна БД",
                value=_co_init_mode,
                border_radius=10,
                width=200,
                options=[
                    ft.dropdown.Option("local", "Локальна"),
                    ft.dropdown.Option("vps", "VPS"),
                ],
            )
            co_local_f = ft.TextField(
                label="Локальна БД (DSN)",
                value=_co_init_local,
                border_radius=10,
                multiline=True,
                min_lines=2,
                max_lines=4,
            )
            co_vps_f = ft.TextField(
                label="VPS БД (DSN)",
                value=_co_init_vps,
                border_radius=10,
                multiline=True,
                min_lines=2,
                max_lines=4,
            )
            co_delete_old_btn = ft.OutlinedButton(
                "Видалити дані зі старої БД",
                icon=ft.Icons.DELETE_FOREVER,
                style=ft.ButtonStyle(color="red"),
                visible=False,
            )

            def _co_save(_):
                try:
                    new_mode = str(co_mode_f.value or "local").lower()
                    new_local = str(co_local_f.value or "").strip()
                    new_vps = str(co_vps_f.value or "").strip()
                    full2 = self.db.get_infrastructure_settings()
                    full2.setdefault("companies_db", {})
                    full2["companies_db"][_company_id] = {
                        "mode": new_mode,
                        "local_dsn": new_local,
                        "vps_dsn": new_vps,
                    }
                    ok = self.db.set_infrastructure_settings(full2)
                    if ok:
                        new_active = new_vps if new_mode == "vps" else new_local
                        co_cur_label.value = f"Поточна БД: {new_active or '—'}"
                        with contextlib.suppress(Exception):
                            co_cur_label.update()
                    self._show_snack(
                        "Збережено" if ok else "Не вдалося зберегти",
                        "green" if ok else "red",
                    )
                except Exception:
                    logger.exception("[admin] _co_save failed")
                    self._show_snack("Не вдалося зберегти", "red")

            def _co_migrate_and_switch(_):
                try:
                    full2 = self.db.get_infrastructure_settings()
                    c_cfg2 = (full2.get("companies_db") or {}).get(_company_id) or {}
                    c_mode2 = str(c_cfg2.get("mode") or "local")
                    src = (
                        str(c_cfg2.get("vps_dsn") or "").strip()
                        if c_mode2 == "vps"
                        else str(c_cfg2.get("local_dsn") or "").strip()
                    ) or _global_auth_dsn
                    tgt_mode = str(co_mode_f.value or "local").lower()
                    tgt = (
                        str(co_vps_f.value or "").strip()
                        if tgt_mode == "vps"
                        else str(co_local_f.value or "").strip()
                    )
                    if not src:
                        self._show_snack("Поточна активна БД невідома!", "orange")
                        return
                    if not tgt:
                        self._show_snack("Вкажіть DSN нової (цільової) БД!", "orange")
                        return
                    if src == tgt:
                        self._show_snack("Джерело і призначення однакові", "orange")
                        return
                    _co_state["old_dsn"] = src

                    def _on_co_success():
                        try:
                            full3 = self.db.get_infrastructure_settings()
                            full3.setdefault("companies_db", {})
                            full3["companies_db"][_company_id] = {
                                "mode": tgt_mode,
                                "local_dsn": str(co_local_f.value or "").strip(),
                                "vps_dsn": str(co_vps_f.value or "").strip(),
                            }
                            self.db.set_infrastructure_settings(full3)
                            co_cur_label.value = f"Поточна БД: {tgt}"
                            co_delete_old_btn.visible = True
                            with contextlib.suppress(Exception):
                                self.page.update()
                        except Exception:
                            logger.exception("[admin] _on_co_success failed")
                            self._show_snack("Перенесення виконано, але не вдалося оновити налаштування", "red")

                    self._run_migration_dialog(
                        source_dsn=src, target_dsn=tgt, on_success=_on_co_success
                    )
                except Exception:
                    logger.exception("[admin] _co_migrate_and_switch failed")
                    self._show_snack("Не вдалося виконати перенесення", "red")

            def _co_delete_old(_):
                old = str(_co_state.get("old_dsn") or "").strip()
                if not old:
                    self._show_snack(
                        "Адреса старої БД невідома. Перенесіть дані спочатку.", "orange"
                    )
                    return

                def _do_co_delete(__):
                    self.page.pop_dialog()
                    try:
                        ok2, msg2 = self.db.drop_database_contents(old)
                        self._show_snack(msg2, "green" if ok2 else "red")
                        if ok2:
                            co_delete_old_btn.visible = False
                            _co_state["old_dsn"] = ""
                            with contextlib.suppress(Exception):
                                self.page.update()
                    except Exception:
                        logger.exception("[admin] _do_co_delete failed")
                        self._show_snack("Не вдалося видалити дані зі старої БД", "red")

                co_confirm_dlg = ft.AlertDialog(
                    modal=True,
                    title=ft.Text("Видалити дані зі старої БД?"),
                    content=ft.Container(
                        width=420,
                        content=ft.Column(
                            [
                                ft.Text("Буде видалено всі дані з:", size=13),
                                ft.Text(old, size=11, color="grey700", selectable=True),
                                ft.Text(
                                    "Цю дію неможливо відмінити!",
                                    size=12,
                                    color="red",
                                    weight=ft.FontWeight.W_700,
                                ),
                            ],
                            spacing=8,
                            tight=True,
                        ),
                    ),
                    actions=[
                        ft.TextButton(
                            "Скасувати",
                            on_click=lambda _: self.page.pop_dialog(),
                        ),
                        ft.FilledButton(
                            "Видалити",
                            icon=ft.Icons.DELETE_FOREVER,
                            style=ft.ButtonStyle(bgcolor="red"),
                            on_click=_do_co_delete,
                        ),
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
                self.page.show_dialog(co_confirm_dlg)

            co_delete_old_btn.on_click = _co_delete_old

            self.content_area.controls.append(
                ft.Card(
                    ft.Container(
                        padding=15,
                        content=ft.Column(
                            [
                                ft.Text(
                                    f"База даних компанії «{_company_name}»",
                                    size=15,
                                    weight=ft.FontWeight.W_700,
                                ),
                                ft.Text(
                                    "Дані компанії можна зберігати на окремій локальній або VPS базі. "
                                    "За замовчуванням використовується загальна БД авторизації.",
                                    size=12,
                                    color="grey700",
                                ),
                                co_cur_label,
                                ft.Divider(height=4, color="transparent"),
                                co_mode_f,
                                co_local_f,
                                co_vps_f,
                                ft.Row(
                                    [
                                        ft.FilledButton(
                                            "Зберегти",
                                            icon=ft.Icons.SAVE,
                                            on_click=_co_save,
                                        ),
                                        ft.FilledButton(
                                            "Копіювати і переключити",
                                            icon=ft.Icons.SWAP_HORIZ,
                                            on_click=_co_migrate_and_switch,
                                        ),
                                    ],
                                    spacing=8,
                                    wrap=True,
                                ),
                                co_delete_old_btn,
                            ],
                            spacing=10,
                        ),
                    )
                )
            )

        self.page.update()

    def _run_migration_dialog(self, source_dsn: str, target_dsn: str, on_success=None):
        """Show a progress dialog and copy data source→target in a background thread.
        Nothing is deleted from source automatically — user may delete manually after success.
        """
        import threading

        log_col = ft.Column([], spacing=2, scroll=ft.ScrollMode.AUTO)
        status_text = ft.Text("Виконується копіювання даних...", size=13, weight="bold")
        close_btn = ft.TextButton(
            "Закрити",
            disabled=True,
            on_click=lambda _: self.page.pop_dialog(),
        )
        delete_src_btn = ft.OutlinedButton(
            "Видалити дані з джерела",
            icon=ft.Icons.DELETE_FOREVER,
            style=ft.ButtonStyle(color="red"),
            visible=False,
        )

        def _delete_source(_):
            delete_src_btn.visible = False
            with contextlib.suppress(Exception):
                self.page.update()
            try:
                ok2, msg2 = self.db.drop_database_contents(source_dsn)
                color2 = "green" if ok2 else "red"
                log_col.controls.append(
                    ft.Text(f"Видалення джерела: {msg2}", size=11, color=color2)
                )
            except Exception as _ex:
                log_col.controls.append(
                    ft.Text(f"Видалення: помилка — {_ex}", size=11, color="red")
                )
            with contextlib.suppress(Exception):
                self.page.update()

        delete_src_btn.on_click = _delete_source

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Копіювання даних між БД"),
            content=ft.Container(
                width=520,
                height=320,
                content=ft.Column(
                    [
                        status_text,
                        ft.Divider(height=8),
                        ft.Container(height=260, content=log_col),
                    ],
                    spacing=4,
                ),
            ),
            actions=[delete_src_btn, close_btn],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

        def _progress(msg: str):
            color = (
                "red"
                if "ПОМИЛКА" in msg
                else ("grey700" if msg.startswith("  ") else None)
            )
            log_col.controls.append(ft.Text(msg, size=11, color=color))
            with contextlib.suppress(Exception):
                self.page.update()

        def _run():
            try:
                ok, summary = self.db.migrate_local_to_vps(
                    target_dsn=target_dsn,
                    source_dsn=source_dsn,
                    progress_callback=_progress,
                )
            except Exception as _ex:
                logger.exception("[admin] migrate_local_to_vps failed")
                ok, summary = False, f"Помилка перенесення: {_ex}"
            status_text.value = summary
            status_text.color = "green" if ok else "red"
            close_btn.disabled = False
            if ok:
                delete_src_btn.visible = True
            with contextlib.suppress(Exception):
                self.page.update()
            if ok and on_success:
                with contextlib.suppress(Exception):
                    on_success()

        threading.Thread(target=_run, daemon=True).start()

    def open_bulk_product_template(self, _=None):
        try:
            from views.product_bulk_template_ui import show_bulk_product_template_dialog

            def _after_import():
                cid = str(self.locked_company_id or self.user_data.get("company_id") or "").strip()
                if cid:
                    with contextlib.suppress(Exception):
                        self.db.invalidate_products_cache(cid)

            show_bulk_product_template_dialog(self, on_after_import=_after_import)
        except Exception:
            logger.exception("open_bulk_product_template failed")
            self._show_snack("Не вдалося відкрити майстер шаблону", "red")

    def _close_dialog(self, dlg):
        if not self.page:
            return
        self.page.pop_dialog()

    def _build_superadmin_context(self):
        # Admin context (single-company scope), but keep the same view wiring.
        return {
            "id": self.user_data.get("id"),
            "full_name": self.user_data.get("full_name"),
            "role": "Адмін",
            "company_id": self.locked_company_id or self.user_data.get("company_id"),
            "active_manager_id": self.user_data.get("id"),
            "bypass_category_access": True,
            "superadmin_explicit_mode": False,
        }

    def show_extensions(self, e):
        """
        Admin must have the same Extensions UI, but limited to one company and
        WITHOUT SuperAdmin toggles.
        """
        self.current_tab = "extensions"
        existing = getattr(self, "_ext_view_inst", None)
        # Sync-poll path (e is None): a background sync pulse re-renders
        # whatever tab is current (see _show_current_tab/from_sync), but the
        # extensions view has deep navigation state — which extension/tab is
        # open, open forms, GSM Parser's brand-table expand state. Rebuilding
        # it from scratch on every pulse reset all of that every few seconds.
        # Only an explicit click (e is not None) should ever rebuild it.
        if e is None and existing is not None:
            return
        self.content_area.controls.clear()
        active_company_id = self.locked_company_id or self.user_data.get("company_id")
        if existing is None:
            self._ext_view_inst = ExtensionsView(
                self.content_area,
                self.user_data,
                self.db,
                active_company_id=active_company_id,
                is_superadmin=False,
            )
        else:
            # User explicitly clicked the tab — navigate back to the extensions list.
            existing.current_extension_key = None
        self._ext_view_inst.build()
        self.page.update()

    def show_copy_setup(self, e):
        # Same Copy Setup UX as SuperAdmin, but scoped to locked company.
        self.current_tab = "copy_setup"
        self.content_area.controls.clear()

        company_id = self.locked_company_id or self.user_data.get("company_id")
        if not company_id:
            self.content_area.controls.extend(
                [
                    ft.Text("📋 Копіювання", size=24, weight="bold"),
                    ft.Text("Не визначено компанію для адміна.", color="red"),
                ]
            )
            self.page.update()
            return

        managers = [
            m
            for m in self.db.get_company_managers(company_id)
            if isinstance(m, dict) and m.get("id")
        ]
        if not managers:
            self.content_area.controls.extend(
                [
                    ft.Text("📋 Копіювання", size=24, weight="bold"),
                    ft.Text(
                        "У вибраній компанії немає користувачів для налаштування.",
                        color="grey700",
                    ),
                ]
            )
            self.page.update()
            return

        def _open_module(module_key):
            if module_key == "categories":
                self.show_categories(None)
                return
            if module_key == "suppliers":
                self.show_suppliers(None)
                return
            if module_key == "packaging":
                self.show_packaging(None)
                return
            if module_key == "users":
                self.show_users(None)
                return

        ctx = self._build_superadmin_context()
        ctx["company_id"] = company_id
        CopySetupView(
            self.content_area, ctx, self.db, on_open_module=_open_module
        ).build()
        self.page.update()

    def show_company_settings(self, e):
        """
        Company-wide setup (regions, delivery methods, supplier payment field
        templates) — reuses the same SettingsView regular managers see under
        "Налаштування"; can_manage_company_settings already covers the admin
        role, so the company-editing controls render live with no extra gating.
        """
        self.current_tab = "company"
        self.content_area.controls.clear()
        ctx = self._build_superadmin_context()
        ctx["active_region_id"] = None
        ctx["subordinate_view_readonly"] = False
        ctx["email"] = self.user_data.get("email")
        ctx["phone"] = self.user_data.get("phone")
        ctx["password"] = self.user_data.get("password")
        ctx["ui_theme"] = _themes.get_active_theme_key(
            (self.user_data.get("id") or "").strip()
        )
        sv = SettingsView(self.content_area, ctx, self.db)
        sv._on_theme_changed = self._apply_admin_theme_live
        sv._on_nav_pack_changed = self._apply_admin_nav_live
        sv.build()
        self.page.update()

    def show_categories(self, e):
        """
        Same Categories UX as SuperAdmin, but scoped to locked company and without
        any "Всі компанії" behavior.
        """
        self.current_tab = "categories"
        self.content_area.controls.clear()

        # Force single company scope
        company_id = self.locked_company_id or self.user_data.get("company_id")
        if not company_id:
            self.content_area.controls.append(
                ft.Text("Не визначено компанію", color="red")
            )
            self.page.update()
            return
        self.selected_company_id = company_id

        companies = self.db.get_all_companies()
        company_name = next(
            (
                c.get("name")
                for c in companies
                if isinstance(c, dict) and c.get("id") == company_id
            ),
            "-",
        )

        regions = self.db.get_company_regions(company_id)
        region_names = [
            r.get("name") for r in regions if isinstance(r, dict) and r.get("name")
        ]
        allowed_region_names = (
            ([ALL_REGIONS_KEY] + region_names)
            if len(region_names) > 1
            else region_names
        )
        if allowed_region_names and (
            self.selected_region_name not in allowed_region_names
        ):
            self.selected_region_name = allowed_region_names[0]

        if self.selected_region_name == ALL_REGIONS_KEY:
            combined_regions = (
                " + ".join(region_names) if region_names else "Всі регіони"
            )
            region_label = f"Всі регіони ({combined_regions})"
        else:
            region_label = self.selected_region_name or "не обрано"

        view_host = ft.Column(expand=True, scroll=ft.ScrollMode.ADAPTIVE)
        self.content_area.controls.extend(
            [
                ft.Text("Категорії та доступи", size=24, weight="bold"),
                ft.Row(
                    [
                        ft.Text(
                            f"Поточна компанія: {company_name}",
                            color="grey700",
                            expand=True,
                        ),
                        ft.Text(f"Регіон: {region_label}", color="grey700"),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                ft.Row(
                    [
                        ft.FilledButton(
                            "Створити категорію",
                            icon=ft.Icons.CATEGORY,
                            on_click=lambda _: self._open_sa_category_dialog(),
                        ),
                    ],
                    spacing=8,
                    wrap=True,
                ),
                ft.Divider(height=10, color="transparent"),
                view_host,
            ]
        )

        # host must be attached before nested view build
        self.page.update()

        ctx = self._build_superadmin_context()
        ctx["company_id"] = company_id
        CategoriesBrandsView(
            view_host, ctx, self.db, self.selected_region_name, catalog_config={}
        ).build()
        self.page.update()

    def _open_sa_category_dialog(self):
        """
        Admin version of "create category" dialog.
        Same UX as SuperAdmin's stable dialog, but WITHOUT company selector.
        Company is always locked_company_id.
        """
        company_id = self.locked_company_id or self.user_data.get("company_id")
        if not company_id:
            self._show_snack("Не визначено компанію для адміна", "red")
            return

        region_f = ft.Dropdown(label="Регіон", border_radius=10, options=[])
        name_f = ft.TextField(label="Назва категорії", border_radius=10)
        use_prod_cb = ft.Checkbox(label="Товари (окремий рівень)", value=False)
        use_prod_row = ft.Container(
            content=use_prod_cb,
            padding=ft.padding.symmetric(horizontal=8, vertical=6),
            border_radius=8,
        )
        levels_rows = []
        levels_rows_container = ft.Column(spacing=10)

        def load_default_levels(region_id=None):
            cfg = (
                self.db.get_company_catalog_config(company_id, region_id)
                if company_id
                else {}
            )
            levels_rows.clear()
            levels_rows_container.controls.clear()
            for row in cfg.get("levels") or []:
                pos = int(row.get("position", 0))
                if pos < 1 or pos > 6:
                    continue
                title_f = ft.TextField(
                    label=f"Назва рівня {pos}",
                    value=(row.get("title") or f"Рівень {pos}"),
                    border_radius=10,
                    width=320,
                )
                enabled_cb = ft.Checkbox(
                    label="Увімкнено", value=bool(row.get("enabled"))
                )
                level_row = {
                    "position": pos,
                    "title": title_f,
                    "enabled": enabled_cb,
                    "row": ft.Row([title_f, enabled_cb], spacing=10, wrap=True),
                }
                levels_rows.append(level_row)
                levels_rows_container.controls.append(level_row.get("row"))

        managers_block = ft.Column(spacing=4)
        manager_radio_group = ft.RadioGroup(content=ft.Column(spacing=4))

        def load_regions():
            regions = self.db.get_company_regions(company_id)
            region_options = [
                ft.dropdown.Option(key=r.get("id"), text=r.get("name"))
                for r in regions
                if isinstance(r, dict) and r.get("id")
            ]
            # Admin: if there are multiple regions, force choosing a concrete region.
            # Do NOT allow "Всі регіони" selection here.
            region_f.options = region_options
            region_f.value = region_options[0].key if region_options else None
            region_f.visible = len(region_options) >= 1

        def load_managers():
            manager_radio_group.content.controls.clear()
            manager_radio_group.value = None
            managers = self.db.get_company_managers(company_id)
            manager_radio_group.content.controls.append(
                ft.Radio(value="", label="Без власника (призначити пізніше)")
            )
            for m in managers:
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                manager_radio_group.content.controls.append(
                    ft.Radio(
                        value=m.get("id"),
                        label=f"{(m.get('full_name') or '').strip()} ({(m.get('role') or '').strip()})".strip(),
                    )
                )
            managers_block.controls = [manager_radio_group]

        def on_region_change(_):
            load_default_levels(
                None if region_f.value == ALL_REGIONS_KEY else region_f.value
            )
            if self.page:
                self.page.update()

        region_f.on_select = on_region_change
        load_regions()
        load_managers()
        load_default_levels(
            None if region_f.value == ALL_REGIONS_KEY else region_f.value
        )

        def save_click(_):
            self._clear_field_errors(region_f, name_f)
            region_id = region_f.value
            category_name = (name_f.value or "").strip()
            if not region_id or not category_name:
                if not region_id:
                    self._mark_required(region_f, "Оберіть регіон")
                if not category_name:
                    self._mark_required(name_f, "Введіть назву")
                self._show_snack("Оберіть регіон та введіть назву", "orange")
                if self.page:
                    self.page.update()
                return
            if region_id == ALL_REGIONS_KEY:
                self._show_snack(
                    "Для створення категорії оберіть конкретний регіон", "orange"
                )
                return

            hierarchy_config = {
                "use_products": bool(use_prod_cb.value),
                "levels": [
                    {
                        "position": row.get("position"),
                        "title": (
                            row.get("title").value or f"Рівень {row.get('position')}"
                        ).strip(),
                        "enabled": bool(row.get("enabled").value),
                    }
                    for row in levels_rows
                ],
            }

            category_id = self.db.create_category_with_access(
                company_id,
                region_id,
                category_name,
                manager_id=None,
                hierarchy_config=hierarchy_config,
            )
            if not category_id:
                self._show_snack("Не вдалося створити категорію", "red")
                return

            selected_manager_ids = (
                [manager_radio_group.value] if manager_radio_group.value else []
            )
            if not self.db.replace_category_access(category_id, selected_manager_ids):
                self._show_snack(
                    "Категорія створена, але доступи не збережено", "orange"
                )
            else:
                self._show_snack("Категорію створено та доступи збережено", "green")

            self.page.pop_dialog()
            self.show_categories(None)

        dlg = ft.AlertDialog(
            title=ft.Text("Створити категорію"),
            content=ft.Container(
                width=self._dialog_width(560),
                height=self._dialog_content_height(640),
                content=ft.Column(
                    [
                        region_f,
                        name_f,
                        ft.Text("Конфігурація рівнів", weight="bold", size=12),
                        levels_rows_container,
                        use_prod_row,
                        ft.Text("Власник категорії (одразу):", weight="bold", size=12),
                        managers_block,
                    ],
                    spacing=10,
                    scroll=ft.ScrollMode.ADAPTIVE,
                ),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: self._close_dialog(dlg)),
                ft.FilledButton("Зберегти", on_click=save_click),
            ],
        )
        self.page.show_dialog(dlg)

    def _open_sa_access_dialog(self):
        """
        Admin version of access dialog (assign category access to a manager),
        without any company selector. Company is always locked_company_id.
        """
        company_id = self.locked_company_id or self.user_data.get("company_id")
        if not company_id:
            self._show_snack("Не визначено компанію для адміна", "red")
            return

        region_f = ft.Dropdown(label="Регіон", border_radius=10, options=[])
        manager_f = ft.Dropdown(label="Користувач", border_radius=10, options=[])
        no_categories_hint = ft.Text(
            "У вибраному регіоні немає категорій",
            size=12,
            color="grey600",
            visible=False,
        )
        categories_block = ft.Column(spacing=4)
        category_checks = {}

        def load_regions(manager_id=None):
            regions = self.db.get_company_regions(company_id)
            # If the selected manager is restricted to specific region(s),
            # only offer those — never let the admin browse/assign categories
            # from a region the manager doesn't have access to.
            manager_allowed_region_ids = (
                set(self.db.get_manager_allowed_region_ids(manager_id))
                if manager_id
                else set()
            )
            selectable_regions = (
                [
                    r
                    for r in regions
                    if isinstance(r, dict) and r.get("id") in manager_allowed_region_ids
                ]
                if manager_allowed_region_ids
                else regions
            )
            region_options = [
                ft.dropdown.Option(key=r.get("id"), text=r.get("name"))
                for r in selectable_regions
                if isinstance(r, dict) and r.get("id")
            ]
            if len(region_options) > 1:
                region_f.options = [
                    ft.dropdown.Option(key=ALL_REGIONS_KEY, text="Всі регіони")
                ]
                region_f.options.extend(region_options)
                region_f.value = ALL_REGIONS_KEY
            else:
                region_f.options = region_options
                region_f.value = region_options[0].key if region_options else None
            region_f.visible = len(region_options) > 1

        def load_managers():
            managers = self.db.get_company_managers(company_id)
            manager_f.options = [
                ft.dropdown.Option(
                    key=m.get("id"),
                    text=f"{m.get('full_name', '')} ({m.get('role', '')})",
                )
                for m in managers
                if isinstance(m, dict) and m.get("id")
            ]
            manager_f.value = manager_f.options[0].key if manager_f.options else None

        def load_categories_for_manager(region_id, manager_id):
            categories_block.controls.clear()
            category_checks.clear()
            if not manager_id:
                categories_block.controls.append(
                    ft.Text("Оберіть користувача", size=12, color="grey600")
                )
                no_categories_hint.visible = False
                return

            target_region_id = None if region_id == ALL_REGIONS_KEY else region_id
            manager_allowed_region_ids = set(
                self.db.get_manager_allowed_region_ids(manager_id)
            )
            categories = [
                c
                for c in self.db.get_company_categories(company_id, target_region_id)
                if isinstance(c, dict)
                and c.get("id")
                and (
                    not manager_allowed_region_ids
                    or c.get("region_id") in manager_allowed_region_ids
                )
            ]
            categories.sort(key=lambda c: (c.get("name") or "").lower())

            nodes = self.db.get_company_catalog_nodes(company_id)
            cat_node_id_by_cat_id = {}
            for n in nodes:
                if not isinstance(n, dict):
                    continue
                if (n.get("node_type") or "") != "category":
                    continue
                legacy = (n.get("legacy_id") or "").strip()
                if legacy.startswith("cat:") and n.get("id"):
                    cat_node_id_by_cat_id[legacy.split(":", 1)[1]] = n.get("id")

            selected_node_ids = set(
                self.db.get_manager_scope_node_ids(
                    manager_id, company_id=company_id, permission="view"
                )
            )
            selected_legacy_cat_ids = set()
            if not selected_node_ids:
                selected_legacy_cat_ids = set(
                    self.db.get_manager_category_ids(manager_id)
                )

            if not categories:
                no_categories_hint.visible = True
                categories_block.controls.append(
                    ft.Text(
                        "У вибраному контексті немає категорій",
                        size=12,
                        color="grey600",
                    )
                )
                return

            no_categories_hint.visible = False
            for cat in categories:
                cat_id = cat.get("id")
                node_id = cat_node_id_by_cat_id.get(cat_id)
                checked = (node_id in selected_node_ids) or (
                    cat_id in selected_legacy_cat_ids
                )
                cb = ft.Checkbox(
                    label=self._format_category_label(cat, company_id),
                    value=bool(checked),
                )
                category_checks[cat_id] = {"checkbox": cb, "node_id": node_id}
                categories_block.controls.append(cb)

        def _set_all_categories(value: bool):
            for info in category_checks.values():
                cb = (info or {}).get("checkbox")
                if cb is not None:
                    cb.value = bool(value)

        def on_region_change(_):
            load_categories_for_manager(region_f.value, manager_f.value)
            if self.page:
                self.page.update()

        def on_manager_change(_):
            load_regions(manager_f.value)
            load_categories_for_manager(region_f.value, manager_f.value)
            if self.page:
                self.page.update()

        region_f.on_select = on_region_change
        manager_f.on_select = on_manager_change

        load_managers()
        load_regions(manager_f.value)
        load_categories_for_manager(region_f.value, manager_f.value)

        def save_click(_):
            self._clear_field_errors(region_f, manager_f)
            region_id = region_f.value
            manager_id = manager_f.value
            if not region_id or not manager_id:
                if not region_id:
                    self._mark_required(region_f, "Оберіть регіон")
                if not manager_id:
                    self._mark_required(manager_f, "Оберіть користувача")
                self._show_snack("Оберіть регіон та користувача", "orange")
                if self.page:
                    self.page.update()
                return

            # Prefer scopes if we have node ids; otherwise fallback to legacy category access.
            selected_node_ids = [
                info.get("node_id")
                for info in category_checks.values()
                if info.get("checkbox")
                and info.get("checkbox").value
                and info.get("node_id")
            ]
            if selected_node_ids:
                ok = self.db.replace_manager_scope_nodes(
                    manager_id, company_id, "view", selected_node_ids
                )
            else:
                selected_cat_ids = [
                    cid
                    for cid, info in category_checks.items()
                    if info.get("checkbox") and info.get("checkbox").value
                ]
                ok = self.db.replace_manager_categories(manager_id, selected_cat_ids)

            if ok:
                self._show_snack("Доступи збережено", "green")
                self.page.pop_dialog()
                self.show_users(None)
            else:
                self._show_snack("Не вдалося зберегти доступи", "red")

        dlg = ft.AlertDialog(
            title=ft.Text("Доступи (категорії)"),
            content=ft.Container(
                width=self._dialog_width(560),
                height=self._dialog_content_height(660),
                content=ft.Column(
                    [
                        region_f,
                        manager_f,
                        no_categories_hint,
                        ft.Row(
                            [
                                ft.Text(
                                    "Категорії з доступом:",
                                    weight="bold",
                                    size=12,
                                    expand=True,
                                ),
                                ft.TextButton(
                                    "Вибрати всі",
                                    on_click=lambda _: (
                                        _set_all_categories(True),
                                        self.page.update(),
                                    ),
                                ),
                                ft.TextButton(
                                    "Очистити",
                                    on_click=lambda _: (
                                        _set_all_categories(False),
                                        self.page.update(),
                                    ),
                                ),
                            ],
                            spacing=6,
                            wrap=True,
                        ),
                        categories_block,
                    ],
                    tight=True,
                    spacing=10,
                    scroll=ft.ScrollMode.ADAPTIVE,
                ),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: self._close_dialog(dlg)),
                ft.FilledButton("Зберегти", on_click=save_click),
            ],
        )
        self.page.show_dialog(dlg)
