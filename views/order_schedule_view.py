"""Розклад замовлень — планування закупівель на 12 місяців вперед."""

import asyncio
import contextlib
import json
import os
import uuid
from datetime import date, datetime, timedelta

import flet as ft

import i18n
from logger import logger
from views import product_naming
from views.async_view_build_helper import \
    create_orders_header_loading_indicator
from views.mobile_layout_helpers import is_mobile, make_scrollable_chip_row
from views.number_format import format_grouped, row_text_matches, safe_float
from views.orders_view import _wrap_text_for_width

try:
    import requests as _requests

    _HAS_REQUESTS = True
except Exception:
    _HAS_REQUESTS = False


_BG = "#F8FAFC"
_PANEL = "#FFFFFF"
_ALT = "#F8FAFC"
_BORDER = "#CBD5E1"
_HDR = "#E2E8F0"
_TEXT = "#1E293B"
_MUTED = "#64748B"
_ACCENT = "#1D4ED8"
_SOFT = "#EFF6FF"
_WARN = "#FEF3C7"
_WARN_FG = "#92400E"
_OK = "#DCFCE7"
_OK_FG = "#166534"
_ERR = "#FEE2E2"
_ERR_FG = "#991B1B"
_SLATE = "#F1F5F9"

_LEFT_CB_W = 32
_LEFT_STATUS_W = 60
_LEFT_NAME_W = 200
_ARCHIVE_W = 100
_MONTH_W = 120
_ROW_BASE_H = 200
_ROW_CARD_H = 100
_RIGHT_CELL_PAD = 6
_PX_COL_MIN = 35
_PX_COL_MAX = 1200
_PX_ROW_MIN = 1
_PX_ROW_MAX = 300
_PX_TEXT_MIN = 6
_PX_TEXT_MAX = 32


def _safe_float(v, default=None):
    return safe_float(v, default)


def _fmt_money(v: float | None) -> str:
    if v is None:
        return ""
    return format_grouped(v, decimals=2, trim=True)


def _fmt_date(d) -> str:
    raw = str(d or "").strip()
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw[:10]).strftime("%d.%m.%Y")
    except Exception:
        return raw[:10]


def _parse_date(raw) -> date | None:
    txt = str(raw or "").strip()
    if not txt:
        return None
    try:
        return datetime.fromisoformat(txt[:10]).date()
    except Exception:
        return None


def _picker_event_iso_date(e) -> str:
    raw = getattr(e, "data", None)
    if isinstance(raw, bool) or raw is None:
        ctl = getattr(e, "control", None)
        raw = getattr(ctl, "value", None) if ctl is not None else None

    if raw is None:
        return ""

    if isinstance(raw, datetime):
        dt = raw
        if dt.tzinfo is not None:
            try:
                dt = dt.astimezone()
            except Exception:
                pass
        return dt.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()

    txt = str(raw).strip().strip('"')
    if txt.isdigit():
        try:
            num = int(txt)
            ts = (num / 1000.0) if num > 100000000000 else float(num)
            return datetime.fromtimestamp(ts).date().isoformat()
        except Exception:
            pass

    if (
        "T" in txt
        or txt.endswith("Z")
        or "+" in txt[10:]
        or "-" in txt[10:]
        or " " in txt
    ):
        try:
            dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone()
            return dt.date().isoformat()
        except Exception:
            pass

    if len(txt) >= 10:
        head = txt[:10]
        try:
            date.fromisoformat(head)
            return head
        except Exception:
            pass

    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return txt[:10]


def _month_start(d: date | None = None) -> date:
    cur = d or date.today()
    return date(cur.year, cur.month, 1)


def _add_months(d: date, months: int) -> date:
    month_idx = (d.month - 1) + int(months)
    year = d.year + month_idx // 12
    month = (month_idx % 12) + 1
    return date(year, month, 1)


def _month_key(d: date) -> str:
    return d.strftime("%Y-%m-01")


def _month_title(d: date, t=None) -> str:
    _t = t or (lambda k, v: v)
    months = {
        1: _t("orders.month.1", "Січень"),
        2: _t("orders.month.2", "Лютий"),
        3: _t("orders.month.3", "Березень"),
        4: _t("orders.month.4", "Квітень"),
        5: _t("orders.month.5", "Травень"),
        6: _t("orders.month.6", "Червень"),
        7: _t("orders.month.7", "Липень"),
        8: _t("orders.month.8", "Серпень"),
        9: _t("orders.month.9", "Вересень"),
        10: _t("orders.month.10", "Жовтень"),
        11: _t("orders.month.11", "Листопад"),
        12: _t("orders.month.12", "Грудень"),
    }
    return f"{months.get(d.month, str(d.month))} {d.year}"


class OrderScheduleView:
    CURRENCIES = ["USD", "CNY", "EUR", "UAH", "PLN"]

    def __init__(
        self,
        content_area,
        user_data,
        db,
        active_region=None,
        quick_filter: str = "",
    ):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.active_region = (
            (active_region or "").strip() if isinstance(active_region, str) else ""
        )

        self.company_id = str(self.user_data.get("company_id") or "").strip()
        self.manager_id = str(
            self.user_data.get("active_manager_id") or self.user_data.get("id") or ""
        ).strip()
        self.t = i18n.bind(str(self.user_data.get("id") or ""))
        from views import themes as _th_mod

        _th_osc = _th_mod.get_active_theme(self.manager_id)
        self._th_text = _th_osc["text"]
        self._th_muted = _th_osc["muted"]
        self._th_icon = _th_osc["icon_color"]
        self._th_panel = _th_osc["panel"]
        self._th_border = _th_osc["border"]
        self._th_chip_bg = _th_osc.get("chip_bg", _th_osc["panel"])
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
            or "Менеджер"
        ).strip()
        self.region_label = str(
            self.user_data.get("active_region_label")
            or self.active_region
            or "Всі регіони"
        ).strip()
        self.read_only = bool(self.user_data.get("subordinate_view_readonly"))

        self._region_id: str | None = None
        self._products: list[dict] = []
        self._groups: list[dict] = []
        self._tasks: list[dict] = []
        self._delivery_methods: list[dict] = []
        self._categories: list[dict] = []
        self._enabled_levels: list[dict] = []
        self._level_mode_options: list[tuple[str, str]] = []
        self._category_level_pos_by_title: dict[str, dict[str, int]] = {}
        self._title_default_pos: dict[str, int] = {}
        self._node_names: dict[str, str] = {}
        self._node_rows: dict[str, dict] = {}
        self._selected: set[str] = set()
        self._filter_category_ids: set[str] = set()
        self._quick_task_filter = str(quick_filter or "").strip().lower()
        self._search_text_query: str = ""
        self._display_mode = "name"
        self._uah_rates_cache = {}
        self._uah_rates_ts = 0.0
        self._opened_archive_month_keys: list[str] = []
        self._visible_months_count: int = 9
        self._pin_last_archive_month: bool = False
        self._table_layout = {
            "left_name_w": _LEFT_NAME_W,
            "archive_w": _ARCHIVE_W,
            "month_w": _MONTH_W,
            "row_h": _ROW_BASE_H,
            "row_card_h": _ROW_CARD_H,
            "text_size": 10,
            "text_h_align": "center",
            "text_v_align": "center",
            "text_wrap": True,
        }
        self._show_archive_column = True
        self._right_column_order = ["archive", "months"]
        self._header_loading_indicator = None
        self._async_build_token = 0
        self._workspace_state: dict = {}
        self._workspace_outbox: dict = {}
        self._workspace_outbox_running = False
        self._workspace_sync_status = "synced"
        self._workspace_sync_error_count = 0
        self._sync_indicator_text = None
        self._sync_indicator_dot = None
        self._sync_indicator_container = None
        self._workspace_sync_loop_started = False
        self._ops_outbox: dict[str, dict] = {}
        self._ops_flush_running = False
        # Scroll-position tracking (preserved across rebuilds).
        self._v_offset: float = 0.0
        self._h_offset: float = 0.0
        self._v_scroll_col = None
        self._h_scroll_row = None
        # content_area itself is also independently scrollable (ADAPTIVE) but
        # had no on_scroll tracking at all, so a page-level scroll was
        # silently lost on every rebuild (background ref-data sync, adding a
        # date, etc.) — the page visibly snapped back to the top.
        self._content_area_offset: float = 0.0
        # Per-group ownership for shared-access edit gating.
        self._owned_group_ids: set[str] = set()
        # Drag & drop: holds source task/row/month while a card is being dragged.
        self._drag_state: dict = {}
        self._drag_scroll_dir: str = ""  # "left" | "right" | ""
        self._schedule_ui_ready = False
        self._initial_loaded: bool = False
        self._schedule_cache_pulse: int = 0
        self._ref_data_pulse_ts: int = 0
        self._rebuild_scheduled: bool = False
        self._right_header_scroll = None
        self._right_body_scroll = None
        self._top_h_scrollbar = None
        self._h_sync_pending = False
        self._h_scrollbar_offset = 0.0
        self._left_w: int = 0
        self._right_inner_w: int = 0
        _som = self.user_data.get("shared_orders_map")
        self._shared_orders_map: dict[str, dict] = (
            _som if isinstance(_som, dict) else {}
        )
        # Widget refs for in-place month cell update (avoid full rebuild on task save/delete).
        self._month_cell_tasks_col_refs: dict = {}
        self._month_cell_container_refs: dict = (
            {}
        )  # same keys → cell (ft.Container) refs
        # Refs for incremental archive-month column add/remove (avoids full rebuild).
        self._right_rows_refs: list = []  # ft.Container per body row
        self._right_body_content_ref = None  # ft.Container holding right body Column
        self._last_built_rows: list = []  # row dicts from last _build_rows()
        self._left_rows_refs: list = []  # ft.Container per left body row
        self._left_body_col_ref = None  # ft.Column holding left rows
        self._right_rows_col_ref = None  # ft.Column holding right rows
        self._product_full_key_map: dict = (
            {}
        )  # pid → (name, cat, l1..l6) after hydration
        self._inactive_section_collapsed: bool = True
        # Column sort
        self._col_sort_key: str = ""  # "name" or month-key like "2026-06"
        self._col_sort_dir: int = 0  # 0=off, 1=asc, 2=desc

    def _ordered_schedule_right_columns(
        self, show_archive: bool | None = None
    ) -> list[str]:
        include_archive = (
            self._show_archive_column if show_archive is None else bool(show_archive)
        )
        allowed = ["archive", "months"] if include_archive else ["months"]
        raw = [
            str(v).strip() for v in (self._right_column_order or []) if str(v).strip()
        ]
        normalized: list[str] = []
        for cid in raw:
            if cid in allowed and cid not in normalized:
                normalized.append(cid)
        for cid in allowed:
            if cid not in normalized:
                normalized.append(cid)
        return normalized

    def _table_layout_state_key(self) -> str:
        region = str(self._region_id or self.active_region or "all").strip() or "all"
        company = str(self.company_id or "no_company").strip() or "no_company"
        return f"{company}:{region}"

    def _workspace_user_id(self) -> str:
        ud = self.user_data or {}
        return str(
            ud.get("id") or ud.get("active_manager_id") or self.manager_id or ""
        ).strip()

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

    # ── File cache for tasks + groups (pulse-verified) ────────────────────────

    def _schedule_cache_path(self) -> str:
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_reg = (
            (self._region_id or self.active_region or "all")
            .replace("/", "_")
            .replace("\\", "_")
        )
        return os.path.join(
            self._app_data_dir(),
            f"order_schedule_{safe_cid}_{safe_mgr}_{safe_reg}.json",
        )

    def _schedule_scope_pulse(self) -> int:
        try:
            out = (
                self.db.get_companies_sync_pulses([self.company_id], ["schedule"]) or {}
            )
            by_cmp = (out or {}).get(self.company_id, {})
            return int((by_cmp or {}).get("schedule") or 0)
        except Exception:
            return 0

    def _load_schedule_cache(self) -> int | None:
        """Load tasks + groups from file. Returns cached pulse_ts or None on miss."""
        path = self._schedule_cache_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            tasks = data.get("tasks")
            groups = data.get("groups")
            if not isinstance(tasks, list) or not isinstance(groups, list):
                return None
            self._tasks = [dict(t) for t in tasks if isinstance(t, dict)]
            self._groups = [dict(g) for g in groups if isinstance(g, dict)]
            return int(data.get("pulse_ts") or 0)
        except Exception:
            return None

    def _save_schedule_cache(self, pulse_ts: int) -> None:
        path = self._schedule_cache_path()
        try:
            payload = {
                "pulse_ts": int(pulse_ts),
                "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
                "tasks": [dict(t) for t in (self._tasks or []) if isinstance(t, dict)],
                "groups": [
                    dict(g) for g in (self._groups or []) if isinstance(g, dict)
                ],
            }
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
            self._schedule_cache_pulse = pulse_ts
        except Exception:
            pass

    def _apply_outbox_to_schedule(self) -> None:
        """Re-apply pending outbox items over loaded tasks/groups."""
        del_task_ids: set[str] = set()
        del_group_ids: set[str] = set()
        for op in self._ops_outbox.values():
            if not isinstance(op, dict):
                continue
            kind = op.get("kind", "")
            data = op.get("data") or {}
            eid = str(data.get("id") or "").strip()
            if not eid:
                continue
            if kind == "del_task":
                del_task_ids.add(eid)
            elif kind == "del_group":
                del_group_ids.add(eid)
            elif kind == "task":
                for i, t in enumerate(self._tasks):
                    if str((t or {}).get("id") or "").strip() == eid:
                        self._tasks[i] = dict(data)
                        break
                else:
                    self._tasks.append(dict(data))
            elif kind == "group":
                for i, g in enumerate(self._groups):
                    if str((g or {}).get("id") or "").strip() == eid:
                        self._groups[i] = dict(data)
                        break
                else:
                    self._groups.append(dict(data))
        if del_task_ids:
            self._tasks = [
                t
                for t in self._tasks
                if str((t or {}).get("id") or "").strip() not in del_task_ids
            ]
        if del_group_ids:
            self._groups = [
                g
                for g in self._groups
                if str((g or {}).get("id") or "").strip() not in del_group_ids
            ]

    def _cleanup_orphaned_groups(self) -> None:
        """Remove groups whose ALL product_ids no longer exist in self._products.
        These are left over from products deleted in the Товари tab."""
        if not self._groups or not self._products:
            return
        valid_pids = {
            str(p.get("id") or "").strip()
            for p in self._products
            if isinstance(p, dict) and p.get("id")
        }
        to_delete: list[str] = []
        keep_groups: list[dict] = []
        orphan_gids: set[str] = set()
        for g in self._groups:
            if not isinstance(g, dict):
                continue
            gid = str(g.get("id") or "").strip()
            if not gid:
                continue
            pids = [
                str(x).strip() for x in (g.get("product_ids") or []) if str(x).strip()
            ]
            if pids and not any(p in valid_pids for p in pids):
                to_delete.append(gid)
                orphan_gids.add(gid)
            else:
                keep_groups.append(g)
        if to_delete:
            self._groups = keep_groups
            self._tasks = [
                t
                for t in self._tasks
                if str((t or {}).get("group_id") or "").strip() not in orphan_gids
            ]
            for gid in to_delete:
                self._ops_outbox[f"del_group:{gid}"] = {
                    "kind": "del_group",
                    "data": {"id": gid},
                }
                self._ops_outbox.pop(f"group:{gid}", None)
            self._save_ops_outbox()

    def _workspace_outbox_file_path(self) -> str:
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        return os.path.join(
            self._app_data_dir(),
            f"order_schedule_workspace_outbox_{safe_cid}_{safe_mgr}.json",
        )

    def _workspace_state_file_path(self) -> str:
        uid = (
            str((self.user_data or {}).get("id") or "nouser")
            .replace("/", "_")
            .replace("\\", "_")
        )
        cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        rid = (
            str(self._region_id or self.active_region or "all")
            .replace("/", "_")
            .replace("\\", "_")
        )
        return os.path.join(
            self._app_data_dir(), f"order_schedule_ws_{cid}_{uid}_{rid}.json"
        )

    def _save_workspace_state_to_file(self) -> None:
        if not self._workspace_state:
            return
        try:
            import json as _json

            with open(self._workspace_state_file_path(), "w", encoding="utf-8") as f:
                _json.dump(self._workspace_state, f, ensure_ascii=False, default=str)
        except Exception:
            pass

    def _load_workspace_state(self) -> None:
        manager_id = self._workspace_user_id()
        if not manager_id:
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
        payload = self.db.get_manager_workspace_state(manager_id) or {}
        self._workspace_state = payload if isinstance(payload, dict) else {}
        self._save_workspace_state_to_file()

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

    def _ops_outbox_file_path(self) -> str:
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        return os.path.join(
            self._app_data_dir(),
            f"order_schedule_ops_outbox_{safe_cid}_{safe_mgr}.json",
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
                    if kind == "task":
                        ok = await asyncio.to_thread(
                            self.db.upsert_order_schedule_task, op["data"]
                        )
                    elif kind == "group":
                        ok = await asyncio.to_thread(
                            self.db.upsert_order_schedule_group, op["data"]
                        )
                    elif kind == "del_task":
                        ok = await asyncio.to_thread(
                            self.db.delete_order_schedule_task,
                            str((op.get("data") or {}).get("id") or ""),
                        )
                    elif kind == "del_group":
                        ok = await asyncio.to_thread(
                            self.db.delete_order_schedule_group,
                            str((op.get("data") or {}).get("id") or ""),
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
            if not error_count and not self._ops_outbox:
                # All flushed — update cache so next startup skips the DB fetch.
                new_pulse = await asyncio.to_thread(self._schedule_scope_pulse)
                if new_pulse:
                    await asyncio.to_thread(self._save_schedule_cache, new_pulse)
        except Exception:
            error_count += 1
        finally:
            self._ops_flush_running = False
            if error_count > 0:
                self._set_workspace_sync_status("error")
            else:
                self._refresh_workspace_sync_indicator_state()

    def _merge_workspace_section(self, latest: dict, snapshot: dict, key: str) -> None:
        lv = latest.get(key) if isinstance(latest.get(key), dict) else {}
        sv = snapshot.get(key) if isinstance(snapshot.get(key), dict) else {}
        if lv or sv:
            merged = dict(lv)
            merged.update(sv)
            latest[key] = merged

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
            txt.color = "#059669"
            dot.bgcolor = "#059669"
            ctr.tooltip = self.t("cost_orders.sync.synced_tip", "Workspace state вже в БД. Натисніть для примусової синхронізації.")
        elif status == "pending":
            txt.value = self.t("orders.sync.pending", "Локально")
            txt.color = "#B45309"
            dot.bgcolor = "#F59E0B"
            ctr.tooltip = self.t("cost_orders.sync.pending_tip", "Workspace state збережено локально і ще не передано в БД. Натисніть для синхронізації.")
        elif status == "syncing":
            txt.value = self.t("orders.sync.syncing", "Оновлення...")
            txt.color = "#1D4ED8"
            dot.bgcolor = "#3B82F6"
            ctr.tooltip = self.t("cost_orders.sync.syncing_tip", "Йде передача workspace state в БД.")
        elif status == "error":
            txt.value = self.t("orders.sync.error", "Не збережено")
            txt.color = "#DC2626"
            dot.bgcolor = "#DC2626"
            ctr.tooltip = self.t("cost_orders.sync.error_tip", "Workspace state не вдалося передати в БД. Натисніть для повторної спроби.")
        with contextlib.suppress(Exception):
            ctr.update()

    def _refresh_workspace_sync_indicator_state(self) -> None:
        if self._workspace_outbox_running or self._ops_flush_running:
            self._set_workspace_sync_status("syncing")
            return
        if self._workspace_outbox or self._ops_outbox:
            self._set_workspace_sync_status("pending")
            return
        self._set_workspace_sync_status("synced")

    def _build_workspace_sync_indicator(self) -> ft.Container:
        self._sync_indicator_dot = ft.Container(
            width=8, height=8, border_radius=4, bgcolor=_BORDER
        )
        self._sync_indicator_text = ft.Text("", size=10, color=self._th_text)

        def _on_force_sync(_):
            page = self._pg()
            if page and hasattr(page, "run_task"):
                with contextlib.suppress(Exception):
                    page.run_task(self.flush_pending_async)

        self._sync_indicator_container = ft.Container(
            content=ft.Row(
                [self._sync_indicator_dot, self._sync_indicator_text],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                tight=True,
            ),
            padding=ft.padding.symmetric(horizontal=8, vertical=4),
            border_radius=6,
            bgcolor=self._th_panel,
            border=ft.border.all(1, "#E2E8F0"),
            tooltip="Натисніть для збереження в БД",
            on_click=_on_force_sync,
            ink=True,
        )
        self._refresh_workspace_sync_indicator_state()
        return self._sync_indicator_container

    async def _flush_workspace_state_async(self) -> None:
        if self._workspace_outbox_running or not self._workspace_state:
            return
        self._workspace_outbox_running = True
        self._set_workspace_sync_status("syncing")
        error_count = 0
        try:
            manager_id = self._workspace_user_id()
            if not manager_id:
                self._set_workspace_sync_status("error")
                return
            snapshot = dict(self._workspace_state)
            ok = await asyncio.to_thread(
                self.db.save_manager_workspace_state, manager_id, snapshot
            )
            if ok:
                pending_snapshot = (
                    dict(self._workspace_outbox)
                    if isinstance(self._workspace_outbox, dict)
                    else {}
                )
                self._workspace_state = snapshot
                if pending_snapshot == snapshot:
                    self._workspace_outbox = {}
                    with contextlib.suppress(Exception):
                        path = self._workspace_outbox_file_path()
                        if os.path.exists(path):
                            os.remove(path)
                else:
                    self._workspace_state = pending_snapshot
                    self._save_workspace_outbox()
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
                if self._workspace_outbox:
                    page = self._pg()
                    if page and hasattr(page, "run_task"):
                        with contextlib.suppress(Exception):
                            page.run_task(self._flush_workspace_state_async)

    async def _force_sync_workspace_state_async(self) -> None:
        # See media_plan_view.py's matching method for the full reasoning:
        # _flush_workspace_state_async no-ops immediately if a flush is
        # already running, which let _restart_tab's rebuild proceed
        # before an in-flight save had actually reached the DB — the
        # fresh instance's disk/DB re-read could then race that write and
        # load a snapshot from just before it.
        for _ in range(100):  # up to ~5s
            if not self._workspace_outbox_running:
                break
            await asyncio.sleep(0.05)
        await self._flush_workspace_state_async()

    def _restart_tab(self) -> None:
        """Full tab restart after column changes — saves to local+DB then rebuilds via ctrl+f5."""
        import contextlib as _ctx

        with _ctx.suppress(Exception):
            self._save_workspace_state_to_file()
        pg = self._pg()
        if pg is None:
            self._schedule_rebuild()
            return
        dash = getattr(pg, "_dashboard", None)
        if dash is None:
            self._schedule_rebuild()
            return

        async def _do_restart():
            with _ctx.suppress(Exception):
                await self._force_sync_workspace_state_async()
            with _ctx.suppress(Exception):
                await dash._handle_ctrl_f5()

        with _ctx.suppress(Exception):
            pg.run_task(_do_restart)

    def _queue_workspace_state_save(self) -> None:
        self._save_workspace_outbox()
        self._refresh_workspace_sync_indicator_state()
        page = self._pg()
        if page and hasattr(page, "run_task"):
            with contextlib.suppress(Exception):
                page.run_task(self._flush_workspace_state_async)

    async def _workspace_sync_loop(self) -> None:
        while True:
            await asyncio.sleep(45)
            pg = self._pg()
            if not pg or str(getattr(pg, "_active_view", "")) != "show_schedule":
                continue
            if self._workspace_outbox:
                await self._flush_workspace_state_async()
            if self._ops_outbox:
                await self._flush_ops_async()

    def _apply_table_layout(self) -> None:
        global _LEFT_NAME_W, _ARCHIVE_W, _MONTH_W, _ROW_BASE_H, _ROW_CARD_H
        _LEFT_NAME_W = int(
            max(
                _PX_COL_MIN,
                min(
                    _PX_COL_MAX,
                    int(self._table_layout.get("left_name_w") or _LEFT_NAME_W),
                ),
            )
        )
        _ARCHIVE_W = int(
            max(
                _PX_COL_MIN,
                min(
                    _PX_COL_MAX, int(self._table_layout.get("archive_w") or _ARCHIVE_W)
                ),
            )
        )
        _MONTH_W = int(
            max(
                _PX_COL_MIN,
                min(_PX_COL_MAX, int(self._table_layout.get("month_w") or _MONTH_W)),
            )
        )
        _ROW_BASE_H = int(
            max(
                _PX_ROW_MIN,
                min(_PX_ROW_MAX, int(self._table_layout.get("row_h") or _ROW_BASE_H)),
            )
        )
        _ROW_CARD_H = int(
            max(
                _PX_ROW_MIN,
                min(
                    _PX_ROW_MAX,
                    int(self._table_layout.get("row_card_h") or _ROW_CARD_H),
                ),
            )
        )

    def _load_table_layout_state(self) -> None:
        manager_id = self._workspace_user_id()
        if not manager_id:
            self._apply_table_layout()
            return
        try:
            payload = (
                self._workspace_state
                if isinstance(self._workspace_state, dict) and self._workspace_state
                else (self.db.get_manager_workspace_state(manager_id) or {})
            )
            if isinstance(payload, dict):
                store = (
                    payload.get("order_schedule_table_layout")
                    if isinstance(payload.get("order_schedule_table_layout"), dict)
                    else {}
                )
                row = (
                    store.get(self._table_layout_state_key())
                    if isinstance(store, dict)
                    else None
                )
                if isinstance(row, dict):
                    for key in [
                        "left_name_w",
                        "archive_w",
                        "month_w",
                        "row_h",
                        "row_card_h",
                    ]:
                        try:
                            self._table_layout[key] = int(
                                row.get(key) or self._table_layout.get(key) or 0
                            )
                        except Exception:
                            pass
                    try:
                        self._table_layout["text_size"] = int(
                            min(
                                _PX_TEXT_MAX,
                                max(_PX_TEXT_MIN, int(row.get("text_size") or 10)),
                            )
                        )
                    except Exception:
                        pass
                    self._table_layout["text_h_align"] = "center"
                    self._table_layout["text_v_align"] = "center"
                    self._table_layout["text_wrap"] = bool(row.get("text_wrap", True))
        except Exception:
            pass
        self._apply_table_layout()

    def _save_table_layout_state(self) -> None:
        manager_id = self._workspace_user_id()
        if not manager_id:
            return
        try:
            payload = (
                dict(self._workspace_state)
                if isinstance(self._workspace_state, dict)
                else {}
            )
            store = (
                payload.get("order_schedule_table_layout")
                if isinstance(payload.get("order_schedule_table_layout"), dict)
                else {}
            )
            store[self._table_layout_state_key()] = dict(self._table_layout)
            payload["order_schedule_table_layout"] = store
            self._workspace_state = payload
            self._save_workspace_state_to_file()
            self._queue_workspace_state_save()
        except Exception:
            pass

    def _load_visible_columns_state(self) -> None:
        manager_id = self._workspace_user_id()
        if not manager_id:
            return
        try:
            payload = (
                self._workspace_state
                if isinstance(self._workspace_state, dict) and self._workspace_state
                else (self.db.get_manager_workspace_state(manager_id) or {})
            )
            # ── month view settings ───────────────────────────────────────────
            mv = (
                payload.get("order_schedule_month_view")
                if isinstance(payload, dict)
                else {}
            )
            if isinstance(mv, dict):
                mv_row = (
                    mv.get(self._table_layout_state_key())
                    if isinstance(mv, dict)
                    else None
                )
                if isinstance(mv_row, dict):
                    self._visible_months_count = max(
                        1, min(24, int(mv_row.get("visible_months_count") or 9))
                    )
                    self._pin_last_archive_month = bool(
                        mv_row.get("pin_last_archive_month", False)
                    )
                    raw_keys = mv_row.get("opened_archive_month_keys") or []
                    if isinstance(raw_keys, list):
                        self._opened_archive_month_keys = [
                            str(k) for k in raw_keys if k
                        ]
            # ── visible columns (archive) ─────────────────────────────────────
            store = (
                payload.get("order_schedule_visible_columns")
                if isinstance(payload, dict)
                else {}
            )
            row = (
                store.get(self._table_layout_state_key())
                if isinstance(store, dict)
                else None
            )
            if isinstance(row, dict):
                self._show_archive_column = bool(row.get("archive", True))
                raw_order = row.get("order")
                if isinstance(raw_order, list):
                    order: list[str] = []
                    for v in raw_order:
                        cid = str(v).strip()
                        if cid in {"archive", "months"} and cid not in order:
                            order.append(cid)
                    for cid in ["archive", "months"]:
                        if cid not in order:
                            order.append(cid)
                    if order:
                        self._right_column_order = order
        except Exception:
            pass

    def _save_month_view_settings(self) -> None:
        manager_id = self._workspace_user_id()
        if not manager_id:
            return
        try:
            payload = (
                dict(self._workspace_state)
                if isinstance(self._workspace_state, dict)
                else {}
            )
            store = (
                payload.get("order_schedule_month_view")
                if isinstance(payload.get("order_schedule_month_view"), dict)
                else {}
            )
            store[self._table_layout_state_key()] = {
                "visible_months_count": self._visible_months_count,
                "pin_last_archive_month": self._pin_last_archive_month,
                "opened_archive_month_keys": list(self._opened_archive_month_keys),
            }
            payload["order_schedule_month_view"] = store
            self._workspace_state = payload
            self._save_workspace_state_to_file()
            self._queue_workspace_state_save()
        except Exception:
            pass

    def _save_visible_columns_state(self) -> None:
        manager_id = self._workspace_user_id()
        if not manager_id:
            return
        try:
            payload = (
                dict(self._workspace_state)
                if isinstance(self._workspace_state, dict)
                else {}
            )
            store = (
                payload.get("order_schedule_visible_columns")
                if isinstance(payload.get("order_schedule_visible_columns"), dict)
                else {}
            )
            store[self._table_layout_state_key()] = {
                "archive": bool(self._show_archive_column),
                "order": list(self._ordered_schedule_right_columns(show_archive=True)),
            }
            payload["order_schedule_visible_columns"] = store
            self._workspace_state = payload
            self._save_workspace_state_to_file()
            self._queue_workspace_state_save()
        except Exception:
            pass

    def _open_table_dialog(self, _=None) -> None:
        """Combined Таблиця dialog with two tabs: Стовпчики and Розміри."""
        page = self._pg()
        if not page:
            return

        # ── Tab 1: Стовпчики content ───────────────────────────────────────────
        cb_archive = ft.Checkbox(label="Архів", value=bool(self._show_archive_column))
        months_f = ft.TextField(
            label=self.t("media.field.months_count", "Кількість місяців"),
            value=str(self._visible_months_count),
            width=200,
            hint_text="1–24",
            keyboard_type=ft.KeyboardType.NUMBER,
            # No on_change previously — Flet doesn't push keystroke updates
            # back to this control's server-side .value without a
            # registered handler, so "Зберегти" (which reads months_f.value
            # directly) kept seeing the ORIGINAL value no matter what was
            # typed, always falling through to look unchanged/reverted.
            on_change=lambda e: None,
        )

        def _save_months_only(_=None):
            # Standalone save workaround: commits just the months count,
            # independent of the archive/order/pin settings. Uses
            # _schedule_rebuild (redraw THIS instance from its
            # already-updated in-memory state), not _restart_tab —
            # _restart_tab's hard ctrl+f5 rebuild creates a brand-new
            # instance that re-reads workspace state from disk/DB, which
            # raced the still-in-flight async flush this same save queues
            # and could load a snapshot from just before this change.
            # _schedule_rebuild never touches disk/DB, so there's nothing
            # to race — this is what actually made the field's value
            # reliably stick, on top of the earlier on_change fix.
            try:
                cnt = max(1, min(24, int(str(months_f.value or "12").strip() or "12")))
            except Exception:
                cnt = 12
            self._visible_months_count = cnt
            self._save_month_view_settings()
            self._schedule_rebuild()
            self._snack(self.t("media.months_saved", f"Збережено: {cnt} міс."), "#10B981")

        months_row = ft.Row(
            [
                months_f,
                ft.IconButton(
                    icon=ft.Icons.SAVE_OUTLINED,
                    tooltip=self.t("media.field.months_save", "Зберегти кількість місяців"),
                    on_click=_save_months_only,
                    style=ft.ButtonStyle(padding=ft.padding.all(4)),
                ),
            ],
            spacing=2,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        current_order_value = ",".join(
            self._ordered_schedule_right_columns(show_archive=True)
        )
        if current_order_value not in {"archive,months", "months,archive"}:
            current_order_value = "archive,months"
        order_dd = ft.Dropdown(
            label=self.t("schedule.field.right_block_order", "Порядок правого блоку"),
            width=280,
            value=current_order_value,
            options=[
                ft.dropdown.Option("archive,months", self.t("schedule.field.order_archive_months", "Архів → Місяці")),
                ft.dropdown.Option("months,archive", self.t("schedule.field.order_months_archive", "Місяці → Архів")),
            ],
        )
        pin_cb = ft.Checkbox(
            label=self.t("media.field.show_prev_month", "Показувати попередній місяць"),
            value=self._pin_last_archive_month,
        )

        def _save_columns(_e=None):
            self._show_archive_column = bool(cb_archive.value)
            raw_order_value = str(order_dd.value or "archive,months").strip()
            order = [
                v for v in raw_order_value.split(",") if v in {"archive", "months"}
            ]
            if order:
                self._right_column_order = order
            self._save_visible_columns_state()
            try:
                cnt = max(1, min(24, int(str(months_f.value or "12").strip() or "12")))
            except Exception:
                cnt = 12
            self._visible_months_count = cnt
            self._pin_last_archive_month = bool(pin_cb.value)
            self._save_month_view_settings()
            page.pop_dialog()
            self._restart_tab()

        tab1_content = ft.Container(
            width=350,
            content=ft.Column(
                [
                    cb_archive,
                    ft.Divider(height=8),
                    order_dd,
                    ft.Divider(height=8),
                    months_row,
                    pin_cb,
                    ft.Divider(height=8),
                    ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save_columns),
                ],
                spacing=6,
                scroll=ft.ScrollMode.AUTO,
            ),
            height=340,
        )

        # ── Tab 2: Розміри content ─────────────────────────────────────────────
        left_name_f = ft.TextField(
            label=self.t("schedule.field.position", "Позиція розкладу"),
            value=str(int(self._table_layout.get("left_name_w") or _LEFT_NAME_W)),
            width=180,
        )
        archive_f = ft.TextField(
            label=self.t("media.field.archive", "Архів"),
            value=str(int(self._table_layout.get("archive_w") or _ARCHIVE_W)),
            width=180,
        )
        month_f = ft.TextField(
            label=self.t("media.field.month", "Місяць"),
            value=str(int(self._table_layout.get("month_w") or _MONTH_W)),
            width=180,
        )
        row_h_f = ft.TextField(
            label=self.t("orders.col_settings.row_height", "Висота рядка"),
            value=str(int(self._table_layout.get("row_h") or _ROW_BASE_H)),
            width=180,
        )
        card_h_f = ft.TextField(
            label=self.t("schedule.field.card_height", "Висота картки"),
            value=str(int(self._table_layout.get("row_card_h") or _ROW_CARD_H)),
            width=180,
        )
        txt_size_f = ft.TextField(
            label=self.t("media.field.text_size", "Розмір тексту"),
            value=str(int(self._table_layout.get("text_size") or 10)),
            width=180,
        )
        wrap_cb = ft.Checkbox(
            label=self.t("media.field.text_wrap", "Перенесення тексту"),
            value=bool(self._table_layout.get("text_wrap", True)),
        )

        def _save_layout(_e=None):
            self._table_layout["left_name_w"] = int(
                max(
                    _PX_COL_MIN,
                    min(
                        _PX_COL_MAX,
                        int(
                            _safe_float(left_name_f.value, _LEFT_NAME_W) or _LEFT_NAME_W
                        ),
                    ),
                )
            )
            self._table_layout["archive_w"] = int(
                max(
                    _PX_COL_MIN,
                    min(
                        _PX_COL_MAX,
                        int(_safe_float(archive_f.value, _ARCHIVE_W) or _ARCHIVE_W),
                    ),
                )
            )
            self._table_layout["month_w"] = int(
                max(
                    _PX_COL_MIN,
                    min(
                        _PX_COL_MAX,
                        int(_safe_float(month_f.value, _MONTH_W) or _MONTH_W),
                    ),
                )
            )
            self._table_layout["row_h"] = int(
                max(
                    _PX_ROW_MIN,
                    min(
                        _PX_ROW_MAX,
                        int(_safe_float(row_h_f.value, _ROW_BASE_H) or _ROW_BASE_H),
                    ),
                )
            )
            self._table_layout["row_card_h"] = int(
                max(
                    _PX_ROW_MIN,
                    min(
                        _PX_ROW_MAX,
                        int(_safe_float(card_h_f.value, _ROW_CARD_H) or _ROW_CARD_H),
                    ),
                )
            )
            self._table_layout["text_size"] = int(
                min(
                    _PX_TEXT_MAX,
                    max(_PX_TEXT_MIN, int(_safe_float(txt_size_f.value, 10) or 10)),
                )
            )
            self._table_layout["text_h_align"] = "center"
            self._table_layout["text_v_align"] = "center"
            self._table_layout["text_wrap"] = bool(wrap_cb.value)
            self._apply_table_layout()
            self._save_table_layout_state()
            page.pop_dialog()
            self._schedule_rebuild()

        tab2_content = ft.Container(
            width=460,
            content=ft.Column(
                [
                    ft.Text("Ширини стовпчиків (px)", size=11, color=_MUTED),
                    ft.Row([left_name_f, archive_f], wrap=True, spacing=8),
                    ft.Row([month_f], wrap=True, spacing=8),
                    ft.Divider(height=8),
                    ft.Row([row_h_f, card_h_f], wrap=True, spacing=8),
                    ft.Row([txt_size_f], wrap=True, spacing=8),
                    wrap_cb,
                    ft.Divider(height=8),
                    ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save_layout),
                ],
                spacing=8,
                scroll=ft.ScrollMode.AUTO,
            ),
            height=400,
        )

        # ── Combine into AlertDialog with Tabs ─────────────────────────────────
        combined_dlg = ft.AlertDialog(modal=True)

        def _close_combined(_=None):
            page.pop_dialog()

        def _tbl_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, self.t("payments.dlg.table_title", "Таблиця"), [
                self.t("schedule.tbl_help.step1", "1. «Стовпчики» — вмикайте/вимикайте стовпці."),
                self.t("schedule.tbl_help.step2", "2. «Розміри» — налаштуйте ширину стовпців і висоту рядків."),
                self.t("schedule.tbl_help.step3", "3. Натисніть «Закрити»."),
            ])

        combined_dlg.title = ft.Row(
            [
                ft.Text(self.t("payments.dlg.table_title", "Таблиця"), expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_tbl_help),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
        )
        combined_dlg.content = ft.Container(
            width=480,
            height=460,
            content=ft.Column(
                [
                    ft.Text(
                        "Стовпчики",
                        size=12,
                        weight=ft.FontWeight.W_600,
                        color=self._th_text,
                    ),
                    tab1_content,
                    ft.Divider(height=12),
                    ft.Text(
                        "Розміри",
                        size=12,
                        weight=ft.FontWeight.W_600,
                        color=self._th_text,
                    ),
                    tab2_content,
                ],
                scroll=ft.ScrollMode.AUTO,
                spacing=4,
            ),
        )
        combined_dlg.actions = [
            ft.TextButton(self.t("packaging.dlg.close", "Закрити"), on_click=_close_combined),
        ]
        combined_dlg.actions_alignment = ft.MainAxisAlignment.END
        page.show_dialog(combined_dlg)

    def _open_budget_dialog(self, _=None) -> None:
        """Бюджет modal: monthly budget totals grouped by category/level/name."""
        page = self._pg()
        if not page:
            return

        months = self._visible_month_anchors()

        try:
            saved_mode = str(
                self.db.get_manager_ui_preference(
                    self.manager_id, "order_schedule_budget_mode", ""
                )
                or ""
            ).strip()
        except Exception:
            saved_mode = ""

        group_opts: list[tuple[str, str]] = [
            ("category", "Категорія"),
            ("name", "Назва"),
        ]
        group_opts.extend(self._level_mode_options or [])
        valid_keys = {k for k, _ in group_opts}
        if saved_mode not in valid_keys:
            saved_mode = next(iter(valid_keys), "category")

        cur_mode: list[str] = [saved_mode]
        _default_ccy = self.CURRENCIES[0] if self.CURRENCIES else "USD"
        cur_ccy: list[str] = [_default_ccy]

        def _calc_budget(mode: str) -> dict[str, dict[str, float]]:
            """Returns amounts in USD; _render_table converts to display currency."""
            pbid = self._products_by_id()
            tbg = self._tasks_by_group()
            grp_label: dict[str, str] = {}
            for row in self._build_rows():
                if mode == "name":
                    lbl = self._group_display_label(row)
                else:
                    pids = [
                        str(p or "").strip()
                        for p in (row.get("product_ids") or [])
                        if str(p or "").strip()
                    ]
                    first_prod = next((pbid[p] for p in pids if p in pbid), None)
                    raw = (
                        self._display_value_for_product(first_prod, mode)
                        if first_prod
                        else ""
                    ) or ""
                    lbl = raw or "Інше"
                for gid in row.get("group_ids") or (
                    [str(row.get("group_id") or "")] if row.get("group_id") else []
                ):
                    gs = str(gid or "").strip()
                    if gs and gs not in grp_label:
                        grp_label[gs] = lbl

            grps_by_id = {
                str((g or {}).get("id") or "").strip(): g
                for g in (self._groups or [])
                if isinstance(g, dict) and (g.get("id") or "")
            }
            out: dict[str, dict[str, float]] = {}

            for gid, tasks in tbg.items():
                lbl = grp_label.get(str(gid or ""))
                if lbl is None:
                    continue
                grp = grps_by_id.get(str(gid or ""))
                term = 0
                if grp:
                    grp_term = grp.get("production_term_days")
                    if grp_term is not None:
                        try:
                            term = int(grp_term)
                        except Exception:
                            pass
                    else:
                        for pid in grp.get("product_ids") or []:
                            prod = pbid.get(str(pid or "").strip())
                            if prod is not None:
                                try:
                                    t = prod.get("production_term_days")
                                    if t is not None:
                                        term = int(t)
                                except Exception:
                                    pass
                                break
                for task in tasks:
                    if not isinstance(task, dict):
                        continue
                    # Use actual amount when available, fall back to planned
                    amt_usd = _safe_float(task.get("actual_amount_usd"), None)
                    if amt_usd is None or amt_usd == 0.0:
                        amt_usd = _safe_float(task.get("planned_amount_usd"), None)
                    if not amt_usd:
                        continue
                    # Base date: actual order start date (date_from).
                    # Adding production_term_days to date_from gives the payment/ready date.
                    # E.g. date_from=05.05 + 35d = 09.06 → budget falls in June.
                    base = _parse_date(task.get("date_from"))
                    if not base:
                        anchor = _parse_date(task.get("month_anchor"))
                        if anchor:
                            base = _month_start(anchor)
                        else:
                            continue
                    tgt_mk = _month_key(_month_start(base + timedelta(days=term)))
                    bucket = out.setdefault(lbl, {})
                    bucket[tgt_mk] = bucket.get(tgt_mk, 0.0) + amt_usd
            return out

        # Cell layout constants and builders (used both in header_ctrl and _render_table).
        LW, CW, RH, HH = 200, 110, 30, 36

        def _lcell(text: str, bold: bool = False, header: bool = False) -> ft.Container:
            return ft.Container(
                width=LW,
                height=HH if header else RH,
                content=ft.Text(
                    text,
                    size=10,
                    weight="bold" if bold else None,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    no_wrap=True,
                ),
                padding=ft.padding.only(left=6, right=4, top=2, bottom=2),
                alignment=ft.Alignment(-1, 0),
            )

        def _mcell(
            text: str, bold: bool = False, muted: bool = False, header: bool = False
        ) -> ft.Container:
            return ft.Container(
                width=CW,
                height=HH if header else RH,
                content=ft.Text(
                    text,
                    size=10,
                    weight="bold" if bold else None,
                    color=_MUTED if muted else None,
                    text_align=ft.TextAlign.RIGHT,
                    overflow=ft.TextOverflow.ELLIPSIS,
                ),
                alignment=ft.Alignment(1, 0),
                padding=ft.padding.only(right=8, top=2, bottom=2),
            )

        # table_slot holds only data rows (header row is separate and fixed).
        table_slot = ft.Column(spacing=0, tight=True)
        # body_col scrolls vertically; header_ctrl stays pinned above it.
        body_col = ft.Column(
            [table_slot], spacing=0, scroll=ft.ScrollMode.AUTO, expand=True
        )

        def _fmt_budget_amt(usd_val: float, ccy: str) -> str:
            converted = (
                self._convert_amount(usd_val, "USD", ccy) if ccy != "USD" else usd_val
            )
            if converted is None:
                converted = usd_val
                ccy = "USD"
            return f"{converted:,.0f} {ccy}"

        def _render_table():
            ccy = cur_ccy[0]
            budget = _calc_budget(cur_mode[0])
            labels = sorted(budget.keys(), key=str.lower)
            mkeys = [_month_key(m) for m in months]
            totals = {
                mk: sum(budget.get(lbl, {}).get(mk, 0.0) for lbl in labels)
                for mk in mkeys
            }

            rows: list = []

            for i, lbl in enumerate(labels):
                row_data = budget.get(lbl, {})
                rows.append(
                    ft.Container(
                        bgcolor=_ALT if i % 2 else None,
                        content=ft.Row(
                            [_lcell(lbl)]
                            + [
                                _mcell(
                                    (
                                        _fmt_budget_amt(row_data[mk], ccy)
                                        if row_data.get(mk)
                                        else "—"
                                    ),
                                    muted=not row_data.get(mk),
                                )
                                for mk in mkeys
                            ],
                            spacing=0,
                            tight=True,
                        ),
                    )
                )

            rows.append(ft.Divider(height=1, thickness=1, color=_BORDER))
            rows.append(
                ft.Container(
                    bgcolor=_SOFT,
                    content=ft.Row(
                        [_lcell("Разом", bold=True)]
                        + [
                            _mcell(
                                (
                                    _fmt_budget_amt(totals[mk], ccy)
                                    if totals.get(mk)
                                    else "—"
                                ),
                                bold=True,
                            )
                            for mk in mkeys
                        ],
                        spacing=0,
                        tight=True,
                    ),
                )
            )

            table_slot.controls = rows
            with contextlib.suppress(Exception):
                page.update()

        def _on_mode(e):
            nm = str(e.control.value or "").strip()
            if nm not in valid_keys:
                return
            cur_mode[0] = nm
            try:
                self.db.set_manager_ui_preference(
                    self.manager_id, "order_schedule_budget_mode", nm
                )
            except Exception:
                pass
            _render_table()

        def _on_ccy(e):
            cur_ccy[0] = str(e.control.value or _default_ccy).strip() or _default_ccy
            _render_table()

        mode_dd = ft.Dropdown(
            label=self.t("schedule.field.grouping", "Групування"),
            value=cur_mode[0],
            options=[ft.dropdown.Option(k, v) for k, v in group_opts],
            width=220,
        )
        mode_dd.on_select = _on_mode
        mode_dd.on_change = _on_mode

        ccy_dd = ft.Dropdown(
            label=self.t("orders.field.currency", "Валюта"),
            value=cur_ccy[0],
            options=[ft.dropdown.Option(c) for c in self.CURRENCIES],
            width=110,
        )
        ccy_dd.on_select = _on_ccy
        ccy_dd.on_change = _on_ccy

        dlg = ft.AlertDialog(modal=True)

        def _close(_=None):
            page.pop_dialog()

        # Budget h-scroll: buttons appear when month columns overflow dialog width.
        # Dialog inner width ≈ 960 − 32 (padding 16×2) = 928; LW=200, CW=110 per month.
        _b_needs = (200 + len(months) * 110) > 912
        _b_step = max(300, len(months) * 110 // 3)
        _b_hscroll_ref = ft.Ref[ft.Row]()

        def _do_budget_hscroll(delta):
            async def _run():
                with contextlib.suppress(Exception):
                    if _b_hscroll_ref.current:
                        await _b_hscroll_ref.current.scroll_to(
                            delta=delta, duration=200
                        )

            with contextlib.suppress(Exception):
                page.run_task(_run)

        _b_sh = ft.BoxShadow(
            spread_radius=1,
            blur_radius=6,
            color=ft.Colors.with_opacity(0.28, ft.Colors.BLACK),
            offset=ft.Offset(0, 2),
        )
        _b_bs = ft.ButtonStyle(padding=ft.padding.all(4))
        _b_left = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_LEFT,
                icon_size=18,
                icon_color=_ACCENT,
                on_click=lambda _: _do_budget_hscroll(-_b_step),
                style=_b_bs,
            ),
            left=4,
            top=195,
            bgcolor=_PANEL,
            border_radius=18,
            width=32,
            height=32,
            shadow=_b_sh,
            opacity=0.9,
            visible=_b_needs,
        )
        _b_right = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_RIGHT,
                icon_size=18,
                icon_color=_ACCENT,
                on_click=lambda _: _do_budget_hscroll(_b_step),
                style=_b_bs,
            ),
            right=4,
            top=195,
            bgcolor=_PANEL,
            border_radius=18,
            width=32,
            height=32,
            shadow=_b_sh,
            opacity=0.9,
            visible=_b_needs,
        )

        # Header stays out of the scrolling Row entirely and is repositioned via
        # left=-offset in lockstep with the body's own on_scroll event — the
        # same proven pattern as the main schedule table's sticky header
        # (self._on_h_scroll / _right_header_stack_child). Nesting header+body
        # as one shared scrollable (the previous approach) visually desynced
        # them once scroll_to() was used (the header/columns "з'їжджають").
        header_ctrl = ft.Container(
            bgcolor=_HDR,
            content=ft.Row(
                [_lcell("Група", bold=True, header=True)]
                + [_mcell(_month_title(m, self.t), bold=True, header=True) for m in months],
                spacing=0,
                tight=True,
            ),
            left=0,
            top=0,
        )
        header_viewport = ft.Container(
            height=HH,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.Stack([header_ctrl]),
        )

        def _on_budget_h_scroll(e):
            try:
                offset = float(getattr(e, "pixels", 0) or 0)
            except Exception:
                return
            header_ctrl.left = -offset
            with contextlib.suppress(Exception):
                header_ctrl.update()

        body_h_scroll_row = ft.Row(
            [body_col],
            spacing=0,
            scroll=ft.ScrollMode.HIDDEN,
            on_scroll=_on_budget_h_scroll,
            vertical_alignment=ft.CrossAxisAlignment.START,
            ref=_b_hscroll_ref,
        )
        body_viewport = ft.Container(
            height=430 - HH,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=body_h_scroll_row,
        )
        # inner_col: header (fixed viewport) + divider + body (own h+v scroll).
        inner_col = ft.Column(
            [
                header_viewport,
                ft.Divider(height=1, thickness=1, color=_BORDER),
                body_viewport,
            ],
            spacing=0,
            expand=True,
        )

        def _budget_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, self.t("schedule.dlg.budget", "Бюджет"), [
                self.t("schedule.budget_help.step1", "1. Оберіть режим групування (категорія/рівень/назва)."),
                self.t("schedule.budget_help.step2", "2. Оберіть валюту для розрахунку."),
                self.t("schedule.budget_help.step3", "3. Таблиця показує планові суми по місяцях для кожної групи."),
            ])

        dlg.title = ft.Row(
            [
                ft.Text(self.t("schedule.dlg.budget", "Бюджет"), weight="bold", expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_budget_help),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
        )
        dlg.content = ft.Container(
            width=960,
            height=520,
            bgcolor=_PANEL,
            padding=16,
            content=ft.Column(
                [
                    ft.Row([mode_dd, ccy_dd], spacing=12),
                    ft.Divider(height=4, color=_BORDER),
                    ft.Stack(
                        [
                            inner_col,
                            _b_left,
                            _b_right,
                        ]
                    ),
                ],
                spacing=6,
            ),
        )
        dlg.actions = [ft.TextButton(self.t("packaging.dlg.close", "Закрити"), on_click=_close)]
        dlg.actions_alignment = ft.MainAxisAlignment.END

        _render_table()
        page.show_dialog(dlg)

    def _open_columns_dialog(self, _=None):
        page = self._pg()
        if not page:
            return
        cb_archive = ft.Checkbox(label="Архів", value=bool(self._show_archive_column))
        months_f = ft.TextField(
            label=self.t("media.field.months_count", "Кількість місяців"),
            value=str(self._visible_months_count),
            width=200,
            hint_text="1–24",
            keyboard_type=ft.KeyboardType.NUMBER,
            # No on_change previously — Flet doesn't push keystroke updates
            # back to this control's server-side .value without a
            # registered handler, so "Зберегти" (which reads months_f.value
            # directly) kept seeing the ORIGINAL value no matter what was
            # typed, always falling through to look unchanged/reverted.
            on_change=lambda e: None,
        )

        def _save_months_only(_=None):
            # Standalone save workaround: commits just the months count,
            # independent of the archive/order/pin settings and without
            # needing the big "Зберегти" below.
            try:
                cnt = max(1, min(24, int(str(months_f.value or "12").strip() or "12")))
            except Exception:
                cnt = 12
            self._visible_months_count = cnt
            self._save_month_view_settings()
            self._schedule_rebuild()
            self._snack(self.t("media.months_saved", f"Збережено: {cnt} міс."), "#10B981")

        months_row = ft.Row(
            [
                months_f,
                ft.IconButton(
                    icon=ft.Icons.SAVE_OUTLINED,
                    tooltip=self.t("media.field.months_save", "Зберегти кількість місяців"),
                    on_click=_save_months_only,
                    style=ft.ButtonStyle(padding=ft.padding.all(4)),
                ),
            ],
            spacing=2,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        current_order_value = ",".join(
            self._ordered_schedule_right_columns(show_archive=True)
        )
        if current_order_value not in {"archive,months", "months,archive"}:
            current_order_value = "archive,months"
        order_dd = ft.Dropdown(
            label=self.t("schedule.field.right_block_order", "Порядок правого блоку"),
            width=280,
            value=current_order_value,
            options=[
                ft.dropdown.Option("archive,months", self.t("schedule.field.order_archive_months", "Архів → Місяці")),
                ft.dropdown.Option("months,archive", self.t("schedule.field.order_months_archive", "Місяці → Архів")),
            ],
        )
        pin_cb = ft.Checkbox(
            label=self.t("media.field.show_prev_month", "Показувати попередній місяць"),
            value=self._pin_last_archive_month,
        )
        dlg = ft.AlertDialog(modal=True)

        def _close(_e=None):
            page.pop_dialog()

        def _save(_e=None):
            self._show_archive_column = bool(cb_archive.value)
            raw_order_value = str(order_dd.value or "archive,months").strip()
            order = [
                v for v in raw_order_value.split(",") if v in {"archive", "months"}
            ]
            if order:
                self._right_column_order = order
            self._save_visible_columns_state()
            try:
                cnt = max(1, min(24, int(str(months_f.value or "12").strip() or "12")))
            except Exception:
                cnt = 12
            self._visible_months_count = cnt
            self._pin_last_archive_month = bool(pin_cb.value)
            self._save_month_view_settings()
            page.pop_dialog()
            self._schedule_rebuild()

        def _cols_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, self.t("media.dlg.cols_months", "Стовпці та місяці"), [
                self.t("schedule.cols_help.step1", "1. Вмикайте/вимикайте показ колонки Архів."),
                self.t("schedule.cols_help.step2", "2. Оберіть порядок правого блоку (Архів → Місяці або навпаки)."),
                self.t("schedule.cols_help.step3", "3. Вкажіть кількість видимих місяців."),
                self.t("schedule.cols_help.step4", "4. Натисніть «Зберегти»."),
            ])

        dlg.title = ft.Row(
            [
                ft.Text(self.t("media.dlg.cols_months", "Стовпці та місяці"), expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_cols_help),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
        )
        dlg.content = ft.Container(
            width=320,
            content=ft.Column(
                [
                    cb_archive,
                    ft.Divider(height=8),
                    order_dd,
                    ft.Divider(height=8),
                    months_row,
                    pin_cb,
                ],
                spacing=6,
            ),
        )
        dlg.actions = [
            ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_close),
            ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save),
        ]
        dlg.actions_alignment = ft.MainAxisAlignment.END
        page.show_dialog(dlg)

    def _open_table_layout_dialog(self, _=None):
        page = self._pg()
        if not page:
            return
        left_name_f = ft.TextField(
            label=self.t("schedule.field.position", "Позиція розкладу"),
            value=str(int(self._table_layout.get("left_name_w") or _LEFT_NAME_W)),
            width=180,
        )
        archive_f = ft.TextField(
            label=self.t("media.field.archive", "Архів"),
            value=str(int(self._table_layout.get("archive_w") or _ARCHIVE_W)),
            width=180,
        )
        month_f = ft.TextField(
            label=self.t("media.field.month", "Місяць"),
            value=str(int(self._table_layout.get("month_w") or _MONTH_W)),
            width=180,
        )
        row_h_f = ft.TextField(
            label=self.t("orders.col_settings.row_height", "Висота рядка"),
            value=str(int(self._table_layout.get("row_h") or _ROW_BASE_H)),
            width=180,
        )
        card_h_f = ft.TextField(
            label=self.t("schedule.field.card_height", "Висота картки"),
            value=str(int(self._table_layout.get("row_card_h") or _ROW_CARD_H)),
            width=180,
        )
        txt_size_f = ft.TextField(
            label=self.t("media.field.text_size", "Розмір тексту"),
            value=str(int(self._table_layout.get("text_size") or 10)),
            width=180,
        )
        wrap_cb = ft.Checkbox(
            label=self.t("media.field.text_wrap", "Перенесення тексту"),
            value=bool(self._table_layout.get("text_wrap", True)),
        )

        dlg = ft.AlertDialog(modal=True)

        def _close(_e=None):
            page.pop_dialog()

        def _save(_e=None):
            self._table_layout["left_name_w"] = int(
                max(
                    _PX_COL_MIN,
                    min(
                        _PX_COL_MAX,
                        int(
                            _safe_float(left_name_f.value, _LEFT_NAME_W) or _LEFT_NAME_W
                        ),
                    ),
                )
            )
            self._table_layout["archive_w"] = int(
                max(
                    _PX_COL_MIN,
                    min(
                        _PX_COL_MAX,
                        int(_safe_float(archive_f.value, _ARCHIVE_W) or _ARCHIVE_W),
                    ),
                )
            )
            self._table_layout["month_w"] = int(
                max(
                    _PX_COL_MIN,
                    min(
                        _PX_COL_MAX,
                        int(_safe_float(month_f.value, _MONTH_W) or _MONTH_W),
                    ),
                )
            )
            self._table_layout["row_h"] = int(
                max(
                    _PX_ROW_MIN,
                    min(
                        _PX_ROW_MAX,
                        int(_safe_float(row_h_f.value, _ROW_BASE_H) or _ROW_BASE_H),
                    ),
                )
            )
            self._table_layout["row_card_h"] = int(
                max(
                    _PX_ROW_MIN,
                    min(
                        _PX_ROW_MAX,
                        int(_safe_float(card_h_f.value, _ROW_CARD_H) or _ROW_CARD_H),
                    ),
                )
            )
            self._table_layout["text_size"] = int(
                min(
                    _PX_TEXT_MAX,
                    max(_PX_TEXT_MIN, int(_safe_float(txt_size_f.value, 10) or 10)),
                )
            )
            self._table_layout["text_h_align"] = "center"
            self._table_layout["text_v_align"] = "center"
            self._table_layout["text_wrap"] = bool(wrap_cb.value)
            self._apply_table_layout()
            self._save_table_layout_state()
            page.pop_dialog()
            self._schedule_rebuild()

        def _sizes_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, self.t("media.dlg.col_width_height", "Стовпчик / Ширина / Висота"), [
                self.t("schedule.sizes_help.step1", "1. Введіть ширину (px) для кожного стовпчика."),
                self.t("schedule.sizes_help.step2", "2. Вкажіть висоту рядка і картки."),
                self.t("schedule.sizes_help.step3", "3. Налаштуйте розмір тексту і перенос рядків."),
                self.t("schedule.sizes_help.step4", "4. Натисніть «Зберегти»."),
            ])

        dlg.title = ft.Row(
            [
                ft.Text(self.t("media.dlg.col_width_height", "Стовпчик / Ширина / Висота"), expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_sizes_help),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
        )
        dlg.content = ft.Container(
            width=420,
            content=ft.Column(
                [
                    ft.Text("Ширини стовпчиків (px)", size=11, color=_MUTED),
                    ft.Row([left_name_f, archive_f], wrap=True, spacing=8),
                    ft.Row([month_f], wrap=True, spacing=8),
                    ft.Divider(height=8),
                    ft.Row([row_h_f, card_h_f], wrap=True, spacing=8),
                    ft.Row([txt_size_f], wrap=True, spacing=8),
                    wrap_cb,
                ],
                spacing=8,
            ),
        )
        dlg.actions = [
            ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_close),
            ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save),
        ]
        dlg.actions_alignment = ft.MainAxisAlignment.END
        page.show_dialog(dlg)

    def _pg(self):
        if not self.content_area:
            return None
        try:
            return self.content_area.page
        except RuntimeError:
            return None

    def _show_help(self) -> None:
        from views.help_utils import show_modal_help
        page = self._pg()
        show_modal_help(page, self.t("schedule.help.title", "Розклад Замовлень"), [
            self.t("schedule.help.desc", "Графік закупівель: плануйте обсяги і бюджети по місяцях для кожного товару."),
            (self.t("schedule.help.steps_title", "Як користуватись"), None),
            self.t("schedule.help.step1", "1. Рядки — товари, стовпці — місяці."),
            self.t("schedule.help.step2", "2. Клікніть на клітинку, щоб внести плановий обсяг замовлення."),
            self.t("schedule.help.step3", "3. «Бюджет» — перегляньте плановий бюджет по місяцях і категоріях."),
            self.t("schedule.help.step4", "4. «Таблиця» — налаштуйте видимі стовпці та їх розміри."),
            self.t("schedule.help.step5", "5. «+Замітка» — додайте планову позицію або нотатку в розклад."),
            self.t("schedule.help.step6", "6. Можна об'єднувати і розділяти рядки товарів."),
            self.t("schedule.help.step7", "7. Архівні місяці зберігаються для перегляду в колонці «Архів»."),
        ])

    def _snack(self, text: str, color: str = _ACCENT):
        page = self._pg()
        if not page:
            return
        try:
            page.snack_bar = ft.SnackBar(
                content=ft.Text(text, color="white"), bgcolor=color, duration=3000
            )
            page.snack_bar.open = True
            page.update()
        except Exception:
            pass

    def _persist_task_async(
        self, payload: dict, *, reload_on_error: bool = True
    ) -> None:
        snapshot = dict(payload or {})
        if not snapshot:
            return
        tid = str(snapshot.get("id") or "").strip()
        if not tid:
            return
        self._ops_outbox[f"task:{tid}"] = {
            "kind": "task",
            "data": snapshot,
        }
        self._save_ops_outbox()
        self._refresh_workspace_sync_indicator_state()
        # Same reasoning as _toggle_task_status: a status edit here also
        # flips whether the notebook's linked reminder counts as done, so
        # flush now and refresh the toolbar badge instead of waiting for the
        # 45s outbox loop / a notebook open-close cycle.
        pg = self._pg()
        if pg and hasattr(pg, "run_task"):
            with contextlib.suppress(Exception):
                pg.run_task(self._sync_status_change_and_refresh_badge)

    # ── Scroll tracking helpers ───────────────────────────────────────────────

    def _on_content_area_scroll(self, e) -> None:
        try:
            self._content_area_offset = float(getattr(e, "pixels", 0) or 0)
        except Exception:
            pass

    def _on_v_scroll(self, e) -> None:
        try:
            self._v_offset = float(getattr(e, "pixels", 0) or 0)
        except Exception:
            pass

    def _on_h_scroll(self, e) -> None:
        try:
            offset = float(getattr(e, "pixels", 0) or 0)
            self._h_offset = offset
            self._h_scrollbar_offset = offset
        except Exception:
            return
        item = getattr(self, "_right_header_stack_child", None)
        if item is not None:
            item.left = -offset
            with contextlib.suppress(Exception):
                item.update()

    def _schedule_rebuild(self, *, show_stub: bool = True) -> None:
        """Deferred rebuild — batches rapid changes; runs in a worker thread to avoid UI freeze."""
        if getattr(self, "_rebuild_scheduled", False):
            return
        self._rebuild_scheduled = True
        pg = self._pg()
        if pg and hasattr(pg, "run_task"):

            async def _do(_show_stub=show_stub):
                await asyncio.sleep(0.02)
                self._rebuild_scheduled = False
                if _show_stub:
                    with contextlib.suppress(Exception):
                        self._render_loading_stub()
                await asyncio.to_thread(self._rebuild, False)

            pg.run_task(_do)
        else:
            self._rebuild_scheduled = False
            self._rebuild(reload=False)

    def apply_text_search(self, query: str) -> None:
        """Ctrl+F row filter: hide rows whose Назва/План/Факт don't match `query`."""
        self._search_text_query = str(query or "").strip()
        self._schedule_rebuild()

    def clear_text_search(self) -> None:
        self.apply_text_search("")

    def _row_matches_text_search(self, row: dict) -> bool:
        group_ids = [
            str(v).strip()
            for v in (row.get("group_ids") or [row.get("group_id")])
            if str(v).strip()
        ]
        by_group = self._tasks_by_group()
        task_fields: list = []
        for gid in group_ids:
            for task in by_group.get(gid, []):
                task_fields.extend(
                    (
                        task.get("planned_qty"),
                        task.get("planned_amount"),
                        task.get("actual_qty"),
                        task.get("actual_amount"),
                    )
                )
        return row_text_matches(
            self._search_text_query,
            self._group_display_label(row),
            *(row.get("product_names") or []),
            *task_fields,
        )

    def _inplace_rows_update(
        self,
        remove_keys: set,
        new_rows_data: list[dict],
        insert_pos: int,
    ) -> bool:
        """Remove rows by key and insert new rows at insert_pos. Returns True on success.

        Used by merge/split for instant UI update without a full rebuild.
        """
        left_col = self._left_body_col_ref
        right_col = self._right_rows_col_ref
        if (
            left_col is None
            or right_col is None
            or not self._last_built_rows
            or len(self._left_rows_refs) != len(self._last_built_rows)
            or len(self._right_rows_refs) != len(self._last_built_rows)
        ):
            return False

        # Indices to remove (descending to keep positions valid during pop)
        rm_idxs = sorted(
            [
                i
                for i, r in enumerate(self._last_built_rows)
                if self._sel_key(r) in remove_keys
            ],
            reverse=True,
        )
        if not rm_idxs and not new_rows_data:
            return True  # nothing to do
        if not rm_idxs and remove_keys:
            return False  # keys requested but not found

        # Remove from all parallel lists (highest idx first)
        for ri in rm_idxs:
            if ri < len(left_col.controls):
                left_col.controls.pop(ri)
            if ri < len(right_col.controls):
                right_col.controls.pop(ri)
            if ri < len(self._last_built_rows):
                self._last_built_rows.pop(ri)
            if ri < len(self._left_rows_refs):
                self._left_rows_refs.pop(ri)
            if ri < len(self._right_rows_refs):
                self._right_rows_refs.pop(ri)

        # Build and insert new rows at insert_pos
        visible_months = self._visible_month_anchors()
        show_archive = bool(self._show_archive_column)
        for offset, new_row in enumerate(new_rows_data):
            pos = min(insert_pos + offset, len(self._last_built_rows))
            row_h = self._row_height(new_row)
            new_left = self._left_row(new_row, pos, row_h)
            right_cells = []
            for cid in self._ordered_schedule_right_columns(show_archive):
                if cid == "archive" and show_archive:
                    right_cells.append(self._archive_cell(new_row, row_h))
                elif cid == "months":
                    right_cells.extend(
                        [self._month_cell(new_row, m, row_h) for m in visible_months]
                    )
            new_right = ft.Container(
                height=row_h,
                bgcolor=_ALT if pos % 2 else _PANEL,
                content=ft.Row(right_cells, spacing=0),
            )
            left_col.controls.insert(pos, new_left)
            right_col.controls.insert(pos, new_right)
            self._last_built_rows.insert(pos, new_row)
            self._left_rows_refs.insert(pos, new_left)
            self._right_rows_refs.insert(pos, new_right)

        with contextlib.suppress(Exception):
            left_col.update()
        with contextlib.suppress(Exception):
            right_col.update()
        return True

    def _inject_pinned_h_scrollbar(self, left_w: int, right_inner_w: int) -> None:
        """Kept for API compatibility; top scrollbar is now embedded in the layout."""

    def _on_pinned_h_scroll(self, e) -> None:
        """User dragged the top scrollbar → drive body + header."""
        offset = float(getattr(e, "pixels", 0) or 0)
        self._h_offset = offset
        self._h_scrollbar_offset = offset
        if getattr(self, "_h_sync_pending", False):
            return
        self._h_sync_pending = True
        pg = self._pg()
        if pg and hasattr(pg, "run_task"):
            pg.run_task(self._sync_top_scroll_to_body)

    async def _sync_top_scroll_to_body(self) -> None:
        """Top scrollbar moved → drive body + header (not the top rail, to avoid loop)."""
        self._h_sync_pending = False
        offset = getattr(self, "_h_scrollbar_offset", 0.0)
        if self._right_body_scroll is not None:
            with contextlib.suppress(Exception):
                await self._right_body_scroll.scroll_to(offset=offset, duration=0)
        if self._right_header_scroll is not None:
            with contextlib.suppress(Exception):
                await self._right_header_scroll.scroll_to(offset=offset, duration=0)

    async def _sync_h_scroll_coalesced(self) -> None:
        """Body scrolled natively → drive header + top scrollbar (not body, to avoid loop)."""
        self._h_sync_pending = False
        offset = getattr(self, "_h_scrollbar_offset", 0.0)
        if self._right_header_scroll is not None:
            with contextlib.suppress(Exception):
                await self._right_header_scroll.scroll_to(offset=offset, duration=0)
        if getattr(self, "_top_h_scrollbar", None) is not None:
            with contextlib.suppress(Exception):
                await self._top_h_scrollbar.scroll_to(offset=offset, duration=0)

    def _row_for_group_id(self, gid: str) -> dict | None:
        """Return a minimal row dict for a group, looked up from self._groups."""
        gid = str(gid or "").strip()
        if not gid:
            return None
        for g in self._groups or []:
            if not isinstance(g, dict):
                continue
            if str(g.get("id") or "").strip() == gid:
                result = dict(g)
                result["group_id"] = gid
                return result
        return {"group_id": gid}

    def _is_group_owned(self, row: dict) -> bool:
        """Return True when the current manager owns this schedule group."""
        gid = str(row.get("group_id") or "").strip()
        if not gid or bool(row.get("virtual")):
            return True  # virtual / unsaved row → allow edit
        return gid in self._owned_group_ids

    def _restore_scroll_after_rebuild(self) -> None:
        """Schedule async scroll restoration after the next page.update()."""
        sv, sh = self._v_offset, self._h_offset
        sc = float(getattr(self, "_content_area_offset", 0.0) or 0.0)
        if sv <= 0 and sh <= 0 and sc <= 0:
            return
        pg = self._pg()
        if not pg or not hasattr(pg, "run_task"):
            return
        body_col = self._v_scroll_col

        async def _restore():
            await asyncio.sleep(0.06)
            try:
                if sc > 0:
                    await self.content_area.scroll_to(offset=sc, duration=0)
            except Exception:
                pass
            try:
                if body_col is not None and sv > 0:
                    await body_col.scroll_to(offset=sv, duration=0)
            except Exception:
                pass
            try:
                if self._right_body_scroll is not None and sh > 0:
                    await self._right_body_scroll.scroll_to(offset=sh, duration=0)
            except Exception:
                pass
            # Restore header position via Stack left offset (synchronous)
            hsc = getattr(self, "_right_header_stack_child", None)
            if hsc is not None and sh > 0:
                hsc.left = -sh
                with contextlib.suppress(Exception):
                    hsc.update()

        try:
            pg.run_task(_restore)
        except Exception:
            pass

    def _resolve_region_id(self) -> str | None:
        name = self.active_region
        if not name:
            return None
        regions = self.db.get_company_regions(self.company_id) or []
        target = name.strip().lower()
        for row in regions:
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id") or "").strip()
            rname = str(row.get("name") or "").strip().lower()
            if rid == name or rname == target:
                return rid or None
        return None

    def _load(self):
        self._region_id = self._resolve_region_id()
        if self._region_id and not self.read_only:
            try:
                if not self.db.manager_has_category_access_in_region(
                    self.manager_id, self.company_id, self._region_id
                ):
                    self.read_only = True
            except Exception:
                pass
        _raw_products = (
            self.db.get_products(
                self.company_id,
                region_id=self._region_id,
                manager_id=self.manager_id,
            )
            or []
        )
        # Deduplicate by (name, category_id, supplier_id) — only collapse rows that
        # are truly the same product (same name + category + supplier).
        _prod_dedup: dict[tuple, dict] = {}
        for _p in _raw_products:
            if not isinstance(_p, dict):
                continue
            _key = (
                str(_p.get("name") or "").strip().lower(),
                str(_p.get("category_id") or "").strip(),
                str(_p.get("supplier_id") or "").strip(),
            )
            _ex = _prod_dedup.get(_key)
            if _ex is None or str(_p.get("updated_at") or "") > str(_ex.get("updated_at") or ""):
                _prod_dedup[_key] = _p
        self._products = list(_prod_dedup.values())
        self._product_disambiguators_cache = None
        self._set_loading_text(f"Товари: {len(self._products)}")

        # ── Cache-first for tasks + groups ────────────────────────────────────
        db_pulse = self._schedule_scope_pulse()
        cached_pulse = self._load_schedule_cache()
        if cached_pulse is None or db_pulse > cached_pulse:
            self._groups = (
                self.db.get_order_schedule_groups(
                    self.company_id,
                    manager_id=self.manager_id,
                    region_id=self._region_id,
                )
                or []
            )
            self._tasks = (
                self.db.get_order_schedule_tasks(
                    self.company_id,
                    manager_id=self.manager_id,
                    region_id=self._region_id,
                    include_archived=True,
                )
                or []
            )
            self._save_schedule_cache(db_pulse)
        # else: self._tasks / self._groups populated from cache
        self._set_loading_text(f"Задачі: {len(self._tasks or [])}")

        # Re-apply any pending (un-flushed) outbox items.
        if self._ops_outbox:
            self._apply_outbox_to_schedule()
        # Auto-remove groups whose product_ids are all orphaned (product deleted in Товари).
        self._cleanup_orphaned_groups()
        self._categories = (
            self.db.get_company_categories(
                self.company_id,
                self._region_id,
                manager_id=self.manager_id,
            )
            or []
        )
        self._set_loading_text("Категорії...")
        try:
            self._ref_data_pulse_ts = self.db.get_reference_data_pulse(self.company_id)
        except Exception:
            pass
        self._delivery_methods = (
            self.db.get_effective_delivery_methods_for_manager(
                self.company_id,
                self.manager_id,
                region_id=self._region_id,
            )
            or []
        )
        self._set_loading_text("Довідники...")
        self._load_table_layout_state()
        self._load_visible_columns_state()
        try:
            cfg = (
                self.db.get_company_catalog_config(self.company_id, self._region_id)
                or {}
            )
            levels = cfg.get("levels") if isinstance(cfg, dict) else []
            enabled = []
            for row in levels or []:
                if not isinstance(row, dict):
                    continue
                try:
                    pos = int(row.get("position") or 0)
                except Exception:
                    continue
                if 1 <= pos <= 6 and bool(row.get("enabled")):
                    enabled.append(
                        {
                            "position": pos,
                            "title": str(row.get("title") or f"Рівень {pos}").strip()
                            or f"Рівень {pos}",
                        }
                    )
            enabled.sort(key=lambda x: int(x.get("position") or 0))
            self._enabled_levels = enabled
        except Exception:
            self._enabled_levels = []
        try:
            unique_titles: dict[str, tuple[str, str, int]] = {}
            self._category_level_pos_by_title = {}
            self._title_default_pos = {}
            for cat in self._categories or []:
                if not isinstance(cat, dict):
                    continue
                cat_id = str(cat.get("id") or "").strip()
                if not cat_id:
                    continue
                cat_pos_by_title: dict[str, int] = {}
                cat_cfg = self.db.get_category_levels_config(cat) or {}
                cat_levels = cat_cfg.get("levels") if isinstance(cat_cfg, dict) else []
                for row in cat_levels or []:
                    if not isinstance(row, dict) or not bool(row.get("enabled")):
                        continue
                    try:
                        pos = int(row.get("position") or 0)
                    except Exception:
                        pos = 0
                    if pos < 1 or pos > 6:
                        continue
                    title = (
                        str(row.get("title") or f"Рівень {pos}").strip()
                        or f"Рівень {pos}"
                    )
                    title_key = title.lower()
                    if title_key not in cat_pos_by_title:
                        cat_pos_by_title[title_key] = pos
                    if title_key not in self._title_default_pos:
                        self._title_default_pos[title_key] = pos
                    # No duplicate titles in the dropdown.
                    if title_key not in unique_titles:
                        unique_titles[title_key] = (
                            f"level-title:{title_key}",
                            title,
                            pos,
                        )
                if cat_pos_by_title:
                    self._category_level_pos_by_title[cat_id] = cat_pos_by_title
            if not unique_titles:
                for row in self._enabled_levels or []:
                    try:
                        pos = int(row.get("position") or 0)
                    except Exception:
                        pos = 0
                    if pos < 1 or pos > 6:
                        continue
                    title = (
                        str(row.get("title") or f"Рівень {pos}").strip()
                        or f"Рівень {pos}"
                    )
                    title_key = title.lower()
                    if title_key not in self._title_default_pos:
                        self._title_default_pos[title_key] = pos
                    if title_key not in unique_titles:
                        unique_titles[title_key] = (
                            f"level-title:{title_key}",
                            title,
                            pos,
                        )
            self._level_mode_options = [
                (mode, title)
                for mode, title, _pos in sorted(
                    unique_titles.values(),
                    key=lambda item: (item[2], item[1].lower()),
                )
            ]
        except Exception:
            self._level_mode_options = []
            self._category_level_pos_by_title = {}
            self._title_default_pos = {}
        try:
            nodes = self.db.get_company_catalog_nodes(self.company_id) or []
            self._node_names = {
                str(n.get("id") or "").strip(): str(n.get("name") or "").strip()
                for n in nodes
                if isinstance(n, dict) and (n.get("id") or "")
            }
            self._node_rows = {
                str(n.get("id") or "").strip(): n
                for n in nodes
                if isinstance(n, dict) and (n.get("id") or "")
            }
        except Exception:
            self._node_names = {}
            self._node_rows = {}
        self._hydrate_product_level_ids()
        try:
            pref_mode = self.db.get_manager_ui_preference(
                self.manager_id,
                "order_schedule_display_mode",
                "",
            )
            if str(pref_mode or "").strip():
                self._display_mode = self._normalize_display_mode(pref_mode)
        except Exception:
            pass
        try:
            self._owned_group_ids = {
                str(x)
                for x in (
                    self.db.get_entity_owner_entity_ids(
                        self.company_id, self.manager_id, "schedule_group"
                    )
                    or []
                )
                if str(x).strip()
            }
        except Exception:
            self._owned_group_ids = set()
        self._initial_loaded = True

    def _hydrate_product_level_ids(self):
        if not self._products or not self._node_rows:
            return
        for product in self._products:
            if not isinstance(product, dict):
                continue
            leaf_id = str(product.get("catalog_node_id") or "").strip()
            if not leaf_id:
                continue
            current = leaf_id
            hops = 0
            while current and hops < 40:
                row = self._node_rows.get(current)
                if not isinstance(row, dict):
                    break
                node_type = str(row.get("node_type") or "").strip().lower()
                if node_type.startswith("level_"):
                    try:
                        pos = int(node_type.split("_", 1)[1])
                    except Exception:
                        pos = 0
                    if 1 <= pos <= 6:
                        level_key = f"level_{pos}_node_id"
                        if not str(product.get(level_key) or "").strip():
                            product[level_key] = str(row.get("id") or "").strip()
                current = str(row.get("parent_id") or "").strip()
                hops += 1
        # Build a set of "full keys" for each product so _build_rows can skip
        # visually-identical rows without removing products from self._products.
        _seen_full_product_keys: set[tuple] = set()
        _product_full_key_map: dict[str, tuple] = {}
        for _p in self._products:
            if not isinstance(_p, dict):
                continue
            _pid = str(_p.get("id") or "").strip()
            if not _pid:
                continue
            _fk = (
                str(_p.get("name") or "").strip().lower(),
                str(_p.get("category_id") or "").strip(),
                str(_p.get("level_1_node_id") or "").strip(),
                str(_p.get("level_2_node_id") or "").strip(),
                str(_p.get("level_3_node_id") or "").strip(),
                str(_p.get("level_4_node_id") or "").strip(),
                str(_p.get("level_5_node_id") or "").strip(),
                str(_p.get("level_6_node_id") or "").strip(),
            )
            _product_full_key_map[_pid] = _fk
        self._product_full_key_map: dict[str, tuple] = _product_full_key_map

    def _products_by_id(self) -> dict[str, dict]:
        return {
            str((p or {}).get("id") or "").strip(): p
            for p in (self._products or [])
            if isinstance(p, dict) and (p.get("id") or "")
        }

    def _categories_by_id(self) -> dict[str, str]:
        return {
            str((c or {}).get("id") or "").strip(): str(c.get("name") or "").strip()
            for c in (self._categories or [])
            if isinstance(c, dict) and (c.get("id") or "")
        }

    def _product_disambiguators(self) -> dict[str, str]:
        """Returns {product_id → suffix} — " | Кат: ... | Постач: ..." for
        products sharing a name with a different category and/or supplier.
        See views/product_naming.py for the shared rule. Cached on self and
        invalidated whenever _load() refreshes self._products — this is called
        once per product row when building the grouped-by-name schedule table,
        so recomputing the full-catalog scan every time would be O(n^2)."""
        cached = getattr(self, "_product_disambiguators_cache", None)
        if cached is not None:
            return cached

        cat_by_id = {
            str((c or {}).get("id") or "").strip(): c
            for c in (self._categories or [])
            if isinstance(c, dict) and (c.get("id") or "")
        }
        if not hasattr(self, "_node_path_cache"):
            self._node_path_cache = {}
        _node_path_of = product_naming.make_node_path_resolver(
            self.db, self._node_path_cache
        )

        def _category_of(p: dict) -> str:
            # Category via the catalog-tree root, not just category_id — some
            # products are classified only through the tree and never get a
            # flat category_id written.
            return product_naming.resolve_category_label(
                p, node_path_of=_node_path_of, category_by_id=cat_by_id
            )

        result = product_naming.compute_name_suffixes(
            self._products or [],
            category_of=_category_of,
            supplier_of=product_naming.resolve_supplier_label,
        )
        self._product_disambiguators_cache = result
        return result

    def _product_display_name(
        self, product: dict, disambiguators: dict | None = None
    ) -> str:
        prod = product if isinstance(product, dict) else {}
        name = str(prod.get("name") or "").strip() or "-"
        _dis = (
            disambiguators
            if isinstance(disambiguators, dict)
            else self._product_disambiguators()
        )
        pid = str(prod.get("id") or "").strip()
        return name + (_dis.get(pid, "") if pid else "")

    def _month_anchors(self) -> list[date]:
        start = _month_start()
        return [_add_months(start, idx) for idx in range(self._visible_months_count)]

    def _pinned_archive_month(self):
        if not self._pin_last_archive_month:
            return None
        return _add_months(_month_start(), -1)

    def _opened_archive_months(self) -> list[date]:
        out = []
        current_start = _month_start()
        for raw in self._opened_archive_month_keys:
            parsed = _parse_date(raw)
            if not parsed:
                continue
            parsed = _month_start(parsed)
            if parsed > current_start:
                continue
            out.append(parsed)
        unique = sorted({_month_key(v): v for v in out}.values())
        return unique

    def _visible_month_anchors(self) -> list[date]:
        seen: set = set()
        out: list = []
        # Opened archive months (sorted oldest first)
        for m in self._opened_archive_months():
            key = _month_key(m)
            if key not in seen:
                seen.add(key)
                out.append(m)
        # Pinned previous month
        pinned = self._pinned_archive_month()
        if pinned is not None:
            key = _month_key(pinned)
            if key not in seen:
                seen.add(key)
                out.append(pinned)
        # Current + future months
        for m in self._month_anchors():
            key = _month_key(m)
            if key not in seen:
                seen.add(key)
                out.append(m)
        return out

    def _is_archive_month_opened(self, month_anchor: date) -> bool:
        key = _month_key(_month_start(month_anchor))
        return key in {_month_key(v) for v in self._opened_archive_months()}

    def _display_options(self) -> list[tuple[str, str]]:
        out = [("name", "Назва"), ("category", "Категорія")]
        out.extend(self._level_mode_options or [])
        return out

    def _normalize_display_mode(self, mode: str) -> str:
        raw = str(mode or "").strip()
        options = self._display_options()
        key_set = {str(k).strip() for k, _ in options}
        if raw in key_set:
            return raw
        if raw.startswith("level:"):
            legacy_tail = raw.split(":", 1)[1]
            if ":" in legacy_tail:
                legacy_title_key = legacy_tail.split(":", 1)[1].strip().lower()
                candidate = f"level-title:{legacy_title_key}"
                if candidate in key_set:
                    return candidate
        raw_l = raw.lower()
        for key, label in options:
            if raw_l and raw_l == str(label or "").strip().lower():
                return str(key).strip() or "name"
        return "name"

    def _level_position_from_mode(self, mode: str) -> int:
        txt = str(mode or "").strip()
        if not txt.startswith("level:"):
            return 0
        tail = txt.split(":", 1)[1]
        pos_txt = tail.split(":", 1)[0]
        try:
            pos = int(pos_txt)
        except Exception:
            pos = 0
        return pos if 1 <= pos <= 6 else 0

    def _level_title_from_mode(self, mode: str) -> str:
        mode_s = str(mode or "").strip()
        for key, label in self._level_mode_options or []:
            if str(key or "").strip() == mode_s:
                return str(label or "").strip() or "Рівень"
        if mode_s.startswith("level-title:"):
            title_key = mode_s.split(":", 1)[1].strip()
            if title_key:
                return title_key.capitalize()
        pos = self._level_position_from_mode(mode_s)
        if not pos:
            return "Рівень"
        for row in self._enabled_levels or []:
            try:
                if int(row.get("position") or 0) == pos:
                    return (
                        str(row.get("title") or f"Рівень {pos}").strip()
                        or f"Рівень {pos}"
                    )
            except Exception:
                continue
        return f"Рівень {pos}"

    def _resolve_level_node_id_for_product(self, product: dict, pos: int) -> str:
        if not isinstance(product, dict) or pos < 1 or pos > 6:
            return ""

        def _is_target_level(node_id: str) -> bool:
            row = (
                self._node_rows.get(node_id)
                if isinstance(self._node_rows, dict)
                else None
            )
            node_type = str((row or {}).get("node_type") or "").strip().lower()
            return node_type == f"level_{pos}"

        direct = str(product.get(f"level_{pos}_node_id") or "").strip()
        if direct and _is_target_level(direct):
            return direct

        start_ids = []
        if direct:
            start_ids.append(direct)
        leaf = str(product.get("catalog_node_id") or "").strip()
        if leaf and leaf not in start_ids:
            start_ids.append(leaf)

        for start_id in start_ids:
            current = start_id
            hops = 0
            while current and hops < 40:
                row = (
                    self._node_rows.get(current)
                    if isinstance(self._node_rows, dict)
                    else None
                )
                if not isinstance(row, dict):
                    break
                node_type = str(row.get("node_type") or "").strip().lower()
                if node_type == f"level_{pos}":
                    return str(row.get("id") or "").strip()
                current = str(row.get("parent_id") or "").strip()
                hops += 1
        return ""

    def _level_position_for_product(self, product: dict, mode: str) -> int:
        mode_s = self._normalize_display_mode(mode)
        if mode_s.startswith("level-title:"):
            title_key = mode_s.split(":", 1)[1].strip().lower()
            cat_id = str((product or {}).get("category_id") or "").strip()
            if cat_id:
                by_cat = self._category_level_pos_by_title.get(cat_id) or {}
                pos = int(by_cat.get(title_key) or 0)
                if 1 <= pos <= 6:
                    return pos
            # IMPORTANT: do not fallback to a global title->position mapping for
            # products whose category does not define this title. Otherwise,
            # selecting e.g. "Підкатегорія" in one category may accidentally show
            # another title (like "Бренд") from other categories sharing the same
            # position index.
            return 0
        if mode_s.startswith("level:"):
            return self._level_position_from_mode(mode_s)
        return 0

    def _display_value_for_product(self, product: dict, mode: str) -> str:
        mode = self._normalize_display_mode(mode)
        if not isinstance(product, dict):
            return "-"
        if mode == "name":
            return self._product_display_name(product)
        if mode == "category":
            cat_name = self._categories_by_id().get(
                str(product.get("category_id") or "").strip(), ""
            )
            return cat_name or "Без категорії"
        if str(mode or "").startswith("level"):
            pos = self._level_position_for_product(product, mode)
            if not pos:
                return ""
            node_id = self._resolve_level_node_id_for_product(product, pos)
            level_name = self._node_names.get(node_id)
            if level_name:
                return level_name
            return ""
        return str(product.get("name") or "-").strip() or "-"

    def _display_bucket_key_for_product(self, product: dict, mode: str) -> str:
        mode = self._normalize_display_mode(mode)
        if not isinstance(product, dict):
            return "-"
        if mode == "name":
            return str(product.get("id") or "").strip() or (
                str(product.get("name") or "-").strip().lower() or "-"
            )
        if mode == "category":
            cat_id = str(product.get("category_id") or "").strip()
            if cat_id:
                return f"category:{cat_id}"
            return "category:" + (
                self._display_value_for_product(product, mode).strip().lower() or "-"
            )
        if str(mode or "").startswith("level"):
            # For level-title: modes, use the display label as bucket key so that
            # identically-named nodes from different categories (e.g. two "No_Name"
            # brand nodes in different category trees) are merged into a single row.
            if mode.startswith("level-title:"):
                label = self._display_value_for_product(product, mode)
                label_s = str(label or "").strip()
                if not label_s:
                    return ""
                title_key = mode.split(":", 1)[1].strip().lower()
                return f"level-title:{title_key}:{label_s.lower()}"
            pos = self._level_position_for_product(product, mode)
            if not pos:
                return ""
            node_id = self._resolve_level_node_id_for_product(product, pos)
            if node_id:
                return f"level:{pos}:{node_id}"
            return ""
        return self._display_value_for_product(product, mode).strip().lower() or "-"

    def _group_display_label(self, row: dict) -> str:
        override = str(row.get("display_label") or "").strip()
        if override:
            return override
        # In "name" mode, product_names already contains disambiguated names
        # (set at build time via _product_display_name). Use them directly.
        if self._display_mode == "name":
            stored = [
                str(n or "").strip()
                for n in (row.get("product_names") or [])
                if str(n or "").strip()
            ]
            if stored:
                return (
                    stored[0] if len(stored) == 1 else f"{stored[0]} +{len(stored) - 1}"
                )
        products_by_id = self._products_by_id()
        labels = []
        for pid in row.get("product_ids") or []:
            prod = products_by_id.get(str(pid).strip()) or {}
            label = self._display_value_for_product(prod, self._display_mode)
            if label and label not in labels:
                labels.append(label)
        if not labels:
            if self._display_mode == "name":
                labels = [str((row.get("product_names") or ["-"])[0] or "-")]
            elif self._display_mode == "category":
                labels = ["Без категорії"]
            elif self._display_mode.startswith("level"):
                labels = [""]
            else:
                labels = ["-"]
        if len(labels) == 1:
            return labels[0]
        return f"{labels[0]} +{len(labels) - 1}"

    def _filtered_products(self) -> list[dict]:
        out = []
        for product in self._products:
            if not isinstance(product, dict):
                continue
            if self._filter_category_ids:
                if (
                    str(product.get("category_id") or "").strip()
                    not in self._filter_category_ids
                ):
                    continue
            out.append(product)
        return out

    def _build_rows(self) -> list[dict]:
        filtered_products = self._filtered_products()
        filtered_ids = {
            str((p or {}).get("id") or "").strip()
            for p in filtered_products
            if isinstance(p, dict) and (p.get("id") or "")
        }
        rows = []
        aggregate_mode = self._display_mode != "name"
        used_product_ids = set()
        shown_in_group_pids: set[str] = set()  # products already claimed by a group row
        # Track "full keys" to prevent duplicate rows for truly identical products
        # (same name + category + all levels). Products stay in self._products so groups
        # that reference them remain valid — only the second *row* is suppressed.
        shown_full_keys: set[tuple] = set()
        products_by_id = self._products_by_id()
        disambiguators = self._product_disambiguators()
        grouped_rows: dict[str, dict] = {}

        def _merge_into_bucket(seed: dict, label: str, bucket_key: str | None = None):
            key = (
                str(bucket_key or "").strip()
                or label.strip().lower()
                or str(seed.get("key") or uuid.uuid4().hex)
            )
            bucket = grouped_rows.get(key)
            if bucket is None:
                bucket = {
                    "key": str(seed.get("key") or f"agg:{key}"),
                    "group_id": str(seed.get("group_id") or "").strip(),
                    "group_ids": list(
                        seed.get("group_ids")
                        or ([] if not seed.get("group_id") else [seed.get("group_id")])
                    ),
                    "product_ids": [],
                    "product_names": [],
                    "category_id": str(seed.get("category_id") or "").strip(),
                    "virtual": bool(seed.get("virtual", True)),
                    "merged": False,
                    "display_label": label,
                }
                grouped_rows[key] = bucket
            for gid in seed.get("group_ids") or (
                [] if not seed.get("group_id") else [seed.get("group_id")]
            ):
                gid_s = str(gid or "").strip()
                if gid_s and gid_s not in bucket["group_ids"]:
                    bucket["group_ids"].append(gid_s)
            if not bucket.get("group_id") and bucket.get("group_ids"):
                bucket["group_id"] = bucket["group_ids"][0]
            for pid in seed.get("product_ids") or []:
                pid_s = str(pid or "").strip()
                if pid_s and pid_s not in bucket["product_ids"]:
                    bucket["product_ids"].append(pid_s)
            for pname in seed.get("product_names") or []:
                pname_s = str(pname or "").strip()
                if pname_s and pname_s not in bucket["product_names"]:
                    bucket["product_names"].append(pname_s)
            bucket["virtual"] = bool(
                bucket.get("virtual", True) and seed.get("virtual", True)
            )
            bucket["merged"] = len(bucket["product_ids"]) > 1

        for grp in self._groups:
            if not isinstance(grp, dict):
                continue
            is_note = str(grp.get("item_type") or "").strip() == "note"
            product_ids = [
                str(v).strip() for v in (grp.get("product_ids") or []) if str(v).strip()
            ]
            if not product_ids and not is_note:
                continue
            # Note rows: always shown (no product_ids to filter on), but respect category filter
            if is_note:
                note_cat = str(grp.get("category_id") or "").strip()
                if (
                    self._filter_category_ids
                    and note_cat
                    and note_cat not in self._filter_category_ids
                ):
                    continue
            elif filtered_ids and not any(pid in filtered_ids for pid in product_ids):
                continue
            # Dedup: skip group if all its products already shown in another group row.
            if (
                not aggregate_mode
                and product_ids
                and all(pid in shown_in_group_pids for pid in product_ids)
            ):
                continue
            _grp_inactive = bool(grp.get("is_inactive", False))
            _ws_inactive_keys = set(
                self._workspace_state.get("inactive_row_keys") or []
            )
            _grp_key = f"g:{grp.get('id') or ''!s}"
            if is_note:
                # Note row: no real products, just a display_label and optional category/levels
                note_label = str(grp.get("display_label") or "").strip() or "—"
                _note_price = grp.get("price")
                try:
                    _note_price = (
                        float(_note_price) if _note_price is not None else None
                    )
                except (TypeError, ValueError):
                    _note_price = None
                seed = {
                    "key": _grp_key,
                    "group_id": str(grp.get("id") or "").strip(),
                    "group_ids": [str(grp.get("id") or "").strip()],
                    "product_ids": [],
                    "product_names": [note_label],
                    "category_id": str(grp.get("category_id") or "").strip(),
                    "display_label": note_label,
                    "virtual": False,
                    "merged": False,
                    "item_type": "note",
                    "note": str(grp.get("note") or "").strip(),
                    "production_term_days": grp.get("production_term_days"),
                    "price": _note_price,
                    "currency": str(grp.get("currency") or "").strip(),
                    **{
                        f"level_{p}_node_id": str(
                            grp.get(f"level_{p}_node_id") or ""
                        ).strip()
                        for p in range(1, 7)
                    },
                    "is_inactive": _grp_inactive or (_grp_key in _ws_inactive_keys),
                }
                rows.append(seed)
                continue
            product_names = []
            first_category = ""
            for pid in product_ids:
                prod = products_by_id.get(pid) or {}
                if prod and not first_category:
                    first_category = str(prod.get("category_id") or "").strip()
                pname = self._product_display_name(
                    prod, disambiguators
                ) or self._group_display_label(
                    {"product_ids": [pid], "product_names": [pid]}
                )
                product_names.append(pname)
                if pid in filtered_ids:
                    used_product_ids.add(pid)
                shown_in_group_pids.add(pid)
            seed = {
                "key": _grp_key,
                "group_id": str(grp.get("id") or "").strip(),
                "group_ids": [str(grp.get("id") or "").strip()],
                "product_ids": product_ids,
                "product_names": product_names,
                "category_id": first_category
                or str(grp.get("category_id") or "").strip(),
                "virtual": False,
                "merged": len(product_ids) > 1,
                "display_label": str(grp.get("display_label") or "").strip(),
                "is_inactive": _grp_inactive or (_grp_key in _ws_inactive_keys),
            }
            if aggregate_mode:
                for pid in product_ids:
                    prod = products_by_id.get(pid) or {}
                    label = self._display_value_for_product(prod, self._display_mode)
                    if not str(label or "").strip():
                        continue
                    per_seed = {
                        "key": f"g:{grp.get('id') or ''!s}:{pid}",
                        "group_id": str(grp.get("id") or "").strip(),
                        "group_ids": [str(grp.get("id") or "").strip()],
                        "product_ids": [pid],
                        "product_names": [
                            self._product_display_name(prod, disambiguators)
                        ],
                        "category_id": str(prod.get("category_id") or "").strip(),
                        "virtual": False,
                        "merged": False,
                    }
                    _merge_into_bucket(
                        per_seed,
                        label,
                        self._display_bucket_key_for_product(prod, self._display_mode),
                    )
            else:
                # Skip rows that are visually identical to an already-shown row
                # (same name + category + all levels = true duplicate products).
                # Guard: never skip if this group has tasks — that would hide orders.
                if not aggregate_mode and len(product_ids) == 1:
                    _fk = self._product_full_key_map.get(product_ids[0])
                    if _fk and _fk in shown_full_keys:
                        _grp_id_s = str(grp.get("id") or "").strip()
                        _has_tasks = _grp_id_s and any(
                            str((t or {}).get("group_id") or "").strip() == _grp_id_s
                            for t in self._tasks
                        )
                        if not _has_tasks:
                            for pid in product_ids:
                                shown_in_group_pids.add(pid)
                                if pid in filtered_ids:
                                    used_product_ids.add(pid)
                            continue
                    if _fk:
                        shown_full_keys.add(_fk)
                rows.append(seed)
        for prod in filtered_products:
            pid = str(prod.get("id") or "").strip()
            if not pid or pid in used_product_ids:
                continue
            _p_key = f"p:{pid}"
            _ws_inactive_keys_p = set(
                self._workspace_state.get("inactive_row_keys") or []
            )
            seed = {
                "key": _p_key,
                "group_id": "",
                "group_ids": [],
                "product_ids": [pid],
                "product_names": [self._product_display_name(prod, disambiguators)],
                "category_id": str(prod.get("category_id") or "").strip(),
                "virtual": True,
                "merged": False,
                "is_inactive": _p_key in _ws_inactive_keys_p,
            }
            if aggregate_mode:
                label = self._display_value_for_product(prod, self._display_mode)
                bucket_key = self._display_bucket_key_for_product(
                    prod, self._display_mode
                )
                if str(label or "").strip() and str(bucket_key or "").strip():
                    _merge_into_bucket(seed, label, bucket_key)
            else:
                rows.append(seed)
        if aggregate_mode:
            rows = list(grouped_rows.values())
        cat_by_id = self._categories_by_id()
        active_rows = [r for r in rows if not r.get("is_inactive")]
        inactive_rows = [r for r in rows if r.get("is_inactive")]

        if self._col_sort_key and self._col_sort_dir:
            # Column sort overrides category/name sort
            _ck = self._col_sort_key
            _rev = self._col_sort_dir == 2
            if _ck != "name":
                # Precompute latest task date per month for each row
                tasks_by_group = self._tasks_by_group()
                for _r in active_rows + inactive_rows:
                    gids = _r.get("group_ids") or (
                        [_r.get("group_id")] if _r.get("group_id") else []
                    )
                    dbm: dict[str, str] = {}
                    for _gid in gids:
                        for _t in tasks_by_group.get(str(_gid or "").strip(), []):
                            _td = str(
                                _t.get("date_to") or _t.get("month_anchor") or ""
                            )[:10]
                            if len(_td) >= 7:
                                _mk = _td[:7]
                                if _mk not in dbm or _td > dbm[_mk]:
                                    dbm[_mk] = _td
                    _r["_dates_by_month"] = dbm

            _month_prefix = _ck[:7]  # "2026-06" from "2026-06-01"

            def _col_key(r: dict):
                if _ck == "name":
                    lbl = self._group_display_label(r).lower()
                    return lbl  # "" < any string → empty first in asc, last in desc
                # _dates_by_month keyed by "2026-06" prefix
                d = r.get("_dates_by_month", {}).get(_month_prefix, "")
                return d  # "" < any date → empty first in asc, last in desc

            active_rows.sort(key=_col_key, reverse=_rev)
        else:
            # Default sort: by category name, then by display label
            active_rows.sort(
                key=lambda r: (
                    str(
                        cat_by_id.get(str(r.get("category_id") or "").strip()) or ""
                    ).lower(),
                    self._group_display_label(r).lower(),
                )
            )

        if not inactive_rows:
            return active_rows
        header: dict = {
            "key": "__inactive_header__",
            "item_type": "inactive_header",
            "inactive_count": len(inactive_rows),
            "group_id": "",
            "group_ids": [],
            "product_ids": [],
            "product_names": [],
            "display_label": f"Неактивні ({len(inactive_rows)})",
            "category_id": "",
            "virtual": True,
            "merged": False,
            "is_inactive": False,
        }
        if self._inactive_section_collapsed:
            return active_rows + [header]
        return active_rows + [header] + inactive_rows

    def _tasks_by_group(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for task in self._tasks:
            if not isinstance(task, dict):
                continue
            gid = str(task.get("group_id") or "").strip()
            if not gid:
                continue
            out.setdefault(gid, []).append(task)
        return out

    def _task_is_archived(self, task: dict) -> bool:
        if bool(task.get("archived")):
            return True
        today = date.today()
        current_month_start = _month_start(today)
        due_date = _parse_date(task.get("date_to")) or _parse_date(
            task.get("month_anchor")
        )
        month_anchor = _parse_date(task.get("month_anchor")) or current_month_start
        status = str(task.get("status") or "not_ordered").strip()
        # Archive only when the task's month has fully passed (not mid-month)
        if due_date and _month_start(due_date) < current_month_start:
            return True
        if status == "ordered" and month_anchor < current_month_start:
            return True
        return False

    def _task_needs_order_now(self, task: dict) -> bool:
        if not isinstance(task, dict):
            return False
        if bool(task.get("archived")):
            return False
        status = str(task.get("status") or "not_ordered").strip().lower()
        if status == "ordered":
            return False
        today = date.today()
        date_from = _parse_date(task.get("date_from")) or _parse_date(
            task.get("month_anchor")
        )
        date_to = _parse_date(task.get("date_to"))
        if not date_from:
            return False
        if today < date_from:
            return False
        if date_to and today > date_to:
            return False
        return True

    def _task_is_overdue(self, task: dict) -> bool:
        """True only for not-ordered tasks whose due date/month has already passed."""
        if not isinstance(task, dict):
            return False
        status = str(task.get("status") or "not_ordered").strip().lower()
        if status == "ordered":
            return False
        today = date.today()
        due_date = _parse_date(task.get("date_to")) or _parse_date(
            task.get("month_anchor")
        )
        if not due_date:
            return False
        return due_date < today

    def _task_matches_quick_filter(self, task: dict) -> bool:
        mode = str(self._quick_task_filter or "").strip().lower()
        if not mode:
            return True
        if mode == "need_to_order":
            return self._task_needs_order_now(task)
        if mode == "overdue":
            return self._task_is_overdue(task)
        return True

    def _is_overdue_filter(self) -> bool:
        return str(self._quick_task_filter or "").strip().lower() == "overdue"

    def _task_month_key_for_quick_filter(self, task: dict) -> str:
        mode = str(self._quick_task_filter or "").strip().lower()
        if mode == "overdue":
            due = _parse_date(task.get("date_to")) or _parse_date(
                task.get("month_anchor")
            )
            if due:
                return _month_key(_month_start(due))
            return ""
        return str(task.get("month_anchor") or "")[:10]

    def _visible_months_for_quick_filter(self, rows: list[dict]) -> list[date]:
        group_ids = {
            str(v).strip()
            for row in (rows or [])
            for v in (row.get("group_ids") or [row.get("group_id")])
            if str(v).strip()
        }
        if not group_ids:
            return []
        months: dict[str, date] = {}
        by_group = self._tasks_by_group()
        for gid in group_ids:
            for task in by_group.get(gid, []):
                if not self._task_matches_quick_filter(task):
                    continue
                key = self._task_month_key_for_quick_filter(task)
                parsed = _parse_date(key)
                if not parsed:
                    continue
                ms = _month_start(parsed)
                months[_month_key(ms)] = ms
        return sorted(months.values())

    def _row_has_matching_quick_filter(self, row: dict) -> bool:
        group_ids = [
            str(v).strip()
            for v in (row.get("group_ids") or [row.get("group_id")])
            if str(v).strip()
        ]
        if not group_ids:
            return False
        by_group = self._tasks_by_group()
        for gid in group_ids:
            for task in by_group.get(gid, []):
                if self._task_matches_quick_filter(task):
                    return True
        return False

    def _row_tasks_for_month(
        self,
        row: dict,
        month_anchor: date,
        include_archived: bool = False,
    ) -> list[dict]:
        group_ids = [
            str(v).strip()
            for v in (row.get("group_ids") or [row.get("group_id")])
            if str(v).strip()
        ]
        if not group_ids:
            return []
        target = _month_key(month_anchor)
        items = []
        overdue_mode = self._is_overdue_filter()
        for gid in group_ids:
            for task in self._tasks_by_group().get(gid, []):
                month_val = self._task_month_key_for_quick_filter(task)
                if month_val != target:
                    continue
                if (
                    not overdue_mode
                    and self._task_is_archived(task)
                    and not include_archived
                ):
                    continue
                if not self._task_matches_quick_filter(task):
                    continue
                items.append(task)
        items.sort(
            key=lambda t: (
                str(t.get("date_to") or "9999-12-31"),
                int(t.get("sort_index") or 0),
            )
        )
        return items

    def _row_archived_tasks(self, row: dict) -> list[dict]:
        if self._is_overdue_filter():
            # In overdue mode we render overdue cards directly in month columns.
            return []
        group_ids = [
            str(v).strip()
            for v in (row.get("group_ids") or [row.get("group_id")])
            if str(v).strip()
        ]
        if not group_ids:
            return []
        shown_past = {_month_key(m) for m in self._opened_archive_months()}
        items = [
            t
            for gid in group_ids
            for t in self._tasks_by_group().get(gid, [])
            if self._task_is_archived(t)
            and str(t.get("month_anchor") or "")[:10] not in shown_past
        ]
        items.sort(
            key=lambda t: (
                str(t.get("month_anchor") or ""),
                str(t.get("date_to") or ""),
            ),
            reverse=True,
        )
        return items

    def _row_height(self, row: dict) -> int:
        # Grow past the configured base height when the (possibly long,
        # disambiguated "Name | Кат: X | Постач: Y") row label needs more
        # lines than the base height comfortably fits — otherwise it clips.
        try:
            label = self._group_display_label(row)
        except Exception:
            return _ROW_BASE_H
        if not label:
            return _ROW_BASE_H
        width = max(40, _LEFT_NAME_W - 26)
        wrapped = _wrap_text_for_width(label, width)
        needed_lines = max(1, wrapped.count("\n") + 1)
        txt_size = int(max(6, min(32, int(self._table_layout.get("text_size") or 10))))
        needed_h = needed_lines * (txt_size + 6) + 40  # + fixed chrome (buttons/padding)
        return int(max(_ROW_BASE_H, min(400, needed_h)))

    def _col_sort_cycle(self, key: str, _e=None) -> None:
        if self._col_sort_key != key:
            self._col_sort_key = key
            self._col_sort_dir = 1
        elif self._col_sort_dir == 1:
            self._col_sort_dir = 2
        elif self._col_sort_dir == 2:
            self._col_sort_key = ""
            self._col_sort_dir = 0
        self._schedule_rebuild()

    def _toggle_inactive_section(self, _=None) -> None:
        self._inactive_section_collapsed = not self._inactive_section_collapsed
        left_col = self._left_body_col_ref
        right_col = self._right_rows_col_ref
        if (
            left_col is None
            or right_col is None
            or not self._last_built_rows
            or len(self._left_rows_refs) != len(self._last_built_rows)
            or len(self._right_rows_refs) != len(self._last_built_rows)
        ):
            self._schedule_rebuild(show_stub=False)
            return
        header_idx = next(
            (
                i
                for i, r in enumerate(self._last_built_rows)
                if r.get("key") == "__inactive_header__"
            ),
            None,
        )
        if header_idx is None:
            self._schedule_rebuild(show_stub=False)
            return
        # Rebuild header widget to flip the icon
        hdr_row = self._last_built_rows[header_idx]
        new_hdr = self._left_row(hdr_row, header_idx, self._row_height(hdr_row))
        left_col.controls[header_idx] = new_hdr
        self._left_rows_refs[header_idx] = new_hdr
        if self._inactive_section_collapsed:
            # Remove every row after the header (all inactive)
            while len(self._last_built_rows) > header_idx + 1:
                if header_idx + 1 < len(left_col.controls):
                    left_col.controls.pop(header_idx + 1)
                if header_idx + 1 < len(right_col.controls):
                    right_col.controls.pop(header_idx + 1)
                if header_idx + 1 < len(self._last_built_rows):
                    self._last_built_rows.pop(header_idx + 1)
                if header_idx + 1 < len(self._left_rows_refs):
                    self._left_rows_refs.pop(header_idx + 1)
                if header_idx + 1 < len(self._right_rows_refs):
                    self._right_rows_refs.pop(header_idx + 1)
        else:
            # Build inactive rows by getting the full list and slicing after header
            all_rows = self._build_rows()
            inactive_rows = all_rows[header_idx + 1 :]
            visible_months = self._visible_month_anchors()
            show_archive = bool(self._show_archive_column)
            for offset, new_row in enumerate(inactive_rows):
                pos = header_idx + 1 + offset
                row_h = self._row_height(new_row)
                new_left = self._left_row(new_row, pos, row_h)
                right_cells: list = []
                for cid in self._ordered_schedule_right_columns(show_archive):
                    if cid == "archive" and show_archive:
                        right_cells.append(self._archive_cell(new_row, row_h))
                    elif cid == "months":
                        right_cells.extend(
                            [
                                self._month_cell(new_row, m, row_h)
                                for m in visible_months
                            ]
                        )
                new_right = ft.Container(
                    height=row_h,
                    bgcolor=_ALT if pos % 2 else _PANEL,
                    content=ft.Row(right_cells, spacing=0),
                )
                left_col.controls.insert(pos, new_left)
                right_col.controls.insert(pos, new_right)
                self._last_built_rows.insert(pos, new_row)
                self._left_rows_refs.insert(pos, new_left)
                self._right_rows_refs.insert(pos, new_right)
        with contextlib.suppress(Exception):
            left_col.update()
        with contextlib.suppress(Exception):
            right_col.update()

    def _sel_key(self, row: dict) -> str:
        return str(row.get("key") or "")

    def _is_row_inactive(self, row: dict) -> bool:
        if bool(row.get("is_inactive")):
            return True
        keys = set(self._workspace_state.get("inactive_row_keys") or [])
        return self._sel_key(row) in keys

    def _set_row_inactive(self, row: dict, new_val: bool) -> None:
        """Persist inactive state to group record, workspace state, snapshot, and outbox."""
        row["is_inactive"] = new_val
        key = self._sel_key(row)
        keys = set(self._workspace_state.get("inactive_row_keys") or [])
        if new_val:
            keys.add(key)
        else:
            keys.discard(key)
        self._workspace_state["inactive_row_keys"] = list(keys)
        self._save_workspace_state_to_file()
        # Also persist in group record (snapshot + DB via outbox)
        gid = str(row.get("group_id") or "").strip()
        if gid:
            for g in self._groups:
                if isinstance(g, dict) and str(g.get("id") or "").strip() == gid:
                    g["is_inactive"] = new_val
                    break
            self._save_group(row)
        elif bool(row.get("virtual")) and new_val:
            # Virtual row: create a group so inactive state persists in snapshot
            self._save_group(row)
        self._save_schedule_cache(self._schedule_cache_pulse)
        pg = self._pg()
        if pg and hasattr(pg, "run_task"):
            pg.run_task(self._flush_workspace_state_async)

    def _toggle_row_inactive(self, row: dict) -> None:
        if self.read_only:
            self._snack(self.t("schedule.snack.readonly_mode", "У режимі перегляду редагування недоступне"), _WARN_FG)
            return
        new_val = not self._is_row_inactive(row)
        self._set_row_inactive(row, new_val)
        if new_val:
            self._snack(self.t("schedule.snack.product_deactivated", "Товар переміщено до неактивних"), _ERR_FG)
        else:
            self._snack(self.t("schedule.snack.product_reactivated", "Товар знову активний"), _OK_FG)
        self._schedule_rebuild()

    def _copy_to_clipboard(self, text: str):
        page = self._pg()
        if not page:
            return
        try:
            page.set_clipboard(text or "")
            self._snack(self.t("media.copied", "Скопійовано"), "#0F172A")
        except Exception:
            pass

    def _save_group(self, row: dict, *, forced_group_id: str | None = None) -> str:
        if self.read_only:
            return ""
        gid = (
            forced_group_id
            or str(row.get("group_id") or "").strip()
            or next(
                (
                    str(v).strip()
                    for v in (row.get("group_ids") or [])
                    if str(v).strip()
                ),
                "",
            )
        )
        # If still no gid, check if an existing group already owns these products
        # to avoid creating duplicate groups for the same product on repeated calls.
        if not gid and not forced_group_id:
            row_pids = {
                str(p).strip() for p in (row.get("product_ids") or []) if str(p).strip()
            }
            if row_pids:
                for g in self._groups:
                    if not isinstance(g, dict):
                        continue
                    g_pids = {
                        str(p).strip()
                        for p in (g.get("product_ids") or [])
                        if str(p).strip()
                    }
                    if g_pids and g_pids == row_pids:
                        gid = str(g.get("id") or "").strip()
                        if gid:
                            row["group_id"] = gid
                            row["virtual"] = False
                            break
        if not gid:
            gid = uuid.uuid4().hex
        _item_type = str(row.get("item_type") or "").strip() or "product"
        payload = {
            "id": gid,
            "company_id": self.company_id,
            "manager_id": self.manager_id,
            "region_id": self._region_id or "",
            "category_id": str(row.get("category_id") or "").strip(),
            "item_type": _item_type,
            "display_mode": "name" if _item_type == "note" else self._display_mode,
            "display_label": str(row.get("display_label") or "").strip(),
            "is_inactive": bool(row.get("is_inactive", False)),
            "product_ids": list(row.get("product_ids") or []),
            "product_names": list(row.get("product_names") or []),
            "note": str(row.get("note") or "").strip(),
            "level_node_ids": {},
        }
        self._ops_outbox[f"group:{gid}"] = {
            "kind": "group",
            "data": payload,
        }
        # Keep self._groups in sync so _rebuild(reload=False) sees the new group
        for i, g in enumerate(self._groups):
            if isinstance(g, dict) and str(g.get("id") or "").strip() == gid:
                self._groups[i] = {**g, **payload}
                break
        else:
            self._groups.append(dict(payload))
        self._owned_group_ids.add(gid)
        self._save_ops_outbox()
        self._refresh_workspace_sync_indicator_state()
        row["group_id"] = gid
        row["virtual"] = False
        return gid

    def _merge_selected(self, _=None):
        if self.read_only:
            self._snack(self.t("schedule.snack.readonly_mode", "У режимі перегляду редагування недоступне"), _WARN_FG)
            return
        # Use cached rows — avoids blocking _build_rows() call on UI thread.
        all_rows = self._last_built_rows or []
        rows = [r for r in all_rows if self._sel_key(r) in self._selected]
        if len(rows) < 2:
            return
        old_keys = {self._sel_key(r) for r in rows}
        insert_pos = next(
            (i for i, r in enumerate(all_rows) if self._sel_key(r) in old_keys), 0
        )
        merged_ids = []
        merged_names = []
        category_id = ""
        migrated_tasks = []
        new_gid = uuid.uuid4().hex
        tasks_by_grp = self._tasks_by_group()
        for row in rows:
            for pid, pname in zip(
                row.get("product_ids") or [], row.get("product_names") or []
            ):
                pid_s = str(pid).strip()
                if not pid_s or pid_s in merged_ids:
                    continue
                merged_ids.append(pid_s)
                merged_names.append(str(pname or "-").strip() or "-")
            if not category_id:
                category_id = str(row.get("category_id") or "").strip()
            gid = str(row.get("group_id") or "").strip()
            if gid:
                migrated_tasks.extend([dict(t) for t in tasks_by_grp.get(gid, [])])
        new_row = {
            "key": f"g:{new_gid}",
            "group_id": new_gid,
            "group_ids": [new_gid],
            "product_ids": merged_ids,
            "product_names": merged_names,
            "category_id": category_id,
            "virtual": False,
            "merged": len(merged_ids) > 1,
        }
        gid = self._save_group(new_row, forced_group_id=new_gid)
        if not gid:
            self._snack(self.t("schedule.snack.merge_failed", "Не вдалося об'єднати рядки"), _ERR_FG)
            return
        # Update self._tasks in-memory, queue to outbox
        for task in migrated_tasks:
            task["group_id"] = gid
            task["id"] = task.get("id") or uuid.uuid4().hex
            task["company_id"] = self.company_id
            task["manager_id"] = self.manager_id
            task["region_id"] = self._region_id or ""
            tid = str(task["id"])
            for i, t in enumerate(self._tasks):
                if str((t or {}).get("id") or "").strip() == tid:
                    self._tasks[i] = dict(task)
                    break
            else:
                self._tasks.append(dict(task))
            self._ops_outbox[f"task:{tid}"] = {"kind": "task", "data": dict(task)}
        old_gids = {
            str(r.get("group_id") or "").strip()
            for r in rows
            if r.get("group_id") and str(r.get("group_id")) != gid
        }
        if old_gids:
            self._groups = [
                g
                for g in self._groups
                if str((g or {}).get("id") or "").strip() not in old_gids
            ]
            for old_gid in old_gids:
                self._ops_outbox[f"del_group:{old_gid}"] = {
                    "kind": "del_group",
                    "data": {"id": old_gid},
                }
                self._ops_outbox.pop(f"group:{old_gid}", None)
        self._save_ops_outbox()
        self._refresh_workspace_sync_indicator_state()
        self._selected.clear()
        self._snack(self.t("schedule.snack.merged", "Рядки об'єднано"), _OK_FG)
        # Instant in-place UI update; quiet background rebuild if fast path fails.
        if not self._inplace_rows_update(old_keys, [new_row], insert_pos):
            self._schedule_rebuild(show_stub=False)

    def _split_selected(self, _=None):
        if self.read_only:
            self._snack(self.t("schedule.snack.readonly_mode", "У режимі перегляду редагування недоступне"), _WARN_FG)
            return
        # Use cached rows — avoids blocking _build_rows() call on UI thread.
        all_rows = self._last_built_rows or []
        rows = [r for r in all_rows if self._sel_key(r) in self._selected]
        candidates = [
            r
            for r in rows
            if len(r.get("product_ids") or []) > 1 or len(r.get("group_ids") or []) > 1
        ]
        if not candidates:
            self._snack(self.t("schedule.snack.no_merged_to_split", "Немає об'єднаних рядків для роз'єднання"), _WARN_FG)
            return
        products_by_id = self._products_by_id()
        split_disambiguators = self._product_disambiguators()
        split_count = 0
        by_group = self._tasks_by_group()
        all_source_gids: set[str] = set()
        # new_row_dicts[cand_key] = list of individual row dicts created from that candidate
        new_rows_per_candidate: list[tuple[str, list[dict]]] = []
        for row in candidates:
            cand_key = self._sel_key(row)
            source_group_ids = [
                str(v).strip()
                for v in (row.get("group_ids") or [row.get("group_id")])
                if str(v).strip()
            ]
            all_source_gids.update(source_group_ids)
            source_tasks = [
                dict(t)
                for gid in source_group_ids
                for t in (by_group.get(gid, []) or [])
            ]
            new_rows_for_cand = []
            for pid in row.get("product_ids") or []:
                pid_s = str(pid).strip()
                if not pid_s:
                    continue
                prod = products_by_id.get(pid_s) or {}
                new_gid = uuid.uuid4().hex
                pname = self._product_display_name(prod, split_disambiguators)
                payload = {
                    "id": new_gid,
                    "company_id": self.company_id,
                    "manager_id": self.manager_id,
                    "region_id": self._region_id or "",
                    "category_id": str(
                        prod.get("category_id") or row.get("category_id") or ""
                    ).strip(),
                    "item_type": "product",
                    "display_mode": "name",
                    "display_label": pname,
                    "product_ids": [pid_s],
                    "product_names": [pname],
                    "level_node_ids": {},
                }
                self._groups.append(dict(payload))
                self._ops_outbox[f"group:{new_gid}"] = {
                    "kind": "group",
                    "data": dict(payload),
                }
                split_count += 1
                for task in source_tasks:
                    cloned = dict(task)
                    cloned["id"] = uuid.uuid4().hex
                    cloned["group_id"] = new_gid
                    cloned["company_id"] = self.company_id
                    cloned["manager_id"] = self.manager_id
                    cloned["region_id"] = self._region_id or ""
                    self._tasks.append(dict(cloned))
                    self._ops_outbox[f"task:{cloned['id']}"] = {
                        "kind": "task",
                        "data": dict(cloned),
                    }
                new_rows_for_cand.append(
                    {
                        "key": f"g:{new_gid}",
                        "group_id": new_gid,
                        "group_ids": [new_gid],
                        "product_ids": [pid_s],
                        "product_names": [pname],
                        "category_id": str(
                            prod.get("category_id") or row.get("category_id") or ""
                        ).strip(),
                        "virtual": False,
                        "merged": False,
                    }
                )
            new_rows_per_candidate.append((cand_key, new_rows_for_cand))
        # Remove old source groups from memory + outbox
        if all_source_gids:
            self._groups = [
                g
                for g in self._groups
                if str((g or {}).get("id") or "").strip() not in all_source_gids
            ]
            for sgid in all_source_gids:
                self._ops_outbox[f"del_group:{sgid}"] = {
                    "kind": "del_group",
                    "data": {"id": sgid},
                }
                self._ops_outbox.pop(f"group:{sgid}", None)
        self._save_ops_outbox()
        self._refresh_workspace_sync_indicator_state()
        self._selected.clear()
        self._snack(self.t("schedule.snack.split_positions", "Роз'єднано: {n} позицій").format(n=split_count), _OK_FG)
        # Instant in-place UI update: process candidates bottom-to-top so indices stay valid.
        all_cand_keys = {self._sel_key(c) for c in candidates}
        all_new_rows = [r for _, nr in new_rows_per_candidate for r in nr]
        insert_pos = next(
            (
                i
                for i, r in enumerate(self._last_built_rows or [])
                if self._sel_key(r) in all_cand_keys
            ),
            0,
        )
        if not self._inplace_rows_update(all_cand_keys, all_new_rows, insert_pos):
            self._schedule_rebuild(show_stub=False)

    def _open_group_label_edit_dialog(self, row: dict) -> None:
        """Dialog to set a custom display label for a merged (multi-product) schedule group."""
        pg = self._pg()
        if not pg:
            return
        pnames = row.get("product_names") or []
        current = str(row.get("display_label") or "").strip()
        hint = " + ".join(pnames[:3]) + ("..." if len(pnames) > 3 else "")
        tf = ft.TextField(
            label=self.t("media.dlg.custom_name", "Своя назва"),
            hint_text=hint,
            value=current,
            width=380,
            autofocus=True,
            border_radius=10,
        )
        def _custom_name_help(_):
            from views.help_utils import show_modal_help
            _pg2 = self._pg()
            show_modal_help(_pg2, self.t("media.dlg.custom_name", "Своя назва"), [
                self.t("schedule.custom_name_help.step1", "1. Введіть власну назву для рядка."),
                self.t("schedule.custom_name_help.step2", "2. «Скинути» — повернути автоматичну назву."),
                self.t("schedule.custom_name_help.step3", "3. Натисніть «Зберегти»."),
            ])

        dlg = ft.AlertDialog(modal=True, title=ft.Row(
            [
                ft.Text(self.t("media.dlg.custom_name", "Своя назва"), expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_custom_name_help),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
        ))

        def _close(_e=None):
            with contextlib.suppress(Exception):
                pg.pop_dialog()

        def _apply_label(new_label: str):
            row["display_label"] = new_label
            gid = str(row.get("group_id") or "").strip()
            if gid:
                for grp in self._groups:
                    if (
                        isinstance(grp, dict)
                        and str(grp.get("id") or "").strip() == gid
                    ):
                        grp["display_label"] = new_label
                        break
            _close()
            self._save_group(row)
            # In-place: rebuild only the single left row that changed its label.
            sel_key = self._sel_key(row)
            idx = next(
                (
                    i
                    for i, r in enumerate(self._last_built_rows or [])
                    if self._sel_key(r) == sel_key
                ),
                None,
            )
            if (
                idx is not None
                and idx < len(self._left_rows_refs)
                and self._left_body_col_ref is not None
                and idx < len(self._left_body_col_ref.controls)
            ):
                row_h = self._row_height(row)
                new_left = self._left_row(row, idx, row_h)
                self._left_rows_refs[idx] = new_left
                self._left_body_col_ref.controls[idx] = new_left
                if idx < len(self._last_built_rows):
                    self._last_built_rows[idx] = row
                with contextlib.suppress(Exception):
                    self._left_body_col_ref.update()
            else:
                self._schedule_rebuild(show_stub=False)

        def _save(_e=None):
            _apply_label(str(tf.value or "").strip())

        def _clear(_e=None):
            _apply_label("")

        dlg.content = ft.Container(content=tf, padding=ft.padding.only(top=8))
        dlg.actions = [
            ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_close),
            ft.TextButton(self.t("common.reset", "Скинути"), on_click=_clear),
            ft.ElevatedButton(self.t("common.save", "Зберегти"), on_click=_save),
        ]
        dlg.actions_alignment = ft.MainAxisAlignment.END
        pg.show_dialog(dlg)

    def _open_note_row_dialog(self, existing_row: dict | None = None) -> None:
        """Create or edit a custom 'note' row (placeholder with no Товари entry)."""
        pg = self._pg()
        if not pg:
            return
        is_edit = bool(existing_row)
        gid = str((existing_row or {}).get("group_id") or "").strip() if is_edit else ""

        node_rows_ref: dict = getattr(self, "_node_rows", {}) or {}
        cats = [
            c for c in (self._categories or []) if isinstance(c, dict) and c.get("id")
        ]
        init_cat = (
            str((existing_row or {}).get("category_id") or "").strip()
            if is_edit
            else ""
        )
        init_levels = (
            {
                f"level_{p}_node_id": str(
                    (existing_row or {}).get(f"level_{p}_node_id") or ""
                ).strip()
                for p in range(1, 7)
            }
            if is_edit
            else {}
        )
        init_term = (
            str((existing_row or {}).get("production_term_days") or "").strip()
            if is_edit
            else ""
        )

        name_f = ft.TextField(
            label=self.t("schedule.field.name_required", "Назва *"),
            value=(
                str((existing_row or {}).get("display_label") or "").strip()
                if is_edit
                else ""
            ),
            width=360,
            autofocus=True,
            border_radius=10,
        )
        note_f = ft.TextField(
            label=self.t("schedule.field.comment_optional", "Коментар (необов'язково)"),
            value=(
                str((existing_row or {}).get("note") or "").strip() if is_edit else ""
            ),
            width=360,
            multiline=True,
            min_lines=2,
            max_lines=3,
            border_radius=10,
        )
        term_f = ft.TextField(
            label=self.t("schedule.field.production_days", "Термін виробництва (днів)"),
            value=init_term,
            width=360,
            keyboard_type=ft.KeyboardType.NUMBER,
            border_radius=10,
        )

        def _on_cat_change(e):
            cid = str(e.control.value or "").strip()
            _rebuild_levels(1, cid, cid, {})
            _update_level_dds(0)

        cat_dd = ft.Dropdown(
            label=self.t("schedule.field.category_required", "Категорія *"),
            options=[
                ft.dropdown.Option(
                    key=str(c.get("id") or "").strip(),
                    text=str(c.get("name") or "").strip(),
                )
                for c in sorted(cats, key=lambda x: str(x.get("name") or "").lower())
            ],
            value=init_cat or None,
            width=360,
            border_radius=10,
            on_select=_on_cat_change,
        )

        # Pre-allocate 6 level dropdowns — all hidden initially.
        # They must exist in the dialog tree from the first render so Flet
        # properly mounts them in Flutter. Toggling visible/options on
        # already-mounted controls is reliable; dynamically appending brand-new
        # controls to levels_col.controls after the dialog opens is not (Flet
        # 0.84 patch protocol skips controls whose __prev_lists snapshot was
        # never initialised during the frozen initial-render pass).
        _MAX_LEVELS = 6
        level_dds: list[ft.Dropdown] = [
            ft.Dropdown(
                label=f"Рівень {p}",
                options=[],
                visible=False,
                width=360,
                border_radius=10,
            )
            for p in range(1, _MAX_LEVELS + 1)
        ]
        levels_col = ft.Column(list(level_dds), spacing=8)

        def _find_nodes(pos: int, parent_id: str) -> list[dict]:
            if not parent_id:
                return []
            result = []
            for nd in node_rows_ref.values():
                if not isinstance(nd, dict):
                    continue
                nd_type = str(nd.get("node_type") or "").strip().lower()
                lp_raw = nd.get("level_position")
                try:
                    nd_lp = int(lp_raw) if lp_raw is not None else -1
                except Exception:
                    nd_lp = -1
                if nd_type != f"level_{pos}" and nd_lp != pos:
                    continue
                nd_par = str(
                    nd.get("parent_id") or nd.get("parent_node_id") or ""
                ).strip()
                if nd_par == parent_id:
                    result.append(nd)
                    continue
                if pos == 1:
                    nd_data = nd.get("data")
                    if (
                        isinstance(nd_data, dict)
                        and str(nd_data.get("category_id") or "").strip() == parent_id
                    ):
                        result.append(nd)
            return sorted(result, key=lambda x: str(x.get("name") or "").lower())

        def _level_label(cat_id: str, pos: int) -> str:
            by_title = (self._category_level_pos_by_title or {}).get(cat_id, {})
            pos_to_tk = {v: k for k, v in by_title.items()}
            tk = pos_to_tk.get(pos)
            if tk:
                for key, lbl in self._level_mode_options or []:
                    if key == f"level-title:{tk}":
                        return lbl
            return f"Рівень {pos}"

        def _rebuild_levels(
            from_pos: int, cat_id: str, parent_id: str, kept: dict
        ) -> None:
            cur_parent = parent_id
            for p in range(1, _MAX_LEVELS + 1):
                dd = level_dds[p - 1]
                if p < from_pos:
                    continue
                nodes = _find_nodes(p, cur_parent)
                if not nodes:
                    for q in range(p, _MAX_LEVELS + 1):
                        slot = level_dds[q - 1]
                        slot.visible = False
                        slot.options = []
                        slot.value = None
                    break
                valid_ids = {str(n.get("id") or "").strip() for n in nodes}
                pre = kept.get(f"level_{p}_node_id", "")
                if pre and pre not in valid_ids:
                    pre = ""
                dd.label = _level_label(cat_id, p)
                dd.options = [
                    ft.dropdown.Option(key="", text=self.t("schedule.dropdown.not_selected", "— не вибрано —")),
                    *[
                        ft.dropdown.Option(
                            key=str(n.get("id") or ""),
                            text=str(n.get("name") or ""),
                        )
                        for n in nodes
                    ],
                ]
                dd.value = pre or None
                dd.visible = True
                if pre:
                    cur_parent = pre
                else:
                    for q in range(p + 1, _MAX_LEVELS + 1):
                        slot = level_dds[q - 1]
                        slot.visible = False
                        slot.options = []
                        slot.value = None
                    break

        def _update_level_dds(from_idx: int = 0) -> None:
            for _dd in level_dds[from_idx:]:
                with contextlib.suppress(Exception):
                    _dd.update()

        def _on_level_dd(e, _p, _c):
            chosen = str(e.control.value or "").strip()
            _rebuild_levels(_p + 1, _c, chosen, {})
            _update_level_dds(_p)

        for _pi in range(1, _MAX_LEVELS + 1):
            level_dds[_pi - 1].on_select = lambda e, p=_pi: _on_level_dd(
                e, p, str(cat_dd.value or "").strip()
            )

        def _on_cat_change(e):
            cid = str(e.control.value or "").strip()
            _rebuild_levels(1, cid, cid, {})
            _update_level_dds(0)

        if init_cat:
            _rebuild_levels(1, init_cat, init_cat, init_levels)

        init_price = (
            str((existing_row or {}).get("price") or "").strip() if is_edit else ""
        )
        init_currency = (
            str((existing_row or {}).get("currency") or "").strip()
            if is_edit
            else (self.CURRENCIES[0] if self.CURRENCIES else "UAH")
        )
        price_f = ft.TextField(
            label=self.t("schedule.field.unit_price", "Ціна за одиницю"),
            value=init_price,
            width=230,
            keyboard_type=ft.KeyboardType.NUMBER,
            border_radius=10,
        )
        currency_dd = ft.Dropdown(
            label=self.t("orders.field.currency", "Валюта"),
            options=[ft.dropdown.Option(key=c, text=c) for c in self.CURRENCIES],
            value=init_currency or (self.CURRENCIES[0] if self.CURRENCIES else "UAH"),
            width=120,
            border_radius=10,
        )
        price_row = ft.Row([price_f, currency_dd], spacing=8, tight=True)

        content_col = ft.Column(
            [
                name_f,
                cat_dd,
                term_f,
                note_f,
                price_row,
                ft.Divider(height=4),
                levels_col,
            ],
            spacing=10,
            scroll=ft.ScrollMode.AUTO,
        )

        def _note_help(_):
            from views.help_utils import show_modal_help
            _pg2 = self._pg()
            show_modal_help(_pg2, self.t("schedule.dlg.note", "Замітка / планова позиція"), [
                self.t("schedule.note_help.step1", "1. Введіть назву позиції або нотатки."),
                self.t("schedule.note_help.step2", "2. Оберіть категорію."),
                self.t("schedule.note_help.step3", "3. Вкажіть термін виробництва (дні) якщо відомий."),
                self.t("schedule.note_help.step4", "4. Додайте ціну і валюту (опціонально)."),
                self.t("schedule.note_help.step5", "5. Оберіть рівні ієрархії (підкатегорія, серія тощо)."),
                self.t("schedule.note_help.step6", "6. Натисніть «Зберегти»."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                [
                    ft.Text(self.t("schedule.dlg.note", "Замітка / планова позиція"), weight="bold", expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_note_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Container(
                width=400,
                height=500,
                padding=16,
                content=content_col,
            ),
        )

        def _close(_e=None):
            with contextlib.suppress(Exception):
                pg.pop_dialog()

        def _save(_e=None):
            label = str(name_f.value or "").strip()
            if not label:
                self._snack(self.t("schedule.snack.enter_name", "Введіть назву"), _WARN_FG)
                return
            cat_id = str(cat_dd.value or "").strip()
            if not cat_id:
                self._snack(self.t("schedule.snack.select_category", "Оберіть категорію"), _WARN_FG)
                return
            note = str(note_f.value or "").strip()
            try:
                term_days: int | None = (
                    int(str(term_f.value or "").strip())
                    if str(term_f.value or "").strip()
                    else None
                )
            except (ValueError, TypeError):
                term_days = None
            try:
                price_val = (
                    float(str(price_f.value or "").strip().replace(",", "."))
                    if str(price_f.value or "").strip()
                    else None
                )
            except (ValueError, TypeError):
                price_val = None
            currency_val = str(currency_dd.value or "UAH").strip()
            level_vals: dict[str, str] = {}
            for pi, dd in enumerate(level_dds, start=1):
                if not dd.visible:
                    break
                nid = str(dd.value or "").strip()
                if nid:
                    level_vals[f"level_{pi}_node_id"] = nid
            new_gid = gid or uuid.uuid4().hex
            payload: dict = {
                "id": new_gid,
                "company_id": self.company_id,
                "manager_id": self.manager_id,
                "region_id": self._region_id or "",
                "item_type": "note",
                "display_mode": "name",
                "display_label": label,
                "category_id": cat_id,
                "product_ids": [],
                "product_names": [label],
                "note": note,
                "production_term_days": term_days,
                "price": price_val,
                "currency": currency_val,
                "level_node_ids": level_vals,
                **{
                    f"level_{p}_node_id": level_vals.get(f"level_{p}_node_id", "")
                    for p in range(1, 7)
                },
            }
            replaced = False
            for i, g in enumerate(self._groups):
                if isinstance(g, dict) and str(g.get("id") or "").strip() == new_gid:
                    self._groups[i] = {**g, **payload}
                    replaced = True
                    break
            if not replaced:
                self._groups.append(dict(payload))
            self._owned_group_ids.add(new_gid)
            self._ops_outbox[f"group:{new_gid}"] = {
                "kind": "group",
                "data": dict(payload),
            }
            self._save_ops_outbox()
            self._refresh_workspace_sync_indicator_state()
            # Re-price already-created tasks for this position — a note's price
            # is only read at task-dialog-save time, so editing it here would
            # otherwise leave existing tasks' planned/actual amounts stale.
            if price_val is not None:
                affected_months: set[str] = set()
                for i, t in enumerate(self._tasks or []):
                    if not isinstance(t, dict):
                        continue
                    if str(t.get("group_id") or "").strip() != new_gid:
                        continue
                    updated_t = dict(t)
                    changed = False
                    qty_val = _safe_float(updated_t.get("planned_qty"), None)
                    if qty_val is not None:
                        updated_t["planned_amount"] = float(qty_val) * float(price_val)
                        updated_t["planned_currency"] = currency_val
                        changed = True
                    actual_qty_val = _safe_float(updated_t.get("actual_qty"), None)
                    if actual_qty_val is not None:
                        updated_t["actual_amount"] = float(actual_qty_val) * float(
                            price_val
                        )
                        updated_t["actual_currency"] = currency_val
                        changed = True
                    if changed:
                        self._tasks[i] = updated_t
                        tid = str(updated_t.get("id") or "").strip()
                        if tid:
                            self._ops_outbox[f"task:{tid}"] = {
                                "kind": "task",
                                "data": updated_t,
                            }
                        month_str = str(updated_t.get("month_anchor") or "").strip()
                        if month_str:
                            affected_months.add(month_str[:10])
                if affected_months:
                    self._save_ops_outbox()
                    row_ctx = self._row_for_group_id(new_gid)
                    if row_ctx:
                        for month_str in affected_months:
                            try:
                                month_d = date.fromisoformat(month_str)
                            except Exception:
                                continue
                            self._fast_refresh_month_cell_tasks(row_ctx, month_d)
            _close()
            self._snack(self.t("schedule.snack.note_saved", "Замітку збережено"), _OK_FG)
            if is_edit and existing_row:
                existing_row.update(payload)
                existing_row["product_names"] = [label]
                existing_row["display_label"] = label
                sel_key = self._sel_key(existing_row)
                idx = next(
                    (
                        i
                        for i, r in enumerate(self._last_built_rows or [])
                        if self._sel_key(r) == sel_key
                    ),
                    None,
                )
                if (
                    idx is not None
                    and idx < len(self._left_rows_refs)
                    and self._left_body_col_ref is not None
                ):
                    row_h = self._row_height(existing_row)
                    new_left = self._left_row(existing_row, idx, row_h)
                    self._left_rows_refs[idx] = new_left
                    if idx < len(self._left_body_col_ref.controls):
                        self._left_body_col_ref.controls[idx] = new_left
                        with contextlib.suppress(Exception):
                            self._left_body_col_ref.update()
                    if idx < len(self._last_built_rows):
                        self._last_built_rows[idx] = existing_row
                    return
            # New row: insert in-place (append to visible list)
            new_row = {
                "key": f"g:{new_gid}",
                "group_id": new_gid,
                "group_ids": [new_gid],
                "product_ids": [],
                "product_names": [label],
                "category_id": cat_id,
                **{
                    f"level_{p}_node_id": level_vals.get(f"level_{p}_node_id", "")
                    for p in range(1, 7)
                },
                "display_label": label,
                "virtual": False,
                "merged": False,
                "item_type": "note",
                "note": note,
                "production_term_days": term_days,
                "price": price_val,
                "currency": currency_val,
                "is_inactive": False,
            }
            # Find insert position in the sorted active section.
            # Default sort: (category_name.lower(), display_label.lower()).
            # When column sort is active we can't trivially find the position, so
            # fall through to a full rebuild for that edge-case.
            if self._col_sort_key and self._col_sort_dir:
                self._schedule_rebuild(show_stub=False)
            elif (
                self._last_built_rows is not None
                and self._left_body_col_ref is not None
            ):
                cat_by_id = self._categories_by_id()
                new_sort_key = (
                    str(
                        cat_by_id.get(str(new_row.get("category_id") or "").strip())
                        or ""
                    ).lower(),
                    self._group_display_label(new_row).lower(),
                )
                insert_pos = len(self._last_built_rows)
                for _ri, _r in enumerate(self._last_built_rows):
                    if (
                        _r.get("is_inactive")
                        or str(_r.get("item_type") or "") == "inactive_header"
                    ):
                        insert_pos = _ri
                        break
                    _r_key = (
                        str(
                            cat_by_id.get(str(_r.get("category_id") or "").strip())
                            or ""
                        ).lower(),
                        self._group_display_label(_r).lower(),
                    )
                    if _r_key >= new_sort_key:
                        insert_pos = _ri
                        break
                if not self._inplace_rows_update(set(), [new_row], insert_pos):
                    self._schedule_rebuild(show_stub=False)
            else:
                self._schedule_rebuild(show_stub=False)

        def _delete(_e=None):
            if not gid:
                _close()
                return
            self._groups = [
                g for g in self._groups if str((g or {}).get("id") or "").strip() != gid
            ]
            self._tasks = [
                t
                for t in self._tasks
                if str((t or {}).get("group_id") or "").strip() != gid
            ]
            self._ops_outbox[f"del_group:{gid}"] = {
                "kind": "del_group",
                "data": {"id": gid},
            }
            self._ops_outbox.pop(f"group:{gid}", None)
            self._save_ops_outbox()
            self._refresh_workspace_sync_indicator_state()
            _close()
            self._snack(self.t("schedule.snack.note_deleted", "Замітку видалено"), _OK_FG)
            if existing_row:
                old_key = self._sel_key(existing_row)
                if not self._inplace_rows_update({old_key}, [], 0):
                    self._schedule_rebuild(show_stub=False)

        dlg.actions = [
            ft.TextButton(self.t("common.delete", "Видалити"), on_click=_delete, visible=is_edit),
            ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_close),
            ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save),
        ]
        dlg.actions_alignment = ft.MainAxisAlignment.END
        pg.show_dialog(dlg)

    def _show_group_items(self, row: dict):
        page = self._pg()
        if not page:
            return
        names = [
            str(v or "").strip()
            for v in (row.get("product_names") or [])
            if str(v or "").strip()
        ]
        if not names:
            names = ["-"]
        body = ft.Column(
            [
                ft.Text(f"{idx + 1}. {name}", size=12, selectable=True)
                for idx, name in enumerate(names)
            ],
            spacing=6,
            scroll=ft.ScrollMode.ALWAYS,
            height=min(360, 36 + 24 * len(names)),
        )

        def _close(_=None):
            try:
                page.pop_dialog()
            except Exception:
                dialog.open = False
                try:
                    dialog.update()
                except Exception:
                    pass

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(self.t("schedule.dlg.group_items", "Позиції у групі"), weight="bold"),
            content=ft.Container(width=520, content=body),
            actions=[ft.TextButton(self.t("packaging.dlg.close", "Закрити"), on_click=_close)],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dialog)

    def _fetch_uah_rates(self) -> dict:
        if not _HAS_REQUESTS:
            return {}
        urls = [
            "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json",
            "https://open.er-api.com/v6/latest/UAH",
        ]
        for idx, url in enumerate(urls):
            try:
                resp = _requests.get(url, timeout=6)
                resp.raise_for_status()
                data = resp.json()
                out = {"UAH": 1.0}
                if idx == 0 and isinstance(data, list):
                    for row in data:
                        if not isinstance(row, dict):
                            continue
                        cc = str(row.get("cc") or "").upper().strip()
                        rate = _safe_float(row.get("rate"), None)
                        unit = _safe_float(row.get("unit"), 1.0) or 1.0
                        if cc and rate and rate > 0 and unit > 0:
                            out[cc] = float(rate) / float(unit)
                    if len(out) > 1:
                        return out
                if idx == 1 and isinstance(data, dict):
                    rates = (
                        data.get("rates") if isinstance(data.get("rates"), dict) else {}
                    )
                    for cc, val in rates.items():
                        amount = _safe_float(val, None)
                        cc_u = str(cc or "").upper().strip()
                        if cc_u and cc_u != "UAH" and amount and amount > 0:
                            out[cc_u] = 1.0 / float(amount)
                    if len(out) > 1:
                        return out
            except Exception:
                continue
        return {}

    def _uah_rates(self) -> dict:
        """Cache-only read — NEVER performs blocking network I/O. If the
        cache is missing/stale, a background refresh is scheduled (see
        `_schedule_uah_rates_background_refresh`) and the best data we have
        so far is returned immediately. This used to call
        `_fetch_uah_rates()` (blocking HTTP, up to ~12s worst case) INLINE
        whenever the 1200s cache expired, on whatever thread called
        `_convert_amount` — including synchronous save handlers — which
        could freeze the whole app's event loop."""
        ts = datetime.now().timestamp()
        is_fresh = bool(self._uah_rates_cache) and (ts - self._uah_rates_ts) < 1200
        if not is_fresh:
            self._schedule_uah_rates_background_refresh()
        return self._uah_rates_cache

    def _schedule_uah_rates_background_refresh(self) -> None:
        if getattr(self, "_uah_rates_refresh_in_flight", False):
            return
        page = self._pg()
        if not (page and hasattr(page, "run_task")):
            return
        self._uah_rates_refresh_in_flight = True

        async def _refresh():
            try:
                fresh = await asyncio.to_thread(self._fetch_uah_rates)
                if fresh:
                    self._uah_rates_cache = fresh
                    self._uah_rates_ts = datetime.now().timestamp()
            except Exception:
                logger.exception("Background UAH rate refresh failed")
            finally:
                self._uah_rates_refresh_in_flight = False

        with contextlib.suppress(Exception):
            page.run_task(_refresh)

    def _convert_amount(
        self, amount: float | None, from_ccy: str, to_ccy: str
    ) -> float | None:
        if amount is None:
            return None
        src = str(from_ccy or "USD").upper().strip() or "USD"
        dst = str(to_ccy or "USD").upper().strip() or "USD"
        if src == dst:
            return float(amount)
        rates = self._uah_rates() or {}

        def _uah(ccy: str) -> float | None:
            if ccy == "UAH":
                return 1.0
            return _safe_float(rates.get(ccy), None)

        src_uah = _uah(src)
        dst_uah = _uah(dst)
        if not src_uah or not dst_uah:
            return None
        return float(amount) * (float(src_uah) / float(dst_uah))

    def _auto_pricing_context(self, row: dict) -> tuple[bool, float | None, str]:
        if self._display_mode != "name":
            return False, None, "USD"
        # Note rows use their own stored price/currency if set
        if str(row.get("item_type") or "").strip() == "note":
            note_price = _safe_float(row.get("price"), None)
            note_ccy = str(row.get("currency") or "USD").upper().strip() or "USD"
            return note_price is not None, note_price, note_ccy
        pids = list(row.get("product_ids") or [])
        if len(pids) != 1:
            return False, None, "USD"
        prod = self._products_by_id().get(str(pids[0]).strip()) or {}
        price = _safe_float(prod.get("purchase_price"), None)
        currency = str(prod.get("purchase_currency") or "USD").upper().strip() or "USD"
        return price is not None, price, currency

    def _toggle_task_status(self, task: dict):
        if self.read_only:
            return
        updated = dict(task)
        updated["status"] = (
            "ordered" if str(task.get("status") or "") != "ordered" else "not_ordered"
        )
        tid = str(updated.get("id") or "").strip()
        if tid:
            for i, t in enumerate(self._tasks or []):
                if str((t or {}).get("id") or "").strip() == tid:
                    self._tasks[i] = dict(updated)
                    break
        self._ops_outbox[f"task:{tid}"] = {"kind": "task", "data": updated}
        self._save_ops_outbox()
        self._refresh_workspace_sync_indicator_state()
        # In-place refresh — rebuild only the affected month cell.
        gid = str(updated.get("group_id") or "").strip()
        month_str = str(updated.get("month_anchor") or "").strip()
        row_ctx = self._row_for_group_id(gid)
        try:
            month_d = date.fromisoformat(month_str[:10]) if month_str else None
        except Exception:
            month_d = None
        if row_ctx and month_d:
            self._fast_refresh_month_cell_tasks(row_ctx, month_d)
        else:
            self._schedule_rebuild()
        # Status flips whether this task is "done" in the notebook's linked
        # reminder (order_task_id) — flush it to the DB right away (instead
        # of waiting for the 45s outbox loop) and refresh the toolbar's
        # notebook badge, so the counter doesn't require opening/closing the
        # notebook to notice the change.
        pg = self._pg()
        if pg and hasattr(pg, "run_task"):
            with contextlib.suppress(Exception):
                pg.run_task(self._sync_status_change_and_refresh_badge)

    async def _sync_status_change_and_refresh_badge(self) -> None:
        with contextlib.suppress(Exception):
            await self.flush_pending_async()
        pg = self._pg()
        dash = getattr(pg, "_dashboard", None) if pg else None
        if dash is not None:
            with contextlib.suppress(Exception):
                dash._refresh_notebook_badge()

    def _delete_task(self, task_id: str):
        if self.read_only:
            return
        tid = str(task_id or "").strip()
        if not tid:
            return
        # Find task before removing so we know which cell to refresh.
        task_obj = next(
            (
                t
                for t in (self._tasks or [])
                if str((t or {}).get("id") or "").strip() == tid
            ),
            None,
        )
        self._tasks = [
            t
            for t in (self._tasks or [])
            if str((t or {}).get("id") or "").strip() != tid
        ]
        # Stage in outbox — DB call on next 45s flush.
        self._ops_outbox[f"del_task:{tid}"] = {"kind": "del_task", "data": {"id": tid}}
        self._save_ops_outbox()
        self._refresh_workspace_sync_indicator_state()
        self._snack(self.t("schedule.snack.task_deleted", "Задачу видалено"), _OK_FG)
        # In-place refresh of the affected month cell.
        if task_obj:
            gid = str(task_obj.get("group_id") or "").strip()
            month_str = str(task_obj.get("month_anchor") or "").strip()
            row_ctx = self._row_for_group_id(gid)
            try:
                month_d = date.fromisoformat(month_str[:10]) if month_str else None
            except Exception:
                month_d = None
            if row_ctx and month_d:
                self._fast_refresh_month_cell_tasks(row_ctx, month_d)
                return
        self._schedule_rebuild()

    @staticmethod
    def _shift_date_to_month(d: date, target_month: date) -> date:
        """Return d with month/year replaced by target_month, day clamped to valid range."""
        import calendar as _cal

        last_day = _cal.monthrange(target_month.year, target_month.month)[1]
        return date(target_month.year, target_month.month, min(d.day, last_day))

    def _open_drag_drop_dialog(
        self,
        src_task: dict,
        src_row: dict,
        src_month: date | None,
        dst_row: dict,
        dst_month: date,
    ) -> None:
        page = self._pg()
        if not page:
            return
        if self.read_only:
            self._snack(self.t("schedule.snack.readonly_mode", "У режимі перегляду редагування недоступне"), _WARN_FG)
            return

        src_gid = str(src_row.get("group_id") or "").strip()
        dst_gid = str(dst_row.get("group_id") or "").strip()
        src_label = self._group_display_label(src_row)
        dst_label = self._group_display_label(dst_row)
        src_month_str = _month_title(src_month, self.t) if src_month else "?"
        dst_month_str = _month_title(dst_month, self.t)

        # Build adjusted dates for the destination.
        def _adjusted(field: str) -> str | None:
            raw = _parse_date(src_task.get(field))
            if not raw:
                return None
            return self._shift_date_to_month(raw, dst_month).isoformat()

        new_date_from = _adjusted("date_from") or _month_key(dst_month)
        new_date_to = _adjusted("date_to") or _month_key(dst_month)
        same_month = (src_month == dst_month) if src_month else True
        same_product = src_gid == dst_gid

        def _close(_=None):
            with contextlib.suppress(Exception):
                page.pop_dialog()

        def _do_copy(_=None):
            _close()
            new_tid = uuid.uuid4().hex
            # Ensure destination group exists (including virtual rows with no group_id yet).
            if not dst_gid or not self._is_group_owned(dst_row):
                self._save_group(dst_row)
            effective_dst_gid = (
                str(dst_row.get("group_id") or dst_gid).strip() or dst_gid
            )
            if not effective_dst_gid:
                self._snack(self.t("schedule.snack.no_target_row", "Не вдалося визначити рядок призначення"), _ERR_FG)
                return
            new_task = {
                **dict(src_task),
                "id": new_tid,
                "group_id": effective_dst_gid,
                "month_anchor": _month_key(dst_month),
                "date_from": new_date_from,
                "date_to": new_date_to,
                "archived": False,
            }
            self._tasks.append(new_task)
            self._persist_task_async(new_task)
            self._snack(self.t("schedule.snack.order_copied", "Замовлення скопійовано"), _OK_FG)
            # Refresh destination cell only — fast in-place update.
            dst_row_ctx = self._row_for_group_id(effective_dst_gid) or dst_row
            self._fast_refresh_month_cell_tasks(
                dst_row_ctx, dst_month, cell_lookup_row=dst_row
            )

        def _do_move(_=None):
            _close()
            # Ensure destination group exists (including virtual rows with no group_id yet).
            if not dst_gid or not self._is_group_owned(dst_row):
                self._save_group(dst_row)
            effective_dst_gid = (
                str(dst_row.get("group_id") or dst_gid).strip() or dst_gid
            )
            if not effective_dst_gid:
                self._snack(self.t("schedule.snack.no_target_row", "Не вдалося визначити рядок призначення"), _ERR_FG)
                return
            tid = str(src_task.get("id") or "").strip()
            moved_task = {
                **dict(src_task),
                "group_id": effective_dst_gid,
                "month_anchor": _month_key(dst_month),
                "date_from": new_date_from,
                "date_to": new_date_to,
                "archived": False,
            }
            if tid:
                for i, t in enumerate(self._tasks or []):
                    if str((t or {}).get("id") or "").strip() == tid:
                        self._tasks[i] = moved_task
                        break
                else:
                    self._tasks.append(moved_task)
            else:
                self._tasks.append(moved_task)
            self._persist_task_async(moved_task)
            self._snack(self.t("schedule.snack.order_moved", "Замовлення переміщено"), _OK_FG)
            # Fast in-place update: clear source cell, fill destination cell.
            if src_month and not (same_month and same_product):
                src_row_ctx = self._row_for_group_id(src_gid) if src_gid else None
                if src_row_ctx and src_month:
                    self._fast_refresh_month_cell_tasks(
                        src_row_ctx, src_month, cell_lookup_row=src_row
                    )
            dst_row_ctx = self._row_for_group_id(effective_dst_gid) or dst_row
            self._fast_refresh_month_cell_tasks(
                dst_row_ctx, dst_month, cell_lookup_row=dst_row
            )

        # Build info lines for the dialog.
        info_lines = [f"{src_month_str} → {dst_month_str}"]
        if not same_product:
            info_lines.append(f"{src_label} → {dst_label}")

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(self.t("schedule.dlg.move_order", "Перенести замовлення"), weight="bold"),
            content=ft.Container(
                width=340,
                content=ft.Column(
                    [ft.Text(line, size=13, color=_TEXT) for line in info_lines],
                    spacing=4,
                ),
            ),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_close),
                ft.OutlinedButton(content=self.t("schedule.copy_btn", "Скопіювати"), on_click=_do_copy),
                ft.FilledButton("Перемістити", on_click=_do_move),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dialog)

    def _open_archive_month_picker(self, _=None):
        page = self._pg()
        if not page:
            return
        current_start = _month_start()
        year_now = current_start.year
        month_dd = ft.Dropdown(
            label=self.t("media.field.month", "Місяць"),
            width=180,
            border_radius=10,
            options=[
                ft.dropdown.Option(str(m), _month_title(date(2024, m, 1), self.t).split()[0])
                for m in range(1, 13)
            ],
            value=str(max(1, current_start.month - 1)),
        )
        year_dd = ft.Dropdown(
            label=self.t("schedule.field.year", "Рік"),
            width=140,
            border_radius=10,
            options=[
                ft.dropdown.Option(str(y), str(y))
                for y in range(year_now - 3, year_now + 1)
            ],
            value=str(year_now),
        )

        def _close(_=None):
            try:
                page.pop_dialog()
            except Exception:
                dialog.open = False
                try:
                    dialog.update()
                except Exception:
                    pass

        def _apply(_=None):
            try:
                selected = date(int(year_dd.value), int(month_dd.value), 1)
            except Exception:
                self._snack(self.t("media.err.select_month_year", "Оберіть місяць і рік"), _WARN_FG)
                return
            if selected > current_start:
                self._snack(
                    self.t("schedule.snack.archive_past_only", "В архів можна відкрити лише минулі та поточний місяць"),
                    _WARN_FG,
                )
                return
            key = _month_key(selected)
            already_open = key in self._opened_archive_month_keys
            if not already_open:
                self._opened_archive_month_keys.append(key)
                self._opened_archive_month_keys = [
                    _month_key(v) for v in self._opened_archive_months()
                ]
            self._save_month_view_settings()
            _close()
            if already_open:
                return
            if self._schedule_ui_ready and self._fast_add_archive_month(selected):
                return
            self._schedule_rebuild()

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(self.t("schedule.dlg.open_archive_month", "Відкрити місяць з архіву"), weight="bold"),
            content=ft.Container(
                width=360,
                content=ft.Row([month_dd, year_dd], spacing=10),
            ),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_close),
                ft.FilledButton(self.t("extensions.open", "Відкрити"), on_click=_apply),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dialog)

    def _close_all_archive_months(self, _=None):
        if not self._opened_archive_month_keys:
            return
        self._opened_archive_month_keys = []
        self._save_month_view_settings()
        self._schedule_rebuild()

    def _remove_archive_month(self, month_key: str):
        old_visible = self._visible_month_anchors()
        old_pos = next(
            (i for i, m in enumerate(old_visible) if _month_key(m) == month_key),
            None,
        )
        self._opened_archive_month_keys = [
            k for k in self._opened_archive_month_keys if k != month_key
        ]
        self._save_month_view_settings()
        if (
            self._schedule_ui_ready
            and old_pos is not None
            and self._fast_remove_archive_month_at(old_pos)
        ):
            return
        self._schedule_rebuild()

    def _open_task_dialog(
        self, row: dict, month_anchor: date, task: dict | None = None
    ):
        if self.read_only or not self._is_group_owned(row):
            self._snack(self.t("schedule.snack.readonly_mode", "У режимі перегляду редагування недоступне"), _WARN_FG)
            return
        # Opening several cards back-to-back (each open closes the previous
        # one) can outrun Flutter's dialog close/open transition and render
        # a blank/gray dialog surface. Ignore re-entrant opens while one is
        # already up, and give a just-closed dialog a brief moment to
        # actually finish its close animation before opening the next.
        import time as _time

        if getattr(self, "_task_dialog_open", False):
            return
        _cooldown_left = 0.25 - (
            _time.monotonic() - getattr(self, "_last_dialog_close_ts", 0.0)
        )
        page = self._pg()
        if not page:
            return
        if _cooldown_left > 0:
            if hasattr(page, "run_task"):
                async def _delayed_open(_r=row, _m=month_anchor, _t=task, _d=_cooldown_left):
                    await asyncio.sleep(_d)
                    self._open_task_dialog(_r, _m, _t)

                with contextlib.suppress(Exception):
                    page.run_task(_delayed_open)
            return
        gid = str(row.get("group_id") or "").strip()
        if not gid:
            gid = self._save_group(row)
            if not gid:
                self._snack(self.t("schedule.snack.row_prep_failed", "Не вдалося підготувати рядок для збереження"), _ERR_FG)
                return
        task_row = dict(task or {})
        can_auto, unit_price, auto_ccy = self._auto_pricing_context(row)
        # Derive unit price from existing task data for proportional recalc when can_auto=False
        _init_plan_qty = _safe_float(task_row.get("planned_qty"), None)
        _init_plan_amt = _safe_float(task_row.get("planned_amount"), None)
        _manual_unit_price = (
            _init_plan_amt / _init_plan_qty
            if _init_plan_qty and _init_plan_qty != 0 and _init_plan_amt is not None
            else None
        )
        item_label = self._group_display_label(row)
        month_value = _month_key(month_anchor)
        qty_f = ft.TextField(
            label=self.t("schedule.field.quantity", "Кількість"),
            border_radius=10,
            width=140,
            value=_fmt_money(_safe_float(task_row.get("planned_qty"), None)),
        )
        planned_amount_f = ft.TextField(
            label=self.t("schedule.field.supplier_amount", "Сума постачальника"),
            border_radius=10,
            width=170,
            value=_fmt_money(_safe_float(task_row.get("planned_amount"), None)),
            disabled=True,
        )
        planned_currency_f = ft.Dropdown(
            label=self.t("orders.field.currency", "Валюта"),
            width=120,
            border_radius=10,
            options=[ft.dropdown.Option(c) for c in self.CURRENCIES],
            value=str(task_row.get("planned_currency") or auto_ccy or "USD")
            .upper()
            .strip()
            or "USD",
            disabled=can_auto,
        )
        planned_usd_txt = ft.Text("", size=12, color=_MUTED)
        actual_qty_f = ft.TextField(
            label=self.t("schedule.field.ordered_actual", "Замовлено факт"),
            border_radius=10,
            width=140,
            value=_fmt_money(_safe_float(task_row.get("actual_qty"), None)),
        )
        actual_amount_f = ft.TextField(
            label=self.t("schedule.field.amount_actual", "Сума факт"),
            border_radius=10,
            width=170,
            value=_fmt_money(_safe_float(task_row.get("actual_amount"), None)),
            disabled=True,
        )
        actual_currency_f = ft.Dropdown(
            label=self.t("schedule.field.currency_actual", "Валюта факт"),
            width=120,
            border_radius=10,
            options=[ft.dropdown.Option(c) for c in self.CURRENCIES],
            value=str(
                task_row.get("actual_currency") or planned_currency_f.value or "USD"
            )
            .upper()
            .strip()
            or "USD",
            disabled=can_auto,
        )
        actual_usd_txt = ft.Text("", size=12, color=_MUTED)
        status_f = ft.Dropdown(
            label=self.t("orders.field.status", "Статус"),
            width=180,
            border_radius=10,
            options=[
                ft.dropdown.Option("not_ordered", self.t("schedule.status.not_ordered", "Не замовлено")),
                ft.dropdown.Option("ordered", self.t("schedule.status.ordered", "Замовлено")),
            ],
            value=str(task_row.get("status") or "not_ordered").strip() or "not_ordered",
        )
        delivery_dd = ft.Dropdown(
            label=self.t("orders.field.delivery_type", "Тип доставки"),
            border_radius=10,
            width=260,
            options=[
                ft.dropdown.Option(
                    str((m or {}).get("id") or ""), str((m or {}).get("name") or "")
                )
                for m in (self._delivery_methods or [])
                if isinstance(m, dict) and (m.get("id") or "")
            ],
            value=str(task_row.get("delivery_type_id") or "").strip(),
        )
        _from_label = _fmt_date(task_row.get("date_from") or month_value) or self.t("schedule.dropdown.select", "Оберіть")
        _to_label = _fmt_date(task_row.get("date_to") or month_value) or self.t("schedule.dropdown.select", "Оберіть")
        from_btn = ft.OutlinedButton(content=_from_label, width=180)
        to_btn = ft.OutlinedButton(content=_to_label, width=180)
        picked_dates = {
            "from": str(task_row.get("date_from") or month_value)[:10],
            "to": str(task_row.get("date_to") or month_value)[:10],
        }

        def _recalc_labels(_=None):
            qty_val = _safe_float(qty_f.value, None)
            planned_currency = (
                str(planned_currency_f.value or auto_ccy or "USD").upper().strip()
                or "USD"
            )
            planned_amount = _safe_float(planned_amount_f.value, None)
            if can_auto:
                if qty_val is not None and unit_price is not None:
                    planned_amount = float(qty_val) * float(unit_price)
                    planned_amount_f.value = _fmt_money(planned_amount)
                    planned_currency_f.value = auto_ccy
                else:
                    planned_amount = None
                    planned_amount_f.value = ""
            elif _manual_unit_price is not None and qty_val is not None:
                planned_amount = round(qty_val * _manual_unit_price, 2)
                planned_amount_f.value = _fmt_money(planned_amount)
                try:
                    planned_amount_f.update()
                except Exception:
                    pass
            planned_usd = (
                self._convert_amount(planned_amount, planned_currency, "USD")
                if planned_amount is not None
                else None
            )
            planned_usd_txt.value = (
                (
                    f"План: {_fmt_money(planned_amount)} {planned_currency}"
                    + (
                        f"  /  {_fmt_money(planned_usd)} $"
                        if planned_amount is not None
                        and planned_currency != "USD"
                        and planned_usd is not None
                        else ""
                    )
                )
                if planned_amount is not None
                else "План: —"
            )

            actual_amount = _safe_float(actual_amount_f.value, None)
            actual_currency = (
                str(actual_currency_f.value or planned_currency or "USD")
                .upper()
                .strip()
                or "USD"
            )
            if can_auto:
                actual_qty_val = _safe_float(actual_qty_f.value, None)
                if actual_qty_val is not None and unit_price is not None:
                    actual_amount = float(actual_qty_val) * float(unit_price)
                    actual_amount_f.value = _fmt_money(actual_amount)
                    actual_currency = auto_ccy
                    actual_currency_f.value = auto_ccy
                else:
                    actual_amount = None
                    actual_amount_f.value = ""
            elif _manual_unit_price is not None:
                actual_qty_val = _safe_float(actual_qty_f.value, None)
                if actual_qty_val is not None:
                    actual_amount = round(actual_qty_val * _manual_unit_price, 2)
                    actual_amount_f.value = _fmt_money(actual_amount)
                else:
                    actual_amount = None
                    actual_amount_f.value = ""
                try:
                    actual_amount_f.update()
                except Exception:
                    pass
            actual_usd = (
                self._convert_amount(actual_amount, actual_currency, "USD")
                if actual_amount is not None
                else None
            )
            actual_usd_txt.value = (
                (
                    f"Факт: {_fmt_money(actual_amount)} {actual_currency}"
                    + (
                        f"  /  {_fmt_money(actual_usd)} $"
                        if actual_amount is not None
                        and actual_currency != "USD"
                        and actual_usd is not None
                        else ""
                    )
                )
                if actual_amount is not None
                else "Факт: —"
            )
            try:
                planned_usd_txt.update()
            except Exception:
                pass
            try:
                actual_usd_txt.update()
            except Exception:
                pass
            if can_auto:
                for _ctrl in (
                    planned_amount_f,
                    planned_currency_f,
                    actual_amount_f,
                    actual_currency_f,
                ):
                    try:
                        _ctrl.update()
                    except Exception:
                        pass

        qty_f.on_change = _recalc_labels
        planned_amount_f.on_change = _recalc_labels
        planned_currency_f.on_change = _recalc_labels
        planned_currency_f.on_select = _recalc_labels
        actual_qty_f.on_change = _recalc_labels
        actual_amount_f.on_change = _recalc_labels
        actual_currency_f.on_change = _recalc_labels
        actual_currency_f.on_select = _recalc_labels

        def _open_date_picker(slot: str):
            current = _parse_date(picked_dates.get(slot)) or month_anchor
            _closed = {"done": False}

            def _close_picker():
                if _closed["done"]:
                    return
                _closed["done"] = True
                dp.open = False
                with contextlib.suppress(Exception):
                    dp.update()
                with contextlib.suppress(Exception):
                    page.overlay.remove(dp)

            def _picked(e):
                value = _picker_event_iso_date(e)
                if not value:
                    _close_picker()
                    return
                picked_dates[slot] = value
                btn = from_btn if slot == "from" else to_btn
                btn.content = _fmt_date(value) or self.t("schedule.dropdown.select", "Оберіть")
                _close_picker()
                try:
                    btn.update()
                except Exception:
                    pass

            def _picker_dismiss(_=None):
                _close_picker()

            dp = ft.DatePicker(
                value=datetime.combine(current, datetime.min.time()),
                first_date=date(2020, 1, 1),
                last_date=date(2100, 12, 31),
                entry_mode=ft.DatePickerEntryMode.CALENDAR_ONLY,
                date_picker_mode=ft.DatePickerMode.DAY,
                locale=ft.Locale("uk", "UA"),
                on_change=_picked,
                on_dismiss=_picker_dismiss,
            )
            page.overlay.append(dp)
            dp.open = True
            page.update()

        def _close(_=None):
            self._task_dialog_open = False
            self._last_dialog_close_ts = _time.monotonic()
            with contextlib.suppress(Exception):
                page.pop_dialog()

        def _save(_=None):
            selected_delivery = next(
                (
                    m
                    for m in (self._delivery_methods or [])
                    if str((m or {}).get("id") or "").strip()
                    == str(delivery_dd.value or "").strip()
                ),
                None,
            )
            planned_currency = (
                str(planned_currency_f.value or auto_ccy or "USD").upper().strip()
                or "USD"
            )
            planned_amount = _safe_float(planned_amount_f.value, None)
            if can_auto:
                qty_num = _safe_float(qty_f.value, None)
                if qty_num is not None and unit_price is not None:
                    planned_amount = float(qty_num) * float(unit_price)
                    planned_currency = auto_ccy
            actual_currency = (
                str(actual_currency_f.value or planned_currency or "USD")
                .upper()
                .strip()
                or "USD"
            )
            actual_amount = _safe_float(actual_amount_f.value, None)
            if can_auto:
                actual_qty_num = _safe_float(actual_qty_f.value, None)
                if actual_qty_num is not None and unit_price is not None:
                    actual_amount = float(actual_qty_num) * float(unit_price)
                    actual_currency = auto_ccy
                else:
                    actual_amount = None
            payload = {
                "id": str(task_row.get("id") or "").strip() or uuid.uuid4().hex,
                "group_id": gid,
                "company_id": self.company_id,
                "manager_id": self.manager_id,
                "region_id": self._region_id or "",
                "month_anchor": month_value,
                "date_from": picked_dates.get("from") or month_value,
                "date_to": picked_dates.get("to") or month_value,
                "planned_qty": _safe_float(qty_f.value, None),
                "planned_amount": planned_amount,
                "planned_currency": planned_currency,
                "planned_amount_usd": (
                    self._convert_amount(planned_amount, planned_currency, "USD")
                    if planned_amount is not None
                    else None
                ),
                "actual_qty": _safe_float(actual_qty_f.value, None),
                "actual_amount": actual_amount,
                "actual_currency": actual_currency,
                "actual_amount_usd": (
                    self._convert_amount(
                        actual_amount,
                        actual_currency,
                        "USD",
                    )
                    if actual_amount is not None
                    else None
                ),
                "delivery_type_id": str(delivery_dd.value or "").strip(),
                "delivery_type_name": str(
                    (selected_delivery or {}).get("name") or ""
                ).strip(),
                "status": str(status_f.value or "not_ordered").strip() or "not_ordered",
                "archived": False,
                "sort_index": int(task_row.get("sort_index") or 0),
            }
            tid = str(payload.get("id") or "").strip()
            if tid:
                replaced = False
                for i, existing_row in enumerate(self._tasks or []):
                    if str((existing_row or {}).get("id") or "").strip() == tid:
                        self._tasks[i] = dict(payload)
                        replaced = True
                        break
                if not replaced:
                    self._tasks.append(dict(payload))
            _close()
            self._persist_task_async(payload, reload_on_error=True)
            self._snack(self.t("schedule.snack.task_saved", "Задачу збережено"), _OK_FG)
            # Pass `row` (has the "key" field for rk: lookup), not `payload` (task dict).
            self._fast_refresh_month_cell_tasks(row, month_anchor)

        def _delete(_=None):
            task_id = str(task_row.get("id") or "").strip()
            if not task_id:
                _close()
                return
            # Remove from in-memory list immediately (optimistic delete).
            self._tasks = [
                t
                for t in (self._tasks or [])
                if str((t or {}).get("id") or "").strip() != task_id
            ]
            # Stage delete in outbox — DB call happens on next 45s flush or "Локально" press.
            self._ops_outbox[f"del_task:{task_id}"] = {
                "kind": "del_task",
                "data": {"id": task_id},
            }
            self._save_ops_outbox()
            self._refresh_workspace_sync_indicator_state()
            _close()
            self._snack(self.t("schedule.snack.task_deleted", "Задачу видалено"), _OK_FG)
            self._fast_refresh_month_cell_tasks(row, month_anchor)

        _recalc_labels()
        dialog = ft.AlertDialog(
            modal=True,
            on_dismiss=_close,
            title=ft.Text(self.t("schedule.dlg.order_plan", "План замовлення"), weight="bold"),
            content=ft.Container(
                width=860,
                height=460,
                padding=18,
                bgcolor=_PANEL,
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Container(
                                    expand=True,
                                    padding=10,
                                    border=ft.border.all(1, _BORDER),
                                    border_radius=10,
                                    bgcolor=_SLATE,
                                    content=ft.Column(
                                        [
                                            ft.Text(
                                                f"Регіон: {self.region_label}",
                                                size=12,
                                                color=_MUTED,
                                            ),
                                            ft.Text(
                                                item_label,
                                                size=16,
                                                weight="bold",
                                                selectable=True,
                                            ),
                                            ft.Text(
                                                _month_title(month_anchor, self.t),
                                                size=11,
                                                color=_ACCENT,
                                            ),
                                        ],
                                        spacing=4,
                                    ),
                                )
                            ]
                        ),
                        ft.Row(
                            [
                                ft.Container(
                                    width=180,
                                    content=ft.Column(
                                        [
                                            ft.Text("Дата з", size=11, color=_MUTED),
                                            from_btn,
                                        ],
                                        spacing=4,
                                    ),
                                ),
                                ft.Container(
                                    width=180,
                                    content=ft.Column(
                                        [
                                            ft.Text("Дата по", size=11, color=_MUTED),
                                            to_btn,
                                        ],
                                        spacing=4,
                                    ),
                                ),
                                qty_f,
                                status_f,
                                delivery_dd,
                            ],
                            spacing=10,
                            wrap=True,
                        ),
                        ft.Row(
                            [
                                ft.Container(
                                    expand=True,
                                    padding=12,
                                    border=ft.border.all(1, _BORDER),
                                    border_radius=12,
                                    bgcolor=self._th_panel,
                                    content=ft.Column(
                                        [
                                            ft.Text(
                                                "План",
                                                size=12,
                                                weight="bold",
                                                color=_TEXT,
                                            ),
                                            ft.Row(
                                                [planned_amount_f, planned_currency_f],
                                                spacing=10,
                                                wrap=True,
                                            ),
                                            planned_usd_txt,
                                        ],
                                        spacing=8,
                                    ),
                                ),
                                ft.Container(
                                    expand=True,
                                    padding=12,
                                    border=ft.border.all(1, _BORDER),
                                    border_radius=12,
                                    bgcolor=self._th_panel,
                                    content=ft.Column(
                                        [
                                            ft.Text(
                                                "Факт",
                                                size=12,
                                                weight="bold",
                                                color=_TEXT,
                                            ),
                                            ft.Row(
                                                [
                                                    actual_qty_f,
                                                    actual_amount_f,
                                                    actual_currency_f,
                                                ],
                                                spacing=10,
                                                wrap=True,
                                            ),
                                            actual_usd_txt,
                                        ],
                                        spacing=8,
                                    ),
                                ),
                            ],
                            spacing=10,
                            wrap=True,
                        ),
                    ],
                    spacing=12,
                    scroll=ft.ScrollMode.AUTO,
                ),
            ),
            actions=[
                ft.TextButton(
                    self.t("common.delete", "Видалити"), on_click=_delete, visible=bool(task_row.get("id"))
                ),
                ft.TextButton("Скасувати", on_click=_close),
                ft.FilledButton("Зберегти", on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        from_btn.on_click = lambda _: _open_date_picker("from")
        to_btn.on_click = lambda _: _open_date_picker("to")
        self._task_dialog_open = True
        page.show_dialog(dialog)

    def _status_colors(self, status: str) -> tuple[str, str]:
        if str(status or "") == "ordered":
            return _OK, _OK_FG
        return _WARN, _WARN_FG

    def _task_card(self, row: dict, task: dict, month_anchor: date) -> ft.Container | ft.Draggable:
        planned_amount = _safe_float(task.get("planned_amount"), None)
        planned_currency = (
            str(task.get("planned_currency") or "USD").upper().strip() or "USD"
        )
        actual_amount = _safe_float(task.get("actual_amount"), None)
        actual_currency = (
            str(task.get("actual_currency") or planned_currency or "USD")
            .upper()
            .strip()
            or "USD"
        )
        status_bg, status_fg = self._status_colors(
            str(task.get("status") or "not_ordered")
        )
        delivery_name = str(task.get("delivery_type_name") or "—").strip() or "—"
        range_text = f"{_fmt_date(task.get('date_from'))} - {_fmt_date(task.get('date_to'))}".strip(
            " -"
        )
        plan_text = f"План: {_fmt_money(_safe_float(task.get('planned_qty'), None)) or '—'} | {_fmt_money(planned_amount) or '—'} {planned_currency}"
        if (
            planned_currency != "USD"
            and _safe_float(task.get("planned_amount_usd"), None) is not None
        ):
            plan_text += (
                f" / {_fmt_money(_safe_float(task.get('planned_amount_usd'), None))}$"
            )
        fact_spans = None
        if (
            _safe_float(task.get("actual_qty"), None) is not None
            or actual_amount is not None
        ):
            _qty_str = _fmt_money(_safe_float(task.get("actual_qty"), None)) or "—"
            _tail = f" | {_fmt_money(actual_amount) or '—'} {actual_currency}"
            if (
                actual_currency != "USD"
                and _safe_float(task.get("actual_amount_usd"), None) is not None
            ):
                _tail += f" / {_fmt_money(_safe_float(task.get('actual_amount_usd'), None))}$"
            fact_spans = [
                ft.TextSpan("Факт: ", ft.TextStyle(size=10, color=self._th_text)),
                ft.TextSpan(
                    _qty_str,
                    ft.TextStyle(
                        size=10, color=self._th_text, weight=ft.FontWeight.BOLD
                    ),
                ),
                ft.TextSpan(_tail, ft.TextStyle(size=10, color=self._th_text)),
            ]
        card = ft.Container(
            bgcolor=self._th_panel,
            border=ft.border.all(1, _BORDER),
            border_radius=10,
            padding=ft.padding.symmetric(horizontal=8, vertical=6),
            margin=ft.margin.only(bottom=5),
            on_click=lambda _e, r=row, m=month_anchor, t=dict(
                task
            ): self._open_task_dialog(r, m, t),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Container(
                                bgcolor=status_bg,
                                border_radius=20,
                                padding=ft.padding.symmetric(horizontal=8, vertical=3),
                                content=ft.Text(
                                    (
                                        self.t("schedule.status.ordered", "Замовлено")
                                        if str(task.get("status") or "") == "ordered"
                                        else self.t("schedule.status.not_ordered", "Не замовлено")
                                    ),
                                    size=9,
                                    color=status_fg,
                                    weight="bold",
                                ),
                                on_click=lambda e, t=dict(
                                    task
                                ): self._toggle_task_status(t),
                            ),
                            ft.Text(delivery_name, size=9, color=_MUTED),
                            ft.Row(expand=True),
                            ft.IconButton(
                                icon=ft.Icons.EDIT_OUTLINED,
                                icon_size=14,
                                tooltip="Редагувати задачу",
                                on_click=lambda e, r=row, m=month_anchor, t=dict(
                                    task
                                ): self._open_task_dialog(r, m, t),
                                style=ft.ButtonStyle(padding=ft.padding.all(0)),
                                width=20,
                                height=20,
                                visible=self._is_group_owned(row),
                            ),
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                icon_size=14,
                                tooltip="Видалити задачу",
                                on_click=lambda e, tid=str(
                                    task.get("id") or ""
                                ): self._delete_task(tid),
                                style=ft.ButtonStyle(padding=ft.padding.all(0)),
                                width=20,
                                height=20,
                                visible=self._is_group_owned(row),
                            ),
                        ],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text(
                        range_text or "Без дати", size=10, color=_TEXT, weight="bold"
                    ),
                    ft.Text(plan_text, size=10, color=_TEXT),
                    ft.Text(
                        spans=fact_spans
                        or [
                            ft.TextSpan(
                                "Факт: —", ft.TextStyle(size=10, color=self._th_text)
                            )
                        ],
                    ),
                ],
                spacing=2,
            ),
        )
        if not self._is_group_owned(row):
            return card
        task_snap = dict(task)
        row_snap = dict(row)

        def _on_drag_start(_e, t=task_snap, r=row_snap, m=month_anchor):
            self._drag_state = {"task": t, "src_row": r, "src_month": m}

        feedback = ft.Container(
            bgcolor=status_bg,
            border_radius=8,
            padding=ft.padding.symmetric(horizontal=10, vertical=6),
            content=ft.Text(
                range_text or "Без дати",
                size=10,
                color=status_fg,
                weight="bold",
                no_wrap=True,
            ),
        )
        placeholder = ft.Container(
            bgcolor=_BORDER,
            border_radius=10,
            height=100,
            margin=ft.margin.only(bottom=5),
        )
        return ft.Draggable(
            group="order_card",
            content=card,
            content_when_dragging=placeholder,
            content_feedback=feedback,
            on_drag_start=_on_drag_start,
        )

    def _start_drag_edge_scroll(self, direction: str) -> None:
        if self._drag_scroll_dir == direction:
            return
        self._drag_scroll_dir = direction
        pg = self._pg()
        if pg:
            with contextlib.suppress(Exception):
                pg.run_task(self._drag_scroll_tick)

    def _stop_drag_edge_scroll(self) -> None:
        self._drag_scroll_dir = ""

    async def _drag_scroll_tick(self) -> None:
        step = max(40, _MONTH_W // 3)
        while self._drag_scroll_dir:
            hr = self._h_scroll_row
            if hr:
                delta = -step if self._drag_scroll_dir == "left" else step
                with contextlib.suppress(Exception):
                    await hr.scroll_to(delta=delta, duration=120)
            await asyncio.sleep(0.12)

    def _archive_cell(self, row: dict, row_h: int) -> ft.Container:
        if str(row.get("item_type") or "").strip() == "inactive_header":
            archive_w = int(self._table_layout.get("archive_w") or _ARCHIVE_W)
            return ft.Container(
                width=archive_w,
                height=row_h,
                bgcolor=_SLATE,
                border=ft.border.all(1, _BORDER),
            )
        archived = self._row_archived_tasks(row)
        overdue_archived = [
            t
            for t in archived
            if str(t.get("status") or "not_ordered").strip().lower() != "ordered"
        ]
        txt_size = int(max(6, min(32, int(self._table_layout.get("text_size") or 10))))
        h_align = str(self._table_layout.get("text_h_align") or "center")
        txt_align = (
            ft.TextAlign.LEFT
            if h_align == "left"
            else (ft.TextAlign.RIGHT if h_align == "right" else ft.TextAlign.CENTER)
        )
        allow_wrap = bool(self._table_layout.get("text_wrap", True))

        def _open_months_for(tasks):
            new_anchors = []
            for t in tasks:
                anchor = str(t.get("month_anchor") or "")[:10]
                if anchor and anchor not in self._opened_archive_month_keys:
                    self._opened_archive_month_keys.append(anchor)
                    parsed = _parse_date(anchor)
                    if parsed:
                        new_anchors.append(_month_start(parsed))
            self._opened_archive_month_keys = [
                _month_key(v) for v in self._opened_archive_months()
            ]
            self._save_month_view_settings()
            if not new_anchors:
                return
            # When adding multiple months at once, _fast_add_archive_month computes
            # month_pos relative to the full NEW visible list (all months already in
            # _opened_archive_month_keys), but existing cells only contain previously
            # added months — the offset is wrong for each subsequent fast-add.
            # Full rebuild is the safe path whenever more than one month is added.
            if len(new_anchors) > 1 or not self._schedule_ui_ready:
                self._schedule_rebuild()
                return
            if not self._fast_add_archive_month(new_anchors[0]):
                self._schedule_rebuild()

        total_count = len(archived)
        overdue_count = len(overdue_archived)

        count_controls = [
            ft.GestureDetector(
                content=ft.Text(
                    str(total_count),
                    size=txt_size,
                    color=_MUTED,
                    weight="w500",
                ),
                on_tap=lambda _, a=archived: _open_months_for(a),
                mouse_cursor=ft.MouseCursor.CLICK,
            ),
        ]
        if overdue_count:
            count_controls += [
                ft.Text("/", size=txt_size, color=_MUTED),
                ft.GestureDetector(
                    content=ft.Text(
                        str(overdue_count),
                        size=txt_size,
                        color="red",
                        weight="bold",
                    ),
                    on_tap=lambda _, oa=overdue_archived: _open_months_for(oa),
                    mouse_cursor=ft.MouseCursor.CLICK,
                ),
            ]

        return ft.Container(
            width=_ARCHIVE_W,
            height=row_h,
            border=ft.border.all(1, _BORDER),
            bgcolor=_WARN if archived else _ALT,
            padding=ft.padding.all(_RIGHT_CELL_PAD),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text(self.t("schedule.col.archive", "Архів"), size=10, weight="bold", color=_TEXT),
                            ft.Row(expand=True),
                            ft.Row(count_controls, spacing=2, tight=True),
                        ],
                        spacing=4,
                    ),
                    ft.Text(
                        "Приховано" if archived else "Порожньо",
                        size=txt_size,
                        color=_MUTED,
                        text_align=txt_align,
                        no_wrap=(not allow_wrap),
                    ),
                ],
                spacing=4,
            ),
        )

    def _month_cell(self, row: dict, month_anchor: date, row_h: int) -> ft.DragTarget:
        if str(row.get("item_type") or "").strip() == "inactive_header":
            month_w = int(self._table_layout.get("month_w") or _MONTH_W)
            return ft.DragTarget(
                content=ft.Container(
                    width=month_w,
                    height=row_h,
                    bgcolor=_SLATE,
                    border=ft.border.all(1, _BORDER),
                ),
                group="task",
            )
        is_past_month = month_anchor < _month_start()
        show_archived_here = is_past_month or self._is_archive_month_opened(
            month_anchor
        )
        txt_size = int(max(6, min(32, int(self._table_layout.get("text_size") or 10))))
        h_align = str(self._table_layout.get("text_h_align") or "center")
        txt_align = (
            ft.TextAlign.LEFT
            if h_align == "left"
            else (ft.TextAlign.RIGHT if h_align == "right" else ft.TextAlign.CENTER)
        )
        tasks = self._row_tasks_for_month(
            row, month_anchor, include_archived=show_archived_here
        )

        tasks_col = ft.Column(
            [self._task_card(row, task, month_anchor) for task in tasks],
            spacing=4,
            scroll=ft.ScrollMode.ALWAYS,
            height=max(28, row_h - 36),
        )

        dst_row = dict(row)
        dst_month = month_anchor
        _normal_bg = _ALT if is_past_month else _PANEL
        _hover_bg = "#DBEAFE"
        _normal_border = ft.border.all(1, _BORDER)
        _hover_border = ft.border.all(2, "#3B82F6")

        cell = ft.Container(
            width=_MONTH_W,
            height=row_h,
            border=_normal_border,
            bgcolor=_normal_bg,
            padding=ft.padding.all(_RIGHT_CELL_PAD),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text(
                                _month_title(month_anchor),
                                size=txt_size,
                                color=_MUTED,
                                text_align=txt_align,
                                no_wrap=True,
                            ),
                            ft.Row(expand=True),
                            ft.IconButton(
                                icon=ft.Icons.ADD_CIRCLE_OUTLINE,
                                icon_size=16,
                                tooltip="Додати задачу",
                                on_click=lambda _e, r=row, m=month_anchor: self._open_task_dialog(
                                    r, m
                                ),
                                style=ft.ButtonStyle(padding=ft.padding.all(0)),
                                width=22,
                                height=22,
                                visible=self._is_group_owned(row),
                            ),
                        ],
                        spacing=2,
                    ),
                    tasks_col,
                ],
                spacing=4,
            ),
        )

        # Store refs after both tasks_col and cell are built so cell.update() is available.
        # Two keys: gid-based (for existing groups) and row_key-based (for virtual rows).
        _ref_gid = str(row.get("group_id") or "").strip()
        _ref_rk = str(row.get("key") or "").strip()
        _mk = _month_key(month_anchor)
        if _ref_gid:
            _k = f"{_ref_gid}:{_mk}"
            self._month_cell_tasks_col_refs[_k] = tasks_col
            self._month_cell_container_refs[_k] = cell
        if _ref_rk:
            _rk = f"rk:{_ref_rk}:{_mk}"
            self._month_cell_tasks_col_refs[_rk] = tasks_col
            self._month_cell_container_refs[_rk] = cell

        def _restore_cell():
            cell.bgcolor = _normal_bg
            cell.border = _normal_border
            with contextlib.suppress(Exception):
                cell.update()

        def _on_will_accept(e):
            cell.bgcolor = _hover_bg
            cell.border = _hover_border
            with contextlib.suppress(Exception):
                cell.update()

        def _on_leave(e):
            _restore_cell()

        def _on_drop(e, r=dst_row, m=dst_month):
            _restore_cell()
            state = dict(self._drag_state)
            if not state:
                return
            src_task = state.get("task") or {}
            src_row = state.get("src_row") or {}
            src_month = state.get("src_month")
            src_gid = str(src_row.get("group_id") or "").strip()
            dst_gid = str(r.get("group_id") or "").strip()
            if src_gid == dst_gid and src_month == m:
                return
            self._open_drag_drop_dialog(src_task, src_row, src_month, r, m)

        return ft.DragTarget(
            group="order_card",
            content=cell,
            on_will_accept=_on_will_accept,
            on_accept=_on_drop,
            on_leave=_on_leave,
        )

    def _fast_refresh_month_cell_tasks(
        self,
        row: dict,
        month_anchor: date,
        *,
        cell_lookup_row: dict | None = None,
    ) -> None:
        """In-place update of one month cell's task list after a save or delete.

        cell_lookup_row: original build-time row dict (has 'key' field) used as
        fallback lookup for virtual rows whose gid changed after _save_group.
        """
        _mk = _month_key(month_anchor)
        gid = str(row.get("group_id") or "").strip()
        gid_key = f"{gid}:{_mk}"
        tasks_col = self._month_cell_tasks_col_refs.get(gid_key)
        cell_ref = self._month_cell_container_refs.get(gid_key)

        if tasks_col is None:
            # Fallback: look up by build-time row key (covers virtual rows).
            _lookup = cell_lookup_row or row
            rk = str(_lookup.get("key") or "").strip()
            if rk:
                rk_key = f"rk:{rk}:{_mk}"
                tasks_col = self._month_cell_tasks_col_refs.get(rk_key)
                cell_ref = self._month_cell_container_refs.get(rk_key)
                if tasks_col is not None and gid:
                    # Register gid-based ref so future calls (e.g. _save_task) find it.
                    self._month_cell_tasks_col_refs[gid_key] = tasks_col
                    self._month_cell_container_refs[gid_key] = cell_ref

        if tasks_col is None:
            self._schedule_rebuild()
            return

        is_past_month = month_anchor < _month_start()
        show_archived_here = is_past_month or self._is_archive_month_opened(
            month_anchor
        )
        try:
            tasks = self._row_tasks_for_month(
                row, month_anchor, include_archived=show_archived_here
            )
            tasks_col.controls = [self._task_card(row, t, month_anchor) for t in tasks]
            # Prefer cell.update() — it is proven to work (hover events use it) and
            # forces Flutter to process the full cell subtree including tasks_col.
            if cell_ref is not None:
                cell_ref.update()
            else:
                tasks_col.update()
        except Exception:
            self._schedule_rebuild()

    def _left_row(self, row: dict, idx: int, row_h: int) -> ft.Container:
        if str(row.get("item_type") or "").strip() == "inactive_header":
            n = int(row.get("inactive_count") or 0)
            icon = (
                ft.Icons.EXPAND_MORE
                if not self._inactive_section_collapsed
                else ft.Icons.CHEVRON_RIGHT
            )
            left_w = _LEFT_CB_W + _LEFT_STATUS_W + _LEFT_NAME_W
            return ft.Container(
                width=left_w,
                height=row_h,
                bgcolor=_SLATE,
                border=ft.border.all(1, _BORDER),
                on_click=lambda _: self._toggle_inactive_section(),
                content=ft.Row(
                    [
                        ft.Icon(icon, size=16, color=_MUTED),
                        ft.Text(
                            f"Неактивні ({n})",
                            size=11,
                            color=_MUTED,
                            weight="bold",
                        ),
                    ],
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.padding.only(left=12),
            )
        selected = self._sel_key(row) in self._selected
        is_inactive = self._is_row_inactive(row)
        is_note = str(row.get("item_type") or "").strip() == "note"
        label = self._group_display_label(row)
        txt_size = int(max(6, min(32, int(self._table_layout.get("text_size") or 10))))
        h_align = str(self._table_layout.get("text_h_align") or "center")
        txt_align = (
            ft.TextAlign.LEFT
            if h_align == "left"
            else (ft.TextAlign.RIGHT if h_align == "right" else ft.TextAlign.CENTER)
        )
        col_cross_align = (
            ft.CrossAxisAlignment.START
            if h_align == "left"
            else (
                ft.CrossAxisAlignment.END
                if h_align == "right"
                else ft.CrossAxisAlignment.CENTER
            )
        )
        allow_wrap = bool(self._table_layout.get("text_wrap", True))

        def _toggle(e, key=self._sel_key(row), _idx=idx):
            if bool(getattr(e.control, "value", False)):
                self._selected.add(key)
            else:
                self._selected.discard(key)
            # Fast update: only bgcolor of the left row changes on selection toggle.
            if _idx < len(self._left_rows_refs):
                left_ctr = self._left_rows_refs[_idx]
                _is_inact = self._is_row_inactive(row)
                _is_sel = key in self._selected
                left_ctr.bgcolor = (
                    _ERR
                    if _is_inact
                    else (_SOFT if _is_sel else (_ALT if _idx % 2 else _PANEL))
                )
                with contextlib.suppress(Exception):
                    left_ctr.update()
            else:
                self._schedule_rebuild()

        def _on_inactive_change(e, r=row, _idx=idx):
            new_val = bool(getattr(e.control, "value", False))
            self._set_row_inactive(r, new_val)
            if new_val:
                # Collapse inactive section so the newly inactive row is hidden under the header
                self._inactive_section_collapsed = True
            self._schedule_rebuild(show_stub=False)

        inactive_cb = ft.Checkbox(
            value=is_inactive,
            label=" ",
            label_style=ft.TextStyle(size=8, color=_ERR_FG if is_inactive else _MUTED),
            visible=not self.read_only,
        )
        inactive_cb.on_change = _on_inactive_change

        btn_row = ft.Row(
            [
                ft.IconButton(
                    icon=(
                        ft.Icons.STICKY_NOTE_2_OUTLINED
                        if is_note
                        else ft.Icons.EDIT_OUTLINED
                    ),
                    icon_size=13,
                    icon_color=_ACCENT if is_note else None,
                    tooltip=self.t("schedule.tooltip.edit_note", "Редагувати замітку") if is_note else self.t("media.dlg.custom_name", "Своя назва"),
                    visible=not self.read_only,
                    on_click=(
                        (lambda _e, r=row: self._open_note_row_dialog(r))
                        if is_note
                        else (lambda _e, r=row: self._open_group_label_edit_dialog(r))
                    ),
                    style=ft.ButtonStyle(padding=ft.padding.all(0)),
                    width=20,
                    height=20,
                ),
            ],
            spacing=0,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        _os_pids = [str(x).strip() for x in (row.get("product_ids") or []) if str(x).strip()]
        if len(_os_pids) == 1:
            from views.product_quick_view import product_icon_btn as _osiqb
            btn_row.controls.insert(0, _osiqb(
                _os_pids[0], self.content_area, self.db, self.user_data, size=13
            ))

        if is_inactive:
            row_bg = _ERR
        elif selected:
            row_bg = _SOFT
        else:
            row_bg = _ALT if idx % 2 else _PANEL

        status_col = ft.Container(
            width=_LEFT_STATUS_W,
            height=row_h,
            border=ft.border.only(left=ft.BorderSide(1, _BORDER)),
            alignment=ft.Alignment(0, 0),
            content=ft.Column(
                [
                    inactive_cb,
                ],
                spacing=0,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                tight=True,
            ),
        )

        return ft.Container(
            height=row_h,
            bgcolor=row_bg,
            border=ft.border.all(1, _BORDER),
            content=ft.Row(
                [
                    ft.Container(
                        width=_LEFT_CB_W,
                        alignment=ft.Alignment(0, 0),
                        content=ft.Checkbox(value=selected, on_change=_toggle),
                    ),
                    status_col,
                    ft.Container(
                        width=_LEFT_NAME_W,
                        height=row_h,
                        padding=ft.padding.symmetric(horizontal=6),
                        border=ft.border.only(left=ft.BorderSide(1, _BORDER)),
                        content=ft.Column(
                            [
                                ft.Container(expand=True),
                                ft.Column(
                                    [
                                        ft.Container(
                                            width=_LEFT_NAME_W - 20,
                                            tooltip=(
                                                ft.Tooltip(message=str(label), wait_duration=0)
                                                if label else None
                                            ),
                                            content=ft.Text(
                                                str(label or ""),
                                                size=txt_size,
                                                color=_ERR_FG if is_inactive else _TEXT,
                                                text_align=txt_align,
                                                no_wrap=(not allow_wrap),
                                                max_lines=(None if allow_wrap else 1),
                                                overflow=(
                                                    ft.TextOverflow.VISIBLE
                                                    if allow_wrap
                                                    else ft.TextOverflow.ELLIPSIS
                                                ),
                                                selectable=True,
                                            ),
                                        ),
                                        (
                                            ft.TextButton(
                                                f"{len(row.get('product_ids') or [])} позицій",
                                                on_click=lambda _e, r=row: self._show_group_items(
                                                    r
                                                ),
                                                style=ft.ButtonStyle(
                                                    padding=ft.padding.symmetric(
                                                        horizontal=4, vertical=0
                                                    ),
                                                    text_style=ft.TextStyle(size=10),
                                                    color=_ACCENT,
                                                ),
                                            )
                                            if len(row.get("product_ids") or []) > 1
                                            else ft.Text(
                                                "1 позиція", size=10, color=_MUTED
                                            )
                                        ),
                                        btn_row,
                                    ],
                                    spacing=4,
                                    horizontal_alignment=col_cross_align,
                                ),
                                ft.Container(expand=True),
                            ],
                            spacing=0,
                        ),
                    ),
                ],
                spacing=0,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _table_header_left(self) -> ft.Container:
        dd = ft.Dropdown(
            value=self._normalize_display_mode(self._display_mode),
            width=180,
            text_size=11,
            border_radius=8,
            dense=True,
            content_padding=ft.padding.symmetric(horizontal=8, vertical=6),
            options=[ft.dropdown.Option(k, v) for k, v in self._display_options()],
        )

        def _change(e):
            # Resolve mode robustly across Flet variants: some builds expose the
            # selected key via control.value, while e.data may contain a non-key
            # payload. Prefer control/dropdown value first.
            valid_modes = {k for k, _ in self._display_options()}
            candidates = [
                str(getattr(getattr(e, "control", None), "value", "") or "").strip(),
                str(getattr(e, "data", "") or "").strip(),
                str(dd.value or "").strip(),
            ]
            picked_mode = ""
            for cand in candidates:
                if not cand:
                    continue
                norm = self._normalize_display_mode(cand)
                if norm in valid_modes:
                    picked_mode = norm
                    break
            if not picked_mode:
                picked_mode = self._normalize_display_mode(self._display_mode)
            self._display_mode = picked_mode
            dd.value = self._display_mode
            try:
                self.db.set_manager_ui_preference(
                    self.manager_id,
                    "order_schedule_display_mode",
                    self._display_mode,
                )
            except Exception:
                pass
            self._schedule_rebuild()

        dd.on_change = _change
        # Some Flet builds emit selection via on_select for Dropdown.
        dd.on_select = _change
        return ft.Container(
            bgcolor=_HDR,
            border=ft.border.all(1, _BORDER),
            border_radius=ft.border_radius.only(top_left=10),
            height=72,
            content=ft.Row(
                [
                    ft.Container(width=_LEFT_CB_W),
                    ft.Container(
                        width=_LEFT_STATUS_W,
                        border=ft.border.only(left=ft.BorderSide(1, _BORDER)),
                        alignment=ft.Alignment(0, 0),
                        content=ft.Text(
                            self.t("schedule.col.state", "Стан"),
                            size=11,
                            weight="bold",
                            color=_TEXT,
                        ),
                    ),
                    ft.Container(
                        width=_LEFT_NAME_W,
                        padding=ft.padding.only(left=8, right=8, top=0, bottom=2),
                        border=ft.border.only(left=ft.BorderSide(1, _BORDER)),
                        content=ft.Column(
                            [
                                ft.Row(
                                    [
                                        ft.Text(
                                            self.t("schedule.col.name", "Позиція розкладу"),
                                            size=11,
                                            weight="bold",
                                            color=_TEXT,
                                        ),
                                        ft.IconButton(
                                            icon=(
                                                ft.Icons.ARROW_UPWARD
                                                if (
                                                    self._col_sort_key == "name"
                                                    and self._col_sort_dir == 1
                                                )
                                                else (
                                                    ft.Icons.ARROW_DOWNWARD
                                                    if (
                                                        self._col_sort_key == "name"
                                                        and self._col_sort_dir == 2
                                                    )
                                                    else ft.Icons.UNFOLD_MORE
                                                )
                                            ),
                                            icon_size=12,
                                            icon_color=(
                                                _TEXT
                                                if self._col_sort_key == "name"
                                                else "#AAAAAA"
                                            ),
                                            on_click=lambda _: self._col_sort_cycle(
                                                "name"
                                            ),
                                            tooltip=self.t(
                                                "schedule.col.name_sort_tooltip",
                                                "Сортувати за назвою (А-Я / Я-А)",
                                            ),
                                            style=ft.ButtonStyle(
                                                padding=ft.padding.all(0)
                                            ),
                                            width=20,
                                            height=20,
                                        ),
                                    ],
                                    spacing=2,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                                dd,
                            ],
                            spacing=2,
                        ),
                    ),
                ],
                spacing=0,
            ),
        )

    def _table_header_right(
        self,
        visible_months: list[date] | None = None,
        show_archive_column: bool | None = None,
    ) -> ft.Container:
        months = list(visible_months or self._visible_month_anchors())
        show_archive = (
            self._show_archive_column
            if show_archive_column is None
            else bool(show_archive_column)
        )
        ordered_columns = self._ordered_schedule_right_columns(show_archive)
        chips = []
        for month in self._opened_archive_months():
            key = _month_key(month)
            chips.append(
                ft.Container(
                    bgcolor=self._th_chip_bg,
                    border_radius=999,
                    padding=ft.padding.only(left=8, right=4, top=2, bottom=2),
                    content=ft.Row(
                        [
                            ft.Text(_month_title(month, self.t), size=8, color=self._th_icon),
                            ft.IconButton(
                                icon=ft.Icons.CLOSE,
                                icon_size=10,
                                icon_color=self._th_text,
                                on_click=lambda _e, k=key: self._remove_archive_month(
                                    k
                                ),
                                style=ft.ButtonStyle(padding=ft.padding.all(0)),
                                width=16,
                                height=16,
                            ),
                        ],
                        spacing=2,
                        tight=True,
                    ),
                )
            )
        cells: list[ft.Control] = []
        for cid in ordered_columns:
            if cid == "archive" and show_archive:
                cells.append(
                    ft.Container(
                        width=_ARCHIVE_W,
                        padding=ft.padding.symmetric(horizontal=8, vertical=8),
                        content=ft.Column(
                            [
                                ft.Row(
                                    [
                                        ft.Text(
                                            self.t("schedule.col.archive", "Архів"),
                                            size=11,
                                            weight="bold",
                                            color=_TEXT,
                                        ),
                                        ft.Row(expand=True),
                                        ft.IconButton(
                                            icon=ft.Icons.ADD_CIRCLE_OUTLINE,
                                            icon_size=16,
                                            tooltip=self.t("schedule.dlg.open_archive_month", "Відкрити місяць з архіву"),
                                            on_click=self._open_archive_month_picker,
                                            style=ft.ButtonStyle(
                                                padding=ft.padding.all(0)
                                            ),
                                            width=20,
                                            height=20,
                                        ),
                                        ft.IconButton(
                                            icon=ft.Icons.KEYBOARD_DOUBLE_ARROW_LEFT,
                                            icon_size=14,
                                            icon_color=_MUTED,
                                            tooltip=self.t("schedule.tooltip.hide_archive", "Сховати всі архівні місяці"),
                                            on_click=self._close_all_archive_months,
                                            style=ft.ButtonStyle(
                                                padding=ft.padding.all(0)
                                            ),
                                            width=20,
                                            height=20,
                                            visible=bool(chips),
                                        ),
                                    ],
                                    spacing=4,
                                ),
                                (
                                    make_scrollable_chip_row(
                                        chips, spacing=4, run_spacing=4
                                    )
                                    if is_mobile(self._pg())
                                    else ft.Row(
                                        chips,
                                        spacing=4,
                                        run_spacing=4,
                                        wrap=True,
                                    )
                                ),
                            ],
                            spacing=4,
                        ),
                    )
                )
            elif cid == "months":
                for _month in months:
                    _mk = _month_key(_month)
                    _active = self._col_sort_key == _mk
                    cells.append(
                        ft.Container(
                            width=_MONTH_W,
                            padding=ft.padding.symmetric(horizontal=8, vertical=4),
                            on_click=lambda _, k=_mk: self._col_sort_cycle(k),
                            tooltip=self.t(
                                "schedule.col.months_sort_tooltip",
                                "Сортувати по останній даті замовлення",
                            ),
                            content=ft.Column(
                                [
                                    ft.Text(
                                        _month_title(_month, self.t),
                                        size=11,
                                        weight="bold",
                                        color=_TEXT,
                                        text_align=ft.TextAlign.CENTER,
                                        no_wrap=True,
                                    ),
                                    ft.Row(
                                        [
                                            ft.Text(
                                                self.t("schedule.col.deadline", "дедлайн ↑"),
                                                size=9,
                                                color=_MUTED,
                                                text_align=ft.TextAlign.CENTER,
                                            ),
                                            ft.Icon(
                                                (
                                                    ft.Icons.ARROW_UPWARD
                                                    if (
                                                        _active
                                                        and self._col_sort_dir == 1
                                                    )
                                                    else (
                                                        ft.Icons.ARROW_DOWNWARD
                                                        if (
                                                            _active
                                                            and self._col_sort_dir == 2
                                                        )
                                                        else ft.Icons.UNFOLD_MORE
                                                    )
                                                ),
                                                size=10,
                                                color=_TEXT if _active else "#AAAAAA",
                                            ),
                                        ],
                                        spacing=2,
                                        alignment=ft.MainAxisAlignment.CENTER,
                                    ),
                                ],
                                spacing=2,
                                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                alignment=ft.MainAxisAlignment.CENTER,
                            ),
                        )
                    )
        return ft.Container(
            bgcolor=_HDR,
            border=ft.border.all(1, _BORDER),
            border_radius=ft.border_radius.only(top_right=10),
            height=72,
            content=ft.Row(cells, spacing=0),
        )

    def _build_filter_bar(self) -> ft.Container:
        def _mk_cat_chip(cid: str, label: str, active: bool) -> ft.Container:
            def _toggle(_):
                if cid == "":
                    self._filter_category_ids.clear()
                elif cid in self._filter_category_ids:
                    self._filter_category_ids.discard(cid)
                else:
                    self._filter_category_ids.add(cid)
                self._selected.clear()
                self._schedule_rebuild()

            return ft.Container(
                bgcolor=self._th_chip_bg if active else self._th_panel,
                border=ft.border.all(1, self._th_icon if active else self._th_border),
                border_radius=999,
                padding=ft.padding.symmetric(horizontal=10, vertical=4),
                on_click=_toggle,
                ink=True,
                content=ft.Text(
                    label, size=11, color=self._th_icon if active else self._th_text
                ),
            )

        sorted_cats = sorted(
            [
                c
                for c in (self._categories or [])
                if isinstance(c, dict) and c.get("id")
            ],
            key=lambda x: str(x.get("name") or "").lower(),
        )
        cat_chips: list = [_mk_cat_chip("", "Всі", not self._filter_category_ids)]
        for _c in sorted_cats:
            _cid = str(_c.get("id") or "")
            cat_chips.append(
                _mk_cat_chip(
                    _cid,
                    str(_c.get("name") or "")[:30],
                    _cid in self._filter_category_ids,
                )
            )
        cat_filter = ft.Row(cat_chips, spacing=4, scroll=ft.ScrollMode.HIDDEN)

        sort_reset_btn = None
        if self._col_sort_key:

            def _reset_sort_osc(_e=None):
                self._col_sort_key = ""
                self._col_sort_dir = 0
                self._schedule_rebuild()

            sort_reset_btn = ft.TextButton(
                "Скинути сортування",
                icon=ft.Icons.SORT,
                on_click=_reset_sort_osc,
                style=ft.ButtonStyle(text_style=ft.TextStyle(size=11)),
                tooltip=self.t("media.tooltip.reset_col_sort", "Скинути сортування колонки"),
            )

        return ft.Container(
            bgcolor=_PANEL,
            border=ft.border.all(1, "#E2E8F0"),
            border_radius=12,
            padding=12,
            content=ft.Row(
                [
                    *([sort_reset_btn] if sort_reset_btn is not None else []),
                    ft.Container(
                        bgcolor=_SOFT,
                        border_radius=999,
                        padding=ft.padding.symmetric(horizontal=12, vertical=8),
                        content=ft.Text(
                            f"Регіон: {self.region_label}",
                            size=12,
                            color=_ACCENT,
                            weight="bold",
                        ),
                    ),
                    cat_filter,
                    (
                        ft.Container(
                            bgcolor=_WARN,
                            border=ft.border.all(1, _BORDER),
                            border_radius=999,
                            padding=ft.padding.symmetric(horizontal=10, vertical=6),
                            content=ft.Text(
                                (
                                    "Фільтр: Треба замовити"
                                    if self._quick_task_filter == "need_to_order"
                                    else "Фільтр: Прострочені"
                                ),
                                size=11,
                                color=_WARN_FG,
                                weight="bold",
                            ),
                        )
                        if self._quick_task_filter in {"need_to_order", "overdue"}
                        else ft.Container(width=0, height=0)
                    ),
                    ft.Row(expand=True),
                    ft.OutlinedButton(
                        self.t("schedule.dlg.budget", "Бюджет"),
                        icon=ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED,
                        height=35,
                        style=ft.ButtonStyle(
                            text_style=ft.TextStyle(size=11),
                            padding=ft.padding.symmetric(horizontal=10, vertical=2),
                        ),
                        on_click=self._open_budget_dialog,
                    ),
                    ft.OutlinedButton(
                        self.t("payments.dlg.table_title", "Таблиця"),
                        icon=ft.Icons.TABLE_CHART_OUTLINED,
                        height=35,
                        style=ft.ButtonStyle(
                            text_style=ft.TextStyle(size=11),
                            padding=ft.padding.symmetric(horizontal=10, vertical=2),
                        ),
                        on_click=self._open_table_dialog,
                    ),
                    ft.OutlinedButton(
                        self.t("schedule.add_note_btn", "+ Замітка"),
                        icon=ft.Icons.STICKY_NOTE_2_OUTLINED,
                        height=35,
                        visible=not self.read_only,
                        style=ft.ButtonStyle(
                            text_style=ft.TextStyle(size=11),
                            padding=ft.padding.symmetric(horizontal=10, vertical=2),
                        ),
                        on_click=lambda _: self._open_note_row_dialog(),
                    ),
                    ft.ElevatedButton(
                        self.t("schedule.merge_selected", "Об'єднати обрані"),
                        icon=ft.Icons.MERGE_TYPE,
                        on_click=self._merge_selected,
                        disabled=len(self._selected) < 2,
                        visible=not self.read_only,
                        style=ft.ButtonStyle(bgcolor=_ACCENT, color="white"),
                    ),
                    ft.OutlinedButton(
                        self.t("schedule.split_selected", "Роз'єднати обрані"),
                        icon=ft.Icons.CALL_SPLIT,
                        on_click=self._split_selected,
                        disabled=len(self._selected) < 1,
                        visible=not self.read_only,
                    ),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _fast_add_archive_month(self, month_anchor: date) -> bool:
        """Insert one archive month column in-place — no full rebuild needed.
        Returns True on success, False if a full rebuild is required instead."""
        try:
            rows = self._last_built_rows
            if (
                not rows
                or not self._right_rows_refs
                or self._right_body_content_ref is None
            ):
                return False
            if len(rows) != len(self._right_rows_refs):
                return False
            show_archive = bool(self._show_archive_column)
            visible_months = self._visible_month_anchors()  # already includes new month
            row_h = _ROW_BASE_H
            month_key = _month_key(month_anchor)
            month_pos = next(
                (i for i, m in enumerate(visible_months) if _month_key(m) == month_key),
                None,
            )
            if month_pos is None:
                return False
            insert_idx = (1 if show_archive else 0) + month_pos
            for row_idx, row in enumerate(rows):
                right_cells_row = self._right_rows_refs[row_idx].content
                new_cell = self._month_cell(row, month_anchor, row_h)
                right_cells_row.controls.insert(insert_idx, new_cell)
            new_inner_w = (0 if not show_archive else _ARCHIVE_W) + len(
                visible_months
            ) * _MONTH_W
            self._right_body_content_ref.width = new_inner_w
            self._right_inner_w = new_inner_w
            hdr_child = getattr(self, "_right_header_stack_child", None)
            if hdr_child is not None:
                hdr_child.width = new_inner_w
                hdr_child.content = self._table_header_right(
                    visible_months=visible_months,
                    show_archive_column=show_archive,
                )
                with contextlib.suppress(Exception):
                    hdr_child.update()
            self.content_area.update()
            return True
        except Exception:
            return False

    def _fast_remove_archive_month_at(self, old_month_pos: int) -> bool:
        """Remove one archive month column in-place at the given old position index.
        Returns True on success, False if a full rebuild is required instead."""
        try:
            if not self._right_rows_refs or self._right_body_content_ref is None:
                return False
            show_archive = bool(self._show_archive_column)
            remove_idx = (1 if show_archive else 0) + old_month_pos
            for row_container in self._right_rows_refs:
                right_cells_row = row_container.content
                if remove_idx < len(right_cells_row.controls):
                    right_cells_row.controls.pop(remove_idx)
            visible_months = self._visible_month_anchors()  # already updated
            new_inner_w = (0 if not show_archive else _ARCHIVE_W) + len(
                visible_months
            ) * _MONTH_W
            self._right_body_content_ref.width = new_inner_w
            self._right_inner_w = new_inner_w
            hdr_child = getattr(self, "_right_header_stack_child", None)
            if hdr_child is not None:
                hdr_child.width = new_inner_w
                hdr_child.content = self._table_header_right(
                    visible_months=visible_months,
                    show_archive_column=show_archive,
                )
                with contextlib.suppress(Exception):
                    hdr_child.update()
            self.content_area.update()
            return True
        except Exception:
            return False

    def _build_sync(self):
        self._load_workspace_state()
        self._load_workspace_outbox()
        self._load_ops_outbox()
        self._header_loading_indicator = create_orders_header_loading_indicator(
            visible=True
        )
        temp_header = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.EVENT_NOTE, size=24, color=self._th_icon),
                    ft.Column(
                        [
                            ft.Text(
                                self.t("schedule.title", "Розклад Замовлень"), size=22, weight="bold", color=_TEXT
                            ),
                            ft.Text(
                                f"{self.region_label} · {self.manager_name}",
                                size=12,
                                color=_MUTED,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                                max_lines=1,
                            ),
                        ],
                        spacing=1,
                    ),
                    self._header_loading_indicator,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.only(bottom=6),
            border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
        )
        self.content_area.controls.clear()
        self.content_area.controls.append(temp_header)
        self.content_area.update()
        # Load data in background so the spinner renders immediately
        pg = self._pg()
        if pg and hasattr(pg, "run_task"):
            with contextlib.suppress(Exception):
                pg.run_task(self._async_initial_build)
        else:
            self._rebuild(reload=True)
            if not self._workspace_sync_loop_started:
                pg2 = self._pg()
                if pg2 and hasattr(pg2, "run_task"):
                    self._workspace_sync_loop_started = True
                    with contextlib.suppress(Exception):
                        pg2.run_task(self._workspace_sync_loop)

    def _set_loading_text(self, text: str) -> None:
        """Update the loading indicator text (safe to call from any thread)."""
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

    def _hide_spinner(self) -> None:
        """Always-safe: hide the loading indicator and update it."""
        try:
            ind = self._header_loading_indicator
            if ind is not None and ind.visible:
                ind.visible = False
                with contextlib.suppress(Exception):
                    ind.update()
        except Exception:
            pass

    async def _async_initial_build(self):
        """Load data in a background thread, then rebuild UI — also in a thread."""
        try:
            await asyncio.to_thread(self._load)
        except Exception:
            pass
        try:
            await asyncio.to_thread(self._rebuild, False)
        except Exception:
            try:
                self._rebuild(reload=False)
            except Exception:
                self._hide_spinner()
        if not self._workspace_sync_loop_started:
            pg = self._pg()
            if pg and hasattr(pg, "run_task"):
                self._workspace_sync_loop_started = True
                with contextlib.suppress(Exception):
                    pg.run_task(self._workspace_sync_loop)

    def build(self):
        global _BG, _PANEL, _ALT, _BORDER, _HDR, _TEXT, _MUTED, _ACCENT, _SOFT, _SLATE
        global _WARN, _WARN_FG
        from views import themes as _th_mod

        _th_b = _th_mod.get_active_theme(self.manager_id)
        _BG = _th_b["bg"]
        _PANEL = _th_b["panel"]
        _ALT = _th_b.get("alt", _th_b["panel"])
        _BORDER = _th_b["border"]
        _HDR = _th_b["hdr"]
        _TEXT = _th_b["text"]
        _MUTED = _th_b["muted"]
        _ACCENT = _th_b["icon_color"]
        _SOFT = _th_b.get("soft", _th_b["panel"])
        _SLATE = _th_b.get("alt", _th_b["panel"])
        _WARN = _th_b["warn_bg"]
        _WARN_FG = _th_b["warn"]
        if self._general_mode and self._general_manager_ids:

            import flet as _ft

            from views.general_wrapper import build_general_sections

            _cid = str(self.user_data.get("company_id") or "").strip()
            _db = self.db
            _ar = self.active_region
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

            # Count schedule groups per manager (merged groups = 1)
            def _count_products(mid, _db=_db, _cid=_cid, _rid=_rid):
                groups = (
                    _db.get_order_schedule_groups(_cid, manager_id=mid, region_id=_rid)
                    or []
                )
                return len(groups)

            _table_btn = _ft.OutlinedButton(
                self.t("payments.dlg.table_title", "Таблиця"),
                icon=_ft.Icons.TABLE_CHART_OUTLINED,
                height=35,
                style=_ft.ButtonStyle(
                    text_style=_ft.TextStyle(size=11),
                    padding=_ft.padding.symmetric(horizontal=10, vertical=2),
                ),
                on_click=lambda _: self._open_table_dialog(),
            )
            build_general_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={"active_region": self.active_region},
                count_getter=_count_products,
                count_label="Товарів:",
                top_controls=[_ft.Row([_table_btn], spacing=8)],
            )
            return

        # All regions → region sub-groups (single-user mode)
        if not self.active_region:
            from views.general_wrapper import build_region_sections

            _cid2, _db2, _mid2 = self.company_id, self.db, self.manager_id
            # Count schedule groups per region (merged groups = 1)
            _groups2 = _db2.get_order_schedule_groups(_cid2, manager_id=_mid2) or []
            _rcounts2: dict[str, int] = {}
            for _g2 in _groups2:
                if not isinstance(_g2, dict):
                    continue
                _r2 = str(_g2.get("region_id") or "").strip()
                if _r2:
                    _rcounts2[_r2] = _rcounts2.get(_r2, 0) + 1

            def _count_rcos(rid, _c=_rcounts2):
                return _c.get(rid, 0)

            build_region_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={},
                count_getter=_count_rcos,
                region_kwarg_name="active_region",
                count_label="Товарів:",
            )
            return

        self._build_sync()

    def _render_loading_stub(self):
        if not self.content_area:
            return
        self._header_loading_indicator = create_orders_header_loading_indicator(
            visible=True
        )
        # Clean up h-scroll overlay buttons from any previous render.
        pg = getattr(self, "_pg", lambda: None)()
        if pg:
            for _b in getattr(pg, "_catman_hscroll_btns", []):
                with contextlib.suppress(Exception):
                    pg.overlay.remove(_b)
            pg._catman_hscroll_btns = []
        # Keep spinner in content_area (never in sticky) so that content_area.update()
        # at the end of _rebuild hides the spinner and shows the table in ONE Flutter
        # render cycle — eliminating the gap where sticky (small) renders before body (large).
        spinner_row = ft.Row(
            [self._header_loading_indicator],
            alignment=ft.MainAxisAlignment.CENTER,
        )
        self.content_area.controls.clear()
        self.content_area.controls.append(spinner_row)
        try:
            self.content_area.update()
        except Exception:
            pass

    def _rebuild(self, reload: bool = False):
        # ── Theme color shadowing ──────────────────────────────────────────────
        from views.themes import get_active_theme as _get_theme

        _th = _get_theme(self.manager_id)
        _BG = _th["bg"]
        _PANEL = _th["panel"]
        _ALT = _th["alt"]
        _BORDER = _th["border"]
        _HDR = _th["hdr"]
        _TEXT = _th["text"]
        _MUTED = _th["muted"]
        _ACCENT = _th["icon_color"]
        _SOFT = _th["soft"]
        _WARN = _th["warn_bg"]
        _WARN_FG = _th["warn"]
        _OK = _th["ok_bg"]
        _OK_FG = _th["ok"]
        _ERR = _th["err_bg"]
        _ERR_FG = _th["err"]
        _SLATE = _th["alt"]
        # ──────────────────────────────────────────────────────────────────────
        if self._header_loading_indicator is None:
            self._header_loading_indicator = create_orders_header_loading_indicator(
                visible=False
            )
        if reload or not self._initial_loaded:
            self._load()
        valid_modes = {k for k, _ in self._display_options()}
        self._display_mode = self._normalize_display_mode(self._display_mode)
        if self._display_mode not in valid_modes:
            self._display_mode = "name"
        rows = self._build_rows()
        quick_mode = str(self._quick_task_filter or "").strip().lower()
        quick_filter_modes = {"need_to_order", "overdue"}
        if quick_mode in quick_filter_modes:
            rows = [r for r in rows if self._row_has_matching_quick_filter(r)]
        if self._search_text_query:
            rows = [r for r in rows if self._row_matches_text_search(r)]
        visible_months = self._visible_month_anchors()
        if quick_mode in quick_filter_modes:
            visible_months = self._visible_months_for_quick_filter(rows)
        show_archive_column = bool(self._show_archive_column) and (
            quick_mode not in quick_filter_modes
        )
        left_rows = []
        right_rows = []
        _total_rows = len(rows)
        if _total_rows:
            self._set_loading_text(f"0/{_total_rows}")
        for idx, row in enumerate(rows):
            if _total_rows and (idx % 3 == 0 or idx == _total_rows - 1):
                self._set_loading_text(f"{idx + 1}/{_total_rows}")
            row_h = self._row_height(row)
            left_rows.append(self._left_row(row, idx, row_h))
            right_cells = []
            for cid in self._ordered_schedule_right_columns(show_archive_column):
                if cid == "archive" and show_archive_column:
                    right_cells.append(self._archive_cell(row, row_h))
                elif cid == "months":
                    right_cells.extend(
                        [
                            self._month_cell(row, month, row_h)
                            for month in visible_months
                        ]
                    )
            _right_bg = (
                _ERR if self._is_row_inactive(row) else (_ALT if idx % 2 else _PANEL)
            )
            right_rows.append(
                ft.Container(
                    height=row_h,
                    bgcolor=_right_bg,
                    content=ft.Row(right_cells, spacing=0),
                )
            )

        right_inner_width = (0 if not show_archive_column else _ARCHIVE_W) + len(
            visible_months
        ) * _MONTH_W

        page = self._pg()
        vp_w = int(getattr(self.content_area, "width", 0) or 0)
        if vp_w <= 0 and page:
            vp_w = int(getattr(page, "width", 0) or 0)
        vp_w = max(300, vp_w or 1360)
        right_host_w = max(
            200, vp_w - (_LEFT_CB_W + _LEFT_STATUS_W + _LEFT_NAME_W) - 28
        )

        # ── Google Sheets layout: pinned header + scrollable body ─────────────
        left_header_container = ft.Container(
            width=_LEFT_CB_W + _LEFT_STATUS_W + _LEFT_NAME_W,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            bgcolor=_PANEL,
            content=self._table_header_left(),
        )
        _left_rows_col = ft.Column([], spacing=0)
        left_body_col = ft.Container(
            width=_LEFT_CB_W + _LEFT_STATUS_W + _LEFT_NAME_W,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            bgcolor=_PANEL,
            content=_left_rows_col,
        )

        right_header_content = ft.Container(
            width=right_inner_width,
            content=self._table_header_right(
                visible_months=visible_months,
                show_archive_column=show_archive_column,
            ),
        )
        # Stack approach: shift header content via left=-offset (synchronous, zero lag)
        right_header_content.left = -int(self._h_offset or 0)
        right_header_content.top = 0
        self._right_header_stack_child = right_header_content
        self._right_header_scroll = None  # replaced by stack; scroll_to no longer used

        right_header_host = ft.Container(
            width=right_host_w,
            height=72,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.Stack([right_header_content]),
        )

        _right_rows_col = ft.Column([], spacing=0)
        right_body_content = ft.Container(
            width=right_inner_width,
            bgcolor=_PANEL,
            content=_right_rows_col,
        )
        # Hidden scrollbar — h-scroll driven by overlay buttons instead.
        right_body_scroll_row = ft.Row(
            [right_body_content],
            spacing=0,
            scroll=ft.ScrollMode.HIDDEN,
            on_scroll=self._on_h_scroll,  # drives header sync only
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
        self._right_body_scroll = right_body_scroll_row
        self._top_h_scrollbar = None  # no custom rail needed
        # Drag-to-scroll edge zones: transparent hover strips at left/right of scroll area.
        _edge_w = 36
        _left_edge = ft.Container(
            width=_edge_w,
            bgcolor=None,
            on_hover=lambda e: (
                self._start_drag_edge_scroll("left")
                if e.data == "true"
                else self._stop_drag_edge_scroll()
            ),
        )
        _right_edge = ft.Container(
            width=_edge_w,
            bgcolor=None,
            on_hover=lambda e: (
                self._start_drag_edge_scroll("right")
                if e.data == "true"
                else self._stop_drag_edge_scroll()
            ),
        )
        right_body_host = ft.Stack(
            [
                ft.Container(width=right_host_w, content=right_body_scroll_row),
                ft.Row(
                    [_left_edge, ft.Row(expand=True), _right_edge],
                    expand=True,
                ),
            ],
            width=right_host_w,
        )

        header_row = ft.Row(
            [left_header_container, right_header_host],
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
        body_row = ft.Row(
            [left_body_col, right_body_host],
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )
        body_col = ft.Column(
            [body_row],
            spacing=0,
            expand=True,
            scroll=ft.ScrollMode.HIDDEN,
            on_scroll=self._on_v_scroll,
        )
        self._v_scroll_col = body_col
        self._h_scroll_row = right_body_scroll_row

        # ── Header title row with sync indicator on the right ─────────────────
        header_title_row = ft.Row(
            [
                ft.Icon(ft.Icons.EVENT_NOTE, size=24, color=self._th_icon),
                ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Text(
                                    self.t("schedule.title", "Розклад Замовлень"), size=22, weight="bold", color=_TEXT
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.HELP_OUTLINE,
                                    icon_size=18,
                                    icon_color=_TEXT,
                                    tooltip=self.t("common.help.tooltip", "Довідка"),
                                    on_click=lambda _e: self._show_help(),
                                ),
                            ],
                            spacing=2,
                            tight=True,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Text(
                            f"{self.region_label} · {self.manager_name}",
                            size=12,
                            color=_MUTED,
                            no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                            max_lines=1,
                        ),
                    ],
                    spacing=1,
                ),
                self._header_loading_indicator,
                ft.VerticalDivider(width=1),
                ft.Text(
                    self.t("schedule.months_ahead", "Планування на {n} міс. вперед").format(n=self._visible_months_count),
                    size=12,
                    color=_MUTED,
                ),
                ft.Row(expand=True),
                self._build_workspace_sync_indicator(),
            ],
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        filter_bar = self._build_filter_bar()

        # Pin header title, filter bar AND table header_row to the sticky slot.
        pg_s = self._pg()
        _sticky = getattr(pg_s, "_view_sticky_header", None) if pg_s else None

        _title_ctr = ft.Container(
            content=header_title_row,
            padding=ft.padding.only(bottom=4),
            border=ft.border.only(bottom=ft.BorderSide(1, "#E2E8F0")),
        )
        _filter_ctr = ft.Container(
            content=filter_bar,
            padding=ft.padding.only(bottom=4),
        )

        self._sticky_title_ref = _title_ctr
        self._sticky_filter_ref = _filter_ctr
        self._sticky_table_header_ref = header_row

        if _sticky is not None:
            outer_col = ft.Column([body_col], spacing=0, expand=True)
        else:
            outer_col = ft.Column([header_row, body_col], spacing=0, expand=True)

        # ── Horizontal-scroll overlay buttons ──────────────────────────────────
        _h_needs = right_inner_width > right_host_w
        _h_step = max(300, right_inner_width // 3)
        _lc_w = _LEFT_CB_W + _LEFT_STATUS_W + _LEFT_NAME_W

        def _do_hscroll_sched(delta):
            async def _run():
                with contextlib.suppress(Exception):
                    if self._right_body_scroll is not None:
                        await self._right_body_scroll.scroll_to(
                            delta=delta, duration=200
                        )

            with contextlib.suppress(Exception):
                _pg2 = self._pg()
                if _pg2:
                    _pg2.run_task(_run)

        _sh = ft.BoxShadow(
            spread_radius=1,
            blur_radius=8,
            color=ft.Colors.with_opacity(0.28, ft.Colors.BLACK),
            offset=ft.Offset(0, 2),
        )
        _bs = ft.ButtonStyle(padding=ft.padding.all(4))
        _hs_left = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_LEFT,
                icon_size=20,
                icon_color=_ACCENT,
                on_click=lambda _: _do_hscroll_sched(-_h_step),
                style=_bs,
            ),
            left=4,
            top=220,
            bgcolor=_PANEL,
            border_radius=20,
            width=36,
            height=36,
            shadow=_sh,
            opacity=0.9,
            visible=_h_needs,
        )
        _hs_right = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_RIGHT,
                icon_size=20,
                icon_color=_ACCENT,
                on_click=lambda _: _do_hscroll_sched(_h_step),
                style=_bs,
            ),
            right=6,
            top=220,
            bgcolor=_PANEL,
            border_radius=20,
            width=36,
            height=36,
            shadow=_sh,
            opacity=0.9,
            visible=_h_needs,
        )

        layout = ft.Column([outer_col], spacing=0, tight=True)

        # Bump generation so any running _async_fill_rows from a prior _rebuild stops.
        self._fill_generation = getattr(self, "_fill_generation", 0) + 1
        self._pending_left_rows = left_rows
        self._pending_right_rows = right_rows
        self._pending_left_rows_col = _left_rows_col
        self._pending_right_rows_col = _right_rows_col
        self._pending_right_body_content = right_body_content
        self._pending_rows_data = list(rows)

        # Send skeleton with empty body columns to Flutter — fast single update.
        self.content_area.controls.clear()
        with contextlib.suppress(Exception):
            self.content_area.scroll = ft.ScrollMode.ADAPTIVE
            self.content_area.on_scroll = self._on_content_area_scroll
        self.content_area.controls.append(layout)
        self.content_area.update()

        # Move h-scroll buttons to page overlay so they stay visible during v-scroll.
        for _b in getattr(page, "_catman_hscroll_btns", []):
            with contextlib.suppress(Exception):
                page.overlay.remove(_b)
        page._catman_hscroll_btns = []
        if _h_needs:
            _btn_top = int((getattr(page, "height", None) or 900) * 0.5)
            _hs_left.top = _btn_top
            _hs_right.top = _btn_top
            page.overlay.append(_hs_left)
            page.overlay.append(_hs_right)
            page._catman_hscroll_btns = [_hs_left, _hs_right]
            with contextlib.suppress(Exception):
                page.update()
        self._hs_left_btn = _hs_left
        self._hs_right_btn = _hs_right

        # Lightweight resize handler — only toggles button visibility, no full rebuild.
        def _on_sched_resized(_e=None):
            if str(getattr(page, "_active_view", "") or "") != "show_schedule":
                return
            try:
                vp = int(getattr(self.content_area, "width", 0) or 0)
                if vp <= 0:
                    vp = int(getattr(page, "width", 0) or 0)
                if vp <= 0:
                    return
                _l_w = getattr(
                    self, "_left_w", _LEFT_CB_W + _LEFT_STATUS_W + _LEFT_NAME_W
                )
                _r_w = getattr(self, "_right_inner_w", 0)
                new_needs = _r_w > max(200, vp - _l_w - 28)
                btn_l = getattr(self, "_hs_left_btn", None)
                btn_r = getattr(self, "_hs_right_btn", None)
                if btn_l is not None and btn_l.visible != new_needs:
                    btn_l.visible = new_needs
                    btn_r.visible = new_needs
                    with contextlib.suppress(Exception):
                        btn_l.update()
                    with contextlib.suppress(Exception):
                        btn_r.update()
            except Exception:
                pass

        try:
            if hasattr(page, "on_resized"):
                page.on_resized = _on_sched_resized
            elif hasattr(page, "on_resize"):
                page.on_resize = _on_sched_resized
        except Exception:
            pass

        # Dismiss spinner + show sticky header immediately.
        if self._header_loading_indicator is not None:
            self._header_loading_indicator.visible = False
        if _sticky is not None:
            _sticky.controls[:] = [_title_ctr, _filter_ctr, header_row]
            with contextlib.suppress(Exception):
                _sticky.update()

        self._refresh_workspace_sync_indicator_state()
        self._left_w = _LEFT_CB_W + _LEFT_STATUS_W + _LEFT_NAME_W
        self._right_inner_w = right_inner_width
        self._schedule_ui_ready = True

        # Schedule progressive row filling on the event loop.
        _pg_fill = self._pg()
        if _pg_fill and hasattr(_pg_fill, "run_task"):
            with contextlib.suppress(Exception):
                _pg_fill.run_task(self._async_fill_rows)
        else:
            # No page — fill synchronously as fallback.
            _left_rows_col.controls.extend(left_rows)
            _right_rows_col.controls.extend(right_rows)
            self._right_rows_refs = list(right_rows)
            self._right_body_content_ref = right_body_content
            self._last_built_rows = list(rows)
            self._left_rows_refs = list(left_rows)
            self._left_body_col_ref = _left_rows_col
            self._right_rows_col_ref = _right_rows_col
            with contextlib.suppress(Exception):
                _left_rows_col.update()
            with contextlib.suppress(Exception):
                _right_rows_col.update()
            self._restore_scroll_after_rebuild()

    async def _async_fill_rows(self) -> None:
        """Fill body columns with table rows in small batches so Flutter stays responsive."""
        import asyncio as _asyncio

        _gen = self._fill_generation
        left_rows = self._pending_left_rows or []
        right_rows = self._pending_right_rows or []
        left_col = getattr(self, "_pending_left_rows_col", None)
        right_col = getattr(self, "_pending_right_rows_col", None)
        right_body = getattr(self, "_pending_right_body_content", None)
        rows_data = getattr(self, "_pending_rows_data", None) or []

        if left_col is None or right_col is None:
            return

        # Clear stale fast-refresh refs so any interaction during fill falls back
        # to _schedule_rebuild() rather than touching detached controls.
        self._right_rows_refs = []
        self._last_built_rows = []
        self._right_body_content_ref = None
        self._left_rows_refs = []
        self._left_body_col_ref = None
        self._right_rows_col_ref = None

        _BATCH = 8
        _total = max(len(left_rows), len(right_rows))
        if _total == 0:
            self._right_rows_refs = []
            self._right_body_content_ref = right_body
            self._last_built_rows = []
            self._restore_scroll_after_rebuild()
            return

        for _i in range(0, _total, _BATCH):
            if getattr(self, "_fill_generation", 0) != _gen:
                return  # superseded by a newer _rebuild
            _lb = left_rows[_i : _i + _BATCH]
            _rb = right_rows[_i : _i + _BATCH]
            if _lb:
                left_col.controls.extend(_lb)
            if _rb:
                right_col.controls.extend(_rb)
            with contextlib.suppress(Exception):
                left_col.update()
            with contextlib.suppress(Exception):
                right_col.update()
            await _asyncio.sleep(0)  # yield so Flutter processes this batch

        if getattr(self, "_fill_generation", 0) != _gen:
            return

        # All rows rendered — wire up fast-refresh refs and restore scroll.
        self._right_rows_refs = list(right_rows)
        self._right_body_content_ref = right_body
        self._last_built_rows = list(rows_data)
        self._left_rows_refs = list(left_rows)
        self._left_body_col_ref = left_col
        self._right_rows_col_ref = right_col
        self._restore_scroll_after_rebuild()

    async def resume(self) -> None:
        """Called when switching back to this cached tab.
        Silently validates DB pulse; only shows spinner/reloads if data changed."""
        import asyncio as _asyncio

        if not self._schedule_ui_ready:
            return

        # Restore sticky header immediately so month names appear on tab switch
        # (not after the async pulse check which would make them appear slowly)
        # Guard: only overwrite sticky header if THIS tab is currently active.
        pg_imm = self._pg()
        if pg_imm and getattr(pg_imm, "_active_view", "") == "show_schedule":
            _sticky_imm = getattr(pg_imm, "_view_sticky_header", None)
            if _sticky_imm is not None:
                _t_imm = getattr(self, "_sticky_title_ref", None)
                _f_imm = getattr(self, "_sticky_filter_ref", None)
                _th_imm = getattr(self, "_sticky_table_header_ref", None)
                _new_ctrls_imm = [c for c in [_t_imm, _f_imm, _th_imm] if c is not None]
                if _new_ctrls_imm:
                    _sticky_imm.controls[:] = _new_ctrls_imm
                    with contextlib.suppress(Exception):
                        _sticky_imm.update()

        _saved_controls = list(getattr(self.content_area, "controls", None) or [])

        # Silently check DB pulse — no loading stub until we know data changed.
        _data_changed = True
        if self._schedule_cache_pulse > 0 and _saved_controls:
            try:
                _new_pulse = await _asyncio.to_thread(self._schedule_scope_pulse)
                _data_changed = _new_pulse != self._schedule_cache_pulse
            except Exception:
                _data_changed = True

        # After await: check if user already switched to a different tab.
        _pg_check = self._pg()
        if _pg_check and getattr(_pg_check, "_active_view", "") != "show_schedule":
            return

        if not _data_changed:
            # Ensure sticky header shows the correct Schedule header.
            pg_r = self._pg()
            _sticky_r = getattr(pg_r, "_view_sticky_header", None) if pg_r else None
            if _sticky_r is not None:
                _t = getattr(self, "_sticky_title_ref", None)
                _f = getattr(self, "_sticky_filter_ref", None)
                _th = getattr(self, "_sticky_table_header_ref", None)
                new_ctrls = [c for c in [_t, _f, _th] if c is not None]
                if new_ctrls:
                    _sticky_r.controls[:] = new_ctrls
                    with contextlib.suppress(Exception):
                        _sticky_r.update()

            # Background: check if reference data changed since last load.
            async def _bg_ref_data_check():
                try:
                    ref_pulse = await _asyncio.to_thread(
                        self.db.get_reference_data_pulse, self.company_id
                    )
                    if ref_pulse > self._ref_data_pulse_ts:
                        with contextlib.suppress(Exception):
                            self._render_loading_stub()
                            if self.content_area:
                                self.content_area.update()
                        try:
                            await _asyncio.to_thread(self._rebuild, True)
                        except Exception:
                            self._hide_spinner()
                except Exception:
                    self._hide_spinner()

            _ref_pg = self._pg()
            if _ref_pg and hasattr(_ref_pg, "run_task"):
                with contextlib.suppress(Exception):
                    _ref_pg.run_task(_bg_ref_data_check)
        else:
            # Data changed — show loading stub then rebuild in thread.
            with contextlib.suppress(Exception):
                self._render_loading_stub()
                if self.content_area:
                    self.content_area.update()
            try:
                await _asyncio.to_thread(self._rebuild, True)
            except Exception:
                self._hide_spinner()

        # Clear any stale page-slot scrollbar left by a previous tab.
        pg = self._pg()
        _slot = getattr(pg, "_view_h_scrollbar_slot", None) if pg else None
        if _slot is not None and _slot.controls:
            with contextlib.suppress(Exception):
                _slot.controls.clear()
                _slot.update()
