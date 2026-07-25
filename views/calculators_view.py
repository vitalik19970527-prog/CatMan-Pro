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
import threading
import time
import urllib.request
from datetime import datetime
from typing import Optional

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

_CALC_TYPES = [
    ("basic", "Звичайний"),
    ("cost", "Собівартість"),
    ("profit", "Рентабельність"),
    ("turnover", "Оборот / Забезп."),
]
_CALC_LABELS = dict(_CALC_TYPES)

# ── FX cache ───────────────────────────────────────────────────────────────
_FX_CACHE: dict = {"ts": 0.0, "rates": {}}
_FX_REFRESH_IN_FLIGHT = False
_FX_LAST_ERRORS: list[str] = []


def _fetch_uah_rates() -> dict:
    """Cache-only read — NEVER performs blocking network I/O on the calling
    thread. If the cache is missing/stale, a background daemon thread is
    kicked off to refresh it (see `_refresh_uah_rates_background`) and the
    best data available right now is returned immediately.

    Why: this used to fetch NBU/er-api rates INLINE (up to ~12s worst case:
    two sequential 6s-timeout requests) whenever the cache expired. Both
    callers (`_open_cost`, `_open_profit`) run synchronously in the click
    handler that opens the calculator dialog, so that inline fetch froze the
    whole app for however long the HTTP calls took — every time a user
    opened "Собівартість"/"Рентабельність" with a cold cache."""
    global _FX_CACHE, _FX_REFRESH_IN_FLIGHT
    now = time.time()
    ts = float(_FX_CACHE.get("ts") or 0)
    rates = _FX_CACHE.get("rates") or {}
    is_fresh = ts > 0 and (now - ts) < (900 if rates else 120)
    if not is_fresh and not _FX_REFRESH_IN_FLIGHT:
        _FX_REFRESH_IN_FLIGHT = True
        threading.Thread(target=_refresh_uah_rates_background, daemon=True).start()
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
    """Actual network fetch. Blocking — only ever call from a background
    thread (see `_refresh_uah_rates_background`), never from the UI thread."""

    def _get(url: str):
        with urllib.request.urlopen(url, timeout=6) as r:
            return json.loads(r.read().decode())

    def _nbu() -> dict:
        data = _get("https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json")
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

    def _er_api() -> dict:
        raw = _get("https://open.er-api.com/v6/latest/UAH")
        rts = (raw or {}).get("rates") if isinstance(raw, dict) else {}
        out: dict = {"UAH": 1.0}
        for cc, v in (rts or {}).items():
            cc_u = str(cc).upper().strip()
            fv = float(v) if v else 0
            if cc_u and cc_u != "UAH" and fv > 0:
                out[cc_u] = 1.0 / fv
        return out

    def _fawazahmed0() -> dict:
        # Free, keyless, CDN-hosted currency API — third independent fallback
        # in case both NBU and open.er-api.com are unreachable (corporate
        # firewall, ISP block, etc).
        raw = _get(
            "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/"
            "v1/currencies/uah.json"
        )
        rts = (raw or {}).get("uah") if isinstance(raw, dict) else {}
        out: dict = {"UAH": 1.0}
        for cc, v in (rts or {}).items():
            cc_u = str(cc).upper().strip()
            fv = float(v) if v else 0
            if cc_u and cc_u != "UAH" and fv > 0:
                out[cc_u] = 1.0 / fv
        return out

    global _FX_LAST_ERRORS
    errors: list[str] = []
    result: dict = {}
    for fn in (_nbu, _er_api, _fawazahmed0):
        try:
            result = fn() or {}
            if result.get("USD") and result.get("UAH"):
                _FX_LAST_ERRORS = []
                return result
        except Exception as exc:
            errors.append(f"{fn.__name__}: {type(exc).__name__}: {exc}")
        else:
            if not result:
                errors.append(f"{fn.__name__}: порожня відповідь")

    _FX_LAST_ERRORS = errors
    if errors:
        logger.warning("[calc] fx rate fetch failed for all sources: %s", "; ".join(errors))
    return result


def _convert(amount: float, from_ccy: str, to_ccy: str, rates: dict) -> Optional[float]:
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


def _safe_float(v) -> Optional[float]:
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


def _show_save_dialog(page, db, manager_id: str, calc_type: str, data: dict):
    _t = i18n.bind(str(manager_id or ""))
    name_f = ft.TextField(
        label=_t("calc.save_dlg.name_label", "Назва розрахунку"), width=300, border_radius=8, autofocus=True
    )
    sdlg = ft.AlertDialog(modal=True)

    def _do(_e=None):
        nm = str(name_f.value or "").strip() or _CALC_LABELS.get(calc_type, calc_type)
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
        ft.Text(_t("calc.save_dlg.title", "Зберегти розрахунок"), weight="bold", expand=True),
        ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=_t("common.help.tooltip", "Довідка"), on_click=_save_dlg_help),
    ], vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True)
    sdlg.content = ft.Container(width=320, padding=16, content=name_f)
    sdlg.actions = [
        ft.TextButton(_t("common.cancel", "Скасувати"), on_click=lambda _: page.pop_dialog()),
        ft.FilledButton(_t("common.save", "Зберегти"), on_click=_do),
    ]
    sdlg.actions_alignment = ft.MainAxisAlignment.END
    name_f.on_submit = _do
    page.open(sdlg)


# ── Shared widgets ─────────────────────────────────────────────────────────
def _field(
    label: str, width=180, value="", number=True, optional=False
) -> ft.TextField:
    lbl = label + (" (необ.)" if optional else "")
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
    options = [ft.dropdown.Option(key="", text="— оберіть —")] + [
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

    def __init__(self):
        self.title = None
        self.content = None
        self.actions: list = []
        self.actions_alignment = None
        self._panel: ft.Container | None = None
        # Set to the exact outer pixel width of dlg.content so the title bar
        # can be stretched to the same width and expand=True on the title text works.
        self.panel_width: int | None = None

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
            body.append(self.content)
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
        tooltip="Клацніть кнопку калькулятора щоб активувати клавіатуру",
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
            page.on_keyboard_event = None
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
            val_text.value = "Помилка"
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

    dlg.title = ft.Row(
        [
            ft.Text("Звичайний", weight="bold", size=12, expand=True,
                    no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
            _kb_dot,
            ft.IconButton(ft.Icons.BOOKMARK_ADD_OUTLINED, tooltip="Зберегти",
                          on_click=_do_save, icon_size=18),
            *(
                [ft.IconButton(ft.Icons.ARROW_BACK, tooltip="Назад",
                               on_click=_go_back, icon_size=16)]
                if back_fn else []
            ),
            ft.IconButton(ft.Icons.CLOSE, on_click=_close, icon_size=16),
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

_LEFT_W   = 290   # left panel container width
_INP_W    = 274   # inputs column width
_RIGHT_W  = 330   # right panel container width
_RES_W    = 314   # results column width
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


def _cost_card(
    method_name: str,
    rows: list,           # list of (label, value, color, bold)
    *,
    highlight: bool = False,
) -> ft.Container:
    row_widgets = [
        ft.Row(
            [
                ft.Text(lbl, size=11, color=_MUTED, expand=True),
                ft.Text(val, size=11, color=clr,
                        weight=ft.FontWeight.W_700 if bld else None),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        )
        for lbl, val, clr, bld in rows
    ]
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


def _open_cost(
    page,
    db,
    manager_id: str,
    company_id: str,
    region_id: str,
    regions_list: list[str],
    prefill: Optional[dict] = None,
    back_fn=None,
):
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
    price_f    = _field("Ціна закупівлі *",  width=_PR_W,  value=pf.get("purchase_price", ""))
    pkg_f      = _field("Ціна упаковки",     width=_PR_W,  value=pf.get("pkg_price", ""), optional=True)
    weight_f   = _field("Вага, кг *",        width=_INP_W, value=pf.get("weight", ""))
    rent_f     = _field("Рентабельність, %", width=_INP_W, value=pf.get("rent_pct", ""), optional=True)
    buy_ccy_dd  = _dd("Валюта",  ccy_opts0, value=pf_buy_ccy,  width=_CCY_W)
    pkg_ccy_dd  = _dd("Валюта",  ccy_opts0, value=pf_pkg_ccy,  width=_CCY_W)
    cost_ccy_dd = _dd("Валюта С/В", ccy_opts0, value=pf_cost_ccy, width=_INP_W)

    # ── Dynamic sections ───────────────────────────────────────────────────────
    methods_col = ft.Column([], spacing=6, tight=True)
    tiers_col   = ft.Column([], spacing=4, tight=True)
    method_widgets: dict[str, dict] = {}
    tier_widgets:   dict[str, dict] = {}

    def _rebuild_methods(methods: list[dict], pf_methods: dict, dm_overrides: Optional[dict] = None):
        method_widgets.clear()
        rows = []
        if not methods:
            rows.append(ft.Text("Типи доставки не налаштовані для цього регіону",
                                size=11, color=_MUTED, italic=True))
        for m in methods:
            mid   = str(m.get("id") or "")
            mname = str(m.get("name") or mid)
            eff   = m.get("effective_price_per_kg") or m.get("base_price_per_kg") or 0
            mccy  = str(m.get("effective_price_currency") or m.get("price_currency") or "USD").upper()
            pf_m  = (pf_methods or {}).get(mid) or {}
            ov_entry = (dm_overrides or {}).get(mid)
            if isinstance(ov_entry, dict) and "enabled" in ov_entry:
                cb_val = bool(ov_entry["enabled"])
            elif "checked" in pf_m:
                cb_val = bool(pf_m["checked"])
            else:
                cb_val = bool(m.get("user_enabled", True))

            def _on_cb(ev, method_id=mid):
                cur_ov = dm_overrides_state[0]
                if method_id not in cur_ov or not isinstance(cur_ov[method_id], dict):
                    cur_ov[method_id] = {}
                cur_ov[method_id]["enabled"] = bool(ev.control.value)
                db.save_manager_delivery_overrides(manager_id, cur_ov, region_id=cur_uuid[0])

            cb = ft.Checkbox(
                label=mname, value=cb_val, on_change=_on_cb,
                label_style=ft.TextStyle(size=12),
            )
            tariff_f = ft.TextField(
                label="тариф/кг",
                value=str(pf_m.get("tariff") or eff or ""),
                width=82, border_radius=6,
                keyboard_type=ft.KeyboardType.NUMBER, dense=True,
                content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
                label_style=ft.TextStyle(size=10),
                text_style=ft.TextStyle(size=12),
            )
            method_widgets[mid] = {"name": mname, "ccy": mccy, "cb": cb, "tariff_f": tariff_f}
            rows.append(ft.Row(
                [
                    ft.Container(content=cb, expand=True),
                    tariff_f,
                    ft.Text(mccy, size=9, color=_MUTED, width=28),
                ],
                spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ))
        methods_col.controls = rows

    def _rebuild_tiers(tiers: list[dict], pf_tiers: dict):
        tier_widgets.clear()
        rows = []
        if not tiers:
            rows.append(ft.Text("Ціни продажу не налаштовані для цього регіону",
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
                new_opts = [ft.dropdown.Option(key="", text="— оберіть —")] + [
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
                            f"⚠ Помилка перемикання регіону: {rid_name}",
                            color=_ERR_FG, size=12,
                        ),
                    )
                ]
            with contextlib.suppress(Exception):
                page.update()

        region_dd = _dd("Регіон", [(r, r) for r in regions_list],
                        value=cur_region[0], width=_INP_W)
        region_dd.on_select = _on_region_change
        region_section = [_section("Регіон"), region_dd]

    # ── Inputs column ──────────────────────────────────────────────────────────
    inputs_col = ft.Column(
        [
            *region_section,
            _section("Закупівля"),
            ft.Row([price_f, buy_ccy_dd], spacing=4),
            ft.Row([pkg_f,   pkg_ccy_dd], spacing=4),
            weight_f,
            _section("Валюта собівартості"),
            cost_ccy_dd,
            _section("Типи доставки  (✓ — обрати, тариф — редагується)"),
            ft.Container(content=methods_col),
            _section("Рентабельність (необов'яз.)"),
            rent_f,
            _section("Ціни продажу (необов'яз.)"),
            ft.Container(content=tiers_col),
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

    # ── Dialog ────────────────────────────────────────────────────────────────
    dlg = _FloatDialog()

    def _close(_=None):
        with contextlib.suppress(Exception):
            _fdlg_close(page, dlg)

    async def _go_back(_=None):
        _close()
        if back_fn:
            await asyncio.sleep(0.05)
            with contextlib.suppress(Exception):
                back_fn()

    # ── Calculate ─────────────────────────────────────────────────────────────
    def _calculate(_=None):
        rates = _fetch_uah_rates()
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
            _err("Введіть ціну закупівлі")
            return
        if weight is None:
            _err("Введіть вагу")
            return

        selected = [(mid, w) for mid, w in method_widgets.items() if w["cb"].value]
        if not selected:
            _err("Оберіть хоча б один тип доставки")
            return

        buy_in_cv = _convert(purchase_price, buy_ccy, cost_ccy, rates)
        if buy_in_cv is None:
            detail = f" ({'; '.join(_FX_LAST_ERRORS)})" if _FX_LAST_ERRORS else " (курси ще не завантажені — спробуйте ще раз за кілька секунд)"
            _err(f"Немає курсу {buy_ccy} → {cost_ccy}{detail}")
            return
        pkg_in_cv  = (_convert(pkg_price, pkg_ccy, cost_ccy, rates) or 0.0) if pkg_price else 0.0
        base_cost  = buy_in_cv + pkg_in_cv
        has_rent   = rent_pct is not None and 0 < rent_pct < 100

        tier_vals: list[tuple[str, str, float]] = []
        for tid, tw in tier_widgets.items():
            v = _safe_float(tw["field"].value)
            if v is not None and v > 0:
                tier_vals.append((tw["name"], tw["ccy"], v))

        extra_ccys: set[str] = {buy_ccy, pkg_ccy}
        cards = []

        for mid, mw in selected:
            tariff      = _safe_float(mw["tariff_f"].value) or 0.0
            mccy        = mw["ccy"]
            delivery_cv = _convert(weight * tariff, mccy, cost_ccy, rates) or (weight * tariff)
            extra_ccys.add(mccy)
            cv = base_cost + delivery_cv

            card_rows: list[tuple[str, str, str, bool]] = [
                ("Ціна закупівлі:", _fmt_money(buy_in_cv, cost_ccy), _DARK, False),
            ]
            if pkg_in_cv:
                card_rows.append(("Упаковка:", _fmt_money(pkg_in_cv, cost_ccy), _MUTED, False))
            card_rows.append(
                (f"Доставка ({weight} кг × {tariff} {mccy}/кг):",
                 _fmt_money(delivery_cv, cost_ccy), _MUTED, False)
            )
            card_rows.append(("Собівартість:", _fmt_money(cv, cost_ccy), _C_OK, True))

            if has_rent:
                sale = cv / (1.0 - rent_pct / 100.0)
                card_rows.append(
                    (f"Ціна при рент. {rent_pct:.0f}%:", _fmt_money(sale, cost_ccy), _C_BLUE, True)
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
                     f"{r_pct:.1f}%", clr, False)
                )

            cards.append(_cost_card(mw["name"], card_rows, highlight=len(selected) == 1))

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
                        ft.Text("Результати розрахунку", size=12,
                                weight=ft.FontWeight.W_700, color=_DARK),
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

    def _do_save(_=None):
        data = {
            "region_id":    cur_region[0],
            "purchase_price": price_f.value,
            "purchase_ccy": buy_ccy_dd.value,
            "pkg_price":    pkg_f.value,
            "pkg_ccy":      pkg_ccy_dd.value,
            "cost_ccy":     cost_ccy_dd.value,
            "weight":       weight_f.value,
            "rent_pct":     rent_f.value,
            "methods": {
                mid: {"checked": w["cb"].value, "tariff": w["tariff_f"].value}
                for mid, w in method_widgets.items()
            },
            "tiers": {tid: tw["field"].value for tid, tw in tier_widgets.items()},
        }
        _show_save_dialog(page, db, manager_id, "cost", data)

    # ── Dialog title ──────────────────────────────────────────────────────────
    dlg.title = ft.Row(
        [
            ft.Text("Собівартість", weight="bold", size=13, expand=True),
            ft.IconButton(ft.Icons.BOOKMARK_ADD_OUTLINED, tooltip="Зберегти",
                          on_click=_do_save, icon_size=18),
            *(
                [ft.IconButton(ft.Icons.ARROW_BACK, tooltip="Назад",
                               on_click=_go_back, icon_size=16)]
                if back_fn else []
            ),
            ft.IconButton(ft.Icons.CLOSE, on_click=_close, icon_size=16),
        ]
    )

    _dlg_w = _LEFT_W + 21 + _RIGHT_W + 32  # 290+21+420+32 = 763
    dlg.panel_width = _dlg_w
    dlg.content = ft.Container(
        padding=ft.padding.symmetric(horizontal=16, vertical=8),
        content=ft.Row(
            [
                # Left: inputs + pinned button
                ft.Column(
                    [
                        ft.Container(
                            width=_LEFT_W,
                            height=_PANEL_H - 48,
                            content=inputs_col,
                        ),
                        ft.FilledButton(
                            "  Розрахувати",
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
                    height=_PANEL_H,
                    content=result_col,
                ),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.START,
        ),
    )
    _fdlg_open(page, dlg)


# ─────────────────────────────────────────────────────────────────────────────
# ── 3. Рентабельність ─────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
def _open_profit(
    page,
    db,
    manager_id: str,
    company_id: str,
    region_id: str,
    regions_list: list[str],
    prefill: Optional[dict] = None,
    back_fn=None,
):
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

    def _all_ccys(tiers) -> list[str]:
        s: set[str] = {"USD", "UAH", "EUR", "PLN", "CNY"}
        for t in tiers:
            s.add(str(t.get("currency") or "USD").upper())
        return sorted(s)

    ccy_opts0 = [(c, c) for c in _all_ccys(tiers0)]
    _PL = 280   # left column width
    _PR = 240   # right column width
    cost_f  = _field("С/В *",           width=160, value=pf.get("cost", ""))
    cost_dd = _dd("Валюта", ccy_opts0, value=str(pf.get("cost_ccy") or "USD"), width=114)
    rent_f  = _field("Рентабельність, %", width=_PL - 4, value=pf.get("rent_pct", ""), optional=True)

    tiers_col = ft.Column([], spacing=4)
    result_col = ft.Column([], spacing=4)
    rates_row = ft.Row([], wrap=True, spacing=6)
    tier_widgets: dict[str, dict] = {}

    def _rebuild_tiers(tiers: list[dict], pf_tiers: dict):
        tier_widgets.clear()
        rows = []
        if not tiers:
            rows.append(
                ft.Text(
                    "Ціни продажу не налаштовані для цього регіону",
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

    _rebuild_tiers(tiers0, pf.get("tiers") or {})

    def _on_region_change(e):
        rid = str(e.control.value or "").strip()
        if not rid:
            return
        try:
            cur_region[0] = rid
            new_tiers = _load_tiers(rid)
            new_ccys = _all_ccys(new_tiers)
            new_opts = [ft.dropdown.Option(key="", text="— оберіть —")] + [
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
                        f"⚠ Помилка перемикання регіону: {rid}",
                        color=_ERR_FG, size=12,
                    ),
                )
            ]
        with contextlib.suppress(Exception):
            page.update()

    region_section: list = []
    if len(regions_list) > 1:
        region_dd = _dd(
            "Регіон", [(r, r) for r in regions_list], value=cur_region[0], width=200
        )
        region_dd.on_select = _on_region_change
        region_section = [_section("Регіон"), region_dd]

    dlg = _FloatDialog()

    def _close(_=None):
        with contextlib.suppress(Exception):
            _fdlg_close(page, dlg)

    async def _go_back(_=None):
        _close()
        if back_fn:
            await asyncio.sleep(0.05)
            with contextlib.suppress(Exception):
                back_fn()

    def _calculate(_=None):
        rates = _fetch_uah_rates()
        cv = _safe_float(cost_f.value)
        cccy = str(cost_dd.value or "USD").upper()
        rpct = _safe_float(rent_f.value)
        if cv is None:
            result_col.controls = [
                ft.Container(
                    bgcolor="#FEF3C7", border=ft.border.all(1, "#F59E0B"),
                    border_radius=8, padding=ft.padding.all(12),
                    content=ft.Text("⚠  Введіть С/В", color=_C_WARN, size=12),
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

        extra_ccys: set[str] = set()
        card_rows: list[tuple[str, str, str, bool]] = [
            (f"С/В ({cccy}):", _fmt_money(cv, cccy), _C_OK, True),
        ]
        if has_rent:
            sale = cv / (1.0 - rpct / 100.0)
            card_rows.append(
                (f"Ціна при рент. {rpct:.0f}%:", _fmt_money(sale, cccy), _C_BLUE, True)
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
                (f"{tnm} ({price_label}):", f"{r_pct:.1f}%", clr, False)
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
                        ft.Text("Результати розрахунку", size=12,
                                weight=ft.FontWeight.W_700, color=_DARK),
                        *(
                            [ft.Text(rate_str, size=9, color=_MUTED)]
                            if rate_str else []
                        ),
                    ],
                    spacing=2, tight=True,
                ),
            ),
            _cost_card("Рентабельність", card_rows),
        ]
        rates_row.controls = []
        with contextlib.suppress(Exception):
            page.update()

    def _do_save(_=None):
        data = {
            "region_id": cur_region[0],
            "cost": cost_f.value,
            "cost_ccy": cost_dd.value,
            "rent_pct": rent_f.value,
            "tiers": {tid: tw["field"].value for tid, tw in tier_widgets.items()},
        }
        _show_save_dialog(page, db, manager_id, "profit", data)

    _PW = _PL + 1 + _PR + 32  # 280+1+240+32 = 553

    dlg.title = ft.Row(
        [
            ft.Text("Рентабельність", weight="bold", size=13, expand=True),
            ft.IconButton(ft.Icons.BOOKMARK_ADD_OUTLINED, tooltip="Зберегти",
                          on_click=_do_save, icon_size=18),
            *(
                [ft.IconButton(ft.Icons.ARROW_BACK, tooltip="Назад",
                               on_click=_go_back, icon_size=16)]
                if back_fn else []
            ),
            ft.IconButton(ft.Icons.CLOSE, on_click=_close, icon_size=16),
        ]
    )
    dlg.panel_width = _PW

    inputs_col = ft.Column(
        [
            *region_section,
            _section("С/В"),
            ft.Row([cost_f, cost_dd], spacing=6),
            _section("Рентабельність (необов'яз.)"),
            rent_f,
            _section("Ціни продажу (необов'яз.)"),
            tiers_col,
            ft.Container(height=4),
            ft.FilledButton(
                "  Розрахувати", on_click=_calculate, icon=ft.Icons.CALCULATE,
                width=_PL,
                style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
            ),
        ],
        spacing=4,
        scroll=ft.ScrollMode.AUTO,
        width=_PL,
    )

    dlg.content = ft.Container(
        padding=ft.padding.symmetric(horizontal=16, vertical=8),
        content=ft.Row(
            [
                ft.Container(content=inputs_col, width=_PL, height=_PANEL_H),
                ft.VerticalDivider(width=1, color=_BORDER),
                ft.Container(
                    content=ft.Column(
                        [result_col],
                        spacing=4,
                        scroll=ft.ScrollMode.AUTO,
                    ),
                    width=_PR,
                    height=_PANEL_H,
                ),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.START,
        ),
    )
    _fdlg_open(page, dlg)


# ─────────────────────────────────────────────────────────────────────────────
# ── 4. Обертаємість / Забезпеченість (combined) ───────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
def _open_turnover_coverage(page, db, manager_id: str, prefill: Optional[dict] = None, back_fn=None):
    pf = prefill or {}
    dlg = _FloatDialog()
    _FW = 336  # field width
    start_f = _field("Склад початок *", width=_FW, value=pf.get("start", ""))
    end_f = _field("Склад кінець *", width=_FW, value=pf.get("end", ""))
    days_f = _field("Кількість днів у періоді *", width=_FW, value=pf.get("days", ""))
    cost_f = _field("С/В реалізованого товару *", width=_FW, value=pf.get("cost", ""))

    turnover_text = ft.Text("—", size=20, weight="bold", color=_ACCENT)
    coverage_text = ft.Text("—", size=20, weight="bold", color=_ACCENT)

    def _close(_=None):
        with contextlib.suppress(Exception):
            _fdlg_close(page, dlg)

    async def _go_back(_=None):
        _close()
        if back_fn:
            await asyncio.sleep(0.05)
            with contextlib.suppress(Exception):
                back_fn()

    def _calculate(_=None):
        start = _safe_float(start_f.value)
        end = _safe_float(end_f.value)
        days = _safe_float(days_f.value)
        cv = _safe_float(cost_f.value)
        if None in (start, end, days, cv):
            turnover_text.value = "⚠ Заповніть усі поля"
            turnover_text.color = _WARN_FG
            coverage_text.value = ""
        elif cv == 0:
            turnover_text.value = "⚠ С/В = 0"
            turnover_text.color = _WARN_FG
            coverage_text.value = ""
        else:
            avg = (start + end) / 2.0
            turnover = (avg * days) / cv
            coverage = (end * days) / cv
            turnover_text.value = f"{_fmt_val(turnover, 1)} днів"
            turnover_text.color = _ACCENT
            coverage_text.value = f"{_fmt_val(coverage, 1)} днів"
            coverage_text.color = _ACCENT
        with contextlib.suppress(Exception):
            page.update()

    def _do_save(_=None):
        _show_save_dialog(
            page, db, manager_id, "turnover",
            {"start": start_f.value, "end": end_f.value,
             "days": days_f.value, "cost": cost_f.value},
        )

    dlg.title = ft.Row(
        [
            ft.Text("Оборот / Забезпеченість", weight="bold", size=13, expand=True),
            ft.IconButton(ft.Icons.BOOKMARK_ADD_OUTLINED, tooltip="Зберегти",
                          on_click=_do_save, icon_size=18),
            *(
                [ft.IconButton(ft.Icons.ARROW_BACK, tooltip="Назад",
                               on_click=_go_back, icon_size=16)]
                if back_fn else []
            ),
            ft.IconButton(ft.Icons.CLOSE, on_click=_close, icon_size=16),
        ]
    )
    _PW = 400
    _IW = _PW - 32
    dlg.panel_width = _PW
    dlg.content = ft.Container(
        width=_PW,
        padding=ft.padding.symmetric(horizontal=16, vertical=10),
        content=ft.Column(
            [
                ft.Container(
                    content=ft.Column([
                        ft.Text(
                            "Обертаємість = ((Поч + Кін) / 2 × Дні) / С/В",
                            size=9, color=_MUTED, italic=True,
                        ),
                        ft.Text(
                            "Забезпеченість = (Кін × Дні) / С/В",
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
                    "  Розрахувати", on_click=_calculate, icon=ft.Icons.CALCULATE,
                    width=_IW,
                    style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
                ),
                ft.Divider(height=6, color=_BORDER),
                ft.Container(
                    content=ft.Column([
                        ft.Row([
                            ft.Text("Обертаємість:", size=12, color=_MUTED, width=130),
                            turnover_text,
                        ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        ft.Row([
                            ft.Text("Забезпеченість:", size=12, color=_MUTED, width=130),
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
    _fdlg_open(page, dlg)


# ─────────────────────────────────────────────────────────────────────────────
# ── 6. Збережені ──────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
def _open_saved(
    page, db, manager_id: str, company_id: str, region_id: str, regions_list: list[str],
    back_fn=None,
):
    dlg = _FloatDialog()
    list_col = ft.Column([], spacing=4, scroll=ft.ScrollMode.AUTO)

    filter_cbs: dict[str, ft.Checkbox] = {
        k: ft.Checkbox(label=v, value=True) for k, v in _CALC_TYPES
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
                ft.Text("Немає збережених розрахунків", size=11, color=_MUTED)
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
                                    str(s.get("calc_label") or ""),
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
                                        "Відкрити",
                                        on_click=lambda _, e=entry: _open_entry(e),
                                    ),
                                    ft.IconButton(
                                        ft.Icons.DELETE_OUTLINE,
                                        icon_size=16,
                                        icon_color=_ERR_FG,
                                        tooltip="Видалити",
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

    for cb in filter_cbs.values():
        cb.on_change = _refresh_list

    dlg.title = ft.Row(
        [
            ft.Text("Збережені розрахунки", weight="bold", size=13, expand=True),
            *(
                [ft.IconButton(ft.Icons.ARROW_BACK, tooltip="Назад",
                               on_click=_go_back, icon_size=16)]
                if back_fn else []
            ),
            ft.IconButton(ft.Icons.CLOSE, on_click=_close, icon_size=16),
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
    regions_list: Optional[list[str]] = None,
    *,
    prefill_type: Optional[str] = None,
    prefill_data: Optional[dict] = None,
):
    """Open the main calculators picker dialog."""
    _t = i18n.bind(str(manager_id or ""))
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
        "profit": ft.Icons.TRENDING_UP,
        "turnover": ft.Icons.LOOP,
        "saved": ft.Icons.BOOKMARK,
    }
    _calc_labels = {
        "basic": _t("calc.type.basic", "Звичайний"),
        "cost": _t("calc.type.cost", "Собівартість"),
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

    dlg.title = ft.Row(
        [
            ft.Text(_t("calc.picker_title", "Калькулятори"), weight="bold", expand=True),
            ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=_t("common.help.tooltip", "Довідка"), on_click=_picker_help),
            ft.IconButton(ft.Icons.CLOSE, on_click=_close, icon_size=18),
        ]
    )
    dlg.panel_width = 420
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
    elif key == "profit":
        _open_profit(
            page, db, manager_id, company_id, region_id, regions_list, prefill=data, back_fn=_back
        )
    elif key in ("turnover", "coverage"):
        _open_turnover_coverage(page, db, manager_id, prefill=data, back_fn=_back)
    elif key == "saved":
        _open_saved(page, db, manager_id, company_id, region_id, regions_list, back_fn=_back)
