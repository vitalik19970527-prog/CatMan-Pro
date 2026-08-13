import asyncio
import contextlib
import os
import uuid

import flet as ft

from views.async_view_build_helper import safe_update_with_retry

# ---------------------------------------------------------------------------
# Theme colour shims
# ---------------------------------------------------------------------------
_ACCENT  = '#1D4ED8'
_MUTED   = '#475569'
_BORDER  = '#D6DCE6'
_PANEL   = '#FFFFFF'
_CHIP_T  = '#1E3A8A'
_SOFT_I  = '#E0E7FF'


def _th_from_view(view) -> dict:
    from views.themes import get_active_theme
    return get_active_theme(str(getattr(view, 'manager_id', '') or ''))


import i18n
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


class PackagingView:
    HEADER_SIZE = 24
    META_TEXT_SIZE = 12
    EMPTY_TEXT_SIZE = 14
    ALL_REGIONS_KEY = "__all_regions__"
    # Persists the archive-section collapse toggle across PackagingView
    # re-instantiation (dashboard.py caches one instance per session, but a
    # region/manager switch or Ctrl+F5 still creates a fresh one, and a plain
    # `self` attribute would reset to collapsed every time). Keyed by company_id.
    _archive_collapsed_by_company: dict = {}

    def __init__(self, content_area, user_data, db, current_region_name):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.company_id = self.user_data.get("company_id")
        self.current_region_name = current_region_name
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
        self.t = i18n.bind(self.manager_id or "")
        _nr = (self.user_data.get("role") or "").strip().lower().replace(" ", "")
        self.is_admin = _nr in {"адмін", "админ", "admin"}
        self.is_privileged = self.is_superadmin or self.is_admin
        self._manager_name_cache = {}

        raw_regions = self.db.get_company_regions(self.company_id)
        self.regions_data = raw_regions if isinstance(raw_regions, list) else []
        self._region_name_by_id = {
            r.get("id"): r.get("name")
            for r in self.regions_data
            if isinstance(r, dict) and r.get("id")
        }

        self.temp_image_path = None
        self._debounce_tasks = {}
        self._search_query = ""
        self._header_loading_indicator = None

    def _page(self):
        try:
            return self.content_area.page if self.content_area else None
        except RuntimeError:
            return None

    def _can_modify_packaging(self, packaging):
        packaging = packaging if isinstance(packaging, dict) else {}
        owner_id = (packaging.get("manager_id") or "").strip()
        current_id = (self.manager_id or "").strip()
        if self.is_privileged:
            return True
        packaging_id = (packaging.get("id") or "").strip()
        if packaging_id and self.db.has_entity_owner(
            self.company_id, current_id, "packaging", packaging_id
        ):
            return True
        if not owner_id:
            return True
        return owner_id == current_id

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
            logger.exception("Resolve packaging manager name failed")
            return ""

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

    def _debounce_call(self, key, callback, delay=0.3):
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
            title="Оберіть зображення пакування",
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

    def delete_packaging_image(self, p_id):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        packaging_row = next(
            (
                p
                for p in (
                    self.db.get_packaging(
                        self.company_id, None, manager_id=self.manager_id
                    )
                    or []
                )
                if isinstance(p, dict) and p.get("id") == p_id
            ),
            None,
        )
        if packaging_row and not self._can_modify_packaging(packaging_row):
            self._show_snack(self.t("packaging.locked_record", "Це пакування закріплене за іншим користувачем"), "orange")
            return

        def confirm_delete_img(e):
            try:
                # 1. Отримуємо дані пакування, щоб знайти URL картинки
                packaging_data = next(
                    (
                        p
                        for p in self.db.get_packaging(
                            self.company_id, None, manager_id=self.manager_id
                        )
                        if p.get("id") == p_id
                    ),
                    None,
                )
                if packaging_data and packaging_data.get("image"):
                    image_url = packaging_data["image"]
                    # 2. Видаляємо файл зі storage за URL
                    self.db.delete_file_by_url(image_url)

                    # 3. Оновлюємо БД (image = None)
                    if self.db.update_packaging(p_id, {"image": None}):
                        self._close_dlg(confirm_dlg)
                        self.load_packaging()
                        self._show_snack(self.t("packaging.image_deleted", "Зображення видалено"), "blue")
                    else:
                        self._show_snack(self.t("packaging.err.db_update", "Помилка оновлення БД"), "red")
            except Exception as ex:
                logger.exception("Delete packaging image failed")
                self._show_snack(f"Помилка: {ex}", "red")

        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text(self.t("packaging.dlg.delete_image", "Видалити зображення пакування?")),
            actions=[
                ft.TextButton(self.t("common.no", "Ні"), on_click=lambda _: self._close_dlg(confirm_dlg)),
                ft.TextButton(self.t("common.yes", "Так"), on_click=confirm_delete_img),
            ],
        )
        self.content_area.page.show_dialog(confirm_dlg)

    def build(self):
        _th = _th_from_view(self)
        global _ACCENT, _MUTED, _BORDER, _PANEL, _CHIP_T, _SOFT_I
        _ACCENT = _th["icon_color"]; _MUTED = _th["muted"]
        _BORDER = _th["border"]; _PANEL = _th["panel"]
        _CHIP_T = _th.get("chip_text", "#1E3A8A"); _SOFT_I = _th.get("chip_bg", "#E0E7FF")
        if not self.content_area.page:
            return
        if self._general_mode and self._general_manager_ids:
            from views.general_wrapper import build_general_sections

            _rid = self._effective_region_id()
            _cid, _db = self.company_id, self.db

            def _count_packs(mid, _db=_db, _cid=_cid, _rid=_rid):
                items = _db.get_packaging(_cid, _rid, manager_id=mid) or []
                return len(items)

            build_general_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={"current_region_name": self.current_region_name},
                count_getter=_count_packs,
                count_label="Пакувань:",
            )
            return

        if self.current_region_name in {None, "", self.ALL_REGIONS_KEY, "Всі регіони"}:
            from views.general_wrapper import build_region_sections

            _cid2, _db2, _mid2 = self.company_id, self.db, self.manager_id
            _all2 = _db2.get_packaging(_cid2, None, manager_id=_mid2) or []
            _rcounts: dict[str, int] = {}
            for _pk2 in _all2:
                if not isinstance(_pk2, dict):
                    continue
                _r2 = str(_pk2.get("region_id") or "").strip()
                if _r2:
                    _rcounts[_r2] = _rcounts.get(_r2, 0) + 1

            def _count_rpk(rid, _c=_rcounts):
                return _c.get(rid, 0)

            build_region_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={},
                count_getter=_count_rpk,
                region_kwarg_name="current_region_name",
                count_label="Пакувань:",
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
        # or editing packaging is tied to a category/level, so a manager
        # with zero category ownership in the current region should only
        # browse, not add/edit.
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
                        ft.Text(self.t("packaging.title", "Пакування"), size=self.HEADER_SIZE, weight="bold"),
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
                            self.t("packaging.add_button", "Додати пакування"),
                            icon=ft.Icons.ADD,
                            height=30,
                            style=ft.ButtonStyle(
                                text_style=ft.TextStyle(size=11),
                                padding=ft.padding.symmetric(horizontal=8, vertical=2),
                            ),
                            on_click=lambda _: self._page()
                            and self._page().run_task(self.show_packaging_dialog),
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
            hint_text=self.t("packaging.search_hint", "Швидкий пошук пакування"),
            prefix_icon=ft.Icons.SEARCH,
            border_radius=10,
            height=36,
            text_size=12,
            content_padding=ft.padding.symmetric(horizontal=10, vertical=6),
            on_change=lambda _: self._debounce_call(
                "packaging_screen_search", self.load_packaging
            ),
        )

        # Vertical scroll must be owned by dashboard `content_area`.
        self.packaging_list = ft.Column(
            spacing=4,
        )
        self.loading_box = ft.Container(
            padding=14,
            visible=True,
            content=ft.Column(
                [
                    ft.Text(self.t("packaging.loading", "Завантаження пакування..."), color="grey700"),
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
                self.packaging_list,
            ]
        )
        self.content_area.update()
        page.run_task(self._async_initial_load)

    async def _async_initial_load(self):
        if self._general_mode and self._general_manager_ids:
            return
        if not hasattr(self, "packaging_list"):
            return
        if self._header_loading_indicator is not None:
            self._header_loading_indicator.visible = True
        if self.content_area.page and hasattr(self.content_area, "update"):
            self.content_area.update()
        await asyncio.sleep(0.01)
        self.load_packaging()
        if getattr(self, "loading_box", None) is not None:
            self.loading_box.visible = False
        if self._header_loading_indicator is not None:
            self._header_loading_indicator.visible = False
        if self.content_area.page and hasattr(self.content_area, "update"):
            self.content_area.update()
        self._packaging_ui_ready = True
        with contextlib.suppress(Exception):
            self._packaging_pulse_ts = self._packaging_scope_pulse_ts()

    def _packaging_scope_pulse_ts(self) -> int:
        try:
            out = self.db.get_companies_sync_pulses(
                [self.company_id], ["packaging", "suppliers", "catalog"]
            ) or {}
            by_company = out.get(self.company_id) if isinstance(out, dict) else {}
            if not isinstance(by_company, dict):
                return 0
            return max((int(v or 0) for v in by_company.values()), default=0)
        except Exception:
            return 0

    async def resume(self) -> None:
        """Called when returning to this cached tab. This view previously had
        NO reload mechanism at all once built once — a packaging change made
        on another device/session (or by a керівник's own subordinate) never
        showed up here short of an app restart. Cheap pulse check + a real
        (force_refresh) reload only when something genuinely changed."""
        if not getattr(self, "_packaging_ui_ready", False):
            return

        async def _bg_packaging_pulse_check():
            try:
                new_pulse = await asyncio.to_thread(self._packaging_scope_pulse_ts)
                cached_pulse = int(getattr(self, "_packaging_pulse_ts", 0) or 0)
                if new_pulse > cached_pulse:
                    self._packaging_pulse_ts = new_pulse
                    self.load_packaging(force_refresh=True)
            except Exception:
                pass

        _pg = self._page()
        if _pg and hasattr(_pg, "run_task"):
            with contextlib.suppress(Exception):
                _pg.run_task(_bg_packaging_pulse_check)

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

    def load_packaging(self, force_refresh: bool = False):
        if not hasattr(self, "packaging_list"):
            return
        self.packaging_list.controls.clear()
        current_region_id = self._effective_region_id()

        load_manager_id = self.manager_id
        raw_packaging = self.db.get_packaging(
            self.company_id,
            current_region_id,
            manager_id=load_manager_id,
            force_refresh=force_refresh,
        )
        packaging = raw_packaging if isinstance(raw_packaging, list) else []
        self._set_loading_text(f"Пакування: {len(packaging)}")

        query = (
            (self.search_f.value or "").strip().lower()
            if hasattr(self, "search_f")
            else ""
        )
        if query:
            filtered_rows = []
            for p in packaging:
                if not isinstance(p, dict):
                    continue
                supplier_name = (
                    (p.get("suppliers") or {}).get("name")
                    if isinstance(p.get("suppliers"), dict)
                    else ""
                ) or ""
                hay = " ".join(
                    [
                        str(p.get("name") or ""),
                        str(p.get("size") or ""),
                        str(p.get("price") or ""),
                        str(p.get("notes") or ""),
                        str(supplier_name),
                    ]
                ).lower()
                if query in hay:
                    filtered_rows.append(p)
            packaging = filtered_rows

        if not packaging:
            self.packaging_list.controls.append(
                ft.Container(
                    content=ft.Text(
                        self.t("packaging.empty_state", "Немає пакування у вибраному контексті"),
                        color="grey600",
                        size=self.EMPTY_TEXT_SIZE,
                        text_align=ft.TextAlign.LEFT,
                    ),
                    padding=ft.padding.only(left=4, top=6, bottom=6),
                )
            )
        else:
            for p in packaging:
                if isinstance(p, dict):
                    self.packaging_list.controls.append(self._create_packaging_card(p))

        with contextlib.suppress(Exception):
            self.packaging_list.controls.append(
                self._build_archive_section(current_region_id, load_manager_id)
            )

        safe_update_with_retry(self.packaging_list, self._page())

    def _build_archive_section(self, region_id, manager_id):
        """Згорнута секція архівованого пакування з можливістю відновлення."""
        archived_rows = (
            self.db.get_packaging(
                self.company_id,
                region_id,
                manager_id=manager_id,
                archived=True,
            )
            or []
        )
        state_key = str(self.company_id or "")
        collapsed = PackagingView._archive_collapsed_by_company.get(state_key, True)

        def _toggle(_):
            PackagingView._archive_collapsed_by_company[state_key] = not collapsed
            self.load_packaging()

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
                        self.t("packaging.archive_section", "Архів") + f" ({len(archived_rows)})",
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
                            self.t("packaging.archive_empty", "Архів порожній"),
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
                        controls.append(self._create_packaging_card(row, is_archived=True))
        return ft.Container(
            content=ft.Column(controls, spacing=2),
            margin=ft.margin.only(top=10),
            border=ft.border.only(top=ft.BorderSide(1, "grey300")),
        )

    def _create_packaging_card(self, p, is_archived: bool = False):
        supplier_name = self.t("packaging.unknown_supplier", "Невідомий постачальник")
        supplier_currency = "USD"
        if p.get("suppliers"):
            supplier_name = p["suppliers"].get("name", supplier_name)
            supplier_currency = p["suppliers"].get("currency", supplier_currency)

        region_name = self._region_name_by_id.get(p.get("region_id"), self.t("packaging.unknown_region", "Незн. регіон"))
        manager_name = self._resolve_manager_name(
            p.get("company_id"), p.get("manager_id")
        )
        can_modify = self._can_modify_packaging(p)

        def open_image_view(_=None):
            _urls = [
                self.db.resolve_image_file_path(str(p.get(k) or "").strip())
                for k in ("image", "image2", "image3")
                if str(p.get(k) or "").strip()
            ]
            if not _urls:
                self._show_snack(self.t("packaging.no_photo", "У цього пакування немає фото"), "orange")
                return
            page = self._page()
            if not page:
                return
            _dw = (getattr(page, "window_width", None) or 1200) * 0.72
            _dh = (getattr(page, "window_height", None) or 900) * 0.86
            _img_w = _dw - 120
            _img_h = _dh - 50
            _idx = [0]
            _gal_img = ft.Image(src=_urls[0], width=_img_w, height=_img_h, fit=ft.BoxFit.CONTAIN)
            _counter = ft.Text(f"1 / {len(_urls)}", color=ft.Colors.GREY_500, size=11, visible=len(_urls) > 1)

            def _go(delta, _=None):
                _idx[0] = (_idx[0] + delta) % len(_urls)
                _gal_img.src = _urls[_idx[0]]
                _counter.value = f"{_idx[0] + 1} / {len(_urls)}"
                with contextlib.suppress(Exception):
                    _gal_img.update()
                    _counter.update()

            _arrow_style = ft.ButtonStyle(bgcolor=ft.Colors.with_opacity(0.22, ft.Colors.WHITE), shape=ft.CircleBorder())
            _prev = ft.IconButton(icon=ft.Icons.CHEVRON_LEFT, icon_color=ft.Colors.WHITE, icon_size=34, on_click=lambda _: _go(-1), visible=len(_urls) > 1, style=_arrow_style)
            _next_btn = ft.IconButton(icon=ft.Icons.CHEVRON_RIGHT, icon_color=ft.Colors.WHITE, icon_size=34, on_click=lambda _: _go(1), visible=len(_urls) > 1, style=_arrow_style)
            _dlg_ref = [None]

            def _close_dlg(_=None):
                page.on_keyboard_event = _prev_kb[0]
                with contextlib.suppress(Exception):
                    page.pop_dialog()

            _prev_kb = [getattr(page, "on_keyboard_event", None)]

            def _on_kb(e: ft.KeyboardEvent):
                if e.key == "Arrow Left":
                    _go(-1)
                elif e.key == "Arrow Right":
                    _go(1)
                elif e.key == "Escape":
                    _close_dlg()

            page.on_keyboard_event = _on_kb
            _dlg_ref[0] = ft.AlertDialog(
                content=ft.Stack(
                    [
                        ft.Container(
                            width=_dw, height=_dh, bgcolor="#111111",
                            content=ft.Column(
                                [
                                    ft.Row([_prev, ft.Container(content=_gal_img, expand=True, alignment=ft.Alignment(0, 0)), _next_btn], expand=True, vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=4),
                                    ft.Container(content=_counter, alignment=ft.Alignment(0, 0), padding=ft.padding.only(bottom=4)),
                                ],
                                spacing=0, expand=True,
                            ),
                        ),
                        ft.Container(
                            content=ft.IconButton(icon=ft.Icons.CLOSE, icon_color=ft.Colors.WHITE, icon_size=20, on_click=_close_dlg, style=ft.ButtonStyle(bgcolor=ft.Colors.with_opacity(0.55, ft.Colors.BLACK))),
                            right=6, top=6,
                        ),
                    ],
                    width=_dw, height=_dh,
                ),
                bgcolor="#111111",
                content_padding=ft.padding.all(0),
                on_dismiss=lambda _: setattr(page, "on_keyboard_event", _prev_kb[0]),
            )
            page.show_dialog(_dlg_ref[0])

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
                                                p.get("name", "Без назви"),
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
                                        ],
                                        spacing=7,
                                    ),
                                    ft.Row(
                                        [
                                            ft.Text(
                                                self.t("packaging.card.size", "Розмір: {v}").format(v=p.get('size', '-')), size=11
                                            ),
                                            ft.Row(
                                                [
                                                    ft.Text(
                                                        self.t("packaging.card.price", "Ціна: {price} {ccy}").format(price=p.get('price', '0'), ccy=supplier_currency),
                                                        size=11,
                                                        weight="bold",
                                                    ),
                                                    ft.Text(
                                                        self.t("packaging.card.from_supplier", "від {name}").format(name=supplier_name),
                                                        size=10,
                                                        color="grey600",
                                                    ),
                                                ],
                                                spacing=8,
                                            ),
                                        ],
                                        spacing=8,
                                    ),
                                    ft.Text(
                                        self.t(
                                            "packaging.card.min_optimal",
                                            "Мін: {min_qty} / Опт: {optimal_qty} шт",
                                        ).format(
                                            min_qty=p.get("min_qty") or "-",
                                            optimal_qty=p.get("optimal_qty") or "-",
                                        ),
                                        size=10,
                                        color="grey600",
                                        visible=bool(p.get("min_qty") or p.get("optimal_qty")),
                                    ),
                                    ft.Row(
                                        [
                                            ft.TextButton(
                                                self.t("packaging.card.photo_btn", "Фото"),
                                                icon=ft.Icons.IMAGE,
                                                style=ft.ButtonStyle(
                                                    text_style=ft.TextStyle(size=11),
                                                    padding=ft.padding.symmetric(
                                                        horizontal=6, vertical=0
                                                    ),
                                                ),
                                                visible=any(p.get(k) for k in ("image", "image2", "image3")),
                                                on_click=open_image_view,
                                            ),
                                            ft.IconButton(
                                                ft.Icons.DELETE_OUTLINE,
                                                icon_color="red300",
                                                tooltip=self.t("packaging.tooltip.delete_image", "Видалити зображення"),
                                                visible=bool(p.get("image"))
                                                and not self.read_only_subordinate,
                                                on_click=lambda _: self.delete_packaging_image(
                                                    p.get("id")
                                                ),
                                            ),
                                        ],
                                        spacing=4,
                                    ),
                                    ft.Text(
                                        self.t("packaging.card.notes", "Нотатки: {v}").format(v=p.get('notes', '')),
                                        size=10,
                                        italic=True,
                                        visible=bool(p.get("notes")),
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
                                        on_click=lambda _, _p=p: self._page()
                                        and self._page().run_task(
                                            self.show_packaging_details_dialog, _p
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
                                            self.t("packaging.tooltip.locked_record", "Запис закріплений за іншим користувачем")
                                            if not can_modify
                                            else None
                                        ),
                                        on_click=lambda _, _p=p: self._page()
                                        and self._page().run_task(
                                            self.show_packaging_dialog, _p
                                        ),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.UNARCHIVE_OUTLINED
                                        if is_archived
                                        else ft.Icons.ARCHIVE_OUTLINED,
                                        icon_color="blue" if is_archived else "grey700",
                                        tooltip=(
                                            (
                                                self.t("packaging.tooltip.restore", "Відновити з архіву")
                                                if is_archived
                                                else self.t("packaging.tooltip.archive", "Архівувати")
                                            )
                                            if can_modify
                                            else self.t("packaging.tooltip.locked_record", "Запис закріплений за іншим користувачем")
                                        ),
                                        visible=not self.read_only_subordinate
                                        and getattr(self, "_region_create_allowed", True),
                                        disabled=not can_modify,
                                        on_click=(
                                            (lambda _: self.restore_packaging(p.get("id")))
                                            if is_archived
                                            else (lambda _: self.archive_packaging(p.get("id")))
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
            logger.warning("[modal:packaging] close skipped: page is None")
            return
        logger.info(
            "[modal:packaging] close:start dlg=%s@%s open=%s overlay_before=%s",
            type(dlg).__name__,
            id(dlg),
            getattr(dlg, "open", None),
            len(getattr(page, "overlay", []) or []),
        )
        page.pop_dialog()
        logger.info(
            "[modal:packaging] close:finish dlg=%s@%s open=%s overlay_after=%s",
            type(dlg).__name__,
            id(dlg),
            getattr(dlg, "open", None),
            len(getattr(page, "overlay", []) or []),
        )

    async def show_packaging_details_dialog(self, packaging):
        """Builds the (DB-heavy) packaging card dialog off the UI thread,
        then shows it — see ProductsView.show_product_details_dialog for
        the same fix and why it matters on Android."""
        dlg = await asyncio.to_thread(self._build_packaging_details_dialog, packaging)
        self.content_area.page.show_dialog(dlg)

    def _build_packaging_details_dialog(self, packaging) -> ft.AlertDialog:
        packaging = packaging if isinstance(packaging, dict) else {}
        company_id = packaging.get("company_id") or self.company_id
        region_name = self._region_name_by_id.get(
            packaging.get("region_id"), self.t("packaging.unknown_region", "Незн. регіон")
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
            company_id, packaging.get("manager_id")
        )
        supplier_name = (
            (packaging.get("suppliers") or {}).get("name")
            if isinstance(packaging.get("suppliers"), dict)
            else ""
        ) or "-"
        supplier_currency = (
            (packaging.get("suppliers") or {}).get("currency")
            if isinstance(packaging.get("suppliers"), dict)
            else ""
        ) or "USD"

        def _value_row(label, value):
            return ft.Column(
                [
                    ft.Text(label, size=11, color="grey700"),
                    ft.Text(value or "-", size=14, selectable=True),
                ],
                spacing=2,
            )

        # Build gallery for detail view
        _det_photo_urls = [
            self.db.resolve_image_file_path(str(packaging.get(k) or "").strip())
            for k in ("image", "image2", "image3")
            if str(packaging.get(k) or "").strip()
        ]

        def _det_gallery(start_idx: int = 0):
            urls = _det_photo_urls
            if not urls:
                return
            _pg = self.content_area.page
            _idx = [max(0, min(start_idx, len(urls) - 1))]
            _dw = (getattr(_pg, "window_width", None) or getattr(_pg, "width", None) or 1200) * 0.88
            _dh = (getattr(_pg, "window_height", None) or getattr(_pg, "height", None) or 900) * 0.90
            _img_w = _dw - 120
            _img_h = _dh - 60
            _gal_img = ft.Image(src=urls[_idx[0]], width=_img_w, height=_img_h, fit=ft.BoxFit.CONTAIN)
            _gal_counter = ft.Text(f"{_idx[0] + 1} / {len(urls)}", color=ft.Colors.GREY_500, size=11, visible=len(urls) > 1)

            def _go(delta, _=None):
                _idx[0] = (_idx[0] + delta) % len(urls)
                _gal_img.src = urls[_idx[0]]
                _gal_counter.value = f"{_idx[0] + 1} / {len(urls)}"
                with contextlib.suppress(Exception):
                    _gal_img.update()
                    _gal_counter.update()

            _arrow_style = ft.ButtonStyle(bgcolor=ft.Colors.with_opacity(0.22, ft.Colors.WHITE), shape=ft.CircleBorder())
            _prev_btn = ft.IconButton(icon=ft.Icons.CHEVRON_LEFT, icon_color=ft.Colors.WHITE, icon_size=34, on_click=lambda _: _go(-1), visible=len(urls) > 1, style=_arrow_style)
            _next_btn = ft.IconButton(icon=ft.Icons.CHEVRON_RIGHT, icon_color=ft.Colors.WHITE, icon_size=34, on_click=lambda _: _go(1), visible=len(urls) > 1, style=_arrow_style)
            _gal_dlg_ref = [None]
            _prev_kb = [getattr(_pg, "on_keyboard_event", None)]

            def _close_gal(_=None):
                _pg.on_keyboard_event = _prev_kb[0]
                with contextlib.suppress(Exception):
                    _pg.close(_gal_dlg_ref[0])

            def _on_gal_kb(e: ft.KeyboardEvent):
                if e.key == "Arrow Left":
                    _go(-1)
                elif e.key == "Arrow Right":
                    _go(1)
                elif e.key == "Escape":
                    _close_gal()

            _pg.on_keyboard_event = _on_gal_kb
            _gal_dlg_ref[0] = ft.AlertDialog(
                content=ft.Stack(
                    [
                        ft.Container(
                            width=_dw, height=_dh, bgcolor="#111111",
                            content=ft.Column(
                                [
                                    ft.Row([_prev_btn, ft.Container(content=_gal_img, expand=True, alignment=ft.Alignment(0, 0)), _next_btn], expand=True, vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=4),
                                    ft.Container(content=_gal_counter, alignment=ft.Alignment(0, 0), padding=ft.padding.only(bottom=6)),
                                ],
                                spacing=0, expand=True,
                            ),
                        ),
                        ft.Container(
                            content=ft.IconButton(icon=ft.Icons.CLOSE, icon_color=ft.Colors.WHITE, icon_size=20, on_click=_close_gal, style=ft.ButtonStyle(bgcolor=ft.Colors.with_opacity(0.55, ft.Colors.BLACK))),
                            right=6, top=6,
                        ),
                    ],
                    width=_dw, height=_dh,
                ),
                bgcolor="#111111",
                content_padding=ft.padding.all(0),
                on_dismiss=lambda _: setattr(_pg, "on_keyboard_event", _prev_kb[0]),
            )
            _pg.open(_gal_dlg_ref[0])

        def _det_img_box(key, width, height, gallery_idx=None):
            raw = str(packaging.get(key) or "").strip()
            if raw:
                _click = (lambda e, i=gallery_idx: _det_gallery(i)) if gallery_idx is not None else None
                return ft.Container(
                    width=width, height=height, border_radius=8,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
                    bgcolor=ft.Colors.GREY_100,
                    alignment=ft.alignment.Alignment(0, 0),
                    on_click=_click,
                    content=ft.Image(src=self.db.resolve_image_file_path(raw), fit=ft.BoxFit.CONTAIN, width=width, height=height),
                )
            return ft.Container(
                width=width, height=height, border_radius=8,
                border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
                bgcolor=ft.Colors.GREY_100,
                alignment=ft.alignment.Alignment(0, 0),
                content=ft.Column([ft.Icon(ft.Icons.IMAGE_OUTLINED, size=max(18, int(width * 0.18)), color="grey400")], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            )

        _det_gal_idx = {
            k: i
            for i, k in enumerate(k for k in ("image", "image2", "image3") if str(packaging.get(k) or "").strip())
        }
        _large_w, _large_h = 300, 300
        _small_w, _small_h = 145, 145
        _images_panel = ft.Column(
            [
                _det_img_box("image", _large_w, _large_h, _det_gal_idx.get("image")),
                ft.Row([_det_img_box("image2", _small_w, _small_h, _det_gal_idx.get("image2")), _det_img_box("image3", _small_w, _small_h, _det_gal_idx.get("image3"))], spacing=8, tight=True),
            ],
            spacing=8, tight=True,
        ) if _det_photo_urls else ft.Text("Немає зображення", color="grey600")

        def _pkg_view_help(_):
            from views.help_utils import show_modal_help
            _pg = self._page()
            show_modal_help(_pg, self.t("packaging.view_help.title", "Перегляд пакування"), [
                self.t("packaging.view_help.step1", "1. Тут відображаються всі дані пакування."),
                self.t("packaging.view_help.step2", "2. Фото можна переглянути у повному розмірі."),
                self.t("packaging.view_help.step3", "3. Натисніть «Закрити», щоб повернутись до списку."),
            ])

        dlg = ft.AlertDialog(
            title=ft.Row(
                [
                    ft.Text(f"Перегляд пакування: {packaging.get('name') or 'Без назви'}", selectable=True, expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_pkg_view_help),
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
                            ],
                            spacing=8,
                            wrap=True,
                        ),
                        ft.Row(
                            [
                                _value_row(self.t("nav.company_label", "Компанія"), company_name),
                                _value_row(self.t("orders.filter.supplier", "Постачальник"), supplier_name),
                            ],
                            spacing=20,
                            wrap=True,
                        ),
                        ft.Row(
                            [
                                _value_row("Розмір", packaging.get("size")),
                                _value_row(
                                    "Ціна",
                                    f"{packaging.get('price') or '-'} {supplier_currency}",
                                ),
                            ],
                            spacing=20,
                            wrap=True,
                        ),
                        _value_row("Нотатки", packaging.get("notes")),
                        ft.Divider(),
                        ft.Text("Фото (до 3-х)", weight="bold"),
                        _images_panel,
                    ],
                    spacing=12,
                    scroll=ft.ScrollMode.ADAPTIVE,
                ),
            ),
            actions=[ft.TextButton(self.t("packaging.dlg.close", "Закрити"), on_click=lambda _: self._close_dlg(dlg))],
        )
        return dlg

    async def show_packaging_dialog(self, packaging=None):
        """Builds the (DB-heavy) packaging form off the UI thread, then
        shows it — see ProductsView.show_product_dialog for the same fix
        and why it matters on Android. The fast, non-DB permission checks
        run here on the main thread first (cheap, and _show_snack/
        page.update() must not be called from the background builder
        thread)."""
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

        result = await asyncio.to_thread(self._build_packaging_dialog, packaging)
        if isinstance(result, tuple):
            _, error_msg = result
            self._show_snack(error_msg, "orange")
            return
        if result is not None:
            self.content_area.page.show_dialog(result)

    def _build_packaging_dialog(self, packaging=None):
        if (
            isinstance(packaging, dict)
            and packaging.get("id")
            and not self._can_modify_packaging(packaging)
        ):
            return None, self.t(
                "packaging.locked_record", "Це пакування закріплене за іншим користувачем"
            )

        is_edit = isinstance(packaging, dict)
        if not is_edit:
            packaging = {}

        _existing_urls = [
            (packaging.get(k) or "") if isinstance(packaging, dict) else ""
            for k in ("image", "image2", "image3")
        ]
        _temp_paths = [None, None, None]
        _slot_texts = [
            ft.Text(
                "Завантажено" if _existing_urls[i] else "Не завантажено",
                size=11,
                color="grey700",
            )
            for i in range(3)
        ]

        companies = self.db.get_all_companies() if self.is_superadmin else []
        company_options = [
            ft.dropdown.Option(key=c.get("id"), text=c.get("name"))
            for c in companies
            if isinstance(c, dict) and c.get("id")
        ]
        default_company_id = (
            packaging.get("company_id")
            or (self.company_id if not is_edit else "")
            or (company_options[0].key if company_options else None)
        )

        company_f = ft.Dropdown(
            label=self.t("nav.company_label", "Компанія"),
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
            label=self.t("packaging.field.box_name", "Назва коробки"), value=packaging.get("name", ""), border_radius=10
        )
        size_f = ft.TextField(
            label=self.t("packaging.field.box_size", "Розмір коробки"), value=packaging.get("size", ""), border_radius=10
        )
        price_f = ft.TextField(
            label=self.t("packaging.field.box_price", "Ціна коробки"),
            value=packaging.get("price", ""),
            border_radius=10,
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        # Per-packaging stock thresholds (шт) — each packaging can need a very
        # different "enough" amount (a supplier's small accessory box vs a
        # large device box), so this replaces the old company-wide hardcoded
        # 2500/5000 warning levels on the Home dashboard card.
        min_qty_f = ft.TextField(
            label=self.t("packaging.field.min_qty", "Мінімум, шт"),
            value=str(packaging.get("min_qty") or ""),
            border_radius=10,
            keyboard_type=ft.KeyboardType.NUMBER,
            width=235,
        )
        optimal_qty_f = ft.TextField(
            label=self.t("packaging.field.optimal_qty", "Оптимум, шт"),
            value=str(packaging.get("optimal_qty") or ""),
            border_radius=10,
            keyboard_type=ft.KeyboardType.NUMBER,
            width=235,
        )
        notes_f = ft.TextField(
            label=self.t("packaging.field.notes", "Нотатки по доробці"),
            value=packaging.get("notes", ""),
            border_radius=10,
            multiline=True,
        )

        # Отримуємо список регіонів -- лише ті, де є доступ до категорій
        # (регіон і категорія — окремі гранти; без категорії нема куди
        # прив'язати нове пакування). У режимі редагування поточний
        # збережений регіон лишається в списку, навіть якщо доступ до нього
        # відтоді забрали, щоб не зламати перегляд/збереження існуючого.
        region_rows_all = _regions_for(default_company_id)
        _existing_region_id = str((packaging or {}).get("region_id") or "").strip()
        # Batched: was one manager_has_category_access_in_region() call PER
        # region (role lookup + scope queries + catalog_nodes lookup each
        # time) — now a single query computes every allowed region up front.
        _allowed_region_ids = self.db.get_manager_create_allowed_region_ids(
            self.manager_id, self.company_id
        )
        region_rows = [
            r
            for r in region_rows_all
            if isinstance(r, dict)
            and (
                str(r.get("id") or "") == _existing_region_id
                or _allowed_region_ids is None
                or str(r.get("id") or "") in _allowed_region_ids
            )
        ]
        region_options = [
            ft.dropdown.Option(key=r.get("id"), text=r.get("name"))
            for r in region_rows
            if isinstance(r, dict)
        ]

        def _suppliers_for(company_id, region_id):
            # In privileged mode we still filter by selected region; manager scoping is for non-privileged users.
            _supp_manager_id = self.manager_id
            rows = self.db.get_suppliers(
                company_id, region_id, manager_id=_supp_manager_id
            )
            return [s for s in rows if isinstance(s, dict) and s.get("id")]

        default_supplier_id = packaging.get("supplier_id")
        default_region_id = packaging.get("region_id")
        # In edit mode: do NOT auto-pick current region if missing; show as saved (even if empty).
        if (
            (not is_edit)
            and (not default_region_id)
            and self.current_region_name
            and self.current_region_name != "__all_regions__"
        ):
            current_region = next(
                (r for r in region_rows if r.get("name") == self.current_region_name),
                None,
            )
            if current_region:
                default_region_id = current_region.get("id")

        # Валюта підтягується автоматично від постачальника
        currency_display = ft.Text(
            "Валюта: буде підтягнута від постачальника", size=12, color="grey600"
        )
        quick_fill_btn = ft.Container(visible=False)
        suppliers_cache_ref = {"rows": []}

        region_f = ft.Dropdown(
            label=self.t("orders.field.region", "Регіон"),
            options=([ft.dropdown.Option(key="", text="—")] + region_options),
            value=default_region_id,
            border_radius=10,
        )

        def _reload_suppliers_for_context(keep_value=True):
            prev_value = supplier_f.value if keep_value else None
            rows = _suppliers_for(_get_work_company_id(), region_f.value)
            suppliers_cache_ref["rows"] = rows
            supplier_f.options = [ft.dropdown.Option(key="", text="—")] + [
                ft.dropdown.Option(key=s.get("id"), text=s.get("name")) for s in rows
            ]
            valid_ids = {getattr(opt, "key", None) for opt in supplier_f.options}
            if prev_value in valid_ids:
                supplier_f.value = prev_value
            elif default_supplier_id in valid_ids:
                supplier_f.value = default_supplier_id
            elif supplier_f.options:
                # In create mode select first supplier; in edit mode keep empty if missing.
                supplier_f.value = supplier_f.options[0].key if (not is_edit) else ""
            else:
                supplier_f.value = None

        def _on_supplier_change(e):
            selected_supplier = next(
                (
                    s
                    for s in suppliers_cache_ref["rows"]
                    if isinstance(s, dict) and s.get("id") == supplier_f.value
                ),
                None,
            )
            if selected_supplier:
                currency = selected_supplier.get("currency", "USD")
                currency_display.value = f"Валюта: {currency}"
            else:
                currency_display.value = "Валюта: буде підтягнута від постачальника"
            with contextlib.suppress(Exception):
                currency_display.update()

        supplier_f = ft.Dropdown(
            label=self.t("orders.filter.supplier", "Постачальник"),
            options=[],
            value=default_supplier_id,
            border_radius=10,
            on_select=_on_supplier_change,
        )

        def apply_quick_fill(data_match):
            name_f.value = data_match.get("name", name_f.value)
            size_f.value = data_match.get("size", "")
            price_f.value = data_match.get("price", "")
            min_qty_f.value = str(data_match.get("min_qty") or "")
            optimal_qty_f.value = str(data_match.get("optimal_qty") or "")
            notes_f.value = data_match.get("notes", "")
            region_f.value = data_match.get("region_id")
            supplier_f.value = data_match.get("supplier_id")
            quick_fill_btn.visible = False
            _on_supplier_change(None)

        def check_existing_packaging(e):
            error_text.visible = False  # Clear error when name changes
            query = name_f.value.strip()

            def _run_search():
                if not is_edit and len(query) > 1:
                    work_company_id = _get_work_company_id()
                    matches = (
                        self.db.search_packaging_company(work_company_id, query, 5)
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
                            supplier_name = (
                                match.get("suppliers", {}).get(
                                    "name", "без постачальника"
                                )
                                if isinstance(match.get("suppliers"), dict)
                                else "без постачальника"
                            )
                            region_name = _region_name_for(
                                match.get("company_id"), match.get("region_id")
                            )
                            packaging_name = match.get("name", "пакування")
                            controls.append(
                                ft.Row(
                                    [
                                        ft.Text(
                                            f"{packaging_name} / {supplier_name} / {region_name}",
                                            size=12,
                                        ),
                                        ft.ElevatedButton(
                                            self.t("packaging.copy_btn", "Копіювати"),
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

            self._debounce_call("packaging_dialog_search", _run_search)

        name_f.on_change = check_existing_packaging

        _reload_suppliers_for_context(keep_value=True)
        if supplier_f.value:
            _on_supplier_change(None)

        # --- User assignment section (admin/superadmin creating new packaging) ---
        user_checks = {}  # manager_id -> ft.Checkbox
        pkg_users_col = ft.Column([], spacing=4, visible=False)
        pkg_users_expanded = [False]
        pkg_users_loaded = [False]

        def _rebuild_pkg_users_for_company(cid):
            user_checks.clear()
            mgrs = self.db.get_company_managers(cid) if cid else []
            pkg_users_col.controls = []
            for m in mgrs:
                if isinstance(m, dict) and m.get("id"):
                    cb = ft.Checkbox(
                        label=m.get("full_name")
                        or m.get("email")
                        or f"[{m['id'][:8]}]",
                        value=True,
                    )
                    user_checks[m["id"]] = cb
                    pkg_users_col.controls.append(cb)

        def _ensure_pkg_users_loaded():
            if pkg_users_loaded[0]:
                return
            _rebuild_pkg_users_for_company(_get_work_company_id())
            pkg_users_loaded[0] = True

        _pkg_expand_btn = ft.IconButton(
            icon=ft.Icons.EXPAND_MORE, tooltip="Розгорнути список користувачів"
        )

        def _toggle_pkg_users(_):
            pkg_users_expanded[0] = not pkg_users_expanded[0]
            if pkg_users_expanded[0]:
                _ensure_pkg_users_loaded()
            pkg_users_col.visible = pkg_users_expanded[0]
            _pkg_expand_btn.icon = (
                ft.Icons.EXPAND_LESS if pkg_users_expanded[0] else ft.Icons.EXPAND_MORE
            )
            with contextlib.suppress(Exception):
                pkg_users_col.update()
            with contextlib.suppress(Exception):
                _pkg_expand_btn.update()

        _pkg_expand_btn.on_click = _toggle_pkg_users

        pkg_user_selection_section = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text("Призначити користувачам:", weight="bold", size=13),
                            _pkg_expand_btn,
                        ],
                        spacing=5,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    pkg_users_col,
                ]
            ),
            border=ft.border.all(1, ft.Colors.BLUE_GREY_200),
            border_radius=10,
            padding=ft.padding.symmetric(horizontal=10, vertical=8),
            visible=not is_edit and self.is_privileged,
        )

        def _on_company_change(_):
            work_company_id = _get_work_company_id()
            fresh_regions = _regions_for(work_company_id)
            region_f.options = [
                ft.dropdown.Option(key=r.get("id"), text=r.get("name"))
                for r in fresh_regions
            ]
            if not any(
                getattr(opt, "key", None) == region_f.value for opt in region_f.options
            ):
                region_f.value = region_f.options[0].key if region_f.options else None

            _reload_suppliers_for_context(keep_value=False)
            _on_supplier_change(None)
            if not is_edit and self.is_privileged:
                pkg_users_loaded[0] = False
                pkg_users_col.controls = []
                if pkg_users_expanded[0]:
                    _ensure_pkg_users_loaded()

        def _on_region_change(_):
            _reload_suppliers_for_context(keep_value=False)
            _on_supplier_change(None)

        if self.is_superadmin:
            company_f.on_select = _on_company_change
        region_f.on_select = _on_region_change

        def _make_pick_fn(slot_idx: int):
            async def _pick(_=None):
                try:
                    page = self.content_area.page
                    if not page:
                        return
                    local_path = self._pick_local_image_file()
                    if not local_path:
                        return
                    if not os.path.exists(local_path):
                        self._show_snack(self.t("packaging.err.file_not_found", "Вибраний файл не знайдено"), "red")
                        return
                    _temp_paths[slot_idx] = local_path
                    _slot_texts[slot_idx].value = f"Нове: {os.path.basename(local_path)}"
                    self._show_snack(f"Фото {slot_idx + 1} вибрано", "blue")
                    with contextlib.suppress(Exception):
                        _slot_texts[slot_idx].update()
                except Exception as ex:
                    logger.exception("Packaging image picking failed")
                    self._show_snack(f"Помилка вибору файлу: {ex}", "red")
            return _pick

        def _make_clear_fn(slot_idx: int):
            def _clear(_=None):
                _temp_paths[slot_idx] = None
                _existing_urls[slot_idx] = ""
                _slot_texts[slot_idx].value = "Не завантажено"
                with contextlib.suppress(Exception):
                    _slot_texts[slot_idx].update()
            return _clear

        async def save_click(e):
            try:
                self._clear_field_errors(company_f, name_f, supplier_f, price_f)
                error_text.visible = False
                work_company_id = _get_work_company_id()
                if not work_company_id:
                    self._mark_required(company_f, self.t("packaging.err.select_company", "Оберіть компанію"))
                    self._show_snack(self.t("packaging.err.select_company", "Оберіть компанію"), "orange")
                    return
                if not (name_f.value or "").strip():
                    if not (name_f.value or "").strip():
                        self._mark_required(name_f, self.t("packaging.err.enter_name", "Введіть назву"))
                    self._show_snack("Заповніть Назву", "orange")
                    return

                if not supplier_f.value:
                    self._mark_required(supplier_f, self.t("packaging.err.no_supplier", "Не обраний постачальник пакування"))
                    error_text.value = self.t("packaging.err.no_supplier", "Не обраний постачальник пакування")
                    error_text.visible = True
                    self._show_snack(self.t("packaging.err.no_supplier", "Не обраний постачальник пакування"), "orange")
                    with contextlib.suppress(Exception):
                        error_text.update()
                    return

                if not (price_f.value or "").strip():
                    self._mark_required(price_f, self.t("packaging.err.no_price", "Не задана ціна пакування"))
                    error_text.value = self.t("packaging.err.no_price", "Не задана ціна пакування")
                    error_text.visible = True
                    self._show_snack(self.t("packaging.err.no_price", "Не задана ціна пакування"), "orange")
                    with contextlib.suppress(Exception):
                        error_text.update()
                    return

                # Check for duplicate packaging name for current user in this region
                normalized_name = (name_f.value or "").strip().lower()
                _pk_region = region_f.value
                existing_packaging = await asyncio.to_thread(
                    lambda: self.db.get_packaging(
                        work_company_id,
                        region_id=_pk_region,
                        manager_id=self.manager_id,
                    )
                )
                for existing in existing_packaging:
                    if not isinstance(existing, dict):
                        continue
                    existing_name = (existing.get("name") or "").strip().lower()
                    existing_id = existing.get("id")
                    current_id = packaging.get("id") if is_edit else None
                    # Check if name matches and it's not the same packaging being edited
                    if existing_name == normalized_name and existing_id != current_id:
                        error_text.value = "❌ " + self.t("packaging.err.name_exists_ui", "Пакування з таким іменем вже існує")
                        error_text.visible = True
                        self._mark_required(name_f, self.t("packaging.err.name_exists", "Пакування з таким ім'ям вже існує"))
                        with contextlib.suppress(Exception):
                            error_text.update()
                        return

                save_btn.disabled = True
                with contextlib.suppress(Exception):
                    save_btn.update()

                _img_keys = ("image", "image2", "image3")
                for _si, _img_key in enumerate(_img_keys):
                    if _temp_paths[_si]:
                        try:
                            if is_edit and _existing_urls[_si]:
                                self.db.delete_file_by_url(_existing_urls[_si])
                        except Exception:
                            logger.warning("Failed to delete old packaging image before upload")
                        compressed = self._compress_image(_temp_paths[_si])
                        uploaded_url = self.db.upload_file(
                            "packaging", compressed, f"packing_box_{uuid.uuid4()}.jpg"
                        )
                        if uploaded_url:
                            _existing_urls[_si] = uploaded_url
                        if os.path.exists(compressed) and "temp_" in compressed:
                            with contextlib.suppress(Exception):
                                os.remove(compressed)

                data = {
                    "company_id": work_company_id,
                    "region_id": region_f.value,
                    "supplier_id": supplier_f.value,
                    "name": name_f.value,
                    "size": size_f.value,
                    "price": price_f.value,
                    "min_qty": min_qty_f.value,
                    "optimal_qty": optimal_qty_f.value,
                    "image": _existing_urls[0] or None,
                    "image2": _existing_urls[1] or None,
                    "image3": _existing_urls[2] or None,
                    "notes": notes_f.value,
                }

                if is_edit:
                    if self.is_privileged:
                        data["manager_id"] = packaging.get("manager_id", "")
                    else:
                        data["manager_id"] = self.manager_id or packaging.get(
                            "manager_id", ""
                        )
                    _pid = packaging.get("id")
                    _data_snap = dict(data)
                    success = await asyncio.to_thread(
                        lambda: self.db.update_packaging(_pid, _data_snap)
                    )
                    if (not success) and (not self.is_privileged):
                        # Fallback: keep user workflow unblocked by creating personal packaging copy.
                        _fb_data = {**_data_snap, "manager_id": self.manager_id or ""}
                        success = await asyncio.to_thread(
                            lambda: self.db.add_packaging(_fb_data)
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
                        self._show_snack(self.t("packaging.err.select_user", "Оберіть хоча б одного користувача"), "orange")
                        return
                    _data_snap = dict(data)
                    ok_count = 0
                    for mid in selected_managers:
                        _m_data = {**_data_snap, "manager_id": mid}
                        if await asyncio.to_thread(
                            lambda d=_m_data: self.db.add_packaging(d)
                        ):
                            ok_count += 1
                    success = ok_count > 0
                else:
                    data["manager_id"] = self.manager_id or ""
                    _data_snap = dict(data)
                    success = await asyncio.to_thread(
                        lambda: self.db.add_packaging(_data_snap)
                    )
                if success:
                    self._close_dlg(dlg)
                    self.load_packaging()
                    self._show_snack(self.t("packaging.saved", "Дані збережено!"), "green")
                else:
                    save_btn.disabled = False
                    self._show_snack(self.t("packaging.err.db", "Помилка БД"), "red")
            except Exception as ex:
                logger.exception("Packaging save failed")
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

        def _pkg_edit_help(_):
            from views.help_utils import show_modal_help
            _pg = self._page()
            show_modal_help(_pg, self.t("packaging.dlg.edit_title", "Пакування"), [
                self.t("packaging.edit_help.step1", "1. Введіть назву пакування."),
                self.t("packaging.edit_help.step2", "2. Вкажіть розмір і ціну."),
                self.t("packaging.edit_help.step3", "3. Оберіть регіон і постачальника."),
                self.t("packaging.edit_help.step4", "4. Призначте відповідального менеджера (якщо потрібно)."),
                self.t("packaging.edit_help.step5", "5. Завантажте до 3-х фото через іконки завантаження."),
                self.t("packaging.edit_help.step6", "6. Натисніть «Зберегти»."),
            ])

        dlg = ft.AlertDialog(
            title=ft.Row(
                [
                    ft.Text(self.t("packaging.dlg.edit_title", "Пакування"), expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_pkg_edit_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Column(
                [
                    error_text,
                    company_f,
                    name_f,
                    quick_fill_btn,
                    size_f,
                    price_f,
                    ft.Row([min_qty_f, optimal_qty_f], spacing=10),
                    ft.Row([region_f, supplier_f], spacing=10),
                    pkg_user_selection_section,
                    currency_display,
                    ft.Column(
                        [
                            ft.Text("Фото пакування (до 3-х):", size=12, weight="bold"),
                            *[
                                ft.Row(
                                    [
                                        ft.Text(f"Фото {_si + 1}:", size=11, color="grey700", width=55),
                                        ft.IconButton(
                                            ft.Icons.UPLOAD_FILE,
                                            tooltip=f"Завантажити фото {_si + 1}",
                                            icon_size=18,
                                            on_click=lambda _, i=_si: asyncio.create_task(_make_pick_fn(i)()),
                                        ),
                                        ft.IconButton(
                                            ft.Icons.DELETE_OUTLINE,
                                            tooltip=f"Видалити фото {_si + 1}",
                                            icon_size=18,
                                            icon_color="red300",
                                            on_click=lambda _, i=_si: _make_clear_fn(i)(),
                                        ),
                                        _slot_texts[_si],
                                    ],
                                    spacing=4,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                )
                                for _si in range(3)
                            ],
                        ],
                        spacing=4,
                        tight=True,
                    ),
                    notes_f,
                ],
                tight=True,
                spacing=10,
                width=500,
                scroll=ft.ScrollMode.ADAPTIVE,
            ),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=lambda _: self._close_dlg(dlg)),
                save_btn,
            ],
        )
        return dlg

    def archive_packaging(self, p_id):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        packaging_row = None
        for row in (
            self.db.get_packaging(self.company_id, None, manager_id=self.manager_id)
            or []
        ):
            if isinstance(row, dict) and row.get("id") == p_id:
                packaging_row = row
                break
        if packaging_row and not self._can_modify_packaging(packaging_row):
            self._show_snack(self.t("packaging.locked_record", "Це пакування закріплене за іншим користувачем"), "orange")
            return

        def confirm_archive(e):
            if self.db.archive_packaging(p_id):
                self._close_dlg(confirm_dlg)
                self.load_packaging()
                self._show_snack(self.t("packaging.archived", "Перенесено в архів"), "blue")

        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text(self.t("packaging.dlg.archive_confirm", "Перенести пакування в архів? Його можна буде відновити.")),
            actions=[
                ft.TextButton(self.t("common.no", "Ні"), on_click=lambda _: self._close_dlg(confirm_dlg)),
                ft.TextButton(self.t("common.yes", "Так"), on_click=confirm_archive),
            ],
        )
        self.content_area.page.show_dialog(confirm_dlg)

    def restore_packaging(self, p_id):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return
        if self.db.restore_packaging(p_id):
            self.load_packaging()
            self._show_snack(self.t("packaging.restored", "Відновлено з архіву"), "blue")
        else:
            self._show_snack(self.t("packaging.err.restore", "Не вдалося відновити"), "red")

    def _show_help(self) -> None:
        from views.help_utils import show_modal_help
        page = self._page()
        show_modal_help(page, self.t("packaging.help.title", "Пакування"), [
            self.t("packaging.help.desc", "Каталог пакувальних матеріалів: назва, розмір, ціна, постачальник, фото."),
            (self.t("packaging.help.steps_title", "Як користуватись"), None),
            self.t("packaging.help.step1", "1. Натисніть «Додати пакування» для створення нового запису."),
            self.t("packaging.help.step2", "2. Заповніть назву, розмір, ціну, оберіть постачальника і регіон."),
            self.t("packaging.help.step3", "3. Завантажте до 3-х фотографій пакування."),
            self.t("packaging.help.step4", "4. «Переглянути» — детальна картка без редагування."),
            self.t("packaging.help.step5", "5. «Редагувати» — повна форма з усіма полями."),
            self.t("packaging.help.step6", "6. Записи, закріплені за іншим менеджером, доступні лише для перегляду."),
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
