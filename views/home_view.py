import asyncio
import contextlib
import time
from datetime import date, datetime, timedelta

import flet as ft

import fx_rates
import i18n
from views.async_view_build_helper import (
    create_orders_header_loading_indicator, run_async_build)
from views.number_format import safe_float

# Sync-pulse scopes covering every table the Home KPI grid reads from.
# Used by resume() to decide whether a full rebuild is needed on tab switch —
# see database.py's self._pulse(company_id, scope) call sites for what bumps
# each scope (orders/suppliers/products/packaging/catalog changes, packaging
# supply+writeoff transactions, payments, schedule tasks, media plan entries,
# pricing, company-level settings incl. regions/delivery methods).
HOME_PULSE_SCOPES = [
    "orders",
    "suppliers",
    "products",
    "packaging",
    "catalog",
    "payments",
    "schedule",
    "media_plan",
    "pricing",
    "company",
]

# ---------------------------------------------------------------------------
# Theme colour shims — module defaults, shadowed by live theme at render time
# ---------------------------------------------------------------------------
_OK = "#16A34A"
_ERR = "#DC2626"
_WARN_T = "#92400E"
_ACCENT = "#1D4ED8"
_STONE = "#78716C"
_WARN = "#F59E0B"
_BORDER = "#D6DCE6"
_PANEL = "#FFFFFF"
_STONE2 = "#44403C"
_WARN_BG = "#FEF9C3"
_HDR = "#E2E8F0"
_OK_T = "#166534"
_FDE68A = "#FDE68A"
_FED7AA = "#FED7AA"
_ERR_DK = "#9A3412"
_WARN2 = "#D97706"
_MUTED = "#6B7280"
_SKY_BG = "#93C5FD"
_MUTED2 = "#64748B"
_SOFT = "#EFF6FF"
_INK = "#1C1917"


def _th_from_view(view) -> dict:
    from views.themes import get_active_theme

    return get_active_theme(str(getattr(view, "manager_id", "") or ""))


try:
    from logger import logger as _home_logger
except Exception:
    import logging

    _home_logger = logging.getLogger("home_view")


class HomeView:
    HEADER_SIZE = 24
    META_TEXT_SIZE = 12
    KPI_CARD_SIZE = 100
    KPI_LABEL_SIZE = 10
    KPI_COUNT_SIZE = 15
    KPI_META_SIZE = 10

    def __init__(
        self,
        content_area,
        user_data,
        db,
        current_region_name,
        active_region=None,
        on_open_schedule=None,
        on_open_need_to_order=None,
        on_open_overdue=None,
        on_open_orders_postprod_not_submitted=None,
        on_open_cp_orders_missing_logistics_fact=None,
        on_open_orders_missing_status_date=None,
        on_open_supplier_debt=None,
        on_open_pending_series_to_product=None,
        on_open_packaging_supplier_debt=None,
        on_open_packaging_low_stock=None,
        on_open_media_plan_stale=None,
        on_open_media_plan_schedule=None,
        on_open_categories_missing_target_margin=None,
        on_open_incomplete_products=None,
        on_open_incomplete_suppliers=None,
        on_open_orders_missing_fact_weight=None,
        on_open_all_suppliers=None,
        on_open_all_categories=None,
        on_open_all_products=None,
        on_open_all_packaging=None,
        on_open_orders_status_production=None,
        on_open_orders_status_ready_for_shipment=None,
        on_open_orders_status_in_transit=None,
        on_open_orders_status_at_warehouse=None,
        on_open_orders_status_accepted=None,
        get_orders_override=None,
        on_general_selection_changed=None,
    ):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.current_region_name = current_region_name
        # Raw region id (or None for all regions), same as passed to other views.
        self.active_region = active_region if active_region else None
        fallback_open = on_open_schedule if callable(on_open_schedule) else None
        self.on_open_need_to_order = (
            on_open_need_to_order if callable(on_open_need_to_order) else fallback_open
        )
        self.on_open_overdue = (
            on_open_overdue if callable(on_open_overdue) else fallback_open
        )
        self.on_open_orders_postprod_not_submitted = (
            on_open_orders_postprod_not_submitted
            if callable(on_open_orders_postprod_not_submitted)
            else None
        )
        self.on_open_cp_orders_missing_logistics_fact = (
            on_open_cp_orders_missing_logistics_fact
            if callable(on_open_cp_orders_missing_logistics_fact)
            else None
        )
        self.on_open_orders_missing_status_date = (
            on_open_orders_missing_status_date
            if callable(on_open_orders_missing_status_date)
            else None
        )
        self.on_open_supplier_debt = (
            on_open_supplier_debt if callable(on_open_supplier_debt) else None
        )
        self.on_open_pending_series_to_product = (
            on_open_pending_series_to_product
            if callable(on_open_pending_series_to_product)
            else None
        )
        self.on_open_packaging_supplier_debt = (
            on_open_packaging_supplier_debt
            if callable(on_open_packaging_supplier_debt)
            else None
        )
        self.on_open_packaging_low_stock = (
            on_open_packaging_low_stock
            if callable(on_open_packaging_low_stock)
            else None
        )
        self.on_open_media_plan_stale = (
            on_open_media_plan_stale if callable(on_open_media_plan_stale) else None
        )
        self.on_open_media_plan_schedule = (
            on_open_media_plan_schedule
            if callable(on_open_media_plan_schedule)
            else None
        )
        self.on_open_categories_missing_target_margin = (
            on_open_categories_missing_target_margin
            if callable(on_open_categories_missing_target_margin)
            else None
        )
        self.on_open_incomplete_products = (
            on_open_incomplete_products
            if callable(on_open_incomplete_products)
            else None
        )
        self.on_open_incomplete_suppliers = (
            on_open_incomplete_suppliers
            if callable(on_open_incomplete_suppliers)
            else None
        )
        self.on_open_orders_missing_fact_weight = (
            on_open_orders_missing_fact_weight
            if callable(on_open_orders_missing_fact_weight)
            else None
        )
        self.on_open_all_suppliers = (
            on_open_all_suppliers if callable(on_open_all_suppliers) else None
        )
        self.on_open_all_categories = (
            on_open_all_categories if callable(on_open_all_categories) else None
        )
        self.on_open_all_products = (
            on_open_all_products if callable(on_open_all_products) else None
        )
        self.on_open_all_packaging = (
            on_open_all_packaging if callable(on_open_all_packaging) else None
        )
        self.on_open_orders_status_production = (
            on_open_orders_status_production
            if callable(on_open_orders_status_production)
            else None
        )
        self.on_open_orders_status_ready_for_shipment = (
            on_open_orders_status_ready_for_shipment
            if callable(on_open_orders_status_ready_for_shipment)
            else None
        )
        self.on_open_orders_status_in_transit = (
            on_open_orders_status_in_transit
            if callable(on_open_orders_status_in_transit)
            else None
        )
        self.on_open_orders_status_at_warehouse = (
            on_open_orders_status_at_warehouse
            if callable(on_open_orders_status_at_warehouse)
            else None
        )
        self.on_open_orders_status_accepted = (
            on_open_orders_status_accepted
            if callable(on_open_orders_status_accepted)
            else None
        )

        self.company_id = self.user_data.get("company_id")
        self.manager_id = self.user_data.get("active_manager_id") or self.user_data.get(
            "id"
        )
        try:
            self._effective_features = self.db.get_effective_features(
                self.manager_id, self.company_id
            )
        except Exception:
            self._effective_features = {}
        self.t = i18n.bind(str(self.user_data.get("id") or ""))
        self._feat_on = lambda key: bool(self._effective_features.get(key, True))
        role_raw = (self.user_data.get("role") or "").strip().lower()
        self.is_admin_like = role_raw in {
            "адмін",
            "админ",
            "admin",
            "суперадмін",
            "superadmin",
        }
        # "Загальні" mode
        self.general_mode: bool = bool(self.user_data.get("general_mode", False))
        self.general_manager_ids: list[str] = list(
            self.user_data.get("general_manager_ids") or []
        )
        self.all_subordinates_flat: list[dict] = list(
            self.user_data.get("all_subordinates_flat") or []
        )
        self.on_general_selection_changed = (
            on_general_selection_changed
            if callable(on_general_selection_changed)
            else None
        )
        self._header_loading_indicator = None
        self._async_build_token = 0
        self._prefetch_cache = None
        self._last_home_resize_rebuild_ts = 0.0
        self._get_live_orders = get_orders_override
        self._live_orders_snapshot: list | None = None
        self._build_completed_ts = 0.0
        # Cache-vs-DB pulse: set after each successful build, checked on resume().
        # None = never built → always rebuild. Covers every scope Home reads
        # from (orders/suppliers/products/packaging/catalog/payments/schedule/
        # media_plan/pricing/company) — see HOME_PULSE_SCOPES.
        self._home_scope_pulses: dict | None = None
        # When True, _all_orders_unfiltered() skips the shared-orders fast path
        # and always reads fresh data from DB (set by resume() before each rebuild).
        self._force_fresh_from_db: bool = False

    def stop(self):
        """Invalidate any scheduled/in-flight async build for fast tab switching."""
        self._async_build_token = int(getattr(self, "_async_build_token", 0) or 0) + 1
        self._prefetch_cache = None

    async def resume(self, silent: bool = False) -> None:
        """Rebuild all counters from DB on navigation back to this tab —
        unless a cheap pulse check shows nothing changed since the last build,
        in which case the already-rendered content is left as-is (same
        pulse-skip pattern used by order_schedule_view/media_plan_view).

        silent=True is used for background live-sync refreshes (triggered by
        a remote DB pulse while this tab may be the one currently on screen)
        — it skips the loading-stub content wipe so the KPI grid doesn't
        blink to a spinner while the manager is reading it. See build().
        """
        if not self.content_area:
            return
        pg = getattr(self.content_area, "page", None)
        if not pg:
            return

        _saved_controls = list(getattr(self.content_area, "controls", None) or [])
        _data_changed = True
        if self._home_scope_pulses and _saved_controls:
            try:
                new_pulses = await asyncio.to_thread(
                    self.db.get_companies_sync_pulses,
                    [self.company_id],
                    HOME_PULSE_SCOPES,
                )
                new_pulses = (new_pulses or {}).get(self.company_id) or {}
                _data_changed = new_pulses != self._home_scope_pulses
            except Exception:
                _data_changed = True

        if not _data_changed:
            return

        self._live_orders_snapshot = None
        # Force DB reads: disable the live-orders fast path so _all_orders_unfiltered()
        # and _orders_in_scope() always hit the DB instead of the potentially-stale
        # shared orders map maintained by the dashboard.
        self._force_fresh_from_db = True
        # Delegate to build() which runs _build_sync in a background thread
        # with a timeout — avoids blocking the asyncio event loop on DB calls.
        self.build(silent=silent)

    def _home_viewport_width(self) -> int:
        def _to_int(value) -> int:
            try:
                return int(float(value))
            except Exception:
                return 0

        host_w = _to_int(getattr(self.content_area, "width", 0))
        if host_w > 0:
            return max(320, int(host_w * 0.99))

        page = getattr(self.content_area, "page", None)
        page_w = _to_int(getattr(page, "width", 0)) if page else 0
        if page_w <= 0:
            win = getattr(page, "window", None) if page else None
            page_w = _to_int(getattr(win, "width", 0)) if win else 0
        if page_w <= 0:
            return 720
        return max(320, int(page_w * 0.99))

    def _resolve_region_id(self):
        """Resolve region token to region id; supports id or display name.

        Result is cached per build (via _prefetch_get) — this is called from
        ~30 different helper methods during a single _build_sync() run, and
        get_company_regions() is a plain uncached query, so without caching
        every call paid a full extra DB round-trip.
        """
        token = str(self.active_region or self.current_region_name or "").strip()
        if not token or token in {"__all_regions__", "Всі регіони"}:
            return None

        def _load():
            rows = self.db.get_company_regions(self.company_id) or []
            token_l = token.lower()
            ids = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                rid = str(row.get("id") or "").strip()
                rname = str(row.get("name") or "").strip()
                if rid:
                    ids.add(rid)
                if token == rid:
                    return rid
                if rname and token_l == rname.lower():
                    return rid or None

            # Fallback: if token already looks like an id but region list wasn't available/matched.
            if token in ids:
                return token
            return token

        return self._prefetch_get(f"resolve_region_id_{token}", _load)

    @staticmethod
    def _safe_float(value, default=None):
        return safe_float(value, default)

    def _prefetch_get(self, key: str, loader):
        cache = self._prefetch_cache if isinstance(self._prefetch_cache, dict) else None
        if cache is not None and key in cache:
            return cache.get(key)
        value = loader()
        if cache is not None:
            cache[key] = value
        return value

    def _delivery_methods_in_scope(self) -> list[dict]:
        if not self.company_id:
            return []

        def _load():
            try:
                region_id = self._resolve_region_id()
                mgr_ids = (
                    [str(m).strip() for m in self.general_manager_ids if m]
                    if (self.general_mode and self.general_manager_ids)
                    else [self.manager_id]
                )
                seen_ids: set[str] = set()
                result: list[dict] = []
                for mid in mgr_ids:
                    for row in (
                        self.db.get_effective_delivery_methods_for_manager(
                            self.company_id, mid, region_id=region_id
                        )
                        or []
                    ):
                        if not isinstance(row, dict):
                            continue
                        rid = str(row.get("id") or "").strip()
                        if rid and rid not in seen_ids:
                            seen_ids.add(rid)
                            result.append(row)
                return result
            except Exception:
                return []

        return self._prefetch_get("home_delivery_methods", _load) or []

    def _delivery_methods_name_by_id(self) -> dict[str, str]:
        mapping = {}
        for row in self._delivery_methods_in_scope():
            mid = str(row.get("id") or "").strip()
            name = str(row.get("name") or row.get("label") or "").strip()
            if mid and name:
                mapping[mid] = name
        return mapping

    def _delivery_label_for_order(self, order: dict, id_to_name: dict[str, str]) -> str:
        label = str(
            order.get("delivery_type_name") or order.get("delivery_type") or ""
        ).strip()
        if label:
            return label
        did = str(order.get("delivery_type_id") or "").strip()
        if did and did in (id_to_name or {}):
            return str(id_to_name.get(did) or "").strip()
        return "Без типу доставки"

    def _schedule_tasks_in_scope(self, include_archived: bool = True):
        if not self.company_id:
            return []

        def _load_all():
            try:
                region_id = self._resolve_region_id()
                if self.general_mode and self.general_manager_ids:
                    seen_ids: set[str] = set()
                    combined: list[dict] = []
                    for mid in self.general_manager_ids:
                        try:
                            rows = (
                                self.db.get_order_schedule_tasks(
                                    self.company_id,
                                    manager_id=mid,
                                    region_id=region_id,
                                    include_archived=True,
                                )
                                or []
                            )
                            for t in rows:
                                if not isinstance(t, dict):
                                    continue
                                tid = str(t.get("id") or "").strip()
                                if tid and tid not in seen_ids:
                                    seen_ids.add(tid)
                                    combined.append(t)
                                elif not tid:
                                    combined.append(t)
                        except Exception:
                            pass
                    return combined
                tasks = self.db.get_order_schedule_tasks(
                    self.company_id,
                    manager_id=self.manager_id,
                    region_id=region_id,
                    include_archived=True,
                )
                return [row for row in (tasks or []) if isinstance(row, dict)]
            except Exception:
                return []

        all_tasks = self._prefetch_get("home_schedule_tasks_all", _load_all) or []
        if include_archived:
            return all_tasks
        return [t for t in all_tasks if not bool((t or {}).get("archived"))]

    def _suppliers_in_scope(self):
        if not self.company_id:
            return []

        def _load():
            try:
                region_id = self._resolve_region_id()
                if self.general_mode and self.general_manager_ids:
                    seen_ids: set[str] = set()
                    combined: list[dict] = []
                    for mid in self.general_manager_ids:
                        try:
                            rows = (
                                self.db.get_suppliers(
                                    self.company_id, region_id=region_id, manager_id=mid
                                )
                                or []
                            )
                            for r in rows:
                                if not isinstance(r, dict):
                                    continue
                                rid = str(r.get("id") or "").strip()
                                if rid and rid not in seen_ids:
                                    seen_ids.add(rid)
                                    combined.append(r)
                        except Exception:
                            pass
                    return combined
                rows = self.db.get_suppliers(
                    self.company_id,
                    region_id=region_id,
                    manager_id=self.manager_id,
                )
                return [row for row in (rows or []) if isinstance(row, dict)]
            except Exception:
                return []

        return self._prefetch_get("home_suppliers_scope", _load) or []

    def _products_in_scope(self):
        if not self.company_id:
            return []

        def _load():
            try:
                region_id = self._resolve_region_id()
                if self.general_mode and self.general_manager_ids:
                    seen_ids: set[str] = set()
                    combined: list[dict] = []
                    for mid in self.general_manager_ids:
                        try:
                            rows = (
                                self.db.get_products(
                                    self.company_id, region_id=region_id, manager_id=mid
                                )
                                or []
                            )
                            for r in rows:
                                if not isinstance(r, dict):
                                    continue
                                rid = str(r.get("id") or "").strip()
                                if rid and rid not in seen_ids:
                                    seen_ids.add(rid)
                                    combined.append(r)
                        except Exception:
                            pass
                    return combined
                rows = self.db.get_products(
                    self.company_id,
                    region_id=region_id,
                    manager_id=self.manager_id,
                )
                return [row for row in (rows or []) if isinstance(row, dict)]
            except Exception:
                return []

        return self._prefetch_get("home_products_scope", _load) or []

    def _categories_in_scope(self):
        if not self.company_id:
            return []

        def _load():
            try:
                region_id = self._resolve_region_id()
                if self.general_mode and self.general_manager_ids:
                    seen_ids: set[str] = set()
                    combined: list[dict] = []
                    for mid in self.general_manager_ids:
                        try:
                            rows = (
                                self.db.get_accessible_categories(
                                    self.company_id, mid, region_id
                                )
                                or []
                            )
                            for r in rows:
                                if not isinstance(r, dict):
                                    continue
                                rid = str(r.get("id") or "").strip()
                                if rid and rid not in seen_ids:
                                    seen_ids.add(rid)
                                    combined.append(r)
                        except Exception:
                            pass
                    return combined
                rows = self.db.get_accessible_categories(
                    self.company_id,
                    self.manager_id,
                    region_id,
                )
                return [row for row in (rows or []) if isinstance(row, dict)]
            except Exception:
                return []

        return self._prefetch_get("home_categories_scope", _load) or []

    @staticmethod
    def _parse_date(raw):
        txt = str(raw or "").strip()
        if not txt:
            return None
        try:
            return datetime.fromisoformat(txt[:10]).date()
        except Exception:
            return None

    def _task_needs_order(self, task, today):
        if not isinstance(task, dict):
            return False
        if bool(task.get("archived")):
            return False

        status = str(task.get("status") or "not_ordered").strip().lower()
        if status == "ordered":
            return False

        date_from = self._parse_date(task.get("date_from")) or self._parse_date(
            task.get("month_anchor")
        )
        date_to = self._parse_date(task.get("date_to"))
        if not date_from:
            return False

        if today < date_from:
            return False
        if date_to and today > date_to:
            return False
        return True

    def _count_need_to_order(self):
        if not self.company_id:
            return 0
        tasks = self._schedule_tasks_in_scope(include_archived=False)
        today = date.today()
        return sum(1 for t in (tasks or []) if self._task_needs_order(t, today))

    def _task_is_overdue(self, task, today):
        if not isinstance(task, dict):
            return False
        if bool(task.get("archived")):
            return False
        status = str(task.get("status") or "not_ordered").strip().lower()
        if status == "ordered":
            return False
        due_date = self._parse_date(task.get("date_to")) or self._parse_date(
            task.get("month_anchor")
        )
        if not due_date:
            return False
        return due_date < today

    def _count_overdue(self):
        if not self.company_id:
            return 0
        tasks = self._schedule_tasks_in_scope(include_archived=False)
        today = date.today()
        return sum(1 for t in (tasks or []) if self._task_is_overdue(t, today))

    def _count_orders_postprod_not_submitted(self):
        if not self.company_id:
            return 0
        orders = self._all_orders_unfiltered()
        total = 0
        for order in orders or []:
            if not isinstance(order, dict):
                continue
            status = self._normalize_order_status(order.get("status"))
            submission = (
                str(order.get("submission_status") or "not_submitted").strip().lower()
            )
            if status != "production" and submission == "not_submitted":
                total += 1
        return total

    def _count_cp_orders_missing_logistics_fact(self):
        if not self.company_id:
            return 0
        orders = self._all_orders_unfiltered()
        total = 0
        _statuses_seen: dict = {}
        for order in orders or []:
            if not isinstance(order, dict):
                continue
            status = self._normalize_order_status(order.get("status"))
            _statuses_seen[status] = _statuses_seen.get(status, 0) + 1
            if status not in {
                "ready_for_shipment",
                "in_transit",
                "at_warehouse",
                "accepted",
            }:
                continue
            fact_total = self._safe_float(order.get("cp_logistics_fact_total"), None)
            if fact_total is None or abs(float(fact_total)) <= 1e-9:
                total += 1
        _home_logger.info(
            "[DIAG][logistics_fact] general=%s orders_in_scope=%d statuses=%s result=%d",
            self.general_mode,
            len(orders or []),
            _statuses_seen,
            total,
        )
        return total

    def _count_orders_missing_status_date(self):
        """Count orders where the date field for the current status is missing or future."""
        if not self.company_id:
            return 0
        orders = self._all_orders_unfiltered()
        date_field_map = {
            "ready_for_shipment": "date_ready_at_supplier",
            "in_transit": "date_shipped",
            "at_warehouse": "date_arrived_warehouse",
            "accepted": "date_accepted",
        }
        today = date.today()
        total = 0
        for order in orders or []:
            if not isinstance(order, dict):
                continue
            status = self._normalize_order_status(order.get("status"))
            date_field = date_field_map.get(status)
            if date_field is None:
                continue  # production — skip
            raw = str(order.get(date_field) or "").strip()
            if not raw:
                total += 1
                continue
            try:
                d = datetime.fromisoformat(raw[:10]).date()
            except Exception:
                total += 1
                continue
            if d > today:
                total += 1
        return total

    def _count_supplier_debt(self):
        """Return (we_owe_count, they_owe_count) for product-order suppliers.
        we_owe_count:  suppliers where balance > 0 (we owe them).
        they_owe_count: suppliers where balance < 0 (they owe us).
        Applies the same order-scope filter as payments_view."""
        if not self.company_id:
            return (0, 0)
        orders = self._all_orders_unfiltered() or []
        # In general mode, load suppliers from all selected managers for currency lookup
        if self.general_mode and self.general_manager_ids:
            _sup_seen: set[str] = set()
            _sup_all: list = []
            _rid = self._resolve_region_id()
            for _mid in self.general_manager_ids:
                try:
                    _subs = (
                        self.db.get_suppliers(self.company_id, _rid, manager_id=_mid)
                        or []
                    )
                    for _s in _subs:
                        _sid = str((_s or {}).get("id") or "").strip()
                        if _sid and _sid not in _sup_seen:
                            _sup_seen.add(_sid)
                            _sup_all.append(_s)
                except Exception:
                    pass
            suppliers = _sup_all
        else:
            suppliers = self._suppliers_in_scope() or []
        try:
            # Settlements are supplier-level balances (opening/compensations) and can be
            # entered outside the exact region context. Keep orders region-scoped, but
            # take settlements across the company to avoid false positive debt suppliers.
            settlements = self.db.get_supplier_settlements(self.company_id, None) or []
        except Exception:
            settlements = []

        # Build supplier currency map (all suppliers, to resolve ccy regardless of region)
        sup_ccy = {
            str(s.get("id") or ""): str(s.get("currency") or "USD").upper().strip()
            or "USD"
            for s in suppliers
            if isinstance(s, dict)
        }

        def _in_scope(o: dict) -> bool:
            """Mirror payments_view._is_order_in_scope logic."""
            st_raw = str(o.get("status") or "production").strip()
            st = st_raw.lower()
            pay = str(o.get("payment_status") or "unpaid").strip().lower()
            is_paid = pay in {"paid", "сплачений", "сплачено", "оплачено"}
            is_advance = pay in {
                "advance",
                "аванс",
                "передплата",
                "предоплата",
                "частково",
                "частково сплачено",
            }
            is_transferred = pay in {"transferred", "передано"}
            is_production = (
                st in {"production", "виробництво", "в виробництві", "виро", "виробн."}
                or st_raw == "Виробництво"
            )
            if is_paid or is_advance:
                return True
            if is_transferred:
                return not is_production
            return not is_production

        products = self._products_in_scope() or []
        prod_by_id = {
            str((p or {}).get("id") or "").strip(): p
            for p in products
            if isinstance(p, dict)
        }

        def _order_amount_for_ccy(o: dict, supplier_ccy: str) -> float:
            """Mirror payments_view._order_amount_supplier_ccy: only count if currencies match."""
            sc = (supplier_ccy or "USD").upper().strip() or "USD"
            items = o.get("items") if isinstance(o.get("items"), list) else []
            # Order-level manual override (single-item orders).
            man = self._safe_float(o.get("amount_manual_override"), None)
            if man is not None and len(items) <= 1:
                return float(man)
            if not items:
                return 0.0
            total = 0.0
            for it in items:
                if not isinstance(it, dict):
                    continue
                # Item-level override.
                it_m = self._safe_float(it.get("amount_manual_override"), None)
                if it_m is not None:
                    total += float(it_m)
                    continue
                q = self._safe_float(it.get("qty") or it.get("quantity"), 0.0) or 0.0
                p = self._safe_float(it.get("purchase_price"), None)
                ccy = str(it.get("purchase_currency") or sc).upper().strip() or sc
                # Fallback to product catalog — same as payments_view._line_amount_supplier_ccy.
                if p is None:
                    pid = str(it.get("product_id") or "").strip()
                    prod = prod_by_id.get(pid)
                    p = self._safe_float((prod or {}).get("purchase_price"), None)
                    ccy = (
                        str((prod or {}).get("purchase_currency") or ccy or "")
                        .upper()
                        .strip()
                        or ccy
                    )
                if p is None or ccy != sc:
                    continue
                total += float(q) * float(p)
            return total

        # Build supplier -> in-scope orders map
        sup_orders: dict = {}
        for o in orders:
            if not isinstance(o, dict):
                continue
            if not _in_scope(o):
                continue
            sid = str(o.get("supplier_id") or "").strip()
            if not sid:
                continue
            sup_orders.setdefault(sid, []).append(o)

        # Include suppliers from settlements even if they have no in-scope orders
        # (e.g. production-only orders excluded from scope but opening balance exists).
        all_sup_ids = set(sup_orders.keys())
        for s in settlements:
            if isinstance(s, dict):
                sid = str(s.get("supplier_id") or "").strip()
                if sid:
                    all_sup_ids.add(sid)

        debt_count = 0
        credit_count = 0
        for sid in all_sup_ids:
            # Resolve currency: prefer explicit supplier record; fallback to most common
            # purchase_currency among in-scope orders (handles suppliers outside scope lookup).
            ccy = sup_ccy.get(sid, "")
            if not ccy:
                ccy_counts: dict = {}
                for o in sup_orders.get(sid, []):
                    for it in (
                        o.get("items") if isinstance(o.get("items"), list) else [o]
                    ):
                        ic = (
                            str((it or {}).get("purchase_currency") or "")
                            .upper()
                            .strip()
                        )
                        if ic:
                            ccy_counts[ic] = ccy_counts.get(ic, 0) + 1
                ccy = max(ccy_counts, key=ccy_counts.get) if ccy_counts else "USD"
            debt = 0.0
            paid = 0.0
            for o in sup_orders.get(sid, []):
                debt += _order_amount_for_ccy(o, ccy)
                paid += self._safe_float(o.get("paid_amount"), 0.0) or 0.0
            sett = 0.0
            for s in settlements:
                if not isinstance(s, dict):
                    continue
                if str(s.get("supplier_id") or "").strip() != sid:
                    continue
                s_ccy = str(s.get("currency") or ccy).upper().strip() or ccy
                if s_ccy == ccy:
                    sett += self._safe_float(s.get("amount"), 0.0) or 0.0
            balance = (debt - paid) + sett
            if balance > 0.0001:
                debt_count += 1
            elif balance < -0.0001:
                credit_count += 1
        return (debt_count, credit_count)

    def _count_pending_series_to_product(self):
        """Count series candidates that are not completed into product cards yet."""
        if not self.company_id:
            return 0
        try:
            from views.products_view import ProductsView

            if self.general_mode and self.general_manager_ids:
                total = 0
                for mid in self.general_manager_ids:
                    try:
                        total += self._count_pending_series_for_manager(mid)
                    except Exception:
                        pass
                return total

            # When viewing another manager, ensure probe_user reflects their context
            is_viewing_other = self.manager_id != self.user_data.get("id")
            probe_manager_id = self.manager_id
            probe_role = self.user_data.get("role")

            probe_user = {
                "company_id": self.company_id,
                "active_manager_id": probe_manager_id,
                "id": (
                    probe_manager_id if is_viewing_other else self.user_data.get("id")
                ),
                "role": probe_role,
            }
            pv = ProductsView(
                content_area=None,
                user_data=probe_user,
                db=self.db,
                current_region_name=(
                    self.current_region_name
                    if str(self.current_region_name or "").strip()
                    else self.active_region
                ),
            )
            region_id = self._resolve_region_id()
            candidates = pv._pending_series_candidates(self.company_id, region_id) or []
            if not candidates:
                return 0

            # Mirror Products dialog logic: hide series that already have a product
            # directly on the series node or on any deeper detail node.
            company_nodes = self.db.get_company_catalog_nodes(self.company_id) or []
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

            # When viewing another manager, filter products by their manager_id
            products_manager_id = probe_manager_id
            products_in_scope = (
                self.db.get_products(
                    self.company_id, region_id, manager_id=products_manager_id
                )
                or []
            )
            for prod in products_in_scope:
                if not isinstance(prod, dict):
                    continue
                node_id = str(prod.get("catalog_node_id") or "").strip()
                if not node_id:
                    continue

                if node_id in candidate_series_ids:
                    series_with_detail_ids.add(node_id)
                    continue

                cur = node_id
                hops = 0
                while cur and hops < 30:
                    if cur in candidate_series_ids and cur != node_id:
                        series_with_detail_ids.add(cur)
                        break
                    node = node_by_id.get(cur) or {}
                    cur = str(
                        node.get("parent_node_id") or node.get("parent_id") or ""
                    ).strip()
                    hops += 1

            filtered = [
                c
                for c in candidates
                if isinstance(c, dict)
                and (c.get("catalog_node_id") not in series_with_detail_ids)
            ]
            return len(filtered)
        except Exception:
            return 0

    def _count_pending_series_for_manager(self, probe_manager_id: str) -> int:
        """Count pending series-to-product for a single manager."""
        try:
            from views.products_view import ProductsView

            probe_user = {
                "company_id": self.company_id,
                "active_manager_id": probe_manager_id,
                "id": probe_manager_id,
                "role": self.user_data.get("role"),
            }
            pv = ProductsView(
                content_area=None,
                user_data=probe_user,
                db=self.db,
                current_region_name=(
                    self.current_region_name
                    if str(self.current_region_name or "").strip()
                    else self.active_region
                ),
            )
            region_id = self._resolve_region_id()
            candidates = pv._pending_series_candidates(self.company_id, region_id) or []
            if not candidates:
                return 0
            company_nodes = self.db.get_company_catalog_nodes(self.company_id) or []
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
            series_with_detail_ids: set = set()
            products_in_scope = (
                self.db.get_products(
                    self.company_id, region_id, manager_id=probe_manager_id
                )
                or []
            )
            for prod in products_in_scope:
                if not isinstance(prod, dict):
                    continue
                node_id = str(prod.get("catalog_node_id") or "").strip()
                if not node_id:
                    continue
                if node_id in candidate_series_ids:
                    series_with_detail_ids.add(node_id)
                    continue
                cur, hops = node_id, 0
                while cur and hops < 30:
                    if cur in candidate_series_ids and cur != node_id:
                        series_with_detail_ids.add(cur)
                        break
                    node = node_by_id.get(cur) or {}
                    cur = str(
                        node.get("parent_node_id") or node.get("parent_id") or ""
                    ).strip()
                    hops += 1
            return len(
                [
                    c
                    for c in candidates
                    if isinstance(c, dict)
                    and c.get("catalog_node_id") not in series_with_detail_ids
                ]
            )
        except Exception:
            return 0

    def _packaging_probe_for_manager(self, probe_manager_id: str):
        """Build (and cache for this build) a loaded PackagingStockView plus
        that manager's visible product ids.

        _count_packaging_supplier_debt(_for_manager), _count_packaging_low_stock
        (_for_manager) and _packaging_stock_rows each used to independently
        construct a PackagingStockView and call its (deliberately uncached,
        force_refresh=True) _load_packaging_data() for the SAME manager — up
        to 3x redundant heavy loads per manager per Home build. Sharing one
        load per manager here cuts that duplication.
        """
        def _load():
            from views.packaging_stock_view import PackagingStockView

            probe_user = {
                "company_id": self.company_id,
                "active_manager_id": probe_manager_id,
                "id": probe_manager_id,
                "role": self.user_data.get("role"),
            }
            ps = PackagingStockView(
                content_area=None,
                user_data=probe_user,
                db=self.db,
                region_ref=self._resolve_region_id(),
            )
            ps._load_packaging_data()
            visible_product_ids: set = set()
            for rid in ps._region_ids_for_scope() or [None]:
                for p in (
                    self.db.get_products(
                        self.company_id, rid, manager_id=probe_manager_id
                    )
                    or []
                ):
                    if isinstance(p, dict):
                        pid = str(p.get("id") or "").strip()
                        if pid:
                            visible_product_ids.add(pid)
            return ps, visible_product_ids

        return self._prefetch_get(f"packaging_probe_{probe_manager_id}", _load)

    def _count_packaging_supplier_debt_for_manager(self, probe_manager_id: str) -> set:
        """Return set of debt packaging supplier IDs for a single manager."""
        try:
            ps, visible_product_ids = self._packaging_probe_for_manager(probe_manager_id)
            debt_suppliers: set = set()
            for ctx in ps._packaging_items or []:
                if not isinstance(ctx, dict):
                    continue
                linked_ids = ps._ctx_linked_product_ids(ctx)
                if not linked_ids:
                    continue
                if visible_product_ids and not any(
                    pid in visible_product_ids for pid in linked_ids
                ):
                    continue
                sid = str(ctx.get("packaging_supplier_id") or "").strip()
                if not sid:
                    continue
                ctx_id = str(ctx.get("id") or "").strip()
                if not ctx_id:
                    continue
                _rid_ctx = str(ctx.get("region_id") or "").strip()
                opening = self.db.get_packaging_supply_opening(ctx_id) or {}
                rows = [
                    x
                    for x in (self.db.list_packaging_supply_orders(ctx_id) or [])
                    if isinstance(x, dict)
                ]
                wo_rows = [
                    {**x, "row_kind": "writeoff"}
                    for x in (
                        self.db.get_packaging_writeoffs(
                            ctx_id, manager_id=probe_manager_id, region_id=_rid_ctx
                        )
                        or []
                    )
                    if isinstance(x, dict)
                ]
                eff_pack, eff_goods = ps._packaging_journal_final_stocks(
                    opening, rows, wo_rows
                )
                paid_total = sum(float(x.get("paid_amount") or 0) for x in rows)
                balance = float(eff_pack) + float(eff_goods) - paid_total
                if balance > 0.001:
                    debt_suppliers.add(sid)
            return debt_suppliers
        except Exception:
            return set()

    def _count_packaging_supplier_debt(self):
        """Count packaging suppliers where packaging context balance is positive."""
        if not self.company_id or not self._feat_on("packaging"):
            return 0
        try:
            if self.general_mode and self.general_manager_ids:
                all_debt_suppliers: set[str] = set()
                for mid in self.general_manager_ids:
                    try:
                        all_debt_suppliers |= (
                            self._count_packaging_supplier_debt_for_manager(mid)
                        )
                    except Exception:
                        pass
                return len(all_debt_suppliers)

            probe_manager_id = self.manager_id
            ps, visible_product_ids = self._packaging_probe_for_manager(probe_manager_id)

            debt_suppliers = set()
            boxes_by_id = ps._packaging_boxes_by_id or {}
            for ctx in ps._packaging_items or []:
                if not isinstance(ctx, dict):
                    continue
                linked_ids = ps._ctx_linked_product_ids(ctx)
                if not linked_ids:
                    continue
                if visible_product_ids and not any(
                    pid in visible_product_ids for pid in linked_ids
                ):
                    continue
                sid = str(ctx.get("packaging_supplier_id") or "").strip()
                if not sid:
                    continue
                ctx_id = str(ctx.get("id") or "").strip()
                if not ctx_id:
                    continue
                opening = self.db.get_packaging_supply_opening(ctx_id) or {}
                rows = self.db.list_packaging_supply_orders(ctx_id) or []
                pack_row = (
                    boxes_by_id.get(str(ctx.get("packaging_id") or "").strip()) or {}
                )
                bal = ps._packaging_context_balance_net(opening, rows, pack_row)
                if float(bal) > 0.0001:
                    debt_suppliers.add(sid)
            return len(debt_suppliers)
        except Exception:
            return 0

    def _count_packaging_low_stock(self):
        """Return low packaging contexts count as (yellow_lt_5000, red_lt_2500)."""
        if not self.company_id or not self._feat_on("pack_stock"):
            return 0, 0
        try:
            if self.general_mode and self.general_manager_ids:
                total_yellow, total_red = 0, 0
                for mid in self.general_manager_ids:
                    try:
                        y, r = self._count_packaging_low_stock_for_manager(mid)
                        total_yellow += y
                        total_red += r
                    except Exception:
                        pass
                return total_yellow, total_red

            probe_manager_id = self.manager_id
            ps, visible_product_ids = self._packaging_probe_for_manager(probe_manager_id)

            yellow = 0
            red = 0
            for ctx in ps._packaging_items or []:
                if not isinstance(ctx, dict):
                    continue
                linked_ids = ps._ctx_linked_product_ids(ctx)
                if not linked_ids:
                    continue
                if visible_product_ids and not any(
                    pid in visible_product_ids for pid in linked_ids
                ):
                    continue
                ctx_id = str(ctx.get("id") or "").strip()
                if not ctx_id:
                    continue
                _rid_ctx = str(ctx.get("region_id") or "").strip()
                opening = self.db.get_packaging_supply_opening(ctx_id) or {}
                rows = [
                    x
                    for x in (self.db.list_packaging_supply_orders(ctx_id) or [])
                    if isinstance(x, dict)
                ]
                wo_rows = [
                    {**x, "row_kind": "writeoff"}
                    for x in (
                        self.db.get_packaging_writeoffs(
                            ctx_id, manager_id=probe_manager_id, region_id=_rid_ctx
                        )
                        or []
                    )
                    if isinstance(x, dict)
                ]
                eff_pack, eff_goods = ps._packaging_journal_final_stocks(
                    opening, rows, wo_rows
                )
                total_stock_v = float(eff_pack) + float(eff_goods)
                if total_stock_v < 2500.0:
                    red += 1
                elif 2500.0 < total_stock_v < 5000.0:
                    yellow += 1
            return yellow, red
        except Exception:
            return 0, 0

    def _count_packaging_low_stock_for_manager(self, probe_manager_id: str) -> tuple:
        """Return (yellow, red) packaging low-stock counts for a single manager."""
        try:
            ps, visible_product_ids = self._packaging_probe_for_manager(probe_manager_id)
            yellow, red = 0, 0
            for ctx in ps._packaging_items or []:
                if not isinstance(ctx, dict):
                    continue
                linked_ids = ps._ctx_linked_product_ids(ctx)
                if not linked_ids:
                    continue
                if visible_product_ids and not any(
                    pid in visible_product_ids for pid in linked_ids
                ):
                    continue
                ctx_id = str(ctx.get("id") or "").strip()
                if not ctx_id:
                    continue
                _rid_ctx = str(ctx.get("region_id") or "").strip()
                opening = self.db.get_packaging_supply_opening(ctx_id) or {}
                rows = [
                    x
                    for x in (self.db.list_packaging_supply_orders(ctx_id) or [])
                    if isinstance(x, dict)
                ]
                wo_rows = [
                    {**x, "row_kind": "writeoff"}
                    for x in (
                        self.db.get_packaging_writeoffs(
                            ctx_id, manager_id=probe_manager_id, region_id=_rid_ctx
                        )
                        or []
                    )
                    if isinstance(x, dict)
                ]
                eff_pack, eff_goods = ps._packaging_journal_final_stocks(
                    opening, rows, wo_rows
                )
                total_v = float(eff_pack) + float(eff_goods)
                if total_v < 2500.0:
                    red += 1
                elif 2500.0 < total_v < 5000.0:
                    yellow += 1
            return yellow, red
        except Exception:
            return 0, 0

    def _packaging_stock_rows(self) -> list[tuple]:
        """Return list of (product_label, pack_qty, goods_qty) for each packaging context.

        product_label = «Назва товару (Назва пакування)»
        Returns [] when no packaging data is available.
        """
        if not self.company_id or not self._feat_on("pack_stock"):
            return []
        try:
            region_id = self._resolve_region_id()

            if self.general_mode and self.general_manager_ids:
                # Collect unique packaging contexts from ALL selected managers.
                # Deduplication by ctx_id prevents double-counting supply when managers
                # share the same physical packaging context.
                seen_ctx_ids: set[str] = set()
                # list of (ctx_dict, ps_instance) — ps_instance owns product/box lookups
                unique_ctxs: list[tuple] = []

                for mid in self.general_manager_ids:
                    ps_mid, _ = self._packaging_probe_for_manager(mid)
                    for ctx in ps_mid._packaging_items or []:
                        if not isinstance(ctx, dict):
                            continue
                        ctx_id = str(ctx.get("id") or "").strip()
                        if not ctx_id or ctx_id in seen_ctx_ids:
                            continue
                        seen_ctx_ids.add(ctx_id)
                        unique_ctxs.append((ctx, ps_mid))

                if not unique_ctxs:
                    return []

                raw_by_region: dict[str, list[tuple]] = {}
                for ctx, ps_inst in unique_ctxs:
                    linked_ids = ps_inst._ctx_linked_product_ids(ctx)
                    if not linked_ids:
                        continue
                    ctx_id = str(ctx.get("id") or "").strip()
                    _rid_ctx = str(ctx.get("region_id") or "").strip()
                    # Supply loaded once per unique ctx_id — no double-counting
                    opening = self.db.get_packaging_supply_opening(ctx_id) or {}
                    supply_rows = [
                        x
                        for x in (self.db.list_packaging_supply_orders(ctx_id) or [])
                        if isinstance(x, dict)
                    ]
                    # Write-offs combined from ALL selected managers
                    wo_seen: set[str] = set()
                    writeoff_rows: list[dict] = []
                    for mid in self.general_manager_ids:
                        fetched = self.db.get_packaging_writeoffs(
                            ctx_id, manager_id=mid, region_id=_rid_ctx
                        ) or []
                        for x in fetched:
                            if not isinstance(x, dict):
                                continue
                            wid = str(x.get("id") or "").strip()
                            normed = {**x, "row_kind": "writeoff"}
                            if wid and wid not in wo_seen:
                                wo_seen.add(wid)
                                writeoff_rows.append(normed)
                            elif not wid:
                                writeoff_rows.append(normed)

                    eff_pack, eff_goods = ps_inst._packaging_journal_final_stocks(
                        opening, supply_rows, writeoff_rows
                    )

                    prod_names = []
                    for pid in linked_ids:
                        p = (ps_inst._packaging_products_by_id or {}).get(pid) or {}
                        pname = str(p.get("name") or "").strip()
                        if pname:
                            prod_names.append(pname)
                    if not prod_names:
                        prod_names = [ctx_id]

                    pack_box = (ps_inst._packaging_boxes_by_id or {}).get(
                        str(ctx.get("packaging_id") or "").strip()
                    ) or {}
                    pack_name = str(
                        pack_box.get("name") or pack_box.get("packaging_name") or ""
                    ).strip()

                    prod_label = ", ".join(prod_names)
                    raw_by_region.setdefault(_rid_ctx, []).append(
                        (prod_label, pack_name, float(eff_pack), float(eff_goods))
                    )

                # Split by region when multiple regions are present
                if len(raw_by_region) > 1 and region_id is None:
                    _rmap = self._region_name_map()
                    result: list[tuple] = []
                    for rid_key, rid_rows in sorted(
                        raw_by_region.items(), key=lambda x: _rmap.get(x[0], x[0])
                    ):
                        rname = _rmap.get(rid_key) or rid_key or "Невідомий регіон"
                        result.append(("__REGION__", rname, 0.0, 0.0))
                        result.extend(rid_rows)
                    return result

                result = []
                for rows in raw_by_region.values():
                    result.extend(rows)
                return result

            # Single manager path
            probe_manager_id = self.manager_id
            ps, visible_product_ids = self._packaging_probe_for_manager(probe_manager_id)

            if not ps._packaging_items:
                return []

            raw_by_region_s: dict[str, list[tuple]] = {}
            for ctx in ps._packaging_items or []:
                if not isinstance(ctx, dict):
                    continue
                linked_ids = ps._ctx_linked_product_ids(ctx)
                if not linked_ids:
                    continue
                if visible_product_ids and not any(
                    pid in visible_product_ids for pid in linked_ids
                ):
                    continue
                ctx_id = str(ctx.get("id") or "").strip()
                if not ctx_id:
                    continue
                _rid_ctx = str(ctx.get("region_id") or "").strip()
                opening = self.db.get_packaging_supply_opening(ctx_id) or {}
                supply_rows = [
                    x
                    for x in (self.db.list_packaging_supply_orders(ctx_id) or [])
                    if isinstance(x, dict)
                ]
                _raw_wo = self.db.get_packaging_writeoffs(
                    ctx_id, manager_id=probe_manager_id, region_id=_rid_ctx
                ) or []
                writeoff_rows = [{**x, "row_kind": "writeoff"} for x in _raw_wo if isinstance(x, dict)]
                eff_pack, eff_goods = ps._packaging_journal_final_stocks(
                    opening, supply_rows, writeoff_rows
                )

                prod_names = []
                for pid in linked_ids:
                    p = (ps._packaging_products_by_id or {}).get(pid) or {}
                    pname = str(p.get("name") or "").strip()
                    if pname:
                        prod_names.append(pname)
                if not prod_names:
                    prod_names = [ctx_id]

                pack_box = (ps._packaging_boxes_by_id or {}).get(
                    str(ctx.get("packaging_id") or "").strip()
                ) or {}
                pack_name = str(
                    pack_box.get("name") or pack_box.get("packaging_name") or ""
                ).strip()

                prod_label = ", ".join(prod_names)
                raw_by_region_s.setdefault(_rid_ctx, []).append(
                    (prod_label, pack_name, float(eff_pack), float(eff_goods))
                )

            if len(raw_by_region_s) > 1 and region_id is None:
                _rmap = self._region_name_map()
                result_s: list[tuple] = []
                for rid_key, rid_rows in sorted(
                    raw_by_region_s.items(), key=lambda x: _rmap.get(x[0], x[0])
                ):
                    rname = _rmap.get(rid_key) or rid_key or "Невідомий регіон"
                    result_s.append(("__REGION__", rname, 0.0, 0.0))
                    result_s.extend(rid_rows)
                return result_s

            result_s = []
            for rows in raw_by_region_s.values():
                result_s.extend(rows)
            return result_s
        except Exception:
            return []

    @staticmethod
    def _apply_pack_rows_to_col(col, rows: list) -> None:
        def _fmt(v: float) -> str:
            if v == int(v):
                return f"{int(v):,}".replace(",", " ")
            return f"{v:,.1f}".replace(",", " ")

        new_controls = []
        for row in rows:
            if len(row) >= 2 and row[0] == "__REGION__":
                new_controls.append(
                    ft.Container(
                        padding=ft.padding.only(top=6, bottom=2),
                        content=ft.Column(
                            [
                                ft.Text(str(row[1]), size=10, weight=ft.FontWeight.W_700,
                                        italic=True, color=_WARN_T),
                                ft.Divider(height=1, color=_WARN, thickness=1),
                            ],
                            spacing=2, tight=True,
                        ),
                    )
                )
                continue
            prod_label, pack_name, eff_pack, eff_goods = row[0], row[1], row[2], row[3]
            pack_s = _fmt(eff_pack)
            goods_s = _fmt(eff_goods)
            pack_color = _ERR if eff_pack < 0 else _WARN_T
            goods_color = _ERR if eff_goods < 0 else _ACCENT
            label_parts: list = [
                ft.Text(prod_label, size=9, color=_STONE2, no_wrap=False, max_lines=3,
                        overflow=ft.TextOverflow.VISIBLE),
            ]
            if pack_name:
                label_parts.append(
                    ft.Text(f"({pack_name})", size=9, weight="bold", color=_INK,
                            no_wrap=False, max_lines=2, overflow=ft.TextOverflow.VISIBLE)
                )
            new_controls.append(
                ft.Container(
                    bgcolor=_WARN_BG,
                    border_radius=10,
                    padding=ft.padding.symmetric(horizontal=8, vertical=7),
                    content=ft.Row(
                        [
                            ft.Column(label_parts, spacing=1, tight=True, expand=True),
                            ft.Column(
                                [
                                    ft.Text(pack_s, size=10, weight="bold",
                                            color=pack_color, text_align=ft.TextAlign.RIGHT),
                                    ft.Text(goods_s, size=10, weight="bold",
                                            color=goods_color, text_align=ft.TextAlign.RIGHT),
                                ],
                                spacing=2, tight=True,
                                horizontal_alignment=ft.CrossAxisAlignment.END,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        spacing=6,
                    ),
                )
            )
        col.controls[:] = new_controls
        with contextlib.suppress(Exception):
            col.update()

    def _build_packaging_stock_card(self) -> "ft.Control | None":
        """Build the packaging stock card widget. Returns None if no data."""
        try:
            rows = self._packaging_stock_rows()
        except Exception:
            rows = []
        if not rows:
            return None

        def _fmt(v: float) -> str:
            if v == int(v):
                return f"{int(v):,}".replace(",", " ")
            return f"{v:,.1f}".replace(",", " ")

        item_controls = []
        for row in rows:
            if len(row) >= 2 and row[0] == "__REGION__":
                item_controls.append(
                    ft.Container(
                        padding=ft.padding.only(top=6, bottom=2),
                        content=ft.Column(
                            [
                                ft.Text(
                                    str(row[1]),
                                    size=10,
                                    weight=ft.FontWeight.W_700,
                                    italic=True,
                                    color=_WARN_T,
                                ),
                                ft.Divider(height=1, color=_WARN, thickness=1),
                            ],
                            spacing=2,
                            tight=True,
                        ),
                    )
                )
                continue
            prod_label, pack_name, eff_pack, eff_goods = row[0], row[1], row[2], row[3]
            pack_s = _fmt(eff_pack)
            goods_s = _fmt(eff_goods)
            pack_color = _ERR if eff_pack < 0 else _WARN_T
            goods_color = _ERR if eff_goods < 0 else _ACCENT

            label_parts: list = [
                ft.Text(
                    prod_label,
                    size=9,
                    color=_STONE2,
                    no_wrap=False,
                    max_lines=3,
                    overflow=ft.TextOverflow.VISIBLE,
                ),
            ]
            if pack_name:
                label_parts.append(
                    ft.Text(
                        f"({pack_name})",
                        size=9,
                        weight="bold",
                        color=_INK,
                        no_wrap=False,
                        max_lines=2,
                        overflow=ft.TextOverflow.VISIBLE,
                    )
                )

            item_controls.append(
                ft.Container(
                    bgcolor=_WARN_BG,
                    border_radius=10,
                    padding=ft.padding.symmetric(horizontal=8, vertical=7),
                    content=ft.Row(
                        [
                            ft.Column(
                                label_parts,
                                spacing=1,
                                tight=True,
                                expand=True,
                            ),
                            ft.Column(
                                [
                                    ft.Text(
                                        pack_s,
                                        size=10,
                                        weight="bold",
                                        color=pack_color,
                                        text_align=ft.TextAlign.RIGHT,
                                    ),
                                    ft.Text(
                                        goods_s,
                                        size=10,
                                        weight="bold",
                                        color=goods_color,
                                        text_align=ft.TextAlign.RIGHT,
                                    ),
                                ],
                                spacing=2,
                                tight=True,
                                horizontal_alignment=ft.CrossAxisAlignment.END,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        spacing=6,
                    ),
                )
            )

        legend = ft.Column(
            [
                ft.Text(self.t("home.pack_supplier", "пак. пост./"), size=10.5, color=_WARN_T, italic=True),
                ft.Text(self.t("home.prod_supplier", "тов. пост."), size=10.5, color=_ACCENT, italic=True),
            ],
            spacing=1,
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.END,
        )

        _items_col = ft.Column(
            item_controls,
            spacing=8,
            scroll=ft.ScrollMode.ADAPTIVE,
        )
        self._pack_stock_items_col = _items_col

        return ft.Container(
            width=240,
            height=380,
            bgcolor=_FDE68A,
            border=ft.border.all(1, _WARN),
            border_radius=12,
            padding=10,
            content=ft.Column(
                [
                    ft.Container(
                        bgcolor=_FED7AA,
                        border_radius=10,
                        padding=ft.padding.symmetric(horizontal=10, vertical=8),
                        content=ft.Row(
                            [
                                ft.Text(
                                    "Залишок пакування, шт.",
                                    size=12,
                                    weight="bold",
                                    color=_ERR_DK,
                                    expand=True,
                                ),
                                legend,
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ),
                    ft.Container(
                        expand=True,
                        content=_items_col,
                    ),
                ],
                spacing=10,
            ),
        )

    def _count_media_plan_stale(self):
        """Return stale media-plan counts as (yellow_14_29, red_30_plus_or_empty)."""
        if not self.company_id or not self._feat_on("media_plan"):
            return 0, 0
        try:
            from views.media_plan_view import MediaPlanView

            manager_ids = (
                self.general_manager_ids
                if (self.general_mode and self.general_manager_ids)
                else [self.manager_id]
            )
            total_yellow, total_red = 0, 0
            seen_row_ids: set[str] = set()

            for mid in manager_ids:
                try:
                    probe_user = (
                        self.user_data.copy()
                        if isinstance(self.user_data, dict)
                        else {}
                    )
                    probe_user["active_manager_id"] = mid
                    probe_user["general_mode"] = False
                    mv = MediaPlanView(
                        content_area=None,
                        user_data=probe_user,
                        db=self.db,
                        active_region=self._resolve_region_id(),
                    )
                    mv._load()
                    rows = mv._build_rows()
                    for row in rows or []:
                        rid = str(row.get("id") or id(row))
                        if rid in seen_row_ids:
                            continue
                        seen_row_ids.add(rid)
                        if bool(row.get("not_relevant")):
                            continue
                        bucket = mv._stale_bucket(
                            row.get("days_since_publication"),
                            row.get("post_dates") or [],
                        )
                        if bucket == "yellow":
                            total_yellow += 1
                        elif bucket == "red":
                            total_red += 1
                except Exception:
                    pass
            return total_yellow, total_red
        except Exception:
            return 0, 0

    def _count_media_plan_schedule_end(self):
        """Returns (days_until_end: int, has_plan: bool).
        days_until_end = days until the latest future post_date across all entries.
        If no future post exists, returns 0.
        """
        if not self.company_id or not self._feat_on("media_plan"):
            return 0, False
        try:
            import json as _json

            region_id = self._resolve_region_id()
            mgr_ids = (
                self.general_manager_ids
                if (self.general_mode and self.general_manager_ids)
                else [self.manager_id]
            )
            today = date.today()
            future_dates: set = set()
            for mid in mgr_ids:
                try:
                    entries = (
                        self.db.get_media_plan_entries(
                            self.company_id,
                            manager_id=mid,
                            region_id=region_id,
                        )
                        or []
                    )
                    for en in entries:
                        raw = en.get("post_dates") or []
                        if isinstance(raw, str):
                            try:
                                raw = _json.loads(raw)
                            except Exception:
                                raw = []
                        if not isinstance(raw, list):
                            raw = []
                        for d_str in raw:
                            try:
                                d = date.fromisoformat(str(d_str)[:10])
                                if d >= today:
                                    future_dates.add(d)
                            except Exception:
                                pass
                except Exception:
                    pass

            if not future_dates:
                return 0, False

            max_future = max(future_dates)
            return (max_future - today).days, True
        except Exception:
            return 0, False

    def _count_categories_missing_target_margin(self):
        if not self.company_id:
            return 0
        try:
            categories = self._categories_in_scope()

            total = 0
            for cat in categories:
                if not isinstance(cat, dict):
                    continue
                raw_margin = cat.get("target_margin_percent")
                if raw_margin is None or str(raw_margin).strip() == "":
                    margin = self.db.get_category_target_margin(cat)
                else:
                    try:
                        margin = float(str(raw_margin).replace(",", "."))
                    except Exception:
                        margin = None
                if margin is None:
                    total += 1
            return total
        except Exception:
            return 0

    def _count_incomplete_products(self):
        """Count products missing purchase_price, weight_kg, or selling_prices."""

        def is_value_present(value):
            """Check if value is not None, not empty, and meaningful."""
            if value is None:
                return False
            if isinstance(value, str):
                return bool(value.strip())
            if isinstance(value, (dict, list)):
                return len(value) > 0
            if isinstance(value, (int, float)):
                return value != 0
            return bool(value)

        if not self.company_id:
            return 0
        try:
            products = self._products_in_scope()
            total = 0
            for prod in products:
                if not isinstance(prod, dict):
                    continue
                # Check if all required fields are filled
                has_purchase_price = is_value_present(prod.get("purchase_price"))
                has_weight = is_value_present(prod.get("weight_kg"))
                has_production_term = is_value_present(prod.get("production_term_days"))
                has_selling_prices = is_value_present(prod.get("sales_prices"))

                # Product is incomplete if ANY required field is missing
                if not (
                    has_purchase_price
                    and has_weight
                    and has_production_term
                    and has_selling_prices
                ):
                    total += 1
            return total
        except Exception:
            return 0

    def _count_orders_missing_fact_weight(self):
        """Count non-production orders with no fact weight (weight_actual_kg not set on any item)."""
        if not self.company_id:
            return 0
        orders = self._all_orders_unfiltered()
        total = 0
        _non_prod = {"in_transit", "at_warehouse", "accepted", "ready_for_shipment"}
        for order in orders or []:
            if not isinstance(order, dict):
                continue
            status = self._normalize_order_status(order.get("status"))
            if status not in _non_prod:
                continue
            items = order.get("items") if isinstance(order.get("items"), list) else []
            if not items:
                continue
            weights = [(it or {}).get("weight_actual_kg") for it in items]
            has_any_fact_weight = any(
                self._safe_float(w, None) is not None
                and float(self._safe_float(w, 0.0) or 0) > 0
                for w in weights
            )
            if not has_any_fact_weight:
                total += 1
        return total

    def _general_count_unique(self, db_method_name: str, **kwargs) -> int:
        """In general mode: call db.<method>(company_id, region_id, manager_id=mid, **kwargs)
        for each selected manager, deduplicate by 'id', return total unique count."""
        seen: set[str] = set()
        region_id = self._resolve_region_id()
        method = getattr(self.db, db_method_name)
        for mid in self.general_manager_ids or []:
            try:
                rows = (
                    method(self.company_id, region_id, manager_id=mid, **kwargs) or []
                )
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    rid = str(row.get("id") or "").strip()
                    if rid:
                        seen.add(rid)
            except Exception:
                pass
        return len(seen)

    def _count_total_suppliers(self):
        if not self.company_id:
            return 0
        try:
            # _suppliers_in_scope() already computes the same per-manager union
            # in general_mode (and is _prefetch_get-cached) — reuse it instead
            # of re-querying get_suppliers() once more per manager here.
            suppliers = self._suppliers_in_scope()
            return sum(1 for row in suppliers if isinstance(row, dict))
        except Exception:
            return 0

    def _count_total_categories(self):
        if not self.company_id:
            return 0
        try:
            if self.general_mode and self.general_manager_ids:
                return self._general_count_unique("get_company_categories")
            categories = self._categories_in_scope() or []
            return sum(1 for row in categories if isinstance(row, dict))
        except Exception:
            return 0

    def _count_total_products(self):
        if not self.company_id:
            return 0
        try:
            # _products_in_scope() already computes the same per-manager union
            # in general_mode (and is _prefetch_get-cached) — reuse it instead
            # of re-querying get_products() once more per manager here.
            products = self._products_in_scope()
            return sum(1 for row in products if isinstance(row, dict))
        except Exception:
            return 0

    def _count_total_packaging(self):
        if not self.company_id or not self._feat_on("packaging"):
            return 0
        try:
            if self.general_mode and self.general_manager_ids:
                return self._general_count_unique("get_packaging")
            region_id = self._resolve_region_id()
            # When viewing another manager, always filter by their manager_id
            viewer_manager_id = self.manager_id
            packaging_rows = (
                self.db.get_packaging(
                    self.company_id,
                    region_id=region_id,
                    manager_id=viewer_manager_id,
                )
                or []
            )
            return sum(1 for row in packaging_rows if isinstance(row, dict))
        except Exception:
            return 0

    def _all_orders_unfiltered(self) -> list:
        """Return ALL orders for this manager/mode without product-ID filtering.
        Used for the 'Замовлень' KPI card and status counters (В дорозі, etc.).
        Result is cached per build via _prefetch_get so only one DB call per build.
        Uses shared_orders_map (via _get_live_orders) when available so newly added
        orders appear immediately even before the outbox is flushed to DB."""
        if not self.company_id:
            return []

        # Fast path: shared map has orders (set immediately on save, before DB flush).
        # Skip when _force_fresh_from_db is set (i.e. after resume() call) so that
        # payment/order changes made in other views are reflected immediately from DB.
        # Only safe for a SPECIFIC region: the shared map only contains orders for
        # regions that have actually been loaded into it this session, so in
        # "Всі регіони" mode (region_id empty) it can silently under-report until
        # every region has been visited once — always fall through to a real DB
        # query for the all-regions aggregate instead.
        live_fn = getattr(self, "_get_live_orders", None)
        region_id = self._resolve_region_id()
        if callable(live_fn) and region_id and not getattr(self, "_force_fresh_from_db", False):
            live = live_fn()
            if live:
                mgr = (self.manager_id or "").strip()
                if self.general_mode and self.general_manager_ids:
                    mgr_set = {str(m).strip() for m in self.general_manager_ids if m}
                    result = [
                        o for o in live
                        if isinstance(o, dict)
                        and (not region_id or str(o.get("region_id") or "").strip() == region_id)
                        and str(o.get("manager_id") or "").strip() in mgr_set
                    ]
                else:
                    result = [
                        o for o in live
                        if isinstance(o, dict)
                        and (not region_id or str(o.get("region_id") or "").strip() == region_id)
                        and (not mgr or str(o.get("manager_id") or "").strip() == mgr)
                    ]
                if result:
                    return result

        def _load():
            try:
                region_id = self._resolve_region_id()
                # Fallback to DB when shared map is empty (first tab visit before orders_view loaded).
                if self.general_mode and self.general_manager_ids:
                    mgr_ids = [str(m).strip() for m in self.general_manager_ids if m]
                    # get_orders() runs an identical unbounded company+region scan
                    # for every call regardless of manager_id (manager_id only
                    # narrows the result via an in-Python post-filter) — fetching
                    # it once and reusing the raw rows avoids N duplicate full
                    # table scans for N managers in this view.
                    try:
                        raw_rows = [
                            r
                            for r in (
                                self.db.get_orders(self.company_id, region_id=region_id)
                                or []
                            )
                            if isinstance(r, dict)
                        ]
                    except Exception:
                        raw_rows = []
                    seen: set = set()
                    result: list = []
                    for mid in mgr_ids:
                        try:
                            visible_ctx = self.db._get_manager_visible_scope_context(
                                self.company_id, mid, region_id or None
                            )
                            if visible_ctx.get("restricted"):
                                allowed_ids = visible_ctx.get("order_ids") or set()
                                mgr_rows = [
                                    r
                                    for r in raw_rows
                                    if str(r.get("id") or "").strip() in allowed_ids
                                ]
                            else:
                                mgr_rows = raw_rows
                            for r in mgr_rows:
                                oid = str(r.get("id") or "").strip()
                                if oid and oid not in seen:
                                    seen.add(oid)
                                    result.append(r)
                        except Exception:
                            pass
                    _home_logger.info(
                        "[home] _all_orders_unfiltered GENERAL managers=%s total=%d",
                        mgr_ids,
                        len(result),
                    )
                    return result
                mgr = (self.manager_id or "").strip()
                rows = (
                    self.db.get_orders(
                        self.company_id,
                        region_id=region_id,
                        manager_id=mgr if mgr else None,
                    )
                    or []
                )
                rows = [r for r in rows if isinstance(r, dict)]
                _home_logger.info(
                    "[home] _all_orders_unfiltered SINGLE mgr=%s region=%s total=%d",
                    mgr,
                    region_id,
                    len(rows),
                )
                return rows
            except Exception:
                _home_logger.exception("[home] _all_orders_unfiltered _load failed")
                return []

        return self._prefetch_get("all_orders_unfiltered", _load) or []

    def _count_all_orders_for_kpi(self) -> int:
        """Count ALL orders for this manager (all statuses, no product filter)."""
        return len(self._all_orders_unfiltered())

    def _orders_in_scope(self):
        if self._live_orders_snapshot is not None:
            region_id = self._resolve_region_id()
            snap_total = len(self._live_orders_snapshot)
            # Diagnostic: log manager_id distribution in snapshot
            if self.general_mode:
                _snap_mgr_dist: dict = {}
                for _o in self._live_orders_snapshot:
                    if isinstance(_o, dict):
                        _mid = str(_o.get("manager_id") or "?").strip()
                        _snap_mgr_dist[_mid] = _snap_mgr_dist.get(_mid, 0) + 1
                _home_logger.info(
                    "[DIAG][orders_in_scope] source=SNAPSHOT GENERAL total=%d mgr_dist=%s",
                    snap_total,
                    _snap_mgr_dist,
                )
            else:
                _home_logger.info(
                    "[DIAG][orders_in_scope] source=SNAPSHOT single_mgr=%s total=%d",
                    self.manager_id,
                    snap_total,
                )

            def _from_snapshot():
                snap = list(self._live_orders_snapshot)
                if not region_id:
                    _home_logger.info(
                        "[home_orders] source=snapshot region=ALL total=%d", snap_total
                    )
                    return snap
                # collect unique region_ids in snapshot for diagnostics
                snap_regions = set(
                    str(o.get("region_id") or "").strip()
                    for o in snap
                    if isinstance(o, dict)
                )
                filtered = [
                    o
                    for o in snap
                    if isinstance(o, dict)
                    and str(o.get("region_id") or "").strip() == region_id
                ]
                _home_logger.info(
                    "[home_orders] source=snapshot region_id=%s total_snap=%d filtered=%d snap_region_ids=%s",
                    region_id,
                    snap_total,
                    len(filtered),
                    snap_regions,
                )
                return filtered

            return self._prefetch_get("home_orders_scope", _from_snapshot) or []
        if not self.company_id:
            return []

        def _load():
            try:
                region_id = self._resolve_region_id()
                mgr = (self.manager_id or "").strip()
                try:
                    products = (
                        self.db.get_products(
                            self.company_id,
                            region_id=region_id,
                            manager_id=self.manager_id,
                        )
                        or []
                    )
                except Exception:
                    products = []
                visible_ids = {
                    str((p or {}).get("id") or "").strip()
                    for p in products
                    if isinstance(p, dict) and (p.get("id") or "")
                }
                # NO manager_id in DB call — avoids _get_manager_visible_scope_context
                # which can over-filter (e.g. restricted scope returns fewer orders than
                # what is actually the manager's data).
                # visible_product_ids pre-filters at SQL level so the result set is small.
                orders = self.db.get_orders(
                    self.company_id,
                    region_id=region_id,
                    visible_product_ids=list(visible_ids) if visible_ids else None,
                )
                rows = [row for row in (orders or []) if isinstance(row, dict)]
                # Strict filter: only this manager's orders.
                # The old "OR any items in visible_ids" caused over-counting (other
                # managers' orders that happen to use the same products were included).
                result = [
                    row
                    for row in rows
                    if mgr and (row.get("manager_id") or "").strip() == mgr
                ]
                _home_logger.info(
                    "[DIAG][orders_in_scope] source=DB mgr=%s region=%s products=%d db_raw=%d result=%d",
                    mgr,
                    region_id,
                    len(visible_ids),
                    len(rows),
                    len(result),
                )
                return result
            except Exception:
                _home_logger.exception("[home_orders] _load failed")
                return []

        return self._prefetch_get("home_orders_scope", _load) or []

    def _days_between_or_none(self, start_raw, end_raw):
        start_dt = self._parse_date(start_raw)
        end_dt = self._parse_date(end_raw)
        if start_dt is None or end_dt is None:
            return None
        try:
            return max(0, int((end_dt - start_dt).days))
        except Exception:
            return None

    def _normalize_order_status(self, raw_status) -> str:
        raw = str(raw_status or "production").strip().lower()
        # Tolerate punctuation/suffix noise (e.g. "Прийнятий№").
        cleaned = "".join(
            ch for ch in raw if ch.isalnum() or ch.isspace() or ch == "_"
        ).strip()
        aliases = {
            "production": "production",
            "виробництво": "production",
            "в виробництві": "production",
            "ready_for_shipment": "ready_for_shipment",
            "ready": "ready_for_shipment",
            "готово": "ready_for_shipment",
            "готовий": "ready_for_shipment",
            "in_transit": "in_transit",
            "transit": "in_transit",
            "в дорозі": "in_transit",
            "at_warehouse": "at_warehouse",
            "warehouse": "at_warehouse",
            "на складі": "at_warehouse",
            "accepted": "accepted",
            "done": "accepted",
            "прийнятий": "accepted",
            "прийняті": "accepted",
            "прийнято": "accepted",
        }
        if cleaned in aliases:
            return aliases.get(cleaned, cleaned)
        if "прийнят" in cleaned:
            return "accepted"
        if "на скла" in cleaned:
            return "at_warehouse"
        if "дороз" in cleaned:
            return "in_transit"
        if "готов" in cleaned:
            return "ready_for_shipment"
        if "вироб" in cleaned:
            return "production"
        return aliases.get(raw, cleaned or "production")

    def _delivery_type_labels(self, orders: list[dict]) -> list[str]:
        labels = []
        seen = set()

        def _add(raw):
            text = str(raw or "").strip()
            key = text.lower()
            if not text or key in seen:
                return
            seen.add(key)
            labels.append(text)

        methods = self._delivery_methods_in_scope()
        for method in methods:
            _add(method.get("name") or method.get("label") or method.get("id"))

        id_to_name = self._delivery_methods_name_by_id()

        for order in orders or []:
            _add(self._delivery_label_for_order(order, id_to_name))
        return labels

    def _format_avg_days(self, values: list[int | float]) -> str:
        if not values:
            return "-"
        try:
            avg = round(sum(float(v) for v in values) / max(1, len(values)))
            return f"{int(avg)} днів"
        except Exception:
            return "-"

    def _format_avg_rate(self, values: list[int | float]) -> str:
        if not values:
            return "-"
        try:
            avg = sum(float(v) for v in values) / max(1, len(values))
            return f"{avg:.2f}"
        except Exception:
            return "-"

    def _format_avg_cost(self, values: list[int | float]) -> str:
        if not values:
            return "-"
        try:
            avg = sum(float(v) for v in values) / max(1, len(values))
            return f"${avg:.2f}"
        except Exception:
            return "-"

    def _order_fact_weight_total_or_none(self, order: dict) -> float | None:
        items = order.get("items") if isinstance(order.get("items"), list) else []
        if not items:
            return None
        values = []
        for item in items:
            if not isinstance(item, dict):
                continue
            weight = self._safe_float(item.get("weight_actual_kg"), None)
            if weight is None:
                continue
            if float(weight) < 0:
                continue
            values.append(float(weight))
        if not values:
            return None
        total = float(sum(values))
        return total if total > 0 else None

    def _order_tariff_per_kg_or_none(self, order: dict) -> float | None:
        """Resolve per-kg tariff from the best available order source.

        Priority:
        1) cp_logistics_fact_total / fact_weight_total (actual fact-based tariff)
        2) delivery_price_snapshot (tariff saved in Orders table "Тариф" column)
        3) legacy delivery_price
        """
        logistics_fact = self._safe_float(order.get("cp_logistics_fact_total"), None)
        fact_weight_total = self._order_fact_weight_total_or_none(order)
        if (
            logistics_fact is not None
            and fact_weight_total is not None
            and float(fact_weight_total) > 0
        ):
            return float(logistics_fact) / float(fact_weight_total)

        snapshot_tariff = self._safe_float(order.get("delivery_price_snapshot"), None)
        if snapshot_tariff is not None and float(snapshot_tariff) >= 0:
            return float(snapshot_tariff)

        legacy_tariff = self._safe_float(order.get("delivery_price"), None)
        if legacy_tariff is not None and float(legacy_tariff) >= 0:
            return float(legacy_tariff)

        return None

    def _region_name_map(self) -> dict[str, str]:
        """Return {region_id: region_name} for the current company."""
        try:
            rows = self.db.get_company_regions(self.company_id) or []
            return {
                str(r.get("id") or "")
                .strip(): str(r.get("name") or r.get("id") or "")
                .strip()
                for r in rows
                if isinstance(r, dict) and r.get("id")
            }
        except Exception:
            return {}

    def _with_region_blocks(
        self,
        orders: list[dict],
        compute_fn,
        *args,
        **kwargs,
    ) -> list[tuple]:
        """When 'Всі регіони' is active, split orders by region and compute compute_fn per block.

        Each block is prefixed by ("__REGION__", region_name).
        If only one region (or a specific region is selected), call compute_fn normally.
        """
        is_all_regions = self.active_region is None or str(
            self.active_region or ""
        ).strip() in {"", "__all_regions__", "Всі регіони"}
        if not is_all_regions:
            return compute_fn(orders, *args, **kwargs)

        # Group orders by region_id
        region_groups: dict[str, list[dict]] = {}
        for o in orders or []:
            if not isinstance(o, dict):
                continue
            rid = str(o.get("region_id") or "").strip()
            if rid not in region_groups:
                region_groups[rid] = []
            region_groups[rid].append(o)

        if len(region_groups) <= 1:
            # Only one region in data — no need for blocks
            return compute_fn(orders, *args, **kwargs)

        name_map = self._region_name_map()
        result: list[tuple] = []
        for rid, region_orders in sorted(
            region_groups.items(), key=lambda kv: name_map.get(kv[0], kv[0])
        ):
            region_name = name_map.get(rid) or rid or "Невідомий регіон"
            self._active_split_region_id = rid
            try:
                region_rows = compute_fn(region_orders, *args, **kwargs)
            finally:
                self._active_split_region_id = None
            if not region_rows:
                continue
            result.append(("__REGION__", region_name))
            result.extend(region_rows)
        return result or compute_fn(orders, *args, **kwargs)

    def _delivery_average_rows(
        self,
        orders: list[dict],
        value_getter,
        status_filter=None,
        overall_label: str | None = None,
    ) -> list[tuple[str, str]]:
        """Generic method for calculating average values by delivery type.

        Args:
            orders: List of orders to process
            value_getter: Function to extract value from an order
            status_filter: Optional status to filter by. If None, uses all statuses.
        """
        labels = self._delivery_type_labels(orders)
        buckets: dict[str, list[int | float]] = {label: [] for label in labels}
        all_values: list[int | float] = []
        id_to_name = self._delivery_methods_name_by_id()

        for order in orders or []:
            order_status = str(order.get("status") or "production").strip().lower()
            if status_filter is not None and order_status != status_filter:
                continue
            label = self._delivery_label_for_order(order, id_to_name)
            if label not in buckets:
                buckets[label] = []
                labels.append(label)
            value = value_getter(order)
            if value is None:
                continue
            buckets[label].append(value)
            all_values.append(value)

        rows = [
            (label, self._format_avg_days(buckets.get(label, []))) for label in labels
        ]
        if overall_label is not None:
            rows.insert(0, (str(overall_label), self._format_avg_days(all_values)))
        return rows

    def _accepted_delivery_average_rows(
        self, orders: list[dict]
    ) -> list[tuple[str, str]]:
        """Calculate average delivery time (ready -> warehouse) per delivery type.

        Includes only orders where:
        - status in ["at_warehouse", "accepted"]
        - date_arrived_warehouse is present
        Delivery time equals "Терм. доставки" from Orders table.
        """
        filtered = [
            o
            for o in (orders or [])
            if isinstance(o, dict)
            and self._normalize_order_status(o.get("status"))
            in {"at_warehouse", "accepted"}
            and self._parse_date(o.get("date_arrived_warehouse")) is not None
        ]
        return self._delivery_average_rows(
            filtered,
            lambda order: (
                self._safe_float(order.get("duration_transit_days"), None)
                if self._safe_float(order.get("duration_transit_days"), None)
                is not None
                else self._days_between_or_none(
                    order.get("date_shipped"), order.get("date_arrived_warehouse")
                )
            ),
            status_filter=None,
            overall_label=self.t("home.overall_label", "Загалом:"),
        )

    def _accepted_shoulder_average_rows(
        self, orders: list[dict]
    ) -> list[tuple[str, str]]:
        """Calculate average shoulder ("Плече") per delivery type.

        Includes only accepted orders with date_accepted present.
        """
        filtered = [
            o
            for o in (orders or [])
            if isinstance(o, dict)
            and self._normalize_order_status(o.get("status")) == "accepted"
            and self._parse_date(o.get("date_accepted")) is not None
        ]
        return self._delivery_average_rows(
            filtered,
            lambda order: (
                self._safe_float(order.get("log_days"), None)
                if self._safe_float(order.get("log_days"), None) is not None
                else self._days_between_or_none(
                    order.get("order_date"), order.get("date_accepted")
                )
            ),
            status_filter=None,
            overall_label=self.t("home.overall_label", "Загалом:"),
        )

    def _average_delivery_tariff_rows(
        self, orders: list[dict]
    ) -> list[tuple[str, str]]:
        """Average tariff by delivery type.

        Includes all orders where status is NOT ["production", "ready_for_shipment"].
        """
        filtered = [
            o
            for o in (orders or [])
            if isinstance(o, dict)
            and self._normalize_order_status(o.get("status"))
            not in {"production", "ready_for_shipment"}
        ]
        labels = self._delivery_type_labels(filtered)
        buckets: dict[str, list[float]] = {label: [] for label in labels}
        all_values: list[float] = []
        id_to_name = self._delivery_methods_name_by_id()

        for order in filtered:
            if not isinstance(order, dict):
                continue
            label = self._delivery_label_for_order(order, id_to_name)
            if label not in buckets:
                buckets[label] = []
                labels.append(label)

            tariff = self._order_tariff_per_kg_or_none(order)
            if tariff is None:
                continue
            buckets[label].append(tariff)
            all_values.append(tariff)

        rows = [
            (label, self._format_avg_rate(buckets.get(label, []))) for label in labels
        ]
        rows.insert(
            0,
            (
                "Загальний середній тариф доставки:",
                self._format_avg_rate(all_values),
            ),
        )
        return rows

    def _term_summary_rows(self, orders: list[dict]) -> list[tuple[str, str]]:
        """Calculate production and acceptance times from all orders.

        - Production time: average of "Терм. виробництва" for statuses not in [production, ready_for_shipment], with date_shipped present
        - Acceptance time: average of "Терм. прий-ки" for accepted status with date_accepted present
        - Deviation: average of saved "Відхилення" for accepted status with date_accepted present
        - Status counters: normalized by canonical status keys with date-based fallback
        """
        production_values = []
        acceptance_values = []
        deviation_values = []
        status_counts = {
            "production": 0,
            "ready_for_shipment": 0,
            "in_transit": 0,
            "at_warehouse": 0,
            "accepted": 0,
        }

        products_by_id = {
            str((p or {}).get("id") or "").strip(): p
            for p in (self._products_in_scope() or [])
            if isinstance(p, dict) and str((p or {}).get("id") or "").strip()
        }
        delivery_by_id = {
            str((d or {}).get("id") or "").strip(): d
            for d in (self._delivery_methods_in_scope() or [])
            if isinstance(d, dict) and str((d or {}).get("id") or "").strip()
        }

        def _safe_iso_date(raw):
            txt = str(raw or "").strip()[:10]
            if not txt:
                return None
            try:
                return date.fromisoformat(txt)
            except Exception:
                return None

        def _add_days_to_iso(iso10: str, days_val: float) -> str:
            base = _safe_iso_date(iso10)
            if base is None:
                return ""
            try:
                return (base + timedelta(days=int(round(float(days_val))))).isoformat()
            except Exception:
                return ""

        def _order_deviation_days(order: dict) -> int | None:
            if not isinstance(order, dict):
                return None

            order_date = str(order.get("order_date") or "")[:10]
            accepted_iso = str(order.get("date_accepted") or "")[:10]
            if not order_date or not accepted_iso:
                return None

            date_shipped = str(order.get("date_shipped") or "")[:10]
            date_wh = str(order.get("date_arrived_warehouse") or "")[:10]

            dur_prod = self._safe_float(order.get("duration_production_days"), None)
            if dur_prod is None:
                dur_prod = self._days_between_or_none(order_date, date_shipped)
            dur_deliv = self._safe_float(order.get("duration_transit_days"), None)
            if dur_deliv is None:
                dur_deliv = self._days_between_or_none(date_shipped, date_wh)

            prod_term_days = None
            items = order.get("items") if isinstance(order.get("items"), list) else []
            for it in items:
                if not isinstance(it, dict):
                    continue
                pid = str(it.get("product_id") or "").strip()
                prod = products_by_id.get(pid) if pid else None
                p_days = self._safe_float(
                    (prod or {}).get("production_term_days"), None
                )
                if p_days is None:
                    continue
                p_days = max(0.0, float(p_days))
                prod_term_days = (
                    p_days
                    if prod_term_days is None
                    else max(float(prod_term_days), p_days)
                )
            if prod_term_days is None:
                prod_term_days = self._safe_float(dur_prod, None)

            deliv_term_days = None
            did = str(order.get("delivery_type_id") or "").strip()
            drow = delivery_by_id.get(did)
            if isinstance(drow, dict):
                deliv_term_days = self._safe_float(
                    drow.get("effective_delivery_term_days"),
                    None,
                )
                if deliv_term_days is None:
                    deliv_term_days = self._safe_float(
                        drow.get("base_delivery_term_days"),
                        None,
                    )
                if deliv_term_days is None:
                    deliv_term_days = self._safe_float(
                        drow.get("delivery_term_days"),
                        None,
                    )
            if deliv_term_days is None:
                deliv_term_days = self._safe_float(dur_deliv, None)

            if prod_term_days is None and deliv_term_days is None:
                return None
            receive_iso = _add_days_to_iso(
                order_date,
                float(prod_term_days or 0.0) + float(deliv_term_days or 0.0),
            )
            receive_dt = _safe_iso_date(receive_iso)
            accepted_dt = _safe_iso_date(accepted_iso)
            if receive_dt is None or accepted_dt is None:
                return None
            return int((accepted_dt - receive_dt).days)

        for order in orders or []:
            if not isinstance(order, dict):
                continue

            status = self._normalize_order_status(order.get("status"))
            if status not in status_counts:
                status = "production"
            status_counts[status] += 1

            has_date_shipped = self._parse_date(order.get("date_shipped")) is not None
            has_date_accepted = self._parse_date(order.get("date_accepted")) is not None

            if status not in {"production", "ready_for_shipment"} and has_date_shipped:
                prod_days = self._safe_float(
                    order.get("duration_production_days"), None
                )
                if prod_days is None:
                    prod_days = self._days_between_or_none(
                        order.get("order_date"), order.get("date_shipped")
                    )
                if prod_days is not None:
                    production_values.append(prod_days)

            if status == "accepted" and has_date_accepted:
                acc_days = self._safe_float(order.get("duration_warehouse_days"), None)
                if acc_days is None:
                    acc_days = self._days_between_or_none(
                        order.get("date_arrived_warehouse"), order.get("date_accepted")
                    )
                if acc_days is not None:
                    acceptance_values.append(acc_days)

                dev_days = self._safe_float(order.get("deviation_days"), None)
                if dev_days is None:
                    dev_days = _order_deviation_days(order)
                if dev_days is not None:
                    deviation_values.append(dev_days)

        dev_text = self._format_avg_days(deviation_values)
        try:
            dev_avg = (
                sum(float(v) for v in deviation_values) / max(1, len(deviation_values))
                if deviation_values
                else None
            )
        except Exception:
            dev_avg = None
        if dev_avg is None or dev_text == "-":
            dev_color = None
        elif dev_avg >= 1:
            dev_color = _ERR  # red
        else:
            dev_color = _OK  # green

        return [
            ("Середній час виробництва", self._format_avg_days(production_values)),
            ("Середній час прийомки", self._format_avg_days(acceptance_values)),
            ("Середнє відхилення", dev_text, None, dev_color),
            (
                'Кількість замовлень "Виробництво"',
                str(status_counts["production"]),
                self.on_open_orders_status_production,
            ),
            (
                'Кількість замовлень "Готовий"',
                str(status_counts["ready_for_shipment"]),
                self.on_open_orders_status_ready_for_shipment,
            ),
            (
                'Кількість замовлень "В Дорозі"',
                str(status_counts["in_transit"]),
                self.on_open_orders_status_in_transit,
            ),
            (
                'Кількість замовлень "На складі"',
                str(status_counts["at_warehouse"]),
                self.on_open_orders_status_at_warehouse,
            ),
            (
                'Кількість замовлень "Прийняті"',
                str(status_counts["accepted"]),
                self.on_open_orders_status_accepted,
            ),
        ]

    def _production_term_by_product_rows(
        self, orders: list[dict]
    ) -> list[tuple[str, str]]:
        """Calculate average production time by product.

        Includes only orders where status != "production".
        Groups by product name from order items.
        """
        buckets: dict[str, list[int]] = {}

        for order in orders or []:
            if not isinstance(order, dict):
                continue
            status = str(order.get("status") or "production").strip().lower()
            if status == "production":
                continue

            # Get production days
            prod_days = order.get("duration_production_days")
            if prod_days is None:
                prod_days = self._days_between_or_none(
                    order.get("order_date"), order.get("date_shipped")
                )
            if prod_days is None:
                prod_days = self._days_between_or_none(
                    order.get("order_date"), order.get("date_accepted")
                )
            if prod_days is None:
                continue

            # Iterate items to get product names
            items = order.get("items") or []
            if not items:
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if not str(item.get("product_id") or "").strip():
                    continue  # skip items with no real product (custom-name-only orders)
                prod_name = str(item.get("product_name") or "—").strip()
                if prod_name not in buckets:
                    buckets[prod_name] = []
                buckets[prod_name].append(prod_days)

        rows = [
            (name, self._format_avg_days(values))
            for name, values in sorted(buckets.items())
        ]
        return rows

    def _production_term_by_level_rows(
        self, orders: list[dict]
    ) -> list[tuple[str, str]]:
        """Calculate average production time by level node.

        Includes only orders where status != "production".
        Groups by level node name from order items.
        """
        buckets: dict[str, list[int]] = {}

        for order in orders or []:
            if not isinstance(order, dict):
                continue
            status = str(order.get("status") or "production").strip().lower()
            if status == "production":
                continue

            # Get production days
            prod_days = order.get("duration_production_days")
            if prod_days is None:
                prod_days = self._days_between_or_none(
                    order.get("order_date"), order.get("date_shipped")
                )
            if prod_days is None:
                prod_days = self._days_between_or_none(
                    order.get("order_date"), order.get("date_accepted")
                )
            if prod_days is None:
                continue

            # Iterate items to get level node names
            items = order.get("items") or []
            if not items:
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if not str(item.get("product_id") or "").strip():
                    continue  # skip items with no real product
                level_name = str(item.get("level_node_name") or "—").strip()
                if level_name not in buckets:
                    buckets[level_name] = []
                buckets[level_name].append(prod_days)

        rows = [
            (name, self._format_avg_days(values))
            for name, values in sorted(buckets.items())
        ]
        return rows

    def _average_cost_by_product_rows(
        self, orders: list[dict]
    ) -> list[tuple[str, str]]:
        """Calculate average unit cost by product.

        Groups by product name from order items.
        Uses unit_cost_calc_usd from each item.
        """
        buckets: dict[str, list[float]] = {}

        for order in orders or []:
            if not isinstance(order, dict):
                continue

            items = order.get("items") or []
            if not items:
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue

                if not str(item.get("product_id") or "").strip():
                    continue  # skip items with no real product
                cost = self._safe_float(item.get("unit_cost_calc_usd"), None)
                if cost is None:
                    continue
                if float(cost) < 0:
                    continue
                # Items can each be in a different native pivot currency now —
                # normalize to USD before bucketing/averaging across products.
                cost_ccy = (
                    str(item.get("unit_cost_calc_currency") or "USD").upper().strip()
                    or "USD"
                )
                cost_usd = fx_rates.convert(float(cost), cost_ccy, "USD")
                if cost_usd is None:
                    continue
                cost = cost_usd

                prod_name = str(item.get("product_name") or "—").strip()
                if prod_name not in buckets:
                    buckets[prod_name] = []
                buckets[prod_name].append(float(cost))

        rows = [
            (name, self._format_avg_cost(values))
            for name, values in sorted(buckets.items())
        ]
        return rows

    def _average_cost_by_level_rows(self, orders: list[dict]) -> list[tuple[str, str]]:
        """Calculate average unit cost by level node.

        Groups by level node name from order items.
        Uses unit_cost_calc_usd from each item.
        """
        buckets: dict[str, list[float]] = {}

        for order in orders or []:
            if not isinstance(order, dict):
                continue

            items = order.get("items") or []
            if not items:
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue

                if not str(item.get("product_id") or "").strip():
                    continue  # skip items with no real product
                cost = self._safe_float(item.get("unit_cost_calc_usd"), None)
                if cost is None:
                    continue
                if float(cost) < 0:
                    continue
                # Items can each be in a different native pivot currency now —
                # normalize to USD before bucketing/averaging across products.
                cost_ccy = (
                    str(item.get("unit_cost_calc_currency") or "USD").upper().strip()
                    or "USD"
                )
                cost_usd = fx_rates.convert(float(cost), cost_ccy, "USD")
                if cost_usd is None:
                    continue
                cost = cost_usd

                level_name = str(item.get("level_node_name") or "—").strip()
                if level_name not in buckets:
                    buckets[level_name] = []
                buckets[level_name].append(float(cost))

        rows = [
            (name, self._format_avg_cost(values))
            for name, values in sorted(buckets.items())
        ]
        return rows

    def _average_cost_summary_rows(self, orders: list[dict]) -> list[tuple[str, str]]:
        """Calculate average cost summary: products + levels grouped together.

        Similar to _term_summary_rows, combines both product and level groupings.
        """
        product_cost_rows = self._average_cost_by_product_rows(orders)
        level_cost_rows = self._average_cost_by_level_rows(orders)

        # Combine: label them appropriately and interleave or separate
        result = []
        if product_cost_rows:
            result.extend(product_cost_rows)
        if level_cost_rows:
            result.extend(level_cost_rows)

        return result

    def _count_incomplete_suppliers(self):
        """Count suppliers missing name, phone, region, currency, or at least one payment field."""

        def is_value_present(value):
            """Check if value is not None, not empty, and meaningful."""
            if value is None:
                return False
            if isinstance(value, str):
                return bool(value.strip())
            if isinstance(value, (dict, list)):
                return len(value) > 0
            if isinstance(value, (int, float)):
                return value != 0
            return bool(value)

        def has_payment_method_selected_and_filled(supp):
            if not isinstance(supp, dict):
                return False

            payment_fields = supp.get("payment_fields")
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
            if is_value_present(supp.get("swift")):
                return True
            if is_value_present(supp.get("alipay_id")):
                return True
            if is_value_present(supp.get("wechat_id")):
                return True
            if is_value_present(supp.get("alipay_qr")):
                return True
            if is_value_present(supp.get("wechat_qr")):
                return True

            payment_data = (
                supp.get("data", {}) if isinstance(supp.get("data"), dict) else {}
            )
            if is_value_present(payment_data.get("swift")):
                return True
            if is_value_present(payment_data.get("alipay_id")):
                return True
            if is_value_present(payment_data.get("wechat_id")):
                return True
            if is_value_present(payment_data.get("alipay_qr")):
                return True
            if is_value_present(payment_data.get("wechat_qr")):
                return True

            return False

        if not self.company_id:
            return 0
        try:
            suppliers = self._suppliers_in_scope() or []
            total = 0
            for supp in suppliers:
                if not isinstance(supp, dict):
                    continue
                # Check if all required fields are filled
                has_name = is_value_present(supp.get("name"))
                has_phone = is_value_present(supp.get("phone"))
                has_region = is_value_present(supp.get("region_id"))
                has_currency = is_value_present(supp.get("currency"))
                has_payment_field = has_payment_method_selected_and_filled(supp)

                # Supplier is incomplete if ANY required field is missing
                if not (
                    has_name
                    and has_phone
                    and has_region
                    and has_currency
                    and has_payment_field
                ):
                    total += 1
            return total
        except Exception:
            return 0

    def _kpi_card_schedule_end(self, days_until_end, has_plan, on_click):
        """KPI card for media plan schedule end date."""
        days_val = max(0, int(days_until_end or 0))
        num_color = _ERR if days_val <= 4 else _OK
        body = ft.Column(
            [
                ft.Text(
                    "Медіаплан",
                    size=self.KPI_LABEL_SIZE,
                    weight="bold",
                    text_align=ft.TextAlign.CENTER,
                    no_wrap=False,
                    max_lines=2,
                ),
                ft.Text(
                    str(days_val),
                    size=self.KPI_COUNT_SIZE,
                    weight="bold",
                    color=num_color,
                    text_align=ft.TextAlign.CENTER,
                ),
                ft.Text(
                    "дн. до кінця",
                    size=self.KPI_META_SIZE,
                    color="grey700",
                    text_align=ft.TextAlign.CENTER,
                    no_wrap=False,
                ),
            ],
            spacing=4,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
        )

        return ft.Container(
            width=self.KPI_CARD_SIZE,
            height=self.KPI_CARD_SIZE,
            border=ft.border.all(1, _BORDER),
            border_radius=10,
            bgcolor=_PANEL,
            padding=8,
            ink=True,
            on_click=on_click,
            content=body,
        )

    def _table_layout_state_key(self) -> str:
        region_id = str(self._resolve_region_id() or "all").strip() or "all"
        company_id = str(self.company_id or "no_company").strip() or "no_company"
        return f"home:{company_id}:{region_id}"

    def _home_term_display_state_key(self) -> str:
        return f"{self._table_layout_state_key()}:term_production_display"

    def _load_home_term_display_title_keys(self) -> list[str]:
        try:
            manager_id = str(self.manager_id or "").strip()
            if not manager_id:
                return []
            payload = self.db.get_manager_workspace_state(manager_id) or {}
            if not isinstance(payload, dict):
                return []
            store = (
                payload.get("home_term_production_display")
                if isinstance(payload.get("home_term_production_display"), dict)
                else {}
            )
            raw = store.get(self._home_term_display_state_key())
            if not isinstance(raw, list):
                return []
            out = [str(x).strip().lower() for x in raw if str(x).strip()]
            return out
        except Exception:
            return []

    def _save_home_term_display_title_keys(self, keys: list[str]) -> None:
        try:
            manager_id = str(self.manager_id or "").strip()
            if not manager_id:
                return
            payload = self.db.get_manager_workspace_state(manager_id) or {}
            if not isinstance(payload, dict):
                payload = {}
            store = (
                payload.get("home_term_production_display")
                if isinstance(payload.get("home_term_production_display"), dict)
                else {}
            )
            store[self._home_term_display_state_key()] = [
                str(x).strip().lower() for x in (keys or []) if str(x).strip()
            ]
            payload["home_term_production_display"] = store
            self.db.save_manager_workspace_state(manager_id, payload)
        except Exception:
            pass

    def _home_term_display_title_options(self) -> list[tuple[str, str]]:
        try:
            # Use _categories_in_scope() — already handles general mode (combined from all managers)
            categories = self._categories_in_scope() or []
        except Exception:
            categories = []

        options: list[tuple[str, str]] = [("name", "Назва")]
        seen = {"name", "category"}
        titled: list[tuple[int, str, str]] = []
        for cat in categories:
            if not isinstance(cat, dict):
                continue
            cfg = self.db.get_category_levels_config(cat) or {}
            levels = cfg.get("levels") if isinstance(cfg.get("levels"), list) else []
            for row in levels:
                if not isinstance(row, dict) or not bool(row.get("enabled")):
                    continue
                label = str(row.get("title") or "").strip()
                if not label:
                    continue
                key = label.lower()
                if key in seen:
                    continue
                seen.add(key)
                try:
                    pos = int(row.get("position") or 0)
                except Exception:
                    pos = 0
                titled.append((pos, label.lower(), key))
                options.append((key, label))

        # Keep a stable, readable order: name first, category second, then by position/title.
        tail = sorted(
            options[1:],
            key=lambda pair: (
                next((p for p, _, k in titled if k == pair[0]), 999),
                pair[1].lower(),
            ),
        )
        return [options[0], ("category", "Категорія")] + tail

    def _selected_home_term_display_title_keys(self) -> list[str]:
        options = self._home_term_display_title_options()
        available_keys = [
            str(k or "").strip().lower() for k, _ in options if str(k or "").strip()
        ]
        saved = [
            k for k in self._load_home_term_display_title_keys() if k in available_keys
        ]
        if saved:
            return saved
        return ["name"] if "name" in available_keys else []

    def _days_between_or_none(self, start_raw, end_raw):
        start_dt = self._parse_date(start_raw)
        end_dt = self._parse_date(end_raw)
        if start_dt is None or end_dt is None:
            return None
        try:
            return max(0, int((end_dt - start_dt).days))
        except Exception:
            return None

    def _home_term_production_rows(
        self, orders: list[dict], selected_title_keys: list[str]
    ) -> list[tuple[str, str]]:
        keys = [
            str(k or "").strip().lower()
            for k in (selected_title_keys or [])
            if str(k or "").strip()
        ]
        if not keys:
            return [("Немає даних", "-")]

        selected_key_set = set(keys)

        # Use combined products/categories in general mode (via _products_in_scope / _categories_in_scope)
        try:
            all_products = self._products_in_scope() or []
        except Exception:
            all_products = []

        products_by_id = {}
        for prod in all_products:
            if not isinstance(prod, dict):
                continue
            pid = str(prod.get("id") or "").strip()
            if pid:
                products_by_id[pid] = prod

        try:
            categories = self._categories_in_scope() or []
        except Exception:
            categories = []

        cat_cfg_by_id: dict[str, dict] = {}
        for cat in categories or []:
            if not isinstance(cat, dict):
                continue
            cid = str(cat.get("id") or "").strip()
            if not cid:
                continue
            cat_cfg_by_id[cid] = self.db.get_category_levels_config(cat) or {}

        categories_by_id: dict[str, str] = {
            str(c.get("id") or "").strip(): str(c.get("name") or "").strip()
            for c in (categories or [])
            if isinstance(c, dict) and c.get("id")
        }

        try:
            all_nodes = self.db.get_company_catalog_nodes(self.company_id) or []
        except Exception:
            all_nodes = []
        nodes_by_id = {
            str(node.get("id") or "").strip(): node
            for node in all_nodes
            if isinstance(node, dict) and str(node.get("id") or "").strip()
        }

        def _node_names_by_position(
            node_id: str, item: dict | None = None
        ) -> dict[int, str]:
            out: dict[int, str] = {}
            cur = str(node_id or "").strip()
            hops = 0
            while cur and hops < 40:
                row = nodes_by_id.get(cur)
                if not isinstance(row, dict):
                    break
                try:
                    pos = int(row.get("level_position") or 0)
                except Exception:
                    pos = 0
                if 1 <= pos <= 6 and pos not in out:
                    out[pos] = str(row.get("name") or "").strip()
                cur = str(row.get("parent_node_id") or "").strip()
                hops += 1
            for pos in range(1, 7):
                direct_id = ""
                if isinstance(item, dict):
                    direct_id = str(item.get(f"level_{pos}_node_id") or "").strip()
                if not direct_id:
                    direct_id = str(
                        (
                            products_by_id.get(
                                str(item.get("product_id") or "").strip()
                            )
                            or {}
                        ).get(f"level_{pos}_node_id")
                        if isinstance(item, dict)
                        else ""
                    ).strip()
                if direct_id and pos not in out:
                    direct_row = nodes_by_id.get(direct_id)
                    if isinstance(direct_row, dict):
                        out[pos] = str(direct_row.get("name") or "").strip()
            return out

        label_by_key = {k: v for k, v in self._home_term_display_title_options()}
        buckets: dict[tuple[str, str], list[int]] = {}

        for order in orders or []:
            if not isinstance(order, dict):
                continue
            if str(order.get("status") or "production").strip().lower() == "production":
                continue
            duration = order.get("duration_production_days")
            if duration is None:
                duration = self._days_between_or_none(
                    order.get("order_date"), order.get("date_shipped")
                )
            if duration is None:
                duration = self._days_between_or_none(
                    order.get("order_date"), order.get("date_accepted")
                )
            if duration is None:
                continue
            try:
                duration = int(duration)
            except Exception:
                continue

            items = order.get("items") if isinstance(order.get("items"), list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("product_id") or "").strip()
                if not pid:
                    continue  # skip items with no catalog product (custom "Своя назва")
                prod = products_by_id.get(pid) if pid else None

                if "name" in selected_key_set:
                    display_name = str(
                        item.get("product_name")
                        or (prod.get("name") if isinstance(prod, dict) else "")
                        or item.get("name")
                        or ""
                    ).strip()
                    if display_name:
                        buckets.setdefault(("name", display_name), []).append(duration)

                cat_id = str(
                    (prod.get("category_id") if isinstance(prod, dict) else None)
                    or item.get("category_id")
                    or ""
                ).strip()
                if not cat_id:
                    continue

                if "category" in selected_key_set:
                    cat_name = categories_by_id.get(cat_id, "")
                    if cat_name:
                        buckets.setdefault(("category", cat_name), []).append(duration)

                cfg = cat_cfg_by_id.get(cat_id) or {}
                levels = (
                    cfg.get("levels") if isinstance(cfg.get("levels"), list) else []
                )
                node_id = str(
                    item.get("catalog_node_id")
                    or (prod.get("catalog_node_id") if isinstance(prod, dict) else "")
                    or ""
                ).strip()
                node_names_by_pos = _node_names_by_position(node_id, item)
                for level in levels:
                    if not isinstance(level, dict) or not bool(level.get("enabled")):
                        continue
                    key = str(level.get("title") or "").strip().lower()
                    if key not in selected_key_set or key in {"name", "category"}:
                        continue
                    try:
                        pos = int(level.get("position") or 0)
                    except Exception:
                        pos = 0
                    node_name = str(node_names_by_pos.get(pos) or "").strip()
                    if not node_name:
                        continue
                    buckets.setdefault((key, node_name), []).append(duration)

        rows: list[tuple[str, str]] = []
        for key in keys:
            matches = [
                (name, vals)
                for (bucket_key, name), vals in buckets.items()
                if bucket_key == key and vals
            ]
            matches.sort(key=lambda pair: pair[0].lower())
            for name, vals in matches:
                avg = round(sum(float(v) for v in vals) / max(1, len(vals)))
                label = (
                    name if len(keys) == 1 else f"{label_by_key.get(key, key)}: {name}"
                )
                rows.append((label, f"{int(avg)} днів"))

        if not rows:
            rows.append(("Немає даних", "-"))
        return rows

    def _home_cost_display_state_key(self) -> str:
        return f"{self._table_layout_state_key()}:cost_display"

    def _load_home_cost_display_title_keys(self) -> list[str]:
        try:
            manager_id = str(self.manager_id or "").strip()
            if not manager_id:
                return []
            payload = self.db.get_manager_workspace_state(manager_id) or {}
            if not isinstance(payload, dict):
                return []
            store = (
                payload.get("home_cost_display")
                if isinstance(payload.get("home_cost_display"), dict)
                else {}
            )
            raw = store.get(self._home_cost_display_state_key())
            if not isinstance(raw, list):
                return []
            out = [str(x).strip().lower() for x in raw if str(x).strip()]
            return out
        except Exception:
            return []

    def _save_home_cost_display_title_keys(self, keys: list[str]) -> None:
        try:
            manager_id = str(self.manager_id or "").strip()
            if not manager_id:
                return
            payload = self.db.get_manager_workspace_state(manager_id) or {}
            if not isinstance(payload, dict):
                payload = {}
            store = (
                payload.get("home_cost_display")
                if isinstance(payload.get("home_cost_display"), dict)
                else {}
            )
            store[self._home_cost_display_state_key()] = [
                str(x).strip().lower() for x in (keys or []) if str(x).strip()
            ]
            payload["home_cost_display"] = store
            self.db.save_manager_workspace_state(manager_id, payload)
        except Exception:
            pass

    def _home_cost_display_title_options(self) -> list[tuple[str, str]]:
        try:
            # Use _categories_in_scope() — already handles general mode (combined from all managers)
            categories = self._categories_in_scope() or []
        except Exception:
            categories = []

        options: list[tuple[str, str]] = [("name", "Назва")]
        seen = {"name", "category"}
        titled: list[tuple[int, str, str]] = []
        for cat in categories:
            if not isinstance(cat, dict):
                continue
            cfg = self.db.get_category_levels_config(cat) or {}
            levels = cfg.get("levels") if isinstance(cfg.get("levels"), list) else []
            for row in levels:
                if not isinstance(row, dict) or not bool(row.get("enabled")):
                    continue
                label = str(row.get("title") or "").strip()
                if not label:
                    continue
                key = label.lower()
                if key in seen:
                    continue
                seen.add(key)
                try:
                    pos = int(row.get("position") or 0)
                except Exception:
                    pos = 0
                titled.append((pos, label.lower(), key))
                options.append((key, label))

        tail = sorted(
            options[1:],
            key=lambda pair: (
                next((p for p, _, k in titled if k == pair[0]), 999),
                pair[1].lower(),
            ),
        )
        return [options[0], ("category", "Категорія")] + tail

    def _selected_home_cost_display_title_keys(self) -> list[str]:
        options = self._home_cost_display_title_options()
        available_keys = [
            str(k or "").strip().lower() for k, _ in options if str(k or "").strip()
        ]
        saved = [
            k for k in self._load_home_cost_display_title_keys() if k in available_keys
        ]
        if saved:
            return saved
        return ["name"] if "name" in available_keys else []

    def _home_cost_production_rows(
        self, orders: list[dict], selected_title_keys: list[str]
    ) -> list[tuple[str, str]]:
        """Average cost rows for Home dashboard.

        Same grouping logic as `_home_term_production_rows`, but value source is
        Cost Price Orders column `С/В$` (`unit_cost_usd` in cost_price_order_inputs).
        """
        keys = [
            str(k or "").strip().lower()
            for k in (selected_title_keys or [])
            if str(k or "").strip()
        ]
        if not keys:
            return [("Немає даних", "-")]

        selected_key_set = set(keys)
        region_id = self._resolve_region_id()

        # Cost price inputs — combine from all managers in general mode
        try:
            if self.general_mode and self.general_manager_ids:
                inputs_by_key: dict = {}
                for mid in self.general_manager_ids:
                    try:
                        partial = (
                            self.db.get_cost_price_order_inputs(
                                self.company_id, region_id=region_id, manager_id=mid
                            )
                            or {}
                        )
                        inputs_by_key.update(partial)
                    except Exception:
                        pass
            else:
                inputs_by_key = (
                    self.db.get_cost_price_order_inputs(
                        self.company_id,
                        region_id=region_id,
                        manager_id=str(self.manager_id or "").strip(),
                    )
                    or {}
                )
        except Exception:
            inputs_by_key = {}

        # Products and categories — use combined scope (handles general mode internally)
        try:
            all_products = self._products_in_scope() or []
        except Exception:
            all_products = []

        products_by_id = {}
        for prod in all_products:
            if not isinstance(prod, dict):
                continue
            pid = str(prod.get("id") or "").strip()
            if pid:
                products_by_id[pid] = prod

        try:
            categories = self._categories_in_scope() or []
        except Exception:
            categories = []

        cat_cfg_by_id: dict[str, dict] = {}
        for cat in categories or []:
            if not isinstance(cat, dict):
                continue
            cid = str(cat.get("id") or "").strip()
            if not cid:
                continue
            cat_cfg_by_id[cid] = self.db.get_category_levels_config(cat) or {}

        categories_by_id: dict[str, str] = {
            str(c.get("id") or "").strip(): str(c.get("name") or "").strip()
            for c in (categories or [])
            if isinstance(c, dict) and c.get("id")
        }

        try:
            all_nodes = self.db.get_company_catalog_nodes(self.company_id) or []
        except Exception:
            all_nodes = []
        nodes_by_id = {
            str(node.get("id") or "").strip(): node
            for node in all_nodes
            if isinstance(node, dict) and str(node.get("id") or "").strip()
        }

        def _node_names_by_position(
            node_id: str, item: dict | None = None
        ) -> dict[int, str]:
            out: dict[int, str] = {}
            cur = str(node_id or "").strip()
            hops = 0
            while cur and hops < 40:
                row = nodes_by_id.get(cur)
                if not isinstance(row, dict):
                    break
                try:
                    pos = int(row.get("level_position") or 0)
                except Exception:
                    pos = 0
                if 1 <= pos <= 6 and pos not in out:
                    out[pos] = str(row.get("name") or "").strip()
                cur = str(row.get("parent_node_id") or "").strip()
                hops += 1
            for pos in range(1, 7):
                direct_id = ""
                if isinstance(item, dict):
                    direct_id = str(item.get(f"level_{pos}_node_id") or "").strip()
                if not direct_id:
                    direct_id = str(
                        (
                            products_by_id.get(
                                str(item.get("product_id") or "").strip()
                            )
                            or {}
                        ).get(f"level_{pos}_node_id")
                        if isinstance(item, dict)
                        else ""
                    ).strip()
                if direct_id and pos not in out:
                    direct_row = nodes_by_id.get(direct_id)
                    if isinstance(direct_row, dict):
                        out[pos] = str(direct_row.get("name") or "").strip()
            return out

        label_by_key = {k: v for k, v in self._home_cost_display_title_options()}
        buckets: dict[tuple[str, str], list[float]] = {}

        for order in orders or []:
            if not isinstance(order, dict):
                continue

            items = order.get("items") if isinstance(order.get("items"), list) else []
            order_id = str(order.get("id") or "").strip()
            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue

                if not order_id:
                    continue
                input_key = f"{order_id}:{int(idx)}"
                input_row = (
                    inputs_by_key.get(input_key)
                    if isinstance(inputs_by_key, dict)
                    else None
                )
                cost = self._safe_float((input_row or {}).get("unit_cost_usd"), None)
                cost_ccy = str(
                    (input_row or {}).get("unit_cost_currency") or ""
                ).upper().strip()
                if cost is None:
                    # Historical fallback from orders table calc value.
                    cost = self._safe_float(item.get("unit_cost_calc_usd"), None)
                    cost_ccy = str(
                        item.get("unit_cost_calc_currency") or ""
                    ).upper().strip()
                if cost is None:
                    continue
                if float(cost) < 0:
                    continue
                # Items/inputs can each be in a different native pivot
                # currency now — normalize to USD before bucketing/averaging
                # across many different products below.
                cost_usd = fx_rates.convert(float(cost), cost_ccy or "USD", "USD")
                if cost_usd is None:
                    continue
                cost = cost_usd

                pid = str(item.get("product_id") or "").strip()
                if not pid:
                    continue  # skip items with no catalog product (custom "Своя назва")
                prod = products_by_id.get(pid) if pid else None

                if "name" in selected_key_set:
                    display_name = str(
                        (prod.get("name") if isinstance(prod, dict) else "")
                        or item.get("product_name")
                        or item.get("name")
                        or ""
                    ).strip()
                    if display_name:
                        buckets.setdefault(("name", display_name), []).append(
                            float(cost)
                        )

                cat_id = str(
                    (prod.get("category_id") if isinstance(prod, dict) else None)
                    or item.get("category_id")
                    or ""
                ).strip()
                if not cat_id:
                    continue

                if "category" in selected_key_set:
                    cat_name = categories_by_id.get(cat_id, "")
                    if cat_name:
                        buckets.setdefault(("category", cat_name), []).append(
                            float(cost)
                        )

                cfg = cat_cfg_by_id.get(cat_id) or {}
                levels = (
                    cfg.get("levels") if isinstance(cfg.get("levels"), list) else []
                )
                node_id = str(
                    item.get("catalog_node_id")
                    or (prod.get("catalog_node_id") if isinstance(prod, dict) else "")
                    or ""
                ).strip()
                node_names_by_pos = _node_names_by_position(node_id, item)
                for level in levels:
                    if not isinstance(level, dict) or not bool(level.get("enabled")):
                        continue
                    key = str(level.get("title") or "").strip().lower()
                    if key not in selected_key_set or key in {"name", "category"}:
                        continue
                    try:
                        pos = int(level.get("position") or 0)
                    except Exception:
                        pos = 0
                    node_name = str(node_names_by_pos.get(pos) or "").strip()
                    if not node_name:
                        continue
                    buckets.setdefault((key, node_name), []).append(float(cost))

        rows: list[tuple[str, str]] = []
        for key in keys:
            matches = [
                (name, vals)
                for (bucket_key, name), vals in buckets.items()
                if bucket_key == key and vals
            ]
            matches.sort(key=lambda pair: pair[0].lower())
            for name, vals in matches:
                avg = sum(float(v) for v in vals) / max(1, len(vals))
                label = (
                    name if len(keys) == 1 else f"{label_by_key.get(key, key)}: {name}"
                )
                rows.append((label, f"${avg:.2f}"))

        if not rows:
            rows.append(("Немає даних", "-"))
        return rows

    def _open_home_term_display_settings(self, _=None):
        page = getattr(self.content_area, "page", None)
        if not page:
            return
        options = self._home_term_display_title_options()
        selected = set(self._selected_home_term_display_title_keys())
        checkboxes: list[ft.Checkbox] = []

        def _mk_checkbox(key: str, label: str):
            cb = ft.Checkbox(value=(key in selected), label=label)
            checkboxes.append(cb)
            return cb

        items = [_mk_checkbox(key, label) for key, label in options]

        def _save(_e=None):
            picked = [
                key
                for (key, _), cb in zip(options, checkboxes)
                if bool(getattr(cb, "value", False))
            ]
            try:
                page.pop_dialog()
            except Exception:
                try:
                    dlg.open = False
                    page.update()
                except Exception:
                    pass

            async def _bg(_p=list(picked)):
                import asyncio as _ai

                with contextlib.suppress(Exception):
                    await _ai.to_thread(self._save_home_term_display_title_keys, _p)
                self.build()

            try:
                page.run_task(_bg)
            except Exception:
                with contextlib.suppress(Exception):
                    self._save_home_term_display_title_keys(picked)
                self.build()

        def _cancel(_e=None):
            try:
                page.pop_dialog()
            except Exception:
                try:
                    dlg.open = False
                    page.update()
                except Exception:
                    pass

        _dlg_title_prod = "Що показувати в середньому терміні виробництва"
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Text(_dlg_title_prod, expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=lambda _: self._show_display_selection_help(page, _dlg_title_prod)),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
            content=ft.Container(
                width=420,
                height=320,
                content=ft.Column(items, spacing=0, scroll=ft.ScrollMode.ADAPTIVE),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=_cancel),
                ft.FilledButton("Зберегти", on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    def _show_help(self) -> None:
        page = getattr(self.content_area, "page", None)
        if not page:
            return
        from views.help_utils import show_modal_help
        t = self.t
        show_modal_help(page, t("home.help.title", "Довідка — Головна"), [
            t("home.help.desc", "Головна — ваш особистий дашборд дій. Показує лічильники проблем і зведені середні показники по замовленнях."),
            (t("home.help.steps_title", "Як користуватись"), None),
            t("home.help.step1", "1. Клацніть будь-яку картку — система відкриє відповідний розділ з відфільтрованими даними."),
            t("home.help.step2", "2. Колір картки: 🔴 червоний — проблема, 🟡 жовтий — попередження, 🟢 зелений — все гаразд."),
            t("home.help.step3", "3. Прокрутіть донизу щоб побачити таблиці середніх показників."),
            (t("home.help.counters_title", "Лічильники"), None),
            t("home.help.c_overdue", "🔴 Не замовлених — задачі, у яких минула дата виконання і статус ще не «замовлено»."),
            t("home.help.c_need_order", "🟡 Треба Замовити — активні задачі замовлення без статусу «замовлено»."),
            t("home.help.c_logistics", "В дорозі/склад/прийняті — замовлення у фінальних статусах без фактичної вартості логістики."),
            t("home.help.c_status_date", "Дата статусу — замовлення де поле дати для поточного статусу порожнє або стоїть у майбутньому."),
            t("home.help.c_1c", "Не внесених в облік — замовлення що вийшли з виробництва та ще не позначені як внесені в облік."),
            t("home.help.c_weight", "Замовлень без факт ваги — замовлення без вказаної фактичної ваги для розпаковки."),
            t("home.help.c_products", "Товарів без даних — товари з незаповненими обов'язковими полями."),
            t("home.help.c_series", "Серій не дороблених — незавершені серії, ще не перенесені в картку товару."),
            t("home.help.c_margin", "Категорій без цільової рент. — категорії без заданого % цільової рентабельності."),
            t("home.help.c_supplier_debt", "Постачальників товару — кількість з невирівняним балансом (🔴 ми винні, 🟡 постачальник винен)."),
            t("home.help.c_pack_stock", "Залишок Пакування — позиції з критично малим залишком (🟡 < 5 000 шт, 🔴 < 2 500 шт)."),
            t("home.help.c_pack_debt", "Постачальників пакування — постачальники пакування з невирівняним балансом."),
            t("home.help.c_suppliers_empty", "Постачальників без даних — постачальники з незаповненими обов'язковими полями."),
            t("home.help.c_media", "Медіа-план — записи, що давно не оновлювались (🟡 14–30 дн, 🔴 30+ дн)."),
            t("home.help.c_media_schedule", "Кінець медіаплану — кількість днів до завершення поточного медіа-циклу."),
            t("home.help.c_totals", "Загальні показники — повна кількість постачальників, категорій, товарів, пакувань і замовлень."),
            (t("home.help.tables_title", "Таблиці середніх показників"), None),
            t("home.help.t_delivery", "Середній час доставки — середня кількість днів від відправки до прийому, по регіонах."),
            t("home.help.t_tariff", "Середній тариф доставки — середня вартість доставки на одиницю товару по регіонах."),
            t("home.help.t_shoulder", "Середнє плече — середній логістичний час до розподільника по регіонах."),
            t("home.help.t_terms", "Середні терміни — зведення середніх виробничих і логістичних термінів по замовленнях."),
            t("home.help.t_prod_term", "Середній термін виробництва — середній час від замовлення до готовності по обраних товарах (⚙ налаштовується)."),
            t("home.help.t_weight", "Середня фактична вага — середня фактична вага замовлень по обраних товарах."),
            t("home.help.t_cost", "Середня Собівартість Замовлення — середня повна вартість одного замовлення по товарах."),
            t("home.help.t_stock_cost", "Середня Собівартість Склад — поточна середня собівартість товарів на складі."),
            t("home.help.t_blended", "Середня Собівартість З+С — зведена собівартість: замовлення + склад разом."),
        ])

    def _show_display_selection_help(self, page, title: str) -> None:
        from views.help_utils import show_modal_help
        show_modal_help(page, title, [
            self.t("home.display_help.step1", "1. Відмітьте показники які потрібно відображати в таблиці."),
            self.t("home.display_help.step2", "2. Натисніть «Зберегти»."),
        ])

    def _open_home_cost_display_settings(self, _=None):
        page = getattr(self.content_area, "page", None)
        if not page:
            return
        options = self._home_cost_display_title_options()
        selected = set(self._selected_home_cost_display_title_keys())
        checkboxes: list[ft.Checkbox] = []

        def _mk_checkbox(key: str, label: str):
            cb = ft.Checkbox(value=(key in selected), label=label)
            checkboxes.append(cb)
            return cb

        items = [_mk_checkbox(key, label) for key, label in options]

        def _save(_e=None):
            picked = [
                key
                for (key, _), cb in zip(options, checkboxes)
                if bool(getattr(cb, "value", False))
            ]
            try:
                page.pop_dialog()
            except Exception:
                try:
                    dlg.open = False
                    page.update()
                except Exception:
                    pass

            async def _bg(_p=list(picked)):
                import asyncio as _ai

                with contextlib.suppress(Exception):
                    await _ai.to_thread(self._save_home_cost_display_title_keys, _p)
                self.build()

            try:
                page.run_task(_bg)
            except Exception:
                with contextlib.suppress(Exception):
                    self._save_home_cost_display_title_keys(picked)
                self.build()

        def _cancel(_e=None):
            try:
                page.pop_dialog()
            except Exception:
                try:
                    dlg.open = False
                    page.update()
                except Exception:
                    pass

        _dlg_title_cost = "Що показувати в середній собівартості"
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Text(_dlg_title_cost, expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=lambda _: self._show_display_selection_help(page, _dlg_title_cost)),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
            content=ft.Container(
                width=420,
                height=320,
                content=ft.Column(items, spacing=0, scroll=ft.ScrollMode.ADAPTIVE),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=_cancel),
                ft.FilledButton("Зберегти", on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    # ── Середня фактична вага (по товару) ────────────────────────────────────

    def _home_fact_weight_display_state_key(self) -> str:
        return f"{self._table_layout_state_key()}:fact_weight_display"

    def _load_home_fact_weight_display_title_keys(self) -> list[str]:
        try:
            manager_id = str(self.manager_id or "").strip()
            if not manager_id:
                return []
            payload = self.db.get_manager_workspace_state(manager_id) or {}
            if not isinstance(payload, dict):
                return []
            store = (
                payload.get("home_fact_weight_display")
                if isinstance(payload.get("home_fact_weight_display"), dict)
                else {}
            )
            raw = store.get(self._home_fact_weight_display_state_key())
            if not isinstance(raw, list):
                return []
            return [str(x).strip().lower() for x in raw if str(x).strip()]
        except Exception:
            return []

    def _save_home_fact_weight_display_title_keys(self, keys: list[str]) -> None:
        try:
            manager_id = str(self.manager_id or "").strip()
            if not manager_id:
                return
            payload = self.db.get_manager_workspace_state(manager_id) or {}
            if not isinstance(payload, dict):
                payload = {}
            store = (
                payload.get("home_fact_weight_display")
                if isinstance(payload.get("home_fact_weight_display"), dict)
                else {}
            )
            store[self._home_fact_weight_display_state_key()] = [
                str(x).strip().lower() for x in (keys or []) if str(x).strip()
            ]
            payload["home_fact_weight_display"] = store
            self.db.save_manager_workspace_state(manager_id, payload)
        except Exception:
            pass

    def _home_fact_weight_display_title_options(self) -> list[tuple[str, str]]:
        try:
            categories = self._categories_in_scope() or []
        except Exception:
            categories = []
        options: list[tuple[str, str]] = [("name", "Назва")]
        seen = {"name", "category"}
        titled: list[tuple[int, str, str]] = []
        for cat in categories:
            if not isinstance(cat, dict):
                continue
            cfg = self.db.get_category_levels_config(cat) or {}
            levels = cfg.get("levels") if isinstance(cfg.get("levels"), list) else []
            for row in levels:
                if not isinstance(row, dict) or not bool(row.get("enabled")):
                    continue
                label = str(row.get("title") or "").strip()
                if not label:
                    continue
                key = label.lower()
                if key in seen:
                    continue
                seen.add(key)
                try:
                    pos = int(row.get("position") or 0)
                except Exception:
                    pos = 0
                titled.append((pos, label.lower(), key))
                options.append((key, label))
        tail = sorted(
            options[1:],
            key=lambda pair: (
                next((p for p, _, k in titled if k == pair[0]), 999),
                pair[1].lower(),
            ),
        )
        return [options[0], ("category", "Категорія")] + tail

    def _selected_home_fact_weight_display_title_keys(self) -> list[str]:
        options = self._home_fact_weight_display_title_options()
        available_keys = [
            str(k or "").strip().lower() for k, _ in options if str(k or "").strip()
        ]
        saved = [
            k
            for k in self._load_home_fact_weight_display_title_keys()
            if k in available_keys
        ]
        if saved:
            return saved
        return ["name"] if "name" in available_keys else []

    def _home_fact_weight_rows(
        self, orders: list[dict], selected_title_keys: list[str]
    ) -> list[tuple[str, str]]:
        """Average actual unit weight = Σ(weight_actual_kg) / Σ(qty) per product group.

        Only items where weight_actual_kg is set and > 0 are included.
        """
        keys = [
            str(k or "").strip().lower()
            for k in (selected_title_keys or [])
            if str(k or "").strip()
        ]
        if not keys:
            return [("Немає даних", "-")]

        selected_key_set = set(keys)

        try:
            all_products = self._products_in_scope() or []
        except Exception:
            all_products = []
        products_by_id = {}
        for prod in all_products:
            if not isinstance(prod, dict):
                continue
            pid = str(prod.get("id") or "").strip()
            if pid:
                products_by_id[pid] = prod

        try:
            categories = self._categories_in_scope() or []
        except Exception:
            categories = []
        cat_cfg_by_id: dict[str, dict] = {}
        for cat in categories or []:
            if not isinstance(cat, dict):
                continue
            cid = str(cat.get("id") or "").strip()
            if not cid:
                continue
            cat_cfg_by_id[cid] = self.db.get_category_levels_config(cat) or {}

        categories_by_id: dict[str, str] = {
            str(c.get("id") or "").strip(): str(c.get("name") or "").strip()
            for c in (categories or [])
            if isinstance(c, dict) and c.get("id")
        }

        try:
            all_nodes = self.db.get_company_catalog_nodes(self.company_id) or []
        except Exception:
            all_nodes = []
        nodes_by_id = {
            str(node.get("id") or "").strip(): node
            for node in all_nodes
            if isinstance(node, dict) and str(node.get("id") or "").strip()
        }

        def _node_names_by_position(
            node_id: str, item: dict | None = None
        ) -> dict[int, str]:
            out: dict[int, str] = {}
            cur = str(node_id or "").strip()
            hops = 0
            while cur and hops < 40:
                row = nodes_by_id.get(cur)
                if not isinstance(row, dict):
                    break
                try:
                    pos = int(row.get("level_position") or 0)
                except Exception:
                    pos = 0
                if 1 <= pos <= 6 and pos not in out:
                    out[pos] = str(row.get("name") or "").strip()
                cur = str(row.get("parent_node_id") or "").strip()
                hops += 1
            for pos in range(1, 7):
                direct_id = ""
                if isinstance(item, dict):
                    direct_id = str(item.get(f"level_{pos}_node_id") or "").strip()
                if not direct_id:
                    direct_id = str(
                        (
                            products_by_id.get(
                                str(item.get("product_id") or "").strip()
                            )
                            or {}
                        ).get(f"level_{pos}_node_id")
                        if isinstance(item, dict)
                        else ""
                    ).strip()
                if direct_id and pos not in out:
                    direct_row = nodes_by_id.get(direct_id)
                    if isinstance(direct_row, dict):
                        out[pos] = str(direct_row.get("name") or "").strip()
            return out

        label_by_key = {
            k: v for k, v in self._home_fact_weight_display_title_options()
        }
        # Accumulate total weight and total qty per bucket — Σw / Σq is more accurate
        # than avg(w/q) when order sizes differ significantly.
        buckets_w: dict[tuple[str, str], float] = {}
        buckets_q: dict[tuple[str, str], float] = {}

        def _add(bucket_key, w, q):
            buckets_w[bucket_key] = buckets_w.get(bucket_key, 0.0) + w
            buckets_q[bucket_key] = buckets_q.get(bucket_key, 0.0) + q

        for order in orders or []:
            if not isinstance(order, dict):
                continue
            items = order.get("items") if isinstance(order.get("items"), list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("product_id") or "").strip()
                if not pid:
                    continue
                weight_fact = self._safe_float(item.get("weight_actual_kg"), None)
                qty = self._safe_float(item.get("qty") or item.get("quantity"), None)
                if weight_fact is None or weight_fact <= 0 or qty is None or qty <= 0:
                    continue
                prod = products_by_id.get(pid)

                if "name" in selected_key_set:
                    display_name = str(
                        (prod.get("name") if isinstance(prod, dict) else "")
                        or item.get("product_name")
                        or item.get("name")
                        or ""
                    ).strip()
                    if display_name:
                        _add(("name", display_name), float(weight_fact), float(qty))

                cat_id = str(
                    (prod.get("category_id") if isinstance(prod, dict) else None)
                    or item.get("category_id")
                    or ""
                ).strip()
                if not cat_id:
                    continue

                if "category" in selected_key_set:
                    cat_name = categories_by_id.get(cat_id, "")
                    if cat_name:
                        _add(("category", cat_name), float(weight_fact), float(qty))

                cfg = cat_cfg_by_id.get(cat_id) or {}
                levels = (
                    cfg.get("levels") if isinstance(cfg.get("levels"), list) else []
                )
                node_id = str(
                    item.get("catalog_node_id")
                    or (prod.get("catalog_node_id") if isinstance(prod, dict) else "")
                    or ""
                ).strip()
                node_names_by_pos = _node_names_by_position(node_id, item)
                for level in levels:
                    if not isinstance(level, dict) or not bool(level.get("enabled")):
                        continue
                    key = str(level.get("title") or "").strip().lower()
                    if key not in selected_key_set or key in {"name", "category"}:
                        continue
                    try:
                        pos = int(level.get("position") or 0)
                    except Exception:
                        pos = 0
                    node_name = str(node_names_by_pos.get(pos) or "").strip()
                    if not node_name:
                        continue
                    _add((key, node_name), float(weight_fact), float(qty))

        rows: list[tuple[str, str]] = []
        for key in keys:
            matches = [
                (name, buckets_w[bk], buckets_q[bk])
                for bk in buckets_w
                if bk[0] == key and buckets_q.get(bk, 0) > 0
                for name in [bk[1]]
            ]
            matches.sort(key=lambda t: t[0].lower())
            for name, total_w, total_q in matches:
                avg = total_w / total_q
                label = (
                    name if len(keys) == 1 else f"{label_by_key.get(key, key)}: {name}"
                )
                rows.append((label, f"{avg:.3f} кг"))

        if not rows:
            rows.append(("Немає даних", "-"))
        return rows

    def _open_home_fact_weight_display_settings(self, _=None):
        page = getattr(self.content_area, "page", None)
        if not page:
            return
        options = self._home_fact_weight_display_title_options()
        selected = set(self._selected_home_fact_weight_display_title_keys())
        checkboxes: list[ft.Checkbox] = []

        def _mk_checkbox(key: str, label: str):
            cb = ft.Checkbox(value=(key in selected), label=label)
            checkboxes.append(cb)
            return cb

        items = [_mk_checkbox(key, label) for key, label in options]

        def _save(_e=None):
            picked = [
                key
                for (key, _), cb in zip(options, checkboxes)
                if bool(getattr(cb, "value", False))
            ]
            try:
                page.pop_dialog()
            except Exception:
                try:
                    dlg.open = False
                    page.update()
                except Exception:
                    pass

            async def _bg(_p=list(picked)):
                import asyncio as _ai

                with contextlib.suppress(Exception):
                    await _ai.to_thread(
                        self._save_home_fact_weight_display_title_keys, _p
                    )
                self.build()

            try:
                page.run_task(_bg)
            except Exception:
                with contextlib.suppress(Exception):
                    self._save_home_fact_weight_display_title_keys(picked)
                self.build()

        def _cancel(_e=None):
            try:
                page.pop_dialog()
            except Exception:
                try:
                    dlg.open = False
                    page.update()
                except Exception:
                    pass

        _dlg_title_weight = "Що показувати в середній фактичній вазі товару"
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Text(_dlg_title_weight, expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=lambda _: self._show_display_selection_help(page, _dlg_title_weight)),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
            content=ft.Container(
                width=420,
                height=320,
                content=ft.Column(items, spacing=0, scroll=ft.ScrollMode.ADAPTIVE),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=_cancel),
                ft.FilledButton("Зберегти", on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    # ── Середнє плече (по товару) ─────────────────────────────────────────────

    def _home_shoulder_display_state_key(self) -> str:
        return f"{self._table_layout_state_key()}:shoulder_display"

    def _load_home_shoulder_display_title_keys(self) -> list[str]:
        try:
            manager_id = str(self.manager_id or "").strip()
            if not manager_id:
                return []
            payload = self.db.get_manager_workspace_state(manager_id) or {}
            if not isinstance(payload, dict):
                return []
            store = (
                payload.get("home_shoulder_display")
                if isinstance(payload.get("home_shoulder_display"), dict)
                else {}
            )
            raw = store.get(self._home_shoulder_display_state_key())
            if not isinstance(raw, list):
                return []
            return [str(x).strip().lower() for x in raw if str(x).strip()]
        except Exception:
            return []

    def _save_home_shoulder_display_title_keys(self, keys: list[str]) -> None:
        try:
            manager_id = str(self.manager_id or "").strip()
            if not manager_id:
                return
            payload = self.db.get_manager_workspace_state(manager_id) or {}
            if not isinstance(payload, dict):
                payload = {}
            store = (
                payload.get("home_shoulder_display")
                if isinstance(payload.get("home_shoulder_display"), dict)
                else {}
            )
            store[self._home_shoulder_display_state_key()] = [
                str(x).strip().lower() for x in (keys or []) if str(x).strip()
            ]
            payload["home_shoulder_display"] = store
            self.db.save_manager_workspace_state(manager_id, payload)
        except Exception:
            pass

    def _home_shoulder_display_title_options(self) -> list[tuple[str, str]]:
        try:
            categories = self._categories_in_scope() or []
        except Exception:
            categories = []
        options: list[tuple[str, str]] = [("name", "Назва")]
        seen = {"name", "category"}
        titled: list[tuple[int, str, str]] = []
        for cat in categories:
            if not isinstance(cat, dict):
                continue
            cfg = self.db.get_category_levels_config(cat) or {}
            levels = cfg.get("levels") if isinstance(cfg.get("levels"), list) else []
            for row in levels:
                if not isinstance(row, dict) or not bool(row.get("enabled")):
                    continue
                label = str(row.get("title") or "").strip()
                if not label:
                    continue
                key = label.lower()
                if key in seen:
                    continue
                seen.add(key)
                try:
                    pos = int(row.get("position") or 0)
                except Exception:
                    pos = 0
                titled.append((pos, label.lower(), key))
                options.append((key, label))
        tail = sorted(
            options[1:],
            key=lambda pair: (
                next((p for p, _, k in titled if k == pair[0]), 999),
                pair[1].lower(),
            ),
        )
        return [options[0], ("category", "Категорія")] + tail

    def _selected_home_shoulder_display_title_keys(self) -> list[str]:
        options = self._home_shoulder_display_title_options()
        available_keys = [
            str(k or "").strip().lower() for k, _ in options if str(k or "").strip()
        ]
        saved = [
            k
            for k in self._load_home_shoulder_display_title_keys()
            if k in available_keys
        ]
        if saved:
            return saved
        return ["name"] if "name" in available_keys else []

    def _home_shoulder_product_rows(
        self, orders: list[dict], selected_title_keys: list[str]
    ) -> list[tuple[str, str]]:
        """Average shoulder (log_days) per product for accepted orders.

        Only includes accepted orders that have log_days set.
        Each accepted order's log_days is attributed to every product item it contains.
        """
        keys = [
            str(k or "").strip().lower()
            for k in (selected_title_keys or [])
            if str(k or "").strip()
        ]
        if not keys:
            return [("Немає даних", "-")]

        selected_key_set = set(keys)

        try:
            all_products = self._products_in_scope() or []
        except Exception:
            all_products = []
        products_by_id = {}
        for prod in all_products:
            if not isinstance(prod, dict):
                continue
            pid = str(prod.get("id") or "").strip()
            if pid:
                products_by_id[pid] = prod

        try:
            categories = self._categories_in_scope() or []
        except Exception:
            categories = []
        cat_cfg_by_id: dict[str, dict] = {}
        for cat in categories or []:
            if not isinstance(cat, dict):
                continue
            cid = str(cat.get("id") or "").strip()
            if not cid:
                continue
            cat_cfg_by_id[cid] = self.db.get_category_levels_config(cat) or {}

        categories_by_id: dict[str, str] = {
            str(c.get("id") or "").strip(): str(c.get("name") or "").strip()
            for c in (categories or [])
            if isinstance(c, dict) and c.get("id")
        }

        try:
            all_nodes = self.db.get_company_catalog_nodes(self.company_id) or []
        except Exception:
            all_nodes = []
        nodes_by_id = {
            str(node.get("id") or "").strip(): node
            for node in all_nodes
            if isinstance(node, dict) and str(node.get("id") or "").strip()
        }

        def _node_names_by_position(
            node_id: str, item: dict | None = None
        ) -> dict[int, str]:
            out: dict[int, str] = {}
            cur = str(node_id or "").strip()
            hops = 0
            while cur and hops < 40:
                row = nodes_by_id.get(cur)
                if not isinstance(row, dict):
                    break
                try:
                    pos = int(row.get("level_position") or 0)
                except Exception:
                    pos = 0
                if 1 <= pos <= 6 and pos not in out:
                    out[pos] = str(row.get("name") or "").strip()
                cur = str(row.get("parent_node_id") or "").strip()
                hops += 1
            for pos in range(1, 7):
                direct_id = ""
                if isinstance(item, dict):
                    direct_id = str(item.get(f"level_{pos}_node_id") or "").strip()
                if not direct_id:
                    direct_id = str(
                        (
                            products_by_id.get(
                                str(item.get("product_id") or "").strip()
                            )
                            or {}
                        ).get(f"level_{pos}_node_id")
                        if isinstance(item, dict)
                        else ""
                    ).strip()
                if direct_id and pos not in out:
                    direct_row = nodes_by_id.get(direct_id)
                    if isinstance(direct_row, dict):
                        out[pos] = str(direct_row.get("name") or "").strip()
            return out

        label_by_key = {k: v for k, v in self._home_shoulder_display_title_options()}
        buckets: dict[tuple[str, str], list[float]] = {}

        for order in orders or []:
            if not isinstance(order, dict):
                continue
            if self._normalize_order_status(order.get("status")) != "accepted":
                continue
            log_days = self._safe_float(order.get("log_days"), None)
            if log_days is None:
                continue
            items = order.get("items") if isinstance(order.get("items"), list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("product_id") or "").strip()
                if not pid:
                    continue
                prod = products_by_id.get(pid)

                if "name" in selected_key_set:
                    display_name = str(
                        (prod.get("name") if isinstance(prod, dict) else "")
                        or item.get("product_name")
                        or item.get("name")
                        or ""
                    ).strip()
                    if display_name:
                        buckets.setdefault(("name", display_name), []).append(
                            float(log_days)
                        )

                cat_id = str(
                    (prod.get("category_id") if isinstance(prod, dict) else None)
                    or item.get("category_id")
                    or ""
                ).strip()
                if not cat_id:
                    continue

                if "category" in selected_key_set:
                    cat_name = categories_by_id.get(cat_id, "")
                    if cat_name:
                        buckets.setdefault(("category", cat_name), []).append(
                            float(log_days)
                        )

                cfg = cat_cfg_by_id.get(cat_id) or {}
                levels = (
                    cfg.get("levels") if isinstance(cfg.get("levels"), list) else []
                )
                node_id = str(
                    item.get("catalog_node_id")
                    or (prod.get("catalog_node_id") if isinstance(prod, dict) else "")
                    or ""
                ).strip()
                node_names_by_pos = _node_names_by_position(node_id, item)
                for level in levels:
                    if not isinstance(level, dict) or not bool(level.get("enabled")):
                        continue
                    key = str(level.get("title") or "").strip().lower()
                    if key not in selected_key_set or key in {"name", "category"}:
                        continue
                    try:
                        pos = int(level.get("position") or 0)
                    except Exception:
                        pos = 0
                    node_name = str(node_names_by_pos.get(pos) or "").strip()
                    if not node_name:
                        continue
                    buckets.setdefault((key, node_name), []).append(float(log_days))

        rows: list[tuple[str, str]] = []
        for key in keys:
            matches = [
                (name, vals)
                for (bucket_key, name), vals in buckets.items()
                if bucket_key == key and vals
            ]
            matches.sort(key=lambda pair: pair[0].lower())
            for name, vals in matches:
                avg = sum(float(v) for v in vals) / max(1, len(vals))
                label = (
                    name if len(keys) == 1 else f"{label_by_key.get(key, key)}: {name}"
                )
                rows.append((label, f"{int(round(avg))} дн."))

        if not rows:
            rows.append(("Немає даних", "-"))
        return rows

    def _open_home_shoulder_display_settings(self, _=None):
        page = getattr(self.content_area, "page", None)
        if not page:
            return
        options = self._home_shoulder_display_title_options()
        selected = set(self._selected_home_shoulder_display_title_keys())
        checkboxes: list[ft.Checkbox] = []

        def _mk_checkbox(key: str, label: str):
            cb = ft.Checkbox(value=(key in selected), label=label)
            checkboxes.append(cb)
            return cb

        items = [_mk_checkbox(key, label) for key, label in options]

        def _save(_e=None):
            picked = [
                key
                for (key, _), cb in zip(options, checkboxes)
                if bool(getattr(cb, "value", False))
            ]
            try:
                page.pop_dialog()
            except Exception:
                try:
                    dlg.open = False
                    page.update()
                except Exception:
                    pass

            async def _bg(_p=list(picked)):
                import asyncio as _ai

                with contextlib.suppress(Exception):
                    await _ai.to_thread(self._save_home_shoulder_display_title_keys, _p)
                self.build()

            try:
                page.run_task(_bg)
            except Exception:
                with contextlib.suppress(Exception):
                    self._save_home_shoulder_display_title_keys(picked)
                self.build()

        def _cancel(_e=None):
            try:
                page.pop_dialog()
            except Exception:
                try:
                    dlg.open = False
                    page.update()
                except Exception:
                    pass

        _dlg_title_shoulder = "Що показувати в середньому плечі"
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Text(_dlg_title_shoulder, expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=lambda _: self._show_display_selection_help(page, _dlg_title_shoulder)),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
            content=ft.Container(
                width=420,
                height=320,
                content=ft.Column(items, spacing=0, scroll=ft.ScrollMode.ADAPTIVE),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=_cancel),
                ft.FilledButton("Зберегти", on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    # ── Середня Собівартість Склад ────────────────────────────────────────────

    def _home_stock_cost_display_state_key(self) -> str:
        return f"{self._table_layout_state_key()}:stock_cost_display"

    def _load_home_stock_cost_display_title_keys(self) -> list[str]:
        try:
            manager_id = str(self.manager_id or "").strip()
            if not manager_id:
                return []
            payload = self.db.get_manager_workspace_state(manager_id) or {}
            if not isinstance(payload, dict):
                return []
            store = (
                payload.get("home_stock_cost_display")
                if isinstance(payload.get("home_stock_cost_display"), dict)
                else {}
            )
            raw = store.get(self._home_stock_cost_display_state_key())
            if not isinstance(raw, list):
                return []
            return [str(x).strip().lower() for x in raw if str(x).strip()]
        except Exception:
            return []

    def _save_home_stock_cost_display_title_keys(self, keys: list[str]) -> None:
        try:
            manager_id = str(self.manager_id or "").strip()
            if not manager_id:
                return
            payload = self.db.get_manager_workspace_state(manager_id) or {}
            if not isinstance(payload, dict):
                payload = {}
            store = (
                payload.get("home_stock_cost_display")
                if isinstance(payload.get("home_stock_cost_display"), dict)
                else {}
            )
            store[self._home_stock_cost_display_state_key()] = [
                str(x).strip().lower() for x in (keys or []) if str(x).strip()
            ]
            payload["home_stock_cost_display"] = store
            self.db.save_manager_workspace_state(manager_id, payload)
        except Exception:
            pass

    def _home_stock_cost_display_title_options(self) -> list[tuple[str, str]]:
        try:
            categories = self._categories_in_scope() or []
        except Exception:
            categories = []
        options: list[tuple[str, str]] = [("name", "Назва")]
        seen = {"name", "category"}
        titled: list[tuple[int, str, str]] = []
        for cat in categories:
            if not isinstance(cat, dict):
                continue
            cfg = self.db.get_category_levels_config(cat) or {}
            levels = cfg.get("levels") if isinstance(cfg.get("levels"), list) else []
            for row in levels:
                if not isinstance(row, dict) or not bool(row.get("enabled")):
                    continue
                label = str(row.get("title") or "").strip()
                if not label:
                    continue
                key = label.lower()
                if key in seen:
                    continue
                seen.add(key)
                try:
                    pos = int(row.get("position") or 0)
                except Exception:
                    pos = 0
                titled.append((pos, label.lower(), key))
                options.append((key, label))
        tail = sorted(
            options[1:],
            key=lambda pair: (
                next((p for p, _, k in titled if k == pair[0]), 999),
                pair[1].lower(),
            ),
        )
        return [options[0], ("category", "Категорія")] + tail

    def _selected_home_stock_cost_display_title_keys(self) -> list[str]:
        options = self._home_stock_cost_display_title_options()
        available_keys = [
            str(k or "").strip().lower() for k, _ in options if str(k or "").strip()
        ]
        saved = [
            k
            for k in self._load_home_stock_cost_display_title_keys()
            if k in available_keys
        ]
        if saved:
            return saved
        return ["name"] if "name" in available_keys else []

    def _home_stock_freshness_badge(self) -> tuple[str, str]:
        """Returns (badge_text, badge_color) based on days since last stock input edit."""
        try:
            from datetime import date as _date

            region_id = self._resolve_region_id()
            if self.general_mode and self.general_manager_ids:
                all_inputs: dict = {}
                for mid in self.general_manager_ids:
                    partial = (
                        self.db.get_cost_price_stock_inputs(
                            self.company_id, region_id=region_id, manager_id=mid
                        )
                        or {}
                    )
                    all_inputs.update(partial)
            else:
                all_inputs = (
                    self.db.get_cost_price_stock_inputs(
                        self.company_id,
                        region_id=region_id,
                        manager_id=str(self.manager_id or "").strip(),
                    )
                    or {}
                )
            max_date = None
            for inp in (all_inputs or {}).values():
                if not isinstance(inp, dict):
                    continue
                ts_raw = inp.get("updated_at")
                if not ts_raw:
                    continue
                dt = self._parse_date(ts_raw)
                if dt and (max_date is None or dt > max_date):
                    max_date = dt
            if max_date is None:
                return ("—", _STONE)
            days = (_date.today() - max_date).days
            if days < 20:
                color = _OK
            elif days <= 30:
                color = _WARN2
            else:
                color = _ERR
            return (f"{days} дн.", color)
        except Exception:
            return ("?", _STONE)

    def _home_stock_cost_rows(
        self, selected_title_keys: list[str]
    ) -> list[tuple[str, str]]:
        """Average unit_cost_stock per product group, split by region when 'Всі регіони'."""
        if self._resolve_region_id() is None:
            region_map = self._region_name_map()
            if len(region_map) > 1:
                result: list[tuple[str, str]] = []
                for rid, rname in sorted(region_map.items(), key=lambda x: x[1]):
                    rows = self._home_stock_cost_rows_for_region(selected_title_keys, rid)
                    if rows and not (len(rows) == 1 and rows[0][1] == "-"):
                        result.append(("__REGION__", rname))
                        result.extend(rows)
                return result or [("Немає даних", "-")]
        return self._home_stock_cost_rows_for_region(
            selected_title_keys, self._resolve_region_id()
        )

    def _home_stock_cost_rows_for_region(
        self, selected_title_keys: list[str], region_id
    ) -> list[tuple[str, str]]:
        """Average unit_cost_stock (amount / qty) per product group from С/В Склад."""
        keys = [
            str(k or "").strip().lower()
            for k in (selected_title_keys or [])
            if str(k or "").strip()
        ]
        if not keys:
            return [("Немає даних", "-")]

        selected_key_set = set(keys)

        try:
            if self.general_mode and self.general_manager_ids:
                stock_inputs: dict = {}
                for mid in self.general_manager_ids:
                    partial = (
                        self.db.get_cost_price_stock_inputs(
                            self.company_id, region_id=region_id, manager_id=mid
                        )
                        or {}
                    )
                    for pid, inp in (partial or {}).items():
                        if pid not in stock_inputs:
                            stock_inputs[pid] = inp
            else:
                stock_inputs = (
                    self.db.get_cost_price_stock_inputs(
                        self.company_id,
                        region_id=region_id,
                        manager_id=str(self.manager_id or "").strip(),
                    )
                    or {}
                )
        except Exception:
            stock_inputs = {}

        try:
            all_products = self._products_in_scope() or []
        except Exception:
            all_products = []
        products_by_id = {}
        for prod in all_products:
            if not isinstance(prod, dict):
                continue
            pid = str(prod.get("id") or "").strip()
            if pid:
                products_by_id[pid] = prod

        try:
            categories = self._categories_in_scope() or []
        except Exception:
            categories = []
        cat_cfg_by_id: dict[str, dict] = {}
        for cat in categories or []:
            if not isinstance(cat, dict):
                continue
            cid = str(cat.get("id") or "").strip()
            if not cid:
                continue
            cat_cfg_by_id[cid] = self.db.get_category_levels_config(cat) or {}

        categories_by_id: dict[str, str] = {
            str(c.get("id") or "").strip(): str(c.get("name") or "").strip()
            for c in (categories or [])
            if isinstance(c, dict) and c.get("id")
        }

        try:
            all_nodes = self.db.get_company_catalog_nodes(self.company_id) or []
        except Exception:
            all_nodes = []
        nodes_by_id = {
            str(node.get("id") or "").strip(): node
            for node in all_nodes
            if isinstance(node, dict) and str(node.get("id") or "").strip()
        }

        def _node_names_by_position(pid: str) -> dict[int, str]:
            prod = products_by_id.get(pid) or {}
            out: dict[int, str] = {}
            node_id = str(prod.get("catalog_node_id") or "").strip()
            cur = node_id
            hops = 0
            while cur and hops < 40:
                row = nodes_by_id.get(cur)
                if not isinstance(row, dict):
                    break
                try:
                    pos = int(row.get("level_position") or 0)
                except Exception:
                    pos = 0
                if 1 <= pos <= 6 and pos not in out:
                    out[pos] = str(row.get("name") or "").strip()
                cur = str(row.get("parent_node_id") or "").strip()
                hops += 1
            for pos in range(1, 7):
                direct_id = str(prod.get(f"level_{pos}_node_id") or "").strip()
                if direct_id and pos not in out:
                    direct_row = nodes_by_id.get(direct_id)
                    if isinstance(direct_row, dict):
                        out[pos] = str(direct_row.get("name") or "").strip()
            return out

        label_by_key = {k: v for k, v in self._home_stock_cost_display_title_options()}
        buckets: dict[tuple[str, str], list[float]] = {}

        for pid, inp in (stock_inputs or {}).items():
            if not isinstance(inp, dict):
                continue
            qty = self._safe_float(inp.get("qty"), None)
            amount = self._safe_float(inp.get("amount"), None)
            if qty is None or qty <= 0 or amount is None:
                continue
            unit_cost = float(amount) / float(qty)
            prod = products_by_id.get(str(pid).strip())
            if prod is None:
                # Archived/deleted product — get_products() excludes archived
                # by default, and stock inputs carry no name snapshot of
                # their own, so there's no way to show it meaningfully. Skip
                # it entirely rather than lumping unrelated archived
                # products together under one generic "Видалений товар" row
                # that used to still count toward the visible averages.
                continue

            if "name" in selected_key_set:
                display_name = str(prod.get("name") or "").strip()
                if display_name:
                    buckets.setdefault(("name", display_name), []).append(unit_cost)

            cat_id = str(
                (prod.get("category_id") if isinstance(prod, dict) else "") or ""
            ).strip()
            if not cat_id:
                continue

            if "category" in selected_key_set:
                cat_name = categories_by_id.get(cat_id, "")
                if cat_name:
                    buckets.setdefault(("category", cat_name), []).append(unit_cost)

            cfg = cat_cfg_by_id.get(cat_id) or {}
            levels = cfg.get("levels") if isinstance(cfg.get("levels"), list) else []
            node_names_by_pos = _node_names_by_position(str(pid).strip())
            for level in levels:
                if not isinstance(level, dict) or not bool(level.get("enabled")):
                    continue
                key = str(level.get("title") or "").strip().lower()
                if key not in selected_key_set or key in {"name", "category"}:
                    continue
                try:
                    pos = int(level.get("position") or 0)
                except Exception:
                    pos = 0
                node_name = str(node_names_by_pos.get(pos) or "").strip()
                if not node_name:
                    continue
                buckets.setdefault((key, node_name), []).append(unit_cost)

        rows: list[tuple[str, str]] = []
        for key in keys:
            matches = [
                (name, vals)
                for (bucket_key, name), vals in buckets.items()
                if bucket_key == key and vals
            ]
            matches.sort(key=lambda pair: pair[0].lower())
            for name, vals in matches:
                avg = sum(float(v) for v in vals) / max(1, len(vals))
                label = (
                    name if len(keys) == 1 else f"{label_by_key.get(key, key)}: {name}"
                )
                rows.append((label, f"{avg:.2f}"))

        if not rows:
            rows.append(("Немає даних", "-"))
        return rows

    def _open_home_stock_cost_display_settings(self, _=None):
        page = getattr(self.content_area, "page", None)
        if not page:
            return
        options = self._home_stock_cost_display_title_options()
        selected = set(self._selected_home_stock_cost_display_title_keys())
        checkboxes: list[ft.Checkbox] = []

        def _mk_checkbox(key: str, label: str):
            cb = ft.Checkbox(value=(key in selected), label=label)
            checkboxes.append(cb)
            return cb

        items = [_mk_checkbox(key, label) for key, label in options]

        def _save(_e=None):
            picked = [
                key
                for (key, _), cb in zip(options, checkboxes)
                if bool(getattr(cb, "value", False))
            ]
            try:
                page.pop_dialog()
            except Exception:
                try:
                    dlg.open = False
                    page.update()
                except Exception:
                    pass

            async def _bg(_p=list(picked)):
                import asyncio as _ai

                with contextlib.suppress(Exception):
                    await _ai.to_thread(
                        self._save_home_stock_cost_display_title_keys, _p
                    )
                self.build()

            try:
                page.run_task(_bg)
            except Exception:
                with contextlib.suppress(Exception):
                    self._save_home_stock_cost_display_title_keys(picked)
                self.build()

        def _cancel(_e=None):
            try:
                page.pop_dialog()
            except Exception:
                try:
                    dlg.open = False
                    page.update()
                except Exception:
                    pass

        _dlg_title_stock = "Що показувати в середній собівартості складу"
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Text(_dlg_title_stock, expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=lambda _: self._show_display_selection_help(page, _dlg_title_stock)),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
            content=ft.Container(
                width=420,
                height=320,
                content=ft.Column(items, spacing=0, scroll=ft.ScrollMode.ADAPTIVE),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=_cancel),
                ft.FilledButton("Зберегти", on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    # ── Середня Собівартість З+С ──────────────────────────────────────────────

    def _home_blended_cost_display_state_key(self) -> str:
        return f"{self._table_layout_state_key()}:blended_cost_display"

    def _load_home_blended_cost_display_title_keys(self) -> list[str]:
        try:
            manager_id = str(self.manager_id or "").strip()
            if not manager_id:
                return []
            payload = self.db.get_manager_workspace_state(manager_id) or {}
            if not isinstance(payload, dict):
                return []
            store = (
                payload.get("home_blended_cost_display")
                if isinstance(payload.get("home_blended_cost_display"), dict)
                else {}
            )
            raw = store.get(self._home_blended_cost_display_state_key())
            if not isinstance(raw, list):
                return []
            return [str(x).strip().lower() for x in raw if str(x).strip()]
        except Exception:
            return []

    def _save_home_blended_cost_display_title_keys(self, keys: list[str]) -> None:
        try:
            manager_id = str(self.manager_id or "").strip()
            if not manager_id:
                return
            payload = self.db.get_manager_workspace_state(manager_id) or {}
            if not isinstance(payload, dict):
                payload = {}
            store = (
                payload.get("home_blended_cost_display")
                if isinstance(payload.get("home_blended_cost_display"), dict)
                else {}
            )
            store[self._home_blended_cost_display_state_key()] = [
                str(x).strip().lower() for x in (keys or []) if str(x).strip()
            ]
            payload["home_blended_cost_display"] = store
            self.db.save_manager_workspace_state(manager_id, payload)
        except Exception:
            pass

    def _home_blended_cost_display_title_options(self) -> list[tuple[str, str]]:
        try:
            categories = self._categories_in_scope() or []
        except Exception:
            categories = []
        options: list[tuple[str, str]] = [("name", "Назва")]
        seen = {"name", "category"}
        titled: list[tuple[int, str, str]] = []
        for cat in categories:
            if not isinstance(cat, dict):
                continue
            cfg = self.db.get_category_levels_config(cat) or {}
            levels = cfg.get("levels") if isinstance(cfg.get("levels"), list) else []
            for row in levels:
                if not isinstance(row, dict) or not bool(row.get("enabled")):
                    continue
                label = str(row.get("title") or "").strip()
                if not label:
                    continue
                key = label.lower()
                if key in seen:
                    continue
                seen.add(key)
                try:
                    pos = int(row.get("position") or 0)
                except Exception:
                    pos = 0
                titled.append((pos, label.lower(), key))
                options.append((key, label))
        tail = sorted(
            options[1:],
            key=lambda pair: (
                next((p for p, _, k in titled if k == pair[0]), 999),
                pair[1].lower(),
            ),
        )
        return [options[0], ("category", "Категорія")] + tail

    def _selected_home_blended_cost_display_title_keys(self) -> list[str]:
        options = self._home_blended_cost_display_title_options()
        available_keys = [
            str(k or "").strip().lower() for k, _ in options if str(k or "").strip()
        ]
        saved = [
            k
            for k in self._load_home_blended_cost_display_title_keys()
            if k in available_keys
        ]
        if saved:
            return saved
        return ["name"] if "name" in available_keys else []

    def _home_blended_cost_rows(
        self, orders: list[dict], selected_title_keys: list[str]
    ) -> list[tuple[str, str]]:
        """Average unit_cost_blended (avg of stock cost + order cost) per product group.

        unit_cost_blended per product = average of:
          - unit_cost_stock = amount/qty from stock inputs
          - avg order item unit_cost_usd across accepted orders for that product
        """
        keys = [
            str(k or "").strip().lower()
            for k in (selected_title_keys or [])
            if str(k or "").strip()
        ]
        if not keys:
            return [("Немає даних", "-")]

        selected_key_set = set(keys)
        # When called from _with_region_blocks, use the current split region;
        # otherwise fall back to the globally selected region.
        region_id = getattr(self, "_active_split_region_id", None)
        if region_id is None:
            region_id = self._resolve_region_id()

        # Load stock inputs
        try:
            if self.general_mode and self.general_manager_ids:
                stock_inputs: dict = {}
                for mid in self.general_manager_ids:
                    partial = (
                        self.db.get_cost_price_stock_inputs(
                            self.company_id, region_id=region_id, manager_id=mid
                        )
                        or {}
                    )
                    for pid, inp in (partial or {}).items():
                        if pid not in stock_inputs:
                            stock_inputs[pid] = inp
            else:
                stock_inputs = (
                    self.db.get_cost_price_stock_inputs(
                        self.company_id,
                        region_id=region_id,
                        manager_id=str(self.manager_id or "").strip(),
                    )
                    or {}
                )
        except Exception:
            stock_inputs = {}

        # Load order cost inputs (same as Замовлення widget)
        try:
            if self.general_mode and self.general_manager_ids:
                order_inputs: dict = {}
                for mid in self.general_manager_ids:
                    partial = (
                        self.db.get_cost_price_order_inputs(
                            self.company_id, region_id=region_id, manager_id=mid
                        )
                        or {}
                    )
                    order_inputs.update(partial)
            else:
                order_inputs = (
                    self.db.get_cost_price_order_inputs(
                        self.company_id,
                        region_id=region_id,
                        manager_id=str(self.manager_id or "").strip(),
                    )
                    or {}
                )
        except Exception:
            order_inputs = {}

        # Build per-product average order cost
        order_costs_by_product: dict[str, list[float]] = {}
        for order in orders or []:
            if not isinstance(order, dict):
                continue
            order_id = str(order.get("id") or "").strip()
            if not order_id:
                continue
            for idx, item in enumerate(order.get("items") or []):
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("product_id") or "").strip()
                if not pid:
                    continue
                input_key = f"{order_id}:{int(idx)}"
                _input_row = order_inputs.get(input_key) or {}
                cost = self._safe_float(_input_row.get("unit_cost_usd"), None)
                cost_ccy = str(_input_row.get("unit_cost_currency") or "").upper().strip()
                if cost is None:
                    cost = self._safe_float(item.get("unit_cost_calc_usd"), None)
                    cost_ccy = str(
                        item.get("unit_cost_calc_currency") or ""
                    ).upper().strip()
                if cost is not None and float(cost) >= 0:
                    # Each sample can be in a different native pivot currency
                    # now — normalize to USD so it blends correctly with
                    # unit_cost_stock (which carries no currency of its own).
                    cost_usd = fx_rates.convert(float(cost), cost_ccy or "USD", "USD")
                    if cost_usd is not None:
                        order_costs_by_product.setdefault(pid, []).append(cost_usd)

        try:
            all_products = self._products_in_scope() or []
        except Exception:
            all_products = []
        products_by_id = {}
        for prod in all_products:
            if not isinstance(prod, dict):
                continue
            pid = str(prod.get("id") or "").strip()
            if pid:
                products_by_id[pid] = prod

        try:
            categories = self._categories_in_scope() or []
        except Exception:
            categories = []
        cat_cfg_by_id: dict[str, dict] = {}
        for cat in categories or []:
            if not isinstance(cat, dict):
                continue
            cid = str(cat.get("id") or "").strip()
            if not cid:
                continue
            cat_cfg_by_id[cid] = self.db.get_category_levels_config(cat) or {}

        categories_by_id: dict[str, str] = {
            str(c.get("id") or "").strip(): str(c.get("name") or "").strip()
            for c in (categories or [])
            if isinstance(c, dict) and c.get("id")
        }

        try:
            all_nodes = self.db.get_company_catalog_nodes(self.company_id) or []
        except Exception:
            all_nodes = []
        nodes_by_id = {
            str(node.get("id") or "").strip(): node
            for node in all_nodes
            if isinstance(node, dict) and str(node.get("id") or "").strip()
        }

        def _node_names_by_position_for_pid(pid: str) -> dict[int, str]:
            prod = products_by_id.get(pid) or {}
            out: dict[int, str] = {}
            cur = str(prod.get("catalog_node_id") or "").strip()
            hops = 0
            while cur and hops < 40:
                row = nodes_by_id.get(cur)
                if not isinstance(row, dict):
                    break
                try:
                    pos = int(row.get("level_position") or 0)
                except Exception:
                    pos = 0
                if 1 <= pos <= 6 and pos not in out:
                    out[pos] = str(row.get("name") or "").strip()
                cur = str(row.get("parent_node_id") or "").strip()
                hops += 1
            for pos in range(1, 7):
                direct_id = str(prod.get(f"level_{pos}_node_id") or "").strip()
                if direct_id and pos not in out:
                    direct_row = nodes_by_id.get(direct_id)
                    if isinstance(direct_row, dict):
                        out[pos] = str(direct_row.get("name") or "").strip()
            return out

        label_by_key = {
            k: v for k, v in self._home_blended_cost_display_title_options()
        }
        buckets: dict[tuple[str, str], list[float]] = {}

        # Collect all product IDs from both sources
        all_pids = set(str(pid).strip() for pid in (stock_inputs or {}).keys())
        all_pids.update(order_costs_by_product.keys())

        for pid in all_pids:
            if not pid:
                continue
            # Compute unit_cost_stock
            inp = (stock_inputs or {}).get(pid)
            unit_cost_stock = None
            if isinstance(inp, dict):
                qty = self._safe_float(inp.get("qty"), None)
                amount = self._safe_float(inp.get("amount"), None)
                if qty is not None and qty > 0 and amount is not None:
                    unit_cost_stock = float(amount) / float(qty)

            # Compute avg order cost
            order_costs = order_costs_by_product.get(pid) or []
            avg_order_cost = (
                sum(order_costs) / max(1, len(order_costs)) if order_costs else None
            )

            # Blended = average of available costs
            candidates = [c for c in [unit_cost_stock, avg_order_cost] if c is not None]
            if not candidates:
                continue
            unit_cost_blended = sum(candidates) / len(candidates)

            prod = products_by_id.get(pid)
            if prod is None:
                # Archived/deleted product — get_products() excludes archived
                # by default. Skip it entirely (same as the pure-stock cost
                # card) rather than lumping unrelated archived products
                # together under one generic "Видалений товар" row that
                # used to still count toward the visible averages.
                continue

            if "name" in selected_key_set:
                display_name = str(prod.get("name") or "").strip()
                if display_name:
                    buckets.setdefault(("name", display_name), []).append(
                        unit_cost_blended
                    )

            cat_id = str(
                (prod.get("category_id") if isinstance(prod, dict) else "") or ""
            ).strip()
            if not cat_id:
                continue

            if "category" in selected_key_set:
                cat_name = categories_by_id.get(cat_id, "")
                if cat_name:
                    buckets.setdefault(("category", cat_name), []).append(
                        unit_cost_blended
                    )

            cfg = cat_cfg_by_id.get(cat_id) or {}
            levels = cfg.get("levels") if isinstance(cfg.get("levels"), list) else []
            node_names_by_pos = _node_names_by_position_for_pid(pid)
            for level in levels:
                if not isinstance(level, dict) or not bool(level.get("enabled")):
                    continue
                key = str(level.get("title") or "").strip().lower()
                if key not in selected_key_set or key in {"name", "category"}:
                    continue
                try:
                    pos = int(level.get("position") or 0)
                except Exception:
                    pos = 0
                node_name = str(node_names_by_pos.get(pos) or "").strip()
                if not node_name:
                    continue
                buckets.setdefault((key, node_name), []).append(unit_cost_blended)

        rows: list[tuple[str, str]] = []
        for key in keys:
            matches = [
                (name, vals)
                for (bucket_key, name), vals in buckets.items()
                if bucket_key == key and vals
            ]
            matches.sort(key=lambda pair: pair[0].lower())
            for name, vals in matches:
                avg = sum(float(v) for v in vals) / max(1, len(vals))
                label = (
                    name if len(keys) == 1 else f"{label_by_key.get(key, key)}: {name}"
                )
                rows.append((label, f"{avg:.2f}"))

        if not rows:
            rows.append(("Немає даних", "-"))
        return rows

    def _open_home_blended_cost_display_settings(self, _=None):
        page = getattr(self.content_area, "page", None)
        if not page:
            return
        options = self._home_blended_cost_display_title_options()
        selected = set(self._selected_home_blended_cost_display_title_keys())
        checkboxes: list[ft.Checkbox] = []

        def _mk_checkbox(key: str, label: str):
            cb = ft.Checkbox(value=(key in selected), label=label)
            checkboxes.append(cb)
            return cb

        items = [_mk_checkbox(key, label) for key, label in options]

        def _save(_e=None):
            picked = [
                key
                for (key, _), cb in zip(options, checkboxes)
                if bool(getattr(cb, "value", False))
            ]
            try:
                page.pop_dialog()
            except Exception:
                try:
                    dlg.open = False
                    page.update()
                except Exception:
                    pass

            async def _bg(_p=list(picked)):
                import asyncio as _ai

                with contextlib.suppress(Exception):
                    await _ai.to_thread(
                        self._save_home_blended_cost_display_title_keys, _p
                    )
                self.build()

            try:
                page.run_task(_bg)
            except Exception:
                with contextlib.suppress(Exception):
                    self._save_home_blended_cost_display_title_keys(picked)
                self.build()

        def _cancel(_e=None):
            try:
                page.pop_dialog()
            except Exception:
                try:
                    dlg.open = False
                    page.update()
                except Exception:
                    pass

        _dlg_title_blended = "Що показувати в середній собівартості З+С"
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Text(_dlg_title_blended, expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=lambda _: self._show_display_selection_help(page, _dlg_title_blended)),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
            content=ft.Container(
                width=420,
                height=320,
                content=ft.Column(items, spacing=0, scroll=ft.ScrollMode.ADAPTIVE),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=_cancel),
                ft.FilledButton("Зберегти", on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    def _kpi_card(
        self,
        title: str,
        count: int,
        meta: str,
        on_click,
        positive_is_good: bool = False,
        explicit_color: str | None = None,
    ):
        value = int(count or 0)
        if explicit_color is not None:
            count_color = explicit_color
        elif positive_is_good:
            count_color = _OK if value > 0 else _MUTED
        else:
            count_color = _ERR if value > 0 else _OK
        return ft.Container(
            width=self.KPI_CARD_SIZE,
            height=self.KPI_CARD_SIZE,
            border=ft.border.all(1, _BORDER),
            border_radius=10,
            bgcolor=_PANEL,
            padding=8,
            ink=True,
            on_click=on_click,
            content=ft.Column(
                [
                    ft.Text(
                        title,
                        size=self.KPI_LABEL_SIZE,
                        weight="bold",
                        color=_INK,
                        text_align=ft.TextAlign.CENTER,
                        no_wrap=False,
                        max_lines=2,
                    ),
                    ft.Text(
                        str(count),
                        size=self.KPI_COUNT_SIZE,
                        weight="bold",
                        color=count_color,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        meta,
                        size=self.KPI_META_SIZE,
                        color=_MUTED,
                        text_align=ft.TextAlign.CENTER,
                        no_wrap=False,
                    ),
                ],
                spacing=4,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
        )

    def _kpi_group_divider(self):
        return ft.Container(
            width=2,
            height=max(72, int(self.KPI_CARD_SIZE) - 16),
            bgcolor=_SKY_BG,
            border_radius=999,
            margin=ft.margin.symmetric(horizontal=2, vertical=8),
        )

    def _stats_list_card(
        self,
        title: str,
        rows,
        settings_on_click=None,
        badge_text: str | None = None,
        badge_color: str = _OK,
        name_to_pid: dict | None = None,
    ):
        from views.product_quick_view import product_icon_btn as _home_piqb
        list_controls = []
        for row in rows:
            if not isinstance(row, tuple) or len(row) < 2:
                continue
            label = row[0]
            value = row[1]

            # Region header block: ("__REGION__", "Назва регіону")
            if label == "__REGION__":
                list_controls.append(
                    ft.Container(
                        padding=ft.padding.only(top=6, bottom=2),
                        content=ft.Column(
                            [
                                ft.Text(
                                    str(value),
                                    size=10,
                                    weight=ft.FontWeight.W_700,
                                    italic=True,
                                    color=_WARN_T,
                                ),
                                ft.Divider(height=1, color=_WARN, thickness=1),
                            ],
                            spacing=2,
                            tight=True,
                        ),
                    )
                )
                continue

            on_click = row[2] if len(row) > 2 else None
            value_color_override = row[3] if len(row) > 3 else None
            if value_color_override:
                val_color = value_color_override
            elif callable(on_click):
                val_color = _ACCENT
            elif value == "-":
                val_color = _STONE
            else:
                val_color = _WARN_T
            _pid_for_label = (
                str((name_to_pid or {}).get(label) or "").strip()
                if name_to_pid
                else ""
            )
            _label_text = ft.Text(
                label,
                size=9,
                color=_STONE2,
                no_wrap=False,
                max_lines=4,
                overflow=ft.TextOverflow.ELLIPSIS,
                expand=True,
            )
            _label_inner = (
                ft.Row(
                    [
                        _label_text,
                        _home_piqb(_pid_for_label, self.content_area, self.db, self.user_data, size=11),
                    ],
                    spacing=2,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    tight=True,
                )
                if _pid_for_label
                else _label_text
            )
            list_controls.append(
                ft.Container(
                    bgcolor=_WARN_BG,
                    border_radius=10,
                    padding=ft.padding.symmetric(horizontal=8, vertical=6),
                    ink=callable(on_click),
                    on_click=on_click if callable(on_click) else None,
                    content=ft.Row(
                        [
                            ft.Container(
                                expand=True,
                                content=_label_inner,
                            ),
                            ft.Container(
                                width=55,
                                alignment=ft.Alignment(1, 0),
                                content=ft.Text(
                                    value,
                                    size=11,
                                    weight="bold",
                                    color=val_color,
                                    text_align=ft.TextAlign.RIGHT,
                                    no_wrap=True,
                                ),
                            ),
                        ],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )

        card_height = 380
        return ft.Container(
            width=240,
            height=card_height,
            bgcolor=_FDE68A,
            border=ft.border.all(1, _WARN),
            border_radius=12,
            padding=10,
            content=ft.Column(
                [
                    ft.Container(
                        bgcolor=_FED7AA,
                        border_radius=10,
                        padding=ft.padding.symmetric(horizontal=10, vertical=8),
                        content=ft.Row(
                            [
                                ft.Text(
                                    title,
                                    size=12,
                                    weight="bold",
                                    color=_ERR_DK,
                                    expand=True,
                                ),
                                *(
                                    [
                                        ft.Text(
                                            badge_text,
                                            size=11,
                                            weight="bold",
                                            color=badge_color,
                                        )
                                    ]
                                    if badge_text
                                    else []
                                ),
                                ft.IconButton(
                                    visible=callable(settings_on_click),
                                    icon=ft.Icons.SETTINGS,
                                    icon_size=16,
                                    tooltip="Налаштувати",
                                    on_click=settings_on_click,
                                    style=ft.ButtonStyle(
                                        padding=ft.padding.all(0),
                                        shape=ft.RoundedRectangleBorder(
                                            radius=8,
                                        ),
                                    ),
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ),
                    ft.Container(
                        expand=True,
                        content=ft.Column(
                            list_controls,
                            spacing=8,
                            scroll=ft.ScrollMode.ADAPTIVE,
                        ),
                    ),
                ],
                spacing=10,
            ),
        )

    def _kpi_card_dual_threshold(
        self,
        title: str,
        yellow_count: int,
        red_count: int,
        meta: str,
        on_click,
    ):
        yellow_color = _OK if int(yellow_count or 0) == 0 else _WARN2
        red_color = _OK if int(red_count or 0) == 0 else _ERR
        return ft.Container(
            width=self.KPI_CARD_SIZE,
            height=self.KPI_CARD_SIZE,
            border=ft.border.all(1, _BORDER),
            border_radius=10,
            bgcolor=_PANEL,
            padding=8,
            ink=True,
            on_click=on_click,
            content=ft.Column(
                [
                    ft.Text(
                        title,
                        size=self.KPI_LABEL_SIZE,
                        weight="bold",
                        text_align=ft.TextAlign.CENTER,
                        no_wrap=False,
                        max_lines=2,
                    ),
                    ft.Row(
                        [
                            ft.Text(
                                str(int(yellow_count or 0)),
                                size=self.KPI_COUNT_SIZE,
                                weight="bold",
                                color=yellow_color,
                            ),
                            ft.Text(
                                "/",
                                size=self.KPI_COUNT_SIZE,
                                weight="bold",
                                color=_MUTED2,
                            ),
                            ft.Text(
                                str(int(red_count or 0)),
                                size=self.KPI_COUNT_SIZE,
                                weight="bold",
                                color=red_color,
                            ),
                        ],
                        spacing=4,
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                    ft.Text(
                        meta,
                        size=self.KPI_META_SIZE,
                        color="grey700",
                        text_align=ft.TextAlign.CENTER,
                        no_wrap=False,
                    ),
                ],
                spacing=4,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
        )

    # ─────────────────────── "Загальні" mode helpers ─────────────────────────

    def _load_general_combined_snapshot(self) -> None:
        """Load orders for all selected managers and merge into a single snapshot."""
        mgr_name_by_id: dict[str, str] = {}
        for m in self.all_subordinates_flat:
            if isinstance(m, dict):
                mgr_name_by_id[str(m.get("id") or "")] = str(
                    m.get("full_name") or m.get("id") or ""
                )
        own_id = str(self.user_data.get("id") or "")
        mgr_name_by_id[own_id] = str(self.user_data.get("full_name") or "Я")

        region_id = self._resolve_region_id()

        # KEY FIX: save the pre-existing snapshot (own manager's cached data) before
        # overwriting _live_orders_snapshot.  Re-using it for own orders ensures the
        # general-mode count matches the individual "Мої дані" count — both use the
        # same data source (disk snapshot) instead of general using fresh DB which
        # may differ when DB was updated after the snapshot was saved.
        # Reuse the pre-existing snapshot for own (supervisor) orders.
        # Do NOT filter by region here — the snapshot was already loaded for the
        # correct region, and some orders may legitimately have region_id=None.
        # An empty own_cached_orders ([] not None) means no orders in snapshot →
        # fall back to DB load (same as for other managers).
        own_cached_orders: list | None = None
        if self._live_orders_snapshot is not None and own_id in (
            self.general_manager_ids or []
        ):
            own_cached_orders = [
                dict(o) for o in self._live_orders_snapshot if isinstance(o, dict)
            ]
            if not own_cached_orders:
                own_cached_orders = None  # empty snapshot → fall back to DB
            _home_logger.info(
                "[DIAG][general_snapshot] OWN orders from cache=%d",
                len(own_cached_orders or []),
            )

        combined: list[dict] = []
        seen_order_ids: set[str] = set()

        # get_orders() issues an identical unbounded company+region scan on every
        # call regardless of manager_id (manager_id only drives an in-Python
        # post-filter via _get_manager_visible_scope_context) — fetch the raw
        # rows once here and reuse them for every manager instead of re-running
        # the same full table scan once per manager (same fix as
        # _all_orders_unfiltered above).
        try:
            raw_company_orders = [
                o
                for o in (
                    self.db.get_orders(self.company_id, region_id=region_id) or []
                )
                if isinstance(o, dict)
            ]
        except Exception:
            raw_company_orders = []

        for mid in self.general_manager_ids:
            try:
                # For own orders: reuse cached snapshot so the count matches individual mode.
                if mid == own_id and own_cached_orders is not None:
                    mgr_orders = own_cached_orders
                    _home_logger.info(
                        "[DIAG][general_snapshot] mgr=%s REUSE CACHE %d orders",
                        mid,
                        len(mgr_orders),
                    )
                else:
                    try:
                        visible_ctx = self.db._get_manager_visible_scope_context(
                            self.company_id, mid, region_id or None
                        )
                    except Exception:
                        visible_ctx = {}
                    if visible_ctx.get("restricted"):
                        allowed_ids = visible_ctx.get("order_ids") or set()
                        mgr_orders = [
                            o
                            for o in raw_company_orders
                            if str(o.get("id") or "").strip() in allowed_ids
                        ]
                    else:
                        mgr_orders = list(raw_company_orders)
                    _home_logger.info(
                        "[DIAG][general_snapshot] mgr=%s region=%s raw=%d",
                        mid,
                        region_id,
                        len(mgr_orders),
                    )

                mgr_name = mgr_name_by_id.get(mid, mid)
                for o in mgr_orders:
                    oid = str(o.get("id") or "").strip()
                    if oid and oid in seen_order_ids:
                        continue
                    if oid:
                        seen_order_ids.add(oid)
                    o["_gm_manager_id"] = mid
                    o["_gm_manager_name"] = mgr_name
                    combined.append(o)
            except Exception:
                _home_logger.exception(
                    "[home_general] failed loading orders for mgr=%s", mid
                )

        _home_logger.info(
            "[DIAG][general_snapshot] COMBINED total=%d managers=%s region=%s",
            len(combined),
            list(self.general_manager_ids),
            region_id,
        )
        self._live_orders_snapshot = combined

    def _build_general_selector_panel(self) -> ft.Container:
        """Compact inline checkbox panel for choosing participants in 'Загальні' mode."""
        own_id = str(self.user_data.get("id") or "")
        own_name = str(self.user_data.get("full_name") or "Я")

        entries: list[dict] = []
        if own_id:
            entries.append({"id": own_id, "name": own_name or "Я"})
        for m in self.all_subordinates_flat:
            if not isinstance(m, dict):
                continue
            entries.append(
                {
                    "id": str(m.get("id") or ""),
                    "name": str(m.get("full_name") or m.get("id") or ""),
                }
            )

        selected_set: set[str] = set(self.general_manager_ids)
        checkboxes: list[ft.Checkbox] = []
        for entry in entries:
            cb = ft.Checkbox(
                label=entry["name"],
                value=entry["id"] in selected_set,
                data=entry["id"],
                label_style=ft.TextStyle(size=11),
            )
            checkboxes.append(cb)

        def _on_apply(_e=None):
            new_ids = [cb.data for cb in checkboxes if cb.value]
            if not new_ids:
                return
            self.general_manager_ids = new_ids
            if callable(self.on_general_selection_changed):
                self.on_general_selection_changed(new_ids)

        def _select_all(_e=None):
            for cb in checkboxes:
                cb.value = True
                with __import__("contextlib").suppress(Exception):
                    cb.update()

        def _clear_all(_e=None):
            for cb in checkboxes:
                cb.value = False
                with __import__("contextlib").suppress(Exception):
                    cb.update()

        from views.mobile_layout_helpers import (is_mobile,
                                                 make_scrollable_chip_row)

        if is_mobile(self.content_area.page):
            # A wrap=True row of one checkbox per subordinate can grow to
            # several lines on a narrow phone screen, pushing the KPI grid
            # far down. Keep it a single horizontally-scrollable line instead.
            cb_row = make_scrollable_chip_row([cb for cb in checkboxes], spacing=4)
        else:
            cb_row = ft.Row(
                [cb for cb in checkboxes],
                spacing=4,
                wrap=True,
            )

        return ft.Container(
            bgcolor=_SOFT,
            border=ft.border.all(1, "#BFDBFE"),
            border_radius=6,
            padding=ft.padding.symmetric(horizontal=10, vertical=6),
            content=ft.Row(
                [
                    ft.Text("Учасники:", size=10, weight="bold", color=_ACCENT),
                    cb_row,
                    ft.Row(
                        [
                            ft.TextButton(
                                "Всі",
                                on_click=_select_all,
                                style=ft.ButtonStyle(
                                    padding=ft.padding.symmetric(
                                        horizontal=6, vertical=0
                                    )
                                ),
                            ),
                            ft.TextButton(
                                "Жодного",
                                on_click=_clear_all,
                                style=ft.ButtonStyle(
                                    padding=ft.padding.symmetric(
                                        horizontal=6, vertical=0
                                    )
                                ),
                            ),
                            ft.FilledButton(
                                "Застосувати",
                                on_click=_on_apply,
                                style=ft.ButtonStyle(
                                    padding=ft.padding.symmetric(
                                        horizontal=12, vertical=4
                                    ),
                                ),
                            ),
                        ],
                        spacing=2,
                        tight=True,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                wrap=False,
            ),
        )

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

    # ─────────────────────────────────────────────────────────────────────────

    def _build_sync(self):
        if not self.content_area or not getattr(self.content_area, "page", None):
            return

        self._header_loading_indicator = create_orders_header_loading_indicator(
            visible=True
        )

        self.content_area.controls.clear()
        self.content_area.controls.append(
            ft.Container(
                content=ft.Row(
                    [
                        ft.Text(self.t("home.title", "Головна"), size=self.HEADER_SIZE, weight="bold"),
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
                padding=ft.padding.only(bottom=6),
                border=ft.border.only(bottom=ft.BorderSide(1, _HDR)),
            )
        )
        self.content_area.update()

        # In general mode: pre-load combined orders from all selected managers
        # so that all _count_* functions that call _orders_in_scope() use combined data.
        if self.general_mode and self.general_manager_ids:
            self._set_loading_text("Дані...")
            self._load_general_combined_snapshot()

        def _safe_value(loader, default):
            try:
                return loader()
            except Exception:
                return default

        self._set_loading_text("Замовлення...")
        need_to_order_count = _safe_value(self._count_need_to_order, 0)
        overdue_count = _safe_value(self._count_overdue, 0)
        orders_postprod_not_submitted_count = _safe_value(
            self._count_orders_postprod_not_submitted, 0
        )
        cp_orders_missing_logistics_fact_count = _safe_value(
            self._count_cp_orders_missing_logistics_fact, 0
        )
        orders_missing_status_date_count = _safe_value(
            self._count_orders_missing_status_date, 0
        )
        self._set_loading_text("Борги...")
        supplier_debt_count, supplier_credit_count = _safe_value(self._count_supplier_debt, (0, 0))
        pending_series_to_product_count = _safe_value(
            self._count_pending_series_to_product, 0
        )
        self._set_loading_text("Пакування...")
        packaging_supplier_debt_count = _safe_value(
            self._count_packaging_supplier_debt, 0
        )
        packaging_low_yellow_count, packaging_low_red_count = _safe_value(
            self._count_packaging_low_stock, (0, 0)
        )
        self._set_loading_text("Медіаплан...")
        media_plan_yellow_count, media_plan_red_count = _safe_value(
            self._count_media_plan_stale, (0, 0)
        )
        media_plan_schedule_days, media_plan_schedule_has_plan = _safe_value(
            self._count_media_plan_schedule_end, (None, False)
        )
        self._set_loading_text("Категорії...")
        categories_missing_target_margin_count = _safe_value(
            self._count_categories_missing_target_margin, 0
        )
        self._set_loading_text("Товари...")
        incomplete_products_count = _safe_value(self._count_incomplete_products, 0)
        incomplete_suppliers_count = _safe_value(self._count_incomplete_suppliers, 0)
        orders_missing_fact_weight_count = _safe_value(
            self._count_orders_missing_fact_weight, 0
        )
        total_suppliers_count = _safe_value(self._count_total_suppliers, 0)
        total_categories_count = _safe_value(self._count_total_categories, 0)
        total_products_count = _safe_value(self._count_total_products, 0)
        total_packaging_count = _safe_value(self._count_total_packaging, 0)

        _mode = (
            "GENERAL managers=%s" % self.general_manager_ids
            if self.general_mode
            else "SINGLE mgr=%s" % self.manager_id
        )
        _home_logger.info(
            "[DIAG][counters] %s | overdue=%d need_order=%d logistics=%d status_date=%d postprod=%d "
            "fact_weight=%d products=%d series=%d margin=%d debt=%d credit=%d pack_low=%d/%d pack_debt=%d suppliers_incomplete=%d "
            "| sup=%d cat=%d prod=%d pack=%d",
            _mode,
            overdue_count,
            need_to_order_count,
            cp_orders_missing_logistics_fact_count,
            orders_missing_status_date_count,
            orders_postprod_not_submitted_count,
            orders_missing_fact_weight_count,
            incomplete_products_count,
            pending_series_to_product_count,
            categories_missing_target_margin_count,
            supplier_debt_count,
            supplier_credit_count,
            packaging_low_yellow_count,
            packaging_low_red_count,
            packaging_supplier_debt_count,
            incomplete_suppliers_count,
            total_suppliers_count,
            total_categories_count,
            total_products_count,
            total_packaging_count,
        )
        scoped_orders = _safe_value(self._all_orders_unfiltered, [])
        _tbl_dist: dict = {}
        for _o in scoped_orders or []:
            _k = str(_o.get("manager_id") or "?")[:12]
            _tbl_dist[_k] = _tbl_dist.get(_k, 0) + 1
        _home_logger.info(
            "[DIAG][tables] general=%s scoped_orders=%d mgr_dist=%s",
            self.general_mode,
            len(scoped_orders),
            _tbl_dist,
        )
        delivery_average_rows = _safe_value(
            lambda: self._with_region_blocks(
                scoped_orders, self._accepted_delivery_average_rows
            ),
            [],
        )
        delivery_tariff_average_rows = _safe_value(
            lambda: self._with_region_blocks(
                scoped_orders, self._average_delivery_tariff_rows
            ),
            [],
        )
        shoulder_average_rows = _safe_value(
            lambda: self._with_region_blocks(
                scoped_orders, self._accepted_shoulder_average_rows
            ),
            [],
        )
        term_summary_rows = _safe_value(
            lambda: self._with_region_blocks(scoped_orders, self._term_summary_rows), []
        )
        home_term_display_keys = _safe_value(
            self._selected_home_term_display_title_keys, []
        )
        home_term_display_rows = _safe_value(
            lambda: self._with_region_blocks(
                scoped_orders, self._home_term_production_rows, home_term_display_keys
            ),
            [],
        )
        home_cost_display_keys = _safe_value(
            self._selected_home_cost_display_title_keys, []
        )
        average_cost_summary_rows = _safe_value(
            lambda: self._with_region_blocks(
                scoped_orders, self._home_cost_production_rows, home_cost_display_keys
            ),
            [],
        )
        home_fact_weight_display_keys = _safe_value(
            self._selected_home_fact_weight_display_title_keys, []
        )
        home_fact_weight_display_rows = _safe_value(
            lambda: self._with_region_blocks(
                scoped_orders, self._home_fact_weight_rows, home_fact_weight_display_keys
            ),
            [],
        )
        home_shoulder_display_keys = _safe_value(
            self._selected_home_shoulder_display_title_keys, []
        )
        home_shoulder_display_rows = _safe_value(
            lambda: self._with_region_blocks(
                scoped_orders,
                self._home_shoulder_product_rows,
                home_shoulder_display_keys,
            ),
            [],
        )
        stock_freshness_badge = _safe_value(
            self._home_stock_freshness_badge, ("—", _STONE)
        )
        home_stock_cost_keys = _safe_value(
            self._selected_home_stock_cost_display_title_keys, []
        )
        home_stock_cost_rows = _safe_value(
            lambda: self._home_stock_cost_rows(home_stock_cost_keys), []
        )
        home_blended_cost_keys = _safe_value(
            self._selected_home_blended_cost_display_title_keys, []
        )
        home_blended_cost_rows = _safe_value(
            lambda: self._with_region_blocks(
                scoped_orders, self._home_blended_cost_rows, home_blended_cost_keys
            ),
            [],
        )

        def _open_need_to_order(_):
            if callable(self.on_open_need_to_order):
                self.on_open_need_to_order(None)

        def _open_overdue(_):
            if callable(self.on_open_overdue):
                self.on_open_overdue(None)

        def _open_orders_postprod_not_submitted(_):
            if callable(self.on_open_orders_postprod_not_submitted):
                self.on_open_orders_postprod_not_submitted(None)

        def _open_cp_orders_missing_logistics_fact(_):
            if callable(self.on_open_cp_orders_missing_logistics_fact):
                self.on_open_cp_orders_missing_logistics_fact(None)

        def _open_orders_missing_status_date(_):
            if callable(self.on_open_orders_missing_status_date):
                self.on_open_orders_missing_status_date(None)

        def _open_supplier_debt(_):
            if callable(self.on_open_supplier_debt):
                self.on_open_supplier_debt(None)

        def _open_pending_series_to_product(_):
            if callable(self.on_open_pending_series_to_product):
                self.on_open_pending_series_to_product(None)

        def _open_packaging_supplier_debt(_):
            if callable(self.on_open_packaging_supplier_debt):
                self.on_open_packaging_supplier_debt(None)

        def _open_packaging_low_stock(_):
            if callable(self.on_open_packaging_low_stock):
                self.on_open_packaging_low_stock(None)

        def _open_media_plan_stale(_):
            if callable(self.on_open_media_plan_stale):
                self.on_open_media_plan_stale(None)

        def _open_media_plan_schedule(_):
            if callable(self.on_open_media_plan_schedule):
                self.on_open_media_plan_schedule(None)

        def _open_categories_missing_target_margin(_):
            if callable(self.on_open_categories_missing_target_margin):
                self.on_open_categories_missing_target_margin(None)

        def _open_incomplete_products(_):
            if callable(self.on_open_incomplete_products):
                self.on_open_incomplete_products(None)

        def _open_incomplete_suppliers(_):
            if callable(self.on_open_incomplete_suppliers):
                self.on_open_incomplete_suppliers(None)

        def _open_orders_missing_fact_weight(_):
            if callable(self.on_open_orders_missing_fact_weight):
                self.on_open_orders_missing_fact_weight(None)

        def _open_all_suppliers(_):
            if callable(self.on_open_all_suppliers):
                self.on_open_all_suppliers(None)

        def _open_all_categories(_):
            if callable(self.on_open_all_categories):
                self.on_open_all_categories(None)

        def _open_all_products(_):
            if callable(self.on_open_all_products):
                self.on_open_all_products(None)

        def _open_all_packaging(_):
            if callable(self.on_open_all_packaging):
                self.on_open_all_packaging(None)

        kpi_groups = [
            [
                self._kpi_card(
                    "Не замовлених",
                    overdue_count,
                    "протермінованих задач",
                    _open_overdue,
                ),
                self._kpi_card(
                    "Треба Замовити",
                    need_to_order_count,
                    "активних",
                    _open_need_to_order,
                ),
                self._kpi_card(
                    "В дорозі/склад/прийняті",
                    cp_orders_missing_logistics_fact_count,
                    "без логістика факт",
                    _open_cp_orders_missing_logistics_fact,
                ),
                self._kpi_card(
                    "Дата статусу",
                    orders_missing_status_date_count,
                    "відсутня або майбутня",
                    _open_orders_missing_status_date,
                ),
                self._kpi_card(
                    "Не внесених в облік",
                    orders_postprod_not_submitted_count,
                    "після виробництва",
                    _open_orders_postprod_not_submitted,
                ),
                self._kpi_card(
                    "Замовлень",
                    orders_missing_fact_weight_count,
                    "без факт вага для розп.",
                    _open_orders_missing_fact_weight,
                ),
            ],
            [
                self._kpi_card(
                    "Товарів",
                    incomplete_products_count,
                    "без даних",
                    _open_incomplete_products,
                ),
                self._kpi_card(
                    "Серій не дороблених",
                    pending_series_to_product_count,
                    "в товар",
                    _open_pending_series_to_product,
                ),
                self._kpi_card(
                    "Категорій",
                    categories_missing_target_margin_count,
                    "без цільової рент.",
                    _open_categories_missing_target_margin,
                ),
            ],
            [
                self._kpi_card(
                    "Постачальників товару",
                    supplier_credit_count if supplier_credit_count > 0 and supplier_debt_count == 0 else supplier_debt_count,
                    "Борг (пост. винен)" if supplier_credit_count > 0 and supplier_debt_count == 0 else "Борг (ми винні)",
                    _open_supplier_debt,
                    explicit_color=(
                        _OK if (supplier_debt_count == 0 and supplier_credit_count == 0)
                        else _WARN if (supplier_credit_count > 0 and supplier_debt_count == 0)
                        else _ERR
                    ),
                ),
                *(
                    [
                        self._kpi_card_dual_threshold(
                            "Залишок Пакування шт.",
                            packaging_low_yellow_count,
                            packaging_low_red_count,
                            "<5к/<2.5к",
                            _open_packaging_low_stock,
                        )
                    ]
                    if self._feat_on("pack_stock")
                    else []
                ),
                *(
                    [
                        self._kpi_card(
                            "Постачальників пакування",
                            packaging_supplier_debt_count,
                            "Борг (ми винні)",
                            _open_packaging_supplier_debt,
                            positive_is_good=True,
                        )
                    ]
                    if self._feat_on("packaging")
                    else []
                ),
                self._kpi_card(
                    "Постачальників",
                    incomplete_suppliers_count,
                    "без даних",
                    _open_incomplete_suppliers,
                ),
            ],
            *(
                [
                    [
                        self._kpi_card_dual_threshold(
                            "Медіа-план",
                            media_plan_yellow_count,
                            media_plan_red_count,
                            "14-30дн./30+дн.",
                            _open_media_plan_stale,
                        ),
                        self._kpi_card_schedule_end(
                            media_plan_schedule_days,
                            media_plan_schedule_has_plan,
                            _open_media_plan_schedule,
                        ),
                    ]
                ]
                if self._feat_on("media_plan")
                else []
            ),
            [
                self._kpi_card(
                    "Постачальників",
                    total_suppliers_count,
                    "всього",
                    _open_all_suppliers,
                    positive_is_good=True,
                ),
                self._kpi_card(
                    "Категорій",
                    total_categories_count,
                    "всього",
                    _open_all_categories,
                    positive_is_good=True,
                ),
                self._kpi_card(
                    "Товарів",
                    total_products_count,
                    "всього",
                    _open_all_products,
                    positive_is_good=True,
                ),
                *(
                    [
                        self._kpi_card(
                            "Пакувань",
                            total_packaging_count,
                            "всього",
                            _open_all_packaging,
                            positive_is_good=True,
                        )
                    ]
                    if self._feat_on("packaging")
                    else []
                ),
                self._kpi_card(
                    "Замовлень",
                    self._count_all_orders_for_kpi(),
                    "всього",
                    None,
                    positive_is_good=True,
                ),
            ],
        ]
        gap = 10
        viewport_w = max(320, self._home_viewport_width())
        all_kpi_tiles = []
        for group_index, group in enumerate(kpi_groups):
            if group_index > 0:
                all_kpi_tiles.append(self._kpi_group_divider())
            all_kpi_tiles.extend(group)

        kpi_row_host = ft.Container(
            width=viewport_w,
            content=ft.Row(
                all_kpi_tiles,
                spacing=gap,
                run_spacing=gap,
                wrap=True,
                alignment=ft.MainAxisAlignment.START,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
        )
        self._kpi_row_host = kpi_row_host

        _home_name_to_pid: dict[str, str] = {}
        try:
            for _hp in (self._products_in_scope() or []):
                if not isinstance(_hp, dict):
                    continue
                _hpid = str(_hp.get("id") or "").strip()
                _hpname = str(_hp.get("name") or "").strip()
                if _hpid and _hpname:
                    _home_name_to_pid.setdefault(_hpname, _hpid)
        except Exception:
            pass

        stats_cards = [
            self._stats_list_card("Середній час доставки:", delivery_average_rows),
            self._stats_list_card(
                "Середній тариф доставки:", delivery_tariff_average_rows
            ),
            self._stats_list_card("Середнє плече:", shoulder_average_rows),
            self._stats_list_card("Середні терміни:", term_summary_rows),
            self._stats_list_card(
                "Середній термін виробництва",
                home_term_display_rows,
                settings_on_click=self._open_home_term_display_settings,
                name_to_pid=_home_name_to_pid,
            ),
            self._stats_list_card(
                "Середня фактична вага",
                home_fact_weight_display_rows,
                settings_on_click=self._open_home_fact_weight_display_settings,
                name_to_pid=_home_name_to_pid,
            ),
            self._stats_list_card(
                "Середнє плече",
                home_shoulder_display_rows,
                settings_on_click=self._open_home_shoulder_display_settings,
                name_to_pid=_home_name_to_pid,
            ),
            self._stats_list_card(
                "Середня Собівартість Замовлення",
                average_cost_summary_rows,
                settings_on_click=self._open_home_cost_display_settings,
                name_to_pid=_home_name_to_pid,
            ),
            self._stats_list_card(
                "Середня Собівартість Склад",
                home_stock_cost_rows,
                settings_on_click=self._open_home_stock_cost_display_settings,
                badge_text=stock_freshness_badge[0],
                badge_color=stock_freshness_badge[1],
                name_to_pid=_home_name_to_pid,
            ),
            self._stats_list_card(
                "Середня Собівартість З+С",
                home_blended_cost_rows,
                settings_on_click=self._open_home_blended_cost_display_settings,
                name_to_pid=_home_name_to_pid,
            ),
            *(
                [_pack_stock_card]
                if (
                    _pack_stock_card := _safe_value(
                        self._build_packaging_stock_card, None
                    )
                )
                is not None
                else []
            ),
        ]
        stats_gap = 12

        stats_lists_host = ft.Container(
            width=viewport_w,
            content=ft.Row(
                stats_cards,
                spacing=stats_gap,
                run_spacing=stats_gap,
                wrap=True,
                alignment=ft.MainAxisAlignment.START,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
        )
        self._stats_lists_host = stats_lists_host

        self.content_area.controls.clear()
        if self._header_loading_indicator is not None:
            self._header_loading_indicator.visible = False
        self.content_area.controls.append(
            ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text(self.t("home.title", "Головна"), size=self.HEADER_SIZE, weight="bold"),
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
                    ft.Text(
                        "Лічильники задач для дій. Натисніть картку, щоб перейти у відповідний розділ.",
                        size=self.META_TEXT_SIZE,
                        color="grey700",
                    ),
                    *(
                        [
                            ft.Divider(height=8, color="transparent"),
                            self._build_general_selector_panel(),
                        ]
                        if self.general_mode
                        else []
                    ),
                    ft.Divider(height=6, color="transparent"),
                    kpi_row_host,
                    ft.Divider(height=10, color="transparent"),
                    stats_lists_host,
                ],
                spacing=0,
                expand=True,
            )
        )

        self._build_completed_ts = time.monotonic()
        # Save sync-scope pulses so resume() can skip a full rebuild when
        # nothing relevant to Home changed. Covers every table Home reads
        # from (see HOME_PULSE_SCOPES) — a single cheap query against the
        # small public.sync_pulses table in the general DB.
        try:
            self._home_scope_pulses = (
                self.db.get_companies_sync_pulses(
                    [self.company_id], HOME_PULSE_SCOPES
                ).get(self.company_id)
                or {}
            )
        except Exception:
            self._home_scope_pulses = None

        def _on_home_resized(_=None):
            try:
                pg = self.content_area.page
                if not pg:
                    return
                active = str(getattr(pg, "_active_view", "") or "").strip()
                if active and active != "show_home":
                    return
                now_ts = datetime.now().timestamp()
                if (now_ts - float(self._last_home_resize_rebuild_ts or 0.0)) < 0.35:
                    return
                self._last_home_resize_rebuild_ts = now_ts
                self.build()
                pg.update()
            except Exception:
                pass

        try:
            pg = self.content_area.page
            if hasattr(pg, "on_resized"):
                pg.on_resized = _on_home_resized
            elif hasattr(pg, "on_resize"):
                pg.on_resize = _on_home_resized
        except Exception:
            pass

    def build(self, silent: bool = False):
        _th = _th_from_view(self)
        global _OK, _ERR, _WARN, _WARN_T, _ACCENT, _STONE, _STONE2, _INK
        global _BORDER, _PANEL, _WARN_BG, _HDR, _MUTED, _MUTED2, _OK_T
        global _SKY_BG, _FDE68A, _FED7AA, _ERR_DK, _WARN2, _SOFT
        _OK = _th["ok"]
        _ERR = _th["err"]
        _WARN = _th["warn"]
        _WARN_T = _th["warn"]
        _ACCENT = _th["icon_color"]
        _STONE = _STONE2 = _INK = _th["muted"]
        _BORDER = _th["border"]
        _PANEL = _th["panel"]
        _WARN_BG = _th["warn_bg"]
        _HDR = _th["hdr"]
        _MUTED = _MUTED2 = _th["muted"]
        _OK_T = _th["ok"]
        _SKY_BG = _th["chip_bg"]
        _FDE68A = _FED7AA = _th["bar"]
        _ERR_DK = _th["warn"]
        _WARN2 = _th["warn"]
        _SOFT = _th["soft"]
        if not self.content_area or not getattr(self.content_area, "page", None):
            return

        def _guarded_build_sync():
            self._prefetch_cache = {}
            self._live_orders_snapshot = None
            try:
                self._build_sync()
                self._force_fresh_from_db = False  # clear after successful build
            except Exception as _first_exc:
                _home_logger.exception("_build_sync first attempt failed: %s", _first_exc)
                # Retry once — transient DB/data errors sometimes resolve on second call.
                try:
                    self._prefetch_cache = {}
                    self._build_sync()
                except Exception as _second_exc:
                    _home_logger.exception("_build_sync second attempt failed: %s", _second_exc)
                    # Show error so the page isn't left as a blank white screen.
                    try:
                        if self._header_loading_indicator is not None:
                            self._header_loading_indicator.visible = False
                        if self.content_area:
                            self.content_area.controls.clear()
                            self.content_area.controls.append(
                                ft.Column(
                                    [
                                        ft.Icon(
                                            ft.Icons.WARNING_AMBER_ROUNDED,
                                            size=30,
                                            color="#EF4444",
                                        ),
                                        ft.Text(
                                            "Помилка завантаження Головної",
                                            size=14,
                                            weight="bold",
                                            color="#EF4444",
                                        ),
                                        ft.Text(
                                            str(_second_exc)[:400],
                                            size=11,
                                            color="grey600",
                                        ),
                                        ft.ElevatedButton(
                                            "Повторити",
                                            on_click=lambda _e: self.build(),
                                        ),
                                    ],
                                    spacing=8,
                                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                )
                            )
                            self.content_area.update()
                    except Exception:
                        pass
            finally:
                self._prefetch_cache = None
                self._force_fresh_from_db = False  # always clear flag after build

        run_async_build(
            self,
            page=self.content_area.page,
            build_sync=_guarded_build_sync,
            # silent=True (background live-sync refresh): skip the stub that
            # clears content_area to a spinner — _guarded_build_sync still
            # rebuilds fresh from DB in a thread, and the finished content
            # swaps in with a single update() once ready, so the KPI grid
            # never blinks empty while someone is looking at it.
            loading_stub_sync=None if silent else self._render_loading_stub,
        )

    def _render_loading_stub(self):
        if not self.content_area:
            return
        self._header_loading_indicator = create_orders_header_loading_indicator(
            visible=True
        )
        self.content_area.controls.clear()
        self.content_area.controls.append(
            ft.Container(
                content=ft.Row(
                    [
                        ft.Text(self.t("home.title", "Головна"), size=self.HEADER_SIZE, weight="bold"),
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
                padding=ft.padding.only(bottom=6),
                border=ft.border.only(bottom=ft.BorderSide(1, _HDR)),
            )
        )
        try:
            self.content_area.update()
        except Exception:
            pass
