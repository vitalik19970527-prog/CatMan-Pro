import asyncio
import contextlib
import json
import os
import re
import time as _time
from datetime import datetime

try:
    import tkinter as tk
    from tkinter import filedialog

    TK_AVAILABLE = True
except Exception:
    TK_AVAILABLE = False

import flet as ft

import i18n

# ---------------------------------------------------------------------------
# Theme colour shims
# ---------------------------------------------------------------------------
_OK_DK   = '#0F7B53'
_ALT     = '#FBFCFD'
_OK_BG   = '#D9F2E6'
_ACCENT  = '#1D4ED8'
_ALT2    = '#F8FBFA'
_TEXT    = '#0F172A'
_SOFT    = '#EEF7F1'


def _th_from_view(view) -> dict:
    from views.themes import get_active_theme
    return get_active_theme(str(getattr(view, 'manager_id', '') or ''))


from gsm_parser_runner import FIELD_CATALOG, LAST_PARSE_LOG_PATH
from logger import logger
from views.gsm_parser_view import GsmParserView
from views.mobile_layout_helpers import is_mobile, make_scrollable_chip_row

# ---------------------------------------------------------------------------
# GSM Parser Telegram report — message editor constants
# ---------------------------------------------------------------------------
# The 4 one-off system messages (not per-device, so no field checkboxes make
# sense here) — kept as simple enable+text rows, same shape as before.
_GSM_SYSTEM_MESSAGE_DEFAULTS = {
    "intro": ("Вступ", "<b>👋 Привіт!</b>\n\nДавай детально розглянемо, що ж відбувається на ринку пристроїв сьогодні. 📱💡"),
    "no_new_devices": ("Немає нових пристроїв", "<b>Нових пристрої за добу не з'явилось</b>"),
    "stats": ("Статистика оновлення", "📊 <b>Статистика оновлення:</b>\n⏱ <b>Час парсингу:</b> {execution_time}\n🔄 <b>Кількість моделей зі змінами:</b> {changed_models}"),
    "final": ("Фінальне повідомлення", "🏁 <b>Всі дані оновлено.</b>"),
}
_GSM_SYSTEM_MESSAGE_ORDER = ["intro", "no_new_devices", "stats", "final"]

# The 4 event categories a parse run can produce — each holds an ordered,
# user-editable list of blocks (header / device_card).
_GSM_CATEGORY_LABELS = {
    "gsm_new": ("🌐 Нові пристрої GSM Arena", "Заголовки, картки пристроїв — усе, що йде в Telegram, коли зʼявився новий пристрій на GSM Arena."),
    "ekatalog_new": ("🛒 Нові пристрої E-Katalog", "Те саме для нових позицій на E-Katalog."),
    "ekatalog_shops": ("🛍️ Зʼявились у продажу (E-Katalog)", "Коли в наявних пристроїв кількість магазинів змінюється з 0 на більше."),
    "regions_appeared": ("🌍 Зʼявились регіони (Xiaomi)", "Коли для пристрою Xiaomi вперше знайдено регіональні варіанти — і це НЕ вже покритий новий пристрій чи \"з'явився в продажу\" цього ж запуску."),
}
_GSM_CATEGORY_ORDER = ["gsm_new", "ekatalog_new", "ekatalog_shops", "regions_appeared"]

_GSM_DEFAULT_CATEGORY_BLOCKS = {
    "gsm_new": [
        {"kind": "header", "enabled": True, "text": "<b>🌐 GSM Arena: Нові пристрої:</b>", "photo_path": ""},
        {"kind": "device_card", "enabled": True, "fields": ["brand", "model_name", "device_type", "network_full", "dimensions", "diagonal", "price_uah", "regions", "link"]},
    ],
    "ekatalog_new": [
        {"kind": "header", "enabled": True, "text": "<b>🛒 E-Katalog: Нові пристрої:</b>", "photo_path": ""},
        {"kind": "device_card", "enabled": True, "fields": ["brand", "model_name", "dimensions", "diagonal", "status", "price_min_max", "regions", "link"]},
    ],
    "ekatalog_shops": [
        {"kind": "header", "enabled": True, "text": "<b>🛍️ Почали продавати на E-Katalog:</b>", "photo_path": ""},
        {"kind": "device_card", "enabled": True, "fields": ["brand", "model_name", "dimensions", "diagonal", "status", "price_min_max", "shops_count", "regions", "link"]},
    ],
    "regions_appeared": [
        {"kind": "header", "enabled": True, "text": "<b>🌍 З'явились регіони:</b>", "photo_path": ""},
        {"kind": "device_card", "enabled": True, "fields": ["model_name", "device_type", "network_full", "regions"]},
    ],
}


class ExtensionsView:
    def __init__(
        self,
        content_area,
        user_data,
        db,
        active_company_id=None,
        is_superadmin=False,
    ):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.is_superadmin = bool(is_superadmin)
        self.active_company_id = active_company_id or self.user_data.get("company_id")
        self.t = i18n.bind((self.user_data.get("id") or "").strip())
        self.current_extension_key = None
        self.gsm_detail_tab = "settings"
        self._gsm_config_draft = None
        self._gsm_form_refs = {}
        self._brands_draft = None
        self._brand_form_refs = []
        self._brands_table_body_ref: ft.Column | None = None
        self._brands_table_height_ref: ft.Container | None = None
        self._gsm_recent_count_ref: ft.TextField | None = None
        self._gsm_shops_appeared_max_age_months_ref: ft.TextField | None = None
        self._gsm_ek_on_sale_statuses_ref: ft.TextField | None = None
        self._parser_test_state = {
            "running": False,
            "mode": "full",
            "status": "Готово до тестового запуску",
            "started_at": "",
            "finished_at": "",
            "last_result": "",
        }
        # GSM Parser tab caching — built once (lazily, per tab), shown/hidden
        # on switch. Only the active tab is ever built eagerly; the rest are
        # built in the background the first time the user switches to them.
        self._gsm_tab_panels: dict = {}
        self._gsm_tab_btn_refs: dict = {}
        self._gsm_tab_built: set = set()
        self._gsm_loading_bar_ref: ft.ProgressBar | None = None
        self._gsm_loading_status_ref: ft.Text | None = None
        self._gsm_outer_col = None
        self._log_text_ref = None
        self._log_refresh_fn = None
        self._log_auto_refresh_running = False
        self._log_tab_visible = False
        self._gsm_perms_company_id = None

    def _page(self):
        try:
            return self.content_area.page if self.content_area else None
        except RuntimeError:
            return None

    def _show_help(self) -> None:
        page = self._page()
        if not page:
            return
        from views.help_utils import show_modal_help
        show_modal_help(page, self.t("extensions.help.title", "Довідка — Розширення"), [
            self.t("extensions.help.desc", "Додаткові модулі програми: GSM Parser для моніторингу цін і характеристик смартфонів."),
            (self.t("extensions.help.steps_title", "Як користуватись"), None),
            self.t("extensions.help.step1", "1. Суперадмін вмикає/вимикає парсер для кожної компанії."),
            self.t("extensions.help.step2", "2. Адмін може перемикати в межах дозволу суперадміна."),
            self.t("extensions.help.step3", "3. Натисніть «Відкрити» для доступу до результатів і налаштувань парсера."),
            self.t("extensions.help.step4", "4. В налаштуваннях: задайте бренди, URL-и, затримки та Telegram-звіти."),
        ])

    def _show_snack(self, text, color):
        page = self._page()
        if not page:
            return
        page.snack_bar = ft.SnackBar(
            ft.Text(text, color=ft.Colors.WHITE, weight="bold"),
            bgcolor=color,
            duration=2500,
        )
        page.snack_bar.open = True
        page.update()

    def _show_saved_dialog(self, message: str = "Збережено"):
        page = self._page()
        if not page:
            return
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Успішно"),
            content=ft.Text(str(message or "Збережено")),
            actions=[
                ft.FilledButton("OK", on_click=lambda _e: page.pop_dialog()),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    def _pick_image_path(self) -> str | None:
        if not TK_AVAILABLE:
            self._show_snack(
                "Системний діалог вибору файлу недоступний", ft.Colors.ORANGE
            )
            return None
        root = tk.Tk()
        root.withdraw()
        with contextlib.suppress(Exception):
            root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Оберіть зображення",
            initialdir=os.path.expanduser("~") or os.getcwd(),
            filetypes=[
                ("Картинки", "*.png *.jpg *.jpeg *.webp *.gif"),
                ("Усі файли", "*.*"),
            ],
        )
        root.destroy()
        return path or None

    def _upload_logo_to_field(self, field_control: ft.TextField, logo_kind: str):
        picked = self._pick_image_path()
        if not picked:
            return
        filename = os.path.basename(picked) or f"{logo_kind}.png"
        uploaded_url = self.db.upload_file(
            "gsm_parser_assets",
            picked,
            filename=filename,
        )
        if not uploaded_url:
            self._show_snack(self.t("extensions.err.logo_load", "Не вдалося завантажити логотип"), ft.Colors.RED)
            return
        field_control.value = uploaded_url
        self._capture_gsm_form_state()
        # The header-photo field lives in the message editor (category
        # blocks), which _capture_gsm_form_state() doesn't cover — capture
        # and persist it right away so the upload is durable immediately,
        # without depending on the user remembering to click "Зберегти"
        # afterward.
        self._capture_message_editor_state()
        with contextlib.suppress(Exception):
            self.db.save_gsm_parser_global_config(self._get_gsm_draft())
        try:
            field_control.update()
        except Exception:
            pass
        self._show_snack(self.t("extensions.logo_loaded", "Фото завантажено і збережено"), ft.Colors.GREEN)

    def _clone_data(self, payload):
        try:
            return json.loads(json.dumps(payload, ensure_ascii=False))
        except Exception:
            return payload

    def _launch_parser(self, company_id):
        # Superadmin is the sole company-wide gate — once enabled, it's on
        # for everyone in the company; there's no separate admin-level toggle.
        state = self.db.get_company_gsm_parser_state(company_id)
        if not state.get("sa_enabled"):
            self._show_snack(self.t("extensions.gsm_disabled", "GSM Parser вимкнено для цієї компанії"), ft.Colors.ORANGE)
            return
        GsmParserView(
            self.content_area,
            self.user_data,
            self.db,
            active_company_id=company_id,
        ).build()

    def _extension_defs(self):
        return [
            {
                "key": "gsm_parser",
                "title": "GSM Parser",
                "subtitle": "Керування доступом, розкладом, Telegram-звітами та параметрами парсингу.",
                "icon": ft.Icons.SATELLITE_ALT,
            }
        ]

    def _companies(self):
        companies = [
            c
            for c in (self.db.get_all_companies() or [])
            if isinstance(c, dict) and c.get("id")
        ]
        return sorted(companies, key=lambda c: (c.get("name") or "").strip().lower())

    def _get_gsm_draft(self):
        if not isinstance(self._gsm_config_draft, dict):
            self._gsm_config_draft = self._clone_data(
                self.db.get_gsm_parser_global_config()
            )
        return self._gsm_config_draft

    def _capture_message_editor_state(self):
        """Pulls the current values of the Telegram message editor (bot
        fields, system messages, per-category blocks) from self._gsm_form_refs
        back into the config draft. Self-contained — unlike
        _capture_gsm_form_state, it doesn't depend on self.gsm_detail_tab
        (that tab-scoped gate never matches for this editor, since it lives
        inside the "settings" tab alongside brands/schedule, each with their
        own independent save button — see _build_brands_settings_tab)."""
        draft = self._get_gsm_draft()
        refs = self._gsm_form_refs if isinstance(self._gsm_form_refs, dict) else {}

        telegram_refs = refs.get("telegram") or {}
        telegram = draft.setdefault("telegram", {})
        for key, control in telegram_refs.items():
            telegram[key] = str(getattr(control, "value", "") or "").strip()

        sys_msg_refs = refs.get("system_messages") or {}
        if sys_msg_refs:
            message_rows = []
            for msg_id, row_refs in sys_msg_refs.items():
                enabled_control = row_refs.get("enabled")
                title, _default_text = _GSM_SYSTEM_MESSAGE_DEFAULTS.get(msg_id, (msg_id, ""))
                message_rows.append(
                    {
                        "id": msg_id,
                        "title": title,
                        "message_type": "text",
                        "enabled": (
                            bool(enabled_control.value) if enabled_control else True
                        ),
                        "text": str(getattr(row_refs.get("text"), "value", "") or "").strip(),
                    }
                )
            telegram["messages"] = message_rows

        categories_refs = refs.get("categories") or {}
        if categories_refs:
            categories_out = {}
            for cat_key, block_refs_list in categories_refs.items():
                out_blocks = []
                for br in block_refs_list:
                    kind = br.get("kind")
                    enabled_ctrl = br.get("enabled")
                    enabled_val = bool(enabled_ctrl.value) if enabled_ctrl else True
                    if kind == "header":
                        out_blocks.append(
                            {
                                "kind": "header",
                                "enabled": enabled_val,
                                "text": str(getattr(br.get("text"), "value", "") or ""),
                                "photo_path": str(
                                    getattr(br.get("photo"), "value", "") or ""
                                ).strip(),
                            }
                        )
                    elif kind == "device_card":
                        checks = br.get("field_checks") or {}
                        selected = [
                            key
                            for key, cb in checks.items()
                            if bool(getattr(cb, "value", False))
                        ]
                        out_blocks.append(
                            {
                                "kind": "device_card",
                                "enabled": enabled_val,
                                "fields": selected,
                            }
                        )
                categories_out[cat_key] = out_blocks
            telegram["categories"] = categories_out

    def _capture_gsm_form_state(self):
        if self.current_extension_key != "gsm_parser":
            return
        draft = self._get_gsm_draft()
        refs = self._gsm_form_refs if isinstance(self._gsm_form_refs, dict) else {}

        if self.gsm_detail_tab == "settings":
            settings = draft.setdefault("settings", {})
            comparison = draft.setdefault("comparison", {})
            devices = settings.setdefault("devices", {})
            gsm_rows = []
            for row_refs in refs.get("gsm_rows") or []:
                gsm_rows.append(
                    {
                        "brand": str(row_refs.get("brand").value or "").strip(),
                        "url": str(row_refs.get("url").value or "").strip(),
                        "last_device": str(
                            row_refs.get("last_device").value or ""
                        ).strip(),
                    }
                )
            ekatalog_rows = []
            for row_refs in refs.get("ekatalog_rows") or []:
                ekatalog_rows.append(
                    {
                        "device_type": str(
                            row_refs.get("device_type").value or ""
                        ).strip(),
                        "brand": str(row_refs.get("brand").value or "").strip(),
                        "url": str(row_refs.get("url").value or "").strip(),
                        "last_device": str(
                            row_refs.get("last_device").value or ""
                        ).strip(),
                    }
                )
            devices["gsm_arena"] = gsm_rows
            devices["ekatalog"] = ekatalog_rows

            delays = settings.setdefault("delays", {})
            for key, control in (refs.get("delays") or {}).items():
                raw_value = str(getattr(control, "value", "") or "").strip()
                try:
                    delays[key] = max(0.0, float(raw_value))
                except Exception:
                    delays[key] = 0.0

            retention_control = refs.get("change_retention_days")
            retention_raw = (
                str(getattr(retention_control, "value", "") or "").strip()
                if retention_control
                else ""
            )
            try:
                settings["change_retention_days"] = max(
                    1, min(3650, int(retention_raw))
                )
            except Exception:
                settings["change_retention_days"] = 365

            comp_refs = refs.get("comparison") or {}
            comparison["remove_4g5g"] = bool(
                getattr(comp_refs.get("remove_4g5g"), "value", True)
            )
            comparison["remove_punctuation"] = bool(
                getattr(comp_refs.get("remove_punctuation"), "value", False)
            )
            comparison["case_insensitive"] = bool(
                getattr(comp_refs.get("case_insensitive"), "value", True)
            )
            exclude_words_raw = str(
                getattr(comp_refs.get("exclude_words"), "value", "") or ""
            ).strip()
            comparison["exclude_words"] = [
                part.strip()
                for part in exclude_words_raw.replace("\n", ",").split(",")
                if part.strip()
            ]

            custom_rules_rows = []
            for row_refs in refs.get("comparison_rules") or []:
                enabled = bool(getattr(row_refs.get("enabled"), "value", True))
                pattern = str(
                    getattr(row_refs.get("pattern"), "value", "") or ""
                ).strip()
                replacement = str(
                    getattr(row_refs.get("replacement"), "value", "") or ""
                ).strip()
                if pattern:
                    custom_rules_rows.append(
                        {
                            "enabled": enabled,
                            "pattern": pattern,
                            "replacement": replacement,
                        }
                    )
            comparison["custom_rules"] = custom_rules_rows

    def _set_gsm_company_enabled(self, company_id, enabled):
        if self.is_superadmin:
            ok = self.db.set_company_gsm_parser_superadmin(company_id, enabled)
        else:
            ok = self.db.set_company_gsm_parser_admin(company_id, enabled)
        if ok:
            self._show_snack(self.t("extensions.gsm_state_updated", "Стан GSM Parser оновлено"), ft.Colors.GREEN)
            perms_panel = (self._gsm_tab_panels or {}).get("permissions")
            if perms_panel is not None:
                try:
                    perms_panel.content = self._build_permissions_tab()
                    perms_panel.update()
                except Exception:
                    self._gsm_outer_col = None
                    self._gsm_tab_panels = {}
                    self.build()
            else:
                self.build()
            return
        self._show_snack(self.t("extensions.err.state_update", "Не вдалося оновити стан"), ft.Colors.RED)

    def _section_card(self, title, subtitle, content, action=None):
        header_controls = [
            ft.Column(
                [
                    ft.Text(title, size=18, weight=ft.FontWeight.W_700),
                    ft.Text(subtitle, size=12, color=ft.Colors.BLUE_GREY_700),
                ],
                spacing=2,
                expand=True,
            )
        ]
        if action is not None:
            header_controls.append(action)
        return ft.Container(
            bgcolor=ft.Colors.WHITE,
            border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
            border_radius=18,
            padding=18,
            content=ft.Column(
                [
                    ft.Row(
                        header_controls, alignment=ft.MainAxisAlignment.SPACE_BETWEEN
                    ),
                    ft.Divider(height=14, color=ft.Colors.BLUE_GREY_50),
                    content,
                ],
                spacing=10,
            ),
        )

    def _open_extension(self, extension_key, company_id=None):
        self.current_extension_key = extension_key
        if extension_key == "gsm_parser":
            self.gsm_detail_tab = "settings" if self.is_superadmin else "results"
            self._gsm_tab_panels = {}
            self._gsm_tab_btn_refs = {}
            self._gsm_tab_built = set()
            self._gsm_outer_col = None
        self.build()

    def _back_to_extensions_list(self, _):
        self._capture_gsm_form_state()
        if self.gsm_detail_tab == "settings":
            self._capture_brands_form_state()
        self.current_extension_key = None
        self.gsm_detail_tab = "settings"
        self._gsm_form_refs = {}
        self._brands_draft = None
        self._brand_form_refs = []
        self._gsm_tab_panels = {}
        self._gsm_tab_btn_refs = {}
        self._gsm_tab_built = set()
        self._gsm_outer_col = None
        self.build()

    def _gsm_tab_btn_style(self, is_active: bool) -> ft.ButtonStyle:
        return ft.ButtonStyle(
            bgcolor=_OK_DK if is_active else ft.Colors.WHITE,
            color=ft.Colors.WHITE if is_active else _TEXT,
            side=ft.BorderSide(1, _OK_DK if is_active else ft.Colors.BLUE_GREY_200),
            shape=ft.RoundedRectangleBorder(radius=12),
            padding=ft.padding.symmetric(horizontal=18, vertical=13),
        )

    def _apply_gsm_tab_visibility(self):
        for k, panel in self._gsm_tab_panels.items():
            panel.visible = (k == self.gsm_detail_tab)
        for k, btn in self._gsm_tab_btn_refs.items():
            btn.style = self._gsm_tab_btn_style(k == self.gsm_detail_tab)

    def _switch_gsm_tab(self, tab_key):
        _scrollable = {"permissions", "settings"}

        def _handler(_):
            self._capture_gsm_form_state()
            if self.gsm_detail_tab == "settings":
                self._capture_brands_form_state()
            self.gsm_detail_tab = tab_key

            if self._gsm_outer_col is not None and self._gsm_tab_panels:
                try:
                    self.content_area.scroll = (
                        ft.ScrollMode.ADAPTIVE if tab_key in _scrollable else None
                    )
                except Exception:
                    pass
                self._apply_gsm_tab_visibility()
                with contextlib.suppress(Exception):
                    self._gsm_outer_col.update()

                page = self._page()
                if page and tab_key not in self._gsm_tab_built:
                    page.run_task(self._load_gsm_tab_async, tab_key)
                elif tab_key == "logs" and hasattr(self, "_log_refresh_fn"):
                    self._log_refresh_fn()
                    if page:
                        page.run_task(self._log_auto_refresh_task)
            else:
                self.build()

        return _handler

    def _build_gsm_tab_body(self, key: str):
        """Synchronous build for one GSM Parser tab (DB reads + control
        building). Called only from _load_gsm_tab_async, on the event loop —
        NOT via asyncio.to_thread. GsmParserView calls page.run_task(...)
        itself in a couple of places; doing that from a worker thread
        instead of the loop thread is the kind of thing that produces
        sporadic "something started loading" moments and the brand table's
        expand/collapse state flashing/resetting, because the GSM parser
        module's shared in-memory cache/locks were never designed for two
        threads touching them at once. Every DB call and control constructed
        here stays on the single thread the rest of that module assumes."""
        if key == "permissions":
            return self._build_permissions_tab()
        if key == "settings":
            return self._build_brands_settings_tab()
        if key == "logs":
            return self._build_log_tab()
        return self._build_results_tab()

    def _set_gsm_loading_status(self, text: str) -> None:
        if self._gsm_loading_status_ref is not None:
            self._gsm_loading_status_ref.value = text
            with contextlib.suppress(Exception):
                self._gsm_loading_status_ref.update()

    async def _build_results_tab_async(self):
        """Results tab, specifically: check → sync (with live status text
        via _set_gsm_loading_status) → build, awaited in that explicit
        order. Never "build the table now, sync silently in the background"
        — that raced a full table rebuild against whatever was already on
        screen with no visible indicator, which is what turned a routine
        cache check into multi-minute freezes with no explanation."""
        from views.gsm_parser_view import GsmParserView

        company_id = self.active_company_id or self.user_data.get("company_id") or "__global__"
        view = GsmParserView(
            self.content_area,
            self.user_data,
            self.db,
            active_company_id=company_id,
            is_superadmin=self.is_superadmin,
        )
        return await view._prepare_results_content(status_cb=self._set_gsm_loading_status)

    async def _load_gsm_tab_async(self, key: str):
        """Build one tab's content, with a visible loading bar and every tab
        button disabled meanwhile — this is what used to freeze the whole
        screen (all four tabs were built synchronously, upfront, before
        anything was shown). The build itself still runs on the event loop
        (see _build_gsm_tab_body docstring for why it's not threaded); the
        `await asyncio.sleep(0)` below just yields one tick so the spinner
        and disabled buttons actually reach Flutter before that build runs."""
        if key in self._gsm_tab_built:
            return

        for btn in self._gsm_tab_btn_refs.values():
            btn.disabled = True
        if self._gsm_loading_bar_ref is not None:
            self._gsm_loading_bar_ref.visible = True
        self._set_gsm_loading_status(
            self.t("gsm.checking_cache", "Перевірка кешу…") if key == "results" else ""
        )
        with contextlib.suppress(Exception):
            self._gsm_outer_col.update()
        await asyncio.sleep(0)

        try:
            if key == "results":
                body = await self._build_results_tab_async()
            else:
                body = self._build_gsm_tab_body(key)
        except Exception as exc:
            body = ft.Text(f"Помилка завантаження: {exc}", color=ft.Colors.RED_400)

        panel = self._gsm_tab_panels.get(key)
        if panel is not None:
            panel.content = body
        self._gsm_tab_built.add(key)

        for btn in self._gsm_tab_btn_refs.values():
            btn.disabled = False
        if self._gsm_loading_bar_ref is not None:
            self._gsm_loading_bar_ref.visible = False
        self._set_gsm_loading_status("")
        with contextlib.suppress(Exception):
            self._gsm_outer_col.update()

        if key == "logs" and hasattr(self, "_log_refresh_fn"):
            page = self._page()
            if page:
                page.run_task(self._log_auto_refresh_task)

    async def _log_auto_refresh_task(self):
        import asyncio as _aio

        import views.gsm_parser_view as _gpv
        while True:
            await _aio.sleep(3)
            if self.gsm_detail_tab != "logs":
                break
            running = False
            try:
                running = bool(_gpv._PARSER_RUNNING)
            except Exception:
                pass
            if self._log_refresh_fn:
                self._log_refresh_fn()
            try:
                page = self._page()
            except Exception:
                # content_area was detached from the page (e.g. account
                # switch/tab navigation) — nothing left to refresh, stop.
                break
            if page:
                try:
                    page.update()
                except Exception:
                    pass
            if not running:
                break

    def _save_gsm_config(self, _):
        self._capture_gsm_form_state()
        draft = self._get_gsm_draft()
        if self.db.save_gsm_parser_global_config(draft):
            self._gsm_config_draft = self._clone_data(
                self.db.get_gsm_parser_global_config()
            )
            self._show_snack(self.t("extensions.settings_saved", "Налаштування GSM Parser збережено"), ft.Colors.GREEN)
            self.build()
            return
        self._show_snack(self.t("extensions.err.save_settings", "Не вдалося зберегти налаштування"), ft.Colors.RED)

    def _apply_runs_per_day(self, _):
        self._capture_gsm_form_state()
        draft = self._get_gsm_draft()
        runs = draft.get("automation", {}).get("runs_per_day", 1)
        try:
            runs = max(1, min(12, int(runs)))
        except Exception:
            runs = 1
        start_times = list(draft.get("automation", {}).get("start_times") or [])
        start_times = start_times[:runs]
        while len(start_times) < runs:
            start_times.append("")
        draft.setdefault("automation", {})["runs_per_day"] = runs
        draft.setdefault("automation", {})["start_times"] = start_times
        self.build()

    def _gsm_category_blocks_draft(self, cat_key: str) -> list:
        draft = self._get_gsm_draft()
        telegram = draft.setdefault("telegram", {})
        categories = telegram.setdefault("categories", {})
        if not isinstance(categories.get(cat_key), list):
            fresh_blocks = [dict(b) for b in _GSM_DEFAULT_CATEGORY_BLOCKS.get(cat_key, [])]
            # One-time migration: carry over a previously-uploaded global logo
            # (old fixed-header config) into this category's default header
            # block, so switching to the new editor doesn't silently drop it.
            old_assets = telegram.get("assets") if isinstance(telegram.get("assets"), dict) else {}
            logo_key = "gsm_logo_path" if cat_key == "gsm_new" else "ekatalog_logo_path"
            logo_val = str(old_assets.get(logo_key) or "").strip()
            if logo_val:
                for b in fresh_blocks:
                    if b.get("kind") == "header" and not b.get("photo_path"):
                        b["photo_path"] = logo_val
                        break
            categories[cat_key] = fresh_blocks
        return categories[cat_key]

    def _refresh_gsm_settings_tab(self) -> None:
        """Force the (cached) 'settings' tab panel to actually rebuild — a
        plain self.build() alone hits the fast path in
        _build_gsm_parser_detail and silently keeps showing the stale panel
        (same gotcha _save_brands already works around)."""
        self._gsm_tab_panels = {}
        self._gsm_tab_btn_refs = {}
        self._gsm_outer_col = None
        self.build()

    def _add_category_block(self, cat_key: str, kind: str):
        def _handler(_):
            self._capture_message_editor_state()
            blocks = self._gsm_category_blocks_draft(cat_key)
            if kind == "header":
                blocks.append({"kind": "header", "enabled": True, "text": "", "photo_path": ""})
            else:
                blocks.append({"kind": "device_card", "enabled": True, "fields": []})
            self._refresh_gsm_settings_tab()

        return _handler

    def _remove_category_block(self, cat_key: str, index: int):
        def _handler(_):
            self._capture_message_editor_state()
            blocks = self._gsm_category_blocks_draft(cat_key)
            if 0 <= index < len(blocks):
                blocks.pop(index)
            self._refresh_gsm_settings_tab()

        return _handler

    def _move_category_block(self, cat_key: str, index: int, delta: int):
        def _handler(_):
            self._capture_message_editor_state()
            blocks = self._gsm_category_blocks_draft(cat_key)
            new_index = index + delta
            if 0 <= index < len(blocks) and 0 <= new_index < len(blocks):
                blocks[index], blocks[new_index] = blocks[new_index], blocks[index]
            self._refresh_gsm_settings_tab()

        return _handler

    def _add_device_row(self, source_key):
        def _handler(_):
            self._capture_gsm_form_state()
            draft = self._get_gsm_draft()
            devices = draft.setdefault("settings", {}).setdefault("devices", {})
            rows = devices.setdefault(source_key, [])
            if source_key == "ekatalog":
                rows.append(
                    {
                        "device_type": "Смартфон",
                        "brand": "",
                        "url": "",
                        "last_device": "",
                    }
                )
            else:
                rows.append({"brand": "", "url": "", "last_device": ""})
            self.build()

        return _handler

    def _remove_device_row(self, source_key, index):
        def _handler(_):
            self._capture_gsm_form_state()
            draft = self._get_gsm_draft()
            devices = draft.setdefault("settings", {}).setdefault("devices", {})
            rows = devices.setdefault(source_key, [])
            if 0 <= index < len(rows):
                rows.pop(index)
            self.build()

        return _handler

    def _add_comparison_rule(self, _):
        self._capture_gsm_form_state()
        draft = self._get_gsm_draft()
        comparison = draft.setdefault("comparison", {})
        rules = comparison.setdefault("custom_rules", [])
        rules.append({"enabled": True, "pattern": "", "replacement": ""})
        self.build()

    def _remove_comparison_rule(self, index):
        def _handler(_):
            self._capture_gsm_form_state()
            draft = self._get_gsm_draft()
            comparison = draft.setdefault("comparison", {})
            rules = comparison.setdefault("custom_rules", [])
            if 0 <= index < len(rules):
                rules.pop(index)
            self.build()

        return _handler

    # ------------------------------------------------------------------ #
    #  Brands draft (gsm_parser_brands table)                             #
    # ------------------------------------------------------------------ #
    def _get_brands_draft(self) -> list:
        if self._brands_draft is None:
            self._brands_draft = list(self.db.get_gsm_parser_brands() or [])
        return self._brands_draft

    def _capture_brands_form_state(self):
        rows = []
        for refs in (self._brand_form_refs or []):
            row_id = str(refs.get("_id") or "").strip()
            enabled_ctrl = refs.get("enabled")
            rows.append({
                "id": row_id,
                "brand": str(getattr(refs.get("brand"), "value", "") or "").strip(),
                "gsm_url": str(getattr(refs.get("gsm_url"), "value", "") or "").strip(),
                "last_item": str(getattr(refs.get("last_item"), "value", "") or "").strip(),
                "ek_url_phones": str(getattr(refs.get("ek_url_phones"), "value", "") or "").strip(),
                "ek_url_tablets": str(getattr(refs.get("ek_url_tablets"), "value", "") or "").strip(),
                "ek_url_watches": str(getattr(refs.get("ek_url_watches"), "value", "") or "").strip(),
                "enabled": bool(getattr(enabled_ctrl, "value", True)) if enabled_ctrl is not None else True,
            })
        self._brands_draft = rows

        if self._gsm_recent_count_ref is not None:
            raw = str(getattr(self._gsm_recent_count_ref, "value", "") or "").strip()
            try:
                count = max(1, min(200, int(raw)))
            except Exception:
                count = 15
            draft = self._get_gsm_draft()
            draft.setdefault("settings", {})["gsm_recent_count"] = count

        if self._gsm_shops_appeared_max_age_months_ref is not None:
            raw = str(
                getattr(self._gsm_shops_appeared_max_age_months_ref, "value", "") or ""
            ).strip()
            try:
                months = max(1, min(120, int(raw)))
            except Exception:
                months = 8
            draft = self._get_gsm_draft()
            draft.setdefault("settings", {})["shops_appeared_max_age_months"] = months

        if self._gsm_ek_on_sale_statuses_ref is not None:
            raw = str(getattr(self._gsm_ek_on_sale_statuses_ref, "value", "") or "").strip()
            draft = self._get_gsm_draft()
            draft.setdefault("settings", {})["ek_on_sale_statuses"] = raw or "Продаж"

    def _save_brands(self, _):
        self._capture_brands_form_state()
        ok = self.db.save_gsm_parser_brands_bulk(self._brands_draft or [])
        ok = self.db.save_gsm_parser_global_config(self._get_gsm_draft()) and ok
        if ok:
            self._brands_draft = list(self.db.get_gsm_parser_brands() or [])
            self._show_snack(self.t("extensions.brands_saved", "Бренди збережено"), ft.Colors.GREEN)
            # The "Налаштування" tab panel is cached (see _gsm_tab_panels) and
            # is NOT rebuilt by a plain self.build() — without clearing it,
            # the save silently has no visible effect until the user leaves
            # and re-enters the GSM extension.
            self._gsm_tab_panels = {}
            self._gsm_tab_btn_refs = {}
            self._gsm_outer_col = None
            self.build()
        else:
            self._show_snack(self.t("extensions.err.save_brands", "Не вдалося зберегти бренди"), ft.Colors.RED)

    _BRAND_COL_W = {
        "brand": 130,
        "gsm_url": 260,
        "last_item": 190,
        "ek_phones": 200,
        "ek_tablets": 200,
        "ek_watches": 200,
    }

    def _make_brand_row_ctrl(self, idx: int, refs: dict, row_bg) -> ft.Container:
        return ft.Container(
            bgcolor=row_bg,
            border=ft.border.only(bottom=ft.BorderSide(1, ft.Colors.BLUE_GREY_100)),
            padding=ft.padding.symmetric(horizontal=10, vertical=6),
            content=ft.Row(
                [
                    ft.Container(refs["enabled"], width=40),
                    refs["brand"],
                    refs["gsm_url"],
                    refs["last_item"],
                    refs["ek_url_phones"],
                    refs["ek_url_tablets"],
                    refs["ek_url_watches"],
                    ft.IconButton(
                        icon=ft.Icons.DELETE_OUTLINE,
                        icon_color=ft.Colors.RED_400,
                        tooltip="Видалити рядок",
                        icon_size=18,
                        on_click=self._remove_brand_row(idx),
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _make_brand_refs(self, row: dict) -> dict:
        cw = self._BRAND_COL_W
        return {
            "_id": str(row.get("id") or ""),
            "enabled": ft.Checkbox(
                value=bool(row.get("enabled", True)),
                tooltip="Увімкнено для парсингу",
            ),
            "brand": ft.TextField(
                value=str(row.get("brand") or ""),
                dense=True, border_radius=8, width=cw["brand"],
                text_size=13,
            ),
            "gsm_url": ft.TextField(
                value=str(row.get("gsm_url") or ""),
                dense=True, border_radius=8, width=cw["gsm_url"],
                text_size=13,
            ),
            "last_item": ft.TextField(
                value=str(row.get("last_item") or ""),
                dense=True, border_radius=8, width=cw["last_item"],
                text_size=13,
            ),
            "ek_url_phones": ft.TextField(
                value=str(row.get("ek_url_phones") or ""),
                dense=True, border_radius=8, width=cw["ek_phones"],
                text_size=13,
            ),
            "ek_url_tablets": ft.TextField(
                value=str(row.get("ek_url_tablets") or ""),
                dense=True, border_radius=8, width=cw["ek_tablets"],
                text_size=13,
            ),
            "ek_url_watches": ft.TextField(
                value=str(row.get("ek_url_watches") or ""),
                dense=True, border_radius=8, width=cw["ek_watches"],
                text_size=13,
            ),
        }

    def _add_brand_row(self, _):
        self._capture_brands_form_state()
        draft = self._get_brands_draft()
        new_row = {
            "id": "", "brand": "", "gsm_url": "", "last_item": "",
            "ek_url_phones": "", "ek_url_tablets": "", "ek_url_watches": "",
        }
        draft.append(new_row)

        # In-place update — no full rebuild, so ExpansionTile stays open
        if self._brands_table_body_ref is not None:
            idx = len(self._brand_form_refs)
            refs = self._make_brand_refs(new_row)
            self._brand_form_refs.append(refs)
            row_bg = ft.Colors.WHITE if idx % 2 == 0 else _ALT
            row_ctrl = self._make_brand_row_ctrl(idx, refs, row_bg)
            # Replace "empty" placeholder if it was the only control
            if (len(self._brands_table_body_ref.controls) == 1
                    and getattr(self._brands_table_body_ref.controls[0], "data", None) == "brands_empty_placeholder"):
                self._brands_table_body_ref.controls.clear()
            self._brands_table_body_ref.controls.append(row_ctrl)
            if self._brands_table_height_ref is not None:
                n = len(draft)
                self._brands_table_height_ref.height = min(50 + n * 62, 500)
                with contextlib.suppress(Exception):
                    self._brands_table_height_ref.update()
            with contextlib.suppress(Exception):
                self._brands_table_body_ref.update()
        else:
            self.build()

    def _remove_brand_row(self, index):
        def _handler(_):
            self._capture_brands_form_state()
            draft = self._get_brands_draft()
            if 0 <= index < len(draft):
                draft.pop(index)
            self.build()
        return _handler

    def _build_extensions_matrix(self):
        extension_defs = self._extension_defs()
        cards = []
        for ext in extension_defs:
            cards.append(
                ft.Container(
                    bgcolor=ft.Colors.WHITE,
                    border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
                    border_radius=16,
                    padding=ft.padding.symmetric(horizontal=18, vertical=14),
                    ink=True,
                    on_click=lambda _, key=ext.get("key"): self._open_extension(key),
                    content=ft.Row(
                        [
                            ft.Container(
                                width=48,
                                height=48,
                                border_radius=14,
                                bgcolor=_OK_BG,
                                alignment=ft.alignment.Alignment(0, 0),
                                content=ft.Icon(ext.get("icon"), color=_OK_DK, size=24),
                            ),
                            ft.Column(
                                [
                                    ft.Text(
                                        ext.get("title") or "Розширення",
                                        size=16,
                                        weight=ft.FontWeight.W_700,
                                    ),
                                    ft.Text(
                                        ext.get("subtitle") or "",
                                        size=12,
                                        color=ft.Colors.BLUE_GREY_600,
                                    ),
                                ],
                                expand=True,
                                spacing=3,
                            ),
                            ft.Icon(ft.Icons.CHEVRON_RIGHT, color=ft.Colors.BLUE_GREY_400),
                        ],
                        spacing=14,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )

        return [
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.EXTENSION, size=26, color=_ACCENT),
                            ft.Column(
                                [
                                    ft.Row(
                                        [
                                            ft.Text(self.t("extensions.title", "Розширення"), size=26, weight=ft.FontWeight.W_700),
                                            ft.IconButton(
                                                icon=ft.Icons.HELP_OUTLINE,
                                                icon_size=18,
                                                tooltip=self.t("common.help.tooltip", "Довідка"),
                                                on_click=lambda _e: self._show_help(),
                                            ),
                                        ],
                                        spacing=2,
                                        tight=True,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    ),
                                    ft.Text(
                                        str(self.user_data.get("full_name") or self.user_data.get("email") or ""),
                                        size=12,
                                        color=ft.Colors.BLUE_GREY_500,
                                    ),
                                ],
                                spacing=1,
                            ),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text(
                        self.t("extensions.select_hint", "Оберіть розширення для налаштування."),
                        size=13,
                        color=ft.Colors.BLUE_GREY_700,
                    ),
                    ft.Column(cards, spacing=10),
                ],
                spacing=14,
            )
        ]

    def _build_launch_tab(self):
        """Show parser results and provide manual launch option."""
        state = (
            self._parser_test_state if isinstance(self._parser_test_state, dict) else {}
        )
        try:
            # Get recent parser results from database
            with self.db._pg().cursor() as cur:
                cur.execute("""
                    SELECT 
                        id, source, brand, device_type, model_name,
                        last_seen_at, created_at
                    FROM public.gsm_parser_devices
                    ORDER BY last_seen_at DESC
                    LIMIT 50
                """)
                devices = cur.fetchall() or []
        except Exception as exc:
            devices = []
            logger.exception("Failed to load parser results: %s", exc)

        # Build results table
        result_rows = []
        for dev in devices:
            result_rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text(dev.get("source", ""))),
                        ft.DataCell(ft.Text(dev.get("brand", ""))),
                        ft.DataCell(ft.Text(dev.get("device_type", "") or "-")),
                        ft.DataCell(ft.Text(dev.get("model_name", "")[:40])),
                        ft.DataCell(ft.Text(str(dev.get("last_seen_at", ""))[:10])),
                    ]
                )
            )

        def _run_mode_click(mode: str):
            async def _run_async(page):
                state["running"] = True
                page._parser_is_running = True
                state["mode"] = mode
                state["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                state["finished_at"] = ""
                state["status"] = f"Запуск: {mode}..."
                state["last_result"] = ""

                # ── Overlay refs ──────────────────────────────────────────────
                _ov  = getattr(page, "_prs_overlay",      None)
                _sp  = getattr(page, "_prs_spinner",      None)
                _si  = getattr(page, "_prs_status_icon",  None)
                _ti  = getattr(page, "_prs_title",        None)
                _ms  = getattr(page, "_prs_message",      None)
                _inn = getattr(page, "_prs_inner",        None)

                def _ov_show_running(msg=""):
                    if not (_ov and _sp and _si and _ti and _ms and _inn):
                        return
                    _ov.visible = True
                    _sp.visible = True
                    _si.visible = False
                    _ti.value = f"Парсинг: {mode}"
                    _ms.value = str(msg)[:120]
                    _inn.bgcolor = ft.Colors.BLUE_GREY_900
                    with contextlib.suppress(Exception):
                        page.update()

                def _ov_show_done(success: bool, finished_at: str):
                    if not (_ov and _sp and _si and _ti and _ms and _inn):
                        return
                    _sp.visible = False
                    _si.visible = True
                    if success:
                        _si.name = ft.Icons.CHECK_CIRCLE
                        _ti.value = "Парсинг завершено"
                        _ms.value = f"{mode}  •  {finished_at[-8:]}"
                        _inn.bgcolor = ft.Colors.GREEN_700
                    else:
                        _si.name = ft.Icons.ERROR
                        _ti.value = "Помилка парсингу"
                        _ms.value = f"{mode}  •  {finished_at[-8:]}"
                        _inn.bgcolor = ft.Colors.RED_700
                    with contextlib.suppress(Exception):
                        page.update()

                _ov_show_running("Підключення до сервера...")

                try:
                    if self._page():
                        self.build()
                except Exception:
                    pass

                # Parsing runs EXCLUSIVELY on the VPS (parser_service.py) —
                # never locally. The desktop build doesn't ship GSM_Parser/
                # main.py+ekatalog.py the way the VPS deployment does (and
                # even where it does, running a multi-hour scrape on
                # whichever admin's laptop happens to click this button,
                # tying up their machine and IP, defeats the entire point of
                # hosting the parser on a VPS). If the VPS trigger service
                # isn't reachable, this must fail loudly — never silently
                # fall back to a local run.
                ok = False
                msg = ""
                try:
                    svc_cfg = await asyncio.to_thread(self.db.get_parser_service_config)
                    vps_url = str((svc_cfg or {}).get("vps_service_url") or "").strip().rstrip("/")
                    vps_key = str((svc_cfg or {}).get("vps_api_key") or "").strip()
                    if not vps_url:
                        raise RuntimeError(
                            "VPS-сервіс парсера не налаштований (Розширення → GSM Parser → "
                            "адреса сервісу). Запуск можливий лише на віддаленому сервері."
                        )
                    import httpx

                    headers = {"x-api-key": vps_key} if vps_key else {}
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.post(
                            f"{vps_url}/trigger", json={"mode": mode}, headers=headers
                        )
                        resp.raise_for_status()
                        data = resp.json()
                    ok = bool(data.get("ok"))
                    msg = str(data.get("message") or "")
                    state["status"] = (
                        f"Запущено на сервері: {mode}"
                        if ok
                        else f"Сервер відхилив запуск: {msg or 'невідома причина'}"
                    )
                    state["last_result"] = msg or (
                        "Стежте за прогресом у вкладці «Лог»." if ok else ""
                    )
                except Exception as exc:
                    logger.exception("VPS parser trigger failed: %s", exc)
                    state["status"] = f"Помилка запуску: {mode}"
                    state["last_result"] = str(exc)
                finally:
                    state["running"] = False
                    page._parser_is_running = False
                    state["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    _ov_show_done(ok, state["finished_at"])
                    try:
                        if self._page():
                            self.build()
                    except Exception:
                        pass

            def _handler(_):
                page = self._page()
                # Global double-launch guard
                if page and getattr(page, "_parser_is_running", False):
                    self._show_snack("⚠️ Парсинг вже запущено! Дочекайтесь завершення.", ft.Colors.ORANGE_700)
                    # Also bring the overlay into view if hidden
                    _ov = getattr(page, "_prs_overlay", None)
                    if _ov and not _ov.visible:
                        _ov.visible = True
                        with contextlib.suppress(Exception):
                            page.update()
                    return
                if page:
                    # run_task requires a coroutine FUNCTION (it calls
                    # inspect.iscoroutinefunction on the arg with no
                    # fallback) — passing the already-invoked _run_async(page)
                    # coroutine object here raised TypeError before ever
                    # scheduling it.
                    page.run_task(_run_async, page)

            return _handler

        status_color = (
            ft.Colors.ORANGE_700 if bool(state.get("running")) else ft.Colors.GREEN_700
        )

        return self._section_card(
            self.t("extensions.launch_title", "Запуск"),
            self.t("extensions.launch_subtitle", "Тестовий запуск без розкладу: повний або окремо GSM/E-Katalog."),
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.FilledButton(
                                self.t("gsm.menu.full_parse", "Повний парсинг (ГСМ + ЄК)"),
                                icon=ft.Icons.PLAY_ARROW,
                                on_click=_run_mode_click("full"),
                                disabled=bool(state.get("running")),
                            ),
                            ft.OutlinedButton(
                                self.t("gsm.menu.gsm_only", "Тільки GSM Arena"),
                                icon=ft.Icons.PLAYLIST_PLAY,
                                on_click=_run_mode_click("gsm"),
                                disabled=bool(state.get("running")),
                            ),
                            ft.OutlinedButton(
                                self.t("gsm.menu.ek_only", "Тільки E-Katalog"),
                                icon=ft.Icons.PLAYLIST_ADD_CHECK,
                                on_click=_run_mode_click("ekatalog"),
                                disabled=bool(state.get("running")),
                            ),
                            ft.Text(
                                self.t("extensions.total_results", "Всього результатів: {n}").format(n=len(devices)),
                                size=12,
                                color=ft.Colors.BLUE_GREY_700,
                            ),
                        ],
                        spacing=10,
                        wrap=True,
                    ),
                    ft.Container(
                        border=ft.border.all(1, ft.Colors.BLUE_GREY_200),
                        border_radius=8,
                        padding=10,
                        content=ft.Column(
                            [
                                ft.Row(
                                    [
                                        ft.ProgressRing(
                                            visible=bool(state.get("running")),
                                            width=18,
                                            height=18,
                                            stroke_width=2,
                                        ),
                                        ft.Text(
                                            str(state.get("status") or ""),
                                            color=status_color,
                                        ),
                                    ],
                                    spacing=8,
                                ),
                                ft.Text(
                                    f"Старт: {state.get('started_at') or '-'} | Фініш: {state.get('finished_at') or '-'}",
                                    size=11,
                                    color=ft.Colors.BLUE_GREY_700,
                                ),
                                ft.Text(
                                    str(state.get("last_result") or ""),
                                    size=11,
                                    color=ft.Colors.BLUE_GREY_800,
                                ),
                            ],
                            spacing=6,
                            tight=True,
                        ),
                    ),
                    ft.Container(
                        height=400,
                        content=ft.DataTable(
                            columns=[
                                ft.DataColumn(ft.Text(self.t("extensions.gsm_log.col.source", "Джерело"))),
                                ft.DataColumn(ft.Text(self.t("extensions.gsm_log.col.brand", "Бренд"))),
                                ft.DataColumn(ft.Text(self.t("extensions.gsm_log.col.type", "Тип"))),
                                ft.DataColumn(ft.Text(self.t("extensions.gsm_log.col.model", "Модель"))),
                                ft.DataColumn(ft.Text(self.t("extensions.gsm_log.col.last_seen", "Остання дата"))),
                            ],
                            rows=result_rows
                            or [
                                ft.DataRow(
                                    cells=[
                                        ft.DataCell(
                                            ft.Text(
                                                "Нема даних",
                                                color=ft.Colors.BLUE_GREY_600,
                                            )
                                        )
                                        for _ in range(5)
                                    ]
                                )
                            ],
                        ),
                        border=ft.border.all(1, ft.Colors.BLUE_GREY_200),
                        border_radius=8,
                    ),
                ],
                spacing=12,
            ),
        )

    def _build_schedule_tab(self):
        """Build schedule configuration tab for automated parser execution.

        The VPS daemon (gsm_parser_scheduler.py --daemon) compares run_times
        against datetime.utcnow(), so the DB storage format stays UTC "HH:MM"
        strings — only this UI's INPUT/DISPLAY is in Kyiv time now (that's
        what was actually confusing: the UI was labeled "(UTC)" but nothing
        stopped an admin from typing what they meant as Kyiv time, and the
        desktop's own now-removed local scheduler compared the same strings
        against local time instead of UTC — two different silent
        interpretations of the same stored value)."""
        from zoneinfo import ZoneInfo

        _KYIV_TZ = ZoneInfo("Europe/Kyiv")

        def _utc_to_kyiv(hhmm: str) -> str:
            try:
                h, m = map(int, str(hhmm).strip().split(":"))
                anchor = datetime.now(_KYIV_TZ).date()
                dt_utc = datetime(anchor.year, anchor.month, anchor.day, h, m, tzinfo=ZoneInfo("UTC"))
                dt_kyiv = dt_utc.astimezone(_KYIV_TZ)
                return f"{dt_kyiv.hour:02d}:{dt_kyiv.minute:02d}"
            except Exception:
                return str(hhmm)

        def _kyiv_to_utc(hhmm: str) -> str:
            try:
                h, m = map(int, str(hhmm).strip().split(":"))
                anchor = datetime.now(_KYIV_TZ).date()
                dt_kyiv = datetime(anchor.year, anchor.month, anchor.day, h, m, tzinfo=_KYIV_TZ)
                dt_utc = dt_kyiv.astimezone(ZoneInfo("UTC"))
                return f"{dt_utc.hour:02d}:{dt_utc.minute:02d}"
            except Exception:
                return str(hhmm)

        def _fmt_next_run_kyiv(iso_str) -> str:
            if not iso_str:
                return ""
            try:
                dt_utc = datetime.fromisoformat(str(iso_str)).replace(tzinfo=ZoneInfo("UTC"))
                dt_kyiv = dt_utc.astimezone(_KYIV_TZ)
                return dt_kyiv.strftime("%Y-%m-%d %H:%M") + " (Київ)"
            except Exception:
                return str(iso_str)

        schedule_cfg = self.db.get_gsm_parser_schedule()
        enabled = schedule_cfg.get("enabled", False)
        run_times_utc = schedule_cfg.get("run_times", ["02:00", "14:00"])
        run_times_kyiv = {_utc_to_kyiv(t) for t in run_times_utc}
        next_run = schedule_cfg.get("next_run")

        # Time selection checkboxes, labeled in Kyiv time, 30-minute steps —
        # 8 columns × 6 rows = 48 slots (00:00..23:30).
        time_checks = {}
        grid_cols = []
        for col_idx in range(8):
            col_cbs = []
            for row_idx in range(6):
                slot = col_idx * 6 + row_idx
                hour, minute = divmod(slot * 30, 60)
                time_str = f"{hour:02d}:{minute:02d}"
                cb = ft.Checkbox(
                    label=time_str,
                    value=time_str in run_times_kyiv,
                )
                time_checks[time_str] = cb
                col_cbs.append(cb)
            grid_cols.append(ft.Column(col_cbs, spacing=2, tight=True))

        times_col = ft.Row(grid_cols, spacing=12)

        enable_cb = ft.Checkbox(
            label="Увімкнути автоматичний запуск",
            value=enabled,
        )

        next_run_lbl = ft.Text(
            f"Наступний: {_fmt_next_run_kyiv(next_run) or 'не розраховується'}",
            size=11,
            color=ft.Colors.GREEN_700 if next_run else ft.Colors.BLUE_GREY_700,
        )

        def save_schedule(_):
            selected_kyiv = [t for t, cb in time_checks.items() if cb.value]
            selected_utc = sorted({_kyiv_to_utc(t) for t in selected_kyiv})
            ok = self.db.set_gsm_parser_schedule(
                enabled=bool(enable_cb.value),
                run_times=selected_utc or [_kyiv_to_utc("02:00"), _kyiv_to_utc("14:00")],
            )
            if ok:
                with contextlib.suppress(Exception):
                    _nr = (self.db.get_gsm_parser_schedule() or {}).get("next_run")
                    next_run_lbl.value = f"Наступний: {_fmt_next_run_kyiv(_nr) or 'не розраховується'}"
                    next_run_lbl.color = ft.Colors.GREEN_700 if _nr else ft.Colors.BLUE_GREY_700
                    next_run_lbl.update()
                self._show_saved_dialog("Розклад збережено. Виконується на ВПС (незалежно від того, чи відкрита програма).")
            else:
                self._show_snack("Не вдалося зберегти розклад", "red")

        svc_cfg = self.db.get_parser_service_config()
        vps_url_tf = ft.TextField(
            label="Адреса VPS-сервісу парсера",
            value=str(svc_cfg.get("vps_service_url") or ""),
            hint_text="http://31.131.18.228:8765",
            width=320,
            dense=True,
            border_radius=8,
        )
        vps_key_tf = ft.TextField(
            label="API-ключ",
            value=str(svc_cfg.get("vps_api_key") or ""),
            width=280,
            dense=True,
            border_radius=8,
            password=True,
            can_reveal_password=True,
        )

        def save_vps_config(_):
            ok = self.db.set_parser_service_config(
                vps_url_tf.value or "", vps_key_tf.value or ""
            )
            if ok:
                self._show_saved_dialog("Адресу VPS-сервісу збережено.")
            else:
                self._show_snack("Не вдалося зберегти", "red")

        def show_crontab(_):
            selected_times = [t for t, cb in time_checks.items() if cb.value]
            crontab_lines = []
            for run_time in selected_times or ["02:00", "14:00"]:
                try:
                    h, m = map(int, run_time.split(":"))
                    crontab_lines.append(
                        f"{m} {h} * * * cd /path/to/app && /usr/bin/python3 gsm_parser_scheduler.py --run-once"
                    )
                except Exception:
                    pass

            crontab_text = "\n".join(crontab_lines) if crontab_lines else "# No times"
            _page = self._page()

            def _crontab_help(_):
                from views.help_utils import show_modal_help
                show_modal_help(_page, "Crontab для VPS", [
                    self.t("extensions.crontab_help.step1", "1. Скопіюйте рядки crontab і вставте на VPS-сервері."),
                    self.t("extensions.crontab_help.step2", "2. За потреби відредагуйте шлях до Python і директорії додатку."),
                ])

            dlg = ft.AlertDialog(
                modal=True,
                title=ft.Row([
                    ft.Text("Crontab для VPS", expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_crontab_help),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
                content=ft.TextField(
                    multiline=True,
                    min_lines=6,
                    read_only=True,
                    value=crontab_text,
                    border_radius=8,
                ),
                actions=[
                    ft.TextButton("Закрити", on_click=lambda _: _page.pop_dialog() if _page else None),
                ],
            )
            if _page:
                _page.show_dialog(dlg)

        return ft.Column(
            [
                enable_cb,
                ft.Text("Оберіть час запуску (за Києвом, крок 30 хв):", size=12, weight="bold"),
                ft.Container(
                    border=ft.border.all(1, ft.Colors.BLUE_GREY_200),
                    border_radius=8,
                    padding=12,
                    content=times_col,
                ),
                next_run_lbl,
                ft.Row(
                    [
                        ft.FilledButton(
                            "Зберегти",
                            icon=ft.Icons.SAVE,
                            on_click=save_schedule,
                        ),
                        ft.OutlinedButton(
                            "Crontab для VPS",
                            icon=ft.Icons.CODE,
                            on_click=show_crontab,
                        ),
                    ],
                    spacing=10,
                ),
                ft.Divider(height=24),
                ft.Text(
                    "VPS-сервіс парсера (запуск відбувається виключно тут — "
                    "і автоматичний, і ручний з кнопки «Запустити»):",
                    size=12,
                    weight="bold",
                ),
                ft.Row([vps_url_tf, vps_key_tf], spacing=10, wrap=True),
                ft.FilledButton(
                    "Зберегти адресу сервісу",
                    icon=ft.Icons.SAVE,
                    on_click=save_vps_config,
                ),
            ],
            spacing=12,
        )

    def _build_settings_tab(self):
        draft = self._get_gsm_draft()
        settings = draft.get("settings", {})
        comparison = draft.get("comparison", {})
        devices = settings.get("devices", {})
        delays = settings.get("delays", {})
        self._gsm_form_refs = {
            "gsm_rows": [],
            "ekatalog_rows": [],
            "delays": {},
            "change_retention_days": None,
            "comparison": {},
            "comparison_rules": [],
        }

        change_retention_days_f = ft.TextField(
            label="Зберігання змін (днів)",
            value=str(settings.get("change_retention_days", 365)),
            width=280,
            dense=True,
            keyboard_type=ft.KeyboardType.NUMBER,
            hint_text="Скільки днів тримати історію змін цін та інших параметрів",
        )
        self._gsm_form_refs["change_retention_days"] = change_retention_days_f

        gsm_rows_controls = []
        for idx, row in enumerate(devices.get("gsm_arena", [])):
            refs = {
                "brand": ft.TextField(
                    label="Бренд",
                    value=str(row.get("brand") or ""),
                    width=180,
                    dense=True,
                ),
                "url": ft.TextField(
                    label="Посилання",
                    value=str(row.get("url") or ""),
                    width=360,
                    dense=True,
                ),
                "last_device": ft.TextField(
                    label="Останній",
                    value=str(row.get("last_device") or ""),
                    width=230,
                    dense=True,
                ),
            }
            self._gsm_form_refs["gsm_rows"].append(refs)
            gsm_rows_controls.append(
                ft.Container(
                    border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
                    border_radius=12,
                    padding=10,
                    bgcolor=_ALT,
                    content=ft.Row(
                        [
                            refs["brand"],
                            refs["url"],
                            refs["last_device"],
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                tooltip="Видалити рядок",
                                icon_color=ft.Colors.RED_400,
                                on_click=self._remove_device_row("gsm_arena", idx),
                            ),
                        ],
                        wrap=True,
                        spacing=10,
                    ),
                )
            )

        ekatalog_rows_controls = []
        for idx, row in enumerate(devices.get("ekatalog", [])):
            refs = {
                "device_type": ft.Dropdown(
                    label="Тип пристрою",
                    width=170,
                    dense=True,
                    value=str(row.get("device_type") or "Смартфон"),
                    options=[
                        ft.dropdown.Option("Смартфон"),
                        ft.dropdown.Option("Планшет"),
                        ft.dropdown.Option("Годинник"),
                    ],
                ),
                "brand": ft.TextField(
                    label="Бренд",
                    value=str(row.get("brand") or ""),
                    width=170,
                    dense=True,
                ),
                "url": ft.TextField(
                    label="Посилання",
                    value=str(row.get("url") or ""),
                    width=330,
                    dense=True,
                ),
                "last_device": ft.TextField(
                    label="Останній",
                    value=str(row.get("last_device") or ""),
                    width=210,
                    dense=True,
                ),
            }
            self._gsm_form_refs["ekatalog_rows"].append(refs)
            ekatalog_rows_controls.append(
                ft.Container(
                    border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
                    border_radius=12,
                    padding=10,
                    bgcolor=_ALT,
                    content=ft.Row(
                        [
                            refs["device_type"],
                            refs["brand"],
                            refs["url"],
                            refs["last_device"],
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                tooltip="Видалити рядок",
                                icon_color=ft.Colors.RED_400,
                                on_click=self._remove_device_row("ekatalog", idx),
                            ),
                        ],
                        wrap=True,
                        spacing=10,
                    ),
                )
            )

        delay_meta = [
            ("gsm_brand_page_min_sec", "GSM Arena: сторінка бренду від, сек"),
            ("gsm_brand_page_max_sec", "GSM Arena: сторінка бренду до, сек"),
            ("gsm_device_page_min_sec", "GSM Arena: карточка пристрою від, сек"),
            ("gsm_device_page_max_sec", "GSM Arena: карточка пристрою до, сек"),
            ("ekatalog_search_min_sec", "E-Katalog: пошук від, сек"),
            ("ekatalog_search_max_sec", "E-Katalog: пошук до, сек"),
            ("ekatalog_card_min_sec", "E-Katalog: карточка від, сек"),
            ("ekatalog_card_max_sec", "E-Katalog: карточка до, сек"),
            ("telegram_send_delay_sec", "Telegram: пауза між повідомленнями, сек"),
            ("retry_backoff_base_sec", "Базовий retry/backoff, сек"),
        ]
        delay_controls = []
        for key, label in delay_meta:
            control = ft.TextField(
                label=label,
                value=str(delays.get(key, "")),
                width=250,
                dense=True,
                keyboard_type=ft.KeyboardType.NUMBER,
            )
            self._gsm_form_refs["delays"][key] = control
            delay_controls.append(control)

        comparison_fields = {
            "remove_4g5g": ft.Checkbox(
                label="Прибирати 4G/5G/LTE з назв",
                value=bool(comparison.get("remove_4g5g", True)),
            ),
            "remove_punctuation": ft.Checkbox(
                label="Прибирати пунктуацію",
                value=bool(comparison.get("remove_punctuation", False)),
            ),
            "case_insensitive": ft.Checkbox(
                label="Порівнювати без регістру",
                value=bool(comparison.get("case_insensitive", True)),
            ),
            "exclude_words": ft.TextField(
                label="Слова для ігнору (через кому)",
                value=", ".join(
                    [
                        str(w).strip()
                        for w in (comparison.get("exclude_words") or [])
                        if str(w).strip()
                    ]
                ),
                width=540,
                dense=True,
                multiline=True,
                min_lines=2,
                max_lines=3,
                hint_text="Напр: 4g, 5g, lte, dual sim",
            ),
        }
        self._gsm_form_refs["comparison"] = comparison_fields

        comparison_rule_cards = []
        for idx, row in enumerate(comparison.get("custom_rules") or []):
            refs = {
                "enabled": ft.Checkbox(
                    label="Активне", value=bool(row.get("enabled", True))
                ),
                "pattern": ft.TextField(
                    label="Regex pattern",
                    value=str(row.get("pattern") or ""),
                    width=320,
                    dense=True,
                ),
                "replacement": ft.TextField(
                    label="Replacement",
                    value=str(row.get("replacement") or ""),
                    width=220,
                    dense=True,
                ),
            }
            self._gsm_form_refs["comparison_rules"].append(refs)
            comparison_rule_cards.append(
                ft.Container(
                    border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
                    border_radius=12,
                    padding=10,
                    bgcolor=_ALT,
                    content=ft.Row(
                        [
                            refs["enabled"],
                            refs["pattern"],
                            refs["replacement"],
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                tooltip="Видалити правило",
                                icon_color=ft.Colors.RED_400,
                                on_click=self._remove_comparison_rule(idx),
                            ),
                        ],
                        wrap=True,
                        spacing=10,
                    ),
                )
            )

        return ft.Column(
            [
                self._section_card(
                    self.t("extensions.parser_settings_title", "Налаштування парсингу"),
                    self.t("extensions.parser_settings_subtitle", "База для майбутньої перебудови парсера: бренди, посилання, останні пристрої та антибан-паузи."),
                    ft.Column(
                        [
                            ft.ExpansionTile(
                                title=ft.Text(self.t("extensions.devices_tile", "Пристрої"), weight=ft.FontWeight.W_700),
                                controls_padding=ft.padding.only(
                                    left=8, right=8, bottom=12
                                ),
                                expanded=True,
                                controls=[
                                    ft.Text(
                                        "GSM Arena: бренд, посилання на сторінку бренду і останній пристрій, після якого парсер має зупинитись.",
                                        size=12,
                                        color=ft.Colors.BLUE_GREY_700,
                                    ),
                                    ft.Column(gsm_rows_controls, spacing=8),
                                    ft.Row(
                                        [
                                            ft.OutlinedButton(
                                                "Додати бренд GSM Arena",
                                                icon=ft.Icons.ADD,
                                                on_click=self._add_device_row(
                                                    "gsm_arena"
                                                ),
                                            )
                                        ],
                                        spacing=10,
                                    ),
                                    ft.Divider(height=18),
                                    ft.Text(
                                        "E-Katalog: тип пристрою, бренд, посилання на сторінку новинок і останній пристрій для зупинки.",
                                        size=12,
                                        color=ft.Colors.BLUE_GREY_700,
                                    ),
                                    ft.Column(ekatalog_rows_controls, spacing=8),
                                    ft.Row(
                                        [
                                            ft.OutlinedButton(
                                                "Додати бренд E-Katalog",
                                                icon=ft.Icons.ADD,
                                                on_click=self._add_device_row(
                                                    "ekatalog"
                                                ),
                                            )
                                        ],
                                        spacing=10,
                                    ),
                                ],
                            ),
                            ft.Container(
                                padding=ft.padding.only(
                                    left=8, right=8, top=4, bottom=4
                                ),
                                content=ft.Row(
                                    [
                                        change_retention_days_f,
                                        ft.Text(
                                            "Налаштовує, скільки днів зберігати зміни (ціна та інші оновлення).",
                                            size=12,
                                            color=ft.Colors.BLUE_GREY_700,
                                        ),
                                    ],
                                    wrap=True,
                                    spacing=10,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                            ),
                            ft.ExpansionTile(
                                title=ft.Text(
                                    "Пауза та антибан", weight=ft.FontWeight.W_700
                                ),
                                controls_padding=ft.padding.only(
                                    left=8, right=8, bottom=12
                                ),
                                expanded=True,
                                controls=[
                                    ft.Text(
                                        "Налаштуйте діапазони затримок для різних сайтів, щоб зменшити ризик блокувань.",
                                        size=12,
                                        color=ft.Colors.BLUE_GREY_700,
                                    ),
                                    ft.Row(
                                        delay_controls,
                                        wrap=True,
                                        spacing=10,
                                        run_spacing=10,
                                    ),
                                ],
                            ),
                            ft.ExpansionTile(
                                title=ft.Text(
                                    "Правила порівняння назв",
                                    weight=ft.FontWeight.W_700,
                                ),
                                controls_padding=ft.padding.only(
                                    left=8, right=8, bottom=12
                                ),
                                expanded=True,
                                controls=[
                                    ft.Text(
                                        "Керуйте нормалізацією назв і regex-правилами для точнішого match між GSM Arena та E-Katalog.",
                                        size=12,
                                        color=ft.Colors.BLUE_GREY_700,
                                    ),
                                    ft.Row(
                                        [
                                            comparison_fields["remove_4g5g"],
                                            comparison_fields["remove_punctuation"],
                                            comparison_fields["case_insensitive"],
                                        ],
                                        wrap=True,
                                        spacing=12,
                                    ),
                                    comparison_fields["exclude_words"],
                                    ft.Row(
                                        [
                                            ft.OutlinedButton(
                                                "Додати regex правило",
                                                icon=ft.Icons.ADD,
                                                on_click=self._add_comparison_rule,
                                            )
                                        ],
                                        spacing=10,
                                    ),
                                    ft.Column(comparison_rule_cards, spacing=8),
                                ],
                            ),
                        ],
                        spacing=12,
                    ),
                    action=ft.FilledButton(
                        "Зберегти", icon=ft.Icons.SAVE, on_click=self._save_gsm_config
                    ),
                )
            ],
            spacing=16,
        )

    def _build_results_tab(self):
        """Tab 1 — show parser results (same view for user, admin, superadmin)."""
        from views.gsm_parser_view import GsmParserView
        company_id = self.active_company_id or self.user_data.get("company_id") or "__global__"
        view = GsmParserView(
            self.content_area,
            self.user_data,
            self.db,
            active_company_id=company_id,
            is_superadmin=self.is_superadmin,
        )
        return view._build_results_content()

    def _build_permissions_tab(self):
        """Tab 2 — list of companies with enable/disable toggles."""
        companies = self._companies()
        if not companies:
            return ft.Text("Немає компаній", color=ft.Colors.BLUE_GREY_600)

        states = self.db.get_companies_gsm_parser_state(
            [c.get("id") for c in companies if c.get("id")]
        )
        enabled_count = sum(
            1
            for c in companies
            if bool(states.get(c.get("id"), {}).get("effective_enabled"))
        )

        rows = []
        for company in companies:
            cid = company.get("id")
            state = states.get(cid, {})
            sa_on = bool(state.get("sa_enabled"))
            rows.append(
                ft.Container(
                    bgcolor=ft.Colors.WHITE,
                    border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
                    border_radius=12,
                    padding=ft.padding.symmetric(horizontal=16, vertical=10),
                    content=ft.Row(
                        [
                            ft.Container(
                                width=36,
                                height=36,
                                border_radius=10,
                                bgcolor=_OK_BG if sa_on else ft.Colors.BLUE_GREY_50,
                                alignment=ft.alignment.Alignment(0, 0),
                                content=ft.Icon(
                                    ft.Icons.BUSINESS,
                                    size=18,
                                    color=_OK_DK if sa_on else ft.Colors.BLUE_GREY_400,
                                ),
                            ),
                            ft.Text(
                                company.get("name") or cid or "—",
                                expand=True,
                                weight=ft.FontWeight.W_600,
                                size=14,
                            ),
                            ft.Container(
                                padding=ft.padding.symmetric(horizontal=10, vertical=4),
                                border_radius=999,
                                bgcolor=_OK_BG if sa_on else ft.Colors.BLUE_GREY_50,
                                content=ft.Text(
                                    self.t("common.enabled", "Увімкнено") if sa_on else self.t("common.disabled", "Вимкнено"),
                                    size=12,
                                    color=_OK_DK if sa_on else ft.Colors.BLUE_GREY_500,
                                    weight=ft.FontWeight.W_600,
                                ),
                            ),
                            ft.Switch(
                                value=sa_on,
                                active_color=_OK_DK,
                                on_change=lambda e, cid_=cid: self._set_gsm_company_enabled(
                                    cid_, bool(e.control.value)
                                ),
                            ),
                        ],
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )

        # ---- Per-user access: case/glass compatibility editing + Xiaomi sizes ----
        _PERM_DEFS = [
            ("compat_case", "Чохли"),
            ("compat_glass", "Скло"),
            ("xiaomi_sizes", "Розміри Xiaomi"),
            ("run_parser", "Запуск парсингу"),
            ("manage_devices", "Видалення/виключення пристроїв"),
            ("region_availability", "Наявність по регіонах"),
            ("manual_gsm_link", "Ручні посилання ГСМ Арена"),
        ]
        _SA_ROLES = {"admin", "админ", "адмін", "superadmin", "суперадмин", "суперадмін"}

        if not self._gsm_perms_company_id and companies:
            self._gsm_perms_company_id = companies[0].get("id")

        managers_panel_ref = ft.Ref[ft.Column]()

        def _build_managers_rows(company_id):
            if not company_id:
                return [ft.Text("Немає компаній", color=ft.Colors.BLUE_GREY_600)]
            managers = self.db.get_company_managers(company_id) or []
            perms_map = self.db.get_company_managers_gsm_permissions(company_id) or {}
            mgr_rows = []
            for m in managers:
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                role_norm = str(m.get("role") or "").strip().lower()
                if role_norm in _SA_ROLES:
                    continue  # superadmins/company-admins already have full access
                mid = str(m.get("id"))
                granted = perms_map.get(mid, set())

                def _on_perm_change(e, manager_id=mid, perm_key=None, cid=company_id):
                    ok = self.db.set_manager_gsm_permission(
                        manager_id, cid, perm_key, bool(e.control.value)
                    )
                    self._show_snack(
                        self.t("extensions.access_updated", "Доступ оновлено") if ok else self.t("extensions.err.save", "Помилка збереження"),
                        ft.Colors.GREEN if ok else ft.Colors.RED,
                    )

                checkboxes = [
                    ft.Checkbox(
                        label=label,
                        value=perm_key in granted,
                        on_change=(
                            lambda e, mid_=mid, pk=perm_key: _on_perm_change(
                                e, mid_, pk
                            )
                        ),
                    )
                    for perm_key, label in _PERM_DEFS
                ]
                mgr_rows.append(
                    ft.Container(
                        bgcolor=ft.Colors.WHITE,
                        border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
                        border_radius=10,
                        padding=ft.padding.symmetric(horizontal=14, vertical=8),
                        content=ft.Row(
                            [
                                ft.Column(
                                    [
                                        ft.Text(
                                            m.get("full_name") or m.get("email") or mid,
                                            weight=ft.FontWeight.W_600,
                                            size=13,
                                        ),
                                        ft.Text(
                                            m.get("role") or "",
                                            size=11,
                                            color=ft.Colors.BLUE_GREY_500,
                                        ),
                                    ],
                                    spacing=1,
                                    expand=True,
                                    tight=True,
                                ),
                                *checkboxes,
                            ],
                            spacing=14,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    )
                )
            return mgr_rows or [
                ft.Text("Немає користувачів у цій компанії", color=ft.Colors.BLUE_GREY_600)
            ]

        def _on_perms_company_change(e):
            self._gsm_perms_company_id = e.control.value
            if managers_panel_ref.current:
                managers_panel_ref.current.controls = _build_managers_rows(
                    self._gsm_perms_company_id
                )
                with contextlib.suppress(Exception):
                    managers_panel_ref.current.update()

        company_dd = ft.Dropdown(
            label="Компанія",
            value=self._gsm_perms_company_id,
            options=[
                ft.dropdown.Option(key=c.get("id"), text=c.get("name") or c.get("id"))
                for c in companies
                if c.get("id")
            ],
            width=320,
            dense=True,
            on_select=_on_perms_company_change,
        )

        gsm_perms_section = ft.Column(
            [
                ft.Text(
                    "Доступ користувачів до сумісності та розмірів Xiaomi",
                    size=15,
                    weight=ft.FontWeight.W_700,
                ),
                ft.Text(
                    "Дозвольте співробітнику редагувати сумісність чохлів, скла"
                    " або розміри Xiaomi окремо, без прав суперадміна. Доступ"
                    " можна відкликати в будь-який момент.",
                    size=12,
                    color=ft.Colors.BLUE_GREY_600,
                ),
                company_dd,
                ft.Column(
                    _build_managers_rows(self._gsm_perms_company_id),
                    ref=managers_panel_ref,
                    spacing=8,
                ),
            ],
            spacing=10,
        )

        return ft.Column(
            [
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=14, vertical=8),
                    border_radius=999,
                    bgcolor=_SOFT,
                    content=ft.Text(
                        f"Доступ відкрито для {enabled_count} з {len(companies)} компаній",
                        color=_OK_DK,
                        weight=ft.FontWeight.W_600,
                        size=13,
                    ),
                ),
                ft.Column(rows, spacing=8),
                ft.Divider(height=28),
                gsm_perms_section,
            ],
            spacing=12,
        )

    def _build_brands_settings_tab(self):
        """Tab 3 — brand configuration table (from gsm_parser_brands DB)."""
        brands = self._get_brands_draft()
        self._brand_form_refs = []

        _gsm_settings = self._get_gsm_draft().get("settings") or {}
        self._gsm_recent_count_ref = ft.TextField(
            label="Кількість товару GSM Arena",
            value=str(_gsm_settings.get("gsm_recent_count", 15)),
            width=280,
            dense=True,
            border_radius=8,
            keyboard_type=ft.KeyboardType.NUMBER,
            hint_text="Скільки перших пристроїв бренду перевіряти, якщо бренд вже є в БД",
        )
        recent_count_row = ft.Row(
            [
                self._gsm_recent_count_ref,
                ft.Text(
                    "— для вже відомих брендів парсер перевіряє тільки стільки"
                    " перших (найновіших) позицій з лістингу GSM Arena замість"
                    " усього каталогу.",
                    size=11,
                    color=ft.Colors.BLUE_GREY_600,
                    expand=True,
                ),
            ],
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        self._gsm_shops_appeared_max_age_months_ref = ft.TextField(
            label=self.t(
                "extensions.gsm_shops_appeared_max_age", "Ліміт віку товару, міс."
            ),
            value=str(_gsm_settings.get("shops_appeared_max_age_months", 8)),
            width=280,
            dense=True,
            border_radius=8,
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        shops_appeared_age_row = ft.Row(
            [
                self._gsm_shops_appeared_max_age_months_ref,
                ft.Text(
                    self.t(
                        "extensions.gsm_shops_appeared_max_age_hint",
                        "— \"З'явились в продажу\" (бокова панель і Telegram) враховує лише"
                        " товари, чия дата публікації на E-Katalog (\"Дата додавання\") не"
                        " старша ніж стільки місяців тому.",
                    ),
                    size=11,
                    color=ft.Colors.BLUE_GREY_600,
                    expand=True,
                ),
            ],
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        self._gsm_ek_on_sale_statuses_ref = ft.TextField(
            label=self.t("extensions.ek_on_sale_statuses", "Статус(и) \"у продажу\" на E-Katalog"),
            value=str(_gsm_settings.get("ek_on_sale_statuses") or "Продаж"),
            width=280,
            dense=True,
            border_radius=8,
            hint_text="Продаж, Продаж! Новинка",
        )
        ek_on_sale_statuses_row = ft.Row(
            [
                self._gsm_ek_on_sale_statuses_ref,
                ft.Text(
                    self.t(
                        "extensions.ek_on_sale_statuses_hint",
                        "— через кому. \"З'явились в продажу\" спрацьовує ОДИН раз на пристрій:"
                        " коли його статус на E-Katalog вперше стає одним із цих значень"
                        " (порівняння за початком рядка, без урахування регістру). Якщо пристрій"
                        " вже колись був у продажу, повторне зникнення/повернення в наявність"
                        " більше не сповіщає.",
                    ),
                    size=11,
                    color=ft.Colors.BLUE_GREY_600,
                    expand=True,
                ),
            ],
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        _COL_W = self._BRAND_COL_W
        _HDR_STYLE = dict(size=11, weight=ft.FontWeight.W_700, color=ft.Colors.BLUE_GREY_600)

        header = ft.Container(
            bgcolor=ft.Colors.BLUE_GREY_50,
            border_radius=ft.border_radius.only(top_left=10, top_right=10),
            padding=ft.padding.symmetric(horizontal=10, vertical=8),
            content=ft.Row(
                [
                    ft.Container(ft.Text(self.t("extensions.brands_table.col.enabled", "Увімк."), **_HDR_STYLE), width=40),
                    ft.Container(ft.Text(self.t("extensions.brands_table.col.brand", "Бренд"), **_HDR_STYLE), width=_COL_W["brand"]),
                    ft.Container(ft.Text(self.t("extensions.brands_table.col.gsm_url", "Посилання GsmArena"), **_HDR_STYLE), width=_COL_W["gsm_url"]),
                    ft.Container(ft.Text(self.t("extensions.brands_table.col.last_item", "Останній предмет парсингу"), **_HDR_STYLE), width=_COL_W["last_item"]),
                    ft.Container(ft.Text(self.t("extensions.brands_table.col.ek_phones", "ЄК Смартфон"), **_HDR_STYLE), width=_COL_W["ek_phones"]),
                    ft.Container(ft.Text(self.t("extensions.brands_table.col.ek_tablets", "ЄК Планшети"), **_HDR_STYLE), width=_COL_W["ek_tablets"]),
                    ft.Container(ft.Text(self.t("extensions.brands_table.col.ek_watches", "ЄК Годинники"), **_HDR_STYLE), width=_COL_W["ek_watches"]),
                    ft.Container(width=40),
                ],
                spacing=8,
            ),
        )

        row_controls = []
        for idx, row in enumerate(brands):
            refs = self._make_brand_refs(row)
            self._brand_form_refs.append(refs)
            row_bg = ft.Colors.WHITE if idx % 2 == 0 else _ALT
            row_controls.append(self._make_brand_row_ctrl(idx, refs, row_bg))

        _EMPTY_BRANDS_TAG = "brands_empty_placeholder"
        empty_row = ft.Container(
            data=_EMPTY_BRANDS_TAG,
            padding=ft.padding.symmetric(horizontal=16, vertical=20),
            content=ft.Text(
                "Немає брендів. Натисніть «Додати бренд».",
                color=ft.Colors.BLUE_GREY_400,
                size=13,
            ),
        )

        table_body = ft.Column(
            row_controls if row_controls else [empty_row],
            spacing=0,
            tight=True,
            scroll=ft.ScrollMode.AUTO,
        )
        self._brands_table_body_ref = table_body

        _height_container = ft.Container(
            content=table_body,
            height=min(50 + len(brands) * 62, 500) if brands else 80,
        )
        self._brands_table_height_ref = _height_container

        brands_content = self._section_card(
            self.t("extensions.brands_title", "Бренди та посилання"),
            self.t("extensions.brands_subtitle", "Налаштуйте бренди для парсингу: GSM Arena URL, останній відомий пристрій та посилання на Єкаталог."),
            ft.Column(
                [
                    recent_count_row,
                    ft.Divider(height=8, color=ft.Colors.BLUE_GREY_50),
                    shops_appeared_age_row,
                    ft.Divider(height=8, color=ft.Colors.BLUE_GREY_50),
                    ek_on_sale_statuses_row,
                    ft.Divider(height=14, color=ft.Colors.BLUE_GREY_50),
                    ft.Container(
                        border=ft.border.all(1, ft.Colors.BLUE_GREY_200),
                        border_radius=12,
                        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                        content=ft.Column(
                            [
                                ft.Row(
                                    [header],
                                    scroll=ft.ScrollMode.AUTO,
                                ),
                                _height_container,
                            ],
                            spacing=0,
                            tight=True,
                        ),
                    ),
                    ft.Row(
                        [
                            ft.OutlinedButton(
                                "Додати бренд",
                                icon=ft.Icons.ADD,
                                on_click=self._add_brand_row,
                            ),
                        ],
                        spacing=10,
                    ),
                ],
                spacing=12,
            ),
            action=ft.FilledButton(
                "Зберегти",
                icon=ft.Icons.SAVE,
                on_click=self._save_brands,
            ),
        )

        # ── Розклад ──────────────────────────────────────────────────────────
        schedule_content = self._build_schedule_tab()

        # ── Telegram toggle ───────────────────────────────────────────────────
        draft = self._get_gsm_draft()
        _tg_draft = draft.get("telegram") or {}
        _tg_enabled_init = bool(_tg_draft.get("tg_enabled", True))
        _win_start_init = str(_tg_draft.get("report_window_start") or "21:00").strip() or "21:00"
        _win_end_init = str(_tg_draft.get("report_window_end") or "07:59").strip() or "07:59"
        _send_time_init = str(_tg_draft.get("report_send_time") or "08:00").strip() or "08:00"

        tg_enabled_sw = ft.Switch(
            value=_tg_enabled_init,
            label="Надсилати сповіщення в Telegram після парсингу",
            active_color=_OK_DK,
        )

        bot_token_field = ft.TextField(
            label=self.t("extensions.tg.bot_token", "Bot token"),
            value=str(_tg_draft.get("bot_token") or ""),
            can_reveal_password=True,
            password=True,
            width=320,
            dense=True,
        )
        chat_id_field = ft.TextField(
            label=self.t("extensions.tg.chat_id", "Chat ID"),
            value=str(_tg_draft.get("chat_id") or ""),
            width=200,
            dense=True,
        )
        chat_link_field = ft.TextField(
            label=self.t("extensions.tg.chat_link", "Посилання на чат"),
            value=str(_tg_draft.get("chat_link") or ""),
            width=320,
            dense=True,
        )
        spreadsheet_url_field = ft.TextField(
            label=self.t("extensions.tg.spreadsheet_url", "Посилання на таблицю"),
            value=str(_tg_draft.get("spreadsheet_url") or ""),
            width=320,
            dense=True,
        )

        _HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

        def _norm_hhmm(value: str, fallback: str) -> str:
            v = str(value or "").strip()
            return v if _HHMM_RE.match(v) else fallback

        win_start_field = ft.TextField(
            label="Не турбувати з",
            value=_win_start_init,
            hint_text="21:00",
            width=120,
            dense=True,
        )
        win_end_field = ft.TextField(
            label="До",
            value=_win_end_init,
            hint_text="07:59",
            width=120,
            dense=True,
        )
        send_time_field = ft.TextField(
            label="Надіслати накопичене о",
            value=_send_time_init,
            hint_text="08:00",
            width=170,
            dense=True,
        )
        tg_sw_msg = ft.Text("", size=11)
        pending_status_txt = ft.Text("", size=11)
        next_msg_col = ft.Column(spacing=8)

        def _fmt_pending_item(item: dict) -> tuple[str, str]:
            stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
            send_at_disp = str(item.get("send_at") or "")[:16].replace("T", " ") or "?"
            summary = (
                f"GSM: {int(stats.get('gsm_written', 0) or 0)}, "
                f"ЄК: {int(stats.get('ek_written', 0) or 0)}, "
                f"змін: {int(stats.get('changed', 0) or 0)}, "
                f"помилок: {int(stats.get('errors', 0) or 0)}, "
                f"час: {float(stats.get('elapsed_sec', 0) or 0):.0f}с"
            )
            return send_at_disp, summary

        def _refresh_next_message_preview():
            try:
                pending = self.db.get_gsm_pending_telegram_notifications() or []
            except Exception:
                pending = []
            if not pending:
                next_msg_col.controls = [
                    ft.Text(
                        "Немає запланованих повідомлень у черзі.",
                        size=12,
                        color=ft.Colors.BLUE_GREY_600,
                    )
                ]
            else:
                rows = []
                for idx, item in enumerate(pending):
                    send_at_disp, summary = _fmt_pending_item(item)

                    def _remove_one(_, i=idx):
                        try:
                            cur = self.db.get_gsm_pending_telegram_notifications() or []
                            remaining = [x for j, x in enumerate(cur) if j != i]
                            self.db.save_gsm_pending_telegram_notifications(remaining)
                            self._show_snack("Повідомлення прибрано з черги", "green")
                        except Exception as exc:
                            self._show_snack(f"Помилка: {exc}", "red")
                        _refresh_next_message_preview()
                        _refresh_pending_status()

                    rows.append(
                        ft.Container(
                            padding=10,
                            border_radius=8,
                            bgcolor=ft.Colors.BLUE_GREY_50,
                            content=ft.Row(
                                [
                                    ft.Column(
                                        [
                                            ft.Text(
                                                f"Заплановано на: {send_at_disp}",
                                                weight=ft.FontWeight.W_600,
                                                size=12,
                                            ),
                                            ft.Text(summary, size=11, color=ft.Colors.BLUE_GREY_700),
                                        ],
                                        spacing=2,
                                        expand=True,
                                    ),
                                    ft.OutlinedButton(
                                        "Прибрати з черги",
                                        icon=ft.Icons.DELETE_OUTLINE,
                                        on_click=_remove_one,
                                        style=ft.ButtonStyle(color=ft.Colors.RED_600),
                                    ),
                                ],
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                        )
                    )
                next_msg_col.controls = rows
            with contextlib.suppress(Exception):
                next_msg_col.update()

        def _refresh_pending_status():
            try:
                pending = self.db.get_gsm_pending_telegram_notifications() or []
            except Exception:
                pending = []
            if pending:
                _times = ", ".join(
                    str(it.get("send_at") or "?")[:16].replace("T", " ")
                    for it in pending
                )
                pending_status_txt.value = f"В черзі відкладених: {len(pending)} (заплановано на {_times})"
                pending_status_txt.color = ft.Colors.ORANGE_800
            else:
                pending_status_txt.value = "В черзі немає відкладених повідомлень"
                pending_status_txt.color = ft.Colors.BLUE_GREY_600
            with contextlib.suppress(Exception):
                pending_status_txt.update()
            _refresh_next_message_preview()

        def _cancel_pending(_):
            try:
                self.db.save_gsm_pending_telegram_notifications([])
                self._show_snack("Відкладені сповіщення скасовано", "green")
            except Exception as exc:
                self._show_snack(f"Помилка: {exc}", "red")
            _refresh_pending_status()

        _refresh_pending_status()

        def _save_tg_toggle(_):
            try:
                d = self._get_gsm_draft()
                tg = d.setdefault("telegram", {})
                tg["tg_enabled"] = bool(tg_enabled_sw.value)
                tg["report_window_start"] = _norm_hhmm(win_start_field.value, "21:00")
                tg["report_window_end"] = _norm_hhmm(win_end_field.value, "07:59")
                tg["report_send_time"] = _norm_hhmm(send_time_field.value, "08:00")
                tg["bot_token"] = str(bot_token_field.value or "").strip()
                tg["chat_id"] = str(chat_id_field.value or "").strip()
                tg["chat_link"] = str(chat_link_field.value or "").strip()
                tg["spreadsheet_url"] = str(spreadsheet_url_field.value or "").strip()
                win_start_field.value = tg["report_window_start"]
                win_end_field.value = tg["report_window_end"]
                send_time_field.value = tg["report_send_time"]
                self.db.save_gsm_parser_global_config(d)
                tg_sw_msg.value = "Збережено"
                tg_sw_msg.color = _OK_DK
            except Exception as exc:
                tg_sw_msg.value = f"Помилка: {exc}"
                tg_sw_msg.color = ft.Colors.RED_600
            with contextlib.suppress(Exception):
                win_start_field.update()
            with contextlib.suppress(Exception):
                win_end_field.update()
            with contextlib.suppress(Exception):
                send_time_field.update()
            with contextlib.suppress(Exception):
                tg_sw_msg.update()

        tg_toggle_content = ft.Column(
            [
                ft.Text(
                    self.t(
                        "extensions.tg.chat_link_hint",
                        "Посилання на чат: відкрийте потрібний чат у Telegram -> Поділитись/Copy link."
                        " Для приватних груп додайте бота в групу і використовуйте Chat ID.",
                    ),
                    size=11,
                    color=ft.Colors.BLUE_GREY_600,
                ),
                ft.Row(
                    [bot_token_field, chat_id_field, chat_link_field, spreadsheet_url_field],
                    wrap=True, spacing=10, run_spacing=10,
                ),
                ft.Divider(height=1, thickness=1),
                tg_enabled_sw,
                ft.Text(
                    "Якщо парсинг завершується у вікні \"Не турбувати\" (діапазон"
                    " може переходити через північ, напр. 21:00 → 07:59),"
                    " сповіщення не надсилається одразу — воно чекає і йде"
                    " о вказаний час \"Надіслати накопичене о\". Це відбувається"
                    " автоматично, поки відкрита ця програма (супер-адмін, або"
                    " користувач з дозволом \"Запуск парсингу\"), або якщо парсер"
                    " працює в режимі демона на VPS — не обов'язково чекати"
                    " наступного запуску парсингу.",
                    size=11,
                    color=ft.Colors.BLUE_GREY_600,
                ),
                ft.Row(
                    [win_start_field, win_end_field, send_time_field],
                    spacing=10,
                ),
                ft.Row(
                    [
                        ft.FilledButton("Зберегти", icon=ft.Icons.SAVE, on_click=_save_tg_toggle),
                        tg_sw_msg,
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Divider(height=1, thickness=1),
                pending_status_txt,
                ft.Row(
                    [
                        ft.OutlinedButton(
                            "Оновити статус",
                            icon=ft.Icons.REFRESH,
                            on_click=lambda _: _refresh_pending_status(),
                        ),
                        ft.OutlinedButton(
                            "Скасувати відкладені",
                            icon=ft.Icons.CANCEL_SCHEDULE_SEND,
                            on_click=_cancel_pending,
                        ),
                    ],
                    spacing=10,
                ),
            ],
            spacing=10,
            tight=True,
        )

        def _tile(title: str, subtitle: str, content) -> ft.ExpansionTile:
            return ft.ExpansionTile(
                title=ft.Text(title, weight=ft.FontWeight.W_700, size=14),
                subtitle=ft.Text(subtitle, size=11, color=ft.Colors.BLUE_GREY_600),
                controls_padding=ft.padding.only(left=8, right=8, bottom=16, top=4),
                controls=[content],
                expanded=False,
            )

        # ── Text повідомлень: системні + блоки по категоріях ──────────────────
        if not isinstance(self._gsm_form_refs, dict):
            self._gsm_form_refs = {}
        self._gsm_form_refs["system_messages"] = {}
        self._gsm_form_refs["categories"] = {}

        _msg_status = ft.Text("", size=11)

        def _save_message_editor(_):
            try:
                self._capture_message_editor_state()
                self.db.save_gsm_parser_global_config(self._get_gsm_draft())
                _msg_status.value = "Збережено"
                _msg_status.color = _OK_DK
            except Exception as exc:
                _msg_status.value = f"Помилка: {exc}"
                _msg_status.color = ft.Colors.RED_600
            with contextlib.suppress(Exception):
                _msg_status.update()

        _tg_messages = (draft.get("telegram") or {}).get("messages") or []
        _msg_index: dict[str, dict] = {
            m.get("id"): m for m in _tg_messages if isinstance(m, dict) and m.get("id")
        }
        sys_msg_rows = []
        for msg_id in _GSM_SYSTEM_MESSAGE_ORDER:
            _title, _default_text = _GSM_SYSTEM_MESSAGE_DEFAULTS[msg_id]
            _title = self.t(f"extensions.sysmsg.{msg_id}", _title)
            _row = _msg_index.get(msg_id) or {}
            _refs = {
                "enabled": ft.Checkbox(
                    label=self.t("extensions.msg_editor.active_msg", "Активне"),
                    value=bool(_row.get("enabled", True)),
                ),
                "text": ft.TextField(
                    value=str(_row.get("text") or _default_text),
                    multiline=True, min_lines=2, max_lines=5,
                    border_radius=8, text_size=12, expand=True,
                ),
            }
            self._gsm_form_refs["system_messages"][msg_id] = _refs
            sys_msg_rows.append(
                ft.Column(
                    [
                        ft.Row([ft.Text(_title, size=12, weight="bold"), _refs["enabled"]], spacing=10),
                        _refs["text"],
                    ],
                    spacing=4, tight=True,
                )
            )
        sys_messages_content = ft.Column(
            [
                *sys_msg_rows,
                ft.Row(
                    [
                        ft.FilledButton(self.t("common.save", "Зберегти"), icon=ft.Icons.SAVE, on_click=_save_message_editor),
                        _msg_status,
                    ],
                    spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
            spacing=14, tight=True,
        )

        category_sections = []
        for cat_key in _GSM_CATEGORY_ORDER:
            _cat_title_default, _cat_subtitle_default = _GSM_CATEGORY_LABELS[cat_key]
            cat_title = self.t(f"extensions.category.{cat_key}.title", _cat_title_default)
            cat_subtitle = self.t(f"extensions.category.{cat_key}.subtitle", _cat_subtitle_default)
            blocks = self._gsm_category_blocks_draft(cat_key)
            self._gsm_form_refs["categories"][cat_key] = []
            block_cards = []
            for idx, block in enumerate(blocks):
                kind = str(block.get("kind") or "header")
                refs = {"kind": kind, "enabled": ft.Checkbox(
                    label=self.t("extensions.msg_editor.active_block", "Активний"),
                    value=bool(block.get("enabled", True)),
                )}
                move_up = ft.IconButton(
                    icon=ft.Icons.ARROW_UPWARD, icon_size=16,
                    tooltip=self.t("extensions.msg_editor.move_up", "Перемістити вище"),
                    disabled=idx == 0,
                    on_click=self._move_category_block(cat_key, idx, -1),
                )
                move_down = ft.IconButton(
                    icon=ft.Icons.ARROW_DOWNWARD, icon_size=16,
                    tooltip=self.t("extensions.msg_editor.move_down", "Перемістити нижче"),
                    disabled=idx == len(blocks) - 1,
                    on_click=self._move_category_block(cat_key, idx, 1),
                )
                delete_btn = ft.IconButton(
                    icon=ft.Icons.DELETE_OUTLINE, icon_color=ft.Colors.RED_400,
                    icon_size=18, tooltip=self.t("extensions.msg_editor.delete_block", "Видалити"),
                    on_click=self._remove_category_block(cat_key, idx),
                )
                if kind == "header":
                    text_field = ft.TextField(
                        label=self.t("extensions.msg_editor.header_text", "Текст заголовка"),
                        value=str(block.get("text") or ""),
                        multiline=True, min_lines=1, max_lines=3,
                        expand=True, dense=True,
                    )
                    photo_field = ft.TextField(
                        label=self.t("extensions.msg_editor.header_photo", "Фото (необов'язково)"),
                        value=str(block.get("photo_path") or ""),
                        width=280, dense=True,
                    )
                    refs["text"] = text_field
                    refs["photo"] = photo_field
                    body = ft.Column(
                        [
                            text_field,
                            ft.Row(
                                [
                                    photo_field,
                                    ft.OutlinedButton(
                                        self.t("extensions.msg_editor.upload_photo", "Завантажити фото"),
                                        icon=ft.Icons.UPLOAD_FILE,
                                        on_click=lambda _, pf=photo_field: self._upload_logo_to_field(pf, "gsm_category_header"),
                                    ),
                                ],
                                wrap=True, spacing=10,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                        ],
                        spacing=8, tight=True,
                    )
                    badge = ft.Container(
                        content=ft.Text(self.t("extensions.msg_editor.badge_header", "Заголовок"), size=10, color=ft.Colors.WHITE),
                        bgcolor=ft.Colors.BLUE_700, padding=ft.padding.symmetric(horizontal=8, vertical=3),
                        border_radius=6,
                    )
                else:
                    selected_fields = set(block.get("fields") or [])
                    field_checks = {}
                    check_ctrls = []
                    for f_key, f_label, _tpl in FIELD_CATALOG:
                        cb = ft.Checkbox(
                            label=self.t(f"extensions.field.{f_key}", f_label),
                            value=f_key in selected_fields,
                        )
                        field_checks[f_key] = cb
                        check_ctrls.append(cb)
                    refs["field_checks"] = field_checks
                    if is_mobile(self._page()):
                        body = make_scrollable_chip_row(
                            check_ctrls, spacing=4, run_spacing=0
                        )
                    else:
                        body = ft.Row(
                            check_ctrls, wrap=True, spacing=4, run_spacing=0
                        )
                    badge = ft.Container(
                        content=ft.Text(self.t("extensions.msg_editor.badge_card", "Картка пристрою"), size=10, color=ft.Colors.WHITE),
                        bgcolor=ft.Colors.TEAL_700, padding=ft.padding.symmetric(horizontal=8, vertical=3),
                        border_radius=6,
                    )

                self._gsm_form_refs["categories"][cat_key].append(refs)
                block_cards.append(
                    ft.Container(
                        border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
                        border_radius=14, padding=12, bgcolor=_ALT,
                        content=ft.Column(
                            [
                                ft.Row(
                                    [badge, refs["enabled"], move_up, move_down, delete_btn],
                                    wrap=True, spacing=6,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                                body,
                            ],
                            spacing=8,
                        ),
                    )
                )

            category_sections.append(
                self._section_card(
                    cat_title, cat_subtitle,
                    ft.Column(
                        [
                            ft.Column(block_cards, spacing=10, tight=True) if block_cards else ft.Text(
                                self.t("extensions.msg_editor.no_blocks", "Блоків немає — додайте заголовок або картку пристрою."),
                                size=12, color=ft.Colors.BLUE_GREY_500, italic=True,
                            ),
                            ft.Row(
                                [
                                    ft.OutlinedButton(
                                        self.t("extensions.msg_editor.add_header", "+ Заголовок"), icon=ft.Icons.ADD,
                                        on_click=self._add_category_block(cat_key, "header"),
                                    ),
                                    ft.OutlinedButton(
                                        self.t("extensions.msg_editor.add_card", "+ Картка пристрою"), icon=ft.Icons.ADD,
                                        on_click=self._add_category_block(cat_key, "device_card"),
                                    ),
                                ],
                                spacing=10, wrap=True,
                            ),
                        ],
                        spacing=12, tight=True,
                    ),
                )
            )

        category_blocks_content = ft.Column(
            [
                ft.Text(
                    self.t(
                        "extensions.msg_editor.category_blocks_intro",
                        "Для кожної категорії — довільна кількість блоків у будь-якому порядку:"
                        " «Заголовок» (текст + необов'язкове фото — розділяє пристрої в чаті)"
                        " і «Картка пристрою» (просто позначте чекбоксами, яку інформацію"
                        " показувати — жодних шаблонів набирати не потрібно).",
                    ),
                    size=12, color=ft.Colors.BLUE_GREY_600,
                ),
                *category_sections,
            ],
            spacing=16, tight=True,
        )

        return ft.Column(
            [
                ft.Card(
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=4, vertical=4),
                        content=ft.Column(
                            [
                                _tile(
                                    "Бренди та посилання",
                                    "GSM Arena URL, останній пристрій, посилання на Єкаталог",
                                    brands_content,
                                ),
                                ft.Divider(height=1, thickness=1),
                                _tile(
                                    "Розклад запусків",
                                    "Автоматичний запуск парсера за розкладом (UTC)",
                                    schedule_content,
                                ),
                                ft.Divider(height=1, thickness=1),
                                _tile(
                                    "Telegram сповіщення",
                                    "Вмикає/вимикає відправку звіту в Telegram",
                                    tg_toggle_content,
                                ),
                                ft.Divider(height=1, thickness=1),
                                _tile(
                                    "Наступне повідомлення телеграм",
                                    "Перегляд того, що заплановано до відправки — можна прибрати непотрібне з черги",
                                    next_msg_col,
                                ),
                                ft.Divider(height=1, thickness=1),
                                _tile(
                                    self.t("extensions.tile.sys_messages.title", "Системні повідомлення"),
                                    self.t("extensions.tile.sys_messages.subtitle", "Вступ, статистика, фінал і текст \"нічого нового\""),
                                    sys_messages_content,
                                ),
                                ft.Divider(height=1, thickness=1),
                                _tile(
                                    self.t("extensions.tile.category_blocks.title", "Повідомлення по категоріях"),
                                    self.t("extensions.tile.category_blocks.subtitle", "Заголовки й картки пристроїв для GSM Arena / E-Katalog — додавайте, видаляйте, змінюйте порядок без коду"),
                                    category_blocks_content,
                                ),
                            ],
                            spacing=0,
                            tight=True,
                        ),
                    ),
                    elevation=2,
                )
            ],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )

    def _read_gsm_parser_log_text(self) -> str:
        """DB-backed first — the parser may actually be running on a
        different machine (the VPS scheduler), in which case
        LAST_PARSE_LOG_PATH here is always stale/empty. Falls back to the
        local file only if the DB has nothing."""
        try:
            db_content = self.db.get_gsm_parser_run_log()
        except Exception:
            db_content = ""
        if db_content:
            return db_content
        try:
            if LAST_PARSE_LOG_PATH.exists():
                return LAST_PARSE_LOG_PATH.read_text(encoding="utf-8")
        except Exception:
            pass
        return "(лог порожній)"

    def _start_log_auto_refresh(self) -> None:
        """Poll the DB-backed run log every few seconds while the "Логи"
        sub-tab is open, so progress from a parser running elsewhere (the
        VPS scheduler) shows up live instead of only on manual refresh /
        app restart. Stops itself the moment the user navigates away."""
        if self._log_auto_refresh_running:
            return
        page = self._page()
        if not page:
            return
        self._log_auto_refresh_running = True

        async def _poll():
            import asyncio

            try:
                while getattr(self, "gsm_detail_tab", None) == "logs":
                    await asyncio.sleep(4)
                    if getattr(self, "gsm_detail_tab", None) != "logs":
                        break
                    if self._log_refresh_fn:
                        with contextlib.suppress(Exception):
                            self._log_refresh_fn()
            finally:
                self._log_auto_refresh_running = False

        with contextlib.suppress(Exception):
            page.run_task(_poll)

    def _build_log_tab(self):
        log_text = self._read_gsm_parser_log_text()

        self._log_text_ref = ft.Text(
            log_text,
            selectable=True,
            font_family="monospace",
            size=12,
            color="#d4d4d4",
        )

        def refresh_log(_=None):
            try:
                new_text = self._read_gsm_parser_log_text()
            except Exception:
                new_text = "(помилка читання логу)"
            if self._log_text_ref:
                self._log_text_ref.value = new_text
                try:
                    self._log_text_ref.update()
                except Exception:
                    pass

        self._log_refresh_fn = refresh_log
        self._start_log_auto_refresh()

        return ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text("Лог останнього парсингу", size=15, weight=ft.FontWeight.W_600, color="#d4d4d4"),
                            ft.IconButton(
                                icon=ft.Icons.REFRESH,
                                tooltip="Оновити лог",
                                on_click=refresh_log,
                                icon_color="#7ec8a0",
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Container(
                        content=ft.Column(
                            [self._log_text_ref],
                            scroll=ft.ScrollMode.ADAPTIVE,
                            expand=True,
                        ),
                        bgcolor="#111827",
                        border_radius=8,
                        padding=12,
                        expand=True,
                    ),
                ],
                expand=True,
                spacing=8,
            ),
            expand=True,
            bgcolor="#1a1a2e",
            border_radius=10,
            padding=12,
        )

    def _build_gsm_parser_detail(self):
        if self.is_superadmin:
            tab_nav = [
                ("results", self.t("extensions.tab.results", "Переглянути Результати"), ft.Icons.TABLE_CHART),
                ("permissions", self.t("extensions.tab.permissions", "Дозволи"), ft.Icons.LOCK_OPEN),
                ("logs", self.t("extensions.tab.logs", "Логи"), ft.Icons.ARTICLE),
                ("settings", self.t("nav.settings_tooltip", "Налаштування"), ft.Icons.TUNE),
            ]
        else:
            tab_nav = [
                ("results", self.t("extensions.tab.results", "Переглянути Результати"), ft.Icons.TABLE_CHART),
                ("settings", self.t("nav.settings_tooltip", "Налаштування"), ft.Icons.TUNE),
            ]

        # Fast path: panels already built — update visibility/styles in-place
        if self._gsm_outer_col is not None and self._gsm_tab_panels:
            self._apply_gsm_tab_visibility()
            if self.gsm_detail_tab == "logs" and hasattr(self, "_log_refresh_fn"):
                self._log_refresh_fn()
            return [self._gsm_outer_col]

        # First-time build: construct tab buttons and empty (loading) panels
        # only — the actual DB reads + control building for each tab used to
        # ALL run synchronously here before anything was shown, which is what
        # made the screen freeze on entry. Only the active tab is built now,
        # in the background (see _load_gsm_tab_async); other tabs build lazily
        # the first time the user switches to them.
        self._gsm_tab_btn_refs = {}
        tab_buttons = []
        for key, title, icon_name in tab_nav:
            btn = ft.FilledButton(
                title,
                icon=icon_name,
                on_click=self._switch_gsm_tab(key),
                style=self._gsm_tab_btn_style(key == self.gsm_detail_tab),
                # Disabled up front — the active tab's background load is
                # kicked off right after this loop, before first paint.
                disabled=True,
            )
            self._gsm_tab_btn_refs[key] = btn
            tab_buttons.append(btn)

        _scrollable_tabs = {"permissions", "settings"}
        self._gsm_tab_panels = {}
        self._gsm_tab_built = set()
        for key, _, _ in tab_nav:
            self._gsm_tab_panels[key] = ft.Container(
                content=ft.Row(
                    [ft.ProgressRing(width=28, height=28, stroke_width=3, color=_OK_DK)],
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
                padding=ft.padding.symmetric(vertical=40),
                visible=(key == self.gsm_detail_tab),
                expand=(key not in _scrollable_tabs),
            )

        try:
            self.content_area.scroll = (
                ft.ScrollMode.ADAPTIVE if self.gsm_detail_tab in _scrollable_tabs else None
            )
        except Exception:
            pass

        self._gsm_loading_bar_ref = ft.ProgressBar(
            visible=True, color=_OK_DK, bgcolor=ft.Colors.BLUE_GREY_100
        )
        self._gsm_loading_status_ref = ft.Text(
            "", size=11, color=ft.Colors.BLUE_GREY_600, visible=True
        )

        self._gsm_outer_col = ft.Column(
            [
                ft.Row(
                    [
                        ft.IconButton(
                            icon=ft.Icons.ARROW_BACK_ROUNDED,
                            tooltip=self.t("extensions.back_to_list", "Назад до розширень"),
                            on_click=self._back_to_extensions_list,
                        ),
                        ft.Column(
                            [ft.Text("GSM Parser", size=26, weight=ft.FontWeight.W_700)],
                            spacing=2,
                            expand=True,
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                self._gsm_loading_bar_ref,
                self._gsm_loading_status_ref,
                (
                    make_scrollable_chip_row(tab_buttons, spacing=10, run_spacing=10)
                    if is_mobile(self._page())
                    else ft.Row(tab_buttons, wrap=True, spacing=10, run_spacing=10)
                ),
                *self._gsm_tab_panels.values(),
            ],
            spacing=14,
            expand=True,
        )

        page = self._page()
        if page:
            page.run_task(self._load_gsm_tab_async, self.gsm_detail_tab)

        return [self._gsm_outer_col]

    def _build_superadmin_controls(self):
        if self.current_extension_key == "gsm_parser":
            return self._build_gsm_parser_detail()
        return self._build_extensions_matrix()

    def _build_admin_controls(self):
        company_id = self.active_company_id or self.user_data.get("company_id")
        if not company_id:
            return [
                ft.Text(self.t("extensions.title", "Розширення"), size=24, weight=ft.FontWeight.W_700),
                ft.Text("Не визначено компанію", color=ft.Colors.RED),
            ]

        state = self.db.get_company_gsm_parser_state(company_id)
        # Superadmin is the sole company-wide switch: on = on for every user
        # in the company, off = off for everyone. Admin has no enable/disable
        # of their own — only the ability to open it when it's on. Each user
        # separately controls whether they PERSONALLY see it, via their own
        # settings (get_gsm_parser_user_prefs), which is unrelated to this.
        sa_enabled = bool(state.get("sa_enabled"))

        def run_click(_):
            if not sa_enabled:
                self._show_snack(
                    "Запуск недоступний: розширення вимкнене суперадміном", ft.Colors.ORANGE
                )
                return
            self._launch_parser(company_id)

        return [
            ft.Row([
                ft.Text(self.t("extensions.title", "Розширення"), size=24, weight=ft.FontWeight.W_700),
                ft.IconButton(
                    icon=ft.Icons.HELP_OUTLINE,
                    icon_size=18,
                    tooltip=self.t("common.help.tooltip", "Довідка"),
                    on_click=lambda _e: self._show_help(),
                ),
            ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
            self._section_card(
                "GSM Parser",
                "Розширення вмикає/вимикає суперадмін для всієї компанії. Адмін лише бачить статус і відкриває перегляд.",
                ft.Column(
                    [
                        ft.Text(
                            self.t("extensions.status_label", "Статус: ")
                            + (
                                self.t("common.enabled", "Увімкнено")
                                if sa_enabled
                                else self.t("common.disabled", "Вимкнено")
                                + self.t("extensions.blocked_by_sa", " (вимкнено суперадміном)")
                            ),
                            color=ft.Colors.GREEN_700 if sa_enabled else ft.Colors.RED_700,
                        ),
                        ft.Row(
                            [
                                ft.FilledButton(
                                    self.t("extensions.open", "Відкрити"),
                                    icon=ft.Icons.PLAY_ARROW,
                                    on_click=run_click,
                                    disabled=not sa_enabled,
                                ),
                            ],
                            spacing=10,
                            wrap=True,
                        ),
                    ],
                    spacing=10,
                ),
            ),
        ]

    def build(self):
        _th = _th_from_view(self)
        global _OK_DK, _ALT, _ALT2, _OK_BG, _ACCENT, _TEXT, _SOFT
        _OK_DK = _th["ok"]; _ALT = _ALT2 = _th["alt"]
        _OK_BG = _th["ok_bg"]; _ACCENT = _th["icon_color"]
        _TEXT = _th["text"]; _SOFT = _th["soft"]
        self.content_area.controls.clear()
        if self.is_superadmin:
            self.content_area.controls.extend(self._build_superadmin_controls())
        else:
            self.content_area.controls.extend(self._build_admin_controls())
        try:
            if self._page():
                self.content_area.update()
        except Exception:
            pass
