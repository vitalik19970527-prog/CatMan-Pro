import asyncio
import contextlib
import copy
import json
import os
import time
import urllib.request
from datetime import datetime
from typing import Optional

import flet as ft

import i18n
from logger import logger
from views.async_view_build_helper import (
    create_orders_header_loading_indicator,
    ensure_orders_cache_fresh,
)
from views.number_format import (
    format_grouped,
    format_maybe_numeric_text,
    row_text_matches,
    safe_float,
)

# ---------------------------------------------------------------------------
# Theme colour shims — module defaults, shadowed by live theme at render time
# ---------------------------------------------------------------------------
_TEXT = "#111827"
_TEXT_DARK = "#0F172A"
_MUTED = "#64748B"
_HDR = "#E2E8F0"
_BORDER = "#E5E7EB"
_PANEL = "#FFFFFF"
_ALT = "#F1F5F9"
_OK_T = "#166534"
_OK_BG = "#DCFCE7"
_ERR_BG = "#FEE2E2"
_ERR = "#DC2626"
_ERR_DARK = "#991B1B"
_WARN_T = "#92400E"
_WARN_BG = "#FEF9C3"
_CHIP_T = "#1E3A8A"
_SOFT_I = "#EEF2FF"
_CHIP_BG = "#C7D2FE"
_ACCENT = "#1D4ED8"
_INDIGO = "#4F46E5"
_INDIGO_DK = "#4338CA"


def _th_from_view(view) -> dict:
    from views.themes import get_active_theme

    return get_active_theme(str(getattr(view, "manager_id", "") or ""))


def _safe_float(v, default: Optional[float] = 0.0) -> Optional[float]:
    return safe_float(v, default)


def _fmt_money(v) -> str:
    return format_grouped(v, decimals=2, trim=True)


class PaymentsView:
    # UI constants
    UI_PANEL_TINT = _SOFT_I
    UI_PANEL_BORDER = _CHIP_BG
    UI_BADGE_INDIGO = _INDIGO
    UI_ACCENT_BLUE = _ACCENT
    UI_TITLE_INK = _TEXT_DARK
    UI_MUTED_INK = _MUTED
    UI_CARD_BORDER = _BORDER
    UI_CARD_BG = _PANEL
    UI_ACCENT_TEAL = "#14B8A6"
    UI_ACCENT_AMBER = "#F59E0B"
    UI_ACCENT_RED = "#EF4444"

    def __init__(
        self, content_area, user_data, db, active_region=None, quick_filter: str = ""
    ):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.active_region = active_region
        self.quick_filter = quick_filter
        self._fx_uah_cache = {"ts": 0.0, "rates": {}}

        self.company_id = (self.user_data.get("company_id") or "").strip()
        self.manager_id = (
            self.user_data.get("active_manager_id") or self.user_data.get("id") or ""
        ).strip()
        self.t = i18n.bind(str(self.user_data.get("id") or ""))
        self.region_id = (
            (active_region or "").strip() if isinstance(active_region, str) else ""
        )
        self.is_read_only = bool(self.user_data.get("subordinate_view_readonly", False))
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

        # Data
        self._suppliers: list = []
        self._orders: list = []
        self._settlements: list = []
        self._products: list = []
        self._delivery_methods: list = []

        # UI state that survives tab switches
        self._expanded_suppliers: set = set()
        self._show_all_suppliers: set = set()
        self._expanded_orders: set = set()
        self._counter_payment_filter: set = set()
        self._search_text_query: str = ""
        # quick_filter from home: "supplier_debt" shows only suppliers where balance > 0.
        self._filter_debt_only: bool = str(self.quick_filter or "") == "supplier_debt"

        # Table layout — payments-specific DB key "payments_table_layout" (separate from orders)
        self._table_row_height: int = 32
        self._table_col_width_overrides: dict = {}
        self._table_col_width_overrides_unit: str = "px"
        self._visible_column_keys_cache: Optional[set] = None
        # "Перший місяць відображення" (формат "YYYY-MM", "" = вся історія).
        # Ховає лише СТАРІ РЯДКИ замовлень при рендері секції постачальника;
        # баланс/борг/лічильники рахуються з повного orders_for_supplier — не чіпаються.
        self._display_from_month: str = ""

        # Async / cache state
        self._payments_ui_ready: bool = False
        self._payments_snapshot_pulse_ts: int = 0
        self._settlements_cache_pulse_ts: int = 0
        self._ref_data_pulse_ts: int = 0
        self._outbox: dict = {}  # "order:{uuid}" / "settlement:{uuid}"
        self._outbox_flush_running: bool = False
        self._sync_timer_task = None
        self._initial_load_task = None
        self._header_loading_indicator = None

        # Sync indicator widget refs
        self._sync_indicator_text = None
        self._sync_indicator_dot = None
        self._sync_indicator_container = None
        # Balance row ref for fast in-place updates
        self._totals_row = None
        # Per-supplier balance container refs {supplier_id: ft.Container}
        self._supplier_balance_refs: dict = {}
        # Per-supplier "Сума Замовлень" and "Сплачено" header badge refs
        self._supplier_debt_refs: dict = {}
        self._supplier_paid_refs: dict = {}
        # Per-supplier section container refs for targeted cross-tab refresh
        self._supplier_section_refs: dict = {}
        # Current body/arrow/rows_host refs for each supplier — updated on every section rebuild
        # so _toggle closures can always access the live widgets even after a frozen diff rebuild
        self._section_toggle_refs: dict = {}
        # Per-supplier expand callbacks for search: {supplier_id: {'show_all': dict, 'render': fn, 'btn': TextButton}}
        self._supplier_expand_callbacks: dict = {}

        # Sticky header widget refs
        self._counters_row: Optional[ft.Row] = None
        self._sticky_header_row = None
        self._sticky_counters_bar = None
        # Sticky totals bar refs (Загальний баланс / Сума Замовлень / Сплачено)
        self._sticky_totals_bar = None
        self._totals_bal_amount_col: Optional[ft.Column] = None
        self._totals_bal_hint: Optional[ft.Text] = None
        self._totals_bal_container: Optional[ft.Container] = None
        self._totals_bal_hint_container: Optional[ft.Container] = None
        self._totals_debt_text: Optional[ft.Text] = None
        self._totals_paid_text: Optional[ft.Text] = None
        self._totals_debt_chip_col: Optional[ft.Column] = None
        self._totals_paid_chip_col: Optional[ft.Column] = None
        self._sticky_totals_bar_row_ref: Optional[ft.Row] = None
        self._sticky_totals_bar_suffix_ref: list = []

        # Region resolution (same pattern as orders_view)
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

        self._load_table_layout_state()

    # ─────────────────────────────── helpers ────────────────────────────────

    def _payment_colors(self, raw: str) -> tuple[str, str]:
        pay = str(raw or "").strip().lower()
        if pay in {"paid", "сплачений", "сплачено", "оплачено"}:
            return _OK_BG, _OK_T
        if pay in {"transferred", "передано"}:
            return "#FEF3C7", "#CA8A04"
        if pay in {
            "advance",
            "аванс",
            "передплата",
            "предоплата",
            "частково",
            "частково сплачено",
        }:
            return "#FED7AA", _WARN_T
        return _ERR_BG, _ERR_DARK

    def _payment_bucket_key(self, raw: str) -> str:
        pay = str(raw or "").strip().lower()
        if pay in {"paid", "сплачений", "сплачено", "оплачено"}:
            return "paid"
        if pay in {"transferred", "передано"}:
            return "transferred"
        if pay in {
            "advance",
            "аванс",
            "передплата",
            "предоплата",
            "частково",
            "частково сплачено",
        }:
            return "advance"
        return "unpaid"

    def _page(self):
        if not self.content_area:
            return None
        try:
            return self.content_area.page
        except RuntimeError:
            return None
        except Exception:
            return None

    def _active_manager_id(self) -> str:
        return str(
            self.user_data.get("active_manager_id") or self.user_data.get("id") or ""
        ).strip()

    def _show_snack(self, text: str, color: str = "#059669") -> None:
        page = self._page()
        if not page:
            return
        import contextlib

        with contextlib.suppress(Exception):
            page.show_dialog(
                ft.SnackBar(
                    ft.Text(text, color=ft.Colors.WHITE, weight="bold"),
                    bgcolor=color,
                    duration=3000,
                )
            )

    def _show_help(self) -> None:
        from views.help_utils import show_modal_help
        page = self._page()
        show_modal_help(page, self.t("payments.help.title", "Взаєморозрахунки"), [
            self.t("payments.help.desc", "Баланс розрахунків з постачальниками: борг, аванси, компенсації."),
            (self.t("payments.help.steps_title", "Як користуватись"), None),
            self.t("payments.help.step1", "1. Чіпи вгорі фільтрують замовлення за станом оплати."),
            self.t("payments.help.step2", "2. Список згрупований по постачальниках — клік розгортає замовлення."),
            self.t("payments.help.step3", "3. Іконка таблиці дозволяє налаштувати видимі стовпці та ширину."),
            self.t("payments.help.step4", "4. «Компенсація» — додайте коригування суми по постачальнику."),
            self.t("payments.help.step5", "5. «Початковий баланс» — вкажіть стартовий борг вручну."),
            self.t("payments.help.step6", "6. Переключайте між менеджерами через фільтр зверху."),
        ])

    def _is_invoice_duplicate(
        self,
        invoice: str,
        region_id: str | None = None,
        exclude_order_id: str | None = None,
    ) -> bool:
        inv = str(invoice or "").strip()
        if not inv:
            return False
        rid = str(region_id or "").strip() or None
        ex_id = str(exclude_order_id or "").strip()
        mid = self._active_manager_id()
        for o in self._orders or []:
            if not isinstance(o, dict):
                continue
            if mid and str(o.get("manager_id") or "").strip() != mid:
                continue
            if rid and str(o.get("region_id") or "").strip() != rid:
                continue
            if ex_id and str(o.get("id") or "").strip() == ex_id:
                continue
            if str(o.get("invoice_no") or "").strip() == inv:
                return True
        return False

    def _workspace_user_id(self) -> str:
        ud = self.user_data or {}
        return str(
            ud.get("id") or ud.get("active_manager_id") or self.manager_id or ""
        ).strip()

    # ─────────────────────────── region resolution ───────────────────────────

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

    # ────────────────────── table layout (payments-specific key) ────────────────

    def _table_layout_key(self) -> str:
        region = (self._effective_region_id() or "all").strip() or "all"
        cid = (self.company_id or "").strip() or "no_company"
        return f"payments_table:{cid}:{region}"

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
                payload.get("payments_table_layout")
                if isinstance(payload, dict)
                else {}
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
                payload.get("payments_table_layout")
                if isinstance(payload.get("payments_table_layout"), dict)
                else {}
            )
            widths = dict(self._table_col_width_overrides or {})
            table_store[self._table_layout_key()] = {
                "row_height": int(self._table_row_height),
                "col_widths": widths,
                "col_widths_unit": "px",
                "display_from_month": str(self._display_from_month or ""),
            }
            payload["payments_table_layout"] = table_store
            await asyncio.to_thread(
                self.db.save_manager_workspace_state, manager_id, payload
            )
        except Exception:
            pass

    # ──────────────────────── column visibility ──────────────────────────────

    def _all_column_keys(self) -> list[str]:
        return [
            "invoice",
            "order1c",
            "status",
            "qty",
            "amount",
            "amount_usd",
            "payment",
            "transferred",
            "paid",
            "product",
            "comment",
            "delivery",
            "unit_cost",
        ]

    def _visible_column_keys_set(self) -> set[str]:
        if self._visible_column_keys_cache is not None:
            return set(self._visible_column_keys_cache)
        all_keys = self._all_column_keys()
        uid = self._workspace_user_id()
        if not uid:
            self._visible_column_keys_cache = set(all_keys)
            return set(all_keys)
        try:
            payload = self.db.get_manager_workspace_state(uid) or {}
            store = (
                payload.get("payments_visible_columns")
                if isinstance(payload, dict)
                else {}
            )
            raw = (
                store.get(self._table_layout_key()) if isinstance(store, dict) else None
            )
            if isinstance(raw, list):
                selected = {str(v).strip() for v in raw if str(v).strip()}
                visible = {k for k in all_keys if k in selected}
                # Migration: columns added after initial release default to visible
                # (old saved states don't include them; user can turn off and re-save)
                _new_default_on = {"transferred", "qty"}
                visible |= {
                    k for k in _new_default_on if k in all_keys and k not in selected
                }
                self._visible_column_keys_cache = visible or set(all_keys)
                return set(self._visible_column_keys_cache)
        except Exception:
            pass
        self._visible_column_keys_cache = set(all_keys)
        return set(all_keys)

    def _set_visible_column_keys(self, keys: list[str]) -> None:
        uid = self._workspace_user_id()
        if not uid:
            return
        all_keys = self._all_column_keys()
        chosen = {str(v).strip() for v in (keys or []) if str(v).strip()}
        normalized = [k for k in all_keys if k in chosen]
        if not normalized:
            normalized = list(all_keys)
        try:
            payload = self.db.get_manager_workspace_state(uid) or {}
            if not isinstance(payload, dict):
                payload = {}
            store = (
                payload.get("payments_visible_columns")
                if isinstance(payload.get("payments_visible_columns"), dict)
                else {}
            )
            store[self._table_layout_key()] = list(normalized)
            payload["payments_visible_columns"] = store
            self.db.save_manager_workspace_state(uid, payload)
            self._visible_column_keys_cache = set(normalized)
        except Exception:
            pass

    # ─────────────────────── file paths / app data dir ───────────────────────

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
            self._app_data_dir(), f"payments_layout_{cid}_{uid}_{rid}.json"
        )

    def _save_layout_to_file(self) -> None:
        try:
            import json as _json

            data = {
                "row_height": int(self._table_row_height),
                "col_widths": dict(self._table_col_width_overrides or {}),
                "col_widths_unit": "px",
                "display_from_month": str(self._display_from_month or ""),
            }
            with open(self._layout_local_file_path(), "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass

    def _payments_snapshot_file(self) -> str:
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_reg = (
            (self._effective_region_id() or "all").replace("/", "_").replace("\\", "_")
        )
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        # Same file as orders_view — one shared cache for both tabs.
        return os.path.join(
            self._app_data_dir(),
            f"orders_snapshot_{safe_cid}_{safe_mgr}_{safe_reg}.json",
        )

    def _settlements_file(self) -> str:
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        return os.path.join(
            self._app_data_dir(),
            f"settlements_{safe_cid}_{safe_mgr}.json",
        )

    def _outbox_file_path(self) -> str:
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        return os.path.join(
            self._app_data_dir(), f"payments_outbox_{safe_cid}_{safe_mgr}.json"
        )

    # ─────────────────────────── pulse timestamp ─────────────────────────────

    def _payments_scope_pulse_ts(self) -> int:
        # Check BOTH "orders" AND "payments" pulses — settlements bump "payments",
        # order edits bump "orders".  Taking the max ensures we detect any change.
        try:
            out = (
                self.db.get_companies_sync_pulses(
                    [self.company_id], ["orders", "payments"]
                )
                or {}
            )
            by_company = out.get(self.company_id) if isinstance(out, dict) else {}
            orders_ts = int((by_company or {}).get("orders") or 0)
            payments_ts = int((by_company or {}).get("payments") or 0)
            return max(orders_ts, payments_ts)
        except Exception:
            return 0

    # ──────────────────────────── snapshot cache ─────────────────────────────

    @staticmethod
    def _normalize_payment_status(orders: list) -> None:
        for o in orders:
            if not isinstance(o, dict):
                continue
            ps = str(o.get("payment_status") or "").strip().lower()
            if ps in {"сплачений", "сплачено", "оплачено"}:
                o["payment_status"] = "paid"
            elif ps in {
                "аванс",
                "передплата",
                "предоплата",
                "частково",
                "частково сплачено",
            }:
                o["payment_status"] = "advance"
            elif ps == "передано":
                o["payment_status"] = "transferred"
            elif ps not in {"paid", "advance", "transferred", "unpaid", ""}:
                o["payment_status"] = "unpaid"

    def _normalize_shared_cache(self) -> None:
        """Normalize Ukrainian payment_status aliases in the shared orders cache file.
        Called before reading so both Payments and Orders see identical English values.
        """
        path = self._payments_snapshot_file()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                return
            orders = payload.get("orders", [])
            if not isinstance(orders, list):
                return
            dirty = False
            for o in orders:
                if not isinstance(o, dict):
                    continue
                ps = str(o.get("payment_status") or "").strip().lower()
                if ps in {"сплачений", "сплачено", "оплачено"}:
                    o["payment_status"] = "paid"
                    dirty = True
                elif ps in {
                    "аванс",
                    "передплата",
                    "предоплата",
                    "частково",
                    "частково сплачено",
                }:
                    o["payment_status"] = "advance"
                    dirty = True
                elif ps == "передано":
                    o["payment_status"] = "transferred"
                    dirty = True
            if dirty:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
                os.replace(tmp, path)
        except Exception:
            pass

    def _load_settlements_from_db(self) -> None:
        """Load only settlements from DB — used on resume to refresh settlement data
        without re-querying orders (which come from the shared cache)."""
        _fallback = list(self._settlements)
        try:
            # Use effective region UUID — self.region_id may be a region name.
            _rid = self._effective_region_id() or None
            result = self.db.get_supplier_settlements(
                self.company_id,
                _rid,
                manager_id=self.manager_id,
                force_refresh=True,
            )
            self._settlements = result or []
        except Exception:
            self._settlements = _fallback  # preserve existing on DB error
        allowed = self._allowed_region_ids()
        if allowed:
            self._settlements = [
                s
                for s in self._settlements
                if isinstance(s, dict)
                and str(s.get("region_id") or "").strip() in allowed
            ]

    def _load_payments_snapshot_cache(self) -> bool:
        path = self._payments_snapshot_file()
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            orders = payload.get("orders", []) if isinstance(payload, dict) else []
            if not isinstance(orders, list):
                return False
            parsed = [r for r in orders if isinstance(r, dict)]
            if not parsed:
                # Empty snapshot means Orders hasn't written real data yet — treat as cache miss
                # so we fall back to the DB query with the correct visible_product_ids filter.
                return False
            self._orders = parsed
            self._normalize_payment_status(self._orders)
            self._sync_to_shared_map()
            self._payments_snapshot_pulse_ts = int((payload or {}).get("pulse_ts") or 0)
        except Exception:
            return False
        try:
            spath = self._settlements_file()
            if os.path.exists(spath):
                with open(spath, "r", encoding="utf-8") as f:
                    sdata = json.load(f)
                slist = sdata.get("settlements", []) if isinstance(sdata, dict) else []
                self._settlements = [r for r in slist if isinstance(r, dict)]
                self._settlements_cache_pulse_ts = int(
                    (sdata if isinstance(sdata, dict) else {}).get("pulse_ts") or 0
                )
            else:
                self._settlements = []
                self._settlements_cache_pulse_ts = 0
        except Exception:
            self._settlements = []
            self._settlements_cache_pulse_ts = 0
        return True

    def _save_payments_snapshot_cache(self, pulse_ts: int | None = None) -> None:
        # Payments only owns the settlements file — never overwrite the shared orders snapshot
        # (that file is owned by orders_view so its region-filtered data stays intact).
        try:
            spath = self._settlements_file()
            ts = int(
                pulse_ts
                if pulse_ts is not None
                else (self._payments_snapshot_pulse_ts or 0)
            )
            spayload = {
                "settlements": [
                    dict(r) for r in (self._settlements or []) if isinstance(r, dict)
                ],
                "pulse_ts": ts,
                "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
            }
            tmp = spath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(spayload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, spath)
        except Exception:
            pass

    def _update_single_settlement_in_cache(self, settlement: dict) -> None:
        """Delta-update a single settlement entry in the settlements JSON cache.
        Much faster than rewriting all settlements; called on every local save."""
        sid = str((settlement or {}).get("id") or "").strip()
        if not sid:
            return
        spath = self._settlements_file()
        if not os.path.exists(spath):
            return
        try:
            with open(spath, "r", encoding="utf-8") as f:
                sdata = json.load(f)
            if not isinstance(sdata, dict):
                return
            slist = sdata.get("settlements", [])
            if not isinstance(slist, list):
                slist = []
            found = False
            for i, s in enumerate(slist):
                if str((s or {}).get("id") or "").strip() == sid:
                    slist[i] = dict(settlement)
                    found = True
                    break
            if not found:
                slist.append(dict(settlement))
            sdata["settlements"] = slist
            sdata["saved_at"] = datetime.utcnow().isoformat(timespec="seconds")
            tmp = spath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sdata, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, spath)
        except Exception:
            pass

    def _remove_settlement_from_cache(self, settlement_id: str) -> None:
        """Remove a single settlement from the cache file after deletion."""
        sid = str(settlement_id or "").strip()
        if not sid:
            return
        spath = self._settlements_file()
        if not os.path.exists(spath):
            return
        try:
            with open(spath, "r", encoding="utf-8") as f:
                sdata = json.load(f)
            if not isinstance(sdata, dict):
                return
            slist = sdata.get("settlements", [])
            if not isinstance(slist, list):
                return
            new_slist = [
                s for s in slist if str((s or {}).get("id") or "").strip() != sid
            ]
            if len(new_slist) == len(slist):
                return  # nothing removed
            sdata["settlements"] = new_slist
            sdata["saved_at"] = datetime.utcnow().isoformat(timespec="seconds")
            tmp = spath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sdata, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, spath)
        except Exception:
            pass

    def _refresh_settlements_cache_pulse(self) -> None:
        """After a successful DB flush, update the settlements cache pulse_ts so next
        resume() doesn't re-fetch settlements unnecessarily (pulse bumped by our own flush).
        """
        try:
            new_pulse = self._payments_scope_pulse_ts()
            if not new_pulse:
                return
            self._settlements_cache_pulse_ts = new_pulse
            self._payments_snapshot_pulse_ts = new_pulse
            spath = self._settlements_file()
            if not os.path.exists(spath):
                return
            with open(spath, "r", encoding="utf-8") as f:
                sdata = json.load(f)
            if not isinstance(sdata, dict):
                return
            if int(sdata.get("pulse_ts") or 0) >= new_pulse:
                return
            sdata["pulse_ts"] = new_pulse
            tmp = spath + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sdata, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, spath)
        except Exception:
            pass

    # ──────────────────────────────── outbox ─────────────────────────────────

    def _merge_orders_outbox(self) -> list:
        """Reads Orders tab outbox and merges any order changes into self._orders.
        Returns list of updated order dicts (empty = no changes)."""
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        path = os.path.join(
            self._app_data_dir(), f"orders_outbox_{safe_cid}_{safe_mgr}.json"
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
            for oid, snap in items.items():
                if not isinstance(snap, dict):
                    continue
                snap_id = str(snap.get("id") or oid or "").strip()
                if not snap_id:
                    continue
                idx = orders_by_id.get(snap_id)
                if idx is None:
                    continue
                existing = self._orders[idx]
                # Compare ALL fields — same record, same DB table
                if any(
                    str(snap.get(k) or "") != str(existing.get(k) or "") for k in snap
                ):
                    merged = {**existing, **snap}
                    self._orders[idx] = merged
                    updated.append(merged)
            return updated
        except Exception:
            return []

    def _apply_in_memory_orders_changes(self, items: dict) -> list:
        """Merges {oid: snap} dict (diffed by dashboard) into self._orders in-place."""
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
        if oid and isinstance(order, dict):
            self._shared_orders_map[oid] = dict(order)
            self._bump_sync_seq()

    def _sync_to_shared_map(self) -> None:
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
        if not self._shared_orders_map:
            return []
        updated: list = []
        local_ids: set = set()
        for i, local in enumerate(self._orders):
            if not isinstance(local, dict):
                continue
            oid = str((local.get("id") or "")).strip()
            if not oid:
                continue
            local_ids.add(oid)
            shared = self._shared_orders_map.get(oid)
            if shared is None:
                continue
            if any(str(shared.get(k) or "") != str(local.get(k) or "") for k in shared):
                self._orders[i] = dict(shared)
                updated.append(shared)
        # Pick up brand-new orders created elsewhere (e.g. Orders tab, same session)
        # that this view hasn't loaded yet — otherwise they only appear after a
        # full reload (app restart / external DB pulse change).
        for oid, shared in self._shared_orders_map.items():
            if oid.startswith("__") and oid.endswith("__"):
                continue  # metadata keys, not order ids
            if oid in local_ids or not isinstance(shared, dict):
                continue
            self._orders.append(dict(shared))
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

    def _purge_deleted_orders(self) -> bool:
        """Remove orders that were deleted in Orders tab. Returns True if any removed."""
        deleted_ids = self._shared_orders_map.get("__deleted_ids__")
        if not deleted_ids or not isinstance(deleted_ids, list):
            return False
        deleted_set = set(deleted_ids)
        before = len(self._orders)
        self._orders = [
            o
            for o in self._orders
            if not (
                isinstance(o, dict) and str((o.get("id") or "")).strip() in deleted_set
            )
        ]
        return len(self._orders) < before

    async def _bg_sync_loop(self) -> None:
        _last_pulse_check = 0.0
        while True:
            try:
                await asyncio.sleep(1.5)
                if not getattr(self, "_payments_ui_ready", False):
                    continue
                seq = int(self._shared_orders_map.get("__sync_seq__", 0) or 0)
                if seq != getattr(self, "_bg_sync_seq", 0):
                    self._bg_sync_seq = seq
                    purged = self._purge_deleted_orders()
                    changed = self._sync_from_shared_map()
                    if purged:
                        self._schedule_rerender()
                    elif changed:
                        if self._supplier_section_refs:
                            await self._refresh_affected_suppliers_async(changed)
                        else:
                            with contextlib.suppress(Exception):
                                self._update_counters()
                # Periodically poll the DB pulse for changes made from another
                # device/session, even while this tab stays open the whole
                # time (not just when the user switches away and back).
                _now = time.monotonic()
                if (_now - _last_pulse_check) >= 20.0:
                    _last_pulse_check = _now
                    await self._bg_payments_pulse_check()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────────────────

    def _load_outbox(self) -> None:
        path = self._outbox_file_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            items = raw.get("items") if isinstance(raw, dict) else {}
            if not isinstance(items, dict):
                return
            for key, snap in items.items():
                key = str(key or "").strip()
                if key and isinstance(snap, dict):
                    self._outbox[key] = snap
        except Exception:
            pass

    def _save_outbox(self) -> None:
        import datetime as _dt

        def _json_default(obj):
            if isinstance(obj, (_dt.datetime, _dt.date)):
                return obj.isoformat()
            return str(obj)

        try:
            path = self._outbox_file_path()
            if not self._outbox:
                with contextlib.suppress(Exception):
                    if os.path.exists(path):
                        os.remove(path)
                return
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "items": self._outbox,
                        "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
                    },
                    f,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=_json_default,
                )
            os.replace(tmp, path)
        except Exception:
            pass

    async def flush_pending_async(self) -> None:
        if self._outbox:
            with contextlib.suppress(Exception):
                await self._flush_outbox_async()

    def _patch_orders_snapshot(self, patches: dict) -> None:
        """Merge successfully-flushed order patches into the shared orders snapshot file.
        Called after DB flush so that next restart loads the correct paid/transferred values.
        """
        if not patches:
            return
        path = self._payments_snapshot_file()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            orders = payload.get("orders", []) if isinstance(payload, dict) else []
            if not isinstance(orders, list):
                return
            orders_by_idx = {
                str((o or {}).get("id") or "").strip(): i
                for i, o in enumerate(orders)
                if isinstance(o, dict)
            }
            changed = False
            for oid, fields in patches.items():
                idx = orders_by_idx.get(str(oid).strip())
                if idx is not None and isinstance(fields, dict):
                    orders[idx].update(fields)
                    changed = True
            if not changed:
                return
            payload["orders"] = orders
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception:
            pass

    def _refresh_orders_snapshot_pulse(self) -> None:
        """After a successful DB flush, update the snapshot file's pulse_ts so that
        ensure_orders_cache_fresh won't re-fetch from DB unnecessarily on next start."""
        path = self._payments_snapshot_file()
        if not os.path.exists(path):
            return
        try:
            from views.async_view_build_helper import _get_orders_pulse

            new_pulse = _get_orders_pulse(self.db, self.company_id)
            if not new_pulse:
                return
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if not isinstance(payload, dict):
                return
            if int(payload.get("pulse_ts") or 0) >= new_pulse:
                return  # already up-to-date
            payload["pulse_ts"] = new_pulse
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
        except Exception:
            pass

    async def _flush_outbox_async(self) -> None:
        if self._outbox_flush_running or not self._outbox:
            self._refresh_sync_indicator_state()
            return
        self._outbox_flush_running = True
        self._set_sync_status("syncing")
        error_count = 0
        try:
            keys = list(self._outbox.keys())
            # Track which settlement keys exist before flush — used to detect successful flushes
            settlement_keys_before = {
                k
                for k in keys
                if k.startswith("settlement:") or k.startswith("settlement_delete:")
            }
            # Snapshot order patches before flushing so we can persist them after success
            patch_snapshots = {
                key: dict(self._outbox[key])
                for key in keys
                if key.startswith("order_patch:")
                and isinstance(self._outbox.get(key), dict)
            }

            async def _flush_one(key: str):
                snap = self._outbox.get(key)
                if not isinstance(snap, dict):
                    self._outbox.pop(key, None)
                    return True
                try:
                    if key.startswith("order_patch:"):
                        oid = key[len("order_patch:") :]
                        cid = str(self.company_id or "").strip()
                        ok = await asyncio.to_thread(
                            self.db.patch_order_fields, oid, cid, snap
                        )
                    elif key.startswith("order:"):
                        ok = await asyncio.to_thread(
                            self.db.upsert_order, snap, self.manager_id
                        )
                    elif key.startswith("settlement:"):
                        raw_amt = snap.get("amount")
                        if raw_amt is None or raw_amt == "":
                            snap["amount"] = 0.0
                        elif isinstance(raw_amt, str):
                            try:
                                snap["amount"] = float(
                                    str(raw_amt).replace(",", ".").replace(" ", "")
                                )
                            except (ValueError, TypeError):
                                snap["amount"] = 0.0
                        ok = await asyncio.to_thread(
                            self.db.upsert_supplier_settlement, snap
                        )
                    elif key.startswith("settlement_delete:"):
                        sid = str(
                            snap.get("id") or key[len("settlement_delete:") :]
                        ).strip()
                        ok = (
                            await asyncio.to_thread(
                                self.db.delete_supplier_settlement, sid
                            )
                            if sid
                            else True
                        )
                    else:
                        ok = True  # невідомий тип — видаляємо
                except Exception:
                    ok = False
                if ok:
                    self._outbox.pop(key, None)
                return ok

            # Конкурентно флашимо всі записи outbox
            results = await asyncio.gather(
                *[_flush_one(k) for k in keys], return_exceptions=True
            )
            error_count = sum(
                1 for r in results if r is not True and not (isinstance(r, bool) and r)
            )
            self._save_outbox()

            # Update shared orders snapshot with successfully-flushed patches
            # so next restart loads the correct paid_amount / transferred_amount
            successful_patches = {
                key[len("order_patch:") :]: patch_snapshots[key]
                for key in patch_snapshots
                if key not in self._outbox  # successfully flushed = removed from outbox
            }
            flushed_settlements = settlement_keys_before - set(self._outbox.keys())
            # Any order key that was in the snapshot and is now gone from outbox was flushed
            flushed_order_keys = {
                k
                for k in keys
                if (k.startswith("order:") or k.startswith("order_patch:"))
                and k not in self._outbox
            }

            if successful_patches:
                await asyncio.to_thread(self._patch_orders_snapshot, successful_patches)

            # After ANY order flush, refresh snapshot file pulse and update in-memory
            # pulse so resume()._bg_payments_pulse_check doesn't trigger a spurious
            # full _async_initial_load on next tab visit.
            if flushed_order_keys or flushed_settlements:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._refresh_orders_snapshot_pulse)
                with contextlib.suppress(Exception):
                    # Use scope pulse (max of orders+payments) so the cached value
                    # matches what _bg_payments_pulse_check checks — prevents a
                    # spurious _async_initial_load on every tab-revisit after a
                    # settlement flush (settlements bump "payments" pulse, not "orders").
                    _np = await asyncio.to_thread(self._payments_scope_pulse_ts)
                    if _np:
                        self._payments_snapshot_pulse_ts = _np
                        self._settlements_cache_pulse_ts = _np
                        self._shared_orders_map["__session_db_pulse__"] = int(_np)

            # If settlements were flushed, also refresh the settlements file pulse.
            if flushed_settlements:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._refresh_settlements_cache_pulse)

        except Exception:
            error_count += 1
        finally:
            self._outbox_flush_running = False
            if error_count > 0:
                self._set_sync_status("error", error_count)
            else:
                self._refresh_sync_indicator_state()
            if not self._outbox:
                self._stop_sync_timer()

    async def _force_sync_all_async(self) -> None:
        await asyncio.sleep(0)
        self._set_sync_status("syncing")
        if self._outbox:
            await self._flush_outbox_async()
        else:
            self._refresh_sync_indicator_state()

    def _restart_tab(self) -> None:
        """Full tab restart after column changes — saves to local+DB then rebuilds via ctrl+f5."""
        import contextlib as _ctx
        # Persist layout (widths/row height) to local file immediately.
        with _ctx.suppress(Exception):
            self._save_layout_to_file()
        pg = self._page()
        if pg is None:
            self._schedule_refresh_all_sections()
            return
        dash = getattr(pg, "_dashboard", None)
        if dash is None:
            self._schedule_refresh_all_sections()
            return

        async def _do_restart():
            # Flush any unsaved data outbox to DB, then rebuild.
            with _ctx.suppress(Exception):
                await self._force_sync_all_async()
            with _ctx.suppress(Exception):
                await dash._handle_ctrl_f5()

        with _ctx.suppress(Exception):
            pg.run_task(_do_restart)

    # ───────────────────────────── sync timer ────────────────────────────────

    async def _sync_timer_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(45)
                if self._outbox:
                    await self._flush_outbox_async()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def _start_sync_timer(self) -> None:
        if self._sync_timer_task is not None:
            return
        page = self._page()
        if not (page and hasattr(page, "run_task")):
            return
        try:
            # Prefer create_task when already on the event loop (e.g., called from
            # _async_initial_load). This avoids the coroutine-leak warning that
            # run_coroutine_threadsafe produces when called from the loop thread.
            loop = asyncio.get_running_loop()
            if self._sync_timer_task is None:
                self._sync_timer_task = loop.create_task(self._sync_timer_loop())
            if not getattr(self, "_bg_sync_loop_started", False):
                self._bg_sync_loop_started = True
                loop.create_task(self._bg_sync_loop())
        except RuntimeError:
            # Not on the event loop thread — fall back to page.run_task
            with contextlib.suppress(Exception):
                self._sync_timer_task = page.run_task(self._sync_timer_loop)
            if not getattr(self, "_bg_sync_loop_started", False):
                self._bg_sync_loop_started = True
                with contextlib.suppress(Exception):
                    page.run_task(self._bg_sync_loop)

    def _stop_sync_timer(self) -> None:
        if self._sync_timer_task is None:
            return
        with contextlib.suppress(Exception):
            self._sync_timer_task.cancel()
        self._sync_timer_task = None

    # ──────────────────────────── sync indicator ─────────────────────────────

    def _refresh_sync_indicator_state(self) -> None:
        if self._outbox_flush_running:
            self._set_sync_status("syncing")
            return
        if self._outbox:
            self._set_sync_status("pending")
            return
        self._set_sync_status("synced")

    def _set_sync_status(self, status: str, error_count: int = 0) -> None:
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
            txt.color = _ACCENT
            dot.bgcolor = "#3B82F6"
            ctr.tooltip = self.t("orders.sync.syncing_tip", "Йде передача локальних змін у БД.")
        elif status == "error":
            txt.value = self.t("orders.sync.error", "Не збережено")
            txt.color = _ERR
            dot.bgcolor = _ERR
            ctr.tooltip = self.t("orders.sync.error_tip", "Зміни залишилися лише локально. Натисніть для повторної передачі в БД.")
        with contextlib.suppress(Exception):
            ctr.update()

    def _build_sync_indicator(self) -> ft.Container:
        self._sync_indicator_dot = ft.Container(
            width=8, height=8, border_radius=4, bgcolor=_BORDER
        )
        self._sync_indicator_text = ft.Text("", size=10, color=_TEXT)

        def _on_force_sync(_):
            page = self._page()
            if page and hasattr(page, "run_task"):
                with contextlib.suppress(Exception):
                    page.run_task(self._force_sync_all_async)

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
            tooltip="Натисніть для примусової синхронізації з БД",
            on_click=_on_force_sync,
            ink=True,
        )
        self._refresh_sync_indicator_state()
        return self._sync_indicator_container

    # ──────────────────── outbox: persist mutations ───────────────────────────

    def _persist_order_patch(
        self, order_id: str, fields: dict, rebuild_after: bool = False
    ) -> None:
        """Зберігає тільки змінені поля замовлення (не весь об'єкт).
        Використовується для paid_amount та інших точкових змін."""
        if self.is_read_only:
            return
        oid = str(order_id or "").strip()
        if not oid or not fields:
            return
        key = f"order_patch:{oid}"
        # Merge з існуючим патчем (якщо кілька змін до flush)
        existing = dict(self._outbox.get(key) or {})
        existing.update(fields)
        self._outbox[key] = existing
        # Оновлюємо локальний список щоб UI відображав актуальне
        for o in self._orders:
            if str((o or {}).get("id") or "").strip() == oid:
                o.update(fields)
                self._sync_order_to_shared_map(oid, o)
                break
        self._save_outbox()
        self._refresh_sync_indicator_state()
        self._start_sync_timer()
        if rebuild_after:
            with contextlib.suppress(Exception):
                self._update_balance_display()

    def _persist_order_async(
        self, order_payload: dict, rebuild_after: bool = False
    ) -> None:
        if self.is_read_only:
            return
        snapshot = (
            copy.deepcopy(order_payload) if isinstance(order_payload, dict) else {}
        )
        if not snapshot:
            return
        oid = str(snapshot.get("id") or "").strip()
        if not oid:
            return
        self._outbox[f"order:{oid}"] = snapshot
        # Якщо для цього замовлення є незлитий патч — об'єднуємо його в snapshot
        patch_key = f"order_patch:{oid}"
        if patch_key in self._outbox:
            self._outbox.pop(patch_key, None)
        existing_idx = next(
            (
                i
                for i, o in enumerate(self._orders)
                if str((o or {}).get("id") or "").strip() == oid
            ),
            None,
        )
        if existing_idx is not None:
            self._orders[existing_idx] = snapshot
        else:
            self._orders.append(snapshot)
        self._sync_order_to_shared_map(oid, snapshot)
        self._save_outbox()
        self._refresh_sync_indicator_state()
        self._start_sync_timer()
        if rebuild_after:
            with contextlib.suppress(Exception):
                self._update_balance_display()
            with contextlib.suppress(Exception):
                self._update_counters()
            # NOTE: do NOT call _schedule_rerender() here — rebuilding the full
            # widget tree destroys in-flight text fields and blocks re-editing
            # before the outbox is flushed to DB.

    def _persist_settlement_async(
        self, payload: dict, rebuild_after: bool = False
    ) -> None:
        if self.is_read_only:
            return
        import uuid as _uuid

        snapshot = copy.deepcopy(payload) if isinstance(payload, dict) else {}
        if not snapshot:
            return
        sid = str(snapshot.get("id") or "").strip()
        if not sid:
            sid = str(_uuid.uuid4())
            snapshot["id"] = sid
        self._outbox[f"settlement:{sid}"] = snapshot
        existing_idx = next(
            (
                i
                for i, s in enumerate(self._settlements)
                if str((s or {}).get("id") or "").strip() == sid
            ),
            None,
        )
        if existing_idx is not None:
            self._settlements[existing_idx] = snapshot
        else:
            self._settlements.append(snapshot)
        self._save_outbox()
        # Delta-update the settlements cache so next restart has current data immediately.
        with contextlib.suppress(Exception):
            self._update_single_settlement_in_cache(snapshot)
        self._refresh_sync_indicator_state()
        self._start_sync_timer()
        if rebuild_after:
            _sup_id = str(snapshot.get("supplier_id") or "").strip()
            _stype = str(snapshot.get("type") or "")
            _page = self._page()
            # opening_balance / compensation changes affect the section header balance card
            # → must use full section rebuild, not the rows-only fast path.
            _needs_full_section = _stype in ("opening_balance", "compensation")
            if _sup_id and not _needs_full_section and _sup_id in self._supplier_expand_callbacks and _page:
                # Fast path: only re-render the rows_host (skips full section rebuild)
                self._schedule_settlement_rows_refresh(_sup_id)
            elif _sup_id and _sup_id in self._supplier_section_refs and _page:
                with contextlib.suppress(Exception):
                    _page.run_task(
                        self._refresh_affected_suppliers_async,
                        [{"supplier_id": _sup_id}],
                    )
            else:
                self._schedule_rerender()

    # ─────────────────────────────── rerender ────────────────────────────────

    def expand_all_for_search(self) -> None:
        """Expand all 'Показати всі' sections so the search DOM walk can find hidden rows."""
        for sid, ctx in list(self._supplier_expand_callbacks.items()):
            show_all = ctx.get("show_all")
            render = ctx.get("render")
            btn = ctx.get("btn")
            if show_all is None or show_all.get("value"):
                continue  # already expanded
            show_all["value"] = True
            self._show_all_suppliers.add(sid)
            if btn is not None:
                with contextlib.suppress(Exception):
                    btn.text = self.t("payments.show_last_15", "Показати останні 15")
            if render:
                with contextlib.suppress(Exception):
                    render()

    def _schedule_rerender(self) -> None:
        page = self._page()
        if page and hasattr(page, "run_task"):
            with contextlib.suppress(Exception):
                page.run_task(self._async_render_initial)

    def apply_text_search(self, query: str) -> None:
        """Ctrl+F row filter: hide suppliers/orders that don't match `query`."""
        self._search_text_query = str(query or "").strip()
        self._schedule_rerender()

    def clear_text_search(self) -> None:
        self.apply_text_search("")

    def _schedule_settlement_rows_refresh(self, supplier_id: str) -> None:
        """Fast settlement refresh: re-render only the rows_host (no full section rebuild).
        Uses stored _render_rows callback — skips all order-row widget creation overhead.
        """
        page = self._page()
        if page and hasattr(page, "run_task"):
            with contextlib.suppress(Exception):
                page.run_task(self._refresh_settlement_rows_async, supplier_id)

    async def _refresh_settlement_rows_async(self, supplier_id: str) -> None:
        """Re-render only the table rows for a supplier (not the full section).
        ~10x faster than _refresh_affected_suppliers_async because it skips rebuilding
        the section container, header, and all order-row widgets from scratch.
        Runs render in a thread to avoid blocking the event loop during FX rate HTTP fetches.
        """
        ctx = self._supplier_expand_callbacks.get(supplier_id)
        if ctx is None:
            await self._refresh_affected_suppliers_async([{"supplier_id": supplier_id}])
            return
        render_fn = ctx.get("render")
        if not callable(render_fn):
            await self._refresh_affected_suppliers_async([{"supplier_id": supplier_id}])
            return
        rows_host_ref = ctx.get("rows_host")
        # Run in thread: _render_rows may call _supplier_amount_usd → _fx_uah_per_unit
        # which makes blocking HTTP requests when the FX cache is cold.
        # Pass no_update=True so rows_host.update() happens on the event loop below.
        try:
            await asyncio.to_thread(render_fn, True)
        except Exception:
            with contextlib.suppress(Exception):
                await self._refresh_affected_suppliers_async(
                    [{"supplier_id": supplier_id}]
                )
            return
        # Apply the DOM update on the event loop (thread-safe)
        if rows_host_ref is not None:
            with contextlib.suppress(Exception):
                rows_host_ref.update()
        with contextlib.suppress(Exception):
            self._update_balance_display()
        with contextlib.suppress(Exception):
            self._update_counters()

    def _schedule_refresh_all_sections(self) -> None:
        """Fast in-place refresh of ALL supplier sections (parallel build, no content_area clear).
        Used after column width/visibility changes so the screen doesn't go blank."""
        page = self._page()
        if page and hasattr(page, "run_task"):
            with contextlib.suppress(Exception):
                page.run_task(self._refresh_all_sections_async)

    async def _refresh_all_sections_async(self) -> None:
        """Rebuild all known supplier sections in parallel and update DOM in-place."""
        sids = set(self._supplier_section_refs.keys())
        if not sids:
            await self._async_render_initial()
            return
        fake_orders = [{"supplier_id": sid} for sid in sids]
        await self._refresh_affected_suppliers_async(fake_orders)

    def _build_affected_sections(self, affected_sids: set) -> dict:
        """Build new section containers for the given supplier IDs only.
        Returns {supplier_id: new_ft.Container}. Runs in asyncio.to_thread.
        Uses parallel building when multiple sections are affected."""
        supplier_name_by_id = {
            str(s.get("id") or "").strip(): str(s.get("name") or "").strip()
            for s in (self._suppliers or [])
            if isinstance(s, dict)
        }
        sids_to_build = [
            sid for sid in affected_sids if sid in self._supplier_section_refs
        ]
        if not sids_to_build:
            return {}

        def _make_args(sid):
            olist = [
                o
                for o in self._orders
                if isinstance(o, dict)
                and self._is_order_in_scope(o)
                and str(o.get("supplier_id") or "").strip() == sid
            ]
            if self._counter_payment_filter:
                olist = [
                    o
                    for o in olist
                    if self._payment_bucket_key(o.get("payment_status"))
                    in self._counter_payment_filter
                ]
            return sid, supplier_name_by_id.get(sid) or sid, olist

        args_list = [_make_args(sid) for sid in sids_to_build]

        if len(args_list) <= 1:
            result = {}
            for sid, sname, olist in args_list:
                result[sid] = self._build_supplier_section(sid, sname, olist)
            return result

        import concurrent.futures as _cf

        def _build_one(args):
            _sid, _sname, _olist = args
            return _sid, self._build_supplier_section(_sid, _sname, _olist)

        result = {}
        with _cf.ThreadPoolExecutor(max_workers=min(len(args_list), 8)) as pool:
            for sid, sect in pool.map(_build_one, args_list):
                result[sid] = sect
        return result

    async def _refresh_affected_suppliers_async(self, changed_orders: list) -> None:
        """Rebuild only the supplier sections that contain changed orders — instant update."""
        affected_sids = {
            str(o.get("supplier_id") or "").strip()
            for o in changed_orders
            if isinstance(o, dict)
        }
        affected_sids.discard("")
        if not affected_sids:
            return

        new_sections = await asyncio.to_thread(
            self._build_affected_sections, affected_sids
        )
        if not new_sections:
            return

        changed = False
        for sid, new_section in new_sections.items():
            old_section = self._supplier_section_refs.get(sid)
            if old_section is None:
                continue
            try:
                idx = next(
                    i
                    for i, c in enumerate(self.content_area.controls)
                    if c is old_section
                )
                self.content_area.controls[idx] = new_section
                self._supplier_section_refs[sid] = new_section
                changed = True
            except StopIteration:
                pass

        if changed:
            with contextlib.suppress(Exception):
                self.content_area.update()
        with contextlib.suppress(Exception):
            self._update_balance_display()
        with contextlib.suppress(Exception):
            self._update_counters()

    # ── group financial summary helpers ──────────────────────────────────────

    def _compute_group_order_stats(self, orders: list) -> dict:
        """Return {ccy: {sum, paid, balance}} from a filtered order list."""
        by_ccy: dict = {}
        for o in orders:
            if not isinstance(o, dict):
                continue
            sid = str(o.get("supplier_id") or "").strip()
            ccy = self._supplier_currency(sid) if sid else "USD"
            amt = self._order_amount_supplier_ccy(o, ccy)
            paid = _safe_float(o.get("paid_amount"), 0.0) or 0.0
            if amt is None:
                amt = paid  # balance-neutral
            e = by_ccy.setdefault(ccy, {"sum": 0.0, "paid": 0.0})
            e["sum"] += float(amt)
            e["paid"] += paid
        for e in by_ccy.values():
            e["balance"] = e["sum"] - e["paid"]
        return by_ccy

    def _build_group_stat_chips(self, stats: dict) -> list:
        """Build [Balance, Sum, Paid] stat chips from {ccy: {sum, paid, balance}} for group headers."""
        if not stats:
            return []

        def _ccy_sym(c: str) -> str:
            return {"USD": "$", "UAH": "₴", "EUR": "€", "PLN": "zł", "CNY": "¥"}.get(
                str(c or "").upper().strip(), str(c or "")
            )

        def _stat_chip(label: str, lines: list, bg: str, fg: str) -> ft.Container:
            col_items = [
                ft.Text(
                    label,
                    size=11,
                    weight=ft.FontWeight.W_700,
                    color=_MUTED,
                    text_align=ft.TextAlign.CENTER,
                )
            ]
            for line in lines:
                col_items.append(
                    ft.Text(
                        line,
                        size=10,
                        weight=ft.FontWeight.W_700,
                        color=fg,
                        text_align=ft.TextAlign.CENTER,
                    )
                )
            return ft.Container(
                width=132,
                padding=ft.padding.symmetric(horizontal=10, vertical=4),
                bgcolor=bg,
                border_radius=6,
                alignment=ft.alignment.Alignment(0, -1),
                content=ft.Column(
                    col_items,
                    spacing=2,
                    tight=True,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            )

        ccys = sorted(stats.keys())
        total_bal = sum(e["balance"] for e in stats.values())
        if abs(total_bal) < 0.01:
            bal_bg, bal_fg = _OK_BG, _OK_T
        elif total_bal > 0:
            bal_bg, bal_fg = _WARN_BG, _WARN_T
        else:
            bal_bg, bal_fg = _ERR_BG, _ERR_DARK

        bal_label = (
            self.t("payments.balance.label", "Баланс:")
            if abs(total_bal) < 0.01
            else self.t("payments.balance.we_owe_short", "Баланс (ми винні):") if total_bal > 0
            else self.t("payments.balance.they_owe_short", "Баланс (постач.):")
        )

        def _lines_for_value(field: str) -> list:
            if len(ccys) == 1:
                ccy = ccys[0]
                val = stats[ccy][field]
                line1 = f"{_fmt_money(val)} {_ccy_sym(ccy)}"
                rate = self._supplier_to_usd_rate(ccy)
                if ccy != "USD" and rate and rate > 0:
                    return [line1, f"≈ {_fmt_money(val * rate)} $"]
                return [line1]
            # Multiple currencies: convert all to USD first, then show in each
            # non-USD currency present (CNY above $), followed by ≈ X $.
            total_usd = 0.0
            all_ok = True
            for ccy in ccys:
                rate = self._supplier_to_usd_rate(ccy)
                if ccy == "USD":
                    total_usd += stats[ccy][field]
                elif rate and rate > 0:
                    total_usd += stats[ccy][field] * rate
                else:
                    all_ok = False
            if not all_ok:
                # Fallback: show each currency separately
                return [f"{_fmt_money(stats[c][field])} {_ccy_sym(c)}" for c in ccys]
            non_usd = [c for c in ccys if c != "USD"]
            if not non_usd:
                # Pure USD group
                return [f"{_fmt_money(total_usd)} $"]
            lines = []
            for _c in non_usd:
                _r = self._supplier_to_usd_rate(_c)
                _ctotal = (total_usd / _r) if (_r and _r > 0) else stats[_c][field]
                lines.append(f"{_fmt_money(_ctotal)} {_ccy_sym(_c)}")
            if abs(total_usd) > 0.005:
                lines.append(f"≈ {_fmt_money(total_usd)} $")
            return lines

        return [
            _stat_chip(bal_label, _lines_for_value("balance"), bal_bg, bal_fg),
            _stat_chip(self.t("payments.stat.orders_sum", "Сума замовлень:"), _lines_for_value("sum"), _ALT, _TEXT),
            _stat_chip(self.t("payments.stat.paid_sum", "Сплачено:"), _lines_for_value("paid"), _OK_BG, _OK_T),
        ]

    def _build_aggregate_sticky_skeleton(self, page) -> None:
        """Minimal sticky header shown immediately — just title + spinner while data loads."""
        _stk = getattr(page, "_view_sticky_header", None)
        if _stk is None or getattr(page, "_active_view", "") != "show_payments":
            return
        reg_lbl = (
            str(self.user_data.get("active_region_label") or "").strip()
            or self.t("nav.all_regions", "Всі регіони")
        )
        mgr = str(
            self.user_data.get("active_manager_name")
            or self.user_data.get("full_name")
            or ""
        ).strip()
        subtitle = f"{reg_lbl} · {mgr}" if mgr else reg_lbl

        header_row = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.PAYMENTS_OUTLINED, size=26, color=_TEXT),
                            ft.Column(
                                [
                                    ft.Row(
                                        [
                                            ft.Text(
                                                self.t("payments.title", "Взаєморозрахунки"),
                                                size=21,
                                                weight=ft.FontWeight.W_700,
                                                color=_TEXT,
                                            ),
                                            ft.ProgressRing(
                                                width=18,
                                                height=18,
                                                stroke_width=2,
                                                color=_ACCENT,
                                            ),
                                        ],
                                        spacing=10,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        tight=True,
                                    ),
                                    ft.Text(
                                        subtitle,
                                        size=13,
                                        weight=ft.FontWeight.W_500,
                                        color=_TEXT,
                                    ),
                                ],
                                spacing=2,
                                tight=True,
                                expand=True,
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
        _stk.controls[:] = [header_row]
        with contextlib.suppress(Exception):
            _stk.update()

    def _build_aggregate_sticky_now(
        self, page, all_orders: list, *, filter_manager: bool = True
    ) -> None:
        """Build sticky header for aggregate views (all-regions / general mode).
        Shows title, aggregate payment-status counters, and combined balance in all currencies.
        """
        _stk = getattr(page, "_view_sticky_header", None)
        if _stk is None or getattr(page, "_active_view", "") != "show_payments":
            return
        reg_lbl = (
            str(self.user_data.get("active_region_label") or "").strip()
            or self.t("nav.all_regions", "Всі регіони")
        )
        mgr = str(
            self.user_data.get("active_manager_name")
            or self.user_data.get("full_name")
            or ""
        ).strip()
        subtitle = f"{reg_lbl} · {mgr}" if mgr else reg_lbl

        header_row = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.PAYMENTS_OUTLINED, size=26, color=_TEXT),
                            ft.Column(
                                [
                                    ft.Text(
                                        self.t("payments.title", "Взаєморозрахунки"),
                                        size=21,
                                        weight=ft.FontWeight.W_700,
                                        color=_TEXT,
                                    ),
                                    ft.Text(
                                        subtitle,
                                        size=13,
                                        weight=ft.FontWeight.W_500,
                                        color=_TEXT,
                                    ),
                                ],
                                spacing=2,
                                tight=True,
                                expand=True,
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

        # ── aggregate counters ────────────────────────────────────────────────
        agg = {"unpaid": 0, "advance": 0, "transferred": 0, "paid": 0}
        for o in all_orders:
            if not isinstance(o, dict):
                continue
            if (
                filter_manager
                and str(o.get("manager_id") or "").strip() != self.manager_id
            ):
                continue
            if not self._is_order_in_scope(o):
                continue
            bk = self._payment_bucket_key(o.get("payment_status"))
            agg[bk] = agg.get(bk, 0) + 1
        total = sum(agg.values())

        _agg_chips_row = ft.Row([], spacing=4, run_spacing=4, wrap=True)

        def _sep_chip():
            return ft.Container(
                width=1,
                height=20,
                bgcolor=_BORDER,
                margin=ft.margin.symmetric(horizontal=4),
            )

        def _build_agg_chip_list():
            _active = self._counter_payment_filter

            def _cnt_chip(
                label: str, key, count: int, bg: str, fg: str
            ) -> ft.Container:
                is_active = (key is None and not _active) or (
                    key is not None and key in _active
                )

                def _on_chip_click(e, _k=key):
                    if _k is None:
                        self._counter_payment_filter = set()
                    elif _k in self._counter_payment_filter:
                        self._counter_payment_filter.discard(_k)
                    else:
                        self._counter_payment_filter = {_k}
                    _agg_chips_row.controls[:] = _build_agg_chip_list()
                    with contextlib.suppress(Exception):
                        _agg_chips_row.update()
                    self._rebuild_agg_content()

                return ft.Container(
                    bgcolor=_HDR if is_active else bg,
                    border=ft.border.all(1, _TEXT_DARK if is_active else "#00000000"),
                    border_radius=6,
                    padding=ft.padding.symmetric(horizontal=6, vertical=2),
                    ink=True,
                    on_click=_on_chip_click,
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

            return [
                _cnt_chip(self.t("payments.chip.all", "Всі"), None, total, _HDR, _TEXT_DARK),
                _sep_chip(),
                _cnt_chip(self.t("orders.payment.unpaid", "Не сплачен."), "unpaid", agg["unpaid"], _ERR_BG, _ERR),
                _cnt_chip(self.t("orders.payment.advance", "Аванс"), "advance", agg["advance"], "#FED7AA", _WARN_T),
                _cnt_chip(self.t("orders.payment.transferred", "Передано"), "transferred", agg["transferred"], "#FEF3C7", "#B45309"),
                _cnt_chip(self.t("payments.chip.paid", "Сплачено"), "paid", agg["paid"], _OK_BG, _OK_T),
            ]

        _agg_chips_row.controls[:] = _build_agg_chip_list()
        self._counters_row = _agg_chips_row

        counters_bar = ft.Container(
            padding=ft.padding.only(left=2, right=2, top=0, bottom=4),
            content=_agg_chips_row,
        )

        # ── aggregate balance across all suppliers / currencies ───────────────
        stk_controls: list = [header_row, counters_bar]
        try:

            def _ccy_sym(c: str) -> str:
                return {
                    "USD": "$",
                    "UAH": "₴",
                    "EUR": "€",
                    "PLN": "zł",
                    "CNY": "¥",
                }.get(str(c or "").upper().strip(), str(c or ""))

            # Build supplier_id → currency map from self._suppliers.
            # If not loaded yet, skip the balance row silently.
            sup_ccy_map: dict[str, str] = {
                str(s.get("id") or "").strip(): self._supplier_currency(
                    str(s.get("id") or "").strip()
                )
                for s in (self._suppliers or [])
                if isinstance(s, dict) and str(s.get("id") or "").strip()
            }
            if sup_ccy_map:
                bal_by_ccy: dict[str, float] = {}
                debt_by_ccy: dict[str, float] = {}
                paid_by_ccy: dict[str, float] = {}
                for o in all_orders:
                    if not isinstance(o, dict):
                        continue
                    if (
                        filter_manager
                        and str(o.get("manager_id") or "").strip() != self.manager_id
                    ):
                        continue
                    if not self._is_order_in_scope(o):
                        continue
                    sid = str(o.get("supplier_id") or "").strip()
                    if not sid:
                        continue
                    ccy = sup_ccy_map.get(sid, "USD")
                    amt = self._order_amount_supplier_ccy(o, ccy)
                    paid = _safe_float(o.get("paid_amount"), 0.0) or 0.0
                    if amt is None:
                        amt = paid  # balance-neutral fallback
                    bal_by_ccy[ccy] = bal_by_ccy.get(ccy, 0.0) + float(amt) - paid
                    debt_by_ccy[ccy] = debt_by_ccy.get(ccy, 0.0) + float(amt)
                    paid_by_ccy[ccy] = paid_by_ccy.get(ccy, 0.0) + paid

                # Convert all to USD first.
                total_usd = 0.0
                for ccy, bal in bal_by_ccy.items():
                    rate = self._supplier_to_usd_rate(ccy)
                    if rate and rate > 0:
                        total_usd += bal * rate

                all_ccys = sorted(bal_by_ccy.keys())
                if all_ccys:
                    _net = (
                        total_usd
                        if total_usd
                        else (
                            bal_by_ccy.get(all_ccys[0], 0.0)
                            if len(all_ccys) == 1
                            else 0.0
                        )
                    )
                    if abs(_net) < 0.01:
                        _tot_bg, _tot_fg, _tot_hint = (
                            _OK_BG,
                            _OK_T,
                            self.t("payments.balance.zero", "Баланс нульовий"),
                        )
                    elif _net > 0:
                        _tot_bg, _tot_fg, _tot_hint = _WARN_BG, _WARN_T, self.t("payments.balance.we_owe", "Ми винні")
                    else:
                        _tot_bg, _tot_fg, _tot_hint = (
                            _ERR_BG,
                            _ERR_DARK,
                            self.t("payments.balance.they_owe", "Постачальники винні нам"),
                        )

                    _ABW = 132
                    _ABP = ft.padding.symmetric(horizontal=10, vertical=4)
                    _ABHA = ft.CrossAxisAlignment.CENTER

                    # ONE unified balance box (matching single-region style exactly)
                    non_usd_ccys = sorted(c for c in all_ccys if c != "USD")
                    usd_direct_bal = bal_by_ccy.get("USD", 0.0)
                    # Combined $ total: non-USD currencies converted + direct USD balance
                    total_combined_usd = usd_direct_bal
                    for _c in non_usd_ccys:
                        _r = self._supplier_to_usd_rate(_c)
                        if _r and _r > 0:
                            total_combined_usd += bal_by_ccy.get(_c, 0.0) * _r

                    bal_col_items = [
                        ft.Text(
                            self.t("payments.balance.label", "Баланс:"),
                            size=9,
                            weight=ft.FontWeight.W_700,
                            color=_MUTED,
                            text_align=ft.TextAlign.CENTER,
                        )
                    ]
                    if non_usd_ccys:
                        for _ccy in non_usd_ccys:
                            # Show total (all currencies combined) converted to this currency.
                            _r = self._supplier_to_usd_rate(_ccy)
                            _ccy_total = (
                                (total_combined_usd / _r)
                                if (_r and _r > 0)
                                else bal_by_ccy.get(_ccy, 0.0)
                            )
                            bal_col_items.append(
                                ft.Text(
                                    f"{_fmt_money(_ccy_total)} {_ccy_sym(_ccy)}",
                                    size=10,
                                    weight=ft.FontWeight.W_700,
                                    color=_tot_fg,
                                    text_align=ft.TextAlign.CENTER,
                                )
                            )
                        if abs(total_combined_usd) > 0.005:
                            bal_col_items.append(
                                ft.Text(
                                    f"≈ {_fmt_money(total_combined_usd)} $",
                                    size=10,
                                    color=_tot_fg,
                                    text_align=ft.TextAlign.CENTER,
                                )
                            )
                    else:
                        bal_col_items.append(
                            ft.Text(
                                f"{_fmt_money(usd_direct_bal)} $",
                                size=10,
                                weight=ft.FontWeight.W_700,
                                color=_tot_fg,
                                text_align=ft.TextAlign.CENTER,
                            )
                        )

                    bal_box = ft.Container(
                        width=_ABW,
                        padding=_ABP,
                        bgcolor=_tot_bg,
                        border_radius=8,
                        alignment=ft.alignment.Alignment(0, -1),
                        content=ft.Column(
                            bal_col_items,
                            spacing=2,
                            tight=True,
                            horizontal_alignment=_ABHA,
                        ),
                    )
                    boxes = [bal_box]

                    hint_box = ft.Container(
                        width=_ABW,
                        padding=_ABP,
                        bgcolor=_tot_bg,
                        border_radius=8,
                        alignment=ft.alignment.Alignment(0, -1),
                        content=ft.Text(
                            _tot_hint,
                            size=10,
                            weight=ft.FontWeight.W_700,
                            color=_tot_fg,
                            text_align=ft.TextAlign.CENTER,
                        ),
                    )

                    def _sum_chip(
                        label: str, by_ccy: dict, bg: str, fg: str
                    ) -> ft.Container:
                        # Convert everything to USD first, then display in all present currencies.
                        _chip_total_usd = sum(
                            by_ccy.get(_c, 0.0) * (self._supplier_to_usd_rate(_c) or 0)
                            for _c in by_ccy
                        )
                        _chip_non_usd = sorted(c for c in by_ccy if c != "USD")
                        col_items = [
                            ft.Text(
                                label,
                                size=9,
                                weight=ft.FontWeight.W_700,
                                color=_MUTED,
                                text_align=ft.TextAlign.CENTER,
                            )
                        ]
                        for _c in _chip_non_usd:
                            _r = self._supplier_to_usd_rate(_c)
                            _ctotal = (
                                (_chip_total_usd / _r)
                                if (_r and _r > 0)
                                else by_ccy.get(_c, 0.0)
                            )
                            col_items.append(
                                ft.Text(
                                    f"{_fmt_money(_ctotal)} {_ccy_sym(_c)}",
                                    size=10,
                                    weight=ft.FontWeight.W_700,
                                    color=fg,
                                    text_align=ft.TextAlign.CENTER,
                                )
                            )
                        # USD line: show total in $ (either direct USD or converted sum)
                        if _chip_non_usd and abs(_chip_total_usd) > 0.005:
                            col_items.append(
                                ft.Text(
                                    f"≈ {_fmt_money(_chip_total_usd)} $",
                                    size=10,
                                    color=fg,
                                    text_align=ft.TextAlign.CENTER,
                                )
                            )
                        elif not _chip_non_usd:
                            col_items.append(
                                ft.Text(
                                    f"{_fmt_money(by_ccy.get('USD', 0.0))} $",
                                    size=10,
                                    weight=ft.FontWeight.W_700,
                                    color=fg,
                                    text_align=ft.TextAlign.CENTER,
                                )
                            )
                        return ft.Container(
                            width=_ABW,
                            padding=_ABP,
                            bgcolor=bg,
                            border_radius=8,
                            alignment=ft.alignment.Alignment(0, -1),
                            content=ft.Column(
                                col_items,
                                spacing=2,
                                tight=True,
                                horizontal_alignment=_ABHA,
                            ),
                        )

                    debt_chip = _sum_chip(self.t("payments.stat.orders_sum", "Сума замовлень:"), debt_by_ccy, _ALT, _TEXT)
                    paid_chip = _sum_chip(self.t("payments.stat.paid_sum", "Сплачено:"), paid_by_ccy, _OK_BG, _OK_T)

                    bal_row = ft.Container(
                        padding=ft.padding.only(left=2, right=2, top=2, bottom=6),
                        border=ft.border.only(bottom=ft.BorderSide(1, _HDR)),
                        content=ft.Row(
                            [
                                ft.Text(
                                    self.t("payments.balance.total", "Загальний баланс"),
                                    size=11,
                                    weight=ft.FontWeight.W_700,
                                    color=_CHIP_T,
                                ),
                                *boxes,
                                hint_box,
                                ft.Container(width=12),
                                debt_chip,
                                paid_chip,
                            ],
                            spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                            wrap=True,
                            run_spacing=4,
                        ),
                    )
                    stk_controls.append(bal_row)
        except Exception:
            pass

        _stk.controls[:] = stk_controls
        with contextlib.suppress(Exception):
            _stk.update()

    def _update_balance_display(self) -> None:
        """Fast synchronous update of the overall balance row + counters from self._orders.
        Patches self._totals_row in place — no full tree rebuild needed."""
        tr = getattr(self, "_totals_row", None)
        if tr is None:
            return
        try:
            _eff_rid = self._effective_region_id()
            sup_orders: dict = {}
            for o in self._orders:
                if not isinstance(o, dict) or not self._is_order_in_scope(o):
                    continue
                if _eff_rid:
                    o_rid = str(o.get("region_id") or "").strip()
                    if o_rid and o_rid != _eff_rid:
                        continue
                sid = str(o.get("supplier_id") or "").strip()
                if sid:
                    sup_orders.setdefault(sid, []).append(o)

            totals_by_ccy: dict = {}
            debt_by_ccy: dict = {}
            paid_by_ccy: dict = {}
            total_usd = 0.0
            for sid, olist in sup_orders.items():
                ccy = self._supplier_currency(sid)
                debt_s = 0.0
                paid_s = 0.0
                for _o in olist:
                    _amt = self._order_amount_supplier_ccy(_o, ccy)
                    _paid = _safe_float(_o.get("paid_amount"), 0.0) or 0.0
                    if _amt is not None:
                        debt_s += float(_amt)
                        paid_s += _paid
                    else:
                        debt_s += _paid
                        paid_s += _paid
                sett_s = self._supplier_settlement_sum(sid, ccy)
                balance_s = (debt_s - paid_s) + sett_s
                totals_by_ccy[ccy] = totals_by_ccy.get(ccy, 0.0) + balance_s
                debt_by_ccy[ccy] = debt_by_ccy.get(ccy, 0.0) + debt_s
                paid_by_ccy[ccy] = paid_by_ccy.get(ccy, 0.0) + paid_s
                usd = self._supplier_amount_usd(balance_s, ccy)
                if usd is not None:
                    total_usd += float(usd)

            def _ccy_sym(c: str) -> str:
                return {
                    "USD": "$",
                    "UAH": "₴",
                    "EUR": "€",
                    "PLN": "zł",
                    "CNY": "¥",
                }.get(str(c or "").upper().strip(), str(c or ""))

            def _multiline_totals(by_ccy: dict) -> str:
                parts = [
                    f"{_fmt_money(a)} {_ccy_sym(c)}" for c, a in sorted(by_ccy.items())
                ]
                return " | ".join(parts) if parts else "0"

            _all_ccys = sorted(totals_by_ccy.keys())
            _net = (
                total_usd
                if total_usd
                else (list(totals_by_ccy.values())[0] if totals_by_ccy else 0.0)
            )
            if abs(_net) < 0.01:
                _tot_bg, _tot_fg, _tot_hint = _OK_BG, _OK_T, self.t("payments.balance.zero", "Баланс нульовий")
            elif _net > 0:
                _tot_bg, _tot_fg, _tot_hint = _WARN_BG, _WARN_T, self.t("payments.balance.we_owe", "Ми винні")
            else:
                _tot_bg, _tot_fg, _tot_hint = (
                    _ERR_BG,
                    _ERR_DARK,
                    self.t("payments.balance.they_owe", "Постачальники винні нам"),
                )

            _BCW = 132
            _BPAD = ft.padding.symmetric(horizontal=10, vertical=4)
            _BLBL = _MUTED

            def _bal_box(amount_str: str, sub_str: str = "") -> ft.Container:
                col = [
                    ft.Text(
                        self.t("payments.balance.label", "Баланс:"),
                        size=9,
                        weight=ft.FontWeight.W_700,
                        color=_BLBL,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        amount_str,
                        size=10,
                        weight=ft.FontWeight.W_700,
                        color=_tot_fg,
                        text_align=ft.TextAlign.CENTER,
                    ),
                ]
                if sub_str:
                    col.append(
                        ft.Text(
                            sub_str,
                            size=10,
                            color=_tot_fg,
                            text_align=ft.TextAlign.CENTER,
                        )
                    )
                return ft.Container(
                    width=_BCW,
                    padding=_BPAD,
                    bgcolor=_tot_bg,
                    border_radius=8,
                    alignment=ft.alignment.Alignment(0, 0),
                    content=ft.Column(
                        col,
                        spacing=2,
                        tight=True,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )

            def _make_bal_boxes(small: bool) -> list:
                if not _all_ccys:
                    return [_bal_box("0")]
                if len(_all_ccys) == 1:
                    ccy = _all_ccys[0]
                    sub = (
                        (f"≈ {_fmt_money(total_usd)} $")
                        if ccy != "USD" and total_usd
                        else ""
                    )
                    return [
                        _bal_box(
                            f"{_fmt_money(totals_by_ccy[ccy])} {_ccy_sym(ccy)}", sub
                        )
                    ]
                boxes = []
                for target_ccy in _all_ccys:
                    rate = self._supplier_to_usd_rate(target_ccy)
                    amt = (
                        (total_usd / rate)
                        if (rate and rate > 0 and total_usd)
                        else totals_by_ccy.get(target_ccy, 0.0)
                    )
                    boxes.append(_bal_box(f"{_fmt_money(amt)} {_ccy_sym(target_ccy)}"))
                return boxes

            def _hint_box() -> ft.Container:
                return ft.Container(
                    width=_BCW,
                    padding=_BPAD,
                    bgcolor=_tot_bg,
                    border_radius=8,
                    alignment=ft.alignment.Alignment(0, 0),
                    content=ft.Text(
                        _tot_hint,
                        size=10,
                        weight=ft.FontWeight.W_700,
                        color=_tot_fg,
                        text_align=ft.TextAlign.CENTER,
                    ),
                )

            # ── update scrollable totals row ──────────────────────────────────
            if tr is not None:
                row = tr.content
                if isinstance(row, ft.Row):
                    row.controls.clear()
                    row.controls.append(
                        ft.Text(
                            self.t("payments.balance.total", "Загальний баланс"),
                            size=12,
                            weight=ft.FontWeight.W_700,
                            color=_CHIP_T,
                        )
                    )
                    row.controls.extend(_make_bal_boxes(small=False))
                    row.controls.append(_hint_box())
                with contextlib.suppress(Exception):
                    tr.update()

            # ── update sticky totals bar ──────────────────────────────────────
            _stb_row = getattr(self, "_sticky_totals_bar_row_ref", None)
            if _stb_row is not None:
                _suffix = getattr(self, "_sticky_totals_bar_suffix_ref", [])
                _stb_row.controls.clear()
                _stb_row.controls.append(
                    ft.Text(
                        self.t("payments.balance.total", "Загальний баланс"),
                        size=11,
                        weight=ft.FontWeight.W_700,
                        color=_CHIP_T,
                    )
                )
                _stb_row.controls.extend(_make_bal_boxes(small=True))
                _stb_row.controls.append(_hint_box())
                _stb_row.controls.extend(_suffix)

            # Rebuild debt/paid chip columns: cross-convert all currencies to each
            # non-USD currency present, then show ≈ X $ as the last line.
            def _value_lines(by_ccy: dict, fg: str) -> list:
                _tot_usd = sum(
                    by_ccy.get(_c, 0.0) * (self._supplier_to_usd_rate(_c) or 0)
                    for _c in by_ccy
                )
                _non_usd = [c for c in sorted(by_ccy.keys()) if c != "USD"]
                items = []
                for _c in _non_usd:
                    _r = self._supplier_to_usd_rate(_c)
                    _ctotal = (
                        (_tot_usd / _r) if (_r and _r > 0) else by_ccy.get(_c, 0.0)
                    )
                    items.append(
                        ft.Text(
                            f"{_fmt_money(_ctotal)} {_ccy_sym(_c)}",
                            size=10,
                            weight=ft.FontWeight.W_700,
                            color=fg,
                            text_align=ft.TextAlign.CENTER,
                        )
                    )
                if _non_usd and abs(_tot_usd) > 0.005:
                    items.append(
                        ft.Text(
                            f"≈ {_fmt_money(_tot_usd)} $",
                            size=10,
                            color=fg,
                            text_align=ft.TextAlign.CENTER,
                        )
                    )
                elif not _non_usd:
                    items.append(
                        ft.Text(
                            f"{_fmt_money(by_ccy.get('USD', 0.0))} $",
                            size=10,
                            weight=ft.FontWeight.W_700,
                            color=fg,
                            text_align=ft.TextAlign.CENTER,
                        )
                    )
                return items or [
                    ft.Text("0", size=10, color=fg, text_align=ft.TextAlign.CENTER)
                ]

            _debt_col = getattr(self, "_totals_debt_chip_col", None)
            _paid_col = getattr(self, "_totals_paid_chip_col", None)
            _lbl_c = _MUTED
            _lbl_sz = 9
            if _debt_col is not None:
                _debt_col.controls[:] = [
                    ft.Text(
                        self.t("payments.stat.orders_sum", "Сума замовлень:"),
                        size=_lbl_sz,
                        weight=ft.FontWeight.W_700,
                        color=_lbl_c,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    *_value_lines(debt_by_ccy, _TEXT),
                ]
            if _paid_col is not None:
                _paid_col.controls[:] = [
                    ft.Text(
                        self.t("payments.stat.paid_sum", "Сплачено:"),
                        size=_lbl_sz,
                        weight=ft.FontWeight.W_700,
                        color=_lbl_c,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    *_value_lines(paid_by_ccy, _OK_T),
                ]
            _stb = getattr(self, "_sticky_totals_bar", None)
            if _stb is not None:
                with contextlib.suppress(Exception):
                    _stb.update()

        except Exception:
            pass
        with contextlib.suppress(Exception):
            self._update_counters()
        self._update_supplier_balance_refs()

    def _update_supplier_balance_refs(self) -> None:
        refs = getattr(self, "_supplier_balance_refs", {})
        if not refs:
            return

        # Only include USD conversion when FX cache is warm — avoids blocking HTTP call.
        _fx = getattr(self, "_fx_uah_cache", {})
        _fx_warm = (
            bool(_fx.get("ts")) and (time.time() - float(_fx.get("ts") or 0)) < 900
        )

        def _bal_label_upd(v: float) -> str:
            if abs(v) < 0.0001:
                return self.t("payments.balance.label", "Баланс:")
            return self.t("payments.balance.we_owe_short", "Баланс (ми винні):") if v > 0 else self.t("payments.balance.they_owe_full", "Баланс (постач. винен):")

        def _safe_money_upd(v: float) -> str:
            return _fmt_money(0.0 if abs(v) < 0.0001 else v)

        _any_changed = False
        for sid, container in refs.items():
            try:
                ccy = self._supplier_currency(sid)
                olist = [
                    o
                    for o in self._orders
                    if isinstance(o, dict)
                    and self._is_order_in_scope(o)
                    and str(o.get("supplier_id") or "").strip() == sid
                ]
                debt = 0.0
                paid = 0.0
                for _o in olist:
                    _amt = self._order_amount_supplier_ccy(_o, ccy)
                    _p = _safe_float(_o.get("paid_amount"), 0.0) or 0.0
                    if _amt is not None:
                        debt += float(_amt)
                        paid += _p
                    else:
                        debt += _p
                        paid += _p
                sett = self._supplier_settlement_sum(sid, ccy)
                balance = (debt - paid) + sett

                bg = (
                    _OK_BG
                    if abs(balance) < 0.0001
                    else (_WARN_BG if balance > 0 else _ERR_BG)
                )
                fg = (
                    _OK_T
                    if abs(balance) < 0.0001
                    else (_WARN_T if balance > 0 else _ERR_DARK)
                )
                # Value: amount only (no hint), hint moved to label
                bal_text = f"{_safe_money_upd(balance)} {ccy}".strip()
                usd_text = ""
                if _fx_warm and ccy != "USD":
                    bal_usd = self._supplier_amount_usd(
                        balance, ccy, order=(olist[0] if olist else None)
                    )
                    if bal_usd is not None:
                        usd_text = f"{_safe_money_upd(bal_usd)} $"

                container.bgcolor = bg

                def _usd_str_for(amount: float) -> str:
                    if ccy == "USD" or not _fx_warm:
                        return ""
                    v = self._supplier_amount_usd(
                        amount, ccy, order=(olist[0] if olist else None)
                    )
                    return f"{_safe_money_upd(v)} $" if v is not None else ""

                # Balance cell — {"main": Text, "usd": Text, "label": Text}
                _data = getattr(container, "data", None)
                if isinstance(_data, dict):
                    _mt = _data.get("main") or _data.get("bal")
                    _ut = _data.get("usd")
                    _lt = _data.get("label")
                    if isinstance(_lt, ft.Text):
                        _lt.value = _bal_label_upd(balance)
                    if isinstance(_mt, ft.Text):
                        _mt.value = bal_text
                        _mt.color = fg
                    if isinstance(_ut, ft.Text):
                        _ut.value = usd_text
                        _ut.color = fg
                        _ut.visible = bool(usd_text)
                elif isinstance(_data, ft.Text):
                    _data.value = bal_text
                    _data.color = fg
                elif isinstance(container.content, ft.Text):
                    container.content.value = bal_text
                    container.content.color = fg

                # Update "Сума Замовлень" cell
                debt_ct = self._supplier_debt_refs.get(sid)
                if debt_ct is not None:
                    _dd = getattr(debt_ct, "data", None)
                    if isinstance(_dd, dict):
                        _dmt = _dd.get("main")
                        _dut = _dd.get("usd")
                        if isinstance(_dmt, ft.Text):
                            _dmt.value = f"{_safe_money_upd(debt)} {ccy}"
                        if isinstance(_dut, ft.Text):
                            _duv = _usd_str_for(debt)
                            _dut.value = _duv
                            _dut.visible = bool(_duv)
                    elif isinstance(_dd, ft.Text):
                        _dd.value = f"{_safe_money_upd(debt)} {ccy}"
                    elif isinstance(debt_ct.content, ft.Text):
                        debt_ct.content.value = (
                            f"Сума Замовлень: {_safe_money_upd(debt)} {ccy}"
                        )

                # Update "Сплачено" cell
                paid_ct = self._supplier_paid_refs.get(sid)
                if paid_ct is not None:
                    _pd = getattr(paid_ct, "data", None)
                    if isinstance(_pd, dict):
                        _pmt = _pd.get("main")
                        _put = _pd.get("usd")
                        if isinstance(_pmt, ft.Text):
                            _pmt.value = f"{_safe_money_upd(paid)} {ccy}"
                        if isinstance(_put, ft.Text):
                            _puv = _usd_str_for(paid)
                            _put.value = _puv
                            _put.visible = bool(_puv)
                    elif isinstance(_pd, ft.Text):
                        _pd.value = f"{_safe_money_upd(paid)} {ccy}"
                    elif isinstance(paid_ct.content, ft.Text):
                        paid_ct.content.value = (
                            f"Сплачено: {_safe_money_upd(paid)} {ccy}"
                        )

                # Update "Не сплачених" cell
                unpaid_ct = getattr(self, "_supplier_unpaid_refs", {}).get(sid)
                if unpaid_ct is not None:
                    _unpaid_count = sum(
                        1
                        for _o in olist
                        if self._payment_bucket_key(_o.get("payment_status")) != "paid"
                    )
                    _upd_bg = _OK_BG if _unpaid_count == 0 else _ERR_BG
                    _upd_fg = _OK_T if _unpaid_count == 0 else _ERR
                    unpaid_ct.bgcolor = _upd_bg
                    _ud = getattr(unpaid_ct, "data", None)
                    if isinstance(_ud, dict):
                        _uv = _ud.get("val")
                        if isinstance(_uv, ft.Text):
                            _uv.value = str(_unpaid_count)
                            _uv.color = _upd_fg

                # Update "Замовлень" cell
                count_ct = getattr(self, "_supplier_count_refs", {}).get(sid)
                if count_ct is not None:
                    _cd = getattr(count_ct, "data", None)
                    if isinstance(_cd, dict):
                        _cv = _cd.get("val")
                        if isinstance(_cv, ft.Text):
                            _cv.value = str(len(olist))

                _any_changed = True
            except Exception:
                pass

        # One page.update() instead of N individual container.update() calls.
        if _any_changed:
            _p = self._page()
            if _p:
                with contextlib.suppress(Exception):
                    _p.update()

    def _store_supplier_balance_ref(
        self, supplier_id: str, container: ft.Container
    ) -> ft.Container:
        self._supplier_balance_refs[supplier_id] = container
        return container

    def _store_supplier_debt_ref(
        self, supplier_id: str, container: ft.Container
    ) -> ft.Container:
        self._supplier_debt_refs[supplier_id] = container
        return container

    def _store_supplier_paid_ref(
        self, supplier_id: str, container: ft.Container
    ) -> ft.Container:
        self._supplier_paid_refs[supplier_id] = container
        return container

    # ────────────────────────────── data loading ─────────────────────────────

    def _allowed_region_ids(self) -> set[str]:
        active_token = str(self.active_region or "").strip()
        if not active_token:
            return set()
        allowed = set()
        for r in self._regions_data:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id") or "").strip()
            rname = str(r.get("name") or "").strip()
            if active_token and (active_token == rid or active_token == rname):
                if rid:
                    allowed.add(rid)
        if active_token:
            allowed.add(active_token)
        return allowed

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

    def _load_lookups(self) -> None:
        try:
            # General mode: load ALL suppliers so every manager's supplier currencies
            # and names are available for the aggregate balance/sticky calculations.
            _sup_mid = None if self._general_mode else self.manager_id
            self._suppliers = (
                self.db.get_suppliers(self.company_id, None, manager_id=_sup_mid) or []
            )
        except Exception:
            self._suppliers = []
        self._set_loading_text(self.t("payments.loading.suppliers", "Постачальники: {n}").format(n=len(self._suppliers)))
        try:
            # Always load products from ALL regions so visible_product_ids
            # covers the full manager scope — payments balance must be region-agnostic.
            self._products = (
                self.db.get_products(
                    self.company_id,
                    None,  # no region filter: need all products for correct order matching
                    manager_id=self.manager_id,
                )
                or []
            )
        except Exception:
            self._products = []
        self._set_loading_text(self.t("payments.loading.products", "Товари: {n}").format(n=len(self._products)))
        try:
            rid_eff = str(self.region_id or "").strip()
            self._delivery_methods = (
                self.db.get_effective_delivery_methods_for_manager(
                    self.company_id, self.manager_id, rid_eff or None
                )
                or []
            )
        except Exception:
            self._delivery_methods = []
        self._set_loading_text(self.t("payments.loading.orders_ellipsis", "Замовлення..."))
        # Do NOT filter self._suppliers by region — suppliers without region_id
        # or with a mismatched region_id would disappear from the name lookup,
        # causing their IDs to display instead of names. Orders are already
        # scoped by region via _is_order_in_scope, so the supplier list is safe unfiltered.

    def _load_data_from_db(self) -> None:
        try:
            visible_product_ids = [
                str((p or {}).get("id") or "").strip()
                for p in (self._products or [])
                if isinstance(p, dict) and (p.get("id") or "").strip()
            ] or None
            # Payments balance is region-agnostic: always load ALL orders across
            # all regions so the supplier totals are consistent regardless of which
            # region button is active in the dashboard header.
            if self._general_mode and self._general_manager_ids:
                combined: list = []
                for mid in self._general_manager_ids:
                    try:
                        mgr_orders = (
                            self.db.get_orders(
                                self.company_id,
                                None,  # all regions
                                manager_id=mid,
                                force_refresh=True,
                                visible_product_ids=visible_product_ids,
                            )
                            or []
                        )
                        mgr_name = (self._general_manager_names or {}).get(mid, mid)
                        for o in mgr_orders:
                            if isinstance(o, dict):
                                o["_gm_manager_id"] = mid
                                o["_gm_manager_name"] = mgr_name
                        combined.extend(o for o in mgr_orders if isinstance(o, dict))
                    except Exception:
                        pass
                self._orders = combined
            else:
                self._orders = (
                    self.db.get_orders(
                        self.company_id,
                        None,  # all regions — balance must not depend on region filter
                        manager_id=self.manager_id,
                        force_refresh=True,
                        visible_product_ids=visible_product_ids,
                    )
                    or []
                )
        except Exception:
            self._orders = []
        self._set_loading_text(self.t("payments.loading.orders", "Замовлення: {n}").format(n=len(self._orders)))
        try:
            # Load settlements from all regions (balance is region-agnostic)
            self._settlements = (
                self.db.get_supplier_settlements(
                    self.company_id,
                    None,  # all regions
                    manager_id=self.manager_id,
                    force_refresh=True,
                )
                or []
            )
        except Exception:
            self._settlements = []
        self._set_loading_text(self.t("payments.loading.settlements", "Розрахунки: {n}").format(n=len(self._settlements)))
        # Do NOT filter orders/settlements by region — the balance with a supplier
        # is the total across ALL regions, not region-specific.  Region selection
        # affects which SUPPLIERS are shown (via _load_lookups supplier filter), not
        # the order amounts used to compute the balance.
        self._normalize_payment_status(self._orders)
        self._sync_to_shared_map()

    def _merge_outbox_into_data(self) -> None:
        for key, snap in list(self._outbox.items()):
            if not isinstance(snap, dict):
                continue
            if key.startswith("order:"):
                oid = str(snap.get("id") or "").strip()
                if not oid:
                    continue
                idx = next(
                    (
                        i
                        for i, o in enumerate(self._orders)
                        if str((o or {}).get("id") or "").strip() == oid
                    ),
                    None,
                )
                if idx is not None:
                    self._orders[idx] = snap
                else:
                    self._orders.append(snap)
                self._sync_order_to_shared_map(oid, snap)
            elif key.startswith("order_patch:"):
                # Patch only specific fields (e.g. paid_amount, transferred_amount)
                oid = key[len("order_patch:") :].strip()
                if not oid:
                    continue
                idx = next(
                    (
                        i
                        for i, o in enumerate(self._orders)
                        if str((o or {}).get("id") or "").strip() == oid
                    ),
                    None,
                )
                if idx is not None:
                    self._orders[idx].update(snap)
                    self._sync_order_to_shared_map(oid, self._orders[idx])
            elif key.startswith("settlement:"):
                sid = str(snap.get("id") or "").strip()
                if not sid:
                    continue
                idx = next(
                    (
                        i
                        for i, s in enumerate(self._settlements)
                        if str((s or {}).get("id") or "").strip() == sid
                    ),
                    None,
                )
                if idx is not None:
                    self._settlements[idx] = snap
                else:
                    self._settlements.append(snap)
            elif key.startswith("settlement_delete:"):
                sid = str(snap.get("id") or key[len("settlement_delete:") :]).strip()
                if sid:
                    self._settlements = [
                        s
                        for s in self._settlements
                        if str((s or {}).get("id") or "").strip() != sid
                    ]

    # ───────────────────────────── counters ──────────────────────────────────

    def _compute_counters(self) -> dict:
        counters = {"unpaid": 0, "advance": 0, "transferred": 0, "paid": 0}
        _eff_rid = self._effective_region_id()
        for o in self._orders or []:
            if not isinstance(o, dict):
                continue
            if not self._is_order_in_scope(o):
                continue
            if _eff_rid:
                o_rid = str(o.get("region_id") or "").strip()
                if o_rid and o_rid != _eff_rid:
                    continue
            if not str(o.get("supplier_id") or "").strip():
                continue
            bk = self._payment_bucket_key(o.get("payment_status"))
            counters[bk] = int(counters.get(bk) or 0) + 1
        return counters

    def _update_counters(self) -> None:
        if self._counters_row is None:
            return
        counters = self._compute_counters()
        total = sum(counters.values())
        active_filter = self._counter_payment_filter

        def _chip(label: str, key, count: int, bg: str, fg: str):
            active = (key is None and not active_filter) or (
                key is not None and key in active_filter
            )

            def _click(_e, _k=key):
                if _k is None:
                    self._counter_payment_filter = set()
                elif _k in self._counter_payment_filter:
                    self._counter_payment_filter.discard(_k)
                else:
                    self._counter_payment_filter = {_k}
                self._update_counters()
                self._schedule_rerender()

            return ft.Container(
                bgcolor=_HDR if active else bg,
                border=ft.border.all(1, _TEXT_DARK if active else "#00000000"),
                border_radius=6,
                padding=ft.padding.symmetric(horizontal=6, vertical=2),
                ink=True,
                on_click=_click,
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

        def _sep():
            return ft.Container(
                width=1,
                height=20,
                bgcolor=_BORDER,
                margin=ft.margin.symmetric(horizontal=4),
            )

        self._counters_row.controls[:] = [
            _chip(self.t("payments.chip.all", "Всі"), None, total, _HDR, _TEXT_DARK),
            _sep(),
            _chip(self.t("orders.payment.unpaid", "Не сплачен."), "unpaid", counters.get("unpaid", 0), _ERR_BG, _ERR),
            _chip(self.t("orders.payment.advance", "Аванс"), "advance", counters.get("advance", 0), "#FED7AA", _WARN_T),
            _chip(self.t("orders.payment.transferred", "Передано"), "transferred", counters.get("transferred", 0), "#FEF3C7", "#B45309"),
            _chip(self.t("payments.chip.paid", "Сплачено"), "paid", counters.get("paid", 0), _OK_BG, _OK_T),
        ]
        with contextlib.suppress(Exception):
            self._counters_row.update()

    # ───────────────────────── table layout dialog ───────────────────────────

    def _open_table_layout_dialog(self) -> None:
        page = self._page()
        if not page:
            return

        base_defs = {
            "invoice": 60,
            "order1c": 60,
            "status": 100,
            "amount": 80,
            "amount_usd": 80,
            "payment": 100,
            "transferred": 90,
            "paid": 100,
            "product": 250,
            "comment": 180,
            "delivery": 80,
            "unit_cost": 60,
            "expand": 80,
        }
        key_labels = {
            "invoice": "Інвойс",
            "order1c": self.t("payments.col.order1c", "ЗП обліку"),
            "status": self.t("orders.field.status", "Статус"),
            "amount": self.t("payments.col.amount", "Вартість"),
            "amount_usd": self.t("payments.col.amount_usd", "Вартість $"),
            "payment": self.t("payments.col.payment", "Сплата"),
            "transferred": self.t("orders.payment.transferred", "Передано"),
            "paid": self.t("payments.col.paid", "Сплачено"),
            "product": self.t("orders.filter.product", "Товар"),
            "comment": self.t("orders.field.comment", "Коментар"),
            "delivery": self.t("orders.field.delivery_type", "Тип доставки"),
            "unit_cost": self.t("payments.col.unit_cost", "С/В"),
            "expand": self.t("payments.col.expand", "Розкриття"),
        }

        row_h_f = ft.TextField(
            label=self.t("payments.dlg.row_height", "Висота рядка, px"),
            value=str(int(self._table_row_height)),
            width=180,
            text_size=11,
        )
        _dfm_month_names = {
            1: "Січень", 2: "Лютий", 3: "Березень", 4: "Квітень",
            5: "Травень", 6: "Червень", 7: "Липень", 8: "Серпень",
            9: "Вересень", 10: "Жовтень", 11: "Листопад", 12: "Грудень",
        }
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
            return f"{_dfm_month_names.get(mm, m[5:7])} {m[:4]}"

        display_from_dd = ft.Dropdown(
            label=self.t(
                "payments.dlg.display_from_month", "Перший місяць відображення"
            ),
            value=(self._display_from_month if self._display_from_month in _dfm_months else ""),
            options=[
                ft.dropdown.Option("", self.t("payments.dlg.show_all_history", "Показати всю історію"))
            ]
            + [ft.dropdown.Option(m, _dfm_label(m)) for m in _dfm_months],
            width=320,
            text_size=11,
        )

        all_keys = [k for k in self._all_column_keys() if k != "unit_cost"]
        visible_keys_set = self._visible_column_keys_set()

        vis_rows = []
        for key in all_keys:
            cb = ft.Checkbox(
                label=key_labels.get(key, key),
                value=(key in visible_keys_set),
            )
            vis_rows.append((key, cb))

        width_rows = []
        for key in all_keys + ["expand"]:
            wv = int(
                (self._table_col_width_overrides or {}).get(key)
                or base_defs.get(key)
                or 80
            )
            fld = ft.TextField(
                label=f"{key_labels.get(key, key)} ({key}), px",
                value=str(wv),
                width=240,
                text_size=11,
            )
            width_rows.append((key, fld))

        def _close(_=None):
            with contextlib.suppress(Exception):
                page.close(dialog)

        def _save(_=None):
            rh = _safe_float(row_h_f.value, None)
            if rh is not None:
                self._table_row_height = int(max(26, min(72, int(rh))))
            ovs = {}
            for key, fld in width_rows:
                w = _safe_float(fld.value, None)
                if w is None:
                    continue
                ovs[key] = int(max(35, min(900, int(w))))
            self._table_col_width_overrides = ovs
            self._display_from_month = str(display_from_dd.value or "").strip()
            chosen = [key for key, cb in vis_rows if bool(getattr(cb, "value", True))]
            self._set_visible_column_keys(chosen)
            self._visible_column_keys_cache = set(chosen) if chosen else set(all_keys)
            self._save_table_layout_state()
            _close()
            self._restart_tab()

        vis_col = ft.Column(
            [cb for _, cb in vis_rows],
            spacing=4,
            scroll=ft.ScrollMode.ADAPTIVE,
            height=160,
        )
        widths_col = ft.Column(
            [f for _, f in width_rows],
            spacing=6,
            scroll=ft.ScrollMode.ADAPTIVE,
            height=320,
        )
        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(self.t("payments.dlg.table_title", "Таблиця")),
            content=ft.Container(
                width=520,
                content=ft.Column(
                    [
                        ft.Text(self.t("orders.col_settings.row_height", "Висота рядка"), size=11, weight="bold"),
                        row_h_f,
                        ft.Divider(height=8),
                        display_from_dd,
                        ft.Divider(height=8),
                        ft.Text(self.t("orders.section.visible_columns", "Видимі колонки"), size=11, weight="bold"),
                        vis_col,
                        ft.Divider(height=8),
                        ft.Text(self.t("payments.dlg.col_widths", "Ширини стовпців, px"), size=11, weight="bold"),
                        widths_col,
                    ],
                    spacing=8,
                ),
            ),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_close),
                ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dialog)

    # ─────────────────────────── async load flow ─────────────────────────────

    def _sync_build_tree(self) -> list:
        """Build the full widget tree synchronously — called via asyncio.to_thread
        so it never blocks the event loop (including any HTTP FX-rate fetch)."""
        self._supplier_balance_refs = {}
        self._supplier_debt_refs = {}
        self._supplier_paid_refs = {}
        self._supplier_unpaid_refs = {}
        self._supplier_count_refs = {}
        self._supplier_section_refs = {}
        self._supplier_expand_callbacks = {}

        # In general mode: group orders by manager → supplier, then build manager sections
        if self._general_mode and self._general_manager_ids:
            return self._sync_build_tree_general()

        _eff_rid = self._effective_region_id()  # None → all regions
        sup_orders_all: dict = {}
        for o in self._orders:
            if not isinstance(o, dict):
                continue
            if not self._is_order_in_scope(o):
                continue
            if _eff_rid:
                o_rid = str(o.get("region_id") or "").strip()
                if o_rid and o_rid != _eff_rid:
                    continue
            sid = str(o.get("supplier_id") or "").strip()
            if not sid:
                continue
            sup_orders_all.setdefault(sid, []).append(o)

        sup_orders: dict = {}
        if self._counter_payment_filter:
            for sid, olist in sup_orders_all.items():
                picked = [
                    o
                    for o in olist
                    if self._payment_bucket_key(o.get("payment_status"))
                    in self._counter_payment_filter
                ]
                if picked:
                    sup_orders[sid] = picked
        else:
            sup_orders = sup_orders_all

        # Ctrl+F row filter: narrow each supplier's orders to those matching the
        # query across Інвойс/ЗП обліку/Кільк./Вартість/Вартість $/Передано/Сплачено/Товар;
        # suppliers with zero matches are dropped entirely (same shape as the
        # payment-bucket filter above).
        if self._search_text_query:
            q = self._search_text_query
            text_narrowed: dict = {}
            for sid, olist in sup_orders.items():
                ccy = self._supplier_currency(sid)
                picked = []
                for o in olist:
                    items = o.get("items") if isinstance(o.get("items"), list) else []
                    if not items:
                        items = [o]
                    qtys = [it.get("qty") or it.get("quantity") for it in items]
                    pnames = [it.get("product_name") for it in items]
                    amt = self._order_amount_supplier_ccy(o, ccy)
                    amt_usd = (
                        self._supplier_amount_usd(amt, ccy, order=o)
                        if amt is not None
                        else None
                    )
                    if row_text_matches(
                        q,
                        o.get("invoice_no"),
                        o.get("order_no_1c"),
                        *qtys,
                        amt,
                        amt_usd,
                        o.get("transferred_amount"),
                        o.get("paid_amount"),
                        *pnames,
                    ):
                        picked.append(o)
                if picked:
                    text_narrowed[sid] = picked
            sup_orders = text_narrowed
            self._show_all_suppliers |= set(sup_orders.keys())

        # quick_filter="supplier_debt": keep only suppliers where balance > 0 (we owe).
        if self._filter_debt_only:
            debt_sup: dict = {}
            for sid, olist in sup_orders.items():
                ccy = self._supplier_currency(sid)
                debt = 0.0
                paid = 0.0
                for _o in olist:
                    _amt = self._order_amount_supplier_ccy(_o, ccy)
                    _p = float(_safe_float(_o.get("paid_amount"), 0.0) or 0)
                    if _amt is not None:
                        debt += float(_amt)
                        paid += _p
                    else:
                        debt += _p
                        paid += _p
                sett = self._supplier_settlement_sum(sid, ccy)
                if (debt - paid) + sett > 0.0001:
                    debt_sup[sid] = olist
            sup_orders = debt_sup

        supplier_name_by_id = {
            str(s.get("id") or "").strip(): str(s.get("name") or "").strip()
            for s in (self._suppliers or [])
            if isinstance(s, dict)
        }

        import concurrent.futures as _cf

        _sorted_items = sorted(
            sup_orders.items(),
            key=lambda kv: (supplier_name_by_id.get(kv[0]) or kv[0]).lower(),
        )

        sections = []
        # When a status filter is active — build ONE flat combined table instead of per-supplier sections.
        use_flat = bool(self._counter_payment_filter) and len(_sorted_items) > 0
        if use_flat:
            flat_results = []
            for sid, olist in _sorted_items:
                sname = supplier_name_by_id.get(sid) or sid
                flat_results.append(
                    self._build_supplier_section(sid, sname, olist, flat_mode=True)
                )
            if flat_results:
                first = flat_results[0]
                w_final = first["w_final"]
                # Combined rows: header once, then per-supplier label + rows
                combined: list = []
                combined.append(first["header_row"])
                for fr in flat_results:
                    rh = fr["rows_host"]
                    # rh.controls[0] is the header_row — skip it; rest are data rows
                    sname_label = ft.Container(
                        bgcolor=_SOFT_I,
                        border=ft.border.only(bottom=ft.BorderSide(1, _CHIP_BG)),
                        padding=ft.padding.symmetric(horizontal=8, vertical=3),
                        content=ft.Text(
                            fr["supplier_name"],
                            size=10,
                            weight=ft.FontWeight.W_700,
                            color=_INDIGO,
                        ),
                    )
                    combined.append(sname_label)
                    combined.extend(rh.controls[1:])  # skip header_row, add data rows
                flat_section = self._wrap_flat_combined(combined, w_final)
                sections = [flat_section]
        elif len(_sorted_items) <= 1:
            for sid, olist in _sorted_items:
                sname = supplier_name_by_id.get(sid) or sid
                section = self._build_supplier_section(sid, sname, olist)
                sections.append(section)
                self._supplier_section_refs[sid] = section
        else:
            _args = [
                (sid, supplier_name_by_id.get(sid) or sid, olist)
                for sid, olist in _sorted_items
            ]

            def _build_one_section(args):
                _sid, _sname, _olist = args
                return _sid, self._build_supplier_section(_sid, _sname, _olist)

            _results: dict = {}
            _max_w = min(len(_args), 8)
            with _cf.ThreadPoolExecutor(max_workers=_max_w) as _pool:
                for _sid, _sect in _pool.map(_build_one_section, _args):
                    _results[_sid] = _sect

            for sid, _ in _sorted_items:
                if sid in _results:
                    sections.append(_results[sid])
                    self._supplier_section_refs[sid] = _results[sid]

        totals_by_ccy: dict = {}
        total_usd = 0.0
        for sid, olist in sup_orders.items():
            ccy = self._supplier_currency(sid)
            debt_s = 0.0
            paid_s = 0.0
            for o in olist:
                amt = self._order_amount_supplier_ccy(o, ccy)
                paid_o = _safe_float(o.get("paid_amount"), 0.0) or 0.0
                if amt is not None:
                    debt_s += float(amt)
                    paid_s += paid_o
                else:
                    debt_s += paid_o
                    paid_s += paid_o
            sett_s = self._supplier_settlement_sum(sid, ccy)
            balance_s = (debt_s - paid_s) + sett_s
            totals_by_ccy[ccy] = totals_by_ccy.get(ccy, 0.0) + balance_s
            usd = self._supplier_amount_usd(balance_s, ccy)
            if usd is not None:
                total_usd += float(usd)

        def _ccy_sym(c: str) -> str:
            return {"USD": "$", "UAH": "₴", "EUR": "€", "PLN": "zł", "CNY": "¥"}.get(
                str(c or "").upper().strip(), str(c or "")
            )

        _all_ccys = sorted(totals_by_ccy.keys())
        _net = (
            total_usd
            if total_usd
            else (list(totals_by_ccy.values())[0] if totals_by_ccy else 0.0)
        )
        if abs(_net) < 0.01:
            _tot_bg, _tot_fg, _tot_hint = _OK_BG, _OK_T, self.t("payments.balance.zero", "Баланс нульовий")
        elif _net > 0:
            _tot_bg, _tot_fg, _tot_hint = _WARN_BG, _WARN_T, self.t("payments.balance.we_owe", "Ми винні")
        else:
            _tot_bg, _tot_fg, _tot_hint = (
                _ERR_BG,
                _ERR_DARK,
                self.t("payments.balance.they_owe", "Постачальники винні нам"),
            )

        _SCW = 132
        _SPD = ft.padding.symmetric(horizontal=10, vertical=4)
        _SLC = _MUTED
        _SA = ft.alignment.Alignment(0, 0)
        _SHA = ft.CrossAxisAlignment.CENTER

        def _bal_box_s(amount_str: str, sub_str: str = "") -> ft.Container:
            col = [
                ft.Text(
                    "Баланс:",
                    size=9,
                    weight=ft.FontWeight.W_700,
                    color=_SLC,
                    text_align=ft.TextAlign.CENTER,
                ),
                ft.Text(
                    amount_str,
                    size=10,
                    weight=ft.FontWeight.W_700,
                    color=_tot_fg,
                    text_align=ft.TextAlign.CENTER,
                ),
            ]
            if sub_str:
                col.append(
                    ft.Text(
                        sub_str, size=11, color=_tot_fg, text_align=ft.TextAlign.CENTER
                    )
                )
            return ft.Container(
                width=_SCW,
                padding=_SPD,
                bgcolor=_tot_bg,
                border_radius=8,
                alignment=_SA,
                content=ft.Column(
                    col, spacing=2, tight=True, horizontal_alignment=_SHA
                ),
            )

        def _make_bal_boxes(small: bool) -> list:
            if not _all_ccys:
                return [_bal_box_s("0")]
            if len(_all_ccys) == 1:
                ccy = _all_ccys[0]
                sub = (
                    (f"≈ {_fmt_money(total_usd)} $")
                    if ccy != "USD" and total_usd
                    else ""
                )
                return [
                    _bal_box_s(f"{_fmt_money(totals_by_ccy[ccy])} {_ccy_sym(ccy)}", sub)
                ]
            boxes = []
            for target_ccy in _all_ccys:
                rate = self._supplier_to_usd_rate(target_ccy)
                amt = (
                    (total_usd / rate)
                    if (rate and rate > 0 and total_usd)
                    else totals_by_ccy.get(target_ccy, 0.0)
                )
                boxes.append(_bal_box_s(f"{_fmt_money(amt)} {_ccy_sym(target_ccy)}"))
            return boxes

        totals_row = ft.Container(
            content=ft.Row(
                [
                    ft.Text(
                        self.t("payments.balance.total", "Загальний баланс"),
                        size=12,
                        weight=ft.FontWeight.W_700,
                        color=_CHIP_T,
                    ),
                    *_make_bal_boxes(small=False),
                    ft.Container(
                        width=_SCW,
                        padding=_SPD,
                        bgcolor=_tot_bg,
                        border_radius=8,
                        alignment=_SA,
                        content=ft.Text(
                            _tot_hint,
                            size=10,
                            weight=ft.FontWeight.W_700,
                            color=_tot_fg,
                            text_align=ft.TextAlign.CENTER,
                        ),
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                wrap=True,
            ),
            padding=ft.padding.symmetric(horizontal=6, vertical=8),
            border=ft.border.only(bottom=ft.BorderSide(1, _HDR)),
            margin=ft.margin.only(bottom=6),
        )

        # Keep ref for _update_balance_display (updates the sticky bar values)
        # but do NOT add totals_row to content_area — it lives in the sticky header.
        self._totals_row = totals_row
        return (
            sections
            if sections
            else [
                ft.Container(
                    content=ft.Text(
                        self.t("payments.no_orders_to_show", "Немає замовлень для відображення"),
                        size=12,
                        color=_TEXT,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    padding=ft.padding.all(24),
                    alignment=ft.alignment.Alignment(0, 0),
                )
            ]
        )

    def _sync_build_tree_general(self) -> list:
        """Build widget tree for 'Загальні' mode.
        Each manager is a collapsible section (collapsed by default).
        Inside each manager section: supplier sub-sections (read-only)."""
        import concurrent.futures as _cf
        import contextlib as _cl

        # Group in-scope orders by manager
        mgr_orders_map: dict[str, list] = {}
        for o in self._orders:
            if not isinstance(o, dict) or not self._is_order_in_scope(o):
                continue
            mid = str(o.get("_gm_manager_id") or o.get("manager_id") or "").strip()
            mgr_orders_map.setdefault(mid, []).append(o)

        supplier_name_by_id = {
            str(s.get("id") or "").strip(): str(s.get("name") or "").strip()
            for s in (self._suppliers or [])
            if isinstance(s, dict)
        }

        sections_all: list = []

        for mid in self._general_manager_ids:
            mgr_orders = mgr_orders_map.get(mid, [])
            mgr_name = (self._general_manager_names or {}).get(mid, mid)
            order_count = len({str(o.get("id") or id(o)) for o in mgr_orders})

            # Build supplier sub-sections (lazily, inside the body column)
            sup_orders_map: dict = {}
            for o in mgr_orders:
                sid = str(o.get("supplier_id") or "").strip()
                if sid:
                    sup_orders_map.setdefault(sid, []).append(o)

            _sorted = sorted(
                sup_orders_map.items(),
                key=lambda kv: (supplier_name_by_id.get(kv[0]) or kv[0]).lower(),
            )

            # Build supplier sections in a thread pool
            sup_sections: list = []
            if _sorted:
                _args = [
                    (sid, supplier_name_by_id.get(sid) or sid, olist)
                    for sid, olist in _sorted
                ]
                if len(_args) == 1:
                    sid, sname, olist = _args[0]
                    sup_sections.append(self._build_supplier_section(sid, sname, olist))
                else:

                    def _build_one(args, _self=self):
                        _s, _n, _ol = args
                        return _s, _self._build_supplier_section(_s, _n, _ol)

                    _res: dict = {}
                    with _cf.ThreadPoolExecutor(max_workers=min(len(_args), 4)) as pool:
                        for _sid, _sect in pool.map(_build_one, _args):
                            _res[_sid] = _sect
                    for sid, _ in _sorted:
                        if sid in _res:
                            sup_sections.append(_res[sid])

            body_col = ft.Column(
                sup_sections
                or [
                    ft.Container(
                        content=ft.Text(self.t("orders.empty_state", "Немає замовлень"), size=11, color=_MUTED),
                        padding=ft.padding.symmetric(horizontal=16, vertical=8),
                    )
                ],
                spacing=4,
                visible=False,
            )  # collapsed by default

            arrow_icon = ft.Icon(ft.Icons.EXPAND_MORE, size=18, color=_INDIGO_DK)
            order_badge = (
                ft.Container(
                    bgcolor=_CHIP_BG,
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

            header_row = ft.Container(
                bgcolor=_SOFT_I,
                border=ft.border.all(1, _CHIP_BG),
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
                            color=_INDIGO_DK,
                            expand=True,
                        ),
                        order_badge,
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            )

            mgr_section = ft.Column(
                [header_row, body_col],
                spacing=0,
            )
            sections_all.append(
                ft.Container(
                    content=mgr_section,
                    margin=ft.margin.only(bottom=6),
                )
            )

        if not sections_all:
            return [
                ft.Container(
                    content=ft.Text(
                        self.t("payments.no_orders_to_show", "Немає замовлень для відображення"),
                        size=12,
                        color=_TEXT,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    padding=ft.padding.all(24),
                )
            ]
        return sections_all

    # ─────────────────── aggregate flat-table (all modes) ────────────────────

    def _rebuild_agg_content(self) -> None:
        """Called when an aggregate status chip is clicked. Toggles flat table on/off."""
        if not self._counter_payment_filter:
            # Restore the normal collapsible-sections view.
            try:
                self.build()
            except Exception:
                pass
            return
        # Ensure supplier names are loaded for the flat table.
        if not self._suppliers:
            try:
                if self._general_mode:
                    self._suppliers = self.db.get_suppliers(self.company_id, None) or []
                else:
                    self._suppliers = (
                        self.db.get_suppliers(
                            self.company_id, None, manager_id=self.manager_id
                        )
                        or []
                    )
            except Exception:
                pass
        flat_controls = self._build_agg_flat_sections()
        # Preserve any top-level non-section controls (e.g. "Таблиця" button row).
        preserved: list = []
        for c in list(self.content_area.controls):
            if isinstance(c, ft.Column):
                break
            preserved.append(c)
        with contextlib.suppress(Exception):
            self.content_area.scroll = ft.ScrollMode.ADAPTIVE
        self.content_area.controls[:] = preserved + flat_controls
        with contextlib.suppress(Exception):
            self.content_area.update()

    def _build_agg_flat_sections(self) -> list:
        """Build a flat combined table from _agg_orders_cache filtered by _counter_payment_filter."""
        from collections import defaultdict

        orders_cache = list(getattr(self, "_agg_orders_cache", []) or [])
        active_filter = self._counter_payment_filter
        is_general = self._general_mode and bool(self._general_manager_ids)

        # Filter by scope and payment status.
        filtered: list = []
        for o in orders_cache:
            if not isinstance(o, dict):
                continue
            if not self._is_order_in_scope(o):
                continue
            if self._payment_bucket_key(o.get("payment_status")) not in active_filter:
                continue
            # For User+All Regions: restrict to current manager.
            if not is_general:
                mid = str(o.get("_gm_manager_id") or o.get("manager_id") or "").strip()
                if mid and mid != self.manager_id:
                    continue
            if self._search_text_query:
                ccy = self._supplier_currency(str(o.get("supplier_id") or "").strip())
                items = o.get("items") if isinstance(o.get("items"), list) else []
                if not items:
                    items = [o]
                amt = self._order_amount_supplier_ccy(o, ccy)
                amt_usd = (
                    self._supplier_amount_usd(amt, ccy, order=o)
                    if amt is not None
                    else None
                )
                if not row_text_matches(
                    self._search_text_query,
                    o.get("invoice_no"),
                    o.get("order_no_1c"),
                    *[it.get("qty") or it.get("quantity") for it in items],
                    amt,
                    amt_usd,
                    o.get("transferred_amount"),
                    o.get("paid_amount"),
                    *[it.get("product_name") for it in items],
                ):
                    continue
            filtered.append(o)

        if not filtered:
            return [
                ft.Container(
                    content=ft.Text(
                        self.t("payments.no_orders_with_status", "Немає замовлень з таким статусом"),
                        size=12,
                        color=_MUTED,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    padding=ft.padding.all(24),
                )
            ]

        sup_name_by_id: dict[str, str] = {
            str(s.get("id") or "").strip(): str(s.get("name") or "").strip()
            for s in (self._suppliers or [])
            if isinstance(s, dict)
        }
        rid_to_name: dict[str, str] = {
            str(r.get("id") or "").strip(): str(r.get("name") or "").strip()
            for r in (self._regions_data or [])
            if isinstance(r, dict)
        }
        active_rid = self._effective_region_id()

        if is_general and active_rid:
            # General + Region: manager → supplier
            mid_sid: dict = defaultdict(lambda: defaultdict(list))
            for o in filtered:
                mid = str(o.get("_gm_manager_id") or o.get("manager_id") or "").strip()
                sid = str(o.get("supplier_id") or "").strip()
                if sid:
                    mid_sid[mid][sid].append(o)
            sorted_mids = [m for m in (self._general_manager_ids or []) if m in mid_sid]
            return self._assemble_agg_flat_2level(
                mid_sid,
                sorted_mids,
                sup_name_by_id,
                outer_label_fn=lambda k: (self._general_manager_names or {}).get(k, k),
                outer_bg=_SOFT_I,
                outer_fg=_INDIGO_DK,
                outer_border=_CHIP_BG,
                sup_indent=16,
            )

        elif is_general and not active_rid:
            # General + All Regions: manager → region → supplier
            mid_rid_sid: dict = defaultdict(
                lambda: defaultdict(lambda: defaultdict(list))
            )
            for o in filtered:
                mid = str(o.get("_gm_manager_id") or o.get("manager_id") or "").strip()
                rid = str(o.get("region_id") or "").strip()
                sid = str(o.get("supplier_id") or "").strip()
                if sid:
                    mid_rid_sid[mid][rid][sid].append(o)
            return self._assemble_agg_flat_3level(
                mid_rid_sid, sup_name_by_id, rid_to_name
            )

        else:
            # User + All Regions: region → supplier
            rid_sid: dict = defaultdict(lambda: defaultdict(list))
            for o in filtered:
                rid = str(o.get("region_id") or "").strip()
                sid = str(o.get("supplier_id") or "").strip()
                if sid:
                    rid_sid[rid][sid].append(o)
            sorted_rids = sorted(
                rid_sid.keys(),
                key=lambda r: (rid_to_name.get(r) or r or "").lower(),
            )
            return self._assemble_agg_flat_2level(
                rid_sid,
                sorted_rids,
                sup_name_by_id,
                outer_label_fn=lambda k: rid_to_name.get(k) or k or "Без регіону",
                outer_bg=_SOFT_I,
                outer_fg="#2563EB",
                outer_border=_CHIP_BG,
                sup_indent=16,
            )

    def _assemble_agg_flat_2level(
        self,
        outer_sid_map: dict,
        sorted_outer_keys: list,
        sup_name_by_id: dict,
        *,
        outer_label_fn,
        outer_bg: str,
        outer_fg: str,
        outer_border: str,
        sup_indent: int = 8,
    ) -> list:
        """Assemble flat table: outer_label → supplier_label → order rows."""
        # Build supplier sections in flat mode.
        flat_items: list = []  # [(outer_key, flat_result)]
        for ok in sorted_outer_keys:
            sid_orders = outer_sid_map.get(ok, {})
            sorted_sids = sorted(
                sid_orders.keys(),
                key=lambda s: (sup_name_by_id.get(s) or s).lower(),
            )
            for sid in sorted_sids:
                olist = sid_orders[sid]
                sname = sup_name_by_id.get(sid) or sid
                fr = self._build_supplier_section(sid, sname, olist, flat_mode=True)
                flat_items.append((ok, fr))

        if not flat_items:
            return []

        w_final = flat_items[0][1]["w_final"]
        header_row = flat_items[0][1]["header_row"]

        combined: list = [header_row]
        prev_outer = object()  # sentinel
        for ok, fr in flat_items:
            if ok != prev_outer:
                prev_outer = ok
                combined.append(
                    ft.Container(
                        bgcolor=outer_bg,
                        border=ft.border.only(bottom=ft.BorderSide(1, outer_border)),
                        padding=ft.padding.symmetric(horizontal=8, vertical=3),
                        content=ft.Text(
                            outer_label_fn(ok),
                            size=10,
                            weight=ft.FontWeight.W_700,
                            color=outer_fg,
                        ),
                    )
                )
            combined.append(
                ft.Container(
                    bgcolor=_SOFT_I,
                    border=ft.border.only(bottom=ft.BorderSide(1, _CHIP_BG)),
                    padding=ft.padding.only(left=sup_indent, right=8, top=3, bottom=3),
                    content=ft.Text(
                        fr["supplier_name"],
                        size=10,
                        weight=ft.FontWeight.W_700,
                        color=_INDIGO,
                    ),
                )
            )
            combined.extend(fr["rows_host"].controls[1:])

        return [self._wrap_flat_combined(combined, w_final)]

    def _assemble_agg_flat_3level(
        self,
        mid_rid_sid: dict,
        sup_name_by_id: dict,
        rid_to_name: dict,
    ) -> list:
        """Assemble flat table: manager → region → supplier → order rows."""
        sorted_mids = [m for m in (self._general_manager_ids or []) if m in mid_rid_sid]
        if not sorted_mids:
            sorted_mids = sorted(
                mid_rid_sid.keys(),
                key=lambda m: (
                    (self._general_manager_names or {}).get(m, m) or ""
                ).lower(),
            )

        flat_items: list = []  # [(mid, rid, flat_result)]
        for mid in sorted_mids:
            rid_sid = mid_rid_sid.get(mid, {})
            sorted_rids = sorted(
                rid_sid.keys(),
                key=lambda r: (rid_to_name.get(r) or r or "").lower(),
            )
            for rid in sorted_rids:
                sid_orders = rid_sid[rid]
                sorted_sids = sorted(
                    sid_orders.keys(),
                    key=lambda s: (sup_name_by_id.get(s) or s).lower(),
                )
                for sid in sorted_sids:
                    olist = sid_orders[sid]
                    sname = sup_name_by_id.get(sid) or sid
                    fr = self._build_supplier_section(sid, sname, olist, flat_mode=True)
                    flat_items.append((mid, rid, fr))

        if not flat_items:
            return []

        w_final = flat_items[0][2]["w_final"]
        header_row = flat_items[0][2]["header_row"]

        combined: list = [header_row]
        prev_mid = object()
        prev_rid = object()
        for mid, rid, fr in flat_items:
            if mid != prev_mid:
                prev_mid = mid
                prev_rid = object()  # reset region sentinel
                combined.append(
                    ft.Container(
                        bgcolor=_SOFT_I,
                        border=ft.border.only(bottom=ft.BorderSide(1, _CHIP_BG)),
                        padding=ft.padding.symmetric(horizontal=8, vertical=3),
                        content=ft.Text(
                            (self._general_manager_names or {}).get(mid, mid),
                            size=10,
                            weight=ft.FontWeight.W_700,
                            color=_INDIGO_DK,
                        ),
                    )
                )
            if rid != prev_rid:
                prev_rid = rid
                combined.append(
                    ft.Container(
                        bgcolor=_SOFT_I,
                        border=ft.border.only(bottom=ft.BorderSide(1, _CHIP_BG)),
                        padding=ft.padding.only(left=16, right=8, top=3, bottom=3),
                        content=ft.Text(
                            rid_to_name.get(rid) or rid or "Без регіону",
                            size=10,
                            weight=ft.FontWeight.W_700,
                            color=_ACCENT,
                        ),
                    )
                )
            combined.append(
                ft.Container(
                    bgcolor=_SOFT_I,
                    border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
                    padding=ft.padding.only(left=24, right=8, top=3, bottom=3),
                    content=ft.Text(
                        fr["supplier_name"],
                        size=10,
                        weight=ft.FontWeight.W_700,
                        color=_INDIGO,
                    ),
                )
            )
            combined.extend(fr["rows_host"].controls[1:])

        return [self._wrap_flat_combined(combined, w_final)]

    def _wrap_flat_combined(self, combined: list, w_final: float) -> ft.Container:
        """Wrap flat rows in the standard scrollable container."""
        # expand=True + scroll=HIDDEN: inner v-scroll so buttons stay fixed in Stack.
        flat_rows_host = ft.Column(combined, spacing=0, scroll=ft.ScrollMode.HIDDEN, expand=True)
        _pay_scroll_row = ft.Row(
            [
                ft.Container(
                    width=max(1, int(w_final)),
                    clip_behavior=ft.ClipBehavior.HARD_EDGE,
                    content=flat_rows_host,
                    expand=True,
                )
            ],
            spacing=0,
            expand=True,
            scroll=ft.ScrollMode.HIDDEN,
        )

        # Overlay h-scroll buttons — visible only when table is wider than viewport.
        _pg = self._page()
        _vp_w = int(getattr(self.content_area, "width", 0) or 0)
        if _vp_w <= 0 and _pg:
            _vp_w = int(getattr(_pg, "width", 0) or 0)
        _vp_w = max(600, _vp_w or 1200)
        _pay_needs = int(w_final) > _vp_w
        _pay_step = max(300, int(w_final) // 3)

        def _do_hscroll_pay(delta):
            async def _run():
                with contextlib.suppress(Exception):
                    await _pay_scroll_row.scroll_to(delta=delta, duration=200)

            with contextlib.suppress(Exception):
                if _pg:
                    _pg.run_task(_run)

        _pay_sh = ft.BoxShadow(
            spread_radius=1,
            blur_radius=8,
            color=ft.Colors.with_opacity(0.28, ft.Colors.BLACK),
            offset=ft.Offset(0, 2),
        )
        _pay_bs = ft.ButtonStyle(padding=ft.padding.all(4))
        _pay_left = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_LEFT,
                icon_size=20,
                icon_color=_ACCENT,
                on_click=lambda _: _do_hscroll_pay(-_pay_step),
                style=_pay_bs,
            ),
            left=4,
            top=60,
            bgcolor=_PANEL,
            border_radius=20,
            width=36,
            height=36,
            shadow=_pay_sh,
            opacity=0.9,
            visible=_pay_needs,
        )
        _pay_right = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_RIGHT,
                icon_size=20,
                icon_color=_ACCENT,
                on_click=lambda _: _do_hscroll_pay(_pay_step),
                style=_pay_bs,
            ),
            right=4,
            top=60,
            bgcolor=_PANEL,
            border_radius=20,
            width=36,
            height=36,
            shadow=_pay_sh,
            opacity=0.9,
            visible=_pay_needs,
        )

        return ft.Stack(
            [
                ft.Container(
                    bgcolor=_PANEL,
                    border=ft.border.all(1, _BORDER),
                    border_radius=8,
                    content=_pay_scroll_row,
                ),
                _pay_left,
                _pay_right,
            ],
        )

    async def _async_render_initial(self) -> None:
        page = self._page()
        if not page:
            return
        tree_controls = await asyncio.to_thread(self._sync_build_tree)
        with contextlib.suppress(Exception):
            self.content_area.scroll = ft.ScrollMode.ADAPTIVE
        self.content_area.controls[:] = tree_controls
        with contextlib.suppress(Exception):
            self.content_area.update()
        with contextlib.suppress(Exception):
            self._update_balance_display()  # also updates sticky totals bar + counters

    async def _async_initial_load(self) -> None:
        page = self._page()
        if not page:
            return
        _is_sub = bool(self.user_data.get("_is_sub_section_view"))

        # "Загальні" mode: load suppliers + FX, then update aggregate sticky header.
        if self._general_mode and self._general_manager_ids:
            try:
                await asyncio.to_thread(self._load_lookups)
            except Exception:
                pass
            try:
                await asyncio.to_thread(self._fx_uah_per_unit)
            except Exception:
                pass
            _agg_orders = getattr(self, "_agg_orders_cache", []) or []
            _pg = self._page()
            # Sub-section views must not overwrite the parent aggregate sticky header.
            if _pg and not _is_sub:
                with contextlib.suppress(Exception):
                    self._build_aggregate_sticky_now(
                        _pg, _agg_orders, filter_manager=False
                    )
                with contextlib.suppress(Exception):
                    _pg.update()
            self._payments_ui_ready = True
            return

        # "Всі регіони" mode: load suppliers + FX, then update aggregate sticky header.
        if not self.active_region and not self._general_mode:
            try:
                await asyncio.to_thread(self._load_lookups)
            except Exception:
                pass
            try:
                await asyncio.to_thread(self._fx_uah_per_unit)
            except Exception:
                pass
            # Use the pre-fetched cache (from DB, all regions) set during build().
            _agg_orders = getattr(self, "_agg_orders_cache", []) or []
            _pg = self._page()
            # Sub-section views must not overwrite the parent aggregate sticky header.
            if _pg and not _is_sub:
                with contextlib.suppress(Exception):
                    self._build_aggregate_sticky_now(
                        _pg, _agg_orders, filter_manager=True
                    )
                with contextlib.suppress(Exception):
                    _pg.update()
            self._payments_ui_ready = True
            return
        try:
            await asyncio.to_thread(self._load_lookups)
            with contextlib.suppress(Exception):
                self._ref_data_pulse_ts = await asyncio.to_thread(
                    self.db.get_reference_data_pulse, self.company_id
                )

            # Verify the shared orders snapshot is up-to-date BEFORE reading it.
            # Use effective region ID (UUID) — active_region may be a name string.
            _eff_region = self._effective_region_id()
            await ensure_orders_cache_fresh(
                user_data=self.user_data,
                db=self.db,
                app_data_dir=self._app_data_dir(),
                company_id=self.company_id,
                manager_id=self.manager_id,
                region_id=_eff_region,
                shared_orders_map=self._shared_orders_map,
            )

            db_pulse_ts = await asyncio.to_thread(self._payments_scope_pulse_ts)
            # Normalize Ukrainian payment_status aliases in the shared cache BEFORE reading
            await asyncio.to_thread(self._normalize_shared_cache)
            # Capture outbox before any load so a concurrent flush() that clears
            # self._outbox between _load_outbox() and _merge_outbox_into_data()
            # does not cause pending edits to be silently dropped.
            _pre_load_outbox = dict(self._outbox)
            cache_ready = await asyncio.to_thread(self._load_payments_snapshot_cache)

            if cache_ready:
                await asyncio.to_thread(self._load_outbox)
                # Restore items a concurrent flush may have already removed from both
                # self._outbox and the disk file before _load_outbox() ran.
                for _ok, _ov in list(_pre_load_outbox.items()):
                    if isinstance(_ov, dict) and _ok not in self._outbox:
                        self._outbox[_ok] = _ov
                # Only re-fetch settlements from DB when the DB has changed since last sync.
                # _settlements_cache_pulse_ts is read from the settlements JSON on load.
                # Skipping the DB fetch on unchanged pulse makes tab-open instant.
                if db_pulse_ts and db_pulse_ts != self._settlements_cache_pulse_ts:
                    await asyncio.to_thread(self._load_settlements_from_db)
                    self._payments_snapshot_pulse_ts = db_pulse_ts
                    self._settlements_cache_pulse_ts = db_pulse_ts
                    await asyncio.to_thread(
                        self._save_payments_snapshot_cache, db_pulse_ts
                    )
                else:
                    self._payments_snapshot_pulse_ts = db_pulse_ts
                # Merge pending outbox AFTER (possible) fresh DB load so any un-flushed edits
                # (flush failed on previous session) survive the restart.
                await asyncio.to_thread(self._merge_outbox_into_data)
            else:
                # No snapshot yet (first run or cache deleted) — fall back to DB.
                await asyncio.to_thread(self._load_data_from_db)
                await asyncio.to_thread(self._load_outbox)
                # Restore items a concurrent flush may have already removed.
                for _ok, _ov in list(_pre_load_outbox.items()):
                    if isinstance(_ov, dict) and _ok not in self._outbox:
                        self._outbox[_ok] = _ov
                await asyncio.to_thread(self._merge_outbox_into_data)
                self._payments_snapshot_pulse_ts = db_pulse_ts
                self._settlements_cache_pulse_ts = db_pulse_ts
                await asyncio.to_thread(self._save_payments_snapshot_cache, db_pulse_ts)

            await self._async_render_initial()
            # Pre-warm FX cache so every supplier expand is instant (no HTTP on click).
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._fx_uah_per_unit)
        except Exception:
            pass
        finally:
            self._payments_ui_ready = True
            self._start_sync_timer()
            with contextlib.suppress(Exception):
                self._refresh_sync_indicator_state()
            # Hide spinner after a delay so Flutter finishes rendering before it disappears.
            _ind = self._header_loading_indicator
            if _ind is not None:
                _pg = self._page()
                if _pg and hasattr(_pg, "run_task"):

                    async def _hide_ind(_i=_ind):
                        import asyncio as _aio

                        await _aio.sleep(0.30)
                        _i.visible = False
                        with contextlib.suppress(Exception):
                            _i.update()

                    with contextlib.suppress(Exception):
                        _pg.run_task(_hide_ind)
                else:
                    _ind.visible = False

    async def resume(self) -> None:
        if not self._payments_ui_ready:
            return
        page = self._page()
        _sticky = getattr(page, "_view_sticky_header", None) if page else None
        _is_sub = bool(self.user_data.get("_is_sub_section_view"))
        if (
            _sticky is not None
            and not _is_sub
            and getattr(page, "_active_view", None) == "show_payments"
        ):
            _hr = getattr(self, "_sticky_header_row", None)
            _cb = getattr(self, "_sticky_counters_bar", None)
            _tb = getattr(self, "_sticky_totals_bar", None)
            new_controls = [c for c in [_hr, _cb, _tb] if c is not None]
            if new_controls:
                _sticky.controls[:] = new_controls
                with contextlib.suppress(Exception):
                    _sticky.update()
        with contextlib.suppress(Exception):
            self._update_counters()
        with contextlib.suppress(Exception):
            self._update_balance_display()
        try:
            purged = self._purge_deleted_orders()
            cross_tab_orders = self._sync_from_shared_map()
            if purged:
                self._schedule_rerender()
            elif cross_tab_orders:
                if self._supplier_section_refs:
                    # Fast path: rebuild only the affected supplier sections
                    await self._refresh_affected_suppliers_async(cross_tab_orders)
                else:
                    # Fallback: full rebuild (first render not done yet)
                    with contextlib.suppress(Exception):
                        self._update_balance_display()
                    with contextlib.suppress(Exception):
                        self._update_counters()
                    self._schedule_rerender()
        except Exception:
            pass

        _run_pg = self._page()
        if _run_pg and hasattr(_run_pg, "run_task"):
            with contextlib.suppress(Exception):
                _run_pg.run_task(self._bg_payments_pulse_check)

    async def _bg_payments_pulse_check(self) -> None:
        """DB pulse check — if remote data changed, trigger full reload.

        Called both right after resume() (tab switch) AND periodically from
        _bg_sync_loop (~every 20s) so a change made on another device shows
        up here even if we never switch away from this tab."""
        try:
            new_pulse = await asyncio.to_thread(self._payments_scope_pulse_ts)
            cached_pulse = int(getattr(self, "_payments_snapshot_pulse_ts", 0) or 0)
            if new_pulse > cached_pulse:
                # Check if the pulse increase came from our own session's DB writes.
                session_db_pulse = int(
                    (self._shared_orders_map or {}).get("__session_db_pulse__", 0)
                    or 0
                )
                if new_pulse <= session_db_pulse:
                    # Own write — advance cached pulse, no reload needed.
                    self._payments_snapshot_pulse_ts = new_pulse
                    return
                # Skip reload while our own flush is still in progress (race window).
                if getattr(self, "_outbox_flush_running", False):
                    return
                # Genuine external change — clear session flags and reload.
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
            # Payments pulse unchanged — check if reference data changed.
            ref_pulse = await asyncio.to_thread(
                self.db.get_reference_data_pulse, self.company_id
            )
            if ref_pulse > self._ref_data_pulse_ts:
                await asyncio.to_thread(self._load_lookups)
                self._ref_data_pulse_ts = ref_pulse
                self._schedule_rerender()
        except Exception:
            pass

    def apply_live_scope_sync(self, scopes: set) -> None:
        try:
            if not scopes or not (set(scopes) & {"orders", "payments", "packaging"}):
                return
            pg = self._page()
            if pg and str(getattr(pg, "_active_view", "")) != "show_payments":
                return
            return
        except Exception:
            pass

    def _supplier_currency(self, supplier_id: str) -> str:
        sup = next(
            (
                s
                for s in (self._suppliers or [])
                if str((s or {}).get("id") or "") == supplier_id
            ),
            None,
        )
        ccy = str((sup or {}).get("currency") or "USD").upper().strip() or "USD"
        return ccy

    def _fx_uah_per_unit(self) -> dict:
        """
        Returns mapping: currency_code -> UAH per 1 unit of currency.
        Cache-only — NEVER performs blocking network I/O. If the cache is
        missing/stale, a background refresh is scheduled (see
        `_fx_uah_schedule_background_refresh`) and the best data we have so
        far (possibly empty, on the very first call) is returned immediately.

        Why: this used to fall through to up to 5 sequential HTTP requests
        (6s timeout each = up to 30s worst case) INLINE whenever the 900s/
        120s cache expired. Callers include synchronous blur handlers like
        `_on_paid_blur` (via `_update_balance_display`) that are NOT run in
        `asyncio.to_thread` — that blocking chain froze the whole Flet event
        loop for the whole app, not just this tab. This was the root cause
        of "довго зберігається сума яка сплачена" in Взаєморозрахунки.
        """
        try:
            now = time.time()
            # Prefer the session-wide shared cache (user_data) so sub-instances
            # created by build_region_sections don't each make their own HTTP request.
            _shared = (
                self.user_data.get("_fx_uah_shared_cache")
                if isinstance(self.user_data, dict)
                else None
            )
            if isinstance(_shared, dict) and float(_shared.get("ts") or 0) > 0:
                self._fx_uah_cache = _shared

            holder = (
                self._fx_uah_cache
                if isinstance(getattr(self, "_fx_uah_cache", None), dict)
                else {"ts": 0.0, "rates": {}}
            )
            cached_ts = float(holder.get("ts") or 0.0)
            cached_rates = holder.get("rates") or {}
            # Cache successful result for 900s; failed (empty) result for 120s
            # before triggering another background refresh attempt.
            is_fresh = cached_ts > 0 and (now - cached_ts) < (
                900 if cached_rates else 120
            )
            if not is_fresh:
                self._fx_uah_schedule_background_refresh()
            return cached_rates
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
                result = {"ts": time.time(), "rates": rates}
                self._fx_uah_cache = result
                if isinstance(self.user_data, dict):
                    self.user_data["_fx_uah_shared_cache"] = result
                # Rates just changed — refresh any totals already on screen.
                with contextlib.suppress(Exception):
                    self._update_balance_display()
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
            # NBU format is already UAH per 1 unit (sometimes per `unit`).
            # https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json
            data = _get_json(
                "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
            )
            if not isinstance(data, list):
                return {}
            out = {"UAH": 1.0}
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

        def _from_er_api() -> dict:
            raw = _get_json("https://open.er-api.com/v6/latest/UAH")
            rates = raw.get("rates") if isinstance(raw, dict) else {}
            rates = rates if isinstance(rates, dict) else {}
            out = {"UAH": 1.0}
            for cc, val in rates.items():
                v = _safe_float(val, None)
                cc_u = str(cc or "").upper().strip()
                if not cc_u or cc_u == "UAH" or v is None or v <= 0:
                    continue
                # 1 UAH = v foreign -> 1 foreign = 1/v UAH
                out[cc_u] = 1.0 / float(v)
            return out

        def _from_exchangerate_host() -> dict:
            raw = _get_json("https://api.exchangerate.host/latest?base=UAH")
            rates = raw.get("rates") if isinstance(raw, dict) else {}
            rates = rates if isinstance(rates, dict) else {}
            out = {"UAH": 1.0}
            for cc, val in rates.items():
                v = _safe_float(val, None)
                cc_u = str(cc or "").upper().strip()
                if not cc_u or cc_u == "UAH" or v is None or v <= 0:
                    continue
                out[cc_u] = 1.0 / float(v)
            return out

        def _from_frankfurter() -> dict:
            raw = _get_json("https://api.frankfurter.app/latest?from=UAH")
            rates = raw.get("rates") if isinstance(raw, dict) else {}
            rates = rates if isinstance(rates, dict) else {}
            out = {"UAH": 1.0}
            for cc, val in rates.items():
                v = _safe_float(val, None)
                cc_u = str(cc or "").upper().strip()
                if not cc_u or cc_u == "UAH" or v is None or v <= 0:
                    continue
                out[cc_u] = 1.0 / float(v)
            return out

        def _from_floatrates() -> dict:
            raw = _get_json("https://www.floatrates.com/daily/uah.json")
            if not isinstance(raw, dict):
                return {}
            out = {"UAH": 1.0}
            for cc, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                # floatrates gives 1 UAH = rate foreign
                v = _safe_float(entry.get("rate"), None)
                cc_u = str(cc or "").upper().strip()
                if not cc_u or cc_u == "UAH" or v is None or v <= 0:
                    continue
                out[cc_u] = 1.0 / float(v)
            return out

        out = {}
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

    def _supplier_to_usd_rate(
        self, supplier_ccy: str, order: dict | None = None
    ) -> Optional[float]:
        ccy = str(supplier_ccy or "").upper().strip() or "USD"
        if ccy == "USD":
            return 1.0

        fx = self._fx_uah_per_unit()
        uah_ccy = _safe_float(fx.get(ccy), None)
        uah_usd = _safe_float(fx.get("USD"), None)
        if uah_ccy is None or uah_usd is None or uah_usd <= 0:
            return None
        return float(uah_ccy) / float(uah_usd)

    def _supplier_amount_usd(
        self, amount: Optional[float], supplier_ccy: str, order: dict | None = None
    ) -> Optional[float]:
        if amount is None:
            return None
        rate = self._supplier_to_usd_rate(supplier_ccy, order=order)
        if rate is None:
            return None
        try:
            return float(amount) * float(rate)
        except Exception:
            return None

    def _is_order_in_scope(self, o: dict) -> bool:
        """Фільтр для вкладки Взаєморозрахунки.
        Показуємо: всі НЕвиробничі + виробничі тільки якщо оплата Аванс або Сплачено.
        Приховуємо: Виробництво + Не сплачений."""
        st_raw = str(o.get("status") or "production").strip()
        pay_raw = str(o.get("payment_status") or "unpaid").strip()
        st = st_raw.casefold()
        pay = pay_raw.casefold()

        is_production = (
            st in {"production", "виробництво", "в виробництві", "виро", "виробн."}
            or st_raw == "Виробництво"
        )
        if not is_production:
            return True  # всі НЕвиробничі замовлення показуємо

        # Виробничі — тільки аванс або сплачено
        is_advance = pay in {
            "advance",
            "аванс",
            "передплата",
            "предоплата",
            "частково",
            "частково сплачено",
        }
        is_paid = pay in {"paid", "сплачений", "сплачено", "оплачено"}
        return is_advance or is_paid

    def _line_amount_supplier_ccy(
        self, o: dict, it: Optional[dict], idx: int, supplier_ccy: str
    ) -> Optional[float]:
        """
        Same basis as the 'Вартість' cell in this view: per-line amount in the supplier's currency
        (order-level total when a single line; otherwise per-item override or q×price in supplier ccy).
        """
        sc = (supplier_ccy or "USD").upper().strip() or "USD"
        items = o.get("items") if isinstance(o.get("items"), list) else []
        order_man = _safe_float(o.get("amount_manual_override"), None)
        if order_man is not None and len(items) <= 1:
            return float(order_man)
        it0 = it if isinstance(it, dict) else {}
        it_m = _safe_float(it0.get("amount_manual_override"), None)
        if it_m is not None:
            return float(it_m)
        q = _safe_float(it0.get("qty") or it0.get("quantity") or 0, 0.0) or 0.0
        p = _safe_float(it0.get("purchase_price"), None)
        ccy = str(it0.get("purchase_currency") or sc).upper().strip() or sc
        if p is None:
            pid = str(it0.get("product_id") or "").strip()
            prod = next(
                (
                    p
                    for p in (self._products or [])
                    if str((p or {}).get("id") or "") == pid
                ),
                None,
            )
            p = _safe_float((prod or {}).get("purchase_price"), None)
            ccy = (
                str((prod or {}).get("purchase_currency") or ccy or "").upper().strip()
                or ccy
            )
        if p is None or ccy != sc:
            return None
        return float(q) * float(p)

    def _order_amount_supplier_ccy(self, o: dict, supplier_ccy: str) -> Optional[float]:
        items = o.get("items") if isinstance(o.get("items"), list) else []
        if not items:
            return None
        total = 0.0
        has_any = False
        for idx, it in enumerate(items):
            v = self._line_amount_supplier_ccy(
                o, it if isinstance(it, dict) else {}, idx, supplier_ccy
            )
            if v is not None:
                total += float(v)
                has_any = True
        return total if has_any else None

    def _item_weight_kg(self, it: dict) -> float:
        try:
            w_manual = _safe_float((it or {}).get("weight_actual_kg"), None)
            if w_manual is not None:
                return max(0.0, float(w_manual))
            q = (
                _safe_float(
                    (it or {}).get("qty") or (it or {}).get("quantity") or 0.0, 0.0
                )
                or 0.0
            )
            w_u = _safe_float((it or {}).get("weight_per_unit_kg"), 0.0) or 0.0
            if w_u <= 0:
                pid = str((it or {}).get("product_id") or "").strip()
                prod = next(
                    (
                        p
                        for p in (self._products or [])
                        if isinstance(p, dict) and str((p or {}).get("id") or "") == pid
                    ),
                    None,
                )
                w_u = _safe_float((prod or {}).get("weight_kg"), 0.0) or 0.0
            return max(0.0, float(q) * float(w_u))
        except Exception:
            return 0.0

    def _item_unit_cost_usd(
        self, order: dict, it: dict, supplier_ccy: str
    ) -> Optional[float]:
        try:
            q = (
                _safe_float(
                    (it or {}).get("qty") or (it or {}).get("quantity") or 0.0, 0.0
                )
                or 0.0
            )
            if q <= 0:
                return None
            # Goods USD
            p = _safe_float((it or {}).get("purchase_price"), None)
            ccy = str((it or {}).get("purchase_currency") or "").upper().strip()
            if p is None:
                # Fallback to product card purchase price/currency (matches Orders behavior).
                pid = str((it or {}).get("product_id") or "").strip()
                prod = next(
                    (
                        p
                        for p in (self._products or [])
                        if isinstance(p, dict) and str((p or {}).get("id") or "") == pid
                    ),
                    None,
                )
                p = _safe_float((prod or {}).get("purchase_price"), None)
                ccy = (
                    str((prod or {}).get("purchase_currency") or ccy or "")
                    .upper()
                    .strip()
                )
            if p is None:
                return None
            ccy = ccy or str(supplier_ccy or "").upper().strip() or "USD"
            goods = float(q) * float(p)
            if ccy == "USD":
                goods_usd = float(goods)
            elif ccy == str(supplier_ccy or "").upper().strip():
                goods_usd = self._supplier_amount_usd(
                    float(goods), supplier_ccy, order=None
                )
            else:
                goods_usd = None
            if goods_usd is None:
                return None
            # Shipping USD (tariff * weight)
            tariff = _safe_float(order.get("delivery_price_snapshot"), None)
            tariff_ccy = (
                str(order.get("delivery_price_currency_snapshot") or "").upper().strip()
                or ""
            )
            if tariff is None:
                # Fallback: use delivery method tariff when snapshot is missing (matches OrdersView behavior).
                did = str(order.get("delivery_type_id") or "").strip()
                dname = str(order.get("delivery_type_name") or "").strip()
                method_row = None
                if did:
                    method_row = next(
                        (
                            m
                            for m in (self._delivery_methods or [])
                            if isinstance(m, dict)
                            and str(m.get("id") or "").strip() == did
                        ),
                        None,
                    )
                if method_row is None and dname:
                    dn = dname.lower().strip()
                    method_row = next(
                        (
                            m
                            for m in (self._delivery_methods or [])
                            if isinstance(m, dict)
                            and str(m.get("name") or "").lower().strip() == dn
                        ),
                        None,
                    )
                if isinstance(method_row, dict):
                    tariff = _safe_float(
                        (
                            method_row.get("effective_price_per_kg")
                            if method_row.get("effective_price_per_kg") is not None
                            else method_row.get("base_price_per_kg")
                        ),
                        None,
                    )
                    tariff_ccy = (
                        str(
                            method_row.get("effective_price_currency")
                            or method_row.get("price_currency")
                            or method_row.get("base_price_currency")
                            or tariff_ccy
                            or "USD"
                        )
                        .upper()
                        .strip()
                    )

            tariff_ccy = tariff_ccy or "USD"
            ship_usd = 0.0
            if tariff is not None:
                if str(tariff_ccy or "").upper().strip() == "USD":
                    t_usd = float(tariff)
                elif (
                    str(tariff_ccy or "").upper().strip()
                    == str(supplier_ccy or "").upper().strip()
                ):
                    t_usd = self._supplier_amount_usd(
                        float(tariff), supplier_ccy, order=None
                    )
                else:
                    t_usd = None
                if t_usd is not None:
                    ship_usd = float(t_usd) * float(self._item_weight_kg(it))
            return (float(goods_usd) + float(ship_usd)) / float(q)
        except Exception:
            return None

    def _supplier_settlement_sum(self, supplier_id: str, supplier_ccy: str) -> float:
        total = 0.0
        for s in self._settlements or []:
            if not isinstance(s, dict):
                continue
            if str(s.get("supplier_id") or "").strip() != supplier_id:
                continue
            ccy = str(s.get("currency") or supplier_ccy).upper().strip() or supplier_ccy
            if ccy != supplier_ccy:
                continue
            total += _safe_float(s.get("amount"), 0.0) or 0.0
        return total

    def _build_supplier_section(
        self,
        supplier_id: str,
        supplier_name: str,
        orders_for_supplier: list,
        counters: dict | None = None,
        flat_mode: bool = False,
    ) -> ft.Container:
        supplier_ccy = self._supplier_currency(supplier_id)
        delivery_by_id = {
            str((m or {}).get("id") or ""): (m or {}).get("name")
            for m in (self._delivery_methods or [])
            if isinstance(m, dict)
        }

        # Ensure delivery id->name mapping contains values coming from orders too.
        # Some orders may have delivery_type_id like "custom" while keeping a real name in delivery_type_name.
        try:
            for _o in orders_for_supplier or []:
                if not isinstance(_o, dict):
                    continue
                _did = str(_o.get("delivery_type_id") or "").strip()
                _dname = str(_o.get("delivery_type_name") or "").strip()
                if not _did or not _dname:
                    continue
                cur = str(delivery_by_id.get(_did) or "").strip()
                if (not cur) or (cur.lower() in {"custom", _did.lower()}):
                    delivery_by_id[_did] = _dname
        except Exception:
            pass

        # Opening balance: single effective value (default 0).
        opening_amt = 0.0
        try:
            all_sett_local = [
                x
                for x in (self._settlements or [])
                if isinstance(x, dict)
                and str(x.get("supplier_id") or "").strip() == supplier_id
            ]
            openings_local = [
                s
                for s in all_sett_local
                if str(s.get("type") or "") == "opening_balance"
            ]
            if openings_local:
                # If there are multiple, use the latest by ts/created_at.
                latest = sorted(
                    openings_local,
                    key=lambda x: (
                        str(x.get("ts") or x.get("created_at") or ""),
                        str(x.get("updated_at") or ""),
                        str(x.get("id") or ""),
                    ),
                    reverse=True,
                )[0]
                opening_amt = _safe_float(latest.get("amount"), 0.0) or 0.0
        except Exception:
            opening_amt = 0.0

        debt = 0.0
        paid_sum = 0.0
        for o in orders_for_supplier:
            # Debt is per-item if items have overrides.
            amt = self._order_amount_supplier_ccy(o, supplier_ccy)
            paid = _safe_float(o.get("paid_amount"), 0.0) or 0.0
            if amt is not None:
                debt += float(amt)
                paid_sum += paid
            else:
                # No calculable order amount (items missing or no purchase prices).
                # Treat paid_amount as the effective debt so the order is balance-neutral
                # and doesn't inflate paid_sum without a matching debt entry.
                debt += paid
                paid_sum += paid

        settlement_sum = self._supplier_settlement_sum(supplier_id, supplier_ccy)
        # opening balance and compensations are stored in settlements; amount is signed already
        balance = (debt - paid_sum) + settlement_sum

        def _bal_label(v: float) -> str:
            """Label carries the 'ми винні' / 'постач. винен' hint."""
            if abs(v) < 0.0001:
                return self.t("payments.balance.label", "Баланс:")
            if v > 0:
                return self.t("payments.balance.we_owe_short", "Баланс (ми винні):")
            return self.t("payments.balance.they_owe_full", "Баланс (постач. винен):")

        def _safe_money(v: float) -> str:
            """Format number; treat values near zero as 0 to avoid '-0'."""
            return _fmt_money(0.0 if abs(v) < 0.0001 else v)

        bg = _OK_BG if abs(balance) < 0.0001 else (_WARN_BG if balance > 0 else _ERR_BG)
        fg = _OK_T if abs(balance) < 0.0001 else (_WARN_T if balance > 0 else _ERR_DARK)

        bal_label_str = _bal_label(balance)
        # Value line: just amount + currency, no hint (hint moved to label)
        bal_ccy_str = f"{_safe_money(balance)} {supplier_ccy}".strip()
        bal_usd_str = ""
        if supplier_ccy != "USD":
            bal_usd = self._supplier_amount_usd(
                balance,
                supplier_ccy,
                order=(orders_for_supplier[0] if orders_for_supplier else None),
            )
            if bal_usd is not None:
                bal_usd_str = f"{_safe_money(bal_usd)} $"
        badge_pkg = ft.Container(
            padding=ft.padding.symmetric(horizontal=6, vertical=4),
            bgcolor=self.UI_BADGE_INDIGO,
            border_radius=6,
            content=ft.Text(
                "ПОСТАЧАЛЬНИК", size=8, weight=ft.FontWeight.W_800, color="white"
            ),
        )

        # ── helper: amount → USD string (empty if same currency or no rate) ──
        _ref_order = orders_for_supplier[0] if orders_for_supplier else None

        def _to_usd_str(amount: float) -> str:
            if supplier_ccy == "USD":
                return ""
            v = self._supplier_amount_usd(amount, supplier_ccy, order=_ref_order)
            return f"{_fmt_money(v)} $" if v is not None else ""

        debt_usd_str = _to_usd_str(debt)
        paid_usd_str = _to_usd_str(paid_sum)
        opening_usd_str = _to_usd_str(opening_amt)

        # ── stat cell builder (vertical, supports optional USD second line) ──
        # Label on top (11px grey), main value bold below, optional USD line last.
        # container.data = {"main": ft.Text, "usd": ft.Text, "label": ft.Text}
        _LBL = _MUTED

        _CW = 132  # uniform chip width for all stat cells

        def _stat_cell(
            label: str,
            value: str,
            bg: str,
            val_color: str,
            width: int = _CW,  # kept for compat; always uses _CW
            usd_str: str = "",
        ) -> ft.Container:
            label_text = ft.Text(
                label,
                size=9,
                weight=ft.FontWeight.W_700,
                color=_LBL,
                text_align=ft.TextAlign.CENTER,
            )
            main_text = ft.Text(
                value,
                size=10,
                color=val_color,
                weight=ft.FontWeight.W_700,
                no_wrap=False,  # supplier ccy: bold
                text_align=ft.TextAlign.CENTER,
            )
            usd_text = ft.Text(
                usd_str,
                size=10,
                color=val_color,
                weight=None,
                no_wrap=False,
                visible=bool(usd_str),  # $: not bold
                text_align=ft.TextAlign.CENTER,
            )
            col_items = [label_text, main_text, usd_text]
            return ft.Container(
                width=_CW,
                padding=ft.padding.symmetric(horizontal=6, vertical=3),
                bgcolor=bg,
                border_radius=6,
                alignment=ft.alignment.Alignment(0, -1),
                data={"main": main_text, "usd": usd_text, "label": label_text},
                content=ft.Column(
                    col_items,
                    spacing=2,
                    tight=True,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            )

        # 1. Баланс
        bal_cell = _stat_cell(bal_label_str, bal_ccy_str, bg, fg, usd_str=bal_usd_str)
        # 2. Сума Замовлень
        debt_cell = _stat_cell(
            "Сума Замовлень:",
            f"{_fmt_money(debt)} {supplier_ccy}",
            _ALT,
            _TEXT,
            usd_str=debt_usd_str,
        )
        # 3. Сплачено
        paid_cell = _stat_cell(
            self.t("payments.stat.paid_sum", "Сплачено:"),
            f"{_fmt_money(paid_sum)} {supplier_ccy}",
            _OK_BG,
            _OK_T,
            usd_str=paid_usd_str,
        )
        # 4. Початк. Баланс
        opening_cell = _stat_cell(
            "Початк. Баланс:",
            f"{_fmt_money(opening_amt)} {supplier_ccy}",
            "#DBEAFE",
            _TEXT,
            usd_str=opening_usd_str,
        )

        self._store_supplier_balance_ref(supplier_id, bal_cell)
        self._store_supplier_debt_ref(supplier_id, debt_cell)
        self._store_supplier_paid_ref(supplier_id, paid_cell)

        order_count = len(orders_for_supplier)
        unpaid_count = sum(
            1
            for o in orders_for_supplier
            if self._payment_bucket_key(o.get("payment_status")) != "paid"
        )
        _upd_bg = _OK_BG if unpaid_count == 0 else _ERR_BG
        _upd_fg = _OK_T if unpaid_count == 0 else _ERR
        _unpaid_val_text = ft.Text(
            str(unpaid_count),
            size=10,
            weight=ft.FontWeight.W_700,
            color=_upd_fg,
            text_align=ft.TextAlign.CENTER,
        )
        unpaid_cell = ft.Container(
            width=_CW,
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            bgcolor=_upd_bg,
            border_radius=6,
            alignment=ft.alignment.Alignment(0, -1),
            data={"val": _unpaid_val_text},
            content=ft.Column(
                [
                    ft.Text(
                        "Не сплачених:",
                        size=9,
                        weight=ft.FontWeight.W_700,
                        color=_MUTED,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    _unpaid_val_text,
                ],
                spacing=2,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        _count_val_text = ft.Text(
            str(order_count),
            size=10,
            weight=ft.FontWeight.W_700,
            color=_ACCENT,
            text_align=ft.TextAlign.CENTER,
        )
        count_cell = ft.Container(
            width=_CW,
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            bgcolor=_CHIP_BG,
            border_radius=6,
            alignment=ft.alignment.Alignment(0, -1),
            data={"val": _count_val_text},
            content=ft.Column(
                [
                    ft.Text(
                        "Замовлень:",
                        size=9,
                        weight=ft.FontWeight.W_700,
                        color=_MUTED,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    _count_val_text,
                ],
                spacing=2,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        self._supplier_unpaid_refs[supplier_id] = unpaid_cell
        self._supplier_count_refs[supplier_id] = count_cell

        header = ft.Row(
            controls=[
                ft.Container(
                    expand=True,
                    content=ft.Row(
                        [
                            badge_pkg,
                            ft.Icon(
                                ft.Icons.LOCAL_SHIPPING_OUTLINED,
                                size=18,
                                color=self.UI_ACCENT_BLUE,
                            ),
                            ft.Text(
                                supplier_name or supplier_id,
                                size=16,
                                weight=ft.FontWeight.W_700,
                                color=_TEXT,
                            ),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ),
                ft.Row(
                    [
                        unpaid_cell,
                        count_cell,
                        bal_cell,
                        debt_cell,
                        paid_cell,
                        opening_cell,
                    ],
                    spacing=6,
                    tight=True,
                    wrap=True,
                    vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )

        body = ft.Column(spacing=0, visible=(supplier_id in self._expanded_suppliers))

        # buttons row
        def _open_opening_balance(_):
            self._open_opening_balance_dialog(supplier_id, supplier_name, supplier_ccy)

        def _open_comp(_):
            self._open_compensation_dialog(
                supplier_id, supplier_name, supplier_ccy, orders_for_supplier
            )

        btns = ft.Row(
            controls=[
                ft.OutlinedButton(
                    self.t("payments.opening_balance", "Початковий баланс"),
                    height=28,
                    style=ft.ButtonStyle(
                        text_style=ft.TextStyle(size=11),
                        side=ft.BorderSide(1, self.UI_PANEL_BORDER),
                        padding=ft.padding.symmetric(horizontal=10, vertical=4),
                    ),
                    on_click=_open_opening_balance,
                ),
                ft.FilledButton(
                    self.t("payments.compensation", "Компенсація"),
                    height=28,
                    style=ft.ButtonStyle(
                        text_style=ft.TextStyle(size=11),
                        padding=ft.padding.symmetric(horizontal=10, vertical=4),
                        bgcolor={ft.ControlState.DEFAULT: self.UI_BADGE_INDIGO},
                        color={ft.ControlState.DEFAULT: ft.Colors.WHITE},
                    ),
                    on_click=_open_comp,
                ),
            ],
            spacing=8,
            wrap=True,
        )

        # Table layout (compact like "Замовлення")
        ROW_H = int(max(26, min(72, int(getattr(self, "_table_row_height", 32) or 32))))
        HDR_H = int(max(34, ROW_H + 4))
        TXT = 11
        BDR = self.UI_CARD_BORDER
        BG_HDR = _HDR
        BG_A = _PANEL  # парна позиція замовлення
        BG_B = "#F8FAFC"  # непарна позиція — легке розмежування
        BG_MULTI_SUBROW = _WARN_BG  # підзамовлення — тепле розмежування
        BG_COMP = _SOFT_I  # компенсація — акцентний фон
        BG_OPEN = _OK_BG  # початковий баланс — світло-зелений

        def _ccy_symbol(ccy: str) -> str:
            c = str(ccy or "").upper().strip()
            return {
                "USD": "$",
                "UAH": "₴",
                "EUR": "€",
                "PLN": "zł",
                "CNY": "¥",
            }.get(c, c)

        cols = [
            (self.t("payments.col.invoice", "Інвойс"), 90),  # 0
            (self.t("payments.col.order1c", "ЗП обліку"), 100),  # 1
            (self.t("payments.col.status", "Статус"), 100),  # 2
            (self.t("payments.col.amount", "Вартість"), 80),  # 3
            # Hide USD column when supplier currency is already USD.
            (self.t("payments.col.amount_usd", "Вартість $"), 0 if supplier_ccy == "USD" else 80),  # 4
            (self.t("payments.col.payment", "Сплата"), 90),  # 5
            (self.t("payments.col.transferred", "Передано"), 90),  # 6  ← NEW
            (self.t("payments.col.paid", "Сплачено"), 90),  # 7
            (self.t("payments.col.product", "Товар"), 350),  # 8
            (self.t("payments.col.comment", "Коментар"), 160),  # 9
            (self.t("payments.col.delivery", "Тип доставки"), 80),  # 10
            (self.t("payments.col.unit_cost", "С/В"), 80),  # 11
            (" ", 90),  # 12  expand / "+" for orders with multiple line items
        ]
        visible_keys = set(self._visible_column_keys_set())
        col_keys = [
            "invoice",
            "order1c",
            "status",
            "amount",
            "amount_usd",
            "payment",
            "transferred",  # 6  ← NEW
            "paid",  # 7
            "product",  # 8
            "comment",  # 9
            "delivery",  # 10
            "unit_cost",  # 11
            "expand",  # 12
        ]
        order_keys = list(self._all_column_keys())
        _cw = self._table_col_width_overrides or {}
        header_map = {
            "invoice": (self.t("payments.col.invoice", "Інвойс"), int(_cw.get("invoice") or 60)),
            "order1c": (self.t("payments.col.order1c", "ЗП обліку"), int(_cw.get("order1c") or 60)),
            "status": (self.t("payments.col.status", "Статус"), int(_cw.get("status") or 100)),
            "qty": (self.t("payments.col.qty", "Кільк."), int(_cw.get("qty") or 50)),
            "amount": (self.t("payments.col.amount", "Вартість"), int(_cw.get("amount") or 80)),
            "amount_usd": (
                self.t("payments.col.amount_usd", "Вартість $"),
                0 if supplier_ccy == "USD" else int(_cw.get("amount_usd") or 80),
            ),
            "payment": (self.t("payments.col.payment", "Сплата"), int(_cw.get("payment") or 100)),
            "transferred": (self.t("payments.col.transferred", "Передано"), int(_cw.get("transferred") or 90)),
            "paid": (self.t("payments.col.paid", "Сплачено"), int(_cw.get("paid") or 100)),
            "product": (self.t("payments.col.product", "Товар"), int(_cw.get("product") or 250)),
            "comment": (self.t("payments.col.comment", "Коментар"), int(_cw.get("comment") or 180)),
            "delivery": (self.t("payments.col.delivery", "Тип доставки"), int(_cw.get("delivery") or 80)),
            "unit_cost": (self.t("payments.col.unit_cost", "С/В"), 0),
        }
        ordered_core = [k for k in order_keys if k in header_map]
        cols = [header_map[k] for k in ordered_core] + [
            (" ", int(_cw.get("expand") or 60))
        ]
        col_keys = ordered_core + ["expand"]
        adjusted_cols = []
        for idx, (label, width) in enumerate(cols):
            key = col_keys[idx] if idx < len(col_keys) else ""
            effective_width = int(width or 0)
            if key not in {"expand", ""} and key not in visible_keys:
                effective_width = 0
            adjusted_cols.append((label, effective_width))
        cols = adjusted_cols
        # Key-to-width lookup so row-building code stays stable when columns
        # are reordered or new ones are inserted.
        _wcol = {col_keys[i]: cols[i][1] for i in range(len(col_keys))}

        def _text_cell(
            txt: str,
            w: int,
            bold: bool = False,
            bgc: str = BG_A,
            color: str = _TEXT,
            h: int = ROW_H,
            wrap: bool = True,
        ):
            if int(w or 0) <= 0:
                # Fully hide zero-width columns (e.g., "Вартість $" when supplier currency is USD).
                return ft.Container(visible=False, width=0, height=0)
            return ft.Container(
                width=w,
                height=h,
                bgcolor=bgc,
                border=ft.border.all(1, BDR),
                padding=ft.padding.all(0),
                alignment=ft.Alignment(0, 0),
                content=ft.Text(
                    format_maybe_numeric_text(str(txt or "")),
                    size=TXT,
                    weight="bold" if bold else "w400",
                    color=color,
                    text_align=ft.TextAlign.CENTER,
                    no_wrap=False,
                    max_lines=None,
                    overflow=ft.TextOverflow.VISIBLE,
                ),
            )

        def _ctrl_cell(ctrl: ft.Control, w: int, bgc: str = BG_A, h: int = ROW_H):
            if int(w or 0) <= 0:
                return ft.Container(visible=False, width=0, height=0)
            return ft.Container(
                width=w,
                height=h,
                bgcolor=bgc,
                border=ft.border.all(1, BDR),
                padding=0,
                alignment=ft.Alignment(0, 0),
                content=ctrl,
            )

        def _prod_ctrl_cell(ctrl: ft.Control, pid_val: str, w: int, bgc: str, h: int):
            """Product ctrl cell with an optional quick-view icon overlay."""
            if int(w or 0) <= 0:
                return ft.Container(visible=False, width=0, height=0)
            pid_val = str(pid_val or "").strip()
            if not pid_val:
                return _ctrl_cell(ctrl, w, bgc, h)
            from views.product_quick_view import product_icon_btn
            _btn = product_icon_btn(
                pid_val, self.content_area, self.db, self.user_data, size=11
            )
            return ft.Container(
                width=w,
                height=h,
                bgcolor=bgc,
                border=ft.border.all(1, BDR),
                padding=0,
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
                content=ft.Stack(
                    [
                        ctrl,
                        ft.Container(
                            content=_btn,
                            right=1,
                            top=max(0, (h - 22) // 2),
                        ),
                    ],
                    width=w,
                    height=h,
                ),
            )

        def _tf(
            value: str,
            w: int,
            read_only: bool = False,
            suffix: str = "",
            h: int = ROW_H,
            multiline: bool = False,
        ):
            # Do not use `suffix`: it adds extra padding/space and can misalign text.
            # Instead, append the currency symbol directly with a narrow no-break space.
            v = str(value or "")
            sfx = _ccy_symbol(suffix) if str(suffix or "").strip() else ""
            if v and sfx:
                v = f"{v}\u202f{sfx}"
            # Flet TextField (especially multiline) can appear top-aligned in tall rows.
            # For single-line fields, add dynamic vertical padding so text stays centered.
            try:
                h_i = int(h or ROW_H)
            except Exception:
                h_i = ROW_H
            # Keep TextField rendering stable (dense=True) but center content in tall rows
            # using controlled vertical padding.
            try:
                vpad = 0 if multiline else max(0, int((h_i - (int(TXT) + 6)) / 2))
            except Exception:
                vpad = 0
            return ft.TextField(
                value=v,
                width=w,
                height=h,
                text_size=TXT,
                border=ft.InputBorder.NONE,
                # Keep strict centering (vertical padding can visually "push" the text off-center).
                content_padding=ft.padding.symmetric(horizontal=0, vertical=vpad),
                dense=True,
                text_align=ft.TextAlign.CENTER,
                text_vertical_align=ft.VerticalAlignment.CENTER,
                read_only=read_only or self.is_read_only,
                suffix=None,
                multiline=bool(multiline),
                min_lines=(1 if multiline else None),
                max_lines=(5 if multiline else 1),
            )

        def _editable_amount_cell(
            settlement_s: dict, current_amt: float, w: int, bgc: str, color: str
        ) -> ft.Container:
            """Inline-editable amount cell for balance / compensation rows."""
            if int(w or 0) <= 0:
                return ft.Container(visible=False, width=0, height=0)
            raw_val = str(current_amt) if current_amt else ""

            def _save_amount(e):
                raw = (
                    str(getattr(e.control, "value", "") or "").strip().replace(",", ".")
                )
                if raw == "":
                    new_val = 0.0
                else:
                    new_val = _safe_float(raw, None)
                    if new_val is None:
                        return
                import datetime as _dtmod

                payload = dict(settlement_s)
                payload["amount"] = float(new_val)
                payload["updated_at"] = _dtmod.datetime.utcnow().isoformat()
                self._persist_settlement_async(payload, rebuild_after=True)
                # Миттєво оновлюємо баланс/лічильники без очікування async refresh
                with contextlib.suppress(Exception):
                    self._update_balance_display()
                with contextlib.suppress(Exception):
                    self._update_supplier_balance_refs()

            try:
                vpad = max(0, int((int(ROW_H) - (int(TXT) + 6)) / 2))
            except Exception:
                vpad = 0
            tf = ft.TextField(
                value=raw_val,
                width=w,
                height=ROW_H,
                text_size=TXT,
                color=color,
                border=ft.InputBorder.NONE,
                content_padding=ft.padding.symmetric(horizontal=4, vertical=vpad),
                dense=True,
                text_align=ft.TextAlign.CENTER,
                text_vertical_align=ft.VerticalAlignment.CENTER,
                bgcolor=bgc,
                read_only=self.is_read_only,
                on_submit=_save_amount,
                on_blur=_save_amount,
            )
            return ft.Container(
                width=w,
                height=ROW_H,
                bgcolor=bgc,
                border=ft.border.all(1, BDR),
                padding=0,
                content=tf,
            )

        def _pm(
            value: str,
            w: int,
            options: list,
            on_change,
            bgc: str,
            allow_wrap: bool = True,
            text_color: str = _TEXT,
            h: int = ROW_H,
            get_style=None,
        ):
            opt_map = {
                str(getattr(opt, "key", "") or ""): str(
                    getattr(opt, "text", "") or getattr(opt, "key", "") or ""
                )
                for opt in (options or [])
            }
            current = str(value or "")
            selected_label = opt_map.get(current, current)

            lbl = ft.Text(
                str(selected_label or ""),
                size=TXT,
                color=text_color,
                text_align=ft.TextAlign.CENTER,
                no_wrap=False,
                max_lines=None,
                overflow=ft.TextOverflow.VISIBLE,
            )
            cell = ft.Container(
                width=max(int((w or 80) - 6), 40),
                height=h,
                bgcolor=bgc,
                alignment=ft.Alignment(0, 0),
                padding=ft.padding.all(0),
                content=lbl,
            )

            def _pick(k):
                lbl.value = opt_map.get(k, k)
                if get_style is not None:
                    with contextlib.suppress(Exception):
                        new_bg, new_fg = get_style(k)
                        cell.bgcolor = new_bg
                        lbl.color = new_fg
                with contextlib.suppress(Exception):
                    lbl.update()
                with contextlib.suppress(Exception):
                    cell.update()
                on_change(k)

            menu_items = [
                ft.PopupMenuItem(
                    content=opt_map.get(
                        str(getattr(opt, "key", "") or ""),
                        str(getattr(opt, "key", "") or ""),
                    ),
                    on_click=(
                        lambda _e, k=str(getattr(opt, "key", "") or ""): _pick(k)
                    ),
                )
                for opt in (options or [])
            ]

            return ft.PopupMenuButton(
                icon=None,
                items=menu_items,
                menu_position=ft.PopupMenuPosition.UNDER,
                disabled=self.is_read_only,
                content=cell,
            )

        header_row = ft.Row(
            [
                _text_cell(t, w, bold=True, bgc=BG_HDR, color=_CHIP_T, h=HDR_H)
                for t, w in cols
            ],
            spacing=0,
        )

        status_opts = [
            ("production", self.t("payments.status.production", "Виробництво")),
            ("ready_for_shipment", self.t("orders.status.ready_for_shipment", "Готовий")),
            ("in_transit", self.t("orders.status.in_transit", "В дорозі")),
            ("at_warehouse", self.t("orders.status.at_warehouse", "На складі")),
            ("accepted", self.t("orders.status.accepted", "Прийнятий")),
        ]
        payment_opts = [
            ("unpaid", self.t("orders.payment.unpaid_full", "Не сплачений")),
            ("advance", self.t("orders.payment.advance", "Аванс")),
            ("transferred", self.t("orders.payment.transferred", "Передано")),
            ("paid", self.t("orders.payment.paid_full", "Сплачений")),
        ]

        def _delivery_opts() -> list[tuple[str, str]]:
            return [(k, str(v or k)) for k, v in delivery_by_id.items() if k]

        def _queue_commit(factory):
            # Inline edits must commit immediately to keep typing/dropdowns snappy.
            factory()

        def _status_label(raw: str) -> str:
            for k, lab in status_opts:
                if k == raw:
                    return str(lab)
            return raw or ""

        def _item_rows_for_order(o: dict, order_idx: int = 0) -> list:
            # Default header row height. Must be defined early because nested closures may
            # reference it even if later calculations short-circuit on edge cases.
            row_h_hdr = ROW_H

            def _need_lines(txt: str, w: int) -> int:
                width = int(w or 0)
                if width <= 0:
                    return 1
                s = "-" if txt is None else str(txt)
                # Approx: ~7px per char at TXT≈12
                chars_per_line = max(4, int(width // 7))
                needed = (len(s) + chars_per_line - 1) // chars_per_line
                return max(1, min(8, int(needed)))

            def _strip_pname(raw_cmt: str, pname: str) -> str:
                """Strip product_name prefix from item_comment (stored by legacy code)."""
                raw = str(raw_cmt or "").strip()
                pn = str(pname or "").strip()
                if pn and raw:
                    prefix = pn + "; "
                    if raw.lower().startswith(prefix.lower()):
                        return raw[len(prefix) :]
                    if raw.lower() == pn.lower():
                        return ""
                return raw

            # Dynamic height, but applied to the WHOLE row (all columns share `h=row_h`).
            def _row_h_for(o_row: dict, it_row: dict | None = None) -> int:
                it0 = it_row if isinstance(it_row, dict) else {}
                max_lines = max(
                    _need_lines(str(o_row.get("invoice_no") or ""), _wcol.get("invoice", 0)),
                    _need_lines(str(o_row.get("order_no_1c") or ""), _wcol.get("order1c", 0)),
                    _need_lines(
                        str(_status_label(str(o_row.get("status") or ""))), _wcol.get("status", 0)
                    ),
                    _need_lines(str(o_row.get("comment") or ""), _wcol.get("comment", 0)),
                    _need_lines(
                        str(o_row.get("delivery_type_name") or ""), _wcol.get("delivery", 0)
                    ),
                    _need_lines(str(it0.get("product_name") or ""), _wcol.get("product", 0)),
                    _need_lines(
                        _strip_pname(
                            str(it0.get("item_comment") or ""),
                            str(it0.get("product_name") or ""),
                        ),
                        _wcol.get("comment", 0),
                    ),
                )
                # Keep vertical slack minimal (comment column used to make rows too tall).
                return (
                    int(max(ROW_H, min(220, 16 * max_lines + 4)))
                    if max_lines > 1
                    else ROW_H
                )

            def _mk_shared_controls(h: int):
                inv_tf = _tf(o.get("invoice_no"), _wcol.get("invoice", 0), h=h)
                zp_tf = _tf(o.get("order_no_1c"), _wcol.get("order1c", 0), h=h)
                comment_tf = _tf(o.get("comment"), _wcol.get("comment", 0), h=h, multiline=True)
                return inv_tf, zp_tf, comment_tf

            items = o.get("items") if isinstance(o.get("items"), list) else []
            if not items:
                # Legacy / flattened orders: keep single-line orders usable in this view.
                items = [
                    {
                        "product_id": str(o.get("product_id") or "").strip(),
                        "product_name": str(o.get("product_name") or "").strip(),
                        "qty": o.get("qty") or o.get("quantity") or "",
                        "purchase_price": o.get("purchase_price") or "",
                        "purchase_currency": o.get("purchase_currency")
                        or o.get("currency")
                        or "",
                        "amount_manual_override": o.get("amount_manual_override") or "",
                        "item_comment": o.get("comment") or "",
                    }
                ]

            # Product dropdown must be scoped to current supplier (within current region).
            # `self._products` is region-filtered in build(); filter further by supplier_id here.
            def _prod_supplier_id(p: dict) -> str:
                if not isinstance(p, dict):
                    return ""
                return str(
                    p.get("supplier_id")
                    or p.get("supplierId")
                    or p.get("supplier")
                    or ""
                ).strip()

            filtered_products = [
                p
                for p in (self._products or [])
                if isinstance(p, dict)
                and (p.get("id") or "")
                and (_prod_supplier_id(p) == str(supplier_id or "").strip())
            ]
            # Fallback: if catalog rows don't carry supplier_id in this deployment,
            # build minimal options from existing supplier orders to keep the list relevant.
            if not filtered_products:
                try:
                    seen = set()
                    derived = []
                    for _o in orders_for_supplier or []:
                        its = (
                            (_o or {}).get("items")
                            if isinstance((_o or {}).get("items"), list)
                            else []
                        )
                        if not its:
                            its = [
                                {
                                    "product_id": (_o or {}).get("product_id"),
                                    "product_name": (_o or {}).get("product_name"),
                                }
                            ]
                        for _it in its:
                            if not isinstance(_it, dict):
                                continue
                            pid0 = str(_it.get("product_id") or "").strip()
                            if not pid0 or pid0 in seen:
                                continue
                            seen.add(pid0)
                            derived.append(
                                {
                                    "id": pid0,
                                    "name": str(_it.get("product_name") or pid0),
                                }
                            )
                    filtered_products = derived
                except Exception:
                    filtered_products = []

            prod_opts = [
                ft.dropdown.Option(str(p.get("id") or ""), str(p.get("name") or ""))
                for p in (filtered_products or [])
                if isinstance(p, dict) and (p.get("id") or "")
            ]

            paid_stored = str(
                o.get("paid_amount") if o.get("paid_amount") is not None else ""
            )
            base_inv = str(o.get("invoice_no") or "").strip()
            base_zp = str(o.get("order_no_1c") or "").strip()

            # --- single visible row (same as before): one editable line + empty expand slot ---
            if len(items) <= 1:
                idx, it = 0, items[0] if items else {}
                bg = BG_A if order_idx % 2 == 0 else BG_B
                row_h = _row_h_for(o, it if isinstance(it, dict) else {})
                inv_tf_shared, zp_tf_shared, comment_tf_shared = _mk_shared_controls(
                    row_h
                )
                inv_ctrl = inv_tf_shared
                zp_ctrl = zp_tf_shared
                it0_qty = it if isinstance(it, dict) else {}
                qty_raw = str(it0_qty.get("qty") or it0_qty.get("quantity") or "")
                qty_tf = _tf(
                    format_grouped(qty_raw) if qty_raw.strip() else "",
                    _wcol.get("qty", 0),
                    h=row_h,
                    multiline=False,
                )
                transferred_stored = str(o.get("transferred_amount") or "")
                transferred_tf = _tf(
                    (
                        format_grouped(transferred_stored)
                        if transferred_stored.strip()
                        else ""
                    ),
                    _wcol.get("transferred", 0),
                    h=row_h,
                )
                _paid_has_value = bool(str(o.get("paid_amount") or "").strip())
                _transferred_has_value = bool(transferred_stored.strip())
                # Червона заливка клітинки "Сплачено" якщо є "Передано" але немає "Сплачено"
                _paid_cell_bg = (
                    _ERR_BG
                    if (_transferred_has_value and not _paid_has_value)
                    else None
                )
                paid_tf = _tf(
                    format_grouped(paid_stored) if paid_stored.strip() else "",
                    _wcol.get("paid", 0),
                    h=row_h,
                )
                comment_ctrl = comment_tf_shared
                current_status = str(o.get("status") or "production")
                current_payment = str(o.get("payment_status") or "unpaid")
                current_delivery = str(o.get("delivery_type_id") or "")

                pid = str((it or {}).get("product_id") or "").strip()
                _fresh_pname = str(
                    next(
                        (
                            p.get("name", "")
                            for p in (self._products or [])
                            if str((p or {}).get("id") or "") == pid
                        ),
                        "",
                    )
                ).strip()
                pname = (
                    _fresh_pname or str((it or {}).get("product_name") or "").strip()
                )
                local_prod_opts = list(prod_opts)
                if pid and not any(
                    str(getattr(opt, "key", "")) == pid for opt in local_prod_opts
                ):
                    local_prod_opts.insert(0, ft.dropdown.Option(pid, pname or pid))
                commit_state = {
                    "status": current_status,
                    "payment": current_payment,
                    "delivery": current_delivery,
                    "product": pid or "",
                }

                sc = (supplier_ccy or "USD").upper().strip() or "USD"
                computed_amt = None
                try:
                    it0 = it if isinstance(it, dict) else {}
                    q = (
                        _safe_float(it0.get("qty") or it0.get("quantity") or 0, 0.0)
                        or 0.0
                    )
                    p = _safe_float(it0.get("purchase_price"), None)
                    ccy = str(it0.get("purchase_currency") or sc).upper().strip() or sc
                    if p is None:
                        pid0 = str(it0.get("product_id") or "").strip()
                        prod0 = next(
                            (
                                p
                                for p in (self._products or [])
                                if str((p or {}).get("id") or "") == pid0
                            ),
                            None,
                        )
                        p = _safe_float((prod0 or {}).get("purchase_price"), None)
                        ccy = (
                            str((prod0 or {}).get("purchase_currency") or ccy or "")
                            .upper()
                            .strip()
                            or ccy
                        )
                    if p is not None and ccy == sc:
                        computed_amt = float(q) * float(p)
                except Exception:
                    computed_amt = None
                order_m = _safe_float(o.get("amount_manual_override"), None)
                it_override = _safe_float(
                    (it or {}).get("amount_manual_override"), None
                )
                if order_m is not None:
                    amt_val = _fmt_money(order_m)
                elif it_override is not None:
                    amt_val = _fmt_money(it_override)
                else:
                    amt_val = (
                        _fmt_money(computed_amt) if computed_amt is not None else ""
                    )
                # Keep numeric cells single-line so they stay vertically centered in tall rows.
                amount_tf = _tf(
                    amt_val, _wcol.get("amount", 0), suffix=supplier_ccy, h=row_h, multiline=False
                )

                # Always allow recalculation/override regardless of status/payment.
                uc_override = _safe_float(
                    (it or {}).get("unit_cost_manual_override"), None
                )
                if uc_override is not None:
                    unit_cost_tf = _tf(
                        _fmt_money(uc_override), _wcol.get("unit_cost", 0), suffix="USD", h=row_h
                    )
                else:
                    display = str((it or {}).get("unit_cost_display") or "").strip()
                    if not display:
                        # Prefer numeric calc (stored from OrdersView) and format like Orders.
                        uc_num = _safe_float((it or {}).get("unit_cost_calc_usd"), None)
                        display = (
                            _fmt_money(uc_num)
                            if uc_num is not None
                            else str((it or {}).get("unit_cost_calc_usd") or "").strip()
                        )
                    if not display:
                        # Fallback: compute on the fly if OrdersView hasn't persisted calc yet.
                        uc_calc = self._item_unit_cost_usd(
                            o, it if isinstance(it, dict) else {}, supplier_ccy
                        )
                        display = _fmt_money(uc_calc) if uc_calc is not None else ""
                    unit_cost_tf = _tf(display, _wcol.get("unit_cost", 0), suffix="USD", h=row_h)

                def _commit(item_index=0, _paid_f=paid_tf):
                    if item_index == 0:
                        _new_inv = str(inv_tf_shared.value or "").strip()
                        _old_inv = str(o.get("invoice_no") or "").strip()
                        if _new_inv and _new_inv != _old_inv:
                            _rid = str(o.get("region_id") or "").strip() or None
                            _oid = str(o.get("id") or "").strip()
                            if self._is_invoice_duplicate(
                                _new_inv, _rid, exclude_order_id=_oid
                            ):
                                self._show_snack(
                                    self.t("orders.err.invoice_exists", "Інвойс {inv!r} вже існує в цьому регіоні").format(inv=_new_inv),
                                    _ERR,
                                )
                                return
                        o["invoice_no"] = _new_inv
                        o["order_no_1c"] = str(zp_tf_shared.value or "").strip()
                        o["comment"] = str(comment_tf_shared.value or "")
                    o["status"] = str(
                        commit_state.get("status") or "production"
                    ).strip()
                    o["payment_status"] = str(
                        commit_state.get("payment") or "unpaid"
                    ).strip()
                    o["paid_amount"] = str(_paid_f.value or "")
                    did = str(commit_state.get("delivery") or "").strip()
                    o["delivery_type_id"] = did
                    o["delivery_type_name"] = str(
                        delivery_by_id.get(did) or o.get("delivery_type_name") or ""
                    )

                    items_local = (
                        o.get("items") if isinstance(o.get("items"), list) else []
                    )
                    while len(items_local) < (item_index + 1):
                        items_local.append({})
                    items_local[item_index] = dict(items_local[item_index] or {})
                    new_pid = str(commit_state.get("product") or "").strip()
                    items_local[item_index]["product_id"] = new_pid
                    prod = next(
                        (
                            p
                            for p in (self._products or [])
                            if str((p or {}).get("id") or "") == new_pid
                        ),
                        None,
                    )
                    items_local[item_index]["product_name"] = (prod or {}).get(
                        "name"
                    ) or (items_local[item_index].get("product_name") or "")
                    v_amt = _safe_float(amount_tf.value, None)
                    items_local[item_index]["amount_manual_override"] = (
                        _fmt_money(v_amt) if v_amt is not None else ""
                    )
                    v_uc = _safe_float(unit_cost_tf.value, None)
                    items_local[item_index]["unit_cost_manual_override"] = (
                        _fmt_money(v_uc) if v_uc is not None else ""
                    )
                    o["items"] = items_local

                    try:
                        if (
                            isinstance(o.get("items"), list)
                            and o["items"]
                            and isinstance(o["items"][0], dict)
                        ):
                            o["items"][0]["paid_amount"] = str(
                                o.get("paid_amount") or ""
                            )
                    except Exception:
                        pass

                    self._persist_order_async(o, rebuild_after=True)

                st_ctrl = _pm(
                    current_status,
                    _wcol.get("status", 0),
                    [ft.dropdown.Option(k, v) for k, v in status_opts],
                    on_change=(
                        lambda v, st=commit_state, c=_commit: (
                            st.__setitem__("status", v),
                            _queue_commit(lambda ci=c: ci()),
                        )
                    ),
                    bgc=bg,
                    h=row_h,
                )
                pay_bg, pay_fg = self._payment_colors(current_payment)
                pay_ctrl = _pm(
                    current_payment,
                    _wcol.get("payment", 0),
                    [ft.dropdown.Option(k, v) for k, v in payment_opts],
                    on_change=(
                        lambda v, st=commit_state, c=_commit: (
                            st.__setitem__("payment", v),
                            _queue_commit(lambda ci=c: ci()),
                        )
                    ),
                    bgc=pay_bg,
                    text_color=pay_fg,
                    h=row_h,
                    get_style=lambda k: self._payment_colors(k),
                )
                del_ctrl = (
                    _pm(
                        current_delivery,
                        _wcol.get("delivery", 0),
                        [ft.dropdown.Option(k, v) for k, v in _delivery_opts()],
                        on_change=(
                            lambda v, st=commit_state, c=_commit: (
                                st.__setitem__("delivery", v),
                                _queue_commit(lambda ci=c: ci()),
                            )
                        ),
                        bgc=bg,
                        allow_wrap=True,
                        h=row_h,
                    )
                    if _delivery_opts()
                    else _text_cell(
                        str(o.get("delivery_type_name") or ""),
                        _wcol.get("delivery", 0),
                        bgc=bg,
                        wrap=True,
                    )
                )
                prod_ctrl = _pm(
                    pid or "",
                    _wcol.get("product", 0),
                    local_prod_opts,
                    on_change=(
                        lambda v, st=commit_state, c=_commit: (
                            st.__setitem__("product", v),
                            _queue_commit(lambda ci=c: ci()),
                        )
                    ),
                    bgc=bg,
                    h=row_h,
                )

                def _on_field_blur(_e, c=_commit):
                    _queue_commit(lambda ci=c: ci())

                def _norm_amount(v) -> str:
                    """Normalize amount string for comparison: '10'000' → '10000'."""
                    s = str(v or "").strip().replace("'", "").replace(",", ".")
                    try:
                        f = float(s)
                        return str(int(f)) if f == int(f) else str(f)
                    except ValueError:
                        return s

                def _fmt_after_save(tf, raw: str) -> None:
                    """Update TextField display to apostrophe-formatted value."""
                    formatted = format_grouped(raw) if raw else ""
                    if formatted != str(tf.value or ""):
                        with contextlib.suppress(
                            Exception
                        ):  # guard against frozen controls
                            tf.value = formatted
                        with contextlib.suppress(Exception):
                            tf.update()

                # Mutable ref so both handlers and row builder share the same pointer
                _paid_cell_ref: list = [None]

                def _update_paid_cell_bg(_o_state=o, _ref=_paid_cell_ref, _base=bg):
                    """Instantly redraw paid-cell background — red when transferred but not paid."""
                    cell = _ref[0]
                    if cell is None:
                        return
                    has_paid = bool(
                        _norm_amount(str(_o_state.get("paid_amount") or ""))
                    )
                    has_transferred = bool(
                        _norm_amount(str(_o_state.get("transferred_amount") or ""))
                    )
                    new_bg = _ERR_BG if (has_transferred and not has_paid) else _base
                    if cell.bgcolor != new_bg:
                        try:
                            cell.bgcolor = new_bg
                        except Exception:
                            return
                        with contextlib.suppress(Exception):
                            cell.update()

                def _on_transferred_blur(_e, _o=o, _tf=transferred_tf):
                    """Швидкий патч — зберігає тільки transferred_amount."""
                    val_raw = _norm_amount(_tf.value)
                    old_raw = _norm_amount(_o.get("transferred_amount"))
                    if val_raw == old_raw:
                        _fmt_after_save(_tf, val_raw)
                        _update_paid_cell_bg()
                        return
                    _o["transferred_amount"] = val_raw
                    # rebuild_after=False: no full section rebuild needed — in-place updates below
                    self._persist_order_patch(
                        str(_o.get("id") or ""),
                        {"transferred_amount": val_raw},
                        rebuild_after=False,
                    )
                    with contextlib.suppress(Exception):
                        self._update_balance_display()
                    _fmt_after_save(_tf, val_raw)
                    _update_paid_cell_bg()

                def _on_paid_blur(_e, _o=o, _paid_f=paid_tf):
                    """Швидкий патч — зберігає тільки paid_amount без повного commit."""
                    paid_raw = _norm_amount(_paid_f.value)
                    old_raw = _norm_amount(_o.get("paid_amount"))
                    if paid_raw == old_raw:
                        _fmt_after_save(_paid_f, paid_raw)
                        _update_paid_cell_bg()
                        return
                    _o["paid_amount"] = paid_raw
                    with contextlib.suppress(Exception):
                        if (
                            isinstance(_o.get("items"), list)
                            and _o["items"]
                            and isinstance(_o["items"][0], dict)
                        ):
                            _o["items"][0]["paid_amount"] = paid_raw
                    # rebuild_after=False: TextField format + cell color updated in-place below
                    self._persist_order_patch(
                        str(_o.get("id") or ""),
                        {"paid_amount": paid_raw},
                        rebuild_after=False,
                    )
                    with contextlib.suppress(Exception):
                        self._update_balance_display()
                    # Apostrophe format = visual confirmation: saved to cache
                    _fmt_after_save(_paid_f, paid_raw)
                    _update_paid_cell_bg()

                def _on_qty_blur(
                    _e,
                    _o=o,
                    _it=it,
                    _item_idx=idx,
                    _qty_f=qty_tf,
                    _amt_f=amount_tf,
                    _ccy=sc,
                ):
                    raw = str(_qty_f.value or "").strip().replace("'", "").replace(",", ".")
                    old_raw = str((_it or {}).get("qty") or "").strip()
                    if raw == old_raw:
                        return
                    new_qty = _safe_float(raw, None)
                    if new_qty is None and raw:
                        return
                    items_local = (
                        _o.get("items") if isinstance(_o.get("items"), list) else []
                    )
                    while len(items_local) <= _item_idx:
                        items_local.append({})
                    items_local[_item_idx] = dict(items_local[_item_idx] or {})
                    items_local[_item_idx]["qty"] = new_qty if new_qty is not None else ""
                    _o["items"] = items_local
                    # Recalculate amount if no manual override
                    order_m = _safe_float(_o.get("amount_manual_override"), None)
                    it_m = _safe_float(items_local[_item_idx].get("amount_manual_override"), None)
                    if order_m is None and it_m is None:
                        new_amt = self._line_amount_supplier_ccy(
                            _o, items_local[_item_idx], _item_idx, supplier_ccy
                        )
                        if new_amt is not None and _amt_f is not None:
                            _amt_f.value = _fmt_money(new_amt)
                            with contextlib.suppress(Exception):
                                _amt_f.update()
                    _fmt_after_save(_qty_f, raw)
                    self._persist_order_async(_o, rebuild_after=False)

                qty_tf.on_blur = _on_qty_blur
                qty_tf.on_submit = _on_qty_blur
                transferred_tf.on_blur = _on_transferred_blur
                transferred_tf.on_submit = _on_transferred_blur
                paid_tf.on_blur = _on_paid_blur
                paid_tf.on_submit = _on_paid_blur
                amount_tf.on_blur = _on_field_blur
                amount_tf.on_submit = _on_field_blur
                unit_cost_tf.on_blur = _on_field_blur
                unit_cost_tf.on_submit = _on_field_blur
                for c in [inv_tf_shared, zp_tf_shared, comment_tf_shared]:
                    c.on_blur = _on_field_blur
                    c.on_submit = _on_field_blur

                line_sup = self._line_amount_supplier_ccy(o, it, idx, supplier_ccy)
                if sc == "USD":
                    usd_txt = ""
                else:
                    usd_v = self._supplier_amount_usd(line_sup, supplier_ccy, order=o)
                    usd_txt = (
                        f"{_fmt_money(usd_v)} {_ccy_symbol('USD')}"
                        if usd_v is not None
                        else ""
                    )

                spacer = _text_cell("", _wcol.get("expand", 0), bgc=bg, h=row_h)
                _paid_cell = _ctrl_cell(
                    paid_tf, _wcol.get("paid", 0), bgc=_paid_cell_bg or bg, h=row_h
                )
                _paid_cell_ref[0] = _paid_cell
                _row_key = str(o.get("id") or "").strip()
                row = ft.Row(
                    key=_row_key if _row_key else None,
                    controls=[
                        _ctrl_cell(inv_ctrl, _wcol.get("invoice", 0), bgc=bg, h=row_h),
                        _ctrl_cell(zp_ctrl, _wcol.get("order1c", 0), bgc=bg, h=row_h),
                        _ctrl_cell(st_ctrl, _wcol.get("status", 0), bgc=bg, h=row_h),
                        _ctrl_cell(qty_tf, _wcol.get("qty", 0), bgc=bg, h=row_h),
                        _ctrl_cell(amount_tf, _wcol.get("amount", 0), bgc=bg, h=row_h),
                        _text_cell(usd_txt, _wcol.get("amount_usd", 0), bgc=bg, color=_TEXT, h=row_h),
                        _ctrl_cell(pay_ctrl, _wcol.get("payment", 0), bgc=bg, h=row_h),
                        _ctrl_cell(transferred_tf, _wcol.get("transferred", 0), bgc=bg, h=row_h),
                        _paid_cell,
                        _prod_ctrl_cell(prod_ctrl, pid, _wcol.get("product", 0), bg, row_h),
                        _ctrl_cell(comment_ctrl, _wcol.get("comment", 0), bgc=bg, h=row_h),
                        (
                            _ctrl_cell(del_ctrl, _wcol.get("delivery", 0), bgc=bg, h=row_h)
                            if _delivery_opts()
                            else _text_cell(
                                str(o.get("delivery_type_name") or ""),
                                _wcol.get("delivery", 0),
                                bgc=bg,
                                h=row_h,
                            )
                        ),
                        _ctrl_cell(unit_cost_tf, _wcol.get("unit_cost", 0), bgc=bg, h=row_h),
                        spacer,
                    ],
                    spacing=0,
                )
                return [row]

            # --- multiple line items: summary row + expandable detail rows (003/01, ...) ---
            order_uid = str(o.get("id") or "").strip() or str(id(o))

            header_cs = {
                "status": str(o.get("status") or "production"),
                "payment": str(o.get("payment_status") or "unpaid"),
                "delivery": str(o.get("delivery_type_id") or ""),
            }
            bgp = BG_A if order_idx % 2 == 0 else BG_B
            row_h_hdr = _row_h_for(
                o, items[0] if items and isinstance(items[0], dict) else {}
            )
            inv_tf_shared, zp_tf_shared, comment_tf_shared = _mk_shared_controls(
                row_h_hdr
            )
            _hdr_transferred_stored = str(o.get("transferred_amount") or "")
            transferred_tf_header = _tf(
                _hdr_transferred_stored, _wcol.get("transferred", 0), h=row_h_hdr
            )
            _hdr_paid_has = bool(str(o.get("paid_amount") or "").strip())
            _hdr_trans_has = bool(_hdr_transferred_stored.strip())
            _hdr_paid_bg = _ERR_BG if (_hdr_trans_has and not _hdr_paid_has) else None
            paid_tf_header = _tf(paid_stored, _wcol.get("paid", 0), h=row_h_hdr)
            sc_g = (supplier_ccy or "USD").upper().strip() or "USD"
            sum_sup = self._order_amount_supplier_ccy(o, supplier_ccy)
            sum_txt = (
                f"{_fmt_money(sum_sup)} {_ccy_symbol(supplier_ccy)}"
                if sum_sup is not None
                else ""
            )
            total_qty = sum(
                _safe_float((it or {}).get("qty") or (it or {}).get("quantity") or 0, 0.0) or 0.0
                for it in (items if isinstance(items, list) else [])
            )
            total_qty_txt = format_grouped(str(int(total_qty))) if total_qty else ""

            sum_usd = None
            if sc_g != "USD":
                if sum_sup is not None:
                    sum_usd = self._supplier_amount_usd(sum_sup, supplier_ccy, order=o)
            sum_usd_txt = _fmt_money(sum_usd) if sum_usd is not None else ""
            if sum_usd is not None:
                sum_usd_txt = f"{sum_usd_txt} {_ccy_symbol('USD')}"

            def _save_header():
                _new_inv = str(inv_tf_shared.value or "").strip()
                _old_inv = str(o.get("invoice_no") or "").strip()
                if _new_inv and _new_inv != _old_inv:
                    _rid = str(o.get("region_id") or "").strip() or None
                    _oid = str(o.get("id") or "").strip()
                    if self._is_invoice_duplicate(
                        _new_inv, _rid, exclude_order_id=_oid
                    ):
                        self._show_snack(
                            self.t("orders.err.invoice_exists", "Інвойс {inv!r} вже існує в цьому регіоні").format(inv=_new_inv), _ERR
                        )
                        return
                o["invoice_no"] = _new_inv
                o["order_no_1c"] = str(zp_tf_shared.value or "").strip()
                o["comment"] = str(comment_tf_shared.value or "")
                o["status"] = str(header_cs.get("status") or "production").strip()
                o["payment_status"] = str(header_cs.get("payment") or "unpaid").strip()
                o["paid_amount"] = str(paid_tf_header.value or "")
                did = str(header_cs.get("delivery") or "").strip()
                o["delivery_type_id"] = did
                o["delivery_type_name"] = str(
                    delivery_by_id.get(did) or o.get("delivery_type_name") or ""
                )
                try:
                    if (
                        isinstance(o.get("items"), list)
                        and o["items"]
                        and isinstance(o["items"][0], dict)
                    ):
                        o["items"][0]["paid_amount"] = str(o.get("paid_amount") or "")
                except Exception:
                    pass
                self._persist_order_async(o, rebuild_after=True)

            st_hdr = _pm(
                header_cs["status"],
                _wcol.get("status", 0),
                [ft.dropdown.Option(k, v) for k, v in status_opts],
                on_change=(
                    lambda v: (
                        header_cs.__setitem__("status", v),
                        _queue_commit(_save_header),
                    )
                ),
                bgc=bgp,
                h=row_h_hdr,
            )
            pay_hdr = _pm(
                header_cs["payment"],
                _wcol.get("payment", 0),
                [ft.dropdown.Option(k, v) for k, v in payment_opts],
                on_change=(
                    lambda v: (
                        header_cs.__setitem__("payment", v),
                        _queue_commit(_save_header),
                    )
                ),
                bgc=self._payment_colors(header_cs["payment"])[0],
                text_color=self._payment_colors(header_cs["payment"])[1],
                h=row_h_hdr,
                get_style=lambda k: self._payment_colors(k),
            )
            del_hdr = (
                _pm(
                    header_cs["delivery"],
                    _wcol.get("delivery", 0),
                    [ft.dropdown.Option(k, v) for k, v in _delivery_opts()],
                    on_change=(
                        lambda v: (
                            header_cs.__setitem__("delivery", v),
                            _queue_commit(_save_header),
                        )
                    ),
                    bgc=bgp,
                    allow_wrap=True,
                    h=row_h_hdr,
                )
                if _delivery_opts()
                else _text_cell(
                    str(o.get("delivery_type_name") or ""),
                    _wcol.get("delivery", 0),
                    bgc=bgp,
                    wrap=True,
                )
            )

            def _blur_header(_e):
                _queue_commit(_save_header)

            def _on_transferred_hdr_blur(_e, _o=o, _tf=transferred_tf_header):
                """Швидкий патч для transferred_amount у заголовку."""
                val = str(_tf.value or "")
                if val == str(_o.get("transferred_amount") or ""):
                    return
                _o["transferred_amount"] = val
                self._persist_order_patch(
                    str(_o.get("id") or ""),
                    {"transferred_amount": val},
                    rebuild_after=True,
                )

            transferred_tf_header.on_blur = _on_transferred_hdr_blur
            transferred_tf_header.on_submit = _on_transferred_hdr_blur

            def _on_paid_hdr_blur(_e, _o=o, _pf=paid_tf_header):
                """Швидкий патч для paid_amount у заголовку замовлення."""
                paid_val = str(_pf.value or "")
                old_val = str(_o.get("paid_amount") or "")
                if paid_val == old_val:
                    return
                _o["paid_amount"] = paid_val
                with contextlib.suppress(Exception):
                    if (
                        isinstance(_o.get("items"), list)
                        and _o["items"]
                        and isinstance(_o["items"][0], dict)
                    ):
                        _o["items"][0]["paid_amount"] = paid_val
                self._persist_order_patch(
                    str(_o.get("id") or ""),
                    {"paid_amount": paid_val},
                    rebuild_after=True,
                )

            paid_tf_header.on_blur = _on_paid_hdr_blur
            paid_tf_header.on_submit = _on_paid_hdr_blur
            for cx in [inv_tf_shared, zp_tf_shared, comment_tf_shared]:
                cx.on_blur = _blur_header
                cx.on_submit = _blur_header

            # Heights/multiline are set during control creation.

            is_expanded = order_uid in self._expanded_orders

            exp_lbl = ft.Text("−" if is_expanded else "+", size=TXT, weight="bold")

            detail_col = ft.Column(spacing=0, visible=is_expanded)

            def _toggle_expand(_):
                oid = order_uid
                if oid in self._expanded_orders:
                    self._expanded_orders.discard(oid)
                else:
                    self._expanded_orders.add(oid)
                detail_col.visible = oid in self._expanded_orders
                exp_lbl.value = "−" if detail_col.visible else "+"
                pg = self._page()
                if pg:
                    try:
                        pg.update(detail_col, exp_lbl)
                    except Exception:
                        self._schedule_rerender()
                else:
                    self._schedule_rerender()

            expand_btn = ft.TextButton(
                content=exp_lbl,
                width=_wcol.get("expand", 0),
                height=row_h_hdr,
                style=ft.ButtonStyle(padding=ft.padding.all(2)),
                tooltip="Товари",
                on_click=_toggle_expand,
            )

            def _blur_line(ci):
                _queue_commit(ci)

            for ix, it in enumerate(items):
                bg = BG_MULTI_SUBROW
                row_h_line = _row_h_for(o, it if isinstance(it, dict) else {})

                pid = str((it or {}).get("product_id") or "").strip()
                pname = str((it or {}).get("product_name") or "").strip()
                local_prod_opts = list(prod_opts)
                if pid and not any(
                    str(getattr(opt, "key", "")) == pid for opt in local_prod_opts
                ):
                    local_prod_opts.insert(0, ft.dropdown.Option(pid, pname or pid))
                line_cs = {"product": pid or ""}

                computed_amt = None
                try:
                    it0 = it if isinstance(it, dict) else {}
                    q = (
                        _safe_float(it0.get("qty") or it0.get("quantity") or 0, 0.0)
                        or 0.0
                    )
                    p = _safe_float(it0.get("purchase_price"), None)
                    ccy = (
                        str(it0.get("purchase_currency") or sc_g).upper().strip()
                        or sc_g
                    )
                    if p is None:
                        pid0 = str(it0.get("product_id") or "").strip()
                        prod0 = next(
                            (
                                p
                                for p in (self._products or [])
                                if str((p or {}).get("id") or "") == pid0
                            ),
                            None,
                        )
                        p = _safe_float((prod0 or {}).get("purchase_price"), None)
                        ccy = (
                            str((prod0 or {}).get("purchase_currency") or ccy or "")
                            .upper()
                            .strip()
                            or ccy
                        )
                    if p is not None and ccy == sc_g:
                        computed_amt = float(q) * float(p)
                except Exception:
                    computed_amt = None
                it_override = _safe_float(
                    (it or {}).get("amount_manual_override"), None
                )
                amt_val = (
                    _fmt_money(it_override)
                    if it_override is not None
                    else (_fmt_money(computed_amt) if computed_amt is not None else "")
                )
                amount_tf = _tf(
                    amt_val,
                    _wcol.get("amount", 0),
                    suffix=supplier_ccy,
                    h=row_h_line,
                    multiline=False,
                )

                # Always allow recalculation/override regardless of status/payment.
                uc_override = _safe_float(
                    (it or {}).get("unit_cost_manual_override"), None
                )
                if uc_override is not None:
                    unit_cost_tf = _tf(
                        _fmt_money(uc_override), _wcol.get("unit_cost", 0), suffix="USD", h=row_h_line
                    )
                else:
                    display = str((it or {}).get("unit_cost_display") or "").strip()
                    if not display:
                        uc_num = _safe_float((it or {}).get("unit_cost_calc_usd"), None)
                        display = (
                            _fmt_money(uc_num)
                            if uc_num is not None
                            else str((it or {}).get("unit_cost_calc_usd") or "").strip()
                        )
                    if not display:
                        uc_calc = self._item_unit_cost_usd(
                            o, it if isinstance(it, dict) else {}, supplier_ccy
                        )
                        display = _fmt_money(uc_calc) if uc_calc is not None else ""
                    unit_cost_tf = _tf(display, _wcol.get("unit_cost", 0), suffix="USD", h=row_h_line)

                it_qty_raw = str((it or {}).get("qty") or (it or {}).get("quantity") or "")
                qty_line_tf = _tf(
                    format_grouped(it_qty_raw) if it_qty_raw.strip() else "",
                    _wcol.get("qty", 0),
                    h=row_h_line,
                    multiline=False,
                )
                _raw_cmt = str((it or {}).get("item_comment") or "")
                _pn_for_strip = str((it or {}).get("product_name") or "")
                comment_line_tf = _tf(
                    _strip_pname(_raw_cmt, _pn_for_strip),
                    _wcol.get("comment", 0),
                    h=row_h_line,
                    multiline=True,
                )

                def _commit_line(
                    ix=ix,
                    amt_f=amount_tf,
                    uc_f=unit_cost_tf,
                    ls=line_cs,
                    cmt=comment_line_tf,
                ):
                    st = header_cs["status"]
                    py = header_cs["payment"]
                    dl = header_cs["delivery"]
                    o["status"] = str(st or "production").strip()
                    o["payment_status"] = str(py or "unpaid").strip()
                    o["paid_amount"] = str(paid_tf_header.value or "")
                    dd = str(dl or "").strip()
                    o["delivery_type_id"] = dd
                    o["delivery_type_name"] = str(
                        delivery_by_id.get(dd) or o.get("delivery_type_name") or ""
                    )
                    items_local = (
                        o.get("items") if isinstance(o.get("items"), list) else []
                    )
                    while len(items_local) < (ix + 1):
                        items_local.append({})
                    items_local[ix] = dict(items_local[ix] or {})
                    npid = str(ls.get("product") or "").strip()
                    items_local[ix]["product_id"] = npid
                    prod_loc = next(
                        (
                            p
                            for p in (self._products or [])
                            if str((p or {}).get("id") or "") == npid
                        ),
                        None,
                    )
                    items_local[ix]["product_name"] = (prod_loc or {}).get("name") or (
                        items_local[ix].get("product_name") or ""
                    )
                    items_local[ix]["item_comment"] = str(cmt.value or "")[:400]
                    va = _safe_float(amt_f.value, None)
                    items_local[ix]["amount_manual_override"] = (
                        _fmt_money(va) if va is not None else ""
                    )
                    vu = _safe_float(uc_f.value, None)
                    items_local[ix]["unit_cost_manual_override"] = (
                        _fmt_money(vu) if vu is not None else ""
                    )
                    o["items"] = items_local
                    try:
                        if (
                            isinstance(o.get("items"), list)
                            and o["items"]
                            and isinstance(o["items"][0], dict)
                        ):
                            o["items"][0]["paid_amount"] = str(
                                o.get("paid_amount") or ""
                            )
                    except Exception:
                        pass
                    self._persist_order_async(o, rebuild_after=True)

                prod_c = _pm(
                    line_cs["product"],
                    _wcol.get("product", 0),
                    local_prod_opts,
                    on_change=(
                        lambda v, st=line_cs: (
                            st.__setitem__("product", v),
                            _queue_commit(lambda cl=_commit_line: cl()),
                        )
                    ),
                    bgc=bg,
                    h=row_h_line,
                )

                def _blur_amt_uc(_e, cl=_commit_line):
                    _blur_line(cl)

                def _on_qty_line_blur(
                    _e,
                    _o=o,
                    _it=it,
                    _ix=ix,
                    _qty_f=qty_line_tf,
                    _amt_f=amount_tf,
                ):
                    raw = str(_qty_f.value or "").strip().replace("'", "").replace(",", ".")
                    old_raw = str((_it or {}).get("qty") or "").strip()
                    if raw == old_raw:
                        return
                    new_qty = _safe_float(raw, None)
                    if new_qty is None and raw:
                        return
                    items_local = (
                        _o.get("items") if isinstance(_o.get("items"), list) else []
                    )
                    while len(items_local) <= _ix:
                        items_local.append({})
                    items_local[_ix] = dict(items_local[_ix] or {})
                    items_local[_ix]["qty"] = new_qty if new_qty is not None else ""
                    _o["items"] = items_local
                    # Recalculate line amount if no manual override
                    it_m = _safe_float(items_local[_ix].get("amount_manual_override"), None)
                    if it_m is None:
                        new_amt = self._line_amount_supplier_ccy(
                            _o, items_local[_ix], _ix, supplier_ccy
                        )
                        if new_amt is not None and _amt_f is not None:
                            _amt_f.value = _fmt_money(new_amt)
                            with contextlib.suppress(Exception):
                                _amt_f.update()
                    self._persist_order_async(_o, rebuild_after=False)

                qty_line_tf.on_blur = _on_qty_line_blur
                qty_line_tf.on_submit = _on_qty_line_blur
                amount_tf.on_blur = _blur_amt_uc
                amount_tf.on_submit = _blur_amt_uc
                unit_cost_tf.on_blur = _blur_amt_uc
                unit_cost_tf.on_submit = _blur_amt_uc
                comment_line_tf.on_blur = _blur_amt_uc
                comment_line_tf.on_submit = _blur_amt_uc

                inv_item_txt = (
                    f"{base_inv}/{ix + 1:02d}" if base_inv else f"{ix + 1:02d}"
                )
                inv_lab = _text_cell(inv_item_txt, _wcol.get("invoice", 0), bgc=bg, h=row_h_line)
                zp_suf = f"{base_zp}/{ix + 1:02d}" if base_zp else ""
                zp_lab = _text_cell(zp_suf, _wcol.get("order1c", 0), bgc=bg, h=row_h_line)
                st_lab = _text_cell(
                    _status_label(header_cs["status"]),
                    _wcol.get("status", 0),
                    bgc=bg,
                    color=_TEXT,
                    h=row_h_line,
                )

                line_sup = self._line_amount_supplier_ccy(o, it, ix, supplier_ccy)
                if sc_g == "USD":
                    usd_lab = ""
                else:
                    usd_v = self._supplier_amount_usd(
                        line_sup, supplier_ccy, order=None
                    )
                    usd_lab = _fmt_money(usd_v) if usd_v is not None else ""

                detail_col.controls.append(
                    ft.Row(
                        controls=[
                            inv_lab,
                            zp_lab,
                            st_lab,
                            _ctrl_cell(qty_line_tf, _wcol.get("qty", 0), bgc=bg, h=row_h_line),
                            _ctrl_cell(amount_tf, _wcol.get("amount", 0), bgc=bg, h=row_h_line),
                            _text_cell(
                                usd_lab,
                                _wcol.get("amount_usd", 0),
                                bgc=bg,
                                color=_TEXT,
                                h=row_h_line,
                            ),
                            _text_cell("", _wcol.get("payment", 0), bgc=bg, h=row_h_line),
                            _text_cell(
                                "", _wcol.get("transferred", 0), bgc=bg, h=row_h_line
                            ),  # transferred — per-order, не per-item
                            _text_cell(
                                "", _wcol.get("paid", 0), bgc=bg, h=row_h_line
                            ),  # paid — per-order
                            _prod_ctrl_cell(prod_c, pid, _wcol.get("product", 0), bg, row_h_line),
                            _ctrl_cell(
                                comment_line_tf, _wcol.get("comment", 0), bgc=bg, h=row_h_line
                            ),
                            _text_cell("", _wcol.get("delivery", 0), bgc=bg, h=row_h_line),
                            _ctrl_cell(unit_cost_tf, _wcol.get("unit_cost", 0), bgc=bg, h=row_h_line),
                            _text_cell("", _wcol.get("expand", 0), bgc=bg, h=row_h_line),
                        ],
                        spacing=0,
                    )
                )

            hdr_row = ft.Row(
                controls=[
                    _ctrl_cell(inv_tf_shared, _wcol.get("invoice", 0), bgc=bgp, h=row_h_hdr),
                    _ctrl_cell(zp_tf_shared, _wcol.get("order1c", 0), bgc=bgp, h=row_h_hdr),
                    _ctrl_cell(st_hdr, _wcol.get("status", 0), bgc=bgp, h=row_h_hdr),
                    _text_cell(total_qty_txt, _wcol.get("qty", 0), bgc=bgp, color=_TEXT, h=row_h_hdr),
                    _text_cell(sum_txt, _wcol.get("amount", 0), bgc=bgp, h=row_h_hdr),
                    _text_cell(
                        sum_usd_txt, _wcol.get("amount_usd", 0), bgc=bgp, color=_TEXT, h=row_h_hdr
                    ),
                    _ctrl_cell(pay_hdr, _wcol.get("payment", 0), bgc=bgp, h=row_h_hdr),
                    _ctrl_cell(transferred_tf_header, _wcol.get("transferred", 0), bgc=bgp, h=row_h_hdr),
                    _ctrl_cell(
                        paid_tf_header, _wcol.get("paid", 0), bgc=_hdr_paid_bg or bgp, h=row_h_hdr
                    ),
                    _text_cell(
                        f"{len(items)} товар(ів)",
                        _wcol.get("product", 0),
                        bgc=bgp,
                        color=_TEXT,
                        h=row_h_hdr,
                    ),
                    _ctrl_cell(comment_tf_shared, _wcol.get("comment", 0), bgc=bgp, h=row_h_hdr),
                    (
                        _ctrl_cell(del_hdr, _wcol.get("delivery", 0), bgc=bgp, h=row_h_hdr)
                        if _delivery_opts()
                        else _text_cell(
                            str(o.get("delivery_type_name") or ""),
                            _wcol.get("delivery", 0),
                            bgc=bgp,
                            h=row_h_hdr,
                        )
                    ),
                    _text_cell("", _wcol.get("unit_cost", 0), bgc=bgp, h=row_h_hdr),
                    _ctrl_cell(expand_btn, _wcol.get("expand", 0), bgc=bgp, h=row_h_hdr),
                ],
                spacing=0,
            )

            return [ft.Column(spacing=0, controls=[hdr_row, detail_col])]

        def _inv_num(v) -> float:
            s = str(v or "").strip().lstrip("#").strip()
            # Parse as float so "391.3" < "392" correctly (not "3913" > "392").
            try:
                return float(s) if s else -1.0
            except ValueError:
                pass
            # Fallback: parse leading numeric part (e.g. "391-A" → 391.0).
            import re as _re

            m = _re.match(r"[\d.]+", s)
            if m:
                try:
                    return float(m.group())
                except Exception:
                    pass
            return -1.0

        def _pay_bucket(v: str) -> int:
            pay = str(v or "").strip().lower()
            # unpaid/advance first, transferred next, paid last
            if pay in {"paid", "сплачений", "сплачено", "оплачено"}:
                return 2
            if pay in {"transferred", "передано"}:
                return 1
            if pay in {
                "advance",
                "аванс",
                "передплата",
                "предоплата",
                "частково",
                "частково сплачено",
            }:
                return 0
            return 0

        def _status_rank(v: str) -> int:
            st = str(v or "").strip().lower()
            if (
                st in {"production", "виробництво", "в виробництві", "виро", "виробн."}
                or v == "Виробництво"
            ):
                return 0
            if st in {
                "ready_for_shipment",
                "готовий",
                "in_transit",
                "в дорозі",
                "дорога",
                "вдорозі",
                "transit",
            }:
                return 1
            if st in {"at_warehouse", "на складі", "склад"}:
                return 2
            if st in {"accepted", "прийнятий", "прийнято"}:
                return 3
            return 9

        # Sorting for settlements list:
        # 1) unpaid + advance on top, then transferred, paid at bottom
        # 2) status order: production -> ready/in_transit -> at_warehouse -> accepted
        # 3) invoice descending (bigger invoice higher)
        orders_sorted = sorted(
            orders_for_supplier,
            key=lambda x: (
                _pay_bucket(x.get("payment_status")),
                _status_rank(x.get("status")),
                -_inv_num(x.get("invoice_no")),
            ),
        )
        # "Перший місяць відображення" — ховає РЯДКИ старіших замовлень з рендеру.
        # Баланс/борг/order_count вище вже пораховані з orders_for_supplier (повний список) —
        # цей фільтр торкається лише того, що потрапляє у видиму таблицю нижче.
        _dfm = (self._display_from_month or "").strip()
        if _dfm:
            orders_sorted = [
                o for o in orders_sorted
                if str((o or {}).get("order_date") or "")[:7] >= _dfm
            ]
        show_all = {"value": (supplier_id in self._show_all_suppliers)}

        rows_host = ft.Column(spacing=0, scroll=ft.ScrollMode.HIDDEN)

        def _render_rows(no_update=False):
            rows_host.controls.clear()
            rows_host.controls.append(header_row)
            # settlements rows:
            # - compensation tied to an invoice renders immediately BELOW that invoice
            # - manual ("Інший") compensation renders near bottom, above opening balance
            all_sett = [
                x
                for x in (self._settlements or [])
                if isinstance(x, dict)
                and str(x.get("supplier_id") or "").strip() == supplier_id
            ]
            compensations = [
                s for s in all_sett if str(s.get("type") or "") == "compensation"
            ]
            openings = [
                s for s in all_sett if str(s.get("type") or "") == "opening_balance"
            ]
            subset = orders_sorted if show_all["value"] else orders_sorted[:15]

            # Dynamically set visible height: header + up to 10 data rows (rows may include
            # compensations and opening balance). This avoids large empty gaps under table.
            try:
                data_rows = len(subset) + len(compensations) + (1 if openings else 0)
                data_rows = max(1, int(data_rows))
                visible_rows = min(10, data_rows)
                # header_row uses HDR_H; data rows use ROW_H
                rows_host.height = int(HDR_H + visible_rows * ROW_H)
            except Exception:
                # fallback: no fixed height
                try:
                    if hasattr(rows_host, "height"):
                        delattr(rows_host, "height")
                except Exception:
                    pass

            def _comp_row(s: dict) -> ft.Row:
                amt_s = _safe_float(s.get("amount"), 0.0) or 0.0
                amt_usd_s = None
                if supplier_ccy != "USD":
                    amt_usd_s = self._supplier_amount_usd(
                        float(amt_s),
                        supplier_ccy,
                        order=(orders_for_supplier[0] if orders_for_supplier else None),
                    )
                sid_sett = str((s or {}).get("id") or "").strip()

                def _edit_click(_e=None, ss=s):
                    self._open_compensation_dialog(
                        supplier_id,
                        supplier_name,
                        supplier_ccy,
                        orders_for_supplier,
                        existing=ss,
                    )

                def _delete_click(_e=None, ss=s):
                    if self.is_read_only:
                        return
                    page = self._page()
                    if not page:
                        return
                    sid_local = str((ss or {}).get("id") or "").strip()
                    if not sid_local:
                        return

                    def _do_del(_x=None, _sid_local=sid_local):
                        # 1. Remove from in-memory settlements immediately
                        self._settlements = [
                            _s
                            for _s in self._settlements
                            if str((_s or {}).get("id") or "").strip() != _sid_local
                        ]
                        # 2. Cancel any pending upsert for this settlement
                        self._outbox.pop(f"settlement:{_sid_local}", None)
                        # 3. Queue delete in outbox — actual DB delete happens on next flush
                        self._outbox[f"settlement_delete:{_sid_local}"] = {
                            "id": _sid_local
                        }
                        # 4. Persist outbox + remove from settlements cache
                        with contextlib.suppress(Exception):
                            self._save_outbox()
                        with contextlib.suppress(Exception):
                            self._remove_settlement_from_cache(_sid_local)
                        # 5. Show "Локально" indicator and start 45s flush timer
                        self._refresh_sync_indicator_state()
                        self._start_sync_timer()
                        try:
                            page.close(dlg)
                        except Exception:
                            with contextlib.suppress(Exception):
                                page.pop_dialog()
                        if supplier_id and supplier_id in self._supplier_section_refs:
                            with contextlib.suppress(Exception):
                                page.run_task(
                                    self._refresh_affected_suppliers_async,
                                    [{"supplier_id": supplier_id}],
                                )
                        else:
                            self._schedule_rerender()

                    dlg = ft.AlertDialog(
                        modal=True,
                        title=ft.Text(self.t("payments.dlg.delete_comp_title", "Видалити компенсацію?")),
                        content=ft.Text(self.t("common.irreversible", "Цю дію неможливо скасувати.")),
                        actions=[
                            ft.TextButton(
                                self.t("common.cancel", "Скасувати"),
                                on_click=lambda _x: (
                                    page.close(dlg)
                                    if hasattr(page, "close")
                                    else page.pop_dialog()
                                ),
                            ),
                            ft.FilledButton(self.t("common.delete", "Видалити"), on_click=_do_del),
                        ],
                    )
                    page.show_dialog(dlg)

                actions = (
                    ft.Row(
                        [
                            ft.IconButton(
                                ft.Icons.EDIT_OUTLINED,
                                icon_size=16,
                                tooltip="Редагувати",
                                on_click=_edit_click,
                            ),
                            ft.IconButton(
                                ft.Icons.DELETE_OUTLINE,
                                icon_size=16,
                                tooltip="Видалити",
                                on_click=_delete_click,
                            ),
                        ],
                        spacing=0,
                        alignment=ft.MainAxisAlignment.CENTER,
                    )
                    if sid_sett
                    else ft.Text("", size=11)
                )
                return ft.Row(
                    controls=[
                        _text_cell(
                            str(s.get("invoice_no") or ""), _wcol.get("invoice", 0), bgc=BG_COMP
                        ),
                        _text_cell(
                            str(s.get("order_no_1c") or ""), _wcol.get("order1c", 0), bgc=BG_COMP
                        ),
                        _text_cell(
                            "Компенсація", _wcol.get("status", 0), bgc=BG_COMP, color=_CHIP_T
                        ),
                        _text_cell("", _wcol.get("qty", 0), bgc=BG_COMP),
                        _editable_amount_cell(s, amt_s, _wcol.get("amount", 0), BG_COMP, _CHIP_T),
                        _text_cell(
                            _fmt_money(amt_usd_s) if amt_usd_s is not None else "",
                            _wcol.get("amount_usd", 0),
                            bgc=BG_COMP,
                        ),
                        _text_cell("", _wcol.get("payment", 0), bgc=BG_COMP),
                        _text_cell("", _wcol.get("transferred", 0), bgc=BG_COMP),  # transferred
                        _text_cell("", _wcol.get("paid", 0), bgc=BG_COMP),  # paid
                        _text_cell(
                            str(s.get("product_name") or ""), _wcol.get("product", 0), bgc=BG_COMP
                        ),
                        _text_cell(
                            str(s.get("comment") or ""), _wcol.get("comment", 0), bgc=BG_COMP
                        ),
                        _text_cell("", _wcol.get("delivery", 0), bgc=BG_COMP),
                        _text_cell("", _wcol.get("unit_cost", 0), bgc=BG_COMP),
                        _ctrl_cell(actions, _wcol.get("expand", 0), bgc=BG_COMP),
                    ],
                    spacing=0,
                )

            subset_invoices = {
                str((o or {}).get("invoice_no") or "").strip()
                for o in (subset or [])
                if isinstance(o, dict)
            }
            comp_by_inv: dict[str, list] = {}
            comp_bottom: list = []
            for s in sorted(
                compensations,
                key=lambda x: (str(x.get("created_at") or ""), str(x.get("id") or "")),
                reverse=True,
            ):
                inv = str((s or {}).get("invoice_no") or "").strip()
                manual = bool((s or {}).get("manual_invoice"))
                if inv and (not manual) and (inv in subset_invoices):
                    comp_by_inv.setdefault(inv, []).append(s)
                else:
                    comp_bottom.append(s)

            if isinstance(counters, dict):
                for _o in subset or []:
                    if not isinstance(_o, dict):
                        continue
                    bk = self._payment_bucket_key(_o.get("payment_status"))
                    counters[bk] = int(counters.get(bk) or 0) + 1

            for _oi, o in enumerate(subset):
                inv_o = str((o or {}).get("invoice_no") or "").strip()
                for r in _item_rows_for_order(o, order_idx=_oi):
                    rows_host.controls.append(r)
                for s in comp_by_inv.get(inv_o) or []:
                    rows_host.controls.append(_comp_row(s))

            # Manual/unknown compensations: render near bottom (above opening balance).
            for s in comp_bottom:
                rows_host.controls.append(_comp_row(s))

            # Opening balance last (bottom), regardless of when it was added.
            for s in openings:
                amt_s = _safe_float(s.get("amount"), 0.0) or 0.0
                amt_usd_s = None
                if supplier_ccy != "USD":
                    amt_usd_s = self._supplier_amount_usd(
                        float(amt_s),
                        supplier_ccy,
                        order=(orders_for_supplier[0] if orders_for_supplier else None),
                    )
                rows_host.controls.append(
                    ft.Row(
                        controls=[
                            _text_cell("", _wcol.get("invoice", 0), bgc=BG_OPEN),
                            _text_cell("", _wcol.get("order1c", 0), bgc=BG_OPEN),
                            _text_cell(
                                self.t("payments.opening_balance", "Початковий баланс"),
                                _wcol.get("status", 0),
                                bgc=BG_OPEN,
                                color=_OK_T,
                            ),
                            _text_cell("", _wcol.get("qty", 0), bgc=BG_OPEN),
                            _editable_amount_cell(s, amt_s, _wcol.get("amount", 0), BG_OPEN, _OK_T),
                            _text_cell(
                                _fmt_money(amt_usd_s) if amt_usd_s is not None else "",
                                _wcol.get("amount_usd", 0),
                                bgc=BG_OPEN,
                            ),
                            _text_cell("", _wcol.get("payment", 0), bgc=BG_OPEN),
                            _text_cell("", _wcol.get("transferred", 0), bgc=BG_OPEN),  # transferred
                            _text_cell("", _wcol.get("paid", 0), bgc=BG_OPEN),  # paid
                            _text_cell("", _wcol.get("product", 0), bgc=BG_OPEN),  # product
                            _text_cell(
                                str(s.get("comment") or ""), _wcol.get("comment", 0), bgc=BG_OPEN
                            ),
                            _text_cell("", _wcol.get("delivery", 0), bgc=BG_OPEN),
                            _text_cell("", _wcol.get("unit_cost", 0), bgc=BG_OPEN),
                            _text_cell("", _wcol.get("expand", 0), bgc=BG_OPEN),
                        ],
                        spacing=0,
                    )
                )
            if not no_update:
                # Bootstrap __prev_lists so update can diff controls list after
                # a section rebuild (frozen diff doesn't call protocol.py serializer,
                # leaving __prev_lists unset on the new controls).
                if not hasattr(rows_host, "__prev_lists"):
                    object.__setattr__(rows_host, "__prev_lists", {"controls": []})
                try:
                    rows_host.update()
                except Exception:
                    pass

        # Store expand context so search can reveal hidden rows before collecting matches
        _expand_ctx: dict = {
            "show_all": show_all,
            "render": _render_rows,
            "btn": None,
            "rows_host": rows_host,
        }
        self._supplier_expand_callbacks[supplier_id] = _expand_ctx

        def _toggle_all(_):
            show_all["value"] = not show_all["value"]
            if show_all["value"]:
                self._show_all_suppliers.add(supplier_id)
            else:
                self._show_all_suppliers.discard(supplier_id)
            toggle_btn.text = (
                self.t("payments.show_last_15", "Показати останні 15") if show_all["value"]
                else self.t("payments.show_all", "Показати всі")
            )
            _render_rows()

        toggle_btn = ft.TextButton(self.t("payments.show_all", "Показати всі"), on_click=_toggle_all)
        _expand_ctx["btn"] = toggle_btn
        # Lazy: build rows only if section is initially expanded (e.g. restored from state).
        # On first expand click _toggle will call _render_rows().
        if supplier_id in self._expanded_suppliers:
            _render_rows()

        # Build table core and shell (packaging-style) so horizontal scroll appears
        w_final = sum(int(w) for _t, w in cols)
        table_core = ft.Column(
            [
                ft.Container(
                    width=max(1, int(w_final)),
                    clip_behavior=ft.ClipBehavior.HARD_EDGE,
                    content=rows_host,
                    padding=ft.padding.only(bottom=15),
                )
            ],
            spacing=0,
            tight=True,
            width=max(1, int(w_final)),
        )

        table_shell = ft.Container(
            width=max(1, int(w_final)),
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            bgcolor=_PANEL,
            border_radius=8,
            border=ft.border.all(1, _BORDER),
            content=table_core,
        )

        _sup_scroll_row = ft.Row(
            [table_shell], spacing=0, expand=True, scroll=ft.ScrollMode.HIDDEN
        )
        _sup_pg = self._page()
        _sup_vp = int(getattr(self.content_area, "width", 0) or 0) or int(
            getattr(_sup_pg, "width", 0) or 1200
        )
        _sup_needs = int(w_final) > max(600, _sup_vp)
        _sup_step = max(300, int(w_final) // 3)

        def _do_hscroll_sup(delta):
            async def _run():
                with contextlib.suppress(Exception):
                    await _sup_scroll_row.scroll_to(delta=delta, duration=200)

            with contextlib.suppress(Exception):
                if _sup_pg:
                    _sup_pg.run_task(_run)

        _sup_sh = ft.BoxShadow(
            spread_radius=1,
            blur_radius=8,
            color=ft.Colors.with_opacity(0.28, ft.Colors.BLACK),
            offset=ft.Offset(0, 2),
        )
        _sup_bs = ft.ButtonStyle(padding=ft.padding.all(4))
        _sup_left = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_LEFT,
                icon_size=20,
                icon_color=_ACCENT,
                on_click=lambda _: _do_hscroll_sup(-_sup_step),
                style=_sup_bs,
            ),
            left=4,
            top=60,
            bgcolor=_PANEL,
            border_radius=20,
            width=36,
            height=36,
            shadow=_sup_sh,
            opacity=0.9,
            visible=_sup_needs,
        )
        _sup_right = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_RIGHT,
                icon_size=20,
                icon_color=_ACCENT,
                on_click=lambda _: _do_hscroll_sup(_sup_step),
                style=_sup_bs,
            ),
            right=4,
            top=60,
            bgcolor=_PANEL,
            border_radius=20,
            width=36,
            height=36,
            shadow=_sup_sh,
            opacity=0.9,
            visible=_sup_needs,
        )

        body.controls = [
            ft.Container(padding=ft.padding.only(top=6, bottom=0), content=btns),
            ft.Row([toggle_btn], alignment=ft.MainAxisAlignment.END),
            ft.Container(
                content=ft.Stack([_sup_scroll_row, _sup_left, _sup_right]),
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
            ),
        ]

        stripe = self.UI_BADGE_INDIGO
        header_ct = ft.Container(
            content=ft.Row(
                [
                    ft.Container(width=4, height=24, bgcolor=stripe, border_radius=3),
                    ft.Container(content=header, expand=True),
                    ft.Icon(ft.Icons.EXPAND_MORE, size=16, color=stripe),
                ],
                spacing=6,
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.symmetric(horizontal=8, vertical=6),
            bgcolor=_SOFT_I,
            border_radius=ft.border_radius.only(top_left=8, top_right=8),
        )

        arrow = header_ct.content.controls[2]

        # Register live widget refs so _toggle always uses current controls even after
        # section rebuild via frozen diff (which creates new body/arrow objects with new UIDs).
        self._section_toggle_refs[supplier_id] = {
            "body": body,
            "arrow": arrow,
            "rows_host": rows_host,
            "render": _render_rows,
        }

        def _toggle(_e):
            _refs = self._section_toggle_refs.get(supplier_id, {})
            _body = _refs.get("body", body)
            _arrow = _refs.get("arrow", arrow)
            _rows_host = _refs.get("rows_host", rows_host)
            _render = _refs.get("render", _render_rows)

            # Guard: control may be frozen if the page rebuilt this section while
            # it was part of a same-key diff. Re-read from refs just in case, but
            # if still frozen there is nothing we can do — skip silently.
            try:
                new_visible = not _body.visible
                _body.visible = new_visible
            except RuntimeError:
                return
            try:
                _arrow.name = ft.Icons.EXPAND_LESS if new_visible else ft.Icons.EXPAND_MORE
            except RuntimeError:
                pass

            if new_visible:
                self._expanded_suppliers.add(supplier_id)
                if not _rows_host.controls:
                    _pg = self._page()
                    if _pg and hasattr(_pg, "run_task"):

                        async def _expand_async(_r=_render, _b=_body, _rh=_rows_host):
                            # Build rows in a thread WITHOUT calling rows_host.update()
                            # from the thread (not safe); update from the event loop after.
                            await asyncio.to_thread(_r, True)
                            # Ensure prev-lists tracking is initialised so the diff works.
                            if not hasattr(_rh, "__prev_lists"):
                                object.__setattr__(_rh, "__prev_lists", {"controls": []})
                            with contextlib.suppress(Exception):
                                _b.update()

                        _pg.run_task(_expand_async)
                        with contextlib.suppress(Exception):
                            _body.update()
                        with contextlib.suppress(Exception):
                            _arrow.update()
                        return
                    else:
                        _render(no_update=True)
            else:
                self._expanded_suppliers.discard(supplier_id)
            with contextlib.suppress(Exception):
                _body.update()
            with contextlib.suppress(Exception):
                _arrow.update()

        header_ct.on_click = _toggle

        section = ft.Container(
            # NOTE: do NOT add key=supplier_id here. Flet treats same-key controls as
            # a "frozen diff" during in-place section rebuilds, which permanently sets
            # _frozen=True on all child controls (body, arrow, rows_host). That makes
            # subsequent property writes raise RuntimeError. Identity-based lookup in
            # _supplier_section_refs works without the key.
            border=ft.border.all(1.5, _CHIP_BG),
            border_radius=8,
            bgcolor=_ALT,
            margin=ft.margin.only(bottom=4),
            clip_behavior=ft.ClipBehavior.NONE,
            content=ft.Column(
                [
                    header_ct,
                    ft.Container(
                        padding=ft.padding.only(left=8, right=8, top=8, bottom=0),
                        content=body,
                    ),
                ],
                spacing=0,
            ),
        )

        if flat_mode:
            # Force-build rows immediately for the flat combined table.
            _render_rows(no_update=True)
            return {
                "rows_host": rows_host,
                "header_row": header_row,
                "w_final": w_final,
                "supplier_name": supplier_name,
            }

        return section

    def _open_opening_balance_dialog(
        self, supplier_id: str, supplier_name: str, supplier_ccy: str
    ):
        page = self._page()
        if not page:
            return
        # Pre-fill and update a single opening balance row (instead of creating multiple).
        existing_open = None
        try:
            all_sett = [
                x
                for x in (self._settlements or [])
                if isinstance(x, dict)
                and str(x.get("supplier_id") or "").strip() == supplier_id
            ]
            openings = [
                s for s in all_sett if str(s.get("type") or "") == "opening_balance"
            ]
            if openings:
                existing_open = sorted(
                    openings,
                    key=lambda x: (
                        str(x.get("ts") or x.get("created_at") or ""),
                        str(x.get("updated_at") or ""),
                        str(x.get("id") or ""),
                    ),
                    reverse=True,
                )[0]
        except Exception:
            existing_open = None

        existing_amt = _safe_float((existing_open or {}).get("amount"), 0.0) or 0.0
        existing_cmt = str((existing_open or {}).get("comment") or "")

        amount_f = ft.TextField(
            label=self.t("payments.opening_balance_ccy", "Початковий баланс ({ccy})").format(ccy=supplier_ccy),
            value=_fmt_money(existing_amt),
            text_size=10,
        )
        comment_f = ft.TextField(label=self.t("orders.field.comment", "Коментар"), value="", text_size=10)
        comment_f.value = existing_cmt

        def _save(_):
            amt = _safe_float(amount_f.value, 0.0) or 0.0
            payload = {
                "id": (existing_open or {}).get("id") or "",
                "company_id": self.company_id,
                "region_id": self.region_id,
                "supplier_id": supplier_id,
                "supplier_name": supplier_name,
                "currency": supplier_ccy,
                "amount": float(amt),
                "type": "opening_balance",
                "comment": str(comment_f.value or ""),
                "manager_id": self.manager_id or "",
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }
            try:
                page.close(dlg)
            except Exception:
                try:
                    page.pop_dialog()
                except Exception:
                    pass
            self._persist_settlement_async(payload, rebuild_after=True)

        def _balance_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, self.t("payments.opening_balance", "Початковий баланс"), [
                self.t("payments.balance_help.step1", "1. Введіть суму початкового боргу (позитивне — ми винні постачальнику, від'ємне — постачальник винен нам)."),
                self.t("payments.balance_help.step2", "2. Додайте коментар для пояснення балансу."),
                self.t("payments.balance_help.step3", "3. Натисніть «Зберегти»."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                [
                    ft.Text(self.t("payments.opening_balance", "Початковий баланс"), expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_balance_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Column([amount_f, comment_f], tight=True),
            actions=[
                ft.TextButton(
                    self.t("common.cancel", "Скасувати"),
                    on_click=lambda _e: (
                        page.close(dlg) if hasattr(page, "close") else page.pop_dialog()
                    ),
                ),
                ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save),
            ],
        )
        page.show_dialog(dlg)

    def _open_compensation_dialog(
        self,
        supplier_id: str,
        supplier_name: str,
        supplier_ccy: str,
        orders_for_supplier: list,
        existing: dict | None = None,
    ):
        page = self._page()
        if not page:
            return

        active_manager_id = str(
            self.user_data.get("active_manager_id") or self.user_data.get("id") or ""
        ).strip()

        # Product list: region + supplier + current user.
        product_rows: list[dict] = []
        try:
            product_rows = (
                self.db.get_products(
                    self.company_id,
                    region_id=self.region_id,
                    manager_id=active_manager_id or None,
                    supplier_id=str(supplier_id or "").strip() or None,
                )
                or []
            )
        except Exception:
            product_rows = []

        if not product_rows:
            # Fallback: derive only from supplier orders (still region-filtered by page).
            try:
                seen = set()
                derived = []
                for _o in orders_for_supplier or []:
                    if not isinstance(_o, dict):
                        continue
                    its = _o.get("items") if isinstance(_o.get("items"), list) else []
                    if not its:
                        its = [
                            {
                                "product_id": _o.get("product_id"),
                                "product_name": _o.get("product_name"),
                            }
                        ]
                    for it in its:
                        if not isinstance(it, dict):
                            continue
                        pid0 = str(it.get("product_id") or "").strip()
                        if not pid0 or pid0 in seen:
                            continue
                        seen.add(pid0)
                        derived.append(
                            {"id": pid0, "name": str(it.get("product_name") or pid0)}
                        )
                product_rows = derived
            except Exception:
                product_rows = []

        product_opts = sorted(
            {
                str(p.get("id") or "").strip(): str(p.get("name") or "").strip()
                for p in (product_rows or [])
                if isinstance(p, dict)
            }.items(),
            key=lambda kv: (kv[1] or kv[0]).lower(),
        )
        prod_name_by_id = {k: v for k, v in (product_opts or []) if k}
        product_dd = ft.Dropdown(
            label=self.t("orders.filter.product", "Товар"),
            options=[ft.dropdown.Option(k, v or k) for k, v in product_opts if k],
            width=380,
            text_size=10,
        )
        # Ensure there is an initial selection; otherwise invoice refresh sees empty pid.
        try:
            if not str(getattr(product_dd, "value", "") or "").strip():
                first_key = ""
                for opt in product_dd.options or []:
                    k = str(getattr(opt, "key", "") or "").strip()
                    if k:
                        first_key = k
                        break
                if first_key:
                    product_dd.value = first_key
        except Exception:
            pass
        # Invoice + ZP accounting #: selectable from existing values with "Інший" for manual input.
        OTHER_KEY = "__other__"
        invoice_dd = ft.Dropdown(label=self.t("orders.field.invoice_num", "№ Інвойсу"), options=[], width=200, text_size=10)
        invoice_manual_f = ft.TextField(
            label=self.t("payments.other_invoice", "Інший інвойс"), value="", text_size=10, visible=False, width=200
        )
        zp_dd = ft.Dropdown(label=self.t("payments.col.order1c", "ЗП обліку"), options=[], width=200, text_size=10)
        zp_manual_f = ft.TextField(
            label=self.t("payments.other_1c", "Інший ЗП обліку"), value="", text_size=10, visible=False, width=200
        )
        inv_hint = ft.Text("", size=10, color=_TEXT)
        comment_f = ft.TextField(
            label=self.t("orders.field.comment", "Коментар"), multiline=True, min_lines=2, max_lines=3, text_size=10
        )
        amount_f = ft.TextField(
            label=self.t("payments.compensation_amount", "Сума компенсації ({ccy})").format(ccy=supplier_ccy), value="0", text_size=10
        )
        is_edit = bool(
            isinstance(existing, dict) and str(existing.get("id") or "").strip()
        )
        if is_edit and isinstance(existing, dict):
            # Prefill editable fields
            try:
                comment_f.value = str(existing.get("comment") or "")
            except Exception:
                pass
            try:
                amt0 = _safe_float(existing.get("amount"), 0.0) or 0.0
                amount_f.value = _fmt_money(abs(float(amt0)))
            except Exception:
                pass

        def _order_items(o: dict) -> list[dict]:
            if not isinstance(o, dict):
                return []
            its = o.get("items") if isinstance(o.get("items"), list) else []
            if its:
                out = [it for it in its if isinstance(it, dict)]
                # Some legacy/single-line orders keep product fields on the order itself.
                # Ensure item 0 has product_name/product_id so matching works reliably.
                if len(out) == 1:
                    try:
                        if not str((out[0] or {}).get("product_name") or "").strip():
                            if str(o.get("product_name") or "").strip():
                                out[0]["product_name"] = str(
                                    o.get("product_name") or ""
                                ).strip()
                        if not str((out[0] or {}).get("product_id") or "").strip():
                            if str(o.get("product_id") or "").strip():
                                out[0]["product_id"] = str(
                                    o.get("product_id") or ""
                                ).strip()
                    except Exception:
                        pass
                return out
            # legacy flattened order
            return [
                {
                    "product_id": o.get("product_id"),
                    "product_name": o.get("product_name"),
                }
            ]

        # Guard to avoid refresh-trigger loops (dropdown updates can fire on_change).
        _refresh_guard = {"busy": False}

        def _refresh_invoice_opts(
            _e=None, *, zp_filter: str | None = None, preserve_value: bool = True
        ):
            pid = str(product_dd.value or "").strip()
            sel_name = str(prod_name_by_id.get(pid) or "").strip()

            def _norm_name(s: str) -> str:
                try:
                    s0 = str(s or "")
                except Exception:
                    s0 = ""
                # Normalize common invisible separators (NBSP/narrow NBSP/ZW spaces) to plain space.
                for ch in [
                    "\u00a0",
                    "\u202f",
                    "\u2007",
                    "\u2009",
                    "\u200a",
                    "\u200b",
                    "\u200c",
                    "\u200d",
                    "\ufeff",
                ]:
                    s0 = s0.replace(ch, " ")
                s0 = " ".join(s0.strip().split())
                return s0.casefold()

            sel_name_n = _norm_name(sel_name)
            invs = []
            scanned = 0
            has_invoice = 0
            matched = 0
            for o in orders_for_supplier:
                if not isinstance(o, dict):
                    continue
                scanned += 1
                if not sel_name_n:
                    continue

                def _extract_names_n(order: dict) -> set[str]:
                    names: list[str] = []
                    # Item-level names
                    for it in _order_items(order):
                        if not isinstance(it, dict):
                            continue
                        raw = (
                            it.get("product_name")
                            or it.get("product")
                            or it.get("title")
                            or it.get("name")
                            or ""
                        )
                        raw_s = str(raw or "").strip()
                        if raw_s:
                            names.append(_norm_name(raw_s))
                    # Order-level names (legacy / flattened)
                    for key in (
                        "product_name",
                        "product",
                        "product_title",
                        "title",
                        "name",
                    ):
                        raw_o = str((order or {}).get(key) or "").strip()
                        if raw_o:
                            names.append(_norm_name(raw_o))
                    # Sometimes UI stores a combined label like "2 товар(ів)" in product_name; ignore.
                    names = [n for n in names if n and not n.endswith("товар(ів)")]
                    return set(names)

                names_set = _extract_names_n(o)
                # Fallback: match by product_id if it exists on order/item.
                try:
                    if pid:
                        if str((o or {}).get("product_id") or "").strip() == pid:
                            names_set.add(sel_name_n)
                        for it in _order_items(o):
                            if (
                                isinstance(it, dict)
                                and str(it.get("product_id") or "").strip() == pid
                            ):
                                names_set.add(sel_name_n)
                                break
                except Exception:
                    pass

                # Match: exact or substring (to tolerate slight naming differences).
                ok = sel_name_n in names_set
                if not ok and sel_name_n:
                    for nm in names_set:
                        if (sel_name_n in nm) or (nm in sel_name_n):
                            ok = True
                            break
                if not ok:
                    continue
                # Optional filter by ZP (when user selected ZP first).
                if zp_filter:
                    if (
                        str(o.get("order_no_1c") or "").strip()
                        != str(zp_filter or "").strip()
                    ):
                        continue
                inv_raw = str(o.get("invoice_no") or "").strip()
                if inv_raw:
                    has_invoice += 1
                matched += 1
                invs.append(inv_raw)
            invs = sorted({x for x in invs if x})
            inv_hint.value = f"Знайдено інвойсів: {len(invs)}"
            invoice_dd.options = [ft.dropdown.Option(OTHER_KEY, "Інший")] + [
                ft.dropdown.Option(x, x) for x in invs
            ]
            # If there are no invoices for selected product, default to "Інший".
            if not invs:
                invoice_dd.value = OTHER_KEY
            else:
                if (
                    preserve_value
                    and invoice_dd.value
                    and str(invoice_dd.value) in invs
                ):
                    invoice_dd.value = invoice_dd.value
                else:
                    invoice_dd.value = (
                        OTHER_KEY
                        if invoice_dd.value == OTHER_KEY
                        else (invoice_dd.value if invoice_dd.value in invs else None)
                    )
            invoice_manual_f.visible = bool(invoice_dd.value == OTHER_KEY)
            if invoice_dd.value != OTHER_KEY:
                invoice_manual_f.value = ""
            with contextlib.suppress(Exception):
                invoice_dd.update()
            with contextlib.suppress(Exception):
                inv_hint.update()
            with contextlib.suppress(Exception):
                invoice_manual_f.update()

        def _refresh_zp_opts(
            _e=None, *, inv_filter: str | None = None, preserve_value: bool = True
        ):
            pid = str(product_dd.value or "").strip()
            sel_name = str(prod_name_by_id.get(pid) or "").strip()

            def _norm_name(s: str) -> str:
                try:
                    s0 = str(s or "")
                except Exception:
                    s0 = ""
                for ch in [
                    "\u00a0",
                    "\u202f",
                    "\u2007",
                    "\u2009",
                    "\u200a",
                    "\u200b",
                    "\u200c",
                    "\u200d",
                    "\ufeff",
                ]:
                    s0 = s0.replace(ch, " ")
                s0 = " ".join(s0.strip().split())
                return s0.casefold()

            sel_name_n = _norm_name(sel_name)
            inv_choice = str(invoice_dd.value or "").strip()

            zps = []
            # If invoice is manual/other, we can't derive ZP list -> keep only "Інший".
            if inv_choice == OTHER_KEY:
                zps = []
            elif sel_name_n:
                for o in orders_for_supplier:
                    if not isinstance(o, dict):
                        continue
                    if inv_filter:
                        if (
                            str(o.get("invoice_no") or "").strip()
                            != str(inv_filter or "").strip()
                        ):
                            continue
                    names_n = []
                    for it in _order_items(o):
                        if not isinstance(it, dict):
                            continue
                        it_name = str(
                            it.get("product_name") or it.get("name") or ""
                        ).strip()
                        if it_name:
                            names_n.append(_norm_name(it_name))
                    o_name = str((o or {}).get("product_name") or "").strip()
                    if o_name:
                        names_n.append(_norm_name(o_name))
                    try:
                        if pid:
                            if str((o or {}).get("product_id") or "").strip() == pid:
                                names_n.append(sel_name_n)
                            for it in _order_items(o):
                                if (
                                    isinstance(it, dict)
                                    and str(it.get("product_id") or "").strip() == pid
                                ):
                                    names_n.append(sel_name_n)
                                    break
                    except Exception:
                        pass
                    names_set = set(names_n)
                    ok = sel_name_n in names_set
                    if not ok and sel_name_n:
                        for nm in names_set:
                            if (sel_name_n in nm) or (nm in sel_name_n):
                                ok = True
                                break
                    if not ok:
                        continue
                    zps.append(str(o.get("order_no_1c") or "").strip())
            zps = sorted({x for x in zps if x})
            zp_dd.options = [ft.dropdown.Option(OTHER_KEY, "Інший")] + [
                ft.dropdown.Option(x, x) for x in zps
            ]
            # If there are no ZP values for selected (product+invoice), default to "Інший".
            if not zps:
                zp_dd.value = OTHER_KEY
            else:
                if preserve_value and zp_dd.value and str(zp_dd.value) in zps:
                    zp_dd.value = zp_dd.value
                else:
                    zp_dd.value = (
                        OTHER_KEY
                        if zp_dd.value == OTHER_KEY
                        else (zp_dd.value if zp_dd.value in zps else None)
                    )
            zp_manual_f.visible = bool(zp_dd.value == OTHER_KEY)
            if zp_dd.value != OTHER_KEY:
                zp_manual_f.value = ""
            with contextlib.suppress(Exception):
                zp_dd.update()
            with contextlib.suppress(Exception):
                zp_manual_f.update()

        def _on_product_change(_e=None):
            if _refresh_guard["busy"]:
                return
            _refresh_guard["busy"] = True
            try:
                # On product change: refresh both lists from scratch.
                invoice_dd.value = None
                zp_dd.value = None
                invoice_manual_f.value = ""
                zp_manual_f.value = ""
                _refresh_invoice_opts(zp_filter=None, preserve_value=False)
                _refresh_zp_opts(inv_filter=None, preserve_value=False)
            finally:
                _refresh_guard["busy"] = False

        def _on_invoice_change(_e=None):
            if _refresh_guard["busy"]:
                return
            _refresh_guard["busy"] = True
            try:
                inv_choice = str(invoice_dd.value or "").strip()
                invoice_manual_f.visible = bool(inv_choice == OTHER_KEY)
                if inv_choice != OTHER_KEY:
                    invoice_manual_f.value = ""
                with contextlib.suppress(Exception):
                    invoice_manual_f.update()
                # Invoice chosen -> refresh ZP list filtered by invoice.
                _refresh_zp_opts(
                    inv_filter=(None if inv_choice == OTHER_KEY else inv_choice),
                    preserve_value=False,
                )
            finally:
                _refresh_guard["busy"] = False

        def _on_zp_change(_e=None):
            if _refresh_guard["busy"]:
                return
            _refresh_guard["busy"] = True
            try:
                zp_choice = str(zp_dd.value or "").strip()
                zp_manual_f.visible = bool(zp_choice == OTHER_KEY)
                if zp_choice != OTHER_KEY:
                    zp_manual_f.value = ""
                with contextlib.suppress(Exception):
                    zp_manual_f.update()
                # ZP chosen -> refresh invoice list filtered by ZP.
                _refresh_invoice_opts(
                    zp_filter=(None if zp_choice == OTHER_KEY else zp_choice),
                    preserve_value=False,
                )
                # After invoice list potentially narrowed, refresh ZP again for the chosen invoice.
                inv_choice = str(invoice_dd.value or "").strip()
                _refresh_zp_opts(
                    inv_filter=(None if inv_choice == OTHER_KEY else inv_choice),
                    preserve_value=True,
                )
            finally:
                _refresh_guard["busy"] = False

        # Flet can fire either on_change or on_select depending on platform/widget state.
        # Attach both to ensure dependent dropdowns refresh immediately.
        product_dd.on_change = _on_product_change
        product_dd.on_select = _on_product_change
        invoice_dd.on_change = _on_invoice_change
        invoice_dd.on_select = _on_invoice_change
        zp_dd.on_change = _on_zp_change
        zp_dd.on_select = _on_zp_change

        # Initial population (so "Інший" shows immediately when lists are empty).
        _on_product_change()

        # If editing existing settlement, apply its selections after initial population.
        if is_edit and isinstance(existing, dict):
            try:
                ex_pid = str(existing.get("product_id") or "").strip()
                if ex_pid:
                    product_dd.value = ex_pid
            except Exception:
                pass
            try:
                _on_product_change()
            except Exception:
                pass
            try:
                ex_inv = str(existing.get("invoice_no") or "").strip()
                ex_inv_manual = bool(existing.get("manual_invoice"))
                if ex_inv_manual or not ex_inv:
                    invoice_dd.value = OTHER_KEY
                    invoice_manual_f.value = ex_inv
                else:
                    invoice_dd.value = ex_inv
                _on_invoice_change()
            except Exception:
                pass
            try:
                ex_zp = str(existing.get("order_no_1c") or "").strip()
                ex_zp_manual = bool(existing.get("manual_order_no_1c"))
                if ex_zp_manual or not ex_zp:
                    zp_dd.value = OTHER_KEY
                    zp_manual_f.value = ex_zp
                else:
                    zp_dd.value = ex_zp
                _on_zp_change()
            except Exception:
                pass

        def _save(_):
            amt = _safe_float(amount_f.value, 0.0) or 0.0
            # compensation stored with negative sign
            pid = str(product_dd.value or "").strip()
            prod_name = next((v for k, v in product_opts if k == pid), "")
            inv_choice = str(invoice_dd.value or "").strip()
            inv_eff = (
                str(invoice_manual_f.value or "").strip()
                if inv_choice == OTHER_KEY
                else inv_choice
            )
            zp_choice = str(zp_dd.value or "").strip()
            zp_eff = (
                str(zp_manual_f.value or "").strip()
                if zp_choice == OTHER_KEY
                else zp_choice
            )
            payload = {
                "id": (
                    str(existing.get("id") or "").strip()
                    if is_edit and isinstance(existing, dict)
                    else ""
                ),
                "company_id": self.company_id,
                "region_id": self.region_id,
                "supplier_id": supplier_id,
                "supplier_name": supplier_name,
                "currency": supplier_ccy,
                "amount": float(-abs(amt)),
                "type": "compensation",
                "product_id": pid,
                "product_name": prod_name,
                "invoice_no": inv_eff,
                "order_no_1c": zp_eff,
                "manual_invoice": bool(inv_choice == OTHER_KEY),
                "manual_order_no_1c": bool(zp_choice == OTHER_KEY),
                "comment": str(comment_f.value or ""),
                "manager_id": self.manager_id or "",
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
            }
            try:
                page.close(dlg)
            except Exception:
                try:
                    page.pop_dialog()
                except Exception:
                    pass
            self._persist_settlement_async(payload, rebuild_after=True)

        def _comp_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, self.t("payments.compensation", "Компенсація"), [
                self.t("payments.comp_help.step1", "1. Оберіть продукт і рахунок-фактуру."),
                self.t("payments.comp_help.step2", "2. Оберіть або введіть вручну ЗП номер."),
                self.t("payments.comp_help.step3", "3. Введіть коментар і суму компенсації."),
                self.t("payments.comp_help.step4", "4. Натисніть «Додати» / «Зберегти»."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                [
                    ft.Text(self.t("payments.compensation", "Компенсація"), expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_comp_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Column(
                [
                    ft.Text(self.t("payments.supplier_colon", "Постачальник: {name}").format(name=supplier_name), size=10, color=_TEXT),
                    product_dd,
                    ft.Row([invoice_dd, invoice_manual_f], spacing=8),
                    inv_hint,
                    ft.Row([zp_dd, zp_manual_f], spacing=8),
                    comment_f,
                    amount_f,
                ],
                tight=True,
            ),
            actions=[
                ft.TextButton(
                    self.t("common.cancel", "Скасувати"),
                    on_click=lambda _e: (
                        page.close(dlg) if hasattr(page, "close") else page.pop_dialog()
                    ),
                ),
                ft.FilledButton(self.t("common.save", "Зберегти") if is_edit else self.t("common.add", "Додати"), on_click=_save),
            ],
        )
        page.show_dialog(dlg)

    def build(self):
        _th = _th_from_view(self)
        self.UI_PANEL_TINT = _th["soft"]
        self.UI_PANEL_BORDER = _th["chip_bg"]
        self.UI_BADGE_INDIGO = _th["accent"]
        self.UI_ACCENT_BLUE = _th["icon_color"]
        self.UI_TITLE_INK = _th["text"]
        self.UI_MUTED_INK = _th["muted"]
        self.UI_CARD_BORDER = _th["border"]
        self.UI_CARD_BG = _th["panel"]
        self.UI_ACCENT_TEAL = _th["ok"]
        self.UI_ACCENT_AMBER = _th["warn"]
        self.UI_ACCENT_RED = _th["err"]
        global _TEXT, _TEXT_DARK, _MUTED, _HDR, _BORDER, _PANEL, _ALT
        global _OK_T, _OK_BG, _ERR_BG, _ERR, _ERR_DARK, _WARN_T, _WARN_BG
        global _CHIP_T, _SOFT_I, _CHIP_BG, _ACCENT, _INDIGO, _INDIGO_DK
        _TEXT = _TEXT_DARK = _th["text"]
        _MUTED = _th["muted"]
        _HDR = _th["hdr"]
        _BORDER = _th["border"]
        _PANEL = _th["panel"]
        _ALT = _th["alt"]
        _OK_T = _th["ok"]
        _OK_BG = _th["ok_bg"]
        _ERR_BG = _th["err_bg"]
        _ERR = _ERR_DARK = _th["err"]
        _WARN_T = _th["warn"]
        _WARN_BG = _th["warn_bg"]
        _CHIP_T = _th["chip_text"]
        _SOFT_I = _th["soft"]
        _CHIP_BG = _th["chip_bg"]
        _ACCENT = _th["icon_color"]
        _INDIGO = _INDIGO_DK = _th["accent"]
        page = self._page()
        if not page:
            return

        if self._general_mode and self._general_manager_ids:
            from views.general_wrapper import build_general_sections

            _ar = self.active_region
            _cid, _db = self.company_id, self.db
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
            _all_orders = _db.get_orders(_cid, _rid) or []
            # Load suppliers for currency lookup (used in per-manager chips).
            if not self._suppliers:
                try:
                    self._suppliers = _db.get_suppliers(_cid, None) or []
                except Exception:
                    pass
            _mgr_counts: dict[str, int] = {}
            _mgr_unpaid_counts: dict[str, int] = {}
            _mgr_orders_map: dict[str, list] = {}
            for _o in _all_orders:
                if not isinstance(_o, dict):
                    continue
                if not self._is_order_in_scope(_o):
                    continue
                _m = str(_o.get("_gm_manager_id") or _o.get("manager_id") or "").strip()
                if _m:
                    _mgr_counts[_m] = _mgr_counts.get(_m, 0) + 1
                    _mgr_orders_map.setdefault(_m, []).append(_o)
                    if self._payment_bucket_key(_o.get("payment_status")) != "paid":
                        _mgr_unpaid_counts[_m] = _mgr_unpaid_counts.get(_m, 0) + 1

            def _count_orders(mid, _counts=_mgr_counts):
                return _counts.get(mid, 0)

            def _count_unpaid_orders(mid, _counts=_mgr_unpaid_counts):
                return _counts.get(mid, 0)

            def _mgr_chips(mid, _omap=_mgr_orders_map):
                stats = self._compute_group_order_stats(_omap.get(mid, []))
                return self._build_group_stat_chips(stats)

            _is_sub_gm = bool(self.user_data.get("_is_sub_section_view"))
            # Sub-views must NOT overwrite the parent aggregate sticky header.
            if not _is_sub_gm:
                with contextlib.suppress(Exception):
                    self._build_aggregate_sticky_skeleton(page)
            _table_btn = ft.OutlinedButton(
                "Таблиця",
                icon=ft.Icons.TUNE,
                height=30,
                style=ft.ButtonStyle(
                    text_style=ft.TextStyle(size=11),
                    padding=ft.padding.symmetric(horizontal=8, vertical=2),
                ),
                on_click=lambda _: self._open_table_layout_dialog(),
            )
            # Store orders for async sticky update after FX loads.
            self._agg_orders_cache = _all_orders
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
                unpaid_count_getter=_count_unpaid_orders,
                chips_getter=_mgr_chips,
                top_controls=[ft.Row([_table_btn], spacing=8)],
            )
            # Schedule async load → loads suppliers + FX → updates sticky header.
            # Sub-views skip this: their parent already owns the sticky update.
            if page and hasattr(page, "run_task") and not _is_sub_gm:

                async def _gen_load():
                    import asyncio as _aio

                    await _aio.sleep(0.1)
                    await self._async_initial_load()

                with contextlib.suppress(Exception):
                    self._initial_load_task = page.run_task(_gen_load)
            return

        if not self.active_region:
            from views.general_wrapper import build_region_sections

            _cid2, _db2, _mid2 = self.company_id, self.db, self.manager_id
            _is_sub_rgn_view = bool(self.user_data.get("_is_sub_section_view"))
            # Show skeleton sticky header immediately (spinner); full data comes async.
            # Sub-views must NOT overwrite the parent aggregate sticky header.
            if not _is_sub_rgn_view:
                with contextlib.suppress(Exception):
                    self._build_aggregate_sticky_skeleton(page)
            # Load suppliers for currency lookup in per-region chips.
            if not self._suppliers:
                try:
                    self._suppliers = (
                        _db2.get_suppliers(_cid2, None, manager_id=_mid2) or []
                    )
                except Exception:
                    pass
            # Always fetch from DB so all regions are included (shared_orders_map
            # may contain only one region's orders from a previous navigation).
            _all2 = _db2.get_orders(_cid2, None) or []
            self._agg_orders_cache = _all2
            _rcounts: dict[str, int] = {}
            _runpaid: dict[str, int] = {}
            _region_orders_map: dict[str, list] = {}
            for _o2 in _all2:
                if not isinstance(_o2, dict):
                    continue
                if (
                    str(
                        _o2.get("_gm_manager_id") or _o2.get("manager_id") or ""
                    ).strip()
                    != _mid2
                ):
                    continue
                if not self._is_order_in_scope(_o2):
                    continue
                _r2 = str(_o2.get("region_id") or "").strip()
                if _r2:
                    _rcounts[_r2] = _rcounts.get(_r2, 0) + 1
                    _region_orders_map.setdefault(_r2, []).append(_o2)
                    if self._payment_bucket_key(_o2.get("payment_status")) != "paid":
                        _runpaid[_r2] = _runpaid.get(_r2, 0) + 1

            def _count_rp(rid, _c=_rcounts):
                return _c.get(rid, 0)

            def _count_runpaid(rid, _c=_runpaid):
                return _c.get(rid, 0)

            def _region_chips(rid, _omap=_region_orders_map):
                stats = self._compute_group_order_stats(_omap.get(rid, []))
                return self._build_group_stat_chips(stats)

            _tbl2 = ft.OutlinedButton(
                "Таблиця",
                icon=ft.Icons.TUNE,
                height=30,
                style=ft.ButtonStyle(
                    text_style=ft.TextStyle(size=11),
                    padding=ft.padding.symmetric(horizontal=8, vertical=2),
                ),
                on_click=lambda _: self._open_table_layout_dialog(),
            )
            _is_sub_rgn = bool(self.user_data.get("_is_sub_section_view"))
            build_region_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={"quick_filter": self.quick_filter},
                count_getter=_count_rp,
                region_kwarg_name="active_region",
                unpaid_count_getter=_count_runpaid,
                chips_getter=_region_chips,
                top_controls=[ft.Row([_tbl2], spacing=8)] if not _is_sub_rgn else None,
            )
            # Schedule async load → loads suppliers + FX → updates sticky header.
            # Sub-views skip this: the parent already owns the sticky update.
            if page and hasattr(page, "run_task") and not _is_sub_rgn_view:

                async def _agg_load():
                    import asyncio as _aio

                    await _aio.sleep(0.1)
                    await self._async_initial_load()

                with contextlib.suppress(Exception):
                    self._initial_load_task = page.run_task(_agg_load)
            return

        try:
            self.content_area.scroll = ft.ScrollMode.ADAPTIVE
            self.content_area.spacing = 0
            self.content_area.padding = 0
        except Exception:
            pass
        self.content_area.controls.clear()

        reg_lbl = (
            str(self.user_data.get("active_region_label", "") or "").strip() or "Регіон"
        )
        mgr = str(
            self.user_data.get("active_manager_name")
            or self.user_data.get("full_name")
            or ""
        ).strip()
        subtitle = f"{reg_lbl} · {mgr}" if mgr else reg_lbl

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

        _sync_indicator = self._build_sync_indicator()
        self._refresh_sync_indicator_state()

        self._header_loading_indicator = create_orders_header_loading_indicator(
            visible=True
        )

        header_row = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.PAYMENTS_OUTLINED, size=26, color=_TEXT),
                            ft.Column(
                                [
                                    ft.Row(
                                        [
                                            ft.Text(
                                                self.t("payments.title", "Взаєморозрахунки"),
                                                size=21,
                                                weight=ft.FontWeight.W_700,
                                                color=_TEXT,
                                            ),
                                            self._header_loading_indicator,
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
                                    ),
                                ],
                                spacing=2,
                                tight=True,
                                expand=True,
                            ),
                            ft.Row([table_btn, _sync_indicator], spacing=8, wrap=True),
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

        self._counters_row = ft.Row(spacing=4, run_spacing=6, wrap=True)
        counters_bar = ft.Container(
            padding=ft.padding.only(left=2, right=2, top=0, bottom=4),
            content=self._counters_row,
        )

        # ── sticky totals bar — all chips: fixed 165×65, content centered ──────
        _TCW = 132
        _chip_pad = ft.padding.symmetric(horizontal=10, vertical=4)
        _LBL_C = _MUTED
        _CA = ft.alignment.Alignment(0, 0)
        _HA = ft.CrossAxisAlignment.CENTER

        self._totals_bal_amount_col = ft.Column(
            [
                ft.Text(
                    "Баланс:",
                    size=9,
                    weight=ft.FontWeight.W_700,
                    color=_LBL_C,
                    text_align=ft.TextAlign.CENTER,
                ),
                ft.Text(
                    "…",
                    size=12,
                    weight=ft.FontWeight.W_700,
                    color=_WARN_T,
                    text_align=ft.TextAlign.CENTER,
                ),
            ],
            spacing=2,
            tight=True,
            horizontal_alignment=_HA,
        )
        self._totals_bal_hint = ft.Text(
            "…",
            size=10,
            weight=ft.FontWeight.W_700,
            color=_WARN_T,
            text_align=ft.TextAlign.CENTER,
        )
        self._totals_bal_container = ft.Container(
            width=_TCW,
            padding=_chip_pad,
            bgcolor=_WARN_BG,
            border_radius=8,
            alignment=_CA,
            content=self._totals_bal_amount_col,
        )
        self._totals_bal_hint_container = ft.Container(
            width=_TCW,
            padding=_chip_pad,
            bgcolor=_WARN_BG,
            border_radius=8,
            alignment=_CA,
            content=self._totals_bal_hint,
        )
        self._totals_debt_text = ft.Text(
            "…",
            size=10,
            weight=ft.FontWeight.W_700,
            color=_TEXT,
            text_align=ft.TextAlign.CENTER,
        )
        self._totals_paid_text = ft.Text(
            "…",
            size=10,
            weight=ft.FontWeight.W_700,
            color=_OK_T,
            text_align=ft.TextAlign.CENTER,
        )

        def _totals_chip(label: str, val_text: ft.Text, bg: str) -> ft.Container:
            return ft.Container(
                width=_TCW,
                padding=_chip_pad,
                bgcolor=bg,
                border_radius=8,
                alignment=_CA,
                content=ft.Column(
                    [
                        ft.Text(
                            label,
                            size=9,
                            weight=ft.FontWeight.W_700,
                            color=_LBL_C,
                            text_align=ft.TextAlign.CENTER,
                        ),
                        val_text,
                    ],
                    spacing=2,
                    tight=True,
                    horizontal_alignment=_HA,
                ),
            )

        # Suffix controls (Сума замовлень / Сплачено) — rebuilt in _update_balance_display with $ lines
        _sep_ctrl = ft.Container(width=12)
        _debt_col = ft.Column(
            [
                ft.Text(
                    self.t("payments.stat.orders_sum", "Сума замовлень:"),
                    size=9,
                    weight=ft.FontWeight.W_700,
                    color=_LBL_C,
                    text_align=ft.TextAlign.CENTER,
                ),
                self._totals_debt_text,
            ],
            spacing=2,
            tight=True,
            horizontal_alignment=_HA,
        )
        self._totals_debt_chip_col = _debt_col
        _debt_chip = ft.Container(
            width=_TCW,
            padding=_chip_pad,
            bgcolor=_ALT,
            border_radius=8,
            content=_debt_col,
        )
        _paid_col = ft.Column(
            [
                ft.Text(
                    self.t("payments.stat.paid_sum", "Сплачено:"),
                    size=9,
                    weight=ft.FontWeight.W_700,
                    color=_LBL_C,
                    text_align=ft.TextAlign.CENTER,
                ),
                self._totals_paid_text,
            ],
            spacing=2,
            tight=True,
            horizontal_alignment=_HA,
        )
        self._totals_paid_chip_col = _paid_col
        _paid_chip = ft.Container(
            width=_TCW,
            padding=_chip_pad,
            bgcolor=_OK_BG,
            border_radius=8,
            content=_paid_col,
        )
        self._sticky_totals_bar_suffix_ref = [_sep_ctrl, _debt_chip, _paid_chip]

        _stb_inner_row = ft.Row(
            controls=[
                ft.Text(
                    self.t("payments.balance.total", "Загальний баланс"),
                    size=11,
                    weight=ft.FontWeight.W_700,
                    color=_CHIP_T,
                ),
                self._totals_bal_container,
                self._totals_bal_hint_container,
                _sep_ctrl,
                _debt_chip,
                _paid_chip,
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            wrap=True,
            run_spacing=4,
        )
        self._sticky_totals_bar_row_ref = _stb_inner_row

        self._sticky_totals_bar = ft.Container(
            padding=ft.padding.only(left=2, right=2, top=2, bottom=6),
            border=ft.border.only(bottom=ft.BorderSide(1, _HDR)),
            content=_stb_inner_row,
        )

        self._sticky_header_row = header_row
        self._sticky_counters_bar = counters_bar

        # Sub-views (expanded inside region/general groups) must NOT replace the
        # aggregate sticky header built by the top-level view.
        _is_sub = bool(self.user_data.get("_is_sub_section_view"))
        if not _is_sub:
            _sticky = getattr(page, "_view_sticky_header", None)
            if (
                _sticky is not None
                and getattr(page, "_active_view", "") == "show_payments"
            ):
                _sticky.controls[:] = [
                    header_row,
                    counters_bar,
                    self._sticky_totals_bar,
                ]
                with contextlib.suppress(Exception):
                    _sticky.update()

        with contextlib.suppress(Exception):
            self.content_area.update()

        # When instantiated as a sub-view by build_region_sections, the dashboard
        # does NOT call _defer_view_initial_load — so we self-schedule the async
        # load here.  If the dashboard DOES call _defer_view_initial_load afterwards,
        # it will cancel _initial_load_task and reschedule — no harm done.
        if page and hasattr(page, "run_task"):

            async def _self_start():
                import asyncio as _aio

                await _aio.sleep(0.06)
                await self._async_initial_load()

            with contextlib.suppress(Exception):
                self._initial_load_task = page.run_task(_self_start)
