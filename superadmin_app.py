"""CatMan SupAdm — standalone Superadmin-only mini app.

Reuses database.py / superadmin_dashboard.py / i18n from the main CatMan Pro
app (no business-logic duplication). Skips the regular AuthScreen entirely:
the owner's identity (company/manager/email/phone) is fixed below, and the
login screen only ever asks for the password — see [[project_telegram_site_bot]]
Phase 4 for why (this app is for one specific owner, not general distribution).

Uses its own single-instance lock port (_SINGLE_INSTANCE_PORT) distinct from
main.py's, so this app and the main CatMan Pro app can run side by side on
the same PC without tripping each other's lock.
"""

import io
import os
import socket
import sys
from contextlib import suppress

import flet as ft

from database import DataManager
from logger import logger
from superadmin_dashboard import SuperAdminDashboard

_SINGLE_INSTANCE_PORT = 49713

# Fixed owner identity — this app is single-purpose, not for general
# distribution, so there's no signup/company-picker flow, only a password.
# Company name is deliberately left empty: per database.py:check_auth, an
# empty company_name triggers the global superadmin-only lookup (matched by
# email, no company scoping needed) rather than the per-company login path.
OWNER_COMPANY_NAME = ""
OWNER_MANAGER_NAME = "Віталій"
OWNER_EMAIL = "vitalik.1997.05.27@gmail.com"
OWNER_PHONE = "380958561057"


class _SingleInstanceFileLock:
    def __init__(self, handle: io.TextIOBase, lock_len: int = 1):
        self._handle = handle
        self._lock_len = lock_len

    def close(self):
        handle = getattr(self, "_handle", None)
        if handle is None:
            return
        self._handle = None
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, self._lock_len)
        except Exception:
            logger.exception("Failed to release single-instance file lock")
        finally:
            with suppress(Exception):
                handle.close()


class _NullLock:
    def close(self):
        pass


def _acquire_single_instance_lock():
    """Same TCP-loopback strategy as main.py's, on a different port so both
    apps can run at once — see main.py:_acquire_single_instance_lock."""
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
            with suppress(Exception):
                probe.close()

    if _probe_alive():
        return None

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    with suppress(Exception):
        sock.set_inheritable(False)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        return sock
    except OSError:
        with suppress(Exception):
            sock.close()
        if _probe_alive():
            return None
        logger.warning(
            "single_instance: could not bind port %d but no live server detected; allowing start",
            port,
        )
        return _NullLock()


def main(page: ft.Page):
    page.title = "CatMan SupAdm"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.locale_configuration = ft.LocaleConfiguration(
        supported_locales=[ft.Locale("uk", "UA")],
        current_locale=ft.Locale("uk", "UA"),
    )
    page.window.width = 420
    page.window.height = 560
    page.window.resizable = True
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER

    db = DataManager()

    def show_login(reason: str = "", transition_token=None):
        page.clean()
        password_field = ft.TextField(
            label="Пароль",
            password=True,
            can_reveal_password=True,
            width=280,
            on_submit=lambda e: do_login(None),
        )
        error_text = ft.Text("", color="red", size=12)
        login_btn = ft.FilledButton("Увійти", width=280)

        def do_login(_):
            error_text.value = ""
            login_btn.disabled = True
            page.update()
            user_data = db.check_auth(
                OWNER_COMPANY_NAME,
                OWNER_MANAGER_NAME,
                password_field.value or "",
                OWNER_EMAIL,
                OWNER_PHONE,
            )
            login_btn.disabled = False
            role = (user_data or {}).get("role") if isinstance(user_data, dict) else None
            is_superadmin = (role or "").strip().lower().replace(" ", "") in {
                "суперадмін",
                "суперадмин",
                "superadmin",
            }
            if not user_data or not is_superadmin:
                error_text.value = "Невірний пароль"
                page.update()
                return
            open_dashboard(user_data)

        login_btn.on_click = do_login

        page.add(
            ft.Column(
                [
                    ft.Text("CatMan SupAdm", size=22, weight="bold"),
                    ft.Text(OWNER_MANAGER_NAME, size=13, color="grey700"),
                    ft.Container(height=20),
                    password_field,
                    error_text,
                    ft.Container(height=8),
                    login_btn,
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                expand=True,
            )
        )
        page.update()

    def open_dashboard(user_data: dict):
        page.window.maximized = True
        dash = SuperAdminDashboard(
            page,
            user_data,
            on_logout=lambda reason="unknown", transition_token=None: show_login(
                reason, transition_token=transition_token
            ),
        )
        dash.build()
        page.update()

    show_login()


if __name__ == "__main__":
    _lock = _acquire_single_instance_lock()
    if _lock is None:
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showwarning(
                "CatMan SupAdm",
                "Програма вже запущена!\n\nЗнайдіть вікно CatMan SupAdm на панелі задач.",
            )
            root.destroy()
        except Exception:
            logger.exception("Failed to show already-running warning via tkinter")
        sys.exit(0)

    try:
        ft.run(main)
    finally:
        _lock.close()
