"""CatMan Калькулятори — browser (web) version.

Runs entirely client-side via Flet's Pyodide/WebAssembly build
(`flet publish` — see scripts/build_calculator_web.ps1), hosted as plain
static files on GitHub Pages. No Python server involved anywhere, which
shapes everything below:

  - No `database`/psycopg import anywhere in this file, directly or
    indirectly — psycopg is a compiled C extension and browsers can only
    run WebAssembly/JS, so a real Postgres connection is simply not
    possible here. calculator_app.py (the Windows installer) is
    unaffected — it keeps talking to Postgres normally.

  - The password is a plain hardcoded comparison (_WEB_PASSWORD below),
    not a live DB check. Change it and republish to change or revoke
    access — deliberately independent from the desktop app's password
    (SuperAdmin → "Калькулятор" → "Пароль для веб-версії" only labels
    what the DESKTOP password page shows; it doesn't reach this file).

  - "Saved calculations" are shared across every visitor of the web
    version. Reads go straight to the public raw.githubusercontent.com
    URL (no auth needed for GET on a public file). Writes go through
    GitHub's Contents API using _DATA_TOKEN below — that token is
    deliberately scoped to ONLY this one small, isolated data repo
    (catman-calculator-web), never the real CatMan-Pro repo, so even if
    someone extracts it from the page source (anyone can — it ships in
    the client-side bundle), the worst they can do is mess with other
    people's saved calculator templates, nothing about the actual
    product, site, or releases.

NOTE: the pyodide.http.pyfetch calls below follow Pyodide's documented
fetch-style API, but this file has not yet been exercised inside a real
browser — Pyodide is only present at runtime inside the built web bundle,
not in this desktop dev venv, so there was no way to test it here first.
Treat the shared-saves read/write path as the one part of this file that
may need a live debugging pass once actually opened in a browser.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import time

import flet as ft

import i18n
from logger import logger
from views import calculators_view as _calc
from views.calculators_view import open_calculators_modal, set_web_standalone_mode

_AUTH_PREF_KEY = "catman_calc_authed"


def _pw_fingerprint(pw: str) -> str:
    """Hash of a password value, stored/compared instead of a bare
    True/False flag. A bare flag (the old behavior) never got invalidated
    by changing _WEB_PASSWORD — every browser that had ever logged in
    stayed logged in forever, even after the password changed, which
    defeated the documented "change it and republish to revoke access"
    promise at the top of this file. Comparing fingerprints means a
    changed-and-republished password immediately requires every browser
    to re-enter it, restoring actual revocability."""
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()

APP_VERSION = "1.0.5"

# Fallback only — used when the remote access file (see _ACCESS_RAW_URL
# below) can't be reached at all, so a GitHub/network hiccup never locks
# every visitor out. The password SuperAdmin actually manages lives in
# access/calculator_web_access.json in the catman-calculator-web repo —
# see _fetch_remote_password_hash. Changing THIS constant no longer does
# anything by itself; it only matters as the emergency fallback value.
_WEB_PASSWORD = "111111"

# Scoped ONLY to catman-calculator-web's Contents (read/write) — see the
# module docstring for why this must never be a token with any access to
# the real CatMan-Pro repo.
_DATA_OWNER = "vitalik19970527-prog"
_DATA_REPO = "catman-calculator-web"
_DATA_BRANCH = "main"
_DATA_PATH = "saves/calculator_saved.json"
_DATA_TOKEN = "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"

_RAW_URL = (
    f"https://raw.githubusercontent.com/{_DATA_OWNER}/{_DATA_REPO}/"
    f"{_DATA_BRANCH}/{_DATA_PATH}"
)
_API_URL = f"https://api.github.com/repos/{_DATA_OWNER}/{_DATA_REPO}/contents/{_DATA_PATH}"

# Access-password file — public GET only, no token needed to read (this app
# never writes it; only SuperAdmin does, from the desktop app, using its
# own separate credentials that never ship in this browser bundle). Kept in
# the same low-blast-radius repo as the shared saves, not the real
# CatMan-Pro repo.
_ACCESS_PATH = "access/calculator_web_access.json"
_ACCESS_RAW_URL = (
    f"https://raw.githubusercontent.com/{_DATA_OWNER}/{_DATA_REPO}/"
    f"{_DATA_BRANCH}/{_ACCESS_PATH}"
)


async def _fetch_remote_password_hash() -> str | None:
    """Public, unauthenticated GET of the current password's fingerprint.
    Returns None (never an empty string) on any failure — callers must
    treat that as "couldn't verify remotely" and fall back to
    _pw_fingerprint(_WEB_PASSWORD), not as "no password required"."""
    try:
        from pyodide.http import pyfetch

        resp = await asyncio.wait_for(
            pyfetch(f"{_ACCESS_RAW_URL}?_ts={int(time.time())}"), timeout=5.0
        )
        if not resp.ok:
            return None
        text = await asyncio.wait_for(resp.string(), timeout=5.0)
        data = json.loads(text)
        h = str((data or {}).get("password_hash") or "").strip()
        return h or None
    except Exception:
        return None

# Populated once at startup by _fetch_shared_saves(), then read/written
# synchronously by _WebDB — see that class for why this two-step
# (async prefetch, then sync in-memory access) shape is needed at all.
_shared_saves_cache: dict = {"prefs": {}, "sha": None, "loaded": False}


async def _fetch_shared_saves() -> None:
    """Pre-loads the shared saved-calculations file once at startup, before
    the picker/password gate ever calls get_manager_ui_preference — that
    method must stay synchronous (existing calculators_view.py code calls
    it with no await), so the actual network fetch has to happen earlier,
    not lazily on first read.

    Both fetches are wrapped in asyncio.wait_for with an explicit timeout,
    not just contextlib.suppress(Exception) — suppress only catches an
    exception that's actually raised, and a pyfetch() call that never
    resolves (dropped connection, CORS preflight stall, GitHub API hiccup)
    raises nothing at all. _startup() awaits this function before the
    password gate or the app itself ever renders, so an unbounded hang
    here is a permanent blank screen — confirmed live: this was the actual
    cause of the startup hang, not shared_preferences (which a prior
    hotfix guarded instead, without fixing anything, and was reverted)."""
    with contextlib.suppress(Exception):
        from pyodide.http import pyfetch

        # Cache-bust: GitHub's raw CDN can serve a stale copy for a while
        # after a write otherwise.
        resp = await asyncio.wait_for(
            pyfetch(f"{_RAW_URL}?_ts={id(_shared_saves_cache)}"), timeout=5.0
        )
        if resp.ok:
            text = await asyncio.wait_for(resp.string(), timeout=5.0)
            with contextlib.suppress(Exception):
                _shared_saves_cache["prefs"] = json.loads(text) or {}

    with contextlib.suppress(Exception):
        from pyodide.http import pyfetch

        # Contents API read (authenticated) — only to learn the file's
        # current sha, which PUT needs to update an existing file. Doesn't
        # touch the raw CDN path above, so it can't return a stale sha.
        resp = await asyncio.wait_for(
            pyfetch(
                _API_URL,
                method="GET",
                headers={
                    "Authorization": f"Bearer {_DATA_TOKEN}",
                    "Accept": "application/vnd.github+json",
                },
            ),
            timeout=5.0,
        )
        if resp.ok:
            data = json.loads(await asyncio.wait_for(resp.string(), timeout=5.0))
            _shared_saves_cache["sha"] = data.get("sha")

    _shared_saves_cache["loaded"] = True


async def _push_shared_saves(prefs: dict) -> None:
    """Fire-and-forget background push, kicked off from _WebDB's
    (synchronous) set_manager_ui_preference via page.run_task — see there."""
    try:
        from pyodide.http import pyfetch

        content_str = json.dumps(prefs, ensure_ascii=False)
        payload = {
            "message": "Update shared calculator saves",
            "content": base64.b64encode(content_str.encode("utf-8")).decode("ascii"),
            "branch": _DATA_BRANCH,
        }
        if _shared_saves_cache.get("sha"):
            payload["sha"] = _shared_saves_cache["sha"]
        resp = await asyncio.wait_for(
            pyfetch(
                _API_URL,
                method="PUT",
                headers={
                    "Authorization": f"Bearer {_DATA_TOKEN}",
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                },
                body=json.dumps(payload),
            ),
            timeout=8.0,
        )
        if resp.ok:
            data = json.loads(await asyncio.wait_for(resp.string(), timeout=5.0))
            _shared_saves_cache["sha"] = (data.get("content") or {}).get("sha")
        else:
            logger.warning(
                "CatMan Калькулятори (web): push shared saves failed, status=%s",
                resp.status,
            )
    except Exception:
        logger.exception("CatMan Калькулятори (web): failed to push shared saves")


class _WebDB:
    """Same minimal interface as calculator_app.py's _StandaloneDB — only
    the methods the calculators actually call. Delivery methods/price
    tiers stay editable_name (no company data on the web either); saved
    calculations go through the shared GitHub-backed cache above instead
    of a local file, and are visible to every visitor of the web version."""

    def __init__(self, page: ft.Page):
        self._page = page

    def get_manager_ui_preference(self, manager_id, key, default=""):
        return _shared_saves_cache["prefs"].get(key, default)

    def set_manager_ui_preference(self, manager_id, key, value):
        _shared_saves_cache["prefs"][key] = value
        # set_manager_ui_preference is called synchronously by existing,
        # shared calculators_view.py code (no await at the call site) —
        # the actual GitHub write has to happen in the background instead
        # of blocking here, since a real network call can't be synchronous
        # in a browser. The in-memory cache above is already updated, so
        # this session's own UI sees the change immediately regardless of
        # whether the background push has landed yet.
        with contextlib.suppress(Exception):
            self._page.run_task(_push_shared_saves, dict(_shared_saves_cache["prefs"]))

    def get_translations_for_lang(self, lang_code: str) -> dict:
        return i18n.get_static_seed_translations(lang_code)

    def get_manager_workspace_state(self, manager_id):
        return {}

    def save_manager_workspace_state(self, manager_id, state) -> bool:
        return True

    # ── Delivery methods (Собівартість / С/В+CLO) — same as the desktop
    # standalone app: no real company data, so the manager types their own
    # delivery names via the editable_name/dynamic-add UI. ──────────────
    @staticmethod
    def _default_delivery_methods() -> list[dict]:
        return [
            {
                "id": "method_1", "name": "", "effective_price_per_kg": 0,
                "effective_price_currency": "USD", "user_enabled": True,
                "editable_name": True,
            }
        ]

    def get_effective_delivery_methods_for_manager(self, *args, **kwargs):
        return self._default_delivery_methods()

    def get_company_delivery_methods(self, *args, **kwargs):
        return self._default_delivery_methods()

    def get_manager_delivery_overrides(self, *args, **kwargs):
        return {}

    def save_manager_delivery_overrides(self, *args, **kwargs):
        pass

    # ── Price tiers / regions — none configured on the web either ──────
    def get_manager_region_price_tiers_overrides(self, *args, **kwargs):
        return []

    def get_region_price_tiers(self, *args, **kwargs):
        return []

    def get_company_regions(self, *args, **kwargs):
        return []


def _install_fullscreen_dialogs() -> None:
    """Same fullscreen-panel override as the desktop standalone app (see
    calculator_app.py) — the web version is a single-purpose page too, so
    the floating/draggable panel design (built for hovering over the rest
    of CatMan Pro) doesn't apply here either."""

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
                body.append(
                    ft.Container(
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
                    )
                )
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


def _show_password_gate(page: ft.Page, on_success, expected_hash: str) -> None:
    """Fullscreen password prompt. `expected_hash` is resolved ONCE by the
    caller (_startup) — either the live value fetched from
    access/calculator_web_access.json in catman-calculator-web (SuperAdmin-
    editable, takes effect immediately, no rebuild), or, only if that fetch
    genuinely failed, the local _WEB_PASSWORD fallback fingerprint."""
    pw_field = ft.TextField(
        label="Пароль",
        password=True,
        can_reveal_password=True,
        width=260,
        autofocus=True,
    )
    status_text = ft.Text("", size=12)

    async def _try_login(_=None):
        pw = (pw_field.value or "").strip()
        if not pw:
            status_text.value = "Введіть пароль"
            status_text.color = "#B45309"
            with contextlib.suppress(Exception):
                page.update()
            return
        if _pw_fingerprint(pw) == expected_hash:
            # Persisted (survives a hard reload) so the top-right refresh
            # button (see calculators_view.py's _close_or_refresh_btn)
            # never bounces the user back to this gate — only _startup's
            # own check below reads it back. Stores a fingerprint of the
            # password that was actually entered, not a bare flag — see
            # _pw_fingerprint's docstring for why.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    page.shared_preferences.set(
                        _AUTH_PREF_KEY, _pw_fingerprint(pw)
                    ),
                    timeout=2.0,
                )
            with contextlib.suppress(Exception):
                page.overlay.clear()
            on_success()
            return
        status_text.value = "Невірний пароль"
        status_text.color = "#DC2626"
        pw_field.value = ""
        with contextlib.suppress(Exception):
            page.update()

    pw_field.on_submit = _try_login
    login_btn = ft.FilledButton(
        "Увійти", icon=ft.Icons.LOGIN, width=260, on_click=_try_login,
    )

    page.controls = [
        ft.Column(
            [
                ft.Icon(ft.Icons.LOCK_OUTLINE, size=40, color="#1E40AF"),
                ft.Text("CatMan Калькулятори (веб)", size=18, weight="bold"),
                ft.Text("Введіть пароль доступу", size=12, color="#64748B"),
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


def main(page: ft.Page):
    set_web_standalone_mode(True)
    _install_fullscreen_dialogs()

    page.title = f"CatMan Калькулятори {APP_VERSION} (веб)"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.vertical_alignment = ft.MainAxisAlignment.CENTER
    page.padding = 0

    def _start_app(_=None):
        db = _WebDB(page)
        i18n.init_i18n(db)
        i18n.load_manager_language(db, "local")

        async def _fx_auto_refresh_loop():
            # Desktop refreshes lazily whenever a calculator dialog reopens
            # with a stale (>15 min) cache — no explicit timer needed there,
            # since threading.Thread makes that refresh cheap/invisible. The
            # web build has no threads, so without this loop the FX cache
            # would only ever refresh at the moment someone happens to open
            # a Собівартість/Рентабельність dialog; this keeps it warm even
            # while the picker/other screens are open, matching desktop's
            # "just always has current rates" feel.
            while True:
                _calc._fetch_uah_rates(page)
                await asyncio.sleep(1800)

        page.run_task(_fx_auto_refresh_loop)

        def _open_picker(_=None):
            open_calculators_modal(
                page, db, manager_id="local", company_id="", region_id="", regions_list=[]
            )

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
        _open_picker()

    async def _startup():
        # Shared saves must be loaded BEFORE the picker can open "Збережені"
        # correctly — see _fetch_shared_saves' own docstring for why this
        # can't just happen lazily inside get_manager_ui_preference.
        await _fetch_shared_saves()

        # Live password fingerprint, fetched fresh on every startup — so a
        # password SuperAdmin just changed takes effect for every visitor's
        # very next visit, not "whenever this file next gets rebuilt and
        # republished" (the old, purely-local-constant behavior). Falls
        # back to the local constant only if the remote file genuinely
        # couldn't be reached (see _fetch_remote_password_hash's docstring)
        # — a network hiccup must never lock every visitor out.
        expected_hash = await _fetch_remote_password_hash()
        if expected_hash is None:
            expected_hash = _pw_fingerprint(_WEB_PASSWORD)

        authed = False
        with contextlib.suppress(Exception):
            stored_fingerprint = await asyncio.wait_for(
                page.shared_preferences.get(_AUTH_PREF_KEY), timeout=2.0
            )
            authed = bool(stored_fingerprint) and stored_fingerprint == expected_hash
        if authed:
            _start_app()
        else:
            _show_password_gate(page, _start_app, expected_hash)

    page.run_task(_startup)


if __name__ == "__main__":
    ft.run(main)
