import asyncio
import contextlib
import hashlib
import json
import os
import time
import uuid
from datetime import datetime

import flet as ft

import i18n
from config import Config
from database import DataManager
from logger import logger
from views import themes as _themes
from views.calculators_view import open_calculators_modal
from views.notebook_view import get_notebook_badge_counts, open_notebook_modal

# Р’Р°Р¶РєС– view РїС–РґРІР°РЅС‚Р°Р¶СѓСЋС‚СЊСЃСЏ Р»С–РЅРёРІРѕ РІ show_* — С€РІРёРґС€РёР№ СЃС‚Р°СЂС‚ РїС–СЃР»СЏ РІС…РѕРґСѓ.


def _normalize_role(role):
    return (role or "").strip().lower().replace(" ", "")


def _is_admin_or_superadmin(role):
    return _normalize_role(role) in {
        "адмін",
        "админ",
        "admin",
        "суперадмін",
        "суперадмин",
        "superadmin",
    }


ALL_REGIONS_KEY = "__all_regions__"


class MainDashboard:
    def __init__(
        self, page: ft.Page, user_data, on_logout=None, on_switch_account=None
    ):
        self.page = page
        self.page._dashboard = self  # allow child views to trigger tab restarts
        self.db = DataManager()
        self.user_data = user_data if user_data else {}
        self.t = i18n.bind((self.user_data.get("id") or "").strip())
        self.on_logout = on_logout
        self.on_switch_account = on_switch_account
        self.company_id = self.user_data.get("company_id")
        # Route all queries to the company's private schema.
        if self.company_id:
            self.db.set_company(
                self.company_id,
                self.user_data.get("company_name", ""),
                manager_id=self.user_data.get("id", ""),
            )
        self.current_manager_id = self.user_data.get("id")
        self.current_manager_name = self.user_data.get("full_name", "Мої дані")
        # Single-active-device enforcement: the token check_auth stamped for
        # THIS login. _watch_session_kick polls the DB copy and force-logs-out
        # the moment it no longer matches (another device logged into the
        # same account and overwrote it).
        self._my_session_token = str(self.user_data.get("session_token") or "").strip()
        self.visible_managers = []
        self.current_region = None
        self.current_view_func = self.show_home
        self.content_area = ft.Column(
            expand=True,
            scroll=ft.ScrollMode.ADAPTIVE,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        # Global horizontal scroller for all views/tables; scrollbar is rendered
        # in the bottom content block (above workspace tabs footer).
        self._content_hscroll = ft.Row(
            [self.content_area],
            scroll=ft.ScrollMode.HIDDEN,
            spacing=0,
            expand=True,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
        self._last_global_hscroll_offset = 0.0
        self._global_hscroll_offsets_by_view = {}
        self._content_hscroll.on_scroll = self._on_global_horizontal_scroll
        # Refs для динамічних частин хедера (оновлюються без перебудови)
        self._region_buttons_row = ft.Row(spacing=5)
        self._manager_buttons_row = ft.Row(spacing=5, wrap=True)
        self._layout_built = False  # будуємо layout лише один раз
        self._view_sticky_header = ft.Column([], spacing=0, tight=True)
        self._sync_poll_started = False
        self._gsm_tg_flush_loop_started = False
        self._sync_last_pulses = {}
        self._sync_dirty_scopes = set()
        self._on_demand_synced_keys = set()
        self._workspace_tabs_row = ft.Row(
            spacing=6,
            run_spacing=6,
            wrap=True,
            expand=True,
        )
        # Slot for per-view pinned horizontal scrollbar (populated by views like orders).
        self._view_h_scrollbar_slot = ft.Column([], spacing=0, tight=True)
        self._workspace_state_loaded = False
        self._workspaces = []
        # True в†’ load own orders; False в†’ force "Загальні" mode (load from selected subordinates).
        self._work_orders_mode: bool = True
        # "Загальні" mode — combines data from selected subordinates
        self.current_general_mode: bool = False
        self.current_general_manager_ids: list[str] = []  # selected subordinate IDs
        self._all_subordinates_flat: list[dict] = (
            []
        )  # full recursive tree (loaded once)
        self._active_workspace_id = ""
        self._workspace_last_saved_text = ""
        self._sidebar_visible = False
        self._sidebar_container = None
        self._sidebar_col_ref = None
        self._sidebar_toggle_btn = None
        self._live_home_view_ref = None
        self._live_orders_view_ref = None
        self._preloaded_orders: list | None = None
        self._live_cost_price_orders_view_ref = None
        self._live_payments_view_ref = None
        self._live_pack_stock_view_ref = None
        self._live_cost_price_stock_view_ref = None
        self._live_media_plan_view_ref = None
        self._live_schedule_view_ref = None
        self._live_products_view_ref = None
        self._last_sync_pulse_view_reload_mono = 0.0
        self._pending_sync_pulse_view_reload = False
        self._has_remote_updates_pending = False
        self._last_remote_updates_notice_mono = 0.0
        self._logout_in_progress = False
        self._view_switch_intent_depth = 0
        self._calc_btn_ref: ft.Container | None = None
        self._notebook_btn_ref: ft.Container | None = None
        self._notebook_badge_today_cont: ft.Container | None = None
        self._notebook_badge_overdue_cont: ft.Container | None = None
        # Tab caching — visibility panel approach
        self._view_cache: dict[str, dict] = {}
        self._cached_view_wrappers: dict[str, ft.Column] = {}
        # Shared orders map — single source of truth for cross-tab sync
        self._shared_orders_map: dict[str, dict] = {}
        # Shared products map — same idea, for the Товари tab's cross-tab live sync
        self._shared_products_map: dict[str, dict] = {}
        self._setup_shared_map_notifier()
        self._scratch_col = ft.Column(
            expand=True,
            visible=True,
            scroll=ft.ScrollMode.ADAPTIVE,
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        self.content_area.controls.append(self._scratch_col)
        # Page zoom state (Ctrl+Scroll on desktop, pinch on mobile)
        self._zoom_factor: float = 1.0
        self._zoom_steps = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 1.00, 1.10, 1.15, 1.25, 1.50, 2.00]
        self._zoom_min = 0.35
        self._zoom_max = 2.00
        self._zoomable: ft.Container | None = None
        self._scaled_content: ft.Container | None = None
        self._main_layout_ref: ft.Row | None = None
        self._zoom_h_scroller: ft.Row | None = None
        self._zoom_by_vkey: dict[str, float] = {}  # per-tab zoom, cleared on app close
        self._mobile_default_zoom_applied: bool = False
        self._mobile_pinch_start_factor: float = 1.0
        self._search_hl_ctrl: ft.Control | None = None
        self._search_hl_saves: tuple | None = None  # (ctrl, orig_bgcolor, orig_color)
        self._search_hl_all_saves: list = (
            []
        )  # [(ctrl, orig_bg, orig_color), ...] for all matches
        self._home_last_refresh_mono: float = 0.0
        self._zoom_indicator_text: ft.Text | None = None
        self._zoom_indicator: ft.Container | None = None
        self._zoom_indicator_seq: int = 0
        self._zoom_apply_seq: int = 0

    def _with_view_switch_intent(self, fn):
        self._view_switch_intent_depth += 1
        try:
            return fn()
        finally:
            self._view_switch_intent_depth = max(0, self._view_switch_intent_depth - 1)

    def _defer_view_initial_load(self, view_ref, delay: float = 0.06) -> None:
        """If a view started an initial background task at build time, cancel it
        and schedule a slightly-delayed run to avoid blocking UI during tab switch.
        """
        if view_ref is None:
            return
        page = getattr(self, "page", None)

        try:
            task = getattr(view_ref, "_initial_load_task", None)
            if task is not None:
                with contextlib.suppress(Exception):
                    task.cancel()
                try:
                    view_ref._initial_load_task = None
                except Exception:
                    pass
        except Exception:
            pass

        if not hasattr(view_ref, "_async_initial_load"):
            return

        async def _delayed():
            import asyncio

            await asyncio.sleep(delay)
            try:
                await view_ref._async_initial_load()
            except Exception:
                try:
                    logger.exception("Deferred initial load failed")
                except Exception:
                    pass

        if page and hasattr(page, "run_task"):
            try:
                page.run_task(_delayed)
                return
            except Exception:
                pass
        try:
            import asyncio

            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_delayed())
                return
        except Exception:
            pass
        try:
            import threading

            threading.Thread(target=lambda: None).start()
        except Exception:
            pass

    # Maps _active_view name → _view_cache key for pending-flush lookup.
    _VIEW_NAME_TO_CACHE_KEY: dict[str, str] = {
        "show_home": "home",
        "show_orders": "orders",
        "show_payments": "payments",
        "show_cost_price_orders": "cost_price_orders",
        "show_cost_price_stock": "cost_price_stock",
        "show_pack_stock": "pack_stock",
        "show_schedule": "schedule",
        "show_media_plan": "media_plan",
        "show_products": "products",
        "show_categories_brands": "categories",
        "show_suppliers": "suppliers",
        "show_packaging": "packaging",
        "show_files": "files",
    }

    async def _flush_leaving_view_async(self, leaving_view: str) -> None:
        """Flush unsaved outbox of the view the user is leaving."""
        try:
            cache_key = self._VIEW_NAME_TO_CACHE_KEY.get(leaving_view)
            if not cache_key:
                return
            # Try workspace-specific key first (orders/payments/cost_price_orders),
            # then fall back to the global key (home, pack_stock, etc.).
            _ws_key = f"{cache_key}_{self._active_workspace_id}"
            inst = (
                self._view_cache.get(_ws_key) or self._view_cache.get(cache_key) or {}
            ).get("instance")
            if inst is None:
                return
            if hasattr(inst, "flush_pending_async"):
                await inst.flush_pending_async()
        except Exception:
            pass

    def _is_feature_enabled(self, feature_key: str) -> bool:
        """Live check (no caching) so a superadmin toggle takes effect
        immediately, and impersonation is respected."""
        from feature_flags import TOGGLEABLE_FEATURES

        if feature_key not in TOGGLEABLE_FEATURES:
            return True
        try:
            mgr_id = self.current_manager_id or self.user_data.get("id")
            return bool(
                self.db.get_effective_features(mgr_id, self.company_id).get(
                    feature_key, True
                )
            )
        except Exception:
            return True

    def _can_enter_view(self, target_view: str, e=None) -> bool:
        target = str(target_view or "").strip()
        if target:
            _sc = getattr(self.db, "_active_company_schema", "") or "public"
            logger.info("[TAB] %s  |  schema: %s", target, _sc)
        if not target:
            logger.info("[view-switch-debug] allow empty target")
            return True
        current = ""
        try:
            current = str(getattr(self.page, "_active_view", "") or "").strip()
        except Exception:
            current = ""
        # First render or same-view refresh are always allowed.
        if not current or current == target:
            logger.info(
                "[view-switch-debug] allow same/first | current=%s | target=%s",
                current,
                target,
            )
            return True
        # Explicit user intent paths.
        if e is not None or self._view_switch_intent_depth > 0:
            logger.info(
                "[view-switch-debug] allow explicit intent | current=%s | target=%s | has_event=%s | intent_depth=%s",
                current,
                target,
                bool(e is not None),
                self._view_switch_intent_depth,
            )
            # Flush unsaved data of the view being left (non-blocking background task).
            if current and current != target:
                with contextlib.suppress(Exception):
                    self.page.run_task(self._flush_leaving_view_async, current)
            return True
        logger.debug(
            "[view-switch] blocked unexpected transition | from=%s | to=%s | user_id=%s",
            current,
            target,
            self.user_data.get("id"),
        )
        return False

    def _setup_shared_map_notifier(self) -> None:
        """Register __on_change__ in shared orders map so any view that bumps
        the sync-seq automatically triggers resume() on all other live views.
        Debounced to at most once per 0.8s to avoid flooding the event loop."""
        import time

        _last_ts = [0.0]
        _live_refs = [
            "_live_home_view_ref",
            "_live_orders_view_ref",
            "_live_cost_price_orders_view_ref",
            "_live_payments_view_ref",
            "_live_cost_price_stock_view_ref",
            "_live_pack_stock_view_ref",
            "_live_media_plan_view_ref",
            "_live_schedule_view_ref",
        ]
        # Map live-ref attribute name → page._active_view key.
        # The currently active view already applies edits in-place; calling
        # resume() on it from _on_orders_changed would trigger an unnecessary
        # background DB-pulse check which can cause a full _async_initial_load.
        _ref_to_view_key = {
            "_live_home_view_ref": "show_home",
            "_live_orders_view_ref": "show_orders",
            "_live_cost_price_orders_view_ref": "show_cost_price_orders",
            "_live_payments_view_ref": "show_payments",
            "_live_cost_price_stock_view_ref": "show_cost_price_stock",
            "_live_pack_stock_view_ref": "show_pack_stock",
            "_live_media_plan_view_ref": "show_media_plan",
            "_live_schedule_view_ref": "show_schedule",
        }

        def _on_orders_changed():
            now = time.time()
            if now - _last_ts[0] < 0.8:
                return
            _last_ts[0] = now
            active_key = str(getattr(self.page, "_active_view", "") or "").strip()
            for _ref in _live_refs:
                _view = getattr(self, _ref, None)
                if _view is None or not hasattr(_view, "resume"):
                    continue
                # Skip the currently active view — it already handles its own
                # in-place updates and doesn't need a resume() that could
                # accidentally trigger a full reload via the pulse check.
                if active_key and _ref_to_view_key.get(_ref) == active_key:
                    continue
                try:
                    self.page.run_task(_view.resume)
                except Exception:
                    pass

        self._shared_orders_map["__on_change__"] = _on_orders_changed

    def _active_view_key(self) -> str:
        try:
            pg_view = str(getattr(self.page, "_active_view", "") or "").strip()
            if pg_view:
                return pg_view
        except Exception:
            pass
        try:
            return str(getattr(self.current_view_func, "__name__", "") or "").strip()
        except Exception:
            return ""

    def _remember_global_hscroll_for_current_view(self) -> None:
        key = self._active_view_key()
        if not key:
            return
        try:
            self._global_hscroll_offsets_by_view[key] = float(
                self._last_global_hscroll_offset or 0.0
            )
        except Exception:
            pass

    def _on_global_horizontal_scroll(self, e) -> None:
        try:
            px = float(getattr(e, "pixels", 0.0) or 0.0)
            self._last_global_hscroll_offset = px
            key = self._active_view_key()
            if key:
                self._global_hscroll_offsets_by_view[key] = px
        except Exception:
            pass

    async def _restore_global_hscroll_for_view(self, view_key: str) -> None:
        key = str(view_key or "").strip()
        if not key:
            return
        target = float(self._global_hscroll_offsets_by_view.get(key, 0.0) or 0.0)
        if abs(target) <= 0.5:
            return
        for delay in (0.01, 0.05, 0.12, 0.22):
            await asyncio.sleep(delay)
            try:
                await self._content_hscroll.scroll_to(offset=target, duration=0)
            except Exception:
                continue

    def _schedule_restore_global_hscroll(self, view_key: str) -> None:
        key = str(view_key or "").strip()
        if not key:
            return
        try:
            self.page.run_task(self._restore_global_hscroll_for_view, key)
        except Exception:
            pass

    def _sync_scopes(self):
        return [
            "catalog",
            "suppliers",
            "packaging",
            "products",
            "orders",
            "pricing",
            "extensions",
            "company",
        ]

    def _consume_scope_dirty(self, scope):
        """Check and consume a dirty scope flag."""
        if scope in self._sync_dirty_scopes:
            self._sync_dirty_scopes.discard(scope)
            return True
        return False

    def _mark_remote_updates(self, changed_scopes) -> None:
        scopes = set(changed_scopes or set())
        if not scopes:
            return
        self._sync_dirty_scopes.update(scopes)
        self._has_remote_updates_pending = True
        try:
            prev = set(
                getattr(self.page, "_sync_remote_changes_pending_scopes", set())
                or set()
            )
            self.page._sync_remote_changes_pending_scopes = set(prev | scopes)
        except Exception:
            pass

        # Lightweight marker: no auto-reload, just notify that fresh data exists.
        now = time.monotonic()
        if now - float(self._last_remote_updates_notice_mono or 0.0) < 10.0:
            return
        self._last_remote_updates_notice_mono = now
        with contextlib.suppress(Exception):
            self.page.snack_bar = ft.SnackBar(
                content=ft.Text(
                    "Є нові дані в БД. Оновлення застосуються при вході на вкладку.",
                    color="#FFFFFF",
                    weight="bold",
                ),
                bgcolor="#1D4ED8",
                duration=2500,
            )
            self.page.snack_bar.open = True
            self.page.update()

    async def _background_warmup(self):
        # Delay warmup to let the UI render and avoid competing CPU/IO at startup.
        await asyncio.sleep(1.0)
        _wu_t0 = time.monotonic()
        logger.info("[bg-warmup] СТАРТ (після 1с затримки)")
        try:
            company_id = self.company_id
            manager_id = self.current_manager_id or self.user_data.get("id")
            region_id = self.current_region
            # All DB calls run in a thread so they never block the asyncio event loop.
            _t = time.monotonic()
            await asyncio.to_thread(self.db.get_all_companies)
            logger.info(f"[bg-warmup] get_all_companies {time.monotonic()-_t:.3f}s")
            if company_id:
                _t = time.monotonic()
                await asyncio.to_thread(self.db.get_company_managers, company_id)
                logger.info(
                    f"[bg-warmup] get_company_managers {time.monotonic()-_t:.3f}s"
                )
                _t = time.monotonic()
                await asyncio.to_thread(self.db.get_company_regions, company_id)
                logger.info(
                    f"[bg-warmup] get_company_regions {time.monotonic()-_t:.3f}s"
                )
                _t = time.monotonic()
                await asyncio.to_thread(
                    self.db.get_accessible_categories, company_id, manager_id, region_id
                )
                logger.info(
                    f"[bg-warmup] get_accessible_categories {time.monotonic()-_t:.3f}s"
                )
                _t = time.monotonic()
                await asyncio.to_thread(
                    self.db.get_suppliers, company_id, region_id, manager_id=manager_id
                )
                logger.info(f"[bg-warmup] get_suppliers {time.monotonic()-_t:.3f}s")
                _t = time.monotonic()
                await asyncio.to_thread(
                    self.db.get_packaging, company_id, region_id, manager_id=manager_id
                )
                logger.info(f"[bg-warmup] get_packaging {time.monotonic()-_t:.3f}s")
                # Deferred: sync_packaging_supply_contexts is heavy — PackagingStock builds sync on demand.
                _t = time.monotonic()
                await asyncio.to_thread(
                    self.db.get_products, company_id, region_id, manager_id=manager_id
                )
                logger.info(f"[bg-warmup] get_products {time.monotonic()-_t:.3f}s")
                _t = time.monotonic()
                await asyncio.to_thread(
                    self.db.get_company_delivery_methods, company_id
                )
                logger.info(
                    f"[bg-warmup] get_company_delivery_methods {time.monotonic()-_t:.3f}s"
                )
                # Low-priority Orders prefetch: keep very small snapshot only.
                # Reduce size to avoid long DB scans during app startup.
                try:
                    logger.info("[bg-warmup] get_orders(limit=80) СТАРТ")
                    _t = time.monotonic()
                    prefetched_orders = (
                        await asyncio.to_thread(
                            self.db.get_orders,
                            company_id,
                            region_id,
                            limit=80,
                            offset=0,
                            manager_id=manager_id,
                        )
                        or []
                    )
                    logger.info(
                        f"[bg-warmup] get_orders(limit=80) {time.monotonic()-_t:.3f}s → {len(prefetched_orders)} рядків"
                    )
                except Exception:
                    prefetched_orders = []
                    logger.warning(
                        "[bg-warmup] get_orders ПОМИЛКА (prefetch пропущено)"
                    )
                try:
                    setattr(
                        self.page,
                        "_orders_prefetch_cache",
                        {
                            "company_id": str(company_id or "").strip(),
                            "manager_id": str(manager_id or "").strip(),
                            "region_id": str(region_id or "").strip(),
                            "rows": [
                                dict(r)
                                for r in prefetched_orders
                                if isinstance(r, dict)
                            ],
                            "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
                        },
                    )
                except Exception:
                    pass
            logger.info(f"[bg-warmup] ЗАВЕРШЕНО за {time.monotonic()-_wu_t0:.3f}s")
        except Exception:
            logger.exception("Dashboard warmup failed")

    def _sync_snapshot_file(self):
        company_id = (self.company_id or "").strip()
        manager_id = (self.user_data.get("id") or "").strip()
        seed = f"{company_id}:{manager_id}".encode("utf-8")
        suffix = hashlib.sha1(seed).hexdigest()[:12]
        return os.path.join(self._app_data_dir(), f"sync_pulses_main_{suffix}.json")

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

    def _active_scopes_for_current_view(self):
        try:
            view_name = getattr(self.current_view_func, "__name__", "")
            if view_name == "show_products":
                return ["products", "packaging", "suppliers", "catalog", "pricing"]
            if view_name == "show_categories_brands":
                return ["catalog"]
            if view_name == "show_suppliers":
                return ["suppliers", "catalog"]
            if view_name == "show_packaging":
                return ["packaging", "suppliers"]
            if view_name == "show_orders":
                return ["orders", "payments", "products", "suppliers", "packaging"]
            if view_name == "show_payments":
                return ["orders", "payments", "products", "suppliers", "packaging"]
            if view_name == "show_cost_price_orders":
                return [
                    "orders",
                    "payments",
                    "products",
                    "suppliers",
                    "packaging",
                    "pricing",
                ]
            if view_name == "show_cost_price_stock":
                return ["products", "suppliers", "packaging", "pricing", "catalog"]
            if view_name == "show_pack_stock":
                return ["orders", "payments", "packaging", "products", "suppliers"]
            if view_name == "show_schedule":
                return ["orders"]
            if view_name == "show_media_plan":
                return ["orders", "products"]
            return []
        except Exception:
            return []

    def _can_auto_reload_current_view(self) -> bool:
        try:
            view_name = getattr(self.current_view_func, "__name__", "")
            # Orders screen supports inline/background edits and should not be
            # force-rebuilt by sync pulses.
            if view_name == "show_orders":
                return False
            # Склад пакування: оновлення через apply_live_scope_sync → reload(), без повного clear+build.
            if view_name == "show_pack_stock":
                return False
            return True
        except Exception:
            return True

    def _get_shared_orders(self):
        """Return orders from shared_orders_map — the single source of truth.

        shared_orders_map is updated immediately on every field edit (via
        _sync_order_to_shared_map), so home view counters always reflect the
        latest state without waiting for a DB flush.
        """
        if self._shared_orders_map:
            return [v for v in self._shared_orders_map.values() if isinstance(v, dict)]
        # Fallback when shared map is empty (before first tab opened):
        # search any workspace-specific or legacy orders cache slot.
        _ov = None
        for _k, _v in self._view_cache.items():
            if _k == "orders" or _k.startswith("orders_"):
                _ov = (_v or {}).get("instance")
                if _ov is not None:
                    break
        if _ov is not None:
            _orders = getattr(_ov, "_orders", None)
            if _orders:
                return list(_orders)
        if self._preloaded_orders:
            return list(self._preloaded_orders)
        return None

    def _ensure_own_preloaded_sync(self) -> None:
        """Synchronously load supervisor's snapshot from disk if not already in memory.

        Called before entering general mode so REUSE CACHE has data on the first render.
        Disk read only — no DB query, completes in <50ms typically.
        """
        if self._preloaded_orders:
            return
        try:
            import os as _os

            own_id = str(self.user_data.get("id") or "").strip()
            from views.async_view_build_helper import (
                _load_snapshot_orders,
                _orders_snapshot_path,
            )

            company_id = str(self.company_id or "").strip()
            if not own_id or not company_id:
                return
            region_id = self._current_region_for_query()
            app_data = _os.getenv("FLET_APP_STORAGE_DATA") or _os.path.join(
                _os.getenv("APPDATA") or _os.path.expanduser("~"), "CatManPro"
            )
            path = _orders_snapshot_path(app_data, company_id, own_id, region_id)
            orders = _load_snapshot_orders(path)
            # If specific-region snapshot empty/missing, try "all regions" snapshot
            if not orders and region_id:
                path_all = _orders_snapshot_path(app_data, company_id, own_id, None)
                orders_all = _load_snapshot_orders(path_all)
                if orders_all:
                    # Filter to this region
                    orders = [
                        o
                        for o in orders_all
                        if isinstance(o, dict)
                        and str(o.get("region_id") or "").strip() == region_id
                    ] or orders_all
            if orders:
                self._preloaded_orders = orders
                logger.info(
                    "[DIAG][ensure_own_preloaded] loaded %d orders from disk snapshot",
                    len(orders),
                )
        except Exception:
            pass

    async def _preload_home_orders_bg(self):
        """Load orders at startup so home view has correct data before orders tab is visited.
        Uses orders_view's disk snapshot (fast) then refreshes from DB in background."""
        import time as _time

        _start_ts = _time.monotonic()
        await asyncio.sleep(0.4)
        try:
            from views.orders_view import OrdersView

            _region = self._current_region_for_query()
            ov = OrdersView(
                ft.Column(),
                self._build_context_user(),
                self.db,
                _region,
            )
            # Load lookups FIRST so _effective_region_id() resolves the display name
            # to its UUID — without this, _orders_snapshot_file() uses "all" as the
            # region key regardless of which region is actually selected, causing
            # the wrong snapshot to be loaded.
            await asyncio.to_thread(ov._load_lookups)
            loaded = await asyncio.to_thread(ov._load_orders_snapshot_cache)
            # Pulse-check: if snapshot exists but DB has newer data (e.g. edits from
            # another device), discard it and reload from DB.
            if loaded and ov._orders:
                try:
                    db_pulse = await asyncio.to_thread(ov._orders_scope_pulse_ts)
                    if db_pulse > int(getattr(ov, "_orders_snapshot_pulse_ts", 0) or 0):
                        loaded = False
                        ov._orders = []
                        logger.info(
                            "[home-preload] snapshot stale (db_pulse=%d > snap=%d) — reloading from DB",
                            db_pulse,
                            ov._orders_snapshot_pulse_ts,
                        )
                except Exception:
                    pass
            if not loaded or not ov._orders:
                await asyncio.to_thread(ov._load_orders, True)
            logger.info(
                "[home-preload] region=%r snapshot=%s orders=%d",
                _region,
                loaded,
                len(ov._orders or []),
            )
            if ov._orders:
                self._preloaded_orders = list(ov._orders)
                # Only rebuild home if its last successful build completed BEFORE this
                # preload started — meaning it rendered with stale/empty data and now
                # has fresh orders to show. If home was already built after we started
                # (e.g. triggered by a region change), skip the redundant second render.
                _home_inst = (self._view_cache.get("home") or {}).get("instance")
                if _home_inst is not None:
                    _home_inst._live_orders_snapshot = list(self._preloaded_orders)
                    _built_ts = float(
                        getattr(_home_inst, "_build_completed_ts", 0.0) or 0.0
                    )
                    if _built_ts < _start_ts:
                        with contextlib.suppress(Exception):
                            self.page.run_task(_home_inst.resume)
        except Exception:
            logger.exception("[home-preload] failed")

    async def _startup_build_home(self):
        """Load orders data (snapshot or DB), then show home exactly once — no double render."""
        try:
            from views.orders_view import OrdersView

            _region = self._current_region_for_query()
            ov = OrdersView(
                ft.Column(),
                self._build_context_user(),
                self.db,
                _region,
            )
            # Load lookups FIRST so _effective_region_id() resolves the display name
            # to its UUID — without this, _orders_snapshot_file() falls back to "all".
            await asyncio.to_thread(ov._load_lookups)
            loaded = await asyncio.to_thread(ov._load_orders_snapshot_cache)
            # Pulse-check: discard stale snapshot so counters are correct on first load.
            if loaded and ov._orders:
                try:
                    db_pulse = await asyncio.to_thread(ov._orders_scope_pulse_ts)
                    if db_pulse > int(getattr(ov, "_orders_snapshot_pulse_ts", 0) or 0):
                        loaded = False
                        ov._orders = []
                        logger.info(
                            "[home-startup] snapshot stale (db_pulse=%d > snap=%d) — reloading from DB",
                            db_pulse,
                            getattr(ov, "_orders_snapshot_pulse_ts", 0),
                        )
                except Exception:
                    pass
            if not loaded or not ov._orders:
                await asyncio.to_thread(ov._load_orders, True)
            logger.info(
                "[home-startup] region=%r snapshot=%s orders=%d",
                _region,
                loaded,
                len(ov._orders or []),
            )
            if ov._orders:
                self._preloaded_orders = list(ov._orders)
        except Exception:
            logger.exception("[home-startup] failed")

        # Show home once with data already in _preloaded_orders.
        # Must go through _with_view_switch_intent — by the time this async
        # preload finishes, page._active_view may already have been set to a
        # restored workspace's view, and a bare current_view_func(None) call
        # (e=None, no intent) gets silently blocked by _can_enter_view, which
        # left the Home tab rendered empty (matches every other programmatic
        # current_view_func(None) call site — see _close_workspace etc.).
        with contextlib.suppress(Exception):
            self._with_view_switch_intent(lambda: self.current_view_func(None))
            self.page.update()

    def _detach_live_mirror_views_for(self, keep: str | None):
        k = (keep or "").strip()

        def _is_inst_cached(inst) -> bool:
            return any(
                isinstance(v, dict) and v.get("instance") is inst
                for v in self._view_cache.values()
            )

        if k != "home":
            _hv = getattr(self, "_live_home_view_ref", None)
            # Don't stop cached views — they stay alive for instant tab switching
            if _hv is not None and "home" not in self._view_cache:
                if hasattr(_hv, "stop"):
                    with contextlib.suppress(Exception):
                        _hv.stop()
                self._live_home_view_ref = None
        if k != "orders":
            _ov = getattr(self, "_live_orders_view_ref", None)
            if _ov is not None:
                if _is_inst_cached(_ov):
                    # Cached views skip the stop() teardown below (their
                    # background loops keep running for instant tab
                    # switching) — but that also means stop()'s "flush outbox
                    # + persist snapshot to disk" safety net never runs when
                    # leaving a cached orders tab. A newly created/edited
                    # order that's still only in memory + the outbox (DB
                    # write pending, or racing the debounced snapshot write)
                    # would then sit unpersisted on disk until the next full
                    # app restart. Run just that persistence step here.
                    if hasattr(_ov, "_background_sync_on_leave"):
                        with contextlib.suppress(Exception):
                            self.page.run_task(_ov._background_sync_on_leave)
                else:
                    if hasattr(_ov, "stop"):
                        with contextlib.suppress(Exception):
                            _ov.stop()
                    self._live_orders_view_ref = None
        if k != "cost_price_orders":
            _cpov = getattr(self, "_live_cost_price_orders_view_ref", None)
            if _cpov is not None and not _is_inst_cached(_cpov):
                if hasattr(_cpov, "stop"):
                    with contextlib.suppress(Exception):
                        _cpov.stop()
                self._live_cost_price_orders_view_ref = None
        if k != "payments":
            _pv = getattr(self, "_live_payments_view_ref", None)
            if _pv is not None and not _is_inst_cached(_pv):
                self._live_payments_view_ref = None
        if k != "pack_stock":
            if "pack_stock" not in self._view_cache:
                self._live_pack_stock_view_ref = None
        if k != "cost_price_stock":
            if "cost_price_stock" not in self._view_cache:
                self._live_cost_price_stock_view_ref = None
        if k != "media_plan":
            if "media_plan" not in self._view_cache:
                self._live_media_plan_view_ref = None
        if k != "schedule":
            if "schedule" not in self._view_cache:
                self._live_schedule_view_ref = None
        if k != "products":
            if "products" not in self._view_cache:
                self._live_products_view_ref = None

    def _invalidate_context_cache(
        self, keep_preloaded_orders: bool = False, preserve_keys: frozenset = frozenset()
    ) -> None:
        """Clear all region/manager-sensitive view caches so the next tab visit rebuilds fresh.

        preserve_keys: cache keys to leave untouched — for views (like "files")
        that have their own cheap in-place update path (e.g. set_region()) and
        don't need a full teardown+rebuild on every region click. Wiping them
        anyway forces a full rebuild through the generic show_X() cold-start
        path, which is slower and — if anything in that path throws — leaves
        the tab's wrapper cleared with nothing to replace it (blank screen).
        """
        # Cancel any in-progress async builds to avoid race conditions
        # (e.g., switching from "Загальні" to "Мої дані" while the general build is running).
        for live_ref in [
            self._live_home_view_ref,
            self._live_orders_view_ref,
            self._live_cost_price_orders_view_ref,
            self._live_payments_view_ref,
            self._live_pack_stock_view_ref,
            self._live_cost_price_stock_view_ref,
            self._live_media_plan_view_ref,
            self._live_schedule_view_ref,
        ]:
            if live_ref is not None:
                with contextlib.suppress(Exception):
                    live_ref._async_build_token = (
                        int(getattr(live_ref, "_async_build_token", 0) or 0) + 999
                    )
        for key, wrapper in list(self._cached_view_wrappers.items()):
            if key in preserve_keys:
                continue
            with contextlib.suppress(Exception):
                wrapper.controls.clear()
                wrapper.visible = False
            # Physically remove from content_area so the old wrapper can't re-appear
            # when a stale async build writes content to it after cache clear.
            with contextlib.suppress(Exception):
                if wrapper in self.content_area.controls:
                    self.content_area.controls.remove(wrapper)
            del self._cached_view_wrappers[key]
            self._view_cache.pop(key, None)
        if not keep_preloaded_orders:
            self._preloaded_orders = None
        self._live_home_view_ref = None
        self._live_orders_view_ref = None
        self._live_cost_price_orders_view_ref = None
        self._live_payments_view_ref = None
        self._live_pack_stock_view_ref = None
        self._live_cost_price_stock_view_ref = None
        self._live_media_plan_view_ref = None
        self._live_schedule_view_ref = None
        self._live_products_view_ref = None

    def _get_or_create_cached_wrapper(self, key: str) -> ft.Column:
        if key not in self._cached_view_wrappers:
            col = ft.Column(
                expand=True,
                visible=False,
                # gsm_parser builds its own internal header/body scroll split
                # (table header stays put, only the device rows scroll) — that
                # only works if this wrapper does NOT also scroll, otherwise
                # it swallows the inner split and drags everything together.
                # _activate_cached_view() toggles content_area's own scroll
                # the same way so the whole ancestor chain stays unscrolled
                # for this one tab.
                scroll=(None if key == "gsm_parser" else ft.ScrollMode.ADAPTIVE),
                spacing=0,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            )
            self._cached_view_wrappers[key] = col
            self.content_area.controls.append(col)
            # Ensure wrapper.page propagates so views can call self._page()
            with contextlib.suppress(Exception):
                self.content_area.update()
        return self._cached_view_wrappers[key]

    def _activate_cached_view(self, key: str) -> ft.Column:
        _to_update = []
        if self._scratch_col.visible:
            self._scratch_col.visible = False
            _to_update.append(self._scratch_col)
        for k, w in self._cached_view_wrappers.items():
            new_vis = k == key
            if w.visible != new_vis:
                w.visible = new_vis
                _to_update.append(w)
        # gsm_parser's own header/body scroll split needs an unscrolled
        # ancestor chain (see _get_or_create_cached_wrapper) — content_area
        # is shared by every tab, so toggle it per activation and restore
        # ADAPTIVE for everyone else.
        _want_scroll = None if key == "gsm_parser" else ft.ScrollMode.ADAPTIVE
        if self.content_area.scroll != _want_scroll:
            self.content_area.scroll = _want_scroll
            _to_update.append(self.content_area)
        for _ctrl in _to_update:
            with contextlib.suppress(Exception):
                _ctrl.update()
        # Remove h-scroll overlay buttons left by the previous view.
        pg = self.page
        if pg:
            for _b in getattr(pg, '_catman_hscroll_btns', []):
                with contextlib.suppress(Exception):
                    pg.overlay.remove(_b)
            pg._catman_hscroll_btns = []
        return self._cached_view_wrappers[key]

    def _activate_scratch_view(self) -> ft.Column:
        for w in self._cached_view_wrappers.values():
            w.visible = False
        self._scratch_col.visible = True
        self._scratch_col.controls.clear()
        # Restore normal page scrolling in case gsm_parser (the one tab that
        # needs content_area unscrolled) was the previously active view.
        if self.content_area.scroll != ft.ScrollMode.ADAPTIVE:
            self.content_area.scroll = ft.ScrollMode.ADAPTIVE
            with contextlib.suppress(Exception):
                self.content_area.update()
        # Remove h-scroll overlay buttons left by the previous view.
        pg = self.page
        if pg:
            for _b in getattr(pg, '_catman_hscroll_btns', []):
                with contextlib.suppress(Exception):
                    pg.overlay.remove(_b)
            pg._catman_hscroll_btns = []
        return self._scratch_col

    async def _scroll_content_area_top(self):
        try:
            import asyncio

            await asyncio.sleep(0.05)
            # Find the currently visible panel (wrapper or scratch) and scroll it
            _active_col = None
            for _w in self._cached_view_wrappers.values():
                if getattr(_w, "visible", False):
                    _active_col = _w
                    break
            if _active_col is None and getattr(self._scratch_col, "visible", False):
                _active_col = self._scratch_col
            if _active_col is not None:
                await _active_col.scroll_to(offset=0, duration=0)
            elif self.content_area:
                await self.content_area.scroll_to(offset=0, duration=0)
        except Exception:
            pass

    # (cache_key, show_fn_name, live_ref_attr, relevant_scopes)
    # "company"/"pricing" are unioned into every entry below: delivery
    # methods and price-tier segmentation are company-wide settings that can
    # affect prices/margins shown on any of these tabs, not just the two
    # dedicated cost-price screens — see _refresh_sidebar_for_feature_change.
    _VIEW_REFRESH_MAP = [
        ("home", "show_home", "_live_home_view_ref", frozenset({"orders", "payments", "packaging", "company", "pricing"})),
        (
            "orders",
            "show_orders",
            "_live_orders_view_ref",
            frozenset({"orders", "payments", "products", "suppliers", "packaging", "company", "pricing"}),
        ),
        (
            "payments",
            "show_payments",
            "_live_payments_view_ref",
            frozenset({"orders", "payments", "products", "suppliers", "packaging", "company", "pricing"}),
        ),
        (
            "cost_price_orders",
            "show_cost_price_orders",
            "_live_cost_price_orders_view_ref",
            frozenset(
                {"orders", "payments", "products", "suppliers", "packaging", "pricing", "company"}
            ),
        ),
        (
            "cost_price_stock",
            "show_cost_price_stock",
            "_live_cost_price_stock_view_ref",
            frozenset({"products", "suppliers", "packaging", "pricing", "catalog", "company"}),
        ),
        (
            "pack_stock",
            "show_pack_stock",
            "_live_pack_stock_view_ref",
            frozenset({"orders", "payments", "packaging", "products", "suppliers", "company", "pricing"}),
        ),
        ("schedule", "show_schedule", "_live_schedule_view_ref", frozenset({"orders", "products", "company", "pricing"})),
        (
            "media_plan",
            "show_media_plan",
            "_live_media_plan_view_ref",
            frozenset({"orders", "products", "company", "pricing"}),
        ),
    ]

    _view_bg_refresh_min_interval: float = (
        30.0  # seconds between background tab rebuilds
    )

    def _maybe_refresh_live_views(self, changed_scopes) -> None:
        try:
            scopes = set(changed_scopes or set())
            if not scopes:
                return
            if "extensions" in scopes:
                with contextlib.suppress(Exception):
                    self._handle_extensions_scope_change()
            active_vn = getattr(self.current_view_func, "__name__", "")
            for cache_key, view_fn, ref_attr, rel_scopes in self._VIEW_REFRESH_MAP:
                if not (scopes & rel_scopes):
                    continue
                inst = getattr(self, ref_attr, None) or (
                    self._view_cache.get(cache_key) or {}
                ).get("instance")
                if inst is None:
                    continue
                is_active = active_vn == view_fn
                with contextlib.suppress(Exception):
                    self.page.run_task(
                        self._bg_refresh_one_view_async, cache_key, inst, is_active
                    )
        except Exception:
            logger.exception("Live mirror view refresh failed")

    async def _bg_refresh_one_view_async(
        self, cache_key: str, inst, is_active: bool
    ) -> None:
        # Note: `is_active` is intentionally no longer used to blanket-skip a
        # refresh here. Home's resume(silent=True) never wipes the visible
        # content, and Orders' resume() already refuses to reload while this
        # session has unsaved local edits (see orders_view._bg_orders_pulse_check).
        # Both are safe to call whether or not their tab is on screen.
        try:
            # Throttle background rebuilds per tab.
            now = time.monotonic()
            ts_attr = f"_view_bg_ts_{cache_key}"
            if now - getattr(self, ts_attr, 0.0) < self._view_bg_refresh_min_interval:
                return
            setattr(self, ts_attr, now)
            if cache_key == "home":
                # silent=True skips the loading-stub content wipe so the KPI
                # grid doesn't blink to a spinner while someone is reading it.
                with contextlib.suppress(Exception):
                    await inst.resume(silent=True)
                return
            # Prefer resume() for all views — it now validates DB pulse and
            # reloads from DB only when data actually changed.
            if hasattr(inst, "resume"):
                with contextlib.suppress(Exception):
                    await inst.resume()
            elif hasattr(inst, "build"):
                with contextlib.suppress(Exception):
                    inst.build()
        except Exception:
            pass

    def _on_network_reconnect(self) -> None:
        """Called by main.py when network connectivity is restored.
        Clears per-session cache verification flags so the next resume() on any
        cached tab will force a fresh DB pulse check instead of skipping it."""
        try:
            keys_to_clear = [
                k
                for k in list(self.user_data.keys())
                if k.startswith("_orders_cache_session_verified_")
                or k.startswith("_orders_cache_is_verifying_")
            ]
            for k in keys_to_clear:
                self.user_data.pop(k, None)
            # Reset per-tab background refresh timestamps so throttle won't block
            # the first post-reconnect refresh.
            for cache_key, _, _, _ in self._VIEW_REFRESH_MAP:
                ts_attr = f"_view_bg_ts_{cache_key}"
                with contextlib.suppress(Exception):
                    setattr(self, ts_attr, 0.0)
            logger.info(
                "[reconnect] session cache flags cleared; views will revalidate on next visit"
            )
        except Exception:
            logger.exception("[reconnect] _on_network_reconnect failed")

    _home_refresh_min_interval: float = 20.0  # seconds between auto-refreshes

    async def _refresh_home_async(self) -> None:
        """Refresh home view counters and tables without full page reload."""
        now = time.monotonic()
        last = getattr(self, "_home_last_refresh_mono", 0.0)
        if now - last < self._home_refresh_min_interval:
            return
        self._home_last_refresh_mono = now
        hv = (self._view_cache.get("home") or {}).get("instance")
        if hv is None:
            return
        # Update orders snapshot in-memory — no DB call, no rebuild.
        # The home view will use fresh snapshot on next build() (tab switch or Ctrl+F5).
        ov = getattr(self, "_live_orders_view_ref", None)
        if ov is not None and getattr(ov, "_orders", None):
            hv._live_orders_snapshot = list(ov._orders)
        elif self._preloaded_orders:
            hv._live_orders_snapshot = list(self._preloaded_orders)
        # Refresh packaging stock card in-place (DB fetch in thread, UI on main loop).
        col = getattr(hv, "_pack_stock_items_col", None)
        if col is not None:
            try:
                rows = await asyncio.to_thread(hv._packaging_stock_rows)
            except Exception:
                rows = []
            with contextlib.suppress(Exception):
                hv._apply_pack_rows_to_col(col, rows)
        # NEVER call hv.build() from background — causes visible page flash every 45s.

    def _gsm_tg_flush_permitted(self) -> bool:
        """Gate for gsm_local_tg_flush_loop — re-checked every tick, not
        cached, since this session's manager could be granted/revoked the
        permission by a superadmin while the app stays open."""
        try:
            if not self.db.is_company_gsm_parser_enabled(self.company_id):
                return False
            manager_id = str(self.user_data.get("id") or "")
            if not manager_id:
                return False
            return "run_parser" in (self.db.get_manager_gsm_permissions(manager_id) or set())
        except Exception:
            return False

    async def _home_auto_refresh_loop(self) -> None:
        """Background snapshot updater — only updates _live_orders_snapshot,
        never calls build() to avoid visible page restarts every 45s."""
        await asyncio.sleep(45)
        while True:
            try:
                await self._refresh_home_async()
            except Exception:
                pass
            await asyncio.sleep(45)

    def _sync_view_reload_min_interval_s(self) -> float:
        return max(
            2.5,
            float(getattr(Config, "SYNC_VIEW_RELOAD_MIN_INTERVAL_SECONDS", 5.0) or 5.0),
        )

    def _on_sync_pulse_needs_full_view_reload(self) -> None:
        # Strict sync mode: never auto-reload the current view on pulses.
        self._pending_sync_pulse_view_reload = True

    def _flush_pending_sync_pulse_view_reload_if_due(self) -> None:
        if not self._pending_sync_pulse_view_reload:
            return
        # Keep this as a lightweight marker lifecycle only; no view reload.
        now = time.monotonic()
        if (
            now - self._last_sync_pulse_view_reload_mono
            < self._sync_view_reload_min_interval_s()
        ):
            return
        self._pending_sync_pulse_view_reload = False
        self._last_sync_pulse_view_reload_mono = now

    def _shared_live_sync_tick(self, pulses=None, company_id=None):
        try:
            if not bool(getattr(Config, "SYNC_PULSES_ENABLED", False)):
                return False
            if company_id is None:
                company_id = (self.company_id or "").strip()
            if not company_id:
                return False

            # "extensions" (feature toggles / GSM Parser) is unioned in
            # unconditionally — the sidebar depends on it regardless of which
            # tab is currently open, unlike per-view scopes below, so it must
            # not be skipped just because the current view (e.g. Home,
            # Settings, GSM Parser) has no per-view scopes of its own.
            if pulses is None:
                # Fallback path (pulses not pre-fetched by the caller) — kept
                # for safety, but the hot path below now fetches off-thread.
                active_scopes = list(set(self._active_scopes_for_current_view()) | {"extensions"})
                pulses = self.db.get_companies_sync_pulses(
                    [company_id], scopes=active_scopes, force_refresh=True
                )
            current = pulses.get(company_id, {}) if isinstance(pulses, dict) else {}
            if not isinstance(current, dict):
                return False

            changed_scopes = set()
            snapshot_changed = False
            for scope, ts in current.items():
                key = f"{company_id}:{scope}"
                prev = int(self._sync_last_pulses.get(key, 0) or 0)
                now_ts = int(ts or 0)
                if now_ts <= 0:
                    continue
                if prev > 0 and now_ts > prev:
                    self.db.invalidate_scope_cache(company_id, scope)
                    changed_scopes.add(scope)
                if now_ts != prev:
                    snapshot_changed = True
                self._sync_last_pulses[key] = now_ts

            if snapshot_changed:
                self._save_sync_snapshot()

            if not changed_scopes:
                return False

            self._mark_remote_updates(changed_scopes)
            self._maybe_refresh_live_views(changed_scopes)
            self._on_sync_pulse_needs_full_view_reload()
            return True
        except Exception:
            logger.exception("Shared-live sync tick failed")
            return False

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
        if not bool(getattr(Config, "SYNC_PULSES_ENABLED", False)):
            return
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

        async def _poll_once():
            nonlocal interval, idle_cycles, error_streak
            if mode == "cache_only":
                return
            if mode == "shared_live":
                # The DB round trip here used to run directly on this
                # coroutine (called synchronously, not awaited) — over
                # localhost that was sub-millisecond and invisible, but over
                # a remote VPS connection it blocks the ENTIRE asyncio event
                # loop (including button click handlers) for the length of
                # the round trip, every couple of seconds. Move just the
                # network fetch to a worker thread so the UI stays
                # responsive; the (Flet-control-touching) processing below
                # still runs on the main thread as before.
                try:
                    company_id = (self.company_id or "").strip()
                    pulses = None
                    if company_id:
                        active_scopes = list(
                            set(self._active_scopes_for_current_view()) | {"extensions"}
                        )
                        pulses = await asyncio.to_thread(
                            self.db.get_companies_sync_pulses,
                            [company_id],
                            active_scopes,
                            True,
                        )
                    if company_id and pulses is not None:
                        self._shared_live_sync_tick(pulses=pulses, company_id=company_id)
                except Exception:
                    logger.exception("Shared-live sync off-thread fetch failed")
                self._flush_pending_sync_pulse_view_reload_if_due()
                interval = fast_interval
                return
            self._flush_pending_sync_pulse_view_reload_if_due()
            try:
                company_id = (self.company_id or "").strip()
                if not company_id:
                    return

                pulses = await asyncio.to_thread(
                    self.db.get_companies_sync_pulses,
                    [company_id],
                    self._sync_scopes(),
                    True,
                )
                current = pulses.get(company_id, {}) if isinstance(pulses, dict) else {}
                if not isinstance(current, dict):
                    return

                changed = False
                changed_scopes = set()
                snapshot_changed = False
                for scope, ts in current.items():
                    key = f"{company_id}:{scope}"
                    prev = int(self._sync_last_pulses.get(key, 0) or 0)
                    now_ts = int(ts or 0)
                    if now_ts <= 0:
                        continue
                    if prev > 0 and now_ts > prev:
                        self.db.invalidate_scope_cache(company_id, scope)
                        changed = True
                        changed_scopes.add(scope)
                    if now_ts != prev:
                        snapshot_changed = True
                    self._sync_last_pulses[key] = now_ts

                if snapshot_changed:
                    self._save_sync_snapshot()

                if changed:
                    logger.info(
                        "[bg-sync] зміни в БД: scope=%s → _maybe_refresh_live_views",
                        sorted(changed_scopes),
                    )
                    self._mark_remote_updates(changed_scopes)
                    self._maybe_refresh_live_views(changed_scopes)

                if changed:
                    self._on_sync_pulse_needs_full_view_reload()
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
                logger.exception("Main dashboard sync poll failed")
                error_streak += 1
                interval = min(
                    60.0, max(5.0, interval * (1.6 + min(error_streak, 5) * 0.1))
                )

        # Check for remote changes immediately on login/startup instead of
        # only after the first `interval` sleep -- otherwise edits made from
        # another PC while this session was closed wouldn't be pulled in
        # until several seconds after the dashboard was already on screen
        # (and, if that first poll happened to fail -- e.g. the shared VPS
        # briefly out of free connections -- potentially much later, since
        # the local on-disk order/product snapshots are only ever refreshed
        # by this same loop noticing a bumped pulse).
        await _poll_once()
        while True:
            await asyncio.sleep(interval)
            await _poll_once()

    def _app_data_dir(self):
        flet_storage = os.getenv("FLET_APP_STORAGE_DATA")
        if flet_storage:
            target = flet_storage
        else:
            base = os.getenv("APPDATA") or os.path.expanduser("~")
            target = os.path.join(base, "CatManPro")
        os.makedirs(target, exist_ok=True)
        return target

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

    def _wrap_with_background(self, content):
        bg_src = self.db.get_effective_background_source(
            self.company_id, self.user_data.get("id")
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
                self.company_id, self.user_data.get("id")
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

    def _workspace_state_file(self):
        company_id = (self.company_id or "").strip()
        manager_id = (self.user_data.get("id") or "").strip()
        seed = f"{company_id}:{manager_id}".encode("utf-8")
        suffix = hashlib.sha1(seed).hexdigest()[:12]
        return os.path.join(self._app_data_dir(), f"workspace_pages_{suffix}.json")

    def _workspace_title_for_view(self, view_name):
        labels = {
            "show_home": self.t("nav.home_tab_title", "Головна"),
            "show_products": self.t("nav.products", "Товари"),
            "show_categories_brands": self.t("nav.categories", "Категорії"),
            "show_suppliers": self.t("nav.suppliers", "Постачальники"),
            "show_packaging": self.t("nav.packaging", "Пакування"),
            "show_copy_setup": self.t("nav.copy", "Копіювання"),
            "show_orders": self.t("nav.orders", "Замовлення"),
            "show_payments": self.t("nav.payments", "Взаєморозрахунки"),
            "show_cost_price": self.t("nav.cost_orders", "С/В Замовлення"),
            "show_cost_price_orders": self.t("nav.cost_orders", "С/В Замовлення"),
            "show_cost_price_stock": self.t("nav.cost_stock", "С/В Склад"),
            "show_pack_stock": self.t("nav.pack_stock", "Склад Пакування"),
            "show_media_plan": self.t("nav.media", "Медіа План"),
            "show_schedule": self.t("nav.schedule_short", "Розклад"),
            "show_settings": self.t("nav.settings_tooltip", "Налаштування"),
            "show_gsm_parser": self.t("nav.gsm", "GSM Parser"),
            "show_files": self.t("nav.files", "Файли"),
        }
        return labels.get(view_name, self.t("nav.tab_fallback", "Сторінка"))

    def _new_workspace(self, view_name="show_home", fixed_home: bool = False):
        region_value = (
            self.current_region if self.current_region is not None else ALL_REGIONS_KEY
        )
        is_fixed_home = bool(fixed_home)
        if is_fixed_home:
            view_name = "show_home"
        return {
            "id": str(uuid.uuid4()),
            "view": view_name,
            "title": (
                "Головна"
                if is_fixed_home
                else self._workspace_title_for_view(view_name)
            ),
            "manager_id": self.current_manager_id or self.user_data.get("id"),
            "region": region_value,
            "filters": {},
            "fixed_home": is_fixed_home,
        }

    def _sanitize_workspace(self, row):
        if not isinstance(row, dict):
            return None
        wid = str(row.get("id") or "").strip() or str(uuid.uuid4())
        view_name = str(row.get("view") or "show_home").strip()
        manager_id = str(row.get("manager_id") or "").strip() or (
            self.user_data.get("id") or ""
        )
        region = row.get("region") if row.get("region") is not None else ALL_REGIONS_KEY
        filters = row.get("filters") if isinstance(row.get("filters"), dict) else {}
        products_filters = (
            filters.get("products") if isinstance(filters.get("products"), dict) else {}
        )
        category_ids = (
            products_filters.get("category_ids")
            if isinstance(products_filters.get("category_ids"), list)
            else []
        )
        if not category_ids and products_filters.get("category_id"):
            category_ids = [str(products_filters.get("category_id"))]
        subcategory_ids = (
            products_filters.get("subcategory_ids")
            if isinstance(products_filters.get("subcategory_ids"), list)
            else []
        )
        if not subcategory_ids and products_filters.get("subcategory_id"):
            subcategory_ids = [str(products_filters.get("subcategory_id"))]
        brand_ids = (
            products_filters.get("brand_ids")
            if isinstance(products_filters.get("brand_ids"), list)
            else []
        )
        if not brand_ids and products_filters.get("brand_id"):
            brand_ids = [str(products_filters.get("brand_id"))]
        return {
            "id": wid,
            "view": view_name,
            "title": str(row.get("title") or self._workspace_title_for_view(view_name)),
            "manager_id": manager_id,
            "region": region,
            "fixed_home": bool(row.get("fixed_home", False)),
            "filters": {
                "products": {
                    "query": str(products_filters.get("query") or ""),
                    "category_id": str(products_filters.get("category_id") or ""),
                    "category_ids": [
                        str(x).strip() for x in category_ids if str(x).strip()
                    ],
                    "subcategory_id": str(products_filters.get("subcategory_id") or ""),
                    "subcategory_ids": [
                        str(x).strip() for x in subcategory_ids if str(x).strip()
                    ],
                    "brand_id": str(products_filters.get("brand_id") or ""),
                    "brand_ids": [str(x).strip() for x in brand_ids if str(x).strip()],
                    "filters_expanded": bool(
                        products_filters.get("filters_expanded", False)
                    ),
                    "sort_order": str(products_filters.get("sort_order") or "asc"),
                }
            },
        }

    def _active_workspace(self):
        for ws in self._workspaces:
            if isinstance(ws, dict) and ws.get("id") == self._active_workspace_id:
                return ws
        return None

    def _ensure_fixed_home_workspace(self, rows):
        normalized = [r for r in (rows or []) if isinstance(r, dict)]
        home_ws = next((r for r in normalized if bool(r.get("fixed_home"))), None)
        if home_ws is None:
            home_ws = self._new_workspace("show_home", fixed_home=True)
            normalized.insert(0, home_ws)
        else:
            normalized = [home_ws] + [r for r in normalized if r is not home_ws]

        home_ws["fixed_home"] = True
        home_ws["view"] = "show_home"
        home_ws["title"] = self.t("nav.home_tab_title", "Головна")
        if not isinstance(home_ws.get("filters"), dict):
            home_ws["filters"] = {}
        return normalized, str(home_ws.get("id") or "").strip()

    def _save_workspace_state_local(self, payload):
        try:
            with open(self._workspace_state_file(), "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            logger.exception("Save workspace state local failed")
            return False

    def _load_workspace_state_local(self):
        try:
            path = self._workspace_state_file()
            if not os.path.exists(path):
                return {}
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            logger.exception("Load workspace state local failed")
            return {}

    def _serialize_workspace_state(self):
        rows = []
        for ws in self._workspaces:
            clean = self._sanitize_workspace(ws)
            if clean:
                rows.append(clean)
        rows, home_id = self._ensure_fixed_home_workspace(rows)
        _mid = (self.user_data.get("id") or "").strip()
        return {
            "version": 1,
            # Home is always first and active on app start.
            "active_id": home_id or self._active_workspace_id or rows[0].get("id"),
            "workspaces": rows,
            "updated_at": int(datetime.now().timestamp()),
            "ui_theme": _themes.get_active_theme_key(_mid),
            "ui_nav_pack": _themes.get_active_nav_pack_key(_mid),
        }

    def _persist_workspace_state(self):
        payload = self._serialize_workspace_state()
        try:
            payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except Exception:
            payload_text = ""
        if payload_text and payload_text == self._workspace_last_saved_text:
            return
        manager_id = (self.user_data.get("id") or "").strip()
        merged_payload = dict(payload)
        try:
            existing_payload = {}
            if manager_id:
                existing_payload = self.db.get_manager_workspace_state(manager_id) or {}
            if not isinstance(existing_payload, dict) or not existing_payload:
                existing_payload = self._load_workspace_state_local()
            if isinstance(existing_payload, dict):
                merged_payload = dict(existing_payload)
                merged_payload.update(payload)
        except Exception:
            logger.exception("Merge workspace state failed")
            merged_payload = dict(payload)

        self._save_workspace_state_local(merged_payload)
        if manager_id:
            self.db.save_manager_workspace_state(manager_id, merged_payload)
        if payload_text:
            self._workspace_last_saved_text = payload_text

    def _schedule_workspace_save(self):
        try:
            self.page.run_task(self._async_save_workspace_state)
        except Exception:
            self._persist_workspace_state()

    async def _async_save_workspace_state(self):
        await asyncio.sleep(0.01)
        self._persist_workspace_state()

    def _ensure_workspace_state_loaded(self):
        if self._workspace_state_loaded:
            return
        payload = {}
        manager_id = (self.user_data.get("id") or "").strip()
        if manager_id:
            payload = self.db.get_manager_workspace_state(manager_id)
        if not isinstance(payload, dict) or not payload.get("workspaces"):
            payload = self._load_workspace_state_local()

        loaded_rows = []
        for row in (
            payload.get("workspaces") if isinstance(payload, dict) else []
        ) or []:
            clean = self._sanitize_workspace(row)
            if clean:
                loaded_rows.append(clean)

        loaded_rows, home_id = self._ensure_fixed_home_workspace(loaded_rows)

        active_id = str((payload or {}).get("active_id") or "").strip()
        if not any((r.get("id") == active_id) for r in loaded_rows):
            active_id = home_id or loaded_rows[0].get("id")

        # On startup always open Home first.
        active_id = home_id or active_id

        self._workspaces = loaded_rows
        self._active_workspace_id = active_id
        self._workspace_state_loaded = True
        # Load "work orders mode" preference (default True).
        if isinstance(payload, dict) and "work_orders_mode" in payload:
            self._work_orders_mode = bool(payload.get("work_orders_mode", True))
        # Load theme and symbol pack preferences.
        if manager_id and isinstance(payload, dict):
            _themes.set_active_theme(manager_id, payload.get("ui_theme"))
            _themes.set_active_nav_pack(manager_id, payload.get("ui_nav_pack"))
            i18n.set_active_language(manager_id, payload.get("ui_language"))
        self._apply_workspace_context(self._active_workspace())
        # On startup always reset to own account — never open with a subordinate's context.
        own_id = str(self.user_data.get("id") or "").strip()
        _was_subordinate = own_id and self.current_manager_id != own_id
        if own_id:
            self.current_manager_id = own_id
            self.current_manager_name = (
                str(self.user_data.get("full_name") or "Мої дані").strip() or "Мої дані"
            )
        if _was_subordinate:
            # Previous session ended on a subordinate — cached view data is stale.
            # Invalidate caches and reset all saved tab contexts to own account.
            self._view_cache.clear()
            self._cached_view_wrappers.clear()
            self._preloaded_orders = None
            for _ws in self._workspaces:
                if isinstance(_ws, dict):
                    _ws["manager_id"] = own_id
        self._refresh_workspace_tabs()

    def _apply_workspace_context(self, ws, restore_manager_region=True):
        if not isinstance(ws, dict):
            return
        if restore_manager_region:
            manager_id = (ws.get("manager_id") or "").strip()
            manager_lookup = {
                self.user_data.get("id"): self.user_data.get("full_name", "Мої дані")
            }
            for m in self.visible_managers or []:
                if isinstance(m, dict) and m.get("id"):
                    manager_lookup[m.get("id")] = m.get("full_name") or m.get("id")
            if manager_id in manager_lookup:
                self.current_manager_id = manager_id
                self.current_manager_name = manager_lookup.get(manager_id) or "Мої дані"
            else:
                self.current_manager_id = self.user_data.get("id")
                self.current_manager_name = self.user_data.get("full_name", "Мої дані")

            regions = self._refresh_regions()
            region_choices = ([ALL_REGIONS_KEY] if len(regions) > 1 else []) + regions
            desired_region = ws.get("region")
            if desired_region in region_choices:
                self.current_region = desired_region
                self._refresh_regions()

        view_callable = self._resolve_view_callable(ws.get("view") or "show_home")
        self.current_view_func = view_callable

    def _resolve_view_callable(self, view_name):
        mapping = {
            "show_home": self.show_home,
            "show_settings": self.show_settings,
            "show_products": self.show_products,
            "show_categories_brands": self.show_categories_brands,
            "show_suppliers": self.show_suppliers,
            "show_packaging": self.show_packaging,
            "show_copy_setup": self.show_copy_setup,
            "show_gsm_parser": self.show_gsm_parser,
            "show_orders": self.show_orders,
            "show_payments": self.show_payments,
            "show_cost_price": self.show_cost_price_orders,
            "show_cost_price_orders": self.show_cost_price_orders,
            "show_cost_price_stock": self.show_cost_price_stock,
            "show_pack_stock": self.show_pack_stock,
            "show_media_plan": self.show_media_plan,
            "show_schedule": self.show_schedule,
            "show_files": self.show_files,
        }
        return mapping.get(view_name, self.show_home)

    def _set_view_for_workspace(self, view_name):
        self._remember_global_hscroll_for_current_view()
        # Clear search highlight from the tab we're leaving
        self._clear_search_highlight()
        # Switching tabs: reset zoom visually then restore saved zoom for the target tab
        if abs(getattr(self, "_zoom_factor", 1.0) - 1.0) > 0.001:
            self.page.run_task(self._reset_zoom_async)
        target_vkey = view_name[5:] if view_name.startswith("show_") else view_name
        saved = self._zoom_by_vkey.get(target_vkey, 1.0)
        if abs(saved - 1.0) > 0.001:
            self._zoom_factor = saved
            with contextlib.suppress(Exception):
                self.page._zoom_factor = saved
            self.page.run_task(self._apply_zoom_async, self._zoom_apply_seq)
        try:
            self._view_sticky_header.controls.clear()
            self._view_sticky_header.update()
        except Exception:
            pass
        try:
            self._view_h_scrollbar_slot.controls.clear()
            self._view_h_scrollbar_slot.update()
        except Exception:
            pass
        ws = self._active_workspace()
        self.current_view_func = self._resolve_view_callable(view_name)
        changed = False
        # Keep fixed Home tab as Home: opening another module from Home creates
        # a regular workspace tab and navigates there.
        if (
            isinstance(ws, dict)
            and bool(ws.get("fixed_home"))
            and view_name != "show_home"
        ):
            row = self._new_workspace(view_name)
            self._workspaces.append(row)
            self._active_workspace_id = row.get("id")
            ws = row
            changed = True
        if isinstance(ws, dict):
            old_view = ws.get("view")
            old_title = ws.get("title")
            ws["view"] = view_name
            ws["title"] = (
                "Головна"
                if bool(ws.get("fixed_home"))
                else self._workspace_title_for_view(view_name)
            )
            ws["manager_id"] = self.current_manager_id or self.user_data.get("id")
            ws["region"] = (
                self.current_region
                if self.current_region is not None
                else ALL_REGIONS_KEY
            )
            changed = old_view != ws.get("view") or old_title != ws.get("title")
        self._refresh_workspace_tabs()
        self._schedule_restore_global_hscroll(view_name)
        if changed:
            self._set_sidebar_visible(False)
            self._schedule_workspace_save()

    def _set_sidebar_visible(self, visible: bool):
        self._sidebar_visible = bool(visible)
        if self._sidebar_container is not None:
            self._sidebar_container.visible = self._sidebar_visible
            self._sidebar_container.width = 216 if self._sidebar_visible else 0
            if self._layout_built:
                with contextlib.suppress(Exception):
                    self._sidebar_container.update()
        if self._sidebar_toggle_btn is not None:
            self._sidebar_toggle_btn.icon = (
                ft.Icons.MENU_OPEN if self._sidebar_visible else ft.Icons.MENU
            )
            if self._layout_built:
                with contextlib.suppress(Exception):
                    self._sidebar_toggle_btn.update()

    def _refresh_workspace_tabs(self):
        if self._workspace_tabs_row is None:
            return
        import views.themes as _themes_mod
        _mid = (self.user_data.get("id") or "").strip()
        _ct = _themes_mod.get_active_theme(_mid)
        _tab_active_bg = _ct["chip_bg"]
        _tab_inactive_bg = _ct["soft"]
        _tab_active_text = _ct["chip_text"]
        _tab_inactive_text = _ct["muted"]
        controls = []
        for idx, ws in enumerate(self._workspaces):
            if not isinstance(ws, dict):
                continue
            active = ws.get("id") == self._active_workspace_id
            title = (ws.get("title") or self.t("nav.tab_default_title", "Сторінка {n}").format(n=idx + 1)).strip()
            closable = not bool(ws.get("fixed_home"))
            _wid = ws.get("id")
            _view = ws.get("view") or "show_home"
            _fixed = bool(ws.get("fixed_home"))

            def _lmb(_, wid=_wid):
                self._switch_workspace(wid)

            def _rmb(_, wid=_wid, view=_view, t=title, fh=_fixed):
                self._show_tab_context_menu(wid, view, t, fh)

            def _mmb(_, view=_view):
                self._open_in_new_workspace(view)

            controls.append(
                ft.GestureDetector(
                    on_secondary_tap=_rmb,
                    on_tertiary_tap_up=_mmb,
                    content=ft.Container(
                        padding=ft.padding.symmetric(horizontal=6, vertical=2),
                        border_radius=8,
                        bgcolor=_tab_active_bg if active else _tab_inactive_bg,
                        content=ft.Row(
                            [
                                ft.TextButton(
                                    content=ft.Text(
                                        title,
                                        size=11,
                                        color=_tab_active_text if active else _tab_inactive_text,
                                    ),
                                    on_click=_lmb,
                                    style=ft.ButtonStyle(
                                        padding=ft.padding.symmetric(
                                            horizontal=3, vertical=0
                                        ),
                                    ),
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.CLOSE,
                                    icon_size=12,
                                    tooltip=self.t("nav.close_tab", "Закрити сторінку"),
                                    style=ft.ButtonStyle(padding=ft.padding.all(0)),
                                    on_click=lambda _, wid=_wid: self._close_workspace(wid),
                                    visible=closable,
                                ),
                            ],
                            spacing=1,
                            tight=True,
                        ),
                    ),
                )
            )
        self._workspace_tabs_row.controls = controls
        if self._layout_built:
            try:
                self._workspace_tabs_row.update()
            except RuntimeError:
                # Row is not mounted yet during early build/restore stage.
                pass

    def _switch_workspace(self, workspace_id):
        target_id = str(workspace_id or "").strip()
        if not target_id or target_id == self._active_workspace_id:
            return
        self._save_active_workspace_context()
        self._active_workspace_id = target_id
        # manager/region are global — only restore the view function from workspace
        self._apply_workspace_context(
            self._active_workspace(), restore_manager_region=False
        )
        self._refresh_managers()
        self._refresh_regions()
        if self._layout_built:
            self._manager_buttons_row.update()
            self._region_buttons_row.update()
        self._refresh_workspace_tabs()
        self._with_view_switch_intent(lambda: self.current_view_func(None))
        self._schedule_workspace_save()

    def _close_workspace(self, workspace_id):
        target_id = str(workspace_id or "").strip()
        target_ws = next(
            (
                r
                for r in self._workspaces
                if isinstance(r, dict) and str(r.get("id") or "").strip() == target_id
            ),
            None,
        )
        if isinstance(target_ws, dict) and bool(target_ws.get("fixed_home")):
            return
        # Clean up workspace-specific cached view instances before removing the workspace.
        _suffix = f"_{target_id}"
        for _k in [k for k in list(self._view_cache.keys()) if k.endswith(_suffix)]:
            _inst = (self._view_cache.pop(_k, None) or {}).get("instance")
            if _inst is not None and hasattr(_inst, "stop"):
                with contextlib.suppress(Exception):
                    _inst.stop()
            _wrapper = self._cached_view_wrappers.pop(_k, None)
            if _wrapper is not None:
                with contextlib.suppress(Exception):
                    if _wrapper in self.content_area.controls:
                        self.content_area.controls.remove(_wrapper)
        rows = [
            r
            for r in self._workspaces
            if isinstance(r, dict) and r.get("id") != target_id
        ]
        rows, home_id = self._ensure_fixed_home_workspace(rows)
        self._workspaces = rows
        if not any((r.get("id") == self._active_workspace_id) for r in rows):
            self._active_workspace_id = home_id or rows[0].get("id")
            self._apply_workspace_context(
                self._active_workspace(), restore_manager_region=False
            )
            self._with_view_switch_intent(lambda: self.current_view_func(None))
        self._refresh_workspace_tabs()
        self._schedule_workspace_save()

    def _add_workspace(self, _=None):
        self._save_active_workspace_context()
        row = self._new_workspace("show_home")
        self._workspaces.append(row)
        self._active_workspace_id = row.get("id")
        self._apply_workspace_context(row, restore_manager_region=False)
        self._refresh_managers()
        self._refresh_regions()
        if self._layout_built:
            self._manager_buttons_row.update()
            self._region_buttons_row.update()
        self._refresh_workspace_tabs()
        self._with_view_switch_intent(lambda: self.show_home(None))
        self._schedule_workspace_save()

    def _open_in_new_workspace(self, view_name: str):
        self._save_active_workspace_context()
        row = self._new_workspace(view_name)
        self._workspaces.append(row)
        self._active_workspace_id = row.get("id")
        self._apply_workspace_context(row, restore_manager_region=False)
        view_func = getattr(self, view_name, None)
        if not callable(view_func):
            view_func = self.show_home
        self._refresh_managers()
        self._refresh_regions()
        if self._layout_built:
            with contextlib.suppress(Exception):
                self._manager_buttons_row.update()
            with contextlib.suppress(Exception):
                self._region_buttons_row.update()
        self._refresh_workspace_tabs()
        self._with_view_switch_intent(lambda: view_func(None))
        self._schedule_workspace_save()

    def _show_tab_context_menu(self, wid: str, view_name: str, title: str, fixed_home: bool):
        _dlg = [None]

        def _close():
            with contextlib.suppress(Exception):
                self.page.pop_dialog()

        def _open_here(_):
            _close()
            self._switch_workspace(wid)

        def _open_new(_):
            _close()
            self._open_in_new_workspace(view_name)

        btns = []
        if not fixed_home:
            btns.append(
                ft.TextButton(
                    content=ft.Row(
                        [ft.Icon(ft.Icons.OPEN_IN_BROWSER, size=16), ft.Text(self.t("nav.open_here", "Відкрити в цьому вікні"), size=13)],
                        spacing=8,
                        tight=True,
                    ),
                    on_click=_open_here,
                    style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=12, vertical=8)),
                )
            )
        btns.append(
            ft.TextButton(
                content=ft.Row(
                    [ft.Icon(ft.Icons.OPEN_IN_NEW, size=16), ft.Text(self.t("nav.open_new_window", "Відкрити в новому вікні"), size=13)],
                    spacing=8,
                    tight=True,
                ),
                on_click=_open_new,
                style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=12, vertical=8)),
            )
        )
        _dlg[0] = ft.AlertDialog(
            modal=False,
            title=ft.Text(title, size=13, weight="bold"),
            content=ft.Column(btns, tight=True, spacing=0),
            content_padding=ft.padding.only(left=8, right=8, bottom=8, top=4),
            title_padding=ft.padding.only(left=16, right=16, top=16, bottom=4),
        )
        with contextlib.suppress(Exception):
            self.page.open(_dlg[0])

    def _save_active_workspace_context(self):
        ws = self._active_workspace()
        if not isinstance(ws, dict):
            return
        ws["manager_id"] = self.current_manager_id or self.user_data.get("id")
        ws["region"] = (
            self.current_region if self.current_region is not None else ALL_REGIONS_KEY
        )
        ws["view"] = getattr(self.current_view_func, "__name__", "show_home")
        ws["title"] = self._workspace_title_for_view(ws["view"])

    def _on_products_state_change(self, state):
        ws = self._active_workspace()
        if not isinstance(ws, dict):
            return
        if not isinstance(ws.get("filters"), dict):
            ws["filters"] = {}
        ws["filters"]["products"] = {
            "query": str((state or {}).get("query") or ""),
            "category_id": str((state or {}).get("category_id") or ""),
            "category_ids": [
                str(x).strip()
                for x in ((state or {}).get("category_ids") or [])
                if str(x).strip()
            ],
            "subcategory_id": str((state or {}).get("subcategory_id") or ""),
            "subcategory_ids": [
                str(x).strip()
                for x in ((state or {}).get("subcategory_ids") or [])
                if str(x).strip()
            ],
            "brand_id": str((state or {}).get("brand_id") or ""),
            "brand_ids": [
                str(x).strip()
                for x in ((state or {}).get("brand_ids") or [])
                if str(x).strip()
            ],
            "filters_expanded": bool((state or {}).get("filters_expanded", False)),
            "sort_order": str((state or {}).get("sort_order") or "asc"),
        }
        self._schedule_workspace_save()

    def _refresh_regions(self):
        """Оновлює кнопки регіонів без перебудови layout."""
        _mid = (self.user_data.get("id") or "").strip()
        from views import themes as _th_mod
        _rth = _th_mod.get_active_theme(_mid)
        _r_active_bg = _rth["accent"]
        _r_active_text = _rth.get("accent_text", "#FFFFFF")
        _r_inactive_bg = _rth["chip_bg"]
        _r_inactive_text = _rth["chip_text"]
        current_manager_role = None
        if self.current_manager_id == self.user_data.get("id"):
            current_manager_role = self.user_data.get("role")
        else:
            for m in self.visible_managers:
                if isinstance(m, dict) and m.get("id") == self.current_manager_id:
                    current_manager_role = m.get("role")
                    break
        regions_raw = self.db.get_visible_regions_for_manager(
            self.company_id, self.current_manager_id, current_manager_role
        )
        regions = [
            r["name"] for r in regions_raw if isinstance(r, dict) and r.get("name")
        ]
        region_choices = ([ALL_REGIONS_KEY] if len(regions) > 1 else []) + regions
        if region_choices and self.current_region not in region_choices:
            self.current_region = region_choices[0]
        self._region_buttons_row.controls.clear()
        if len(region_choices) > 1:
            for r_name in region_choices:
                is_active = r_name == self.current_region
                label = "Всі регіони" if r_name == ALL_REGIONS_KEY else r_name
                self._region_buttons_row.controls.append(
                    ft.Container(
                        content=ft.Text(
                            label,
                            size=10,
                            weight="bold" if is_active else "normal",
                            color=_r_active_text if is_active else _r_inactive_text,
                        ),
                        padding=ft.padding.symmetric(horizontal=10, vertical=2),
                        bgcolor=_r_active_bg if is_active else _r_inactive_bg,
                        border_radius=5,
                        data=r_name,
                        on_click=self._on_region_click,
                    )
                )
        return regions

    def _current_region_for_query(self):
        if self.current_region == ALL_REGIONS_KEY:
            return None
        return self.current_region

    def _current_region_label(self):
        if self.current_region == ALL_REGIONS_KEY:
            return "Всі регіони"
        return self.current_region or "-"

    def _set_work_orders_mode(self, enabled: bool) -> None:
        """Toggle 'work orders mode'. When False, all views use subordinate data (general mode)."""
        self._work_orders_mode = bool(enabled)
        manager_id = (self.user_data.get("id") or "").strip()
        if manager_id:
            with contextlib.suppress(Exception):
                payload = self.db.get_manager_workspace_state(manager_id) or {}
                if not isinstance(payload, dict):
                    payload = {}
                payload["work_orders_mode"] = self._work_orders_mode
                self.db.save_manager_workspace_state(manager_id, payload)
        # When disabling: force switch to general mode immediately.
        if not self._work_orders_mode:
            if not self.current_general_mode:
                with contextlib.suppress(Exception):
                    self._on_general_click()
        # Invalidate view cache so next open re-renders with the new context.
        self._view_cache.clear()

    def _refresh_managers(self):
        """Оновлює кнопки менеджерів без перебудови layout."""
        self.visible_managers = self.db.get_visible_managers(
            self.company_id, self.user_data.get("id"), self.user_data.get("role")
        )
        # Lazy-load recursive subordinate tree (only when not yet loaded).
        # Reload when the list of direct visible managers changed (structural change).
        _vis_ids = frozenset(
            m.get("id")
            for m in self.visible_managers
            if isinstance(m, dict) and m.get("id")
        )
        if not self._all_subordinates_flat or _vis_ids != getattr(
            self, "_last_vis_ids", None
        ):
            self._last_vis_ids = _vis_ids
            try:
                self._all_subordinates_flat = (
                    self.db.get_all_subordinates_recursive(
                        self.user_data.get("id"), self.company_id
                    )
                    or []
                )
            except Exception:
                self._all_subordinates_flat = []

        # If current viewed manager is no longer visible, reset to own
        if not any(
            m.get("id") == self.current_manager_id
            for m in self.visible_managers
            if isinstance(m, dict)
        ):
            if not self.current_general_mode:
                self.current_manager_id = self.user_data.get("id")
                self.current_manager_name = self.user_data.get("full_name", "Мої дані")
        self._manager_buttons_row.controls.clear()
        # If there are no allowed subordinate targets and we're on own profile,
        # do not render the switch buttons row at all.
        visible_subs = self._all_subordinates_flat
        if (
            not self.visible_managers
            and not self.current_general_mode
            and self.current_manager_id == self.user_data.get("id")
        ):
            return

        # When work_orders_mode is OFF: force general mode immediately (no own data).
        if not self._work_orders_mode and not self.current_general_mode:
            visible_subs_now = self._all_subordinates_flat
            sub_ids_now = [
                str(m.get("id") or "")
                for m in visible_subs_now
                if isinstance(m, dict) and m.get("id")
            ]
            self.current_general_mode = True
            if not self.current_general_manager_ids:
                self.current_general_manager_ids = sub_ids_now

        _m_mid = (self.user_data.get("id") or "").strip()
        from views import themes as _mgr_th_mod
        _mth = _mgr_th_mod.get_active_theme(_m_mid)
        _m_active_bg = _mth["accent"]
        _m_active_text = _mth.get("accent_text", "#FFFFFF")
        _m_inactive_bg = _mth["chip_bg"]
        _m_inactive_text = _mth["chip_text"]
        own = (not self.current_general_mode) and (
            self.current_manager_id == self.user_data.get("id")
        )
        # "Мої дані" button — hidden when work_orders_mode is OFF.
        if self._work_orders_mode:
            self._manager_buttons_row.controls.append(
                ft.Container(
                    content=ft.Text(
                        "Мої дані",
                        size=10,
                        weight="bold" if own else "normal",
                        color=_m_active_text if own else _m_inactive_text,
                    ),
                    padding=ft.padding.symmetric(horizontal=10, vertical=2),
                    bgcolor=_m_active_bg if own else _m_inactive_bg,
                    border_radius=5,
                    data={
                        "id": self.user_data.get("id"),
                        "full_name": self.user_data.get("full_name", "Мої дані"),
                    },
                    on_click=self._on_manager_click,
                )
            )
        # "Загальні" button — only if there are visible (non-test-filtered) subordinates
        if visible_subs:
            is_general = self.current_general_mode
            self._manager_buttons_row.controls.append(
                ft.Container(
                    content=ft.Text(
                        "Загальні",
                        size=10,
                        weight="bold" if is_general else "normal",
                        color=_m_active_text if is_general else _m_inactive_text,
                    ),
                    padding=ft.padding.symmetric(horizontal=10, vertical=2),
                    bgcolor=_m_active_bg if is_general else _m_inactive_bg,
                    border_radius=5,
                    on_click=self._on_general_click,
                )
            )
        for manager in self.visible_managers:
            if not isinstance(manager, dict) or manager.get("id") == self.user_data.get(
                "id"
            ):
                continue
            is_active = (not self.current_general_mode) and (
                manager.get("id") == self.current_manager_id
            )
            self._manager_buttons_row.controls.append(
                ft.Container(
                    content=ft.Text(
                        manager.get("full_name", "Менеджер"),
                        size=10,
                        weight="bold" if is_active else "normal",
                        color=_m_active_text if is_active else _m_inactive_text,
                    ),
                    padding=ft.padding.symmetric(horizontal=10, vertical=2),
                    bgcolor=_m_active_bg if is_active else _m_inactive_bg,
                    border_radius=5,
                    data=manager,
                    on_click=self._on_manager_click,
                )
            )

    def _on_region_click(self, e):
        try:
            # Refresh region selector from DB on each interaction,
            # so newly created regions appear without navigating away.
            self._refresh_regions()
            try:
                self._region_buttons_row.update()
            except Exception:
                pass
            self.current_region = e.control.data
            self._save_active_workspace_context()
            self._refresh_regions()
            self._region_buttons_row.update()
            # Same manager, only region changed — "files" has its own cheap
            # set_region() update path (see FilesView.set_region), so keep it
            # cached instead of forcing a full teardown+rebuild through the
            # generic show_files() cold-start path.
            self._invalidate_context_cache(preserve_keys=frozenset({"files"}))

            # Reload the current tab without blocking this click handler —
            # same fix as _apply_language_live/_apply_theme_live: scheduling
            # via run_task lets the region-button highlight above render
            # immediately, and (for "files") the DB refetch inside
            # set_region() now runs off-thread too, so nothing freezes.
            async def _reload_current_view() -> None:
                with contextlib.suppress(Exception):
                    view_func = self.current_view_func
                    if callable(view_func):
                        view_func(None)

            with contextlib.suppress(Exception):
                self.page.run_task(_reload_current_view)
            self.page.run_task(self._preload_home_orders_bg)
            self._schedule_workspace_save()
        except Exception:
            logger.exception("Region change failed")

    def _on_general_click(self, e=None):
        """Switch to 'Загальні' mode — combined view of selected subordinates."""
        try:
            from views.general_selection_store import load_general_selection

            own_id = str(self.user_data.get("id") or "")
            visible_subs = self._all_subordinates_flat
            all_sub_ids = {
                str(m.get("id") or "") for m in visible_subs if isinstance(m, dict)
            }
            # When work_orders_mode is OFF, own data is excluded from general view.
            include_own = self._work_orders_mode
            saved = load_general_selection(own_id)
            valid_ids = all_sub_ids | ({own_id} if include_own else set())
            selected_ids = (
                [sid for sid in saved if sid in valid_ids]
                if saved
                else list(all_sub_ids | ({own_id} if include_own else set()))
            )
            if not selected_ids:
                selected_ids = list(all_sub_ids | ({own_id} if include_own else set()))
            self.current_general_mode = True
            self.current_general_manager_ids = selected_ids
            self.current_manager_id = own_id  # keep own id as "base"
            self.current_manager_name = self.user_data.get("full_name", "Мої дані")
            self._save_active_workspace_context()
            self._refresh_managers()
            self._refresh_regions()
            self._manager_buttons_row.update()
            self._region_buttons_row.update()
            # General mode uses own manager as base — keep preloaded orders.
            # Also ensure own snapshot is loaded from disk so REUSE CACHE works
            # on the very first render (without waiting for _preload_home_orders_bg).
            self._invalidate_context_cache(keep_preloaded_orders=True)
            self._ensure_own_preloaded_sync()
            self.current_view_func(None)
            self.page.run_task(self._preload_home_orders_bg)
            self._schedule_workspace_save()
        except Exception:
            logger.exception("General mode switch failed")

    def _on_general_selection_changed(self, selected_ids: list[str]) -> None:
        """Called by HomeView when user applies new subordinate selection in general mode."""
        try:
            from views.general_selection_store import save_general_selection

            own_id = str(self.user_data.get("id") or "")
            save_general_selection(own_id, selected_ids)
            self.current_general_manager_ids = list(selected_ids)
            # Keep preloaded orders — own user's data is still the base context
            self._invalidate_context_cache(keep_preloaded_orders=True)
            self._ensure_own_preloaded_sync()
            self.current_view_func(None)
            self.page.run_task(self._preload_home_orders_bg)
            self._schedule_workspace_save()
        except Exception:
            logger.exception("general_selection_changed failed")

    def _on_manager_click(self, e):
        try:
            selected = e.control.data if isinstance(e.control.data, dict) else {}
            manager_id = selected.get("id")
            if not manager_id:
                return
            # When work_orders_mode is OFF, own data is forbidden.
            if not self._work_orders_mode and manager_id == self.user_data.get("id"):
                return
            # Hard guard: only allow switching to own profile or visible allowed targets.
            if manager_id != self.user_data.get("id"):
                allowed_ids = {
                    m.get("id")
                    for m in (self.visible_managers or [])
                    if isinstance(m, dict) and m.get("id")
                }
                if manager_id not in allowed_ids:
                    return
            self.current_general_mode = False
            self.current_general_manager_ids = []
            self.current_manager_id = manager_id
            self.current_manager_name = selected.get("full_name") or "Мої дані"
            self._save_active_workspace_context()
            self._refresh_managers()
            self._refresh_regions()
            self._manager_buttons_row.update()
            self._region_buttons_row.update()
            # Always clear preloaded orders — they may contain data from the
            # previous manager's context. For own account, immediately reload
            # the supervisor's snapshot from disk so the first render is correct.
            self._invalidate_context_cache(keep_preloaded_orders=False)
            _switching_to_own = manager_id == self.user_data.get("id")
            if _switching_to_own:
                self._ensure_own_preloaded_sync()
            self.current_view_func(None)
            self.page.run_task(self._preload_home_orders_bg)
            self._schedule_workspace_save()
        except Exception:
            logger.exception("Manager context change failed")

    def build(self):
        try:
            self.page.window_maximized = True

            company_name = ""
            try:
                if self.company_id:
                    for row in self.db.get_all_companies():
                        if isinstance(row, dict) and row.get("id") == self.company_id:
                            company_name = row.get("name", "")
                            break
            except Exception:
                company_name = ""

            current_email = (self.user_data.get("email") or "").strip()
            self._upsert_saved_account(
                company_name,
                self.user_data.get("full_name", ""),
                current_email,
                self.user_data.get("phone", ""),
                role=self.user_data.get("role", ""),
                manager_id=self.user_data.get("id", ""),
                company_id=self.user_data.get("company_id", ""),
            )

            # Будуємо layout тільки один раз
            if self._layout_built:
                self._refresh_regions()
                self._refresh_managers()
                self._ensure_workspace_state_loaded()
                if not self._sync_poll_started:
                    self._sync_last_pulses = self._load_sync_snapshot()
                    self._sync_poll_started = True
                    self.page.run_task(self._sync_poll_loop)
                    self.page.run_task(self._home_auto_refresh_loop)
                if not self._gsm_tg_flush_loop_started:
                    self._gsm_tg_flush_loop_started = True
                    from gsm_parser_runner import gsm_local_tg_flush_loop

                    self.page.run_task(
                        gsm_local_tg_flush_loop, self.db, self._gsm_tg_flush_permitted
                    )
                self.page.run_task(self._background_warmup)
                self._refresh_workspace_tabs()
                self.page.update()
                self.page.run_task(self._startup_build_home)
                return

            self._layout_built = True
            self.page.clean()
            self.page.horizontal_alignment = ft.CrossAxisAlignment.START
            self.page.vertical_alignment = ft.MainAxisAlignment.START
            self.page.padding = ft.padding.all(0)

            self._refresh_managers()
            self._refresh_regions()
            self._ensure_workspace_state_loaded()

            _dash_manager_id = (self.user_data.get("id") or "").strip()
            _active_theme = _themes.get_active_theme(_dash_manager_id)
            _bar_color = _themes.get_bar_color(_active_theme)
            _th_muted = _active_theme["muted"]
            _th_text = _active_theme["text"]
            _th_bg = _active_theme["bg"]
            _th_panel = _active_theme["panel"]
            _th_border = _active_theme["border"]
            _th_chip_bg = _active_theme["chip_bg"]
            _th_icon = _active_theme["icon_color"]
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

            is_impersonation = bool(self.user_data.get("impersonated_by_superadmin"))

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
                allowed_reasons = {
                    "logout_button_click",
                    "account_switch_confirmed",
                    "system_backup_lock",
                    "session_kicked",
                }
                if reason not in allowed_reasons:
                    logger.error(
                        "[logout] blocked unexpected request | reason=%s | user_id=%s | active_view=%s",
                        reason,
                        self.user_data.get("id"),
                        getattr(
                            getattr(self.page, "_active_view", None),
                            "strip",
                            lambda: "",
                        )()
                        or getattr(self.page, "_active_view", ""),
                    )
                    return
                # Require explicit UI intent stamp; blocks accidental/implicit transitions.
                try:
                    intent_reason = str(
                        getattr(self.page, "_manual_logout_intent_reason", "") or ""
                    ).strip()
                    intent_ts = float(
                        getattr(self.page, "_manual_logout_intent_ts", 0.0) or 0.0
                    )
                    import time as _time

                    if intent_reason != reason or (_time.time() - intent_ts) > 8.0:
                        logger.error(
                            "[logout] blocked missing manual intent | reason=%s | active_view=%s",
                            reason,
                            getattr(
                                getattr(self.page, "_active_view", None),
                                "strip",
                                lambda: "",
                            )()
                            or getattr(self.page, "_active_view", ""),
                        )
                        return
                except Exception:
                    return
                self._logout_in_progress = True
                logger.debug(
                    "[logout] requested | reason=%s | user_id=%s | company_id=%s | active_view=%s",
                    reason,
                    self.user_data.get("id"),
                    self.user_data.get("company_id"),
                    getattr(
                        getattr(self.page, "_active_view", None), "strip", lambda: ""
                    )()
                    or getattr(self.page, "_active_view", ""),
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
                    logger.exception("Logout failed")
                finally:
                    try:
                        self.page._manual_logout_intent_reason = ""
                        self.page._manual_logout_intent_ts = 0.0
                    except Exception:
                        pass
                    self._logout_in_progress = False

            def _close_dialog(dialog):
                try:
                    self.page.pop_dialog()
                except Exception:
                    try:
                        dialog.open = False
                        self.page.update()
                    except Exception:
                        pass

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
                        ft.TextButton(
                            self.t("common.cancel", "Скасувати"), on_click=lambda __: _close_dialog(dlg)
                        ),
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
                try:
                    self.page.open(dlg)
                except Exception:
                    self.page.dialog = dlg
                    dlg.open = True
                    self.page.update()

            def open_account_chooser_dialog(_=None):
                logger.info(
                    "[acct-switch] redirecting to login screen instead of opening dashboard modal"
                )
                _confirm_switch_account(_)

            def back_to_superadmin(_):
                try:
                    impersonator = self.user_data.get("impersonator") or {}
                    if not isinstance(impersonator, dict) or not impersonator.get("id"):
                        return
                    from superadmin_dashboard import SuperAdminDashboard

                    SuperAdminDashboard(
                        self.page,
                        impersonator,
                        on_logout=self.on_logout,
                        on_switch_account=self.on_switch_account,
                    ).build()
                    self.page.update()
                except Exception:
                    logger.exception("Back to superadmin failed")

            from views.mobile_layout_helpers import (
                is_mobile as _is_mobile_device,
                is_mobile_landscape as _is_mobile_landscape,
                register_resize_listener as _register_resize_listener,
            )

            page_w = int(getattr(self.page, "width", 0) or 0)
            if page_w <= 0:
                win = getattr(self.page, "window", None)
                page_w = int(getattr(win, "width", 0) or 0) if win else 0
            # On a phone, always use the compact header sizing regardless of
            # orientation -- deriving it purely from page_w<980 meant a
            # landscape phone (width can pass 980 on some devices) would
            # switch to desktop-sized buttons/fonts that don't fit the much
            # shorter landscape height.
            is_small_header = bool(
                _is_mobile_device(self.page) or (page_w > 0 and page_w < 980)
            )

            title_size = 14 if is_small_header else 16
            subtitle_size = 11 if is_small_header else 12
            clock_size = 16 if is_small_header else 20
            date_size = 10 if is_small_header else 12
            button_h = 32 if is_small_header else 36
            # Mobile-only (never touches desktop/exe sizing, even a narrow
            # windowed one, which only gets is_small_header above): phone
            # screens are short enough that the stock 44x44 icon chips and
            # 12px row spacing alone made the header eat a large chunk of
            # the screen (see screenshots) — shrink those specifically.
            _is_mob = _is_mobile_device(self.page)
            _icon_box = 34 if _is_mob else 44
            _avatar_size = 28 if _is_mob else 40
            _header_row_spacing = 6 if _is_mob else 12

            menu_toggle_btn = ft.IconButton(
                icon=ft.Icons.MENU,
                icon_color=_th_icon,
                icon_size=20 if _is_mob else None,
                tooltip=self.t("nav.toggle_menu", "Показати/сховати меню"),
            )

            self._header_username_text_ref = ft.Text(
                self.user_data.get("full_name") or self.t("nav.default_user", "Користувач"),
                size=title_size,
                weight="bold",
                color=_th_text,
            )
            self._header_subtitle_text_ref = ft.Text(
                self.user_data.get("role") or self.t("nav.default_role", "Менеджер"),
                size=subtitle_size,
                color=_th_muted,
            )

            top_left = ft.Row(
                [
                    ft.Container(
                        content=menu_toggle_btn,
                        width=_icon_box,
                        height=_icon_box,
                        border_radius=10 if _is_mob else 12,
                        bgcolor=_th_chip_bg,
                    ),
                    ft.Icon(
                        ft.Icons.ACCOUNT_CIRCLE,
                        size=_avatar_size,
                        color=_th_icon,
                    ),
                    ft.Column(
                        [
                            self._header_username_text_ref,
                            self._header_subtitle_text_ref,
                            self._region_buttons_row,
                        ],
                        spacing=0,
                        tight=True,
                    ),
                    ft.IconButton(
                        icon=ft.Icons.HELP_OUTLINE,
                        icon_size=16 if _is_mob else 18,
                        icon_color=_th_icon,
                        tooltip=self.t("nav.help_tooltip", "Довідка"),
                        on_click=lambda _e: self._show_nav_help(),
                    ),
                ],
                spacing=_header_row_spacing,
                # On a squeezed mobile header (esp. landscape, where top_left
                # only gets a quarter of the row's width) this content can be
                # wider than its allotted space. A plain Row doesn't clip
                # overflow, so without wrap it renders past its bounds and
                # visually collides with top_center's clock ("?" over the
                # time) instead of just... being not wrapped.
                wrap=is_small_header,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
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

            if not getattr(self, "_backup_lock_watch_started", False):
                self._backup_lock_watch_started = True

                async def _watch_backup_lock():
                    # Independent of Config.SYNC_PULSES_ENABLED (unlike
                    # _sync_poll_loop) — a superadmin backup/restore must
                    # force out active sessions even when live sync polling
                    # is turned off. See is_backup_locked / _acquire_backup_lock.
                    while True:
                        await asyncio.sleep(10)
                        try:
                            cid = (self.company_id or "").strip()
                            if not cid:
                                continue
                            locked = await asyncio.to_thread(self.db.is_backup_locked, cid)
                            if not locked:
                                continue
                            with contextlib.suppress(Exception):
                                self.page.snack_bar = ft.SnackBar(
                                    content=ft.Text(
                                        self.t(
                                            "dashboard.backup_lock_kick",
                                            "Технічні роботи на сервері (резервне "
                                            "копіювання). Зачекайте кілька хвилин і "
                                            "увійдіть знову.",
                                        ),
                                        color="#FFFFFF",
                                        weight="bold",
                                    ),
                                    bgcolor="#B91C1C",
                                    duration=6000,
                                )
                                self.page.snack_bar.open = True
                                self.page.update()
                            _mark_manual_logout_intent("system_backup_lock")
                            do_logout(None, reason="system_backup_lock")
                            return
                        except Exception:
                            logger.exception("[backup-lock-watch] tick failed")

                self.page.run_task(_watch_backup_lock)

            if not getattr(self, "_session_kick_watch_started", False):
                self._session_kick_watch_started = True

                async def _watch_session_kick():
                    # Independent of Config.SYNC_PULSES_ENABLED (unlike
                    # _sync_poll_loop) — only one device may hold an active
                    # session per manager account; a login elsewhere must
                    # force this one out even with live sync polling off. See
                    # check_auth / get_manager_active_session_token.
                    if not self._my_session_token:
                        # This login never got a token (e.g. REST-backend
                        # mode, where get_manager_active_session_token is a
                        # no-op) — nothing to compare against, so skip
                        # entirely rather than false-triggering on every tick.
                        return
                    while True:
                        await asyncio.sleep(12)
                        try:
                            # Always the ACTUAL logged-in account — never
                            # self.current_manager_id, which is the "viewing
                            # as a subordinate" workspace-tab context and
                            # changes constantly during normal use, causing
                            # false kicks against an unrelated manager's token.
                            mid = str(self.user_data.get("id") or "").strip()
                            if not mid:
                                continue
                            current_token = await asyncio.to_thread(
                                self.db.get_manager_active_session_token, mid
                            )
                            if current_token is None:
                                # Lookup failed/unsupported this tick — skip,
                                # don't treat as a mismatch.
                                continue
                            if (
                                current_token
                                and current_token != self._my_session_token
                            ):
                                with contextlib.suppress(Exception):
                                    self.page.snack_bar = ft.SnackBar(
                                        content=ft.Text(
                                            self.t(
                                                "dashboard.session_kicked",
                                                "Ви увійшли в акаунт на іншому "
                                                "пристрої. Цю сесію завершено.",
                                            ),
                                            color="#FFFFFF",
                                            weight="bold",
                                        ),
                                        bgcolor="#B91C1C",
                                        duration=6000,
                                    )
                                    self.page.snack_bar.open = True
                                    self.page.update()
                                _mark_manual_logout_intent("session_kicked")
                                do_logout(None, reason="session_kicked")
                                return
                        except Exception:
                            logger.exception("[session-kick-watch] tick failed")

                self.page.run_task(_watch_session_kick)

            top_center = ft.Column(
                [
                    _clock_text,
                    ft.Text(datetime.now().strftime("%d.%m.%Y"), size=date_size, color=_th_muted),
                    self._manager_buttons_row,
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=1,
                tight=True,
            )

            _calc_btn = self._make_toolbar_btn(
                "calculator", ft.Icons.CALCULATE, self.t("nav.calculators", "Калькулятори"), self._open_calculator,
                _ref_attr="_calc_btn_ref",
            )
            _calc_btn.visible = self._is_feature_enabled("calculators")
            _notebook_btn = self._make_notebook_toolbar_btn()
            _notebook_btn.visible = self._is_feature_enabled("notebook")

            top_right = ft.Row(
                [
                    ft.FilledButton(
                        self.t("nav.switch_account", "Змінити акаунт"),
                        icon=ft.Icons.SWAP_HORIZ,
                        on_click=open_account_chooser_dialog,
                        tooltip=self.t("nav.switch_account_tooltip", "Повернутись до екрана входу"),
                        height=button_h,
                    ),
                    ft.Container(
                        content=ft.Row(
                            [
                                ft.Icon(
                                    ft.Icons.DESKTOP_WINDOWS,
                                    size=16,
                                    color=ft.Colors.ORANGE_900,
                                ),
                                ft.Text(
                                    self.t("nav.remote_mode", "Режим віддаленого доступу"),
                                    size=12,
                                    color=ft.Colors.ORANGE_900,
                                    weight="bold",
                                ),
                            ],
                            spacing=6,
                        ),
                        padding=ft.padding.symmetric(horizontal=10, vertical=6),
                        border_radius=10,
                        bgcolor=ft.Colors.ORANGE_50,
                        visible=is_impersonation,
                    ),
                    ft.Container(
                        content=ft.FilledButton(
                            self.t("nav.back_to_superadmin", "Повернутись до СуперАдміна"),
                            icon=ft.Icons.ADMIN_PANEL_SETTINGS,
                            on_click=back_to_superadmin,
                            height=button_h,
                        ),
                        visible=is_impersonation,
                    ),
                    _calc_btn,
                    _notebook_btn,
                    ft.Container(
                        content=ft.IconButton(
                            icon=ft.Icons.HOME_ROUNDED,
                            icon_color=_th_icon,
                            icon_size=18 if _is_mob else None,
                            tooltip=self.t("nav.home_tooltip", "Головна сторінка"),
                            on_click=self.show_home,
                        ),
                        width=_icon_box,
                        height=_icon_box,
                        border_radius=10 if _is_mob else 12,
                        bgcolor=_th_chip_bg,
                    ),
                    ft.Container(
                        content=ft.IconButton(
                            icon=ft.Icons.SETTINGS,
                            icon_color=_th_icon,
                            icon_size=18 if _is_mob else None,
                            tooltip=self.t("nav.settings_tooltip", "Налаштування"),
                            on_click=self.show_settings,
                        ),
                        width=_icon_box,
                        height=_icon_box,
                        border_radius=10 if _is_mob else 12,
                        bgcolor=_th_chip_bg,
                    ),
                    self._build_lang_switcher_box(_dash_manager_id, _th_chip_bg),
                    ft.Container(
                        content=ft.IconButton(
                            icon=ft.Icons.LOGOUT,
                            icon_color=ft.Colors.RED_700,
                            icon_size=18 if _is_mob else None,
                            tooltip=self.t("nav.logout_tooltip", "Вийти з акаунта"),
                            on_click=lambda _: (
                                _mark_manual_logout_intent("logout_button_click"),
                                do_logout(_, reason="logout_button_click"),
                            ),
                        ),
                        width=_icon_box,
                        height=_icon_box,
                        border_radius=10 if _is_mob else 12,
                        bgcolor="#FFF1F2",
                    ),
                ],
                spacing=6 if _is_mob else 10,
                run_spacing=6 if _is_mob else 8,
                wrap=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.END,
            )

            def _build_shell_header_row():
                # In mobile landscape the width comfortably fits all three
                # blocks side by side, but stays under the 980 'md' breakpoint
                # ResponsiveRow uses -- so left/center/right would otherwise
                # keep stacking full-width on top of each other (three extra
                # full-width rows), which is the main reason the shell ate
                # 80-90% of a short landscape screen. Lay them out in one Row
                # instead whenever we know it's a mobile device in landscape.
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

            _header_inner_column = ft.Column(
                [_build_shell_header_row()],
                spacing=6,
                tight=True,
            )

            header = ft.Container(
                content=_header_inner_column,
                padding=ft.padding.symmetric(
                    horizontal=14,
                    vertical=(4 if _is_mobile_landscape(self.page) else (8 if is_small_header else 10)),
                ),
                bgcolor=_bar_color,
                border_radius=15,
                margin=ft.margin.only(bottom=4),
            )
            self._header_container_ref = header

            def _on_shell_resized(_e=None):
                try:
                    if not _is_mobile_device(self.page):
                        return
                    _header_inner_column.controls = [_build_shell_header_row()]
                    header.padding = ft.padding.symmetric(
                        horizontal=14,
                        vertical=(4 if _is_mobile_landscape(self.page) else 8),
                    )
                    header.update()
                except Exception:
                    logger.exception("shell header resize reflow failed")

            _register_resize_listener(self.page, "shell_header", _on_shell_resized)

            module_controls = [
                self._menu_btn(ft.Icons.CATEGORY, self.t("nav.categories", "Категорії"), self.show_categories_brands, nav_key="categories"),
                self._menu_btn(ft.Icons.INVENTORY, self.t("nav.products", "Товари"), self.show_products, nav_key="products"),
                self._menu_btn(ft.Icons.LOCAL_SHIPPING, self.t("nav.suppliers", "Постачальники"), self.show_suppliers, nav_key="suppliers"),
            ]
            if self._is_feature_enabled("packaging"):
                module_controls.append(
                    self._menu_btn(ft.Icons.ALL_INBOX, self.t("nav.packaging", "Пакування"), self.show_packaging, nav_key="packaging")
                )
            if self._is_feature_enabled("files"):
                module_controls.append(
                    self._menu_btn(ft.Icons.FOLDER_COPY_OUTLINED, self.t("nav.files", "Файли"), self.show_files, nav_key="files")
                )
            if _is_admin_or_superadmin(self.user_data.get("role")):
                module_controls.append(
                    self._menu_btn(ft.Icons.CONTENT_COPY, self.t("nav.copy", "Копіювання"), self.show_copy_setup, nav_key="copy")
                )

            operations_controls = [
                self._menu_btn(ft.Icons.SHOPPING_CART, self.t("nav.orders", "Замовлення"), self.show_orders, cache_key="orders", nav_key="orders"),
            ]
            if self._is_feature_enabled("payments"):
                operations_controls.append(
                    self._menu_btn(ft.Icons.PAYMENTS, self.t("nav.payments", "Взаєморозрахунки"), self.show_payments, cache_key="payments", nav_key="payments")
                )
            if self._is_feature_enabled("cost_price_orders"):
                operations_controls.append(
                    self._menu_btn(ft.Icons.CALCULATE, self.t("nav.cost_orders", "С/В Замовлення"), self.show_cost_price_orders, cache_key="cost_price_orders", nav_key="cost_orders")
                )
            if self._is_feature_enabled("cost_price_stock"):
                operations_controls.append(
                    self._menu_btn(ft.Icons.WAREHOUSE, self.t("nav.cost_stock", "С/В Склад"), self.show_cost_price_stock, cache_key="cost_price_stock", nav_key="cost_stock")
                )
            if self._is_feature_enabled("pack_stock"):
                operations_controls.append(
                    self._menu_btn(ft.Icons.WAREHOUSE, self.t("nav.pack_stock", "Склад Пакування"), self.show_pack_stock, cache_key="pack_stock", nav_key="pack_stock")
                )

            _tools_btns = []
            if self._is_feature_enabled("media_plan"):
                _tools_btns.append(
                    self._menu_btn(ft.Icons.NEWSPAPER, self.t("nav.media", "Медіа План"), self.show_media_plan, cache_key="media_plan", nav_key="media")
                )
            if self._is_feature_enabled("schedule"):
                _tools_btns.append(
                    self._menu_btn(ft.Icons.EVENT_NOTE, self.t("nav.schedule", "Розклад Замовлень"), self.show_schedule, cache_key="schedule", nav_key="schedule")
                )
            media_plan_controls = ft.Column(
                _tools_btns,
                spacing=1,
                tight=True,
            )

            extensions_controls = []
            if self.db.is_company_gsm_parser_enabled(self.company_id):
                extensions_controls.append(
                    self._menu_btn(ft.Icons.SATELLITE_ALT, self.t("nav.gsm", "GSM Parser"), self.show_gsm_parser, nav_key="gsm")
                )

            sidebar_items = [
                ft.Container(
                    content=ft.Text(self.t("nav.section_modules", "МОДУЛІ"), size=10, weight="bold", color=_th_muted),
                    alignment=ft.Alignment(0, 0),
                    padding=ft.padding.only(bottom=3),
                ),
                *module_controls,
            ]
            if operations_controls:
                sidebar_items.extend(
                    [
                        ft.Divider(height=14, thickness=1, color=_th_border),
                        ft.Container(
                            content=ft.Text(
                                self.t("nav.section_operations", "ОПЕРАЦІЇ"), size=10, weight="bold", color=_th_muted
                            ),
                            alignment=ft.Alignment(0, 0),
                            padding=ft.padding.only(bottom=3),
                        ),
                        *operations_controls,
                    ]
                )
            if _tools_btns:
                sidebar_items.extend(
                    [
                        ft.Divider(height=14, thickness=1, color=_th_border),
                        ft.Container(
                            content=ft.Text(
                                self.t("nav.section_tools", "ІНСТРУМЕНТИ"), size=10, weight="bold", color=_th_muted
                            ),
                            alignment=ft.Alignment(0, 0),
                            padding=ft.padding.only(bottom=3),
                        ),
                        media_plan_controls,
                    ]
                )

            if extensions_controls:
                sidebar_items.extend(
                    [
                        ft.Divider(height=14, thickness=1, color=_th_border),
                        ft.Container(
                            content=ft.Text(
                                self.t("nav.section_extensions", "РОЗШИРЕННЯ"), size=10, weight="bold", color=_th_muted
                            ),
                            alignment=ft.Alignment(0, 0),
                            padding=ft.padding.only(bottom=3),
                        ),
                        *extensions_controls,
                    ]
                )

            # ── GSM Parser sidebar panel ───────────────────────────────────
            _parser_items = self._build_gsm_parser_panel_items()
            if _parser_items:
                sidebar_items.extend(_parser_items)

            _sidebar_col = ft.Column(
                sidebar_items, spacing=1, scroll=ft.ScrollMode.ADAPTIVE, expand=True
            )
            sidebar = ft.Container(
                width=216,
                padding=ft.padding.symmetric(vertical=6),
                visible=self._sidebar_visible,
                content=_sidebar_col,
            )
            self._sidebar_container = sidebar
            self._sidebar_col_ref = _sidebar_col

            if _is_mobile_device(self.page):
                # wrap=True (the desktop default, set once in __init__) makes
                # tab chips stack one-per-line the moment they don't fit a
                # phone's width — with several tabs open this alone can eat
                # most of the footer's height. A single horizontally
                # scrollable line is the standard mobile pattern (see
                # mobile_layout_helpers.make_scrollable_chip_row) and costs a
                # fixed, small height no matter how many tabs are open.
                self._workspace_tabs_row.wrap = False
                self._workspace_tabs_row.scroll = ft.ScrollMode.AUTO

            workspace_bar = ft.Container(
                bgcolor=_bar_color,
                border_radius=10,
                padding=ft.padding.symmetric(
                    horizontal=8, vertical=(2 if _is_mobile_device(self.page) else 4)
                ),
                content=ft.Row(
                    [
                        ft.Container(
                            padding=ft.padding.symmetric(horizontal=6, vertical=2),
                            border_radius=8,
                            bgcolor="#1D4ED8",
                            content=ft.Row(
                                [
                                    ft.TextButton(
                                        content=ft.Row(
                                            [
                                                ft.Icon(
                                                    ft.Icons.ADD,
                                                    size=13,
                                                    color=ft.Colors.WHITE,
                                                ),
                                                ft.Text(
                                                    self.t("nav.new_tab", "Нова сторінка"),
                                                    size=11,
                                                    color=ft.Colors.WHITE,
                                                ),
                                            ],
                                            spacing=4,
                                            tight=True,
                                        ),
                                        on_click=self._add_workspace,
                                        style=ft.ButtonStyle(
                                            padding=ft.padding.symmetric(
                                                horizontal=3, vertical=0
                                            ),
                                            overlay_color=ft.Colors.with_opacity(
                                                0.15, ft.Colors.WHITE
                                            ),
                                        ),
                                    ),
                                ],
                                spacing=1,
                                tight=True,
                            ),
                        ),
                        ft.VerticalDivider(width=1, color=_th_border),
                        self._workspace_tabs_row,
                    ],
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            )

            self._workspace_bar_container_ref = workspace_bar
            workspace_footer = ft.Container(
                padding=ft.padding.only(
                    left=0, right=0, bottom=(2 if _is_mob else 8)
                ),
                content=ft.SafeArea(
                    maintain_bottom_view_padding=True,
                    minimum_padding=ft.padding.only(bottom=2),
                    content=workspace_bar,
                ),
            )

            center_panel = ft.Column(
                [
                    self._view_sticky_header,
                    ft.Container(
                        content=self.content_area,
                        expand=True,
                        padding=ft.padding.only(
                            left=0, right=0, top=0, bottom=(2 if _is_mob else 8)
                        ),
                        bgcolor="transparent",
                        border_radius=15,
                        clip_behavior=ft.ClipBehavior.HARD_EDGE,
                    ),
                    self._view_h_scrollbar_slot,
                ],
                expand=True,
                spacing=0,
            )

            main_layout = ft.Row(
                controls=[
                    sidebar,
                    center_panel,
                ],
                spacing=6,
                expand=True,
                vertical_alignment=ft.CrossAxisAlignment.START,
            )

            # Attach sidebar toggle after controls exist in closure.
            self._sidebar_toggle_btn = menu_toggle_btn

            def _toggle_sidebar(_):
                self._set_sidebar_visible(not bool(self._sidebar_visible))

            menu_toggle_btn.on_click = _toggle_sidebar
            self._set_sidebar_visible(self._sidebar_visible)

            self._scaled_content = None
            self._main_layout_ref = main_layout
            _zoomable_container = ft.Container(
                content=main_layout,
                expand=True,
            )
            if _is_mobile_device(self.page):
                # Pinch-to-zoom — desktop keeps Ctrl+Scroll/Ctrl+±/0 only, since a
                # GestureDetector here would otherwise intercept two-finger
                # trackpad gestures too.
                self._zoomable = ft.GestureDetector(
                    content=_zoomable_container,
                    on_scale_start=self._on_mobile_scale_start,
                    on_scale_update=self._on_mobile_scale_update,
                    on_scale_end=self._on_mobile_scale_end,
                )
            else:
                self._zoomable = _zoomable_container
            self._zoom_h_scroller = None  # unused; keep for compatibility
            self._zoom_indicator_text = ft.Text(
                "100%", size=16, color="white", weight="bold"
            )
            self._zoom_indicator = ft.Container(
                content=self._zoom_indicator_text,
                bgcolor=ft.Colors.with_opacity(0.85, "#1E293B"),
                border_radius=10,
                padding=ft.padding.symmetric(horizontal=18, vertical=10),
                visible=False,
                bottom=24,
                right=24,
                shadow=ft.BoxShadow(
                    blur_radius=12, color=ft.Colors.with_opacity(0.3, "#000000")
                ),
            )
            self._workspace_footer_ref = workspace_footer
            self.page.add(
                self._wrap_with_background(
                    ft.Column(
                        controls=[
                            header,
                            ft.Stack(
                                controls=[self._zoomable, self._zoom_indicator],
                                expand=True,
                                clip_behavior=ft.ClipBehavior.HARD_EDGE,
                            ),
                            workspace_footer,
                        ],
                        expand=True,
                        spacing=2 if _is_mob else 6,
                        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                    )
                )
            )

            try:
                self.page._view_sticky_header = self._view_sticky_header
            except Exception:
                pass
            try:
                self.page._view_h_scrollbar_slot = self._view_h_scrollbar_slot
            except Exception:
                pass

            if not self._sync_poll_started:
                self._sync_last_pulses = self._load_sync_snapshot()
                self._sync_poll_started = True
                self.page.run_task(self._sync_poll_loop)
                self.page.run_task(self._home_auto_refresh_loop)
            if not self._gsm_tg_flush_loop_started:
                self._gsm_tg_flush_loop_started = True
                from gsm_parser_runner import gsm_local_tg_flush_loop

                self.page.run_task(
                    gsm_local_tg_flush_loop, self.db, self._gsm_tg_flush_permitted
                )

            self._refresh_workspace_tabs()
            self.page.run_task(self._background_warmup)
            self.page.run_task(self._startup_build_home)
            self._refresh_notebook_badge()
            # Expose a callback so Settings can refresh background without restart.
            try:
                setattr(
                    self.page, "_refresh_background_now", self._refresh_background_now
                )
            except Exception:
                pass
            # Reconnect callback: clears session verification flags so next resume()
            # forces a fresh DB pulse check on every cached tab.
            try:
                setattr(self.page, "_on_network_reconnect", self._on_network_reconnect)
            except Exception:
                pass
            # Global keyboard shortcuts
            self._search_overlay_stack = None
            self._search_results: list = []
            self._search_result_idx: int = 0
            self._search_query: str = ""
            self.page.on_keyboard_event = self._on_keyboard_event
            self.page.update()

            # Mobile: start at 65% so a phone screen shows meaningfully more
            # of a desktop-proportioned layout than 100% ever could — once,
            # on first build only (later tab switches restore whatever zoom
            # the user left each tab at, including one they pinched back up).
            if (
                not self._mobile_default_zoom_applied
                and _is_mobile_device(self.page)
            ):
                self._mobile_default_zoom_applied = True
                self._zoom_factor = 0.65
                self._zoom_apply_seq += 1
                self.page.run_task(self._apply_zoom_async, self._zoom_apply_seq)

        except Exception as ex:
            logger.exception("Dashboard build failed")
            self.page.add(ft.Text(f"Помилка в Dashboard: {ex}", color="red"))
            self.page.update()

    def _menu_btn(self, icon, text, action, cache_key: str = "", nav_key: str = ""):
        def _on_click(e=None):
            self._set_sidebar_visible(False)
            if cache_key:
                # For per-workspace views (orders/payments/cost_price_orders) the
                # cache key is workspace-specific; fall back to the bare key for
                # global views (pack_stock, cost_price_stock, etc.).
                _ws_cache_key = f"{cache_key}_{self._active_workspace_id}"
                _effective_key = (
                    _ws_cache_key if _ws_cache_key in self._view_cache else cache_key
                )
                _old = getattr(
                    self, f"_live_{cache_key.replace('-','_')}_view_ref", None
                )
                _ws_inst = (self._view_cache.get(_effective_key) or {}).get("instance")
                if _old is not None and _old is _ws_inst and hasattr(_old, "stop"):
                    with contextlib.suppress(Exception):
                        _old.stop()
                self._view_cache.pop(_effective_key, None)
                _w = self._cached_view_wrappers.get(_effective_key)
                if _w is not None:
                    _w.controls.clear()
            action(e)

        _view_name = getattr(action, "__name__", "show_home")

        def _rmb(e):
            _dlg = [None]

            def _close():
                with contextlib.suppress(Exception):
                    self.page.pop_dialog()

            def _open_here(_):
                _close()
                _on_click(None)

            def _open_new(_):
                _close()
                self._set_sidebar_visible(False)
                self._open_in_new_workspace(_view_name)

            _dlg[0] = ft.AlertDialog(
                modal=False,
                title=ft.Text(text, size=13, weight="bold"),
                content=ft.Column(
                    [
                        ft.TextButton(
                            content=ft.Row(
                                [ft.Icon(ft.Icons.OPEN_IN_BROWSER, size=16), ft.Text(self.t("nav.open_here", "Відкрити в цьому вікні"), size=13)],
                                spacing=8, tight=True,
                            ),
                            on_click=_open_here,
                            style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=12, vertical=8)),
                        ),
                        ft.TextButton(
                            content=ft.Row(
                                [ft.Icon(ft.Icons.OPEN_IN_NEW, size=16), ft.Text(self.t("nav.open_new_window", "Відкрити в новому вікні"), size=13)],
                                spacing=8, tight=True,
                            ),
                            on_click=_open_new,
                            style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=12, vertical=8)),
                        ),
                    ],
                    tight=True,
                    spacing=0,
                ),
                content_padding=ft.padding.only(left=8, right=8, bottom=8, top=4),
                title_padding=ft.padding.only(left=16, right=16, top=16, bottom=4),
            )
            with contextlib.suppress(Exception):
                self.page.open(_dlg[0])

        def _mmb(e):
            self._set_sidebar_visible(False)
            self._open_in_new_workspace(_view_name)

        # Resolve icon from active nav pack if nav_key is given.
        _manager_id = (self.user_data.get("id") or "").strip()
        _nav_pack = _themes.get_active_nav_pack(_manager_id)
        _pack_type = _nav_pack.get("type", "material")
        _btn_th = _themes.get_active_theme(_manager_id)
        _btn_text_clr = _btn_th["text"]
        _btn_icon_clr = _btn_th["icon_color"]
        _btn_bg = _btn_th["alt"]
        _btn_hover_bg = _btn_th["soft"]
        _btn_hover_clr = _btn_th["accent"]
        if nav_key:
            _raw_icon = _nav_pack.get(nav_key, icon)
        else:
            _raw_icon = icon
        if _pack_type == "text":
            icon_widget = ft.Text(str(_raw_icon), size=15, color=_btn_icon_clr)
        else:
            icon_widget = ft.Icon(_raw_icon, size=16, color=_btn_icon_clr)

        return ft.GestureDetector(
            on_secondary_tap=_rmb,
            on_tertiary_tap_up=_mmb,
            content=ft.TextButton(
                content=ft.Row([icon_widget, ft.Text(text, size=11, color=_btn_text_clr)], spacing=8),
                on_click=_on_click,
                width=209,
                style=ft.ButtonStyle(
                    shape=ft.RoundedRectangleBorder(radius=8),
                    padding=ft.padding.symmetric(horizontal=8, vertical=3),
                    color={
                        ft.ControlState.DEFAULT: _btn_text_clr,
                        ft.ControlState.HOVERED: _btn_hover_clr,
                    },
                    bgcolor={
                        ft.ControlState.DEFAULT: _btn_bg,
                        ft.ControlState.HOVERED: _btn_hover_bg,
                    },
                ),
            ),
        )

    def _stat_card(self, title, value, color):
        _th = _themes.get_active_theme((self.user_data.get("id") or "").strip())
        return ft.Container(
            content=ft.Row(
                [
                    ft.Column(
                        [
                            ft.Text(title, size=10, color=_th["muted"]),
                            ft.Text(value, size=14, weight="bold", color=color),
                        ],
                        expand=True,
                        spacing=2,
                    ),
                    ft.Icon(ft.Icons.CHEVRON_RIGHT, size=13, color=_th["muted"]),
                ]
            ),
            padding=ft.padding.all(8),
            bgcolor=_th["panel"],
            border=ft.border.all(1, _th["border"]),
            border_radius=8,
        )

    def _clear_and_update(self, title):
        # За замовчуванням content_area має вертикальний скрол.
        # Окремі вкладки (наприклад "Склад пакування") можуть вимикати його локально.
        try:
            self.content_area.scroll = ft.ScrollMode.ADAPTIVE
        except Exception:
            pass
        try:
            self._view_sticky_header.controls.clear()
        except Exception:
            pass
        self.content_area.controls.clear()
        _dash_mid = (self.user_data.get("id") or "").strip()
        _dash_th = _themes.get_active_theme(_dash_mid)
        self.content_area.controls.append(
            ft.Text(
                f"{title} ({self._current_region_label()}) - {self.current_manager_name}",
                size=24,
                weight="bold",
                color=_dash_th["text"],
            )
        )
        self.page.update()

    def _build_context_user(self):
        context_user = dict(self.user_data)
        context_user["active_manager_id"] = self.current_manager_id
        context_user["active_manager_name"] = self.current_manager_name
        # Views (e.g. Settings) need the current region context to read/write region-scoped config.
        context_user["active_region_id"] = self._current_region_for_query()
        context_user["active_region_label"] = self._current_region_label()
        context_user["bypass_category_access"] = _is_admin_or_superadmin(
            self.user_data.get("role")
        )
        context_user["subordinate_view_readonly"] = (
            self.current_general_mode
            or self.current_manager_id != self.user_data.get("id")
        )
        context_user["shared_orders_map"] = self._shared_orders_map
        context_user["shared_products_map"] = self._shared_products_map
        # "Загальні" mode context
        if self._work_orders_mode:
            context_user["general_mode"] = self.current_general_mode
            context_user["general_manager_ids"] = list(self.current_general_manager_ids)
        else:
            # Work orders mode OFF → force general mode, subordinates only (never own data).
            own_id = str(self.user_data.get("id") or "")
            all_sub_ids = [
                str(m.get("id") or "")
                for m in self._all_subordinates_flat
                if isinstance(m, dict) and m.get("id") and m.get("id") != own_id
            ]
            # Use saved selection filtered to exclude own ID; fall back to all subordinates.
            saved_sel = [
                sid for sid in self.current_general_manager_ids if sid != own_id
            ]
            selected = saved_sel if saved_sel else all_sub_ids
            context_user["general_mode"] = True
            context_user["general_manager_ids"] = selected
        # Full subordinate tree (for checkbox panel in home view)
        context_user["all_subordinates_flat"] = list(self._all_subordinates_flat)
        # Theme key for views that apply theming.
        _ctx_mgr_id = (self.user_data.get("id") or "").strip()
        context_user["ui_theme"] = _themes.get_active_theme_key(_ctx_mgr_id)
        context_user["ui_nav_pack"] = _themes.get_active_nav_pack_key(_ctx_mgr_id)
        return context_user

    # --- РњР•РўРћР”Р Р’Р†Р”РћР‘Р РђР–Р•РќРќРЇ Р РћР—Р”Р†Р›Р†Р’ ---

    def show_home(self, e):
        if not self._can_enter_view("show_home", e):
            return
        from views.home_view import HomeView

        def _ensure_new_workspace_from_home():
            ws = self._active_workspace()
            if not (isinstance(ws, dict) and bool(ws.get("fixed_home"))):
                return
            row = self._new_workspace("show_home")
            self._workspaces.append(row)
            self._active_workspace_id = row.get("id")
            self._apply_workspace_context(row)
            self._refresh_workspace_tabs()

        def _open_home_kpi_target(open_fn):
            def _runner():
                _ensure_new_workspace_from_home()
                open_fn()
                self._schedule_workspace_save()

            return self._with_view_switch_intent(_runner)

        def _open_schedule_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_schedule(None, quick_filter="need_to_order")
            )

        def _open_overdue_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_schedule(None, quick_filter="overdue")
            )

        def _open_orders_postprod_not_submitted_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_orders(
                    None, quick_filter="post_production_not_submitted"
                )
            )

        def _open_cp_orders_missing_logistics_fact_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_cost_price_orders(
                    None, quick_filter="non_production_missing_logistics_fact"
                )
            )

        def _open_orders_missing_status_date_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_orders(None, quick_filter="missing_status_date")
            )

        def _open_supplier_debt_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_payments(None, quick_filter="supplier_debt")
            )

        def _open_pending_series_to_product_from_home(_=None):
            return _open_home_kpi_target(lambda: self.show_products(None))

        def _open_packaging_supplier_debt_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_pack_stock(
                    None, quick_filter="packaging_supplier_debt"
                )
            )

        def _open_packaging_low_stock_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_pack_stock(None, quick_filter="packaging_low_stock")
            )

        def _open_media_plan_stale_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_media_plan(None, quick_filter="stale_posts")
            )

        def _open_media_plan_schedule_from_home(_=None):
            # Force fresh build without any stale-posts filter from cache.
            return _open_home_kpi_target(
                lambda: self.show_media_plan(None, quick_filter="clear")
            )

        def _open_categories_missing_target_margin_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_categories_brands(
                    None, quick_filter="missing_target_margin"
                )
            )

        def _open_incomplete_products_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_products(None, quick_filter="incomplete_products")
            )

        def _open_incomplete_suppliers_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_suppliers(None, quick_filter="incomplete_suppliers")
            )

        def _open_orders_missing_fact_weight_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_cost_price_orders(
                    None, quick_filter="missing_fact_weight_non_production"
                )
            )

        def _open_all_suppliers_from_home(_=None):
            return _open_home_kpi_target(lambda: self.show_suppliers(None))

        def _open_all_categories_from_home(_=None):
            return _open_home_kpi_target(lambda: self.show_categories_brands(None))

        def _open_all_products_from_home(_=None):
            return _open_home_kpi_target(lambda: self.show_products(None))

        def _open_all_packaging_from_home(_=None):
            return _open_home_kpi_target(lambda: self.show_packaging(None))

        def _open_orders_status_production_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_orders(None, quick_filter="status:production")
            )

        def _open_orders_status_ready_for_shipment_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_orders(None, quick_filter="status:ready_for_shipment")
            )

        def _open_orders_status_in_transit_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_orders(None, quick_filter="status:in_transit")
            )

        def _open_orders_status_at_warehouse_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_orders(None, quick_filter="status:at_warehouse")
            )

        def _open_orders_status_accepted_from_home(_=None):
            return _open_home_kpi_target(
                lambda: self.show_orders(None, quick_filter="status:accepted")
            )

        try:
            self._show_home_render(
                HomeView,
                on_open_need_to_order=_open_schedule_from_home,
                on_open_overdue=_open_overdue_from_home,
                on_open_orders_postprod_not_submitted=_open_orders_postprod_not_submitted_from_home,
                on_open_cp_orders_missing_logistics_fact=_open_cp_orders_missing_logistics_fact_from_home,
                on_open_orders_missing_status_date=_open_orders_missing_status_date_from_home,
                on_open_supplier_debt=_open_supplier_debt_from_home,
                on_open_pending_series_to_product=_open_pending_series_to_product_from_home,
                on_open_packaging_supplier_debt=_open_packaging_supplier_debt_from_home,
                on_open_packaging_low_stock=_open_packaging_low_stock_from_home,
                on_open_media_plan_stale=_open_media_plan_stale_from_home,
                on_open_media_plan_schedule=_open_media_plan_schedule_from_home,
                on_open_categories_missing_target_margin=_open_categories_missing_target_margin_from_home,
                on_open_incomplete_products=_open_incomplete_products_from_home,
                on_open_incomplete_suppliers=_open_incomplete_suppliers_from_home,
                on_open_orders_missing_fact_weight=_open_orders_missing_fact_weight_from_home,
                on_open_all_suppliers=_open_all_suppliers_from_home,
                on_open_all_categories=_open_all_categories_from_home,
                on_open_all_products=_open_all_products_from_home,
                on_open_all_packaging=_open_all_packaging_from_home,
                on_open_orders_status_production=_open_orders_status_production_from_home,
                on_open_orders_status_ready_for_shipment=_open_orders_status_ready_for_shipment_from_home,
                on_open_orders_status_in_transit=_open_orders_status_in_transit_from_home,
                on_open_orders_status_at_warehouse=_open_orders_status_at_warehouse_from_home,
                on_open_orders_status_accepted=_open_orders_status_accepted_from_home,
            )
        except Exception:
            logger.exception("[home] show_home failed — rendering retry panel instead of a blank tab")
            with contextlib.suppress(Exception):
                _wrapper = self._cached_view_wrappers.get("home") or self._get_or_create_cached_wrapper("home")
                _wrapper.controls.clear()

                def _retry(_e=None):
                    with contextlib.suppress(Exception):
                        self.show_home(None)

                _wrapper.controls.append(
                    ft.Column(
                        [
                            ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=30, color="#EF4444"),
                            ft.Text(
                                "Помилка завантаження вкладки",
                                size=14,
                                weight="bold",
                                color="#EF4444",
                            ),
                            ft.ElevatedButton("Оновити", on_click=_retry),
                        ],
                        spacing=8,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )
                for _kk, _ww in self._cached_view_wrappers.items():
                    _ww.visible = _kk == "home"
                _wrapper.update()
            with contextlib.suppress(Exception):
                self.page.update()

    def _show_home_render(self, HomeView, **home_callbacks) -> None:
        # Fast path: show cached home view instantly, then refresh data in background
        _home_wrapper = self._cached_view_wrappers.get("home")
        if _home_wrapper is not None and len(_home_wrapper.controls) > 0:
            _home_inst = (self._view_cache.get("home") or {}).get("instance")
            self._detach_live_mirror_views_for("home")
            self._set_view_for_workspace("show_home")
            try:
                self.page._active_view = "show_home"
            except Exception:
                pass
            self._scratch_col.visible = False
            for _kk, _ww in self._cached_view_wrappers.items():
                _ww.visible = _kk == "home"
            _sticky = getattr(self.page, "_view_sticky_header", None)
            if _sticky is not None:
                _sticky.controls.clear()
            self.page.update()
            if _home_inst is not None:
                try:
                    self.page.run_task(_home_inst.resume)
                except Exception:
                    pass
            return

        self._detach_live_mirror_views_for("home")
        self._set_view_for_workspace("show_home")
        try:
            self.page._active_view = "show_home"
        except Exception:
            pass
        _home_wrapper = self._get_or_create_cached_wrapper("home")
        _home_wrapper.controls.clear()
        self._activate_cached_view("home")
        with contextlib.suppress(Exception):
            self.page.update()
        hv = HomeView(
            _home_wrapper,
            self._build_context_user(),
            self.db,
            self._current_region_label(),
            active_region=self._current_region_for_query(),
            get_orders_override=self._get_shared_orders,
            on_general_selection_changed=self._on_general_selection_changed,
            **home_callbacks,
        )
        self._live_home_view_ref = hv
        self._view_cache["home"] = {"instance": hv}
        # Always force a fresh DB read on the first build so counters are correct
        # immediately — the snapshot/shared-map may have stale paid_amount data
        # (payments_view updates orders in DB but not in the orders_view memory).
        # This mirrors what resume() does on every navigation back to home.
        hv._force_fresh_from_db = True
        self._safe_build(hv, _home_wrapper, self.show_home)
        self.page.update()

    # ── Toolbar icon buttons (calculator / notebook) ─────────────────────────

    def _make_toolbar_btn(self, nav_key: str, fallback_icon, tooltip: str, on_click, *, _ref_attr: str = "") -> ft.Container:
        """Build a themed toolbar icon button and optionally store it in self.<_ref_attr>."""
        _mid = (self.user_data.get("id") or "").strip()
        th       = _themes.get_active_theme(_mid)
        pack     = _themes.get_active_nav_pack(_mid)
        pack_type = pack.get("type", "material")
        raw      = pack.get(nav_key, fallback_icon)
        icon_clr = th.get("icon_color", ft.Colors.BLUE_900)
        btn_bg   = th.get("soft", "#EFF4FF")
        if pack_type == "text":
            btn = ft.Container(
                content=ft.Text(str(raw), size=18),
                tooltip=tooltip,
                on_click=on_click,
                alignment=ft.Alignment(0, 0),
                ink=True,
                border_radius=8,
            )
        else:
            btn = ft.IconButton(icon=raw, icon_color=icon_clr, tooltip=tooltip, on_click=on_click)
        container = ft.Container(content=btn, width=44, height=44, border_radius=12, bgcolor=btn_bg)
        if _ref_attr:
            setattr(self, _ref_attr, container)
        return container

    def _safe_build(self, view_inst, wrapper: ft.Column, reload_fn=None) -> bool:
        """Call view_inst.build(); on any exception show an error panel instead of a blank screen."""
        try:
            view_inst.build()
            return True
        except Exception as _exc:
            logger.exception("View build failed")
            with contextlib.suppress(Exception):
                wrapper.controls.clear()
                def _retry(_e=None, _fn=reload_fn):
                    with contextlib.suppress(Exception):
                        if callable(_fn):
                            _fn(None)
                wrapper.controls.append(
                    ft.Column(
                        [
                            ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=30, color="#EF4444"),
                            ft.Text(
                                "Помилка завантаження вкладки",
                                size=14,
                                weight="bold",
                                color="#EF4444",
                            ),
                            ft.Text(str(_exc)[:300], size=11, color="grey600"),
                            ft.ElevatedButton("Оновити", on_click=_retry),
                        ],
                        spacing=8,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )
                wrapper.update()
            with contextlib.suppress(Exception):
                self.page.update()
            return False

    def _make_notebook_toolbar_btn(self) -> ft.Stack:
        _mid = (self.user_data.get("id") or "").strip()
        th        = _themes.get_active_theme(_mid)
        pack      = _themes.get_active_nav_pack(_mid)
        pack_type = pack.get("type", "material")
        raw       = pack.get("notebook", ft.Icons.MENU_BOOK)
        icon_clr  = th.get("icon_color", ft.Colors.BLUE_900)
        btn_bg    = th.get("soft", "#EFF4FF")
        _notebook_tooltip = self.t("nav.notebook_tooltip", "Блокнот")
        if pack_type == "text":
            btn = ft.Container(
                content=ft.Text(str(raw), size=18),
                tooltip=_notebook_tooltip,
                on_click=self._open_notebook,
                alignment=ft.Alignment(0, 0),
                ink=True,
                border_radius=8,
            )
        else:
            btn = ft.IconButton(icon=raw, icon_color=icon_clr, tooltip=_notebook_tooltip, on_click=self._open_notebook)
        self._notebook_btn_ref = ft.Container(content=btn, width=44, height=44, border_radius=12, bgcolor=btn_bg)

        today_n, overdue_n = get_notebook_badge_counts(
            self.db, _mid, self.company_id or ""
        )

        today_badge = ft.Container(
            content=ft.Text(
                str(today_n) if today_n else "",
                size=8, color="#000000",
                weight=ft.FontWeight.W_700,
                text_align=ft.TextAlign.CENTER,
            ),
            bgcolor="#FFD600",
            border_radius=8,
            height=16,
            padding=ft.padding.symmetric(horizontal=3),
            alignment=ft.alignment.Alignment(0, 0),
            top=1, right=1,
            visible=today_n > 0,
        )
        overdue_badge = ft.Container(
            content=ft.Text(
                str(overdue_n) if overdue_n else "",
                size=8, color="#FFFFFF",
                weight=ft.FontWeight.W_700,
                text_align=ft.TextAlign.CENTER,
            ),
            bgcolor="#E53935",
            border_radius=8,
            height=16,
            padding=ft.padding.symmetric(horizontal=3),
            alignment=ft.alignment.Alignment(0, 0),
            bottom=1, right=1,
            visible=overdue_n > 0,
        )
        self._notebook_badge_today_cont = today_badge
        self._notebook_badge_overdue_cont = overdue_badge
        return ft.Stack([self._notebook_btn_ref, today_badge, overdue_badge], width=44, height=44)

    def _refresh_notebook_badge(self) -> None:
        if self._notebook_badge_today_cont is None:
            return
        _mid = (self.user_data.get("id") or "").strip()
        today_n, overdue_n = get_notebook_badge_counts(
            self.db, _mid, self.company_id or ""
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
        if not self._is_feature_enabled("calculators"):
            return
        open_calculators_modal(
            self.page, self.db,
            manager_id=str(self.current_manager_id or self.user_data.get("id") or ""),
            company_id=str(self.company_id or ""),
            region_id=str(self._current_region_for_query() or ""),
            regions_list=[
                str(rb.data)
                for rb in (self._region_buttons_row.controls or [])
                if rb.data and str(rb.data) not in ("", ALL_REGIONS_KEY)
            ] or [str(self._current_region_for_query() or "")],
        )

    def _open_notebook(self, _=None) -> None:
        if not self._is_feature_enabled("notebook"):
            return
        open_notebook_modal(
            self.page, self.db,
            manager_id=str(self.user_data.get("id") or ""),
            on_close=self._refresh_notebook_badge,
            company_id=str(self.company_id or ""),
        )

    def _update_toolbar_btns(self) -> None:
        """Refresh calculator and notebook toolbar buttons after theme/pack change."""
        _mid      = (self.user_data.get("id") or "").strip()
        th        = _themes.get_active_theme(_mid)
        pack      = _themes.get_active_nav_pack(_mid)
        pack_type = pack.get("type", "material")
        icon_clr  = th.get("icon_color", ft.Colors.BLUE_900)
        btn_bg    = th.get("soft", "#EFF4FF")
        for ref, nav_key, fallback, tooltip, handler in [
            (self._calc_btn_ref,     "calculator", ft.Icons.CALCULATE, "Калькулятори", self._open_calculator),
            (self._notebook_btn_ref, "notebook",   ft.Icons.MENU_BOOK, "Блокнот",      self._open_notebook),
        ]:
            if ref is None:
                continue
            raw = pack.get(nav_key, fallback)
            if pack_type == "text":
                btn = ft.Container(
                    content=ft.Text(str(raw), size=18),
                    tooltip=tooltip,
                    on_click=handler,
                    alignment=ft.Alignment(0, 0),
                    ink=True,
                    border_radius=8,
                )
            else:
                btn = ft.IconButton(icon=raw, icon_color=icon_clr, tooltip=tooltip, on_click=handler)
            ref.content = btn
            ref.bgcolor = btn_bg
            with contextlib.suppress(Exception):
                ref.update()

    def _build_gsm_parser_panel_items(self) -> list:
        """Sidebar 'ПАРСЕР' news panel. Extracted from build() so it can also
        be rebuilt live from _refresh_sidebar_for_feature_change()."""
        try:
            if not self.db.is_company_gsm_parser_enabled(self.company_id):
                return []
            _mid = self.current_manager_id or ""
            _uprefs = self.db.get_gsm_parser_user_prefs(_mid) if _mid else {}
            if not bool(_uprefs.get("enabled", True)):
                return []

            _pnl_mid = (self.user_data.get("id") or "").strip()
            _pnl_th = _themes.get_active_theme(_pnl_mid)
            _th_text = _pnl_th["text"]
            _th_icon = _pnl_th["icon_color"]
            _th_muted = _pnl_th["muted"]
            _th_border = _pnl_th["border"]
            _th_panel = _pnl_th["panel"]

            _sdata = self.db.get_gsm_parser_sidebar_data(
                brand_filter=_uprefs.get("brands", "all"),
                device_type_filter=_uprefs.get("device_types", "all"),
                limit=30,
            )

            _page_ref = self.page

            def _open_card(e, _did):
                from views.gsm_parser_view import GsmParserView

                async def _do():
                    dev = await asyncio.to_thread(
                        self.db.get_gsm_merged_device_by_id, _did
                    )
                    if not dev:
                        return
                    _gv = GsmParserView(None, self._build_context_user(), self.db)
                    _gv._device_detail_modal(_page_ref, dev)

                if _page_ref and hasattr(_page_ref, "run_task"):
                    _page_ref.run_task(_do)

            def _parser_sub(title: str, items: list, icon):
                # No devices in this category since the last one — don't
                # render the block at all (not even an empty-state line).
                # Only categories that actually have something to show up
                # take up space in the panel.
                if not items:
                    return None
                _SCROLL_H = 90  # ~3 single-line items visible; wrapping items scroll
                item_widgets = [
                    ft.TextButton(
                        content=ft.Text(
                            str(it.get("model_name") or "—"),
                            size=11,
                            no_wrap=False,
                            color=_th_text,
                        ),
                        style=ft.ButtonStyle(
                            padding=ft.padding.symmetric(horizontal=4, vertical=2),
                            overlay_color=ft.Colors.with_opacity(0.08, _th_icon),
                        ),
                        on_click=lambda e, did=str(it.get("id") or ""): _open_card(e, did),
                    )
                    for it in items
                ]
                body = ft.Container(
                    height=_SCROLL_H,
                    padding=ft.padding.only(left=2, right=2, bottom=2),
                    content=ft.Column(
                        item_widgets,
                        spacing=0,
                        tight=True,
                        scroll=ft.ScrollMode.ADAPTIVE,
                    ),
                )
                return ft.Container(
                    border=ft.border.only(
                        left=ft.BorderSide(3, _th_icon),
                        top=ft.BorderSide(1, _th_border),
                        right=ft.BorderSide(1, _th_border),
                        bottom=ft.BorderSide(1, _th_border),
                    ),
                    border_radius=8,
                    bgcolor=_th_panel,
                    margin=ft.margin.symmetric(vertical=3),
                    padding=ft.padding.only(left=7, right=4, top=5, bottom=3),
                    content=ft.Column(
                        [
                            ft.Row(
                                [
                                    ft.Icon(icon, size=12, color=_th_icon),
                                    ft.Text(
                                        title,
                                        size=9,
                                        weight="bold",
                                        color=_th_muted,
                                    ),
                                ],
                                spacing=5,
                            ),
                            body,
                        ],
                        spacing=2,
                        tight=True,
                    ),
                )

            _panels = [
                p
                for p in (
                    _parser_sub(
                        self.t("gsm.sidebar.gsm_new", "Нові GSM Arena:"),
                        _sdata.get("gsm_new") or [],
                        ft.Icons.SMARTPHONE,
                    ),
                    _parser_sub(
                        self.t("gsm.sidebar.ek_new", "Нові Єкаталог:"),
                        _sdata.get("ek_new") or [],
                        ft.Icons.STOREFRONT,
                    ),
                    _parser_sub(
                        self.t("gsm.sidebar.appeared_sale", "З'явились в продажу:"),
                        _sdata.get("appeared_for_sale") or [],
                        ft.Icons.NEW_RELEASES,
                    ),
                    _parser_sub(
                        self.t("gsm.sidebar.regions_appeared", "З'явились регіони:"),
                        _sdata.get("regions_appeared") or [],
                        ft.Icons.PUBLIC,
                    ),
                )
                if p is not None
            ]
            # Nothing new anywhere — don't show a bare "ПАРСЕР" header with
            # no content under it.
            if not _panels:
                return []
            return [
                ft.Divider(height=14, thickness=1, color=_th_border),
                ft.Container(
                    content=ft.Text(
                        self.t("nav.section_parser", "ПАРСЕР"),
                        size=10,
                        weight="bold",
                        color=_th_muted,
                    ),
                    alignment=ft.Alignment(0, 0),
                    padding=ft.padding.only(bottom=3),
                ),
                *_panels,
            ]
        except Exception:
            return []

    # (feature_key -> show_X view function name) for tabs that are gated by
    # TOGGLEABLE_FEATURES / GSM Parser and must bounce back to their own
    # disabled-state guard when the feature is turned off while open.
    _FEATURE_TO_VIEW_FN = {
        "packaging": "show_packaging",
        "files": "show_files",
        "payments": "show_payments",
        "cost_price_orders": "show_cost_price_orders",
        "cost_price_stock": "show_cost_price_stock",
        "pack_stock": "show_pack_stock",
        "media_plan": "show_media_plan",
        "schedule": "show_schedule",
    }

    def _refresh_sidebar_for_feature_change(self) -> None:
        """Rebuilds the sidebar (module/operations/tools/extensions buttons +
        GSM parser panel) live, without a restart, when a feature toggle or
        the GSM Parser company/user switch changes. Reuses _apply_nav_pack_live's
        button-list rebuild by re-applying the manager's own current nav pack."""
        manager_id = (self.user_data.get("id") or "").strip()
        pack_key = _themes.get_active_nav_pack_key(manager_id)
        self._apply_nav_pack_live(pack_key)
        col = getattr(self, "_sidebar_col_ref", None)
        if col is None:
            return
        with contextlib.suppress(Exception):
            col.controls += self._build_gsm_parser_panel_items()
            col.update()

    def _handle_extensions_scope_change(self) -> None:
        """Called when a sync pulse reports the 'extensions' scope changed
        (feature toggles / GSM Parser enabled-disabled). Refreshes the
        sidebar live and, if the currently open tab belongs to a feature
        that just got disabled, re-triggers its own entry guard (which
        already shows the "Вкладку вимкнено" message)."""
        self._refresh_sidebar_for_feature_change()
        active_vn = getattr(self.current_view_func, "__name__", "")
        for feat_key, fn_name in self._FEATURE_TO_VIEW_FN.items():
            if active_vn == fn_name and not self._is_feature_enabled(feat_key):
                with contextlib.suppress(Exception):
                    getattr(self, fn_name)(None)
                return
        if active_vn == "show_gsm_parser" and not self.db.is_company_gsm_parser_enabled(
            self.company_id
        ):
            with contextlib.suppress(Exception):
                self.show_gsm_parser(None)

    def _apply_nav_pack_live(self, pack_key: str) -> None:
        """Called by settings when user picks a nav pack — rebuilds sidebar icons immediately."""
        manager_id = (self.user_data.get("id") or "").strip()
        _themes.set_active_nav_pack(manager_id, pack_key)
        self._update_toolbar_btns()
        col = getattr(self, "_sidebar_col_ref", None)
        if col is None:
            return
        # Rebuild sidebar items with new pack icons.
        col.controls.clear()
        _sb_mid = (self.user_data.get("id") or "").strip()
        _sb_th = _themes.get_active_theme(_sb_mid)
        _sb_muted = _sb_th["muted"]
        _sb_border = _sb_th["border"]
        _is_admin = _is_admin_or_superadmin(self.user_data.get("role"))
        _gsm_on = self.db.is_company_gsm_parser_enabled(self.company_id)
        module_controls = [
            self._menu_btn(ft.Icons.CATEGORY, "Категорії", self.show_categories_brands, nav_key="categories"),
            self._menu_btn(ft.Icons.INVENTORY, "Товари", self.show_products, nav_key="products"),
            self._menu_btn(ft.Icons.LOCAL_SHIPPING, "Постачальники", self.show_suppliers, nav_key="suppliers"),
        ]
        if self._is_feature_enabled("packaging"):
            module_controls.append(self._menu_btn(ft.Icons.ALL_INBOX, "Пакування", self.show_packaging, nav_key="packaging"))
        if self._is_feature_enabled("files"):
            module_controls.append(self._menu_btn(ft.Icons.FOLDER_COPY_OUTLINED, "Файли", self.show_files, nav_key="files"))
        if _is_admin:
            module_controls.append(self._menu_btn(ft.Icons.CONTENT_COPY, "Копіювання", self.show_copy_setup, nav_key="copy"))
        operations_controls = [
            self._menu_btn(ft.Icons.SHOPPING_CART, "Замовлення", self.show_orders, cache_key="orders", nav_key="orders"),
        ]
        if self._is_feature_enabled("payments"):
            operations_controls.append(self._menu_btn(ft.Icons.PAYMENTS, "Взаєморозрахунки", self.show_payments, cache_key="payments", nav_key="payments"))
        if self._is_feature_enabled("cost_price_orders"):
            operations_controls.append(self._menu_btn(ft.Icons.CALCULATE, "С/В Замовлення", self.show_cost_price_orders, cache_key="cost_price_orders", nav_key="cost_orders"))
        if self._is_feature_enabled("cost_price_stock"):
            operations_controls.append(self._menu_btn(ft.Icons.WAREHOUSE, "С/В Склад", self.show_cost_price_stock, cache_key="cost_price_stock", nav_key="cost_stock"))
        if self._is_feature_enabled("pack_stock"):
            operations_controls.append(self._menu_btn(ft.Icons.WAREHOUSE, "Склад Пакування", self.show_pack_stock, cache_key="pack_stock", nav_key="pack_stock"))
        _tools_btns = []
        if self._is_feature_enabled("media_plan"):
            _tools_btns.append(self._menu_btn(ft.Icons.NEWSPAPER, "Медіа План", self.show_media_plan, cache_key="media_plan", nav_key="media"))
        if self._is_feature_enabled("schedule"):
            _tools_btns.append(self._menu_btn(ft.Icons.EVENT_NOTE, "Розклад Замовлень", self.show_schedule, cache_key="schedule", nav_key="schedule"))
        col.controls += [
            ft.Container(content=ft.Text("МОДУЛІ", size=10, weight="bold", color=_sb_muted), alignment=ft.Alignment(0, 0), padding=ft.padding.only(bottom=3)),
            *module_controls,
            ft.Divider(height=14, thickness=1, color=_sb_border),
            ft.Container(content=ft.Text("ОПЕРАЦІЇ", size=10, weight="bold", color=_sb_muted), alignment=ft.Alignment(0, 0), padding=ft.padding.only(bottom=3)),
            *operations_controls,
        ]
        if _tools_btns:
            col.controls += [
                ft.Divider(height=14, thickness=1, color=_sb_border),
                ft.Container(content=ft.Text("ІНСТРУМЕНТИ", size=10, weight="bold", color=_sb_muted), alignment=ft.Alignment(0, 0), padding=ft.padding.only(bottom=3)),
                ft.Column(_tools_btns, spacing=1, tight=True),
            ]
        if _gsm_on:
            col.controls += [
                ft.Divider(height=14, thickness=1, color=_sb_border),
                ft.Container(content=ft.Text("РОЗШИРЕННЯ", size=10, weight="bold", color=_sb_muted), alignment=ft.Alignment(0, 0), padding=ft.padding.only(bottom=3)),
                self._menu_btn(ft.Icons.SATELLITE_ALT, "GSM Parser", self.show_gsm_parser, nav_key="gsm"),
            ]
        with contextlib.suppress(Exception):
            col.update()

    def _show_nav_help(self) -> None:
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(self.t("nav.help_title", "Довідка — навігація")),
            content=ft.Container(
                width=420,
                content=ft.Text(
                    self.t(
                        "nav.help_body",
                        "Ліворуч — меню розділів (товари, замовлення, склад тощо),"
                        " кнопкою-«гамбургером» його можна сховати/показати. Вгорі"
                        " праворуч — калькулятори, блокнот, головна сторінка,"
                        " налаштування, перемикач мови та кнопка виходу. По центру"
                        " — поточні дата й час. Клік правою кнопкою миші на пункті"
                        " меню відкриває його в новому вікні.",
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
        try:
            self.page.open(dlg)
        except Exception:
            self.page.dialog = dlg
            dlg.open = True
            self.page.update()

    def _build_lang_switcher_box(self, manager_id: str, bg_color: str) -> ft.Container:
        box = i18n.build_language_switcher(
            self.db,
            lambda code: self._apply_language_live(code),
            manager_id=manager_id,
            bg_color=bg_color,
        )
        self._lang_switcher_box_ref = box
        return box

    def _apply_language_live(self, lang_code: str) -> None:
        """Called when the user picks a language from the switcher — saves
        it, refreshes the switcher button itself, and instantly reloads the
        current tab in the new language. Other cached tabs are invalidated
        so they pick up the new language next time they're opened."""
        manager_id = (self.user_data.get("id") or "").strip()
        i18n.save_manager_language(self.db, manager_id, lang_code)
        try:
            _active_theme = _themes.get_active_theme(manager_id)
            _th_chip_bg = _active_theme["chip_bg"]
            box = getattr(self, "_lang_switcher_box_ref", None)
            if box is not None:
                fresh = i18n.build_language_switcher(
                    self.db,
                    lambda code: self._apply_language_live(code),
                    manager_id=manager_id,
                    bg_color=_th_chip_bg,
                )
                box.content = fresh.content
                box.update()
        except Exception:
            pass
        # Drop every cached view instance so each re-renders with the new
        # language next time it's opened. Wrapper Columns must be removed
        # from content_area.controls explicitly here — clearing only the
        # dict orphans them in the tree forever (still attached, just
        # unreferenced), so they keep piling up on every switch and bloat
        # the next full page.update() into a multi-second freeze.
        for wrapper in self._cached_view_wrappers.values():
            with contextlib.suppress(Exception):
                self.content_area.controls.remove(wrapper)
        self._view_cache.clear()
        self._cached_view_wrappers.clear()

        async def _reload_current_view() -> None:
            with contextlib.suppress(Exception):
                view_func = self.current_view_func
                if callable(view_func):
                    view_func(None)

        with contextlib.suppress(Exception):
            self.page.run_task(_reload_current_view)

    def _apply_theme_live(self, theme_key: str) -> None:
        """Called by settings when user picks a new theme — rebuilds bars and current view."""
        manager_id = (self.user_data.get("id") or "").strip()
        _themes.set_active_theme(manager_id, theme_key)
        theme = _themes.get_active_theme(manager_id)
        bar_color = _themes.get_bar_color(theme)
        # Update dashboard bar containers if they exist.
        try:
            if getattr(self, "_header_container_ref", None) is not None:
                self._header_container_ref.bgcolor = bar_color
                self._header_container_ref.update()
        except Exception:
            pass
        try:
            if getattr(self, "_workspace_bar_container_ref", None) is not None:
                self._workspace_bar_container_ref.bgcolor = bar_color
                self._workspace_bar_container_ref.update()
        except Exception:
            pass
        # Update header text colors on live theme change.
        try:
            if getattr(self, "_header_username_text_ref", None) is not None:
                self._header_username_text_ref.color = theme["text"]
                self._header_username_text_ref.update()
        except Exception:
            pass
        try:
            if getattr(self, "_header_subtitle_text_ref", None) is not None:
                self._header_subtitle_text_ref.color = theme["muted"]
                self._header_subtitle_text_ref.update()
        except Exception:
            pass
        try:
            if getattr(self, "_clock_text", None) is not None:
                self._clock_text.color = theme["text"]
                self._clock_text.update()
        except Exception:
            pass
        try:
            self.page.bgcolor = theme["bg"]
            _live_dark = _themes._is_dark(theme["bg"])
            self.page.theme_mode = (
                ft.ThemeMode.DARK if _live_dark else ft.ThemeMode.LIGHT
            )
            if _live_dark:
                self.page.dark_theme = ft.Theme(
                    color_scheme=ft.ColorScheme(
                        on_surface=theme["text"],
                        on_background=theme["text"],
                        surface=theme["panel"],
                    ),
                )
            self.page.update()
        except Exception:
            pass
        # Invalidate all cached views so they rebuild with the new theme.
        # Wrapper Columns must be removed from content_area.controls
        # explicitly — clearing only the dict orphans them in the tree
        # forever, bloating every future full page.update() into a freeze.
        for wrapper in self._cached_view_wrappers.values():
            with contextlib.suppress(Exception):
                self.content_area.controls.remove(wrapper)
        self._view_cache.clear()
        self._cached_view_wrappers.clear()

        # Rebuild the currently visible view instantly, without blocking
        # the click handler.
        async def _reload_current_view() -> None:
            with contextlib.suppress(Exception):
                view_func = self.current_view_func
                if callable(view_func):
                    view_func(None)

        with contextlib.suppress(Exception):
            self.page.run_task(_reload_current_view)
        self._update_toolbar_btns()

    def show_settings(self, e):
        # Settings always show the logged-in user's own profile — even when viewing
        # a subordinate or in "Загальні" mode. Force own-user context here.
        if not self._can_enter_view("show_settings", e):
            return
        from views.settings_view import SettingsView

        self._detach_live_mirror_views_for("")
        self._set_view_for_workspace("show_settings")
        try:
            self.page._active_view = "show_settings"
        except Exception:
            pass

        cache_key = "settings"
        # Instant switch: this tab's own build is a full ~3600-line rebuild with
        # several synchronous DB calls — expensive enough that, under load
        # (e.g. the local GSM parser running concurrently), it could take tens
        # of seconds and look "stuck" on every single visit. Reuse the already-
        # built instance like show_products/show_home do; region/manager
        # switches already flush this cache via _invalidate_context_cache().
        if self._view_cache.get(cache_key):
            self._activate_cached_view(cache_key)
            with contextlib.suppress(Exception):
                self.page.update()
            return

        _col = self._get_or_create_cached_wrapper(cache_key)
        _col.controls.clear()
        self._activate_cached_view(cache_key)
        with contextlib.suppress(Exception):
            self.page.update()
        # Build a clean own-user context: override manager-switch fields so settings
        # always edit the currently logged-in account regardless of general/subordinate mode.
        ctx = dict(self.user_data)
        ctx["active_manager_id"] = self.user_data.get("id")
        ctx["active_manager_name"] = self.user_data.get("full_name", "Мої дані")
        ctx["active_region_id"] = self._current_region_for_query()
        ctx["active_region_label"] = self._current_region_label()
        ctx["bypass_category_access"] = _is_admin_or_superadmin(
            self.user_data.get("role")
        )
        ctx["subordinate_view_readonly"] = False  # own profile → always editable
        ctx["shared_orders_map"] = self._shared_orders_map
        ctx["general_mode"] = False
        ctx["general_manager_ids"] = []
        ctx["all_subordinates_flat"] = list(self._all_subordinates_flat)
        ctx["work_orders_mode"] = self._work_orders_mode
        ctx["ui_theme"] = _themes.get_active_theme_key(
            (self.user_data.get("id") or "").strip()
        )
        sv = SettingsView(_col, ctx, self.db)
        sv._on_work_orders_mode_changed = self._set_work_orders_mode
        sv._on_theme_changed = self._apply_theme_live
        sv._on_nav_pack_changed = self._apply_nav_pack_live
        sv._on_feature_toggle_changed = self._refresh_sidebar_for_feature_change
        # This screen edits data other tabs cache locally (delivery methods,
        # payment field overrides, price tiers) — now that Settings itself is
        # cached and won't rebuild on every visit, a successful save must
        # explicitly flush those OTHER tabs' caches so they refetch fresh data
        # next time they're opened. preserve_keys keeps this tab's own cache
        # (the on-screen instance already reflects what was just saved).
        sv._on_settings_saved = lambda: self._invalidate_context_cache(
            preserve_keys=frozenset({cache_key})
        )
        if self._safe_build(sv, _col, self.show_settings):
            self._view_cache[cache_key] = {"instance": sv}
        self.page.update()

    def _reload_products_tab(self) -> None:
        """Flush all product-related in-process caches, then rebuild the products tab.

        Called by ProductsView after a successful product save so the newly added
        product appears immediately without requiring a full program restart.
        """
        with contextlib.suppress(Exception):
            self.db.invalidate_products_cache(self.company_id)
        cache_key = "products"
        self._view_cache.pop(cache_key, None)
        wrapper = self._cached_view_wrappers.get(cache_key)
        if wrapper is not None:
            wrapper.controls.clear()
        self.show_products(None)

    def open_bulk_product_template_user(self, _=None):
        try:
            from views.product_bulk_template_ui import show_bulk_product_template_dialog_user

            show_bulk_product_template_dialog_user(self, on_after_import=self._reload_products_tab)
        except Exception:
            logger.exception("open_bulk_product_template_user failed")
            try:
                if self.page:
                    self.page.snack_bar = ft.SnackBar(
                        content=ft.Text("Не вдалося відкрити майстер шаблону"),
                        bgcolor="red",
                        behavior=ft.SnackBarBehavior.FLOATING,
                    )
                    self.page.snack_bar.open = True
                    self.page.update()
            except Exception:
                pass

    def show_products(self, e, quick_filter: str = ""):
        if not self._can_enter_view("show_products", e):
            return
        from views.products_view import ProductsView

        self._detach_live_mirror_views_for("")
        self._set_view_for_workspace("show_products")
        try:
            self.page._active_view = "show_products"
        except Exception:
            pass

        cache_key = "products"
        # Instant switch: reuse cached view when it's already built and no filter override.
        if not quick_filter and self._view_cache.get(cache_key):
            self._activate_cached_view(cache_key)
            _inst = (self._view_cache.get(cache_key) or {}).get("instance")
            if _inst is not None:
                self._live_products_view_ref = _inst
                if hasattr(_inst, "resume"):
                    with contextlib.suppress(Exception):
                        self.page.run_task(_inst.resume)
            return

        active_ws = self._active_workspace() or {}
        products_state = (
            ((active_ws.get("filters") or {}).get("products") or {})
            if isinstance(active_ws, dict)
            else {}
        )
        if quick_filter:
            _col = self._activate_scratch_view()
        else:
            _col = self._get_or_create_cached_wrapper(cache_key)
            _col.controls.clear()
            self._activate_cached_view(cache_key)
            with contextlib.suppress(Exception):
                self.page.update()
        pv = ProductsView(
            _col,
            self._build_context_user(),
            self.db,
            self._current_region_for_query(),
            initial_state=products_state,
            on_state_change=self._on_products_state_change,
            on_bulk_template=self.open_bulk_product_template_user,
            on_products_reload=self._reload_products_tab,
            quick_filter=quick_filter,
        )
        if not quick_filter:
            self._live_products_view_ref = pv
        if self._safe_build(pv, _col, self.show_products):
            if not quick_filter:
                self._view_cache[cache_key] = {"instance": pv}
            try:
                self._defer_view_initial_load(pv, delay=0.06)
            except Exception:
                pass
        self.page.update()

    def show_categories_brands(self, e, quick_filter=None):
        logger.info(
            "[categories-debug] dashboard.show_categories_brands enter | active_view=%s | has_event=%s | manager_id=%s | role=%s | region=%s",
            getattr(self.page, "_active_view", ""),
            bool(e is not None),
            self.user_data.get("id"),
            self.user_data.get("role"),
            self._current_region_for_query(),
        )
        if not self._can_enter_view("show_categories_brands", e):
            logger.error(
                "[categories-debug] dashboard.show_categories_brands blocked by _can_enter_view"
            )
            return
        from views.categories_brands_view import CategoriesBrandsView

        self._detach_live_mirror_views_for("")
        self._set_view_for_workspace("show_categories_brands")
        try:
            self.page._active_view = "show_categories_brands"
        except Exception:
            pass

        cache_key = "categories"
        _qf = str(quick_filter or "").strip().lower()
        # Fast-path: show cached view immediately, then refresh in background so
        # newly created series/categories (e.g. from Products view) appear without
        # a manual Ctrl+F5 reload.
        if not _qf and self._view_cache.get(cache_key):
            self._activate_cached_view(cache_key)
            _ci = (self._view_cache.get(cache_key) or {}).get("instance")
            if _ci is not None and hasattr(_ci, "resume"):
                with contextlib.suppress(Exception):
                    self.page.run_task(_ci.resume)
            return

        if _qf:
            _col = self._activate_scratch_view()
        else:
            _col = self._get_or_create_cached_wrapper(cache_key)
            _col.controls.clear()
            self._activate_cached_view(cache_key)
            with contextlib.suppress(Exception):
                self.page.update()
        try:
            region_ref = self._current_region_for_query()
            region_id = None
            logger.info(
                "[categories-debug] dashboard.show_categories_brands context | region_ref=%s",
                region_ref,
            )
            if region_ref and region_ref != ALL_REGIONS_KEY:
                if isinstance(region_ref, str):
                    for reg in (
                        self.db.get_company_regions(self.user_data.get("company_id"))
                        or []
                    ):
                        if not isinstance(reg, dict):
                            continue
                        if (
                            str(reg.get("id") or "").strip() == str(region_ref).strip()
                            or str(reg.get("name") or "").strip()
                            == str(region_ref).strip()
                        ):
                            region_id = str(reg.get("id") or "").strip()
                            break
            logger.info(
                "[categories-debug] dashboard.show_categories_brands resolved region_id=%s",
                region_id,
            )
            catalog_config = self.db.get_company_catalog_config(
                self.user_data.get("company_id"), region_id
            )
            logger.info(
                "[categories-debug] dashboard.show_categories_brands catalog_config loaded | type=%s | keys=%s",
                type(catalog_config).__name__,
                (
                    list((catalog_config or {}).keys())
                    if isinstance(catalog_config, dict)
                    else []
                ),
            )
            cbv = CategoriesBrandsView(
                _col,
                self._build_context_user(),
                self.db,
                self._current_region_for_query(),
                catalog_config=catalog_config,
                quick_filter=_qf,
            )
            cbv.build()
            if not _qf:
                self._view_cache[cache_key] = {"instance": cbv}
            try:
                self._defer_view_initial_load(cbv, delay=0.06)
            except Exception:
                pass
            logger.info(
                "[categories-debug] dashboard.show_categories_brands build called | content_controls=%s",
                len(getattr(_col, "controls", []) or []),
            )
            self.page.update()
            logger.info(
                "[categories-debug] dashboard.show_categories_brands page.update complete"
            )
        except Exception as ex:
            logger.exception("Open categories view failed")
            _col.controls.clear()
            _col.controls.append(
                ft.Text(f"Помилка відкриття вкладки категорій: {ex}", color="red")
            )
            self.page.update()

    def show_suppliers(self, e, quick_filter: str = ""):
        if not self._can_enter_view("show_suppliers", e):
            return
        from views.suppliers_view import SuppliersView

        self._detach_live_mirror_views_for("")
        self._set_view_for_workspace("show_suppliers")
        try:
            self.page._active_view = "show_suppliers"
        except Exception:
            pass

        cache_key = "suppliers"
        if not quick_filter and self._view_cache.get(cache_key):
            self._activate_cached_view(cache_key)
            return

        if quick_filter:
            _col = self._activate_scratch_view()
        else:
            _col = self._get_or_create_cached_wrapper(cache_key)
            _col.controls.clear()
            self._activate_cached_view(cache_key)
            with contextlib.suppress(Exception):
                self.page.update()
        # Створюємо екземпляр. Наступний метод build() сам подбає про FilePicker.
        sv = SuppliersView(
            _col,
            self._build_context_user(),
            self.db,
            self._current_region_for_query(),
            quick_filter=quick_filter,
        )
        if self._safe_build(sv, _col, self.show_suppliers):
            if not quick_filter:
                self._view_cache[cache_key] = {"instance": sv}
            try:
                self._defer_view_initial_load(sv, delay=0.06)
            except Exception:
                pass
        self.page.update()

    def show_packaging(self, e):
        if not self._can_enter_view("show_packaging", e):
            return
        if not self._is_feature_enabled("packaging"):
            _scratch = self._activate_scratch_view()
            _scratch.controls.append(ft.Text("Вкладку вимкнено для вашої компанії", color="grey700"))
            self.page.update()
            return
        from views.packaging_view import PackagingView

        self._detach_live_mirror_views_for("")
        self._set_view_for_workspace("show_packaging")
        try:
            self.page._active_view = "show_packaging"
        except Exception:
            pass

        cache_key = "packaging"
        if self._view_cache.get(cache_key):
            self._activate_cached_view(cache_key)
            return

        _col = self._get_or_create_cached_wrapper(cache_key)
        _col.controls.clear()
        self._activate_cached_view(cache_key)
        with contextlib.suppress(Exception):
            self.page.update()
        pv = PackagingView(
            _col,
            self._build_context_user(),
            self.db,
            self._current_region_for_query(),
        )
        if self._safe_build(pv, _col, self.show_packaging):
            self._view_cache[cache_key] = {"instance": pv}
            try:
                self._defer_view_initial_load(pv, delay=0.06)
            except Exception:
                pass
        self.page.update()

    def show_files(self, e):
        if not self._can_enter_view("show_files", e):
            return
        if not self._is_feature_enabled("files"):
            _scratch = self._activate_scratch_view()
            _scratch.controls.append(ft.Text("Вкладку вимкнено для вашої компанії", color="grey700"))
            self.page.update()
            return
        from views.files_view import FilesView

        self._detach_live_mirror_views_for("")
        self._set_view_for_workspace("show_files")
        try:
            self.page._active_view = "show_files"
        except Exception:
            pass

        cache_key = "files"
        # Must read the LIVE current region, not self.user_data — that dict is
        # the raw login snapshot and is never updated on region clicks, so this
        # used to silently pass the stale login-time region on every refresh.
        new_region_id = str(self._current_region_for_query() or "")
        new_region_label = self._current_region_for_query() or ""
        if self._view_cache.get(cache_key):
            # Fast path: update region in cached view without layout rebuild.
            # set_region() is async (DB fetch runs off-thread), so schedule it
            # instead of blocking this call site.
            inst = self._view_cache[cache_key].get("instance")
            if inst is not None and hasattr(inst, "set_region"):
                with contextlib.suppress(Exception):
                    self.page.run_task(inst.set_region, new_region_id, new_region_label)
            self._activate_cached_view(cache_key)
            return

        _col = self._get_or_create_cached_wrapper(cache_key)
        _col.controls.clear()
        self._activate_cached_view(cache_key)
        with contextlib.suppress(Exception):
            self.page.update()
        ctx = self._build_context_user()
        fv = FilesView(_col, ctx, self.db, new_region_label)
        if self._safe_build(fv, _col, self.show_files):
            self._view_cache[cache_key] = {"instance": fv}
            try:
                self._defer_view_initial_load(fv, delay=0.06)
            except Exception:
                pass
        self.page.update()

    def show_copy_setup(self, e):
        if not self._can_enter_view("show_copy_setup", e):
            return
        from views.copy_setup_view import CopySetupView

        self._detach_live_mirror_views_for("")
        self._set_view_for_workspace("show_copy_setup")
        try:
            self.page._active_view = "show_copy_setup"
        except Exception:
            pass
        _scratch = self._activate_scratch_view()

        def _open_module(module_key):
            if module_key == "categories":
                self._with_view_switch_intent(lambda: self.show_categories_brands(None))
                return
            if module_key == "suppliers":
                self._with_view_switch_intent(lambda: self.show_suppliers(None))
                return
            if module_key == "packaging":
                self._with_view_switch_intent(lambda: self.show_packaging(None))
                return
            if module_key == "users":
                self._with_view_switch_intent(lambda: self.show_settings(None))
                return

        _csv = CopySetupView(
            _scratch,
            self._build_context_user(),
            self.db,
            on_open_module=_open_module,
            consume_dirty_scope=self._consume_scope_dirty,
        )
        self._safe_build(_csv, _scratch, self.show_copy_setup)
        self.page.update()

    def show_gsm_parser(self, e):
        if not self._can_enter_view("show_gsm_parser", e):
            return
        if not self.db.is_company_gsm_parser_enabled(self.company_id):
            self._detach_live_mirror_views_for("")
            self._set_view_for_workspace("show_gsm_parser")
            try:
                self.page._active_view = "show_gsm_parser"
            except Exception:
                pass
            _scratch = self._activate_scratch_view()
            _scratch.controls.append(
                ft.Text("GSM Parser вимкнено для вашої компанії", color="grey700")
            )
            self.page.update()
            return

        from views.gsm_parser_view import GsmParserView

        # Fast path: tab was already built this session — just toggle
        # visibility instantly instead of rebuilding the whole table/cards.
        # resume() itself only touches the DB if the daily sync boundary
        # was missed (or a force-reload was requested) — otherwise this is
        # a pure visibility toggle with zero DB/network work.
        _ci = (self._view_cache.get("gsm_parser") or {}).get("instance")
        if _ci is not None and getattr(_ci, "_gsm_ui_ready", False):
            self._detach_live_mirror_views_for("")
            self._set_view_for_workspace("show_gsm_parser")
            try:
                self.page._active_view = "show_gsm_parser"
            except Exception:
                pass
            self._activate_cached_view("gsm_parser")
            self.page.update()
            with contextlib.suppress(Exception):
                self.page.run_task(_ci.resume)
            return

        self._detach_live_mirror_views_for("")
        self._set_view_for_workspace("show_gsm_parser")
        try:
            self.page._active_view = "show_gsm_parser"
        except Exception:
            pass
        _gsm_wrapper = self._get_or_create_cached_wrapper("gsm_parser")
        _gsm_wrapper.controls.clear()
        self._activate_cached_view("gsm_parser")
        with contextlib.suppress(Exception):
            self.page.update()
        try:
            _gv = GsmParserView(_gsm_wrapper, self._build_context_user(), self.db)
            self._view_cache["gsm_parser"] = {"instance": _gv}
            self._safe_build(_gv, _gsm_wrapper, self.show_gsm_parser)
        except Exception:
            self._view_cache.pop("gsm_parser", None)
        self.page.update()

    def show_orders(self, e, quick_filter: str = ""):
        if not self._can_enter_view("show_orders", e):
            return
        from views.orders_view import OrdersView

        # Fast path: show cached view instantly (only when no quick_filter override).
        # _ws_key computed here — active_workspace_id is stable when no new workspace is created.
        if not quick_filter:
            _ws_key = f"orders_{self._active_workspace_id}"
            _cache = self._view_cache.get(_ws_key)
            _inst = (_cache or {}).get("instance")
            if _inst is not None and getattr(_inst, "_orders_ui_ready", False):
                # Invalidate cache if region changed — wrong region data would be shown.
                _cur_reg = self._current_region_for_query()
                _cac_reg = getattr(_inst, "active_region", "__unset__")
                if _cur_reg != _cac_reg:
                    self._view_cache.pop(_ws_key, None)
                else:
                    self._set_view_for_workspace("show_orders")
                    try:
                        self.page._active_view = "show_orders"
                    except Exception:
                        pass
                    self._live_orders_view_ref = _inst
                    # Restore sticky header before the page.update()
                    _sticky = getattr(self.page, "_view_sticky_header", None)
                    if _sticky is not None:
                        _hr = getattr(_inst, "_sticky_header_row", None)
                        _cb = getattr(_inst, "_sticky_counters_bar", None)
                        if _hr is not None and _cb is not None:
                            _sticky.controls[:] = [_hr, _cb]
                    self._scratch_col.visible = False
                    for _kk, _ww in self._cached_view_wrappers.items():
                        _ww.visible = _kk == _ws_key
                    self.page.update()
                    # resume() syncs from _shared_orders_map — no manual diff needed
                    try:
                        self.page.run_task(_inst.resume)
                    except Exception:
                        pass
                    return

        # Stop old orders view only if it's not cached in any workspace slot
        _old_ov = getattr(self, "_live_orders_view_ref", None)
        if _old_ov is not None:
            _is_cached = any(
                isinstance(v, dict) and v.get("instance") is _old_ov
                for v in self._view_cache.values()
            )
            if not _is_cached and hasattr(_old_ov, "stop"):
                with contextlib.suppress(Exception):
                    _old_ov.stop()

        self._detach_live_mirror_views_for("orders")
        self._set_view_for_workspace("show_orders")
        # Compute _ws_key AFTER _set_view_for_workspace — clicking a counter from the
        # fixed-home workspace causes _set_view_for_workspace to create a new workspace
        # and update _active_workspace_id to the new workspace's id.
        _ws_key = f"orders_{self._active_workspace_id}"
        try:
            self.page._active_view = "show_orders"
        except Exception:
            pass

        _orders_wrapper = self._get_or_create_cached_wrapper(_ws_key)
        _orders_wrapper.controls.clear()
        self._activate_cached_view(_ws_key)
        with contextlib.suppress(Exception):
            self.page.update()
        ov = OrdersView(
            _orders_wrapper,
            self._build_context_user(),
            self.db,
            self._current_region_for_query(),
            quick_filter=quick_filter,
        )
        self._live_orders_view_ref = ov
        self._view_cache[_ws_key] = {"instance": ov}
        if self._safe_build(ov, _orders_wrapper, self.show_orders):
            try:
                self._defer_view_initial_load(ov, delay=0.06)
            except Exception:
                pass
        self.page.update()
        try:
            self.page.run_task(self._scroll_content_area_top)
        except Exception:
            pass

    def show_payments(self, e, quick_filter: str = ""):
        if not self._can_enter_view("show_payments", e):
            return
        if not self._is_feature_enabled("payments"):
            _scratch = self._activate_scratch_view()
            _scratch.controls.append(ft.Text("Вкладку вимкнено для вашої компанії", color="grey700"))
            self.page.update()
            return
        from views.payments_view import PaymentsView

        # Fast path: show cached payments view instantly
        if not quick_filter:
            _ws_key = f"payments_{self._active_workspace_id}"
            _cache = self._view_cache.get(_ws_key)
            _inst = (_cache or {}).get("instance")
            if _inst is not None and getattr(_inst, "_payments_ui_ready", False):
                # Invalidate cache if user switched to a different region — the
                # cached instance was built for a different region scope and its
                # data/layout won't match (e.g. "РЈРєСЂР°С—РЅР°" cache shown as "Всі регіони").
                _current_region = self._current_region_for_query()
                _cached_region = getattr(_inst, "active_region", "__unset__")
                if _current_region != _cached_region:
                    self._view_cache.pop(_ws_key, None)
                    # Fall through to cold path below
                else:
                    self._detach_live_mirror_views_for("payments")
                    self._set_view_for_workspace("show_payments")
                    try:
                        self.page._active_view = "show_payments"
                    except Exception:
                        pass
                    self._live_payments_view_ref = _inst
                    _sticky = getattr(self.page, "_view_sticky_header", None)
                    if _sticky is not None:
                        _hr = getattr(_inst, "_sticky_header_row", None)
                        _cb = getattr(_inst, "_sticky_counters_bar", None)
                        if _hr is not None and _cb is not None:
                            _sticky.controls[:] = [_hr, _cb]
                    self._scratch_col.visible = False
                    for _kk, _ww in self._cached_view_wrappers.items():
                        _ww.visible = _kk == _ws_key
                    self.page.update()
                    # resume() syncs from _shared_orders_map — no manual diff needed
                    try:
                        self.page.run_task(_inst.resume)
                    except Exception:
                        pass
                    return

        self._detach_live_mirror_views_for("payments")
        self._set_view_for_workspace("show_payments")
        # Compute _ws_key AFTER _set_view_for_workspace — counter click from fixed-home
        # workspace causes _set_view_for_workspace to create a new workspace first.
        _ws_key = f"payments_{self._active_workspace_id}"
        try:
            self.page._active_view = "show_payments"
        except Exception:
            pass
        _payments_wrapper = self._get_or_create_cached_wrapper(_ws_key)
        _payments_wrapper.controls.clear()
        self._activate_cached_view(_ws_key)
        with contextlib.suppress(Exception):
            self.page.update()
        pv = PaymentsView(
            _payments_wrapper,
            self._build_context_user(),
            self.db,
            self._current_region_for_query(),
            quick_filter=quick_filter,
        )
        self._live_payments_view_ref = pv
        self._view_cache[_ws_key] = {"instance": pv}
        try:
            pv.build()
            try:
                self._defer_view_initial_load(pv, delay=0.06)
            except Exception:
                pass
        except Exception as _pv_ex:
            import traceback

            _payments_wrapper.controls.clear()
            _payments_wrapper.controls.append(
                ft.Text(
                    f"❌ Взаєморозрахунки: {_pv_ex}\n{traceback.format_exc()}",
                    color="#EF4444",
                    size=11,
                )
            )
            self._view_cache.pop(_ws_key, None)
        self.page.update()
        try:
            self.page.run_task(self._scroll_content_area_top)
        except Exception:
            pass

    def show_cost_price(self, e):
        # Legacy alias for existing workspaces/routes.
        self.show_cost_price_orders(e)

    def show_cost_price_orders(self, e, quick_filter: str = ""):
        if not self._can_enter_view("show_cost_price_orders", e):
            return
        if not self._is_feature_enabled("cost_price_orders"):
            _scratch = self._activate_scratch_view()
            _scratch.controls.append(ft.Text("Вкладку вимкнено для вашої компанії", color="grey700"))
            self.page.update()
            return
        from views.cost_price_orders_table import CostPriceView

        # Fast path: show cached cost-price-orders view instantly
        if not quick_filter:
            _ws_key = f"cost_price_orders_{self._active_workspace_id}"
            _cpw = self._cached_view_wrappers.get(_ws_key)
            _ci = (self._view_cache.get(_ws_key) or {}).get("instance")
            if _cpw is not None and len(_cpw.controls) > 0 and _ci is not None:
                # Invalidate cache if region changed.
                _cur_reg = self._current_region_for_query()
                _cac_reg = getattr(_ci, "active_region", "__unset__")
                if _cur_reg != _cac_reg:
                    self._view_cache.pop(_ws_key, None)
                    _cpw.controls.clear()
                else:
                    self._detach_live_mirror_views_for("cost_price_orders")
                    self._set_view_for_workspace("show_cost_price_orders")
                    try:
                        self.page._active_view = "show_cost_price_orders"
                    except Exception:
                        pass
                    self._live_cost_price_orders_view_ref = _ci
                    self._scratch_col.visible = False
                    for _kk, _ww in self._cached_view_wrappers.items():
                        _ww.visible = _kk == _ws_key
                    _sticky = getattr(self.page, "_view_sticky_header", None)
                    if _sticky is not None:
                        _hr = getattr(_ci, "_sticky_header_ref", None)
                        _cb = getattr(_ci, "_sticky_currency_bar_ref", None)
                        _cn = getattr(_ci, "_sticky_counters_ref", None)
                        _fb = getattr(_ci, "_sticky_filter_bar_ref", None)
                        _th = getattr(_ci, "_sticky_table_header_ref", None)
                        _new = [c for c in [_hr, _cb, _cn, _fb, _th] if c is not None]
                        if _new:
                            _sticky.controls[:] = _new
                    self.page.update()
                    if hasattr(_ci, "resume"):
                        try:
                            _ci._resume_last_seq = -1
                            self.page.run_task(_ci.resume)
                        except Exception:
                            pass
                    return

        # Stop old cost_price_orders view only if it's not cached in any workspace slot
        _old_cpv = getattr(self, "_live_cost_price_orders_view_ref", None)
        if _old_cpv is not None:
            _is_cached = any(
                isinstance(v, dict) and v.get("instance") is _old_cpv
                for v in self._view_cache.values()
            )
            if not _is_cached and hasattr(_old_cpv, "stop"):
                with contextlib.suppress(Exception):
                    _old_cpv.stop()
        self._detach_live_mirror_views_for("")
        self._set_view_for_workspace("show_cost_price_orders")
        # Compute _ws_key AFTER _set_view_for_workspace — counter click from fixed-home
        # workspace causes _set_view_for_workspace to create a new workspace first.
        _ws_key = f"cost_price_orders_{self._active_workspace_id}"
        try:
            self.page._active_view = "show_cost_price_orders"
        except Exception:
            pass
        _cp_wrapper = self._get_or_create_cached_wrapper(_ws_key)
        _cp_wrapper.controls.clear()
        self._activate_cached_view(_ws_key)
        with contextlib.suppress(Exception):
            self.page.update()
        cpv = CostPriceView(
            _cp_wrapper,
            self._build_context_user(),
            self.db,
            self._current_region_for_query(),
            quick_filter=quick_filter,
        )
        self._live_cost_price_orders_view_ref = cpv
        self._view_cache[_ws_key] = {"instance": cpv}
        if self._safe_build(cpv, _cp_wrapper, self.show_cost_price_orders):
            try:
                self._defer_view_initial_load(cpv, delay=0.06)
            except Exception:
                pass
        self.page.update()
        try:
            self.page.run_task(self._scroll_content_area_top)
        except Exception:
            pass

    def show_cost_price_stock(self, e):
        if not self._can_enter_view("show_cost_price_stock", e):
            return
        if not self._is_feature_enabled("cost_price_stock"):
            _scratch = self._activate_scratch_view()
            _scratch.controls.append(ft.Text("Вкладку вимкнено для вашої компанії", color="grey700"))
            self.page.update()
            return
        from views.cost_price_stock_page import CostPriceStockView

        # Fast path: show cached view instantly
        _cpsi = (self._view_cache.get("cost_price_stock") or {}).get("instance")
        if _cpsi is not None and getattr(_cpsi, "_stock_ui_ready", False):
            # Invalidate cache if region changed.
            _cur_reg = self._current_region_for_query()
            _cac_reg = getattr(_cpsi, "active_region", "__unset__")
            _cps_w = None
            if _cur_reg != _cac_reg:
                self._view_cache.pop("cost_price_stock", None)
                _cps_w = self._cached_view_wrappers.get("cost_price_stock")
            if _cps_w is not None:
                _cps_w.controls.clear()
            else:
                self._detach_live_mirror_views_for("cost_price_stock")
                self._set_view_for_workspace("show_cost_price_stock")
                try:
                    self.page._active_view = "show_cost_price_stock"
                except Exception:
                    pass
                self._live_cost_price_stock_view_ref = _cpsi
                self._scratch_col.visible = False
                for _kk, _ww in self._cached_view_wrappers.items():
                    _ww.visible = _kk == "cost_price_stock"
                _sticky = getattr(self.page, "_view_sticky_header", None)
                if _sticky is not None:
                    _hr = getattr(_cpsi, "_sticky_header_ref", None)
                    _cb = getattr(_cpsi, "_sticky_currency_bar_ref", None)
                    _cn = getattr(_cpsi, "_sticky_counters_ref", None)
                    _fb = getattr(_cpsi, "_sticky_filter_bar_ref", None)
                    _tr = getattr(_cpsi, "_sticky_totals_row_ref", None)
                    _th = getattr(_cpsi, "_sticky_table_header_ref", None)
                    _new = [c for c in [_hr, _cb, _cn, _fb, _tr, _th] if c is not None]
                    if _new:
                        _sticky.controls[:] = _new
                self.page.update()
                if hasattr(_cpsi, "resume"):
                    try:
                        _cpsi._resume_last_seq = -1
                        self.page.run_task(_cpsi.resume)
                    except Exception:
                        pass
                return

        if _cpsi is not None:
            # A build for this view is already in flight (e.g. a rapid
            # double-click before _stock_ui_ready flips True) - constructing
            # a SECOND CostPriceStockView here used to race the first one:
            # both target the same shared wrapper/sticky header, and since
            # each instance's _render() only clears+repopulates once its own
            # (awaited) data load finishes, whichever one lands last would
            # leave the OTHER's header/filter block stacked above it instead
            # of replacing it. Just re-show the in-flight instance's tab.
            self._detach_live_mirror_views_for("cost_price_stock")
            self._set_view_for_workspace("show_cost_price_stock")
            try:
                self.page._active_view = "show_cost_price_stock"
            except Exception:
                pass
            self._live_cost_price_stock_view_ref = _cpsi
            self._activate_cached_view("cost_price_stock")
            with contextlib.suppress(Exception):
                self.page.update()
            return

        self._detach_live_mirror_views_for("cost_price_stock")
        self._set_view_for_workspace("show_cost_price_stock")
        try:
            self.page._active_view = "show_cost_price_stock"
        except Exception:
            pass
        _cps_wrapper = self._get_or_create_cached_wrapper("cost_price_stock")
        _cps_wrapper.controls.clear()
        self._activate_cached_view("cost_price_stock")
        with contextlib.suppress(Exception):
            self.page.update()
        cpsv = CostPriceStockView(
            _cps_wrapper,
            self._build_context_user(),
            self.db,
            self._current_region_for_query(),
        )
        self._live_cost_price_stock_view_ref = cpsv
        self._view_cache["cost_price_stock"] = {"instance": cpsv}
        if self._safe_build(cpsv, _cps_wrapper, self.show_cost_price_stock):
            try:
                self._defer_view_initial_load(cpsv, delay=0.06)
            except Exception:
                pass
        self.page.update()
        try:
            self.page.run_task(self._scroll_content_area_top)
        except Exception:
            pass

    def show_pack_stock(self, e, quick_filter: str = ""):
        if not self._can_enter_view("show_pack_stock", e):
            return
        if not self._is_feature_enabled("pack_stock"):
            _scratch = self._activate_scratch_view()
            _scratch.controls.append(ft.Text("Вкладку вимкнено для вашої компанії", color="grey700"))
            self.page.update()
            return
        from views.packaging_stock_view import PackagingStockView

        # Fast path: show cached pack_stock view instantly
        if not quick_filter:
            _ci = (self._view_cache.get("pack_stock") or {}).get("instance")
            if _ci is not None and getattr(_ci, "_pack_ui_ready", False):
                # Invalidate cache if region changed.
                _cur_reg = self._current_region_for_query()
                # PackagingStockView stores region as region_ref, not active_region
                _cac_reg = getattr(
                    _ci, "region_ref", getattr(_ci, "active_region", "__unset__")
                )
                if _cur_reg != _cac_reg:
                    self._view_cache.pop("pack_stock", None)
                    _psw = self._cached_view_wrappers.get("pack_stock")
                    if _psw is not None:
                        _psw.controls.clear()
                else:
                    self._detach_live_mirror_views_for("pack_stock")
                    self._set_view_for_workspace("show_pack_stock")
                    try:
                        self.page._active_view = "show_pack_stock"
                    except Exception:
                        pass
                    self._live_pack_stock_view_ref = _ci
                    self._scratch_col.visible = False
                    for _kk, _ww in self._cached_view_wrappers.items():
                        _ww.visible = _kk == "pack_stock"
                    _sticky = getattr(self.page, "_view_sticky_header", None)
                    if _sticky is not None:
                        _hr = getattr(_ci, "_sticky_header_ref", None)
                        if _hr is not None:
                            _sticky.controls[:] = [_hr]
                    self.page.update()
                    if hasattr(_ci, "resume"):
                        try:
                            _ci._force_reload = True
                            self.page.run_task(_ci.resume)
                        except Exception:
                            pass
                    return

        self._detach_live_mirror_views_for("pack_stock")
        self._set_view_for_workspace("show_pack_stock")
        try:
            self.page._active_view = "show_pack_stock"
        except Exception:
            pass
        _ps_wrapper = self._get_or_create_cached_wrapper("pack_stock")
        _ps_wrapper.controls.clear()
        self._activate_cached_view("pack_stock")
        with contextlib.suppress(Exception):
            self.page.update()
        try:
            sv = PackagingStockView(
                _ps_wrapper,
                self._build_context_user(),
                self.db,
                self._current_region_for_query(),
                quick_filter=quick_filter,
            )
            self._live_pack_stock_view_ref = sv
            self._view_cache["pack_stock"] = {"instance": sv}
            sv.build()
            try:
                self._defer_view_initial_load(sv, delay=0.06)
            except Exception:
                pass
        except Exception as ex:
            _ps_wrapper.controls.clear()
            _ps_wrapper.controls.append(
                ft.Text(f"❌ Склад пакування: {ex}", color="#EF4444")
            )
            self._view_cache.pop("pack_stock", None)
        self.page.update()
        try:
            self.page.run_task(self._scroll_content_area_top)
        except Exception:
            pass

    def show_media_plan(self, e, quick_filter: str = ""):
        if not self._can_enter_view("show_media_plan", e):
            return
        if not self._is_feature_enabled("media_plan"):
            _scratch = self._activate_scratch_view()
            _scratch.controls.append(ft.Text("Вкладку вимкнено для вашої компанії", color="grey700"))
            self.page.update()
            return
        from views.media_plan_view import MediaPlanView

        # "clear" means: rebuild fresh without any filter (bypasses stale-filter cache).
        if quick_filter == "clear":
            quick_filter = ""
            self._view_cache.pop("media_plan", None)
            _mp_w = self._cached_view_wrappers.get("media_plan")
            if _mp_w:
                _mp_w.controls.clear()

        # Fast path: show cached view instantly
        if not quick_filter:
            _mpi = (self._view_cache.get("media_plan") or {}).get("instance")
            if _mpi is not None and getattr(_mpi, "_media_plan_ui_ready", False):
                self._detach_live_mirror_views_for("media_plan")
                self._set_view_for_workspace("show_media_plan")
                try:
                    self.page._active_view = "show_media_plan"
                except Exception:
                    pass
                self._live_media_plan_view_ref = _mpi
                self._scratch_col.visible = False
                for _kk, _ww in self._cached_view_wrappers.items():
                    _ww.visible = _kk == "media_plan"
                _sticky = getattr(self.page, "_view_sticky_header", None)
                if _sticky is not None:
                    _t = getattr(_mpi, "_sticky_title_ref", None)
                    _f = getattr(_mpi, "_sticky_filter_ref", None)
                    _th = getattr(_mpi, "_sticky_table_header_ref", None)
                    _new = [c for c in [_t, _f, _th] if c is not None]
                    if _new:
                        _sticky.controls[:] = _new
                self.page.update()
                if hasattr(_mpi, "resume"):
                    try:
                        _mpi._entries_cache_pulse = 0
                        self.page.run_task(_mpi.resume)
                    except Exception:
                        pass
                return

        self._detach_live_mirror_views_for("media_plan")
        self._set_view_for_workspace("show_media_plan")
        try:
            self.page._active_view = "show_media_plan"
        except Exception:
            pass
        _mp_wrapper = self._get_or_create_cached_wrapper("media_plan")
        _mp_wrapper.controls.clear()
        self._activate_cached_view("media_plan")
        with contextlib.suppress(Exception):
            self.page.update()
        try:
            mv = MediaPlanView(
                _mp_wrapper,
                self._build_context_user(),
                self.db,
                active_region=self._current_region_for_query(),
                quick_filter=quick_filter,
            )
            self._live_media_plan_view_ref = mv
            self._view_cache["media_plan"] = {"instance": mv}
            mv.build()
        except Exception as _ex:
            _mp_wrapper.controls.clear()
            _mp_wrapper.controls.append(
                ft.Text(f"❌ Медіа план: {_ex}", color="#EF4444")
            )
            self._view_cache.pop("media_plan", None)
        self.page.update()
        try:
            self.page.run_task(self._scroll_content_area_top)
        except Exception:
            pass

    def show_schedule(self, e, quick_filter: str = ""):
        if not self._can_enter_view("show_schedule", e):
            return
        if not self._is_feature_enabled("schedule"):
            _scratch = self._activate_scratch_view()
            _scratch.controls.append(ft.Text("Вкладку вимкнено для вашої компанії", color="grey700"))
            self.page.update()
            return
        from views.order_schedule_view import OrderScheduleView

        # Fast path: show cached view instantly
        if not quick_filter:
            _sci = (self._view_cache.get("schedule") or {}).get("instance")
            if _sci is not None and getattr(_sci, "_schedule_ui_ready", False):
                self._detach_live_mirror_views_for("schedule")
                self._set_view_for_workspace("show_schedule")
                try:
                    self.page._active_view = "show_schedule"
                except Exception:
                    pass
                self._live_schedule_view_ref = _sci
                self._scratch_col.visible = False
                for _kk, _ww in self._cached_view_wrappers.items():
                    _ww.visible = _kk == "schedule"
                _sticky = getattr(self.page, "_view_sticky_header", None)
                if _sticky is not None:
                    _t = getattr(_sci, "_sticky_title_ref", None)
                    _f = getattr(_sci, "_sticky_filter_ref", None)
                    _th = getattr(_sci, "_sticky_table_header_ref", None)
                    _new = [c for c in [_t, _f, _th] if c is not None]
                    if _new:
                        _sticky.controls[:] = _new
                self.page.update()
                if hasattr(_sci, "resume"):
                    try:
                        _sci._schedule_cache_pulse = 0
                        self.page.run_task(_sci.resume)
                    except Exception:
                        pass
                return

        self._detach_live_mirror_views_for("schedule")
        self._set_view_for_workspace("show_schedule")
        try:
            self.page._active_view = "show_schedule"
        except Exception:
            pass
        _sc_wrapper = self._get_or_create_cached_wrapper("schedule")
        _sc_wrapper.controls.clear()
        self._activate_cached_view("schedule")
        with contextlib.suppress(Exception):
            self.page.update()
        try:
            sv = OrderScheduleView(
                _sc_wrapper,
                self._build_context_user(),
                self.db,
                active_region=self._current_region_for_query(),
                quick_filter=quick_filter,
            )
            self._live_schedule_view_ref = sv
            self._view_cache["schedule"] = {"instance": sv}
            sv.build()
        except Exception as _ex:
            import traceback

            _sc_wrapper.controls.clear()
            _sc_wrapper.controls.append(
                ft.Text(
                    f"❌ Розклад Замовлень: {_ex}\n{traceback.format_exc()}",
                    color="#EF4444",
                    size=11,
                )
            )
            self._view_cache.pop("schedule", None)
        self.page.update()
        try:
            self.page.run_task(self._scroll_content_area_top)
        except Exception:
            pass

    # ────────────────────────────────────────────────────────────────────────
    # Global keyboard shortcuts
    # ────────────────────────────────────────────────────────────────────────

    def _on_keyboard_event(self, e: ft.KeyboardEvent) -> None:
        try:
            key = str(e.key or "").strip()
            if not e.ctrl:
                if e.alt and key in ("Arrow Left", "Arrow Right"):
                    self.page.run_task(self._handle_alt_h_scroll, key)
                    return
                if (
                    key == "Escape"
                    and getattr(self, "_search_overlay_stack", None) is not None
                ):
                    self._close_search_overlay()
                return
            if key == "F5":
                self.page.run_task(self._handle_ctrl_f5)
            elif key.lower() == "f":
                self._handle_ctrl_f8()
            elif key.lower() == "s":
                self.page.run_task(self._handle_ctrl_f4)
            elif key in ("+", "=", "Add", "Numpad +", "Numpad Add"):
                self._zoom_step(1)
            elif key in ("-", "Subtract", "Numpad -", "Numpad Subtract"):
                self._zoom_step(-1)
            elif key in ("0", "Numpad 0"):
                self.page.run_task(self._reset_zoom_async)
        except Exception:
            pass

    async def _handle_alt_h_scroll(self, key: str) -> None:
        try:
            vkey = self._current_cache_key()
            inst = (self._view_cache.get(vkey) or {}).get("instance")
            if inst is None:
                return
            scroll = getattr(inst, "_cv_body_scroll", None) or getattr(
                inst, "_h_scroll_row", None
            )
            if scroll is None:
                return
            _STEP = 300
            delta = _STEP if key == "Arrow Right" else -_STEP
            await scroll.scroll_to(delta=delta, duration=0)
        except Exception:
            pass

    def _current_cache_key(self) -> str:
        active = self._active_view_key()
        return active[5:] if active.startswith("show_") else active

    # Ctrl+F5 — Sync snapshot vs DB, pull differences, then rebuild current view
    async def _handle_ctrl_f5(self) -> None:
        vkey = self._current_cache_key()
        active_func_name = self._active_view_key()

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

        # Step 1: fetch fresh pulses from DB and compare against local snapshot
        changed_scopes: set[str] = set()
        company_id = (self.company_id or "").strip()
        if company_id:
            try:
                all_scopes = self._sync_scopes()
                pulses = await asyncio.to_thread(
                    self.db.get_companies_sync_pulses,
                    [company_id],
                    scopes=all_scopes,
                    force_refresh=True,
                )
                current = (
                    pulses.get(company_id, {}) if isinstance(pulses, dict) else {}
                )
                snapshot_changed = False
                for scope, ts in (current or {}).items():
                    key = f"{company_id}:{scope}"
                    prev = int(self._sync_last_pulses.get(key, 0) or 0)
                    now_ts = int(ts or 0)
                    if now_ts <= 0:
                        continue
                    if now_ts != prev:
                        # DB differs from local snapshot → invalidate local cache
                        self.db.invalidate_scope_cache(company_id, scope)
                        changed_scopes.add(scope)
                        self._sync_last_pulses[key] = now_ts
                        snapshot_changed = True

                if snapshot_changed:
                    self._save_sync_snapshot()
                    logger.info(
                        "[ctrl+f5] оновлено scopes з БД: %s", sorted(changed_scopes)
                    )
            except Exception:
                logger.exception("[ctrl+f5] pulse check failed")

        # Step 2: clear view cache and rebuild from fresh DB data.
        # Cache keys for workspace-specific views are "{base}_{workspace_id}" —
        # collect all keys that match the current base so workspace views are
        # also invalidated correctly.
        keys_to_clear = [
            k for k in list(self._view_cache)
            if k == vkey or k.startswith(vkey + "_")
        ]
        for _k in keys_to_clear:
            self._view_cache.pop(_k, None)
            wrapper = self._cached_view_wrappers.get(_k)
            if wrapper is not None:
                wrapper.controls.clear()
        show_fn = getattr(self, active_func_name, None)
        if callable(show_fn):
            with contextlib.suppress(Exception):
                show_fn(None)

        if changed_scopes:
            _snack(
                f"Знайдено зміни в БД ({', '.join(sorted(changed_scopes))}). Сторінку оновлено.",
                "#15803D",
            )
        else:
            _snack("Дані актуальні. Сторінку перезавантажено.", "#374151")

    # Ctrl+S — Flush only the ACTIVE tab's unsaved cache to DB
    async def _handle_ctrl_f4(self) -> None:
        vkey = self._current_cache_key()
        inst = (self._view_cache.get(vkey) or {}).get("instance")
        if inst is None:
            return
        with contextlib.suppress(Exception):
            if hasattr(inst, "flush_pending_async"):
                await inst.flush_pending_async()
            elif hasattr(inst, "_force_sync_all_async"):
                await inst._force_sync_all_async()
            elif hasattr(inst, "_force_sync_workspace_state_async"):
                await inst._force_sync_workspace_state_async()
            elif hasattr(inst, "_flush_pack_outbox_async"):
                await inst._flush_pack_outbox_async()

    # Ctrl+F — Toggle floating search overlay
    def _handle_ctrl_f8(self) -> None:
        if getattr(self, "_search_overlay_stack", None) is not None:
            self._close_search_overlay()
            return
        self._open_search_overlay()

    def _close_search_overlay(self) -> None:
        stack = getattr(self, "_search_overlay_stack", None)
        if stack is None:
            return
        with contextlib.suppress(Exception):
            if stack in self.page.overlay:
                self.page.overlay.remove(stack)
        self._search_overlay_stack = None
        row_filter_target = self._row_filter_target()
        if row_filter_target is not None:
            with contextlib.suppress(Exception):
                row_filter_target.clear_text_search()
        self._search_results = []
        self._search_result_idx = 0
        # Restore all highlights without a separate page.update()
        for ctrl, orig_bg, orig_color in self._search_hl_all_saves or []:
            ctrl.bgcolor = orig_bg
            with contextlib.suppress(Exception):
                ctrl.color = orig_color
        self._search_hl_all_saves = []
        self._search_hl_saves = None
        self._search_hl_ctrl = None
        # Single update covers both overlay removal and all highlight restores
        with contextlib.suppress(Exception):
            self.page.update()

    # Views where Ctrl+F should hide non-matching rows/groups instead of the
    # generic highlight-and-jump tree walk. Keyed by _current_cache_key() base
    # string; value is the always-maintained "_live_*_view_ref" attribute name
    # (NOT self._view_cache, whose keys are workspace-scoped for some of these).
    _ROW_FILTER_VIEW_REFS = {
        "payments": "_live_payments_view_ref",
        "cost_price_orders": "_live_cost_price_orders_view_ref",
        "cost_price_stock": "_live_cost_price_stock_view_ref",
        "pack_stock": "_live_pack_stock_view_ref",
        "media_plan": "_live_media_plan_view_ref",
        "schedule": "_live_schedule_view_ref",
    }

    def _row_filter_target(self):
        """Return the live view instance for a row-filter-enabled tab, or None."""
        ref_attr = self._ROW_FILTER_VIEW_REFS.get(self._current_cache_key())
        if not ref_attr:
            return None
        inst = getattr(self, ref_attr, None)
        if inst is None or not hasattr(inst, "apply_text_search"):
            return None
        return inst

    def _search_collect_matches(self, query: str) -> list:
        """Walk content_area control tree and return list of (ctrl, ancestors) for all matching text."""
        vkey = self._current_cache_key()
        inst = (self._view_cache.get(vkey) or {}).get("instance")
        if inst is None:
            return []
        # C/Р’ Р—Р°РјРѕРІР»РµРЅРЅСЏ has 10k+ table cells — full tree walk freezes the UI.
        # Search is not useful in a dense table view, so skip it entirely.
        if vkey in ("cost_price_orders", "cost_price_stock"):
            return []
        ca = getattr(inst, "content_area", None)
        if ca is None:
            return []
        q = query.lower()
        results: list = []

        def _walk(ctrl, ancs: list, depth: int) -> None:
            if depth > 80 or len(results) >= 500 or ctrl is None:
                return
            if isinstance(ctrl, ft.Text):
                v = ctrl.value
                if v is not None and q in str(v).lower():
                    results.append((ctrl, list(ancs)))
            elif isinstance(ctrl, ft.TextField):
                v = ctrl.value
                if v is not None and q in str(v).lower():
                    results.append((ctrl, list(ancs)))
            ancs.append(ctrl)
            try:
                for attr in (
                    "controls",
                    "content",
                    "actions",
                    "leading",
                    "trailing",
                    "title",
                    "subtitle",
                ):
                    child = getattr(ctrl, attr, None)
                    if child is None:
                        continue
                    if isinstance(child, list):
                        for c in child:
                            if isinstance(c, ft.Control):
                                _walk(c, ancs, depth + 1)
                    elif isinstance(child, ft.Control):
                        _walk(child, ancs, depth + 1)
            finally:
                ancs.pop()

        _walk(ca, [], 0)
        return results

    _SEARCH_HL_COLOR = "#2563EB"  # РїРѕС‚РѕС‡РЅРёР№ Р·Р±С–Рі — РЅР°СЃРёС‡РµРЅРёР№ СЃРёРЅС–Р№
    _SEARCH_HL_TEXT_COLOR = "#FFFFFF"  # білий текст на поточному
    _SEARCH_HL_ALL_COLOR = "#BFDBFE"  # С–РЅС€С– Р·Р±С–РіРё — СЃРІС–С‚Р»Рѕ-СЃРёРЅС–Р№
    _SEARCH_HL_ALL_TEXT_COLOR = "#1E40AF"  # темно-синій текст на інших

    def _sweep_clear_highlights(self) -> None:
        """Walk the current view's tree and clear any stuck highlight colors."""
        vkey = self._current_cache_key()
        inst = (self._view_cache.get(vkey) or {}).get("instance")
        if inst is None:
            return
        ca = getattr(inst, "content_area", None)
        if ca is None:
            return

        def _walk(ctrl: ft.Control, depth: int = 0) -> None:
            if depth > 80 or ctrl is None:
                return
            if isinstance(ctrl, (ft.Text, ft.TextField, ft.Container)):
                if ctrl.bgcolor in (self._SEARCH_HL_COLOR, self._SEARCH_HL_ALL_COLOR):
                    ctrl.bgcolor = None
                if getattr(ctrl, "color", None) in (
                    self._SEARCH_HL_TEXT_COLOR,
                    self._SEARCH_HL_ALL_TEXT_COLOR,
                ):
                    ctrl.color = None
            for attr in (
                "controls",
                "content",
                "actions",
                "leading",
                "trailing",
                "title",
                "subtitle",
            ):
                child = getattr(ctrl, attr, None)
                if isinstance(child, list):
                    for c in child:
                        if isinstance(c, ft.Control):
                            _walk(c, depth + 1)
                elif isinstance(child, ft.Control):
                    _walk(child, depth + 1)

        _walk(ca)

    def _clear_search_highlight(self) -> None:
        """Restore all highlighted controls to their original state."""
        _had = bool(self._search_hl_all_saves or self._search_hl_saves)
        for ctrl, orig_bg, orig_color in self._search_hl_all_saves or []:
            ctrl.bgcolor = orig_bg
            with contextlib.suppress(Exception):
                ctrl.color = orig_color
        self._search_hl_all_saves = []
        self._search_hl_saves = None
        self._search_hl_ctrl = None
        if _had:
            with contextlib.suppress(Exception):
                self.page.update()

    @staticmethod
    def _search_hl_target(ctrl: ft.Control, ancestors: list) -> ft.Control:
        """Return nearest ft.Container ancestor to highlight (gives full-cell color).
        Falls back to ctrl itself (ft.Text/ft.TextField) when no container parent exists.
        """
        for anc in reversed(ancestors):
            if isinstance(anc, ft.Container):
                return anc
        return ctrl

    def _apply_all_search_highlights(self, results: list) -> None:
        """Apply dim highlight to ALL matched controls at once."""
        for hl, orig_bg, orig_color in self._search_hl_all_saves or []:
            hl.bgcolor = orig_bg
            with contextlib.suppress(Exception):
                hl.color = orig_color
        self._search_hl_all_saves = []
        self._search_hl_saves = None
        self._search_hl_ctrl = None
        _seen: set = set()
        for ctrl, ancs in results:
            hl = self._search_hl_target(ctrl, ancs)
            if id(hl) in _seen:
                continue
            _seen.add(id(hl))
            self._search_hl_all_saves.append(
                (hl, hl.bgcolor, getattr(hl, "color", None))
            )
            hl.bgcolor = self._SEARCH_HL_ALL_COLOR
            if not isinstance(hl, ft.Container):
                with contextlib.suppress(Exception):
                    hl.color = self._SEARCH_HL_ALL_TEXT_COLOR

    def _apply_search_highlight(self, ctrl: ft.Control, ancestors: list) -> None:
        """Mark current match with strong highlight; restore previous current to dim highlight."""
        hl_ctrl = self._search_hl_target(ctrl, ancestors)
        # Restore previous current to dim (all-matches) color
        if self._search_hl_ctrl is not None and self._search_hl_ctrl is not hl_ctrl:
            self._search_hl_ctrl.bgcolor = self._SEARCH_HL_ALL_COLOR
        self._search_hl_ctrl = None
        # Expand any collapsed ancestor (visible=False)
        for anc in ancestors:
            if getattr(anc, "visible", True) is False:
                anc.visible = True
        # Apply strong highlight to current match (full-cell color)
        hl_ctrl.bgcolor = self._SEARCH_HL_COLOR
        if not isinstance(hl_ctrl, ft.Container):
            with contextlib.suppress(Exception):
                hl_ctrl.color = self._SEARCH_HL_TEXT_COLOR
        self._search_hl_ctrl = hl_ctrl
        # Expand collapsed sections — prefer _open_inline() so collapsible state stays consistent
        _did_open = False
        for anc in ancestors:
            _open_fn = getattr(anc, "_open_inline", None)
            if callable(_open_fn) and not getattr(anc, "_is_open", True):
                _open_fn()
                _did_open = True
            elif not getattr(anc, "visible", True):
                anc.visible = True
                _did_open = True
        with contextlib.suppress(Exception):
            self.page.update()
        # Scroll to the matched control — async because scroll_to() is a coroutine
        vkey = self._current_cache_key()
        inst = (self._view_cache.get(vkey) or {}).get("instance")
        ca = getattr(inst, "content_area", None) if inst else None
        if ca is not None:
            self.page.run_task(
                self._scroll_to_search_result, ca, list(ancestors), _did_open
            )

    async def _scroll_to_search_result(
        self, ca, ancestors: list, did_open: bool = False
    ) -> None:
        """Scroll to the highlighted search result using scroll_key (Flutter ensureVisible)."""
        # Wait for Flutter to finish rendering the highlight/visibility changes.
        # When a collapsed section was just opened we need more time for Flutter to layout.
        await asyncio.sleep(0.7 if did_open else 0.35)
        # Collect keyed ancestors closest-to-match first (innermost key is most precise).
        # Pass the key object as-is so ScrollKey._type="scroll" is preserved — plain strings
        # resolve as ValueKey which is NOT in the scroll registry and silently fails.
        seen_vals: set = set()
        keys: list = []
        for anc in reversed(ancestors):
            k = getattr(anc, "key", None)
            if k is None:
                continue
            k_val = str(k)  # for deduplication only
            if k_val not in seen_vals:
                seen_vals.add(k_val)
                keys.append(k)  # keep the original ScrollKey/ValueKey object

        if not keys:
            return

        # Scroll using the innermost (most precise) key first
        for i, k in enumerate(keys):
            with contextlib.suppress(Exception):
                await ca.scroll_to(scroll_key=k, duration=200)
            if i < len(keys) - 1:
                await asyncio.sleep(0.12)

    def _open_search_overlay(self) -> None:
        self._search_results = []  # list of (ctrl, ancestors)
        self._search_result_idx = 0
        self._search_query = ""
        self._clear_search_highlight()

        count_text = ft.Text("", size=11, color="#64748B", width=88)

        def _update_count_display() -> None:
            n = len(self._search_results)
            if n == 0:
                count_text.value = "Не знайдено" if self._search_query else ""
            else:
                count_text.value = f"{self._search_result_idx + 1} / {n}"
            with contextlib.suppress(Exception):
                count_text.update()

        def _go_to(idx: int) -> None:
            if not self._search_results:
                return
            ctrl, ancs = self._search_results[idx]
            self._apply_search_highlight(ctrl, ancs)

        def _run_search() -> None:
            """Execute search for current field value and jump to first result."""
            query = str(search_field.value or "").strip()
            self._search_query = query
            row_filter_target = self._row_filter_target()
            if row_filter_target is not None:
                # Row-filter tabs: hide non-matching rows/groups, no highlight/jump.
                self._clear_search_highlight()
                self._search_results = []
                self._search_result_idx = 0
                if query:
                    row_filter_target.apply_text_search(query)
                else:
                    row_filter_target.clear_text_search()
                count_text.value = ""
                with contextlib.suppress(Exception):
                    count_text.update()
                return
            self._clear_search_highlight()
            if query:
                self._search_results = self._search_collect_matches(query)
            else:
                self._search_results = []
            self._search_result_idx = 0
            if self._search_results:
                # Highlight ALL matches at once, then mark first as current
                self._apply_all_search_highlights(self._search_results)
                _go_to(0)
            _update_count_display()

        def _on_submit(_) -> None:
            """Enter pressed: new query → run search; same query → go to next result."""
            if self._row_filter_target() is not None:
                # Row-filter tabs have no "next match" concept — every Enter re-applies.
                _run_search()
                return
            current_value = str(search_field.value or "").strip()
            if current_value != self._search_query or not self._search_results:
                _run_search()
            else:
                _nav_next(_)

        def _nav_prev(_) -> None:
            if not self._search_results:
                return
            self._search_result_idx = (self._search_result_idx - 1) % len(
                self._search_results
            )
            _go_to(self._search_result_idx)
            _update_count_display()

        def _nav_next(_) -> None:
            if not self._search_results:
                return
            self._search_result_idx = (self._search_result_idx + 1) % len(
                self._search_results
            )
            _go_to(self._search_result_idx)
            _update_count_display()

        def _close(_) -> None:
            self._close_search_overlay()

        _is_row_filter = self._row_filter_target() is not None

        search_field = ft.TextField(
            hint_text="Фільтр рядків... (Enter)"
            if _is_row_filter
            else "Пошук по сторінці... (Enter)",
            on_submit=_on_submit,
            height=34,
            text_size=13,
            border=ft.InputBorder.OUTLINE,
            border_color="#CBD5E1",
            focused_border_color="#2563EB",
            content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
            expand=True,
            autofocus=True,
        )

        panel = ft.Container(
            content=ft.Row(
                [
                    search_field,
                    count_text,
                    ft.IconButton(
                        ft.Icons.KEYBOARD_ARROW_UP,
                        on_click=_nav_prev,
                        icon_size=18,
                        tooltip="Попереднє",
                        icon_color="#475569",
                        style=ft.ButtonStyle(padding=ft.padding.all(2)),
                        visible=not _is_row_filter,
                    ),
                    ft.IconButton(
                        ft.Icons.KEYBOARD_ARROW_DOWN,
                        on_click=_nav_next,
                        icon_size=18,
                        tooltip="Наступне (Enter)",
                        icon_color="#475569",
                        style=ft.ButtonStyle(padding=ft.padding.all(2)),
                        visible=not _is_row_filter,
                    ),
                    ft.IconButton(
                        ft.Icons.CLOSE,
                        on_click=_close,
                        icon_size=18,
                        tooltip="Закрити (Esc)",
                        icon_color="#475569",
                        style=ft.ButtonStyle(padding=ft.padding.all(2)),
                    ),
                ],
                spacing=2,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor="white",
            border=ft.border.all(1, "#CBD5E1"),
            border_radius=8,
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            shadow=ft.BoxShadow(
                spread_radius=1,
                blur_radius=12,
                color=ft.Colors.with_opacity(0.12, ft.Colors.BLACK),
                offset=ft.Offset(0, 4),
            ),
            width=460,
            right=20,
            top=56,
        )

        # `panel` already carries its own position (right/top) — page.overlay
        # children are full-screen-positioned by Flutter already, so wrapping
        # it in an expand=True Stack here would create a full-screen hit-test
        # area that silently blocks scroll/pointer input across the whole app
        # for as long as the search box is open (same bug already fixed for
        # the parser progress banners in main.py and gsm_parser_view.py).
        self._search_overlay_stack = panel
        self.page.overlay.append(panel)
        with contextlib.suppress(Exception):
            self.page.update()

    # ────────────────────────────────────────────────────────────────────────
    # Ctrl+Scroll page zoom
    # ────────────────────────────────────────────────────────────────────────

    def _zoom_step(self, direction: int) -> None:
        steps = self._zoom_steps
        current = self._zoom_factor
        # Find nearest step and move one step in direction
        idx = min(range(len(steps)), key=lambda i: abs(steps[i] - current))
        new_idx = max(0, min(len(steps) - 1, idx + direction))
        target = steps[new_idx]
        if abs(target - current) < 0.001:
            return
        self._zoom_factor = target
        self._zoom_apply_seq += 1
        self.page.run_task(self._apply_zoom_async, self._zoom_apply_seq)

    # ────────────────────────────────────────────────────────────────────────
    # Mobile pinch-to-zoom (GestureDetector on_scale_*)
    # ────────────────────────────────────────────────────────────────────────

    def _on_mobile_scale_start(self, _e=None) -> None:
        self._mobile_pinch_start_factor = self._zoom_factor

    def _on_mobile_scale_update(self, e) -> None:
        try:
            raw = float(self._mobile_pinch_start_factor) * float(
                getattr(e, "scale", 1.0) or 1.0
            )
        except Exception:
            return
        target = max(self._zoom_min, min(self._zoom_max, raw))
        self._zoom_factor = target
        ml = self._main_layout_ref
        if ml is not None:
            # Live visual feedback only during the gesture — a pure Transform.scale,
            # no width/height relayout (that happens once at gesture end in
            # _apply_zoom_async) so rapid pinch updates stay smooth.
            ml.scale = ft.Scale(scale=target, alignment=ft.Alignment(-1, -1))
            with contextlib.suppress(Exception):
                ml.update()

    def _on_mobile_scale_end(self, _e=None) -> None:
        self._zoom_apply_seq += 1
        self.page.run_task(self._apply_zoom_async, self._zoom_apply_seq)

    def _zoom_page_dims(self) -> tuple[float, float]:
        pw = getattr(self.page, "width", None) or 0
        ph = getattr(self.page, "height", None) or 0
        if not pw:
            try:
                pw = self.page.window.width or 1280
            except Exception:
                pw = 1280
        if not ph:
            try:
                ph = self.page.window.height or 800
            except Exception:
                ph = 800
        return float(pw), float(ph)

    async def _apply_zoom_async(self, seq: int) -> None:
        if self._zoomable is None:
            return
        # 50 ms debounce: coalesces rapid scroll events into one render
        await asyncio.sleep(0.05)
        if self._zoom_apply_seq != seq:
            return
        target = self._zoom_factor
        with contextlib.suppress(Exception):
            self.page._zoom_factor = target
        # Save zoom per tab
        vkey = self._current_cache_key()
        if abs(target - 1.0) < 0.001:
            self._zoom_by_vkey.pop(vkey, None)
        else:
            self._zoom_by_vkey[vkey] = target
        ml = self._main_layout_ref
        if ml is not None:
            if abs(target - 1.0) < 0.001:
                ml.scale = None
                ml.width = None
                ml.height = None
            else:
                pw, ph = self._zoom_page_dims()
                # Stack height ≈ page_height − header(~88px) − footer(~60px) − spacing(~12px)
                stack_h = max(300.0, ph - 160.0)
                # layout width/height Г— scale = visual width/height = Stack bounds → no overflow
                ml.width = pw / target
                ml.height = stack_h / target
                ml.scale = ft.Scale(scale=target, alignment=ft.Alignment(-1, -1))
            with contextlib.suppress(Exception):
                ml.update()
        self._show_zoom_indicator()
        # Notify active view to re-render with the new effective viewport width.
        with contextlib.suppress(Exception):
            _rh = getattr(self.page, "on_resized", None) or getattr(
                self.page, "on_resize", None
            )
            if callable(_rh):
                _rh(None)

    async def _reset_zoom_async(self) -> None:
        if abs(getattr(self, "_zoom_factor", 1.0) - 1.0) < 0.001:
            return
        self._zoom_factor = 1.0
        with contextlib.suppress(Exception):
            self.page._zoom_factor = 1.0
        vkey = self._current_cache_key()
        self._zoom_by_vkey.pop(vkey, None)
        if self._main_layout_ref is not None:
            self._main_layout_ref.scale = None
            self._main_layout_ref.width = None
            self._main_layout_ref.height = None
            with contextlib.suppress(Exception):
                self._main_layout_ref.update()
        if self._zoom_indicator is not None:
            self._zoom_indicator.visible = False
            with contextlib.suppress(Exception):
                self._zoom_indicator.update()
        with contextlib.suppress(Exception):
            _rh = getattr(self.page, "on_resized", None) or getattr(
                self.page, "on_resize", None
            )
            if callable(_rh):
                _rh(None)

    def _show_zoom_indicator(self) -> None:
        if self._zoom_indicator is None or self._zoom_indicator_text is None:
            return
        pct = int(round(self._zoom_factor * 100))
        self._zoom_indicator_text.value = f"Масштаб: {pct}%"
        self._zoom_indicator.visible = True
        with contextlib.suppress(Exception):
            self._zoom_indicator.update()
        self._zoom_indicator_seq += 1
        self.page.run_task(self._hide_zoom_indicator_delayed, self._zoom_indicator_seq)

    async def _hide_zoom_indicator_delayed(self, seq: int) -> None:
        await asyncio.sleep(3.0)
        if getattr(self, "_zoom_indicator_seq", 0) != seq:
            return
        if self._zoom_indicator is not None:
            self._zoom_indicator.visible = False
            with contextlib.suppress(Exception):
                self._zoom_indicator.update()
