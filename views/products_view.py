import asyncio
import contextlib
import json
import os
import uuid

import flet as ft

import fx_rates

# ---------------------------------------------------------------------------
# Theme colour shims
# ---------------------------------------------------------------------------
_BORDER  = '#E5E7EB'
_ALT     = '#F8FAFC'
_ACCENT  = '#1D4ED8'
_MUTED   = '#475569'
_ALT2    = '#F9FAFB'
_PANEL   = '#FFFFFF'


def _th_from_view(view) -> dict:
    from views.themes import get_active_theme
    return get_active_theme(str(getattr(view, 'manager_id', '') or ''))


from config import Config
import i18n
from logger import logger
from views.async_view_build_helper import safe_update_with_retry
from views.mobile_layout_helpers import is_mobile, make_scrollable_chip_row
from views.number_format import format_grouped, safe_float
from views import product_naming

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


class ProductsView:
    HEADER_SIZE = 28
    META_TEXT_SIZE = 12
    EMPTY_TEXT_SIZE = 14
    NEW_ITEM_KEY = "__create_new__"
    ALL_REGIONS_KEY = "__all_regions__"
    MAX_LEVELS = 7  # 6 category levels + 1 optional extra depth
    CURRENCIES = ["USD", "CNY", "EUR", "UAH", "PLN"]
    HIERARCHY_LABELS = {
        "subcategory": "Підкатегорія",
        "series": "Серія",
        "brand": "Бренд",
    }

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
            "ZLOTY": "PLN",
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

    FX_TO_USD = {
        "USD": 1.0,
        "EUR": 1.08,
        "PLN": 0.25,
        "CNY": 0.14,
        "UAH": 0.025,
    }

    def __init__(
        self,
        content_area,
        user_data,
        db,
        current_region_name,
        initial_state=None,
        on_state_change=None,
        on_bulk_template=None,
        on_products_reload=None,
        quick_filter: str = "",
    ):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.company_id = self.user_data.get("company_id")
        self.manager_id = self.user_data.get("active_manager_id") or self.user_data.get(
            "id"
        )
        self.t = i18n.bind(str(self.user_data.get("id") or ""))
        self.current_region_name = current_region_name
        self.quick_filter = str(quick_filter or "").strip().lower()
        self.read_only_subordinate = bool(
            self.user_data.get("subordinate_view_readonly")
        )
        self._general_mode: bool = bool(self.user_data.get("general_mode", False))
        self._general_manager_ids: list[str] = list(self.user_data.get("general_manager_ids") or [])
        _spm = self.user_data.get("shared_products_map")
        self._shared_products_map: dict = _spm if isinstance(_spm, dict) else {}
        self._bg_products_sync_seq: int = 0
        self._products_ui_ready: bool = False
        self._products_snapshot_pulse_ts: int = 0
        self._ref_data_pulse_ts: int = 0
        from views.general_mode_mixin import build_general_manager_names
        self._general_manager_names: dict[str, str] = build_general_manager_names(
            self.user_data.get("all_subordinates_flat") or [], self.user_data
        )
        self.is_superadmin = (self.user_data.get("role") or "") == "СуперАдмін"
        try:
            self.packaging_enabled = bool(
                self.db.get_effective_features(self.manager_id, self.company_id).get(
                    "packaging", True
                )
            )
        except Exception:
            self.packaging_enabled = True

        raw_regions = self.db.get_company_regions(self.company_id)
        self.regions_data = raw_regions if isinstance(raw_regions, list) else []
        self._debounce_tasks = {}
        self._region_price_tiers_cache = {}
        self._region_delivery_methods_cache = {}
        self._manager_names_by_company_cache = {}
        self._header_loading_indicator = None
        self._fixed_top = None
        state = initial_state if isinstance(initial_state, dict) else {}
        self._selected_category_filter_ids = self._state_ids(
            state.get("category_ids"), state.get("category_id")
        )
        self._selected_subcategory_filter_ids = self._state_ids(
            state.get("subcategory_ids"), state.get("subcategory_id")
        )
        self._selected_brand_filter_ids = self._state_ids(
            state.get("brand_ids"), state.get("brand_id")
        )
        self._selected_series_filter_ids = self._state_ids(
            state.get("series_ids"), state.get("series_id")
        )
        self._advanced_filters_expanded = bool(state.get("filters_expanded", False))
        self._search_query = str(state.get("query") or "")
        self._product_sort_order = (
            "desc"
            if str(state.get("sort_order") or "").strip().lower() == "desc"
            else "asc"
        )
        self._category_filter_host = None
        self._subcategory_filter_host = None
        self._brand_filter_host = None
        self._series_filter_host = None
        self._subcategory_filter_row = None
        self._brand_filter_row = None
        self._series_filter_row = None
        self._sort_order_host = None
        self._advanced_filters_toggle_btn = None
        self._on_state_change = on_state_change if callable(on_state_change) else None
        self._on_bulk_template = (
            on_bulk_template if callable(on_bulk_template) else None
        )
        self._on_products_reload = (
            on_products_reload if callable(on_products_reload) else None
        )
        self._product_path_cache = {}
        self._catalog_config_by_region = {}
        self.temp_image_path = None

        # In-memory product list cache — populated once per session (or on a
        # forced/pulse-triggered reload), reused across filter/search/sort
        # changes so those don't re-hit the DB (500-1000 rows would otherwise
        # mean 4 fresh queries per keystroke).
        self._products_cache: dict = {}
        self._products_cache_loaded: bool = False
        self._archived_products_cache: list = []
        self._suppliers_by_id: dict = {}
        self._packaging_by_id: dict = {}
        self._categories_by_id: dict = {}

    def _trigger_products_reload(self):
        """Ask the dashboard to fully reload the products tab (cache flush + view rebuild).

        Falls back to a local load_products() when no dashboard callback is wired up
        (e.g. quick-filter scratch views or standalone usage).
        """
        if callable(self._on_products_reload):
            self._on_products_reload()
        else:
            self._catalog_config_by_region.clear()
            self._selected_category_filter_ids.clear()
            self._selected_subcategory_filter_ids.clear()
            self._selected_brand_filter_ids.clear()
            self._selected_series_filter_ids.clear()
            self.load_products(force_reload=True)

    def _refresh_supplier_lookup_cache(self) -> None:
        with contextlib.suppress(Exception):
            region_id = self._effective_region_id()
            rows = (
                self.db.get_suppliers(
                    self.company_id, region_id, manager_id=self.manager_id
                )
                or []
            )
            self._suppliers_by_id = {
                s.get("id"): s for s in rows if isinstance(s, dict) and s.get("id")
            }

    def _refresh_packaging_lookup_cache(self) -> None:
        with contextlib.suppress(Exception):
            region_id = self._effective_region_id()
            rows = (
                self.db.get_packaging(
                    self.company_id, region_id, manager_id=self.manager_id
                )
                or []
            )
            self._packaging_by_id = {
                p.get("id"): p for p in rows if isinstance(p, dict) and p.get("id")
            }

    def _refresh_category_lookup_cache(self) -> None:
        with contextlib.suppress(Exception):
            region_id = self._effective_region_id()
            rows = (
                self.db.get_company_categories(
                    self.company_id, region_id, manager_id=self.manager_id
                )
                or []
            )
            self._categories_by_id = {
                c.get("id"): c for c in rows if isinstance(c, dict) and c.get("id")
            }

    def _apply_saved_product_to_cache(self, product_id: str, payload: dict) -> None:
        """Optimistic in-memory patch after a successful edit: merge the saved
        fields into the cached row and re-attach supplier/packaging/category
        (from the already-warm lookups) — no DB re-fetch needed.

        Those lookup dicts are only rebuilt on a full load_products(force_reload=True)
        (first load / tab switch), so a supplier/packaging/category picked during
        THIS edit that isn't in them yet (e.g. just created moments ago) would
        otherwise fall back to {} and render as "None" until the next full
        reload. Do one cheap targeted re-fetch of just that lookup instead."""
        pid = str(product_id or "").strip()
        if not pid:
            return
        existing = self._products_cache.get(pid) or {}
        row = {**existing, **payload, "id": pid}
        sid = str(row.get("supplier_id") or "").strip()
        pkid = str(row.get("packaging_id") or "").strip()
        cid = str(row.get("category_id") or "").strip()
        if sid and sid not in self._suppliers_by_id:
            self._refresh_supplier_lookup_cache()
        if pkid and pkid not in self._packaging_by_id:
            self._refresh_packaging_lookup_cache()
        if cid and cid not in self._categories_by_id:
            self._refresh_category_lookup_cache()
        if sid:
            row["supplier"] = self._suppliers_by_id.get(sid) or {}
        if pkid:
            row["packaging"] = self._packaging_by_id.get(pkid) or {}
        if cid:
            row["category"] = self._categories_by_id.get(cid) or {}
        row["_display_name"] = self._resolve_product_display_name(row)
        self._products_cache[pid] = row
        self._sync_product_to_shared_map(pid, row)

    def _recompute_display_names(self, rows: list) -> None:
        """Resolve each row's base display name, then add/remove the
        cross-product disambiguation suffix (" | Кат: X" / " | Постач: Y")
        for any name that currently repeats within `rows`. Always starts from
        the base name (never appends onto an already-suffixed value), so a
        product whose name just STOPPED colliding with another correctly
        loses its suffix too — not just gains one."""
        for row in rows:
            if isinstance(row, dict):
                row["_display_name"] = self._resolve_product_display_name(row)
        _node_path_of = product_naming.make_node_path_resolver(
            self.db, self._product_path_cache
        )
        _name_suffixes = product_naming.compute_name_suffixes(
            rows,
            name_of=lambda p: str(p.get("_display_name") or ""),
            category_of=lambda p: product_naming.resolve_category_label(
                p, node_path_of=_node_path_of, category_by_id=self._categories_by_id
            ),
            supplier_of=lambda p: product_naming.resolve_supplier_label(
                p, supplier_by_id=self._suppliers_by_id
            ),
        )
        for row in rows:
            if not isinstance(row, dict):
                continue
            _suf = _name_suffixes.get(str(row.get("id") or ""))
            if _suf:
                row["_display_name"] = row["_display_name"] + _suf

    # ──────────────────── cross-tab live sync (shared map) ────────────────────
    # Product mutations still write to the DB immediately (dialog-driven single
    # actions with an edit-lock, unlike Orders' per-keystroke inline edits — no
    # outbox/45s-batching needed here). This section only makes an already-saved
    # change appear instantly in every OTHER open Товари tab/session, without an
    # app restart, mirroring orders_view.py's shared-map + resume() pulse pattern.

    def _page(self):
        try:
            return self.content_area.page if self.content_area else None
        except RuntimeError:
            return None

    def _app_data_dir(self) -> str:
        base = os.getenv("FLET_APP_STORAGE_DATA") or os.path.join(
            os.getenv("APPDATA") or os.path.expanduser("~"), "CatManPro"
        )
        os.makedirs(base, exist_ok=True)
        return base

    def _sync_product_to_shared_map(self, pid: str, product: dict) -> None:
        """Write a COPY of product to the shared map after a save/archive/restore."""
        if pid and isinstance(product, dict):
            self._shared_products_map[pid] = dict(product)
            self._bump_products_sync_seq()

    def _sync_products_to_shared_map(self) -> None:
        """Populate the shared map after the first load in this session.
        New entries: store a copy, keep local as-is. Existing entries (another
        tab already loaded first): adopt the shared copy as the local truth."""
        for pid, product in list(self._products_cache.items()):
            if not isinstance(product, dict):
                continue
            existing = self._shared_products_map.get(pid)
            if existing is None:
                self._shared_products_map[pid] = dict(product)
            else:
                self._products_cache[pid] = dict(existing)

    def _sync_products_from_shared_map(self) -> list:
        """Compare cached products with the shared map; replace changed local
        entries with a fresh copy and return them (for other tabs' edits)."""
        if not self._shared_products_map:
            return []
        updated: list = []
        for pid, local in list(self._products_cache.items()):
            if not isinstance(local, dict):
                continue
            shared = self._shared_products_map.get(pid)
            if shared is None:
                continue
            if any(
                str(shared.get(k) or "") != str(local.get(k) or "")
                for k in shared
                if not str(k).startswith("_")
            ):
                self._products_cache[pid] = dict(shared)
                updated.append(shared)
        # Pick up brand-new products saved in another tab this session, and
        # drop ones archived/deleted there — otherwise they'd only appear
        # after a full reload (see the equivalent payments_view.py fix).
        deleted_ids = self._shared_products_map.get("__deleted_ids__")
        if isinstance(deleted_ids, list):
            for pid in deleted_ids:
                if self._products_cache.pop(str(pid), None) is not None:
                    updated.append({"id": str(pid), "_deleted": True})
        # The shared map accumulates products from every region visited this
        # session (not just the active one), so "brand-new" pickup must be
        # scoped to the current region filter — otherwise switching regions
        # once and then saving anything leaks the other region's products
        # into this view via the background sync loop.
        region_id = self._effective_region_id()
        for pid, shared in self._shared_products_map.items():
            if pid.startswith("__") and pid.endswith("__"):
                continue
            if pid in self._products_cache or not isinstance(shared, dict):
                continue
            if region_id and str(shared.get("region_id") or "") != str(region_id):
                continue
            self._products_cache[pid] = dict(shared)
            updated.append(shared)
        return updated

    def _bump_products_sync_seq(self) -> None:
        try:
            seq = int(self._shared_products_map.get("__sync_seq__", 0) or 0)
            self._shared_products_map["__sync_seq__"] = seq + 1
            cb = self._shared_products_map.get("__on_change__")
            if callable(cb):
                cb()
        except Exception:
            pass

    async def _bg_products_sync_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(1.5)
                if not self._products_ui_ready:
                    continue
                seq = int(self._shared_products_map.get("__sync_seq__", 0) or 0)
                if seq != self._bg_products_sync_seq:
                    self._bg_products_sync_seq = seq
                    changed = self._sync_products_from_shared_map()
                    if changed:
                        self.load_products()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def _start_bg_products_sync(self) -> None:
        page = self._page()
        if page and hasattr(page, "run_task"):
            with contextlib.suppress(Exception):
                page.run_task(self._bg_products_sync_loop)

    def _products_scope_pulse_ts(self) -> int:
        try:
            out = self.db.get_companies_sync_pulses([self.company_id], ["products"]) or {}
            by_company = out.get(self.company_id) if isinstance(out, dict) else {}
            ts = (by_company or {}).get("products") if isinstance(by_company, dict) else 0
            return int(ts or 0)
        except Exception:
            return 0

    async def resume(self) -> None:
        """Called when returning to a cached Товари tab. Syncs cross-tab changes
        from the shared map, then a background DB-pulse check reloads only if
        another session/device changed something (own-session writes are
        already reflected via the shared map, so no reload flicker for those)."""
        if not self._products_ui_ready:
            return
        with contextlib.suppress(Exception):
            changed = self._sync_products_from_shared_map()
            if changed:
                self.load_products()

        async def _bg_products_pulse_check():
            try:
                new_pulse = await asyncio.to_thread(self._products_scope_pulse_ts)
                cached_pulse = int(self._products_snapshot_pulse_ts or 0)
                if new_pulse > cached_pulse:
                    session_db_pulse = int(
                        (self._shared_products_map or {}).get(
                            "__session_db_pulse__", 0
                        )
                        or 0
                    )
                    if new_pulse <= session_db_pulse:
                        self._products_snapshot_pulse_ts = new_pulse
                        return
                    # Genuine external change — force a full reload.
                    self._products_snapshot_pulse_ts = new_pulse
                    self.load_products(force_reload=True)
            except Exception:
                pass

        _pg = self._page()
        if _pg and hasattr(_pg, "run_task"):
            with contextlib.suppress(Exception):
                _pg.run_task(_bg_products_pulse_check)

    def _pick_local_image_file(self):
        if not TK_AVAILABLE:
            raise RuntimeError("Локальний діалог файлів недоступний")
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title="Оберіть зображення товару",
            filetypes=[
                ("Зображення", "*.jpg *.jpeg *.png *.gif *.bmp *.webp"),
                ("Усі файли", "*.*"),
            ],
        )
        root.destroy()
        return path

    def _compress_image(self, input_path):
        if not Image:
            return input_path
        try:
            temp_output = f"temp_{uuid.uuid4()}.jpg"
            with Image.open(input_path) as img:
                img = img.convert("RGB")
                img.thumbnail((1000, 1000))
                img.save(temp_output, "JPEG", quality=75)
            return temp_output
        except Exception:
            logger.exception("Product image compression error")
            return input_path

    def _state_ids(self, values, single_value=None):
        out = []
        if isinstance(values, (list, tuple, set)):
            out = [str(v).strip() for v in values if str(v).strip()]
        elif values not in (None, ""):
            out = [str(values).strip()]
        elif single_value not in (None, ""):
            out = [str(single_value).strip()]
        return set([x for x in out if x])

    def _normalize_level_title(self, value):
        return " ".join(str(value or "").strip().lower().split())

    def _toggle_product_sort_order(self, order: str):
        order = "desc" if str(order or "").strip().lower() == "desc" else "asc"
        if self._product_sort_order == order:
            return
        self._product_sort_order = order
        self.load_products()
        self._emit_state_changed()

    def _refresh_sort_order_controls(self):
        if not getattr(self, "_sort_order_host", None):
            return
        self._sort_order_host.controls = [
            ft.Text("Сортування:", size=10, color="grey700", weight="w500"),
            ft.OutlinedButton(
                "А→Я",
                height=24,
                style=ft.ButtonStyle(
                    padding=ft.padding.symmetric(horizontal=6, vertical=0),
                    text_style=ft.TextStyle(size=10, weight="w500"),
                    bgcolor=(_BORDER if self._product_sort_order == "asc" else None),
                ),
                on_click=lambda _: self._toggle_product_sort_order("asc"),
            ),
            ft.OutlinedButton(
                "Я→А",
                height=24,
                style=ft.ButtonStyle(
                    padding=ft.padding.symmetric(horizontal=6, vertical=0),
                    text_style=ft.TextStyle(size=10, weight="w500"),
                    bgcolor=(_BORDER if self._product_sort_order == "desc" else None),
                ),
                on_click=lambda _: self._toggle_product_sort_order("desc"),
            ),
        ]

    def _emit_state_changed(self):
        if not callable(self._on_state_change):
            return
        try:
            category_ids = sorted(self._selected_category_filter_ids)
            subcategory_ids = sorted(self._selected_subcategory_filter_ids)
            brand_ids = sorted(self._selected_brand_filter_ids)
            series_ids = sorted(self._selected_series_filter_ids)
            self._on_state_change(
                {
                    "category_id": category_ids[0] if len(category_ids) == 1 else "",
                    "category_ids": category_ids,
                    "subcategory_id": (
                        subcategory_ids[0] if len(subcategory_ids) == 1 else ""
                    ),
                    "subcategory_ids": subcategory_ids,
                    "brand_id": brand_ids[0] if len(brand_ids) == 1 else "",
                    "brand_ids": brand_ids,
                    "series_id": series_ids[0] if len(series_ids) == 1 else "",
                    "series_ids": series_ids,
                    "filters_expanded": bool(self._advanced_filters_expanded),
                    "sort_order": str(self._product_sort_order or "asc"),
                    "query": (
                        (self.search_f.value or "").strip()
                        if getattr(self, "search_f", None)
                        else ""
                    ),
                }
            )
        except Exception:
            logger.exception("ProductsView state callback failed")

    def _debounce_call(self, key, callback, delay=0.25):
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

    def _dialog_content_height(self, desired=620, min_height=280):
        try:
            page = self._page()
            page_h = getattr(page, "height", None) if page else None
            if not page_h and page:
                win = getattr(page, "window", None)
                page_h = getattr(win, "height", None) if win else None
            if isinstance(page_h, (int, float)) and page_h > 0:
                return max(min_height, min(desired, int(page_h * 0.78)))
        except Exception:
            pass
        return desired

    def _dialog_content_width(self, desired=820, min_width=600):
        try:
            page = self._page()
            page_w = getattr(page, "width", None) if page else None
            if not page_w and page:
                win = getattr(page, "window", None)
                page_w = getattr(win, "width", None) if win else None
            if isinstance(page_w, (int, float)) and page_w > 0:
                return max(min_width, int(page_w * 0.90))
        except Exception:
            pass
        return desired

    def _parse_hierarchy_order(self, value):
        raw = str(value or "").strip().lower()
        if not raw:
            return ["subcategory", "series", "brand"]
        out = []
        for part in raw.split(">"):
            token = part.strip()
            if token in {"subcategory", "series", "brand"} and token not in out:
                out.append(token)
        return out

    def _get_manager_name(self, company_id, manager_id):
        company_id = (company_id or "").strip()
        manager_id = (manager_id or "").strip()
        if not company_id or not manager_id:
            return ""
        if company_id not in self._manager_names_by_company_cache:
            rows = [
                m
                for m in (self.db.get_company_managers(company_id) or [])
                if isinstance(m, dict) and m.get("id")
            ]
            self._manager_names_by_company_cache[company_id] = {
                m.get("id"): (m.get("full_name") or m.get("email") or m.get("id"))
                for m in rows
            }
        return self._manager_names_by_company_cache.get(company_id, {}).get(
            manager_id, manager_id
        )

    def _packaging_label_with_owner(self, packaging_row, company_id):
        name = (
            (packaging_row.get("name") or "").strip()
            if isinstance(packaging_row, dict)
            else ""
        )
        if not name:
            return ""
        if not self._is_admin_or_superadmin():
            return name
        manager_id = (
            (packaging_row.get("manager_id") or "").strip()
            if isinstance(packaging_row, dict)
            else ""
        )
        if not manager_id:
            return f"{name} (без користувача)"
        owner_name = self._get_manager_name(company_id, manager_id)
        return f"{name} ({owner_name})"

    def _category_levels_config(self, category_row):
        cfg = self.db.get_category_levels_config(
            category_row if isinstance(category_row, dict) else {}
        )
        levels = []
        for row in (cfg or {}).get("levels", []):
            if not isinstance(row, dict):
                continue
            try:
                pos = int(row.get("position"))
            except Exception:
                continue
            if pos < 1 or pos > self.MAX_LEVELS:
                continue
            levels.append(
                {
                    "position": pos,
                    "title": (row.get("title") or f"Рівень {pos}").strip(),
                    "enabled": bool(row.get("enabled")),
                }
            )
        levels.sort(key=lambda x: x.get("position", 0))
        return {
            "levels": levels,
            "use_products": bool((cfg or {}).get("use_products", True)),
        }

    def _enabled_levels(self, category_row):
        cfg = self._category_levels_config(category_row)
        return [row for row in cfg.get("levels", []) if row.get("enabled")]

    def _dedupe_rows_by_id(self, rows):
        seen = set()
        out = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            row_id = row.get("id")
            if not row_id or row_id in seen:
                continue
            seen.add(row_id)
            out.append(row)
        return out

    def _resolve_product_display_name(self, product):
        name = (product.get("name") or "").strip() if isinstance(product, dict) else ""
        node_id = (
            product.get("catalog_node_id") if isinstance(product, dict) else None
        ) or ""

        if not node_id:
            return name or "Без назви"

        try:
            path_rows = self.db.get_node_path(node_id) or []
            # Lowest node name (series/detail leaf).
            leaf_name = ""
            for row in reversed(path_rows):
                if isinstance(row, dict):
                    node_name = (row.get("name") or "").strip()
                    if node_name:
                        leaf_name = node_name
                        break

            # If product has its own explicit name (and it differs from leaf),
            # prefer it. This fixes "list shows series, not configured product name".
            if name and leaf_name and name != leaf_name:
                return name

            # Legacy behavior:
            # - for series mode, display leaf node name when product.name is empty or equals leaf.
            # - for non-series nodes, also fallback to leaf name.
            if leaf_name:
                return leaf_name
        except Exception:
            pass

        return name or "Без назви"

    def _build_virtual_series_products(self, selected_region_id, real_rows):
        max_virtual_rows = 600
        existing_node_ids = {
            (r.get("catalog_node_id") or "")
            for r in (real_rows or [])
            if isinstance(r, dict) and (r.get("catalog_node_id") or "")
        }
        out = []

        company_ids = []
        if self.company_id:
            company_ids = [self.company_id]
        elif self.is_superadmin:
            company_ids = [
                c.get("id")
                for c in (self.db.get_all_companies() or [])
                if isinstance(c, dict) and c.get("id")
            ]

        for company_id in company_ids:
            try:
                regions = [
                    r
                    for r in (self.db.get_company_regions(company_id) or [])
                    if isinstance(r, dict) and r.get("id")
                ]
                if selected_region_id:
                    regions = [r for r in regions if r.get("id") == selected_region_id]

                for region in regions:
                    region_id = region.get("id")
                    categories = [
                        c
                        for c in (
                            self.db.get_company_categories(
                                company_id,
                                region_id,
                                manager_id=self.manager_id,
                            )
                            or []
                        )
                        if isinstance(c, dict) and c.get("id")
                    ]
                    for category in categories:
                        try:
                            cfg = self._category_levels_config(category)
                            enabled_levels = [
                                lvl
                                for lvl in cfg.get("levels", [])
                                if lvl.get("enabled")
                            ]
                            use_products = bool(cfg.get("use_products", True))
                            if use_products or not enabled_levels:
                                continue

                            parent_ids = [None]
                            leaf_rows = []
                            for lvl in enabled_levels:
                                pos = int(lvl.get("position"))
                                next_ids = []
                                next_rows = []
                                for parent_id in parent_ids:
                                    rows = self.db.get_category_level_nodes(
                                        category.get("id"),
                                        pos,
                                        parent_node_id=parent_id,
                                    )
                                    for row in rows or []:
                                        if not isinstance(row, dict) or not row.get(
                                            "id"
                                        ):
                                            continue
                                        next_ids.append(row.get("id"))
                                        next_rows.append(row)
                                parent_ids = next_ids
                                leaf_rows = next_rows
                                if not parent_ids:
                                    break

                            for leaf in leaf_rows:
                                leaf_id = leaf.get("id") or ""
                                leaf_name = (leaf.get("name") or "").strip()
                                if not leaf_id or not leaf_name:
                                    continue
                                if leaf_id in existing_node_ids:
                                    continue
                                existing_node_ids.add(leaf_id)
                                out.append(
                                    {
                                        "id": None,
                                        "company_id": company_id,
                                        "region_id": region_id,
                                        "category_id": category.get("id"),
                                        "catalog_node_id": leaf_id,
                                        "name": leaf_name,
                                        "is_series_mode": True,
                                        "manager_id": "",
                                        "supplier_id": None,
                                        "packaging_id": None,
                                        "purchase_price": "",
                                        "purchase_currency": "",
                                        "weight_kg": "",
                                        "production_term_days": "",
                                        "notes": "",
                                        "sales_prices_json": "",
                                        "supplier": {},
                                        "packaging": {},
                                        "category": {
                                            "id": category.get("id"),
                                            "name": category.get("name"),
                                        },
                                        "_virtual": True,
                                    }
                                )
                                if len(out) >= max_virtual_rows:
                                    return out
                        except Exception:
                            logger.exception(
                                "Build virtual rows failed for category %s",
                                category.get("id"),
                            )
            except Exception:
                logger.exception("Build virtual rows failed for company %s", company_id)

        return out

    def _pending_series_candidates(self, company_id, region_id=None):
        """Build pending series-as-product candidates on demand for quick completion modal."""
        if not company_id:
            return []
        out = []
        try:
            existing_node_ids = self.db.get_product_catalog_node_ids(
                company_id, region_id
            )

            regions = [
                r
                for r in (self.db.get_company_regions(company_id) or [])
                if isinstance(r, dict) and r.get("id")
            ]
            if region_id:
                regions = [r for r in regions if r.get("id") == region_id]

            for region in regions:
                rid = region.get("id")
                categories = [
                    c
                    for c in (
                        self.db.get_company_categories(
                            company_id,
                            rid,
                            manager_id=self.manager_id,
                        )
                        or []
                    )
                    if isinstance(c, dict) and c.get("id")
                ]
                for category in categories:
                    category_id = category.get("id")
                    cfg = self._category_levels_config(category)
                    enabled_levels = sorted(
                        [lvl for lvl in cfg.get("levels", []) if lvl.get("enabled")],
                        key=lambda x: int(x.get("position") or 0),
                    )
                    use_products = bool(cfg.get("use_products", True))
                    if use_products or not enabled_levels:
                        continue

                    # IMPORTANT: use level_nodes, not catalog_nodes, so deleted/obsolete
                    # hierarchy rows don't leak into the pending-series counter.
                    parent_ids = [None]
                    leaf_rows = []
                    for lvl in enabled_levels:
                        pos = int(lvl.get("position"))
                        next_ids = []
                        next_rows = []
                        seen_next_ids = set()
                        for parent_id in parent_ids:
                            level_nodes = self.db.get_category_level_nodes(
                                category_id,
                                pos,
                                parent_node_id=parent_id,
                            )
                            for node in level_nodes or []:
                                if not isinstance(node, dict):
                                    continue
                                child_id = str(node.get("id") or "").strip()
                                if not child_id or child_id in seen_next_ids:
                                    continue
                                seen_next_ids.add(child_id)
                                next_ids.append(child_id)
                                next_rows.append(
                                    {
                                        "id": child_id,
                                        "name": str(node.get("name") or "").strip(),
                                    }
                                )
                        parent_ids = next_ids
                        leaf_rows = next_rows
                        if not parent_ids:
                            break

                    # Skip leaf nodes that have descendants (children on next level)
                    # to avoid showing "pending" series that structurally exist.
                    leaf_ids_with_children = set()
                    if enabled_levels:
                        last_pos = int(enabled_levels[-1].get("position") or 0)
                        next_pos = last_pos + 1
                        for leaf in leaf_rows:
                            leaf_id = leaf.get("id") or ""
                            if not leaf_id:
                                continue
                            children = self.db.get_category_level_nodes(
                                category_id, next_pos, parent_node_id=leaf_id
                            )
                            if children:
                                leaf_ids_with_children.add(leaf_id)

                    for leaf in leaf_rows:
                        leaf_id = leaf.get("id") or ""
                        leaf_name = (leaf.get("name") or "").strip()
                        if (
                            not leaf_id
                            or not leaf_name
                            or leaf_id in existing_node_ids
                            or leaf_id in leaf_ids_with_children
                        ):
                            continue
                        out.append(
                            {
                                "company_id": company_id,
                                "region_id": rid,
                                "region_name": region.get("name") or "",
                                "category_id": category.get("id"),
                                "category_name": category.get("name") or "",
                                "catalog_node_id": leaf_id,
                                "name": leaf_name,
                            }
                        )

            out.sort(
                key=lambda r: (
                    (r.get("region_name") or "").lower(),
                    (r.get("category_name") or "").lower(),
                    (r.get("name") or "").lower(),
                )
            )
        except Exception:
            logger.exception("Build pending series candidates failed")
            return []
        return out

    def show_series_completion_dialog(self):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        company_id = self.company_id
        if not company_id:
            self._show_snack("Оберіть компанію, щоб доробити серію в товар", "orange")
            return

        region_id = self._effective_region_id()

        search_f = ft.TextField(
            label="Пошук",
            hint_text="серія / категорія / регіон",
            border_radius=10,
            prefix_icon=ft.Icons.SEARCH,
            width=560,
            height=34,
            text_size=11,
            content_padding=ft.padding.symmetric(horizontal=8, vertical=5),
        )
        series_f = ft.Dropdown(label="Серія для доробки", options=[], border_radius=10)
        category_locked_f = ft.TextField(
            label="Категорія", border_radius=10, disabled=True
        )
        hierarchy_locked_f = ft.TextField(
            label="Рівні ієрархії",
            border_radius=10,
            disabled=True,
            multiline=True,
            min_lines=2,
            max_lines=4,
        )
        detail_toggle = ft.Checkbox(
            label="Увімкнути деталізацію (товар нижче серії)", value=False
        )
        detail_select = ft.Dropdown(label="Деталізація", options=[], border_radius=10)
        detail_new_name_f = ft.TextField(
            label="Нова деталізація", border_radius=10, visible=False
        )
        detail_hint = ft.Text("", size=11, color="grey700")
        detail_block = ft.Column(
            [detail_select, detail_new_name_f, detail_hint], spacing=6, visible=False
        )
        supplier_f = ft.Dropdown(label=self.t("orders.filter.supplier", "Постачальник"), options=[], border_radius=10)
        packaging_f = ft.Dropdown(label=self.t("nav.packaging", "Пакування"), options=[], border_radius=10, visible=self.packaging_enabled)
        purchase_price_f = ft.TextField(label=self.t("products.field.purchase_price", "Ціна закупівлі"), border_radius=10)
        purchase_currency_f = ft.Dropdown(
            label=self.t("products.field.purchase_currency", "Валюта закупівлі"),
            options=[ft.dropdown.Option(x) for x in self.CURRENCIES],
            border_radius=10,
            value="USD",
            disabled=True,
        )
        purchase_sum_usd_f = ft.TextField(
            label="Сума закупівлі ($)",
            border_radius=10,
            read_only=True,
            visible=False,
        )
        weight_f = ft.TextField(label=self.t("products.field.weight", "Вага (кг)"), border_radius=10)
        production_term_f = ft.TextField(
            label="Термін виробництва (дні)", border_radius=10
        )
        sales_prices_container = ft.Column(spacing=6)
        notes_f = ft.TextField(
            label=self.t("products.field.notes", "Нотатки"), border_radius=10, multiline=True, min_lines=2, max_lines=4
        )

        # Product images (up to 3) for series completion flow
        _sc_temp_paths = [None, None, None]
        _sc_slot_texts = [
            ft.Text(self.t("products.field.photo_not_loaded", "Не завантажено"), size=11, color="grey700")
            for _ in range(3)
        ]

        def _sc_make_pick_fn(slot_idx: int):
            async def _pick(_=None):
                try:
                    local_path = self._pick_local_image_file()
                    if not local_path:
                        return
                    if not os.path.exists(local_path):
                        self._show_snack(self.t("packaging.err.file_not_found", "Вибраний файл не знайдено"), "red")
                        return
                    _sc_temp_paths[slot_idx] = local_path
                    _sc_slot_texts[slot_idx].value = f"Нове: {os.path.basename(local_path)}"
                    with contextlib.suppress(Exception):
                        _sc_slot_texts[slot_idx].update()
                    self._show_snack(
                        self.t("products.field.photo_selected", "Фото {n} вибрано").format(n=slot_idx + 1), "blue"
                    )
                except Exception as ex:
                    logger.exception("Series completion image picking failed")
                    self._show_snack(self.t("products.err.photo_select", "Помилка вибору фото: {ex}").format(ex=ex), "red")
            return _pick

        def _sc_make_clear_fn(slot_idx: int):
            def _clear(_=None):
                _sc_temp_paths[slot_idx] = None
                _sc_slot_texts[slot_idx].value = self.t("products.field.photo_not_loaded", "Не завантажено")
                with contextlib.suppress(Exception):
                    _sc_slot_texts[slot_idx].update()
            return _clear

        candidate_map = {}
        all_candidates_ref = {"items": []}
        current_candidate_ref = {"row": None}
        extra_level_pos_ref = {"value": None}
        detail_rows_ref = {"rows": []}
        supplier_currency_map_ref = {"map": {}}
        loading_text = ft.Text("Завантаження серій...", color="grey700", size=12)
        loading_ring = ft.ProgressRing(width=22, height=22, stroke_width=2)
        form_block = ft.Column(
            [
                series_f,
                category_locked_f,
                hierarchy_locked_f,
                detail_toggle,
                detail_block,
                ft.Row(
                    [
                        ft.Container(width=360, content=supplier_f),
                        ft.Container(width=360, content=packaging_f),
                    ],
                    spacing=10,
                    wrap=True,
                ),
                ft.Row(
                    [purchase_price_f, purchase_currency_f, purchase_sum_usd_f],
                    spacing=10,
                    wrap=True,
                ),
                ft.Row(
                    [
                        ft.Container(width=260, content=weight_f),
                        ft.Container(width=260, content=production_term_f),
                    ],
                    spacing=10,
                    wrap=True,
                ),
                ft.Column(
                    [
                        ft.Text(self.t("products.field.photos_up_to_3", "Фото товару (до 3-х):"), size=12, weight="bold"),
                        *[
                            ft.Row(
                                [
                                    ft.Text(self.t("products.field.photo_n", "Фото {n}:").format(n=_si + 1),
                                            size=11, color="grey700", width=55),
                                    ft.IconButton(
                                        ft.Icons.UPLOAD_FILE,
                                        tooltip=self.t("products.field.upload_photo_n", "Завантажити фото {n}").format(n=_si + 1),
                                        icon_size=18,
                                        on_click=lambda _, i=_si: asyncio.create_task(
                                            _sc_make_pick_fn(i)()
                                        ),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.DELETE_OUTLINE,
                                        tooltip=self.t("products.field.delete_photo_n", "Видалити фото {n}").format(n=_si + 1),
                                        icon_size=18,
                                        icon_color="red300",
                                        on_click=lambda _, i=_si: _sc_make_clear_fn(i)(),
                                    ),
                                    _sc_slot_texts[_si],
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
                ft.Text("Ціни продажу (по рівнях регіону)", size=12, weight="bold"),
                sales_prices_container,
                notes_f,
            ],
            spacing=10,
            visible=False,
        )

        def _sync_detail_new_name_visibility():
            detail_new_name_f.visible = (
                detail_block.visible and detail_select.value == self.NEW_ITEM_KEY
            )

        def _load_sales_prices_for_region(region_id):
            sales_prices_container.controls.clear()
            tiers = self.db.get_effective_region_price_tiers_for_manager(
                self.company_id,
                self.manager_id,
                region_id,
            )
            for t in tiers:
                if not isinstance(t, dict):
                    continue
                tf = ft.TextField(
                    label=f"{t.get('name')} ({t.get('currency')})",
                    border_radius=10,
                    width=260,
                )
                tf.data = {
                    "tier_id": t.get("id"),
                    "name": t.get("name"),
                    "currency": t.get("currency"),
                }
                sales_prices_container.controls.append(tf)
            if not sales_prices_container.controls:
                sales_prices_container.controls.append(
                    ft.Text(
                        "Для цього регіону не налаштовано рівні цін",
                        size=11,
                        color="grey600",
                    )
                )

        def _update_detail_options(row, keep_selection=False):
            detail_rows_ref["rows"] = []
            extra_level_pos_ref["value"] = None
            detail_select.options = [
                ft.dropdown.Option(
                    key=self.NEW_ITEM_KEY, text="Створити нову деталізацію"
                )
            ]
            detail_select.value = self.NEW_ITEM_KEY
            detail_hint.value = ""
            _sync_detail_new_name_visibility()
            if not row:
                return

            path_rows = self.db.get_node_path(row.get("catalog_node_id")) or []
            if not path_rows:
                detail_hint.value = "Не вдалося визначити позицію серії для деталізації"
                return

            try:
                leaf_pos = max(
                    int(p.get("position", 0)) for p in path_rows if isinstance(p, dict)
                )
            except Exception:
                leaf_pos = 0
            extra_pos = leaf_pos + 1
            extra_level_pos_ref["value"] = extra_pos

            if extra_pos > self.MAX_LEVELS:
                detail_hint.value = "Досягнуто максимальної глибини ієрархії"
                return

            existing_rows = (
                self.db.get_category_level_nodes(
                    row.get("category_id"),
                    extra_pos,
                    parent_node_id=row.get("catalog_node_id"),
                )
                or []
            )
            detail_rows_ref["rows"] = [
                r for r in existing_rows if isinstance(r, dict) and r.get("id")
            ]

            options = [
                ft.dropdown.Option(
                    key=r.get("id"), text=r.get("name") or f"Рівень {extra_pos}"
                )
                for r in detail_rows_ref["rows"]
            ]
            options.append(
                ft.dropdown.Option(
                    key=self.NEW_ITEM_KEY, text="Створити нову деталізацію"
                )
            )
            previous = detail_select.value if keep_selection else None
            detail_select.options = options
            valid_keys = {getattr(opt, "key", None) for opt in detail_select.options}
            detail_select.value = (
                previous if previous in valid_keys else self.NEW_ITEM_KEY
            )
            detail_hint.value = f"Позиція деталізації: рівень {extra_pos}. Можна обрати існуючу або створити нову."
            _sync_detail_new_name_visibility()

        def _on_detail_toggle(_):
            detail_block.visible = bool(detail_toggle.value)
            if detail_block.visible:
                _update_detail_options(
                    current_candidate_ref.get("row"), keep_selection=True
                )
            else:
                detail_new_name_f.value = ""
            _sync_detail_new_name_visibility()
            with contextlib.suppress(Exception):
                detail_block.update()
            with contextlib.suppress(Exception):
                detail_new_name_f.update()

        def _on_detail_select_change(_):
            _sync_detail_new_name_visibility()
            with contextlib.suppress(Exception):
                detail_new_name_f.update()

        def _refresh_purchase_sum_usd(_=None):
            ccy = self._normalize_currency_code(purchase_currency_f.value, "USD")
            price_val = self._to_float_or_none(purchase_price_f.value)
            usd_val = self._convert_to_usd(price_val, ccy)
            show = usd_val is not None and ccy not in {"USD", "$"}
            purchase_sum_usd_f.visible = bool(show)
            purchase_sum_usd_f.value = f"{usd_val:.2f}" if show else ""

        def _fill_suppliers_packaging(row):
            rid = row.get("region_id")
            owner_id = self.manager_id or None
            suppliers = self.db.get_suppliers(company_id, rid, manager_id=owner_id)
            supplier_currency_map_ref["map"] = {
                s.get("id"): self._normalize_currency_code(s.get("currency"), "USD")
                for s in (suppliers or [])
                if isinstance(s, dict) and s.get("id")
            }
            supplier_f.options = [
                ft.dropdown.Option(key=s.get("id"), text=s.get("name"))
                for s in (suppliers or [])
                if isinstance(s, dict) and s.get("id")
            ]
            supplier_f.value = supplier_f.options[0].key if supplier_f.options else None
            purchase_currency_f.value = self._normalize_currency_code(
                supplier_currency_map_ref["map"].get(supplier_f.value, "USD"),
                "USD",
            )
            _refresh_purchase_sum_usd()

            packaging_rows = self.db.get_packaging(
                company_id, rid, manager_id=owner_id, supplier_rows=suppliers
            )
            packaging_f.options = [ft.dropdown.Option(key="", text="None")]
            for p in packaging_rows:
                if isinstance(p, dict) and p.get("id"):
                    packaging_f.options.append(
                        ft.dropdown.Option(
                            key=p.get("id"),
                            text=self._packaging_label_with_owner(p, company_id),
                        )
                    )
            packaging_f.value = ""

        def _update_locked_fields(row):
            category_locked_f.value = row.get("category_name") or "-"
            path_rows = self.db.get_node_path(row.get("catalog_node_id")) or []
            path_names = [
                n.get("name")
                for n in path_rows
                if isinstance(n, dict) and n.get("name")
            ]
            hierarchy_locked_f.value = " -> ".join(path_names) if path_names else "-"

        def _on_series_change(_):
            row = candidate_map.get(series_f.value)
            current_candidate_ref["row"] = row
            if not row:
                return
            production_term_f.value = str(row.get("production_term_days") or "")
            _update_locked_fields(row)
            _update_detail_options(row, keep_selection=False)
            _fill_suppliers_packaging(row)
            _load_sales_prices_for_region(row.get("region_id"))
            with contextlib.suppress(Exception):
                supplier_f.update()
            with contextlib.suppress(Exception):
                packaging_f.update()
            with contextlib.suppress(Exception):
                sales_prices_container.update()

        def _render_candidate_options(filtered_rows):
            ordered_rows = sorted(
                list(filtered_rows or []),
                key=lambda r: (
                    1 if r.get("_has_completion_detail") else 0,
                    (r.get("region_name") or "").lower(),
                    (r.get("category_name") or "").lower(),
                    (r.get("name") or "").lower(),
                ),
            )
            candidate_map.clear()
            series_f.options = []
            for idx, row in enumerate(ordered_rows):
                key = f"cand_{idx}"
                text = f"{row.get('name') or '-'} | {row.get('category_name') or '-'} | {row.get('region_name') or '-'}"
                series_f.options.append(ft.dropdown.Option(key=key, text=text))
                candidate_map[key] = row

            if series_f.options:
                series_f.value = series_f.options[0].key
                _on_series_change(None)
            else:
                series_f.value = None
                current_candidate_ref["row"] = None
                category_locked_f.value = "-"
                hierarchy_locked_f.value = "-"
                supplier_f.options = []
                supplier_f.value = None
                packaging_f.options = [ft.dropdown.Option(key="", text="None")]
                packaging_f.value = ""
                detail_toggle.value = False
                detail_block.visible = False
                detail_new_name_f.value = ""
                _update_detail_options(None)
                sales_prices_container.controls.clear()

        def _apply_local_filter(_=None):
            query = (search_f.value or "").strip().lower()
            rows = all_candidates_ref["items"]
            if query:
                tokens = [t for t in query.split() if t]

                def _match(row):
                    hay = " ".join(
                        [
                            str(row.get("name") or ""),
                            str(row.get("category_name") or ""),
                            str(row.get("region_name") or ""),
                        ]
                    ).lower()
                    return all(tok in hay for tok in tokens)

                rows = [r for r in rows if _match(r)]
            _render_candidate_options(rows)
            with contextlib.suppress(Exception):
                series_f.update()

        def _on_supplier_change(_):
            row = current_candidate_ref.get("row")
            if not row:
                return
            rid = row.get("region_id")
            owner_id = self.manager_id or None
            prev_packaging = packaging_f.value
            suppliers = self.db.get_suppliers(company_id, rid, manager_id=owner_id)
            supplier_currency_map_ref["map"] = {
                s.get("id"): self._normalize_currency_code(s.get("currency"), "USD")
                for s in (suppliers or [])
                if isinstance(s, dict) and s.get("id")
            }
            packaging_rows = self.db.get_packaging(
                company_id, rid, manager_id=owner_id, supplier_rows=suppliers
            )
            packaging_f.options = [ft.dropdown.Option(key="", text="None")]
            for p in packaging_rows:
                if isinstance(p, dict) and p.get("id"):
                    packaging_f.options.append(
                        ft.dropdown.Option(
                            key=p.get("id"),
                            text=self._packaging_label_with_owner(p, company_id),
                        )
                    )
            valid_packaging_ids = {
                getattr(opt, "key", None) for opt in packaging_f.options
            }
            packaging_f.value = (
                prev_packaging if prev_packaging in valid_packaging_ids else ""
            )
            purchase_currency_f.value = self._normalize_currency_code(
                supplier_currency_map_ref["map"].get(supplier_f.value, "USD"),
                "USD",
            )
            _refresh_purchase_sum_usd()
            with contextlib.suppress(Exception):
                packaging_f.update()
            with contextlib.suppress(Exception):
                purchase_currency_f.update()

        def _cancel(_):
            for _i in range(3):
                _sc_temp_paths[_i] = None
            self._close_dlg(dlg)

        def _save(_):
            row = current_candidate_ref.get("row")
            if not row:
                self._show_snack(self.t("products.select_series", "Оберіть серію"), "orange")
                return

            target_node_id = row.get("catalog_node_id")
            product_name = row.get("name") or f"Чернетка {str(uuid.uuid4())[:8]}"

            if detail_toggle.value:
                extra_pos = extra_level_pos_ref.get("value")
                if not extra_pos or extra_pos > self.MAX_LEVELS:
                    self._show_snack(
                        "Не вдалося визначити рівень деталізації", "orange"
                    )
                    return
                if detail_select.value == self.NEW_ITEM_KEY:
                    new_detail_name = (detail_new_name_f.value or "").strip()
                    if not new_detail_name:
                        self._show_snack("Введіть назву нової деталізації", "orange")
                        return
                    created = self.db.find_or_create_level_node(
                        row.get("category_id"),
                        extra_pos,
                        new_detail_name,
                        parent_node_id=row.get("catalog_node_id"),
                    )
                    created_id = (
                        created.get("id")
                        if isinstance(created, dict)
                        else str(created or "").strip()
                    )
                    if not created_id:
                        self._show_snack("Не вдалося створити деталізацію", "red")
                        return
                    target_node_id = created_id
                    product_name = new_detail_name.strip() or product_name
                else:
                    selected = next(
                        (
                            r
                            for r in detail_rows_ref.get("rows", [])
                            if r.get("id") == detail_select.value
                        ),
                        None,
                    )
                    if not selected:
                        self._show_snack(
                            "Оберіть деталізацію або створіть нову", "orange"
                        )
                        return
                    target_node_id = selected.get("id")
                    product_name = (
                        selected.get("name") or product_name
                    ).strip() or product_name

            # Duplicate name check: block only if same name + same category + same supplier
            _row_category_id = str(row.get("category_id") or "").strip()
            _row_region_id = str(row.get("region_id") or "").strip()
            _new_supplier_id = str(supplier_f.value or "").strip()
            _existing_products = (
                self.db.get_products(
                    company_id, _row_region_id, manager_id=self.manager_id
                )
                or []
            )
            _norm_name = product_name.strip().lower()
            for _ep in _existing_products:
                if not isinstance(_ep, dict):
                    continue
                if (_ep.get("name") or "").strip().lower() != _norm_name:
                    continue
                if str(_ep.get("category_id") or "").strip() != _row_category_id:
                    continue
                if str(_ep.get("supplier_id") or "").strip() != _new_supplier_id:
                    continue
                self._show_snack(
                    "Товар з такою назвою вже існує в цій категорії та постачальника",
                    "red",
                )
                return

            payload = {
                "company_id": company_id,
                "region_id": row.get("region_id"),
                "category_id": row.get("category_id"),
                "catalog_node_id": target_node_id,
                "name": product_name,
                "is_series_mode": True,
                "image": None,
                "manager_id": self.manager_id or "",
                "supplier_id": supplier_f.value or None,
                "packaging_id": (packaging_f.value or None) if self.packaging_enabled else None,
                "purchase_price": self._to_float_or_none(purchase_price_f.value),
                "purchase_currency": self._normalize_currency_code(
                    purchase_currency_f.value, "USD"
                ),
                "sales_prices_json": "[]",
                "weight_kg": self._to_float_or_none(weight_f.value),
                "production_term_days": self._to_float_or_none(production_term_f.value),
                "notes": notes_f.value or "",
            }

            sales_rows = []
            for ctrl in sales_prices_container.controls:
                if isinstance(ctrl, ft.TextField) and isinstance(
                    getattr(ctrl, "data", None), dict
                ):
                    val = self._to_float_or_none(ctrl.value)
                    if val is None:
                        continue
                    sales_rows.append(
                        {
                            "tier_id": ctrl.data.get("tier_id"),
                            "name": ctrl.data.get("name"),
                            "currency": ctrl.data.get("currency"),
                            "price": val,
                        }
                    )
            payload["sales_prices_json"] = json.dumps(sales_rows, ensure_ascii=False)

            _img_keys = ("image", "image2", "image3")
            for _si, _img_key in enumerate(_img_keys):
                if _sc_temp_paths[_si]:
                    compressed = self._compress_image(_sc_temp_paths[_si])
                    uploaded_url = self.db.upload_file(
                        "products", compressed, f"product_{uuid.uuid4()}.jpg"
                    )
                    if uploaded_url:
                        payload[_img_key] = uploaded_url
                    if os.path.exists(compressed) and "temp_" in compressed:
                        with contextlib.suppress(Exception):
                            os.remove(compressed)
                    _sc_temp_paths[_si] = None

            ok = self.db.add_product(payload)
            if ok:
                self._close_dlg(dlg)
                self._show_snack(self.t("products.series_completed", "Серію дороблено в товар"), "green")
                self._trigger_products_reload()
            else:
                self._show_snack(self.t("products.err.save", "Не вдалося зберегти товар"), "red")

        series_f.on_select = _on_series_change

        # Always keep candidates fresh: user can delete/modify series elsewhere while this dialog is open.
        def _reload_candidates(_e=None):
            try:
                loading_ring.visible = True
                loading_text.visible = True
                loading_text.value = "Оновлення списку серій..."
                form_block.visible = False
                save_btn.disabled = True
                with contextlib.suppress(Exception):
                    loading_ring.update()
                with contextlib.suppress(Exception):
                    loading_text.update()
                with contextlib.suppress(Exception):
                    form_block.update()
                with contextlib.suppress(Exception):
                    save_btn.update()
            except Exception:
                pass
            with contextlib.suppress(Exception):
                asyncio.create_task(_load_candidates_async())

        # Refresh on focus/open to avoid stale list after deletes.
        series_f.on_focus = _reload_candidates
        series_f.on_click = _reload_candidates
        supplier_f.on_select = _on_supplier_change
        purchase_price_f.on_change = _refresh_purchase_sum_usd
        detail_toggle.on_change = _on_detail_toggle
        detail_select.on_select = _on_detail_select_change
        search_f.on_change = lambda _: _apply_local_filter()
        save_btn = ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save, disabled=True)

        async def _load_candidates_async():
            try:
                candidates = self._pending_series_candidates(company_id, region_id)
                if not self.content_area.page:
                    return
                if not candidates:
                    loading_ring.visible = False
                    loading_text.value = self.t("products.no_series_to_complete", "Немає серій для доробки у товар")
                    with contextlib.suppress(Exception):
                        loading_ring.update()
                    with contextlib.suppress(Exception):
                        loading_text.update()
                    return

                # Series that already have at least one deeper detail product should stay selectable,
                # but be moved to the end of the options list.
                company_nodes = self.db.get_company_catalog_nodes(company_id) or []
                node_by_id = {
                    n.get("id"): n
                    for n in company_nodes
                    if isinstance(n, dict) and n.get("id")
                }
                candidate_series_ids = {
                    c.get("catalog_node_id")
                    for c in candidates
                    if isinstance(c, dict) and c.get("catalog_node_id")
                }
                series_with_detail_ids = set()

                # Use all products in scope, not just current manager, so candidates disappear immediately
                # when a series is completed by anyone (or created earlier under another manager).
                products_in_scope = (
                    self.db.get_products(
                        company_id, region_id, manager_id=self.manager_id
                    )
                    or []
                )
                for prod in products_in_scope:
                    if not isinstance(prod, dict):
                        continue
                    node_id = (prod.get("catalog_node_id") or "").strip()
                    if not node_id:
                        continue

                    # If a product is created directly on the series node (no detailization),
                    # the series is already completed and must not appear in the candidates list.
                    if node_id in candidate_series_ids:
                        series_with_detail_ids.add(node_id)
                        continue

                    cur = node_id
                    hops = 0
                    # Walk up ancestors; if any ancestor is a candidate series (and not the same node),
                    # then this series already has a deeper detail product.
                    while cur and hops < 30:
                        if cur in candidate_series_ids and cur != node_id:
                            series_with_detail_ids.add(cur)
                            break
                        cur = (node_by_id.get(cur) or {}).get("parent_id")
                        hops += 1

                for row in candidates:
                    if isinstance(row, dict):
                        row["_has_completion_detail"] = (
                            row.get("catalog_node_id") in series_with_detail_ids
                        )

                # Remove series which already have a product (either directly or via a deeper detail node).
                candidates = [
                    c
                    for c in candidates
                    if isinstance(c, dict)
                    and (c.get("catalog_node_id") not in series_with_detail_ids)
                ]
                all_candidates_ref["items"] = candidates
                _apply_local_filter()
                loading_ring.visible = False
                loading_text.visible = False
                form_block.visible = True
                save_btn.disabled = False
                with contextlib.suppress(Exception):
                    loading_ring.update()
                with contextlib.suppress(Exception):
                    loading_text.update()
                with contextlib.suppress(Exception):
                    form_block.update()
                with contextlib.suppress(Exception):
                    save_btn.update()
            except Exception:
                logger.exception("Load series completion candidates failed")
                loading_ring.visible = False
                loading_text.value = "Не вдалося завантажити серії"
                with contextlib.suppress(Exception):
                    loading_ring.update()
                with contextlib.suppress(Exception):
                    loading_text.update()

        def _series_help(_):
            from views.help_utils import show_modal_help
            _pg = self._page()
            show_modal_help(_pg, self.t("products.dlg.complete_series", "Доробити серію в товар"), [
                self.t("products.series_help.desc", "Конвертує незавершену серію в повноцінний товар каталогу."),
                self.t("products.series_help.step1", "1. Знайдіть серію у пошуковому полі або виберіть зі списку."),
                self.t("products.series_help.step2", "2. Заповніть назву товару, постачальника, пакування, ціну."),
                self.t("products.series_help.step3", "3. Додайте фото (до 3-х штук) якщо потрібно."),
                self.t("products.series_help.step4", "4. Натисніть «Зберегти» — серія стане повноцінним товаром."),
            ])

        dlg = ft.AlertDialog(
            title=ft.Row(
                [
                    ft.Text(self.t("products.dlg.complete_series", "Доробити серію в товар"), expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_series_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Container(
                width=760,
                height=self._dialog_content_height(640),
                content=ft.Column(
                    [
                        ft.Row([loading_ring, loading_text], spacing=10),
                        search_f,
                        form_block,
                    ],
                    spacing=10,
                    scroll=ft.ScrollMode.ADAPTIVE,
                ),
            ),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_cancel),
                save_btn,
            ],
        )
        self.content_area.page.show_dialog(dlg)
        try:
            asyncio.create_task(_load_candidates_async())
        except Exception:
            logger.exception("Start series candidates loader failed")

    def _series_rows_for_category(self, category_id):
        if not category_id:
            return []
        buckets = self.db.get_all_series_for_categories([category_id]) or {}
        rows = []
        for key, bucket in buckets.items():
            if isinstance(key, tuple) and key and key[0] == category_id:
                rows.extend(bucket or [])
        return self._dedupe_rows_by_id(rows)

    def _brand_rows_for_category(self, category_id):
        if not category_id:
            return []
        buckets = self.db.get_all_brands_for_categories([category_id]) or {}
        rows = []
        for key, bucket in buckets.items():
            if isinstance(key, tuple) and key and key[0] == category_id:
                rows.extend(bucket or [])
        return self._dedupe_rows_by_id(rows)

    def _to_float_or_none(self, value):
        return safe_float(value, None)

    def _fmt(self, value):
        parsed = self._to_float_or_none(value)
        if parsed is None:
            return ""
        return format_grouped(parsed)

    def _clear_field_errors(self, *controls):
        for control in controls:
            if control is not None and hasattr(control, "error_text"):
                control.error_text = None

    def _mark_required(self, control, message="Поле обов'язкове"):
        if control is not None and hasattr(control, "error_text"):
            control.error_text = message

    def _convert_to_usd(self, amount, currency):
        val = self._to_float_or_none(amount)
        if val is None:
            return None
        rate = self.FX_TO_USD.get((currency or "").upper())
        if not rate:
            return None
        return val * rate

    def _convert_from_usd(self, amount_usd, currency):
        val = self._to_float_or_none(amount_usd)
        if val is None:
            return None
        rate = self.FX_TO_USD.get((currency or "").upper())
        if not rate:
            return None
        return val / rate

    def _show_snack(self, text, color):
        page = self.content_area.page
        if not page:
            return
        page.snack_bar = ft.SnackBar(
            ft.Text(text, color=ft.Colors.WHITE, weight="bold"),
            bgcolor=color,
            duration=3000,
        )

    def _show_help(self) -> None:
        from views.help_utils import show_modal_help
        page = self._page()
        if not page:
            return
        t = self.t
        show_modal_help(page, t("products.help.title", "Довідка — Товари"), [
            t("products.help.desc",
              "Каталог товарів: назва, постачальник, пакування, ціна закупівлі, вага, фото. Фільтр за категорією, брендом, серією."),
            (t("products.help.steps_title", "Як користуватись"), None),
            t("products.help.step1", "1. Фільтруйте список за категорією, брендом або серією за допомогою випадаючих списків."),
            t("products.help.step2", "2. Клацніть на картку товару → відкриється перегляд з можливістю редагування."),
            t("products.help.step3", "3. «Додати товар» — створити нову позицію каталогу вручну."),
            t("products.help.step4", "4. «Доробити серію» — конвертувати незавершену серію в повноцінний товар."),
            t("products.help.step5", "5. «Товар по шаблону» — масовий імпорт товарів через Excel-файл."),
            t("products.help.step6", "6. У картці товару: вкладки Фото / Сумісності / Розміри для відповідних даних."),
        ])

    def build(self):
        _th = _th_from_view(self)
        global _BORDER, _ALT, _ALT2, _ACCENT, _MUTED, _PANEL
        _BORDER = _th["border"]; _ALT = _ALT2 = _th["alt"]
        _ACCENT = _th["icon_color"]; _MUTED = _th["muted"]
        _PANEL = _th["panel"]
        if not self.content_area.page:
            return
        if self._general_mode and self._general_manager_ids:
            from views.general_wrapper import build_general_sections
            _rid = self._effective_region_id()
            _cid, _db = self.company_id, self.db
            def _count_prods(mid, _db=_db, _cid=_cid, _rid=_rid):
                return len(_db.get_products(_cid, _rid, manager_id=mid) or [])
            build_general_sections(
                self.content_area, self.user_data, self.db, self.__class__,
                view_extra_kwargs={"current_region_name": self.current_region_name},
                count_getter=_count_prods,
                count_label="Товарів:",
            )
            return

        if self.current_region_name in {None, "", self.ALL_REGIONS_KEY, "Всі регіони"}:
            from views.general_wrapper import build_region_sections
            _cid2, _db2, _mid2 = self.company_id, self.db, self.manager_id
            _all2 = _db2.get_products(_cid2, region_id=None, manager_id=_mid2) or []
            _rcounts: dict[str, int] = {}
            for _p2 in _all2:
                if not isinstance(_p2, dict): continue
                _r2 = str(_p2.get("region_id") or "").strip()
                if _r2: _rcounts[_r2] = _rcounts.get(_r2, 0) + 1
            def _count_rp(rid, _c=_rcounts): return _c.get(rid, 0)
            build_region_sections(
                self.content_area, self.user_data, self.db, self.__class__,
                view_extra_kwargs={}, count_getter=_count_rp, region_kwarg_name="current_region_name",
                count_label="Товарів:",
            )
            return

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

        # Region access and category access are separate grants -- a manager
        # can see a region without owning any category node in it. Creating
        # a product always requires a category, so hide the create-family
        # buttons (leaving read-only browsing/search intact) when there's
        # nowhere to actually put a new one in the current region.
        try:
            _can_create_here = self.db.manager_has_category_access_in_region(
                self.manager_id, self.company_id, self._effective_region_id()
            )
        except Exception:
            _can_create_here = True
        # Cached on the instance so _create_product_card() (called once per
        # visible row) doesn't re-run this DB check for every single product.
        self._region_create_allowed = _can_create_here

        header = ft.Column(
            [
                ft.Row(
                    [
                        ft.Text(self.t("products.title", "Товари"), size=self.HEADER_SIZE, weight="bold"),
                        self._header_loading_indicator,
                        ft.IconButton(
                            icon=ft.Icons.HELP_OUTLINE,
                            icon_size=18,
                            tooltip=self.t("common.help.tooltip", "Довідка"),
                            on_click=lambda _e: self._show_help(),
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=8,
                ),
                ft.Row(
                    [
                        ft.FilledButton(
                            self.t("products.add_btn", "Додати товар"),
                            icon=ft.Icons.ADD,
                            height=30,
                            style=ft.ButtonStyle(
                                text_style=ft.TextStyle(size=11),
                                padding=ft.padding.symmetric(horizontal=8, vertical=2),
                            ),
                            on_click=lambda _: self._page()
                            and self._page().run_task(self.show_product_dialog),
                            visible=not self.read_only_subordinate,
                        ),
                        ft.OutlinedButton(
                            self.t("products.complete_series_btn", "Доробити серію"),
                            icon=ft.Icons.AUTO_FIX_HIGH,
                            height=30,
                            style=ft.ButtonStyle(
                                text_style=ft.TextStyle(size=11),
                                padding=ft.padding.symmetric(horizontal=8, vertical=2),
                            ),
                            on_click=lambda _: self.show_series_completion_dialog(),
                            visible=not self.read_only_subordinate,
                        ),
                        ft.OutlinedButton(
                            self.t("nav.bulk_template", "Товар по шаблону"),
                            icon=ft.Icons.TABLE_CHART,
                            height=30,
                            style=ft.ButtonStyle(
                                text_style=ft.TextStyle(size=11),
                                padding=ft.padding.symmetric(horizontal=8, vertical=2),
                            ),
                            on_click=lambda _: self._open_bulk_template(),
                            visible=not self.read_only_subordinate,
                        ),
                    ],
                    spacing=6,
                    wrap=True,
                    run_spacing=4,
                    visible=not self.read_only_subordinate and _can_create_here,
                ),
            ],
            spacing=6,
            tight=True,
        )

        self.search_f = ft.TextField(
            value=self._search_query,
            hint_text=self.t("products.search_hint", "Пошук товарів"),
            border_radius=10,
            prefix_icon=ft.Icons.SEARCH,
            filled=True,
            # A fixed 420px field sits on its own line with nothing to
            # constrain it — on a ~360-390px-wide phone that overflows the
            # screen horizontally. Let it size to the available width on
            # mobile instead; desktop keeps the original fixed width.
            width=None if is_mobile(self.content_area.page) else 420,
            height=36,
            content_padding=ft.padding.symmetric(horizontal=10, vertical=6),
            text_size=12,
            on_change=lambda _: self._debounce_call(
                "products_screen_search", self._on_search_change
            ),
        )

        # Category/subcategory/brand/series filter chips: wrap=True (desktop)
        # let a long chip list quietly push the products table further down
        # the screen the more filters exist -- on a short phone screen this
        # was one of the biggest space-eaters. Single scrollable line on
        # mobile instead, same pattern as the workspace tab bar fix.
        _prod_is_mob = is_mobile(self.content_area.page)
        if _prod_is_mob:
            self._category_filter_host = make_scrollable_chip_row([], spacing=6)
            self._subcategory_filter_host = make_scrollable_chip_row([], spacing=4)
            self._brand_filter_host = make_scrollable_chip_row([], spacing=4)
            self._series_filter_host = make_scrollable_chip_row([], spacing=4)
        else:
            self._category_filter_host = ft.Row(spacing=6, wrap=True)
            self._subcategory_filter_host = ft.Row(spacing=4, wrap=True)
            self._brand_filter_host = ft.Row(spacing=4, wrap=True)
            self._series_filter_host = ft.Row(spacing=4, wrap=True)
        self._advanced_filters_toggle_btn = ft.OutlinedButton(
            "Фільтри +",
            height=24,
            style=ft.ButtonStyle(
                padding=ft.padding.symmetric(horizontal=6, vertical=0),
                text_style=ft.TextStyle(size=10, weight="w500"),
            ),
            on_click=lambda _: self._toggle_advanced_filters(),
        )

        self._subcategory_filter_row = ft.Row(
            [
                ft.Text("Підкатегорії:", size=10, color="grey700", weight="w500"),
                self._subcategory_filter_host,
            ],
            spacing=6,
            wrap=not _prod_is_mob,
            visible=False,
        )
        self._brand_filter_row = ft.Row(
            [
                ft.Text("Бренди:", size=10, color="grey700", weight="w500"),
                self._brand_filter_host,
            ],
            spacing=6,
            wrap=not _prod_is_mob,
            visible=False,
        )
        self._series_filter_row = ft.Row(
            [
                ft.Text("Серії:", size=10, color="grey700", weight="w500"),
                self._series_filter_host,
            ],
            spacing=6,
            wrap=not _prod_is_mob,
            visible=False,
        )
        self._sort_order_host = (
            make_scrollable_chip_row([], spacing=6)
            if _prod_is_mob
            else ft.Row(spacing=6, wrap=True)
        )
        self._refresh_sort_order_controls()

        filters_block = ft.Column(
            [
                ft.Row(
                    [
                        ft.Text("Категорії:", size=11, color="grey700", weight="w500"),
                        self._advanced_filters_toggle_btn,
                    ],
                    spacing=6,
                    wrap=True,
                ),
                self._category_filter_host,
                self._subcategory_filter_row,
                self._brand_filter_row,
                self._series_filter_row,
            ],
            spacing=4,
        )

        # Outer wrapper must NOT scroll — ListView handles its own lazy scroll.
        try:
            self.content_area.scroll = None
            self.content_area.auto_scroll = False
        except Exception:
            pass

        # ListView renders only visible items → smooth scrolling regardless of count.
        self.products_list = ft.ListView(
            expand=True,
            spacing=2,
            padding=ft.padding.only(bottom=8),
        )
        self.products_meta = ft.Text("", size=self.META_TEXT_SIZE, color="grey700")
        self.loading_box = ft.Container(
            padding=14,
            visible=True,
            content=ft.Column(
                [
                    ft.Text(self.t("products.loading", "Завантаження товарів..."), color="grey700"),
                    ft.ProgressRing(width=22, height=22, stroke_width=3),
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        self._fixed_top = ft.Column(
            [
                header,
                self.search_f,
                self._sort_order_host,
                filters_block,
                ft.Text(
                    self.t("packaging.superadmin_mode_hint", "Режим: Всі компанії/регіони. Натискайте Редагувати для модалки."),
                    size=self.META_TEXT_SIZE,
                    color="grey700",
                    visible=self.is_superadmin and not self.company_id,
                ),
                ft.Text(
                    self.t("packaging.readonly_mode_hint", "Режим перегляду підлеглого: створення і редагування вимкнено"),
                    size=self.META_TEXT_SIZE,
                    color="grey700",
                    visible=self.read_only_subordinate,
                ),
                self.products_meta,
                ft.Divider(height=6, color="transparent"),
                self.loading_box,
            ],
            spacing=4,
            tight=True,
        )

        self.content_area.controls.extend([self._fixed_top, self.products_list])
        self.content_area.update()
        self.content_area.page.run_task(self._async_initial_load)

    async def _async_initial_load(self):
        if self._general_mode and self._general_manager_ids:
            return
        if not hasattr(self, "products_list"):
            return
        await asyncio.sleep(0.01)
        self.load_products()
        if getattr(self, "loading_box", None) is not None:
            self.loading_box.visible = False
            with contextlib.suppress(Exception):
                self.loading_box.update()
        if self._header_loading_indicator is not None:
            self._header_loading_indicator.visible = False
            with contextlib.suppress(Exception):
                self._header_loading_indicator.update()
        if not self._products_ui_ready:
            self._products_ui_ready = True
            with contextlib.suppress(Exception):
                self._products_snapshot_pulse_ts = self._products_scope_pulse_ts()
            self._sync_products_to_shared_map()
            self._start_bg_products_sync()

    def _effective_region_id(self):
        if len(self.regions_data) == 1:
            return self.regions_data[0].get("id")
        if (
            self.current_region_name
            and self.current_region_name != self.ALL_REGIONS_KEY
        ):
            resolved = self._region_id_by_name(self.current_region_name)
            if resolved:
                return resolved
            # Refresh regions and retry to avoid stale cache after company/region switch.
            self.regions_data = [
                r
                for r in self.db.get_company_regions(self.company_id)
                if isinstance(r, dict)
            ]
            return self._region_id_by_name(self.current_region_name)
        return None

    def _on_search_change(self):
        self.load_products()
        self._emit_state_changed()

    def _toggle_advanced_filters(self):
        self._advanced_filters_expanded = not bool(self._advanced_filters_expanded)
        self.load_products()
        self._emit_state_changed()

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

    def load_products(self, force_reload: bool = False):
        self.products_list.controls.clear()
        self.products_meta.value = ""
        self._refresh_sort_order_controls()
        try:
            region_id = self._effective_region_id()
            viewer_manager_id = self.manager_id

            if force_reload or not self._products_cache_loaded:
                real_rows = (
                    self.db.get_products(
                        self.company_id,
                        region_id,
                        manager_id=viewer_manager_id,
                    )
                    or []
                )
                self._set_loading_text(f"Товари: {len(real_rows)}")
                virtual_rows = []
                if bool(getattr(Config, "PRODUCTS_ENABLE_VIRTUAL_ROWS", False)):
                    virtual_rows = (
                        self._build_virtual_series_products(region_id, real_rows) or []
                    )
                rows = real_rows + virtual_rows
                self._manager_names_by_company_cache = {}
                self._product_path_cache = {}

                # Rebuild per-screen memoized data to keep UI responsive on large product lists.
                self._region_price_tiers_cache = {}
                self._region_delivery_methods_cache = {}

                # Enrich with nested `supplier`/`packaging`/`category` dicts over the
                # FULL loaded list (not just filtered `items`) — moved ahead of the
                # disambiguation pass below so category_of/supplier_of can see them.
                supplier_by_id = {}
                packaging_by_id = {}
                category_by_id = {}
                with contextlib.suppress(Exception):
                    supplier_rows = (
                        self.db.get_suppliers(
                            self.company_id, region_id, manager_id=self.manager_id
                        )
                        or []
                    )
                    supplier_by_id = {
                        s.get("id"): s
                        for s in supplier_rows
                        if isinstance(s, dict) and s.get("id")
                    }
                    self._set_loading_text(f"Постачальники: {len(supplier_rows)}")
                with contextlib.suppress(Exception):
                    packaging_rows = (
                        self.db.get_packaging(
                            self.company_id, region_id, manager_id=self.manager_id
                        )
                        or []
                    )
                    packaging_by_id = {
                        p.get("id"): p
                        for p in packaging_rows
                        if isinstance(p, dict) and p.get("id")
                    }
                    self._set_loading_text(f"Пакування: {len(packaging_rows)}")
                with contextlib.suppress(Exception):
                    category_rows = (
                        self.db.get_company_categories(
                            self.company_id,
                            region_id,
                            manager_id=self.manager_id,
                        )
                        or []
                    )
                    category_by_id = {
                        c.get("id"): c
                        for c in category_rows
                        if isinstance(c, dict) and c.get("id")
                    }
                    self._set_loading_text(f"Категорії: {len(category_rows)}")
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    sid = str(row.get("supplier_id") or "").strip()
                    pid = str(row.get("packaging_id") or "").strip()
                    cid = str(row.get("category_id") or "").strip()
                    if sid:
                        row["supplier"] = supplier_by_id.get(sid) or {}
                    if pid:
                        row["packaging"] = packaging_by_id.get(pid) or {}
                    if cid:
                        row["category"] = category_by_id.get(cid) or {}

                self._products_cache = {
                    str(r.get("id")): r
                    for r in rows
                    if isinstance(r, dict) and r.get("id")
                }
                # Kept around so a single-row optimistic patch after a save can
                # re-attach nested supplier/packaging/category dicts without a
                # fresh DB round-trip (see _apply_saved_product_to_cache).
                self._suppliers_by_id = supplier_by_id
                self._packaging_by_id = packaging_by_id
                self._categories_by_id = category_by_id
                self._archived_products_cache = (
                    self.db.get_products(
                        self.company_id,
                        region_id,
                        manager_id=viewer_manager_id,
                        archived=True,
                    )
                    or []
                )
                self._products_cache_loaded = True
            else:
                # Cache already warm — reuse it, no DB round-trips for a plain
                # filter/search/sort change (this is the whole point: 500-1000
                # rows shouldn't mean 4 fresh queries per keystroke).
                real_rows = [
                    r for r in self._products_cache.values() if not r.get("_virtual")
                ]
                virtual_rows = [
                    r for r in self._products_cache.values() if r.get("_virtual")
                ]
                rows = list(self._products_cache.values())

            # Resolve base names + cross-product disambiguation suffixes on
            # EVERY render (not just a full DB reload) — this is cheap (pure
            # in-memory grouping; node-path lookups reuse the warm
            # _product_path_cache, no DB calls) and must run every time so a
            # just-edited name that now collides (or stops colliding) with
            # another cached product gets its " | Кат: X" / " | Постач: Y"
            # suffix added or removed immediately, without a full reload.
            self._recompute_display_names(rows)

            query = (self.search_f.value or "").strip().lower()

            items = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                display_name = row.get("_display_name") or ""
                if query and query not in display_name.lower():
                    continue
                # Apply quick filters
                if self.quick_filter == "incomplete_products":
                    # Check if value is present and meaningful (not None, not empty)
                    def is_complete(value):
                        if value is None:
                            return False
                        if isinstance(value, str):
                            return bool(value.strip())
                        if isinstance(value, (dict, list)):
                            return len(value) > 0
                        if isinstance(value, (int, float)):
                            return value != 0
                        return bool(value)

                    has_purchase_price = is_complete(row.get("purchase_price"))
                    has_weight = is_complete(row.get("weight_kg"))
                    has_production_term = is_complete(row.get("production_term_days"))
                    has_sales_prices = is_complete(row.get("sales_prices"))

                    # Include only if MISSING at least one required field
                    is_incomplete = not (
                        has_purchase_price
                        and has_weight
                        and has_production_term
                        and has_sales_prices
                    )
                    if not is_incomplete:
                        continue  # Skip complete products

                items.append(row)

            # `supplier`/`packaging`/`category` nested dicts were already
            # attached to these same row objects above (over the full `rows`
            # list, before disambiguation ran) — nothing left to do here.

            region_key = region_id or ""
            catalog_config = self._catalog_config_by_region.get(region_key)
            if catalog_config is None:
                catalog_config = (
                    self.db.get_company_catalog_config(self.company_id, region_id)
                    if self.company_id
                    else {}
                )
                self._catalog_config_by_region[region_key] = catalog_config

            levels = (
                (catalog_config or {}).get("levels")
                if isinstance(catalog_config, dict)
                else []
            )
            sub_positions = set()
            brand_positions = set()
            series_positions = set()
            for lvl in levels or []:
                if not isinstance(lvl, dict):
                    continue
                if not bool(lvl.get("enabled", True)):
                    continue
                pos = int(lvl.get("position") or 0)
                if pos <= 0:
                    continue
                title_norm = self._normalize_level_title(lvl.get("title"))
                if title_norm == "підкатегорія":
                    sub_positions.add(pos)
                elif title_norm in {"серія", "серия", "series"}:
                    series_positions.add(pos)
                elif title_norm == "бренд":
                    brand_positions.add(pos)

            category_map = {}
            for row in items:
                cid = (row.get("category_id") or "").strip()
                if not cid:
                    continue
                cname = ""
                if isinstance(row.get("category"), dict):
                    cname = (row.get("category") or {}).get("name") or ""
                if not cname:
                    cname = row.get("category_name") or ""
                category_map[cid] = cname or category_map.get(cid) or "Без назви"

            self._selected_category_filter_ids = {
                cid for cid in self._selected_category_filter_ids if cid in category_map
            }

            filtered = []
            for row in items:
                row_cid = (row.get("category_id") or "").strip()
                if (
                    self._selected_category_filter_ids
                    and row_cid not in self._selected_category_filter_ids
                ):
                    continue
                filtered.append(row)

            def _path_rows(row):
                node_id = (row.get("catalog_node_id") or "").strip()
                if not node_id:
                    return []
                path = self._product_path_cache.get(node_id)
                if path is None:
                    path = self.db.get_node_path(node_id) or []
                    self._product_path_cache[node_id] = path
                return path

            def _ids_for_positions(row, positions):
                if not positions:
                    return set()
                ids = set()
                for p in _path_rows(row):
                    if not isinstance(p, dict):
                        continue
                    try:
                        pos = int(p.get("position") or 0)
                    except Exception:
                        pos = 0
                    pid = (p.get("id") or "").strip()
                    if pid and pos in positions:
                        ids.add(pid)
                return ids

            subcategory_map = {}
            brand_map = {}
            series_map = {}
            # Fallback for "Всі регіони" or inconsistent config:
            # if level titles are not set, but path positions exist, still expose filters.
            try:
                present_positions = set()
                for row in filtered:
                    for p in _path_rows(row):
                        if not isinstance(p, dict):
                            continue
                        try:
                            present_positions.add(int(p.get("position") or 0))
                        except Exception:
                            continue
                if not sub_positions and 1 in present_positions:
                    sub_positions = {1}
                if not brand_positions and 2 in present_positions:
                    brand_positions = {2}
                if not series_positions and 3 in present_positions:
                    series_positions = {3}
            except Exception:
                pass
            # Each level's chip *options* must come from a set of rows already
            # narrowed by the levels ABOVE it (but not by itself, so the user
            # can still change their mind on that level) — otherwise choosing
            # a brand never trims the series list, since it would still be
            # built from every row in the category regardless of brand.
            _rows_for_subcategory_map = filtered
            _rows_for_brand_map = (
                [
                    row
                    for row in filtered
                    if _ids_for_positions(row, sub_positions)
                    & self._selected_subcategory_filter_ids
                ]
                if self._selected_subcategory_filter_ids
                else filtered
            )
            _rows_for_series_map = (
                [
                    row
                    for row in _rows_for_brand_map
                    if _ids_for_positions(row, brand_positions)
                    & self._selected_brand_filter_ids
                ]
                if self._selected_brand_filter_ids
                else _rows_for_brand_map
            )

            def _collect_level_map(rows, positions, target_map):
                for row in rows:
                    for p in _path_rows(row):
                        if not isinstance(p, dict):
                            continue
                        pid = (p.get("id") or "").strip()
                        if not pid:
                            continue
                        try:
                            pos = int(p.get("position") or 0)
                        except Exception:
                            pos = 0
                        if pos in positions:
                            target_map[pid] = (p.get("name") or "").strip() or f"Рівень {pos}"

            _collect_level_map(_rows_for_subcategory_map, sub_positions, subcategory_map)
            _collect_level_map(_rows_for_brand_map, brand_positions, brand_map)
            _collect_level_map(_rows_for_series_map, series_positions, series_map)

            self._selected_subcategory_filter_ids = {
                sid
                for sid in self._selected_subcategory_filter_ids
                if sid in subcategory_map
            }
            self._selected_brand_filter_ids = {
                bid for bid in self._selected_brand_filter_ids if bid in brand_map
            }
            self._selected_series_filter_ids = {
                sid for sid in self._selected_series_filter_ids if sid in series_map
            }

            if self._selected_subcategory_filter_ids:
                filtered = [
                    row
                    for row in filtered
                    if _ids_for_positions(row, sub_positions)
                    & self._selected_subcategory_filter_ids
                ]

            if self._selected_brand_filter_ids:
                filtered = [
                    row
                    for row in filtered
                    if _ids_for_positions(row, brand_positions)
                    & self._selected_brand_filter_ids
                ]

            if self._selected_series_filter_ids:
                filtered = [
                    row
                    for row in filtered
                    if _ids_for_positions(row, series_positions)
                    & self._selected_series_filter_ids
                ]

            filtered.sort(
                key=lambda row: (
                    str(
                        row.get("_display_name")
                        or self._resolve_product_display_name(row)
                        or ""
                    ).casefold(),
                    str(row.get("id") or ""),
                ),
                reverse=(self._product_sort_order == "desc"),
            )

            def _group_ids_by_name(id_name_map: dict) -> dict:
                """Same catalog level name can exist as several distinct
                nodes (e.g. a brand re-created under different subcategories)
                — group them under one chip by exact name instead of showing
                one duplicate chip per underlying node id."""
                groups: dict = {}
                for _id, _name in id_name_map.items():
                    _key = str(_name or "").strip()
                    if not _key or not _id:
                        continue
                    groups.setdefault(_key, set()).add(_id)
                return groups

            def _multi_btn_group(text, ids, selected_ids, on_click):
                ids = {str(x).strip() for x in (ids or []) if str(x).strip()}
                selected = bool(ids & selected_ids)

                def _make_style(is_sel):
                    return ft.ButtonStyle(
                        padding=ft.padding.symmetric(horizontal=7, vertical=1),
                        text_style=ft.TextStyle(size=10, weight="w500"),
                        bgcolor=(_BORDER if is_sel else None),
                    )

                btn = ft.OutlinedButton(text, height=26, style=_make_style(selected))

                def _on_click(_, _ids=ids, b=btn, _sids=selected_ids):
                    will_select = not bool(_ids & _sids)
                    b.style = _make_style(will_select)
                    import contextlib as _cl
                    with _cl.suppress(Exception):
                        b.update()
                    on_click(_ids)

                btn.on_click = _on_click
                return btn

            def _multi_btn(text, key, selected_ids, on_click):
                selected = key in selected_ids

                def _make_style(is_sel):
                    return ft.ButtonStyle(
                        padding=ft.padding.symmetric(horizontal=7, vertical=1),
                        text_style=ft.TextStyle(size=10, weight="w500"),
                        bgcolor=(_BORDER if is_sel else None),
                    )

                btn = ft.OutlinedButton(text, height=26, style=_make_style(selected))

                def _on_click(_, k=key, b=btn, _sids=selected_ids):
                    will_select = k not in _sids
                    b.style = _make_style(will_select)
                    import contextlib as _cl
                    with _cl.suppress(Exception):
                        b.update()
                    on_click(k)

                btn.on_click = _on_click
                return btn

            def _clear_btn(on_clear, selected):
                def _make_style(is_sel):
                    return ft.ButtonStyle(
                        padding=ft.padding.symmetric(horizontal=7, vertical=1),
                        text_style=ft.TextStyle(size=10, weight="w500"),
                        bgcolor=(_BORDER if is_sel else None),
                    )

                btn = ft.OutlinedButton("Всі", height=26, style=_make_style(selected))

                def _on_clear_click(_, b=btn):
                    b.style = _make_style(True)
                    import contextlib as _cl
                    with _cl.suppress(Exception):
                        b.update()
                    on_clear()

                btn.on_click = _on_clear_click
                return btn

            def _toggle_category(category_id):
                key = str(category_id or "").strip()
                if not key:
                    return
                if key in self._selected_category_filter_ids:
                    self._selected_category_filter_ids.discard(key)
                else:
                    self._selected_category_filter_ids.add(key)
                self.load_products()
                self._emit_state_changed()

            def _clear_categories():
                self._selected_category_filter_ids.clear()
                self.load_products()
                self._emit_state_changed()

            def _toggle_subcategory_group(ids):
                ids = {str(x).strip() for x in (ids or []) if str(x).strip()}
                if not ids:
                    return
                if ids & self._selected_subcategory_filter_ids:
                    self._selected_subcategory_filter_ids -= ids
                else:
                    self._selected_subcategory_filter_ids |= ids
                self.load_products()
                self._emit_state_changed()

            def _clear_subcategories():
                self._selected_subcategory_filter_ids.clear()
                self.load_products()
                self._emit_state_changed()

            def _toggle_brand_group(ids):
                ids = {str(x).strip() for x in (ids or []) if str(x).strip()}
                if not ids:
                    return
                if ids & self._selected_brand_filter_ids:
                    self._selected_brand_filter_ids -= ids
                else:
                    self._selected_brand_filter_ids |= ids
                self.load_products()
                self._emit_state_changed()

            def _clear_brands():
                self._selected_brand_filter_ids.clear()
                self.load_products()
                self._emit_state_changed()

            def _toggle_series_group(ids):
                ids = {str(x).strip() for x in (ids or []) if str(x).strip()}
                if not ids:
                    return
                if ids & self._selected_series_filter_ids:
                    self._selected_series_filter_ids -= ids
                else:
                    self._selected_series_filter_ids |= ids
                self.load_products()
                self._emit_state_changed()

            def _clear_series():
                self._selected_series_filter_ids.clear()
                self.load_products()
                self._emit_state_changed()

            if self._category_filter_host is not None:
                cat_controls = [
                    _clear_btn(
                        _clear_categories, not self._selected_category_filter_ids
                    )
                ]
                for cid, cname in sorted(
                    category_map.items(), key=lambda x: (x[1] or "").lower()
                ):
                    cat_controls.append(
                        _multi_btn(
                            cname,
                            cid,
                            self._selected_category_filter_ids,
                            _toggle_category,
                        )
                    )
                self._category_filter_host.controls = cat_controls

            show_subcategory_filter = bool(sub_positions)
            _has_adv = bool(sub_positions or brand_positions or series_positions)
            if self._advanced_filters_toggle_btn is not None:
                self._advanced_filters_toggle_btn.text = (
                    "▾ Рівні −" if self._advanced_filters_expanded else "▸ Рівні +"
                )
                self._advanced_filters_toggle_btn.visible = _has_adv

            # Show subcategory/brand/series filters only when expanded.
            show_adv_rows = self._advanced_filters_expanded and _has_adv
            if self._subcategory_filter_row is not None:
                self._subcategory_filter_row.visible = (
                    show_subcategory_filter and show_adv_rows
                )
            if show_subcategory_filter and self._subcategory_filter_host is not None:
                sub_controls = [
                    _clear_btn(
                        _clear_subcategories, not self._selected_subcategory_filter_ids
                    )
                ]
                for sname, sids in sorted(
                    _group_ids_by_name(subcategory_map).items(),
                    key=lambda x: (x[0] or "").lower(),
                ):
                    sub_controls.append(
                        _multi_btn_group(
                            sname,
                            sids,
                            self._selected_subcategory_filter_ids,
                            _toggle_subcategory_group,
                        )
                    )
                self._subcategory_filter_host.controls = sub_controls
            elif self._subcategory_filter_host is not None:
                self._subcategory_filter_host.controls = []

            show_brand_filter = bool(brand_positions)
            if self._brand_filter_row is not None:
                self._brand_filter_row.visible = show_brand_filter and show_adv_rows
            if show_brand_filter and self._brand_filter_host is not None:
                brand_controls = [
                    _clear_btn(_clear_brands, not self._selected_brand_filter_ids)
                ]
                for bname, bids in sorted(
                    _group_ids_by_name(brand_map).items(),
                    key=lambda x: (x[0] or "").lower(),
                ):
                    brand_controls.append(
                        _multi_btn_group(
                            bname,
                            bids,
                            self._selected_brand_filter_ids,
                            _toggle_brand_group,
                        )
                    )
                self._brand_filter_host.controls = brand_controls
            elif self._brand_filter_host is not None:
                self._brand_filter_host.controls = []

            show_series_filter = bool(series_positions)
            if self._series_filter_row is not None:
                self._series_filter_row.visible = show_series_filter and show_adv_rows
            if show_series_filter and self._series_filter_host is not None:
                series_controls = [
                    _clear_btn(_clear_series, not self._selected_series_filter_ids)
                ]
                for sname, sids in sorted(
                    _group_ids_by_name(series_map).items(),
                    key=lambda x: (x[0] or "").lower(),
                ):
                    series_controls.append(
                        _multi_btn_group(
                            sname,
                            sids,
                            self._selected_series_filter_ids,
                            _toggle_series_group,
                        )
                    )
                self._series_filter_host.controls = series_controls
            elif self._series_filter_host is not None:
                self._series_filter_host.controls = []

            self.products_meta.value = f"Знайдено: {len(filtered)} | Реальні: {len(real_rows)} | Серія як товар: {len(virtual_rows)}"

            if not filtered:
                self.products_list.controls.append(
                    ft.Container(
                        padding=ft.padding.only(left=4, top=6, bottom=6),
                        content=ft.Text(
                            self.t("products.empty_state", "Немає товарів у вибраному контексті"),
                            color="grey600",
                            size=self.EMPTY_TEXT_SIZE,
                            text_align=ft.TextAlign.LEFT,
                        ),
                    )
                )
            else:
                for row in filtered:
                    row_id = row.get("id") if isinstance(row, dict) else None
                    row_name = row.get("_display_name") if isinstance(row, dict) else ""
                    try:
                        ctrl = self._create_product_card(row)
                        self.products_list.controls.append(ctrl)
                    except Exception:
                        logger.exception(
                            "Products render failed | id=%r name=%r", row_id, row_name
                        )
                        self.products_list.controls.append(
                            ft.Text(
                                f"Помилка рендера товару: {row_name or row_id or 'без назви'}",
                                color="red600",
                                size=12,
                            )
                        )

            with contextlib.suppress(Exception):
                self.products_list.controls.append(
                    self._build_archive_section(region_id, viewer_manager_id)
                )

        except Exception:
            logger.exception("Load products failed")
            self.products_meta.value = "Помилка завантаження товарів"
            self.products_list.controls.append(
                ft.Container(
                    padding=ft.padding.only(left=4, top=6, bottom=6),
                    content=ft.Text(
                        "Не вдалося завантажити товари",
                        color="red600",
                        size=self.EMPTY_TEXT_SIZE,
                        text_align=ft.TextAlign.LEFT,
                    ),
                )
            )

        _pg = self._page()
        if getattr(self, "_fixed_top", None):
            safe_update_with_retry(self._fixed_top, _pg)
        safe_update_with_retry(self.products_list, _pg)

    def _build_archive_section(self, region_id, manager_id):
        """Згорнута секція архівованих товарів з можливістю відновлення."""
        archived_rows = self._archived_products_cache or []
        collapsed = getattr(self, "_products_archive_collapsed", True)

        def _toggle(_):
            self._products_archive_collapsed = not getattr(
                self, "_products_archive_collapsed", True
            )
            self.load_products()

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
                        self.t("products.archive_section", "Архів") + f" ({len(archived_rows)})",
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
                            self.t("products.archive_empty", "Архів порожній"),
                            color="grey600",
                            size=12,
                        ),
                    )
                )
            else:
                for row in archived_rows:
                    with contextlib.suppress(Exception):
                        controls.append(self._create_product_card(row, is_archived=True))
        return ft.Container(
            content=ft.Column(controls, spacing=2),
            margin=ft.margin.only(top=10),
            border=ft.border.only(top=ft.BorderSide(1, "grey300")),
        )

    def _compute_blended_cost_usd(self, product, pivot_ccy: str = "USD") -> float | None:
        pid = str(product.get("id") or "").strip()
        if not pid:
            return None
        company_id = str(product.get("company_id") or self.company_id or "").strip()
        region_id = product.get("region_id")
        manager_id = str(self.manager_id or "").strip()

        unit_cost_stock_usd = None
        try:
            stock_inputs = self.db.get_cost_price_stock_inputs(
                company_id, region_id=region_id, manager_id=manager_id
            )
            inp = (stock_inputs or {}).get(pid) or {}
            qty = self._to_float_or_none(inp.get("qty"))
            amount = self._to_float_or_none(inp.get("amount"))
            ccy = str(inp.get("ccy") or "USD").upper()
            if qty is not None and qty > 0 and amount is not None:
                unit_cost_in_ccy = float(amount) / float(qty)
                unit_cost_stock_usd = fx_rates.convert(unit_cost_in_ccy, ccy, pivot_ccy)
        except Exception:
            unit_cost_stock_usd = None

        avg_order_cost_usd = None
        try:
            orders = self.db.get_orders(
                company_id,
                region_id=region_id,
                manager_id=manager_id,
                visible_product_ids=[pid],
            )
            order_inputs = self.db.get_cost_price_order_inputs(
                company_id, region_id=region_id, manager_id=manager_id
            )
            # Each sample can be in a different native currency now, so every
            # one is converted into pivot_ccy before being averaged — mixing
            # raw numbers across currencies would be meaningless.
            order_costs = []
            for order in orders or []:
                if not isinstance(order, dict):
                    continue
                status_norm = str(order.get("status") or "").strip().casefold()
                if status_norm in {
                    "accepted", "прийнятий", "прийнято",
                    "production", "виробництво", "вироб-во",
                }:
                    continue
                order_id = str(order.get("id") or "").strip()
                for idx, item in enumerate(order.get("items") or []):
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("product_id") or "").strip() != pid:
                        continue
                    input_key = f"{order_id}:{idx}"
                    order_input = order_inputs.get(input_key) or {}
                    cost = self._to_float_or_none(order_input.get("unit_cost_usd"))
                    cost_ccy = (
                        str(order_input.get("unit_cost_currency") or "").upper().strip()
                    )
                    if cost is None:
                        cost = self._to_float_or_none(item.get("unit_cost_calc_usd"))
                        cost_ccy = (
                            str(item.get("unit_cost_calc_currency") or "").upper().strip()
                        )
                    cost_ccy = cost_ccy or "USD"
                    if cost is not None and float(cost) >= 0:
                        cost_pivot = fx_rates.convert(float(cost), cost_ccy, pivot_ccy)
                        if cost_pivot is not None:
                            order_costs.append(cost_pivot)
            if order_costs:
                avg_order_cost_usd = sum(order_costs) / len(order_costs)
        except Exception:
            avg_order_cost_usd = None

        candidates = [
            c for c in [unit_cost_stock_usd, avg_order_cost_usd] if c is not None
        ]
        return sum(candidates) / len(candidates) if candidates else None

    def _profitability_controls(self, product, enabled_delivery_ids=None):
        pname = product.get("name") or "?"
        region_id = product.get("region_id")
        product_company_id = product.get("company_id") or self.company_id
        logger.debug(
            f"[profitability] product={pname} region_id={region_id} enabled_ids={enabled_delivery_ids}"
        )
        if not region_id:
            logger.debug(f"[profitability] {pname}: no region_id → fallback matrix")

        supplier = (
            product.get("supplier", {})
            if isinstance(product.get("supplier"), dict)
            else {}
        )
        packaging = (
            product.get("packaging", {})
            if isinstance(product.get("packaging"), dict)
            else {}
        )

        purchase_price = self._to_float_or_none(product.get("purchase_price"))
        purchase_ccy = (
            product.get("purchase_currency") or supplier.get("currency") or "USD"
        ).upper()

        price_tiers = (
            self._region_price_tiers_cache.get(region_id)
            if region_id in self._region_price_tiers_cache
            else None
        )
        if price_tiers is None:
            price_tiers = (
                self.db.get_effective_region_price_tiers_for_manager(
                    self.company_id,
                    self.manager_id,
                    region_id,
                )
                if region_id
                else []
            )
            self._region_price_tiers_cache[region_id] = price_tiers

        # Cost-price pivot currency: this product's own currency if purchase
        # price + every sale-tier price share one currency, otherwise USD —
        # re-evaluated fresh every call so a later currency change (e.g. a
        # tier switched to a different currency) is picked up automatically.
        pivot_ccy = fx_rates.resolve_pivot_currency(
            [
                purchase_ccy,
                *(
                    str(t.get("currency") or "USD").upper().strip() or "USD"
                    for t in (price_tiers or [])
                    if isinstance(t, dict) and t.get("enabled", True)
                ),
            ]
        )

        purchase_usd = (
            fx_rates.convert(purchase_price, purchase_ccy, pivot_ccy)
            if purchase_price is not None
            else None
        )

        packaging_price = self._to_float_or_none(packaging.get("price"))
        packaging_ccy = (
            (packaging.get("suppliers") or {}).get("currency")
            if isinstance(packaging.get("suppliers"), dict)
            else None
        ) or purchase_ccy
        packaging_ccy = str(packaging_ccy or "USD").upper()
        packaging_usd = (
            fx_rates.convert(packaging_price, packaging_ccy, pivot_ccy)
            if packaging_price is not None
            else 0.0
        )

        weight = self._to_float_or_none(product.get("weight_kg")) or 0.0

        sales_prices = {}
        raw_sales = product.get("sales_prices_json")
        if isinstance(raw_sales, list):
            for row in raw_sales:
                if isinstance(row, dict) and row.get("tier_id"):
                    sales_prices[row.get("tier_id")] = row
                elif isinstance(row, dict) and row.get("name"):
                    # Backward compatibility for older rows with missing tier_id.
                    sales_prices[f"name:{row.get('name')}"] = row
        elif isinstance(raw_sales, str) and raw_sales.strip():
            try:
                parsed = json.loads(raw_sales)
                if isinstance(parsed, list):
                    for row in parsed:
                        if isinstance(row, dict) and row.get("tier_id"):
                            sales_prices[row.get("tier_id")] = row
                        elif isinstance(row, dict) and row.get("name"):
                            sales_prices[f"name:{row.get('name')}"] = row
            except Exception:
                sales_prices = {}

        delivery_methods = (
            self.db.get_effective_delivery_methods_for_manager(
                product_company_id, self.manager_id, region_id
            )
            if region_id
            else []
        )
        logger.debug(
            f"[profitability] {pname}: raw_delivery_methods={[(d.get('name'), d.get('effective_price_per_kg'), d.get('effective_price_currency')) for d in delivery_methods if isinstance(d,dict)]}"
        )
        if enabled_delivery_ids is not None:
            allowed = set(enabled_delivery_ids)
            delivery_methods = [
                row
                for row in delivery_methods
                if isinstance(row, dict) and row.get("id") in allowed
            ]
        self._region_delivery_methods_cache[region_id] = delivery_methods
        logger.debug(
            f"[profitability] {pname}: after_filter delivery_methods={[(d.get('name'), d.get('effective_price_per_kg')) for d in delivery_methods if isinstance(d,dict)]}"
        )

        delivery_cols = []
        for row in delivery_methods:
            if not isinstance(row, dict):
                continue
            delivery_cols.append(
                {
                    "name": row.get("name") or "Доставка",
                    "price_per_kg": self._to_float_or_none(
                        row.get("effective_price_per_kg")
                    ),
                    "currency": str(
                        row.get("effective_price_currency")
                        or row.get("base_price_currency")
                        or "USD"
                    ).upper(),
                }
            )
        if not delivery_cols:
            delivery_cols = [
                {
                    "name": self.t("products.price_matrix.col.delivery_fallback", "Доставка"),
                    "price_per_kg": None,
                    "currency": "USD",
                }
            ]

        blended_usd = self._compute_blended_cost_usd(product, pivot_ccy)

        headers = [
            self.t("products.price_matrix.col.tier", "Рівень ціни"),
            self.t("products.price_matrix.col.sale_price", "Ціна продажу"),
        ] + [
            c.get("name") or self.t("products.price_matrix.col.delivery_fallback", "Доставка")
            for c in delivery_cols
        ]
        if blended_usd is not None:
            headers.append(self.t("products.price_matrix.col.avg_fact", "Середнє факт"))
        category_id = product.get("category_id")
        target_margin = self.db.get_category_target_margin(category_id)

        def _margin_color(margin_percent):
            try:
                margin_percent = float(margin_percent)
            except Exception:
                return ft.Colors.BLUE_GREY_900

            def _lerp_rgb(start, end, ratio):
                ratio = max(0.0, min(1.0, float(ratio)))
                r = int(start[0] + (end[0] - start[0]) * ratio)
                g = int(start[1] + (end[1] - start[1]) * ratio)
                b = int(start[2] + (end[2] - start[2]) * ratio)
                return f"#{r:02X}{g:02X}{b:02X}"

            pivot = float(target_margin) if target_margin is not None else 0.0
            red = (127, 29, 29)
            red_alert = (255, 0, 32)
            yellow = (250, 204, 21)
            green_light = (34, 197, 94)
            green_dark = (20, 83, 45)
            low_span = max(12.0, abs(pivot) * 0.6 if pivot else 18.0)
            high_span = max(12.0, abs(pivot) * 0.6 if pivot else 18.0)

            if margin_percent < 0:
                negative_span = max(8.0, low_span * 0.6)
                ratio = (margin_percent + negative_span) / negative_span
                return _lerp_rgb(red_alert, red, ratio)

            if margin_percent < pivot:
                ratio = (margin_percent - (pivot - low_span)) / low_span
                return _lerp_rgb(red, yellow, ratio)

            ratio = (margin_percent - pivot) / high_span
            return _lerp_rgb(green_light, green_dark, ratio)

        def _margin_fill_color(margin_percent):
            base = _margin_color(margin_percent)
            try:
                hex_color = str(base).lstrip("#")
                if len(hex_color) != 6:
                    return None
                red = int(hex_color[0:2], 16)
                green = int(hex_color[2:4], 16)
                blue = int(hex_color[4:6], 16)
                try:
                    margin_percent = float(margin_percent)
                except Exception:
                    margin_percent = 0.0
                if margin_percent < 0:
                    mix_ratio = 0.18
                else:
                    mix_ratio = 0.28
                fill_red = int(255 - (255 - red) * (1.0 - mix_ratio))
                fill_green = int(255 - (255 - green) * (1.0 - mix_ratio))
                fill_blue = int(255 - (255 - blue) * (1.0 - mix_ratio))
                return f"#{fill_red:02X}{fill_green:02X}{fill_blue:02X}"
            except Exception:
                return None

        col_widths = [92, 86] + [72 for _ in delivery_cols]
        if blended_usd is not None:
            col_widths.append(82)

        def _build_row(values, is_header=False, is_cost=False, colors=None):
            cells = []
            for idx, value in enumerate(values):
                txt = str(value if value not in (None, "") else "-")
                txt_color = ft.Colors.BLUE_GREY_900
                cell_bg = None
                if not is_header and not is_cost and idx >= 2 and txt.endswith("%"):
                    try:
                        pct = float(txt[:-1])
                        cell_bg = _margin_fill_color(pct)
                    except Exception:
                        cell_bg = None
                cells.append(
                    ft.Container(
                        width=(col_widths[idx] if idx < len(col_widths) else 140),
                        padding=ft.padding.symmetric(horizontal=8, vertical=6),
                        bgcolor=cell_bg,
                        border=(
                            ft.border.only(right=ft.BorderSide(1, _BORDER))
                            if idx < (len(values) - 1)
                            else None
                        ),
                        content=ft.Text(
                            txt,
                            size=11,
                            weight=(
                                "bold" if is_header or (is_cost and idx == 0) else None
                            ),
                            color=(ft.Colors.BLUE_GREY_900 if is_header else txt_color),
                            text_align=ft.TextAlign.CENTER,
                        ),
                    )
                )
            return ft.Container(
                border=ft.border.all(1, _BORDER),
                bgcolor=(
                    _ALT
                    if is_header
                    else (_ALT2 if is_cost else ft.Colors.WHITE)
                ),
                content=ft.Row(cells, spacing=0, tight=True),
            )

        cost_values = ["Собівартість", "-"]
        for col in delivery_cols:
            if (
                purchase_usd is None
                or packaging_usd is None
                or col.get("price_per_kg") is None
            ):
                cost_values.append("-")
                continue
            delivery_cost_usd = fx_rates.convert(
                (col.get("price_per_kg") or 0.0) * weight, col.get("currency"), pivot_ccy
            )
            if delivery_cost_usd is None:
                cost_values.append("-")
                continue
            full_cost_usd = purchase_usd + (packaging_usd or 0.0) + delivery_cost_usd
            cost_values.append(f"{full_cost_usd:.2f} {pivot_ccy}")

        if blended_usd is not None:
            cost_values.append(f"{blended_usd:.2f} {pivot_ccy}")

        matrix_rows = [
            {
                "values": cost_values,
                "is_cost": True,
                "colors": [None for _ in cost_values],
            }
        ]
        for tier in price_tiers or []:
            if not isinstance(tier, dict):
                continue
            tid = tier.get("id")
            tname = tier.get("name") or "Рівень"
            tccy = str(tier.get("currency") or "USD").upper()
            entry = (
                (sales_prices.get(tid) if tid else None)
                or sales_prices.get(f"name:{tname}")
                or {}
            )
            sale_val = self._to_float_or_none((entry or {}).get("price"))
            sale_txt = f"{sale_val:g} {tccy}" if sale_val is not None else "-"
            sale_usd = (
                fx_rates.convert(sale_val, tccy, pivot_ccy) if sale_val is not None else None
            )

            row_values = [tname, sale_txt]
            row_colors = [None, None]
            for col in delivery_cols:
                if (
                    sale_usd is None
                    or sale_usd <= 0
                    or purchase_usd is None
                    or packaging_usd is None
                    or col.get("price_per_kg") is None
                ):
                    row_values.append("-")
                    row_colors.append(None)
                    continue
                delivery_cost_usd = fx_rates.convert(
                    (col.get("price_per_kg") or 0.0) * weight, col.get("currency"), pivot_ccy
                )
                if delivery_cost_usd is None:
                    row_values.append("-")
                    row_colors.append(None)
                    continue

                # Margin is a ratio, invariant to which common pivot currency
                # everything above was converted into.
                cost_usd = purchase_usd + (packaging_usd or 0.0) + delivery_cost_usd
                margin = (sale_usd - cost_usd) / sale_usd
                margin_pct = margin * 100
                row_values.append(f"{margin_pct:.1f}%")
                row_colors.append(_margin_color(margin_pct))

            if blended_usd is not None:
                if sale_usd is None or sale_usd <= 0:
                    row_values.append("-")
                    row_colors.append(None)
                else:
                    margin = (sale_usd - blended_usd) / sale_usd
                    margin_pct = margin * 100
                    row_values.append(f"{margin_pct:.1f}%")
                    row_colors.append(_margin_color(margin_pct))

            matrix_rows.append(
                {"values": row_values, "is_cost": False, "colors": row_colors}
            )

        if len(matrix_rows) == 1:
            matrix_rows.append(
                {
                    "values": ["-", "-"] + ["-" for _ in delivery_cols] + (
                        ["-"] if blended_usd is not None else []
                    ),
                    "is_cost": False,
                    "colors": [None for _ in headers],
                }
            )

        matrix_table_rows = [_build_row(headers, is_header=True)]
        for row in matrix_rows:
            matrix_table_rows.append(
                _build_row(
                    row.get("values") or [],
                    is_cost=bool(row.get("is_cost")),
                    colors=row.get("colors") or [],
                )
            )

        tariffs_rows = []
        for col in delivery_cols:
            tariff_txt = "-"
            if col.get("price_per_kg") is not None:
                tariff_txt = f"{col.get('price_per_kg'):g} {col.get('currency')}/кг"
            tariffs_rows.append([col.get("name") or "Доставка", tariff_txt])
        tariffs_col_widths = [130, 100]

        def _tariff_row(values, is_header=False):
            return ft.Container(
                border=ft.border.all(1, _BORDER),
                bgcolor=(_ALT if is_header else ft.Colors.WHITE),
                content=ft.Row(
                    [
                        ft.Container(
                            width=tariffs_col_widths[0],
                            padding=ft.padding.symmetric(horizontal=8, vertical=6),
                            border=ft.border.only(right=ft.BorderSide(1, _BORDER)),
                            content=ft.Text(
                                str(values[0] if values[0] not in (None, "") else "-"),
                                size=11,
                                weight=("bold" if is_header else None),
                                text_align=ft.TextAlign.CENTER,
                            ),
                        ),
                        ft.Container(
                            width=tariffs_col_widths[1],
                            padding=ft.padding.symmetric(horizontal=8, vertical=6),
                            content=ft.Text(
                                str(values[1] if values[1] not in (None, "") else "-"),
                                size=11,
                                weight=("bold" if is_header else None),
                                text_align=ft.TextAlign.CENTER,
                            ),
                        ),
                    ],
                    spacing=0,
                    tight=True,
                ),
            )

        logger.debug(
            f"[profitability] {pname}: matrix_rows={len(matrix_rows)} delivery_cols={len(delivery_cols)}"
        )
        controls = [
            ft.Text(
                "Таблиця собівартості та рентабельності",
                size=12,
                weight="bold",
                color="grey800",
            ),
            ft.Text(
                (
                    f"Цільова рентабельність категорії: {target_margin:.1f}%"
                    if target_margin is not None
                    else "Цільова рентабельність категорії не задана"
                ),
                size=11,
                color="grey700",
            ),
            ft.Row(
                [ft.Column(matrix_table_rows, spacing=0, tight=True)],
                scroll=ft.ScrollMode.ADAPTIVE,
            ),
            ft.Divider(height=8, color="transparent"),
            ft.Text(self.t("products.price_matrix.tariffs_title", "Тарифи доставки"), size=12, weight="bold", color="grey800"),
            _tariff_row(
                [
                    self.t("products.price_matrix.col.delivery_type", "Тип доставки"),
                    self.t("products.price_matrix.col.tariff", "Тариф"),
                ],
                is_header=True,
            ),
        ]
        if tariffs_rows:
            for row in tariffs_rows:
                controls.append(_tariff_row(row))
        else:
            controls.append(_tariff_row(["-", "-"]))
        return controls

    def _create_product_card(self, product, is_archived: bool = False):
        def _card_width() -> int:
            def _to_int(value) -> int:
                try:
                    return int(float(value))
                except Exception:
                    return 0

            host_w = _to_int(getattr(self.content_area, "width", 0))
            page = self._page()
            page_w = _to_int(getattr(page, "width", 0)) if page else 0
            if page_w <= 0 and page is not None:
                win = getattr(page, "window", None)
                page_w = _to_int(getattr(win, "width", 0)) if win else 0
            viewport_w = host_w or page_w or 980

            if viewport_w <= 560:
                return max(320, viewport_w - 28)
            # ~60% wider than previous compact visual width.
            return max(520, min(680, int(viewport_w * 0.62)))

        display_name = product.get(
            "_display_name"
        ) or self._resolve_product_display_name(product)
        is_virtual = bool(product.get("_virtual"))

        node_id = str((product or {}).get("catalog_node_id") or "").strip()
        hierarchy_label = "-"
        if node_id:
            try:
                path_rows = self._product_path_cache.get(node_id)
                if path_rows is None:
                    path_rows = self.db.get_node_path(node_id) or []
                    self._product_path_cache[node_id] = path_rows
                path_names = [
                    str((row or {}).get("name") or "").strip()
                    for row in (path_rows or [])
                    if isinstance(row, dict)
                    and str((row or {}).get("name") or "").strip()
                ]
                if path_names:
                    hierarchy_label = " -> ".join(path_names)
            except Exception:
                hierarchy_label = "-"

        supplier_name = (
            (product.get("supplier") or {}).get("name")
            if isinstance(product.get("supplier"), dict)
            else ""
        ) or "-"
        packaging_row = (
            product.get("packaging")
            if isinstance(product.get("packaging"), dict)
            else {}
        )
        packaging_name = (
            self._packaging_label_with_owner(packaging_row, product.get("company_id"))
            or "None"
        )

        purchase_price = (
            product.get("purchase_price") if isinstance(product, dict) else None
        )
        purchase_currency = (
            product.get("purchase_currency") if isinstance(product, dict) else None
        ) or "-"
        weight = product.get("weight_kg") if isinstance(product, dict) else None
        production_term_days = (
            product.get("production_term_days") if isinstance(product, dict) else None
        )

        purchase_amount_only = "-"
        if purchase_price not in (None, ""):
            purchase_amount_only = f"{purchase_price}"

        purchase_ccy_norm = str(purchase_currency or "").strip().upper()
        purchase_usd = self._convert_to_usd(purchase_price, purchase_ccy_norm)
        show_purchase_usd = purchase_usd is not None and purchase_ccy_norm not in {
            "USD",
            "$",
        }
        purchase_usd_label = f"~${purchase_usd:.2f}" if show_purchase_usd else ""

        weight_label = "-"
        if weight not in (None, ""):
            weight_label = f"{weight} кг"

        production_term_label = "-"
        if production_term_days not in (None, ""):
            production_term_label = f"{production_term_days} дн"

        secondary_parts = [
            f"Валюта закупівлі: {purchase_ccy_norm or '-'}",
            f"Вага: {weight_label}",
            f"Термін виробництва: {production_term_label}",
        ]
        if show_purchase_usd:
            secondary_parts.insert(1, f"В $ по курсу: {purchase_usd_label}")

        notes_text = str(product.get("notes") or "").strip()

        def _show_product_photo(_):
            _urls = [
                self.db.resolve_image_file_path(str((product or {}).get(k) or "").strip())
                for k in ("image", "image2", "image3")
                if str((product or {}).get(k) or "").strip()
            ]
            if not _urls:
                self._show_snack(self.t("products.no_photo", "У цього товару немає фото"), "orange")
                return

            page = self._page()
            if not page:
                return

            _dw = (getattr(page, "window_width", None) or 1200) * 0.72
            _dh = (getattr(page, "window_height", None) or 900) * 0.86
            _img_w = _dw - 120
            _img_h = _dh - 50
            _idx = [0]

            _gal_img = ft.Image(
                src=_urls[0],
                width=_img_w,
                height=_img_h,
                fit=ft.BoxFit.CONTAIN,
            )
            _counter = ft.Text(
                f"1 / {len(_urls)}", color=ft.Colors.GREY_500, size=11,
                visible=len(_urls) > 1,
            )

            def _go(delta, _=None):
                _idx[0] = (_idx[0] + delta) % len(_urls)
                _gal_img.src = _urls[_idx[0]]
                _counter.value = f"{_idx[0] + 1} / {len(_urls)}"
                with contextlib.suppress(Exception):
                    _gal_img.update()
                    _counter.update()

            _arrow_style = ft.ButtonStyle(
                bgcolor=ft.Colors.with_opacity(0.22, ft.Colors.WHITE),
                shape=ft.CircleBorder(),
            )
            _prev = ft.IconButton(
                icon=ft.Icons.CHEVRON_LEFT, icon_color=ft.Colors.WHITE, icon_size=34,
                on_click=lambda _: _go(-1), visible=len(_urls) > 1, style=_arrow_style,
            )
            _next_btn = ft.IconButton(
                icon=ft.Icons.CHEVRON_RIGHT, icon_color=ft.Colors.WHITE, icon_size=34,
                on_click=lambda _: _go(1), visible=len(_urls) > 1, style=_arrow_style,
            )

            _dlg_ref = [None]

            def _close_photo_dlg(_=None):
                page.on_keyboard_event = _prev_kb_handler[0]
                with contextlib.suppress(Exception):
                    page.pop_dialog()

            _prev_kb_handler = [getattr(page, "on_keyboard_event", None)]

            def _on_kb(e: ft.KeyboardEvent):
                if e.key == "Arrow Left":
                    _go(-1)
                elif e.key == "Arrow Right":
                    _go(1)
                elif e.key == "Escape":
                    _close_photo_dlg()

            page.on_keyboard_event = _on_kb

            _dlg_ref[0] = ft.AlertDialog(
                content=ft.Stack(
                    [
                        ft.Container(
                            width=_dw, height=_dh,
                            bgcolor="#111111",
                            content=ft.Column(
                                [
                                    ft.Row(
                                        [
                                            _prev,
                                            ft.Container(
                                                content=_gal_img, expand=True,
                                                alignment=ft.Alignment(0, 0),
                                            ),
                                            _next_btn,
                                        ],
                                        expand=True,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        spacing=4,
                                    ),
                                    ft.Container(
                                        content=_counter,
                                        alignment=ft.Alignment(0, 0),
                                        padding=ft.padding.only(bottom=4),
                                    ),
                                ],
                                spacing=0, expand=True,
                            ),
                        ),
                        ft.Container(
                            content=ft.IconButton(
                                icon=ft.Icons.CLOSE,
                                icon_color=ft.Colors.WHITE,
                                icon_size=20,
                                on_click=_close_photo_dlg,
                                style=ft.ButtonStyle(
                                    bgcolor=ft.Colors.with_opacity(0.55, ft.Colors.BLACK),
                                ),
                            ),
                            right=6, top=6,
                        ),
                    ],
                    width=_dw, height=_dh,
                ),
                bgcolor="#111111",
                content_padding=ft.padding.all(0),
                on_dismiss=lambda _: setattr(page, "on_keyboard_event", _prev_kb_handler[0]),
            )
            page.open(_dlg_ref[0])

        # Parse sales prices for the inline price panel
        _sales_rows = []
        _raw_sales = product.get("sales_prices_json")
        if isinstance(_raw_sales, list):
            _sales_rows = [r for r in _raw_sales if isinstance(r, dict)]
        elif isinstance(_raw_sales, str) and _raw_sales.strip():
            try:
                _parsed = json.loads(_raw_sales)
                if isinstance(_parsed, list):
                    _sales_rows = [r for r in _parsed if isinstance(r, dict)]
            except Exception:
                pass

        if _sales_rows:
            _price_rows = []
            for _sr in _sales_rows:
                _pval = self._to_float_or_none(_sr.get("price"))
                _pname = str(_sr.get("name") or "").strip() or "Рівень"
                _pccy = str(_sr.get("currency") or "").strip()
                _pstr = format_grouped(_pval) if _pval is not None else "—"
                _price_rows.append(
                    ft.Row(
                        [
                            ft.Text(_pname, size=10, color="grey600", expand=True),
                            ft.Text(
                                f"{_pstr} {_pccy}".strip(),
                                size=10,
                                weight="w600",
                            ),
                        ],
                        spacing=4,
                    )
                )
            _prices_body = ft.Column(_price_rows, spacing=3, tight=True)
        else:
            _prices_body = ft.Text("Ціни не вказані", size=10, color="grey500", italic=True)

        _price_panel = ft.Container(
            width=190,
            padding=ft.padding.symmetric(horizontal=6, vertical=2),
            content=ft.Column(
                [
                    ft.Text("Ціни продажу", size=10, weight="bold", color="grey700"),
                    ft.Divider(height=3, color="transparent"),
                    _prices_body,
                ],
                spacing=3,
                tight=True,
            ),
        )

        # Combined card: main info + prices in one border block.
        return ft.Container(
            width=_card_width(),
            padding=ft.padding.symmetric(horizontal=8, vertical=6),
            margin=ft.margin.only(bottom=4),
            border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
            border_radius=8,
            bgcolor=_PANEL,
            content=ft.Row(
                [
                    ft.Column(
                        [
                            ft.Row(
                                [
                                    ft.Text(
                                        display_name or "Без назви",
                                        size=13,
                                        weight="bold",
                                        selectable=True,
                                        expand=True,
                                    ),
                                    ft.Container(width=0, height=0),
                                ],
                                spacing=10,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                            ft.Column(
                                [
                                    ft.Text(
                                        spans=[
                                            ft.TextSpan("Постачальник: ", style=ft.TextStyle(size=11, color="orange" if is_virtual else "grey700")),
                                            ft.TextSpan(supplier_name, style=ft.TextStyle(size=11, color="orange" if is_virtual else "grey700", weight=ft.FontWeight.BOLD)),
                                            ft.TextSpan(
                                                (f" | Пакування: {packaging_name}" if self.packaging_enabled else "")
                                                + f" | Закупівля: {purchase_amount_only}",
                                                style=ft.TextStyle(size=11, color="orange" if is_virtual else "grey700"),
                                            ),
                                        ],
                                    ),
                                    ft.Text(" | ".join(secondary_parts), size=11, color="orange" if is_virtual else "grey700"),
                                    ft.Text(
                                        spans=[
                                            ft.TextSpan("Ієрархія: ", style=ft.TextStyle(size=11, color="orange" if is_virtual else "grey700")),
                                            ft.TextSpan(hierarchy_label, style=ft.TextStyle(size=11, color="orange" if is_virtual else "grey700", weight=ft.FontWeight.BOLD)),
                                        ],
                                    ),
                                    *(
                                        [ft.Text(f"Нотатки: {notes_text}", size=11, color="orange" if is_virtual else "grey700")]
                                        if notes_text else []
                                    ),
                                    *(
                                        [ft.Text("Серія як товар (чернетка)", size=11, color="orange")]
                                        if is_virtual else []
                                    ),
                                ],
                                spacing=2,
                                tight=True,
                            ),
                            ft.Row(
                                [
                                    ft.OutlinedButton(
                                        self.t("products.view_btn", "Переглянути товар"),
                                        icon=ft.Icons.VISIBILITY,
                                        height=28,
                                        style=ft.ButtonStyle(
                                            padding=ft.padding.symmetric(
                                                horizontal=7, vertical=1
                                            ),
                                            text_style=ft.TextStyle(size=11),
                                        ),
                                        on_click=lambda _, _p=product: self._page()
                                        and self._page().run_task(
                                            self.show_product_details_dialog, _p
                                        ),
                                    ),
                                    ft.OutlinedButton(
                                        "Фото",
                                        icon=ft.Icons.IMAGE,
                                        height=28,
                                        style=ft.ButtonStyle(
                                            padding=ft.padding.symmetric(
                                                horizontal=7, vertical=1
                                            ),
                                            text_style=ft.TextStyle(size=11),
                                        ),
                                        visible=any(
                                            str((product or {}).get(k) or "").strip()
                                            for k in ("image", "image2", "image3")
                                        ),
                                        on_click=_show_product_photo,
                                    ),
                                    ft.OutlinedButton(
                                        self.t("products.edit_btn", "Редагувати товар"),
                                        icon=ft.Icons.EDIT,
                                        height=28,
                                        style=ft.ButtonStyle(
                                            padding=ft.padding.symmetric(
                                                horizontal=7, vertical=1
                                            ),
                                            text_style=ft.TextStyle(size=11),
                                        ),
                                        visible=not self.read_only_subordinate
                                        and getattr(self, "_region_create_allowed", True),
                                        on_click=lambda _, _p=product: self._page()
                                        and self._page().run_task(
                                            self.show_product_dialog, _p
                                        ),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.UNARCHIVE_OUTLINED
                                        if is_archived
                                        else ft.Icons.ARCHIVE_OUTLINED,
                                        icon_color="blue" if is_archived else "grey700",
                                        tooltip=(
                                            self.t("products.restore", "Відновити з архіву")
                                            if is_archived
                                            else self.t("products.archive", "Архівувати")
                                        ),
                                        visible=not self.read_only_subordinate
                                        and getattr(self, "_region_create_allowed", True)
                                        and bool(product.get("id")),
                                        on_click=(
                                            (lambda _: self.restore_product(product.get("id")))
                                            if is_archived
                                            else (lambda _: self.archive_product(product.get("id")))
                                        ),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.DELETE_OUTLINE,
                                        icon_color="red",
                                        tooltip="Видалити назавжди",
                                        visible=self._is_admin_or_superadmin()
                                        and bool(product.get("id")),
                                        on_click=lambda _, pid=product.get("id"): self.delete_product(
                                            pid
                                        ),
                                    ),
                                ],
                                spacing=8,
                                wrap=True,
                                tight=True,
                            ),
                        ],
                        spacing=5,
                        tight=True,
                        expand=True,
                    ),
                    ft.VerticalDivider(width=1, color=ft.Colors.BLUE_GREY_100),
                    _price_panel,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
        )

    def _close_dlg(self, dlg):
        page = self._page()
        if not page:
            logger.warning("[modal:products] close skipped: page is None")
            return
        logger.info(
            "[modal:products] close:start dlg=%s@%s open=%s overlay_before=%s",
            type(dlg).__name__,
            id(dlg),
            getattr(dlg, "open", None),
            len(getattr(page, "overlay", []) or []),
        )
        page.pop_dialog()
        logger.info(
            "[modal:products] close:finish dlg=%s@%s open=%s overlay_after=%s",
            type(dlg).__name__,
            id(dlg),
            getattr(dlg, "open", None),
            len(getattr(page, "overlay", []) or []),
        )

    async def show_product_details_dialog(
        self, product, on_switch_to_edit=None, _dlg_ref=None, existing_dlg=None
    ):
        """Builds the (DB-heavy) product card dialog off the UI thread, then
        shows it. On Android, a handful of sequential blocking DB round
        trips done inline on the single Flet event loop (companies/regions/
        node-path/delivery-methods lookups) is what made this and every
        other modal take up to a minute to open — the network itself was
        fine, the event loop was just starved. Desktop's low-latency LAN
        masked this completely.

        existing_dlg: reuse this already-open AlertDialog in place (patch
        title/content/actions, then .update()) instead of opening a new
        one — see show_product_dialog's matching parameter for why."""
        dlg = await asyncio.to_thread(
            self._build_product_details_dialog,
            product, on_switch_to_edit, _dlg_ref, existing_dlg,
        )
        if existing_dlg is not None:
            with contextlib.suppress(Exception):
                dlg.update()
        else:
            self.content_area.page.show_dialog(dlg)

    def _build_product_details_dialog(
        self, product, on_switch_to_edit=None, _dlg_ref=None, existing_dlg=None
    ) -> ft.AlertDialog:
        # Declared up front (not at construction time below) so the
        # "Закрити" button's closure — and _dlg_ref — always target the
        # SAME object whether it's freshly created or an already-open
        # dialog being reused for a Редагування->Перегляд switch.
        dlg = existing_dlg if existing_dlg is not None else ft.AlertDialog()
        product = product if isinstance(product, dict) else {}
        display_name = product.get(
            "_display_name"
        ) or self._resolve_product_display_name(product)
        company_id = product.get("company_id") or self.company_id
        company_name = next(
            (
                row.get("name")
                for row in self.db.get_all_companies() or []
                if isinstance(row, dict) and row.get("id") == company_id
            ),
            "",
        )
        region_name = next(
            (
                row.get("name")
                for row in self.db.get_company_regions(company_id) or []
                if isinstance(row, dict) and row.get("id") == product.get("region_id")
            ),
            "-",
        )
        category_name = (
            (product.get("category") or {}).get("name")
            if isinstance(product.get("category"), dict)
            else ""
        ) or "-"
        supplier_name = (
            (product.get("supplier") or {}).get("name")
            if isinstance(product.get("supplier"), dict)
            else ""
        ) or "-"
        packaging_row = (
            product.get("packaging")
            if isinstance(product.get("packaging"), dict)
            else {}
        )
        packaging_name = (
            self._packaging_label_with_owner(packaging_row, company_id) or "None"
        )
        purchase_price_top = self._to_float_or_none(product.get("purchase_price"))
        purchase_ccy_top = str(product.get("purchase_currency") or "USD").upper()
        purchase_usd_top = (
            self._convert_to_usd(purchase_price_top, purchase_ccy_top)
            if purchase_price_top is not None
            else None
        )
        packaging_price_top = self._to_float_or_none(packaging_row.get("price"))
        packaging_ccy_top = (
            (packaging_row.get("suppliers") or {}).get("currency")
            if isinstance(packaging_row.get("suppliers"), dict)
            else None
        ) or purchase_ccy_top
        packaging_ccy_top = str(packaging_ccy_top or "USD").upper()
        packaging_usd_top = (
            self._convert_to_usd(packaging_price_top, packaging_ccy_top)
            if packaging_price_top is not None
            else 0.0
        )
        full_price_label = "-"
        if purchase_usd_top is not None and packaging_usd_top is not None:
            full_usd = purchase_usd_top + (packaging_usd_top or 0.0)
            full_in_purchase_ccy = self._convert_from_usd(full_usd, purchase_ccy_top)
            if full_in_purchase_ccy is not None:
                full_price_label = (
                    f"{full_in_purchase_ccy:g} {purchase_ccy_top} (~{full_usd:.2f} USD)"
                )

        path_names = []
        for node in self.db.get_node_path(product.get("catalog_node_id")) or []:
            if isinstance(node, dict) and node.get("name"):
                path_names.append(node.get("name"))
        hierarchy_text = " -> ".join(path_names) if path_names else "-"

        product_region_id = product.get("region_id")

        def _value_row(label, value):
            return ft.Column(
                [
                    ft.Text(label, size=11, color="grey700"),
                    ft.Text(value or "-", size=14, selectable=True),
                ],
                spacing=2,
            )

        # Narrow the cost-price/margin table down to only the delivery
        # methods this product's supplier actually ships by (set on the
        # supplier, "Якими способами доставки веземо товар цього
        # постачальника") — an unset/empty list on the supplier means
        # unrestricted (every region method applies, same as before this
        # feature existed), so untouched suppliers keep showing everything.
        _supplier_ctx = (
            product.get("supplier") if isinstance(product.get("supplier"), dict) else {}
        )
        _supplier_enabled_ids_raw = _supplier_ctx.get("enabled_delivery_method_ids")
        _supplier_enabled_ids = (
            _supplier_enabled_ids_raw if isinstance(_supplier_enabled_ids_raw, list) else []
        )
        _supplier_restricted = len(_supplier_enabled_ids) > 0
        selected_delivery_ids = set()
        for method in self.db.get_effective_delivery_methods_for_manager(
            company_id, self.manager_id, product_region_id
        ):
            if not isinstance(method, dict) or not method.get("id"):
                continue
            if _supplier_restricted and method.get("id") not in _supplier_enabled_ids:
                continue
            selected_delivery_ids.add(method.get("id"))
        profitability_controls = self._profitability_controls(
            product, selected_delivery_ids
        )

        # --- Responsive layout: left 65%, right 35% ---
        _panel_spacing = 4
        _dialog_w = self._dialog_content_width(desired=900, min_width=680)
        _left_w = int(_dialog_w * 0.65)
        _right_w = _dialog_w - _left_w - _panel_spacing
        _dlg_h = self._dialog_content_height(700, 380)

        # Image sizes: 300px large, 145px each small (2×145+8≈300).
        _large_img_w = 300
        _large_img_h = 300
        _small_img_w = 145
        _small_img_h = 145

        # Collect non-empty photo URLs in display order for gallery navigation.
        _photo_urls = [
            self.db.resolve_image_source(str((product or {}).get(k) or "").strip())
            for k in ("image", "image2", "image3")
            if str((product or {}).get(k) or "").strip()
        ]

        def _photo_gallery(start_idx: int = 0):
            """Full-screen gallery dialog — arrows, keyboard, Esc to close."""
            urls = _photo_urls
            if not urls:
                return
            _pg = self.content_area.page
            _idx = [max(0, min(start_idx, len(urls) - 1))]
            _dw = (getattr(_pg, "window_width", None) or getattr(_pg, "width", None) or 1200) * 0.88
            _dh = (getattr(_pg, "window_height", None) or getattr(_pg, "height", None) or 900) * 0.90
            _img_w = _dw - 120
            _img_h = _dh - 60

            _gal_img = ft.Image(
                src=urls[_idx[0]],
                width=_img_w,
                height=_img_h,
                fit=ft.BoxFit.CONTAIN,
            )
            _gal_counter = ft.Text(
                f"{_idx[0] + 1} / {len(urls)}",
                color=ft.Colors.GREY_500,
                size=11,
                visible=len(urls) > 1,
            )

            def _go(delta, _=None):
                _idx[0] = (_idx[0] + delta) % len(urls)
                _gal_img.src = urls[_idx[0]]
                _gal_counter.value = f"{_idx[0] + 1} / {len(urls)}"
                with contextlib.suppress(Exception):
                    _gal_img.update()
                    _gal_counter.update()

            _arrow_style = ft.ButtonStyle(
                bgcolor=ft.Colors.with_opacity(0.22, ft.Colors.WHITE),
                shape=ft.CircleBorder(),
            )
            _prev_btn = ft.IconButton(
                icon=ft.Icons.CHEVRON_LEFT,
                icon_color=ft.Colors.WHITE,
                icon_size=34,
                on_click=lambda _: _go(-1),
                visible=len(urls) > 1,
                style=_arrow_style,
            )
            _next_btn = ft.IconButton(
                icon=ft.Icons.CHEVRON_RIGHT,
                icon_color=ft.Colors.WHITE,
                icon_size=34,
                on_click=lambda _: _go(1),
                visible=len(urls) > 1,
                style=_arrow_style,
            )

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
                            width=_dw,
                            height=_dh,
                            bgcolor="#111111",
                            content=ft.Column(
                                [
                                    ft.Row(
                                        [
                                            _prev_btn,
                                            ft.Container(
                                                content=_gal_img,
                                                expand=True,
                                                alignment=ft.Alignment(0, 0),
                                            ),
                                            _next_btn,
                                        ],
                                        expand=True,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        spacing=4,
                                    ),
                                    ft.Container(
                                        content=_gal_counter,
                                        alignment=ft.Alignment(0, 0),
                                        padding=ft.padding.only(bottom=6),
                                    ),
                                ],
                                spacing=0,
                                expand=True,
                            ),
                        ),
                        ft.Container(
                            content=ft.IconButton(
                                icon=ft.Icons.CLOSE,
                                icon_color=ft.Colors.WHITE,
                                icon_size=20,
                                on_click=_close_gal,
                                style=ft.ButtonStyle(
                                    bgcolor=ft.Colors.with_opacity(0.55, ft.Colors.BLACK),
                                ),
                            ),
                            right=6,
                            top=6,
                        ),
                    ],
                    width=_dw,
                    height=_dh,
                ),
                bgcolor="#111111",
                content_padding=ft.padding.all(0),
                on_dismiss=lambda _: setattr(_pg, "on_keyboard_event", _prev_kb[0]),
            )
            _pg.open(_gal_dlg_ref[0])

        def _img_box(key, width, height, gallery_idx=None):
            raw = str((product or {}).get(key) or "").strip()
            if raw:
                _click = (lambda e, i=gallery_idx: _photo_gallery(i)) if gallery_idx is not None else None
                return ft.Container(
                    width=width,
                    height=height,
                    border_radius=8,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    border=ft.border.all(1, _BORDER),
                    bgcolor=_ALT,
                    alignment=ft.alignment.Alignment(0, 0),
                    on_click=_click,
                    content=ft.Image(
                        src=self.db.resolve_image_source(raw),
                        fit=ft.BoxFit.CONTAIN,
                        width=width,
                        height=height,
                    ),
                )
            return ft.Container(
                width=width,
                height=height,
                border_radius=8,
                border=ft.border.all(1, _BORDER),
                bgcolor=_ALT,
                alignment=ft.alignment.Alignment(0, 0),
                content=ft.Column(
                    [
                        ft.Icon(ft.Icons.IMAGE_OUTLINED, size=max(18, int(width * 0.18)), color="grey400"),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=4,
                ),
            )

        # Map gallery index: only images that actually exist get an index.
        _gal_idx = {
            k: i
            for i, k in enumerate(
                k for k in ("image", "image2", "image3")
                if str((product or {}).get(k) or "").strip()
            )
        }

        _images_panel = ft.Column(
            [
                _img_box("image", _large_img_w, _large_img_h, _gal_idx.get("image")),
                ft.Row(
                    [
                        _img_box("image2", _small_img_w, _small_img_h, _gal_idx.get("image2")),
                        _img_box("image3", _small_img_w, _small_img_h, _gal_idx.get("image3")),
                    ],
                    spacing=8,
                    tight=True,
                ),
            ],
            spacing=8,
            tight=True,
        )

        _mode_toggle = (
            ft.Row(
                [
                    ft.FilledButton(
                        self.t("products.mode.view", "Перегляд"),
                        style=ft.ButtonStyle(
                            padding=ft.padding.symmetric(horizontal=10, vertical=2),
                            text_style=ft.TextStyle(size=11),
                        ),
                        height=28,
                        disabled=True,
                    ),
                    ft.OutlinedButton(
                        self.t("products.mode.edit", "Редагування"),
                        on_click=lambda _: on_switch_to_edit(),
                        style=ft.ButtonStyle(
                            padding=ft.padding.symmetric(horizontal=10, vertical=2),
                            text_style=ft.TextStyle(size=11),
                        ),
                        height=28,
                    ),
                ],
                spacing=4,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            if callable(on_switch_to_edit)
            else None
        )

        dlg.title = (
            ft.Row(
                [
                    ft.Text(
                        f"Картка товару: {display_name}",
                        selectable=True,
                        expand=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                        max_lines=1,
                    ),
                    _mode_toggle,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            if _mode_toggle
            else ft.Text(f"Перегляд товару: {display_name}", selectable=True)
        )
        dlg.content = (
            ft.Container(
                width=_dialog_w,
                height=_dlg_h,
                padding=0,
                margin=0,
                content=ft.Row(
                    [
                        ft.Container(
                            width=_left_w,
                            height=_dlg_h,
                            content=ft.Column(
                                [
                                    ft.Row(
                                        [
                                            _value_row("Компанія", company_name),
                                            _value_row("Регіон", region_name),
                                            _value_row("Категорія", category_name),
                                        ],
                                        spacing=20,
                                        wrap=True,
                                    ),
                                    ft.Row(
                                        [
                                            _value_row("Постачальник", supplier_name),
                                            *(
                                                [_value_row("Пакування", packaging_name)]
                                                if self.packaging_enabled
                                                else []
                                            ),
                                            _value_row("Ціна повна", full_price_label),
                                        ],
                                        spacing=20,
                                        wrap=True,
                                    ),
                                    ft.Row(
                                        [
                                            _value_row(
                                                "Ціна закупівлі",
                                                f"{product.get('purchase_price') or '-'} {product.get('purchase_currency') or ''}",
                                            ),
                                            _value_row(
                                                "Вага",
                                                f"{product.get('weight_kg') or '-'} кг",
                                            ),
                                            _value_row(
                                                "Термін виробництва",
                                                (
                                                    f"{product.get('production_term_days')} дн"
                                                    if product.get(
                                                        "production_term_days"
                                                    )
                                                    not in (None, "")
                                                    else "-"
                                                ),
                                            ),
                                        ],
                                        spacing=20,
                                        wrap=True,
                                    ),
                                    _value_row("Ієрархія", hierarchy_text),
                                    _value_row("Нотатки", product.get("notes")),
                                    ft.Divider(),
                                    ft.Text(
                                        "Аналітика цін, собівартості і рентабельності",
                                        weight="bold",
                                    ),
                                    *profitability_controls,
                                ],
                                spacing=12,
                                scroll=ft.ScrollMode.ADAPTIVE,
                            ),
                        ),
                        ft.Container(
                            width=_large_img_w + 4,
                            padding=0,
                            margin=0,
                            alignment=ft.Alignment(x=-1, y=-1),
                            content=_images_panel,
                        ),
                    ],
                    spacing=_panel_spacing,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
            )
        )
        dlg.actions = [ft.TextButton(self.t("packaging.dlg.close", "Закрити"), on_click=lambda _: self._close_dlg(dlg))]
        if isinstance(_dlg_ref, list):
            _dlg_ref[0] = dlg
        return dlg

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

    def _open_bulk_template(self):
        """Відкрити діалог «Товар по шаблону» для поточного користувача."""
        if callable(self._on_bulk_template):
            self._on_bulk_template()
        else:
            self._show_snack("Функція недоступна", "orange")

    async def show_product_dialog(
        self, product=None, on_switch_to_view=None, existing_dlg=None
    ):
        """Async so the lock-acquisition/hydration/companies-list DB round
        trips below (and the dropdown-cascade population further down, see
        _on_company_change et al.) run off the UI thread — on Android these
        used to run inline on Flet's single event loop, which is what made
        opening this dialog take up to a minute.

        existing_dlg: when the quick-view modal is switching from
        Перегляд to Редагування, the ALREADY-OPEN AlertDialog object is
        passed in here and reused in place (its title/content/actions
        mutated, then .update()) instead of opening a second dialog and
        closing the first — that close+reopen choreography raced Flet's
        dialog transition no matter how carefully it was timed and could
        silently leave nothing open at all. `dlg` below always refers to
        this SAME object throughout the function (declared once, up
        front) specifically so every closure that references `dlg` -
        including ones defined long before the dialog's actual
        title/content/actions are — targets the right one either way."""
        logger.info(
            "[edit-switch] show_product_dialog entered pid=%s read_only_subordinate=%s",
            (product or {}).get("id"), self.read_only_subordinate,
        )
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        dlg = existing_dlg if existing_dlg is not None else ft.AlertDialog()
        is_edit = isinstance(product, dict) and bool((product or {}).get("id"))
        try:
            _has_access = await asyncio.to_thread(
                self.db.manager_has_category_access_in_region,
                self.manager_id, self.company_id, self._effective_region_id()
            )
        except Exception:
            _has_access = True
        if not _has_access:
            self._show_snack(
                self.t(
                    "products.no_category_access",
                    "Немає доступу до категорій у цьому регіоні — лише перегляд",
                ),
                "orange",
            )
            return
        product = product or {}
        self.temp_image_path = None
        edit_lock_token = ""
        lock_company_id = product.get("company_id") or self.company_id
        lock_released_ref = {"done": False}
        lock_heartbeat_task_ref = {"task": None}

        # IMPORTANT: on edit, always hydrate from DB so the modal shows exactly what is saved.
        # UI list rows may be partial and can miss fields like supplier_id/packaging_id.
        if is_edit:
            pid = str((product or {}).get("id") or "").strip()
            if pid:
                try:
                    fresh = await asyncio.to_thread(
                        self.db.get_product_by_id, lock_company_id, pid
                    )
                    if isinstance(fresh, dict):
                        merged = dict(product)
                        merged.update(fresh)  # DB wins
                        product = merged
                except Exception:
                    logger.exception("Failed to hydrate product from DB for edit modal")

        if is_edit:
            lock_res = await asyncio.to_thread(
                self.db.acquire_edit_lock,
                lock_company_id,
                "products",
                product.get("id"),
                self.manager_id,
                self.user_data.get("full_name") or "",
                ttl_seconds=180,
            )
            if not bool(lock_res.get("ok")):
                self._show_snack(
                    lock_res.get("message") or self.t("products.locked_by_other", "Запис редагується іншим користувачем"),
                    "orange",
                )
                return
            edit_lock_token = lock_res.get("lock_token") or ""

            async def _heartbeat_loop():
                while True:
                    await asyncio.sleep(90)
                    if lock_released_ref["done"]:
                        return
                    await asyncio.to_thread(
                        self.db.heartbeat_edit_lock,
                        lock_company_id,
                        "products",
                        product.get("id"),
                        self.manager_id,
                        edit_lock_token,
                        ttl_seconds=180,
                    )

            try:
                lock_heartbeat_task_ref["task"] = asyncio.create_task(_heartbeat_loop())
            except Exception:
                logger.exception("Failed to start product lock heartbeat")

        def _release_lock_once():
            if (not is_edit) or lock_released_ref["done"]:
                return
            lock_released_ref["done"] = True
            hb_task = lock_heartbeat_task_ref.get("task")
            if hb_task and not hb_task.done():
                hb_task.cancel()
            _pg_release = self._page()
            if _pg_release and hasattr(_pg_release, "run_task"):
                async def _release_async():
                    await asyncio.to_thread(
                        self.db.release_edit_lock,
                        lock_company_id,
                        "products",
                        product.get("id"),
                        self.manager_id,
                        edit_lock_token,
                    )
                with contextlib.suppress(Exception):
                    _pg_release.run_task(_release_async)
            else:
                self.db.release_edit_lock(
                    lock_company_id,
                    "products",
                    product.get("id"),
                    self.manager_id,
                    edit_lock_token,
                )

        companies = (
            await asyncio.to_thread(self.db.get_all_companies)
            if self.is_superadmin
            else []
        )
        default_company_id = (
            product.get("company_id")
            or (self.company_id if not is_edit else None)
            or next(
                (c.get("id") for c in companies if isinstance(c, dict) and c.get("id")),
                None,
            )
        )

        # Build path map from leaf node for pre-filling hierarchy dropdowns on edit
        _edit_path_map = {}
        if is_edit and product.get("catalog_node_id"):
            _node_path = await asyncio.to_thread(
                self.db.get_node_path, product.get("catalog_node_id")
            )
            for _ni in _node_path:
                if isinstance(_ni, dict) and _ni.get("position") and _ni.get("id"):
                    _edit_path_map[int(_ni["position"])] = _ni["id"]

        company_f = ft.Dropdown(
            label="Компанія",
            options=[
                ft.dropdown.Option(key=c.get("id"), text=c.get("name"))
                for c in companies
                if isinstance(c, dict) and c.get("id")
            ],
            value=default_company_id,
            border_radius=10,
            visible=self.is_superadmin,
        )

        region_f = ft.Dropdown(label="Регіон", options=[], border_radius=10)
        category_f = ft.Dropdown(label="Категорія", options=[], border_radius=10)
        hierarchy_info = ft.Text("", size=11, color="grey700")

        level_fields = {}
        for pos in range(1, self.MAX_LEVELS + 1):
            label = f"Рівень {pos}"
            selector = ft.Dropdown(label=label, options=[], border_radius=10)
            new_name = ft.TextField(
                label=f"Нова назва: {label.lower()}", border_radius=10, visible=False
            )
            level_fields[pos] = {
                "select": selector,
                "new": new_name,
                "rows": [],
                "block": ft.Column([selector, new_name], spacing=6, visible=False),
                "initial_id": _edit_path_map.get(pos) if is_edit else None,
            }
        hierarchy_container = ft.Column(spacing=8)

        # --- Extra optional level for mixed-depth (some nodes deeper than category default) ---
        extra_level_new = ft.TextField(
            label=self.t("products.field.name", "Назва товару"), border_radius=10, visible=False
        )
        extra_level_block = ft.Column(
            [extra_level_new],
            spacing=6,
            visible=False,
        )
        extra_level_toggle = ft.Checkbox(
            label="Додати деталізацію (підрівень)",
            value=False,
        )
        # Extra initial is computed lazily inside _rebuild_hierarchy_fields (depends on category config)
        _extra_initial_id_ref = [None]  # [node_id] set in _rebuild_hierarchy_fields
        _extra_level_pos_ref = [None]  # [computed_extra_position]

        def _sync_extra_new_visibility():
            extra_level_new.visible = bool(extra_level_block.visible)

        def _toggle_extra_level(e):
            extra_level_block.visible = bool(extra_level_toggle.value)
            if (
                extra_level_block.visible
                and is_edit
                and not (extra_level_new.value or "").strip()
            ):
                extra_level_new.value = str(product.get("name") or "").strip()
            _sync_extra_new_visibility()
            with contextlib.suppress(Exception):
                extra_level_block.update()

        extra_level_toggle.on_change = _toggle_extra_level

        name_f = ft.TextField(label=self.t("products.field.name", "Назва товару"), border_radius=10)
        error_text = ft.Text(
            "",
            color="red",
            size=11,
            weight="bold",
            visible=False,
        )
        manager_f = ft.Dropdown(
            label="Користувач-власник",
            options=[],
            border_radius=10,
            visible=self._is_admin_or_superadmin(),
        )
        supplier_f = ft.Dropdown(label=self.t("orders.filter.supplier", "Постачальник"), options=[], border_radius=10)
        packaging_f = ft.Dropdown(label=self.t("nav.packaging", "Пакування"), options=[], border_radius=10, visible=self.packaging_enabled)
        purchase_price_f = ft.TextField(label=self.t("products.field.purchase_price", "Ціна закупівлі"), border_radius=10)
        purchase_currency_f = ft.Dropdown(
            label=self.t("products.field.purchase_currency", "Валюта закупівлі"),
            options=[ft.dropdown.Option("")]
            + [ft.dropdown.Option(x) for x in self.CURRENCIES],
            border_radius=10,
            value=("USD" if not is_edit else ""),
            disabled=True,
        )
        purchase_sum_usd_f = ft.TextField(
            label="Сума закупівлі ($)",
            border_radius=10,
            read_only=True,
            visible=False,
        )
        weight_f = ft.TextField(label=self.t("products.field.weight", "Вага (кг)"), border_radius=10)
        production_term_f = ft.TextField(
            label="Термін виробництва (дні)", border_radius=10
        )
        notes_f = ft.TextField(
            label=self.t("products.field.notes", "Нотатки"), border_radius=10, multiline=True, min_lines=3, max_lines=5
        )
        _existing_urls = [
            (product.get(k) or "") if isinstance(product, dict) else ""
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
                        self._show_snack("Вибраний файл не знайдено", "red")
                        return
                    _temp_paths[slot_idx] = local_path
                    _slot_texts[slot_idx].value = f"Нове: {os.path.basename(local_path)}"
                    with contextlib.suppress(Exception):
                        _slot_texts[slot_idx].update()
                except Exception as ex:
                    logger.exception("Product image picking failed")
                    self._show_snack(f"Помилка вибору фото: {ex}", "red")
            return _pick

        def _make_clear_fn(slot_idx: int):
            def _clear(_=None):
                _temp_paths[slot_idx] = None
                _existing_urls[slot_idx] = ""
                _slot_texts[slot_idx].value = "Не завантажено"
                with contextlib.suppress(Exception):
                    _slot_texts[slot_idx].update()
            return _clear

        sales_prices_container = ft.Column(spacing=6)

        current_region_id = product.get("region_id")
        current_category_id = product.get("category_id")
        current_manager_id = product.get("manager_id") or self.manager_id
        supplier_currency_by_id = {}

        if is_edit:
            name_f.value = product.get("name", "")
            supplier_f.value = product.get("supplier_id")
            packaging_f.value = product.get("packaging_id")
            purchase_price_f.value = str(product.get("purchase_price") or "")
            purchase_currency_f.value = (
                self._normalize_currency_code(product.get("purchase_currency"), "USD")
                if product.get("purchase_currency")
                else ""
            )
            weight_f.value = str(product.get("weight_kg") or "")
            production_term_f.value = str(product.get("production_term_days") or "")
            notes_f.value = product.get("notes") or ""

        def _refresh_purchase_sum_usd(_=None):
            ccy = self._normalize_currency_code(purchase_currency_f.value, "USD")
            price_val = self._to_float_or_none(purchase_price_f.value)
            usd_val = self._convert_to_usd(price_val, ccy)
            show = usd_val is not None and ccy not in {"USD", "$"}
            purchase_sum_usd_f.visible = bool(show)
            purchase_sum_usd_f.value = f"{usd_val:.2f}" if show else ""

        def _get_work_company_id():
            return company_f.value if self.is_superadmin else self.company_id

        def _on_name_change(_):
            error_text.visible = False
            with contextlib.suppress(Exception):
                error_text.update()

        name_f.on_change = _on_name_change

        def _owner_manager_id():
            return manager_f.value if manager_f.visible else (self.manager_id or "")

        async def _allowed_region_ids_for_owner(company_id):
            owner_id = _owner_manager_id()
            if not company_id or not owner_id:
                return set()
            try:
                rows = await asyncio.to_thread(
                    self.db.get_manager_allowed_region_ids, owner_id
                )
                return {rid for rid in (rows or []) if rid}
            except Exception:
                return set()

        async def _categories_for_context(company_id, region_id):
            owner_id = _owner_manager_id()
            if owner_id:
                rows = await asyncio.to_thread(
                    self.db.get_accessible_categories, company_id, owner_id, region_id
                )
            else:
                rows = await asyncio.to_thread(
                    self.db.get_company_categories,
                    company_id,
                    region_id,
                    manager_id=self.manager_id,
                )
            return [r for r in (rows or []) if isinstance(r, dict) and r.get("id")]

        async def _load_managers(company_id):
            if not manager_f.visible:
                return
            _managers_raw = await asyncio.to_thread(
                self.db.get_company_managers, company_id
            )
            rows = [
                m
                for m in _managers_raw
                if isinstance(m, dict) and m.get("id")
            ]
            manager_f.options = [
                ft.dropdown.Option(
                    key=m.get("id"),
                    text=m.get("full_name") or m.get("email") or m.get("id"),
                )
                for m in rows
            ]
            valid_keys = {getattr(opt, "key", None) for opt in manager_f.options}
            if current_manager_id in valid_keys:
                manager_f.value = current_manager_id
            elif self.manager_id in valid_keys:
                manager_f.value = self.manager_id
            elif manager_f.options:
                manager_f.value = manager_f.options[0].key
            else:
                manager_f.value = None

        async def _load_regions(company_id):
            _regions_raw = await asyncio.to_thread(
                self.db.get_company_regions, company_id
            )
            rows = [
                r
                for r in _regions_raw
                if isinstance(r, dict) and r.get("id")
            ]
            allowed_region_ids = await _allowed_region_ids_for_owner(company_id)
            if allowed_region_ids:
                rows = [r for r in rows if r.get("id") in allowed_region_ids]
            region_f.options = [
                ft.dropdown.Option(key=r.get("id"), text=r.get("name")) for r in rows
            ]
            valid_keys = {getattr(opt, "key", None) for opt in region_f.options}
            if current_region_id and current_region_id in valid_keys:
                region_f.value = current_region_id
            elif is_edit and current_region_id:
                # Keep saved region on edit even if it is not visible in current scope.
                saved_label = str(product.get("region_name") or "").strip()
                if not saved_label:
                    saved_label = f"Збережено (нема в списку): {current_region_id}"
                region_f.options = [
                    ft.dropdown.Option(key=current_region_id, text=saved_label)
                ] + list(region_f.options or [])
                region_f.value = current_region_id
            elif region_f.options:
                region_f.value = region_f.options[0].key
            else:
                region_f.value = None

        async def _load_categories(company_id, region_id):
            rows = await _categories_for_context(company_id, region_id)
            category_f.options = [
                ft.dropdown.Option(key=r.get("id"), text=r.get("name")) for r in rows
            ]
            valid_keys = {getattr(opt, "key", None) for opt in category_f.options}
            if current_category_id and current_category_id in valid_keys:
                category_f.value = current_category_id
            elif is_edit and current_category_id:
                # Keep saved category on edit even if it is not visible in current scope.
                saved_label = str(product.get("category_name") or "").strip()
                if not saved_label:
                    saved_label = f"Збережено (нема в списку): {current_category_id}"
                category_f.options = [
                    ft.dropdown.Option(key=current_category_id, text=saved_label)
                ] + list(category_f.options or [])
                category_f.value = current_category_id
            elif category_f.options:
                category_f.value = category_f.options[0].key
            else:
                category_f.value = None

        async def _load_suppliers_packaging(company_id, region_id, keep_supplier=True):
            owner_id = _owner_manager_id()
            prev_supplier = supplier_f.value if keep_supplier else None
            prev_packaging = packaging_f.value

            suppliers = await asyncio.to_thread(
                self.db.get_suppliers, company_id, region_id, manager_id=owner_id or None
            )
            supplier_currency_by_id.clear()
            supplier_currency_by_id.update(
                {
                    s.get("id"): self._normalize_currency_code(s.get("currency"), "USD")
                    for s in (suppliers or [])
                    if isinstance(s, dict) and s.get("id")
                }
            )
            supplier_f.options = [ft.dropdown.Option(key="", text="—")] + [
                ft.dropdown.Option(key=s.get("id"), text=s.get("name"))
                for s in suppliers
                if isinstance(s, dict) and s.get("id")
            ]
            valid_supplier_ids = {
                getattr(opt, "key", None) for opt in supplier_f.options
            }
            if prev_supplier in valid_supplier_ids:
                supplier_f.value = prev_supplier
            elif is_edit and prev_supplier:
                # Do not auto-change saved supplier on edit. Keep the stored value even if it's
                # not present in the visible suppliers list (e.g., access scope changed).
                # Add it as a synthetic option so the dropdown doesn't reset to the first item.
                saved_label = (product.get("supplier_name") or "").strip()
                if not saved_label:
                    saved_label = f"Збережено (нема в списку): {prev_supplier}"
                supplier_f.options = [
                    ft.dropdown.Option(key=prev_supplier, text=saved_label)
                ] + list(supplier_f.options or [])
                supplier_f.value = prev_supplier
            elif supplier_f.options:
                # In edit mode keep empty if missing; in create mode select first real item.
                supplier_f.value = (
                    ""
                    if is_edit
                    else (
                        supplier_f.options[1].key
                        if len(supplier_f.options) > 1
                        else supplier_f.options[0].key
                    )
                )
            else:
                supplier_f.value = None
            if supplier_f.value in supplier_currency_by_id:
                purchase_currency_f.value = self._normalize_currency_code(
                    supplier_currency_by_id.get(supplier_f.value, "USD"),
                    "USD",
                )
            _refresh_purchase_sum_usd()

            packaging_rows = await asyncio.to_thread(
                self.db.get_packaging,
                company_id,
                region_id,
                manager_id=owner_id or None,
            )

            packaging_f.options = [ft.dropdown.Option(key="", text="None")]
            for p in packaging_rows:
                if isinstance(p, dict) and p.get("id"):
                    packaging_f.options.append(
                        ft.dropdown.Option(
                            key=p.get("id"),
                            text=self._packaging_label_with_owner(p, company_id),
                        )
                    )

            valid_packaging_ids = {
                getattr(opt, "key", None) for opt in packaging_f.options
            }
            if prev_packaging in valid_packaging_ids:
                packaging_f.value = prev_packaging
            elif is_edit and prev_packaging:
                saved_label = str(product.get("packaging_name") or "").strip()
                if not saved_label:
                    saved_label = f"Збережено (нема в списку): {prev_packaging}"
                packaging_f.options = [
                    ft.dropdown.Option(key=prev_packaging, text=saved_label)
                ] + list(packaging_f.options or [])
                packaging_f.value = prev_packaging
            else:
                packaging_f.value = ""

        async def _load_sales_prices(region_id):
            sales_prices_container.controls.clear()
            sales_map = {}
            raw_sales = product.get("sales_prices_json") if is_edit else None
            def _add_to_sales_map(row):
                if not isinstance(row, dict):
                    return
                tid = row.get("tier_id")
                price = row.get("price")
                name = row.get("name")
                if tid:
                    sales_map[tid] = price
                if name:
                    sales_map.setdefault(f"name:{name}", price)

            if isinstance(raw_sales, list):
                for row in raw_sales:
                    _add_to_sales_map(row)
            elif isinstance(raw_sales, str) and raw_sales.strip():
                try:
                    parsed = json.loads(raw_sales)
                    if isinstance(parsed, list):
                        for row in parsed:
                            _add_to_sales_map(row)
                except Exception:
                    sales_map = {}

            tiers = await asyncio.to_thread(
                self.db.get_effective_region_price_tiers_for_manager,
                self.company_id,
                self.manager_id,
                region_id,
            )
            for t in tiers:
                if not isinstance(t, dict):
                    continue
                tid = t.get("id")
                tf = ft.TextField(
                    label=f"{t.get('name')} ({t.get('currency')})",
                    border_radius=10,
                    value=(
                        ""
                        if (
                            (v := sales_map.get(tid)) is None
                            and (v := sales_map.get(f"name:{t.get('name')}")) is None
                        )
                        else str(v)
                    ),
                    width=260,
                )
                tf.data = {
                    "tier_id": tid,
                    "name": t.get("name"),
                    "currency": t.get("currency"),
                }
                sales_prices_container.controls.append(tf)

        def _selected_existing_id(level_position):
            field = level_fields[level_position]
            value = field["select"].value
            if value and value != self.NEW_ITEM_KEY:
                return value
            return None

        def _selected_level_name(level_position):
            field = level_fields[level_position]
            if not field["block"].visible:
                return None
            value = field["select"].value
            if value == self.NEW_ITEM_KEY:
                return (field["new"].value or "").strip() or None
            selected = next(
                (row for row in field["rows"] if row.get("id") == value), None
            )
            return (selected or {}).get("name")

        def _sync_level_new_visibility(level_position):
            field = level_fields[level_position]
            field["new"].visible = (
                field["block"].visible and field["select"].value == self.NEW_ITEM_KEY
            )

        async def _rows_for_level(level_position, category_id):
            if not category_id:
                return []
            parent_id = None
            # The immediate parent is the nearest ENABLED level below this
            # position — not necessarily position-1. A category can have
            # disabled levels in between (e.g. Бренд=2, Модель=3 disabled,
            # Серія=4): looking only at level_fields[pos-1] would land on a
            # disabled, invisible level, skip setting parent_id entirely, and
            # end up querying root-level nodes — returning every series
            # across every brand, so switching Бренд never changes what
            # Серія shows (whatever was selected initially stays "valid").
            for prev_pos in range(int(level_position) - 1, 0, -1):
                previous_field = level_fields.get(prev_pos)
                if previous_field and previous_field["block"].visible:
                    parent_id = _selected_existing_id(prev_pos)
                    if not parent_id:
                        return []
                    break
            _nodes_raw = await asyncio.to_thread(
                self.db.get_category_level_nodes,
                category_id, int(level_position), parent_node_id=parent_id
            )
            return self._dedupe_rows_by_id(_nodes_raw)

        async def _extra_rows_for_leaf(category_id, leaf_pos, leaf_parent_id):
            """Fetch level_(leaf_pos+1) children under the selected leaf node."""
            if not category_id or not leaf_parent_id:
                return []
            extra_pos = int(leaf_pos) + 1
            if extra_pos > self.MAX_LEVELS:
                return []
            _nodes_raw = await asyncio.to_thread(
                self.db.get_category_level_nodes,
                category_id, extra_pos, parent_node_id=leaf_parent_id
            )
            return self._dedupe_rows_by_id(_nodes_raw)

        def _refresh_extra_level_options(category_id, leaf_pos, leaf_node_id):
            _extra_level_pos_ref[0] = int(leaf_pos) + 1 if leaf_pos else None
            has_extra = bool(
                leaf_node_id
                and _extra_level_pos_ref[0]
                and _extra_level_pos_ref[0] <= self.MAX_LEVELS
            )
            extra_level_block.visible = has_extra and bool(extra_level_toggle.value)
            _sync_extra_new_visibility()

        async def _refresh_level_options(enabled_levels):
            for level_row in enabled_levels:
                pos = int(level_row.get("position"))
                field = level_fields[pos]
                rows = await _rows_for_level(pos, category_f.value)
                field["rows"] = rows
                previous = field["select"].value
                field["select"].options = [
                    ft.dropdown.Option(
                        key=row.get("id"),
                        text=row.get("name", level_row.get("title") or f"Рівень {pos}"),
                    )
                    for row in rows
                    if isinstance(row, dict) and row.get("id")
                ]
                field["select"].options.append(
                    ft.dropdown.Option(key=self.NEW_ITEM_KEY, text="Створити нове")
                )

                valid_keys = {
                    opt.key
                    for opt in field["select"].options
                    if getattr(opt, "key", None) is not None
                }
                preferred = field.get("initial_id")
                if preferred and preferred in valid_keys:
                    field["select"].value = preferred
                    field["initial_id"] = None
                elif previous in valid_keys:
                    field["select"].value = previous
                elif rows:
                    field["select"].value = rows[0].get("id")
                else:
                    field["select"].value = self.NEW_ITEM_KEY
                _sync_level_new_visibility(pos)
                # Server-driven options+value changes on an already-mounted
                # Dropdown don't always repaint from an ancestor .update()
                # alone (unlike the field the user just clicked themselves,
                # which Flutter already redrew locally) — push this control's
                # own update explicitly so the new list/selection actually
                # shows.
                with contextlib.suppress(Exception):
                    field["select"].update()
                with contextlib.suppress(Exception):
                    field["new"].update()

        async def _on_level_change(level_position, _):
            _sync_level_new_visibility(level_position)
            cid = category_f.value
            company_id = _get_work_company_id()
            region_id = region_f.value
            selected = next(
                (
                    c
                    for c in await _categories_for_context(company_id, region_id)
                    if isinstance(c, dict) and c.get("id") == cid
                ),
                None,
            )
            if not selected:
                return
            cfg = self._category_levels_config(selected)
            enabled_levels = [
                lvl for lvl in cfg.get("levels", []) if lvl.get("enabled")
            ]
            await _refresh_level_options(enabled_levels)
            # Refresh extra level options when last configured level changes
            if enabled_levels and int(level_position) == int(
                enabled_levels[-1].get("position")
            ):
                last_node_id = _selected_existing_id(level_position)
                _refresh_extra_level_options(cid, level_position, last_node_id)
            with contextlib.suppress(Exception):
                hierarchy_container.update()
            with contextlib.suppress(Exception):
                extra_level_block.update()

        async def _rebuild_hierarchy_fields():
            hierarchy_container.controls.clear()
            cid = category_f.value
            company_id = _get_work_company_id()
            region_id = region_f.value
            selected = None
            for c in await _categories_for_context(company_id, region_id):
                if isinstance(c, dict) and c.get("id") == cid:
                    selected = c
                    break

            if not selected:
                hierarchy_info.value = ""
                name_f.label = "Назва товару"
                name_f.disabled = False
                hierarchy_container.controls.append(name_f)
                return

            cfg = self._category_levels_config(selected)
            enabled_levels = [
                lvl for lvl in cfg.get("levels", []) if lvl.get("enabled")
            ]

            use_products = bool(cfg.get("use_products", True))
            level_titles = []
            for pos in range(1, self.MAX_LEVELS + 1):
                level_fields[pos]["block"].visible = False

            for level_row in enabled_levels:
                pos = int(level_row.get("position"))
                title = level_row.get("title") or f"Рівень {pos}"
                level_titles.append(title)
                level_fields[pos]["select"].label = title
                level_fields[pos]["new"].label = f"Нова назва: {str(title).lower()}"
                level_fields[pos]["block"].visible = True

            hierarchy_info.value = (
                "Ієрархія: Категорія -> "
                + (
                    " -> ".join(level_titles)
                    if level_titles
                    else "(без додаткових рівнів)"
                )
                + (" -> Товар" if use_products else " -> Товар=листовий елемент")
            )

            await _refresh_level_options(enabled_levels)

            for level_row in enabled_levels:
                hierarchy_container.controls.append(
                    level_fields[int(level_row.get("position"))]["block"]
                )

            # --- extra depth toggle (always available when there are configured levels) ---
            if enabled_levels:
                last_pos = int(enabled_levels[-1].get("position"))
                last_node_id = _selected_existing_id(last_pos)
                # Compute extra initial from path map (any level beyond last enabled)
                _extra_initial_id_ref[0] = _edit_path_map.get(last_pos + 1)
                _refresh_extra_level_options(cid, last_pos, last_node_id)
                has_extra_initial = bool(_extra_initial_id_ref[0])
                extra_level_toggle.value = has_extra_initial
                extra_level_block.visible = has_extra_initial
                _sync_extra_new_visibility()
                extra_level_toggle.label = f"Додати деталізацію під \"{enabled_levels[-1].get('title') or f'Рівень {last_pos}'}\""
                hierarchy_container.controls.append(extra_level_toggle)
                hierarchy_container.controls.append(extra_level_block)

            if not use_products and enabled_levels:
                if extra_level_toggle.value:
                    extra_level_new.label = "Назва товару (деталізація)"
                    extra_level_new.visible = True
                    name_f.visible = False
                    if is_edit and not (extra_level_new.value or "").strip():
                        extra_level_new.value = str(product.get("name") or "").strip()
                elif is_edit:
                    name_f.label = "Назва серії"
                    name_f.disabled = False
                    name_f.visible = True
                    extra_level_new.visible = False
                else:
                    name_f.label = ""
                    name_f.disabled = True
                    name_f.visible = False
                    extra_level_new.visible = False
            else:
                name_f.label = "Назва товару"
                name_f.disabled = False
                name_f.visible = True
                extra_level_new.visible = False

            hierarchy_container.controls.append(name_f)

        init_guard = {"active": True}

        async def _on_company_change(_, force: bool = False):
            if init_guard["active"] and not force:
                return
            cid = _get_work_company_id()
            await _load_managers(cid)
            await _load_regions(cid)
            await _load_categories(cid, region_f.value)
            await _load_suppliers_packaging(cid, region_f.value, keep_supplier=bool(is_edit))
            await _load_sales_prices(region_f.value)
            await _rebuild_hierarchy_fields()
            with contextlib.suppress(Exception):
                region_f.update()
            with contextlib.suppress(Exception):
                category_f.update()
            with contextlib.suppress(Exception):
                supplier_f.update()
            with contextlib.suppress(Exception):
                packaging_f.update()
            with contextlib.suppress(Exception):
                sales_prices_container.update()
            with contextlib.suppress(Exception):
                hierarchy_container.update()

        async def _on_manager_change(_):
            if init_guard["active"]:
                return
            cid = _get_work_company_id()
            await _load_regions(cid)
            await _load_categories(cid, region_f.value)
            await _load_suppliers_packaging(cid, region_f.value, keep_supplier=False)
            await _load_sales_prices(region_f.value)
            await _rebuild_hierarchy_fields()
            with contextlib.suppress(Exception):
                region_f.update()
            with contextlib.suppress(Exception):
                category_f.update()
            with contextlib.suppress(Exception):
                supplier_f.update()
            with contextlib.suppress(Exception):
                packaging_f.update()
            with contextlib.suppress(Exception):
                sales_prices_container.update()
            with contextlib.suppress(Exception):
                hierarchy_container.update()

        def _make_level_change_handler(current_pos):
            # Flet only awaits a handler when the assigned callable itself is
            # a coroutine function (inspect.iscoroutinefunction) — a plain
            # lambda that merely CALLS an async function returns an
            # unawaited, silently-discarded coroutine. Needs a real `async
            # def` per position (a lambda can't be async), hence this
            # factory instead of the lambda-with-default-arg pattern used
            # for the sync handlers elsewhere in this dialog.
            async def _handler(e):
                await _on_level_change(current_pos, e)

            return _handler

        for pos in range(1, self.MAX_LEVELS + 1):
            # This Flet build fires Dropdown's "on_select" on a user pick, not
            # "on_change" (matches the mode_dd/ccy_dd pattern elsewhere in the
            # app) — on_change-only never fired for a manual selection here,
            # so the next level's options silently never refreshed.
            level_fields[pos]["select"].on_select = _make_level_change_handler(pos)
            level_fields[pos]["select"].on_change = _make_level_change_handler(pos)

        async def _on_region_change(_):
            if init_guard["active"]:
                return
            cid = _get_work_company_id()
            await _load_categories(cid, region_f.value)
            await _load_suppliers_packaging(cid, region_f.value, keep_supplier=False)
            await _load_sales_prices(region_f.value)
            await _rebuild_hierarchy_fields()
            with contextlib.suppress(Exception):
                category_f.update()
            with contextlib.suppress(Exception):
                supplier_f.update()
            with contextlib.suppress(Exception):
                packaging_f.update()
            with contextlib.suppress(Exception):
                sales_prices_container.update()
            with contextlib.suppress(Exception):
                hierarchy_container.update()

        async def _on_category_change(_):
            if init_guard["active"]:
                return
            await _rebuild_hierarchy_fields()
            with contextlib.suppress(Exception):
                hierarchy_container.update()

        async def _on_supplier_change(_):
            if init_guard["active"]:
                return
            cid = _get_work_company_id()
            await _load_suppliers_packaging(cid, region_f.value, keep_supplier=True)
            with contextlib.suppress(Exception):
                packaging_f.update()

        if self.is_superadmin:
            company_f.on_select = _on_company_change
        if manager_f.visible:
            manager_f.on_select = _on_manager_change
        region_f.on_select = _on_region_change
        category_f.on_select = _on_category_change
        supplier_f.on_select = _on_supplier_change
        purchase_price_f.on_change = _refresh_purchase_sum_usd

        quick_copy_block = ft.Column(spacing=5, visible=False)

        def _apply_copy_from(match):
            name_f.value = match.get("name", "")
            purchase_price_f.value = str(match.get("purchase_price") or "")
            purchase_currency_f.value = self._normalize_currency_code(
                match.get("purchase_currency"), "USD"
            )
            weight_f.value = str(match.get("weight_kg") or "")
            production_term_f.value = str(match.get("production_term_days") or "")
            notes_f.value = match.get("notes") or ""
            _refresh_purchase_sum_usd()
            quick_copy_block.controls.clear()
            quick_copy_block.visible = False
            with contextlib.suppress(Exception):
                quick_copy_block.update()

        def _on_name_search(_):
            query = (name_f.value or "").strip()
            if is_edit or len(query) < 2:
                quick_copy_block.controls.clear()
                quick_copy_block.visible = False
                with contextlib.suppress(Exception):
                    quick_copy_block.update()
                return

            if self.is_superadmin:
                matches = self.db.search_products_global(query, 8)
            else:
                matches = self.db.search_products_company(
                    _get_work_company_id(), query, 8
                )

            if not matches:
                quick_copy_block.controls.clear()
                quick_copy_block.visible = False
                with contextlib.suppress(Exception):
                    quick_copy_block.update()
                return

            controls = [ft.Text("Швидке копіювання з існуючих товарів:", size=12)]
            for m in matches:
                controls.append(
                    ft.Row(
                        [
                            ft.Text(m.get("name", ""), size=12, expand=True),
                            ft.ElevatedButton(
                                "Копіювати",
                                icon=ft.Icons.COPY_ALL,
                                on_click=lambda _, row=m: _apply_copy_from(row),
                            ),
                        ],
                        spacing=8,
                    )
                )
            quick_copy_block.controls = controls
            quick_copy_block.visible = True
            with contextlib.suppress(Exception):
                quick_copy_block.update()

        name_f.on_change = _on_name_search

        async def _save(_):
            self._clear_field_errors(company_f, region_f, category_f, manager_f, name_f)
            for pos in range(1, self.MAX_LEVELS + 1):
                self._clear_field_errors(
                    level_fields[pos]["select"], level_fields[pos]["new"]
                )
            current_product_id = str(product.get("id") or "").strip()
            original_catalog_node_id = str(product.get("catalog_node_id") or "").strip()
            company_id = _get_work_company_id()
            region_id = region_f.value
            category_id = category_f.value

            # Auto-fill context fields when possible. Draft creation with empty business fields is allowed.
            if not company_id and self.is_superadmin and company_f.value:
                company_id = company_f.value
            if not region_id and region_f.options:
                region_id = next(
                    (opt.key for opt in region_f.options if getattr(opt, "key", None)),
                    None,
                )
                region_f.value = region_id
            if not category_id and category_f.options:
                category_id = next(
                    (
                        opt.key
                        for opt in category_f.options
                        if getattr(opt, "key", None)
                    ),
                    None,
                )
                category_f.value = category_id

            if not company_id or not region_id or not category_id:
                self._show_snack(
                    "Не вдалося визначити контекст (компанія/регіон/категорія)",
                    "orange",
                )
                return

            category_row = next(
                (
                    c
                    for c in await _categories_for_context(company_id, region_id)
                    if isinstance(c, dict) and c.get("id") == category_id
                ),
                None,
            )
            if not category_row:
                self._show_snack("Не вдалося визначити категорію", "red")
                return

            cfg = self._category_levels_config(category_row)
            enabled_levels = [
                lvl for lvl in cfg.get("levels", []) if lvl.get("enabled")
            ]
            use_products = bool(cfg.get("use_products", True))

            levels_payload = {}
            for level_row in enabled_levels:
                pos = int(level_row.get("position"))
                field = level_fields[pos]
                if not field["block"].visible:
                    continue
                if field["select"].value == self.NEW_ITEM_KEY:
                    lvl_name = (field["new"].value or "").strip()
                    if lvl_name:
                        levels_payload[pos] = {"name": lvl_name}
                else:
                    node_id = field["select"].value
                    if node_id:
                        levels_payload[pos] = {"node_id": node_id}

            # Empty level names are allowed for drafts; missing nodes will be handled below.

            # --- Handle optional extra depth level ---
            extra_pos = _extra_level_pos_ref[0]
            extra_level_name_resolved = None
            if extra_level_toggle.value and extra_pos and extra_pos <= self.MAX_LEVELS:
                extra_text = (extra_level_new.value or name_f.value or "").strip()
                if extra_text:
                    extra_level_name_resolved = extra_text
                    existing_extra_node_id = str(_extra_initial_id_ref[0] or "").strip()
                    if existing_extra_node_id and is_edit:
                        levels_payload[extra_pos] = {"node_id": existing_extra_node_id}
                        with contextlib.suppress(Exception):
                            self.db.update_level_node(
                                existing_extra_node_id, extra_text
                            )
                    else:
                        levels_payload[extra_pos] = {"name": extra_text}

            _cat_id_snap = category_id
            _lvl_snap = dict(levels_payload)
            rel = await asyncio.to_thread(
                lambda: self.db.resolve_or_create_product_levels(_cat_id_snap, _lvl_snap)
            )
            rel = rel or {}

            leaf_position = (
                int(enabled_levels[-1].get("position")) if enabled_levels else None
            )
            leaf_level_name = (
                _selected_level_name(leaf_position) if leaf_position else None
            )
            if not use_products:
                # series-mode: keep the leaf name for create, but allow editing an existing one.
                if extra_level_toggle.value:
                    final_name = (
                        extra_level_name_resolved or (name_f.value or "").strip()
                    )
                elif is_edit:
                    final_name = (
                        (name_f.value or "")
                        or extra_level_name_resolved
                        or leaf_level_name
                    ).strip()
                else:
                    final_name = (
                        extra_level_name_resolved
                        or leaf_level_name
                        or (name_f.value or "")
                    ).strip()
            else:
                final_name = (name_f.value or "").strip()

            if not final_name:
                final_name = f"Чернетка {str(uuid.uuid4())[:8]}"

            # Check for duplicate product name for current user in this region
            normalized_name = final_name.strip().lower()
            _cid_snap = company_id
            _rid_snap = region_id
            existing_products = (
                await asyncio.to_thread(
                    lambda: self.db.get_products(_cid_snap, _rid_snap, manager_id=self.manager_id)
                )
                or []
            )
            new_supplier_id = str(supplier_f.value or "").strip()
            for existing in existing_products:
                if not isinstance(existing, dict):
                    continue
                existing_name = (existing.get("name") or "").strip().lower()
                existing_id = str(existing.get("id") or "").strip()
                # Skip if this is the current product being edited, or if name is different
                if is_edit and existing_id == current_product_id:
                    # This is the product we're editing — skip duplicate check for it
                    continue
                if existing_name != normalized_name:
                    # Different product name — skip
                    continue
                # Same name in same scope — allow if category or supplier differs
                existing_cat = str(existing.get("category_id") or "").strip()
                existing_sup = str(existing.get("supplier_id") or "").strip()
                if existing_cat != str(category_id or "").strip():
                    continue
                if existing_sup != new_supplier_id:
                    continue
                error_text.value = "❌ Товар з такою назвою вже існує"
                error_text.visible = True
                self._mark_required(name_f, self.t("products.err.name_exists", "Товар з такою назвою вже існує"))
                with contextlib.suppress(Exception):
                    error_text.update()
                with contextlib.suppress(Exception):
                    name_f.update()
                return

            # Keep the original leaf/detail node on edit unless the hierarchy was actually changed.
            saved_catalog_node_id = rel.get("leaf_node_id") or original_catalog_node_id
            if is_edit and original_catalog_node_id and not extra_level_toggle.value:
                hierarchy_unchanged = True
                for level_row in enabled_levels:
                    pos = int(level_row.get("position"))
                    selected_id = str(level_fields[pos]["select"].value or "").strip()
                    original_id = str(_edit_path_map.get(pos) or "").strip()
                    if selected_id and original_id and selected_id != original_id:
                        hierarchy_unchanged = False
                        break
                    if selected_id and not original_id:
                        hierarchy_unchanged = False
                        break
                if hierarchy_unchanged:
                    saved_catalog_node_id = original_catalog_node_id

            sales_rows = []
            for ctrl in sales_prices_container.controls:
                if isinstance(ctrl, ft.TextField) and isinstance(
                    getattr(ctrl, "data", None), dict
                ):
                    val = self._to_float_or_none(ctrl.value)
                    if val is None:
                        continue
                    sales_rows.append(
                        {
                            "tier_id": ctrl.data.get("tier_id"),
                            "name": ctrl.data.get("name"),
                            "currency": ctrl.data.get("currency"),
                            "price": val,
                        }
                    )

            payload = {
                "company_id": company_id,
                "region_id": region_id,
                "category_id": category_id,
                "catalog_node_id": saved_catalog_node_id,
                "name": final_name,
                "is_series_mode": bool(not use_products),
                "image": _existing_urls[0] or None,
                "image2": _existing_urls[1] or None,
                "image3": _existing_urls[2] or None,
                # Owner must be the currently active manager context (who is creating now),
                # not an arbitrary manager selected in the dialog.
                "manager_id": (self.manager_id or self.user_data.get("id") or ""),
                "supplier_id": supplier_f.value or None,
                "packaging_id": (packaging_f.value or None) if self.packaging_enabled else None,
                "purchase_price": self._to_float_or_none(purchase_price_f.value),
                "purchase_currency": self._normalize_currency_code(
                    purchase_currency_f.value, "USD"
                ),
                "sales_prices_json": json.dumps(sales_rows, ensure_ascii=False),
                "weight_kg": self._to_float_or_none(weight_f.value),
                "production_term_days": self._to_float_or_none(production_term_f.value),
                "notes": notes_f.value or "",
            }

            if is_edit and not use_products and saved_catalog_node_id:
                new_series_name = (final_name or "").strip()
                current_series_name = (product.get("name") or "").strip()
                if new_series_name and new_series_name != current_series_name:
                    with contextlib.suppress(Exception):
                        self.db.update_level_node(
                            saved_catalog_node_id, new_series_name
                        )

            _img_keys = ("image", "image2", "image3")
            for _si, _img_key in enumerate(_img_keys):
                if _temp_paths[_si]:
                    try:
                        if is_edit and _existing_urls[_si]:
                            self.db.delete_file_by_url(_existing_urls[_si])
                    except Exception:
                        logger.warning("Failed to delete old product image before upload")
                    compressed = self._compress_image(_temp_paths[_si])
                    uploaded_url = self.db.upload_file(
                        "products", compressed, f"product_{uuid.uuid4()}.jpg"
                    )
                    if uploaded_url:
                        payload[_img_key] = uploaded_url
                    if os.path.exists(compressed) and "temp_" in compressed:
                        with contextlib.suppress(Exception):
                            os.remove(compressed)
            _payload_snap = dict(payload)
            if is_edit:
                _pid = product.get("id")
                _lock = edit_lock_token
                ok = await asyncio.to_thread(
                    lambda: self.db.update_product(
                        _pid, _payload_snap,
                        manager_id=self.manager_id,
                        lock_token=_lock,
                    )
                )
            else:
                ok = await asyncio.to_thread(
                    lambda: self.db.add_product(_payload_snap)
                )
            if ok:
                _release_lock_once()
                self._close_dlg(dlg)
                self._show_snack(self.t("products.saved", "Товар збережено"), "green")
                if is_edit and self._products_cache_loaded:
                    # Optimistic in-memory patch — no full reload/DB re-fetch,
                    # so editing one product among 500-1000 stays instant.
                    self._apply_saved_product_to_cache(_pid, _payload_snap)
                    self.load_products()
                else:
                    # New product: we don't get the generated id back from
                    # add_product(), so a real reload is needed to pick it up.
                    self.load_products(force_reload=True)
            else:
                self._show_snack(self.t("products.err.save", "Не вдалося зберегти товар"), "red")

        def _cancel(_):
            _release_lock_once()
            self._close_dlg(dlg)

        def _on_dismiss(_):
            _release_lock_once()

        def _switch_back_to_view(_):
            _release_lock_once()
            # Reuse the SAME already-open dialog object for the view
            # content instead of closing this one and opening a new one -
            # that close-then-reopen choreography raced Flet's dialog
            # close transition no matter how it was timed and could
            # silently leave nothing open at all. on_switch_to_view (see
            # product_quick_view.py's _switch_to_view) takes `dlg` and
            # passes it straight through as existing_dlg.
            _pg2 = self._page()
            if _pg2 and hasattr(_pg2, "run_task"):

                async def _do_switch_back():
                    with contextlib.suppress(Exception):
                        await on_switch_to_view(dlg)

                with contextlib.suppress(Exception):
                    _pg2.run_task(_do_switch_back)
            # else: no page reference available — nothing we can safely
            # schedule the (async) on_switch_to_view callback onto.

        _mode_toggle_edit = (
            ft.Row(
                [
                    ft.OutlinedButton(
                        self.t("products.mode.view", "Перегляд"),
                        on_click=_switch_back_to_view,
                        style=ft.ButtonStyle(
                            padding=ft.padding.symmetric(horizontal=10, vertical=2),
                            text_style=ft.TextStyle(size=11),
                        ),
                        height=28,
                    ),
                    ft.FilledButton(
                        self.t("products.mode.edit", "Редагування"),
                        style=ft.ButtonStyle(
                            padding=ft.padding.symmetric(horizontal=10, vertical=2),
                            text_style=ft.TextStyle(size=11),
                        ),
                        height=28,
                        disabled=True,
                    ),
                ],
                spacing=4,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            if callable(on_switch_to_view)
            else None
        )

        dialog_loading = ft.Container(
            padding=ft.padding.symmetric(vertical=24),
            content=ft.Column(
                [
                    ft.Text("Підготовка форми товару...", color="grey700"),
                    ft.ProgressRing(width=28, height=28, stroke_width=3),
                ],
                spacing=12,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        dialog_form = ft.Container(
            visible=False,
            content=ft.Column(
                [
                    error_text,
                    company_f,
                    region_f,
                    manager_f,
                    category_f,
                    hierarchy_info,
                    hierarchy_container,
                    ft.Row(
                        [
                            ft.Container(width=360, content=supplier_f),
                            ft.Container(width=360, content=packaging_f),
                        ],
                        spacing=10,
                        wrap=True,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                    ft.Row(
                        [
                            purchase_price_f,
                            purchase_currency_f,
                            purchase_sum_usd_f,
                        ],
                        spacing=10,
                        wrap=True,
                    ),
                    ft.Row(
                        [
                            ft.Container(width=260, content=weight_f),
                            ft.Container(width=260, content=production_term_f),
                        ],
                        spacing=10,
                        wrap=True,
                    ),
                    ft.Column(
                        [
                            ft.Text("Фото товару (до 3-х):", size=12, weight="bold"),
                            *[
                                ft.Row(
                                    [
                                        ft.Text(f"Фото {_si + 1}:", size=11, color="grey700", width=55),
                                        ft.IconButton(
                                            ft.Icons.UPLOAD_FILE,
                                            tooltip=f"Завантажити фото {_si + 1}",
                                            icon_size=18,
                                            on_click=lambda _, i=_si: asyncio.create_task(
                                                _make_pick_fn(i)()
                                            ),
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
                    ft.Text("Ціни продажу (по рівнях регіону)", size=12, weight="bold"),
                    sales_prices_container,
                    notes_f,
                ],
                spacing=10,
                scroll=ft.ScrollMode.ADAPTIVE,
                height=self._dialog_content_height(680),
            ),
        )
        save_btn = ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save, disabled=True)

        def _prod_help(_):
            from views.help_utils import show_modal_help
            _pg = self._page()
            show_modal_help(_pg, self.t("products.dlg.edit_title", "Товар"), [
                self.t("products.edit_help.step1", "1. Оберіть компанію, регіон, категорію."),
                self.t("products.edit_help.step2", "2. Введіть назву товару, оберіть постачальника і тип пакування."),
                self.t("products.edit_help.step3", "3. Вкажіть ціну закупівлі, вагу, штрихкод."),
                self.t("products.edit_help.step4", "4. Завантажте фото (до 3-х) кнопкою «Фото»."),
                self.t("products.edit_help.step5", "5. Вкладка «Сумісності» — прив'язка до аксесуарів."),
                self.t("products.edit_help.step6", "6. Вкладка «Розміри» — розміри Xiaomi якщо потрібно."),
                self.t("products.edit_help.step7", "7. Натисніть «Зберегти»."),
            ])

        dlg.title = ft.Row(
            [
                ft.Text(self.t("products.dlg.edit_title", "Товар"), expand=True),
                *([_mode_toggle_edit] if _mode_toggle_edit else []),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_prod_help),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
        )
        dlg.on_dismiss = _on_dismiss
        dlg.content = ft.Container(
            width=760,
            content=ft.Column([dialog_loading, dialog_form], spacing=0),
        )
        dlg.actions = [
            ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_cancel),
            save_btn,
        ]
        if existing_dlg is not None:
            # Same dialog object, already open — just patch what changed,
            # no close/open transition (and no second dialog) at all.
            with contextlib.suppress(Exception):
                dlg.update()
        else:
            self.content_area.page.show_dialog(dlg)

        def _estimate_form_height() -> int:
            """Rough px estimate of the currently-visible fields, so the
            scrollable form gets a height close to its real content instead
            of always reserving the full page-based cap — that static cap
            left a big empty gap under short forms (e.g. a fresh product
            with no compatibility/extra-level data yet)."""
            h = 0
            if (error_text.value or "").strip():
                h += 26
            for fld in (company_f, region_f, manager_f, category_f):
                if fld.visible:
                    h += 62
            h += 22  # hierarchy_info text
            for lvl_field in level_fields.values():
                if lvl_field["block"].visible:
                    h += 62
                    if lvl_field["new"].visible:
                        h += 62
            if extra_level_toggle in hierarchy_container.controls:
                h += 46  # extra_level_toggle row
                if extra_level_block.visible:
                    h += 62
            h += 78  # supplier/packaging row
            h += 78  # purchase price/currency/sum row
            h += 78  # weight/production term row
            h += 22 + 3 * 40  # photo section title + 3 photo rows
            h += 22  # "Ціни продажу" title
            h += max(1, len(sales_prices_container.controls)) * 52
            h += 90  # notes_f (multiline)
            h += 12 * 13  # Column spacing between the ~13 top-level children
            return h

        async def _async_prepare_dialog():
            await asyncio.sleep(0.01)
            try:
                # Initial fill must not reset saved values in edit mode.
                await _on_company_change(None, force=True)
                init_guard["active"] = False
                dialog_loading.visible = False
                dialog_form.visible = True
                save_btn.disabled = False
                with contextlib.suppress(Exception):
                    _estimated = _estimate_form_height()
                    _cap = self._dialog_content_height(680)
                    dialog_form.content.height = max(280, min(_cap, _estimated))
                with contextlib.suppress(Exception):
                    dialog_loading.update()
                with contextlib.suppress(Exception):
                    dialog_form.update()
                with contextlib.suppress(Exception):
                    save_btn.update()
            except Exception:
                logger.exception("Failed to prepare product dialog")

        if self.content_area.page:
            self.content_area.page.run_task(_async_prepare_dialog)

    def archive_product(self, product_id):
        def _confirm(_):
            if self.db.archive_product(product_id, manager_id=self.manager_id):
                self._catalog_config_by_region.clear()
                # Move the row between in-memory caches instead of a full DB
                # re-fetch — load_products() below then just re-filters/renders
                # from the already-warm cache.
                pid = str(product_id)
                moved = self._products_cache.pop(pid, None)
                if moved is not None:
                    self._archived_products_cache = [moved] + list(
                        self._archived_products_cache or []
                    )
                    self._shared_products_map.pop(pid, None)
                    deleted = self._shared_products_map.setdefault(
                        "__deleted_ids__", []
                    )
                    if pid not in deleted:
                        deleted.append(pid)
                    self._bump_products_sync_seq()
                self.load_products()
                self._close_dlg(dlg)
                self._show_snack(self.t("products.archived", "Товар перенесено в архів"), "blue")
            else:
                self._show_snack(self.t("products.err.archive", "Не вдалося архівувати товар"), "red")

        dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text(self.t("products.dlg.archive_confirm", "Перенести товар в архів? Його можна буде відновити.")),
            actions=[
                ft.TextButton("Ні", on_click=lambda _: self._close_dlg(dlg)),
                ft.TextButton("Так", on_click=_confirm),
            ],
        )
        self.content_area.page.show_dialog(dlg)

    def delete_product(self, product_id):
        if not self._is_admin_or_superadmin():
            self._show_snack(
                "Лише Адмін/СуперАдмін може видаляти товари", "orange"
            )
            return

        def _confirm(_):
            ok, msg = self.db.delete_product(self.company_id, product_id)
            self._close_dlg(dlg)
            if ok:
                pid = str(product_id)
                self._catalog_config_by_region.clear()
                self._products_cache.pop(pid, None)
                self._shared_products_map.pop(pid, None)
                deleted = self._shared_products_map.setdefault("__deleted_ids__", [])
                if pid not in deleted:
                    deleted.append(pid)
                self._bump_products_sync_seq()
                self.load_products()
            self._show_snack(msg, "green" if ok else "red")

        dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text(
                "Видалити товар НАЗАВЖДИ? Це неможливо скасувати. Якщо товар "
                "використовується (замовлення/розклад/собівартість/медіаплан) - "
                "видалення відхилиться, скористайтесь архівом."
            ),
            actions=[
                ft.TextButton("Ні", on_click=lambda _: self._close_dlg(dlg)),
                ft.TextButton("Так", on_click=_confirm, style=ft.ButtonStyle(color="red")),
            ],
        )
        self.content_area.page.show_dialog(dlg)

    def restore_product(self, product_id):
        if self.db.restore_product(product_id):
            self._catalog_config_by_region.clear()
            pid = str(product_id)
            restored = None
            remaining = []
            for r in self._archived_products_cache or []:
                if isinstance(r, dict) and str(r.get("id") or "") == pid:
                    restored = r
                else:
                    remaining.append(r)
            self._archived_products_cache = remaining
            if restored is not None:
                self._products_cache[pid] = restored
                deleted = self._shared_products_map.get("__deleted_ids__")
                if isinstance(deleted, list) and pid in deleted:
                    deleted.remove(pid)
                self._sync_product_to_shared_map(pid, restored)
            self.load_products()
            self._show_snack(self.t("products.restored", "Товар відновлено з архіву"), "blue")
        else:
            self._show_snack(self.t("products.err.restore", "Не вдалося відновити товар"), "red")
