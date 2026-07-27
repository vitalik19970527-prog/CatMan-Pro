import asyncio
import contextlib
import json
import os
from datetime import datetime
from typing import Any, Optional

import flet as ft

import fx_rates
import i18n
from logger import logger
from views.async_view_build_helper import create_orders_header_loading_indicator
from views.cost_price_stock_table import (
    build_stock_counters,
    build_stock_filter_bar,
    build_stock_table,
    build_stock_totals,
    set_stock_query,
)
from views.number_format import format_grouped, row_text_matches, safe_float
from views import product_naming

# ---------------------------------------------------------------------------
# Theme colour shims — module defaults, shadowed by live theme at render time
# ---------------------------------------------------------------------------
_PANEL   = '#FFFFFF'
_ERR     = '#DC2626'
_HDR     = '#E2E8F0'
_MUTED   = '#6B7280'
_ALT     = '#F8FAFC'
_MUTED2  = '#475569'
_TEXT    = '#0F172A'
_OK      = '#059669'
_ERR_BG  = '#FEE2E2'
_BORDER  = '#E5E7EB'
_ACCENT  = '#1D4ED8'
_CHIP_T  = '#1E3A8A'
_WARN    = '#F59E0B'
_WARN_DK = '#B45309'
_ACCENT2 = '#3B82F6'
_BORDER2 = '#D1D5DB'
_ALT2    = '#F1F5F9'
_OK_BG   = '#DCFCE7'


def _th_from_view(view) -> dict:
    from views.themes import get_active_theme
    return get_active_theme(str(getattr(view, "manager_id", "") or ""))


def _safe_float(v, default: Optional[float] = None) -> Optional[float]:
    return safe_float(v, default)


def _popup_filter(
    view,
    caption: str,
    selected_key: str,
    options: list[tuple[str, str]],
    on_pick,
    *,
    width: int = 138,
):
    _th = _th_from_view(view)
    _BORDER = _BORDER2 = _th["border"]; _TEXT = _th["text"]; _MUTED = _th["muted"]
    _PANEL = _th["panel"]; _HDR = _th["hdr"]; _CHIP_T = _th["chip_text"]; _ALT = _ALT2 = _th["alt"]
    selected_text = next(
        (txt for key, txt in options if key == selected_key),
        options[0][1] if options else "-",
    )
    items = [
        ft.PopupMenuItem(
            content=ft.Text(str(txt), size=8),
            on_click=view._guard_ui_action(
                "stock_currency_pick",
                lambda selected=key: on_pick(selected),
                "Помилка зміни валюти",
            ),
        )
        for key, txt in options
    ]
    return ft.PopupMenuButton(
        icon=None,
        items=items,
        menu_position=ft.PopupMenuPosition.UNDER,
        content=ft.Container(
            width=width,
            height=35,
            border=ft.border.all(1, "#1F2937"),
            border_radius=8,
            bgcolor=_ALT,
            padding=ft.padding.symmetric(horizontal=6, vertical=4),
            content=ft.Column(
                [
                    ft.Text(
                        caption,
                        size=9,
                        color=_MUTED2,
                        text_align=ft.TextAlign.CENTER,
                        no_wrap=True,
                    ),
                    ft.Text(
                        selected_text,
                        size=8,
                        color=_TEXT,
                        text_align=ft.TextAlign.CENTER,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                ],
                spacing=2,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                tight=True,
            ),
        ),
    )


def build_stock_currency_bar(view) -> ft.Container:
    _th = _th_from_view(view)
    _BORDER = _BORDER2 = _th["border"]; _TEXT = _th["text"]; _MUTED = _MUTED2 = _th["muted"]
    _PANEL = _th["panel"]; _HDR = _th["hdr"]; _CHIP_T = _th["chip_text"]; _ALT = _ALT2 = _th["alt"]
    defaults = view._column_defaults()
    stock_ccy = str(defaults.get("stock_ccy") or "USD").upper().strip() or "USD"
    orders_unit_cost_ccy = (
        str(defaults.get("orders_unit_cost_ccy") or "USD").upper().strip() or "USD"
    )
    stock_new_price_ccy = (
        str(defaults.get("stock_new_price_ccy") or "USD").upper().strip() or "USD"
    )
    currency_opts = [
        (currency, f"{currency} {view._currency_symbol(currency)}")
        for currency in view.CURRENCIES
    ]
    controls = [
        _popup_filter(
            view,
            "Склад, валюта",
            stock_ccy,
            currency_opts,
            lambda value: view._set_stock_cost_currency(value),
            width=126,
        ),
        _popup_filter(
            view,
            "Нова ціна, валюта",
            stock_new_price_ccy,
            currency_opts,
            lambda value: view._set_stock_new_price_currency(value),
            width=138,
        ),
        _popup_filter(
            view,
            "С/В замовлення, валюта",
            orders_unit_cost_ccy,
            currency_opts,
            lambda value: view._set_orders_unit_cost_currency(value),
            width=156,
        ),
    ]
    return ft.Container(
        bgcolor=_ALT,
        border_radius=6,
        padding=ft.padding.symmetric(horizontal=4, vertical=4),
        content=ft.Row(controls, spacing=8, wrap=True),
    )


class CostPriceStockView:
    CURRENCIES = ["USD", "CNY", "EUR", "UAH", "PLN"]

    def __init__(self, content_area, user_data, db, active_region=None):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.active_region = active_region

        self.company_id = str(self.user_data.get("company_id") or "").strip()
        self.manager_id = str(
            self.user_data.get("active_manager_id") or self.user_data.get("id") or ""
        ).strip()
        try:
            self.packaging_enabled = bool(
                self.db.get_effective_features(self.manager_id, self.company_id).get(
                    "packaging", True
                )
            )
        except Exception:
            self.packaging_enabled = True
        self.t = i18n.bind(str(self.user_data.get("id") or ""))
        self._general_mode: bool = bool(self.user_data.get("general_mode", False))
        self._general_manager_ids: list[str] = list(
            self.user_data.get("general_manager_ids") or []
        )
        from views.general_mode_mixin import build_general_manager_names

        self._general_manager_names: dict[str, str] = build_general_manager_names(
            self.user_data.get("all_subordinates_flat") or [], self.user_data
        )
        self.manager_name = str(
            self.user_data.get("active_manager_name")
            or self.user_data.get("full_name")
            or self.t("cost_orders.default_manager", "Менеджер")
        ).strip()
        self.is_read_only = bool(self.user_data.get("subordinate_view_readonly", False))

        self._region_id = None
        self._products = []
        self._packaging = []
        self._suppliers = []
        self._categories = []
        self._price_tiers = []
        self._orders = []

        self._product_by_id = {}
        self._packaging_by_id = {}
        self._orders_unit_cost_usd_by_product = {}
        self._orders_unit_cost_ccy_by_product = {}
        self._active_order_product_ids: set = set()
        self._stock_inputs_by_product = {}

        self._stock_filter_supplier = "all"
        self._stock_filter_category = "all"
        self._stock_filter_query = ""
        self._stock_presence_filter = "all"  # all | with_qty | without_qty | with_cost | without_cost | without_both

        self._workspace_state = {}
        self._workspace_outbox = {}
        self._workspace_outbox_running = False
        self._workspace_sync_status = "synced"
        self._workspace_sync_error_count = 0
        self._sync_indicator_text = None
        self._sync_indicator_dot = None
        self._sync_indicator_container = None
        self._ops_outbox: dict[str, dict] = {}
        self._ops_flush_running = False
        self._uah_rates_cache = {}
        self._uah_rates_ts = 0.0
        self._warehouse_rows = []
        self._archived_products: list = []
        self._archived_warehouse_rows: list = []
        self._stock_archive_collapsed = True
        self._stock_computed_refs: dict[str, dict] = {}
        self._resize_refresh_state = {"last_w": 0, "pending": False}
        self._content_scroll_offset = 0.0
        self._table_h_scroll_offset = 0.0
        self._h_scroll_slot_content = None
        self._cv_body_scroll = None
        self._cv_header_scroll = None
        self._cv_header_stack_child = None
        self._h_scroll_needs = False
        self._h_scroll_step = 300
        self._v_body_col_ref = None
        self._is_rendering = False
        self._render_pending = False
        self._header_loading_indicator = None
        self._async_build_token = 0
        self._workspace_sync_loop_started = False
        self._ref_data_pulse_ts: int = 0
        self._stock_ui_ready = False
        _som = self.user_data.get("shared_orders_map")
        self._shared_orders_map: dict[str, dict] = (
            _som if isinstance(_som, dict) else {}
        )

    def _pg(self):
        if not self.content_area:
            return None
        try:
            return self.content_area.page
        except RuntimeError:
            return None
        except Exception:
            return None

    def _snack(self, text: str, color: str = _OK):
        pg = self._pg()
        if not pg:
            return
        pg.snack_bar = ft.SnackBar(
            content=ft.Text(text, color=_PANEL, weight="bold"),
            bgcolor=color,
            duration=2500,
        )
        pg.snack_bar.open = True
        pg.update()

    def _show_help(self) -> None:
        from views.help_utils import show_modal_help
        page = self._pg()
        show_modal_help(page, self.t("cost_stock.help.title", "С/В Склад"), [
            self.t("cost_stock.help.desc", "Складський залишок товарів: кількість, вартість, собівартість, рентабельність."),
            (self.t("cost_stock.help.steps_title", "Як користуватись"), None),
            self.t("cost_stock.help.step1", "1. Чіпи вгорі фільтрують товари за наявністю кількості або вартості."),
            self.t("cost_stock.help.step2", "2. «Таблиця» налаштовує видимі стовпці та їх ширину."),
            self.t("cost_stock.help.step3", "3. Дропдауни над таблицею перемикають валюту для логістики, нової ціни, С/В."),
            self.t("cost_stock.help.step4", "4. «Видимість цін» — оберіть які рівні цін відображати."),
            self.t("cost_stock.help.step5", "5. Іконка копіювання у рядку — скопіювати дані товару."),
        ])

    def _guard_ui_action(self, tag: str, action, error_text: str = "Помилка дії"):
        if self.is_read_only:

            def _ro(_e=None):
                self._snack(self.t("cost_orders.readonly_notice", "Режим перегляду — редагування заборонено"), "#64748B")
                return None

            return _ro

        def _handler(_e=None):
            try:
                return action()
            except Exception:
                logger.exception("[cost_price_stock] ui_action_failed | tag=%s", tag)
                self._snack(error_text, _ERR)
                return None

        return _handler

    def _bind_commit_handlers(
        self,
        control: ft.Control,
        commit,
        *,
        tag: str,
        error_text: str = "Помилка збереження",
    ) -> None:
        if self.is_read_only:
            with contextlib.suppress(Exception):
                control.disabled = True
            return
        last_value = str(getattr(control, "value", "") or "")

        def _handle(_e=None):
            nonlocal last_value
            current_value = str(getattr(control, "value", "") or "")
            if current_value == last_value:
                return
            try:
                commit(current_value)
                last_value = current_value
            except Exception:
                logger.exception("[cost_price_stock] commit_failed | tag=%s", tag)
                self._snack(error_text, _ERR)

        control.on_submit = _handle
        control.on_blur = _handle

    def _resolve_region_id(self) -> Optional[str]:
        raw = str(self.active_region or "").strip()
        if not raw:
            return None
        if len(raw) >= 24 and "-" not in raw:
            return raw
        regions = self.db.get_company_regions(self.company_id) or []
        target = raw.lower()
        for region in regions:
            if not isinstance(region, dict):
                continue
            region_id = str(region.get("id") or "").strip()
            name = str(region.get("name") or "").strip().lower()
            if region_id == raw or name == target:
                return region_id
        return None

    def _load_workspace_state(self):
        user_id = str((self.user_data or {}).get("id") or "").strip()
        if not user_id:
            self._workspace_state = {}
            return
        local_path = self._workspace_state_file_path()
        if os.path.exists(local_path):
            try:
                import json as _json

                with open(local_path, "r", encoding="utf-8") as f:
                    loaded = _json.load(f)
                if isinstance(loaded, dict):
                    self._workspace_state = loaded
                    return
            except Exception:
                pass
        self._workspace_state = self.db.get_manager_workspace_state(user_id) or {}
        self._save_workspace_state_to_file()

    def _app_data_dir(self) -> str:
        pg = self._pg()
        path = ""
        with contextlib.suppress(Exception):
            path = str(getattr(pg, "client_storage_path", "") or "") if pg else ""
        if not path:
            path = os.getenv("FLET_APP_STORAGE_DATA") or os.path.join(
                os.path.expanduser("~"), ".catmanpro"
            )
        os.makedirs(path, exist_ok=True)
        return path

    def _workspace_outbox_file_path(self) -> str:
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        return os.path.join(
            self._app_data_dir(),
            f"cost_price_stock_workspace_outbox_{safe_cid}_{safe_mgr}.json",
        )

    def _workspace_state_file_path(self) -> str:
        uid = (
            str((self.user_data or {}).get("id") or "nouser")
            .replace("/", "_")
            .replace("\\", "_")
        )
        cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        rid = str(self._region_id or "all").replace("/", "_").replace("\\", "_")
        return os.path.join(
            self._app_data_dir(),
            f"cp_stock_workspace_state_{cid}_{uid}_{rid}.json",
        )

    def _save_workspace_state_to_file(self) -> None:
        if not self._workspace_state:
            return
        try:
            with open(self._workspace_state_file_path(), "w", encoding="utf-8") as f:
                import json as _json

                _json.dump(self._workspace_state, f, ensure_ascii=False, default=str)
        except Exception:
            pass

    def _ops_outbox_file_path(self) -> str:
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        return os.path.join(
            self._app_data_dir(),
            f"cost_price_stock_ops_outbox_{safe_cid}_{safe_mgr}.json",
        )

    def _load_ops_outbox(self) -> None:
        path = self._ops_outbox_file_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            items = raw.get("items") if isinstance(raw, dict) else None
            if isinstance(items, dict):
                self._ops_outbox = {
                    str(k): dict(v) for k, v in items.items() if isinstance(v, dict)
                }
        except Exception:
            pass

    def _save_ops_outbox(self) -> None:
        try:
            path = self._ops_outbox_file_path()
            if not self._ops_outbox:
                with contextlib.suppress(Exception):
                    if os.path.exists(path):
                        os.remove(path)
                return
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "items": self._ops_outbox,
                        "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
                    },
                    f,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            os.replace(tmp, path)
        except Exception:
            pass

    def _enqueue_stock_op(self, product_id: str, patch_keys: set | None = None) -> None:
        """Queue a stock-input save. patch_keys=None means full row; otherwise only
        merge the specified keys into the existing outbox entry so prior unsaved
        fields (e.g. amount typed but not yet flushed) are never overwritten."""
        pid = str(product_id or "").strip()
        if not pid:
            return
        full_row = dict(self._stock_inputs_by_product.get(pid) or {})
        key = f"stock:{self.company_id}:{pid}"
        existing = self._ops_outbox.get(key)
        if patch_keys and isinstance(existing, dict):
            # Merge only the changed fields; keep everything else already queued.
            merged = dict(existing.get("data") or {})
            for k in patch_keys:
                merged[k] = full_row.get(k)
            # ccy is required for DB upsert — always keep it current.
            merged["ccy"] = full_row.get("ccy", "USD")
            merged["new_ccy"] = full_row.get("new_ccy", "USD")
            existing["data"] = merged
        else:
            self._ops_outbox[key] = {
                "kind": "stock_input",
                "company_id": self.company_id,
                "product_id": pid,
                "data": full_row,
                "region_id": self._region_id,
                "manager_id": self.manager_id,
            }
        self._save_ops_outbox()
        self._refresh_workspace_sync_indicator_state()
        self._schedule_ops_flush()

    _ops_flush_debounce_seq: int = 0

    def _schedule_ops_flush(self, delay: float = 0.2) -> None:
        """Trigger a debounced flush ~0.2 s after the last change."""
        self._ops_flush_debounce_seq = getattr(self, "_ops_flush_debounce_seq", 0) + 1
        my_seq = self._ops_flush_debounce_seq
        pg = self._pg()
        if not pg or not hasattr(pg, "run_task"):
            return

        async def _do(seq: int = my_seq) -> None:
            await asyncio.sleep(delay)
            if getattr(self, "_ops_flush_debounce_seq", 0) != seq:
                return
            await self._flush_ops_async()

        with contextlib.suppress(Exception):
            pg.run_task(_do)

    async def flush_pending_async(self) -> None:
        if self._ops_outbox:
            with contextlib.suppress(Exception):
                await self._flush_ops_async()
        if self._workspace_outbox:
            with contextlib.suppress(Exception):
                await self._flush_workspace_state_async()

    async def _flush_ops_async(self) -> None:
        if self._ops_flush_running or not self._ops_outbox:
            self._refresh_workspace_sync_indicator_state()
            return
        self._ops_flush_running = True
        self._refresh_workspace_sync_indicator_state()
        error_count = 0
        try:
            keys = list(self._ops_outbox.keys())

            async def _flush_one(key: str):
                op = self._ops_outbox.get(key)
                if not isinstance(op, dict):
                    self._ops_outbox.pop(key, None)
                    return True
                kind = str(op.get("kind") or "")
                try:
                    if kind == "stock_input":
                        ok = await asyncio.to_thread(
                            self.db.upsert_cost_price_stock_input,
                            op["company_id"],
                            op["product_id"],
                            op["data"],
                            region_id=op.get("region_id"),
                            manager_id=op.get("manager_id"),
                        )
                    else:
                        ok = True
                except Exception:
                    ok = False
                if ok:
                    self._ops_outbox.pop(key, None)
                return ok

            results = await asyncio.gather(
                *[_flush_one(k) for k in keys], return_exceptions=True
            )
            error_count = sum(
                1 for r in results if r is not True and not (isinstance(r, bool) and r)
            )
            self._save_ops_outbox()
        except Exception:
            error_count += 1
        finally:
            self._ops_flush_running = False
            if error_count > 0:
                self._set_workspace_sync_status("error")
            else:
                self._refresh_workspace_sync_indicator_state()

    def _apply_ops_outbox_to_local(self) -> None:
        for op in self._ops_outbox.values():
            if not isinstance(op, dict):
                continue
            if op.get("kind") == "stock_input":
                pid = str(op.get("product_id") or "")
                data = op.get("data") or {}
                if pid and isinstance(data, dict):
                    if pid not in self._stock_inputs_by_product or not isinstance(
                        self._stock_inputs_by_product.get(pid), dict
                    ):
                        self._stock_inputs_by_product[pid] = {}
                    self._stock_inputs_by_product[pid].update(data)

    def _load_workspace_outbox(self) -> None:
        path = self._workspace_outbox_file_path()
        if not os.path.exists(path):
            self._refresh_workspace_sync_indicator_state()
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            payload = raw.get("payload") if isinstance(raw, dict) else None
            if isinstance(payload, dict):
                self._workspace_outbox = dict(payload)
                self._workspace_state = dict(payload)
        except Exception:
            pass
        self._refresh_workspace_sync_indicator_state()

    def _save_workspace_outbox(self) -> None:
        try:
            payload = {
                "payload": (
                    self._workspace_state
                    if isinstance(self._workspace_state, dict)
                    else {}
                ),
                "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
            }
            path = self._workspace_outbox_file_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
            self._workspace_outbox = dict(payload["payload"])
        except Exception:
            self._set_workspace_sync_status("error")

    def _refresh_workspace_sync_indicator_state(self) -> None:
        if self._workspace_outbox_running or self._ops_flush_running:
            self._set_workspace_sync_status("syncing")
            return
        if self._workspace_outbox or self._ops_outbox:
            self._set_workspace_sync_status("pending")
            return
        self._set_workspace_sync_status("synced")

    def _set_workspace_sync_status(self, status: str, error_count: int = 0) -> None:
        self._workspace_sync_status = str(status or "synced")
        if status == "error":
            self._workspace_sync_error_count = max(
                int(error_count or 0), self._workspace_sync_error_count
            )
        elif status == "synced":
            self._workspace_sync_error_count = 0

        txt = self._sync_indicator_text
        dot = self._sync_indicator_dot
        ctr = self._sync_indicator_container
        if txt is None or dot is None or ctr is None:
            return
        if status == "synced":
            txt.value = self.t("orders.sync.synced", "Збережено")
            txt.color = _OK
            dot.bgcolor = _OK
            ctr.tooltip = self.t("cost_orders.sync.synced_tip", "Workspace state вже в БД. Натисніть для примусової синхронізації.")
        elif status == "pending":
            txt.value = self.t("orders.sync.pending", "Локально")
            txt.color = _WARN_DK
            dot.bgcolor = _WARN
            ctr.tooltip = self.t("cost_orders.sync.pending_tip", "Workspace state збережено локально і ще не передано в БД. Натисніть для синхронізації.")
        elif status == "syncing":
            txt.value = self.t("orders.sync.syncing", "Оновлення...")
            txt.color = _ACCENT
            dot.bgcolor = _ACCENT2
            ctr.tooltip = self.t("cost_orders.sync.syncing_tip", "Йде передача workspace state в БД.")
        elif status == "error":
            txt.value = self.t("orders.sync.error", "Не збережено")
            txt.color = _ERR
            dot.bgcolor = _ERR
            ctr.tooltip = self.t("cost_orders.sync.error_tip", "Workspace state не вдалося передати в БД. Натисніть для повторної спроби.")
        with contextlib.suppress(Exception):
            ctr.update()

    def _build_workspace_sync_indicator(self) -> ft.Container:
        self._sync_indicator_dot = ft.Container(
            width=8, height=8, border_radius=4, bgcolor=_BORDER2
        )
        self._sync_indicator_text = ft.Text("", size=10, color=_MUTED)

        def _on_force_sync(_):
            page = self._pg()
            if page and hasattr(page, "run_task"):
                with contextlib.suppress(Exception):
                    page.run_task(self._force_sync_workspace_state_async)

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
            on_click=_on_force_sync,
            ink=True,
        )
        self._refresh_workspace_sync_indicator_state()
        return self._sync_indicator_container

    async def _flush_workspace_state_async(self) -> None:
        if self._workspace_outbox_running:
            return
        if not self._workspace_state:
            if self._workspace_outbox:
                self._workspace_outbox = {}
                with contextlib.suppress(Exception):
                    path = self._workspace_outbox_file_path()
                    if os.path.exists(path):
                        os.remove(path)
                self._refresh_workspace_sync_indicator_state()
            return
        self._workspace_outbox_running = True
        self._set_workspace_sync_status("syncing")
        error_count = 0
        try:
            user_id = str((self.user_data or {}).get("id") or "").strip()
            if not user_id:
                self._set_workspace_sync_status("error")
                return
            snapshot = (
                dict(self._workspace_state)
                if isinstance(self._workspace_state, dict)
                else {}
            )
            ok = await asyncio.to_thread(
                self.db.save_manager_workspace_state, user_id, snapshot
            )
            if ok:
                self._workspace_state = snapshot
                self._save_workspace_state_to_file()
                self._workspace_outbox = {}
                with contextlib.suppress(Exception):
                    path = self._workspace_outbox_file_path()
                    if os.path.exists(path):
                        os.remove(path)
            else:
                error_count += 1
        except Exception:
            error_count += 1
        finally:
            self._workspace_outbox_running = False
            if error_count > 0:
                self._set_workspace_sync_status("error", error_count)
            else:
                self._refresh_workspace_sync_indicator_state()

    async def _force_sync_workspace_state_async(self) -> None:
        await self._flush_workspace_state_async()
        if self._ops_outbox:
            await self._flush_ops_async()

    def _queue_workspace_state_save(self) -> None:
        self._save_workspace_outbox()
        self._refresh_workspace_sync_indicator_state()
        page = self._pg()
        if page and hasattr(page, "run_task"):
            with contextlib.suppress(Exception):
                page.run_task(self._flush_workspace_state_async)

    def _save_workspace_state(self):
        self._queue_workspace_state_save()

    def _restart_tab(self) -> None:
        """Full tab restart after column changes — saves to local+DB then rebuilds via ctrl+f5."""
        import contextlib as _ctx
        # Persist current state to local file immediately (synchronous).
        with _ctx.suppress(Exception):
            self._save_workspace_state_to_file()
        pg = self._pg()
        if pg is None:
            self._schedule_render(0.05)
            return
        dash = getattr(pg, "_dashboard", None)
        if dash is None:
            self._schedule_render(0.05)
            return

        async def _do_restart():
            # Flush workspace state to DB before rebuilding.
            with _ctx.suppress(Exception):
                await self._force_sync_workspace_state_async()
            with _ctx.suppress(Exception):
                await dash._handle_ctrl_f5()

        with _ctx.suppress(Exception):
            pg.run_task(_do_restart)

    def _state_bucket(self) -> dict:
        key = f"cost_profit:{self.company_id}:{self._region_id or 'all'}"
        if not isinstance(self._workspace_state.get("cost_profit"), dict):
            self._workspace_state["cost_profit"] = {}
        bucket = self._workspace_state["cost_profit"].get(key)
        if not isinstance(bucket, dict):
            bucket = {
                "column_defaults": {
                    "stock_ccy": "USD",
                    "stock_new_price_ccy": "USD",
                    "orders_unit_cost_ccy": "USD",
                },
            }
            self._workspace_state["cost_profit"][key] = bucket
        return bucket

    def _column_defaults(self) -> dict:
        bucket = self._state_bucket()
        defaults = (
            bucket.get("column_defaults")
            if isinstance(bucket.get("column_defaults"), dict)
            else {}
        )
        normalized = dict(defaults)
        normalized["stock_ccy"] = (
            str(defaults.get("stock_ccy") or "USD").upper().strip() or "USD"
        )
        normalized["stock_new_price_ccy"] = (
            str(defaults.get("stock_new_price_ccy") or "USD").upper().strip() or "USD"
        )
        normalized["orders_unit_cost_ccy"] = (
            str(defaults.get("orders_unit_cost_ccy") or "USD").upper().strip() or "USD"
        )
        visible_raw = defaults.get("stock_visible_tier_ids")
        if isinstance(visible_raw, list):
            normalized["stock_visible_tier_ids"] = [
                str(v).strip() for v in visible_raw if str(v).strip()
            ]
        elif "stock_visible_tier_ids" in defaults:
            normalized["stock_visible_tier_ids"] = []
        visible_base_raw = defaults.get("stock_visible_base_column_ids")
        if isinstance(visible_base_raw, list):
            normalized["stock_visible_base_column_ids"] = [
                str(v).strip() for v in visible_base_raw if str(v).strip()
            ]
        elif "stock_visible_base_column_ids" in defaults:
            normalized["stock_visible_base_column_ids"] = []
        order_raw = defaults.get("stock_base_column_order_ids")
        if isinstance(order_raw, list):
            normalized["stock_base_column_order_ids"] = [
                str(v).strip() for v in order_raw if str(v).strip()
            ]
        elif "stock_base_column_order_ids" in defaults:
            normalized["stock_base_column_order_ids"] = []
        widths_raw = defaults.get("stock_col_width_px")
        if isinstance(widths_raw, dict):
            normalized_widths = {}
            for raw_key, raw_value in widths_raw.items():
                key = str(raw_key or "").strip()
                if not key:
                    continue
                try:
                    width = int(float(raw_value))
                except Exception:
                    continue
                if width <= 0:
                    continue
                normalized_widths[key] = max(35, min(1200, width))
            normalized["stock_col_width_px"] = normalized_widths
        elif "stock_col_width_px" in defaults:
            normalized["stock_col_width_px"] = {}
        if defaults != normalized:
            bucket["column_defaults"] = normalized
        return normalized

    def _patch_column_defaults(self, patch: dict) -> None:
        if not isinstance(patch, dict) or not patch:
            return
        bucket = self._state_bucket()
        defaults = (
            bucket.get("column_defaults")
            if isinstance(bucket.get("column_defaults"), dict)
            else {}
        )
        merged_defaults = dict(defaults)
        for key, value in patch.items():
            merged_defaults[key] = value
        bucket["column_defaults"] = merged_defaults
        self._save_workspace_state_to_file()
        self._save_workspace_state()

    def _all_stock_base_column_defs(self) -> list[dict]:
        defaults = self._column_defaults()
        stock_ccy = str(defaults.get("stock_ccy") or "USD").upper().strip() or "USD"
        orders_unit_cost_ccy = (
            str(defaults.get("orders_unit_cost_ccy") or "USD").upper().strip() or "USD"
        )
        stock_new_price_ccy = (
            str(defaults.get("stock_new_price_ccy") or "USD").upper().strip() or "USD"
        )
        width_map = (
            defaults.get("stock_col_width_px")
            if isinstance(defaults.get("stock_col_width_px"), dict)
            else {}
        )

        def _width(key: str, fallback: int) -> int:
            raw = width_map.get(key)
            try:
                value = int(float(raw))
            except Exception:
                value = int(fallback)
            return max(35, min(1200, value))

        return [
            {"key": "product", "label": self.t("cost_stock.col.product", "Товар"), "width": _width("product", 200)},
            {"key": "qty", "label": self.t("cost_stock.col.qty", "К-сть склад"), "width": _width("qty", 75)},
            {
                "key": "amount_stock",
                "label": f"{self.t('cost_stock.col.amount_stock', 'Вартість склад')} {self._currency_symbol(stock_ccy)}",
                "width": _width("amount_stock", 90),
            },
            {
                "key": "unit_cost_stock",
                "label": f"{self.t('cost_stock.col.unit_cost_stock', 'С/В склад')} {self._currency_symbol(stock_ccy)}",
                "width": _width("unit_cost_stock", 70),
            },
            {
                "key": "orders_unit_cost",
                "label": f"{self.t('cost_stock.col.orders_unit_cost', 'С/В замовлення')} {self._currency_symbol(orders_unit_cost_ccy)}",
                "width": _width("orders_unit_cost", 80),
            },
            {
                "key": "unit_cost_blended",
                "label": f"{self.t('cost_stock.col.unit_cost_blended', 'С/В')} {self._currency_symbol(stock_ccy)}",
                "width": _width("unit_cost_blended", 65),
            },
            {
                "key": "new_price",
                "label": f"{self.t('cost_stock.col.new_price', 'Нова ціна')} {self._currency_symbol(stock_new_price_ccy)}",
                "width": _width("new_price", 50),
            },
            {
                "key": "new_margin",
                "label": self.t("cost_stock.col.new_margin", "Рентаб. нова"),
                "width": _width("new_margin", 50),
            },
        ]

    def _visible_stock_base_column_ids(self) -> set[str]:
        defaults = self._column_defaults()
        all_ids = [
            str(col.get("key") or "").strip()
            for col in self._all_stock_base_column_defs()
            if str(col.get("key") or "").strip()
        ]
        raw = defaults.get("stock_visible_base_column_ids")
        if not isinstance(raw, list):
            return set(all_ids)
        selected = {str(v).strip() for v in raw if str(v).strip()}
        filtered = {col_id for col_id in all_ids if col_id in selected}
        if not filtered:
            filtered = set(all_ids)
        # Product name column must stay visible as the primary row identifier.
        filtered.add("product")
        return filtered

    def _ordered_stock_base_column_ids(self) -> list[str]:
        defaults = self._column_defaults()
        all_ids = [
            str(col.get("key") or "").strip()
            for col in self._all_stock_base_column_defs()
            if str(col.get("key") or "").strip()
        ]
        raw = defaults.get("stock_base_column_order_ids")
        if not isinstance(raw, list):
            normalized = list(all_ids)
        else:
            normalized_raw = [str(v).strip() for v in raw if str(v).strip()]
            normalized = [cid for cid in normalized_raw if cid in all_ids] + [
                cid for cid in all_ids if cid not in set(normalized_raw)
            ]
        # Keep product name frozen as first column.
        normalized = [cid for cid in normalized if cid != "product"]
        return ["product"] + normalized

    def _set_visible_stock_base_column_ids(self, column_ids: list[str]) -> None:
        all_ids = [
            str(col.get("key") or "").strip()
            for col in self._all_stock_base_column_defs()
            if str(col.get("key") or "").strip()
        ]
        chosen = {str(v).strip() for v in (column_ids or []) if str(v).strip()}
        normalized = [col_id for col_id in all_ids if col_id in chosen]
        if not normalized:
            normalized = list(all_ids)
        if "product" not in normalized:
            normalized.insert(0, "product")
        self._patch_column_defaults({"stock_visible_base_column_ids": normalized})
        self._render()

    def _set_stock_base_column_order_ids(self, column_ids: list[str]) -> None:
        all_ids = [
            str(col.get("key") or "").strip()
            for col in self._all_stock_base_column_defs()
            if str(col.get("key") or "").strip()
        ]
        normalized_raw = [str(v).strip() for v in (column_ids or []) if str(v).strip()]
        normalized = [cid for cid in normalized_raw if cid in all_ids] + [
            cid for cid in all_ids if cid not in set(normalized_raw)
        ]
        normalized = [cid for cid in normalized if cid != "product"]
        normalized = ["product"] + normalized
        self._patch_column_defaults({"stock_base_column_order_ids": normalized})

    def _open_stock_base_columns_dialog(self) -> None:
        page = self._pg()
        if not page:
            return

        defaults = self._column_defaults()

        selected_cols = set(self._visible_stock_base_column_ids())
        ordered_ids = list(self._ordered_stock_base_column_ids())
        by_key = {
            str(col.get("key") or "").strip(): col
            for col in self._all_stock_base_column_defs()
        }
        width_inputs: dict[str, ft.TextField] = {}
        cols_host = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO)

        def _render_col_rows():
            controls = []
            total = len(ordered_ids)
            for idx, key in enumerate(ordered_ids):
                col = by_key.get(key)
                if not col:
                    continue
                label = str(col.get("label") or key).strip() or key
                cb = ft.Checkbox(label=label, value=(key in selected_cols))
                width_value = str(int(col.get("width") or 0) or "")
                width_field = ft.TextField(
                    value=width_value,
                    width=68,
                    dense=True,
                    text_size=11,
                    content_padding=ft.padding.symmetric(horizontal=6, vertical=6),
                    keyboard_type=ft.KeyboardType.NUMBER,
                    hint_text="Шир.",
                )
                width_inputs[key] = width_field

                def _toggle_col(e, current_key=key):
                    if bool(getattr(e.control, "value", False)):
                        selected_cols.add(current_key)
                    else:
                        selected_cols.discard(current_key)

                def _move_up(_e, i=idx):
                    if i <= 0:
                        return
                    ordered_ids[i - 1], ordered_ids[i] = (
                        ordered_ids[i],
                        ordered_ids[i - 1],
                    )
                    _render_col_rows()

                def _move_down(_e, i=idx):
                    if i >= len(ordered_ids) - 1:
                        return
                    ordered_ids[i + 1], ordered_ids[i] = (
                        ordered_ids[i],
                        ordered_ids[i + 1],
                    )
                    _render_col_rows()

                cb.on_change = _toggle_col
                controls.append(
                    ft.Row(
                        [
                            ft.Container(width=200, content=cb),
                            ft.IconButton(
                                icon=ft.Icons.KEYBOARD_ARROW_UP,
                                icon_size=15,
                                tooltip="Вгору",
                                disabled=(idx == 0),
                                on_click=_move_up,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.KEYBOARD_ARROW_DOWN,
                                icon_size=15,
                                tooltip="Вниз",
                                disabled=(idx == total - 1),
                                on_click=_move_down,
                            ),
                            ft.Container(width=76, content=width_field),
                        ],
                        spacing=2,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )
            cols_host.controls = controls
            try:
                cols_host.update()
            except Exception:
                pass

        _render_col_rows()

        tiers = [
            tier
            for tier in (self._price_tiers or [])
            if isinstance(tier, dict) and str((tier or {}).get("id") or "").strip()
        ]
        selected_tiers = set(self._visible_stock_tier_ids())
        tier_widths_cfg = defaults.get("stock_tier_widths") or {}

        tier_width_inputs: dict[str, dict] = {}
        tier_rows = []

        tier_rows.append(
            ft.Row(
                [
                    ft.Container(
                        width=50,
                        content=ft.Text("Назва рівня", size=9, color=_MUTED),
                    ),
                    ft.Container(
                        width=50, content=ft.Text("Ціна px", size=9, color=_MUTED)
                    ),
                    ft.Container(
                        width=50, content=ft.Text("Рент. px", size=9, color=_MUTED)
                    ),
                ],
                spacing=4,
            )
        )

        for tier in tiers:
            tier_id = str((tier or {}).get("id") or "").strip()
            tier_name = str((tier or {}).get("name") or "Рівень").strip() or "Рівень"
            display_name = tier_name if len(tier_name) <= 22 else tier_name[:20] + "…"
            cb_t = ft.Checkbox(
                label=display_name,
                value=(tier_id in selected_tiers),
                label_style=ft.TextStyle(size=11),
                tooltip=tier_name if len(tier_name) > 22 else None,
            )

            def _toggle_tier(e, tid=tier_id):
                if bool(getattr(e.control, "value", False)):
                    selected_tiers.add(tid)
                else:
                    selected_tiers.discard(tid)

            cb_t.on_change = _toggle_tier

            _tw = tier_widths_cfg.get(tier_id) or {}
            pw_field = ft.TextField(
                value=str(int(_tw.get("price_w") or 50)),
                width=56,
                dense=True,
                text_size=11,
                content_padding=ft.padding.symmetric(horizontal=6, vertical=6),
                keyboard_type=ft.KeyboardType.NUMBER,
                hint_text="px",
            )
            mw_field = ft.TextField(
                value=str(int(_tw.get("margin_w") or 50)),
                width=56,
                dense=True,
                text_size=11,
                content_padding=ft.padding.symmetric(horizontal=6, vertical=6),
                keyboard_type=ft.KeyboardType.NUMBER,
                hint_text="px",
            )
            tier_width_inputs[tier_id] = {"price_w": pw_field, "margin_w": mw_field}

            tier_rows.append(
                ft.Row(
                    [
                        ft.Container(
                            width=50,
                            content=cb_t,
                            clip_behavior=ft.ClipBehavior.HARD_EDGE,
                        ),
                        ft.Container(width=50, content=pw_field),
                        ft.Container(width=50, content=mw_field),
                    ],
                    spacing=4,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
            )

        new_price_col = by_key.get("new_price") or {}
        new_price_cb = ft.Checkbox(
            label=str(new_price_col.get("label") or "Нова ціна"),
            value=("new_price" in selected_cols),
            label_style=ft.TextStyle(size=11),
        )
        new_price_cb.on_change = lambda e: (
            selected_cols.add("new_price")
            if bool(getattr(e.control, "value", False))
            else selected_cols.discard("new_price")
        )
        _wdef = defaults.get("stock_col_width_px") or {}
        np_w_input = ft.TextField(
            value=str(int(_wdef.get("new_price") or 50)),
            width=50,
            dense=True,
            text_size=11,
            content_padding=ft.padding.symmetric(horizontal=6, vertical=6),
            keyboard_type=ft.KeyboardType.NUMBER,
            hint_text="px",
        )

        if tiers:
            tier_rows.append(ft.Divider(height=8, color=_HDR))

        tier_rows.append(
            ft.Row(
                [
                    ft.Container(
                        width=50,
                        content=new_price_cb,
                        clip_behavior=ft.ClipBehavior.HARD_EDGE,
                    ),
                    ft.Container(width=50, content=np_w_input),
                    ft.Container(width=50, content=ft.Text("", size=9)),
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )

        content_col = ft.Column(
            [
                ft.Text("Стовпці", size=12, weight="bold", color=_CHIP_T),
                ft.Container(height=240, content=cols_host),
                ft.Divider(height=12, color=_HDR),
                ft.Text("Ціни", size=12, weight="bold", color=_CHIP_T),
                ft.Column(tier_rows, spacing=4, tight=True),
            ],
            spacing=6,
            scroll=ft.ScrollMode.AUTO,
        )

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text("Таблиця"),
            content=ft.Container(width=460, height=540, content=content_col),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _e: page.pop_dialog()),
                ft.FilledButton(
                    "Зберегти",
                    on_click=lambda _e: self._save_stock_table_dialog(
                        page,
                        dialog,
                        ordered_ids,
                        selected_cols,
                        width_inputs,
                        selected_tiers,
                        tier_width_inputs,
                        new_price_cb,
                        np_w_input,
                    ),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dialog)

    def _save_stock_table_dialog(
        self,
        page,
        dialog,
        ordered_ids: list[str],
        selected_cols: set[str],
        width_inputs: dict[str, ft.TextField],
        selected_tiers: set[str],
        tier_width_inputs: dict[str, dict],
        new_price_cb,
        np_w_input,
    ) -> None:
        width_patch: dict = {}
        for key, field in (width_inputs or {}).items():
            raw = str(getattr(field, "value", "") or "").strip()
            try:
                w = int(float(raw))
            except Exception:
                continue
            if w <= 0:
                continue
            width_patch[key] = max(35, min(1200, w))

        if bool(getattr(new_price_cb, "value", False)):
            selected_cols.add("new_price")
        else:
            selected_cols.discard("new_price")
        try:
            np_w = int(float(str(getattr(np_w_input, "value", "") or "").strip()))
            if np_w > 0:
                width_patch["new_price"] = max(70, min(1200, np_w))
        except Exception:
            pass

        tier_widths_patch: dict = {}
        all_tier_ids = self._all_stock_tier_ids()
        for tid, fields in (tier_width_inputs or {}).items():
            pw_field = fields.get("price_w")
            mw_field = fields.get("margin_w")
            try:
                pw = int(float(str(getattr(pw_field, "value", "") or "").strip()))
            except Exception:
                pw = 50
            try:
                mw = int(float(str(getattr(mw_field, "value", "") or "").strip()))
            except Exception:
                mw = 50
            tier_widths_patch[tid] = {
                "price_w": max(30, min(400, pw)),
                "margin_w": max(30, min(400, mw)),
            }

        self._patch_column_defaults(
            {
                "stock_base_column_order_ids": list(ordered_ids),
                "stock_visible_base_column_ids": list(selected_cols),
                "stock_col_width_px": width_patch,
                "stock_visible_tier_ids": [
                    tid for tid in all_tier_ids if tid in selected_tiers
                ],
                "stock_tier_widths": tier_widths_patch,
            }
        )
        page.pop_dialog()
        self._restart_tab()

    def _all_stock_tier_ids(self) -> list[str]:
        return [
            str((tier or {}).get("id") or "").strip()
            for tier in (self._price_tiers or [])
            if isinstance(tier, dict) and str((tier or {}).get("id") or "").strip()
        ]

    def _visible_stock_tier_ids(self) -> set[str]:
        all_ids = self._all_stock_tier_ids()
        defaults = self._column_defaults()
        raw = defaults.get("stock_visible_tier_ids")
        if not isinstance(raw, list):
            return set(all_ids)
        selected = {str(v).strip() for v in raw if str(v).strip()}
        return {tid for tid in all_ids if tid in selected}

    def _set_visible_stock_tier_ids(self, tier_ids: list[str]) -> None:
        all_ids = self._all_stock_tier_ids()
        chosen = {str(v).strip() for v in (tier_ids or []) if str(v).strip()}
        self._patch_column_defaults(
            {"stock_visible_tier_ids": [tid for tid in all_ids if tid in chosen]}
        )
        self._restart_tab()

    def _open_stock_price_columns_dialog(self) -> None:
        page = self._pg()
        if not page:
            return
        tiers = [
            tier
            for tier in (self._price_tiers or [])
            if isinstance(tier, dict) and str((tier or {}).get("id") or "").strip()
        ]
        if not tiers:
            self._snack("Немає цінових рівнів", _WARN)
            return

        selected = set(self._visible_stock_tier_ids())
        checkboxes = []

        for tier in tiers:
            tier_id = str((tier or {}).get("id") or "").strip()
            tier_name = str((tier or {}).get("name") or "Рівень").strip() or "Рівень"
            cb = ft.Checkbox(label=tier_name, value=(tier_id in selected))

            def _toggle(e, current_id=tier_id):
                if bool(getattr(e.control, "value", False)):
                    selected.add(current_id)
                else:
                    selected.discard(current_id)

            cb.on_change = _toggle
            checkboxes.append(cb)

        dlg = ft.AlertDialog(modal=True)

        def _close(_e=None):
            try:
                if hasattr(page, "close_dialog"):
                    page.close_dialog()
                else:
                    dlg.open = False
                    with contextlib.suppress(Exception):
                        dlg.update()
            except Exception:
                with contextlib.suppress(Exception):
                    dlg.open = False
                    dlg.update()

        def _apply(_e=None):
            self._set_visible_stock_tier_ids(list(selected))
            _close()

        def _prices_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, "Видимість цін", [
                self.t("cost_stock.prices_help.step1", "1. Відмітьте рівні цін які потрібно відображати в таблиці."),
                self.t("cost_stock.prices_help.step2", "2. Натисніть «Застосувати»."),
            ])

        dlg.title = ft.Row(
            [
                ft.Text("Видимість цін", expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_prices_help),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
        )
        dlg.content = ft.Container(
            width=360,
            content=ft.Column(
                checkboxes,
                spacing=4,
                tight=True,
                scroll=ft.ScrollMode.AUTO,
            ),
        )
        dlg.actions = [
            ft.TextButton("Скасувати", on_click=_close),
            ft.FilledButton("Застосувати", on_click=_apply),
        ]
        dlg.actions_alignment = ft.MainAxisAlignment.END
        try:
            if hasattr(page, "show_dialog"):
                page.show_dialog(dlg)
            else:
                page.dialog = dlg
                dlg.open = True
                with contextlib.suppress(Exception):
                    dlg.update()
        except Exception:
            logger.exception("[cost_price_stock] open_price_columns_dialog_failed")
            self._snack("Не вдалося відкрити список цін", _ERR)

    def _currency_symbol(self, ccy: str) -> str:
        c = str(ccy or "").upper().strip()
        return {
            "USD": "$",
            "UAH": "₴",
            "EUR": "€",
            "PLN": "zł",
            "CNY": "¥",
        }.get(c, c)

    def _fmt_money(self, val: Optional[float], ccy: str = "") -> str:
        if val is None:
            return ""
        amount = format_grouped(val, decimals=2, trim=True)
        if not ccy:
            return amount
        return f"{amount} {self._currency_symbol(ccy)}"

    def _fetch_uah_rates(self) -> dict:
        # Delegates to the shared fx_rates module (also used by
        # cost_price_orders_table.py, orders_view.py, products_view.py) so
        # every screen fetches/caches live rates identically.
        return fx_rates.fetch_uah_rates()

    def _get_uah_rates_cached(self) -> dict:
        return fx_rates.get_uah_rates()

    def _convert_currency(
        self, amount: Optional[float], from_ccy: str, to_ccy: str
    ) -> Optional[float]:
        return fx_rates.convert(amount, from_ccy, to_ccy)

    def _sales_price_for_tier(
        self, product: dict, tier: dict
    ) -> tuple[Optional[float], str]:
        if not isinstance(product, dict) or not isinstance(tier, dict):
            return None, str((tier or {}).get("currency") or "USD").upper()
        sales = product.get("sales_prices_json")
        rows = []
        if isinstance(sales, list):
            rows = sales
        elif isinstance(sales, str) and sales.strip():
            try:
                parsed = json.loads(sales)
                if isinstance(parsed, list):
                    rows = parsed
            except Exception:
                rows = []
        tid = str(tier.get("id") or "").strip()
        tname = str(tier.get("name") or "").strip().lower()
        tccy = str(tier.get("currency") or "USD").upper().strip() or "USD"
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("tier_id") or "").strip() == tid:
                return _safe_float(row.get("price"), None), tccy
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("name") or "").strip().lower() == tname:
                return _safe_float(row.get("price"), None), tccy
        return None, tccy

    def _packaging_price_for_product(self, product: dict) -> tuple[float, str]:
        if not isinstance(product, dict):
            return 0.0, "USD"
        if not self.packaging_enabled:
            return 0.0, str(product.get("purchase_currency") or "USD").upper()
        packaging_id = str(product.get("packaging_id") or "").strip()
        if not packaging_id:
            return 0.0, str(product.get("purchase_currency") or "USD").upper()
        packaging = self._packaging_by_id.get(packaging_id) or {}
        pack_amount = _safe_float(packaging.get("price"), 0.0) or 0.0
        pack_ccy = (
            str(
                (
                    (packaging.get("suppliers") or {}).get("currency")
                    if isinstance(packaging.get("suppliers"), dict)
                    else ""
                )
                or product.get("purchase_currency")
                or "USD"
            )
            .upper()
            .strip()
            or "USD"
        )
        return pack_amount, pack_ccy

    def _order_item_basis_weight(self, item: dict, product: dict) -> float:
        weight_fact = _safe_float((item or {}).get("weight_actual_kg"), None)
        if weight_fact is not None and weight_fact >= 0:
            return float(weight_fact)
        qty = (
            _safe_float((item or {}).get("qty") or (item or {}).get("quantity"), 0.0)
            or 0.0
        )
        weight_unit = _safe_float((item or {}).get("weight_per_unit_kg"), None)
        if weight_unit is None and isinstance(product, dict):
            weight_unit = _safe_float(product.get("weight_kg"), 0.0)
        return max(0.0, float(qty) * float(weight_unit or 0.0))

    def _cost_pivot_currency_for_product(self, product: dict, cache: dict) -> str:
        """Currency to compute this product's cost price in: its own
        currency if purchase price + every sale-tier price share one
        currency, otherwise USD. Re-evaluated fresh per build so a later
        currency change (e.g. a tier switched to a different currency)
        automatically flips the product back to USD next time."""
        pid = str((product or {}).get("id") or "").strip()
        if pid in cache:
            return cache[pid]
        purchase_ccy = (
            str((product or {}).get("purchase_currency") or "USD").upper().strip()
            or "USD"
        )
        tier_currencies = [
            str(t.get("currency") or "USD").upper().strip() or "USD"
            for t in (self._price_tiers or [])
            if isinstance(t, dict) and t.get("enabled", True)
        ]
        pivot = fx_rates.resolve_pivot_currency([purchase_ccy, *tier_currencies])
        cache[pid] = pivot
        return pivot

    def _build_orders_unit_cost_map(self) -> None:
        from datetime import datetime as _dt

        _now = _dt.utcnow()
        accum_qty: dict[str, float] = {}
        accum_total_cost_pivot: dict[str, float] = {}
        pivot_ccy_by_product: dict[str, str] = {}
        _pivot_cache: dict = {}
        active_product_ids: set = set()

        for order in self._orders:
            if not isinstance(order, dict):
                continue
            status_norm = str(order.get("status") or "").strip().casefold()
            # Рахуємо С/В тільки для замовлень, що ще ні прийняті, ні на виробництві.
            if status_norm in {
                "accepted", "прийнятий", "прийнято",
                "production", "виробництво", "вироб-во",
            }:
                continue
            _od = str(order.get("order_date") or "")[:10]
            try:
                _d = _dt.fromisoformat(_od)
                if _d.year < _now.year:
                    continue
            except Exception:
                pass
            items = order.get("items") if isinstance(order.get("items"), list) else []
            if not items:
                items = [
                    {
                        "product_id": str(order.get("product_id") or ""),
                        "qty": order.get("qty") or order.get("quantity") or 0,
                        "purchase_price": order.get("purchase_price"),
                        "purchase_currency": order.get("purchase_currency") or "USD",
                    }
                ]

            weights: list[float] = []
            for item in items:
                product_id = str((item or {}).get("product_id") or "").strip()
                product = self._product_by_id.get(product_id) or {}
                weights.append(self._order_item_basis_weight(item, product))

            total_basis = sum(weights) or 0.0
            tariff = _safe_float(order.get("delivery_price_snapshot"), None)
            tariff_ccy = (
                str(order.get("delivery_price_currency_snapshot") or "USD")
                .upper()
                .strip()
                or "USD"
            )
            fact_total = _safe_float(order.get("cp_logistics_fact_total"), None)
            fact_ccy = (
                str(order.get("cp_logistics_fact_currency") or "USD").upper().strip()
                or "USD"
            )

            for idx, item in enumerate(items):
                product_id = str((item or {}).get("product_id") or "").strip()
                if not product_id:
                    continue
                active_product_ids.add(product_id)
                product = self._product_by_id.get(product_id) or {}
                qty = (
                    _safe_float(
                        (item or {}).get("qty") or (item or {}).get("quantity"), 0.0
                    )
                    or 0.0
                )
                if qty <= 0:
                    continue

                purchase_price = _safe_float((item or {}).get("purchase_price"), None)
                purchase_ccy = (
                    str(
                        (item or {}).get("purchase_currency")
                        or product.get("purchase_currency")
                        or "USD"
                    )
                    .upper()
                    .strip()
                    or "USD"
                )
                if purchase_price is None and isinstance(product, dict):
                    purchase_price = _safe_float(product.get("purchase_price"), None)
                if purchase_price is None:
                    continue

                pivot_ccy = self._cost_pivot_currency_for_product(product, _pivot_cache)
                pivot_ccy_by_product[product_id] = pivot_ccy

                pack_price, pack_ccy = self._packaging_price_for_product(product)
                pack_in_purchase = self._convert_currency(
                    pack_price, pack_ccy, purchase_ccy
                )
                if pack_in_purchase is None:
                    pack_in_purchase = 0.0

                unit_full_price = float(purchase_price) + float(pack_in_purchase)
                full_amount = float(unit_full_price) * float(qty)
                full_amount_pivot = self._convert_currency(
                    full_amount, purchase_ccy, pivot_ccy
                )
                if full_amount_pivot is None:
                    continue

                basis_weight = weights[idx] if idx < len(weights) else 0.0
                calc_log_total = None
                if tariff is not None:
                    calc_log_total = float(basis_weight) * float(tariff)
                calc_log_pivot = self._convert_currency(calc_log_total, tariff_ccy, pivot_ccy)

                fact_share = None
                if fact_total is not None and total_basis > 0:
                    fact_share = float(fact_total) * (
                        float(basis_weight) / float(total_basis)
                    )
                fact_log_pivot = self._convert_currency(fact_share, fact_ccy, pivot_ccy)
                logistics_for_cv_pivot = (
                    fact_log_pivot if fact_log_pivot is not None else calc_log_pivot
                ) or 0.0

                total_cost_pivot = float(full_amount_pivot) + float(logistics_for_cv_pivot)
                accum_qty[product_id] = accum_qty.get(product_id, 0.0) + float(qty)
                accum_total_cost_pivot[product_id] = accum_total_cost_pivot.get(
                    product_id, 0.0
                ) + float(total_cost_pivot)

        out = {}
        out_ccy = {}
        for product_id, total_qty in accum_qty.items():
            if total_qty <= 0:
                continue
            out[product_id] = float(accum_total_cost_pivot.get(product_id, 0.0)) / float(
                total_qty
            )
            out_ccy[product_id] = pivot_ccy_by_product.get(product_id, "USD")
        self._orders_unit_cost_usd_by_product = out
        self._orders_unit_cost_ccy_by_product = out_ccy
        self._active_order_product_ids = active_product_ids

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

    def _load(self):
        self._region_id = self._resolve_region_id()
        self._load_workspace_state()
        self._load_workspace_outbox()
        self._load_ops_outbox()

        self._products = (
            self.db.get_products(
                self.company_id,
                region_id=self._region_id,
                manager_id=self.manager_id,
                force_refresh=True,
            )
            or []
        )
        self._set_loading_text(f"Товари: {len(self._products)}")
        self._archived_products = (
            self.db.get_products(
                self.company_id,
                region_id=self._region_id,
                manager_id=self.manager_id,
                force_refresh=True,
                archived=True,
            )
            or []
        )
        self._suppliers = (
            self.db.get_suppliers(
                self.company_id,
                self._region_id,
                manager_id=self.manager_id,
                force_refresh=True,
            )
            or []
        )
        self._set_loading_text(f"Постачальники: {len(self._suppliers)}")
        self._packaging = (
            self.db.get_packaging(
                self.company_id,
                self._region_id,
                manager_id=self.manager_id,
                force_refresh=True,
            )
            or []
        )
        self._set_loading_text(f"Пакування: {len(self._packaging)}")
        self._categories = (
            self.db.get_company_categories(
                self.company_id,
                self._region_id,
                manager_id=self.manager_id,
            )
            or []
        )
        self._set_loading_text(f"Категорії: {len(self._categories)}")
        try:
            self._ref_data_pulse_ts = self.db.get_reference_data_pulse(self.company_id)
        except Exception:
            pass
        self._price_tiers = (
            self.db.get_effective_region_price_tiers_for_manager(
                self.company_id,
                self.manager_id,
                self._region_id,
            )
            if self._region_id
            else []
        )

        self._set_loading_text("Замовлення...")
        self._orders = (
            self.db.get_orders(
                self.company_id,
                region_id=self._region_id,
                manager_id=self.manager_id,
                force_refresh=True,
            )
            or []
        )
        self._set_loading_text(f"Замовлення: {len(self._orders)}")
        self._stock_inputs_by_product = (
            self.db.get_cost_price_stock_inputs(
                self.company_id,
                region_id=self._region_id,
                manager_id=self.manager_id,
            )
            or {}
        )
        self._apply_ops_outbox_to_local()

        self._product_by_id = {
            str((product or {}).get("id") or "").strip(): product
            for product in self._products
            if isinstance(product, dict) and (product.get("id") or "")
        }
        self._packaging_by_id = {
            str((packaging or {}).get("id") or "").strip(): packaging
            for packaging in self._packaging
            if isinstance(packaging, dict) and (packaging.get("id") or "")
        }

        self._build_orders_unit_cost_map()

        self._build_warehouse_rows()
        self._build_warehouse_rows(self._archived_products, "_archived_warehouse_rows")

    def _warehouse_key(self, product_id: str) -> str:
        return str(product_id or "").strip()

    def _build_warehouse_rows(self, products: list | None = None, store_attr: str = "_warehouse_rows"):
        _products = self._products if products is None else products
        defaults = self._column_defaults()
        stock_ccy = str(defaults.get("stock_ccy") or "USD").upper().strip() or "USD"
        orders_unit_cost_ccy = (
            str(defaults.get("orders_unit_cost_ccy") or "USD").upper().strip() or "USD"
        )
        default_new_ccy = (
            str(defaults.get("stock_new_price_ccy") or "USD").upper().strip() or "USD"
        )
        warehouse_state = (
            self._stock_inputs_by_product
            if isinstance(self._stock_inputs_by_product, dict)
            else {}
        )
        # Disambiguation suffix (" | Кат: ... | Постач: ...") for products that
        # share a name — see views/product_naming.py. Category is resolved via
        # the catalog-tree root (not the flat category_id, which some products
        # never get written to — it used to make them look identical).
        _category_by_id = {
            str((c or {}).get("id") or "").strip(): c
            for c in (self._categories or [])
            if isinstance(c, dict) and (c.get("id") or "")
        }
        _supplier_by_id = {
            str((s or {}).get("id") or "").strip(): s
            for s in (self._suppliers or [])
            if isinstance(s, dict) and (s.get("id") or "")
        }
        if not hasattr(self, "_node_path_cache"):
            self._node_path_cache = {}
        _node_path_of = product_naming.make_node_path_resolver(
            self.db, self._node_path_cache
        )
        _name_suffixes = product_naming.compute_name_suffixes(
            _products,
            category_of=lambda p: product_naming.resolve_category_label(
                p, node_path_of=_node_path_of, category_by_id=_category_by_id
            ),
            supplier_of=lambda p: product_naming.resolve_supplier_label(
                p, supplier_by_id=_supplier_by_id
            ),
        )
        rows = []
        for product in _products:
            if not isinstance(product, dict):
                continue
            product_id = str(product.get("id") or "").strip()
            if not product_id:
                continue
            key = self._warehouse_key(product_id)
            item_state = (
                warehouse_state.get(key)
                if isinstance(warehouse_state.get(key), dict)
                else {}
            )

            qty = _safe_float(item_state.get("qty"), None)
            amount = _safe_float(item_state.get("amount"), None)
            ccy = str(item_state.get("ccy") or stock_ccy).upper().strip() or stock_ccy
            amount_stock_ccy = self._convert_currency(amount, ccy, stock_ccy)
            if amount_stock_ccy is None:
                amount_stock_ccy = amount

            unit_cost_stock = None
            if qty is not None and qty > 0 and amount_stock_ccy is not None:
                unit_cost_stock = float(amount_stock_ccy) / float(qty)

            order_unit_cost_usd = _safe_float(
                self._orders_unit_cost_usd_by_product.get(product_id), None
            )
            order_unit_cost_source_ccy = self._orders_unit_cost_ccy_by_product.get(
                product_id, "USD"
            )
            orders_unit_cost = self._convert_currency(
                order_unit_cost_usd, order_unit_cost_source_ccy, orders_unit_cost_ccy
            )
            order_unit_cost_stock = self._convert_currency(
                orders_unit_cost, orders_unit_cost_ccy, stock_ccy
            )

            cost_candidates = [
                c
                for c in [unit_cost_stock, order_unit_cost_stock]
                if c is not None and c >= 0
            ]
            unit_cost_blended = None
            if cost_candidates:
                unit_cost_blended = sum(cost_candidates) / float(len(cost_candidates))

            tier_pairs = []
            for tier in self._price_tiers:
                sale_price, sale_ccy = self._sales_price_for_tier(product, tier)
                margin = None
                if (
                    sale_price is not None
                    and sale_price > 0
                    and unit_cost_blended is not None
                ):
                    unit_cost_sale = self._convert_currency(
                        unit_cost_blended, stock_ccy, sale_ccy
                    )
                    if unit_cost_sale is not None:
                        margin = (float(sale_price) - float(unit_cost_sale)) / float(
                            sale_price
                        )
                tier_pairs.append(
                    {
                        "tier": tier,
                        "sale_price": sale_price,
                        "sale_ccy": sale_ccy,
                        "margin": margin,
                    }
                )

            new_price = _safe_float(item_state.get("new_price"), None)
            new_ccy = (
                str(
                    item_state.get("stock_new_ccy")
                    or item_state.get("new_ccy")
                    or default_new_ccy
                )
                .upper()
                .strip()
                or default_new_ccy
            )
            new_margin = None
            if (
                new_price is not None
                and new_price > 0
                and unit_cost_blended is not None
            ):
                unit_cost_new = self._convert_currency(
                    unit_cost_blended, stock_ccy, new_ccy
                )
                if unit_cost_new is not None:
                    new_margin = (float(new_price) - float(unit_cost_new)) / float(
                        new_price
                    )

            _pname = str(product.get("name") or "-")
            _pname = _pname + _name_suffixes.get(product_id, "")
            rows.append(
                {
                    "product": product,
                    "product_id": product_id,
                    "name": _pname,
                    "supplier_id": str(product.get("supplier_id") or "").strip(),
                    "category_id": str(product.get("category_id") or "").strip(),
                    "qty": qty,
                    "amount": amount,
                    "ccy": ccy,
                    "amount_stock_ccy": amount_stock_ccy,
                    "stock_ccy": stock_ccy,
                    "unit_cost_stock": unit_cost_stock,
                    "orders_unit_cost": orders_unit_cost,
                    "orders_unit_cost_ccy": orders_unit_cost_ccy,
                    "unit_cost_blended": unit_cost_blended,
                    "tier_pairs": tier_pairs,
                    "new_price": new_price,
                    "new_ccy": new_ccy,
                    "new_margin": new_margin,
                    "has_active_orders": product_id in self._active_order_product_ids,
                }
            )

        setattr(self, store_attr, rows)

    def archive_stock_row(self, product_id: str) -> None:
        """Move a product to the archive — its already-computed warehouse row
        is moved between the in-memory lists directly (no full _load() refetch)."""
        if self.is_read_only:
            return
        pid = self._warehouse_key(product_id)
        if not pid:
            return
        if not self.db.archive_product(pid, manager_id=self.manager_id):
            self._snack("Не вдалося архівувати товар", _ERR)
            return
        row = next(
            (r for r in self._warehouse_rows if r.get("product_id") == pid), None
        )
        self._warehouse_rows = [
            r for r in self._warehouse_rows if r.get("product_id") != pid
        ]
        product = self._product_by_id.pop(pid, None)
        self._products = [
            p for p in self._products if str((p or {}).get("id") or "") != pid
        ]
        if product is not None:
            product = dict(product)
            product["is_archived"] = True
            if row is not None:
                row = dict(row)
                row["product"] = product
            self._archived_products = [product] + list(self._archived_products or [])
        if row is not None:
            self._archived_warehouse_rows = [row] + list(
                self._archived_warehouse_rows or []
            )
        self._snack("Товар перенесено в архів", _ACCENT)
        self._render()

    def restore_stock_row(self, product_id: str) -> None:
        """Restore a product from the archive back into the active warehouse list."""
        if self.is_read_only:
            return
        pid = self._warehouse_key(product_id)
        if not pid:
            return
        if not self.db.restore_product(pid):
            self._snack("Не вдалося відновити товар", _ERR)
            return
        row = next(
            (r for r in self._archived_warehouse_rows if r.get("product_id") == pid),
            None,
        )
        self._archived_warehouse_rows = [
            r for r in self._archived_warehouse_rows if r.get("product_id") != pid
        ]
        product = next(
            (p for p in self._archived_products if str((p or {}).get("id") or "") == pid),
            None,
        )
        self._archived_products = [
            p for p in self._archived_products if str((p or {}).get("id") or "") != pid
        ]
        if product is not None:
            product = dict(product)
            product["is_archived"] = False
            self._product_by_id[pid] = product
            self._products = list(self._products or []) + [product]
            if row is not None:
                row = dict(row)
                row["product"] = product
        if row is not None:
            self._warehouse_rows = list(self._warehouse_rows or []) + [row]
        self._snack("Товар відновлено з архіву", _OK)
        self._render()

    def _build_stock_archive_section(self) -> ft.Control:
        """Згорнута секція архівованих товарів складу з можливістю відновлення —
        той самий візуальний паттерн, що й в інших вкладках (Товари, тощо)."""
        archived_rows = self._archived_warehouse_rows or []
        collapsed = getattr(self, "_stock_archive_collapsed", True)

        def _toggle(_e):
            self._stock_archive_collapsed = not getattr(
                self, "_stock_archive_collapsed", True
            )
            self._render()

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
                        self.t("stock.archive_section", "Архів") + f" ({len(archived_rows)})",
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
        controls: list[ft.Control] = [header]
        if not collapsed:
            if not archived_rows:
                controls.append(
                    ft.Container(
                        padding=ft.padding.only(left=28, bottom=6),
                        content=ft.Text(
                            self.t("stock.archive_empty", "Архів порожній"),
                            color="grey600",
                            size=12,
                        ),
                    )
                )
            else:
                defaults = self._column_defaults()
                stock_ccy = str(defaults.get("stock_ccy") or "USD").upper().strip() or "USD"
                orders_unit_cost_ccy = (
                    str(defaults.get("orders_unit_cost_ccy") or "USD").upper().strip()
                    or "USD"
                )
                stock_new_price_ccy = (
                    str(defaults.get("stock_new_price_ccy") or "USD").upper().strip()
                    or "USD"
                )
                archive_table = build_stock_table(
                    self,
                    archived_rows,
                    stock_ccy,
                    orders_unit_cost_ccy,
                    stock_new_price_ccy,
                    include_header_row=True,
                )
                controls.append(self._wrap_archive_table_simple(archive_table))
        return ft.Container(
            content=ft.Column(controls, spacing=2),
            margin=ft.margin.only(top=10),
            border=ft.border.only(top=ft.BorderSide(1, "grey300")),
        )

    def _wrap_archive_table_simple(self, table: dict) -> ft.Control:
        """Lightweight horizontal-scroll wrapper for the archive mini-table —
        deliberately independent of _wrap_stock_table_with_scroll's h-scroll-sync
        state (self._cv_body_scroll/_h_scroll_*), which belongs to the main table."""
        viewport_width = max(320, int(self._get_viewport_width()))
        frozen_part = table.get("frozen")
        scroll_part = table.get("scrollable")
        frozen_width = int(table.get("frozen_width") or 0)
        scroll_width = int(table.get("scroll_width") or 0)
        fixed_width = min(max(0, frozen_width), max(0, viewport_width - 120))
        scroll_host_width = max(120, viewport_width - fixed_width)
        return ft.Row(
            [
                ft.Container(
                    width=fixed_width,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    bgcolor=_PANEL,
                    content=ft.Container(width=frozen_width, content=frozen_part),
                ),
                ft.Container(
                    width=scroll_host_width,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    content=ft.Row(
                        [ft.Container(width=scroll_width, content=scroll_part)],
                        scroll=ft.ScrollMode.AUTO,
                    ),
                ),
            ],
            spacing=0,
        )

    def _save_warehouse_row(self, product_id: str, patch: dict):
        if self.is_read_only:
            return
        product_id = self._warehouse_key(product_id)
        if not product_id:
            return
        row_state = self._stock_inputs_by_product.get(product_id)
        if not isinstance(row_state, dict):
            row_state = {}
        patch_local = dict(patch or {})
        if not str(patch_local.get("ccy") or "").strip():
            patch_local["ccy"] = (
                str(
                    row_state.get("ccy")
                    or self._column_defaults().get("stock_ccy")
                    or "USD"
                )
                .upper()
                .strip()
                or "USD"
            )
        if not str(patch_local.get("new_ccy") or "").strip():
            patch_local["new_ccy"] = (
                str(
                    row_state.get("new_ccy")
                    or row_state.get("stock_new_ccy")
                    or self._column_defaults().get("stock_new_price_ccy")
                    or "USD"
                )
                .upper()
                .strip()
                or "USD"
            )
        changed = any(row_state.get(key) != value for key, value in patch_local.items())
        if not changed:
            return
        row_state.update(patch_local)
        self._stock_inputs_by_product[product_id] = row_state

        self._enqueue_stock_op(product_id, set(patch_local.keys()))
        if "new_price" in patch_local or "new_ccy" in patch_local:
            self._update_new_margin_inplace(product_id)
        if "qty" in patch_local or "amount" in patch_local or "ccy" in patch_local:
            # Перераховуємо тільки один продукт замість _build_warehouse_rows() (всіх).
            self._recalc_one_warehouse_row(product_id)
            self._update_stock_computed_inplace(product_id)
            self._update_tier_cells_inplace(product_id)
            self._update_new_margin_inplace(product_id)
            self._update_stock_totals_inplace()

    def _update_stock_totals_inplace(self) -> None:
        """Recompute the two summary stat tiles (Загальна кількість / Загальна
        сума) from the currently filtered rows and patch their Text controls
        directly — keeps them live on every qty/amount edit, no full _render()."""
        refs = getattr(self, "_stock_totals_refs", None)
        if not isinstance(refs, dict):
            return
        rows = self._stock_filtered()
        total_qty = sum((_safe_float(r.get("qty"), None) or 0) for r in rows)
        total_amount = sum(
            (_safe_float(r.get("amount_stock_ccy"), None) or 0) for r in rows
        )
        stock_ccy = str((rows[0].get("stock_ccy") if rows else None) or "USD").upper()
        qty_text = refs.get("qty_text")
        sum_text = refs.get("sum_text")
        if qty_text is not None:
            qty_text.value = self._fmt_money(total_qty)
            with contextlib.suppress(Exception):
                qty_text.update()
        if sum_text is not None:
            sum_text.value = self._fmt_money(total_amount, stock_ccy)
            with contextlib.suppress(Exception):
                sum_text.update()

    def _update_stock_computed_inplace(self, product_id: str) -> None:
        """Оновлює unit_cost_stock, unit_cost_blended та qty-cell-color in-place."""
        pid = str(product_id or "").strip()
        refs = getattr(self, "_stock_computed_refs", {}).get(pid)
        if not refs:
            return
        row = next(
            (
                r
                for r in (self._warehouse_rows or []) + (self._archived_warehouse_rows or [])
                if str(r.get("product_id") or "") == pid
            ),
            None,
        )
        if row is None:
            return
        defaults = self._column_defaults()
        stock_ccy = str(defaults.get("stock_ccy") or "USD").upper().strip() or "USD"

        for ref_key, row_key in (
            ("unit_cost_stock", "unit_cost_stock"),
            ("unit_cost_blended", "unit_cost_blended"),
        ):
            txt = refs.get(ref_key)
            if txt is None:
                continue
            txt.value = self._fmt_money(_safe_float(row.get(row_key), None), stock_ccy)
            with contextlib.suppress(Exception):
                txt.update()

        # Оновлюємо колір клітинки qty (червоний якщо порожньо)
        row_bg = refs.get("row_bg", _PANEL)
        qty_cell = refs.get("qty_cell")
        qty_tf = refs.get("qty_tf")
        qty_empty = _safe_float(row.get("qty"), None) is None
        qty_bg = _ERR_BG if qty_empty else row_bg
        if qty_cell is not None:
            qty_cell.bgcolor = qty_bg
            with contextlib.suppress(Exception):
                qty_cell.update()
        if qty_tf is not None:
            qty_tf.fill_color = qty_bg
            with contextlib.suppress(Exception):
                qty_tf.update()

        # Оновлюємо колір клітинки amount (червоний якщо порожньо)
        amount_cell = refs.get("amount_cell")
        amount_tf = refs.get("amount_tf")
        amt_empty = _safe_float(row.get("amount"), None) is None
        amt_bg = _ERR_BG if amt_empty else row_bg
        if amount_cell is not None:
            amount_cell.bgcolor = amt_bg
            with contextlib.suppress(Exception):
                amount_cell.update()
        if amount_tf is not None:
            amount_tf.fill_color = amt_bg
            with contextlib.suppress(Exception):
                amount_tf.update()

        # Оновлюємо orders_unit_cost (С/В Замовлення) — потрібно при cross-tab sync
        ouc_txt = refs.get("orders_unit_cost_txt")
        if ouc_txt is not None:
            ouc_ccy = str(refs.get("orders_unit_cost_ccy") or "USD")
            ouc_txt.value = self._fmt_money(
                _safe_float(row.get("orders_unit_cost"), None), ouc_ccy
            )
            with contextlib.suppress(Exception):
                ouc_txt.update()

    def _update_new_margin_inplace(self, product_id: str) -> None:
        refs = getattr(self, "_new_price_margin_refs", {}).get(
            str(product_id or "").strip()
        )
        if not refs:
            return
        row_state = (
            self._stock_inputs_by_product.get(self._warehouse_key(product_id)) or {}
        )
        new_price = _safe_float(row_state.get("new_price"), None)
        new_ccy = str(row_state.get("new_ccy") or refs.get("new_ccy") or "USD").upper()
        unit_cost_blended = refs.get("unit_cost_blended")
        stock_ccy = str(refs.get("stock_ccy") or "USD").upper()
        category_id = str(refs.get("category_id") or "")
        target = self._target_margin(category_id)
        new_margin = None
        if new_price is not None and new_price > 0 and unit_cost_blended is not None:
            unit_cost_new = self._convert_currency(
                unit_cost_blended, stock_ccy, new_ccy
            )
            if unit_cost_new is not None:
                new_margin = (float(new_price) - float(unit_cost_new)) / float(
                    new_price
                )
        m_bg, m_fg = self._margin_colors(new_margin, target)
        margin_text = "-" if new_margin is None else f"{float(new_margin) * 100:.1f}%"
        nm_half = refs.get("nm_half")
        outer = refs.get("outer")
        if nm_half is not None:
            nm_half.bgcolor = m_bg
            if nm_half.content is not None:
                nm_half.content.value = margin_text
                nm_half.content.color = m_fg
        if outer is not None:
            outer.bgcolor = m_bg
        pg = self._pg()
        if pg is not None:
            import contextlib

            with contextlib.suppress(Exception):
                if nm_half is not None:
                    nm_half.update()
            with contextlib.suppress(Exception):
                if outer is not None:
                    outer.update()

    def _recalc_one_warehouse_row(self, product_id: str) -> None:
        """Перераховує row-дані тільки для одного продукту і оновлює _warehouse_rows."""
        pid = self._warehouse_key(product_id)
        if not pid:
            return
        product = next(
            (
                p
                for p in (self._products or []) + (self._archived_products or [])
                if isinstance(p, dict) and str(p.get("id") or "").strip() == pid
            ),
            None,
        )
        if product is None:
            return
        defaults = self._column_defaults()
        stock_ccy = str(defaults.get("stock_ccy") or "USD").upper().strip() or "USD"
        orders_unit_cost_ccy = (
            str(defaults.get("orders_unit_cost_ccy") or "USD").upper().strip() or "USD"
        )
        default_new_ccy = (
            str(defaults.get("stock_new_price_ccy") or "USD").upper().strip() or "USD"
        )
        item_state = self._stock_inputs_by_product.get(pid) or {}

        qty = _safe_float(item_state.get("qty"), None)
        amount = _safe_float(item_state.get("amount"), None)
        ccy = str(item_state.get("ccy") or stock_ccy).upper().strip() or stock_ccy
        amount_stock_ccy = self._convert_currency(amount, ccy, stock_ccy)
        if amount_stock_ccy is None:
            amount_stock_ccy = amount

        unit_cost_stock = None
        if qty is not None and qty > 0 and amount_stock_ccy is not None:
            unit_cost_stock = float(amount_stock_ccy) / float(qty)

        order_unit_cost_usd = _safe_float(
            self._orders_unit_cost_usd_by_product.get(pid), None
        )
        order_unit_cost_source_ccy = self._orders_unit_cost_ccy_by_product.get(
            pid, "USD"
        )
        orders_unit_cost = self._convert_currency(
            order_unit_cost_usd, order_unit_cost_source_ccy, orders_unit_cost_ccy
        )
        order_unit_cost_stock = self._convert_currency(
            orders_unit_cost, orders_unit_cost_ccy, stock_ccy
        )

        cost_candidates = [
            c
            for c in [unit_cost_stock, order_unit_cost_stock]
            if c is not None and c >= 0
        ]
        unit_cost_blended = (
            sum(cost_candidates) / float(len(cost_candidates))
            if cost_candidates
            else None
        )

        tier_pairs = []
        for tier in self._price_tiers:
            sale_price, sale_ccy = self._sales_price_for_tier(product, tier)
            margin = None
            if (
                sale_price is not None
                and sale_price > 0
                and unit_cost_blended is not None
            ):
                unit_cost_sale = self._convert_currency(
                    unit_cost_blended, stock_ccy, sale_ccy
                )
                if unit_cost_sale is not None:
                    margin = (float(sale_price) - float(unit_cost_sale)) / float(
                        sale_price
                    )
            tier_pairs.append(
                {
                    "tier": tier,
                    "sale_price": sale_price,
                    "sale_ccy": sale_ccy,
                    "margin": margin,
                }
            )

        new_price = _safe_float(item_state.get("new_price"), None)
        new_ccy = (
            str(
                item_state.get("stock_new_ccy")
                or item_state.get("new_ccy")
                or default_new_ccy
            )
            .upper()
            .strip()
            or default_new_ccy
        )
        new_margin = None
        if new_price is not None and new_price > 0 and unit_cost_blended is not None:
            unit_cost_new = self._convert_currency(
                unit_cost_blended, stock_ccy, new_ccy
            )
            if unit_cost_new is not None:
                new_margin = (float(new_price) - float(unit_cost_new)) / float(
                    new_price
                )

        # Оновлюємо рядок у _warehouse_rows або _archived_warehouse_rows —
        # залежно від того, де зараз цей товар (активний чи в архіві).
        _patch = {
            "qty": qty,
            "amount": amount,
            "ccy": ccy,
            "amount_stock_ccy": amount_stock_ccy,
            "stock_ccy": stock_ccy,
            "unit_cost_stock": unit_cost_stock,
            "orders_unit_cost": orders_unit_cost,
            "orders_unit_cost_ccy": orders_unit_cost_ccy,
            "unit_cost_blended": unit_cost_blended,
            "tier_pairs": tier_pairs,
            "new_price": new_price,
            "new_ccy": new_ccy,
            "new_margin": new_margin,
        }
        for rows_list in (self._warehouse_rows, self._archived_warehouse_rows):
            for i, row in enumerate(rows_list):
                if str(row.get("product_id") or "") == pid:
                    rows_list[i].update(_patch)
                    # Оновлюємо unit_cost_blended у refs для _update_new_margin_inplace
                    _margin_refs = getattr(self, "_new_price_margin_refs", {}).get(pid)
                    if isinstance(_margin_refs, dict):
                        _margin_refs["unit_cost_blended"] = unit_cost_blended
                    return

    def _update_tier_cells_inplace(self, product_id: str) -> None:
        """Оновлює клітинки цінових рівнів для одного продукту in-place."""
        pid = self._warehouse_key(product_id)
        if not pid:
            return
        refs = getattr(self, "_stock_computed_refs", {}).get(pid)
        if not refs or "tier_cells" not in refs:
            return
        row = next(
            (
                r
                for r in (self._warehouse_rows or []) + (self._archived_warehouse_rows or [])
                if str(r.get("product_id") or "") == pid
            ),
            None,
        )
        if row is None:
            return
        tier_cells = refs.get("tier_cells") or []
        target = self._target_margin(
            str(
                (row.get("product") or {}).get("category_id")
                or row.get("category_id")
                or ""
            )
        )
        pairs = row.get("tier_pairs") or []
        cells_to_update = []
        for i, (tier_c, sale_txt, margin_txt) in enumerate(tier_cells):
            pair = pairs[i] if i < len(pairs) else {}
            sale = _safe_float(pair.get("sale_price"), None)
            margin = pair.get("margin")
            bg_m, fg_m = self._margin_colors(margin, target)
            sale_txt.value = self._fmt_money(sale, pair.get("sale_ccy") or "USD")
            margin_txt.value = "-" if margin is None else f"{margin * 100:.1f}%"
            margin_txt.color = fg_m
            tier_c.bgcolor = bg_m
            cells_to_update.append(tier_c)
        pg = self._pg()
        if pg and cells_to_update:
            import contextlib

            with contextlib.suppress(Exception):
                pg.update(*cells_to_update)

    def _schedule_render(self, delay_sec: float = 0.0) -> None:
        self._render_seq = getattr(self, "_render_seq", 0) + 1
        my_seq = self._render_seq
        self._render_pending = True
        pg = self._pg()
        if pg and hasattr(pg, "run_task"):

            async def _do_render(s=my_seq, d=delay_sec):
                try:
                    if d and d > 0:
                        await asyncio.sleep(float(d))
                    if self._render_seq != s:
                        return
                    self._render()
                finally:
                    self._render_pending = False

            try:
                pg.run_task(_do_render)
                return
            except Exception:
                self._render_pending = False
        try:
            self._render()
        finally:
            self._render_pending = False

    def _set_stock_new_price_currency(self, value: str):
        selected = str(value or "USD").upper().strip() or "USD"
        bucket = self._state_bucket()
        defaults = self._column_defaults()
        if defaults.get("stock_new_price_ccy") == selected:
            return
        defaults["stock_new_price_ccy"] = selected
        bucket["column_defaults"] = defaults
        self._save_workspace_state()
        self._build_warehouse_rows()
        self._persist_and_render(self.t("cost_stock.currency_updated", "Валюту складу оновлено"))

    def _set_stock_cost_currency(self, value: str):
        selected = str(value or "USD").upper().strip() or "USD"
        bucket = self._state_bucket()
        defaults = self._column_defaults()
        current = str(defaults.get("stock_ccy") or "USD").upper().strip() or "USD"
        if current == selected:
            return

        defaults["stock_ccy"] = selected
        bucket["column_defaults"] = defaults
        self._save_workspace_state()
        self._build_warehouse_rows()
        self._persist_and_render(self.t("cost_stock.currency_updated", "Валюту складу оновлено"))

    def _set_orders_unit_cost_currency(self, value: str):
        selected = str(value or "USD").upper().strip() or "USD"
        bucket = self._state_bucket()
        defaults = self._column_defaults()
        if defaults.get("orders_unit_cost_ccy") == selected:
            return
        defaults["orders_unit_cost_ccy"] = selected
        bucket["column_defaults"] = defaults
        self._save_workspace_state()
        self._build_warehouse_rows()
        self._persist_and_render(self.t("cost_stock.order_cost_currency_updated", "Валюту С/В замовлень оновлено"))

    def apply_text_search(self, query: str) -> None:
        """Ctrl+F row filter: hide rows whose Товар/К-сть/Вартість/ціни don't match `query`."""
        set_stock_query(self, query)

    def clear_text_search(self) -> None:
        self.apply_text_search("")

    def _stock_filtered(self) -> list:
        out = []
        query = self._stock_filter_query.strip().lower()
        pf = self._stock_presence_filter
        for row in self._warehouse_rows:
            if (
                self._stock_filter_supplier != "all"
                and row.get("supplier_id") != self._stock_filter_supplier
            ):
                continue
            if (
                self._stock_filter_category != "all"
                and row.get("category_id") != self._stock_filter_category
            ):
                continue
            if query:
                visible_tiers = self._visible_stock_tier_ids()
                tier_prices = [
                    tp.get("sale_price")
                    for tp in (row.get("tier_pairs") or [])
                    if str((tp.get("tier") or {}).get("id") or "") in visible_tiers
                ]
                if not row_text_matches(
                    query,
                    row.get("name"),
                    row.get("qty"),
                    row.get("amount"),
                    row.get("unit_cost_stock"),
                    row.get("unit_cost_blended"),
                    row.get("new_price"),
                    *tier_prices,
                ):
                    continue
            if pf != "all":
                has_qty = (_safe_float(row.get("qty"), None) or 0) > 0
                has_cost = (_safe_float(row.get("amount"), None) or 0) > 0
                if pf == "with_qty" and not has_qty:
                    continue
                if pf == "without_qty" and has_qty:
                    continue
                if pf == "with_cost" and not has_cost:
                    continue
                if pf == "without_cost" and has_cost:
                    continue
                if pf == "without_both" and (has_qty or has_cost):
                    continue
            out.append(row)
        return out

    def _target_margin(self, category_id: str) -> Optional[float]:
        return self.db.get_category_target_margin(category_id)

    def _margin_colors(
        self, margin: Optional[float], target_margin_pct: Optional[float]
    ) -> tuple[str, str]:
        if margin is None:
            return (_ALT2, "#64748B")
        margin_pct = float(margin) * 100.0
        pivot = float(target_margin_pct or 0.0)
        if margin_pct >= pivot:
            return (_OK_BG, "#166534")
        if margin_pct >= pivot - 5.0:
            return ("#FEF3C7", "#92400E")
        return (_ERR_BG, _ERR)

    def _kpi_chip(self, label: str, value: Any, bg: str, fg: str) -> ft.Container:
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
                            str(value), size=9, color=_PANEL, weight="bold"
                        ),
                    ),
                ],
                spacing=4,
                tight=True,
            ),
        )

    def _get_viewport_width(self, page=None) -> int:
        pg = page or (self.content_area.page if self.content_area else None)
        if not pg:
            return 1200
        host_w = (
            int(getattr(self.content_area, "width", 0) or 0) if self.content_area else 0
        )
        if host_w > 0:
            return max(320, int(host_w))
        page_w = int(getattr(pg, "width", 0) or 0)
        if page_w <= 0:
            win = getattr(pg, "window", None)
            page_w = int(getattr(win, "width", 0) or 0) if win else 0
        return max(320, page_w if page_w > 0 else 1200)

    def _attach_resize_handler(self) -> None:
        page = self._pg()
        if not page:
            return

        def _on_resized(_e=None):
            try:
                active_view = str(getattr(page, "_active_view", "") or "").strip()
            except Exception:
                active_view = ""
            if active_view != "show_cost_price_stock":
                return

            try:
                new_w = int(self._get_viewport_width(page))
            except Exception:
                new_w = 0

            holder = self._resize_refresh_state
            if abs(int(holder.get("last_w") or 0) - int(new_w or 0)) < 4:
                return
            holder["last_w"] = int(new_w or 0)
            if holder.get("pending"):
                return
            holder["pending"] = True

            async def _rerender_later():
                try:
                    await asyncio.sleep(0.05)
                    self._render()
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

    def _wrap_stock_table_with_scroll(self, table: dict) -> ft.Control:
        viewport_width = max(320, int(self._get_viewport_width()))
        frozen_part = table.get("frozen")
        scroll_part = table.get("scrollable")
        frozen_width = int(table.get("frozen_width") or 0)
        scroll_width = int(table.get("scroll_width") or 0)
        shell_width = max(0, scroll_width)
        fixed_width = (
            min(max(0, frozen_width), max(0, viewport_width - 120))
            if viewport_width > 240
            else frozen_width
        )
        scroll_view_width = max(120, viewport_width - fixed_width)
        scroll_host_width = (
            min(scroll_view_width, shell_width)
            if shell_width > 0
            else scroll_view_width
        )

        frozen_shell = ft.Container(
            width=fixed_width,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            bgcolor=_PANEL,
            border_radius=8,
            border=ft.border.all(1, _BORDER),
            content=ft.Container(
                width=frozen_width,
                alignment=ft.Alignment(-1, 0),
                content=frozen_part,
            ),
        )
        table_shell = ft.Container(
            width=shell_width,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            bgcolor=_PANEL,
            border_radius=8,
            border=ft.border.all(1, _BORDER),
            content=ft.Container(
                width=shell_width,
                alignment=ft.Alignment(-1, 0),
                content=scroll_part,
            ),
        )

        def _on_body_scroll(e):
            self._table_h_scroll_offset = max(
                0.0, float(getattr(e, "pixels", 0.0) or 0.0)
            )
            item = getattr(self, "_cv_header_stack_child", None)
            if item is not None:
                item.left = -self._table_h_scroll_offset
                with contextlib.suppress(Exception):
                    item.update()

        scroll_row = ft.Row(
            [table_shell],
            spacing=0,
            expand=True,
            scroll=ft.ScrollMode.HIDDEN,
            on_scroll=_on_body_scroll,
        )
        self._cv_body_scroll = scroll_row
        self._h_scroll_slot_content = None

        pg = self._pg()
        if pg and hasattr(pg, "run_task") and self._table_h_scroll_offset > 0:
            _restore_off = self._table_h_scroll_offset

            async def _restore_h_scroll():
                import asyncio as _aio

                await _aio.sleep(0)
                with contextlib.suppress(Exception):
                    await scroll_row.scroll_to(offset=_restore_off, duration=0)
                _sc = getattr(self, "_cv_header_stack_child", None)
                if _sc is not None:
                    _sc.left = -_restore_off
                    with contextlib.suppress(Exception):
                        _sc.update()

            try:
                pg.run_task(_restore_h_scroll)
            except Exception:
                pass

        # Clear page-level slot if another view left one behind.
        _scroll_slot = getattr(pg, "_view_h_scrollbar_slot", None) if pg else None
        if _scroll_slot is not None and _scroll_slot.controls:
            try:
                _scroll_slot.controls.clear()
                _scroll_slot.update()
            except Exception:
                pass

        # Store h-scroll params for _render() to create overlay buttons.
        self._h_scroll_needs = shell_width > scroll_host_width
        self._h_scroll_step = max(300, shell_width // 3)
        self._h_scroll_width = shell_width

        return ft.Container(
            width=viewport_width,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            frozen_shell,
                            ft.Container(
                                width=scroll_host_width, content=scroll_row, padding=0
                            ),
                        ],
                        spacing=0,
                    ),
                ],
                spacing=0,
                tight=True,
            ),
            padding=0,
        )

    def _build_stock_table_header_widget(
        self, table_content: dict, viewport_width: int, frozen_width: int, scroll_width: int
    ) -> ft.Control:
        """Builds a pinned header widget (frozen + scrollable with hidden h-scroll)
        from the header rows returned by build_stock_table(..., include_header_row=False)."""
        fixed_width = (
            min(max(0, frozen_width), max(0, viewport_width - 120))
            if viewport_width > 240
            else frozen_width
        )
        scroll_view_width = max(120, viewport_width - fixed_width)
        scroll_host_width = (
            min(scroll_view_width, scroll_width)
            if scroll_width > 0
            else scroll_view_width
        )

        frozen_header_shell = ft.Container(
            width=fixed_width,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            bgcolor=_PANEL,
            border_radius=8,
            border=ft.border.all(1, _BORDER),
            content=ft.Container(
                width=frozen_width,
                alignment=ft.Alignment(-1, 0),
                content=table_content.get("left_header_row"),
            ),
        )
        right_header_content = ft.Container(
            width=scroll_width,
            content=table_content.get("right_header_row"),
        )
        right_header_content.left = -int(self._table_h_scroll_offset or 0)
        right_header_content.top = 0
        self._cv_header_stack_child = right_header_content
        self._cv_header_scroll = None

        header_h = int(table_content.get("header_height") or 34)
        right_header_host = ft.Container(
            width=scroll_host_width,
            height=header_h,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.Stack([right_header_content]),
            padding=0,
        )
        return ft.Row(
            [frozen_header_shell, right_header_host],
            spacing=0,
        )

    def _persist_and_render(self, success_text: Optional[str] = "Збережено") -> None:
        if success_text:
            self._snack(success_text, "#0EA5E9")
        self._render()

    def _render(self):
        _th = _th_from_view(self)
        global _PANEL, _ERR, _HDR, _MUTED, _MUTED2, _ALT, _TEXT, _OK, _ERR_BG
        global _BORDER, _ACCENT, _ACCENT2, _CHIP_T, _WARN, _WARN_DK, _OK_BG
        _PANEL = _th["panel"]; _ERR = _th["err"]; _HDR = _th["hdr"]
        _MUTED = _MUTED2 = _th["muted"]; _ALT = _ALT2 = _th["alt"]
        _TEXT = _th["text"]; _OK = _th["ok"]; _ERR_BG = _th["err_bg"]
        _BORDER = _BORDER2 = _th["border"]; _ACCENT = _ACCENT2 = _th["icon_color"]
        _CHIP_T = _th["chip_text"]; _WARN = _WARN_DK = _th["warn"]; _OK_BG = _th["ok_bg"]
        self._stock_computed_refs = {}
        self._new_price_margin_refs = {}
        self._is_rendering = True
        region_label = str(
            self.user_data.get("active_region_label")
            or self.active_region
            or "Всі регіони"
        ).strip()
        manager_label = str(self.manager_name or "").strip()
        subtitle = (
            f"{region_label} · {manager_label}" if manager_label else region_label
        )
        _sync_indicator = self._build_workspace_sync_indicator()
        # Set correct initial text ("Збережено" / "Локально" / "Не збережено")
        with contextlib.suppress(Exception):
            self._refresh_workspace_sync_indicator_state()
        if self._header_loading_indicator is not None:
            self._header_loading_indicator.visible = False
        _loading_indicator = create_orders_header_loading_indicator(visible=False)
        self._header_loading_indicator = _loading_indicator
        header_row = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.WAREHOUSE, size=26, color=_ACCENT),
                            ft.Column(
                                [
                                    ft.Row(
                                        [
                                            ft.Text(
                                                self.t("cost_stock.title", "Собівартість / Рентабельність • Склад"),
                                                size=21,
                                                weight="bold",
                                                color=_TEXT,
                                            ),
                                            _loading_indicator,
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
                                        color=_MUTED2,
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
                                        on_click=self._guard_ui_action(
                                            "stock_table_settings",
                                            self._open_stock_base_columns_dialog,
                                            self.t("cost_orders.err.table_settings", "Помилка налаштувань таблиці"),
                                        ),
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
                spacing=6,
                tight=True,
            ),
            border=ft.border.only(bottom=ft.BorderSide(1, _HDR)),
            padding=ft.padding.only(bottom=12),
        )

        rows = self._stock_filtered()
        defaults = self._column_defaults()
        stock_ccy = str(defaults.get("stock_ccy") or "USD").upper().strip() or "USD"
        orders_unit_cost_ccy = (
            str(defaults.get("orders_unit_cost_ccy") or "USD").upper().strip() or "USD"
        )
        stock_new_price_ccy = (
            str(defaults.get("stock_new_price_ccy") or "USD").upper().strip() or "USD"
        )
        pg = self._pg()
        _sticky = getattr(pg, "_view_sticky_header", None) if pg else None
        if getattr(pg, "_active_view", "") != "show_cost_price_stock":
            _sticky = None

        # When sticky slot is available, exclude header row from table data so
        # it can be placed in the sticky widget without double-ownership issues.
        table_content = build_stock_table(
            self,
            rows,
            stock_ccy,
            orders_unit_cost_ccy,
            stock_new_price_ccy,
            include_header_row=(_sticky is None),
        )
        table_container = self._wrap_stock_table_with_scroll(table_content)
        archive_section = self._build_stock_archive_section()

        currency_bar = build_stock_currency_bar(self)
        counters = build_stock_counters(self, rows)
        filter_bar = build_stock_filter_bar(self)
        totals_row = build_stock_totals(self, rows)

        self.content_area.controls.clear()

        if _sticky is not None:
            vp_w = int(self._get_viewport_width())
            frozen_w = int(table_content.get("frozen_width") or 0)
            scroll_w = int(table_content.get("scroll_width") or 0)
            table_header_widget = self._build_stock_table_header_widget(
                table_content, vp_w, frozen_w, scroll_w
            )
            _sticky.controls.clear()
            _sticky.controls.extend([
                header_row,
                currency_bar,
                counters,
                filter_bar,
                totals_row,
                table_header_widget,
            ])
            with contextlib.suppress(Exception):
                _sticky.update()
            body_column = ft.Column([table_container, archive_section], spacing=0, tight=True)
            _v_body_items = [body_column]
        else:
            table_header_widget = None
            body_column = ft.Column(
                [currency_bar, counters, filter_bar, totals_row, table_container, archive_section],
                spacing=4,
                tight=True,
            )
            _v_body_items = [header_row, body_column]

        # Inner scroll column (transparent scrollbar) — wraps all body content.
        _v_body = ft.Column(_v_body_items, spacing=0, tight=True)
        self._v_body_col_ref = _v_body

        # H-scroll overlay buttons live OUTSIDE the scroll column → stay fixed on screen.
        _h_needs = getattr(self, "_h_scroll_needs", False)
        _h_step = getattr(self, "_h_scroll_step", 300)

        def _do_cps_hscroll(delta):
            async def _run():
                with contextlib.suppress(Exception):
                    if self._cv_body_scroll is not None:
                        await self._cv_body_scroll.scroll_to(delta=delta, duration=200)
            with contextlib.suppress(Exception):
                if pg:
                    pg.run_task(_run)

        _cps_sh = ft.BoxShadow(spread_radius=1, blur_radius=8,
            color=ft.Colors.with_opacity(0.28, ft.Colors.BLACK), offset=ft.Offset(0, 2))
        _cps_bs = ft.ButtonStyle(padding=ft.padding.all(4))
        _cps_left = ft.Container(
            content=ft.IconButton(ft.Icons.CHEVRON_LEFT, icon_size=20, icon_color=_ACCENT,
                on_click=lambda _: _do_cps_hscroll(-_h_step), style=_cps_bs),
            left=4, top=300,
            bgcolor=_PANEL, border_radius=20, width=36, height=36,
            shadow=_cps_sh, opacity=0.9, visible=_h_needs,
        )
        _cps_right = ft.Container(
            content=ft.IconButton(ft.Icons.CHEVRON_RIGHT, icon_size=20, icon_color=_ACCENT,
                on_click=lambda _: _do_cps_hscroll(_h_step), style=_cps_bs),
            right=4, top=300,
            bgcolor=_PANEL, border_radius=20, width=36, height=36,
            shadow=_cps_sh, opacity=0.9, visible=_h_needs,
        )

        try:
            self.content_area.scroll = ft.ScrollMode.ADAPTIVE
        except Exception:
            pass
        self.content_area.controls.append(_v_body)

        self._sticky_header_ref = header_row
        self._sticky_currency_bar_ref = currency_bar
        self._sticky_counters_ref = counters
        self._sticky_filter_bar_ref = filter_bar
        self._sticky_table_header_ref = table_header_widget

        if pg:
            # Lightweight resize handler — updates h-scroll button visibility only.
            _w_final_cps = getattr(self, "_h_scroll_width", 0)

            def _on_cps_resized(_e=None):
                if str(getattr(pg, "_active_view", "") or "") != "show_cost_price_stock":
                    return
                try:
                    vp = int(getattr(self.content_area, "width", 0) or 0)
                    if vp <= 0:
                        vp = int(getattr(pg, "width", 0) or 0)
                    if vp <= 0:
                        return
                    _wf = getattr(self, "_h_scroll_width", _w_final_cps)
                    new_needs = _wf > max(200, vp - 28)
                    bl = getattr(self, "_cps_left_btn", None)
                    br = getattr(self, "_cps_right_btn", None)
                    if bl is not None and bl.visible != new_needs:
                        bl.visible = new_needs
                        br.visible = new_needs
                        with contextlib.suppress(Exception):
                            bl.update()
                        with contextlib.suppress(Exception):
                            br.update()
                except Exception:
                    pass

            try:
                if hasattr(pg, "on_resized"):
                    pg.on_resized = _on_cps_resized
                elif hasattr(pg, "on_resize"):
                    pg.on_resize = _on_cps_resized
            except Exception:
                pass

            pg.update()
            # Move h-scroll buttons to page overlay so they stay visible at all
            # vertical scroll positions (not clipped inside the scrollable column).
            for _b in getattr(pg, '_catman_hscroll_btns', []):
                with contextlib.suppress(Exception):
                    pg.overlay.remove(_b)
            pg._catman_hscroll_btns = []
            if _h_needs:
                _btn_top = int((getattr(pg, 'height', None) or 900) * 0.5)
                _cps_left.top = _btn_top
                _cps_right.top = _btn_top
                pg.overlay.append(_cps_left)
                pg.overlay.append(_cps_right)
                pg._catman_hscroll_btns = [_cps_left, _cps_right]
                with contextlib.suppress(Exception):
                    pg.update()
            self._cps_left_btn = _cps_left
            self._cps_right_btn = _cps_right
            self._restore_content_scroll()
        self._is_rendering = False
        self._stock_ui_ready = True

    def _render_loading_stub(self):
        self._header_loading_indicator = create_orders_header_loading_indicator(
            visible=True
        )
        region_label = str(
            self.user_data.get("active_region_label")
            or self.active_region
            or "Всі регіони"
        ).strip()
        manager_label = str(self.manager_name or "").strip()
        subtitle = (
            f"{region_label} · {manager_label}" if manager_label else region_label
        )
        _stub_header = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.WAREHOUSE, size=26, color=_ACCENT),
                            ft.Column(
                                [
                                    ft.Row(
                                        [
                                            ft.Text(
                                                self.t("cost_stock.title", "Собівартість / Рентабельність • Склад"),
                                                size=21,
                                                weight="bold",
                                                color=_TEXT,
                                            ),
                                            self._header_loading_indicator,
                                        ],
                                        spacing=8,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        tight=True,
                                    ),
                                    ft.Text(
                                        subtitle,
                                        size=13,
                                        weight=ft.FontWeight.W_500,
                                        color=_MUTED2,
                                    ),
                                ],
                                spacing=2,
                                tight=True,
                                expand=True,
                            ),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                ],
                spacing=6,
                tight=True,
            ),
            border=ft.border.only(bottom=ft.BorderSide(1, _HDR)),
            padding=ft.padding.only(bottom=12),
        )
        pg = self._pg()
        if pg:
            for _b in getattr(pg, '_catman_hscroll_btns', []):
                with contextlib.suppress(Exception):
                    pg.overlay.remove(_b)
            pg._catman_hscroll_btns = []
        _sticky = getattr(pg, "_view_sticky_header", None) if pg else None
        if getattr(pg, "_active_view", "") != "show_cost_price_stock":
            _sticky = None
        self.content_area.controls.clear()
        if _sticky is not None:
            _sticky.controls.clear()
            _sticky.controls.append(_stub_header)
            with contextlib.suppress(Exception):
                _sticky.update()
        else:
            self.content_area.controls.append(_stub_header)
        with contextlib.suppress(Exception):
            self.content_area.update()

    async def _build_async(self) -> None:
        token = int(self._async_build_token or 0)
        await asyncio.to_thread(self._load)
        if token != int(self._async_build_token or 0):
            return
        self._sync_to_shared_map()
        self._render()

    # ──────────────────── shared orders map ──────────────────────────────────

    def _sync_order_to_shared_map(self, oid: str, order: dict) -> None:
        if oid and isinstance(order, dict):
            self._shared_orders_map[oid] = dict(order)

    def _sync_to_shared_map(self) -> None:
        """After bulk load: put orders into shared map; if another tab already
        has a newer version, adopt that version for cost calculations."""
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
        """Detect orders changed by other tabs. Returns list of updated orders."""
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
            if any(str(shared.get(k) or "") != str(local.get(k) or "") for k in local):
                self._orders[i] = dict(shared)
                updated.append(shared)
        return updated

    def _bump_sync_seq(self) -> None:
        try:
            seq = int(self._shared_orders_map.get("__sync_seq__", 0) or 0)
            self._shared_orders_map["__sync_seq__"] = seq + 1
        except Exception:
            pass

    def _purge_deleted_orders(self) -> bool:
        """Remove orders deleted in Orders tab. Returns True if any removed."""
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

    def _bg_apply_sync(self) -> None:
        if not self._stock_ui_ready:
            return
        purged = self._purge_deleted_orders()
        # Force-apply all orders from shared map (seq already confirmed changed).
        # We don't use _sync_from_shared_map() here because its field comparison
        # misses changes to keys that exist in shared but not in the local copy
        # (e.g. "items" populated by Packaging/Orders tab).
        any_updated = purged
        for i, local in enumerate(self._orders):
            if not isinstance(local, dict):
                continue
            oid = str((local.get("id") or "")).strip()
            if not oid:
                continue
            shared = self._shared_orders_map.get(oid)
            if shared is not None:
                self._orders[i] = dict(shared)
                any_updated = True
        if not any_updated:
            return
        with contextlib.suppress(Exception):
            self._build_orders_unit_cost_map()
        with contextlib.suppress(Exception):
            self._build_warehouse_rows()
        with contextlib.suppress(Exception):
            self._render()

    async def _bg_sync_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(1.5)
                seq = int(self._shared_orders_map.get("__sync_seq__", 0) or 0)
                if seq != getattr(self, "_bg_sync_seq", 0):
                    self._bg_sync_seq = seq
                    self._bg_apply_sync()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    def _inject_h_scroll_slot(self) -> None:
        """No-op: top scrollbar is now embedded in the table layout."""

    async def resume(self) -> None:
        """Called when switching back to this cached tab. Syncs cross-tab order
        changes and re-renders if the shared-map sequence changed since last visit."""
        pg = self._pg()
        _sticky = getattr(pg, "_view_sticky_header", None) if pg else None
        if _sticky is not None and getattr(pg, "_active_view", None) == "show_cost_price_stock":
            _hr = getattr(self, "_sticky_header_ref", None)
            _cb = getattr(self, "_sticky_currency_bar_ref", None)
            _cn = getattr(self, "_sticky_counters_ref", None)
            _fb = getattr(self, "_sticky_filter_bar_ref", None)
            _th = getattr(self, "_sticky_table_header_ref", None)
            new_controls = [c for c in [_hr, _cb, _cn, _fb, _th] if c is not None]
            if new_controls:
                _sticky.controls.clear()
                _sticky.controls.extend(new_controls)
                with contextlib.suppress(Exception):
                    _sticky.update()
        if not self._stock_ui_ready:
            return

        # Background: check if reference data (suppliers/products/packaging/categories) changed.
        async def _bg_ref_data_check():
            try:
                ref_pulse = await asyncio.to_thread(
                    self.db.get_reference_data_pulse, self.company_id
                )
                if ref_pulse > self._ref_data_pulse_ts:
                    await asyncio.to_thread(self._load)
                    self._ref_data_pulse_ts = ref_pulse
                    with contextlib.suppress(Exception):
                        self._render()
            except Exception:
                pass
        _ref_pg2 = self._pg()
        if _ref_pg2 and hasattr(_ref_pg2, "run_task"):
            with contextlib.suppress(Exception):
                _ref_pg2.run_task(_bg_ref_data_check)

        current_seq = int(self._shared_orders_map.get("__sync_seq__", 0) or 0)
        if current_seq == getattr(self, "_resume_last_seq", -1):
            self._inject_h_scroll_slot()
            return
        self._resume_last_seq = current_seq
        self._purge_deleted_orders()
        # Force-update every known order from the shared map.
        for i, local in enumerate(self._orders):
            if not isinstance(local, dict):
                continue
            oid = str((local.get("id") or "")).strip()
            if oid:
                shared = self._shared_orders_map.get(oid)
                if shared is not None:
                    self._orders[i] = dict(shared)
        self._build_orders_unit_cost_map()
        computed_refs = getattr(self, "_stock_computed_refs", {})
        if computed_refs:
            # Fast path: оновлюємо тільки клітинки (без повного _render()).
            # _recalc_one_warehouse_row оновлює і _warehouse_rows[i], і
            # _new_price_margin_refs[pid]["unit_cost_blended"] для нової ціни.
            for row in list(self._warehouse_rows):
                pid = str(row.get("product_id") or "")
                if pid:
                    self._recalc_one_warehouse_row(pid)
                    self._update_stock_computed_inplace(pid)
                    self._update_tier_cells_inplace(pid)
                    self._update_new_margin_inplace(pid)
        else:
            self._build_warehouse_rows()
            self._render()

    async def _workspace_sync_loop(self) -> None:
        while True:
            await asyncio.sleep(45)
            pg = self._pg()
            if (
                not pg
                or str(getattr(pg, "_active_view", "")) != "show_cost_price_stock"
            ):
                continue
            if self._workspace_outbox:
                await self._flush_workspace_state_async()
            if self._ops_outbox:
                await self._flush_ops_async()

    def _remember_content_scroll(self, e=None) -> None:
        if self._is_rendering:
            return
        try:
            val = float(getattr(e, "pixels", 0.0) or 0.0)
            if val < 0:
                return
            self._content_scroll_offset = val
        except Exception:
            pass

    def _restore_content_scroll(self) -> None:
        pg = self._pg()
        if not pg or not hasattr(pg, "run_task"):
            return
        offset = max(0.0, float(self._content_scroll_offset or 0.0))
        if offset <= 0:
            return
        _target = getattr(self, "_v_body_col_ref", None) or self.content_area

        async def _restore():
            try:
                # Wait a tick for controls to mount, otherwise Flet may ignore scroll_to.
                await asyncio.sleep(0.06)
                await _target.scroll_to(offset=offset, duration=0)
            except Exception:
                pass

        try:
            pg.run_task(_restore)
        except Exception:
            pass

    def build(self):
        _th = _th_from_view(self)
        global _PANEL, _ERR, _HDR, _MUTED, _MUTED2, _ALT, _TEXT, _OK, _ERR_BG
        global _BORDER, _ACCENT, _ACCENT2, _CHIP_T, _WARN, _WARN_DK, _OK_BG
        _PANEL = _th["panel"]; _ERR = _th["err"]; _HDR = _th["hdr"]
        _MUTED = _MUTED2 = _th["muted"]; _ALT = _ALT2 = _th["alt"]
        _TEXT = _th["text"]; _OK = _th["ok"]; _ERR_BG = _th["err_bg"]
        _BORDER = _BORDER2 = _th["border"]; _ACCENT = _ACCENT2 = _th["icon_color"]
        _CHIP_T = _th["chip_text"]; _WARN = _WARN_DK = _th["warn"]; _OK_BG = _th["ok_bg"]
        if self._general_mode and self._general_manager_ids:
            from views.general_wrapper import build_general_sections

            _cid, _db = self.company_id, self.db
            _ar = self.active_region

            def _rid_for(_ar=_ar, _db=_db, _cid=_cid):
                if not _ar:
                    return None
                for r in _db.get_company_regions(_cid) or []:
                    if isinstance(r, dict) and r.get("name") == _ar:
                        return r.get("id")
                return None

            _rid = _rid_for()
            # Count products (not orders) for stock view badge
            _all_prods = _db.get_products(_cid, region_id=_rid) or []
            _mgr_counts: dict[str, int] = {}
            for _p in _all_prods:
                if not isinstance(_p, dict):
                    continue
                _m = str(_p.get("manager_id") or "").strip()
                if _m:
                    _mgr_counts[_m] = _mgr_counts.get(_m, 0) + 1

            def _count_prods(mid, _counts=_mgr_counts):
                return _counts.get(mid, 0)

            build_general_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={"active_region": self.active_region},
                count_getter=_count_prods,
            )
            return

        # All regions → region sub-groups (single-user mode)
        if not self.active_region:
            from views.general_wrapper import build_region_sections

            _cid2, _db2, _mid2 = self.company_id, self.db, self.manager_id
            # Count products per region for stock view badge
            _all_prods2 = _db2.get_products(_cid2, manager_id=_mid2) or []
            _rcounts2: dict[str, int] = {}
            for _p2 in _all_prods2:
                if not isinstance(_p2, dict):
                    continue
                _r2 = str(_p2.get("region_id") or "").strip()
                if _r2:
                    _rcounts2[_r2] = _rcounts2.get(_r2, 0) + 1

            def _count_rcps(rid, _c=_rcounts2):
                return _c.get(rid, 0)

            build_region_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={},
                count_getter=_count_rcps,
                region_kwarg_name="active_region",
            )
            return

        self._attach_resize_handler()
        self._load_ops_outbox()
        self._render_loading_stub()
        self._async_build_token = int(self._async_build_token or 0) + 1
        token = int(self._async_build_token or 0)
        page = self._pg()
        if page and hasattr(page, "run_task"):

            async def _runner():
                await asyncio.sleep(0.01)
                if token != int(self._async_build_token or 0):
                    return
                await self._build_async()

            with contextlib.suppress(Exception):
                page.run_task(_runner)
                if not getattr(self, "_workspace_sync_loop_started", False):
                    self._workspace_sync_loop_started = True
                    page.run_task(self._workspace_sync_loop)
                if not getattr(self, "_bg_sync_loop_started", False):
                    self._bg_sync_loop_started = True
                    page.run_task(self._bg_sync_loop)
                return
        self._load()
        self._sync_to_shared_map()
        self._render()
