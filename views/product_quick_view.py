"""Shared product quick-view / quick-edit modal callable from any view."""
from __future__ import annotations

import asyncio
import contextlib

import flet as ft
import i18n
from logger import logger


async def open_product_quick_view(
    content_area,
    db,
    product_id: str,
    user_data: dict,
) -> None:
    """Fetch product by ID and open the details modal with a Перегляд/Редагування toggle.

    The product fetch (and the details dialog's own build, see
    ProductsView.show_product_details_dialog) run off the UI thread — doing
    these DB round trips inline on Flet's single event loop is what made
    this modal (and every other one built the same way) take up to a
    minute to open on Android, where each round trip is much slower than
    on desktop's LAN."""
    _mid = str((user_data or {}).get("active_manager_id") or (user_data or {}).get("id") or "")
    _t = i18n.bind(str((user_data or {}).get("id") or ""))
    pid = str(product_id or "").strip()
    if not pid:
        return

    page = getattr(content_area, "page", None)
    if not page:
        return

    company_id = (user_data or {}).get("company_id")
    try:
        product = await asyncio.to_thread(db.get_product_by_id, company_id, pid)
    except Exception:
        logger.exception("open_product_quick_view: get_product_by_id failed pid=%s", pid)
        product = None

    if not product:
        try:
            page.snack_bar = ft.SnackBar(
                ft.Text(_t("products.not_found", "Товар не знайдено"), color="white"),
                bgcolor="#EF4444",
                duration=2500,
            )
            page.snack_bar.open = True
            page.update()
        except Exception:
            pass
        return

    from views.products_view import ProductsView

    pv = ProductsView(content_area, user_data, db, current_region_name="")

    can_edit = not bool((user_data or {}).get("subordinate_view_readonly", False))

    dlg_ref: list = [None]

    def _close_current_dlg():
        dlg = dlg_ref[0]
        if dlg is not None:
            try:
                page.pop_dialog()
            except Exception:
                pass
            dlg_ref[0] = None

    async def _switch_to_view():
        # Re-fetch so any edits made just before switching back are reflected.
        try:
            fresh = await asyncio.to_thread(db.get_product_by_id, company_id, pid)
        except Exception:
            fresh = None
        await _open_view(fresh or product)

    def _switch_to_edit():
        _close_current_dlg()
        # Opening the edit dialog in the same tick as closing the view
        # dialog can outrun Flutter's close transition — the new dialog
        # silently fails to render and the modal just disappears ("closes,
        # doesn't switch to edit"). Give the close a brief moment to settle.
        if hasattr(page, "run_task"):

            async def _delayed_open():
                await asyncio.sleep(0.2)
                with contextlib.suppress(Exception):
                    await pv.show_product_dialog(product, on_switch_to_view=_switch_to_view)

            with contextlib.suppress(Exception):
                page.run_task(_delayed_open)
        # else: no page/run_task available — nothing we can safely schedule
        # the (now async) show_product_dialog coroutine onto.

    async def _open_view(prod: dict) -> None:
        await pv.show_product_details_dialog(
            prod,
            on_switch_to_edit=_switch_to_edit if can_edit else None,
            _dlg_ref=dlg_ref,
        )

    await _open_view(product)


def product_icon_btn(
    product_id: str,
    content_area,
    db,
    user_data: dict,
    *,
    size: int = 14,
    tooltip: str | None = None,
) -> ft.Control:
    """Small icon button that opens the product quick-view modal.

    Returns an invisible zero-size container when product_id is empty.
    """
    _mid = str((user_data or {}).get("active_manager_id") or (user_data or {}).get("id") or "")
    _t = i18n.bind(str((user_data or {}).get("id") or ""))
    pid = str(product_id or "").strip()
    if not pid:
        return ft.Container(width=0, height=0, visible=False)
    def _on_click(_e):
        _pg = getattr(content_area, "page", None)
        if _pg and hasattr(_pg, "run_task"):
            with contextlib.suppress(Exception):
                _pg.run_task(open_product_quick_view, content_area, db, pid, user_data)

    return ft.IconButton(
        icon=ft.Icons.INFO_OUTLINE,
        icon_size=size,
        icon_color="#94A3B8",
        tooltip=tooltip if tooltip is not None else _t("products.card_tooltip", "Картка товару"),
        on_click=_on_click,
        style=ft.ButtonStyle(
            padding=ft.padding.all(0),
        ),
        width=size + 8,
        height=size + 8,
    )
