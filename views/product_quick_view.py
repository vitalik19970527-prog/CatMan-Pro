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

    # ONE AlertDialog object lives for the whole modal's lifetime — both
    # switch directions patch its title/content/actions in place
    # (existing_dlg=...) instead of ever closing it and opening a second
    # one. Closing-then-reopening raced Flet's async dialog-close
    # transition no matter how carefully it was timed (fixed sleeps,
    # polling page._dialogs for the close to "land" — both proved
    # unreliable) and could silently leave nothing open at all. There is
    # now exactly one open/close for this whole modal: the very first
    # open below, and whatever the user does to dismiss it (Закрити/Esc/
    # Скасувати/Зберегти) at the end.
    dlg_ref: list = [None]

    async def _switch_to_view(_existing_dlg=None):
        # products_view.py's _switch_back_to_view calls this with the
        # shared dialog object as its one argument — same object as
        # dlg_ref[0] either way (it's never replaced), so both work; the
        # parameter just keeps the call signature honest either way.
        # Re-fetch so any edits made just before switching back are reflected.
        try:
            fresh = await asyncio.to_thread(db.get_product_by_id, company_id, pid)
        except Exception:
            fresh = None
        await _open_view(fresh or product, existing_dlg=_existing_dlg or dlg_ref[0])

    def _switch_to_edit():
        if not hasattr(page, "run_task"):
            # No page/run_task available — nothing we can safely schedule
            # the (now async) show_product_dialog coroutine onto.
            return

        async def _do_switch_to_edit():
            try:
                # show_product_dialog does several sequential DB round trips
                # (access check, hydrate, acquire_edit_lock) before it ever
                # touches the dialog — a hung/slow one of those previously
                # looked EXACTLY like the close/reopen race ("nothing
                # happens", no error, just an unresponsive click) with no
                # way to tell them apart. A bounded wait turns a stuck call
                # into a visible, actionable message instead of an
                # indefinite silent hang.
                await asyncio.wait_for(
                    pv.show_product_dialog(
                        product,
                        on_switch_to_view=_switch_to_view,
                        existing_dlg=dlg_ref[0],
                    ),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "open_product_quick_view: switch to edit timed out pid=%s", pid
                )
                with contextlib.suppress(Exception):
                    page.snack_bar = ft.SnackBar(
                        ft.Text(
                            "Редагування не відповідає (можливо, проблема з мережею/БД). Спробуйте ще раз.",
                            color="white",
                        ),
                        bgcolor="#EF4444",
                        duration=4000,
                    )
                    page.snack_bar.open = True
                    page.update()
            except Exception as e:
                # Previously swallowed via contextlib.suppress(Exception) -
                # any real bug in here looked identical to a close/reopen
                # race ("modal just disappears, nothing happens"),
                # impossible to tell apart or diagnose. Surface it instead.
                logger.exception(
                    "open_product_quick_view: switch to edit failed pid=%s", pid
                )
                with contextlib.suppress(Exception):
                    page.snack_bar = ft.SnackBar(
                        ft.Text(f"Не вдалося відкрити редагування: {e}", color="white"),
                        bgcolor="#EF4444",
                        duration=3500,
                    )
                    page.snack_bar.open = True
                    page.update()

        with contextlib.suppress(Exception):
            page.run_task(_do_switch_to_edit)

    async def _open_view(prod: dict, existing_dlg=None) -> None:
        await pv.show_product_details_dialog(
            prod,
            on_switch_to_edit=_switch_to_edit if can_edit else None,
            _dlg_ref=dlg_ref,
            existing_dlg=existing_dlg,
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
