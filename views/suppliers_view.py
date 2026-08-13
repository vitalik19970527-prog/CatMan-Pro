import asyncio
import contextlib
import os
import re
import uuid

import flet as ft
import i18n

from views.async_view_build_helper import safe_update_with_retry

# ---------------------------------------------------------------------------
# Theme colour shims
# ---------------------------------------------------------------------------
_ACCENT = "#1D4ED8"
_MUTED = "#475569"
_BORDER = "#D6DCE6"
_PANEL = "#FFFFFF"
_CHIP_T = "#1E3A8A"
_SOFT_I = "#E0E7FF"


def _th_from_view(view) -> dict:
    from views.themes import get_active_theme

    return get_active_theme(str(getattr(view, "manager_id", "") or ""))


from logger import logger

# Для роботи з картинками (стиснення)
try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import tkinter as tk
    from tkinter import filedialog

    TK_AVAILABLE = True
except Exception:
    TK_AVAILABLE = False


def _sniff_image_ext(data: bytes) -> str:
    """Detect the real image format from magic bytes — used when the cached
    file's own extension is missing/generic (".bin"), so a saved QR code
    still opens as a picture instead of an unrecognized file."""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def _sanitize_filename_part(text: str) -> str:
    """Strip characters Windows/macOS/Linux all forbid in filenames."""
    cleaned = re.sub(r'[\\/:*?"<>|]+', " ", str(text or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "Файл"


class SuppliersView:
    HEADER_SIZE = 24
    META_TEXT_SIZE = 12
    EMPTY_TEXT_SIZE = 14
    ALL_REGIONS_KEY = "__all_regions__"
    # Persists the archive-section collapse toggle across SuppliersView
    # re-instantiation (a fresh instance is created on every tab load and
    # every background sync-pulse reload, so a plain `self` attribute would
    # reset to collapsed every time). Keyed by company_id.
    _archive_collapsed_by_company: dict = {}

    @staticmethod
    def _normalize_currency_code(raw, default="USD"):
        allowed = {"USD", "CNY", "EUR", "UAH", "PLN"}
        aliases = {
            "$": "USD",
            "USDT": "USD",
            "RMB": "CNY",
            "YUAN": "CNY",
            "EURO": "EUR",
            "ZL": "PLN",
            "GRN": "UAH",
            "HRN": "UAH",
            "ГРН": "UAH",
        }
        token = str(raw or "").strip().upper().replace(" ", "")
        token = token.replace(".", "").replace("_", "")
        if token in allowed:
            return token
        if token in aliases:
            return aliases[token]
        fallback = str(default or "USD").strip().upper()
        return fallback if fallback in allowed else "USD"

    def __init__(
        self, content_area, user_data, db, current_region_name, quick_filter: str = ""
    ):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.company_id = self.user_data.get("company_id")
        self.current_region_name = current_region_name
        self.quick_filter = str(quick_filter or "").strip().lower()
        self.read_only_subordinate = bool(
            self.user_data.get("subordinate_view_readonly")
        )
        self._general_mode: bool = bool(self.user_data.get("general_mode", False))
        self._general_manager_ids: list[str] = list(
            self.user_data.get("general_manager_ids") or []
        )
        from views.general_mode_mixin import build_general_manager_names

        self._general_manager_names: dict[str, str] = build_general_manager_names(
            self.user_data.get("all_subordinates_flat") or [], self.user_data
        )
        self.is_superadmin = (self.user_data.get("role") or "") == "СуперАдмін"
        self.manager_id = self.user_data.get(
            "active_manager_id", self.user_data.get("id")
        )
        self.t = i18n.bind(str(self.manager_id or ""))
        _nr = (self.user_data.get("role") or "").strip().lower().replace(" ", "")
        self.is_admin = _nr in {"адмін", "админ", "admin"}
        self.is_privileged = self.is_superadmin or self.is_admin
        self.available_currencies = ["USD", "UAH", "EUR", "PLN", "CNY"]
        self._manager_name_cache = {}

        raw_regions = self.db.get_company_regions(self.company_id)
        self.regions_data = raw_regions if isinstance(raw_regions, list) else []
        self._region_name_by_id = {
            r.get("id"): r.get("name")
            for r in self.regions_data
            if isinstance(r, dict) and r.get("id")
        }

        self.temp_alipay_path = None
        self.temp_wechat_path = None
        self.current_upload_target = None
        self._debounce_tasks = {}
        self._search_query = ""
        self._header_loading_indicator = None

    def _page(self):
        try:
            return self.content_area.page if self.content_area else None
        except RuntimeError:
            return None

    def _can_modify_supplier(self, supplier):
        supplier = supplier if isinstance(supplier, dict) else {}
        owner_id = (supplier.get("manager_id") or "").strip()
        current_id = (self.manager_id or "").strip()
        if self.is_privileged:
            return True
        supplier_id = (supplier.get("id") or "").strip()
        # Was a per-card `has_entity_owner` DB round trip (N queries for N
        # suppliers on every render); load_suppliers() now fetches this same
        # manager's whole owned-supplier-id set once and caches it here.
        if supplier_id and supplier_id in self._owned_supplier_ids_cache():
            return True
        if not owner_id:
            return True
        return owner_id == current_id

    def _owned_supplier_ids_cache(self) -> set:
        cached = getattr(self, "_owned_supplier_ids", None)
        if cached is not None:
            return cached
        current_id = (self.manager_id or "").strip()
        try:
            ids = set(
                self.db.get_entity_owner_entity_ids(
                    self.company_id, current_id, "supplier"
                )
                or []
            )
        except Exception:
            ids = set()
        self._owned_supplier_ids = ids
        return ids

    def _resolve_manager_name(self, company_id, manager_id):
        try:
            manager_id = (manager_id or "").strip()
            if not manager_id:
                return ""

            target_company_id = company_id or self.company_id
            cache_key = target_company_id or "__all__"
            cached = self._manager_name_cache.get(cache_key)
            if cached is None:
                cached = {}
                if target_company_id:
                    for manager in (
                        self.db.get_company_managers(target_company_id) or []
                    ):
                        if not isinstance(manager, dict) or not manager.get("id"):
                            continue
                        cached[manager.get("id")] = (
                            manager.get("full_name")
                            or manager.get("name")
                            or manager.get("email")
                            or manager.get("phone")
                            or manager.get("id")
                        )
                self._manager_name_cache[cache_key] = cached

            resolved = cached.get(manager_id, "")
            if resolved:
                return resolved
            if manager_id == self.user_data.get(
                "active_manager_id"
            ) or manager_id == self.user_data.get("id"):
                return (
                    self.user_data.get("active_manager_name")
                    or self.user_data.get("full_name")
                    or ""
                )
            return ""
        except Exception:
            logger.exception("Resolve supplier manager name failed")
            return ""

    def _effective_payment_fields_cached(self, company_id: str, region_id: str) -> list:
        """Memoized per (company_id, manager_id, region_id) for the duration
        of one render pass — this was called twice per supplier card
        (_get_enabled_payment_method_names + _get_effective_payment_image_entries),
        and get_effective_payment_fields_for_manager itself is 4 uncached DB
        round trips, so a list of N suppliers used to cost up to ~8×N queries
        for data that's identical across (almost) the whole list."""
        cache = getattr(self, "_eff_payment_fields_cache", None)
        if cache is None:
            cache = {}
            self._eff_payment_fields_cache = cache
        key = (company_id, self.manager_id, region_id)
        if key in cache:
            return cache[key]
        try:
            fields = (
                self.db.get_effective_payment_fields_for_manager(
                    company_id, self.manager_id, region_id=region_id
                )
                or []
            )
        except Exception:
            fields = []
        cache[key] = fields
        return fields

    def _get_enabled_payment_method_names(self, supplier: dict) -> list[str]:
        """
        Returns list of enabled payment method names for a supplier.
        E.g. ["SWIFT", "Alipay", "WeChat"]
        """
        supplier = supplier if isinstance(supplier, dict) else {}
        company_id = (supplier.get("company_id") or self.company_id or "").strip()
        region_id = (supplier.get("region_id") or "").strip()
        if not company_id:
            return []

        # Get effective payment fields template for manager
        effective_fields = self._effective_payment_fields_cached(company_id, region_id)

        # Build map of field_id -> supplier's payment_field config
        by_field_id = {}
        pf = supplier.get("payment_fields")
        if isinstance(pf, list):
            for r in pf:
                if isinstance(r, dict) and r.get("field_id"):
                    by_field_id[str(r.get("field_id"))] = r

        # Collect enabled payment methods
        enabled_names = []
        for f in effective_fields:
            if not isinstance(f, dict) or not f.get("id"):
                continue
            fid = str(f.get("id"))
            field_name = (f.get("name") or "").strip()
            if not field_name:
                continue

            # Check if enabled in supplier's payment_fields
            row = by_field_id.get(fid) or {}
            if isinstance(row, dict):
                enabled = row.get("enabled")
                if enabled:
                    enabled_names.append(field_name)

        return enabled_names

    def _get_effective_payment_image_entries(self, supplier: dict) -> list[dict]:
        """
        Returns list of image entries to display for supplier payment fields:
        [{title, url, source}] where source is legacy key (alipay_qr/wechat_qr) or field_id.
        """
        supplier = supplier if isinstance(supplier, dict) else {}
        company_id = (supplier.get("company_id") or self.company_id or "").strip()
        region_id = (supplier.get("region_id") or "").strip()
        if not company_id:
            return []

        effective_fields = self._effective_payment_fields_cached(company_id, region_id)

        by_field_id = {}
        pf = supplier.get("payment_fields")
        if isinstance(pf, list):
            for r in pf:
                if isinstance(r, dict) and r.get("field_id"):
                    by_field_id[str(r.get("field_id"))] = r

        legacy = {
            "alipay_qr": str(supplier.get("alipay_qr") or "").strip(),
            "wechat_qr": str(supplier.get("wechat_qr") or "").strip(),
        }

        # map code -> legacy qr field key if template wants image
        code_to_legacy_qr = {
            "alipay_qr": "alipay_qr",
            "alipay": "alipay_qr",
            "alipayid": "alipay_qr",
            "alipay_id": "alipay_qr",
            "wechat_qr": "wechat_qr",
            "wechat": "wechat_qr",
            "wechatid": "wechat_qr",
            "wechat_id": "wechat_qr",
        }

        out = []
        for f in effective_fields:
            if not isinstance(f, dict) or not f.get("id") or not f.get("has_image"):
                continue
            fid = str(f.get("id"))
            code = str(f.get("code") or "").strip().lower()
            title = str(f.get("name") or "").strip() or code or "QR"

            url = ""
            row = by_field_id.get(fid) or {}
            if isinstance(row, dict):
                url = str(row.get("image_url") or "").strip()
            if not url:
                legacy_key = code_to_legacy_qr.get(code)
                if legacy_key:
                    url = legacy.get(legacy_key, "")
                    if url:
                        out.append({"title": title, "url": url, "source": legacy_key})
                        continue
            if url:
                out.append({"title": title, "url": url, "source": fid})
        return out

    def delete_payment_field_image(self, supplier_id: str, field_id: str):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        supplier_row = next(
            (
                s
                for s in (
                    self.db.get_suppliers(
                        self.company_id, None, manager_id=self.manager_id
                    )
                    or []
                )
                if isinstance(s, dict) and s.get("id") == supplier_id
            ),
            None,
        )
        if supplier_row and not self._can_modify_supplier(supplier_row):
            self._show_snack(
                self.t("suppliers.locked_by_other", "Цей постачальник закріплений за іншим користувачем"), "orange"
            )
            return

        def confirm_delete_pf(_):
            try:
                suppliers = self.db.get_suppliers(
                    self.company_id, None, manager_id=self.manager_id
                )
                supplier = next(
                    (
                        s
                        for s in suppliers
                        if isinstance(s, dict) and s.get("id") == supplier_id
                    ),
                    None,
                )
                if not supplier:
                    self._show_snack("Постачальника не знайдено", "red")
                    return

                pf = (
                    supplier.get("payment_fields")
                    if isinstance(supplier, dict)
                    else None
                )
                if not isinstance(pf, list):
                    self._show_snack("Немає платіжних полів", "orange")
                    return

                fid = str(field_id or "").strip()
                if not fid:
                    self._show_snack("Не визначено поле", "orange")
                    return

                changed = False
                for row in pf:
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("field_id") or "").strip() != fid:
                        continue
                    img_url = str(row.get("image_url") or "").strip()
                    if img_url:
                        with contextlib.suppress(Exception):
                            self.db.delete_file_by_url(img_url)
                    row["image_url"] = ""
                    changed = True
                    break

                if not changed:
                    self._show_snack("Картинку не знайдено", "orange")
                    return

                ok = self.db.update_supplier(supplier_id, {"payment_fields": pf})
                if ok:
                    self._close_dlg(confirm_dlg)
                    self.load_suppliers()
                    self._show_snack("Картинку видалено", "blue")
                else:
                    self._show_snack("Помилка видалення", "red")
            except Exception as ex:
                logger.exception("Delete payment field image failed")
                self._show_snack(f"Помилка видалення: {ex}", "red")

        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text("Видалити картинку з платіжного поля?"),
            actions=[
                ft.TextButton(self.t("common.no", "Ні"), on_click=lambda _: self._close_dlg(confirm_dlg)),
                ft.TextButton(self.t("common.yes", "Так"), on_click=confirm_delete_pf),
            ],
        )
        self.content_area.page.show_dialog(confirm_dlg)

    def _region_id_by_name(self, name):
        target = (name or "").strip().lower()
        if not target:
            return None
        for row in self.regions_data:
            if not isinstance(row, dict):
                continue
            rid = row.get("id")
            rname = (row.get("name") or "").strip().lower()
            if rid == name or rname == target:
                return rid
        return None

    def _effective_region_id(self):
        name = self.current_region_name
        if not name or name == self.ALL_REGIONS_KEY:
            return None
        resolved = self._region_id_by_name(name)
        if resolved:
            return resolved
        # Refresh regions and retry to avoid stale cache after company/region switch.
        self.regions_data = [
            r
            for r in self.db.get_company_regions(self.company_id)
            if isinstance(r, dict)
        ]
        self._region_name_by_id = {
            r.get("id"): r.get("name")
            for r in self.regions_data
            if isinstance(r, dict) and r.get("id")
        }
        return self._region_id_by_name(name)

    def _debounce_call(self, key, callback, delay=0.12):
        prev = self._debounce_tasks.get(key)
        if prev and not prev.done():
            prev.cancel()

        async def _runner():
            try:
                await asyncio.sleep(delay)
                callback()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Debounce callback failed")

        self._debounce_tasks[key] = asyncio.create_task(_runner())

    def _pick_local_image_file(self):
        if not TK_AVAILABLE:
            raise RuntimeError("Локальний діалог файлів недоступний")
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Оберіть QR-зображення",
            filetypes=[
                ("Зображення", "*.jpg *.jpeg *.png *.gif *.bmp *.webp"),
                ("Усі файли", "*.*"),
            ],
        )
        root.destroy()
        return path

    def _clear_field_errors(self, *controls):
        for control in controls:
            if control is not None and hasattr(control, "error_text"):
                control.error_text = None

    def _mark_required(self, control, message="Поле обов'язкове"):
        if control is not None and hasattr(control, "error_text"):
            control.error_text = message

    def build(self):
        _th = _th_from_view(self)
        global _ACCENT, _MUTED, _BORDER, _PANEL, _CHIP_T, _SOFT_I
        _ACCENT = _th["icon_color"]
        _MUTED = _th["muted"]
        _BORDER = _th["border"]
        _PANEL = _th["panel"]
        _CHIP_T = _th.get("chip_text", "#1E3A8A")
        _SOFT_I = _th.get("chip_bg", "#E0E7FF")
        if not self.content_area.page:
            return
        if self._general_mode and self._general_manager_ids:
            from views.general_wrapper import build_general_sections

            _rid = self._effective_region_id()
            _cid, _db = self.company_id, self.db

            def _count_supps(mid, _db=_db, _cid=_cid, _rid=_rid):
                items = _db.get_suppliers(_cid, _rid, manager_id=mid) or []
                return len(items)

            build_general_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={
                    "current_region_name": self.current_region_name,
                    "quick_filter": self.quick_filter,
                },
                count_getter=_count_supps,
                count_label="Постачальників:",
            )
            return

        if self.current_region_name in {None, "", self.ALL_REGIONS_KEY, "Всі регіони"}:
            from views.general_wrapper import build_region_sections

            _cid2, _db2, _mid2 = self.company_id, self.db, self.manager_id
            _all2 = _db2.get_suppliers(_cid2, None, manager_id=_mid2) or []
            _rcounts: dict[str, int] = {}
            for _s2 in _all2:
                if not isinstance(_s2, dict):
                    continue
                _r2 = str(_s2.get("region_id") or "").strip()
                if _r2:
                    _rcounts[_r2] = _rcounts.get(_r2, 0) + 1

            def _count_rs(rid, _c=_rcounts):
                return _c.get(rid, 0)

            build_region_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={"quick_filter": self.quick_filter},
                count_getter=_count_rs,
                region_kwarg_name="current_region_name",
                count_label="Постачальників:",
            )
            return

        page = self.content_area.page
        self.content_area.controls.clear()

        self._header_loading_indicator = ft.Row(
            [
                ft.ProgressRing(width=16, height=16, stroke_width=2, color=_ACCENT),
                ft.Text("Завантаження...", size=10, color=_MUTED),
            ],
            spacing=6,
            visible=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # Region access and category access are separate grants -- creating
        # or editing a supplier is tied to the category/level it serves, so
        # a manager with zero category ownership in the current region
        # should only be able to browse, not add/edit.
        try:
            self._region_create_allowed = self.db.manager_has_category_access_in_region(
                self.manager_id, self.company_id, self._effective_region_id()
            )
        except Exception:
            self._region_create_allowed = True

        header = ft.Column(
            [
                ft.Row(
                    [
                        ft.Text(
                            self.t("suppliers.title", "Постачальники"), size=self.HEADER_SIZE, weight="bold"
                        ),
                        self._header_loading_indicator,
                        ft.IconButton(
                            icon=ft.Icons.HELP_OUTLINE,
                            icon_size=18,
                            tooltip=self.t("common.help.tooltip", "Довідка"),
                            on_click=lambda _e: self._show_help(),
                        ),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    [
                        ft.FilledButton(
                            self.t("suppliers.add_btn", "Додати постачальника"),
                            icon=ft.Icons.ADD,
                            height=30,
                            style=ft.ButtonStyle(
                                text_style=ft.TextStyle(size=11),
                                padding=ft.padding.symmetric(horizontal=8, vertical=2),
                            ),
                            on_click=lambda _: self._page()
                            and self._page().run_task(self.show_supplier_dialog),
                            visible=not self.read_only_subordinate
                            and self._region_create_allowed,
                        ),
                    ],
                    spacing=6,
                ),
            ],
            spacing=6,
            tight=True,
        )

        self.search_f = ft.TextField(
            hint_text=self.t("suppliers.search_hint", "Швидкий пошук постачальників"),
            prefix_icon=ft.Icons.SEARCH,
            border_radius=10,
            height=36,
            text_size=12,
            content_padding=ft.padding.symmetric(horizontal=10, vertical=6),
            on_change=lambda _: self._debounce_call(
                "suppliers_screen_search", self.load_suppliers
            ),
        )

        # Vertical scroll must be owned by dashboard `content_area`.
        self.suppliers_list = ft.Column(
            spacing=4,
        )
        self.loading_box = ft.Container(
            padding=14,
            visible=True,
            content=ft.Column(
                [
                    ft.Text(self.t("suppliers.loading", "Завантаження постачальників..."), color="grey700"),
                    ft.ProgressRing(width=22, height=22, stroke_width=3),
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        self.content_area.controls.extend(
            [
                header,
                ft.Text(
                    self.t("packaging.superadmin_mode_hint", "Режим: Всі компанії/регіони. Натискайте Редагувати для модалки."),
                    color="grey700",
                    size=self.META_TEXT_SIZE,
                    visible=self.is_superadmin and not self.company_id,
                ),
                ft.Text(
                    self.t("packaging.readonly_mode_hint", "Режим перегляду підлеглого: створення і редагування вимкнено"),
                    color="grey700",
                    size=self.META_TEXT_SIZE,
                    visible=self.read_only_subordinate,
                ),
                ft.Row([self.search_f]),
                ft.Divider(height=6, color="transparent"),
                self.loading_box,
                self.suppliers_list,
            ]
        )
        self.content_area.update()
        page.run_task(self._async_initial_load)

    async def _async_initial_load(self):
        if self._general_mode and self._general_manager_ids:
            return
        if not hasattr(self, "suppliers_list"):
            return
        if self._header_loading_indicator is not None:
            self._header_loading_indicator.visible = True
        if self.content_area.page and hasattr(self.content_area, "update"):
            self.content_area.update()
        await asyncio.sleep(0.01)
        self.load_suppliers()
        if getattr(self, "loading_box", None) is not None:
            self.loading_box.visible = False
        if self._header_loading_indicator is not None:
            self._header_loading_indicator.visible = False
        if self.content_area.page and hasattr(self.content_area, "update"):
            self.content_area.update()
        self._suppliers_ui_ready = True
        with contextlib.suppress(Exception):
            self._suppliers_pulse_ts = self._suppliers_scope_pulse_ts()

    def _suppliers_scope_pulse_ts(self) -> int:
        try:
            out = self.db.get_companies_sync_pulses(
                [self.company_id], ["suppliers", "catalog"]
            ) or {}
            by_company = out.get(self.company_id) if isinstance(out, dict) else {}
            if not isinstance(by_company, dict):
                return 0
            return max((int(v or 0) for v in by_company.values()), default=0)
        except Exception:
            return 0

    async def resume(self) -> None:
        """Called when returning to this cached tab. This view previously
        had NO reload mechanism at all once built once — a supplier change
        made on another device/session never showed up here short of an app
        restart. Cheap pulse check + a real (force_refresh) reload only when
        something genuinely changed."""
        if not getattr(self, "_suppliers_ui_ready", False):
            return

        async def _bg_suppliers_pulse_check():
            try:
                new_pulse = await asyncio.to_thread(self._suppliers_scope_pulse_ts)
                cached_pulse = int(getattr(self, "_suppliers_pulse_ts", 0) or 0)
                if new_pulse > cached_pulse:
                    self._suppliers_pulse_ts = new_pulse
                    self.load_suppliers(force_refresh=True)
            except Exception:
                pass

        _pg = self._page()
        if _pg and hasattr(_pg, "run_task"):
            with contextlib.suppress(Exception):
                _pg.run_task(_bg_suppliers_pulse_check)

    def _compress_image(self, input_path):
        if not Image:
            return input_path
        try:
            temp_output = f"temp_{uuid.uuid4()}.jpg"
            with Image.open(input_path) as img:
                img = img.convert("RGB")
                img.thumbnail((800, 800))
                img.save(temp_output, "JPEG", quality=70)
            return temp_output
        except Exception:
            logger.exception("Compression error")
            return input_path

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

    def load_suppliers(self, force_refresh: bool = False):
        if not hasattr(self, "suppliers_list"):
            return
        self.suppliers_list.controls.clear()
        current_region_id = self._effective_region_id()
        # Recompute once per load — ownership can change between loads
        # (reassignment), but every card in THIS render shares one fetch
        # instead of one `has_entity_owner` round trip per card.
        self._owned_supplier_ids = None
        self._eff_payment_fields_cache = {}

        load_manager_id = self.manager_id
        raw_suppliers = self.db.get_suppliers(
            self.company_id,
            current_region_id,
            manager_id=load_manager_id,
            force_refresh=force_refresh,
        )
        suppliers = raw_suppliers if isinstance(raw_suppliers, list) else []
        self._set_loading_text(f"Постачальники: {len(suppliers)}")

        query = (
            (self.search_f.value or "").strip().lower()
            if hasattr(self, "search_f")
            else ""
        )
        if query:
            filtered_rows = []
            for s in suppliers:
                if not isinstance(s, dict):
                    continue
                hay = " ".join(
                    [
                        str(s.get("name") or ""),
                        str(s.get("phone") or ""),
                        str(s.get("currency") or ""),
                        str(s.get("notes") or ""),
                    ]
                ).lower()
                if query in hay:
                    filtered_rows.append(s)
            suppliers = filtered_rows

        # Apply quick filters
        if self.quick_filter == "incomplete_suppliers":

            def is_value_present(value):
                if value is None:
                    return False
                if isinstance(value, str):
                    return bool(value.strip())
                if isinstance(value, (dict, list)):
                    return len(value) > 0
                if isinstance(value, (int, float)):
                    return value != 0
                return bool(value)

            def has_payment_method_selected_and_filled(supplier_row):
                if not isinstance(supplier_row, dict):
                    return False

                payment_fields = supplier_row.get("payment_fields")
                if isinstance(payment_fields, list):
                    for row in payment_fields:
                        if not isinstance(row, dict):
                            continue
                        if not bool(row.get("enabled")):
                            continue
                        if is_value_present(row.get("text_value")) or is_value_present(
                            row.get("image_url")
                        ):
                            return True

                # Legacy fallback
                if is_value_present(supplier_row.get("swift")):
                    return True
                if is_value_present(supplier_row.get("alipay_id")):
                    return True
                if is_value_present(supplier_row.get("wechat_id")):
                    return True
                if is_value_present(supplier_row.get("alipay_qr")):
                    return True
                if is_value_present(supplier_row.get("wechat_qr")):
                    return True

                data_dict = supplier_row.get("data")
                if isinstance(data_dict, dict):
                    if is_value_present(data_dict.get("swift")):
                        return True
                    if is_value_present(data_dict.get("alipay_id")):
                        return True
                    if is_value_present(data_dict.get("wechat_id")):
                        return True
                    if is_value_present(data_dict.get("alipay_qr")):
                        return True
                    if is_value_present(data_dict.get("wechat_qr")):
                        return True

                return False

            filtered_rows = []
            for s in suppliers:
                if not isinstance(s, dict):
                    continue

                has_name = is_value_present(s.get("name"))
                has_phone = is_value_present(s.get("phone"))
                has_region = is_value_present(s.get("region_id"))
                has_currency = is_value_present(s.get("currency"))
                has_payment = has_payment_method_selected_and_filled(s)

                # Include only if MISSING at least one required field
                is_incomplete = not (
                    has_name
                    and has_phone
                    and has_region
                    and has_currency
                    and has_payment
                )
                if is_incomplete:
                    filtered_rows.append(s)
            suppliers = filtered_rows

        if not suppliers:
            self.suppliers_list.controls.append(
                ft.Container(
                    content=ft.Text(
                        self.t("suppliers.empty_state", "Немає постачальників у вибраному контексті"),
                        color="grey600",
                        size=self.EMPTY_TEXT_SIZE,
                        text_align=ft.TextAlign.LEFT,
                    ),
                    padding=ft.padding.only(left=4, top=6, bottom=6),
                )
            )
        else:
            for s in suppliers:
                if isinstance(s, dict):
                    self.suppliers_list.controls.append(self._create_supplier_card(s))

        with contextlib.suppress(Exception):
            self.suppliers_list.controls.append(
                self._build_archive_section(current_region_id, load_manager_id)
            )

        safe_update_with_retry(self.suppliers_list, self._page())

    def _build_archive_section(self, region_id, manager_id):
        """Згорнута секція архівованих постачальників з можливістю відновлення."""
        archived_rows = (
            self.db.get_suppliers(
                self.company_id,
                region_id,
                manager_id=manager_id,
                archived=True,
            )
            or []
        )
        state_key = str(self.company_id or "")
        collapsed = SuppliersView._archive_collapsed_by_company.get(state_key, True)

        def _toggle(_):
            SuppliersView._archive_collapsed_by_company[state_key] = not collapsed
            self.load_suppliers()

        header = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(
                        ft.Icons.EXPAND_MORE if not collapsed else ft.Icons.CHEVRON_RIGHT,
                        size=18,
                        color="grey700",
                    ),
                    ft.Icon(ft.Icons.ARCHIVE_OUTLINED, size=16, color="grey700"),
                    ft.Text(
                        self.t("suppliers.archive_section", "Архів") + f" ({len(archived_rows)})",
                        size=13,
                        weight="bold",
                        color="grey700",
                    ),
                ],
                spacing=6,
            ),
            padding=ft.padding.symmetric(horizontal=8, vertical=8),
            on_click=_toggle,
            ink=True,
        )
        controls = [header]
        if not collapsed:
            if not archived_rows:
                controls.append(
                    ft.Container(
                        padding=ft.padding.only(left=28, bottom=6),
                        content=ft.Text(
                            self.t("suppliers.archive_empty", "Архів порожній"),
                            color="grey600",
                            size=12,
                        ),
                    )
                )
            else:
                for row in archived_rows:
                    if not isinstance(row, dict):
                        continue
                    with contextlib.suppress(Exception):
                        controls.append(self._create_supplier_card(row, is_archived=True))
        return ft.Container(
            content=ft.Column(controls, spacing=2),
            margin=ft.margin.only(top=10),
            border=ft.border.only(top=ft.BorderSide(1, "grey300")),
        )

    def _create_supplier_card(self, s, is_archived: bool = False):
        region_name = self._region_name_by_id.get(s.get("region_id"), "Незн. регіон")
        manager_name = self._resolve_manager_name(
            s.get("company_id"), s.get("manager_id")
        )
        can_modify = self._can_modify_supplier(s)

        def open_qr_view(url, title):
            u = str(url or "").strip()
            if not u:
                return
            src = self.db.resolve_image_file_path(u)
            qr_img = ft.Image(src=src, width=400, height=400, fit=ft.BoxFit.CONTAIN)

            async def _do_save():
                pg = self.content_area.page
                if not pg:
                    return
                try:
                    with open(src, "rb") as fh:
                        data = fh.read()
                except Exception as ex:
                    self._show_snack(
                        self.t("suppliers.err.read_file", "Помилка читання файлу: {err}").format(err=ex),
                        "red",
                    )
                    return
                # The cached file's own extension can be the generic ".bin"
                # fallback (resolve_image_file_path couldn't tell the image
                # type from a missing/unrecognized content_type) — sniff the
                # real format from the bytes so the saved file still opens as
                # a picture, not an unrecognized file.
                ext = os.path.splitext(src)[1].lower()
                if ext in ("", ".bin"):
                    ext = _sniff_image_ext(data)
                supplier_part = _sanitize_filename_part(s.get("name"))
                title_part = _sanitize_filename_part(title)
                fname = f"{supplier_part} {title_part}{ext}"
                fp = ft.FilePicker()
                pg.services.append(fp)
                pg.update()
                await asyncio.sleep(0.2)
                save_path = await fp.save_file(
                    dialog_title=self.t("suppliers.save_photo_dialog", 'Зберегти "{fname}"').format(fname=fname),
                    file_name=fname,
                    allowed_extensions=[ext.lstrip(".")] if ext else None,
                )
                with contextlib.suppress(Exception):
                    pg.services.remove(fp)
                    pg.update()
                if not save_path:
                    return
                try:
                    with open(save_path, "wb") as fh:
                        fh.write(data)
                    self._show_snack(
                        self.t("suppliers.photo_saved", "Збережено: {name}").format(
                            name=os.path.basename(save_path)
                        ),
                        "green",
                    )
                except Exception as ex:
                    self._show_snack(
                        self.t("suppliers.err.save_file", "Помилка збереження: {err}").format(err=ex),
                        "red",
                    )

            def _run_save(_e=None):
                pg = self.content_area.page
                if pg and hasattr(pg, "run_task"):
                    pg.run_task(_do_save)

            qr_dlg = ft.AlertDialog(
                title=ft.Text(title),
                content=ft.Container(
                    content=qr_img,
                    alignment=ft.Alignment(0.5, 0.5),
                    width=400,
                    height=400,
                ),
                actions=[
                    ft.TextButton(
                        self.t("suppliers.save_photo", "Зберегти"),
                        icon=ft.Icons.SAVE_ALT,
                        on_click=_run_save,
                        disabled=not (src and os.path.exists(src)),
                    ),
                    ft.TextButton(self.t("packaging.dlg.close", "Закрити"), on_click=lambda _: self._close_dlg(qr_dlg)),
                ],
            )
            self.content_area.page.show_dialog(qr_dlg)

        payment_imgs = self._get_effective_payment_image_entries(s)
        return ft.Row(
            [
                ft.Container(
                    expand=True,
                    margin=ft.margin.only(bottom=6),
                    padding=8,
                    border=ft.border.all(1, _BORDER),
                    border_radius=10,
                    bgcolor=_PANEL,
                    content=ft.Row(
                        [
                            ft.Column(
                                [
                                    ft.Row(
                                        [
                                            ft.Text(
                                                s.get("name", "Без назви"),
                                                weight="bold",
                                                size=13,
                                                selectable=True,
                                            ),
                                            ft.Container(
                                                content=ft.Text(
                                                    region_name,
                                                    size=9,
                                                    weight="bold",
                                                    color="white",
                                                ),
                                                bgcolor="bluegrey400",
                                                padding=ft.padding.symmetric(
                                                    horizontal=6, vertical=1
                                                ),
                                                border_radius=5,
                                            ),
                                            ft.Container(
                                                content=ft.Text(
                                                    manager_name,
                                                    size=9,
                                                    weight="bold",
                                                    color=_CHIP_T,
                                                ),
                                                bgcolor=_SOFT_I,
                                                padding=ft.padding.symmetric(
                                                    horizontal=6, vertical=1
                                                ),
                                                border_radius=5,
                                                visible=bool(manager_name),
                                            ),
                                            ft.Container(
                                                content=ft.Text(
                                                    self._normalize_currency_code(
                                                        s.get("currency", "USD"), "USD"
                                                    ),
                                                    size=9,
                                                    weight="bold",
                                                    color=_CHIP_T,
                                                ),
                                                bgcolor=_SOFT_I,
                                                padding=ft.padding.symmetric(
                                                    horizontal=6, vertical=1
                                                ),
                                                border_radius=5,
                                            ),
                                        ],
                                        spacing=7,
                                    ),
                                    # Payment method badges row
                                    ft.Row(
                                        [
                                            ft.Container(
                                                content=ft.Text(
                                                    method_name,
                                                    size=9,
                                                    weight="bold",
                                                    color="#1F2937",
                                                ),
                                                bgcolor="#FDD835",
                                                padding=ft.padding.symmetric(
                                                    horizontal=6, vertical=2
                                                ),
                                                border_radius=5,
                                            )
                                            for method_name in self._get_enabled_payment_method_names(
                                                s
                                            )
                                        ],
                                        spacing=5,
                                        wrap=True,
                                    ),
                                    ft.Row(
                                        [
                                            ft.Icon(ft.Icons.PHONE, size=12),
                                            ft.Text(s.get("phone", "-"), size=11),
                                        ],
                                        spacing=5,
                                    ),
                                    ft.Column(
                                        [
                                            ft.Row(
                                                [
                                                    ft.TextButton(
                                                        img.get("title") or "QR",
                                                        icon=ft.Icons.QR_CODE,
                                                        style=ft.ButtonStyle(
                                                            text_style=ft.TextStyle(
                                                                size=11
                                                            ),
                                                            padding=ft.padding.symmetric(
                                                                horizontal=6, vertical=0
                                                            ),
                                                        ),
                                                        on_click=lambda _, u=img.get(
                                                            "url"
                                                        ), t=img.get(
                                                            "title"
                                                        ): open_qr_view(
                                                            u, t
                                                        ),
                                                    ),
                                                    ft.IconButton(
                                                        ft.Icons.DELETE,
                                                        icon_color="red",
                                                        tooltip="Видалити QR",
                                                        visible=not self.read_only_subordinate,
                                                        on_click=lambda _, src=img.get(
                                                            "source"
                                                        ): (
                                                            self.delete_qr_image(
                                                                s.get("id"), src
                                                            )
                                                            if src
                                                            in {
                                                                "alipay_qr",
                                                                "wechat_qr",
                                                            }
                                                            else self.delete_payment_field_image(
                                                                s.get("id"), src
                                                            )
                                                        ),
                                                    ),
                                                ],
                                                spacing=4,
                                            )
                                            for img in (payment_imgs or [])
                                            if isinstance(img, dict) and img.get("url")
                                        ],
                                        spacing=4,
                                    ),
                                    ft.Text(
                                        f"Нотатки: {s.get('notes', '')}",
                                        size=10,
                                        italic=True,
                                        visible=bool(s.get("notes")),
                                    ),
                                ],
                                expand=True,
                            ),
                            ft.Row(
                                [
                                    ft.OutlinedButton(
                                        self.t("packaging.card.view_btn", "Переглянути"),
                                        icon=ft.Icons.VISIBILITY,
                                        height=28,
                                        style=ft.ButtonStyle(
                                            text_style=ft.TextStyle(size=11),
                                            padding=ft.padding.symmetric(
                                                horizontal=7, vertical=1
                                            ),
                                        ),
                                        on_click=lambda _, _s=s: self._page()
                                        and self._page().run_task(
                                            self.show_supplier_details_dialog, _s
                                        ),
                                    ),
                                    ft.OutlinedButton(
                                        self.t("packaging.card.edit_btn", "Редагувати"),
                                        icon=ft.Icons.EDIT,
                                        height=28,
                                        style=ft.ButtonStyle(
                                            text_style=ft.TextStyle(size=11),
                                            padding=ft.padding.symmetric(
                                                horizontal=7, vertical=1
                                            ),
                                        ),
                                        visible=not self.read_only_subordinate
                                        and getattr(self, "_region_create_allowed", True),
                                        disabled=not can_modify,
                                        tooltip=(
                                            "Запис закріплений за іншим користувачем"
                                            if not can_modify
                                            else None
                                        ),
                                        on_click=lambda _, _s=s: self._page()
                                        and self._page().run_task(self.show_supplier_dialog, _s),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.UNARCHIVE_OUTLINED
                                        if is_archived
                                        else ft.Icons.ARCHIVE_OUTLINED,
                                        icon_color="blue" if is_archived else "grey700",
                                        tooltip=(
                                            (
                                                self.t("suppliers.restore", "Відновити з архіву")
                                                if is_archived
                                                else self.t("suppliers.archive", "Архівувати")
                                            )
                                            if can_modify
                                            else "Запис закріплений за іншим користувачем"
                                        ),
                                        visible=not self.read_only_subordinate
                                        and getattr(self, "_region_create_allowed", True),
                                        disabled=not can_modify,
                                        on_click=(
                                            (lambda _: self.restore_supplier(s.get("id")))
                                            if is_archived
                                            else (lambda _: self.archive_supplier(s.get("id")))
                                        ),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.DELETE_OUTLINE,
                                        icon_color="red",
                                        tooltip=(
                                            "Видалити назавжди"
                                            if can_modify
                                            else "Запис закріплений за іншим користувачем"
                                        ),
                                        visible=not self.read_only_subordinate
                                        and self._is_admin_or_superadmin(),
                                        disabled=not can_modify,
                                        on_click=lambda _, sid=s.get("id"): self.delete_supplier(
                                            sid
                                        ),
                                    ),
                                ],
                                spacing=4,
                                wrap=True,
                            ),
                        ]
                    ),
                )
            ],
            expand=True,
        )

    def _close_dlg(self, dlg):
        page = self._page()
        if not page:
            logger.warning("[modal:suppliers] close skipped: page is None")
            return
        logger.info(
            "[modal:suppliers] close:start dlg=%s@%s open=%s overlay_before=%s",
            type(dlg).__name__,
            id(dlg),
            getattr(dlg, "open", None),
            len(getattr(page, "overlay", []) or []),
        )
        page.pop_dialog()
        logger.info(
            "[modal:suppliers] close:finish dlg=%s@%s open=%s overlay_after=%s",
            type(dlg).__name__,
            id(dlg),
            getattr(dlg, "open", None),
            len(getattr(page, "overlay", []) or []),
        )

    async def show_supplier_details_dialog(self, supplier):
        """Builds the (DB-heavy) supplier card dialog off the UI thread, then
        shows it — see ProductsView.show_product_details_dialog for the same
        fix and why it matters on Android."""
        dlg = await asyncio.to_thread(self._build_supplier_details_dialog, supplier)
        self.content_area.page.show_dialog(dlg)

    def _build_supplier_details_dialog(self, supplier) -> ft.AlertDialog:
        supplier = supplier if isinstance(supplier, dict) else {}
        company_id = supplier.get("company_id") or self.company_id
        region_name = self._region_name_by_id.get(
            supplier.get("region_id"), "Незн. регіон"
        )
        company_name = next(
            (
                row.get("name")
                for row in self.db.get_all_companies() or []
                if isinstance(row, dict) and row.get("id") == company_id
            ),
            "",
        )
        manager_name = self._resolve_manager_name(
            company_id, supplier.get("manager_id")
        )

        def _value_row(label, value):
            return ft.Container(
                expand=True,
                content=ft.Column(
                    [
                        ft.Text(label, size=11, color="grey700"),
                        ft.Text(
                            value or "-",
                            size=14,
                            selectable=True,
                            no_wrap=False,
                            max_lines=None,
                            overflow=ft.TextOverflow.VISIBLE,
                        ),
                    ],
                    spacing=2,
                ),
            )

        def _qr_block(title, url):
            if not url:
                return ft.Column(
                    [ft.Text(title, weight="bold"), ft.Text("Немає", color="grey600")],
                    spacing=6,
                )
            u = str(url or "").strip()
            image_src = self.db.resolve_image_file_path(u)
            return ft.Column(
                [
                    ft.Text(title, weight="bold", size=13),
                    ft.Container(
                        content=ft.Image(
                            src=image_src,
                            width=220,
                            height=220,
                            fit=ft.BoxFit.CONTAIN,
                        ),
                        width=230,
                        height=230,
                        border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
                        border_radius=10,
                        padding=8,
                    ),
                ],
                spacing=6,
            )

        def _payment_display_controls():
            def _payment_value_row(label, value):
                return ft.Container(
                    expand=True,
                    content=ft.Column(
                        [
                            ft.Text(
                                label,
                                size=13,
                                weight="bold",
                                color="grey800",
                            ),
                            ft.Text(
                                value or "-",
                                size=14,
                                selectable=True,
                                no_wrap=False,
                                max_lines=None,
                                overflow=ft.TextOverflow.VISIBLE,
                            ),
                        ],
                        spacing=2,
                    ),
                )

            company_id_local = company_id
            legacy_by_code = {
                "swift": {"text_value": supplier.get("swift") or "", "image_url": ""},
                "alipay_id": {
                    "text_value": supplier.get("alipay_id") or "",
                    "image_url": "",
                },
                "wechat_id": {
                    "text_value": supplier.get("wechat_id") or "",
                    "image_url": "",
                },
                "alipay_qr": {
                    "text_value": "",
                    "image_url": supplier.get("alipay_qr") or "",
                },
                "wechat_qr": {
                    "text_value": "",
                    "image_url": supplier.get("wechat_qr") or "",
                },
            }
            by_id = {
                str(r.get("field_id")): r
                for r in (supplier.get("payment_fields") or [])
                if isinstance(r, dict) and r.get("field_id")
            }
            eff = (
                self.db.get_effective_payment_fields_for_manager(
                    company_id_local,
                    self.manager_id,
                    region_id=supplier.get("region_id"),
                )
                if company_id_local
                else []
            )
            # If no templates configured, show legacy view.
            if not eff:
                rows = []
                if (supplier.get("swift") or "").strip():
                    rows.append(_value_row("SWIFT / Реквізити", supplier.get("swift")))
                if (supplier.get("alipay_id") or "").strip() or (
                    supplier.get("wechat_id") or ""
                ).strip():
                    rows.append(
                        ft.Row(
                            [
                                _value_row("Alipay ID", supplier.get("alipay_id")),
                                _value_row("WeChat ID", supplier.get("wechat_id")),
                            ],
                            spacing=20,
                            wrap=True,
                        )
                    )
                imgs = []
                if supplier.get("alipay_qr"):
                    imgs.append(_qr_block("Alipay QR", supplier.get("alipay_qr")))
                if supplier.get("wechat_qr"):
                    imgs.append(_qr_block("WeChat QR", supplier.get("wechat_qr")))
                if imgs:
                    rows.append(ft.Row(imgs, spacing=14, wrap=True))
                return (
                    [ft.Text("Дані для оплат", weight="bold", size=13)] + rows
                    if rows
                    else []
                )

            out_rows = []
            for f in eff or []:
                if not isinstance(f, dict) or not f.get("id"):
                    continue
                fid = str(f.get("id"))
                code = str(f.get("code") or "").strip().lower()
                name = str(f.get("name") or "").strip() or code or "Поле"
                if fid in by_id and not bool(
                    (by_id.get(fid) or {}).get("enabled", True)
                ):
                    continue
                src_row = (
                    (by_id.get(fid) if fid in by_id else None)
                    or legacy_by_code.get(code)
                    or {}
                )
                if bool(f.get("has_text")):
                    txt = str(src_row.get("text_value") or "").strip()
                    if txt:
                        out_rows.append(_payment_value_row(name, txt))

            img_entries = self._get_effective_payment_image_entries(supplier)
            out_imgs = [
                _qr_block(str(e.get("title") or "QR"), e.get("url"))
                for e in (img_entries or [])
                if isinstance(e, dict) and e.get("url")
            ]

            controls = []
            if out_rows or out_imgs:
                controls.append(ft.Text("Дані для оплат", weight="bold", size=13))
                controls.extend(out_rows)
                if out_imgs:
                    controls.append(ft.Row(out_imgs, spacing=14, wrap=True))
            return controls

        def _view_help(_):
            from views.help_utils import show_modal_help
            _pg = self._page()
            show_modal_help(_pg, self.t("suppliers.view_help.title", "Перегляд постачальника"), [
                self.t("suppliers.view_help.step1", "1. Тут відображаються всі дані постачальника."),
                self.t("suppliers.view_help.step2", "2. QR-коди можна збільшити, натиснувши на них."),
                self.t("suppliers.view_help.step3", "3. Натисніть «Закрити», щоб повернутись до списку."),
            ])

        dlg = ft.AlertDialog(
            title=ft.Row(
                [
                    ft.Text(f"Перегляд постачальника: {supplier.get('name') or 'Без назви'}", selectable=True, expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_view_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Container(
                width=760,
                height=620,
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Container(
                                    content=ft.Text(
                                        region_name,
                                        size=10,
                                        weight="bold",
                                        color="white",
                                    ),
                                    bgcolor="bluegrey400",
                                    padding=ft.padding.symmetric(
                                        horizontal=8, vertical=2
                                    ),
                                    border_radius=5,
                                ),
                                ft.Container(
                                    content=ft.Text(
                                        manager_name,
                                        size=10,
                                        weight="bold",
                                        color=_CHIP_T,
                                    ),
                                    bgcolor=_SOFT_I,
                                    padding=ft.padding.symmetric(
                                        horizontal=8, vertical=2
                                    ),
                                    border_radius=5,
                                    visible=bool(manager_name),
                                ),
                                ft.Container(
                                    content=ft.Text(
                                        self._normalize_currency_code(
                                            supplier.get("currency") or "USD", "USD"
                                        ),
                                        size=10,
                                        weight="bold",
                                        color=_CHIP_T,
                                    ),
                                    bgcolor=_SOFT_I,
                                    padding=ft.padding.symmetric(
                                        horizontal=8, vertical=2
                                    ),
                                    border_radius=5,
                                ),
                            ],
                            spacing=8,
                            wrap=True,
                        ),
                        ft.Row(
                            [
                                _value_row("Компанія", company_name),
                                _value_row("Телефон", supplier.get("phone")),
                            ],
                            spacing=20,
                            wrap=True,
                        ),
                        _value_row("Нотатки", supplier.get("notes")),
                        ft.Divider(),
                        *_payment_display_controls(),
                    ],
                    spacing=12,
                    scroll=ft.ScrollMode.ADAPTIVE,
                ),
            ),
            actions=[ft.TextButton(self.t("packaging.dlg.close", "Закрити"), on_click=lambda _: self._close_dlg(dlg))],
        )
        return dlg

    async def show_supplier_dialog(self, supplier=None):
        """Builds the (DB-heavy) supplier form off the UI thread, then shows
        it — see ProductsView.show_product_dialog for the same fix and why
        it matters on Android. The fast, non-DB permission checks run here
        on the main thread first (cheap, and _show_snack/page.update() must
        not be called from the background thread the builder runs in)."""
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return
        if not getattr(self, "_region_create_allowed", True):
            self._show_snack(
                self.t(
                    "products.no_category_access",
                    "Немає доступу до категорій у цьому регіоні — лише перегляд",
                ),
                "orange",
            )
            return

        result = await asyncio.to_thread(self._build_supplier_dialog, supplier)
        if isinstance(result, tuple):
            _, error_msg = result
            self._show_snack(error_msg, "orange")
            return
        if result is not None:
            self.content_area.page.show_dialog(result)

    def _build_supplier_dialog(self, supplier=None):
        if (
            isinstance(supplier, dict)
            and supplier.get("id")
            and not self._can_modify_supplier(supplier)
        ):
            return None, self.t(
                "suppliers.locked_by_other", "Цей постачальник закріплений за іншим користувачем"
            )

        is_edit = isinstance(supplier, dict)
        if not is_edit:
            supplier = {}

        self.temp_alipay_path = None
        self.temp_wechat_path = None

        companies = self.db.get_all_companies() if self.is_superadmin else []
        company_options = [
            ft.dropdown.Option(key=c.get("id"), text=c.get("name"))
            for c in companies
            if isinstance(c, dict) and c.get("id")
        ]
        default_company_id = (
            supplier.get("company_id")
            or (self.company_id if not is_edit else "")
            or (company_options[0].key if company_options else None)
        )

        company_f = ft.Dropdown(
            label="Компанія",
            options=(
                ([ft.dropdown.Option(key="", text="—")] + company_options)
                if self.is_superadmin
                else company_options
            ),
            value=default_company_id,
            border_radius=10,
            visible=self.is_superadmin,
        )

        def _get_work_company_id():
            return company_f.value if self.is_superadmin else self.company_id

        def _regions_for(company_id):
            rows = self.db.get_company_regions(company_id)
            return [r for r in rows if isinstance(r, dict) and r.get("id")]

        region_name_cache = {}

        def _region_name_for(company_id, region_id):
            key = ((company_id or "").strip(), (region_id or "").strip())
            if key in region_name_cache:
                return region_name_cache[key]
            label = "іншому регіоні"
            for r in _regions_for(company_id):
                if r.get("id") == region_id:
                    label = r.get("name") or label
                    break
            region_name_cache[key] = label
            return label

        name_f = ft.TextField(
            label=self.t("suppliers.field.name", "Ім'я постачальника"), value=supplier.get("name", ""), border_radius=10
        )
        phone_f = ft.TextField(
            label=self.t("suppliers.field.phone", "Номер телефону"), value=supplier.get("phone", ""), border_radius=10
        )
        # Legacy fields (kept for fallback if company templates are not configured)
        swift_f = ft.TextField(
            label="SWIFT / Реквізити",
            value=supplier.get("swift", ""),
            border_radius=10,
            multiline=True,
            min_lines=1,
            max_lines=3,
        )
        alipay_id_f = ft.TextField(
            label="Alipay ID", value=supplier.get("alipay_id", ""), border_radius=10
        )
        wechat_id_f = ft.TextField(
            label="WeChat ID", value=supplier.get("wechat_id", ""), border_radius=10
        )
        alipay_qr_status = ft.Text(
            (
                f"Поточний файл: {os.path.basename(supplier.get('alipay_qr', ''))}"
                if supplier.get("alipay_qr")
                else "QR Alipay не завантажено"
            ),
            size=11,
            color="grey700",
        )
        wechat_qr_status = ft.Text(
            (
                f"Поточний файл: {os.path.basename(supplier.get('wechat_qr', ''))}"
                if supplier.get("wechat_qr")
                else "QR WeChat не завантажено"
            ),
            size=11,
            color="grey700",
        )

        # Dynamic payment fields (company templates + manager overrides)
        payment_section = ft.Column(spacing=8)
        payment_rows = []
        temp_payment_images = {}  # field_id -> local_path
        current_payment_images = {}  # field_id -> existing url

        # Which of the region's delivery methods this supplier's goods
        # actually ship by — used to narrow the cost-price/margin table on
        # the product card and the delivery-type choices in Orders down to
        # just what applies to this supplier, instead of every method the
        # company has for the region. An empty/missing list means
        # "unrestricted" (every method applies) — this is the default for
        # both brand-new suppliers and every supplier that existed before
        # this feature, so nothing already filled in changes behavior until
        # someone explicitly unchecks something here.
        delivery_methods_section = ft.Column(spacing=4)
        delivery_methods_checks: dict = {}

        def _rebuild_delivery_methods_section():
            delivery_methods_checks.clear()
            work_company_id = _get_work_company_id()
            region_id = region_f.value or supplier.get("region_id")
            methods = (
                self.db.get_company_delivery_methods(work_company_id, region_id)
                if work_company_id and region_id
                else []
            )
            methods = [m for m in methods if isinstance(m, dict) and m.get("id")]
            if not methods:
                delivery_methods_section.controls = [
                    ft.Text(
                        "Оберіть регіон — спершу там мають бути налаштовані способи доставки",
                        size=11,
                        color="grey600",
                    )
                ]
                return
            existing_ids = supplier.get("enabled_delivery_method_ids")
            restricted = isinstance(existing_ids, list) and len(existing_ids) > 0
            allowed_ids = set(existing_ids) if restricted else set()
            rows = []
            for m in methods:
                mid = m.get("id")
                cb = ft.Checkbox(
                    label=str(m.get("name") or mid),
                    value=(not restricted) or (mid in allowed_ids),
                )
                delivery_methods_checks[mid] = cb
                rows.append(cb)
            delivery_methods_section.controls = [
                ft.Text(
                    "Якими способами доставки веземо товар цього постачальника",
                    weight="bold",
                    size=13,
                ),
                ft.Text(
                    "Впливає на таблицю собівартості/рентабельності товару і на "
                    "вибір способу доставки в замовленнях. Позначте лише "
                    "актуальні для цього постачальника — за замовчуванням "
                    "враховуються всі.",
                    size=11,
                    color="grey600",
                ),
                *rows,
            ]

        def _legacy_payment_fields_map():
            return {
                "swift": {"text_value": supplier.get("swift") or "", "image_url": ""},
                "alipay_id": {
                    "text_value": supplier.get("alipay_id") or "",
                    "image_url": "",
                },
                "wechat_id": {
                    "text_value": supplier.get("wechat_id") or "",
                    "image_url": "",
                },
                "alipay_qr": {
                    "text_value": "",
                    "image_url": supplier.get("alipay_qr") or "",
                },
                "wechat_qr": {
                    "text_value": "",
                    "image_url": supplier.get("wechat_qr") or "",
                },
            }

        def _rebuild_payment_section():
            nonlocal payment_rows
            payment_rows = []
            payment_section.controls = []
            work_company_id = _get_work_company_id()
            effective_fields = (
                self.db.get_effective_payment_fields_for_manager(
                    work_company_id,
                    self.manager_id,
                    region_id=region_f.value or supplier.get("region_id"),
                )
                if work_company_id
                else []
            )

            # Existing supplier values
            existing_list = (
                supplier.get("payment_fields") if isinstance(supplier, dict) else None
            )
            by_id = {}
            if isinstance(existing_list, list):
                for row in existing_list:
                    if isinstance(row, dict) and row.get("field_id"):
                        by_id[str(row["field_id"])] = row

            legacy_by_code = _legacy_payment_fields_map()

            if not effective_fields:
                # Fallback to old static UI
                payment_section.controls = [
                    ft.Text("Дані для оплат", weight="bold", size=14),
                    swift_f,
                    alipay_id_f,
                    ft.Row(
                        [
                            ft.OutlinedButton(
                                "Завантажити QR Alipay",
                                icon=ft.Icons.UPLOAD_FILE,
                                tooltip="Завантажити Alipay QR",
                                height=36,
                                on_click=lambda _: asyncio.create_task(
                                    start_picking("alipay")
                                ),
                            ),
                            alipay_qr_status,
                        ],
                        spacing=10,
                        wrap=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    wechat_id_f,
                    ft.Row(
                        [
                            ft.OutlinedButton(
                                "Завантажити QR WeChat",
                                icon=ft.Icons.UPLOAD_FILE,
                                tooltip="Завантажити WeChat QR",
                                height=36,
                                on_click=lambda _: asyncio.create_task(
                                    start_picking("wechat")
                                ),
                            ),
                            wechat_qr_status,
                        ],
                        spacing=10,
                        wrap=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ]
                return

            payment_section.controls.append(
                ft.Text("Дані для оплат", weight="bold", size=14)
            )

            async def _pick_payment_image(field_id: str, status_text: ft.Text):
                try:
                    local_path = self._pick_local_image_file()
                    if local_path:
                        temp_payment_images[field_id] = local_path
                        status_text.value = (
                            f"Вибрано файл: {os.path.basename(local_path)}"
                        )
                        with contextlib.suppress(Exception):
                            status_text.update()
                except Exception:
                    logger.exception("Pick payment image failed")
                    self._show_snack("Не вдалося вибрати файл", "red")

            for f in effective_fields:
                if not isinstance(f, dict) or not f.get("id"):
                    continue
                fid = str(f.get("id"))
                code = str(f.get("code") or "").strip().lower()
                fname = str(f.get("name") or "").strip() or code or "Поле"
                has_text = bool(f.get("has_text"))
                has_image = bool(f.get("has_image"))

                existing = by_id.get(fid) or {}
                used_legacy = False
                if fid not in by_id and code and code in legacy_by_code:
                    existing = legacy_by_code.get(code) or {}
                    used_legacy = True

                if fid in by_id:
                    enabled_val = bool((by_id.get(fid) or {}).get("enabled", True))
                else:
                    enabled_val = (
                        bool(
                            (existing.get("text_value") or "").strip()
                            or (existing.get("image_url") or "").strip()
                        )
                        if used_legacy
                        else False
                    )

                enabled_cb = ft.Checkbox(
                    label=fname, value=enabled_val, disabled=self.read_only_subordinate
                )
                text_f = None
                img_status = None
                img_btn = None

                if has_text:
                    text_f = ft.TextField(
                        label=fname,
                        value=str(existing.get("text_value") or ""),
                        border_radius=10,
                        multiline=False,
                        disabled=self.read_only_subordinate,
                        visible=bool(enabled_cb.value),
                    )
                if has_image:
                    existing_url = str(existing.get("image_url") or "")
                    current_payment_images[fid] = existing_url
                    img_status = ft.Text(
                        (
                            f"Поточний файл: {os.path.basename(existing_url)}"
                            if existing_url
                            else "Файл не завантажено"
                        ),
                        size=11,
                        color="grey700",
                        visible=bool(enabled_cb.value),
                    )
                    img_btn = ft.OutlinedButton(
                        f"Завантажити {fname}",
                        icon=ft.Icons.UPLOAD_FILE,
                        height=36,
                        disabled=self.read_only_subordinate,
                        visible=bool(enabled_cb.value),
                        on_click=lambda _, _fid=fid, _st=img_status: asyncio.create_task(
                            _pick_payment_image(_fid, _st)
                        ),
                    )

                def _toggle(
                    _=None, cb=enabled_cb, tf=text_f, st=img_status, btn=img_btn
                ):
                    v = bool(cb.value)
                    if tf is not None:
                        tf.visible = v
                        with contextlib.suppress(Exception):
                            tf.update()
                    if st is not None:
                        st.visible = v
                        with contextlib.suppress(Exception):
                            st.update()
                    if btn is not None:
                        btn.visible = v
                        with contextlib.suppress(Exception):
                            btn.update()

                enabled_cb.on_change = _toggle
                _toggle()

                payment_rows.append(
                    {
                        "field_id": fid,
                        "code": code,
                        "name": fname,
                        "has_text": has_text,
                        "has_image": has_image,
                        "enabled_cb": enabled_cb,
                        "text_f": text_f,
                    }
                )

                block_controls = [enabled_cb]
                if text_f is not None:
                    block_controls.append(text_f)
                if img_btn is not None and img_status is not None:
                    block_controls.append(
                        ft.Row(
                            [img_btn, img_status],
                            spacing=10,
                            wrap=True,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        )
                    )

                payment_section.controls.append(ft.Column(block_controls, spacing=6))

        default_region_id = supplier.get("region_id")
        region_rows = _regions_for(default_company_id)
        # In edit mode: do NOT auto-pick current region if missing; show as saved (even if empty).
        if (
            (not is_edit)
            and (not default_region_id)
            and self.current_region_name
            and self.current_region_name != "__all_regions__"
        ):
            for r in region_rows:
                if r.get("name") == self.current_region_name:
                    default_region_id = r.get("id")
                    break
        if (not is_edit) and (not default_region_id) and region_rows:
            default_region_id = region_rows[0].get("id")

        region_f = ft.Dropdown(
            label="Регіон / Філія",
            options=(
                [ft.dropdown.Option(key="", text="—")]
                + [
                    ft.dropdown.Option(key=r.get("id"), text=r.get("name"))
                    for r in region_rows
                ]
            ),
            value=default_region_id,
            border_radius=10,
        )
        currency_f = ft.Dropdown(
            label=self.t("orders.field.currency", "Валюта"),
            options=[ft.dropdown.Option("")]
            + [ft.dropdown.Option(c) for c in self.available_currencies],
            value=(
                self._normalize_currency_code(
                    supplier.get("currency"), self.available_currencies[0]
                )
                if (not is_edit or supplier.get("currency"))
                else ""
            ),
            border_radius=10,
        )
        notes_f = ft.TextField(
            label=self.t("products.field.notes", "Нотатки"),
            value=supplier.get("notes", ""),
            border_radius=10,
            multiline=True,
        )

        quick_fill_btn = ft.Container(visible=False)

        def apply_quick_fill(data_match):
            name_f.value = data_match.get("name", name_f.value)
            phone_f.value = data_match.get("phone", "")
            swift_f.value = data_match.get("swift", "")
            alipay_id_f.value = data_match.get("alipay_id", "")
            wechat_id_f.value = data_match.get("wechat_id", "")
            currency_f.value = self._normalize_currency_code(
                data_match.get("currency", self.available_currencies[0]),
                self.available_currencies[0],
            )
            notes_f.value = data_match.get("notes", "")
            quick_fill_btn.visible = False
            with contextlib.suppress(Exception):
                quick_fill_btn.update()
            with contextlib.suppress(Exception):
                name_f.update()
            with contextlib.suppress(Exception):
                phone_f.update()

        def check_existing_supplier(e):
            error_text.visible = False  # Clear error when name changes
            query = name_f.value.strip()

            def _run_search():
                if not is_edit and len(query) > 1:
                    work_company_id = _get_work_company_id()
                    matches = (
                        self.db.search_suppliers_company(work_company_id, query, 5)
                        if work_company_id
                        else []
                    )
                    if matches:
                        controls = [
                            ft.Text(
                                (
                                    f"Знайдено {len(matches)} варіантів:"
                                    if len(matches) > 1
                                    else "Знайдено 1 варіант:"
                                ),
                                size=12,
                            )
                        ]
                        for match in matches:
                            reg_name = _region_name_for(
                                match.get("company_id"), match.get("region_id")
                            )
                            supplier_name = match.get("name", "постачальника")
                            controls.append(
                                ft.Row(
                                    [
                                        ft.Text(
                                            f"{supplier_name} у {reg_name}", size=12
                                        ),
                                        ft.ElevatedButton(
                                            "Копіювати",
                                            icon=ft.Icons.COPY_ALL,
                                            on_click=lambda _, m=match: apply_quick_fill(
                                                m
                                            ),
                                        ),
                                    ],
                                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                    spacing=10,
                                )
                            )
                        quick_fill_btn.content = ft.Column(controls, spacing=5)
                        quick_fill_btn.visible = True
                    else:
                        quick_fill_btn.visible = False
                else:
                    quick_fill_btn.visible = False
                with contextlib.suppress(Exception):
                    quick_fill_btn.update()

            self._debounce_call("suppliers_dialog_search", _run_search)

        name_f.on_change = check_existing_supplier

        # --- User assignment section (admin/superadmin creating new supplier) ---
        user_checks = {}  # manager_id -> ft.Checkbox
        users_col = ft.Column([], spacing=4, visible=False)
        users_expanded = [False]
        users_loaded = [False]

        def _rebuild_users_for_company(cid):
            user_checks.clear()
            mgrs = self.db.get_company_managers(cid) if cid else []
            users_col.controls = []
            for m in mgrs:
                if isinstance(m, dict) and m.get("id"):
                    cb = ft.Checkbox(
                        label=m.get("full_name")
                        or m.get("email")
                        or f"[{m['id'][:8]}]",
                        value=True,
                    )
                    user_checks[m["id"]] = cb
                    users_col.controls.append(cb)

        def _ensure_users_loaded():
            if users_loaded[0]:
                return
            _rebuild_users_for_company(_get_work_company_id())
            users_loaded[0] = True

        _expand_btn = ft.IconButton(
            icon=ft.Icons.EXPAND_MORE, tooltip="Розгорнути список користувачів"
        )

        def _toggle_users(_):
            users_expanded[0] = not users_expanded[0]
            if users_expanded[0]:
                _ensure_users_loaded()
            users_col.visible = users_expanded[0]
            _expand_btn.icon = (
                ft.Icons.EXPAND_LESS if users_expanded[0] else ft.Icons.EXPAND_MORE
            )
            with contextlib.suppress(Exception):
                users_col.update()
            with contextlib.suppress(Exception):
                _expand_btn.update()

        _expand_btn.on_click = _toggle_users

        user_selection_section = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text("Призначити користувачам:", weight="bold", size=13),
                            _expand_btn,
                        ],
                        spacing=5,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    users_col,
                ]
            ),
            border=ft.border.all(1, ft.Colors.BLUE_GREY_200),
            border_radius=10,
            padding=ft.padding.symmetric(horizontal=10, vertical=8),
            visible=not is_edit and self.is_privileged,
        )

        def _on_company_change(_):
            rows = _regions_for(_get_work_company_id())
            region_f.options = [
                ft.dropdown.Option(key=r.get("id"), text=r.get("name")) for r in rows
            ]
            if not any(
                getattr(opt, "key", None) == region_f.value for opt in region_f.options
            ):
                region_f.value = region_f.options[0].key if region_f.options else None
            # Rebuild dynamic payment fields for the selected company.
            _rebuild_payment_section()
            _rebuild_delivery_methods_section()
            if not is_edit and self.is_privileged:
                users_loaded[0] = False
                users_col.controls = []
                if users_expanded[0]:
                    _ensure_users_loaded()
            with contextlib.suppress(Exception):
                region_f.update()
            with contextlib.suppress(Exception):
                payment_section.update()
            with contextlib.suppress(Exception):
                delivery_methods_section.update()
            with contextlib.suppress(Exception):
                users_col.update()

        if self.is_superadmin:
            company_f.on_select = _on_company_change

        def _on_region_change(_):
            _rebuild_payment_section()
            _rebuild_delivery_methods_section()
            with contextlib.suppress(Exception):
                payment_section.update()
            with contextlib.suppress(Exception):
                delivery_methods_section.update()

        region_f.on_select = _on_region_change

        # initial payment section build
        _rebuild_payment_section()
        _rebuild_delivery_methods_section()

        # ВИПРАВЛЕНО: функція вибору файлів
        async def start_picking(target):
            try:
                self.current_upload_target = target
                page = self.content_area.page
                if not page:
                    return

                logger.info("Starting local file dialog for %s", target)
                local_path = self._pick_local_image_file()
                logger.info("Selected file for %s: %s", target, local_path)
                if local_path:
                    if not os.path.exists(local_path):
                        logger.error("Selected file does not exist: %s", local_path)
                        self._show_snack("Вибраний файл не знайдено", "red")
                    else:
                        if self.current_upload_target == "alipay":
                            self.temp_alipay_path = local_path
                            alipay_qr_status.value = (
                                f"Вибрано файл: {os.path.basename(local_path)}"
                            )
                            self._show_snack(
                                f"QR Alipay вибрано: {os.path.basename(local_path)}",
                                "blue",
                            )
                            with contextlib.suppress(Exception):
                                alipay_qr_status.update()
                        elif self.current_upload_target == "wechat":
                            self.temp_wechat_path = local_path
                            wechat_qr_status.value = (
                                f"Вибрано файл: {os.path.basename(local_path)}"
                            )
                            self._show_snack(
                                f"QR WeChat вибрано: {os.path.basename(local_path)}",
                                "blue",
                            )
                            with contextlib.suppress(Exception):
                                wechat_qr_status.update()
                else:
                    logger.info("File dialog was cancelled for %s", target)
            except Exception as ex:
                logger.exception("File picking failed")
                self._show_snack(f"Помилка вибору файлу: {ex}", "red")

        async def save_click(e):
            try:
                self._clear_field_errors(company_f, name_f, phone_f)
                work_company_id = _get_work_company_id()
                if not work_company_id:
                    self._mark_required(company_f, "Оберіть компанію")
                    self._show_snack("Оберіть компанію", "orange")
                    with contextlib.suppress(Exception):
                        company_f.update()
                    return
                if (
                    not (name_f.value or "").strip()
                    or not (phone_f.value or "").strip()
                ):
                    if not (name_f.value or "").strip():
                        self._mark_required(name_f, "Введіть ім'я")
                    if not (phone_f.value or "").strip():
                        self._mark_required(phone_f, "Введіть телефон")
                    self._show_snack("Заповніть Ім'я та Телефон", "orange")
                    with contextlib.suppress(Exception):
                        name_f.update()
                    with contextlib.suppress(Exception):
                        phone_f.update()
                    return

                current_id = supplier.get("id") if is_edit else None
                _sup_name_val = name_f.value
                _sup_region_val = region_f.value
                if await asyncio.to_thread(
                    lambda: self.db.supplier_name_exists(
                        work_company_id,
                        _sup_name_val,
                        region_id=_sup_region_val,
                        manager_id=self.manager_id,
                        exclude_supplier_id=current_id,
                    )
                ):
                    err = "Постачальник з таким іменем у вас вже існує в цьому регіоні"
                    error_text.value = f"❌ {err}"
                    error_text.visible = True
                    self._mark_required(name_f, err)
                    with contextlib.suppress(Exception):
                        error_text.update()
                    with contextlib.suppress(Exception):
                        name_f.update()
                    return

                save_btn.disabled = True
                with contextlib.suppress(Exception):
                    save_btn.update()

                final_alipay_qr = supplier.get("alipay_qr", "")
                final_wechat_qr = supplier.get("wechat_qr", "")

                if self.temp_alipay_path:
                    logger.info(
                        "Uploading Alipay image from local path: %s",
                        self.temp_alipay_path,
                    )
                    logger.info(
                        "Alipay file exists: %s", os.path.exists(self.temp_alipay_path)
                    )
                    compressed = self._compress_image(self.temp_alipay_path)
                    url = self.db.upload_file(
                        "qrcodes", compressed, f"qrcode_alipay_{uuid.uuid4()}.jpg"
                    )
                    if url:
                        final_alipay_qr = url
                    if os.path.exists(compressed) and "temp_" in compressed:
                        os.remove(compressed)

                if self.temp_wechat_path:
                    logger.info(
                        "Uploading WeChat image from local path: %s",
                        self.temp_wechat_path,
                    )
                    logger.info(
                        "WeChat file exists: %s", os.path.exists(self.temp_wechat_path)
                    )
                    compressed = self._compress_image(self.temp_wechat_path)
                    url = self.db.upload_file(
                        "qrcodes", compressed, f"qrcode_wechat_{uuid.uuid4()}.jpg"
                    )
                    if url:
                        final_wechat_qr = url
                    if os.path.exists(compressed) and "temp_" in compressed:
                        os.remove(compressed)

                # Upload dynamic payment images (if any)
                final_payment_images = dict(current_payment_images)
                for fid, local_path in (temp_payment_images or {}).items():
                    try:
                        if not local_path:
                            continue
                        compressed = self._compress_image(local_path)
                        url = self.db.upload_file(
                            "qrcodes",
                            compressed,
                            f"payment_field_{fid}_{uuid.uuid4()}.jpg",
                        )
                        if url:
                            final_payment_images[fid] = url
                        if os.path.exists(compressed) and "temp_" in compressed:
                            os.remove(compressed)
                    except Exception:
                        logger.exception("Upload payment image failed")

                # An empty list means "unrestricted" (every region delivery
                # method applies) — saved whenever nothing was explicitly
                # narrowed down (all checked, or none checked at all/no
                # methods to choose from), so this never blocks the cost
                # table by accident.
                _checked_delivery_ids = [
                    mid for mid, cb in delivery_methods_checks.items() if cb.value
                ]
                _enabled_delivery_method_ids = (
                    _checked_delivery_ids
                    if 0 < len(_checked_delivery_ids) < len(delivery_methods_checks)
                    else []
                )

                data = {
                    "company_id": work_company_id,
                    "region_id": region_f.value,
                    "name": name_f.value,
                    "phone": phone_f.value,
                    "currency": self._normalize_currency_code(currency_f.value, "USD"),
                    "swift": swift_f.value,
                    "alipay_id": alipay_id_f.value,
                    "wechat_id": wechat_id_f.value,
                    "alipay_qr": final_alipay_qr,
                    "wechat_qr": final_wechat_qr,
                    "notes": notes_f.value,
                    "enabled_delivery_method_ids": _enabled_delivery_method_ids,
                }

                # Build payment_fields payload if company has configured effective fields.
                effective_fields = (
                    self.db.get_effective_payment_fields_for_manager(
                        work_company_id,
                        self.manager_id,
                        region_id=region_f.value or supplier.get("region_id"),
                    )
                    if work_company_id
                    else []
                )
                if effective_fields:
                    pf_payload = []
                    for row in payment_rows:
                        fid = row.get("field_id")
                        enabled = bool(row["enabled_cb"].value)
                        text_val = (
                            row["text_f"].value
                            if (enabled and row.get("text_f") is not None)
                            else ""
                        )
                        img_url = (
                            final_payment_images.get(fid, "")
                            if (enabled and row.get("has_image"))
                            else ""
                        )
                        pf_payload.append(
                            {
                                "field_id": fid,
                                "enabled": enabled,
                                "text_value": text_val,
                                "image_url": img_url,
                            }
                        )
                    data["payment_fields"] = pf_payload

                    # Transitional mapping to legacy keys if template codes match.
                    code_to_row = {}
                    for pr in pf_payload:
                        fid = str(pr.get("field_id") or "")
                        match = next(
                            (
                                ef
                                for ef in effective_fields
                                if str(ef.get("id") or "") == fid
                            ),
                            None,
                        )
                        if match:
                            code_to_row[
                                str(match.get("code") or "").strip().lower()
                            ] = pr
                    if "swift" in code_to_row:
                        data["swift"] = code_to_row["swift"].get("text_value") or ""
                    if "alipay_id" in code_to_row:
                        data["alipay_id"] = (
                            code_to_row["alipay_id"].get("text_value") or ""
                        )
                    if "wechat_id" in code_to_row:
                        data["wechat_id"] = (
                            code_to_row["wechat_id"].get("text_value") or ""
                        )
                    if "alipay_qr" in code_to_row:
                        data["alipay_qr"] = (
                            code_to_row["alipay_qr"].get("image_url") or ""
                        )
                    if "wechat_qr" in code_to_row:
                        data["wechat_qr"] = (
                            code_to_row["wechat_qr"].get("image_url") or ""
                        )

                if is_edit:
                    if self.is_privileged:
                        data["manager_id"] = supplier.get("manager_id", "")
                    else:
                        data["manager_id"] = self.manager_id or supplier.get(
                            "manager_id", ""
                        )
                    _sid = supplier.get("id")
                    _data_snap = dict(data)
                    success = await asyncio.to_thread(
                        lambda: self.db.update_supplier(_sid, _data_snap)
                    )
                    if (not success) and (not self.is_privileged):
                        # Fallback: keep user workflow unblocked by creating personal supplier copy.
                        _fb_data = {**_data_snap, "manager_id": self.manager_id or ""}
                        success = await asyncio.to_thread(
                            lambda: self.db.add_supplier(_fb_data)
                        )
                        if success:
                            self._show_snack(
                                self.t("packaging.base_update_failed_copy_created", "Оновити базовий запис не вдалося — створено вашу персональну копію"),
                                "blue",
                            )
                elif self.is_privileged:
                    selected_managers = [
                        mid for mid, cb in user_checks.items() if cb.value
                    ]
                    if not selected_managers:
                        save_btn.disabled = False
                        with contextlib.suppress(Exception):
                            save_btn.update()
                        self._show_snack("Оберіть хоча б одного користувача", "orange")
                        return
                    _data_snap = dict(data)
                    ok_count = 0
                    for mid in selected_managers:
                        _m_data = {**_data_snap, "manager_id": mid}
                        if await asyncio.to_thread(
                            lambda d=_m_data: self.db.add_supplier(d)
                        ):
                            ok_count += 1
                    success = ok_count > 0
                else:
                    data["manager_id"] = self.manager_id or ""
                    _data_snap = dict(data)
                    success = await asyncio.to_thread(
                        lambda: self.db.add_supplier(_data_snap)
                    )
                if success:
                    self._close_dlg(dlg)
                    self.load_suppliers()
                    self._show_snack(self.t("packaging.saved", "Дані збережено!"), "green")
                else:
                    save_btn.disabled = False
                    self._show_snack(self.t("packaging.err.db", "Помилка БД"), "red")
            except Exception as ex:
                logger.exception("Supplier save failed")
                save_btn.disabled = False
                with contextlib.suppress(Exception):
                    save_btn.update()
                self._show_snack(f"Помилка збереження: {ex}", "red")

        save_btn = ft.ElevatedButton(self.t("common.save", "Зберегти"), on_click=save_click)

        error_text = ft.Text(
            "",
            color="red",
            size=11,
            weight="bold",
            visible=False,
        )

        def _supplier_help(_):
            from views.help_utils import show_modal_help
            _pg = self._page()
            show_modal_help(_pg, self.t("suppliers.edit_help.title", "Постачальник"), [
                self.t("suppliers.edit_help.step1", "1. Введіть назву постачальника."),
                self.t("suppliers.edit_help.step2", "2. Вкажіть телефон і оберіть регіон та валюту."),
                self.t("suppliers.edit_help.step3", "3. Призначте відповідального менеджера (якщо потрібно)."),
                self.t("suppliers.edit_help.step4", "4. У розділі «Оплата» заповніть налаштовані для компанії платіжні поля."),
                self.t("suppliers.edit_help.step5", "5. Додайте QR-код через іконку завантаження."),
                self.t("suppliers.edit_help.step6", "6. Натисніть «Зберегти»."),
            ])

        dlg = ft.AlertDialog(
            title=ft.Row(
                [
                    ft.Text("Постачальник", expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_supplier_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Column(
                [
                    error_text,
                    company_f,
                    name_f,
                    quick_fill_btn,
                    phone_f,
                    ft.Row([region_f, currency_f], spacing=10),
                    user_selection_section,
                    ft.Divider(),
                    payment_section,
                    ft.Divider(),
                    delivery_methods_section,
                    notes_f,
                ],
                tight=True,
                spacing=10,
                width=450,
                scroll=ft.ScrollMode.ADAPTIVE,
            ),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=lambda _: self._close_dlg(dlg)),
                save_btn,
            ],
        )
        return dlg

    def archive_supplier(self, s_id):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        supplier_row = None
        for row in (
            self.db.get_suppliers(self.company_id, None, manager_id=self.manager_id)
            or []
        ):
            if isinstance(row, dict) and row.get("id") == s_id:
                supplier_row = row
                break
        if supplier_row and not self._can_modify_supplier(supplier_row):
            self._show_snack(
                self.t("suppliers.locked_by_other", "Цей постачальник закріплений за іншим користувачем"), "orange"
            )
            return

        def confirm_archive(e):
            if self.db.archive_supplier(s_id):
                self._close_dlg(confirm_dlg)
                self.load_suppliers()
                self._show_snack(self.t("suppliers.archived", "Перенесено в архів"), "blue")

        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text(self.t("suppliers.dlg.archive_confirm", "Перенести постачальника в архів? Його можна буде відновити.")),
            actions=[
                ft.TextButton(self.t("common.no", "Ні"), on_click=lambda _: self._close_dlg(confirm_dlg)),
                ft.TextButton(self.t("common.yes", "Так"), on_click=confirm_archive),
            ],
        )
        self.content_area.page.show_dialog(confirm_dlg)

    def restore_supplier(self, s_id):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return
        if self.db.restore_supplier(s_id):
            self.load_suppliers()
            self._show_snack(self.t("suppliers.restored", "Відновлено з архіву"), "blue")
        else:
            self._show_snack(self.t("suppliers.err.restore", "Не вдалося відновити"), "red")

    def _is_admin_or_superadmin(self):
        role = (self.user_data.get("role") or "").strip().lower()
        return role in {
            "адмін",
            "админ",
            "admin",
            "суперадмін",
            "суперадмин",
            "superadmin",
        }

    def delete_supplier(self, s_id):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return
        if not self._is_admin_or_superadmin():
            self._show_snack(
                "Лише Адмін/СуперАдмін може видаляти постачальників", "orange"
            )
            return

        supplier_row = None
        for row in (
            self.db.get_suppliers(self.company_id, None, manager_id=self.manager_id)
            or []
        ):
            if isinstance(row, dict) and row.get("id") == s_id:
                supplier_row = row
                break
        if supplier_row and not self._can_modify_supplier(supplier_row):
            self._show_snack(
                self.t("suppliers.locked_by_other", "Цей постачальник закріплений за іншим користувачем"), "orange"
            )
            return

        def confirm_delete(e):
            ok, msg = self.db.delete_supplier(self.company_id, s_id)
            self._close_dlg(confirm_dlg)
            if ok:
                self.load_suppliers()
            self._show_snack(msg, "green" if ok else "red")

        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text(
                "Видалити постачальника НАЗАВЖДИ? Це неможливо скасувати. Якщо "
                "постачальник використовується (товари/паковання/замовлення/"
                "взаєморозрахунки) - видалення відхилиться, скористайтесь архівом."
            ),
            actions=[
                ft.TextButton(self.t("common.no", "Ні"), on_click=lambda _: self._close_dlg(confirm_dlg)),
                ft.TextButton(
                    self.t("common.yes", "Так"), on_click=confirm_delete, style=ft.ButtonStyle(color="red")
                ),
            ],
        )
        self.content_area.page.show_dialog(confirm_dlg)

    def delete_qr_image(self, supplier_id, qr_field):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        supplier_row = next(
            (
                s
                for s in (
                    self.db.get_suppliers(
                        self.company_id, None, manager_id=self.manager_id
                    )
                    or []
                )
                if isinstance(s, dict) and s.get("id") == supplier_id
            ),
            None,
        )
        if supplier_row and not self._can_modify_supplier(supplier_row):
            self._show_snack(
                self.t("suppliers.locked_by_other", "Цей постачальник закріплений за іншим користувачем"), "orange"
            )
            return

        def confirm_delete_qr(e):
            try:
                # Спочатку отримуємо поточні дані постачальника
                suppliers = self.db.get_suppliers(
                    self.company_id, None, manager_id=self.manager_id
                )
                supplier = next(
                    (s for s in suppliers if s.get("id") == supplier_id), None
                )

                if not supplier:
                    self._show_snack("Постачальника не знайдено", "red")
                    return

                qr_url = supplier.get(qr_field)
                if qr_url:
                    try:
                        self.db.delete_file_by_url(qr_url)
                    except Exception as ex:
                        logger.warning("Не вдалося видалити файл з storage: %s", ex)

                # Очищаємо поле QR в базі даних
                update_data = {qr_field: ""}
                if self.db.update_supplier(supplier_id, update_data):
                    self._close_dlg(confirm_dlg)
                    self.load_suppliers()
                    qr_type = "Alipay" if qr_field == "alipay_qr" else "WeChat"
                    self._show_snack(f"{qr_type} QR видалено", "blue")
                else:
                    self._show_snack("Помилка видалення QR", "red")
            except Exception as ex:
                logger.exception("Delete QR image failed")
                self._show_snack(f"Помилка видалення: {ex}", "red")

        qr_type = "Alipay" if qr_field == "alipay_qr" else "WeChat"
        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text(f"Видалити {qr_type} QR зображення?"),
            actions=[
                ft.TextButton(self.t("common.no", "Ні"), on_click=lambda _: self._close_dlg(confirm_dlg)),
                ft.TextButton(self.t("common.yes", "Так"), on_click=confirm_delete_qr),
            ],
        )
        self.content_area.page.show_dialog(confirm_dlg)

    def _show_help(self) -> None:
        from views.help_utils import show_modal_help
        page = self._page()
        show_modal_help(page, self.t("suppliers.help.title", "Постачальники"), [
            self.t("suppliers.help.desc", "Каталог постачальників: контакти, реквізити оплати, регіони, валюта."),
            (self.t("suppliers.help.steps_title", "Як користуватись"), None),
            self.t("suppliers.help.step1", "1. Натисніть «Додати постачальника» для створення нового запису."),
            self.t("suppliers.help.step2", "2. Заповніть назву, телефон, регіон і валюту."),
            self.t("suppliers.help.step3", "3. У розділі «Оплата» заповніть налаштовані для компанії платіжні поля та додайте QR-коди."),
            self.t("suppliers.help.step4", "4. «Переглянути» — деталі без редагування; «Редагувати» — повна форма."),
            self.t("suppliers.help.step5", "5. Постачальники, закріплені за іншим менеджером, доступні лише для перегляду."),
            self.t("suppliers.help.step6", "6. Іконка кошика видаляє постачальника (підтвердження обов'язкове)."),
        ])

    def _show_snack(self, text, color):
        if self.content_area.page:
            self.content_area.page.snack_bar = ft.SnackBar(
                ft.Text(text, color=ft.Colors.WHITE, weight="bold"),
                bgcolor=color,
                duration=3000,
            )
            self.content_area.page.snack_bar.open = True
            self.content_area.page.update()
