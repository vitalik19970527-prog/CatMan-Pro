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

    # current_region_name="" used to be hardcoded here — _effective_region_id()
    # only resolves a region when a name is given AND the company has more
    # than one region, so for any multi-region company this silently
    # produced region_id=None below, which manager_has_category_access_in_region
    # treats as "no access" unconditionally. Pull the SAME current-region
    # value the main Товари tab uses (dashboard.py's ProductsView call) via
    # the page's cached dashboard reference.
    _region_name = ""
    _dash = getattr(page, "_dashboard", None)
    if _dash is not None:
        with contextlib.suppress(Exception):
            _region_name = _dash._current_region_for_query() or ""

    pv = ProductsView(content_area, user_data, db, current_region_name=_region_name)

    # Editing requires owning at least one category node in the current
    # region (see manager_has_category_access_in_region) — a manager with
    # view-only region access (e.g. a керівник looking at a subordinate's
    # region) previously still saw a clickable "Редагування" tab that
    # simply rejected the switch once clicked. Checking this up front and
    # folding it into can_edit means those users never see a tab that
    # doesn't work for them — only "Перегляд" is offered.
    try:
        _has_cat_access = await asyncio.to_thread(
            db.manager_has_category_access_in_region,
            _mid, company_id, pv._effective_region_id(),
        )
    except Exception:
        _has_cat_access = True

    can_edit = (
        not bool((user_data or {}).get("subordinate_view_readonly", False))
        and _has_cat_access
    )

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
        # Diagnostic trail for the repeated "nothing happens" report -
        # this confirms/rules out each link in the chain: did the click
        # even reach here, is `page` usable, did the task actually get
        # scheduled AND started. Two rounds of fixes based on guesses
        # (close/reopen race, then a DB-hang timeout) didn't resolve it,
        # so this stays until a log line pinpoints exactly which step
        # never happens.
        logger.info(
            "[edit-switch] click received pid=%s has_run_task=%s",
            pid, hasattr(page, "run_task"),
        )
        if not hasattr(page, "run_task"):
            # No page/run_task available — nothing we can safely schedule
            # the (now async) show_product_dialog coroutine onto.
            logger.error("[edit-switch] page has no run_task — aborting pid=%s", pid)
            return

        async def _do_switch_to_edit():
            logger.info("[edit-switch] task started pid=%s", pid)
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

        try:
            page.run_task(_do_switch_to_edit)
        except Exception:
            # This was a bare contextlib.suppress before — if page.run_task
            # ITSELF raises synchronously (not from inside the coroutine),
            # the click would look exactly like "nothing happens" with
            # nothing in the log at all. Now it's at least visible.
            logger.exception(
                "[edit-switch] page.run_task(_do_switch_to_edit) raised pid=%s", pid
            )

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
