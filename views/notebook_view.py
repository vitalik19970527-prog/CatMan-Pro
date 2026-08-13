"""
views/notebook_view.py — per-user dated task diary, opens as a modal dialog.
"""

from __future__ import annotations

import calendar
import contextlib
import json
import uuid
from datetime import date, timedelta

import flet as ft

import i18n
from logger import logger
from views import themes as _themes

_PREF_KEY = "notebook_diary_v1"

_UA_MONTHS = [
    "Січень",
    "Лютий",
    "Березень",
    "Квітень",
    "Травень",
    "Червень",
    "Липень",
    "Серпень",
    "Вересень",
    "Жовтень",
    "Листопад",
    "Грудень",
]
_UA_MONTHS_GEN = [
    "січня",
    "лютого",
    "березня",
    "квітня",
    "травня",
    "червня",
    "липня",
    "серпня",
    "вересня",
    "жовтня",
    "листопада",
    "грудня",
]
_UA_WD_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
_UA_WD_LONG = [
    "Понеділок",
    "Вівторок",
    "Середа",
    "Четвер",
    "П'ятниця",
    "Субота",
    "Неділя",
]

# Font sizes (+1 px from original)
_SZ_DATE_HDR = 16
_SZ_SECTION = 11
_SZ_TASK = 13
_SZ_INPUT = 13
_SZ_SAVE = 11
_SZ_CAL_MTH = 12
_SZ_CAL_WD = 10
_SZ_CAL_DAY = 10
_SZ_RECENT = 11

# Calendar cell size
_CAL_W = 27
_CAL_H = 27

# Spiral binding strip width
_RING_W = 24

# Dialog dimensions
_DLG_W = 860
_DLG_H = 520
_LEFT_W = 215


def _load(db, mid: str) -> dict:
    try:
        raw = db.get_manager_ui_preference(mid, _PREF_KEY, "{}")
        result = json.loads(raw) if raw else {}
        logger.debug("[notebook] _load mid=%r keys=%s", mid, list(result.keys())[:5])
        return result
    except Exception:
        logger.exception("[notebook] _load FAILED mid=%r", mid)
        return {}


def _persist(db, mid: str, diary: dict) -> None:
    try:
        db.set_manager_ui_preference(
            mid, _PREF_KEY, json.dumps(diary, ensure_ascii=False)
        )
    except Exception:
        pass


def _dk(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _has_content(entry: dict) -> bool:
    return bool((entry.get("tasks") or []) or (entry.get("notes") or "").strip())


# ── recurring tasks: date math + generation ─────────────────────────────────
# Recurring tasks are stored in the same per-manager JSON blob: a new
# diary["_series"] dict (series_id -> {text, recur, disabled_from}) plus a
# "series_id" tag on every generated task instance. Order-schedule-linked
# tasks use a separate "order_task_id" tag and their own (unbounded, no
# 6-month cap) generation logic further below — they're intentionally not
# part of "_series".


def _add_months_keep_day(d: date, months: int) -> date:
    """Add whole months to d, preserving day-of-month (clamped to the target
    month's last day, e.g. 31.01 + 1 month -> 28/29.02)."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day))


def _advance_recur_date(d: date, recur: str) -> date:
    if recur == "weekly":
        return d + timedelta(days=7)
    if recur == "monthly":
        return _add_months_keep_day(d, 1)
    return d + timedelta(days=1)  # "daily" and any unrecognized value


def _series_dates(diary: dict, sid: str) -> list[date]:
    out = []
    for dk, entry in diary.items():
        if dk == "_series":
            continue
        if any(t.get("series_id") == sid for t in (entry.get("tasks") or [])):
            try:
                out.append(date.fromisoformat(dk))
            except Exception:
                continue
    return sorted(out)


def _generate_recurring(diary: dict, sid: str) -> bool:
    """Extend one series forward, from its latest existing instance, up to
    today + 6 months. Idempotent — a no-op once already extended that far.
    Returns True if any new instance was added (caller should persist)."""
    series = (diary.get("_series") or {}).get(sid)
    if not series or series.get("disabled_from"):
        return False
    recur = series.get("recur") or "daily"
    text = series.get("text") or ""
    dates = _series_dates(diary, sid)
    if not dates:
        return False
    horizon = _add_months_keep_day(date.today(), 6)
    changed = False
    cur = _advance_recur_date(dates[-1], recur)
    guard = 0  # safety cap — never loop more than ~a few years' worth
    while cur <= horizon and guard < 1000:
        entry = diary.setdefault(_dk(cur), {"tasks": [], "notes": ""})
        entry.setdefault("tasks", []).append(
            {"id": str(uuid.uuid4()), "text": text, "done": False, "series_id": sid}
        )
        changed = True
        cur = _advance_recur_date(cur, recur)
        guard += 1
    return changed


def _generate_all_recurring(diary: dict) -> bool:
    changed = False
    for sid in list((diary.get("_series") or {}).keys()):
        if _generate_recurring(diary, sid):
            changed = True
    return changed


def _remove_series_after(diary: dict, sid: str, from_date: date) -> None:
    """Delete every instance of this series dated strictly after from_date."""
    for dk in list(diary.keys()):
        if dk == "_series":
            continue
        try:
            d = date.fromisoformat(dk)
        except Exception:
            continue
        if d <= from_date:
            continue
        entry = diary.get(dk) or {}
        tasks = entry.get("tasks") or []
        new_tasks = [t for t in tasks if t.get("series_id") != sid]
        if len(new_tasks) != len(tasks):
            entry["tasks"] = new_tasks


def _create_series(diary: dict, task: dict, freq: str, instance_date: date) -> str:
    """First time a task is made recurring — new series, generate forward
    from this instance's own date."""
    sid = str(uuid.uuid4())
    diary.setdefault("_series", {})[sid] = {
        "text": task.get("text") or "",
        "recur": freq,
        "disabled_from": None,
    }
    task["series_id"] = sid
    _generate_recurring(diary, sid)
    return sid


def _enable_or_change_series(
    diary: dict, sid: str, freq: str, clicked_date: date
) -> None:
    """Pick a frequency on a task that already has a series_id — two cases:
    - series is currently DISABLED (re-enabling): nothing exists after its
      disabled_from cutoff already, so just clear the cutoff and resume
      generating forward from the latest existing instance — no rebase.
    - series is currently ACTIVE (changing daily<->weekly<->monthly): rebase
      from the clicked instance forward — drop everything after it and
      regenerate at the new cadence, since there's no disabled gap to
      preserve here."""
    series = diary.setdefault("_series", {}).setdefault(
        sid, {"text": "", "recur": freq, "disabled_from": None}
    )
    was_disabled = bool(series.get("disabled_from"))
    series["recur"] = freq
    series["disabled_from"] = None
    if not was_disabled:
        _remove_series_after(diary, sid, clicked_date)
    _generate_recurring(diary, sid)


def _disable_series(diary: dict, sid: str, from_date: date) -> None:
    """Turn recurrence off as of from_date: everything up to and including
    from_date stays untouched; everything after is deleted; nothing more
    generates until re-enabled."""
    series = (diary.get("_series") or {}).get(sid)
    if not series:
        return
    series["disabled_from"] = _dk(from_date)
    _remove_series_after(diary, sid, from_date)


# ── order-schedule-linked tasks: unbounded daily copy until fulfilled ───────


def _format_order_task_text(ot: dict, name_by_group: dict) -> str:
    name = name_by_group.get(str(ot.get("group_id") or ""), "?")
    qty = ot.get("planned_qty")
    qty_s = ""
    if qty not in (None, ""):
        try:
            qty_s = str(int(round(float(qty))))
        except Exception:
            qty_s = str(qty)
    delivery = str(ot.get("delivery_type_name") or "").strip()
    tail = " ".join(x for x in (f"{qty_s}шт" if qty_s else "", delivery) if x)
    return f"Замовити {name} - {tail}" if tail else f"Замовити {name}"


def _sync_order_schedule_tasks(db, diary: dict, company_id: str, mid: str) -> bool:
    """Two-way sync with 'Розклад замовлень', across ALL of the user's regions
    (the notebook is per-user, not per-region — it must not depend on
    whatever region happens to be selected on the dashboard right now):
    - not-yet-ordered tasks whose window has started get a fresh copy added
      to TODAY's entry (once per day; old days' copies are never touched, so
      they keep accumulating until the order is placed).
    - tasks already marked "ordered" (whether from here or from Order
      Schedule directly) get every still-open linked instance, on every
      date, crossed off at once."""
    if not company_id:
        return False
    try:
        # Archived tasks still exist (just parked) and must not be treated as
        # deleted below, even though they're excluded from the main loop so
        # they don't keep generating fresh reminders. `tasks` (non-archived)
        # is always a subset of `all_tasks` — one query + a Python filter
        # instead of two identical round trips that only differ by an
        # `archived=FALSE` clause.
        all_tasks = (
            db.get_order_schedule_tasks(company_id, mid, include_archived=True) or []
        )
        tasks = [t for t in all_tasks if isinstance(t, dict) and not t.get("archived")]
        groups = db.get_order_schedule_groups(company_id, mid) or []
    except Exception:
        logger.exception("[notebook] order-schedule sync fetch failed")
        return False

    name_by_group: dict[str, str] = {}
    for g in groups:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id") or "")
        if not gid:
            continue
        label = str(g.get("display_label") or "").strip()
        if not label:
            names = g.get("product_names") or []
            label = str(names[0]) if names else "?"
        name_by_group[gid] = label

    today = date.today()
    changed = False
    existing_oids: set[str] = {
        str(ot.get("id") or "")
        for ot in all_tasks
        if isinstance(ot, dict) and ot.get("id")
    }
    for ot in tasks:
        if not isinstance(ot, dict):
            continue
        oid = str(ot.get("id") or "")
        if not oid:
            continue
        status = str(ot.get("status") or "not_ordered").strip()
        if status == "ordered":
            for dk, entry in diary.items():
                if dk == "_series":
                    continue
                for t in entry.get("tasks") or []:
                    if t.get("order_task_id") == oid and not t.get("done"):
                        t["done"] = True
                        changed = True
            continue

        start_raw = ot.get("date_from") or ot.get("month_anchor")
        start_d = None
        if start_raw:
            try:
                start_d = (
                    start_raw
                    if isinstance(start_raw, date)
                    else date.fromisoformat(str(start_raw)[:10])
                )
            except Exception:
                start_d = None
        if start_d is None or start_d > today:
            continue
        today_entry = diary.setdefault(_dk(today), {"tasks": [], "notes": ""})
        if any(t.get("order_task_id") == oid for t in today_entry.get("tasks") or []):
            continue
        today_entry.setdefault("tasks", []).append(
            {
                "id": str(uuid.uuid4()),
                "text": _format_order_task_text(ot, name_by_group),
                "done": False,
                "order_task_id": oid,
            }
        )
        changed = True

    # Schedule tasks that were deleted outright (not marked "ordered", just
    # removed) no longer show up in `tasks` at all, so the loop above never
    # touches their linked notebook entries — cross those off too, otherwise
    # they nag forever with no way to resolve them from Order Schedule.
    for dk, entry in diary.items():
        if dk == "_series":
            continue
        for t in entry.get("tasks") or []:
            oid_link = t.get("order_task_id")
            if oid_link and oid_link not in existing_oids and not t.get("done"):
                t["done"] = True
                changed = True

    return changed


def _lazy_sync(db, mid: str, company_id: str = "") -> dict:
    """Load the diary and bring it up to date: extend recurring series
    forward, and (when company context is available) sync order-schedule
    tasks both ways. Persists only if something actually changed."""
    diary = _load(db, mid)
    changed = _generate_all_recurring(diary)
    if company_id and _sync_order_schedule_tasks(db, diary, company_id, mid):
        changed = True
    if changed:
        _persist(db, mid, diary)
    return diary


def get_notebook_badge_counts(db, mid: str, company_id: str = "") -> tuple[int, int]:
    """Return (today_undone, overdue_undone) counts for the toolbar badge."""
    diary = _lazy_sync(db, str(mid or "").strip(), company_id)
    today = date.today()
    today_key = _dk(today)
    today_count = 0
    overdue_count = 0
    for dk, entry in diary.items():
        if dk == "_series":
            continue
        tasks = entry.get("tasks") or []
        undone = sum(1 for t in tasks if not t.get("done"))
        if dk == today_key:
            today_count = undone
        else:
            try:
                d = date.fromisoformat(dk)
            except Exception:
                continue
            if d < today:
                overdue_count += undone
    return today_count, overdue_count


def _is_light_color(hex_color: str) -> bool:
    c = str(hex_color or "").lstrip("#")
    if len(c) < 6:
        return True
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
        return (r * 299 + g * 587 + b * 114) / 1000 > 128
    except Exception:
        return True


def open_notebook_modal(
    page, db, manager_id: str, on_close=None, company_id: str = ""
) -> None:
    """Open the per-user dated task diary as a modal dialog."""
    _mid = str(manager_id or "").strip()
    _cid = str(company_id or "").strip()
    _t = i18n.bind(_mid)
    # Translated month/weekday lists
    _months = [_t(f"orders.month.{i}", v) for i, v in enumerate(_UA_MONTHS, 1)]
    _months_gen = [
        _t(f"notebook.month_gen.{i}", v) for i, v in enumerate(_UA_MONTHS_GEN, 1)
    ]
    _wd_short = [_t(f"media.day.{i}", v) for i, v in enumerate(_UA_WD_SHORT)]
    _wd_long = [_t(f"notebook.day_long.{i}", v) for i, v in enumerate(_UA_WD_LONG)]
    th = _themes.get_active_theme(_mid)
    bg = th["bg"]
    panel = th["panel"]
    hdr = th["hdr"]
    border = th["border"]
    tc = th["text"]
    mc = th["muted"]
    accent = th["accent"]
    atxt = th.get("accent_text", "#FFFFFF")
    soft = th["soft"]
    ok_clr = th["ok"]
    radius = th.get("radius", 8)

    _light = _is_light_color(panel)
    paper_bg = "#FFFEF5" if _light else panel
    rule_clr = "#EBE8D5" if _light else border
    margin_clr = "#FFCDD2" if _light else border
    cover_bg = "#E8E0D5" if _light else hdr
    ring_bg = "#C8BEB0" if _light else border
    ring_hole = "#FFFEF5" if _light else bg

    diary: dict = _lazy_sync(db, _mid, _cid)
    st = {"sel": date.today(), "vm": date.today().replace(day=1)}
    edit_st = {"id": None}  # task id currently being edited
    _batch = [False]  # when True, individual control.update() calls are skipped

    # ── shared widget refs ─────────────────────────────────────────────────
    cal_grid = ft.Column(spacing=2, tight=True)
    recent_col = ft.Column(spacing=2, tight=True)
    date_hdr = ft.Text("", size=_SZ_DATE_HDR, weight=ft.FontWeight.W_700, color=tc)
    tasks_col = ft.Column(
        spacing=4,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )
    save_lbl = ft.Text("", size=_SZ_SAVE, color=ok_clr)

    new_task_tf = ft.TextField(
        hint_text=_t("notebook.task_hint", "Нова задача… Enter — додати"),
        border_radius=radius,
        border_color=border,
        focused_border_color=accent,
        text_style=ft.TextStyle(color=tc, size=_SZ_INPUT),
        hint_style=ft.TextStyle(color=mc, size=_SZ_INPUT),
        bgcolor=panel,
        dense=True,
        height=36,
        content_padding=ft.padding.symmetric(horizontal=10, vertical=6),
    )
    notes_tf = ft.TextField(
        hint_text=_t("notebook.notes_hint", "Вільні нотатки…"),
        multiline=True,
        min_lines=3,
        max_lines=6,
        border_radius=radius,
        border_color=border,
        focused_border_color=accent,
        text_style=ft.TextStyle(color=tc, size=_SZ_INPUT),
        hint_style=ft.TextStyle(color=mc, size=_SZ_INPUT),
        bgcolor=panel,
        content_padding=ft.padding.symmetric(horizontal=10, vertical=8),
    )

    # _overlay_ref is filled after the overlay is built (forward reference)
    _overlay_ref: list = [None]
    # small paper-style popups (repeat-frequency picker, qty-confirm) stack
    # on top of the main overlay the same way — a second entry in page.overlay
    _popup_ref: list = [None]

    def _close(_=None):
        ov = _overlay_ref[0]
        if ov is not None and ov in page.overlay:
            with contextlib.suppress(Exception):
                page.overlay.remove(ov)
                page.update()
        if on_close is not None:
            with contextlib.suppress(Exception):
                on_close()

    def _show_notebook_help():
        from views.help_utils import show_modal_help

        show_modal_help(
            page,
            _t("notebook.title", "Мій блокнот"),
            [
                _t(
                    "notebook.help.desc",
                    "Особисті нотатки: довільний текст, організований у дерево розділів.",
                ),
                (
                    _t("notebook.help.steps_title", "Як користуватись"),
                    None,
                ),
                _t(
                    "notebook.help.step1",
                    "1. Ліворуч — дерево розділів; клікніть для вибору, «+» — додати підрозділ.",
                ),
                _t(
                    "notebook.help.step2",
                    "2. Праворуч — редактор тексту; введіть нотатки та натисніть «Зберегти».",
                ),
                _t(
                    "notebook.help.step3",
                    "3. Блокнот особистий — інші користувачі не бачать ваших нотаток.",
                ),
            ],
        )

    def _close_popup(_=None):
        p = _popup_ref[0]
        if p is not None and p in page.overlay:
            with contextlib.suppress(Exception):
                page.overlay.remove(p)
        _popup_ref[0] = None
        with contextlib.suppress(Exception):
            page.update()

    def _show_popup(card: ft.Container):
        _close_popup()
        popup = ft.Container(
            content=card,
            bgcolor="#40000000",
            alignment=ft.Alignment(0, 0),
            expand=True,
        )
        _popup_ref[0] = popup
        page.overlay.append(popup)
        page.update()

    def _entry() -> dict:
        return diary.setdefault(_dk(st["sel"]), {"tasks": [], "notes": ""})

    def _after_diary_change():
        """Common refresh after any diary mutation made outside the normal
        per-row handlers (repeat popup, qty-confirm popup)."""
        _persist(db, _mid, diary)
        _batch[0] = True
        try:
            _refresh_right()
            _refresh_cal()
            _refresh_recent()
        finally:
            _batch[0] = False
        with contextlib.suppress(Exception):
            page.update()

    # ── "Повторення" popup — pick daily/weekly/monthly or turn it off ──────
    def _open_repeat_popup(task: dict, instance_date: date):
        sid = task.get("series_id")
        series = (diary.get("_series") or {}).get(sid) if sid else None
        current_recur = series.get("recur") if series else None
        is_disabled = bool(series and series.get("disabled_from"))

        def _pick(freq):
            if not sid:
                _create_series(diary, task, freq, instance_date)
            else:
                _enable_or_change_series(diary, sid, freq, instance_date)
            _close_popup()
            _after_diary_change()

        def _turn_off(_=None):
            if sid:
                _disable_series(diary, sid, instance_date)
            _close_popup()
            _after_diary_change()

        def _opt_row(label, freq):
            is_current = bool(series) and current_recur == freq and not is_disabled
            return ft.Container(
                content=ft.Row(
                    [
                        ft.Icon(
                            (
                                ft.Icons.CHECK_CIRCLE
                                if is_current
                                else ft.Icons.CIRCLE_OUTLINED
                            ),
                            size=15,
                            color=accent if is_current else mc,
                        ),
                        ft.Text(
                            label,
                            size=13,
                            color=tc,
                            weight=ft.FontWeight.W_600 if is_current else None,
                        ),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
                border_radius=6,
                ink=True,
                on_click=lambda _, f=freq: _pick(f),
            )

        popup_card = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.REPLAY, size=15, color=accent),
                            ft.Text(
                                _t("notebook.repeat.title", "Повторення"),
                                size=13,
                                weight=ft.FontWeight.W_700,
                                color=tc,
                                expand=True,
                            ),
                            ft.IconButton(
                                ft.Icons.CLOSE,
                                icon_size=15,
                                icon_color=mc,
                                on_click=lambda _: _close_popup(),
                                style=ft.ButtonStyle(padding=ft.padding.all(2)),
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Divider(height=6, color=rule_clr),
                    _opt_row(_t("notebook.repeat.daily", "Щодня"), "daily"),
                    _opt_row(_t("notebook.repeat.weekly", "Щотижня"), "weekly"),
                    _opt_row(_t("notebook.repeat.monthly", "Щомісяця"), "monthly"),
                    ft.Divider(
                        height=6, color=rule_clr, visible=bool(sid) and not is_disabled
                    ),
                    ft.Container(
                        content=ft.Row(
                            [
                                ft.Icon(
                                    ft.Icons.NOT_INTERESTED,
                                    size=15,
                                    color=ft.Colors.RED_400,
                                ),
                                ft.Text(
                                    _t("notebook.repeat.off", "Не повторювати"),
                                    size=12,
                                    color=ft.Colors.RED_400,
                                ),
                            ],
                            spacing=8,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        padding=ft.padding.symmetric(horizontal=10, vertical=8),
                        border_radius=6,
                        ink=True,
                        on_click=_turn_off,
                        visible=bool(sid) and not is_disabled,
                    ),
                ],
                spacing=2,
                tight=True,
            ),
            bgcolor=paper_bg,
            border=ft.border.all(1, border),
            border_radius=radius,
            padding=ft.padding.symmetric(horizontal=6, vertical=8),
            width=230,
            shadow=ft.BoxShadow(spread_radius=1, blur_radius=18, color="#59000000"),
        )
        _show_popup(popup_card)

    # ── "Скільки штук фактично замовили?" popup — order-linked tasks only ──
    def _confirm_order_task(order_task_id: str, actual_qty: float):
        if not _cid:
            return
        try:
            rows = db.get_order_schedule_tasks(_cid, _mid, include_archived=True) or []
        except Exception:
            logger.exception("[notebook] could not reload order_schedule_tasks")
            return
        full = next(
            (r for r in rows if str(r.get("id") or "") == str(order_task_id)), None
        )
        if not full:
            return
        updated = dict(full)
        updated["actual_qty"] = actual_qty
        updated["status"] = "ordered"
        try:
            db.upsert_order_schedule_task(updated)
        except Exception:
            logger.exception(
                "[notebook] failed to upsert order_schedule_task %s", order_task_id
            )
            return
        for dk, entry in diary.items():
            if dk == "_series":
                continue
            for t in entry.get("tasks") or []:
                if t.get("order_task_id") == order_task_id:
                    t["done"] = True
        _after_diary_change()

    def _open_qty_popup(task: dict):
        oid = task.get("order_task_id")
        qty_f = ft.TextField(
            label=_t("notebook.qty_label", "Фактична кількість"),
            keyboard_type=ft.KeyboardType.NUMBER,
            border_radius=radius,
            border_color=border,
            focused_border_color=accent,
            text_style=ft.TextStyle(color=tc, size=_SZ_INPUT),
            bgcolor=panel,
            dense=True,
            autofocus=True,
        )
        err_txt = ft.Text("", size=11, color=ft.Colors.RED_400)

        def _confirm(_=None):
            raw = (qty_f.value or "").strip().replace(",", ".")
            try:
                qty_val = float(raw) if raw else None
            except Exception:
                qty_val = None
            if qty_val is None:
                err_txt.value = _t("notebook.qty_err", "Вкажіть кількість")
                with contextlib.suppress(Exception):
                    err_txt.update()
                return
            _close_popup()
            _confirm_order_task(oid, qty_val)

        popup_card = ft.Container(
            content=ft.Column(
                [
                    ft.Text(
                        _t("notebook.qty_title", "Скільки штук фактично замовили?"),
                        size=13,
                        weight=ft.FontWeight.W_700,
                        color=tc,
                        no_wrap=False,
                    ),
                    ft.Divider(height=6, color=rule_clr),
                    qty_f,
                    err_txt,
                    ft.Row(
                        [
                            ft.TextButton(
                                _t("common.cancel", "Скасувати"),
                                on_click=lambda _: _close_popup(),
                            ),
                            ft.FilledButton(
                                _t("common.confirm", "Підтвердити"),
                                icon=ft.Icons.CHECK,
                                on_click=_confirm,
                                style=ft.ButtonStyle(
                                    bgcolor=accent,
                                    color=atxt,
                                    shape=ft.RoundedRectangleBorder(radius=8),
                                ),
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.END,
                        spacing=8,
                    ),
                ],
                spacing=8,
                tight=True,
            ),
            bgcolor=paper_bg,
            border=ft.border.all(1, border),
            border_radius=radius,
            padding=16,
            width=280,
            shadow=ft.BoxShadow(spread_radius=1, blur_radius=18, color="#59000000"),
        )
        _show_popup(popup_card)

    # ── task list ──────────────────────────────────────────────────────────
    def _rebuild_tasks():
        tasks = _entry().get("tasks") or []
        tasks_col.controls.clear()
        logger.debug(
            "[notebook] _rebuild_tasks: %d tasks, batch=%s", len(tasks), _batch[0]
        )
        for task in tasks:
            task.setdefault("id", str(uuid.uuid4()))
            tid = task["id"]
            done = bool(task.get("done"))
            label = task.get("text", "")
            is_editing = edit_st["id"] == tid

            is_order_linked = bool(task.get("order_task_id"))
            _sid = task.get("series_id")
            _series_active = bool(_sid) and not (
                (diary.get("_series") or {}).get(_sid, {}).get("disabled_from")
            )
            _repeat_tooltip = (
                {
                    "daily": _t("notebook.repeat.tt_daily", "Повторюється: щодня"),
                    "weekly": _t("notebook.repeat.tt_weekly", "Повторюється: щотижня"),
                    "monthly": _t(
                        "notebook.repeat.tt_monthly", "Повторюється: щомісяця"
                    ),
                }.get(
                    (
                        (diary.get("_series") or {}).get(_sid, {}).get("recur")
                        if _sid
                        else None
                    ),
                    _t("notebook.repeat.tooltip", "Повторення"),
                )
                if _series_active
                else _t("notebook.repeat.tooltip", "Зробити повторюваною")
            )

            def _toggle(e, t=task):
                if t.get("order_task_id") and e.control.value and not t.get("done"):
                    # Order-linked tasks don't check off directly — confirm
                    # the actually-ordered quantity first (this also writes
                    # back to "Розклад замовлень"). Revert the checkbox
                    # until that's done.
                    e.control.value = False
                    with contextlib.suppress(Exception):
                        e.control.update()
                    _open_qty_popup(t)
                    return
                t["done"] = e.control.value
                save_lbl.value = ""
                with contextlib.suppress(Exception):
                    page.update()
                _rebuild_tasks()

            def _open_repeat(_=None, t=task):
                _open_repeat_popup(t, st["sel"])

            def _delete(_, t=task):
                tl = _entry().get("tasks") or []
                tl[:] = [x for x in tl if x.get("id") != t["id"]]
                edit_st["id"] = None
                _batch[0] = True
                try:
                    _rebuild_tasks()
                    _refresh_cal()
                    _refresh_recent()
                finally:
                    _batch[0] = False
                with contextlib.suppress(Exception):
                    page.update()

            def _start_edit(_, t=task):
                edit_st["id"] = t["id"]
                _rebuild_tasks()

            def _confirm_edit(_, t=task, edit_field=None):
                if edit_field is not None:
                    t["text"] = (edit_field.value or "").strip() or t["text"]
                edit_st["id"] = None
                save_lbl.value = ""
                with contextlib.suppress(Exception):
                    page.update()
                _rebuild_tasks()

            if is_editing:
                edit_tf = ft.TextField(
                    value=label,
                    border_radius=radius,
                    border_color=accent,
                    focused_border_color=accent,
                    text_style=ft.TextStyle(color=tc, size=_SZ_TASK),
                    bgcolor=panel,
                    dense=True,
                    content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
                    autofocus=True,
                    multiline=True,
                    min_lines=1,
                    max_lines=4,
                )
                edit_tf.on_submit = lambda e, t=task, tf=edit_tf: _confirm_edit(
                    e, t, tf
                )
                edit_tf.on_blur = lambda e, t=task, tf=edit_tf: _confirm_edit(e, t, tf)

                row_content = ft.Row(
                    [
                        ft.Checkbox(
                            value=done,
                            on_change=_toggle,
                            fill_color={
                                ft.ControlState.SELECTED: accent,
                                ft.ControlState.DEFAULT: border,
                            },
                            check_color=atxt,
                        ),
                        ft.Container(content=edit_tf, expand=True),
                        ft.IconButton(
                            ft.Icons.REPLAY,
                            icon_size=13,
                            icon_color=accent if _series_active else mc,
                            on_click=_open_repeat,
                            tooltip=_repeat_tooltip,
                            style=ft.ButtonStyle(padding=ft.padding.all(2)),
                            visible=not is_order_linked,
                        ),
                        ft.IconButton(
                            ft.Icons.CHECK,
                            icon_size=14,
                            icon_color=accent,
                            on_click=lambda e, t=task, tf=edit_tf: _confirm_edit(
                                e, t, tf
                            ),
                            tooltip=_t("common.save", "Зберегти"),
                            style=ft.ButtonStyle(padding=ft.padding.all(2)),
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=4,
                )
            else:
                label_widget = (
                    ft.Text(
                        "",
                        size=_SZ_TASK,
                        no_wrap=False,
                        spans=[
                            ft.TextSpan(
                                label,
                                style=ft.TextStyle(
                                    decoration=ft.TextDecoration.LINE_THROUGH, color=mc
                                ),
                            )
                        ],
                    )
                    if done
                    else ft.Text(label, size=_SZ_TASK, color=tc, no_wrap=False)
                )
                label_row_controls = []
                if is_order_linked:
                    label_row_controls.append(
                        ft.Container(
                            content=ft.Icon(
                                ft.Icons.LOCAL_SHIPPING_OUTLINED,
                                size=13,
                                color=mc,
                            ),
                            tooltip=_t(
                                "notebook.order_linked_tooltip",
                                "Пов'язано з розкладом замовлень",
                            ),
                        )
                    )
                label_row_controls.append(
                    ft.Container(content=label_widget, expand=True)
                )

                row_content = ft.Row(
                    [
                        ft.Checkbox(
                            value=done,
                            on_change=_toggle,
                            fill_color={
                                ft.ControlState.SELECTED: accent,
                                ft.ControlState.DEFAULT: border,
                            },
                            check_color=atxt,
                        ),
                        ft.Row(
                            label_row_controls,
                            expand=True,
                            spacing=6,
                            vertical_alignment=ft.CrossAxisAlignment.START,
                        ),
                        ft.IconButton(
                            ft.Icons.REPLAY,
                            icon_size=13,
                            icon_color=accent if _series_active else mc,
                            on_click=_open_repeat,
                            tooltip=_repeat_tooltip,
                            style=ft.ButtonStyle(padding=ft.padding.all(2)),
                            visible=not is_order_linked,
                        ),
                        ft.IconButton(
                            ft.Icons.EDIT_OUTLINED,
                            icon_size=13,
                            icon_color=mc,
                            on_click=_start_edit,
                            tooltip=_t("packaging.card.edit_btn", "Редагувати"),
                            style=ft.ButtonStyle(padding=ft.padding.all(2)),
                        ),
                        ft.IconButton(
                            ft.Icons.CLOSE,
                            icon_size=13,
                            icon_color=mc,
                            on_click=_delete,
                            tooltip=_t("common.delete", "Видалити"),
                            style=ft.ButtonStyle(padding=ft.padding.all(2)),
                        ),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    spacing=4,
                )

            tasks_col.controls.append(
                ft.Container(
                    content=row_content,
                    bgcolor=soft if done else panel,
                    border_radius=6,
                    padding=ft.padding.symmetric(horizontal=6, vertical=4),
                    border=ft.border.all(1, accent if is_editing else border),
                )
            )
        if not _batch[0]:
            with contextlib.suppress(Exception):
                tasks_col.update()

    def _add_task(_=None):
        txt = (new_task_tf.value or "").strip()
        if not txt:
            return
        _entry().setdefault("tasks", []).append(
            {"id": str(uuid.uuid4()), "text": txt, "done": False}
        )
        new_task_tf.value = ""
        save_lbl.value = ""
        _batch[0] = True
        try:
            _rebuild_tasks()
            _refresh_cal()
            _refresh_recent()
        finally:
            _batch[0] = False
        with contextlib.suppress(Exception):
            page.update()

    new_task_tf.on_submit = _add_task

    def _on_notes_change(e):
        _entry()["notes"] = e.control.value or ""
        save_lbl.value = ""
        with contextlib.suppress(Exception):
            save_lbl.update()

    notes_tf.on_change = _on_notes_change

    # ── save ──────────────────────────────────────────────────────────────
    def _save(_=None):
        _entry()["notes"] = notes_tf.value or ""
        _persist(db, _mid, diary)
        save_lbl.value = "✓ " + _t("orders.sync.synced", "Збережено")
        with contextlib.suppress(Exception):
            save_lbl.update()

    # ── date navigation ────────────────────────────────────────────────────
    def _select(d: date):
        _entry()["notes"] = notes_tf.value or ""
        edit_st["id"] = None
        st["sel"] = d
        st["vm"] = d.replace(day=1)
        _batch[0] = True
        try:
            _refresh_right()
            _refresh_cal()
            _refresh_recent()
        finally:
            _batch[0] = False
        with contextlib.suppress(Exception):
            page.update()

    def _refresh_right():
        d = st["sel"]
        suffix = (
            " (" + _t("notebook.today", "Сьогодні") + ")" if d == date.today() else ""
        )
        date_hdr.value = f"{_wd_long[d.weekday()]}, {d.day} {_months_gen[d.month-1]} {d.year}{suffix}"
        entry = _entry()
        notes_value = entry.get("notes", "")
        tasks_list = entry.get("tasks") or []
        logger.debug(
            "[notebook] _refresh_right date=%s notes_len=%d tasks=%d diary_keys=%s",
            _dk(d),
            len(notes_value),
            len(tasks_list),
            list(diary.keys())[:5],
        )
        notes_tf.value = notes_value
        save_lbl.value = ""
        _rebuild_tasks()  # guarded by _batch, caller does page.update()

    # ── mini calendar ──────────────────────────────────────────────────────
    def _refresh_cal():
        vm = st["vm"]
        sel = st["sel"]
        today = date.today()
        cal_grid.controls.clear()

        def _prev(_):
            m = st["vm"]
            st["vm"] = (
                m.replace(month=m.month - 1)
                if m.month > 1
                else m.replace(year=m.year - 1, month=12)
            )
            _refresh_cal()

        def _next(_):
            m = st["vm"]
            st["vm"] = (
                m.replace(month=m.month + 1)
                if m.month < 12
                else m.replace(year=m.year + 1, month=1)
            )
            _refresh_cal()

        cal_grid.controls.append(
            ft.Row(
                [
                    ft.IconButton(
                        ft.Icons.CHEVRON_LEFT,
                        on_click=_prev,
                        icon_size=16,
                        icon_color=mc,
                        style=ft.ButtonStyle(padding=ft.padding.all(2)),
                    ),
                    ft.Text(
                        f"{_months[vm.month-1]} {vm.year}",
                        size=_SZ_CAL_MTH,
                        weight=ft.FontWeight.W_600,
                        color=tc,
                        expand=True,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.IconButton(
                        ft.Icons.CHEVRON_RIGHT,
                        on_click=_next,
                        icon_size=16,
                        icon_color=mc,
                        style=ft.ButtonStyle(padding=ft.padding.all(2)),
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )

        # weekday header row
        cal_grid.controls.append(
            ft.Row(
                [
                    ft.Container(
                        ft.Text(
                            wd,
                            size=_SZ_CAL_WD,
                            color=mc,
                            text_align=ft.TextAlign.CENTER,
                        ),
                        width=_CAL_W,
                        alignment=ft.Alignment(0, 0),
                    )
                    for wd in _wd_short
                ],
                spacing=1,
            )
        )

        # day cells — no Stack to avoid first-render grey-stripe glitch
        for week in calendar.monthcalendar(vm.year, vm.month):
            cells = []
            for day_num in week:
                if day_num == 0:
                    cells.append(ft.Container(width=_CAL_W, height=_CAL_H))
                    continue
                d = date(vm.year, vm.month, day_num)
                dk = _dk(d)
                is_sel = d == sel
                is_today = d == today
                has_data = dk in diary and _has_content(diary[dk])

                day_bg = accent if is_sel else (soft if is_today else "transparent")
                day_fg = atxt if is_sel else (accent if is_today else tc)

                # Border for days with content (avoids Stack+positioned children)
                cell_border = None
                if has_data and is_sel:
                    cell_border = ft.border.all(2, atxt)
                elif has_data:
                    cell_border = ft.border.all(2, accent)

                def _dc(_, dd=d):
                    _select(dd)

                cells.append(
                    ft.Container(
                        width=_CAL_W,
                        height=_CAL_H,
                        border_radius=max(radius // 2, 4),
                        bgcolor=day_bg,
                        border=cell_border,
                        alignment=ft.Alignment(0, 0),
                        on_click=_dc,
                        ink=True,
                        content=ft.Text(
                            str(day_num),
                            size=_SZ_CAL_DAY,
                            color=day_fg,
                            text_align=ft.TextAlign.CENTER,
                        ),
                    )
                )
            cal_grid.controls.append(ft.Row(cells, spacing=1, tight=True))

        if not _batch[0]:
            with contextlib.suppress(Exception):
                cal_grid.update()

    # ── recent entries sidebar list ────────────────────────────────────────
    def _refresh_recent():
        recent_col.controls.clear()
        active = sorted(
            [dk for dk, e in diary.items() if _has_content(e)],
            reverse=True,
        )[:6]

        if not active:
            recent_col.controls.append(
                ft.Text(
                    _t("notebook.empty_recent", "Поки нічого"),
                    size=_SZ_RECENT,
                    color=mc,
                    italic=True,
                )
            )
        else:
            for dk in active:
                try:
                    d = date.fromisoformat(dk)
                except Exception:
                    continue
                is_sel = d == st["sel"]
                tasks = diary.get(dk, {}).get("tasks") or []
                done_n = sum(1 for t in tasks if t.get("done"))
                badge = f"  {done_n}/{len(tasks)}" if tasks else ""

                def _rc(_, dd=d):
                    _select(dd)

                recent_col.controls.append(
                    ft.Container(
                        content=ft.Row(
                            [
                                ft.Container(
                                    width=3, height=13, bgcolor=accent, border_radius=2
                                ),
                                ft.Text(
                                    f"{d.day} {_months_gen[d.month-1]}{badge}",
                                    size=_SZ_RECENT,
                                    color=accent if is_sel else tc,
                                    weight=ft.FontWeight.W_600 if is_sel else None,
                                ),
                            ],
                            spacing=5,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        bgcolor=soft if is_sel else "transparent",
                        border_radius=6,
                        padding=ft.padding.symmetric(horizontal=6, vertical=3),
                        on_click=_rc,
                        ink=True,
                    )
                )
        if not _batch[0]:
            with contextlib.suppress(Exception):
                recent_col.update()

    # ── spiral binding strip ───────────────────────────────────────────────
    def _make_rings():
        holes = [
            ft.Container(
                width=18,
                height=18,
                bgcolor=ring_hole,
                border_radius=9,
                border=ft.border.all(2, mc),
                margin=ft.margin.symmetric(horizontal=3),
            )
            for _ in range(14)
        ]
        return ft.Container(
            width=_RING_W,
            bgcolor=ring_bg,
            content=ft.Column(
                holes,
                spacing=18,
                alignment=ft.MainAxisAlignment.CENTER,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                tight=True,
            ),
            padding=ft.padding.symmetric(vertical=22),
        )

    # ── layout panels ──────────────────────────────────────────────────────
    left_panel = ft.Container(
        width=_LEFT_W,
        bgcolor=cover_bg,
        border_radius=ft.border_radius.only(top_left=radius, bottom_left=radius),
        padding=ft.padding.symmetric(horizontal=10, vertical=10),
        content=ft.Column(
            [
                cal_grid,
                ft.Divider(height=8, color=border),
                ft.Text(
                    _t("notebook.recent_section", "Останні записи"),
                    size=_SZ_SECTION,
                    weight=ft.FontWeight.W_600,
                    color=mc,
                ),
                ft.Container(height=2),
                ft.Container(content=recent_col, expand=True),
                ft.FilledTonalButton(
                    _t("notebook.today", "Сьогодні"),
                    icon=ft.Icons.TODAY,
                    on_click=lambda _: _select(date.today()),
                    height=32,
                    style=ft.ButtonStyle(
                        shape=ft.RoundedRectangleBorder(radius=radius),
                        bgcolor=soft,
                        color=accent,
                        padding=ft.padding.symmetric(horizontal=8, vertical=4),
                    ),
                ),
            ],
            spacing=4,
            expand=True,
        ),
    )

    _right_w = _DLG_W - _LEFT_W - _RING_W  # 621
    right_panel = ft.Container(
        width=_right_w,
        bgcolor=paper_bg,
        border_radius=ft.border_radius.only(top_right=radius, bottom_right=radius),
        padding=ft.padding.only(left=18, right=14, top=14, bottom=12),
        border=ft.border.only(left=ft.BorderSide(2, margin_clr)),
        clip_behavior=ft.ClipBehavior.HARD_EDGE,
        content=ft.Column(
            [
                date_hdr,
                ft.Divider(height=6, color=rule_clr),
                ft.Text(
                    _t("notebook.tasks_section", "Задачі"),
                    size=_SZ_SECTION,
                    weight=ft.FontWeight.W_600,
                    color=mc,
                ),
                ft.Container(height=2),
                ft.Container(content=tasks_col, expand=True),
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Icon(ft.Icons.ADD, size=14, color=mc),
                            ft.Container(content=new_task_tf, expand=True),
                        ],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    padding=ft.padding.only(top=4),
                ),
                ft.Divider(height=14, color=rule_clr),
                ft.Text(
                    _t("notebook.notes_section", "Нотатки"),
                    size=_SZ_SECTION,
                    weight=ft.FontWeight.W_600,
                    color=mc,
                ),
                ft.Container(height=2),
                notes_tf,
            ],
            spacing=4,
            expand=True,
        ),
    )

    # ── assemble overlay (replaces ft.AlertDialog — avoids content rendering bug) ──
    dialog_box = ft.Container(
        content=ft.Column(
            [
                # title bar
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Icon(ft.Icons.MENU_BOOK_ROUNDED, size=20, color=accent),
                            ft.Text(
                                _t("notebook.title", "Мій блокнот"),
                                weight=ft.FontWeight.W_700,
                                size=14,
                                expand=True,
                                color=tc,
                            ),
                            save_lbl,
                            ft.FilledButton(
                                _t("common.save", "Зберегти"),
                                icon=ft.Icons.SAVE_OUTLINED,
                                on_click=_save,
                                height=32,
                                style=ft.ButtonStyle(
                                    bgcolor=accent,
                                    color=atxt,
                                    shape=ft.RoundedRectangleBorder(radius=8),
                                    padding=ft.padding.symmetric(
                                        horizontal=12, vertical=4
                                    ),
                                ),
                            ),
                            ft.IconButton(
                                ft.Icons.HELP_OUTLINE,
                                icon_size=18,
                                icon_color=mc,
                                tooltip=_t("common.help.tooltip", "Довідка"),
                                on_click=lambda _: _show_notebook_help(),
                            ),
                            ft.IconButton(
                                ft.Icons.CLOSE,
                                on_click=_close,
                                icon_size=18,
                                icon_color=mc,
                            ),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    width=_DLG_W,
                    padding=ft.padding.only(left=16, right=8, top=10, bottom=8),
                    border=ft.border.only(bottom=ft.BorderSide(1, border)),
                ),
                # panels row
                ft.Container(
                    content=ft.Row(
                        [left_panel, _make_rings(), right_panel],
                        spacing=0,
                        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                    ),
                    width=_DLG_W,
                    height=_DLG_H,
                ),
            ],
            spacing=0,
            tight=True,
        ),
        bgcolor=paper_bg,
        border_radius=radius,
        border=ft.border.all(1, border),
        shadow=ft.BoxShadow(spread_radius=1, blur_radius=20, color="#59000000"),
        width=_DLG_W,
    )

    overlay = ft.Container(
        content=dialog_box,
        bgcolor="#80000000",
        alignment=ft.Alignment(0, 0),
        expand=True,
    )
    _overlay_ref[0] = overlay

    _refresh_cal()
    _refresh_right()
    _refresh_recent()

    page.overlay.append(overlay)
    page.update()
