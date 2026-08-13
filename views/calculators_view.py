"""
views/calculators_view.py
Multi-purpose calculators modal accessible from the global header.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime

import flet as ft

import i18n
from logger import logger

# ── Palette ────────────────────────────────────────────────────────────────
_ACCENT = "#1E40AF"
_PANEL = "#F8FAFC"
_HDR = "#EFF6FF"
_BORDER = "#E2E8F0"
_OK_FG = "#16A34A"
_WARN_FG = "#B45309"
_ERR_FG = "#DC2626"
_MUTED = "#64748B"
_SOFT = "#F1F5F9"
_DARK = "#1E293B"
_ALT = "#F8FAFC"

# Set once (True) by calculator_web_app.py's main() — the standalone
# GitHub Pages web build. There, every dialog's top-right "X" is
# meaningless (there's nothing behind it to return to; on web it just left
# a blank screen) and gets replaced with a refresh action instead — see
# _close_or_refresh_btn. The desktop app (dashboard.py) and the Windows
# calculator_app.py installer never call set_web_standalone_mode, so this
# stays False for them and the X keeps closing the dialog as before.
_WEB_STANDALONE_MODE = False


def set_web_standalone_mode(value: bool = True) -> None:
    global _WEB_STANDALONE_MODE
    _WEB_STANDALONE_MODE = bool(value)


def _close_or_refresh_btn(page, _close, icon_size: int = 16) -> ft.IconButton:
    """Top-right dialog button: closes the dialog (desktop/embedded), or —
    in the standalone web build — hard-reloads the page instead, since
    there's no underlying screen to reveal by closing. The reload bypasses
    the browser cache (cache-busting query param) so a freshly published
    build is always picked up, and never re-shows the password gate: see
    calculator_web_app.py's _startup, which checks a persisted
    shared_preferences flag before deciding whether to show it."""
    if not _WEB_STANDALONE_MODE:
        return ft.IconButton(ft.Icons.CLOSE, on_click=_close, icon_size=icon_size)

    async def _hard_reload(_e=None):
        base = str(page.url or "").split("?", 1)[0].split("#", 1)[0]
        target = f"{base}?_r={int(time.time())}"
        with contextlib.suppress(Exception):
            await page.launch_url(target, web_popup_window_name=ft.UrlTarget.SELF)

    return ft.IconButton(
        ft.Icons.REFRESH,
        tooltip="Оновити сторінку",
        on_click=_hard_reload,
        icon_size=icon_size,
    )


_CALC_TYPES = [
    ("basic", "Звичайний"),
    ("cost", "Собівартість"),
    ("cost_clo", "С/В з митом"),
    ("profit", "Рентабельність"),
    ("turnover", "Оборот / Забезп."),
]
_CALC_LABELS = dict(_CALC_TYPES)

# Same keys as open_calculators_modal's own icon-grid labels below (reused
# here for the save-name default and the "Збережені" list/filter, so every
# place a calc type's short name shows up translates from one single key
# per type instead of drifting apart into near-duplicates).
_CALC_LABEL_I18N_KEYS: dict[str, tuple[str, str]] = {
    "basic": ("calc.type.basic", "Звичайний"),
    "cost": ("calc.type.cost", "Собівартість"),
    "cost_clo": ("calc.type.cost_clo", "С/В з митом"),
    "profit": ("calc.type.profit", "Рентабельність"),
    "turnover": ("calc.type.turnover", "Оборот / Забезп."),
}


def _calc_type_label(t, calc_type: str) -> str:
    ikey, ua = _CALC_LABEL_I18N_KEYS.get(
        calc_type, (f"calc.{calc_type}.title", _CALC_LABELS.get(calc_type, calc_type))
    )
    return t(ikey, ua)

def _run_in_background(fn) -> None:
    """threading.Thread(...).start() everywhere except the browser (this
    module is also used by calculator_web_app.py's Flet/Pyodide build,
    which runs Python single-threaded — real OS thread creation isn't
    available there and raises "can't start new thread"). Falls back to
    just calling fn() synchronously in that case: slower for that one
    call (blocks until it returns instead of running in the background),
    but the app keeps working instead of crashing outright."""
    try:
        threading.Thread(target=fn, daemon=True).start()
    except Exception:
        fn()


# ── FX cache ───────────────────────────────────────────────────────────────
_FX_CACHE: dict = {"ts": 0.0, "rates": {}}
_FX_REFRESH_IN_FLIGHT = False
_FX_LAST_ERRORS: list[str] = []

# Pyodide (the in-browser Python runtime calculator_web_app.py runs under)
# has no raw sockets, so urllib.request.urlopen raises "URLError: unknown
# url type: https" for every real request — confirmed live in the web
# build. sys.platform == "emscripten" is the standard way to detect it.
_IS_PYODIDE = sys.platform == "emscripten"

_NBU_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
_ER_API_URL = "https://open.er-api.com/v6/latest/UAH"
_FAWAZAHMED0_URL = (
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/"
    "v1/currencies/uah.json"
)


def _parse_nbu(data) -> dict:
    if not isinstance(data, list):
        return {}
    out: dict = {"UAH": 1.0}
    for row in data:
        cc = str(row.get("cc") or "").upper().strip()
        rate = row.get("rate")
        unit = float(row.get("unit") or 1) or 1
        if cc and rate:
            out[cc] = float(rate) / unit
    return out


def _parse_uah_rates_map(raw, key: str) -> dict:
    """Shared parser for open.er-api.com and fawazahmed0 — both return
    {key: {"USD": 0.024, ...}} (UAH per unit foreign), inverted here to the
    same "units of CCY per 1 UAH" shape _parse_nbu already produces."""
    rts = (raw or {}).get(key) if isinstance(raw, dict) else {}
    out: dict = {"UAH": 1.0}
    for cc, v in (rts or {}).items():
        cc_u = str(cc).upper().strip()
        fv = float(v) if v else 0
        if cc_u and cc_u != "UAH" and fv > 0:
            out[cc_u] = 1.0 / fv
    return out


_FX_SOURCES = (
    (_NBU_URL, _parse_nbu),
    (_ER_API_URL, lambda raw: _parse_uah_rates_map(raw, "rates")),
    (_FAWAZAHMED0_URL, lambda raw: _parse_uah_rates_map(raw, "uah")),
)


def _fetch_uah_rates(page=None) -> dict:
    """Cache-only read — NEVER performs blocking network I/O on the calling
    thread. If the cache is missing/stale, a background refresh is kicked
    off (a daemon thread on desktop, an async page task in the browser —
    see `_refresh_uah_rates_background`/`_refresh_uah_rates_async_pyodide`)
    and the best data available right now is returned immediately.

    Why: this used to fetch NBU/er-api rates INLINE (up to ~12s worst case:
    two sequential 6s-timeout requests) whenever the cache expired. Both
    callers (`_open_cost`, `_open_profit`) run synchronously in the click
    handler that opens the calculator dialog, so that inline fetch froze the
    whole app for however long the HTTP calls took — every time a user
    opened "Собівартість"/"Рентабельність" with a cold cache.

    page is optional (existing desktop call sites that don't pass it keep
    working) but required to actually refresh on the web build — Pyodide
    has no threads, so without a page to schedule the async fetch on, a
    stale/empty web cache just stays stale."""
    global _FX_CACHE, _FX_REFRESH_IN_FLIGHT
    now = time.time()
    ts = float(_FX_CACHE.get("ts") or 0)
    rates = _FX_CACHE.get("rates") or {}
    is_fresh = ts > 0 and (now - ts) < (900 if rates else 120)
    if not is_fresh and not _FX_REFRESH_IN_FLIGHT:
        _FX_REFRESH_IN_FLIGHT = True
        if _IS_PYODIDE and page is not None:
            with contextlib.suppress(Exception):
                page.run_task(_refresh_uah_rates_async_pyodide)
        elif not _IS_PYODIDE:
            _run_in_background(_refresh_uah_rates_background)
        else:
            _FX_REFRESH_IN_FLIGHT = False
    return rates


def _refresh_uah_rates_background() -> None:
    global _FX_CACHE, _FX_REFRESH_IN_FLIGHT
    try:
        fresh = _fetch_uah_rates_blocking()
        if fresh:
            _FX_CACHE = {"ts": time.time(), "rates": fresh}
    finally:
        _FX_REFRESH_IN_FLIGHT = False


def _fetch_uah_rates_blocking() -> dict:
    """Actual network fetch via urllib — desktop only (real sockets
    required). Blocking — only ever call from a background thread (see
    `_refresh_uah_rates_background`), never from the UI thread."""

    def _get(url: str):
        with urllib.request.urlopen(url, timeout=6) as r:
            return json.loads(r.read().decode())

    return _try_fx_sources_sync(_get)


async def _refresh_uah_rates_async_pyodide() -> None:
    global _FX_CACHE, _FX_REFRESH_IN_FLIGHT
    try:
        fresh = await _fetch_uah_rates_async_pyodide()
        if fresh:
            _FX_CACHE = {"ts": time.time(), "rates": fresh}
    finally:
        _FX_REFRESH_IN_FLIGHT = False


async def _fetch_uah_rates_async_pyodide() -> dict:
    """Browser equivalent of `_fetch_uah_rates_blocking` — uses
    pyodide.http.pyfetch (wraps the browser's own fetch API) instead of
    urllib, since urllib can't open real sockets in the Pyodide sandbox.
    Import is local so the desktop build never needs the pyodide package."""
    from pyodide.http import pyfetch

    async def _get(url: str):
        resp = await pyfetch(url, method="GET")
        return await resp.json()

    return await _try_fx_sources_async(_get)


def _fx_source_error(exc_or_none, name: str, result: dict) -> str | None:
    if exc_or_none is not None:
        exc = exc_or_none
        return f"{name}: {type(exc).__name__}: {exc}"
    if not result:
        return f"{name}: порожня відповідь"
    return None


def _try_fx_sources_sync(get_fn) -> dict:
    """Try each FX source in order (see `_FX_SOURCES`) using a plain sync
    callable(url) -> parsed JSON. Desktop/urllib path — see
    `_try_fx_sources_async` for the Pyodide/pyfetch equivalent (kept as a
    separate function since an async def always returns a coroutine, even
    when its body happens not to await anything on a given call — it can't
    double as a sync callable)."""
    global _FX_LAST_ERRORS
    errors: list[str] = []
    result: dict = {}
    for url, parser in _FX_SOURCES:
        name = url.split("/")[2]
        try:
            result = parser(get_fn(url)) or {}
        except Exception as exc:
            errors.append(_fx_source_error(exc, name, result))
            continue
        if result.get("USD") and result.get("UAH"):
            _FX_LAST_ERRORS = []
            return result
        err = _fx_source_error(None, name, result)
        if err:
            errors.append(err)

    _FX_LAST_ERRORS = errors
    if errors:
        logger.warning("[calc] fx rate fetch failed for all sources: %s", "; ".join(errors))
    return result


async def _try_fx_sources_async(get_fn) -> dict:
    """Async twin of `_try_fx_sources_sync` for the Pyodide/pyfetch path —
    get_fn is an async callable(url) -> parsed JSON."""
    global _FX_LAST_ERRORS
    errors: list[str] = []
    result: dict = {}
    for url, parser in _FX_SOURCES:
        name = url.split("/")[2]
        try:
            result = parser(await get_fn(url)) or {}
        except Exception as exc:
            errors.append(_fx_source_error(exc, name, result))
            continue
        if result.get("USD") and result.get("UAH"):
            _FX_LAST_ERRORS = []
            return result
        err = _fx_source_error(None, name, result)
        if err:
            errors.append(err)

    _FX_LAST_ERRORS = errors
    if errors:
        logger.warning("[calc] fx rate fetch failed for all sources: %s", "; ".join(errors))
    return result


def _convert(amount: float, from_ccy: str, to_ccy: str, rates: dict) -> float | None:
    fc, tc = from_ccy.upper(), to_ccy.upper()
    if fc == tc:
        return amount
    fr = rates.get(fc)
    to = rates.get(tc)
    if not fr or not to:
        return None
    return amount * float(fr) / float(to)


def _resolve_region_id(db, company_id: str, region_name: str) -> str:
    """Convert region name to UUID using company regions lookup."""
    if not region_name or not company_id:
        return region_name or ""
    try:
        regions = db.get_company_regions(company_id) or []
        name_to_id = {
            str(r.get("name") or "").strip().lower(): str(r.get("id") or "").strip()
            for r in regions
            if isinstance(r, dict) and r.get("name") and r.get("id")
        }
        uid = name_to_id.get(str(region_name).strip().lower(), "")
        if uid:
            return uid
    except Exception:
        pass
    return region_name


def _fmt_val(v: float, digits: int = 2) -> str:
    if abs(v) >= 1000:
        return f"{v:,.{digits}f}"
    if abs(v) < 0.0001 and v != 0:
        return f"{v:.6f}"
    return f"{v:.{digits}f}"


def _fmt_money(v: float, ccy: str) -> str:
    syms = {"USD": "$", "EUR": "€", "UAH": "₴", "PLN": "zł", "CNY": "¥"}
    s = syms.get(ccy.upper(), ccy + " ")
    return s + _fmt_val(v)


def _safe_float(v) -> float | None:
    try:
        return float(str(v or "").replace(",", ".").strip())
    except Exception:
        return None


# ── Saved calculations ─────────────────────────────────────────────────────
_PREF_KEY = "calculators_saved_v1"


def _load_saved(db, manager_id: str) -> list[dict]:
    try:
        raw = db.get_manager_ui_preference(manager_id, _PREF_KEY, "[]")
        return json.loads(raw) if raw else []
    except Exception:
        return []


def _persist_saved(db, manager_id: str, saved: list[dict]) -> None:
    try:
        db.set_manager_ui_preference(
            manager_id, _PREF_KEY, json.dumps(saved[:100], ensure_ascii=False)
        )
    except Exception:
        pass


def _save_calc_entry(
    db, manager_id: str, calc_type: str, name: str, data: dict
) -> None:
    saved = _load_saved(db, manager_id)
    saved.insert(
        0,
        {
            "id": str(int(time.time() * 1000)),
            "calc_type": calc_type,
            "calc_label": _CALC_LABELS.get(calc_type, calc_type),
            "name": name,
            "ts": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "data": data,
        },
    )
    _persist_saved(db, manager_id, saved)


def _delete_calc_entry(db, manager_id: str, entry_id: str) -> None:
    saved = _load_saved(db, manager_id)
    _persist_saved(
        db, manager_id, [s for s in saved if str(s.get("id") or "") != entry_id]
    )


def _rename_calc_entry(db, manager_id: str, entry_id: str, new_name: str) -> None:
    saved = _load_saved(db, manager_id)
    for s in saved:
        if str(s.get("id") or "") == entry_id:
            s["name"] = new_name
            break
    _persist_saved(db, manager_id, saved)


def _show_save_dialog(page, db, manager_id: str, calc_type: str, data: dict):
    _t = i18n.bind(str(manager_id or ""))
    _ACTIVE_MANAGER_ID[0] = str(manager_id or "")
    name_f = ft.TextField(
        label=_t("calc.save_dlg.name_label", "Назва розрахунку"), width=300, border_radius=8, autofocus=True
    )
    sdlg = ft.AlertDialog(modal=True)

    def _do(_e=None):
        nm = str(name_f.value or "").strip() or _calc_type_label(_t, calc_type)
        _save_calc_entry(db, manager_id, calc_type, nm, data)
        with contextlib.suppress(Exception):
            page.pop_dialog()

    def _save_dlg_help(_):
        from views.help_utils import show_modal_help
        show_modal_help(page, _t("calc.save_dlg.title", "Зберегти розрахунок"), [
            _t("calc.save_help.step1", "1. Введіть назву розрахунку."),
            _t("calc.save_help.step2", "2. Натисніть «Зберегти» — розрахунок додається до «Збережених»."),
        ])

    sdlg.title = ft.Row([
        ft.Text(_t("calc.save_dlg.title", "Зберегти розрахунок"), weight="bold", expand=True,
                no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
        ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=_t("common.help.tooltip", "Довідка"), on_click=_save_dlg_help),
    ], vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True)
    sdlg.content = ft.Container(width=320, padding=16, content=name_f)
    sdlg.actions = [
        ft.TextButton(_t("common.cancel", "Скасувати"), on_click=lambda _: page.pop_dialog()),
        ft.FilledButton(_t("common.save", "Зберегти"), on_click=_do),
    ]
    sdlg.actions_alignment = ft.MainAxisAlignment.END
    name_f.on_submit = _do
    page.show_dialog(sdlg)


# ── Shared widgets ─────────────────────────────────────────────────────────
# Set to the manager_id of whichever calculator panel is currently being
# built (each _open_*/_build_*_panel sets this right after binding its own
# _t), so these two generic helpers can translate their small bits of
# boilerplate text without threading a `t` callable through every one of
# their ~40 call sites across the file.
_ACTIVE_MANAGER_ID: list[str] = [""]


def _field(
    label: str, width=180, value="", number=True, optional=False
) -> ft.TextField:
    _t = i18n.bind(_ACTIVE_MANAGER_ID[0])
    lbl = label + (" " + _t("calc.field.optional_suffix", "(необ.)") if optional else "")
    return ft.TextField(
        label=lbl,
        value=str(value) if value else "",
        width=width,
        dense=True,
        keyboard_type=ft.KeyboardType.NUMBER if number else ft.KeyboardType.TEXT,
        border_radius=6,
        content_padding=ft.padding.symmetric(horizontal=10, vertical=6),
        label_style=ft.TextStyle(size=10),
    )


def _dd(
    label: str, opts: list[tuple[str, str]], value: str = "", width=180
) -> ft.Dropdown:
    _t = i18n.bind(_ACTIVE_MANAGER_ID[0])
    options = [ft.dropdown.Option(key="", text=_t("calc.field.choose_placeholder", "— оберіть —"))] + [
        ft.dropdown.Option(key=k, text=v) for k, v in opts
    ]
    return ft.Dropdown(
        label=label, options=options, value=value or None, width=width,
        border_radius=6, dense=True,
        label_style=ft.TextStyle(size=10),
        text_size=12,
        content_padding=ft.padding.symmetric(horizontal=10, vertical=6),
    )


def _section(title: str) -> ft.Container:
    return ft.Container(
        content=ft.Text(title, size=10, weight="bold", color=_MUTED),
        padding=ft.padding.only(top=5, bottom=1),
    )


def _cell(
    text: str, w: int, bold=False, muted=False, right=False, header=False
) -> ft.Container:
    return ft.Container(
        width=w,
        height=32 if not header else 38,
        bgcolor=_HDR if header else None,
        border=ft.border.all(1, _BORDER),
        alignment=ft.Alignment(1 if right else -1, 0),
        padding=ft.padding.symmetric(horizontal=6),
        content=ft.Text(
            text,
            size=10,
            weight="bold" if bold or header else None,
            color=_MUTED if muted else (_DARK if not header else _ACCENT),
            overflow=ft.TextOverflow.ELLIPSIS,
            no_wrap=True,
            text_align=ft.TextAlign.RIGHT if right else ft.TextAlign.LEFT,
        ),
    )


# ── Floating panel (non-modal draggable overlay) ──────────────────────────

_FLOAT_PANEL_KEY = "__calc_float_panel__"


class _FloatDialog:
    """
    Non-modal draggable panel rendered via page.overlay.
    Replaces ft.AlertDialog so the calculator stays visible while the user
    works on the main page. Drag it by the title bar.
    """

    # Class-level (not per-instance) — calculator_app.py/calculator_web_app.py's
    # _install_fullscreen_dialogs() flips this to True once at process
    # startup, before any dialog exists. _resize_holder() (below, in
    # _run_multi_panel_calculator) reads it to decide whether to cap
    # row_holder's width to the available window width (CatMan Pro's
    # draggable dialog — no outer scroll wrapper, still needs the cap +
    # panels_row's own internal scroll to reach overflow) or let it size
    # naturally (the fullscreen apps' own _open override wraps everything
    # in its own outer horizontal+vertical scroll, making an inner cap
    # redundant — and worse, WRONG, since it's computed once and goes
    # stale after the window/zoom later changes).
    _is_fullscreen_build: bool = False

    def __init__(self):
        self.title = None
        self.content = None
        self.actions: list = []
        self.actions_alignment = None
        self._panel: ft.Container | None = None
        # Set to the exact outer pixel width of dlg.content so the title bar
        # can be stretched to the same width and expand=True on the title text works.
        self.panel_width: int | None = None
        # Only meaningful for the portable app's fullscreen _open override
        # (see calculator_app.py._install_fullscreen_dialogs) — the picker
        # is a small fixed-size grid of buttons, not a form meant to fill
        # the window, so it should sit centered in the available space
        # instead of pinned to the top-left corner like the calculator
        # forms (which deliberately DO fill available width/height).
        self.center_content: bool = False

    def _open(self, page) -> None:
        _x = 24.0
        _y = 52.0

        _ref = [None]
        _last_gp = [0.0, 0.0]  # last known global pointer position

        def _on_drag_start(e: ft.DragStartEvent):
            _last_gp[0] = e.global_position.x
            _last_gp[1] = e.global_position.y

        def _on_drag(e: ft.DragUpdateEvent):
            p = _ref[0]
            if p is None:
                return
            dx = e.global_position.x - _last_gp[0]
            dy = e.global_position.y - _last_gp[1]
            _last_gp[0] = e.global_position.x
            _last_gp[1] = e.global_position.y
            p.left = max(0.0, (p.left or 0.0) + dx)
            p.top = max(0.0, (p.top or 0.0) + dy)
            with contextlib.suppress(Exception):
                p.update()

        title_bar = ft.GestureDetector(
            on_pan_start=_on_drag_start,
            on_pan_update=_on_drag,
            mouse_cursor=ft.MouseCursor.MOVE,
            drag_interval=14,
            content=ft.Container(
                content=self.title,
                padding=ft.padding.symmetric(horizontal=16, vertical=9),
                bgcolor=_HDR,
                border_radius=ft.border_radius.only(top_left=12, top_right=12),
                border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
            ),
        )

        body: list = [title_bar]
        if self.content is not None:
            if getattr(self, "center_content", False):
                # Small, never-overflowing content (the picker) — left as-is,
                # no height cap/scroll needed.
                body.append(self.content)
            else:
                # Cap content height to fit within the CatMan Pro window and
                # let it scroll internally instead of silently growing past
                # the visible area — this modal floats via page.overlay at a
                # fixed (left, top) position with NO height constraint of
                # its own, so a tall calculator form (many delivery methods,
                # price tiers, etc.) could extend below the bottom of the
                # window with no way to reach the rest of it. CatMan Pro's
                # own Ctrl+/- shortcut resizes the whole APP WINDOW, not
                # this floating panel, so it doesn't help here either.
                avail_h = 800.0
                with contextlib.suppress(Exception):
                    avail_h = float(
                        getattr(page, "height", None)
                        or getattr(page.window, "height", None)
                        or 800.0
                    )
                content_max_h = max(300.0, avail_h - _y - 130.0)
                body.append(
                    ft.Container(
                        content=ft.Column(
                            [self.content],
                            scroll=ft.ScrollMode.AUTO,
                        ),
                        height=content_max_h,
                    )
                )
        if self.actions:
            body.append(
                ft.Container(
                    content=ft.Row(
                        self.actions,
                        alignment=self.actions_alignment or ft.MainAxisAlignment.END,
                        wrap=True,
                    ),
                    padding=ft.padding.only(left=16, right=16, bottom=12, top=4),
                )
            )

        # STRETCH is safe only when panel has an explicit width (bounded Column).
        # Without explicit width the Column would propagate infinite constraints → hang.
        _pw = self.panel_width
        panel = ft.Container(
            left=_x,
            top=_y,
            width=_pw if _pw else None,
            bgcolor=_PANEL,
            border_radius=12,
            border=ft.border.all(1, "#CBD5E1"),
            shadow=ft.BoxShadow(
                blur_radius=24,
                spread_radius=0,
                color="#28000000",
                offset=ft.Offset(0, 6),
            ),
            content=ft.Column(
                body,
                spacing=0,
                tight=True,
                horizontal_alignment=(
                    ft.CrossAxisAlignment.STRETCH if _pw else ft.CrossAxisAlignment.START
                ),
            ),
        )
        _ref[0] = panel
        self._panel = panel

        old = getattr(page, _FLOAT_PANEL_KEY, None)
        if old is not None and old in page.overlay:
            panel.left = old.left or _x
            panel.top = old.top or _y
            page.overlay.remove(old)

        setattr(page, _FLOAT_PANEL_KEY, panel)
        page.overlay.append(panel)
        with contextlib.suppress(Exception):
            page.update()

    def _close(self, page) -> None:
        if self._panel is not None:
            if self._panel in page.overlay:
                page.overlay.remove(self._panel)
            if self._panel is getattr(page, _FLOAT_PANEL_KEY, None):
                setattr(page, _FLOAT_PANEL_KEY, None)
        with contextlib.suppress(Exception):
            page.update()


def _fdlg_open(page, dlg: _FloatDialog) -> None:
    dlg._open(page)


def _fdlg_close(page, dlg: _FloatDialog) -> None:
    dlg._close(page)


def _rate_texts(
    buy_ccy: str, cost_ccy: str, extra_ccys: set, rates: dict
) -> list[ft.Text]:
    all_ccys = {buy_ccy.upper(), cost_ccy.upper()} | {c.upper() for c in extra_ccys}
    all_ccys.discard("")
    texts = []
    seen: set = set()
    for c1 in sorted(all_ccys):
        for c2 in sorted(all_ccys):
            if c1 == c2:
                continue
            key = tuple(sorted([c1, c2]))
            if key in seen:
                continue
            seen.add(key)
            v = _convert(1.0, c1, c2, rates)
            if v:
                texts.append(
                    ft.Text(f"1 {c1} = {_fmt_val(v, 4)} {c2}", size=9, color=_MUTED)
                )
    return texts


# ─────────────────────────────────────────────────────────────────────────────
# ── 1. Звичайний ──────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
def _open_basic(page, db, manager_id: str, back_fn=None):
    _t = i18n.bind(str(manager_id or ""))
    _ACTIVE_MANAGER_ID[0] = str(manager_id or "")
    dlg = _FloatDialog()
    expr_text = ft.Text(
        "", size=11, color=_MUTED, text_align=ft.TextAlign.RIGHT, selectable=True
    )
    val_text = ft.Text(
        "0", size=30, weight="bold", text_align=ft.TextAlign.RIGHT, selectable=True
    )

    state = {"expr": "", "result": None, "eq": False, "error": False}
    _kb_active = [False]

    # Visual dot: green = keyboard captured, grey = passive
    _kb_dot = ft.Container(
        width=8, height=8,
        bgcolor="#94A3B8",
        border_radius=4,
        tooltip=_t("calc.basic.kb_hint", "Клацніть кнопку калькулятора щоб активувати клавіатуру"),
    )

    def _set_kb_active(val: bool):
        _kb_active[0] = val
        _kb_dot.bgcolor = "#22C55E" if val else "#94A3B8"
        with contextlib.suppress(Exception):
            _kb_dot.update()

    def _on_focus_gain(e):
        _set_kb_active(True)

    def _on_focus_lose(e):
        _set_kb_active(False)

    focus_field = ft.TextField(
        width=236,
        height=2,
        border_color="transparent",
        focused_border_color="transparent",
        color="transparent",
        bgcolor="transparent",
        cursor_color="transparent",
        cursor_height=0.1,
        content_padding=ft.padding.all(0),
        autofocus=True,
        on_focus=_on_focus_gain,
        on_blur=_on_focus_lose,
    )

    async def _refocus_async():
        await asyncio.sleep(0.04)
        with contextlib.suppress(Exception):
            await focus_field.focus()
        with contextlib.suppress(Exception):
            page.update()

    def _activate_keyboard(_=None):
        _set_kb_active(True)
        with contextlib.suppress(Exception):
            page.run_task(_refocus_async)

    def _close(_=None):
        with contextlib.suppress(Exception):
            # Restore the exe's Ctrl+Plus/Minus zoom handler (if any — only
            # set there, never on CatMan Pro's page) instead of just wiping
            # the slot to None, so zoom keeps working for whatever
            # calculator gets opened next.
            _zoom_check = getattr(page, "_catman_zoom_check", None)
            page.on_keyboard_event = _zoom_check
        with contextlib.suppress(Exception):
            _fdlg_close(page, dlg)

    async def _go_back(_=None):
        _close()
        if back_fn:
            await asyncio.sleep(0.05)
            with contextlib.suppress(Exception):
                back_fn()

    def _refresh():
        if state["error"]:
            val_text.value = _t("calc.basic.error", "Помилка")
            expr_text.value = state["expr"]
        elif state["eq"]:
            r = state["result"]
            val_text.value = _fmt_val(r) if r is not None else "0"
            expr_text.value = state["expr"] + " ="
        else:
            val_text.value = state["expr"] or "0"
            expr_text.value = ""
        with contextlib.suppress(Exception):
            page.update()

    def _press(t: str):
        e, eq = state["expr"], state["eq"]
        if state["error"]:
            state.update(error=False, expr="", result=None, eq=False)
            e = ""

        if t == "C":
            state.update(expr="", result=None, eq=False, error=False)
            _refresh()
            return

        if t == "←":
            state["eq"] = False
            state["expr"] = "" if eq else e[:-1]
            _refresh()
            return

        if t == "=":
            raw = str(state["result"]) if eq else e
            if not raw:
                return
            try:
                safe = raw.replace("×", "*").replace("÷", "/")
                safe = re.sub(
                    r"√\((.+?)\)", lambda m: str(math.sqrt(float(m.group(1)))), safe
                )
                safe = re.sub(
                    r"\((.+?)\)\^2", lambda m: str(float(m.group(1)) ** 2), safe
                )
                result = eval(safe, {"__builtins__": {}}, {})  # nosec
                result = round(float(result), 10)
                if result == int(result):
                    result = int(result)
                state.update(result=result, eq=True, expr=raw)
            except Exception:
                state.update(error=True, eq=False, expr=raw)
            _refresh()
            return

        if t == "%":
            raw = str(state["result"]) if eq else e
            try:
                val = float(raw) / 100
                state.update(expr=str(val), result=val, eq=True)
            except Exception:
                pass
            _refresh()
            return

        if t == "√":
            raw = str(state["result"]) if eq else e
            try:
                val = math.sqrt(float(raw))
                state.update(expr=f"√({raw})", result=round(val, 10), eq=True)
            except Exception:
                state.update(error=True)
            _refresh()
            return

        if t == "x²":
            raw = str(state["result"]) if eq else e
            try:
                val = float(raw) ** 2
                state.update(expr=f"({raw})^2", result=round(val, 10), eq=True)
            except Exception:
                state.update(error=True)
            _refresh()
            return

        if eq:
            state["eq"] = False
            state["expr"] = str(state["result"]) + t if t in "+-×÷" else t
        else:
            state["expr"] = e + t
        _refresh()

    _KEY_MAP = {
        "0": "0", "1": "1", "2": "2", "3": "3", "4": "4",
        "5": "5", "6": "6", "7": "7", "8": "8", "9": "9",
        "Numpad 0": "0", "Numpad 1": "1", "Numpad 2": "2",
        "Numpad 3": "3", "Numpad 4": "4", "Numpad 5": "5",
        "Numpad 6": "6", "Numpad 7": "7", "Numpad 8": "8", "Numpad 9": "9",
        "+": "+", "-": "-", "*": "×", "/": "÷",
        "Numpad Add": "+", "Numpad Subtract": "-",
        "Numpad Multiply": "×", "Numpad Divide": "÷",
        ".": ".", ",": ".", "Numpad Decimal": ".",
        "Delete": "C", "Escape": "C",
        "Backspace": "←",
        "Enter": "=", "Numpad Enter": "=",
    }

    def _on_key(e: ft.KeyboardEvent):
        # Only set on the exe's page (see calculator_app.py's main()) —
        # None/no-op everywhere else, including CatMan Pro.
        _zoom_check = getattr(page, "_catman_zoom_check", None)
        if _zoom_check and _zoom_check(e):
            return
        if not _kb_active[0]:
            return
        token = _KEY_MAP.get(e.key)
        if token:
            _press(token)

    page.on_keyboard_event = _on_key

    _BTN_W, _BTN_H, _GRID_W = 52, 44, 236

    def _cbtn(label: str, w=_BTN_W, h=_BTN_H, primary=False, danger=False):
        color = "white" if primary else (_ERR_FG if danger else _DARK)
        bg = _ACCENT if primary else ("#FFF1F2" if danger else _SOFT)

        def _btn_click(_, t=label):
            _activate_keyboard()
            _press(t)

        return ft.Container(
            content=ft.Text(
                label, size=13, weight="bold", color=color,
                text_align=ft.TextAlign.CENTER,
            ),
            width=w, height=h, bgcolor=bg,
            border_radius=8,
            border=ft.border.all(1, _BORDER),
            alignment=ft.Alignment(0, 0),
            on_click=_btn_click, ink=True,
        )

    display = ft.Container(
        content=ft.Column(
            [expr_text, val_text],
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.END,
        ),
        bgcolor=_SOFT,
        border_radius=8,
        padding=ft.padding.symmetric(horizontal=10, vertical=6),
        width=_GRID_W,
    )

    grid = ft.Column(
        [
            ft.Row([_cbtn("C", danger=True), _cbtn("←"), _cbtn("%"), _cbtn("÷")], spacing=4),
            ft.Row([_cbtn("7"), _cbtn("8"), _cbtn("9"), _cbtn("×")], spacing=4),
            ft.Row([_cbtn("4"), _cbtn("5"), _cbtn("6"), _cbtn("-")], spacing=4),
            ft.Row([_cbtn("1"), _cbtn("2"), _cbtn("3"), _cbtn("+")], spacing=4),
            ft.Row([_cbtn("√"), _cbtn("0"), _cbtn("."), _cbtn("x²")], spacing=4),
            ft.Row([_cbtn("=", w=_GRID_W, primary=True)], spacing=4),
        ],
        spacing=4,
    )

    def _do_save(_=None):
        raw = (
            str(state["result"])
            if state["eq"] and state["result"] is not None
            else state["expr"]
        )
        _show_save_dialog(
            page, db, manager_id, "basic", {"expr": state["expr"], "result": raw}
        )

    def _on_lang_select(code: str) -> None:
        # No prefill support on Basic (it's a scratch calculator, not
        # comparison panels) — reopening just clears the display, same as
        # closing and reopening it manually would.
        i18n.save_manager_language(db, str(manager_id or ""), code)
        _close()
        _open_basic(page, db, manager_id, back_fn=back_fn)

    dlg.title = ft.Row(
        [
            ft.Text(_t("calc.basic.title", "Звичайний"), weight="bold", size=12, expand=True,
                    no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
            _kb_dot,
            i18n.build_language_switcher(db, _on_lang_select, manager_id=str(manager_id or "")),
            ft.IconButton(ft.Icons.BOOKMARK_ADD_OUTLINED, tooltip=_t("common.save", "Зберегти"),
                          on_click=_do_save, icon_size=18),
            *(
                [ft.IconButton(ft.Icons.ARROW_BACK, tooltip=_t("common.back", "Назад"),
                               on_click=_go_back, icon_size=16)]
                if back_fn else []
            ),
            _close_or_refresh_btn(page, _close, icon_size=16),
        ]
    )
    _PW = _GRID_W + 32  # 268
    dlg.panel_width = _PW
    dlg.content = ft.Container(
        width=_PW,
        padding=ft.padding.symmetric(horizontal=16, vertical=6),
        content=ft.Column([
            focus_field,
            ft.GestureDetector(
                on_tap=_activate_keyboard,
                content=ft.Column(
                    [display, ft.Divider(height=4), grid],
                    spacing=6, tight=True,
                ),
            ),
        ], spacing=0, tight=True),
    )
    _fdlg_open(page, dlg)


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# ── 2. Собівартість ── повністю переписано для Flet 0.84 ─────────────────────
# ─────────────────────────────────────────────────────────────────────────────
_C_OK    = "#15803D"
_C_WARN  = "#B45309"
_C_ERR   = "#DC2626"
_C_BLUE  = "#1D4ED8"
_C_BG    = "#F8FAFC"
_C_BG2   = "#EFF6FF"
_C_CARD  = "#FFFFFF"

_LEFT_W   = 246   # left panel container width (-15%, so 3 comparison panels fit the window width)
_INP_W    = 233   # inputs column width (-15%)
_RIGHT_W  = 280   # right panel container width (-15%)
_RES_W    = 267   # results column width (-15%)
_PANEL_H  = 520   # panel height


def _cost_row(label: str, value: str, color: str = _DARK,
              bold: bool = False, small: bool = False) -> ft.Row:
    sz = 10 if small else 12
    return ft.Row(
        [
            ft.Text(label, size=sz, color=_MUTED, expand=True),
            ft.Text(value, size=sz, color=color,
                    weight=ft.FontWeight.W_700 if bold else None),
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        spacing=4,
    )


# Soft, semi-transparent grey (not the solid _BORDER) — specifically for the
# row/column rule lines inside the inline %-breakdown rows in _cost_card;
# left slightly wider (1.4 vs the plain 1px dividers elsewhere) since a
# fully-opaque grey at that width read as "dark" against the card's white
# background.
_LINE_SOFT = ft.Colors.with_opacity(0.30, "#000000")


def _cost_card(
    method_name: str,
    rows: list,  # list of (label, value, color, bold, pct) — pct is the row's
                 # % share of the card's final total, or None for rows that
                 # shouldn't show one (the final total itself, "Ціна при
                 # рент.", price-tier margin rows).
    *,
    highlight: bool = False,
) -> ft.Container:
    # All sizes/widths in this function are -15% vs. their original values,
    # matching the same reduction applied to _LEFT_W/_RIGHT_W — so 3
    # comparison panels fit side-by-side in the window instead of
    # overflowing into horizontal scroll.
    row_widgets = []
    for lbl, val, clr, bld, pct in rows:
        if pct is None:
            if val.endswith("%"):
                # Price-tier margin rows (B2B/Vip/Dealer/...) — their value
                # IS a percentage. Badged too, like the cost-share %s, but
                # plain light-grey/black — not the semantic green/amber/red
                # (_C_OK/_C_WARN/_C_ERR) it used to be, since that green
                # kept reading as the same green as the bold "Собівартість
                # з митом:" total right above it.
                value_text = ft.Container(
                    ft.Text(val, size=9, color=_DARK, weight=ft.FontWeight.W_700),
                    bgcolor=_SOFT,
                    border_radius=5,
                    padding=ft.padding.symmetric(horizontal=6, vertical=2),
                    alignment=ft.Alignment(0, 0),
                )
            else:
                value_text = ft.Text(val, size=9, color=clr, weight=ft.FontWeight.W_700 if bld else None)
            row_widgets.append(
                ft.Row(
                    [ft.Text(lbl, size=9, color=_MUTED, expand=True), value_text],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                )
            )
            continue
        # Rows that carry a %-share use the per-row `clr` — each
        # semantically distinct row (a plain cost component vs. the duty
        # amount vs. the pre-duty subtotal) gets its OWN fixed color so
        # they read as different things at a glance (see call sites for
        # which color means what). `bld` marks "highlight" rows (duty,
        # subtotal) — bigger/bold/colored — vs. plain leaf cost rows,
        # which stay small neutral dark. The % is a solid-background badge
        # (not just colored text) so it never reads as "another number"
        # sitting next to the price.
        value_text = ft.Text(
            val, size=11 if bld else 10,
            color=clr if bld else _DARK,
            weight=ft.FontWeight.W_700 if bld else ft.FontWeight.W_500,
        )
        pct_badge = ft.Container(
            ft.Text(f"{pct:.1f}%", size=9, color=ft.Colors.WHITE, weight=ft.FontWeight.W_700),
            bgcolor=_MUTED,
            border_radius=5,
            padding=ft.padding.symmetric(horizontal=6, vertical=2),
            width=46,
            alignment=ft.Alignment(0, 0),
        )
        row_widgets.append(
            ft.Container(
                content=ft.Row(
                    [
                        ft.Text(lbl, size=9, color=_MUTED, expand=True),
                        ft.Container(value_text, width=77, alignment=ft.Alignment(1, 0)),
                        pct_badge,
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.padding.only(bottom=6, top=3),
                border=ft.border.only(bottom=ft.BorderSide(1.2, _LINE_SOFT)),
            )
        )

    return ft.Container(
        bgcolor=_C_BG2 if highlight else _C_CARD,
        border=ft.border.all(2 if highlight else 1, _C_BLUE if highlight else _BORDER),
        border_radius=10,
        padding=ft.padding.symmetric(horizontal=14, vertical=10),
        content=ft.Column(
            [
                ft.Text(method_name, size=12, weight=ft.FontWeight.W_700, color=_C_BLUE),
                ft.Divider(height=6, color=_BORDER),
                *row_widgets,
            ],
            spacing=5,
            tight=True,
        ),
    )


_MAX_COMPARE_PANELS = 5


def _run_multi_panel_calculator(
    page,
    db,
    manager_id: str,
    calc_type: str,
    title: str,
    prefill,
    panel_factory,
    single_panel_width: int,
    back_fn=None,
    help_fn=None,
    reopen_fn=None,
):
    """Shared host for calculators that support side-by-side comparison
    panels (everything except Звичайний). Renders 1..5 panel_factory()
    instances in a horizontally-scrollable row so you can tweak just the
    1-2 fields that differ between scenarios and compare results at a
    glance — "+" duplicates the LAST panel's current INPUT VALUES (never
    its computed result; press "Розрахувати" again in the new panel) into
    a fresh one; "Зберегти" stores every visible panel's data as one
    {"panels": [...]} entry so reopening it recreates the whole comparison
    set at once, in the same order.

    panel_factory(prefill: dict, on_remove: Callable|None) -> (container,
    get_data): builds ONE independent panel (its own widgets/local state —
    calling panel_factory again produces a fully separate instance) and
    returns its root control plus a zero-arg callable returning that
    panel's current input values as a plain dict (never including computed
    results — those are recomputed fresh in whichever panel the user
    presses "Розрахувати" in). on_remove is a no-arg callback the panel may
    wire to its own small "×" (only meaningful/shown when there's more
    than one panel — the host calls this per-panel, see _add_panel below).

    prefill accepts either the new multi-panel shape ({"panels": [dict,
    ...]}) or a bare single-calculator dict (every save made before this
    feature existed) — both normalize to a list of per-panel prefills
    here, so old saved calculations keep opening exactly as before."""
    global _PANEL_H
    _t = i18n.bind(str(manager_id or ""))
    _ACTIVE_MANAGER_ID[0] = str(manager_id or "")
    # Kick off the FX-rate background refresh (see _fetch_uah_rates) the
    # instant the calculator opens, not on the first "Розрахувати" click —
    # so the cache is already warm by the time the user finishes typing.
    _fetch_uah_rates(page)

    # Same idea as the width fix below: _PANEL_H used to be a fixed 520px
    # regardless of window size, leaving a large dead strip under the
    # dialog on any reasonably tall window (e.g. rows of delivery methods
    # get their own internal scroll well before they need to). Every
    # _build_*_panel function reads this same module-level constant at
    # build time, so setting it once here — before any panel is actually
    # built below — makes every panel in THIS dialog use the current
    # window's real height instead.
    with contextlib.suppress(Exception):
        win_h = 0
        with contextlib.suppress(Exception):
            win_h = int(getattr(page, "height", None) or 0)
        if not win_h:
            win_h = int(page.window.height or 0)
        if win_h:
            # ~200px reserved for the dialog's own title bar + top/bottom
            # padding + the app's own header chrome above the dialog.
            _PANEL_H = max(400, win_h - 200)

    if (
        isinstance(prefill, dict)
        and isinstance(prefill.get("panels"), list)
        and prefill["panels"]
    ):
        prefill_list = [p for p in prefill["panels"] if isinstance(p, dict)] or [{}]
    else:
        prefill_list = [prefill if isinstance(prefill, dict) else {}]
    prefill_list = prefill_list[:_MAX_COMPARE_PANELS]

    panels: list[dict] = []
    panels_row = ft.Row(
        [], spacing=14, scroll=ft.ScrollMode.AUTO,
        vertical_alignment=ft.CrossAxisAlignment.START, tight=True,
    )
    row_holder = ft.Container(content=panels_row, clip_behavior=ft.ClipBehavior.HARD_EDGE)
    add_btn_ref: list = [None]
    # Created here (not further down, next to _close/_go_back/dlg.title as
    # before) — _resize_holder needs to set dlg.panel_width, and it already
    # runs once per panel during the prefill loop below, before those other
    # dlg-using closures are ever actually CALLED (only defined).
    dlg = _FloatDialog()

    def _available_width() -> int:
        # page.width mirrors the ACTUAL rendered viewport (matches how
        # dashboard.py's own _zoom_page_dims picks a width — page.width
        # first, page.window.width only as a fallback). page.window.width
        # is the raw OS window descriptor, which does not reliably reflect
        # a MAXIMIZED window's true size on this Flet version — using it as
        # primary is what left the dialog sized for a small/default window
        # even when the app was actually maximized full-screen.
        candidates = []
        with contextlib.suppress(Exception):
            candidates.append(int(getattr(page, "width", None) or 0))
        with contextlib.suppress(Exception):
            candidates.append(int(page.window.width or 0))
        candidates = [c for c in candidates if c > 0]
        return max(candidates) if candidates else 1280

    def _resize_holder():
        natural_w = len(panels) * single_panel_width + max(0, len(panels) - 1) * 14
        if _FloatDialog._is_fullscreen_build:
            # The fullscreen apps (standalone exe / web) wrap everything in
            # their own outer horizontal+vertical scroll now (see their
            # _open override) — row_holder can just take its natural full
            # width; an inner cap here would be redundant AND wrong, since
            # it'd be computed once from the CURRENT window size and go
            # stale the moment the window resizes or the page is zoomed
            # afterward, right back to the "3rd panel needs an inner
            # scrollbar to reach" problem this was meant to solve.
            w = natural_w
        else:
            # CatMan Pro's floating/draggable dialog has no outer scroll
            # wrapper — always fill the actual window width (minus a small
            # margin) rather than shrinking the dialog to exactly N whole
            # panels; trying to size the dialog to "precisely fit 2, not 3"
            # left a large dead gap between the (narrower) dialog and the
            # actual window edge whenever the true available width didn't
            # divide evenly by single panel width, with the next panel
            # visually peeking through instead of being cleanly clipped.
            # Anything that doesn't fit is reached via panels_row's own
            # horizontal scroll (ScrollMode.AUTO) instead. ~64px accounts
            # for the dialog's left offset (_FloatDialog._open) plus a
            # right-side margin so it never touches the window edge.
            w = min(natural_w, max(single_panel_width, _available_width() - 64))
        row_holder.width = w
        # Keep the WHOLE floating dialog (title bar included) locked to the
        # same width as its content — otherwise the title bar (which
        # stretches via expand=True with nothing of its own to constrain
        # it) can render wider than row_holder, leaving a visible gap on
        # the right with the next panel peeking through instead of being
        # cleanly clipped/scrolled away.
        dlg.panel_width = w
        with contextlib.suppress(Exception):
            # Only touch an already-open panel's own .width when it's the
            # real floating/draggable variant (positioned via left/top —
            # see _FloatDialog._open). The fullscreen apps reuse this same
            # class but never set left/top, since that panel is meant to
            # fill the whole window — forcing a fixed width on it there
            # would shrink it instead of leaving it fullscreen.
            if dlg._panel is not None and getattr(dlg._panel, "left", None) is not None:
                dlg._panel.width = w
                dlg._panel.update()

    def _update_add_btn():
        if add_btn_ref[0] is not None:
            add_btn_ref[0].disabled = len(panels) >= _MAX_COMPARE_PANELS
            with contextlib.suppress(Exception):
                add_btn_ref[0].update()

    def _remove_panel(entry: dict):
        if len(panels) <= 1:
            return
        with contextlib.suppress(ValueError):
            panels.remove(entry)
        panels_row.controls = [p["container"] for p in panels]
        _resize_holder()
        _update_add_btn()
        with contextlib.suppress(Exception):
            page.update()

    def _add_panel(pf: dict):
        if len(panels) >= _MAX_COMPARE_PANELS:
            return
        entry: dict = {}

        def _on_remove(_e=None, _entry=entry):
            _remove_panel(_entry)

        container, get_data = panel_factory(pf, _on_remove)
        entry["container"] = container
        entry["get_data"] = get_data
        panels.append(entry)
        panels_row.controls = [p["container"] for p in panels]
        _resize_holder()
        _update_add_btn()

    for pf in prefill_list:
        _add_panel(pf)

    def _on_add(_=None):
        if not panels or len(panels) >= _MAX_COMPARE_PANELS:
            return
        _add_panel(panels[-1]["get_data"]())
        with contextlib.suppress(Exception):
            page.update()

    def _close(_=None):
        with contextlib.suppress(Exception):
            _fdlg_close(page, dlg)

    async def _go_back(_=None):
        _close()
        if back_fn:
            await asyncio.sleep(0.05)
            with contextlib.suppress(Exception):
                back_fn()

    def _do_save(_=None):
        data = {"panels": [p["get_data"]() for p in panels]}
        _show_save_dialog(page, db, manager_id, calc_type, data)

    def _on_lang_select(code: str) -> None:
        # Switching language mid-entry shouldn't force closing the modal —
        # save the current panels' input values, close, and have the
        # calculator's own wrapper (_open_cost/_open_cost_clo/_open_profit/
        # _open_turnover_coverage) reopen itself: that rebuilds title/help
        # text with a freshly-bound _t in the new language, which a plain
        # recursive call into THIS function couldn't do (title/help_fn here
        # are already-resolved strings/closures frozen in the old language).
        i18n.save_manager_language(db, str(manager_id or ""), code)
        data = {"panels": [p["get_data"]() for p in panels]}
        _close()
        if reopen_fn:
            with contextlib.suppress(Exception):
                reopen_fn(data)

    add_btn_ref[0] = ft.IconButton(
        ft.Icons.ADD_BOX_OUTLINED,
        tooltip=_t("calc.compare.add_tooltip", "Додати панель для порівняння (до {n})").format(n=_MAX_COMPARE_PANELS),
        on_click=_on_add,
        icon_size=18,
    )
    _update_add_btn()

    dlg.title = ft.Row(
        [
            ft.Text(title, weight="bold", size=13, expand=True,
                    no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
            i18n.build_language_switcher(db, _on_lang_select, manager_id=str(manager_id or "")),
            *(
                [ft.IconButton(ft.Icons.HELP_OUTLINE, tooltip=_t("common.help.tooltip", "Довідка"),
                               on_click=help_fn, icon_size=18)]
                if help_fn else []
            ),
            add_btn_ref[0],
            ft.IconButton(ft.Icons.BOOKMARK_ADD_OUTLINED, tooltip=_t("common.save", "Зберегти"),
                          on_click=_do_save, icon_size=18),
            *(
                [ft.IconButton(ft.Icons.ARROW_BACK, tooltip=_t("common.back", "Назад"),
                               on_click=_go_back, icon_size=16)]
                if back_fn else []
            ),
            _close_or_refresh_btn(page, _close, icon_size=16),
        ]
    )
    dlg.content = ft.Container(
        padding=ft.padding.symmetric(horizontal=16, vertical=8),
        content=row_holder,
    )
    _fdlg_open(page, dlg)


def _build_cost_panel(
    page, db, manager_id: str, company_id: str, region_id: str,
    regions_list: list[str], prefill: dict | None, on_remove,
):
    """One independent Собівартість panel — inputs, its own result column,
    its own _calculate/_get_data closures. Called once per comparison
    panel by _open_cost's factory; each call has fully separate local
    state (method_widgets, tier_widgets, etc.), so N panels never share
    anything but the region/company/regions_list they were opened with."""
    _t = i18n.bind(str(manager_id or ""))
    _ACTIVE_MANAGER_ID[0] = str(manager_id or "")
    pf = prefill or {}

    cur_region = [str(pf.get("region_id") or region_id or "")]

    def _load_for_region(rid_name: str):
        methods: list[dict] = []
        price_tiers: list[dict] = []
        rid = _resolve_region_id(db, company_id, rid_name)
        try:
            methods = (
                db.get_effective_delivery_methods_for_manager(
                    company_id, manager_id, rid, include_user_disabled=True
                )
                or []
            )
        except Exception:
            try:
                methods = db.get_company_delivery_methods(company_id, rid) or []
            except Exception:
                pass
        # Region-configured methods used to come back with editable_name
        # unset/False — real preset name, tariff editable, but no way to
        # rename it or add a brand new one (dynamic, below, requires EVERY
        # method to be editable_name). Stamping it True here still seeds
        # each method's name/tariff from the real region config (mname
        # reads m["name"] either way — see the render loop) but now lets
        # the manager edit the name too, and enables "+ Додати спосіб
        # доставки" — matching the standalone/web calculators' flexibility
        # instead of being locked to whatever the region has configured.
        for m in methods:
            m["editable_name"] = True
        try:
            price_tiers = (
                db.get_manager_region_price_tiers_overrides(manager_id, rid) or []
            )
            if not price_tiers:
                price_tiers = db.get_region_price_tiers(rid) or []
        except Exception:
            pass
        return methods, price_tiers

    methods0, tiers0 = _load_for_region(cur_region[0])
    cur_uuid = [_resolve_region_id(db, company_id, cur_region[0])]
    dm_overrides_state = [
        db.get_manager_delivery_overrides(manager_id, region_id=cur_uuid[0]) or {}
    ]
    # No real per-company price tiers configured for this region (always
    # true in the standalone/portable app, which has no regions at all) —
    # render a growable manual list ("+" adds as many levels as needed)
    # instead of a "not configured" message + a single fixed manual field.
    tiers_dynamic = not bool(tiers0)

    def _all_ccys(methods, tiers) -> list[str]:
        s: set[str] = {"USD", "UAH", "EUR", "PLN", "CNY"}
        for m in methods:
            s.add(str(m.get("effective_price_currency") or m.get("price_currency") or "USD").upper())
        for t in tiers:
            s.add(str(t.get("currency") or "USD").upper())
        return sorted(s)

    ccy_opts0 = [(c, c) for c in _all_ccys(methods0, tiers0)]
    pf_buy_ccy  = str(pf.get("purchase_ccy") or "CNY")
    pf_pkg_ccy  = str(pf.get("pkg_ccy") or "CNY")
    pf_cost_ccy = str(pf.get("cost_ccy") or "USD")

    # ── Input widgets ──────────────────────────────────────────────────────────
    _CCY_W = 114   # currency dropdown width
    _PR_W  = _INP_W - _CCY_W - 4  # price field width = 274-114-4 = 156
    price_f    = _field(_t("calc.cost.purchase_price", "Ціна закупівлі *"),  width=_PR_W,  value=pf.get("purchase_price", ""))
    pkg_f      = _field(_t("calc.cost.pkg_price", "Ціна упаковки"),     width=_PR_W,  value=pf.get("pkg_price", ""), optional=True)
    weight_f   = _field(_t("calc.cost.weight", "Вага, кг *"),        width=_INP_W, value=pf.get("weight", ""))
    rent_f     = _field(_t("calc.field.rent_pct", "Рентабельність, %"), width=_INP_W, value=pf.get("rent_pct", ""), optional=True)
    buy_ccy_dd  = _dd(_t("calc.field.currency", "Валюта"),  ccy_opts0, value=pf_buy_ccy,  width=_CCY_W)
    pkg_ccy_dd  = _dd(_t("calc.field.currency", "Валюта"),  ccy_opts0, value=pf_pkg_ccy,  width=_CCY_W)
    cost_ccy_dd = _dd(_t("calc.cost.cost_currency", "Валюта С/В"), ccy_opts0, value=pf_cost_ccy, width=_INP_W)

    _TIER_CCYS = ("USD", "UAH", "EUR", "PLN", "CNY")
    custom_tier_name_f = _field(
        _t("calc.tier.custom_name", "Назва рівня"), width=_INP_W,
        value=pf.get("custom_tier_name", ""), number=False, optional=True,
    )
    custom_tier_price_f = _field(
        _t("calc.tier.custom_price", "Ціна рівня"), width=_PR_W,
        value=pf.get("custom_tier_price", ""), optional=True,
    )
    custom_tier_ccy_dd = _dd(
        _t("calc.field.currency", "Валюта"), [(c, c) for c in _TIER_CCYS],
        value=str(pf.get("custom_tier_ccy") or "USD"), width=_CCY_W,
    )

    # ── Dynamic sections ───────────────────────────────────────────────────────
    methods_col = ft.Column([], spacing=6, tight=True)
    tiers_col   = ft.Column([], spacing=4, tight=True)
    method_widgets: dict[str, dict] = {}
    tier_widgets:   dict[str, dict] = {}
    manual_tiers_col = ft.Column([], spacing=4, tight=True)
    manual_tier_widgets: dict[str, dict] = {}
    _manual_tiers_next_id = [1]
    if tiers_dynamic:
        _mt_seed = pf.get("manual_tiers")
        if not _mt_seed and pf.get("custom_tier_name"):
            # Backward compat: a saved calc from before this dynamic list
            # existed only had one singular custom_tier_name/price/ccy.
            _mt_seed = [{
                "name": pf.get("custom_tier_name"), "price": pf.get("custom_tier_price"),
                "ccy": pf.get("custom_tier_ccy"),
            }]
        manual_tiers_state: list[dict] = [
            {"id": t.get("id") or f"manual_tier_{i + 1}", "name": t.get("name", ""),
             "price": t.get("price", ""), "ccy": t.get("ccy") or "USD"}
            for i, t in enumerate(_mt_seed or [])
        ]
        _manual_tiers_next_id[0] = len(manual_tiers_state) + 1
    else:
        manual_tiers_state = []

    def _rebuild_methods(methods: list[dict], pf_methods: dict, dm_overrides: dict | None = None):
        method_widgets.clear()
        rows = []
        # Every method carries editable_name=True now (region-configured
        # ones are stamped True in _load_for_region, seeded with the
        # region's real name/tariff; standalone/web ones start blank) — so
        # this always renders a growable list (rename in place, "+" for as
        # many as needed) rather than a fixed, uneditable preset.
        dynamic = bool(methods) and all(bool(x.get("editable_name")) for x in methods)
        if not methods:
            rows.append(ft.Text(_t("calc.cost.no_delivery_methods", "Типи доставки не налаштовані для цього регіону"),
                                size=11, color=_MUTED, italic=True))
        for idx, m in enumerate(methods):
            mid   = str(m.get("id") or "")
            # editable_name (standalone portable app only, no real company
            # delivery methods configured) — the manager types their own
            # delivery name instead of picking from a fixed preset list.
            editable_name = bool(m.get("editable_name"))
            mname = str(m.get("name") or "").strip() if editable_name else str(m.get("name") or mid)
            eff   = m.get("effective_price_per_kg") or m.get("base_price_per_kg") or 0
            mccy  = str(m.get("effective_price_currency") or m.get("price_currency") or "USD").upper()
            pf_m  = (pf_methods or {}).get(mid) or {}
            # Deliberately ignores dm_overrides / m["user_enabled"] (the
            # manager's general "which delivery methods do I use" setting,
            # shared with Orders) — the calculator is a scratch/what-if tool,
            # so it always starts with nothing pre-checked unless restoring
            # a previously SAVED calculation, never a general app setting.
            cb_val = bool(pf_m.get("checked", False))

            def _on_cb(ev, method_id=mid):
                cur_ov = dm_overrides_state[0]
                if method_id not in cur_ov or not isinstance(cur_ov[method_id], dict):
                    cur_ov[method_id] = {}
                cur_ov[method_id]["enabled"] = bool(ev.control.value)
                db.save_manager_delivery_overrides(manager_id, cur_ov, region_id=cur_uuid[0])

            tariff_f = ft.TextField(
                label=_t("calc.cost.tariff_per_kg", "тариф/кг"),
                value=str(pf_m.get("tariff") or eff or ""),
                width=82, border_radius=6,
                keyboard_type=ft.KeyboardType.NUMBER, dense=True,
                content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
                label_style=ft.TextStyle(size=10),
                text_style=ft.TextStyle(size=12),
            )
            # Two rows per method (name up top, checkbox+tariff+currency
            # below) instead of cramming everything into one row — at the
            # narrower -15% widths, one row didn't leave enough room for
            # longer delivery names before they got clipped.
            if editable_name:
                cb = ft.Checkbox(value=cb_val, on_change=_on_cb)
                name_f = ft.TextField(
                    value=str(pf_m.get("name") or mname or ""),
                    hint_text=_t("calc.cost.delivery_name_hint", "Назва доставки"),
                    dense=True, border_radius=6, expand=True,
                    content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
                    text_style=ft.TextStyle(size=12),
                )
                method_widgets[mid] = {"name": mname or _t("calc.cost.delivery_generic_name", "Доставка {n}").format(n=idx + 1),
                                       "name_f": name_f, "ccy": mccy, "cb": cb, "tariff_f": tariff_f}
                top_row = ft.Row([name_f], spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER)
            else:
                cb = ft.Checkbox(value=cb_val, on_change=_on_cb)
                method_widgets[mid] = {"name": mname, "name_f": None, "ccy": mccy, "cb": cb, "tariff_f": tariff_f}
                top_row = ft.Row(
                    [ft.Text(mname, size=11, color=_DARK, expand=True)],
                    spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
            bottom_controls = [
                cb, tariff_f,
                ft.Text(mccy, size=9, color=_MUTED, width=28),
            ]
            if dynamic and len(methods) > 1:
                bottom_controls.append(ft.IconButton(
                    ft.Icons.CLOSE, icon_size=14, icon_color=_MUTED,
                    tooltip=_t("common.delete", "Видалити"),
                    on_click=lambda _, method_id=mid: _remove_method_slot(method_id),
                ))
            rows.append(ft.Column(
                [
                    top_row,
                    ft.Row(bottom_controls, spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ],
                spacing=2, tight=True,
            ))
        if dynamic:
            rows.append(ft.TextButton(
                "+ " + _t("calc.cost.add_delivery_method", "Додати спосіб доставки"),
                icon=ft.Icons.ADD, on_click=_add_method_slot,
                style=ft.ButtonStyle(padding=0),
            ))
        methods_col.controls = rows

    def _snapshot_methods_pf() -> dict:
        return {
            mid: {
                "checked": w["cb"].value, "tariff": w["tariff_f"].value,
                **({"name": w["name_f"].value} if w.get("name_f") else {}),
            }
            for mid, w in method_widgets.items()
        }

    def _add_method_slot(_=None):
        snap = _snapshot_methods_pf()
        n = len(methods0) + 1
        methods0.append({
            "id": f"custom_method_{n}", "name": "", "effective_price_per_kg": 0,
            "effective_price_currency": "USD", "editable_name": True,
        })
        _rebuild_methods(methods0, snap, dm_overrides_state[0])
        with contextlib.suppress(Exception):
            page.update()

    def _remove_method_slot(mid: str):
        snap = _snapshot_methods_pf()
        snap.pop(mid, None)
        methods0[:] = [m for m in methods0 if str(m.get("id") or "") != mid]
        _rebuild_methods(methods0, snap, dm_overrides_state[0])
        with contextlib.suppress(Exception):
            page.update()

    def _rebuild_tiers(tiers: list[dict], pf_tiers: dict):
        tier_widgets.clear()
        rows = []
        if not tiers:
            rows.append(ft.Text(_t("calc.cost.no_price_tiers", "Ціни продажу не налаштовані для цього регіону"),
                                size=11, color=_MUTED, italic=True))
        for t in tiers:
            tid  = str(t.get("id") or "")
            tnm  = str(t.get("name") or tid)
            tccy = str(t.get("currency") or "USD").upper()
            tf   = ft.TextField(
                value=str((pf_tiers or {}).get(tid) or ""),
                width=96, border_radius=6,
                keyboard_type=ft.KeyboardType.NUMBER, dense=True,
                content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
                text_style=ft.TextStyle(size=12),
            )
            tier_widgets[tid] = {"name": tnm, "ccy": tccy, "field": tf}
            rows.append(ft.Row(
                [ft.Text(f"{tnm} ({tccy})", size=10, expand=True,
                         overflow=ft.TextOverflow.ELLIPSIS), tf],
                spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ))
        tiers_col.controls = rows

    def _sync_manual_tiers_from_widgets():
        for t in manual_tiers_state:
            w = manual_tier_widgets.get(t["id"])
            if w:
                t["name"] = w["name_f"].value
                t["price"] = w["price_f"].value
                t["ccy"] = w["ccy_dd"].value

    def _rebuild_manual_tiers():
        manual_tier_widgets.clear()
        rows = []
        for t in manual_tiers_state:
            tid = t["id"]
            name_f = ft.TextField(
                value=str(t.get("name") or ""),
                hint_text=_t("calc.tier.custom_name", "Назва рівня"),
                dense=True, border_radius=6, expand=True,
                content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
                text_style=ft.TextStyle(size=12),
            )
            price_f = ft.TextField(
                value=str(t.get("price") or ""), width=90, border_radius=6, dense=True,
                keyboard_type=ft.KeyboardType.NUMBER,
                content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
                text_style=ft.TextStyle(size=12),
            )
            ccy_dd = _dd("", [(c, c) for c in _TIER_CCYS], value=str(t.get("ccy") or "USD"), width=_CCY_W)
            manual_tier_widgets[tid] = {"name_f": name_f, "price_f": price_f, "ccy_dd": ccy_dd}
            rows.append(ft.Container(
                padding=ft.padding.only(bottom=4),
                border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                name_f,
                                ft.IconButton(
                                    ft.Icons.CLOSE, icon_size=14, icon_color=_MUTED,
                                    tooltip=_t("common.delete", "Видалити"),
                                    on_click=lambda _, tid=tid: _remove_manual_tier(tid),
                                ),
                            ],
                            spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Row([price_f, ccy_dd], spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ],
                    spacing=4, tight=True,
                ),
            ))
        rows.append(ft.TextButton(
            "+ " + _t("calc.tier.add_level", "Додати рівень ціни"),
            icon=ft.Icons.ADD, on_click=_add_manual_tier,
            style=ft.ButtonStyle(padding=0),
        ))
        manual_tiers_col.controls = rows

    def _add_manual_tier(_=None):
        _sync_manual_tiers_from_widgets()
        n = _manual_tiers_next_id[0]
        _manual_tiers_next_id[0] += 1
        manual_tiers_state.append({"id": f"manual_tier_{n}", "name": "", "price": "", "ccy": "USD"})
        _rebuild_manual_tiers()
        with contextlib.suppress(Exception):
            page.update()

    def _remove_manual_tier(tid: str):
        _sync_manual_tiers_from_widgets()
        manual_tiers_state[:] = [t for t in manual_tiers_state if t["id"] != tid]
        _rebuild_manual_tiers()
        with contextlib.suppress(Exception):
            page.update()

    if tiers_dynamic:
        _rebuild_manual_tiers()

    # Restoring a saved calculation: the standalone/editable-name delivery
    # list always starts from a single blank slot (see
    # _StandaloneDB._default_delivery_methods), but the saved calc may have
    # 2+ user-added ones — without re-creating a slot for each saved id
    # here, _rebuild_methods below only had "method_1" to iterate over and
    # silently dropped every other saved delivery method on reopen (looked
    # like they'd been deleted, even though _get_data had saved them fine).
    _pf_methods_seed = pf.get("methods") or {}
    if methods0 and all(bool(m.get("editable_name")) for m in methods0):
        _existing_method_ids = {str(m.get("id") or "") for m in methods0}
        for _extra_id in _pf_methods_seed:
            if _extra_id not in _existing_method_ids:
                methods0.append({
                    "id": _extra_id, "name": "", "effective_price_per_kg": 0,
                    "effective_price_currency": "USD", "editable_name": True,
                })

    _rebuild_methods(methods0, pf.get("methods") or {}, dm_overrides_state[0])
    _rebuild_tiers(tiers0, pf.get("tiers") or {})

    # ── Region section ─────────────────────────────────────────────────────────
    region_section: list = []
    if len(regions_list) > 1:
        def _on_region_change(e):
            rid_name = str(e.control.value or "").strip()
            if not rid_name:
                return
            try:
                cur_region[0] = rid_name
                cur_uuid[0]   = _resolve_region_id(db, company_id, rid_name)
                dm_overrides_state[0] = db.get_manager_delivery_overrides(manager_id, region_id=cur_uuid[0]) or {}
                new_m, new_t = _load_for_region(rid_name)
                new_ccys = _all_ccys(new_m, new_t)
                new_opts = [ft.dropdown.Option(key="", text=_t("calc.field.choose_placeholder", "— оберіть —"))] + [
                    ft.dropdown.Option(key=c, text=c) for c in new_ccys
                ]
                for dd in (buy_ccy_dd, pkg_ccy_dd, cost_ccy_dd):
                    cur_v = dd.value
                    dd.options = new_opts
                    if cur_v not in new_ccys:
                        dd.value = new_ccys[0] if new_ccys else None
                _rebuild_methods(new_m, {}, dm_overrides_state[0])
                _rebuild_tiers(new_t, {})
                result_col.controls = []
            except Exception:
                logger.exception("Region switch failed in cost calculator (region=%s)", rid_name)
                result_col.controls = [
                    ft.Container(
                        bgcolor="#FEE2E2", border=ft.border.all(1, "#DC2626"),
                        border_radius=8, padding=ft.padding.all(12),
                        content=ft.Text(
                            _t("calc.err.region_switch", "⚠ Помилка перемикання регіону: {region}").format(region=rid_name),
                            color=_ERR_FG, size=12,
                        ),
                    )
                ]
            with contextlib.suppress(Exception):
                page.update()

        region_dd = _dd(_t("calc.field.region", "Регіон"), [(r, r) for r in regions_list],
                        value=cur_region[0], width=_INP_W)
        region_dd.on_select = _on_region_change
        region_section = [_section(_t("calc.field.region", "Регіон")), region_dd]

    # ── Inputs column ──────────────────────────────────────────────────────────
    inputs_col = ft.Column(
        [
            *region_section,
            _section(_t("calc.cost.section.purchase", "Закупівля")),
            ft.Row([price_f, buy_ccy_dd], spacing=4),
            ft.Row([pkg_f,   pkg_ccy_dd], spacing=4),
            weight_f,
            _section(_t("calc.cost.section.cost_currency", "Валюта собівартості")),
            cost_ccy_dd,
            _section(_t("calc.cost.section.delivery_methods", "Типи доставки  (✓ — обрати, тариф — редагується)")),
            ft.Container(content=methods_col),
            _section(_t("calc.field.rent_pct_optional", "Рентабельність (необов'яз.)")),
            rent_f,
            _section(_t("calc.cost.section.price_tiers", "Ціни продажу (необов'яз.)")),
            *(
                [ft.Container(content=manual_tiers_col)]
                if tiers_dynamic else
                [
                    ft.Container(content=tiers_col),
                    _section(_t("calc.tier.custom_section", "Додатковий рівень ціни (вручну)")),
                    custom_tier_name_f,
                    ft.Row([custom_tier_price_f, custom_tier_ccy_dd], spacing=4),
                ]
            ),
        ],
        spacing=4,
        scroll=ft.ScrollMode.AUTO,
        width=_INP_W,
    )

    # ── Results column ─────────────────────────────────────────────────────────
    result_col = ft.Column(
        [],
        spacing=8,
        scroll=ft.ScrollMode.AUTO,
        width=_RES_W,
    )

    # ── Calculate ─────────────────────────────────────────────────────────────
    def _calculate(_=None):
        rates = _fetch_uah_rates(page)
        purchase_price = _safe_float(price_f.value)
        pkg_price      = _safe_float(pkg_f.value) or 0.0
        weight         = _safe_float(weight_f.value)
        rent_pct       = _safe_float(rent_f.value)
        buy_ccy  = str(buy_ccy_dd.value  or "USD").upper()
        pkg_ccy  = str(pkg_ccy_dd.value  or "USD").upper()
        cost_ccy = str(cost_ccy_dd.value or "USD").upper()

        def _err(msg: str):
            result_col.controls = [
                ft.Container(
                    bgcolor="#FEF3C7", border=ft.border.all(1, "#F59E0B"),
                    border_radius=8, padding=ft.padding.all(12),
                    content=ft.Text(f"⚠  {msg}", color=_C_WARN, size=12),
                )
            ]
            with contextlib.suppress(Exception):
                page.update()

        if purchase_price is None:
            _err(_t("calc.err.enter_purchase_price", "Введіть ціну закупівлі"))
            return
        if weight is None:
            _err(_t("calc.err.enter_weight", "Введіть вагу"))
            return

        selected = [(mid, w) for mid, w in method_widgets.items() if w["cb"].value]
        if not selected:
            _err(_t("calc.err.choose_delivery_method", "Оберіть хоча б один тип доставки"))
            return

        buy_in_cv = _convert(purchase_price, buy_ccy, cost_ccy, rates)
        if buy_in_cv is None:
            detail = (
                f" ({'; '.join(_FX_LAST_ERRORS)})" if _FX_LAST_ERRORS
                else " " + _t("calc.err.rates_not_loaded", "(курси ще не завантажені — спробуйте ще раз за кілька секунд)")
            )
            _err(_t("calc.err.no_rate", "Немає курсу {a} → {b}").format(a=buy_ccy, b=cost_ccy) + detail)
            return
        pkg_in_cv  = (_convert(pkg_price, pkg_ccy, cost_ccy, rates) or 0.0) if pkg_price else 0.0
        base_cost  = buy_in_cv + pkg_in_cv
        has_rent   = rent_pct is not None and 0 < rent_pct < 100

        tier_vals: list[tuple[str, str, float]] = []
        for tid, tw in tier_widgets.items():
            v = _safe_float(tw["field"].value)
            if v is not None and v > 0:
                tier_vals.append((tw["name"], tw["ccy"], v))
        if tiers_dynamic:
            for w in manual_tier_widgets.values():
                nm = str(w["name_f"].value or "").strip()
                pr = _safe_float(w["price_f"].value)
                if nm and pr is not None and pr > 0:
                    tier_vals.append((nm, str(w["ccy_dd"].value or "USD").upper(), pr))
        else:
            custom_tier_name = str(custom_tier_name_f.value or "").strip()
            custom_tier_price = _safe_float(custom_tier_price_f.value)
            if custom_tier_name and custom_tier_price is not None and custom_tier_price > 0:
                tier_vals.append((custom_tier_name, str(custom_tier_ccy_dd.value or "USD").upper(), custom_tier_price))

        extra_ccys: set[str] = {buy_ccy, pkg_ccy}
        cards = []

        for mid, mw in selected:
            tariff      = _safe_float(mw["tariff_f"].value) or 0.0
            mccy        = mw["ccy"]
            mw_name     = (str(mw["name_f"].value or "").strip() if mw.get("name_f") else "") or mw["name"]
            delivery_cv = _convert(weight * tariff, mccy, cost_ccy, rates) or (weight * tariff)
            extra_ccys.add(mccy)
            cv = base_cost + delivery_cv

            # Each row's pct (5th tuple element) is its share of the final
            # С/В (cv, the card's own 100% baseline) — None on rows that
            # shouldn't show one (the total itself, "Ціна при рент.", tier
            # margin rows). See _cost_card's docstring.
            pct_of = (lambda amt: amt / cv * 100) if cv > 0 else (lambda amt: None)

            card_rows: list[tuple[str, str, str, bool, float | None]] = [
                (_t("calc.result.purchase_price", "Ціна закупівлі:"), _fmt_money(buy_in_cv, cost_ccy), _DARK, False, pct_of(buy_in_cv)),
            ]
            if pkg_in_cv:
                card_rows.append((_t("calc.result.packaging", "Упаковка:"), _fmt_money(pkg_in_cv, cost_ccy), _MUTED, False, pct_of(pkg_in_cv)))
            card_rows.append(
                (_t("calc.result.delivery_line", "Доставка ({weight} кг × {tariff} {ccy}/кг):").format(weight=weight, tariff=tariff, ccy=mccy),
                 _fmt_money(delivery_cv, cost_ccy), _MUTED, False, pct_of(delivery_cv))
            )
            card_rows.append((_t("calc.result.cost", "Собівартість:"), _fmt_money(cv, cost_ccy), _C_OK, True, None))

            if has_rent:
                sale = cv / (1.0 - rent_pct / 100.0)
                card_rows.append(
                    (_t("calc.result.price_at_rent", "Ціна при рент. {pct:.0f}%:").format(pct=rent_pct), _fmt_money(sale, cost_ccy), _C_BLUE, True, None)
                )

            for tnm, tccy, tprice in tier_vals:
                tprice_cv = _convert(tprice, tccy, cost_ccy, rates) or tprice
                r_pct = (tprice_cv - cv) / tprice_cv * 100 if tprice_cv > 0 else 0.0
                clr = _C_OK if r_pct >= 20 else (_C_WARN if r_pct >= 0 else _C_ERR)
                price_label = (
                    f"{_fmt_money(tprice, tccy)} - {_fmt_money(tprice_cv, cost_ccy)}"
                    if tccy != cost_ccy else _fmt_money(tprice_cv, cost_ccy)
                )
                card_rows.append(
                    (f"{tnm} ({price_label}):",
                     f"{r_pct:.1f}%", clr, False, None)
                )

            cards.append(_cost_card(mw_name, card_rows, highlight=True))

        # Rates line
        rate_parts = _rate_texts(buy_ccy, cost_ccy, extra_ccys, rates)
        rate_str   = "  |  ".join(t.value for t in rate_parts) if rate_parts else ""

        result_col.controls = [
            ft.Container(
                bgcolor=_C_BG,
                border_radius=8,
                padding=ft.padding.symmetric(horizontal=12, vertical=8),
                content=ft.Column(
                    [
                        ft.Text(_t("calc.results_title", "Результати розрахунку"), size=12,
                                weight=ft.FontWeight.W_700, color=_DARK),
                        ft.Text(
                            _t("calc.cost.formula", "С/В = Ціна закупівлі + Упаковка + (Вага × Тариф доставки)"),
                            size=9, color=_MUTED, italic=True,
                        ),
                        *(
                            [ft.Text(rate_str, size=9, color=_MUTED)]
                            if rate_str else []
                        ),
                    ],
                    spacing=2, tight=True,
                ),
            ),
            *cards,
        ]
        with contextlib.suppress(Exception):
            page.update()

    def _get_data() -> dict:
        return {
            "region_id":    cur_region[0],
            "purchase_price": price_f.value,
            "purchase_ccy": buy_ccy_dd.value,
            "pkg_price":    pkg_f.value,
            "pkg_ccy":      pkg_ccy_dd.value,
            "cost_ccy":     cost_ccy_dd.value,
            "weight":       weight_f.value,
            "rent_pct":     rent_f.value,
            "methods": {
                mid: {
                    "checked": w["cb"].value, "tariff": w["tariff_f"].value,
                    **({"name": w["name_f"].value} if w.get("name_f") else {}),
                }
                for mid, w in method_widgets.items()
            },
            "tiers": {tid: tw["field"].value for tid, tw in tier_widgets.items()},
            **(
                {
                    "manual_tiers": [
                        {"id": tid, "name": w["name_f"].value, "price": w["price_f"].value, "ccy": w["ccy_dd"].value}
                        for tid, w in manual_tier_widgets.items()
                    ],
                }
                if tiers_dynamic else
                {
                    "custom_tier_name": custom_tier_name_f.value,
                    "custom_tier_price": custom_tier_price_f.value,
                    "custom_tier_ccy": custom_tier_ccy_dd.value,
                }
            ),
        }

    panel_body = ft.Row(
        [
            ft.Column(
                [
                    ft.Container(
                        width=_LEFT_W,
                        content=inputs_col,
                    ),
                    ft.FilledButton(
                        "  " + _t("calc.action.calculate", "Розрахувати"),
                        icon=ft.Icons.CALCULATE,
                        on_click=_calculate,
                        width=_LEFT_W,
                        style=ft.ButtonStyle(
                            shape=ft.RoundedRectangleBorder(radius=8),
                        ),
                    ),
                ],
                spacing=6,
                tight=True,
            ),
            ft.VerticalDivider(width=1, color=_BORDER),
            ft.Container(
                width=_RIGHT_W,
                content=result_col,
            ),
        ],
        spacing=10,
        vertical_alignment=ft.CrossAxisAlignment.START,
    )
    container = ft.Container(
        border=ft.border.all(1, _BORDER),
        border_radius=10,
        padding=8,
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Container(expand=True),
                        ft.IconButton(
                            ft.Icons.CLOSE, icon_size=14, tooltip=_t("calc.compare.remove_panel", "Прибрати панель"),
                            on_click=on_remove,
                        ),
                    ],
                    tight=True,
                ),
                panel_body,
            ],
            spacing=0, tight=True,
        ),
    )
    return container, _get_data


def _open_cost(
    page, db, manager_id: str, company_id: str, region_id: str,
    regions_list: list[str], prefill: dict | None = None, back_fn=None,
):
    _t = i18n.bind(str(manager_id or ""))
    _ACTIVE_MANAGER_ID[0] = str(manager_id or "")

    def _factory(pf: dict, on_remove):
        return _build_cost_panel(
            page, db, manager_id, company_id, region_id, regions_list, pf, on_remove
        )

    def _reopen(data: dict):
        _open_cost(page, db, manager_id, company_id, region_id, regions_list, prefill=data, back_fn=back_fn)

    _run_multi_panel_calculator(
        page, db, manager_id, "cost", _t("calc.cost.title", "Собівартість"), prefill, _factory,
        single_panel_width=_LEFT_W + 21 + _RIGHT_W + 32,
        back_fn=back_fn,
        reopen_fn=_reopen,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ── 2b. Собівартість з митом ──────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
def _build_cost_clo_panel(
    page, db, manager_id: str, company_id: str, region_id: str,
    regions_list: list[str], prefill: dict | None, on_remove,
):
    """One independent Собівартість+мито panel (see _build_cost_panel for
    the multi-panel comparison mechanics — this mirrors it exactly plus
    two extra manually-entered fields — Ставка мита (%) and Коефіцієнт
    коригування фрахту — for routes where duty accrues only on the
    outside-EU leg of a combined route (e.g. part of the trip is outside
    the EU, part is already within it). Мито = ставка% × (С/В без мита ÷
    коефіцієнт); the коефіцієнт (e.g. 1,2 for an 80/20 outside-EU/EU split)
    already encodes "keep only the outside-EU share" — see the on-screen
    help for the worked example this was specified against (9.7% rate,
    invoice 2 + logistics 1, 80% outside EU → мито = 9.7% × (3 ÷ 1.2) =
    0.2425). Deliberately generic (no hardcoded country names) so the tool
    applies to any route split, not just one specific origin/destination.

    Internal identifiers (function name, "cost_clo" dispatch/calc_type key,
    "clo_rate"/"clo_coef" data-dict keys) were kept as-is when the on-screen
    labels were renamed from CLO to мито/коригування фрахту — cosmetic
    rename only, no need to migrate already-saved calculations."""
    _t = i18n.bind(str(manager_id or ""))
    _ACTIVE_MANAGER_ID[0] = str(manager_id or "")
    pf = prefill or {}

    cur_region = [str(pf.get("region_id") or region_id or "")]

    def _load_for_region(rid_name: str):
        methods: list[dict] = []
        price_tiers: list[dict] = []
        rid = _resolve_region_id(db, company_id, rid_name)
        try:
            methods = (
                db.get_effective_delivery_methods_for_manager(
                    company_id, manager_id, rid, include_user_disabled=True
                )
                or []
            )
        except Exception:
            try:
                methods = db.get_company_delivery_methods(company_id, rid) or []
            except Exception:
                pass
        # Region-configured methods used to come back with editable_name
        # unset/False — real preset name, tariff editable, but no way to
        # rename it or add a brand new one (dynamic, below, requires EVERY
        # method to be editable_name). Stamping it True here still seeds
        # each method's name/tariff from the real region config (mname
        # reads m["name"] either way — see the render loop) but now lets
        # the manager edit the name too, and enables "+ Додати спосіб
        # доставки" — matching the standalone/web calculators' flexibility
        # instead of being locked to whatever the region has configured.
        for m in methods:
            m["editable_name"] = True
        try:
            price_tiers = (
                db.get_manager_region_price_tiers_overrides(manager_id, rid) or []
            )
            if not price_tiers:
                price_tiers = db.get_region_price_tiers(rid) or []
        except Exception:
            pass
        return methods, price_tiers

    methods0, tiers0 = _load_for_region(cur_region[0])
    cur_uuid = [_resolve_region_id(db, company_id, cur_region[0])]
    dm_overrides_state = [
        db.get_manager_delivery_overrides(manager_id, region_id=cur_uuid[0]) or {}
    ]
    # No real per-company price tiers configured for this region (always
    # true in the standalone/portable app, which has no regions at all) —
    # render a growable manual list ("+" adds as many levels as needed)
    # instead of a "not configured" message + a single fixed manual field.
    tiers_dynamic = not bool(tiers0)

    def _all_ccys(methods, tiers) -> list[str]:
        s: set[str] = {"USD", "UAH", "EUR", "PLN", "CNY"}
        for m in methods:
            s.add(str(m.get("effective_price_currency") or m.get("price_currency") or "USD").upper())
        for t in tiers:
            s.add(str(t.get("currency") or "USD").upper())
        return sorted(s)

    ccy_opts0 = [(c, c) for c in _all_ccys(methods0, tiers0)]
    pf_buy_ccy  = str(pf.get("purchase_ccy") or "CNY")
    pf_pkg_ccy  = str(pf.get("pkg_ccy") or "CNY")
    pf_cost_ccy = str(pf.get("cost_ccy") or "USD")

    # ── Input widgets ──────────────────────────────────────────────────────────
    _CCY_W = 114   # currency dropdown width
    _PR_W  = _INP_W - _CCY_W - 4  # price field width = 274-114-4 = 156
    price_f    = _field(_t("calc.cost.purchase_price", "Ціна закупівлі *"),  width=_PR_W,  value=pf.get("purchase_price", ""))
    pkg_f      = _field(_t("calc.cost.pkg_price", "Ціна упаковки"),     width=_PR_W,  value=pf.get("pkg_price", ""), optional=True)
    weight_f   = _field(_t("calc.cost.weight", "Вага, кг *"),        width=_INP_W, value=pf.get("weight", ""))
    rent_f     = _field(_t("calc.field.rent_pct", "Рентабельність, %"), width=_INP_W, value=pf.get("rent_pct", ""), optional=True)
    clo_rate_f = _field(_t("calc.cost_clo.duty_rate", "Ставка мита, % *"),   width=_INP_W, value=pf.get("clo_rate", ""))
    clo_coef_f = _field(_t("calc.cost_clo.freight_coef", "Коеф. для нарахування мита (CLA) *"),  width=_INP_W, value=pf.get("clo_coef", ""))
    buy_ccy_dd  = _dd(_t("calc.field.currency", "Валюта"),  ccy_opts0, value=pf_buy_ccy,  width=_CCY_W)
    pkg_ccy_dd  = _dd(_t("calc.field.currency", "Валюта"),  ccy_opts0, value=pf_pkg_ccy,  width=_CCY_W)
    cost_ccy_dd = _dd(_t("calc.cost.cost_currency", "Валюта С/В"), ccy_opts0, value=pf_cost_ccy, width=_INP_W)

    _TIER_CCYS = ("USD", "UAH", "EUR", "PLN", "CNY")
    custom_tier_name_f = _field(
        _t("calc.tier.custom_name", "Назва рівня"), width=_INP_W,
        value=pf.get("custom_tier_name", ""), number=False, optional=True,
    )
    custom_tier_price_f = _field(
        _t("calc.tier.custom_price", "Ціна рівня"), width=_PR_W,
        value=pf.get("custom_tier_price", ""), optional=True,
    )
    custom_tier_ccy_dd = _dd(
        _t("calc.field.currency", "Валюта"), [(c, c) for c in _TIER_CCYS],
        value=str(pf.get("custom_tier_ccy") or "USD"), width=_CCY_W,
    )

    # ── Dynamic sections ───────────────────────────────────────────────────────
    methods_col = ft.Column([], spacing=6, tight=True)
    tiers_col   = ft.Column([], spacing=4, tight=True)
    method_widgets: dict[str, dict] = {}
    tier_widgets:   dict[str, dict] = {}
    manual_tiers_col = ft.Column([], spacing=4, tight=True)
    manual_tier_widgets: dict[str, dict] = {}
    _manual_tiers_next_id = [1]
    if tiers_dynamic:
        _mt_seed = pf.get("manual_tiers")
        if not _mt_seed and pf.get("custom_tier_name"):
            # Backward compat: a saved calc from before this dynamic list
            # existed only had one singular custom_tier_name/price/ccy.
            _mt_seed = [{
                "name": pf.get("custom_tier_name"), "price": pf.get("custom_tier_price"),
                "ccy": pf.get("custom_tier_ccy"),
            }]
        manual_tiers_state: list[dict] = [
            {"id": t.get("id") or f"manual_tier_{i + 1}", "name": t.get("name", ""),
             "price": t.get("price", ""), "ccy": t.get("ccy") or "USD"}
            for i, t in enumerate(_mt_seed or [])
        ]
        _manual_tiers_next_id[0] = len(manual_tiers_state) + 1
    else:
        manual_tiers_state = []

    def _rebuild_methods(methods: list[dict], pf_methods: dict, dm_overrides: dict | None = None):
        method_widgets.clear()
        rows = []
        # Every method carries editable_name=True now (region-configured
        # ones are stamped True in _load_for_region, seeded with the
        # region's real name/tariff; standalone/web ones start blank) — so
        # this always renders a growable list (rename in place, "+" for as
        # many as needed) rather than a fixed, uneditable preset.
        dynamic = bool(methods) and all(bool(x.get("editable_name")) for x in methods)
        if not methods:
            rows.append(ft.Text(_t("calc.cost.no_delivery_methods", "Типи доставки не налаштовані для цього регіону"),
                                size=11, color=_MUTED, italic=True))
        for idx, m in enumerate(methods):
            mid   = str(m.get("id") or "")
            # editable_name (standalone portable app only, no real company
            # delivery methods configured) — the manager types their own
            # delivery name instead of picking from a fixed preset list.
            editable_name = bool(m.get("editable_name"))
            mname = str(m.get("name") or "").strip() if editable_name else str(m.get("name") or mid)
            eff   = m.get("effective_price_per_kg") or m.get("base_price_per_kg") or 0
            mccy  = str(m.get("effective_price_currency") or m.get("price_currency") or "USD").upper()
            pf_m  = (pf_methods or {}).get(mid) or {}
            # Deliberately ignores dm_overrides / m["user_enabled"] (the
            # manager's general "which delivery methods do I use" setting,
            # shared with Orders) — the calculator is a scratch/what-if tool,
            # so it always starts with nothing pre-checked unless restoring
            # a previously SAVED calculation, never a general app setting.
            cb_val = bool(pf_m.get("checked", False))

            def _on_cb(ev, method_id=mid):
                cur_ov = dm_overrides_state[0]
                if method_id not in cur_ov or not isinstance(cur_ov[method_id], dict):
                    cur_ov[method_id] = {}
                cur_ov[method_id]["enabled"] = bool(ev.control.value)
                db.save_manager_delivery_overrides(manager_id, cur_ov, region_id=cur_uuid[0])

            tariff_f = ft.TextField(
                label=_t("calc.cost.tariff_per_kg", "тариф/кг"),
                value=str(pf_m.get("tariff") or eff or ""),
                width=82, border_radius=6,
                keyboard_type=ft.KeyboardType.NUMBER, dense=True,
                content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
                label_style=ft.TextStyle(size=10),
                text_style=ft.TextStyle(size=12),
            )
            # Two rows per method (name up top, checkbox+tariff+currency
            # below) instead of cramming everything into one row — at the
            # narrower -15% widths, one row didn't leave enough room for
            # longer delivery names before they got clipped.
            if editable_name:
                cb = ft.Checkbox(value=cb_val, on_change=_on_cb)
                name_f = ft.TextField(
                    value=str(pf_m.get("name") or mname or ""),
                    hint_text=_t("calc.cost.delivery_name_hint", "Назва доставки"),
                    dense=True, border_radius=6, expand=True,
                    content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
                    text_style=ft.TextStyle(size=12),
                )
                method_widgets[mid] = {"name": mname or _t("calc.cost.delivery_generic_name", "Доставка {n}").format(n=idx + 1),
                                       "name_f": name_f, "ccy": mccy, "cb": cb, "tariff_f": tariff_f}
                top_row = ft.Row([name_f], spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER)
            else:
                cb = ft.Checkbox(value=cb_val, on_change=_on_cb)
                method_widgets[mid] = {"name": mname, "name_f": None, "ccy": mccy, "cb": cb, "tariff_f": tariff_f}
                top_row = ft.Row(
                    [ft.Text(mname, size=11, color=_DARK, expand=True)],
                    spacing=0, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
            bottom_controls = [
                cb, tariff_f,
                ft.Text(mccy, size=9, color=_MUTED, width=28),
            ]
            if dynamic and len(methods) > 1:
                bottom_controls.append(ft.IconButton(
                    ft.Icons.CLOSE, icon_size=14, icon_color=_MUTED,
                    tooltip=_t("common.delete", "Видалити"),
                    on_click=lambda _, method_id=mid: _remove_method_slot(method_id),
                ))
            rows.append(ft.Column(
                [
                    top_row,
                    ft.Row(bottom_controls, spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ],
                spacing=2, tight=True,
            ))
        if dynamic:
            rows.append(ft.TextButton(
                "+ " + _t("calc.cost.add_delivery_method", "Додати спосіб доставки"),
                icon=ft.Icons.ADD, on_click=_add_method_slot,
                style=ft.ButtonStyle(padding=0),
            ))
        methods_col.controls = rows

    def _snapshot_methods_pf() -> dict:
        return {
            mid: {
                "checked": w["cb"].value, "tariff": w["tariff_f"].value,
                **({"name": w["name_f"].value} if w.get("name_f") else {}),
            }
            for mid, w in method_widgets.items()
        }

    def _add_method_slot(_=None):
        snap = _snapshot_methods_pf()
        n = len(methods0) + 1
        methods0.append({
            "id": f"custom_method_{n}", "name": "", "effective_price_per_kg": 0,
            "effective_price_currency": "USD", "editable_name": True,
        })
        _rebuild_methods(methods0, snap, dm_overrides_state[0])
        with contextlib.suppress(Exception):
            page.update()

    def _remove_method_slot(mid: str):
        snap = _snapshot_methods_pf()
        snap.pop(mid, None)
        methods0[:] = [m for m in methods0 if str(m.get("id") or "") != mid]
        _rebuild_methods(methods0, snap, dm_overrides_state[0])
        with contextlib.suppress(Exception):
            page.update()

    def _rebuild_tiers(tiers: list[dict], pf_tiers: dict):
        tier_widgets.clear()
        rows = []
        if not tiers:
            rows.append(ft.Text(_t("calc.cost.no_price_tiers", "Ціни продажу не налаштовані для цього регіону"),
                                size=11, color=_MUTED, italic=True))
        for t in tiers:
            tid  = str(t.get("id") or "")
            tnm  = str(t.get("name") or tid)
            tccy = str(t.get("currency") or "USD").upper()
            tf   = ft.TextField(
                value=str((pf_tiers or {}).get(tid) or ""),
                width=96, border_radius=6,
                keyboard_type=ft.KeyboardType.NUMBER, dense=True,
                content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
                text_style=ft.TextStyle(size=12),
            )
            tier_widgets[tid] = {"name": tnm, "ccy": tccy, "field": tf}
            rows.append(ft.Row(
                [ft.Text(f"{tnm} ({tccy})", size=10, expand=True,
                         overflow=ft.TextOverflow.ELLIPSIS), tf],
                spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ))
        tiers_col.controls = rows

    def _sync_manual_tiers_from_widgets():
        for t in manual_tiers_state:
            w = manual_tier_widgets.get(t["id"])
            if w:
                t["name"] = w["name_f"].value
                t["price"] = w["price_f"].value
                t["ccy"] = w["ccy_dd"].value

    def _rebuild_manual_tiers():
        manual_tier_widgets.clear()
        rows = []
        for t in manual_tiers_state:
            tid = t["id"]
            name_f = ft.TextField(
                value=str(t.get("name") or ""),
                hint_text=_t("calc.tier.custom_name", "Назва рівня"),
                dense=True, border_radius=6, expand=True,
                content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
                text_style=ft.TextStyle(size=12),
            )
            price_f = ft.TextField(
                value=str(t.get("price") or ""), width=90, border_radius=6, dense=True,
                keyboard_type=ft.KeyboardType.NUMBER,
                content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
                text_style=ft.TextStyle(size=12),
            )
            ccy_dd = _dd("", [(c, c) for c in _TIER_CCYS], value=str(t.get("ccy") or "USD"), width=_CCY_W)
            manual_tier_widgets[tid] = {"name_f": name_f, "price_f": price_f, "ccy_dd": ccy_dd}
            rows.append(ft.Container(
                padding=ft.padding.only(bottom=4),
                border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                name_f,
                                ft.IconButton(
                                    ft.Icons.CLOSE, icon_size=14, icon_color=_MUTED,
                                    tooltip=_t("common.delete", "Видалити"),
                                    on_click=lambda _, tid=tid: _remove_manual_tier(tid),
                                ),
                            ],
                            spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Row([price_f, ccy_dd], spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ],
                    spacing=4, tight=True,
                ),
            ))
        rows.append(ft.TextButton(
            "+ " + _t("calc.tier.add_level", "Додати рівень ціни"),
            icon=ft.Icons.ADD, on_click=_add_manual_tier,
            style=ft.ButtonStyle(padding=0),
        ))
        manual_tiers_col.controls = rows

    def _add_manual_tier(_=None):
        _sync_manual_tiers_from_widgets()
        n = _manual_tiers_next_id[0]
        _manual_tiers_next_id[0] += 1
        manual_tiers_state.append({"id": f"manual_tier_{n}", "name": "", "price": "", "ccy": "USD"})
        _rebuild_manual_tiers()
        with contextlib.suppress(Exception):
            page.update()

    def _remove_manual_tier(tid: str):
        _sync_manual_tiers_from_widgets()
        manual_tiers_state[:] = [t for t in manual_tiers_state if t["id"] != tid]
        _rebuild_manual_tiers()
        with contextlib.suppress(Exception):
            page.update()

    if tiers_dynamic:
        _rebuild_manual_tiers()

    # Restoring a saved calculation: the standalone/editable-name delivery
    # list always starts from a single blank slot (see
    # _StandaloneDB._default_delivery_methods), but the saved calc may have
    # 2+ user-added ones — without re-creating a slot for each saved id
    # here, _rebuild_methods below only had "method_1" to iterate over and
    # silently dropped every other saved delivery method on reopen (looked
    # like they'd been deleted, even though _get_data had saved them fine).
    _pf_methods_seed = pf.get("methods") or {}
    if methods0 and all(bool(m.get("editable_name")) for m in methods0):
        _existing_method_ids = {str(m.get("id") or "") for m in methods0}
        for _extra_id in _pf_methods_seed:
            if _extra_id not in _existing_method_ids:
                methods0.append({
                    "id": _extra_id, "name": "", "effective_price_per_kg": 0,
                    "effective_price_currency": "USD", "editable_name": True,
                })

    _rebuild_methods(methods0, pf.get("methods") or {}, dm_overrides_state[0])
    _rebuild_tiers(tiers0, pf.get("tiers") or {})

    # ── Region section ─────────────────────────────────────────────────────────
    region_section: list = []
    if len(regions_list) > 1:
        def _on_region_change(e):
            rid_name = str(e.control.value or "").strip()
            if not rid_name:
                return
            try:
                cur_region[0] = rid_name
                cur_uuid[0]   = _resolve_region_id(db, company_id, rid_name)
                dm_overrides_state[0] = db.get_manager_delivery_overrides(manager_id, region_id=cur_uuid[0]) or {}
                new_m, new_t = _load_for_region(rid_name)
                new_ccys = _all_ccys(new_m, new_t)
                new_opts = [ft.dropdown.Option(key="", text=_t("calc.field.choose_placeholder", "— оберіть —"))] + [
                    ft.dropdown.Option(key=c, text=c) for c in new_ccys
                ]
                for dd in (buy_ccy_dd, pkg_ccy_dd, cost_ccy_dd):
                    cur_v = dd.value
                    dd.options = new_opts
                    if cur_v not in new_ccys:
                        dd.value = new_ccys[0] if new_ccys else None
                _rebuild_methods(new_m, {}, dm_overrides_state[0])
                _rebuild_tiers(new_t, {})
                result_col.controls = []
            except Exception:
                logger.exception("Region switch failed in cost+duty calculator (region=%s)", rid_name)
                result_col.controls = [
                    ft.Container(
                        bgcolor="#FEE2E2", border=ft.border.all(1, "#DC2626"),
                        border_radius=8, padding=ft.padding.all(12),
                        content=ft.Text(
                            _t("calc.err.region_switch", "⚠ Помилка перемикання регіону: {region}").format(region=rid_name),
                            color=_ERR_FG, size=12,
                        ),
                    )
                ]
            with contextlib.suppress(Exception):
                page.update()

        region_dd = _dd(_t("calc.field.region", "Регіон"), [(r, r) for r in regions_list],
                        value=cur_region[0], width=_INP_W)
        region_dd.on_select = _on_region_change
        region_section = [_section(_t("calc.field.region", "Регіон")), region_dd]

    # ── Inputs column ──────────────────────────────────────────────────────────
    inputs_col = ft.Column(
        [
            *region_section,
            _section(_t("calc.cost.section.purchase", "Закупівля")),
            ft.Row([price_f, buy_ccy_dd], spacing=4),
            ft.Row([pkg_f,   pkg_ccy_dd], spacing=4),
            weight_f,
            _section(_t("calc.cost.section.cost_currency", "Валюта собівартості")),
            cost_ccy_dd,
            _section(_t("calc.cost.section.delivery_methods", "Типи доставки  (✓ — обрати, тариф — редагується)")),
            ft.Container(content=methods_col),
            _section(_t("calc.cost_clo.section.duty", "Мито (ставка × (С/В ÷ коеф. коригування))")),
            clo_rate_f,
            clo_coef_f,
            _section(_t("calc.field.rent_pct_optional", "Рентабельність (необов'яз.)")),
            rent_f,
            _section(_t("calc.cost.section.price_tiers", "Ціни продажу (необов'яз.)")),
            *(
                [ft.Container(content=manual_tiers_col)]
                if tiers_dynamic else
                [
                    ft.Container(content=tiers_col),
                    _section(_t("calc.tier.custom_section", "Додатковий рівень ціни (вручну)")),
                    custom_tier_name_f,
                    ft.Row([custom_tier_price_f, custom_tier_ccy_dd], spacing=4),
                ]
            ),
        ],
        spacing=4,
        scroll=ft.ScrollMode.AUTO,
        width=_INP_W,
    )

    # ── Results column ─────────────────────────────────────────────────────────
    result_col = ft.Column(
        [],
        spacing=8,
        scroll=ft.ScrollMode.AUTO,
        width=_RES_W,
    )

    # ── Calculate ─────────────────────────────────────────────────────────────
    def _calculate(_=None):
        rates = _fetch_uah_rates(page)
        purchase_price = _safe_float(price_f.value)
        pkg_price      = _safe_float(pkg_f.value) or 0.0
        weight         = _safe_float(weight_f.value)
        rent_pct       = _safe_float(rent_f.value)
        clo_rate       = _safe_float(clo_rate_f.value)
        clo_coef       = _safe_float(clo_coef_f.value)
        buy_ccy  = str(buy_ccy_dd.value  or "USD").upper()
        pkg_ccy  = str(pkg_ccy_dd.value  or "USD").upper()
        cost_ccy = str(cost_ccy_dd.value or "USD").upper()

        def _err(msg: str):
            result_col.controls = [
                ft.Container(
                    bgcolor="#FEF3C7", border=ft.border.all(1, "#F59E0B"),
                    border_radius=8, padding=ft.padding.all(12),
                    content=ft.Text(f"⚠  {msg}", color=_C_WARN, size=12),
                )
            ]
            with contextlib.suppress(Exception):
                page.update()

        if purchase_price is None:
            _err(_t("calc.err.enter_purchase_price", "Введіть ціну закупівлі"))
            return
        if weight is None:
            _err(_t("calc.err.enter_weight", "Введіть вагу"))
            return
        if clo_rate is None:
            _err(_t("calc.cost_clo.err.enter_duty_rate", "Введіть ставку мита"))
            return
        if not clo_coef:
            _err(_t("calc.cost_clo.err.enter_freight_coef", "Введіть коефіцієнт для нарахування мита (CLA)"))
            return

        selected = [(mid, w) for mid, w in method_widgets.items() if w["cb"].value]
        if not selected:
            _err(_t("calc.err.choose_delivery_method", "Оберіть хоча б один тип доставки"))
            return

        buy_in_cv = _convert(purchase_price, buy_ccy, cost_ccy, rates)
        if buy_in_cv is None:
            detail = (
                f" ({'; '.join(_FX_LAST_ERRORS)})" if _FX_LAST_ERRORS
                else " " + _t("calc.err.rates_not_loaded", "(курси ще не завантажені — спробуйте ще раз за кілька секунд)")
            )
            _err(_t("calc.err.no_rate", "Немає курсу {a} → {b}").format(a=buy_ccy, b=cost_ccy) + detail)
            return
        pkg_in_cv  = (_convert(pkg_price, pkg_ccy, cost_ccy, rates) or 0.0) if pkg_price else 0.0
        base_cost  = buy_in_cv + pkg_in_cv
        has_rent   = rent_pct is not None and 0 < rent_pct < 100

        tier_vals: list[tuple[str, str, float]] = []
        for tid, tw in tier_widgets.items():
            v = _safe_float(tw["field"].value)
            if v is not None and v > 0:
                tier_vals.append((tw["name"], tw["ccy"], v))
        if tiers_dynamic:
            for w in manual_tier_widgets.values():
                nm = str(w["name_f"].value or "").strip()
                pr = _safe_float(w["price_f"].value)
                if nm and pr is not None and pr > 0:
                    tier_vals.append((nm, str(w["ccy_dd"].value or "USD").upper(), pr))
        else:
            custom_tier_name = str(custom_tier_name_f.value or "").strip()
            custom_tier_price = _safe_float(custom_tier_price_f.value)
            if custom_tier_name and custom_tier_price is not None and custom_tier_price > 0:
                tier_vals.append((custom_tier_name, str(custom_tier_ccy_dd.value or "USD").upper(), custom_tier_price))

        extra_ccys: set[str] = {buy_ccy, pkg_ccy}
        cards = []

        for mid, mw in selected:
            tariff      = _safe_float(mw["tariff_f"].value) or 0.0
            mccy        = mw["ccy"]
            mw_name     = (str(mw["name_f"].value or "").strip() if mw.get("name_f") else "") or mw["name"]
            delivery_cv = _convert(weight * tariff, mccy, cost_ccy, rates) or (weight * tariff)
            extra_ccys.add(mccy)
            cv = base_cost + delivery_cv
            clo_amount = clo_rate / 100.0 * (cv / clo_coef)
            cv_clo = cv + clo_amount

            # Every row up to (not including) the final "Собівартість з
            # митом" total gets its % share of that total (cv_clo) — that
            # includes the "С/В без мита" subtotal, not just the leaf cost
            # components. None on rows after it. See _cost_card's docstring.
            pct_of = (lambda amt: amt / cv_clo * 100) if cv_clo > 0 else (lambda amt: None)

            card_rows: list[tuple[str, str, str, bool, float | None]] = [
                (_t("calc.result.purchase_price", "Ціна закупівлі:"), _fmt_money(buy_in_cv, cost_ccy), _DARK, False, pct_of(buy_in_cv)),
            ]
            if pkg_in_cv:
                card_rows.append((_t("calc.result.packaging", "Упаковка:"), _fmt_money(pkg_in_cv, cost_ccy), _MUTED, False, pct_of(pkg_in_cv)))
            card_rows.append(
                (_t("calc.result.delivery_line", "Доставка ({weight} кг × {tariff} {ccy}/кг):").format(weight=weight, tariff=tariff, ccy=mccy),
                 _fmt_money(delivery_cv, cost_ccy), _MUTED, False, pct_of(delivery_cv))
            )
            card_rows.append((_t("calc.cost_clo.result.cost_no_duty", "С/В без мита:"), _fmt_money(cv, cost_ccy), _C_BLUE, True, pct_of(cv)))
            card_rows.append(
                (_t("calc.cost_clo.result.duty_line", "Мито ({rate:.1f}% ÷ {coef:g}):").format(rate=clo_rate, coef=clo_coef),
                 _fmt_money(clo_amount, cost_ccy), _ERR_FG, True, pct_of(clo_amount))
            )
            card_rows.append((_t("calc.cost_clo.result.cost_with_duty", "Собівартість з митом:"), _fmt_money(cv_clo, cost_ccy), _C_OK, True, None))

            if has_rent:
                sale = cv_clo / (1.0 - rent_pct / 100.0)
                card_rows.append(
                    (_t("calc.result.price_at_rent", "Ціна при рент. {pct:.0f}%:").format(pct=rent_pct), _fmt_money(sale, cost_ccy), _C_BLUE, True, None)
                )

            for tnm, tccy, tprice in tier_vals:
                tprice_cv = _convert(tprice, tccy, cost_ccy, rates) or tprice
                r_pct = (tprice_cv - cv_clo) / tprice_cv * 100 if tprice_cv > 0 else 0.0
                clr = _C_OK if r_pct >= 20 else (_C_WARN if r_pct >= 0 else _C_ERR)
                price_label = (
                    f"{_fmt_money(tprice, tccy)} - {_fmt_money(tprice_cv, cost_ccy)}"
                    if tccy != cost_ccy else _fmt_money(tprice_cv, cost_ccy)
                )
                card_rows.append(
                    (f"{tnm} ({price_label}):",
                     f"{r_pct:.1f}%", clr, False, None)
                )

            cards.append(_cost_card(mw_name, card_rows, highlight=True))

        # Rates line
        rate_parts = _rate_texts(buy_ccy, cost_ccy, extra_ccys, rates)
        rate_str   = "  |  ".join(t.value for t in rate_parts) if rate_parts else ""

        result_col.controls = [
            ft.Container(
                bgcolor=_C_BG,
                border_radius=8,
                padding=ft.padding.symmetric(horizontal=12, vertical=8),
                content=ft.Column(
                    [
                        ft.Text(_t("calc.results_title", "Результати розрахунку"), size=12,
                                weight=ft.FontWeight.W_700, color=_DARK),
                        ft.Text(
                            _t("calc.cost_clo.formula", "С/В з митом = С/В + Ставка мита% × (С/В ÷ Коефіцієнт для нарахування мита (CLA))"),
                            size=9, color=_MUTED, italic=True,
                        ),
                        *(
                            [ft.Text(rate_str, size=9, color=_MUTED)]
                            if rate_str else []
                        ),
                    ],
                    spacing=2, tight=True,
                ),
            ),
            *cards,
        ]
        with contextlib.suppress(Exception):
            page.update()

    def _get_data() -> dict:
        return {
            "region_id":    cur_region[0],
            "purchase_price": price_f.value,
            "purchase_ccy": buy_ccy_dd.value,
            "pkg_price":    pkg_f.value,
            "pkg_ccy":      pkg_ccy_dd.value,
            "cost_ccy":     cost_ccy_dd.value,
            "weight":       weight_f.value,
            "rent_pct":     rent_f.value,
            "clo_rate":     clo_rate_f.value,
            "clo_coef":     clo_coef_f.value,
            "methods": {
                mid: {
                    "checked": w["cb"].value, "tariff": w["tariff_f"].value,
                    **({"name": w["name_f"].value} if w.get("name_f") else {}),
                }
                for mid, w in method_widgets.items()
            },
            "tiers": {tid: tw["field"].value for tid, tw in tier_widgets.items()},
            **(
                {
                    "manual_tiers": [
                        {"id": tid, "name": w["name_f"].value, "price": w["price_f"].value, "ccy": w["ccy_dd"].value}
                        for tid, w in manual_tier_widgets.items()
                    ],
                }
                if tiers_dynamic else
                {
                    "custom_tier_name": custom_tier_name_f.value,
                    "custom_tier_price": custom_tier_price_f.value,
                    "custom_tier_ccy": custom_tier_ccy_dd.value,
                }
            ),
        }

    panel_body = ft.Row(
        [
            ft.Column(
                [
                    ft.Container(
                        width=_LEFT_W,
                        content=inputs_col,
                    ),
                    ft.FilledButton(
                        "  " + _t("calc.action.calculate", "Розрахувати"),
                        icon=ft.Icons.CALCULATE,
                        on_click=_calculate,
                        width=_LEFT_W,
                        style=ft.ButtonStyle(
                            shape=ft.RoundedRectangleBorder(radius=8),
                        ),
                    ),
                ],
                spacing=6,
                tight=True,
            ),
            ft.VerticalDivider(width=1, color=_BORDER),
            ft.Container(
                width=_RIGHT_W,
                content=result_col,
            ),
        ],
        spacing=10,
        vertical_alignment=ft.CrossAxisAlignment.START,
    )
    container = ft.Container(
        border=ft.border.all(1, _BORDER),
        border_radius=10,
        padding=8,
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Container(expand=True),
                        ft.IconButton(
                            ft.Icons.CLOSE, icon_size=14, tooltip=_t("calc.compare.remove_panel", "Прибрати панель"),
                            on_click=on_remove,
                        ),
                    ],
                    tight=True,
                ),
                panel_body,
            ],
            spacing=0, tight=True,
        ),
    )
    return container, _get_data


def _open_cost_clo(
    page, db, manager_id: str, company_id: str, region_id: str,
    regions_list: list[str], prefill: dict | None = None, back_fn=None,
):
    _t = i18n.bind(str(manager_id or ""))
    _ACTIVE_MANAGER_ID[0] = str(manager_id or "")

    def _factory(pf: dict, on_remove):
        return _build_cost_clo_panel(
            page, db, manager_id, company_id, region_id, regions_list, pf, on_remove
        )

    def _help(_):
        from views.help_utils import show_modal_help
        show_modal_help(page, _t("calc.cost_clo.title", "Собівартість з митом"), [
            _t("calc.cost_clo.help1", "Мито нараховується тільки на ту частку доставки, що йде поза межами ЄС "
               "(до кордону з країнами ЄС) — Коефіцієнт для нарахування мита (CLA) вже враховує цю частку."),
            _t("calc.cost_clo.help2", "Формула: Мито = Ставка мита% × (С/В без мита ÷ Коефіцієнт для нарахування мита (CLA))."),
            _t("calc.cost_clo.help3", "Приклад: інвойс 2 + логістика 1 = 3, ставка 9,7%, коефіцієнт 1,2 "
               "(80% поза ЄС / 20% в межах ЄС) → мито = 9,7% × (3 ÷ 1,2) = 0,2425."),
            _t("calc.cost_clo.help4", "Підсумкова С/В з митом використовується далі для рентабельності й "
               "порівняння з цінами продажу — так само, як у звичайному "
               "калькуляторі Собівартості."),
            _t("calc.compare.help_note", "Кнопкою «+» можна додати ще одну панель для порівняння (до 5) — "
               "копіюються лише введені значення, не результат."),
        ])

    def _reopen(data: dict):
        _open_cost_clo(page, db, manager_id, company_id, region_id, regions_list, prefill=data, back_fn=back_fn)

    _run_multi_panel_calculator(
        page, db, manager_id, "cost_clo", _t("calc.cost_clo.title", "Собівартість з митом"), prefill, _factory,
        single_panel_width=_LEFT_W + 21 + _RIGHT_W + 32,
        back_fn=back_fn,
        help_fn=_help,
        reopen_fn=_reopen,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ── 3. Рентабельність ─────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
def _build_profit_panel(
    page,
    db,
    manager_id: str,
    company_id: str,
    region_id: str,
    regions_list: list[str],
    prefill: dict | None,
    on_remove,
):
    _t = i18n.bind(str(manager_id or ""))
    _ACTIVE_MANAGER_ID[0] = str(manager_id or "")
    pf = prefill or {}
    cur_region = [str(pf.get("region_id") or region_id or "")]

    def _load_tiers(rid_name: str):
        rid = _resolve_region_id(db, company_id, rid_name)
        try:
            t = db.get_manager_region_price_tiers_overrides(manager_id, rid) or []
            return t or db.get_region_price_tiers(rid) or []
        except Exception:
            return []

    tiers0 = _load_tiers(cur_region[0])
    # No real per-company price tiers configured for this region (always
    # true in the standalone/portable app, which has no regions at all) —
    # render a growable manual list ("+" adds as many levels as needed)
    # instead of a "not configured" message + a single fixed manual field.
    tiers_dynamic = not bool(tiers0)

    def _all_ccys(tiers) -> list[str]:
        s: set[str] = {"USD", "UAH", "EUR", "PLN", "CNY"}
        for t in tiers:
            s.add(str(t.get("currency") or "USD").upper())
        return sorted(s)

    ccy_opts0 = [(c, c) for c in _all_ccys(tiers0)]
    _PL = 280   # left column width
    _PR = 240   # right column width
    cost_f  = _field(_t("calc.profit.cost", "С/В *"),           width=160, value=pf.get("cost", ""))
    cost_dd = _dd(_t("calc.field.currency", "Валюта"), ccy_opts0, value=str(pf.get("cost_ccy") or "USD"), width=114)
    rent_f  = _field(_t("calc.field.rent_pct", "Рентабельність, %"), width=_PL - 4, value=pf.get("rent_pct", ""), optional=True)

    _TIER_CCYS = ("USD", "UAH", "EUR", "PLN", "CNY")
    custom_tier_name_f = _field(
        _t("calc.tier.custom_name", "Назва рівня"), width=_PL, value=pf.get("custom_tier_name", ""),
        number=False, optional=True,
    )
    custom_tier_price_f = _field(
        _t("calc.tier.custom_price", "Ціна рівня"), width=160, value=pf.get("custom_tier_price", ""), optional=True,
    )
    custom_tier_ccy_dd = _dd(
        _t("calc.field.currency", "Валюта"), [(c, c) for c in _TIER_CCYS],
        value=str(pf.get("custom_tier_ccy") or "USD"), width=114,
    )

    tiers_col = ft.Column([], spacing=4)
    result_col = ft.Column([], spacing=4)
    rates_row = ft.Row([], wrap=True, spacing=6)
    tier_widgets: dict[str, dict] = {}
    manual_tiers_col = ft.Column([], spacing=4, tight=True)
    manual_tier_widgets: dict[str, dict] = {}
    _manual_tiers_next_id = [1]
    if tiers_dynamic:
        _mt_seed = pf.get("manual_tiers")
        if not _mt_seed and pf.get("custom_tier_name"):
            _mt_seed = [{
                "name": pf.get("custom_tier_name"), "price": pf.get("custom_tier_price"),
                "ccy": pf.get("custom_tier_ccy"),
            }]
        manual_tiers_state: list[dict] = [
            {"id": t.get("id") or f"manual_tier_{i + 1}", "name": t.get("name", ""),
             "price": t.get("price", ""), "ccy": t.get("ccy") or "USD"}
            for i, t in enumerate(_mt_seed or [])
        ]
        _manual_tiers_next_id[0] = len(manual_tiers_state) + 1
    else:
        manual_tiers_state = []

    def _rebuild_tiers(tiers: list[dict], pf_tiers: dict):
        tier_widgets.clear()
        rows = []
        if not tiers:
            rows.append(
                ft.Text(
                    _t("calc.cost.no_price_tiers", "Ціни продажу не налаштовані для цього регіону"),
                    size=11,
                    color=_MUTED,
                    italic=True,
                )
            )
        for t in tiers:
            tid = str(t.get("id") or "")
            tnm = str(t.get("name") or tid)
            tccy = str(t.get("currency") or "USD").upper()
            tf = ft.TextField(
                value=str((pf_tiers or {}).get(tid) or ""),
                width=90,
                border_radius=6,
                keyboard_type=ft.KeyboardType.NUMBER,
                dense=True,
                content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
                text_style=ft.TextStyle(size=12),
            )
            tier_widgets[tid] = {"name": tnm, "ccy": tccy, "field": tf}
            rows.append(
                ft.Row(
                    [
                        ft.Text(
                            f"{tnm} ({tccy})",
                            size=10,
                            expand=True,
                            overflow=ft.TextOverflow.ELLIPSIS,
                        ),
                        tf,
                    ],
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                )
            )
        tiers_col.controls = rows

    def _sync_manual_tiers_from_widgets():
        for t in manual_tiers_state:
            w = manual_tier_widgets.get(t["id"])
            if w:
                t["name"] = w["name_f"].value
                t["price"] = w["price_f"].value
                t["ccy"] = w["ccy_dd"].value

    def _rebuild_manual_tiers():
        manual_tier_widgets.clear()
        rows = []
        for t in manual_tiers_state:
            tid = t["id"]
            name_f = ft.TextField(
                value=str(t.get("name") or ""),
                hint_text=_t("calc.tier.custom_name", "Назва рівня"),
                dense=True, border_radius=6, expand=True,
                content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
                text_style=ft.TextStyle(size=12),
            )
            price_f = ft.TextField(
                value=str(t.get("price") or ""), width=90, border_radius=6, dense=True,
                keyboard_type=ft.KeyboardType.NUMBER,
                content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
                text_style=ft.TextStyle(size=12),
            )
            ccy_dd = _dd("", [(c, c) for c in _TIER_CCYS], value=str(t.get("ccy") or "USD"), width=114)
            manual_tier_widgets[tid] = {"name_f": name_f, "price_f": price_f, "ccy_dd": ccy_dd}
            rows.append(ft.Container(
                padding=ft.padding.only(bottom=4),
                border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                name_f,
                                ft.IconButton(
                                    ft.Icons.CLOSE, icon_size=14, icon_color=_MUTED,
                                    tooltip=_t("common.delete", "Видалити"),
                                    on_click=lambda _, tid=tid: _remove_manual_tier(tid),
                                ),
                            ],
                            spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Row([price_f, ccy_dd], spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ],
                    spacing=4, tight=True,
                ),
            ))
        rows.append(ft.TextButton(
            "+ " + _t("calc.tier.add_level", "Додати рівень ціни"),
            icon=ft.Icons.ADD, on_click=_add_manual_tier,
            style=ft.ButtonStyle(padding=0),
        ))
        manual_tiers_col.controls = rows

    def _add_manual_tier(_=None):
        _sync_manual_tiers_from_widgets()
        n = _manual_tiers_next_id[0]
        _manual_tiers_next_id[0] += 1
        manual_tiers_state.append({"id": f"manual_tier_{n}", "name": "", "price": "", "ccy": "USD"})
        _rebuild_manual_tiers()
        with contextlib.suppress(Exception):
            page.update()

    def _remove_manual_tier(tid: str):
        _sync_manual_tiers_from_widgets()
        manual_tiers_state[:] = [t for t in manual_tiers_state if t["id"] != tid]
        _rebuild_manual_tiers()
        with contextlib.suppress(Exception):
            page.update()

    if tiers_dynamic:
        _rebuild_manual_tiers()

    _rebuild_tiers(tiers0, pf.get("tiers") or {})

    def _on_region_change(e):
        rid = str(e.control.value or "").strip()
        if not rid:
            return
        try:
            cur_region[0] = rid
            new_tiers = _load_tiers(rid)
            new_ccys = _all_ccys(new_tiers)
            new_opts = [ft.dropdown.Option(key="", text=_t("calc.field.choose_placeholder", "— оберіть —"))] + [
                ft.dropdown.Option(key=c, text=c) for c in new_ccys
            ]
            cost_dd.options = new_opts
            _rebuild_tiers(new_tiers, {})
            result_col.controls = []
            rates_row.controls = []
        except Exception:
            logger.exception("Region switch failed in profit calculator (region=%s)", rid)
            result_col.controls = [
                ft.Container(
                    bgcolor="#FEE2E2", border=ft.border.all(1, "#DC2626"),
                    border_radius=8, padding=ft.padding.all(12),
                    content=ft.Text(
                        _t("calc.err.region_switch", "⚠ Помилка перемикання регіону: {region}").format(region=rid),
                        color=_ERR_FG, size=12,
                    ),
                )
            ]
        with contextlib.suppress(Exception):
            page.update()

    region_section: list = []
    if len(regions_list) > 1:
        region_dd = _dd(
            _t("calc.field.region", "Регіон"), [(r, r) for r in regions_list], value=cur_region[0], width=200
        )
        region_dd.on_select = _on_region_change
        region_section = [_section(_t("calc.field.region", "Регіон")), region_dd]

    def _calculate(_=None):
        rates = _fetch_uah_rates(page)
        cv = _safe_float(cost_f.value)
        cccy = str(cost_dd.value or "USD").upper()
        rpct = _safe_float(rent_f.value)
        if cv is None:
            result_col.controls = [
                ft.Container(
                    bgcolor="#FEF3C7", border=ft.border.all(1, "#F59E0B"),
                    border_radius=8, padding=ft.padding.all(12),
                    content=ft.Text("⚠  " + _t("calc.profit.err.enter_cost", "Введіть С/В"), color=_C_WARN, size=12),
                )
            ]
            with contextlib.suppress(Exception):
                page.update()
            return

        has_rent = rpct is not None and 0 < rpct < 100
        tier_vals: list[tuple[str, str, float]] = []
        for tw in tier_widgets.values():
            v = _safe_float(tw["field"].value)
            if v is not None and v > 0:
                tier_vals.append((tw["name"], tw["ccy"], v))
        if tiers_dynamic:
            for w in manual_tier_widgets.values():
                nm = str(w["name_f"].value or "").strip()
                pr = _safe_float(w["price_f"].value)
                if nm and pr is not None and pr > 0:
                    tier_vals.append((nm, str(w["ccy_dd"].value or "USD").upper(), pr))
        else:
            custom_tier_name = str(custom_tier_name_f.value or "").strip()
            custom_tier_price = _safe_float(custom_tier_price_f.value)
            if custom_tier_name and custom_tier_price is not None and custom_tier_price > 0:
                tier_vals.append((custom_tier_name, str(custom_tier_ccy_dd.value or "USD").upper(), custom_tier_price))

        # Рентабельність has no itemized cost components (just a manually
        # entered total), so — per explicit decision — it doesn't get the
        # %-breakdown feature; every row here passes pct=None.
        extra_ccys: set[str] = set()
        card_rows: list[tuple[str, str, str, bool, float | None]] = [
            (_t("calc.profit.result.cost_line", "С/В ({ccy}):").format(ccy=cccy), _fmt_money(cv, cccy), _C_OK, True, None),
        ]
        if has_rent:
            sale = cv / (1.0 - rpct / 100.0)
            card_rows.append(
                (_t("calc.result.price_at_rent", "Ціна при рент. {pct:.0f}%:").format(pct=rpct), _fmt_money(sale, cccy), _C_BLUE, True, None)
            )
        for tnm, tccy, tprice in tier_vals:
            tprice_cv = _convert(tprice, tccy, cccy, rates) or tprice
            r_pct = (tprice_cv - cv) / tprice_cv * 100 if tprice_cv > 0 else 0.0
            clr = _C_OK if r_pct >= 20 else (_C_WARN if r_pct >= 0 else _C_ERR)
            price_label = (
                f"{_fmt_money(tprice, tccy)} - {_fmt_money(tprice_cv, cccy)}"
                if tccy != cccy else _fmt_money(tprice_cv, cccy)
            )
            card_rows.append(
                (f"{tnm} ({price_label}):", f"{r_pct:.1f}%", clr, False, None)
            )
            extra_ccys.add(tccy)

        rate_parts = _rate_texts(cccy, cccy, extra_ccys, rates)
        rate_str = "  |  ".join(t.value for t in rate_parts) if rate_parts else ""

        result_col.controls = [
            ft.Container(
                bgcolor=_C_BG,
                border_radius=8,
                padding=ft.padding.symmetric(horizontal=12, vertical=8),
                content=ft.Column(
                    [
                        ft.Text(_t("calc.results_title", "Результати розрахунку"), size=12,
                                weight=ft.FontWeight.W_700, color=_DARK),
                        ft.Text(
                            _t("calc.profit.formula", "Ціна = С/В ÷ (1 − Рентабельність%)  |  Рентабельність% = (Ціна − С/В) ÷ Ціна × 100"),
                            size=9, color=_MUTED, italic=True,
                        ),
                        *(
                            [ft.Text(rate_str, size=9, color=_MUTED)]
                            if rate_str else []
                        ),
                    ],
                    spacing=2, tight=True,
                ),
            ),
            _cost_card(_t("calc.profit.title", "Рентабельність"), card_rows),
        ]
        rates_row.controls = []
        with contextlib.suppress(Exception):
            page.update()

    def _get_data() -> dict:
        return {
            "region_id": cur_region[0],
            "cost": cost_f.value,
            "cost_ccy": cost_dd.value,
            "rent_pct": rent_f.value,
            "tiers": {tid: tw["field"].value for tid, tw in tier_widgets.items()},
            **(
                {
                    "manual_tiers": [
                        {"id": tid, "name": w["name_f"].value, "price": w["price_f"].value, "ccy": w["ccy_dd"].value}
                        for tid, w in manual_tier_widgets.items()
                    ],
                }
                if tiers_dynamic else
                {
                    "custom_tier_name": custom_tier_name_f.value,
                    "custom_tier_price": custom_tier_price_f.value,
                    "custom_tier_ccy": custom_tier_ccy_dd.value,
                }
            ),
        }

    inputs_col = ft.Column(
        [
            *region_section,
            _section(_t("calc.profit.cost_section", "С/В")),
            ft.Row([cost_f, cost_dd], spacing=6),
            _section(_t("calc.field.rent_pct_optional", "Рентабельність (необов'яз.)")),
            rent_f,
            _section(_t("calc.cost.section.price_tiers", "Ціни продажу (необов'яз.)")),
            *(
                [ft.Container(content=manual_tiers_col)]
                if tiers_dynamic else
                [
                    tiers_col,
                    _section(_t("calc.tier.custom_section", "Додатковий рівень ціни (вручну)")),
                    custom_tier_name_f,
                    ft.Row([custom_tier_price_f, custom_tier_ccy_dd], spacing=6),
                ]
            ),
            ft.Container(height=4),
            ft.FilledButton(
                "  " + _t("calc.action.calculate", "Розрахувати"), on_click=_calculate, icon=ft.Icons.CALCULATE,
                width=_PL,
                style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
            ),
        ],
        spacing=4,
        scroll=ft.ScrollMode.AUTO,
        width=_PL,
    )

    panel_body = ft.Row(
        [
            ft.Container(content=inputs_col, width=_PL),
            ft.VerticalDivider(width=1, color=_BORDER),
            ft.Container(
                content=ft.Column(
                    [result_col],
                    spacing=4,
                ),
                width=_PR,
            ),
        ],
        spacing=10,
        vertical_alignment=ft.CrossAxisAlignment.START,
    )
    container = ft.Container(
        border=ft.border.all(1, _BORDER),
        border_radius=10,
        padding=8,
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Container(expand=True),
                        ft.IconButton(
                            ft.Icons.CLOSE, icon_size=14, tooltip=_t("calc.compare.remove_panel", "Прибрати панель"),
                            on_click=on_remove,
                        ),
                    ],
                    tight=True,
                ),
                panel_body,
            ],
            spacing=0, tight=True,
        ),
    )
    return container, _get_data


def _open_profit(
    page, db, manager_id: str, company_id: str, region_id: str,
    regions_list: list[str], prefill: dict | None = None, back_fn=None,
):
    _t = i18n.bind(str(manager_id or ""))
    _ACTIVE_MANAGER_ID[0] = str(manager_id or "")

    def _factory(pf: dict, on_remove):
        return _build_profit_panel(
            page, db, manager_id, company_id, region_id, regions_list, pf, on_remove
        )

    def _reopen(data: dict):
        _open_profit(page, db, manager_id, company_id, region_id, regions_list, prefill=data, back_fn=back_fn)

    _run_multi_panel_calculator(
        page, db, manager_id, "profit", _t("calc.profit.title", "Рентабельність"), prefill, _factory,
        single_panel_width=238 + 1 + 204 + 32,  # -15%, matches cost/cost_clo's _LEFT_W/_RIGHT_W reduction
        back_fn=back_fn,
        reopen_fn=_reopen,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ── 4. Обертаємість / Забезпеченість (combined) ───────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
def _build_turnover_panel(page, db, manager_id: str, prefill: dict | None, on_remove):
    _t = i18n.bind(str(manager_id or ""))
    _ACTIVE_MANAGER_ID[0] = str(manager_id or "")
    pf = prefill or {}
    _FW = 336  # field width
    start_f = _field(_t("calc.turnover.start", "Склад початок *"), width=_FW, value=pf.get("start", ""))
    end_f = _field(_t("calc.turnover.end", "Склад кінець *"), width=_FW, value=pf.get("end", ""))
    days_f = _field(_t("calc.turnover.days", "Кількість днів у періоді *"), width=_FW, value=pf.get("days", ""))
    cost_f = _field(_t("calc.turnover.cost", "С/В реалізованого товару *"), width=_FW, value=pf.get("cost", ""))

    turnover_text = ft.Text("—", size=20, weight="bold", color=_ACCENT)
    coverage_text = ft.Text("—", size=20, weight="bold", color=_ACCENT)

    def _calculate(_=None):
        start = _safe_float(start_f.value)
        end = _safe_float(end_f.value)
        days = _safe_float(days_f.value)
        cv = _safe_float(cost_f.value)
        days_word = _t("calc.turnover.days_unit", "днів")
        if None in (start, end, days, cv):
            turnover_text.value = "⚠ " + _t("calc.turnover.err.fill_all", "Заповніть усі поля")
            turnover_text.color = _WARN_FG
            coverage_text.value = ""
        elif cv == 0:
            turnover_text.value = "⚠ " + _t("calc.turnover.err.cost_zero", "С/В = 0")
            turnover_text.color = _WARN_FG
            coverage_text.value = ""
        else:
            avg = (start + end) / 2.0
            turnover = (avg * days) / cv
            coverage = (end * days) / cv
            turnover_text.value = f"{_fmt_val(turnover, 1)} {days_word}"
            turnover_text.color = _ACCENT
            coverage_text.value = f"{_fmt_val(coverage, 1)} {days_word}"
            coverage_text.color = _ACCENT
        with contextlib.suppress(Exception):
            page.update()

    def _get_data() -> dict:
        return {
            "start": start_f.value, "end": end_f.value,
            "days": days_f.value, "cost": cost_f.value,
        }

    _PW = 400
    _IW = _PW - 32
    panel_body = ft.Container(
        width=_PW,
        padding=ft.padding.symmetric(horizontal=16, vertical=10),
        content=ft.Column(
            [
                ft.Container(
                    content=ft.Column([
                        ft.Text(
                            _t("calc.turnover.formula1", "Обертаємість = ((Поч + Кін) / 2 × Дні) / С/В"),
                            size=9, color=_MUTED, italic=True,
                        ),
                        ft.Text(
                            _t("calc.turnover.formula2", "Забезпеченість = (Кін × Дні) / С/В"),
                            size=9, color=_MUTED, italic=True,
                        ),
                    ], spacing=2, tight=True),
                    bgcolor=_SOFT, border_radius=6,
                    padding=ft.padding.symmetric(horizontal=10, vertical=6),
                ),
                start_f,
                end_f,
                days_f,
                cost_f,
                ft.FilledButton(
                    "  " + _t("calc.action.calculate", "Розрахувати"), on_click=_calculate, icon=ft.Icons.CALCULATE,
                    width=_IW,
                    style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                ),
                ft.Divider(height=6, color=_BORDER),
                ft.Container(
                    content=ft.Column([
                        ft.Row([
                            ft.Text(_t("calc.turnover.turnover_label", "Обертаємість:"), size=12, color=_MUTED, width=130),
                            turnover_text,
                        ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        ft.Row([
                            ft.Text(_t("calc.turnover.coverage_label", "Забезпеченість:"), size=12, color=_MUTED, width=130),
                            coverage_text,
                        ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    ], spacing=8, tight=True),
                    bgcolor=_SOFT, border_radius=8,
                    padding=ft.padding.symmetric(horizontal=14, vertical=10),
                ),
            ],
            spacing=8,
            tight=True,
        ),
    )
    container = ft.Container(
        border=ft.border.all(1, _BORDER),
        border_radius=10,
        padding=8,
        content=ft.Column(
            [
                ft.Row(
                    [
                        ft.Container(expand=True),
                        ft.IconButton(
                            ft.Icons.CLOSE, icon_size=14, tooltip=_t("calc.compare.remove_panel", "Прибрати панель"),
                            on_click=on_remove,
                        ),
                    ],
                    tight=True,
                ),
                panel_body,
            ],
            spacing=0, tight=True,
        ),
    )
    return container, _get_data


def _open_turnover_coverage(page, db, manager_id: str, prefill: dict | None = None, back_fn=None):
    _t = i18n.bind(str(manager_id or ""))
    _ACTIVE_MANAGER_ID[0] = str(manager_id or "")

    def _factory(pf: dict, on_remove):
        return _build_turnover_panel(page, db, manager_id, pf, on_remove)

    def _reopen(data: dict):
        _open_turnover_coverage(page, db, manager_id, prefill=data, back_fn=back_fn)

    _run_multi_panel_calculator(
        page, db, manager_id, "turnover", _t("calc.turnover.title", "Оборот / Забезпеченість"), prefill, _factory,
        single_panel_width=400,
        back_fn=back_fn,
        reopen_fn=_reopen,
    )


# ─────────────────────────────────────────────────────────────────────────────
# ── 6. Збережені ──────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
def _open_saved(
    page, db, manager_id: str, company_id: str, region_id: str, regions_list: list[str],
    back_fn=None,
):
    _t = i18n.bind(str(manager_id or ""))
    dlg = _FloatDialog()
    list_col = ft.Column([], spacing=4, scroll=ft.ScrollMode.AUTO)

    filter_cbs: dict[str, ft.Checkbox] = {
        k: ft.Checkbox(label=_calc_type_label(_t, k), value=True) for k, v in _CALC_TYPES
    }

    def _close(_=None):
        with contextlib.suppress(Exception):
            _fdlg_close(page, dlg)

    async def _go_back(_=None):
        _close()
        if back_fn:
            await asyncio.sleep(0.05)
            with contextlib.suppress(Exception):
                back_fn()

    def _open_entry(entry: dict):
        ct = str(entry.get("calc_type") or "")
        data = entry.get("data") or {}
        _close()
        _dispatch(page, db, manager_id, company_id, region_id, regions_list, ct, data)

    def _refresh_list(_=None):
        saved = _load_saved(db, manager_id)
        active = {k for k, cb in filter_cbs.items() if cb.value}
        filtered = [s for s in saved if str(s.get("calc_type") or "") in active]

        if not filtered:
            list_col.controls = [
                ft.Text(_t("calc.saved.empty", "Немає збережених розрахунків"), size=11, color=_MUTED)
            ]
            with contextlib.suppress(Exception):
                page.update()
            return

        rows = []
        for s in filtered:
            entry = s
            rows.append(
                ft.Container(
                    bgcolor=_SOFT,
                    border_radius=8,
                    border=ft.border.all(1, _BORDER),
                    padding=ft.padding.symmetric(horizontal=10, vertical=6),
                    content=ft.Row(
                        [
                            ft.Container(
                                content=ft.Text(
                                    _calc_type_label(_t, str(s.get("calc_type") or "")),
                                    size=10,
                                    color=_ACCENT,
                                    weight="bold",
                                ),
                                width=110,
                            ),
                            ft.Container(
                                content=ft.Text(
                                    str(s.get("name") or ""),
                                    size=11,
                                    weight="bold",
                                    color=_DARK,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                                expand=True,
                            ),
                            ft.Container(
                                content=ft.Text(
                                    str(s.get("ts") or ""), size=10, color=_MUTED
                                ),
                                width=110,
                            ),
                            ft.Row(
                                [
                                    ft.TextButton(
                                        _t("calc.saved.open", "Відкрити"),
                                        on_click=lambda _, e=entry: _open_entry(e),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.EDIT_OUTLINED,
                                        icon_size=16,
                                        tooltip=_t("common.edit", "Редагувати"),
                                        on_click=lambda _, e=entry: _do_rename(e),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.DELETE_OUTLINE,
                                        icon_size=16,
                                        icon_color=_ERR_FG,
                                        tooltip=_t("common.delete", "Видалити"),
                                        on_click=lambda _, e=entry: _do_delete(e),
                                    ),
                                ],
                                spacing=0,
                            ),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )
        list_col.controls = rows
        with contextlib.suppress(Exception):
            page.update()

    def _do_delete(entry: dict):
        _delete_calc_entry(db, manager_id, str(entry.get("id") or ""))
        _refresh_list()

    def _do_rename(entry: dict):
        name_f = ft.TextField(
            label=_t("calc.save_dlg.name_label", "Назва розрахунку"),
            value=str(entry.get("name") or ""), width=300, border_radius=8, autofocus=True,
        )
        rdlg = ft.AlertDialog(modal=True)

        def _do(_e=None):
            nm = str(name_f.value or "").strip()
            if nm:
                _rename_calc_entry(db, manager_id, str(entry.get("id") or ""), nm)
            with contextlib.suppress(Exception):
                page.pop_dialog()
            _refresh_list()

        rdlg.title = ft.Text(_t("calc.rename_dlg.title", "Перейменувати розрахунок"), weight="bold")
        rdlg.content = ft.Container(width=320, padding=16, content=name_f)
        rdlg.actions = [
            ft.TextButton(_t("common.cancel", "Скасувати"), on_click=lambda _: page.pop_dialog()),
            ft.FilledButton(_t("common.save", "Зберегти"), on_click=_do),
        ]
        rdlg.actions_alignment = ft.MainAxisAlignment.END
        name_f.on_submit = _do
        page.show_dialog(rdlg)

    for cb in filter_cbs.values():
        cb.on_change = _refresh_list

    def _on_lang_select(code: str) -> None:
        i18n.save_manager_language(db, str(manager_id or ""), code)
        _close()
        _open_saved(page, db, manager_id, company_id, region_id, regions_list, back_fn=back_fn)

    dlg.title = ft.Row(
        [
            ft.Text(_t("calc.saved.title", "Збережені розрахунки"), weight="bold", size=13, expand=True,
                    no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
            i18n.build_language_switcher(db, _on_lang_select, manager_id=str(manager_id or "")),
            *(
                [ft.IconButton(ft.Icons.ARROW_BACK, tooltip=_t("common.back", "Назад"),
                               on_click=_go_back, icon_size=16)]
                if back_fn else []
            ),
            _close_or_refresh_btn(page, _close, icon_size=16),
        ]
    )
    dlg.panel_width = 740
    dlg.content = ft.Container(
        width=740,
        height=480,
        padding=ft.padding.symmetric(horizontal=16, vertical=10),
        content=ft.Column(
            [
                ft.Row(list(filter_cbs.values()), spacing=12, wrap=False),
                ft.Divider(height=4, color=_BORDER),
                ft.Container(
                    content=list_col,
                    height=408,
                    clip_behavior=ft.ClipBehavior.HARD_EDGE,
                ),
            ],
            spacing=6,
            tight=True,
        ),
    )
    _fdlg_open(page, dlg)
    _refresh_list()


# ─────────────────────────────────────────────────────────────────────────────
# ── Main entry ─────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
def open_calculators_modal(
    page,
    db,
    manager_id: str,
    company_id: str,
    region_id: str,
    regions_list: list[str] | None = None,
    *,
    prefill_type: str | None = None,
    prefill_data: dict | None = None,
):
    """Open the main calculators picker dialog."""
    _t = i18n.bind(str(manager_id or ""))
    _ACTIVE_MANAGER_ID[0] = str(manager_id or "")
    rl = regions_list or ([region_id] if region_id else [])

    if prefill_type:
        _dispatch(
            page,
            db,
            manager_id,
            company_id,
            region_id,
            rl,
            prefill_type,
            prefill_data or {},
        )
        return

    dlg = _FloatDialog()

    def _close(_=None):
        with contextlib.suppress(Exception):
            _fdlg_close(page, dlg)

    def _pick(key: str):
        _close()
        _dispatch(page, db, manager_id, company_id, region_id, rl, key, {})

    icons_map = {
        "basic": ft.Icons.CALCULATE,
        "cost": ft.Icons.ATTACH_MONEY,
        "cost_clo": ft.Icons.LOCAL_SHIPPING,
        "profit": ft.Icons.TRENDING_UP,
        "turnover": ft.Icons.LOOP,
        "saved": ft.Icons.BOOKMARK,
    }
    _calc_labels = {
        "basic": _t("calc.type.basic", "Звичайний"),
        "cost": _t("calc.type.cost", "Собівартість"),
        "cost_clo": _t("calc.type.cost_clo", "С/В з митом"),
        "profit": _t("calc.type.profit", "Рентабельність"),
        "turnover": _t("calc.type.turnover", "Оборот / Забезп."),
        "saved": _t("calc.type.saved", "Збережені"),
    }
    all_items = [(k, _calc_labels.get(k, lbl)) for k, lbl in _CALC_TYPES] + [("saved", _calc_labels["saved"])]

    def _picker_btn(key: str, label: str) -> ft.Container:
        return ft.Container(
            content=ft.Column(
                [
                    ft.Icon(
                        icons_map.get(key, ft.Icons.CALCULATE), size=26, color=_ACCENT
                    ),
                    ft.Text(
                        label,
                        size=12,
                        weight="bold",
                        color=_DARK,
                        text_align=ft.TextAlign.CENTER,
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=6,
                tight=True,
            ),
            width=120,
            height=86,
            bgcolor=_SOFT,
            border_radius=14,
            border=ft.border.all(1, _BORDER),
            alignment=ft.Alignment(0, 0),
            on_click=lambda _, k=key: _pick(k),
            ink=True,
        )

    # 2 rows × 3 columns
    row1 = ft.Row([_picker_btn(k, lbl) for k, lbl in all_items[:3]], spacing=12)
    row2 = ft.Row([_picker_btn(k, lbl) for k, lbl in all_items[3:]], spacing=12)

    def _picker_help(_):
        from views.help_utils import show_modal_help
        show_modal_help(page, _t("calc.picker_title", "Калькулятори"), [
            _t("calc.picker_help.step1", "1. Оберіть потрібний тип калькулятора."),
            _t("calc.picker_help.step2", "2. Вибраний калькулятор відкриється одразу."),
        ])

    def _on_lang_select(code: str) -> None:
        i18n.save_manager_language(db, str(manager_id or ""), code)
        _close()
        open_calculators_modal(
            page, db, manager_id, company_id, region_id, regions_list,
            prefill_type=prefill_type, prefill_data=prefill_data,
        )

    dlg.title = ft.Row(
        [
            ft.Text(_t("calc.picker_title", "Калькулятори"), weight="bold", expand=True,
                    no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
            i18n.build_language_switcher(db, _on_lang_select, manager_id=str(manager_id or "")),
            ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=_t("common.help.tooltip", "Довідка"), on_click=_picker_help),
            _close_or_refresh_btn(page, _close, icon_size=18),
        ]
    )
    dlg.panel_width = 420
    dlg.center_content = True
    dlg.content = ft.Container(
        width=420,
        padding=16,
        content=ft.Column([row1, row2], spacing=12, tight=True),
    )
    _fdlg_open(page, dlg)


def _dispatch(page, db, manager_id, company_id, region_id, regions_list, key, data):
    def _back():
        open_calculators_modal(page, db, manager_id, company_id, region_id, regions_list)

    if key == "basic":
        _open_basic(page, db, manager_id, back_fn=_back)
    elif key == "cost":
        _open_cost(
            page, db, manager_id, company_id, region_id, regions_list, prefill=data, back_fn=_back
        )
    elif key == "cost_clo":
        _open_cost_clo(
            page, db, manager_id, company_id, region_id, regions_list, prefill=data, back_fn=_back
        )
    elif key == "profit":
        _open_profit(
            page, db, manager_id, company_id, region_id, regions_list, prefill=data, back_fn=_back
        )
    elif key in ("turnover", "coverage"):
        _open_turnover_coverage(page, db, manager_id, prefill=data, back_fn=_back)
    elif key == "saved":
        _open_saved(page, db, manager_id, company_id, region_id, regions_list, back_fn=_back)
