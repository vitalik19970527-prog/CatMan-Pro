import asyncio
import contextlib
import os
from typing import Callable, Optional

import flet as ft
import i18n
from logger import logger as _abh_logger


def create_orders_header_loading_indicator(visible: bool = True) -> ft.Row:
    """Create the same header loading indicator style used in Orders view."""
    return ft.Row(
        [
            ft.ProgressRing(width=16, height=16, stroke_width=2),
            ft.Text(i18n.t("async.loading", "Завантаження..."), size=10),
        ],
        spacing=6,
        visible=visible,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )


def run_async_build(
    host,
    *,
    page,
    build_sync: Callable[[], None],
    loading_stub_sync: Optional[Callable[[], None]] = None,
    delay_sec: float = 0.01,
    token_attr: str = "_async_build_token",
    indicator_attr: str = "_header_loading_indicator",
) -> None:
    """Run a sync builder through page.run_task with token guard and indicator toggling."""
    if callable(loading_stub_sync):
        with contextlib.suppress(Exception):
            loading_stub_sync()

    setattr(host, token_attr, int(getattr(host, token_attr, 0) or 0) + 1)
    token = int(getattr(host, token_attr) or 0)

    # Max seconds a build may run before we give up and show a retry button.
    _BUILD_TIMEOUT = int(os.environ.get("CM_BUILD_TIMEOUT", "60"))

    async def _runner():
        await asyncio.sleep(max(0.0, float(delay_sec or 0.0)))
        if token != int(getattr(host, token_attr, 0) or 0):
            return

        indicator = getattr(host, indicator_attr, None)
        if indicator is not None:
            indicator.visible = True
            with contextlib.suppress(Exception):
                indicator.update()

        _build_exc: BaseException | None = None
        try:
            # Run in a background thread so a hanging DB query can't freeze the
            # asyncio event loop (and the whole UI) indefinitely.
            await asyncio.wait_for(
                asyncio.to_thread(build_sync),
                timeout=_BUILD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            _abh_logger.error(
                "run_async_build: build_sync timed out after %ds", _BUILD_TIMEOUT
            )
            try:
                area = getattr(host, "content_area", None)
                if area is not None and hasattr(area, "controls"):
                    area.controls.clear()
                    area.controls.append(
                        ft.Column(
                            [
                                ft.Icon(ft.Icons.WIFI_OFF, size=30, color="#F59E0B"),
                                ft.Text(
                                    i18n.t("async.timeout_title", "Завантаження зависло"),
                                    size=14,
                                    weight="bold",
                                    color="#92400E",
                                ),
                                ft.Text(
                                    i18n.t("async.timeout_body", "Сервер не відповів вчасно. Спробуйте ще раз."),
                                    size=11,
                                    color="grey600",
                                ),
                                ft.ElevatedButton(
                                    i18n.t("async.retry_btn", "Повторити"),
                                    on_click=lambda _e: (
                                        run_async_build(
                                            host,
                                            page=page,
                                            build_sync=build_sync,
                                            loading_stub_sync=loading_stub_sync,
                                            delay_sec=0,
                                            token_attr=token_attr,
                                            indicator_attr=indicator_attr,
                                        )
                                    ),
                                ),
                            ],
                            spacing=8,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        )
                    )
                    with contextlib.suppress(Exception):
                        area.update()
            except Exception:
                pass
        except Exception as _exc:
            _build_exc = _exc
            _abh_logger.exception("build_sync raised inside run_async_build")
            # Show error UI so the tab is never left as a blank white screen.
            try:
                area = getattr(host, "content_area", None)
                if area is not None and hasattr(area, "controls"):
                    area.controls.clear()
                    area.controls.append(
                        ft.Column(
                            [
                                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=30, color="#EF4444"),
                                ft.Text(
                                    i18n.t("async.error_title", "Помилка завантаження вкладки"),
                                    size=14,
                                    weight="bold",
                                    color="#EF4444",
                                ),
                                ft.Text(str(_exc)[:300], size=11, color="grey600"),
                                ft.ElevatedButton(
                                    i18n.t("async.retry_btn", "Повторити"),
                                    on_click=lambda _e: (
                                        run_async_build(
                                            host,
                                            page=page,
                                            build_sync=build_sync,
                                            loading_stub_sync=loading_stub_sync,
                                            delay_sec=0,
                                            token_attr=token_attr,
                                            indicator_attr=indicator_attr,
                                        )
                                    ),
                                ),
                            ],
                            spacing=8,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        )
                    )
                    with contextlib.suppress(Exception):
                        area.update()
            except Exception:
                pass
        finally:
            if token == int(getattr(host, token_attr, 0) or 0):
                area = getattr(host, "content_area", None)
                if area is not None and hasattr(area, "update"):
                    with contextlib.suppress(Exception):
                        area.update()
                # Hide the spinner AFTER Flutter has time to render the new content.
                # Calling indicator.update() immediately would hide it before Flutter
                # finishes layout on large/complex pages — causing a blank flash.
                indicator2 = getattr(host, indicator_attr, None)
                if indicator2 is not None:
                    await asyncio.sleep(0.30)
                    if token == int(getattr(host, token_attr, 0) or 0):
                        indicator2.visible = False
                        with contextlib.suppress(Exception):
                            indicator2.update()

    if page and hasattr(page, "run_task"):
        with contextlib.suppress(Exception):
            page.run_task(_runner)
            return

    build_sync()


# ── Shared orders-cache verifier ─────────────────────────────────────────────
#
# Architecture: cache-only (no snapshot files).
#
# shared_orders_map (in-memory dict) is the single source of truth.
# On app restart the map is empty. The FIRST tab that opens calls
# ensure_orders_cache_fresh(), which:
#   1. Checks DB pulse vs last-known pulse (stored in user_data, resets to 0 on restart)
#   2. If pulse differs OR map is empty → load from DB into map
#   3. Sets a per-session "verified" flag so subsequent tab opens are instant
#
# After the map is populated, all tabs read from map (fast, consistent).
# Edits update map immediately and queue a patch to DB (every 45s or on Save).
#
# Snapshot files (.json) are no longer read or written by this module.

_VERIFYING_KEY = "_orders_cache_is_verifying"
_PULSE_KEY_PREFIX = "_orders_cache_pulse_"


def _verified_key(region_id: Optional[str]) -> str:
    return f"_orders_cache_session_verified_{region_id or 'all'}"


def _get_orders_pulse(db, company_id: str) -> int:
    try:
        out = db.get_companies_sync_pulses([company_id], ["orders"]) or {}
        by_company = (out or {}).get(company_id, {})
        return int((by_company or {}).get("orders") or 0)
    except Exception:
        return 0


# Legacy helpers kept so existing callers in cost_price_orders_table.py compile.
# They are no longer called by ensure_orders_cache_fresh.
def _orders_snapshot_path(
    app_data_dir: str, company_id: str, manager_id: str, region_id: Optional[str]
) -> str:
    def _safe(s: str) -> str:
        return (s or "").replace("/", "_").replace("\\", "_")

    cid = _safe(company_id) or "nocompany"
    mgr = _safe(manager_id) or "nouser"
    reg = _safe(region_id) if region_id else "all"
    return os.path.join(app_data_dir, f"orders_snapshot_{cid}_{mgr}_{reg}.json")


def _save_snapshot(*args, **kwargs) -> None:
    """Snapshot writes are disabled. Kept for import compatibility only."""
    pass


def _read_snapshot_pulse(path: str) -> int:
    """Snapshot reads are disabled. Kept for import compatibility only."""
    return 0


def _load_snapshot_orders(path: str) -> list:
    """Snapshot reads are disabled. Kept for import compatibility only."""
    return []


async def ensure_orders_cache_fresh(
    user_data: dict,
    db,
    app_data_dir: str,
    company_id: str,
    manager_id: str,
    region_id: Optional[str],
    shared_orders_map: dict,
    visible_product_ids: Optional[list] = None,
) -> None:
    """Ensure shared_orders_map is up-to-date with the DB.

    Cache-only: no snapshot files. Called once per session per region.

    Reload conditions:
    - Cache (shared_orders_map) is empty  — always reload on fresh start
    - DB pulse differs from last-known pulse — reload when another device made changes

    Spinner is shown by the caller while this runs.
    """
    _vkey = _verified_key(region_id)
    _verifying_key = f"{_VERIFYING_KEY}_{region_id or 'all'}"
    _pulse_key = f"{_PULSE_KEY_PREFIX}{region_id or 'all'}"

    # Already verified this session — skip immediately.
    if user_data.get(_vkey):
        return

    # Another tab is currently verifying — wait up to 5 s.
    if user_data.get(_verifying_key):
        for _ in range(50):
            await asyncio.sleep(0.1)
            if user_data.get(_vkey):
                return
        return

    user_data[_verifying_key] = True
    try:
        db_pulse = await asyncio.to_thread(_get_orders_pulse, db, company_id)
        cached_pulse = int(user_data.get(_pulse_key) or 0)

        has_orders = any(not str(k).startswith("__") for k in shared_orders_map)

        # Reload when:
        # - cache is empty (app just started, nothing loaded yet)
        # - DB has newer data (pulse changed)
        # - pulse=0 (server doesn't support pulse) → always reload on FIRST open per session
        #   This is critical: startup may populate cache with stale API-cached data.
        #   With pulse=0 we can't detect if server changed, so we always do one fresh load.
        if db_pulse == 0:
            needs_reload = True
        elif not has_orders:
            needs_reload = True
        elif db_pulse != cached_pulse:
            needs_reload = True
        else:
            needs_reload = False

        if needs_reload:
            if not visible_product_ids:
                products = await asyncio.to_thread(
                    lambda: db.get_products(
                        company_id,
                        manager_id=manager_id,
                        region_id=region_id,
                    )
                    or []
                )
                visible_product_ids = [
                    str((p or {}).get("id") or "").strip()
                    for p in products
                    if isinstance(p, dict) and (p.get("id") or "").strip()
                ] or None

            orders = await asyncio.to_thread(
                lambda: db.get_orders(
                    company_id,
                    region_id,
                    manager_id=manager_id,
                    force_refresh=True,
                    visible_product_ids=visible_product_ids,
                )
                or []
            )

            shared_orders_map.clear()
            for o in orders:
                oid = str((o or {}).get("id") or "").strip()
                if oid:
                    shared_orders_map[oid] = dict(o)

            user_data[_pulse_key] = db_pulse

        user_data[_vkey] = True

    except Exception:
        _abh_logger.debug("[CACHE-CHECK] ERROR during verification", exc_info=True)
        user_data[_vkey] = True
    finally:
        user_data[_verifying_key] = False
