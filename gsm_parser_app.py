"""CatManGSMParser — standalone single-purpose app.

Just the GSM Parser tab (views/gsm_parser_view.py's GsmParserView) behind the
exact same login screen/saved-account logic as CatMan Pro (auth_screen.py's
AuthScreen, reused as-is) — no sidebar, no other tabs. Lets a company that
only wants the parser (or wants it running on a second screen alongside
another app) skip the full CatMan Pro workspace entirely.

Permissions are unchanged from the real app: superadmin and managers granted
GSM Parser edit permissions get the editable view, everyone else gets the
same read-only view GsmParserView already renders for them. Registration is
disabled — this app is opened for an existing account, never used to create
one (see AuthScreen(..., allow_registration=False) below).

Built as its own exe (see scripts/build_release.ps1's "gsmparser" profile).
"""

import contextlib
import socket
import sys

import flet as ft

import app_storage
import app_updater
from auth_screen import AuthScreen
from database import DataManager
from logger import logger

APP_VERSION = "1.0.5"

# Own single-instance lock port, distinct from main.py's (49712) and
# superadmin_app.py's (49713) — so all standalone CatMan apps can run side
# by side on the same PC without tripping each other's lock.
_SINGLE_INSTANCE_PORT = 49714


class _NullLock:
    def close(self):
        pass


def _acquire_single_instance_lock():
    """Same TCP-loopback strategy as main.py/superadmin_app.py — see
    main.py's own copy for the full rationale."""
    port = _SINGLE_INSTANCE_PORT

    def _probe_alive() -> bool:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            with contextlib.suppress(Exception):
                probe.close()

    if _probe_alive():
        return None

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    with contextlib.suppress(Exception):
        sock.set_inheritable(False)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        return sock
    except OSError:
        with contextlib.suppress(Exception):
            sock.close()
        if _probe_alive():
            return None
        logger.warning(
            "single_instance: could not bind port %d but no live server detected;"
            " allowing start",
            port,
        )
        return _NullLock()


def _is_superadmin_role(role) -> bool:
    normalized = (role or "").strip().lower().replace(" ", "")
    return normalized in {"суперадмін", "суперадмин", "superadmin"}


def main(page: ft.Page):
    # Called here (start of main(), like main.py's own main()) rather than
    # only in the __main__ guard below — the code-protection build compiles
    # this module with Nuitka and imports it from a generic stub script
    # (see build_release.ps1), which never executes an `if __name__ ==
    # "__main__":` block since __name__ isn't "__main__" for an imported
    # module. Must run before anything (DataManager(), AuthScreen's saved-
    # account file, etc.) computes a cache path.
    app_storage.apply_storage_dir_override()
    page.title = f"CatMan GSM Parser {APP_VERSION}"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.window.width = 1280
    page.window.height = 820
    page.window.resizable = True
    page.window.min_width = 900
    page.window.min_height = 600
    page.window.maximized = True
    page.window.full_screen = False
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.vertical_alignment = ft.MainAxisAlignment.START
    page.padding = 0

    db = DataManager()

    def show_login():
        page.clean()
        page.window.maximized = True
        page.window.full_screen = False
        page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
        page.vertical_alignment = ft.MainAxisAlignment.START
        auth = AuthScreen(
            page,
            on_success=_on_login,
            allow_registration=False,
            on_settings_click=lambda: auth.open_storage_settings_dialog(),
        )
        auth.show_login()

    def _on_login(user_data):
        page.clean()
        user_data = user_data if isinstance(user_data, dict) else {}
        company_id = user_data.get("company_id")
        is_superadmin = _is_superadmin_role(user_data.get("role"))

        # Superadmin isn't scoped to one company's toggle — everyone else
        # only gets in if their company actually has the extension enabled
        # (same gate dashboard.py's own show_gsm_parser applies).
        if not is_superadmin and not db.is_company_gsm_parser_enabled(company_id):
            page.add(
                ft.Column(
                    [
                        ft.Icon(ft.Icons.INFO_OUTLINE, size=40, color=ft.Colors.GREY_500),
                        ft.Text(
                            "ГСМ Парсер не увімкнено для вашої компанії —"
                            " зверніться до адміністратора",
                            size=14,
                            color=ft.Colors.GREY_800,
                            text_align=ft.TextAlign.CENTER,
                        ),
                        ft.FilledButton("Вийти", on_click=lambda e: show_login()),
                    ],
                    spacing=16,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    alignment=ft.MainAxisAlignment.CENTER,
                    expand=True,
                )
            )
            page.update()
            return

        page.window.maximized = True
        page.window.full_screen = False
        page.vertical_alignment = ft.MainAxisAlignment.START
        content_area = ft.Column(expand=True)
        page.add(content_area)

        from views.gsm_parser_view import GsmParserView

        gv = GsmParserView(
            content_area,
            user_data,
            db,
            active_company_id=company_id,
            is_superadmin=is_superadmin,
        )
        gv.build()
        page.update()

    async def _startup():
        blocked = await app_updater.check_and_maybe_block(
            page,
            app_version=APP_VERSION,
            get_policy_fn=DataManager().get_gsm_parser_update_policy,
            exe_basename="CatManGSMParser",
        )
        if blocked:
            return
        show_login()

    page.run_task(_startup)


if __name__ == "__main__":
    _lock = _acquire_single_instance_lock()
    if _lock is None:
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showwarning(
                "CatMan GSM Parser",
                "Програма вже запущена!\n\nЗнайдіть вікно CatMan GSM Parser на панелі задач.",
            )
            root.destroy()
        except Exception:
            logger.exception("Failed to show already-running warning via tkinter")
        sys.exit(0)

    try:
        ft.run(main)
    finally:
        _lock.close()







