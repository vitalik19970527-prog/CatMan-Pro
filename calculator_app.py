"""CatMan Калькулятори — standalone portable mini-app.

Just the calculators picker (Звичайний / Собівартість / С/В з митом /
Рентабельність / Оборот-Забезпеченість) from views/calculators_view.py,
running with no login, no company, no database — everything (delivery
tariffs, price tiers) is entered by hand each time. "Saved" calculations
persist to a JSON file in the standard per-user app-data folder (see
_prefs_path) instead of the real app's DB.

Built as its own single-file portable exe (see scripts/build_calculator_app.ps1)
— no installer, just copy CatManCalculator.exe anywhere and run it.
"""

import asyncio
import contextlib
import json
import os
import sys
import time

import flet as ft

import i18n
from logger import logger
from views import calculators_view as _calc
from views.calculators_view import open_calculators_modal

APP_VERSION = "1.0.8"


def _check_calculator_password_blocking(password: str) -> tuple[bool, str]:
    """Real (non-standalone) DB check — the whole point of this gate is
    that SuperAdmin can revoke access remotely at any time (e.g. from an
    ex-employee's copy of this exe), so it must hit the live shared DB on
    every launch rather than trust anything cached locally. Blocking —
    only ever call via asyncio.to_thread from the UI.

    Returns (ok, error_message). error_message is only non-empty when the
    DB itself couldn't be reached (retried once) — a transient hiccup used
    to come back indistinguishable from "wrong password", which is exactly
    what "sometimes says wrong password even though I typed it right" was:
    a connection blip silently returned an empty policy, and an empty
    expected password always fails the comparison."""
    from database import DataManager

    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            db = DataManager()
            policy = db.get_calculator_access_policy()
            expected = str(policy.get("password") or "").strip()
            ok = bool(expected) and str(password or "").strip() == expected
            return ok, ""
        except Exception as e:
            last_exc = e
            if attempt == 0:
                time.sleep(0.6)
    logger.exception("CatMan Калькулятори: password check failed after retry", exc_info=last_exc)
    return False, "Не вдалося з'єднатися з сервером. Перевірте інтернет і спробуйте ще раз."


def _show_password_gate(page: ft.Page, on_success) -> None:
    """Fullscreen password prompt shown before anything else. One shared
    password for everyone, set/changed by SuperAdmin — see
    superadmin_dashboard.py's show_calculator_access()."""
    busy = [False]

    pw_field = ft.TextField(
        label="Пароль",
        password=True,
        can_reveal_password=True,
        width=260,
        autofocus=True,
        on_submit=lambda e: page.run_task(_try_login),
    )
    status_text = ft.Text("", size=12)
    login_btn = ft.FilledButton(
        "Увійти", icon=ft.Icons.LOGIN, width=260,
        on_click=lambda e: page.run_task(_try_login),
    )

    async def _try_login(_=None):
        if busy[0]:
            return
        pw = (pw_field.value or "").strip()
        if not pw:
            status_text.value = "Введіть пароль"
            status_text.color = "#B45309"
            with contextlib.suppress(Exception):
                page.update()
            return
        busy[0] = True
        login_btn.disabled = True
        status_text.value = "Перевірка..."
        status_text.color = "#64748B"
        with contextlib.suppress(Exception):
            page.update()
        try:
            ok, err = await asyncio.to_thread(_check_calculator_password_blocking, pw)
        except Exception:
            ok, err = False, ""
        busy[0] = False
        login_btn.disabled = False
        if ok:
            with contextlib.suppress(Exception):
                page.overlay.clear()
            on_success()
            return
        # A connection failure gets its own message (see
        # _check_calculator_password_blocking) — telling the user "wrong
        # password" when the DB was simply unreachable is misleading, and
        # was exactly why the correct password sometimes looked rejected.
        status_text.value = err or "Невірний пароль"
        status_text.color = "#DC2626"
        pw_field.value = ""
        with contextlib.suppress(Exception):
            page.update()
        with contextlib.suppress(Exception):
            await pw_field.focus()

    page.controls = [
        ft.Column(
            [
                ft.Icon(ft.Icons.LOCK_OUTLINE, size=40, color="#1E40AF"),
                ft.Text("CatMan Калькулятори", size=18, weight="bold"),
                ft.Text(
                    "Введіть пароль доступу", size=12, color="#64748B",
                ),
                pw_field,
                login_btn,
                status_text,
            ],
            spacing=12,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
            expand=True,
        )
    ]
    page.update()


def _install_fullscreen_dialogs() -> None:
    """Replace _FloatDialog's floating/draggable/shadowed panel (designed to
    hover ON TOP of the rest of the main CatMan Pro app, see its own
    docstring) with one that just fills this standalone app's single
    window — no border/shadow/rounded corners/absolute position, so it
    reads as the app's own content instead of "a window inside a window".
    Patches the CLASS, so every calculator dialog (they all build a fresh
    _FloatDialog()) picks it up automatically. Scoped to THIS process only —
    the main CatMan Pro app (a separate exe) never imports calculator_app.py,
    so its own floating/draggable calculator behavior is untouched."""

    def _open(self, page: ft.Page) -> None:
        body: list = []
        if self.title is not None:
            # Deliberately OUTSIDE the scrollable content area below —
            # stays pinned at full width instead of scrolling away
            # sideways with the content, or resizing with it.
            body.append(
                ft.Container(
                    content=self.title,
                    padding=ft.padding.symmetric(horizontal=16, vertical=10),
                    bgcolor="#EFF6FF",
                    border=ft.border.only(bottom=ft.BorderSide(1, "#E2E8F0")),
                )
            )
        if self.content is not None:
            if getattr(self, "center_content", False):
                # Small, never-overflowing content (just the picker) —
                # plain centered container, no scroll involved. Scroll
                # actually BREAKS centering: a scroll-enabled Column
                # ignores its own alignment/horizontal_alignment entirely
                # and always anchors content to the top-left regardless —
                # confirmed live (the picker drifted off-center the moment
                # scroll was added below for the calculator forms).
                body.append(
                    ft.Container(
                        content=self.content,
                        padding=16,
                        expand=True,
                        alignment=ft.Alignment(0, 0),
                    )
                )
            else:
                # Calculator forms — can genuinely overflow, so THIS needs
                # real scroll. Nested Row(scroll)->Column(scroll) is the
                # standard Flet/Flutter pattern for 2D scrolling: nothing
                # gets clipped regardless of window size or zoom level, and
                # nothing is pre-computed from window dimensions that can
                # go stale after the window/zoom changes later.
                content_wrap = ft.Container(
                    content=ft.Row(
                        [
                            ft.Column(
                                [self.content],
                                scroll=ft.ScrollMode.AUTO,
                                expand=True,
                                # Explicit, not relied-on defaults —
                                # content smaller than the available
                                # space (e.g. the saved-calculations
                                # list) drifted to vertical/horizontal
                                # center otherwise instead of sitting at
                                # the top-left like a normal form.
                                horizontal_alignment=ft.CrossAxisAlignment.START,
                            ),
                        ],
                        scroll=ft.ScrollMode.AUTO,
                        expand=True,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                    padding=16,
                    expand=True,
                    # Ctrl+Plus/Minus zoom (see _zoom_check below) — a real
                    # Flutter Scale transform, not the web build's CSS zoom
                    # hack: Flet's Scale defaults transform_hit_tests=True,
                    # so clicks stay correctly aligned with the visual size
                    # (unlike CSS zoom on the web build, which desynced
                    # Flutter's canvas from the DOM and broke clicks — this
                    # doesn't have that problem since there's no browser/DOM
                    # layer in between here at all).
                    scale=getattr(page, "_zoom_scale", 1.0),
                )
                page._zoom_target = content_wrap
                body.append(content_wrap)
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
        panel = ft.Container(
            content=ft.Column(body, spacing=0, tight=False, expand=True),
            expand=True,
            bgcolor="#FFFFFF",
        )
        self._panel = panel
        page.controls = [panel]
        with contextlib.suppress(Exception):
            page.update()

    def _close(self, page: ft.Page) -> None:
        page.controls = []
        with contextlib.suppress(Exception):
            page.update()

    _calc._FloatDialog._open = _open
    _calc._FloatDialog._close = _close
    _calc._FloatDialog._is_fullscreen_build = True


_ZOOM_MIN = 0.5
_ZOOM_MAX = 2.0
_ZOOM_STEP = 0.05


def _zoom_check(page: ft.Page, e: ft.KeyboardEvent) -> bool:
    """Ctrl+Plus/Minus zoom for the calculator forms — returns True if this
    event WAS a zoom shortcut (so callers know not to treat it as anything
    else). Installed as the page's default on_keyboard_event in main(), and
    also checked first by views/calculators_view.py's basic-calculator
    keypad handler (which otherwise fully owns page.on_keyboard_event while
    its own dialog is open) via page._catman_zoom_check — see that file's
    _on_key/_close for the other half of this."""
    if not e.ctrl:
        return False
    if e.key in ("+", "Numpad Add"):
        delta = _ZOOM_STEP
    elif e.key in ("-", "Numpad Subtract"):
        delta = -_ZOOM_STEP
    else:
        return False

    scale = getattr(page, "_zoom_scale", 1.0)
    scale = max(_ZOOM_MIN, min(_ZOOM_MAX, scale + delta))
    page._zoom_scale = scale
    target = getattr(page, "_zoom_target", None)
    if target is not None:
        with contextlib.suppress(Exception):
            target.scale = scale
            target.update()
    return True


def _prefs_path() -> str:
    # Now a proper installed app (Setup.exe, not a movable portable exe),
    # so saved calculations live in a dedicated subfolder right next to the
    # installed program itself, inside its own install root — the
    # installer (CatManCalculator.installer.iss) grants ordinary users
    # write permission on this one specific subfolder via [Dirs], since the
    # rest of the install root (under Program Files) stays admin-only.
    base = (
        os.path.dirname(os.path.abspath(sys.executable))
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__))
    )
    app_dir = os.path.join(base, "SavedCalculations")
    os.makedirs(app_dir, exist_ok=True)
    return os.path.join(app_dir, "calculator_saved.json")


class _StandaloneDB:
    """Implements only the DataManager methods the calculators actually
    call — no network, no login, no company. Delivery methods are a fixed
    generic list (name + editable tariff field, defaults to 0) so the
    Собівартість/С/В+CLO calculators always have at least one selectable
    option even with zero company data behind them."""

    def __init__(self):
        self._path = _prefs_path()
        self._prefs: dict = self._load()

    def _load(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._prefs, f, ensure_ascii=False)
        except Exception:
            logger.exception("CatMan Калькулятори: failed to save %s", self._path)

    # ── "Saved calculations" persistence (see _load_saved/_persist_saved) ──
    def get_manager_ui_preference(self, manager_id, key, default=""):
        return self._prefs.get(key, default)

    def set_manager_ui_preference(self, manager_id, key, value):
        self._prefs[key] = value
        self._save()

    # ── i18n (see i18n.load_manager_language/save_manager_language/t) ──────
    # No Postgres here, so translated VALUES can't come from
    # public.translation_values like in the real app — instead this reads
    # straight from i18n.py's own EN/PL/ZH/DE seed tables (the same data
    # that seeds a real install's DB on first run), so the portable exe's
    # 5-language switcher works fully offline.
    def get_translations_for_lang(self, lang_code: str) -> dict:
        return i18n.get_static_seed_translations(lang_code)

    def get_manager_workspace_state(self, manager_id):
        return self._prefs.get("_workspace_state") or {}

    def save_manager_workspace_state(self, manager_id, state) -> bool:
        self._prefs["_workspace_state"] = state
        self._save()
        return True

    # ── Delivery methods (Собівартість / С/В+CLO) ──────────────────────────
    @staticmethod
    def _default_delivery_methods() -> list[dict]:
        # No real company/manager delivery methods exist standalone, so a
        # fixed Авіа/Морем/Залізницею/Автотранспорт preset doesn't fit
        # everyone — instead start with a single blank, manually-named slot
        # (editable_name flag) and let the manager add as many more as they
        # need via the "+" button in _rebuild_methods
        # (views/calculators_view.py).
        return [
            {"id": "method_1", "name": "", "effective_price_per_kg": 0,
             "effective_price_currency": "USD", "user_enabled": True,
             "editable_name": True}
        ]

    def get_effective_delivery_methods_for_manager(self, *args, **kwargs):
        return self._default_delivery_methods()

    def get_company_delivery_methods(self, *args, **kwargs):
        return self._default_delivery_methods()

    def get_manager_delivery_overrides(self, *args, **kwargs):
        return {}

    def save_manager_delivery_overrides(self, *args, **kwargs):
        pass

    # ── Price tiers / regions — none configured standalone (optional fields) ──
    def get_manager_region_price_tiers_overrides(self, *args, **kwargs):
        return []

    def get_region_price_tiers(self, *args, **kwargs):
        return []

    def get_company_regions(self, *args, **kwargs):
        return []


def main(page: ft.Page):
    _install_fullscreen_dialogs()

    page.title = f"CatMan Калькулятори {APP_VERSION}"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.window.width = 820
    page.window.height = 640
    page.window.resizable = True
    page.window.min_width = 480
    page.window.min_height = 420
    # Opens maximized, same as CatMan Pro (main.py) — width/height above are
    # just the restored-down size if the user un-maximizes.
    page.window.full_screen = False
    page.window.maximized = True
    # Native title bar (minimize/maximize/close) stays on — no custom
    # buttons added, nothing to duplicate.
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.vertical_alignment = ft.MainAxisAlignment.CENTER
    page.padding = 0

    # Ctrl+Plus/Minus zoom, on by default. views/calculators_view.py's
    # basic-calculator keypad handler takes over page.on_keyboard_event
    # while its own dialog is open (and restores this on close) — it checks
    # page._catman_zoom_check first, so zoom keeps working there too.
    page._zoom_scale = 1.0
    page._zoom_target = None
    page._catman_zoom_check = lambda e: _zoom_check(page, e)
    page.on_keyboard_event = page._catman_zoom_check

    def _start_app(_=None):
        db = _StandaloneDB()
        i18n.init_i18n(db)
        i18n.load_manager_language(db, "local")

        def _open_picker(_=None):
            open_calculators_modal(
                page, db, manager_id="local", company_id="", region_id="", regions_list=[]
            )

        # Fallback screen — only ever seen if the picker's own X gets closed
        # without closing the app window itself.
        reopen_btn = ft.FilledButton(
            "Відкрити калькулятори", icon=ft.Icons.CALCULATE, on_click=_open_picker
        )
        page.controls = [
            ft.Column(
                [reopen_btn],
                spacing=14,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                expand=True,
            )
        ]
        page.update()

        # The picker and every calculator dialog already carry their own
        # language switcher in their header (i18n.build_language_switcher,
        # same as the main app) — an extra page-level one here just showed
        # up as a second, out-of-sync globe icon floating over the dialog's
        # own one. Nothing else needs a page-wide switcher, so it's dropped.

        # Open the picker immediately on launch, filling the whole window.
        _open_picker()

    async def _startup():
        import app_updater
        from database import DataManager

        blocked = await app_updater.check_and_maybe_block(
            page,
            app_version=APP_VERSION,
            get_policy_fn=DataManager().get_calculator_update_policy,
            exe_basename="CatManCalculator",
        )
        if blocked:
            return
        _show_password_gate(page, _start_app)

    page.run_task(_startup)


if __name__ == "__main__":
    ft.run(main)
