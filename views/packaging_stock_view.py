"""
Склад пакування — журнал замовлень у постачальника пакування в розрізі товарних постачальників
(товари з модуля «Товари», які посилаються на запис пакування).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import urllib.request
from collections import defaultdict
from datetime import date, datetime
from typing import Any

import flet as ft

import i18n
from logger import logger
from views import product_naming
from views.async_view_build_helper import \
    create_orders_header_loading_indicator
from views.mobile_layout_helpers import is_mobile, make_scrollable_chip_row
from views.number_format import (format_grouped, format_maybe_numeric_text,
                                 row_text_matches, safe_float)
from views.orders_view import _wrap_text_for_width

# ---------------------------------------------------------------------------
# Theme colour shims — module defaults, shadowed by live theme at render time
# ---------------------------------------------------------------------------
_TEXT = "#111827"
_ERR = "#DC2626"
_HDR = "#E2E8F0"
_PANEL = "#FFFFFF"
_ALT = "#F8FAFC"
_ALT2 = "#FAFAFA"
_STONE = "#44403C"
_BORDER = "#CBD5E1"
_ACCENT = "#1D4ED8"
_SOFT_I = "#EEF2FF"
_INDIGO = "#4338CA"
_SKY = "#2563EB"
_OK_BG = "#DCFCE7"
_OK = "#059669"
_CHIP_T = "#1E3A8A"
_TEAL = "#0D9488"

# Consecutive-failure limit before a stuck outbox op is dropped instead of
# retried forever (see _ops_outbox_fail_counts in __init__).
_OPS_OUTBOX_MAX_RETRIES = 5
_SOFT_V = "#F5F3FF"
_TEXT2 = "#334155"
_SKY_BG = "#E0F2FE"
_ERR_DK = "#B91C1C"
_WARN_BG = "#FEF3C7"
_WARN = "#B45309"


def _th_from_view(view) -> dict:
    from views.themes import get_active_theme

    return get_active_theme(str(getattr(view, "manager_id", "") or ""))


# Поля журналу supply, де значення трактуємо як число (пробіл/кома → крапка) перед записом у БД.
_PACKAGING_SUPPLY_NUMERIC_PAYLOAD_FIELDS = frozenset(
    {"qty_ordered", "cost_amount", "paid_amount", "shipped_goods_supplier_qty"}
)


def _safe_float(raw: Any, default: float | None = None) -> float | None:
    return safe_float(raw, default)


def _safe_int_seq(raw: Any, default: int = 0) -> int:
    """seq_no з БД/psycopg: безпечне ціле для сортування та відображення."""
    x = _safe_float(raw, None)
    if x is None:
        return default
    try:
        return int(x)
    except Exception:
        return default


def _fmt_money(v: Any) -> str:
    return format_grouped(v, decimals=2, trim=True)


def _fmt_qty(v: Any) -> str:
    return format_grouped(v)


def _date_to_iso(d: Any) -> str:
    if d is None:
        return ""
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    s = str(d).strip()
    if not s:
        return ""
    if "T" in s:
        return s.split("T", 1)[0][:10]
    return s[:10]


def _datepicker_value_to_iso_date(picked) -> str:
    if picked is None:
        return ""
    try:
        if isinstance(picked, datetime):
            dt = picked
            if dt.tzinfo is not None:
                try:
                    dt = dt.astimezone()
                except Exception:
                    pass
            return dt.date().isoformat()
        if isinstance(picked, date):
            return picked.isoformat()
        if hasattr(picked, "date"):
            d = picked.date()
            if isinstance(d, date):
                return d.isoformat()
    except Exception:
        pass

    raw = str(picked or "").strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.date().isoformat()
    except Exception:
        return raw[:10]


def _date_picker_event_value(event: Any) -> Any:
    """Дата підтвердження: ``Event.data`` або ``control.value`` (bool у data — службові події діалога, не дата)."""
    raw = getattr(event, "data", None)
    if isinstance(raw, bool):
        ctl = getattr(event, "control", None)
        return getattr(ctl, "value", None) if ctl is not None else None
    if raw is not None:
        return raw
    ctl = getattr(event, "control", None)
    return getattr(ctl, "value", None) if ctl is not None else None


def _opening_ledger_from_row(o: dict | None) -> dict[str, float]:
    """Нормалізує початковий знімок в розрахунковий ledger для журналу."""
    d = o if isinstance(o, dict) else {}
    raw_pack = float(_safe_float(d.get("stock_packaging_supplier_qty"), 0) or 0)
    raw_goods = float(_safe_float(d.get("stock_goods_supplier_qty"), 0) or 0)
    bal_gross = float(_safe_float(d.get("balance_amount"), 0) or 0)

    ship = float(_safe_float(d.get("opening_shipped_goods_qty"), 0) or 0)
    if ship < 0:
        ship = 0.0
    if ship > raw_pack:
        ship = float(raw_pack)

    paid = float(_safe_float(d.get("balance_paid_amount"), 0) or 0)
    if paid < 0:
        paid = 0.0

    eff_pack = float(raw_pack) - float(ship)
    eff_goods = float(raw_goods) + float(ship)
    bal_net = float(bal_gross) - float(paid)
    return {
        "raw_pack": float(raw_pack),
        "raw_goods": float(raw_goods),
        "bal_gross": float(bal_gross),
        "ship": float(ship),
        "paid": float(paid),
        "eff_pack": float(eff_pack),
        "eff_goods": float(eff_goods),
        "bal_net": float(bal_net),
    }


def _opening_journal_has_snapshot(oph: dict | None) -> bool:
    """Той самий критерій, що в таблиці журналу: чи задано початковий знімок (рядок «Початкові залишки»)."""
    d = oph if isinstance(oph, dict) else {}
    leg = _opening_ledger_from_row(d)
    return (
        abs(float(leg.get("raw_pack") or 0)) > 1e-12
        or abs(float(leg.get("raw_goods") or 0)) > 1e-12
        or abs(float(leg.get("bal_gross") or 0)) > 1e-12
        or abs(float(leg.get("ship") or 0)) > 1e-12
        or abs(float(leg.get("paid") or 0)) > 1e-12
    )


def _packaging_supply_chrono_sort_key(rr: dict) -> tuple:
    """Порядок рядків для накопичення залишку пак./товару — як у журналі (хронологія)."""
    d = (_date_to_iso(rr.get("order_date")) or "9999-12-31")[:10]
    inv_str = str(rr.get("invoice_no") or "")
    rk = 0 if str(rr.get("row_kind") or "") == "goods" else 1
    seq = _safe_int_seq(rr.get("seq_no"), 0)
    lk = str(rr.get("linked_order_line_key") or "")
    # Numeric ascending within same date (lower invoice processed first chronologically).
    # Non-numeric invoices (write-offs like "WO-1") sort after all numeric rows.
    try:
        inv_num = float(inv_str.replace(",", ".").strip())
    except Exception:
        inv_num = float("inf")
    return (d, inv_num, rk, seq, lk)


def _packaging_balance_direction_hint(amount: float) -> str:
    """Той самий зміст знаку, що в «Взаєморозрахунках»: + сума → ми винні постачальнику, − → він винен нам."""
    try:
        v = float(amount)
    except Exception:
        v = 0.0
    if abs(v) < 0.0001:
        return "(0)"
    return "(ми винні)" if v > 0 else "(постачальник винен)"


class PackagingStockView:
    ROW_H = 32
    HDR_EXTRA = 6
    # Колонки журналу в «контексті товару»: після базового розрахунку ширини множаться на це число (>1 розширює таблицю, можлива горизонтальна прокрутка).
    ORDERS_TABLE_COL_WIDTH_MUL = 1
    # Фінальний масштаб усіх колонок таблиці замовлень пакування.
    # 1.0 = ширина = width_px (без масштабування).
    ORDERS_TABLE_COL_SCALE = 1.0
    ORDERS_TABLE_HDR_TEXT = 9.5
    OPENING_ROW_KIND = "opening"
    HISTORY_ROW_LIMIT = 20  # rows shown by default; older rows collapse under a toggle
    BDR = _BORDER
    BG_HDR = _HDR
    BG_A = _PANEL
    TXT = 10
    # Картки залишків: одна система розмірів для KPI, постачальника, пакування та блоку товару (див. _packaging_remainder_strip_ui).
    REMAINDER_PAD_H = 4
    REMAINDER_PAD_V = 3
    REMAINDER_LBL_SZ = 7
    REMAINDER_VAL_SZ = 10
    REMAINDER_HEADLINE_SZ = 8
    REMAINDER_ROW_GAP = 4
    REMAINDER_OUTER_PAD = 4
    REMAINDER_TILE_RADIUS = 6
    REMAINDER_FRAME_RADIUS = 8
    REMAINDER_VW_BUDGET_FRAC = 0.44
    REMAINDER_TILE_W_MIN = 64
    REMAINDER_TILE_W_MAX = 142
    # Вкладені блоки дерева (постачальник / пакування / товар): компактніший макет залишків.
    TREE_REMAINDER_WIDTH_MUL = 0.70
    # На базі звуженої плитки (TREE_REMAINDER_WIDTH_MUL): додатково +20% ширини клітинок у дереві.
    TREE_REMAINDER_WIDTH_BOOST = 1.20
    TREE_REMAINDER_PAD_MUL = 0.75
    TREE_REMAINDER_VAL_DELTA = 1.5
    TREE_REMAINDER_LBL_DELTA = 1.0
    # Підписи «Склад пакування» / «Баланс $» у трьох блоках дерева: +1.5 px до поточного tree_compact.
    TREE_REMAINDER_LBL_PLUS = 3.0
    TREE_REMAINDER_GAP_TRIM = 1
    TREE_REMAINDER_OUTER_PAD_MUL = 0.75
    # Кнопки «Замовити пакування» / «Початковий баланс» у картці контексту товару
    CONTEXT_BTN_H = round(30 * 0.75)
    CONTEXT_BTN_TEXT = 12
    # Горизонтальний падінг ≈ на 30% менший за типовий Material (~20–24px)
    CONTEXT_BTN_PAD_H = 8
    CONTEXT_BTN_PAD_V = 2

    # UI Constants
    UI_ACCENT_BLUE = _ACCENT
    UI_TITLE_INK = "#0F172A"
    UI_MUTED_INK = "#475569"

    def __init__(
        self,
        content_area,
        user_data: dict,
        db,
        region_ref: str | None,
        quick_filter: str = "",
    ):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.region_ref = region_ref
        self.quick_filter = str(quick_filter or "").strip()
        self._search_text_query: str = ""
        self.company_id = str(self.user_data.get("company_id") or "").strip()
        self.manager_id = str(
            self.user_data.get("active_manager_id") or self.user_data.get("id") or ""
        ).strip()
        self.t = i18n.bind(str(self.user_data.get("id") or ""))
        self.subordinate_ro = bool(self.user_data.get("subordinate_view_readonly"))
        if not self.subordinate_ro:
            try:
                _eff_rid = self._resolve_region_id()
                if _eff_rid and not self.db.manager_has_category_access_in_region(
                    self.manager_id, self.company_id, _eff_rid
                ):
                    self.subordinate_ro = True
            except Exception:
                pass
        self._general_mode: bool = bool(self.user_data.get("general_mode", False))
        self._general_manager_ids: list[str] = list(
            self.user_data.get("general_manager_ids") or []
        )
        from views.general_mode_mixin import build_general_manager_names

        self._general_manager_names: dict[str, str] = build_general_manager_names(
            self.user_data.get("all_subordinates_flat") or [], self.user_data
        )
        # Без expand/scroll: інакше всередині dashboard content_area (вже scroll+expand)
        # _tree часто отримує висоту 0 — контент є, але не видно.
        self._tree_host = ft.Column(spacing=3)
        self._tables_inner_container = None
        # content_area is the only scrollable region here (_tree_host has no
        # scroll of its own, see comment above) but had no on_scroll tracking
        # — a full _tree_host.controls.clear()+rebuild (_render_tree) could
        # otherwise leave the page snapped back to the top with nothing to
        # restore it to.
        self._content_area_offset: float = 0.0
        self._tables_outer_container = None
        self._loading_box = None
        self._header_loading_indicator = None
        self._sync_indicator_text = None
        self._sync_indicator_dot = None
        self._sync_indicator_container = None
        self._pack_sync_status = "synced"
        self._ops_outbox: dict[str, dict] = {}
        # Consecutive-failure counter per outbox op key. An op whose target row
        # no longer exists (e.g. a stale temp-ID patch that never got re-keyed)
        # would otherwise fail forever and permanently pin the sync status to
        # "Не збережено" — even across restarts, since the outbox is persisted
        # to a local file. After _OPS_OUTBOX_MAX_RETRIES failures the op is
        # dropped instead of retried indefinitely.
        self._ops_outbox_fail_counts: dict[str, int] = {}
        self._ops_outbox_running = False
        self._ops_sync_loop_started = False
        self._ref_data_pulse_ts: int = 0
        # Guards _async_initial_load against overlapping runs: the pulse-check
        # (every 20s from _bg_sync_loop, and again on every resume()) schedules
        # a full reload whenever it sees newer reference data, but _ref_data_pulse_ts
        # only gets updated once that reload actually reaches _load_packaging_metadata.
        # Without this flag, a slow reload still in flight gets re-triggered by the
        # next pulse check before it finishes, stacking up overlapping full
        # tree rebuilds — this is what showed up as "Оновлення" never settling
        # and expand/collapse feeling stuck (found via live testing).
        self._initial_load_in_progress = False
        self._auto_refresh_started = False
        self._auto_refresh_last_sig = ""
        self._orders_last_known_snapshot_sig = ""
        self._counters_row: ft.Row | None = None

        self._cv_body_scroll = None
        self._reload_seq = 0
        self._packaging_data = None
        self._packaging_suppliers_by_id = {}
        self._packaging_products_by_id = {}
        self._packaging_boxes_by_id = {}
        self._packaging_items = []
        self._packaging_suppliers = []
        self._packaging_products = []
        self._packaging_boxes = []
        self._packaging_kpis = {}
        # Local cache of supply orders per context; populated on full load, updated by inline edits.
        self._local_orders_cache: dict[str, list[dict]] = {}
        self._local_opening_cache: dict[str, dict] = {}
        # Write-off rows keyed by context_id; updated immediately on queue, loaded from DB on startup.
        self._local_writeoff_cache: dict[str, list[dict]] = {}
        # IDs of DB write-offs that have been deleted locally but async DB delete may not be done yet.
        self._local_writeoff_delete_pending: set[str] = set()

        self._expanded_packaging_suppliers: set[str] = set()
        self._expanded_packaging_boxes: set[str] = set()
        self._expanded_contexts: set[str] = set()
        # Розгорнуті групи товарних рядків одного замовлення в журналі (linked_order_id).
        self._expanded_pack_order_groups: set[str] = set()
        # Контексти, де розгорнуто «старі» рядки (21+ в хронологічному порядку).
        self._expanded_pack_history_ctxs: set[str] = set()
        self._pack_col_width_overrides: dict[str, int] = {}
        self._pack_visible_column_ids: set[str] | None = None
        self._pack_column_order_ids: list[str] | None = None
        # "Перший місяць відображення" (формат "YYYY-MM", "" = вся історія).
        # Ховає лише старі РЯДКИ журналу (order_date) з рендеру _build_orders_table;
        # залишок (bn/cur_pack_ctx/cur_goods_ctx) рахується з повних orders_ctx/writeoffs_ctx.
        self._display_from_month: str = ""
        # In-place update: ctx_id → wrapper container; order_id → ctx_id
        self._pack_ctx_wrappers: dict[str, ft.Container] = {}
        self._pack_order_ctx_map: dict[str, str] = {}
        self._kpi_hosts: dict[str, ft.Control] | None = None
        self._fx_uah_cache: dict[str, Any] = {"ts": 0.0, "rates": {}}
        self._pack_ui_ready = False
        self._pack_cache_pulse_ts: int = 0
        _som = self.user_data.get("shared_orders_map")
        self._shared_orders_map: dict[str, dict] = (
            _som if isinstance(_som, dict) else {}
        )

    def _log_pack(self, msg: str, *args) -> None:
        pass

    def _set_pack_sync_status(self, status: str) -> None:
        self._pack_sync_status = str(status or "synced")
        txt = self._sync_indicator_text
        dot = self._sync_indicator_dot
        ctr = self._sync_indicator_container
        if txt is None or dot is None or ctr is None:
            return
        if self._pack_sync_status == "synced":
            txt.value = self.t("orders.sync.synced", "Збережено")
            txt.color = _OK
            dot.bgcolor = _OK
            ctr.tooltip = self.t("pack_stock.sync.synced_tip", "Дані в БД. Натисніть для оновлення з БД.")
        elif self._pack_sync_status == "pending":
            txt.value = self.t("orders.sync.pending", "Локально")
            txt.color = "#B45309"
            dot.bgcolor = "#F59E0B"
            ctr.tooltip = self.t("orders.sync.pending_tip", "Дані збережені в кеші та ще не перенесені в БД. Натисніть для примусової синхронізації.")
        elif self._pack_sync_status == "syncing":
            txt.value = self.t("orders.sync.syncing", "Оновлення...")
            txt.color = _TEXT
            dot.bgcolor = "#3B82F6"
            ctr.tooltip = self.t("orders.sync.syncing_tip", "Йде передача локальних змін у БД.")
        else:
            txt.value = self.t("orders.sync.error", "Не збережено")
            txt.color = _ERR
            dot.bgcolor = _ERR
            ctr.tooltip = self.t("pack_stock.sync.error_tip", "Помилка оновлення. Натисніть для повторної спроби.")
        with contextlib.suppress(Exception):
            ctr.update()

    def _refresh_pack_sync_status(self) -> None:
        if self._ops_outbox_running:
            self._set_pack_sync_status("syncing")
            return
        if self._ops_outbox:
            self._set_pack_sync_status("pending")
            return
        self._set_pack_sync_status("synced")

    def _build_pack_sync_indicator(self) -> ft.Container:
        self._sync_indicator_dot = ft.Container(
            width=8, height=8, border_radius=4, bgcolor=_BORDER
        )
        self._sync_indicator_text = ft.Text("", size=10, color=_TEXT)

        def _on_click(_):
            page = self._page()
            if not page or not hasattr(page, "run_task"):
                return
            if self._ops_outbox:
                with contextlib.suppress(Exception):
                    page.run_task(self._flush_pack_outbox_async)

        self._sync_indicator_container = ft.Container(
            content=ft.Row(
                [self._sync_indicator_dot, self._sync_indicator_text],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                tight=True,
            ),
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            border_radius=6,
            bgcolor=_ALT,
            border=ft.border.all(1, _HDR),
            tooltip="Натисніть для збереження в БД",
            on_click=_on_click,
            ink=True,
        )
        self._refresh_pack_sync_status()
        return self._sync_indicator_container

    def _pack_outbox_file_path(self) -> str:
        base = os.getenv("FLET_APP_STORAGE_DATA") or os.path.join(
            os.path.expanduser("~"), ".catmanpro"
        )
        os.makedirs(base, exist_ok=True)
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        return os.path.join(base, f"packaging_ops_outbox_{safe_cid}_{safe_mgr}.json")

    def _pack_orders_cache_file_path(self) -> str:
        base = os.getenv("FLET_APP_STORAGE_DATA") or os.path.join(
            os.path.expanduser("~"), ".catmanpro"
        )
        os.makedirs(base, exist_ok=True)
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        return os.path.join(base, f"packaging_orders_cache_{safe_cid}_{safe_mgr}.json")

    def _pack_scope_pulse(self) -> int:
        try:
            out = (
                self.db.get_companies_sync_pulses([self.company_id], ["packaging"])
                or {}
            )
            by_company = (out or {}).get(self.company_id, {})
            return int((by_company or {}).get("packaging") or 0)
        except Exception:
            return 0

    def _load_pack_orders_cache(self) -> int | None:
        """Load cached supply orders + opening + writeoffs from file. Returns cached pulse_ts or None on miss."""
        path = self._pack_orders_cache_file_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            orders = data.get("orders")
            opening = data.get("opening")
            writeoffs = data.get("writeoffs")
            if not isinstance(orders, dict):
                return None
            self._local_orders_cache = {
                str(k): [dict(r) for r in v if isinstance(r, dict)]
                for k, v in orders.items()
                if isinstance(v, list)
            }
            self._local_opening_cache = {
                str(k): dict(v)
                for k, v in (opening or {}).items()
                if isinstance(v, dict)
            }
            if isinstance(writeoffs, dict):
                self._local_writeoff_cache = {
                    str(k): [dict(r) for r in v if isinstance(r, dict)]
                    for k, v in writeoffs.items()
                    if isinstance(v, list)
                }
            return int(data.get("pulse_ts") or 0)
        except Exception:
            return None

    def _save_pack_orders_cache(self, pulse_ts: int) -> None:
        """Persist supply orders + opening balance + writeoffs to local cache file."""
        path = self._pack_orders_cache_file_path()
        try:

            def _ser(obj):
                import datetime as _dt

                if isinstance(obj, (_dt.datetime, _dt.date)):
                    return obj.isoformat()
                return str(obj)

            payload = {
                "pulse_ts": int(pulse_ts),
                "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
                "orders": {
                    k: [dict(r) for r in v if isinstance(r, dict)]
                    for k, v in (self._local_orders_cache or {}).items()
                },
                "opening": {
                    k: dict(v)
                    for k, v in (self._local_opening_cache or {}).items()
                    if isinstance(v, dict)
                },
                "writeoffs": {
                    k: [dict(r) for r in v if isinstance(r, dict)]
                    for k, v in (self._local_writeoff_cache or {}).items()
                    if isinstance(v, list)
                },
            }
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    payload, f, ensure_ascii=False, separators=(",", ":"), default=_ser
                )
            os.replace(tmp, path)
        except Exception:
            pass

    def _apply_outbox_to_local_cache(self) -> None:
        """Re-apply pending outbox items over local cache (used when flush failed on startup)."""
        for op_key, op in self._ops_outbox.items():
            kind = str((op or {}).get("kind") or "")
            if kind == "order_patch":
                oid = str(op.get("order_id") or "").strip()
                pl = op.get("payload") or {}
                if oid and pl:
                    self._apply_local_supply_order_patch(oid, pl)
            elif kind == "opening_save":
                kwargs = op.get("kwargs") or {}
                ctx_id = str(kwargs.get("context_id") or "").strip()
                if ctx_id:
                    existing = dict(self._local_opening_cache.get(ctx_id) or {})
                    existing.update(
                        {k: v for k, v in kwargs.items() if k != "context_id"}
                    )
                    self._local_opening_cache[ctx_id] = existing
            elif kind == "writeoff_save":
                kwargs = op.get("kwargs") or {}
                ctx_id = str(kwargs.get("context_id") or "").strip()
                if ctx_id:
                    _pack_qty = float(kwargs.get("pack_qty") or kwargs.get("qty") or 0)
                    _goods_qty = float(kwargs.get("goods_qty") or 0)
                    wo_row = {
                        "id": op_key,
                        "context_id": ctx_id,
                        "row_kind": "writeoff",
                        "qty": _pack_qty,
                        "pack_qty": _pack_qty,
                        "goods_qty": _goods_qty,
                        "order_date": kwargs.get("order_date") or None,
                        "seq_no": kwargs.get("seq_no") or 0,
                        "invoice_no": str(kwargs.get("invoice_no") or ""),
                        "comment": str(kwargs.get("comment") or "Списання"),
                    }
                    _seq_w = int(kwargs.get("seq_no") or 0)
                    existing = self._local_writeoff_cache.get(ctx_id) or []
                    self._local_writeoff_cache[ctx_id] = [
                        r
                        for r in existing
                        if str(r.get("id") or "") != op_key
                        and int(r.get("seq_no") or 0) != _seq_w
                    ]
                    self._local_writeoff_cache[ctx_id].append(wo_row)
            elif kind == "writeoff_delete":
                kwargs = op.get("kwargs") or {}
                ctx_id = str(kwargs.get("context_id") or "").strip()
                _seq_d = int(kwargs.get("seq_no") or 0)
                _id_d = str(kwargs.get("id") or "").strip()
                if ctx_id:
                    if _id_d:
                        self._local_writeoff_cache[ctx_id] = [
                            r
                            for r in (self._local_writeoff_cache.get(ctx_id) or [])
                            if str(r.get("id") or "").strip() != _id_d
                        ]
                    elif _seq_d:
                        self._local_writeoff_cache[ctx_id] = [
                            r
                            for r in (self._local_writeoff_cache.get(ctx_id) or [])
                            if int(r.get("seq_no") or 0) != _seq_d
                        ]

    def _pack_layout_file_path(self) -> str:
        base = os.getenv("FLET_APP_STORAGE_DATA") or os.path.join(
            os.path.expanduser("~"), ".catmanpro"
        )
        os.makedirs(base, exist_ok=True)
        uid = (
            str((self.user_data or {}).get("id") or "nouser")
            .replace("/", "_")
            .replace("\\", "_")
        )
        cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        rid = str(self.region_ref or "all").replace("/", "_").replace("\\", "_")
        return os.path.join(base, f"packaging_layout_{cid}_{uid}_{rid}.json")

    def _save_pack_layout_to_file(self) -> None:
        try:
            import json as _json

            data = {
                "col_widths": dict(self._pack_col_width_overrides or {}),
                "visible_columns": sorted(list(self._visible_pack_column_ids())),
                "column_order": list(self._ordered_pack_column_ids()),
                "col_widths_unit": "px",
                "display_from_month": str(self._display_from_month or ""),
            }
            with open(self._pack_layout_file_path(), "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    def _load_pack_outbox(self) -> None:
        path = self._pack_outbox_file_path()
        if not os.path.exists(path):
            self._refresh_pack_sync_status()
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            payload = raw.get("payload") if isinstance(raw, dict) else None
            if isinstance(payload, dict):
                self._ops_outbox = {
                    str(k): dict(v) for k, v in payload.items() if isinstance(v, dict)
                }
                # Restore delete-pending set so DB sync doesn't re-add rows pending deletion
                for _op in self._ops_outbox.values():
                    if isinstance(_op, dict) and _op.get("kind") == "writeoff_delete":
                        _del_id = str((_op.get("kwargs") or {}).get("id") or "")
                        if _del_id:
                            self._local_writeoff_delete_pending.add(_del_id)
        except Exception:
            pass
        self._refresh_pack_sync_status()

    def _save_pack_outbox(self) -> None:
        path = self._pack_outbox_file_path()
        if not self._ops_outbox:
            with contextlib.suppress(Exception):
                if os.path.exists(path):
                    os.remove(path)
            return
        payload = {
            "payload": self._ops_outbox,
            "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
        }
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception:
            self._set_pack_sync_status("error")

    async def flush_pending_async(self) -> None:
        if self._ops_outbox:
            with contextlib.suppress(Exception):
                await self._flush_pack_outbox_async()

    def _restart_tab(self) -> None:
        """Full tab restart after column changes — saves to local+DB then rebuilds via ctrl+f5."""
        import contextlib as _ctx

        # Persist column layout to local file immediately.
        with _ctx.suppress(Exception):
            self._save_pack_layout_to_file()
        pg = self._page()
        if pg is None:
            self._schedule_debounced_reload()
            return
        dash = getattr(pg, "_dashboard", None)
        if dash is None:
            self._schedule_debounced_reload()
            return

        async def _do_restart():
            # Flush column layout and any pending ops to DB before rebuilding.
            with _ctx.suppress(Exception):
                await self._async_flush_pack_layout_to_db()
            with _ctx.suppress(Exception):
                await self.flush_pending_async()
            with _ctx.suppress(Exception):
                await dash._handle_ctrl_f5()

        with _ctx.suppress(Exception):
            pg.run_task(_do_restart)

    async def _flush_pack_outbox_async(self) -> None:
        if self._ops_outbox_running or not self._ops_outbox:
            self._refresh_pack_sync_status()
            return
        self._ops_outbox_running = True
        self._refresh_pack_sync_status()
        failed = False
        # Collects (ctx_id, op_key) for writeoffs that were flushed successfully.
        # Applied in one batch after asyncio.gather to avoid concurrent coroutines
        # overwriting each other's cache edits when multiple writeoffs share a ctx_id.
        _flushed_writeoff_keys: list[tuple[str, str]] = []
        try:
            # Shallow copy — values are the EXACT same dict objects as in live outbox.
            # _queue_pack_order_patch now creates NEW dicts on every edit, so we can
            # use `is` identity check to detect entries modified during the flush.
            snapshot = dict(self._ops_outbox)

            async def _flush_pack_one(op_key: str, snap_op: dict):
                kind = str((snap_op or {}).get("kind") or "").strip()
                if kind == "order_patch":
                    order_id = str((snap_op or {}).get("order_id") or "").strip()
                    payload = (
                        (snap_op or {}).get("payload")
                        if isinstance((snap_op or {}).get("payload"), dict)
                        else {}
                    )
                    if not order_id or not payload:
                        if self._ops_outbox.get(op_key) is snap_op:
                            self._ops_outbox.pop(op_key, None)
                        return True
                    # Temp-order guard: if supply_insert for this temp ID is also
                    # in this flush cycle's snapshot, the INSERT runs in parallel and
                    # UPDATE(tmp_xxx) would fail with 0 rows.  Skip the patch here —
                    # supply_insert's success handler (below) will re-key this op to
                    # the real order ID so the next flush cycle picks it up correctly.
                    if (
                        order_id.startswith("tmp_")
                        and f"supply_insert:{order_id}" in snapshot
                    ):
                        return True  # not a failure; will be retried after re-key
                    ok = await asyncio.to_thread(
                        self.db.update_packaging_supply_order,
                        order_id,
                        payload,
                    )
                    if ok:
                        # Only remove if no newer edit replaced this entry.
                        if self._ops_outbox.get(op_key) is snap_op:
                            self._ops_outbox.pop(op_key, None)
                    return ok
                elif kind == "opening_save":
                    kwargs = (
                        (snap_op or {}).get("kwargs")
                        if isinstance((snap_op or {}).get("kwargs"), dict)
                        else {}
                    )
                    if not kwargs:
                        if self._ops_outbox.get(op_key) is snap_op:
                            self._ops_outbox.pop(op_key, None)
                        return True
                    ok = await asyncio.to_thread(
                        self.db.save_packaging_supply_opening, **kwargs
                    )
                    if ok:
                        if self._ops_outbox.get(op_key) is snap_op:
                            self._ops_outbox.pop(op_key, None)
                    return ok
                elif kind == "writeoff_save":
                    kwargs = (
                        (snap_op or {}).get("kwargs")
                        if isinstance((snap_op or {}).get("kwargs"), dict)
                        else {}
                    )
                    if not kwargs:
                        if self._ops_outbox.get(op_key) is snap_op:
                            self._ops_outbox.pop(op_key, None)
                        return True
                    ok = await asyncio.to_thread(
                        self.db.save_packaging_writeoff, **kwargs
                    )
                    if ok:
                        if self._ops_outbox.get(op_key) is snap_op:
                            self._ops_outbox.pop(op_key, None)
                        # Collect for batch cache cleanup after gather (avoids concurrent
                        # coroutines overwriting each other's cache edits).
                        _wo_ctx = str((kwargs or {}).get("context_id") or "").strip()
                        if _wo_ctx:
                            _flushed_writeoff_keys.append((_wo_ctx, op_key))
                    return ok
                elif kind == "writeoff_delete":
                    kwargs = (
                        (snap_op or {}).get("kwargs")
                        if isinstance((snap_op or {}).get("kwargs"), dict)
                        else {}
                    )
                    if not kwargs:
                        if self._ops_outbox.get(op_key) is snap_op:
                            self._ops_outbox.pop(op_key, None)
                        return True
                    _db_kwargs = {k: v for k, v in (kwargs or {}).items()}
                    ok = await asyncio.to_thread(
                        self.db.delete_packaging_writeoff, **_db_kwargs
                    )
                    if ok:
                        if self._ops_outbox.get(op_key) is snap_op:
                            self._ops_outbox.pop(op_key, None)
                        _del_id = str((kwargs or {}).get("id") or "").strip()
                        if _del_id:
                            self._local_writeoff_delete_pending.discard(_del_id)
                    return ok or True  # treat "row not found" as success
                elif kind == "supply_insert":
                    _si_ctx = str((snap_op or {}).get("ctx_id") or "").strip()
                    _si_tmp = str((snap_op or {}).get("temp_id") or "").strip()
                    _si_rec = (snap_op or {}).get("record")
                    if not _si_rec or not isinstance(_si_rec, dict):
                        if self._ops_outbox.get(op_key) is snap_op:
                            self._ops_outbox.pop(op_key, None)
                        return True
                    real_oid = await asyncio.to_thread(
                        self.db.add_packaging_supply_order, dict(_si_rec)
                    )
                    if real_oid:
                        for _r in self._local_orders_cache.get(_si_ctx) or []:
                            if isinstance(_r, dict) and _r.get("id") == _si_tmp:
                                _r["id"] = real_oid
                                break
                        with contextlib.suppress(Exception):
                            self._pack_order_ctx_map.pop(_si_tmp, None)
                            if _si_ctx:
                                self._pack_order_ctx_map[real_oid] = _si_ctx
                        # Re-key any pending order_patch from temp ID to real ID so
                        # the next flush cycle can UPDATE the now-existing DB row.
                        _tmp_pk = f"order:{_si_tmp}"
                        _tmp_pop = self._ops_outbox.get(_tmp_pk)
                        if (
                            isinstance(_tmp_pop, dict)
                            and _tmp_pop.get("kind") == "order_patch"
                        ):
                            _tmp_pl = dict((_tmp_pop or {}).get("payload") or {})
                            _real_pk = f"order:{real_oid}"
                            _ex_real = self._ops_outbox.get(_real_pk)
                            if (
                                isinstance(_ex_real, dict)
                                and _ex_real.get("kind") == "order_patch"
                            ):
                                _merged = dict((_ex_real or {}).get("payload") or {})
                                _merged.update(_tmp_pl)
                                self._ops_outbox[_real_pk] = {
                                    "kind": "order_patch",
                                    "order_id": real_oid,
                                    "payload": _merged,
                                }
                            else:
                                self._ops_outbox[_real_pk] = {
                                    "kind": "order_patch",
                                    "order_id": real_oid,
                                    "payload": _tmp_pl,
                                }
                            self._ops_outbox.pop(_tmp_pk, None)
                        if self._ops_outbox.get(op_key) is snap_op:
                            self._ops_outbox.pop(op_key, None)
                    return bool(real_oid)
                else:
                    if self._ops_outbox.get(op_key) is snap_op:
                        self._ops_outbox.pop(op_key, None)
                    return True

            op_keys = list(snapshot.keys())
            results = await asyncio.gather(
                *[_flush_pack_one(k, v) for k, v in snapshot.items()],
                return_exceptions=True,
            )
            still_failed = []
            for op_key, r in zip(op_keys, results):
                ok = r is True or (isinstance(r, bool) and r)
                if ok:
                    self._ops_outbox_fail_counts.pop(op_key, None)
                    continue
                # Op failed (returned falsy or raised). If the handler didn't
                # already remove it, count the failure and drop it once it's
                # failed too many times in a row — otherwise a poison entry
                # (e.g. a patch whose target row no longer exists, such as an
                # orphaned temp-ID that never got re-keyed to a real order id)
                # retries forever and permanently pins the sync status to
                # "Не збережено", even across restarts.
                if op_key in self._ops_outbox:
                    count = self._ops_outbox_fail_counts.get(op_key, 0) + 1
                    self._ops_outbox_fail_counts[op_key] = count
                    if count >= _OPS_OUTBOX_MAX_RETRIES:
                        logger.warning(
                            "[pack_stock] dropping outbox op after %d failed attempts: %s",
                            count, op_key,
                        )
                        self._ops_outbox.pop(op_key, None)
                        self._ops_outbox_fail_counts.pop(op_key, None)
                        continue
                still_failed.append(op_key)
            failed = bool(still_failed)
            # Remove flushed writeoff placeholders from local cache in one pass per ctx_id
            # so concurrent successes don't overwrite each other's changes.
            if _flushed_writeoff_keys:
                _ctx_to_rm: dict[str, set[str]] = {}
                for _wctx, _wkey in _flushed_writeoff_keys:
                    _ctx_to_rm.setdefault(_wctx, set()).add(_wkey)
                for _wctx, _keys_rm in _ctx_to_rm.items():
                    self._local_writeoff_cache[_wctx] = [
                        r
                        for r in (self._local_writeoff_cache.get(_wctx) or [])
                        if str(r.get("id") or "") not in _keys_rm
                    ]
            self._save_pack_outbox()
            if not failed and not self._ops_outbox:
                # All items flushed — update local cache file with fresh pulse so
                # next startup skips the unnecessary DB re-fetch.
                new_pulse = await asyncio.to_thread(self._pack_scope_pulse)
                if new_pulse:
                    await asyncio.to_thread(self._save_pack_orders_cache, new_pulse)
                    self._pack_cache_pulse_ts = new_pulse
        except Exception:
            failed = True
        finally:
            self._ops_outbox_running = False
            if failed:
                self._set_pack_sync_status("error")
            else:
                self._refresh_pack_sync_status()
            # If new edits arrived during the flush, schedule another pass shortly.
            if self._ops_outbox and not failed:
                self._schedule_debounced_flush(0.5)

    _ops_flush_debounce_seq: int = 0

    def _schedule_debounced_flush(self, delay: float = 0.5) -> None:
        """Негайний debounced flush в БД після зміни — не чекаємо 45с."""
        self._ops_flush_debounce_seq = getattr(self, "_ops_flush_debounce_seq", 0) + 1
        my_seq = self._ops_flush_debounce_seq
        pg = self._page()
        if not pg or not hasattr(pg, "run_task"):
            return

        async def _do(seq: int = my_seq) -> None:
            await asyncio.sleep(delay)
            if getattr(self, "_ops_flush_debounce_seq", 0) != seq:
                return
            await self._flush_pack_outbox_async()

        with contextlib.suppress(Exception):
            pg.run_task(_do)

    def _queue_pack_order_patch(self, order_id: str, payload: dict) -> None:
        if self.subordinate_ro:
            return
        oid = str(order_id or "").strip()
        if not oid or not isinstance(payload, dict) or not payload:
            return
        # For temp orders still awaiting DB insert, merge the patch directly
        # into the pending supply_insert record (mutate in-place to preserve
        # the outer dict's identity for the flush `is`-check).  This means the
        # INSERT will carry the latest field values and no separate order_patch
        # op is needed, avoiding the parallel-flush failure described below.
        if oid.startswith("tmp_"):
            _ins_key = f"supply_insert:{oid}"
            _ins_op = self._ops_outbox.get(_ins_key)
            if isinstance(_ins_op, dict) and _ins_op.get("kind") == "supply_insert":
                _rec = _ins_op.get("record")
                if isinstance(_rec, dict):
                    _rec.update(payload)  # in-place: preserves outer dict identity
                    self._save_pack_outbox()
                    self._refresh_pack_sync_status()
                    return
        key = f"order:{oid}"
        # Fresh edit — don't let a stale failure count from an earlier,
        # possibly since-resolved issue count against this new payload.
        self._ops_outbox_fail_counts.pop(key, None)
        existing = self._ops_outbox.get(key)
        if isinstance(existing, dict) and existing.get("kind") == "order_patch":
            # Merge patch — create a NEW dict object (not mutation!) so that
            # _flush_pack_outbox_async can detect a replaced entry via `is` check
            # and avoid popping data that arrived during a concurrent flush.
            merged = dict(existing.get("payload") or {})
            merged.update(payload)
            self._ops_outbox[key] = {
                "kind": "order_patch",
                "order_id": oid,
                "payload": merged,
            }
        else:
            self._ops_outbox[key] = {
                "kind": "order_patch",
                "order_id": oid,
                "payload": dict(payload),
            }
        self._save_pack_outbox()
        self._refresh_pack_sync_status()

    def _queue_pack_opening_save(self, kwargs: dict) -> None:
        ctx_id = str((kwargs or {}).get("context_id") or "").strip()
        if not ctx_id:
            return
        self._ops_outbox[f"opening:{ctx_id}"] = {
            "kind": "opening_save",
            "kwargs": dict(kwargs or {}),
        }
        # Update local opening cache immediately so _render_tree reflects the
        # new value without waiting for a DB round-trip (fixes "shows only after refresh").
        existing = dict(self._local_opening_cache.get(ctx_id) or {})
        existing.update({k: v for k, v in (kwargs or {}).items() if k != "context_id"})
        self._local_opening_cache[ctx_id] = existing
        self._save_pack_outbox()
        self._refresh_pack_sync_status()

    async def _ops_sync_loop(self) -> None:
        while True:
            await asyncio.sleep(45)
            page = self._page()
            if not page:
                continue
            if self._ops_outbox:
                with contextlib.suppress(Exception):
                    await self._flush_pack_outbox_async()

    # ── page helpers ───────────────────────────────────────────────────────
    def _page(self):
        if not self.content_area:
            return None
        try:
            return self.content_area.page
        except RuntimeError:
            # Control is not attached to page yet (or already detached).
            return None

    def _on_content_area_scroll(self, e) -> None:
        try:
            self._content_area_offset = float(getattr(e, "pixels", 0) or 0)
        except Exception:
            pass

    def _restore_content_area_scroll(self) -> None:
        offset = max(0.0, float(getattr(self, "_content_area_offset", 0.0) or 0.0))
        if offset <= 0:
            return
        pg = self._page()
        if not pg or not hasattr(pg, "run_task"):
            return

        async def _restore():
            await asyncio.sleep(0.06)
            with contextlib.suppress(Exception):
                await self.content_area.scroll_to(offset=offset, duration=0)

        with contextlib.suppress(Exception):
            pg.run_task(_restore)

    def _show_help(self) -> None:
        from views.help_utils import show_modal_help
        page = self._page()
        show_modal_help(page, self.t("pack_stock.help.title", "Склад пакування"), [
            self.t("pack_stock.help.desc", "Журнал закупівель пакувальних матеріалів: замовлення, кількість, статуси."),
            (self.t("pack_stock.help.steps_title", "Як користуватись"), None),
            self.t("pack_stock.help.step1", "1. Дерево згруповане по постачальниках пакування — клік розгортає позиції."),
            self.t("pack_stock.help.step2", "2. «Замовити» — відкриває форму нового замовлення пакування."),
            self.t("pack_stock.help.step3", "3. «Початковий залишок» — вкажіть стартовий залишок на складі."),
            self.t("pack_stock.help.step4", "4. «Списати» — зафіксуйте витрату пакування зі складу."),
            self.t("pack_stock.help.step5", "5. Іконка таблиці налаштовує видимі стовпці."),
            self.t("pack_stock.help.step6", "6. Синхронізатор показує стан збереження локальних змін."),
        ])

    def _show_pack_snack(self, text: str, color: str = _ERR) -> None:
        pg = self._page()
        if not pg:
            return
        import contextlib as _cl

        with _cl.suppress(Exception):
            pg.show_dialog(
                ft.SnackBar(
                    ft.Text(text, color=ft.Colors.WHITE, weight="bold"),
                    bgcolor=color,
                    duration=3000,
                )
            )

    def _is_pack_invoice_dup(self, exclude_oid: str, invoice: str) -> bool:
        inv = str(invoice or "").strip()
        if not inv:
            return False
        ex = str(exclude_oid or "").strip()
        for ctx_orders in (self._local_orders_cache or {}).values():
            for o in ctx_orders:
                if not isinstance(o, dict):
                    continue
                if ex and str(o.get("id") or "").strip() == ex:
                    continue
                if str(o.get("invoice_no") or "").strip() == inv:
                    return True
        return False

    def _viewport_w(self, page) -> int:
        host_w = (
            int(getattr(self.content_area, "width", 0) or 0) if self.content_area else 0
        )
        if host_w > 0:
            return max(320, int(host_w))
        page_w = int(getattr(page, "width", 0) or 0)
        if page_w <= 0:
            win = getattr(page, "window", None)
            page_w = int(getattr(win, "width", 0) or 0) if win else 0
        return max(320, page_w if page_w > 0 else 320)

    def _apply_viewport_size(self, _=None):
        page = self._page()
        if not page:
            return
        if not self._tables_inner_container or not self._tables_outer_container:
            return
        try:
            vw = int(self._viewport_w(page))
            self._tables_inner_container.width = max(700, vw - 24)
            self._tables_outer_container.update()
        except Exception:
            with contextlib.suppress(Exception):
                if self._tables_inner_container:
                    self._tables_inner_container.update()

    def _attach_resize_handler(self):
        page = self._page()
        if not page:
            return

        def _on_resized(e):
            try:
                self._apply_viewport_size(e)
            except Exception:
                pass

        try:
            if hasattr(page, "on_resized"):
                page.on_resized = _on_resized
            elif hasattr(page, "on_resize"):
                page.on_resize = _on_resized
        except Exception:
            pass

    def _viewport_w(self, page: ft.Page) -> int:
        host_w = (
            int(getattr(self.content_area, "width", 0) or 0) if self.content_area else 0
        )
        if host_w > 0:
            return max(320, int(host_w))
        page_w = int(getattr(page, "width", 0) or 0)
        if page_w <= 0:
            win = getattr(page, "window", None)
            page_w = int(getattr(win, "width", 0) or 0) if win else 0
        return max(320, page_w or 980)

    def _manager_region_rows(self) -> list[dict]:
        """Ті самі регіони, що в шапці (ім’я → id), incl. обмеження видимості для менеджера."""
        try:
            rows = (
                self.db.get_visible_regions_for_manager(
                    self.company_id, self.manager_id
                )
                or []
            )
        except Exception:
            rows = []
        out = [r for r in rows if isinstance(r, dict)]
        if out:
            return out
        self._log_pack(
            "_manager_region_rows: fallback get_company_regions (visible list empty)"
        )
        return [
            r
            for r in (self.db.get_company_regions(self.company_id) or [])
            if isinstance(r, dict)
        ]

    def _resolve_region_id(self) -> str:
        """Шапка передає зазвичай *назву* регіону; у БД у контекстах — uuid. Без uuid запит повертає 0 рядків."""
        rr = "" if self.region_ref is None else str(self.region_ref).strip()
        if not rr:
            return ""
        regions = self._manager_region_rows()
        known_ids = {
            str(r.get("id") or "").strip()
            for r in regions
            if str(r.get("id") or "").strip()
        }
        if rr in known_ids:
            return rr
        rrl = rr.casefold()
        for r in regions:
            rid = str(r.get("id") or "").strip()
            rname = str(r.get("name") or "").strip()
            if rid and rid == rr:
                return rid
            if rname == rr or (rname and rname.casefold() == rrl):
                return rid
        self._log_pack(
            "_resolve_region_id: no match for ref=%r (known_ids=%s names=%s)",
            rr,
            sorted(known_ids)[:5],
            [str(x.get("name")) for x in regions[:8]],
        )
        return ""

    def _region_ids_for_scope(self) -> list[str]:
        """При вибраному конкретному регіоні — один id; якщо region_ref=None («Всі регіони») — усі id з видимих регіонів."""
        if self.region_ref is not None:
            rid = self._resolve_region_id()
            return [rid] if rid else []
        out: list[str] = []
        seen: set[str] = set()
        for r in self._manager_region_rows():
            i = str(r.get("id") or "").strip()
            if i and i not in seen:
                seen.add(i)
                out.append(i)
        return sorted(out)

    def _region_label(self, rid: str) -> str:
        rr = str(rid or "").strip()
        if not rr:
            return "—"
        for r in self._manager_region_rows():
            if str(r.get("id") or "").strip() == rr:
                return str(r.get("name") or rr).strip() or rr
        return rr

    @staticmethod
    def _ctx_linked_product_ids(ctx: dict) -> list[str]:
        linked = ctx.get("linked_product_ids")
        if isinstance(linked, (bytes, bytearray)):
            try:
                linked = linked.decode("utf-8", errors="replace")
            except Exception:
                linked = "[]"
        if isinstance(linked, str):
            try:
                linked = json.loads(linked)
            except Exception:
                linked = []
        linked = linked if isinstance(linked, list) else []
        seen: set[str] = set()
        out: list[str] = []
        for x in linked:
            pid = str(x or "").strip()
            if pid and pid not in seen:
                seen.add(pid)
                out.append(pid)
        return out

    # ── table column math (pct like Orders) ───────────────────────────────
    @staticmethod
    def _short_label(txt: str, mx: int = 16) -> str:
        s = str(txt or "").strip() or "—"
        return s if len(s) <= mx else (s[: max(1, mx - 1)] + "…")

    @staticmethod
    def _text_wrap(value: Any, **kwargs: Any) -> ft.Text:
        """Текст із переносами в межах ширини (Row з expand або Column stretch)."""
        overflow = kwargs.pop("overflow", ft.TextOverflow.VISIBLE)
        max_lines = kwargs.pop("max_lines", None)
        text_align = kwargs.pop("text_align", ft.TextAlign.START)
        return ft.Text(
            str(value if value is not None else ""),
            text_align=text_align,
            no_wrap=False,
            max_lines=max_lines,
            overflow=overflow,
            **kwargs,
        )

    def _col_specs(
        self,
        show_usd_cost: bool,
        packaging_supplier_name: str,
        goods_supplier_name: str,
        *,
        budget_w: int | None = None,
    ) -> list[dict]:
        """
        Ширини колонок тут задаються полем pct (пропорції «зайвого» ширини після min_side).
        Поле width у результаті завжди обчислюється — його зміни в коді ігноруються після побудови списку.
        Для фіксованої ширини в пікселях додайте width_px до словника колонки (напр. \"width_px\": 120).
        Після підсумку сумарної ширини до budget множиться на ORDERS_TABLE_COL_WIDTH_MUL (типово 2).
        """
        p1 = self._short_label(packaging_supplier_name, 14)
        p2 = self._short_label(goods_supplier_name, 14)
        cols: list[dict] = [
            {"key": "seq", "label": "№", "pct": 3.0, "width_px": 25},
            {"key": "d", "label": self.t("pack_stock.col.d", "Дата"), "pct": 7.5, "width_px": 70},
            {"key": "qty", "label": self.t("pack_stock.col.qty", "Кількість"), "pct": 7.0, "width_px": 70},
            {"key": "st", "label": self.t("pack_stock.col.st", "Статус"), "pct": 7.0, "width_px": 80},
            {"key": "cost", "label": self.t("pack_stock.col.cost", "Вартість"), "pct": 8.0, "width_px": 80},
            *(
                [{"key": "cost_usd", "label": self.t("pack_stock.col.cost_usd", "Вартість $"), "pct": 8.0, "width_px": 80}]
                if show_usd_cost
                else []
            ),
            {"key": "paid", "label": self.t("pack_stock.col.paid", "Сплачено"), "pct": 8.0, "width_px": 80},
            {"key": "ship", "label": f"{self.t('pack_stock.col.ship', 'Відпр.')}\n({p2})", "pct": 8.0, "width_px": 70},
            {
                "key": "bal_pack",
                "label": f"{self.t('pack_stock.col.bal_pack', 'Залишок')}\n({p1})",
                "pct": 8.0,
                "width_px": 70,
            },
            {
                "key": "bal_goods",
                "label": f"{self.t('pack_stock.col.bal_goods', 'Залишок')}\n({p2})",
                "pct": 8.0,
                "width_px": 70,
            },
            {"key": "zp", "label": self.t("pack_stock.col.zp", "ЗП обліку"), "pct": 7.0, "width_px": 60},
            {"key": "inv", "label": self.t("pack_stock.col.inv", "Інвойс"), "pct": 7.0, "width_px": 50},
            {"key": "cmt", "label": self.t("pack_stock.col.cmt", "Коментар"), "pct": 20.0, "width_px": 120},
            {"key": "del", "label": "", "pct": 3.5, "width_px": 35},
        ]
        all_cols_by_key = {str(c.get("key") or ""): dict(c) for c in cols}
        ordered_ids = [
            k for k in self._ordered_pack_column_ids() if k in all_cols_by_key
        ]
        if ordered_ids:
            cols = [all_cols_by_key[k] for k in ordered_ids]
        visible_ids = set(self._visible_pack_column_ids())
        if visible_ids:
            filtered_cols = [c for c in cols if str(c.get("key") or "") in visible_ids]
            if filtered_cols:
                cols = filtered_cols
        width_overrides = (
            self._pack_col_width_overrides
            if isinstance(self._pack_col_width_overrides, dict)
            else {}
        )
        for c in cols:
            key = str(c.get("key") or "")
            if key in width_overrides:
                c["width_px"] = int(
                    max(24, min(900, int(width_overrides.get(key) or 0)))
                )

        # Stable minimum baseline: 100% == 1080px; on wider screens grow to available budget.
        vw = max(1080, int(budget_w or 1080))

        def _partition_int_totals(weights: list[float], total_units: int) -> list[int]:
            """Цілочисельний розподіл із сумою рівно total_units (largest remainder)."""
            if not weights:
                return []
            tw = float(sum(weights))
            tw = tw if tw > 0 else 1.0
            raw = [
                (float(weights[i]) / tw) * float(total_units)
                for i in range(len(weights))
            ]
            base = [int(x) for x in raw]
            rem = int(total_units) - sum(base)

            order = sorted(
                range(len(base)), key=lambda i: raw[i] - base[i], reverse=True
            )
            ln = len(base) if base else 1
            k = 0
            while rem > 0:
                base[order[k % ln]] += 1
                rem -= 1
                k += 1

            while rem < 0:
                j = max(range(len(base)), key=lambda i: base[i])
                if base[j] <= 0:
                    break
                base[j] -= 1
                rem += 1

            return base

        min_side = 24

        # Build width map: width_px always wins (including user overrides).
        # The table has horizontal scroll so widths exceeding the viewport are fine.
        fixed_by_key: dict[str, int] = {}
        for c in cols:
            wpx = c.get("width_px", None)
            if wpx is None:
                continue
            try:
                fixed_by_key[str(c["key"])] = max(min_side, int(wpx))
            except (TypeError, ValueError):
                continue

        flex_cols = [c for c in cols if str(c["key"]) not in fixed_by_key]
        n_flex = len(flex_cols)
        fixed_sum = sum(fixed_by_key.values())

        acc: list[dict] = []
        if n_flex == 0:
            # All columns have explicit pixel widths — use them directly.
            # Table width = sum(widths); horizontal scroll if wider than viewport.
            for c in cols:
                k = str(c["key"])
                acc.append(dict(c, width=fixed_by_key.get(k, min_side)))
        else:
            # Mix of fixed and flex columns.
            # Fixed columns keep their px values; flex columns share remaining space.
            rem_vw = max(min_side * n_flex, int(vw) - fixed_sum)
            wts_f = [float(c.get("pct") or 1.0) for c in flex_cols]
            extras = _partition_int_totals(wts_f, rem_vw - min_side * n_flex)
            flex_i = 0
            for c in cols:
                k = str(c["key"])
                if k in fixed_by_key:
                    acc.append(dict(c, width=fixed_by_key[k]))
                else:
                    cx = dict(c)
                    cx["width"] = min_side + int(extras[flex_i])
                    flex_i += 1
                    acc.append(cx)

        if not acc:
            return []

        # When ALL columns have explicit width_px (n_flex==0), don't force the
        # sum to equal the viewport budget — the table uses horizontal scroll.
        if n_flex > 0:
            s_check = sum(int(c.get("width") or 0) for c in acc)
            drift = vw - s_check
            if drift != 0 and acc:
                # Adjust only the last FLEX column so fixed columns keep their widths.
                for ci in reversed(range(len(acc))):
                    if str(acc[ci].get("key") or "") not in fixed_by_key:
                        acc[ci]["width"] = max(
                            min_side, int(acc[ci].get("width") or 0) + int(drift)
                        )
                        break

        mul = max(1, int(getattr(self.__class__, "ORDERS_TABLE_COL_WIDTH_MUL", 1) or 1))
        if mul > 1:
            vw_out = int(vw) * mul
            scaled: list[dict] = []
            for c in acc:
                cx = dict(c)
                cx["width"] = max(min_side, int(cx.get("width") or 0) * mul)
                scaled.append(cx)
            drift_s = vw_out - sum(int(x.get("width") or 0) for x in scaled)
            if drift_s != 0 and scaled:
                scaled[-1]["width"] = max(
                    min_side, int(scaled[-1].get("width") or 0) + int(drift_s)
                )
            acc = scaled

        # Boost removed — widths are now pixel-exact from width_px / user overrides.

        scale = float(getattr(self.__class__, "ORDERS_TABLE_COL_SCALE", 1.0) or 1.0)
        if acc and abs(scale - 1.0) > 1e-9:
            scaled_acc: list[dict] = []
            for c in acc:
                cx = dict(c)
                cx["width"] = max(12, int(round(int(cx.get("width") or 0) * scale)))
                scaled_acc.append(cx)
            acc = scaled_acc

        return acc

    def _cell(
        self,
        text: str,
        w: int,
        h: int,
        *,
        bold: bool = False,
        bg: str = BG_A,
        color: str = _TEXT,
    ):
        wi, hi = int(w or 0), int(h or 0)
        if wi <= 0:
            self._log_pack(
                "_cell skipped: w<=0 h=%s text_preview=%r", hi, (str(text or "")[:40])
            )
            return ft.Container(width=0, height=0, visible=False)
        raw = str(text or "")
        if raw not in {"", "-", "—"}:
            try:
                raw = format_maybe_numeric_text(raw)
            except Exception:
                pass
        # У фіксованій висоті комірки багаторядковий Text з VISIBLE давав злам верстки (сірий блок).
        return ft.Container(
            width=wi,
            height=hi,
            bgcolor=bg,
            border=ft.border.all(1, self.BDR),
            padding=0,
            alignment=ft.Alignment(0, 0),
            content=ft.Text(
                raw,
                size=self.TXT,
                weight="bold" if bold else "w400",
                color=color,
                text_align=ft.TextAlign.CENTER,
                no_wrap=True,
                max_lines=1,
                overflow=ft.TextOverflow.ELLIPSIS,
            ),
        )

    def _cell_wrap(
        self,
        text: str,
        w: int,
        min_h: int,
        *,
        bold: bool = False,
        bg: str = BG_A,
        color: str = _TEXT,
    ):
        """Cell that allows text wrapping and adaptive height (grows if text wraps)."""
        wi, hi = int(w or 0), int(min_h or 0)
        if wi <= 0:
            return ft.Container(width=0, height=0, visible=False)
        raw = str(text or "")
        if raw not in {"", "-", "—"}:
            try:
                raw = format_maybe_numeric_text(raw)
            except Exception:
                pass
        # Allow wrapping and multiple lines, height expands as needed
        wrapped_text = _wrap_text_for_width(raw, wi)
        return ft.Container(
            width=wi,
            height=hi,
            bgcolor=bg,
            border=ft.border.all(1, self.BDR),
            padding=ft.padding.symmetric(horizontal=4, vertical=2),
            alignment=ft.Alignment(0, -1),  # Align to top-left
            content=ft.Text(
                wrapped_text,
                size=self.TXT,
                weight="bold" if bold else "w400",
                color=color,
                text_align=ft.TextAlign.LEFT,
                no_wrap=False,
                max_lines=None,  # Allow unlimited lines
                overflow=ft.TextOverflow.CLIP,
            ),
        )

    def _calc_wrap_row_height(
        self,
        invoice_text: str,
        comment_text: str,
        inv_w: int,
        cmt_w: int,
        base_h: int,
    ) -> int:
        """Calculate unified row height so the entire row grows with wrapped invoice/comment."""

        def _line_count(txt: str, width: int) -> int:
            wrapped = _wrap_text_for_width(str(txt or ""), int(width or 0))
            lines = [ln for ln in str(wrapped or "").split("\n") if ln != ""]
            return max(1, len(lines))

        try:
            base = int(base_h or self.ROW_H)
        except Exception:
            base = int(self.ROW_H)
        max_lines = max(
            _line_count(invoice_text, inv_w),
            _line_count(comment_text, cmt_w),
        )
        if max_lines <= 1:
            return base
        return int(max(base, min(240, 16 * max_lines + 6)))

    def _header_cell(
        self,
        text: str,
        w: int,
        h: int,
        *,
        bold: bool = True,
        bg: str = BG_HDR,
        color: str = _CHIP_T,
    ) -> ft.Control:
        """Шапка журналу складу пакування: перенос через _wrap_text_for_width, max_lines=4."""
        wi, hi = int(w or 0), int(h or 0)
        if wi <= 0:
            return ft.Container(width=0, height=0, visible=False)
        display_text = _wrap_text_for_width(str(text or ""), wi)
        hs = float(self.__class__.ORDERS_TABLE_HDR_TEXT)
        return ft.Container(
            width=wi,
            height=hi,
            bgcolor=bg,
            border=ft.border.all(1, self.BDR),
            border_radius=0,
            alignment=ft.Alignment(0, 0),
            padding=ft.padding.symmetric(horizontal=0, vertical=0),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.Container(
                expand=True,
                alignment=ft.Alignment(0, 0),
                content=ft.Text(
                    display_text,
                    size=hs,
                    weight="bold" if bold else "w400",
                    color=color,
                    text_align=ft.TextAlign.CENTER,
                    no_wrap=False,
                    max_lines=4,
                    overflow=ft.TextOverflow.VISIBLE,
                ),
            ),
        )

    def _tf(
        self,
        value: str,
        w: int,
        h: int,
        *,
        read_only: bool = False,
        multiline: bool = False,
        on_focus=None,
        on_blur=None,
        on_submit=None,
        numeric: bool = False,
        keyboard_type=None,
        always_call_on_tap: bool = False,
    ):
        vpad = 0 if multiline else max(0, int((h - (self.TXT + 6)) / 2))
        kt = (
            keyboard_type
            if keyboard_type is not None
            else (ft.KeyboardType.NUMBER if numeric else ft.KeyboardType.TEXT)
        )
        tf = ft.TextField(
            value=str(value or ""),
            width=w,
            height=h,
            text_size=self.TXT,
            border=ft.InputBorder.NONE,
            content_padding=ft.padding.symmetric(horizontal=0, vertical=vpad),
            dense=True,
            text_align=ft.TextAlign.CENTER,
            text_vertical_align=ft.VerticalAlignment.CENTER,
            read_only=read_only or self.subordinate_ro,
            multiline=multiline,
            min_lines=1 if multiline else None,
            max_lines=3 if multiline else 1,
            on_focus=on_focus,
            on_blur=on_blur,
            on_submit=on_submit,
            keyboard_type=kt,
            always_call_on_tap=always_call_on_tap,
        )
        return tf

    def _open_supply_order_date_picker(self, order_id: str, initial_iso: str) -> None:
        """Календар для дати збереженого рядка «замовлення пакування» (supply)."""
        page = self._page()
        if not page or self.subordinate_ro:
            return
        oid = str(order_id or "").strip()
        if not oid:
            return

        initial_d = date.today()
        ds = str(initial_iso or "").strip()[:10]
        try:
            if len(ds) >= 10:
                initial_d = datetime.fromisoformat(ds).date()
        except Exception:
            initial_d = date.today()

        picker = ft.DatePicker(
            value=initial_d,
            first_date=date(2018, 1, 1),
            last_date=date(2100, 12, 31),
            entry_mode=ft.DatePickerEntryMode.CALENDAR_ONLY,
            date_picker_mode=ft.DatePickerMode.DAY,
            locale=ft.Locale("uk", "UA"),
            cancel_text="Очистити",
            confirm_text="OK",
            modal=True,
            error_format_text="",
            error_invalid_text="",
        )

        def _picked(e):
            raw = _date_picker_event_value(e)
            if raw is None:
                od_commit = None
            else:
                try:
                    s = _datepicker_value_to_iso_date(raw)
                    od_commit = (s.strip()[:10] or None) if s else None
                except Exception:
                    od_commit = str(raw)[:10] or None
            # Close picker immediately to reduce UI blocking perception.
            try:
                page.close_dialog()
            except Exception:
                pass
            try:
                self._apply_local_supply_order_patch(oid, {"order_date": od_commit})
                self._persist_supply_order_patch_async(oid, {"order_date": od_commit})
                ctx_id = self._pack_order_ctx_map.get(oid)
                if not (ctx_id and self._refresh_pack_ctx_inplace(ctx_id)):
                    self._schedule_debounced_reload()
            except Exception:
                pass

        picker.on_change = _picked
        page.show_dialog(picker)

    def _open_writeoff_date_picker(self, ctx_id: str, wo_row: dict) -> None:
        """Календар для дати рядка списання. Після вибору — оновлює кеш і перерендерує."""
        page = self._page()
        if not page or self.subordinate_ro:
            return
        initial_iso = str(wo_row.get("order_date") or "").strip()[:10]
        initial_d = date.today()
        try:
            if len(initial_iso) >= 10:
                initial_d = datetime.fromisoformat(initial_iso).date()
        except Exception:
            pass

        picker = ft.DatePicker(
            value=initial_d,
            first_date=date(2018, 1, 1),
            last_date=date(2100, 12, 31),
            entry_mode=ft.DatePickerEntryMode.CALENDAR_ONLY,
            date_picker_mode=ft.DatePickerMode.DAY,
            locale=ft.Locale("uk", "UA"),
            cancel_text="Очистити",
            confirm_text="OK",
            modal=True,
            error_format_text="",
            error_invalid_text="",
        )

        def _picked(e):
            raw = _date_picker_event_value(e)
            new_date = None
            if raw is not None:
                try:
                    new_date = _datepicker_value_to_iso_date(raw)
                    new_date = new_date.strip()[:10] or None
                except Exception:
                    new_date = str(raw)[:10] or None
            try:
                page.close_dialog()
            except Exception:
                pass
            self._update_writeoff_field(ctx_id, wo_row, "order_date", new_date)

        picker.on_change = _picked
        page.show_dialog(picker)

    def _open_writeoff_qty_editor(self, ctx_id: str, wo_row: dict, field: str) -> None:
        """Мінідіалог для редагування pack_qty або goods_qty рядка списання."""
        page = self._page()
        if not page or self.subordinate_ro:
            return
        current = float(wo_row.get(field) or 0)
        label = (
            "Списання зі складу постачальника пакування, шт."
            if field == "pack_qty"
            else "Списання зі складу постачальника товару, шт."
        )
        tf = ft.TextField(
            value=str(int(current) if current == int(current) else current),
            keyboard_type=ft.KeyboardType.NUMBER,
            width=200,
            dense=False,
            text_size=14,
            border_radius=8,
            filled=True,
            bgcolor=_ALT2,
            border_color=_HDR,
            focused_border_color=_ERR,
            content_padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            autofocus=True,
        )
        dlg_ref: list = [None]

        def _close_qty_dlg():
            with contextlib.suppress(Exception):
                self._close_dlg(dlg_ref[0])

        def _save(_=None):
            raw = str(tf.value or "").strip().replace(",", ".").replace(" ", "")
            try:
                new_val = float(raw) if raw else 0.0
            except Exception:
                new_val = 0.0
            _close_qty_dlg()
            self._update_writeoff_field(ctx_id, wo_row, field, new_val)

        tf.on_submit = _save
        dlg_ref[0] = ft.AlertDialog(
            modal=True,
            title=ft.Text(label, size=13, weight="bold", color=_TEXT),
            content=ft.Container(width=220, content=tf, padding=ft.padding.only(top=8)),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: _close_qty_dlg()),
                ft.FilledButton(
                    "Зберегти",
                    style=ft.ButtonStyle(bgcolor=_ERR),
                    on_click=_save,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg_ref[0])

    def _update_writeoff_field(
        self, ctx_id: str, wo_row: dict, field: str, new_value
    ) -> None:
        """Оновлює одне поле рядка списання в кеші та відкладає збереження в БД."""
        wo_id = str(wo_row.get("id") or "").strip()
        seq = int(wo_row.get("seq_no") or 0)
        # Update in-memory cache — also try seq_no fallback when id no longer
        # matches (happens after outbox flush clears the local placeholder).
        _cache_hit = False
        for r in self._local_writeoff_cache.get(ctx_id) or []:
            if str(r.get("id") or "") == wo_id:
                r[field] = new_value
                if field in ("pack_qty", "goods_qty"):
                    r["qty"] = float(r.get("pack_qty") or 0)
                _cache_hit = True
                break
        if not _cache_hit and seq:
            for r in self._local_writeoff_cache.get(ctx_id) or []:
                if int(r.get("seq_no") or 0) == seq:
                    r[field] = new_value
                    if field in ("pack_qty", "goods_qty"):
                        r["qty"] = float(r.get("pack_qty") or 0)
                    wo_id = str(r.get("id") or "").strip()
                    break
        # Re-queue save with updated fields
        merged = dict(wo_row)
        for r in self._local_writeoff_cache.get(ctx_id) or []:
            if str(r.get("id") or "") == wo_id:
                merged = dict(r)
                break
        # Always apply new_value — guards against stale snapshot when cache entry
        # was removed after flush but before the UI re-rendered with a fresh snapshot.
        merged[field] = new_value
        if field in ("pack_qty", "goods_qty"):
            merged["qty"] = float(merged.get("pack_qty") or 0)
        # Look up region_id from the context to match the UNIQUE constraint
        # (context_id, product_id, seq_no, manager_id, region_id) used on the
        # original INSERT.  Using _region_id_live (always "") would create a
        # duplicate row instead of updating the existing one.
        _ctx_region = next(
            (
                str((_c or {}).get("region_id") or "").strip()
                for _c in (self._packaging_items or [])
                if str((_c or {}).get("id") or "").strip() == ctx_id
            ),
            str(getattr(self, "_region_id_live", "") or ""),
        )
        save_kwargs = {
            "context_id": ctx_id,
            "pack_qty": float(merged.get("pack_qty") or 0),
            "goods_qty": float(merged.get("goods_qty") or 0),
            "qty": float(merged.get("pack_qty") or 0),
            "seq_no": seq,
            "manager_id": self.manager_id,
            "region_id": _ctx_region,
            "company_id": self.company_id,
            "comment": str(merged.get("comment") or "Списання"),
            "invoice_no": str(merged.get("invoice_no") or ""),
            "order_date": merged.get("order_date") or None,
        }
        self._queue_pack_writeoff_save(save_kwargs)
        self._refresh_pack_ctx_inplace(ctx_id)

    def _open_comment_editor(
        self,
        current_value: str,
        on_save_fn,
        title: str = "Коментар",
    ) -> None:
        """Generic inline comment editor — small dialog with a multiline TextField."""
        page = self._page()
        if not page:
            return
        dlg_ref: list = [None]

        def _close():
            with contextlib.suppress(Exception):
                self._close_dlg(dlg_ref[0])

        tf = ft.TextField(
            value=current_value,
            multiline=True,
            min_lines=2,
            max_lines=5,
            autofocus=True,
            expand=True,
        )

        def _save(_=None):
            _close()
            on_save_fn(tf.value or "")

        tf.on_submit = _save
        dlg_ref[0] = ft.AlertDialog(
            modal=True,
            title=ft.Text(title, size=13),
            content=ft.Container(
                ft.Row([tf], expand=True),
                width=320,
                padding=ft.padding.symmetric(vertical=4),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: _close()),
                ft.FilledButton("Зберегти", on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg_ref[0])

    def _persist_opening_comment(self, context_id: str, new_comment: str) -> None:
        """Save a new comment for the opening balance row (outbox → DB)."""
        cid = str(context_id or "").strip()
        if not cid or self.subordinate_ro:
            return
        cur = dict(self._local_opening_cache.get(cid) or {})
        pk = float(cur.get("stock_packaging_supplier_qty") or 0)
        rg = float(cur.get("stock_goods_supplier_qty") or 0)
        bg = float(cur.get("balance_amount") or 0)
        bccy = str(cur.get("balance_currency") or "USD").strip().upper() or "USD"
        sh = float(cur.get("opening_shipped_goods_qty") or 0)
        pd_amt = float(cur.get("balance_paid_amount") or 0)
        save_kwargs = {
            "context_id": cid,
            "stock_packaging_supplier_qty": pk,
            "stock_goods_supplier_qty": rg,
            "balance_amount": bg,
            "balance_currency": bccy,
            "opening_shipped_goods_qty": sh,
            "balance_paid_amount": pd_amt,
            "comment": str(new_comment or ""),
            "company_id": self.company_id,
        }
        self._queue_pack_opening_save(save_kwargs)
        self._refresh_pack_sync_status()
        if not self._refresh_pack_ctx_inplace(cid):
            self._schedule_debounced_reload(0.1)

    def _wrap_context_product_panel(
        self, header: ft.Control, body: ft.Control
    ) -> ft.Container:
        """Контекст «товар»: без вкладеного ExpansionTile — на web вкладені tile + маса TextField дають порожню сіру плиту."""
        stripe = _TEAL
        hdr_bg = _ALT
        outline_clr = _HDR
        return ft.Container(
            margin=ft.margin.only(bottom=4),
            border=ft.border.all(1, outline_clr),
            border_radius=10,
            bgcolor=_PANEL,
            clip_behavior=ft.ClipBehavior.NONE,
            content=ft.Column(
                [
                    ft.Container(
                        bgcolor=hdr_bg,
                        padding=ft.padding.symmetric(horizontal=8, vertical=5),
                        content=ft.Row(
                            [
                                ft.Container(width=5, bgcolor=stripe, border_radius=3),
                                ft.Container(content=header, expand=True),
                            ],
                            spacing=5,
                            vertical_alignment=ft.CrossAxisAlignment.START,
                            intrinsic_height=True,
                        ),
                    ),
                    body,
                ],
                spacing=0,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        )

    def _make_collapsible(
        self,
        header: ft.Control,
        body: ft.Control,
        *,
        expanded: bool,
        on_expanded,
        tier: str = "context",
    ) -> ft.Container:
        tier = str(tier or "").strip().lower()
        # Візуальна ієрархія: постачальник пакування (верхній) vs контекст SKU (внутрішній).
        stripe = "#4F46E5"
        hdr_bg_exp = _SOFT_I
        hdr_bg_coll = _SOFT_I
        outline_clr = "#C7D2FE"
        outline_w = 1.5
        if tier == "box":
            stripe = "#5B21B6"
            hdr_bg_exp = _SOFT_V
            hdr_bg_coll = _SOFT_V
            outline_clr = "#DDD6FE"
            outline_w = 1.2
        elif tier == "context":
            stripe = _TEAL
            hdr_bg_exp = _ALT
            hdr_bg_coll = _ALT
            outline_clr = _HDR
            outline_w = 1.0

        body_col = ft.Column([body], visible=bool(expanded), spacing=0)
        arrow = ft.Icon(
            ft.Icons.EXPAND_LESS if expanded else ft.Icons.EXPAND_MORE,
            size=16,
            color=stripe,
        )
        outer = ft.Container(
            border=ft.border.all(outline_w, outline_clr),
            border_radius=10,
            bgcolor=_PANEL,
            clip_behavior=ft.ClipBehavior.NONE,
        )

        def _toggle(_):
            body_col.visible = not body_col.visible
            arrow.name = (
                ft.Icons.EXPAND_LESS if body_col.visible else ft.Icons.EXPAND_MORE
            )
            try:
                on_expanded(expanded=bool(body_col.visible))
            except TypeError:
                try:
                    on_expanded()
                except Exception:
                    pass
            except Exception:
                pass
            try:
                outer.update()
            except Exception:
                pass

        header_ct = ft.Container(
            content=ft.Row(
                [
                    ft.Container(width=5, bgcolor=stripe, border_radius=3),
                    ft.Container(content=header, expand=True),
                    arrow,
                ],
                spacing=8,
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                intrinsic_height=True,
            ),
            on_click=_toggle,
            bgcolor=hdr_bg_exp if expanded else hdr_bg_coll,
            padding=ft.padding.symmetric(horizontal=8, vertical=6),
            border_radius=ft.border_radius.only(top_left=10, top_right=10),
        )
        outer.content = ft.Column(
            [
                header_ct,
                ft.Container(
                    content=body_col,
                    padding=ft.padding.only(left=6, right=6, bottom=8, top=3),
                ),
            ],
            spacing=0,
        )
        return outer

    def _pack_kpi_chip(
        self, label: str, badge_text: str, sign: float | None
    ) -> ft.Container:
        """Компактний лічильник у стилі «Замовлення»: >0 — зелений; 0 і «−» — червоний; без знагу — білий/сірий."""
        if sign is None:
            bg_chip, fg_chip = _PANEL, _TEXT2
        elif float(sign) > 0:
            bg_chip, fg_chip = _OK_BG, "#16A34A"
        else:
            bg_chip, fg_chip = "#FEE2E2", _ERR
        badge = str(badge_text).strip()
        short_b = badge[:32] + ("…" if len(badge) > 32 else "")
        return ft.Container(
            bgcolor=bg_chip,
            border_radius=6,
            padding=ft.padding.symmetric(horizontal=6, vertical=2),
            content=ft.Row(
                [
                    ft.Text(str(label), size=9, color=fg_chip),
                    ft.Container(
                        bgcolor=fg_chip,
                        border_radius=10,
                        padding=ft.padding.symmetric(horizontal=5, vertical=1),
                        content=ft.Text(short_b, size=9, color=_PANEL, weight="bold"),
                    ),
                ],
                spacing=4,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _pack_kpi_chip_sep(self) -> ft.Container:
        return ft.Container(
            width=1,
            height=20,
            bgcolor=_BORDER,
            margin=ft.margin.symmetric(horizontal=4),
        )

    def _supplier_name(self, supplier_id: str, suppliers: list[dict]) -> str:
        sid = str(supplier_id or "").strip()
        if not sid:
            return "—"
        for s in suppliers or []:
            if not isinstance(s, dict):
                continue
            if str(s.get("id") or "").strip() == sid:
                return str(s.get("name") or sid).strip() or sid
        return sid

    def _packaging_currency(self, pack_row: dict | None) -> str:
        if not isinstance(pack_row, dict):
            return "USD"
        cur = ""
        try:
            sup = (
                pack_row.get("suppliers")
                if isinstance(pack_row.get("suppliers"), dict)
                else {}
            )
            cur = str(sup.get("currency") or "").strip()
        except Exception:
            cur = ""
        return (cur or "USD").upper()

    @staticmethod
    def _supply_row_cost_amount(row: dict, pack_unit_price: float) -> float:
        """Вартість рядка supply: з поля cost_amount, інакше qty_ordered × ціна пакування (як у журналі)."""
        c = _safe_float(row.get("cost_amount"), None)
        if c is not None:
            return float(c)
        q = float(_safe_float(row.get("qty_ordered"), 0) or 0)
        px = float(pack_unit_price or 0)
        if q and px:
            return float(q * px)
        return 0.0

    def _packaging_context_balance_net(
        self,
        opening: dict | None,
        orders: list,
        pack_row: dict | None,
    ) -> float:
        """Грошовий баланс контексту: (баланс_початок − сплачено_початок) + Σ cost(supply) − Σ paid(supply)."""
        leg = _opening_ledger_from_row(opening)
        base = float(leg.get("bal_net") or 0)
        px = 0.0
        if isinstance(pack_row, dict):
            px = float(_safe_float(pack_row.get("price"), 0) or 0)
        sum_c = 0.0
        sum_p = 0.0
        for row in orders or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("row_kind") or "").strip() != "supply":
                continue
            sum_c += self._supply_row_cost_amount(row, px)
            sum_p += float(_safe_float(row.get("paid_amount"), 0) or 0)
        return base + sum_c - sum_p

    def _packaging_journal_final_stocks(
        self,
        opening: dict | None,
        orders: list,
        writeoffs: list | None = None,
    ) -> tuple[float, float]:
        """Поточні залишки пакування та товару після всіх рядків журналу."""
        oph = opening if isinstance(opening, dict) else {}
        rows_in = [r for r in (orders or []) if isinstance(r, dict)]
        wo_rows = [r for r in (writeoffs or []) if isinstance(r, dict)]
        rows_chrono = sorted(rows_in + wo_rows, key=_packaging_supply_chrono_sort_key)
        leg0 = _opening_ledger_from_row(oph)
        if _opening_journal_has_snapshot(oph):
            pack_run = float(leg0.get("eff_pack") or 0)
            goods_run = float(leg0.get("eff_goods") or 0)
        else:
            pack_run = 0.0
            goods_run = 0.0
        for rr in rows_chrono:
            rk0 = str(rr.get("row_kind") or "").strip()
            rid0 = str(rr.get("id") or "").strip()
            if not rid0:
                continue
            if rk0 == "supply":
                qv = float(rr.get("qty_ordered") or 0)
                sh = max(0.0, float(rr.get("shipped_goods_supplier_qty") or 0))
                pack_run += qv - sh
                goods_run += sh
            elif rk0 == "goods":
                qv = float(rr.get("qty_ordered") or 0)
                goods_run -= qv
            elif rk0 == "writeoff":
                pack_run -= float(rr.get("pack_qty") or rr.get("qty") or 0)
                goods_run -= float(rr.get("goods_qty") or 0)
        return pack_run, goods_run

    def _rollup_balance_entries_to_usd(
        self, entries: list[tuple[float, str]]
    ) -> float | None:
        """Сума балансів у USD (через NBU), якщо для всіх валют відомий курс; інакше None."""
        if not entries:
            return None
        s = 0.0
        for amt, ccy in entries:
            c = str(ccy or "USD").upper().strip() or "USD"
            if c == "USD":
                s += float(amt)
                continue
            u = self._packaging_amount_to_usd(float(amt), c)
            if u is None:
                return None
            s += float(u)
        return s

    def _rollup_balance_entries_to_ccy(
        self, entries: list[tuple[float, str]], target_ccy: str
    ) -> float | None:
        """Σ усіх пар (сума, валюта), перерахованих у target_ccy через курси NBU до UAH."""
        t = str(target_ccy or "").upper().strip()
        if not entries or not t:
            return None
        fx = self._fx_uah_per_unit()
        uah_t = _safe_float(fx.get(t), None)
        if uah_t is None or uah_t <= 0:
            return None
        s = 0.0
        for amt, ccy in entries:
            c = str(ccy or "USD").upper().strip() or "USD"
            uah_c = _safe_float(fx.get(c), None)
            if uah_c is None or uah_c <= 0:
                return None
            try:
                s += float(amt) * float(uah_c) / float(uah_t)
            except Exception:
                return None
        return s

    @staticmethod
    def _aggregate_balances_by_currency(
        entries: list[tuple[float, str]],
    ) -> dict[str, float]:
        acc: dict[str, float] = defaultdict(float)
        for amt, ccy in entries:
            c = str(ccy or "USD").upper().strip() or "USD"
            try:
                acc[c] += float(amt)
            except Exception:
                continue
        return dict(acc)

    def _native_and_usd_balance_lines_for_entries(
        self, entries: list[tuple[float, str]]
    ) -> tuple[str, str | None]:
        """Текст для колонок «Баланс» / «Баланс $»; друге значення None — колонку $ не показувати (усі суми вже в USD)."""
        if not entries:
            return "—", None
        ccys = {str(c or "USD").upper().strip() or "USD" for _, c in entries}
        if len(ccys) == 1:
            c0 = next(iter(ccys))
            tot = float(sum(float(a) for a, _ in entries))
            native = f"{_fmt_money(tot)} {c0} {_packaging_balance_direction_hint(tot)}".strip()
            if c0 == "USD":
                return native, None
            ust = self._packaging_amount_to_usd(tot, c0)
            usd_body = (
                f"{_fmt_money(ust)} USD {_packaging_balance_direction_hint(ust)}".strip()
                if ust is not None
                else "—"
            )
            return native, usd_body
        rollup = self._rollup_balance_entries_to_usd(entries)
        native = "Декілька валют"
        if rollup is None:
            return native, "—"
        return (
            native,
            f"{_fmt_money(rollup)} USD {_packaging_balance_direction_hint(rollup)}".strip(),
        )

    def _packaging_remainder_strip_ui(
        self,
        *,
        headline: str | None,
        pack_qty: float,
        goods_qty: float,
        native_balance_body: str,
        usd_balance_body: str | None,
        outer_bg: str = _ALT,
        outer_border: str = _HDR,
        tree_compact: bool = False,
    ) -> ft.Control:
        """Один макет клітинок для KPI «Поточний залишок», блоків постачальник/пакування та картки товару."""
        cls = self.__class__
        pad_h = int(cls.REMAINDER_PAD_H)
        pad_v = int(cls.REMAINDER_PAD_V)
        ttl = float(cls.REMAINDER_LBL_SZ)
        bod = float(cls.REMAINDER_VAL_SZ)
        row_spacing = int(cls.REMAINDER_ROW_GAP)
        outer_pad = int(cls.REMAINDER_OUTER_PAD)
        tl_radius = int(cls.REMAINDER_TILE_RADIUS)
        fr_radius = int(cls.REMAINDER_FRAME_RADIUS)
        w_min = int(cls.REMAINDER_TILE_W_MIN)
        w_max = int(cls.REMAINDER_TILE_W_MAX)
        vw_frac = float(cls.REMAINDER_VW_BUDGET_FRAC)
        col_inner_gap = 2

        if tree_compact:
            pm = float(cls.TREE_REMAINDER_PAD_MUL)
            pad_h = max(2, int(round(pad_h * pm)))
            pad_v = max(2, int(round(pad_v * pm)))
            ttl = max(6.0, ttl - float(cls.TREE_REMAINDER_LBL_DELTA))
            ttl += float(cls.TREE_REMAINDER_LBL_PLUS)
            bod = max(7.5, bod - float(cls.TREE_REMAINDER_VAL_DELTA))
            row_spacing = max(2, row_spacing - int(cls.TREE_REMAINDER_GAP_TRIM))
            outer_pad = max(
                2, int(round(outer_pad * float(cls.TREE_REMAINDER_OUTER_PAD_MUL)))
            )
            col_inner_gap = 1
            tl_radius = max(4, tl_radius - 1)
            fr_radius = max(5, fr_radius - 1)

        specs: list[tuple[str, str, str, str]] = [
            (self.t("pack_stock.title", "Склад пакування"), f"{_fmt_qty(pack_qty)} шт.", _SKY_BG, "#0369A1"),
            ("Склад товару", f"{_fmt_qty(goods_qty)} шт.", "#FFF7ED", "#C2410C"),
            ("Баланс", native_balance_body, _OK_BG, "#047857"),
        ]
        if usd_balance_body is not None:
            specs.append(("Баланс $", usd_balance_body, _SOFT_I, _INDIGO))

        vw_eff = int(getattr(self, "_last_viewport_w", None) or 980)
        total_budget = max(220, int(vw_eff * vw_frac))
        n_tile = len(specs)
        tile_w = max(
            w_min,
            min(
                w_max,
                int((total_budget - row_spacing * max(0, n_tile - 1)) / max(1, n_tile)),
            ),
        )
        if tree_compact:
            twm = float(cls.TREE_REMAINDER_WIDTH_MUL) * float(
                cls.TREE_REMAINDER_WIDTH_BOOST
            )
            tile_w = max(43, int(round(tile_w * twm)))

        tiles: list = []
        for lbl, val, bg, brd in specs:
            # Enlarge Balance tiles by ~30% to emphasize financial columns
            w_tile = int(tile_w)
            try:
                if str(lbl or "").strip().startswith("Баланс"):
                    w_tile = min(w_max, int(round(float(tile_w) * 1.3)))
            except Exception:
                w_tile = int(tile_w)

            tiles.append(
                ft.Container(
                    width=w_tile,
                    padding=ft.padding.symmetric(horizontal=pad_h, vertical=pad_v),
                    bgcolor=bg,
                    border_radius=tl_radius,
                    border=ft.border.all(1, brd),
                    clip_behavior=ft.ClipBehavior.NONE,
                    content=ft.Column(
                        [
                            ft.Text(
                                str(lbl),
                                size=ttl,
                                weight=ft.FontWeight.W_600,
                                color=_TEXT,
                                no_wrap=False,
                                max_lines=2,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                str(val),
                                size=bod,
                                weight=ft.FontWeight.W_700,
                                color=_TEXT,
                                no_wrap=False,
                                max_lines=4,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                        ],
                        tight=True,
                        spacing=col_inner_gap,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )

        inner = ft.Row(
            tiles,
            spacing=row_spacing,
            run_spacing=row_spacing,
            wrap=True,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )

        if headline:
            hs = float(cls.REMAINDER_HEADLINE_SZ)
            if tree_compact:
                hs = max(6.5, hs - 1.0)
            hdr = ft.Text(
                headline,
                size=hs,
                weight=ft.FontWeight.W_800,
                color=_TEXT,
            )
            col = ft.Column([hdr, inner], spacing=max(2, row_spacing), tight=True)
        else:
            col = inner

        return ft.Container(
            padding=ft.padding.all(outer_pad),
            bgcolor=outer_bg,
            border_radius=fr_radius,
            border=ft.border.all(1, outer_border),
            clip_behavior=ft.ClipBehavior.NONE,
            alignment=ft.Alignment(-1.0, 0.0),
            content=col,
        )

    def _fx_uah_per_unit(self) -> dict:
        """Грубий курс до UAH (як «Взаєморозрахунки») для оцінки суми у USD.

        Cache-only — NEVER performs blocking network I/O. If the cache is
        missing/stale, a background refresh is scheduled and the best data
        we have so far (possibly empty on the very first call) is returned
        immediately. Same fix as payments_view.py's `_fx_uah_per_unit`: this
        used to do the NBU HTTP fetch INLINE (6s timeout) whenever the 900s
        cache expired, which could freeze the whole Flet event loop if
        called from a synchronous save/blur path."""
        try:
            holder = (
                self._fx_uah_cache
                if isinstance(getattr(self, "_fx_uah_cache", None), dict)
                else {"ts": 0.0, "rates": {}}
            )
            now = time.time()
            rates = holder.get("rates") or {}
            is_fresh = bool(rates) and (now - float(holder.get("ts") or 0.0)) < 900
            if not is_fresh:
                self._fx_uah_schedule_background_refresh()
            return rates
        except Exception:
            return {}

    def _fx_uah_schedule_background_refresh(self) -> None:
        """Fire-and-forget refresh of FX rates off the UI thread. Safe to
        call often — a flag prevents overlapping refreshes."""
        if getattr(self, "_fx_uah_refresh_in_flight", False):
            return
        page = self._page()
        if not (page and hasattr(page, "run_task")):
            return
        self._fx_uah_refresh_in_flight = True

        async def _refresh():
            try:
                rates = await asyncio.to_thread(self._fx_uah_fetch_blocking)
                self._fx_uah_cache = {"ts": time.time(), "rates": rates}
            except Exception:
                logger.exception("Background FX rate refresh failed")
            finally:
                self._fx_uah_refresh_in_flight = False

        with contextlib.suppress(Exception):
            page.run_task(_refresh)

    def _fx_uah_fetch_blocking(self) -> dict:
        """Actual network fetch. Blocking — only ever call from a worker
        thread via `asyncio.to_thread` (see `_fx_uah_schedule_background_refresh`),
        never directly from the UI thread."""

        def _get_json(url: str) -> dict:
            with urllib.request.urlopen(url, timeout=6) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")

        def _from_nbu() -> dict:
            data = _get_json(
                "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
            )
            if not isinstance(data, list):
                return {}
            out: dict[str, float] = {"UAH": 1.0}
            for row in data:
                if not isinstance(row, dict):
                    continue
                cc = str(row.get("cc") or "").upper().strip()
                rate = _safe_float(row.get("rate"), None)
                unit = _safe_float(row.get("unit"), 1.0) or 1.0
                if not cc or rate is None or rate <= 0 or unit <= 0:
                    continue
                out[cc] = float(rate) / float(unit)
            return out

        out: dict = {}
        for fetcher in (_from_nbu,):
            try:
                out = fetcher() or {}
                if (
                    out
                    and _safe_float(out.get("USD"), None)
                    and _safe_float(out.get("UAH"), None)
                ):
                    break
            except Exception:
                pass
        return out

    def _packaging_amount_to_usd(
        self, amount: float | None, ccy: str
    ) -> float | None:
        if amount is None:
            return None
        c = str(ccy or "").upper().strip() or "USD"
        if c == "USD":
            return float(amount)
        fx = self._fx_uah_per_unit()
        uah_ccy = _safe_float(fx.get(c), None)
        uah_usd = _safe_float(fx.get("USD"), None)
        if uah_ccy is None or uah_usd is None or uah_usd <= 0:
            return None
        rate = float(uah_ccy) / float(uah_usd)
        try:
            return float(amount) * float(rate)
        except Exception:
            return None

    def _persist_opening_transfer_or_paid(
        self,
        context_id: str,
        *,
        shipped_goods_qty: float | None = None,
        balance_paid_amount: float | None = None,
    ) -> None:
        cid = str(context_id or "").strip()
        if not cid or self.subordinate_ro:
            return
        # Use local cache first (already up-to-date from previous saves); fall back to DB.
        cur = dict(self._local_opening_cache.get(cid) or {})
        if not cur:
            with contextlib.suppress(Exception):
                cur = dict(self.db.get_packaging_supply_opening(cid) or {})
        if not cur:
            return
        pk = float(cur.get("stock_packaging_supplier_qty") or 0)
        rg = float(cur.get("stock_goods_supplier_qty") or 0)
        bg = float(cur.get("balance_amount") or 0)
        bccy = str(cur.get("balance_currency") or "USD").strip().upper() or "USD"
        sh = float(cur.get("opening_shipped_goods_qty") or 0)
        pd = float(cur.get("balance_paid_amount") or 0)
        if shipped_goods_qty is not None:
            try:
                sh = max(0.0, min(float(shipped_goods_qty), pk))
            except Exception:
                sh = max(0.0, min(float(sh or 0), pk))
        if balance_paid_amount is not None:
            try:
                pd = max(0.0, float(balance_paid_amount))
            except Exception:
                pd = max(0.0, float(pd))
        cmt_op = str(cur.get("comment") or "")
        save_kwargs = {
            "context_id": cid,
            "stock_packaging_supplier_qty": pk,
            "stock_goods_supplier_qty": rg,
            "balance_amount": bg,
            "balance_currency": bccy,
            "opening_shipped_goods_qty": sh,
            "balance_paid_amount": pd,
            "comment": cmt_op,
            "company_id": self.company_id,
        }
        self._queue_pack_opening_save(save_kwargs)
        self._refresh_pack_sync_status()
        if not self._refresh_pack_ctx_inplace(cid):
            self._schedule_debounced_reload(0.1)

    def _apply_local_supply_order_patch(self, order_id: str, payload: dict) -> None:
        oid = str(order_id or "").strip()
        if not oid or not isinstance(payload, dict):
            return
        for orders in self._local_orders_cache.values():
            for row in orders:
                if not isinstance(row, dict):
                    continue
                if str(row.get("id") or "").strip() == oid:
                    row.update(payload)
                    if (
                        "qty_ordered" in payload
                        and str(row.get("row_kind") or "").strip() == "goods"
                    ):
                        with contextlib.suppress(Exception):
                            self._sync_qty_to_shared_orders_map(
                                row, payload["qty_ordered"]
                            )
                    return

    @staticmethod
    def _resolve_pack_line_idx(items: list, comment: str, line_key_raw: Any) -> int:
        n = len(items)
        if line_key_raw is not None and str(line_key_raw).strip() != "":
            try:
                parsed = int(str(line_key_raw).strip())
                if 0 <= parsed < n:
                    return parsed
            except (TypeError, ValueError):
                pass
        pc = str(comment or "").strip().lower()
        if pc:
            for i, it in enumerate(items):
                if not isinstance(it, dict):
                    continue
                pn = str(it.get("product_name") or "").strip().lower()
                if pn and (pn == pc or pn in pc or pc in pn):
                    return i
        return 0 if n == 1 else -1

    def _sync_qty_to_shared_orders_map(self, goods_row: dict, new_qty: Any) -> None:
        linked_oid = str(goods_row.get("linked_order_id") or "").strip()
        if not linked_oid or not self._shared_orders_map:
            return
        shared_order = self._shared_orders_map.get(linked_oid)
        if not isinstance(shared_order, dict):
            return
        items_src = shared_order.get("items") or []
        if not isinstance(items_src, list):
            return
        line_key_raw = goods_row.get("linked_order_line_key")
        comment = str(goods_row.get("comment") or "").strip()
        line_idx = self._resolve_pack_line_idx(items_src, comment, line_key_raw)
        if line_idx < 0 or line_idx >= len(items_src):
            return
        try:
            qv = float(str(new_qty or "0").replace(",", ".").strip())
        except Exception:
            qv = 0.0
        updated_order = dict(shared_order)
        updated_items = [dict(it) if isinstance(it, dict) else it for it in items_src]
        if isinstance(updated_items[line_idx], dict):
            updated_items[line_idx]["qty"] = qv
            updated_items[line_idx]["quantity"] = qv
        updated_order["items"] = updated_items
        updated_order["updated_at"] = datetime.utcnow().isoformat(timespec="seconds")
        self._shared_orders_map[linked_oid] = updated_order
        self._bump_sync_seq()

    def _persist_supply_order_patch_async(self, order_id: str, payload: dict) -> None:
        """Queue update in local outbox; DB sync is handled by background loop."""
        oid = str(order_id or "").strip()
        if not oid or not isinstance(payload, dict):
            return
        pl = dict(payload)
        self._queue_pack_order_patch(oid, pl)

    def _load_orders_and_opening_from_db(self) -> None:
        """Load supply orders + opening balances from DB for all known contexts.
        Called on full refresh; skipped when local file cache is still fresh."""
        new_cache: dict[str, list[dict]] = {}
        new_opening_cache: dict[str, dict] = {}
        for _ctx in self._packaging_items:
            _cid = str((_ctx or {}).get("id") or "").strip()
            if not _cid:
                continue
            try:
                _orders = self.db.list_packaging_supply_orders(_cid) or []
                new_cache[_cid] = [dict(o) for o in _orders if isinstance(o, dict)]
            except Exception:
                new_cache[_cid] = list(self._local_orders_cache.get(_cid) or [])
            try:
                _opening = self.db.get_packaging_supply_opening(_cid) or {}
                new_opening_cache[_cid] = (
                    dict(_opening) if isinstance(_opening, dict) else {}
                )
            except Exception:
                new_opening_cache[_cid] = dict(
                    self._local_opening_cache.get(_cid) or {}
                )
        self._local_orders_cache = new_cache
        self._local_opening_cache = new_opening_cache

        # Load write-offs from DB for all contexts
        new_writeoff_cache: dict[str, list[dict]] = {}
        _pending_keys = set(self._ops_outbox.keys())
        for _ctx in self._packaging_items:
            _cid = str((_ctx or {}).get("id") or "").strip()
            if not _cid:
                continue
            _rid_ctx = str((_ctx or {}).get("region_id") or "").strip()
            _wo_rows: list[dict] = []
            _db_ids: set[str] = set()
            with contextlib.suppress(Exception):
                _raw = (
                    self.db.get_packaging_writeoffs(
                        _cid, manager_id=self.manager_id, region_id=_rid_ctx
                    )
                    or []
                )
                _del_pend2 = self._local_writeoff_delete_pending
                for _r in _raw:
                    if isinstance(_r, dict):
                        _rid = str(_r.get("id") or "")
                        if not _rid:
                            continue
                        _db_ids.add(_rid)
                        if _rid in _del_pend2:
                            continue  # skip rows being deleted
                        _pack_qty = float(_r.get("pack_qty") or _r.get("qty") or 0)
                        _goods_qty = float(_r.get("goods_qty") or 0)
                        _wo_rows.append(
                            {
                                "id": _rid,
                                "context_id": _cid,
                                "row_kind": "writeoff",
                                "qty": _pack_qty,
                                "pack_qty": _pack_qty,
                                "goods_qty": _goods_qty,
                                "order_date": _r.get("order_date") or None,
                                "seq_no": _r.get("seq_no") or 0,
                                "invoice_no": str(_r.get("invoice_no") or ""),
                                "comment": str(_r.get("comment") or "Списання"),
                            }
                        )
            # Keep pending local entries (still in outbox, not yet in DB)
            _pending_local_ctx: list[dict] = []
            for _lr in self._local_writeoff_cache.get(_cid) or []:
                if isinstance(_lr, dict):
                    _lid = str(_lr.get("id") or "")
                    if _lid in _pending_keys and _lid not in _db_ids:
                        _pending_local_ctx.append(_lr)
            # Pending local edits shadow DB rows with the same seq_no
            _pending_seqs_ctx = {
                int(_lr.get("seq_no") or 0)
                for _lr in _pending_local_ctx
                if int(_lr.get("seq_no") or 0)
            }
            _wo_rows = [
                r
                for r in _wo_rows
                if int(r.get("seq_no") or 0) not in _pending_seqs_ctx
            ]
            _wo_rows.extend(_pending_local_ctx)
            new_writeoff_cache[_cid] = _wo_rows
        self._local_writeoff_cache = new_writeoff_cache

    @staticmethod
    def _orders_sig(orders: list[dict]) -> dict[str, str]:
        """Signature {order_id: updated_at_str} for fast change detection."""
        return {
            str(o.get("id") or ""): str(o.get("updated_at") or "")
            for o in (orders or [])
            if isinstance(o, dict) and str(o.get("id") or "").strip()
        }

    @staticmethod
    def _opening_sig(opening: dict) -> str:
        """Compact signature of opening balance fields for change detection."""
        d = opening if isinstance(opening, dict) else {}
        return "|".join(
            str(d.get(k) or "")
            for k in (
                "updated_at",
                "stock_packaging_supplier_qty",
                "stock_goods_supplier_qty",
                "balance_amount",
                "balance_paid_amount",
                "opening_shipped_goods_qty",
            )
        )

    def _incremental_sync_from_db(self) -> bool:
        """Compare each context's orders and opening with DB.
        Updates only the entries that differ in the in-memory cache.
        Also removes stale context entries no longer in _packaging_items.
        Returns True if anything changed."""
        current_ids = {
            str((c or {}).get("id") or "").strip()
            for c in (self._packaging_items or [])
            if str((c or {}).get("id") or "").strip()
        }
        changed = False

        for ctx in self._packaging_items or []:
            ctx_id = str((ctx or {}).get("id") or "").strip()
            if not ctx_id:
                continue

            # --- orders ---
            try:
                db_orders = [
                    dict(o)
                    for o in (self.db.list_packaging_supply_orders(ctx_id) or [])
                    if isinstance(o, dict)
                ]
                cached_orders = self._local_orders_cache.get(ctx_id)
                if cached_orders is None or self._orders_sig(
                    db_orders
                ) != self._orders_sig(cached_orders):
                    self._local_orders_cache[ctx_id] = db_orders
                    changed = True
                    logger.info("pack_stock: orders updated for ctx=%s", ctx_id)
            except Exception:
                if ctx_id not in self._local_orders_cache:
                    self._local_orders_cache[ctx_id] = []

            # --- opening balance ---
            try:
                db_opening = self.db.get_packaging_supply_opening(ctx_id) or {}
                db_opening = dict(db_opening) if isinstance(db_opening, dict) else {}
                cached_opening = self._local_opening_cache.get(ctx_id)
                if cached_opening is None or self._opening_sig(
                    db_opening
                ) != self._opening_sig(cached_opening):
                    self._local_opening_cache[ctx_id] = db_opening
                    changed = True
            except Exception:
                if ctx_id not in self._local_opening_cache:
                    self._local_opening_cache[ctx_id] = {}

            # --- write-offs (списання) ---
            try:
                rid_ctx = str((ctx or {}).get("region_id") or "").strip()
                raw_wo = (
                    self.db.get_packaging_writeoffs(
                        ctx_id, manager_id=self.manager_id, region_id=rid_ctx
                    )
                    or []
                )
                wo_rows: list[dict] = []
                db_ids: set[str] = set()
                _del_pending = self._local_writeoff_delete_pending
                for r in raw_wo:
                    if not isinstance(r, dict):
                        continue
                    _rid = str(r.get("id") or "")
                    if not _rid:
                        continue
                    db_ids.add(_rid)
                    if _rid in _del_pending:
                        continue  # skip rows being deleted
                    _pack_qty = float(r.get("pack_qty") or r.get("qty") or 0)
                    _goods_qty = float(r.get("goods_qty") or 0)
                    wo_rows.append(
                        {
                            "id": _rid,
                            "context_id": ctx_id,
                            "row_kind": "writeoff",
                            "qty": _pack_qty,
                            "pack_qty": _pack_qty,
                            "goods_qty": _goods_qty,
                            "order_date": r.get("order_date") or None,
                            "seq_no": r.get("seq_no") or 0,
                            "invoice_no": str(r.get("invoice_no") or ""),
                            "comment": str(r.get("comment") or "Списання"),
                        }
                    )
                # Keep only local entries that are still pending in outbox (not yet flushed)
                _pending_keys = set(self._ops_outbox.keys())
                _pending_local: list[dict] = []
                for r in self._local_writeoff_cache.get(ctx_id) or []:
                    if isinstance(r, dict):
                        _lid = str(r.get("id") or "")
                        if _lid in _pending_keys and _lid not in db_ids:
                            _pending_local.append(r)
                # Pending local edits shadow DB rows with the same seq_no
                _pending_seqs = {
                    int(r.get("seq_no") or 0)
                    for r in _pending_local
                    if int(r.get("seq_no") or 0)
                }
                wo_rows = [
                    r for r in wo_rows if int(r.get("seq_no") or 0) not in _pending_seqs
                ]
                wo_rows.extend(_pending_local)
                self._local_writeoff_cache[ctx_id] = wo_rows
            except Exception:
                if ctx_id not in self._local_writeoff_cache:
                    self._local_writeoff_cache[ctx_id] = []

        # Remove stale entries no longer in active contexts
        for stale in list(self._local_orders_cache.keys()):
            if stale not in current_ids:
                del self._local_orders_cache[stale]
                changed = True
        for stale in list(self._local_opening_cache.keys()):
            if stale not in current_ids:
                del self._local_opening_cache[stale]
        for stale in list(self._local_writeoff_cache.keys()):
            if stale not in current_ids:
                del self._local_writeoff_cache[stale]

        return changed

    def _load_packaging_metadata(self) -> None:
        """Load metadata (suppliers, products, packaging, contexts) from DB.
        Does NOT load per-context orders/opening — those come from cache or _load_orders_and_opening_from_db.
        """
        scope_region_ids = self._region_ids_for_scope()

        try:
            suppliers = (
                self.db.get_suppliers(
                    self.company_id,
                    None,
                    manager_id=self.manager_id,
                    force_refresh=True,
                )
                or []
            )
        except Exception:
            suppliers = []
        self._set_loading_text(f"Постачальники: {len(suppliers)}")
        try:
            products = (
                self.db.get_products(
                    self.company_id,
                    None,
                    manager_id=self.manager_id,
                    force_refresh=True,
                )
                or []
            )
        except Exception:
            products = []
        self._set_loading_text(f"Товари: {len(products)}")
        try:
            packaging = (
                self.db.get_packaging(
                    self.company_id,
                    None,
                    manager_id=self.manager_id,
                    force_refresh=True,
                )
                or []
            )
        except Exception:
            packaging = []
        self._set_loading_text(f"Пакування: {len(packaging)}")

        contexts: list[dict] = []
        for rid in scope_region_ids:
            rid_s = str(rid or "").strip()
            if not rid_s:
                continue
            with contextlib.suppress(Exception):
                contexts.extend(
                    self.db.sync_packaging_supply_contexts(
                        self.company_id,
                        rid_s,
                        manager_id=self.manager_id,
                    )
                    or []
                )
            with contextlib.suppress(Exception):
                self.db.reconcile_packaging_supply_goods_links(
                    self.company_id,
                    rid_s,
                    manager_id=self.manager_id,
                )

        self._packaging_suppliers = [s for s in suppliers if isinstance(s, dict)]
        self._packaging_products = [p for p in products if isinstance(p, dict)]
        self._packaging_boxes = [p for p in packaging if isinstance(p, dict)]
        self._packaging_items = [c for c in contexts if isinstance(c, dict)]

        self._packaging_suppliers_by_id = {
            str((s or {}).get("id") or "").strip(): s
            for s in self._packaging_suppliers
            if str((s or {}).get("id") or "").strip()
        }
        self._packaging_products_by_id = {
            str((p or {}).get("id") or "").strip(): p
            for p in self._packaging_products
            if str((p or {}).get("id") or "").strip()
        }
        self._packaging_boxes_by_id = {
            str((b or {}).get("id") or "").strip(): b
            for b in self._packaging_boxes
            if str((b or {}).get("id") or "").strip()
        }
        self._packaging_data = {
            "contexts": self._packaging_items,
            "suppliers": self._packaging_suppliers,
            "products": self._packaging_products,
            "boxes": self._packaging_boxes,
        }

        supplier_ids = {
            str((c or {}).get("packaging_supplier_id") or "").strip()
            for c in self._packaging_items
            if str((c or {}).get("packaging_supplier_id") or "").strip()
        }
        box_ids = {
            str((c or {}).get("packaging_id") or "").strip()
            for c in self._packaging_items
            if str((c or {}).get("packaging_id") or "").strip()
        }
        self._packaging_kpis = {
            "suppliers": len(supplier_ids),
            "boxes": len(box_ids),
            "contexts": len(self._packaging_items),
        }

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

    def _load_packaging_data(self):
        scope_region_ids = self._region_ids_for_scope()

        try:
            suppliers = (
                self.db.get_suppliers(
                    self.company_id,
                    None,
                    manager_id=self.manager_id,
                    force_refresh=True,
                )
                or []
            )
        except Exception:
            suppliers = []
        try:
            products = (
                self.db.get_products(
                    self.company_id,
                    None,
                    manager_id=self.manager_id,
                    force_refresh=True,
                )
                or []
            )
        except Exception:
            products = []
        try:
            packaging = (
                self.db.get_packaging(
                    self.company_id,
                    None,
                    manager_id=self.manager_id,
                    force_refresh=True,
                )
                or []
            )
        except Exception:
            packaging = []
        with contextlib.suppress(Exception):
            self._ref_data_pulse_ts = self.db.get_reference_data_pulse(self.company_id)

        contexts: list[dict] = []
        for rid in scope_region_ids:
            rid_s = str(rid or "").strip()
            if not rid_s:
                continue
            with contextlib.suppress(Exception):
                contexts.extend(
                    self.db.sync_packaging_supply_contexts(
                        self.company_id,
                        rid_s,
                        manager_id=self.manager_id,
                    )
                    or []
                )
            # Rebuild goods rows (товарні замовлення) from orders so that
            # series/product renames and order changes are reflected immediately.
            with contextlib.suppress(Exception):
                self.db.reconcile_packaging_supply_goods_links(
                    self.company_id,
                    rid_s,
                    manager_id=self.manager_id,
                )

        self._packaging_suppliers = [s for s in suppliers if isinstance(s, dict)]
        self._packaging_products = [p for p in products if isinstance(p, dict)]
        self._packaging_boxes = [p for p in packaging if isinstance(p, dict)]
        self._packaging_items = [c for c in contexts if isinstance(c, dict)]

        self._packaging_suppliers_by_id = {
            str((s or {}).get("id") or "").strip(): s
            for s in self._packaging_suppliers
            if str((s or {}).get("id") or "").strip()
        }
        self._packaging_products_by_id = {
            str((p or {}).get("id") or "").strip(): p
            for p in self._packaging_products
            if str((p or {}).get("id") or "").strip()
        }
        self._packaging_boxes_by_id = {
            str((b or {}).get("id") or "").strip(): b
            for b in self._packaging_boxes
            if str((b or {}).get("id") or "").strip()
        }

        self._packaging_data = {
            "contexts": self._packaging_items,
            "suppliers": self._packaging_suppliers,
            "products": self._packaging_products,
            "boxes": self._packaging_boxes,
        }

        self._load_orders_and_opening_from_db()

        supplier_ids = {
            str((c or {}).get("packaging_supplier_id") or "").strip()
            for c in self._packaging_items
            if str((c or {}).get("packaging_supplier_id") or "").strip()
        }
        box_ids = {
            str((c or {}).get("packaging_id") or "").strip()
            for c in self._packaging_items
            if str((c or {}).get("packaging_id") or "").strip()
        }
        self._packaging_kpis = {
            "suppliers": len(supplier_ids),
            "boxes": len(box_ids),
            "contexts": len(self._packaging_items),
        }

    async def _async_initial_load(self):
        import asyncio as _aio

        await _aio.sleep(0.01)
        if self._initial_load_in_progress:
            logger.info("pack_stock: _async_initial_load SKIPPED (already running)")
            return
        self._initial_load_in_progress = True
        try:
            logger.info("pack_stock: _async_initial_load START")
            self._set_pack_sync_status("syncing")

            # Flush any pending ops from previous session before loading fresh data.
            if self._ops_outbox:
                await self._flush_pack_outbox_async()

            if self._header_loading_indicator is not None:
                self._header_loading_indicator.visible = True
                with contextlib.suppress(Exception):
                    self._header_loading_indicator.update()
            if self._loading_box:
                self._loading_box.visible = True
                with contextlib.suppress(Exception):
                    self._loading_box.update()

            # 1. Always load metadata (suppliers, products, packaging, contexts) from DB.
            await _aio.to_thread(self._load_packaging_metadata)

            # 2. Load whatever is in the file cache (may be empty / stale).
            await _aio.to_thread(self._load_pack_orders_cache)

            # 3. Read current pulse BEFORE incremental sync (sync_packaging_supply_contexts
            #    in step 1 already wrote a new pulse — capture it here so the saved cache
            #    timestamp matches and resume() won't needlessly re-trigger a reload).
            db_pulse = await _aio.to_thread(self._pack_scope_pulse)

            # 4. Incremental sync: compare every context's orders+opening with DB,
            #    update only the entries that actually differ — cache = DB after this.
            _changed = await _aio.to_thread(self._incremental_sync_from_db)

            # Always save cache with current pulse so resume() pulse check stays stable.
            await _aio.to_thread(self._save_pack_orders_cache, db_pulse)
            self._pack_cache_pulse_ts = db_pulse
            if _changed:
                logger.info(
                    "pack_stock: incremental sync applied changes (pulse=%s)", db_pulse
                )
            else:
                logger.info(
                    "pack_stock: incremental sync — cache matches DB (pulse=%s)",
                    db_pulse,
                )

            # 5. Re-apply any pending (failed-to-flush) outbox items over fresh data.
            if self._ops_outbox:
                self._apply_outbox_to_local_cache()
            else:
                self._save_pack_outbox()  # clean up empty file

            self._render_tree(force_full=True)

            if self._header_loading_indicator is not None:
                self._header_loading_indicator.visible = False
                with contextlib.suppress(Exception):
                    self._header_loading_indicator.update()
            if self._loading_box:
                self._loading_box.visible = False
                with contextlib.suppress(Exception):
                    self._loading_box.update()
            self._refresh_pack_sync_status()
            self._pack_ui_ready = True
            logger.info("pack_stock: _async_initial_load DONE")
            pg_bg = self._page()
            if (
                pg_bg
                and hasattr(pg_bg, "run_task")
                and not getattr(self, "_bg_sync_loop_started", False)
            ):
                self._bg_sync_loop_started = True
                pg_bg.run_task(self._bg_sync_loop)
        except Exception:
            logger.exception("Packaging Stock initial load failed")
            self._set_pack_sync_status("error")
            if self._header_loading_indicator is not None:
                self._header_loading_indicator.visible = False
                with contextlib.suppress(Exception):
                    self._header_loading_indicator.update()
            if self._loading_box:
                with contextlib.suppress(Exception):
                    self._loading_box.visible = False
                    self._loading_box.update()
            with contextlib.suppress(Exception):
                self._tree_host.controls.clear()
                self._tree_host.controls.append(
                    self._text_wrap(
                        "Помилка завантаження даних Складу пакування. Перевірте лог.",
                        size=12,
                        color=_ERR_DK,
                    )
                )
                self._tree_host.update()
        finally:
            self._initial_load_in_progress = False

    # ──────────────────── shared orders map ──────────────────────────────────

    def _sync_from_shared_map(self) -> list:
        """Compare packaging_items and goods rows in local cache with shared map.
        Syncs linked_order_status and goods-row qty_ordered. Returns changed orders."""
        if not self._shared_orders_map:
            return []
        updated: list = []
        seen_status: set[str] = set()

        # 1. Sync linked_order_status on packaging_items (context level)
        for item in self._packaging_items or []:
            if not isinstance(item, dict):
                continue
            oid = str(item.get("linked_order_id") or "").strip()
            if not oid or oid in seen_status:
                continue
            seen_status.add(oid)
            shared = self._shared_orders_map.get(oid)
            if not isinstance(shared, dict):
                continue
            local_status = str(item.get("linked_order_status") or "").strip()
            shared_status = str(shared.get("status") or "").strip()
            if local_status != shared_status:
                item["linked_order_status"] = shared_status
                if shared not in updated:
                    updated.append(shared)

        # 2. Sync goods-row qty_ordered from shared map (Orders→Packaging direction)
        for orders in self._local_orders_cache.values():
            for row in orders:
                if not isinstance(row, dict):
                    continue
                if str(row.get("row_kind") or "").strip() != "goods":
                    continue
                oid = str(row.get("linked_order_id") or "").strip()
                if not oid:
                    continue
                shared_order = self._shared_orders_map.get(oid)
                if not isinstance(shared_order, dict):
                    continue
                items_src = shared_order.get("items") or []
                if not isinstance(items_src, list) or not items_src:
                    continue
                line_key_raw = row.get("linked_order_line_key")
                comment = str(row.get("comment") or "").strip()
                line_idx = self._resolve_pack_line_idx(items_src, comment, line_key_raw)
                if line_idx < 0 or line_idx >= len(items_src):
                    continue
                item_dict = items_src[line_idx]
                if not isinstance(item_dict, dict):
                    continue
                shared_qty = _safe_float(
                    (
                        item_dict.get("qty")
                        if item_dict.get("qty") is not None
                        else item_dict.get("quantity")
                    ),
                    None,
                )
                local_qty = _safe_float(row.get("qty_ordered"), None)
                if shared_qty is not None and shared_qty != local_qty:
                    row["qty_ordered"] = shared_qty
                    if shared_order not in updated:
                        updated.append(shared_order)

        return updated

    def _bump_sync_seq(self) -> None:
        try:
            seq = int(self._shared_orders_map.get("__sync_seq__", 0) or 0)
            self._shared_orders_map["__sync_seq__"] = seq + 1
        except Exception:
            pass

    def _purge_deleted_linked_orders(self) -> bool:
        """Remove in-memory data linked to orders deleted in Orders tab.
        Returns True if anything was purged."""
        deleted_ids = self._shared_orders_map.get("__deleted_ids__")
        if not deleted_ids or not isinstance(deleted_ids, list):
            return False
        deleted_set = set(deleted_ids)
        purged = False
        # Remove goods rows linked to deleted orders from local orders cache.
        for ctx_id in list(self._local_orders_cache.keys()):
            old = self._local_orders_cache[ctx_id]
            new = [
                r
                for r in old
                if not (
                    isinstance(r, dict)
                    and str(r.get("linked_order_id") or "").strip() in deleted_set
                )
            ]
            if len(new) < len(old):
                self._local_orders_cache[ctx_id] = new
                purged = True
        # Clear linked_order_id reference on packaging contexts.
        for item in self._packaging_items or []:
            if not isinstance(item, dict):
                continue
            loid = str(item.get("linked_order_id") or "").strip()
            if loid in deleted_set:
                item["linked_order_id"] = ""
                item["linked_order_status"] = ""
                purged = True
        return purged

    def _bg_apply_sync(self) -> None:
        if not self._pack_ui_ready:
            return
        self._purge_deleted_linked_orders()
        changed = self._sync_from_shared_map()
        if not changed:
            return
        # Refresh only already-rendered context sections — no full page rebuild.
        for _cid in list(self._pack_ctx_wrappers.keys()):
            with contextlib.suppress(Exception):
                self._refresh_pack_ctx_inplace(_cid)

    async def _bg_sync_loop(self) -> None:
        _last_pulse_check = 0.0
        while True:
            try:
                await asyncio.sleep(1.5)
                seq = int(self._shared_orders_map.get("__sync_seq__", 0) or 0)
                if seq != getattr(self, "_bg_sync_seq", 0):
                    self._bg_sync_seq = seq
                    self._bg_apply_sync()
                # Periodically poll DB pulses for changes made from another
                # device/session, even while this tab stays open the whole
                # time (not just when the user switches away and back).
                _now = time.monotonic()
                if self._pack_ui_ready and (_now - _last_pulse_check) >= 20.0:
                    _last_pulse_check = _now
                    await self._bg_pack_ref_data_check()
                    await self._bg_pack_orders_pulse_check()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def resume(self) -> None:
        """Called when switching back to this cached tab. Syncs order changes
        from shared map, and triggers full DB reload if remote data changed."""

        pg = self._page()
        _sticky = getattr(pg, "_view_sticky_header", None) if pg else None
        if (
            _sticky is not None
            and getattr(pg, "_active_view", None) == "show_pack_stock"
        ):
            _hr = getattr(self, "_sticky_header_ref", None)
            if _hr is not None:
                _sticky.controls.clear()
                _sticky.controls.append(_hr)
                with contextlib.suppress(Exception):
                    _sticky.update()
        if not self._pack_ui_ready:
            return

        # Explicit force-reload (Ctrl+F5 or navigation request) — the only
        # allowed full reload path.
        if getattr(self, "_force_reload", False):
            self._force_reload = False
            _cur_pg = self._page()
            if _cur_pg and hasattr(_cur_pg, "run_task"):
                with contextlib.suppress(Exception):
                    _cur_pg.run_task(self._async_initial_load)
            return

        # Shared-map sync: update in-memory state, then refresh only rendered
        # context sections — no full page rebuild, no DB pulse triggers.
        current_seq = int(self._shared_orders_map.get("__sync_seq__", 0) or 0)
        if current_seq != getattr(self, "_resume_last_seq", -1):
            self._resume_last_seq = current_seq
            self._purge_deleted_linked_orders()
            changed = self._sync_from_shared_map()
            if changed:
                for _cid in list(self._pack_ctx_wrappers.keys()):
                    with contextlib.suppress(Exception):
                        self._refresh_pack_ctx_inplace(_cid)

        with contextlib.suppress(Exception):
            if pg and hasattr(pg, "run_task"):
                pg.run_task(self._bg_pack_ref_data_check)

        with contextlib.suppress(Exception):
            if pg and hasattr(pg, "run_task"):
                pg.run_task(self._bg_pack_orders_pulse_check)

    async def _bg_pack_ref_data_check(self) -> None:
        """Background reference-data pulse check — Товари/Постачальники/
        Пакування/Категорії can change from another tab (e.g. a product
        renamed in Товари, which changes its "Кат:"/"Постач:" disambiguation
        suffix here too). Mirrors the same check already used in
        orders_view.py / cost_price_orders_table.py / cost_price_stock_page.py.

        Called both right after resume() (tab switch) AND periodically from
        _bg_sync_loop (~every 20s) so a change made on another device shows
        up here even if we never switch away from this tab."""
        try:
            new_pulse = await asyncio.to_thread(
                self.db.get_reference_data_pulse, self.company_id
            )
            if new_pulse > int(getattr(self, "_ref_data_pulse_ts", 0) or 0):
                _pg2 = self._page()
                if _pg2 and hasattr(_pg2, "run_task"):
                    with contextlib.suppress(Exception):
                        _pg2.run_task(self._async_initial_load)
        except Exception:
            pass

    async def _bg_pack_orders_pulse_check(self) -> None:
        """Background "packaging" scope pulse check — this is the DB signal
        bumped by reconcile_packaging_supply_goods_links() whenever an
        order is upserted (database.py upsert_order). Without this, a new
        order created in the Orders tab (whose outbox flush lands ~45s
        later, bumping this pulse) was never picked up by an already-open
        Packaging Stock tab — _sync_from_shared_map only patches rows that
        already exist locally, it can't discover a brand-new linked row.

        Called both right after resume() (tab switch) AND periodically from
        _bg_sync_loop (~every 20s) so a change made on another device shows
        up here even if we never switch away from this tab."""
        try:
            new_pulse = await asyncio.to_thread(self._pack_scope_pulse)
            if new_pulse <= int(getattr(self, "_pack_cache_pulse_ts", 0) or 0):
                return
            changed = await asyncio.to_thread(self._incremental_sync_from_db)
            await asyncio.to_thread(self._save_pack_orders_cache, new_pulse)
            self._pack_cache_pulse_ts = new_pulse
            if changed:
                self._render_tree(force_full=True)
        except Exception:
            pass

    def apply_text_search(self, query: str) -> None:
        """Ctrl+F row filter: hide товар groups with no matching orders."""
        self._search_text_query = str(query or "").strip()
        self._render_tree(force_full=True)

    def clear_text_search(self) -> None:
        self.apply_text_search("")

    def _render_tree(self, force_full: bool = False):
        pg = self._page()
        if not pg:
            logger.warning(
                "pack_stock: _render_tree called but page is None; hiding loading box to avoid spinner"
            )
            if self._loading_box:
                try:
                    self._loading_box.visible = False
                    self._loading_box.update()
                except Exception:
                    pass
            return

        logger.info(
            "pack_stock: _render_tree START force_full=%s contexts=%s",
            force_full,
            len(self._packaging_items or []),
        )

        if self._loading_box:
            # Ensure loading box is visible while rendering, then hide it.
            self._loading_box.visible = True
            with contextlib.suppress(Exception):
                self._loading_box.update()

        if force_full:
            self._tree_host.controls.clear()
            self._pack_ctx_wrappers.clear()

            contexts = [c for c in (self._packaging_items or []) if isinstance(c, dict)]
            boxes_by_id = self._packaging_boxes_by_id or {}
            products_by_id = self._packaging_products_by_id or {}
            suppliers = self._packaging_suppliers or []

            # Apply quick_filter from home KPI cards.
            _qf = self.quick_filter
            if _qf in {"packaging_low_stock", "packaging_supplier_debt"}:
                filtered_ctxs = []
                for _ctx in contexts:
                    _cid = str(_ctx.get("id") or "").strip()
                    if not _cid:
                        continue
                    _opening = dict(self._local_opening_cache.get(_cid) or {})
                    if not _opening:
                        with contextlib.suppress(Exception):
                            _opening = dict(
                                self.db.get_packaging_supply_opening(_cid) or {}
                            )
                    _orders_c = list(self._local_orders_cache.get(_cid) or [])
                    _wo_c = list(self._local_writeoff_cache.get(_cid) or [])
                    if _qf == "packaging_low_stock":
                        ep, eg = self._packaging_journal_final_stocks(
                            _opening, _orders_c, _wo_c
                        )
                        _pack_row_low = (
                            boxes_by_id.get(str(_ctx.get("packaging_id") or "")) or {}
                        )
                        _optimal_qty = (
                            _safe_float(_pack_row_low.get("optimal_qty"), 5000.0)
                            or 5000.0
                        )
                        if float(ep or 0) + float(eg or 0) < _optimal_qty:
                            filtered_ctxs.append(_ctx)
                    elif _qf == "packaging_supplier_debt":
                        _pack_row = (
                            boxes_by_id.get(str(_ctx.get("packaging_id") or "")) or {}
                        )
                        bn = self._packaging_context_balance_net(
                            _opening, _orders_c, _pack_row
                        )
                        if float(bn or 0) > 0.0001:
                            filtered_ctxs.append(_ctx)
                contexts = filtered_ctxs

            # Ctrl+F row filter: keep only contexts (≈ one товар) with at least
            # one order matching Кількість/Інвойс/ЗП обліку.
            if self._search_text_query:
                q = self._search_text_query
                contexts = [
                    c
                    for c in contexts
                    if any(
                        row_text_matches(
                            q,
                            o.get("invoice_no"),
                            o.get("zp_1c"),
                            o.get("qty") or o.get("pack_qty") or o.get("goods_qty"),
                        )
                        for o in (
                            self._local_orders_cache.get(
                                str(c.get("id") or "").strip()
                            )
                            or []
                        )
                    )
                ]

            if not contexts:
                self._tree_host.controls.append(
                    self._text_wrap(
                        "Ще немає даних у Складі пакування.",
                        size=12,
                        color=_TEXT,
                    )
                )
            else:
                grouped_sup: dict[str, list[dict]] = defaultdict(list)
                for ctx in contexts:
                    sid = str((ctx or {}).get("packaging_supplier_id") or "").strip()
                    grouped_sup[sid].append(ctx)

                def _sup_sort_key(item):
                    sid, _rows = item
                    return (self._supplier_name(sid, suppliers) or sid).lower()

                for sid, sup_rows in sorted(grouped_sup.items(), key=_sup_sort_key):
                    sup_name = (
                        self._supplier_name(sid, suppliers) if sid else "Постачальник"
                    )

                    # count unique packaging items (boxes) that have ≥1 linked product
                    _sup_pids_with_products: set[str] = set()
                    for _c in sup_rows or []:
                        if self._ctx_linked_product_ids(_c):
                            _pid = str((_c or {}).get("packaging_id") or "").strip()
                            if _pid:
                                _sup_pids_with_products.add(_pid)
                    sup_pack_count = len(_sup_pids_with_products)

                    sup_header = ft.Row(
                        [
                            ft.Container(
                                padding=ft.padding.symmetric(horizontal=6, vertical=3),
                                bgcolor=_SOFT_I,
                                border_radius=6,
                                content=ft.Text(
                                    "ПОСТАЧАЛЬНИК",
                                    size=8,
                                    weight=ft.FontWeight.W_800,
                                    color=_CHIP_T,
                                ),
                            ),
                            ft.Icon(
                                ft.Icons.LOCAL_SHIPPING_OUTLINED,
                                size=18,
                                color=_INDIGO,
                            ),
                            ft.Container(
                                expand=True,
                                content=self._text_wrap(
                                    sup_name,
                                    size=13,
                                    weight=ft.FontWeight.W_700,
                                    color=_TEXT,
                                ),
                            ),
                            self._pack_kpi_chip(
                                "Пакувань", str(sup_pack_count), sign=None
                            ),
                        ],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )

                    sup_body = ft.Column(spacing=4)
                    grouped_box: dict[str, list[dict]] = defaultdict(list)
                    for ctx in sup_rows:
                        pid = str((ctx or {}).get("packaging_id") or "").strip()
                        grouped_box[pid].append(ctx)

                    for pid, box_rows in sorted(
                        grouped_box.items(),
                        key=lambda kv: (
                            str(
                                (boxes_by_id.get(kv[0]) or {}).get("name")
                                or kv[0]
                                or ""
                            ).lower()
                        ),
                    ):
                        box_row = boxes_by_id.get(pid) or {}
                        box_name = str(
                            box_row.get("name") or pid or "Пакування"
                        ).strip()
                        box_key = f"{sid}:{pid}" if sid or pid else pid

                        # Only include box if it has contexts with linked products
                        filtered_box_rows = [
                            c
                            for c in (box_rows or [])
                            if self._ctx_linked_product_ids(c)
                        ]
                        if not filtered_box_rows:
                            continue

                        box_header = ft.Row(
                            [
                                ft.Container(
                                    padding=ft.padding.symmetric(
                                        horizontal=6, vertical=3
                                    ),
                                    bgcolor=_SOFT_I,
                                    border_radius=6,
                                    content=ft.Text(
                                        "ПАКУВАННЯ",
                                        size=8,
                                        weight=ft.FontWeight.W_800,
                                        color=_CHIP_T,
                                    ),
                                ),
                                ft.Icon(
                                    ft.Icons.INVENTORY_2_OUTLINED,
                                    size=18,
                                    color=_INDIGO,
                                ),
                                ft.Container(
                                    expand=True,
                                    content=self._text_wrap(
                                        box_name,
                                        size=12,
                                        weight=ft.FontWeight.W_700,
                                        color=_TEXT,
                                    ),
                                ),
                                self._pack_kpi_chip(
                                    "Товарів", str(len(filtered_box_rows)), sign=None
                                ),
                            ],
                            spacing=6,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        )

                        box_body = ft.Column(spacing=4)

                        # Групуємо по постачальнику ТОВАРУ (goods_supplier_id).
                        # Якщо декілька товарів одного постачальника → показуємо разом.
                        _gs_groups: dict[str, list[dict]] = defaultdict(list)
                        for _ctx in filtered_box_rows:
                            _gsid = str(
                                (_ctx or {}).get("goods_supplier_id") or ""
                            ).strip()
                            _gs_groups[_gsid].append(_ctx)

                        for _gsid, _gs_ctxs in sorted(
                            _gs_groups.items(),
                            key=lambda kv: (
                                self._supplier_name(kv[0], suppliers) or kv[0] or ""
                            ).lower(),
                        ):
                            if len(_gs_ctxs) > 1:
                                # Кілька товарів одного постачальника товару → підзаголовок
                                _gs_name = (
                                    self._supplier_name(_gsid, suppliers)
                                    if _gsid
                                    else "Постачальник товару"
                                )
                                _prod_names = ", ".join(
                                    str(
                                        (
                                            products_by_id.get(
                                                str(
                                                    (_ctx or {}).get("goods_product_id")
                                                    or ""
                                                ).strip()
                                            )
                                            or {}
                                        ).get("name")
                                        or str(
                                            (_ctx or {}).get("goods_product_id") or ""
                                        )
                                        or "?"
                                    ).strip()
                                    for _ctx in _gs_ctxs
                                )
                                _gs_header = ft.Row(
                                    [
                                        ft.Container(
                                            padding=ft.padding.symmetric(
                                                horizontal=5, vertical=2
                                            ),
                                            bgcolor=_WARN_BG,
                                            border_radius=5,
                                            content=ft.Text(
                                                "ПОСТАЧ. ТОВАРУ",
                                                size=7,
                                                weight=ft.FontWeight.W_800,
                                                color=_WARN,
                                            ),
                                        ),
                                        ft.Icon(
                                            ft.Icons.STOREFRONT_OUTLINED,
                                            size=14,
                                            color=_WARN,
                                        ),
                                        ft.Container(
                                            expand=True,
                                            content=self._text_wrap(
                                                f"{_gs_name}  ·  {_prod_names}",
                                                size=11,
                                                weight=ft.FontWeight.W_600,
                                                color=_TEXT,
                                            ),
                                        ),
                                    ],
                                    spacing=5,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                )
                                _gs_body = ft.Column(spacing=3)
                                for _ctx in _gs_ctxs:
                                    _cid = str((_ctx or {}).get("id") or "").strip()
                                    _cs = self._build_context_section(
                                        _ctx,
                                        boxes_by_id,
                                        products_by_id,
                                        suppliers,
                                        grouped_under_box=True,
                                    )
                                    _cw = ft.Container(content=_cs)
                                    if _cid:
                                        self._pack_ctx_wrappers[_cid] = _cw
                                    _gs_body.controls.append(_cw)
                                _gs_key = f"{sid}:{pid}:{_gsid}"
                                box_body.controls.append(
                                    self._make_collapsible(
                                        _gs_header,
                                        _gs_body,
                                        expanded=(_gs_key in self._expanded_contexts),
                                        on_expanded=lambda *, expanded, k=_gs_key: (
                                            self._expanded_contexts.add(k)
                                            if expanded
                                            else self._expanded_contexts.discard(k)
                                        ),
                                        tier="context",
                                    )
                                )
                            else:
                                # Один товар — показуємо як раніше без підзаголовка
                                for _ctx in _gs_ctxs:
                                    _cid2 = str((_ctx or {}).get("id") or "").strip()
                                    _cs2 = self._build_context_section(
                                        _ctx,
                                        boxes_by_id,
                                        products_by_id,
                                        suppliers,
                                        grouped_under_box=True,
                                    )
                                    _cw2 = ft.Container(content=_cs2)
                                    if _cid2:
                                        self._pack_ctx_wrappers[_cid2] = _cw2
                                    box_body.controls.append(_cw2)

                        def _on_box_expanded(*, expanded: bool, key=box_key):
                            if expanded:
                                self._expanded_packaging_boxes.add(key)
                            else:
                                self._expanded_packaging_boxes.discard(key)

                        sup_body.controls.append(
                            self._make_collapsible(
                                box_header,
                                box_body,
                                expanded=(box_key in self._expanded_packaging_boxes),
                                on_expanded=_on_box_expanded,
                                tier="box",
                            )
                        )

                    def _on_sup_expanded(*, expanded: bool, key=sid):
                        if expanded:
                            self._expanded_packaging_suppliers.add(key)
                        else:
                            self._expanded_packaging_suppliers.discard(key)

                    # Only append supplier group if it has any boxes (after filtering)
                    if getattr(sup_body, "controls", None):
                        self._tree_host.controls.append(
                            self._make_collapsible(
                                sup_header,
                                sup_body,
                                expanded=(sid in self._expanded_packaging_suppliers),
                                on_expanded=_on_sup_expanded,
                                tier="supplier",
                            )
                        )

            vw = self._viewport_w(pg)
            with contextlib.suppress(Exception):
                self._tables_inner_container.width = max(700, int(vw) - 24)

        with contextlib.suppress(Exception):
            self._tree_host.update()
        self._build_counters_bar()
        if self._counters_row:
            with contextlib.suppress(Exception):
                self._counters_row.update()

        if self._loading_box:
            self._loading_box.visible = False
            # Prefer updating via content_area so the diff reaches Flutter even
            # when the _loading_box control itself has no direct page reference yet.
            try:
                self._loading_box.update()
            except Exception:
                with contextlib.suppress(Exception):
                    self.content_area.update()
        self._restore_content_area_scroll()

    def reload(self, refresh_data: bool = True):
        """Оновлення дерева після inline-змін у журналі."""
        try:
            if refresh_data:
                self._load_packaging_data()
            self._render_tree(force_full=True)
        except Exception:
            logger.exception("Packaging stock reload failed")

    def _refresh_pack_ctx_inplace(self, ctx_id: str) -> bool:
        """Перебудовує один контекст пакування і оновлює wrapper in-place. True = успіх."""
        wrapper = self._pack_ctx_wrappers.get(ctx_id)
        if wrapper is None:
            return False
        ctx = next(
            (
                c
                for c in (self._packaging_items or [])
                if isinstance(c, dict) and str(c.get("id") or "").strip() == ctx_id
            ),
            None,
        )
        if ctx is None:
            return False
        boxes_by_id = self._packaging_boxes_by_id or {}
        products_by_id = self._packaging_products_by_id or {}
        suppliers = self._packaging_suppliers or []
        try:
            new_section = self._build_context_section(
                ctx, boxes_by_id, products_by_id, suppliers, grouped_under_box=True
            )
            wrapper.content = new_section
            pg = self._page()
            if pg:
                with contextlib.suppress(Exception):
                    wrapper.update()
            return True
        except Exception:
            logger.exception("pack_stock _refresh_pack_ctx_inplace ctx=%s", ctx_id)
            return False

    def _schedule_debounced_reload(self, delay: float = 0.25) -> None:
        """Schedule a debounced tree rebuild — rapid edits coalesce into one render."""
        self._reload_seq += 1
        my_seq = self._reload_seq
        pg = self._page()
        if not pg or not hasattr(pg, "run_task"):
            return

        async def _do(seq: int = my_seq) -> None:
            await asyncio.sleep(delay)
            if self._reload_seq != seq:
                return
            try:
                self._render_tree(force_full=True)
            except Exception:
                logger.exception("pack_stock debounced reload failed")

        pg.run_task(_do)

    async def _auto_refresh_loop(self):
        import asyncio

        from config import Config

        refresh_iv = max(
            45.0,
            float(
                getattr(Config, "PACKAGING_AUTO_REFRESH_INTERVAL_SECONDS", 45.0) or 45.0
            ),
        )
        while True:
            await asyncio.sleep(refresh_iv)
            pg = self._page()
            if not pg:
                continue
            if str(getattr(pg, "_active_view", "")) != "show_pack_stock":
                continue
            try:
                if self._ops_outbox:
                    await self._flush_pack_outbox_async()
            except Exception:
                pass

    def _start_auto_refresh_loop(self, page: ft.Page):
        if (
            not getattr(self, "_auto_refresh_started", False)
            and page
            and hasattr(page, "run_task")
        ):
            self._auto_refresh_started = True
            page.run_task(self._auto_refresh_loop)

    def _packaging_sig(self, data: Any) -> str:
        try:
            return json.dumps(data or {}, ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(time.time())

    def _chip(self, label: str, count, bg: str, fg: str) -> ft.Container:
        return ft.Container(
            bgcolor=bg,
            border_radius=6,
            padding=ft.padding.symmetric(horizontal=6, vertical=2),
            content=ft.Row(
                [
                    ft.Text(label, size=9, color=fg),
                    ft.Container(
                        bgcolor=fg,
                        border_radius=10,
                        padding=ft.padding.symmetric(horizontal=5, vertical=1),
                        content=ft.Text(
                            str(count), size=9, color=_PANEL, weight="bold"
                        ),
                    ),
                ],
                spacing=4,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _sep(self) -> ft.Container:
        return ft.Container(
            width=1,
            height=20,
            bgcolor=_BORDER,
            margin=ft.margin.symmetric(horizontal=4),
        )

    def _build_counters_bar(self) -> ft.Container:
        total_sup = int((self._packaging_kpis or {}).get("suppliers") or 0)
        total_boxes = int((self._packaging_kpis or {}).get("boxes") or 0)
        total_ctx = int((self._packaging_kpis or {}).get("contexts") or 0)

        if self._counters_row:
            self._counters_row.controls.clear()
            self._counters_row.controls.extend(
                [
                    self._chip("Постачальників", total_sup, _PANEL, _TEXT2),
                    self._sep(),
                    self._chip("Пакувань", total_boxes, _SOFT_I, _INDIGO),
                    self._chip("Контекстів", total_ctx, "#F0FDF4", "#166534"),
                ]
            )
            self._counters_row.update()
        return ft.Container()

    def _pack_columns_all_keys(self) -> list[str]:
        return [
            "seq",
            "d",
            "qty",
            "st",
            "cost",
            "cost_usd",
            "paid",
            "ship",
            "bal_pack",
            "bal_goods",
            "zp",
            "inv",
            "cmt",
            "del",
        ]

    def _pack_columns_labels(self) -> dict[str, str]:
        return {
            "seq": "№",
            "d": self.t("pack_stock.col.d", "Дата"),
            "qty": self.t("pack_stock.col.qty", "Кількість"),
            "st": self.t("pack_stock.col.st", "Статус"),
            "cost": self.t("pack_stock.col.cost", "Вартість"),
            "cost_usd": self.t("pack_stock.col.cost_usd", "Вартість $"),
            "paid": self.t("pack_stock.col.paid", "Сплачено"),
            "ship": self.t("pack_stock.col.ship", "Відпр."),
            "bal_pack": self.t("pack_stock.toggle.bal_pack", "Залишок (пак.)"),
            "bal_goods": self.t("pack_stock.toggle.bal_goods", "Залишок (тов.)"),
            "zp": self.t("pack_stock.col.zp", "ЗП обліку"),
            "inv": self.t("pack_stock.col.inv", "Інвойс"),
            "cmt": self.t("pack_stock.col.cmt", "Коментар"),
            "del": self.t("pack_stock.toggle.del", "Видалення"),
        }

    def _pack_layout_state_key(self) -> str:
        region = str(self._resolve_region_id() or "all").strip() or "all"
        company = str(self.company_id or "no_company").strip() or "no_company"
        return f"{company}:{region}"

    def _load_pack_table_layout_state(self) -> None:
        uid = str((self.user_data or {}).get("id") or self.manager_id or "").strip()
        if not uid:
            return
        local_path = self._pack_layout_file_path()
        if os.path.exists(local_path):
            try:
                import json as _json

                with open(local_path, "r", encoding="utf-8") as f:
                    row = _json.load(f)
                if isinstance(row, dict):
                    widths = (
                        row.get("col_widths")
                        if isinstance(row.get("col_widths"), dict)
                        else {}
                    )
                    self._pack_col_width_overrides = {
                        str(k): int(max(24, min(900, int(v))))
                        for k, v in (widths or {}).items()
                        if _safe_float(v, None) is not None
                    }
                    all_keys = self._pack_columns_all_keys()
                    visible_raw = (
                        row.get("visible_columns")
                        if isinstance(row.get("visible_columns"), list)
                        else None
                    )
                    if isinstance(visible_raw, list):
                        visible = {
                            str(x).strip()
                            for x in visible_raw
                            if str(x).strip() in set(all_keys)
                        }
                        if visible:
                            self._pack_visible_column_ids = visible
                    order_raw = (
                        row.get("column_order")
                        if isinstance(row.get("column_order"), list)
                        else None
                    )
                    if isinstance(order_raw, list):
                        normalized = [
                            str(x).strip()
                            for x in order_raw
                            if str(x).strip() in set(all_keys)
                        ]
                        self._pack_column_order_ids = normalized + [
                            key for key in all_keys if key not in set(normalized)
                        ]
                    dfm = row.get("display_from_month")
                    if isinstance(dfm, str):
                        self._display_from_month = dfm.strip()[:7]
                    return
            except Exception:
                pass
        try:
            payload = self.db.get_manager_workspace_state(uid) or {}
            if not isinstance(payload, dict):
                return
            store = (
                payload.get("packaging_table_layout")
                if isinstance(payload.get("packaging_table_layout"), dict)
                else {}
            )
            row = (
                store.get(self._pack_layout_state_key())
                if isinstance(store, dict)
                else None
            )
            if not isinstance(row, dict):
                return
            widths = (
                row.get("col_widths") if isinstance(row.get("col_widths"), dict) else {}
            )
            normalized_widths: dict[str, int] = {}
            for key, value in (widths or {}).items():
                vv = _safe_float(value, None)
                if vv is None:
                    continue
                normalized_widths[str(key)] = int(max(24, min(900, int(vv))))
            self._pack_col_width_overrides = normalized_widths

            all_keys = self._pack_columns_all_keys()
            visible_raw = (
                row.get("visible_columns")
                if isinstance(row.get("visible_columns"), list)
                else None
            )
            if isinstance(visible_raw, list):
                visible = {
                    str(x).strip()
                    for x in visible_raw
                    if str(x).strip() in set(all_keys)
                }
                if visible:
                    self._pack_visible_column_ids = visible

            order_raw = (
                row.get("column_order")
                if isinstance(row.get("column_order"), list)
                else None
            )
            if isinstance(order_raw, list):
                normalized = [
                    str(x).strip() for x in order_raw if str(x).strip() in set(all_keys)
                ]
                self._pack_column_order_ids = normalized + [
                    key for key in all_keys if key not in set(normalized)
                ]

            dfm = row.get("display_from_month")
            if isinstance(dfm, str):
                self._display_from_month = dfm.strip()[:7]

            self._save_pack_layout_to_file()
        except Exception:
            pass

    def _save_pack_table_layout_state(self) -> None:
        # Save to local file immediately — no DB block on UI thread
        self._save_pack_layout_to_file()
        # Queue async DB write
        pg = self._page()
        if pg and hasattr(pg, "run_task"):
            with contextlib.suppress(Exception):
                pg.run_task(self._async_flush_pack_layout_to_db)

    async def _async_flush_pack_layout_to_db(self) -> None:
        uid = str((self.user_data or {}).get("id") or self.manager_id or "").strip()
        if not uid:
            return
        try:
            payload = (
                await asyncio.to_thread(self.db.get_manager_workspace_state, uid) or {}
            )
            if not isinstance(payload, dict):
                payload = {}
            store = (
                payload.get("packaging_table_layout")
                if isinstance(payload.get("packaging_table_layout"), dict)
                else {}
            )
            store[self._pack_layout_state_key()] = {
                "col_widths": dict(self._pack_col_width_overrides or {}),
                "visible_columns": sorted(list(self._visible_pack_column_ids())),
                "column_order": list(self._ordered_pack_column_ids()),
                "col_widths_unit": "px",
                "display_from_month": str(self._display_from_month or ""),
            }
            payload["packaging_table_layout"] = store
            await asyncio.to_thread(self.db.save_manager_workspace_state, uid, payload)
        except Exception:
            pass

    def _visible_pack_column_ids(self) -> set[str]:
        if isinstance(self._pack_visible_column_ids, set):
            return set(self._pack_visible_column_ids)
        all_keys = self._pack_columns_all_keys()
        self._pack_visible_column_ids = set(all_keys)
        return set(self._pack_visible_column_ids)

    def _ordered_pack_column_ids(self) -> list[str]:
        all_keys = self._pack_columns_all_keys()
        if (
            isinstance(self._pack_column_order_ids, list)
            and self._pack_column_order_ids
        ):
            normalized = [k for k in self._pack_column_order_ids if k in all_keys]
            return normalized + [k for k in all_keys if k not in set(normalized)]
        return list(all_keys)

    def _open_packaging_columns_dialog(self) -> None:
        page = self._page()
        if not page:
            return

        labels = self._pack_columns_labels()
        selected = set(self._visible_pack_column_ids())
        ordered_ids = list(self._ordered_pack_column_ids())
        widths = dict(self._pack_col_width_overrides or {})
        rows_host = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, expand=True)

        _dfm_month_names = {
            1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень",
            5: "Травень", 6: "Червень", 7: "Липень", 8: "Серпень",
            9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень",
        }
        _dfm_months = sorted(
            {
                str((o or {}).get("order_date") or "")[:7]
                for _orders in (self._local_orders_cache or {}).values()
                for o in (_orders or [])
                if isinstance(o, dict) and str((o or {}).get("order_date") or "")[:7]
            }
        )

        def _dfm_label(m: str) -> str:
            try:
                mm = int(m[5:7])
            except Exception:
                return m
            return f"{_dfm_month_names.get(mm, m[5:7])} {m[:4]}"

        display_from_dd = ft.Dropdown(
            label="Перший місяць відображення",
            value=(self._display_from_month if self._display_from_month in _dfm_months else ""),
            options=[ft.dropdown.Option("", "Показати всю історію")]
            + [ft.dropdown.Option(m, _dfm_label(m)) for m in _dfm_months],
            width=320,
            text_size=11,
        )

        def _render_rows():
            controls = []
            total = len(ordered_ids)
            for idx, key in enumerate(ordered_ids):
                cb = ft.Checkbox(label=labels.get(key, key), value=(key in selected))
                w_field = ft.TextField(
                    value=str(int(widths.get(key) or 0)) if widths.get(key) else "",
                    width=86,
                    dense=True,
                    text_size=11,
                    hint_text="auto",
                )

                def _toggle(e, current_key=key):
                    if bool(getattr(e.control, "value", False)):
                        selected.add(current_key)
                    else:
                        selected.discard(current_key)

                def _on_width_blur(_e=None, current_key=key, fld=w_field):
                    wv = _safe_float(fld.value, None)
                    if wv is None:
                        widths.pop(current_key, None)
                        return
                    widths[current_key] = int(max(24, min(900, int(wv))))

                def _move_up(_e, i=idx):
                    if i <= 0:
                        return
                    ordered_ids[i - 1], ordered_ids[i] = (
                        ordered_ids[i],
                        ordered_ids[i - 1],
                    )
                    _render_rows()

                def _move_down(_e, i=idx):
                    if i >= len(ordered_ids) - 1:
                        return
                    ordered_ids[i + 1], ordered_ids[i] = (
                        ordered_ids[i],
                        ordered_ids[i + 1],
                    )
                    _render_rows()

                cb.on_change = _toggle
                w_field.on_blur = _on_width_blur
                controls.append(
                    ft.Row(
                        [
                            ft.Container(width=180, content=cb),
                            ft.Text("px", size=10, color=_TEXT),
                            w_field,
                            ft.IconButton(
                                icon=ft.Icons.KEYBOARD_ARROW_UP,
                                icon_size=16,
                                tooltip="Вгору",
                                disabled=(idx == 0),
                                on_click=_move_up,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.KEYBOARD_ARROW_DOWN,
                                icon_size=16,
                                tooltip="Вниз",
                                disabled=(idx == total - 1),
                                on_click=_move_down,
                            ),
                        ],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )
            rows_host.controls = controls
            with contextlib.suppress(Exception):
                rows_host.update()

        _render_rows()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Налаштування стовпців журналу"),
            content=ft.Container(
                width=420,
                height=480,
                content=ft.Column(
                    [display_from_dd, ft.Divider(height=8), rows_host],
                    spacing=6,
                    expand=True,
                ),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _e: page.pop_dialog()),
                ft.FilledButton(
                    "Зберегти",
                    on_click=lambda _e: (
                        setattr(
                            self,
                            "_pack_visible_column_ids",
                            (
                                set(selected)
                                if selected
                                else set(self._pack_columns_all_keys())
                            ),
                        ),
                        setattr(self, "_pack_column_order_ids", list(ordered_ids)),
                        setattr(self, "_pack_col_width_overrides", dict(widths)),
                        setattr(
                            self,
                            "_display_from_month",
                            str(display_from_dd.value or "").strip(),
                        ),
                        self._save_pack_table_layout_state(),
                        page.pop_dialog(),
                        self._restart_tab(),
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dialog)

    # ── build ───────────────────────────────────────────────────────────────
    def build(self):
        _th = _th_from_view(self)
        global _TEXT, _ERR, _HDR, _PANEL, _ALT, _ALT2, _STONE, _BORDER
        global _ACCENT, _SKY, _SOFT_I, _INDIGO, _OK_BG, _OK, _CHIP_T
        global _TEAL, _SOFT_V, _TEXT2, _SKY_BG, _ERR_DK, _WARN_BG, _WARN
        _TEXT = _th["text"]
        _ERR = _th["err"]
        _HDR = _th["hdr"]
        _PANEL = _th["panel"]
        _ALT = _ALT2 = _th["alt"]
        _STONE = _th["muted"]
        _BORDER = _th["border"]
        _ACCENT = _SKY = _th["icon_color"]
        _SOFT_I = _th["soft"]
        _INDIGO = _th["accent"]
        _OK_BG = _th["ok_bg"]
        _OK = _th["ok"]
        _CHIP_T = _th["chip_text"]
        _TEAL = _th["ok"]
        _SOFT_V = _th["soft"]
        _TEXT2 = _th["muted"]
        _SKY_BG = _th["soft"]
        _ERR_DK = _th["err"]
        _WARN_BG = _th["warn_bg"]
        _WARN = _th["warn"]
        if self._general_mode and self._general_manager_ids:
            from views.general_wrapper import build_general_sections

            _cid, _db = self.company_id, self.db
            _rr = self.region_ref
            # Resolve region name → UUID (region_ref may be a name string)
            _rid_resolved = ""
            if _rr:
                for _r in _db.get_company_regions(_cid) or []:
                    if not isinstance(_r, dict):
                        continue
                    _rname = str(_r.get("name") or "").strip()
                    _rid_val = str(_r.get("id") or "").strip()
                    if _rname == _rr.strip() or _rid_val == _rr.strip():
                        _rid_resolved = _rid_val
                        break
            _rid_for_count = _rid_resolved or None

            def _count_packs(mid, _db=_db, _cid=_cid, _rid=_rid_for_count):
                packs = _db.get_packaging(_cid, _rid) or []
                return sum(
                    1
                    for p in packs
                    if isinstance(p, dict)
                    and str(p.get("manager_id") or "").strip() == mid
                )

            build_general_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={
                    "region_ref": self.region_ref,
                    "quick_filter": self.quick_filter,
                },
                count_getter=_count_packs,
                count_label=self.t("pack_stock.count_label", "Пакувань:"),
            )
            return

        # All regions → region sub-groups (single-user mode)
        if not self.region_ref:
            from views.general_wrapper import build_region_sections

            _cid2, _db2, _mid2 = self.company_id, self.db, self.manager_id
            _mid2_str = str(_mid2 or "").strip()
            _all2_raw = _db2.get_packaging(_cid2, None) or []
            _all2 = [
                p
                for p in _all2_raw
                if isinstance(p, dict)
                and str(p.get("manager_id") or "").strip() == _mid2_str
            ]
            _rcounts2: dict[str, int] = {}
            for _pk2 in _all2:
                if not isinstance(_pk2, dict):
                    continue
                _r2 = str(_pk2.get("region_id") or "").strip()
                if _r2:
                    _rcounts2[_r2] = _rcounts2.get(_r2, 0) + 1

            def _count_rcpks(rid, _c=_rcounts2):
                return _c.get(rid, 0)

            build_region_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={"quick_filter": self.quick_filter},
                count_getter=_count_rcpks,
                region_kwarg_name="region_ref",
                count_label=self.t("pack_stock.count_label", "Пакувань:"),
            )
            return

        self._expanded_packaging_suppliers.clear()
        self._expanded_packaging_boxes.clear()
        self._expanded_contexts.clear()
        self._expanded_pack_order_groups.clear()
        self._load_pack_outbox()
        self._load_pack_table_layout_state()
        try:
            self.content_area.scroll = ft.ScrollMode.ADAPTIVE
            self.content_area.on_scroll = self._on_content_area_scroll
        except Exception:
            pass

        reg_lbl = (
            str(self.user_data.get("active_region_label", "") or "").strip() or "Регіон"
        )
        mgr = str(
            self.user_data.get("active_manager_name")
            or self.user_data.get("full_name")
            or ""
        ).strip()
        subtitle = f"{reg_lbl} · {mgr}" if mgr else reg_lbl
        self._header_loading_indicator = create_orders_header_loading_indicator(
            visible=True
        )
        _sync_indicator = self._build_pack_sync_indicator()

        header_row = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(
                                ft.Icons.LOCAL_SHIPPING_OUTLINED,
                                size=26,
                                color=self.UI_ACCENT_BLUE,
                            ),
                            ft.Column(
                                [
                                    ft.Row(
                                        [
                                            ft.Text(
                                                self.t("pack_stock.title", "Склад пакування"),
                                                size=21,
                                                weight=ft.FontWeight.W_700,
                                                color=self.UI_TITLE_INK,
                                            ),
                                            self._header_loading_indicator,
                                            ft.IconButton(
                                                icon=ft.Icons.HELP_OUTLINE,
                                                icon_size=18,
                                                icon_color=self.UI_TITLE_INK,
                                                tooltip=self.t("common.help.tooltip", "Довідка"),
                                                on_click=lambda _e: self._show_help(),
                                            ),
                                        ],
                                        spacing=8,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        tight=True,
                                    ),
                                    ft.Text(
                                        subtitle,
                                        size=13,
                                        weight=ft.FontWeight.W_500,
                                        color=self.UI_MUTED_INK,
                                        no_wrap=True,
                                        overflow=ft.TextOverflow.ELLIPSIS,
                                        max_lines=1,
                                    ),
                                ],
                                spacing=2,
                                tight=True,
                                expand=True,
                            ),
                            ft.Row(
                                [
                                    ft.OutlinedButton(
                                        self.t("payments.dlg.table_title", "Таблиця"),
                                        icon=ft.Icons.TABLE_CHART_OUTLINED,
                                        height=35,
                                        style=ft.ButtonStyle(
                                            text_style=ft.TextStyle(size=11),
                                            padding=ft.padding.symmetric(
                                                horizontal=10, vertical=2
                                            ),
                                        ),
                                        on_click=lambda _e: self._open_packaging_columns_dialog(),
                                    ),
                                    _sync_indicator,
                                ],
                                spacing=8,
                                wrap=True,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                tight=True,
            ),
            border=ft.border.only(bottom=ft.BorderSide(1, _HDR)),
            padding=ft.padding.only(bottom=12),
        )

        _counters_row_kwargs = dict(
            spacing=4,
            run_spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        if is_mobile(self._page()):
            self._counters_row = make_scrollable_chip_row([], **_counters_row_kwargs)
        else:
            self._counters_row = ft.Row([], wrap=True, **_counters_row_kwargs)
        counters_bar = ft.Container(
            padding=ft.padding.symmetric(horizontal=2, vertical=2),
            content=self._counters_row,
        )

        self._loading_box = ft.Container(
            margin=0,
            padding=0,
            visible=True,
            expand=True,
            bgcolor="transparent",
            on_click=lambda _e: None,
        )

        self._tables_inner_container = ft.Container(
            padding=0,
            content=self._tree_host,
            bgcolor=_PANEL,
            expand=True,
        )

        self._tables_outer_container = ft.Row(
            [self._tables_inner_container],
            expand=True,
            spacing=0,
        )
        try:
            self._tables_outer_container.bgcolor = _PANEL
        except Exception:
            pass

        self.content_area.controls.clear()
        try:
            self.content_area.bgcolor = _PANEL
        except Exception:
            pass

        pg = self._page()
        _sticky = getattr(pg, "_view_sticky_header", None) if pg else None
        if getattr(pg, "_active_view", "") != "show_pack_stock":
            _sticky = None
        if _sticky is not None:
            _sticky.controls.clear()
            _sticky.controls.append(header_row)
            with contextlib.suppress(Exception):
                _sticky.update()
            self.content_area.controls.extend(
                [counters_bar, self._loading_box, self._tables_outer_container]
            )
        else:
            self.content_area.controls.extend(
                [
                    header_row,
                    counters_bar,
                    self._loading_box,
                    self._tables_outer_container,
                ]
            )
        self._sticky_header_ref = header_row
        self.content_area.update()
        pg = self._page()
        if pg:
            # Store the task so _defer_view_initial_load (called by dashboard after
            # build()) can cancel it and reschedule with a small delay.  Without this,
            # _async_initial_load runs twice: once from here and once from the deferred
            # call, causing a double-load and a second spinner flash.
            with contextlib.suppress(Exception):
                self._initial_load_task = pg.run_task(self._async_initial_load)
            self._start_auto_refresh_loop(pg)
            if not self._ops_sync_loop_started:
                self._ops_sync_loop_started = True
                with contextlib.suppress(Exception):
                    pg.run_task(self._ops_sync_loop)
            try:
                self._attach_resize_handler()
            except Exception:
                pass
            try:
                self._apply_viewport_size()
            except Exception:
                pass

    def _product_name_suffixes_for(self, prod_by_id: dict) -> dict[str, str]:
        """Disambiguation suffixes (" | Кат: ... | Постач: ...") for products
        sharing a name — cached by identity of `prod_by_id` since this is
        called once per linked-products row and the caller reuses the same
        dict across many rows in one render pass. See views/product_naming.py."""
        if getattr(self, "_pkg_name_suffixes_src", None) is not prod_by_id:
            self._pkg_name_suffixes_src = prod_by_id
            if not hasattr(self, "_node_path_cache"):
                self._node_path_cache = {}
            _node_path_of = product_naming.make_node_path_resolver(
                self.db, self._node_path_cache
            )
            self._pkg_name_suffixes = product_naming.compute_name_suffixes(
                list((prod_by_id or {}).values()),
                category_of=lambda p: product_naming.resolve_category_label(
                    p, node_path_of=_node_path_of
                ),
                supplier_of=product_naming.resolve_supplier_label,
            )
        return self._pkg_name_suffixes or {}

    def _build_context_section(
        self,
        ctx: dict,
        pack_by_id: dict,
        prod_by_id: dict,
        suppliers: list[dict],
        *,
        grouped_under_box: bool = False,
    ) -> ft.Container:
        ctx_id = str(ctx.get("id") or "").strip()
        pack_id = str(ctx.get("packaging_id") or "").strip()
        pack_row = pack_by_id.get(pack_id) or {}
        pack_name = str(pack_row.get("name") or pack_id).strip() or "—"
        goods_sid = str(ctx.get("goods_supplier_id") or "").strip()
        goods_name = self._supplier_name(goods_sid, suppliers) if goods_sid else "—"
        linked_ids = self._ctx_linked_product_ids(ctx)
        try:
            self.content_area.scroll = ft.ScrollMode.ADAPTIVE
        except Exception:
            pass

        if not linked_ids:
            return ft.Container(height=0, visible=False)

        _name_suffixes = self._product_name_suffixes_for(prod_by_id)
        names: list[str] = []
        for pid in linked_ids:
            p = prod_by_id.get(pid)
            if isinstance(p, dict):
                nm = str(p.get("name") or pid).strip()
                if nm:
                    names.append(nm + _name_suffixes.get(str(pid), ""))
        if names:
            products_title = ", ".join(names)
        else:
            lim = linked_ids[:12]
            products_title = ", ".join(lim) + ("…" if len(linked_ids) > 12 else "")

        ccy = self._packaging_currency(pack_row)
        show_usd = ccy != "USD"

        # Both opening balances and supply orders are pre-loaded in
        # _load_packaging_data() — no live DB call needed here.
        opening = dict(self._local_opening_cache.get(ctx_id) or {})
        if not opening:
            # Fallback (e.g. first load before cache populated): fetch once and store.
            with contextlib.suppress(Exception):
                opening = dict(self.db.get_packaging_supply_opening(ctx_id) or {})
                self._local_opening_cache[ctx_id] = opening
        op_ccy = str(opening.get("balance_currency") or ccy or "USD").upper()
        orders_ctx = list(self._local_orders_cache.get(ctx_id) or [])
        writeoffs_ctx = list(self._local_writeoff_cache.get(ctx_id) or [])
        # Будуємо зворотну карту order→ctx для in-place оновлення
        if ctx_id:
            for _o in orders_ctx:
                _oid = str((_o or {}).get("id") or "").strip()
                if _oid:
                    self._pack_order_ctx_map[_oid] = ctx_id
        bn = self._packaging_context_balance_net(opening, orders_ctx, pack_row)
        cur_pack_ctx, cur_goods_ctx = self._packaging_journal_final_stocks(
            opening, orders_ctx, writeoffs_ctx
        )
        nat_b, usd_b = self._native_and_usd_balance_lines_for_entries([(bn, op_ccy)])

        opening_chip = ft.Container(
            clip_behavior=ft.ClipBehavior.NONE,
            content=self._packaging_remainder_strip_ui(
                headline="Поточний залишок",
                pack_qty=cur_pack_ctx,
                goods_qty=cur_goods_ctx,
                native_balance_body=nat_b,
                usd_balance_body=usd_b,
                outer_bg="#F0FDF4",
                outer_border="#6EE7B7",
                tree_compact=True,
            ),
        )

        from views.product_quick_view import product_icon_btn as _piqb

        _prod_rows_ui: list = []
        for _pid in linked_ids:
            _p = prod_by_id.get(_pid)
            _pname = (
                str((_p or {}).get("name") or _pid).strip() + _name_suffixes.get(str(_pid), "")
                if isinstance(_p, dict)
                else str(_pid)
            )
            _prod_rows_ui.append(
                ft.Row(
                    [
                        self._text_wrap(
                            _pname,
                            size=12,
                            weight=ft.FontWeight.W_700,
                            color=_TEXT,
                            expand=True,
                        ),
                        _piqb(
                            _pid, self.content_area, self.db, self.user_data, size=12
                        ),
                    ],
                    spacing=2,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
            )
        _prod_names_col = (
            ft.Column(_prod_rows_ui, spacing=1, tight=True)
            if _prod_rows_ui
            else self._text_wrap(
                products_title, size=12, weight=ft.FontWeight.W_700, color=_TEXT
            )
        )

        if grouped_under_box:
            hdr_click = ft.Column(
                [
                    ft.Row(
                        [
                            ft.Container(
                                padding=ft.padding.symmetric(horizontal=6, vertical=3),
                                bgcolor=_SOFT_I,
                                border_radius=6,
                                content=ft.Text(
                                    "ТОВАР",
                                    size=8,
                                    weight=ft.FontWeight.W_800,
                                    color=_CHIP_T,
                                ),
                            ),
                            ft.Icon(
                                ft.Icons.SHOPPING_BAG_OUTLINED, size=18, color=_INDIGO
                            ),
                            ft.Container(expand=True, content=_prod_names_col),
                        ],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        intrinsic_height=True,
                    ),
                    # Раніше: Row(wrap=True)+Text(expand=True) ламало web-верстку («сіра плита»); переніс flex на Container.
                    ft.Row(
                        [
                            self._text_wrap("Постачання товару", size=8, color=_TEXT),
                            ft.Container(
                                expand=True,
                                content=self._text_wrap(
                                    goods_name,
                                    size=10,
                                    weight=ft.FontWeight.W_600,
                                    color=_TEXT,
                                ),
                            ),
                        ],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        intrinsic_height=True,
                    ),
                ],
                spacing=2,
                tight=True,
            )
            detail = ft.Column([opening_chip], spacing=2)
        else:
            hdr_click = ft.Column(
                [
                    ft.Row(
                        [
                            ft.Container(
                                padding=ft.padding.symmetric(horizontal=6, vertical=3),
                                bgcolor=_OK_BG,
                                border_radius=6,
                                content=ft.Text(
                                    "ЖУРНАЛ SKU",
                                    size=8,
                                    weight=ft.FontWeight.W_800,
                                    color=_OK,
                                ),
                            ),
                            ft.Icon(ft.Icons.INVENTORY_2_OUTLINED, size=18, color=_OK),
                            self._text_wrap(
                                pack_name,
                                size=12,
                                weight=ft.FontWeight.W_700,
                                color=_TEXT,
                                expand=True,
                            ),
                        ],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        intrinsic_height=True,
                    ),
                    # Раніше: Row(wrap=True)+Text(expand=True) ламало web-верстку («сіра плита»); переніс flex на Container.
                    ft.Row(
                        [
                            self._text_wrap("Постачання товару", size=8, color=_TEXT),
                            ft.Container(
                                expand=True,
                                content=self._text_wrap(
                                    goods_name,
                                    size=10,
                                    weight=ft.FontWeight.W_600,
                                    color=_TEXT,
                                ),
                            ),
                        ],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        intrinsic_height=True,
                    ),
                ],
                spacing=2,
                tight=True,
            )
            detail = ft.Column(
                [
                    ft.Column(
                        [
                            self._text_wrap("Привʼязані товари", size=9, color=_TEXT),
                            *_prod_rows_ui,
                        ],
                        spacing=1,
                        tight=True,
                    ),
                    opening_chip,
                ],
                spacing=2,
            )

        _ctx_btn_style = ft.ButtonStyle(
            text_style=ft.TextStyle(size=self.CONTEXT_BTN_TEXT),
            padding=ft.padding.symmetric(
                horizontal=self.CONTEXT_BTN_PAD_H,
                vertical=self.CONTEXT_BTN_PAD_V,
            ),
        )
        btns = ft.Row(
            [
                ft.FilledButton(
                    "Замовити пакування",
                    height=self.CONTEXT_BTN_H,
                    style=_ctx_btn_style,
                    disabled=self.subordinate_ro,
                    on_click=lambda _e, c=ctx, pr=pack_row: self._page()
                    and self._page().run_task(self._open_supply_order_dialog, c, pr),
                ),
                ft.OutlinedButton(
                    "Початковий баланс",
                    height=self.CONTEXT_BTN_H,
                    style=_ctx_btn_style,
                    disabled=self.subordinate_ro,
                    on_click=lambda _e, c=ctx, pr=pack_row: self._open_opening_dialog(
                        c, pr
                    ),
                ),
                ft.OutlinedButton(
                    "Списати пакування",
                    height=self.CONTEXT_BTN_H,
                    style=_ctx_btn_style,
                    disabled=self.subordinate_ro,
                    on_click=lambda _e, c=ctx, pr=pack_row: self._open_writeoff_dialog(
                        c, pr
                    ),
                ),
            ],
            spacing=max(4, round(8 * 0.7)),
        )

        # Приховуємо таблицю лише для контексту «пакування без постачання товару» (goods_supplier_id порожній),
        # який sync створює як заготовку; для пар packaging×постачальник товарів журнал показуємо завжди.
        goods_sid_live = str(ctx.get("goods_supplier_id") or "").strip()
        show_journal = bool(linked_ids) or bool(goods_sid_live)
        self._log_pack(
            "context ctx_id=%s rid=%s pack_id=%s linked_n=%s goods_sup=%s show_journal=%s pack_row_name=%r",
            ctx_id[:16] if ctx_id else "",
            str(ctx.get("region_id") or "")[:16],
            pack_id[:16] if pack_id else "",
            len(linked_ids),
            bool(goods_sid_live),
            show_journal,
            (pack_row.get("name") if isinstance(pack_row, dict) else None) or "",
        )
        tbl: ft.Control | None = None
        if show_journal:
            orders = orders_ctx
            self._log_pack(
                "context TABLE build start ctx=%s orders_raw=%s show_usd=%s",
                ctx_id[:16],
                len(orders),
                show_usd,
            )
            try:
                tbl = self._build_orders_table(
                    ctx_id,
                    pack_row,
                    orders,
                    goods_name,
                    packaging_supplier_label=self._supplier_name(
                        str(ctx.get("packaging_supplier_id") or ""), suppliers
                    ),
                    show_usd_cost=show_usd,
                    opening_snapshot=opening,
                )
                self._log_pack(
                    "context TABLE build ok ctx=%s tbl_type=%s",
                    ctx_id[:16],
                    type(tbl).__name__,
                )
            except Exception:
                logger.exception(
                    "packaging_stock_view: _build_orders_table failed ctx_id=%s",
                    str(ctx_id)[:24] if ctx_id else "",
                )
                tbl = self._text_wrap(
                    "Помилка побудови таблиці журналу.",
                    color=_ERR_DK,
                    size=12,
                )

        body_cols = [detail, btns]
        if tbl is not None:
            body_cols.append(tbl)

        body = ft.Container(
            padding=ft.padding.symmetric(horizontal=8, vertical=6),
            bgcolor=_ALT,
            border_radius=8,
            border=ft.border.all(1, _BORDER),
            content=ft.Column(body_cols, spacing=6, tight=True),
        )

        # Робимо секцію товару згортаною (accordion) і зберігаємо стан розгортання.
        def _on_ctx_expanded(*, expanded: bool):
            if expanded:
                self._expanded_contexts.add(ctx_id)
            else:
                self._expanded_contexts.discard(ctx_id)

        return self._make_collapsible(
            hdr_click,
            body,
            expanded=(ctx_id in self._expanded_contexts),
            on_expanded=_on_ctx_expanded,
            tier="context",
        )

    def _build_orders_table(
        self,
        context_id: str,
        pack_row: dict,
        orders: list[dict],
        goods_supplier_name: str,
        *,
        packaging_supplier_label: str,
        show_usd_cost: bool,
        opening_snapshot: dict | None = None,
    ) -> ft.Control:
        base_vw = max(280, getattr(self, "_last_viewport_w", 960))
        try:
            cw = int(getattr(self.content_area, "width", 0) or 0)
            if cw > 0:
                base_vw = min(base_vw, cw)
        except Exception:
            pass
        # Ширину раніше брали як усю content_area → sum(columns) була > реальної ширини вкладених акордеонів.
        # 220 недостатньо: типово потрібно ~45–55% під padding/вкладення (web гірший за desktop).
        nest_reserve = max(420, min(880, int(base_vw * 0.54)))
        budget_vw = max(260, int(base_vw) - nest_reserve)
        specs = self._col_specs(
            show_usd_cost,
            packaging_supplier_label,
            goods_supplier_name,
            budget_w=budget_vw,
        )

        # Висота шапки як у orders_view._build_table_header (оцінка рядків + 18×рядки + відступ).
        def _est_header_lines(col: dict) -> int:
            txt = str(col.get("label") or "")
            width = max(40, int(col.get("width") or 80) - 2)
            chars_per_line = max(4, width // 5)
            needed = (len(txt) + chars_per_line - 1) // chars_per_line
            return max(1, min(4, needed))

        header_lines = max((_est_header_lines(c) for c in specs), default=1)
        hdr_h = max(34, int(self.ROW_H) + 8, 18 * header_lines + 8)
        w_sum = sum(int(c.get("width") or 0) for c in specs)
        self._log_pack(
            "orders_table START ctx=%s rows=%s cols=%s width_sum=%s hdr_h=%s viewport_raw=%s base_vw=%s nest_reserve≈%s budget_vw=%s pkg=%r goods=%r",
            str(context_id)[:16],
            len(orders or []),
            len(specs),
            w_sum,
            hdr_h,
            getattr(self, "_last_viewport_w", None),
            base_vw,
            int(base_vw) - budget_vw,
            budget_vw,
            (pack_row.get("name") if isinstance(pack_row, dict) else None) or "",
            (goods_supplier_name or "")[:40],
        )

        header_cells = [
            self._header_cell(
                str(c.get("label") or ""),
                int(c.get("width") or 40),
                hdr_h,
                bold=True,
                bg=self.BG_HDR,
                color=_CHIP_T,
            )
            for c in specs
        ]

        rows_col = ft.Column(spacing=0, tight=True)

        # History split: first HISTORY_ROW_LIMIT data rows are always visible;
        # older rows go into _hid_rows and are pre-built for instant expand.
        _vis_rows: list = []
        _hid_rows: list = []
        _open_rows: list = []  # opening balance row — always visible, always last
        _vis_count = [0]  # mutable so inner closures can read the live value

        def _append_row(widget, kind: str = "data") -> None:
            if kind == "open":
                _open_rows.append(widget)
            elif _vis_count[0] < PackagingStockView.HISTORY_ROW_LIMIT:
                _vis_rows.append(widget)
                _vis_count[0] += 1
            else:
                _hid_rows.append(widget)

        pack_price = _safe_float(pack_row.get("price"), 0.0) or 0.0
        ccy = self._packaging_currency(pack_row)
        oph = opening_snapshot if isinstance(opening_snapshot, dict) else {}
        op_bal_ccy = str(oph.get("balance_currency") or ccy or "USD").upper()
        has_opening_snapshot = _opening_journal_has_snapshot(oph)

        if not orders and not has_opening_snapshot:
            rows_col.controls.append(
                ft.Container(
                    padding=ft.padding.all(8),
                    content=self._text_wrap(
                        "Ще немає замовлень пакування. Натисніть «Замовити пакування».",
                        color="grey600",
                        size=11,
                    ),
                )
            )

        orders_dicts = [r for r in orders if isinstance(r, dict)]
        # Write-off rows from local cache — deduplicated by id
        _raw_wo = self._local_writeoff_cache.get(str(context_id) or "") or []
        _seen_wo: set[str] = set()
        writeoff_rows: list[dict] = []
        for _wor in _raw_wo:
            if not isinstance(_wor, dict):
                continue
            _woid = str(_wor.get("id") or "").strip()
            if _woid and _woid not in _seen_wo:
                _seen_wo.add(_woid)
                writeoff_rows.append(_wor)

        # All rows (supply + goods + write-offs) sorted chronologically oldest→newest
        rows_chrono = sorted(
            orders_dicts + writeoff_rows, key=_packaging_supply_chrono_sort_key
        )

        # UI display: newest date first; write-offs sort by date just like supply rows
        def _row_sort_key_ui(rr: dict) -> tuple:
            _ds = (_date_to_iso(rr.get("order_date")) or "").strip()[:10]
            if len(_ds) >= 10 and _ds[4] == "-" and _ds[7] == "-":
                try:
                    _d_inv = -int(_ds.replace("-", ""))
                except Exception:
                    _d_inv = 10**9
            else:
                _d_inv = 10**9
            _rk = str(rr.get("row_kind") or "").strip()
            _inv = str(rr.get("invoice_no") or "")
            _grk = 0 if _rk == "goods" else 1
            _seq = _safe_int_seq(rr.get("seq_no"), 0)
            _lk = str(rr.get("linked_order_line_key") or "")
            # Convert invoice to negative float for descending numeric sort:
            # invoice 286 → -286 sorts before -285 → 286 appears above 285.
            # Write-offs (non-numeric, e.g. "WO-1") get -inf so they appear
            # at the top of any same-date group (consistent with original behaviour).
            try:
                _inv_num = -float(_inv.replace(",", ".").strip())
            except Exception:
                _inv_num = float("-inf")
            if _rk == "writeoff":
                return (_d_inv, float("-inf"), _grk, -_seq, _lk)
            return (_d_inv, _inv_num, _grk, _seq, _lk)

        rows_sorted_ui = sorted(orders_dicts + writeoff_rows, key=_row_sort_key_ui)
        self._log_pack(
            "orders_table rows_ui=%s kinds_sample=%s writeoffs=%s",
            len(rows_sorted_ui),
            sorted({str(r.get("row_kind") or "").strip() for r in rows_sorted_ui})[:8],
            len(writeoff_rows),
        )
        # Balance: single chronological pass — write-offs interleave with orders by date.
        # Opening balance is the base.  A write-off on date X reduces balances for all
        # rows processed AFTER it (= rows with later date = shown ABOVE it in the display).
        leg0 = _opening_ledger_from_row(oph)
        pack_run = float(leg0["eff_pack"]) if has_opening_snapshot else 0.0
        goods_run = float(leg0["eff_goods"]) if has_opening_snapshot else 0.0

        bal_pack_after: dict[str, float] = {}
        bal_goods_after: dict[str, float] = {}
        # Secondary index keyed by seq_no for write-off rows — fallback when id changes
        # between local-pending ("writeoff:{ctx}:{seq}") and DB UUID states.
        bal_pack_after_seq: dict[int, float] = {}
        bal_goods_after_seq: dict[int, float] = {}
        for rr in rows_chrono:
            rk0 = str(rr.get("row_kind") or "").strip()
            rid0 = str(rr.get("id") or "").strip()
            if not rid0:
                continue
            if rk0 == "supply":
                qv = float(rr.get("qty_ordered") or 0)
                sh = max(0.0, float(rr.get("shipped_goods_supplier_qty") or 0))
                pack_run += qv - sh
                goods_run += sh
                bal_pack_after[rid0] = pack_run
                bal_goods_after[rid0] = goods_run
            elif rk0 == "goods":
                qv = float(rr.get("qty_ordered") or 0)
                goods_run -= qv
                bal_pack_after[rid0] = pack_run
                bal_goods_after[rid0] = goods_run
            elif rk0 == "writeoff":
                pack_run -= float(rr.get("pack_qty") or rr.get("qty") or 0)
                goods_run -= float(rr.get("goods_qty") or 0)
                bal_pack_after[rid0] = pack_run
                bal_goods_after[rid0] = goods_run
                _wo_seq_k = int(rr.get("seq_no") or 0)
                if _wo_seq_k:
                    bal_pack_after_seq[_wo_seq_k] = pack_run
                    bal_goods_after_seq[_wo_seq_k] = goods_run

        rows_for_ui: list[dict] = list(rows_sorted_ui)
        # "Перший місяць відображення" — ховає лише СТАРІ рядки з рендеру.
        # bal_pack_after/bal_goods_after вище вже пораховані по ПОВНІЙ rows_chrono,
        # тому баланс "після" на видимих рядках лишається коректним (враховує приховану історію).
        _dfm = (self._display_from_month or "").strip()
        if _dfm:
            rows_for_ui = [
                r for r in rows_for_ui if str(r.get("order_date") or "")[:7] >= _dfm
            ]
        if has_opening_snapshot:
            rows_for_ui.append(
                {
                    "id": f"pk_open:{context_id}",
                    "row_kind": PackagingStockView.OPENING_ROW_KIND,
                    "seq_no": 0,
                    "order_date": None,
                    "qty_ordered": 0,
                    "status": "",
                    "comment": "Початкові залишки",
                }
            )

        # Групування товарних рядків одного замовлення: якщо кілька goods-рядків
        # з одним linked_order_id → відображаємо «шапку» + кнопку «+» + жовті підрядки.
        _goods_by_order: dict[str, list[dict]] = {}
        for _rr in rows_for_ui:
            if str(_rr.get("row_kind") or "").strip() == "goods":
                _loid = str(_rr.get("linked_order_id") or "").strip()
                if _loid:
                    _goods_by_order.setdefault(_loid, []).append(_rr)
        # linked_order_id де > 1 goods-рядків у цьому контексті
        _multi_goods_loids: set[str] = {
            k for k, v in _goods_by_order.items() if len(v) > 1
        }
        # ids що вже відрендерено у складі групи
        _rendered_goods_ids: set[str] = set()

        def _base_invoice(inv: str) -> str:
            """Повертає базовий інвойс без суфіксу /01: 'ABC/01' → 'ABC'."""
            s = str(inv or "").strip()
            if "/" in s:
                parts = s.rsplit("/", 1)
                if len(parts) == 2 and parts[1].isdigit():
                    return parts[0]
            return s

        # Counters for goods-row alternation and supply-row tracking
        _goods_row_idx = 0
        wmap = {str(c.get("key")): int(c.get("width") or 40) for c in specs}
        # Ensure all possible column keys exist in wmap so wmap["key"] never raises KeyError
        # when columns are hidden via the column-toggle dialog. Value 0 means "hidden".
        for _k in self._pack_columns_all_keys():
            wmap.setdefault(_k, 0)

        # Pre-compute: кожному goods-рядку призначаємо seq_no найближчого попереднього
        # supply-замовлення (в хронологічному порядку oldest→newest).
        _goods_supply_seq: dict[str, int] = {}
        _cur_supply_seq = 0
        for _rr in rows_chrono:
            _rk0 = str(_rr.get("row_kind") or "").strip()
            _rid0 = str(_rr.get("id") or "").strip()
            if not _rid0:
                continue
            if _rk0 == "supply":
                _cur_supply_seq = _safe_int_seq(_rr.get("seq_no"), 0) or _cur_supply_seq
            elif _rk0 == "goods":
                _goods_supply_seq[_rid0] = _cur_supply_seq

        for row in rows_for_ui:
            oid = str(row.get("id") or "").strip()
            rk = str(row.get("row_kind") or "").strip()

            # Goods-рядки, що вже відрендерені у складі групи — пропускаємо.
            if rk == "goods" and oid in _rendered_goods_ids:
                continue

            # Якщо це перший рядок мульти-goods-групи — рендеримо групу цілком.
            if rk == "goods":
                _loid_this = str(row.get("linked_order_id") or "").strip()
                if _loid_this in _multi_goods_loids:
                    grp = _goods_by_order.get(_loid_this, [])
                    for _gr in grp:
                        _rendered_goods_ids.add(str(_gr.get("id") or ""))
                    _is_grp_expanded = _loid_this in self._expanded_pack_order_groups
                    # Базовий інвойс для шапки групи
                    _grp_inv_base = _base_invoice(str(grp[0].get("invoice_no") or ""))
                    _grp_zp_base = _base_invoice(str(grp[0].get("zp_1c") or ""))
                    _grp_date = str(grp[0].get("order_date") or "")[:10]
                    _grp_qty_total = sum(float(r.get("qty_ordered") or 0) for r in grp)
                    _grp_cost_total = sum(float(r.get("cost_amount") or 0) for r in grp)
                    # Збираємо унікальні частини коментарів (до першої "; "), щоб уникнути дублювання
                    _seen_cmt: set[str] = set()
                    _unique_cmt_parts: list[str] = []
                    for _r in grp:
                        _part = str(_r.get("comment") or "").split(";")[0].strip()
                        if _part and _part not in _seen_cmt:
                            _seen_cmt.add(_part)
                            _unique_cmt_parts.append(_part)
                    _grp_comments = "; ".join(_unique_cmt_parts)
                    _grp_bp_end = bal_pack_after.get(str(grp[-1].get("id") or ""))
                    _grp_bg_end = bal_goods_after.get(str(grp[-1].get("id") or ""))
                    _grp_bal_pack_disp = (
                        "—" if _grp_bp_end is None else _fmt_qty(_grp_bp_end)
                    )
                    _grp_bal_goods_disp = (
                        "—" if _grp_bg_end is None else _fmt_qty(_grp_bg_end)
                    )
                    _grp_bg_hdr = _PANEL if _goods_row_idx % 2 == 0 else _ALT
                    _grp_row_h = self._calc_wrap_row_height(
                        _grp_inv_base,
                        _grp_comments[:60],
                        wmap["inv"] or 50,
                        wmap["cmt"] or 120,
                        self.ROW_H,
                    )
                    _exp_lbl = ft.Text(
                        "−" if _is_grp_expanded else "+",
                        size=self.TXT,
                        weight="bold",
                        color=_TEXT,
                    )
                    _grp_ck: dict = {}
                    if wmap["seq"]:
                        _grp_ck["seq"] = self._cell(
                            "", wmap["seq"], _grp_row_h, bg=_grp_bg_hdr
                        )
                    if wmap["d"]:
                        _grp_ck["d"] = self._cell(
                            _grp_date, wmap["d"], _grp_row_h, bg=_grp_bg_hdr
                        )
                    if wmap["qty"]:
                        _grp_ck["qty"] = self._cell(
                            _fmt_qty(_grp_qty_total),
                            wmap["qty"],
                            _grp_row_h,
                            bg=_grp_bg_hdr,
                        )
                    if wmap["st"]:
                        _grp_ck["st"] = self._cell(
                            "", wmap["st"], _grp_row_h, bg=_grp_bg_hdr
                        )
                    if wmap["cost"]:
                        _grp_ck["cost"] = self._cell(
                            _fmt_money(_grp_cost_total) if _grp_cost_total else "—",
                            wmap["cost"],
                            _grp_row_h,
                            bg=_grp_bg_hdr,
                        )
                    if wmap["cost_usd"] and show_usd_cost:
                        _gu = self._packaging_amount_to_usd(float(_grp_cost_total), ccy)
                        _grp_ck["cost_usd"] = self._cell(
                            _fmt_money(_gu) if _gu is not None else "—",
                            wmap["cost_usd"],
                            _grp_row_h,
                            bg=_grp_bg_hdr,
                        )
                    if wmap["paid"]:
                        _grp_ck["paid"] = self._cell(
                            "—", wmap["paid"], _grp_row_h, bg=_grp_bg_hdr
                        )
                    if wmap["ship"]:
                        _grp_ck["ship"] = self._cell(
                            "—", wmap["ship"], _grp_row_h, bg=_grp_bg_hdr
                        )
                    if wmap["bal_pack"]:
                        _grp_ck["bal_pack"] = self._cell(
                            _grp_bal_pack_disp,
                            wmap["bal_pack"],
                            _grp_row_h,
                            bg=_grp_bg_hdr,
                        )
                    if wmap["bal_goods"]:
                        _grp_ck["bal_goods"] = self._cell(
                            _grp_bal_goods_disp,
                            wmap["bal_goods"],
                            _grp_row_h,
                            bg=_grp_bg_hdr,
                        )
                    if wmap["zp"]:
                        _grp_ck["zp"] = self._cell(
                            _grp_zp_base, wmap["zp"], _grp_row_h, bg=_grp_bg_hdr
                        )
                    if wmap["inv"]:
                        _grp_ck["inv"] = self._cell_wrap(
                            _grp_inv_base, wmap["inv"], _grp_row_h, bg=_grp_bg_hdr
                        )
                    if wmap["cmt"]:
                        _grp_ck["cmt"] = self._cell_wrap(
                            _grp_comments[:60], wmap["cmt"], _grp_row_h, bg=_grp_bg_hdr
                        )
                    if wmap["del"]:
                        _grp_ck["del"] = ft.Container(
                            width=wmap["del"],
                            height=_grp_row_h,
                            bgcolor=_grp_bg_hdr,
                            border=ft.border.all(1, self.BDR),
                            alignment=ft.Alignment(0, 0),
                            content=ft.TextButton(
                                content=_exp_lbl,
                                style=ft.ButtonStyle(padding=ft.padding.all(0)),
                                width=wmap["del"],
                                height=_grp_row_h,
                                tooltip=f"Розгорнути/згорнути ({len(grp)} поз.)",
                            ),
                        )
                    _hdr_cells_grp = [
                        _grp_ck[c["key"]] for c in specs if c["key"] in _grp_ck
                    ]
                    # Підрядки групи (жовта заливка, read-only summary)
                    _BG_SUB = "#FEFCE8"
                    _sub_rows_col = ft.Column(spacing=0, visible=_is_grp_expanded)

                    def _build_group_sub_rows() -> None:
                        if _sub_rows_col.controls:
                            return
                        for _sr in grp:
                            _sr_inv = str(_sr.get("invoice_no") or "")
                            _sr_zp = str(_sr.get("zp_1c") or "")
                            _sr_date = str(_sr.get("order_date") or "")[:10]
                            _sr_qty = _fmt_qty(float(_sr.get("qty_ordered") or 0))
                            _sr_cmt = str(_sr.get("comment") or "")
                            _sr_cost = _fmt_money(_sr.get("cost_amount"))
                            _sr_bp = bal_pack_after.get(str(_sr.get("id") or ""))
                            _sr_bg = bal_goods_after.get(str(_sr.get("id") or ""))
                            _sr_row_h = self._calc_wrap_row_height(
                                _sr_inv,
                                _sr_cmt,
                                wmap["inv"] or 50,
                                wmap["cmt"] or 120,
                                self.ROW_H,
                            )
                            _sr_ck: dict = {}
                            if wmap["seq"]:
                                _sr_ck["seq"] = self._cell(
                                    "", wmap["seq"], _sr_row_h, bg=_BG_SUB
                                )
                            if wmap["d"]:
                                _sr_ck["d"] = self._cell(
                                    _sr_date, wmap["d"], _sr_row_h, bg=_BG_SUB
                                )
                            if wmap["qty"]:
                                _sr_ck["qty"] = self._cell(
                                    _sr_qty, wmap["qty"], _sr_row_h, bg=_BG_SUB
                                )
                            if wmap["st"]:
                                _sr_ck["st"] = self._cell(
                                    "", wmap["st"], _sr_row_h, bg=_BG_SUB
                                )
                            if wmap["cost"]:
                                _sr_ck["cost"] = self._cell(
                                    _sr_cost, wmap["cost"], _sr_row_h, bg=_BG_SUB
                                )
                            if wmap["cost_usd"] and show_usd_cost:
                                _ca = _safe_float(_sr.get("cost_amount"), None)
                                _uv = (
                                    self._packaging_amount_to_usd(float(_ca), ccy)
                                    if _ca is not None
                                    else None
                                )
                                _sr_ck["cost_usd"] = self._cell(
                                    _fmt_money(_uv) if _uv is not None else "—",
                                    wmap["cost_usd"],
                                    _sr_row_h,
                                    bg=_BG_SUB,
                                )
                            if wmap["paid"]:
                                _sr_ck["paid"] = self._cell(
                                    "—", wmap["paid"], _sr_row_h, bg=_BG_SUB
                                )
                            if wmap["ship"]:
                                _sr_ck["ship"] = self._cell(
                                    "—", wmap["ship"], _sr_row_h, bg=_BG_SUB
                                )
                            if wmap["bal_pack"]:
                                _sr_ck["bal_pack"] = self._cell(
                                    "—" if _sr_bp is None else _fmt_qty(_sr_bp),
                                    wmap["bal_pack"],
                                    _sr_row_h,
                                    bg=_BG_SUB,
                                )
                            if wmap["bal_goods"]:
                                _sr_ck["bal_goods"] = self._cell(
                                    "—" if _sr_bg is None else _fmt_qty(_sr_bg),
                                    wmap["bal_goods"],
                                    _sr_row_h,
                                    bg=_BG_SUB,
                                )
                            if wmap["zp"]:
                                _sr_ck["zp"] = self._cell(
                                    _sr_zp, wmap["zp"], _sr_row_h, bg=_BG_SUB
                                )
                            if wmap["inv"]:
                                _sr_ck["inv"] = self._cell_wrap(
                                    _sr_inv, wmap["inv"], _sr_row_h, bg=_BG_SUB
                                )
                            if wmap["cmt"]:
                                _sr_oid = str(_sr.get("id") or "")
                                _sr_ctx = str(context_id or "")

                                def _click_sr_cmt(
                                    _e, _oid=_sr_oid, _ctx=_sr_ctx, _cv=_sr_cmt
                                ):
                                    def _on_save(v, __oid=_oid, __ctx=_ctx):
                                        self._apply_local_supply_order_patch(
                                            __oid, {"comment": v}
                                        )
                                        self._persist_supply_order_patch_async(
                                            __oid, {"comment": v}
                                        )
                                        if not self._refresh_pack_ctx_inplace(__ctx):
                                            self._schedule_debounced_reload(0.15)

                                    self._open_comment_editor(_cv, _on_save)

                                _sr_ck["cmt"] = ft.Container(
                                    width=wmap["cmt"],
                                    height=_sr_row_h,
                                    bgcolor=_BG_SUB,
                                    border=ft.border.all(1, self.BDR),
                                    alignment=ft.Alignment(0, 0),
                                    ink=not self.subordinate_ro,
                                    on_click=(
                                        None if self.subordinate_ro else _click_sr_cmt
                                    ),
                                    tooltip=(
                                        "Змінити коментар"
                                        if not self.subordinate_ro
                                        else None
                                    ),
                                    padding=ft.padding.symmetric(horizontal=4),
                                    content=ft.Text(
                                        _sr_cmt,
                                        size=self.TXT,
                                        color=_STONE,
                                        text_align=ft.TextAlign.CENTER,
                                        no_wrap=False,
                                    ),
                                )
                            if wmap["del"]:
                                _sr_ck["del"] = ft.Container(
                                    width=wmap["del"],
                                    height=_sr_row_h,
                                    bgcolor=_BG_SUB,
                                    border=ft.border.all(1, self.BDR),
                                )
                            _sub_cells = [
                                _sr_ck[c["key"]] for c in specs if c["key"] in _sr_ck
                            ]
                            _sub_rows_col.controls.append(
                                ft.Row(_sub_cells, spacing=0, intrinsic_height=True)
                            )

                    if _is_grp_expanded:
                        _build_group_sub_rows()

                    def _toggle_grp(
                        _e, _lid=_loid_this, _col=_sub_rows_col, _lbl=_exp_lbl
                    ):
                        if _lid in self._expanded_pack_order_groups:
                            self._expanded_pack_order_groups.discard(_lid)
                        else:
                            self._expanded_pack_order_groups.add(_lid)
                        _col.visible = _lid in self._expanded_pack_order_groups
                        if _col.visible and not _col.controls:
                            _build_group_sub_rows()
                        _lbl.value = "−" if _col.visible else "+"
                        # Fast-path: update only affected controls (much faster than full page update).
                        try:
                            _lbl.update()
                            _col.update()
                        except Exception:
                            try:
                                (self.content_area.page or self.content_area).update()
                            except Exception:
                                self._schedule_debounced_reload()

                    # Wire toggle to the "del" cell if visible, else the last visible cell
                    _toggle_target = _grp_ck.get("del") or (
                        _hdr_cells_grp[-1] if _hdr_cells_grp else None
                    )
                    if (
                        _toggle_target
                        and getattr(_toggle_target, "content", None) is not None
                    ):
                        _toggle_target.content.on_click = _toggle_grp
                    elif _toggle_target:
                        _toggle_target.on_click = _toggle_grp
                    _append_row(
                        ft.Column(
                            [
                                ft.Row(
                                    _hdr_cells_grp, spacing=0, intrinsic_height=True
                                ),
                                _sub_rows_col,
                            ],
                            spacing=0,
                            tight=True,
                        )
                    )
                    _goods_row_idx += len(
                        grp
                    )  # sub-rows also count for next group's alternation
                    continue  # Вся група відрендерена, переходимо до наступного рядка

            if rk == PackagingStockView.OPENING_ROW_KIND:
                ledger_r = _opening_ledger_from_row(oph)
                bg_op = _OK_BG  # світло-зелений
                extra_usd_op: list = []
                gross_m = float(ledger_r["bal_gross"])
                if show_usd_cost:
                    u_est = self._packaging_amount_to_usd(gross_m, op_bal_ccy)
                    usd_txt = _fmt_money(u_est) if u_est is not None else "—"
                    extra_usd_op.append(
                        self._cell(usd_txt, wmap["cost_usd"], self.ROW_H, bg=bg_op)
                    )
                cost_disp_open = _fmt_money(gross_m) if abs(gross_m) > 1e-12 else "—"
                pk_disp = (
                    _fmt_qty(ledger_r["eff_pack"])
                    if abs(ledger_r["eff_pack"]) > 1e-12
                    else "—"
                )
                gd_disp = (
                    _fmt_qty(ledger_r["eff_goods"])
                    if abs(ledger_r["eff_goods"]) > 1e-12
                    else "—"
                )
                cmt_op = str(row.get("comment") or "").strip() or "Початкові залишки"
                _op_row_h = self._calc_wrap_row_height(
                    "—",
                    cmt_op,
                    wmap["inv"],
                    wmap["cmt"],
                    self.ROW_H,
                )
                cid_open = str(context_id or "").strip()

                def _blur_ship_o(e):
                    self._persist_opening_transfer_or_paid(
                        cid_open,
                        shipped_goods_qty=float(_safe_float(e.control.value, 0) or 0),
                    )

                def _blur_paid_o(e):
                    self._persist_opening_transfer_or_paid(
                        cid_open,
                        balance_paid_amount=float(_safe_float(e.control.value, 0) or 0),
                    )

                cost_ro = self._tf(
                    cost_disp_open,
                    wmap["cost"] or 80,
                    _op_row_h,
                    read_only=True,
                )
                ship_ed = self._tf(
                    _fmt_qty(ledger_r["ship"]),
                    wmap["ship"] or 70,
                    _op_row_h,
                    read_only=self.subordinate_ro,
                    numeric=True,
                    on_blur=None if self.subordinate_ro else _blur_ship_o,
                )
                paid_ed = self._tf(
                    _fmt_money(ledger_r["paid"]),
                    wmap["paid"] or 80,
                    _op_row_h,
                    read_only=self.subordinate_ro,
                    numeric=True,
                    on_blur=None if self.subordinate_ro else _blur_paid_o,
                )
                _op_ck: dict = {}
                if wmap["seq"]:
                    _op_ck["seq"] = self._cell("—", wmap["seq"], _op_row_h, bg=bg_op)
                if wmap["d"]:
                    _op_ck["d"] = self._cell("—", wmap["d"], _op_row_h, bg=bg_op)
                if wmap["qty"]:
                    # Clickable qty cell opens the opening dialog for editing
                    _op_ctx = next(
                        (
                            c
                            for c in (self._packaging_items or [])
                            if str((c or {}).get("id") or "") == cid_open
                        ),
                        {},
                    )
                    _op_qty_disp = (
                        pk_disp
                        if pk_disp != "—"
                        else (gd_disp if gd_disp != "—" else "—")
                    )
                    _op_ck["qty"] = ft.Container(
                        width=wmap["qty"],
                        height=_op_row_h,
                        bgcolor=bg_op,
                        border=ft.border.all(1, self.BDR),
                        alignment=ft.Alignment(0, 0),
                        ink=not self.subordinate_ro,
                        tooltip=(
                            "Редагувати початковий залишок"
                            if not self.subordinate_ro
                            else None
                        ),
                        on_click=(
                            None
                            if self.subordinate_ro
                            else (
                                lambda _e, _c=_op_ctx, _pr=pack_row: self._open_opening_dialog(
                                    _c, _pr
                                )
                            )
                        ),
                        content=ft.Text(
                            _op_qty_disp,
                            size=self.TXT,
                            color=_ACCENT if not self.subordinate_ro else _STONE,
                            text_align=ft.TextAlign.CENTER,
                            no_wrap=True,
                        ),
                    )
                if wmap["st"]:
                    _op_ck["st"] = self._cell("—", wmap["st"], _op_row_h, bg=bg_op)
                if wmap["cost"]:
                    _op_ck["cost"] = ft.Container(
                        width=wmap["cost"],
                        height=_op_row_h,
                        bgcolor=bg_op,
                        border=ft.border.all(1, self.BDR),
                        padding=0,
                        content=cost_ro,
                    )
                if wmap["cost_usd"] and show_usd_cost and extra_usd_op:
                    _op_ck["cost_usd"] = extra_usd_op[0]
                if wmap["paid"]:
                    _op_ck["paid"] = ft.Container(
                        width=wmap["paid"],
                        height=_op_row_h,
                        bgcolor=bg_op,
                        border=ft.border.all(1, self.BDR),
                        padding=0,
                        content=paid_ed,
                    )
                if wmap["ship"]:
                    _op_ck["ship"] = ft.Container(
                        width=wmap["ship"],
                        height=_op_row_h,
                        bgcolor=bg_op,
                        border=ft.border.all(1, self.BDR),
                        padding=0,
                        content=ship_ed,
                    )
                if wmap["bal_pack"]:
                    _op_ck["bal_pack"] = self._cell(
                        pk_disp, wmap["bal_pack"], _op_row_h, bg=bg_op
                    )
                if wmap["bal_goods"]:
                    _op_ck["bal_goods"] = self._cell(
                        gd_disp, wmap["bal_goods"], _op_row_h, bg=bg_op
                    )
                if wmap["zp"]:
                    _op_ck["zp"] = self._cell("—", wmap["zp"], _op_row_h, bg=bg_op)
                if wmap["inv"]:
                    _op_ck["inv"] = self._cell_wrap(
                        "—", wmap["inv"], _op_row_h, bg=bg_op
                    )
                if wmap["cmt"]:
                    _cmt_op_cid = cid_open

                    def _click_op_cmt(_e, _c=_cmt_op_cid, _cv=cmt_op):
                        self._open_comment_editor(
                            _cv,
                            lambda v, __c=_c: self._persist_opening_comment(__c, v),
                        )

                    _op_ck["cmt"] = ft.Container(
                        width=wmap["cmt"],
                        height=_op_row_h,
                        bgcolor=bg_op,
                        border=ft.border.all(1, self.BDR),
                        alignment=ft.Alignment(0, 0),
                        ink=not self.subordinate_ro,
                        on_click=None if self.subordinate_ro else _click_op_cmt,
                        tooltip="Змінити коментар" if not self.subordinate_ro else None,
                        padding=ft.padding.symmetric(horizontal=4),
                        content=ft.Text(
                            cmt_op,
                            size=self.TXT,
                            color=_STONE,
                            text_align=ft.TextAlign.CENTER,
                            no_wrap=False,
                        ),
                    )
                if wmap["del"]:
                    _op_ck["del"] = ft.Container(
                        width=wmap["del"],
                        height=_op_row_h,
                        bgcolor=bg_op,
                        border=ft.border.all(1, self.BDR),
                    )
                cells_open = [_op_ck[c["key"]] for c in specs if c["key"] in _op_ck]
                _append_row(
                    ft.Row(cells_open, spacing=0, intrinsic_height=True), "open"
                )
                continue

            # ── Write-off row (Списання пакування) ──────────────────────────
            if rk == "writeoff":
                _bg_wo = "#FEF2F2"  # light red
                _wo_pack_qty = float(row.get("pack_qty") or row.get("qty") or 0)
                _wo_goods_qty = float(row.get("goods_qty") or 0)
                _wo_bp = bal_pack_after.get(oid)
                _wo_bg = bal_goods_after.get(oid)
                # Fallback: if id changed between local-pending and DB states, look up by seq_no
                if _wo_bp is None or _wo_bg is None:
                    _fb_seq = int(row.get("seq_no") or 0)
                    if _fb_seq:
                        if _wo_bp is None:
                            _wo_bp = bal_pack_after_seq.get(_fb_seq)
                        if _wo_bg is None:
                            _wo_bg = bal_goods_after_seq.get(_fb_seq)
                _wo_cmt = str(row.get("comment") or "Списання")
                _wo_inv = str(row.get("invoice_no") or "")
                _wo_seq = int(row.get("seq_no") or 0)

                def _del_writeoff(_e, _oid=oid, _ctx_id=context_id, _seq=_wo_seq):
                    if self.subordinate_ro:
                        return
                    _oid_s = str(_oid)
                    _ctx_s = str(_ctx_id)
                    # 1. Remove from local cache immediately — match by id only to
                    # avoid accidentally deleting other rows that share seq_no=0.
                    self._local_writeoff_cache[_ctx_s] = [
                        r
                        for r in (self._local_writeoff_cache.get(_ctx_s) or [])
                        if str(r.get("id") or "") != _oid_s
                    ]
                    # 2. Remove any pending save outbox entry for this seq
                    for _k in list(self._ops_outbox.keys()):
                        _op = self._ops_outbox.get(_k) or {}
                        if _k == _oid_s or (
                            str(_op.get("kwargs", {}).get("seq_no") or "") == str(_seq)
                            and str(_op.get("kwargs", {}).get("context_id") or "")
                            == _ctx_s
                        ):
                            self._ops_outbox.pop(_k, None)
                    # 3. Queue delete in outbox only if the row exists in DB (real UUID id)
                    _is_db_row = not _oid_s.startswith("writeoff:")
                    if _is_db_row:
                        _del_key = f"writeoff_del:{_ctx_s}:{_seq}"
                        self._ops_outbox[_del_key] = {
                            "kind": "writeoff_delete",
                            "kwargs": {
                                "context_id": _ctx_s,
                                "seq_no": _seq,
                                "id": _oid_s,
                                "manager_id": self.manager_id,
                                "region_id": str(
                                    getattr(self, "_region_id_live", "") or ""
                                ),
                                "company_id": self.company_id,
                            },
                        }
                        # Guard sync from restoring this row until outbox flushed
                        self._local_writeoff_delete_pending.add(_oid_s)
                    self._save_pack_outbox()
                    self._refresh_pack_sync_status()
                    self._refresh_pack_ctx_inplace(_ctx_s)

                _wo_date_raw = str(row.get("order_date") or "").strip()[:10]
                _wo_date_disp = _wo_date_raw if len(_wo_date_raw) == 10 else "—"

                # Capture row snapshot for closures
                _wo_row_snap = dict(row)
                _wo_ctx_id = str(context_id or "")

                def _click_wo_date(_e, _cid=_wo_ctx_id, _r=_wo_row_snap):
                    self._open_writeoff_date_picker(_cid, _r)

                def _click_wo_pack(_e, _cid=_wo_ctx_id, _r=_wo_row_snap):
                    self._open_writeoff_qty_editor(_cid, _r, "pack_qty")

                def _click_wo_goods(_e, _cid=_wo_ctx_id, _r=_wo_row_snap):
                    self._open_writeoff_qty_editor(_cid, _r, "goods_qty")

                def _click_wo_cmt(_e, _cid=_wo_ctx_id, _r=_wo_row_snap):
                    _cur_cmt = str(_r.get("comment") or "Списання")
                    self._open_comment_editor(
                        _cur_cmt,
                        lambda v, __cid=_cid, __r=_r: self._update_writeoff_field(
                            __cid, __r, "comment", v
                        ),
                    )

                _wo_ck: dict = {}
                if wmap["seq"]:
                    _wo_ck["seq"] = self._cell(
                        str(_wo_seq) if _wo_seq else "—",
                        wmap["seq"],
                        self.ROW_H,
                        bg=_bg_wo,
                    )
                if wmap["d"]:
                    # Clickable date cell — opens date picker inline
                    _wo_ck["d"] = ft.Container(
                        width=wmap["d"],
                        height=self.ROW_H,
                        bgcolor=_bg_wo,
                        border=ft.border.all(1, self.BDR),
                        alignment=ft.Alignment(0, 0),
                        ink=not self.subordinate_ro,
                        on_click=None if self.subordinate_ro else _click_wo_date,
                        tooltip="Змінити дату" if not self.subordinate_ro else None,
                        content=ft.Text(
                            _wo_date_disp,
                            size=self.TXT,
                            color=_ACCENT if not self.subordinate_ro else _STONE,
                            text_align=ft.TextAlign.CENTER,
                            no_wrap=True,
                        ),
                    )
                if wmap["qty"]:
                    # Split cell: top = pack_qty, bottom = goods_qty, each clickable
                    _pack_str = f"−{_fmt_qty(_wo_pack_qty)}" if _wo_pack_qty else "0"
                    _goods_str = f"−{_fmt_qty(_wo_goods_qty)}" if _wo_goods_qty else "0"
                    _sub_h = self.ROW_H // 2
                    _wo_ck["qty"] = ft.Container(
                        width=wmap["qty"],
                        height=self.ROW_H,
                        bgcolor=_bg_wo,
                        border=ft.border.all(1, self.BDR),
                        padding=0,
                        content=ft.Column(
                            [
                                ft.Container(
                                    width=wmap["qty"],
                                    height=_sub_h,
                                    border=ft.border.only(
                                        bottom=ft.BorderSide(1, self.BDR)
                                    ),
                                    alignment=ft.Alignment(0, 0),
                                    ink=not self.subordinate_ro,
                                    on_click=(
                                        None if self.subordinate_ro else _click_wo_pack
                                    ),
                                    tooltip=(
                                        "Склад пост. пакування"
                                        if not self.subordinate_ro
                                        else None
                                    ),
                                    content=ft.Text(
                                        _pack_str,
                                        size=self.TXT,
                                        weight="bold",
                                        color=_ERR,
                                        text_align=ft.TextAlign.CENTER,
                                        no_wrap=True,
                                    ),
                                ),
                                ft.Container(
                                    width=wmap["qty"],
                                    height=_sub_h,
                                    alignment=ft.Alignment(0, 0),
                                    ink=not self.subordinate_ro,
                                    on_click=(
                                        None if self.subordinate_ro else _click_wo_goods
                                    ),
                                    tooltip=(
                                        "Склад пост. товару"
                                        if not self.subordinate_ro
                                        else None
                                    ),
                                    content=ft.Text(
                                        _goods_str,
                                        size=self.TXT,
                                        weight="bold",
                                        color=_SKY,
                                        text_align=ft.TextAlign.CENTER,
                                        no_wrap=True,
                                    ),
                                ),
                            ],
                            spacing=0,
                            tight=True,
                        ),
                    )
                if wmap["st"]:
                    _wo_ck["st"] = self._cell(
                        "Списання", wmap["st"], self.ROW_H, bg=_bg_wo, color=_ERR
                    )
                if wmap["cost"]:
                    _wo_ck["cost"] = self._cell(
                        "—", wmap["cost"], self.ROW_H, bg=_bg_wo
                    )
                if wmap["cost_usd"]:
                    _wo_ck["cost_usd"] = self._cell(
                        "—", wmap["cost_usd"], self.ROW_H, bg=_bg_wo
                    )
                if wmap["paid"]:
                    _wo_ck["paid"] = self._cell(
                        "—", wmap["paid"], self.ROW_H, bg=_bg_wo
                    )
                if wmap["ship"]:
                    _wo_ck["ship"] = self._cell(
                        "—", wmap["ship"], self.ROW_H, bg=_bg_wo
                    )
                if wmap["bal_pack"]:
                    _wo_ck["bal_pack"] = self._cell(
                        "—" if _wo_bp is None else _fmt_qty(_wo_bp),
                        wmap["bal_pack"],
                        self.ROW_H,
                        bg=_bg_wo,
                        color=_ERR if _wo_bp is not None and _wo_bp < 0 else None,
                    )
                if wmap["bal_goods"]:
                    _wo_ck["bal_goods"] = self._cell(
                        "—" if _wo_bg is None else _fmt_qty(_wo_bg),
                        wmap["bal_goods"],
                        self.ROW_H,
                        bg=_bg_wo,
                        color=_ERR if _wo_bg is not None and _wo_bg < 0 else None,
                    )
                if wmap["zp"]:
                    _wo_ck["zp"] = self._cell("—", wmap["zp"], self.ROW_H, bg=_bg_wo)
                if wmap["inv"]:
                    _wo_ck["inv"] = self._cell(
                        _wo_inv, wmap["inv"], self.ROW_H, bg=_bg_wo
                    )
                if wmap["cmt"]:
                    _wo_ck["cmt"] = ft.Container(
                        width=wmap["cmt"],
                        height=self.ROW_H,
                        bgcolor=_bg_wo,
                        border=ft.border.all(1, self.BDR),
                        alignment=ft.Alignment(0, 0),
                        ink=not self.subordinate_ro,
                        on_click=None if self.subordinate_ro else _click_wo_cmt,
                        tooltip="Змінити коментар" if not self.subordinate_ro else None,
                        padding=ft.padding.symmetric(horizontal=4),
                        content=ft.Text(
                            _wo_cmt,
                            size=self.TXT,
                            color=_STONE,
                            text_align=ft.TextAlign.CENTER,
                            no_wrap=False,
                        ),
                    )
                if wmap["del"] and not self.subordinate_ro:
                    _wo_ck["del"] = ft.Container(
                        width=wmap["del"],
                        height=self.ROW_H,
                        bgcolor=_bg_wo,
                        border=ft.border.all(1, self.BDR),
                        alignment=ft.Alignment(0, 0),
                        content=ft.IconButton(
                            icon=ft.Icons.DELETE_OUTLINE,
                            icon_color=_ERR,
                            icon_size=16,
                            tooltip="Видалити списання",
                            on_click=_del_writeoff,
                            style=ft.ButtonStyle(padding=ft.padding.all(0)),
                        ),
                    )
                elif wmap["del"]:
                    _wo_ck["del"] = ft.Container(
                        width=wmap["del"],
                        height=self.ROW_H,
                        bgcolor=_bg_wo,
                        border=ft.border.all(1, self.BDR),
                    )
                _append_row(
                    ft.Row(
                        [_wo_ck[c["key"]] for c in specs if c["key"] in _wo_ck],
                        spacing=0,
                        intrinsic_height=True,
                    )
                )
                continue

            qty = float(row.get("qty_ordered") or 0)
            shipped = float(row.get("shipped_goods_supplier_qty") or 0)
            pack_unit_px = (
                float(_safe_float(pack_row.get("price"), 0) or 0)
                if isinstance(pack_row, dict)
                else 0.0
            )

            # Рядки замовлення (goods): ЗП / інвойс / дата / кількість синхронізуються із «Замовлення», зміни тут записуються в orders.
            is_goods = rk == "goods"

            bp_end = bal_pack_after.get(oid)
            bg_end = bal_goods_after.get(oid)
            bal_pack_disp = "—" if bp_end is None else _fmt_qty(bp_end)
            if is_goods:
                bg_goods = _PANEL if _goods_row_idx % 2 == 0 else _ALT
                _goods_row_idx += 1
            else:
                # Supply rows: uniform light-blue (like "Компенсація" in payments)
                bg_goods = _SKY_BG

            def _commit_factory(field: str, conv=None, *, row_order_id: str):
                """Не покладатися на `oid` з циклу — для on_blur (асинхронно) він був би останнім рядком."""
                roid = str(row_order_id or "").strip()

                def _inner(e):
                    if not roid:
                        logger.warning(
                            "pack_stock inline_commit: порожній row_order_id, field=%s",
                            field,
                        )
                        return
                    ctrl = e.control
                    raw_ui = ctrl.value if ctrl is not None else ""
                    payload = {}
                    if field == "order_date":
                        payload["order_date"] = str(raw_ui or "").strip()[:10] or None
                    elif conv is not None:
                        s = str(raw_ui or "").strip()
                        if field in _PACKAGING_SUPPLY_NUMERIC_PAYLOAD_FIELDS:
                            s = s.replace(" ", "").replace(",", ".")
                        try:
                            payload[field] = conv(s)
                        except Exception as conv_exc:
                            logger.warning(
                                "pack_stock inline_commit: невдале перетворення oid=%s field=%s raw=%r: %s",
                                roid[:20],
                                field,
                                raw_ui,
                                conv_exc,
                            )
                            return
                    else:
                        payload[field] = str(raw_ui or "")

                    if field == "invoice_no":
                        _new_inv = str(payload.get("invoice_no") or "").strip()
                        if _new_inv and self._is_pack_invoice_dup(roid, _new_inv):
                            _old_inv = next(
                                (
                                    str(o.get("invoice_no") or "")
                                    for _ctx in self._local_orders_cache.values()
                                    for o in _ctx
                                    if isinstance(o, dict)
                                    and str(o.get("id") or "") == roid
                                ),
                                "",
                            )
                            if ctrl is not None:
                                ctrl.value = _old_inv
                                with contextlib.suppress(Exception):
                                    ctrl.update()
                            self._show_pack_snack(f"Інвойс {_new_inv!r} вже існує")
                            return

                    try:
                        logger.info(
                            "pack_stock inline_commit: oid=%s field=%s payload=%s",
                            roid[:24],
                            field,
                            payload,
                        )
                        self._apply_local_supply_order_patch(roid, payload)
                        # Format numeric fields with ' thousands separator immediately.
                        if (
                            field in _PACKAGING_SUPPLY_NUMERIC_PAYLOAD_FIELDS
                            and ctrl is not None
                        ):
                            fv = payload.get(field)
                            if fv is not None:
                                try:
                                    formatted = (
                                        _fmt_money(fv)
                                        if field in ("cost_amount", "paid_amount")
                                        else _fmt_qty(fv)
                                    )
                                    ctrl.value = formatted
                                    with contextlib.suppress(Exception):
                                        ctrl.update()
                                except Exception:
                                    pass
                        # Тільки структурні зміни (qty, вартість) потребують rebuild залишків.
                        # paid_amount змінює баланс — потребує перебудови залишків.
                        if field in (
                            "qty_ordered",
                            "cost_amount",
                            "paid_amount",
                            "shipped_goods_supplier_qty",
                        ):
                            # Спробуємо in-place: перебудувати тільки 1 контекст, не весь tree.
                            _ctx_id = self._pack_order_ctx_map.get(roid)
                            if not (
                                _ctx_id and self._refresh_pack_ctx_inplace(_ctx_id)
                            ):
                                self._schedule_debounced_reload(0.1)
                        self._persist_supply_order_patch_async(roid, payload)
                    except Exception:
                        logger.exception(
                            "pack_stock inline_commit: виняток oid=%s field=%s payload=%s",
                            roid[:24],
                            field,
                            payload,
                        )

                return _inner

            def _supply_blur_restore_if_empty_then_commit(
                snap_display: str, commit_inner, *, revert_if_empty: bool
            ):
                """Supply: редагуємо поточне значення; якщо числове поле лишили порожнім — лише відновлюємо відображення без запису в БД."""

                def _on_blur(e):
                    c = e.control
                    raw = str(c.value or "").strip()
                    if revert_if_empty and raw == "":
                        c.value = snap_display
                        with contextlib.suppress(Exception):
                            c.update()
                        return
                    commit_inner(e)

                return _on_blur

            seq_i = _safe_int_seq(row.get("seq_no"), 0)
            if is_goods:
                # Goods-рядки отримують seq_no найближчого попереднього supply-замовлення.
                _eff_seq = _goods_supply_seq.get(oid, 0) or seq_i
                seq_disp = str(_eff_seq) if _eff_seq > 0 else "—"
            else:
                seq_disp = str(seq_i) if seq_i > 0 else "0"

            date_iso = _date_to_iso(row.get("order_date"))
            if is_goods:
                date_tf = self._tf(
                    date_iso,
                    wmap["d"],
                    self.ROW_H,
                    read_only=self.subordinate_ro,
                    on_blur=(
                        _commit_factory("order_date", row_order_id=oid)
                        if not self.subordinate_ro
                        else None
                    ),
                )
                date_cell = ft.Container(
                    width=wmap["d"],
                    height=self.ROW_H,
                    bgcolor=bg_goods,
                    border=ft.border.all(1, self.BDR),
                    padding=0,
                    content=date_tf,
                )
            else:
                # supply: дата через календар (як у діалозі); зміна → БД + reload → залишки
                date_lbl = ft.Text(
                    str(date_iso or "")[:10],
                    size=self.TXT,
                    text_align=ft.TextAlign.CENTER,
                    color=_TEXT,
                )
                date_cell = ft.Container(
                    width=wmap["d"],
                    height=self.ROW_H,
                    bgcolor=bg_goods,
                    border=ft.border.all(1, self.BDR),
                    padding=0,
                    alignment=ft.Alignment(0, 0),
                    on_click=(
                        None
                        if self.subordinate_ro
                        else lambda _e, iid=oid, d0=date_iso: self._open_supply_order_date_picker(
                            iid, d0
                        )
                    ),
                    content=ft.Container(
                        expand=True,
                        alignment=ft.Alignment(0, 0),
                        content=date_lbl,
                    ),
                )

            if is_goods:
                _qty_commit_inner = _commit_factory(
                    "qty_ordered",
                    lambda x: float(_safe_float(x, 0) or 0),
                    row_order_id=oid,
                )
                _qty_commit = _qty_commit_inner if not self.subordinate_ro else None
                qty_tf = self._tf(
                    _fmt_qty(qty),
                    wmap["qty"],
                    self.ROW_H,
                    read_only=self.subordinate_ro,
                    numeric=True,
                    keyboard_type=ft.KeyboardType.TEXT,
                    always_call_on_tap=bool(not self.subordinate_ro),
                    on_blur=_qty_commit,
                    on_submit=_qty_commit,
                )
            else:
                _snap_qty = _fmt_qty(qty)

                def _supply_qty_commit(e, _roid=str(oid), _pp=float(pack_unit_px)):
                    """Supply: кількість → БД; вартість = кількість × ціна пакування (якщо ціна задана). Іншу вартість можна змінити окремо в колонці «Вартість»."""
                    if not _roid or self.subordinate_ro:
                        return
                    c = e.control
                    raw = (
                        str(c.value if c is not None else "")
                        .strip()
                        .replace(" ", "")
                        .replace(",", ".")
                    )
                    if not raw:
                        return
                    try:
                        new_q = float(_safe_float(raw, 0) or 0)
                    except Exception as exc:
                        logger.warning(
                            "pack_stock supply_qty_commit: parse oid=%s raw=%r %s",
                            _roid[:24],
                            raw,
                            exc,
                        )
                        return
                    payload: dict[str, Any] = {"qty_ordered": new_q}
                    if float(_pp or 0) > 0:
                        payload["cost_amount"] = float(new_q) * float(_pp)
                    try:
                        logger.info(
                            "pack_stock supply_qty_commit: oid=%s payload=%s",
                            _roid[:24],
                            payload,
                        )
                        self._apply_local_supply_order_patch(_roid, payload)
                        self._persist_supply_order_patch_async(_roid, payload)
                        _c = self._pack_order_ctx_map.get(_roid)
                        if not (_c and self._refresh_pack_ctx_inplace(_c)):
                            self._schedule_debounced_reload(0.5)
                    except Exception:
                        logger.exception(
                            "pack_stock supply_qty_commit oid=%s", _roid[:24]
                        )

                _qb_qty = _supply_blur_restore_if_empty_then_commit(
                    _snap_qty, _supply_qty_commit, revert_if_empty=True
                )
                qty_tf = self._tf(
                    _snap_qty,
                    wmap["qty"],
                    self.ROW_H,
                    read_only=self.subordinate_ro,
                    numeric=True,
                    keyboard_type=ft.KeyboardType.TEXT,
                    always_call_on_tap=bool(not self.subordinate_ro),
                    on_blur=_qb_qty,
                    on_submit=_qb_qty,
                )

            if is_goods:
                st_outer = self._cell(
                    "—", wmap["st"], self.ROW_H, bg=bg_goods, color=_TEXT
                )
            else:
                st_val = str(row.get("status") or "production").strip() or "production"
                if st_val not in ("production", "ready_for_shipment"):
                    st_val = "production"
                stat_labels_pick = {
                    "production": "Виробництво",
                    "ready_for_shipment": "Готовий",
                }
                disp_st = stat_labels_pick.get(st_val, "Виробництво")
                wst = max(int(wmap["st"] or 0), 40)
                st_lbl_txt = _wrap_text_for_width(disp_st, wst)

                def _status_label_core() -> ft.Text:
                    return ft.Text(
                        st_lbl_txt,
                        size=self.TXT,
                        color=_TEXT,
                        text_align=ft.TextAlign.CENTER,
                        no_wrap=False,
                        max_lines=4,
                        overflow=ft.TextOverflow.VISIBLE,
                    )

                if self.subordinate_ro:
                    st_outer = ft.Container(
                        width=wst,
                        height=self.ROW_H,
                        bgcolor=bg_goods,
                        border=ft.border.all(1, self.BDR),
                        padding=0,
                        alignment=ft.Alignment(0, 0),
                        clip_behavior=ft.ClipBehavior.HARD_EDGE,
                        content=ft.Container(
                            width=wst,
                            height=self.ROW_H,
                            alignment=ft.Alignment(0, 0),
                            padding=ft.padding.symmetric(horizontal=2, vertical=0),
                            content=_status_label_core(),
                        ),
                    )
                else:
                    # Rebuilding this PopupMenuButton on every background
                    # refresh (ref-data/pulse checks, ~every 20s) recreates
                    # its Flutter overlay and force-closes it if the user
                    # has it open — reuse the same control across renders
                    # unless something it actually displays changed.
                    if not isinstance(
                        getattr(self, "_pack_status_popup_cache", None), dict
                    ):
                        self._pack_status_popup_cache = {}
                    _st_sig = (st_val, wst, bg_goods)
                    _st_cached = self._pack_status_popup_cache.get(str(oid))
                    if _st_cached is not None and _st_cached[0] == _st_sig:
                        st_outer = _st_cached[1]
                    else:
                        st_menu_opts = [
                            ft.PopupMenuItem(
                                content=ft.Text(
                                    "Виробництво",
                                    size=self.TXT,
                                    no_wrap=False,
                                    max_lines=3,
                                    overflow=ft.TextOverflow.VISIBLE,
                                ),
                                on_click=(
                                    lambda _e, iid=oid: self._on_status_change(
                                        iid, "production"
                                    )
                                ),
                            ),
                            ft.PopupMenuItem(
                                content=ft.Text(
                                    "Готовий",
                                    size=self.TXT,
                                    no_wrap=False,
                                    max_lines=3,
                                    overflow=ft.TextOverflow.VISIBLE,
                                ),
                                on_click=(
                                    lambda _e, iid=oid: self._on_status_change(
                                        iid, "ready_for_shipment"
                                    )
                                ),
                            ),
                        ]
                        st_outer = ft.Container(
                            width=wst,
                            height=self.ROW_H,
                            bgcolor=bg_goods,
                            border=ft.border.all(1, self.BDR),
                            padding=0,
                            clip_behavior=ft.ClipBehavior.HARD_EDGE,
                            content=ft.PopupMenuButton(
                                icon=None,
                                tooltip="Обрати статус",
                                items=st_menu_opts,
                                menu_position=ft.PopupMenuPosition.UNDER,
                                content=ft.Container(
                                    width=wst,
                                    height=self.ROW_H,
                                    bgcolor=bg_goods,
                                    alignment=ft.Alignment(0, 0),
                                    padding=ft.padding.symmetric(horizontal=2, vertical=0),
                                    content=_status_label_core(),
                                ),
                            ),
                        )
                        self._pack_status_popup_cache[str(oid)] = (_st_sig, st_outer)

            cost_val = row.get("cost_amount")
            if not is_goods and cost_val is None and qty and pack_price:
                cost_val = qty * pack_price

            if is_goods:
                cost_tf = self._tf(
                    _fmt_money(cost_val),
                    wmap["cost"],
                    self.ROW_H,
                    read_only=True,
                    numeric=True,
                    on_blur=None,
                )
            else:
                _snap_cost = _fmt_money(cost_val)
                _cost_ci = _commit_factory(
                    "cost_amount",
                    lambda x: _safe_float(x, None),
                    row_order_id=oid,
                )
                _cb_co = _supply_blur_restore_if_empty_then_commit(
                    _snap_cost, _cost_ci, revert_if_empty=False
                )
                cost_tf = self._tf(
                    _snap_cost,
                    wmap["cost"],
                    self.ROW_H,
                    read_only=self.subordinate_ro,
                    numeric=True,
                    always_call_on_tap=bool(not self.subordinate_ro),
                    on_blur=_cb_co,
                    on_submit=_cb_co,
                )

            extra_usd: list = []
            if show_usd_cost:
                if is_goods:
                    extra_usd.append(
                        self._cell("—", wmap["cost_usd"], self.ROW_H, bg=bg_goods)
                    )
                else:
                    camt = _safe_float(cost_val, None)
                    if camt is None:
                        extra_usd.append(
                            self._cell("—", wmap["cost_usd"], self.ROW_H, bg=bg_goods)
                        )
                    else:
                        usd_amt = self._packaging_amount_to_usd(float(camt), ccy)
                        extra_usd.append(
                            self._cell(
                                _fmt_money(usd_amt) if usd_amt is not None else "—",
                                wmap["cost_usd"],
                                self.ROW_H,
                                bg=bg_goods,
                            )
                        )

            if is_goods:
                paid_tf = self._tf(
                    "—",
                    wmap["paid"],
                    self.ROW_H,
                    read_only=True,
                    numeric=True,
                    on_blur=None,
                )
                ship_tf = self._tf(
                    "—",
                    wmap["ship"],
                    self.ROW_H,
                    read_only=True,
                    numeric=True,
                    on_blur=None,
                )
            else:
                _snap_paid = _fmt_money(row.get("paid_amount"))
                _paid_ci = _commit_factory(
                    "paid_amount",
                    lambda x: float(_safe_float(x, 0) or 0),
                    row_order_id=oid,
                )
                _pb_paid = _supply_blur_restore_if_empty_then_commit(
                    _snap_paid, _paid_ci, revert_if_empty=True
                )
                paid_tf = self._tf(
                    _snap_paid,
                    wmap["paid"],
                    self.ROW_H,
                    read_only=self.subordinate_ro,
                    numeric=True,
                    always_call_on_tap=bool(not self.subordinate_ro),
                    on_blur=_pb_paid,
                    on_submit=_pb_paid,
                )
                _snap_ship = _fmt_qty(shipped)
                _ship_ci = _commit_factory(
                    "shipped_goods_supplier_qty",
                    lambda x: float(_safe_float(x, 0) or 0),
                    row_order_id=oid,
                )
                _sb_sh = _supply_blur_restore_if_empty_then_commit(
                    _snap_ship, _ship_ci, revert_if_empty=True
                )
                ship_tf = self._tf(
                    _snap_ship,
                    wmap["ship"],
                    self.ROW_H,
                    read_only=self.subordinate_ro,
                    numeric=True,
                    always_call_on_tap=bool(not self.subordinate_ro),
                    on_blur=_sb_sh,
                    on_submit=_sb_sh,
                )

            bal1 = self._cell(bal_pack_disp, wmap["bal_pack"], self.ROW_H, bg=bg_goods)
            bal2 = self._cell(
                "—" if bg_end is None else _fmt_qty(bg_end),
                wmap["bal_goods"],
                self.ROW_H,
                bg=bg_goods,
            )

            if not is_goods:
                _snap_zp = str(row.get("zp_1c") or "")
                _zp_ci = _commit_factory(
                    "zp_1c", lambda x: str(x or ""), row_order_id=oid
                )
                _zb_zp = _supply_blur_restore_if_empty_then_commit(
                    _snap_zp, _zp_ci, revert_if_empty=False
                )
                zp_tf = self._tf(
                    _snap_zp,
                    wmap["zp"],
                    self.ROW_H,
                    read_only=self.subordinate_ro,
                    always_call_on_tap=bool(not self.subordinate_ro),
                    on_blur=_zb_zp,
                    on_submit=_zb_zp,
                )
                _snap_inv = str(row.get("invoice_no") or "")
                _inv_ci = _commit_factory(
                    "invoice_no", lambda x: str(x or ""), row_order_id=oid
                )
                _ib_inv = _supply_blur_restore_if_empty_then_commit(
                    _snap_inv, _inv_ci, revert_if_empty=False
                )
                inv_tf = self._tf(
                    _snap_inv,
                    wmap["inv"],
                    self.ROW_H,
                    read_only=self.subordinate_ro,
                    multiline=True,
                    always_call_on_tap=bool(not self.subordinate_ro),
                    on_blur=_ib_inv,
                    on_submit=_ib_inv,
                )
            else:

                def _zp_blur_goods(e, _roid=str(oid)):
                    try:
                        pl = {"zp_1c": str(e.control.value or "").strip()[:60]}
                        logger.info(
                            "pack_stock inline_commit goods: oid=%s payload=%s",
                            _roid[:24],
                            pl,
                        )
                        self._apply_local_supply_order_patch(_roid, pl)
                        self._persist_supply_order_patch_async(_roid, pl)
                        _c = self._pack_order_ctx_map.get(_roid)
                        if not (_c and self._refresh_pack_ctx_inplace(_c)):
                            self._schedule_debounced_reload()
                    except Exception:
                        logger.exception(
                            "pack_stock inline_commit goods zp oid=%s", _roid[:24]
                        )

                def _inv_blur_goods(e, _roid=str(oid)):
                    try:
                        _new_inv = str(e.control.value or "").strip()[:60]
                        if _new_inv and self._is_pack_invoice_dup(_roid, _new_inv):
                            _old_inv = next(
                                (
                                    str(o.get("invoice_no") or "")
                                    for _ctx in self._local_orders_cache.values()
                                    for o in _ctx
                                    if isinstance(o, dict)
                                    and str(o.get("id") or "") == _roid
                                ),
                                "",
                            )
                            if e.control is not None:
                                e.control.value = _old_inv
                                with contextlib.suppress(Exception):
                                    e.control.update()
                            self._show_pack_snack(f"Інвойс {_new_inv!r} вже існує")
                            return
                        pl = {"invoice_no": _new_inv}
                        logger.info(
                            "pack_stock inline_commit goods: oid=%s payload=%s",
                            _roid[:24],
                            pl,
                        )
                        self._apply_local_supply_order_patch(_roid, pl)
                        self._persist_supply_order_patch_async(_roid, pl)
                        _c = self._pack_order_ctx_map.get(_roid)
                        if not (_c and self._refresh_pack_ctx_inplace(_c)):
                            self._schedule_debounced_reload()
                    except Exception:
                        logger.exception(
                            "pack_stock inline_commit goods inv oid=%s", _roid[:24]
                        )

                zp_tf = self._tf(
                    str(row.get("zp_1c") or ""),
                    wmap["zp"],
                    self.ROW_H,
                    read_only=self.subordinate_ro,
                    on_blur=_zp_blur_goods,
                )
                inv_tf = self._tf(
                    str(row.get("invoice_no") or ""),
                    wmap["inv"],
                    self.ROW_H,
                    read_only=self.subordinate_ro,
                    multiline=True,
                    on_blur=_inv_blur_goods,
                )

            # multiline=False: у висоті ROW_H багаторядковий поле давало глюки верстки (сірий блок).
            _snap_cmt = str(row.get("comment") or "")
            _cmt_ci = _commit_factory(
                "comment",
                lambda x: str(x or "").strip()[:400],
                row_order_id=oid,
            )
            if is_goods:
                _cb_cmt_g = None if self.subordinate_ro else _cmt_ci
                cmt_tf = self._tf(
                    _snap_cmt,
                    wmap["cmt"],
                    self.ROW_H,
                    read_only=self.subordinate_ro,
                    multiline=True,
                    always_call_on_tap=bool(not self.subordinate_ro),
                    on_blur=_cb_cmt_g,
                    on_submit=_cb_cmt_g,
                )
            else:
                _cb_cmt_s = (
                    None
                    if self.subordinate_ro
                    else _supply_blur_restore_if_empty_then_commit(
                        _snap_cmt, _cmt_ci, revert_if_empty=False
                    )
                )
                cmt_tf = self._tf(
                    _snap_cmt,
                    wmap["cmt"],
                    self.ROW_H,
                    read_only=self.subordinate_ro,
                    multiline=True,
                    always_call_on_tap=bool(not self.subordinate_ro),
                    on_blur=_cb_cmt_s,
                    on_submit=_cb_cmt_s,
                )

            if is_goods:
                del_cell = ft.Container(
                    width=wmap["del"],
                    height=self.ROW_H,
                    bgcolor=bg_goods,
                    border=ft.border.all(1, self.BDR),
                )
            else:
                del_btn = ft.IconButton(
                    icon=ft.Icons.DELETE_OUTLINE,
                    icon_size=18,
                    tooltip="Видалити рядок",
                    disabled=self.subordinate_ro,
                    on_click=lambda _e, iid=oid: self._confirm_delete_row(iid),
                )
                del_cell = ft.Container(
                    width=wmap["del"],
                    height=self.ROW_H,
                    bgcolor=bg_goods,
                    border=ft.border.all(1, self.BDR),
                    alignment=ft.Alignment(0, 0),
                    content=del_btn,
                )

            # Build cells as a key→control dict, then filter to visible columns in spec order.
            # This prevents crashes when a column is hidden (wmap[key]=0) via column toggle.
            _row_ck: dict = {}
            if wmap["seq"]:
                _row_ck["seq"] = self._cell(
                    seq_disp, wmap["seq"], self.ROW_H, bg=bg_goods
                )
            if wmap["d"]:
                _row_ck["d"] = date_cell
            if wmap["qty"]:
                _row_ck["qty"] = ft.Container(
                    width=wmap["qty"],
                    height=self.ROW_H,
                    bgcolor=bg_goods,
                    border=ft.border.all(1, self.BDR),
                    padding=0,
                    content=qty_tf,
                )
            if wmap["st"]:
                _row_ck["st"] = st_outer
            if wmap["cost"]:
                _row_ck["cost"] = ft.Container(
                    width=wmap["cost"],
                    height=self.ROW_H,
                    bgcolor=bg_goods,
                    border=ft.border.all(1, self.BDR),
                    padding=0,
                    content=cost_tf,
                )
            if wmap["cost_usd"] and extra_usd:
                _row_ck["cost_usd"] = extra_usd[0]
            if wmap["paid"]:
                _row_ck["paid"] = ft.Container(
                    width=wmap["paid"],
                    height=self.ROW_H,
                    bgcolor=bg_goods,
                    border=ft.border.all(1, self.BDR),
                    padding=0,
                    content=paid_tf,
                )
            if wmap["ship"]:
                _row_ck["ship"] = ft.Container(
                    width=wmap["ship"],
                    height=self.ROW_H,
                    bgcolor=bg_goods,
                    border=ft.border.all(1, self.BDR),
                    padding=0,
                    content=ship_tf,
                )
            if wmap["bal_pack"]:
                _row_ck["bal_pack"] = bal1
            if wmap["bal_goods"]:
                _row_ck["bal_goods"] = bal2
            if wmap["zp"]:
                _row_ck["zp"] = ft.Container(
                    width=wmap["zp"],
                    height=self.ROW_H,
                    bgcolor=bg_goods,
                    border=ft.border.all(1, self.BDR),
                    padding=0,
                    content=zp_tf,
                )
            if wmap["inv"]:
                _row_ck["inv"] = ft.Container(
                    width=wmap["inv"],
                    height=self.ROW_H,
                    bgcolor=bg_goods,
                    border=ft.border.all(1, self.BDR),
                    padding=0,
                    content=inv_tf,
                )
            if wmap["cmt"]:
                _row_ck["cmt"] = ft.Container(
                    width=wmap["cmt"],
                    height=self.ROW_H,
                    bgcolor=bg_goods,
                    border=ft.border.all(1, self.BDR),
                    padding=0,
                    content=cmt_tf,
                )
            if wmap["del"]:
                _row_ck["del"] = del_cell
            cells = [_row_ck[c["key"]] for c in specs if c["key"] in _row_ck]

            _append_row(ft.Row(cells, spacing=0, intrinsic_height=True))

        # ── Assemble rows_col: visible rows + optional history toggle + opening balance ──
        rows_col.controls.extend(_vis_rows)
        if _hid_rows:
            _n_hid = len(_hid_rows)
            _hist_expanded = context_id in self._expanded_pack_history_ctxs
            _hist_col = ft.Column(
                _hid_rows, spacing=0, tight=True, visible=_hist_expanded
            )
            _hist_lbl = ft.Text(
                ("−" if _hist_expanded else "+") + f"  {_n_hid} старших рядків",
                size=11,
                weight=ft.FontWeight.W_600,
                color=_ACCENT,
            )

            def _toggle_hist(
                _e,
                _cid=context_id,
                _col=_hist_col,
                _lbl=_hist_lbl,
                _n=_n_hid,
            ):
                if _cid in self._expanded_pack_history_ctxs:
                    self._expanded_pack_history_ctxs.discard(_cid)
                else:
                    self._expanded_pack_history_ctxs.add(_cid)
                _col.visible = _cid in self._expanded_pack_history_ctxs
                _s = "−" if _col.visible else "+"
                _lbl.value = f"{_s}  {_n} старших рядків"
                try:
                    _lbl.update()
                    _col.update()
                except Exception:
                    pass

            _toggle_ctr = ft.Container(
                bgcolor=_SOFT_I,
                border=ft.border.all(1, _BORDER),
                border_radius=4,
                padding=ft.padding.symmetric(horizontal=10, vertical=5),
                margin=ft.margin.symmetric(vertical=1),
                on_click=_toggle_hist,
                ink=True,
                content=_hist_lbl,
            )
            rows_col.controls.append(_toggle_ctr)
            rows_col.controls.append(_hist_col)
        rows_col.controls.extend(_open_rows)

        hdr = ft.Container(
            bgcolor=self.BG_HDR,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.Row(
                header_cells,
                spacing=0,
                intrinsic_height=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        w_final = sum(int(c.get("width") or 0) for c in specs)
        tbl_core = ft.Column(
            [hdr, rows_col], spacing=0, tight=True, width=max(1, int(w_final))
        )

        self._log_pack(
            "orders_table DONE ctx=%s rows_col_children=%s hdr_cells=%s w_final=%s (без вкладеного гориз. scroll)",
            str(context_id)[:16],
            len(rows_col.controls),
            len(header_cells),
            w_final,
        )
        table_shell = ft.Container(
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            bgcolor=_PANEL,
            border_radius=8,
            border=ft.border.all(1, "#E5E7EB"),
            content=tbl_core,
        )
        content_row = ft.Row([table_shell], spacing=0, scroll=ft.ScrollMode.HIDDEN)
        self._cv_body_scroll = content_row

        # Overlay h-scroll buttons — visible only when table is wider than viewport.
        _pk_needs = w_final > base_vw
        _pk_step = max(300, int(w_final) // 3)
        _pk_page = self._page()

        def _do_hscroll_pk(delta):
            async def _run():
                with contextlib.suppress(Exception):
                    await content_row.scroll_to(delta=delta, duration=200)

            with contextlib.suppress(Exception):
                if _pk_page:
                    _pk_page.run_task(_run)

        _pk_sh = ft.BoxShadow(
            spread_radius=1,
            blur_radius=8,
            color=ft.Colors.with_opacity(0.28, ft.Colors.BLACK),
            offset=ft.Offset(0, 2),
        )
        _pk_bs = ft.ButtonStyle(padding=ft.padding.all(4))
        _pk_left = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_LEFT,
                icon_size=20,
                icon_color=self.UI_ACCENT_BLUE,
                on_click=lambda _: _do_hscroll_pk(-_pk_step),
                style=_pk_bs,
            ),
            left=4,
            top=60,
            bgcolor=_PANEL,
            border_radius=20,
            width=36,
            height=36,
            shadow=_pk_sh,
            opacity=0.9,
            visible=_pk_needs,
        )
        _pk_right = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_RIGHT,
                icon_size=20,
                icon_color=self.UI_ACCENT_BLUE,
                on_click=lambda _: _do_hscroll_pk(_pk_step),
                style=_pk_bs,
            ),
            right=4,
            top=60,
            bgcolor=_PANEL,
            border_radius=20,
            width=36,
            height=36,
            shadow=_pk_sh,
            opacity=0.9,
            visible=_pk_needs,
        )

        return ft.Container(
            content=ft.Stack(
                [ft.Column([content_row], spacing=0, tight=True), _pk_left, _pk_right],
            ),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )

    def _on_status_change(self, order_id: str, value: str):
        oid = str(order_id or "").strip()
        if oid.startswith("pk_open:"):
            return
        try:
            self._apply_local_supply_order_patch(oid, {"status": value})
            self._queue_pack_order_patch(oid, {"status": value})
            ctx_id = self._pack_order_ctx_map.get(oid)
            if not (ctx_id and self._refresh_pack_ctx_inplace(ctx_id)):
                self._schedule_debounced_reload()
        except Exception:
            pass

    def _confirm_delete_row(self, order_id: str):
        page = self._page()
        if not page:
            return
        oid0 = str(order_id or "").strip()
        if oid0.startswith("pk_open:"):
            return

        def _do_delete(_):
            self.db.delete_packaging_supply_order(oid0)
            # Clean any pending outbox patch for this order so flush doesn't
            # try to update the now-deleted row and stay in "Не збережено" state.
            outbox_key = f"order:{oid0}"
            if outbox_key in self._ops_outbox:
                self._ops_outbox.pop(outbox_key, None)
                with contextlib.suppress(Exception):
                    self._save_pack_outbox()
            self._close_dlg(dlg)
            self.reload()

        dlg = ft.AlertDialog(
            title=self._text_wrap(
                "Видалити запис?", size=18, weight=ft.FontWeight.W_600
            ),
            content=self._text_wrap("Це дію неможливо скасувати.", size=14),
            actions=[
                ft.TextButton("Ні", on_click=lambda _: self._close_dlg(dlg)),
                ft.TextButton("Так", on_click=_do_delete),
            ],
        )
        page.show_dialog(dlg)

    def _close_dlg(self, dlg: ft.AlertDialog):
        page = self._page()
        if not page:
            return
        try:
            page.pop_dialog()
        except Exception:
            with contextlib.suppress(Exception):
                page.close_dialog(dlg)

    async def _open_supply_order_dialog(self, ctx: dict, pack_row: dict):
        """Builds the (DB-heavy) supply-order dialog off the UI thread, then
        shows it — see ProductsView.show_product_details_dialog for the
        same fix and why it matters on Android."""
        page = self._page()
        if not page or self.subordinate_ro:
            return
        dlg = await asyncio.to_thread(self._build_supply_order_dialog, ctx, pack_row)
        if dlg is not None:
            page.show_dialog(dlg)

    def _build_supply_order_dialog(self, ctx: dict, pack_row: dict):
        page = self._page()
        rid = str(ctx.get("region_id") or "").strip() or (
            getattr(self, "_region_id_live", "") or ""
        )
        ctx_id = str(ctx.get("id") or "").strip()
        seq = self.db.packaging_supply_next_seq(ctx_id)
        reg_name = (
            self._region_label(rid)
            if rid
            else str(self.user_data.get("active_region_label") or "")
        )

        pack_name = str(pack_row.get("name") or "").strip()
        ps_name = ""
        try:
            s = (
                pack_row.get("suppliers")
                if isinstance(pack_row.get("suppliers"), dict)
                else {}
            )
            ps_name = str(s.get("name") or "").strip()
        except Exception:
            ps_name = ""

        suppliers = (
            self.db.get_suppliers(self.company_id, rid, manager_id=self.manager_id)
            or []
        )
        gid = str(ctx.get("goods_supplier_id") or "").strip()
        goods_name = self._supplier_name(gid, suppliers) if gid else "—"

        linked = ctx.get("linked_product_ids")
        if isinstance(linked, str):
            try:
                linked = json.loads(linked)
            except Exception:
                linked = []
        linked = linked if isinstance(linked, list) else []
        products = (
            self.db.get_products(self.company_id, rid, manager_id=self.manager_id) or []
        )
        pmap = {
            str(p.get("id") or "").strip(): p for p in products if isinstance(p, dict)
        }
        _supplier_by_id = {
            str((s or {}).get("id") or "").strip(): s
            for s in suppliers
            if isinstance(s, dict) and (s.get("id") or "")
        }
        if not hasattr(self, "_node_path_cache"):
            self._node_path_cache = {}
        _node_path_of = product_naming.make_node_path_resolver(
            self.db, self._node_path_cache
        )
        _pn_suffixes = product_naming.compute_name_suffixes(
            products,
            category_of=lambda p: product_naming.resolve_category_label(
                p, node_path_of=_node_path_of
            ),
            supplier_of=lambda p: product_naming.resolve_supplier_label(
                p, supplier_by_id=_supplier_by_id
            ),
        )
        pname_list = [
            str((pmap.get(str(x)) or {}).get("name") or x) + _pn_suffixes.get(str(x), "")
            for x in linked
        ]

        try:
            win_w = int(getattr(page, "width", 0) or 0)
            if win_w <= 0:
                win_w = int(getattr(getattr(page, "window", None), "width", 0) or 0)
        except Exception:
            win_w = 0
        field_w = min(max(300, (win_w or 900) - 160), 580)

        def _field_style(*, multiline: bool = False, max_lines: int = 6) -> dict:
            out = {
                "width": field_w,
                "dense": False,
                "text_size": 14,
                "border_radius": 8,
                "filled": True,
                "bgcolor": _ALT2,
                "border_color": _HDR,
                "focused_border_color": _SKY,
                "content_padding": ft.Padding.symmetric(horizontal=14, vertical=12),
                "text_align": ft.TextAlign.CENTER,
                "text_vertical_align": ft.VerticalAlignment.CENTER,
            }
            if multiline:
                out["multiline"] = True
                out["min_lines"] = 1
                out["max_lines"] = max_lines
                out["text_vertical_align"] = ft.VerticalAlignment.CENTER
            return out

        def _lbl(txt: str) -> ft.Text:
            return self._text_wrap(
                txt, size=12, weight=ft.FontWeight.W_600, color=_TEXT
            )

        def _block(caption: str, ctrl: ft.Control) -> ft.Column:
            return ft.Column(
                [_lbl(caption), ctrl],
                spacing=6,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            )

        def _kv(k: str, v: str) -> ft.Column:
            return ft.Column(
                [
                    self._text_wrap(k, size=11, color=_TEXT),
                    self._text_wrap(
                        v, size=13, weight=ft.FontWeight.W_600, color=_TEXT
                    ),
                ],
                spacing=2,
                tight=True,
            )

        supply_status = {"value": "production"}
        stat_labels = {
            "production": "Виробництво",
            "ready_for_shipment": "Готовий",
        }

        date_f = ft.TextField(
            value=date.today().isoformat(),
            hint_text="РРРР-ММ-ДД",
            read_only=True,
            keyboard_type=ft.KeyboardType.TEXT,
            **_field_style(),
        )
        calendar_btn = ft.IconButton(
            icon=ft.Icons.CALENDAR_MONTH,
            icon_size=22,
            tooltip="Обрати дату",
        )
        date_row_ctrl = ft.Row(
            [
                ft.Container(content=date_f, expand=True),
                calendar_btn,
            ],
            spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        date_block = ft.Column(
            [
                _lbl("Дата замовлення"),
                date_row_ctrl,
                self._text_wrap(
                    "Натисніть поле дати або іконку — календар відкриється поверх цієї форми (модалка не закривається).",
                    size=11,
                    color=_TEXT,
                ),
            ],
            spacing=6,
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        qty_f = ft.TextField(
            keyboard_type=ft.KeyboardType.TEXT,
            **_field_style(multiline=True, max_lines=4),
        )
        cost_f = ft.TextField(
            keyboard_type=ft.KeyboardType.TEXT,
            **_field_style(multiline=True, max_lines=4),
        )
        paid_f = ft.TextField(
            keyboard_type=ft.KeyboardType.TEXT,
            **_field_style(multiline=True, max_lines=4),
        )
        ship_f = ft.TextField(
            keyboard_type=ft.KeyboardType.TEXT,
            **_field_style(multiline=True, max_lines=4),
        )
        zp_f = ft.TextField(max_length=60, **_field_style(multiline=True, max_lines=4))
        inv_f = ft.TextField(max_length=60, **_field_style(multiline=True, max_lines=4))
        cmt_f = ft.TextField(
            multiline=True,
            min_lines=2,
            max_lines=5,
            keyboard_type=ft.KeyboardType.TEXT,
            text_align=ft.TextAlign.START,
            text_vertical_align=ft.VerticalAlignment.START,
            **{
                k: v
                for k, v in _field_style().items()
                if k not in ("text_align", "text_vertical_align")
            },
        )

        stat_txt = ft.Text(
            stat_labels[supply_status["value"]],
            size=14,
            color=_TEXT,
            text_align=ft.TextAlign.CENTER,
            width=float(field_w) - 72,
            no_wrap=False,
            max_lines=8,
            overflow=ft.TextOverflow.VISIBLE,
        )

        def _set_supply_status(key: str):
            supply_status["value"] = key
            stat_txt.value = stat_labels.get(key, key)
            try:
                stat_txt.update()
            except Exception:
                pass

        stat_pick = ft.Container(
            width=float(field_w),
            bgcolor=_ALT2,
            border_radius=8,
            border=ft.border.all(1, _HDR),
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            padding=ft.padding.all(0),
            content=ft.PopupMenuButton(
                icon=None,
                tooltip="Обрати статус",
                menu_position=ft.PopupMenuPosition.UNDER,
                padding=ft.Padding.symmetric(horizontal=14, vertical=12),
                items=[
                    ft.PopupMenuItem(
                        content=ft.Text(stat_labels["production"]),
                        on_click=lambda _e: _set_supply_status("production"),
                    ),
                    ft.PopupMenuItem(
                        content=ft.Text(stat_labels["ready_for_shipment"]),
                        on_click=lambda _e: _set_supply_status("ready_for_shipment"),
                    ),
                ],
                content=ft.Container(
                    width=float(field_w) - 28,
                    alignment=ft.Alignment(0, 0),
                    content=stat_txt,
                ),
            ),
        )

        def _recalc_cost(_=None):
            try:
                q = float(qty_f.value or 0)
            except Exception:
                q = 0.0
            px = float(_safe_float(pack_row.get("price"), 0.0) or 0)
            cost_f.value = _fmt_money(q * px)

        qty_f.on_change = lambda _: _recalc_cost()
        _recalc_cost()

        products_txt = "; ".join(pname_list) if pname_list else "—"

        ctx_card = ft.Container(
            padding=14,
            bgcolor=_ALT,
            border_radius=10,
            border=ft.border.all(1, _BORDER),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.PLACE_OUTLINED, size=18, color=_TEXT),
                            self._text_wrap(
                                "Куди записується рядок",
                                size=12,
                                weight=ft.FontWeight.W_700,
                                color=_TEXT,
                                expand=True,
                            ),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                    _kv("Регіон", reg_name or "—"),
                    _kv("Постачальник пакування", ps_name or "—"),
                    _kv("Пакування (SKU)", pack_name or "—"),
                    _kv("Постачальник товару", goods_name),
                    ft.Column(
                        [
                            self._text_wrap("Товари", size=11, color=_TEXT),
                            self._text_wrap(
                                products_txt,
                                size=13,
                                weight=ft.FontWeight.W_500,
                                color=_TEXT,
                                max_lines=24,
                            ),
                        ],
                        spacing=2,
                        tight=True,
                    ),
                ],
                spacing=10,
                tight=True,
            ),
        )

        def _section_title(text: str) -> ft.Container:
            return ft.Container(
                padding=ft.padding.only(top=4, bottom=2),
                content=self._text_wrap(
                    text, size=13, weight=ft.FontWeight.W_700, color=_CHIP_T
                ),
            )

        form_col = ft.Column(
            [
                ctx_card,
                _section_title("Параметри замовлення"),
                date_block,
                _block("Кількість, шт.", qty_f),
                _block("Статус", stat_pick),
                _block(
                    "Вартість (перераховується від кількості × ціна пакування; можна змінити)",
                    cost_f,
                ),
                _section_title("Оплата та відвантаження"),
                _block("Сплачено", paid_f),
                _block("Відправлено постачальнику товару, шт.", ship_f),
                _section_title("ЗП обліку та інвойс"),
                _block("ЗП обліку", zp_f),
                _block("Інвойс (за потреби)", inv_f),
                _block("Коментар", cmt_f),
            ],
            spacing=12,
            tight=True,
            scroll=ft.ScrollMode.AUTO,
        )

        scroll_box = ft.Container(
            width=field_w + 40,
            height=min(520, max(360, int((win_w or 700) * 0.55))),
            padding=ft.padding.symmetric(horizontal=16, vertical=12),
            content=form_col,
        )

        def _supply_help(_):
            from views.help_utils import show_modal_help
            _pg = self._page()
            show_modal_help(_pg, "Замовлення пакування", [
                self.t("pack_stock.supply_help.step1", "1. Оберіть дату замовлення."),
                self.t("pack_stock.supply_help.step2", "2. Вкажіть кількість і вартість."),
                self.t("pack_stock.supply_help.step3", "3. Оберіть статус замовлення."),
                self.t("pack_stock.supply_help.step4", "4. Введіть суму оплати, якщо є."),
                self.t("pack_stock.supply_help.step5", "5. Заповніть номер рахунку і коментар (за потреби)."),
                self.t("pack_stock.supply_help.step6", "6. Натисніть «Зберегти»."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Column(
                [
                    ft.Row(
                        [
                            self._text_wrap("Замовлення пакування", size=18, weight=ft.FontWeight.W_700, color=_TEXT, expand=True),
                            ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_supply_help),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
                    ),
                    self._text_wrap(
                        f"Авто-номер: {seq} · {pack_name}", size=12, color=_TEXT
                    ),
                ],
                spacing=4,
                tight=True,
            ),
            content=scroll_box,
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: self._close_dlg(dlg)),
                ft.FilledButton(
                    "Зберегти",
                    on_click=lambda _e: self._save_supply_dialog(
                        dlg,
                        context_id=ctx_id,
                        company_id=self.company_id,
                        seq_no=seq,
                        date_f=date_f,
                        qty_f=qty_f,
                        supply_status=supply_status,
                        cost_f=cost_f,
                        paid_f=paid_f,
                        ship_f=ship_f,
                        zp_f=zp_f,
                        inv_f=inv_f,
                        cmt_f=cmt_f,
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        def _open_supply_material_calendar(_e=None):
            initial_d = date.today()
            try:
                ds = str(date_f.value or "").strip()[:10]
                if ds:
                    initial_d = datetime.fromisoformat(ds).date()
            except Exception:
                initial_d = date.today()

            picker = ft.DatePicker(
                value=initial_d,
                first_date=date(2018, 1, 1),
                last_date=date(2100, 12, 31),
                entry_mode=ft.DatePickerEntryMode.CALENDAR_ONLY,
                date_picker_mode=ft.DatePickerMode.DAY,
                locale=ft.Locale("uk", "UA"),
                cancel_text="Очистити",
                confirm_text="OK",
                modal=True,
                error_format_text="",
                error_invalid_text="",
            )

            def _picked_supply_cal(e):
                raw = _date_picker_event_value(e)
                if raw is None:
                    date_f.value = ""
                else:
                    try:
                        date_f.value = _datepicker_value_to_iso_date(raw)
                    except Exception:
                        date_f.value = str(raw)[:10]
                try:
                    date_f.update()
                    dlg.update()
                except Exception:
                    pass
                try:
                    picker.open = False
                    picker.update()
                except Exception:
                    pass
                with contextlib.suppress(Exception):
                    if picker in (page.overlay or []):
                        page.overlay.remove(picker)
                        page.update()

            picker.on_change = _picked_supply_cal
            page.overlay.append(picker)
            picker.open = True
            with contextlib.suppress(Exception):
                picker.update()

        date_f.on_click = _open_supply_material_calendar
        calendar_btn.on_click = _open_supply_material_calendar

        return dlg

    def _save_supply_dialog(self, dlg, **fields):
        import uuid as _uuid

        ctx_id = str(fields.get("context_id") or "").strip()
        qty = float(_safe_float(fields["qty_f"].value, 0) or 0)
        dt = str(fields["date_f"].value or "").strip()[:10]
        new_record = {
            "company_id": fields.get("company_id"),
            "context_id": ctx_id,
            "seq_no": int(fields.get("seq_no") or 1),
            "order_date": dt or None,
            "qty_ordered": qty,
            "row_kind": "supply",
            "status": str(
                (fields.get("supply_status") or {}).get("value") or "production"
            ),
            "cost_amount": _safe_float(fields["cost_f"].value, None),
            "paid_amount": float(_safe_float(fields["paid_f"].value, 0) or 0),
            "shipped_goods_supplier_qty": float(
                _safe_float(fields["ship_f"].value, 0) or 0
            ),
            "zp_1c": fields["zp_f"].value or "",
            "invoice_no": fields["inv_f"].value or "",
            "comment": fields["cmt_f"].value or "",
        }

        # Close dialog immediately
        self._close_dlg(dlg)
        self._expanded_contexts.add(ctx_id)

        # Add a temp row to local cache immediately so UI updates without DB round-trip
        temp_id = f"tmp_{_uuid.uuid4().hex[:12]}"
        new_record["id"] = temp_id
        if ctx_id not in self._local_orders_cache:
            self._local_orders_cache[ctx_id] = []
        self._local_orders_cache[ctx_id].append(new_record)
        with contextlib.suppress(Exception):
            self._pack_order_ctx_map[temp_id] = ctx_id

        # Queue for DB flush via outbox (45s timer or manual button) — outbox pattern
        _insert_record = {k: v for k, v in new_record.items() if k != "id"}
        _out_key = f"supply_insert:{temp_id}"
        self._ops_outbox[_out_key] = {
            "kind": "supply_insert",
            "ctx_id": ctx_id,
            "temp_id": temp_id,
            "record": _insert_record,
        }
        self._save_pack_outbox()
        self._refresh_pack_sync_status()

        if not self._refresh_pack_ctx_inplace(ctx_id):
            self._render_tree(force_full=True)

    def _open_opening_dialog(self, ctx: dict, pack_row: dict):
        page = self._page()
        if not page or self.subordinate_ro:
            return
        ctx_id = str(ctx.get("id") or "").strip()
        # Load current values so the dialog shows what's already saved
        ex = (
            self._local_opening_cache.get(ctx_id)
            or self.db.get_packaging_supply_opening(ctx_id)
            or {}
        )

        pk = float(ex.get("stock_packaging_supplier_qty") or 0)
        gk = float(ex.get("stock_goods_supplier_qty") or 0)

        try:
            win_w = int(getattr(page, "width", 0) or 0)
            if win_w <= 0:
                win_w = int(getattr(getattr(page, "window", None), "width", 0) or 0)
        except Exception:
            win_w = 0
        field_w = min(max(300, win_w - 120), 440) if win_w else 380

        pack_name = str(pack_row.get("name") or "").strip() or "—"

        def _form_row(caption: str, ctrl: ft.Control) -> ft.Column:
            return ft.Column(
                [
                    self._text_wrap(
                        caption, size=12, weight=ft.FontWeight.W_500, color=_TEXT
                    ),
                    ctrl,
                ],
                spacing=6,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            )

        def _input_qty(val: float) -> ft.TextField:
            return ft.TextField(
                value=_fmt_qty(val),
                width=field_w,
                dense=False,
                text_size=14,
                border_radius=8,
                filled=True,
                bgcolor=_ALT2,
                border_color=_HDR,
                focused_border_color=_SKY,
                content_padding=ft.Padding.symmetric(horizontal=14, vertical=12),
                keyboard_type=ft.KeyboardType.NUMBER,
            )

        f_pk = _input_qty(pk)
        f_gk = _input_qty(gk)
        f_cmt = ft.TextField(
            value=str(ex.get("comment") or ""),
            width=field_w,
            dense=False,
            text_size=14,
            border_radius=8,
            filled=True,
            bgcolor=_ALT2,
            border_color=_HDR,
            focused_border_color=_SKY,
            content_padding=ft.Padding.symmetric(horizontal=14, vertical=12),
            keyboard_type=ft.KeyboardType.TEXT,
            hint_text="Необов'язково",
        )

        def _save(_=None):
            save_kwargs = {
                "context_id": ctx_id,
                "stock_packaging_supplier_qty": max(
                    0.0, float(_safe_float(f_pk.value, 0) or 0)
                ),
                "stock_goods_supplier_qty": max(
                    0.0, float(_safe_float(f_gk.value, 0) or 0)
                ),
                # Preserve existing financial fields so mutual-settlements tab is not broken
                "balance_amount": float(ex.get("balance_amount") or 0),
                "balance_currency": str(ex.get("balance_currency") or "USD")
                .strip()
                .upper()
                or "USD",
                "opening_shipped_goods_qty": float(
                    ex.get("opening_shipped_goods_qty") or 0
                ),
                "balance_paid_amount": float(ex.get("balance_paid_amount") or 0),
                "company_id": self.company_id,
            }
            self._queue_pack_opening_save(save_kwargs)
            self._close_dlg(dlg)
            self._expanded_contexts.add(ctx_id)
            self._refresh_pack_sync_status()
            if not self._refresh_pack_ctx_inplace(ctx_id):
                self._schedule_debounced_reload(0.1)

        def _opening_help(_):
            from views.help_utils import show_modal_help
            _pg = self._page()
            show_modal_help(_pg, "Початковий залишок", [
                self.t("pack_stock.opening_help.step1", "1. Введіть залишок пакування на складі постачальника пакування (шт.)."),
                self.t("pack_stock.opening_help.step2", "2. Введіть залишок на складі постачальника товару (шт.)."),
                self.t("pack_stock.opening_help.step3", "3. Додайте коментар (за потреби)."),
                self.t("pack_stock.opening_help.step4", "4. Натисніть «Зберегти»."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Column(
                [
                    ft.Row(
                        [
                            self._text_wrap("Початковий залишок", size=18, weight=ft.FontWeight.W_600, color=_TEXT, expand=True),
                            ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_opening_help),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
                    ),
                    self._text_wrap(pack_name, size=12, color=_TEXT2),
                ],
                spacing=4,
                tight=True,
            ),
            content=ft.Container(
                width=field_w + 24,
                padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                content=ft.Column(
                    [
                        _form_row("Пакування у постачальника пакування, шт.", f_pk),
                        _form_row("Товар у постачальника товару, шт.", f_gk),
                        _form_row("Коментар", f_cmt),
                    ],
                    spacing=16,
                    tight=True,
                ),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: self._close_dlg(dlg)),
                ft.FilledButton("Зберегти", on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        page.show_dialog(dlg)

    # ─────────────────────── Списання пакування ──────────────────────────────

    def _queue_pack_writeoff_save(self, kwargs: dict) -> None:
        if self.subordinate_ro:
            return
        ctx_id = str((kwargs or {}).get("context_id") or "").strip()
        seq = int((kwargs or {}).get("seq_no") or 0)
        key = f"writeoff:{ctx_id}:{seq}"
        self._ops_outbox[key] = {
            "kind": "writeoff_save",
            "kwargs": dict(kwargs or {}),
        }
        self._save_pack_outbox()
        self._refresh_pack_sync_status()
        # Apply write-off to local cache immediately so balance updates without DB round-trip.
        if ctx_id:
            _pack_qty = float(
                (kwargs or {}).get("pack_qty") or (kwargs or {}).get("qty") or 0
            )
            _goods_qty = float((kwargs or {}).get("goods_qty") or 0)
            wo_row = {
                "id": key,
                "context_id": ctx_id,
                "row_kind": "writeoff",
                "qty": _pack_qty,
                "pack_qty": _pack_qty,
                "goods_qty": _goods_qty,
                "order_date": (kwargs or {}).get("order_date") or None,
                "seq_no": seq,
                "invoice_no": str((kwargs or {}).get("invoice_no") or ""),
                "comment": str((kwargs or {}).get("comment") or "Списання"),
            }
            # Replace by both outbox key AND seq_no to prevent duplicates when editing
            # an existing DB row (its id is a UUID, not the outbox key)
            existing = self._local_writeoff_cache.get(ctx_id) or []
            self._local_writeoff_cache[ctx_id] = [
                r
                for r in existing
                if str(r.get("id") or "") != key and int(r.get("seq_no") or 0) != seq
            ]
            self._local_writeoff_cache[ctx_id].append(wo_row)

    def _open_writeoff_dialog(self, ctx: dict, pack_row: dict) -> None:
        """Діалог «Списати пакування» — списуємо кількість зі складу постачальника товару."""
        page = self._page()
        if not page or self.subordinate_ro:
            return

        ctx_id = str(ctx.get("id") or "").strip()
        if not ctx_id:
            return

        # Всі товари цього контексту (зазвичай один, але може бути кілька)
        product_ids = list(
            {
                str((p or {}).get("id") or "").strip()
                for p in (self._packaging_products or [])
                if str((p or {}).get("id") or "").strip()
            }
            & {
                str(lid or "").strip()
                for lid in (
                    ctx.get("linked_product_ids") or [ctx.get("goods_product_id") or ""]
                )
                if str(lid or "").strip()
            }
        )
        if not product_ids:
            product_ids = [str(ctx.get("goods_product_id") or "").strip()]
        product_ids = [p for p in product_ids if p]

        pack_name = (
            str(pack_row.get("name") or pack_row.get("packaging_name") or "").strip()
            or "—"
        )
        rid = str(ctx.get("region_id") or "").strip() or (
            getattr(self, "_region_id_live", "") or ""
        )

        try:
            win_w = int(getattr(page, "width", 0) or 0)
            if win_w <= 0:
                win_w = int(getattr(getattr(page, "window", None), "width", 0) or 0)
        except Exception:
            win_w = 0
        field_w = min(max(280, win_w - 120), 440) if win_w else 360

        def _input(val: str = "", kb=ft.KeyboardType.NUMBER) -> ft.TextField:
            return ft.TextField(
                value=val,
                width=field_w,
                dense=False,
                text_size=14,
                border_radius=8,
                filled=True,
                bgcolor=_ALT2,
                border_color=_HDR,
                focused_border_color=_ERR,
                content_padding=ft.Padding.symmetric(horizontal=14, vertical=12),
                keyboard_type=kb,
            )

        f_pack_qty = _input("0")
        f_goods_qty = _input("0")
        comment_f = _input("Списання", kb=ft.KeyboardType.TEXT)

        date_f = ft.TextField(
            value=date.today().isoformat(),
            hint_text="РРРР-ММ-ДД",
            read_only=True,
            width=field_w,
            dense=False,
            text_size=14,
            border_radius=8,
            filled=True,
            bgcolor=_ALT2,
            border_color=_HDR,
            focused_border_color=_ERR,
            content_padding=ft.Padding.symmetric(horizontal=14, vertical=12),
            keyboard_type=ft.KeyboardType.TEXT,
        )
        calendar_btn = ft.IconButton(
            icon=ft.Icons.CALENDAR_MONTH,
            icon_size=22,
            tooltip="Обрати дату",
        )
        date_row_ctrl = ft.Row(
            [ft.Container(content=date_f, expand=True), calendar_btn],
            spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        dlg: ft.AlertDialog | None = None

        def _save(_=None):
            nonlocal dlg
            raw_p = (
                str(f_pack_qty.value or "").strip().replace(",", ".").replace(" ", "")
            )
            raw_g = (
                str(f_goods_qty.value or "").strip().replace(",", ".").replace(" ", "")
            )
            try:
                pack_q = float(raw_p or "0") if raw_p else 0.0
            except Exception:
                pack_q = 0.0
            try:
                goods_q = float(raw_g or "0") if raw_g else 0.0
            except Exception:
                goods_q = 0.0
            if pack_q <= 0 and goods_q <= 0:
                if dlg is not None:
                    self._close_dlg(dlg)
                return
            seq = self.db.packaging_writeoff_next_seq(
                ctx_id, manager_id=self.manager_id, region_id=rid
            )
            # Ensure seq is unique vs. local pending write-offs not yet flushed to DB.
            # Check both local cache AND outbox keys so a race between cache cleanup
            # and the next writeoff creation never causes a seq collision.
            _local_seqs = {
                int(r.get("seq_no") or 0)
                for r in (self._local_writeoff_cache.get(ctx_id) or [])
                if isinstance(r, dict)
            }
            _prefix = f"writeoff:{ctx_id}:"
            for _ok in self._ops_outbox:
                if _ok.startswith(_prefix):
                    try:
                        _local_seqs.add(int(_ok[len(_prefix) :]))
                    except (ValueError, TypeError):
                        pass
            while seq in _local_seqs:
                seq += 1
            inv_no = f"WO-{seq}"
            order_date_val = (
                str(date_f.value or "").strip()[:10] or date.today().isoformat()
            )
            save_kwargs = {
                "context_id": ctx_id,
                "pack_qty": pack_q,
                "goods_qty": goods_q,
                "qty": pack_q,
                "seq_no": seq,
                "manager_id": self.manager_id,
                "region_id": rid,
                "company_id": self.company_id,
                "comment": str(comment_f.value or "Списання"),
                "invoice_no": inv_no,
                "order_date": order_date_val,
            }
            self._queue_pack_writeoff_save(save_kwargs)
            if dlg is not None:
                self._close_dlg(dlg)
            self._refresh_pack_ctx_inplace(ctx_id)

        def _form_col(label: str, ctrl) -> ft.Column:
            return ft.Column(
                [ft.Text(label, size=12, weight="bold", color=_TEXT), ctrl],
                spacing=6,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            )

        rows_ctrl = [
            _form_col("Дата списання", date_row_ctrl),
            _form_col("Списання зі складу постачальника пакування, шт.", f_pack_qty),
            _form_col("Списання зі складу постачальника товару, шт.", f_goods_qty),
            _form_col("Коментар", comment_f),
        ]

        def _writeoff_help(_):
            from views.help_utils import show_modal_help
            _pg = self._page()
            show_modal_help(_pg, "Списати пакування", [
                self.t("pack_stock.writeoff_help.step1", "1. Оберіть дату списання."),
                self.t("pack_stock.writeoff_help.step2", "2. Вкажіть кількість зі складу постачальника пакування."),
                self.t("pack_stock.writeoff_help.step3", "3. Вкажіть кількість зі складу постачальника товару."),
                self.t("pack_stock.writeoff_help.step4", "4. Додайте коментар."),
                self.t("pack_stock.writeoff_help.step5", "5. Натисніть «Списати»."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                [
                    ft.Icon(ft.Icons.REMOVE_CIRCLE_OUTLINE, color=_ERR, size=20),
                    ft.Text("Списати пакування", size=16, weight="bold", expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_writeoff_help),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            content=ft.Container(
                width=field_w + 24,
                padding=ft.Padding.symmetric(horizontal=12, vertical=10),
                content=ft.Column(
                    [
                        ft.Text(f"Пакування: {pack_name}", size=12, color=_TEXT),
                        ft.Divider(height=8),
                        *rows_ctrl,
                    ],
                    spacing=10,
                    scroll=ft.ScrollMode.ADAPTIVE,
                ),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: self._close_dlg(dlg)),
                ft.FilledButton(
                    "Списати",
                    style=ft.ButtonStyle(bgcolor=_ERR),
                    on_click=_save,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        def _open_writeoff_calendar(_e=None):
            initial_d = date.today()
            try:
                ds = str(date_f.value or "").strip()[:10]
                if ds:
                    initial_d = datetime.fromisoformat(ds).date()
            except Exception:
                initial_d = date.today()

            picker = ft.DatePicker(
                value=initial_d,
                first_date=date(2018, 1, 1),
                last_date=date(2100, 12, 31),
                entry_mode=ft.DatePickerEntryMode.CALENDAR_ONLY,
                date_picker_mode=ft.DatePickerMode.DAY,
                locale=ft.Locale("uk", "UA"),
                cancel_text="Очистити",
                confirm_text="OK",
                modal=True,
                error_format_text="",
                error_invalid_text="",
            )

            def _picked(e):
                raw = _date_picker_event_value(e)
                if raw is None:
                    date_f.value = ""
                else:
                    try:
                        date_f.value = _datepicker_value_to_iso_date(raw)
                    except Exception:
                        date_f.value = str(raw)[:10]
                try:
                    date_f.update()
                    dlg.update()
                except Exception:
                    pass
                try:
                    picker.open = False
                    picker.update()
                except Exception:
                    pass
                with contextlib.suppress(Exception):
                    if picker in (page.overlay or []):
                        page.overlay.remove(picker)
                        page.update()

            picker.on_change = _picked
            page.overlay.append(picker)
            picker.open = True
            with contextlib.suppress(Exception):
                picker.update()

        date_f.on_click = _open_writeoff_calendar
        calendar_btn.on_click = _open_writeoff_calendar

        page.show_dialog(dlg)
