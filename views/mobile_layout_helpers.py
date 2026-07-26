"""Shared helpers for mobile-only (Android) layout adaptations.

Every helper here is a no-op (or returns False) on desktop/exe builds, so
importing and using these from a view never changes desktop appearance --
`is_mobile()` is only True when `page.platform` is an Android/iOS platform.
"""
import logging

import flet as ft

logger = logging.getLogger(__name__)


def is_mobile(page) -> bool:
    try:
        return bool(page and page.platform and page.platform.is_mobile())
    except Exception:
        return False


def is_mobile_landscape(page) -> bool:
    if not is_mobile(page):
        return False
    try:
        w = float(getattr(page, "width", 0) or 0)
        h = float(getattr(page, "height", 0) or 0)
        return w > 0 and h > 0 and w > h
    except Exception:
        return False


def register_resize_listener(page, key: str, handler) -> None:
    """Let several independent on_resized handlers coexist on one page.

    Flet only exposes a single `page.on_resized` slot. Several views (and now
    the dashboard shell) each need their own reaction to resize/rotation;
    assigning `page.on_resized = handler` directly -- the pattern every view
    used before -- silently discards whichever handler was attached earlier.
    This keeps a per-page registry keyed by caller and dispatches to all of
    them, so e.g. orders_view's column-width handler and the shell's
    landscape-compaction handler both keep firing.
    """
    listeners = getattr(page, "_mobile_resize_listeners", None)
    if listeners is None:
        listeners = {}
        page._mobile_resize_listeners = listeners

        def _dispatch(e):
            for listener_key, fn in list(listeners.items()):
                try:
                    fn(e)
                except Exception:
                    logger.exception("resize listener '%s' failed", listener_key)

        page.on_resized = _dispatch
    listeners[key] = handler


def make_scrollable_chip_row(controls, **row_kwargs) -> ft.Row:
    """Single-line, horizontally scrollable chip/stat row.

    On mobile the previous `wrap=True` rows broke onto several lines the
    moment horizontal space ran out, quietly eating the vertical space the
    data table below needed. A horizontally scrollable single line is the
    standard mobile pattern for this (filter chips, stat counters, etc.) and
    costs a fixed, small height regardless of how many chips there are.
    """
    row_kwargs.pop("wrap", None)
    row_kwargs.setdefault("spacing", 8)
    return ft.Row(controls, wrap=False, scroll=ft.ScrollMode.AUTO, **row_kwargs)


def apply_no_wrap_subtitle(text_control: ft.Text) -> ft.Text:
    """Force a header subtitle onto one line with an ellipsis instead of
    letting Flutter fall back to one-character-per-line wrapping when the
    available width collapses to near zero (seen on mobile with a squeezed
    header row)."""
    text_control.no_wrap = True
    text_control.overflow = ft.TextOverflow.ELLIPSIS
    text_control.max_lines = 1
    return text_control
