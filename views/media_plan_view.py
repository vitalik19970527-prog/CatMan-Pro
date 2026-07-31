"""Медіа план — таблиця товарів регіону з датами публікацій."""

import asyncio
import contextlib
import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta

import flet as ft

import i18n
from views import product_naming
from views.async_view_build_helper import \
    create_orders_header_loading_indicator
from views.mobile_layout_helpers import is_mobile, make_scrollable_chip_row
from views.number_format import row_text_matches

_log = logging.getLogger("media_plan")

_ROW_A = "#FFFFFF"
_ROW_B = "#F8FAFC"
_ROW_INACTIVE = "#FEECEC"
_HDR_BG = "#E2E8F0"
_BORDER = "#CBD5E1"
_ACCENT = "#1D4ED8"
_SEL_BG = "#EFF6FF"

_G_BG, _G_FG = "#DCFCE7", "#166534"
_Y_BG, _Y_FG = "#FEF9C3", "#92400E"
_R_BG, _R_FG = "#FEE2E2", "#991B1B"
_N_BG, _N_FG = "#F1F5F9", "#94A3B8"

# Date chip colors: past / today / future
_DATE_PAST_BG, _DATE_PAST_FG = "#FFF3E0", "#C2410C"
_DATE_TODAY_BG, _DATE_TODAY_FG = "#E0F2FE", "#0369A1"
_DATE_FUTURE_BG, _DATE_FUTURE_FG = "#DCFCE7", "#15803D"


def _date_chip_colors(ds: str, close_gap_today: bool = False) -> tuple[str, str]:
    """Return (bg, fg) for a date chip.

    close_gap_today=True → red (close gap detected, within 24-hour alert window).
    Past → light orange. Today → light blue. Future → light green.
    """
    if close_gap_today:
        return _R_BG, _R_FG
    today = date.today().isoformat()
    d = str(ds)[:10]
    if d < today:
        return _DATE_PAST_BG, _DATE_PAST_FG
    if d == today:
        return _DATE_TODAY_BG, _DATE_TODAY_FG
    return _DATE_FUTURE_BG, _DATE_FUTURE_FG

_OK_C = "#166534"
_ERR_C = "#EF4444"

_CB_W = 40
_ACT_W = 70
_ARCHIVE_SECTION_W = 100
_NAME_W = 250
_COMMENT_W = 250
_DAYS_W = 80
_DATES_W = 100
_ROW_H = 70
_PX_COL_MIN = 30
_PX_COL_MAX = 200
_PX_ROW_MIN = 1
_PX_ROW_MAX = 120
_PX_TEXT_MIN = 4
_PX_TEXT_MAX = 20
_MONTH_COL_W = 140  # width of each month column in the right panel


def _month_start(d=None):
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
    names = {
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
    return f"{names.get(d.month, str(d.month))} {d.year}"


def _jlist(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            r = json.loads(v)
            return r if isinstance(r, list) else []
        except Exception:
            return []
    return []


def _days_ago(d_str: str | None) -> int | None:
    if not d_str:
        return None
    try:
        return (date.today() - date.fromisoformat(str(d_str)[:10])).days
    except Exception:
        return None


def _fmt_date(d_str: str | None) -> str:
    if not d_str:
        return "-"
    try:
        return date.fromisoformat(str(d_str)[:10]).strftime("%d.%m.%y")
    except Exception:
        return str(d_str)[:10]


def _day_colors(days: int | None) -> tuple[str, str]:
    if days is None:
        return _N_BG, _N_FG
    if days < 14:
        return _G_BG, _G_FG
    if days < 30:
        return _Y_BG, _Y_FG
    return _R_BG, _R_FG


class MediaPlanView:
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
        _th_mp = _th_mod.get_active_theme(self.manager_id)
        self._th_text = _th_mp["text"]
        self._th_muted = _th_mp["muted"]
        self._th_icon = _th_mp["icon_color"]
        self._th_panel = _th_mp["panel"]
        self._th_border = _th_mp["border"]
        self._th_chip_bg = _th_mp.get("chip_bg", _th_mp["panel"])
        self._th_chip_text = _th_mp.get("chip_text", "#1E3A8A")
        self._th_ok = _th_mp["ok"]
        self._th_ok_bg = _th_mp["ok_bg"]
        self._th_warn = _th_mp["warn"]
        self._th_warn_bg = _th_mp["warn_bg"]
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
        self.is_read_only = bool(self.user_data.get("subordinate_view_readonly", False))
        self.region_label = str(
            self.user_data.get("active_region_label")
            or self.active_region
            or "Всі регіони"
        ).strip()

        self._products: list[dict] = []
        self._entries: list[dict] = []
        self._selected: set[str] = set()
        self._region_id: str | None = None
        self._quick_filter = str(quick_filter or "").strip().lower()
        self._search_text_query: str = ""

        self._filter_category_ids: set[str] = set()
        self._filter_level_node_ids: dict[str, str] = {}
        self._adv_filters_open = False
        self._categories: list[dict] = []
        self._enabled_levels: list[dict] = []
        self._node_names: dict[str, str] = {}
        self._category_level_pos_by_title: dict[str, dict[str, int]] = {}
        self._title_default_pos: dict[str, int] = {}
        self._level_title_options: list[tuple[str, str]] = []
        self._table_layout = {
            "name_w": _NAME_W,
            "comment_w": _COMMENT_W,
            "days_w": _DAYS_W,
            "dates_w": _DATES_W,
            "archive_w": _ARCHIVE_SECTION_W,
            "month_col_w": _MONTH_COL_W,
            "row_h": _ROW_H,
            "text_size": 11,
            "text_h_align": "center",
            "text_v_align": "center",
            "text_wrap": True,
        }
        self._visible_columns = {"name", "comment", "days"}
        self._column_order_ids = ["name", "comment", "days", "dates"]
        # Monthly view state
        self._visible_months_count: int = 3
        self._pin_last_archive_month: bool = False
        self._opened_archive_month_keys: list = []
        self._allowed_post_weekdays: set[int] = {0, 1, 2, 3, 4, 5, 6}
        # Sorting state
        self._category_sort_order: str = "asc"
        self._item_sort_order: str = "asc"
        self._default_level_sort_title_key: str = ""
        self._category_level_sort_title_key: dict[str, str] = {}
        self._category_custom_order_ids: list[str] = []
        self._level_value_custom_order_by_title_key: dict[str, list[str]] = {}
        # Column sort (overrides category/item sort when active)
        self._col_sort_key: str = ""   # "name", "days", or month-key like "2026-06"
        self._col_sort_dir: int = 0    # 0=off, 1=asc, 2=desc
        # Inactive rows grouped section
        self._inactive_section_collapsed: bool = True
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
        # had no on_scroll tracking at all, so a page-level scroll (as
        # opposed to the inner table body's own scroll) was silently lost on
        # every _rebuild() — the page visibly snapped back to the top after
        # any action that rebuilds (adding a date, background ref-data sync).
        self._content_area_offset: float = 0.0
        # Per-entry ownership (for shared-access edit gating).
        self._owned_entry_ids: set[str] = set()
        self._initial_loaded: bool = False
        self._rebuild_scheduled: bool = False
        self._catalog_nodes_by_id: dict = {}
        self._right_header_scroll = None
        self._right_body_scroll = None
        self._top_h_scrollbar = None
        self._h_sync_pending = False
        self._h_scrollbar_offset = 0.0
        self._left_w: int = 0
        self._right_inner_w: int = 0
        self._media_plan_ui_ready = False
        self._entries_cache_pulse: int = 0
        self._ref_data_pulse_ts: int = 0
        _som = self.user_data.get("shared_orders_map")
        self._shared_orders_map: dict[str, dict] = (
            _som if isinstance(_som, dict) else {}
        )
        # Widget refs for in-place cell updates (avoid full rebuild on date change).
        self._month_cell_chips_col_refs: dict = {}
        self._left_row_days_chip_refs: dict = {}
        self._date_chips_row_refs: dict = {}

    def _all_media_column_defs(self) -> list[tuple[str, str, str]]:
        return [
            ("name", self.t("media.col.product_name", "Назва товару"), "name_w"),
            ("comment", self.t("media.col.comment", "Коментар"), "comment_w"),
            ("days", self.t("media.col.days_pub", "Днів з публ."), "days_w"),
            ("dates", self.t("media.col.pub_dates", "Дати публікації"), "dates_w"),
        ]

    def _ordered_media_column_ids(self) -> list[str]:
        all_ids = [key for key, _label, _w_key in self._all_media_column_defs()]
        raw = [str(v).strip() for v in (self._column_order_ids or []) if str(v).strip()]
        return [cid for cid in raw if cid in all_ids] + [
            cid for cid in all_ids if cid not in set(raw)
        ]

    def _visible_ordered_media_column_ids(self) -> list[str]:
        visible = set(self._visible_columns or set())
        return [cid for cid in self._ordered_media_column_ids() if cid in visible]

    def _is_stale_filter_mode(self) -> bool:
        return self._quick_filter == "stale_posts"

    @staticmethod
    def _stale_bucket(days: int | None, post_dates: list[str]) -> str:
        if not post_dates:
            return "red"
        if days is None:
            return "red"
        if days >= 30:
            return "red"
        if days >= 14:
            return "yellow"
        return "green"

    def _row_matches_quick_filter(self, row: dict) -> bool:
        # Inactive rows always pass through — they are shown in the inactive section
        if bool(row.get("not_relevant")):
            return True
        if not self._is_stale_filter_mode():
            return True
        bucket = self._stale_bucket(
            row.get("days_since_publication"), row.get("post_dates") or []
        )
        return bucket in {"yellow", "red"}

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

    # ── File cache for entries (pulse-verified) ───────────────────────────────

    def _entries_cache_path(self) -> str:
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_reg = (
            (self._region_id or self.active_region or "all")
            .replace("/", "_")
            .replace("\\", "_")
        )
        # v2: upsert_media_plan_entry never bumped the "media_plan" sync pulse
        # (only delete did), so every v1 cache file's pulse_ts got stuck at
        # whatever it was the first time it was ever written (often 0, from
        # before any entries existed) and never looked stale again — real
        # edits kept saving fine to the DB, but this file-cache silently kept
        # serving that frozen-in-time snapshot on every later tab open,
        # making months of real data disappear from view and, worse, causing
        # already-existing rows to be (mis)treated as brand new on every
        # session (duplicate DB entries — see _row_cell_key_base/_add_date).
        # New filename so every pre-existing (poisoned) cache file is simply
        # never found again -> forces one clean re-fetch from the DB for
        # every user, without needing to touch each machine's cache by hand.
        return os.path.join(
            self._app_data_dir(),
            f"media_plan_entries_v2_{safe_cid}_{safe_mgr}_{safe_reg}.json",
        )

    def _entries_scope_pulse(self) -> int:
        try:
            out = (
                self.db.get_companies_sync_pulses([self.company_id], ["media_plan"])
                or {}
            )
            by_cmp = (out or {}).get(self.company_id, {})
            return int((by_cmp or {}).get("media_plan") or 0)
        except Exception:
            return 0

    def _load_entries_cache(self) -> int | None:
        """Load entries from file cache. Returns cached pulse_ts or None on miss."""
        path = self._entries_cache_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("entries") if isinstance(data, dict) else None
            if not isinstance(entries, list):
                return None
            self._entries = [dict(e) for e in entries if isinstance(e, dict)]
            return int(data.get("pulse_ts") or 0)
        except Exception:
            return None

    def _save_entries_cache(self, pulse_ts: int) -> None:
        path = self._entries_cache_path()
        try:
            payload = {
                "pulse_ts": int(pulse_ts),
                "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
                "entries": [
                    dict(e) for e in (self._entries or []) if isinstance(e, dict)
                ],
            }
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, path)
            self._entries_cache_pulse = pulse_ts
        except Exception:
            pass

    def _apply_outbox_to_entries(self) -> None:
        """Re-apply pending outbox items over loaded entries (for failed-flush scenarios)."""
        del_entry_ids: set = set()
        for op in self._ops_outbox.values():
            if not isinstance(op, dict):
                continue
            kind = op.get("kind") or ""
            if kind == "entry_delete":
                eid = str((op.get("data") or {}).get("id") or "").strip()
                if eid:
                    del_entry_ids.add(eid)
            elif kind == "entry":
                entry = op.get("data") or {}
                eid = str(entry.get("id") or "").strip()
                if not eid:
                    continue
                for i, e in enumerate(self._entries):
                    if str((e or {}).get("id") or "").strip() == eid:
                        self._entries[i] = dict(entry)
                        break
                else:
                    self._entries.append(dict(entry))
        if del_entry_ids:
            self._entries = [
                e
                for e in self._entries
                if str((e or {}).get("id") or "").strip() not in del_entry_ids
            ]

    def _cleanup_orphaned_entries(self) -> None:
        """Remove entries whose ALL product_ids no longer exist in self._products.
        These are left over from products deleted in the Товари tab."""
        if not self._entries or not self._products:
            return
        valid_pids = {
            str(p.get("id") or "").strip()
            for p in self._products
            if isinstance(p, dict) and p.get("id")
        }
        to_delete: list[str] = []
        keep: list[dict] = []
        for en in self._entries:
            if not isinstance(en, dict):
                continue
            eid = str(en.get("id") or "").strip()
            if not eid:
                continue
            pids = [
                str(x).strip() for x in _jlist(en.get("product_ids")) if str(x).strip()
            ]
            # Remove: no product_ids at all (old "додаткові пости") OR all product_ids orphaned
            if not pids or not any(p in valid_pids for p in pids):
                to_delete.append(eid)
            else:
                keep.append(en)
        if to_delete:
            self._entries = keep
            for eid in to_delete:
                self._ops_outbox[f"entry_delete:{eid}"] = {
                    "kind": "entry_delete",
                    "data": {"id": eid},
                }
                self._ops_outbox.pop(f"entry:{eid}", None)
            self._save_ops_outbox()

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
            self._app_data_dir(), f"media_plan_ws_{cid}_{uid}_{rid}.json"
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

    def _workspace_outbox_file_path(self) -> str:
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        return os.path.join(
            self._app_data_dir(),
            f"media_plan_workspace_outbox_{safe_cid}_{safe_mgr}.json",
        )

    def _ops_outbox_file_path(self) -> str:
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        return os.path.join(
            self._app_data_dir(),
            f"media_plan_ops_outbox_{safe_cid}_{safe_mgr}.json",
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
                    if kind == "entry":
                        ok = await asyncio.to_thread(
                            self.db.upsert_media_plan_entry, op["data"]
                        )
                    elif kind == "entry_delete":
                        _del_id = str((op.get("data") or {}).get("id") or "").strip()
                        ok = (
                            await asyncio.to_thread(
                                self.db.delete_media_plan_entry, _del_id
                            )
                            if _del_id
                            else True
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
                # All flushed — refresh cache so next startup skips DB fetch.
                new_pulse = await asyncio.to_thread(self._entries_scope_pulse)
                if new_pulse:
                    await asyncio.to_thread(self._save_entries_cache, new_pulse)
        except Exception:
            error_count += 1
        finally:
            self._ops_flush_running = False
            if error_count > 0:
                self._set_workspace_sync_status("error")
            else:
                self._refresh_workspace_sync_indicator_state()

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
            width=8, height=8, border_radius=4, bgcolor=self._th_border
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
            if not pg or str(getattr(pg, "_active_view", "")) != "show_media_plan":
                continue
            if self._workspace_outbox:
                await self._flush_workspace_state_async()
            if self._ops_outbox:
                await self._flush_ops_async()

    def _load_table_layout_state(self) -> None:
        manager_id = self._workspace_user_id()
        if not manager_id:
            return
        try:
            payload = (
                self._workspace_state
                if isinstance(self._workspace_state, dict) and self._workspace_state
                else (self.db.get_manager_workspace_state(manager_id) or {})
            )
            if not isinstance(payload, dict):
                return
            store = (
                payload.get("media_plan_table_layout")
                if isinstance(payload.get("media_plan_table_layout"), dict)
                else {}
            )
            row = (
                store.get(self._table_layout_state_key())
                if isinstance(store, dict)
                else None
            )
            if not isinstance(row, dict):
                return
            self._table_layout["name_w"] = int(
                max(_PX_COL_MIN, min(_PX_COL_MAX, int(row.get("name_w") or _NAME_W)))
            )
            self._table_layout["comment_w"] = int(
                max(
                    _PX_COL_MIN,
                    min(_PX_COL_MAX, int(row.get("comment_w") or _COMMENT_W)),
                )
            )
            self._table_layout["days_w"] = int(
                max(_PX_COL_MIN, min(_PX_COL_MAX, int(row.get("days_w") or _DAYS_W)))
            )
            self._table_layout["archive_w"] = int(
                max(
                    _PX_COL_MIN,
                    min(_PX_COL_MAX, int(row.get("archive_w") or _ARCHIVE_SECTION_W)),
                )
            )
            self._table_layout["month_col_w"] = int(
                max(
                    _PX_COL_MIN,
                    min(_PX_COL_MAX, int(row.get("month_col_w") or _MONTH_COL_W)),
                )
            )
            self._table_layout["dates_w"] = int(
                max(_PX_COL_MIN, min(_PX_COL_MAX, int(row.get("dates_w") or _DATES_W)))
            )
            self._table_layout["row_h"] = int(
                max(_PX_ROW_MIN, min(_PX_ROW_MAX, int(row.get("row_h") or _ROW_H)))
            )
            self._table_layout["text_size"] = int(
                min(_PX_TEXT_MAX, max(_PX_TEXT_MIN, int(row.get("text_size") or 11)))
            )
            self._table_layout["text_h_align"] = "center"
            self._table_layout["text_v_align"] = "center"
            self._table_layout["text_wrap"] = bool(row.get("text_wrap", True))
        except Exception:
            pass

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
                payload.get("media_plan_table_layout")
                if isinstance(payload.get("media_plan_table_layout"), dict)
                else {}
            )
            store[self._table_layout_state_key()] = {
                "name_w": int(
                    max(
                        _PX_COL_MIN,
                        min(
                            _PX_COL_MAX,
                            int(self._table_layout.get("name_w") or _NAME_W),
                        ),
                    )
                ),
                "comment_w": int(
                    max(
                        _PX_COL_MIN,
                        min(
                            _PX_COL_MAX,
                            int(self._table_layout.get("comment_w") or _COMMENT_W),
                        ),
                    )
                ),
                "days_w": int(
                    max(
                        _PX_COL_MIN,
                        min(
                            _PX_COL_MAX,
                            int(self._table_layout.get("days_w") or _DAYS_W),
                        ),
                    )
                ),
                "archive_w": int(
                    max(
                        _PX_COL_MIN,
                        min(
                            _PX_COL_MAX,
                            int(
                                self._table_layout.get("archive_w")
                                or _ARCHIVE_SECTION_W
                            ),
                        ),
                    )
                ),
                "month_col_w": int(
                    max(
                        _PX_COL_MIN,
                        min(
                            _PX_COL_MAX,
                            int(self._table_layout.get("month_col_w") or _MONTH_COL_W),
                        ),
                    )
                ),
                "dates_w": int(
                    max(
                        _PX_COL_MIN,
                        min(
                            _PX_COL_MAX,
                            int(self._table_layout.get("dates_w") or _DATES_W),
                        ),
                    )
                ),
                "row_h": int(
                    max(
                        _PX_ROW_MIN,
                        min(
                            _PX_ROW_MAX, int(self._table_layout.get("row_h") or _ROW_H)
                        ),
                    )
                ),
                "text_size": int(
                    min(
                        _PX_TEXT_MAX,
                        max(
                            _PX_TEXT_MIN, int(self._table_layout.get("text_size") or 11)
                        ),
                    )
                ),
                "text_h_align": "center",
                "text_v_align": "center",
                "text_wrap": bool(self._table_layout.get("text_wrap", True)),
            }
            payload["media_plan_table_layout"] = store
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
            store = (
                payload.get("media_plan_visible_columns")
                if isinstance(payload, dict)
                else {}
            )
            raw = (
                store.get(self._table_layout_state_key())
                if isinstance(store, dict)
                else None
            )
            allowed = {"name", "comment", "days", "dates"}
            raw_visible = raw
            raw_order = None
            if isinstance(raw, dict):
                raw_visible = raw.get("visible")
                raw_order = raw.get("order")
            if isinstance(raw_visible, list):
                vals = {
                    str(v).strip() for v in raw_visible if str(v).strip() in allowed
                }
                if vals:
                    self._visible_columns = vals
            if isinstance(raw_order, list):
                order = [str(v).strip() for v in raw_order if str(v).strip() in allowed]
                if order:
                    self._column_order_ids = order
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
                payload.get("media_plan_visible_columns")
                if isinstance(payload.get("media_plan_visible_columns"), dict)
                else {}
            )
            visible_ordered = [
                cid
                for cid in self._ordered_media_column_ids()
                if cid in set(self._visible_columns or set())
            ]
            store[self._table_layout_state_key()] = {
                "visible": visible_ordered,
                "order": list(self._ordered_media_column_ids()),
            }
            payload["media_plan_visible_columns"] = store
            self._workspace_state = payload
            self._save_workspace_state_to_file()
            self._queue_workspace_state_save()
        except Exception:
            pass

    def _open_columns_dialog(self, _e=None) -> None:
        pg = self._pg()
        if not pg:
            return
        selected = set(self._visible_columns or {"name", "comment", "days"})
        defs = {key: label for key, label, _w_key in self._all_media_column_defs()}
        ordered_ids = list(self._ordered_media_column_ids())
        rows_host = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO)

        def _render_rows() -> None:
            controls = []
            total = len(ordered_ids)
            for idx, key in enumerate(ordered_ids):
                label = str(defs.get(key) or key)
                cb = ft.Checkbox(label=label, value=(key in selected))

                def _toggle(e, current_key=key):
                    if bool(getattr(e.control, "value", False)):
                        selected.add(current_key)
                    else:
                        selected.discard(current_key)

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
                controls.append(
                    ft.Row(
                        [
                            ft.Container(width=232, content=cb),
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
                        spacing=2,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )
            rows_host.controls = controls
            try:
                rows_host.update()
            except Exception:
                pass

        _render_rows()

        months_f = ft.TextField(
            label=self.t("media.field.months_count", "Кількість місяців"),
            value=str(self._visible_months_count),
            width=200,
            hint_text="1–24",
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        pin_cb = ft.Checkbox(
            label=self.t("media.field.show_prev_month", "Показувати попередній місяць"),
            value=self._pin_last_archive_month,
        )

        cats_sorted = sorted(
            [
                c
                for c in (self._categories or [])
                if isinstance(c, dict) and (c.get("id") or "")
            ],
            key=lambda x: str(x.get("name") or "").lower(),
        )

        # Filter to only categories available in user's products
        _product_cat_ids = {
            str(p.get("category_id") or "")
            for p in (self._products or [])
            if isinstance(p, dict)
        }
        cats_sorted = [
            c for c in cats_sorted if str(c.get("id") or "").strip() in _product_cat_ids
        ]

        cat_name_by_id = {
            str(c.get("id") or "").strip(): str(c.get("name") or "").strip()
            for c in cats_sorted
            if str(c.get("id") or "").strip()
        }

        cat_order_selected: list[str] = [
            str(cid).strip()
            for cid in (self._category_custom_order_ids or [])
            if str(cid).strip() in cat_name_by_id
        ]
        cat_order_col = ft.Column(spacing=2)

        cat_add_dd = ft.Dropdown(
            label="Додати категорію",
            width=210,
            options=[],
        )

        def _get_available_categories() -> list[ft.dropdown.Option]:
            """Повертає категорії, які ще не додані в чергу"""
            in_order = set(str(cid).strip() for cid in cat_order_selected)
            return [
                ft.dropdown.Option(
                    str(c.get("id") or ""), str(c.get("name") or "")[:30]
                )
                for c in cats_sorted
                if str(c.get("id") or "").strip() not in in_order
            ]

        def _add_category_to_order(_e=None):
            """Додає вибрану категорію в чергу"""
            cid = str(cat_add_dd.value or "").strip()
            if cid and cid in cat_name_by_id and cid not in cat_order_selected:
                cat_order_selected.append(cid)
                cat_add_dd.value = ""
                cat_add_dd.options = _get_available_categories()
                _refresh_cat_order_view()
                # OPTIMIZATION: Use page.update() instead of individual dlg.update()
                try:
                    dlg.update()
                except Exception:
                    pass

        def _refresh_cat_order_view() -> None:
            cat_order_col.controls.clear()
            if not cat_order_selected:
                cat_order_col.controls.append(
                    ft.Text("Черга категорій не задана", size=10, color=self._th_text)
                )
                return
            total = len(cat_order_selected)
            for idx, cid in enumerate(list(cat_order_selected)):
                cname = str(cat_name_by_id.get(cid) or cid)

                def _mk_up(i=idx):
                    def _up(_e):
                        if i <= 0 or i >= len(cat_order_selected):
                            return
                        cat_order_selected[i - 1], cat_order_selected[i] = (
                            cat_order_selected[i],
                            cat_order_selected[i - 1],
                        )
                        _refresh_cat_order_view()
                        # OPTIMIZATION: Use page.update() instead of individual dlg.update()
                        try:
                            dlg.update()
                        except Exception:
                            pass

                    return _up

                def _mk_down(i=idx):
                    def _down(_e):
                        if i < 0 or i >= len(cat_order_selected) - 1:
                            return
                        cat_order_selected[i + 1], cat_order_selected[i] = (
                            cat_order_selected[i],
                            cat_order_selected[i + 1],
                        )
                        _refresh_cat_order_view()
                        # OPTIMIZATION: Use page.update() instead of individual dlg.update()
                        try:
                            dlg.update()
                        except Exception:
                            pass

                    return _down

                def _mk_remove(c=cid):
                    def _remove(_e):
                        if c in cat_order_selected:
                            cat_order_selected.remove(c)
                        cat_add_dd.options = _get_available_categories()
                        _refresh_cat_order_view()
                        # OPTIMIZATION: Use page.update() instead of individual dlg.update()
                        try:
                            dlg.update()
                        except Exception:
                            pass

                    return _remove

                cat_order_col.controls.append(
                    ft.Row(
                        [
                            ft.Text(cname[:30], size=10, color=self._th_text, expand=True),
                            ft.IconButton(
                                icon=ft.Icons.KEYBOARD_ARROW_UP,
                                icon_size=14,
                                visible=(total > 1 and idx > 0),
                                on_click=_mk_up(),
                                style=ft.ButtonStyle(padding=ft.padding.all(0)),
                                width=18,
                                height=18,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.KEYBOARD_ARROW_DOWN,
                                icon_size=14,
                                visible=(total > 1 and idx < total - 1),
                                on_click=_mk_down(),
                                style=ft.ButtonStyle(padding=ft.padding.all(0)),
                                width=18,
                                height=18,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.CLOSE,
                                icon_size=12,
                                on_click=_mk_remove(),
                                style=ft.ButtonStyle(padding=ft.padding.all(0)),
                                width=16,
                                height=16,
                            ),
                        ],
                        spacing=2,
                    )
                )

        _refresh_cat_order_view()
        cat_add_dd.options = _get_available_categories()

        level_value_custom_map = {
            str(k).strip(): [str(x).strip() for x in (vals or []) if str(x).strip()]
            for k, vals in (self._level_value_custom_order_by_title_key or {}).items()
            if str(k or "").strip()
        }

        saved_level_title_by_cat: dict[str, str] = {}
        for raw_key, vals in (level_value_custom_map or {}).items():
            full_key = str(raw_key or "").strip()
            if not full_key or not isinstance(vals, list) or not vals:
                continue
            cid, sep, t_key = full_key.partition("|")
            cid = str(cid or "").strip()
            t_key = str(t_key or "").strip().lower() if sep else ""
            if cid and t_key and cid not in saved_level_title_by_cat:
                saved_level_title_by_cat[cid] = t_key

        title_text_by_key = {
            str(k or "").strip().lower(): str(t or "").strip() or str(k or "")
            for k, t in (self._level_title_options or [])
            if str(k or "").strip()
        }

        level_blocks_col = ft.Column(spacing=6)

        def _level_key_for(cat_id: str, title_key: str) -> str:
            return f"{str(cat_id or '').strip()}|{str(title_key or '').strip().lower()}"

        def _category_level_options(cat_id: str) -> list[ft.dropdown.Option]:
            cid = str(cat_id or "").strip()
            if not cid:
                return []
            title_pos = (self._category_level_pos_by_title or {}).get(cid) or {}
            pairs = sorted(
                [
                    (str(tk), int(pos or 0))
                    for tk, pos in title_pos.items()
                    if str(tk).strip()
                ],
                key=lambda x: (x[1], str(title_text_by_key.get(x[0], x[0])).lower()),
            )
            return [
                ft.dropdown.Option(
                    str(tk), str(title_text_by_key.get(str(tk), str(tk)))[:30]
                )
                for tk, _ in pairs
            ]

        def _level_object_options(cat_id: str, title_key: str) -> list[tuple[str, str]]:
            cid = str(cat_id or "").strip()
            t_key = str(title_key or "").strip().lower()
            # print(f"[_level_object_options] cat_id={cid}, title_key={t_key}")
            if not cid or not t_key:
                # print("[_level_object_options] empty cid or t_key, returning []")
                return []

            # Get position from category levels
            pos_dict = (self._category_level_pos_by_title or {}).get(cid) or {}
            pos = int(pos_dict.get(t_key, 0) or 0)
            # print(f"[_level_object_options] category pos_dict={pos_dict}, pos={pos}")

            if pos <= 0:
                # Try to use title_default_pos as fallback
                pos = int((self._title_default_pos or {}).get(t_key, 0) or 0)
                # print(f"[_level_object_options] fallback to default pos={pos}")
                if pos <= 0:
                    # print("[_level_object_options] pos still <=0, returning []")
                    return []

            # Instead of looking for level_X_node_id in products,
            # get all nodes for this category at this level from the database
            try:
                # Get all catalog nodes for this category
                all_nodes = self.db.get_company_catalog_nodes(self.company_id) or []
                # print(f"[_level_object_options] total catalog nodes: {len(all_nodes)}")
                if all_nodes:
                    # print(
                    #     f"[_level_object_options] FIRST NODE STRUCTURE: {all_nodes[0]}"
                    # )
                    pass

                # Build node lookup by id for hierarchy traversal
                nodes_by_id: dict[str, dict] = {}
                for node in all_nodes:
                    if isinstance(node, dict) and str(node.get("id") or "").strip():
                        nodes_by_id[str(node.get("id")).strip()] = node

                # Filter nodes by category_id and level
                nodes_for_level: dict[str, str] = {}
                for node in all_nodes:
                    if not isinstance(node, dict):
                        continue
                    # Get category_id from nested data field
                    node_data = node.get("data") or {}
                    if isinstance(node_data, dict):
                        node_cat_id = str(node_data.get("category_id") or "").strip()
                    else:
                        node_cat_id = ""

                    # Use level_position instead of level
                    node_level = int(node.get("level_position", 0) or 0)
                    node_id = str(node.get("id") or "").strip()
                    node_name = str(node.get("name") or "").strip()

                    if node_cat_id == cid and node_level == pos and node_id:
                        nodes_for_level[node_id] = node_name or node_id

                # print(
                #     f"[_level_object_options] nodes for cat {cid} at level {pos}: {nodes_for_level}"
                # )

                # Now get which of these nodes are used in products
                # For each product, traverse up the hierarchy to find node at target level
                uniq: dict[str, str] = {}
                for prod in self._products or []:
                    if not isinstance(prod, dict):
                        continue
                    if str(prod.get("category_id") or "").strip() != cid:
                        continue

                    # Start from product's catalog_node_id and go up the hierarchy
                    prod_node_id = str(prod.get("catalog_node_id") or "").strip()
                    if not prod_node_id:
                        continue

                    # Traverse up until we find node at target level
                    current_node_id = prod_node_id
                    visited = set()
                    while current_node_id and current_node_id not in visited:
                        visited.add(current_node_id)
                        current_node = nodes_by_id.get(current_node_id)
                        if not current_node:
                            break

                        current_level = int(current_node.get("level_position", 0) or 0)
                        if current_level == pos:
                            # Found target level
                            node_name = (
                                str(current_node.get("name") or "").strip()
                                or current_node_id
                            )
                            uniq[current_node_id] = node_name
                            break

                        # Go to parent
                        current_node_id = str(
                            current_node.get("parent_node_id") or ""
                        ).strip()

                # print(
                #     f"[_level_object_options] products with nodes at this level: {len(uniq)}"
                # )

                # If no products found for this level, use all nodes at this level
                if not uniq:
                    uniq = nodes_for_level.copy()
                    # print(
                    #     f"[_level_object_options] no products found, using all nodes at this level: {len(uniq)}"
                    # )

                # Deduplicate by name - keep only unique names
                unique_by_name: dict[str, str] = {}
                for nid, name in uniq.items():
                    if name not in unique_by_name:
                        unique_by_name[name] = nid

                result = sorted(
                    [(nid, name) for name, nid in unique_by_name.items()],
                    key=lambda x: (x[1].lower(), x[0]),
                )
                # print(f"[_level_object_options] result after dedup={result}")
                return result
            except Exception:
                # print(f"[_level_object_options] ERROR: {e}")
                import traceback

                traceback.print_exc()
                return []

        def _add_level_block(initial_cid: str = "", initial_t_key: str = ""):
            cat_dd = ft.Dropdown(
                label="Категорія (рівні)",
                width=170,
                options=[
                    ft.dropdown.Option(
                        str(c.get("id") or ""), str(c.get("name") or "")[:30]
                    )
                    for c in cats_sorted
                ],
                value=(
                    str(initial_cid or "").strip()
                    or (str(cats_sorted[0].get("id") or "") if cats_sorted else "")
                ),
            )
            level_dd = ft.Dropdown(label="Рівень", width=150, options=[])
            order_col = ft.Column(spacing=2)
            row_holder = ft.Column(spacing=4)

            def _refresh_block_controls(_e=None):
                cid = str(cat_dd.value or "").strip()
                prev_level = str(level_dd.value or "").strip().lower()
                level_opts = _category_level_options(cid)
                level_dd.options = level_opts
                if prev_level and any(
                    str(getattr(op, "key", "") or "").strip().lower() == prev_level
                    for op in level_opts
                ):
                    level_dd.value = prev_level
                else:
                    saved_level = (
                        str(saved_level_title_by_cat.get(cid) or initial_t_key or "")
                        .strip()
                        .lower()
                    )
                    if saved_level and any(
                        str(getattr(op, "key", "") or "").strip().lower() == saved_level
                        for op in level_opts
                    ):
                        level_dd.value = saved_level
                    else:
                        level_dd.value = (
                            str(getattr(level_opts[0], "key", "") or "")
                            if level_opts
                            else ""
                        )
                _refresh_block_order_view()
                # OPTIMIZATION: Use page.update() instead of individual widget.update()
                try:
                    dlg.update()
                except Exception:
                    pass

            def _refresh_block_order_view():
                order_col.controls.clear()
                cid = str(cat_dd.value or "").strip()
                t_key = str(level_dd.value or "").strip().lower()
                compound = _level_key_for(cid, t_key)
                cur = [
                    str(nid).strip()
                    for nid in (level_value_custom_map.get(compound) or [])
                    if str(nid).strip()
                ]
                names = {nid: nm for nid, nm in _level_object_options(cid, t_key)}

                if not cur:
                    order_col.controls.append(
                        ft.Text("Черга значень не задана", size=10, color=self._th_text)
                    )
                    return

                total = len(cur)
                for idx, nid in enumerate(cur):
                    nlabel = str(names.get(nid) or self._node_names.get(nid) or nid)

                    def _mk_up(i=idx, key=compound):
                        def _up(_e):
                            arr = list(level_value_custom_map.get(key) or [])
                            if i <= 0 or i >= len(arr):
                                return
                            arr[i - 1], arr[i] = arr[i], arr[i - 1]
                            level_value_custom_map[key] = arr
                            _refresh_block_order_view()
                            # OPTIMIZATION: Use page.update() instead of individual dlg.update()
                            try:
                                dlg.update()
                            except Exception:
                                pass

                        return _up

                    def _mk_down(i=idx, key=compound):
                        def _down(_e):
                            arr = list(level_value_custom_map.get(key) or [])
                            if i < 0 or i >= len(arr) - 1:
                                return
                            arr[i + 1], arr[i] = arr[i], arr[i + 1]
                            level_value_custom_map[key] = arr
                            _refresh_block_order_view()
                            # OPTIMIZATION: Use page.update() instead of individual dlg.update()
                            try:
                                dlg.update()
                            except Exception:
                                pass

                        return _down

                    def _mk_remove(key=compound, node_id=nid):
                        def _remove(_e):
                            arr = [
                                x
                                for x in (level_value_custom_map.get(key) or [])
                                if x != node_id
                            ]
                            if arr:
                                level_value_custom_map[key] = arr
                            else:
                                level_value_custom_map.pop(key, None)
                            _refresh_block_order_view()
                            # OPTIMIZATION: Use page.update() instead of individual dlg.update()
                            try:
                                dlg.update()
                            except Exception:
                                pass

                        return _remove

                    order_col.controls.append(
                        ft.Row(
                            [
                                ft.Text(
                                    nlabel[:30], size=10, color=self._th_text, expand=True
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.KEYBOARD_ARROW_UP,
                                    icon_size=14,
                                    visible=(total > 1 and idx > 0),
                                    on_click=_mk_up(),
                                    style=ft.ButtonStyle(padding=ft.padding.all(0)),
                                    width=18,
                                    height=18,
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.KEYBOARD_ARROW_DOWN,
                                    icon_size=14,
                                    visible=(total > 1 and idx < total - 1),
                                    on_click=_mk_down(),
                                    style=ft.ButtonStyle(padding=ft.padding.all(0)),
                                    width=18,
                                    height=18,
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.CLOSE,
                                    icon_size=12,
                                    on_click=_mk_remove(),
                                    style=ft.ButtonStyle(padding=ft.padding.all(0)),
                                    width=16,
                                    height=16,
                                ),
                            ],
                            spacing=2,
                        )
                    )

            def _add_objects_to_block(_e=None):
                cid = str(cat_dd.value or "").strip()
                t_key = str(level_dd.value or "").strip().lower()
                if not cid or not t_key:
                    return
                compound = _level_key_for(cid, t_key)
                objects = _level_object_options(cid, t_key)
                if not objects:
                    return
                level_value_custom_map[compound] = [nid for nid, _ in objects]
                saved_level_title_by_cat[cid] = t_key
                _refresh_block_order_view()
                # OPTIMIZATION: Use page.update() instead of individual dlg.update()
                try:
                    dlg.update()
                except Exception:
                    pass

            def _remove_block(_e=None):
                if len(level_blocks_col.controls) <= 1:
                    return
                if row_holder in level_blocks_col.controls:
                    level_blocks_col.controls.remove(row_holder)
                # OPTIMIZATION: Use page.update() instead of individual dlg.update()
                try:
                    dlg.update()
                except Exception:
                    pass

            def _add_new_block(_e=None):
                _add_level_block("", "")
                # OPTIMIZATION: Use page.update() instead of individual dlg.update()
                try:
                    dlg.update()
                except Exception:
                    pass

            top_row = ft.Row(
                [
                    cat_dd,
                    level_dd,
                    ft.IconButton(
                        icon=ft.Icons.ADD_CIRCLE_OUTLINE,
                        icon_size=18,
                        tooltip="Додати об'єкти рівня в чергу",
                        on_click=_add_objects_to_block,
                        style=ft.ButtonStyle(padding=ft.padding.all(4)),
                    ),
                    ft.IconButton(
                        icon=ft.Icons.ADD,
                        icon_size=18,
                        tooltip="Додати ще один блок сортування рівнів",
                        on_click=_add_new_block,
                        style=ft.ButtonStyle(padding=ft.padding.all(4)),
                    ),
                    ft.IconButton(
                        icon=ft.Icons.CLOSE,
                        icon_size=16,
                        tooltip="Прибрати блок",
                        on_click=_remove_block,
                        style=ft.ButtonStyle(padding=ft.padding.all(2)),
                    ),
                ],
                spacing=6,
            )

            row_holder.controls = [top_row, order_col]
            level_blocks_col.controls.append(row_holder)

            cat_dd.on_change = _refresh_block_controls
            cat_dd.on_select = _refresh_block_controls
            level_dd.on_change = _refresh_block_controls
            level_dd.on_select = _refresh_block_controls
            _refresh_block_controls()

        seeded_pairs: list[tuple[str, str]] = []
        for raw_key, vals in (level_value_custom_map or {}).items():
            full_key = str(raw_key or "").strip()
            if not full_key or not isinstance(vals, list) or not vals:
                continue
            cid, sep, t_key = full_key.partition("|")
            cid = str(cid or "").strip()
            t_key = str(t_key or "").strip().lower() if sep else ""
            if cid and t_key and (cid, t_key) not in seeded_pairs:
                seeded_pairs.append((cid, t_key))

        if not seeded_pairs:
            default_cid = str(cats_sorted[0].get("id") or "") if cats_sorted else ""
            default_t_key = ""
            opts = _category_level_options(default_cid)
            if opts:
                default_t_key = str(getattr(opts[0], "key", "") or "").strip().lower()
            seeded_pairs = [(default_cid, default_t_key)]

        for cid, t_key in seeded_pairs:
            _add_level_block(cid, t_key)

        dlg = ft.AlertDialog(modal=True)

        def _close(_=None):
            pg.pop_dialog()

        def _save(_=None):
            if not selected:
                selected.add("name")
            self._visible_columns = set(selected)
            self._column_order_ids = list(ordered_ids)
            self._save_visible_columns_state()
            try:
                n = max(1, min(24, int(str(months_f.value or "3").strip() or "3")))
            except Exception:
                n = 3
            self._visible_months_count = n
            self._pin_last_archive_month = bool(pin_cb.value)
            self._save_month_view_settings()

            self._category_custom_order_ids = list(cat_order_selected)
            self._level_value_custom_order_by_title_key = {
                str(k).strip(): [str(v).strip() for v in (vals or []) if str(v).strip()]
                for k, vals in (level_value_custom_map or {}).items()
                if str(k).strip()
            }

            # Auto-bind category -> active level title when level order exists.
            auto_cat_level: dict[str, str] = {}
            for raw_key, vals in (
                self._level_value_custom_order_by_title_key or {}
            ).items():
                full_key = str(raw_key or "").strip()
                if not full_key or not isinstance(vals, list) or not vals:
                    continue
                cid, sep, t_key = full_key.partition("|")
                cid = str(cid or "").strip()
                t_key = str(t_key or "").strip().lower() if sep else ""
                if cid and t_key and cid not in auto_cat_level:
                    auto_cat_level[cid] = t_key
            if auto_cat_level:
                self._category_level_sort_title_key = auto_cat_level
                if not str(self._default_level_sort_title_key or "").strip():
                    self._default_level_sort_title_key = (
                        str(next(iter(auto_cat_level.values())) or "").strip().lower()
                    )

            self._save_sorting_settings()

            pg.pop_dialog()
            self._schedule_rebuild()

        def _cols_help(_):
            from views.help_utils import show_modal_help
            _pg2 = self._pg()
            show_modal_help(_pg2, self.t("media.dlg.cols_months", "Стовпці та місяці"), [
                self.t("media.cols_help.step1", "1. Вмикайте/вимикайте потрібні стовпці."),
                self.t("media.cols_help.step2", "2. Вкажіть кількість видимих місяців."),
                self.t("media.cols_help.step3", "3. Налаштуйте черговість відображення категорій."),
                self.t("media.cols_help.step4", "4. Натисніть «Зберегти»."),
            ])

        dlg.title = ft.Row(
            [
                ft.Text(self.t("media.dlg.cols_months", "Стовпці та місяці"), expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_cols_help),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
        )
        dlg.content = ft.Container(
            width=430,
            content=ft.Column(
                [
                    rows_host,
                    ft.Divider(height=8),
                    months_f,
                    pin_cb,
                    ft.Divider(height=8),
                    ft.Text("Черговість категорій", size=10, color=self._th_text),
                    ft.Row(
                        [
                            cat_add_dd,
                            ft.IconButton(
                                icon=ft.Icons.ADD_CIRCLE_OUTLINE,
                                icon_size=18,
                                tooltip="Додати категорію в чергу",
                                on_click=_add_category_to_order,
                                style=ft.ButtonStyle(padding=ft.padding.all(4)),
                            ),
                        ],
                        spacing=6,
                    ),
                    cat_order_col,
                    ft.Text("Сортування по рівням", size=10, color=self._th_text),
                    level_blocks_col,
                ],
                spacing=6,
            ),
        )

        dlg.actions = [
            ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_close),
            ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save),
        ]
        dlg.actions_alignment = ft.MainAxisAlignment.END
        pg.show_dialog(dlg)

    def _open_table_layout_dialog(self, _e=None) -> None:
        pg = self._pg()
        if not pg:
            return

        def _close(_=None):
            pg.pop_dialog()

        def _num(txt, default):
            try:
                s = str(txt or "").strip().replace(",", ".")
                if s == "":
                    return float(default)
                return float(s)
            except Exception:
                return float(default)

        name_w_f = ft.TextField(
            label=self.t("media.field.product_name", "Назва товару"),
            value=str(int(self._table_layout.get("name_w") or _NAME_W)),
            width=180,
        )
        cmt_w_f = ft.TextField(
            label=self.t("orders.field.comment", "Коментар"),
            value=str(int(self._table_layout.get("comment_w") or _COMMENT_W)),
            width=180,
        )
        days_w_f = ft.TextField(
            label=self.t("media.field.days_pub", "Днів з публ."),
            value=str(int(self._table_layout.get("days_w") or _DAYS_W)),
            width=180,
        )
        archive_w_f = ft.TextField(
            label=self.t("media.field.archive", "Архів"),
            value=str(int(self._table_layout.get("archive_w") or _ARCHIVE_SECTION_W)),
            width=180,
        )
        month_col_w_f = ft.TextField(
            label=self.t("media.field.month", "Місяць"),
            value=str(int(self._table_layout.get("month_col_w") or _MONTH_COL_W)),
            width=180,
        )
        row_h_f = ft.TextField(
            label=self.t("media.field.row_height", "Висота рядка (px)"),
            value=str(int(self._table_layout.get("row_h") or _ROW_H)),
            width=200,
        )
        txt_size_f = ft.TextField(
            label=self.t("media.field.text_size", "Розмір тексту"),
            value=str(int(self._table_layout.get("text_size") or 11)),
            width=200,
        )
        wrap_cb = ft.Checkbox(
            label=self.t("media.field.text_wrap", "Перенесення тексту"),
            value=bool(self._table_layout.get("text_wrap")),
        )

        def _save(_=None):
            try:
                self._table_layout["name_w"] = int(
                    max(
                        _PX_COL_MIN,
                        min(_PX_COL_MAX, int(_num(name_w_f.value, _NAME_W))),
                    )
                )
                self._table_layout["comment_w"] = int(
                    max(
                        _PX_COL_MIN,
                        min(_PX_COL_MAX, int(_num(cmt_w_f.value, _COMMENT_W))),
                    )
                )
                self._table_layout["days_w"] = int(
                    max(
                        _PX_COL_MIN,
                        min(_PX_COL_MAX, int(_num(days_w_f.value, _DAYS_W))),
                    )
                )
                self._table_layout["archive_w"] = int(
                    max(
                        _PX_COL_MIN,
                        min(
                            _PX_COL_MAX,
                            int(_num(archive_w_f.value, _ARCHIVE_SECTION_W)),
                        ),
                    )
                )
                self._table_layout["month_col_w"] = int(
                    max(
                        _PX_COL_MIN,
                        min(_PX_COL_MAX, int(_num(month_col_w_f.value, _MONTH_COL_W))),
                    )
                )
                self._table_layout["row_h"] = int(
                    max(_PX_ROW_MIN, min(_PX_ROW_MAX, int(_num(row_h_f.value, _ROW_H))))
                )
                self._table_layout["text_size"] = int(
                    min(
                        _PX_TEXT_MAX, max(_PX_TEXT_MIN, int(_num(txt_size_f.value, 11)))
                    )
                )
                self._table_layout["text_h_align"] = "center"
                self._table_layout["text_v_align"] = "center"
                self._table_layout["text_wrap"] = bool(wrap_cb.value)
            except Exception:
                pass
            self._save_table_layout_state()
            _close()
            self._schedule_rebuild()

        dlg = ft.AlertDialog(modal=True)
        def _sizes_help(_):
            from views.help_utils import show_modal_help
            _pg2 = self._pg()
            show_modal_help(_pg2, self.t("media.dlg.col_width_height", "Стовпчик / Ширина / Висота"), [
                self.t("media.sizes_help.step1", "1. Введіть ширину (px) для кожного стовпчика."),
                self.t("media.sizes_help.step2", "2. Вкажіть висоту рядка і розмір тексту."),
                self.t("media.sizes_help.step3", "3. Натисніть «Зберегти»."),
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
                    ft.Text("Ширини стовпчиків (px)", size=11, color=self._th_text),
                    ft.Row([name_w_f, cmt_w_f], wrap=True, spacing=8),
                    ft.Row([days_w_f, archive_w_f], wrap=True, spacing=8),
                    ft.Row([month_col_w_f], wrap=True, spacing=8),
                    ft.Divider(height=8),
                    row_h_f,
                    txt_size_f,
                    wrap_cb,
                ],
                spacing=8,
                tight=True,
            ),
        )
        dlg.actions = [
            ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_close),
            ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save),
        ]
        dlg.actions_alignment = ft.MainAxisAlignment.END
        pg.show_dialog(dlg)

    def _open_table_dialog(self, _e=None) -> None:
        """Combined dialog with two tabs: Стовпчики and Розміри."""
        pg = self._pg()
        if not pg:
            return

        # ── Build Tab 1: Стовпчики (columns) content ──────────────────────────
        selected = set(self._visible_columns or {"name", "comment", "days"})
        defs = {key: label for key, label, _w_key in self._all_media_column_defs()}
        ordered_ids = list(self._ordered_media_column_ids())
        rows_host = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO)

        def _render_rows() -> None:
            controls = []
            total = len(ordered_ids)
            for idx, key in enumerate(ordered_ids):
                label = str(defs.get(key) or key)
                cb = ft.Checkbox(label=label, value=(key in selected))

                def _toggle(e, current_key=key):
                    if bool(getattr(e.control, "value", False)):
                        selected.add(current_key)
                    else:
                        selected.discard(current_key)

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
                controls.append(
                    ft.Row(
                        [
                            ft.Container(width=232, content=cb),
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
                        spacing=2,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )
            rows_host.controls = controls
            with contextlib.suppress(Exception):
                rows_host.update()

        _render_rows()

        months_f = ft.TextField(
            label=self.t("media.field.months_count", "Кількість місяців"),
            value=str(self._visible_months_count),
            width=200,
            hint_text="1–24",
            keyboard_type=ft.KeyboardType.NUMBER,
        )
        pin_cb = ft.Checkbox(
            label=self.t("media.field.show_prev_month", "Показувати попередній місяць"),
            value=self._pin_last_archive_month,
        )

        _UA_DAYS = [
            self.t("media.day.0", "Пн"), self.t("media.day.1", "Вт"),
            self.t("media.day.2", "Ср"), self.t("media.day.3", "Чт"),
            self.t("media.day.4", "Пт"), self.t("media.day.5", "Сб"),
            self.t("media.day.6", "Нд"),
        ]
        _day_checks: list[tuple[int, ft.Checkbox]] = [
            (i, ft.Checkbox(label=lbl, value=(i in self._allowed_post_weekdays)))
            for i, lbl in enumerate(_UA_DAYS)
        ]
        if is_mobile(pg):
            days_row = make_scrollable_chip_row(
                [cb for _, cb in _day_checks], spacing=4
            )
        else:
            days_row = ft.Row([cb for _, cb in _day_checks], spacing=4, wrap=True)

        def _save_columns(_=None):
            if not selected:
                selected.add("name")
            self._visible_columns = set(selected)
            self._column_order_ids = list(ordered_ids)
            self._save_visible_columns_state()
            try:
                n = max(1, min(24, int(str(months_f.value or "3").strip() or "3")))
            except Exception:
                n = 3
            self._visible_months_count = n
            self._pin_last_archive_month = bool(pin_cb.value)
            self._save_month_view_settings()
            new_days = {i for i, cb in _day_checks if bool(cb.value)}
            self._allowed_post_weekdays = new_days or {0, 1, 2, 3, 4, 5, 6}
            self._save_post_days_settings()
            pg.pop_dialog()
            self._restart_tab()

        tab1_content = ft.Container(
            width=430,
            content=ft.Column(
                [
                    rows_host,
                    ft.Divider(height=8),
                    months_f,
                    pin_cb,
                    ft.Divider(height=8),
                    ft.Text("Дні для постів", size=11, color=self._th_text, weight=ft.FontWeight.W_500),
                    days_row,
                    ft.Divider(height=8),
                    ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save_columns),
                ],
                spacing=6,
                scroll=ft.ScrollMode.AUTO,
            ),
            height=480,
        )

        # ── Build Tab 2: Розміри (layout) content ─────────────────────────────
        def _num(txt, default):
            try:
                s = str(txt or "").strip().replace(",", ".")
                if s == "":
                    return float(default)
                return float(s)
            except Exception:
                return float(default)

        name_w_f = ft.TextField(
            label=self.t("media.field.product_name", "Назва товару"),
            value=str(int(self._table_layout.get("name_w") or _NAME_W)),
            width=180,
        )
        cmt_w_f = ft.TextField(
            label=self.t("orders.field.comment", "Коментар"),
            value=str(int(self._table_layout.get("comment_w") or _COMMENT_W)),
            width=180,
        )
        days_w_f = ft.TextField(
            label=self.t("media.field.days_pub", "Днів з публ."),
            value=str(int(self._table_layout.get("days_w") or _DAYS_W)),
            width=180,
        )
        archive_w_f = ft.TextField(
            label=self.t("media.field.archive", "Архів"),
            value=str(int(self._table_layout.get("archive_w") or _ARCHIVE_SECTION_W)),
            width=180,
        )
        month_col_w_f = ft.TextField(
            label=self.t("media.field.month", "Місяць"),
            value=str(int(self._table_layout.get("month_col_w") or _MONTH_COL_W)),
            width=180,
        )
        row_h_f = ft.TextField(
            label=self.t("media.field.row_height", "Висота рядка (px)"),
            value=str(int(self._table_layout.get("row_h") or _ROW_H)),
            width=200,
        )
        txt_size_f = ft.TextField(
            label=self.t("media.field.text_size", "Розмір тексту"),
            value=str(int(self._table_layout.get("text_size") or 11)),
            width=200,
        )
        wrap_cb2 = ft.Checkbox(
            label=self.t("media.field.text_wrap", "Перенесення тексту"),
            value=bool(self._table_layout.get("text_wrap")),
        )

        def _save_layout(_=None):
            try:
                self._table_layout["name_w"] = int(
                    max(
                        _PX_COL_MIN,
                        min(_PX_COL_MAX, int(_num(name_w_f.value, _NAME_W))),
                    )
                )
                self._table_layout["comment_w"] = int(
                    max(
                        _PX_COL_MIN,
                        min(_PX_COL_MAX, int(_num(cmt_w_f.value, _COMMENT_W))),
                    )
                )
                self._table_layout["days_w"] = int(
                    max(
                        _PX_COL_MIN,
                        min(_PX_COL_MAX, int(_num(days_w_f.value, _DAYS_W))),
                    )
                )
                self._table_layout["archive_w"] = int(
                    max(
                        _PX_COL_MIN,
                        min(
                            _PX_COL_MAX,
                            int(_num(archive_w_f.value, _ARCHIVE_SECTION_W)),
                        ),
                    )
                )
                self._table_layout["month_col_w"] = int(
                    max(
                        _PX_COL_MIN,
                        min(_PX_COL_MAX, int(_num(month_col_w_f.value, _MONTH_COL_W))),
                    )
                )
                self._table_layout["row_h"] = int(
                    max(_PX_ROW_MIN, min(_PX_ROW_MAX, int(_num(row_h_f.value, _ROW_H))))
                )
                self._table_layout["text_size"] = int(
                    min(
                        _PX_TEXT_MAX, max(_PX_TEXT_MIN, int(_num(txt_size_f.value, 11)))
                    )
                )
                self._table_layout["text_h_align"] = "center"
                self._table_layout["text_v_align"] = "center"
                self._table_layout["text_wrap"] = bool(wrap_cb2.value)
            except Exception:
                pass
            self._save_table_layout_state()
            pg.pop_dialog()
            self._schedule_rebuild()

        tab2_content = ft.Container(
            width=430,
            content=ft.Column(
                [
                    ft.Text("Ширини стовпчиків (px)", size=11, color=self._th_text),
                    ft.Row([name_w_f, cmt_w_f], wrap=True, spacing=8),
                    ft.Row([days_w_f, archive_w_f], wrap=True, spacing=8),
                    ft.Row([month_col_w_f], wrap=True, spacing=8),
                    ft.Divider(height=8),
                    row_h_f,
                    txt_size_f,
                    wrap_cb2,
                    ft.Divider(height=8),
                    ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save_layout),
                ],
                spacing=8,
                tight=True,
                scroll=ft.ScrollMode.AUTO,
            ),
            height=400,
        )

        # ── Combine into AlertDialog with Tabs ────────────────────────────────
        combined_dlg = ft.AlertDialog(modal=True)

        def _close_combined(_=None):
            pg.pop_dialog()

        def _tbl_help(_):
            from views.help_utils import show_modal_help
            _pg2 = self._pg()
            show_modal_help(_pg2, self.t("payments.dlg.table_title", "Таблиця"), [
                self.t("media.tbl_help.step1", "1. У розділі «Стовпчики» вмикайте/вимикайте потрібні стовпці."),
                self.t("media.tbl_help.step2", "2. У розділі «Розміри» налаштуйте ширину стовпців і висоту рядків."),
                self.t("media.tbl_help.step3", "3. Натисніть «Закрити» для застосування."),
            ])

        combined_dlg.title = ft.Row(
            [
                ft.Text(self.t("payments.dlg.table_title", "Таблиця"), expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_tbl_help),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
        )
        combined_dlg.content = ft.Container(
            width=460,
            height=540,
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
                        "Розміри", size=12, weight=ft.FontWeight.W_600, color=self._th_text
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
        pg.show_dialog(combined_dlg)

    def _pg(self):
        if not self.content_area:
            return None
        try:
            return self.content_area.page
        except RuntimeError:
            return None

    def _snack(self, text: str, color: str = _ACCENT):
        pg = self._pg()
        if not pg:
            return
        try:
            pg.snack_bar = ft.SnackBar(
                content=ft.Text(text, color="white"), bgcolor=color, duration=3000
            )
            pg.snack_bar.open = True
            pg.update()
        except Exception:
            pass

    def _show_help(self) -> None:
        from views.help_utils import show_modal_help
        page = self._pg()
        show_modal_help(page, self.t("media.help.title", "Медіа План"), [
            self.t("media.help.desc", "Планування графіку публікацій товарів у соціальних мережах."),
            (self.t("media.help.steps_title", "Як користуватись"), None),
            self.t("media.help.step1", "1. Рядки — товари, стовпці — дні місяців."),
            self.t("media.help.step2", "2. Клікніть на клітинку, щоб додати або прибрати дату публікації."),
            self.t("media.help.step3", "3. Кнопка «Таблиця» налаштовує видимі стовпці та їх розміри."),
            self.t("media.help.step4", "4. Можна об'єднати рядки товарів в один пост або додати додатковий пост."),
            self.t("media.help.step5", "5. Фільтри відбирають товари, що потребують уваги (>14 днів без публікацій)."),
            self.t("media.help.step6", "6. «Відкрити місяць з архіву» — переглянути публікації за минулі місяці."),
        ])

    # ── Deferred rebuild helper ───────────────────────────────────────────────

    def _schedule_rebuild(self) -> None:
        """Schedule a deferred rebuild so rapid changes batch into one render."""
        if getattr(self, "_rebuild_scheduled", False):
            return
        self._rebuild_scheduled = True
        pg = self._pg()
        if pg and hasattr(pg, "run_task"):

            async def _do():
                await asyncio.sleep(0.02)
                self._rebuild_scheduled = False
                with contextlib.suppress(Exception):
                    self._render_loading_stub()
                await asyncio.to_thread(self._rebuild, False)

            pg.run_task(_do)
        else:
            self._rebuild_scheduled = False
            self._rebuild(reload=False)

    def apply_text_search(self, query: str) -> None:
        """Ctrl+F row filter: hide rows whose Товар/коментар don't match `query`."""
        self._search_text_query = str(query or "").strip()
        self._schedule_rebuild()

    def clear_text_search(self) -> None:
        self.apply_text_search("")

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

    def _is_entry_owned(self, row: dict) -> bool:
        """Return True when the current manager owns this media-plan entry."""
        eid = str(row.get("entry_id") or "").strip()
        if not eid:
            return True  # virtual / unsaved row → allow edit
        row_manager_id = str(row.get("manager_id") or "").strip()
        if row_manager_id and row_manager_id == str(self.manager_id or "").strip():
            return True
        return eid in self._owned_entry_ids

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

    # ── Pinned horizontal scrollbar helpers ───────────────────────────────────

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

    def _resolve_region_id(self) -> str | None:
        name = self.active_region
        if not name:
            return None
        regions = self.db.get_company_regions(self.company_id) or []
        target = name.strip().lower()
        for r in regions:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id") or "").strip()
            rname = str(r.get("name") or "").strip().lower()
            if rid == name or rname == target:
                return rid or None
        return None

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

    def _load(self, force: bool = False):
        if self._initial_loaded and not force:
            return
        self._region_id = self._resolve_region_id()
        if self._region_id and not self.is_read_only:
            try:
                if not self.db.manager_has_category_access_in_region(
                    self.manager_id, self.company_id, self._region_id
                ):
                    self.is_read_only = True
            except Exception:
                pass
        raw_products = (
            self.db.get_products(
                self.company_id,
                region_id=self._region_id,
                manager_id=self.manager_id,
            )
            or []
        )
        # Deduplicate by (name, category_id, supplier_id) — only collapse rows that
        # are truly the same product (same name + category + supplier).
        _dedup: dict[tuple, dict] = {}
        for _p in raw_products:
            if not isinstance(_p, dict):
                continue
            _key = (
                str(_p.get("name") or "").strip().lower(),
                str(_p.get("category_id") or "").strip(),
                str(_p.get("supplier_id") or "").strip(),
            )
            _existing = _dedup.get(_key)
            if _existing is None:
                _dedup[_key] = _p
            else:
                # Keep the row with newer updated_at
                if str(_p.get("updated_at") or "") > str(
                    _existing.get("updated_at") or ""
                ):
                    _dedup[_key] = _p
        self._products = list(_dedup.values())
        self._set_loading_text(f"Товари: {len(self._products)}")

        # ── Cache-first for entries ────────────────────────────────────────────
        db_pulse = self._entries_scope_pulse()
        cached_pulse = self._load_entries_cache()
        if cached_pulse is None or db_pulse > cached_pulse:
            self._entries = (
                self.db.get_media_plan_entries(
                    self.company_id,
                    manager_id=self.manager_id,
                    region_id=self._region_id,
                )
                or []
            )
            self._save_entries_cache(db_pulse)
        # else: self._entries populated from cache by _load_entries_cache()
        self._set_loading_text(f"Записи: {len(self._entries or [])}")

        # Re-apply any pending (un-flushed) outbox items over loaded data.
        if self._ops_outbox:
            self._apply_outbox_to_entries()
        # Auto-remove entries whose product_ids are all orphaned (product deleted from Товари).
        self._cleanup_orphaned_entries()
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
                    if title_key not in unique_titles:
                        unique_titles[title_key] = (title_key, title, pos)
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
                        unique_titles[title_key] = (title_key, title, pos)

            self._level_title_options = [
                (k, title)
                for k, title, _pos in sorted(
                    unique_titles.values(), key=lambda item: (item[2], item[1].lower())
                )
            ]
        except Exception:
            self._level_title_options = []
            self._category_level_pos_by_title = {}
            self._title_default_pos = {}

        try:
            nodes = self.db.get_company_catalog_nodes(self.company_id) or []
            self._node_names = {
                str(n.get("id") or "").strip(): str(n.get("name") or "").strip()
                for n in nodes
                if isinstance(n, dict) and (n.get("id") or "")
            }
            self._catalog_nodes_by_id = {
                str(n.get("id") or "").strip(): n
                for n in nodes
                if isinstance(n, dict) and str(n.get("id") or "").strip()
            }
            # print(f"[_load] _node_names loaded: {len(self._node_names)} nodes")
            # print(f"[_load] _products loaded: {len(self._products)} products")
            # print(
            #     f"[_load] _category_level_pos_by_title keys: {list(self._category_level_pos_by_title.keys())}"
            # )
            # print(f"[_load] _title_default_pos: {self._title_default_pos}")
        except Exception as e:
            print(f"[_load] ERROR loading nodes: {e}")
            self._node_names = {}
        self._load_month_view_settings()
        self._load_post_days_settings()
        self._load_sorting_settings()
        try:
            self._owned_entry_ids = {
                str(x)
                for x in (
                    self.db.get_entity_owner_entity_ids(
                        self.company_id, self.manager_id, "media_plan"
                    )
                    or []
                )
                if str(x).strip()
            }
        except Exception:
            self._owned_entry_ids = set()
        self._initial_loaded = True

    @staticmethod
    def _latest_date(dates: list[str]) -> str | None:
        if not dates:
            return None
        try:
            return max(str(d)[:10] for d in dates if d)
        except Exception:
            return None

    @staticmethod
    def _picker_iso_date(ev) -> str | None:
        """Extract an ISO date from DatePicker event without timezone day-shift."""

        def _normalize(raw_val) -> str | None:
            if raw_val is None:
                return None
            if isinstance(raw_val, datetime):
                dt = raw_val
                if dt.tzinfo is not None:
                    try:
                        dt = dt.astimezone()
                    except Exception:
                        pass
                return dt.date().isoformat()
            if isinstance(raw_val, date):
                return raw_val.isoformat()

            txt = str(raw_val).strip().strip('"')
            if not txt:
                return None

            if txt.isdigit():
                try:
                    num = int(txt)
                    ts = (num / 1000.0) if num > 100000000000 else float(num)
                    return datetime.fromtimestamp(ts).date().isoformat()
                except Exception:
                    pass

            # Parse full datetime (with timezone) before fallback to YYYY-MM-DD head.
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
            return txt[:10] if txt else None

        try:
            raw = getattr(ev, "data", "")
        except Exception:
            raw = None
        if raw:
            v = _normalize(raw)
            if v:
                return v

        picked = getattr(getattr(ev, "control", None), "value", None)
        return _normalize(picked)

    @staticmethod
    def _level_filter_key(pos: int, title: str) -> str:
        return f"{int(pos)}|{str(title or '').strip().lower()}"

    def _categories_by_id(self) -> dict[str, str]:
        return {
            str((c or {}).get("id") or "").strip(): str(c.get("name") or "").strip()
            for c in (self._categories or [])
            if isinstance(c, dict) and (c.get("id") or "")
        }

    def _product_name_suffixes(self) -> dict[str, str]:
        """Returns {product_id → suffix} — " | Кат: ... | Постач: ..." for
        products sharing a name with a different category and/or supplier.
        See views/product_naming.py for the shared rule."""
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
            # flat category_id, which used to make same-named products with
            # different categories look identical (no suffix shown at all).
            return product_naming.resolve_category_label(
                p, node_path_of=_node_path_of, category_by_id=cat_by_id
            )

        return product_naming.compute_name_suffixes(
            self._products or [],
            category_of=_category_of,
            supplier_of=product_naming.resolve_supplier_label,
        )

    def _product_display_name(
        self, product: dict, suffixes: dict | None = None
    ) -> str:
        prod = product if isinstance(product, dict) else {}
        name = str(prod.get("name") or "").strip() or "-"
        _suf = suffixes if isinstance(suffixes, dict) else self._product_name_suffixes()
        pid = str(prod.get("id") or "").strip()
        return name + (_suf.get(pid, "") if pid else "")

    def _filtered_products(self) -> list[dict]:
        out = []
        for prod in self._products:
            if self._filter_category_ids:
                cat = str(prod.get("category_id") or "").strip()
                if cat not in self._filter_category_ids:
                    continue
            ok = True
            for level_key, node_id in (self._filter_level_node_ids or {}).items():
                if not node_id:
                    continue
                parts = str(level_key or "").split("|", 1)
                try:
                    pos = int(parts[0])
                except Exception:
                    continue
                if str(prod.get(f"level_{int(pos)}_node_id") or "").strip() != node_id:
                    ok = False
                    break
            if ok:
                out.append(prod)
        return out

    def _build_rows(self) -> list[dict]:
        prod_by_id = {
            str(p.get("id") or ""): p
            for p in (self._products or [])
            if isinstance(p, dict) and (p.get("id") or "")
        }
        cat_name_by_id = self._categories_by_id()
        dup_name_keys = self._product_name_suffixes()

        def _row_category_name(pids: list[str]) -> str:
            names = []
            for pid in pids or []:
                prod = prod_by_id.get(str(pid) or "") or {}
                cid = str(prod.get("category_id") or "").strip()
                cname = str(cat_name_by_id.get(cid) or "").strip()
                if cname and cname not in names:
                    names.append(cname)
            if not names:
                return ""
            return " / ".join(sorted(names, key=lambda x: x.lower()))

        def _row_primary_category_id(pids: list[str]) -> str:
            cat_ids = []
            for pid in pids or []:
                prod = prod_by_id.get(str(pid) or "") or {}
                cid = str(prod.get("category_id") or "").strip()
                if cid and cid not in cat_ids:
                    cat_ids.append(cid)
            return sorted(cat_ids)[0] if cat_ids else ""

        def _row_level_name_map(pids: list[str]) -> dict[int, str]:
            out: dict[int, str] = {}
            for lvl in self._enabled_levels or []:
                try:
                    pos = int(lvl.get("position") or 0)
                except Exception:
                    continue
                if not (1 <= pos <= 6):
                    continue
                vals = []
                for pid in pids or []:
                    prod = prod_by_id.get(str(pid) or "") or {}
                    nid = str(prod.get(f"level_{pos}_node_id") or "").strip()
                    if not nid:
                        continue
                    nname = str(self._node_names.get(nid) or nid).strip()
                    if nname and nname not in vals:
                        vals.append(nname)
                out[pos] = (
                    " / ".join(sorted(vals, key=lambda x: x.lower())) if vals else ""
                )
            return out

        # Use catalog nodes cached by _load() — no DB query on every rebuild.
        _catalog_nodes_by_id: dict[str, dict] = self._catalog_nodes_by_id or {}

        def _row_level_node_map(pids: list[str]) -> dict[int, str]:
            out: dict[int, str] = {}
            nodes_by_id = _catalog_nodes_by_id  # use pre-fetched map

            for lvl in self._enabled_levels or []:
                try:
                    pos = int(lvl.get("position") or 0)
                except Exception:
                    continue
                if not (1 <= pos <= 6):
                    continue
                nodes: list[tuple[str, str]] = []
                for pid in pids or []:
                    prod = prod_by_id.get(str(pid) or "") or {}
                    # Try old format first
                    nid = str(prod.get(f"level_{pos}_node_id") or "").strip()
                    if not nid:
                        # Traverse hierarchy from catalog_node_id
                        prod_node_id = str(prod.get("catalog_node_id") or "").strip()
                        if prod_node_id:
                            current_node_id = prod_node_id
                            visited = set()
                            while current_node_id and current_node_id not in visited:
                                visited.add(current_node_id)
                                current_node = nodes_by_id.get(current_node_id)
                                if not current_node:
                                    break
                                current_level = int(
                                    current_node.get("level_position", 0) or 0
                                )
                                if current_level == pos:
                                    nid = current_node_id
                                    break
                                current_node_id = str(
                                    current_node.get("parent_node_id") or ""
                                ).strip()
                    if not nid:
                        continue
                    nname = str(self._node_names.get(nid) or nid).strip()
                    pair = (nname.lower(), nid)
                    if pair not in nodes:
                        nodes.append(pair)
                if nodes:
                    nodes.sort(key=lambda x: (x[0], x[1]))
                    out[pos] = str(nodes[0][1] or "").strip()
            return out

        pid_to_entry = {}
        my_mid = str(self.manager_id or "").strip()

        def _entry_priority(en: dict) -> tuple:
            en_mid = str(en.get("manager_id") or "").strip()
            en_id = str(en.get("id") or "").strip()
            pids = _jlist(en.get("product_ids"))
            # Prefer current manager rows first, then explicitly owned rows,
            # then merged rows (more informative), then newest-ish timestamps.
            return (
                1 if my_mid and en_mid == my_mid else 0,
                1 if en_id and en_id in self._owned_entry_ids else 0,
                1 if len(pids) > 1 else 0,
                str(en.get("updated_at") or en.get("created_at") or ""),
                en_id,
            )

        for en in self._entries:
            for pid in _jlist(en.get("product_ids")):
                key = str(pid)
                prev = pid_to_entry.get(key)
                if prev is None or _entry_priority(en) > _entry_priority(prev):
                    pid_to_entry[key] = en

        rows = []
        seen_eids: set[str] = set()

        for prod in sorted(
            self._filtered_products(), key=lambda p: str(p.get("name") or "").lower()
        ):
            pid = str(prod.get("id") or "")
            en = pid_to_entry.get(pid)
            if en:
                eid = str(en.get("id") or "")
                if eid in seen_eids:
                    continue
                seen_eids.add(eid)
                pids = [str(x) for x in _jlist(en.get("product_ids"))]
                _stored_pnames = [str(x) for x in _jlist(en.get("product_names"))]
                pnames = []
                for _i, _pid in enumerate(pids):
                    _prod = prod_by_id.get(_pid) or {}
                    _fresh = (
                        self._product_display_name(_prod, dup_name_keys)
                        if _prod
                        else ""
                    )
                    if not _fresh or _fresh == "-":
                        _fresh = (
                            _stored_pnames[_i] if _i < len(_stored_pnames) else _pid
                        )
                    pnames.append(_fresh)
                dates = sorted(
                    set(str(d)[:10] for d in _jlist(en.get("post_dates")) if d),
                    reverse=True,
                )
                rows.append(
                    {
                        "entry_id": eid,
                        "manager_id": str(en.get("manager_id") or "").strip(),
                        "product_ids": pids,
                        "product_names": pnames,
                        "display_label": str(en.get("display_label") or "").strip(),
                        "is_additional_post": False,
                        "category_id": _row_primary_category_id(pids),
                        "category_name": _row_category_name(pids),
                        "level_name_by_pos": _row_level_name_map(pids),
                        "level_node_id_by_pos": _row_level_node_map(pids),
                        "comment": str(en.get("comment") or "-"),
                        "post_dates": dates,
                        "days_since_publication": _days_ago(self._latest_date(dates)),
                        "not_relevant": bool(en.get("not_relevant", False)),
                        "virtual": False,
                        "merged": len(pids) > 1,
                    }
                )
            else:
                rows.append(
                    {
                        "entry_id": None,
                        "manager_id": str(self.manager_id or "").strip(),
                        "product_ids": [pid],
                        "product_names": [
                            self._product_display_name(prod, dup_name_keys)
                        ],
                        "is_additional_post": False,
                        "category_id": str(prod.get("category_id") or "").strip(),
                        "category_name": str(
                            cat_name_by_id.get(
                                str(prod.get("category_id") or "").strip()
                            )
                            or ""
                        ).strip(),
                        "level_name_by_pos": _row_level_name_map([pid]),
                        "level_node_id_by_pos": _row_level_node_map([pid]),
                        "comment": "-",
                        "post_dates": [],
                        "days_since_publication": None,
                        "not_relevant": False,
                        "virtual": True,
                        "merged": False,
                    }
                )

        # Entries without any matching product are intentionally omitted —
        # the product list (Товари tab) is the single source of truth.

        rows = [r for r in rows if self._row_matches_quick_filter(r)]
        if self._search_text_query:
            rows = [
                r
                for r in rows
                if row_text_matches(
                    self._search_text_query,
                    *(r.get("product_names") or []),
                    r.get("comment"),
                )
            ]
        rows = self._sort_rows(rows)
        return rows

    @staticmethod
    def _row_title_sort_value(row: dict) -> str:
        names = row.get("product_names") or []
        return " + ".join(str(x) for x in names if str(x).strip()).strip().lower()

    def _row_effective_level_title_key(self, row: dict) -> str:
        cid = str(row.get("category_id") or "").strip()
        explicit = (
            str(
                (self._category_level_sort_title_key or {}).get(
                    cid, self._default_level_sort_title_key
                )
                or ""
            )
            .strip()
            .lower()
        )
        if explicit:
            return explicit

        # Fallback: infer active title key from saved custom level ordering for this category.
        for raw_key, vals in (
            self._level_value_custom_order_by_title_key or {}
        ).items():
            full_key = str(raw_key or "").strip()
            if not full_key or not isinstance(vals, list) or not vals:
                continue
            k_cid, sep, k_title = full_key.partition("|")
            if not sep:
                continue
            if str(k_cid or "").strip() != cid:
                continue
            t_key = str(k_title or "").strip().lower()
            if t_key:
                return t_key

        return ""

    def _row_level_sort_pos(self, row: dict) -> int:
        cid = str(row.get("category_id") or "").strip()
        title_key = self._row_effective_level_title_key(row)
        if not title_key:
            return 0
        by_cat = (self._category_level_pos_by_title or {}).get(cid) or {}
        try:
            return int(
                by_cat.get(
                    title_key,
                    (self._title_default_pos or {}).get(title_key, 0),
                )
                or 0
            )
        except Exception:
            return 0

    def _row_level_sort_value(self, row: dict) -> str:
        pos = self._row_level_sort_pos(row)
        if pos <= 0:
            return ""
        lvl_map = row.get("level_name_by_pos") or {}
        return str(lvl_map.get(pos) or "").strip().lower()

    @staticmethod
    def _norm_sort_token(v: str) -> str:
        return " ".join(str(v or "").strip().lower().split())

    def _col_sort_cycle(self, key: str, _e=None) -> None:
        """Cycle sort for a column: off → asc → desc → off."""
        if self._col_sort_key != key:
            self._col_sort_key = key
            self._col_sort_dir = 1
        elif self._col_sort_dir == 1:
            self._col_sort_dir = 2
        elif self._col_sort_dir == 2:
            self._col_sort_key = ""
            self._col_sort_dir = 0
        self._schedule_rebuild()

    def _toggle_inactive_section(self, _e=None) -> None:
        self._inactive_section_collapsed = not self._inactive_section_collapsed
        self._schedule_rebuild()

    def _sort_rows(self, rows: list[dict]) -> list[dict]:
        out = list(rows or [])

        # ── Column sort (overrides category/item sort when active) ────────────
        if self._col_sort_key and self._col_sort_dir:
            _ck = self._col_sort_key
            _rev = self._col_sort_dir == 2

            _month_prefix = _ck[:7]  # "2026-06" from "2026-06-01"

            def _col_key(r: dict):
                if _ck == "name":
                    names = r.get("product_names") or []
                    lbl = (" + ".join(sorted(names))).lower()
                    # "" is smallest → first in asc, last in desc
                    return lbl  # empty string sorts correctly on its own
                if _ck == "days":
                    d = r.get("days_since_publication")
                    # -1 < any real value → empty first in asc, last in desc
                    return -1 if d is None else int(d)
                # month key: compare first 7 chars "2026-06"
                mds = sorted([x for x in (r.get("post_dates") or []) if str(x)[:7] == _month_prefix])
                # "" < any date string → empty first in asc, last in desc
                return mds[-1] if mds else ""

            active_out = [r for r in out if not r.get("not_relevant", False)]
            inactive_out = [r for r in out if r.get("not_relevant", False)]
            active_out.sort(key=_col_key, reverse=_rev)
            return self._apply_inactive_grouping(active_out, inactive_out)

        out.sort(
            key=lambda r: self._row_title_sort_value(r),
            reverse=(self._item_sort_order == "desc"),
        )

        level_value_rank_by_cat_title: dict[tuple[str, str], dict[str, int]] = {}
        for raw_key, vals in (
            self._level_value_custom_order_by_title_key or {}
        ).items():
            full_key = str(raw_key or "").strip()
            if not full_key or not isinstance(vals, list):
                continue
            cat_id, sep, title_key = full_key.partition("|")
            cid = str(cat_id or "").strip()
            tk = str(title_key or "").strip().lower() if sep else ""
            if not cid or not tk:
                continue
            ranks: dict[str, int] = {}
            idx = 0
            for raw in vals:
                node_id = str(raw or "").strip()
                if not node_id or node_id in ranks:
                    continue
                ranks[node_id] = idx
                idx += 1
            if ranks:
                level_value_rank_by_cat_title[(cid, tk)] = ranks

        out.sort(
            key=lambda r: (
                self._row_level_sort_value(r) == "",
                (
                    level_value_rank_by_cat_title.get(
                        (
                            str(r.get("category_id") or "").strip(),
                            self._row_effective_level_title_key(r),
                        ),
                        {},
                    ).get(
                        str(
                            (r.get("level_node_id_by_pos") or {}).get(
                                self._row_level_sort_pos(r), ""
                            )
                            or ""
                        ).strip(),
                        10**9,
                    )
                ),
                self._row_level_sort_value(r),
            ),
        )

        rank_map = {
            str(cid).strip(): idx
            for idx, cid in enumerate(self._category_custom_order_ids or [])
            if str(cid).strip()
        }
        if rank_map:
            listed = [
                r for r in out if str(r.get("category_id") or "").strip() in rank_map
            ]
            rest = [
                r
                for r in out
                if str(r.get("category_id") or "").strip() not in rank_map
            ]
            listed.sort(
                key=lambda r: rank_map.get(
                    str(r.get("category_id") or "").strip(), 10**9
                )
            )
            rest.sort(
                key=lambda r: (
                    str(r.get("category_name") or "").strip() == "",
                    str(r.get("category_name") or "").strip().lower(),
                ),
                reverse=(self._category_sort_order == "desc"),
            )
            out = listed + rest
        else:
            out.sort(
                key=lambda r: (
                    str(r.get("category_name") or "").strip() == "",
                    str(r.get("category_name") or "").strip().lower(),
                ),
                reverse=(self._category_sort_order == "desc"),
            )

        active_out = [r for r in out if not r.get("not_relevant", False)]
        inactive_out = [r for r in out if r.get("not_relevant", False)]
        return self._apply_inactive_grouping(active_out, inactive_out)

    def _apply_inactive_grouping(
        self, active_rows: list[dict], inactive_rows: list[dict]
    ) -> list[dict]:
        """Append inactive rows after active rows, optionally collapsed under a header."""
        if not inactive_rows:
            return active_rows
        header: dict = {
            "key": "__inactive_header__",
            "item_type": "inactive_header",
            "inactive_count": len(inactive_rows),
            "product_names": [],
            "not_relevant": False,
            "virtual": True,
            "display_label": f"Неактивні ({len(inactive_rows)})",
        }
        if self._inactive_section_collapsed:
            return active_rows + [header]
        return active_rows + [header] + inactive_rows

    def _update_entries_in_place(self, payload: dict) -> None:
        """Merge payload into self._entries so rebuild and cache stay current."""
        entry_id = str(payload.get("id") or "").strip()
        if not entry_id:
            return
        for i, en in enumerate(self._entries):
            if isinstance(en, dict) and str(en.get("id") or "").strip() == entry_id:
                self._entries[i] = {**en, **payload}
                return
        self._entries.append(dict(payload))

    def _save_row(self, row: dict) -> bool:
        if self.is_read_only:
            return False
        if not row.get("entry_id"):
            row["entry_id"] = uuid.uuid4().hex
            row["virtual"] = False
        payload = {
            "id": row["entry_id"],
            "company_id": self.company_id,
            "manager_id": self.manager_id,
            "region_id": self._region_id,
            "product_ids": row.get("product_ids") or [],
            "product_names": row.get("product_names") or [],
            "display_label": row.get("display_label") or "",
            "comment": row.get("comment") or "",
            "not_relevant": bool(row.get("not_relevant", False)),
            "post_dates": sorted(set(row.get("post_dates") or []), reverse=True),
        }
        entry_id = str(payload["id"])
        self._update_entries_in_place(payload)
        self._ops_outbox[f"entry:{entry_id}"] = {
            "kind": "entry",
            "data": payload,
        }
        self._save_ops_outbox()
        self._refresh_workspace_sync_indicator_state()
        return True

    def _save_row_async(self, row: dict, *, reload_on_error: bool = True) -> None:
        payload = {
            "id": row.get("entry_id") or uuid.uuid4().hex,
            "company_id": self.company_id,
            "manager_id": self.manager_id,
            "region_id": self._region_id,
            "product_ids": list(row.get("product_ids") or []),
            "product_names": list(row.get("product_names") or []),
            "display_label": row.get("display_label") or "",
            "comment": row.get("comment") or "",
            "not_relevant": bool(row.get("not_relevant", False)),
            "post_dates": sorted(set(row.get("post_dates") or []), reverse=True),
        }
        entry_id = str(payload["id"])
        row["entry_id"] = entry_id
        row["virtual"] = False
        self._update_entries_in_place(payload)
        self._ops_outbox[f"entry:{entry_id}"] = {
            "kind": "entry",
            "data": payload,
        }
        self._save_ops_outbox()
        self._refresh_workspace_sync_indicator_state()

    def _open_entry_label_edit_dialog(self, row: dict) -> None:
        """Dialog to set a custom display name for a merged (multi-product) media-plan entry."""
        pg = self._pg()
        if not pg:
            return
        pnames = row.get("product_names") or []
        current = str(row.get("display_label") or "").strip()
        hint = " + ".join(pnames[:3]) + ("..." if len(pnames) > 3 else "")
        tf = ft.TextField(
            label="Своя назва",
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
                self.t("media.custom_name_help.step1", "1. Введіть власну назву для об'єднаного рядка товарів."),
                self.t("media.custom_name_help.step2", "2. «Скинути» — видалити власну назву і повернути автоматичну."),
                self.t("media.custom_name_help.step3", "3. Натисніть «Зберегти»."),
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
            # Also update the backing entry in self._entries so next rebuild
            # picks up the new label without waiting for a DB round-trip.
            eid = str(row.get("entry_id") or "").strip()
            if eid:
                for en in self._entries:
                    if isinstance(en, dict) and str(en.get("id") or "").strip() == eid:
                        en["display_label"] = new_label
                        break
            _close()
            self._save_row_async(row, reload_on_error=False)
            self._schedule_rebuild()

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
        pg.open(dlg)

    def _open_additional_post_dialog(self, _=None):
        pg = self._pg()
        if not pg:
            return

        name_f = ft.TextField(
            label="Назва додаткового поста",
            hint_text="Наприклад: Рекламний пост TikTok",
            width=360,
            autofocus=True,
        )
        comment_f = ft.TextField(
            label="Коментар (опц.)",
            width=360,
            multiline=True,
            min_lines=2,
            max_lines=4,
        )
        dlg = ft.AlertDialog(modal=True)

        def _close(_e=None):
            pg.pop_dialog()

        def _save(_e=None):
            title = str(name_f.value or "").strip()
            if not title:
                self._snack(self.t("media.err.enter_extra_post_name", "Вкажіть назву додаткового поста"), _ERR_C)
                return
            new_eid = uuid.uuid4().hex
            new_row = {
                "entry_id": new_eid,
                "manager_id": self.manager_id,
                "product_ids": [],
                "product_names": [title],
                "is_additional_post": True,
                "category_id": "",
                "category_name": "",
                "level_name_by_pos": {},
                "level_node_id_by_pos": {},
                "comment": str(comment_f.value or "-").strip() or "-",
                "post_dates": [],
                "days_since_publication": None,
                "not_relevant": False,
                "virtual": False,
                "merged": False,
            }
            # Add to in-memory entries so _build_rows picks it up.
            self._entries.append(
                {
                    "id": new_eid,
                    "company_id": self.company_id,
                    "manager_id": self.manager_id,
                    "region_id": self._region_id,
                    "product_ids": [],
                    "product_names": [title],
                    "comment": new_row["comment"],
                    "not_relevant": False,
                    "post_dates": [],
                }
            )
            self._save_row_async(new_row)
            pg.pop_dialog()
            self._schedule_rebuild()
            self._snack(self.t("media.extra_post_created", "Додатковий пост створено"), _OK_C)

        def _extra_post_help(_):
            from views.help_utils import show_modal_help
            _pg2 = self._pg()
            show_modal_help(_pg2, self.t("media.dlg.new_extra_post", "Новий додатковий пост"), [
                self.t("media.extra_post_help.step1", "1. Введіть назву поста (наприклад, «Рекламний пост TikTok»)."),
                self.t("media.extra_post_help.step2", "2. Додайте коментар (опціонально)."),
                self.t("media.extra_post_help.step3", "3. Натисніть «Створити»."),
            ])

        dlg.title = ft.Row(
            [
                ft.Text(self.t("media.dlg.new_extra_post", "Новий додатковий пост"), expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_extra_post_help),
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
        )
        dlg.content = ft.Container(
            width=400,
            content=ft.Column([name_f, comment_f], spacing=10, tight=True),
        )
        dlg.actions = [
            ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_close),
            ft.FilledButton(self.t("media.create_btn", "Створити"), on_click=_save),
        ]
        dlg.actions_alignment = ft.MainAxisAlignment.END
        pg.show_dialog(dlg)

    def _delete_additional_post(self, row: dict, _e=None):
        if self.is_read_only:
            return
        entry_id = str(row.get("entry_id") or "").strip()
        if not entry_id:
            return
        if not bool(row.get("is_additional_post", False)):
            return
        # Remove from local entries immediately
        self._entries = [
            e
            for e in self._entries
            if str((e or {}).get("id") or "").strip() != entry_id
        ]
        # Cancel any pending upsert for this entry, then queue delete
        self._ops_outbox.pop(f"entry:{entry_id}", None)
        self._ops_outbox[f"entry_delete:{entry_id}"] = {
            "kind": "entry_delete",
            "data": {"id": entry_id},
        }
        self._save_ops_outbox()
        self._selected.discard(self._sel_key(row))
        # Rebuild from updated local state — no DB reload needed
        self._schedule_rebuild()
        self._refresh_workspace_sync_indicator_state()
        self._snack(self.t("media.extra_post_deleted", "Додатковий пост видалено"), _OK_C)

    def _merge_selected(self, e=None):
        if self.is_read_only:
            return
        if len(self._selected) < 2:
            return
        rows = self._build_rows()
        sel = [r for r in rows if self._sel_key(r) in self._selected]
        if len(sel) < 2:
            return

        all_pids: list = []
        all_pnames: list = []
        all_dates: set = set()
        merged_cmt = ""
        for r in sel:
            for pid, pname in zip(
                r.get("product_ids") or [], r.get("product_names") or []
            ):
                if pid not in all_pids:
                    all_pids.append(pid)
                    all_pnames.append(pname)
            all_dates.update(r.get("post_dates") or [])
            if not merged_cmt and r.get("comment") and r.get("comment") != "-":
                merged_cmt = str(r.get("comment") or "")

        new_entry = {
            "id": uuid.uuid4().hex,
            "company_id": self.company_id,
            "manager_id": self.manager_id,
            "region_id": self._region_id,
            "product_ids": all_pids,
            "product_names": all_pnames,
            "display_label": "",
            "comment": merged_cmt,
            "not_relevant": False,
            "post_dates": sorted(all_dates, reverse=True),
        }
        ids_to_delete = [r["entry_id"] for r in sel if r.get("entry_id")]
        n_pids = len(all_pids)
        new_eid = str(new_entry["id"])

        # Keep _owned_entry_ids in sync: merged entry takes ownership, old entries lose it.
        for eid in ids_to_delete:
            self._owned_entry_ids.discard(eid)
        self._owned_entry_ids.add(new_eid)

        # Instant: update self._entries + outbox, then rebuild UI locally
        self._entries = [
            e
            for e in self._entries
            if isinstance(e, dict)
            and str(e.get("id") or "").strip() not in set(ids_to_delete)
        ]
        self._entries.append(dict(new_entry))
        for eid in ids_to_delete:
            self._ops_outbox[f"entry_delete:{eid}"] = {
                "kind": "entry_delete",
                "data": {"id": eid},
            }
            self._ops_outbox.pop(f"entry:{eid}", None)
        self._ops_outbox[f"entry:{new_eid}"] = {"kind": "entry", "data": new_entry}
        self._save_ops_outbox()
        self._refresh_workspace_sync_indicator_state()
        self._selected.clear()
        self._schedule_rebuild()
        self._snack(self.t("media.merged_products", "Об'єднано {n} товари в один пост").format(n=n_pids), _OK_C)

    def _split(self, entry_id: str, e=None):
        en = next(
            (x for x in self._entries if str(x.get("id") or "") == entry_id), None
        )
        if not en:
            return
        pids = _jlist(en.get("product_ids"))
        pnames = _jlist(en.get("product_names"))
        dates = sorted(set(_jlist(en.get("post_dates"))), reverse=True)
        cmt = str(en.get("comment") or "")
        not_rel = bool(en.get("not_relevant", False))
        n_pids = len(pids)
        entries_to_create = [
            {
                "id": uuid.uuid4().hex,
                "company_id": self.company_id,
                "manager_id": self.manager_id,
                "region_id": self._region_id,
                "product_ids": [pid],
                "product_names": [pname],
                "display_label": "",
                "comment": cmt,
                "not_relevant": not_rel,
                "post_dates": list(dates),
            }
            for pid, pname in zip(pids, pnames)
        ]
        # Keep _owned_entry_ids in sync: new split entries take ownership, old merged loses it.
        self._owned_entry_ids.discard(entry_id)
        for new_en in entries_to_create:
            self._owned_entry_ids.add(str(new_en["id"]))

        # Instant: update self._entries + outbox, rebuild UI locally
        self._entries = [
            e
            for e in self._entries
            if not (isinstance(e, dict) and str(e.get("id") or "").strip() == entry_id)
        ]
        for new_en in entries_to_create:
            self._entries.append(dict(new_en))
        self._ops_outbox[f"entry_delete:{entry_id}"] = {
            "kind": "entry_delete",
            "data": {"id": entry_id},
        }
        self._ops_outbox.pop(f"entry:{entry_id}", None)
        for new_en in entries_to_create:
            neid = str(new_en["id"])
            self._ops_outbox[f"entry:{neid}"] = {"kind": "entry", "data": new_en}
        self._save_ops_outbox()
        self._refresh_workspace_sync_indicator_state()
        self._selected.discard(entry_id)
        self._schedule_rebuild()
        self._snack(self.t("media.split_rows", "Розділено на {n} рядки").format(n=n_pids), _OK_C)

    def _split_selected(self, e=None):
        if self.is_read_only:
            return
        rows = self._build_rows()
        sel = [
            r
            for r in rows
            if self._sel_key(r) in self._selected
            and r.get("merged")
            and r.get("entry_id")
        ]
        if not sel:
            self._snack(self.t("media.select_merged_to_split", "Оберіть об'єднані рядки для роз'єднання"), "#B45309")
            return

        entry_ids = [r["entry_id"] for r in sel]
        all_entries_to_create = []
        for r in sel:
            en = next(
                (x for x in self._entries if str(x.get("id") or "") == r["entry_id"]),
                None,
            )
            if not en:
                continue
            pids = _jlist(en.get("product_ids"))
            pnames = _jlist(en.get("product_names"))
            dates = sorted(set(_jlist(en.get("post_dates"))), reverse=True)
            cmt = str(en.get("comment") or "")
            not_rel = bool(en.get("not_relevant", False))
            for pid, pname in zip(pids, pnames):
                all_entries_to_create.append(
                    {
                        "id": uuid.uuid4().hex,
                        "company_id": self.company_id,
                        "manager_id": self.manager_id,
                        "region_id": self._region_id,
                        "product_ids": [pid],
                        "product_names": [pname],
                        "comment": cmt,
                        "not_relevant": not_rel,
                        "post_dates": list(dates),
                    }
                )

        total_new = len(all_entries_to_create)

        # Keep _owned_entry_ids in sync: new split entries take ownership, old merged lose it.
        for eid in entry_ids:
            self._owned_entry_ids.discard(eid)
        for new_en in all_entries_to_create:
            self._owned_entry_ids.add(str(new_en["id"]))

        # Instant: update self._entries + outbox, rebuild UI locally
        ids_set = set(entry_ids)
        self._entries = [
            e
            for e in self._entries
            if not (isinstance(e, dict) and str(e.get("id") or "").strip() in ids_set)
        ]
        for new_en in all_entries_to_create:
            self._entries.append(dict(new_en))
        for eid in entry_ids:
            self._ops_outbox[f"entry_delete:{eid}"] = {
                "kind": "entry_delete",
                "data": {"id": eid},
            }
            self._ops_outbox.pop(f"entry:{eid}", None)
        for new_en in all_entries_to_create:
            neid = str(new_en["id"])
            self._ops_outbox[f"entry:{neid}"] = {"kind": "entry", "data": new_en}
        self._save_ops_outbox()
        self._refresh_workspace_sync_indicator_state()
        for eid in entry_ids:
            self._selected.discard(f"e:{eid}")
        self._schedule_rebuild()
        self._snack(self.t("media.unmerged_rows", "Роз'єднано на {n} рядки").format(n=total_new), _OK_C)

    @staticmethod
    def _kpi_chip(label: str, count, bg: str, fg: str):
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
                            str(count), size=9, color="white", weight="bold"
                        ),
                    ),
                ],
                spacing=4,
                tight=True,
            ),
        )

    @staticmethod
    def _vsep():
        return ft.Container(
            width=1,
            height=20,
            bgcolor=_BORDER,
            margin=ft.margin.symmetric(horizontal=4),
        )

    def _hcol(self, text: str, expand: bool = False, width=None):
        kw = {}
        if expand:
            kw["expand"] = True
        elif width:
            kw["width"] = width
        return ft.Container(
            **kw,
            content=ft.Text(text, size=11, weight="bold", color=self._th_text),
            padding=ft.padding.symmetric(horizontal=8, vertical=6),
        )

    def _next_post_date_str(self) -> str | None:
        """Nearest allowed-weekday date with no existing post across all entries."""
        allowed = self._allowed_post_weekdays or {0, 1, 2, 3, 4, 5, 6}
        existing: set[date] = set()
        for entry in self._entries:
            if not isinstance(entry, dict):
                continue
            for ds in _jlist(entry.get("post_dates") or []):
                try:
                    existing.add(date.fromisoformat(str(ds)[:10]))
                except Exception:
                    pass
        _UA_SHORT = {
            0: self.t("media.day.0", "Пн"), 1: self.t("media.day.1", "Вт"),
            2: self.t("media.day.2", "Ср"), 3: self.t("media.day.3", "Чт"),
            4: self.t("media.day.4", "Пт"), 5: self.t("media.day.5", "Сб"),
            6: self.t("media.day.6", "Нд"),
        }
        cur = date.today()
        for _ in range(366):
            if cur.weekday() in allowed and cur not in existing:
                wd = _UA_SHORT.get(cur.weekday(), "")
                return f"({wd}, {cur.strftime('%d.%m.%Y')})"
            cur = cur + timedelta(days=1)
        return None

    def _build_filter_bar(self) -> ft.Container:
        dd_style = {
            "text_size": 11,
            "content_padding": ft.padding.symmetric(horizontal=6, vertical=4),
            "border_radius": 6,
            "height": 32,
            "trailing_icon": ft.Icon(ft.Icons.ARROW_DROP_DOWN, size=0),
            "selected_trailing_icon": ft.Icon(ft.Icons.ARROW_DROP_DOWN, size=0),
        }

        # Only show categories that appear in the user's products
        _product_cat_ids = {
            str(p.get("category_id") or "")
            for p in self._products
            if isinstance(p, dict)
        }
        _visible_cats = [
            c
            for c in self._categories
            if isinstance(c, dict) and str(c.get("id") or "") in _product_cat_ids
        ]

        def _mk_cat_chip(cid: str, label: str, active: bool) -> ft.Container:
            def _toggle(_):
                if cid == "":
                    self._filter_category_ids.clear()
                elif cid in self._filter_category_ids:
                    self._filter_category_ids.discard(cid)
                else:
                    self._filter_category_ids.add(cid)
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

        sorted_cats = sorted(_visible_cats, key=lambda x: (x.get("name") or "").lower())
        cat_chips: list = [_mk_cat_chip("", "Всі", not self._filter_category_ids)]
        for _c in sorted_cats:
            _cid = str(_c.get("id") or "")
            if not _cid:
                continue
            cat_chips.append(
                _mk_cat_chip(
                    _cid,
                    str(_c.get("name") or "")[:30],
                    _cid in self._filter_category_ids,
                )
            )
        cat_filter = ft.Row(cat_chips, spacing=4, scroll=ft.ScrollMode.HIDDEN)

        level_dds = []
        for row in self._enabled_levels:
            pos = int(row.get("position") or 0)
            title = str(row.get("title") or f"Рівень {pos}").strip() or f"Рівень {pos}"
            level_key = self._level_filter_key(pos, title)
            nodes = {}
            for p in self._products:
                nid = str((p or {}).get(f"level_{pos}_node_id") or "").strip()
                if nid and nid not in nodes:
                    nodes[nid] = self._node_names.get(nid, nid)
            if not nodes:
                continue

            dd = ft.Dropdown(
                value=str((self._filter_level_node_ids or {}).get(level_key, "") or ""),
                label=title,
                options=[ft.dropdown.Option("", f"Всі: {title}")]
                + [
                    ft.dropdown.Option(nid, (name or nid)[:44])
                    for nid, name in sorted(
                        nodes.items(), key=lambda kv: (kv[1] or "").lower()
                    )
                ],
                width=150,
                **dd_style,
            )

            def _mk_on_level(k=level_key, cur_dd=dd):
                def _on(e):
                    self._filter_level_node_ids[k] = str(cur_dd.value or "").strip()
                    self._schedule_rebuild()

                return _on

            dd.on_change = _mk_on_level()
            dd.on_select = _mk_on_level()
            level_dds.append(dd)

        def _toggle_adv(e):
            self._adv_filters_open = not self._adv_filters_open
            self._schedule_rebuild()

        adv_btn = ft.IconButton(
            icon=(
                ft.Icons.ADD_CIRCLE_OUTLINE
                if not self._adv_filters_open
                else ft.Icons.REMOVE_CIRCLE_OUTLINE
            ),
            icon_size=18,
            icon_color=_ACCENT,
            tooltip="Показати рівні" if not self._adv_filters_open else "Сховати рівні",
            on_click=_toggle_adv,
            visible=bool(level_dds),
            style=ft.ButtonStyle(padding=ft.padding.all(2)),
        )

        controls = [cat_filter, adv_btn]
        if self._adv_filters_open:
            controls += level_dds

        if self._col_sort_key:

            def _reset_sort(_e=None):
                self._col_sort_key = ""
                self._col_sort_dir = 0
                self._schedule_rebuild()

            controls.insert(
                0,
                ft.TextButton(
                    self.t("media.reset_sort", "Скинути сортування"),
                    icon=ft.Icons.SORT,
                    on_click=_reset_sort,
                    style=ft.ButtonStyle(text_style=ft.TextStyle(size=11)),
                    tooltip=self.t("media.tooltip.reset_col_sort", "Скинути сортування колонки"),
                ),
            )

        has_active = bool(self._filter_category_ids) or any(
            v for v in self._filter_level_node_ids.values()
        )
        if has_active:

            def _reset(e):
                self._filter_category_ids = set()
                self._filter_level_node_ids = {}
                self._schedule_rebuild()

            controls.append(
                ft.TextButton(
                    self.t("common.reset", "Скинути"),
                    icon=ft.Icons.FILTER_ALT_OFF,
                    on_click=_reset,
                    style=ft.ButtonStyle(text_style=ft.TextStyle(size=11)),
                )
            )

        next_date = self._next_post_date_str()
        _next_text = ft.Text(next_date or "", size=11, color=self._th_ok)
        _next_chip = ft.Container(
            bgcolor=self._th_ok_bg,
            border=ft.border.all(1, self._th_ok),
            border_radius=999,
            padding=ft.padding.symmetric(horizontal=10, vertical=4),
            content=_next_text,
            visible=bool(next_date),
        )
        self._next_date_chip_ref = (_next_chip, _next_text)
        controls.append(_next_chip)

        return ft.Container(
            padding=ft.padding.symmetric(horizontal=12, vertical=6),
            margin=ft.margin.only(bottom=10),
            content=ft.Row(
                controls,
                spacing=6,
                wrap=False,
                scroll=ft.ScrollMode.ALWAYS,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _fast_refresh_date_chips(self, row: dict) -> None:
        """In-place update of the «Дати» column chip row after a date is
        added or removed — mirrors _fast_refresh_month_cell so this column
        doesn't force a full-table rebuild (visible scroll-to-top flash) for
        every single date edit."""
        key_base = self._row_cell_key_base(row)
        ref = self._date_chips_row_refs.get(key_base)
        if ref is None:
            self._schedule_rebuild()
            return
        ref.controls = self._build_date_chips_list(row)
        try:
            ref.update()
        except Exception:
            self._schedule_rebuild()
            return

        # Also refresh the «days since last post» chip in the left panel,
        # same as _fast_refresh_month_cell.
        days_ref = self._left_row_days_chip_refs.get(key_base)
        if days_ref is not None:
            dates = row.get("post_dates") or []
            days_val = _days_ago(self._latest_date(dates))
            stale_bucket = self._stale_bucket(days_val, dates)
            if stale_bucket == "red":
                d_bg, d_fg = _R_BG, _R_FG
            elif stale_bucket == "yellow":
                d_bg, d_fg = _Y_BG, _Y_FG
            else:
                d_bg, d_fg = _G_BG, _G_FG
            days_lbl = f"{days_val} д." if days_val is not None else "-"
            days_ref.bgcolor = d_bg
            if isinstance(days_ref.content, ft.Text):
                days_ref.content.value = days_lbl
                days_ref.content.color = d_fg
            with contextlib.suppress(Exception):
                days_ref.update()

    def _build_date_chips_list(self, row: dict) -> list:
        chips = []
        close_gap, latest_iso = self._latest_gap_less_than_14(
            row.get("post_dates") or []
        )
        for ds in row.get("post_dates") or []:

            def _mk_remove(d=ds, r=row):
                def _remove(e):
                    r["post_dates"] = [x for x in (r.get("post_dates") or []) if x != d]
                    self._save_row_async(r, reload_on_error=False)
                    self._fast_refresh_date_chips(r)

                return _remove

            _is_close_gap_chip = (
                close_gap
                and latest_iso
                and str(ds)[:10] == latest_iso
                and latest_iso >= date.today().isoformat()
            )
            _chip_bg, _chip_fg = _date_chip_colors(ds, close_gap_today=_is_close_gap_chip)
            chips.append(
                ft.Container(
                    bgcolor=_chip_bg,
                    border_radius=4,
                    margin=ft.margin.only(right=4),
                    padding=ft.padding.only(left=6, right=2, top=2, bottom=2),
                    content=ft.Row(
                        [
                            ft.Text(
                                _fmt_date(ds),
                                size=10,
                                color=_chip_fg,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.CLOSE,
                                icon_size=10,
                                icon_color=self._th_text,
                                on_click=_mk_remove(),
                                style=ft.ButtonStyle(padding=ft.padding.all(0)),
                                width=16,
                                height=16,
                            ),
                        ],
                        spacing=2,
                        tight=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )

        def _add_date(e, r=row):
            pg = self._pg()
            if not pg:
                return

            def _picked(ev):
                ds = self._picker_iso_date(ev)
                if not ds:
                    return
                existing = list(r.get("post_dates") or [])
                if ds not in existing:
                    existing.append(ds)
                    existing.sort(reverse=True)
                    r["post_dates"] = existing
                # Close picker immediately, then save + fast in-place refresh.
                with contextlib.suppress(Exception):
                    pg.close_dialog()
                self._save_row_async(r, reload_on_error=False)
                self._fast_refresh_date_chips(r)

            def _picker_dismiss_add(_=None):
                with contextlib.suppress(Exception):
                    pg.close_dialog()

            dp = ft.DatePicker(
                value=datetime.combine(date.today(), datetime.min.time()),
                first_date=date(2020, 1, 1),
                last_date=date(2100, 12, 31),
                entry_mode=ft.DatePickerEntryMode.CALENDAR_ONLY,
                date_picker_mode=ft.DatePickerMode.DAY,
                locale=ft.Locale("uk", "UA"),
                on_change=_picked,
                on_dismiss=_picker_dismiss_add,
            )
            pg.show_dialog(dp)

        chips.append(
            ft.IconButton(
                icon=ft.Icons.ADD_CIRCLE_OUTLINE,
                icon_size=16,
                icon_color=_ACCENT,
                tooltip="Додати дату публікації",
                on_click=_add_date,
                style=ft.ButtonStyle(padding=ft.padding.all(2)),
            )
        )

        return chips

    def _date_chips(self, row: dict):
        row_ctrl = ft.Row(
            self._build_date_chips_list(row),
            spacing=0,
            wrap=False,
            scroll=ft.ScrollMode.ALWAYS,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        key_base = self._row_cell_key_base(row)
        if key_base:
            self._date_chips_row_refs[key_base] = row_ctrl
        return row_ctrl

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
                    ft.Icon(ft.Icons.NEWSPAPER, size=24, color=self._th_icon),
                    ft.Column(
                        [
                            ft.Text(
                                self.t("media.title", "Медіа План"), size=20, weight="bold", color=self._th_text
                            ),
                            ft.Text(
                                f"{self.region_label} · {self.manager_name}",
                                size=12,
                                color=self._th_muted,
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
        # Load data in background so the spinner renders immediately.
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
        global _ROW_A, _ROW_B, _HDR_BG, _BORDER, _ACCENT, _SEL_BG
        from views import themes as _th_mod
        _th_b = _th_mod.get_active_theme(self.manager_id)
        _ROW_A = _th_b["panel"]
        _ROW_B = _th_b.get("alt", _th_b["panel"])
        _HDR_BG = _th_b["hdr"]
        _BORDER = _th_b["border"]
        _ACCENT = _th_b["icon_color"]
        _SEL_BG = _th_b.get("soft", _th_b["panel"])
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

            # Count media plan entries per manager (merged entries = 1, not per-product)
            def _count_products(mid, _db=_db, _cid=_cid, _rid=_rid):
                entries = (
                    _db.get_media_plan_entries(_cid, manager_id=mid, region_id=_rid)
                    or []
                )
                return len(entries)

            _table_btn = _ft.OutlinedButton(
                "Таблиця",
                icon=_ft.Icons.TABLE_CHART_OUTLINED,
                height=35,
                style=_ft.ButtonStyle(
                    text_style=_ft.TextStyle(size=11),
                    padding=_ft.padding.symmetric(horizontal=10, vertical=2),
                ),
                on_click=lambda _: self._open_columns_dialog(),
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
            # Count media plan entries per region (merged entries = 1)
            _entries2 = _db2.get_media_plan_entries(_cid2, manager_id=_mid2) or []
            _rcounts2: dict[str, int] = {}
            for _e2 in _entries2:
                if not isinstance(_e2, dict):
                    continue
                _r2 = str(_e2.get("region_id") or "").strip()
                if _r2:
                    _rcounts2[_r2] = _rcounts2.get(_r2, 0) + 1

            def _count_rcmp(rid, _c=_rcounts2):
                return _c.get(rid, 0)

            build_region_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={},
                count_getter=_count_rcmp,
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
        pg = self._pg()
        if pg:
            for _b in getattr(pg, '_catman_hscroll_btns', []):
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

    # ── Month view state ──────────────────────────────────────────────────────

    def _load_month_view_settings(self) -> None:
        manager_id = self._workspace_user_id()
        if not manager_id:
            return
        try:
            _from_memory = isinstance(self._workspace_state, dict) and bool(
                self._workspace_state
            )
            payload = (
                self._workspace_state
                if _from_memory
                else (self.db.get_manager_workspace_state(manager_id) or {})
            )
            store = (
                payload.get("media_plan_month_view")
                if isinstance(payload, dict)
                else {}
            )
            _key = self._table_layout_state_key()
            row = store.get(_key) if isinstance(store, dict) else None
            # Diagnostic for the "months count keeps reverting" report — not
            # yet reproduced live, so this stays until it fires once with
            # the actual key/value mismatch visible.
            _log.info(
                "[media-plan-months] LOAD key=%s from_memory=%s row=%s store_keys=%s",
                _key, _from_memory, row,
                list(store.keys()) if isinstance(store, dict) else None,
            )
            if not isinstance(row, dict):
                return
            self._visible_months_count = max(
                1, min(24, int(row.get("visible_months_count") or 3))
            )
            self._pin_last_archive_month = bool(
                row.get("pin_last_archive_month", False)
            )
            raw_keys = row.get("opened_archive_month_keys") or []
            if isinstance(raw_keys, list):
                self._opened_archive_month_keys = [str(k) for k in raw_keys if k]
        except Exception:
            _log.exception("[media-plan-months] LOAD failed")

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
                payload.get("media_plan_month_view")
                if isinstance(payload.get("media_plan_month_view"), dict)
                else {}
            )
            _key = self._table_layout_state_key()
            store[_key] = {
                "visible_months_count": self._visible_months_count,
                "pin_last_archive_month": self._pin_last_archive_month,
                "opened_archive_month_keys": list(self._opened_archive_month_keys),
            }
            _log.info(
                "[media-plan-months] SAVE key=%s visible_months_count=%s",
                _key, self._visible_months_count,
            )
            payload["media_plan_month_view"] = store
            self._workspace_state = payload
            self._queue_workspace_state_save()
        except Exception:
            pass

    def _post_days_local_path(self) -> str:
        safe_cid = (self.company_id or "nocompany").replace("/", "_").replace("\\", "_")
        safe_mgr = (self.manager_id or "nouser").replace("/", "_").replace("\\", "_")
        return os.path.join(
            self._app_data_dir(),
            f"media_plan_post_days_{safe_cid}_{safe_mgr}.json",
        )

    def _load_post_days_settings(self) -> None:
        # Primary: dedicated local file (no region-path ambiguity)
        path = self._post_days_local_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                raw = data.get("allowed_weekdays") if isinstance(data, dict) else None
                if isinstance(raw, list) and raw:
                    parsed = {int(x) for x in raw if str(x).strip().isdigit() and 0 <= int(x) <= 6}
                    if parsed:
                        self._allowed_post_weekdays = parsed
                        return
            except Exception:
                pass
        # Fallback: workspace state (for migration from old format or cross-device restore)
        manager_id = self._workspace_user_id()
        if not manager_id:
            return
        try:
            payload = (
                self._workspace_state
                if isinstance(self._workspace_state, dict) and self._workspace_state
                else (self.db.get_manager_workspace_state(manager_id) or {})
            )
            store = (
                payload.get("media_plan_post_days")
                if isinstance(payload, dict)
                else {}
            )
            if not isinstance(store, dict):
                return
            # Try region-specific key first, then any key in store
            row = store.get(self._table_layout_state_key())
            if not isinstance(row, dict) and store:
                row = next(iter(store.values()), None)
            if not isinstance(row, dict):
                return
            raw = row.get("allowed_weekdays")
            if isinstance(raw, list) and raw:
                parsed = {int(x) for x in raw if str(x).strip().isdigit() and 0 <= int(x) <= 6}
                if parsed:
                    self._allowed_post_weekdays = parsed
        except Exception:
            pass

    def _save_post_days_settings(self) -> None:
        # Save immediately to dedicated local file (reliable across restarts)
        try:
            path = self._post_days_local_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"allowed_weekdays": sorted(self._allowed_post_weekdays)},
                    f,
                    ensure_ascii=False,
                )
            os.replace(tmp, path)
        except Exception:
            pass
        # Also update workspace state for snapshot indicator + 45s DB sync
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
                payload.get("media_plan_post_days")
                if isinstance(payload.get("media_plan_post_days"), dict)
                else {}
            )
            store[self._table_layout_state_key()] = {
                "allowed_weekdays": sorted(self._allowed_post_weekdays),
            }
            payload["media_plan_post_days"] = store
            self._workspace_state = payload
            self._queue_workspace_state_save()
        except Exception:
            pass

    def _load_sorting_settings(self) -> None:
        manager_id = self._workspace_user_id()
        if not manager_id:
            return
        # print(f"[_load_sorting_settings] START, manager_id={manager_id}")
        try:
            payload = (
                self._workspace_state
                if isinstance(self._workspace_state, dict) and self._workspace_state
                else (self.db.get_manager_workspace_state(manager_id) or {})
            )
            store = (
                payload.get("media_plan_sorting") if isinstance(payload, dict) else {}
            )
            row = (
                store.get(self._table_layout_state_key())
                if isinstance(store, dict)
                else None
            )
            # print(
            #     f"[_load_sorting_settings] state_key={self._table_layout_state_key()}, row_found={row is not None}"
            # )
            if not isinstance(row, dict):
                # print("[_load_sorting_settings] No sorting settings in DB")
                return

            cat_order = str(row.get("category_sort_order") or "asc").strip().lower()
            self._category_sort_order = (
                cat_order if cat_order in {"asc", "desc"} else "asc"
            )

            item_order = str(row.get("item_sort_order") or "asc").strip().lower()
            self._item_sort_order = (
                item_order if item_order in {"asc", "desc"} else "asc"
            )

            level_key = (
                str(row.get("default_level_sort_title_key") or "").strip().lower()
            )
            if not level_key:
                try:
                    legacy_pos = int(row.get("default_level_sort_pos") or 0)
                except Exception:
                    legacy_pos = 0
                level_key = ""
                if legacy_pos > 0:
                    for t_key, pos in (self._title_default_pos or {}).items():
                        if int(pos or 0) == legacy_pos:
                            level_key = t_key
                            break
            self._default_level_sort_title_key = level_key

            raw_map = row.get("category_level_sort_title_key")
            if not isinstance(raw_map, dict):
                raw_map = {}
                legacy_map = row.get("category_level_sort_pos") or {}
                if isinstance(legacy_map, dict):
                    for cid, pos_raw in legacy_map.items():
                        c = str(cid or "").strip()
                        if not c:
                            continue
                        try:
                            pos = int(pos_raw or 0)
                        except Exception:
                            pos = 0
                        if pos <= 0:
                            continue
                        by_cat = self._category_level_pos_by_title.get(c) or {}
                        found = ""
                        for t_key, p in by_cat.items():
                            if int(p or 0) == pos:
                                found = t_key
                                break
                        if not found:
                            for t_key, p in (self._title_default_pos or {}).items():
                                if int(p or 0) == pos:
                                    found = t_key
                                    break
                        if found:
                            raw_map[c] = found

            out = {}
            for k, v in (raw_map or {}).items():
                cid = str(k or "").strip()
                if not cid:
                    continue
                t_key = str(v or "").strip().lower()
                out[cid] = t_key
            self._category_level_sort_title_key = out

            raw_order = row.get("category_custom_order_ids") or []
            if isinstance(raw_order, list):
                self._category_custom_order_ids = [
                    str(x).strip() for x in raw_order if str(x).strip()
                ]
            else:
                self._category_custom_order_ids = []

            raw_level_order = row.get("level_value_custom_order_by_title_key") or {}
            lvl_out: dict[str, list[str]] = {}
            if isinstance(raw_level_order, dict):
                for k, vals in raw_level_order.items():
                    full_key = str(k or "").strip()
                    if not full_key or not isinstance(vals, list):
                        continue
                    # Key format: "{cat_id}|{title_key_lower}"
                    seq = [str(x).strip() for x in vals if str(x).strip()]
                    if seq:
                        lvl_out[full_key] = seq
            self._level_value_custom_order_by_title_key = lvl_out
            # print(
            #     f"[_load_sorting_settings] DONE: loaded {len(lvl_out)} level orderings"
            # )
            # for k, v in lvl_out.items():
            #     print(f"[_load_sorting_settings]   {k} -> {len(v)} items")
        except Exception:
            # print(f"[_load_sorting_settings] ERROR: {e}")
            pass

    def _save_sorting_settings(self) -> None:
        manager_id = self._workspace_user_id()
        if not manager_id:
            return
        # print("[_save_sorting_settings] START")
        # print(
        #     f"[_save_sorting_settings] _level_value_custom_order_by_title_key={self._level_value_custom_order_by_title_key}"
        # )
        try:
            payload = (
                dict(self._workspace_state)
                if isinstance(self._workspace_state, dict)
                else {}
            )
            store = (
                payload.get("media_plan_sorting")
                if isinstance(payload.get("media_plan_sorting"), dict)
                else {}
            )
            store[self._table_layout_state_key()] = {
                "category_sort_order": self._category_sort_order,
                "item_sort_order": self._item_sort_order,
                "default_level_sort_title_key": str(
                    self._default_level_sort_title_key or ""
                )
                .strip()
                .lower(),
                "category_level_sort_title_key": {
                    str(k or "").strip(): str(v or "").strip().lower()
                    for k, v in (self._category_level_sort_title_key or {}).items()
                    if str(k or "").strip()
                },
                "category_custom_order_ids": [
                    str(x).strip()
                    for x in (self._category_custom_order_ids or [])
                    if str(x).strip()
                ],
                "level_value_custom_order_by_title_key": {
                    str(k or "")
                    .strip()
                    .lower(): [str(v).strip() for v in (vals or []) if str(v).strip()]
                    for k, vals in (
                        self._level_value_custom_order_by_title_key or {}
                    ).items()
                    if str(k or "").strip()
                },
                # Legacy mirror keys for compatibility with previous builds.
                "default_level_sort_pos": int(
                    self._title_default_pos.get(
                        str(self._default_level_sort_title_key or "").strip().lower(), 0
                    )
                    or 0
                ),
                "category_level_sort_pos": {
                    str(cid or "").strip(): int(
                        
                            (
                                self._category_level_pos_by_title.get(
                                    str(cid or "").strip()
                                )
                                or {}
                            ).get(str(t_key or "").strip().lower(), 0)
                            or (
                                self._title_default_pos.get(
                                    str(t_key or "").strip().lower(), 0
                                )
                            )
                            or 0
                        
                    )
                    for cid, t_key in (
                        self._category_level_sort_title_key or {}
                    ).items()
                    if str(cid or "").strip()
                },
            }
            payload["media_plan_sorting"] = store
            self._workspace_state = payload
            self._queue_workspace_state_save()
            # print(
            #     f"[_save_sorting_settings] SAVED {len(store[self._table_layout_state_key()].get('level_value_custom_order_by_title_key', {}))} level orderings"
            # )
        except Exception:
            # print(f"[_save_sorting_settings] ERROR: {e}")
            pass

    # ── Month navigation helpers ───────────────────────────────────────────────

    def _month_anchors(self) -> list:
        start = _month_start()
        return [_add_months(start, i) for i in range(self._visible_months_count)]

    def _pinned_archive_month(self):
        if not self._pin_last_archive_month:
            return None
        return _add_months(_month_start(), -1)

    def _opened_archive_months(self) -> list:
        today_start = _month_start()
        seen: set = set()
        out = []
        for raw in self._opened_archive_month_keys:
            try:
                d = date.fromisoformat(str(raw)[:10])
                d = _month_start(d)
                if d >= today_start:
                    continue
                key = _month_key(d)
                if key not in seen:
                    seen.add(key)
                    out.append(d)
            except Exception:
                pass
        out.sort()
        return out

    def _visible_month_anchors(self) -> list:
        seen: set = set()
        result = []
        # Opened archive months (sorted oldest first)
        for m in self._opened_archive_months():
            key = _month_key(m)
            if key not in seen:
                seen.add(key)
                result.append(m)
        # Pinned previous month (if not already in opened)
        pinned = self._pinned_archive_month()
        if pinned is not None:
            key = _month_key(pinned)
            if key not in seen:
                seen.add(key)
                result.append(pinned)
        # Current + future months
        for m in self._month_anchors():
            key = _month_key(m)
            if key not in seen:
                seen.add(key)
                result.append(m)
        return result

    def _is_archive_month(self, month: date) -> bool:
        return month < _month_start()

    def _is_pinned_archive_month(self, month: date) -> bool:
        pinned = self._pinned_archive_month()
        if pinned is None:
            return False
        return _month_key(month) == _month_key(pinned)

    def _dates_for_month(self, post_dates: list, month: date) -> list:
        prefix = month.strftime("%Y-%m")
        return sorted([d for d in post_dates if str(d)[:7] == prefix])

    @staticmethod
    def _latest_gap_less_than_14(post_dates: list) -> tuple[bool, str | None]:
        parsed = []
        for raw in post_dates or []:
            try:
                parsed.append(date.fromisoformat(str(raw)[:10]))
            except Exception:
                pass
        uniq = sorted(set(parsed), reverse=True)
        if not uniq:
            return False, None
        latest_iso = uniq[0].isoformat()
        if len(uniq) < 2:
            return False, latest_iso
        return (uniq[0] - uniq[1]).days < 14, latest_iso

    def _row_height_monthly(self, row: dict) -> int:
        if row.get("item_type") == "inactive_header":
            return 32
        post_dates = row.get("post_dates") or []
        max_dates = 1
        for m in self._visible_month_anchors():
            n = min(3, len(self._dates_for_month(post_dates, m)))
            max_dates = max(max_dates, n)
        row_h = int(self._table_layout.get("row_h") or _ROW_H)
        return max(row_h, 34 + max_dates * 24)

    # ── Monthly left panel ────────────────────────────────────────────────────

    def _table_header_left_monthly(self) -> "ft.Container":
        name_w = int(self._table_layout.get("name_w") or _NAME_W)
        comment_w = int(self._table_layout.get("comment_w") or _COMMENT_W)
        days_w = int(self._table_layout.get("days_w") or _DAYS_W)
        _sep = ft.border.only(left=ft.BorderSide(1, _BORDER))
        controls: list = [
            ft.Container(width=_CB_W, content=ft.Checkbox(visible=False)),
            ft.Container(
                width=_ACT_W,
                alignment=ft.Alignment(0, 0),
                border=_sep,
                content=ft.Text(
                    self.t("media.col.inactive_short", "\u041d\u0435\u0430\u043a\u0442."),
                    size=8,
                    weight="bold",
                    color=self._th_text,
                    text_align=ft.TextAlign.CENTER,
                ),
            ),
        ]
        for col_id in self._visible_ordered_media_column_ids():
            if col_id == "name":
                _sk = "name"
                _active = self._col_sort_key == _sk
                controls.append(
                    ft.Container(
                        width=name_w,
                        alignment=ft.Alignment(0, 0),
                        border=_sep,
                        on_click=lambda _, k=_sk: self._col_sort_cycle(k),
                        tooltip=self.t(
                            "media.col.name_sort_tooltip",
                            "\u0421\u043e\u0440\u0442\u0443\u0432\u0430\u0442\u0438 \u0437\u0430 \u043d\u0430\u0437\u0432\u043e\u044e (\u0410-\u042f / \u042f-\u0410)",
                        ),
                        content=ft.Row(
                            [
                                ft.Text(
                                    self.t("media.col.product_name", "\u041d\u0430\u0437\u0432\u0430 \u0442\u043e\u0432\u0430\u0440\u0443"),
                                    size=11,
                                    weight="bold",
                                    color=self._th_text,
                                ),
                                ft.Icon(
                                    ft.Icons.ARROW_UPWARD
                                    if (_active and self._col_sort_dir == 1)
                                    else ft.Icons.ARROW_DOWNWARD
                                    if (_active and self._col_sort_dir == 2)
                                    else ft.Icons.UNFOLD_MORE,
                                    size=12,
                                    color=self._th_text if _active else "#AAAAAA",
                                ),
                            ],
                            spacing=2,
                            alignment=ft.MainAxisAlignment.CENTER,
                        ),
                    )
                )
            elif col_id == "comment":
                controls.append(
                    ft.Container(
                        width=comment_w,
                        alignment=ft.Alignment(0, 0),
                        border=_sep,
                        content=ft.Text(
                            self.t("media.col.comment", "\u041a\u043e\u043c\u0435\u043d\u0442\u0430\u0440"),
                            size=11,
                            weight="bold",
                            color=self._th_text,
                            text_align=ft.TextAlign.CENTER,
                        ),
                    )
                )
            elif col_id == "days":
                _sk = "days"
                _active = self._col_sort_key == _sk
                controls.append(
                    ft.Container(
                        width=days_w,
                        alignment=ft.Alignment(0, 0),
                        border=_sep,
                        on_click=lambda _, k=_sk: self._col_sort_cycle(k),
                        tooltip=self.t(
                            "media.col.days_sort_tooltip",
                            "\u0421\u043e\u0440\u0442\u0443\u0432\u0430\u0442\u0438 \u0437\u0430 \u0434\u043d\u044f\u043c\u0438 \u0437 \u043f\u0443\u0431\u043b\u0456\u043a\u0430\u0446\u0456\u0457",
                        ),
                        content=ft.Row(
                            [
                                ft.Text(
                                    self.t("media.col.days_pub", "\u0414\u043d\u0456 \u0437 \u043f\u0443\u0431\u043b."),
                                    size=11,
                                    weight="bold",
                                    color=self._th_text,
                                ),
                                ft.Icon(
                                    ft.Icons.ARROW_UPWARD
                                    if (_active and self._col_sort_dir == 1)
                                    else ft.Icons.ARROW_DOWNWARD
                                    if (_active and self._col_sort_dir == 2)
                                    else ft.Icons.UNFOLD_MORE,
                                    size=12,
                                    color=self._th_text if _active else "#AAAAAA",
                                ),
                            ],
                            spacing=2,
                            alignment=ft.MainAxisAlignment.CENTER,
                        ),
                    )
                )
        return ft.Container(
            bgcolor=_HDR_BG,
            border=ft.border.all(1, _BORDER),
            border_radius=ft.border_radius.only(top_left=8),
            height=56,
            content=ft.Row(controls, spacing=0),
        )

    def _left_row_monthly(self, row: dict, idx: int, row_h: int) -> "ft.Container":
        if row.get("item_type") == "inactive_header":
            count = int(row.get("inactive_count") or 0)
            label = f"Неактивні ({count})"
            collapsed = self._inactive_section_collapsed
            return ft.Container(
                height=row_h,
                bgcolor="#F5F5F5",
                border=ft.border.only(
                    bottom=ft.BorderSide(1, _BORDER),
                    left=ft.BorderSide(1, _BORDER),
                ),
                padding=ft.padding.symmetric(horizontal=8, vertical=0),
                content=ft.Row(
                    [
                        ft.IconButton(
                            icon=ft.Icons.CHEVRON_RIGHT
                            if collapsed
                            else ft.Icons.EXPAND_MORE,
                            icon_size=16,
                            icon_color="#555555",
                            on_click=self._toggle_inactive_section,
                            style=ft.ButtonStyle(padding=ft.padding.all(2)),
                            width=24,
                            height=24,
                        ),
                        ft.Text(
                            label,
                            size=11,
                            weight="bold",
                            color="#888888",
                            italic=True,
                        ),
                    ],
                    spacing=4,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            )
        sel_key = self._sel_key(row)
        is_sel = sel_key in self._selected
        is_inactive = bool(row.get("not_relevant", False))
        name_w = int(self._table_layout.get("name_w") or _NAME_W)
        comment_w = int(self._table_layout.get("comment_w") or _COMMENT_W)
        days_w = int(self._table_layout.get("days_w") or _DAYS_W)
        txt_size = int(
            max(
                _PX_TEXT_MIN,
                min(_PX_TEXT_MAX, int(self._table_layout.get("text_size") or 11)),
            )
        )
        allow_wrap = bool(self._table_layout.get("text_wrap", True))
        h_align = str(self._table_layout.get("text_h_align") or "center")
        v_align = str(self._table_layout.get("text_v_align") or "center")
        txt_align = (
            ft.TextAlign.CENTER
            if h_align == "center"
            else (ft.TextAlign.RIGHT if h_align == "right" else ft.TextAlign.LEFT)
        )
        vtxt_align = (
            ft.VerticalAlignment.START
            if v_align == "top"
            else (
                ft.VerticalAlignment.END
                if v_align == "bottom"
                else ft.VerticalAlignment.CENTER
            )
        )

        def _on_check(e, key=sel_key):
            if e.control.value:
                self._selected.add(key)
            else:
                self._selected.discard(key)
            self._schedule_rebuild()

        pnames = row.get("product_names") or ["-"]
        _mp_pids = [str(x).strip() for x in (row.get("product_ids") or []) if str(x).strip()]
        _custom_lbl = str(row.get("display_label") or "").strip()
        name_text = (
            _custom_lbl
            if _custom_lbl
            else (" + ".join(pnames) if len(pnames) > 1 else (pnames[0] or "-"))
        )

        def _copy(e, nm=name_text):
            pg = self._pg()
            if pg:
                try:
                    pg.set_clipboard(nm)
                    self._snack(
                        "\u0421\u043a\u043e\u043f\u0456\u0439\u043e\u0432\u0430\u043d\u043e",
                        "#0F172A",
                    )
                except Exception:
                    pass

        inactive_cb = ft.Checkbox(
            label="\u041d\u0435\u0430\u043a\u0442.",
            value=bool(row.get("not_relevant", False)),
            label_style=ft.TextStyle(size=8),
        )

        def _toggle_nr(e, r=row):
            if not self._is_entry_owned(r):
                self._snack(self.t("media.not_owner", "Ви не є власником цього запису"), "#B45309")
                e.control.value = r.get("not_relevant", False)
                try:
                    e.control.update()
                except Exception:
                    pass
                return
            r["not_relevant"] = bool(e.control.value)
            self._save_row(r)
            self._schedule_rebuild()

        inactive_cb.on_change = _toggle_nr

        controls: list = [
            ft.Container(
                width=_CB_W,
                padding=ft.padding.only(left=8),
                content=ft.Checkbox(value=is_sel, on_change=_on_check),
            ),
            ft.Container(
                width=_ACT_W,
                alignment=ft.Alignment(0, 0),
                content=ft.Column(
                    [
                        inactive_cb,
                        ft.IconButton(
                            icon=ft.Icons.DELETE_OUTLINE,
                            icon_size=14,
                            icon_color="#DC2626",
                            tooltip="Видалити додатковий пост",
                            on_click=lambda e, r=row: self._delete_additional_post(
                                r, e
                            ),
                            visible=bool(row.get("is_additional_post", False))
                            and self._is_entry_owned(row),
                            style=ft.ButtonStyle(padding=ft.padding.all(0)),
                            width=18,
                            height=18,
                        ),
                    ],
                    spacing=0,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    alignment=ft.MainAxisAlignment.CENTER,
                    tight=True,
                ),
            ),
        ]
        if "name" in self._visible_columns:
            _is_merged = len(pnames) > 1
            _mp_single_pid = _mp_pids[0] if len(_mp_pids) == 1 else ""
            _btn_total_w = 20 + (20 if _mp_single_pid else 0)
            from views.product_quick_view import product_icon_btn as _mpiqb
            _mp_info_btn = (
                _mpiqb(_mp_single_pid, self.content_area, self.db, self.user_data, size=13)
                if _mp_single_pid
                else ft.Container(width=0, height=0, visible=False)
            )
            controls.append(
                ft.Container(
                    width=name_w,
                    height=row_h,
                    padding=ft.padding.symmetric(horizontal=4),
                    alignment=ft.alignment.Alignment(0, 0),
                    content=ft.Row(
                        [
                            ft.Container(
                                width=max(60, name_w - _btn_total_w - 8),
                                alignment=ft.alignment.Alignment(0, 0),
                                tooltip=(
                                    ft.Tooltip(message=name_text, wait_duration=0)
                                    if name_text else None
                                ),
                                content=ft.Text(
                                    name_text,
                                    size=txt_size,
                                    color=self._th_text,
                                    no_wrap=not allow_wrap,
                                    max_lines=(4 if allow_wrap else 1),
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                    text_align=txt_align,
                                    selectable=True,
                                    weight=(
                                        ft.FontWeight.W_600
                                        if _is_merged
                                        else ft.FontWeight.W_400
                                    ),
                                ),
                            ),
                            _mp_info_btn,
                            ft.IconButton(
                                icon=ft.Icons.EDIT_OUTLINED,
                                icon_size=13,
                                tooltip="\u0421\u0432\u043e\u044f \u043d\u0430\u0437\u0432\u0430",
                                visible=not self.is_read_only,
                                on_click=lambda _e, r=row: self._open_entry_label_edit_dialog(
                                    r
                                ),
                                style=ft.ButtonStyle(padding=ft.padding.all(0)),
                                width=20,
                                height=20,
                            ),
                        ],
                        spacing=0,
                        tight=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )
        if "comment" in self._visible_columns:
            comment_tf = ft.TextField(
                value=row.get("comment") or "",
                hint_text="\u041a\u043e\u043c\u0435\u043d\u0442\u0430\u0440...",
                border=ft.InputBorder.OUTLINE,
                content_padding=ft.padding.symmetric(horizontal=6, vertical=6),
                text_size=txt_size,
                color=self._th_text,
                multiline=allow_wrap,
                min_lines=1,
                max_lines=(5 if allow_wrap else 1),
                width=max(60, comment_w - 10),
                text_align=txt_align,
                text_vertical_align=vtxt_align,
                read_only=self.is_read_only,
            )

            def _mk_save_cmt(r=row, tf=comment_tf):
                def _save(e):
                    r["comment"] = tf.value or ""
                    self._save_row(r)

                return _save

            comment_tf.on_blur = _mk_save_cmt()
            comment_tf.on_submit = _mk_save_cmt()
            controls.append(
                ft.Container(
                    width=comment_w,
                    height=row_h,
                    padding=ft.padding.symmetric(horizontal=4, vertical=4),
                    content=comment_tf,
                )
            )
        if "days" in self._visible_columns:
            dates = row.get("post_dates") or []
            days = _days_ago(self._latest_date(dates))
            stale_bucket = self._stale_bucket(days, dates)
            if stale_bucket == "red":
                d_bg, d_fg = _R_BG, _R_FG
            elif stale_bucket == "yellow":
                d_bg, d_fg = _Y_BG, _Y_FG
            else:
                d_bg, d_fg = _G_BG, _G_FG
            days_lbl = f"{days} \u0434." if days is not None else "-"
            _days_inner = ft.Container(
                bgcolor=d_bg,
                border_radius=6,
                padding=ft.padding.symmetric(horizontal=6, vertical=3),
                content=ft.Text(
                    days_lbl,
                    size=txt_size,
                    color=d_fg,
                    text_align=txt_align,
                    no_wrap=True,
                ),
            )
            _key_base_days = self._row_cell_key_base(row)
            if _key_base_days:
                self._left_row_days_chip_refs[_key_base_days] = _days_inner
            controls.append(
                ft.Container(
                    width=days_w,
                    alignment=ft.Alignment(0, 0),
                    padding=ft.padding.symmetric(horizontal=4, vertical=4),
                    content=_days_inner,
                )
            )
        return ft.Container(
            height=row_h,
            bgcolor=(
                _ROW_INACTIVE
                if is_inactive
                else (_SEL_BG if is_sel else (_ROW_B if idx % 2 else _ROW_A))
            ),
            border=ft.border.all(1, _BORDER),
            content=ft.Row(
                controls, spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER
            ),
        )

    # ── Monthly right panel ───────────────────────────────────────────────────

    def _table_header_right_monthly(self, visible_months: list) -> "ft.Container":
        archive_w = int(self._table_layout.get("archive_w") or _ARCHIVE_SECTION_W)
        opened = self._opened_archive_months()
        pinned = self._pinned_archive_month()
        chips: list = []
        for m in opened:
            key = _month_key(m)
            chips.append(
                ft.Container(
                    bgcolor=self._th_chip_bg,
                    border_radius=999,
                    padding=ft.padding.only(left=8, right=4, top=2, bottom=2),
                    content=ft.Row(
                        [
                            ft.Text(_month_title(m, self.t), size=9, color=self._th_chip_text),
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
        opened_keys = {_month_key(m) for m in opened}
        if pinned is not None and _month_key(pinned) not in opened_keys:
            chips.append(
                ft.Container(
                    bgcolor=self._th_chip_bg,
                    border_radius=999,
                    padding=ft.padding.symmetric(horizontal=8, vertical=3),
                    content=ft.Text(
                        f"\U0001f4cc {_month_title(pinned, self.t)}", size=9, color=self._th_chip_text
                    ),
                    tooltip=self.t("media.col.pinned_month", "\u0417\u0430\u043a\u0440\u0456\u043f\u043b\u0435\u043d\u0438\u0439 \u043c\u0456\u0441\u044f\u0446\u044c"),
                )
            )
        month_hdrs: list = []
        for m in visible_months:
            is_arc = self._is_archive_month(m)
            is_pin = self._is_pinned_archive_month(m)
            label = _month_title(m, self.t)
            color = self._th_chip_text if is_pin else (self._th_muted if is_arc else self._th_text)
            _mk = _month_key(m)
            _active = self._col_sort_key == _mk
            month_hdrs.append(
                ft.Container(
                    width=int(self._table_layout.get("month_col_w") or _MONTH_COL_W),
                    padding=ft.padding.symmetric(horizontal=8, vertical=4),
                    on_click=lambda _, k=_mk: self._col_sort_cycle(k),
                    tooltip=self.t("media.col.month_sort_tooltip", "Сортувати по останній даті"),
                    content=ft.Column(
                        [
                            ft.Text(
                                label,
                                size=11,
                                weight="bold",
                                color=color,
                                text_align=ft.TextAlign.CENTER,
                            ),
                            ft.Icon(
                                ft.Icons.ARROW_UPWARD
                                if (_active and self._col_sort_dir == 1)
                                else ft.Icons.ARROW_DOWNWARD
                                if (_active and self._col_sort_dir == 2)
                                else ft.Icons.UNFOLD_MORE,
                                size=10,
                                color=self._th_text if _active else "#AAAAAA",
                            ),
                        ],
                        spacing=1,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                )
            )
        archive_section = ft.Container(
            width=archive_w,
            padding=ft.padding.symmetric(horizontal=8, vertical=6),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text(
                                self.t("media.col.archive", "\u0410\u0440\u0445\u0456\u0432"),
                                size=11,
                                weight="bold",
                                color=self._th_text,
                            ),
                            ft.Row(expand=True),
                            ft.IconButton(
                                icon=ft.Icons.ADD_CIRCLE_OUTLINE,
                                icon_size=16,
                                tooltip=self.t("media.col.open_archive_month", "\u0412\u0456\u0434\u043a\u0440\u0438\u0442\u0438 \u043c\u0456\u0441\u044f\u0446\u044c \u0437 \u0430\u0440\u0445\u0456\u0432\u0443"),
                                on_click=self._open_archive_month_picker,
                                style=ft.ButtonStyle(padding=ft.padding.all(0)),
                                width=20,
                                height=20,
                            ),
                        ],
                        spacing=4,
                    ),
                    ft.Row(
                        chips,
                        spacing=4,
                        scroll=ft.ScrollMode.ALWAYS,
                    ),
                ],
                spacing=2,
            ),
        )
        return ft.Container(
            bgcolor=_HDR_BG,
            border=ft.border.all(1, _BORDER),
            border_radius=ft.border_radius.only(top_right=8),
            height=56,
            content=ft.Row(
                [archive_section] + month_hdrs,
                spacing=0,
            ),
        )

    def _row_cell_key_base(self, row: dict) -> str:
        """Stable identity for a row's cached cell/chip widget refs.

        entry_id is None for a not-yet-saved (virtual) row and only becomes a
        real UUID after the FIRST date is saved — using entry_id as the cache
        key meant every virtual row's ref was registered under an empty-string
        key at render time, then looked up under its brand-new UUID right
        after that first save, always missing and falling back to a full
        table rebuild (visible scroll-to-top). product_ids stays the same
        before and after that first save, so keying on it instead keeps the
        cached ref valid across the whole row lifecycle.
        """
        pids = sorted(str(x) for x in (row.get("product_ids") or []) if x)
        return ",".join(pids) or str(row.get("entry_id") or "").strip()

    def _build_month_chips(self, row: dict, month: date) -> list:
        """Build date chip widgets for one month cell. Used by both initial build and in-place refresh."""
        dates_in_month = self._dates_for_month(row.get("post_dates") or [], month)
        is_past = self._is_archive_month(month)
        is_inactive = bool(row.get("not_relevant", False))
        close_gap, latest_iso = self._latest_gap_less_than_14(
            row.get("post_dates") or []
        )

        chips: list = []
        for ds in dates_in_month[:3]:

            def _mk_remove(d=ds, r=row, m=month):
                def _remove(_e):
                    if not self._is_entry_owned(r):
                        self._snack(self.t("media.not_owner", "Ви не є власником цього запису"), "#B45309")
                        return
                    r["post_dates"] = [x for x in (r.get("post_dates") or []) if x != d]
                    self._save_row_async(r, reload_on_error=False)
                    self._fast_refresh_month_cell(r, m)

                return _remove

            _is_close_gap_chip = (
                (not is_past)
                and close_gap
                and latest_iso
                and str(ds)[:10] == latest_iso
                and latest_iso >= date.today().isoformat()
            )
            _chip_bg, _chip_fg = _date_chip_colors(ds, close_gap_today=_is_close_gap_chip)
            chips.append(
                ft.Container(
                    bgcolor=_chip_bg,
                    border=ft.border.all(1, _BORDER),
                    border_radius=6,
                    padding=ft.padding.only(left=6, right=2, top=3, bottom=3),
                    content=ft.Row(
                        [
                            ft.Text(str(ds), size=10, color=_chip_fg),
                            ft.IconButton(
                                icon=ft.Icons.CLOSE,
                                icon_size=12,
                                icon_color=self._th_text,
                                tooltip="Видалити дату",
                                visible=self._is_entry_owned(row),
                                on_click=_mk_remove(),
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

        if (not is_inactive) and len(dates_in_month) < 3:

            def _add_date(_e, r=row, m=month):
                if not self._is_entry_owned(r):
                    self._snack(self.t("media.not_owner", "Ви не є власником цього запису"), "#B45309")
                    return
                pg = self._pg()
                if not pg:
                    return

                def _picked(ev):
                    ds = self._picker_iso_date(ev)
                    if not ds:
                        return
                    existing = list(r.get("post_dates") or [])
                    if ds not in existing:
                        existing.append(ds)
                        existing.sort(reverse=True)
                        r["post_dates"] = existing
                    with contextlib.suppress(Exception):
                        pg.close_dialog()
                    self._save_row_async(r, reload_on_error=False)
                    self._fast_refresh_month_cell(r, m)

                def _picker_dismiss_month(_=None):
                    with contextlib.suppress(Exception):
                        pg.close_dialog()

                default_dt = datetime.combine(m, datetime.min.time())
                dp = ft.DatePicker(
                    value=default_dt,
                    first_date=date(2020, 1, 1),
                    last_date=date(2100, 12, 31),
                    entry_mode=ft.DatePickerEntryMode.CALENDAR_ONLY,
                    date_picker_mode=ft.DatePickerMode.DAY,
                    locale=ft.Locale("uk", "UA"),
                    on_change=_picked,
                    on_dismiss=_picker_dismiss_month,
                )
                pg.show_dialog(dp)

            chips.append(
                ft.IconButton(
                    icon=ft.Icons.ADD_CIRCLE_OUTLINE,
                    icon_size=16,
                    tooltip="Додати дату",
                    on_click=_add_date,
                    disabled=(not self._is_entry_owned(row)),
                    style=ft.ButtonStyle(padding=ft.padding.all(0)),
                    width=18,
                    height=18,
                )
            )

        return chips

    def _month_cell(self, row: dict, month: date, row_h: int) -> "ft.Container":
        col_w = int(self._table_layout.get("month_col_w") or _MONTH_COL_W)
        is_past = self._is_archive_month(month)
        is_inactive = bool(row.get("not_relevant", False))

        chips = self._build_month_chips(row, month)
        chips_col = ft.Column(chips, spacing=2, scroll=ft.ScrollMode.ADAPTIVE)

        _key_base = self._row_cell_key_base(row)
        if _key_base:
            self._month_cell_chips_col_refs[f"{_key_base}:{_month_key(month)}"] = (
                chips_col
            )

        bg = _ROW_INACTIVE if is_inactive else ("#F8FAFC" if is_past else "#FFFFFF")
        return ft.Container(
            width=col_w,
            height=row_h,
            border=ft.border.all(1, _BORDER),
            bgcolor=bg,
            padding=ft.padding.all(5),
            content=chips_col,
        )

    def _fast_refresh_month_cell(self, row: dict, month: date) -> None:
        """In-place update of one month cell after a date is added or removed."""
        _key_base = self._row_cell_key_base(row)
        chips_col = self._month_cell_chips_col_refs.get(
            f"{_key_base}:{_month_key(month)}"
        )
        if chips_col is None:
            self._schedule_rebuild()
            return

        chips_col.controls = self._build_month_chips(row, month)
        try:
            chips_col.update()
        except Exception:
            self._schedule_rebuild()
            return

        # Update the «next free date» counter chip in the filter bar.
        _chip_ref = getattr(self, "_next_date_chip_ref", None)
        if _chip_ref is not None:
            _container, _text = _chip_ref
            _ndt = self._next_post_date_str()
            _text.value = _ndt or ""
            _container.visible = bool(_ndt)
            with contextlib.suppress(Exception):
                _container.update()

        # Also update the «days since last post» chip in the left panel.
        days_ref = self._left_row_days_chip_refs.get(_key_base)
        if days_ref is not None:
            dates = row.get("post_dates") or []
            days_val = _days_ago(self._latest_date(dates))
            stale_bucket = self._stale_bucket(days_val, dates)
            if stale_bucket == "red":
                d_bg, d_fg = _R_BG, _R_FG
            elif stale_bucket == "yellow":
                d_bg, d_fg = _Y_BG, _Y_FG
            else:
                d_bg, d_fg = _G_BG, _G_FG
            days_lbl = f"{days_val} д." if days_val is not None else "-"
            days_ref.bgcolor = d_bg
            if isinstance(days_ref.content, ft.Text):
                days_ref.content.value = days_lbl
                days_ref.content.color = d_fg
            try:
                days_ref.update()
            except Exception:
                pass

    def _archive_row_stub(self, row_h: int, row_bg: str) -> "ft.Container":
        archive_w = int(self._table_layout.get("archive_w") or _ARCHIVE_SECTION_W)
        return ft.Container(
            width=archive_w,
            height=row_h,
            border=ft.border.all(1, _BORDER),
            bgcolor=row_bg,
        )

    # ── Archive month management ───────────────────────────────────────────────

    def _open_archive_month_picker(self, _=None):
        pg = self._pg()
        if not pg:
            return
        today_start = _month_start()
        year_now = today_start.year
        month_dd = ft.Dropdown(
            label="\u041c\u0456\u0441\u044f\u0446\u044c",
            width=180,
            border_radius=10,
            options=[
                ft.dropdown.Option(str(m), _month_title(date(2024, m, 1), self.t).split()[0])
                for m in range(1, 13)
            ],
            value=str(max(1, today_start.month - 1)),
        )
        year_dd = ft.Dropdown(
            label="\u0420\u0456\u043a",
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
                pg.pop_dialog()
            except Exception:
                pass

        def _apply(_=None):
            try:
                selected = date(int(year_dd.value), int(month_dd.value), 1)
            except Exception:
                self._snack(
                    self.t("media.err.select_month_year", "\u041e\u0431\u0435\u0440\u0456\u0442\u044c \u043c\u0456\u0441\u044f\u0446\u044c \u0456 \u0440\u0456\u043a"),
                    _Y_FG,
                )
                return
            if selected >= today_start:
                self._snack(
                    "\u0412 \u0430\u0440\u0445\u0456\u0432 \u043c\u043e\u0436\u043d\u0430 \u0432\u0456\u0434\u043a\u0440\u0438\u0442\u0438 \u043b\u0438\u0448\u0435 \u043c\u0438\u043d\u0443\u043b\u0456 \u043c\u0456\u0441\u044f\u0446\u0456",
                    _Y_FG,
                )
                return
            key = _month_key(selected)
            if key not in self._opened_archive_month_keys:
                self._opened_archive_month_keys.append(key)
            self._save_month_view_settings()
            _close()
            self._schedule_rebuild()

        def _archive_help(_):
            from views.help_utils import show_modal_help
            _pg2 = self._pg()
            show_modal_help(_pg2, "\u0412\u0456\u0434\u043a\u0440\u0438\u0442\u0438 \u043c\u0456\u0441\u044f\u0446\u044c \u0437 \u0430\u0440\u0445\u0456\u0432\u0443", [
                self.t("media.archive_help.step1", "1. \u041e\u0431\u0435\u0440\u0456\u0442\u044c \u043c\u0456\u0441\u044f\u0446\u044c \u0456 \u0440\u0456\u043a."),
                self.t("media.archive_help.step2", "2. \u041d\u0430\u0442\u0438\u0441\u043d\u0456\u0442\u044c \u00ab\u0412\u0456\u0434\u043a\u0440\u0438\u0442\u0438\u00bb \u2014 \u043c\u0456\u0441\u044f\u0446\u044c \u0437'\u044f\u0432\u0438\u0442\u044c\u0441\u044f \u0432 \u0442\u0430\u0431\u043b\u0438\u0446\u0456."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                [
                    ft.Text("\u0412\u0456\u0434\u043a\u0440\u0438\u0442\u0438 \u043c\u0456\u0441\u044f\u0446\u044c \u0437 \u0430\u0440\u0445\u0456\u0432\u0443", weight="bold", expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "\u0414\u043e\u0432\u0456\u0434\u043a\u0430"), on_click=_archive_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Container(
                width=360,
                content=ft.Row([month_dd, year_dd], spacing=10),
            ),
            actions=[
                ft.TextButton("\u0421\u043a\u0430\u0441\u0443\u0432\u0430\u0442\u0438", on_click=_close),
                ft.FilledButton("\u0412\u0456\u0434\u043a\u0440\u0438\u0442\u0438", on_click=_apply),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        pg.show_dialog(dlg)

    def _remove_archive_month(self, month_key_str: str):
        self._opened_archive_month_keys = [
            k for k in self._opened_archive_month_keys if k != month_key_str
        ]
        self._save_month_view_settings()
        self._schedule_rebuild()

    def _row_widget(self, row: dict, idx: int):
        sel_key = self._sel_key(row)
        is_sel = sel_key in self._selected

        def _on_check(e, key=sel_key):
            if e.control.value:
                self._selected.add(key)
            else:
                self._selected.discard(key)
            self._schedule_rebuild()

        pnames = row.get("product_names") or ["-"]
        name_text = " + ".join(pnames) if len(pnames) > 1 else (pnames[0] or "-")
        name_w = int(self._table_layout.get("name_w") or _NAME_W)
        comment_w = int(self._table_layout.get("comment_w") or _COMMENT_W)
        days_w = int(self._table_layout.get("days_w") or _DAYS_W)
        dates_w = int(self._table_layout.get("dates_w") or _DATES_W)
        row_h = int(self._table_layout.get("row_h") or _ROW_H)
        txt_size = int(max(6, min(32, int(self._table_layout.get("text_size") or 11))))
        h_align = str(self._table_layout.get("text_h_align") or "left")
        v_align = str(self._table_layout.get("text_v_align") or "center")
        allow_wrap = bool(self._table_layout.get("text_wrap", True))
        txt_align = (
            ft.TextAlign.LEFT
            if h_align == "left"
            else (ft.TextAlign.RIGHT if h_align == "right" else ft.TextAlign.CENTER)
        )
        vtxt_align = (
            ft.VerticalAlignment.START
            if v_align == "top"
            else (
                ft.VerticalAlignment.END
                if v_align == "bottom"
                else ft.VerticalAlignment.CENTER
            )
        )

        def _copy(e, nm=name_text):
            pg = self._pg()
            if pg:
                try:
                    pg.set_clipboard(nm)
                    self._snack(self.t("media.copied", "Скопійовано"), "#0F172A")
                except Exception:
                    pass

        name_cell = ft.Row(
            [
                ft.TextField(
                    value=name_text,
                    read_only=True,
                    border=ft.InputBorder.NONE,
                    content_padding=ft.padding.symmetric(horizontal=4, vertical=2),
                    text_size=txt_size,
                    color=self._th_text,
                    width=max(80, name_w - 50),
                    multiline=allow_wrap,
                    text_align=txt_align,
                    text_vertical_align=vtxt_align,
                    tooltip=ft.Tooltip(message=name_text, wait_duration=0) if name_text else None,
                    text_style=ft.TextStyle(
                        weight=(
                            ft.FontWeight.W_600
                            if row.get("merged")
                            else ft.FontWeight.W_400
                        )
                    ),
                ),
                ft.IconButton(
                    icon=ft.Icons.COPY_OUTLINED,
                    icon_size=13,
                    tooltip="Копіювати назву",
                    on_click=_copy,
                    style=ft.ButtonStyle(padding=ft.padding.all(2)),
                ),
            ],
            spacing=0,
            expand=True,
        )

        comment_tf = ft.TextField(
            value=row.get("comment") or "",
            hint_text="Коментар...",
            border=ft.InputBorder.OUTLINE,
            content_padding=ft.padding.symmetric(horizontal=6, vertical=6),
            text_size=txt_size,
            color=self._th_text,
            multiline=allow_wrap,
            min_lines=1,
            max_lines=(5 if allow_wrap else 1),
            width=max(80, comment_w - 10),
            text_align=txt_align,
            text_vertical_align=vtxt_align,
            read_only=self.is_read_only,
        )

        def _mk_save_cmt(r=row, tf=comment_tf):
            def _save(e):
                r["comment"] = tf.value or ""
                self._save_row(r)

            return _save

        comment_tf.on_blur = _mk_save_cmt()
        comment_tf.on_submit = _mk_save_cmt()

        dates = row.get("post_dates") or []
        days = _days_ago(self._latest_date(dates))
        stale_bucket = self._stale_bucket(days, dates)
        if stale_bucket == "red":
            d_bg, d_fg = _R_BG, _R_FG
        elif stale_bucket == "yellow":
            d_bg, d_fg = _Y_BG, _Y_FG
        else:
            d_bg, d_fg = _G_BG, _G_FG
        days_lbl = f"{days} д." if days is not None else "-"

        inactive_cb = ft.Checkbox(
            label="Неакт.",
            value=bool(row.get("not_relevant", False)),
        )

        def _toggle_not_relevant(e, r=row):
            r["not_relevant"] = bool(e.control.value)
            self._save_row(r)
            self._schedule_rebuild()

        inactive_cb.on_change = _toggle_not_relevant

        days_chip = ft.Container(
            bgcolor=d_bg,
            border_radius=6,
            padding=ft.padding.symmetric(horizontal=6, vertical=3),
            content=ft.Text(
                days_lbl,
                size=txt_size,
                color=d_fg,
                text_align=txt_align,
                no_wrap=(not allow_wrap),
            ),
        )

        def _add_date(e, r=row):
            pg = self._pg()
            if not pg:
                return
            r["post_dates"].append(datetime.now().strftime("%Y-%m-%d"))
            self._save_row(r)
            self._schedule_rebuild()

        row_controls = [
            ft.Container(
                width=_CB_W,
                padding=ft.padding.only(left=8),
                content=ft.Checkbox(value=is_sel, on_change=_on_check),
            ),
            ft.Container(
                width=_ACT_W,
                padding=ft.padding.symmetric(horizontal=4, vertical=4),
                alignment=ft.Alignment(0, 0),
                content=inactive_cb,
            ),
        ]
        for col_id in self._visible_ordered_media_column_ids():
            if col_id == "name":
                row_controls.append(
                    ft.Container(
                        content=name_cell,
                        width=name_w,
                        padding=ft.padding.symmetric(vertical=4, horizontal=4),
                    )
                )
            elif col_id == "comment":
                row_controls.append(
                    ft.Container(
                        content=comment_tf,
                        width=comment_w,
                        padding=ft.padding.symmetric(vertical=4, horizontal=4),
                    )
                )
            elif col_id == "days":
                row_controls.append(
                    ft.Container(
                        content=days_chip,
                        width=days_w,
                        alignment=ft.Alignment(0, 0),
                        padding=ft.padding.symmetric(vertical=4, horizontal=4),
                    )
                )
            elif col_id == "dates":
                row_controls.append(
                    ft.Container(
                        content=self._date_chips(row),
                        width=dates_w,
                        padding=ft.padding.symmetric(vertical=4, horizontal=4),
                    )
                )

        return ft.Container(
            content=ft.Row(row_controls, spacing=0),
            height=max(row_h, 56),
            bgcolor=_SEL_BG if is_sel else (_ROW_B if idx % 2 else _ROW_A),
        )

    def _sel_key(self, row: dict) -> str:
        eid = row.get("entry_id")
        if eid:
            return f"e:{eid}"
        pids = sorted(row.get("product_ids") or [])
        return f"p:{'|'.join(pids)}"

    def _table_header(self) -> ft.Container:
        name_w = int(self._table_layout.get("name_w") or _NAME_W)
        comment_w = int(self._table_layout.get("comment_w") or _COMMENT_W)
        days_w = int(self._table_layout.get("days_w") or _DAYS_W)
        dates_w = int(self._table_layout.get("dates_w") or _DATES_W)
        hdr_controls = [
            ft.Container(
                width=_CB_W,
                padding=ft.padding.only(left=8),
                content=ft.Checkbox(visible=False),
            ),
            self._hcol(" ", width=_ACT_W),
        ]
        for col_id in self._visible_ordered_media_column_ids():
            if col_id == "name":
                hdr_controls.append(self._hcol(self.t("media.col.product_name", "Назва товару"), width=name_w))
            elif col_id == "comment":
                hdr_controls.append(self._hcol(self.t("media.col.comment", "Коментар"), width=comment_w))
            elif col_id == "days":
                hdr_controls.append(self._hcol(self.t("media.col.days_pub", "Днів з публ."), width=days_w))
            elif col_id == "dates":
                hdr_controls.append(self._hcol(self.t("media.col.pub_dates", "Дати публікації"), width=dates_w))

        return ft.Container(
            bgcolor=_HDR_BG,
            border=ft.border.all(1, _BORDER),
            border_radius=ft.border_radius.only(top_left=8, top_right=8),
            content=ft.Row(hdr_controls, spacing=0),
        )

    def _rebuild(self, reload: bool = False):
        # ── Theme color shadowing ──────────────────────────────────────────────
        from views.themes import get_active_theme as _get_theme
        _th = _get_theme(self.manager_id)
        _ROW_A        = _th["panel"]
        _ROW_B        = _th["alt"]
        _ROW_INACTIVE = _th["err_bg"]
        _HDR_BG       = _th["hdr"]
        _BORDER       = _th["border"]
        _ACCENT       = _th["icon_color"]
        _SEL_BG       = _th["soft"]
        _G_BG, _G_FG  = _th["ok_bg"],   _th["ok"]
        _Y_BG, _Y_FG  = _th["warn_bg"], _th["warn"]
        _R_BG, _R_FG  = _th["err_bg"],  _th["err"]
        _N_BG, _N_FG  = _th["alt"],     _th["muted"]
        _OK_C         = _th["ok"]
        _ERR_C        = _th["err"]
        # ──────────────────────────────────────────────────────────────────────
        if self._header_loading_indicator is None:
            self._header_loading_indicator = create_orders_header_loading_indicator(
                visible=False
            )
        if reload or not self._initial_loaded:
            self._load()
            self._load_table_layout_state()
            self._load_visible_columns_state()

        rows = self._build_rows()
        visible_months = self._visible_month_anchors()

        # ── Build left and right rows ──────────────────────────────────────────
        left_rows: list = []
        right_rows: list = []
        for idx, row in enumerate(rows):
            row_h = self._row_height_monthly(row)
            left_rows.append(self._left_row_monthly(row, idx, row_h))
            if row.get("item_type") == "inactive_header":
                right_rows.append(
                    ft.Container(
                        height=row_h,
                        bgcolor="#F5F5F5",
                        border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
                    )
                )
                continue
            col_w = int(self._table_layout.get("month_col_w") or _MONTH_COL_W)
            row_bg = (
                _ROW_INACTIVE
                if bool(row.get("not_relevant", False))
                else (_ROW_B if idx % 2 else _ROW_A)
            )
            right_rows.append(
                ft.Container(
                    height=row_h,
                    bgcolor=row_bg,
                    content=ft.Row(
                        [self._archive_row_stub(row_h, row_bg)]
                        + [self._month_cell(row, m, row_h) for m in visible_months],
                        spacing=0,
                    ),
                )
            )

        # ── Calculate widths ───────────────────────────────────────────────────
        left_w = _CB_W + _ACT_W
        for col_id in self._visible_ordered_media_column_ids():
            if col_id == "name":
                left_w += int(self._table_layout.get("name_w") or _NAME_W)
            elif col_id == "comment":
                left_w += int(self._table_layout.get("comment_w") or _COMMENT_W)
            elif col_id == "days":
                left_w += int(self._table_layout.get("days_w") or _DAYS_W)
            elif col_id == "dates":
                left_w += int(self._table_layout.get("dates_w") or _DATES_W)
        archive_w = int(self._table_layout.get("archive_w") or _ARCHIVE_SECTION_W)
        col_w = int(self._table_layout.get("month_col_w") or _MONTH_COL_W)
        right_inner_w = archive_w + len(visible_months) * col_w

        pg = self._pg()
        vp_w = int(getattr(self.content_area, "width", 0) or 0)
        if vp_w <= 0 and pg:
            vp_w = int(getattr(pg, "width", 0) or 0)
            if vp_w <= 0:
                win = getattr(pg, "window", None)
                vp_w = int(getattr(win, "width", 0) or 0) if win else 0
        vp_w = max(300, vp_w if vp_w > 0 else 1200)
        right_host_w = max(200, vp_w - left_w - 28)

        # Store for resume() re-injection.
        self._left_w = left_w
        self._right_inner_w = right_inner_w

        # ── Build pinned header row (left + right header, no v-scroll) ─────────
        left_header_container = ft.Container(
            width=left_w,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=self._table_header_left_monthly(),
        )

        right_header_content = ft.Container(
            width=right_inner_w,
            content=self._table_header_right_monthly(visible_months),
        )
        # Stack approach: shift header via left=-offset (synchronous, zero lag)
        right_header_content.left = -int(self._h_offset or 0)
        right_header_content.top = 0
        self._right_header_stack_child = right_header_content
        self._right_header_scroll = None  # replaced by stack approach

        right_header_host = ft.Container(
            width=right_host_w,
            height=56,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            content=ft.Stack([right_header_content]),
        )

        header_row = ft.Row(
            [left_header_container, right_header_host],
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )

        # ── Build body (left body col + right body col, with v-scroll) ──────────
        _left_rows_col = ft.Column([], spacing=0)
        left_body_col = ft.Container(
            width=left_w,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
            bgcolor=self._th_panel,
            content=_left_rows_col,
        )

        _right_rows_col = ft.Column([], spacing=0)
        right_body_content = ft.Container(
            width=right_inner_w,
            bgcolor=self._th_panel,
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

        right_body_host = ft.Container(
            width=right_host_w,
            content=right_body_scroll_row,
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

        _title_row = ft.Row(
            [
                ft.Icon(ft.Icons.NEWSPAPER, size=24, color=self._th_icon),
                ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Text(self.t("media.title", "Медіа План"), size=20, weight="bold", color=self._th_text),
                                ft.IconButton(
                                    icon=ft.Icons.HELP_OUTLINE,
                                    icon_size=18,
                                    icon_color=self._th_text,
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
                            color=self._th_muted,
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
                    self.t("media.stale_filter_hint", "Фільтр: >14 днів або без публікацій"),
                    size=12,
                    color=self._th_muted,
                    visible=self._is_stale_filter_mode(),
                ),
                ft.Row(expand=True),
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
                ft.ElevatedButton(
                    "Об'єднати обрані",
                    icon=ft.Icons.MERGE_TYPE,
                    on_click=self._merge_selected,
                    visible=not self.is_read_only and len(self._selected) > 1,
                ),
                ft.OutlinedButton(
                    "Роз'єднати",
                    icon=ft.Icons.CALL_SPLIT,
                    on_click=self._split_selected,
                    visible=not self.is_read_only and len(self._selected) >= 1,
                    style=ft.ButtonStyle(
                        text_style=ft.TextStyle(size=11),
                        padding=ft.padding.symmetric(horizontal=10, vertical=2),
                    ),
                ),
                self._build_workspace_sync_indicator(),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
        )

        _filter_bar = self._build_filter_bar()

        # Push title, filter bar AND table header_row into the sticky slot.
        pg_s = self._pg()
        _sticky = getattr(pg_s, "_view_sticky_header", None) if pg_s else None
        if getattr(pg_s, "_active_view", "") != "show_media_plan":
            _sticky = None

        _title_ctr = ft.Container(
            content=_title_row,
            padding=ft.padding.only(bottom=4),
            border=ft.border.only(bottom=ft.BorderSide(1, "#E2E8F0")),
        )
        _filter_ctr = ft.Container(
            content=_filter_bar, padding=ft.padding.only(bottom=4)
        )

        if _sticky is not None:
            outer_col = ft.Column([body_col], spacing=0, expand=True)
        else:
            outer_col = ft.Column([header_row, body_col], spacing=0, expand=True)

        self._sticky_title_ref = _title_ctr
        self._sticky_filter_ref = _filter_ctr
        self._sticky_table_header_ref = header_row

        # ── Horizontal-scroll overlay buttons ──────────────────────────────────
        _mp_needs = right_inner_w > right_host_w
        _mp_step = max(300, right_inner_w // 3)

        def _do_hscroll_mp(delta):
            async def _run():
                with contextlib.suppress(Exception):
                    if self._right_body_scroll is not None:
                        await self._right_body_scroll.scroll_to(delta=delta, duration=200)
            with contextlib.suppress(Exception):
                _pg2 = self._pg()
                if _pg2:
                    _pg2.run_task(_run)

        _mp_sh = ft.BoxShadow(spread_radius=1, blur_radius=8,
            color=ft.Colors.with_opacity(0.28, ft.Colors.BLACK), offset=ft.Offset(0, 2))
        _mp_bs = ft.ButtonStyle(padding=ft.padding.all(4))
        _mp_left = ft.Container(
            content=ft.IconButton(ft.Icons.CHEVRON_LEFT, icon_size=20,
                icon_color=self._th_icon,
                on_click=lambda _: _do_hscroll_mp(-_mp_step), style=_mp_bs),
            left=4, top=200,
            bgcolor=self._th_panel, border_radius=20, width=36, height=36,
            shadow=_mp_sh, opacity=0.9, visible=_mp_needs,
        )
        _mp_right = ft.Container(
            content=ft.IconButton(ft.Icons.CHEVRON_RIGHT, icon_size=20,
                icon_color=self._th_icon,
                on_click=lambda _: _do_hscroll_mp(_mp_step), style=_mp_bs),
            right=6, top=200,
            bgcolor=self._th_panel, border_radius=20, width=36, height=36,
            shadow=_mp_sh, opacity=0.9, visible=_mp_needs,
        )

        main_col = ft.Column(
            [ft.Divider(height=1, color=_BORDER), outer_col],
            spacing=0,
            tight=True,
        )

        # Bump generation so any running _async_fill_rows from a prior _rebuild stops.
        self._fill_generation = getattr(self, "_fill_generation", 0) + 1
        self._pending_left_rows = left_rows
        self._pending_right_rows = right_rows
        self._pending_left_rows_col = _left_rows_col
        self._pending_right_rows_col = _right_rows_col
        self._pending_right_body_content = right_body_content
        self._pending_rows_data = list(rows)

        # Hide spinner BEFORE sticky update so Flutter sees it hidden from the start.
        if self._header_loading_indicator is not None:
            self._header_loading_indicator.visible = False

        # Update sticky immediately (fast — spinner already hidden).
        if _sticky is not None:
            _sticky.controls[:] = [_title_ctr, _filter_ctr, header_row]
            with contextlib.suppress(Exception):
                _sticky.update()

        # Store button refs for lightweight resize updates.
        self._mp_left_btn = _mp_left
        self._mp_right_btn = _mp_right

        # Lightweight resize handler — only toggles button visibility, no full rebuild.
        def _on_mp_resized(_e=None):
            if str(getattr(pg, "_active_view", "") or "") != "show_media_plan":
                return
            try:
                vp = int(getattr(self.content_area, "width", 0) or 0)
                if vp <= 0:
                    vp = int(getattr(pg, "width", 0) or 0)
                if vp <= 0:
                    return
                _l_w = getattr(self, "_left_w", left_w)
                _r_w = getattr(self, "_right_inner_w", right_inner_w)
                new_needs = _r_w > max(200, vp - _l_w - 28)
                btn_l = getattr(self, "_mp_left_btn", None)
                btn_r = getattr(self, "_mp_right_btn", None)
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
            if hasattr(pg, "on_resized"):
                pg.on_resized = _on_mp_resized
            elif hasattr(pg, "on_resize"):
                pg.on_resize = _on_mp_resized
        except Exception:
            pass

        # Send skeleton with empty body columns to Flutter — fast single update.
        self.content_area.controls.clear()
        with contextlib.suppress(Exception):
            self.content_area.scroll = ft.ScrollMode.ADAPTIVE
            self.content_area.on_scroll = self._on_content_area_scroll
        self.content_area.controls.append(main_col)
        self.content_area.update()

        # Move h-scroll buttons to page overlay so they stay visible during v-scroll.
        for _b in getattr(pg, '_catman_hscroll_btns', []):
            with contextlib.suppress(Exception):
                pg.overlay.remove(_b)
        pg._catman_hscroll_btns = []
        if _mp_needs:
            _btn_top = int((getattr(pg, 'height', None) or 900) * 0.5)
            _mp_left.top = _btn_top
            _mp_right.top = _btn_top
            pg.overlay.append(_mp_left)
            pg.overlay.append(_mp_right)
            pg._catman_hscroll_btns = [_mp_left, _mp_right]
            with contextlib.suppress(Exception):
                pg.update()

        self._refresh_workspace_sync_indicator_state()
        self._media_plan_ui_ready = True

        # Schedule progressive row filling on the event loop.
        _pg_fill = self._pg()
        if _pg_fill and hasattr(_pg_fill, "run_task"):
            with contextlib.suppress(Exception):
                _pg_fill.run_task(self._async_fill_rows)
        else:
            # No page — fill synchronously as fallback.
            _left_rows_col.controls.extend(left_rows)
            _right_rows_col.controls.extend(right_rows)
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

        if left_col is None or right_col is None:
            return

        _BATCH = 8
        _total = max(len(left_rows), len(right_rows))
        if _total == 0:
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

        self._restore_scroll_after_rebuild()

    async def resume(self) -> None:
        """Called when switching back to this cached tab.
        Silently validates DB pulse; only shows spinner/reloads if data changed."""
        import asyncio as _asyncio

        if not self._media_plan_ui_ready:
            return

        _saved_controls = list(getattr(self.content_area, "controls", None) or [])

        # Silently check DB pulse — no loading stub until we know data changed.
        _data_changed = True
        if self._entries_cache_pulse > 0 and _saved_controls:
            try:
                _new_pulse = await _asyncio.to_thread(self._entries_scope_pulse)
                _data_changed = _new_pulse != self._entries_cache_pulse
            except Exception:
                _data_changed = True

        # After await: check if user already switched to a different tab.
        _pg_active = self._pg()
        if _pg_active and getattr(_pg_active, "_active_view", "") != "show_media_plan":
            return

        if not _data_changed:
            # Ensure sticky header shows the correct Media Plan header.
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
                    _pg_c = self._pg()
                    if _pg_c and getattr(_pg_c, "_active_view", "") != "show_media_plan":
                        return
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
