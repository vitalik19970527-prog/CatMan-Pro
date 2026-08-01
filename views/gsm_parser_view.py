"""
GSM Parser Results View
- Merged GSM Arena + EK table with color-highlighted sizes/diagonals
- Background loading, cache, trigger-based reload
- Device card modal with full GSM + EK specs, adaptive price/shops charts
- SA: launch parser, clear data
- Filter presets per user
"""

import asyncio
import contextlib
import json
import logging
import os
import queue
import re
import threading
import time
import unittest  # noqa: F401 — matplotlib (charts below) imports this
# transitively at runtime in a way PyInstaller's static analysis never
# detects, so the packaged exe/apk silently lacked it and every chart with
# real data (not the "insufficient data" placeholder) failed with
# "No module named 'unittest'" — a real static import here is the proven fix
# (verified with an isolated PyInstaller repro); --hidden-import alone did
# NOT force it to bundle.
from collections import Counter
from datetime import date, datetime, timedelta

import flet as ft

import i18n
from gsm_parser_runner import fetch_and_apply_manual_ek_url
from logger import logger as _catlog

logger = logging.getLogger("gsm_parser_view")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setLevel(logging.ERROR)
    _h.setFormatter(logging.Formatter("%(asctime)s [GSM] %(levelname)s %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.ERROR)
    logger.propagate = False
# `logger` above has propagate=False and an ERROR-only console handler —
# nothing it logs ever reaches logs/catman.log. _catlog is the shared,
# file-backed logger from logger.py, used specifically for the reveal
# flow's error path below so a failure there is actually diagnosable
# after the fact instead of only visible (if at all) in a live console.


def _warm_up_matplotlib() -> None:
    """matplotlib's FIRST import (see _build_adaptive_charts) can itself
    take a full second or more — cheap on any later call, but on whatever
    thread pays it first. Kicking this off once, here, at module import
    time (this view module is imported well before a user could possibly
    click a chart) means that cost is already paid by the time anyone
    opens a chart, on either a background thread or the main one."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot  # noqa: F401
    except Exception:
        pass


threading.Thread(target=_warm_up_matplotlib, daemon=True).start()

# ---------------------------------------------------------------------------
# Theme colour shims
# ---------------------------------------------------------------------------
_SOFT = "#DCE5F8"
_ALT = "#F7FAFF"
_OK_DK = "#0F7B53"
_OK_BG = "#D9F2E6"
_TEXT = "#0F172A"
_ACCENT = "#1D4ED8"
_MUTED = "#475569"
_BORDER = "#CBD5E1"
_PANEL = "#FFFFFF"

# ── Column specs (key, width, label, sortable) ─────────────────────────────
# Single source of truth — used by BOTH header row and data rows.
# Changing a width here automatically updates both.
_GSM_COLS: list[tuple] = [
    ("", 55, "", False),  # photo thumbnail
    ("brand", 70, "Бренд", True),
    ("gsm_name", 120, "Назва", True),
    ("network", 50, "Мережа", False),
    ("type", 82, "Тип", True),
    ("year", 41, "Рік", True),
    ("release", 82, "Реліз", True),
    ("gsm_size", 92, "Розмір", True),
    ("gsm_diag", 65, "Діагональ", True),
]
_EK_COLS: list[tuple] = [
    ("ek_name", 120, "Назва ЄК", True),
    ("ek_net", 50, "Мережа", False),
    ("ek_date", 68, "Дата", True),
    ("ek_size", 92, "Розмір", True),
    ("ek_diag", 65, "Діагональ", True),
    ("ek_status", 78, "Статус", True),
    ("ek_shops", 78, "Магазини", True),
    ("ek_price", 78, "Ціна", True),
]
# Convenience dict: col_key → width
_COL_W: dict[str, int] = {k: w for k, w, *_ in _GSM_COLS + _EK_COLS if k}


def _th_from_view(view) -> dict:
    from views.themes import get_active_theme

    return get_active_theme(str(getattr(view, "manager_id", "") or ""))


# ---------------------------------------------------------------------------
# Highlight palette — many colors/shades for size & diagonal grouping
# ---------------------------------------------------------------------------
_HIGHLIGHT_PALETTE = [
    "#FFF9C4",
    "#F8BBD9",
    "#B3E5FC",
    "#C8E6C9",
    "#E1BEE7",
    "#FFE0B2",
    "#B2DFDB",
    "#FFCCBC",
    "#D1C4E9",
    "#F0F4C3",
    "#B2EBF2",
    "#DCEDC8",
    "#FCE4EC",
    "#E3F2FD",
    "#FFF3E0",
    "#E0F2F1",
    "#EDE7F6",
    "#E8F5E9",
    "#FBE9E7",
    "#E8EAF6",
    "#FFF8E1",
    "#E1F5FE",
    "#F3E5F5",
    "#FAFAFA",
    "#F9FBE7",
]


def _compute_color_maps(items: list[dict]) -> tuple[dict, dict, dict, dict]:
    """Returns (gsm_size_colors, ek_size_colors, gsm_diag_colors, ek_diag_colors).
    Each column is colored independently — no cross-column matching."""
    gsm_sizes, ek_sizes, gsm_diags, ek_diags = [], [], [], []
    for m in items:
        gs = m.get("gsm_specs") or {}
        es = m.get("ek_specs") or {}
        if v := _fmt_dims(gs):
            gsm_sizes.append(v)
        if v := _fmt_dims(es):
            ek_sizes.append(v)
        if v := str(gs.get("display_size") or "").strip():
            gsm_diags.append(v)
        if v := str(es.get("display") or "").strip():
            ek_diags.append(v)

    def _mk_map(vals: list[str]) -> dict:
        counts = Counter(v for v in vals if v)
        cmap: dict[str, str] = {}
        idx = 0
        for val, cnt in counts.most_common():
            if cnt >= 2:
                cmap[val] = _HIGHLIGHT_PALETTE[idx % len(_HIGHLIGHT_PALETTE)]
                idx += 1
        return cmap

    return _mk_map(gsm_sizes), _mk_map(ek_sizes), _mk_map(gsm_diags), _mk_map(ek_diags)


def _fmt_dims(specs: dict) -> str:
    """Format dimensions as length×width×thickness (integers for l/w, 1-decimal for t)."""
    if not isinstance(specs, dict):
        return ""
    ln = str(specs.get("length") or "").strip()
    w = str(specs.get("width") or "").strip()
    t = str(specs.get("thickness") or "").strip()
    if ln and w and t:
        try:
            lf, wf, tf = float(ln), float(w), float(t)
            return f"{lf:.0f}×{wf:.0f}×{tf:.1f}"
        except Exception:
            pass
    raw = str(specs.get("dimensions") or "").strip()
    m = re.match(r"([\d.]+)\s*[x×]\s*([\d.]+)\s*[x×]\s*([\d.]+)", raw)
    if m:
        try:
            lf, wf, tf = float(m.group(1)), float(m.group(2)), float(m.group(3))
            return f"{lf:.0f}×{wf:.0f}×{tf:.1f}"
        except Exception:
            pass
    return ""


def _dim_value(specs: dict, which: str) -> float | None:
    """One dimension (length/width/thickness) as a float, for sorting by
    just that axis — e.g. to find the narrowest/widest device for box
    selection. Same source/fallback as _fmt_dims: the dedicated key first,
    then parsed out of the combined "dimensions" text."""
    if not isinstance(specs, dict):
        return None
    raw = str(specs.get(which) or "").strip()
    if raw:
        try:
            return float(raw)
        except Exception:
            pass
    dims = str(specs.get("dimensions") or "").strip()
    m = re.match(r"([\d.]+)\s*[x×]\s*([\d.]+)\s*[x×]\s*([\d.]+)", dims)
    if m:
        idx = {"length": 1, "width": 2, "thickness": 3}.get(which)
        if idx:
            try:
                return float(m.group(idx))
            except Exception:
                pass
    return None


def _release_label(gsm_specs: dict) -> str:
    """Returns 'Р: <date>' or 'А: <date>' or empty string."""
    rel = str(gsm_specs.get("released") or "").strip()
    ann = str(gsm_specs.get("announced") or "").strip()
    if rel:
        return f"Р: {rel[:10]}"
    if ann:
        return f"А: {ann[:10]}"
    return ""


def _parse_gsm_ddmmyyyy(s: str) -> tuple[int, int, int] | None:
    """Parse GSM Arena's stored "DD.MM.YYYY" date text (parse_gsm_date in
    GSM_Parser/main.py) into (year, month, day), or None for free text such
    as "Coming soon" that isn't a real date."""
    s = (s or "").strip()
    if len(s) != 10 or s[2] != "." or s[5] != ".":
        return None
    dd, mm, yyyy = s[0:2], s[3:5], s[6:10]
    if dd.isdigit() and mm.isdigit() and yyyy.isdigit():
        return int(yyyy), int(mm), int(dd)
    return None


# Duplicated from gsm_parser_runner._EK_MONTHS_UKR (kept local so the UI's
# sort logic never has to import that module — it pulls in the heavy
# scraping stack and is deliberately only imported lazily elsewhere in this
# file).
_EK_MONTHS_UKR: dict[str, int] = {
    "січень": 1, "лютий": 2, "березень": 3, "квітень": 4, "травень": 5, "червень": 6,
    "липень": 7, "серпень": 8, "вересень": 9, "жовтень": 10, "листопад": 11, "грудень": 12,
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}


def _parse_ek_date_added(raw: str) -> tuple[int, int]:
    """Parse E-Katalog's "Дата додавання" free text (e.g. "12 листопада
    2021" or "Травень 2025") into (year, month), 0 for whichever part is
    missing/unrecognized."""
    year = 0
    month = 0
    for part in str(raw or "").lower().split():
        if part in _EK_MONTHS_UKR:
            month = _EK_MONTHS_UKR[part]
        elif part.isdigit() and len(part) == 4:
            year = int(part)
    return year, month


def _gsm_native_order_key(d: dict):
    """Mirrors get_gsm_merged_results' default ORDER BY (database.py) in
    Python — belt-and-suspenders so the intended order (GSM Arena "Coming
    soon" devices on top, then newest release date, falling back to
    announce date, then standalone E-Katalog-only devices by "Дата
    додавання") holds even if a caller ends up re-sorting an already
    DB-ordered list (e.g. the in-memory device cache, which persists across
    tab visits and isn't guaranteed to always reflect the latest query).

    Ascending sort on the returned tuple reproduces: tier ascending, date
    descending (newest first) — hence the negated year/month/day."""
    gs = d.get("gsm_specs") or {}
    es = d.get("ek_specs") or {}
    if d.get("gsm_name"):
        released = str(gs.get("released") or "").strip()
        if "coming" in released.lower():
            return (0, 0, 0, 0)
        rel = _parse_gsm_ddmmyyyy(released)
        if rel:
            y, m, day = rel
            return (1, -y, -m, -day)
        ann = _parse_gsm_ddmmyyyy(str(gs.get("announced") or "").strip())
        if ann:
            y, m, day = ann
            return (2, -y, -m, -day)
        return (4, 0, 0, 0)
    year, month = _parse_ek_date_added(str(es.get("date_added") or ""))
    return (3, -year, -month, 0)


# ---------------------------------------------------------------------------
# Module-level cache (survives tab switching within a session)
# ---------------------------------------------------------------------------
_CACHE_LOCK = threading.Lock()
_DEVICE_CACHE: list[dict] | None = None
_CACHE_TRIGGER_TS = None  # datetime | None — trigger timestamp when cache was loaded
_CACHE_LOADING = False
# Pre-computed filter option sets — refreshed once per cache load, not per click.
_FILTER_OPTIONS: dict = {"brands": [], "types": [], "years": [], "statuses": []}

# Per-device detail cache: compat, comment, variants, chart history
_DETAIL_CACHE: dict[str, dict] = {}
_DETAIL_LOCK = threading.Lock()
_DETAIL_TS = None  # trigger_ts when _DETAIL_CACHE was fully built

# Per-device gallery photo cache: device_id -> list of LOCAL file paths
# (downloaded from the DB once, written to disk under _photo_cache_dir()).
# Never fetched live from GSM Arena by the app.
_PHOTO_CACHE: dict[str, list] = {}
_PHOTO_LOCK = threading.Lock()

# Guards the on-disk cache file's tmp-write + os.replace sequence.
# _save_cache_to_disk() is fired off from many places (fire-and-forget
# threads after every edit, plus the sync engine) with no coordination —
# two threads racing to write/rename the SAME .tmp path at once causes
# Windows to raise "PermissionError: file is being used by another
# process" on os.replace, since one thread still has it open when the
# other tries to replace it.
_CACHE_SAVE_LOCK = threading.Lock()

# Compare feature — module-level so state survives tab switches
_COMPARE_SELECTED: set[str] = set()
_COMPARE_BTN_CB = None  # callable() | None — refreshes compare button label/visibility
_MAX_COMPARE = 10

# Brand collapse state — persists across tab switches (all users)
_COLLAPSED_BRANDS: set[str] = set()
# Brands we've already assigned a default collapsed-state to — every brand
# defaults to COLLAPSED the first time it's ever seen this session, so a
# fresh load never auto-expands (and auto-builds rows for) the whole catalog
# in the background. Once a brand is in this set, the user's own toggles are
# what control _COLLAPSED_BRANDS from then on.
_KNOWN_BRANDS: set[str] = set()

# Daily-refresh tracking: datetime when cache was last loaded from DB
_CACHE_LOAD_TS: datetime | None = None

# Rendered-chart cache — device_id -> pure-data payload (b64 PNG + hotspot
# coordinates, no live Flet controls). Keyed separately from _DETAIL_CACHE so
# a sync that leaves a device's history untouched never forces its chart to
# re-render matplotlib from scratch; invalidated only when that device's
# history actually changed (see the "sig" check in _build_adaptive_charts).
_CHART_RENDER_LOCK = threading.Lock()
_CHART_RENDER_CACHE: dict[str, dict] = {}


# Which size-group tab was last active in the compat-editing UI, per
# (device_id, accessory_type) — purely a UI-memory convenience so the tab
# doesn't jump back to the first size group every time a row is added/deleted.
_LAST_COMPAT_GROUP_IDX: dict[tuple[str, str], int] = {}


def _get_cache():
    with _CACHE_LOCK:
        return _DEVICE_CACHE, _CACHE_TRIGGER_TS, _CACHE_LOADING


def _compute_filter_options(devices: list[dict]) -> dict:
    brands = sorted({str(d.get("brand") or "") for d in devices if d.get("brand")})
    types = sorted({str(d.get("device_type") or "") for d in devices if d.get("device_type")})
    years = sorted(
        {str((d.get("gsm_specs") or {}).get("year") or "") for d in devices
         if (d.get("gsm_specs") or {}).get("year")},
        reverse=True,
    )
    statuses = sorted(
        {str((d.get("ek_specs") or {}).get("status") or "") for d in devices
         if (d.get("ek_specs") or {}).get("status")}
    )
    return {"brands": brands, "types": types, "years": years, "statuses": statuses}


def _set_cache(devices, trigger_ts):
    global _DEVICE_CACHE, _CACHE_TRIGGER_TS, _CACHE_LOADING, _FILTER_OPTIONS, _CACHE_LOAD_TS
    with _CACHE_LOCK:
        _DEVICE_CACHE = devices
        _CACHE_TRIGGER_TS = trigger_ts
        _CACHE_LOADING = False
        _CACHE_LOAD_TS = datetime.now()
        if devices:
            _FILTER_OPTIONS = _compute_filter_options(devices)


def _set_loading(val: bool):
    global _CACHE_LOADING
    with _CACHE_LOCK:
        _CACHE_LOADING = val


def _get_device_detail(device_id: str) -> dict | None:
    with _DETAIL_LOCK:
        return _DETAIL_CACHE.get(device_id)


def _set_device_detail(device_id: str, data: dict):
    with _DETAIL_LOCK:
        _DETAIL_CACHE[device_id] = data


# ---------------------------------------------------------------------------
# Persistent on-disk cache (survives app restarts) — global, not per-company:
# GSM Arena/E-Katalog data isn't company-scoped. Holds the full device list,
# per-device detail (compat/comments/variants/history) and the sync boundary
# timestamp, so a cold app start paints the table from disk instead of always
# re-pulling everything from the DB.
# ---------------------------------------------------------------------------
_LAST_SYNC_TS: datetime | None = None  # persisted — survives app restarts


def _app_data_dir() -> str:
    base = os.getenv("FLET_APP_STORAGE_DATA") or os.path.join(
        os.getenv("APPDATA") or os.path.expanduser("~"), "CatManPro"
    )
    os.makedirs(base, exist_ok=True)
    return base


def _cache_file_path() -> str:
    return os.path.join(_app_data_dir(), "gsm_parser_cache_v1.json")


_PHOTO_CACHE_DIR_READY = False


def _photo_cache_dir() -> str:
    # Called once per device row when rendering a brand group (up to ~200+
    # times per expand) — os.makedirs(exist_ok=True) is still a real syscall
    # every time even when the dir already exists, and on Windows that's
    # often intercepted by AV real-time scanning. Only the first caller in
    # the process needs to actually create it.
    global _PHOTO_CACHE_DIR_READY
    d = os.path.join(_app_data_dir(), "gsm_photos_cache")
    if not _PHOTO_CACHE_DIR_READY:
        os.makedirs(d, exist_ok=True)
        _PHOTO_CACHE_DIR_READY = True
    return d


def _photo_cache_path(device_id: str) -> str:
    return os.path.join(_photo_cache_dir(), f"{device_id}.jpg")


def _json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def _load_cache_from_disk() -> bool:
    """Populate module globals from the on-disk cache file. Returns True if
    a usable cache was loaded, False if the file is missing/unreadable."""
    global _DEVICE_CACHE, _CACHE_TRIGGER_TS, _FILTER_OPTIONS, _DETAIL_CACHE, _LAST_SYNC_TS
    path = _cache_file_path()
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        devices = payload.get("devices") if isinstance(payload, dict) else None
        if not isinstance(devices, list):
            return False
        detail_by_id = payload.get("detail_by_id") if isinstance(payload, dict) else {}
        last_sync_raw = payload.get("last_sync_ts") if isinstance(payload, dict) else None
        with _CACHE_LOCK:
            _DEVICE_CACHE = devices
            _CACHE_TRIGGER_TS = None
            if devices:
                _FILTER_OPTIONS = _compute_filter_options(devices)
        with _DETAIL_LOCK:
            _DETAIL_CACHE = dict(detail_by_id) if isinstance(detail_by_id, dict) else {}
        _LAST_SYNC_TS = datetime.fromisoformat(last_sync_raw) if last_sync_raw else None
        return True
    except Exception:
        logger.exception("[GSM] failed to load on-disk cache")
        return False


def _save_cache_to_disk() -> None:
    """Persist the current in-memory cache to disk (temp file + rename, same
    pattern as orders_view.py's snapshot cache save)."""
    devices, _, _ = _get_cache()
    with _DETAIL_LOCK:
        detail_by_id = dict(_DETAIL_CACHE)
    payload = {
        "devices": devices or [],
        "detail_by_id": detail_by_id,
        "last_sync_ts": _LAST_SYNC_TS.isoformat() if _LAST_SYNC_TS else None,
        "saved_at": datetime.now().isoformat(),
    }
    path = _cache_file_path()
    tmp_path = path + ".tmp"
    with _CACHE_SAVE_LOCK:
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, default=_json_default)
            os.replace(tmp_path, path)
        except Exception:
            logger.exception("[GSM] failed to save on-disk cache")


def _chart_cache_file_path() -> str:
    return os.path.join(_app_data_dir(), "gsm_chart_cache_v1.json")


def _load_chart_cache_from_disk() -> bool:
    """Populate _CHART_RENDER_CACHE from disk — pre-rendered charts from a
    previous session mean _preload_charts_for_current_page finds most
    devices already cache-hit (the "sig" check inside _build_adaptive_charts
    still catches any that changed since), instead of re-rendering the
    whole visible page's matplotlib charts from scratch every app launch."""
    global _CHART_RENDER_CACHE
    path = _chart_cache_file_path()
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            return False
        with _CHART_RENDER_LOCK:
            _CHART_RENDER_CACHE = payload
        return True
    except Exception:
        logger.exception("[GSM] failed to load on-disk chart cache")
        return False


def _save_chart_cache_to_disk() -> None:
    """Persist _CHART_RENDER_CACHE (device_id -> b64 PNG + hotspot data) to
    its own file, separate from gsm_parser_cache_v1.json — this dict is
    made of base64 PNG strings for potentially thousands of devices, so
    keeping it out of the devices/detail payload keeps THAT file (loaded
    on every cold start) small and fast to parse regardless of how many
    charts have ever been pre-rendered."""
    with _CHART_RENDER_LOCK:
        payload = dict(_CHART_RENDER_CACHE)
    path = _chart_cache_file_path()
    tmp_path = path + ".tmp"
    with _CACHE_SAVE_LOCK:
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, default=_json_default)
            os.replace(tmp_path, path)
        except Exception:
            logger.exception("[GSM] failed to save on-disk chart cache")


def _last_9am_boundary(now: datetime | None = None) -> datetime:
    """The most recent 8:00 — today's if we're past it, otherwise yesterday's."""
    now = now or datetime.now()
    today_9 = now.replace(hour=8, minute=0, second=0, microsecond=0)
    return today_9 if now >= today_9 else today_9 - timedelta(days=1)


def _needs_daily_sync(last_sync_ts: datetime | None) -> bool:
    """True when the cache hasn't been synced since the most recent 8:00
    boundary — correct for BOTH 'it's after 8:00 and we haven't refreshed
    today' and 'it's before 8:00 and we never refreshed since yesterday's
    8:00'. (The old _needs_daily_refresh unconditionally returned False
    before 8:00, which silently skipped the missed-yesterday case.)"""
    if last_sync_ts is None:
        return True
    return last_sync_ts < _last_9am_boundary()


# ---------------------------------------------------------------------------
# Module-level parser run-state (persists across tab switches in same session)
# ---------------------------------------------------------------------------
_PARSER_LOCK = threading.Lock()
_PARSER_RUNNING = False
_PARSER_PAUSED = False
_PARSER_STATUS = ""
_PARSER_STOP_EVENT: threading.Event | None = None
_PARSER_PAUSE_EVENT: threading.Event | None = None
_PARSER_PROGRESS_Q: queue.Queue | None = None
_PARSER_DONE_FLAG: dict = {"done": True}
_PARSER_STATUS_CB = None  # callable(str) | None — current view's status updater
_PARSER_BUTTONS_CB = (
    None  # callable(bool, bool) | None — current view's button state updater
)
_PARSER_REFRESH_TABLE_CB = None  # callable() | None — current view's table refresh
_MONITOR_TASKS_ACTIVE = False  # guards against duplicate monitor task spawns

# ---------------------------------------------------------------------------
# Scheduled (delayed) manual launch — lets whoever is allowed to run the
# parser ("Запустити") pick a future time instead of launching immediately
# (e.g. running it locally late at night, delayed past midnight so today's
# changes get recorded under tomorrow's date). Session-only, same lifetime
# as the run-state above — does not survive an app restart, matching how
# _PARSER_RUNNING itself doesn't either.
# ---------------------------------------------------------------------------
_PARSER_SCHEDULE_TARGET_TS: float | None = None  # epoch seconds; None = nothing scheduled
_PARSER_SCHEDULE_MODE = "full"
_PARSER_SCHEDULE_STATUS_CB = None  # callable(str) | None — current view's countdown updater
_PARSER_SCHEDULE_LAUNCH_CB = None  # callable(Any, str) -> awaitable | None — current view's _launch_async


def _parser_is_running() -> bool:
    with _PARSER_LOCK:
        return _PARSER_RUNNING


def _parser_is_paused() -> bool:
    with _PARSER_LOCK:
        return _PARSER_PAUSED


def _parser_get_status() -> str:
    with _PARSER_LOCK:
        return _PARSER_STATUS


def _parser_set_status(s: str) -> None:
    global _PARSER_STATUS
    with _PARSER_LOCK:
        _PARSER_STATUS = s
    cb = _PARSER_STATUS_CB
    if cb:
        with contextlib.suppress(Exception):
            cb(s)


def _parser_notify_buttons(running: bool, paused: bool) -> None:
    cb = _PARSER_BUTTONS_CB
    if cb:
        with contextlib.suppress(Exception):
            cb(running, paused)


def _parser_schedule_target() -> float | None:
    with _PARSER_LOCK:
        return _PARSER_SCHEDULE_TARGET_TS


def _parser_schedule_set_status(s: str) -> None:
    cb = _PARSER_SCHEDULE_STATUS_CB
    if cb:
        with contextlib.suppress(Exception):
            cb(s)


def _schedule_countdown_parts(target_ts: float) -> tuple[str, str]:
    remaining = max(0, int(target_ts - time.time()))
    h, rem = divmod(remaining, 3600)
    m, s = divmod(rem, 60)
    when = datetime.fromtimestamp(target_ts).strftime("%H:%M")
    if h:
        left = f"{h} год {m} хв"
    elif m:
        left = f"{m} хв {s} с"
    else:
        left = f"{s} с"
    return when, left


def _format_schedule_countdown(target_ts: float) -> str:
    when, left = _schedule_countdown_parts(target_ts)
    return i18n.t(
        "gsm.schedule.active_status", "Заплановано на {time} (через {left})"
    ).format(time=when, left=left)


def _parser_cancel_schedule() -> None:
    global _PARSER_SCHEDULE_TARGET_TS
    with _PARSER_LOCK:
        _PARSER_SCHEDULE_TARGET_TS = None
    _parser_schedule_set_status("")


async def _parser_schedule_waiter(target_ts: float, mode: str) -> None:
    """Background waiter for a scheduled launch. Runs on the page's own event
    loop (started via page_ctrl.run_task at schedule time), independent of
    which view/tab happens to be visible when the target time arrives — the
    same "keeps running regardless of tab visibility" model the running
    parser's own progress-monitor tasks already rely on."""
    global _PARSER_SCHEDULE_TARGET_TS
    try:
        while True:
            with _PARSER_LOCK:
                cur = _PARSER_SCHEDULE_TARGET_TS
            if cur != target_ts:
                return  # cancelled, or superseded by a newer schedule
            remaining = cur - time.time()
            if remaining <= 0:
                break
            _parser_schedule_set_status(_format_schedule_countdown(cur))
            await asyncio.sleep(1.0 if remaining < 5 else min(5.0, remaining))
        with _PARSER_LOCK:
            if _PARSER_SCHEDULE_TARGET_TS == target_ts:
                _PARSER_SCHEDULE_TARGET_TS = None
        _parser_schedule_set_status("")
        if _parser_is_running():
            return
        launch_cb = _PARSER_SCHEDULE_LAUNCH_CB
        if launch_cb is not None:
            with contextlib.suppress(Exception):
                await launch_cb(None, mode)
    except asyncio.CancelledError:
        pass


def _parser_start_schedule(page_ctrl, delay_seconds: float, mode: str) -> bool:
    global _PARSER_SCHEDULE_TARGET_TS, _PARSER_SCHEDULE_MODE
    if not page_ctrl or not hasattr(page_ctrl, "run_task"):
        return False
    target_ts = time.time() + max(1.0, float(delay_seconds or 0))
    with _PARSER_LOCK:
        _PARSER_SCHEDULE_TARGET_TS = target_ts
        _PARSER_SCHEDULE_MODE = mode
    _parser_schedule_set_status(_format_schedule_countdown(target_ts))
    with contextlib.suppress(Exception):
        page_ctrl.run_task(_parser_schedule_waiter, target_ts, mode)
    return True


_PARSER_BANNER_REF: ft.Stack | None = (
    None  # ft.Stack in page.overlay (transparent, non-blocking)
)
_PARSER_BANNER_TEXT_REF: ft.Text | None = None  # text inside the stack for updates


def _show_parser_banner(page_ctrl) -> None:
    global _PARSER_BANNER_REF, _PARSER_BANNER_TEXT_REF
    if page_ctrl is None:
        return
    with contextlib.suppress(Exception):
        if _PARSER_BANNER_REF is not None and _PARSER_BANNER_REF in page_ctrl.overlay:
            return  # already shown
        status_text = ft.Text(i18n.t("gsm.running", "Парсинг виконується…"), color=ft.Colors.WHITE, size=11)
        _PARSER_BANNER_TEXT_REF = status_text
        badge = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(
                        ft.Icons.HOURGLASS_TOP_ROUNDED, color=ft.Colors.WHITE, size=14
                    ),
                    status_text,
                ],
                spacing=6,
                tight=True,
            ),
            bgcolor=ft.Colors.ORANGE_700,
            border_radius=20,
            padding=ft.padding.symmetric(horizontal=14, vertical=8),
            right=20,
            bottom=70,
            shadow=ft.BoxShadow(
                spread_radius=1,
                blur_radius=8,
                color=ft.Colors.BLACK26,
                offset=ft.Offset(0, 2),
            ),
        )
        # In Flutter's Overlay, each entry is already full-screen — no expand needed.
        # Without expand=True Flet does not add an Expanded wrapper that creates a
        # full-screen hit-area and absorbs pointer/scroll events on other pages.
        _PARSER_BANNER_REF = ft.Stack([badge])
        page_ctrl.overlay.append(_PARSER_BANNER_REF)
        page_ctrl.update()


def _hide_parser_banner(page_ctrl) -> None:
    global _PARSER_BANNER_REF, _PARSER_BANNER_TEXT_REF
    if page_ctrl is None:
        return
    with contextlib.suppress(Exception):
        if _PARSER_BANNER_REF is not None and _PARSER_BANNER_REF in page_ctrl.overlay:
            page_ctrl.overlay.remove(_PARSER_BANNER_REF)
            page_ctrl.update()
    _PARSER_BANNER_REF = None
    _PARSER_BANNER_TEXT_REF = None


def _update_parser_banner(page_ctrl, msg: str) -> None:
    if _PARSER_BANNER_TEXT_REF is None:
        return
    with contextlib.suppress(Exception):
        _PARSER_BANNER_TEXT_REF.value = msg or i18n.t("gsm.running", "Парсинг виконується…")
        _PARSER_BANNER_TEXT_REF.update()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_STATUS_LABELS = ["Новинка", "Продаж", "Застарів", "Знято з виробництва"]


def _safe_float(v) -> float | None:
    try:
        return float(
            str(v or "").replace(" ", "").replace(",", ".").replace("грн", "").strip()
        )
    except Exception:
        return None


def _fmt_price(v) -> str:
    f = _safe_float(v)
    if f is None:
        return "—"
    return f"{f:,.0f} ₴".replace(",", " ")


# ---------------------------------------------------------------------------
# Standalone sidebar device quick-card (no GsmParserView instance needed)
_sidebar_card_dialog: dict = {}  # page_id → open AlertDialog reference


def open_sidebar_device_card(db, page, device_id: str, t_fn=None) -> None:
    """Open a compact device info card from the sidebar widget."""
    _t = t_fn or (lambda k, d="": d)
    device = db.get_gsm_parser_device_by_id(device_id)
    if not device:
        return
    specs = device.get("specs") or {}
    if not isinstance(specs, dict):
        specs = {}
    is_ek = str(device.get("source") or "").strip().lower().startswith("e-")

    # Close any previously open sidebar card
    pid = id(page)
    prev = _sidebar_card_dialog.get(pid)
    if prev is not None:
        with contextlib.suppress(Exception):
            page.pop_dialog()
        _sidebar_card_dialog.pop(pid, None)

    dlg = ft.AlertDialog(modal=False)

    def _close(_=None):
        with contextlib.suppress(Exception):
            page.pop_dialog()
        _sidebar_card_dialog.pop(pid, None)

    def _srow(label: str, value, unit: str = ""):
        v = str(value or "").strip()
        if not v or v == "—":
            return None
        return ft.Row(
            [
                ft.Text(label + ":", size=10, color="#64748B", width=80),
                ft.Text(v + unit, size=10, weight=ft.FontWeight.W_500, expand=True),
            ],
            spacing=4,
            tight=True,
        )

    if is_ek:
        raw_specs = [
            _srow(_t("gsm.spec.shops", "Магазини"), specs.get("shops_count")),
            _srow(_t("gsm.spec.price_min", "Ціна від"), specs.get("price_min"), " грн"),
            _srow(_t("gsm.spec.price_max", "Ціна до"), specs.get("price_max"), " грн"),
            _srow(_t("gsm.spec.status", "Статус"), specs.get("status")),
            _srow(_t("gsm.spec.display", "Діагональ"), specs.get("display_size"), "'"),
            _srow(_t("gsm.spec.battery", "Батарея"), specs.get("battery")),
        ]
    else:
        raw_specs = [
            _srow(_t("gsm.spec.announced", "Анонс"), specs.get("announced")),
            _srow(_t("gsm.spec.released", "Реліз"), specs.get("released")),
            _srow(_t("gsm.spec.display", "Діагональ"), specs.get("display_size"), "'"),
            _srow(_t("gsm.spec.battery", "Батарея"), specs.get("battery")),
            _srow(_t("gsm.spec.chipset", "Процесор"), specs.get("chipset") or specs.get("processor")),
            _srow(_t("gsm.spec.memory", "Пам'ять"), specs.get("memory") or specs.get("ram")),
        ]
    spec_rows = [r for r in raw_specs if r is not None]

    source_name = "E-Katalog" if is_ek else "GSM Arena"
    source_url = str(device.get("source_url") or "").strip()
    img_url = str(device.get("image_url") or "").strip()

    img_widget = (
        ft.Image(src=img_url, width=72, height=96, fit=ft.BoxFit.CONTAIN, border_radius=6)
        if img_url
        else ft.Container(
            width=72,
            height=96,
            bgcolor="#F1F5F9",
            border_radius=6,
            content=ft.Icon(ft.Icons.PHONE_ANDROID, size=32, color="#94A3B8"),
            alignment=ft.Alignment(0, 0),
        )
    )

    dlg.content = ft.Container(
        width=370,
        padding=ft.padding.all(4),
        content=ft.Column(
            [
                ft.Row(
                    [
                        img_widget,
                        ft.Column(
                            [
                                ft.Text(
                                    str(device.get("model_name") or "—"),
                                    weight=ft.FontWeight.W_700,
                                    size=13,
                                    max_lines=2,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                                ft.Text(
                                    f"{device.get('brand', '')}  ·  {device.get('device_type', '')}",
                                    size=10,
                                    color="#64748B",
                                ),
                                ft.Text(source_name, size=9, color="#94A3B8"),
                                ft.Divider(height=6, color="transparent"),
                                *spec_rows,
                            ],
                            spacing=3,
                            expand=True,
                            tight=True,
                        ),
                    ],
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                *(
                    [
                        ft.TextButton(
                            _t("gsm.sidebar.open_source", f"Відкрити на {source_name}"),
                            icon=ft.Icons.OPEN_IN_NEW,
                            url=source_url,
                            style=ft.ButtonStyle(
                                padding=ft.padding.only(top=4),
                                color="#1D4ED8",
                            ),
                        )
                    ]
                    if source_url
                    else []
                ),
            ],
            spacing=8,
            tight=True,
        ),
    )
    dlg.actions = [
        ft.TextButton(_t("common.close", "Закрити"), on_click=_close)
    ]
    dlg.actions_alignment = ft.MainAxisAlignment.END
    _sidebar_card_dialog[pid] = dlg
    with contextlib.suppress(Exception):
        page.open(dlg)


# ---------------------------------------------------------------------------
class GsmParserView:

    def __init__(
        self,
        content_area,
        user_data,
        db,
        active_company_id=None,
        is_superadmin: bool = False,
    ):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.active_company_id = active_company_id or self.user_data.get("company_id")
        self.is_superadmin = bool(is_superadmin)
        self.manager_id = str(
            self.user_data.get("manager_id") or self.user_data.get("id") or ""
        )
        self.t = i18n.bind(self.manager_id)
        # Translated column header tuples — same structure as module-level _GSM_COLS/_EK_COLS
        _gsm_lbl = {
            "brand": "Бренд", "gsm_name": "Назва", "network": "Мережа",
            "type": "Тип", "year": "Рік", "release": "Реліз",
            "gsm_size": "Розмір", "gsm_diag": "Діагональ",
        }
        _ek_lbl = {
            "ek_name": "Назва ЄК", "ek_net": "Мережа", "ek_date": "Дата",
            "ek_size": "Розмір", "ek_diag": "Діагональ", "ek_status": "Статус",
            "ek_shops": "Магазини", "ek_price": "Ціна",
        }
        self._gsm_cols_t = [
            (k, w, self.t(f"gsm.col.{k}", _gsm_lbl.get(k, lbl)) if k else lbl, s)
            for k, w, lbl, s in _GSM_COLS
        ]
        self._ek_cols_t = [
            (k, w, self.t(f"gsm.col.{k}", _ek_lbl.get(k, lbl)) if k else lbl, s)
            for k, w, lbl, s in _EK_COLS
        ]
        self._status_labels_list = [
            self.t(f"gsm.status.{i}", v)
            for i, v in enumerate(_STATUS_LABELS)
        ]

        _granted_gsm_perms: set = set()
        if not self.is_superadmin and self.manager_id:
            with contextlib.suppress(Exception):
                _granted_gsm_perms = db.get_manager_gsm_permissions(self.manager_id)
        self.can_edit_case = self.is_superadmin or "compat_case" in _granted_gsm_perms
        self.can_edit_glass = self.is_superadmin or "compat_glass" in _granted_gsm_perms
        self.can_edit_xiaomi_sizes = (
            self.is_superadmin or "xiaomi_sizes" in _granted_gsm_perms
        )
        self.can_run_parser = self.is_superadmin or "run_parser" in _granted_gsm_perms

        # Per-instance UI state
        self._search = ""
        self._sort_col = "brand"
        self._sort_asc = True
        self._page_idx = 0
        # Toggled by the "Групування" toolbar button — when True, the table
        # is a single flat list of every device (no brand headers/dividers/
        # collapse state at all) instead of the normal brand-grouped view.
        self._flat_mode: bool = False
        self._flat_last_page_devs: list[dict] | None = None
        self._pending_fill: tuple | None = None
        self._reveal_status_ref: ft.Text | None = None
        self._reveal_rows_done: int = 0
        self._reveal_rows_total: int = 0
        self._reveal_charts_done: int = 0
        self._reveal_charts_total: int = 0
        # Device ids whose chart+photo are already warmed (either by the
        # initial full preload, or by a later catch-up — see
        # _catch_up_new_devices). Lets a filter change that reveals
        # devices outside what was originally preloaded (e.g. a user who
        # only ever filters to one device type) top up just the delta
        # instead of assuming everything is already ready.
        self._warm_device_ids: set[str] = set()
        self._catch_up_running: bool = False
        self._brand_devs_by_name: dict[str, list[dict]] = {}
        self._brand_body_by_name: dict[str, ft.Container] = {}
        self._color_maps: tuple = ({}, {}, {}, {})
        # Serializes ALL brand-row builds (single toggle clicks, rapid
        # multi-toggle, and "expand all") through one queue/drainer so at
        # most one _fill_brand_body_async ever runs at a time — expanding
        # two brands back-to-back used to start two independent fills that
        # ran interleaved on the same thread, which is exactly the "build
        # hundreds of controls" CPU spike the chunking was meant to avoid.
        self._brand_fill_queue: list[tuple[ft.Container, list[dict]]] = []
        self._brand_fill_running: bool = False
        self._filter_open = False
        self._filter_brands: list[str] = []
        self._filter_types: list[str] = []
        self._filter_years: list[str] = []
        self._filter_statuses: list[str] = []
        self._filter_source: str = ""
        self._filter_diag: str = ""
        self._filter_size: str = ""
        self._auto_preset_id: str = ""  # preset to pre-select in dropdown on build

        # References to live controls for in-place update
        self._page_ref = None  # ft.Page, set on first build
        self._table_body_ref: ft.Column | None = None
        self._stats_ref: ft.Text | None = None
        self._pager_ref: ft.Row | None = None
        self._loading_bar_ref: ft.ProgressBar | None = None
        self._loading_status_ref: ft.Text | None = None
        self._status_cb = None  # optional external sink for _update_loading_status text
        self._sync_btn_ref: ft.OutlinedButton | None = None
        self._launch_menu_ref: ft.PopupMenuButton | None = None
        self._active_device_dialog: ft.AlertDialog | None = None
        self._filter_panel_ref: ft.Container | None = None
        self._presets_dd_ref: ft.Dropdown | None = None  # "Мої фільтри" dropdown
        self._log_text_ref: ft.Text | None = None  # "Лог" tab content
        # Set True at the end of build() — gates resume() and tells dashboard's
        # fast path this tab is safe to just toggle visible instead of rebuilding.
        self._gsm_ui_ready: bool = False

    # ------------------------------------------------------------------ #
    #  Cache & loading                                                     #
    # ------------------------------------------------------------------ #
    def _needs_reload(self) -> bool:
        devices, _, loading = _get_cache()
        if loading:
            return False
        if devices is None:
            return True
        if _needs_daily_sync(_LAST_SYNC_TS):
            return True
        # A parse run (by ANY user/admin/superadmin, on any machine) marks
        # gsm_parser_trigger.last_parse_at when it finishes — regardless of
        # who triggered it. If that's newer than what this session last
        # synced, the local cache is stale even though it's the same day
        # and force-refreshes instead of waiting for tomorrow's 8:00
        # boundary or an unrelated manual "Синхронізація" click.
        with contextlib.suppress(Exception):
            trigger = self.db.get_gsm_parse_trigger() or {}
            last_parse_at = trigger.get("last_parse_at")
            if last_parse_at and _LAST_SYNC_TS and last_parse_at > _LAST_SYNC_TS:
                return True
        return False

    def _load_devices_sync(self) -> list[dict]:
        return self.db.get_gsm_merged_results(limit=50000) or []

    def _fetch_device_detail_sync(self, device_id: str, dev: dict) -> dict:
        """Fetch all card-level DB data for one device (runs in thread)."""
        brand = str(dev.get("brand") or "").strip().lower()
        compat = {
            "case": self.db.get_gsm_compatible_devices(device_id, "case") or [],
            "glass": self.db.get_gsm_compatible_devices(device_id, "glass") or [],
        }
        comment = self.db.get_gsm_device_comment(device_id)
        variants = self.db.get_gsm_regional_variants(device_id) if brand == "xiaomi" else []
        hist730 = self.db.get_gsm_daily_history(device_id, days=730) or []
        return {"compat": compat, "comment": comment, "variants": variants, "hist730": hist730}

    def _refresh_cached_detail(self, device_id: str, dev: dict | None = None) -> None:
        """Re-fetch one device's detail from DB and patch the in-memory cache
        immediately after a compat/comment/regional-variant edit — otherwise
        the edit wouldn't be visible until the next full sync. Also schedules
        a disk persist so a cold restart sees the edit too."""
        try:
            if dev is None:
                devices, _, _ = _get_cache()
                dev = next(
                    (d for d in (devices or []) if str(d.get("id") or "") == device_id),
                    None,
                ) or {}
            detail = self._fetch_device_detail_sync(device_id, dev)
            _set_device_detail(device_id, detail)
            # Persisting the on-disk cache means serializing the FULL device
            # list (could be thousands of devices) — too slow to do inline on
            # a click handler. Fire-and-forget in a background thread; the
            # in-memory cache (already updated above) is what the UI reads.
            with contextlib.suppress(Exception):
                threading.Thread(target=_save_cache_to_disk, daemon=True).start()
        except Exception:
            logger.exception("[GSM] _refresh_cached_detail failed for %s", device_id)

    def _update_loading_status(self, text: str, progress: float | None = None):
        """Update status text and optionally the progress bar value."""
        if self._loading_status_ref is not None:
            self._loading_status_ref.value = text
            with contextlib.suppress(Exception):
                self._loading_status_ref.update()
        if self._loading_bar_ref is not None and progress is not None:
            self._loading_bar_ref.value = progress
            with contextlib.suppress(Exception):
                self._loading_bar_ref.update()
        if self._status_cb is not None:
            with contextlib.suppress(Exception):
                self._status_cb(text)
        if self._reveal_status_ref is not None:
            self._reveal_status_ref.value = text
            with contextlib.suppress(Exception):
                self._reveal_status_ref.update()

    def _update_reveal_progress_text(self) -> None:
        """Real progress counter — explicit product requirement over a
        plain spinner. Cheap enough to call after every 50-item batch (row
        or chart). Updates both the reveal panel's status text (initial
        full-preload, table hidden) AND the top loading_status text (used
        during a manual "Синхронізувати" — table stays visible, this bar
        is the only visible sign a resync/rebuild is still in progress),
        since either one could be the visible one depending on context."""
        parts = []
        if self._reveal_rows_total:
            parts.append(f"Рядки: {min(self._reveal_rows_done, self._reveal_rows_total)}/{self._reveal_rows_total}")
        if self._reveal_charts_total:
            parts.append(f"Діаграми: {min(self._reveal_charts_done, self._reveal_charts_total)}/{self._reveal_charts_total}")
        if not parts:
            return
        text = "Завантаження… " + ", ".join(parts)
        if self._reveal_status_ref is not None:
            self._reveal_status_ref.value = text
            with contextlib.suppress(Exception):
                self._reveal_status_ref.update()
        if self._loading_status_ref is not None:
            self._loading_status_ref.value = text
            with contextlib.suppress(Exception):
                self._loading_status_ref.update()

    def _cache_busy(self) -> bool:
        """True while a cache sync is in progress. Interactions that depend on
        the cache being complete (brand expand, opening a card) should no-op
        with a snack rather than act on a half-loaded cache — the rule is:
        loading bar visible = not ready yet, loading bar gone = everything is
        instant."""
        _, _, loading = _get_cache()
        return bool(loading)

    def _warn_cache_busy(self) -> None:
        self._show_snack(self.t("gsm.loading_wait", "Зачекайте, дані ще завантажуються…"), ft.Colors.ORANGE)

    def _set_toolbar_busy(self, busy: bool) -> None:
        """Disable the launch/sync controls while a blocking sync is running
        — the page must not be 'active' (interactive) until loading is fully
        done, per the requested behavior."""
        for ref in (self._sync_btn_ref, self._launch_menu_ref):
            if ref is not None:
                ref.disabled = busy
                with contextlib.suppress(Exception):
                    ref.update()

    def _hide_loading_overlay(self):
        """Hide loading bar and status text — only called once a sync is
        FULLY complete, never mid-flight."""
        if self._loading_bar_ref is not None:
            self._loading_bar_ref.visible = False
            with contextlib.suppress(Exception):
                self._loading_bar_ref.update()
        if self._loading_status_ref is not None:
            self._loading_status_ref.visible = False
            with contextlib.suppress(Exception):
                self._loading_status_ref.update()
        self._set_toolbar_busy(False)

    def _sync_cache_from_db(self, force: bool = False) -> list[dict]:
        """The single sync engine — used by cold start, the daily-boundary
        check, the 'Синхронізація' button, and the live-refresh-during-parse
        loop. Only pulls what's different from the cache (new/changed/deleted
        devices + their detail + their main photo), never a full re-pull
        except for the very first sync ever (empty cache).

        Returns the changed device rows so callers that only care about a
        handful of devices (e.g. the live-refresh-during-parse loop) can
        patch just those rows in place instead of rebuilding the whole
        table — a full rebuild resets scroll position and re-collapses
        whatever the user currently has expanded."""
        global _LAST_SYNC_TS, _DETAIL_TS
        devices, _, _ = _get_cache()
        since_ts = None if (force or not devices) else _LAST_SYNC_TS
        is_first_sync = not devices

        if is_first_sync:
            self._update_loading_status(self.t("gsm.loading_devices", "Завантаження пристроїв…"), 0.0)
            changed_rows = self._load_devices_sync()
            total = len(changed_rows)
            self._update_loading_status(f"Завантаження пристроїв… 0/{total}", 0.0)
        else:
            changed_rows = self.db.get_gsm_merged_results_changed_since(since_ts) or []
            total = len(changed_rows)
            self._update_loading_status(
                self.t("gsm.sync_n", "Синхронізація… {n} змінених").format(n=f"0/{total}") if total else self.t("gsm.syncing", "Синхронізація…"),
                0.0,
            )

        changed_ids = [str(r.get("id") or "") for r in changed_rows if r.get("id")]

        if since_ts is None:
            # Full authoritative snapshot (first-ever sync, or an explicit
            # forced resync via "Синхронізація") — changed_rows already
            # reflects the DB's current sort order (see
            # get_gsm_merged_results), so adopt it as-is. Merging into the
            # OLD cached dict here would silently keep every device in
            # whatever position the cache already had, since a dict only
            # updates values for existing keys without moving them — so a
            # forced resync would patch prices/dates but never actually
            # re-sort anything.
            by_id = {str(d.get("id") or ""): d for d in changed_rows if d.get("id")}
        else:
            # True incremental delta (the 10s live-refresh-during-parse tick)
            # — merge into the existing cache order in place so scroll
            # position / expanded brand groups never get disturbed by a
            # routine background tick.
            by_id = {str(d.get("id") or ""): d for d in (devices or []) if d.get("id")}
            for row in changed_rows:
                rid = str(row.get("id") or "")
                if rid:
                    by_id[rid] = row
        # Deletion diff — hard deletes leave no trace except a missing id.
        if not is_first_sync:
            with contextlib.suppress(Exception):
                live_ids = self.db.get_gsm_device_ids()
                if live_ids:
                    by_id = {k: v for k, v in by_id.items() if k in live_ids}
        merged_devices = list(by_id.values())

        # Bulk-refresh detail for changed devices only — comments/variants/compat
        # are now scoped to changed_ids too (previously full-table pulls on
        # every sync, which was most of the "background load" cost since any
        # automated parser run anywhere marks the cache stale for every user).
        with _DETAIL_LOCK:
            detail_by_id = dict(_DETAIL_CACHE)
        if changed_ids:
            all_comments: dict = {}
            all_variants: dict = {}
            hist_delta: dict = {}
            all_compat: dict = {}
            with contextlib.suppress(Exception):
                all_comments = self.db.get_all_gsm_device_comments(changed_ids) or {}
            with contextlib.suppress(Exception):
                all_variants = self.db.get_all_gsm_regional_variants(changed_ids) or {}
            with contextlib.suppress(Exception):
                hist_delta = self.db.get_gsm_daily_history_since(since_ts) or {}
            with contextlib.suppress(Exception):
                # Scoped to changed_ids instead of every device's compat rows —
                # at 5000 devices with only a handful changed, pulling everything
                # every sync was the dominant cost.
                all_compat = self.db.get_all_gsm_compatible_by_device(changed_ids) or {}

            hist_cutoff = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")
            for did in changed_ids:
                prev = detail_by_id.get(did) or {}
                prev_hist = prev.get("hist730") or []
                new_hist = hist_delta.get(did) or []
                if since_ts is None:
                    # since_ts=None means get_gsm_daily_history_since returned
                    # each device's COMPLETE current history (first sync, or
                    # a forced "Синхронізація"/daily reload) — not a delta —
                    # so it must fully REPLACE prev_hist, never union with it.
                    # Unioning here would mean rows deleted from the DB (e.g.
                    # a history cleanup) stay cached forever, since a union
                    # never drops a key just because the new pull lacks it.
                    merged_hist = [
                        h for h in new_hist
                        if str(h.get("record_date") or "")[:10] >= hist_cutoff
                    ]
                elif new_hist:
                    # De-dup by record_date: a device can be "changed since"
                    # and re-synced more than once on the same calendar day
                    # (up to 12 automated runs/day) — without this, each
                    # sync's delta re-appended another copy of that day's row
                    # on top of prev_hist instead of replacing it, so this
                    # list only ever grew, never trimmed, across every sync
                    # ever run — that's what ballooned the on-disk cache to
                    # hundreds of MB. Keep newest per date, then cap at
                    # ~730 days like the name always claimed but never enforced.
                    by_date = {
                        str(h.get("record_date") or "")[:10]: h
                        for h in (*prev_hist, *new_hist)
                    }
                    merged_hist = [
                        by_date[d] for d in sorted(by_date) if d >= hist_cutoff
                    ]
                else:
                    merged_hist = prev_hist
                detail_by_id[did] = {
                    "compat": all_compat.get(did, {"case": [], "glass": []}),
                    "comment": all_comments.get(did),
                    "variants": all_variants.get(did, []),
                    "hist730": merged_hist,
                }

        # Local photo cache — main thumbnail only, for instant table paint.
        if changed_ids:
            with contextlib.suppress(Exception):
                photos = self.db.get_gsm_device_main_photos_bulk(changed_ids)
                for did, photo_bytes in photos.items():
                    try:
                        with open(_photo_cache_path(did), "wb") as f:
                            f.write(photo_bytes)
                    except Exception:
                        pass

        for i in range(total):
            self._update_loading_status(
                self.t("gsm.sync_n", "Синхронізація… {n} змінених").format(n=f"{i+1}/{total}") if total else self.t("gsm.syncing", "Синхронізація…"),
                (i + 1) / total if total else None,
            )

        _set_cache(merged_devices, None)
        with _DETAIL_LOCK:
            _DETAIL_CACHE.clear()
            _DETAIL_CACHE.update(detail_by_id)
            _DETAIL_TS = None
        _LAST_SYNC_TS = datetime.now()
        with contextlib.suppress(Exception):
            _save_cache_to_disk()
        return changed_rows

    async def _background_load(self, page):
        """Cold start: paint instantly from the on-disk cache if present, then
        — only if the daily 8:00 boundary was missed (or 'Синхронізація' was
        forced) — run a blocking diff-sync against the DB. The loading bar
        only disappears once the sync is fully done; the page stays
        non-interactive (toolbar disabled) until then.

        Skips the disk-cache reload entirely once the in-memory cache is
        already warm: this tab is rebuilt from scratch on every visit (it's
        not a cached/persistent view), but `_DEVICE_CACHE` is a module-level
        global that survives across those rebuilds for the life of the app
        process — re-reading and re-parsing a multi-MB JSON file on every
        single tab switch was a major cause of "freezes when switching tabs".
        """
        try:
            logger.info("[GSM] _background_load: started")

            devices, _, loading = _get_cache()
            if loading:
                # Another instance's sync is already running in the
                # background (e.g. the user switched away and back mid-sync)
                # — don't start a second overlapping one.
                return

            if devices is None:
                loaded_from_disk = await asyncio.to_thread(_load_cache_from_disk)
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(_load_chart_cache_from_disk)
                if loaded_from_disk:
                    self._refresh_table(fill_all_brands=True)

            if self._needs_reload():
                _set_loading(True)
                self._set_toolbar_busy(True)
                self._update_loading_status(self.t("gsm.loading_devices", "Завантаження пристроїв…"), 0.0)
                if self._loading_bar_ref is not None:
                    self._loading_bar_ref.visible = True
                    with contextlib.suppress(Exception):
                        self._loading_bar_ref.update()
                if self._loading_status_ref is not None:
                    self._loading_status_ref.visible = True
                    with contextlib.suppress(Exception):
                        self._loading_status_ref.update()
                try:
                    await asyncio.to_thread(self._sync_cache_from_db, False)
                finally:
                    _set_loading(False)
                self._refresh_table(fill_all_brands=True)

            self._hide_loading_overlay()

        except Exception as exc:
            logger.error("[GSM] _background_load FAILED: %s", exc, exc_info=True)
            _set_loading(False)
            self._hide_loading_overlay()

    async def _reveal_sync(self, page):
        """Runs automatically every time the GSM Parser tab opens (see
        _reveal_and_sync). Data updates once a day, from a scheduled
        parse — so a DB round trip on every single tab visit is pure
        overhead the rest of the day; only sync when _needs_reload() says
        the daily boundary was actually missed (or a parse just finished),
        same gating _background_load already uses. Otherwise this just
        builds the table from whatever's already cached in memory/disk —
        no DB call at all."""
        try:
            devices, _, loading = _get_cache()
            if loading:
                # Someone else's sync (e.g. a concurrently-triggered
                # _background_load) is already running — wait for it to
                # finish instead of returning early. Returning early here
                # used to skip the whole pipeline below (row-fill wait,
                # diagrams, photo cache) entirely, revealing the table on
                # whatever partial state that other sync happened to leave
                # behind at that exact moment — the actual cause of
                # "loading disappears fast but the table/charts still hang".
                for _ in range(3000):  # up to ~10 minutes — see _reveal_and_sync's cap
                    await asyncio.sleep(0.2)
                    devices, _, loading = _get_cache()
                    if not loading:
                        break

            if devices is None:
                # True cold start — nothing in memory yet. Load from disk
                # inline (fast) instead of delegating to _background_load,
                # which does its own fire-and-forget _refresh_table call
                # and returns without ever reaching diagrams/photo-cache —
                # that was the actual bug: on the very first load of the
                # day (devices is always None then), this whole pipeline
                # never ran at all.
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(_load_cache_from_disk)
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(_load_chart_cache_from_disk)

            if not _parser_is_running() and self._needs_reload():
                _set_loading(True)
                self._set_toolbar_busy(True)
                try:
                    await asyncio.to_thread(self._sync_cache_from_db, False)
                finally:
                    _set_loading(False)

            # From here down: the SAME unified pipeline runs regardless of
            # which branch above got us here. Build the container/header
            # structure now, but hand the actual
            # row-fill work back to us instead of letting _refresh_table
            # fire-and-forget it via page_ctrl.run_task — awaiting it
            # directly here removes the race that used to let the table
            # reveal (spinner gone) before every brand had actually
            # finished filling (see _refresh_table's defer_fill_to_caller
            # docstring for the full explanation).
            self._reveal_rows_done = 0
            self._reveal_rows_total = 0
            self._reveal_charts_done = 0
            self._reveal_charts_total = 0
            self._refresh_table(fill_all_brands=True, defer_fill_to_caller=True)
            pending = self._pending_fill
            self._pending_fill = None
            if pending:
                _to_fill, color_maps = pending
                self._reveal_rows_total = sum(len(devs) for _, devs in _to_fill)
                self._update_reveal_progress_text()
                await self._fill_expanded_brands_async(_to_fill, color_maps)

            # Diagrams — batched, and cache-aware (_build_adaptive_charts
            # skips anything already rendered whose price history hasn't
            # changed since last time), so this is only slow the very
            # first time it ever runs.
            await self._preload_charts_for_current_page()

            # Local photo cache — bulk-warmed from DB for the WHOLE
            # filtered catalog, not just whatever this sync's changed_ids
            # covered, so no row's first paint ever falls back to fetching
            # from GSM Arena/E-Katalog directly.
            await self._warm_photo_cache_from_db()
            with contextlib.suppress(Exception):
                self._warm_device_ids = {
                    str(d.get("id") or "") for d in self._get_filtered_devices()
                }
            self._hide_loading_overlay()
            _catlog.info(
                "[GSM] _reveal_sync completed: rows=%s/%s charts=%s/%s",
                self._reveal_rows_done, self._reveal_rows_total,
                self._reveal_charts_done, self._reveal_charts_total,
            )
        except Exception as exc:
            logger.error("[GSM] _reveal_sync FAILED: %s", exc, exc_info=True)
            _catlog.exception("[GSM] _reveal_sync FAILED — table may reveal only partially loaded")

    async def _warm_photo_cache_from_db(self, devices: list[dict] | None = None) -> None:
        """Bulk-warms the local disk photo cache (_photo_cache_path) from
        the DB (gsm_parser_device_photos, already 100% populated for every
        device the table can ever render — verified directly against the
        live DB) for the given device set (default: the whole current
        filtered catalog). changed_ids-based warming (see
        _sync_cache_from_db) only covers whatever a diff sync just
        touched, so a device that was never "changed" during this
        process's lifetime could otherwise have no local file yet and hit
        _build_table_row's image_url fallback — a live fetch straight from
        GSM Arena/E-Katalog, which must never happen (photos only ever
        come from this app's own DB)."""
        try:
            page_devs = devices if devices is not None else self._page_items(
                self._get_filtered_devices()
            )
        except Exception:
            logger.exception("[GSM] _warm_photo_cache_from_db: failed to compute device list")
            return
        missing_ids = [
            did for d in page_devs
            if (did := str(d.get("id") or "").strip())
            and not os.path.exists(_photo_cache_path(did))
        ]
        if not missing_ids:
            return
        CHUNK = 50
        for i in range(0, len(missing_ids), CHUNK):
            chunk_ids = missing_ids[i : i + CHUNK]
            try:
                photos = await asyncio.to_thread(
                    self.db.get_gsm_device_main_photos_bulk, chunk_ids
                )
                for did, photo_bytes in photos.items():
                    with contextlib.suppress(Exception):
                        with open(_photo_cache_path(did), "wb") as f:
                            f.write(photo_bytes)
            except Exception:
                logger.exception("[GSM] photo cache warm failed for a chunk")
            await asyncio.sleep(0)

    async def _preload_charts_for_current_page(self, devices: list[dict] | None = None) -> None:
        """Pre-renders every device's chart in the given set (default: the
        whole current filtered catalog) into _CHART_RENDER_CACHE so
        opening ANY device's chart tab afterwards hits the cache (see
        _build_lazy_tab) instead of running matplotlib on click. Called by
        _reveal_and_sync strictly AFTER the brand-row-fill queue has fully
        drained — never concurrently with it, same reasoning as
        _queue_brand_fill: two things building Flet/matplotlib work on
        this thread at once is what freezes the app at scale. Sequential,
        chunked, yielding between chunks so the event loop can still
        service other UI events during this pass."""
        CHUNK = 50  # explicit spec: load/render in blocks of 50
        try:
            page_devs = devices if devices is not None else self._page_items(
                self._get_filtered_devices()
            )
        except Exception:
            logger.exception(
                "[GSM] _preload_charts_for_current_page: failed to compute device list"
            )
            return
        total = len(page_devs)
        self._reveal_charts_total = total
        self._reveal_charts_done = 0
        for i in range(0, total, CHUNK):
            chunk = page_devs[i : i + CHUNK]
            for dev in chunk:
                did = str(dev.get("id") or "").strip()
                if not did:
                    continue
                try:
                    # Return value (a throwaway ft.Control) is discarded —
                    # only the _CHART_RENDER_CACHE side effect matters here.
                    # Cache-hit devices (unchanged history, see the sig
                    # check inside) return near-instantly. Off the UI thread
                    # (still one device at a time, never concurrent - see
                    # the docstring) so the matplotlib render for a
                    # not-yet-cached device can't stall hover/click
                    # responsiveness on anything already on screen while
                    # this whole pass is still working through the rest of
                    # the page in the background.
                    await asyncio.to_thread(self._build_adaptive_charts, did, dev)
                except Exception:
                    logger.exception(
                        "[GSM] chart preload failed for device_id=%s", did
                    )
            self._reveal_charts_done = min(i + CHUNK, total)
            self._update_reveal_progress_text()
            await asyncio.sleep(0)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(_save_chart_cache_to_disk)

    async def _prepare_results_content(self, status_cb=None) -> ft.Control:
        """Results-tab entry point: check whether a sync is needed, run it
        HERE (awaited, feeding live progress text to `status_cb`) if so, and
        only build the table once that's done.

        This is deliberately NOT the _background_load pattern (build the
        table immediately, race a silent background sync against it) — that
        is exactly what produced multi-minute freezes with no visible
        loading indicator: the sync and the subsequent full _refresh_table()
        rebuild could both still be running well after a stale table had
        already been painted, with no on-screen sign anything was happening."""
        self._status_cb = status_cb
        try:
            devices, _, loading = _get_cache()
            if not loading:
                if devices is None:
                    self._update_loading_status(
                        self.t("gsm.loading_devices", "Завантаження пристроїв…")
                    )
                    await asyncio.to_thread(_load_cache_from_disk)

                if not _parser_is_running() and self._needs_reload():
                    _set_loading(True)
                    try:
                        self._update_loading_status(self.t("gsm.syncing", "Синхронізація…"))
                        await asyncio.to_thread(self._sync_cache_from_db, False)
                    finally:
                        _set_loading(False)
            self._update_loading_status(self.t("gsm.preparing_page", "Підготовка сторінки…"))
        except Exception as exc:
            logger.error("[GSM] _prepare_results_content sync FAILED: %s", exc, exc_info=True)
        finally:
            self._status_cb = None

        return self._build_results_content()

    def _get_or_fetch_device_detail(self, device_id: str, dev: dict | None = None) -> dict:
        """Return cached detail or fetch+cache it now (lazy, on first card open)."""
        cached = _get_device_detail(device_id)
        if cached is not None:
            return cached
        detail = self._fetch_device_detail_sync(device_id, dev or {})
        _set_device_detail(device_id, detail)
        return detail

    def _get_filtered_devices(self) -> list[dict]:
        """Filter and sort merged (GSM+EK) device list."""
        devices, _, _ = _get_cache()
        if not devices:
            return []
        result = list(devices)

        if self._search:
            q = self._search.lower()
            result = [
                d
                for d in result
                if q in (d.get("brand") or "").lower()
                or q in (d.get("gsm_name") or "").lower()
            ]
        if self._filter_brands:
            result = [d for d in result if d.get("brand") in self._filter_brands]
        if self._filter_types:
            result = [d for d in result if d.get("device_type") in self._filter_types]
        if self._filter_years:
            result = [
                d
                for d in result
                if str((d.get("gsm_specs") or {}).get("year") or "")
                in self._filter_years
            ]
        if self._filter_statuses:
            result = [
                d
                for d in result
                if str((d.get("ek_specs") or {}).get("status") or "")
                in self._filter_statuses
            ]
        if self._filter_diag:
            fd = self._filter_diag
            result = [
                d
                for d in result
                if str((d.get("gsm_specs") or {}).get("display_size") or "") == fd
                or str((d.get("ek_specs") or {}).get("display") or "") == fd
            ]
        if self._filter_size:
            fs = self._filter_size
            result = [
                d
                for d in result
                if _fmt_dims(d.get("gsm_specs") or {}) == fs
                or _fmt_dims(d.get("ek_specs") or {}) == fs
            ]

        reverse = not self._sort_asc
        col = self._sort_col

        def _secondary_key(col):
            if col == "brand":
                # Native/default order — real date-based, not a plain name
                # sort (see _gsm_native_order_key). Pass 2 below still does
                # the final brand-name groupby; this only controls the
                # order *within* each brand group.
                return _gsm_native_order_key
            if col == "device_type":
                return lambda d: (d.get(col) or "").lower()
            if col in {"gsm_name", "model_name"}:
                return lambda d: (d.get("gsm_name") or "").lower()
            if col == "last_seen_at":
                return lambda d: str(d.get("last_seen_at") or "")
            if col == "year":
                return lambda d: str((d.get("gsm_specs") or {}).get("year") or "")
            if col == "type":
                return lambda d: (d.get("device_type") or "").lower()
            if col == "release":
                def _rel_key(d):
                    gs = d.get("gsm_specs") or {}
                    released = str(gs.get("released") or "").strip()
                    rel = _parse_gsm_ddmmyyyy(released)
                    if rel:
                        return (0, *rel)
                    ann = _parse_gsm_ddmmyyyy(str(gs.get("announced") or "").strip())
                    if ann:
                        return (1, *ann)
                    # Free text ("Coming soon" etc.) or nothing at all —
                    # keeps these together and after every real date.
                    return (2, 0, 0, 0)
                return _rel_key
            if col == "gsm_size":
                return lambda d: _fmt_dims(d.get("gsm_specs") or {}).lower()
            if col in {"gsm_length", "gsm_width", "gsm_thickness"}:
                _which = col[len("gsm_"):]

                def _gsm_dim_key(d, _w=_which):
                    v = _dim_value(d.get("gsm_specs") or {}, _w)
                    return v if v is not None else -1.0

                return _gsm_dim_key
            if col == "gsm_diag":
                def _diag_key(d):
                    try:
                        return float(str((d.get("gsm_specs") or {}).get("display_size") or ""))
                    except Exception:
                        return 0.0
                return _diag_key
            if col == "ek_name":
                return lambda d: (d.get("ek_name") or "").lower()
            if col == "ek_date":
                def _ek_date_key(d):
                    return _parse_ek_date_added(
                        str((d.get("ek_specs") or {}).get("date_added") or "")
                    )
                return _ek_date_key
            if col == "ek_size":
                return lambda d: _fmt_dims(d.get("ek_specs") or {}).lower()
            if col in {"ek_length", "ek_width", "ek_thickness"}:
                _which = col[len("ek_"):]

                def _ek_dim_key(d, _w=_which):
                    v = _dim_value(d.get("ek_specs") or {}, _w)
                    return v if v is not None else -1.0

                return _ek_dim_key
            if col == "ek_diag":
                def _ek_diag_key(d):
                    try:
                        return float(str((d.get("ek_specs") or {}).get("display") or ""))
                    except Exception:
                        return 0.0
                return _ek_diag_key
            if col == "ek_status":
                return lambda d: str((d.get("ek_specs") or {}).get("status") or "").lower()
            if col == "ek_shops":
                def _shops_key(d):
                    try:
                        return int((d.get("ek_specs") or {}).get("shops_count") or -1)
                    except Exception:
                        return -1
                return _shops_key
            if col == "ek_price":
                def _price_key(d):
                    es = d.get("ek_specs") or {}
                    if reverse:  # Z→A: max price
                        v = es.get("price_max") or es.get("price_min") or es.get("price_low") or 0
                    else:  # A→Z: min price
                        v = es.get("price_min") or es.get("price_low") or es.get("price_max") or 0
                    try:
                        return float(v)
                    except Exception:
                        return 0.0
                return _price_key
            return None

        key_fn = _secondary_key(col)
        if key_fn is not None:
            if col == "brand":
                # _gsm_native_order_key already encodes "newest first" via
                # negated date components — asc/desc toggling on the brand
                # header isn't a meaningful concept for this tiered key, so
                # `reverse` is intentionally not applied here.
                result = sorted(result, key=key_fn)
            else:
                # Pass 1: order everything by the chosen column/direction.
                result = sorted(result, key=key_fn, reverse=reverse)
        # Pass 2: stable regroup by brand (ascending) so brand groups never
        # interleave — ties (same brand) keep the order from pass 1, so the
        # chosen sort still applies *within* each brand group. Only makes
        # sense for the brand-grouped view — in flat mode (grouping off)
        # the user expects one true global sort by the chosen column, not
        # a sort-within-brand-then-brand-order, so skip it entirely there.
        if not self._flat_mode:
            result = sorted(result, key=lambda d: (d.get("brand") or "").lower())
        return result

    def _page_items(self, filtered: list[dict]) -> list[dict]:
        # Explicit product requirement: everything stays on ONE page/table,
        # no pagination — a 50/page split was tried and rejected. Speed
        # instead comes from _ROW_CTRL_CACHE (see _get_or_build_row): every
        # device's row Control is built at most once per process and
        # reused from then on, whether it's revealed via brand-expand or
        # via flat mode, and a background pass (_prebuild_all_rows) warms
        # that cache for the whole filtered catalog shortly after load so
        # toggling "Групування" off draws from cache instead of building
        # ~1000 rows on the spot.
        self._last_filtered_count = len(filtered)
        return filtered

    # ------------------------------------------------------------------ #
    #  Helper: show snack                                                  #
    # ------------------------------------------------------------------ #
    def _get_page_safe(self):
        """Prefer the cached page ref; fall back to content_area.page, but
        never let a detached control's `.page` getter (RuntimeError, not
        None, once removed from the tree — e.g. after an account switch
        while a background parse run is still finishing) crash the caller."""
        if self._page_ref:
            return self._page_ref
        try:
            return self.content_area.page if self.content_area else None
        except (RuntimeError, AttributeError):
            return None

    def _show_snack(self, text, color):
        page = self._get_page_safe()
        if not page:
            return
        page.snack_bar = ft.SnackBar(
            ft.Text(text, color=ft.Colors.WHITE, weight="bold"),
            bgcolor=color,
            duration=3000,
        )

    def _show_help(self) -> None:
        page = self._get_page_safe()
        if not page:
            return
        from views.help_utils import show_modal_help
        show_modal_help(page, self.t("gsm.help.title", "Довідка — GSM Parser"), [
            self.t("gsm.help.desc", "Результати GSM Parser: моніторинг цін і характеристик смартфонів з GSM Arena та E-Katalog."),
            (self.t("gsm.help.steps_title", "Як користуватись"), None),
            self.t("gsm.help.step1", "1. Таблиця показує всі спарсені пристрої з поточними цінами та статусами."),
            self.t("gsm.help.step2", "2. «Фільтри» — розширена фільтрація за брендом, типом, роком, статусом, джерелом."),
            self.t("gsm.help.step3", "3. Клік на рядок — відкриває картку пристрою (характеристики, сумісність, діаграма цін)."),
            self.t("gsm.help.step4", "4. «Синхронізація» оновлює кеш з бази даних."),
            self.t("gsm.help.step5", "5. «Запустити» — запускає парсер (повний / лише GSM / лише E-Katalog)."),
            self.t("gsm.help.step6", "6. «Запланувати запуск…» у тому ж меню — запускає парсер не одразу, а через заданий час (застосунок має лишатись відкритим)."),
        ])

    # ------------------------------------------------------------------ #
    #  Device card modal                                                   #
    # ------------------------------------------------------------------ #
    def _price_history_popup(self, page, device: dict):
        """Small popup with daily E-Katalog price/shops history. Accepts merged or flat dict."""
        device_id = str(device.get("id") or "")
        ek_id = str(device.get("ek_id") or "")

        async def _do():
            history = await asyncio.to_thread(self.db.get_gsm_daily_history, device_id, days=90)

            # Merge EK device history (recorded under EK id before gsm_ref fix, or legacy data)
            if ek_id and ek_id != device_id:
                ek_history = await asyncio.to_thread(self.db.get_gsm_daily_history, ek_id, days=90)
                if ek_history:
                    by_date = {str(h.get("record_date") or "")[:10]: h for h in history}
                    for eh in ek_history:
                        rd = str(eh.get("record_date") or "")[:10]
                        if rd in by_date:
                            row = by_date[rd]
                            if not row.get("price_ek_min"):
                                row["price_ek_min"] = eh.get("price_ek_min")
                            if not row.get("price_ek_max"):
                                row["price_ek_max"] = eh.get("price_ek_max")
                            if not row.get("shops_count"):
                                row["shops_count"] = eh.get("shops_count")
                        else:
                            by_date[rd] = eh
                    history = sorted(
                        by_date.values(), key=lambda h: str(h.get("record_date") or "")
                    )

            if not history:
                rows = [
                    ft.Text(
                        self.t("gsm.no_data_90days", "Немає даних за останні 90 днів"),
                        color=ft.Colors.BLUE_GREY_400,
                        size=13,
                    )
                ]
            else:
                hdr = ft.Row(
                    [
                        ft.Container(
                            ft.Text("Дата", size=11, weight=ft.FontWeight.W_700), width=90
                        ),
                        ft.Container(
                            ft.Text("ЄК мін ₴", size=11, weight=ft.FontWeight.W_700),
                            width=85,
                        ),
                        ft.Container(
                            ft.Text("ЄК макс ₴", size=11, weight=ft.FontWeight.W_700),
                            width=85,
                        ),
                        ft.Container(
                            ft.Text("Магаз.", size=11, weight=ft.FontWeight.W_700), width=55
                        ),
                    ],
                    spacing=6,
                )
                data_rows = []
                for h in reversed(history[-30:]):
                    rd = str(h.get("record_date") or "")[:10]
                    data_rows.append(
                        ft.Row(
                            [
                                ft.Container(ft.Text(rd, size=12), width=90),
                                ft.Container(
                                    ft.Text(_fmt_price(h.get("price_ek_min")), size=12),
                                    width=85,
                                ),
                                ft.Container(
                                    ft.Text(_fmt_price(h.get("price_ek_max")), size=12),
                                    width=85,
                                ),
                                ft.Container(
                                    ft.Text(str(h.get("shops_count") or "—"), size=12),
                                    width=55,
                                ),
                            ],
                            spacing=6,
                        )
                    )
                rows = [hdr, ft.Divider(height=6)] + data_rows

            dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text(
                    f"Історія: {device.get('model_name', '')}",
                    size=15,
                    weight=ft.FontWeight.W_700,
                ),
                content=ft.Container(
                    width=530,
                    height=min(60 + len(history) * 28, 360),
                    content=ft.Column(rows, spacing=4, scroll=ft.ScrollMode.AUTO),
                ),
                actions=[ft.TextButton("Закрити", on_click=lambda _: page.pop_dialog())],
                actions_alignment=ft.MainAxisAlignment.END,
            )
            page.show_dialog(dlg)

        page.run_task(_do)

    def _build_adaptive_charts(self, device_id: str, dev: dict | None = None) -> ft.Control:
        """Price and shops charts rendered via matplotlib → base64 PNG.

        The rendered PNG + hover-hotspot data are cached per device_id
        (_CHART_RENDER_CACHE) so re-opening the same device's chart tab
        doesn't re-run matplotlib unless its history actually changed —
        matplotlib import + render + savefig is the dominant cost here."""
        detail = self._get_or_fetch_device_detail(device_id, dev)
        history = detail.get("hist730") or []
        if len(history) < 2:
            return ft.Text(
                "Недостатньо даних для діаграми", color=ft.Colors.BLUE_GREY_400, size=12
            )

        sig = (len(history), str(history[-1].get("record_date") or ""))
        with _CHART_RENDER_LOCK:
            cached = _CHART_RENDER_CACHE.get(device_id)
        if cached is not None and cached.get("sig") == sig:
            return self._render_chart_from_data(cached)

        try:
            import base64
            import io

            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.dates as mdates
            import matplotlib.pyplot as plt
            from matplotlib.ticker import (FixedLocator, FuncFormatter,
                                           MaxNLocator)

            dates, ek_min_vals, ek_max_vals, shops_vals = [], [], [], []
            for h in history:
                raw = str(h.get("record_date") or "")[:10]
                try:
                    dates.append(datetime.fromisoformat(raw))
                except Exception:
                    continue
                ek_min_vals.append(_safe_float(h.get("price_ek_min")))
                ek_max_vals.append(_safe_float(h.get("price_ek_max")))
                shops_vals.append(_safe_float(h.get("shops_count")))

            if not dates:
                return ft.Text("Немає дат", color=ft.Colors.BLUE_GREY_400, size=12)

            # AK/AL/AM (shops/price_min/price_max) are three INDEPENDENT
            # change logs — a date only gets an entry for a field if THAT
            # field changed that day, so plenty of dates have e.g. a min-price
            # entry but no max-price entry even though a max price obviously
            # still existed that day (it just hadn't changed recently).
            # Forward-fill each series so the hover panel always reflects
            # "last known value as of this date" instead of "did this exact
            # field change on this exact date".
            def _forward_fill(vals: list) -> list:
                filled = []
                last = None
                for v in vals:
                    if v is not None:
                        last = v
                    filled.append(last)
                return filled

            ek_min_filled = _forward_fill(ek_min_vals)
            ek_max_filled = _forward_fill(ek_max_vals)
            shops_filled = _forward_fill(shops_vals)

            def _mk_fmt_loc():
                """Day ticks while ≤100 distinct days are present (only at
                dates that actually have data — never calendar placeholders
                for gaps), then month ticks while ≤50 distinct months, then
                year ticks beyond that."""
                uniq_days = sorted({d.date() for d in dates})
                if len(uniq_days) <= 100:
                    tick_days = uniq_days
                    if len(tick_days) > 12:
                        step = max(1, round(len(tick_days) / 10))
                        tick_days = tick_days[::step]
                        if uniq_days[-1] not in tick_days:
                            tick_days = tick_days + [uniq_days[-1]]
                    ticks = [mdates.date2num(d) for d in tick_days]
                    return mdates.DateFormatter("%d.%m"), FixedLocator(ticks)

                uniq_months = sorted({(d.year, d.month) for d in dates})
                if len(uniq_months) <= 50:
                    ticks = [
                        mdates.date2num(datetime(y, m, 1)) for y, m in uniq_months
                    ]
                    return mdates.DateFormatter("%m.%y"), FixedLocator(ticks)

                uniq_years = sorted({d.year for d in dates})
                ticks = [mdates.date2num(datetime(y, 1, 1)) for y in uniq_years]
                return mdates.DateFormatter("%Y"), FixedLocator(ticks)

            def _pairs(ds, vals):
                return (
                    zip(*[(d, v) for d, v in zip(ds, vals) if v is not None])
                    if any(v is not None for v in vals)
                    else ([], [])
                )

            COL_MIN = _OK_DK      # emerald — ЄК мін (краща/нижча ціна)
            COL_MAX = "#D97706"   # amber — ЄК макс
            COL_SHOPS = "#7C3AED"  # violet — магазини
            COL_GRID = "#EEF0F3"
            COL_AXIS = _BORDER
            COL_MUTED = _MUTED
            COL_TITLE = _TEXT

            FIG_W_IN, FIG_H_IN = 6.4, 4.3
            CHART_DPI = 120
            fig, (ax1, ax2) = plt.subplots(
                2, 1, figsize=(FIG_W_IN, FIG_H_IN), facecolor="#FFFFFF", dpi=CHART_DPI
            )
            fig.subplots_adjust(
                hspace=1.0, left=0.12, right=0.97, top=0.85, bottom=0.16
            )

            date_range_label = f"{dates[0]:%d.%m.%Y} — {dates[-1]:%d.%m.%Y}"

            def _style_axis(ax, title):
                ax.set_facecolor("#FFFFFF")
                ax.text(
                    0.0, 1.32, title, transform=ax.transAxes, fontsize=10.5,
                    fontweight="bold", color=COL_TITLE, va="bottom",
                )
                ax.text(
                    0.0, 1.10, date_range_label, transform=ax.transAxes,
                    fontsize=7.5, color=COL_MUTED, va="bottom",
                )
                ax.tick_params(axis="both", labelsize=7, colors=COL_MUTED, length=0)
                for side in ("top", "right", "left"):
                    ax.spines[side].set_visible(False)
                ax.spines["bottom"].set_color(COL_AXIS)
                ax.set_axisbelow(True)
                ax.grid(axis="y", color=COL_GRID, linestyle="-", linewidth=1.0)

            # ---- Price chart ---------------------------------------------
            _style_axis(ax1, "Ціни ЄК")
            fmt1, loc1 = _mk_fmt_loc()
            ax1.xaxis.set_major_formatter(fmt1)
            ax1.xaxis.set_major_locator(loc1)
            plt.setp(ax1.get_xticklabels(), rotation=30, ha="right")
            ax1.yaxis.set_major_formatter(
                FuncFormatter(lambda v, _p: f"{v:,.0f}".replace(",", " "))
            )
            ax1.yaxis.set_major_locator(MaxNLocator(nbins=5, integer=True))

            band = [
                (d, lo, hi)
                for d, lo, hi in zip(dates, ek_min_filled, ek_max_filled)
                if lo is not None and hi is not None
            ]
            if band:
                band_d, band_lo, band_hi = zip(*band)
                ax1.fill_between(
                    band_d, band_lo, band_hi, color=_ACCENT, alpha=0.07, zorder=1
                )

            mn_d, mn_v = _pairs(dates, ek_min_vals)
            if mn_d:
                mn_d, mn_v = list(mn_d), list(mn_v)
                ax1.plot(
                    mn_d, mn_v, color=COL_MIN, linewidth=1.8, marker="o",
                    markersize=3.2, markerfacecolor="#FFFFFF",
                    markeredgecolor=COL_MIN, markeredgewidth=1.2, zorder=3,
                )
                ax1.annotate(
                    f"мін {mn_v[-1]:,.0f} ₴".replace(",", " "),
                    xy=(mn_d[-1], mn_v[-1]), xytext=(-2, 9),
                    textcoords="offset points", ha="right", fontsize=7.5,
                    fontweight="bold", color=COL_MIN,
                )
            mx_d, mx_v = _pairs(dates, ek_max_vals)
            if mx_d:
                mx_d, mx_v = list(mx_d), list(mx_v)
                ax1.plot(
                    mx_d, mx_v, color=COL_MAX, linewidth=1.8, marker="o",
                    markersize=3.2, markerfacecolor="#FFFFFF",
                    markeredgecolor=COL_MAX, markeredgewidth=1.2, zorder=3,
                )
                ax1.annotate(
                    f"макс {mx_v[-1]:,.0f} ₴".replace(",", " "),
                    xy=(mx_d[-1], mx_v[-1]), xytext=(-2, 8),
                    textcoords="offset points", ha="right", fontsize=7.5,
                    fontweight="bold", color=COL_MAX,
                )

            # ---- Shops chart -----------------------------------------------
            _style_axis(ax2, "Кількість магазинів")
            fmt2, loc2 = _mk_fmt_loc()
            ax2.xaxis.set_major_formatter(fmt2)
            ax2.xaxis.set_major_locator(loc2)
            plt.setp(ax2.get_xticklabels(), rotation=30, ha="right")
            ax2.yaxis.set_major_locator(MaxNLocator(nbins=5, integer=True))

            sh_d, sh_v = _pairs(dates, shops_vals)
            if sh_d:
                sh_d, sh_v = list(sh_d), list(sh_v)
                ax2.fill_between(sh_d, sh_v, color=COL_SHOPS, alpha=0.12, zorder=1)
                ax2.plot(
                    sh_d, sh_v, color=COL_SHOPS, linewidth=1.8, marker="o",
                    markersize=3.2, markerfacecolor="#FFFFFF",
                    markeredgecolor=COL_SHOPS, markeredgewidth=1.2, zorder=3,
                )
                ax2.annotate(
                    f"{int(sh_v[-1])} магазинів",
                    xy=(sh_d[-1], sh_v[-1]), xytext=(-2, 8),
                    textcoords="offset points", ha="right", fontsize=7.5,
                    fontweight="bold", color=COL_SHOPS,
                )
            else:
                ax2.set_xlim(min(dates), max(dates))

            # Hover panel: matplotlib renders a static PNG (this Flet version
            # has no native chart widget), so exact-value-on-hover is done by
            # overlaying invisible, hoverable Containers on top of the image
            # at each point's real pixel position and pushing the values into
            # a fixed info panel to the RIGHT of the chart — using the SAME
            # dpi for drawing and saving keeps get_window_extent()/transData
            # in exact sync with the final PNG's pixel grid. Flutter's
            # built-in Tooltip was dropped entirely here: it always centers
            # over its trigger (so it kept covering the chart) and only shows
            # after its own hover delay — a fully custom panel is instant and
            # lives outside the plotted area.
            #
            # savefig() already performs a full draw internally — a separate
            # fig.canvas.draw() beforehand rendered the whole figure a SECOND
            # time for nothing (measured: each pass costs ~50-90ms, so this
            # was silently doubling every chart's render time). get_window_extent()
            # / transData stay valid and pixel-identical whether read right
            # after canvas.draw() or right after savefig() — verified byte-for-
            # byte — so savefig() runs first and IS the one draw pass.
            buf = io.BytesIO()
            fig.savefig(buf, format="png")
            FIG_W_PX = FIG_W_IN * CHART_DPI
            FIG_H_PX = FIG_H_IN * CHART_DPI
            DISPLAY_W = 560
            SCALE = DISPLAY_W / FIG_W_PX
            DISPLAY_H = round(FIG_H_PX * SCALE)

            def _ax_px_bounds(ax):
                bbox = ax.get_window_extent()
                # matplotlib y is bottom-up; flip to Flet's top-down origin.
                return FIG_H_PX - bbox.y1, FIG_H_PX - bbox.y0

            def _x_px(ax, d):
                y0 = ax.get_ylim()[0]
                return ax.transData.transform((mdates.date2num(d), y0))[0]

            # Fewer, wider hotspots — each one is an extra Flet control, and
            # this whole Stack gets sent to Flutter in a single patch the
            # first time the chart tab opens. 80 (×2 charts, ×2 controls
            # each) was adding 300+ controls to that one patch, which is
            # exactly the "too much in one update" pattern that reads as a
            # freeze on appearance.
            MAX_HOTSPOTS = 40
            n = len(dates)
            if n <= MAX_HOTSPOTS:
                idxs = list(range(n))
            else:
                step = n / MAX_HOTSPOTS
                idxs = sorted({int(i * step) for i in range(MAX_HOTSPOTS)} | {n - 1})

            y1_top, y1_bottom = _ax_px_bounds(ax1)
            y2_top, y2_bottom = _ax_px_bounds(ax2)
            hotspot_w = max(6.0, (DISPLAY_W * 0.9) / max(len(idxs), 1))
            last_date_str = dates[-1].strftime("%d.%m.%Y")

            # Every hotspot (whichever chart it's overlaid on) carries all
            # three values for its date — hovering EITHER chart updates the
            # whole panel (price min/max AND shops count together), since
            # they're all facets of the same date, not independent charts.
            price_hotspots = []
            shop_hotspots = []
            for i in idxs:
                d = dates[i]
                lo, hi, sv = ek_min_filled[i], ek_max_filled[i], shops_filled[i]
                day_str = d.strftime("%d.%m.%Y")
                if lo is not None or hi is not None:
                    px = _x_px(ax1, d) * SCALE
                    price_hotspots.append(
                        (
                            max(0.0, px - hotspot_w / 2),
                            y1_top * SCALE,
                            hotspot_w,
                            max(4.0, (y1_bottom - y1_top) * SCALE),
                            day_str,
                            lo,
                            hi,
                            sv,
                        )
                    )
                if sv is not None:
                    px = _x_px(ax2, d) * SCALE
                    shop_hotspots.append(
                        (
                            max(0.0, px - hotspot_w / 2),
                            y2_top * SCALE,
                            hotspot_w,
                            max(4.0, (y2_bottom - y2_top) * SCALE),
                            day_str,
                            lo,
                            hi,
                            sv,
                        )
                    )

            plt.close(fig)
            buf.seek(0)
            b64 = base64.b64encode(buf.read()).decode()

            render_data = {
                "sig": sig,
                "b64": b64,
                "w": DISPLAY_W,
                "h": DISPLAY_H,
                "price_hotspots": price_hotspots,
                "shop_hotspots": shop_hotspots,
                "last_date_str": last_date_str,
                "last_min": ek_min_filled[-1],
                "last_max": ek_max_filled[-1],
                "last_shops": shops_filled[-1],
            }
            with _CHART_RENDER_LOCK:
                _CHART_RENDER_CACHE[device_id] = render_data
            return self._render_chart_from_data(render_data)
        except Exception as exc:
            logger.error("[GSM] _build_adaptive_charts failed: %s", exc)
            return ft.Text(
                f"Діаграма недоступна: {exc}", color=ft.Colors.BLUE_GREY_400, size=11
            )

    def _render_chart_from_data(self, data: dict) -> ft.Control:
        """Rebuild the lightweight Flet control tree (PNG image + hover
        hotspots + info panel) from a cached render payload — no matplotlib
        involved, so this is cheap enough to call on every chart-tab open."""
        COL_MIN = _OK_DK
        COL_MAX = "#D97706"
        COL_SHOPS = "#7C3AED"
        COL_GRID = "#EEF0F3"
        COL_AXIS = _BORDER
        COL_MUTED = _MUTED
        COL_TITLE = _TEXT

        hover_date = ft.Text(
            data["last_date_str"], size=12, weight=ft.FontWeight.W_600, color=COL_TITLE
        )
        hover_min = ft.Text(
            _fmt_price(data["last_min"]), size=16, weight=ft.FontWeight.W_700,
            color=COL_MIN,
        )
        hover_max = ft.Text(
            _fmt_price(data["last_max"]), size=16, weight=ft.FontWeight.W_700,
            color=COL_MAX,
        )
        hover_shops = ft.Text(
            str(int(data["last_shops"])) if data["last_shops"] is not None else "—",
            size=16, weight=ft.FontWeight.W_700, color=COL_SHOPS,
        )

        def _panel_row(dot_color, label, value_ctrl):
            return ft.Row(
                [
                    ft.Container(
                        width=9, height=9, bgcolor=dot_color, border_radius=5,
                        margin=ft.margin.only(top=5),
                    ),
                    ft.Column(
                        [ft.Text(label, size=10.5, color=COL_MUTED), value_ctrl],
                        spacing=0,
                    ),
                ],
                spacing=8,
            )

        panel = ft.Container(
            width=190,
            padding=14,
            bgcolor=_PANEL,
            border=ft.border.all(1, COL_AXIS),
            border_radius=12,
            content=ft.Column(
                [
                    ft.Text("Дата", size=10.5, color=COL_MUTED),
                    hover_date,
                    ft.Divider(height=16, color=COL_GRID),
                    _panel_row(COL_MIN, "ЄК мін", hover_min),
                    ft.Container(height=10),
                    _panel_row(COL_MAX, "ЄК макс", hover_max),
                    ft.Divider(height=16, color=COL_GRID),
                    _panel_row(COL_SHOPS, "Магазинів", hover_shops),
                ],
                spacing=2,
            ),
        )

        _last_hover_day = {"value": data["last_date_str"]}
        # Sweeping the cursor across the chart crosses many hotspot
        # boundaries in a fraction of a second — each one is a genuinely
        # DIFFERENT day, so the same-day dedup guard below doesn't skip any
        # of them, and each one costs 4 individual Text.update() calls.
        # That's cheap per call but adds up across a fast sweep into
        # exactly the symptom reported live: a multi-second stall while the
        # backlog of queued hover events works through, then the panel
        # visibly races through several intermediate dates at once as it
        # catches up. _pending holds whatever the CURSOR'S CURRENT position
        # is; only one _apply_pending() is ever in flight at a time
        # (coalescing every event that arrives while it's pending into a
        # single real update instead of one per event), and it always
        # applies the latest known position, not a stale queued one.
        _pending = {"day_str": None, "lo": None, "hi": None, "sv": None, "scheduled": False}

        def _apply_pending():
            day_str = _pending["day_str"]
            if day_str is None:
                return
            hover_date.value = day_str
            hover_min.value = _fmt_price(_pending["lo"])
            hover_max.value = _fmt_price(_pending["hi"])
            _sv = _pending["sv"]
            hover_shops.value = str(int(_sv)) if _sv is not None else "—"
            # panel.update() diffs panel's WHOLE subtree (Container >
            # Column > Text/Divider/Row[Container,Column[Text,Text]] x3)
            # on every call — measured multiple seconds each via py-spy
            # live process dumps, deep in Flet's own object_patch
            # comparison, not our code. Updating just the 4 Text leaves
            # that actually changed is a far smaller diff each (no
            # children of their own) and is all that's visually needed.
            for _t in (hover_date, hover_min, hover_max, hover_shops):
                with contextlib.suppress(Exception):
                    _t.update()

        async def _apply_pending_soon():
            # ~1-2 frames — long enough to coalesce a whole burst of
            # same-tick hover events crossing several hotspots, short
            # enough that a cursor that's actually still (or moving slowly)
            # never reads as delayed.
            await asyncio.sleep(0.03)
            _pending["scheduled"] = False
            _apply_pending()

        def _update_all(day_str, lo, hi, sv):
            # Shared by hotspots on BOTH charts — hovering either the price
            # chart or the shops chart updates all three panel values for
            # that date together, since they're the same date, not
            # independent per-chart readings.
            def _h(e):
                if not e.data:
                    return
                # Flet's on_hover keeps firing while the mouse sits inside the
                # SAME hotspot (not just once on entry) — without this guard
                # every one of those repeats re-ran the update below, even
                # though the panel's own values hadn't changed at all. Skip
                # entirely when we're still on the date we already showed.
                if _last_hover_day["value"] == day_str:
                    return
                _last_hover_day["value"] = day_str
                _pending["day_str"] = day_str
                _pending["lo"] = lo
                _pending["hi"] = hi
                _pending["sv"] = sv
                if _pending["scheduled"]:
                    return
                _pending["scheduled"] = True
                pg = self._get_page_safe()
                if pg is not None:
                    with contextlib.suppress(Exception):
                        pg.run_task(_apply_pending_soon)
                        return
                # No page reference available — fall back to applying
                # immediately rather than dropping the update entirely.
                _pending["scheduled"] = False
                _apply_pending()

            return _h

        hover_overlays = [
            ft.Container(
                left=left, top=top, width=width, height=height,
                on_hover=_update_all(day_str, lo, hi, sv),
            )
            for (left, top, width, height, day_str, lo, hi, sv) in data["price_hotspots"]
        ]
        hover_overlays.extend(
            ft.Container(
                left=left, top=top, width=width, height=height,
                on_hover=_update_all(day_str, lo, hi, sv),
            )
            for (left, top, width, height, day_str, lo, hi, sv) in data["shop_hotspots"]
        )

        chart_stack = ft.Stack(
            [
                ft.Image(
                    src=f"data:image/png;base64,{data['b64']}",
                    width=data["w"],
                    height=data["h"],
                ),
                *hover_overlays,
            ],
            width=data["w"],
            height=data["h"],
        )

        return ft.Row(
            [chart_stack, panel],
            spacing=14,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )

    @staticmethod
    def _close_top_dialog(page):
        """Close the topmost open AlertDialog in page.overlay (Flet 0.84)."""
        with contextlib.suppress(Exception):
            for ov in reversed(list(getattr(page, "overlay", []))):
                if isinstance(ov, ft.AlertDialog):
                    page.pop_dialog()
                    return

    def _compat_global_modal(self, page_ctrl) -> None:
        """Global view of all confirmed compatible pairs, grouped by accessory
        type (Чохли / Захисне скло) and then by status."""
        if not page_ctrl:
            return

        async def _do():
            pairs = await asyncio.to_thread(self.db.get_all_gsm_compatible_pairs) or []
            case_pairs_all = [p for p in pairs if str(p.get("accessory_type") or "case") == "case"]
            # Watches don't take "cases" — split those pairs into their own
            # "Ремінці" (straps) group instead of lumping them under "Чохли".
            case_pairs = [
                p for p in case_pairs_all
                if str(p.get("device_type") or "").strip().lower() != "watch"
            ]
            strap_pairs = [
                p for p in case_pairs_all
                if str(p.get("device_type") or "").strip().lower() == "watch"
            ]
            glass_pairs = [p for p in pairs if str(p.get("accessory_type") or "") == "glass"]

            def _pair_btn(p) -> ft.Control:
                d_name = str(p.get("device_name") or "—")
                d_brand = str(p.get("device_brand") or "")
                c_name = str(p.get("compat_name") or "—")
                c_brand = str(p.get("compat_brand") or "")
                vl = str(p.get("variant_label") or "").strip()
                d_label = f"{d_brand} {d_name}".strip()
                c_label = f"{c_brand} {c_name}".strip() + (f" ({vl})" if vl else "")
                return ft.Container(
                    border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
                    padding=ft.padding.symmetric(horizontal=4, vertical=3),
                    content=ft.Row(
                        [
                            ft.Text(d_label, size=11, expand=True, no_wrap=True),
                            ft.Icon(ft.Icons.SWAP_HORIZ, size=14, color=_MUTED),
                            ft.Text(c_label, size=11, expand=True, no_wrap=True),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )

            def _section(title: str, items: list, color: str) -> ft.Column:
                hdr = ft.Container(
                    bgcolor=color,
                    padding=ft.padding.symmetric(horizontal=8, vertical=4),
                    border_radius=ft.border_radius.only(top_left=6, top_right=6),
                    content=ft.Text(
                        f"{title} ({len(items)})",
                        size=11, weight=ft.FontWeight.W_700, color=ft.Colors.WHITE,
                    ),
                )
                body = ft.Column(
                    [_pair_btn(p) for p in items] if items
                    else [ft.Text("Немає.", size=11, color=_MUTED, italic=True)],
                    spacing=0, tight=True,
                )
                return ft.Column([hdr, body], spacing=0, tight=True)

            def _accessory_group(title: str, items: list) -> ft.Column:
                compatible = [p for p in items if str(p.get("status") or "") == "compatible"]
                partially = [p for p in items if "partially" in str(p.get("status") or "")]
                return ft.Column(
                    [
                        ft.Text(
                            f"{title} ({len(items)})",
                            size=13, weight=ft.FontWeight.W_700,
                        ),
                        _section("Сумісні", compatible, _OK_DK),
                        ft.Container(height=10),
                        _section("Частково сумісні", partially, ft.Colors.AMBER_700),
                    ],
                    spacing=6,
                    tight=True,
                )

            modal_ref: list = [None]

            def _close(_e=None):
                with contextlib.suppress(Exception):
                    if modal_ref[0]:
                        page_ctrl.pop_dialog()

            modal = ft.AlertDialog(
                modal=True,
                title=ft.Row(
                    [
                        ft.Icon(ft.Icons.HANDSHAKE_OUTLINED, color=_OK_DK, size=20),
                        ft.Text(
                            f"Всі сумісні пристрої ({len(pairs)})",
                            size=14, weight=ft.FontWeight.W_700,
                        ),
                        ft.Container(expand=True),
                        ft.IconButton(
                            ft.Icons.CLOSE, icon_size=18, on_click=_close,
                            style=ft.ButtonStyle(padding=ft.padding.all(4)),
                        ),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                content=ft.Container(
                    width=700,
                    height=520,
                    content=ft.Column(
                        [
                            _accessory_group("Чохли", case_pairs),
                            ft.Container(height=18),
                            _accessory_group("Ремінці", strap_pairs),
                            ft.Container(height=18),
                            _accessory_group("Захисне скло", glass_pairs),
                        ],
                        spacing=0,
                        tight=True,
                        scroll=ft.ScrollMode.AUTO,
                    ),
                ),
                content_padding=ft.padding.all(16),
                actions=[],
                inset_padding=ft.padding.symmetric(horizontal=24, vertical=40),
            )
            modal_ref[0] = modal
            with contextlib.suppress(Exception):
                page_ctrl.open(modal)

        page_ctrl.run_task(_do)

    def _build_compat_readonly(self, device_id: str, page, dev: dict | None = None) -> ft.Control:
        """Read-only view: confirmed compatibility, grouped by Чохли / Захисне скло,
        each split into Сумісні / Частково сумісні / Можливо сумісні (Огляд tab,
        all users). The last bucket is not a confirmed status — it's the same
        same-brand+size candidate search used to power the suggestions in the
        edit tab (_build_compat_edit), just surfaced read-only here too."""
        detail = self._get_or_fetch_device_detail(device_id, dev)
        compat_by_type = detail.get("compat")
        if not isinstance(compat_by_type, dict):
            compat_by_type = {"case": compat_by_type or [], "glass": []}
        _STATUS_LABELS_MAP = {
            "compatible": "Сумісний",
            "partially_compatible": "Частково сумісний",
        }
        _dev = dev or {}
        _brand = str(_dev.get("brand") or "")
        _specs = _dev.get("specs") if isinstance(_dev.get("specs"), dict) else {}
        _display_size = str(_specs.get("display_size") or "").strip()
        _length = str(_specs.get("length") or "").strip()
        _width = str(_specs.get("width") or "").strip()
        _case_label = (
            "Ремінці"
            if str(_dev.get("device_type") or "").strip().lower() == "watch"
            else "Чохли"
        )
        _size_groups = self._get_xiaomi_size_groups(device_id, _dev)
        _is_split = len(_size_groups) > 1

        def _group_meta(g: dict) -> str:
            parts = [g.get("size_str") or "", g.get("diag_str") or ""]
            return "  •  ".join(p for p in parts if p)

        def _dedupe(raw: list) -> list:
            # Deduplicate by other_device_id (DB may store both directions)
            seen: set[str] = set()
            out = []
            for c in raw or []:
                oid = str(c.get("other_device_id") or "")
                if oid and oid not in seen:
                    seen.add(oid)
                    out.append(c)
            return out

        def _open(_, _id):
            async def _do():
                other = await asyncio.to_thread(self.db.get_gsm_merged_device_by_id, _id)
                if other:
                    self._device_detail_modal(page, other)

            page.run_task(_do)

        def _status_chip(status_str):
            partially = "partially" in status_str
            return ft.Container(
                padding=ft.padding.symmetric(horizontal=8, vertical=2),
                border_radius=999,
                bgcolor=ft.Colors.AMBER_50 if partially else _OK_BG,
                content=ft.Text(
                    _STATUS_LABELS_MAP.get(status_str, status_str),
                    size=11,
                    color=ft.Colors.AMBER_800 if partially else _OK_DK,
                ),
            )

        def _rows_for(items: list) -> list:
            rows = []
            for c in items:
                oid = str(c.get("other_device_id") or "")
                variant_lbl = str(c.get("variant_label") or "").strip()
                model_display = str(c.get("model_name") or "—")
                if variant_lbl:
                    model_display += f" ({variant_lbl})"
                rows.append(
                    ft.Row(
                        [
                            ft.TextButton(
                                model_display,
                                on_click=lambda _, _id=oid: _open(_, _id),
                                style=ft.ButtonStyle(
                                    padding=ft.padding.symmetric(horizontal=4, vertical=2)
                                ),
                            ),
                            _status_chip(str(c.get("status") or "")),
                        ],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )
            return rows

        def _possible_rows_for(items: list[tuple[dict, str]]) -> list:
            rows = []
            for p, variant_label in items:
                oid = str(p.get("id") or "")
                model_label = str(p.get("model_name") or "—")
                if variant_label:
                    model_label += f" ({variant_label})"
                rows.append(
                    ft.Row(
                        [
                            ft.TextButton(
                                model_label,
                                on_click=lambda _, _id=oid: _open(_, _id),
                                style=ft.ButtonStyle(
                                    padding=ft.padding.symmetric(horizontal=4, vertical=2)
                                ),
                            ),
                            ft.Container(
                                padding=ft.padding.symmetric(horizontal=8, vertical=2),
                                border_radius=999,
                                bgcolor=ft.Colors.BLUE_50,
                                content=ft.Text(
                                    "Можливо сумісний", size=11, color=ft.Colors.BLUE_800,
                                ),
                            ),
                        ],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )
            return rows

        def _sub_block(label: str, items: list, rows: list | None = None) -> ft.Column:
            _rows = rows if rows is not None else _rows_for(items)
            return ft.Column(
                [
                    ft.Text(label, size=12, weight=ft.FontWeight.W_600, color=_MUTED),
                    (
                        ft.Column(_rows, spacing=4, tight=True)
                        if _rows
                        else ft.Text("Немає.", size=12, color=_MUTED, italic=True)
                    ),
                ],
                spacing=4,
                tight=True,
            )

        def _possible_for(
            accessory_type: str, exclude_ids: set[str], active_group: dict | None = None
        ) -> list[tuple[dict, str]]:
            # Same candidate search that powers the suggestions in the edit
            # tab: same brand + same physical footprint for cases, same
            # brand + same diagonal for glass. When a size group is active,
            # ITS OWN L/W/diagonal are used instead of the blanket specs.
            if not _brand:
                return []
            if accessory_type == "case":
                _len = active_group["L"] if active_group else _length
                _wid = active_group["W"] if active_group else _width
                possible_raw = (
                    self.db.get_gsm_possible_compatible_by_size_expanded(
                        _brand, _len, _wid, device_id
                    )
                    if _len and _wid
                    else []
                )
            else:
                _diag = active_group["diagonal"] if active_group else _display_size
                possible_raw = (
                    self.db.get_gsm_possible_compatible_expanded(_brand, _diag, device_id)
                    if _diag
                    else []
                )
            expanded: list[tuple[dict, str]] = []
            for p in possible_raw or []:
                oid = str(p.get("id") or "")
                if not oid or oid in exclude_ids:
                    continue
                variants_raw = p.get("regional_variants") or []
                sized_variants = [
                    v for v in (variants_raw if isinstance(variants_raw, list) else [])
                    if isinstance(v, dict)
                    and str(v.get("region") or "").strip()
                    and str(v.get("L") or "").strip()
                ]
                unique_lengths = {str(v.get("L") or "").strip() for v in sized_variants}
                if len(unique_lengths) <= 1:
                    expanded.append((p, ""))
                else:
                    for v in sized_variants:
                        region = str(v.get("region") or "").strip()
                        l_val = str(v.get("L") or "").strip()
                        expanded.append((p, f"{region} ({l_val}мм)"))
            return expanded

        def _group_block(title: str, raw_items: list, accessory_type: str) -> ft.Column:
            items = _dedupe(raw_items)

            if not _is_split:
                compatible = [
                    c for c in items if "partially" not in str(c.get("status") or "")
                ]
                partial = [c for c in items if "partially" in str(c.get("status") or "")]
                confirmed_ids = {str(c.get("other_device_id") or "") for c in items}
                possible = _possible_for(accessory_type, confirmed_ids)
                return ft.Column(
                    [
                        ft.Text(title, size=13, weight=ft.FontWeight.W_700),
                        _sub_block("Сумісні", compatible),
                        _sub_block("Частково сумісні", partial),
                        _sub_block("Можливо сумісні", [], rows=_possible_rows_for(possible)),
                    ],
                    spacing=6,
                    tight=True,
                )

            # Multi-size device: this card covers several physically distinct
            # sizes — show a separate, clearly delimited sub-section per size
            # so nothing reads as "one shared list" for what are effectively
            # different devices.
            sub_sections = []
            for i, g in enumerate(_size_groups):
                g_label = g.get("label") or ""
                g_items = [
                    c for c in items
                    if not str(c.get("variant_label") or "").strip()
                    or str(c.get("variant_label") or "").strip() == g_label
                ]
                compatible = [
                    c for c in g_items if "partially" not in str(c.get("status") or "")
                ]
                partial = [c for c in g_items if "partially" in str(c.get("status") or "")]
                confirmed_ids = {str(c.get("other_device_id") or "") for c in g_items}
                possible = _possible_for(accessory_type, confirmed_ids, active_group=g)
                meta = _group_meta(g)
                header = f"Розмір {i + 1}: {meta or '—'}" + (f"  ({g_label})" if g_label else "")
                sub_sections.append(
                    ft.Container(
                        padding=ft.padding.only(left=12, top=6, bottom=6),
                        border=ft.border.only(left=ft.BorderSide(3, _ACCENT)),
                        content=ft.Column(
                            [
                                ft.Text(header, size=12, weight=ft.FontWeight.W_700, color=_ACCENT),
                                _sub_block("Сумісні", compatible),
                                _sub_block("Частково сумісні", partial),
                                _sub_block("Можливо сумісні", [], rows=_possible_rows_for(possible)),
                            ],
                            spacing=4,
                            tight=True,
                        ),
                    )
                )

            return ft.Column(
                [ft.Text(title, size=13, weight=ft.FontWeight.W_700), *sub_sections],
                spacing=10,
                tight=True,
            )

        return ft.Column(
            [
                _group_block(_case_label, compat_by_type.get("case") or [], "case"),
                ft.Divider(height=10),
                _group_block("Захисне скло", compat_by_type.get("glass") or [], "glass"),
            ],
            spacing=8,
            tight=True,
        )

    def _build_compat_edit(
        self, device: dict, page, accessory_type: str, refresh_cb=None
    ) -> ft.Control:
        """Compatibility editing for one accessory_type ('case' | 'glass'):
        auto-suggest + manual same-brand + cross-brand. Gated by can_edit_case/
        can_edit_glass — see tab wiring in the device modal.

        refresh_cb: called after any add/delete to refresh this tab's content
        IN PLACE within the already-open device modal. The device modal is
        opened once and must stay open — it must never close itself and
        reopen a fresh copy just because a compat row changed.

        Xiaomi devices whose regional variants have 2+ DISTINCT physical
        sizes are treated like several devices sharing one product card:
        a size-group selector is shown above the usual editor, and
        compatibility is tracked (and filtered) separately per size via
        the confirmed row's variant_label. Devices with 0-1 size groups
        behave exactly as before (no selector, no filtering)."""
        device_id = str(device.get("id") or "")
        brand = str(device.get("brand") or "")
        specs = device.get("specs") if isinstance(device.get("specs"), dict) else {}
        display_size = str(specs.get("display_size") or "").strip()
        device_type = str(device.get("device_type") or "")

        _STATUS_OPTIONS = ["compatible", "partially_compatible"]
        _STATUS_LABELS_MAP = {
            "compatible": "Сумісний",
            "partially_compatible": "Частково сумісний",
        }

        size_groups = self._get_xiaomi_size_groups(device_id, device)
        is_split = len(size_groups) > 1

        async def _reload():
            if refresh_cb is not None:
                refresh_cb()
                return
            # Fallback only — normal usage always supplies refresh_cb so the
            # already-open modal updates in place instead of closing/reopening.
            GsmParserView._close_top_dialog(page)
            dev = await asyncio.to_thread(self.db.get_gsm_merged_device_by_id, device_id)
            if dev:
                self._device_detail_modal(page, dev)

        def _open_card(oid):
            def _h(_):
                async def _do():
                    other = await asyncio.to_thread(self.db.get_gsm_merged_device_by_id, oid)
                    if other:
                        self._device_detail_modal(page, other)

                page.run_task(_do)

            return _h

        def _combine_label(mine: str, other: str) -> str:
            # "mine" = the CURRENT device's active size-group label; "other"
            # = the CANDIDATE device's own region label (only set when the
            # candidate ITSELF has multiple body sizes). Keep both when both
            # apply so neither side's distinction is lost.
            if mine and other:
                return f"{mine} → {other}"
            return mine or other

        def _build_group_body(active_group: dict | None) -> ft.Control:
            _group_label = active_group["label"] if active_group else ""

            confirmed_raw = self.db.get_gsm_compatible_devices(device_id, accessory_type)
            # Deduplicate by other_device_id (DB may store bidirectional rows)
            _seen: set[str] = set()
            confirmed_all = []
            for _c in confirmed_raw or []:
                _oid = str(_c.get("other_device_id") or "")
                if _oid and _oid not in _seen:
                    _seen.add(_oid)
                    confirmed_all.append(_c)

            if is_split:
                # An empty variant_label was saved before size-splitting
                # existed (or applies to a shared accessory) — show it under
                # every group. A label matching THIS group's own label
                # belongs here too; anything else belongs to another group.
                confirmed = [
                    c for c in confirmed_all
                    if not str(c.get("variant_label") or "").strip()
                    or str(c.get("variant_label") or "").strip() == _group_label
                ]
            else:
                confirmed = confirmed_all
            confirmed_ids = {str(r.get("other_device_id") or "") for r in confirmed}

            # ── Confirmed list with delete buttons ──────────────────────
            def _del_compat(compat_id):
                def _h(_):
                    async def _do():
                        await asyncio.to_thread(
                            self.db.delete_gsm_compatible, device_id, compat_id, accessory_type
                        )
                        await asyncio.to_thread(self._refresh_cached_detail, device_id, device)
                        await asyncio.to_thread(self._refresh_cached_detail, compat_id)  # bidirectional — other side too
                        self._show_snack(self.t("gsm.record_deleted", "Запис видалено"), ft.Colors.ORANGE)
                        await _reload()

                    page.run_task(_do)

                return _h

            conf_rows = []
            for c in confirmed:
                oid = str(c.get("other_device_id") or "")
                partially = "partially" in str(c.get("status") or "")
                status_label = _STATUS_LABELS_MAP.get(str(c.get("status") or ""), "")
                _vlbl = str(c.get("variant_label") or "").strip()
                _model_disp = str(c.get("model_name") or "—") + (f" ({_vlbl})" if _vlbl else "")
                conf_rows.append(
                    ft.Row(
                        [
                            ft.TextButton(
                                _model_disp,
                                on_click=_open_card(oid),
                                style=ft.ButtonStyle(
                                    padding=ft.padding.symmetric(horizontal=4, vertical=2)
                                ),
                            ),
                            ft.Container(
                                padding=ft.padding.symmetric(horizontal=8, vertical=2),
                                border_radius=999,
                                bgcolor=ft.Colors.AMBER_50 if partially else _OK_BG,
                                content=ft.Text(
                                    status_label,
                                    size=11,
                                    color=ft.Colors.AMBER_800 if partially else _OK_DK,
                                ),
                            ),
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                icon_color=ft.Colors.RED_400,
                                icon_size=16,
                                tooltip="Видалити",
                                on_click=_del_compat(oid),
                            ),
                        ],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )

            # ── Auto-suggest, criteria differ by accessory type ──────────
            # Чохли (case): same brand + same physical footprint (L×W) — the
            # diagonal is irrelevant to whether a case fits.
            # Скло (glass): same brand + same diagonal — the screen size is
            # what a glass protector is cut to, the body outline doesn't
            # matter. When a size group is active, ITS OWN L/W/diagonal are
            # used instead of the device's blanket GSM Arena specs.
            if accessory_type == "case":
                _length = active_group["L"] if active_group else str(specs.get("length") or "").strip()
                _width = active_group["W"] if active_group else str(specs.get("width") or "").strip()
                possible_raw = (
                    self.db.get_gsm_possible_compatible_by_size_expanded(
                        brand, _length, _width, device_id
                    )
                    if _length and _width
                    else []
                )
                _suggest_title = "Можливі сумісні (однаковий бренд + розмір)"
            else:
                _diagonal = active_group["diagonal"] if active_group else display_size
                possible_raw = (
                    self.db.get_gsm_possible_compatible_expanded(brand, _diagonal, device_id)
                    if _diagonal
                    else []
                )
                _suggest_title = "Можливі сумісні (однаковий бренд + діагональ)"

            async def _save_one_compat(compat_id, status, compat_variant=""):
                ok = await asyncio.to_thread(
                    self.db.save_gsm_compatible,
                    device_id, compat_id, status or "compatible",
                    variant_label=compat_variant,
                    accessory_type=accessory_type,
                )
                if ok:
                    await asyncio.to_thread(self._refresh_cached_detail, device_id, device)
                    await asyncio.to_thread(self._refresh_cached_detail, compat_id)  # bidirectional — other side too
                return ok

            def _add_compat(compat_id, status_dd, compat_variant=""):
                def _h(_):
                    async def _do():
                        if await _save_one_compat(compat_id, status_dd.value, compat_variant):
                            self._show_snack(self.t("gsm.compat_saved", "Сумісність збережено"), ft.Colors.GREEN)
                            await _reload()

                    page.run_task(_do)

                return _h

            # Expand devices with different-sized regional variants into separate rows
            possible_expanded: list[tuple[dict, str]] = []  # (device_dict, variant_label)
            for p in possible_raw:
                oid = str(p.get("id") or "")
                if oid in confirmed_ids:
                    continue
                variants_raw = p.get("regional_variants") or []
                sized_variants = [
                    v for v in (variants_raw if isinstance(variants_raw, list) else [])
                    if isinstance(v, dict)
                    and str(v.get("region") or "").strip()
                    and str(v.get("L") or "").strip()
                ]
                unique_lengths = {str(v.get("L") or "").strip() for v in sized_variants}
                if len(unique_lengths) <= 1:
                    # Single size or no variants — show as plain entry
                    possible_expanded.append((p, ""))
                else:
                    # Multiple body lengths — show one row per variant
                    for v in sized_variants:
                        region = str(v.get("region") or "").strip()
                        l_val = str(v.get("L") or "").strip()
                        possible_expanded.append((p, f"{region} ({l_val}мм)"))

            # Each suggestion gets its own checkbox + status dropdown, so the
            # user can tick several devices at once (some "Сумісні", some
            # "Частково сумісні") and submit them all in one click via
            # "Додати позначені" instead of clicking Add once per device.
            auto_rows = []
            _suggest_entries: list[tuple] = []  # (checkbox, other_id, status_dd, variant_label)
            for p, variant_label in possible_expanded:
                oid = str(p.get("id") or "")
                model_label = str(p.get("model_name") or "—")
                if variant_label:
                    model_label += f" ({variant_label})"
                dd = ft.Dropdown(
                    value="compatible",
                    dense=True,
                    width=160,
                    options=[
                        ft.DropdownOption(key=k, text=_STATUS_LABELS_MAP[k])
                        for k in _STATUS_OPTIONS
                    ],
                )
                cb = ft.Checkbox(value=False, scale=0.85, tooltip="Позначити для масового додавання")
                _to_save = _combine_label(_group_label, variant_label)
                _suggest_entries.append((cb, oid, dd, _to_save))
                auto_rows.append(
                    ft.Row(
                        [
                            cb,
                            ft.TextButton(
                                model_label,
                                on_click=_open_card(oid),
                                style=ft.ButtonStyle(
                                    padding=ft.padding.symmetric(horizontal=4, vertical=2)
                                ),
                            ),
                            dd,
                            ft.IconButton(
                                icon=ft.Icons.CHECK_CIRCLE_OUTLINE,
                                icon_color=_OK_DK,
                                tooltip="Додати тільки цей",
                                icon_size=18,
                                on_click=_add_compat(oid, dd, _to_save),
                            ),
                        ],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    )
                )

            def _add_selected(_):
                async def _do():
                    added = 0
                    for cb, oid, dd, variant_label in _suggest_entries:
                        if cb.value and await _save_one_compat(oid, dd.value, variant_label):
                            added += 1
                    if added:
                        self._show_snack(f"Додано {added} пристроїв", ft.Colors.GREEN)
                        await _reload()
                    else:
                        self._show_snack(self.t("gsm.nothing_selected", "Нічого не позначено"), ft.Colors.ORANGE)

                page.run_task(_do)

            add_selected_btn = ft.FilledButton(
                self.t("gsm.add_selected", "Додати позначені"),
                icon=ft.Icons.PLAYLIST_ADD_CHECK,
                on_click=_add_selected,
                visible=bool(auto_rows),
                style=ft.ButtonStyle(
                    bgcolor=_OK_DK,
                    padding=ft.padding.symmetric(horizontal=10, vertical=4),
                    text_style=ft.TextStyle(size=11),
                ),
            )

            # ── Smart filterable dropdown helper ─────────────────────────
            def _smart_dropdown(title: str, brand_filter: str | None):
                """Filterable list: shows all same-type devices by default, narrows as user types."""
                results_col = ft.Column(
                    [], spacing=0, tight=True, scroll=ft.ScrollMode.AUTO
                )

                def _build_item(m):
                    oid = str(m.get("id") or "")
                    label = f"{m.get('brand', '')} {m.get('gsm_name', '')}".strip()
                    dd = ft.Dropdown(
                        value="compatible",
                        dense=True,
                        width=150,
                        options=[
                            ft.DropdownOption(key=k, text=v)
                            for k, v in _STATUS_LABELS_MAP.items()
                        ],
                    )
                    return ft.Container(
                        content=ft.Row(
                            [
                                ft.TextButton(
                                    label,
                                    on_click=_open_card(oid),
                                    style=ft.ButtonStyle(
                                        padding=ft.padding.symmetric(
                                            horizontal=4, vertical=2
                                        )
                                    ),
                                    expand=True,
                                ),
                                dd,
                                ft.IconButton(
                                    icon=ft.Icons.ADD_CIRCLE_OUTLINE,
                                    icon_color=_ACCENT,
                                    tooltip=self.t("gsm.add_as_compatible", "Додати як сумісний"),
                                    icon_size=18,
                                    on_click=_add_compat(oid, dd, _group_label),
                                ),
                            ],
                            spacing=4,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
                        padding=ft.padding.symmetric(horizontal=4, vertical=2),
                    )

                def _refresh(q: str):
                    devs, _, _ = _get_cache()
                    q_low = q.strip().lower()
                    matches = []
                    for d in devs or []:
                        if str(d.get("id") or "") == device_id:
                            continue
                        if str(d.get("id") or "") in confirmed_ids:
                            continue
                        if (
                            brand_filter
                            and (d.get("brand") or "").lower() != brand_filter.lower()
                        ):
                            continue
                        if (
                            device_type
                            and (d.get("device_type") or "").lower() != device_type.lower()
                        ):
                            continue
                        if q_low and q_low not in (d.get("gsm_name") or "").lower():
                            continue
                        matches.append(d)
                        if len(matches) >= 20:
                            break
                    results_col.controls = (
                        [_build_item(m) for m in matches]
                        if matches
                        else [
                            ft.Text(
                                self.t("gsm.nothing_found", "Нічого не знайдено"), size=12, color=_MUTED, italic=True
                            )
                        ]
                    )
                    with contextlib.suppress(Exception):
                        results_col.update()

                search_tf = ft.TextField(
                    hint_text=self.t("gsm.compat_search_hint", "Почніть вводити назву…"),
                    dense=True,
                    expand=True,
                    border_radius=8,
                    text_size=12,
                    prefix_icon=ft.Icons.SEARCH,
                    on_change=lambda e: _refresh(e.control.value or ""),
                )

                # Load initial list immediately
                _refresh("")

                return ft.Column(
                    [
                        ft.Text(title, size=13, weight=ft.FontWeight.W_700),
                        search_tf,
                        ft.Container(
                            content=results_col,
                            height=160,
                            border=ft.border.all(1, _BORDER),
                            border_radius=8,
                            padding=ft.padding.symmetric(horizontal=4, vertical=4),
                            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                        ),
                    ],
                    spacing=6,
                    tight=True,
                )

            # ── Assemble SA edit UI ───────────────────────────────────────
            sections = [
                ft.Row(
                    [
                        ft.Text(_suggest_title, size=13, weight=ft.FontWeight.W_700, expand=True),
                        add_selected_btn,
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                (
                    ft.Column(auto_rows, spacing=4, tight=True)
                    if auto_rows
                    else ft.Text(
                        "Не знайдено пристроїв за цим критерієм.",
                        size=12,
                        color=_MUTED,
                    )
                ),
                ft.Divider(height=12),
                ft.Text(self.t("gsm.confirmed_compatible", "Підтверджені сумісні"), size=13, weight=ft.FontWeight.W_700),
                (
                    ft.Column(conf_rows, spacing=4, tight=True)
                    if conf_rows
                    else ft.Text("Поки немає підтверджених.", size=12, color=_MUTED)
                ),
                ft.Divider(height=12),
                _smart_dropdown(f"Вручну — той самий бренд ({brand})", brand_filter=brand),
                ft.Divider(height=12),
                _smart_dropdown("Вручну — будь-який бренд", brand_filter=None),
            ]
            return ft.Column(sections, spacing=8, tight=True)

        if not is_split:
            return _build_group_body(None)

        # ── Multi-size device: this card actually represents several
        # physically distinct devices (different body size and/or diagonal
        # per region) — compatibility must be picked per size group. ───────
        _key = (device_id, accessory_type)
        active_idx = [min(_LAST_COMPAT_GROUP_IDX.get(_key, 0), len(size_groups) - 1)]

        def _group_meta(g: dict) -> str:
            parts = [g.get("size_str") or "", g.get("diag_str") or ""]
            return "  •  ".join(p for p in parts if p)

        def _chip(i: int, g: dict) -> ft.Control:
            active = i == active_idx[0]
            return ft.Container(
                on_click=_select_group(i),
                ink=True,
                border_radius=8,
                padding=ft.padding.symmetric(horizontal=12, vertical=6),
                bgcolor=_ACCENT if active else ft.Colors.BLUE_GREY_50,
                content=ft.Column(
                    [
                        ft.Text(
                            f"Розмір {i + 1}: {_group_meta(g) or '—'}",
                            size=12,
                            weight=ft.FontWeight.W_600,
                            color=ft.Colors.WHITE if active else _TEXT,
                        ),
                        ft.Text(
                            g.get("label") or "",
                            size=10,
                            color=ft.Colors.WHITE70 if active else _MUTED,
                        ),
                    ],
                    spacing=0,
                    tight=True,
                ),
            )

        chip_row_ref = ft.Row(spacing=8, wrap=True)
        body_holder = ft.Column(spacing=0, tight=True)

        def _select_group(i: int):
            def _h(_):
                active_idx[0] = i
                _LAST_COMPAT_GROUP_IDX[_key] = i
                chip_row_ref.controls = [_chip(j, gg) for j, gg in enumerate(size_groups)]
                body_holder.controls = [_build_group_body(size_groups[i])]
                with contextlib.suppress(Exception):
                    chip_row_ref.update()
                with contextlib.suppress(Exception):
                    body_holder.update()

            return _h

        chip_row_ref.controls = [_chip(j, gg) for j, gg in enumerate(size_groups)]
        body_holder.controls = [_build_group_body(size_groups[active_idx[0]])]

        banner = ft.Container(
            padding=ft.padding.symmetric(horizontal=10, vertical=8),
            border_radius=8,
            bgcolor=ft.Colors.AMBER_50,
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.INFO_OUTLINE, size=16, color=ft.Colors.AMBER_800),
                    ft.Text(
                        "У цієї моделі кілька розмірів корпусу (регіональні варіанти). "
                        "Навіть різниця в 1-2 мм може означати, що чохол чи скло "
                        "підходять не для всіх розмірів — тому сумісність вказується "
                        "окремо для кожного розміру, обраного вкладкою нижче.",
                        size=11,
                        color=ft.Colors.AMBER_900,
                        expand=True,
                    ),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
        )

        return ft.Column(
            [banner, chip_row_ref, ft.Divider(height=8), body_holder],
            spacing=8,
            tight=True,
        )

    def _build_comments_section(self, device: dict, page, refresh_cb=None) -> ft.Control:
        device_id = str(device.get("id") or "")
        detail = self._get_or_fetch_device_detail(device_id, device)
        comment_data = detail.get("comment")

        rich_data = []
        comment_text = ""
        if comment_data:
            rich_data = comment_data.get("rich_data") or []
            comment_text = str(comment_data.get("comment_text") or "")

        # Render rich comment
        def _render_rich(segments: list) -> ft.Control:
            controls = []
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                seg_type = str(seg.get("type") or "text")
                text = str(seg.get("text") or "")
                if seg_type == "link":
                    dev_id = str(seg.get("device_id") or "")
                    controls.append(
                        ft.TextButton(
                            text,
                            on_click=lambda _, did=dev_id: self._open_linked_device(
                                page, did
                            ),
                            style=ft.ButtonStyle(
                                padding=ft.padding.symmetric(horizontal=2, vertical=0),
                                color=_ACCENT,
                            ),
                        )
                    )
                else:
                    controls.append(ft.Text(text, size=13))
            return (
                ft.Row(controls, wrap=True, spacing=2)
                if controls
                else ft.Text("", size=13)
            )

        if not comment_text and not rich_data and not self.is_superadmin:
            return ft.Container()

        # SA: show edit button
        def _open_editor(_):
            self._comment_editor_dialog(
                page, device_id, comment_text, rich_data, refresh_cb=refresh_cb
            )

        display = (
            _render_rich(rich_data)
            if rich_data
            else ft.Text(
                comment_text or "Немає коментаря",
                size=13,
                color=ft.Colors.BLUE_GREY_400 if not comment_text else _TEXT,
            )
        )

        controls = [
            ft.Text("Коментар", size=13, weight=ft.FontWeight.W_700),
            display,
        ]
        if self.is_superadmin:
            controls.append(
                ft.TextButton(
                    "Редагувати коментар",
                    icon=ft.Icons.EDIT_NOTE,
                    on_click=_open_editor,
                )
            )

        return ft.Column(controls, spacing=6, tight=True)

    # ------------------------------------------------------------------ #
    #  Regional Variants (Xiaomi only)                                    #
    # ------------------------------------------------------------------ #
    def _build_regional_variants_section(self, device: dict, page, refresh_cb=None) -> ft.Control:
        device_id = str(device.get("id") or "")
        brand = str(device.get("brand") or "").strip().lower()
        if brand != "xiaomi":
            return ft.Container(height=0)

        detail = self._get_or_fetch_device_detail(device_id, device)
        variants = detail.get("variants") or []
        can_edit = self.can_edit_xiaomi_sizes

        _CW = [110, 175, 227, 84]  # Region, Models, Size, Diagonal

        def _fmt_dims(v: dict) -> str:
            parts = [
                str(v.get(k, "")).strip().replace(".", ",") for k in ("L", "W", "T")
            ]
            parts = [p for p in parts if p]
            return "х".join(parts) if parts else "—"

        def _fmt_diag(v: dict) -> str:
            d = str(v.get("diagonal", "")).strip()
            return (d.replace(".", ",") + '"') if d else "—"

        def _hdr_row() -> ft.Container:
            return ft.Container(
                content=ft.Row(
                    [
                        ft.Container(
                            ft.Text(
                                "Регіон",
                                size=11,
                                weight=ft.FontWeight.W_700,
                                color=_MUTED,
                            ),
                            width=_CW[0],
                        ),
                        ft.Container(
                            ft.Text(
                                "Модель",
                                size=11,
                                weight=ft.FontWeight.W_700,
                                color=_MUTED,
                            ),
                            width=_CW[1],
                        ),
                        ft.Container(
                            ft.Text(
                                "Розмір",
                                size=11,
                                weight=ft.FontWeight.W_700,
                                color=_MUTED,
                            ),
                            width=_CW[2],
                        ),
                        ft.Container(
                            ft.Text(
                                "Діагональ",
                                size=11,
                                weight=ft.FontWeight.W_700,
                                color=_MUTED,
                            ),
                            width=_CW[3],
                        ),
                    ],
                    spacing=0,
                ),
                bgcolor=_SOFT,
                padding=ft.padding.symmetric(horizontal=8, vertical=5),
                border_radius=ft.border_radius.only(top_left=6, top_right=6),
            )

        _rows: list[dict] = [dict(v) for v in variants] if can_edit else variants

        def _persist_rows():
            clean = []
            for r in _rows:
                region = str(r.get("region", "")).strip()
                models = str(r.get("models", "")).strip()
                if not region and not models:
                    continue
                clean.append(
                    {
                        "region": region,
                        "models": models,
                        "L": str(r.get("L", "")).replace(",", ".").strip(),
                        "W": str(r.get("W", "")).replace(",", ".").strip(),
                        "T": str(r.get("T", "")).replace(",", ".").strip(),
                        "diagonal": str(r.get("diagonal", ""))
                        .replace(",", ".")
                        .strip(),
                        "source": str(r.get("source", "")).strip(),
                    }
                )

            async def _do():
                # Save silently in the background — do NOT call refresh_cb()
                # here: it rebuilds this row's TextField controls, which on
                # Tab (on_blur fires as focus leaves the field) destroyed the
                # control the user had just tabbed into mid-keystroke. The
                # in-memory cache sync below keeps other views correct
                # without touching what's currently being typed into.
                ok = await asyncio.to_thread(self.db.save_gsm_regional_variants, device_id, clean)
                if ok:
                    await asyncio.to_thread(self._refresh_cached_detail, device_id)
                else:
                    self._show_snack(self.t("gsm.err.save_failed", "Не вдалося зберегти"), ft.Colors.RED)

            page.run_task(_do)

        def _field_change(idx: int, key: str, value: str):
            _rows[idx][key] = value
            _persist_rows()

        def _data_row(idx: int, v: dict, is_last: bool) -> ft.Column:
            if can_edit:
                sep_style = ft.TextStyle(size=12, color=_MUTED)

                def _mk(key: str, hint: str, width: int) -> ft.TextField:
                    tf = ft.TextField(
                        value=str(v.get(key, "")).replace(",", ".").strip(),
                        hint_text=hint,
                        width=width,
                        dense=True,
                        keyboard_type=ft.KeyboardType.NUMBER,
                        content_padding=ft.padding.symmetric(horizontal=4, vertical=2),
                        text_size=12,
                    )
                    tf.on_submit = lambda e, i=idx, k=key: _field_change(i, k, e.control.value)
                    tf.on_blur = lambda e, i=idx, k=key: _field_change(i, k, e.control.value)
                    return tf

                size_cell = ft.Row(
                    [
                        _mk("L", "Дов.", 53),
                        ft.Text("х", style=sep_style),
                        _mk("W", "Шир.", 53),
                        ft.Text("х", style=sep_style),
                        _mk("T", "Тов.", 53),
                    ],
                    spacing=3,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
                diag_field = _mk("diagonal", '"', 60)
                diag_field.suffix = ft.Text('"')
                diag_cell = diag_field
                cell_align = ft.CrossAxisAlignment.CENTER
            else:
                size_cell = ft.Text(_fmt_dims(v), size=12)
                diag_cell = ft.Text(_fmt_diag(v), size=12)
                cell_align = ft.CrossAxisAlignment.START

            return ft.Column(
                [
                    ft.Container(
                        content=ft.Row(
                            [
                                ft.Container(
                                    ft.Text(
                                        str(v.get("region", "")).strip(),
                                        size=12,
                                        weight=ft.FontWeight.W_600,
                                    ),
                                    width=_CW[0],
                                ),
                                ft.Container(
                                    ft.Text(
                                        str(v.get("models", "")).strip(),
                                        size=12,
                                        no_wrap=False,
                                        selectable=True,
                                    ),
                                    width=_CW[1],
                                ),
                                ft.Container(size_cell, width=_CW[2]),
                                ft.Container(diag_cell, width=_CW[3]),
                            ],
                            spacing=0,
                            vertical_alignment=cell_align,
                        ),
                        padding=ft.padding.symmetric(horizontal=8, vertical=6),
                    ),
                    *([] if is_last else [ft.Divider(height=1, color=_BORDER)]),
                ],
                spacing=0,
                tight=True,
            )

        if not variants:
            if not can_edit:
                return ft.Container(height=0)
            body = ft.Text("Немає даних", size=12, color=ft.Colors.BLUE_GREY_400)
        else:
            rows = [
                _data_row(i, v, i == len(_rows) - 1) for i, v in enumerate(_rows)
            ]
            body = ft.Container(
                content=ft.Column([_hdr_row()] + rows, spacing=0, tight=True),
                border=ft.border.all(1, _BORDER),
                border_radius=6,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            )

        controls: list = [
            ft.Container(
                ft.Text(
                    "Регіональні варіанти",
                    size=11,
                    weight=ft.FontWeight.W_700,
                    color=ft.Colors.BLUE_700,
                ),
                border=ft.border.only(bottom=ft.BorderSide(1, ft.Colors.BLUE_200)),
                padding=ft.padding.only(bottom=4),
                margin=ft.margin.only(bottom=4),
            ),
            body,
        ]
        if can_edit:
            controls.append(
                ft.TextButton(
                    "Редагувати варіанти",
                    icon=ft.Icons.EDIT_NOTE,
                    on_click=lambda _: self._regional_variants_editor_dialog(
                        page, device_id, list(_rows), refresh_cb=refresh_cb
                    ),
                )
            )
        controls.append(ft.Divider(height=14))
        return ft.Column(controls, spacing=6, tight=True)

    def _regional_variants_editor_dialog(
        self, page, device_id: str, variants: list, refresh_cb=None
    ):
        """SA dialog to add/edit/delete Xiaomi regional variant rows.

        refresh_cb: refreshes the regional-variants section in place within
        the already-open device modal after saving — the modal must stay
        open, not close and reopen a fresh copy."""
        _rows: list[dict] = [dict(v) for v in variants] if variants else []

        _rows_col = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, height=360)

        def _set(i: int, k: str, v: str):
            _rows[i][k] = v

        def _build_edit_row(idx: int) -> ft.Container:
            v = _rows[idx]
            f_region = ft.TextField(
                value=str(v.get("region", "")),
                width=100,
                dense=True,
                hint_text="Регіон",
                content_padding=ft.padding.symmetric(horizontal=6, vertical=4),
                on_change=lambda e, i=idx: _set(i, "region", e.control.value),
            )
            f_models = ft.TextField(
                value=str(v.get("models", "")),
                width=155,
                dense=True,
                multiline=True,
                min_lines=1,
                max_lines=4,
                hint_text="Моделі (Ctrl+Enter = новий рядок)",
                content_padding=ft.padding.symmetric(horizontal=6, vertical=4),
                on_change=lambda e, i=idx: _set(i, "models", e.control.value),
            )
            f_L = ft.TextField(
                value=str(v.get("L", "")).replace(",", "."),
                width=68,
                dense=True,
                hint_text="Дов.",
                keyboard_type=ft.KeyboardType.NUMBER,
                content_padding=ft.padding.symmetric(horizontal=4, vertical=4),
                on_change=lambda e, i=idx: _set(i, "L", e.control.value),
            )
            f_W = ft.TextField(
                value=str(v.get("W", "")).replace(",", "."),
                width=68,
                dense=True,
                hint_text="Шир.",
                keyboard_type=ft.KeyboardType.NUMBER,
                content_padding=ft.padding.symmetric(horizontal=4, vertical=4),
                on_change=lambda e, i=idx: _set(i, "W", e.control.value),
            )
            f_T = ft.TextField(
                value=str(v.get("T", "")).replace(",", "."),
                width=68,
                dense=True,
                hint_text="Тов.",
                keyboard_type=ft.KeyboardType.NUMBER,
                content_padding=ft.padding.symmetric(horizontal=4, vertical=4),
                on_change=lambda e, i=idx: _set(i, "T", e.control.value),
            )
            f_diag = ft.TextField(
                value=str(v.get("diagonal", "")).replace(",", "."),
                width=62,
                dense=True,
                hint_text='"',
                suffix=ft.Text('"'),
                keyboard_type=ft.KeyboardType.NUMBER,
                content_padding=ft.padding.symmetric(horizontal=4, vertical=4),
                on_change=lambda e, i=idx: _set(i, "diagonal", e.control.value),
            )

            def _del(_, i=idx):
                _rows.pop(i)
                _rebuild()

            sep_style = ft.TextStyle(size=13, color=_MUTED, weight=ft.FontWeight.W_700)
            return ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                f_region,
                                f_models,
                                ft.Row(
                                    [
                                        f_L,
                                        ft.Text("х", style=sep_style),
                                        f_W,
                                        ft.Text("х", style=sep_style),
                                        f_T,
                                    ],
                                    spacing=3,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                                f_diag,
                                ft.IconButton(
                                    icon=ft.Icons.DELETE_OUTLINE,
                                    icon_color=ft.Colors.RED_400,
                                    icon_size=18,
                                    tooltip="Видалити рядок",
                                    on_click=_del,
                                ),
                            ],
                            spacing=6,
                            vertical_alignment=ft.CrossAxisAlignment.START,
                        ),
                        ft.Divider(height=8, color=_BORDER),
                    ],
                    spacing=4,
                    tight=True,
                ),
                padding=ft.padding.only(top=6),
            )

        def _rebuild():
            _rows_col.controls = [_build_edit_row(i) for i in range(len(_rows))]
            with contextlib.suppress(Exception):
                _rows_col.update()

        def _add_row(_):
            _rows.append(
                {"region": "", "models": "", "L": "", "W": "", "T": "", "diagonal": ""}
            )
            _rebuild()

        save_btn = ft.FilledButton("Зберегти", icon=ft.Icons.SAVE)

        def _reset_save_btn():
            save_btn.text = "Зберегти"
            save_btn.disabled = False
            with contextlib.suppress(Exception):
                save_btn.update()

        def _save(_):
            clean = []
            for v in _rows:
                region = str(v.get("region", "")).strip()
                models = str(v.get("models", "")).strip()
                if not region and not models:
                    continue
                clean.append(
                    {
                        "region": region,
                        "models": models,
                        "L": str(v.get("L", "")).replace(",", ".").strip(),
                        "W": str(v.get("W", "")).replace(",", ".").strip(),
                        "T": str(v.get("T", "")).replace(",", ".").strip(),
                        "diagonal": str(v.get("diagonal", ""))
                        .replace(",", ".")
                        .strip(),
                        "source": str(v.get("source", "")).strip(),
                    }
                )
            save_btn.text = "Збереження..."
            save_btn.disabled = True
            with contextlib.suppress(Exception):
                save_btn.update()

            async def _do():
                ok = await asyncio.to_thread(self.db.save_gsm_regional_variants, device_id, clean)
                if ok:
                    await asyncio.to_thread(self._refresh_cached_detail, device_id)
                    page.pop_dialog()
                    self._show_snack(self.t("gsm.variants_saved", "Варіанти збережено"), ft.Colors.GREEN)
                    if refresh_cb is not None:
                        refresh_cb()
                else:
                    _reset_save_btn()
                    self._show_snack(self.t("gsm.err.save_failed", "Не вдалося зберегти"), ft.Colors.RED)

            page.run_task(_do)

        save_btn.on_click = _save

        _rebuild()

        def _variants_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, "Регіональні варіанти Xiaomi", [
                self.t("gsm.variants_help.step1", "1. Додайте рядки для кожного регіонального варіанта моделі Xiaomi (код регіону + суфікс моделі)."),
                self.t("gsm.variants_help.step2", "2. Натисніть «Зберегти» для застосування."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                [
                    ft.Text(
                        "Регіональні варіанти Xiaomi",
                        size=15,
                        weight=ft.FontWeight.W_700,
                    ),
                    ft.Container(expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_variants_help),
                    ft.TextButton(
                        "+ Додати рядок", icon=ft.Icons.ADD, on_click=_add_row
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            content=ft.Container(
                width=680,
                content=ft.Column(
                    [
                        ft.Container(
                            content=ft.Row(
                                [
                                    ft.Container(
                                        ft.Text(
                                            "Регіон",
                                            size=11,
                                            weight=ft.FontWeight.W_700,
                                        ),
                                        width=100,
                                    ),
                                    ft.Container(
                                        ft.Text(
                                            "Модель",
                                            size=11,
                                            weight=ft.FontWeight.W_700,
                                        ),
                                        width=155,
                                    ),
                                    ft.Container(
                                        ft.Text(
                                            "Розмір (Д × Ш × Т)",
                                            size=11,
                                            weight=ft.FontWeight.W_700,
                                        ),
                                        width=224,
                                    ),
                                    ft.Container(
                                        ft.Text(
                                            'Діагональ "',
                                            size=11,
                                            weight=ft.FontWeight.W_700,
                                        ),
                                        width=62,
                                    ),
                                ],
                                spacing=6,
                            ),
                            bgcolor=_SOFT,
                            padding=ft.padding.symmetric(horizontal=6, vertical=5),
                            border_radius=ft.border_radius.only(
                                top_left=4, top_right=4
                            ),
                        ),
                        _rows_col,
                    ],
                    spacing=0,
                    tight=True,
                ),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: page.pop_dialog()),
                save_btn,
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.open(dlg)

    def _open_linked_device(self, page, device_id: str):
        async def _do():
            dev = await asyncio.to_thread(self.db.get_gsm_merged_device_by_id, device_id)
            if dev:
                self._device_detail_modal(page, dev)

        page.run_task(_do)

    def _comment_editor_dialog(
        self, page, device_id: str, current_text: str, current_rich: list, refresh_cb=None
    ):
        """SA dialog to edit rich comment (text + device hyperlinks).

        refresh_cb: refreshes the comments section in place within the
        already-open device modal after saving — the modal must stay open,
        not close and reopen a fresh copy."""

        # Simple approach: edit as plain text; to add a link, user types
        # [LinkText](device_id) syntax which we parse to rich_data segments.
        editor = ft.TextField(
            value=current_text,
            multiline=True,
            min_lines=4,
            max_lines=8,
            label="Коментар (для посилання: [Назва пристрою](device_id))",
            expand=True,
        )

        def _parse_rich(text: str) -> list:
            segments = []
            pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
            pos = 0
            for m in pattern.finditer(text):
                if m.start() > pos:
                    segments.append({"type": "text", "text": text[pos : m.start()]})
                segments.append(
                    {"type": "link", "text": m.group(1), "device_id": m.group(2)}
                )
                pos = m.end()
            if pos < len(text):
                segments.append({"type": "text", "text": text[pos:]})
            return segments

        save_btn = ft.FilledButton("Зберегти", icon=ft.Icons.SAVE)

        def _reset_save_btn():
            save_btn.text = "Зберегти"
            save_btn.disabled = False
            with contextlib.suppress(Exception):
                save_btn.update()

        def _save(_):
            new_text = str(editor.value or "").strip()
            rich = _parse_rich(new_text)
            save_btn.text = "Збереження..."
            save_btn.disabled = True
            with contextlib.suppress(Exception):
                save_btn.update()

            async def _do():
                ok = await asyncio.to_thread(self.db.save_gsm_device_comment, device_id, new_text, rich)
                if ok:
                    await asyncio.to_thread(self._refresh_cached_detail, device_id)
                    page.pop_dialog()
                    self._show_snack(self.t("gsm.comment_saved", "Коментар збережено"), ft.Colors.GREEN)
                    if refresh_cb is not None:
                        refresh_cb()
                else:
                    _reset_save_btn()
                    self._show_snack(self.t("gsm.err.save_failed", "Не вдалося зберегти"), ft.Colors.RED)

            page.run_task(_do)

        save_btn.on_click = _save

        def _comment_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, "Редагування коментаря", [
                self.t("gsm.comment_help.step1", "1. Введіть коментар про пристрій. Для посилання на інший пристрій: [Назва пристрою](device_id)."),
                self.t("gsm.comment_help.step2", "2. Натисніть «Зберегти»."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Text("Редагування коментаря", size=16, weight=ft.FontWeight.W_700, expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_comment_help),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
            content=ft.Container(
                width=560,
                content=ft.Column(
                    [
                        ft.Text(
                            "Для посилання на пристрій використовуйте формат: [Назва пристрою](device_id)",
                            size=11,
                            color=ft.Colors.BLUE_GREY_600,
                        ),
                        editor,
                    ],
                    spacing=10,
                ),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: page.pop_dialog()),
                save_btn,
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    def _manual_ek_url_dialog(self, page, device_id: str, current_url: str, refresh_cb=None):
        """SA dialog: manually pin an E-Katalog product URL to a GSM Arena
        device. Used when auto-matching found nothing (or the wrong page) —
        the page is fetched and applied immediately on save (see
        fetch_and_apply_manual_ek_url), so the card shows the right product
        without waiting for the next scheduled parser run. The saved URL
        also becomes the pinned override for all future parser runs."""
        editor = ft.TextField(
            value=current_url,
            label="Посилання на Єкаталог",
            hint_text="https://ek.ua/ua/...htm",
            expand=True,
        )

        save_btn = ft.FilledButton("Зберегти", icon=ft.Icons.SAVE)

        def _reset_save_btn():
            save_btn.text = "Зберегти"
            save_btn.disabled = False
            with contextlib.suppress(Exception):
                save_btn.update()

        def _save(_):
            new_url = str(editor.value or "").strip()
            save_btn.text = "Збереження..."
            save_btn.disabled = True
            with contextlib.suppress(Exception):
                save_btn.update()

            async def _do():
                ok = await asyncio.to_thread(
                    self.db.save_gsm_device_manual_ek_url, device_id, new_url
                )
                if not ok:
                    _reset_save_btn()
                    self._show_snack(self.t("gsm.err.save_failed", "Не вдалося зберегти"), ft.Colors.RED)
                    return

                if not new_url:
                    await asyncio.to_thread(self._refresh_cached_detail, device_id)
                    page.pop_dialog()
                    self._show_snack(
                        self.t("gsm.manual_ek_url_cleared", "Ручне посилання прибрано"),
                        ft.Colors.GREEN,
                    )
                    if refresh_cb is not None:
                        refresh_cb(new_url)
                    return

                # Take the exact product at this URL right away — no waiting
                # for the next scheduled parser run, no name/type matching
                # or comparison against the GSM Arena device (that's the
                # whole point of pinning a URL by hand: trust it as-is).
                save_btn.text = "Завантаження товару…"
                with contextlib.suppress(Exception):
                    save_btn.update()
                fetch_ok, fetch_msg, ek_dev_id = await asyncio.to_thread(
                    fetch_and_apply_manual_ek_url, self.db, device_id, new_url
                )
                await asyncio.to_thread(self._refresh_cached_detail, device_id)
                page.pop_dialog()

                if not fetch_ok:
                    self._show_snack(
                        self.t(
                            "gsm.manual_ek_url_fetch_failed",
                            "Посилання збережено, але завантажити товар не вдалося: {err}",
                        ).format(err=fetch_msg),
                        ft.Colors.ORANGE,
                    )
                    if refresh_cb is not None:
                        refresh_cb(new_url)
                    return

                # Patch the results-table row + in-memory cache/on-disk
                # snapshot with the freshly fetched EK match, then reopen
                # the card so every field (name, price, specs) reflects it
                # — same in-place-update pattern _confirm_remove_ek_match
                # uses to clear a match, just adding data instead.
                ek_row = await asyncio.to_thread(self.db.get_gsm_device_by_id, ek_dev_id)
                global _DEVICE_CACHE
                updated_merged = None
                with _CACHE_LOCK:
                    if _DEVICE_CACHE is not None:
                        for d in _DEVICE_CACHE:
                            if str(d.get("id") or "") == device_id:
                                d["ek_id"] = ek_dev_id
                                d["ek_name"] = fetch_msg
                                d["ek_url"] = new_url
                                d["ek_specs"] = (ek_row or {}).get("specs") or {}
                                updated_merged = dict(d)
                                break
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(_save_cache_to_disk)
                if updated_merged is not None:
                    self._update_device_row_in_place(
                        device_id, str(updated_merged.get("brand") or ""), updated_merged
                    )
                self._show_snack(
                    self.t("gsm.manual_ek_url_fetched", "Товар ЄК завантажено: {name}").format(name=fetch_msg),
                    ft.Colors.GREEN,
                )
                if updated_merged is not None and self._active_device_dialog is not None:
                    self._device_detail_modal(page, updated_merged)

            page.run_task(_do)

        save_btn.on_click = _save

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Ручне посилання на Єкаталог", size=16, weight=ft.FontWeight.W_700),
            content=ft.Container(
                width=520,
                content=ft.Column(
                    [
                        ft.Text(
                            "Якщо автопошук не знайшов товар (або знайшов не той),"
                            " вставте сюди правильне посилання — товар підвантажиться"
                            " одразу після збереження, без очікування наступного парсингу.",
                            size=11,
                            color=ft.Colors.BLUE_GREY_600,
                        ),
                        editor,
                    ],
                    spacing=10,
                ),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: page.pop_dialog()),
                save_btn,
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    def _merge_devices_dialog(self, page, device_id: str, is_gsm_side: bool, seed_query: str):
        """SA action: manually link a standalone GSM Arena row with a
        standalone E-Katalog row that are actually the same device but
        auto-matching missed it (see _link_new_gsm_device_to_standalone_ek
        in gsm_parser_runner.py — the automatic version of this same
        merge). is_gsm_side=True means THIS card is the GSM Arena row and
        we're searching for its E-Katalog counterpart; False means the
        reverse (this card IS the standalone E-Katalog row)."""
        opposite_source = "E-Katalog" if is_gsm_side else "GSM Arena"
        search_field = ft.TextField(
            value=seed_query, label="Пошук пристрою", expand=True, autofocus=True
        )
        results_col = ft.Column([], spacing=2, scroll=ft.ScrollMode.AUTO, height=260, tight=True)

        def _row_label(r: dict) -> str:
            return f"{r.get('brand') or ''} {r.get('model_name') or ''}".strip()

        async def _do_merge(other_id: str, other_label: str):
            gsm_id_arg = device_id if is_gsm_side else other_id
            ek_id_arg = other_id if is_gsm_side else device_id
            ok, msg = await asyncio.to_thread(
                self.db.merge_gsm_ek_devices, gsm_id_arg, ek_id_arg
            )
            with contextlib.suppress(Exception):
                page.pop_dialog()
            self._show_snack(msg, ft.Colors.GREEN if ok else ft.Colors.RED)
            if ok:
                await asyncio.to_thread(self._refresh_cached_detail, device_id)
                with contextlib.suppress(Exception):
                    self._refresh_table(rebuild_filters=False)

        async def _do_search(_e=None):
            q = (search_field.value or "").strip()
            rows = await asyncio.to_thread(
                self.db.find_standalone_gsm_devices, opposite_source, q
            )
            results_col.controls.clear()
            if not rows:
                results_col.controls.append(
                    ft.Text("Нічого не знайдено", size=12, color=_MUTED)
                )
            for r in rows:
                rid = str(r.get("id") or "")
                label = _row_label(r) or rid
                is_matched = bool(r.get("matched"))

                def _pick(_e2, _rid=rid, _label=label):
                    page.run_task(_do_merge, _rid, _label)

                results_col.controls.append(
                    ft.TextButton(
                        content=ft.Row(
                            [
                                ft.Text(label, size=12, expand=True),
                                ft.Text(
                                    "вже прив'язано — перепризначити",
                                    size=9,
                                    color=ft.Colors.ORANGE_700,
                                    italic=True,
                                )
                                if is_matched
                                else ft.Container(width=0),
                            ],
                            spacing=6,
                        ),
                        on_click=_pick,
                        style=ft.ButtonStyle(
                            padding=ft.padding.symmetric(horizontal=8, vertical=6),
                            alignment=ft.Alignment(-1, 0),
                        ),
                    )
                )
            with contextlib.suppress(Exception):
                results_col.update()

        search_field.on_submit = lambda e: page.run_task(_do_search)
        search_btn = ft.IconButton(
            icon=ft.Icons.SEARCH, tooltip="Шукати", on_click=lambda e: page.run_task(_do_search)
        )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(f"Об'єднати з {opposite_source}", size=15, weight=ft.FontWeight.W_700),
            content=ft.Container(
                width=380,
                content=ft.Column(
                    [
                        ft.Text(
                            "Якщо це той самий пристрій, що автопошук не з'єднав —"
                            " знайдіть його нижче і виберіть, щоб об'єднати одразу.",
                            size=11,
                            color=ft.Colors.BLUE_GREY_600,
                        ),
                        ft.Row([search_field, search_btn]),
                        ft.Divider(height=1),
                        results_col,
                    ],
                    spacing=8,
                    tight=True,
                ),
            ),
            actions=[ft.TextButton("Закрити", on_click=lambda _: page.pop_dialog())],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)
        page.run_task(_do_search)

    def _confirm_remove_ek_match(self, page, device_id: str, ek_id: str):
        """SA action: delete only the E-Katalog side of a merged device (the
        GSM Arena row stays untouched). Use when auto-matching picked the
        wrong product — the next parser run will search again from scratch."""

        def _do_remove(_):
            page.pop_dialog()

            async def _do():
                ok = await asyncio.to_thread(self.db.delete_gsm_device, ek_id)
                if ok:
                    await asyncio.to_thread(self._refresh_cached_detail, device_id)

                    # Clear the EK side of the merged device in the in-memory
                    # table cache + on-disk snapshot, and patch the visible
                    # row in place — no full table reload/reset needed.
                    global _DEVICE_CACHE
                    updated_merged = None
                    with _CACHE_LOCK:
                        if _DEVICE_CACHE is not None:
                            for d in _DEVICE_CACHE:
                                if str(d.get("id") or "") == device_id:
                                    d["ek_id"] = None
                                    d["ek_name"] = None
                                    d["ek_url"] = None
                                    d["ek_specs"] = {}
                                    updated_merged = dict(d)
                                    break
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(_save_cache_to_disk)
                    if updated_merged is not None:
                        self._update_device_row_in_place(
                            device_id, str(updated_merged.get("brand") or ""), updated_merged
                        )

                    # ek_url/ek_specs/etc. shown in the still-open card are now
                    # stale — close it too, rather than try to patch every field.
                    if self._active_device_dialog is not None:
                        with contextlib.suppress(Exception):
                            page.pop_dialog()
                        self._active_device_dialog = None
                    self._show_snack(
                        self.t(
                            "gsm.ek_match_removed",
                            "Товар ЄК прибрано — наступний парсинг спробує знайти знову",
                        ),
                        ft.Colors.GREEN,
                    )
                else:
                    self._show_snack(self.t("gsm.err.save_failed", "Не вдалося зберегти"), ft.Colors.RED)

            page.run_task(_do)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Прибрати товар ЄК?", size=16, weight=ft.FontWeight.W_700),
            content=ft.Text(
                "GSM Arena дані залишаться без змін. Товар E-Katalog буде видалено,"
                " і при наступному парсингу парсер спробує знайти правильний"
                " відповідник ще раз.",
                size=12,
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: page.pop_dialog()),
                ft.FilledButton(
                    "Прибрати",
                    icon=ft.Icons.DELETE_OUTLINE,
                    style=ft.ButtonStyle(bgcolor=ft.Colors.RED_400, color=ft.Colors.WHITE),
                    on_click=_do_remove,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    def _device_detail_modal(self, page, merged: dict):
        # Only one device-detail modal is ever open at a time. Close any
        # already-open one first — covers navigating to a different device's
        # card (this one closes, the other opens) and is a safety net against
        # any leftover code path that might otherwise call this again for the
        # same device and stack a duplicate on top.
        if self._active_device_dialog is not None:
            with contextlib.suppress(Exception):
                page.pop_dialog()
            self._active_device_dialog = None

        # Support both merged (new) and flat (old/linked) dict formats
        if "gsm_specs" in merged:
            gs = merged.get("gsm_specs") or {}
            es = merged.get("ek_specs") or {}
            gsm_name = str(merged.get("gsm_name") or "—")
            ek_name = str(merged.get("ek_name") or "")
            device_id = str(merged.get("id") or "")
            image_url = str(merged.get("image_url") or "")
            gsm_url = str(merged.get("gsm_url") or "")
            ek_url = str(es.get("ek_url") or merged.get("ek_url") or "")
            ek_id = str(merged.get("ek_id") or "")
            brand = str(merged.get("brand") or "—")
        else:
            # Flat legacy format (from _open_linked_device)
            flat = merged.get("specs") or {}
            gs, es = flat, {}
            gsm_name = str(merged.get("model_name") or "—")
            ek_name = ""
            device_id = str(merged.get("id") or "")
            image_url = str(merged.get("image_url") or "")
            gsm_url = str(merged.get("source_url") or "")
            ek_url = ""
            ek_id = ""
            brand = str(merged.get("brand") or "—")

        # Adapter dict for compatible/comments sections (expect flat-style keys)
        _compat_dev = {
            "id": device_id,
            "brand": brand,
            "device_type": str(
                merged.get("device_type") or gs.get("device_type") or ""
            ),
            "specs": {**gs, "display_size": gs.get("display_size", "")},
            "model_name": gsm_name,
        }

        def _open_single_photo(url: str):
            if not url:
                return
            _dw = (getattr(page, "window_width", None) or 1200) * 0.86
            _dh = (getattr(page, "window_height", None) or 900) * 0.90
            _dlg_ref = [None]
            _prev_kb_single = [getattr(page, "on_keyboard_event", None)]

            def _close_dlg(_=None):
                page.on_keyboard_event = _prev_kb_single[0]
                with contextlib.suppress(Exception):
                    page.pop_dialog()

            page.on_keyboard_event = lambda e: (
                _close_dlg() if e.key == "Escape" else None
            )

            _dlg_ref[0] = ft.AlertDialog(
                content=ft.Stack(
                    [
                        ft.Container(
                            content=ft.Image(src=url, fit=ft.BoxFit.CONTAIN),
                            width=_dw,
                            height=_dh,
                            bgcolor="#111111",
                            alignment=ft.Alignment(0, 0),
                        ),
                        ft.Container(
                            content=ft.IconButton(
                                icon=ft.Icons.CLOSE,
                                icon_color=ft.Colors.WHITE,
                                icon_size=20,
                                on_click=_close_dlg,
                                style=ft.ButtonStyle(
                                    bgcolor=ft.Colors.with_opacity(
                                        0.55, ft.Colors.BLACK
                                    ),
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
                on_dismiss=lambda _: setattr(
                    page, "on_keyboard_event", _prev_kb_single[0]
                ),
            )
            page.open(_dlg_ref[0])

        # Shared mutable list of all photo URLs (main + extras, up to 7)
        _all_photos: list = [image_url] if image_url else []

        def _photo_gallery(start_idx: int = 0):
            """Full-screen gallery dialog with left/right arrow navigation."""
            urls = _all_photos
            if not urls:
                return
            _idx = [max(0, min(start_idx, len(urls) - 1))]
            _dw = (getattr(page, "window_width", None) or 1200) * 0.88
            _dh = (getattr(page, "window_height", None) or 900) * 0.90
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
            _prev = ft.IconButton(
                icon=ft.Icons.CHEVRON_LEFT,
                icon_color=ft.Colors.WHITE,
                icon_size=34,
                on_click=lambda _: _go(-1),
                visible=len(urls) > 1,
                style=_arrow_style,
            )
            _next = ft.IconButton(
                icon=ft.Icons.CHEVRON_RIGHT,
                icon_color=ft.Colors.WHITE,
                icon_size=34,
                on_click=lambda _: _go(1),
                visible=len(urls) > 1,
                style=_arrow_style,
            )

            _gal_dlg_ref = [None]
            _prev_kb_gal = [getattr(page, "on_keyboard_event", None)]

            def _close_gal(_=None):
                page.on_keyboard_event = _prev_kb_gal[0]
                with contextlib.suppress(Exception):
                    page.pop_dialog()

            def _on_gal_kb(e: ft.KeyboardEvent):
                if e.key == "Arrow Left":
                    _go(-1)
                elif e.key == "Arrow Right":
                    _go(1)
                elif e.key == "Escape":
                    _close_gal()

            page.on_keyboard_event = _on_gal_kb

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
                                            _prev,
                                            ft.Container(
                                                content=_gal_img,
                                                expand=True,
                                                alignment=ft.Alignment(0, 0),
                                            ),
                                            _next,
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
                                    bgcolor=ft.Colors.with_opacity(
                                        0.55, ft.Colors.BLACK
                                    ),
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
                on_dismiss=lambda _: setattr(
                    page, "on_keyboard_event", _prev_kb_gal[0]
                ),
            )
            page.open(_gal_dlg_ref[0])

        # Thumbnail row — populated asynchronously with up to 6 extra photos
        _thumbs_row = ft.Row(
            controls=[
                ft.ProgressRing(
                    width=16, height=16, stroke_width=2, color=ft.Colors.BLUE_GREY_300
                )
            ],
            spacing=4,
            scroll=ft.ScrollMode.AUTO,
        )

        photo_w, photo_h = 140, 160
        _local_main_photo = _photo_cache_path(device_id) if device_id else ""
        _main_img_src = (
            _local_main_photo
            if _local_main_photo and os.path.exists(_local_main_photo)
            else (image_url or "")
        )
        _main_img = ft.Image(
            src=_main_img_src,
            width=photo_w,
            height=photo_h,
            fit=ft.BoxFit.CONTAIN,
            border_radius=8,
        )
        _main_photo_ctrl = ft.Container(
            content=(
                _main_img
                if _main_img_src
                else ft.Container(
                    width=photo_w,
                    height=photo_h,
                    bgcolor=ft.Colors.BLUE_GREY_50,
                    border_radius=8,
                    alignment=ft.Alignment(0, 0),
                    content=ft.Icon(
                        ft.Icons.PHONE_ANDROID, size=52, color=ft.Colors.BLUE_GREY_200
                    ),
                )
            ),
            on_click=(lambda _: _photo_gallery(0)) if image_url else None,
            ink=bool(image_url),
        )
        photo = ft.Column(
            [_main_photo_ctrl, _thumbs_row],
            spacing=6,
            tight=True,
        )

        def _srow(label: str, value, suffix="") -> ft.Row | None:
            v = str(value or "").strip()
            if not v:
                return None
            return ft.Row(
                [
                    ft.Container(
                        ft.Text(
                            label,
                            size=11,
                            color=ft.Colors.BLUE_GREY_600,
                            weight=ft.FontWeight.W_600,
                        ),
                        width=145,
                    ),
                    ft.Text(v + suffix, size=12, expand=True, no_wrap=False),
                ],
                spacing=6,
            )

        def _section_title(text: str) -> ft.Container:
            return ft.Container(
                ft.Text(
                    text, size=11, weight=ft.FontWeight.W_700, color=ft.Colors.BLUE_700
                ),
                border=ft.border.only(bottom=ft.BorderSide(1, ft.Colors.BLUE_200)),
                padding=ft.padding.only(bottom=4),
                margin=ft.margin.only(bottom=4),
            )

        # ---- GSM Arena tab ----
        net_label = str(gs.get("network_label") or "").strip()
        net_full = str(gs.get("network_full") or "").strip()
        net_display = (
            f"{net_label} ({net_full})"
            if net_full and net_full != net_label
            else net_label or net_full
        )

        gsm_rows = [
            r
            for r in [
                _srow("Бренд", brand),
                _srow(
                    "Тип пристрою", merged.get("device_type") or gs.get("device_type")
                ),
                _srow("Мережа", net_display),
                _srow("Рік", gs.get("year")),
                _srow("Анонс", gs.get("announced")),
                _srow("Реліз", gs.get("released")),
                _srow("Розмір", _fmt_dims(gs) or gs.get("dimensions")),
                _srow("Діагональ", gs.get("display_size"), " дюйм"),
                _srow("Вага", gs.get("weight")),
                _srow("Операційна система", gs.get("os")),
                _srow("Процесор", gs.get("chipset")),
                _srow("Пам'ять", gs.get("memory")),
                _srow("Батарея", gs.get("battery")),
                _srow("Ціна ($)", gs.get("price_gsm")),
                _srow("Ціна (UAH)", gs.get("price_uah")),
            ]
            if r
        ]

        gsm_link_btn = ft.TextButton(
            "Відкрити на GSM Arena",
            icon=ft.Icons.OPEN_IN_NEW,
            on_click=lambda _: page.launch_url(gsm_url) if gsm_url else None,
            disabled=not gsm_url,
        )

        # ---- Єкаталог tab ----
        vars_ = es.get("variations") or {}
        price_lo = str(es.get("price_min") or es.get("price_low") or "").strip()
        price_hi = str(es.get("price_max") or "").strip()

        def _pfmt(v):
            try:
                return f"{float(v):,.0f} ₴".replace(",", " ")
            except Exception:
                return v

        ek_rows = [
            r
            for r in [
                _srow("Назва ЄК", ek_name or gsm_name),
                _srow("Мережа", es.get("target_net")),
                _srow("Дата додавання", es.get("date_added")),
                _srow("Розмір", _fmt_dims(es) or es.get("dimensions")),
                _srow("Діагональ", es.get("display"), " дюйм"),
                _srow("Вага", es.get("weight")),
                _srow("Операційна система", es.get("os")),
                _srow("Процесор", es.get("cpu")),
                _srow("Пам'ять", es.get("memory")),
                _srow("Батарея", es.get("battery")),
                _srow("Статус", es.get("status")),
                _srow("Ціна нижча", _pfmt(price_lo) if price_lo else None),
                _srow("Ціна вища", _pfmt(price_hi) if price_hi else None),
            ]
            if r
        ]

        # Variations table — AP=назва, AQ=колір/хар., AR=ціна нижча, AS=ціна вища, AT=магазини
        def _split_var(key: str) -> list[str]:
            return [
                x.strip() for x in str(vars_.get(key) or "").split("\n") if x.strip()
            ]

        _var_ap = _split_var("AP")  # variant names
        _var_aq = _split_var("AQ")  # colors / characteristics
        _var_ar = _split_var("AR")  # price low
        _var_as = _split_var("AS")  # price high
        _var_at = _split_var("AT")  # shops count

        def _loading_placeholder() -> ft.Container:
            return ft.Container(
                content=ft.Row(
                    [
                        ft.ProgressRing(width=16, height=16, stroke_width=2),
                        ft.Text("Завантаження…", size=11, color=_MUTED),
                    ],
                    spacing=8,
                ),
                height=36,
            )

        # Variations DataTable can run to dozens of rows (5 controls each) for
        # popular models with lots of EK color/shop combos — building it is
        # deferred to the same background loader as compat/comment/regional
        # (see _load_variations_async / _load_lazy_sections) instead of
        # blocking the synchronous open path, since it's the single biggest
        # widget-count contributor in the "Огляд" tab.
        _var_placeholder = _loading_placeholder() if _var_ap else ft.Container(height=0)

        def _load_variations_async():
            if not _var_ap:
                return
            try:
                def _fp(v: str) -> str:
                    try:
                        return f"{float(v):,.0f} ₴".replace(",", " ")
                    except Exception:
                        return v

                _hdr_style = ft.TextStyle(size=10, weight=ft.FontWeight.W_700, color=_MUTED)
                _cell_style = ft.TextStyle(size=11, color=_TEXT)

                def _hdr(txt: str) -> ft.DataColumn:
                    return ft.DataColumn(ft.Text(txt, style=_hdr_style))

                dt_rows = []
                for _i, _name in enumerate(_var_ap):
                    _color = _var_aq[_i] if _i < len(_var_aq) else "—"
                    _p_lo = _fp(_var_ar[_i]) if _i < len(_var_ar) else "—"
                    _p_hi = _fp(_var_as[_i]) if _i < len(_var_as) else "—"
                    _shops = _var_at[_i] if _i < len(_var_at) else "—"
                    dt_rows.append(
                        ft.DataRow(
                            cells=[
                                ft.DataCell(ft.Text(_name, style=_cell_style)),
                                ft.DataCell(ft.Text(_color, style=_cell_style)),
                                ft.DataCell(ft.Text(_p_lo, style=_cell_style)),
                                ft.DataCell(ft.Text(_p_hi, style=_cell_style)),
                                ft.DataCell(ft.Text(_shops, style=_cell_style)),
                            ]
                        )
                    )

                var_table = ft.DataTable(
                    columns=[
                        _hdr(self.t("gsm.device_modal.col.variation", "Варіація")),
                        _hdr(self.t("gsm.device_modal.col.color", "Колір")),
                        _hdr(self.t("gsm.device_modal.col.price_lo", "Ціна нижча")),
                        _hdr(self.t("gsm.device_modal.col.price_hi", "Ціна вища")),
                        _hdr(self.t("gsm.device_modal.col.shops", "Магазини")),
                    ],
                    rows=dt_rows,
                    heading_row_height=28,
                    data_row_min_height=26,
                    data_row_max_height=26,
                    column_spacing=14,
                    divider_thickness=0.5,
                )

                _var_placeholder.content = ft.Column(
                    [_section_title("Варіації"), var_table], spacing=4, tight=True
                )
                _var_placeholder.height = None
                with contextlib.suppress(Exception):
                    _var_placeholder.update()
            except Exception:
                pass

        ek_link_btn = ft.TextButton(
            "Відкрити на Єкаталог",
            icon=ft.Icons.OPEN_IN_NEW,
            on_click=lambda _: page.launch_url(ek_url) if ek_url else None,
            disabled=not ek_url,
        )
        # Mutable holder (not a plain var) so the click handler always reads
        # the LATEST saved URL, and the save callback can flip the icon
        # link->pencil in place — without it the button stayed stuck on the
        # "attach" icon (and the dialog kept reopening with the OLD value)
        # until the whole card was closed/reopened or a full sync ran.
        _manual_ek_url_ref = [str(gs.get("manual_ek_url") or "")]

        def _manual_ek_btn_state(url: str) -> tuple:
            icon = ft.Icons.LINK if not url else ft.Icons.EDIT
            tooltip = (
                "Вказати посилання на Єкаталог вручну"
                if not url
                else "Змінити ручне посилання на Єкаталог"
            )
            return icon, tooltip

        def _on_manual_ek_url_saved(new_url: str):
            _manual_ek_url_ref[0] = new_url
            if ek_manual_btn is not None:
                ek_manual_btn.icon, ek_manual_btn.tooltip = _manual_ek_btn_state(new_url)
                with contextlib.suppress(Exception):
                    ek_manual_btn.update()

        _manual_ek_icon, _manual_ek_tooltip = _manual_ek_btn_state(_manual_ek_url_ref[0])
        ek_manual_btn = (
            ft.IconButton(
                icon=_manual_ek_icon,
                icon_size=16,
                tooltip=_manual_ek_tooltip,
                on_click=lambda _: self._manual_ek_url_dialog(
                    page, device_id, _manual_ek_url_ref[0], refresh_cb=_on_manual_ek_url_saved
                ),
            )
            if self.is_superadmin
            else None
        )
        ek_remove_btn = (
            ft.IconButton(
                icon=ft.Icons.DELETE_OUTLINE,
                icon_size=16,
                icon_color=ft.Colors.RED_400,
                tooltip="Прибрати знайдений товар ЄК (ГСМ Arena залишиться) — наступний парсинг спробує знайти знову",
                on_click=lambda _: self._confirm_remove_ek_match(page, device_id, ek_id),
            )
            if self.is_superadmin and ek_id
            else None
        )
        # Manual-merge button: only when this card is missing its other
        # side (GSM-primary with no EK match, or a standalone EK-only
        # card) — once both sides are present there's nothing to merge.
        _is_gsm_side = gsm_name != "—"
        _merge_missing_side = (_is_gsm_side and not ek_id) or not _is_gsm_side
        merge_btn = (
            ft.IconButton(
                icon=ft.Icons.JOIN_FULL,
                icon_size=16,
                tooltip=(
                    "Об'єднати з існуючим записом Єкаталог"
                    if _is_gsm_side
                    else "Об'єднати з існуючим записом GSM Arena"
                ),
                on_click=lambda _: self._merge_devices_dialog(
                    page, device_id, _is_gsm_side, brand
                ),
            )
            if self.is_superadmin and _merge_missing_side
            else None
        )

        # ---- "Огляд" — combined tab (GSM + EK + compat + comment) ----
        # Compat, regional variants, and comment are loaded lazily to keep
        # the card instant-open. Each starts as a visible spinner row (not a
        # zero-height container) so the user sees something is loading
        # instead of a section that silently appears seconds later.
        _compat_placeholder = _loading_placeholder()
        _is_xiaomi = brand.strip().lower() == "xiaomi"
        _regional_placeholder = (
            _loading_placeholder() if _is_xiaomi else ft.Container(height=0)
        )
        _comment_placeholder = _loading_placeholder()

        overview_tab = ft.Column(
            [
                _regional_placeholder,
                ft.Row(
                    [
                        photo,
                        ft.Container(width=12),
                        ft.Column(
                            gsm_rows or [ft.Text("Немає даних", color=_MUTED)],
                            spacing=5,
                            expand=True,
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
                gsm_link_btn,
                ft.Divider(height=14),
                _section_title("Єкаталог"),
                ft.Column(
                    (ek_rows or [ft.Text("ЄК дані відсутні", color=_MUTED)])
                    + ([ft.Container(height=8), _var_placeholder] if _var_ap else [])
                    + [
                        ft.Row(
                            [ek_link_btn]
                            + ([ek_manual_btn] if ek_manual_btn else [])
                            + ([ek_remove_btn] if ek_remove_btn else [])
                            + ([merge_btn] if merge_btn else []),
                            spacing=0,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        )
                    ],
                    spacing=5,
                ),
                ft.Divider(height=14),
                _section_title("Сумісні пристрої"),
                _compat_placeholder,
                ft.Divider(height=14),
                _comment_placeholder,
            ],
            scroll=ft.ScrollMode.AUTO,
            height=380,
        )

        def _load_compat_async():
            try:
                ctrl = self._build_compat_readonly(device_id, page, _compat_dev)
                _compat_placeholder.content = ctrl
                _compat_placeholder.height = None
                with contextlib.suppress(Exception):
                    _compat_placeholder.update()
            except Exception:
                pass

        def _load_comment_async():
            try:
                ctrl = self._build_comments_section(
                    _compat_dev, page, refresh_cb=_load_comment_async
                )
                _comment_placeholder.content = ctrl
                _comment_placeholder.height = None
                with contextlib.suppress(Exception):
                    _comment_placeholder.update()
            except Exception:
                pass

        def _load_regional_async():
            try:
                ctrl = self._build_regional_variants_section(
                    _compat_dev, page, refresh_cb=_load_regional_async
                )
                if ctrl and getattr(ctrl, "height", None) != 0:
                    _regional_placeholder.content = ctrl
                    _regional_placeholder.height = None
                else:
                    # Nothing to show (no variants, no edit permission) —
                    # collapse the spinner instead of leaving it stuck.
                    _regional_placeholder.content = None
                    _regional_placeholder.height = 0
                with contextlib.suppress(Exception):
                    _regional_placeholder.update()
            except Exception:
                pass

        def _apply_photos(imgs: list):
            """Apply a list of photo URLs to the card UI (main image + thumbnails)."""
            _all_photos.clear()
            _all_photos.extend(imgs[:7])
            _main_img.src = imgs[0]
            if not image_url:
                _main_photo_ctrl.content = _main_img
                _main_photo_ctrl.on_click = lambda _: _photo_gallery(0)
                _main_photo_ctrl.ink = True
                with contextlib.suppress(Exception):
                    _main_photo_ctrl.update()
            else:
                with contextlib.suppress(Exception):
                    _main_img.update()

            def _mk_thumb_ctrl(idx: int, url: str) -> ft.Container:
                return ft.Container(
                    content=ft.Image(src=url, width=46, height=54, fit=ft.BoxFit.CONTAIN),
                    width=46,
                    height=54,
                    border_radius=6,
                    bgcolor=ft.Colors.BLUE_GREY_50,
                    alignment=ft.Alignment(0, 0),
                    on_click=lambda _, i=idx: _photo_gallery(i),
                    ink=True,
                )

            _thumbs_row.controls = [
                _mk_thumb_ctrl(i, url) for i, url in enumerate(_all_photos[1:], 1)
            ]
            with contextlib.suppress(Exception):
                _thumbs_row.update()

        def _load_photos_async():
            """Photos come from the DB (downloaded once at parse time — see
            GSM_Parser/main.py + database.py gsm_parser_device_photos), never
            live from GSM Arena. Written to local cache files on first fetch
            so repeat opens — even after an app restart — are instant local
            reads, no DB round trip needed."""
            with _PHOTO_LOCK:
                cached_paths = _PHOTO_CACHE.get(device_id)
            if cached_paths is not None:
                if cached_paths:
                    _apply_photos(cached_paths)
                else:
                    _thumbs_row.controls = []
                    with contextlib.suppress(Exception):
                        _thumbs_row.update()
                return

            gallery_paths = [_photo_cache_path(f"{device_id}_g{i}") for i in range(7)]
            existing = [p for p in gallery_paths if os.path.exists(p)]
            if existing:
                with _PHOTO_LOCK:
                    _PHOTO_CACHE[device_id] = existing
                _apply_photos(existing)
                return

            try:
                gallery_bytes = self.db.get_gsm_device_gallery_photos(device_id) or []
                paths = []
                for i, b in enumerate(gallery_bytes[:7]):
                    p = _photo_cache_path(f"{device_id}_g{i}")
                    try:
                        with open(p, "wb") as f:
                            f.write(b)
                        paths.append(p)
                    except Exception:
                        continue
                with _PHOTO_LOCK:
                    _PHOTO_CACHE[device_id] = paths

                if not paths:
                    _thumbs_row.controls = []
                    with contextlib.suppress(Exception):
                        _thumbs_row.update()
                    return

                _apply_photos(paths)
            except Exception:
                _thumbs_row.controls = []
                with contextlib.suppress(Exception):
                    _thumbs_row.update()

        # Tabs: "Огляд" (fast), "Діаграми" (lazy), "Чохли (ред.)"/"Ремінці (ред.)"
        # / "Скло (ред.)" [independently permission-gated, lazy]
        _is_watch = str(_compat_dev.get("device_type") or "").strip().lower() == "watch"
        _case_tab_label = "Ремінці (ред.)" if _is_watch else "Чохли (ред.)"
        tab_map = {"overview": overview_tab}
        tab_labels = [("overview", "Огляд"), ("chart", "Діаграми")]
        if self.can_edit_case:
            tab_labels.append(("compat_case", _case_tab_label))
        if self.can_edit_glass:
            tab_labels.append(("compat_glass", "Скло (ред.)"))

        body_container = ft.Container(content=overview_tab, height=380, expand=True)
        tab_state = {"active": "overview"}

        def _btn_style(active: bool):
            return ft.ButtonStyle(
                bgcolor=_OK_DK if active else _PANEL,
                color=_PANEL if active else _TEXT,
                side=ft.BorderSide(1, _OK_DK if active else _BORDER),
                shape=ft.RoundedRectangleBorder(radius=8),
                padding=ft.padding.symmetric(horizontal=10, vertical=6),
            )

        def _loading_tab():
            return ft.Column(
                [ft.ProgressRing(width=36, height=36, color=_OK_DK)],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                height=200,
            )

        def _refresh_compat_tab(key):
            """Rebuild one compat tab's content and swap it in place — used
            both for the initial lazy build and as the refresh_cb fired after
            an add/delete, so the modal never has to close and reopen.
            Runs the actual rebuild (DB queries) in a background thread —
            refresh_cb can be invoked synchronously from a click handler, and
            this must never block the UI thread either way."""

            def _work():
                _atype = "case" if key == "compat_case" else "glass"
                content = ft.Column(
                    [
                        self._build_compat_edit(
                            _compat_dev, page, _atype,
                            refresh_cb=lambda _k=key: _refresh_compat_tab(_k),
                        )
                    ],
                    scroll=ft.ScrollMode.AUTO,
                )
                tab_map[key] = content
                if tab_state["active"] == key:
                    body_container.content = content
                    with contextlib.suppress(Exception):
                        body_container.update()

            threading.Thread(target=_work, daemon=True).start()

        def _build_lazy_tab(key):
            # Always dispatched to a background thread below, even when
            # this device's detail is already warm in _DETAIL_CACHE. This
            # used to special-case the warm-cache path to run
            # _build_adaptive_charts synchronously right here on the
            # click handler — reasoned as "just a ~70-100ms matplotlib
            # render, short enough to block briefly" — but that estimate
            # only holds once matplotlib has already been imported once
            # in this process. Its FIRST import (see the `import
            # matplotlib` inside _build_adaptive_charts) can itself take
            # a full second or more, and that import runs on whichever
            # thread calls it — synchronously on the main thread, that is
            # a multi-second freeze of the entire app (nothing clickable,
            # not even closing the modal), which is exactly what got
            # reported. Never worth the saved thread-handoff overhead.
            body_container.content = _loading_tab()
            with contextlib.suppress(Exception):
                body_container.update()

            def _run():
                try:
                    if key == "chart":
                        content = ft.Column(
                            [self._build_adaptive_charts(device_id, _compat_dev)],
                            scroll=ft.ScrollMode.AUTO,
                        )
                        tab_map[key] = content
                        if tab_state["active"] == key:
                            body_container.content = content
                            with contextlib.suppress(Exception):
                                body_container.update()
                    elif key in ("compat_case", "compat_glass"):
                        _refresh_compat_tab(key)
                    return
                except Exception as exc:
                    logger.error("[GSM] lazy tab %s failed: %s", key, exc)

            threading.Thread(target=_run, daemon=True).start()

        def _switch_tab(key):
            def _h(_):
                tab_state["active"] = key
                if key in tab_map:
                    body_container.content = tab_map[key]
                    with contextlib.suppress(Exception):
                        body_container.update()
                else:
                    _build_lazy_tab(key)
                for btn, bkey in zip(tab_row.controls, [t[0] for t in tab_labels]):
                    btn.style = _btn_style(bkey == key)
                with contextlib.suppress(Exception):
                    tab_row.update()

            return _h

        tab_row = ft.Row(
            [
                ft.FilledButton(
                    lbl, on_click=_switch_tab(key), style=_btn_style(key == "gsm")
                )
                for key, lbl in tab_labels
            ],
            spacing=5,
            wrap=True,
        )

        def _device_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, gsm_name, [
                self.t("gsm.device_help.step1", "1. Перемикайтесь між вкладками: GSM Arena, E-Katalog, сумісність, історія цін."),
                self.t("gsm.device_help.step2", "2. «Редагувати» — додати коментар або посилання на суміжні моделі."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                [
                    ft.Column(
                        [
                            ft.Text(gsm_name, size=16, weight=ft.FontWeight.W_700, selectable=True),
                            ft.Text(
                                f"{brand}  •  GSM Arena + ЄК",
                                size=11,
                                color=ft.Colors.BLUE_GREY_600,
                            ),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_device_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            content=ft.Container(
                width=840,
                content=ft.Column(
                    [tab_row, ft.Divider(height=6), body_container],
                    spacing=6,
                    tight=True,
                ),
            ),
            actions=[ft.TextButton("Закрити", on_click=lambda _: _close_this_modal())],
            actions_alignment=ft.MainAxisAlignment.END,
        )

        def _close_this_modal():
            with contextlib.suppress(Exception):
                page.pop_dialog()
            if self._active_device_dialog is dlg:
                self._active_device_dialog = None

        self._active_device_dialog = dlg
        page.show_dialog(dlg)

        # Load compat, comment, regional variants, and extra photos.
        # Four concurrent threads each building Flet controls contend on the
        # global IdCounter lock (see _fill_expanded_brands_async docstring),
        # which is what turned "instant-open" cards into multi-second
        # freezes on every open — so this always runs sequentially, one
        # section at a time, never four threads at once.
        #
        # Detail data (compat/comment/variants) and the main photo are both
        # bulk-preloaded into _DETAIL_CACHE/_PHOTO_CACHE during the initial
        # sync (see _sync_cache_from_db), so for the overwhelming majority of
        # opens both caches are already warm here — building these sections
        # is then pure in-memory dict lookups + widget construction, cheap
        # enough to run synchronously right after the dialog is shown, with
        # no spinner flash. Only fall back to the background thread (with
        # its placeholder spinners) when either cache is still cold for this
        # device — e.g. a device added since the last sync.
        with _PHOTO_LOCK:
            _photos_warm = device_id in _PHOTO_CACHE
        _sections_warm = _get_device_detail(device_id) is not None and _photos_warm

        def _load_lazy_sections():
            _load_compat_async()
            _load_comment_async()
            _load_regional_async()
            _load_photos_async()
            _load_variations_async()

        if _sections_warm:
            _load_lazy_sections()
        else:
            threading.Thread(target=_load_lazy_sections, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  Filter presets                                                      #
    # ------------------------------------------------------------------ #
    def _save_preset_dialog(self, page):
        name_field = ft.TextField(label="Назва пресету", dense=True, autofocus=True)
        save_btn = ft.FilledButton("Зберегти", icon=ft.Icons.SAVE)

        def _reset_save_btn():
            save_btn.text = "Зберегти"
            save_btn.disabled = False
            with contextlib.suppress(Exception):
                save_btn.update()

        def _save(_):
            name = str(name_field.value or "").strip()
            if not name:
                return
            filters = {
                "brands": self._filter_brands,
                "types": self._filter_types,
                "years": self._filter_years,
                "statuses": self._filter_statuses,
                "source": self._filter_source,
                "search": self._search,
            }
            save_btn.text = "Збереження..."
            save_btn.disabled = True
            with contextlib.suppress(Exception):
                save_btn.update()

            async def _do():
                try:
                    await asyncio.to_thread(self.db.save_gsm_user_preset, self.manager_id, name, filters)
                    page.pop_dialog()
                    self._show_snack(self.t("gsm.preset_saved", "Пресет «{name}» збережено").format(name=name), ft.Colors.GREEN)
                    await asyncio.to_thread(self._refresh_presets_dropdown)
                except Exception:
                    _reset_save_btn()
                    raise

            page.run_task(_do)

        save_btn.on_click = _save

        def _filter_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, self.t("gsm.save_filter", "Зберегти фільтр"), [
                self.t("gsm.filter_help.step1", "1. Введіть назву пресету щоб зберегти поточні налаштування фільтра."),
                self.t("gsm.filter_help.step2", "2. Натисніть «Зберегти» — пресет з'явиться у списку пресетів."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Text(self.t("gsm.save_filter", "Зберегти фільтр"), size=15, weight=ft.FontWeight.W_700, expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_filter_help),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
            content=ft.Container(width=320, content=name_field),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _: page.pop_dialog()),
                save_btn,
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    def _refresh_presets_dropdown(self):
        """Reload presets from DB and update the toolbar dropdown in-place."""
        if not self.manager_id:
            return
        presets = self.db.get_gsm_user_presets(self.manager_id) or []
        new_options = [
            ft.DropdownOption(
                key=str(p.get("id") or ""), text=str(p.get("preset_name") or "—")
            )
            for p in presets
        ]
        if self._presets_dd_ref is not None:
            self._presets_dd_ref.options = new_options
            with contextlib.suppress(Exception):
                self._presets_dd_ref.update()
        # If no ref yet (first ever preset saved), full rebuild is needed
        # — caller already shows snack, user will see it after next _refresh_table

    def _load_preset(self, preset: dict):
        f = preset.get("filters") if isinstance(preset.get("filters"), dict) else {}
        self._filter_brands = list(f.get("brands") or [])
        self._filter_types = list(f.get("types") or [])
        self._filter_years = list(f.get("years") or [])
        self._filter_statuses = list(f.get("statuses") or [])
        self._filter_source = str(f.get("source") or "")
        self._search = str(f.get("search") or "")
        self._page_idx = 0
        self._refresh_table()

    # ------------------------------------------------------------------ #
    #  Build results content                                               #
    # ------------------------------------------------------------------ #
    def _refresh_table(
        self,
        rebuild_filters: bool = True,
        fill_all_brands: bool = False,
        defer_fill_to_caller: bool = False,
    ):
        """Rebuild table body + pagination + stats in-place.

        fill_all_brands=True forces every brand group's rows to be queued
        for building, not just the ones currently expanded — used only by
        the "Показати результати" reveal flow (_reveal_sync), which needs
        the whole page fully built before it hides its loading state so
        expanding ANY brand afterwards is instant instead of triggering a
        background build. Routine refreshes (search/filter/pagination/sync
        ticks) must keep the old lazy-only-visible behavior — building
        every brand's rows on every keystroke would reintroduce the exact
        freeze this laziness was added to avoid."""
        # Every call below clears and rebuilds self._table_body_ref from
        # scratch, orphaning whatever containers a PREVIOUS call's fill
        # queued rows into — but the queue itself was never cleared, so
        # those now-invisible fills kept running anyway. Harmless (if
        # wasteful) in brand-grouped mode where _to_fill is usually just
        # the couple of already-expanded brands, but in flat mode (see
        # _flat_mode) _to_fill is the ENTIRE catalog every single time —
        # a few fast clicks (e.g. re-sorting) queued that many full-catalog
        # rebuilds back to back, which is what read as "everything
        # disappears and hangs" (a real, if bounded, multi-refresh
        # backlog, not an infinite one). Drop anything still queued but
        # not yet started — it's for containers this call is about to
        # orphan anyway. A fill already mid-chunk finishes normally; only
        # ones that hadn't started yet are discarded.
        self._brand_fill_queue.clear()
        try:
            devices, _, loading = _get_cache()
            logger.info(
                "[GSM] _refresh_table: cache=%s loading=%s",
                len(devices) if devices else 0,
                loading,
            )
            filtered = self._get_filtered_devices()
            logger.info("[GSM] _refresh_table: filtered=%d", len(filtered))
            gsm_sz_c, ek_sz_c, gsm_dg_c, ek_dg_c = _compute_color_maps(filtered)
            page_devs = self._page_items(filtered)

            if self._table_body_ref is not None:
                self._table_body_ref.controls.clear()
                rows = []
                self._color_maps = (gsm_sz_c, ek_sz_c, gsm_dg_c, ek_dg_c)
                _to_fill: list[tuple[ft.Container, list[dict]]] = []
                if self._flat_mode:
                    # "Групування" toggled off — one continuous list, no
                    # brand headers/dividers/collapse state at all.
                    _prev_flat_container = (self._brand_body_by_name or {}).get("__flat__")
                    _prev_flat_devs = getattr(self, "_flat_last_page_devs", None)
                    self._brand_devs_by_name = {"__flat__": page_devs}
                    self._brand_body_by_name = {}
                    body_col = ft.Column(spacing=0)
                    body_container = ft.Container(content=body_col, visible=True)

                    _reused_rows = False
                    if (
                        _prev_flat_container is not None
                        and _prev_flat_container.data == "filled"
                        and _prev_flat_container.content is not None
                        and _prev_flat_devs is not None
                        and len(_prev_flat_devs) == len(page_devs)
                    ):
                        _by_id = {
                            getattr(c, "data", None): c
                            for c in _prev_flat_container.content.controls
                        }
                        _prev_by_id = {
                            str(d.get("id") or ""): d for d in _prev_flat_devs
                        }
                        _wanted_ids = [str(d.get("id") or "") for d in page_devs]
                        if (
                            len(_by_id) == len(_wanted_ids)
                            and all(did in _by_id for did in _wanted_ids)
                            # Content-equal, not just same IDs — a background
                            # sync can update prices/status for the SAME set
                            # of devices between two sort clicks; comparing
                            # full dicts (cheap: pure Python data, no Flet
                            # Control construction) instead of just IDs is
                            # what keeps this safe from showing stale rows.
                            and all(
                                _prev_by_id.get(did) == d
                                for did, d in zip(_wanted_ids, page_devs)
                            )
                        ):
                            # Every row for this exact device set is already
                            # built and unchanged (this is a pure re-sort,
                            # not a filter/search/data change) — reorder the
                            # existing Control objects instead of rebuilding
                            # ~1000+ rows from scratch through the chunked
                            # async path. That chunked rebuild (20 rows/
                            # chunk, one Flutter patch per chunk) is what
                            # made every sort click in flat mode feel like it
                            # hangs on a 1000+ device catalog — reusing built
                            # rows makes re-sorting effectively instant.
                            body_col.controls = [_by_id[did] for did in _wanted_ids]
                            body_container.data = "filled"
                            _reused_rows = True

                    if page_devs and not _reused_rows:
                        # Explicit spec: load in 50-item blocks, first 50
                        # rendered before the next 50 starts, all on the
                        # one table/page (no separate pager) — not one
                        # giant single-shot build. Routed through the same
                        # queue _fill_brand_body_async already uses for
                        # brand-expand (CHUNK=50, see its definition) so
                        # there's exactly one row-building path in the
                        # whole file. Critically, nothing else may be
                        # queued at the same time (background sync, chart
                        # preload) — competing async work on the same
                        # thread, not chunking itself, is what made this
                        # feel like a freeze before.
                        _to_fill.append((body_container, page_devs))

                    self._brand_body_by_name["__flat__"] = body_container
                    self._flat_last_page_devs = page_devs
                    rows.append(body_container)
                else:
                    # Group by brand — insert header + thick divider between brands
                    prev_brand: str | None = None
                    brand_group: list[dict] = []
                    brand_groups: list[tuple[str, list[dict]]] = []
                    for d in page_devs:
                        b = str(d.get("brand") or "")
                        if b != prev_brand:
                            if brand_group:
                                brand_groups.append((prev_brand, brand_group))
                            brand_group = [d]
                            prev_brand = b
                        else:
                            brand_group.append(d)
                    if brand_group:
                        brand_groups.append((prev_brand, brand_group))

                    # Cached for the lazy per-brand row build used by toggle
                    # clicks (see _build_brand_header) — avoids a full-table
                    # rebuild on collapse/expand, which used to freeze the
                    # whole app at 2000+ devices.
                    self._brand_devs_by_name = {b: devs for b, devs in brand_groups}
                    # Per-brand body container refs — lets a single delete/
                    # update patch just that one row's control in place
                    # afterwards (see _remove_devices_in_place/
                    # _update_device_row_in_place) instead of rebuilding the
                    # whole table, which used to reset scroll position and
                    # every brand's expand/collapse state.
                    self._brand_body_by_name = {}

                    # Rows are NEVER built synchronously here, even for
                    # brands that should appear expanded — building hundreds
                    # of rows then sending them in one update() is exactly
                    # the "thousands of controls in one Flutter patch"
                    # freeze pattern. Bodies start empty; brands that should
                    # be visible are filled afterwards, one brand at a time,
                    # in small chunks (_fill_expanded_brands_async).
                    first_brand = True
                    for brand_name, brand_devs in brand_groups:
                        if not first_brand:
                            rows.append(ft.Divider(height=3, thickness=3, color=_ACCENT))
                        first_brand = False
                        if brand_name not in _KNOWN_BRANDS:
                            _KNOWN_BRANDS.add(brand_name)
                            _COLLAPSED_BRANDS.add(brand_name)
                        collapsed = brand_name in _COLLAPSED_BRANDS
                        body_col = ft.Column(spacing=0)
                        body_container = ft.Container(content=body_col, visible=not collapsed)
                        self._brand_body_by_name[brand_name] = body_container
                        if fill_all_brands or not collapsed:
                            _to_fill.append((body_container, brand_devs))
                        rows.append(
                            self._build_brand_header(brand_name, len(brand_devs), body_container)
                        )
                        rows.append(body_container)
                self._table_body_ref.controls.extend(rows or [self._empty_row()])
            else:
                logger.warning(
                    "[GSM] _refresh_table: _table_body_ref is None — table not built yet"
                )

            if self._stats_ref is not None:
                quick = []
                if self._filter_diag:
                    quick.append(f"діаг:{self._filter_diag}")
                if self._filter_size:
                    quick.append(f"розмір:{self._filter_size}")
                extra = f" [{', '.join(quick)}]" if quick else ""
                self._stats_ref.value = self.t("gsm.found_devices", "Знайдено: {n} пристроїв{extra}").format(n=len(filtered), extra=extra)
            if self._loading_bar_ref is not None:
                self._loading_bar_ref.visible = loading
            if self._pager_ref is not None:
                self._pager_ref.controls.clear()
                self._pager_ref.controls.extend(self._build_pager_controls())

            page_ctrl = self._get_page_safe()
            logger.info("[GSM] _refresh_table: page_ctrl=%s", page_ctrl is not None)
            # Rebuild filter panel only when available options may have changed
            # (new data load or filter toggle). Skip for pagination/sort — much faster.
            if rebuild_filters and self._filter_panel_ref is not None:
                new_fp = self._build_filter_panel()
                self._filter_panel_ref.content = new_fp

            # Targeted updates only — never page_ctrl.update() (no args). At this
            # point brand bodies are still EMPTY (rows deferred, see _to_fill
            # below), so this tree is small/cheap to send regardless of how
            # many devices are in the table.
            for _ref in (
                self._table_body_ref,
                self._stats_ref,
                self._loading_bar_ref,
                self._pager_ref,
                (self._filter_panel_ref if rebuild_filters else None),
            ):
                if _ref is not None:
                    with contextlib.suppress(Exception):
                        _ref.update()

            if "_to_fill" in locals() and _to_fill:
                if defer_fill_to_caller:
                    # Caller (currently only _reveal_sync) awaits the fill
                    # directly instead of us fire-and-forgetting it via
                    # page_ctrl.run_task — that used to force the caller
                    # to poll self._brand_fill_running/_brand_fill_queue
                    # to know when it's done, and those flags could still
                    # read as "nothing running" for a moment after
                    # run_task schedules the coroutine but before it's
                    # actually had a turn to execute. Polling could then
                    # see a false "already done" and let the table reveal
                    # before brands were actually filled — exactly what
                    # made brand-expand still feel slow after the "loaded
                    # behind the spinner" rework. Handing back the pending
                    # work directly removes the race entirely.
                    self._pending_fill = (
                        _to_fill, (gsm_sz_c, ek_sz_c, gsm_dg_c, ek_dg_c)
                    )
                elif page_ctrl:
                    page_ctrl.run_task(
                        self._fill_expanded_brands_async,
                        _to_fill,
                        (gsm_sz_c, ek_sz_c, gsm_dg_c, ek_dg_c),
                    )

            # Catch-up: a filter change (or search) can bring devices into
            # view that the initial preload never touched — e.g. a user
            # who only ever filters to one device type never needed the
            # other ~300 devices preloaded in the first place. Top up just
            # the delta (chart render + local photo cache) in the
            # background, with its own visible status, instead of either
            # assuming everything is already warm or re-running the full
            # preload. Skipped when deferred (the reveal/manual-sync flows
            # already do their own full warm) and while one is already
            # running (avoid piling up redundant catch-ups on rapid
            # filter clicks).
            if (
                not defer_fill_to_caller
                and page_ctrl
                and not self._catch_up_running
            ):
                _new_devs = [
                    d for d in filtered
                    if str(d.get("id") or "") not in self._warm_device_ids
                ]
                if _new_devs:
                    page_ctrl.run_task(self._catch_up_new_devices, _new_devs)
        except Exception as exc:
            logger.error("[GSM] _refresh_table FAILED: %s", exc, exc_info=True)

    async def _catch_up_new_devices(self, new_devs: list[dict]) -> None:
        """Warms chart+photo cache for devices a filter/search change just
        revealed that the initial (or previous) preload never covered —
        see _refresh_table's catch-up hook."""
        if self._catch_up_running or not new_devs:
            return
        self._catch_up_running = True
        try:
            self._reveal_charts_done = 0
            self._reveal_charts_total = 0
            self._update_loading_status(
                self.t("gsm.catching_up", "Довантаження… {n} нових пристроїв").format(
                    n=len(new_devs)
                )
            )
            if self._loading_bar_ref is not None:
                self._loading_bar_ref.visible = True
                with contextlib.suppress(Exception):
                    self._loading_bar_ref.update()
            if self._loading_status_ref is not None:
                self._loading_status_ref.visible = True
                with contextlib.suppress(Exception):
                    self._loading_status_ref.update()
            await self._preload_charts_for_current_page(new_devs)
            await self._warm_photo_cache_from_db(new_devs)
            self._warm_device_ids.update(
                str(d.get("id") or "") for d in new_devs
            )
            _catlog.info("[GSM] _catch_up_new_devices completed: %s devices", len(new_devs))
        except Exception:
            logger.exception("[GSM] _catch_up_new_devices FAILED")
            _catlog.exception("[GSM] _catch_up_new_devices FAILED")
        finally:
            self._catch_up_running = False
            self._hide_loading_overlay()

    def _empty_row(self) -> ft.Control:
        return ft.Container(
            padding=30,
            content=ft.Column(
                [
                    ft.Icon(
                        ft.Icons.SEARCH_OFF, size=42, color=ft.Colors.BLUE_GREY_200
                    ),
                    ft.Text(
                        self.t("gsm.no_devices_found", "Пристроїв не знайдено. Спробуйте змінити фільтри або запустіть парсинг."),
                        color=ft.Colors.BLUE_GREY_400,
                        text_align=ft.TextAlign.CENTER,
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=10,
            ),
        )

    # ------------------------------------------------------------------ #
    #  Device comparison modal                                            #
    # ------------------------------------------------------------------ #

    def _get_xiaomi_size_groups(self, device_id: str, dev: dict) -> list[dict]:
        """Group a Xiaomi device's regional variants by DISTINCT physical
        size (exact L×W×T + diagonal match — even a 1-2mm difference is
        treated as a different group, since a case/glass sized for one
        may not fit the other).

        Returns [] if not Xiaomi or no regional variants with size data.
        Returns a list of 1+ group dicts otherwise:
        {"label": "Global, China", "regions": [...], "L", "W", "T",
         "diagonal", "size_str", "diag_str"}.
        A single-item result means all regions share one size (no split
        needed); 2+ items means the device must be treated as if it were
        several distinct devices sharing one product card for compat
        purposes.
        """
        brand = str(dev.get("brand") or "").strip().lower()
        if brand != "xiaomi":
            return []
        detail = self._get_or_fetch_device_detail(device_id, dev)
        variants = detail.get("variants") or []
        sized = [
            v for v in variants
            if isinstance(v, dict)
            and str(v.get("region") or "").strip()
            and str(v.get("L") or "").strip()
            and str(v.get("W") or "").strip()
        ]
        if not sized:
            return []

        def _sig(v: dict) -> tuple:
            return (
                str(v.get("L") or "").strip(),
                str(v.get("W") or "").strip(),
                str(v.get("T") or "").strip(),
                str(v.get("diagonal") or "").strip(),
            )

        groups: dict[tuple, list[str]] = {}
        order: list[tuple] = []
        for v in sized:
            sig = _sig(v)
            region = str(v.get("region") or "").strip()
            if sig not in groups:
                groups[sig] = []
                order.append(sig)
            if region not in groups[sig]:
                groups[sig].append(region)

        def _size_str(sig: tuple) -> str:
            L, W, T, _ = sig
            parts = [p for p in (L, W, T) if p]
            return "×".join(parts) + " мм" if parts else ""

        def _diag_str(sig: tuple) -> str:
            d = sig[3]
            return f'{d}"' if d else ""

        result = []
        for sig in order:
            result.append({
                "label": ", ".join(groups[sig]),
                "regions": list(groups[sig]),
                "L": sig[0], "W": sig[1], "T": sig[2], "diagonal": sig[3],
                "size_str": _size_str(sig),
                "diag_str": _diag_str(sig),
            })
        return result

    def _build_compare_entries(self, dev: dict) -> list[dict]:
        """Expand one selected device into one comparison "entry" per
        DISTINCT physical size among its Xiaomi regional variants.

        - No variants at all (or not Xiaomi): one entry, name/specs unchanged.
        - Variants exist but all share one L×W×T+diagonal: still one entry,
          name unchanged, but its size/diagonal come from the regional table
          instead of GSM Arena (more precise/reliable for Xiaomi).
        - Variants split into 2+ distinct sizes: one entry per size group,
          name suffixed with the regions sharing that size, e.g.
          "Xiaomi Redmi Note 15 Pro 4G (Europe, India)".
        """
        device_id = str(dev.get("id") or "")
        base_name = str(dev.get("gsm_name") or dev.get("ek_name") or "—")
        groups = self._get_xiaomi_size_groups(device_id, dev)

        if not groups:
            entry = dict(dev)
            entry["_compare_name"] = base_name
            entry["_variant_label"] = ""
            return [entry]

        if len(groups) == 1:
            g = groups[0]
            entry = dict(dev)
            entry["_compare_name"] = base_name
            entry["_variant_label"] = ""
            entry["_size_override"] = g["size_str"]
            entry["_diag_override"] = g["diag_str"]
            return [entry]

        entries = []
        for g in groups:
            entry = dict(dev)
            entry["_compare_name"] = f"{base_name} ({g['label']})"
            entry["_variant_label"] = g["label"]
            entry["_size_override"] = g["size_str"]
            entry["_diag_override"] = g["diag_str"]
            entries.append(entry)
        return entries

    def _comparison_modal(self, page_ctrl, selected_ids: set) -> None:
        """Open a side-by-side comparison modal for the selected devices.
        Instant: reads only from _DEVICE_CACHE (already in memory)."""
        if not page_ctrl:
            return
        devices, _, _ = _get_cache()
        if not devices:
            return

        # Preserve visible order
        filtered = [d for d in devices if str(d.get("id") or "") in selected_ids]
        if len(filtered) < 2:
            return

        # Expand any Xiaomi device with multiple differently-sized regional
        # variants into one column per size — see _build_compare_entries.
        # Sibling columns from the same selected device share a group color
        # (only assigned when a device actually split into 2+ columns) so
        # they read as "one device, several variants" instead of looking
        # like unrelated devices that happen to have similar names.
        _GROUP_PALETTE = ["#DBEAFE", "#DCFCE7", "#FDE7CB", "#FCE4EC", "#EDE9FE", "#FEF9C3"]
        compare_entries: list[dict] = []
        _palette_idx = 0
        for orig_idx, d in enumerate(filtered):
            entries = self._build_compare_entries(d)
            group_color = None
            if len(entries) > 1:
                group_color = _GROUP_PALETTE[_palette_idx % len(_GROUP_PALETTE)]
                _palette_idx += 1
            for entry in entries:
                entry["_group_idx"] = orig_idx
                entry["_group_color"] = group_color
                compare_entries.append(entry)

        col_w = 260
        cmp_scroll_ref = ft.Ref[ft.Row]()

        def _cmp_scroll(delta):
            async def _do(_d=delta):
                with contextlib.suppress(Exception):
                    if cmp_scroll_ref.current:
                        await cmp_scroll_ref.current.scroll_to(delta=_d, duration=200)
            with contextlib.suppress(Exception):
                page_ctrl.run_task(_do)

        modal_ref: list = [None]

        def _close_modal(_e=None):
            with contextlib.suppress(Exception):
                if modal_ref[0]:
                    page_ctrl.pop_dialog()

        # Photo lists per device, kept in sync as each column loads its photos
        # asynchronously — the full-screen gallery flattens these into one
        # combined left/right sequence across ALL compared devices.
        _dev_names = [
            str(e.get("_compare_name") or e.get("gsm_name") or e.get("ek_name") or "—")
            for e in compare_entries
        ]
        _dev_photo_lists: list[list[str]] = [[] for _ in compare_entries]

        def _open_compare_gallery(dev_idx: int, photo_idx: int):
            urls: list[str] = []
            owners: list[int] = []
            for i, lst in enumerate(_dev_photo_lists):
                for u in lst:
                    urls.append(u)
                    owners.append(i)
            if not urls:
                return
            start = sum(len(_dev_photo_lists[i]) for i in range(dev_idx)) + photo_idx
            start = max(0, min(start, len(urls) - 1))

            _idx = [start]
            _dw = (getattr(page_ctrl, "window_width", None) or 1200) * 0.88
            _dh = (getattr(page_ctrl, "window_height", None) or 900) * 0.90
            _img_w = _dw - 120
            _img_h = _dh - 76

            _gal_img = ft.Image(
                src=urls[_idx[0]], width=_img_w, height=_img_h, fit=ft.BoxFit.CONTAIN
            )
            _gal_caption = ft.Text(
                _dev_names[owners[_idx[0]]],
                color=ft.Colors.WHITE,
                size=12,
                weight=ft.FontWeight.W_600,
            )
            _gal_counter = ft.Text(
                f"{_idx[0] + 1} / {len(urls)}", color=ft.Colors.GREY_500, size=11
            )

            def _go(delta, _=None):
                _idx[0] = (_idx[0] + delta) % len(urls)
                _gal_img.src = urls[_idx[0]]
                _gal_caption.value = _dev_names[owners[_idx[0]]]
                _gal_counter.value = f"{_idx[0] + 1} / {len(urls)}"
                with contextlib.suppress(Exception):
                    _gal_img.update()
                    _gal_caption.update()
                    _gal_counter.update()

            _gal_arrow_style = ft.ButtonStyle(
                bgcolor=ft.Colors.with_opacity(0.22, ft.Colors.WHITE),
                shape=ft.CircleBorder(),
            )
            _gal_prev = ft.IconButton(
                icon=ft.Icons.CHEVRON_LEFT,
                icon_color=ft.Colors.WHITE,
                icon_size=34,
                on_click=lambda _: _go(-1),
                visible=len(urls) > 1,
                style=_gal_arrow_style,
            )
            _gal_next = ft.IconButton(
                icon=ft.Icons.CHEVRON_RIGHT,
                icon_color=ft.Colors.WHITE,
                icon_size=34,
                on_click=lambda _: _go(1),
                visible=len(urls) > 1,
                style=_gal_arrow_style,
            )

            _gal_dlg_ref: list = [None]
            _prev_kb_gal = [getattr(page_ctrl, "on_keyboard_event", None)]

            def _close_gal(_=None):
                page_ctrl.on_keyboard_event = _prev_kb_gal[0]
                with contextlib.suppress(Exception):
                    page_ctrl.pop_dialog()

            def _on_gal_kb(e: ft.KeyboardEvent):
                if e.key == "Arrow Left":
                    _go(-1)
                elif e.key == "Arrow Right":
                    _go(1)
                elif e.key == "Escape":
                    _close_gal()

            page_ctrl.on_keyboard_event = _on_gal_kb

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
                                            _gal_prev,
                                            ft.Container(
                                                content=_gal_img,
                                                expand=True,
                                                alignment=ft.Alignment(0, 0),
                                            ),
                                            _gal_next,
                                        ],
                                        expand=True,
                                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        spacing=4,
                                    ),
                                    ft.Column(
                                        [_gal_caption, _gal_counter],
                                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                        spacing=2,
                                    ),
                                ],
                                spacing=4,
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
                                    bgcolor=ft.Colors.with_opacity(
                                        0.55, ft.Colors.BLACK
                                    ),
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
                on_dismiss=lambda _: setattr(
                    page_ctrl, "on_keyboard_event", _prev_kb_gal[0]
                ),
            )
            page_ctrl.open(_gal_dlg_ref[0])

        columns = [
            self._build_compare_col(
                dev, col_w, page_ctrl, idx, _dev_photo_lists, _open_compare_gallery
            )
            for idx, dev in enumerate(compare_entries)
        ]

        cmp_row = ft.Row(
            columns,
            ref=cmp_scroll_ref,
            scroll=ft.ScrollMode.AUTO,
            spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.START,
        )

        _arrow_style = ft.ButtonStyle(
            padding=ft.padding.all(4),
            overlay_color=ft.Colors.with_opacity(0.1, ft.Colors.BLUE),
        )

        modal = ft.AlertDialog(
            modal=True,
            title=ft.Row(
                [
                    ft.Icon(ft.Icons.COMPARE_ARROWS, color=_ACCENT, size=20),
                    ft.Text(
                        (
                            self.t("gsm.comparison_title", "Порівняння пристроїв ({n})").format(n=len(filtered))
                            if len(compare_entries) == len(filtered)
                            else self.t(
                                "gsm.comparison_title_expanded",
                                "Порівняння: {n} моделей, {m} варіантів",
                            ).format(n=len(filtered), m=len(compare_entries))
                        ),
                        size=14,
                        weight=ft.FontWeight.W_700,
                    ),
                    ft.Container(expand=True),
                    ft.IconButton(
                        ft.Icons.CLOSE,
                        icon_size=18,
                        on_click=_close_modal,
                        style=ft.ButtonStyle(padding=ft.padding.all(4)),
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=8,
            ),
            content=ft.Row(
                [
                    ft.IconButton(
                        ft.Icons.CHEVRON_LEFT,
                        icon_size=24,
                        on_click=lambda _: _cmp_scroll(-(col_w + 4)),
                        style=_arrow_style,
                        icon_color=_ACCENT,
                    ),
                    ft.Container(
                        expand=True,
                        height=580,
                        content=cmp_row,
                        clip_behavior=ft.ClipBehavior.HARD_EDGE,
                    ),
                    ft.IconButton(
                        ft.Icons.CHEVRON_RIGHT,
                        icon_size=24,
                        on_click=lambda _: _cmp_scroll(col_w + 4),
                        style=_arrow_style,
                        icon_color=_ACCENT,
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=0,
            ),
            content_padding=ft.padding.symmetric(horizontal=8, vertical=8),
            actions=[],
            inset_padding=ft.padding.symmetric(horizontal=16, vertical=32),
        )
        modal_ref[0] = modal
        with contextlib.suppress(Exception):
            page_ctrl.open(modal)

    def _build_compare_col(
        self,
        dev: dict,
        width: int,
        page_ctrl,
        dev_idx: int = 0,
        shared_photo_lists: list | None = None,
        open_gallery_cb=None,
    ) -> ft.Container:
        """Build one device column for the comparison modal."""
        device_id = str(dev.get("id") or "")
        gs = dev.get("gsm_specs") or {}
        es = dev.get("ek_specs") or {}

        with _PHOTO_LOCK:
            cached_photos = list(_PHOTO_CACHE.get(device_id) or [])

        if not cached_photos:
            # Same rule as the main table row: only ever show a photo that
            # came from this app's own DB (_photo_cache_path/_PHOTO_CACHE),
            # never a live fetch from GSM Arena/E-Katalog.
            _local_main = _photo_cache_path(device_id) if device_id else ""
            cached_photos = [_local_main] if _local_main and os.path.exists(_local_main) else []
        all_urls = cached_photos
        if shared_photo_lists is not None:
            shared_photo_lists[dev_idx] = list(all_urls)

        photo_row_ref = ft.Ref[ft.Row]()
        _THUMB_W, _THUMB_H = 114, 134

        def _mk_thumb(url: str, idx: int) -> ft.Container:
            return ft.Container(
                content=ft.Image(src=url, width=_THUMB_W, height=_THUMB_H, fit=ft.BoxFit.CONTAIN),
                width=_THUMB_W,
                height=_THUMB_H,
                border_radius=6,
                bgcolor=_ALT,
                alignment=ft.Alignment(0, 0),
                clip_behavior=ft.ClipBehavior.HARD_EDGE,
                ink=open_gallery_cb is not None,
                on_click=(
                    (lambda _, _i=idx: open_gallery_cb(dev_idx, _i))
                    if open_gallery_cb is not None
                    else None
                ),
            )

        _placeholder = ft.Container(
            width=_THUMB_W, height=_THUMB_H, bgcolor=_ALT, border_radius=6,
            content=ft.Icon(ft.Icons.PHONE_ANDROID, size=42, color=_MUTED),
            alignment=ft.Alignment(0, 0),
        )

        photo_row = ft.Row(
            [_mk_thumb(u, i) for i, u in enumerate(all_urls[:8])] if all_urls else [_placeholder],
            ref=photo_row_ref,
            scroll=ft.ScrollMode.AUTO,
            spacing=6,
        )

        # Load photos if not yet cached — from the DB (downloaded once at
        # parse time), never live from GSM Arena. Written to local cache
        # files on first fetch, same as the card modal's _load_photos_async.
        if not cached_photos:

            async def _fetch_photos(_did=device_id):
                with _PHOTO_LOCK:
                    c = _PHOTO_CACHE.get(_did)
                if c is not None:
                    if c:
                        if shared_photo_lists is not None:
                            shared_photo_lists[dev_idx] = list(c[:8])
                        row = photo_row_ref.current
                        if row:
                            row.controls = [_mk_thumb(u, i) for i, u in enumerate(c[:8])]
                            with contextlib.suppress(Exception):
                                row.update()
                    return

                gallery_paths = [_photo_cache_path(f"{_did}_g{i}") for i in range(7)]
                existing = [p for p in gallery_paths if os.path.exists(p)]
                if existing:
                    with _PHOTO_LOCK:
                        _PHOTO_CACHE[_did] = existing
                    if shared_photo_lists is not None:
                        shared_photo_lists[dev_idx] = list(existing[:8])
                    row = photo_row_ref.current
                    if row:
                        row.controls = [_mk_thumb(u, i) for i, u in enumerate(existing[:8])]
                        with contextlib.suppress(Exception):
                            row.update()
                    return

                try:
                    gallery_bytes = await asyncio.to_thread(
                        self.db.get_gsm_device_gallery_photos, _did
                    )
                    paths = []
                    for i, b in enumerate((gallery_bytes or [])[:7]):
                        p = _photo_cache_path(f"{_did}_g{i}")
                        try:
                            with open(p, "wb") as f:
                                f.write(b)
                            paths.append(p)
                        except Exception:
                            continue
                    with _PHOTO_LOCK:
                        _PHOTO_CACHE[_did] = paths
                    if paths:
                        if shared_photo_lists is not None:
                            shared_photo_lists[dev_idx] = list(paths[:8])
                        row = photo_row_ref.current
                        if row:
                            row.controls = [_mk_thumb(u, i) for i, u in enumerate(paths[:8])]
                            with contextlib.suppress(Exception):
                                row.update()
                except Exception:
                    pass

            if page_ctrl:
                with contextlib.suppress(Exception):
                    page_ctrl.run_task(_fetch_photos)

        # Spec row builder
        def _srow(label: str, value) -> ft.Control | None:
            v = str(value or "").strip()
            if not v:
                return None
            return ft.Container(
                width=width,
                border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
                padding=ft.padding.symmetric(horizontal=8, vertical=4),
                content=ft.Row(
                    [
                        ft.Text(label, size=9, color=_MUTED, width=74),
                        ft.Text(
                            v, size=9, weight=ft.FontWeight.W_600,
                            expand=True, no_wrap=False, max_lines=3,
                        ),
                    ],
                    spacing=4,
                    # Row sits inside a vertically-scrolling Column nested in a
                    # horizontally-scrolling outer Row (the device columns) —
                    # without an explicit width on this Container, that nested
                    # scroll chain lets the value Text size to its natural
                    # (unwrapped) content width instead of wrapping, which
                    # shows up as a horizontal scrollbar on the whole dialog.
                ),
            )

        def _shdr(text: str, color: str) -> ft.Container:
            return ft.Container(
                width=width,
                bgcolor=color,
                padding=ft.padding.symmetric(horizontal=8, vertical=4),
                content=ft.Text(text, size=9, weight=ft.FontWeight.W_700, color=ft.Colors.WHITE),
            )

        # Price helper
        def _pfmt(v) -> str:
            try:
                return f"{float(v):,.0f} ₴".replace(",", " ")
            except Exception:
                return str(v or "").strip()

        price_lo = str(es.get("price_min") or es.get("price_low") or "").strip()
        price_hi = str(es.get("price_max") or "").strip()

        # A split-by-region entry carries its own precise size/diagonal from
        # the Xiaomi regional-variants table — prefer that over GSM Arena's
        # single figure, which can't represent per-region differences.
        _size_override = str(dev.get("_size_override") or "").strip()
        _diag_override = str(dev.get("_diag_override") or "").strip()

        gsm_rows = [r for r in [
            _srow("Мережа", gs.get("network_label")),
            _srow("Оголошено", gs.get("announced")),
            _srow("Реліз", gs.get("released")),
            _srow("Розміри", _size_override or gs.get("dimensions")),
            _srow("Діагональ", _diag_override or gs.get("display_size")),
            _srow("Вага", gs.get("weight")),
            _srow("ОС", gs.get("os")),
            _srow("Процесор", gs.get("chipset")),
            _srow("Пам'ять", gs.get("memory")),
            _srow("Батарея", gs.get("battery")),
        ] if r is not None]

        ek_rows = [r for r in [
            _srow("Назва ЄК", dev.get("ek_name")),
            _srow("Мережа", es.get("target_net")),
            _srow("Дата", es.get("date_added")),
            _srow("Розмір", _size_override or es.get("dimensions")),
            _srow("Діагональ", _diag_override or es.get("display")),
            _srow("Вага", es.get("weight")),
            _srow("ОС", es.get("os")),
            _srow("Процесор", es.get("cpu")),
            _srow("Пам'ять", es.get("memory")),
            _srow("Батарея", es.get("battery")),
            _srow("Статус", es.get("status")),
            _srow("Ціна нижча", _pfmt(price_lo) if price_lo else None),
            _srow("Ціна вища", _pfmt(price_hi) if price_hi else None),
        ] if r is not None]

        device_name = str(dev.get("_compare_name") or dev.get("gsm_name") or dev.get("ek_name") or "—")
        device_brand = str(dev.get("brand") or "")
        device_type = str(dev.get("device_type") or "")
        _is_watch = device_type.strip().lower() == "watch"
        _case_label = "Ремінці" if _is_watch else "Чохли"

        # Compatibility — this entry's variant_label (its region group, e.g.
        # "Europe, India") is empty unless the device actually split into
        # several sizes. Compat rows saved without a variant_label are
        # treated as valid for every region (nobody restricted them when
        # adding); rows WITH a label only show for the matching region
        # group, since a case that fits one size won't fit another.
        _variant_label = str(dev.get("_variant_label") or "").strip()
        detail = self._get_or_fetch_device_detail(device_id, dev)
        compat_by_type = detail.get("compat") or {}

        def _compat_names(items: list) -> list[str]:
            out = []
            for c in items or []:
                if not isinstance(c, dict):
                    continue
                vlbl = str(c.get("variant_label") or "").strip()
                if _variant_label and vlbl and vlbl != _variant_label:
                    continue
                name = f"{c.get('brand', '')} {c.get('model_name', '—')}".strip()
                if vlbl:
                    name += f" ({vlbl})"
                out.append(name)
            return out

        case_names = _compat_names(compat_by_type.get("case") or [])
        glass_names = _compat_names(compat_by_type.get("glass") or [])

        def _compat_rows(names: list[str]) -> list[ft.Control]:
            if not names:
                return [
                    ft.Container(
                        width=width,
                        border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
                        padding=ft.padding.symmetric(horizontal=8, vertical=4),
                        content=ft.Text("Немає даних", size=9, color=_MUTED, italic=True),
                    )
                ]
            return [
                ft.Container(
                    width=width,
                    border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
                    padding=ft.padding.symmetric(horizontal=8, vertical=3),
                    content=ft.Text(n, size=9, no_wrap=False, max_lines=2),
                )
                for n in names
            ]

        compat_section = (
            [_shdr(_case_label, "#7C3AED")]
            + _compat_rows(case_names)
            + [_shdr("Скло", "#0891B2")]
            + _compat_rows(glass_names)
        )

        spec_section = []
        if gsm_rows:
            spec_section += [_shdr("GSM Arena", _ACCENT)] + gsm_rows
        if ek_rows:
            spec_section += [_shdr("E-Katalog", _OK_DK)] + ek_rows
        spec_section += compat_section

        _header_bg = str(dev.get("_group_color") or "") or _SOFT

        return ft.Container(
            width=width,
            border=ft.border.only(right=ft.BorderSide(1, _BORDER)),
            content=ft.Column(
                [
                    # Device header
                    ft.Container(
                        bgcolor=_header_bg,
                        padding=ft.padding.symmetric(horizontal=8, vertical=8),
                        content=ft.Column(
                            [
                                ft.Text(
                                    device_name,
                                    size=11,
                                    weight=ft.FontWeight.W_700,
                                    text_align=ft.TextAlign.CENTER,
                                    no_wrap=False,
                                    max_lines=2,
                                ),
                                ft.Text(
                                    f"{device_brand} · {device_type}".strip(" ·"),
                                    size=9,
                                    color=_MUTED,
                                    text_align=ft.TextAlign.CENTER,
                                ),
                            ],
                            spacing=2,
                            tight=True,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ),
                    # Photo strip
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=6, vertical=6),
                        bgcolor=_ALT,
                        height=_THUMB_H + 18,
                        content=photo_row,
                        clip_behavior=ft.ClipBehavior.HARD_EDGE,
                    ),
                    # Specs (scrollable)
                    ft.Container(
                        expand=True,
                        content=ft.Column(
                            spec_section,
                            spacing=0,
                            tight=True,
                            scroll=ft.ScrollMode.AUTO,
                            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                        ),
                    ),
                ],
                spacing=0,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        )

    async def _fill_brand_body_async(
        self, body_container: ft.Container, devs: list[dict], color_maps: tuple
    ) -> None:
        """Lazily populate one brand's rows in small chunks, yielding between
        each chunk. Building all rows then sending ONE update() ships the
        entire new subtree to Flutter in a single patch — for a 100+ device
        brand that is a multi-second-to-tens-of-seconds freeze. Chunking keeps
        every individual patch small, so the UI stays responsive throughout.

        A loading row is shown at the top of the body while the first chunk
        builds, so expanding a large brand reads as "working" rather than
        stuck/unresponsive."""
        if body_container.data == "filled":
            return  # already built — pure visibility toggle handles this case
        controls = body_container.content.controls
        controls.clear()
        controls.append(
            ft.Container(
                content=ft.Row(
                    [
                        ft.ProgressRing(width=14, height=14, stroke_width=2),
                        ft.Text(
                            self.t("gsm.loading_devices", "Завантаження пристроїв…"),
                            size=11,
                            color=_MUTED,
                        ),
                    ],
                    spacing=8,
                ),
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
            )
        )
        with contextlib.suppress(Exception):
            body_container.update()

        gsm_sz_c, ek_sz_c, gsm_dg_c, ek_dg_c = color_maps
        CHUNK = 50  # explicit spec: load/render in blocks of 50
        first_chunk = True
        for i in range(0, len(devs), CHUNK):
            chunk_rows = []
            for d in devs[i : i + CHUNK]:
                try:
                    chunk_rows.append(
                        self._build_table_row(d, gsm_sz_c, ek_sz_c, gsm_dg_c, ek_dg_c)
                    )
                except Exception as row_exc:
                    logger.error(
                        "[GSM] lazy brand-row build failed for %s: %s",
                        d.get("gsm_name"),
                        row_exc,
                    )
            if first_chunk:
                controls.clear()  # drop the loading placeholder
                first_chunk = False
            controls.extend(chunk_rows)
            with contextlib.suppress(Exception):
                body_container.update()
            self._reveal_rows_done += len(chunk_rows)
            self._update_reveal_progress_text()
            await asyncio.sleep(0)
        body_container.data = "filled"

    def _queue_brand_fill(
        self, body_container: ft.Container, devs: list[dict], color_maps: tuple
    ) -> None:
        """Queue one brand's row-build behind a single drainer task, so
        rapid-fire expand clicks (or "expand all") never end up building
        two+ brands' rows concurrently — each independent page.run_task()
        call used to start interleaving on the same thread, which is
        exactly the "build hundreds of controls at once" CPU spike the
        chunking in _fill_brand_body_async exists to avoid."""
        self._brand_fill_queue.append((body_container, devs, color_maps))
        page_ctrl = self._get_page_safe()
        if page_ctrl and not self._brand_fill_running:
            self._brand_fill_running = True
            page_ctrl.run_task(self._drain_brand_fill_queue)

    async def _drain_brand_fill_queue(self) -> None:
        try:
            while self._brand_fill_queue:
                body_container, devs, color_maps = self._brand_fill_queue.pop(0)
                await self._fill_brand_body_async(body_container, devs, color_maps)
        finally:
            self._brand_fill_running = False

    async def _fill_expanded_brands_async(
        self, to_fill: list[tuple[ft.Container, list[dict]]], color_maps: tuple
    ) -> None:
        """Queue multiple brands' bodies through the same single-drainer
        path as individual toggle clicks (never concurrently — concurrent
        Flet builds contend on the global IdCounter lock and inflate build
        time, per established project rule).

        Always returns only once the queue (including what THIS call just
        added) is actually fully drained — critical for callers like
        _reveal_sync that await this directly and reveal the table right
        after. If a drain was already running when we got here (e.g. a
        concurrent _background_load call), the old code just appended to
        the shared queue and returned immediately without waiting for
        that other drain to reach our items — the loading screen would
        vanish while row-building was still actively going in the
        background, fighting chart/photo work for the same thread. That
        was the actual cause of "everything hangs even after the spinner
        is gone"."""
        for body_container, devs in to_fill:
            self._brand_fill_queue.append((body_container, devs, color_maps))
        if not self._brand_fill_running:
            self._brand_fill_running = True
            await self._drain_brand_fill_queue()
        else:
            while self._brand_fill_running or self._brand_fill_queue:
                await asyncio.sleep(0.05)

    def _build_toggle_all_brands_btn(self) -> ft.IconButton:
        """Header-row button (left of 'Бренд') to collapse/expand every brand
        group in one click. Reuses _refresh_table's existing lazy/chunked
        row-fill path (Phase 1 fix) — even expanding all brands at once never
        sends one giant Flutter patch."""
        btn_ref = ft.Ref[ft.IconButton]()

        def _all_collapsed() -> bool:
            all_brands = set(self._brand_devs_by_name.keys())
            return bool(all_brands) and all_brands <= _COLLAPSED_BRANDS

        def _toggle_all(_e=None):
            global _COLLAPSED_BRANDS
            if self._cache_busy():
                self._warn_cache_busy()
                return
            all_brands = set(self._brand_devs_by_name.keys())
            if not all_brands:
                return
            if all_brands <= _COLLAPSED_BRANDS:
                _COLLAPSED_BRANDS -= all_brands
            else:
                _COLLAPSED_BRANDS |= all_brands
            self._refresh_table(rebuild_filters=False)
            if btn_ref.current:
                now_collapsed = _all_collapsed()
                btn_ref.current.icon = (
                    ft.Icons.UNFOLD_LESS if now_collapsed else ft.Icons.UNFOLD_MORE
                )
                btn_ref.current.tooltip = (
                    self.t("gsm.expand_all_brands", "Розгорнути всі бренди") if now_collapsed else self.t("gsm.collapse_all_brands", "Згорнути всі бренди")
                )
                with contextlib.suppress(Exception):
                    btn_ref.current.update()

        return ft.IconButton(
            ref=btn_ref,
            icon=ft.Icons.UNFOLD_LESS if _all_collapsed() else ft.Icons.UNFOLD_MORE,
            icon_size=16,
            icon_color=_ACCENT,
            tooltip="Розгорнути всі бренди" if _all_collapsed() else "Згорнути всі бренди",
            on_click=_toggle_all,
            style=ft.ButtonStyle(padding=ft.padding.all(2)),
        )

    def _build_group_toggle_btn(self) -> ft.OutlinedButton:
        """Toolbar button: switch the whole table between the normal
        brand-grouped view (headers, dividers, collapse/expand) and one
        flat continuous list of every device with no grouping at all."""
        btn_ref = ft.Ref[ft.OutlinedButton]()

        def _label_and_icon():
            if self._flat_mode:
                return "Групування вимкнено", ft.Icons.VIEW_AGENDA_OUTLINED
            return "Групування по брендам", ft.Icons.VIEW_LIST_OUTLINED

        def _toggle_grouping(_e=None):
            if self._cache_busy():
                self._warn_cache_busy()
                return
            self._flat_mode = not self._flat_mode
            self._refresh_table(rebuild_filters=False)
            if btn_ref.current:
                label, icon = _label_and_icon()
                btn_ref.current.content = label
                btn_ref.current.icon = icon
                with contextlib.suppress(Exception):
                    btn_ref.current.update()

        label, icon = _label_and_icon()
        return ft.OutlinedButton(
            ref=btn_ref,
            content=label,
            icon=icon,
            on_click=_toggle_grouping,
            tooltip="Вимкнути/увімкнути групування пристроїв по брендам",
            style=ft.ButtonStyle(
                color=_ACCENT,
                side=ft.BorderSide(1, ft.Colors.BLUE_200),
                padding=ft.padding.symmetric(horizontal=10, vertical=5),
                text_style=ft.TextStyle(size=11),
            ),
        )

    def _build_brand_header(
        self, brand: str, count: int, body_container: ft.Container
    ) -> ft.Container:
        """Collapsible brand-group header row — shared for all users.

        Toggling never triggers a full table rebuild (that used to block the
        UI thread for seconds at 2000+ devices, freezing the whole app). It
        only flips `body_container.visible`, lazily building that brand's
        rows the first time it's expanded (cached afterwards on the
        container itself, so re-toggling is pure visibility flip — instant).
        """
        collapsed = brand in _COLLAPSED_BRANDS
        arrow_ref = ft.Ref[ft.IconButton]()
        count_ref = ft.Ref[ft.Text]()

        def _toggle(_e=None, _b=brand, _body=body_container):
            global _COLLAPSED_BRANDS
            if self._cache_busy():
                self._warn_cache_busy()
                return
            if _b in _COLLAPSED_BRANDS:
                _COLLAPSED_BRANDS.discard(_b)
                now_collapsed = False
            else:
                _COLLAPSED_BRANDS.add(_b)
                now_collapsed = True

            needs_fill = not now_collapsed and not _body.content.controls

            _body.visible = not now_collapsed
            if arrow_ref.current:
                arrow_ref.current.icon = (
                    ft.Icons.KEYBOARD_ARROW_RIGHT if now_collapsed else ft.Icons.KEYBOARD_ARROW_DOWN
                )
                arrow_ref.current.tooltip = "Розгорнути" if now_collapsed else "Згорнути"
            if count_ref.current:
                count_ref.current.value = (
                    f"{count} пристроїв (згорнуто)" if now_collapsed else f"{count} пристр."
                )
            with contextlib.suppress(Exception):
                _body.update()
                if arrow_ref.current:
                    arrow_ref.current.update()
                if count_ref.current:
                    count_ref.current.update()

            if needs_fill:
                # Build rows off the click handler, in small chunks, queued
                # behind any other brand fill already in progress — never
                # one giant synchronous build + single update(), and never
                # two brands' builds running interleaved at once (see
                # _fill_brand_body_async / _queue_brand_fill docstrings).
                devs = self._brand_devs_by_name.get(_b) or []
                self._queue_brand_fill(_body, devs, self._color_maps)

        return ft.Container(
            bgcolor=_SOFT,
            padding=ft.padding.symmetric(horizontal=10, vertical=5),
            content=ft.Row(
                [
                    ft.IconButton(
                        ref=arrow_ref,
                        icon=ft.Icons.KEYBOARD_ARROW_RIGHT if collapsed else ft.Icons.KEYBOARD_ARROW_DOWN,
                        icon_size=18,
                        icon_color=_ACCENT,
                        on_click=_toggle,
                        tooltip="Розгорнути" if collapsed else "Згорнути",
                        style=ft.ButtonStyle(padding=ft.padding.all(2)),
                    ),
                    ft.Text(
                        brand or "—",
                        size=13,
                        weight=ft.FontWeight.W_700,
                        color=_ACCENT,
                    ),
                    ft.Container(
                        content=ft.Text(
                            f"{count} пристр." if not collapsed else f"{count} пристроїв (згорнуто)",
                            size=10,
                            color=_MUTED,
                            ref=count_ref,
                        ),
                        bgcolor=_PANEL,
                        border=ft.border.all(1, _BORDER),
                        border_radius=8,
                        padding=ft.padding.symmetric(horizontal=7, vertical=2),
                    ),
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _build_table_row(
        self,
        merged: dict,
        gsm_size_colors: dict,
        ek_size_colors: dict,
        gsm_diag_colors: dict,
        ek_diag_colors: dict,
    ) -> ft.Container:
        gs = merged.get("gsm_specs") or {}
        es = merged.get("ek_specs") or {}
        page_ctrl = self._page_ref

        # ---- GSM section ----
        # ONLY the locally-cached photo file (itself sourced from this
        # app's own DB — see _warm_photo_cache_from_db, which bulk-warms
        # this cache for the whole catalog before the table is ever
        # revealed) — never the remote GSM Arena/E-Katalog image_url.
        # Falling back to a live fetch from those sites is explicitly not
        # allowed (photos only ever come from our DB) and was also a real
        # per-row network-latency cost that made large brand groups (and
        # flat mode) feel like they were freezing while building.
        device_id = str(merged.get("id") or "")
        _local_photo = _photo_cache_path(device_id) if device_id else ""
        img_src = (
            _local_photo
            if _local_photo and os.path.exists(_local_photo)
            else ""
        )
        thumb = (
            ft.Image(
                src=img_src,
                width=53,
                height=65,
                fit=ft.BoxFit.CONTAIN,
                border_radius=6,
            )
            if img_src
            else ft.Container(
                width=53,
                height=65,
                bgcolor=_ALT,
                border_radius=6,
                content=ft.Icon(ft.Icons.PHONE_ANDROID, size=26, color=_MUTED),
                alignment=ft.Alignment(0, 0),
            )
        )

        def _center(
            w, content, on_click=None, tooltip=None, bgcolor=None, border_radius=4
        ):
            return ft.Container(
                width=w,
                alignment=ft.Alignment(0, 0),
                ink=on_click is not None,
                on_click=on_click,
                tooltip=tooltip,
                border_radius=border_radius,
                bgcolor=bgcolor,
                padding=ft.padding.symmetric(horizontal=0, vertical=2),
                content=content,
            )

        def _vd():
            return ft.Container(width=1, height=72, bgcolor=_BORDER)

        def _tc(text, w, color=None, bold=False, on_click=None, tooltip=None):
            return _center(
                w,
                ft.Text(
                    str(text) if text else "—",
                    size=11,
                    color=color or _TEXT,
                    weight=ft.FontWeight.W_600 if bold else None,
                    text_align=ft.TextAlign.CENTER,
                ),
                on_click=on_click,
                tooltip=tooltip,
            )

        def _name_cell(text, w, on_click=None, tooltip=None):
            """Name cell: text is selectable (native OS copy — select, Ctrl+C)
            instead of a click/right-click gesture. Opening the device card
            is a separate small button to the right, so it never fights with
            text selection and never risks a GestureDetector with zero
            handlers (Flutter hard-errors on that when a cell has no
            on_click, e.g. an EK name that hasn't been matched yet)."""
            _txt = str(text or "").strip()
            row_controls = [
                ft.Container(
                    content=ft.Text(
                        _txt or "—",
                        size=11,
                        color=_TEXT,
                        text_align=ft.TextAlign.CENTER,
                        selectable=True,
                    ),
                    expand=True,
                    alignment=ft.Alignment(0, 0),
                )
            ]
            if on_click is not None:
                row_controls.append(
                    ft.IconButton(
                        icon=ft.Icons.OPEN_IN_NEW,
                        icon_size=13,
                        tooltip=tooltip or "Відкрити картку товару",
                        on_click=on_click,
                        style=ft.ButtonStyle(padding=ft.padding.all(2)),
                    )
                )
            return ft.Container(
                width=w,
                padding=ft.padding.symmetric(horizontal=2, vertical=2),
                content=ft.Row(
                    row_controls,
                    spacing=0,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            )

        def _hl_cell(
            val: str, width: int, color_map: dict, on_click=None, tooltip=None
        ) -> ft.Container:
            bg = color_map.get(val)
            return _center(
                width,
                ft.Text(
                    val or "—",
                    size=11,
                    color=_TEXT,
                    text_align=ft.TextAlign.CENTER,
                ),
                on_click=on_click,
                tooltip=tooltip,
                bgcolor=bg,
                border_radius=6,
            )

        def _release_cell(val: str, w: int) -> ft.Container:
            if not val:
                return _center(
                    w,
                    ft.Text("—", size=11, color=_TEXT, text_align=ft.TextAlign.CENTER),
                )
            if val.startswith("Р:"):
                label, date = "Реліз:", val[3:].strip()
            elif val.startswith("А:"):
                label, date = "Анонс:", val[3:].strip()
            else:
                label, date = "", val
            return _center(
                w,
                ft.Column(
                    [
                        ft.Text(
                            label,
                            size=9,
                            color=_MUTED,
                            text_align=ft.TextAlign.CENTER,
                        ),
                        ft.Text(
                            date,
                            size=10,
                            color=_TEXT,
                            text_align=ft.TextAlign.CENTER,
                            weight=ft.FontWeight.W_500,
                        ),
                    ],
                    spacing=1,
                    tight=True,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            )

        gsm_size = _fmt_dims(gs)
        gsm_diag = str(gs.get("display_size") or "").strip()
        release = _release_label(gs)

        _brand_val = str(merged.get("brand") or "")
        _type_val = str(merged.get("device_type") or "")

        def _open_modal(_=None):
            if self._cache_busy():
                self._warn_cache_busy()
                return
            if page_ctrl:
                self._device_detail_modal(page_ctrl, dict(merged))

        gsm_section = ft.Row(
            [
                ft.Container(
                    thumb, width=_GSM_COLS[0][1], alignment=ft.Alignment(0, 0)
                ),
                _vd(),
                _tc(
                    _brand_val,
                    _COL_W["brand"],
                    bold=True,
                    on_click=lambda _, v=_brand_val: self._filter_quick("brand", v),
                    tooltip=f"Фільтр: {_brand_val}",
                ),
                _vd(),
                _name_cell(
                    merged.get("gsm_name") or merged.get("ek_name"),
                    _COL_W["gsm_name"],
                    on_click=lambda _: _open_modal(),
                    tooltip="Відкрити картку товару",
                ),
                _vd(),
                _tc(
                    gs.get("network_label") or gs.get("target_net"),
                    _COL_W["network"],
                    _MUTED,
                ),
                _vd(),
                _tc(
                    _type_val,
                    _COL_W["type"],
                    _MUTED,
                    on_click=lambda _, v=_type_val: self._filter_quick("type", v),
                    tooltip=f"Фільтр: {_type_val}",
                ),
                _vd(),
                _tc(
                    gs.get("year") or es.get("date_added_year"),
                    _COL_W["year"],
                    _MUTED,
                ),
                _vd(),
                _release_cell(release, _COL_W["release"]),
                _vd(),
                _hl_cell(
                    gsm_size,
                    _COL_W["gsm_size"],
                    gsm_size_colors,
                    on_click=lambda _, v=gsm_size: self._filter_quick("size", v),
                    tooltip=f"Фільтр розміру: {gsm_size}",
                ),
                _vd(),
                _hl_cell(
                    gsm_diag,
                    _COL_W["gsm_diag"],
                    gsm_diag_colors,
                    on_click=lambda _, v=gsm_diag: self._filter_quick("diag", v),
                    tooltip=f"Фільтр діагоналі: {gsm_diag}",
                ),
            ],
            spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # ---- EK section ----
        ek_size = _fmt_dims(es)
        ek_diag = str(es.get("display") or "").strip()
        # Full raw text (e.g. "12 листопада 2021" or "Травень 2025") — not
        # ISO, so it can't be safely truncated to a fixed char count without
        # losing the year; the cell below wraps it onto 2 lines instead.
        ek_date = str(es.get("date_added") or "").strip()
        status = str(es.get("status") or "").strip()
        shops = str(es.get("shops_count") or es.get("price_low") or "").strip()
        price_lo = str(es.get("price_min") or es.get("price_low") or "").strip()
        price_hi = str(es.get("price_max") or "").strip()

        status_color = {
            "Продаж": _OK_DK,
            "Новинка": ft.Colors.BLUE_600,
            "Застарів": ft.Colors.ORANGE_700,
        }.get(status, ft.Colors.BLUE_GREY_400)

        def _price_cell():
            """Split cell: top = lower price, bottom = higher price."""

            def _pfmt(v):
                try:
                    return f"{float(v):,.0f}₴".replace(",", " ")
                except Exception:
                    return v or "—"

            return ft.GestureDetector(
                content=ft.Container(
                    width=80,
                    alignment=ft.Alignment(0, 0),
                    tooltip="Натисніть для перегляду історії цін",
                    content=ft.Column(
                        [
                            ft.Text(
                                _pfmt(price_lo),
                                size=10,
                                color=_OK_DK,
                                text_align=ft.TextAlign.CENTER,
                            ),
                            ft.Container(height=1, bgcolor=_BORDER),
                            ft.Text(
                                _pfmt(price_hi),
                                size=10,
                                color=_MUTED,
                                text_align=ft.TextAlign.CENTER,
                            ),
                        ],
                        spacing=0,
                        tight=True,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ),
                on_tap=lambda _: (
                    self._warn_cache_busy()
                    if self._cache_busy()
                    else (self._price_history_popup(page_ctrl, merged) if page_ctrl else None)
                ),
            )

        ek_section = ft.Row(
            [
                _name_cell(
                    merged.get("ek_name"),
                    _COL_W["ek_name"],
                ),
                _vd(),
                _tc((es.get("target_net") or "").strip(), _COL_W["ek_net"], _MUTED),
                _vd(),
                _center(
                    _COL_W["ek_date"],
                    ft.Text(
                        ek_date or "—",
                        size=10,
                        color=_MUTED,
                        text_align=ft.TextAlign.CENTER,
                        no_wrap=False,
                        max_lines=2,
                    ),
                ),
                _vd(),
                _hl_cell(
                    ek_size,
                    _COL_W["ek_size"],
                    ek_size_colors,
                    on_click=lambda _, v=ek_size: self._filter_quick("size", v),
                    tooltip=f"Фільтр розміру: {ek_size}",
                ),
                _vd(),
                _hl_cell(
                    ek_diag,
                    _COL_W["ek_diag"],
                    ek_diag_colors,
                    on_click=lambda _, v=ek_diag: self._filter_quick("diag", v),
                    tooltip=f"Фільтр діагоналі: {ek_diag}",
                ),
                _vd(),
                _tc(
                    status or "—",
                    _COL_W["ek_status"],
                    status_color,
                    on_click=lambda _, v=status: (
                        self._filter_quick("status", v) if v else None
                    ),
                    tooltip=f"Фільтр статусу: {status}" if status else None,
                ),
                _vd(),
                _tc(shops, _COL_W["ek_shops"], _MUTED),
                _vd(),
                _price_cell(),
            ],
            spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        def _confirm_delete(_=None):
            if not page_ctrl:
                return
            device_id = str(merged.get("id") or "")
            name = str(merged.get("gsm_name") or merged.get("ek_name") or device_id)

            def _do_delete(__):
                page_ctrl.pop_dialog()

                async def _do():
                    ok = await asyncio.to_thread(self.db.delete_gsm_device, device_id)
                    if ok:
                        global _DEVICE_CACHE
                        with _CACHE_LOCK:
                            if _DEVICE_CACHE is not None:
                                _DEVICE_CACHE = [
                                    d
                                    for d in _DEVICE_CACHE
                                    if str(d.get("id") or "") != device_id
                                ]
                        with contextlib.suppress(Exception):
                            await asyncio.to_thread(_save_cache_to_disk)
                        self._remove_devices_in_place({device_id})
                        self._show_snack(f"Видалено: {name}", ft.Colors.ORANGE_700)
                    else:
                        self._show_snack("Помилка видалення", ft.Colors.RED)

                page_ctrl.run_task(_do)

            dlg_del = ft.AlertDialog(
                modal=True,
                title=ft.Row(
                    [
                        ft.Icon(
                            ft.Icons.WARNING_AMBER_ROUNDED,
                            color=ft.Colors.RED_600,
                            size=22,
                        ),
                        ft.Text(
                            self.t("gsm.delete_device_title", "Видалити пристрій?"), size=15, weight=ft.FontWeight.W_700
                        ),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                content=ft.Text(
                    f"«{name}» буде видалено з БД разом з усією історією цін та змін. Цю дію не можна скасувати.",
                    size=13,
                    color=ft.Colors.BLUE_GREY_700,
                    no_wrap=False,
                    width=360,
                ),
                actions=[
                    ft.TextButton(
                        "Скасувати", on_click=lambda _: page_ctrl.pop_dialog()
                    ),
                    ft.FilledButton(
                        self.t("gsm.yes_delete", "Так, видалити"),
                        icon=ft.Icons.DELETE_FOREVER,
                        on_click=_do_delete,
                        style=ft.ButtonStyle(bgcolor=ft.Colors.RED_600),
                    ),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
            page_ctrl.show_dialog(dlg_del)

        delete_btn = ft.Container(
            visible=self.is_superadmin,
            content=ft.IconButton(
                icon=ft.Icons.DELETE_OUTLINE,
                icon_color=ft.Colors.RED_300,
                icon_size=16,
                tooltip=self.t("gsm.delete_device_sa", "Видалити пристрій (SA)"),
                on_click=_confirm_delete,
                style=ft.ButtonStyle(padding=ft.padding.all(4)),
            ),
            width=20,
        )

        # ---- Compare checkbox ----
        _dev_id = str(merged.get("id") or "")

        def _on_cmp_change(e, _did=_dev_id):
            global _COMPARE_SELECTED
            if e.control.value:
                if len(_COMPARE_SELECTED) < _MAX_COMPARE:
                    _COMPARE_SELECTED.add(_did)
                else:
                    e.control.value = False
                    with contextlib.suppress(Exception):
                        e.control.update()
            else:
                _COMPARE_SELECTED.discard(_did)
            cb = _COMPARE_BTN_CB
            if cb:
                cb()

        cmp_cb = ft.Container(
            width=20,
            alignment=ft.Alignment(0, 0),
            content=ft.Checkbox(
                value=_dev_id in _COMPARE_SELECTED,
                on_change=_on_cmp_change,
                visual_density=ft.VisualDensity.COMPACT,
                splash_radius=12,
                scale=0.8,
                fill_color={
                    ft.ControlState.SELECTED: ft.Colors.BLUE_700,
                    ft.ControlState.DEFAULT: ft.Colors.BLUE_GREY_200,
                },
            ),
        )

        # `key=device_id` (in addition to `data=device_id`, used elsewhere
        # for in-place patching) lets Flet's list-diffing (object_patch.py's
        # _compare_lists) match rows by stable logical identity across a
        # reorder instead of falling back to raw Python-object-id matching
        # — a live py-spy dump during a live sort-click freeze showed the
        # main thread stuck inside exactly this diffing path
        # (_refresh_table's self._table_body_ref.update() call), which is
        # the whole point of reordering already-built Control objects
        # instead of rebuilding them (see the flat-mode row-reuse cache) —
        # that optimization only avoided rebuilding the Controls, not the
        # cost of Flet figuring out the reorder itself without a key.
        return ft.Container(
            key=device_id,
            data=device_id,
            border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
            padding=ft.padding.symmetric(horizontal=10, vertical=3),
            content=ft.Row(
                [
                    cmp_cb,
                    gsm_section,
                    ft.Container(width=1, height=72, bgcolor=_BORDER),
                    ek_section,
                    ft.Container(width=1, height=72, bgcolor=_BORDER),
                    delete_btn,
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _remove_devices_in_place(self, ids: set) -> None:
        """Patch already-deleted-from-cache devices out of the currently
        rendered table without a full _refresh_table() rebuild — that used
        to reset scroll position and re-collapse every brand group. Safe to
        call even for devices whose brand group isn't currently expanded
        (their row was never built, so there's nothing to remove there, but
        the per-brand device list still gets corrected for the next time
        that brand IS expanded)."""
        if not ids:
            return
        with _CHART_RENDER_LOCK:
            for did in ids:
                _CHART_RENDER_CACHE.pop(did, None)
        for brand, devs in list(self._brand_devs_by_name.items()):
            if any(str(d.get("id") or "") in ids for d in devs):
                self._brand_devs_by_name[brand] = [
                    d for d in devs if str(d.get("id") or "") not in ids
                ]
            body_container = (self._brand_body_by_name or {}).get(brand)
            if body_container is None or body_container.content is None:
                continue
            controls = body_container.content.controls
            new_controls = [c for c in controls if getattr(c, "data", None) not in ids]
            if len(new_controls) != len(controls):
                body_container.content.controls = new_controls
                with contextlib.suppress(Exception):
                    body_container.update()
        if self._stats_ref is not None:
            with contextlib.suppress(Exception):
                filtered = self._get_filtered_devices()
                self._stats_ref.value = self.t(
                    "gsm.found_simple", "Знайдено: {n} пристроїв"
                ).format(n=len(filtered))
                self._stats_ref.update()

    def _apply_live_parse_updates(self, changed_rows: list[dict] | None) -> None:
        """Patch just the changed devices' rows in place — used by the
        live-refresh-during-parse loop (every ~10s while a parse is running)
        so periodic price/status updates never reset scroll position or
        re-collapse whatever brand groups the user currently has open,
        unlike a full _refresh_table() rebuild."""
        for row in changed_rows or []:
            if not isinstance(row, dict):
                continue
            did = str(row.get("id") or "")
            brand = str(row.get("brand") or "")
            if did and brand:
                with contextlib.suppress(Exception):
                    self._update_device_row_in_place(did, brand, row)

    def _update_device_row_in_place(self, device_id: str, brand: str, updated: dict) -> None:
        """Rebuild and splice in a single row's control after its underlying
        data changed (e.g. its E-Katalog match was removed) — same
        no-full-rebuild rationale as _remove_devices_in_place."""
        devs = self._brand_devs_by_name.get(brand)
        if devs is not None:
            self._brand_devs_by_name[brand] = [
                updated if str(d.get("id") or "") == device_id else d for d in devs
            ]
        body_container = (self._brand_body_by_name or {}).get(brand)
        if body_container is None or body_container.content is None:
            return
        controls = body_container.content.controls
        for i, c in enumerate(controls):
            if getattr(c, "data", None) == device_id:
                with contextlib.suppress(Exception):
                    controls[i] = self._build_table_row(updated, *self._color_maps)
                    body_container.update()
                break

    def _build_pager_controls(self) -> list:
        # Everything is shown on one page (see _page_items) — no pager UI.
        return []

    def _sort_click(self, col: str):
        if self._sort_col != col:
            self._sort_col = col
            self._sort_asc = True
        elif self._sort_asc:
            self._sort_asc = False
        else:
            # 3rd click — reset to default
            self._sort_col = "brand"
            self._sort_asc = True
        self._page_idx = 0
        self._refresh_table(rebuild_filters=False)

    def _filter_quick(self, field: str, value: str):
        """Toggle a quick cell-click filter and refresh."""
        if not value or value == "—":
            return
        if field == "brand":
            if value in self._filter_brands:
                self._filter_brands = [v for v in self._filter_brands if v != value]
            else:
                self._filter_brands = [value]
        elif field == "diag":
            self._filter_diag = "" if self._filter_diag == value else value
        elif field == "size":
            self._filter_size = "" if self._filter_size == value else value
        elif field == "status":
            if value in self._filter_statuses:
                self._filter_statuses = [v for v in self._filter_statuses if v != value]
            else:
                self._filter_statuses = [value]
        elif field == "type":
            if value in self._filter_types:
                self._filter_types = [v for v in self._filter_types if v != value]
            else:
                self._filter_types = [value]
        self._page_idx = 0
        self._refresh_table()

    def _build_results_content(self) -> ft.Control:
        # Ensure theme vars are current (may be called without going through build())
        _th = _th_from_view(self)
        global _SOFT, _ALT, _OK_DK, _OK_BG, _TEXT, _ACCENT, _MUTED, _BORDER, _PANEL
        global _MONITOR_TASKS_ACTIVE
        _SOFT = _th["soft"]
        _ALT = _th["alt"]
        _OK_DK = _th["ok"]
        _OK_BG = _th["ok_bg"]
        _TEXT = _th["text"]
        _ACCENT = _th.get("icon_color", "#1D4ED8")
        _MUTED = _th.get("muted", "#475569")
        _BORDER = _th.get("border", "#CBD5E1")
        _PANEL = _th.get("panel", "#FFFFFF")

        page_ctrl = None
        with contextlib.suppress(RuntimeError, AttributeError):
            page_ctrl = self.content_area.page if self.content_area else None
        self._page_ref = page_ctrl

        devices, cached_ts, loading = _get_cache()
        logger.info(
            "[GSM] _build_results_content: cache=%s page_ctrl=%s loading=%s",
            len(devices) if devices else None,
            page_ctrl is not None,
            loading,
        )

        # Explicit product requirement: ALL loading (disk cache, DB sync if
        # the daily boundary needs it, building every row) happens ONE
        # place — behind the loading indicator, during _reveal_and_sync
        # (auto-started below, see reveal_panel) — not as a separate
        # background task running alongside it. This used to ALSO fire
        # self._background_load directly from here, which meant two
        # separate scheduled tasks (this one, and _reveal_and_sync's own
        # call into the exact same _background_load for the devices-is-
        # None case) could race and compete for the same thread — that
        # redundant second task, not "background loading" as a concept,
        # is what made the tab feel like it was fighting itself. Deleted;
        # _reveal_and_sync/_reveal_sync now fully own this responsibility.
        _should_reveal = True

        filtered = self._get_filtered_devices()
        page_devs = self._page_items(filtered)

        # ---- Toolbar ----
        search_field = ft.TextField(
            hint_text=self.t("gsm.search_hint", "Пошук за брендом або моделлю…"),
            prefix_icon=ft.Icons.SEARCH,
            dense=True,
            border_radius=10,
            width=210,
            text_size=11,
            value=self._search,
            on_submit=lambda e: self._do_search(str(e.control.value or "")),
        )

        stats_text = ft.Text(
            self.t("gsm.found_simple", "Знайдено: {n} пристроїв").format(n=len(filtered)),
            size=10,
            color=_MUTED,
        )
        self._stats_ref = stats_text

        _initially_loading = loading or devices is None
        loading_bar = ft.ProgressBar(
            visible=_initially_loading,
            color=_OK_DK,
            bgcolor=ft.Colors.BLUE_GREY_100,
            height=3,
        )
        self._loading_bar_ref = loading_bar
        loading_status = ft.Text(
            self.t("gsm.loading_devices", "Завантаження пристроїв…"),
            size=10,
            color=_MUTED,
            visible=_initially_loading,
            text_align=ft.TextAlign.CENTER,
        )
        self._loading_status_ref = loading_status

        async def _force_reload_async(_=None):
            # Table stays visible the whole time (point update, not a full
            # reveal-panel hide/show) — but the loading bar/status must
            # stay up until the table is ACTUALLY fully rebuilt, not just
            # until the DB sync itself returns. The old code called a bare
            # self._refresh_table() (fire-and-forget row-fill, same as
            # _reveal_sync used to) and hid the indicator right after —
            # before rows/diagrams had actually finished, which is exactly
            # why it looked done while the table kept popping in/lagging
            # afterward. Reuses the same race-free pipeline as the initial
            # reveal (defer_fill_to_caller + direct await + diagram
            # preload), just without ever touching reveal_panel/
            # table_container visibility.
            self._set_toolbar_busy(True)
            self._update_loading_status(self.t("gsm.syncing", "Синхронізація…"), 0.0)
            if self._loading_bar_ref is not None:
                self._loading_bar_ref.visible = True
                with contextlib.suppress(Exception):
                    self._loading_bar_ref.update()
            if self._loading_status_ref is not None:
                self._loading_status_ref.visible = True
                with contextlib.suppress(Exception):
                    self._loading_status_ref.update()
            try:
                await asyncio.to_thread(self._sync_cache_from_db, True)

                self._reveal_rows_done = 0
                self._reveal_rows_total = 0
                self._reveal_charts_done = 0
                self._reveal_charts_total = 0
                self._refresh_table(defer_fill_to_caller=True)
                pending = self._pending_fill
                self._pending_fill = None
                if pending:
                    _to_fill, color_maps = pending
                    self._reveal_rows_total = sum(len(devs) for _, devs in _to_fill)
                    self._update_reveal_progress_text()
                    await self._fill_expanded_brands_async(_to_fill, color_maps)

                await self._preload_charts_for_current_page()
                await self._warm_photo_cache_from_db()
            finally:
                self._hide_loading_overlay()

        def _force_reload(_=None):
            if page_ctrl:
                page_ctrl.run_task(_force_reload_async)

        # Build presets before toolbar so they can be inserted after "Фільтри"
        presets = (
            self.db.get_gsm_user_presets(self.manager_id) if self.manager_id else []
        )
        preset_options = [
            ft.DropdownOption(
                key=str(p.get("id") or ""), text=str(p.get("preset_name") or "—")
            )
            for p in presets
        ]
        preset_items = {str(p.get("id") or ""): p for p in presets}

        def _on_preset_change(e):
            pid = str(e.control.value or "")
            if pid and pid in preset_items:
                self._load_preset(preset_items[pid])

                async def _do():
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(self.db.save_gsm_last_preset_id, self.manager_id, pid)

                if page_ctrl:
                    page_ctrl.run_task(_do)

        if preset_options:
            _presets_dd = ft.Dropdown(
                hint_text=self.t("gsm.presets_hint", "Мої Фільтри"),
                dense=True,
                width=170,
                border_radius=10,
                bgcolor=_SOFT,
                border_color=_BORDER,
                focused_border_color=_ACCENT,
                options=preset_options,
                value=self._auto_preset_id or None,
                on_select=_on_preset_change,
                text_size=11,
            )
            self._presets_dd_ref = _presets_dd
        else:
            _presets_dd = None
            self._presets_dd_ref = None

        _save_filter_btn = ft.OutlinedButton(
            self.t("gsm.save_filter", "Зберегти фільтр"),
            icon=ft.Icons.BOOKMARK_ADD_OUTLINED,
            on_click=lambda _: (
                self._save_preset_dialog(page_ctrl) if page_ctrl else None
            ),
            style=ft.ButtonStyle(
                padding=ft.padding.symmetric(horizontal=9, vertical=5),
                text_style=ft.TextStyle(size=11),
            ),
        )

        def _delete_selected_preset(_):
            pid = _presets_dd.value if _presets_dd else None
            if not pid or pid not in preset_items:
                return

            async def _do():
                await asyncio.to_thread(self.db.delete_gsm_user_preset, pid)
                with contextlib.suppress(Exception):
                    last = await asyncio.to_thread(self.db.get_gsm_last_preset_id, self.manager_id)
                    if last == pid:
                        await asyncio.to_thread(self.db.save_gsm_last_preset_id, self.manager_id, "")
                self._refresh_table()

            if page_ctrl:
                page_ctrl.run_task(_do)

        _del_preset_btn = ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE,
            icon_size=16,
            icon_color=ft.Colors.RED_400,
            tooltip="Видалити вибраний фільтр",
            on_click=_delete_selected_preset,
            style=ft.ButtonStyle(padding=ft.padding.all(3)),
            visible=bool(_presets_dd),
        )

        # ---- Compare button (shown when ≥2 devices selected) ----
        _cmp_n_init = len(_COMPARE_SELECTED)
        compare_btn = ft.FilledButton(
            self.t("gsm.compare_n", "Порівняти ({n})").format(n=_cmp_n_init) if _cmp_n_init >= 2 else self.t("gsm.compare", "Порівняти"),
            icon=ft.Icons.COMPARE,
            visible=_cmp_n_init >= 2,
            on_click=lambda _: self._comparison_modal(page_ctrl, set(_COMPARE_SELECTED)),
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.BLUE_700,
                padding=ft.padding.symmetric(horizontal=10, vertical=5),
                text_style=ft.TextStyle(size=11),
            ),
        )

        # ---- Bulk delete button (SA only, shown when ≥1 device selected) ----
        # Reuses the same checkbox selection (_COMPARE_SELECTED) as the compare
        # button — check several devices, then either compare or delete them.
        def _confirm_bulk_delete(_=None):
            if not page_ctrl:
                return
            ids = list(_COMPARE_SELECTED)
            n = len(ids)
            if not n:
                return

            def _do_bulk_delete(__):
                page_ctrl.pop_dialog()

                async def _do():
                    ok_count = 0
                    for did in ids:
                        if await asyncio.to_thread(self.db.delete_gsm_device, did):
                            ok_count += 1
                    global _DEVICE_CACHE
                    deleted_ids = set(ids)
                    with _CACHE_LOCK:
                        if _DEVICE_CACHE is not None:
                            _DEVICE_CACHE = [
                                d
                                for d in _DEVICE_CACHE
                                if str(d.get("id") or "") not in deleted_ids
                            ]
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(_save_cache_to_disk)
                    _COMPARE_SELECTED.clear()
                    _update_cmp_btn()
                    self._remove_devices_in_place(deleted_ids)
                    self._show_snack(f"Видалено: {ok_count}/{n}", ft.Colors.ORANGE_700)

                page_ctrl.run_task(_do)

            dlg_bulk_del = ft.AlertDialog(
                modal=True,
                title=ft.Row(
                    [
                        ft.Icon(
                            ft.Icons.WARNING_AMBER_ROUNDED,
                            color=ft.Colors.RED_600,
                            size=22,
                        ),
                        ft.Text(
                            f"Видалити {n} пристроїв?",
                            size=15,
                            weight=ft.FontWeight.W_700,
                        ),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                content=ft.Text(
                    f"Обрані {n} пристрої(в) буде видалено з БД разом з усією "
                    "історією цін та змін. Цю дію не можна скасувати.",
                    size=13,
                    color=ft.Colors.BLUE_GREY_700,
                    no_wrap=False,
                    width=360,
                ),
                actions=[
                    ft.TextButton(
                        "Скасувати", on_click=lambda _: page_ctrl.pop_dialog()
                    ),
                    ft.FilledButton(
                        "Так, видалити",
                        icon=ft.Icons.DELETE_FOREVER,
                        on_click=_do_bulk_delete,
                        style=ft.ButtonStyle(bgcolor=ft.Colors.RED_600),
                    ),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
            page_ctrl.show_dialog(dlg_bulk_del)

        _bulk_del_n_init = len(_COMPARE_SELECTED)
        bulk_delete_btn = ft.FilledButton(
            f"Видалити ({_bulk_del_n_init})" if _bulk_del_n_init else "Видалити вибрані",
            icon=ft.Icons.DELETE_OUTLINE,
            visible=self.is_superadmin and _bulk_del_n_init >= 1,
            on_click=_confirm_bulk_delete,
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.RED_600,
                padding=ft.padding.symmetric(horizontal=10, vertical=5),
                text_style=ft.TextStyle(size=11),
            ),
        )

        global _COMPARE_BTN_CB

        def _update_cmp_btn():
            n = len(_COMPARE_SELECTED)
            compare_btn.visible = n >= 2
            compare_btn.text = self.t("gsm.compare_n", "Порівняти ({n})").format(n=n)
            with contextlib.suppress(Exception):
                compare_btn.update()
            if self.is_superadmin:
                bulk_delete_btn.visible = n >= 1
                bulk_delete_btn.text = f"Видалити ({n})" if n else "Видалити вибрані"
                with contextlib.suppress(Exception):
                    bulk_delete_btn.update()

        _COMPARE_BTN_CB = _update_cmp_btn

        sync_btn = ft.OutlinedButton(
            self.t("gsm.sync_btn", "Синхронізація"),
            icon=ft.Icons.CLOUD_DOWNLOAD_OUTLINED,
            tooltip=self.t("gsm.sync_tooltip", "Примусово оновити кеш з БД (тільки зміни)"),
            on_click=_force_reload,
            style=ft.ButtonStyle(
                color=_ACCENT,
                side=ft.BorderSide(1, _BORDER),
                padding=ft.padding.symmetric(horizontal=10, vertical=5),
                text_style=ft.TextStyle(size=11),
            ),
        )
        self._sync_btn_ref = sync_btn

        left_toolbar_controls = [
            compare_btn,
            bulk_delete_btn,
            search_field,
            sync_btn,
            ft.FilledButton(
                self.t("gsm.filters_btn", "Фільтри"),
                icon=ft.Icons.FILTER_LIST,
                on_click=self._toggle_filter_panel,
                style=ft.ButtonStyle(
                    bgcolor=(
                        _OK_DK
                        if (
                            self._filter_brands
                            or self._filter_types
                            or self._filter_years
                            or self._filter_statuses
                            or self._filter_source
                        )
                        else None
                    ),
                    padding=ft.padding.symmetric(horizontal=10, vertical=5),
                    text_style=ft.TextStyle(size=11),
                ),
            ),
            *([_presets_dd, _del_preset_btn] if _presets_dd else []),
            _save_filter_btn,
        ]

        right_toolbar_controls = []  # stats_text appended last (rightmost) below
        parser_status_row = None  # set below in SA block; inserted in return column

        # SA, or a manager granted the "run_parser" permission: launch / pause
        # / stop parser buttons + status.
        if self.is_superadmin or self.can_run_parser:
            global _PARSER_STATUS_CB, _PARSER_BUTTONS_CB

            global _PARSER_SCHEDULE_STATUS_CB, _PARSER_SCHEDULE_LAUNCH_CB

            _running_now = _parser_is_running()
            _paused_now = _parser_is_paused()
            _status_now = _parser_get_status()
            _schedule_target_now = _parser_schedule_target()

            parser_status_text = ft.Text(
                _status_now,
                size=11,
                color=ft.Colors.RED_600,
                italic=True,
                visible=bool(_status_now),
            )
            self._parser_status_ref = parser_status_text

            schedule_status_text = ft.Text(
                _format_schedule_countdown(_schedule_target_now) if _schedule_target_now else "",
                size=11,
                color=ft.Colors.BLUE_700,
                italic=True,
                visible=bool(_schedule_target_now),
            )

            _launch_menu_label_ref = ft.Ref[ft.Text]()

            def _menu_item(icon, color, label, on_pick):
                return ft.PopupMenuItem(
                    content=ft.Row(
                        [
                            ft.Icon(icon, size=16, color=color),
                            ft.Text(label, size=12),
                        ],
                        spacing=8,
                    ),
                    on_click=on_pick,
                )

            launch_btn = ft.PopupMenuButton(
                content=ft.Container(
                    content=ft.Row(
                        [
                            ft.Icon(ft.Icons.PLAY_ARROW, color=ft.Colors.WHITE, size=16),
                            ft.Text(
                                self.t("gsm.parser_running", "Парсинг…") if _running_now else self.t("gsm.parser_start", "Запустити"),
                                ref=_launch_menu_label_ref,
                                size=11,
                                color=ft.Colors.WHITE,
                                weight=ft.FontWeight.W_600,
                            ),
                            ft.Icon(ft.Icons.ARROW_DROP_DOWN, color=ft.Colors.WHITE, size=18),
                        ],
                        spacing=2,
                        tight=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    bgcolor=ft.Colors.ORANGE_700,
                    padding=ft.padding.symmetric(horizontal=10, vertical=6),
                    border_radius=6,
                ),
                menu_position=ft.PopupMenuPosition.UNDER,
                disabled=_running_now,
                items=[
                    _menu_item(
                        ft.Icons.ALL_INCLUSIVE, ft.Colors.ORANGE_700,
                        self.t("gsm.menu.full_parse", "Повний парсинг (ГСМ + ЄК)"),
                        lambda e: page_ctrl and page_ctrl.run_task(_launch_async, e, "full"),
                    ),
                    _menu_item(
                        ft.Icons.PHONE_ANDROID_OUTLINED, ft.Colors.INDIGO_700,
                        self.t("gsm.menu.gsm_only", "Тільки GSM Arena"),
                        lambda e: page_ctrl and page_ctrl.run_task(_launch_async, e, "gsm"),
                    ),
                    _menu_item(
                        ft.Icons.STORE_OUTLINED, ft.Colors.TEAL_700,
                        self.t("gsm.menu.ek_only", "Тільки E-Katalog"),
                        lambda e: page_ctrl and page_ctrl.run_task(_launch_async, e, "ekatalog"),
                    ),
                    _menu_item(
                        ft.Icons.SYNC, ft.Colors.PURPLE_700,
                        self.t("gsm.menu.xiaomi_regions", "Регіони Xiaomi"),
                        lambda e: page_ctrl and page_ctrl.run_task(_run_xiaomi_sync, e),
                    ),
                    _menu_item(
                        ft.Icons.SCHEDULE, ft.Colors.BLUE_700,
                        self.t("gsm.schedule.menu_item", "Запланувати запуск…"),
                        lambda e: _open_schedule_dialog(e),
                    ),
                ],
            )
            self._launch_menu_ref = launch_btn

            pause_btn = ft.FilledButton(
                self.t("gsm.continue", "Продовжити") if _paused_now else self.t("gsm.pause", "Пауза"),
                icon=ft.Icons.PLAY_CIRCLE if _paused_now else ft.Icons.PAUSE,
                visible=_running_now,
                on_click=lambda e: _pause_click(e),
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.BLUE_700,
                    padding=ft.padding.symmetric(horizontal=10, vertical=5),
                    text_style=ft.TextStyle(size=11),
                ),
            )

            stop_btn = ft.FilledButton(
                self.t("gsm.stop", "Стоп"),
                icon=ft.Icons.STOP,
                visible=_running_now,
                on_click=lambda e: _stop_click(e),
                style=ft.ButtonStyle(
                    bgcolor=ft.Colors.RED_700,
                    padding=ft.padding.symmetric(horizontal=10, vertical=5),
                    text_style=ft.TextStyle(size=11),
                ),
            )

            # ── Register UI callbacks so the running async task can update THIS view ──
            global _PARSER_REFRESH_TABLE_CB
            _PARSER_REFRESH_TABLE_CB = self._apply_live_parse_updates

            def _ui_status_cb(msg: str) -> None:
                parser_status_text.value = msg
                parser_status_text.visible = bool(msg)
                with contextlib.suppress(Exception):
                    parser_status_row.update()

            def _ui_buttons_cb(running: bool, paused: bool) -> None:
                launch_btn.disabled = running
                if _launch_menu_label_ref.current:
                    _launch_menu_label_ref.current.value = (
                        self.t("gsm.parser_running", "Парсинг…") if running else self.t("gsm.parser_start", "Запустити")
                    )
                pause_btn.visible = running
                pause_btn.text = self.t("gsm.continue", "Продовжити") if paused else self.t("gsm.pause", "Пауза")
                pause_btn.icon = ft.Icons.PLAY_CIRCLE if paused else ft.Icons.PAUSE
                stop_btn.visible = running
                with contextlib.suppress(Exception):
                    launch_btn.update()
                with contextlib.suppress(Exception):
                    pause_btn.update()
                with contextlib.suppress(Exception):
                    stop_btn.update()
                # Auto-reveal table when parser starts so live refresh is visible
                # without requiring the user to click "Показати результати".
                if running:
                    with contextlib.suppress(Exception):
                        _panel = _reveal_refs.get("panel")
                        if _panel is not None and _panel.visible:
                            _reveal_click(None)

            def _ui_schedule_status_cb(msg: str) -> None:
                schedule_status_text.value = msg
                schedule_status_text.visible = bool(msg)
                with contextlib.suppress(Exception):
                    parser_status_row.update()

            _PARSER_STATUS_CB = _ui_status_cb
            _PARSER_BUTTONS_CB = _ui_buttons_cb
            _PARSER_SCHEDULE_STATUS_CB = _ui_schedule_status_cb

            parser_status_row = ft.Row([parser_status_text, schedule_status_text])

            # If parser was already running when this view was built (e.g. after
            # account switch or tab navigation), re-attach the floating badge.
            if _running_now and page_ctrl:
                _show_parser_banner(page_ctrl)

            def _gsm_is_active_view() -> bool:
                """Return True when the GSM parser content area is mounted on a page.

                Previously used page._active_view == 'show_gsm_parser', but that
                attribute is only set in dashboard.py (regular users), NOT in
                superadmin_dashboard.py. Checking that the content_area has a page
                is sufficient: controls hidden with visible=False are still safe to
                update in Flet — Flutter skips rendering invisible subtrees.
                """
                try:
                    return bool(
                        self.content_area and self.content_area.page
                    )
                except Exception:
                    # `.page` raises RuntimeError (not None) once the control
                    # has been detached from the tree (e.g. user switched to
                    # another tab) — that means NOT active, not "assume active".
                    return False

            async def _progress_monitor():
                while not _PARSER_DONE_FLAG["done"]:
                    await asyncio.sleep(0.4)
                    try:
                        # Drain the entire queue — only show the most recent message.
                        # Without draining, the queue accumulates hundreds of messages
                        # at 350+ devices, causing asyncio to stay busy and the UI to freeze.
                        msg = None
                        q = _PARSER_PROGRESS_Q
                        if q is not None:
                            while True:
                                try:
                                    msg = q.get_nowait()
                                except Exception:
                                    break
                        if msg is not None:
                            _update_parser_banner(page_ctrl, msg)
                            # Only push status text when this view is active —
                            # updating detached controls while another page is shown
                            # sends stale Flet→Flutter messages that corrupt scroll state.
                            if _gsm_is_active_view():
                                _parser_set_status(msg)
                    except Exception:
                        pass

            async def _live_table_refresh():
                while not _PARSER_DONE_FLAG["done"]:
                    await asyncio.sleep(10)
                    if _PARSER_DONE_FLAG["done"]:
                        break
                    try:
                        # Diff sync only — pulls what the running parser just
                        # wrote, not the full 50k-row table every 10s.
                        changed_rows = await asyncio.to_thread(
                            self._sync_cache_from_db, False
                        )
                        # Only patch rows when this view is visible — refreshing
                        # detached controls corrupts the active page's scroll
                        # state in Flutter. Patches just the changed rows in
                        # place (never a full _refresh_table()) so this doesn't
                        # reset scroll position or collapse the user's currently
                        # expanded brand groups every ~10s during a parse run.
                        if _gsm_is_active_view():
                            cb = _PARSER_REFRESH_TABLE_CB
                            if cb:
                                cb(changed_rows)
                    except Exception:
                        pass

            # Re-attach monitor tasks when entering Results tab with parser already running.
            # Guard _MONITOR_TASKS_ACTIVE to prevent duplicate task spawns.
            if _running_now and page_ctrl and not _MONITOR_TASKS_ACTIVE:
                _MONITOR_TASKS_ACTIVE = True
                page_ctrl.run_task(_progress_monitor)
                page_ctrl.run_task(_live_table_refresh)

            async def _launch_async(_, mode: str = "full"):
                global _PARSER_RUNNING, _PARSER_PAUSED, _PARSER_STATUS
                global _PARSER_STOP_EVENT, _PARSER_PAUSE_EVENT, _PARSER_PROGRESS_Q
                global _MONITOR_TASKS_ACTIVE
                if _parser_is_running():
                    return
                with _PARSER_LOCK:
                    _PARSER_RUNNING = True
                    _PARSER_PAUSED = False
                    _PARSER_STATUS = "Запускаємо парсер…"
                    _PARSER_STOP_EVENT = threading.Event()
                    _PARSER_PAUSE_EVENT = threading.Event()
                    _PARSER_PAUSE_EVENT.set()  # not paused initially
                    _PARSER_PROGRESS_Q = queue.Queue()
                    _PARSER_DONE_FLAG["done"] = False
                _parser_notify_buttons(True, False)
                _parser_set_status("Запускаємо парсер…")
                _show_parser_banner(page_ctrl)
                _MONITOR_TASKS_ACTIVE = True
                if page_ctrl:
                    page_ctrl.run_task(_progress_monitor)
                    page_ctrl.run_task(_live_table_refresh)
                try:
                    _infra = {}
                    _svc = {}
                    with contextlib.suppress(Exception):
                        _infra = self.db.get_infrastructure_settings()
                    with contextlib.suppress(Exception):
                        _svc = self.db.get_parser_service_config()
                    _vps_mode = str(_infra.get("mode") or "local") == "vps"
                    _vps_url = (
                        str(_svc.get("vps_service_url") or "").strip().rstrip("/")
                    )
                    _vps_key = str(_svc.get("vps_api_key") or "").strip()

                    if _vps_mode and _vps_url:
                        import httpx

                        _parser_set_status("Надсилаємо запит на VPS…")
                        headers = {"x-api-key": _vps_key} if _vps_key else {}
                        async with httpx.AsyncClient() as _hc:
                            _r = await _hc.post(
                                f"{_vps_url}/trigger",
                                json={"mode": mode},
                                headers=headers,
                                timeout=20,
                            )
                        _rj = _r.json() if _r.status_code == 200 else {}
                        ok = _r.status_code == 200 and bool(_rj.get("ok"))
                        result_msg = _rj.get("message", "") or f"HTTP {_r.status_code}"
                        self._show_snack(
                            (
                                f"VPS: {result_msg}"
                                if ok
                                else f"VPS помилка: {result_msg}"
                            ),
                            ft.Colors.GREEN if ok else ft.Colors.RED,
                        )
                    else:
                        from gsm_parser_runner import run_gsm_parser

                        _stop_ev = _PARSER_STOP_EVENT
                        _pause_ev = _PARSER_PAUSE_EVENT

                        def _cb(msg: str):
                            _PARSER_PROGRESS_Q.put(msg)

                        ok, result_msg = await asyncio.to_thread(
                            run_gsm_parser,
                            "__global__",
                            "",
                            "",
                            mode,
                            True,
                            _cb,
                            _stop_ev,
                            _pause_ev,
                        )
                        if ok:
                            _snack_msg, _snack_color = (
                                "Парсинг завершено ✓",
                                ft.Colors.GREEN,
                            )
                        elif result_msg == "Зупинено":
                            _snack_msg, _snack_color = (
                                "Парсинг зупинено",
                                ft.Colors.ORANGE_700,
                            )
                        else:
                            _snack_msg, _snack_color = (
                                f"Помилка: {result_msg}",
                                ft.Colors.RED,
                            )
                        self._show_snack(_snack_msg, _snack_color)
                except Exception as exc:
                    self._show_snack(f"Помилка запуску: {exc}", ft.Colors.RED)
                finally:
                    with _PARSER_LOCK:
                        _PARSER_RUNNING = False
                        _PARSER_PAUSED = False
                    _PARSER_DONE_FLAG["done"] = True
                    _MONITOR_TASKS_ACTIVE = False
                    _parser_notify_buttons(False, False)
                    _parser_set_status("")
                    _hide_parser_banner(page_ctrl)
                    if self._log_text_ref is not None:
                        with contextlib.suppress(Exception):
                            from gsm_parser_runner import LAST_PARSE_LOG_PATH

                            self._log_text_ref.value = LAST_PARSE_LOG_PATH.read_text(
                                encoding="utf-8"
                            )
                            self._log_text_ref.update()
                    global _DEVICE_CACHE, _CACHE_TRIGGER_TS
                    with _CACHE_LOCK:
                        _DEVICE_CACHE = None
                    if page_ctrl:
                        page_ctrl.run_task(self._background_load, page_ctrl)

            global _PARSER_SCHEDULE_LAUNCH_CB
            _PARSER_SCHEDULE_LAUNCH_CB = _launch_async

            def _pause_click(_):
                global _PARSER_PAUSED
                with _PARSER_LOCK:
                    _PARSER_PAUSED = not _PARSER_PAUSED
                    _paused = _PARSER_PAUSED
                if _PARSER_PAUSE_EVENT:
                    if _paused:
                        _PARSER_PAUSE_EVENT.clear()
                    else:
                        _PARSER_PAUSE_EVENT.set()
                _parser_notify_buttons(True, _paused)

            def _stop_click(_):
                if _PARSER_STOP_EVENT:
                    _PARSER_STOP_EVENT.set()
                if _PARSER_PAUSE_EVENT:
                    _PARSER_PAUSE_EVENT.set()  # unblock pause so thread can exit
                _parser_set_status("Зупинка…")

            def _open_schedule_dialog(_e=None):
                page = self._get_page_safe()
                if not page:
                    return

                _mode_state = {"value": _PARSER_SCHEDULE_MODE or "full"}

                def _schedule_help(_=None):
                    from views.help_utils import show_modal_help
                    show_modal_help(page, self.t("gsm.schedule.help_title", "Довідка — Запланований запуск"), [
                        self.t("gsm.schedule.help.desc", "Дозволяє запустити парсер не одразу, а через заданий час — зручно, якщо ви запускаєте парсинг вручну вночі і хочете, щоб зміни зафіксувались уже завтрашнім днем."),
                        (self.t("gsm.schedule.help.steps_title", "Як користуватись"), None),
                        self.t("gsm.schedule.help.step1", "1. Оберіть режим запуску (повний / лише GSM / лише E-Katalog)."),
                        self.t("gsm.schedule.help.step2", "2. Натисніть одну з кнопок швидкого вибору або вкажіть затримку від зараз у полях «Години» / «Хвилини» (наприклад, 1 год 30 хв — запуск через півтори години, а не о 1:30)."),
                        self.t("gsm.schedule.help.step3", "3. Натисніть «Запланувати» — відлік з'явиться біля кнопки «Запустити»."),
                        self.t("gsm.schedule.help.step4", "4. Застосунок має лишатись відкритим — запуск станеться автоматично, коли настане заданий час."),
                        self.t("gsm.schedule.help.step5", "5. «Скасувати заплановий запуск» прибирає таймер без запуску парсера."),
                    ])

                def _on_mode_change(e):
                    _mode_state["value"] = e.control.value

                mode_dd = ft.Dropdown(
                    label=self.t("gsm.schedule.mode_label", "Режим запуску"),
                    value=_mode_state["value"],
                    width=280,
                    options=[
                        ft.dropdown.Option("full", self.t("gsm.menu.full_parse", "Повний парсинг (ГСМ + ЄК)")),
                        ft.dropdown.Option("gsm", self.t("gsm.menu.gsm_only", "Тільки GSM Arena")),
                        ft.dropdown.Option("ekatalog", self.t("gsm.menu.ek_only", "Тільки E-Katalog")),
                    ],
                )
                mode_dd.on_select = _on_mode_change
                mode_dd.on_change = _on_mode_change

                hours_tf = ft.TextField(
                    label=self.t("gsm.schedule.hours_label", "Години"),
                    value="0", width=90, dense=True,
                    keyboard_type=ft.KeyboardType.NUMBER,
                )
                minutes_tf = ft.TextField(
                    label=self.t("gsm.schedule.minutes_label", "Хвилини"),
                    value="0", width=90, dense=True,
                    keyboard_type=ft.KeyboardType.NUMBER,
                )

                _active_ts = _parser_schedule_target()
                active_status_text = ft.Text(
                    _format_schedule_countdown(_active_ts)
                    if _active_ts
                    else self.t("gsm.schedule.none_active", "Запуск не заплановано"),
                    size=12,
                    color=ft.Colors.BLUE_700 if _active_ts else ft.Colors.GREY_600,
                    italic=True,
                )

                def _close_dlg(_=None):
                    with contextlib.suppress(Exception):
                        page.pop_dialog()

                def _do_schedule(delay_seconds: float):
                    ok = _parser_start_schedule(page_ctrl, delay_seconds, _mode_state["value"])
                    _close_dlg()
                    if ok:
                        _target = _parser_schedule_target() or (time.time() + delay_seconds)
                        _, _left = _schedule_countdown_parts(_target)
                        self._show_snack(
                            self.t("gsm.schedule.scheduled_snack", "Заплановано запуск через {left}").format(
                                left=_left
                            ),
                            ft.Colors.BLUE_700,
                        )

                def _cancel_schedule_click(_=None):
                    _parser_cancel_schedule()
                    _close_dlg()
                    self._show_snack(
                        self.t("gsm.schedule.cancelled_snack", "Запланований запуск скасовано"),
                        ft.Colors.GREY_700,
                    )

                def _quick_btn(label: str, seconds: int):
                    return ft.OutlinedButton(
                        label, on_click=lambda e, s=seconds: _do_schedule(s),
                    )

                def _confirm_custom(_=None):
                    try:
                        h = int(float(hours_tf.value or 0))
                        m = int(float(minutes_tf.value or 0))
                    except Exception:
                        h, m = 0, 0
                    total = h * 3600 + m * 60
                    if total <= 0:
                        self._show_snack(
                            self.t("gsm.schedule.invalid_delay", "Вкажіть час більше нуля"),
                            ft.Colors.RED,
                        )
                        return
                    _do_schedule(total)

                dlg = ft.AlertDialog(
                    modal=True,
                    title=ft.Row(
                        [
                            ft.Text(self.t("gsm.schedule.dialog_title", "Запланувати запуск парсера"), weight="bold", expand=True),
                            ft.IconButton(ft.Icons.HELP_OUTLINE, icon_size=18, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_schedule_help),
                        ],
                    ),
                    content=ft.Container(
                        width=380,
                        content=ft.Column(
                            [
                                active_status_text,
                                ft.Divider(height=10),
                                mode_dd,
                                ft.Container(height=6),
                                ft.Text(self.t("gsm.schedule.quick_label", "Швидкий вибір:"), size=12),
                                ft.Row(
                                    [
                                        _quick_btn(self.t("gsm.schedule.quick_15m", "+15 хв"), 15 * 60),
                                        _quick_btn(self.t("gsm.schedule.quick_30m", "+30 хв"), 30 * 60),
                                        _quick_btn(self.t("gsm.schedule.quick_1h", "+1 год"), 60 * 60),
                                        _quick_btn(self.t("gsm.schedule.quick_2h", "+2 год"), 2 * 60 * 60),
                                        _quick_btn(self.t("gsm.schedule.quick_4h", "+4 год"), 4 * 60 * 60),
                                    ],
                                    wrap=True, spacing=6,
                                ),
                                ft.Container(height=6),
                                ft.Text(self.t("gsm.schedule.custom_label", "Або вкажіть затримку від зараз:"), size=12),
                                ft.Row([hours_tf, minutes_tf, ft.FilledButton(
                                    self.t("gsm.schedule.confirm_btn", "Запланувати"),
                                    on_click=_confirm_custom,
                                )], spacing=8),
                            ],
                            spacing=8, tight=True,
                        ),
                    ),
                    actions=[
                        *(
                            [ft.TextButton(
                                self.t("gsm.schedule.cancel_active_btn", "Скасувати запланований запуск"),
                                on_click=_cancel_schedule_click,
                                style=ft.ButtonStyle(color=ft.Colors.RED_700),
                            )]
                            if _active_ts
                            else []
                        ),
                        ft.TextButton(self.t("common.close", "Закрити"), on_click=_close_dlg),
                    ],
                )
                with contextlib.suppress(Exception):
                    page.open(dlg)

            right_toolbar_controls.append(launch_btn)
            right_toolbar_controls.append(pause_btn)
            right_toolbar_controls.append(stop_btn)
            # parser_status_text is rendered as a separate row below the toolbar

            # ---- Sync Xiaomi regions button (SA only) ----
            _sync_status_text = ft.Text(
                "", size=11, color=ft.Colors.TEAL_700, italic=True, visible=False
            )

            async def _run_xiaomi_sync(_):
                _sync_status_text.value = "Синхронізація регіонів Xiaomi…"
                _sync_status_text.visible = True
                with contextlib.suppress(Exception):
                    _sync_status_text.update()
                try:
                    from datetime import datetime as _dt

                    from gsm_parser_runner import LAST_PARSE_LOG_PATH
                    from xiaomi_regions_sync import sync_xiaomi_regions

                    # Append to the existing parser log (don't clear it)
                    def _slog(m: str):
                        line = f"[{_dt.now().strftime('%H:%M:%S')}] {m}"
                        with contextlib.suppress(Exception):
                            with LAST_PARSE_LOG_PATH.open("a", encoding="utf-8") as _lf:
                                _lf.write(line + "\n")

                    # Header separator in log
                    with contextlib.suppress(Exception):
                        with LAST_PARSE_LOG_PATH.open("a", encoding="utf-8") as _lf:
                            _lf.write(
                                f"\n[{_dt.now().strftime('%H:%M:%S')}]"
                                f" === Синхронізація регіонів Xiaomi ==="
                                f" {_dt.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n"
                            )

                    stats = await asyncio.to_thread(
                        sync_xiaomi_regions, self.db, _slog
                    )
                    result_line = (
                        f"Регіони Xiaomi: збіглось {stats['matched']}, "
                        f"оновлено {stats['updated']}, "
                        f"не знайдено {stats['not_found']}"
                    )
                    _sync_status_text.value = result_line
                    self._show_snack(result_line, ft.Colors.TEAL_700)
                    if self._log_text_ref is not None:
                        with contextlib.suppress(Exception):
                            self._log_text_ref.value = LAST_PARSE_LOG_PATH.read_text(
                                encoding="utf-8"
                            )
                            self._log_text_ref.update()
                except Exception as exc:
                    _sync_status_text.value = f"Помилка синхронізації: {exc}"
                    self._show_snack(f"Помилка: {exc}", ft.Colors.RED)
                finally:
                    with contextlib.suppress(Exception):
                        _sync_status_text.update()

            # ---- Clear all data button (SA only) — brand-selective ----
            def _confirm_clear(_):
                all_brands = list(_FILTER_OPTIONS.get("brands") or [])
                if not all_brands:
                    devices_raw, _, _ = _get_cache()
                    if devices_raw:
                        all_brands = sorted({str(d.get("brand") or "") for d in devices_raw if d.get("brand")})

                # Checkbox state: all selected by default
                brand_checked: dict[str, list] = {b: [True] for b in all_brands}
                cb_controls: dict[str, ft.Checkbox] = {}

                def _get_selected_brands():
                    return [b for b in all_brands if brand_checked[b][0]]

                def _on_brand_cb(e, b=None):
                    brand_checked[b][0] = e.control.value
                    confirm_btn.disabled = not _get_selected_brands()
                    with contextlib.suppress(Exception):
                        confirm_btn.update()

                confirm_btn = ft.FilledButton(
                    "Видалити вибрані",
                    icon=ft.Icons.DELETE_FOREVER,
                    style=ft.ButtonStyle(bgcolor=ft.Colors.RED_600),
                    on_click=None,  # set below
                )

                def _do_clear(_):
                    sel = _get_selected_brands()
                    if not sel:
                        return
                    page_ctrl.pop_dialog()

                    async def _do():
                        is_all = set(sel) == set(all_brands)
                        if is_all:
                            result = await asyncio.to_thread(self.db.clear_gsm_parser_all_data)
                        else:
                            result = await asyncio.to_thread(self.db.clear_gsm_parser_brands_data, sel)
                        if result.get("ok"):
                            global _DEVICE_CACHE, _CACHE_TRIGGER_TS
                            with _CACHE_LOCK:
                                _DEVICE_CACHE = None
                                _CACHE_TRIGGER_TS = None
                            brands_str = ", ".join(sel[:3]) + ("…" if len(sel) > 3 else "")
                            self._show_snack(
                                f"Видалено [{brands_str}]: {result.get('devices', 0)} пристроїв, "
                                f"{result.get('changes', 0)} змін",
                                ft.Colors.GREEN,
                            )
                            self._refresh_table()
                        else:
                            self._show_snack("Помилка очищення даних", ft.Colors.RED)

                    page_ctrl.run_task(_do)

                confirm_btn.on_click = _do_clear

                def _toggle_all(val: bool):
                    for b in all_brands:
                        brand_checked[b][0] = val
                        cb = cb_controls.get(b)
                        if cb:
                            cb.value = val
                            with contextlib.suppress(Exception):
                                cb.update()
                    confirm_btn.disabled = not val
                    with contextlib.suppress(Exception):
                        confirm_btn.update()

                brand_rows = []
                for b in all_brands:
                    cb = ft.Checkbox(
                        value=True,
                        label=b,
                        label_style=ft.TextStyle(size=12),
                        on_change=lambda e, _b=b: _on_brand_cb(e, _b),
                    )
                    cb_controls[b] = cb
                    brand_rows.append(cb)

                confirm_dlg = ft.AlertDialog(
                    modal=True,
                    title=ft.Row(
                        [
                            ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=ft.Colors.RED_600, size=26),
                            ft.Text("Очистити дані брендів?", size=15, weight=ft.FontWeight.W_700),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    content=ft.Column(
                        [
                            ft.Text("Оберіть бренди для видалення (всі дані: GSM Arena, ЄК, зміни, статистика):", size=12, color=ft.Colors.BLUE_GREY_700),
                            ft.Row(
                                [
                                    ft.TextButton("Всі", on_click=lambda _: _toggle_all(True), style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=6, vertical=2))),
                                    ft.TextButton("Жоден", on_click=lambda _: _toggle_all(False), style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=6, vertical=2))),
                                ],
                                spacing=4,
                            ),
                            ft.Container(
                                content=ft.Column(brand_rows, spacing=2, tight=True, scroll=ft.ScrollMode.AUTO),
                                height=min(280, len(all_brands) * 34 + 8),
                                border=ft.border.all(1, ft.Colors.RED_200),
                                border_radius=8,
                                bgcolor=ft.Colors.RED_50,
                                padding=ft.padding.symmetric(horizontal=10, vertical=6),
                            ),
                            ft.Text("Ця дія незворотна!", size=11, color=ft.Colors.RED_700, weight=ft.FontWeight.W_600),
                        ],
                        spacing=8,
                        tight=True,
                        width=340,
                    ),
                    actions=[
                        ft.TextButton("Скасувати", on_click=lambda _: page_ctrl.pop_dialog()),
                        confirm_btn,
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
                if page_ctrl:
                    page_ctrl.show_dialog(confirm_dlg)

            right_toolbar_controls.append(
                ft.OutlinedButton(
                    "Очистити дані",
                    icon=ft.Icons.DELETE_SWEEP_OUTLINED,
                    on_click=_confirm_clear,
                    style=ft.ButtonStyle(
                        color=ft.Colors.RED_600,
                        side=ft.BorderSide(1, ft.Colors.RED_300),
                        padding=ft.padding.symmetric(horizontal=10, vertical=5),
                        text_style=ft.TextStyle(size=11),
                    ),
                )
            )

            # ---- Clear EK-only data button — brand-selective ----
            def _confirm_clear_ek(_):
                all_brands = list(_FILTER_OPTIONS.get("brands") or [])
                if not all_brands:
                    devices_raw, _, _ = _get_cache()
                    if devices_raw:
                        all_brands = sorted({str(d.get("brand") or "") for d in devices_raw if d.get("brand")})

                brand_checked_ek: dict[str, list] = {b: [True] for b in all_brands}
                cb_controls_ek: dict[str, ft.Checkbox] = {}

                def _get_sel():
                    return [b for b in all_brands if brand_checked_ek[b][0]]

                ek_confirm_btn = ft.FilledButton(
                    "Очистити ЄК вибраних",
                    icon=ft.Icons.DELETE_SWEEP,
                    style=ft.ButtonStyle(bgcolor=ft.Colors.ORANGE_700),
                    on_click=None,
                )

                def _on_cb(e, b=None):
                    brand_checked_ek[b][0] = e.control.value
                    ek_confirm_btn.disabled = not _get_sel()
                    with contextlib.suppress(Exception):
                        ek_confirm_btn.update()

                def _do_clear_ek(_):
                    sel = _get_sel()
                    if not sel:
                        return
                    page_ctrl.pop_dialog()

                    async def _do():
                        is_all = set(sel) == set(all_brands)
                        if is_all:
                            result = await asyncio.to_thread(self.db.clear_gsm_parser_ek_data)
                        else:
                            result = await asyncio.to_thread(self.db.clear_gsm_parser_ek_brands_data, sel)
                        if result.get("ok"):
                            global _DEVICE_CACHE, _CACHE_TRIGGER_TS
                            with _CACHE_LOCK:
                                _DEVICE_CACHE = None
                                _CACHE_TRIGGER_TS = None
                            brands_str = ", ".join(sel[:3]) + ("…" if len(sel) > 3 else "")
                            self._show_snack(
                                f"Очищено ЄК [{brands_str}]: {result.get('devices', 0)} пристроїв, "
                                f"{result.get('changes', 0)} змін",
                                ft.Colors.GREEN,
                            )
                            self._refresh_table()
                        else:
                            self._show_snack("Помилка очищення даних ЄК", ft.Colors.RED)

                    page_ctrl.run_task(_do)

                ek_confirm_btn.on_click = _do_clear_ek

                def _toggle_all_ek(val: bool):
                    for b in all_brands:
                        brand_checked_ek[b][0] = val
                        cb = cb_controls_ek.get(b)
                        if cb:
                            cb.value = val
                            with contextlib.suppress(Exception):
                                cb.update()
                    ek_confirm_btn.disabled = not val
                    with contextlib.suppress(Exception):
                        ek_confirm_btn.update()

                brand_rows_ek = []
                for b in all_brands:
                    cb = ft.Checkbox(
                        value=True,
                        label=b,
                        label_style=ft.TextStyle(size=12),
                        on_change=lambda e, _b=b: _on_cb(e, _b),
                    )
                    cb_controls_ek[b] = cb
                    brand_rows_ek.append(cb)

                ek_confirm_dlg = ft.AlertDialog(
                    modal=True,
                    title=ft.Row(
                        [
                            ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=ft.Colors.ORANGE_700, size=26),
                            ft.Text("Очистити дані ЄК?", size=15, weight=ft.FontWeight.W_700),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    content=ft.Column(
                        [
                            ft.Text("Тільки дані E-Katalog (GSM Arena не зачіпається). Оберіть бренди:", size=12, color=ft.Colors.BLUE_GREY_700),
                            ft.Row(
                                [
                                    ft.TextButton("Всі", on_click=lambda _: _toggle_all_ek(True), style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=6, vertical=2))),
                                    ft.TextButton("Жоден", on_click=lambda _: _toggle_all_ek(False), style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=6, vertical=2))),
                                ],
                                spacing=4,
                            ),
                            ft.Container(
                                content=ft.Column(brand_rows_ek, spacing=2, tight=True, scroll=ft.ScrollMode.AUTO),
                                height=min(280, len(all_brands) * 34 + 8),
                                border=ft.border.all(1, ft.Colors.ORANGE_200),
                                border_radius=8,
                                bgcolor=ft.Colors.ORANGE_50,
                                padding=ft.padding.symmetric(horizontal=10, vertical=6),
                            ),
                        ],
                        spacing=8,
                        tight=True,
                        width=340,
                    ),
                    actions=[
                        ft.TextButton("Скасувати", on_click=lambda _: page_ctrl.pop_dialog()),
                        ek_confirm_btn,
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
                if page_ctrl:
                    page_ctrl.show_dialog(ek_confirm_dlg)

            right_toolbar_controls.append(
                ft.OutlinedButton(
                    "Очистити ЄК",
                    icon=ft.Icons.STORE_OUTLINED,
                    on_click=_confirm_clear_ek,
                    style=ft.ButtonStyle(
                        color=ft.Colors.ORANGE_700,
                        side=ft.BorderSide(1, ft.Colors.ORANGE_300),
                        padding=ft.padding.symmetric(horizontal=10, vertical=5),
                        text_style=ft.TextStyle(size=11),
                    ),
                )
            )

            # ---- Clear price/shop-count HISTORY only (SA only) — brand-selective.
            # Unlike "Очистити дані"/"Очистити ЄК" above, this leaves devices and
            # GSM Arena data untouched: it only wipes the min/max EK price + shop
            # count history (gsm_parser_daily_history columns + the matching
            # gsm_parser_change_history rows) that feeds the price/shops chart.
            def _confirm_clear_history(_):
                all_brands = list(_FILTER_OPTIONS.get("brands") or [])
                if not all_brands:
                    devices_raw, _, _ = _get_cache()
                    if devices_raw:
                        all_brands = sorted({str(d.get("brand") or "") for d in devices_raw if d.get("brand")})

                brand_checked_hist: dict[str, list] = {b: [True] for b in all_brands}
                cb_controls_hist: dict[str, ft.Checkbox] = {}

                def _get_sel_hist():
                    return [b for b in all_brands if brand_checked_hist[b][0]]

                hist_confirm_btn = ft.FilledButton(
                    "Видалити історію вибраних",
                    icon=ft.Icons.HISTORY_TOGGLE_OFF,
                    style=ft.ButtonStyle(bgcolor=ft.Colors.RED_600),
                    on_click=None,
                )

                def _on_cb_hist(e, b=None):
                    brand_checked_hist[b][0] = e.control.value
                    hist_confirm_btn.disabled = not _get_sel_hist()
                    with contextlib.suppress(Exception):
                        hist_confirm_btn.update()

                def _do_clear_history(_):
                    sel = _get_sel_hist()
                    if not sel:
                        return
                    page_ctrl.pop_dialog()

                    async def _do():
                        is_all = set(sel) == set(all_brands)
                        result = await asyncio.to_thread(
                            self.db.clear_gsm_price_shop_history,
                            None if is_all else sel,
                        )
                        if result.get("ok"):
                            global _DEVICE_CACHE, _CACHE_TRIGGER_TS
                            with _CACHE_LOCK:
                                _DEVICE_CACHE = None
                                _CACHE_TRIGGER_TS = None
                            brands_str = ", ".join(sel[:3]) + ("…" if len(sel) > 3 else "")
                            self._show_snack(
                                f"Видалено історію [{brands_str}]: {result.get('history', 0)} записів, "
                                f"{result.get('changes', 0)} змін",
                                ft.Colors.GREEN,
                            )
                            self._refresh_table()
                        else:
                            self._show_snack("Помилка видалення історії", ft.Colors.RED)

                    page_ctrl.run_task(_do)

                hist_confirm_btn.on_click = _do_clear_history

                def _toggle_all_hist(val: bool):
                    for b in all_brands:
                        brand_checked_hist[b][0] = val
                        cb = cb_controls_hist.get(b)
                        if cb:
                            cb.value = val
                            with contextlib.suppress(Exception):
                                cb.update()
                    hist_confirm_btn.disabled = not val
                    with contextlib.suppress(Exception):
                        hist_confirm_btn.update()

                brand_rows_hist = []
                for b in all_brands:
                    cb = ft.Checkbox(
                        value=True,
                        label=b,
                        label_style=ft.TextStyle(size=12),
                        on_change=lambda e, _b=b: _on_cb_hist(e, _b),
                    )
                    cb_controls_hist[b] = cb
                    brand_rows_hist.append(cb)

                hist_confirm_dlg = ft.AlertDialog(
                    modal=True,
                    title=ft.Row(
                        [
                            ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=ft.Colors.RED_600, size=26),
                            ft.Text("Видалити історію цін/магазинів?", size=15, weight=ft.FontWeight.W_700),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    content=ft.Column(
                        [
                            ft.Text(
                                "Тільки історія мін./макс. ціни ЄК та к-ті магазинів (графіки/попап); "
                                "самі товари НЕ видаляються. Оберіть бренди:",
                                size=12, color=ft.Colors.BLUE_GREY_700,
                            ),
                            ft.Row(
                                [
                                    ft.TextButton("Всі", on_click=lambda _: _toggle_all_hist(True), style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=6, vertical=2))),
                                    ft.TextButton("Жоден", on_click=lambda _: _toggle_all_hist(False), style=ft.ButtonStyle(padding=ft.padding.symmetric(horizontal=6, vertical=2))),
                                ],
                                spacing=4,
                            ),
                            ft.Container(
                                content=ft.Column(brand_rows_hist, spacing=2, tight=True, scroll=ft.ScrollMode.AUTO),
                                height=min(280, len(all_brands) * 34 + 8),
                                border=ft.border.all(1, ft.Colors.RED_200),
                                border_radius=8,
                                bgcolor=ft.Colors.RED_50,
                                padding=ft.padding.symmetric(horizontal=10, vertical=6),
                            ),
                            ft.Text("Ця дія незворотна!", size=11, color=ft.Colors.RED_700, weight=ft.FontWeight.W_600),
                        ],
                        spacing=8,
                        tight=True,
                        width=340,
                    ),
                    actions=[
                        ft.TextButton("Скасувати", on_click=lambda _: page_ctrl.pop_dialog()),
                        hist_confirm_btn,
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
                if page_ctrl:
                    page_ctrl.show_dialog(hist_confirm_dlg)

            right_toolbar_controls.append(
                ft.OutlinedButton(
                    "Очистити історію цін",
                    icon=ft.Icons.HISTORY_TOGGLE_OFF,
                    on_click=_confirm_clear_history,
                    style=ft.ButtonStyle(
                        color=ft.Colors.RED_600,
                        side=ft.BorderSide(1, ft.Colors.RED_300),
                        padding=ft.padding.symmetric(horizontal=10, vertical=5),
                        text_style=ft.TextStyle(size=11),
                    ),
                )
            )

            # ---- Manual EK link entry (SA only) — for EK-only brands (e.g.
            # Amazfit/MiBand) where not everything is in the auto-crawled
            # listing page and some items need to be added by hand. Links
            # are only stored here; gsm_parser_runner merges them into the
            # brand's candidate list on the next run (see "Бренд без GSM
            # Arena" path) so they get parsed with the normal anti-bot
            # pacing instead of an immediate on-demand fetch from the UI. ----
            def _open_manual_ek_dialog(_):
                ek_only_brands = sorted(
                    {
                        str(b.get("brand") or "").strip()
                        for b in (self.db.get_gsm_parser_brands() or [])
                        if str(b.get("brand") or "").strip()
                        and not str(b.get("gsm_url") or "").strip()
                    }
                )
                if not ek_only_brands:
                    self._show_snack(
                        "Немає брендів, які парсяться лише через ЄК", ft.Colors.ORANGE
                    )
                    return

                brand_dd = ft.Dropdown(
                    label="Бренд",
                    value=ek_only_brands[0],
                    dense=True,
                    options=[ft.DropdownOption(key=b, text=b) for b in ek_only_brands],
                    width=200,
                )
                type_dd = ft.Dropdown(
                    label="Тип",
                    value="Smartphone",
                    dense=True,
                    options=[
                        ft.DropdownOption(key="Smartphone", text="Смартфон"),
                        ft.DropdownOption(key="Tablet", text="Планшет"),
                        ft.DropdownOption(key="Watch", text="Годинник"),
                    ],
                    width=140,
                )
                url_field = ft.TextField(
                    label="Посилання на товар (E-Katalog)",
                    hint_text="https://ek.ua/...",
                    dense=True,
                    expand=True,
                )
                links_col = ft.Column(
                    spacing=4, tight=True, scroll=ft.ScrollMode.AUTO, height=180
                )

                def _refresh_links(_=None):
                    links_col.controls.clear()
                    rows = self.db.get_gsm_manual_ek_links(brand_dd.value) or []
                    if not rows:
                        links_col.controls.append(
                            ft.Text(
                                "Немає доданих посилань для цього бренду",
                                size=11,
                                color=_MUTED,
                            )
                        )
                    for r in rows:
                        rid = str(r.get("id") or "")
                        links_col.controls.append(
                            ft.Row(
                                [
                                    ft.Text(
                                        str(r.get("device_type") or ""),
                                        size=10,
                                        color=_MUTED,
                                        width=70,
                                    ),
                                    ft.Text(
                                        str(r.get("url") or ""),
                                        size=11,
                                        expand=True,
                                        selectable=True,
                                        max_lines=1,
                                    ),
                                    ft.IconButton(
                                        ft.Icons.DELETE_OUTLINE,
                                        icon_size=16,
                                        icon_color=ft.Colors.RED_400,
                                        tooltip="Видалити",
                                        on_click=lambda e, _id=rid: _delete_link(_id),
                                    ),
                                ],
                                spacing=6,
                            )
                        )
                    with contextlib.suppress(Exception):
                        links_col.update()

                def _delete_link(link_id):
                    self.db.delete_gsm_manual_ek_link(link_id)
                    _refresh_links()

                brand_dd.on_change = _refresh_links
                brand_dd.on_select = _refresh_links

                def _add_link(_):
                    url = (url_field.value or "").strip()
                    if not url.startswith("http"):
                        self._show_snack(
                            "Вкажіть коректне посилання (http/https)", ft.Colors.RED
                        )
                        return
                    ok = self.db.add_gsm_manual_ek_link(
                        brand_dd.value, type_dd.value, url, self.manager_id or ""
                    )
                    if ok:
                        url_field.value = ""
                        with contextlib.suppress(Exception):
                            url_field.update()
                        _refresh_links()
                        self._show_snack(
                            "Додано. З'явиться після наступного парсингу цього бренду.",
                            ft.Colors.GREEN,
                        )
                    else:
                        self._show_snack(
                            "Помилка додавання (можливо, вже додано)", ft.Colors.RED
                        )

                _refresh_links()

                dlg = ft.AlertDialog(
                    modal=True,
                    title=ft.Row(
                        [
                            ft.Icon(ft.Icons.ADD_LINK, color=ft.Colors.TEAL_700, size=22),
                            ft.Text(
                                "Ручні посилання ЄК", size=15, weight=ft.FontWeight.W_700
                            ),
                        ],
                        spacing=8,
                    ),
                    content=ft.Container(
                        width=520,
                        content=ft.Column(
                            [
                                ft.Text(
                                    "Тільки для брендів, які парсяться лише через "
                                    "E-Katalog (без GSM Arena) — додайте товар, "
                                    "якого немає в автоматичному списку.",
                                    size=11,
                                    color=_MUTED,
                                ),
                                ft.Row([brand_dd, type_dd], spacing=8),
                                ft.Row([url_field], spacing=8),
                                ft.FilledButton(
                                    "Додати", icon=ft.Icons.ADD, on_click=_add_link
                                ),
                                ft.Divider(height=1),
                                ft.Text(
                                    "Додані посилання:", size=11, weight=ft.FontWeight.W_600
                                ),
                                links_col,
                            ],
                            spacing=8,
                            tight=True,
                        ),
                    ),
                    actions=[
                        ft.TextButton(
                            "Закрити", on_click=lambda _: page_ctrl.pop_dialog()
                        ),
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
                if page_ctrl:
                    page_ctrl.show_dialog(dlg)

            right_toolbar_controls.append(
                ft.OutlinedButton(
                    "Посилання ЄК",
                    icon=ft.Icons.ADD_LINK,
                    on_click=_open_manual_ek_dialog,
                    style=ft.ButtonStyle(
                        color=ft.Colors.TEAL_700,
                        side=ft.BorderSide(1, ft.Colors.TEAL_300),
                        padding=ft.padding.symmetric(horizontal=10, vertical=5),
                        text_style=ft.TextStyle(size=11),
                    ),
                )
            )

        # "Сумісні" button — all users
        compat_global_btn = ft.OutlinedButton(
            "Сумісні",
            icon=ft.Icons.HANDSHAKE_OUTLINED,
            on_click=lambda _: self._compat_global_modal(page_ctrl),
            style=ft.ButtonStyle(
                color=_OK_DK,
                side=ft.BorderSide(1, ft.Colors.GREEN_300),
                padding=ft.padding.symmetric(horizontal=10, vertical=5),
                text_style=ft.TextStyle(size=11),
            ),
        )
        # Flat-list/grouped toggle — all users, rightmost button
        group_toggle_btn = self._build_group_toggle_btn()

        # Row 1 — user controls (all users)
        from views.mobile_layout_helpers import (is_mobile,
                                                 make_scrollable_chip_row)

        _gsm_is_mobile = is_mobile(page_ctrl)
        if _gsm_is_mobile:
            # This toolbar packs a search field (210px), a presets dropdown
            # (170px) and half a dozen buttons into one un-wrapped Row — on a
            # narrow phone screen that overflows badly. Keep it a single
            # horizontally-scrollable line instead of trying to squeeze it
            # into a Row alongside an expand=True spacer (which is what was
            # causing the overflow), and stack stats_text below it so nothing
            # competes for width.
            user_row = ft.Column(
                [
                    make_scrollable_chip_row(
                        left_toolbar_controls + [compat_global_btn, group_toggle_btn],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    stats_text,
                ],
                spacing=4,
                tight=True,
            )
        else:
            user_row = ft.Row(
                [
                    ft.Row(
                        left_toolbar_controls + [compat_global_btn, group_toggle_btn],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Container(expand=True),
                    stats_text,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )

        # Row 2 — SA / run_parser-permission controls
        toolbar_rows: list = [user_row]
        if (self.is_superadmin or self.can_run_parser) and right_toolbar_controls:
            sa_row = (
                make_scrollable_chip_row(
                    right_toolbar_controls,
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
                if _gsm_is_mobile
                else ft.Row(
                    right_toolbar_controls,
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
            )
            toolbar_rows.append(sa_row)

        # Row 3 — parser log (SA only, red text)
        if parser_status_row is not None:
            toolbar_rows.append(parser_status_row)

        toolbar = ft.Column(toolbar_rows, spacing=4, tight=True)

        # ---- Filter panel ----
        filter_panel_inner = self._build_filter_panel()
        filter_panel = ft.Container(
            content=filter_panel_inner,
            visible=self._filter_open,
        )
        self._filter_panel_ref = filter_panel

        # ---- Reveal panel (deferred load placeholder) ----
        _reveal_refs = {}

        _reveal_choose_col = ft.Column(
            [
                ft.Icon(ft.Icons.TABLE_VIEW, size=52, color=ft.Colors.BLUE_GREY_200),
                ft.Text(
                    "Оберіть потрібні фільтри та натисніть кнопку нижче",
                    size=13,
                    color=_MUTED,
                    text_align=ft.TextAlign.CENTER,
                ),
                ft.FilledButton(
                    "Показати результати",
                    icon=ft.Icons.PLAY_CIRCLE_OUTLINED,
                    on_click=lambda e: _reveal_click(e),
                    style=ft.ButtonStyle(
                        bgcolor=_OK_DK,
                        padding=ft.padding.symmetric(horizontal=24, vertical=12),
                        text_style=ft.TextStyle(size=13),
                    ),
                ),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=16,
        )
        _reveal_status_text = ft.Text("Завантаження...", size=13, color=_MUTED)
        self._reveal_status_ref = _reveal_status_text
        _reveal_loading_col = ft.Column(
            [
                ft.ProgressRing(width=32, height=32, color=_OK_DK),
                _reveal_status_text,
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=16,
            visible=False,
        )

        async def _reveal_and_sync(page_ctrl_arg):
            # Keep the loading state up until the sync is FULLY done (not
            # just kicked off in the background) — showing the table right
            # away and syncing silently after it let the rest of the app
            # (chart hover, dialog close, everything sharing the same UI
            # thread) feel sluggish for as long as that background sync kept
            # running, even though the table itself already looked ready.
            # One clear wait up front beats a table that LOOKS interactive
            # but secretly still competes with the sync for the same thread.
            try:
                # _reveal_sync now directly awaits everything itself — row
                # fill (via _pending_fill, no more fire-and-forget +
                # polling race), diagram preload, and photo-cache warm —
                # so by the time this returns the table is genuinely fully
                # ready. A 10-minute outer cap is a last-resort safety net
                # only; the per-phase work inside _reveal_sync is what
                # actually bounds this in normal operation.
                await asyncio.wait_for(self._reveal_sync(page_ctrl_arg), timeout=600)
            except asyncio.TimeoutError:
                logger.error("[GSM] _reveal_and_sync: exceeded 10-minute safety cap")
                _catlog.error("[GSM] _reveal_and_sync: exceeded 10-minute safety cap — revealing partially loaded")
            except Exception as exc:
                logger.error("[GSM] _reveal_and_sync FAILED: %s", exc, exc_info=True)
                _catlog.exception("[GSM] _reveal_and_sync FAILED — table may reveal only partially loaded")
            finally:
                _reveal_loading_col.visible = False
                _reveal_refs["panel"].visible = False
                _reveal_refs["table"].visible = True
                if self._loading_bar_ref is not None:
                    self._loading_bar_ref.visible = False
                    with contextlib.suppress(Exception):
                        self._loading_bar_ref.update()
                with contextlib.suppress(Exception):
                    _reveal_refs["panel"].update()
                with contextlib.suppress(Exception):
                    _reveal_refs["table"].update()

        def _reveal_click(_):
            _reveal_choose_col.visible = False
            _reveal_loading_col.visible = True
            with contextlib.suppress(Exception):
                _reveal_refs["panel"].update()
            # The table body was built once at construction time as a flat,
            # ungrouped row list (see _build_results_content); the grouped
            # rebuild happens once _reveal_and_sync's sync finishes, right
            # before the loading state is swapped out for the real table.
            if page_ctrl:
                page_ctrl.run_task(_reveal_and_sync, page_ctrl)

        # Explicit product decision (reverted after trying auto-start):
        # loading must NOT begin until the user clicks "Показати
        # результати" — same behavior for every role, no is_superadmin
        # split. _reveal_choose_col (the picker) stays as the initial
        # visible state; _reveal_click is the only thing that starts
        # _reveal_and_sync.

        reveal_panel = ft.Container(
            visible=_should_reveal,
            expand=True,
            alignment=ft.Alignment(0, 0),
            padding=ft.padding.symmetric(vertical=60),
            content=ft.Column(
                [_reveal_choose_col, _reveal_loading_col],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
        )

        # ---- Table header (two-block: GSM Arena | Єкаталог) ----
        # +20% wider; full column names; vertical dividers (1px) with spacing=4 between cols
        # Use module-level _GSM_COLS / _EK_COLS — single source of truth
        # Each inter-column gap = 4px spacing + 1px divider + 4px spacing = 9px
        _GSM_W = sum(w for _, w, _, _ in _GSM_COLS) + (len(_GSM_COLS) - 1) * 9
        _EK_W = sum(w for _, w, _, _ in _EK_COLS) + (len(_EK_COLS) - 1) * 9

        def _sort_icon(key):
            if self._sort_col != key:
                return ft.Icons.UNFOLD_MORE
            return (
                ft.Icons.KEYBOARD_ARROW_UP
                if self._sort_asc
                else ft.Icons.KEYBOARD_ARROW_DOWN
            )

        # "Розмір" expands into per-axis sort choices (Довжина/Ширина/
        # Товщина) via a small popup instead of three permanent columns —
        # inserting real extra columns would mean every row-render call
        # site across this file (there are many) needs to know about a
        # dynamic 1<->3 column layout, which is far riskier than a popup
        # that reuses the exact same _sort_click/_sort_col machinery every
        # other column already uses. Lets you quickly find the
        # narrowest/widest/thinnest device for box selection.
        _DIM_AXES = [("length", "Довжина"), ("width", "Ширина"), ("thickness", "Товщина")]

        def _dim_popup(prefix: str, txt_color: str) -> ft.PopupMenuButton:
            def _axis_item(axis_key, axis_label):
                col = f"{prefix}_{axis_key}"
                active = self._sort_col == col
                icon = (
                    (ft.Icons.KEYBOARD_ARROW_UP if self._sort_asc else ft.Icons.KEYBOARD_ARROW_DOWN)
                    if active
                    else ft.Icons.UNFOLD_MORE
                )
                return ft.PopupMenuItem(
                    content=ft.Row(
                        [
                            ft.Text(axis_label, size=12, weight=ft.FontWeight.W_600 if active else None),
                            ft.Icon(icon, size=14, color=_MUTED),
                        ],
                        spacing=6,
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    on_click=lambda _, c=col: self._sort_click(c),
                )

            return ft.PopupMenuButton(
                icon=ft.Icons.STRAIGHTEN,
                icon_size=13,
                icon_color=txt_color,
                tooltip="Сортувати за довжиною / шириною / товщиною",
                padding=ft.padding.all(0),
                items=[_axis_item(k, lbl) for k, lbl in _DIM_AXES],
            )

        def _hdr_cell(key, width, label, sortable, txt_color):
            if sortable:
                _extra = (
                    [_dim_popup(key.split("_")[0], txt_color)]
                    if key in ("gsm_size", "ek_size")
                    else []
                )
                return ft.Container(
                    width=width,
                    alignment=ft.Alignment(0, 0),
                    content=ft.Row(
                        [
                            ft.TextButton(
                                content=ft.Row(
                                    [
                                        ft.Text(
                                            label,
                                            size=10,
                                            weight=ft.FontWeight.W_700,
                                            color=txt_color,
                                        ),
                                        ft.Icon(
                                            _sort_icon(key),
                                            size=12,
                                            color=_MUTED,
                                        ),
                                    ],
                                    spacing=2,
                                    tight=True,
                                ),
                                style=ft.ButtonStyle(
                                    padding=ft.padding.symmetric(horizontal=2, vertical=2)
                                ),
                                on_click=lambda _, c=key: self._sort_click(c),
                            ),
                        ]
                        + _extra,
                        spacing=0,
                        tight=True,
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                )
            return ft.Container(
                width=width,
                alignment=ft.Alignment(0, 0),
                content=ft.Text(
                    label,
                    size=10,
                    weight=ft.FontWeight.W_700,
                    color=txt_color,
                    text_align=ft.TextAlign.CENTER,
                ),
            )

        def _hdr_vd():
            return ft.Container(width=1, height=40, bgcolor=_BORDER)

        def _hdr_row_with_vd(cells):
            result = []
            for i, cell in enumerate(cells):
                result.append(cell)
                if i < len(cells) - 1:
                    result.append(_hdr_vd())
            return result

        gsm_hdr_cells = [_hdr_cell(k, w, lbl, s, _ACCENT) for k, w, lbl, s in self._gsm_cols_t]
        ek_hdr_cells = [_hdr_cell(k, w, lbl, s, _OK_DK) for k, w, lbl, s in self._ek_cols_t]

        table_header = ft.Container(
            padding=ft.padding.symmetric(horizontal=10, vertical=0),
            border_radius=ft.border_radius.only(top_left=10, top_right=10),
            content=ft.Column(
                [
                    # Section labels row — spacing=4 + width=1 separator matches data rows
                    ft.Row(
                        [
                            ft.Container(width=20),  # checkbox column placeholder
                            ft.Container(
                                ft.Text(
                                    "GSM Arena",
                                    size=10,
                                    weight=ft.FontWeight.W_700,
                                    color=_ACCENT,
                                ),
                                bgcolor=_SOFT,
                                padding=ft.padding.symmetric(horizontal=8, vertical=3),
                                border_radius=ft.border_radius.only(
                                    top_left=8, top_right=8
                                ),
                                width=_GSM_W,
                            ),
                            ft.Container(width=1),
                            ft.Container(
                                ft.Text(
                                    "Єкаталог",
                                    size=10,
                                    weight=ft.FontWeight.W_700,
                                    color=_OK_DK,
                                ),
                                bgcolor=_OK_BG,
                                padding=ft.padding.symmetric(horizontal=8, vertical=3),
                                border_radius=ft.border_radius.only(
                                    top_left=8, top_right=8
                                ),
                                width=_EK_W,
                            ),
                        ],
                        spacing=4,
                    ),
                    # Column labels row with vertical dividers
                    ft.Row(
                        [
                            ft.Container(
                                content=self._build_toggle_all_brands_btn(),
                                width=20,
                                alignment=ft.Alignment(0, 0),
                            ),
                            ft.Row(_hdr_row_with_vd(gsm_hdr_cells), spacing=4),
                            ft.Container(width=1, height=32, bgcolor=_BORDER),
                            ft.Row(_hdr_row_with_vd(ek_hdr_cells), spacing=4),
                        ],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=0,
                tight=True,
            ),
        )

        # ---- Table rows ----
        # Grouped by brand + lazy-filled, same as _refresh_table(): building
        # and sending ~100 full device rows in one synchronous Flutter patch
        # on the very first load is exactly the freeze pattern that method
        # already avoids on every later refresh — the initial build must
        # not skip it.
        gsm_sz_c, ek_sz_c, gsm_dg_c, ek_dg_c = _compute_color_maps(filtered)

        self._color_maps = (gsm_sz_c, ek_sz_c, gsm_dg_c, ek_dg_c)
        rows: list[ft.Control] = []
        _to_fill: list[tuple[ft.Container, list[dict]]] = []
        if self._flat_mode:
            # Same reasoning as _refresh_table's matching branch: build
            # synchronously, no chunked async queue — the data is already
            # fully cached (no DB round trip per row), so one direct build
            # + one patch beats ~55 chunked patches on total wall time.
            self._brand_devs_by_name = {"__flat__": page_devs}
            self._brand_body_by_name = {}
            body_col = ft.Column(spacing=0)
            body_container = ft.Container(content=body_col, visible=True)
            built_rows = []
            for d in page_devs:
                try:
                    built_rows.append(
                        self._build_table_row(d, gsm_sz_c, ek_sz_c, gsm_dg_c, ek_dg_c)
                    )
                except Exception as row_exc:
                    logger.error(
                        "[GSM] flat-mode initial row build failed for %s: %s",
                        d.get("gsm_name"), row_exc,
                    )
            body_col.controls = built_rows
            body_container.data = "filled"
            self._brand_body_by_name["__flat__"] = body_container
            self._flat_last_page_devs = page_devs
            rows.append(body_container)
        else:
            prev_brand: str | None = None
            brand_group: list[dict] = []
            brand_groups: list[tuple[str, list[dict]]] = []
            for d in page_devs:
                b = str(d.get("brand") or "")
                if b != prev_brand:
                    if brand_group:
                        brand_groups.append((prev_brand, brand_group))
                    brand_group = [d]
                    prev_brand = b
                else:
                    brand_group.append(d)
            if brand_group:
                brand_groups.append((prev_brand, brand_group))

            self._brand_devs_by_name = {b: devs for b, devs in brand_groups}
            self._brand_body_by_name = {}

            first_brand = True
            for brand_name, brand_devs in brand_groups:
                if not first_brand:
                    rows.append(ft.Divider(height=3, thickness=3, color=_ACCENT))
                first_brand = False
                if brand_name not in _KNOWN_BRANDS:
                    _KNOWN_BRANDS.add(brand_name)
                    _COLLAPSED_BRANDS.add(brand_name)
                collapsed = brand_name in _COLLAPSED_BRANDS
                body_col = ft.Column(spacing=0)
                body_container = ft.Container(content=body_col, visible=not collapsed)
                self._brand_body_by_name[brand_name] = body_container
                if not collapsed:
                    _to_fill.append((body_container, brand_devs))
                rows.append(
                    self._build_brand_header(brand_name, len(brand_devs), body_container)
                )
                rows.append(body_container)
        row_controls = rows or [self._empty_row()]
        table_body = ft.Column(row_controls, spacing=0, tight=True)
        self._table_body_ref = table_body

        # _should_reveal is always True now (see above) — the auto-started
        # _reveal_and_sync task fires moments after this function returns
        # and rebuilds this exact table via _refresh_table(fill_all_brands
        # =True), while table_container stays hidden behind reveal_panel
        # the whole time regardless. Firing this fire-and-forget fill here
        # too was pure waste AND a real race: it could still be actively
        # building rows into these containers when _reveal_sync's
        # _refresh_table call replaced self._brand_body_by_name with fresh
        # ones, orphaning that work — but _brand_fill_queue is shared, so
        # the drain loop kept processing those now-pointless entries
        # before ever reaching the real ones, which is what made brand-
        # expand/charts still feel slow even after the loading screen
        # said it was done. Skip entirely; _reveal_and_sync owns this now.
        if page_ctrl and _to_fill and not _should_reveal:
            page_ctrl.run_task(
                self._fill_expanded_brands_async,
                _to_fill,
                (gsm_sz_c, ek_sz_c, gsm_dg_c, ek_dg_c),
            )

        # ---- Pagination ----
        pager_controls = self._build_pager_controls()
        pager_row = ft.Row(
            pager_controls, spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER
        )
        self._pager_ref = pager_row

        # ---- Horizontal scroll with overlay buttons ----
        h_scroll_ref = ft.Ref[ft.Row]()

        def _h_scroll(delta):
            async def _do(_d=delta):
                with contextlib.suppress(Exception):
                    if h_scroll_ref.current:
                        await h_scroll_ref.current.scroll_to(delta=_d, duration=200)

            with contextlib.suppress(Exception):
                if page_ctrl:
                    page_ctrl.run_task(_do)

        _shadow = ft.BoxShadow(
            spread_radius=1,
            blur_radius=6,
            color=ft.Colors.GREY_400,
            offset=ft.Offset(0, 2),
        )
        _scroll_btn_style = ft.ButtonStyle(padding=ft.padding.all(4))

        table_scroll_row = ft.Row(
            ref=h_scroll_ref,
            controls=[
                ft.Column(
                    [
                        table_header,
                        ft.Divider(height=1, color=ft.Colors.BLUE_GREY_200),
                        ft.Column(
                            [table_body],
                            spacing=0,
                            tight=True,
                            scroll=ft.ScrollMode.HIDDEN,
                            expand=True,
                        ),
                    ],
                    spacing=0,
                    tight=True,
                    expand=True,
                )
            ],
            scroll=ft.ScrollMode.HIDDEN,
            expand=True,
        )

        # Show buttons only when table is wider than the current window.
        _gsm_table_min_w = sum(w for _, w, *_ in _GSM_COLS + _EK_COLS)
        _gsm_pg_w = int(getattr(page_ctrl, "window_width", 0) or 0) if page_ctrl else 0
        _gsm_needs = _gsm_table_min_w > max(800, _gsm_pg_w)
        _gsm_step = max(280, _gsm_table_min_w // 3)

        # Pinned to the column-headers row band only (not floating over the
        # data rows below) — top matches the header block's height so the
        # buttons sit beside the header labels instead of overlapping items.
        _gsm_scroll_btn_top = 22

        scroll_left_btn = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_LEFT,
                icon_size=16,
                icon_color=_ACCENT,
                on_click=lambda _: _h_scroll(-_gsm_step),
                style=_scroll_btn_style,
            ),
            left=4,
            top=_gsm_scroll_btn_top,
            bgcolor=_PANEL,
            border_radius=14,
            width=28,
            height=28,
            shadow=_shadow,
            opacity=0.88,
            visible=_gsm_needs,
        )
        scroll_right_btn = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_RIGHT,
                icon_size=16,
                icon_color=_ACCENT,
                on_click=lambda _: _h_scroll(_gsm_step),
                style=_scroll_btn_style,
            ),
            right=4,
            top=_gsm_scroll_btn_top,
            bgcolor=_PANEL,
            border_radius=14,
            width=28,
            height=28,
            shadow=_shadow,
            opacity=0.88,
            visible=_gsm_needs,
        )

        table_container = ft.Container(
            border=ft.border.all(1, _BORDER),
            border_radius=12,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            expand=True,
            visible=not _should_reveal,
            content=ft.Stack(
                [table_scroll_row, scroll_left_btn, scroll_right_btn],
                expand=True,
            ),
        )
        _reveal_refs["panel"] = reveal_panel
        _reveal_refs["table"] = table_container

        _col_controls = [loading_bar, loading_status, toolbar]
        if self.is_superadmin:
            # Xiaomi sync status shown below toolbar (separate from log row)
            _col_controls.append(ft.Row([_sync_status_text]))
        _col_controls.extend([filter_panel, reveal_panel, table_container, pager_row])
        return ft.Column(_col_controls, spacing=6, expand=True)

    def _do_search(self, value: str):
        self._search = value.strip()
        self._page_idx = 0
        self._refresh_table()

    def _toggle_filter_panel(self, _=None):
        self._filter_open = not self._filter_open
        if self._filter_panel_ref is not None:
            self._filter_panel_ref.visible = self._filter_open
            with contextlib.suppress(Exception):
                self._filter_panel_ref.update()
        else:
            # Fallback: standalone mode only
            with contextlib.suppress(Exception):
                self.build()

    def _build_filter_panel(self) -> ft.Control:
        devices, _, loading = _get_cache()
        if devices is None:
            return ft.Container(
                padding=ft.padding.symmetric(horizontal=16, vertical=12),
                content=ft.Row(
                    [
                        ft.ProgressRing(width=14, height=14, stroke_width=2, color=_OK_DK),
                        ft.Text("Дані завантажуються, зачекайте…", size=12, color=_MUTED),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            )
        # Use pre-computed option sets (updated once per cache load, not per click)
        opts = _FILTER_OPTIONS
        all_brands = opts["brands"]
        all_years = opts["years"]
        all_types = opts["types"]
        all_statuses = opts["statuses"]

        def _brand_chip(brand):
            selected = brand in self._filter_brands

            def _select(_):
                if brand not in self._filter_brands:
                    self._filter_brands.append(brand)
                self._page_idx = 0
                self._refresh_table()

            def _remove(_):
                if brand in self._filter_brands:
                    self._filter_brands.remove(brand)
                self._page_idx = 0
                self._refresh_table()

            content = ft.Row(
                [
                    ft.Text(
                        brand, size=11, color=ft.Colors.WHITE if selected else _TEXT
                    ),
                    *(
                        [
                            ft.GestureDetector(
                                content=ft.Icon(
                                    ft.Icons.CLOSE, size=11, color=ft.Colors.WHITE70
                                ),
                                on_tap=_remove,
                            )
                        ]
                        if selected
                        else []
                    ),
                ],
                spacing=4,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            return ft.Container(
                padding=ft.padding.symmetric(horizontal=8, vertical=4),
                border_radius=999,
                bgcolor=_OK_DK if selected else ft.Colors.BLUE_GREY_50,
                border=ft.border.all(
                    1, _OK_DK if selected else ft.Colors.BLUE_GREY_200
                ),
                ink=not selected,
                on_click=_select if not selected else None,
                content=content,
            )

        def _type_chip(t):
            selected = t in self._filter_types

            def _select(_):
                if t not in self._filter_types:
                    self._filter_types.append(t)
                self._page_idx = 0
                self._refresh_table()

            def _remove(_):
                if t in self._filter_types:
                    self._filter_types.remove(t)
                self._page_idx = 0
                self._refresh_table()

            content = ft.Row(
                [
                    ft.Text(t, size=11, color=ft.Colors.WHITE if selected else _TEXT),
                    *(
                        [
                            ft.GestureDetector(
                                content=ft.Icon(
                                    ft.Icons.CLOSE, size=11, color=ft.Colors.WHITE70
                                ),
                                on_tap=_remove,
                            )
                        ]
                        if selected
                        else []
                    ),
                ],
                spacing=4,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            return ft.Container(
                padding=ft.padding.symmetric(horizontal=8, vertical=4),
                border_radius=999,
                bgcolor=_ACCENT if selected else ft.Colors.BLUE_GREY_50,
                border=ft.border.all(
                    1, _ACCENT if selected else ft.Colors.BLUE_GREY_200
                ),
                ink=not selected,
                on_click=_select if not selected else None,
                content=content,
            )

        def _status_chip(s):
            selected = s in self._filter_statuses

            def _select(_):
                if s not in self._filter_statuses:
                    self._filter_statuses.append(s)
                self._page_idx = 0
                self._refresh_table()

            def _remove(_):
                if s in self._filter_statuses:
                    self._filter_statuses.remove(s)
                self._page_idx = 0
                self._refresh_table()

            content = ft.Row(
                [
                    ft.Text(s, size=11, color=ft.Colors.WHITE if selected else _TEXT),
                    *(
                        [
                            ft.GestureDetector(
                                content=ft.Icon(
                                    ft.Icons.CLOSE, size=11, color=ft.Colors.WHITE70
                                ),
                                on_tap=_remove,
                            )
                        ]
                        if selected
                        else []
                    ),
                ],
                spacing=4,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            return ft.Container(
                padding=ft.padding.symmetric(horizontal=8, vertical=4),
                border_radius=999,
                bgcolor=ft.Colors.ORANGE_600 if selected else ft.Colors.BLUE_GREY_50,
                border=ft.border.all(
                    1, ft.Colors.ORANGE_400 if selected else ft.Colors.BLUE_GREY_200
                ),
                ink=not selected,
                on_click=_select if not selected else None,
                content=content,
            )

        def _year_chip(y):
            selected = y in self._filter_years

            def _select(_):
                if y not in self._filter_years:
                    self._filter_years.append(y)
                self._page_idx = 0
                self._refresh_table()

            def _remove(_):
                if y in self._filter_years:
                    self._filter_years.remove(y)
                self._page_idx = 0
                self._refresh_table()

            content = ft.Row(
                [
                    ft.Text(
                        y, size=11, color=ft.Colors.PURPLE_900 if selected else _TEXT
                    ),
                    *(
                        [
                            ft.GestureDetector(
                                content=ft.Icon(
                                    ft.Icons.CLOSE, size=11, color=ft.Colors.PURPLE_700
                                ),
                                on_tap=_remove,
                            )
                        ]
                        if selected
                        else []
                    ),
                ],
                spacing=4,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
            return ft.Container(
                padding=ft.padding.symmetric(horizontal=8, vertical=3),
                border_radius=999,
                bgcolor=ft.Colors.PURPLE_100 if selected else ft.Colors.BLUE_GREY_50,
                border=ft.border.all(
                    1, ft.Colors.PURPLE_400 if selected else ft.Colors.BLUE_GREY_200
                ),
                ink=not selected,
                on_click=_select if not selected else None,
                content=content,
            )

        def _clear_all(_):
            self._filter_brands.clear()
            self._filter_types.clear()
            self._filter_years.clear()
            self._filter_statuses.clear()
            self._filter_source = ""
            self._filter_diag = ""
            self._filter_size = ""
            self._page_idx = 0
            self._refresh_table()

        rows = [
            ft.Row(
                [
                    ft.Text("Фільтри", size=14, weight=ft.FontWeight.W_700),
                    ft.TextButton(
                        "Скинути всі", icon=ft.Icons.CLEAR, on_click=_clear_all
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
        ]

        def _section(label, chips):
            if not chips:
                return None
            return [
                ft.Text(
                    label,
                    size=12,
                    weight=ft.FontWeight.W_600,
                    color=ft.Colors.BLUE_GREY_700,
                ),
                ft.Row(chips, spacing=6, wrap=True, run_spacing=4),
            ]

        for part in [
            _section("Тип пристрою", [_type_chip(t) for t in all_types]),
            _section("Статус", [_status_chip(s) for s in all_statuses]),
            _section("Рік", [_year_chip(y) for y in all_years[:20]]),
            _section("Бренд", [_brand_chip(b) for b in all_brands]),
        ]:
            if part:
                rows.extend(part)

        # Quick filters from cell clicks
        quick_chips = []
        if self._filter_diag:

            def _rm_diag(_):
                self._filter_diag = ""
                self._page_idx = 0
                self._refresh_table()

            quick_chips.append(
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=10, vertical=4),
                    border_radius=999,
                    bgcolor=ft.Colors.TEAL_100,
                    border=ft.border.all(1, ft.Colors.TEAL_400),
                    ink=True,
                    on_click=_rm_diag,
                    tooltip="Зняти фільтр діагоналі",
                    content=ft.Row(
                        [
                            ft.Text(f"Діаг: {self._filter_diag}", size=11),
                            ft.Icon(ft.Icons.CLOSE, size=12),
                        ],
                        spacing=4,
                        tight=True,
                    ),
                )
            )
        if self._filter_size:

            def _rm_size(_):
                self._filter_size = ""
                self._page_idx = 0
                self._refresh_table()

            quick_chips.append(
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=10, vertical=4),
                    border_radius=999,
                    bgcolor=ft.Colors.TEAL_100,
                    border=ft.border.all(1, ft.Colors.TEAL_400),
                    ink=True,
                    on_click=_rm_size,
                    tooltip="Зняти фільтр розміру",
                    content=ft.Row(
                        [
                            ft.Text(f"Розмір: {self._filter_size}", size=11),
                            ft.Icon(ft.Icons.CLOSE, size=12),
                        ],
                        spacing=4,
                        tight=True,
                    ),
                )
            )
        if quick_chips:
            rows.extend(
                [
                    ft.Text(
                        "Активні (кліком)",
                        size=12,
                        weight=ft.FontWeight.W_600,
                        color=ft.Colors.TEAL_700,
                    ),
                    ft.Row(quick_chips, spacing=6, wrap=True),
                ]
            )

        return ft.Container(
            border=ft.border.all(1, ft.Colors.BLUE_GREY_200),
            border_radius=12,
            padding=16,
            bgcolor=ft.Colors.BLUE_GREY_50,
            content=ft.Column(rows, spacing=8, tight=True),
        )

    # ------------------------------------------------------------------ #
    #  Settings tab, brands-only (managers granted "Запуск парсингу" but   #
    #  not full superadmin access) — same underlying rows/save call as     #
    #  the superadmin "Бренди та посилання" block, simplified layout.     #
    # ------------------------------------------------------------------ #
    def _build_brands_only_tab(self, page_ctrl) -> ft.Control:
        try:
            brands = self.db.get_gsm_parser_brands() or []
        except Exception:
            logger.exception("[GSM] _build_brands_only_tab: load failed")
            brands = []

        msg = ft.Text("", size=12)
        row_refs: list[dict] = []

        def _brand_row(row: dict, idx: int) -> ft.Container:
            enabled_cb = ft.Checkbox(value=bool(row.get("enabled", True)))
            gsm_url_f = ft.TextField(
                value=str(row.get("gsm_url") or ""), dense=True, text_size=12,
                label="Посилання GsmArena", expand=True,
            )
            last_item_f = ft.TextField(
                value=str(row.get("last_item") or ""), dense=True, text_size=12,
                label="Останній предмет парсингу", expand=True,
            )
            ek_phones_f = ft.TextField(
                value=str(row.get("ek_url_phones") or ""), dense=True, text_size=12,
                label="ЄК Смартфон", expand=True,
            )
            ek_tablets_f = ft.TextField(
                value=str(row.get("ek_url_tablets") or ""), dense=True, text_size=12,
                label="ЄК Планшети", expand=True,
            )
            ek_watches_f = ft.TextField(
                value=str(row.get("ek_url_watches") or ""), dense=True, text_size=12,
                label="ЄК Годинники", expand=True,
            )
            row_refs.append({
                "id": row.get("id"),
                "brand": row.get("brand"),
                "sort_order": row.get("sort_order", idx),
                "enabled": enabled_cb,
                "gsm_url": gsm_url_f,
                "last_item": last_item_f,
                "ek_url_phones": ek_phones_f,
                "ek_url_tablets": ek_tablets_f,
                "ek_url_watches": ek_watches_f,
            })
            return ft.Container(
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
                bgcolor=ft.Colors.WHITE if idx % 2 == 0 else ft.Colors.BLUE_GREY_50,
                border_radius=8,
                content=ft.Column(
                    [
                        ft.Row(
                            [enabled_cb, ft.Text(str(row.get("brand") or ""), weight=ft.FontWeight.W_700, size=13)],
                            spacing=8,
                        ),
                        ft.Row([gsm_url_f, last_item_f], spacing=8),
                        ft.Row([ek_phones_f, ek_tablets_f, ek_watches_f], spacing=8),
                    ],
                    spacing=6,
                ),
            )

        def _save(_):
            payload = [
                {
                    "id": r["id"],
                    "brand": r["brand"],
                    "sort_order": r["sort_order"],
                    "enabled": bool(r["enabled"].value),
                    "gsm_url": (r["gsm_url"].value or "").strip(),
                    "last_item": (r["last_item"].value or "").strip(),
                    "ek_url_phones": (r["ek_url_phones"].value or "").strip(),
                    "ek_url_tablets": (r["ek_url_tablets"].value or "").strip(),
                    "ek_url_watches": (r["ek_url_watches"].value or "").strip(),
                }
                for r in row_refs
            ]
            ok = False
            with contextlib.suppress(Exception):
                ok = self.db.save_gsm_parser_brands_bulk(payload)
            msg.value = "Збережено" if ok else "Не вдалося зберегти"
            msg.color = _OK_DK if ok else ft.Colors.RED_600
            with contextlib.suppress(Exception):
                msg.update()

        rows_col = ft.Column(
            [_brand_row(b, i) for i, b in enumerate(brands)] or [
                ft.Text("Немає брендів.", color=ft.Colors.BLUE_GREY_400)
            ],
            spacing=6,
        )

        return ft.Container(
            padding=14,
            content=ft.Column(
                [
                    ft.Text("Бренди та посилання", size=15, weight=ft.FontWeight.W_700),
                    ft.Text(
                        "Налаштуйте бренди для парсингу: GSM Arena URL, останній відомий пристрій та посилання на Єкаталог.",
                        size=12, color=ft.Colors.BLUE_GREY_600,
                    ),
                    ft.Divider(height=10),
                    rows_col,
                    ft.Row(
                        [ft.FilledButton("Зберегти", icon=ft.Icons.SAVE, on_click=_save), msg],
                        spacing=10,
                    ),
                ],
                spacing=8,
                scroll=ft.ScrollMode.ADAPTIVE,
                expand=True,
            ),
        )

    # ------------------------------------------------------------------ #
    #  Settings tab (SA only): schedule + VPS service                    #
    # ------------------------------------------------------------------ #
    def _build_settings_tab(self, page_ctrl) -> ft.Control:
        # Load current state
        try:
            sched = self.db.get_gsm_parser_schedule()
        except Exception:
            sched = {"enabled": False, "run_times": ["03:00"]}
        try:
            svc_cfg = self.db.get_parser_service_config()
        except Exception:
            svc_cfg = {"vps_service_url": "", "vps_api_key": ""}
        try:
            infra = self.db.get_infrastructure_settings()
        except Exception:
            infra = {"mode": "local", "vps_db_dsn": ""}

        run_time = (sched.get("run_times") or ["03:00"])[0]
        current_mode = str(infra.get("mode") or "local")

        # ── Local schedule ────────────────────────────────────────────────
        enabled_sw = ft.Switch(
            value=bool(sched.get("enabled")),
            label="Автозапуск за розкладом",
            active_color=_OK_DK,
        )
        time_tf = ft.TextField(
            value=run_time,
            label="Час запуску (ГГ:ХХ)",
            hint_text="03:00",
            width=120,
            dense=True,
            text_size=13,
        )
        sched_msg = ft.Text("", size=11)

        def _save_schedule(_):
            t = str(time_tf.value or "").strip()
            parts = t.split(":")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():

                async def _do():
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(self.db.set_gsm_parser_schedule, enabled_sw.value, [t])
                    sched_msg.value = (
                        f"Збережено: {'увімк.' if enabled_sw.value else 'вимк.'} о {t}"
                    )
                    sched_msg.color = _OK_DK
                    with contextlib.suppress(Exception):
                        sched_msg.update()

                if page_ctrl:
                    page_ctrl.run_task(_do)
            else:
                sched_msg.value = "Невірний формат — введіть ГГ:ХХ (напр. 03:00)"
                sched_msg.color = ft.Colors.RED_600
                with contextlib.suppress(Exception):
                    sched_msg.update()

        sched_section = ft.Container(
            padding=14,
            border_radius=10,
            border=ft.border.all(1, _BORDER),
            bgcolor=ft.Colors.BLUE_GREY_50,
            content=ft.Column(
                [
                    ft.Text(
                        "Розклад (локальний режим)",
                        size=13,
                        weight=ft.FontWeight.W_700,
                        color=_TEXT,
                    ),
                    ft.Text(
                        "Програма запустить парсер автоматично, якщо ПК та додаток відкриті.",
                        size=11,
                        color=_MUTED,
                    ),
                    enabled_sw,
                    ft.Row(
                        [time_tf, ft.FilledButton("Зберегти", on_click=_save_schedule)],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.END,
                    ),
                    sched_msg,
                ],
                spacing=8,
                tight=True,
            ),
        )

        # ── VPS service ───────────────────────────────────────────────────
        mode_badge = ft.Container(
            padding=ft.padding.symmetric(horizontal=10, vertical=4),
            border_radius=999,
            bgcolor=_OK_DK if current_mode == "vps" else ft.Colors.BLUE_GREY_200,
            content=ft.Text(
                "VPS" if current_mode == "vps" else "Локально",
                size=11,
                color=ft.Colors.WHITE if current_mode == "vps" else _TEXT,
                weight=ft.FontWeight.W_600,
            ),
        )

        url_tf = ft.TextField(
            value=svc_cfg.get("vps_service_url", ""),
            label="URL сервісу",
            hint_text="http://45.12.34.56:8765",
            expand=True,
            dense=True,
            text_size=12,
        )
        key_tf = ft.TextField(
            value=svc_cfg.get("vps_api_key", ""),
            label="API ключ",
            password=True,
            can_reveal_password=True,
            expand=True,
            dense=True,
            text_size=12,
        )
        db_dsn_tf = ft.TextField(
            value=infra.get("vps_db_dsn", ""),
            label="PostgreSQL DSN на VPS",
            hint_text="postgresql://user:pass@vps-ip:5432/dbname",
            expand=True,
            dense=True,
            text_size=12,
            password=True,
            can_reveal_password=True,
        )
        vps_msg = ft.Text("", size=11)
        vps_progress = ft.ProgressBar(visible=False, color=_OK_DK, height=4)

        def _set_vps_msg(text: str, ok: bool = True):
            vps_msg.value = text
            vps_msg.color = _OK_DK if ok else ft.Colors.RED_600
            with contextlib.suppress(Exception):
                vps_msg.update()

        def _test_connection(_):
            url = str(url_tf.value or "").strip().rstrip("/")
            if not url:
                _set_vps_msg("Введіть URL сервісу", ok=False)
                return

            async def _do():
                try:
                    import httpx

                    async with httpx.AsyncClient() as c:
                        r = await c.get(f"{url}/status", timeout=8)
                    if r.status_code == 200:
                        d = r.json()
                        _set_vps_msg(
                            f"З'єднання OK  |  розклад: {d.get('schedule', '?')}  |  "
                            f"DB: {'є' if d.get('has_db') else 'не вказана'}"
                        )
                    else:
                        _set_vps_msg(f"HTTP {r.status_code}", ok=False)
                except Exception as exc:
                    _set_vps_msg(f"Помилка: {exc}", ok=False)

            if page_ctrl:
                page_ctrl.run_task(_do)

        def _send_to_vps(_):
            url = str(url_tf.value or "").strip().rstrip("/")
            key = str(key_tf.value or "").strip()
            t = str(time_tf.value or "").strip()
            db_dsn = str(db_dsn_tf.value or "").strip()
            if not url:
                _set_vps_msg("Введіть URL сервісу", ok=False)
                return

            async def _do():
                _set_vps_msg("Відправляємо налаштування...")
                try:
                    import httpx

                    headers = {"x-api-key": key} if key else {}
                    payload = {
                        "api_key": key,
                        "schedule_time": t,
                        "database_url": db_dsn,
                    }
                    async with httpx.AsyncClient() as c:
                        r = await c.post(
                            f"{url}/configure",
                            json=payload,
                            headers=headers,
                            timeout=12,
                        )
                    if r.status_code == 200:
                        _set_vps_msg(
                            f"Надіслано! Розклад VPS: {r.json().get('schedule', '?')}"
                        )
                        with contextlib.suppress(Exception):
                            self.db.set_parser_service_config(url, key)
                            infra_upd = self.db.get_infrastructure_settings()
                            infra_upd["vps_db_dsn"] = db_dsn
                            self.db.set_infrastructure_settings(infra_upd)
                    else:
                        _set_vps_msg(f"HTTP {r.status_code}: {r.text[:80]}", ok=False)
                except Exception as exc:
                    _set_vps_msg(f"Помилка: {exc}", ok=False)

            if page_ctrl:
                page_ctrl.run_task(_do)

        def _migrate_db(_):
            dst_dsn = str(db_dsn_tf.value or "").strip()
            if not dst_dsn:
                _set_vps_msg("Введіть PostgreSQL DSN на VPS", ok=False)
                return

            async def _do():
                vps_progress.visible = True
                with contextlib.suppress(Exception):
                    vps_progress.update()
                _set_vps_msg("Копіюємо дані на VPS (може зайняти кілька хвилин)…")

                def _cb(msg: str):
                    _set_vps_msg(msg)

                ok, msg = await asyncio.to_thread(
                    self.db.migrate_local_to_vps, dst_dsn, _cb
                )
                vps_progress.visible = False
                with contextlib.suppress(Exception):
                    vps_progress.update()
                _set_vps_msg(msg, ok=ok)

            if page_ctrl:
                page_ctrl.run_task(_do)

        def _switch_mode(_):
            new_mode = "vps" if current_mode == "local" else "local"

            async def _do():
                with contextlib.suppress(Exception):
                    infra_upd = await asyncio.to_thread(self.db.get_infrastructure_settings)
                    infra_upd["mode"] = new_mode
                    await asyncio.to_thread(self.db.set_infrastructure_settings, infra_upd)
                if page_ctrl:
                    page_ctrl.run_task(self._background_load, page_ctrl)
                with contextlib.suppress(Exception):
                    self.build()

            if page_ctrl:
                page_ctrl.run_task(_do)

        vps_section = ft.Container(
            padding=14,
            border_radius=10,
            border=ft.border.all(1, _BORDER),
            bgcolor=ft.Colors.BLUE_GREY_50,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text(
                                "VPS Сервіс",
                                size=13,
                                weight=ft.FontWeight.W_700,
                                color=_TEXT,
                            ),
                            mode_badge,
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text(
                        "Встановіть parser_service.py на VPS (pip install fastapi uvicorn apscheduler).",
                        size=11,
                        color=_MUTED,
                    ),
                    url_tf,
                    key_tf,
                    ft.Row(
                        [
                            ft.OutlinedButton(
                                "Перевірити з'єднання",
                                icon=ft.Icons.WIFI_TETHERING,
                                on_click=_test_connection,
                            ),
                            ft.OutlinedButton(
                                (
                                    "VPS режим"
                                    if current_mode == "local"
                                    else "Локальний режим"
                                ),
                                icon=ft.Icons.SWAP_HORIZ,
                                on_click=_switch_mode,
                            ),
                        ],
                        spacing=8,
                        wrap=True,
                    ),
                    ft.Divider(height=8),
                    ft.Text(
                        "Міграція даних — вкажіть PostgreSQL DSN на VPS:",
                        size=11,
                        color=_MUTED,
                        weight=ft.FontWeight.W_600,
                    ),
                    db_dsn_tf,
                    ft.Row(
                        [
                            ft.FilledButton(
                                "Відправити налаштування на VPS",
                                icon=ft.Icons.SEND,
                                on_click=_send_to_vps,
                            ),
                            ft.OutlinedButton(
                                "Скопіювати дані на VPS",
                                icon=ft.Icons.UPLOAD_FILE,
                                on_click=_migrate_db,
                            ),
                        ],
                        spacing=8,
                        wrap=True,
                    ),
                    vps_progress,
                    vps_msg,
                ],
                spacing=8,
                tight=True,
            ),
        )

        return ft.Column(
            [sched_section, ft.SizedBox(height=4), vps_section],
            spacing=0,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )

    # ------------------------------------------------------------------ #
    #  Log tab (SA only)                                                  #
    # ------------------------------------------------------------------ #
    def _build_log_tab(self) -> ft.Control:
        from gsm_parser_runner import LAST_PARSE_LOG_PATH

        log_text = ft.Text(
            "Лог порожній. Запустіть парсинг.",
            size=11,
            font_family="Courier New",
            selectable=True,
            color=ft.Colors.GREEN_300,
        )
        self._log_text_ref = log_text

        def _reload(_=None):
            def _work():
                # DB-backed log first — the parser may be running on a
                # different machine entirely (e.g. the VPS scheduler), in
                # which case the local file below is always stale/empty.
                content = ""
                with contextlib.suppress(Exception):
                    content = self.db.get_gsm_parser_run_log()
                if not content:
                    try:
                        content = LAST_PARSE_LOG_PATH.read_text(encoding="utf-8")
                    except FileNotFoundError:
                        content = "Лог відсутній. Запустіть парсинг."
                    except Exception as exc:
                        content = f"Помилка читання логу: {exc}"
                log_text.value = content
                with contextlib.suppress(Exception):
                    log_text.update()

            threading.Thread(target=_work, daemon=True).start()

        _reload()

        return ft.Column(
            [
                ft.Row(
                    [
                        ft.Text(
                            "Лог останнього парсингу",
                            size=13,
                            weight=ft.FontWeight.W_600,
                            color=_TEXT,
                        ),
                        ft.IconButton(
                            ft.Icons.REFRESH,
                            tooltip="Оновити лог",
                            icon_size=18,
                            on_click=_reload,
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                ft.Container(
                    content=ft.Column(
                        [log_text],
                        scroll=ft.ScrollMode.AUTO,
                        expand=True,
                    ),
                    expand=True,
                    border=ft.border.all(1, _BORDER),
                    border_radius=8,
                    padding=12,
                    bgcolor="#1a1a2e",
                ),
            ],
            expand=True,
            spacing=6,
        )

    # ------------------------------------------------------------------ #
    #  Entry points                                                        #
    # ------------------------------------------------------------------ #
    def build(self):
        # This view relies entirely on its own internal header/body scroll
        # split (table_header stays outside the inner scroll Column — see
        # _build_results_content) instead of the dashboard-level
        # _view_sticky_header slot other views use — it only holds if
        # content_area itself never scrolls, otherwise it "swallows" the
        # inner split and drags the header away with everything else.
        # dashboard.py's _get_or_create_cached_wrapper/_activate_cached_view
        # already special-case "gsm_parser" for this, but this view is also
        # reachable through a SEPARATE, independently-maintained toggle in
        # views/extensions_view.py's Розширення panel (SuperAdmin/Admin) -
        # rather than trust every current and future caller to keep both
        # in sync, enforce it here unconditionally on every entry.
        with contextlib.suppress(Exception):
            self.content_area.scroll = None
        _th = _th_from_view(self)
        global _SOFT, _ALT, _OK_DK, _OK_BG, _TEXT, _ACCENT, _MUTED, _BORDER, _PANEL
        _SOFT = _th["soft"]
        _ALT = _th["alt"]
        _OK_DK = _th["ok"]
        _OK_BG = _th["ok_bg"]
        _TEXT = _th["text"]
        _ACCENT = _th.get("icon_color", "#1D4ED8")
        _MUTED = _th.get("muted", "#475569")
        _BORDER = _th.get("border", "#CBD5E1")
        _PANEL = _th.get("panel", "#FFFFFF")

        # Auto-apply last used preset (set filter state before building the table)
        if self.manager_id:
            import contextlib as _cl

            with _cl.suppress(Exception):
                _last_pid = self.db.get_gsm_last_preset_id(self.manager_id)
                if _last_pid:
                    _all = {
                        str(p.get("id") or ""): p
                        for p in (self.db.get_gsm_user_presets(self.manager_id) or [])
                    }
                    if _last_pid in _all:
                        _pf = _all[_last_pid].get("filters") or {}
                        self._filter_brands = list(_pf.get("brands") or [])
                        self._filter_types = list(_pf.get("types") or [])
                        self._filter_years = list(_pf.get("years") or [])
                        self._filter_statuses = list(_pf.get("statuses") or [])
                        self._filter_source = str(_pf.get("source") or "")
                        self._search = str(_pf.get("search") or "")
                        self._page_idx = 0
                        self._auto_preset_id = _last_pid

        devices_ctrl = self._build_results_content()

        if self.is_superadmin or self.can_run_parser:
            page_ctrl_ref = self._get_page_safe()

            def _lazy_tab_placeholder() -> ft.Container:
                return ft.Container(
                    content=ft.Column(
                        [ft.ProgressRing(width=32, height=32, color=_OK_DK)],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        alignment=ft.MainAxisAlignment.CENTER,
                        height=200,
                    ),
                    padding=ft.padding.only(top=8),
                    expand=True,
                )

            log_container = _lazy_tab_placeholder()
            settings_container = _lazy_tab_placeholder()
            _built_tabs = {"log": False, "settings": False}

            def _build_tab_lazy(key: str):
                if _built_tabs.get(key):
                    return
                _built_tabs[key] = True

                def _work():
                    try:
                        if key == "log":
                            ctrl = self._build_log_tab()
                            log_container.content = ctrl
                            with contextlib.suppress(Exception):
                                log_container.update()
                        elif key == "settings":
                            # Full settings (schedule/VPS/Telegram/backup) for
                            # superadmins; a manager granted only "Запуск
                            # парсингу" gets the brands/links block alone.
                            ctrl = (
                                self._build_settings_tab(page_ctrl_ref)
                                if self.is_superadmin
                                else self._build_brands_only_tab(page_ctrl_ref)
                            )
                            settings_container.content = ctrl
                            with contextlib.suppress(Exception):
                                settings_container.update()
                    except Exception:
                        logger.exception("[GSM] lazy tab '%s' build failed", key)

                threading.Thread(target=_work, daemon=True).start()

            # Manual tab switcher instead of ft.Tabs/TabBar/TabBarView:
            # TabBarView is Flutter's PageView under the hood, and nesting it
            # around content that's independently vertically-scrollable
            # (the brands table, the log list) put the horizontal-swipe and
            # vertical-scroll gesture recognizers into conflict, freezing
            # pointer/scroll input across the whole app. A plain Row of
            # clickable labels swapping a single content Container's
            # `.content` sidesteps PageView entirely — same pattern already
            # used elsewhere in this codebase for tab-like UI.
            _tab_labels = ["Пристрої"]
            _tab_contents = [
                ft.Container(
                    devices_ctrl,
                    padding=ft.padding.only(top=8),
                    expand=True,
                )
            ]
            _settings_tab_idx = 1
            if self.is_superadmin:
                _tab_labels.append("Лог")
                _tab_contents.append(log_container)
                _settings_tab_idx = 2
            _tab_labels.append("Налаштування")
            _tab_contents.append(settings_container)

            _main_content_holder = ft.Container(content=_tab_contents[0], expand=True)
            _tab_buttons: list[ft.Container] = []

            def _tab_btn_style(idx: int, selected: bool) -> None:
                btn = _tab_buttons[idx]
                btn.content.color = _ACCENT if selected else _MUTED
                btn.content.weight = (
                    ft.FontWeight.W_700 if selected else ft.FontWeight.W_400
                )
                btn.border = ft.border.only(
                    bottom=ft.BorderSide(2, _ACCENT if selected else "transparent")
                )

            def _select_main_tab(idx: int, _settings_idx=_settings_tab_idx):
                _main_content_holder.content = _tab_contents[idx]
                for i in range(len(_tab_buttons)):
                    _tab_btn_style(i, i == idx)
                with contextlib.suppress(Exception):
                    _main_content_holder.update()
                    for btn in _tab_buttons:
                        btn.update()
                if self.is_superadmin and idx == 1:
                    _build_tab_lazy("log")
                elif idx == _settings_idx:
                    _build_tab_lazy("settings")

            for i, label_text in enumerate(_tab_labels):
                btn = ft.Container(
                    content=ft.Text(
                        label_text,
                        size=13,
                        weight=ft.FontWeight.W_700 if i == 0 else ft.FontWeight.W_400,
                        color=_ACCENT if i == 0 else _MUTED,
                    ),
                    padding=ft.padding.symmetric(horizontal=16, vertical=10),
                    border=ft.border.only(
                        bottom=ft.BorderSide(2, _ACCENT if i == 0 else "transparent")
                    ),
                    ink=True,
                    on_click=lambda e, idx=i: _select_main_tab(idx),
                )
                _tab_buttons.append(btn)

            main_ctrl = ft.Column(
                [
                    ft.Row(_tab_buttons, spacing=0),
                    ft.Divider(height=1, thickness=1, color=_BORDER),
                    _main_content_holder,
                ],
                spacing=0,
                expand=True,
            )
        else:
            main_ctrl = devices_ctrl

        self.content_area.controls.clear()
        self.content_area.controls.extend(
            [
                ft.Row(
                    [
                        ft.Text(self.t("gsm.title", "GSM Parser — Результати"), size=22, weight=ft.FontWeight.W_700),
                        ft.IconButton(
                            icon=ft.Icons.HELP_OUTLINE,
                            icon_size=18,
                            tooltip=self.t("common.help.tooltip", "Довідка"),
                            on_click=lambda _e: self._show_help(),
                        ),
                    ],
                    spacing=4,
                    tight=True,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                main_ctrl,
            ]
        )
        try:
            if self.content_area.page:
                self.content_area.update()
        except Exception:
            pass
        self._gsm_ui_ready = True

    async def resume(self) -> None:
        """Called by dashboard's fast path when switching back to this
        already-built tab — a pure visibility toggle in the common case.
        Only touches the DB if the daily sync boundary was missed (or the
        parser finished a run elsewhere since our last sync); _needs_reload()
        is the same once-a-day gate used on cold start, so returning to this
        tab many times in one day costs nothing beyond the toggle."""
        # Re-assert on every resume too, not just build() — see build()'s
        # comment for why this can't just trust the caller. Push it right
        # away (not just set the Python-side property) since the function
        # can return early below without any other update() call happening
        # afterward.
        with contextlib.suppress(Exception):
            if self.content_area.scroll is not None:
                self.content_area.scroll = None
                if self.content_area.page:
                    self.content_area.update()
        if not self._gsm_ui_ready:
            return
        if _parser_is_running() or self._cache_busy():
            return
        if not self._needs_reload():
            return
        page = self._get_page_safe()
        if not page:
            return
        _set_loading(True)
        self._set_toolbar_busy(True)
        try:
            await asyncio.to_thread(self._sync_cache_from_db, False)
        finally:
            _set_loading(False)
        self._refresh_table()
        self._hide_loading_overlay()
