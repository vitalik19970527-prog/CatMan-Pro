import asyncio
import contextlib
import functools
import gc
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Callable, Optional, Union

import flet as ft

import fx_rates
import i18n
from logger import logger
from views.number_format import format_grouped, format_maybe_numeric_text, safe_float
from views import product_naming
from views.mobile_layout_helpers import is_mobile, make_scrollable_chip_row

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT and _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_ORDER_ATTACHMENT_MAX_SIZE = 20 * 1024 * 1024  # 20 MB


def _fmt_attachment_size(n: int) -> str:
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} КБ"
    return f"{n / (1024 * 1024):.1f} МБ"


try:
    import requests as _requests

    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


def _safe_float(v, default=None):
    return safe_float(v, default)


# ── Theme colour shims ────────────────────────────────────────────────────────
# Module defaults (light theme); overridden in build() via global statement.
_TEXT = "#111827"
_MUTED = "#64748B"
_BORDER = "#E2E8F0"
_PANEL = "#FFFFFF"
_ALT = "#F8FAFC"
_CHIP_T = "#1E3A8A"
_SOFT_I = "#EEF2FF"
_ERR = "#DC2626"
_ERR_BG = "#FEE2E2"


def _th_from_view(view) -> dict:
    from views.themes import get_active_theme

    return get_active_theme(str(getattr(view, "manager_id", "") or ""))


def _today_str() -> str:
    return date.today().isoformat()


def _normalize_currency_code(raw, default: str = "USD") -> str:
    allowed = {"USD", "CNY", "EUR", "UAH", "PLN"}
    aliases = {
        "$": "USD",
        "USDT": "USD",
        "US DOLLAR": "USD",
        "USDOLLAR": "USD",
        "YUAN": "CNY",
        "RMB": "CNY",
        "CNYRMB": "CNY",
        "EUR€": "EUR",
        "EURO": "EUR",
        "ZL": "PLN",
        "ZLOTY": "PLN",
        "PLNZL": "PLN",
        "GRN": "UAH",
        "UAHGRN": "UAH",
        "HRN": "UAH",
        "ГРН": "UAH",
        "ГРИВНЯ": "UAH",
    }
    token = str(raw or "").strip().upper().replace(" ", "")
    token = token.replace(".", "").replace("_", "")
    if token in allowed:
        return token
    if token in aliases:
        return aliases[token]
    fallback = str(default or "").strip().upper()
    if fallback == "":
        return ""
    return fallback if fallback in allowed else "USD"


def _datepicker_value_to_iso_date(picked) -> str:
    if picked is None:
        return ""
    try:
        if isinstance(picked, datetime):
            dt = picked
            if dt.tzinfo is not None:
                with contextlib.suppress(Exception):
                    dt = dt.astimezone()
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


def _days_between(d1_str: str, d2_str: str) -> Optional[int]:
    try:
        d1 = datetime.fromisoformat(d1_str[:10]).date() if d1_str else None
        d2 = datetime.fromisoformat(d2_str[:10]).date() if d2_str else None
        if d1 and d2:
            return (d2 - d1).days
    except Exception:
        pass
    return None


def _add_days_to_iso(base_date_str: str, days: Optional[float]) -> str:
    try:
        base = str(base_date_str or "").strip()[:10]
        if not base:
            return ""
        d0 = datetime.fromisoformat(base).date()
        d_days = int(round(float(days or 0)))
        if d_days < 0:
            d_days = 0
        return (d0 + timedelta(days=d_days)).isoformat()
    except Exception:
        return ""


def _invoice_sort_key(raw_invoice) -> tuple:
    src = str(raw_invoice or "").strip().lstrip("#")
    if not src:
        return (1, float("inf"), "")

    cleaned = "".join(ch for ch in src if (ch.isdigit() or ch in {".", ",", "-"}))
    if not cleaned:
        return (1, float("inf"), src.lower())

    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")

    if cleaned.count(".") > 1:
        first = cleaned.find(".")
        cleaned = cleaned[: first + 1] + cleaned[first + 1 :].replace(".", "")

    try:
        return (0, float(cleaned), "")
    except Exception:
        digits = "".join(ch for ch in cleaned if ch.isdigit())
        if digits:
            with contextlib.suppress(Exception):
                return (0, float(digits), "")
    return (1, float("inf"), src.lower())


def _order_row_sort_key(order_row: dict) -> tuple:
    raw_date = str((order_row or {}).get("order_date") or "")[:10]
    try:
        ord_date = datetime.fromisoformat(raw_date).date().toordinal()
    except Exception:
        ord_date = -1
    inv_kind, inv_num, inv_text = _invoice_sort_key((order_row or {}).get("invoice_no"))
    if inv_kind == 0:
        inv_key = (0, -float(inv_num), "")
    else:
        inv_key = (1, 0.0, str(inv_text or ""))
    return (-ord_date, inv_key)


@functools.lru_cache(maxsize=4096)
def _wrap_text_for_width(text: str, width_px: Optional[int]) -> str:
    raw = str(text or "")
    if not raw:
        return raw
    width = int(width_px or 0)
    chars_per_line = max(3, (width - 2) // 6) if width > 0 else 14

    out_lines: list[str] = []

    def _hard_break_token(token: str) -> list[str]:
        tok = str(token or "")
        if not tok:
            return [""]
        if len(tok) <= chars_per_line:
            return [tok]
        parts: list[str] = []
        start = 0
        while start < len(tok):
            parts.append(tok[start : start + chars_per_line])
            start += chars_per_line
        if len(parts) >= 2 and len(parts[-1]) <= 1 and len(parts[-2]) >= 3:
            borrow = min(2, len(parts[-2]) - 1)
            parts[-1] = parts[-2][-borrow:] + parts[-1]
            parts[-2] = parts[-2][:-borrow]
            if parts[-2] == "":
                parts.pop(-2)
        return [p for p in parts if p != ""]

    def _split_keep_separators(s: str) -> list[str]:
        seps = {" ", "-", "‑", "–", "—", "/"}
        out: list[str] = []
        buf = ""
        for ch in str(s or ""):
            if ch in seps:
                if buf:
                    out.append(buf)
                    buf = ""
                out.append(ch)
            else:
                buf += ch
        if buf:
            out.append(buf)
        return out

    tokens = _split_keep_separators(raw)
    cur = ""

    for token in tokens:
        if token == "":
            continue
        if token == " ":
            if cur and not cur.endswith(" "):
                cur += " "
            continue

        if len(token) > chars_per_line:
            if cur.strip():
                out_lines.append(cur.rstrip())
                cur = ""
            out_lines.extend(_hard_break_token(token))
            continue

        if not cur:
            cur = token
            continue

        if token in {"-", "‑", "–", "—", "/"}:
            trial = f"{cur}{token}"
        else:
            if cur and cur[-1] in {"-", "‑", "–", "—", "/"}:
                trial = f"{cur}{token}"
            else:
                trial = f"{cur}{token if cur.endswith(' ') else ' ' + token}"
        trial = trial.rstrip()

        if len(trial) <= chars_per_line:
            cur = trial
        else:
            out_lines.append(cur.rstrip())
            cur = token

    if cur.strip() != "":
        out_lines.append(cur.rstrip())

    if len(out_lines) >= 2 and len(out_lines[-1]) <= 1 and len(out_lines[-2]) >= 3:
        borrow = min(2, len(out_lines[-2]) - 1)
        out_lines[-1] = out_lines[-2][-borrow:] + out_lines[-1]
        out_lines[-2] = out_lines[-2][:-borrow].rstrip()
        if out_lines[-2] == "":
            out_lines.pop(-2)

    return "\n".join(out_lines)


class OrdersView:

    STATUSES = [
        "production",
        "ready_for_shipment",
        "in_transit",
        "at_warehouse",
        "accepted",
    ]
    STATUS_LABELS = {
        "production": "Вироб-во",
        "ready_for_shipment": "Готовий",
        "in_transit": "В дорозі",
        "at_warehouse": "На складі",
        "accepted": "Прийнятий",
    }
    STATUS_FG = {
        "production": "#92400E",
        "ready_for_shipment": "#92400E",
        "in_transit": "#1E40AF",
        "at_warehouse": "#5B21B6",
        "accepted": "#065F46",
    }
    STATUS_BG = {
        "production": "#FEF3C7",
        "ready_for_shipment": "#FEF08A",
        "in_transit": "#DBEAFE",
        "at_warehouse": "#EDE9FE",
        "accepted": "#D1FAE5",
    }
    STATUS_TRANSITION = {
        "production": "ready_for_shipment",
        "ready_for_shipment": "in_transit",
        "in_transit": "at_warehouse",
        "at_warehouse": "accepted",
    }
    STATUS_TRANSITION_DATE = {
        "ready_for_shipment": ("date_ready_at_supplier", "Дата готовності"),
        "in_transit": ("date_shipped", "Дата відправки"),
        "at_warehouse": ("date_arrived_warehouse", "Дата отримання на склад"),
        "accepted": ("date_accepted", "Дата прийомки"),
    }
    STATUS_DURATION_FIELD = {
        "ready_for_shipment": ("order_date", "duration_production_days"),
        "in_transit": ("date_ready_at_supplier", "duration_ready_at_supplier_days"),
        "at_warehouse": ("date_shipped", "duration_transit_days"),
        "accepted": ("date_arrived_warehouse", "duration_warehouse_days"),
    }
    PAYMENT_LABELS = {
        "unpaid": "Не сплачен.",
        "advance": "Аванс",
        "transferred": "Передано",
        "paid": "Сплач.",
    }
    SUBMISSION_LABELS = {"not_submitted": "Не переданий", "submitted": "Внесений"}
    MONTH_NAMES = {
        1: "Січень",
        2: "Лютий",
        3: "Березень",
        4: "Квітень",
        5: "Травень",
        6: "Червень",
        7: "Липень",
        8: "Серпень",
        9: "Вересень",
        10: "Жовтень",
        11: "Листопад",
        12: "Грудень",
    }
    CURRENCIES = ["USD", "CNY", "EUR", "UAH", "PLN"]
    HEADER_SIZE = 21

    def __init__(
        self, content_area, user_data, db, active_region=None, quick_filter: str = ""
    ):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.active_region = active_region
        self.quick_filter = quick_filter

        self.company_id = (self.user_data.get("company_id") or "").strip()
        self.manager_id = (
            self.user_data.get("active_manager_id") or self.user_data.get("id") or ""
        ).strip()
        self.t = i18n.bind(str(self.user_data.get("id") or ""))
        # Instance-level translated shadows of class-level display-label dicts.
        # Created once per instance so they reflect the active language at build time.
        # Safe because view instances are re-created on language switches.
        self.STATUS_LABELS = {
            k: self.t(f"orders.status.{k}", v)
            for k, v in OrdersView.STATUS_LABELS.items()
        }
        self.PAYMENT_LABELS = {
            k: self.t(f"orders.payment.{k}", v)
            for k, v in OrdersView.PAYMENT_LABELS.items()
        }
        self.SUBMISSION_LABELS = {
            k: self.t(f"orders.submission.{k}", v)
            for k, v in OrdersView.SUBMISSION_LABELS.items()
        }
        self.MONTH_NAMES = {
            k: self.t(f"orders.month.{k}", v)
            for k, v in OrdersView.MONTH_NAMES.items()
        }
        self.STATUS_TRANSITION_DATE = {
            k: (field_name, self.t(f"orders.transition_date.{k}", label))
            for k, (field_name, label) in OrdersView.STATUS_TRANSITION_DATE.items()
        }
        self.manager_name = (
            self.user_data.get("active_manager_name")
            or self.user_data.get("full_name")
            or ""
        ).strip()
        self.is_read_only = bool(self.user_data.get("subordinate_view_readonly"))
        # "Загальні" mode
        self._general_mode: bool = bool(self.user_data.get("general_mode", False))
        self._general_manager_ids: list[str] = list(
            self.user_data.get("general_manager_ids") or []
        )
        from views.general_mode_mixin import build_general_manager_names

        self._general_manager_names: dict[str, str] = build_general_manager_names(
            self.user_data.get("all_subordinates_flat") or [], self.user_data
        )
        _som = self.user_data.get("shared_orders_map")
        self._shared_orders_map: dict[str, dict] = (
            _som if isinstance(_som, dict) else {}
        )

        self._orders: list = []
        self._suppliers: list = []
        self._suppliers_by_id: dict = {}
        self._products: list = []
        self._delivery_methods_by_id: dict = {}
        self._products_by_id_cache: dict = (
            {}
        )  # rebuilt once after load; reused by every _build_order_row
        self._visible_product_ids_cache: set[str] = set()
        self._delivery_methods: list = []
        self._mgr_workspace_state_cache: dict = {}

        self._filter_status = "all"
        self._filter_delivery_id = "all"
        self._filter_product_id = "all"
        self._filter_category_id = "all"
        self._filter_supplier_id = "all"
        self._filter_payment = "all"
        self._filter_submission = "all"
        self._quick_filter_mode = (
            ""  # custom KPI filter that can't map to standard dropdowns
        )
        # Apply quick_filter pre-sets from home KPI cards.
        _qf = self.quick_filter
        if _qf.startswith("status:"):
            self._filter_status = _qf[len("status:") :]
        elif _qf == "post_production_not_submitted":
            self._filter_status = "all"
            self._filter_submission = "not_submitted"
            self._quick_filter_mode = "not_submitted_non_production"
        elif _qf == "missing_status_date":
            self._quick_filter_mode = "missing_status_date"
        self._filter_receive_month = "all"
        self._filter_accept_month = "all"
        self._filter_payment_month = "all"
        self._filter_level_node_ids = {}
        self._group_expand_mode = "actual"  # actual | all | supplier

        self._tree_host = ft.Column(spacing=3)
        self._tables_inner_container = None
        self._tables_outer_container = None
        self._supplier_section_controls = {}
        self._order_row_controls = {}
        self._order_fast_refresh: dict = {}
        self._order_month_rows_col: dict = {}  # month_key → ft.Column(rows)
        self._month_counter_row_by_key: dict = {}  # month_key → ft.Row(counter chips)
        self._year_counter_row_by_key: dict = {}  # "sid:year" → ft.Row(counter chips)
        self._supplier_counter_row_by_key: dict = (
            {}
        )  # supplier_id → ft.Row(counter chips)
        self._month_collapsible_by_key: dict = (
            {}
        )  # month_key → ft.Container(collapsible)
        self._year_collapsible_by_key: dict = (
            {}
        )  # "sid:year" → ft.Container(collapsible)
        self._supplier_collapsible_by_key: dict = (
            {}
        )  # supplier_id → ft.Container(collapsible)
        self._pending_group_counter_supplier_ids: set[str] = set()
        self._group_counter_refresh_scheduled: bool = False
        self._order_year_body_col: dict = {}  # "sid:year" → ft.Column(month_sections)
        self._order_year_month_numbers: dict = {}  # "sid:year" → list[int] desc
        self._orders_page_size = 60
        self._orders_fetch_batch_size = 120
        self._max_initial_fetch_batches = 3
        self._orders_offset = 0
        self._orders_has_more = False
        self._orders_loading = False
        self._load_more_btn = None
        self._loading_box = None
        self._status_filter_dd = None
        self._supplier_filter_dd = None
        self._delivery_filter_dd = None
        self._product_filter_dd = None
        self._category_filter_dd = None
        self._payment_filter_dd = None
        self._submission_filter_dd = None
        self._level_filter_dd_by_key = {}
        self._filter_bar_host = None
        self._payment_summary_panel_host = None
        self._category_level_titles_cache_key = ""
        self._category_level_titles_cache = {}
        self._owned_order_ids = set()
        self._show_dates_block = True
        self._show_cost_block = True
        self._fx_uah_rates_cache = {}
        self._fx_uah_rates_ts = 0.0
        self._table_row_height = 32
        self._table_col_width_overrides = {}
        self._table_col_width_overrides_unit = "px"
        self._table_col_width_px_migrated = False
        # "Перший місяць відображення": ховає рендер місяців ДО цього (формат "YYYY-MM"),
        # порожній рядок = показувати всю історію. Дані в БД не чіпаються — це лише
        # фільтр рендеру дерева (_filtered_orders), лічильники рахуються з self._orders повністю.
        self._display_from_month: str = ""
        self._suppress_auto_render_until_ts = 0.0
        self._counters_row: Optional[ft.Row] = None
        self._invoice_panel_open_by_order = {}
        # order_id -> [{"id","kind","original_filename","file_size",...}, ...]
        self._order_attachments_cache: dict = {}
        self._expanded_multi_item_order_ids = set()
        self._dialog_editing_order_id = ""
        self._content_v_offset: float = 0.0
        self._table_h_offsets: dict[str, float] = {}
        self._table_scroll_rows: dict[str, ft.Row] = {}
        self._pinned_h_scrollbar: Optional[ft.Row] = None
        self._h_scrollbar_offset: float = 0.0
        self._h_sync_pending: bool = False
        self._header_busy_box: Optional[ft.Container] = None
        self._header_busy_text: Optional[ft.Text] = None
        self._header_busy_tokens: set[str] = set()
        self._editable_product_ids: set = set()

        self._page_loading = False
        self._orders_ui_ready = False
        self._page_title_spinner: Optional[ft.ProgressRing] = None
        self._orders_cache: dict[str, dict] = {}
        # Safety net independent of whatever causes a stray full reload to
        # miss a row (stale snapshot pulse, DB replication lag, etc.) — every
        # order newly created THIS session is remembered here and force
        # re-merged back into self._orders after any reload that doesn't
        # already contain it, so a freshly added order can never silently
        # vanish until the app is restarted.
        self._session_new_orders: dict[str, dict] = {}

        self._outbox: dict[str, dict] = {}
        self._outbox_flush_running: bool = False
        self._sync_timer_task = None
        self._outbox_fast_flush_task = None
        self._outbox_file_save_task = None
        self._outbox_file_save_dirty = False
        self._orders_snapshot_save_scheduled: bool = False
        self._orders_snapshot_pulse_ts = 0
        self._ref_data_pulse_ts: int = 0
        self._snapshot_entity_owner_order_count: int = -1
        self._orders_live_sync_task = None
        self._initial_load_task = None
        self._prefetch_applied_once = False
        # Adaptive auto-expand timing — increases automatically after each failure.
        self._flutter_secs_per_order: float = 0.40
        self._auto_expand_retries: int = 0
        self._sync_status: str = "synced"
        self._sync_error_count: int = 0
        self._sync_indicator_text: Optional[ft.Text] = None
        self._sync_indicator_dot: Optional[ft.Container] = None
        self._sync_indicator_container: Optional[ft.Container] = None
        self._shared_date_picker_open: bool = False
        self._shared_date_picker_last_open_ts: float = 0.0
        self._open_order_btn = None
        self._quick_add_btn = None
        self._table_btn = None
        self._unsaved_local_order_ids: set[str] = set()
        self._force_sync_hooks: dict[str, object] = {}

        self._regions_data = [
            r
            for r in (self.db.get_company_regions(self.company_id) or [])
            if isinstance(r, dict)
        ]
        self._region_id_by_name_map = {
            str(r.get("name") or "").strip(): str(r.get("id") or "").strip()
            for r in self._regions_data
            if r.get("id") and r.get("name")
        }
        self._no_db_after_ready: bool = False
        self._visible_column_keys_cache: Optional[set] = None
        self._click_queue: list = []
        self._click_counter: int = 0
        self._load_table_layout_state()

    def _debug_print(self, msg: str) -> None:
        logger.info("[orders-debug] %s", msg)

    def _region_id_by_name(self, name):
        if not name:
            return None
        return self._region_id_by_name_map.get(str(name).strip())

    def _effective_region_id(self):
        raw = (self.active_region or "").strip()
        if not raw:
            return None
        if raw in self._region_id_by_name_map.values():
            return raw
        return self._region_id_by_name(raw)

    def _region_enabled_levels(self) -> list:
        try:
            region = self._effective_region_id()
            cfg = None
            try:
                cfg = self._company_catalog_config_cache.get(region)
            except Exception:
                cfg = None
            if cfg is None:
                cfg = self.db.get_company_catalog_config(self.company_id, region) or {}
                with contextlib.suppress(Exception):
                    self._company_catalog_config_cache[region] = cfg
            rows = cfg.get("levels") if isinstance(cfg, dict) else []
            out = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                try:
                    pos = int(row.get("position") or 0)
                except Exception:
                    continue
                if pos < 1 or pos > 6:
                    continue
                if not bool(row.get("enabled")):
                    continue
                out.append(
                    {
                        "position": pos,
                        "title": str(row.get("title") or f"Рівень {pos}").strip()
                        or f"Рівень {pos}",
                    }
                )
            out.sort(key=lambda x: int(x.get("position") or 0))
            return out
        except Exception:
            return []

    def _category_level_titles(self) -> dict:
        region_id = str(self._effective_region_id() or "").strip()
        cache_key = f"{self.company_id}:{region_id}"
        if cache_key == self._category_level_titles_cache_key and isinstance(
            self._category_level_titles_cache, dict
        ):
            return self._category_level_titles_cache

        out = {}
        try:
            all_categories = (
                getattr(self, "_categories", None)
                if getattr(self, "_categories", None) is not None
                else (
                    self.db.get_company_categories(
                        self.company_id, region_id, manager_id=self.manager_id
                    )
                    or []
                )
            )
            for cat_row in all_categories:
                if not isinstance(cat_row, dict):
                    continue
                cat_id = str(cat_row.get("id") or "").strip()
                if not cat_id:
                    continue
                cid = cat_id
                cfg = None
                try:
                    cfg = self._category_levels_config_map.get(cid)
                except Exception:
                    cfg = None
                if cfg is None:
                    try:
                        cfg = self.db.get_category_levels_config(cat_row) or {}
                    except Exception:
                        cfg = {}
                    with contextlib.suppress(Exception):
                        self._category_levels_config_map[cid] = cfg
                levels = cfg.get("levels") if isinstance(cfg, dict) else []
                title_by_pos = {}
                for lvl in levels or []:
                    if not isinstance(lvl, dict):
                        continue
                    try:
                        pos = int(lvl.get("position") or 0)
                    except Exception:
                        continue
                    if pos < 1 or pos > 6 or not bool(lvl.get("enabled")):
                        continue
                    title_by_pos[pos] = (
                        str(lvl.get("title") or f"Рівень {pos}").strip()
                        or f"Рівень {pos}"
                    )
                if title_by_pos:
                    out[cat_id] = title_by_pos
        except Exception:
            out = {}

        self._category_level_titles_cache_key = cache_key
        self._category_level_titles_cache = out
        return out

    def _product_card_hierarchy_index(self):
        card_index = {}
        node_name_by_id = {}
        try:
            region_id = self._effective_region_id()
            all_nodes = getattr(self, "_catalog_nodes", None)
            if all_nodes is None:
                all_nodes = self.db.get_company_catalog_nodes(self.company_id) or []
            by_id = {}
            for n in all_nodes:
                if not isinstance(n, dict):
                    continue
                nid = str(n.get("id") or "").strip()
                if not nid:
                    continue
                n_region = str(n.get("region_id") or "").strip()
                if region_id and n_region and n_region != region_id:
                    continue
                by_id[nid] = n
                node_name_by_id[nid] = str(n.get("name") or "").strip()

            cats = getattr(self, "_categories", None)
            if cats is None:
                cats = (
                    self.db.get_company_categories(
                        self.company_id, region_id, manager_id=self.manager_id
                    )
                    or []
                )
            category_map = {
                str((c or {}).get("id") or "").strip(): c
                for c in (cats or [])
                if isinstance(c, dict) and (c.get("id") or "")
            }

            for p in self._products or []:
                if not isinstance(p, dict):
                    continue
                pid = str((p or {}).get("id") or "").strip()
                if not pid:
                    continue

                cat_id = str(
                    p.get("category_id")
                    or (
                        (p.get("category") or {})
                        if isinstance(p.get("category"), dict)
                        else {}
                    ).get("id")
                    or ""
                ).strip()
                cat_name = str(
                    p.get("category_name")
                    or (
                        (p.get("category") or {})
                        if isinstance(p.get("category"), dict)
                        else {}
                    ).get("name")
                    or ""
                ).strip()

                level_node_ids = {}
                for pos in range(1, 7):
                    direct_id = str((p or {}).get(f"level_{pos}_node_id") or "").strip()
                    if direct_id:
                        level_node_ids[pos] = direct_id

                leaf_node_id = str((p or {}).get("catalog_node_id") or "").strip()
                cur = leaf_node_id
                hops = 0
                while cur and hops < 40:
                    row = by_id.get(cur)
                    if not isinstance(row, dict):
                        break
                    node_type = str(row.get("node_type") or "").strip().lower()
                    if node_type == "category" and not cat_name:
                        cat_name = str(row.get("name") or "").strip()
                        if not cat_id:
                            legacy = str(row.get("legacy_id") or "").strip()
                            if legacy.startswith("cat:"):
                                cat_id = legacy.split(":", 1)[1].strip()
                    elif node_type.startswith("level_"):
                        try:
                            pos = int(node_type.split("_", 1)[1])
                        except Exception:
                            pos = 0
                        if 1 <= pos <= 6 and pos not in level_node_ids:
                            level_node_ids[pos] = str(row.get("id") or "").strip()
                    cur = str(row.get("parent_id") or "").strip()
                    hops += 1

                if cat_id and not cat_name:
                    cat_name = str(
                        ((category_map.get(cat_id) or {}).get("name") or "")
                    ).strip()

                card_index[pid] = {
                    "category_id": cat_id,
                    "category_name": cat_name,
                    "level_node_ids": level_node_ids,
                }
        except Exception:
            pass

        return card_index, node_name_by_id

    def _common_items_label(self, items: list) -> str:
        """Returns deepest shared catalog-level name across all items.

        Tries levels 6→1 then falls back to category name.
        Returns '' when no common level found (caller should show count).
        """
        if not items or len(items) < 2:
            return ""
        node_names = getattr(self, "_node_name_by_id", {}) or {}
        products_by_id = self._products_by_id_cache or {}
        level_sets: dict[int, set] = {}
        cat_ids: set = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = str((item.get("product_id") or "")).strip()
            product = products_by_id.get(pid) or {}
            cid = str(product.get("category_id") or "").strip()
            if cid:
                cat_ids.add(cid)
            for pos in range(6, 0, -1):
                nid = str(product.get(f"level_{pos}_node_id") or "").strip()
                if nid:
                    level_sets.setdefault(pos, set()).add(nid)
        for pos in range(6, 0, -1):
            ids = level_sets.get(pos)
            if ids and len(ids) == 1:
                name = node_names.get(next(iter(ids)), "").strip()
                if name:
                    return name
        if len(cat_ids) == 1:
            cats = getattr(self, "_categories", None) or []
            cat_map = {str(c.get("id") or ""): c for c in cats if isinstance(c, dict)}
            cat = cat_map.get(next(iter(cat_ids))) or {}
            cat_name = str(cat.get("name") or "").strip()
            if cat_name:
                return cat_name
        return ""

    def _product_option_label_map(
        self, products: Optional[list] = None
    ) -> dict[str, str]:
        """Return {product_id: display_name}, appending " | Кат: X" / " | Постач: Y"
        for products whose name repeats elsewhere in `products` — see
        views/product_naming.py for the shared disambiguation rule."""
        src = products if isinstance(products, list) else list(self._products or [])

        cat_name_by_id: dict[str, str] = {
            str((c or {}).get("id") or "").strip(): str((c or {}).get("name") or "").strip()
            for c in (getattr(self, "_categories", None) or [])
            if isinstance(c, dict) and (c.get("id") or "") and (c.get("name") or "")
        }
        suppliers_by_id: dict = getattr(self, "_suppliers_by_id", None) or {}
        if not hasattr(self, "_node_path_cache"):
            self._node_path_cache = {}
        _node_path_of = product_naming.make_node_path_resolver(
            self.db, self._node_path_cache
        )

        def _category_of(p: dict) -> str:
            # node_name_by_id (whole catalog_nodes tree, leaf's OWN name) used
            # to be the last-resort fallback here — wrong, since a product's
            # catalog_node_id is its own LEAF node (often literally named the
            # same as the product), not the category. The real category is
            # the ROOT of that node's path, resolved via resolve_category_label.
            return product_naming.resolve_category_label(
                p, node_path_of=_node_path_of
            ) or cat_name_by_id.get(str(p.get("category_id") or "").strip(), "")

        def _supplier_of(p: dict) -> str:
            return str(
                p.get("supplier_name")
                or (
                    ((p.get("supplier") or {}).get("name"))
                    if isinstance(p.get("supplier"), dict)
                    else ""
                )
                or (suppliers_by_id.get(str(p.get("supplier_id") or "").strip()) or {}).get("name", "")
                or ""
            ).strip()

        valid = [p for p in src if isinstance(p, dict) and str(p.get("id") or "").strip()]
        suffixes = product_naming.compute_name_suffixes(
            valid, category_of=_category_of, supplier_of=_supplier_of
        )
        out: dict[str, str] = {}
        for p in valid:
            pid = str(p.get("id") or "").strip()
            name = str(p.get("name") or "").strip() or pid
            out[pid] = name + suffixes.get(pid, "")
        return out

    def _table_layout_key(self) -> str:
        region = (self._effective_region_id() or "all").strip() or "all"
        cid = (self.company_id or "").strip() or "no_company"
        return f"orders_table:{cid}:{region}"

    def _workspace_user_id(self) -> str:
        ud = self.user_data or {}
        return str(
            ud.get("id") or ud.get("active_manager_id") or self.manager_id or ""
        ).strip()

    def _visible_orders_column_keys(self) -> set[str]:
        all_keys = {
            "lines_expand",
            "expand",
            "delete",
            "date",
            "log",
            "inv",
            "order1c",
            "product",
            "status",
            "delivery",
            "comment",
            "qty",
            "amount",
            "amount_usd",
            "payment",
            "submission",
            "date_prod_end",
            "dur_prod",
            "date_wh",
            "dur_deliv",
            "date_payment",
            "date_receive",
            "date_1c",
            "dur_acc",
            "term_acc",
            "w_prev",
            "w_fact",
            "tariff",
            "unit_cost",
            "action_edit",
        }
        if self._visible_column_keys_cache is not None:
            cached = set(self._visible_column_keys_cache)
            if self._show_dates_block:
                cached.add("date_payment")
                cached.add("date_receive")
                cached.add("term_acc")
            return cached or set(all_keys)
        uid = self._workspace_user_id()
        if not uid:
            return set(all_keys)
        try:
            payload = self.db.get_manager_workspace_state(uid) or {}
            store = (
                payload.get("orders_visible_columns")
                if isinstance(payload, dict)
                else {}
            )
            raw = (
                store.get(self._table_layout_key()) if isinstance(store, dict) else None
            )
            if isinstance(raw, list):
                selected = {str(v).strip() for v in raw if str(v).strip()}
                visible = {k for k in all_keys if k in selected}
                self._visible_column_keys_cache = set(visible)
                if self._show_dates_block:
                    visible.add("date_payment")
                    visible.add("date_receive")
                    visible.add("term_acc")
                return visible or set(all_keys)
        except Exception:
            pass
        self._visible_column_keys_cache = set(all_keys)
        return set(all_keys)

    def _set_visible_orders_column_keys(self, keys: list[str]) -> None:
        uid = self._workspace_user_id()
        if not uid:
            return
        all_keys = {
            "lines_expand",
            "expand",
            "delete",
            "date",
            "log",
            "inv",
            "order1c",
            "product",
            "status",
            "delivery",
            "comment",
            "qty",
            "amount",
            "amount_usd",
            "payment",
            "submission",
            "date_prod_end",
            "dur_prod",
            "date_wh",
            "dur_deliv",
            "date_payment",
            "date_receive",
            "date_1c",
            "dur_acc",
            "term_acc",
            "w_prev",
            "w_fact",
            "tariff",
            "unit_cost",
            "action_edit",
        }
        chosen = {str(v).strip() for v in (keys or []) if str(v).strip()}
        normalized = [k for k in chosen if k in all_keys]
        if not normalized:
            normalized = list(all_keys)
        try:
            payload = self.db.get_manager_workspace_state(uid) or {}
            if not isinstance(payload, dict):
                payload = {}
            store = (
                payload.get("orders_visible_columns")
                if isinstance(payload.get("orders_visible_columns"), dict)
                else {}
            )
            store[self._table_layout_key()] = list(normalized)
            payload["orders_visible_columns"] = store
            self.db.save_manager_workspace_state(uid, payload)
            self._visible_column_keys_cache = (
                None  # invalidate so next render reloads from DB
            )
        except Exception:
            pass

    def _load_table_layout_state(self):
        try:
            manager_id = self._workspace_user_id()
            if not manager_id:
                return
            local_path = self._layout_local_file_path()
            if os.path.exists(local_path):
                try:
                    import json as _json

                    with open(local_path, "r", encoding="utf-8") as f:
                        row_state = _json.load(f)
                    if isinstance(row_state, dict):
                        h = _safe_float(row_state.get("row_height"), None)
                        if h is not None:
                            self._table_row_height = max(26, min(72, int(h)))
                        unit = row_state.get("col_widths_unit")
                        if unit in ("px", "pct", "percent", "%"):
                            self._table_col_width_overrides_unit = (
                                "pct" if unit in ("pct", "percent", "%") else "px"
                            )
                        widths = (
                            row_state.get("col_widths")
                            if isinstance(row_state.get("col_widths"), dict)
                            else {}
                        )
                        self._table_col_width_overrides = {
                            str(k): max(35, min(900, int(v)))
                            for k, v in (widths or {}).items()
                            if _safe_float(v, None) is not None
                        }
                        dfm = row_state.get("display_from_month")
                        if isinstance(dfm, str):
                            self._display_from_month = dfm.strip()[:7]
                        return
                except Exception:
                    pass
            payload = self.db.get_manager_workspace_state(manager_id) or {}
            table_store = (
                payload.get("orders_table_layout") if isinstance(payload, dict) else {}
            )
            table_store = table_store if isinstance(table_store, dict) else {}
            row_state = (
                table_store.get(self._table_layout_key())
                if isinstance(table_store, dict)
                else {}
            )
            row_state = row_state if isinstance(row_state, dict) else {}

            h = _safe_float(row_state.get("row_height"), None)
            if h is not None:
                self._table_row_height = max(26, min(72, int(h)))

            unit = row_state.get("col_widths_unit")
            if unit in ("px", "pct", "percent", "%"):
                self._table_col_width_overrides_unit = (
                    "pct" if unit in ("pct", "percent", "%") else "px"
                )

            self._table_col_width_px_migrated = (
                self._table_col_width_overrides_unit == "pct"
            )

            widths = (
                row_state.get("col_widths")
                if isinstance(row_state.get("col_widths"), dict)
                else {}
            )
            normalized = {}
            for k, v in (widths or {}).items():
                w = _safe_float(v, None)
                if w is None:
                    continue
                if self._table_col_width_overrides_unit == "pct":
                    normalized[str(k)] = float(max(0.5, min(90.0, float(w))))
                else:
                    normalized[str(k)] = max(35, min(900, int(w)))
            self._table_col_width_overrides = normalized

            dfm = row_state.get("display_from_month")
            if isinstance(dfm, str):
                self._display_from_month = dfm.strip()[:7]

            self._save_layout_to_file()
        except Exception:
            pass

    def _save_table_layout_state(self):
        self._save_layout_to_file()
        page = self._page()
        if page and hasattr(page, "run_task"):
            with contextlib.suppress(Exception):
                page.run_task(self._async_flush_layout_to_db)

    async def _async_flush_layout_to_db(self) -> None:
        try:
            manager_id = self._workspace_user_id()
            if not manager_id:
                return
            payload = (
                await asyncio.to_thread(self.db.get_manager_workspace_state, manager_id)
                or {}
            )
            if not isinstance(payload, dict):
                payload = {}
            table_store = (
                payload.get("orders_table_layout")
                if isinstance(payload.get("orders_table_layout"), dict)
                else {}
            )
            table_store[self._table_layout_key()] = {
                "row_height": int(self._table_row_height),
                "col_widths": dict(self._table_col_width_overrides or {}),
                "col_widths_unit": (
                    "pct" if self._table_col_width_overrides_unit == "pct" else "px"
                ),
                "display_from_month": str(self._display_from_month or ""),
            }
            payload["orders_table_layout"] = table_store
            await asyncio.to_thread(
                self.db.save_manager_workspace_state, manager_id, payload
            )
        except Exception:
            pass

    def _app_data_dir(self) -> str:
        base = os.getenv("FLET_APP_STORAGE_DATA") or os.path.join(
            os.getenv("APPDATA") or os.path.expanduser("~"), "CatManPro"
        )
        os.makedirs(base, exist_ok=True)
        return base

    def _layout_local_file_path(self) -> str:
        uid = (
            str((self.user_data or {}).get("id") or "nouser")
            .replace("/", "_")
            .replace("\\", "_")
        )
        cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        rid = (
            str(self._effective_region_id() or "all")
            .replace("/", "_")
            .replace("\\", "_")
        )
        return os.path.join(
            self._app_data_dir(), f"orders_layout_{cid}_{uid}_{rid}.json"
        )

    def _save_layout_to_file(self) -> None:
        try:
            import json as _json

            data = {
                "row_height": int(self._table_row_height),
                "col_widths": dict(self._table_col_width_overrides or {}),
                "col_widths_unit": (
                    "pct" if self._table_col_width_overrides_unit == "pct" else "px"
                ),
                "display_from_month": str(self._display_from_month or ""),
            }
            with open(self._layout_local_file_path(), "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    def _legacy_storage_file(self) -> str:
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_reg = (
            (self._effective_region_id() or "all").replace("/", "_").replace("\\", "_")
        )
        return os.path.join(self._app_data_dir(), f"orders_{safe_mgr}_{safe_reg}.json")

    def _orders_snapshot_file(self) -> str:
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_reg = (
            (self._effective_region_id() or "all").replace("/", "_").replace("\\", "_")
        )
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        return os.path.join(
            self._app_data_dir(),
            f"orders_snapshot_{safe_cid}_{safe_mgr}_{safe_reg}_v2.json",
        )

    def _orders_scope_pulse_ts(self) -> int:
        start = time.monotonic()
        try:
            out = self.db.get_companies_sync_pulses([self.company_id], ["orders"]) or {}
            by_company = out.get(self.company_id) if isinstance(out, dict) else {}
            ts = (by_company or {}).get("orders") if isinstance(by_company, dict) else 0
            return int(ts or 0)
        except Exception:
            return 0
        finally:
            self._debug_print(
                f"load pulse ts completed in {time.monotonic() - start:.3f}s"
            )

    def _load_orders_snapshot_cache(self) -> bool:
        start = time.monotonic()
        path = self._orders_snapshot_file()
        if not os.path.exists(path):
            self._debug_print("orders snapshot cache missing")
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            rows = payload.get("orders", []) if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                self._debug_print("orders snapshot cache invalid format")
                return False
            # Deduplicate by order ID (keeps first occurrence after sort)
            _seen: set = set()
            _deduped: list = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                _oid = (r.get("id") or "").strip()
                if _oid and _oid not in _seen:
                    _seen.add(_oid)
                    _deduped.append(r)
            self._orders = _deduped
            # Don't trust an empty snapshot — force DB reload to avoid "stuck at 0" bug.
            if not self._orders:
                self._debug_print(
                    "orders snapshot cache is empty — treating as not ready"
                )
                return False
            self._orders.sort(key=_order_row_sort_key)
            self._sync_to_shared_map()
            self._orders_offset = len(self._orders)
            self._orders_has_more = False
            self._orders_snapshot_pulse_ts = int((payload or {}).get("pulse_ts") or 0)
            self._snapshot_entity_owner_order_count = int(
                (payload or {}).get("entity_owner_order_count", -1)
            )
            self._debug_print(
                f"orders snapshot cache loaded {len(self._orders)} rows pulse_ts={self._orders_snapshot_pulse_ts} entity_owner_order_count={self._snapshot_entity_owner_order_count} in {time.monotonic() - start:.3f}s"
            )
            return True
        except Exception:
            self._debug_print("orders snapshot cache load failed")
            return False

    def _save_orders_snapshot_cache(self, pulse_ts: int | None = None) -> None:
        import datetime as _dt

        def _json_default(obj):
            if isinstance(obj, (_dt.datetime, _dt.date)):
                return obj.isoformat()
            return str(obj)

        try:
            path = self._orders_snapshot_file()
            ts = int(
                pulse_ts
                if pulse_ts is not None
                else (self._orders_snapshot_pulse_ts or 0)
            )
            eo_count = -1
            try:
                eo_count = self.db.count_entity_owner_entity_ids(
                    self.company_id, self.manager_id, "order"
                )
            except Exception:
                pass
            _save_seen: set = set()
            _save_rows: list = []
            for _r in self._orders or []:
                if not isinstance(_r, dict):
                    continue
                _oid = (_r.get("id") or "").strip()
                if _oid and _oid not in _save_seen:
                    _save_seen.add(_oid)
                    _save_rows.append(dict(_r))
            payload = {
                "orders": _save_rows,
                "pulse_ts": ts,
                "entity_owner_order_count": eo_count,
                "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
            }
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    payload,
                    f,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=_json_default,
                )
            os.replace(tmp, path)
            # Clean up legacy snapshot file (same context, no _v2 suffix)
            if path.endswith("_v2.json"):
                legacy_path = path[: -len("_v2.json")] + ".json"
                with contextlib.suppress(Exception):
                    if os.path.exists(legacy_path):
                        os.remove(legacy_path)
        except Exception:
            pass

    def _read_legacy_rows(self) -> list:
        path = self._legacy_storage_file()
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            rows = payload.get("orders", []) if isinstance(payload, dict) else []
            return rows if isinstance(rows, list) else []
        except Exception:
            return []

    def _write_legacy_rows(self, rows: list) -> bool:
        try:
            path = self._legacy_storage_file()
            payload = {
                "orders": rows if isinstance(rows, list) else [],
                "updated_at": datetime.utcnow().isoformat(timespec="seconds"),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def _outbox_file_path(self) -> str:
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        return os.path.join(
            self._app_data_dir(), f"orders_outbox_{safe_cid}_{safe_mgr}.json"
        )

    def _load_outbox(self) -> None:
        start = time.monotonic()
        path = self._outbox_file_path()
        if not os.path.exists(path):
            self._debug_print("outbox file missing")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            items = raw.get("items") if isinstance(raw, dict) else {}
            if not isinstance(items, dict):
                self._debug_print("outbox file invalid format")
                return
            for oid, snap in items.items():
                oid = str(oid or "").strip()
                if oid and isinstance(snap, dict):
                    self._outbox[oid] = snap
                elif oid:
                    self._debug_print(f"outbox item ignored invalid snapshot for {oid}")
        except Exception as ex:
            self._debug_print(f"outbox load failed: {ex}")
        finally:
            self._debug_print(
                f"load outbox finished {len(self._outbox)} items in {time.monotonic() - start:.3f}s"
            )

    def _save_outbox_to_file(self) -> None:
        try:
            path = self._outbox_file_path()
            payload = {
                "items": {oid: snap for oid, snap in self._outbox.items()},
                "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
            }
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception:
            pass

    def _schedule_outbox_file_save(self, delay_sec: float = 0.35) -> None:
        self._outbox_file_save_dirty = True
        if self._outbox_file_save_task is not None:
            return
        page = self._page()
        if not page or not hasattr(page, "run_task"):
            self._save_outbox_to_file()
            self._outbox_file_save_dirty = False
            return

        async def _deferred_save():
            try:
                await asyncio.sleep(max(0.05, float(delay_sec or 0.1)))
                if self._outbox_file_save_dirty:
                    await asyncio.to_thread(self._save_outbox_to_file)
                    self._outbox_file_save_dirty = False
            finally:
                self._outbox_file_save_task = None
                if self._outbox_file_save_dirty:
                    self._schedule_outbox_file_save(delay_sec=delay_sec)

        started = False
        try:
            self._outbox_file_save_task = page.run_task(_deferred_save)
            started = True
        except Exception:
            started = False
        if not started:
            self._outbox_file_save_task = None
            self._save_outbox_to_file()
            self._outbox_file_save_dirty = False

    def _merge_payments_outbox(self) -> list:
        """Reads Payments tab outbox and merges any order changes into self._orders.
        Handles both full-order ('order:{oid}') and patch ('order_patch:{oid}') entries.
        Returns list of updated order dicts (empty = no changes)."""
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        path = os.path.join(
            self._app_data_dir(), f"payments_outbox_{safe_cid}_{safe_mgr}.json"
        )
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            items = raw.get("items") if isinstance(raw, dict) else {}
            if not isinstance(items, dict) or not items:
                return []
            orders_by_id = {
                str((o or {}).get("id") or "").strip(): i
                for i, o in enumerate(self._orders)
                if isinstance(o, dict)
            }
            updated: list = []
            for key, snap in items.items():
                if not isinstance(snap, dict):
                    continue
                # Full order snapshot: key = "order:{oid}"
                if key.startswith("order:"):
                    oid = str(snap.get("id") or key[len("order:") :]).strip()
                # Patch (only changed fields): key = "order_patch:{oid}"
                elif key.startswith("order_patch:"):
                    oid = key[len("order_patch:") :].strip()
                else:
                    continue
                if not oid:
                    continue
                idx = orders_by_id.get(oid)
                if idx is None:
                    continue
                existing = self._orders[idx]
                changed_keys = [
                    k
                    for k in snap
                    if str(snap.get(k) or "") != str(existing.get(k) or "")
                ]
                if changed_keys:
                    self._orders[idx] = {**existing, **snap}
                    updated.append(self._orders[idx])
            return updated
        except Exception:
            return []

    def _apply_in_memory_payments_changes(self, items: dict) -> list:
        """Merges {oid: snap} dict (diffed by dashboard) into self._orders in-place.
        Returns list of merged order dicts that need widget refresh."""
        if not items:
            return []
        orders_by_id = {
            str((o or {}).get("id") or "").strip(): i
            for i, o in enumerate(self._orders)
            if isinstance(o, dict)
        }
        updated: list = []
        for oid, snap in items.items():
            if not isinstance(snap, dict):
                continue
            idx = orders_by_id.get(str(oid).strip())
            if idx is None:
                continue
            existing = self._orders[idx]
            merged = {**existing, **snap}
            self._orders[idx] = merged
            updated.append(merged)
        return updated

    # ──────────────────── shared orders map helpers ───────────────────────────

    def _sync_order_to_shared_map(self, oid: str, order: dict) -> None:
        """Write a COPY of order to shared map after mutation.
        Using dict() ensures self._orders[i] and shared_map[oid] are never the same
        object — in-place mutations to local list won't bleed into the shared map."""
        if oid and isinstance(order, dict):
            self._shared_orders_map[oid] = dict(order)
            self._bump_sync_seq()

    def _sync_to_shared_map(self) -> None:
        """Populate shared map after bulk load.
        - New entry: store a copy in shared map, keep local reference as-is.
        - Existing entry (another tab already has it): replace local with a copy of
          the shared version so this tab starts with the most recent data."""
        for i, order in enumerate(self._orders):
            if not isinstance(order, dict):
                continue
            oid = str((order.get("id") or "")).strip()
            if not oid:
                continue
            existing = self._shared_orders_map.get(oid)
            if existing is None:
                self._shared_orders_map[oid] = dict(order)
            else:
                self._orders[i] = dict(existing)

    def _sync_from_shared_map(self) -> list:
        """Compare self._orders with shared map. Replace changed local entries
        with a fresh copy from the map and return them for widget refresh."""
        if not self._shared_orders_map:
            return []
        updated: list = []
        for i, local in enumerate(self._orders):
            if not isinstance(local, dict):
                continue
            oid = str((local.get("id") or "")).strip()
            if not oid:
                continue
            shared = self._shared_orders_map.get(oid)
            if shared is None:
                continue
            # Compare only real data fields (skip computed/internal keys starting with "_").
            # Internal fields like _receive_month exist locally but not in shared_map
            # (they're computed per-tab), causing false-positive "changed" detections.
            if any(
                str(shared.get(k) or "") != str(local.get(k) or "")
                for k in local
                if not str(k).startswith("_")
            ):
                self._orders[i] = dict(shared)
                updated.append(shared)
        return updated

    def _bump_sync_seq(self) -> None:
        try:
            seq = int(self._shared_orders_map.get("__sync_seq__", 0) or 0)
            self._shared_orders_map["__sync_seq__"] = seq + 1
            _cb = self._shared_orders_map.get("__on_change__")
            if callable(_cb):
                _cb()
        except Exception:
            pass

    def _bg_apply_sync(self) -> None:
        if not getattr(self, "_orders_ui_ready", False):
            return
        changed = self._sync_from_shared_map()
        if not changed:
            return
        for order in changed:
            with contextlib.suppress(Exception):
                self._refresh_single_order_row(order)
        with contextlib.suppress(Exception):
            self._update_counters()

    async def _bg_sync_loop(self) -> None:
        _last_pulse_check = 0.0
        while True:
            try:
                await asyncio.sleep(1.5)
                seq = int(self._shared_orders_map.get("__sync_seq__", 0) or 0)
                if seq != getattr(self, "_bg_sync_seq", 0):
                    self._bg_sync_seq = seq
                    self._bg_apply_sync()
                # Periodically poll the DB pulse for changes made from another
                # device/session, even while this tab stays open the whole
                # time (not just when the user switches away and back — see
                # _bg_orders_pulse_check docstring for why this exists).
                _now = time.monotonic()
                if self._orders_ui_ready and (_now - _last_pulse_check) >= 20.0:
                    _last_pulse_check = _now
                    await self._bg_orders_pulse_check()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────────────

    def _enqueue_outbox(self, order_row: dict, silent: bool = False) -> None:
        if not isinstance(order_row, dict):
            return
        oid = str(order_row.get("id") or "").strip()
        if not oid:
            return
        self._outbox[oid] = dict(order_row)
        # Order is now queued — no longer "unsaved" (moves to "pending" state).
        self._unsaved_local_order_ids.discard(oid)
        if not silent:
            self._sync_order_to_shared_map(oid, order_row)
        self._schedule_outbox_file_save()
        self._refresh_sync_indicator_state()
        self._start_sync_timer()

    def _dequeue_outbox(self, order_id: str) -> None:
        oid = str(order_id or "").strip()
        if not oid:
            return
        removed = self._outbox.pop(oid, None)
        if removed is not None:
            self._save_outbox_to_file()
        if not self._outbox:
            self._stop_sync_timer()

        self._refresh_sync_indicator_state()

    def _set_order_draft_dirty(self, order_id: str, value: bool) -> None:
        oid = str(order_id or "").strip()
        if not oid:
            return
        if value:
            self._unsaved_local_order_ids.add(oid)
        else:
            self._unsaved_local_order_ids.discard(oid)
        self._refresh_sync_indicator_state()

    def _register_force_sync_hook(self, order_id: str, callback) -> None:
        oid = str(order_id or "").strip()
        if not oid or not callable(callback):
            return
        self._force_sync_hooks[oid] = callback

    def _unregister_force_sync_hook(self, order_id: str) -> None:
        oid = str(order_id or "").strip()
        if not oid:
            return
        self._force_sync_hooks.pop(oid, None)
        self._unsaved_local_order_ids.discard(oid)
        self._refresh_sync_indicator_state()

    def _flush_unsaved_local_drafts(self) -> None:
        if self._unsaved_local_order_ids:
            self._debug_print(
                f"flush unsaved local drafts start ids={len(self._unsaved_local_order_ids)}"
            )
        for oid in list(self._unsaved_local_order_ids):
            hook = self._force_sync_hooks.get(oid)
            if not callable(hook):
                self._debug_print(
                    f"flush unsaved local drafts skipped missing hook for {oid}"
                )
                continue
            with contextlib.suppress(Exception):
                hook()
        self._refresh_sync_indicator_state()
        self._debug_print(
            f"flush unsaved local drafts finished outbox={len(self._outbox)} unsaved={len(self._unsaved_local_order_ids)}"
        )

    def _refresh_sync_indicator_state(self) -> None:
        if self._unsaved_local_order_ids:
            self._set_sync_status("unsaved")
            return
        if self._outbox_flush_running:
            self._set_sync_status("syncing")
            return
        if self._outbox:
            self._set_sync_status("pending")
            return
        self._set_sync_status("synced")

    def _set_sync_status(self, status: str, error_count: int = 0) -> None:
        self._sync_status = str(status or "synced")
        if status == "error":
            self._sync_error_count = max(int(error_count or 0), self._sync_error_count)
        elif status == "synced":
            self._sync_error_count = 0

        txt = self._sync_indicator_text
        dot = self._sync_indicator_dot
        ctr = self._sync_indicator_container
        if txt is None or dot is None or ctr is None:
            return
        if status == "synced":
            txt.value = self.t("orders.sync.synced", "Збережено")
            txt.color = "#059669"
            dot.bgcolor = "#059669"
            ctr.tooltip = self.t("orders.sync.synced_tip", "Дані вже перенесені в БД. Натисніть для примусової повторної синхронізації.")
        elif status == "pending":
            txt.value = self.t("orders.sync.pending", "Локально")
            txt.color = "#B45309"
            dot.bgcolor = "#F59E0B"
            ctr.tooltip = self.t("orders.sync.pending_tip", "Дані збережені в кеші та ще не перенесені в БД. Натисніть для примусової синхронізації.")
        elif status == "syncing":
            txt.value = self.t("orders.sync.syncing", "Оновлення...")
            txt.color = "#1D4ED8"
            dot.bgcolor = "#3B82F6"
            ctr.tooltip = self.t("orders.sync.syncing_tip", "Йде передача локальних змін у БД.")
        elif status == "unsaved":
            txt.value = self.t("orders.sync.unsaved", "Не збережено")
            txt.color = "#DC2626"
            dot.bgcolor = "#DC2626"
            ctr.tooltip = self.t("orders.sync.unsaved_tip", "Є зміни в таблиці, які ще не потрапили навіть у кеш. Натисніть для примусового збереження і синхронізації.")
        elif status == "error":
            txt.value = self.t("orders.sync.error", "Не збережено")
            txt.color = "#DC2626"
            dot.bgcolor = "#DC2626"
            ctr.tooltip = self.t("orders.sync.error_tip", "Зміни залишилися лише локально. Натисніть для повторної передачі в БД.")
        with contextlib.suppress(Exception):
            ctr.update()

    def _build_sync_indicator(self) -> ft.Container:
        self._sync_indicator_dot = ft.Container(
            width=8,
            height=8,
            border_radius=4,
            bgcolor=_BORDER,  # neutral gray until first status set
        )
        self._sync_indicator_text = ft.Text(
            "",
            size=10,
            color=_TEXT,
        )

        def _on_force_sync(_):
            self._flush_unsaved_local_drafts()
            if self._outbox_flush_running:
                self._show_snack(self.t("orders.sync.already_running", "Синхронізація вже виконується"), "#1D4ED8")
                return
            if not self._outbox:
                self._refresh_sync_indicator_state()
                self._show_snack(self.t("orders.sync.no_local_changes", "Немає локальних змін для синхронізації"), "#0F766E")
                return
            page = self._page()
            if page and hasattr(page, "run_task"):
                try:
                    page.run_task(self._force_sync_all_async)
                    return
                except Exception:
                    logger.exception("orders: failed to start force sync task")
            self._set_sync_status("error", 1)
            self._show_snack(self.t("orders.sync.start_failed", "Не вдалося запустити синхронізацію"), "#DC2626")

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
            border=ft.border.all(1, "#E2E8F0"),
            tooltip="Натисніть для примусової синхронізації з БД",
            on_click=_on_force_sync,
            ink=True,
        )
        return self._sync_indicator_container

    async def flush_pending_async(self) -> None:
        if self._outbox:
            with contextlib.suppress(Exception):
                await self._flush_outbox_async()

    async def _flush_outbox_async(self) -> None:
        if self._outbox_flush_running:
            self._debug_print("flush outbox skipped: flush already running")
            return
        if not self._outbox:
            self._debug_print("flush outbox skipped: no pending outbox items")
            return
        self._debug_print(f"flush outbox start count={len(self._outbox)}")
        self._outbox_flush_running = True
        self._set_sync_status("syncing")
        error_count = 0
        flushed_count = 0
        start = time.monotonic()
        try:
            oids = list(self._outbox.keys())

            async def _flush_one(oid: str):
                snapshot = self._outbox.get(oid)
                if not isinstance(snapshot, dict):
                    self._debug_print(
                        f"flush outbox removing invalid snapshot for {oid}"
                    )
                    self._outbox.pop(oid, None)
                    return True
                try:
                    if snapshot.get("_pending_delete"):
                        # This is a queued deletion — call DB delete instead of upsert
                        ok = await asyncio.to_thread(
                            self.db.delete_order, oid, self.manager_id
                        )
                        self._debug_print(f"flush outbox DELETE order {oid} ok={ok}")
                    else:
                        ok = await asyncio.to_thread(
                            self.db.upsert_order, snapshot, self.manager_id
                        )
                        self._debug_print(f"flush outbox UPSERT order {oid} ok={ok}")
                except Exception as ex:
                    ok = False
                    self._debug_print(f"flush outbox order {oid} failed: {ex}")
                if ok:
                    self._outbox.pop(oid, None)
                else:
                    self._debug_print(
                        f"flush outbox order {oid} failed, keeping in outbox"
                    )
                return ok

            # Sequential, not asyncio.gather: self.db keeps a single shared
            # psycopg connection per company, reused by every DB call. Two
            # _flush_one() calls racing on that same connection from two
            # different asyncio.to_thread() threads is exactly what left the
            # sync indicator stuck on "Оновлення..." forever after adding two
            # new orders at once — libpq isn't safe for concurrent use from
            # multiple threads on one connection, so one thread just hangs,
            # asyncio.gather() never returns, and this function's own
            # `finally` (which clears _outbox_flush_running) never runs.
            results = []
            for oid in oids:
                try:
                    results.append(await _flush_one(oid))
                except Exception as ex:
                    results.append(ex)
            flushed_count = sum(1 for r in results if r is True)
            error_count = sum(
                1 for r in results if r is not True and not (isinstance(r, bool) and r)
            )

            if self._outbox:
                self._save_outbox_to_file()
                self._debug_print(
                    f"flush outbox partial complete left={len(self._outbox)} items"
                )
            else:
                with contextlib.suppress(Exception):
                    p = self._outbox_file_path()
                    if os.path.exists(p):
                        os.remove(p)
                self._stop_sync_timer()
                self._debug_print("flush outbox complete all items removed")

            if error_count > 0:
                self._set_sync_status("error", error_count)
            else:
                self._refresh_sync_indicator_state()

            # Advance the cached pulse so the next resume() pulse check doesn't
            # treat our own DB writes as "new remote data" and trigger a full reload.
            if flushed_count > 0 and not self._outbox:
                try:
                    new_pulse = await asyncio.to_thread(self._orders_scope_pulse_ts)
                    if new_pulse > self._orders_snapshot_pulse_ts:
                        self._orders_snapshot_pulse_ts = new_pulse
                    # Tell all tabs: this pulse increase is from our own session.
                    with contextlib.suppress(Exception):
                        self._shared_orders_map["__session_db_pulse__"] = new_pulse
                except Exception:
                    pass
        finally:
            self._outbox_flush_running = False
            self._refresh_sync_indicator_state()
            self._debug_print(
                f"flush outbox finished elapsed={time.monotonic() - start:.3f}s flushed={flushed_count} errors={error_count} remaining={len(self._outbox)}"
            )

    async def _force_sync_all_async(self) -> None:
        await asyncio.sleep(0)
        self._set_sync_status("syncing")
        self._flush_unsaved_local_drafts()
        if self._outbox:
            await self._flush_outbox_async()
        else:
            self._refresh_sync_indicator_state()

    async def _wait_until_order_synced(
        self,
        order_id: str,
        timeout_sec: float = 90.0,
        retry_delay_sec: float = 0.35,
    ) -> bool:
        oid = str(order_id or "").strip()
        if not oid:
            return False
        started = time.monotonic()
        while True:
            if oid not in self._outbox and (not bool(self._outbox_flush_running)):
                return True
            await self._flush_outbox_async()
            if oid not in self._outbox and (not bool(self._outbox_flush_running)):
                return True
            if float(timeout_sec or 0.0) > 0 and (time.monotonic() - started) >= float(
                timeout_sec
            ):
                return False
            await asyncio.sleep(max(0.05, float(retry_delay_sec or 0.1)))

    async def _sync_timer_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(45)
                self._debug_print(
                    "sync timer tick: flush unsaved drafts and outbox if pending"
                )
                self._flush_unsaved_local_drafts()
                if self._outbox:
                    self._debug_print(
                        f"sync timer flushing outbox {len(self._outbox)} items"
                    )
                    await self._flush_outbox_async()
                    self._debug_print("sync timer outbox flush complete")
                else:
                    self._debug_print("sync timer no outbox items to flush")
        except asyncio.CancelledError:
            self._debug_print("sync timer cancelled")
            pass
        except Exception as ex:
            self._debug_print(f"sync timer loop error: {ex}")
            pass

    def _start_sync_timer(self) -> None:
        if self._sync_timer_task is not None:
            return
        page = self._page()
        if page and hasattr(page, "run_task"):
            self._debug_print("starting sync timer")
            with contextlib.suppress(Exception):
                self._sync_timer_task = page.run_task(self._sync_timer_loop)

    def _stop_sync_timer(self) -> None:
        if self._sync_timer_task is None:
            return
        with contextlib.suppress(Exception):
            self._sync_timer_task.cancel()
        self._sync_timer_task = None

    async def _background_sync_on_leave(self) -> None:
        prev_flag = getattr(self, "_no_db_after_ready", False)
        try:
            self._no_db_after_ready = False
            with contextlib.suppress(Exception):
                self._flush_unsaved_local_drafts()

            try:
                await asyncio.to_thread(
                    self._save_orders_snapshot_cache, self._orders_snapshot_pulse_ts
                )
            except Exception:
                pass

            try:
                if self._outbox:
                    await self._flush_outbox_async()
            except Exception:
                pass

        finally:
            with contextlib.suppress(Exception):
                self._no_db_after_ready = bool(prev_flag)

    def stop(self) -> None:
        page = self._page()
        _slot = getattr(page, "_view_h_scrollbar_slot", None) if page else None
        if _slot is not None:
            with contextlib.suppress(Exception):
                _slot.controls.clear()
                _slot.update()
        self._pinned_h_scrollbar = None
        if page and hasattr(page, "run_task"):
            try:
                page.run_task(self._background_sync_on_leave)
            except Exception:
                try:
                    page.run_task(
                        lambda: asyncio.to_thread(
                            self._save_orders_snapshot_cache,
                            self._orders_snapshot_pulse_ts,
                        )
                    )
                except Exception:
                    with contextlib.suppress(Exception):
                        self._save_orders_snapshot_cache(self._orders_snapshot_pulse_ts)
        else:
            with contextlib.suppress(Exception):
                self._save_orders_snapshot_cache(self._orders_snapshot_pulse_ts)
        page = page or self._page()
        if page:
            with contextlib.suppress(Exception):
                if getattr(self, "_shared_date_picker", None) in (page.overlay or []):
                    page.overlay.remove(self._shared_date_picker)
            with contextlib.suppress(Exception):
                if self._loading_box in (page.overlay or []):
                    page.overlay.remove(self._loading_box)
        if self._initial_load_task is not None:
            with contextlib.suppress(Exception):
                self._initial_load_task.cancel()
            self._initial_load_task = None
        if self._orders_live_sync_task is not None:
            with contextlib.suppress(Exception):
                self._orders_live_sync_task.cancel()
            self._orders_live_sync_task = None
        if self._sync_timer_task is not None:
            with contextlib.suppress(Exception):
                self._sync_timer_task.cancel()
            self._sync_timer_task = None
        if self._outbox_fast_flush_task is not None:
            with contextlib.suppress(Exception):
                self._outbox_fast_flush_task.cancel()
            self._outbox_fast_flush_task = None
        if self._outbox_file_save_task is not None:
            with contextlib.suppress(Exception):
                self._outbox_file_save_task.cancel()
            self._outbox_file_save_task = None
        if self._outbox_file_save_dirty:
            try:
                if page and hasattr(page, "run_task"):
                    page.run_task(lambda: asyncio.to_thread(self._save_outbox_to_file))
                else:
                    self._save_outbox_to_file()
            finally:
                self._outbox_file_save_dirty = False
        if self._outbox:
            try:
                if page and hasattr(page, "run_task"):
                    page.run_task(lambda: asyncio.to_thread(self._save_outbox_to_file))
                else:
                    self._save_outbox_to_file()
            except Exception:
                with contextlib.suppress(Exception):
                    self._save_outbox_to_file()

    async def resume(self) -> None:
        """Called when returning to this cached tab. Restores sticky header and syncs if data changed."""
        if not self._orders_ui_ready:
            return
        # Restore sticky header into the page slot
        page = self._page()
        _sticky = getattr(page, "_view_sticky_header", None) if page else None
        if _sticky is not None and getattr(page, "_active_view", None) == "show_orders":
            _hr = getattr(self, "_sticky_header_row", None)
            _cb = getattr(self, "_sticky_counters_bar", None)
            if _hr is not None and _cb is not None:
                _sticky.controls[:] = [_hr, _cb]
                with contextlib.suppress(Exception):
                    _sticky.update()
        # Clear pinned scrollbar slot if left over from a previous view
        _slot = getattr(page, "_view_h_scrollbar_slot", None) if page else None
        if _slot is not None:
            with contextlib.suppress(Exception):
                if _slot.controls:
                    _slot.controls.clear()
                    _slot.update()
        # Refresh counters from current in-memory data immediately (before any async work)
        with contextlib.suppress(Exception):
            self._update_counters()
        # Sync cross-tab changes and check DB pulse
        try:
            # Sync changes from shared map (written by payments_view or any other tab)
            cross_tab_orders = self._sync_from_shared_map()

            if cross_tab_orders:
                updated_count = 0
                for order in cross_tab_orders:
                    with contextlib.suppress(Exception):
                        ok = self._refresh_single_order_row(order)
                        if ok:
                            updated_count += 1
                if updated_count == 0:
                    self._schedule_full_rerender()
                with contextlib.suppress(Exception):
                    self._update_counters()
        except Exception:
            pass

        _run_pg = self._page()
        if _run_pg and hasattr(_run_pg, "run_task"):
            with contextlib.suppress(Exception):
                _run_pg.run_task(self._bg_orders_pulse_check)

    async def _bg_orders_pulse_check(self) -> None:
        """DB pulse check — if data changed from an EXTERNAL source (another
        session/device), trigger full reload. Changes from our own session
        are already in shared_orders_map and handled by _sync_from_shared_map.

        Called both right after resume() (tab switch) AND periodically from
        _bg_sync_loop (~every 20s) so a change made on another device shows
        up here even if we never switch away from this tab — see the
        _bg_sync_loop call site for why the periodic call was added."""
        try:
            new_pulse = await asyncio.to_thread(self._orders_scope_pulse_ts)
            cached_pulse = int(getattr(self, "_orders_snapshot_pulse_ts", 0) or 0)
            if new_pulse > cached_pulse:
                # Check if this increase is from our own session's DB writes.
                session_db_pulse = int(
                    (self._shared_orders_map or {}).get("__session_db_pulse__", 0)
                    or 0
                )
                if new_pulse <= session_db_pulse:
                    # Our own write — update cached pulse, no reload needed.
                    self._orders_snapshot_pulse_ts = new_pulse
                    return
                # Skip reload while this session has unsaved local edits —
                # either mid-flush (race window) or queued but not yet
                # flushed (self._outbox non-empty). A full reload here
                # would clobber those in-progress edits. Retried on the
                # next pulse tick once the outbox is empty again.
                if self._outbox_flush_running or self._outbox:
                    return
                # Extra grace window right after a fast-path cell edit
                # (_commit_date_fast/_commit_item_date_fast, etc.): the DB
                # write can land and bump the real pulse a moment before
                # __session_db_pulse__ is updated to match (that only
                # happens once the WHOLE outbox flush finishes). Without
                # this, that tiny gap could be misread as an external
                # change and trigger a jarring full-page reload for an
                # edit the user just made themselves.
                last_local_edit = float(
                    getattr(self, "_last_local_fast_edit_ts", 0.0) or 0.0
                )
                if time.time() - last_local_edit < 3.0:
                    return
                # Genuine external change — clear snapshot so next load fetches DB.
                region_id = self._effective_region_id()
                _vkey = f"_orders_cache_session_verified_{region_id or 'all'}"
                self.user_data.pop(_vkey, None)
                self.user_data.pop(
                    f"_orders_cache_is_verifying_{region_id or 'all'}", None
                )
                _pg = self._page()
                if _pg and hasattr(_pg, "run_task"):
                    with contextlib.suppress(Exception):
                        _pg.run_task(self._async_initial_load)
                return
            # Orders pulse unchanged — check if reference data changed.
            ref_pulse = await asyncio.to_thread(
                self.db.get_reference_data_pulse, self.company_id
            )
            if ref_pulse > self._ref_data_pulse_ts:
                await asyncio.to_thread(self._load_lookups, True)
                self._ref_data_pulse_ts = ref_pulse
                self._schedule_full_rerender()
        except Exception:
            pass

    def _is_order_owned_by_current_manager(self, row: dict) -> bool:
        if not isinstance(row, dict):
            return False
        row_mgr = (row.get("manager_id") or "").strip()
        if self._general_mode:
            owned = row_mgr in set(self._general_manager_ids or [])
            if owned:
                # Stamp manager info for grouping in render
                row["_gm_manager_id"] = row_mgr
                row["_gm_manager_name"] = (self._general_manager_names or {}).get(
                    row_mgr, row_mgr
                )
            return owned
        if row_mgr == (self.manager_id or "").strip():
            return True
        oid = str(row.get("id") or "").strip()
        return bool(oid and oid in (self._owned_order_ids or set()))

    def _refresh_owned_order_ids(self):
        try:
            self._owned_order_ids = set(
                self.db.get_entity_owner_entity_ids(
                    self.company_id, self.manager_id, "order"
                )
                or []
            )
        except Exception:
            self._owned_order_ids = set()

    def _load_orders(self, preload_all: bool = False, force_refresh: bool = False):
        self._debug_print(
            f"load orders start preload_all={preload_all} force_refresh={force_refresh}"
        )
        start = time.monotonic()
        self._orders = []
        self._orders_offset = 0
        self._orders_has_more = False
        self._refresh_owned_order_ids()
        if preload_all:
            # Use a single full-scan query (no LIMIT/OFFSET) to avoid pagination
            # cursor drift: when updated_at changes between batches, OFFSET-based
            # pagination can skip or duplicate rows, causing count mismatches.
            region_id = self._effective_region_id()
            _vpc = self._visible_product_ids_cache
            visible_product_ids = (
                list(_vpc)
                if isinstance(_vpc, (list, set, tuple)) and len(_vpc) > 0
                else None
            )
            products_by_id = {
                str((p or {}).get("id") or "").strip(): p
                for p in (self._products or [])
                if isinstance(p, dict) and (p.get("id") or "")
            }
            delivery_by_id = {
                str((d or {}).get("id") or "").strip(): d
                for d in (self._delivery_methods or [])
                if isinstance(d, dict) and (d.get("id") or "")
            }
            all_rows = (
                self.db.get_orders(
                    self.company_id,
                    region_id,
                    force_refresh=force_refresh,
                    manager_id=self.manager_id if not self._general_mode else None,
                    visible_product_ids=visible_product_ids,
                )
                or []
            )
            _preload_seen: set = set()
            _local_preload: list = []
            for row in all_rows:
                if not self._is_order_visible(row):
                    continue
                oid = (row.get("id") or "").strip()
                if not oid or oid in _preload_seen:
                    continue
                _preload_seen.add(oid)
                with contextlib.suppress(Exception):
                    row["_receive_month"] = self._order_receive_month_key(
                        row,
                        products_by_id=products_by_id,
                        delivery_by_id=delivery_by_id,
                    )
                _local_preload.append(row)
            _local_preload.sort(key=_order_row_sort_key)
            self._orders = _local_preload
            self._orders_offset = len(self._orders)
            self._orders_has_more = False
        else:
            self._load_more_orders(reset=True, force_refresh=force_refresh)
        self._debug_print(
            f"load orders finished total={len(self._orders)} has_more={self._orders_has_more} in {time.monotonic() - start:.3f}s"
        )

    def _is_order_visible(self, row: dict) -> bool:
        if not isinstance(row, dict):
            return False
        if self._is_order_owned_by_current_manager(row):
            return True
        visible_product_ids = self._visible_product_ids_cache
        items = row.get("items") or []
        return any(
            isinstance(it, dict)
            and ((it.get("product_id") or "").strip() in visible_product_ids)
            for it in items
        )

    def _load_more_orders(self, reset=False, force_refresh: bool = False):
        if self._orders_loading:
            return
        start = time.monotonic()
        self._debug_print(
            f"load more orders start reset={reset} force_refresh={force_refresh}"
        )
        self._orders_loading = True

        products_by_id = {
            str((p or {}).get("id") or "").strip(): p
            for p in (self._products or [])
            if isinstance(p, dict) and (p.get("id") or "")
        }
        delivery_by_id = {
            str((d or {}).get("id") or "").strip(): d
            for d in (self._delivery_methods or [])
            if isinstance(d, dict) and (d.get("id") or "")
        }

        if reset:
            self._orders = []
            self._orders_offset = 0
            self._supplier_section_controls = {}

        loaded_visible: list = []
        fetched_any = False
        fetched_batches = 0
        region_id = self._effective_region_id()
        # Pass None (no SQL filter) when cache is empty — avoids "return []" shortcut
        # in get_orders for managers whose products haven't loaded yet or have no restrictions.
        _vpc = self._visible_product_ids_cache
        visible_product_ids = (
            list(_vpc)
            if isinstance(_vpc, (list, set, tuple)) and len(_vpc) > 0
            else None
        )
        try:
            while len(loaded_visible) < self._orders_page_size:
                if reset and fetched_batches >= int(
                    self._max_initial_fetch_batches or 3
                ):
                    self._orders_has_more = True
                    break
                batch = (
                    self.db.get_orders(
                        self.company_id,
                        region_id,
                        force_refresh=force_refresh,
                        limit=self._orders_fetch_batch_size,
                        offset=self._orders_offset,
                        manager_id=self.manager_id if not self._general_mode else None,
                        visible_product_ids=visible_product_ids,
                    )
                    or []
                )
                force_refresh = False  # only bust cache on first batch
                fetched_batches += 1
                batch_count = len(batch)
                self._debug_print(
                    f"orders batch {fetched_batches} fetched {batch_count} rows offset={self._orders_offset}"
                )
                if batch_count <= 0:
                    self._orders_has_more = False
                    break
                fetched_any = True
                self._orders_offset += batch_count
                visible_batch = [row for row in batch if self._is_order_visible(row)]
                loaded_visible.extend(visible_batch)
                if batch_count < self._orders_fetch_batch_size:
                    self._orders_has_more = False
                    break
                self._orders_has_more = True

            if loaded_visible:
                known_ids = {
                    (row.get("id") or "").strip()
                    for row in self._orders
                    if isinstance(row, dict)
                }
                for row in loaded_visible:
                    oid = (row.get("id") or "").strip()
                    if oid and oid not in known_ids:
                        try:
                            row["_receive_month"] = self._order_receive_month_key(
                                row,
                                products_by_id=products_by_id,
                                delivery_by_id=delivery_by_id,
                            )
                        except Exception:
                            pass
                        self._orders.append(row)

                self._orders.sort(key=_order_row_sort_key)
                self._sync_to_shared_map()
            elif reset and not fetched_any:
                legacy_rows = self._read_legacy_rows()
                self._orders = [
                    r
                    for r in legacy_rows
                    if isinstance(r, dict)
                    and (r.get("company_id") or "").strip() == self.company_id
                    and (
                        not region_id or (r.get("region_id") or "").strip() == region_id
                    )
                ]

                self._orders.sort(key=_order_row_sort_key)
                self._sync_to_shared_map()
        finally:
            self._orders_loading = False
            self._update_load_more_button()
            self._debug_print(
                f"load more orders finished loaded_visible={len(loaded_visible)} total_orders={len(self._orders)} has_more={self._orders_has_more} in {time.monotonic() - start:.3f}s"
            )

    def _merge_local_order(self, order_row: dict):
        if not isinstance(order_row, dict):
            return
        oid = (order_row.get("id") or "").strip()
        if not oid:
            return

        self._orders_cache[oid] = dict(order_row)

        found_idx = -1
        old_row = None
        for idx, row in enumerate(self._orders):
            if isinstance(row, dict) and (row.get("id") or "").strip() == oid:
                found_idx = idx
                old_row = row
                break

        new_visible = self._is_order_visible(order_row)
        old_visible = (
            self._is_order_visible(old_row) if isinstance(old_row, dict) else False
        )
        old_status = str((old_row or {}).get("status") or "production").strip()
        new_status = str(order_row.get("status") or "production").strip()
        old_payment = str((old_row or {}).get("payment_status") or "unpaid").strip()
        new_payment = str(order_row.get("payment_status") or "unpaid").strip()
        old_submission = str(
            (old_row or {}).get("submission_status") or "not_submitted"
        ).strip()
        new_submission = str(
            order_row.get("submission_status") or "not_submitted"
        ).strip()
        schedule_counter_refresh = False
        if found_idx < 0:
            schedule_counter_refresh = new_visible
        elif (
            new_visible != old_visible
            or old_status != new_status
            or old_payment != new_payment
            or old_submission != new_submission
        ):
            schedule_counter_refresh = True
        needs_sort = False

        if found_idx >= 0:
            if new_visible:
                self._orders[found_idx] = order_row
                self._sync_order_to_shared_map(oid, order_row)
                old_date = str((old_row or {}).get("order_date") or "")[:10]
                new_date = str(order_row.get("order_date") or "")[:10]
                old_inv = str((old_row or {}).get("invoice_no") or "")
                new_inv = str(order_row.get("invoice_no") or "")
                if old_date != new_date or old_inv != new_inv:
                    needs_sort = True
            else:
                if old_visible:
                    self._orders.pop(found_idx)
        else:
            if new_visible:
                self._orders.append(order_row)
                self._sync_order_to_shared_map(oid, order_row)
                needs_sort = True
                self._session_new_orders[oid] = dict(order_row)
                if len(self._session_new_orders) > 200:
                    self._session_new_orders.pop(next(iter(self._session_new_orders)))

        if needs_sort:
            self._orders.sort(key=_order_row_sort_key)

        if schedule_counter_refresh:
            supplier_id = str((order_row.get("supplier_id") or "") or "__no_supplier__")
            self._schedule_group_counter_refresh({supplier_id})
            with contextlib.suppress(Exception):
                self._collapse_group_if_fully_accepted(
                    affected_supplier_ids={supplier_id}
                )

        self._schedule_orders_snapshot_save()

    def _reapply_session_new_orders(self) -> None:
        """Safety net for _async_initial_load: whatever the exact reason a
        stale snapshot / stray full reload might miss a row (see
        _session_new_orders' docstring at __init__), force every order
        created THIS session back into self._orders if it's not already
        there, right before rendering — so it never looks like it "vanished"
        without a full app restart."""
        if not self._session_new_orders:
            return
        existing_ids = {
            str((row or {}).get("id") or "").strip() for row in (self._orders or [])
        }
        for oid, row in self._session_new_orders.items():
            if oid not in existing_ids:
                self._merge_local_order(dict(row))

    def _schedule_group_counter_refresh(self, affected_supplier_ids=None) -> None:
        affected = {
            str(sid or "").strip() or "__no_supplier__"
            for sid in (affected_supplier_ids or [])
            if sid is not None
        }
        if not affected:
            return
        self._pending_group_counter_supplier_ids.update(affected)
        if self._group_counter_refresh_scheduled:
            return

        page = self._page()
        if not page or not hasattr(page, "run_task"):
            self._refresh_visible_group_counters(
                affected_supplier_ids=self._pending_group_counter_supplier_ids
            )
            self._pending_group_counter_supplier_ids.clear()
            return

        self._group_counter_refresh_scheduled = True

        async def _deferred_refresh():
            try:
                await asyncio.sleep(0.06)
                target = set(self._pending_group_counter_supplier_ids)
                self._pending_group_counter_supplier_ids.clear()
                if target:
                    self._refresh_visible_group_counters(affected_supplier_ids=target)
            finally:
                self._group_counter_refresh_scheduled = False

        with contextlib.suppress(Exception):
            page.run_task(_deferred_refresh)

    def _refresh_visible_group_counters(self, affected_supplier_ids=None) -> None:
        if self._has_active_filters():
            return

        affected = {
            str(sid or "").strip() or "__no_supplier__"
            for sid in (affected_supplier_ids or [])
            if sid is not None
        }

        filtered = self._filtered_orders()
        supplier_map: dict[str, list] = {}
        for row in filtered or []:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("supplier_id") or "").strip() or "__no_supplier__"
            if affected and sid not in affected:
                continue
            supplier_map.setdefault(sid, []).append(row)

        def _apply_counter_row(target_row: ft.Row, rows: list):
            if target_row is None:
                return
            new_row = self._counter_chips(rows)
            target_row.controls = list(new_row.controls)
            with contextlib.suppress(Exception):
                target_row.update()

        for sid, sup_orders in supplier_map.items():
            sup_counter = self._supplier_counter_row_by_key.get(sid)
            _apply_counter_row(sup_counter, sup_orders)

            year_map: dict[str, list] = {}
            for row in sup_orders:
                year = str((row.get("order_date") or "")[:4] or "?")
                year_map.setdefault(year, []).append(row)

            for year, year_orders in year_map.items():
                year_key = f"{sid}:{year}"
                year_counter = self._year_counter_row_by_key.get(year_key)
                _apply_counter_row(year_counter, year_orders)

                month_map: dict[int, list] = {}
                for row in year_orders:
                    try:
                        m = int(str((row.get("order_date") or "")[5:7] or "0"))
                    except Exception:
                        m = 0
                    month_map.setdefault(m, []).append(row)

                for month_num, month_orders in month_map.items():
                    month_key = f"{sid}:{year}:{int(month_num)}"
                    month_counter = self._month_counter_row_by_key.get(month_key)
                    _apply_counter_row(month_counter, month_orders)

    def _schedule_orders_snapshot_save(self) -> None:
        if getattr(self, "_orders_snapshot_save_scheduled", False):
            return
        page = self._page()
        if not page or not hasattr(page, "run_task"):
            self._save_orders_snapshot_cache(self._orders_snapshot_pulse_ts)
            return

        self._orders_snapshot_save_scheduled = True

        async def _save_snapshot_debounced():
            try:
                await asyncio.sleep(0.12)
                self._save_orders_snapshot_cache(self._orders_snapshot_pulse_ts)
            finally:
                self._orders_snapshot_save_scheduled = False

        with contextlib.suppress(Exception):
            page.run_task(_save_snapshot_debounced)

    def _is_order_accepted(self, order_row: dict) -> bool:
        return (
            str((order_row or {}).get("status") or "production").strip() == "accepted"
        )

    def _collapse_group_if_fully_accepted(self, affected_supplier_ids=None) -> None:
        if self._has_active_filters():
            return
        if getattr(self, "_dialog_editing_order_id", "") or (
            getattr(self, "_unsaved_local_order_ids", None) or set()
        ):
            return

        affected = {
            str(sid or "").strip() or "__no_supplier__"
            for sid in (affected_supplier_ids or [])
            if sid is not None
        }
        filtered = self._filtered_orders() or []
        supplier_map: dict[str, list] = {}
        for row in filtered:
            if not isinstance(row, dict):
                continue
            sid = str((row.get("supplier_id") or "").strip()) or "__no_supplier__"
            if affected and sid not in affected:
                continue
            supplier_map.setdefault(sid, []).append(row)

        for sid, sup_orders in supplier_map.items():
            supplier_all_accepted = True
            year_map: dict[str, list] = {}
            for row in sup_orders:
                year = str((row.get("order_date") or "")[:4] or "?")
                year_map.setdefault(year, []).append(row)

            for year, year_orders in year_map.items():
                year_all_accepted = all(self._is_order_accepted(o) for o in year_orders)
                month_map: dict[int, list] = {}
                for row in year_orders:
                    try:
                        m = int(str((row.get("order_date") or "")[5:7] or "0"))
                    except Exception:
                        m = 0
                    month_map.setdefault(m, []).append(row)

                for month_num, month_orders in month_map.items():
                    if not all(self._is_order_accepted(o) for o in month_orders):
                        year_all_accepted = False
                        continue
                    month_key = f"{sid}:{year}:{int(month_num)}"
                    month_section = self._month_collapsible_by_key.get(month_key)
                    if month_section is not None and getattr(
                        month_section, "_is_open", False
                    ):
                        with contextlib.suppress(Exception):
                            month_section._collapse()

                if year_all_accepted:
                    year_key = f"{sid}:{year}"
                    year_section = self._year_collapsible_by_key.get(year_key)
                    if year_section is not None and getattr(
                        year_section, "_is_open", False
                    ):
                        with contextlib.suppress(Exception):
                            year_section._collapse()
                else:
                    supplier_all_accepted = False

            if supplier_all_accepted:
                supplier_section = self._supplier_collapsible_by_key.get(sid)
                if supplier_section is not None and getattr(
                    supplier_section, "_is_open", False
                ):
                    with contextlib.suppress(Exception):
                        supplier_section._collapse()

    def _reposition_order_row_in_month(self, order_row: dict) -> bool:
        if self._has_active_filters():
            return False
        if not isinstance(order_row, dict):
            return False
        oid = (order_row.get("id") or "").strip()
        if not oid:
            return False

        supplier_id, year, month = self._order_group_key(order_row)
        if not year or not month:
            return False
        try:
            month_num = int(month)
        except Exception:
            return False
        month_key = f"{supplier_id}:{year}:{month_num}"
        rows_col = self._order_month_rows_col.get(month_key)
        if rows_col is None:
            return False

        target = self._order_row_controls.get(oid)
        if target is None or target not in rows_col.controls:
            return False

        current_index = rows_col.controls.index(target)
        new_key = _order_row_sort_key(order_row)
        order_by_id = {
            str((o.get("id") or "")).strip(): o
            for o in self._orders
            if isinstance(o, dict)
        }

        other_controls = [ctrl for ctrl in rows_col.controls if ctrl is not target]
        insert_pos = 0
        for ctrl in other_controls:
            ctrl_id = str((getattr(ctrl, "key", None) or "")).strip()
            existing_order = order_by_id.get(ctrl_id) or self._orders_cache.get(ctrl_id)
            if not isinstance(existing_order, dict):
                return False
            if new_key < _order_row_sort_key(existing_order):
                break
            insert_pos += 1

        if insert_pos == current_index:
            return False

        rows_col.controls.pop(current_index)
        rows_col.controls.insert(
            max(0, min(insert_pos, len(rows_col.controls))), target
        )
        with contextlib.suppress(Exception):
            rows_col.update()
        return True

    def _order_group_key(self, order_row: dict):
        if not isinstance(order_row, dict):
            return ("__no_supplier__", "", "")
        supplier_id = (order_row.get("supplier_id") or "") or "__no_supplier__"
        order_date = (order_row.get("order_date") or "")[:10]
        year = order_date[:4] if len(order_date) >= 4 else ""
        month = order_date[5:7] if len(order_date) >= 7 else ""
        return supplier_id, year, month

    def _can_refresh_single_order_row(self, old_row: dict, new_row: dict) -> bool:
        if not isinstance(old_row, dict) or not isinstance(new_row, dict):
            return False
        oid = (new_row.get("id") or "").strip()
        if not oid or oid not in self._order_row_controls:
            return False
        if not self._is_order_visible(new_row):
            return False
        if self._has_active_filters():
            # Flat filtered view: row newly became visible → need full rebuild to insert it
            if not self._is_order_visible(old_row):
                return False
        else:
            # Tree view: group key change requires moving row between month containers
            if self._order_group_key(old_row) != self._order_group_key(new_row):
                return False
        return True

    def _inject_new_order_to_tree(
        self, new_order: dict, skip_update: bool = False
    ) -> bool:
        supplier_id = (new_order.get("supplier_id") or "").strip() or "__no_supplier__"
        od = (new_order.get("order_date") or "")[:10]
        if not od or len(od) < 7:
            return False
        year = od[:4]
        try:
            month_num = int(od[5:7])
        except Exception:
            return False

        month_key = f"{supplier_id}:{year}:{month_num}"
        year_key = f"{supplier_id}:{year}"
        page = self._page()

        rows_col = self._order_month_rows_col.get(month_key)
        if rows_col is not None:
            specs = self._table_col_specs()
            new_key = _order_row_sort_key(new_order)
            insert_pos = 0
            order_by_id = {
                str((o.get("id") or "")).strip(): o
                for o in self._orders
                if isinstance(o, dict)
            }
            for idx, ctrl in enumerate(rows_col.controls):
                ctrl_id = str((getattr(ctrl, "key", None) or "")).strip()
                existing_order = order_by_id.get(ctrl_id) or self._orders_cache.get(
                    ctrl_id
                )
                if not isinstance(existing_order, dict):
                    continue
                if new_key < _order_row_sort_key(existing_order):
                    insert_pos = idx
                    break
                insert_pos = idx + 1
            new_row = self._build_order_row(new_order, specs=specs, row_idx=insert_pos)
            rows_col.controls.insert(
                max(0, min(insert_pos, len(rows_col.controls))), new_row
            )
            if not skip_update and page:
                with contextlib.suppress(Exception):
                    rows_col.update()
            return True

        year_body_col = self._order_year_body_col.get(year_key)
        month_order = self._order_year_month_numbers.get(year_key)
        if year_body_col is not None and isinstance(month_order, list):
            month_name = self.MONTH_NAMES.get(month_num, str(month_num))
            month_already_exists = month_num in month_order
            if month_already_exists:
                # Month section exists but is lazy (body not yet built in Flutter).
                # Gather ALL current orders for this month (new_order is already in
                # self._orders via _merge_local_order) and build a complete section to
                # REPLACE the old lazy one — avoids duplicate month sections in the tree.
                try:
                    _month_orders = sorted(
                        [
                            o
                            for o in (self._orders or [])
                            if isinstance(o, dict)
                            and (
                                str((o.get("supplier_id") or "").strip())
                                or "__no_supplier__"
                            )
                            == supplier_id
                            and str((o.get("order_date") or "")[:4]) == year
                            and int((o.get("order_date") or "0000-00")[5:7] or "0")
                            == month_num
                        ],
                        key=_order_row_sort_key,
                    )
                except Exception:
                    _month_orders = [new_order]
                insert_pos = month_order.index(month_num)
            else:
                _month_orders = [new_order]
                insert_pos = next(
                    (i for i, m in enumerate(month_order) if int(month_num) > int(m)),
                    len(month_order),
                )
            table = self._build_orders_table(_month_orders, table_key=month_key)
            month_counter_row = self._counter_chips(_month_orders)
            self._month_counter_row_by_key[month_key] = month_counter_row
            month_section = self._make_collapsible(
                ft.Row(
                    [
                        ft.Icon(
                            ft.Icons.CALENDAR_MONTH_OUTLINED,
                            size=14,
                            color=_TEXT,
                        ),
                        ft.Text(
                            f"{month_name} {year}",
                            size=12,
                            weight="w600",
                            color=_TEXT,
                        ),
                        month_counter_row,
                    ],
                    spacing=6,
                    wrap=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                [table],
                initially_open=True,
                bg_header=_SOFT_I,
                border_color=_BORDER,
                label=f"Місяць {month_name} {year}",
            )
            self._month_collapsible_by_key[month_key] = month_section
            insert_pos = max(0, min(int(insert_pos), len(year_body_col.controls)))
            if month_already_exists:
                # Replace the old lazy section in-place; avoid shifting indices.
                if insert_pos < len(year_body_col.controls):
                    year_body_col.controls[insert_pos] = month_section
                else:
                    year_body_col.controls.append(month_section)
            else:
                year_body_col.controls.insert(insert_pos, month_section)
                month_order.insert(insert_pos, month_num)
            self._order_year_month_numbers[year_key] = month_order
            if not skip_update and page:
                with contextlib.suppress(Exception):
                    year_body_col.update()
            return True

        return False

    def _remove_order_from_tree(self, order_id: str, order_data: dict) -> bool:
        """Remove one order row from the UI tree without rebuilding the whole section.
        Returns True if the row was found and visually removed from the visible tree.
        Falls back to _render_tree if the row is not in the visible month column."""
        oid = (order_id or "").strip()
        if not oid:
            return False
        target = self._order_row_controls.get(oid)
        if target is None:
            return False
        od = str((order_data or {}).get("order_date") or "")[:10]
        if not od or len(od) < 7:
            return False
        year = od[:4]
        try:
            month_num = int(od[5:7])
        except Exception:
            return False
        supplier_id = (
            str((order_data or {}).get("supplier_id") or "").strip()
            or "__no_supplier__"
        )
        month_key = f"{supplier_id}:{year}:{month_num}"
        rows_col = self._order_month_rows_col.get(month_key)
        if rows_col is None:
            return False
        try:
            if target in rows_col.controls:
                rows_col.controls.remove(target)
            self._order_row_controls.pop(oid, None)
            self._order_fast_refresh.pop(oid, None)
            page = self._page()
            if page:
                with contextlib.suppress(Exception):
                    rows_col.update()
            return True
        except Exception:
            return False

    def _refresh_single_order_row(self, order_row: dict) -> bool:
        oid = ((order_row or {}).get("id") or "").strip()
        target = self._order_row_controls.get(oid)
        if not oid or target is None:
            return False
        replacement = self._build_order_row(order_row)
        self._order_row_controls[oid] = target
        target.content = replacement.content
        target.border = replacement.border
        target.border_radius = replacement.border_radius
        target.bgcolor = replacement.bgcolor
        target.padding = replacement.padding
        target.margin = replacement.margin
        page = self._page()
        if page:
            try:
                target.update()
            except Exception:
                try:

                    async def _deferred_row_update(_t=target):
                        import asyncio as _asyncio

                        await _asyncio.sleep(0.05)
                        with contextlib.suppress(Exception):
                            _t.update()

                    page.run_task(_deferred_row_update)
                except Exception:
                    pass
        return True

    def apply_live_scope_sync(self, scopes) -> None:
        pass

    def _remove_local_order(self, order_id: str):
        oid = (order_id or "").strip()
        if not oid:
            return
        self._unregister_force_sync_hook(oid)
        self._orders_cache.pop(oid, None)
        supplier_id = "__no_supplier__"
        for row in self._orders:
            if isinstance(row, dict) and (row.get("id") or "").strip() == oid:
                supplier_id = str((row.get("supplier_id") or "") or "__no_supplier__")
                break

        self._orders = [
            row
            for row in self._orders
            if not (isinstance(row, dict) and (row.get("id") or "").strip() == oid)
        ]
        self._schedule_orders_snapshot_save()
        self._schedule_group_counter_refresh({supplier_id})
        # Propagate deletion to sibling tabs via shared map.
        self._shared_orders_map.pop(oid, None)
        deleted = self._shared_orders_map.get("__deleted_ids__")
        if not isinstance(deleted, list):
            self._shared_orders_map["__deleted_ids__"] = [oid]
        elif oid not in deleted:
            deleted.append(oid)
        seq = int(self._shared_orders_map.get("__sync_seq__", 0) or 0)
        self._shared_orders_map["__sync_seq__"] = seq + 1

    def _upsert_order(self, order_row: dict, silent: bool = False) -> bool:
        if not isinstance(order_row, dict):
            return False
        oid = (order_row.get("id") or "").strip()
        if not oid:
            return False

        self._enqueue_outbox(order_row, silent=silent)
        return True

    def _request_fast_outbox_flush(self, delay_sec: float = 0.5) -> None:
        if not self._outbox:
            self._debug_print(
                "request fast outbox flush skipped: no pending outbox items"
            )
            return
        if self._outbox_flush_running:
            self._debug_print(
                "request fast outbox flush skipped: flush already running"
            )
            return
        if self._outbox_fast_flush_task is not None:
            self._debug_print("request fast outbox flush skipped: already scheduled")
            return
        page = self._page()
        if not page or not hasattr(page, "run_task"):
            self._debug_print("request fast outbox flush skipped: no page task runner")
            return

        async def _flush_soon():
            try:
                try:
                    delay_value = float(delay_sec or 0.0)
                except Exception:
                    delay_value = 0.0
                if delay_value > 0:
                    await asyncio.sleep(delay_value)
                self._flush_unsaved_local_drafts()
                if self._outbox:
                    await self._flush_outbox_async()
            finally:
                self._outbox_fast_flush_task = None

        try:
            self._outbox_fast_flush_task = page.run_task(_flush_soon)
        except Exception:
            self._outbox_fast_flush_task = None

    def _upsert_order_fast(self, order_row: dict, flush_delay: float = 0.6) -> bool:
        return self._upsert_order(order_row)

    def _enqueue_order_delete(self, order_id: str, order_snapshot: dict) -> None:
        """Queue a deletion in the outbox — same lifecycle as edits.
        Actual DB delete happens in _flush_outbox_async when the sync timer fires.
        Immediately sets the sync indicator to "pending" (Локально)."""
        oid = (order_id or "").strip()
        if not oid:
            return
        snap = dict(order_snapshot) if isinstance(order_snapshot, dict) else {}
        snap["id"] = oid
        snap["_pending_delete"] = True  # flag: this outbox entry is a deletion
        self._outbox[oid] = snap
        self._schedule_outbox_file_save()
        self._refresh_sync_indicator_state()
        self._start_sync_timer()

    def _delete_order(self, order_id: str) -> bool:
        """Direct synchronous DB delete — used by _flush_outbox_async only."""
        if self.db.delete_order(order_id, manager_id=self.manager_id):
            return True

        oid = (order_id or "").strip()
        if not oid:
            return False
        rows = self._read_legacy_rows()
        new_rows = [
            row
            for row in rows
            if not (isinstance(row, dict) and (row.get("id") or "").strip() == oid)
        ]
        return self._write_legacy_rows(new_rows)

    def _next_invoice_no(self) -> str:
        rows = self._orders or []
        max_num = 0
        current_region_id = self._effective_region_id()
        for row in rows:
            if not isinstance(row, dict):
                continue
            if (row.get("manager_id") or "").strip() != self.manager_id:
                continue
            if (
                current_region_id
                and (row.get("region_id") or "").strip() != current_region_id
            ):
                continue
            inv = row.get("invoice_no") or ""
            try:
                n = int(str(inv).lstrip("0") or "0")
                if n > max_num:
                    max_num = n
            except Exception:
                pass
        return str(max_num + 1).zfill(3)

    def _existing_invoices(self, region_id: str | None = None) -> set:
        """Return set of all invoice_no for current manager in given region."""
        rid = str(region_id or "").strip() or None
        result = set()
        for o in self._orders or []:
            if not isinstance(o, dict):
                continue
            if str(o.get("manager_id") or "").strip() != self.manager_id:
                continue
            if rid and str(o.get("region_id") or "").strip() != rid:
                continue
            inv = str(o.get("invoice_no") or "").strip()
            if inv:
                result.add(inv)
        return result

    def _is_invoice_duplicate(
        self,
        invoice: str,
        region_id: str | None = None,
        exclude_order_id: str | None = None,
    ) -> bool:
        """True if invoice already exists for this manager in this region."""
        inv = str(invoice or "").strip()
        if not inv:
            return False
        rid = str(region_id or "").strip() or None
        ex_id = str(exclude_order_id or "").strip()
        for o in self._orders or []:
            if not isinstance(o, dict):
                continue
            if str(o.get("manager_id") or "").strip() != self.manager_id:
                continue
            if rid and str(o.get("region_id") or "").strip() != rid:
                continue
            if ex_id and str(o.get("id") or "").strip() == ex_id:
                continue
            if str(o.get("invoice_no") or "").strip() == inv:
                return True
        return False

    def _invoice_suggestions(
        self, base_invoice: str, region_id: str | None = None
    ) -> list:
        """Return 4 suggestions for a duplicate invoice: 3 sub-variants + 1 next integer."""
        base = str(base_invoice or "").strip()
        existing = self._existing_invoices(region_id)
        suggestions: list = []
        # 3 sub-variants: base.1, base.2, ... (skip already existing)
        i = 1
        while len(suggestions) < 3 and i <= 50:
            sub = f"{base}.{i}"
            if sub not in existing:
                suggestions.append(sub)
            i += 1
        # 1 next unused integer
        try:
            base_int = int(str(base).split(".")[0])
            next_int = base_int + 1
            for _ in range(2000):
                plain = str(next_int)
                padded = str(next_int).zfill(3)
                if plain not in existing and padded not in existing:
                    suggestions.append(
                        padded
                        if (base.startswith("0") or (len(base) == 3 and base.isdigit()))
                        else plain
                    )
                    break
                next_int += 1
        except Exception:
            pass
        return suggestions

    def _load_lookups(self, include_products: bool = True):
        start = time.monotonic()
        region_id = self._effective_region_id()
        try:
            self._suppliers = (
                self.db.get_suppliers(
                    self.company_id, region_id, manager_id=self.manager_id
                )
                or []
            )
            self._suppliers_by_id: dict = {
                str((s or {}).get("id") or "").strip(): s
                for s in self._suppliers
                if isinstance(s, dict) and (s.get("id") or "")
            }
        except Exception:
            self._suppliers = []
            self._suppliers_by_id = {}
        try:
            self._categories = (
                self.db.get_company_categories(
                    self.company_id, region_id, manager_id=self.manager_id
                )
                or []
            )
        except Exception:
            self._categories = []
        self._category_levels_config_map = {}
        self._company_catalog_config_cache = {}
        if include_products:
            try:
                if self._general_mode and self._general_manager_ids:
                    # Load products for ALL selected managers so visible_product_ids
                    # covers all their orders in the SQL pre-filter.
                    _all_prods: list = []
                    _seen_pids: set = set()
                    for _mid in self._general_manager_ids:
                        try:
                            _mp = (
                                self.db.get_products(
                                    self.company_id,
                                    region_id=region_id,
                                    manager_id=_mid,
                                )
                                or []
                            )
                            for _p in _mp:
                                _pid = str((_p or {}).get("id") or "").strip()
                                if _pid and _pid not in _seen_pids:
                                    _seen_pids.add(_pid)
                                    _all_prods.append(_p)
                        except Exception:
                            pass
                    self._products = _all_prods
                else:
                    self._products = (
                        self.db.get_products(
                            self.company_id,
                            region_id=region_id,
                            manager_id=self.manager_id,
                        )
                        or []
                    )
                self._products_by_id_cache = {
                    str((p or {}).get("id") or "").strip(): p
                    for p in self._products
                    if isinstance(p, dict) and (p.get("id") or "")
                }
                self._visible_product_ids_cache = {
                    str((p or {}).get("id") or "").strip()
                    for p in self._products
                    if isinstance(p, dict) and (p.get("id") or "")
                }
                # Disambiguated names (name + " | Кат: ... | Постач: ...") for
                # products that share a name — recomputed once here instead of
                # per table row. See _product_option_label_map / product_naming.py.
                try:
                    self._product_label_map_cache = self._product_option_label_map(
                        self._products
                    )
                except Exception:
                    self._product_label_map_cache = {}
                logger.info(
                    "[orders-debug] _load_lookups | manager_id=%s | region_id=%s | products=%s | visible_ids=%s",
                    self.manager_id,
                    region_id,
                    len(self._products),
                    len(self._visible_product_ids_cache),
                )
            except Exception:
                self._products = []
                self._products_by_id_cache = {}
                self._visible_product_ids_cache = set()
        try:
            all_methods = (
                self.db.get_effective_delivery_methods_for_manager(
                    self.company_id,
                    self.manager_id,
                    region_id=region_id,
                )
                or []
            )
            self._delivery_methods = [
                m
                for m in all_methods
                if isinstance(m, dict) and (m.get("enabled") is not False)
            ]
            self._delivery_methods_by_id: dict = {
                str((m or {}).get("id") or "").strip(): m
                for m in self._delivery_methods
                if isinstance(m, dict) and (m.get("id") or "")
            }
        except Exception:
            self._delivery_methods = []
            self._delivery_methods_by_id = {}
        try:
            self._catalog_nodes = (
                self.db.get_company_catalog_nodes(self.company_id) or []
            )
            self._node_name_by_id = {
                str(n.get("id") or "").strip(): str(n.get("name") or "").strip()
                for n in self._catalog_nodes
                if isinstance(n, dict) and n.get("id") and n.get("name")
            }
        except Exception:
            self._catalog_nodes = []
            self._node_name_by_id = {}
        try:
            self._editable_product_ids = set(
                self.db.get_entity_owner_entity_ids(
                    self.company_id, self.manager_id, "product"
                )
                or []
            )
        except Exception:
            self._editable_product_ids = set()
        try:
            mgr_id = (self.user_data.get("id") or "").strip()
            if mgr_id:
                self._mgr_workspace_state_cache = (
                    self.db.get_manager_workspace_state(mgr_id) or {}
                )
            else:
                self._mgr_workspace_state_cache = {}
        except Exception:
            self._mgr_workspace_state_cache = {}
        self._debug_print(
            f"load lookups finished suppliers={len(self._suppliers)} products={len(self._products)} delivery_methods={len(self._delivery_methods)} editable_products={len(self._editable_product_ids)} include_products={include_products} in {time.monotonic() - start:.3f}s"
        )

    def _delivery_method_tariff(self, method_row: Optional[dict]):
        row = method_row if isinstance(method_row, dict) else {}
        price = _safe_float(
            (
                row.get("effective_price_per_kg")
                if row.get("effective_price_per_kg") is not None
                else row.get("base_price_per_kg")
            ),
            None,
        )
        ccy = (
            str(
                row.get("effective_price_currency")
                or row.get("price_currency")
                or row.get("base_price_currency")
                or "USD"
            )
            .upper()
            .strip()
            or "USD"
        )
        return price, ccy

    def _fetch_nbu_rates(self) -> dict:
        if not _HAS_REQUESTS:
            return {}

        def _from_nbu() -> dict:
            resp = _requests.get(
                "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json",
                timeout=6,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                return {}
            out = {"UAH": 1.0}
            for row in data:
                if not isinstance(row, dict):
                    continue
                cc = str(row.get("cc") or "").upper().strip()
                rate = _safe_float(row.get("rate"))
                unit = _safe_float(row.get("unit"), 1.0) or 1.0
                if not cc or rate is None or rate <= 0 or unit <= 0:
                    continue
                out[cc] = round(float(rate) / float(unit), 6)
            return out

        def _from_er_api() -> dict:
            resp = _requests.get(
                "https://open.er-api.com/v6/latest/UAH",
                timeout=6,
            )
            resp.raise_for_status()
            data = resp.json()
            raw: dict = data.get("rates") or {}
            uah_per_unit: dict = {"UAH": 1.0}
            for cc, val in raw.items():
                cc = cc.upper()
                v = _safe_float(val)
                if cc and v and cc != "UAH":
                    uah_per_unit[cc] = round(1.0 / v, 6)
            return uah_per_unit

        def _from_exchangerate_host() -> dict:
            resp = _requests.get(
                "https://api.exchangerate.host/latest?base=UAH",
                timeout=6,
            )
            resp.raise_for_status()
            data = resp.json()
            raw: dict = data.get("rates") or {}
            uah_per_unit: dict = {"UAH": 1.0}
            for cc, val in raw.items():
                cc = str(cc or "").upper().strip()
                v = _safe_float(val)
                if cc and v and cc != "UAH":
                    uah_per_unit[cc] = round(1.0 / v, 6)
            return uah_per_unit

        def _from_frankfurter() -> dict:
            resp = _requests.get(
                "https://api.frankfurter.app/latest?from=UAH",
                timeout=6,
            )
            resp.raise_for_status()
            data = resp.json()
            raw: dict = data.get("rates") or {}
            uah_per_unit: dict = {"UAH": 1.0}
            for cc, val in raw.items():
                cc = cc.upper()
                v = _safe_float(val)
                if cc and v:
                    uah_per_unit[cc] = round(1.0 / v, 6)
            return uah_per_unit

        def _from_floatrates() -> dict:
            resp = _requests.get(
                "https://www.floatrates.com/daily/uah.json",
                timeout=6,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                return {}
            uah_per_unit: dict = {"UAH": 1.0}
            for cc, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                v = _safe_float(entry.get("rate"))
                cc = str(cc or "").upper().strip()
                if cc and v and cc != "UAH":
                    uah_per_unit[cc] = round(1.0 / v, 6)
            return uah_per_unit

        for attempt, fetcher in enumerate(
            [
                _from_nbu,
                _from_er_api,
                _from_exchangerate_host,
                _from_frankfurter,
                _from_floatrates,
            ],
            1,
        ):
            try:
                result = fetcher()
                if result:
                    return result
            except Exception:
                pass
        return {}

    def _compute_rate_to_usd(self, ccy: str, rates_map: dict) -> Optional[float]:
        ccy = (ccy or "USD").upper()
        if ccy == "USD":
            return 1.0
        usd_r = rates_map.get("USD")
        ccy_r = rates_map.get(ccy)
        if usd_r and ccy_r:
            return round(ccy_r / usd_r, 6)
        return None

    def _get_uah_rates_cached(self) -> dict:
        # Delegates to the shared fx_rates module (also used by
        # cost_price_stock_page.py, cost_price_orders_table.py,
        # products_view.py) so every screen fetches/caches live rates
        # identically.
        return fx_rates.get_uah_rates()

    def _load_fx_rates_for_company(self) -> None:
        """Fetch-once-per-day orchestration, sync (runs off the UI thread
        via asyncio.to_thread): local memory/file -> shared company DB copy
        (real sharing for VPS companies; a harmless no-op extra check for
        Локал) -> network, in that order, so at most one user per company
        per day ever hits the network."""
        if fx_rates.has_today():
            return
        db_rates = None
        with contextlib.suppress(Exception):
            db_rates = self.db.get_company_fx_rates(self.company_id)
        if db_rates:
            fx_rates.set_rates(db_rates)
            return
        fx_rates.ensure_rates_loaded()
        if fx_rates.has_today():
            with contextlib.suppress(Exception):
                self.db.set_company_fx_rates(
                    self.company_id, fx_rates.get_uah_rates(), fx_rates.today_str()
                )

    def _convert_currency_amount(
        self,
        amount: float,
        from_ccy: str,
        to_ccy: str,
    ) -> Optional[float]:
        return fx_rates.convert(amount, from_ccy, to_ccy)

    def _page(self):
        try:
            return self.content_area.page if self.content_area else None
        except RuntimeError:
            return None

    def _orders_viewport_height(self, page) -> int:
        page_h = int(getattr(page, "height", 0) or 0)
        if page_h <= 0:
            win = getattr(page, "window", None)
            page_h = int(getattr(win, "height", 0) or 0) if win else 0
        if page_h > 0:
            by_free_space = page_h - 420
            by_ratio = int(page_h * 0.50)
            return max(220, min(by_free_space, by_ratio))
        return 360

    def _orders_viewport_width(self, page) -> int:
        host_w = (
            int(getattr(self.content_area, "width", 0) or 0) if self.content_area else 0
        )
        if host_w > 0:
            return max(320, int(host_w))
        # content_area not yet measured — return 0 to signal "unknown".
        # Callers must not auto-grow columns when width is unknown, otherwise
        # columns inflate to the full window width on every cold render.
        return 0

    def _apply_orders_viewport_size(self, _=None):
        if not self._tables_inner_container:
            return
        tw = self._get_table_content_width()
        self._debug_print(f"_apply_orders_viewport_size table_w={tw}")
        try:
            self._tables_inner_container.width = tw
            self._tables_inner_container.update()
        except Exception:
            pass

    def _attach_orders_resize_handler(self):
        page = self._page()
        if not page:
            return

        def _on_resized(e):
            self._apply_orders_viewport_size(e)
            try:
                new_w = int(self._orders_viewport_width(page))
            except Exception:
                new_w = 0

            holder = getattr(self, "_resize_refresh_state", None)
            if not isinstance(holder, dict):
                self._resize_refresh_state = {"last_w": new_w, "pending": False}
                self._debug_print(f"orders resize first-measure w={new_w}")
                return

            prev_w = int(holder.get("last_w") or 0)
            holder["last_w"] = int(new_w or 0)

            if bool(getattr(self, "_page_loading", False)):
                self._debug_print(f"orders resize during loading w={new_w} (deferred)")
                return

            self._debug_print(
                f"orders resized event new_w={new_w} prev_w={prev_w} pending={holder.get('pending', False)}"
            )

            if abs(prev_w - int(new_w or 0)) < 4:
                self._debug_print("orders resize ignored small delta")
                return
            if holder.get("pending"):
                return
            holder["pending"] = True

            async def _rerender_later():
                try:
                    await asyncio.sleep(0.12)
                    await self._async_render_initial()
                finally:
                    holder["pending"] = False

            try:
                page.run_task(_rerender_later)
            except Exception:
                holder["pending"] = False

        try:
            if hasattr(page, "on_resized"):
                page.on_resized = _on_resized
            elif hasattr(page, "on_resize"):
                page.on_resize = _on_resized
        except Exception:
            pass

    def _show_help(self) -> None:
        from views.help_utils import show_modal_help
        page = self._page()
        if not page:
            return
        t = self.t
        show_modal_help(page, t("orders.help.title", "Довідка — Замовлення"), [
            t("orders.help.desc",
              "Таблиця замовлень з угрупуванням рік → місяць → постачальник."
              " Усі зміни зберігаються локально і синхронізуються з БД кожні 45 сек."),
            (t("orders.help.steps_title", "Як користуватись"), None),
            t("orders.help.step1", "1. Чіпи статусів вгорі — швидкий фільтр за статусом, оплатою або обліком."),
            t("orders.help.step2", "2. Блок фільтрів — звужте список за постачальником, категорією, товаром або місяцями."),
            t("orders.help.step3", "3. Натисніть «+» щоб створити нове замовлення (коротка форма)."),
            t("orders.help.step4", "4. Клацніть на рядку замовлення → відкриється повна форма редагування."),
            t("orders.help.step5", "5. Значок ▶ ліворуч від рядка розгортає деталі товарів замовлення."),
            t("orders.help.step6", "6. Кнопка ☰ (колонки) — налаштування видимості і ширини стовпців."),
            t("orders.help.step7", "7. Значок 🗑 з'являється при наведенні — видаляє замовлення після підтвердження."),
        ])

    def _show_snack(self, text: str, color: str = "#059669"):
        page = self._page()
        if not page:
            return
        with contextlib.suppress(Exception):
            page.show_dialog(
                ft.SnackBar(
                    ft.Text(text, color=ft.Colors.WHITE, weight="bold"),
                    bgcolor=color,
                    duration=3000,
                )
            )

    def _begin_header_busy(self, text: str = "Завантаження...") -> Optional[str]:
        token = str(uuid.uuid4())
        self._header_busy_tokens.add(token)
        box = self._header_busy_box
        if box is not None:
            box.visible = True
            if self._header_busy_text is not None:
                self._header_busy_text.value = str(text or "Завантаження...")
            with contextlib.suppress(Exception):
                box.update()
        return token

    def _end_header_busy(self, token: Optional[str] = None):
        if not token:
            return
        self._header_busy_tokens.discard(str(token))

        box = self._header_busy_box
        if box is None:
            return
        box.visible = bool(self._header_busy_tokens)
        with contextlib.suppress(Exception):
            box.update()

    def _close_dlg(self, dlg):
        page = self._page()
        if not page:
            return
        with contextlib.suppress(Exception):
            page.pop_dialog()

    def _show_save_overlay(self, message: str) -> None:
        """Show a full-screen blocking overlay with spinner + message.
        Blocks all UI interaction until _hide_save_overlay is called."""
        page = self._page()
        if not page:
            return
        dlg = ft.AlertDialog(
            modal=True,
            title=None,
            title_padding=ft.padding.all(0),
            content_padding=ft.padding.all(0),
            actions=[],
            content=ft.Container(
                padding=ft.padding.symmetric(horizontal=28, vertical=20),
                content=ft.Row(
                    [
                        ft.ProgressRing(
                            width=26, height=26, stroke_width=3, color=_TEXT
                        ),
                        ft.Text(
                            message,
                            size=13,
                            weight=ft.FontWeight.W_600,
                            color=_TEXT,
                        ),
                    ],
                    spacing=14,
                    tight=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ),
        )
        self._save_overlay_dlg = dlg
        with contextlib.suppress(Exception):
            page.show_dialog(dlg)

    def _hide_save_overlay(self) -> None:
        """Close the overlay shown by _show_save_overlay."""
        page = self._page()
        if page and getattr(self, "_save_overlay_dlg", None) is not None:
            with contextlib.suppress(Exception):
                page.pop_dialog()
            self._save_overlay_dlg = None

    def _status_chip(self, status: str) -> ft.Container:
        st = (status or "production").strip()
        label = self.STATUS_LABELS.get(st, st)
        return ft.Container(
            content=ft.Text(
                label, size=10, weight="bold", color=self.STATUS_FG.get(st, "#374151")
            ),
            bgcolor=self.STATUS_BG.get(st, "#E5E7EB"),
            border_radius=999,
            padding=ft.padding.symmetric(horizontal=6, vertical=2),
        )

    def _payment_chip(self, status: str) -> ft.Container:
        label = self.PAYMENT_LABELS.get(status or "unpaid", status)
        st = str(status or "").strip().lower()
        if st == "paid":
            fg, bg = "#166534", "#DCFCE7"
        elif st == "advance":
            fg, bg = "#92400E", "#FED7AA"
        elif st == "transferred":
            fg, bg = "#CA8A04", "#FEF3C7"
        else:
            fg, bg = "#991B1B", "#FEE2E2"
        return ft.Container(
            content=ft.Text(label, size=9, color=fg),
            bgcolor=bg,
            border_radius=999,
            padding=ft.padding.symmetric(horizontal=5, vertical=1),
        )

    def _counter_chips(self, orders: list) -> ft.Row:
        counts = {s: 0 for s in self.STATUSES}
        for o in orders:
            st = (o.get("status") or "production").strip()
            if st in counts:
                counts[st] += 1
        chips = []
        for st in self.STATUSES:
            n = counts[st]
            if n > 0:
                chips.append(
                    ft.Container(
                        content=ft.Text(
                            f"{self.STATUS_LABELS[st]}: {n}",
                            size=9,
                            color=self.STATUS_FG[st],
                        ),
                        bgcolor=self.STATUS_BG[st],
                        border_radius=999,
                        padding=ft.padding.symmetric(horizontal=5, vertical=1),
                    )
                )
        return ft.Row(chips, spacing=3, wrap=True)

    def _orders_facets(self) -> dict:
        suppliers = {}
        deliveries = {}
        products = {}
        categories = {}
        level_nodes = {}
        statuses = set()
        payments = set()
        submissions = set()
        enabled_level_rows = []
        region_enabled_rows = self._region_enabled_levels()
        region_title_by_pos = {
            int(r.get("position") or 0): str(r.get("title") or "").strip()
            for r in (region_enabled_rows or [])
            if int(r.get("position") or 0) in range(1, 7)
        }

        category_level_titles = self._category_level_titles()

        enabled_level_rows_map = {}

        card_index, node_name_by_id = self._product_card_hierarchy_index()

        for o in self._orders or []:
            if not isinstance(o, dict):
                continue
            sid = (o.get("supplier_id") or "").strip()
            sname = (o.get("supplier_name") or sid).strip()
            if sid:
                suppliers[sid] = sname or sid

            did = (o.get("delivery_type_id") or "").strip()
            dname = (o.get("delivery_type_name") or did).strip()
            if did:
                deliveries[did] = dname or did

            st = (o.get("status") or "production").strip()
            if st:
                statuses.add(st)

            pay = (o.get("payment_status") or "unpaid").strip()
            if pay:
                payments.add(pay)

            sub = (o.get("submission_status") or "not_submitted").strip()
            if sub:
                submissions.add(sub)

            for it in o.get("items") or []:
                if not isinstance(it, dict):
                    continue
                pid = (it.get("product_id") or "").strip()
                pname = (it.get("product_name") or pid).strip()
                if pid:
                    products[pid] = pname or pid
                    card = card_index.get(pid) or {}
                    cat_id = str(card.get("category_id") or "").strip()
                    cat_name = str(card.get("category_name") or "").strip()
                    if cat_id:
                        categories[cat_id] = (
                            cat_name or categories.get(cat_id) or cat_id
                        )

                    for pos in range(1, 7):
                        nid = str(
                            ((card.get("level_node_ids") or {}).get(pos) or "")
                        ).strip()
                        if not nid:
                            continue
                        title = (
                            (category_level_titles.get(cat_id) or {}).get(pos)
                            or region_title_by_pos.get(pos)
                            or f"Рівень {pos}"
                        )
                        level_key = self._level_filter_key(pos, title)
                        if level_key not in enabled_level_rows_map:
                            enabled_level_rows_map[level_key] = {
                                "position": pos,
                                "title": title,
                            }
                        lvl_name = node_name_by_id.get(nid) or nid
                        bucket = level_nodes.setdefault(level_key, {})
                        bucket[nid] = lvl_name

        if categories:
            for cat_id in list(categories.keys()):
                title_by_pos = category_level_titles.get(str(cat_id).strip()) or {}
                for pos, title in (title_by_pos or {}).items():
                    try:
                        p = int(pos)
                    except Exception:
                        continue
                    if p < 1 or p > 6:
                        continue
                    t = str(title or f"Рівень {p}").strip() or f"Рівень {p}"
                    level_key = self._level_filter_key(p, t)
                    if level_key not in enabled_level_rows_map:
                        enabled_level_rows_map[level_key] = {
                            "position": p,
                            "title": t,
                        }

        if not enabled_level_rows_map:
            for row in region_enabled_rows or []:
                try:
                    p = int(row.get("position") or 0)
                except Exception:
                    continue
                if p < 1 or p > 6:
                    continue
                t = str(row.get("title") or f"Рівень {p}").strip() or f"Рівень {p}"
                level_key = self._level_filter_key(p, t)
                if level_key not in enabled_level_rows_map:
                    enabled_level_rows_map[level_key] = {
                        "position": p,
                        "title": t,
                    }

        enabled_level_rows = sorted(
            enabled_level_rows_map.values(),
            key=lambda x: (
                int(x.get("position") or 0),
                str(x.get("title") or "").lower(),
            ),
        )

        return {
            "suppliers": suppliers,
            "deliveries": deliveries,
            "products": products,
            "categories": categories,
            "level_nodes": level_nodes,
            "enabled_level_rows": enabled_level_rows,
            "statuses": statuses,
            "payments": payments,
            "submissions": submissions,
        }

    def _is_order_active(self, order: dict) -> bool:
        return (order.get("status") or "production") != "accepted"

    def _has_active_filters(self) -> bool:
        return any(
            [
                self._filter_status != "all",
                self._filter_delivery_id != "all",
                self._filter_product_id != "all",
                self._filter_category_id != "all",
                self._filter_supplier_id != "all",
                self._filter_payment != "all",
                self._filter_submission != "all",
                self._filter_receive_month != "all",
                self._filter_accept_month != "all",
                bool(self._quick_filter_mode),
                self._filter_payment_month != "all",
            ]
        )

    def _reset_all_filters(self) -> None:
        self._filter_status = "all"
        self._filter_delivery_id = "all"
        self._filter_product_id = "all"
        self._filter_category_id = "all"
        self._filter_supplier_id = "all"
        self._filter_payment = "all"
        self._filter_submission = "all"
        self._filter_receive_month = "all"
        self._filter_accept_month = "all"
        self._filter_payment_month = "all"
        self._filter_level_node_ids = {}
        self._schedule_filter_render()

    def _toggle_scalar_filter(self, attr_name: str, value: str) -> None:
        current = str(getattr(self, attr_name, "all") or "all")
        target = str(value or "all") or "all"
        setattr(self, attr_name, "all" if current == target else target)
        self._schedule_full_rerender()

    def _set_receive_month_filter(self, month_key: str) -> None:
        self._filter_receive_month = str(month_key or "all") or "all"
        self._schedule_full_rerender()

    def _set_accept_month_filter(self, month_key: str) -> None:
        self._filter_accept_month = str(month_key or "all") or "all"
        self._schedule_full_rerender()

    def _set_payment_month_filter(self, month_key: str) -> None:
        self._filter_payment_month = str(month_key or "all") or "all"
        self._update_payment_summary_panel()
        self._schedule_full_rerender()

    def _order_receive_date_iso(
        self,
        order: dict,
        products_by_id: Optional[dict] = None,
        delivery_by_id: Optional[dict] = None,
    ) -> str:
        if not isinstance(order, dict):
            return ""
        order_date = str(order.get("order_date") or "")[:10]
        if not order_date:
            return ""

        date_shipped = str(order.get("date_shipped") or "")[:10]
        date_wh = str(order.get("date_arrived_warehouse") or "")[:10]

        dur_prod = _safe_float(order.get("duration_production_days"), None)
        if dur_prod is None:
            dur_prod = _days_between(order_date, date_shipped)
        dur_deliv = _safe_float(order.get("duration_transit_days"), None)
        if dur_deliv is None:
            dur_deliv = _days_between(date_shipped, date_wh)

        prod_term_days = None
        items = order.get("items") if isinstance(order.get("items"), list) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            pid = str(it.get("product_id") or "").strip()
            prod = (products_by_id or {}).get(pid) if pid else None
            p_days = _safe_float((prod or {}).get("production_term_days"), None)
            if p_days is None:
                continue
            p_days = max(0.0, float(p_days))
            prod_term_days = (
                p_days if prod_term_days is None else max(float(prod_term_days), p_days)
            )
        if prod_term_days is None:
            try:
                mgr_id = self.user_data.get("id") or ""
                if mgr_id:
                    payload = getattr(self, "_mgr_workspace_state_cache", None) or {}
                    store = (
                        payload.get("manager_production_term")
                        if isinstance(payload.get("manager_production_term"), dict)
                        else {}
                    )
                    company_key = (
                        str(
                            order.get("company_id") or self.company_id or "no_company"
                        ).strip()
                        or "no_company"
                    )
                    region_key = (
                        str(
                            order.get("region_id")
                            or self._effective_region_id()
                            or "all"
                        ).strip()
                        or "all"
                    )
                    p_row = (
                        store.get(f"{company_key}:{region_key}")
                        if isinstance(store, dict)
                        else None
                    )
                    if isinstance(p_row, dict):
                        p_days = _safe_float(p_row.get("days"), None)
                        if p_days is not None:
                            prod_term_days = p_days
            except Exception:
                pass
            if prod_term_days is None:
                prod_term_days = _safe_float(dur_prod, None)

        deliv_term_days = None
        did = str(order.get("delivery_type_id") or "").strip()
        drow = (delivery_by_id or {}).get(did)
        if isinstance(drow, dict):
            deliv_term_days = _safe_float(
                drow.get("effective_delivery_term_days"), None
            )
            if deliv_term_days is None:
                deliv_term_days = _safe_float(drow.get("base_delivery_term_days"), None)
            if deliv_term_days is None:
                deliv_term_days = _safe_float(drow.get("delivery_term_days"), None)
        if deliv_term_days is None:
            deliv_term_days = _safe_float(dur_deliv, None)

        if prod_term_days is None and deliv_term_days is None:
            return ""

        # Once the order has actually left production (status advanced past
        # "виробництво"/"готовий") and a real "Готово" date (date_shipped) is
        # known, the preliminary receive date is pinned to that real date
        # instead of the original order-date estimate — it's strictly more
        # accurate once available: Отримання = Дата Готово + термін доставки.
        status = str(order.get("status") or "").strip()
        if status not in {"production", "ready_for_shipment"} and date_shipped:
            return _add_days_to_iso(date_shipped, float(deliv_term_days or 0.0))

        total_days = float(prod_term_days or 0.0) + float(deliv_term_days or 0.0)
        return _add_days_to_iso(order_date, total_days)

    def _order_payment_date_iso(
        self,
        order: dict,
        products_by_id: Optional[dict] = None,
    ) -> str:
        """Estimated payment date = order_date + production_term_days (no delivery)."""
        if not isinstance(order, dict):
            return ""
        order_date = str(order.get("order_date") or "")[:10]
        if not order_date:
            return ""

        date_shipped = str(order.get("date_shipped") or "")[:10]
        dur_prod = _safe_float(order.get("duration_production_days"), None)
        if dur_prod is None:
            dur_prod = _days_between(order_date, date_shipped)

        prod_term_days = None
        items = order.get("items") if isinstance(order.get("items"), list) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            pid = str(it.get("product_id") or "").strip()
            prod = (products_by_id or {}).get(pid) if pid else None
            p_days = _safe_float((prod or {}).get("production_term_days"), None)
            if p_days is None:
                continue
            p_days = max(0.0, float(p_days))
            prod_term_days = (
                p_days if prod_term_days is None else max(float(prod_term_days), p_days)
            )
        if prod_term_days is None:
            try:
                mgr_id = self.user_data.get("id") or ""
                if mgr_id:
                    payload = getattr(self, "_mgr_workspace_state_cache", None) or {}
                    store = (
                        payload.get("manager_production_term")
                        if isinstance(payload.get("manager_production_term"), dict)
                        else {}
                    )
                    company_key = (
                        str(
                            order.get("company_id") or self.company_id or "no_company"
                        ).strip()
                        or "no_company"
                    )
                    region_key = (
                        str(
                            order.get("region_id")
                            or self._effective_region_id()
                            or "all"
                        ).strip()
                        or "all"
                    )
                    p_row = (
                        store.get(f"{company_key}:{region_key}")
                        if isinstance(store, dict)
                        else None
                    )
                    if isinstance(p_row, dict):
                        p_days = _safe_float(p_row.get("days"), None)
                        if p_days is not None:
                            prod_term_days = p_days
            except Exception:
                pass
            if prod_term_days is None:
                prod_term_days = _safe_float(dur_prod, None)

        if prod_term_days is None:
            return ""
        return _add_days_to_iso(order_date, float(prod_term_days))

    def _order_payment_month_key(
        self,
        order: dict,
        products_by_id: Optional[dict] = None,
    ) -> str:
        iso = self._order_payment_date_iso(order, products_by_id=products_by_id)
        return iso[:7] if len(iso) >= 7 else ""

    def _order_receive_month_key(
        self,
        order: dict,
        products_by_id: Optional[dict] = None,
        delivery_by_id: Optional[dict] = None,
    ) -> str:
        try:
            cached = (order or {}).get("_receive_month")
            if cached:
                return str(cached or "")[:7]
        except Exception:
            pass

        iso = self._order_receive_date_iso(
            order,
            products_by_id=products_by_id,
            delivery_by_id=delivery_by_id,
        )
        return iso[:7] if len(iso) >= 7 else ""

    def _receive_month_options(self, orders: list[dict]) -> list[tuple[str, str]]:
        products_by_id = {
            str((p or {}).get("id") or "").strip(): p
            for p in (self._products or [])
            if isinstance(p, dict) and (p.get("id") or "")
        }
        delivery_by_id = {
            str((d or {}).get("id") or "").strip(): d
            for d in (self._delivery_methods or [])
            if isinstance(d, dict) and (d.get("id") or "")
        }
        keys = set()
        for o in orders or []:
            k = self._order_receive_month_key(
                o,
                products_by_id=products_by_id,
                delivery_by_id=delivery_by_id,
            )
            if k:
                keys.add(k)

        out = [("all", self.t("orders.filter.all_months", "Всі місяці"))]
        for key in sorted(keys, reverse=True):
            try:
                y = int(key[:4])
                m = int(key[5:7])
                title = f"{self.MONTH_NAMES.get(m, str(m))}, {y}"
            except Exception:
                title = key
            out.append((key, title))
        return out

    def _accept_month_options(self, orders: list[dict]) -> list[tuple[str, str]]:
        keys = set()
        for o in orders or []:
            iso = str((o or {}).get("date_accepted") or "")[:7]
            if len(iso) == 7:
                keys.add(iso)
        out = [("all", self.t("orders.filter.all_months", "Всі місяці"))]
        for key in sorted(keys, reverse=True):
            try:
                y = int(key[:4])
                m = int(key[5:7])
                title = f"{self.MONTH_NAMES.get(m, str(m))}, {y}"
            except Exception:
                title = key
            out.append((key, title))
        return out

    def _payment_month_options(self, orders: list[dict]) -> list[tuple[str, str]]:
        products_by_id = {
            str((p or {}).get("id") or "").strip(): p
            for p in (self._products or [])
            if isinstance(p, dict) and (p.get("id") or "")
        }
        keys = set()
        for o in orders or []:
            k = self._order_payment_month_key(o, products_by_id=products_by_id)
            if k:
                keys.add(k)
        out = [("all", self.t("orders.filter.all_months", "Всі місяці"))]
        for key in sorted(keys, reverse=True):
            try:
                y = int(key[:4])
                m = int(key[5:7])
                title = f"{self.MONTH_NAMES.get(m, str(m))}, {y}"
            except Exception:
                title = key
            out.append((key, title))
        return out

    @staticmethod
    def _level_filter_key(pos: int, title: str) -> str:
        return f"{int(pos)}|{str(title or '').strip().lower()}"

    def _set_group_expand_mode(self, mode: str):
        if mode not in {"actual", "all", "supplier"}:
            return
        self._group_expand_mode = mode
        self._schedule_full_rerender()

    def _toggle_dates_block(self):
        self._show_dates_block = not bool(self._show_dates_block)
        self._schedule_full_rerender()

    def _toggle_cost_block(self):
        self._show_cost_block = not bool(self._show_cost_block)
        self._schedule_full_rerender()

    def _event_raw_value(self, e):
        ctrl_val = getattr(getattr(e, "control", None), "value", None)
        data_val = getattr(e, "data", None)
        raw = ctrl_val if ctrl_val not in (None, "") else data_val
        return str(raw or "").strip()

    def _normalize_dropdown_value(
        self, dd: ft.Dropdown, raw_value: str, fallback: str = "all"
    ) -> str:
        raw = str(raw_value or "").strip()
        if not raw:
            return fallback

        options = list(dd.options or [])
        for opt in options:
            if str(getattr(opt, "key", "")) == raw:
                return raw

        raw_l = raw.lower()
        for opt in options:
            txt = str(getattr(opt, "text", "") or "").strip().lower()
            if txt and txt == raw_l:
                key = str(getattr(opt, "key", "") or "").strip()
                if key:
                    return key

        for opt in options:
            txt = str(getattr(opt, "text", "") or "").strip().lower()
            key = str(getattr(opt, "key", "") or "").strip()
            if not txt:
                continue
            if txt.startswith(raw_l) or raw_l.startswith(txt):
                return key or fallback

        return fallback

    def _filtered_orders(self) -> list:
        out = []
        card_index, _ = self._product_card_hierarchy_index()
        products_by_id = None
        delivery_by_id = None
        if self._filter_receive_month != "all" or self._filter_payment_month != "all":
            products_by_id = {
                str((p or {}).get("id") or "").strip(): p
                for p in (self._products or [])
                if isinstance(p, dict) and (p.get("id") or "")
            }
            delivery_by_id = {
                str((d or {}).get("id") or "").strip(): d
                for d in (self._delivery_methods or [])
                if isinstance(d, dict) and (d.get("id") or "")
            }
        accept_month_filter = self._filter_accept_month
        payment_month_filter = self._filter_payment_month
        display_from = (self._display_from_month or "").strip()
        for o in self._orders:
            if display_from and str(o.get("order_date") or "")[:7] < display_from:
                continue
            if (
                self._filter_supplier_id != "all"
                and (o.get("supplier_id") or "") != self._filter_supplier_id
            ):
                continue
            if (
                self._filter_status != "all"
                and (o.get("status") or "production") != self._filter_status
            ):
                continue
            if (
                self._filter_delivery_id != "all"
                and (o.get("delivery_type_id") or "") != self._filter_delivery_id
            ):
                continue
            if self._filter_product_id != "all":
                items = o.get("items") or []
                if not any(
                    (it.get("product_id") or "") == self._filter_product_id
                    for it in items
                ):
                    continue
            if self._filter_category_id != "all":
                items = o.get("items") or []
                matched_category = False
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    pid = str(it.get("product_id") or "").strip()
                    card = card_index.get(pid) or {}
                    cat_id = str(card.get("category_id") or "").strip()
                    if cat_id == self._filter_category_id:
                        matched_category = True
                        break
                if not matched_category:
                    continue
            if (
                self._filter_payment != "all"
                and (o.get("payment_status") or "unpaid") != self._filter_payment
            ):
                continue
            if (
                self._filter_submission != "all"
                and (o.get("submission_status") or "not_submitted")
                != self._filter_submission
            ):
                continue
            if self._filter_receive_month != "all":
                receive_month = self._order_receive_month_key(
                    o,
                    products_by_id=products_by_id,
                    delivery_by_id=delivery_by_id,
                )
                if receive_month != self._filter_receive_month:
                    continue
            if accept_month_filter != "all":
                order_accept_iso = str((o or {}).get("date_accepted") or "")[:7]
                if order_accept_iso != accept_month_filter:
                    continue
            if payment_month_filter != "all":
                pay_month = self._order_payment_month_key(
                    o, products_by_id=products_by_id
                )
                if pay_month != payment_month_filter:
                    continue

            # Custom quick_filter from home KPI cards.
            if self._quick_filter_mode == "not_submitted_non_production":
                _st = str(o.get("status") or "production").strip().lower()
                if _st in {"production", "виробництво", "в виробництві"}:
                    continue  # exclude production status

            if self._quick_filter_mode == "missing_status_date":
                from datetime import date as _date_cls

                _date_field_map = {
                    "ready_for_shipment": "date_ready_at_supplier",
                    "in_transit": "date_shipped",
                    "at_warehouse": "date_arrived_warehouse",
                    "accepted": "date_accepted",
                }
                _st = str(o.get("status") or "production").strip().lower()
                _df = _date_field_map.get(_st)
                if _df is None:
                    continue  # skip production
                _raw = str(o.get(_df) or "").strip()
                if _raw:
                    try:
                        from datetime import datetime as _dt

                        _d = _dt.fromisoformat(_raw[:10]).date()
                        if _d <= _date_cls.today():
                            continue  # has a valid past/today date — skip
                    except Exception:
                        pass  # can't parse → treat as missing → keep

            out.append(o)
        return out

    def _update_counters(self):
        row = self._counters_row
        if row is None:
            return
        all_orders = self._orders or []
        cnt = {
            "production": 0,
            "ready_for_shipment": 0,
            "in_transit": 0,
            "at_warehouse": 0,
            "accepted": 0,
            "unpaid": 0,
            "advance": 0,
            "transferred": 0,
            "paid": 0,
            "not_submitted": 0,
            "submitted": 0,
        }
        _status_aliases = {
            "прийнятий": "accepted",
            "прийняті": "accepted",
            "прийнято": "accepted",
            "done": "accepted",
            "виробництво": "production",
            "в виробництві": "production",
            "готово": "ready_for_shipment",
            "готовий": "ready_for_shipment",
            "ready": "ready_for_shipment",
            "в дорозі": "in_transit",
            "transit": "in_transit",
            "на складі": "at_warehouse",
            "warehouse": "at_warehouse",
        }
        for o in all_orders:
            if not isinstance(o, dict):
                continue
            st = str(o.get("status") or "").strip()
            st = _status_aliases.get(st.lower(), st)
            if st in cnt:
                cnt[st] += 1
            pay = str(o.get("payment_status") or "unpaid").strip()
            if pay == "paid":
                cnt["paid"] += 1
            elif pay == "advance":
                cnt["advance"] += 1
            elif pay == "transferred":
                cnt["transferred"] += 1
            else:
                cnt["unpaid"] += 1
            sub = str(o.get("submission_status") or "not_submitted").strip()
            if sub == "submitted":
                cnt["submitted"] += 1
            else:
                cnt["not_submitted"] += 1

        filter_state = (
            self._filter_status,
            self._filter_delivery_id,
            self._filter_product_id,
            self._filter_category_id,
            self._filter_supplier_id,
            self._filter_payment,
            self._filter_submission,
            self._filter_receive_month,
            self._filter_accept_month,
        )

        if (
            getattr(self, "_last_counter_counts", None) == cnt
            and getattr(self, "_last_counter_filters", None) == filter_state
        ):
            return
        self._last_counter_counts = dict(cnt)
        self._last_counter_filters = filter_state

        def _chip(
            label: str,
            count: int,
            bg: str,
            fg: str,
            on_click=None,
            active: bool = False,
        ) -> ft.Container:
            return ft.Container(
                bgcolor=_BORDER if active else bg,
                border=ft.border.all(1, _BORDER if active else "#00000000"),
                border_radius=6,
                padding=ft.padding.symmetric(horizontal=6, vertical=2),
                ink=bool(callable(on_click)),
                on_click=on_click,
                content=ft.Row(
                    [
                        ft.Text(label, size=9, color=fg),
                        ft.Container(
                            bgcolor=fg,
                            border_radius=10,
                            padding=ft.padding.symmetric(horizontal=5, vertical=1),
                            content=ft.Text(
                                str(count), size=9, color="#FFFFFF", weight="bold"
                            ),
                        ),
                    ],
                    spacing=4,
                    tight=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            )

        def _sep() -> ft.Container:
            return ft.Container(
                width=1,
                height=20,
                bgcolor=_BORDER,
                margin=ft.margin.symmetric(horizontal=4),
            )

        total_orders = 0
        try:
            total_orders = int(len([o for o in all_orders if isinstance(o, dict)]))
        except Exception:
            total_orders = int(len(all_orders or []))

        group_total = [
            _chip(
                self.t("orders.chip.total", "Замовлень"),
                total_orders,
                "#FFFFFF",
                "#334155",
                on_click=lambda _e: self._reset_all_filters(),
                active=self._has_active_filters(),
            )
        ]
        group_status = [
            _chip(
                self.t("orders.status.production", "Вироб-во"),
                cnt["production"],
                "#FEE2E2",
                "#DC2626",
                on_click=lambda _e: self._toggle_scalar_filter(
                    "_filter_status", "production"
                ),
                active=self._filter_status == "production",
            ),
            _chip(
                self.t("orders.status.ready_for_shipment", "Готовий"),
                cnt.get("ready_for_shipment", 0),
                "#FEF08A",
                "#92400E",
                on_click=lambda _e: self._toggle_scalar_filter(
                    "_filter_status", "ready_for_shipment"
                ),
                active=self._filter_status == "ready_for_shipment",
            ),
            _chip(
                self.t("orders.status.in_transit", "В дорозі"),
                cnt["in_transit"],
                "#FEF9C3",
                "#CA8A04",
                on_click=lambda _e: self._toggle_scalar_filter(
                    "_filter_status", "in_transit"
                ),
                active=self._filter_status == "in_transit",
            ),
            _chip(
                self.t("orders.status.at_warehouse", "На складі"),
                cnt["at_warehouse"],
                "#DCFCE7",
                "#16A34A",
                on_click=lambda _e: self._toggle_scalar_filter(
                    "_filter_status", "at_warehouse"
                ),
                active=self._filter_status == "at_warehouse",
            ),
            _chip(
                self.t("orders.status.accepted", "Прийнятий"),
                cnt["accepted"],
                "#DBEAFE",
                "#1D4ED8",
                on_click=lambda _e: self._toggle_scalar_filter(
                    "_filter_status", "accepted"
                ),
                active=self._filter_status == "accepted",
            ),
        ]
        group_payment = [
            _chip(
                self.t("orders.payment.unpaid", "Не сплачен."),
                cnt["unpaid"],
                "#FEE2E2",
                "#DC2626",
                on_click=lambda _e: self._toggle_scalar_filter(
                    "_filter_payment", "unpaid"
                ),
                active=self._filter_payment == "unpaid",
            ),
            _chip(
                self.t("orders.payment.advance", "Аванс"),
                cnt["advance"],
                "#FED7AA",
                "#92400E",
                on_click=lambda _e: self._toggle_scalar_filter(
                    "_filter_payment", "advance"
                ),
                active=self._filter_payment == "advance",
            ),
            _chip(
                self.t("orders.payment.transferred", "Передано"),
                cnt["transferred"],
                "#FEF3C7",
                "#CA8A04",
                on_click=lambda _e: self._toggle_scalar_filter(
                    "_filter_payment", "transferred"
                ),
                active=self._filter_payment == "transferred",
            ),
            _chip(
                self.t("orders.payment.paid", "Сплач."),
                cnt["paid"],
                "#DCFCE7",
                "#166534",
                on_click=lambda _e: self._toggle_scalar_filter(
                    "_filter_payment", "paid"
                ),
                active=self._filter_payment == "paid",
            ),
        ]
        group_submission = [
            _chip(
                self.t("orders.submission.not_submitted", "Не переданий"),
                cnt["not_submitted"],
                "#F3F4F6",
                "#374151",
                on_click=lambda _e: self._toggle_scalar_filter(
                    "_filter_submission", "not_submitted"
                ),
                active=self._filter_submission == "not_submitted",
            ),
            _chip(
                self.t("orders.submission.submitted", "Внесений"),
                cnt["submitted"],
                "#DCFCE7",
                "#166534",
                on_click=lambda _e: self._toggle_scalar_filter(
                    "_filter_submission", "submitted"
                ),
                active=self._filter_submission == "submitted",
            ),
        ]

        row.controls.clear()
        row.controls.extend(group_total)
        row.controls.append(_sep())
        row.controls.extend(group_status)
        row.controls.append(_sep())
        row.controls.extend(group_payment)
        row.controls.append(_sep())
        row.controls.extend(group_submission)
        try:
            if not getattr(self, "_counters_skip_update", False):
                row.update()
        except Exception:
            pass

    def _build_payment_summary_panel(self) -> Optional[ft.Container]:
        month = self._filter_payment_month
        if month == "all":
            return None
        products_by_id = {
            str((p or {}).get("id") or "").strip(): p
            for p in (self._products or [])
            if isinstance(p, dict) and (p.get("id") or "")
        }
        card_index, _ = self._product_card_hierarchy_index()
        # Collect per-product totals: {(cat_id, cat_name, prod_name): {ccy: amount}}
        product_totals: dict = {}
        ccy_totals: dict = {}
        for o in self._orders or []:
            if not isinstance(o, dict):
                continue
            pay_month = self._order_payment_month_key(o, products_by_id=products_by_id)
            if pay_month != month:
                continue
            items = o.get("items") or []
            for it in items:
                if not isinstance(it, dict):
                    continue
                pid = str(it.get("product_id") or "").strip()
                prod = products_by_id.get(pid) or {}
                card = card_index.get(pid) or {}
                cat_id = str(
                    card.get("category_id") or prod.get("category_id") or ""
                ).strip()
                cat_name = str(
                    card.get("category_name")
                    or prod.get("category_name")
                    or self.t("orders.no_category", "Без категорії")
                ).strip()
                prod_name = str(
                    prod.get("name") or it.get("product_name") or "?"
                ).strip()
                p = _safe_float(it.get("purchase_price"), None)
                if p is None:
                    p = _safe_float((prod or {}).get("purchase_price"), None)
                ccy = _normalize_currency_code(
                    it.get("purchase_currency")
                    or (prod or {}).get("purchase_currency"),
                    "USD",
                )
                q = _safe_float(it.get("qty") or it.get("quantity"), 0.0) or 0.0
                if p is None or q == 0:
                    continue
                amt = q * p
                key = (cat_id, cat_name, prod_name)
                if key not in product_totals:
                    product_totals[key] = {}
                product_totals[key][ccy] = product_totals[key].get(ccy, 0.0) + amt
                ccy_totals[ccy] = ccy_totals.get(ccy, 0.0) + amt

        def _fmt(v: float) -> str:
            return format_grouped(v, decimals=2, trim=True)

        def _ccy_sym(c: str) -> str:
            return {"USD": "$", "UAH": "₴", "EUR": "€", "PLN": "zł", "CNY": "¥"}.get(
                c.upper(), c
            )

        # Whether any non-USD currency is present -> show 3rd USD column
        has_non_usd = any(c != "USD" for c in ccy_totals)
        COL_W = 150

        def _to_usd(ccys: dict) -> Optional[float]:
            total = 0.0
            for c, v in ccys.items():
                if c == "USD":
                    total += v
                else:
                    conv = self._convert_currency_amount(v, c, "USD")
                    if conv is None:
                        return None
                    total += conv
            return total

        def _sup_ccy_text(ccys: dict) -> str:
            parts = [f"{_fmt(v)} {_ccy_sym(c)}" for c, v in sorted(ccys.items())]
            return "  +  ".join(parts)

        def _amt_cell(
            txt: str, bold: bool = False, color: str = "#1D4ED8"
        ) -> ft.Container:
            return ft.Container(
                width=COL_W,
                alignment=ft.Alignment(1, 0),
                content=ft.Text(
                    txt,
                    size=8,
                    color=color,
                    weight="bold" if bold else "w400",
                    text_align=ft.TextAlign.RIGHT,
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
            )

        # Build category groups
        cats: dict = {}
        for (cat_id, cat_name, prod_name), ccys in product_totals.items():
            k = (cat_id, cat_name)
            if k not in cats:
                cats[k] = {}
            cats[k][prod_name] = ccys

        cat_rows = []
        for (cat_id, cat_name), prods in sorted(
            cats.items(), key=lambda kv: kv[0][1].lower()
        ):
            prod_rows = []
            for pname, ccys in sorted(prods.items(), key=lambda kv: kv[0].lower()):
                sup_text = _sup_ccy_text(ccys)
                usd_val = _to_usd(ccys) if has_non_usd else None
                usd_text = f"≈ {_fmt(usd_val)} $" if usd_val is not None else ""
                row_controls = [
                    ft.Container(width=16),
                    ft.Text("•", size=8, color=_TEXT),
                    ft.Container(width=4),
                    ft.Text(pname, size=8, color=_TEXT, expand=True),
                    _amt_cell(sup_text),
                ]
                if has_non_usd:
                    row_controls.append(_amt_cell(usd_text, color=_TEXT))
                prod_rows.append(
                    ft.Row(
                        row_controls,
                        spacing=0,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )
            cat_amt: dict = {}
            for ccys in prods.values():
                for c, v in ccys.items():
                    cat_amt[c] = cat_amt.get(c, 0.0) + v
            cat_sup_text = _sup_ccy_text(cat_amt)
            cat_usd_val = _to_usd(cat_amt) if has_non_usd else None
            cat_usd_text = f"≈ {_fmt(cat_usd_val)} $" if cat_usd_val is not None else ""
            hdr_controls = [
                ft.Text(cat_name, size=9, color=_TEXT, weight="bold", expand=True),
                _amt_cell(cat_sup_text, bold=True, color=_TEXT),
            ]
            if has_non_usd:
                hdr_controls.append(_amt_cell(cat_usd_text, bold=True, color=_TEXT))
            cat_rows.append(
                ft.Column(
                    [
                        ft.Row(
                            hdr_controls,
                            spacing=0,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        *prod_rows,
                    ],
                    spacing=2,
                )
            )
        try:
            y = int(month[:4])
            m_i = int(month[5:7])
            month_label = f"{self.MONTH_NAMES.get(m_i, str(m_i))}, {y}"
        except Exception:
            month_label = month

        # Header totals
        if not ccy_totals:
            grand_sup_text = "Немає даних"
            grand_usd_text = ""
        else:
            grand_sup_text = _sup_ccy_text(ccy_totals)
            grand_usd_val = _to_usd(ccy_totals) if has_non_usd else None
            grand_usd_text = (
                f"≈ {_fmt(grand_usd_val)} $" if grand_usd_val is not None else ""
            )

        hdr_row_controls = [
            ft.Text(f"Сплата — {month_label}", size=9, color=_CHIP_T, weight="bold"),
            ft.Container(expand=True),
            ft.Text("Загальна сума:", size=9, color=_CHIP_T),
            _amt_cell(grand_sup_text, bold=True, color=_TEXT),
        ]
        if has_non_usd:
            hdr_row_controls.append(_amt_cell(grand_usd_text, bold=True, color=_TEXT))

        return ft.Container(
            bgcolor=_SOFT_I,
            border=ft.border.all(1, _BORDER),
            border_radius=6,
            padding=ft.padding.symmetric(horizontal=8, vertical=6),
            margin=ft.margin.only(bottom=4, right=35),
            content=ft.Column(
                [
                    ft.Row(
                        hdr_row_controls,
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    *(
                        [ft.Divider(height=1, color=_BORDER, thickness=1)] + cat_rows
                        if cat_rows
                        else []
                    ),
                ],
                spacing=4,
                tight=True,
            ),
        )

    def _update_payment_summary_panel(self) -> None:
        host = self._payment_summary_panel_host
        if host is None:
            return
        try:
            has_page = bool(getattr(host, "page", None))
        except RuntimeError:
            has_page = False
        if not has_page:
            return
        panel = self._build_payment_summary_panel()
        host.controls = [panel] if panel is not None else []
        with contextlib.suppress(Exception):
            host.update()

    def _build_filter_bar(self) -> ft.Container:
        filter_width = 108
        button_text_size = 8  # compact size
        popup_text_size = 8  # smaller option text in opened menu

        def _to_options(options: list[tuple[str, str]]) -> list[tuple[str, str]]:
            out = []
            for key, label in options or []:
                out.append((str(key or ""), str(label or "")))
            return out

        def _menu_filter(
            caption: str,
            selected_value: str,
            options: list[tuple[str, str]],
            on_pick,
        ) -> ft.PopupMenuButton:
            normalized = _to_options(options)
            selected_key = str(selected_value or "")
            selected_label = next(
                (txt for key, txt in normalized if key == selected_key),
                (normalized[0][1] if normalized else "-"),
            )

            items = []
            for key, txt in normalized:
                items.append(
                    ft.PopupMenuItem(
                        content=ft.Text(
                            txt,
                            size=popup_text_size,
                            no_wrap=False,
                            max_lines=3,
                            overflow=ft.TextOverflow.VISIBLE,
                        ),
                        on_click=(lambda _e, k=key: on_pick(k)),
                    )
                )
            if not items:
                items = [
                    ft.PopupMenuItem(
                        disabled=True,
                        content=ft.Text("Немає варіантів", size=popup_text_size),
                    )
                ]

            return ft.PopupMenuButton(
                icon=None,
                menu_position=ft.PopupMenuPosition.UNDER,
                items=items,
                content=ft.Container(
                    width=filter_width,
                    height=35,
                    border=ft.border.all(1, _BORDER),
                    border_radius=8,
                    bgcolor=_ALT,
                    padding=ft.padding.symmetric(horizontal=7, vertical=4),
                    content=ft.Column(
                        [
                            ft.Text(
                                caption,
                                size=9,
                                color=_TEXT,
                                text_align=ft.TextAlign.CENTER,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                selected_label,
                                size=button_text_size,
                                color=_TEXT,
                                text_align=ft.TextAlign.CENTER,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                        ],
                        spacing=2,
                        tight=True,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ),
            )

        facets = self._orders_facets()
        self._filter_level_node_ids = {}

        self._level_filter_dd_by_key = {}

        delivery_options = [("all", self.t("orders.filter.all_deliveries", "Всі доставки"))] + [
            (did, dname)
            for did, dname in sorted(
                facets["deliveries"].items(), key=lambda kv: (kv[1] or "").lower()
            )
        ]
        supplier_options = [("all", self.t("orders.filter.all_suppliers", "Всі постачальники"))] + [
            (sid, (sname or "")[:44])
            for sid, sname in sorted(
                facets["suppliers"].items(), key=lambda kv: (kv[1] or "").lower()
            )
        ]
        category_options = [("all", self.t("orders.filter.all_categories", "Всі категорії"))] + [
            (cid, (cname or "")[:44])
            for cid, cname in sorted(
                facets["categories"].items(), key=lambda kv: (kv[1] or "").lower()
            )
        ]
        product_options = [("all", self.t("orders.filter.all_products", "Всі товари"))] + [
            (pid, (pname or "")[:38])
            for pid, pname in sorted(
                facets["products"].items(), key=lambda kv: (kv[1] or "").lower()
            )
        ]

        def on_delivery(key: str):
            self._filter_delivery_id = str(key or "all") or "all"
            self._schedule_filter_render()

        def on_product(key: str):
            self._filter_product_id = str(key or "all") or "all"
            self._schedule_filter_render()

        def on_supplier(key: str):
            self._filter_supplier_id = str(key or "all") or "all"
            self._schedule_filter_render()

        def on_category(key: str):
            self._filter_category_id = str(key or "all") or "all"
            self._schedule_filter_render()

        supplier_dd = _menu_filter(
            self.t("orders.filter.supplier", "Постачальник"), self._filter_supplier_id, supplier_options, on_supplier
        )
        category_dd = _menu_filter(
            self.t("orders.filter.category", "Категорія"), self._filter_category_id, category_options, on_category
        )
        delivery_dd = _menu_filter(
            self.t("orders.filter.delivery", "Доставка"), self._filter_delivery_id, delivery_options, on_delivery
        )
        product_dd = _menu_filter(
            self.t("orders.filter.product", "Товар"), self._filter_product_id, product_options, on_product
        )

        self._status_filter_dd = None
        self._supplier_filter_dd = None
        self._delivery_filter_dd = None
        self._product_filter_dd = None
        self._category_filter_dd = None
        self._payment_filter_dd = None
        self._submission_filter_dd = None

        all_orders = self._orders or []

        def _month_popup_button(
            caption: str,
            selected_value: str,
            options: list[tuple[str, str]],
            on_pick,
        ) -> ft.PopupMenuButton:
            is_active = selected_value != "all"
            month_map = {k: v for k, v in options}
            label = month_map.get(selected_value, self.t("orders.filter.all_months", "Всі місяці"))
            items = [
                ft.PopupMenuItem(
                    content=ft.Text(lbl, size=popup_text_size),
                    on_click=(lambda _e, k=key: on_pick(k)),
                )
                for key, lbl in options
            ]
            return ft.PopupMenuButton(
                icon=None,
                menu_position=ft.PopupMenuPosition.UNDER,
                items=items,
                content=ft.Container(
                    width=filter_width,
                    height=35,
                    border=ft.border.all(1, _BORDER),
                    border_radius=8,
                    bgcolor=_BORDER if is_active else _ALT,
                    padding=ft.padding.symmetric(horizontal=7, vertical=4),
                    content=ft.Column(
                        [
                            ft.Text(
                                caption,
                                size=9,
                                color=_TEXT if is_active else _MUTED,
                                text_align=ft.TextAlign.CENTER,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                label,
                                size=button_text_size,
                                color=_TEXT,
                                text_align=ft.TextAlign.CENTER,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                        ],
                        spacing=2,
                        tight=True,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ),
            )

        def on_receive_month(key: str):
            self._filter_receive_month = str(key or "all") or "all"
            self._schedule_filter_render()

        def on_accept_month(key: str):
            self._filter_accept_month = str(key or "all") or "all"
            self._schedule_filter_render()

        def on_payment_month(key: str):
            self._set_payment_month_filter(key)

        receive_month_dd = _month_popup_button(
            self.t("orders.filter.receive_month", "Отримання. Місяць"),
            self._filter_receive_month,
            self._receive_month_options(all_orders),
            on_receive_month,
        )
        accept_month_dd = _month_popup_button(
            self.t("orders.filter.accept_month", "Прийомка. Місяць"),
            self._filter_accept_month,
            self._accept_month_options(all_orders),
            on_accept_month,
        )
        payment_month_dd = _month_popup_button(
            self.t("orders.filter.payment_month", "Сплата. Місяць"),
            self._filter_payment_month,
            self._payment_month_options(all_orders),
            on_payment_month,
        )

        filters_label = ft.Container(
            content=ft.Text(self.t("orders.filter.label", "Фільтри:"), size=9, color=_TEXT),
            padding=ft.padding.only(right=2),
        )

        all_controls = [
            filters_label,
            receive_month_dd,
            accept_month_dd,
            payment_month_dd,
            supplier_dd,
            category_dd,
            delivery_dd,
            product_dd,
        ]

        return ft.Container(
            border_radius=6,
            bgcolor=_ALT,
            padding=ft.padding.symmetric(horizontal=4, vertical=0),
            content=ft.Row(
                all_controls,
                spacing=4,
                wrap=True,
                alignment=ft.MainAxisAlignment.START,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _refresh_filter_dropdown_options(self):
        if self._filter_bar_host is not None:
            try:
                has_page = bool(getattr(self._filter_bar_host, "page", None))
            except RuntimeError:
                has_page = False
            if not has_page:
                return
            self._filter_bar_host.controls = [self._build_filter_bar()]
            with contextlib.suppress(Exception):
                self._filter_bar_host.update()
            return

        if not self._supplier_filter_dd:
            return

        popup_text_size = 8

        def _opt(key: str, text: str):
            label = str(text or "")
            return ft.dropdown.Option(
                key,
                label,
                content=ft.Text(label, size=popup_text_size),
            )

        facets = self._orders_facets()
        enabled_level_rows = facets.get("enabled_level_rows") or []

        self._supplier_filter_dd.options = [_opt("all", self.t("orders.filter.all_suppliers", "Всі постачальники"))] + [
            _opt(sid, (sname or "")[:44])
            for sid, sname in sorted(
                facets["suppliers"].items(), key=lambda kv: (kv[1] or "").lower()
            )
        ]
        if not any(
            str(getattr(opt, "key", "")) == str(self._filter_supplier_id)
            for opt in (self._supplier_filter_dd.options or [])
        ):
            self._filter_supplier_id = "all"
        self._supplier_filter_dd.value = self._filter_supplier_id

        if self._delivery_filter_dd:
            self._delivery_filter_dd.options = [_opt("all", self.t("orders.filter.all_deliveries", "Всі доставки"))] + [
                _opt(did, dname)
                for did, dname in sorted(
                    facets["deliveries"].items(), key=lambda kv: (kv[1] or "").lower()
                )
            ]
            if not any(
                str(getattr(opt, "key", "")) == str(self._filter_delivery_id)
                for opt in (self._delivery_filter_dd.options or [])
            ):
                self._filter_delivery_id = "all"
            self._delivery_filter_dd.value = self._filter_delivery_id

        if self._product_filter_dd:
            self._product_filter_dd.options = [_opt("all", self.t("orders.filter.all_products", "Всі товари"))] + [
                _opt(pid, (pname or "")[:38])
                for pid, pname in sorted(
                    facets["products"].items(), key=lambda kv: (kv[1] or "").lower()
                )
            ]
            if not any(
                str(getattr(opt, "key", "")) == str(self._filter_product_id)
                for opt in (self._product_filter_dd.options or [])
            ):
                self._filter_product_id = "all"
            self._product_filter_dd.value = self._filter_product_id

        if self._category_filter_dd:
            self._category_filter_dd.options = [_opt("all", self.t("orders.filter.all_categories", "Всі категорії"))] + [
                _opt(cid, (cname or "")[:44])
                for cid, cname in sorted(
                    facets["categories"].items(), key=lambda kv: (kv[1] or "").lower()
                )
            ]
            if not any(
                str(getattr(opt, "key", "")) == str(self._filter_category_id)
                for opt in (self._category_filter_dd.options or [])
            ):
                self._filter_category_id = "all"
            self._category_filter_dd.value = self._filter_category_id

        if self._status_filter_dd:
            visible_statuses = [s for s in self.STATUSES if s in facets["statuses"]]
            self._status_filter_dd.options = [_opt("all", self.t("orders.filter.all_statuses", "Всі статуси"))] + [
                _opt(s, self.STATUS_LABELS.get(s, s)) for s in visible_statuses
            ]
            if not any(
                str(getattr(opt, "key", "")) == str(self._filter_status)
                for opt in (self._status_filter_dd.options or [])
            ):
                self._filter_status = "all"
            self._status_filter_dd.value = self._filter_status

        if self._payment_filter_dd:
            self._payment_filter_dd.options = [_opt("all", self.t("orders.filter.all_payments", "Всі оплати"))] + [
                _opt(k, self.PAYMENT_LABELS.get(k, k))
                for k in ["unpaid", "advance", "transferred", "paid"]
                if k in facets["payments"]
            ]
            if not any(
                str(getattr(opt, "key", "")) == str(self._filter_payment)
                for opt in (self._payment_filter_dd.options or [])
            ):
                self._filter_payment = "all"
            self._payment_filter_dd.value = self._filter_payment

        if self._submission_filter_dd:
            self._submission_filter_dd.options = [_opt("all", self.t("orders.filter.all_1c", "Всі статуси обліку"))] + [
                _opt(k, self.SUBMISSION_LABELS.get(k, k))
                for k in ["not_submitted", "submitted"]
                if k in facets["submissions"]
            ]
            if not any(
                str(getattr(opt, "key", "")) == str(self._filter_submission)
                for opt in (self._submission_filter_dd.options or [])
            ):
                self._filter_submission = "all"
            self._submission_filter_dd.value = self._filter_submission

        seen_level_keys = set()
        for row in enabled_level_rows:
            try:
                pos = int(row.get("position") or 0)
            except Exception:
                continue
            if pos < 1 or pos > 6:
                continue
            title = str(row.get("title") or f"Рівень {pos}").strip() or f"Рівень {pos}"
            level_key = self._level_filter_key(pos, title)
            seen_level_keys.add(level_key)
            dd = (self._level_filter_dd_by_key or {}).get(level_key)
            if not dd:
                continue
            nodes_for_level = (facets.get("level_nodes") or {}).get(level_key) or {}
            dd.label = title
            dd.options = [_opt("all", self.t("orders.filter.all_prefix", "Всі: {title}").format(title=title))] + [
                _opt(node_id, (node_name or "")[:44])
                for node_id, node_name in sorted(
                    nodes_for_level.items(), key=lambda kv: (kv[1] or "").lower()
                )
            ]
            if not any(
                str(getattr(opt, "key", ""))
                == str((self._filter_level_node_ids or {}).get(level_key, "all"))
                for opt in (dd.options or [])
            ):
                self._filter_level_node_ids[level_key] = "all"
            dd.value = str(
                (self._filter_level_node_ids or {}).get(level_key, "all") or "all"
            )

        for key in list((self._filter_level_node_ids or {}).keys()):
            if key not in seen_level_keys:
                self._filter_level_node_ids.pop(key, None)

        for ctrl in [
            self._supplier_filter_dd,
            self._delivery_filter_dd,
            self._product_filter_dd,
            self._category_filter_dd,
            self._status_filter_dd,
            self._payment_filter_dd,
            self._submission_filter_dd,
            *list((self._level_filter_dd_by_key or {}).values()),
        ]:
            try:
                if getattr(ctrl, "page", None):
                    ctrl.update()
            except Exception:
                pass

    def _render_tree_general(self, filtered: list, force_full: bool = False) -> None:
        """Render orders in 'Загальні' mode: collapsible manager sections (collapsed by default)."""
        import contextlib as _cl

        self._tree_host.controls.clear()
        self._supplier_section_controls = {}
        self._order_row_controls = {}
        self._order_fast_refresh = {}
        self._order_month_rows_col = {}
        self._order_year_body_col = {}
        self._order_year_month_numbers = {}
        self._month_collapsible_by_key = {}
        self._year_collapsible_by_key = {}
        self._supplier_collapsible_by_key = {}

        # Group by manager (preserve order from _general_manager_ids)
        mgr_order = list(dict.fromkeys(self._general_manager_ids or []))
        mgr_orders_map: dict[str, list] = {mid: [] for mid in mgr_order}
        for o in filtered:
            mid = str(o.get("_gm_manager_id") or o.get("manager_id") or "").strip()
            if mid not in mgr_orders_map:
                mgr_orders_map[mid] = []
                mgr_order.append(mid)
            mgr_orders_map[mid].append(o)

        for mid in mgr_order:
            mgr_orders = mgr_orders_map.get(mid, [])
            mgr_name = (self._general_manager_names or {}).get(mid, mid)
            order_count = len({str(o.get("id") or id(o)) for o in mgr_orders})

            # Build supplier sections inside the body (collapsed by default)
            sup_map: dict = {}
            for o in mgr_orders:
                sid = (o.get("supplier_id") or "") or "__no_supplier__"
                sup_map.setdefault(sid, []).append(o)

            sup_section_controls: list = []
            for sid, sup_orders in sup_map.items():
                sup_row = next((s for s in self._suppliers if s.get("id") == sid), None)
                sup_name = (
                    (sup_row.get("name") if sup_row else None)
                    or sup_orders[0].get("supplier_name")
                    or self.t("orders.no_supplier", "Без постачальника")
                )
                section = self._build_supplier_section(sup_name, sup_orders)
                composite_sid = f"{mid}::{sid}"
                self._supplier_section_controls[composite_sid] = section
                sup_section_controls.append(section)

            body_col = ft.Column(
                sup_section_controls
                or [
                    ft.Container(
                        content=ft.Text(self.t("orders.empty_state", "Немає замовлень"), size=11, color=_TEXT),
                        padding=ft.padding.symmetric(horizontal=16, vertical=8),
                    )
                ],
                spacing=4,
                visible=False,  # collapsed by default
            )

            arrow_icon = ft.Icon(ft.Icons.EXPAND_MORE, size=18, color=_CHIP_T)
            count_badge = (
                ft.Container(
                    bgcolor=_SOFT_I,
                    border_radius=10,
                    padding=ft.padding.symmetric(horizontal=8, vertical=2),
                    content=ft.Text(
                        str(order_count),
                        size=10,
                        color=_CHIP_T,
                        weight=ft.FontWeight.W_600,
                    ),
                )
                if order_count > 0
                else ft.Container(width=0)
            )

            def _toggle(e, bc=body_col, ai=arrow_icon):
                bc.visible = not bc.visible
                ai.name = ft.Icons.EXPAND_LESS if bc.visible else ft.Icons.EXPAND_MORE
                with _cl.suppress(Exception):
                    e.control.parent.update()

            header = ft.Container(
                bgcolor=_SOFT_I,
                border=ft.border.all(1, _BORDER),
                border_radius=ft.border_radius.only(top_left=6, top_right=6),
                padding=ft.padding.symmetric(horizontal=10, vertical=6),
                on_click=_toggle,
                content=ft.Row(
                    [
                        arrow_icon,
                        ft.Text(
                            mgr_name,
                            size=12,
                            weight=ft.FontWeight.W_700,
                            color=_CHIP_T,
                            expand=True,
                        ),
                        count_badge,
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            )

            mgr_section = ft.Container(
                content=ft.Column([header, body_col], spacing=0),
                margin=ft.margin.only(bottom=6),
            )
            self._tree_host.controls.append(mgr_section)

    def _schedule_full_rerender(self) -> None:
        page = self._page()
        if not page or not hasattr(page, "run_task"):
            return

        async def _do_rerender():
            await self._async_render_initial()

        with contextlib.suppress(Exception):
            page.run_task(_do_rerender)

    def _render_tree(self, force_full=False, affected_supplier_ids=None):
        try:
            if (not force_full) and time.time() < float(
                getattr(self, "_suppress_auto_render_until_ts", 0.0) or 0.0
            ):
                return
            if not force_full:
                if getattr(self, "_dialog_editing_order_id", "") or (
                    getattr(self, "_unsaved_local_order_ids", None) or set()
                ):
                    with contextlib.suppress(Exception):
                        self._debug_print("render suppressed: active edits present")
                    return
            if force_full:
                self._debug_print(
                    f"_render_tree force_full={force_full} affected={affected_supplier_ids or []}"
                )
            self._refresh_filter_dropdown_options()
            self._update_counters()
            self._update_payment_summary_panel()
            filtered = self._filtered_orders()
            if self._has_active_filters():
                self._tree_host.controls.clear()
                self._supplier_section_controls = {}
                self._order_row_controls = {}
                self._order_fast_refresh = {}
                self._order_month_rows_col = {}
                self._order_year_body_col = {}
                self._order_year_month_numbers = {}
                self._tree_host.controls.append(
                    self._build_flat_filtered_container(filtered)
                )
                self._update_load_more_button(filtered_count=len(filtered))
                with contextlib.suppress(Exception):
                    self._tree_host.update()
                return

            affected_ids = {
                sid for sid in (affected_supplier_ids or []) if sid is not None
            }
            if force_full or not self._supplier_section_controls:
                self._tree_host.controls.clear()
                self._supplier_section_controls = {}
                self._order_row_controls = {}
                self._order_fast_refresh = {}
                self._order_month_rows_col = {}
                self._order_year_body_col = {}
                self._order_year_month_numbers = {}
                self._month_collapsible_by_key = {}
                self._year_collapsible_by_key = {}
                self._supplier_collapsible_by_key = {}

            if not filtered:
                self._tree_host.controls.clear()
                self._supplier_section_controls = {}
                self._order_row_controls = {}
                self._order_fast_refresh = {}
                self._order_month_rows_col = {}
                self._order_year_body_col = {}
                self._order_year_month_numbers = {}
                self._month_collapsible_by_key = {}
                self._year_collapsible_by_key = {}
                self._supplier_collapsible_by_key = {}
                self._tree_host.controls.append(
                    ft.Container(
                        padding=14,
                        border=ft.border.all(1, _BORDER),
                        border_radius=8,
                        bgcolor=_ALT,
                        content=ft.Text(
                            "Ще немає доданих замовлень",
                            color=_TEXT,
                            size=12,
                            text_align=ft.TextAlign.CENTER,
                        ),
                    )
                )
                page = self._page()
                if page:
                    with contextlib.suppress(Exception):
                        self._tree_host.update()
                return

            # In general mode: group by manager first, then by supplier within each manager
            if self._general_mode:
                self._render_tree_general(filtered, force_full)
                with contextlib.suppress(Exception):
                    self._tree_host.update()
                return

            supplier_map: dict = {}
            for o in filtered:
                sid = (o.get("supplier_id") or "") or "__no_supplier__"
                supplier_map.setdefault(sid, []).append(o)

            if force_full:
                self._order_row_controls = {}
                self._order_fast_refresh = {}
                self._order_month_rows_col = {}
                self._order_year_body_col = {}
                self._order_year_month_numbers = {}
                ordered_supplier_ids = list(supplier_map.keys())
            else:
                ordered_supplier_ids = list(supplier_map.keys())

            existing_ids = set(self._supplier_section_controls.keys())
            current_ids = set(ordered_supplier_ids)
            for removed_id in [sid for sid in existing_ids if sid not in current_ids]:
                ctrl = self._supplier_section_controls.pop(removed_id, None)
                if ctrl in self._tree_host.controls:
                    self._tree_host.controls.remove(ctrl)
                self._supplier_collapsible_by_key.pop(removed_id, None)
                for key in list(self._year_collapsible_by_key.keys()):
                    if key.startswith(f"{removed_id}:"):
                        self._year_collapsible_by_key.pop(key, None)
                for key in list(self._month_collapsible_by_key.keys()):
                    if key.startswith(f"{removed_id}:"):
                        self._month_collapsible_by_key.pop(key, None)

            for sid in ordered_supplier_ids:
                sup_orders = supplier_map.get(sid, [])
                sup_row = next((s for s in self._suppliers if s.get("id") == sid), None)
                sup_name = (
                    (sup_row.get("name") if sup_row else None)
                    or sup_orders[0].get("supplier_name")
                    or self.t("orders.no_supplier", "Без постачальника")
                )
                if force_full or sid not in self._supplier_section_controls:
                    self._supplier_collapsible_by_key.pop(sid, None)
                    for key in list(self._year_collapsible_by_key.keys()):
                        if key.startswith(f"{sid}:"):
                            self._year_collapsible_by_key.pop(key, None)
                    for key in list(self._month_collapsible_by_key.keys()):
                        if key.startswith(f"{sid}:"):
                            self._month_collapsible_by_key.pop(key, None)
                    section = self._build_supplier_section(sup_name, sup_orders)
                    self._supplier_section_controls[sid] = section
                    if section not in self._tree_host.controls:
                        self._tree_host.controls.append(section)
                elif sid in affected_ids:
                    # Clear stale month-level refs so future inject() uses fresh controls.
                    for _k in list(self._order_month_rows_col.keys()):
                        if _k.startswith(f"{sid}:"):
                            self._order_month_rows_col.pop(_k, None)
                    for _k in list(self._order_year_body_col.keys()):
                        if _k.startswith(f"{sid}:"):
                            self._order_year_body_col.pop(_k, None)
                    for _k in list(self._order_year_month_numbers.keys()):
                        if _k.startswith(f"{sid}:"):
                            self._order_year_month_numbers.pop(_k, None)
                    new_section = self._build_supplier_section(sup_name, sup_orders)
                    old_section = self._supplier_section_controls.get(sid)
                    try:
                        index = self._tree_host.controls.index(old_section)
                        self._tree_host.controls[index] = new_section
                    except Exception:
                        if new_section not in self._tree_host.controls:
                            self._tree_host.controls.append(new_section)
                    self._supplier_section_controls[sid] = new_section

            self._update_load_more_button(filtered_count=len(filtered))

            page = self._page()
            if page:
                with contextlib.suppress(Exception):
                    self._tree_host.update()
                self._schedule_restore_scroll_positions()
        except Exception as ex:
            self._tree_host.controls = [
                ft.Container(
                    padding=16,
                    border=ft.border.all(1, _ERR),
                    border_radius=10,
                    bgcolor=_ERR_BG,
                    content=ft.Text(f"Помилка відображення: {ex}", color=_ERR),
                )
            ]
            page = self._page()
            if page:
                with contextlib.suppress(Exception):
                    self._tree_host.update()

    def _schedule_filter_render(self):
        if not hasattr(self, "_filter_render_gen"):
            self._filter_render_gen = 0
        self._filter_render_gen += 1
        gen = self._filter_render_gen

        page = self._page()
        if not page:
            return

        async def _deferred(_gen=gen):
            await asyncio.sleep(0.03)
            if getattr(self, "_filter_render_gen", 0) == _gen:
                await self._async_render_initial()

        with contextlib.suppress(Exception):
            page.run_task(_deferred)

    def _on_content_scroll(self, e):
        with contextlib.suppress(Exception):
            self._content_v_offset = float(getattr(e, "pixels", 0) or 0)

    def _on_table_h_scroll(self, table_key: str, e):
        key = str(table_key or "").strip()
        if not key:
            return
        with contextlib.suppress(Exception):
            self._table_h_offsets[key] = float(getattr(e, "pixels", 0) or 0)

    def _get_table_content_width(self) -> int:
        return max(320, sum(int(c.get("width") or 0) for c in self._table_col_specs()))

    def _inject_pinned_h_scrollbar(self):
        page = self._page()
        _slot = getattr(page, "_view_h_scrollbar_slot", None) if page else None
        if _slot is None:
            return
        tw = self._get_table_content_width()
        self._pinned_h_scrollbar = ft.Row(
            [ft.Container(width=tw, height=1, bgcolor="transparent")],
            scroll=ft.ScrollMode.ALWAYS,
            height=16,
        )
        self._pinned_h_scrollbar.on_scroll = self._on_pinned_h_scroll
        with contextlib.suppress(Exception):
            _slot.controls[:] = [self._pinned_h_scrollbar]
            _slot.update()

    def _on_pinned_h_scroll(self, e):
        offset = float(getattr(e, "pixels", 0) or 0)
        self._h_scrollbar_offset = offset
        if self._tables_outer_container is None:
            return
        page = self._page()
        if not page:
            return
        # Coalesce rapid scroll events — only one pending sync task at a time.
        # Without this, 60+ tasks queue up per second and the event loop stalls.
        if self._h_sync_pending:
            return
        self._h_sync_pending = True
        with contextlib.suppress(Exception):
            page.run_task(self._sync_tables_coalesced)

    async def _sync_tables_coalesced(self):
        """Single coalesced sync task: resets pending flag immediately so next event can queue."""
        self._h_sync_pending = False
        offset = self._h_scrollbar_offset
        await self._sync_tables_to_h_offset(offset)

    def _on_tables_outer_h_scroll(self, e):
        pass  # one-way sync only — pinned scrollbar is the source of truth

    async def _sync_tables_to_h_offset(self, offset: float):
        with contextlib.suppress(Exception):
            if self._tables_outer_container:
                await self._tables_outer_container.scroll_to(offset=offset, duration=0)

    async def _sync_pinned_to_h_offset(self, offset: float):
        with contextlib.suppress(Exception):
            if self._pinned_h_scrollbar:
                await self._pinned_h_scrollbar.scroll_to(offset=offset, duration=0)

    def _schedule_restore_scroll_positions(self):
        page = self._page()
        if not page:
            return

        async def _restore():
            await self._restore_scroll_positions_async()

        with contextlib.suppress(Exception):
            page.run_task(_restore)

    async def _restore_scroll_positions_async(self):
        v_off = float(getattr(self, "_content_v_offset", 0.0) or 0.0)
        h_off = float(getattr(self, "_h_scrollbar_offset", 0.0) or 0.0)
        await asyncio.sleep(0.02)
        try:
            if hasattr(self.content_area, "scroll_to"):
                await self.content_area.scroll_to(offset=v_off, duration=0)
        except Exception:
            pass
        if h_off > 0:
            await self._sync_tables_to_h_offset(h_off)
            await self._sync_pinned_to_h_offset(h_off)

    def _build_flat_filtered_container(self, orders: list) -> ft.Container:
        if not orders:
            return ft.Container(
                padding=14,
                border=ft.border.all(1, _BORDER),
                border_radius=8,
                bgcolor=_ALT,
                content=ft.Text(
                    "Немає результатів за фільтрами",
                    color=_TEXT,
                    size=12,
                    text_align=ft.TextAlign.CENTER,
                ),
            )

        table = self._build_orders_table(orders, table_key="filtered_flat")
        return ft.Container(
            border=ft.border.all(1, _BORDER),
            border_radius=8,
            bgcolor=_SOFT_I,
            padding=6,
            clip_behavior=ft.ClipBehavior.NONE,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text(
                                f"Режим фільтрації: {len(orders)} замовлень в одній таблиці",
                                size=11,
                                color=_TEXT,
                                weight="w500",
                            )
                        ]
                    ),
                    ft.Divider(height=4, color=_BORDER),
                    table,
                ],
                spacing=4,
            ),
        )

    def _update_load_more_button(self, filtered_count=None):
        if self._load_more_btn is None:
            return
        self._load_more_btn.disabled = self._orders_loading
        self._load_more_btn.text = (
            "Завантаження..." if self._orders_loading else "Завантажити ще"
        )
        self._load_more_btn.visible = bool(self._orders_has_more)
        self._load_more_btn.height = 34 if self._load_more_btn.visible else 0
        if filtered_count is not None:
            self._load_more_btn.tooltip = f"Завантажено замовлень: {filtered_count}"

    def _set_page_loading(self, loading: bool):
        is_loading = bool(loading)
        self._page_loading = is_loading
        self._debug_print(f"[set_page_loading] loading={is_loading}")
        indicator = getattr(self, "_header_orders_loading_indicator", None)
        if indicator is not None:
            indicator.visible = is_loading
            try:
                indicator.update()
                self._debug_print("[set_page_loading] indicator.update OK")
            except Exception as _e:
                self._debug_print(f"[set_page_loading] indicator.update FAILED: {_e}")
        if self._loading_box is not None:
            self._loading_box.visible = False
            self._loading_box.on_click = None
            self._loading_box.disabled = True
            with contextlib.suppress(Exception):
                self._loading_box.update()
        for ctrl in [
            getattr(self, "_open_order_btn", None),
            getattr(self, "_quick_add_btn", None),
            getattr(self, "_table_btn", None),
        ]:
            if ctrl is None:
                continue
            ctrl.disabled = is_loading
            _ctrl_has_page = getattr(ctrl, "page", None) is not None
            try:
                ctrl.update()
                self._debug_print(
                    f"[set_page_loading] btn.update OK disabled={is_loading} has_page={_ctrl_has_page}"
                )
            except Exception as _e:
                self._debug_print(
                    f"[set_page_loading] btn.update FAILED disabled={is_loading} has_page={_ctrl_has_page} err={_e}"
                )

    async def _async_render_initial(self, build_all: bool = False):
        _render_t0 = time.monotonic()
        self._debug_print(
            f"[render_initial] START build_all={build_all} orders={len(self._orders or [])}"
        )
        _indicator = getattr(self, "_header_orders_loading_indicator", None)
        _own_spinner = _indicator is not None and not self._page_loading
        if _own_spinner:
            _ltext = (
                _indicator.controls[1]
                if len(getattr(_indicator, "controls", [])) > 1
                else None
            )
            if _ltext is not None:
                _ltext.value = "Оновлення..."
            _indicator.visible = True
            with contextlib.suppress(Exception):
                _indicator.update()
        self._refresh_filter_dropdown_options()
        self._update_counters()
        await asyncio.sleep(0)  # yield after counters update

        filtered = self._filtered_orders()
        self._debug_print(
            f"[render_initial] filtered={len(filtered)} active_filters={self._has_active_filters()}"
        )

        if self._has_active_filters():
            self._tree_host.controls.clear()
            self._supplier_section_controls = {}
            self._order_row_controls = {}
            self._order_fast_refresh = {}
            self._order_month_rows_col = {}
            self._month_counter_row_by_key = {}
            self._year_counter_row_by_key = {}
            self._supplier_counter_row_by_key = {}
            self._order_year_body_col = {}
            self._order_year_month_numbers = {}
            self._tree_host.controls.append(
                self._build_flat_filtered_container(filtered)
            )
            self._update_load_more_button(filtered_count=len(filtered))
            with contextlib.suppress(Exception):
                self._tree_host.update()
            if _own_spinner and _indicator is not None:
                _indicator.visible = False
                with contextlib.suppress(Exception):
                    _indicator.update()
            return

        if not filtered:
            self._tree_host.controls.clear()
            self._supplier_section_controls = {}
            self._order_row_controls = {}
            self._order_fast_refresh = {}
            self._order_month_rows_col = {}
            self._month_counter_row_by_key = {}
            self._year_counter_row_by_key = {}
            self._supplier_counter_row_by_key = {}
            self._order_year_body_col = {}
            self._order_year_month_numbers = {}
            self._tree_host.controls.append(
                ft.Container(
                    padding=14,
                    border=ft.border.all(1, _BORDER),
                    border_radius=8,
                    bgcolor=_ALT,
                    content=ft.Text(
                        "Ще немає доданих замовлень",
                        color=_TEXT,
                        size=12,
                        text_align=ft.TextAlign.CENTER,
                    ),
                )
            )
            with contextlib.suppress(Exception):
                self._tree_host.update()
            if _own_spinner and _indicator is not None:
                _indicator.visible = False
                with contextlib.suppress(Exception):
                    _indicator.update()
            return

        supplier_map: dict = {}
        for o in filtered:
            sid = (o.get("supplier_id") or "") or "__no_supplier__"
            supplier_map.setdefault(sid, []).append(o)

        self._tree_host.controls.clear()
        self._supplier_section_controls = {}
        self._order_row_controls = {}
        self._order_fast_refresh = {}
        self._order_month_rows_col = {}
        self._month_counter_row_by_key = {}
        self._year_counter_row_by_key = {}
        self._supplier_counter_row_by_key = {}
        self._order_year_body_col = {}
        self._order_year_month_numbers = {}
        self._month_collapsible_by_key = {}
        self._year_collapsible_by_key = {}
        self._supplier_collapsible_by_key = {}

        supplier_ids = list(supplier_map.keys())
        for idx, sid in enumerate(supplier_ids):
            sup_orders = supplier_map[sid]
            sup_row = next((s for s in self._suppliers if s.get("id") == sid), None)
            sup_name = (
                (sup_row.get("name") if sup_row else None)
                or sup_orders[0].get("supplier_name")
                or self.t("orders.no_supplier", "Без постачальника")
            )
            self._debug_print(
                f"render supplier {sid or '__no_supplier__'} start orders={len(sup_orders)}"
            )
            start_section = time.monotonic()
            section = await asyncio.to_thread(
                self._build_supplier_section, sup_name, sup_orders
            )
            self._debug_print(
                f"render supplier {sid or '__no_supplier__'} finished in {time.monotonic() - start_section:.3f}s"
            )
            self._supplier_section_controls[sid] = section
            self._tree_host.controls.append(section)
            with contextlib.suppress(Exception):
                self._tree_host.update()
            await asyncio.sleep(
                0
            )  # yield so Flutter processes this before the next supplier

        self._update_load_more_button(filtered_count=len(filtered))
        with contextlib.suppress(Exception):
            self._tree_host.update()
        await self._restore_scroll_positions_async()

        _all_month_items = list(self._month_collapsible_by_key.items())
        if build_all:
            _lazy_months = [
                (k, c)
                for k, c in _all_month_items
                if getattr(c, "_has_lazy_body", False)
            ]
        else:
            _lazy_months = [
                (k, c)
                for k, c in _all_month_items
                if getattr(c, "_initially_open", False)
                and getattr(c, "_has_lazy_body", False)
            ]
        self._debug_print(
            f"[render_initial] total={len(_all_month_items)} to_build={len(_lazy_months)} build_all={build_all}"
        )
        _tables_t = time.monotonic()
        for _mi, (_month_key, _month_ctrl) in enumerate(_lazy_months):
            self._debug_print(
                f"[render_initial] building {_mi+1}/{len(_lazy_months)} key={_month_key}"
            )
            # Skip stale controls: an order added interactively mid-load may
            # have already replaced this month's section via
            # _inject_new_order_to_tree — rebuilding from the pre-add snapshot
            # here would silently drop the newly injected row.
            if self._month_collapsible_by_key.get(_month_key) is not _month_ctrl:
                continue
            _fn = getattr(_month_ctrl, "_build_open_body_for_load", None)
            if callable(_fn):
                await _fn(
                    _cache_only=True
                )  # build in Python, don't send to Flutter yet
            # GC every 10 months (was 5) — Python auto-GC handles the rest,
            # manual trigger only needed to avoid pauses at inconvenient times.
            if _mi % 10 == 9:
                await asyncio.to_thread(gc.collect)
            await asyncio.sleep(0)
        self._debug_print(
            f"[render_initial] all {len(_lazy_months)} month bodies done in {time.monotonic()-_tables_t:.3f}s"
        )

        try:
            _p = self._page()
            _w = int(self._orders_viewport_width(_p)) if _p else 0
            _rrs = getattr(self, "_resize_refresh_state", None)
            if isinstance(_rrs, dict):
                _rrs["last_w"] = _w
            else:
                self._resize_refresh_state = {"last_w": _w, "pending": False}
            self._debug_print(f"[render_initial] resize state stamped last_w={_w}")
        except Exception:
            pass

        # Gradually expand non-accepted months one at a time.
        # Spinner stays active until all months are opened — user waits once, then interacts freely.
        _to_expand = [
            (k, c)
            for k, c in _all_month_items
            if getattr(c, "_initially_open", False)
            and not getattr(c, "_is_open", False)
        ]
        if _to_expand:
            # Adaptive render timing — increases automatically after each failure.
            # Original calibration: 0.83s/order on reference hardware
            # (overflow at 0.37s/order caused 20.8s blocks).
            # _flutter_secs_per_order starts at 0.40 and auto-increases on error.
            # Cap is 0.80. To reset: restart the app (cleared on __init__).
            _FLUTTER_SECS_PER_ORDER = max(0.40, min(0.80, self._flutter_secs_per_order))
            _ROW_BATCH = 2
            _BATCH_SLEEP = 0.02
            _total_exp_orders = sum(
                int(getattr(c, "_order_count", 10) or 10) for _, c in _to_expand
            )
            self._debug_print(
                f"[render_initial] {len(_to_expand)} місяців у черзі, {_total_exp_orders} замовлень"
                f" secs/order={_FLUTTER_SECS_PER_ORDER:.2f} retries={self._auto_expand_retries}"
            )

            _loading_text = None
            if _indicator is not None and len(getattr(_indicator, "controls", [])) > 1:
                _loading_text = _indicator.controls[1]

            _expand_ok = True
            try:
                await asyncio.sleep(0.05)
                _n = 0
                for _month_key_x, _ctrl in _to_expand:
                    if getattr(_ctrl, "_is_open", False):
                        continue
                    # Same staleness guard as the lazy-body-build loop above:
                    # an interactively-added order may have replaced this
                    # month's control via _inject_new_order_to_tree already.
                    if self._month_collapsible_by_key.get(_month_key_x) is not _ctrl:
                        continue
                    _n += 1

                    if _loading_text is not None:
                        _loading_text.value = (
                            f"Відкриваємо місяці... {_n}/{len(_to_expand)}"
                        )
                        with contextlib.suppress(Exception):
                            _loading_text.update()
                    await asyncio.sleep(0)

                    _fn = getattr(_ctrl, "_open_inline", None)
                    if callable(_fn):
                        _fn()
                    _body_col = getattr(_ctrl, "_body_col", None)
                    _header_ct = getattr(_ctrl, "_header_ct", None)

                    # Send header (tiny payload — always fast)
                    if _header_ct is not None:
                        with contextlib.suppress(Exception):
                            _header_ct.update()
                    await asyncio.sleep(0)

                    # Send body rows in small batches — each batch GIL hold < 50ms
                    # so the event loop can process clicks between batches.
                    _n_rows = 0
                    if _body_col is not None:
                        _all_rows = list(_body_col.controls)
                        _n_rows = len(_all_rows)
                        _body_col.controls.clear()
                        with contextlib.suppress(Exception):
                            _body_col.update()
                        await asyncio.sleep(0)
                        for _bi in range(0, max(1, _n_rows), _ROW_BATCH):
                            _body_col.controls.extend(_all_rows[_bi : _bi + _ROW_BATCH])
                            with contextlib.suppress(Exception):
                                _body_col.update()
                            await asyncio.sleep(_BATCH_SLEEP)

                    # Drain: wait for Flutter to finish rendering the last batch.
                    _last_batch = (_n_rows % _ROW_BATCH) or _ROW_BATCH
                    _last_batch = min(_last_batch, max(1, _n_rows))
                    _drain = max(0.5, _last_batch * _FLUTTER_SECS_PER_ORDER + 0.4)
                    self._debug_print(
                        f"[auto-expand] {_n}/{len(_to_expand)} rows={_n_rows} last={_last_batch} drain={_drain:.1f}s"
                    )
                    await asyncio.sleep(_drain)

                # Safety drain: last month still rendering in Flutter
                await asyncio.sleep(0.3)
                self._debug_print(f"[auto-expand] DONE {_n}/{len(_to_expand)}")
                # Success — reset retry counter for future loads
                self._auto_expand_retries = 0

            except Exception as _expand_err:
                _expand_ok = False
                self._auto_expand_retries += 1
                _MAX_AUTO_RETRIES = 2
                # Increase timing by 50% (capped at 0.80) for the next attempt
                _new_rate = min(self._flutter_secs_per_order * 1.5, 0.80)
                self._flutter_secs_per_order = _new_rate
                self._debug_print(
                    f"[auto-expand] ERROR retry={self._auto_expand_retries}: {_expand_err}"
                    f" → підвищую secs/order до {_new_rate:.2f}"
                )
                if self._auto_expand_retries <= _MAX_AUTO_RETRIES:
                    # Schedule a full re-render with new (higher) timing
                    self._debug_print("[auto-expand] → авто-перезапуск сторінки")
                    with contextlib.suppress(Exception):
                        self._schedule_full_rerender()
                else:
                    # Too many retries — reset counter, keep higher timing for safety
                    self._debug_print(
                        "[auto-expand] → перевищено ліміт спроб, скидаю лічильник"
                    )
                    self._auto_expand_retries = 0

            if _loading_text is not None and _expand_ok:
                _loading_text.value = self.t("orders.loading", "Завантаження замовлень...")
                with contextlib.suppress(Exception):
                    _loading_text.update()

        # Pre-sync collapsed months to Flutter while spinner is still active.
        # Controls are already built in Python (_cache_only pass above) — send them
        # hidden so that later user open is just a visibility toggle (instant).
        _collapsed_to_presync = [
            c
            for _, c in _all_month_items
            if not getattr(c, "_is_open", False) and getattr(c, "_has_lazy_body", False)
        ]
        if _collapsed_to_presync:
            self._debug_print(
                f"[render_initial] pre-sync {len(_collapsed_to_presync)} згорнутих місяців → Flutter"
            )
            for _cc in _collapsed_to_presync:
                _sfn = getattr(_cc, "_sync_body_to_flutter", None)
                if callable(_sfn):
                    with contextlib.suppress(Exception):
                        await _sfn(_b_size=16)
            self._debug_print("[render_initial] pre-sync done")

        if _own_spinner and _indicator is not None:
            _indicator.visible = False
            with contextlib.suppress(Exception):
                _indicator.update()

        # Re-sync both scrollbars to the stored offset after all widget churn.
        _h = float(getattr(self, "_h_scrollbar_offset", 0.0) or 0.0)
        await self._sync_tables_to_h_offset(_h)
        await self._sync_pinned_to_h_offset(_h)

        self._debug_print(
            f"[render_initial] DONE total={time.monotonic()-_render_t0:.3f}s"
        )

    async def _background_build_months(self, month_items: list) -> None:
        """Builds closed month bodies silently in background after spinner disappears."""
        _t0 = time.monotonic()
        self._debug_print(f"[bg_build] START {len(month_items)} closed months")
        for _i, (_key, _ctrl) in enumerate(month_items):
            _fn = getattr(_ctrl, "_build_open_body_for_load", None)
            if callable(_fn):
                self._debug_print(f"[bg_build] {_i+1}/{len(month_items)} key={_key}")
                await _fn(_cache_only=True)
            # GC every 8 months in background (was 3) — less critical here
            if _i % 8 == 7:
                await asyncio.to_thread(gc.collect)
            await asyncio.sleep(0)
        self._debug_print(
            f"[bg_build] DONE {len(month_items)} months in {time.monotonic()-_t0:.3f}s"
        )

    async def _async_initial_load(self):
        _load_t0 = time.monotonic()
        self._orders_ui_ready = False
        self._set_page_loading(True)
        try:
            await asyncio.sleep(0)  # yield immediately so spinner renders
            self._debug_print("=== INITIAL LOAD START ===")

            # Warm the FX-rate cache BEFORE building any rows: Варт. $/С/В
            # cells go through fx_rates.convert(), which is cache-only and
            # returns None (blank cell) on a cold cache — with no re-render
            # once the background fetch finishes, a row built during that
            # cold window stayed blank forever. This is a no-op once today's
            # rate is already known (memory, local file, or — for VPS
            # companies — the shared company DB another user already
            # populated today), so it only costs a real wait on the very
            # first fetch of the day, by whoever hits it first.
            _t = time.monotonic()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._load_fx_rates_for_company)
            self._debug_print(f"[load] fx rates ready in {time.monotonic()-_t:.3f}s")

            _t = time.monotonic()
            await asyncio.to_thread(self._load_lookups, True)
            self._debug_print(f"[load] lookups done in {time.monotonic()-_t:.3f}s")
            with contextlib.suppress(Exception):
                self._ref_data_pulse_ts = await asyncio.to_thread(
                    self.db.get_reference_data_pulse, self.company_id
                )

            _t = time.monotonic()
            db_pulse_ts = await asyncio.to_thread(self._orders_scope_pulse_ts)
            self._debug_print(
                f"[load] pulse_ts_db={db_pulse_ts} in {time.monotonic()-_t:.3f}s"
            )

            _t = time.monotonic()
            cache_ready = await asyncio.to_thread(self._load_orders_snapshot_cache)
            cache_pulse_ts = int(getattr(self, "_orders_snapshot_pulse_ts", 0) or 0)
            self._debug_print(
                f"[load] cache_ready={cache_ready} pulse_ts_cache={cache_pulse_ts} orders_in_cache={len(self._orders or [])} in {time.monotonic()-_t:.3f}s"
            )

            # Reload from DB when:
            # - No cache exists
            # - pulse=0: server can't signal changes → always do one fresh load
            # - pulse changed since last snapshot
            needs_db_refresh = (
                (not cache_ready)
                or (not bool(db_pulse_ts))
                or (int(db_pulse_ts) > int(cache_pulse_ts))
            )

            # Extra guard: entity_owners changes don't update the orders pulse.
            # If an admin adds an entity_owner row directly (bypassing upsert_order),
            # the snapshot becomes stale while pulse stays the same.
            # Compare a lightweight COUNT to detect this.
            if (
                not needs_db_refresh
                and cache_ready
                and self._snapshot_entity_owner_order_count >= 0
            ):
                try:
                    current_eo_count = await asyncio.to_thread(
                        self.db.count_entity_owner_entity_ids,
                        self.company_id,
                        self.manager_id,
                        "order",
                    )
                    if (
                        current_eo_count >= 0
                        and current_eo_count != self._snapshot_entity_owner_order_count
                    ):
                        needs_db_refresh = True
                        self._debug_print(
                            f"[load] entity_owner count changed: snapshot={self._snapshot_entity_owner_order_count} db={current_eo_count} → forcing reload"
                        )
                except Exception:
                    pass

            self._debug_print(f"[load] needs_db_refresh={needs_db_refresh}")

            for order in self._orders or []:
                if isinstance(order, dict) and order.get("id"):
                    self._orders_cache[order["id"]] = dict(order)

            _t = time.monotonic()
            await asyncio.to_thread(self._load_outbox)
            self._debug_print(
                f"[load] outbox={len(self._outbox)} in {time.monotonic()-_t:.3f}s"
            )
            if self._outbox:
                for _oid, _snap in list(self._outbox.items()):
                    if not isinstance(_snap, dict):
                        continue
                    if _snap.get("_pending_delete"):
                        _del_oid = str(_snap.get("id") or _oid).strip()
                        self._orders = [
                            o
                            for o in self._orders
                            if str((o or {}).get("id") or "").strip() != _del_oid
                        ]
                        self._orders_cache.pop(_del_oid, None)
                    else:
                        self._merge_local_order(_snap)
                        if _snap.get("id"):
                            self._orders_cache[_snap["id"]] = dict(_snap)
                self._orders.sort(key=_order_row_sort_key)
                self._set_sync_status("pending")
            else:
                self._set_sync_status("synced")

            if cache_ready and not needs_db_refresh:
                self._reapply_session_new_orders()
                self._debug_print(
                    f"[load] PATH=cache_fresh orders={len(self._orders or [])} — rendering from cache"
                )
                await self._async_render_initial(build_all=True)
                return

            self._debug_print("[load] PATH=db_refresh — loading from DB")
            # Snapshot outbox before DB load: a concurrent flush() may clear
            # self._outbox between _load_orders() and the merge below.  Keeping
            # a pre-load copy ensures locally-pending orders survive the race.
            _pre_db_outbox = dict(self._outbox)
            _t = time.monotonic()
            await asyncio.to_thread(self._load_orders, True)
            self._debug_print(
                f"[load] DB orders loaded={len(self._orders or [])} in {time.monotonic()-_t:.3f}s"
            )
            self._orders_snapshot_pulse_ts = int(db_pulse_ts or cache_pulse_ts or 0)
            await asyncio.to_thread(
                self._save_orders_snapshot_cache,
                self._orders_snapshot_pulse_ts,
            )
            self._debug_print(
                f"[load] snapshot saved pulse_ts={self._orders_snapshot_pulse_ts}"
            )

            for order in self._orders or []:
                if isinstance(order, dict) and order.get("id"):
                    self._orders_cache[order["id"]] = dict(order)

            await asyncio.to_thread(self._load_outbox)
            # Merge pre-load outbox items that a concurrent flush may have already
            # removed from self._outbox and the disk file before we got here.
            for _oid, _snap in list(_pre_db_outbox.items()):
                if isinstance(_snap, dict) and _oid not in self._outbox:
                    self._outbox[_oid] = _snap
            self._debug_print(f"[load] outbox re-loaded={len(self._outbox)}")
            if self._outbox:
                for _oid, _snap in list(self._outbox.items()):
                    if not isinstance(_snap, dict):
                        continue
                    if _snap.get("_pending_delete"):
                        _del_oid = str(_snap.get("id") or _oid).strip()
                        self._orders = [
                            o
                            for o in self._orders
                            if str((o or {}).get("id") or "").strip() != _del_oid
                        ]
                        self._orders_cache.pop(_del_oid, None)
                    else:
                        self._merge_local_order(_snap)
                        if _snap.get("id"):
                            self._orders_cache[_snap["id"]] = dict(_snap)
                self._orders.sort(key=_order_row_sort_key)
                self._set_sync_status("pending")
                # Flush immediately — don't wait 45s for the timer.
                # Stale pending-deletes from a previous session cause visible
                # count mismatches (order gone from UI but still in DB).
                await self._flush_outbox_async()
            else:
                self._set_sync_status("synced")

            self._reapply_session_new_orders()
            await self._async_render_initial(build_all=True)
            self._debug_print(
                f"[load] render done total_elapsed={time.monotonic()-_load_t0:.3f}s"
            )

        finally:
            with contextlib.suppress(Exception):
                await asyncio.sleep(0.02)

            self._orders_ui_ready = True
            self._start_sync_timer()
            with contextlib.suppress(Exception):
                self._no_db_after_ready = True
            if self._loading_box is not None:
                self._loading_box.visible = False
                with contextlib.suppress(Exception):
                    self._loading_box.update()
            self._debug_print(
                f"[load] FINALLY: calling _set_page_loading(False) elapsed={time.monotonic()-_load_t0:.3f}s"
            )
            self._set_page_loading(False)
            self._debug_print(
                f"=== INITIAL LOAD COMPLETE === ui_ready={self._orders_ui_ready} orders={len(self._orders or [])} outbox={len(self._outbox or {})} total={time.monotonic()-_load_t0:.3f}s"
            )
            for ctrl in [
                getattr(self, "_open_order_btn", None),
                getattr(self, "_quick_add_btn", None),
                getattr(self, "_table_btn", None),
            ]:
                if ctrl is None:
                    continue
                ctrl.disabled = False
                with contextlib.suppress(Exception):
                    ctrl.update()

    def _build_supplier_section(self, sup_name: str, orders: list) -> ft.Container:
        supplier_key = str((orders or [{}])[0].get("supplier_id") or "__no_supplier__")
        supplier_has_active = any(self._is_order_active(o) for o in orders)
        year_map: dict = {}
        for o in orders:
            y = (o.get("order_date") or "")[:4] or "?"
            year_map.setdefault(y, []).append(o)

        year_sections = []
        for year in sorted(year_map.keys(), reverse=True):
            year_orders = year_map[year]

            month_map: dict = {}
            for o in year_orders:
                try:
                    m = int((o.get("order_date") or "----01-")[5:7])
                except Exception:
                    m = 0
                month_map.setdefault(m, []).append(o)

            month_sections = []
            for month_num in sorted(month_map.keys(), reverse=True):
                month_orders = month_map[month_num]
                month_name = self.MONTH_NAMES.get(month_num, str(month_num))
                month_key = f"{supplier_key}:{year}:{int(month_num)}"
                month_counter_row = self._counter_chips(month_orders)
                self._month_counter_row_by_key[month_key] = month_counter_row

                month_has_active = any(self._is_order_active(o) for o in month_orders)
                if self._group_expand_mode == "all":
                    month_open = True
                elif self._group_expand_mode == "supplier":
                    month_open = False
                else:
                    month_open = month_has_active

                def _month_body_factory(_orders=list(month_orders), _key=month_key):
                    return [self._build_orders_table(_orders, table_key=_key)]

                month_section = self._make_collapsible(
                    ft.Row(
                        [
                            ft.Icon(
                                ft.Icons.CALENDAR_MONTH_OUTLINED,
                                size=14,
                                color=_TEXT,
                            ),
                            ft.Text(
                                f"{month_name} {year}",
                                size=12,
                                weight="w600",
                                color=_TEXT,
                            ),
                            month_counter_row,
                        ],
                        spacing=6,
                        wrap=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    [],
                    initially_open=month_open,
                    bg_header=_SOFT_I,
                    border_color=_BORDER,
                    body_factory=_month_body_factory,
                    label=f"Місяць {month_name} {year}",
                )
                month_section._order_count = len(month_orders)  # type: ignore[attr-defined]
                self._month_collapsible_by_key[month_key] = month_section
                month_sections.append(month_section)

            year_has_active = any(self._is_order_active(o) for o in year_orders)
            if self._group_expand_mode == "all":
                year_open = True
            elif self._group_expand_mode == "supplier":
                year_open = False
            else:
                year_open = year_has_active

            year_counter_row = self._counter_chips(year_orders)
            year_section = self._make_collapsible(
                ft.Row(
                    [
                        ft.Icon(ft.Icons.CALENDAR_TODAY_OUTLINED, size=15, color=_TEXT),
                        ft.Text(
                            str(year),
                            size=13,
                            weight=ft.FontWeight.W_700,
                            color=_CHIP_T,
                        ),
                        year_counter_row,
                    ],
                    spacing=6,
                    wrap=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                month_sections,
                initially_open=year_open,
                bg_header=_SOFT_I,
                border_color=_BORDER,
                label=f"Рік {year}",
            )
            year_key = f"{supplier_key}:{year}"
            self._year_counter_row_by_key[year_key] = year_counter_row
            self._year_collapsible_by_key[year_key] = year_section
            self._order_year_body_col[year_key] = year_section._body_col  # type: ignore[attr-defined]
            self._order_year_month_numbers[year_key] = [
                int(m) for m in sorted(month_map.keys(), reverse=True)
            ]
            year_sections.append(year_section)

        if self._group_expand_mode == "all":
            supplier_open = True
        else:
            supplier_open = supplier_has_active or self._group_expand_mode == "supplier"

        supplier_counter_row = self._counter_chips(orders)
        self._supplier_counter_row_by_key[supplier_key] = supplier_counter_row
        supplier_section = self._make_collapsible(
            ft.Row(
                [
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=6, vertical=3),
                        bgcolor=_SOFT_I,
                        border_radius=5,
                        content=ft.Text(
                            "ПОСТАЧАЛЬНИК",
                            size=8,
                            weight=ft.FontWeight.W_800,
                            color=_CHIP_T,
                        ),
                    ),
                    ft.Icon(ft.Icons.LOCAL_SHIPPING_OUTLINED, size=18, color=_TEXT),
                    ft.Text(sup_name, size=14, weight=ft.FontWeight.W_700, color=_TEXT),
                    supplier_counter_row,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                wrap=True,
            ),
            year_sections,
            initially_open=supplier_open,
            bg_header=_SOFT_I,
            border_color=_BORDER,
            label=f"Постачальник {sup_name}",
        )
        self._supplier_collapsible_by_key[supplier_key] = supplier_section
        return supplier_section

    def _make_collapsible(
        self,
        header_content: ft.Control,
        body_controls: Optional[list] = None,
        initially_open: bool = True,
        bg_header: str = "#EFF6FF",
        border_color: str = "#CBD5E1",
        body_factory: Optional[Callable[[], list]] = None,
        label: str = "",
    ) -> ft.Container:
        controls = list(body_controls or [])
        body_built = bool(controls)
        body_col = ft.Column(controls, spacing=2)
        # Lazy bodies (body_factory, no inline controls) start closed even when initially_open=True.
        # The body appears open only after its cached controls are applied on first click.
        _has_lazy = (not body_built) and callable(body_factory)
        is_open = bool(initially_open) and not _has_lazy
        arrow = ft.Icon(
            ft.Icons.EXPAND_LESS if is_open else ft.Icons.EXPAND_MORE,
            size=16,
            color=_TEXT,
        )
        _open_spinner = ft.ProgressRing(
            width=16, height=16, stroke_width=2, color=_TEXT, visible=False
        )
        arrow_wrap = ft.Stack([arrow, _open_spinner], width=20, height=20)
        body_col.visible = is_open
        body_wrap = ft.Container(content=body_col, padding=0)
        outer = ft.Container(
            border=ft.border.all(1, border_color),
            border_radius=8,
            bgcolor=_ALT,
            margin=ft.margin.only(bottom=2),
            clip_behavior=ft.ClipBehavior.NONE,
        )
        _label = label or "?"
        _click_id_ref: list = [0]  # shared between toggle and _build_open_body_for_load
        _build_gen: list = [
            0
        ]  # increments on each open/close; tasks cancel when gen changes
        _flutter_synced: list = [
            not _has_lazy
        ]  # True once all controls are fully in Flutter
        _prebuilt_controls: list = (
            []
        )  # controls pre-built during initial load, NOT yet in Flutter

        def toggle(_):
            nonlocal body_built, is_open
            _click_t = time.monotonic()
            if not is_open:
                self._click_counter += 1
                _cid = self._click_counter
                _click_id_ref[0] = _cid
                _pending = [c["label"] for c in self._click_queue]
                self._debug_print(
                    f"[click#{_cid}] ВІДКРИТИ «{_label}» | built={body_built} needs_build={not body_built and callable(body_factory)} | черга={_pending or '[]'}"
                )
                is_open = True
                _build_gen[0] += 1
                arrow.name = ft.Icons.EXPAND_LESS
                arrow.visible = False
                _open_spinner.visible = True
                outer._is_open = is_open  # type: ignore[attr-defined]
                body_col.visible = True

                _page_ref = self._page()

                if not body_built and callable(body_factory):
                    self._click_queue.append(
                        {"id": _cid, "label": _label, "t": _click_t}
                    )
                    if _page_ref and hasattr(_page_ref, "run_task"):
                        self._debug_print(
                            f"[click#{_cid}] «{_label}» → в чергу побудови | всього в черзі={len(self._click_queue)}"
                        )

                        async def _spinner_then_build():
                            _my_gen = _build_gen[0]
                            with contextlib.suppress(Exception):
                                header_ct.update()
                            await asyncio.sleep(0.08)
                            await _build_open_body_for_load(_gen=_my_gen)
                            await asyncio.sleep(0.3)
                            if _build_gen[0] == _my_gen:
                                _open_spinner.visible = False
                                arrow.visible = True
                                with contextlib.suppress(Exception):
                                    header_ct.update()

                        _page_ref.run_task(_spinner_then_build)
                    else:
                        self._debug_print(
                            f"[click#{_cid}] «{_label}» → синхронна побудова (немає run_task)"
                        )
                        try:
                            body_col.controls = list(body_factory() or [])
                            body_built = True
                        except Exception:
                            pass
                        self._click_queue = [
                            c for c in self._click_queue if c["label"] != _label
                        ]
                        with contextlib.suppress(Exception):
                            body_col.update()
                else:
                    _reason = "вже побудовано" if body_built else "немає фабрики"
                    # Capture whether prebuilt controls need first-time send to Flutter
                    _had_prebuilt = bool(_prebuilt_controls)
                    if _prebuilt_controls:
                        body_col.controls = list(_prebuilt_controls)
                        _prebuilt_controls.clear()
                        self._debug_print(
                            f"[click#{_cid}] «{_label}» → надсилаємо кешовані {len(body_col.controls)} рядків у Flutter"
                        )
                    self._debug_print(
                        f"[click#{_cid}] «{_label}» → відкривається ({_reason}) {time.monotonic()-_click_t:.3f}s"
                    )

                    if _page_ref and hasattr(_page_ref, "run_task"):

                        async def _spinner_then_open():
                            _my_gen = _build_gen[0]
                            with contextlib.suppress(Exception):
                                header_ct.update()
                            await asyncio.sleep(0.08)
                            if not _flutter_synced[0]:
                                _all = list(body_col.controls)
                                body_col.controls.clear()
                                with contextlib.suppress(Exception):
                                    body_col.update()
                                _B = 4
                                for _bi in range(0, max(1, len(_all)), _B):
                                    if _build_gen[0] != _my_gen:
                                        _flutter_synced[0] = False
                                        body_col.controls.extend(_all[_bi:])
                                        break
                                    body_col.controls.extend(_all[_bi : _bi + _B])
                                    with contextlib.suppress(Exception):
                                        body_col.update()
                                    await asyncio.sleep(0.08)
                                else:
                                    _flutter_synced[0] = True
                            else:
                                with contextlib.suppress(Exception):
                                    body_col.update()
                            await asyncio.sleep(0.3)
                            if _build_gen[0] == _my_gen:
                                _open_spinner.visible = False
                                arrow.visible = True
                                with contextlib.suppress(Exception):
                                    header_ct.update()

                        _page_ref.run_task(_spinner_then_open)
                    else:
                        with contextlib.suppress(Exception):
                            header_ct.update()
                        with contextlib.suppress(Exception):
                            body_col.update()
            else:
                self._click_counter += 1
                _cid = self._click_counter
                self._debug_print(f"[click#{_cid}] ЗАКРИТИ «{_label}»")
                _build_gen[0] += 1
                body_col.visible = False
                is_open = False
                arrow.name = ft.Icons.EXPAND_MORE
                arrow.visible = True
                _open_spinner.visible = False
                outer._is_open = is_open  # type: ignore[attr-defined]
                with contextlib.suppress(Exception):
                    body_col.update()
                with contextlib.suppress(Exception):
                    header_ct.update()

        header_ct = ft.Container(
            content=ft.Row(
                [header_content, arrow_wrap],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            on_click=toggle,
            bgcolor=bg_header,
            padding=ft.padding.symmetric(horizontal=8, vertical=5),
            border_radius=ft.border_radius.only(top_left=8, top_right=8),
        )
        outer.content = ft.Column([header_ct, body_wrap], spacing=0)
        outer._body_col = body_col  # type: ignore[attr-defined]
        outer._header_ct = header_ct  # type: ignore[attr-defined]
        outer._body_wrap = body_wrap  # type: ignore[attr-defined]
        outer._is_open = is_open  # type: ignore[attr-defined]
        outer._toggle_open = toggle  # type: ignore[attr-defined]
        outer._collapse = lambda: toggle(None) if outer._is_open else None  # type: ignore[attr-defined]
        outer._expand = lambda: toggle(None) if not outer._is_open else None  # type: ignore[attr-defined]
        outer._initially_open = initially_open  # type: ignore[attr-defined]
        outer._has_lazy_body = (not bool(body_controls)) and callable(body_factory)  # type: ignore[attr-defined]

        def _open_inline():
            """Apply prebuilt controls and mark open in Python only — caller must call tree_host.update() after all months."""
            nonlocal is_open
            if is_open:
                return
            if _prebuilt_controls:
                body_col.controls = list(_prebuilt_controls)
                _prebuilt_controls.clear()
            body_col.visible = True
            is_open = True
            arrow.name = ft.Icons.EXPAND_LESS
            outer._is_open = True  # type: ignore[attr-defined]
            _flutter_synced[0] = True  # caller will tree_host.update(), syncing Flutter

        outer._open_inline = _open_inline  # type: ignore[attr-defined]

        _body_building = False

        async def _build_open_body_for_load(_cache_only: bool = False, _gen: int = -1):
            nonlocal body_built, _body_building
            _cid = _click_id_ref[0]
            if body_built or not callable(body_factory):
                self._debug_print(
                    f"[build#{_cid}] «{_label}» пропуск (built={body_built})"
                )
                if not _cache_only:
                    self._click_queue = [
                        c for c in self._click_queue if c["label"] != _label
                    ]
                return
            if _body_building:
                self._debug_print(
                    f"[build#{_cid}] «{_label}» вже будується, пропуск | черга={[c['label'] for c in self._click_queue]}"
                )
                return
            _body_building = True
            if _cache_only:
                self._debug_print(
                    f"[build#{_cid}] «{_label}» СТАРТ (cache-only, Flutter не отримає)"
                )
            else:
                _q_labels = [c["label"] for c in self._click_queue]
                self._debug_print(
                    f"[build#{_cid}] «{_label}» СТАРТ | черга зараз={_q_labels}"
                )
            try:
                _t0 = time.monotonic()

                def _factory_with_gc(_bf=body_factory):
                    gc.collect()  # prevent GC pauses mid-build
                    return _bf()

                new_controls = await asyncio.to_thread(_factory_with_gc)
                _build_s = time.monotonic() - _t0
                _list_controls = list(new_controls or [])
                body_built = True
                if _cache_only:
                    _prebuilt_controls[:] = _list_controls
                    self._debug_print(
                        f"[build#{_cid}] «{_label}» кешовано {len(_list_controls)} рядків за {_build_s:.3f}s (Flutter чекає відкриття)"
                    )
                    _body_building = False
                    return
                body_col.controls = _list_controls
                self._click_queue = [
                    c for c in self._click_queue if c["label"] != _label
                ]
                _q_after = [c["label"] for c in self._click_queue]
                self._debug_print(
                    f"[build#{_cid}] «{_label}» побудовано {len(body_col.controls)} рядків за {_build_s:.3f}s | залишок черги={_q_after}"
                )
            except Exception as _e:
                self._debug_print(f"[build#{_cid}] «{_label}» ПОМИЛКА: {_e}")
                if not _cache_only:
                    self._click_queue = [
                        c for c in self._click_queue if c["label"] != _label
                    ]
                _body_building = False
                return
            _body_building = False
            # Send rows in batches — avoids 30s GIL holds from large payloads
            try:
                _upd_t0 = time.monotonic()
                _all_built = list(body_col.controls)
                body_col.controls.clear()
                body_col.update()  # register empty column first
                _B = 4
                for _bi in range(0, max(1, len(_all_built)), _B):
                    if _gen != -1 and _build_gen[0] != _gen:
                        _flutter_synced[0] = False
                        body_col.controls.extend(_all_built[_bi:])
                        break
                    body_col.controls.extend(_all_built[_bi : _bi + _B])
                    body_col.update()
                    await asyncio.sleep(0.08)
                else:
                    _flutter_synced[0] = True
                self._debug_print(
                    f"[build#{_cid}] «{_label}» UI оновлено {len(_all_built)} рядків за {time.monotonic()-_upd_t0:.3f}s ✓"
                )
            except Exception as _e2:
                self._debug_print(f"[build#{_cid}] «{_label}» update FAILED: {_e2}")

        outer._build_open_body_for_load = _build_open_body_for_load  # type: ignore[attr-defined]
        outer._flutter_synced = _flutter_synced  # type: ignore[attr-defined]

        async def _sync_body_to_flutter(_b_size: int = 16) -> None:
            """Send prebuilt-but-hidden controls to Flutter so later open is instant."""
            if _flutter_synced[0]:
                return
            if _prebuilt_controls:
                body_col.controls = list(_prebuilt_controls)
                _prebuilt_controls.clear()
            if not body_col.controls:
                return
            _all = list(body_col.controls)
            body_col.controls.clear()
            with contextlib.suppress(Exception):
                body_col.update()
            await asyncio.sleep(0)
            for _bi in range(0, max(1, len(_all)), _b_size):
                body_col.controls.extend(_all[_bi : _bi + _b_size])
                with contextlib.suppress(Exception):
                    body_col.update()
                await asyncio.sleep(0.01)
            _flutter_synced[0] = True

        outer._sync_body_to_flutter = _sync_body_to_flutter  # type: ignore[attr-defined]
        return outer

    def _table_col_specs(self, hide_amount_usd: bool = False) -> list:
        cols = [
            {"key": "lines_expand", "label": "+", "width": 38},
            {"key": "expand", "label": "", "width": 30},
            {"key": "delete", "label": "", "width": 30},
            {"key": "date", "label": self.t("orders.col.date", "Дата"), "width": 60},
            {"key": "log", "label": self.t("orders.col.log", "Плече"), "width": 45},
            *(
                [
                    {"key": "date_payment", "label": self.t("orders.col.date_payment", "Оплата"), "width": 50},
                    {"key": "date_receive", "label": self.t("orders.col.date_receive", "Отрим"), "width": 50},
                    {"key": "dur_acc", "label": self.t("orders.col.dur_acc", "Відхил"), "width": 45},
                ]
                if self._show_dates_block
                else []
            ),
            {"key": "inv", "label": self.t("orders.col.inv", "Інвойс"), "width": 50},
            {"key": "order1c", "label": self.t("orders.col.order1c", "ЗП обліку"), "width": 50},
            {"key": "product", "label": self.t("orders.col.product", "Товар"), "width": 120},
            {"key": "status", "label": self.t("orders.col.status", "Статус"), "width": 70},
            {"key": "delivery", "label": self.t("orders.col.delivery", "Тип доставки"), "width": 55},
            {"key": "comment", "label": self.t("orders.col.comment", "Коментар"), "width": 75},
            {"key": "qty", "label": self.t("orders.col.qty", "К-сть"), "width": 55},
            {"key": "amount", "label": self.t("orders.col.amount", "Вартість"), "width": 65},
            *(
                []
                if hide_amount_usd
                else [{"key": "amount_usd", "label": self.t("orders.col.amount_usd", "Варт. $"), "width": 65}]
            ),
            {"key": "payment", "label": self.t("orders.col.payment", "Сплата"), "width": 55},
            {"key": "submission", "label": self.t("orders.col.submission", "Внесення даних"), "width": 60},
        ]
        if self._show_dates_block:
            cols.extend(
                [
                    {"key": "date_prod_end", "label": self.t("orders.col.date_prod_end", "Готово"), "width": 60},
                    {"key": "dur_prod", "label": self.t("orders.col.dur_prod", "Терм вир-ва"), "width": 45},
                    {"key": "date_wh", "label": self.t("orders.col.date_wh", "На складі"), "width": 60},
                    {"key": "dur_deliv", "label": self.t("orders.col.dur_deliv", "Терм дос-ки"), "width": 45},
                    {"key": "date_1c", "label": self.t("orders.col.date_1c", "Прийомка"), "width": 60},
                    {"key": "term_acc", "label": self.t("orders.col.term_acc", "Терм прий-ки"), "width": 45},
                ]
            )
        if self._show_cost_block:
            cols.extend(
                [
                    {"key": "w_prev", "label": self.t("orders.col.w_prev", "Вага розр"), "width": 50},
                    {"key": "w_fact", "label": self.t("orders.col.w_fact", "Вага факт"), "width": 50},
                    {"key": "tariff", "label": self.t("orders.col.tariff", "Тариф"), "width": 30},
                    {"key": "unit_cost", "label": self.t("orders.col.unit_cost", "С/В"), "width": 30},
                ]
            )
        cols.append(
            {"key": "action_edit", "label": "", "width": 60}
        )  # edit / delete — no status shortcuts

        visible = self._visible_orders_column_keys()
        always_visible = {"lines_expand", "expand", "delete", "action_edit"}
        cols = [
            c
            for c in cols
            if str(c.get("key") or "") in always_visible
            or str(c.get("key") or "") in visible
        ]
        ov_unit = getattr(self, "_table_col_width_overrides_unit", "px") or "px"
        if ov_unit == "pct" and isinstance(self._table_col_width_overrides, dict):
            migrated = {}
            for col in cols:
                key = str(col.get("key") or "")
                if not key:
                    continue
                pct = _safe_float(
                    (self._table_col_width_overrides or {}).get(key), None
                )
                base_w = int(col.get("width") or 80)
                if pct is None:
                    continue
                migrated[key] = int(
                    max(35, min(900, round((float(pct) / 100.0) * 1080)))
                )
                if migrated[key] <= 0:
                    migrated[key] = base_w
            self._table_col_width_overrides = migrated
            self._table_col_width_overrides_unit = "px"
            self._table_col_width_px_migrated = True
            with contextlib.suppress(Exception):
                self._save_table_layout_state()

        for col in cols:
            col["base_width"] = int(col.get("width") or 80)

        if self._table_col_width_overrides:
            for c in cols:
                key = str(c.get("key") or "")
                if not key:
                    continue
                ov = _safe_float(self._table_col_width_overrides.get(key), None)
                if ov is not None:
                    c["width"] = int(max(35, min(900, int(ov))))

        page = self._page()
        target_w = int(self._orders_viewport_width(page)) if page else 0
        cur_sum = sum(int(c.get("width") or 0) for c in cols)
        has_user_overrides = bool(self._table_col_width_overrides)
        # Only auto-grow when: actual width is known (>0), no user overrides,
        # and table is narrower than the viewport.
        # Cap growth at 300px so one column never balloons to fill the window.
        if (not has_user_overrides) and target_w > 0 and cur_sum < target_w:
            grow_key = "comment"
            grow_col = next(
                (c for c in cols if str(c.get("key") or "") == grow_key), None
            )
            if grow_col is None:
                grow_col = next(
                    (c for c in cols if str(c.get("key") or "") == "product"), None
                )
            if grow_col is None and cols:
                grow_col = cols[-1]
            if grow_col is not None:
                grow_amount = min(300, int(target_w - cur_sum))
                grow_col["width"] = int(grow_col.get("width") or 0) + grow_amount
        return cols

    def _table_cell(
        self,
        text: str = "",
        width: Optional[int] = None,
        expand: bool = False,
        size: Union[int, float] = 8,
        bold: bool = False,
        color: str = "#1F2937",
        height: Optional[int] = None,
        no_wrap: bool = True,
        max_lines: Optional[int] = 1,
        bgcolor: str = "#FFFFFF",
        text_align: ft.TextAlign = ft.TextAlign.CENTER,
    ) -> ft.Container:
        display_text = "-" if text is None else str(text)
        if display_text not in {"-", ""}:
            with contextlib.suppress(Exception):
                display_text = format_maybe_numeric_text(display_text)
        dyn_height = height
        if not no_wrap:
            display_text = _wrap_text_for_width(display_text, width)
            try:
                line_count = max(1, len(str(display_text or "").split("\n")))
                base_h = int(height or 0)
                if base_h > 0:
                    dyn_height = max(base_h, min(240, 16 * line_count + 6))
            except Exception:
                dyn_height = height
        return ft.Container(
            width=width,
            expand=expand,
            height=dyn_height,
            border=ft.border.all(1, "#CBD5E1"),
            border_radius=0,
            bgcolor=bgcolor,
            alignment=ft.Alignment(0, 0),
            padding=ft.padding.symmetric(horizontal=0, vertical=0),
            content=ft.Text(
                display_text,
                size=size,
                weight="bold" if bold else "w400",
                color=color,
                text_align=text_align,
                no_wrap=no_wrap,
                max_lines=max_lines,
                overflow=(
                    ft.TextOverflow.ELLIPSIS if no_wrap else ft.TextOverflow.VISIBLE
                ),
            ),
        )

    def _build_table_header(self, specs: Optional[list] = None) -> ft.Container:
        cells = []
        specs = specs if isinstance(specs, list) else self._table_col_specs()

        def _est_header_lines(col: dict) -> int:
            txt = str(col.get("label") or "")
            width = max(40, int(col.get("width") or 80) - 2)
            chars_per_line = max(4, width // 5)
            needed = (len(txt) + chars_per_line - 1) // chars_per_line
            return max(1, min(4, needed))

        header_lines = max((_est_header_lines(c) for c in specs), default=1)
        header_h = max(34, int(self._table_row_height) + 8, 18 * header_lines + 8)
        for col in specs:
            cells.append(
                self._table_cell(
                    text=col.get("label", ""),
                    width=col.get("width"),
                    expand=bool(col.get("expand")),
                    size=8,
                    bold=True,
                    color=_CHIP_T,
                    height=header_h,
                    no_wrap=False,
                    max_lines=4,
                    bgcolor=(
                        "#BFDBFE" if col.get("key") == "date_receive" else _BORDER
                    ),
                )
            )
        return ft.Container(
            bgcolor=_BORDER,
            border=ft.border.all(1, _BORDER),
            border_radius=0,
            padding=ft.padding.symmetric(horizontal=0, vertical=0),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.Row(
                cells, spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER
            ),
        )

    def _open_table_layout_dialog(self):
        page = self._page()
        if not page:
            return

        specs = self._table_col_specs()
        selected_columns = set(self._visible_orders_column_keys())
        toggleable_keys = [
            "date",
            "log",
            "date_receive",
            "dur_acc",
            "inv",
            "order1c",
            "product",
            "status",
            "delivery",
            "comment",
            "qty",
            "amount",
            "amount_usd",
            "payment",
            "submission",
            "date_prod_end",
            "dur_prod",
            "date_wh",
            "dur_deliv",
            "date_1c",
            "term_acc",
            "w_prev",
            "w_fact",
            "tariff",
            "unit_cost",
        ]
        labels = {
            str(c.get("key") or ""): str(c.get("label") or c.get("key") or "")
            for c in specs
            if c.get("key")
        }
        col_checks = []
        for key in toggleable_keys:
            title = labels.get(key, key)
            cb = ft.Checkbox(label=f"{title} ({key})", value=(key in selected_columns))
            col_checks.append((key, cb))

        row_h_f = ft.TextField(
            label=self.t("orders.col_settings.row_height", "Висота рядка"),
            value=str(int(self._table_row_height)),
            width=140,
            border_radius=8,
        )

        _dfm_months = sorted(
            {
                str(o.get("order_date") or "")[:7]
                for o in (self._orders or [])
                if isinstance(o, dict) and str(o.get("order_date") or "")[:7]
            }
        )

        def _dfm_label(m: str) -> str:
            try:
                mm = int(m[5:7])
            except Exception:
                return m
            return f"{self.MONTH_NAMES.get(mm, m[5:7])} {m[:4]}"

        display_from_dd = ft.Dropdown(
            label=self.t(
                "orders.col_settings.display_from_month", "Перший місяць відображення"
            ),
            value=(self._display_from_month if self._display_from_month in _dfm_months else ""),
            options=[
                ft.dropdown.Option("", self.t("orders.col_settings.show_all_history", "Показати всю історію"))
            ]
            + [ft.dropdown.Option(m, _dfm_label(m)) for m in _dfm_months],
            width=320,
            border_radius=8,
        )

        width_rows = []
        for col in specs:
            key = str(col.get("key") or "")
            if not key:
                continue
            px_val = int(col.get("width") or col.get("base_width") or 80)
            width_f = ft.TextField(
                label=f"{col.get('label', key)} ({key}), px",
                value=str(px_val),
                width=240,
                border_radius=8,
            )
            width_rows.append((key, width_f))

        widths_col = ft.Column(
            [f for _, f in width_rows],
            spacing=8,
            scroll=ft.ScrollMode.ADAPTIVE,
            height=360,
        )

        cols_pick_col = ft.Column(
            [cb for _, cb in col_checks],
            spacing=4,
            scroll=ft.ScrollMode.ADAPTIVE,
            height=220,
        )

        def _close(_=None):
            self._close_dlg(dlg)

        def _save(_):
            new_h = _safe_float(row_h_f.value, None)
            if new_h is None:
                row_h_f.error_text = "Вкажіть число"
                with contextlib.suppress(Exception):
                    row_h_f.update()
                return
            row_h_f.error_text = None
            self._table_row_height = max(26, min(72, int(new_h)))

            overrides = {}
            for key, fld in width_rows:
                w = _safe_float(fld.value, None)
                if w is None:
                    continue
                w_px = int(max(35, min(900, int(float(w)))))
                overrides[key] = w_px
            self._table_col_width_overrides = overrides
            self._table_col_width_overrides_unit = "px"
            self._table_col_width_px_migrated = False
            self._display_from_month = str(display_from_dd.value or "").strip()
            selected = [key for key, cb in col_checks if bool(cb.value)]
            self._set_visible_orders_column_keys(selected)

            self._save_table_layout_state()
            self._close_dlg(dlg)
            self._schedule_full_rerender()

        def _reset(_):
            self._table_row_height = 32
            self._table_col_width_overrides = {}
            self._table_col_width_overrides_unit = "px"
            self._table_col_width_px_migrated = False
            self._display_from_month = ""
            self._save_table_layout_state()
            self._close_dlg(dlg)
            self._schedule_full_rerender()

        def _tbl_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, self.t("orders.dlg.column_settings", "Розміри таблиці"), [
                self.t("orders.col_help.step1", "1. Відмітьте колонки, які хочете бачити в таблиці."),
                self.t("orders.col_help.step2", "2. Введіть висоту рядка в пікселях (рекомендовано 36–48, менше = більше рядків)."),
                self.t("orders.col_help.step4", "4. Задайте ширину кожної колонки або залиште 0 для авто."),
                self.t("orders.col_help.step5", "5. «Зберегти» — налаштування синхронізуються між усіма пристроями."),
                self.t("orders.col_help.step6", "«Скинути» — повертає всі ширини до стандартних значень."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                [
                    ft.Text(self.t("orders.dlg.column_settings", "Розміри таблиці"), weight="bold", expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_tbl_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Container(
                width=620,
                content=ft.Column(
                    [
                        ft.Text(
                            "Зміни зберігаються в БД профілю і підтягуються на інших пристроях.",
                            size=12,
                            color=_TEXT,
                        ),
                        ft.Text(self.t("orders.section.visible_columns", "Видимі колонки"), size=11, weight="w600", color=_TEXT),
                        cols_pick_col,
                        row_h_f,
                        ft.Divider(height=8, color=_BORDER),
                        display_from_dd,
                        ft.Divider(height=8, color=_BORDER),
                        widths_col,
                    ],
                    spacing=8,
                    scroll=ft.ScrollMode.ADAPTIVE,
                    height=560,
                ),
            ),
            actions=[
                ft.TextButton(self.t("common.reset", "Скинути"), on_click=_reset),
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_close),
                ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    def _build_orders_table(self, orders: list, table_key: str = "") -> ft.Container:
        hide_amount_usd = False
        try:
            first = (orders or [None])[0] or {}
            supplier_id = str((first or {}).get("supplier_id") or "").strip()
            sup_row = next(
                (
                    s
                    for s in (self._suppliers or [])
                    if str((s or {}).get("id") or "").strip() == supplier_id
                ),
                None,
            )
            sup_ccy = (
                str(
                    (sup_row or {}).get("currency")
                    or (sup_row or {}).get("supplier_currency")
                    or ""
                )
                .upper()
                .strip()
            )
            hide_amount_usd = sup_ccy == "USD"
        except Exception:
            hide_amount_usd = False

        _uncached_ids = [
            str((o or {}).get("id") or "").strip()
            for o in (orders or [])
            if isinstance(o, dict)
            and str((o or {}).get("id") or "").strip() not in self._order_attachments_cache
        ]
        if _uncached_ids:
            with contextlib.suppress(Exception):
                self._order_attachments_cache.update(
                    self.db.get_order_attachments_meta_bulk(_uncached_ids)
                )
            for _oid in _uncached_ids:
                self._order_attachments_cache.setdefault(_oid, [])

        specs = self._table_col_specs(hide_amount_usd=hide_amount_usd)
        rows = [
            self._build_order_row(o, specs=specs, row_idx=i)
            for i, o in enumerate(orders)
        ]
        table_width = sum(int(c.get("width") or 0) for c in specs)
        w_final = max(1, int(table_width))
        inner_rows_col = ft.Column(rows, spacing=1)
        tbl_core = ft.Column(
            [self._build_table_header(specs), inner_rows_col],
            spacing=3,
            tight=True,
            width=int(w_final),
        )

        table_shell = ft.Container(
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            bgcolor=_PANEL,
            border_radius=8,
            border=ft.border.all(1, _BORDER),
            content=tbl_core,
        )

        key = str(table_key or "").strip()
        if key:
            self._order_month_rows_col[key] = inner_rows_col
        return ft.Row(
            [table_shell],
            scroll=ft.ScrollMode.ALWAYS,
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )

    def _build_order_row(
        self, order: dict, specs: Optional[list] = None, row_idx: int = 0
    ) -> ft.Container:
        row_h = int(max(26, min(72, self._table_row_height)))
        row_bg = _PANEL if row_idx % 2 == 0 else _ALT  # чергування панель/альт
        status = order.get("status") or "production"
        invoice = order.get("invoice_no") or "?"
        order_date = (order.get("order_date") or "")[:10]
        delivery_name = order.get("delivery_type_name") or "-"
        payment_st = order.get("payment_status") or "unpaid"
        submission_st = order.get("submission_status") or "not_submitted"
        items = order.get("items") or []
        _single_pid = (
            str(items[0].get("product_id") or "").strip() if len(items) == 1 else ""
        )
        product_text = (
            (
                (self._product_label_map_cache or {}).get(_single_pid)
                or (self._products_by_id_cache or {}).get(_single_pid, {}).get("name")
                or items[0].get("product_name")
                or "-"
            )
            if len(items) == 1
            else (
                self._common_items_label(items) or f"{len(items)} товари"
                if len(items) > 1
                else "-"
            )
        )
        _main_pid = _single_pid
        order_1c = order.get("order_no_1c") or ""
        comment = order.get("comment") or ""

        draft_order = dict(order)
        draft_items = [dict(it) for it in items if isinstance(it, dict)]

        log_days = None
        log_text = "—"

        def _item_qty_value(it: dict) -> float:
            q = _safe_float((it or {}).get("qty"), None)
            if q is None:
                q = _safe_float((it or {}).get("quantity"), 0.0)
            return float(q or 0.0)

        for _it in draft_items:
            if not isinstance(_it, dict):
                continue
            _qv = _item_qty_value(_it)
            _it["qty"] = _qv
            _it["quantity"] = _qv

        def _resolve_actual_weights(
            items_list: list,
            total_override: Optional[float] = None,
            prod_map: Optional[dict] = None,
        ):
            prev_weights = []
            manual_weights = []
            for it in items_list:
                q = max(_item_qty_value(it), 0.0)
                w_u = max(
                    _safe_float((it or {}).get("weight_per_unit_kg"), 0.0) or 0.0, 0.0
                )
                if w_u == 0.0 and prod_map:
                    pid = str((it or {}).get("product_id") or "").strip()
                    prod = prod_map.get(pid) or {}
                    w_u = max(_safe_float(prod.get("weight_kg"), 0.0) or 0.0, 0.0)
                prev = q * w_u
                prev_weights.append(prev)
                w_m = _safe_float((it or {}).get("weight_actual_kg"), None)
                manual_weights.append(w_m if (w_m is not None and w_m >= 0) else None)

            resolved = [None] * len(items_list)
            unset_idxs = []
            fixed_sum = 0.0
            for i, w_m in enumerate(manual_weights):
                if w_m is None:
                    unset_idxs.append(i)
                else:
                    resolved[i] = w_m
                    fixed_sum += w_m

            if total_override is not None:
                target_total = max(total_override, 0.0)
                remain = max(target_total - fixed_sum, 0.0)
                if unset_idxs:
                    basis = sum(prev_weights[i] for i in unset_idxs)
                    if basis > 0:
                        for i in unset_idxs:
                            resolved[i] = remain * (prev_weights[i] / basis)
                    else:
                        each = remain / max(len(unset_idxs), 1)
                        for i in unset_idxs:
                            resolved[i] = each
            else:
                for i in unset_idxs:
                    resolved[i] = prev_weights[i]

            final_weights = [(w if w is not None else 0.0) for w in resolved]
            return final_weights, prev_weights

        products_by_id = self._products_by_id_cache or {
            str((p or {}).get("id") or "").strip(): p
            for p in (self._products or [])
            if isinstance(p, dict) and (p.get("id") or "")
        }

        def _effective_item_pricing(item: dict):
            it = item if isinstance(item, dict) else {}
            pid = str((it.get("product_id") or "")).strip()
            prod = products_by_id.get(pid)

            p = _safe_float((it or {}).get("purchase_price"), None)
            ccy = _normalize_currency_code((it or {}).get("purchase_currency"), "USD")

            if p is None and isinstance(prod, dict):
                p_prod = _safe_float(prod.get("purchase_price"), None)
                ccy_prod = _normalize_currency_code(prod.get("purchase_currency"), ccy)
                if p_prod is not None:
                    p = p_prod
                if ccy_prod:
                    ccy = ccy_prod

            return p, ccy

        def _compute_totals(items_list: list):
            total_qty_local = 0.0
            total_amount_local = 0.0
            has_price_local = False
            for it in items_list:
                q = _item_qty_value(it)
                p, _ = _effective_item_pricing(it)
                total_qty_local += q
                if p is not None:
                    total_amount_local += q * p
                    has_price_local = True
            qty_local = format_grouped(total_qty_local)
            amount_local = (
                format_grouped(total_amount_local, decimals=2)
                if has_price_local
                else ""
            )
            manual_amount = _safe_float(draft_order.get("amount_manual_override"), None)
            if manual_amount is not None:
                amount_local = format_grouped(manual_amount, decimals=2)
            return qty_local, amount_local

        qty_text, amount_text = _compute_totals(draft_items)

        first_p, first_ccy = _effective_item_pricing(
            (draft_items[0] if draft_items else {}) or {}
        )
        base_item_ccy = first_ccy or "USD"
        manual_amount_override = _safe_float(
            draft_order.get("amount_manual_override"), None
        )

        def _items_total_usd(items_list: list) -> Optional[float]:
            if manual_amount_override is not None:
                manual_conv = self._convert_currency_amount(
                    manual_amount_override,
                    base_item_ccy,
                    "USD",
                )
                return manual_conv

            total_usd = 0.0
            has_any = False
            for it in items_list:
                q = _item_qty_value(it)
                p, ccy = _effective_item_pricing(it)
                if p is None:
                    continue
                amt = q * p
                conv = self._convert_currency_amount(
                    amt,
                    ccy,
                    "USD",
                )
                if conv is None:
                    continue
                total_usd += conv
                has_any = True
            return total_usd if has_any else None

        amount_usd_val = _items_total_usd(draft_items)
        amount_usd_text = (
            format_grouped(amount_usd_val, decimals=2)
            if amount_usd_val is not None
            else ""
        )

        def _ccy_symbol(ccy: str) -> str:
            c = str(ccy or "").upper().strip()
            return {
                "USD": "$",
                "UAH": "₴",
                "EUR": "€",
                "PLN": "zł",
                "CNY": "¥",
            }.get(c, c)

        def _amount_with_ccy(raw_amount: str, ccy: str) -> str:
            a = str(raw_amount or "").strip()
            c = _ccy_symbol(ccy)
            if not a:
                return ""
            return f"{a}\u202f{c}".strip() if c else a

        amount_display_text = _amount_with_ccy(amount_text, base_item_ccy)
        amount_usd_display_text = (
            _amount_with_ccy(amount_usd_text, "USD") if amount_usd_text else ""
        )

        def _need_lines_for_text(txt: str, w: Optional[int]) -> int:
            try:
                width = int(w or 0)
            except Exception:
                width = 0
            if width <= 0:
                return 1
            s = "-" if txt is None else str(txt)
            wrapped = _wrap_text_for_width(s, width)
            return max(1, int(str(wrapped).count("\n")) + 1)

        try:
            _specs_for_h = specs if isinstance(specs, list) else None
            if not isinstance(_specs_for_h, list):
                _specs_for_h = self._table_col_specs()
            _by_key_h = {c["key"]: c for c in _specs_for_h}
            line_counts = [
                _need_lines_for_text(
                    order_date, (_by_key_h.get("date") or {}).get("width")
                ),
                _need_lines_for_text(
                    log_text, (_by_key_h.get("log") or {}).get("width")
                ),
                _need_lines_for_text(
                    str(invoice or ""), (_by_key_h.get("inv") or {}).get("width")
                ),
                _need_lines_for_text(
                    str(order_1c or ""), (_by_key_h.get("order1c") or {}).get("width")
                ),
                _need_lines_for_text(
                    str(product_text or ""),
                    (_by_key_h.get("product") or {}).get("width"),
                ),
                _need_lines_for_text(
                    self.STATUS_LABELS.get(status, status),
                    (_by_key_h.get("status") or {}).get("width"),
                ),
                _need_lines_for_text(
                    str(delivery_name or ""),
                    (_by_key_h.get("delivery") or {}).get("width"),
                ),
                _need_lines_for_text(
                    str(comment or ""), (_by_key_h.get("comment") or {}).get("width")
                ),
                _need_lines_for_text(
                    str(qty_text or ""), (_by_key_h.get("qty") or {}).get("width")
                ),
                _need_lines_for_text(
                    str(amount_display_text or ""),
                    (_by_key_h.get("amount") or {}).get("width"),
                ),
            ]
            if "amount_usd" in _by_key_h:
                line_counts.append(
                    _need_lines_for_text(
                        str(amount_usd_display_text or ""),
                        (_by_key_h.get("amount_usd") or {}).get("width"),
                    )
                )
            line_counts.append(
                _need_lines_for_text(
                    self.PAYMENT_LABELS.get(payment_st, payment_st),
                    (_by_key_h.get("payment") or {}).get("width"),
                )
            )
            line_counts.append(
                _need_lines_for_text(
                    self.SUBMISSION_LABELS.get(submission_st, submission_st),
                    (_by_key_h.get("submission") or {}).get("width"),
                )
            )
            max_lines_row = max(line_counts) if line_counts else 1
            if max_lines_row > 1:
                row_h = int(min(220, max(row_h, 16 * max_lines_row + 4)))
        except Exception:
            pass

        date_prod_end = (draft_order.get("date_shipped") or "")[:10]
        date_wh = (draft_order.get("date_arrived_warehouse") or "")[:10]
        date_1c = (draft_order.get("date_accepted") or "")[:10]
        dur_prod = _days_between(order_date, date_prod_end)
        dur_deliv = _days_between(date_prod_end, date_wh)
        dur_acc = _days_between(date_wh, date_1c)
        draft_order["duration_production_days"] = dur_prod
        draft_order["duration_transit_days"] = dur_deliv
        draft_order["duration_warehouse_days"] = dur_acc

        _prod_term_values = []
        for _it in draft_items:
            _it_pid = str((_it or {}).get("product_id") or "").strip()
            _it_prod = products_by_id.get(_it_pid) or {}
            _it_term = _safe_float(_it_prod.get("production_term_days"), None)
            if _it_term is not None:
                _prod_term_values.append(_it_term)
        avg_production_term_days = (
            sum(_prod_term_values) / len(_prod_term_values)
            if _prod_term_values
            else None
        )
        dur_prod_bg = row_bg
        if dur_prod is not None and avg_production_term_days is not None:
            # Actual production time longer than the average for these
            # products (per their product-card "Термін виробництва") → red;
            # at or under average → green.
            dur_prod_bg = (
                "#FEE2E2" if dur_prod > avg_production_term_days else "#DCFCE7"
            )

        if order_date and date_1c:
            log_days = _days_between(order_date, date_1c)
        elif status != "production" and order_date:
            log_days = _days_between(order_date, _today_str())
        else:
            log_days = None
        log_text = f"{log_days} дн." if log_days is not None else "—"
        if log_days is None:
            draft_order.pop("log_days", None)
        else:
            draft_order["log_days"] = log_days
        term_acc = dur_acc
        if term_acc is None and date_wh and date_1c:
            term_acc = _days_between(date_wh, date_1c)

        entered_fact_total = _safe_float(draft_order.get("__fact_weight_total"), None)
        resolved_weights, prev_weights = _resolve_actual_weights(
            draft_items, entered_fact_total, prod_map=products_by_id
        )
        prev_weight = sum(prev_weights)
        fact_weight = sum(resolved_weights)
        has_fact = (entered_fact_total is not None) or any(
            _safe_float((it or {}).get("weight_actual_kg"), None) is not None
            for it in draft_items
        )
        prev_weight_text = f"{round(prev_weight)}" if prev_weight else ""
        fact_weight_text = f"{round(fact_weight)}" if has_fact else ""
        tariff = _safe_float(draft_order.get("delivery_price_snapshot"), None)
        tariff_ccy = (
            (draft_order.get("delivery_price_currency_snapshot") or "").upper().strip()
        )
        delivery_type_id = str((order.get("delivery_type_id") or "")).strip()
        delivery_type_name = str(
            (order.get("delivery_type_name") or order.get("delivery_type") or "")
        ).strip()

        selected_delivery_row = (
            (self._delivery_methods_by_id or {}).get(delivery_type_id)
            if delivery_type_id
            else None
        )
        if selected_delivery_row is None and delivery_type_name:
            dn = delivery_type_name.lower().strip()
            selected_delivery_row = next(
                (
                    m
                    for m in (self._delivery_methods or [])
                    if str((m or {}).get("name") or "").lower().strip() == dn
                ),
                None,
            )
        if tariff is None and isinstance(selected_delivery_row, dict):
            tariff, tariff_ccy = self._delivery_method_tariff(selected_delivery_row)
        if tariff_ccy == "" and isinstance(selected_delivery_row, dict):
            _p_tmp, _c_tmp = self._delivery_method_tariff(selected_delivery_row)
            tariff_ccy = _c_tmp or tariff_ccy
        tariff_text = f"{tariff:.2f} {tariff_ccy}" if tariff is not None else ""

        delivery_by_id = {
            str((selected_delivery_row or {}).get("id") or ""): selected_delivery_row
        }
        date_receive = self._order_receive_date_iso(
            draft_order,
            products_by_id=products_by_id,
            delivery_by_id=delivery_by_id,
        )
        date_payment = self._order_payment_date_iso(
            draft_order,
            products_by_id=products_by_id,
        )

        def _safe_iso_date(value: str) -> Optional[date]:
            raw = str(value or "").strip()[:10]
            if len(raw) != 10 or raw[4] != "-" or raw[7] != "-":
                return None
            try:
                return date.fromisoformat(raw)
            except Exception:
                return None

        receive_dt = _safe_iso_date(date_receive)
        accepted_dt = _safe_iso_date(date_1c)
        today_dt = date.today()
        date_receive_bg = "#DBEAFE"  # default blue
        if receive_dt is not None:
            if accepted_dt is not None:
                # Прийомка filled: planned < actual → late → red; planned > actual → early → green
                if receive_dt < accepted_dt:
                    date_receive_bg = "#FEE2E2"  # red — delivered late
                elif receive_dt > accepted_dt:
                    date_receive_bg = "#DCFCE7"  # green — delivered early
                # equal → stays blue
            else:
                # Прийомка empty: colour by urgency relative to today
                days_until = (receive_dt - today_dt).days
                if days_until > 7:
                    date_receive_bg = "#DCFCE7"  # green — >7 days ahead
                elif days_until >= 1:
                    date_receive_bg = "#FEF9C3"  # yellow — 1–7 days ahead
                else:
                    date_receive_bg = "#FEE2E2"  # red — today or overdue

        deviation_days: Optional[int] = None
        deviation_text = ""
        deviation_bg = row_bg
        if receive_dt is not None:
            ref_dt = accepted_dt if accepted_dt is not None else today_dt
            deviation_days = int((ref_dt - receive_dt).days)
            deviation_text = f"{deviation_days} дн."
            deviation_bg = "#DBEAFE" if deviation_days <= 0 else "#FEE2E2"

        def _current_tariff_usd() -> tuple[Optional[float], str, Optional[float], str]:
            t = _safe_float(draft_order.get("delivery_price_snapshot"), None)
            t_ccy = (
                (
                    draft_order.get("delivery_price_currency_snapshot")
                    or tariff_ccy
                    or ""
                )
                .upper()
                .strip()
            )
            if t is None and isinstance(selected_delivery_row, dict):
                t, t_ccy = self._delivery_method_tariff(selected_delivery_row)
            if t_ccy == "" and isinstance(selected_delivery_row, dict):
                _p_tmp2, _c_tmp2 = self._delivery_method_tariff(selected_delivery_row)
                t_ccy = _c_tmp2 or t_ccy
            t_text = f"{t:.2f} {t_ccy}" if t is not None else ""
            t_ccy_eff = (t_ccy or "USD").upper()
            t_usd = self._convert_currency_amount(
                t or 0.0,
                t_ccy_eff,
                "USD",
            )
            return t, t_ccy, t_usd, t_text

        tariff, tariff_ccy, tariff_usd, tariff_text = _current_tariff_usd()

        def _draft_item_amounts_and_uc():
            _t, _tccy, _tusd, _ttext = _current_tariff_usd()
            try:
                resolved_w_for_calc, _prev_w_for_calc = _resolve_actual_weights(
                    draft_items, None, prod_map=products_by_id
                )
            except Exception:
                resolved_w_for_calc = []
            item_amount_usd_vals_l = []
            item_unit_cost_usd_l = []
            item_unit_cost_ccy_l = []
            for idx, it in enumerate(draft_items):
                q = _item_qty_value(it)
                p, ccy = _effective_item_pricing(it)
                goods_amt = (q * p) if p is not None else None
                goods_usd = self._convert_currency_amount(
                    goods_amt,
                    ccy,
                    "USD",
                )
                if manual_amount_override is not None and len(draft_items) == 1:
                    goods_usd = self._convert_currency_amount(
                        manual_amount_override,
                        base_item_ccy,
                        "USD",
                    )
                item_amount_usd_vals_l.append(goods_usd)

                # Cost-price pivot: this item's own currency if the purchase
                # price and logistics tariff share one currency, otherwise USD.
                item_ccy = str(
                    (base_item_ccy if manual_amount_override is not None and len(draft_items) == 1 else ccy)
                    or "USD"
                ).upper().strip() or "USD"
                pivot_ccy = (
                    fx_rates.resolve_pivot_currency([item_ccy, _tccy])
                    if _t is not None
                    else item_ccy
                )
                goods_pivot = self._convert_currency_amount(goods_amt, ccy, pivot_ccy)
                if manual_amount_override is not None and len(draft_items) == 1:
                    goods_pivot = self._convert_currency_amount(
                        manual_amount_override, base_item_ccy, pivot_ccy
                    )
                tariff_pivot = (
                    self._convert_currency_amount(_t, _tccy, pivot_ccy)
                    if _t is not None
                    else None
                )
                ship_pivot = (
                    (
                        tariff_pivot
                        * (
                            resolved_w_for_calc[idx]
                            if idx < len(resolved_w_for_calc)
                            else 0.0
                        )
                    )
                    if tariff_pivot is not None
                    else 0.0
                )
                if q > 0 and goods_pivot is not None:
                    item_unit_cost_usd_l.append(((goods_pivot + ship_pivot) / q))
                else:
                    item_unit_cost_usd_l.append(None)
                item_unit_cost_ccy_l.append(pivot_ccy)
            return item_amount_usd_vals_l, item_unit_cost_usd_l, item_unit_cost_ccy_l

        item_amount_usd_vals, item_unit_cost_usd, item_unit_cost_ccy = (
            _draft_item_amounts_and_uc()
        )

        for _ui, _dit in enumerate(draft_items):
            _calc = _safe_float((_dit or {}).get("unit_cost_calc_usd"), None)
            if _calc is None:
                continue
            while _ui >= len(item_unit_cost_usd):
                item_unit_cost_usd.append(None)
            while _ui >= len(item_unit_cost_ccy):
                item_unit_cost_ccy.append("USD")
            item_unit_cost_usd[_ui] = float(_calc)
            item_unit_cost_ccy[_ui] = (
                str((_dit or {}).get("unit_cost_calc_currency") or "USD").upper().strip()
                or "USD"
            )

        unit_cost_text = ""
        if len(item_unit_cost_usd) == 1 and item_unit_cost_usd[0] is not None:
            _uc_ccy0 = item_unit_cost_ccy[0] if item_unit_cost_ccy else "USD"
            unit_cost_text = format_grouped(item_unit_cost_usd[0], decimals=2)
            if _uc_ccy0 and _uc_ccy0 != "USD":
                unit_cost_text = f"{unit_cost_text} {_uc_ccy0}"

        if not isinstance(specs, list):
            hide_amount_usd = False
            try:
                supplier_id = str((order or {}).get("supplier_id") or "").strip()
                sup_row = (self._suppliers_by_id or {}).get(supplier_id)
                sup_ccy = (
                    str(
                        (sup_row or {}).get("currency")
                        or (sup_row or {}).get("supplier_currency")
                        or ""
                    )
                    .upper()
                    .strip()
                )
                hide_amount_usd = sup_ccy == "USD"
            except Exception:
                hide_amount_usd = False
            specs = self._table_col_specs(hide_amount_usd=hide_amount_usd)
        by_key = {c["key"]: c for c in specs}
        order_id = (order.get("id") or "").strip()

        def _estimate_lines_for_cell(key: str, value: str) -> int:
            col_w = int((by_key.get(key) or {}).get("width") or 80)
            wrapped = _wrap_text_for_width(str(value or ""), col_w)
            lines = len([ln for ln in wrapped.split("\n") if ln.strip() != ""])
            return max(1, min(6, lines))

        row_line_need = max(
            _estimate_lines_for_cell("product", product_text),
            _estimate_lines_for_cell("comment", comment),
            _estimate_lines_for_cell("delivery", delivery_name),
            _estimate_lines_for_cell("status", self.STATUS_LABELS.get(status, status)),
            _estimate_lines_for_cell(
                "payment", self.PAYMENT_LABELS.get(payment_st, payment_st)
            ),
            _estimate_lines_for_cell(
                "submission", self.SUBMISSION_LABELS.get(submission_st, submission_st)
            ),
        )
        row_h = max(row_h, min(120, 4 + row_line_need * 14))

        def _status_colors(st: str):
            s = str(st or "").strip()
            if s == "production":
                return "#FECACA", "#7F1D1D"
            if s == "in_transit":
                return "#BFDBFE", "#1E3A8A"
            if s == "at_warehouse":
                return "#DCFCE7", "#166534"
            if s == "accepted":
                return "#065F46", "#FFFFFF"
            return "#FFFFFF", "#1F2937"

        def _payment_colors(st: str):
            s = str(st or "").strip().lower()
            if s == "paid":
                return "#DCFCE7", "#166534"
            if s == "advance":
                return "#FED7AA", "#92400E"
            if s == "transferred":
                return "#FEF3C7", "#CA8A04"
            return "#FEE2E2", "#991B1B"

        def _submission_colors(st: str):
            if str(st or "").strip() == "submitted":
                return "#065F46", "#FFFFFF"
            return "#FECACA", "#7F1D1D"

        delivery_palette = [
            "#FEF3C7",
            "#FDE68A",
            "#FECACA",
            "#FCA5A5",
            "#DCFCE7",
            "#BBF7D0",
            "#BFDBFE",
            "#93C5FD",
            "#E9D5FF",
            "#DDD6FE",
        ]

        def _delivery_colors(name: str):
            raw = str(name or "").strip()
            if not raw:
                return "#FFFFFF", "#1F2937"
            idx = sum(ord(ch) for ch in raw) % len(delivery_palette)
            return delivery_palette[idx], "#1F2937"

        status_bg, status_fg = _status_colors(status)
        payment_bg, payment_fg = _payment_colors(payment_st)
        submission_bg, submission_fg = _submission_colors(submission_st)
        delivery_bg, delivery_fg = _delivery_colors(delivery_name)

        def _cell(
            key: str,
            text: str = "",
            size: Union[int, float] = 8,
            bold: bool = False,
            color: str = _TEXT,
            bgcolor: str = row_bg,
            no_wrap: bool = False,
            max_lines: int = 10,
        ):
            col = by_key[key]
            talign = ft.TextAlign.LEFT if key == "product" else ft.TextAlign.CENTER
            return self._table_cell(
                text=text,
                width=col.get("width"),
                expand=bool(col.get("expand")),
                size=size,
                bold=bold,
                color=color,
                height=row_h,
                text_align=talign,
                no_wrap=no_wrap,
                max_lines=max_lines,
                bgcolor=bgcolor,
            )

        dirty_state = {"value": False}
        row_container_ref = ft.Ref[ft.Container]()
        summary_qty_text = ft.Text(
            qty_text,
            size=8,
            text_align=ft.TextAlign.CENTER,
            no_wrap=False,
            overflow=ft.TextOverflow.VISIBLE,
        )
        summary_amount_text = ft.Text(
            amount_display_text,
            size=8,
            text_align=ft.TextAlign.CENTER,
            no_wrap=False,
            overflow=ft.TextOverflow.VISIBLE,
            max_lines=10,
        )
        summary_amount_usd_text = ft.Text(
            amount_usd_display_text,
            size=8,
            text_align=ft.TextAlign.CENTER,
            no_wrap=False,
            overflow=ft.TextOverflow.VISIBLE,
            max_lines=10,
        )

        edit_actions = []
        role_slug = (
            str((self.user_data or {}).get("role") or "")
            .strip()
            .lower()
            .replace(" ", "")
        )
        is_privileged = role_slug in {
            "admin",
            "адмін",
            "админ",
            "superadmin",
            "суперадмін",
            "суперадмин",
        }
        editable_product_ids = self._editable_product_ids
        can_inline_edit = bool(not self.is_read_only) and (
            is_privileged
            or bool(editable_product_ids)
            or self._is_order_owned_by_current_manager(order)
        )
        can_delete = (not self.is_read_only) and (
            self._is_order_owned_by_current_manager(order) or is_privileged
        )
        if can_inline_edit or can_delete:

            def _del_fn(oid=order.get("id")):
                return lambda _: self._confirm_delete_order(oid)

            def _edit_fn(o=order):
                return lambda _: self._open_order_dialog(existing=o)

            if can_inline_edit:
                edit_actions.append(
                    ft.IconButton(
                        ft.Icons.EDIT_OUTLINED,
                        icon_size=13,
                        on_click=_edit_fn(),
                        tooltip=self.t("orders.tooltip.edit", "Редагувати (в т.ч. декілька товарів)"),
                    )
                )

            if can_delete:
                edit_actions.append(
                    ft.IconButton(
                        ft.Icons.DELETE_OUTLINE,
                        icon_size=13,
                        on_click=_del_fn(),
                        tooltip=self.t("common.delete", "Видалити"),
                    ),
                )

        edit_action_cell = ft.Container(
            width=by_key["action_edit"].get("width"),
            height=row_h,
            alignment=ft.Alignment(0, 0),
            border=ft.border.all(1, "#CBD5E1"),
            border_radius=0,
            bgcolor=row_bg,
            content=(
                ft.Row(
                    edit_actions,
                    spacing=1,
                    wrap=False,
                    alignment=ft.MainAxisAlignment.CENTER,
                )
                if edit_actions
                else ft.Text("", size=8, color=_TEXT)
            ),
        )

        lw_ex = int((by_key.get("lines_expand") or {}).get("width") or 22)
        _lines_expanded = bool(
            order_id and order_id in self._expanded_multi_item_order_ids
        )
        _sub_rows_col_ref: dict = {"col": None}
        if len(items) > 1 and order_id:
            lines_expand_btn = ft.IconButton(
                icon=ft.Icons.REMOVE if _lines_expanded else ft.Icons.ADD,
                icon_size=14,
                tooltip=(
                    "Згорнути рядки товарів"
                    if _lines_expanded
                    else "Показати рядки товарів"
                ),
                style=ft.ButtonStyle(
                    padding=ft.padding.all(0),
                    shape=ft.RoundedRectangleBorder(radius=4),
                ),
                width=lw_ex,
                height=row_h,
            )
        else:
            lines_expand_btn = None

        lines_expand_cell = ft.Container(
            width=lw_ex,
            height=row_h,
            alignment=ft.Alignment(0, 0),
            border=ft.border.all(1, "#CBD5E1"),
            border_radius=0,
            bgcolor=row_bg,
            content=(
                lines_expand_btn
                if lines_expand_btn is not None
                else ft.Text("", size=8)
            ),
        )

        if lines_expand_btn is not None:

            def _toggle_multi_item_rows(_):
                oid = order_id
                if not oid:
                    return
                if oid in self._expanded_multi_item_order_ids:
                    self._expanded_multi_item_order_ids.discard(oid)
                else:
                    self._expanded_multi_item_order_ids.add(oid)
                expanded_now = oid in self._expanded_multi_item_order_ids
                col = _sub_rows_col_ref.get("col")
                if col is not None:
                    col.visible = expanded_now
                    lines_expand_btn.icon = (
                        ft.Icons.REMOVE if expanded_now else ft.Icons.ADD
                    )
                    lines_expand_btn.tooltip = (
                        "Згорнути рядки товарів"
                        if expanded_now
                        else "Показати рядки товарів"
                    )
                    try:
                        col.update()
                        lines_expand_btn.update()
                    except Exception:
                        pass
                else:
                    sid_eff = (order.get("supplier_id") or "") or "__no_supplier__"
                    try:
                        self._render_tree(
                            force_full=False, affected_supplier_ids=[sid_eff]
                        )
                    except Exception:
                        pass

            lines_expand_btn.on_click = _toggle_multi_item_rows

        def _set_dirty(value: bool):
            dirty_state["value"] = bool(value)
            self._set_order_draft_dirty(order_id, dirty_state["value"])

        def _refresh_totals_controls():
            q, a = _compute_totals(draft_items)
            summary_qty_text.value = q
            try:
                _p0, _c0 = _effective_item_pricing(
                    (draft_items[0] if draft_items else {}) or {}
                )
                c0 = _c0 or base_item_ccy or "USD"
            except Exception:
                c0 = base_item_ccy or "USD"
            try:
                w_amt = int((by_key.get("amount") or {}).get("width") or 0)
            except Exception:
                w_amt = 0
            summary_amount_text.value = _wrap_text_for_width(
                _amount_with_ccy(a, c0), w_amt
            )
            amount_usd_live = _items_total_usd(draft_items)
            try:
                w_usd = int((by_key.get("amount_usd") or {}).get("width") or 0)
            except Exception:
                w_usd = 0
            summary_amount_usd_text.value = _wrap_text_for_width(
                _amount_with_ccy(
                    (
                        format_grouped(amount_usd_live, decimals=2)
                        if amount_usd_live is not None
                        else ""
                    ),
                    "USD",
                ),
                w_usd,
            )
            try:
                summary_qty_text.update()
                summary_amount_text.update()
                summary_amount_usd_text.update()
            except Exception:
                pass

        def _recalc_duration_fields():
            d0 = (draft_order.get("order_date") or "")[:10]
            d1 = (draft_order.get("date_shipped") or "")[:10]
            d2 = (draft_order.get("date_arrived_warehouse") or "")[:10]
            d3 = (draft_order.get("date_accepted") or "")[:10]
            draft_order["duration_production_days"] = _days_between(d0, d1)
            draft_order["duration_transit_days"] = _days_between(d1, d2)
            draft_order["duration_warehouse_days"] = _days_between(d2, d3)

        def _recalc_log_days():
            nonlocal log_days, log_text
            d0 = (draft_order.get("order_date") or "")[:10]
            d3 = (draft_order.get("date_accepted") or "")[:10]
            if d3 and d0:
                log_days = _days_between(d0, d3)
            elif status != "production" and d0:
                log_days = _days_between(d0, _today_str())
            else:
                log_days = None
            log_text = f"{log_days} дн." if log_days is not None else "—"
            if log_days is not None:
                draft_order["log_days"] = log_days
            else:
                draft_order.pop("log_days", None)

        def _recalc_deviation_days():
            d3 = (draft_order.get("date_accepted") or "")[:10]
            if not d3:
                draft_order.pop("deviation_days", None)
                return
            status_now = str(draft_order.get("status") or "").strip().lower()
            is_accepted_now = status_now in {
                "accepted",
                "прийнятий",
                "прийняті",
                "прийнято",
            }
            if not is_accepted_now:
                draft_order.pop("deviation_days", None)
                return

            delivery_by_id = {
                str((m or {}).get("id") or "").strip(): m
                for m in (self._delivery_methods or [])
                if isinstance(m, dict) and str((m or {}).get("id") or "").strip()
            }
            receive_iso = self._order_receive_date_iso(
                draft_order,
                products_by_id=products_by_id,
                delivery_by_id=delivery_by_id,
            )
            dev = _days_between(receive_iso, d3)
            if dev is None:
                draft_order.pop("deviation_days", None)
                return
            draft_order["deviation_days"] = int(dev)

        def _build_persistable_draft_snapshot():
            if not dirty_state["value"]:
                return None
            self._suppress_auto_render_until_ts = time.time() + 2.0
            # Cost-price conversion below needs live FX rates; a cold cache
            # (fresh process, or 20-min TTL just expired) would otherwise
            # silently drop unit_cost_calc_usd from what gets saved.
            fx_rates.ensure_rates_loaded()
            _recalc_duration_fields()
            _recalc_log_days()
            _recalc_deviation_days()

            for it in draft_items:
                if not isinstance(it, dict):
                    continue
                qv = _item_qty_value(it)
                it["qty"] = qv
                it["quantity"] = qv

            entered_fact_total = _safe_float(
                draft_order.pop("__fact_weight_total", None), None
            )
            if entered_fact_total is not None and draft_items:
                resolved_final, _ = _resolve_actual_weights(
                    draft_items, entered_fact_total, prod_map=products_by_id
                )
                for i, it in enumerate(draft_items):
                    if i < len(resolved_final):
                        it["weight_actual_kg"] = round(resolved_final[i], 6)

            _am_tmp, _uc_tmp, _uc_ccy_tmp = _draft_item_amounts_and_uc()
            for _ii, _pit in enumerate(draft_items):
                if not isinstance(_pit, dict):
                    continue
                _pit.pop("unit_cost_manual_override", None)
                _calc = None
                if _ii < len(_uc_tmp) and _uc_tmp[_ii] is not None:
                    try:
                        _calc = float(_uc_tmp[_ii])
                    except Exception:
                        _calc = None
                if _calc is not None:
                    _pit["unit_cost_calc_usd"] = round(_calc, 6)
                    _pit["unit_cost_calc_currency"] = (
                        _uc_ccy_tmp[_ii] if _ii < len(_uc_ccy_tmp) else "USD"
                    )
                    _pit["unit_cost_display"] = f"{_calc:.2f}"
                else:
                    _pit.pop("unit_cost_calc_usd", None)
                    _pit.pop("unit_cost_calc_currency", None)
                    _pit.pop("unit_cost_display", None)
                _pit.pop("unit_cost_locked_usd", None)

            updated = dict(draft_order)
            updated["items"] = [dict(it) for it in draft_items]
            updated["updated_at"] = datetime.utcnow().isoformat(timespec="seconds")
            return updated

        def _apply_persisted_draft_snapshot(
            updated: dict, force_full=False, skip_ui_refresh=False
        ):
            if not isinstance(updated, dict):
                return
            if skip_ui_refresh:
                # Grid-only save (invoice/packing table). Update local order and shared
                # map silently — no __on_change__, no resume(), no row rebuild.
                # MUST update shared_map too: without it, _bg_sync_loop detects a
                # "cross-tab change" 1.5 s later (local has grid data, shared doesn't)
                # and overwrites local with the old version, erasing the grid.
                _oid = (updated.get("id") or "").strip()
                if _oid:
                    self._orders_cache[_oid] = dict(updated)
                    for _i, _o in enumerate(self._orders):
                        if (
                            isinstance(_o, dict)
                            and (_o.get("id") or "").strip() == _oid
                        ):
                            self._orders[_i] = updated
                            break
                    # Update shared_map directly (no _bump_sync_seq → no __on_change__)
                    # so _sync_from_shared_map sees no difference and won't overwrite us.
                    if self._shared_orders_map is not None:
                        self._shared_orders_map[_oid] = dict(updated)
                _set_dirty(False)
                _refresh_totals_controls()
                self._schedule_orders_snapshot_save()
                return
            self._merge_local_order(updated)
            _set_dirty(False)
            _refresh_totals_controls()
            self._update_counters()
            refreshed = False
            if not force_full:
                refreshed = self._refresh_single_order_row(updated)
            if force_full or (not refreshed):
                sid = (updated.get("supplier_id") or "") or "__no_supplier__"
                self._render_tree(
                    force_full=bool(force_full),
                    affected_supplier_ids=None if force_full else {sid},
                )

        def _persist_draft(_=None, force_full=False, skip_ui_refresh=False):
            updated = _build_persistable_draft_snapshot()
            if not updated:
                return
            if self._upsert_order(updated):
                _apply_persisted_draft_snapshot(
                    updated,
                    force_full=force_full,
                    skip_ui_refresh=skip_ui_refresh,
                )
            else:
                self._show_snack(self.t("orders.err.save_failed", "Не вдалося зберегти"), "#DC2626")

        def _queue_draft_for_sync():
            updated = _build_persistable_draft_snapshot()
            if not updated:
                return None
            self._orders_cache[order_id] = dict(updated)
            self._merge_local_order(updated)
            _set_dirty(False)
            with contextlib.suppress(Exception):
                _refresh_totals_controls()
            if self._upsert_order(updated):
                return dict(updated)
            return None

        self._register_force_sync_hook(order_id, _queue_draft_for_sync)

        live_commit_state = {"value": False}
        date_commit_state = {"running": False, "pending": None, "busy_token": None}
        draft_commit_state = {"running": False, "pending": None}

        def _persist_draft_async(
            force_full=False, skip_ui_refresh=False, skip_notify=False
        ):
            updated = _build_persistable_draft_snapshot()
            if not updated:
                return

            # Immediately commit to local outbox (pure-Python dict ops, no IO)
            # so the indicator switches to "Локально" right now — before any
            # async task is scheduled — eliminating the "Не збережено" flash.
            # silent=True (skip_notify) skips __on_change__ to avoid resume() on all views.
            self._upsert_order(updated, silent=bool(skip_notify))

            page_local = self._page()
            payload = {
                "snapshot": dict(updated),
                "force_full": bool(force_full),
                "skip_ui_refresh": bool(skip_ui_refresh),
                "skip_notify": bool(skip_notify),
            }

            if page_local and hasattr(page_local, "run_task"):

                async def _persist_async(state_payload: dict):
                    current_payload = dict(state_payload or {})
                    try:
                        while True:
                            current = dict(current_payload.get("snapshot") or {})
                            _skip_ui = bool(
                                current_payload.get("skip_ui_refresh") or False
                            )
                            _silent = bool(current_payload.get("skip_notify") or False)
                            ok = self._upsert_order(dict(current), silent=_silent)
                            pending = draft_commit_state.get("pending")
                            if pending:
                                draft_commit_state["pending"] = None
                                current_payload = dict(pending)
                                continue
                            if ok:
                                _apply_persisted_draft_snapshot(
                                    current,
                                    force_full=bool(
                                        current_payload.get("force_full") or False
                                    ),
                                    skip_ui_refresh=_skip_ui,
                                )
                            else:
                                self._show_snack(self.t("orders.err.save_failed", "Не вдалося зберегти"), "#DC2626")
                            break
                    finally:
                        draft_commit_state["running"] = False

                if draft_commit_state["running"]:
                    draft_commit_state["pending"] = dict(payload)
                else:
                    scheduled = False
                    draft_commit_state["running"] = True
                    try:
                        page_local.run_task(_persist_async, payload)
                        scheduled = True
                    except Exception:
                        draft_commit_state["running"] = False
                        scheduled = False
                    if scheduled:
                        return

            if self._upsert_order(updated, silent=bool(skip_notify)):
                _apply_persisted_draft_snapshot(
                    updated,
                    force_full=force_full,
                    skip_ui_refresh=skip_ui_refresh,
                )
            else:
                self._show_snack(self.t("orders.err.save_failed", "Не вдалося зберегти"), "#DC2626")

        def _mark_dirty_and_update():
            _set_dirty(True)
            if live_commit_state["value"]:
                _persist_draft_async(skip_ui_refresh=True)
            else:
                _optimistic_commit_draft()

        def _optimistic_commit_draft():
            _set_dirty(True)

            # Refresh unit_cost_calc_usd so the row re-render shows current С/В
            try:
                _am_opt, _uc_opt, _uc_ccy_opt = _draft_item_amounts_and_uc()
                for _ii, _pit in enumerate(draft_items):
                    if not isinstance(_pit, dict):
                        continue
                    if _ii < len(_uc_opt) and _uc_opt[_ii] is not None:
                        _pit["unit_cost_calc_usd"] = round(float(_uc_opt[_ii]), 6)
                        _pit["unit_cost_calc_currency"] = (
                            _uc_ccy_opt[_ii] if _ii < len(_uc_ccy_opt) else "USD"
                        )
                    else:
                        _pit.pop("unit_cost_calc_usd", None)
                        _pit.pop("unit_cost_calc_currency", None)
            except Exception:
                pass

            preview = dict(draft_order)
            preview["items"] = [dict(it) for it in draft_items]

            self._merge_local_order(preview)
            # Save to local outbox first so the indicator already shows "Локально"
            # before we start the (potentially slow) row rebuild below.
            _persist_draft_async(skip_ui_refresh=True)
            with contextlib.suppress(Exception):
                self._refresh_single_order_row(preview)
            with contextlib.suppress(Exception):
                self._update_counters()

        # Order-level date fields that a sub-item can individually override
        # (production/warehouse-arrival/acceptance can genuinely happen on
        # different days per line — e.g. one line ready earlier than
        # another). Absent an override, an item's *_item value is None and
        # it simply mirrors the order's own date at render time — see
        # _effective_item_date_field().
        _ITEM_DATE_OVERRIDE_FIELDS = {
            "date_shipped",
            "date_arrived_warehouse",
            "date_accepted",
        }

        def _persist_order_via_outbox(updated: dict) -> None:
            """Shared tail for _commit_date_fast/_commit_item_date_fast:
            commit to the local outbox immediately (so "Не збережено" never
            flashes) and flush it, coalescing rapid repeated edits into one
            DB write."""
            self._last_local_fast_edit_ts = time.time()
            page_local = self._page()
            self._upsert_order(updated)
            if page_local and hasattr(page_local, "run_task"):

                async def _persist_async(snapshot: dict):
                    current = dict(snapshot or {})
                    try:
                        while True:
                            pending = date_commit_state.get("pending")
                            if pending:
                                date_commit_state["pending"] = None
                                current = dict(pending)
                                self._upsert_order(dict(current))
                                continue
                            _set_dirty(False)
                            break
                    finally:
                        date_commit_state["running"] = False

                if date_commit_state["running"]:
                    date_commit_state["pending"] = dict(updated)
                else:
                    date_commit_state["running"] = True
                    scheduled = False
                    try:
                        page_local.run_task(_persist_async, updated)
                        scheduled = True
                    except Exception:
                        scheduled = False
                    if not scheduled:
                        date_commit_state["running"] = False
                        if self._upsert_order(updated):
                            _set_dirty(False)
                        else:
                            self._show_snack(self.t("orders.err.save_failed", "Не вдалося зберегти"), "#DC2626")
            else:
                if self._upsert_order(updated):
                    _set_dirty(False)
                else:
                    self._show_snack(self.t("orders.err.save_failed", "Не вдалося зберегти"), "#DC2626")

        def _commit_date_fast(field_name: str, raw_value: str) -> None:
            v = str(raw_value or "").strip()[:10]
            prev_v = str(draft_order.get(field_name) or "").strip()[:10]
            if v == prev_v:
                return

            old_group_key = (
                self._order_group_key(dict(draft_order))
                if field_name == "order_date"
                else None
            )

            # Any item still following this order's OLD date (no deliberate
            # override) keeps following it to the NEW date too — clear its
            # override so it re-inherits at render time. An item deliberately
            # set to a different date is left alone.
            if field_name in _ITEM_DATE_OVERRIDE_FIELDS:
                item_override_key = f"{field_name}_item"
                for _it in draft_items:
                    if not isinstance(_it, dict):
                        continue
                    if str(_it.get(item_override_key) or "").strip()[:10] == prev_v:
                        _it.pop(item_override_key, None)

            draft_order[field_name] = v
            _set_dirty(True)
            _recalc_duration_fields()
            _recalc_log_days()

            updated = dict(draft_order)
            updated["items"] = [dict(it) for it in draft_items]
            updated["updated_at"] = datetime.utcnow().isoformat(timespec="seconds")

            self._merge_local_order(updated)

            group_changed = (
                field_name == "order_date"
                and old_group_key is not None
                and self._order_group_key(updated) != old_group_key
            )

            if group_changed:
                sid_eff = (updated.get("supplier_id") or "") or "__no_supplier__"
                affected: set = {sid_eff}
                if old_group_key:
                    old_sid = old_group_key[0] or "__no_supplier__"
                    affected.add(old_sid)
                with contextlib.suppress(Exception):
                    self._render_tree(force_full=False, affected_supplier_ids=affected)
                # Immediately open the destination month so the order stays visible.
                with contextlib.suppress(Exception):
                    self._inject_new_order_to_tree(updated)
            else:
                if field_name == "order_date":
                    with contextlib.suppress(Exception):
                        repositioned = self._reposition_order_row_in_month(updated)
                else:
                    repositioned = False

                oid_fast = (updated.get("id") or "").strip()
                # fast_fn (_fast_date_refresh) only patches the TOP-level
                # row's own duration/log cells — it has no idea sub-item
                # rows even exist, so it never touches them. For a
                # multi-item order that leaves every sub-row (date, its
                # duration, tariff, С/В) permanently stale: only a full
                # single-row rebuild (_refresh_single_order_row, which goes
                # through _build_order_row and regenerates every sub-item
                # cell) can reflect the change there. So skip the
                # order-only fast path whenever there's more than one item.
                fast_fn = (
                    self._order_fast_refresh.get(oid_fast)
                    if oid_fast and len(draft_items) <= 1
                    else None
                )
                fast_ok = False
                if fast_fn:
                    with contextlib.suppress(Exception):
                        fast_ok = fast_fn(updated)
                if not fast_ok:
                    with contextlib.suppress(Exception):
                        self._refresh_single_order_row(updated)
                if not repositioned and field_name == "order_date":
                    with contextlib.suppress(Exception):
                        self._reposition_order_row_in_month(updated)

            _persist_order_via_outbox(updated)

        def _effective_item_date_field(it: dict, field_name: str) -> str:
            item_override_key = f"{field_name}_item"
            v = str((it or {}).get(item_override_key) or "").strip()[:10]
            return v or str(draft_order.get(field_name) or "").strip()[:10]

        def _commit_item_date_fast(
            item_idx: int, field_name: str, raw_value: str
        ) -> None:
            if not (0 <= item_idx < len(draft_items)):
                return
            it = draft_items[item_idx]
            v = str(raw_value or "").strip()[:10]
            item_override_key = f"{field_name}_item"
            order_v = str(draft_order.get(field_name) or "").strip()[:10]
            cur_v = _effective_item_date_field(it, field_name)
            if v == cur_v:
                return
            if v == order_v:
                # Matches the order's own date again -> not a real
                # override anymore, so it keeps following future order
                # date changes instead of getting stuck on this one.
                it.pop(item_override_key, None)
            else:
                it[item_override_key] = v
            _set_dirty(True)

            updated = dict(draft_order)
            updated["items"] = [dict(x) for x in draft_items]
            updated["updated_at"] = datetime.utcnow().isoformat(timespec="seconds")
            self._merge_local_order(updated)

            # No fast_fn attempt here: an item-level date only ever exists
            # in a multi-item order's sub-rows, and fast_fn only patches
            # the top-level row's own cells — it would report success
            # without ever touching the item cell that actually changed.
            with contextlib.suppress(Exception):
                self._refresh_single_order_row(updated)

            _persist_order_via_outbox(updated)

        def _inline_text_field(
            value: str,
            width: int,
            on_commit,
            numeric: bool = False,
            multiline: bool = False,
            allow_empty_numeric: bool = False,
            live_commit: bool = False,
            clear_on_focus: bool = False,
            bgcolor: str = "#FFFFFF",
            text_color: str = "#111827",
            bold: bool = False,
            text_size: int = 8,
        ):
            try:
                _h_i = int(row_h or 0)
            except Exception:
                _h_i = 0
            try:
                _ts_i = int(text_size or 8)
            except Exception:
                _ts_i = 8
            try:
                if multiline:
                    col_w = max(int(width or 80), 40)
                    wrapped = _wrap_text_for_width(str(value or ""), col_w)
                    nonempty = [
                        ln for ln in wrapped.split("\n") if str(ln).strip() != ""
                    ]
                    clines = max(1, min(6, len(nonempty) if nonempty else 1))
                    line_px = int(_ts_i + 6)
                    inner_h = min(_h_i, clines * line_px) if _h_i else clines * line_px
                    vpad = max(0, int((_h_i - inner_h) / 2)) if _h_i else 0
                else:
                    vpad = max(0, int((_h_i - (_ts_i + 6)) / 2))
            except Exception:
                vpad = max(0, int((_h_i - (_ts_i + 6)) / 2)) if _h_i else 0
            tf = ft.TextField(
                value=str(value or ""),
                width=max(int(width or 80), 40),
                height=row_h,
                text_size=text_size,
                color=text_color,
                text_style=ft.TextStyle(
                    weight=(ft.FontWeight.W_700 if bold else ft.FontWeight.W_400)
                ),
                dense=True,
                border=ft.InputBorder.NONE,
                border_width=0,
                border_radius=0,
                content_padding=ft.padding.symmetric(horizontal=0, vertical=vpad),
                text_align=ft.TextAlign.CENTER,
                text_vertical_align=ft.VerticalAlignment.CENTER,
                filled=True,
                fill_color=bgcolor,
                multiline=multiline,
                min_lines=(1 if multiline else None),
                max_lines=(5 if multiline else 1),
            )

            if clear_on_focus:

                def _on_focus(_e, _tf=tf):
                    try:
                        _tf.value = ""
                        if _tf.page:
                            _tf.update()
                    except Exception:
                        pass

                tf.on_focus = _on_focus

            def _commit(_=None, live=False):
                val = (tf.value or "").strip()
                if numeric:
                    if allow_empty_numeric and val == "":
                        on_commit(None)
                        return
                    vv = str(val or "")
                    with contextlib.suppress(Exception):
                        vv = vv.replace("zł", "")
                    for ch in ["$", "₴", "€", "¥"]:
                        vv = vv.replace(ch, "")
                    vv = "".join(
                        [c for c in vv if (c.isdigit() or c in {".", ",", "-", " "})]
                    )
                    n = _safe_float(vv)
                    if n is None:
                        if live:
                            return
                        tf.value = str(value or "")
                        if tf.page:
                            tf.update()
                        return
                    on_commit(n)
                    if not live:
                        try:
                            tf.value = format_grouped(n)
                            if tf.page:
                                tf.update()
                        except Exception:
                            pass
                    return
                on_commit(val)

            def _commit_live(e):
                live_commit_state["value"] = True
                try:
                    _commit(e, live=True)
                finally:
                    live_commit_state["value"] = False

            if live_commit:
                tf.on_change = _commit_live
            tf.on_submit = lambda e: _commit(e, live=False)
            tf.on_blur = lambda e: _commit(e, live=False)
            return _wrap_editor(width, tf, bgcolor=bgcolor)

        def _wrap_editor(width: int, ctrl: ft.Control, bgcolor: str = "#FFFFFF"):
            return ft.Container(
                width=width,
                height=row_h,
                alignment=ft.Alignment(0, 0),
                border=ft.border.all(1, "#CBD5E1"),
                border_radius=0,
                bgcolor=bgcolor,
                padding=ft.padding.all(0),
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
                content=ctrl,
            )

        def _inline_dropdown(
            value: str,
            width: int,
            options: list,
            on_change,
            bgcolor: str = "#FFFFFF",
            text_color: str = _TEXT,
            quick_view: bool = False,
        ):
            current_value = str(value or "")
            selected_label = ""
            for opt in options or []:
                key = str(getattr(opt, "key", "") or "")
                if key == current_value:
                    selected_label = str(getattr(opt, "text", "") or key)
                    break
            full_label = selected_label
            display_width = max(int(width or 80), 40)
            # When a quick-view button is shown alongside the cell, the text
            # gets less room to wrap into so it doesn't run under the button.
            label_wrap_width = max(display_width - 20, 24) if quick_view else display_width
            selected_label = _wrap_text_for_width(selected_label, label_wrap_width)

            label_text = ft.Text(
                selected_label,
                size=9,
                color=text_color,
                text_align=ft.TextAlign.CENTER,
                no_wrap=False,
                max_lines=3,
                overflow=ft.TextOverflow.VISIBLE,
            )

            def _open_choice_dialog(_):
                page = self._page()
                if not page:
                    return
                _opts = list(options or [])
                if not _opts:
                    return

                dlg_ref = [None]

                def _pick(k, lbl):
                    on_change(k)
                    label_text.value = _wrap_text_for_width(lbl, label_wrap_width)
                    btn.tooltip = ft.Tooltip(message=lbl, wait_duration=0)
                    with contextlib.suppress(Exception):
                        label_text.update()
                    with contextlib.suppress(Exception):
                        btn.update()
                    if dlg_ref[0] is not None:
                        self._close_dlg(dlg_ref[0])

                rows = []
                for opt in _opts:
                    k = str(getattr(opt, "key", "") or "")
                    lbl = str(getattr(opt, "text", "") or k)
                    is_sel = k == current_value
                    _item_bg = "#EFF6FF" if is_sel else None

                    def _make_hover(default_bg):
                        def _on_hover(e):
                            if e.data == "true":
                                e.control.bgcolor = (
                                    "#DBEAFE" if default_bg == "#EFF6FF" else "#F1F5F9"
                                )
                            else:
                                e.control.bgcolor = default_bg
                            with contextlib.suppress(Exception):
                                e.control.update()

                        return _on_hover

                    _row_text_cell = ft.Container(
                        content=ft.Text(
                            lbl,
                            size=12,
                            color=_TEXT if not is_sel else _CHIP_T,
                            weight="w600" if is_sel else "w400",
                            no_wrap=False,
                            max_lines=3,
                        ),
                        padding=ft.padding.symmetric(horizontal=12, vertical=8),
                        border_radius=6,
                        bgcolor=_item_bg,
                        on_click=(lambda _e, _k=k, _l=lbl: _pick(_k, _l)),
                        on_hover=_make_hover(_item_bg),
                        expand=True,
                    )
                    if quick_view and k:
                        from views.product_quick_view import product_icon_btn as _row_qv_btn
                        rows.append(
                            ft.Row(
                                [
                                    _row_text_cell,
                                    _row_qv_btn(
                                        k, self.content_area, self.db, self.user_data, size=14
                                    ),
                                ],
                                spacing=0,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            )
                        )
                    else:
                        rows.append(_row_text_cell)

                dlg = ft.AlertDialog(
                    modal=True,
                    title=ft.Text("Вибір", size=14, weight="bold"),
                    content=ft.Container(
                        width=320,
                        height=min(400, len(rows) * 44 + 16),
                        content=ft.Column(
                            rows,
                            spacing=2,
                            scroll=ft.ScrollMode.ADAPTIVE,
                        ),
                    ),
                    actions=[
                        ft.TextButton(
                            self.t("common.cancel", "Скасувати"), on_click=lambda _: self._close_dlg(dlg_ref[0])
                        ),
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
                dlg_ref[0] = dlg
                page.show_dialog(dlg)

            btn = ft.Container(
                width=(display_width - 20) if quick_view else display_width,
                height=row_h,
                bgcolor=bgcolor,
                alignment=ft.Alignment(0, 0),
                padding=ft.padding.all(0),
                on_click=_open_choice_dialog,
                content=label_text,
                tooltip=ft.Tooltip(message=full_label, wait_duration=0) if full_label else None,
            )
            if quick_view and current_value:
                from views.product_quick_view import product_icon_btn as _cell_qv_btn
                cell_content = ft.Row(
                    [
                        btn,
                        _cell_qv_btn(
                            current_value, self.content_area, self.db, self.user_data, size=13
                        ),
                    ],
                    spacing=0,
                    tight=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
                return _wrap_editor(width, cell_content, bgcolor=bgcolor)
            return _wrap_editor(width, btn, bgcolor=bgcolor)

        def _inline_date_field(value: str, width: int, on_commit):
            value_txt = (value or "")[:10]
            date_text = ft.Text(
                value_txt, size=10, text_align=ft.TextAlign.CENTER, color=_TEXT
            )

            def _apply_date_value(
                raw_value, *, defer_commit: bool = False, page_ref=None
            ) -> None:
                out = str(raw_value or "")[:10]
                date_text.value = out
                with contextlib.suppress(Exception):
                    date_text.update()

                def _do_commit() -> None:
                    on_commit(out)

                if (
                    defer_commit
                    and page_ref is not None
                    and hasattr(page_ref, "run_task")
                ):

                    async def _commit_later():
                        await asyncio.sleep(0)
                        _do_commit()

                    try:
                        page_ref.run_task(_commit_later)
                        return
                    except Exception:
                        pass

                _do_commit()

            def _open_picker(_):
                if bool(getattr(self, "_page_loading", False)):
                    return
                now_ts = time.time()
                if bool(getattr(self, "_shared_date_picker_open", False)):
                    return
                if (
                    now_ts
                    - float(
                        getattr(self, "_shared_date_picker_last_open_ts", 0.0) or 0.0
                    )
                    < 0.5
                ):
                    return
                page = self._page()
                if not page:
                    return
                self._shared_date_picker_open = True
                self._shared_date_picker_last_open_ts = now_ts
                picked_once = {"value": False}
                picker_value = None
                try:
                    current_txt = str(date_text.value or "")[:10]
                    if current_txt:
                        picker_value = datetime.fromisoformat(current_txt).date()
                except Exception:
                    picker_value = None

                picker = getattr(self, "_shared_date_picker", None)
                if picker is None:
                    picker = ft.DatePicker(
                        first_date=date(2018, 1, 1),
                        last_date=date(2100, 12, 31),
                        entry_mode=ft.DatePickerEntryMode.CALENDAR_ONLY,
                        date_picker_mode=ft.DatePickerMode.DAY,
                        locale=ft.Locale("uk", "UA"),
                        cancel_text="Очистити",
                        error_format_text="",
                        error_invalid_text="",
                    )
                    page.show_dialog(picker)

                def _picked(e):
                    picked = getattr(e.control, "value", None)
                    picked_once["value"] = True
                    out = ""
                    if picked is None:
                        out = ""
                    else:
                        try:
                            out = _datepicker_value_to_iso_date(picked)
                        except Exception:
                            out = str(picked)[:10]
                    try:
                        picker.open = False
                        self._shared_date_picker_open = False
                        picker.update()
                    except Exception:
                        self._shared_date_picker_open = False
                        pass
                    _apply_date_value(out, defer_commit=True, page_ref=page)

                def _dismissed(_e):
                    self._shared_date_picker_open = False
                    if not picked_once["value"]:
                        _apply_date_value("", defer_commit=True, page_ref=page)

                sdp_state = getattr(self, "_shared_date_picker_state", None)
                if sdp_state is not None:
                    sdp_state["on_change"] = _picked
                    sdp_state["on_dismiss"] = _dismissed
                else:
                    picker.on_change = _picked
                    picker.on_dismiss = _dismissed

                picker.value = picker_value
                picker.open = True
                try:
                    picker.update()
                except Exception:
                    self._shared_date_picker_open = False
                    pass

            return ft.Container(
                width=width,
                height=row_h,
                border=ft.border.all(1, "#CBD5E1"),
                border_radius=0,
                bgcolor=row_bg,
                padding=ft.padding.all(0),
                alignment=ft.Alignment(0, 0),
                on_click=_open_picker,
                content=date_text,
            )

        inv_cell = _cell(
            "inv",
            f"#{invoice}",
            size=9,
            bold=True,
            bgcolor=_ALT,
        )
        date_cell = _cell("date", order_date or "", size=8, color=_TEXT)
        order1c_cell = _cell(
            "order1c",
            order_1c,
            size=9,
            bold=True,
            color=_TEXT,
            bgcolor=_ALT,
        )
        product_cell = _cell("product", product_text, size=8)
        if product_text and product_text not in {"-", ""}:
            product_cell.tooltip = ft.Tooltip(message=product_text, wait_duration=0)
        if _main_pid:
            from views.product_quick_view import product_icon_btn
            _prod_qv_btn = product_icon_btn(
                _main_pid, self.content_area, self.db, self.user_data, size=10
            )
            product_cell.content = ft.Row(
                [product_cell.content, _prod_qv_btn],
                spacing=2,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            product_cell.alignment = ft.Alignment(-1, 0)
            product_cell.padding = ft.padding.only(left=4, right=2)
        delivery_cell = _cell(
            "delivery", delivery_name, size=8, color=delivery_fg, bgcolor=delivery_bg
        )
        comment_cell = _cell("comment", comment, size=8, color=_TEXT)
        status_cell = _cell(
            "status",
            self.STATUS_LABELS.get(status, status),
            size=8,
            color=status_fg,
            bgcolor=status_bg,
        )
        payment_cell = _cell(
            "payment",
            self.PAYMENT_LABELS.get(payment_st, payment_st),
            size=8,
            color=payment_fg,
            bgcolor=payment_bg,
        )
        submission_cell = _cell(
            "submission",
            self.SUBMISSION_LABELS.get(submission_st, submission_st),
            size=8,
            color=submission_fg,
            bgcolor=submission_bg,
        )
        date_prod_end_cell = (
            _cell("date_prod_end", date_prod_end, size=8)
            if "date_prod_end" in by_key
            else ft.Container()
        )
        dur_prod_cell = (
            _cell(
                "dur_prod",
                f"{dur_prod} дн." if dur_prod is not None else "",
                size=8,
                bgcolor=dur_prod_bg,
            )
            if "dur_prod" in by_key
            else ft.Container()
        )
        date_wh_cell = (
            _cell("date_wh", date_wh, size=8) if "date_wh" in by_key else ft.Container()
        )
        dur_deliv_cell = (
            _cell(
                "dur_deliv", f"{dur_deliv} дн." if dur_deliv is not None else "", size=8
            )
            if "dur_deliv" in by_key
            else ft.Container()
        )
        date_payment_cell = (
            _cell(
                "date_payment",
                date_payment,
                size=8,
                bgcolor=_SOFT_I,
                no_wrap=True,
                max_lines=1,
            )
            if "date_payment" in by_key
            else ft.Container()
        )
        date_receive_cell = (
            _cell(
                "date_receive",
                date_receive,
                size=8,
                bgcolor=date_receive_bg,
                no_wrap=True,
                max_lines=1,
            )
            if "date_receive" in by_key
            else ft.Container()
        )
        date_1c_cell = (
            _cell("date_1c", date_1c, size=8) if "date_1c" in by_key else ft.Container()
        )
        dur_acc_cell = (
            _cell("dur_acc", deviation_text, size=8, bgcolor=deviation_bg)
            if "dur_acc" in by_key
            else ft.Container()
        )
        term_acc_cell = (
            _cell(
                "term_acc",
                f"{term_acc} дн." if term_acc is not None else "",
                size=8,
                bgcolor=row_bg,
            )
            if "term_acc" in by_key
            else ft.Container()
        )
        w_prev_cell = (
            _cell("w_prev", prev_weight_text, size=8)
            if "w_prev" in by_key
            else ft.Container()
        )
        w_fact_cell = (
            _cell("w_fact", fact_weight_text, size=8)
            if "w_fact" in by_key
            else ft.Container()
        )
        tariff_cell = (
            _cell("tariff", tariff_text, size=8)
            if "tariff" in by_key
            else ft.Container()
        )
        unit_cost_cell = (
            _cell("unit_cost", unit_cost_text, size=8)
            if "unit_cost" in by_key
            else ft.Container()
        )

        log_cell = (
            _cell("log", log_text, size=8, color=_TEXT)
            if "log" in by_key
            else ft.Container()
        )

        if order_id:
            _fr_get_page = self._page
            _fr_dc = dur_prod_cell
            _fr_dd = dur_deliv_cell
            _fr_da = dur_acc_cell
            _fr_dr = date_receive_cell
            _fr_ta = term_acc_cell
            _fr_lc = log_cell
            _fr_rb = row_bg
            _fr_products_by_id = products_by_id
            _fr_delivery_by_id = delivery_by_id

            def _make_fast_refresh(
                _get_page=_fr_get_page,
                _dc=_fr_dc,
                _dd=_fr_dd,
                _da=_fr_da,
                _dr=_fr_dr,
                _ta=_fr_ta,
                _lc=_fr_lc,
                _rb=_fr_rb,
                _products_by_id=_fr_products_by_id,
                _delivery_by_id=_fr_delivery_by_id,
            ):
                def _to_date(s: str):
                    try:
                        return date.fromisoformat(s) if len(str(s)) == 10 else None
                    except Exception:
                        return None

                def _fast_date_refresh(updated: dict) -> bool:
                    try:
                        od = (updated.get("order_date") or "")[:10]
                        d1 = (updated.get("date_shipped") or "")[:10]
                        d2 = (updated.get("date_arrived_warehouse") or "")[:10]
                        d3 = (updated.get("date_accepted") or "")[:10]
                        dp = _days_between(od, d1)
                        dd_v = _days_between(d1, d2)
                        ta_v = _days_between(d2, d3)
                        log_d = None
                        if od and d3:
                            log_d = _days_between(od, d3)
                        elif (
                            od
                            and (updated.get("status") or "production") != "production"
                        ):
                            log_d = _days_between(od, _today_str())
                        log_t = f"{log_d} дн." if log_d is not None else "—"
                        dr_val = (
                            self._order_receive_date_iso(
                                updated,
                                products_by_id=_products_by_id,
                                delivery_by_id=_delivery_by_id,
                            )
                            if od
                            else ""
                        )
                        dr_dt = _to_date(dr_val)
                        d3_dt = _to_date(d3)
                        ref_dt = d3_dt if d3_dt is not None else date.today()
                        # Mirror the full-row-build rule (_build_order_row,
                        # ~line 7701) exactly — this fast in-place patch used
                        # to collapse it to just blue/red, so a receive date
                        # more than a week out stayed blue instead of turning
                        # green/yellow until the next full row rebuild.
                        if dr_dt is None:
                            dr_bg = "#DBEAFE"  # default blue — no receive date yet
                        elif d3_dt is not None:
                            if dr_dt < d3_dt:
                                dr_bg = "#FEE2E2"  # red — delivered late
                            elif dr_dt > d3_dt:
                                dr_bg = "#DCFCE7"  # green — delivered early
                            else:
                                dr_bg = "#DBEAFE"  # blue — on time
                        else:
                            days_until = (dr_dt - date.today()).days
                            if days_until > 7:
                                dr_bg = "#DCFCE7"  # green — >7 days ahead
                            elif days_until >= 1:
                                dr_bg = "#FEF9C3"  # yellow — 1-7 days ahead
                            else:
                                dr_bg = "#FEE2E2"  # red — today or overdue
                        dev_text = ""
                        dev_bg = _rb
                        if dr_dt is not None:
                            dev_days = int((ref_dt - dr_dt).days)
                            dev_text = f"{dev_days} дн."
                            dev_bg = "#DBEAFE" if dev_days <= 0 else "#FEE2E2"
                        updates = [
                            (_dc, f"{dp} дн." if dp is not None else "", None),
                            (_dd, f"{dd_v} дн." if dd_v is not None else "", None),
                            (_da, dev_text, dev_bg),
                            (_dr, dr_val, dr_bg),
                            (_ta, f"{ta_v} дн." if ta_v is not None else "", None),
                            (_lc, log_t, None),
                        ]
                        page = _get_page()
                        for cell, txt, bg in updates:
                            try:
                                # _table_cell now returns Container(content=Text) directly
                                text_ctrl = getattr(cell, "content", None)
                                if text_ctrl is not None:
                                    text_ctrl.value = str(txt) if txt else "-"
                                    if bg is not None:
                                        cell.bgcolor = bg
                                    if page:
                                        with contextlib.suppress(Exception):
                                            cell.update()
                            except Exception:
                                pass
                        return True
                    except Exception:
                        return False

                return _fast_date_refresh

            self._order_fast_refresh[order_id] = _make_fast_refresh()

        if can_inline_edit:

            def _commit_invoice(v, _d=draft_order, _orig=invoice):
                new_inv = str(v or "").strip()
                if new_inv == str(_orig or "").strip():
                    return
                if new_inv:
                    _ord_rid = str(
                        _d.get("region_id") or self._effective_region_id() or ""
                    ).strip()
                    _ex_id = str(_d.get("id") or "").strip()
                    if self._is_invoice_duplicate(
                        new_inv, _ord_rid, exclude_order_id=_ex_id
                    ):
                        _suggs = self._invoice_suggestions(new_inv, _ord_rid)
                        _sugg_str = ", ".join(_suggs) if _suggs else ""
                        self._show_snack(
                            (
                                f"Інвойс {new_inv!r} вже існує. Варіанти: {_sugg_str}"
                                if _sugg_str
                                else f"Інвойс {new_inv!r} вже існує в цьому регіоні"
                            ),
                            "#DC2626",
                        )
                        return
                _d.update({"invoice_no": new_inv})
                _mark_dirty_and_update()

            inv_cell = _inline_text_field(
                value=invoice,
                width=by_key["inv"].get("width"),
                bgcolor=_ALT,
                text_color=_TEXT,
                bold=True,
                text_size=9,
                on_commit=_commit_invoice,
            )
            order1c_cell = _inline_text_field(
                value=order_1c,
                width=by_key["order1c"].get("width"),
                bgcolor=_ALT,
                text_color=_TEXT,
                bold=True,
                text_size=9,
                on_commit=lambda v: (
                    draft_order.update({"order_no_1c": str(v or "")[:20]}),
                    _mark_dirty_and_update(),
                ),
            )

            def _date_commit_and_move(v):
                _commit_date_fast("order_date", str(v or "")[:10])

            date_cell = _inline_date_field(
                value=order_date,
                width=by_key["date"].get("width"),
                on_commit=_date_commit_and_move,
            )

            if len(draft_items) == 1:
                cur_supplier = str(draft_order.get("supplier_id") or "").strip()
                source_products = list(self._products or [])
                source_label_map = self._product_option_label_map(source_products)
                prod_options = [
                    ft.dropdown.Option(
                        str(p.get("id") or ""),
                        str(
                            source_label_map.get(
                                str(p.get("id") or ""), p.get("name") or ""
                            )
                        ),
                    )
                    for p in source_products
                    if isinstance(p, dict)
                    and (p.get("id") or "")
                    and (
                        not cur_supplier
                        or str((p.get("supplier_id") or "")).strip() == cur_supplier
                    )
                ]
                if cur_supplier and not prod_options:
                    prod_options = []
                current_pid = str(
                    (draft_items[0] or {}).get("product_id") or ""
                ).strip()
                current_pname = str(
                    (draft_items[0] or {}).get("product_name") or ""
                ).strip()

                if current_pid and not any(
                    str(getattr(opt, "key", "")) == current_pid for opt in prod_options
                ):
                    prod_options.insert(
                        0, ft.dropdown.Option(current_pid, product_text)
                    )
                elif not current_pid and current_pname:
                    prod_options.insert(0, ft.dropdown.Option("", current_pname))

                def _on_product_change(pid_raw):
                    pid = str(pid_raw or "").strip()
                    prod = next(
                        (
                            p
                            for p in (self._products or [])
                            if str((p or {}).get("id") or "") == pid
                        ),
                        None,
                    )
                    if draft_items:
                        draft_items[0]["product_id"] = pid
                        draft_items[0]["product_name"] = (prod or {}).get("name") or (
                            draft_items[0].get("product_name") or ""
                        )
                        pp = _safe_float((prod or {}).get("purchase_price"), None)
                        if pp is not None:
                            draft_items[0]["purchase_price"] = pp
                        pccy = (
                            str((prod or {}).get("purchase_currency") or "")
                            .upper()
                            .strip()
                        )
                        if pccy:
                            draft_items[0]["purchase_currency"] = pccy
                    draft_order.pop("amount_manual_override", None)
                    _optimistic_commit_draft()

                product_cell = _inline_dropdown(
                    value=current_pid,
                    width=by_key["product"].get("width") or 120,
                    options=prod_options,
                    on_change=_on_product_change,
                    quick_view=True,
                )
            status_cell = _inline_dropdown(
                value=status,
                width=by_key["status"].get("width"),
                options=[
                    ft.dropdown.Option(s, self.STATUS_LABELS[s]) for s in self.STATUSES
                ],
                on_change=lambda v: (
                    draft_order.update({"status": str((v or "production")).strip()}),
                    _optimistic_commit_draft(),
                ),
                bgcolor=status_bg,
                text_color=status_fg,
            )

            # Narrow to this order's supplier's enabled delivery methods
            # (set in Постачальники) when it has restricted any — unset on
            # the supplier means unrestricted, same as before this existed.
            _dm_supplier_id = str(draft_order.get("supplier_id") or "").strip()
            _dm_supplier_row = (
                (self._suppliers_by_id or {}).get(_dm_supplier_id)
                if _dm_supplier_id
                else None
            )
            _dm_enabled_ids = (
                _dm_supplier_row.get("enabled_delivery_method_ids")
                if isinstance(_dm_supplier_row, dict)
                else None
            )
            _dm_restricted = (
                isinstance(_dm_enabled_ids, list) and len(_dm_enabled_ids) > 0
            )
            _dm_methods = (
                [
                    m
                    for m in (self._delivery_methods or [])
                    if isinstance(m, dict) and m.get("id") in _dm_enabled_ids
                ]
                if _dm_restricted
                else (self._delivery_methods or [])
            )
            delivery_options = [
                ft.dropdown.Option(str(m.get("id") or ""), str(m.get("name") or ""))
                for m in _dm_methods
                if isinstance(m, dict) and (m.get("id") or "")
            ]
            current_delivery_id = str(
                (draft_order.get("delivery_type_id") or "")
            ).strip()
            if current_delivery_id and not any(
                str(getattr(opt, "key", "")) == current_delivery_id
                for opt in delivery_options
            ):
                delivery_options.insert(
                    0, ft.dropdown.Option(current_delivery_id, delivery_name)
                )

            def _on_delivery_change(selected_raw):
                selected_id = str(selected_raw or "").strip()
                selected = next(
                    (
                        m
                        for m in (self._delivery_methods or [])
                        if str((m or {}).get("id") or "") == selected_id
                    ),
                    None,
                )
                sel_price, sel_ccy = self._delivery_method_tariff(selected)
                draft_order.update(
                    {
                        "delivery_type_id": selected_id,
                        "delivery_type_name": (selected or {}).get("name")
                        or delivery_name,
                        "delivery_price_snapshot": sel_price,
                        "delivery_price_currency_snapshot": sel_ccy,
                    }
                )
                _optimistic_commit_draft()

            delivery_cell = _inline_dropdown(
                value=current_delivery_id,
                width=by_key["delivery"].get("width"),
                options=delivery_options,
                on_change=_on_delivery_change,
                bgcolor=delivery_bg,
                text_color=delivery_fg,
            )

            comment_cell = _inline_text_field(
                value=comment,
                width=by_key["comment"].get("width"),
                on_commit=lambda v: (
                    draft_order.update({"comment": str(v or "")[:400]}),
                    _mark_dirty_and_update(),
                ),
                multiline=True,
            )

            summary_amount_edit_cell = _inline_text_field(
                value=amount_display_text,
                width=by_key["amount"].get("width"),
                on_commit=lambda v: (
                    (
                        draft_order.pop("amount_manual_override", None)
                        if v is None
                        else draft_order.update({"amount_manual_override": v})
                    ),
                    _mark_dirty_and_update(),
                ),
                numeric=True,
                allow_empty_numeric=True,
            )

            payment_cell = _inline_dropdown(
                value=payment_st,
                width=by_key["payment"].get("width"),
                options=[
                    ft.dropdown.Option(k, v) for k, v in self.PAYMENT_LABELS.items()
                ],
                on_change=lambda v: (
                    draft_order.update(
                        {"payment_status": str((v or "unpaid")).strip()}
                    ),
                    _optimistic_commit_draft(),
                ),
                bgcolor=payment_bg,
                text_color=payment_fg,
            )

            submission_cell = _inline_dropdown(
                value=submission_st,
                width=by_key["submission"].get("width"),
                options=[
                    ft.dropdown.Option(k, v) for k, v in self.SUBMISSION_LABELS.items()
                ],
                on_change=lambda v: (
                    draft_order.update(
                        {"submission_status": str((v or "not_submitted")).strip()}
                    ),
                    _optimistic_commit_draft(),
                ),
                bgcolor=submission_bg,
                text_color=submission_fg,
            )

            if "date_prod_end" in by_key:
                date_prod_end_cell = _inline_date_field(
                    value=date_prod_end,
                    width=by_key["date_prod_end"].get("width"),
                    on_commit=lambda v: _commit_date_fast(
                        "date_shipped", str(v or "")[:10]
                    ),
                )
            if "date_wh" in by_key:
                date_wh_cell = _inline_date_field(
                    value=date_wh,
                    width=by_key["date_wh"].get("width"),
                    on_commit=lambda v: _commit_date_fast(
                        "date_arrived_warehouse", str(v or "")[:10]
                    ),
                )
            if "date_1c" in by_key:
                date_1c_cell = _inline_date_field(
                    value=date_1c,
                    width=by_key["date_1c"].get("width"),
                    on_commit=lambda v: _commit_date_fast(
                        "date_accepted", str(v or "")[:10]
                    ),
                )
            if "w_fact" in by_key:
                w_fact_cell = _inline_text_field(
                    value=fact_weight_text,
                    width=by_key["w_fact"].get("width"),
                    on_commit=lambda v: (
                        draft_order.update({"__fact_weight_total": v}),
                        [it.update({"weight_actual_kg": None}) for it in draft_items],
                        _mark_dirty_and_update(),
                    ),
                    numeric=True,
                    allow_empty_numeric=True,
                )
            if "tariff" in by_key:
                tariff_cell = _inline_text_field(
                    value=str(tariff or ""),
                    width=by_key["tariff"].get("width"),
                    on_commit=lambda v: (
                        draft_order.update({"delivery_price_snapshot": v}),
                        _mark_dirty_and_update(),
                    ),
                    numeric=True,
                )
        else:
            summary_amount_edit_cell = ft.Container(
                width=by_key["amount"].get("width"),
                height=row_h,
                alignment=ft.Alignment(0, 0),
                border=ft.border.all(1, "#CBD5E1"),
                border_radius=0,
                bgcolor=row_bg,
                content=summary_amount_text,
            )

        summary_qty_cell: ft.Control
        if can_inline_edit and len(draft_items) == 1:
            _orig_qty_text = str(qty_text or "").strip()

            def _save_summary_qty(v):
                if v is None:
                    try:
                        summary_qty_text.value = _orig_qty_text
                        summary_qty_text.update()
                    except Exception:
                        pass
                    return
                if draft_items:
                    draft_items[0]["qty"] = v
                    draft_items[0]["quantity"] = v
                    draft_order.pop("amount_manual_override", None)
                    draft_items[0].pop("amount_manual_override", None)
                    _mark_dirty_and_update()

            summary_qty_cell = _inline_text_field(
                value=_orig_qty_text,
                width=by_key["qty"].get("width"),
                on_commit=_save_summary_qty,
                numeric=True,
                allow_empty_numeric=True,
                live_commit=False,  # commit on blur/submit only (prevents partial sums like 600+600=1000)
                clear_on_focus=True,  # click clears input for fast overwrite
            )
        else:
            summary_qty_cell = ft.Container(
                width=by_key["qty"].get("width"),
                height=row_h,
                alignment=ft.Alignment(0, 0),
                border=ft.border.all(1, "#CBD5E1"),
                border_radius=0,
                bgcolor=row_bg,
                content=summary_qty_text,
            )

        expand_col_w = int((by_key.get("expand") or {}).get("width") or 22)
        delete_col_w = int((by_key.get("delete") or {}).get("width") or 28)

        # Color of the +/- cell indicates invoice/packing attachment fill status.
        _row_atts = self._order_attachments_cache.get(order_id, []) if order_id else []
        _has_inv = any(str((a or {}).get("kind") or "") == "invoice" for a in _row_atts)
        _has_pack = any(str((a or {}).get("kind") or "") == "packing" for a in _row_atts)
        if _has_inv and _has_pack:
            _expand_bg = "#D1FAE5"   # green — both tables have data
        elif _has_inv or _has_pack:
            _expand_bg = "#FEF9C3"   # yellow — one table has data
        else:
            _expand_bg = "#FEE2E2"   # red — both empty

        expand_btn = ft.IconButton(
            icon=(
                ft.Icons.REMOVE
                if bool(self._invoice_panel_open_by_order.get(order_id, False))
                else ft.Icons.ADD
            ),
            icon_size=14,
            tooltip=self.t("orders.tooltip.invoice_packing", "Інвойс/пакінг"),
            style=ft.ButtonStyle(
                padding=ft.padding.all(0),
                shape=ft.RoundedRectangleBorder(radius=4),
            ),
            width=expand_col_w,
            height=row_h,
        )
        summary_row = ft.Container(
            bgcolor=row_bg,
            border=ft.border.all(1, "#CBD5E1"),
            border_radius=0,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            height=row_h,
            content=ft.Row(
                [
                    lines_expand_cell,
                    (_expand_cell_ctr := ft.Container(
                        width=expand_col_w,
                        height=row_h,
                        alignment=ft.Alignment(0, 0),
                        border=ft.border.all(1, "#CBD5E1"),
                        border_radius=0,
                        bgcolor=_expand_bg,
                        content=expand_btn,
                    )),
                    ft.Container(
                        width=delete_col_w,
                        height=row_h,
                        alignment=ft.Alignment(0, 0),
                        border=ft.border.all(1, "#CBD5E1"),
                        border_radius=0,
                        bgcolor=row_bg,
                        content=ft.IconButton(
                            ft.Icons.DELETE_OUTLINE,
                            icon_size=14,
                            tooltip=self.t("common.delete", "Видалити"),
                            disabled=bool(self.is_read_only),
                            on_click=(
                                lambda _e, oid=order.get(
                                    "id"
                                ): self._confirm_delete_order(oid)
                            ),
                            style=ft.ButtonStyle(
                                padding=ft.padding.all(0),
                                shape=ft.RoundedRectangleBorder(radius=4),
                            ),
                            width=delete_col_w,
                            height=row_h,
                        ),
                    ),
                    date_cell,
                    log_cell,
                    date_payment_cell,
                    date_receive_cell,
                    dur_acc_cell,
                    inv_cell,
                    order1c_cell,
                    product_cell,
                    status_cell,
                    delivery_cell,
                    comment_cell,
                    summary_qty_cell,
                    summary_amount_edit_cell,
                    (
                        ft.Container(
                            width=by_key["amount_usd"].get("width"),
                            height=row_h,
                            alignment=ft.Alignment(0, 0),
                            border=ft.border.all(1, "#CBD5E1"),
                            border_radius=0,
                            bgcolor=row_bg,
                            content=summary_amount_usd_text,
                        )
                        if "amount_usd" in by_key
                        else ft.Container()
                    ),
                    payment_cell,
                    submission_cell,
                    date_prod_end_cell,
                    dur_prod_cell,
                    date_wh_cell,
                    dur_deliv_cell,
                    date_1c_cell,
                    term_acc_cell,
                    w_prev_cell,
                    w_fact_cell,
                    tariff_cell,
                    unit_cost_cell,
                    edit_action_cell,
                ],
                spacing=0,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        sub_rows = []
        _inv_plain = str(invoice or "").strip().lstrip("#")
        _zp_plain = str(order_1c or "").strip()
        if len(items) > 1 and order_id:

            def _remove_draft_line_at(i_remove: int):
                if len(draft_items) <= 1:
                    self._show_snack(
                        "У замовленні має залишитись хоча б один товар", "#92400E"
                    )
                    return
                if not (0 <= i_remove < len(draft_items)):
                    return
                draft_items.pop(i_remove)
                draft_order.pop("amount_manual_override", None)
                oid = str(order_id or "").strip()
                if oid and len(draft_items) == 1:
                    self._expanded_multi_item_order_ids.discard(oid)
                _mark_dirty_and_update()

            for idx, it in enumerate(draft_items):
                q = _item_qty_value(it)
                p, ccy = _effective_item_pricing(it)
                item_qty = (
                    str(int(q))
                    if abs(q - int(q)) < 0.0001
                    else f"{q:.2f}".rstrip("0").rstrip(".")
                )
                line_amt_ov = _safe_float(
                    (it or {}).get("amount_manual_override"), None
                )
                _p_eff_i, _ccy_eff_i = _effective_item_pricing(it)
                _ccy_eff_i = _ccy_eff_i or ""
                if line_amt_ov is not None:
                    item_amount = format_grouped(float(line_amt_ov), decimals=2)
                    if _ccy_eff_i:
                        item_amount = f"{item_amount} {_ccy_symbol(_ccy_eff_i)}"
                elif p is not None:
                    item_amount = format_grouped(q * p, decimals=2)
                    if _ccy_eff_i:
                        item_amount = f"{item_amount} {_ccy_symbol(_ccy_eff_i)}"
                else:
                    item_amount = ""

                item_unit_cost_text = ""
                if (
                    idx < len(item_unit_cost_usd)
                    and item_unit_cost_usd[idx] is not None
                ):
                    item_unit_cost_text = format_grouped(
                        item_unit_cost_usd[idx], decimals=2
                    )
                    _uc_ccy_i = (
                        item_unit_cost_ccy[idx] if idx < len(item_unit_cost_ccy) else "USD"
                    )
                    if _uc_ccy_i and _uc_ccy_i != "USD":
                        item_unit_cost_text = f"{item_unit_cost_text} {_uc_ccy_i}"
                else:
                    _calc2 = _safe_float((it or {}).get("unit_cost_calc_usd"), None)
                    v2 = _calc2
                    if v2 is not None:
                        item_unit_cost_text = format_grouped(float(v2), decimals=2)
                        _uc_ccy_i2 = (
                            str((it or {}).get("unit_cost_calc_currency") or "USD")
                            .upper()
                            .strip()
                            or "USD"
                        )
                        if _uc_ccy_i2 != "USD":
                            item_unit_cost_text = f"{item_unit_cost_text} {_uc_ccy_i2}"

                item_amount_usd_text = ""
                if (
                    idx < len(item_amount_usd_vals)
                    and item_amount_usd_vals[idx] is not None
                ):
                    item_amount_usd_text = _amount_with_ccy(
                        format_grouped(item_amount_usd_vals[idx], decimals=2), "USD"
                    )

                item_prev_weight_text = ""
                if idx < len(prev_weights):
                    item_prev_weight_text = (
                        f"{round(prev_weights[idx])}" if prev_weights[idx] else ""
                    )

                item_fact_weight_text = ""
                if has_fact and idx < len(resolved_weights):
                    item_fact_weight_text = f"{round(resolved_weights[idx])}"

                item_tariff_text = tariff_text

                inv_item_text = (
                    f"{_inv_plain}/{idx + 1:02d}" if _inv_plain else f"{idx + 1:02d}"
                )
                zp_item_text = f"{_zp_plain}/{idx + 1:02d}" if _zp_plain else ""

                _SUB_BG = _ALT
                qty_cell = _cell("qty", item_qty, size=8, color=_TEXT, bgcolor=_SUB_BG)
                amount_cell = _cell(
                    "amount", item_amount, size=8, color=_TEXT, bgcolor=_SUB_BG
                )
                amount_usd_cell = (
                    _cell(
                        "amount_usd",
                        item_amount_usd_text,
                        size=8,
                        color=_TEXT,
                        bgcolor=_SUB_BG,
                    )
                    if "amount_usd" in by_key
                    else ft.Container()
                )
                w_prev_item_cell = (
                    _cell(
                        "w_prev",
                        item_prev_weight_text,
                        size=8,
                        color=_TEXT,
                        bgcolor=_SUB_BG,
                    )
                    if "w_prev" in by_key
                    else ft.Container()
                )
                w_fact_item_cell = (
                    _cell(
                        "w_fact",
                        item_fact_weight_text,
                        size=8,
                        color=_TEXT,
                        bgcolor=_SUB_BG,
                    )
                    if "w_fact" in by_key
                    else ft.Container()
                )
                tariff_item_cell = (
                    _cell(
                        "tariff",
                        item_tariff_text,
                        size=8,
                        color=_TEXT,
                        bgcolor=_SUB_BG,
                    )
                    if "tariff" in by_key
                    else ft.Container()
                )
                _it_pid = str((it or {}).get("product_id") or "").strip()
                _it_fresh_name = (
                    (self._products_by_id_cache or {}).get(_it_pid, {}).get("name")
                )
                _it_disambig_name = (self._product_label_map_cache or {}).get(_it_pid)
                product_item_cell = _cell(
                    "product",
                    (
                        _it_disambig_name
                        or _it_fresh_name
                        or it.get("product_name")
                        or "-"
                    ),
                    size=8,
                    color=_TEXT,
                    bgcolor=_SUB_BG,
                )
                if _it_pid:
                    from views.product_quick_view import product_icon_btn
                    _pit_qv_btn = product_icon_btn(
                        _it_pid, self.content_area, self.db, self.user_data, size=10
                    )
                    product_item_cell.content = ft.Row(
                        [product_item_cell.content, _pit_qv_btn],
                        spacing=2,
                        tight=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                    product_item_cell.alignment = ft.Alignment(-1, 0)
                    product_item_cell.padding = ft.padding.only(left=4, right=2)

                sub_status_cell = _cell(
                    "status",
                    self.STATUS_LABELS.get(status, status),
                    size=10,
                    color=status_fg,
                    bgcolor=status_bg,
                )
                sub_delivery_cell = _cell(
                    "delivery",
                    delivery_name,
                    size=10,
                    color=delivery_fg,
                    bgcolor=delivery_bg,
                )
                sub_payment_cell = _cell(
                    "payment",
                    self.PAYMENT_LABELS.get(payment_st, payment_st),
                    size=10,
                    color=payment_fg,
                    bgcolor=payment_bg,
                )
                sub_submission_cell = _cell(
                    "submission",
                    self.SUBMISSION_LABELS.get(submission_st, submission_st),
                    size=10,
                    color=submission_fg,
                    bgcolor=submission_bg,
                )
                sub_date_prod_end = (
                    _cell(
                        "date_prod_end",
                        _effective_item_date_field(it, "date_shipped"),
                        size=8,
                        bgcolor=_SUB_BG,
                    )
                    if "date_prod_end" in by_key
                    else ft.Container()
                )
                sub_dur_prod = (
                    _cell(
                        "dur_prod",
                        f"{dur_prod} дн." if dur_prod is not None else "",
                        size=8,
                        bgcolor=dur_prod_bg,
                    )
                    if "dur_prod" in by_key
                    else ft.Container()
                )
                sub_date_wh = (
                    _cell(
                        "date_wh",
                        _effective_item_date_field(it, "date_arrived_warehouse"),
                        size=8,
                        bgcolor=_SUB_BG,
                    )
                    if "date_wh" in by_key
                    else ft.Container()
                )
                sub_dur_deliv = (
                    _cell(
                        "dur_deliv",
                        f"{dur_deliv} дн." if dur_deliv is not None else "",
                        size=8,
                        bgcolor=_SUB_BG,
                    )
                    if "dur_deliv" in by_key
                    else ft.Container()
                )
                sub_date_payment = (
                    _cell(
                        "date_payment",
                        date_payment,
                        size=8,
                        bgcolor=_SOFT_I,
                        no_wrap=True,
                        max_lines=1,
                    )
                    if "date_payment" in by_key
                    else ft.Container()
                )
                sub_date_receive = (
                    _cell(
                        "date_receive",
                        date_receive,
                        size=8,
                        bgcolor=date_receive_bg,
                        no_wrap=True,
                        max_lines=1,
                    )
                    if "date_receive" in by_key
                    else ft.Container()
                )
                sub_date_1c = (
                    _cell(
                        "date_1c",
                        _effective_item_date_field(it, "date_accepted"),
                        size=8,
                        bgcolor=_SUB_BG,
                    )
                    if "date_1c" in by_key
                    else ft.Container()
                )
                sub_dur_acc = (
                    _cell("dur_acc", deviation_text, size=8, bgcolor=deviation_bg)
                    if "dur_acc" in by_key
                    else ft.Container()
                )
                sub_term_acc = (
                    _cell(
                        "term_acc",
                        f"{term_acc} дн." if term_acc is not None else "",
                        size=8,
                        bgcolor=_SUB_BG,
                    )
                    if "term_acc" in by_key
                    else ft.Container()
                )

                icom0 = str((it or {}).get("item_comment") or "")
                item_comment_cell = _cell(
                    "comment", icom0, size=8, color=_TEXT, bgcolor=_SUB_BG
                )

                item_product_id = str((it or {}).get("product_id") or "").strip()
                item_can_edit = bool(
                    not self.is_read_only
                    and (
                        is_privileged
                        or item_product_id in editable_product_ids
                        or self._is_order_owned_by_current_manager(order)
                    )
                )

                if item_can_edit:
                    cur_supplier = str(draft_order.get("supplier_id") or "").strip()
                    source_products = list(self._products or [])
                    _prod_label_map = self._product_label_map_cache or {}
                    prod_options = [
                        ft.dropdown.Option(
                            str(p.get("id") or ""),
                            str(
                                _prod_label_map.get(str(p.get("id") or ""))
                                or p.get("name")
                                or ""
                            ),
                        )
                        for p in source_products
                        if isinstance(p, dict)
                        and (p.get("id") or "")
                        and (
                            not cur_supplier
                            or str((p.get("supplier_id") or "")).strip() == cur_supplier
                        )
                    ]
                    if cur_supplier and not prod_options:
                        prod_options = []
                    current_pid = str((it or {}).get("product_id") or "").strip()
                    if current_pid and not any(
                        str(getattr(opt, "key", "")) == current_pid
                        for opt in prod_options
                    ):
                        prod_options.insert(
                            0,
                            ft.dropdown.Option(
                                current_pid,
                                (
                                    _prod_label_map.get(current_pid)
                                    or it.get("product_name")
                                    or ""
                                ),
                            ),
                        )

                    def _save_item_product(pid_raw, i=idx):
                        pid = str(pid_raw or "").strip()
                        prod = next(
                            (
                                p
                                for p in (self._products or [])
                                if str((p or {}).get("id") or "") == pid
                            ),
                            None,
                        )
                        if 0 <= i < len(draft_items):
                            draft_items[i]["product_id"] = pid
                            draft_items[i]["product_name"] = (prod or {}).get(
                                "name"
                            ) or (draft_items[i].get("product_name") or "")
                            draft_items[i].pop("amount_manual_override", None)
                            pp = _safe_float((prod or {}).get("purchase_price"), None)
                            if pp is not None:
                                draft_items[i]["purchase_price"] = pp
                            pccy = (
                                str((prod or {}).get("purchase_currency") or "")
                                .upper()
                                .strip()
                            )
                            if pccy:
                                draft_items[i]["purchase_currency"] = pccy
                        _optimistic_commit_draft()

                    product_item_cell = _inline_dropdown(
                        value=current_pid,
                        width=by_key["product"].get("width") or 120,
                        options=prod_options,
                        on_change=_save_item_product,
                    )

                    # Per-item overrides for Готово/На складі/Прийомка — a
                    # line can finish production/arrive/be accepted on a
                    # different day than the rest of the order. Left alone,
                    # each keeps mirroring the order's own date (see
                    # _effective_item_date_field / _commit_date_fast's
                    # cascade-clear).
                    if "date_prod_end" in by_key:
                        sub_date_prod_end = _inline_date_field(
                            value=_effective_item_date_field(it, "date_shipped"),
                            width=by_key["date_prod_end"].get("width"),
                            on_commit=lambda v, i=idx: _commit_item_date_fast(
                                i, "date_shipped", str(v or "")[:10]
                            ),
                        )
                    if "date_wh" in by_key:
                        sub_date_wh = _inline_date_field(
                            value=_effective_item_date_field(
                                it, "date_arrived_warehouse"
                            ),
                            width=by_key["date_wh"].get("width"),
                            on_commit=lambda v, i=idx: _commit_item_date_fast(
                                i, "date_arrived_warehouse", str(v or "")[:10]
                            ),
                        )
                    if "date_1c" in by_key:
                        sub_date_1c = _inline_date_field(
                            value=_effective_item_date_field(it, "date_accepted"),
                            width=by_key["date_1c"].get("width"),
                            on_commit=lambda v, i=idx: _commit_item_date_fast(
                                i, "date_accepted", str(v or "")[:10]
                            ),
                        )

                    _orig_item_qty = str(item_qty or "").strip()

                    def _save_item_qty(new_qty, i=idx):
                        if new_qty is None:
                            return
                        if 0 <= i < len(draft_items):
                            draft_items[i]["qty"] = new_qty
                            draft_items[i]["quantity"] = new_qty
                            draft_order.pop("amount_manual_override", None)
                            draft_items[i].pop("amount_manual_override", None)
                            _mark_dirty_and_update()

                    qty_cell = _inline_text_field(
                        value=_orig_item_qty,
                        width=by_key["qty"].get("width"),
                        on_commit=_save_item_qty,
                        numeric=True,
                        allow_empty_numeric=True,
                        live_commit=False,
                        clear_on_focus=True,
                    )

                    try:
                        _prev_on_focus = getattr(qty_cell, "on_focus", None)
                    except Exception:
                        _prev_on_focus = None

                    def _multi_qty_focus(_e, _prev=_prev_on_focus):
                        try:
                            summary_qty_text.value = "0"
                            summary_qty_text.update()
                        except Exception:
                            pass
                        try:
                            if callable(_prev):
                                _prev(_e)
                        except Exception:
                            pass

                    with contextlib.suppress(Exception):
                        qty_cell.on_focus = _multi_qty_focus

                    def _save_item_icomment(v_raw, i=idx):
                        if 0 <= i < len(draft_items):
                            draft_items[i]["item_comment"] = str(v_raw or "")[:400]
                            _mark_dirty_and_update()

                    item_comment_cell = _inline_text_field(
                        value=icom0,
                        width=by_key["comment"].get("width"),
                        on_commit=_save_item_icomment,
                        multiline=True,
                    )

                    if "w_fact" in by_key:

                        def _save_item_fact_weight(new_weight, i=idx):
                            if 0 <= i < len(draft_items):
                                draft_items[i]["weight_actual_kg"] = new_weight
                                _mark_dirty_and_update()

                        current_manual_w = _safe_float(
                            (draft_items[idx] if idx < len(draft_items) else {}).get(
                                "weight_actual_kg"
                            ),
                            None,
                        )
                        w_fact_item_cell = _inline_text_field(
                            value=(
                                f"{current_manual_w}"
                                if current_manual_w is not None
                                else item_fact_weight_text
                            ),
                            width=by_key["w_fact"].get("width"),
                            on_commit=_save_item_fact_weight,
                            numeric=True,
                            allow_empty_numeric=True,
                        )

                if can_delete and len(draft_items) > 1 and item_can_edit:
                    sub_delete_cell = ft.Container(
                        width=delete_col_w,
                        height=row_h,
                        alignment=ft.Alignment(0, 0),
                        border=ft.border.all(1, "#CBD5E1"),
                        border_radius=0,
                        bgcolor=_SUB_BG,
                        content=ft.IconButton(
                            ft.Icons.DELETE_OUTLINE,
                            icon_size=14,
                            tooltip="Видалити товар із замовлення",
                            style=ft.ButtonStyle(
                                padding=ft.padding.all(0),
                                shape=ft.RoundedRectangleBorder(radius=4),
                            ),
                            width=delete_col_w,
                            height=row_h,
                            on_click=lambda _e, i=idx: _remove_draft_line_at(i),
                        ),
                    )
                else:
                    sub_delete_cell = ft.Container(
                        width=delete_col_w,
                        height=row_h,
                        border=ft.border.all(1, "#CBD5E1"),
                        border_radius=0,
                        bgcolor=_SUB_BG,
                    )

                sub_rows.append(
                    ft.Container(
                        clip_behavior=ft.ClipBehavior.HARD_EDGE,
                        bgcolor=_ALT,
                        content=ft.Row(
                            [
                                ft.Container(
                                    width=lw_ex,
                                    height=row_h,
                                    border=ft.border.all(1, "#CBD5E1"),
                                    border_radius=0,
                                    bgcolor=_SUB_BG,
                                ),
                                ft.Container(
                                    width=expand_col_w,
                                    height=row_h,
                                    border=ft.border.all(1, "#CBD5E1"),
                                    border_radius=0,
                                    bgcolor=_SUB_BG,
                                ),
                                sub_delete_cell,
                                _cell(
                                    "date",
                                    order_date or "",
                                    size=8,
                                    color=_TEXT,
                                    bgcolor=_SUB_BG,
                                    no_wrap=True,
                                ),
                                _cell(
                                    "log",
                                    log_text,
                                    size=8,
                                    color=_TEXT,
                                    bgcolor=_SUB_BG,
                                ),
                                sub_date_payment,
                                sub_date_receive,
                                sub_dur_acc,
                                _cell(
                                    "inv",
                                    inv_item_text,
                                    size=8,
                                    color=_TEXT,
                                    bgcolor=_SUB_BG,
                                ),
                                _cell(
                                    "order1c",
                                    zp_item_text,
                                    size=8,
                                    color=_TEXT,
                                    bgcolor=_SUB_BG,
                                ),
                                product_item_cell,
                                sub_status_cell,
                                sub_delivery_cell,
                                item_comment_cell,
                                qty_cell,
                                amount_cell,
                                amount_usd_cell,
                                sub_payment_cell,
                                sub_submission_cell,
                                sub_date_prod_end,
                                sub_dur_prod,
                                sub_date_wh,
                                sub_dur_deliv,
                                sub_date_1c,
                                sub_term_acc,
                                w_prev_item_cell,
                                w_fact_item_cell,
                                tariff_item_cell,
                                (
                                    _cell(
                                        "unit_cost",
                                        item_unit_cost_text,
                                        size=8,
                                        bgcolor=_SUB_BG,
                                    )
                                    if "unit_cost" in by_key
                                    else ft.Container()
                                ),
                                _cell("action_edit", "", size=8, bgcolor=_SUB_BG),
                            ],
                            spacing=0,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        height=row_h,
                    )
                )

        _sub_rows_col = ft.Column(sub_rows, spacing=1, visible=_lines_expanded)
        _sub_rows_col_ref["col"] = _sub_rows_col

        # ── Інвойс/Пакінг: прикріплені xlsx-файли ──────────────────────────
        def _order_attachments_by_kind() -> dict:
            atts = self._order_attachments_cache.get(order_id, []) if order_id else []
            out: dict = {}
            for a in atts:
                k = str((a or {}).get("kind") or "")
                if k:
                    out[k] = a
            return out

        def _update_expand_indicator():
            by_kind = _order_attachments_by_kind()
            has_inv = "invoice" in by_kind
            has_pack = "packing" in by_kind
            if has_inv and has_pack:
                new_bg = "#D1FAE5"
            elif has_inv or has_pack:
                new_bg = "#FEF9C3"
            else:
                new_bg = "#FEE2E2"
            if _expand_cell_ctr.bgcolor != new_bg:
                _expand_cell_ctr.bgcolor = new_bg
                with contextlib.suppress(Exception):
                    _expand_cell_ctr.update()

        invoice_panel_body = ft.Container(
            visible=bool(self._invoice_panel_open_by_order.get(order_id, False)),
            margin=ft.margin.only(top=4),
        )

        def _build_attachment_panel_content():
            by_kind = _order_attachments_by_kind()
            rows_ui = []
            for kind, label in (
                ("invoice", self.t("orders.attachment.invoice_label", "Інвойс")),
                ("packing", self.t("orders.attachment.packing_label", "Пакінг")),
            ):
                att = by_kind.get(kind) or {}
                att_id = str(att.get("id") or "")
                fname = str(att.get("original_filename") or "")
                fsize = int(att.get("file_size") or 0)
                has_file = bool(att_id)

                async def _do_open(_aid=att_id, _fname=fname):
                    if not _aid:
                        return
                    data = await asyncio.to_thread(
                        self.db.get_order_attachment_data, _aid
                    )
                    if not data:
                        self._show_snack(
                            self.t(
                                "orders.attachment.err_no_data",
                                "Не вдалося отримати файл",
                            ),
                            "#DC2626",
                        )
                        return
                    try:
                        tmp_path = os.path.join(
                            tempfile.gettempdir(), _fname or "file.xlsx"
                        )
                        with open(tmp_path, "wb") as fh:
                            fh.write(data)
                        os.startfile(tmp_path)  # noqa: S606
                    except Exception as ex:
                        self._show_snack(f"Помилка відкриття: {ex}", "#DC2626")

                async def _do_save(_aid=att_id, _fname=fname):
                    pg = self._page()
                    if not pg or not _aid:
                        return
                    fp = ft.FilePicker()
                    pg.services.append(fp)
                    pg.update()
                    await asyncio.sleep(0.2)
                    save_path = await fp.save_file(
                        dialog_title=self.t(
                            "orders.attachment.save_dialog", 'Зберегти "{fname}"'
                        ).format(fname=_fname),
                        file_name=_fname,
                        allowed_extensions=(
                            [_fname.rsplit(".", 1)[-1]] if "." in _fname else None
                        ),
                    )
                    with contextlib.suppress(Exception):
                        pg.services.remove(fp)
                        pg.update()
                    if not save_path:
                        return
                    data = await asyncio.to_thread(
                        self.db.get_order_attachment_data, _aid
                    )
                    if not data:
                        self._show_snack(
                            self.t(
                                "orders.attachment.err_no_data",
                                "Не вдалося отримати файл",
                            ),
                            "#DC2626",
                        )
                        return
                    try:
                        with open(save_path, "wb") as fh:
                            fh.write(data)
                        self._show_snack(
                            self.t(
                                "orders.attachment.saved", "Збережено: {name}"
                            ).format(name=os.path.basename(save_path))
                        )
                    except Exception as ex:
                        self._show_snack(f"Помилка збереження: {ex}", "#DC2626")

                async def _do_upload(_kind=kind):
                    pg = self._page()
                    if not pg:
                        return
                    fp = ft.FilePicker()
                    pg.services.append(fp)
                    pg.update()
                    await asyncio.sleep(0.2)
                    result = await fp.pick_files(
                        dialog_title=self.t(
                            "orders.attachment.pick_dialog", "Оберіть xlsx-файл"
                        ),
                        allow_multiple=False,
                        allowed_extensions=["xlsx", "xls"],
                    )
                    with contextlib.suppress(Exception):
                        pg.services.remove(fp)
                        pg.update()
                    if not result:
                        return
                    picked = result[0]
                    path = str(getattr(picked, "path", "") or "").strip()
                    upl_name = str(
                        getattr(picked, "name", "") or os.path.basename(path) or "file"
                    ).strip()
                    if not path:
                        return
                    try:
                        with open(path, "rb") as fh:
                            data = fh.read()
                    except Exception as ex:
                        self._show_snack(f"Помилка читання файлу: {ex}", "#DC2626")
                        return
                    if len(data) > _ORDER_ATTACHMENT_MAX_SIZE:
                        self._show_snack(
                            self.t(
                                "orders.attachment.err_too_large",
                                "Файл занадто великий (макс. {sz})",
                            ).format(
                                sz=_fmt_attachment_size(_ORDER_ATTACHMENT_MAX_SIZE)
                            ),
                            "#DC2626",
                        )
                        return
                    ext = (
                        upl_name.rsplit(".", 1)[-1].lower()
                        if "." in upl_name
                        else ""
                    )
                    mime = (
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        if ext == "xlsx"
                        else "application/vnd.ms-excel"
                    )
                    await asyncio.to_thread(
                        self.db.upsert_order_attachment,
                        order_id,
                        self.company_id,
                        _kind,
                        upl_name,
                        data,
                        mime,
                        self.manager_id,
                    )
                    self._order_attachments_cache[order_id] = await asyncio.to_thread(
                        self.db.get_order_attachments_meta, order_id
                    )
                    _update_expand_indicator()
                    _build_attachment_panel_content()

                async def _do_delete(_aid=att_id):
                    if not _aid:
                        return
                    await asyncio.to_thread(self.db.delete_order_attachment, _aid)
                    self._order_attachments_cache[order_id] = await asyncio.to_thread(
                        self.db.get_order_attachments_meta, order_id
                    )
                    _update_expand_indicator()
                    _build_attachment_panel_content()

                def _run_open(_e=None, _fn=_do_open):
                    pg = self._page()
                    if pg:
                        pg.run_task(_fn)

                def _run_save(_e=None, _fn=_do_save):
                    pg = self._page()
                    if pg:
                        pg.run_task(_fn)

                def _run_upload(
                    _e=None, _kind=kind, _fname=fname, _has_file=has_file, _fn=_do_upload
                ):
                    pg = self._page()
                    if not pg:
                        return
                    if _has_file:

                        def _confirm(_x=None, _dlg_fn=_fn):
                            pg.pop_dialog()
                            pg.run_task(_dlg_fn)

                        dlg = ft.AlertDialog(
                            modal=True,
                            title=ft.Text(
                                self.t(
                                    "orders.attachment.replace_confirm",
                                    'Замінити наявний файл "{fname}"?',
                                ).format(fname=_fname)
                            ),
                            actions=[
                                ft.TextButton(
                                    self.t("common.cancel", "Скасувати"),
                                    on_click=lambda _x: pg.pop_dialog(),
                                ),
                                ft.FilledButton(
                                    self.t("orders.attachment.replace_btn", "Замінити"),
                                    on_click=_confirm,
                                ),
                            ],
                        )
                        pg.open(dlg)
                    else:
                        pg.run_task(_fn)

                def _run_delete(_e=None, _fname=fname, _fn=_do_delete):
                    pg = self._page()
                    if not pg:
                        return

                    def _confirm(_x=None, _dlg_fn=_fn):
                        pg.pop_dialog()
                        pg.run_task(_dlg_fn)

                    dlg = ft.AlertDialog(
                        modal=True,
                        title=ft.Text(
                            self.t(
                                "orders.attachment.delete_confirm",
                                'Видалити файл "{fname}"?',
                            ).format(fname=_fname)
                        ),
                        actions=[
                            ft.TextButton(
                                self.t("common.cancel", "Скасувати"),
                                on_click=lambda _x: pg.pop_dialog(),
                            ),
                            ft.FilledButton(
                                self.t("common.delete", "Видалити"),
                                on_click=_confirm,
                                style=ft.ButtonStyle(bgcolor="#DC2626"),
                            ),
                        ],
                    )
                    pg.open(dlg)

                action_buttons = [
                    ft.IconButton(
                        ft.Icons.OPEN_IN_NEW,
                        icon_size=16,
                        tooltip=self.t("orders.attachment.open", "Відкрити"),
                        disabled=not has_file,
                        on_click=_run_open,
                        style=ft.ButtonStyle(padding=ft.padding.all(2)),
                    ),
                    ft.IconButton(
                        ft.Icons.SAVE_ALT,
                        icon_size=16,
                        tooltip=self.t("orders.attachment.save_pc", "Зберегти на ПК"),
                        disabled=not has_file,
                        on_click=_run_save,
                        style=ft.ButtonStyle(padding=ft.padding.all(2)),
                    ),
                ]
                if not self.is_read_only:
                    action_buttons.append(
                        ft.IconButton(
                            ft.Icons.UPLOAD_FILE,
                            icon_size=16,
                            tooltip=self.t("orders.attachment.upload", "Завантажити"),
                            on_click=_run_upload,
                            style=ft.ButtonStyle(padding=ft.padding.all(2)),
                        )
                    )
                    if has_file:
                        action_buttons.append(
                            ft.IconButton(
                                ft.Icons.DELETE_OUTLINE,
                                icon_size=16,
                                tooltip=self.t("orders.attachment.delete", "Видалити"),
                                on_click=_run_delete,
                                style=ft.ButtonStyle(padding=ft.padding.all(2)),
                            )
                        )

                rows_ui.append(
                    ft.Row(
                        [
                            ft.Text(label, size=11, weight="bold", width=56),
                            ft.Row(action_buttons, spacing=0),
                            ft.Text(
                                (
                                    f"{fname} ({_fmt_attachment_size(fsize)})"
                                    if has_file
                                    else self.t(
                                        "orders.attachment.not_uploaded",
                                        "Не завантажено",
                                    )
                                ),
                                size=11,
                                color=_TEXT if has_file else "#94A3B8",
                                no_wrap=True,
                            ),
                        ],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )

            invoice_panel_body.content = ft.Container(
                padding=8,
                border=ft.border.all(1, "#CBD5E1"),
                border_radius=6,
                bgcolor=_PANEL,
                content=ft.Column(
                    [
                        ft.Text(
                            self.t(
                                "orders.help.attachment_desc",
                                'Прикріпіть файл Інвойсу та файл Пакувального листа '
                                "(Excel) до цього замовлення. Керівники бачать і можуть "
                                "завантажити на ПК файли підлеглих, але не можуть їх "
                                "додавати чи видаляти.",
                            ),
                            size=10,
                            color="#64748B",
                        ),
                        *rows_ui,
                    ],
                    spacing=6,
                ),
            )
            with contextlib.suppress(Exception):
                invoice_panel_body.update()

        if invoice_panel_body.visible:
            _build_attachment_panel_content()

        row_control = ft.Container(
            key=ft.ScrollKey(order_id) if order_id else None,
            ref=row_container_ref,
            content=ft.Column(
                [summary_row, _sub_rows_col, invoice_panel_body], spacing=1
            ),
            border=ft.border.all(1, "#CBD5E1"),
            border_radius=0,
            bgcolor=row_bg,
            padding=ft.padding.symmetric(horizontal=0, vertical=0),
            margin=ft.margin.only(bottom=1),
        )

        def _toggle_invoice_panel(_):
            if not order_id:
                return
            new_visible = not bool(
                self._invoice_panel_open_by_order.get(order_id, False)
            )
            self._invoice_panel_open_by_order[order_id] = new_visible
            if new_visible:
                _build_attachment_panel_content()
            invoice_panel_body.visible = new_visible
            try:
                expand_btn.icon = ft.Icons.REMOVE if new_visible else ft.Icons.ADD
                expand_btn.update()
            except Exception:
                pass
            with contextlib.suppress(Exception):
                invoice_panel_body.update()

        expand_btn.on_click = _toggle_invoice_panel
        if order_id:
            self._order_row_controls[order_id] = row_control
        return row_control

    async def _focus_order_row_async(self, order_id: str):
        oid = (order_id or "").strip()
        if not oid:
            return

        await asyncio.sleep(0.08)
        target = self._order_row_controls.get(oid)
        if target is None:
            return

        try:
            if hasattr(self.content_area, "scroll_to"):
                await self.content_area.scroll_to(
                    scroll_key=ft.ScrollKey(oid), duration=450
                )
        except Exception:
            pass

        try:
            target.border = ft.border.all(2, "#0EA5E9")
            target.update()
        except Exception:
            pass

        await asyncio.sleep(1.2)
        try:
            target.border = ft.border.all(1, "#DADCE0")
            target.update()
        except Exception:
            pass

    def _confirm_delete_order(self, order_id: str):
        page = self._page()
        if not page:
            return
        if self.is_read_only:
            self._show_snack("Режим перегляду — видалення заборонено", "#DC2626")
            return

        target_order = next(
            (
                row
                for row in self._orders
                if isinstance(row, dict)
                and (row.get("id") or "").strip() == (order_id or "").strip()
            ),
            None,
        )

        def cancel(_):
            self._close_dlg(dlg)

        def confirm(_):
            # Close confirm dialog, then run async delete with overlay
            self._close_dlg(dlg)
            _sup_id = (target_order or {}).get("supplier_id") or "__no_supplier__"
            _page_ref = self._page()
            if not _page_ref:
                return

            async def _do_delete_async():
                self._show_save_overlay("Видалення замовлення...")
                await asyncio.sleep(0)  # yield so overlay appears
                # Queue the deletion in the outbox (same pattern as edits).
                # The order is removed from memory/UI immediately, then the
                # actual DB delete happens when the sync timer fires.
                self._enqueue_order_delete(order_id, target_order or {})
                self._remove_local_order(order_id)
                removed = self._remove_order_from_tree(order_id, target_order or {})
                if not removed:
                    self._render_tree(affected_supplier_ids=[_sup_id])
                self._update_counters()
                self._hide_save_overlay()
                self._show_snack("Видалено локально. Синхронізація в БД через 45 сек.")

            _page_ref.run_task(_do_delete_async)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(self.t("orders.dlg.delete_title", "Видалити замовлення?"), weight="bold"),
            content=ft.Text(self.t("common.irreversible", "Цю дію неможливо скасувати."), size=13),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=cancel),
                ft.FilledButton(self.t("common.delete", "Видалити"), on_click=confirm),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    def _show_manual_rate_modal(self, ccy: str, on_rate_entered):
        page = self._page()
        if not page:
            on_rate_entered(None)
            return

        rate_f = ft.TextField(
            label=f"Курс {ccy}/USD",
            hint_text="напр. 0.1379 для CNY",
            border_radius=8,
            width=220,
        )

        def skip(_):
            self._close_dlg(dlg)
            on_rate_entered(None)

        def confirm(_):
            r = _safe_float(rate_f.value)
            if not r or r <= 0:
                rate_f.error_text = "Введіть коректний курс"
                with contextlib.suppress(Exception):
                    rate_f.update()
                return
            self._close_dlg(dlg)
            on_rate_entered(r)

        def _rate_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, self.t("orders.nbu_unavailable", "НБУ недоступний"), [
                self.t("orders.rate_help.desc", "Введіть курс: скільки USD коштує 1 одиниця вказаної валюти."),
                self.t("orders.rate_help.example", "Приклад CNY: якщо 1 USD = 7.25 CNY → введіть 0.138 (= 1 ÷ 7.25)."),
                self.t("orders.rate_help.skip", "«Пропустити» — збереже замовлення без USD-конвертації."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                [
                    ft.Text(self.t("orders.nbu_unavailable_body", "НБУ недоступний — введіть курс вручну"), weight="bold", expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_rate_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Container(
                padding=12,
                content=ft.Column(
                    [
                        ft.Text(
                            f"Скільки USD коштує 1 {ccy}?\n(НБУ тимчасово недоступний)",
                            size=13,
                            color=_TEXT,
                        ),
                        rate_f,
                    ],
                    spacing=12,
                    tight=True,
                ),
            ),
            actions=[
                ft.TextButton("Пропустити", on_click=skip),
                ft.FilledButton("OK", on_click=confirm),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    async def _open_quick_add_order_dialog(self, busy_token: Optional[str] = None):
        page = self._page()
        if not page:
            self._end_header_busy(busy_token)
            return

        pending_new_order_id = str(uuid.uuid4())
        save_state = {"in_progress": False}

        regions = [
            r
            for r in (self._regions_data or [])
            if isinstance(r, dict) and (r.get("id") or "")
        ]
        if not regions:
            manager_role = self.user_data.get("role")
            regions = (
                await asyncio.to_thread(
                    self.db.get_visible_regions_for_manager,
                    self.company_id,
                    self.manager_id,
                    manager_role,
                )
                or []
            )
            regions = [
                r for r in regions if isinstance(r, dict) and (r.get("id") or "")
            ]

        def _no_arrow_icon() -> ft.Container:
            return ft.Container(width=0, height=0)

        region_options = [
            ft.dropdown.Option(str(r.get("id") or ""), str(r.get("name") or ""))
            for r in regions
            if (r.get("id") or "")
        ]

        default_region_id = str(
            self._effective_region_id()
            or (region_options[0].key if region_options else "")
        ).strip()
        if default_region_id and not any(
            str(getattr(opt, "key", "")) == default_region_id for opt in region_options
        ):
            default_region_id = str(
                region_options[0].key if region_options else ""
            ).strip()

        region_dd = ft.Dropdown(
            label=self.t("orders.field.region", "Регіон"),
            value=default_region_id,
            options=region_options,
            border_radius=8,
            width=280,
            trailing_icon=_no_arrow_icon(),
            selected_trailing_icon=_no_arrow_icon(),
        )
        region_wrapper = ft.Container(
            border=ft.border.all(1, _BORDER),
            border_radius=8,
            padding=ft.padding.all(1),
            content=region_dd,
        )
        order_date_state = {"value": _today_str()}
        order_date_input = ft.Text(
            order_date_state["value"],
            size=12,
            color=_TEXT,
            no_wrap=True,
        )
        order_date_error = ft.Text("", size=10, color=_ERR, visible=False)
        _picker_open_state = {"open": False}

        def _open_date_picker(_):
            if _picker_open_state["open"]:
                return
            _picker_open_state["open"] = True

            initial = date.today()
            try:
                current_txt = str(order_date_state.get("value") or "")[:10]
                if current_txt:
                    initial = datetime.fromisoformat(current_txt).date()
            except Exception:
                initial = date.today()

            picked_once = {"value": False}

            def _picked(e):
                picked = getattr(e.control, "value", None)
                picked_once["value"] = True
                try:
                    out = (
                        _datepicker_value_to_iso_date(picked)
                        if picked is not None
                        else ""
                    )
                except Exception:
                    out = str(picked)[:10] if picked is not None else ""
                order_date_state["value"] = out
                order_date_input.value = out
                order_date_error.visible = False
                try:
                    order_date_input.update()
                    order_date_error.update()
                except Exception:
                    pass
                _picker_open_state["open"] = False
                sdp = getattr(self, "_shared_date_picker", None)
                if sdp is not None:
                    try:
                        sdp.open = False
                        sdp.update()
                    except Exception:
                        pass

            def _dismissed(_e):
                _picker_open_state["open"] = False
                if not picked_once["value"]:
                    order_date_state["value"] = ""
                    order_date_input.value = ""
                    order_date_error.visible = False
                    try:
                        order_date_input.update()
                        order_date_error.update()
                    except Exception:
                        pass

            sdp = getattr(self, "_shared_date_picker", None)
            sdp_state = getattr(self, "_shared_date_picker_state", None)
            if sdp is not None and sdp_state is not None:
                sdp_state["on_change"] = _picked
                sdp_state["on_dismiss"] = _dismissed
                sdp.value = initial
                sdp.open = True
                sdp.update()
            else:
                picker = ft.DatePicker(
                    value=initial,
                    first_date=date(2018, 1, 1),
                    last_date=date(2100, 12, 31),
                    entry_mode=ft.DatePickerEntryMode.CALENDAR_ONLY,
                    date_picker_mode=ft.DatePickerMode.DAY,
                    locale=ft.Locale("uk", "UA"),
                    cancel_text="Очистити",
                )
                picker.on_change = _picked
                picker.on_dismiss = _dismissed
                page.overlay.append(picker)
                picker.open = True
                with contextlib.suppress(Exception):
                    picker.update()

        order_date_box = ft.Container(
            width=180,
            height=58,
            border=ft.border.all(1, "#CBD5E1"),
            border_radius=8,
            bgcolor=None,
            alignment=ft.Alignment(0, 0),
            padding=ft.padding.symmetric(horizontal=10, vertical=0),
            on_click=_open_date_picker,
            content=ft.Row(
                [
                    ft.Container(
                        expand=True,
                        alignment=ft.Alignment(0, 0),
                        content=ft.TransparentPointer(
                            content=ft.Container(
                                height=32,
                                alignment=ft.Alignment(0, 0),
                                content=order_date_input,
                            )
                        ),
                    ),
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        order_date_cell = ft.Container(
            width=180,
            on_click=_open_date_picker,
            content=ft.Column(
                [
                    ft.TransparentPointer(
                        content=ft.Column(
                            [
                                ft.Text("Дата", size=12, color=_TEXT),
                                order_date_box,
                                order_date_error,
                            ],
                            spacing=4,
                        )
                    ),
                ],
            ),
        )
        supplier_dd = ft.Dropdown(
            label=self.t("orders.field.supplier", "Постачальник"),
            value="",
            options=[ft.dropdown.Option("", "— Оберіть постачальника —")],
            border_radius=8,
            width=380,
            trailing_icon=_no_arrow_icon(),
            selected_trailing_icon=_no_arrow_icon(),
        )
        supplier_wrapper = ft.Container(
            border=ft.border.all(1, _BORDER),
            border_radius=8,
            padding=ft.padding.all(1),
            content=supplier_dd,
        )
        status_default = (
            self._filter_status
            if str(self._filter_status or "all").strip() in self.STATUSES
            else "production"
        )
        status_dd = ft.Dropdown(
            label=self.t("orders.field.status", "Статус"),
            value=status_default,
            options=[
                ft.dropdown.Option(s, self.STATUS_LABELS.get(s, s))
                for s in self.STATUSES
            ],
            border_radius=8,
            width=260,
            trailing_icon=_no_arrow_icon(),
            selected_trailing_icon=_no_arrow_icon(),
        )
        status_wrapper = ft.Container(
            border=ft.border.all(1, _BORDER),
            border_radius=8,
            padding=ft.padding.all(1),
            content=status_dd,
        )

        validation_error_text = ft.Text(
            "", size=11, color=_ERR, visible=False, weight="w600"
        )

        suppliers_state = {"rows": []}

        async def _refresh_suppliers(region_id: str):
            rid = str(region_id or "").strip() or None
            cached = [
                r
                for r in (self._suppliers or [])
                if isinstance(r, dict)
                and (r.get("id") or "")
                and (rid is None or str(r.get("region_id") or "") == rid or not rid)
            ]
            if cached:
                rows = cached
            else:
                if getattr(self, "_no_db_after_ready", False) and getattr(
                    self, "_orders_ui_ready", False
                ):
                    rows = []
                else:
                    rows = (
                        await asyncio.to_thread(
                            self.db.get_suppliers,
                            self.company_id,
                            rid,
                            manager_id=self.manager_id,
                        )
                        or []
                    )
            rows = [r for r in rows if isinstance(r, dict) and (r.get("id") or "")]
            suppliers_state["rows"] = rows

            selected_before = str(supplier_dd.value or "").strip()
            supplier_dd.options = [
                ft.dropdown.Option("", "— Оберіть постачальника —")
            ] + [
                ft.dropdown.Option(
                    str(s.get("id") or ""), str(s.get("name") or "")[:60]
                )
                for s in rows
            ]
            if selected_before and any(
                str(getattr(opt, "key", "")) == selected_before
                for opt in (supplier_dd.options or [])
            ):
                supplier_dd.value = selected_before
            else:
                supplier_dd.value = ""
            with contextlib.suppress(Exception):
                supplier_dd.update()

        def _on_region_change(_):
            page.run_task(_refresh_suppliers(region_dd.value))

        region_dd.on_change = _on_region_change
        region_dd.on_select = _on_region_change
        await _refresh_suppliers(region_dd.value)

        def _close(_=None):
            self._quick_add_dlg_open = False
            self._close_dlg(dlg)

        async def _save(_):
            if save_state["in_progress"]:
                return
            save_state["in_progress"] = True

            region_id = str(region_dd.value or "").strip()
            supplier_id = str(supplier_dd.value or "").strip()
            order_date = str(order_date_state.get("value") or "").strip()[:10]
            status_value = str(status_dd.value or "production").strip()

            errors = []
            if not region_id:
                errors.append("Ви не вказали регіон")
            if not order_date:
                errors.append("Ви не вказали дату")
            if not supplier_id:
                errors.append("Ви не вказали постачальника")
            if status_value not in self.STATUSES:
                errors.append("Ви не вказали статус")

            if errors:
                validation_error_text.value = " · ".join(errors)
                validation_error_text.visible = True
                with contextlib.suppress(Exception):
                    validation_error_text.update()
                save_state["in_progress"] = False
                return

            validation_error_text.visible = False
            with contextlib.suppress(Exception):
                validation_error_text.update()

            supplier_row = next(
                (
                    s
                    for s in (suppliers_state.get("rows") or [])
                    if str((s or {}).get("id") or "") == supplier_id
                ),
                None,
            )
            supplier_name = str((supplier_row or {}).get("name") or "").strip()

            now_iso = datetime.utcnow().isoformat(timespec="seconds")
            new_order = {
                "id": pending_new_order_id,
                "company_id": self.company_id,
                "region_id": region_id,
                "manager_id": self.manager_id,
                "manager_name": self.manager_name,
                "mode": "single",
                "invoice_no": self._next_invoice_no(),
                "order_no_1c": "",
                "order_date": order_date,
                "supplier_id": supplier_id,
                "supplier_name": supplier_name,
                "delivery_type_id": "",
                "delivery_type_name": "",
                "delivery_price_snapshot": None,
                "delivery_price_currency_snapshot": "USD",
                "items": [
                    {
                        "item_id": str(uuid.uuid4()),
                        "product_id": "",
                        "product_name": "",
                        "qty": 0,
                        "purchase_price": None,
                        "purchase_currency": "USD",
                        "weight_per_unit_kg": None,
                        "weight_actual_kg": None,
                    }
                ],
                "status": status_value,
                "payment_status": "unpaid",
                "submission_status": "not_submitted",
                "comment": "",
                "date_shipped": "",
                "date_arrived_warehouse": "",
                "date_accepted": "",
                "duration_production_days": None,
                "duration_transit_days": None,
                "duration_warehouse_days": None,
                "created_at": now_iso,
                "updated_at": now_iso,
            }

            # Close dialog, then show overlay while injecting into tree
            _p = self._page()
            if _p:
                with contextlib.suppress(Exception):
                    _p.pop_dialog()
            self._show_save_overlay("Збереження замовлення...")
            await asyncio.sleep(0)  # yield so overlay appears before tree work

            try:
                self._merge_local_order(new_order)
                self._schedule_group_counter_refresh({supplier_id})
                injected = self._inject_new_order_to_tree(new_order, skip_update=False)
                if not injected:
                    self._render_tree(
                        force_full=False,
                        affected_supplier_ids={supplier_id},
                    )
                self._update_counters()

                save_ok = self._upsert_order_fast(new_order)
                if save_ok:
                    self._hide_save_overlay()
                    self._show_snack(
                        "Збережено локально. Синхронізація в БД через 45 сек.",
                        "#0F766E",
                    )
                    await self._focus_order_row_async(new_order.get("id"))
                else:
                    self._orders = [
                        o
                        for o in (self._orders or [])
                        if (o or {}).get("id") != pending_new_order_id
                    ]
                    self._render_tree(
                        force_full=False,
                        affected_supplier_ids={supplier_id},
                    )
                    self._update_counters()
                    self._hide_save_overlay()
                    self._show_snack("Не вдалося створити замовлення", "#DC2626")
            except Exception:
                self._hide_save_overlay()
                raise

        create_btn = ft.FilledButton(
            "Створити",
            on_click=lambda e: page.run_task(_save(e)),
        )
        def _qadd_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, self.t("orders.dlg.add_title", "Додати замовлення"), [
                self.t("orders.qadd_help.step1", "1. Оберіть Регіон зі списку."),
                self.t("orders.qadd_help.step2", "2. Натисніть на поле «Дата» і виберіть дату замовлення з календаря."),
                self.t("orders.qadd_help.step3", "3. Оберіть Статус замовлення."),
                self.t("orders.qadd_help.step4", "4. Оберіть Постачальника зі списку."),
                self.t("orders.qadd_help.step5", "5. Натисніть «Створити»."),
                self.t("orders.qadd_help.note", "Після створення можна відкрити замовлення для заповнення решти полів."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                [
                    ft.Text(self.t("orders.dlg.add_title", "Додати замовлення"), weight="bold", expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_qadd_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Container(
                width=560,
                content=ft.Column(
                    [
                        ft.Text(
                            "Швидке створення: Регіон + Дата + Постачальник",
                            size=12,
                            color=_TEXT,
                        ),
                        ft.Row([region_wrapper, order_date_cell], spacing=8, wrap=True),
                        status_wrapper,
                        supplier_wrapper,
                        validation_error_text,
                    ],
                    spacing=10,
                    tight=True,
                ),
            ),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_close),
                create_btn,
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        def _show_quick_add_safe():
            try:
                if page:
                    page.show_dialog(dlg)
            except Exception as e:
                logger.exception(f"Помилка при показу діалогу: {e}")
                self._quick_add_dlg_open = False
                if page:
                    self._show_snack(
                        "Помилка при завантаженні діалогу. Спробуйте ще раз.", "#DC2626"
                    )
            finally:
                self._end_header_busy(busy_token)

        _show_quick_add_safe()

    def _open_order_dialog(self, existing=None, busy_token: Optional[str] = None):
        page = self._page()
        if not page:
            self._end_header_busy(busy_token)
            return

        if isinstance(existing, dict) and not self._is_order_owned_by_current_manager(
            existing
        ):
            self._show_snack("Редагування чужих замовлень заборонено", "#DC2626")
            self._end_header_busy(busy_token)
            return

        is_new = existing is None
        row = existing if isinstance(existing, dict) else {}
        new_dialog_order_id = str((row.get("id") or str(uuid.uuid4()))).strip()
        save_state = {"in_progress": False}
        self._dialog_editing_order_id = (
            "" if is_new else str(row.get("id") or "").strip()
        )

        mode_state = {"value": (row.get("mode") or ("single" if is_new else ""))}

        mode_rg = ft.RadioGroup(
            value=mode_state["value"],
            content=ft.Row(
                [
                    ft.Radio(value="single", label=self.t("orders.mode.single", "1 замовлення = 1 товар")),
                    ft.Radio(value="multi", label=self.t("orders.mode.multi", "1 замовлення = декілька товарів")),
                ],
                spacing=16,
            ),
        )

        company_regions = self._regions_data or []
        active_region_id = self._effective_region_id()
        default_region_id = str(
            (row.get("region_id") if not is_new else None)
            or (active_region_id if is_new else "")
            or (company_regions[0].get("id") if company_regions else "")
            or ""
        ).strip()

        region_opts = []
        if (not is_new) and (not default_region_id):
            region_opts.append(ft.dropdown.Option("", "—"))
        region_opts.extend(
            [
                ft.dropdown.Option(r.get("id", ""), r.get("name", ""))
                for r in company_regions
                if isinstance(r, dict) and r.get("id")
            ]
        )

        region_dd = ft.Dropdown(
            label=self.t("orders.field.region", "Регіон"),
            value=default_region_id,
            options=region_opts,
            border_radius=8,
            width=200,
            trailing_icon=ft.Container(width=0, height=0),
            selected_trailing_icon=ft.Container(width=0, height=0),
        )

        lookup_state = {
            "suppliers": [],
            "products": [],
            "delivery_methods": [],
            "packaging_by_id": {},
            "price_tiers": [],
        }

        def _dialog_region_id() -> str:
            return str(region_dd.value or "").strip()

        def _load_dialog_lookups(region_id: str) -> dict:
            region_id = str(region_id or "").strip()
            if region_id and region_id not in self._region_id_by_name_map.values():
                region_id = self._region_id_by_name(region_id) or ""
            region_id = region_id or None
            if getattr(self, "_no_db_after_ready", False) and getattr(
                self, "_orders_ui_ready", False
            ):
                return {
                    "suppliers": list(self._suppliers or []),
                    "products": [],
                    "delivery_methods": list(self._delivery_methods or []),
                    "packaging_by_id": {},
                    "price_tiers": [],
                }

            suppliers = (
                self.db.get_suppliers(
                    self.company_id,
                    region_id,
                    manager_id=self.manager_id,
                )
                or []
            )
            delivery_methods_all = (
                self.db.get_effective_delivery_methods_for_manager(
                    self.company_id,
                    self.manager_id,
                    region_id=region_id,
                )
                or []
            )
            delivery_methods = [
                m
                for m in delivery_methods_all
                if isinstance(m, dict) and (m.get("enabled") is not False)
            ]
            return {
                "suppliers": suppliers,
                "products": [],
                "delivery_methods": delivery_methods,
                "packaging_by_id": {},
                "price_tiers": [],
            }

        _eff_rid = self._effective_region_id()
        needs_initial_lookup_fetch = False
        if self._suppliers and default_region_id == _eff_rid:
            lookup_state["suppliers"] = list(self._suppliers)
            lookup_state["delivery_methods"] = list(self._delivery_methods)
        else:
            needs_initial_lookup_fetch = True

        order_date_picker_state = {"is_open": False, "suppress_until": 0.0}

        def _open_order_date_picker(_e=None):
            now = time.monotonic()
            if order_date_picker_state["is_open"] or now < float(
                order_date_picker_state["suppress_until"]
            ):
                return
            order_date_picker_state["is_open"] = True

            initial = date.today()
            try:
                current_txt = str(order_date_f.value or "")[:10]
                if current_txt:
                    initial = datetime.fromisoformat(current_txt).date()
            except Exception:
                initial = date.today()

            picked_once = {"value": False}

            def _picked(e):
                picked = getattr(e.control, "value", None)
                picked_once["value"] = True
                if picked is None:
                    order_date_f.value = ""
                else:
                    try:
                        out = _datepicker_value_to_iso_date(picked)
                    except Exception:
                        out = str(picked)[:10]
                    order_date_f.value = out
                with contextlib.suppress(Exception):
                    order_date_f.update()
                order_date_picker_state["is_open"] = False
                order_date_picker_state["suppress_until"] = time.monotonic() + 0.25
                sdp = getattr(self, "_shared_date_picker", None)
                if sdp is not None:
                    try:
                        sdp.open = False
                        sdp.update()
                    except Exception:
                        pass

            def _dismissed(_e2):
                order_date_picker_state["is_open"] = False
                order_date_picker_state["suppress_until"] = time.monotonic() + 0.25
                if not picked_once["value"]:
                    order_date_f.value = ""
                    with contextlib.suppress(Exception):
                        order_date_f.update()

            sdp = getattr(self, "_shared_date_picker", None)
            sdp_state = getattr(self, "_shared_date_picker_state", None)
            if sdp is not None and sdp_state is not None:
                sdp_state["on_change"] = _picked
                sdp_state["on_dismiss"] = _dismissed
                sdp.value = initial
                sdp.open = True
                sdp.update()
            else:
                picker = ft.DatePicker(
                    value=initial,
                    first_date=date(2018, 1, 1),
                    last_date=date(2100, 12, 31),
                    entry_mode=ft.DatePickerEntryMode.CALENDAR_ONLY,
                    date_picker_mode=ft.DatePickerMode.DAY,
                    locale=ft.Locale("uk", "UA"),
                    cancel_text="Очистити",
                    error_format_text="",
                    error_invalid_text="",
                )
                picker.on_change = _picked
                picker.on_dismiss = _dismissed
                page.overlay.append(picker)
                picker.open = True
                with contextlib.suppress(Exception):
                    picker.update()

        order_date_f = ft.TextField(
            label=self.t("orders.field.order_date", "Дата замовлення"),
            value=(row.get("order_date") or (_today_str() if is_new else "")),
            hint_text="РРРР-ММ-ДД",
            border_radius=8,
            width=170,
            read_only=True,
            suffix=ft.IconButton(
                icon=ft.Icons.CALENDAR_MONTH,
                tooltip="Обрати дату",
                on_click=_open_order_date_picker,
            ),
        )
        invoice_no_f = ft.TextField(
            label=self.t("orders.field.invoice_num", "№ Інвойсу"),
            value=row.get("invoice_no") or (self._next_invoice_no() if is_new else ""),
            border_radius=8,
            width=120,
        )
        order_no_1c_f = ft.TextField(
            label=self.t("orders.field.1c_num", "№ обліку"),
            value=row.get("order_no_1c") or "",
            border_radius=8,
            width=130,
        )
        comment_f = ft.TextField(
            label=self.t("orders.field.comment", "Коментар"),
            value=row.get("comment") or "",
            border_radius=8,
            multiline=True,
            min_lines=2,
            max_lines=4,
        )

        sup_options = [ft.dropdown.Option("", "— Оберіть постачальника —")]
        for s in lookup_state["suppliers"]:
            sup_options.append(
                ft.dropdown.Option(s.get("id", ""), (s.get("name") or "")[:50])
            )
        supplier_dd = ft.Dropdown(
            label=self.t("orders.field.supplier_required", "Постачальник *"),
            value=row.get("supplier_id") or "",
            options=sup_options,
            border_radius=8,
            width=260,
            trailing_icon=ft.Container(width=0, height=0),
            selected_trailing_icon=ft.Container(width=0, height=0),
        )
        supplier_wrapper = ft.Container(
            border=ft.border.all(1, _BORDER),
            border_radius=8,
            padding=ft.padding.all(1),
            content=supplier_dd,
        )

        del_options = [ft.dropdown.Option("", "— Тип доставки —")]
        for m in lookup_state["delivery_methods"]:
            mid = m.get("id", "")
            mname = m.get("name", "")
            price, ccy = self._delivery_method_tariff(m)
            lbl = f"{mname} ({price} {ccy}/кг)" if price else mname
            del_options.append(ft.dropdown.Option(mid, lbl))
        delivery_dd = ft.Dropdown(
            label=self.t("orders.field.delivery_type", "Тип доставки"),
            value=row.get("delivery_type_id") or "",
            options=del_options,
            border_radius=8,
            width=260,
            trailing_icon=ft.Container(width=0, height=0),
            selected_trailing_icon=ft.Container(width=0, height=0),
        )

        status_dd = ft.Dropdown(
            label=self.t("orders.field.status", "Статус"),
            value=(row.get("status") or ("production" if is_new else "")),
            options=(
                (
                    [ft.dropdown.Option("", "—")]
                    if (not is_new and not (row.get("status") or ""))
                    else []
                )
                + [ft.dropdown.Option(s, self.STATUS_LABELS[s]) for s in self.STATUSES]
            ),
            border_radius=8,
            width=175,
            trailing_icon=ft.Container(width=0, height=0),
            selected_trailing_icon=ft.Container(width=0, height=0),
        )
        payment_dd = ft.Dropdown(
            label=self.t("orders.field.payment", "Оплата"),
            value=(row.get("payment_status") or ("unpaid" if is_new else "")),
            options=(
                (
                    [ft.dropdown.Option("", "—")]
                    if (not is_new and not (row.get("payment_status") or ""))
                    else []
                )
                + [ft.dropdown.Option(k, v) for k, v in self.PAYMENT_LABELS.items()]
            ),
            border_radius=8,
            width=155,
            trailing_icon=ft.Container(width=0, height=0),
            selected_trailing_icon=ft.Container(width=0, height=0),
        )
        submission_dd = ft.Dropdown(
            label=self.t("orders.field.1c", "Статус обліку"),
            value=(row.get("submission_status") or ("not_submitted" if is_new else "")),
            options=(
                (
                    [ft.dropdown.Option("", "—")]
                    if (not is_new and not (row.get("submission_status") or ""))
                    else []
                )
                + [ft.dropdown.Option(k, v) for k, v in self.SUBMISSION_LABELS.items()]
            ),
            border_radius=8,
            width=155,
            trailing_icon=ft.Container(width=0, height=0),
            selected_trailing_icon=ft.Container(width=0, height=0),
        )

        existing_items = list(row.get("items") or [{}])
        items_col = ft.Column(spacing=6)
        item_form_rows: list = []

        def _load_products_for_supplier(supplier_id: str):
            sid = str(supplier_id or "").strip()
            if not sid:
                lookup_state["products"] = []
                return
            cached = [
                p
                for p in (self._products or [])
                if str((p or {}).get("supplier_id") or "").strip() == sid
            ]
            if cached:
                lookup_state["products"] = cached
                return

            if getattr(self, "_no_db_after_ready", False) and getattr(
                self, "_orders_ui_ready", False
            ):
                lookup_state["products"] = []
                return

            products = (
                self.db.get_products(
                    self.company_id,
                    region_id=_dialog_region_id() or None,
                    manager_id=self.manager_id or None,
                    supplier_id=sid,
                )
                or []
            )
            lookup_state["products"] = [
                p
                for p in products
                if str((p or {}).get("supplier_id") or "").strip() == sid
            ]

        def _product_dd_options():
            opts = [ft.dropdown.Option("", "— Оберіть товар —")]
            selected_supplier_id = str(supplier_dd.value or "").strip()

            filtered_products = []
            for p in lookup_state["products"]:
                if (
                    selected_supplier_id
                    and str((p or {}).get("supplier_id") or "").strip()
                    != selected_supplier_id
                ):
                    continue
                filtered_products.append(p)

            label_map = self._product_option_label_map(filtered_products)

            for p in filtered_products:
                product_id = p.get("id", "")
                display_name = label_map.get(str(product_id), str(p.get("name") or ""))
                opts.append(
                    ft.dropdown.Option(
                        product_id,
                        display_name,
                        content=product_naming.with_instant_tooltip(
                            ft.Text(
                                display_name, size=13,
                                no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            display_name,
                        ),
                    )
                )

            return opts

        def _first_item_currency() -> str:
            for idata in item_form_rows:
                if (idata["prod_dd"].value or "").strip():
                    return _normalize_currency_code(idata["ccy_dd"].value, "USD")
            first_item = (
                (row.get("items") or [{}])[0]
                if isinstance(row.get("items"), list) and row.get("items")
                else {}
            )
            return _normalize_currency_code(
                (first_item or {}).get("purchase_currency"), "USD"
            )

        def _add_item_ctrl(item_data: dict):
            idx = len(item_form_rows)
            initial_pid = str(item_data.get("product_id") or "").strip()
            initial_manual_name = (
                str(item_data.get("product_name") or "").strip()
                if not initial_pid
                else ""
            )
            prod_dd = ft.Dropdown(
                label=f"Товар {idx + 1}",
                value=initial_pid,
                options=_product_dd_options(),
                border_radius=8,
                width=int(365 * 1.2),
                # Wider than the field itself so disambiguated names ("Кат: ... |
                # Постач: ...") have room in the open menu; the instant tooltip
                # on each option covers whatever still doesn't fit.
                menu_width=560,
                trailing_icon=ft.Container(width=0, height=0),
                selected_trailing_icon=ft.Container(width=0, height=0),
            )
            manual_name_f = ft.TextField(
                label=self.t("orders.field.custom_name", "Своя назва"),
                value=initial_manual_name,
                border_radius=8,
                width=int(180 * 1.2),
            )
            qty_f = ft.TextField(
                label=self.t("orders.field.quantity", "К-сть"),
                value=str(item_data.get("qty") or ""),
                border_radius=8,
                width=80,
            )
            price_f = ft.TextField(
                label=self.t("orders.field.price", "Ціна"),
                value=str(item_data.get("purchase_price") or ""),
                border_radius=8,
                width=90,
            )
            ccy_dd = ft.Dropdown(
                label=self.t("orders.field.currency_short", "Вал."),
                value=_normalize_currency_code(
                    item_data.get("purchase_currency"), "USD"
                ),
                options=[ft.dropdown.Option(c, c) for c in self.CURRENCIES],
                border_radius=8,
                width=90,
                trailing_icon=ft.Container(width=0, height=0),
                selected_trailing_icon=ft.Container(width=0, height=0),
            )
            weight_unit_f = ft.TextField(
                label=self.t("orders.field.weight_unit", "Вага/шт кг"),
                value=str(item_data.get("weight_per_unit_kg") or ""),
                border_radius=8,
                width=105,
                read_only=False,
            )
            weight_actual_f = ft.TextField(
                label=self.t("orders.field.weight_actual", "Вага факт. кг"),
                value=str(item_data.get("weight_actual_kg") or ""),
                border_radius=8,
                width=115,
            )

            def _apply_selected_product_snapshot(
                selected_product_id: str,
                pf=price_f,
                cd=ccy_dd,
                wf=weight_unit_f,
            ):
                sel_id = (selected_product_id or "").strip()
                prod = next(
                    (
                        p
                        for p in (lookup_state.get("products") or [])
                        if p.get("id") == sel_id
                    ),
                    None,
                )
                selected_supplier_id = str(supplier_dd.value or "").strip()
                if (
                    selected_supplier_id
                    and prod
                    and str((prod or {}).get("supplier_id") or "").strip()
                    != selected_supplier_id
                ):
                    prod = None
                if prod:
                    w = prod.get("weight_kg")
                    wf.value = str(w) if w is not None else ""
                    pp = prod.get("purchase_price")
                    pc = _normalize_currency_code(prod.get("purchase_currency"), "USD")
                    if pp is not None:
                        pf.value = str(pp)
                        cd.value = pc if pc in self.CURRENCIES else "USD"
                    try:
                        wf.update()
                        pf.update()
                        cd.update()
                    except RuntimeError:
                        pass

            def on_prod_change(e):
                _apply_selected_product_snapshot(getattr(e.control, "value", None))

            prod_dd.on_change = on_prod_change
            prod_dd.on_select = on_prod_change
            ccy_dd.on_change = lambda _e: None

            if (prod_dd.value or "").strip():
                _apply_selected_product_snapshot(prod_dd.value)

            ctrl = ft.Column(
                [
                    ft.Row([prod_dd, manual_name_f], spacing=6, wrap=True),
                    ft.Row(
                        [qty_f, price_f, ccy_dd, weight_unit_f, weight_actual_f],
                        spacing=6,
                        wrap=True,
                    ),
                ],
                spacing=6,
                tight=True,
            )
            idata = {
                "ctrl": ctrl,
                "prod_dd": prod_dd,
                "manual_name_f": manual_name_f,
                "qty_f": qty_f,
                "price_f": price_f,
                "ccy_dd": ccy_dd,
                "weight_unit_f": weight_unit_f,
                "weight_actual_f": weight_actual_f,
                "item_id": item_data.get("item_id") or str(uuid.uuid4()),
            }
            item_form_rows.append(idata)
            items_col.controls.append(ctrl)

        if not is_new:
            _load_products_for_supplier(str(supplier_dd.value or "").strip())

        for it in existing_items:
            _add_item_ctrl(it)

        add_item_btn = ft.TextButton(
            "+ Додати товар",
            icon=ft.Icons.ADD,
            visible=mode_state["value"] == "multi",
        )

        def on_add_item(_):
            _add_item_ctrl({})
            items_col.update()

        add_item_btn.on_click = on_add_item

        def on_mode_change(e):
            mode_state["value"] = e.control.value
            is_multi = mode_state["value"] == "multi"
            add_item_btn.visible = is_multi
            if not is_multi:
                while len(item_form_rows) > 1:
                    item_form_rows.pop()
                    items_col.controls.pop()
                items_col.update()
            add_item_btn.update()

        mode_rg.on_change = on_mode_change

        def _dropdown_has_value(dd: ft.Dropdown, value: str) -> bool:
            current = str(value or "")
            for opt in dd.options or []:
                if str(getattr(opt, "key", "")) == current:
                    return True
            return False

        def _refresh_region_lookups(
            selected_region_id: str = None,
            *,
            use_existing_lookup_state: bool = False,
        ):
            region_id = str(
                selected_region_id
                if selected_region_id is not None
                else _dialog_region_id()
            ).strip()
            if not use_existing_lookup_state:
                lookup_state.update(_load_dialog_lookups(region_id))

            supplier_dd.options = [ft.dropdown.Option("", "— Оберіть постачальника —")]
            supplier_dd.options.extend(
                ft.dropdown.Option(s.get("id", ""), (s.get("name") or "")[:50])
                for s in lookup_state["suppliers"]
            )
            if not _dropdown_has_value(supplier_dd, supplier_dd.value):
                supplier_dd.value = ""
            with contextlib.suppress(Exception):
                supplier_dd.update()

            delivery_dd.options = [ft.dropdown.Option("", "— Тип доставки —")]
            for m in lookup_state["delivery_methods"]:
                mid = m.get("id", "")
                mname = m.get("name", "")
                price, ccy = self._delivery_method_tariff(m)
                label = f"{mname} ({price} {ccy}/кг)" if price else mname
                delivery_dd.options.append(ft.dropdown.Option(mid, label))
            if not _dropdown_has_value(delivery_dd, delivery_dd.value):
                delivery_dd.value = ""

            _load_products_for_supplier(str(supplier_dd.value or "").strip())
            product_options = _product_dd_options()
            for idata in item_form_rows:
                idata["prod_dd"].options = product_options
                if not _dropdown_has_value(idata["prod_dd"], idata["prod_dd"].value):
                    idata["prod_dd"].value = ""
                    idata["price_f"].value = ""
                    idata["weight_unit_f"].value = ""
                if getattr(idata["prod_dd"], "page", None):
                    idata["prod_dd"].update()
                    idata["price_f"].update()
                    idata["weight_unit_f"].update()
                    idata["ccy_dd"].update()

            with contextlib.suppress(Exception):
                delivery_dd.update()

        async def _refresh_region_lookups_async(selected_region_id: str = None):
            region_id = str(
                selected_region_id
                if selected_region_id is not None
                else _dialog_region_id()
            ).strip()
            fresh = await asyncio.to_thread(_load_dialog_lookups, region_id)
            if isinstance(fresh, dict):
                lookup_state.update(fresh)
            _refresh_region_lookups(region_id, use_existing_lookup_state=True)

        def _refresh_delivery_options_for_supplier(selected_supplier_id: str = None):
            """Narrow delivery_dd's options to the methods the selected
            supplier's goods actually ship by (supplier.enabled_delivery_method_ids —
            set in the Постачальники edit dialog). Unset/empty on the supplier
            means unrestricted, same as before this existed.

            Never removes the CURRENTLY selected value from the options list,
            even if it wouldn't otherwise pass the filter — an order that
            already has a delivery type chosen (created before this
            restriction, or before the supplier was changed) must never have
            that selection silently cleared just by opening the dialog.
            """
            sid = str(selected_supplier_id or supplier_dd.value or "").strip()
            supplier_row = (
                next(
                    (
                        s
                        for s in (lookup_state["suppliers"] or [])
                        if isinstance(s, dict) and str(s.get("id") or "") == sid
                    ),
                    None,
                )
                if sid
                else None
            )
            enabled_ids = (
                supplier_row.get("enabled_delivery_method_ids")
                if isinstance(supplier_row, dict)
                else None
            )
            restricted = isinstance(enabled_ids, list) and len(enabled_ids) > 0
            all_methods = lookup_state["delivery_methods"] or []
            methods = (
                [
                    m
                    for m in all_methods
                    if isinstance(m, dict) and m.get("id") in enabled_ids
                ]
                if restricted
                else list(all_methods)
            )
            current_val = str(delivery_dd.value or "").strip()
            if current_val and not any(
                str((m or {}).get("id") or "") == current_val for m in methods
            ):
                kept = next(
                    (
                        m
                        for m in all_methods
                        if isinstance(m, dict)
                        and str(m.get("id") or "") == current_val
                    ),
                    None,
                )
                if kept:
                    methods = [kept] + methods
            new_options = [ft.dropdown.Option("", "— Тип доставки —")]
            for m in methods:
                mid = m.get("id", "")
                mname = m.get("name", "")
                price, ccy = self._delivery_method_tariff(m)
                lbl = f"{mname} ({price} {ccy}/кг)" if price else mname
                new_options.append(ft.dropdown.Option(mid, lbl))
            delivery_dd.options = new_options
            with contextlib.suppress(Exception):
                delivery_dd.update()

        def _refresh_products_for_supplier_selection(selected_supplier_id: str = None):
            _load_products_for_supplier(selected_supplier_id)
            product_options = _product_dd_options()
            for idata in item_form_rows:
                idata["prod_dd"].options = product_options[:]
                if not _dropdown_has_value(idata["prod_dd"], idata["prod_dd"].value):
                    idata["prod_dd"].value = ""
                    idata["price_f"].value = ""
                    idata["weight_unit_f"].value = ""
                try:
                    idata["prod_dd"].update()
                    idata["price_f"].update()
                    idata["weight_unit_f"].update()
                    idata["ccy_dd"].update()
                except Exception:
                    pass
            _refresh_delivery_options_for_supplier(selected_supplier_id)

        async def _refresh_products_for_supplier_selection_async(
            selected_supplier_id: str = None,
        ):
            await asyncio.to_thread(
                _load_products_for_supplier,
                selected_supplier_id,
            )
            product_options = _product_dd_options()
            for idata in item_form_rows:
                idata["prod_dd"].options = product_options[:]
                if not _dropdown_has_value(idata["prod_dd"], idata["prod_dd"].value):
                    idata["prod_dd"].value = ""
                    idata["price_f"].value = ""
                    idata["weight_unit_f"].value = ""
                try:
                    idata["prod_dd"].update()
                    idata["price_f"].update()
                    idata["weight_unit_f"].update()
                    idata["ccy_dd"].update()
                except Exception:
                    pass
            _refresh_delivery_options_for_supplier(selected_supplier_id)

        def supplier_refresh_handler(e):
            selected_supplier_id = str(
                getattr(e.control, "value", "") or e.data or ""
            ).strip()
            supplier_dd.value = selected_supplier_id
            if page and hasattr(page, "run_task"):
                with contextlib.suppress(Exception):
                    page.run_task(
                        _refresh_products_for_supplier_selection_async,
                        selected_supplier_id,
                    )
                return
            _refresh_products_for_supplier_selection(selected_supplier_id)

        def region_refresh_handler(e):
            selected_region_id = str(
                getattr(e.control, "value", "") or e.data or ""
            ).strip()
            region_dd.value = selected_region_id
            if page and hasattr(page, "run_task"):
                with contextlib.suppress(Exception):
                    page.run_task(_refresh_region_lookups_async, selected_region_id)
                return
            _refresh_region_lookups(selected_region_id)

        supplier_dd.on_change = supplier_refresh_handler
        supplier_dd.on_select = supplier_refresh_handler
        region_dd.on_change = region_refresh_handler
        region_dd.on_select = region_refresh_handler

        # Initial filter for the currently selected supplier (existing order
        # being edited, or a supplier pre-filled on a new one) — never clears
        # delivery_dd.value, only narrows/reorders the option list.
        with contextlib.suppress(Exception):
            _refresh_delivery_options_for_supplier(supplier_dd.value)

        date_shipped_f = ft.TextField(
            label=self.t("orders.field.ship_date", "Дата відправки"),
            value=row.get("date_shipped") or "",
            hint_text="РРРР-ММ-ДД",
            border_radius=8,
            width=152,
        )
        date_arrived_f = ft.TextField(
            label=self.t("orders.field.warehouse_date", "Дата на складі"),
            value=row.get("date_arrived_warehouse") or "",
            hint_text="РРРР-ММ-ДД",
            border_radius=8,
            width=152,
        )
        date_accepted_f = ft.TextField(
            label=self.t("orders.field.accept_date", "Дата прийомки"),
            value=row.get("date_accepted") or "",
            hint_text="РРРР-ММ-ДД",
            border_radius=8,
            width=152,
        )

        def _dur(field: str) -> str:
            v = row.get(field)
            return f"{v} дн." if v is not None else "—"

        dates_section = ft.Container(
            visible=not is_new,
            padding=ft.padding.only(top=4),
            content=ft.Column(
                [
                    ft.Text(self.t("orders.section.transition_dates", "Дати переходів"), size=13, weight="bold", color=_TEXT),
                    ft.Row(
                        [date_shipped_f, date_arrived_f, date_accepted_f],
                        spacing=8,
                        wrap=True,
                    ),
                    ft.Text(
                        f"Вироб-во: {_dur('duration_production_days')}  "
                        f"Доставка: {_dur('duration_transit_days')}  "
                        f"Приймання: {_dur('duration_warehouse_days')}",
                        size=11,
                        color=_TEXT,
                    ),
                ],
                spacing=8,
            ),
        )

        form_error_text = ft.Text(
            "",
            size=11,
            color=_ERR,
            weight=ft.FontWeight.W_600,
            visible=False,
        )

        dlg_body = ft.Column(
            [
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Container(expand=True, content=mode_rg),
                            region_dd,
                        ],
                        spacing=12,
                        wrap=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    bgcolor=_SOFT_I,
                    border_radius=8,
                    padding=ft.padding.symmetric(horizontal=10, vertical=8),
                ),
                form_error_text,
                ft.Divider(height=6, color=_BORDER),
                ft.Text(self.t("orders.section.basic_info", "Основна інформація"), size=13, weight="bold", color=_TEXT),
                ft.Row(
                    [order_date_f, invoice_no_f, order_no_1c_f], spacing=8, wrap=True
                ),
                ft.Row([supplier_wrapper, delivery_dd], spacing=8, wrap=True),
                comment_f,
                ft.Divider(height=6, color=_BORDER),
                ft.Text(self.t("orders.section.products", "Товари"), size=13, weight="bold", color=_TEXT),
                items_col,
                add_item_btn,
                ft.Divider(height=6, color=_BORDER),
                ft.Text(self.t("orders.section.statuses", "Статуси"), size=13, weight="bold", color=_TEXT),
                ft.Row([status_dd, payment_dd, submission_dd], spacing=8, wrap=True),
                dates_section,
            ],
            spacing=10,
            scroll=ft.ScrollMode.ADAPTIVE,
            height=530,
        )

        def close_dialog(_):
            self._dialog_editing_order_id = ""
            self._close_dlg(dlg)

        def _do_save():
            if save_state["in_progress"]:
                return
            save_state["in_progress"] = True
            form_errors: list[str] = []

            def _show_form_errors(errors: list[str]) -> None:
                if errors:
                    form_error_text.value = " • ".join(errors)
                    form_error_text.visible = True
                else:
                    form_error_text.value = ""
                    form_error_text.visible = False
                with contextlib.suppress(Exception):
                    form_error_text.update()

            try:
                if save_btn:
                    save_btn.disabled = True
                    save_btn.text = "Збереження..."
                    save_btn.update()
            except Exception:
                pass

            selected_region_id = _dialog_region_id()
            region_dd.error_text = None
            order_date_f.error_text = None
            supplier_dd.error_text = None
            status_dd.error_text = None
            payment_dd.error_text = None
            submission_dd.error_text = None

            if not selected_region_id:
                region_dd.error_text = "Оберіть регіон"
                form_errors.append("Не вказано регіон")

            order_date_value = str(order_date_f.value or "").strip()[:10]
            if not order_date_value:
                order_date_f.error_text = "Оберіть дату"
                form_errors.append("Не вказано дату замовлення")

            if not (supplier_dd.value or "").strip():
                supplier_dd.error_text = "Оберіть постачальника"
                supplier_wrapper.border = ft.border.all(2, "#DC2626")
                form_errors.append("Не вказано постачальника")
            else:
                supplier_wrapper.border = ft.border.all(1, "#E5E7EB")

            if not str(status_dd.value or "").strip():
                status_dd.error_text = "Оберіть статус"
                form_errors.append("Не вказано статус")
            if not str(payment_dd.value or "").strip():
                payment_dd.error_text = "Оберіть оплату"
                form_errors.append("Не вказано статус оплати")
            if not str(submission_dd.value or "").strip():
                submission_dd.error_text = "Оберіть статус обліку"
                form_errors.append("Не вказано статус обліку")

            with contextlib.suppress(Exception):
                supplier_wrapper.update()

            if form_errors:
                _show_form_errors(form_errors)
                save_state["in_progress"] = False
                try:
                    if save_btn:
                        save_btn.disabled = False
                        save_btn.text = "Зберегти"
                        save_btn.update()
                except Exception:
                    pass
                with contextlib.suppress(Exception):
                    dlg.update()
                return

            _show_form_errors([])

            # ── invoice duplicate check ───────────────────────────────────────
            _entered_inv = str(invoice_no_f.value or "").strip()
            if _entered_inv:
                _edit_rid = str(selected_region_id or "").strip()
                _exclude_id = new_dialog_order_id if not is_new else None
                if self._is_invoice_duplicate(
                    _entered_inv, _edit_rid, exclude_order_id=_exclude_id
                ):
                    _suggs = self._invoice_suggestions(_entered_inv, _edit_rid)
                    _sugg_str = ", ".join(_suggs) if _suggs else ""
                    invoice_no_f.error_text = (
                        f"Вже є! Варіанти: {_sugg_str}" if _sugg_str else "Вже існує"
                    )
                    _show_form_errors(
                        [
                            f"Інвойс {_entered_inv!r} вже є в цьому регіоні. "
                            + (f"Спробуйте: {_sugg_str}" if _sugg_str else "")
                        ]
                    )
                    save_state["in_progress"] = False
                    try:
                        if save_btn:
                            save_btn.disabled = False
                            save_btn.text = self.t("common.save", "Зберегти")
                            save_btn.update()
                    except Exception:
                        pass
                    with contextlib.suppress(Exception):
                        invoice_no_f.update()
                    with contextlib.suppress(Exception):
                        dlg.update()
                    return
            invoice_no_f.error_text = None
            with contextlib.suppress(Exception):
                invoice_no_f.update()

            order_items = []
            for idata in item_form_rows:
                pid = (idata["prod_dd"].value or "").strip()
                manual_name = str(idata["manual_name_f"].value or "").strip()
                idata["qty_f"].error_text = None
                if not pid and not manual_name:
                    continue
                qty = _safe_float(idata["qty_f"].value)
                if not qty or qty <= 0:
                    idata["qty_f"].error_text = "К-сть > 0"
                    _show_form_errors(
                        ["Для кожного вибраного товару вкажіть к-сть > 0"]
                    )
                    save_state["in_progress"] = False
                    try:
                        if save_btn:
                            save_btn.disabled = False
                            save_btn.text = self.t("common.save", "Зберегти")
                            save_btn.update()
                    except Exception:
                        pass
                    with contextlib.suppress(Exception):
                        dlg.update()
                    return
                idata["qty_f"].error_text = None
                prod = next(
                    (
                        p
                        for p in (lookup_state.get("products") or [])
                        if p.get("id") == pid
                    ),
                    None,
                )
                selected_supplier_id = str(supplier_dd.value or "").strip()
                if (
                    selected_supplier_id
                    and prod
                    and str(prod.get("supplier_id") or "").strip()
                    != selected_supplier_id
                ):
                    _show_form_errors(["Товар не належить обраному постачальнику"])
                    self._show_snack(
                        "Товар не належить обраному постачальнику", "#DC2626"
                    )
                    save_state["in_progress"] = False
                    try:
                        if save_btn:
                            save_btn.disabled = False
                            save_btn.text = self.t("common.save", "Зберегти")
                            save_btn.update()
                    except Exception:
                        pass
                    return
                pname = (
                    (prod.get("name") if prod else None) or manual_name or ""
                ).strip()
                order_items.append(
                    {
                        "item_id": idata.get("item_id") or str(uuid.uuid4()),
                        "product_id": pid,
                        "product_name": pname,
                        "qty": qty,
                        "purchase_price": _safe_float(idata["price_f"].value),
                        "purchase_currency": _normalize_currency_code(
                            idata["ccy_dd"].value, "USD"
                        ),
                        "weight_per_unit_kg": _safe_float(idata["weight_unit_f"].value),
                        "weight_actual_kg": _safe_float(idata["weight_actual_f"].value),
                    }
                )

            del_id = (delivery_dd.value or "").strip()
            del_method = next(
                (m for m in lookup_state["delivery_methods"] if m.get("id") == del_id),
                None,
            )
            del_name = (del_method.get("name") if del_method else None) or del_id
            current_status = status_dd.value or "production"

            if is_new or current_status == "production":
                del_price_snapshot, del_price_ccy_snapshot = (
                    self._delivery_method_tariff(del_method)
                )
            else:
                del_price_snapshot = row.get("delivery_price_snapshot")
                del_price_ccy_snapshot = (
                    row.get("delivery_price_currency_snapshot") or "USD"
                )

            sup = next(
                (
                    s
                    for s in lookup_state["suppliers"]
                    if s.get("id") == (supplier_dd.value or "")
                ),
                None,
            )
            sup_name = (sup.get("name") if sup else None) or ""

            now_iso = datetime.utcnow().isoformat(timespec="seconds")
            order_date_eff = (order_date_f.value or _today_str()).strip()[:10]
            date_shipped_eff = (date_shipped_f.value or "").strip()[:10]
            date_wh_eff = (date_arrived_f.value or "").strip()[:10]
            date_accepted_eff = (date_accepted_f.value or "").strip()[:10]
            payload = {
                "id": new_dialog_order_id,
                "company_id": self.company_id,
                "region_id": selected_region_id,
                "manager_id": self.manager_id,
                "manager_name": self.manager_name,
                "mode": mode_state["value"],
                "invoice_no": (invoice_no_f.value or "").strip(),
                "order_no_1c": (order_no_1c_f.value or "").strip(),
                "order_date": order_date_eff,
                "supplier_id": (supplier_dd.value or "").strip(),
                "supplier_name": sup_name,
                "delivery_type_id": del_id,
                "delivery_type_name": del_name,
                "delivery_price_snapshot": del_price_snapshot,
                "delivery_price_currency_snapshot": del_price_ccy_snapshot,
                "items": order_items,
                "status": current_status,
                "payment_status": payment_dd.value or "unpaid",
                "submission_status": submission_dd.value or "not_submitted",
                "comment": (comment_f.value or "").strip(),
                "date_shipped": date_shipped_eff,
                "date_arrived_warehouse": date_wh_eff,
                "date_accepted": date_accepted_eff,
                "duration_production_days": _days_between(
                    order_date_eff, date_shipped_eff
                ),
                "duration_transit_days": _days_between(date_shipped_eff, date_wh_eff),
                "duration_warehouse_days": _days_between(
                    date_wh_eff, date_accepted_eff
                ),
                "created_at": row.get("created_at") or now_iso,
                "updated_at": now_iso,
            }

            _st_now = str(current_status or "").strip().lower()
            _is_acc = _st_now in {"accepted", "прийнятий", "прийняті", "прийнято"}
            if _is_acc and date_accepted_eff:
                products_by_id = {
                    str((p or {}).get("id") or "").strip(): p
                    for p in (lookup_state.get("products") or [])
                    if isinstance(p, dict) and str((p or {}).get("id") or "").strip()
                }
                delivery_by_id = {
                    str((m or {}).get("id") or "").strip(): m
                    for m in (lookup_state.get("delivery_methods") or [])
                    if isinstance(m, dict) and str((m or {}).get("id") or "").strip()
                }
                receive_iso = self._order_receive_date_iso(
                    payload,
                    products_by_id=products_by_id,
                    delivery_by_id=delivery_by_id,
                )
                dev_days = _days_between(receive_iso, date_accepted_eff)
                if dev_days is not None:
                    payload["deviation_days"] = int(dev_days)
                else:
                    payload.pop("deviation_days", None)
            else:
                payload.pop("deviation_days", None)

            old_row = dict(row) if isinstance(row, dict) else {}
            old_supplier_id = (
                (row.get("supplier_id") or "__no_supplier__") if (not is_new) else None
            )
            new_supplier_id = payload.get("supplier_id") or "__no_supplier__"

            def _apply_locally_sync(snap: dict):
                order_id = snap.get("id")
                if order_id:
                    self._orders_cache[order_id] = dict(snap)
                self._merge_local_order(snap)
                if (not is_new) and self._can_refresh_single_order_row(old_row, snap):
                    self._refresh_single_order_row(snap)
                elif is_new and self._inject_new_order_to_tree(snap):
                    self._update_counters()
                else:
                    affected = {new_supplier_id}
                    if old_supplier_id and old_supplier_id != new_supplier_id:
                        affected.add(old_supplier_id)
                    self._render_tree(force_full=False, affected_supplier_ids=affected)

            def _rollback_after_failed_save():
                if is_new:
                    self._remove_local_order(str(payload.get("id") or ""))
                else:
                    self._merge_local_order(old_row)
                affected = [new_supplier_id]
                if old_supplier_id and old_supplier_id != new_supplier_id:
                    affected.append(old_supplier_id)
                self._render_tree(
                    force_full=bool(is_new and old_supplier_id is None),
                    affected_supplier_ids=affected,
                )

            self._dialog_editing_order_id = ""
            self._close_dlg(dlg)

            save_busy_token = self._begin_header_busy("Зберігаємо замовлення...")

            if page and hasattr(page, "run_task"):

                async def _save_and_render(snap: dict):
                    import asyncio as _aio

                    await _aio.sleep(0)
                    busy_released = False
                    try:
                        _oid = snap.get("id")
                        if _oid:
                            self._orders_cache[_oid] = dict(snap)
                        self._merge_local_order(snap)

                        if is_new:
                            ok = self._upsert_order_fast(dict(snap))
                        else:
                            ok = self._upsert_order(dict(snap))
                        if not ok:
                            _rollback_after_failed_save()
                            self._show_snack(self.t("orders.err.save_local_failed", "Не вдалося зберегти локально"), "#DC2626")
                            self._end_header_busy(save_busy_token)
                            busy_released = True
                            return

                        if is_new:
                            self._show_snack(
                                "Локально збережено. Дані передадуться в БД через 45 секунд або після натискання Локально.",
                                "#0F766E",
                            )
                        else:
                            self._show_snack("Локально збережено")

                        _p = self._page()

                        if not is_new:
                            if self._can_refresh_single_order_row(old_row, snap):
                                self._refresh_single_order_row(snap)
                            else:
                                _aff = {new_supplier_id}
                                if (
                                    old_supplier_id
                                    and old_supplier_id != new_supplier_id
                                ):
                                    _aff.add(old_supplier_id)
                                self._render_tree(
                                    force_full=False, affected_supplier_ids=_aff
                                )
                        else:
                            _sup = (
                                snap.get("supplier_id") or ""
                            ).strip() or "__no_supplier__"
                            _injected = self._inject_new_order_to_tree(
                                snap,
                                skip_update=False,
                            )
                            if not _injected:
                                self._render_tree(
                                    force_full=False,
                                    affected_supplier_ids={_sup},
                                )

                        self._update_counters()
                        self._end_header_busy(save_busy_token)
                        busy_released = True
                    except Exception:
                        logger.exception(
                            "orders: save_and_render failed for new/edit order"
                        )
                        _rollback_after_failed_save()
                        self._show_snack("Помилка створення замовлення", "#DC2626")
                    finally:
                        if not busy_released:
                            self._end_header_busy(save_busy_token)

                scheduled = False
                try:
                    page.run_task(_save_and_render, payload)
                    scheduled = True
                except Exception:
                    scheduled = False
                if scheduled:
                    return

            try:
                _apply_locally_sync(payload)
                if is_new:
                    _ok = self._upsert_order_fast(payload)
                else:
                    _ok = self._upsert_order(payload)
                if _ok:
                    self._show_snack("Локально збережено")
                else:
                    _rollback_after_failed_save()
                    self._show_snack(self.t("orders.err.save_failed", "Не вдалося зберегти"), "#DC2626")
            finally:
                self._end_header_busy(save_busy_token)

        def save_click(_):
            _do_save()

        def _order_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, self.t("orders.dlg.edit_title", "Редагувати замовлення"), [
                self.t("orders.edit_help.step1", "1. Оберіть режим: «1 товар» або «декілька товарів» в одному замовленні."),
                self.t("orders.edit_help.step2", "2. Вкажіть Регіон і Дату замовлення (📅 відкриває календар)."),
                self.t("orders.edit_help.step3", "3. Оберіть Постачальника і Спосіб доставки."),
                self.t("orders.edit_help.step4", "4. Заповніть товар, кількість, суму, валюту та курс."),
                self.t("orders.edit_help.step5", "5. Вкажіть № Інвойсу та № обліку (якщо є)."),
                self.t("orders.edit_help.step6", "6. Встановіть Статус і дати кожного етапу: виробництво → відправка → склад → прийом."),
                self.t("orders.edit_help.step7", "7. Введіть фактичну вагу для розпаковки."),
                self.t("orders.edit_help.step8", "8. Натисніть «Зберегти». Зміни синхронізуються у фоні."),
            ])

        save_btn = ft.FilledButton(self.t("common.save", "Зберегти"), on_click=save_click)
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                [
                    ft.Text(
                        self.t("orders.dlg.new_title", "Нове замовлення") if is_new else self.t("orders.dlg.edit_title", "Редагувати замовлення"),
                        weight="bold", expand=True,
                    ),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_order_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Container(width=720, content=dlg_body),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=close_dialog),
                save_btn,
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        def _show_order_edit_safe():
            try:
                if page:
                    page.show_dialog(dlg)
            except Exception as e:
                logger.exception(f"Помилка при показу діалогу редагування: {e}")
                if page:
                    self._show_snack(
                        "Помилка при завантаженні діалогу. Спробуйте ще раз.", "#DC2626"
                    )
            finally:
                self._end_header_busy(busy_token)

        _show_order_edit_safe()

        if needs_initial_lookup_fetch and page and hasattr(page, "run_task"):
            with contextlib.suppress(Exception):
                page.run_task(_refresh_region_lookups_async, default_region_id)

    def build(self):
        global _TEXT, _MUTED, _BORDER, _PANEL, _ALT, _CHIP_T, _SOFT_I, _ERR, _ERR_BG
        _th = _th_from_view(self)
        _TEXT = _th["text"]
        _MUTED = _th["muted"]
        _BORDER = _th["border"]
        _PANEL = _th["panel"]
        _ALT = _th["alt"]
        _CHIP_T = _th["chip_text"]
        _SOFT_I = _th["soft"]
        _ERR = _th["err"]
        _ERR_BG = _th["err_bg"]
        page = self._page()
        if not page:
            return

            import flet as _ft

            from views.general_wrapper import build_general_sections

            _ar = self.active_region
            _cid, _db = self.company_id, self.db
            # Resolve region name → UUID fresh (avoid stale cache in _region_id_by_name_map)
            _rid = None
            if _ar:
                for _r in _db.get_company_regions(_cid) or []:
                    if isinstance(_r, dict):
                        _rname = str(_r.get("name") or "").strip()
                        if (
                            _rname == _ar.strip()
                            or str(_r.get("id") or "").strip() == _ar.strip()
                        ):
                            _rid = str(_r.get("id") or "").strip() or None
                            break
            logger.info(
                "[orders-debug] general build | active_region=%s | resolved_rid=%s | company_id=%s",
                _ar,
                _rid,
                _cid,
            )
            # Load all orders once, count per manager by manager_id column
            _all_orders = _db.get_orders(_cid, _rid) or []
            logger.info(
                "[orders-debug] general build | total_orders_for_region=%s | unique_managers=%s",
                len(_all_orders),
                len(
                    set(
                        str(o.get("manager_id") or "")
                        for o in _all_orders
                        if isinstance(o, dict)
                    )
                ),
            )
            _mgr_counts: dict[str, int] = {}
            for _o in _all_orders:
                if not isinstance(_o, dict):
                    continue
                _m = str(_o.get("manager_id") or "").strip()
                if _m:
                    _mgr_counts[_m] = _mgr_counts.get(_m, 0) + 1

            def _count_orders(mid, _counts=_mgr_counts):
                return _counts.get(mid, 0)

            _table_btn = _ft.OutlinedButton(
                "Таблиця",
                icon=_ft.Icons.TUNE,
                height=30,
                style=_ft.ButtonStyle(
                    text_style=_ft.TextStyle(size=11),
                    padding=_ft.padding.symmetric(horizontal=8, vertical=2),
                ),
                on_click=lambda _: self._open_table_layout_dialog(),
            )
            build_general_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={
                    "active_region": self.active_region,
                    "quick_filter": self.quick_filter,
                },
                count_getter=_count_orders,
                top_controls=[_ft.Row([_table_btn], spacing=8)],
            )
            return

        # All regions → region sub-groups
        if not self.active_region:

            import flet as _ft2

            from views.general_wrapper import build_region_sections

            _cid2, _db2, _mid2 = self.company_id, self.db, self.manager_id
            _all2 = _db2.get_orders(_cid2, None) or []
            _rcounts: dict[str, int] = {}
            for _o2 in _all2:
                if not isinstance(_o2, dict):
                    continue
                if str(_o2.get("manager_id") or "").strip() != _mid2:
                    continue
                _r2 = str(_o2.get("region_id") or "").strip()
                if _r2:
                    _rcounts[_r2] = _rcounts.get(_r2, 0) + 1

            def _count_region_orders(rid, _c=_rcounts):
                return _c.get(rid, 0)

            _tbl2 = _ft2.OutlinedButton(
                "Таблиця",
                icon=_ft2.Icons.TUNE,
                height=30,
                style=_ft2.ButtonStyle(
                    text_style=_ft2.TextStyle(size=11),
                    padding=_ft2.padding.symmetric(horizontal=8, vertical=2),
                ),
                on_click=lambda _: self._open_table_layout_dialog(),
            )
            build_region_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={"quick_filter": self.quick_filter},
                count_getter=_count_region_orders,
                region_kwarg_name="active_region",
                top_controls=[_ft2.Row([_tbl2], spacing=8)],
            )
            return

        try:
            self.content_area.scroll = ft.ScrollMode.ADAPTIVE
            self.content_area.spacing = 0
            self.content_area.padding = 0
            self.content_area.on_scroll = self._on_content_scroll
        except Exception:
            pass

        self.content_area.controls.clear()
        self._supplier_section_controls = {}
        self._orders = []
        self._orders_offset = 0
        self._orders_has_more = False

        reg_lbl = (
            str(self.user_data.get("active_region_label", "") or "").strip() or "Регіон"
        )
        mgr = str(
            self.user_data.get("active_manager_name")
            or self.user_data.get("full_name")
            or ""
        ).strip()
        subtitle = f"{reg_lbl} · {mgr}" if mgr else reg_lbl

        open_order_btn = ft.FilledButton(
            self.t("orders.dlg.add_title", "Додати замовлення"),
            icon=ft.Icons.ADD,
            height=30,
            style=ft.ButtonStyle(
                text_style=ft.TextStyle(size=11),
                padding=ft.padding.symmetric(horizontal=8, vertical=2),
            ),
            visible=not self.is_read_only,
        )
        quick_add_btn = ft.OutlinedButton(
            "Швидко",
            icon=ft.Icons.FLASH_ON,
            height=30,
            style=ft.ButtonStyle(
                text_style=ft.TextStyle(size=11),
                padding=ft.padding.symmetric(horizontal=8, vertical=2),
            ),
            visible=not self.is_read_only,
        )
        table_btn = ft.OutlinedButton(
            "Таблиця",
            icon=ft.Icons.TUNE,
            height=30,
            style=ft.ButtonStyle(
                text_style=ft.TextStyle(size=11),
                padding=ft.padding.symmetric(horizontal=8, vertical=2),
            ),
            on_click=lambda _: self._open_table_layout_dialog(),
        )
        self._open_order_btn = open_order_btn
        self._quick_add_btn = quick_add_btn
        self._table_btn = table_btn

        self._header_busy_text = ft.Text(
            "Завантаження...", size=11, color=_TEXT, weight=ft.FontWeight.W_500
        )
        self._header_busy_box = ft.Container(
            visible=bool(self._header_busy_tokens),
            content=ft.Row(
                [
                    ft.ProgressRing(width=14, height=14, stroke_width=2),
                    self._header_busy_text,
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                tight=True,
            ),
        )

        def _open_order_click(_):
            if bool(getattr(self, "_page_loading", False)):
                self._show_snack("Сторінка ще завантажується...", "#1D4ED8")
                return
            token = self._begin_header_busy("Відкриваємо форму замовлення...")
            if page and hasattr(page, "run_task"):

                async def _open_after_paint(tkn: str):
                    self._open_order_dialog(busy_token=tkn)

                try:
                    page.run_task(_open_after_paint, token)
                    return
                except Exception:
                    pass
            self._open_order_dialog(busy_token=token)

        def _open_quick_click(_):
            if bool(getattr(self, "_page_loading", False)):
                self._show_snack("Сторінка ще завантажується...", "#1D4ED8")
                return
            if getattr(self, "_quick_add_dlg_open", False):
                return
            self._quick_add_dlg_open = True
            token = self._begin_header_busy("Відкриваємо швидке створення...")
            if page and hasattr(page, "run_task"):

                async def _open_after_paint(tkn: str):
                    await self._open_quick_add_order_dialog(busy_token=tkn)

                try:
                    page.run_task(_open_after_paint, token)
                    return
                except Exception:
                    pass
            page.run_task(self._open_quick_add_order_dialog(busy_token=token))

        open_order_btn.on_click = _open_order_click
        quick_add_btn.on_click = _open_quick_click

        self._header_orders_loading_indicator = ft.Row(
            [
                ft.ProgressRing(width=16, height=16, stroke_width=2, color=_TEXT),
                ft.Text(self.t("orders.loading", "Завантаження замовлень..."), size=11, color=_TEXT),
            ],
            spacing=6,
            visible=False,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self._page_title_spinner = None  # kept for backward compat, not used visually

        _sync_indicator = self._build_sync_indicator()

        self._shared_date_picker = ft.DatePicker(
            first_date=date(2018, 1, 1),
            last_date=date(2100, 12, 31),
            entry_mode=ft.DatePickerEntryMode.CALENDAR_ONLY,
            date_picker_mode=ft.DatePickerMode.DAY,
            locale=ft.Locale("uk", "UA"),
            cancel_text="Очистити",
            error_format_text="",
            error_invalid_text="",
        )
        self._shared_date_picker_state: dict = {"on_change": None, "on_dismiss": None}

        def _shared_picker_on_change(e):
            cb = self._shared_date_picker_state.get("on_change")
            if cb:
                cb(e)

        def _shared_picker_on_dismiss(e):
            cb = self._shared_date_picker_state.get("on_dismiss")
            if cb:
                cb(e)

        self._shared_date_picker.on_change = _shared_picker_on_change
        self._shared_date_picker.on_dismiss = _shared_picker_on_dismiss
        page.overlay.append(self._shared_date_picker)

        header_row = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(
                                ft.Icons.SHOPPING_CART_OUTLINED,
                                size=26,
                                color=_TEXT,
                            ),
                            ft.Column(
                                [
                                    ft.Row(
                                        [
                                            ft.Text(
                                                self.t("orders.title", "Замовлення"),
                                                size=21,
                                                weight=ft.FontWeight.W_700,
                                                color=_TEXT,
                                            ),
                                            self._header_orders_loading_indicator,
                                            ft.IconButton(
                                                icon=ft.Icons.HELP_OUTLINE,
                                                icon_size=18,
                                                icon_color=_TEXT,
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
                                        color=_TEXT,
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
                                    open_order_btn,
                                    quick_add_btn,
                                    table_btn,
                                    _sync_indicator,
                                    self._header_busy_box,
                                ],
                                spacing=8,
                                wrap=True,
                            ),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                tight=True,
            ),
            border=ft.border.only(bottom=ft.BorderSide(1, "#E2E8F0")),
            padding=ft.padding.only(bottom=12),
        )

        _counters_row_kwargs = dict(
            spacing=4,
            run_spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        if is_mobile(page):
            self._counters_row = make_scrollable_chip_row([], **_counters_row_kwargs)
        else:
            self._counters_row = ft.Row([], wrap=True, **_counters_row_kwargs)
        counters_bar = ft.Container(
            padding=ft.padding.only(left=2, right=2, top=0, bottom=4),
            content=self._counters_row,
        )
        # Save refs for sticky header restore on tab resume
        self._sticky_header_row = header_row
        self._sticky_counters_bar = counters_bar

        self._filter_bar_host = ft.Column(
            [self._build_filter_bar()],
            spacing=0,
        )
        self._payment_summary_panel_host = ft.Column([], spacing=0, tight=True)
        self._loading_box = ft.Container(
            margin=0,
            padding=0,
            visible=False,
            expand=True,
            bgcolor="transparent",
            border_radius=0,
            disabled=True,
            on_click=None,
        )
        self._load_more_btn = ft.FilledButton(
            "Завантажити ще",
            icon=ft.Icons.EXPAND_MORE,
            visible=False,
            height=0,
            on_click=lambda _: (
                self._load_more_orders(reset=False),
                self._schedule_full_rerender(),
            ),
        )

        self._table_col_specs()

        # Sub-views (inside build_general_sections) must not clobber the shared sticky slot.
        _is_sub_view = bool(self.user_data.get("subordinate_view_readonly")) and bool(
            self.user_data.get("active_manager_id")
        )
        _sticky_slot = (
            None if _is_sub_view else getattr(self._page(), "_view_sticky_header", None)
        )
        if _sticky_slot is not None:
            _sticky_slot.controls.clear()
            _sticky_slot.controls.extend([header_row, counters_bar])
        else:
            self.content_area.controls.extend([header_row, counters_bar])

        self.content_area.controls.extend(
            [
                self._filter_bar_host,
                self._payment_summary_panel_host,
                self._tree_host,
                ft.Container(padding=0, content=self._load_more_btn),
            ]
        )
        self.content_area.update()
        if _sticky_slot is not None:
            with contextlib.suppress(Exception):
                _sticky_slot.update()
        _run_page = page or self._page()
        if _run_page and hasattr(_run_page, "run_task"):
            self._initial_load_task = _run_page.run_task(self._async_initial_load)
