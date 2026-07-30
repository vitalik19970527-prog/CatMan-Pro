import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import weakref
from contextlib import suppress
from urllib.parse import urlparse

import flet as ft
import httpx
from packaging.version import Version

import i18n
import local_pg_manager
from admin_dashboard import AdminDashboard
from auth_screen import AuthScreen
from config import Config
from dashboard import MainDashboard
from database import DataManager
from logger import logger
from superadmin_dashboard import SuperAdminDashboard

_SINGLE_INSTANCE_PORT = 49712
_REGISTERED_SELECTABLE_TEXTS: "weakref.WeakSet[ft.Text]" = weakref.WeakSet()
_CRASH_HOOKS_INSTALLED = False


class _NullLock:
    """Dummy lock returned when binding fails but no live server is detected."""

    def close(self):
        pass


def _acquire_single_instance_lock():
    """
    Single-instance guard via TCP loopback.

    Strategy:
      1. Probe port with connect() — if it succeeds, a live server is there.
      2. If connect fails (ECONNREFUSED), no live server → bind the port and own it.
      3. If bind also fails (very rare race), try a second probe to confirm.

    Advantages over file-lock only:
      - OS always releases TCP ports when a process dies, even on crash.
      - Works correctly when file handles or sockets are inherited by
        Flet's internal subprocesses.
    """
    port = _SINGLE_INSTANCE_PORT

    def _probe_alive(timeout: float = 0.3) -> bool:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(timeout)
        try:
            probe.connect(("127.0.0.1", port))
            return True  # got a response → someone is listening
        except OSError:
            return False  # connection refused / timeout → nobody home
        finally:
            with suppress(Exception):
                probe.close()

    if _probe_alive():
        return None  # another instance is genuinely running

    # Nothing is listening → try to claim the port
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    # Prevent Flet subprocesses from inheriting this socket (Python 3.4+)
    with suppress(Exception):
        sock.set_inheritable(False)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        return sock
    except OSError:
        with suppress(Exception):
            sock.close()
        # Bind failed because another process already holds the port — two
        # launches racing to start at nearly the same moment (seen in
        # practice: two `python main.py` runs a second apart, each still
        # inside slow module-import startup when the other's probe ran).
        # A single 0.2s probe could spuriously read "nobody home" under
        # system load and this handler used to fall through to *allowing*
        # a duplicate start — which is exactly the scenario it's meant to
        # catch. Retry the confirmation a few times with growing timeouts
        # before conceding; a live listener answers almost immediately, so
        # this only adds real delay in the genuine "OS reserved the port"
        # case this fallback exists for.
        for retry_timeout in (0.3, 0.6, 1.0):
            time.sleep(0.15)
            if _probe_alive(retry_timeout):
                return None
        # Still nothing detected after retries — either the OS has this
        # port reserved for something else, or a very unusual local
        # condition. Be lenient: let the app start rather than blocking it
        # forever.
        logger.warning(
            "single_instance: could not bind port %d but no live server detected"
            " after retries; allowing start",
            port,
        )
        return _NullLock()  # dummy — lets the app start


def _is_superadmin_role(role):
    normalized = (role or "").strip().lower().replace(" ", "")
    return normalized in {"суперадмін", "суперадмин", "superadmin"}


def _is_admin_role(role):
    normalized = (role or "").strip().lower().replace(" ", "")
    return normalized in {"адмін", "админ", "admin"}


def _version_is_outdated(current: str, minimum: str) -> bool:
    try:
        return Version(current) < Version(minimum)
    except Exception:
        logger.exception("Version comparison failed in _version_is_outdated")
        return False


def _ensure_page_dialog_api_compat() -> None:
    if not hasattr(ft.Page, "open"):

        def _open(self, dialog):
            if dialog is None:
                return
            try:
                self.show_dialog(dialog)
            except Exception:
                logger.exception("[modal-shim:open] show_dialog failed")

        ft.Page.open = _open

    if not hasattr(ft.Page, "close"):

        def _close(self, dialog=None):
            try:
                self.pop_dialog()
            except Exception:
                logger.exception("[modal-shim:close] pop_dialog failed")

        ft.Page.close = _close


_ensure_page_dialog_api_compat()


def _install_global_crash_hooks() -> None:
    global _CRASH_HOOKS_INSTALLED
    if _CRASH_HOOKS_INSTALLED:
        return

    def _sys_excepthook(exc_type, exc_value, exc_tb):
        try:
            logger.error(
                "[crash] unhandled_exception | type=%s | value=%s",
                getattr(exc_type, "__name__", str(exc_type)),
                str(exc_value),
                exc_info=(exc_type, exc_value, exc_tb),
            )
        except Exception:
            pass

    def _threading_excepthook(args):
        try:
            logger.error(
                "[crash] unhandled_thread_exception | thread=%s | type=%s | value=%s",
                getattr(getattr(args, "thread", None), "name", ""),
                getattr(
                    getattr(args, "exc_type", None),
                    "__name__",
                    str(getattr(args, "exc_type", "")),
                ),
                str(getattr(args, "exc_value", "")),
                exc_info=(
                    getattr(args, "exc_type", None),
                    getattr(args, "exc_value", None),
                    getattr(args, "exc_traceback", None),
                ),
            )
        except Exception:
            pass

    try:
        sys.excepthook = _sys_excepthook
    except Exception:
        logger.exception("[crash] failed to install sys.excepthook")

    try:
        if hasattr(threading, "excepthook"):
            threading.excepthook = _threading_excepthook
    except Exception:
        logger.exception("[crash] failed to install threading.excepthook")

    _install_flet_session_error_guard()

    _CRASH_HOOKS_INSTALLED = True
    logger.info("[crash] global crash hooks installed")


def _install_flet_session_error_guard() -> None:
    """Stop one well-known, long-standing timing race (a background/deferred
    task's control.update() firing after the user already navigated away and
    Flet removed that control from the page — "Control must be added to the
    page first", seen across many views in catman.log since May 2026: orders,
    categories, packaging stock, bulk product template, and the quick-account
    dropdown on the login screen) from tearing down the whole session.

    flet.messaging.session.Session.dispatch_event reports ANY exception
    raised by ANY event handler to the client as SESSION_CRASHED via
    Session.error(), which blanks the entire page behind a fatal "The
    application encountered an error" banner — even though the app itself
    is otherwise fine and the same exception is already being caught and
    logged locally at dozens of call sites. Only this specific, already-
    understood exception shape is downgraded to a log-and-continue here;
    everything else still reaches the client normally so real bugs stay
    visible instead of being silently swallowed."""
    try:
        from flet.messaging.session import Session

        if getattr(Session, "_catman_error_guard_installed", False):
            return

        _original_session_error = Session.error

        def _guarded_session_error(self, message: str):
            first_line = str(message or "").splitlines()[0] if message else ""
            if "Control must be added to the page first" in first_line:
                logger.warning(
                    "[crash-guard] suppressed session-crash for stale control update: %s",
                    first_line,
                )
                return
            _original_session_error(self, message)

        Session.error = _guarded_session_error
        Session._catman_error_guard_installed = True
        logger.info("[crash] flet session error guard installed")
    except Exception:
        logger.exception("[crash] failed to install flet session error guard")


def _install_selectable_text_registry() -> None:
    try:
        text_cls = ft.Text
        if getattr(text_cls, "_catman_selectable_registry_patched", False):
            return

        original_init = text_cls.__init__

        def _patched_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            try:
                if bool(getattr(self, "selectable", False)):
                    _REGISTERED_SELECTABLE_TEXTS.add(self)
            except Exception:
                logger.exception("Failed to add selectable text to registry")

        text_cls.__init__ = _patched_init
        text_cls._catman_selectable_registry_patched = True
    except Exception:
        logger.exception("Failed to install selectable text registry")


_install_selectable_text_registry()


def _clear_selectable_text_selection(page: ft.Page) -> None:
    if page is None:
        return

    targets = []
    for text in list(_REGISTERED_SELECTABLE_TEXTS):
        try:
            if getattr(text, "page", None) is page and bool(
                getattr(text, "selectable", False)
            ):
                text.selectable = False
                targets.append(text)
        except Exception:
            continue

    if not targets:
        return

    try:
        page.update(*targets)
    except Exception:
        logger.exception("Failed to clear text selection")
        return

    async def _restore_selection_flags():
        await asyncio.sleep(0.03)
        restore_targets = []
        for text in targets:
            try:
                if getattr(text, "page", None) is page:
                    text.selectable = True
                    restore_targets.append(text)
            except Exception:
                continue
        if not restore_targets:
            return
        try:
            page.update(*restore_targets)
        except Exception:
            logger.exception("Failed to restore text selection")

    try:
        page.run_task(_restore_selection_flags)
    except Exception:
        logger.exception("Failed to schedule selection restore")


def _install_global_selection_clear(page: ft.Page) -> None:
    try:
        original_add = page.add
    except Exception:
        logger.exception("Failed to patch page.add for global selection clear")
        return

    if getattr(page, "_catman_selection_clear_installed", False):
        return

    def _wrap_root_control(control):
        if control is None or getattr(control, "_catman_selection_wrapped", False):
            return control

        wrapped = ft.GestureDetector(
            on_tap=lambda _e: _clear_selectable_text_selection(page),
            content=ft.TransparentPointer(content=control),
        )
        try:
            wrapped.expand = bool(getattr(control, "expand", False))
        except Exception:
            pass
        wrapped._catman_selection_wrapped = True
        return wrapped

    def _patched_add(*controls):
        return original_add(*[_wrap_root_control(control) for control in controls])

    page.add = _patched_add
    page._catman_selection_clear_installed = True


def _resolve_probe_target():
    endpoint = (getattr(Config, "APPWRITE_ENDPOINT", "") or "").strip()
    if not endpoint:
        return None
    try:
        parsed = urlparse(endpoint)
        host = (parsed.hostname or "").strip()
        if not host:

            return None
        if parsed.port:
            port = int(parsed.port)
        else:
            port = 443 if (parsed.scheme or "").lower() == "https" else 80
        return host, port
    except Exception:
        logger.exception("Failed to resolve network probe target")
        return None


def _probe_network(host: str, port: int, timeout_seconds: float = 1.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def _compact_numeric(value, factor=0.72, minimum=2):
    try:
        if isinstance(value, (int, float)):
            return max(minimum, int(round(float(value) * factor)))
    except Exception:
        pass
    return value


def _compact_padding_value(value, factor=0.72, minimum=2):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return _compact_numeric(value, factor=factor, minimum=minimum)
    left = getattr(value, "left", None)
    top = getattr(value, "top", None)
    right = getattr(value, "right", None)
    bottom = getattr(value, "bottom", None)
    if any(v is not None for v in (left, top, right, bottom)):
        return ft.Padding.only(
            left=_compact_numeric(left or 0, factor=factor, minimum=minimum),
            top=_compact_numeric(top or 0, factor=factor, minimum=minimum),
            right=_compact_numeric(right or 0, factor=factor, minimum=minimum),
            bottom=_compact_numeric(bottom or 0, factor=factor, minimum=minimum),
        )
    return value


_COMPACT_CHILD_ATTRS = [
    "content",
    "controls",
    "actions",
    "title",
    "label",
    "prefix",
    "suffix",
]


def _scan_toggle_upload_flags(control, toggle_flags, upload_flags, visited=None):
    """One post-order pass over the whole dialog tree, memoizing per node
    (by id) whether ITS subtree contains a Checkbox/Switch or an
    upload/image/photo IconButton.

    _compact_control_tree used to answer this by re-running a fresh
    from-scratch recursive scan for EVERY Row it visited (once for "has
    toggle", once for "has upload") — for a deeply nested dialog (Rows
    inside Rows inside tabs, e.g. the GSM Parser device card) a leaf gets
    re-scanned once per ancestor Row, so cost grows with tree depth x size
    instead of just size. That's what made opening a complex modal feel like
    a freeze. Compute every node's flag exactly once here; the main
    traversal below just looks it up."""
    if control is None:
        return False, False
    marker = id(control)
    if visited is None:
        visited = set()
    if marker in visited:
        return toggle_flags.get(marker, False), upload_flags.get(marker, False)
    visited.add(marker)

    has_toggle = isinstance(control, (ft.Checkbox, ft.Switch))
    has_upload = False
    if isinstance(control, ft.IconButton):
        icon_value = str(getattr(control, "icon", "") or "").upper()
        has_upload = any(kw in icon_value for kw in ("UPLOAD", "IMAGE", "PHOTO"))

    for attr in _COMPACT_CHILD_ATTRS:
        try:
            value = getattr(control, attr, None)
        except Exception:
            value = None
        if value is None:
            continue
        children = value if isinstance(value, list) else [value]
        for child in children:
            c_toggle, c_upload = _scan_toggle_upload_flags(
                child, toggle_flags, upload_flags, visited
            )
            has_toggle = has_toggle or c_toggle
            has_upload = has_upload or c_upload

    toggle_flags[marker] = has_toggle
    upload_flags[marker] = has_upload
    return has_toggle, has_upload


def _compact_control_tree(
    control, seen=None, in_dialog=False, toggle_flags=None, upload_flags=None
):
    if control is None:
        return
    if seen is None:
        seen = set()
    if toggle_flags is None or upload_flags is None:
        # Root call for this dialog — precompute once for the whole tree.
        toggle_flags = {}
        upload_flags = {}
        _scan_toggle_upload_flags(control, toggle_flags, upload_flags)
    marker = id(control)
    if marker in seen:
        return
    seen.add(marker)

    try:
        if isinstance(control, ft.Text):
            size = getattr(control, "size", None)
            if isinstance(size, (int, float)):
                if size >= 20:
                    control.size = max(12, int(size - 4))
                elif size >= 14:
                    control.size = max(11, int(size - 2))
                elif size >= 12:
                    control.size = max(9, int(size - 1))
            elif size is None:
                control.size = 12

        if isinstance(
            control,
            (ft.FilledButton, ft.OutlinedButton, ft.ElevatedButton, ft.TextButton),
        ):
            height = getattr(control, "height", None)
            if height is None or (isinstance(height, (int, float)) and height > 30):
                control.height = 28

        if isinstance(control, ft.IconButton):
            icon_size = getattr(control, "icon_size", None)
            if icon_size is None or (
                isinstance(icon_size, (int, float)) and icon_size > 18
            ):
                control.icon_size = 16

        if isinstance(control, ft.TextField):
            # Avoid hardcoded height in modals: fixed heights cause label/content overlap
            # on multiline fields and mixed form layouts.
            if in_dialog:
                control.height = None
            tsize = getattr(control, "text_size", None)
            if tsize is None or (isinstance(tsize, (int, float)) and tsize > 13):
                control.text_size = 12
            if in_dialog:
                control.content_padding = ft.Padding.symmetric(
                    horizontal=10, vertical=6
                )

        if isinstance(control, ft.Dropdown):
            if in_dialog:
                control.height = None
            tsize = getattr(control, "text_size", None)
            if tsize is None or (isinstance(tsize, (int, float)) and tsize > 13):
                control.text_size = 12
            if in_dialog:
                control.content_padding = ft.Padding.symmetric(
                    horizontal=10, vertical=6
                )

        if isinstance(control, (ft.Row, ft.Column)):
            if in_dialog:
                # Keep consistent compact spacing in every modal block.
                control.spacing = 6
            else:
                spacing = getattr(control, "spacing", None)
                if spacing is None or isinstance(spacing, (int, float)) and spacing < 8:
                    control.spacing = 8

        if in_dialog and isinstance(control, ft.Row):
            # Do not force one-line layout for toggle rows (Checkbox/Switch blocks),
            # otherwise multiselect controls can be clipped in modal forms.
            has_toggle = toggle_flags.get(marker, False)
            has_upload = upload_flags.get(marker, False)
            if has_toggle or has_upload:
                control.wrap = True
                control.scroll = None
                run_spacing = getattr(control, "run_spacing", None)
                if run_spacing is None or (
                    isinstance(run_spacing, (int, float)) and run_spacing < 6
                ):
                    control.run_spacing = 6
            else:
                # Keep data-entry fields in one visual line when possible.
                control.wrap = False
                if getattr(control, "scroll", None) is None:
                    control.scroll = ft.ScrollMode.ADAPTIVE

        if isinstance(control, ft.Container):
            control.padding = _compact_padding_value(
                getattr(control, "padding", None), factor=0.68, minimum=2
            )
            control.margin = _compact_padding_value(
                getattr(control, "margin", None), factor=0.68, minimum=1
            )
            width = getattr(control, "width", None)
            if in_dialog and isinstance(width, (int, float)) and 420 <= width < 960:
                control.width = min(980, int(width * 1.18))

        if isinstance(control, ft.AlertDialog):
            # Uniform small paddings for all modals.
            control.inset_padding = ft.Padding.symmetric(horizontal=10, vertical=10)
            control.content_padding = ft.Padding.symmetric(horizontal=10, vertical=8)
            control.actions_padding = ft.Padding.symmetric(horizontal=10, vertical=8)
            in_dialog = True
    except Exception:
        logger.exception("Dialog control compacting step failed")

    for attr in [
        "title",
        "content",
        "icon",
        "leading",
        "trailing",
        "subtitle",
        "label",
        "prefix",
        "suffix",
        "actions",
        "controls",
    ]:
        try:
            value = getattr(control, attr, None)
        except Exception:
            value = None
        if value is None:
            continue
        if isinstance(value, list):
            for item in value:
                _compact_control_tree(
                    item,
                    seen,
                    in_dialog=in_dialog,
                    toggle_flags=toggle_flags,
                    upload_flags=upload_flags,
                )
        else:
            _compact_control_tree(
                value,
                seen,
                in_dialog=in_dialog,
                toggle_flags=toggle_flags,
                upload_flags=upload_flags,
            )


def _install_global_compact_dialogs(page: ft.Page):
    try:
        original_show_dialog = page.show_dialog
    except Exception:
        return

    compacted_dialog_ids = set()

    def _patched_show_dialog(dialog):
        try:
            if dialog is not None and id(dialog) not in compacted_dialog_ids:
                _compact_control_tree(dialog)
                compacted_dialog_ids.add(id(dialog))
        except Exception:
            logger.exception("Global dialog compacting failed")
        return original_show_dialog(dialog)

    page.show_dialog = _patched_show_dialog


def main(page: ft.Page):
    runtime_id = str(uuid.uuid4())[:8]
    try:
        page._runtime_id = runtime_id
    except Exception:
        pass

    # Every view in this app already calls .update() explicitly and
    # precisely on whatever control actually changed (hundreds of call
    # sites across views/*.py) — nothing here relies on Flet's automatic
    # post-event page update. That automatic update fires after EVERY
    # single event (click, hover-move, anything) regardless, and on a page
    # with a large control tree underneath (e.g. GSM Parser's 2000+ device
    # table) its full-tree diff took multiple seconds PER EVENT — a live
    # py-spy dump caught it running even from inside a handler that had
    # just called ft.context.disable_auto_update() itself, meaning
    # Flet resets that per-event before the next dispatch either way, so
    # disabling it once per handler never reliably stuck. Disabling it
    # globally, once, for the whole session removes that unconditionally.
    with suppress(Exception):
        ft.context.disable_auto_update()

    def _diag_session_meta():
        try:
            session_id = str(getattr(page, "session_id", "") or "")
        except Exception:
            session_id = ""
        return {
            "runtime_id": runtime_id,
            "session_id": session_id,
            "page_id": hex(id(page)),
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
            "python": sys.executable,
            "cwd": os.getcwd(),
        }

    try:
        m = _diag_session_meta()
        logger.info(
            "[session] main_enter | runtime_id=%s | session_id=%s | page_id=%s | pid=%s | thread=%s | python=%s | cwd=%s",
            m["runtime_id"],
            m["session_id"],
            m["page_id"],
            m["pid"],
            m["thread"],
            m["python"],
            m["cwd"],
        )
    except Exception:
        logger.exception("[session] failed to log main_enter")

    page.title = "CatMan Pro"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.locale_configuration = ft.LocaleConfiguration(
        supported_locales=[ft.Locale("uk", "UA")],
        current_locale=ft.Locale("uk", "UA"),
    )

    # Windows/asyncio shutdown noise guard:
    # Ignore WinError 10054 (connection reset) during app exit.
    try:
        loop = asyncio.get_running_loop()

        def _loop_exc_handler(_loop, context):
            try:
                exc = context.get("exception")
                if (
                    isinstance(exc, ConnectionResetError)
                    and getattr(exc, "winerror", None) == 10054
                ):
                    return
                msg = str(context.get("message") or "")
                if "WinError 10054" in msg:
                    return
            except Exception:
                pass
            try:
                loop.default_exception_handler(context)
            except Exception:
                pass

        loop.set_exception_handler(_loop_exc_handler)
    except Exception:
        logger.exception("Failed to get running asyncio loop / set exception handler")

    try:
        if hasattr(ft, "ScrollbarTheme"):
            page.theme = ft.Theme(
                scrollbar_theme=ft.ScrollbarTheme(
                    thickness=6,
                    radius=6,
                    main_axis_margin=2,
                    cross_axis_margin=2,
                )
            )
    except Exception:
        logger.exception("Failed to apply compact scrollbar theme")

    page.window.full_screen = False
    page.window.frameless = False
    page.window.title_bar_hidden = False
    page.window.title_bar_buttons_hidden = False
    page.window.resizable = True
    page.window.movable = True
    page.window.minimizable = True
    page.window.maximizable = True
    page.window.maximized = True

    page.window.width = 450
    page.window.height = 650
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER

    _install_global_compact_dialogs(page)
    _install_global_selection_clear(page)

    probe_target = (
        _resolve_probe_target() if bool(getattr(Config, "USE_CLOUD", True)) else None
    )

    async def _enable_mobile_rotation():
        try:
            if not page.platform.is_mobile():
                return
            # All four orientations allowed — portrait used to be dropped
            # because the layout didn't fit it, but the mobile-wide 20% size
            # reduction (fonts/padding/spacing) makes portrait usable too.
            await page.set_allowed_device_orientations(
                [
                    ft.DeviceOrientation.PORTRAIT_UP,
                    ft.DeviceOrientation.PORTRAIT_DOWN,
                    ft.DeviceOrientation.LANDSCAPE_LEFT,
                    ft.DeviceOrientation.LANDSCAPE_RIGHT,
                ]
            )
        except Exception:
            logger.exception("Failed to enable mobile screen rotation")

    offline_overlay = ft.Container(
        expand=True,
        visible=False,
        bgcolor="#00000066",
        alignment=ft.Alignment(0, 0),
        content=ft.Card(
            ft.Container(
                width=420,
                padding=18,
                bgcolor="#FFF3CD",
                border_radius=12,
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Icon(
                                    ft.Icons.WIFI_OFF, color=ft.Colors.RED_700, size=26
                                ),
                                ft.Text(
                                    i18n.t(
                                        "app.offline.title",
                                        "Немає підключення до мережі",
                                    ),
                                    size=18,
                                    weight="bold",
                                ),
                            ],
                            spacing=10,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        ft.Text(
                            i18n.t(
                                "app.offline.body",
                                "Підключення відсутнє. Вікно зникне автоматично після відновлення інтернету.",
                            ),
                            size=13,
                            color="grey800",
                        ),
                    ],
                    spacing=10,
                ),
            )
        ),
    )
    page.overlay.append(offline_overlay)

    # ── Parser progress floating bar ──────────────────────────────────────────
    _prs_close_btn = ft.IconButton(
        ft.Icons.CLOSE,
        icon_color=ft.Colors.WHITE70,
        icon_size=16,
        padding=0,
    )
    _prs_spinner = ft.ProgressRing(
        width=16,
        height=16,
        stroke_width=2,
        color=ft.Colors.WHITE,
    )
    _prs_status_icon = ft.Icon(
        ft.Icons.CHECK_CIRCLE,
        color=ft.Colors.WHITE,
        size=18,
        visible=False,
    )
    _prs_title = ft.Text(
        i18n.t("app.parser_bar.running", "Парсинг..."),
        size=12,
        weight="bold",
        color=ft.Colors.WHITE,
    )
    _prs_message = ft.Text(
        "",
        size=11,
        color=ft.Colors.with_opacity(0.85, ft.Colors.WHITE),
        max_lines=2,
        no_wrap=False,
    )
    _prs_inner = ft.Container(
        width=320,
        bgcolor=ft.Colors.BLUE_GREY_900,
        border_radius=12,
        padding=ft.Padding.symmetric(horizontal=14, vertical=10),
        shadow=ft.BoxShadow(
            blur_radius=14,
            color=ft.Colors.with_opacity(0.40, ft.Colors.BLACK),
            offset=ft.Offset(0, 4),
        ),
        content=ft.Column(
            [
                ft.Row(
                    [
                        _prs_spinner,
                        _prs_status_icon,
                        _prs_title,
                        ft.Container(expand=True),
                        _prs_close_btn,
                    ],
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                _prs_message,
            ],
            spacing=4,
            tight=True,
        ),
    )
    # NOTE: this used to be wrapped in an extra expand=True Container +
    # expand=True Stack "just to host a small positioned box" — but an
    # expand=True Container covers the ENTIRE page in the hit-test tree even
    # though it paints nothing outside the small bottom-right card, so while
    # visible=True (i.e. for the whole duration of any parser run) it was
    # silently swallowing scroll-wheel input over the whole app, on every
    # tab, until the run finished. page.overlay already provides Stack-style
    # positioning for its direct children, so _prs_positioned (right/bottom
    # set) can go straight in — no full-screen wrapper needed.
    _prs_positioned = ft.Container(
        content=_prs_inner, right=16, bottom=24, visible=False
    )

    def _hide_parser_bar(_=None):
        _prs_positioned.visible = False
        with suppress(Exception):
            page.update()

    _prs_close_btn.on_click = _hide_parser_bar

    page.overlay.append(_prs_positioned)
    page._parser_is_running = False
    page._prs_overlay = _prs_positioned
    page._prs_spinner = _prs_spinner
    page._prs_status_icon = _prs_status_icon
    page._prs_title = _prs_title
    page._prs_message = _prs_message
    page._prs_inner = _prs_inner
    page._hide_parser_bar = _hide_parser_bar
    # ─────────────────────────────────────────────────────────────────────────

    async def _network_watchdog():
        if not probe_target:
            return

        host, port = probe_target
        logger.info(
            f"[bg-network] watchdog запущено → перевірка {host}:{port} кожні 2с"
        )
        fail_streak = 0
        ok_streak = 0
        was_offline = False

        while True:
            try:
                is_online = await asyncio.to_thread(_probe_network, host, port)
                if is_online:
                    ok_streak += 1
                    fail_streak = 0
                    if was_offline and ok_streak >= 2:
                        logger.info("[bg-network] з'єднання ВІДНОВЛЕНО")
                        offline_overlay.visible = False
                        page.snack_bar = ft.SnackBar(
                            ft.Text(
                                i18n.t(
                                    "app.offline.reconnected", "З'єднання відновлено"
                                ),
                                color=ft.Colors.WHITE,
                                weight="bold",
                            ),
                            bgcolor=ft.Colors.GREEN_700,
                            duration=1000,
                        )
                        page.snack_bar.open = True
                        page.update()
                        # Notify dashboard to clear session cache flags so all tabs
                        # will force a fresh DB pulse check on the next visit.
                        try:
                            cb = getattr(page, "_on_network_reconnect", None)
                            if callable(cb):
                                cb()
                        except Exception:
                            logger.exception("[bg-network] reconnect callback failed")
                        was_offline = False
                else:
                    fail_streak += 1
                    ok_streak = 0
                    if (not was_offline) and fail_streak >= 2:
                        logger.warning(
                            f"[bg-network] ОФЛАЙН ({host}:{port} недоступний)"
                        )
                        offline_overlay.visible = True
                        page.update()
                        was_offline = True
            except Exception:
                logger.exception("Network watchdog failed")

            await asyncio.sleep(2.0)

    def show_update_required_screen(min_required: str, download_url: str):
        page.clean()
        update_state = {"busy": False}
        progress_bar = ft.ProgressBar(width=320, value=0, visible=False)
        progress_text = ft.Text(
            "", size=12, color=ft.Colors.BLUE_GREY_700, visible=False
        )
        status_text = ft.Text(
            i18n.t(
                "app.update.blocked_notice", "Без оновлення вхід у систему заблоковано."
            ),
            size=13,
            color="grey700",
            text_align=ft.TextAlign.CENTER,
        )
        download_button = ft.FilledButton(
            i18n.t("app.update.download_button", "Скачати та встановити оновлення"),
            icon=ft.Icons.DOWNLOAD,
        )

        def _normalize_url(raw_url: str) -> str:
            url = (raw_url or "").strip()
            if not url:
                return ""
            try:
                parsed = urlparse(url)
                if parsed.scheme not in {"http", "https"}:
                    url = f"https://{url}"
            except Exception:
                logger.exception("Failed to normalize update URL")
            return url

        def _format_bytes(num: int) -> str:
            if num < 1024:
                return f"{num} B"
            if num < 1024 * 1024:
                return f"{num / 1024:.1f} KB"
            if num < 1024 * 1024 * 1024:
                return f"{num / (1024 * 1024):.1f} MB"
            return f"{num / (1024 * 1024 * 1024):.1f} GB"

        async def _resolve_download_url(
            client: httpx.AsyncClient, raw_url: str, min_required_version: str
        ) -> str:
            """Accept GitHub release tag URLs and convert them to direct asset URLs."""
            normalized = _normalize_url(raw_url)
            if not normalized:
                return normalized

            try:
                parsed = urlparse(normalized)
                host = (parsed.netloc or "").lower()
                path_parts = [p for p in (parsed.path or "").split("/") if p]
                if host not in {"github.com", "www.github.com"}:
                    return normalized

                # Expected: /<owner>/<repo>/releases/tag/<tag>
                if (
                    len(path_parts) >= 5
                    and path_parts[2] == "releases"
                    and path_parts[3] == "tag"
                ):
                    owner = path_parts[0]
                    repo = path_parts[1]
                    tag = path_parts[4]
                    guessed_version = (
                        min_required_version or ""
                    ).strip() or tag.lstrip("v")
                    candidates = [
                        f"https://github.com/{owner}/{repo}/releases/download/{tag}/CatManPro-Setup-{guessed_version}.exe",
                        f"https://github.com/{owner}/{repo}/releases/download/{tag}/CatManPro-Setup.exe",
                        f"https://github.com/{owner}/{repo}/releases/download/{tag}/CatManPro.exe",
                    ]

                    for candidate in candidates:
                        with suppress(Exception):
                            head = await client.head(candidate)
                            if int(getattr(head, "status_code", 0)) < 400:
                                return candidate

                    # Return the most likely direct URL so stream() gets the actual error.
                    return candidates[0]
            except Exception:
                logger.exception("Failed to resolve GitHub release URL")

            return normalized

        def _start_installer_and_exit(installer_path: str) -> None:
            # Visible installer gives user progress/UAC and avoids hidden silent hangs.
            subprocess.Popen(
                [
                    installer_path,
                    "/SP-",
                    "/NORESTART",
                    "/CLOSEAPPLICATIONS",
                    "/FORCECLOSEAPPLICATIONS",
                ],
                close_fds=True,
            )
            # Important: free locks immediately so setup can replace files and relaunch cleanly.
            os._exit(0)

        async def _download_and_install(url: str) -> None:
            update_state["busy"] = True
            download_button.disabled = True
            progress_bar.visible = True
            progress_text.visible = True
            progress_bar.value = 0
            status_text.value = i18n.t(
                "app.update.preparing", "Підготовка до завантаження..."
            )
            page.update()

            try:
                target_dir = os.path.join(tempfile.gettempdir(), "CatManProUpdate")
                os.makedirs(target_dir, exist_ok=True)
                installer_path = os.path.join(target_dir, "CatManPro-Setup-latest.exe")

                timeout = httpx.Timeout(connect=20.0, read=None, write=None, pool=None)
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=timeout
                ) as client:
                    status_text.value = i18n.t(
                        "app.update.searching", "Пошук файлу оновлення..."
                    )
                    page.update()

                    resolved_url = await _resolve_download_url(
                        client, url, min_required
                    )

                    async with client.stream("GET", resolved_url) as resp:
                        resp.raise_for_status()

                        content_type = (resp.headers.get("content-type") or "").lower()
                        final_url = str(resp.url)
                        if "text/html" in content_type:
                            raise RuntimeError(
                                i18n.t(
                                    "app.update.err_not_direct_url",
                                    "URL оновлення не є прямим посиланням на .exe. Вкажіть прямий asset URL з GitHub Releases.",
                                )
                            )

                        total_bytes = 0
                        try:
                            total_bytes = int(resp.headers.get("content-length") or "0")
                        except Exception:
                            total_bytes = 0

                        downloaded = 0
                        last_ui_update = 0.0
                        status_text.value = i18n.t(
                            "app.update.downloading", "Завантаження оновлення..."
                        )
                        if total_bytes <= 0:
                            progress_bar.value = None

                        with open(installer_path, "wb") as f:
                            async for chunk in resp.aiter_bytes(chunk_size=1024 * 128):
                                if not chunk:
                                    continue
                                f.write(chunk)
                                downloaded += len(chunk)

                                now = time.monotonic()
                                if now - last_ui_update >= 0.12:
                                    if total_bytes > 0:
                                        ratio = max(
                                            0.0, min(1.0, downloaded / total_bytes)
                                        )
                                        progress_bar.value = ratio
                                        progress_text.value = f"{int(ratio * 100)}% ({_format_bytes(downloaded)} / {_format_bytes(total_bytes)})"
                                    else:
                                        progress_text.value = i18n.t(
                                            "app.update.downloaded_amount",
                                            "Завантажено: {amount}",
                                        ).format(amount=_format_bytes(downloaded))
                                    page.update()
                                    last_ui_update = now

                        if total_bytes > 0:
                            progress_bar.value = 1.0
                            progress_text.value = f"100% ({_format_bytes(downloaded)} / {_format_bytes(total_bytes)})"
                        else:
                            progress_text.value = i18n.t(
                                "app.update.downloaded_amount", "Завантажено: {amount}"
                            ).format(amount=_format_bytes(downloaded))

                        if downloaded < 1024 * 300:
                            raise RuntimeError(
                                i18n.t(
                                    "app.update.err_file_too_small",
                                    "Файл оновлення занадто малий. Перевірте, що посилання веде прямо на .exe інсталятор.",
                                )
                            )

                        if (not final_url.lower().endswith(".exe")) and (
                            "application/octet-stream" not in content_type
                        ):
                            logger.warning(
                                "Update file may be non-executable | final_url=%s | content_type=%s",
                                final_url,
                                content_type,
                            )

                status_text.value = i18n.t(
                    "app.update.installing", "Запуск установки..."
                )
                progress_text.value = i18n.t(
                    "app.update.installer_launched",
                    "Інсталятор запущено. Підтвердіть UAC і дочекайтесь завершення.",
                )
                page.update()
                await asyncio.sleep(0.35)

                _start_installer_and_exit(installer_path)
            except httpx.HTTPStatusError as exc:
                logger.exception("Update download/install failed with HTTP error")
                status = getattr(getattr(exc, "response", None), "status_code", None)
                req_url = str(getattr(getattr(exc, "request", None), "url", "") or "")
                if status == 404 and "github.com" in req_url.lower():
                    message = i18n.t(
                        "app.update.err_github_404",
                        "GitHub повернув 404. Ймовірно, посилання не на asset .exe "
                        "або репозиторій приватний. Для автооновлення потрібен публічний "
                        "прямий URL на інсталятор.",
                    )
                else:
                    message = i18n.t(
                        "app.update.err_http", "HTTP помилка {status}: {exc}"
                    ).format(status=status or "?", exc=exc)

                status_text.value = i18n.t(
                    "app.update.err_install_failed", "Не вдалося встановити оновлення"
                )
                progress_text.value = message
                page.snack_bar = ft.SnackBar(
                    ft.Text(message),
                    bgcolor=ft.Colors.RED_700,
                    duration=4200,
                )
                page.snack_bar.open = True
                page.update()
                update_state["busy"] = False
                download_button.disabled = False
            except Exception as exc:
                logger.exception("Update download/install failed")
                status_text.value = i18n.t(
                    "app.update.err_install_failed", "Не вдалося встановити оновлення"
                )
                progress_text.value = str(exc)
                page.snack_bar = ft.SnackBar(
                    ft.Text(
                        i18n.t(
                            "app.update.err_generic", "Помилка оновлення: {exc}"
                        ).format(exc=exc)
                    ),
                    bgcolor=ft.Colors.RED_700,
                    duration=3500,
                )
                page.snack_bar.open = True
                page.update()
                update_state["busy"] = False
                download_button.disabled = False

        async def open_download(_):
            if update_state["busy"]:
                return

            url = _normalize_url(download_url)
            if not url:
                page.snack_bar = ft.SnackBar(
                    ft.Text(
                        i18n.t(
                            "app.update.err_no_link", "Посилання на оновлення відсутнє"
                        )
                    ),
                    bgcolor=ft.Colors.RED_700,
                    duration=1800,
                )
                page.snack_bar.open = True
                page.update()
                return

            if page.platform.is_mobile():
                # The desktop flow below downloads the file itself and then
                # subprocess.Popen()'s the Inno Setup installer with silent-
                # install flags — none of that exists on Android. Handing the
                # .apk URL to the OS instead lets it download and prompt its
                # own "Install this app?" dialog, which is the only install
                # path available on the platform anyway.
                with suppress(Exception):
                    await page.launch_url(url)
                return

            page.run_task(_download_and_install, url)

        download_button.on_click = open_download

        page.add(
            ft.Container(
                expand=True,
                content=ft.Column(
                    [
                        ft.Card(
                            ft.Container(
                                width=560,
                                padding=20,
                                content=ft.Column(
                                    [
                                        ft.Icon(
                                            ft.Icons.SYSTEM_UPDATE_ALT,
                                            size=42,
                                            color=ft.Colors.BLUE_700,
                                        ),
                                        ft.Text(
                                            i18n.t(
                                                "app.update.title",
                                                "Потрібне оновлення застосунку",
                                            ),
                                            size=24,
                                            weight="bold",
                                        ),
                                        ft.Text(
                                            i18n.t(
                                                "app.update.version_info",
                                                "Поточна версія: {current}\nМінімально допустима: {required}",
                                            ).format(
                                                current=Config.APP_VERSION,
                                                required=min_required,
                                            ),
                                            size=14,
                                            text_align=ft.TextAlign.CENTER,
                                        ),
                                        status_text,
                                        progress_bar,
                                        progress_text,
                                        ft.Row(
                                            [download_button],
                                            alignment=ft.MainAxisAlignment.CENTER,
                                        ),
                                    ],
                                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                    spacing=14,
                                ),
                            )
                        )
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            )
        )
        page.update()

    _login_transition_busy = {"value": False}
    _session_state = {
        "authenticated": False,
        "login_screen_open": False,
    }
    _allowed_login_screen_reasons = {
        "app_boot",
        "logout_button_click",
        "account_switch_confirmed",
        "system_backup_lock",
        "session_kicked",
    }
    _manual_auth_reasons = {
        "logout_button_click",
        "account_switch_confirmed",
        "system_backup_lock",
        "session_kicked",
    }

    def _install_page_lifecycle_logs():
        def _safe_log(tag: str, extra: str = ""):
            try:
                m = _diag_session_meta()
                logger.info(
                    "[session] %s | runtime_id=%s | session_id=%s | page_id=%s | pid=%s | authenticated=%s | login_open=%s%s",
                    tag,
                    m["runtime_id"],
                    m["session_id"],
                    m["page_id"],
                    m["pid"],
                    bool(_session_state.get("authenticated")),
                    bool(_session_state.get("login_screen_open")),
                    extra,
                )
            except Exception:
                logger.exception("[session] lifecycle log failed | tag=%s", tag)

        def _on_disconnect(_e):
            _safe_log("disconnect")

        def _on_connect(_e):
            _safe_log("connect")

        def _on_window_event(e):
            try:
                data = str(getattr(e, "data", "") or "")
            except Exception:
                data = ""
            _safe_log("window_event", extra=f" | data={data}")

        def _on_error(e):
            try:
                data = str(getattr(e, "data", "") or "")
            except Exception:
                data = ""
            _safe_log("page_error", extra=f" | data={data}")

        try:
            if hasattr(page, "on_disconnect"):
                page.on_disconnect = _on_disconnect
            if hasattr(page, "on_connect"):
                page.on_connect = _on_connect
            if hasattr(page, "on_window_event"):
                page.on_window_event = _on_window_event
            if hasattr(page, "on_error"):
                page.on_error = _on_error
            _safe_log("lifecycle_handlers_installed")
        except Exception:
            logger.exception("[session] failed to install lifecycle handlers")

    _install_page_lifecycle_logs()

    def _consume_manual_logout_intent(expected_reason: str) -> bool:
        try:
            intent_reason = str(
                getattr(page, "_manual_logout_intent_reason", "") or ""
            ).strip()
            intent_ts = float(getattr(page, "_manual_logout_intent_ts", 0.0) or 0.0)
            now_ts = float(time.time())
            is_fresh = (now_ts - intent_ts) <= 8.0
            ok = bool(intent_reason == expected_reason and is_fresh)
        except Exception:
            ok = False
        try:
            page._manual_logout_intent_reason = ""
            page._manual_logout_intent_ts = 0.0
        except Exception:
            pass
        return ok

    def show_login_screen(reason="unknown", transition_token=None):
        reason_norm = str(reason or "unknown").strip()
        active_view = str(getattr(page, "_active_view", "") or "").strip()
        has_authenticated_once = bool(
            getattr(page, "_has_authenticated_session_once", False)
        )
        try:
            stack_hint = " | ".join(
                line.strip() for line in traceback.format_stack(limit=5)[-4:]
            )
        except Exception:
            stack_hint = ""
        logger.info(
            "[auth-nav] request_show_login | reason=%s | active_view=%s | authenticated=%s | login_open=%s | has_once=%s | manual_intent=%s | runtime_id=%s | session_id=%s | pid=%s | stack=%s",
            reason_norm,
            active_view,
            bool(_session_state.get("authenticated")),
            bool(_session_state.get("login_screen_open")),
            has_authenticated_once,
            str(getattr(page, "_manual_logout_intent_reason", "") or ""),
            runtime_id,
            str(getattr(page, "session_id", "") or ""),
            os.getpid(),
            stack_hint,
        )

        # Global hard guard after first successful auth:
        # never allow implicit redirects to login, only explicit logout/account switch.
        if has_authenticated_once and reason_norm not in _manual_auth_reasons:
            logger.error(
                "[auth-nav] blocked implicit login-screen transition after auth | reason=%s | active_view=%s",
                reason_norm,
                active_view,
            )
            return

        if reason_norm in _manual_auth_reasons and not _consume_manual_logout_intent(
            reason_norm
        ):
            logger.error(
                "[auth-nav] blocked manual transition without intent | reason=%s | active_view=%s",
                reason_norm,
                active_view,
            )
            return

        if reason_norm not in _allowed_login_screen_reasons:
            logger.error(
                "[auth-nav] blocked unexpected login-screen transition | reason=%s | active_view=%s",
                reason_norm,
                active_view,
            )
            return
        if reason_norm == "app_boot" and bool(_session_state.get("authenticated")):
            logger.error(
                "[auth-nav] blocked app_boot while authenticated | active_view=%s",
                active_view,
            )
            return
        if reason_norm in {"logout_button_click", "account_switch_confirmed"}:
            expected_token = str(
                getattr(page, "_auth_transition_token", "") or ""
            ).strip()
            incoming_token = str(transition_token or "").strip()
            if not expected_token or incoming_token != expected_token:
                logger.error(
                    "[auth-nav] blocked invalid token | reason=%s | active_view=%s",
                    reason_norm,
                    active_view,
                )
                return
            try:
                page._auth_transition_token = ""
            except Exception:
                pass
        if bool(_session_state.get("login_screen_open")):
            logger.warning(
                "[auth-nav] suppressed duplicate login-screen show | reason=%s",
                reason_norm,
            )
            return
        try:
            _session_state["login_screen_open"] = True
            _session_state["authenticated"] = False
            try:
                page.window.maximized = True
                page.window.full_screen = False
            except Exception:
                pass
            page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
            page.vertical_alignment = ft.MainAxisAlignment.START
            page.bgcolor = None
            auth = AuthScreen(page, on_success=on_login_success)
            auth.show_login()
        except Exception as ex:
            _session_state["login_screen_open"] = False
            logger.exception("Failed to show login screen")
            page.clean()
            page.add(
                ft.Text(
                    i18n.t(
                        "app.error.return_to_login",
                        "Помилка повернення до входу: {exc}",
                    ).format(exc=ex),
                    color="red",
                )
            )
            page.update()

    def on_login_success(user_data, source="interactive"):
        if _login_transition_busy["value"]:
            logger.warning(
                "[auth-nav] on_login_success skipped because transition busy | runtime_id=%s | session_id=%s | pid=%s",
                runtime_id,
                str(getattr(page, "session_id", "") or ""),
                os.getpid(),
            )
            return

        source_norm = str(source or "interactive").strip().lower()
        logger.info(
            "[auth-nav] on_login_success_start | source=%s | user_id=%s | role=%s | runtime_id=%s | session_id=%s | pid=%s",
            source_norm,
            str(user_data.get("user_id") or user_data.get("id") or ""),
            str(user_data.get("role") or ""),
            runtime_id,
            str(getattr(page, "session_id", "") or ""),
            os.getpid(),
        )

        _login_transition_busy["value"] = True
        _session_state["login_screen_open"] = False
        _session_state["authenticated"] = True
        try:
            page._manual_logout_intent_reason = ""
            page._manual_logout_intent_ts = 0.0
            page._has_authenticated_session_once = True
        except Exception:
            pass

        async def _stabilize_window_size():
            # Some crash/rebuild paths in Flet can restore a non-maximized window.
            for delay in (0.0, 0.05, 0.15, 0.4, 0.9):
                await asyncio.sleep(delay)
                try:
                    page.window.maximized = True
                    page.window.full_screen = False
                    page.update()
                except Exception:
                    pass

        async def _login_flow():
            try:
                logger.info(
                    "[auth-nav] login_flow_enter | source=%s | runtime_id=%s | session_id=%s | pid=%s",
                    source_norm,
                    runtime_id,
                    str(getattr(page, "session_id", "") or ""),
                    os.getpid(),
                )
                try:
                    page.run_task(_stabilize_window_size)
                except Exception:
                    pass
                # Remote mode: only SuperAdmin can edit. Others get view-only dashboard.
                if Config.REMOTE_MODE and (
                    not _is_superadmin_role(user_data.get("role"))
                ):
                    user_data["subordinate_view_readonly"] = True
                    user_data["remote_mode"] = True
                    page.window.maximized = True
                    page.window.full_screen = False
                    page.update()
                    dash = MainDashboard(
                        page,
                        user_data,
                        on_logout=(
                            lambda reason="unknown", transition_token=None: show_login_screen(
                                reason, transition_token=transition_token
                            )
                        ),
                        on_switch_account=on_login_success,
                    )
                    dash.build()
                    page.update()
                    try:
                        page.run_task(_stabilize_window_size)
                    except Exception:
                        pass
                    return

                if _is_superadmin_role(user_data.get("role")):
                    page.window.maximized = True
                    page.window.full_screen = False
                    dash = SuperAdminDashboard(
                        page,
                        user_data,
                        on_logout=(
                            lambda reason="unknown", transition_token=None: show_login_screen(
                                reason, transition_token=transition_token
                            )
                        ),
                        on_switch_account=on_login_success,
                    )
                    dash.build()
                    page.update()
                    try:
                        page.run_task(_stabilize_window_size)
                    except Exception:
                        pass
                    # Start GSM parser local scheduler for SA
                    try:
                        import gsm_scheduler

                        _sched_db = DataManager()

                        def _get_sched_cfg() -> dict:
                            with suppress(Exception):
                                sched = _sched_db.get_gsm_parser_schedule()
                                svc = _sched_db.get_parser_service_config()
                                return {**sched, **svc}
                            return {}

                        gsm_scheduler.start(_get_sched_cfg)
                        logger.info("[Scheduler] GSM parser scheduler started")
                    except Exception:
                        logger.exception("[Scheduler] Failed to start GSM scheduler")
                    return

                if _is_admin_role(user_data.get("role")):
                    page.window.maximized = True
                    page.window.full_screen = False
                    dash = AdminDashboard(
                        page,
                        user_data,
                        on_logout=(
                            lambda reason="unknown", transition_token=None: show_login_screen(
                                reason, transition_token=transition_token
                            )
                        ),
                        on_switch_account=on_login_success,
                    )
                    dash.build()
                    page.update()
                    try:
                        page.run_task(_stabilize_window_size)
                    except Exception:
                        pass
                    return

                page.window.maximized = True
                page.window.full_screen = False
                page.update()

                dash = MainDashboard(
                    page,
                    user_data,
                    on_logout=(
                        lambda reason="unknown", transition_token=None: show_login_screen(
                            reason, transition_token=transition_token
                        )
                    ),
                    on_switch_account=on_login_success,
                )
                dash.build()
                page.update()
                try:
                    page.run_task(_stabilize_window_size)
                except Exception:
                    pass
            except Exception:
                logger.exception("Login flow failed | source=%s", source_norm)
                _session_state["authenticated"] = False
                try:
                    page.run_task(_stabilize_window_size)
                except Exception:
                    pass
                show_login_screen(reason="app_boot")
            finally:
                logger.info(
                    "[auth-nav] login_flow_exit | source=%s | authenticated=%s | login_open=%s | runtime_id=%s | session_id=%s | pid=%s",
                    source_norm,
                    bool(_session_state.get("authenticated")),
                    bool(_session_state.get("login_screen_open")),
                    runtime_id,
                    str(getattr(page, "session_id", "") or ""),
                    os.getpid(),
                )
                _login_transition_busy["value"] = False

        page.run_task(_login_flow)

    async def _check_app_update_policy():
        # Runs AFTER the login screen is already on screen. This used to be
        # a synchronous DataManager().get_app_update_policy() call sitting
        # right before show_login_screen(), i.e. a full Postgres round trip
        # (connect + schema check + query) that blocked the very first paint
        # — exactly the "довгий чорний екран" symptom. The version gate only
        # needs to block LOGIN, not the initial render, so it now runs in the
        # background and swaps in the update screen if/when it resolves.
        try:
            policy = await asyncio.to_thread(
                lambda: DataManager().get_app_update_policy()
            )
            min_required = (policy.get("min_required") or "").strip()
            if page.platform.is_mobile():
                download_url = (policy.get("download_url_android") or "").strip() or (
                    policy.get("download_url") or ""
                ).strip()
            else:
                download_url = (policy.get("download_url") or "").strip() or (
                    Config.UPDATE_DOWNLOAD_URL or ""
                ).strip()
            if min_required and _version_is_outdated(Config.APP_VERSION, min_required):
                show_update_required_screen(min_required, download_url)
        except Exception:
            logger.exception("Failed to check app update policy")

    try:
        show_login_screen(reason="app_boot")
    except Exception:
        logger.exception("Unhandled error in main")

    page.run_task(_check_app_update_policy)
    page.run_task(_enable_mobile_rotation)
    page.run_task(_network_watchdog)


if __name__ == "__main__":
    _install_global_crash_hooks()
    _lock = _acquire_single_instance_lock()
    if _lock is None:
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showwarning(
                "CatMan Pro",
                "Програма вже запущена!\n\nЗнайдіть вікно CatMan Pro на панелі задач.",
            )
            root.destroy()
        except Exception:
            logger.exception("Failed to show already-running warning via tkinter")
        sys.exit(0)

    try:
        logger.info(
            "[session] process_boot | pid=%s | python=%s | cwd=%s",
            os.getpid(),
            sys.executable,
            os.getcwd(),
        )
        ft.run(main)
    finally:
        # Never leave an orphaned local Postgres process running on a
        # non-technical user's machine after a clean app exit (Локал storage
        # mode, see local_pg_manager.py). No-op if no local clusters exist.
        with suppress(Exception):
            local_pg_manager.stop_all()
        _lock.close()
