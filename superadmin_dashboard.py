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
from feature_flags import FEATURE_ORDER, TOGGLEABLE_FEATURES, is_role_admin_like
from logger import logger
from views import themes as _themes
from views.copy_setup_view import CopySetupView
from views.extensions_view import ExtensionsView
from views.languages_view import LanguagesView
from views.web_site_view import WebSiteView

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


class SuperAdminDashboard:
    def __init__(
        self,
        page: ft.Page,
        user_data,
        on_logout=None,
        on_switch_account=None,
        allow_all_companies=True,
        locked_company_id=None,
        dashboard_title="СуперАдмін",
    ):
        self.page = page
        self.db = DataManager()
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.t = i18n.bind((self.user_data.get("id") or "").strip())
        self._dashboard_title_overridden = dashboard_title not in (None, "СуперАдмін")
        self.on_logout = on_logout
        self.on_switch_account = on_switch_account
        self.allow_all_companies = bool(allow_all_companies)
        self.locked_company_id = locked_company_id or self.user_data.get("company_id")
        # Set initial company schema; re-call set_company() when switching companies.
        if self.locked_company_id:
            self.db.set_company(
                self.locked_company_id,
                self.user_data.get("company_name", ""),
                manager_id=self.user_data.get("id", ""),
            )
        self.dashboard_title = dashboard_title or "СуперАдмін"
        self.current_tab = "overview"
        self.selected_company_id = (
            self.locked_company_id if not self.allow_all_companies else None
        )
        self.selected_region_name = None
        self._catalog_sync_warmup_done = set()
        self._company_sections_state = {}
        self.currencies_list = ["USD", "UAH", "EUR", "PLN", "CNY"]
        self.content_area = ft.Column(expand=True, scroll=ft.ScrollMode.ADAPTIVE)
        self._sync_poll_started = False
        self._gsm_tg_flush_loop_started = False
        self._sync_last_pulses = {}
        self._sync_dirty_scopes = set()
        self._last_sa_sync_pulse_reload_mono = 0.0
        self._pending_sa_sync_pulse_reload = False
        self._logout_in_progress = False
        self._sa_header_ref = None
        self._sa_modules_row_ref = None
        self._sa_expanded_companies: set[str] = set()
        # Персистить розгорнуті/згорнуті ExpansionTile у вкладці "Інфраструктура"
        # (дані доступу, БД компаній) через будь-які перемальовування вкладки.
        self._infra_expanded_tiles: dict = {}
        # Те саме для розгорнутих переписок у "Підтримка"/"Заявки" — інакше
        # відкрита переписка згортається на кожному фоновому sync-pulse.
        self._bot_thread_expanded: dict = {}

    # ── nav-icon helper ───────────────────────────────────────────────────────
    def _sa_nav_btn(
        self, text: str, nav_key: str, default_icon, on_click, height: int = 36
    ):
        mid = (self.user_data.get("id") or "").strip()
        pack = _themes.get_active_nav_pack(mid)
        pack_type = pack.get("type", "material")
        raw_icon = pack.get(nav_key, default_icon) if nav_key else default_icon
        if pack_type == "text":
            glyph = raw_icon if isinstance(raw_icon, str) else "•"
            return ft.FilledButton(
                content=ft.Row(
                    [ft.Text(glyph, size=14), ft.Text(text, size=12)],
                    spacing=4,
                    tight=True,
                ),
                on_click=on_click,
                height=height,
            )
        return ft.FilledButton(text, icon=raw_icon, on_click=on_click, height=height)

    def _rebuild_sa_nav_buttons(self) -> None:
        row = self._sa_modules_row_ref
        if row is None:
            return
        h = 36
        row.controls = [
            ft.FilledButton(
                self.t("nav.approvals", "Підтвердити заявки"),
                icon=ft.Icons.VERIFIED_USER,
                on_click=self.show_approvals,
                height=h,
            ),
            ft.FilledButton(
                self.t("nav.support_inbox", "Підтримка"),
                icon=ft.Icons.SUPPORT_AGENT,
                on_click=self.show_support_inbox,
                height=h,
            ),
            ft.FilledButton(
                self.t("nav.leads_inbox", "Заявки"),
                icon=ft.Icons.MARK_CHAT_UNREAD,
                on_click=self.show_leads_inbox,
                height=h,
            ),
            ft.FilledButton(
                self.t("nav.users", "Користувачі"), icon=ft.Icons.GROUP, on_click=self.show_users, height=h
            ),
            ft.FilledButton(
                self.t("nav.company", "Компанія"),
                icon=ft.Icons.BUSINESS,
                on_click=self.show_company_settings,
                visible=not self._is_company_scoped(),
                height=h,
            ),
            ft.FilledButton(
                self.t("nav.app_updates", "Оновлення"),
                icon=ft.Icons.SYSTEM_UPDATE_ALT,
                on_click=self.show_app_updates,
                visible=not self._is_company_scoped(),
                height=h,
            ),
            self._sa_nav_btn(
                self.t("nav.extensions", "Розширення"), "extensions", ft.Icons.EXTENSION, self.show_extensions, h
            ),
            self._sa_nav_btn(
                self.t("nav.web_site", "Web сайт"),
                "web_site",
                ft.Icons.PUBLIC,
                self.show_web_site,
                h,
            ),
            ft.FilledButton(
                self.t("nav.languages", "Мови"),
                icon=ft.Icons.LANGUAGE,
                on_click=self.show_languages,
                height=h,
            ),
            self._sa_nav_btn(
                self.t("nav.settings_tooltip", "Налаштування"),
                "settings",
                ft.Icons.SETTINGS,
                self.show_infrastructure_settings,
                h,
            ),
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
                        "nav.help_body_sa",
                        "Вгорі — кнопки розділів суперадміністрування (заявки,"
                        " користувачі, компанія, оновлення, розширення, мови,"
                        " налаштування). Праворуч — вибір компанії, кнопка «Огляд»"
                        " повертає на головний екран, є перемикач мови та кнопка"
                        " виходу. Розділ «Мови» дозволяє керувати перекладами"
                        " інтерфейсу для всіх користувачів.",
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

    def _build_sa_lang_switcher_box(self, manager_id: str, bg_color: str) -> ft.Container:
        box = i18n.build_language_switcher(
            self.db,
            lambda code: self._apply_sa_language_live(code),
            manager_id=manager_id,
            bg_color=bg_color,
        )
        self._sa_lang_switcher_box_ref = box
        return box

    def _apply_sa_language_live(self, lang_code: str) -> None:
        mid = (self.user_data.get("id") or "").strip()
        i18n.save_manager_language(self.db, mid, lang_code)
        try:
            _live_th = _themes.get_active_theme(mid)
            box = getattr(self, "_sa_lang_switcher_box_ref", None)
            if box is not None:
                fresh = i18n.build_language_switcher(
                    self.db,
                    lambda code: self._apply_sa_language_live(code),
                    manager_id=mid,
                    bg_color=_live_th.get("chip_bg"),
                )
                box.content = fresh.content
                box.update()
        except Exception:
            pass
        self.build()

    def _apply_sa_theme_live(self, theme_key: str) -> None:
        mid = (self.user_data.get("id") or "").strip()
        _themes.set_active_theme(mid, theme_key)
        _live_th = _themes.get_active_theme(mid)
        bar_color = _themes.get_bar_color(_live_th)
        if self._sa_header_ref is not None:
            self._sa_header_ref.bgcolor = bar_color
            with contextlib.suppress(Exception):
                self._sa_header_ref.update()
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

    def _apply_sa_nav_live(self, pack_key: str) -> None:
        mid = (self.user_data.get("id") or "").strip()
        _themes.set_active_nav_pack(mid, pack_key)
        self._rebuild_sa_nav_buttons()

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
            self._show_current_tab()
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
        self._show_current_tab()

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

    def build(self):
        self.page.clean()
        self.page.bgcolor = ft.Colors.WHITE
        self.page.padding = ft.padding.all(0)

        _sa_mgr_id = (self.user_data.get("id") or "").strip()
        try:
            _ws = self.db.get_manager_workspace_state(_sa_mgr_id) or {}
            _themes.set_active_theme(_sa_mgr_id, _ws.get("ui_theme"))
            _themes.set_active_nav_pack(_sa_mgr_id, _ws.get("ui_nav_pack"))
            i18n.set_active_language(_sa_mgr_id, _ws.get("ui_language"))
        except Exception:
            pass
        if not self._dashboard_title_overridden:
            self.dashboard_title = self.t("nav.superadmin_title", "СуперАдмін")
        _active_sa_theme = _themes.get_active_theme(_sa_mgr_id)
        _bar_color = _themes.get_bar_color(_active_sa_theme)
        _th_text = _active_sa_theme["text"]
        _th_muted = _active_sa_theme["muted"]
        _th_bg = _active_sa_theme["bg"]
        _th_panel = _active_sa_theme["panel"]
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

        _dd_accent = _active_sa_theme.get("accent", "#3B82F6")
        _dd_border = _active_sa_theme.get("border", "#CBD5E1")
        _dd_icon_color = _active_sa_theme.get("icon_color", _dd_accent)

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
            label="Регіон",
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
                    [ft.dropdown.Option(key=ALL_REGIONS_KEY, text="Всі регіони")]
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

        from views.mobile_layout_helpers import (
            is_mobile as _is_mobile_device,
            is_mobile_landscape as _is_mobile_landscape,
            register_resize_listener as _register_resize_listener,
        )

        page_w = int(getattr(self.page, "width", 0) or 0)
        if page_w <= 0:
            win = getattr(self.page, "window", None)
            page_w = int(getattr(win, "width", 0) or 0) if win else 0
        # Phones always get the compact header regardless of orientation --
        # see dashboard.py's identical fix for why page_w<980 alone isn't
        # enough (landscape width can cross 980 on some devices while still
        # being far too short vertically for the "large" sizing).
        is_small_header = bool(
            _is_mobile_device(self.page) or (page_w > 0 and page_w < 980)
        )

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
                        ft.Text(
                            self.dashboard_title, size=title_size, weight="bold", color=_th_text
                        ),
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
            datetime.now().strftime("%H:%M"),
            size=clock_size,
            weight="bold",
            color=_th_text,
        )
        self._clock_text = _clock_text
        if not getattr(self, "_clock_loop_started", False):
            self._clock_loop_started = True
            self.page.run_task(self._clock_loop)

        top_center = ft.Column(
            [
                _clock_text,
                ft.Text(
                    datetime.now().strftime("%d.%m.%Y"), size=date_size, color=_th_muted
                ),
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
                ft.OutlinedButton(
                    "",
                    icon=ft.Icons.HOME,
                    icon_color=_th_text,
                    tooltip=self.t("nav.overview", "Огляд"),
                    on_click=self.show_overview,
                    height=button_h,
                    style=ft.ButtonStyle(color=_th_text),
                ),
                self._build_sa_lang_switcher_box(_sa_mgr_id, _active_sa_theme.get("chip_bg")),
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

        def _build_sa_shell_header_row():
            # Same fix as dashboard.py: lay left/center/right side by side in
            # mobile landscape instead of letting them keep stacking full
            # width (ResponsiveRow's 'md' breakpoint is 980, which a phone
            # rarely crosses even sideways), which was eating most of the
            # short landscape height.
            if _is_mobile_landscape(self.page):
                return ft.Row(
                    [
                        ft.Container(content=top_left, expand=4),
                        ft.Container(content=top_center, expand=2),
                        ft.Container(content=top_right, expand=6),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
            return ft.ResponsiveRow(
                [
                    ft.Container(col={"xs": 12, "md": 4}, content=top_left),
                    ft.Container(col={"xs": 12, "md": 2}, content=top_center),
                    ft.Container(col={"xs": 12, "md": 6}, content=top_right),
                ],
                columns=12,
                spacing=8,
                run_spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )

        _sa_header_inner_column = ft.Column(
            [_build_sa_shell_header_row(), modules_row],
            spacing=10,
            tight=True,
        )

        header = ft.Container(
            content=_sa_header_inner_column,
            padding=ft.padding.symmetric(
                horizontal=12,
                vertical=(4 if _is_mobile_landscape(self.page) else (8 if is_small_header else 10)),
            ),
            bgcolor=_bar_color,
            border_radius=12,
            margin=ft.margin.only(bottom=10),
        )
        self._sa_header_ref = header
        self._sa_modules_row_ref = modules_row
        self._rebuild_sa_nav_buttons()

        def _on_sa_shell_resized(_e=None):
            try:
                if not _is_mobile_device(self.page):
                    return
                _sa_header_inner_column.controls = [
                    _build_sa_shell_header_row(),
                    modules_row,
                ]
                header.padding = ft.padding.symmetric(
                    horizontal=12,
                    vertical=(4 if _is_mobile_landscape(self.page) else 8),
                )
                header.update()
            except Exception:
                logger.exception("superadmin shell header resize reflow failed")

        _register_resize_listener(self.page, "sa_shell_header", _on_sa_shell_resized)

        self.page.add(
            self._wrap_with_background(
                ft.Column(
                    [
                        header,
                        ft.Container(
                            content=self.content_area,
                            expand=True,
                            bgcolor=ft.Colors.WHITE,
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
            self._sync_last_pulses = self._load_sync_snapshot()
            self._sync_poll_started = True
            self.page.run_task(self._sync_poll_loop)
        if not self._gsm_tg_flush_loop_started:
            self._gsm_tg_flush_loop_started = True
            from gsm_parser_runner import gsm_local_tg_flush_loop

            self.page.run_task(gsm_local_tg_flush_loop, self.db, lambda: True)
        self._show_current_tab()
        self.page.run_task(self._background_warmup)
        # Expose callback so Settings can refresh background without restart.
        try:
            setattr(self.page, "_refresh_background_now", self._refresh_background_now)
        except Exception:
            pass

    def _show_current_tab(self):
        if self.current_tab == "approvals":
            self.show_approvals(None)
            return
        if self.current_tab == "users":
            self.show_users(None)
            return
        if self.current_tab == "company":
            self.show_company_settings(None)
            return
        if self.current_tab == "app_updates":
            self.show_app_updates(None)
            return
        if self.current_tab == "copy_setup":
            self.show_copy_setup(None)
            return
        if self.current_tab == "extensions":
            self.show_extensions(None)
            return
        if self.current_tab == "web_site":
            self.show_web_site(None)
            return
        if self.current_tab == "languages":
            self.show_languages(None)
            return
        if self.current_tab == "infrastructure":
            self.show_infrastructure_settings(None)
            return
        if self.current_tab == "support_inbox":
            self.show_support_inbox(None)
            return
        if self.current_tab == "leads_inbox":
            self.show_leads_inbox(None)
            return
        if self.current_tab == "hierarchy":
            self.show_hierarchy(None)
            return
        self.show_overview(None)

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

    def show_overview(self, e):
        self.current_tab = "overview"
        self.content_area.controls.clear()
        self.content_area.controls.append(
            ft.Row(
                [ft.ProgressRing(width=20, height=20, stroke_width=2), ft.Text("Завантаження огляду…")],
                spacing=10,
            )
        )
        self.page.update()
        self.page.run_task(self._load_overview_async)

    async def _load_overview_async(self):
        data = await asyncio.to_thread(self._compute_overview_data)
        # User may have clicked to a different tab while this was loading —
        # don't stomp on whatever they're looking at now.
        if self.current_tab != "overview":
            return
        self._render_overview(data)

    def _compute_overview_data(self) -> dict:
        """Runs off the UI thread (asyncio.to_thread) — 7 sequential
        synchronous self.db.* calls used to block the UI directly here on
        every single visit to Огляд (the default landing tab)."""
        visible_company_ids = {c.get("id") for c in self._visible_companies()}

        pending_rows = self.db.get_pending_managers()
        pending_rows = [
            p
            for p in pending_rows
            if isinstance(p, dict) and p.get("company_id") in visible_company_ids
        ]
        pending_count = len(pending_rows)

        support_unread = 0
        leads_unread = 0
        with contextlib.suppress(Exception):
            support_unread = self.db.count_unread_bot_conversations("support")
        with contextlib.suppress(Exception):
            leads_unread = self.db.count_unread_bot_conversations("leads")

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

        companies_count = len(
            [
                c
                for c in self.db.get_all_companies()
                if isinstance(c, dict) and c.get("id")
            ]
        )

        xiaomi_missing_count = 0
        with contextlib.suppress(Exception):
            xiaomi_missing_count, _ = self.db.get_xiaomi_devices_missing_dimensions()

        empty_translations_count = 0
        with contextlib.suppress(Exception):
            empty_translations_count = self.db.count_empty_translations()

        return {
            "pending_count": pending_count,
            "support_unread": support_unread,
            "leads_unread": leads_unread,
            "users_count": users_count,
            "companies_count": companies_count,
            "xiaomi_missing_count": xiaomi_missing_count,
            "empty_translations_count": empty_translations_count,
        }

    def _render_overview(self, data: dict):
        self.content_area.controls.clear()
        self.content_area.controls.extend(
            [
                ft.Text("Огляд", size=24, weight="bold"),
                ft.Row(
                    [
                        self._metric_card(
                            "Реєстрації",
                            str(data["pending_count"]),
                            "orange",
                            on_click=lambda _: self.show_approvals(None),
                        ),
                        self._metric_card(
                            "Підтримка",
                            str(data["support_unread"]),
                            "teal",
                            on_click=lambda _: self.show_support_inbox(None),
                        ),
                        self._metric_card(
                            "Заявки",
                            str(data["leads_unread"]),
                            "indigo",
                            on_click=lambda _: self.show_leads_inbox(None),
                        ),
                        self._metric_card(
                            "Компаній",
                            str(data["companies_count"]),
                            "blue",
                            on_click=lambda _: self.show_company_settings(None),
                        ),
                        self._metric_card(
                            "Користувачів",
                            str(data["users_count"]),
                            "green",
                            on_click=lambda _: self.show_users(None),
                        ),
                        self._metric_card(
                            "Xiaomi без розмірів",
                            str(data["xiaomi_missing_count"]),
                            "purple",
                            on_click=lambda _: self._show_xiaomi_dimensions_editor(),
                        ),
                        self._metric_card(
                            "Порожні переклади",
                            str(data["empty_translations_count"]),
                            "red",
                            on_click=lambda _: self.show_languages_filtered_empty(None),
                        ),
                    ],
                    wrap=True,
                ),
            ]
        )

        self.page.update()

    def _metric_card(self, title, value, color, on_click=None):
        return ft.Container(
            width=220,
            padding=15,
            border=ft.border.all(1, "#E5E7EB"),
            border_radius=10,
            ink=True,
            on_click=on_click,
            tooltip=f"Відкрити: {title}" if on_click else None,
            content=ft.Column(
                [
                    ft.Text(title, size=12, color="grey700"),
                    ft.Text(value, size=26, weight="bold", color=color),
                ],
                spacing=3,
            ),
        )

    # ------------------------------------------------------------------ #
    #  Telegram bot inbox (Підтримка / Заявки) — mirrors forward_bot.py    #
    # ------------------------------------------------------------------ #
    def show_support_inbox(self, e):
        self.current_tab = "support_inbox"
        self._build_bot_inbox_tab("support", "Підтримка")

    def show_leads_inbox(self, e):
        self.current_tab = "leads_inbox"
        self._build_bot_inbox_tab("leads", "Заявки")

    def _build_bot_inbox_tab(self, bot_name: str, title: str):
        self.content_area.controls.clear()
        progress = ft.ProgressRing(width=20, height=20, stroke_width=2)
        list_column = ft.Column(spacing=8)
        self.content_area.controls.extend(
            [
                ft.Text(title, size=24, weight="bold"),
                progress,
                list_column,
            ]
        )
        self.page.update()

        async def _load():
            threads = await asyncio.to_thread(
                self.db.get_bot_conversation_threads, bot_name
            )
            progress.visible = False
            if not threads:
                list_column.controls.append(
                    ft.Text("Звернень немає", color="grey600")
                )
            else:
                list_column.controls.extend(
                    self._build_bot_thread_tile(bot_name, t) for t in threads
                )
            self.page.update()

        self.page.run_task(_load)

    def _build_bot_thread_tile(self, bot_name: str, thread: dict):
        tg_user_id = thread.get("tg_user_id")
        name = thread.get("tg_full_name") or "Без імені"
        username = f"@{thread['tg_username']}" if thread.get("tg_username") else ""
        unread = int(thread.get("unread_count") or 0)

        messages_container = ft.Container(content=ft.Text("", size=1))
        loaded = {"done": False}
        thread_key = (bot_name, tg_user_id)
        is_open = self._bot_thread_expanded.get(thread_key, False)

        def _load_thread_now():
            if loaded["done"]:
                return
            loaded["done"] = True

            async def _load_thread():
                msgs = await asyncio.to_thread(
                    self.db.get_bot_conversation_messages, bot_name, tg_user_id
                )
                await asyncio.to_thread(
                    self.db.mark_bot_conversation_read, bot_name, tg_user_id
                )
                messages_container.content = self._build_bot_thread_view(
                    bot_name, tg_user_id, msgs
                )
                self.page.update()

            self.page.run_task(_load_thread)

        def _on_expand(ev):
            expanded_now = bool(getattr(ev.control, "expanded", False))
            self._bot_thread_expanded[thread_key] = expanded_now
            if expanded_now:
                _load_thread_now()

        if is_open:
            _load_thread_now()

        return ft.ExpansionTile(
            title=ft.Row(
                [
                    ft.Text(
                        f"{name} {username}".strip(), expand=True, weight="bold"
                    ),
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=8, vertical=2),
                        border_radius=6,
                        bgcolor="#FEE2E2",
                        content=ft.Text(str(unread), size=11, color="#991B1B"),
                        visible=unread > 0,
                    ),
                ]
            ),
            expanded=is_open,
            on_change=_on_expand,
            controls=[messages_container],
        )

    def _build_bot_thread_view(self, bot_name: str, tg_user_id, msgs: list):
        history = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, height=260)

        def _bubble(text: str, is_out: bool):
            return ft.Row(
                [
                    ft.Container(
                        content=ft.Text(text, selectable=True),
                        padding=10,
                        border_radius=8,
                        bgcolor="#DBEAFE" if is_out else "#F3F4F6",
                        width=320,
                    )
                ],
                alignment=(
                    ft.MainAxisAlignment.END if is_out else ft.MainAxisAlignment.START
                ),
            )

        for m in msgs:
            history.controls.append(
                _bubble(m.get("message_text") or "", m.get("direction") == "out")
            )

        reply_field = ft.TextField(
            hint_text="Відповідь...",
            expand=True,
            multiline=True,
            min_lines=1,
            max_lines=4,
        )
        send_btn = ft.IconButton(ft.Icons.SEND)

        def _send(_):
            text = (reply_field.value or "").strip()
            if not text:
                return
            send_btn.disabled = True
            self.page.update()

            async def _run():
                ok = await asyncio.to_thread(
                    self.db.send_bot_reply, bot_name, tg_user_id, text
                )
                send_btn.disabled = False
                if ok:
                    reply_field.value = ""
                    history.controls.append(_bubble(text, True))
                else:
                    self._show_snack("Не вдалося надіслати", "red")
                self.page.update()

            self.page.run_task(_run)

        send_btn.on_click = _send

        return ft.Column([history, ft.Row([reply_field, send_btn])], spacing=8)

    # ------------------------------------------------------------------ #
    #  Xiaomi missing-dimensions bulk editor                              #
    # ------------------------------------------------------------------ #
    def _show_xiaomi_dimensions_editor(self):
        """Dialog: table of Xiaomi devices with regions/models but no size/diagonal."""
        try:
            _count, rows = self.db.get_xiaomi_devices_missing_dimensions()
        except Exception:
            rows = []

        if not rows:
            dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text("Xiaomi — розміри/діагоналі"),
                content=ft.Text("Всі пристрої вже мають розміри та діагоналі ✓"),
                actions=[
                    ft.FilledButton("OK", on_click=lambda _: self.page.pop_dialog())
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
            self.page.show_dialog(dlg)
            return

        # ── helpers ─────────────────────────────────────────────────────
        SEP_STYLE = ft.TextStyle(size=13, color="#64748B", weight=ft.FontWeight.W_700)

        def _dim_field(val, hint, width=74):
            return ft.TextField(
                value=str(val or "").replace(",", ".").strip(),
                hint_text=hint,
                width=width,
                dense=True,
                keyboard_type=ft.KeyboardType.NUMBER,
                content_padding=ft.padding.symmetric(horizontal=4, vertical=4),
                text_size=12,
            )

        def _diag_field(val):
            return ft.TextField(
                value=str(val or "").replace(",", ".").strip(),
                hint_text="6.73",
                suffix=ft.Text('"'),
                width=68,
                dense=True,
                keyboard_type=ft.KeyboardType.NUMBER,
                content_padding=ft.padding.symmetric(horizontal=4, vertical=4),
                text_size=12,
            )

        # ── build variant row ────────────────────────────────────────────
        def _build_variant_row(v_ref: dict) -> ft.Control:
            """One row per variant: region | models | L x W x T | diagonal."""
            f_L = _dim_field(v_ref.get("L"), "Дов.")
            f_W = _dim_field(v_ref.get("W"), "Шир.")
            f_T = _dim_field(v_ref.get("T"), "Тов.")
            f_diag = _diag_field(v_ref.get("diagonal"))

            def _on_change_L(e):
                v_ref["L"] = e.control.value

            def _on_change_W(e):
                v_ref["W"] = e.control.value

            def _on_change_T(e):
                v_ref["T"] = e.control.value

            def _on_change_diag(e):
                v_ref["diagonal"] = e.control.value

            f_L.on_change = _on_change_L
            f_W.on_change = _on_change_W
            f_T.on_change = _on_change_T
            f_diag.on_change = _on_change_diag

            region = str(v_ref.get("region") or "")
            models = str(v_ref.get("models") or "")
            has_dims = bool(
                str(v_ref.get("L") or "").strip() and str(v_ref.get("W") or "").strip()
            ) and bool(str(v_ref.get("diagonal") or "").strip())

            return ft.Container(
                content=ft.Row(
                    [
                        ft.Container(
                            ft.Text(region, size=11, weight=ft.FontWeight.W_600),
                            width=100,
                        ),
                        ft.Container(
                            ft.SelectionArea(
                                content=ft.Text(models, size=10, color="#475569", no_wrap=False)
                            ),
                            width=180,
                            expand=False,
                        ),
                        ft.Row(
                            [
                                f_L,
                                ft.Text("×", style=SEP_STYLE),
                                f_W,
                                ft.Text("×", style=SEP_STYLE),
                                f_T,
                            ],
                            spacing=3,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        f_diag,
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                bgcolor="#F0FDF4" if has_dims else "#FFF7ED",
                border_radius=4,
                padding=ft.padding.symmetric(horizontal=6, vertical=4),
            )

        # ── build per-device block ──────────────────────────────────────
        all_device_variants: dict[str, list[dict]] = {}  # device_id → mutable variants

        device_blocks: list[ft.Control] = []
        for dev in rows:
            device_id = str(dev.get("id") or "")
            model_name = str(dev.get("model_name") or "Невідомо")
            variants: list[dict] = [dict(v) for v in (dev.get("variants") or [])]
            all_device_variants[device_id] = variants

            variant_rows = [_build_variant_row(v) for v in variants]

            def _make_save_cb(did, mname):
                def _save(_):
                    try:
                        devvars = all_device_variants[did]
                        clean = []
                        for v in devvars:
                            clean.append(
                                {
                                    "region": str(v.get("region") or "").strip(),
                                    "models": str(v.get("models") or "").strip(),
                                    "L": str(v.get("L") or "")
                                    .replace(",", ".")
                                    .strip(),
                                    "W": str(v.get("W") or "")
                                    .replace(",", ".")
                                    .strip(),
                                    "T": str(v.get("T") or "")
                                    .replace(",", ".")
                                    .strip(),
                                    "diagonal": str(v.get("diagonal") or "")
                                    .replace(",", ".")
                                    .strip(),
                                    "source": str(v.get("source") or "").strip(),
                                }
                            )
                        ok = self.db.save_gsm_regional_variants(did, clean)
                        if ok:
                            self._show_snack(f"Збережено: {mname}", "green")
                        else:
                            self._show_snack(f"Помилка збереження: {mname}", "red")
                    except Exception as exc:
                        self._show_snack(f"Помилка: {exc}", "red")

                return _save

            save_btn = ft.FilledButton(
                "Зберегти",
                icon=ft.Icons.SAVE_OUTLINED,
                on_click=_make_save_cb(device_id, model_name),
                style=ft.ButtonStyle(
                    bgcolor="#0F7B53",
                    padding=ft.padding.symmetric(horizontal=10, vertical=5),
                    text_style=ft.TextStyle(size=11),
                ),
            )

            device_blocks.append(
                ft.Container(
                    content=ft.Column(
                        [
                            ft.Row(
                                [
                                    ft.Container(
                                        ft.SelectionArea(
                                            content=ft.Text(
                                                model_name,
                                                size=13,
                                                weight=ft.FontWeight.W_700,
                                            )
                                        ),
                                        expand=True,
                                    ),
                                    save_btn,
                                ],
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            *variant_rows,
                        ],
                        spacing=4,
                        tight=True,
                    ),
                    border=ft.border.all(1, "#E2E8F0"),
                    border_radius=8,
                    padding=ft.padding.symmetric(horizontal=10, vertical=8),
                )
            )

        # ── assemble dialog ──────────────────────────────────────────────
        hdr_row = ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        ft.Text(
                            "Регіон",
                            size=10,
                            weight=ft.FontWeight.W_700,
                            color="#64748B",
                        ),
                        width=100,
                    ),
                    ft.Container(
                        ft.Text(
                            "Номер моделі",
                            size=10,
                            weight=ft.FontWeight.W_700,
                            color="#64748B",
                        ),
                        width=180,
                    ),
                    ft.Container(
                        ft.Text(
                            "Розмір (Д × Ш × Т)",
                            size=10,
                            weight=ft.FontWeight.W_700,
                            color="#64748B",
                        ),
                        width=277,
                    ),
                    ft.Container(
                        ft.Text(
                            "Діагональ",
                            size=10,
                            weight=ft.FontWeight.W_700,
                            color="#64748B",
                        ),
                        width=80,
                    ),
                ],
                spacing=8,
            ),
            bgcolor="#F8FAFC",
            padding=ft.padding.symmetric(horizontal=6, vertical=4),
            border_radius=ft.border_radius.only(top_left=6, top_right=6),
        )

        scroll_col = ft.Column(
            [hdr_row, *device_blocks],
            spacing=8,
            scroll=ft.ScrollMode.ADAPTIVE,
        )

        try:
            page_h = (
                getattr(self.page, "height", None)
                or getattr(getattr(self.page, "window", None), "height", None)
                or 800
            )
            dlg_h = max(300, min(620, int(page_h * 0.78)))
        except Exception:
            dlg_h = 560

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(
                f"Xiaomi — розміри / діагоналі ({len(rows)} пристроїв)",
                size=16,
                weight=ft.FontWeight.W_700,
            ),
            content=ft.Container(
                content=scroll_col,
                width=685,
                height=dlg_h,
            ),
            actions=[ft.TextButton("Закрити", on_click=lambda _: self.page.pop_dialog())],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

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
            region_options = [
                ft.dropdown.Option(key=r.get("id"), text=r.get("name")) for r in regions
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
                    if len(regions) > 1
                    else (regions[0].get("id") if regions else None)
                ),
                width=320,
                visible=len(regions) > 1,
            )
            tree_col = ft.Column([], spacing=2)

            def rebuild_tree(region_id=None):
                all_checks.clear()
                nodes = [n for n in all_nodes if isinstance(n, dict)]
                effective_region_ids = None
                if region_id and region_id != ALL_REGIONS_KEY:
                    effective_region_ids = {region_id}
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
                            "Ліміт користувачів компанії вичерпано, збільшіть ліміт у налаштуваннях компанії",
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
        new_company_storage_f = ft.Dropdown(
            label="Де зберігати дані нової компанії",
            border_radius=10,
            value="vps",
            options=[
                ft.dropdown.Option("vps", "VPS"),
                ft.dropdown.Option("local", "Локально"),
            ],
            visible=company_name_f.visible,
        )
        new_company_vps_host_f = ft.TextField(
            label="VPS хост", border_radius=10, visible=company_name_f.visible
        )
        new_company_vps_port_f = ft.TextField(
            label="VPS порт",
            border_radius=10,
            value="5432",
            visible=company_name_f.visible,
        )
        new_company_vps_user_f = ft.TextField(
            label="VPS користувач БД",
            border_radius=10,
            value="postgres",
            visible=company_name_f.visible,
        )
        new_company_vps_password_f = ft.TextField(
            label="VPS пароль БД",
            border_radius=10,
            password=True,
            can_reveal_password=True,
            visible=company_name_f.visible,
        )
        new_company_vps_hint = ft.Text(
            "Адресу можна залишити порожньою — компанія створиться, але "
            "збереження даних буде заблоковано, поки хтось не вкаже адресу "
            "VPS пізніше.",
            size=11,
            color="grey600",
            visible=company_name_f.visible,
        )

        def _update_new_company_storage_fields_visibility():
            is_new_and_vps = (
                company_name_f.visible and new_company_storage_f.value == "vps"
            )
            for f in (
                new_company_vps_host_f,
                new_company_vps_port_f,
                new_company_vps_user_f,
                new_company_vps_password_f,
                new_company_vps_hint,
            ):
                f.visible = is_new_and_vps

        def _on_new_company_storage_change(_):
            _update_new_company_storage_fields_visibility()
            if self.page:
                self.page.update()

        new_company_storage_f.on_change = _on_new_company_storage_change
        new_company_storage_f.on_select = _on_new_company_storage_change
        full_name_f = ft.TextField(label="Ім'я", border_radius=10)
        email_f = ft.TextField(label="Email", border_radius=10)
        phone_f = ft.TextField(label="Телефон", border_radius=10)
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
            for reg in region_rows:
                if not isinstance(reg, dict) or not reg.get("id"):
                    continue
                rid = reg.get("id")
                cb, cb_row = self._wide_checkbox_row(reg.get("name", "Регіон"), False)
                regions_checks[rid] = cb
                regions_checks_block.controls.append(cb_row)
            if not regions_checks_block.controls:
                regions_checks_block.controls.append(
                    ft.Text("У компанії немає регіонів", size=11, color="grey600")
                )

            nodes = self.db.get_company_catalog_nodes(company_id)
            # Keep hierarchy correct: perform one warm-up sync per company per session.
            if company_id not in self._catalog_sync_warmup_done:
                self.db.sync_company_catalog_nodes(company_id)
                self._catalog_sync_warmup_done.add(company_id)
                nodes = self.db.get_company_catalog_nodes(company_id)
            allowed_types = {"category", "subcategory", "series", "brand"}
            nodes = [
                n
                for n in nodes
                if isinstance(n, dict)
                and n.get("id")
                and n.get("node_type") in allowed_types
            ]
            tree_controls, tree_checks = _build_catalog_tree(nodes, regions_by_id)
            catalog_checks.update(tree_checks)
            catalog_checks_block.controls = tree_controls
            if not tree_controls:
                catalog_checks_block.controls.append(
                    ft.Text(
                        "У компанії немає елементів каталогу", size=11, color="grey600"
                    )
                )

        def on_create_company_change(_):
            company_name_f.visible = (
                create_company_f.value == "__new__" and not self._is_company_scoped()
            )
            new_company_storage_f.visible = company_name_f.visible
            _update_new_company_storage_fields_visibility()
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
                new_storage_mode = new_company_storage_f.value or "vps"
                new_vps_conn = None
                if new_storage_mode == "vps":
                    new_vps_host = (new_company_vps_host_f.value or "").strip()
                    if new_vps_host:
                        new_vps_conn = {
                            "host": new_vps_host,
                            "port": (new_company_vps_port_f.value or "5432").strip(),
                            "user": (new_company_vps_user_f.value or "").strip(),
                            "password": new_company_vps_password_f.value or "",
                        }
                company_id = self.db.create_company(
                    company_name,
                    storage_mode=new_storage_mode,
                    vps_conn=new_vps_conn,
                    allow_pending=True,
                )
                if company_id:
                    self.selected_company_id = company_id
                    if new_storage_mode == "vps" and not new_vps_conn:
                        self._show_snack(
                            "Компанію створено — очікує адресу VPS", "orange"
                        )
            payload = {
                "company_id": company_id,
                "full_name": full_name_f.value.strip(),
                "email": email_f.value.strip(),
                "phone": phone_f.value.strip(),
                "role": role_f.value,
                "password": password_f.value,
                "parent_manager_id": None,
                "is_approved": True,
                "allowed_region_ids_json": [],
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
            new_id = self.db.create_manager(payload)
            if new_id:
                self._show_snack("Користувача створено", "green")
                self.show_users(None)
            else:
                self._show_snack("Помилка створення", "red")

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
                width=120,
                height=36,
                text_size=12,
                content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
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
                                height=28,
                                style=ft.ButtonStyle(
                                    text_style=ft.TextStyle(size=11),
                                    padding=ft.padding.symmetric(
                                        horizontal=8, vertical=2
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
            if normalized_role in {"суперадмін", "суперадмин", "superadmin"} or m.get(
                "id"
            ) == self.user_data.get("id"):
                continue

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
                        ft.DataCell(
                            ft.Text(
                                str(m.get("email") or ""),
                                size=12,
                                selectable=True,
                            )
                        ),
                        ft.DataCell(
                            ft.Text(
                                str(m.get("phone") or ""),
                                size=12,
                                selectable=True,
                            )
                        ),
                        ft.DataCell(
                            ft.Row(
                                [
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
                                            "Заблокувати"
                                            if not is_blocked
                                            else "Розблокувати"
                                        ),
                                        on_click=lambda _, manager=m: block_toggle_click(
                                            None, manager
                                        ),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.DELETE,
                                        icon_color="red",
                                        tooltip="Видалити",
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

        def _make_collapsible_section(title, controls, initially_open=False):
            is_open = self._company_sections_state.get(title, initially_open)
            body = ft.Column(controls, spacing=8, visible=is_open)
            toggle_btn = ft.TextButton(
                title,
                icon=(
                    ft.Icons.KEYBOARD_ARROW_DOWN
                    if is_open
                    else ft.Icons.KEYBOARD_ARROW_RIGHT
                ),
                style=ft.ButtonStyle(
                    padding=ft.padding.symmetric(horizontal=0, vertical=0)
                ),
            )

            def _toggle(_):
                body.visible = not body.visible
                self._company_sections_state[title] = body.visible
                toggle_btn.icon = (
                    ft.Icons.KEYBOARD_ARROW_DOWN
                    if body.visible
                    else ft.Icons.KEYBOARD_ARROW_RIGHT
                )
                self.page.update()

            toggle_btn.on_click = _toggle
            return ft.Card(
                content=ft.Container(
                    padding=15,
                    content=ft.Column([toggle_btn, body], spacing=6),
                )
            )

        self.content_area.controls.extend(
            [
                ft.Text("Користувачі", size=24, weight="bold"),
                _make_collapsible_section(
                    "Створення користувача",
                    [
                        create_company_f,
                        company_name_f,
                        new_company_storage_f,
                        new_company_vps_host_f,
                        ft.Row(
                            [new_company_vps_port_f, new_company_vps_user_f],
                            spacing=10,
                            wrap=True,
                            run_spacing=8,
                        ),
                        new_company_vps_password_f,
                        new_company_vps_hint,
                        ft.Row(
                            [full_name_f, role_f], spacing=10, wrap=True, run_spacing=8
                        ),
                        ft.Row(
                            [email_f, phone_f], spacing=10, wrap=True, run_spacing=8
                        ),
                        ft.Row([password_f], spacing=10, wrap=True, run_spacing=8),
                        ft.FilledButton("Створити", on_click=create_click),
                    ],
                    initially_open=False,
                ),
                ft.Divider(),
                ft.Text("Список компанії", weight="bold"),
                ft.DataTable(
                    columns=[
                        ft.DataColumn(ft.Text("Ім'я")),
                        ft.DataColumn(ft.Text("Роль")),
                        ft.DataColumn(ft.Text("Email")),
                        ft.DataColumn(ft.Text("Телефон")),
                        ft.DataColumn(ft.Text("Дія")),
                        ft.DataColumn(ft.Text("Пароль")),
                    ],
                    data_row_min_height=48,
                    data_row_max_height=140,
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

    def show_company_settings(self, e):
        self.current_tab = "company"
        self.content_area.controls.clear()
        companies = self._visible_companies(
            force_refresh=self._consume_scope_dirty("company")
        )

        def _open_rename_dialog(company_row):
            cid = (company_row or {}).get("id")
            if not cid:
                return
            name_f = ft.TextField(
                label="Назва компанії",
                border_radius=10,
                value=(company_row.get("name") or ""),
                width=340,
            )
            max_users_f = ft.TextField(
                label="Ліміт користувачів (0 = без обмежень)",
                border_radius=10,
                value=str(company_row.get("max_users") or 0),
                width=340,
                keyboard_type=ft.KeyboardType.NUMBER,
            )

            def _save(_):
                new_name = (name_f.value or "").strip()
                if not new_name:
                    self._show_snack("Вкажіть назву", "orange")
                    return
                try:
                    max_users = max(0, int((max_users_f.value or "0").strip() or 0))
                except ValueError:
                    self._show_snack("Ліміт користувачів має бути числом", "orange")
                    return
                ok = self.db.update_company_profile(
                    cid, {"name": new_name, "max_users": max_users}
                )
                if ok:
                    self.page.pop_dialog()
                    self._show_snack("Збережено", "green")
                    self.show_company_settings(None)
                else:
                    self._show_snack("Не вдалося зберегти", "red")

            dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text(f"Перейменувати: {company_row.get('name', '')}"),
                content=ft.Column(
                    [name_f, max_users_f], tight=True, spacing=10, width=360
                ),
                actions=[
                    ft.TextButton("Скасувати", on_click=lambda _: self.page.pop_dialog()),
                    ft.FilledButton("Зберегти", on_click=_save),
                ],
            )
            self.page.show_dialog(dlg)

        def _open_delete_dialog(company_row):
            cid = (company_row or {}).get("id")
            name = (company_row or {}).get("name") or ""
            if not cid:
                return
            confirm_f = ft.TextField(
                label=f"Введіть «{name}» для підтвердження",
                border_radius=10,
                width=340,
            )

            def _confirm(_):
                if (confirm_f.value or "").strip() != name:
                    confirm_f.error_text = "Назва не збігається"
                    self.page.update()
                    return
                ok = self.db.delete_company(cid)
                self.page.pop_dialog()
                if ok:
                    with contextlib.suppress(Exception):
                        _delete_company_local_files(cid)
                    self._show_snack(f"Компанію «{name}» видалено", "green")
                    self.show_company_settings(None)
                else:
                    self._show_snack("Не вдалося видалити компанію", "red")

            dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text(f"Видалити компанію: {name}"),
                content=ft.Column(
                    [
                        ft.Text(
                            "Це видалить компанію та ВСІ її дані (замовлення, товари,"
                            " користувачів, налаштування) НАЗАВЖДИ. Дію неможливо"
                            " відмінити.",
                            color="red",
                            size=12,
                        ),
                        confirm_f,
                    ],
                    tight=True,
                    spacing=10,
                    width=360,
                ),
                actions=[
                    ft.TextButton("Скасувати", on_click=lambda _: self.page.pop_dialog()),
                    ft.FilledButton(
                        "Видалити назавжди",
                        bgcolor="red",
                        color="white",
                        on_click=_confirm,
                    ),
                ],
            )
            self.page.show_dialog(dlg)

        def _toggle_block(company_row):
            cid = (company_row or {}).get("id")
            name = (company_row or {}).get("name") or ""
            if not cid:
                return
            currently_blocked = bool((company_row or {}).get("is_blocked"))
            new_state = not currently_blocked

            def _apply(_=None):
                with contextlib.suppress(Exception):
                    self.page.pop_dialog()
                ok = self.db.set_company_blocked(cid, new_state)
                if ok:
                    msg = (
                        f"Компанію «{name}» заблоковано"
                        if new_state
                        else f"Компанію «{name}» розблоковано"
                    )
                    self._show_snack(msg, "orange" if new_state else "green")
                    self.show_company_settings(None)
                else:
                    self._show_snack("Не вдалося оновити статус", "red")

            if not new_state:
                _apply()
                return
            dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text(f"Блокувати компанію: {name}"),
                content=ft.Text(
                    "Усі користувачі компанії (будь-якої ролі) втратять можливість"
                    " увійти, доки ви не розблокуєте компанію знову."
                ),
                actions=[
                    ft.TextButton("Скасувати", on_click=lambda _: self.page.pop_dialog()),
                    ft.FilledButton("Блокувати", bgcolor="orange", on_click=_apply),
                ],
            )
            self.page.show_dialog(dlg)

        def _open_create_dialog():
            name_f = ft.TextField(
                label="Назва нової компанії", border_radius=10, width=340
            )
            storage_f = ft.Dropdown(
                label="Де зберігати дані",
                border_radius=10,
                width=340,
                value="vps",
                options=[
                    ft.dropdown.Option("vps", "VPS"),
                    ft.dropdown.Option("local", "Локально"),
                ],
            )
            vps_host_f = ft.TextField(label="VPS хост", border_radius=10, width=340)
            vps_port_f = ft.TextField(
                label="VPS порт", border_radius=10, width=340, value="5432"
            )
            vps_user_f = ft.TextField(
                label="VPS користувач БД", border_radius=10, width=340, value="postgres"
            )
            vps_password_f = ft.TextField(
                label="VPS пароль БД",
                border_radius=10,
                width=340,
                password=True,
                can_reveal_password=True,
            )
            vps_fields = ft.Column(
                [
                    vps_host_f,
                    vps_port_f,
                    vps_user_f,
                    vps_password_f,
                    ft.Text(
                        "Адресу можна залишити порожньою — компанія створиться, "
                        "але збереження даних буде заблоковано, поки хтось не "
                        "вкаже адресу VPS пізніше.",
                        size=11,
                        color="grey600",
                        width=340,
                    ),
                ],
                spacing=8,
                visible=True,
            )

            def _on_storage_change(_):
                vps_fields.visible = storage_f.value == "vps"
                if self.page:
                    self.page.update()

            storage_f.on_change = _on_storage_change
            storage_f.on_select = _on_storage_change

            def _create(_):
                name = (name_f.value or "").strip()
                if not name:
                    self._show_snack("Вкажіть назву", "orange")
                    return
                storage_mode = storage_f.value or "vps"
                vps_conn = None
                if storage_mode == "vps":
                    host = (vps_host_f.value or "").strip()
                    if host:
                        vps_conn = {
                            "host": host,
                            "port": (vps_port_f.value or "5432").strip(),
                            "user": (vps_user_f.value or "").strip(),
                            "password": vps_password_f.value or "",
                        }
                cid = self.db.create_company(
                    name,
                    storage_mode=storage_mode,
                    vps_conn=vps_conn,
                    allow_pending=True,
                )
                if cid:
                    self.page.pop_dialog()
                    if storage_mode == "vps" and not vps_conn:
                        self._show_snack(
                            "Компанію створено — очікує адресу VPS", "orange"
                        )
                    else:
                        self._show_snack("Компанію створено", "green")
                    self.show_company_settings(None)
                else:
                    self._show_snack("Не вдалося створити", "red")

            dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text("Нова компанія"),
                content=ft.Column(
                    [name_f, storage_f, vps_fields], tight=True, spacing=10, width=360
                ),
                actions=[
                    ft.TextButton("Скасувати", on_click=lambda _: self.page.pop_dialog()),
                    ft.FilledButton("Створити", on_click=_create),
                ],
            )
            self.page.show_dialog(dlg)

        valid_company_ids = [
            co.get("id")
            for co in companies
            if isinstance(co, dict) and co.get("id")
        ]
        toggles_by_company = self.db.get_companies_feature_toggles(valid_company_ids)

        def _feature_row(cid, key, enabled, disabled_by_cascade, switches):
            feat = TOGGLEABLE_FEATURES[key]

            def _on_change(e, cid_=cid, key_=key):
                new_val = bool(e.control.value)
                ok = self.db.set_company_feature_enabled(cid_, key_, new_val)
                if not ok:
                    e.control.value = not new_val
                    self._show_snack(
                        "Спочатку увімкніть Пакування", "orange"
                    )
                    self.page.update()
                    return
                if key_ == "packaging" and not new_val:
                    pack_stock_sw = switches.get("pack_stock")
                    if pack_stock_sw is not None:
                        pack_stock_sw.value = False
                        pack_stock_sw.disabled = True
                elif key_ == "packaging" and new_val:
                    pack_stock_sw = switches.get("pack_stock")
                    if pack_stock_sw is not None:
                        pack_stock_sw.disabled = False
                self.page.update()

            sw = ft.Switch(
                value=bool(enabled),
                disabled=disabled_by_cascade,
                on_change=_on_change,
            )
            switches[key] = sw
            return ft.Row(
                [ft.Text(feat.label_uk, expand=True), sw],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )

        FEATURE_COL_WIDTH = 46

        def _manager_override_row(manager, cid, overrides, feature_states):
            mid = manager.get("id")
            name = manager.get("full_name") or manager.get("email") or mid
            mid_overrides = overrides.get(mid, {})

            # Mirror database.get_effective_features locally: an override
            # REPLACES the company toggle for this manager (either direction —
            # can widen a feature the company turned off), then the
            # requires-chain is enforced on the resolved per-manager values so
            # e.g. pack_stock only shows effectively-on if packaging is too.
            manager_eff: dict[str, bool] = {}
            for k in FEATURE_ORDER:
                base = (
                    mid_overrides[k]
                    if k in mid_overrides
                    else bool(feature_states.get(k, True))
                )
                req_ok = all(
                    manager_eff.get(r, True) for r in TOGGLEABLE_FEATURES[k].requires
                )
                manager_eff[k] = bool(base and req_ok)

            cells = [ft.Text(name, expand=True, size=13)]
            for key in FEATURE_ORDER:
                company_on = bool(feature_states.get(key, True))
                is_override = key in mid_overrides
                cur = manager_eff.get(key, True)

                def _on_change(
                    e, mid_=mid, cid_=cid, key_=key, company_default=company_on
                ):
                    new_val = bool(e.control.value)
                    # Store an explicit override only when it differs from the
                    # company default — otherwise clear it (inherit), keeping
                    # the DB tidy and correct if the company setting changes.
                    store_val = None if new_val == company_default else new_val
                    ok = self.db.set_manager_feature_override(
                        mid_, cid_, key_, store_val
                    )
                    if not ok:
                        e.control.value = not new_val
                        self._show_snack(
                            "Спочатку увімкніть залежну вкладку для цього користувача",
                            "orange",
                        )
                        self.page.update()

                tooltip = TOGGLEABLE_FEATURES[key].label_uk
                if is_override:
                    tooltip += (
                        " (увімкнено персонально)"
                        if cur
                        else " (вимкнено персонально)"
                    )

                cells.append(
                    ft.Container(
                        width=FEATURE_COL_WIDTH,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Checkbox(
                            value=bool(cur),
                            tooltip=tooltip,
                            on_change=_on_change,
                        ),
                    )
                )
            return ft.Row(cells, spacing=6)

        def _build_users_block(cid, feature_states):
            managers = [
                m
                for m in (self.db.get_company_managers(cid) or [])
                if isinstance(m, dict) and not is_role_admin_like(m.get("role") or "")
            ]
            overrides = self.db.get_company_managers_feature_overrides(cid)
            if not managers:
                return ft.Text(
                    "У компанії немає користувачів", size=12, color="grey600"
                )
            header = ft.Row(
                [ft.Text("", expand=True)]
                + [
                    ft.Container(
                        width=FEATURE_COL_WIDTH,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Text(
                            f.label_uk,
                            size=10,
                            text_align=ft.TextAlign.CENTER,
                        ),
                    )
                    for f in (TOGGLEABLE_FEATURES[k] for k in FEATURE_ORDER)
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.END,
            )
            return ft.Column(
                [header]
                + [
                    _manager_override_row(m, cid, overrides, feature_states)
                    for m in managers
                ],
                spacing=6,
            )

        def _build_company_tile(co):
            cid = co.get("id")
            name = co.get("name") or ""
            feature_states = {
                key: toggles_by_company.get(cid, {}).get(key, True)
                for key in FEATURE_ORDER
            }
            switches: dict[str, ft.Switch] = {}
            feature_rows = [
                _feature_row(
                    cid,
                    key,
                    feature_states[key],
                    disabled_by_cascade=(
                        key == "pack_stock" and not feature_states["packaging"]
                    ),
                    switches=switches,
                )
                for key in FEATURE_ORDER
            ]

            is_expanded = cid in self._sa_expanded_companies
            users_container = ft.Container(
                content=(
                    _build_users_block(cid, feature_states)
                    if is_expanded
                    else ft.Text("", size=1)
                )
            )

            def _on_expand(e, cid_=cid, states=feature_states, cont=users_container):
                if bool(getattr(e.control, "expanded", False)):
                    self._sa_expanded_companies.add(cid_)
                    cont.content = _build_users_block(cid_, states)
                    self.page.update()
                else:
                    self._sa_expanded_companies.discard(cid_)

            is_blocked = bool(co.get("is_blocked"))
            return ft.ExpansionTile(
                expanded=is_expanded,
                title=ft.Row(
                    [
                        ft.Text(name, expand=True, weight="bold", size=15),
                        ft.Container(
                            padding=ft.padding.symmetric(horizontal=8, vertical=2),
                            border_radius=6,
                            bgcolor="#FEE2E2",
                            content=ft.Text("Заблоковано", size=11, color="#991B1B"),
                            visible=is_blocked,
                        ),
                        ft.IconButton(
                            ft.Icons.EDIT,
                            tooltip="Перейменувати",
                            on_click=lambda _, c=co: _open_rename_dialog(c),
                        ),
                        ft.IconButton(
                            ft.Icons.LOCK_OPEN if is_blocked else ft.Icons.LOCK_OUTLINE,
                            tooltip="Розблокувати" if is_blocked else "Блокувати",
                            icon_color="#059669" if is_blocked else "#D97706",
                            on_click=lambda _, c=co: _toggle_block(c),
                        ),
                        ft.IconButton(
                            ft.Icons.DELETE_OUTLINE,
                            tooltip="Видалити",
                            icon_color="#DC2626",
                            on_click=lambda _, c=co: _open_delete_dialog(c),
                        ),
                    ]
                ),
                on_change=_on_expand,
                controls=[
                    ft.Container(
                        padding=ft.padding.only(left=12, right=12, bottom=8),
                        content=ft.Column(
                            [
                                ft.Text("Вкладки компанії", weight="bold", size=13),
                                ft.Column(feature_rows, spacing=4),
                                ft.Divider(),
                                ft.Text("Користувачі", weight="bold", size=13),
                                users_container,
                            ],
                            spacing=8,
                        ),
                    )
                ],
            )

        tiles = [
            _build_company_tile(co)
            for co in companies
            if isinstance(co, dict) and co.get("id")
        ]

        self.content_area.controls.extend(
            [
                ft.Text("Компанії", size=22, weight="bold"),
                ft.Row(
                    [
                        ft.FilledButton(
                            "Нова компанія",
                            icon=ft.Icons.ADD,
                            on_click=lambda _: _open_create_dialog(),
                        ),
                        ft.OutlinedButton(
                            "Об'єднати користувачів (Компанія-Локал)",
                            icon=ft.Icons.MERGE_TYPE,
                            on_click=lambda _: self._open_merge_managers_dialog(),
                        ),
                        ft.OutlinedButton(
                            "Злиття категорій (Компанія-VPS)",
                            icon=ft.Icons.ACCOUNT_TREE,
                            on_click=lambda _: self._open_category_merge_dialog(),
                        ),
                    ],
                    spacing=10,
                ),
                ft.Column(tiles, spacing=6),
            ]
        )
        self.page.update()

    def _open_merge_managers_dialog(self):
        """Фаза F, Case 1 — СуперАдмін-only: regroup several Локал managers
        under one company_id for login/billing (their own local data never
        moves — see database.py: merge_managers_into_company)."""
        companies = self._visible_companies(force_refresh=True)
        local_companies = {
            c.get("id"): c
            for c in companies
            if isinstance(c, dict) and c.get("id") and c.get("storage_mode") == "local"
        }
        if not local_companies:
            self._show_snack("Немає компаній/індивідуалів у режимі Локал", "orange")
            return

        all_managers = self.db.get_company_managers("")
        candidates = [
            m
            for m in all_managers
            if isinstance(m, dict) and m.get("company_id") in local_companies
        ]

        checks: dict[str, ft.Checkbox] = {}
        rows = []
        for m in sorted(candidates, key=lambda x: (x.get("full_name") or "")):
            cid = m.get("company_id")
            cname = (local_companies.get(cid) or {}).get("name", "")
            cb = ft.Checkbox(
                label=f"{m.get('full_name', '')} ({m.get('email', '')}) — {cname}",
                value=False,
            )
            checks[m.get("id")] = cb
            rows.append(cb)

        target_dd = ft.Dropdown(
            label="Цільова компанія (лишиться після злиття)",
            border_radius=10,
            options=[
                ft.dropdown.Option(key=cid, text=c.get("name", cid))
                for cid, c in local_companies.items()
            ],
        )
        new_name_f = ft.TextField(
            label="Нова назва компанії (необов'язково)", border_radius=10
        )

        def _do_merge(_):
            selected = [mid for mid, cb in checks.items() if cb.value]
            if not selected:
                self._show_snack("Оберіть хоча б одного користувача", "orange")
                return
            if not target_dd.value:
                self._show_snack("Оберіть цільову компанію", "orange")
                return
            ok, err = self.db.merge_managers_into_company(
                selected, target_dd.value, new_name_f.value or None
            )
            if ok:
                self.page.pop_dialog()
                self._show_snack("Користувачів об'єднано", "green")
                self.show_company_settings(None)
            else:
                self._show_snack(err or "Помилка злиття", "red")

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Об'єднати користувачів (Компанія-Локал)"),
            content=ft.Container(
                width=460,
                content=ft.Column(
                    [
                        ft.Text(
                            "Кожен лишається на своєму локальному пристрої зі своїми "
                            "даними — змінюється лише компанія для входу.",
                            size=12,
                            color="grey700",
                        ),
                        ft.Column(rows, spacing=4, height=220, scroll=ft.ScrollMode.ADAPTIVE),
                        ft.Divider(),
                        target_dd,
                        new_name_f,
                    ],
                    tight=True,
                    spacing=10,
                ),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: self.page.pop_dialog()),
                ft.FilledButton("Об'єднати", on_click=_do_merge),
            ],
        )
        self.page.show_dialog(dlg)

    def _open_category_merge_dialog(self):
        """Фаза F, Case 2 — СуперАдмін-only: злиття кількох джерел (окремих
        локал-акаунтів або компаній) в одну Компанія-VPS. Тільки категорії/
        рівні можуть дублювати одне одного і потребують ручного рев'ю —
        решта таблиць переноситься як є, кожен лишається при своєму (див.
        database.py: execute_category_merge)."""
        all_managers = self.db.get_company_managers("")
        all_companies = {
            c.get("id"): c
            for c in self.db.get_all_companies()
            if isinstance(c, dict) and c.get("id")
        }
        vps_companies = {
            cid: c for cid, c in all_companies.items() if c.get("storage_mode") == "vps"
        }

        source_checks: dict[str, ft.Checkbox] = {}
        for m in sorted(all_managers, key=lambda x: (x.get("full_name") or "")):
            cname = (all_companies.get(m.get("company_id")) or {}).get("name", "")
            source_checks[m.get("id")] = ft.Checkbox(
                label=f"{m.get('full_name', '')} ({m.get('email', '')}) — {cname}",
                value=False,
            )

        target_dd = ft.Dropdown(
            label="Цільова компанія (VPS)",
            border_radius=10,
            options=[
                ft.dropdown.Option(key=cid, text=c.get("name", cid))
                for cid, c in vps_companies.items()
            ]
            + [ft.dropdown.Option(key="__new__", text="— Нова компанія —")],
        )
        new_target_name_f = ft.TextField(
            label="Назва нової компанії", border_radius=10, visible=False
        )
        target_dd.on_select = lambda _: (
            setattr(new_target_name_f, "visible", target_dd.value == "__new__"),
            self.page.update(),
        )

        body = ft.Column(tight=True, spacing=10, scroll=ft.ScrollMode.ADAPTIVE, height=460)
        dlg = ft.AlertDialog(modal=True, title=ft.Text("Злиття категорій (Компанія-VPS)"))

        # ---- Крок 2 state (заповнюється після "Далі") ----
        plan_state: dict = {"plan": None, "sources": []}
        node_checks: dict[tuple, ft.Checkbox] = {}
        groups: list[dict] = []
        groups_view = ft.Column(spacing=4)

        def _refresh_groups_view():
            groups_view.controls = [
                ft.Text(
                    f"• {g['final_name']}: " + ", ".join(
                        n["name"] for n in plan_state["plan"]["nodes"]
                        if tuple(n["key"]) in {tuple(k) for k in g["member_keys"]}
                    ),
                    size=12,
                )
                for g in groups
            ] or [ft.Text("Груп ще не створено — усе, що не згруповано, копіюється як є.", size=12, color="grey600")]

        def _add_group(_):
            selected = [k for k, cb in node_checks.items() if cb.value]
            if len(selected) < 2:
                self._show_snack("Оберіть щонайменше 2 вузли з різних джерел", "orange")
                return
            name = group_name_f.value.strip() if group_name_f.value else ""
            if not name:
                self._show_snack("Вкажіть назву після об'єднання", "orange")
                return
            groups.append({"member_keys": [list(k) for k in selected], "final_name": name})
            for k in selected:
                node_checks[k].value = False
            group_name_f.value = ""
            _refresh_groups_view()
            self.page.update()

        group_name_f = ft.TextField(label="Назва після об'єднання", border_radius=10, width=260)

        def _norm_node_name(raw_name):
            return " ".join(str(raw_name or "").strip().split()).lower()

        KIND_LABELS = {"category": "", "level": "", "supplier": "Пост.: "}

        def _render_step2():
            plan = plan_state["plan"]
            node_checks.clear()
            rows = []

            # Same-name-in-a-different-source is only a hint for the admin -
            # never auto-merged. Scoped per `kind` so a category and a
            # supplier that happen to share a name don't cross-highlight.
            name_sources: dict[tuple, set] = {}
            for n in plan["nodes"]:
                nk = (n["kind"], _norm_node_name(n["name"]))
                if nk[1]:
                    name_sources.setdefault(nk, set()).add(n["key"][0])

            for src in plan["sources"]:
                rows.append(
                    ft.Text(src.get("full_name") or src.get("manager_id"), weight="bold", size=13)
                )
                src_nodes = [n for n in plan["nodes"] if n["key"][0] == src["manager_id"]]
                by_key = {tuple(n["key"]): n for n in src_nodes}

                def _depth(n):
                    if n["kind"] in ("category", "supplier"):
                        return 0
                    d = 0
                    pk = n.get("parent_key")
                    while pk:
                        d += 1
                        pk = by_key.get(tuple(pk), {}).get("parent_key")
                    return d + 1

                for n in src_nodes:
                    key = tuple(n["key"])
                    label = KIND_LABELS.get(n["kind"], "") + str(n["name"] or "")
                    cb = ft.Checkbox(label=label, value=False)
                    node_checks[key] = cb
                    nk = (n["kind"], _norm_node_name(n["name"]))
                    likely_match = len(name_sources.get(nk, set())) > 1
                    container = ft.Container(
                        content=cb, padding=ft.padding.only(left=16 * _depth(n))
                    )
                    if likely_match:
                        container.bgcolor = "#FEF3C7"
                    rows.append(container)
            body.controls = [
                ft.Text(
                    "Позначте однакові категорії/рівні/постачальники з РІЗНИХ джерел і "
                    "натисніть «Додати групу» з фінальною (стандартизованою) назвою — "
                    "навіть якщо назви геть різні, але це те саме, можна об'єднати. "
                    "Жовтим підсвічені лише збіги назв в різних джерелах — це підказка, "
                    "об'єднання завжди вирішуєте ви. Усе, що не позначено, копіюється без "
                    "змін — кожен лишається при своїх товарах.",
                    size=12,
                    color="grey700",
                ),
                ft.Column(rows, spacing=2),
                ft.Row([group_name_f, ft.FilledButton("Додати групу", on_click=_add_group)], spacing=10),
                ft.Divider(),
                ft.Text("Групи злиття:", weight="bold", size=13),
                groups_view,
            ]
            _refresh_groups_view()
            dlg.actions = [
                ft.TextButton("Назад", on_click=lambda _: _render_step1()),
                ft.FilledButton("Виконати злиття", on_click=_do_execute),
            ]
            self.page.update()

        def _do_execute(_):
            target_cid = target_dd.value
            if target_cid == "__new__":
                name = (new_target_name_f.value or "").strip()
                if not name:
                    self._show_snack("Вкажіть назву нової компанії", "orange")
                    return
                target_cid = self.db.create_company(name, account_kind="company", storage_mode="vps")
            src_ids = [mid for mid, cb in source_checks.items() if cb.value]
            ok, err = self.db.execute_category_merge(src_ids, groups, target_cid)
            if ok:
                self.page.pop_dialog()
                self._show_snack("Злиття виконано", "green")
                self.show_company_settings(None)
            else:
                self._show_snack(err or "Помилка злиття", "red")

        def _go_step2(_):
            selected = [mid for mid, cb in source_checks.items() if cb.value]
            if len(selected) < 2:
                self._show_snack("Оберіть щонайменше 2 джерела для злиття", "orange")
                return
            if not target_dd.value:
                self._show_snack("Оберіть цільову компанію", "orange")
                return
            plan_state["plan"] = self.db.build_category_merge_plan(selected)
            groups.clear()
            _render_step2()

        def _render_step1():
            body.controls = [
                ft.Text(
                    "Оберіть щонайменше 2 джерела (окремих користувачів або "
                    "компаній) для злиття в одну VPS-компанію.",
                    size=12,
                    color="grey700",
                ),
                ft.Column(list(source_checks.values()), spacing=4),
                ft.Divider(),
                target_dd,
                new_target_name_f,
            ]
            dlg.actions = [
                ft.TextButton("Скасувати", on_click=lambda _: self.page.pop_dialog()),
                ft.FilledButton("Далі", on_click=_go_step2),
            ]
            self.page.update()

        dlg.content = ft.Container(width=520, content=body)
        _render_step1()
        self.page.show_dialog(dlg)

    def show_app_updates(self, e):
        self.current_tab = "app_updates"
        self.content_area.controls.clear()

        update_policy = self.db.get_app_update_policy()
        min_version_f = ft.TextField(
            label="Мінімальна версія застосунку",
            hint_text="Наприклад: 1.0.5",
            border_radius=10,
            value=update_policy.get("min_required", ""),
        )
        update_url_f = ft.TextField(
            label="Посилання на оновлення Windows (.exe)",
            hint_text="https://github.com/<owner>/<repo>/releases/latest/download/CatManPro-Setup.exe",
            border_radius=10,
            value=update_policy.get("download_url", "")
            or (Config.UPDATE_DOWNLOAD_URL or ""),
        )
        update_url_android_f = ft.TextField(
            label="Посилання на оновлення Android (.apk)",
            hint_text="https://github.com/<owner>/<repo>/releases/latest/download/CatManPro-android.apk",
            border_radius=10,
            value=update_policy.get("download_url_android", ""),
        )

        def save_updates(_):
            ok = self.db.set_app_update_policy(
                {
                    "min_required": (min_version_f.value or "").strip(),
                    "download_url": (update_url_f.value or "").strip(),
                    "download_url_android": (update_url_android_f.value or "").strip(),
                }
            )
            if ok:
                self._show_saved_dialog("Політику оновлення застосунку збережено.")
            else:
                self._show_snack("Не вдалося зберегти політику оновлення", "red")

        self.content_area.controls.extend(
            [
                ft.Text("Оновлення застосунку", size=22, weight="bold"),
                ft.Card(
                    ft.Container(
                        padding=15,
                        content=ft.Column(
                            [
                                ft.Text(
                                    "Глобальні налаштування для всіх компаній."
                                    " Користувачі з версією нижче мінімальної не зможуть увійти.",
                                    size=12,
                                    color="grey700",
                                ),
                                min_version_f,
                                update_url_f,
                                update_url_android_f,
                                ft.FilledButton(
                                    "Зберегти політику оновлення",
                                    icon=ft.Icons.SAVE,
                                    on_click=save_updates,
                                ),
                            ],
                            spacing=10,
                        ),
                    )
                ),
            ]
        )
        self.page.update()

    def show_copy_setup(self, e):
        self.current_tab = "copy_setup"
        self.content_area.controls.clear()

        if self.selected_company_id in {None, ALL_COMPANIES_KEY}:
            self.content_area.controls.extend(
                [
                    ft.Text("📋 Копіювання", size=24, weight="bold"),
                    ft.Text(
                        "Оберіть конкретну компанію зверху. Копіювання між компаніями заборонено.",
                        color="grey700",
                    ),
                ]
            )
            self.page.update()
            return

        managers = [
            m
            for m in self.db.get_company_managers(self.selected_company_id)
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

        def _open_module(module_key):
            # Категорії/Постачальники/Пакування навмисно не відкриваються з
            # супер-адміна - це внутрішня структура компанії, керує нею лише
            # Адмін. Супер-адмін бачить тут тільки компанії/користувачів.
            if module_key == "users":
                self.show_users(None)
                return

        context_user = {
            "id": self.user_data.get("id"),
            "full_name": self.user_data.get("full_name"),
            "role": "СуперАдмін",
            "company_id": self.selected_company_id,
            "active_manager_id": self.user_data.get("id"),
            "bypass_category_access": True,
            "superadmin_explicit_mode": True,
        }
        CopySetupView(
            self.content_area, context_user, self.db, on_open_module=_open_module
        ).build()
        self.page.update()

    def show_extensions(self, e):
        self.current_tab = "extensions"
        active_company_id = self.selected_company_id
        if active_company_id in {None, ALL_COMPANIES_KEY}:
            active_company_id = self.locked_company_id or self.user_data.get(
                "company_id"
            )
        existing = getattr(self, "_ext_view_inst", None)
        # Sync-poll path (e is None): if already mounted for the same company, do nothing.
        # Rebuilding would collapse ExpansionTiles and reset navigation state.
        if (
            e is None
            and existing is not None
            and getattr(existing, "active_company_id", None) == active_company_id
        ):
            return
        self.content_area.controls.clear()
        if (
            existing is None
            or getattr(existing, "active_company_id", None) != active_company_id
        ):
            self._ext_view_inst = ExtensionsView(
                self.content_area,
                self.user_data,
                self.db,
                active_company_id=active_company_id,
                is_superadmin=True,
            )
        else:
            # User explicitly clicked the tab — navigate back to the extensions list.
            existing.current_extension_key = None
        self._ext_view_inst.build()
        self.page.update()

    def show_web_site(self, e):
        self.current_tab = "web_site"
        existing = getattr(self, "_web_site_view_inst", None)
        if e is None and existing is not None:
            return
        self.content_area.controls.clear()
        self._web_site_view_inst = WebSiteView(
            self.content_area, self.user_data, self.db
        )
        self._web_site_view_inst.build()
        self.page.update()

    def show_languages(self, e):
        self.current_tab = "languages"
        existing = getattr(self, "_languages_view_inst", None)
        if e is None and existing is not None:
            return
        self.content_area.controls.clear()
        self._languages_view_inst = LanguagesView(
            self.content_area, self.user_data, self.db
        )
        self._languages_view_inst.build()
        self.page.update()

    def show_languages_filtered_empty(self, e):
        """Opens the 'Мови' tab pre-filtered to rows with at least one empty
        translation — entry point for the overview 'Порожні переклади' counter."""
        self.current_tab = "languages"
        self.content_area.controls.clear()
        self._languages_view_inst = LanguagesView(
            self.content_area, self.user_data, self.db
        )
        self._languages_view_inst.build(only_empty=True)
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
        _sa_mid = str(self.user_data.get("id") or "").strip()
        from views.themes import build_admin_appearance_section as _build_appearance

        appearance_card = ft.Card(
            ft.Container(
                padding=15,
                content=_build_appearance(
                    self.db,
                    _sa_mid,
                    on_theme_changed=self._apply_sa_theme_live,
                    on_nav_changed=self._apply_sa_nav_live,
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
            companies = self.db.get_all_companies()
            # Live, always-accurate location — parsed straight from the DSN
            # actually in use, never a separately stored copy that can drift
            # out of sync with reality (see .env / apply_infrastructure_switch).
            _general_loc = self.db.get_general_db_location()
            _companies_db_info = {
                str(c.get("id") or ""): c for c in self.db.get_all_companies_db_info()
            }
            # Auth DB — current live connection (source of truth for display)
            _auth_live_dsn = str(self.db._pg_dsn or "").strip()

            def _make_auth_db_section(label: str, initial: dict):
                """Загальна БД (і GSM-парсер разом з нею) — єдина ціль тут:
                DATABASE_URL/.env. Поточна активна БД завжди = живий
                self.db._pg_dsn, а не запам'ятоване значення з попереднього
                збереження цієї форми."""
                _init_mode = str(initial.get("mode") or "local")
                _init_local = str(initial.get("local_dsn") or "")
                _init_vps = str(initial.get("vps_dsn") or "")
                _cur_dsn = _auth_live_dsn

                # State: tracks old DSN after a migration, for the delete button
                _state = {"old_dsn": ""}

                cur_label = ft.Text(
                    f"Поточна БД: {_cur_dsn or '—'}",
                    size=11,
                    color="grey600",
                    selectable=True,
                )

                sec_mode_f = ft.Dropdown(
                    label="Активна БД",
                    value=_init_mode,
                    border_radius=10,
                    width=200,
                    options=[
                        ft.dropdown.Option("local", "Локальна"),
                        ft.dropdown.Option("vps", "VPS"),
                    ],
                )
                sec_local_f = ft.TextField(
                    label="Локальна БД (DSN)",
                    value=_init_local,
                    border_radius=10,
                    multiline=True,
                    min_lines=2,
                    max_lines=4,
                )
                sec_vps_f = ft.TextField(
                    label="VPS БД (DSN)",
                    value=_init_vps,
                    border_radius=10,
                    multiline=True,
                    min_lines=2,
                    max_lines=4,
                )

                delete_old_btn = ft.OutlinedButton(
                    "Видалити дані зі старої БД",
                    icon=ft.Icons.DELETE_FOREVER,
                    style=ft.ButtonStyle(color="red"),
                    visible=False,
                )

                def _save(_):
                    new_mode = str(sec_mode_f.value or "local").lower()
                    new_local = str(sec_local_f.value or "").strip()
                    new_vps = str(sec_vps_f.value or "").strip()
                    full = self.db.get_infrastructure_settings()
                    full["mode"] = new_mode
                    full["local_db_dsn"] = new_local
                    full["vps_db_dsn"] = new_vps
                    ok = self.db.set_infrastructure_settings(full)
                    if not ok:
                        self._show_snack("Не вдалося зберегти", "red")
                        return
                    sw_ok, sw_msg = self.db.apply_infrastructure_switch()
                    new_active = new_vps if new_mode == "vps" else new_local
                    cur_label.value = f"Поточна БД: {new_active or '—'}"
                    with contextlib.suppress(Exception):
                        cur_label.update()
                    self._show_snack(sw_msg, "green" if sw_ok else "orange")

                def _migrate_and_switch(_):
                    src = str(self.db._pg_dsn or "").strip()

                    # Target = the new DSN user wants to switch to
                    tgt_mode = str(sec_mode_f.value or "local").lower()
                    tgt = (
                        str(sec_vps_f.value or "").strip()
                        if tgt_mode == "vps"
                        else str(sec_local_f.value or "").strip()
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

                    _state["old_dsn"] = src

                    def _on_migrate_success():
                        # Auto-switch to new DB
                        full2 = self.db.get_infrastructure_settings()
                        full2["mode"] = tgt_mode
                        if tgt_mode == "vps":
                            full2["vps_db_dsn"] = tgt
                        else:
                            full2["local_db_dsn"] = tgt
                        self.db.set_infrastructure_settings(full2)
                        sw_ok, sw_msg = self.db.apply_infrastructure_switch()
                        self._show_snack(sw_msg, "green" if sw_ok else "orange")
                        cur_label.value = f"Поточна БД: {tgt}"
                        delete_old_btn.visible = True
                        with contextlib.suppress(Exception):
                            self.page.update()

                    self._run_migration_dialog(
                        source_dsn=src,
                        target_dsn=tgt,
                        on_success=_on_migrate_success,
                    )

                def _delete_old(_):
                    old = str(_state.get("old_dsn") or "").strip()
                    if not old:
                        self._show_snack(
                            "Адреса старої БД невідома. Перенесіть дані спочатку.",
                            "orange",
                        )
                        return

                    def _do_delete(__):
                        self.page.pop_dialog()
                        ok2, msg2 = self.db.drop_database_contents(old)
                        self._show_snack(msg2, "green" if ok2 else "red")
                        if ok2:
                            delete_old_btn.visible = False
                            _state["old_dsn"] = ""
                            with contextlib.suppress(Exception):
                                self.page.update()

                    confirm_dlg = ft.AlertDialog(
                        modal=True,
                        title=ft.Text("Видалити дані зі старої БД?"),
                        content=ft.Container(
                            width=420,
                            content=ft.Column(
                                [
                                    ft.Text("Буде видалено всі дані з:", size=13),
                                    ft.Text(
                                        old,
                                        size=11,
                                        color="grey700",
                                        selectable=True,
                                    ),
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
                                on_click=_do_delete,
                            ),
                        ],
                        actions_alignment=ft.MainAxisAlignment.END,
                    )
                    self.page.show_dialog(confirm_dlg)

                delete_old_btn.on_click = _delete_old

                _tile_key = ("auth_db", label)

                def _on_tile_change(e, _key=_tile_key):
                    self._infra_expanded_tiles[_key] = bool(
                        getattr(e.control, "expanded", False)
                    )

                return ft.ExpansionTile(
                    title=ft.Text(label, size=13, weight=ft.FontWeight.W_600),
                    subtitle=cur_label,
                    expanded=self._infra_expanded_tiles.get(_tile_key, False),
                    on_change=_on_tile_change,
                    controls=[
                        ft.Container(
                            padding=ft.padding.only(left=8, right=8, bottom=12),
                            content=ft.Column(
                                [
                                    sec_mode_f,
                                    sec_local_f,
                                    sec_vps_f,
                                    ft.Row(
                                        [
                                            ft.FilledButton(
                                                "Зберегти",
                                                icon=ft.Icons.SAVE,
                                                on_click=_save,
                                            ),
                                            ft.FilledButton(
                                                "Копіювати і переключити",
                                                icon=ft.Icons.SWAP_HORIZ,
                                                on_click=_migrate_and_switch,
                                            ),
                                        ],
                                        spacing=8,
                                        wrap=True,
                                    ),
                                    delete_old_btn,
                                ],
                                spacing=10,
                            ),
                        )
                    ],
                )

            # "Активна БД" завжди показує РЕАЛЬНИЙ поточний режим (з живого
            # DSN), а не запам'ятоване значення з попереднього збереження —
            # інакше після ручної зміни .env перемикач продовжує брехати.
            _live_auth_mode = "local" if _general_loc.get("is_local") else "vps"
            auth_tile = _make_auth_db_section(
                "Дані доступу (загальні; тут же зберігається GSM-парсер)",
                {
                    "mode": _live_auth_mode,
                    "local_dsn": (
                        _auth_live_dsn
                        if _live_auth_mode == "local"
                        else str(cfg.get("local_db_dsn") or "")
                    ),
                    "vps_dsn": (
                        _auth_live_dsn
                        if _live_auth_mode == "vps"
                        else str(cfg.get("vps_db_dsn") or "")
                    ),
                },
            )

            def _make_pending_company_db_tile(cid: str, cname: str):
                host_f = ft.TextField(label="Host", border_radius=10, width=190)
                port_f = ft.TextField(
                    label="Port", value="5432", border_radius=10, width=80
                )
                user_f = ft.TextField(
                    label="Користувач", value="postgres", border_radius=10, width=140
                )
                password_f = ft.TextField(
                    label="Пароль",
                    password=True,
                    can_reveal_password=True,
                    border_radius=10,
                    width=260,
                )
                status_text = ft.Text("", size=11, color="grey600")

                def _provision(_):
                    ok, msg = self.db.set_company_vps_address(
                        cid, host_f.value, port_f.value, user_f.value, password_f.value
                    )
                    self._show_snack(
                        "Адресу вказано, компанію розблоковано"
                        if ok
                        else (msg or "Не вдалося створити базу"),
                        "green" if ok else "red",
                    )
                    if ok:
                        self.show_company_settings(None)
                    else:
                        status_text.value = msg
                        status_text.color = "red700"
                        with contextlib.suppress(Exception):
                            status_text.update()

                _tile_key = ("pending_company_db", cid)

                def _on_tile_change(e, _key=_tile_key):
                    self._infra_expanded_tiles[_key] = bool(
                        getattr(e.control, "expanded", False)
                    )

                return ft.ExpansionTile(
                    title=ft.Text(cname, size=13, weight=ft.FontWeight.W_600),
                    subtitle=ft.Text(
                        "Очікує адресу VPS", size=11, color="orange700"
                    ),
                    expanded=self._infra_expanded_tiles.get(_tile_key, True),
                    on_change=_on_tile_change,
                    controls=[
                        ft.Container(
                            padding=ft.padding.only(left=8, right=8, bottom=12),
                            content=ft.Column(
                                [
                                    ft.Text(
                                        "Компанію створено в режимі VPS, але адресу "
                                        "сервера ще не вказано — збереження даних "
                                        "заблоковано, поки ви не вкажете, де створити "
                                        "базу.",
                                        size=11,
                                        color="grey700",
                                    ),
                                    ft.Row([host_f, port_f], spacing=8, wrap=True),
                                    ft.Row([user_f, password_f], spacing=8, wrap=True),
                                    ft.FilledButton(
                                        "Створити базу за цією адресою",
                                        icon=ft.Icons.STORAGE,
                                        on_click=_provision,
                                    ),
                                    status_text,
                                ],
                                spacing=10,
                            ),
                        )
                    ],
                )

            def _make_company_db_tile(cid: str, cname: str, host: str, port, db_name: str):
                cur_label = ft.Text(
                    f"Поточна БД: {host}:{port or 5432}/{db_name}"
                    if host and db_name
                    else "Поточна БД: не налаштована",
                    size=11,
                    color="grey600",
                    selectable=True,
                )
                host_f = ft.TextField(label="Host", value=host, border_radius=10, width=190)
                port_f = ft.TextField(
                    label="Port", value=str(port or 5432), border_radius=10, width=80
                )
                dbname_f = ft.TextField(
                    label="Назва БД", value=db_name, border_radius=10, width=220
                )
                user_f = ft.TextField(
                    label="Користувач", value="postgres", border_radius=10, width=140
                )
                password_f = ft.TextField(
                    label="Пароль (пусто = не змінювати)",
                    password=True,
                    can_reveal_password=True,
                    border_radius=10,
                    width=260,
                )
                sslmode_f = ft.TextField(
                    label="sslmode (опційно)", value="", border_radius=10, width=140
                )

                def _save(_):
                    ok, msg = self.db.set_company_db_connection(
                        cid,
                        host_f.value,
                        port_f.value,
                        dbname_f.value,
                        user_f.value,
                        password_f.value,
                        sslmode_f.value,
                    )
                    if ok:
                        cur_label.value = (
                            f"Поточна БД: {host_f.value}:{port_f.value}/{dbname_f.value}"
                        )
                        password_f.value = ""
                        with contextlib.suppress(Exception):
                            cur_label.update()
                            password_f.update()
                    self._show_snack(msg, "green" if ok else "red")

                move_status = ft.Text("", size=11, color="grey600")
                move_progress = ft.ProgressRing(
                    width=14, height=14, stroke_width=2, visible=False
                )
                move_btn = ft.OutlinedButton(
                    "Перенести на новий VPS", icon=ft.Icons.SWAP_HORIZ
                )

                def _do_move(_):
                    page = self.page
                    if not page:
                        return

                    async def _run_async():
                        move_btn.disabled = True
                        move_progress.visible = True
                        move_status.value = "Переносимо дані компанії на новий сервер..."
                        move_status.color = "grey600"
                        page.update()
                        try:
                            ok, msg = await asyncio.to_thread(
                                self.db.move_company_vps_database,
                                cid,
                                host_f.value,
                                port_f.value,
                                dbname_f.value,
                                user_f.value,
                                password_f.value,
                                sslmode_f.value,
                            )
                        except Exception as exc:
                            ok, msg = False, str(exc)
                        move_btn.disabled = False
                        move_progress.visible = False
                        move_status.value = msg
                        move_status.color = "green700" if ok else "red700"
                        if ok:
                            cur_label.value = (
                                f"Поточна БД: {host_f.value}:{port_f.value}/{dbname_f.value}"
                            )
                            password_f.value = ""
                        page.update()

                    page.run_task(_run_async)

                move_btn.on_click = _do_move

                _tile_key = ("company_db", cid)

                def _on_tile_change(e, _key=_tile_key):
                    self._infra_expanded_tiles[_key] = bool(
                        getattr(e.control, "expanded", False)
                    )

                return ft.ExpansionTile(
                    title=ft.Text(cname, size=13, weight=ft.FontWeight.W_600),
                    subtitle=cur_label,
                    expanded=self._infra_expanded_tiles.get(_tile_key, False),
                    on_change=_on_tile_change,
                    controls=[
                        ft.Container(
                            padding=ft.padding.only(left=8, right=8, bottom=12),
                            content=ft.Column(
                                [
                                    ft.Row([host_f, port_f], spacing=8, wrap=True),
                                    ft.Row([dbname_f, user_f], spacing=8, wrap=True),
                                    ft.Row([password_f, sslmode_f], spacing=8, wrap=True),
                                    ft.FilledButton(
                                        "Зберегти адресу",
                                        icon=ft.Icons.SAVE,
                                        on_click=_save,
                                        tooltip="Просто переключити підключення на цю адресу "
                                        "(дані НЕ копіюються — база за новою адресою має вже існувати)",
                                    ),
                                    ft.Divider(height=6, color="transparent"),
                                    ft.Text(
                                        "Перенести на новий VPS: копіює всі дані компанії за "
                                        "адресою вище на новий сервер і перемикається на нього. "
                                        "Стара база лишається недоторканою як резервна копія.",
                                        size=11,
                                        color="grey600",
                                    ),
                                    ft.Row(
                                        [move_btn, move_progress],
                                        spacing=8,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    ),
                                    move_status,
                                ],
                                spacing=10,
                            ),
                        )
                    ],
                )

            company_tiles = []
            for _co in companies:
                _cid = str(_co.get("id") or "")
                _cname = str(_co.get("name") or _cid)
                _smode = str(_co.get("storage_mode") or "vps")
                if _smode == "local":
                    _registry = self.db.get_company_local_db_registry(_cid)
                    _device_rows = [
                        ft.Text(
                            f"{r.get('full_name') or r.get('email') or r.get('manager_id')}: "
                            f"{r.get('db_host')}:{r.get('db_port')}/{r.get('db_name')}",
                            size=11,
                            color="grey700",
                            selectable=True,
                        )
                        for r in _registry
                    ]
                    _local_tile_key = ("local_storage_company", _cid)

                    def _on_local_tile_change(e, _key=_local_tile_key):
                        self._infra_expanded_tiles[_key] = bool(
                            getattr(e.control, "expanded", False)
                        )

                    company_tiles.append(
                        ft.ExpansionTile(
                            title=ft.Text(_cname, size=13, weight=ft.FontWeight.W_600),
                            subtitle=ft.Text(
                                f"Локально • {len(_registry)} пристроїв"
                                if _registry
                                else "Локально • ще немає зареєстрованих пристроїв",
                                size=11,
                                color="grey600",
                            ),
                            expanded=self._infra_expanded_tiles.get(
                                _local_tile_key, False
                            ),
                            on_change=_on_local_tile_change,
                            controls=[
                                ft.Container(
                                    padding=ft.padding.only(left=8, right=8, bottom=12),
                                    content=ft.Column(
                                        [
                                            ft.Text(
                                                "Локальний режим: у кожного співробітника "
                                                "власна база на власному ПК. Єдиної адреси "
                                                "компанії немає — центральне редагування чи "
                                                "перенесення тут неможливе.",
                                                size=11,
                                                color="grey600",
                                            ),
                                            *(
                                                _device_rows
                                                or [ft.Text("—", size=11, color="grey600")]
                                            ),
                                        ],
                                        spacing=6,
                                    ),
                                )
                            ],
                        )
                    )
                else:
                    _cinfo = _companies_db_info.get(_cid) or {}
                    if bool(_cinfo.get("db_pending")):
                        company_tiles.append(
                            _make_pending_company_db_tile(_cid, _cname)
                        )
                    else:
                        company_tiles.append(
                            _make_company_db_tile(
                                _cid,
                                _cname,
                                str(_cinfo.get("db_host") or ""),
                                _cinfo.get("db_port") or 5432,
                                str(_cinfo.get("db_name") or ""),
                            )
                        )

            def _show_infra_help():
                from views.help_utils import show_modal_help, vps_migration_help_items

                show_modal_help(
                    self.page,
                    "Перенесення бази даних на VPS / власний сервер",
                    vps_migration_help_items(),
                )

            self.content_area.controls.append(
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
                                    "Формат DSN: postgresql://USER:PASSWORD@HOST:5432/DBNAME. "
                                    "Зміна БД авторизації застосовується одразу після збереження.",
                                    size=12,
                                    color="grey700",
                                ),
                                ft.Container(
                                    padding=8,
                                    border_radius=8,
                                    bgcolor="green50" if not _general_loc.get("is_local") else "amber50",
                                    content=ft.Text(
                                        f"Зараз підключено: {_general_loc.get('host')}:"
                                        f"{_general_loc.get('port')}/{_general_loc.get('db_name')} — "
                                        + ("VPS" if not _general_loc.get("is_local") else "Локально"),
                                        size=12,
                                        weight=ft.FontWeight.W_600,
                                        selectable=True,
                                    ),
                                ),
                                ft.Divider(height=4, color="transparent"),
                                ft.Text(
                                    "Дані доступу",
                                    size=12,
                                    color="grey700",
                                    weight=ft.FontWeight.W_600,
                                ),
                                auth_tile,
                                ft.Divider(height=6),
                                ft.Text(
                                    "Дані компаній (VPS — редагована адреса й перенесення; "
                                    "Локал — власна база кожного співробітника)",
                                    size=12,
                                    color="grey700",
                                    weight=ft.FontWeight.W_600,
                                ),
                                *company_tiles,
                            ],
                            spacing=8,
                        ),
                    )
                )
            )

            # ── Системний бекап (Фаза H): Тестовий акаунт + фільтровані
            # дампи загальної БД (companies/managers vs gsm_parser_*) ──────
            test_company = next(
                (
                    c
                    for c in companies
                    if str(c.get("name") or "").strip().lower() == "test"
                ),
                None,
            )

            def _pick_backup_dest_dir():
                if not TK_AVAILABLE:
                    return ""
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                path = filedialog.askdirectory(title="Оберіть теку для збереження бекапу")
                root.destroy()
                return path or ""

            def _pick_backup_file():
                if not TK_AVAILABLE:
                    return ""
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                path = filedialog.askopenfilename(
                    title="Оберіть файл бекапу",
                    filetypes=[("Backup files", "*.dump")],
                )
                root.destroy()
                return path or ""

            def _make_system_backup_row(title: str, export_fn, import_fn, confirm_msg: str):
                status_text = ft.Text("", size=11, color="grey700")
                progress = ft.ProgressRing(width=14, height=14, stroke_width=2, visible=False)

                def _set_busy(busy: bool):
                    export_btn.disabled = busy
                    import_btn.disabled = busy
                    progress.visible = busy

                def _do_export(_):
                    dest_dir = _pick_backup_dest_dir()
                    if not dest_dir:
                        return

                    async def _run_async():
                        _set_busy(True)
                        status_text.value = "Зберігаємо бекап..."
                        status_text.color = "grey700"
                        self.page.update()
                        try:
                            ok, result = await asyncio.to_thread(export_fn, dest_dir)
                        except Exception as exc:
                            ok, result = False, str(exc)
                        _set_busy(False)
                        status_text.value = (
                            f"Збережено: {result}" if ok else f"Помилка: {result}"
                        )
                        status_text.color = "green" if ok else "red"
                        self.page.update()

                    self.page.run_task(_run_async)

                def _do_import(_):
                    input_file = _pick_backup_file()
                    if not input_file:
                        return

                    def _run_restore():
                        async def _run_async():
                            _set_busy(True)
                            status_text.value = "Відновлюємо з бекапу — не закривайте програму..."
                            status_text.color = "grey700"
                            self.page.update()
                            try:
                                ok, err = await asyncio.to_thread(import_fn, input_file)
                            except Exception as exc:
                                ok, err = False, str(exc)
                            _set_busy(False)
                            status_text.value = "Відновлено успішно" if ok else f"Помилка: {err}"
                            status_text.color = "green" if ok else "red"
                            self.page.update()

                        self.page.run_task(_run_async)

                    def _confirmed(_e=None):
                        self.page.pop_dialog()
                        _run_restore()

                    confirm_dlg = ft.AlertDialog(
                        modal=True,
                        title=ft.Text("Відновити з бекапу?"),
                        content=ft.Container(width=380, content=ft.Text(confirm_msg)),
                        actions=[
                            ft.TextButton(
                                "Скасувати", on_click=lambda _e: self.page.pop_dialog()
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
                    self.page.show_dialog(confirm_dlg)

                export_btn = ft.FilledButton(
                    "Зберегти бекап", icon=ft.Icons.SAVE_ALT, on_click=_do_export
                )
                import_btn = ft.OutlinedButton(
                    "Відновити з бекапу", icon=ft.Icons.RESTORE, on_click=_do_import
                )
                return ft.Column(
                    [
                        ft.Text(title, size=12, weight=ft.FontWeight.W_600),
                        ft.Row(
                            [export_btn, import_btn, progress],
                            spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        status_text,
                    ],
                    spacing=4,
                )

            system_backup_rows = []
            if test_company:
                _test_cid = str(test_company.get("id") or "")
                system_backup_rows.append(
                    _make_system_backup_row(
                        "Тестовий акаунт",
                        lambda dest_dir: self.db.export_company_backup(_test_cid, dest_dir),
                        lambda input_file: self.db.import_company_backup(_test_cid, input_file),
                        "Це перезапише всі поточні дані тестового акаунту даними з цього файлу.",
                    )
                )
            system_backup_rows.append(
                _make_system_backup_row(
                    "Загальна БД (компанії, користувачі)",
                    self.db.export_general_backup,
                    self.db.import_general_backup,
                    "Це перезапише таблиці companies/managers та інші глобальні таблиці "
                    "(без ГСМ-парсеру) даними з цього файлу.",
                )
            )
            system_backup_rows.append(
                _make_system_backup_row(
                    "ГСМ-парсер (фото пристроїв, довідники)",
                    self.db.export_gsm_backup,
                    self.db.import_gsm_backup,
                    "Це перезапише всі таблиці ГСМ-парсеру даними з цього файлу.",
                )
            )

            self.content_area.controls.append(
                ft.Card(
                    ft.Container(
                        padding=15,
                        content=ft.Column(
                            [
                                ft.Text(
                                    "Резервне копіювання (система)",
                                    size=15,
                                    weight=ft.FontWeight.W_700,
                                ),
                                ft.Text(
                                    "Ручний бекап окремо від per-company бекапу в налаштуваннях "
                                    "користувача — для тестового акаунту та спільної системної бази.",
                                    size=12,
                                    color="grey700",
                                ),
                                ft.Divider(height=4, color="transparent"),
                                *system_backup_rows,
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
            ok, summary = self.db.migrate_local_to_vps(
                target_dsn=target_dsn,
                source_dsn=source_dsn,
                progress_callback=_progress,
            )
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
