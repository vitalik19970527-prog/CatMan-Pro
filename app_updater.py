"""Shared auto-update mechanism for every CatMan desktop app (CatMan Pro,
CatMan SupAdm, CatMan Калькулятори, CatManGSMParser) — extracted out of
main.py so all four call the SAME code instead of drifting. The Calculator's
old updater only opened a browser link to the download page instead of
actually downloading+installing (calculator_app.py's old
_show_update_required_screen/_check_calculator_version_blocking) — this
module is what replaced it.

Usage (see main.py / calculator_app.py / gsm_parser_app.py / superadmin_app.py
for the real call sites):

    await app_updater.check_and_maybe_block(
        page,
        app_version=APP_VERSION,
        get_policy_fn=DataManager().get_app_update_policy,  # or get_calculator_update_policy / get_supadm_update_policy / get_gsm_parser_update_policy
        exe_basename="CatManPro",  # or "CatManCalculator" / "CatManSupAdm" / "CatManGSMParser"
    )

exe_basename drives both the guessed-asset-URL fallback (when download_url is
a GitHub "releases/tag/..." page instead of a direct asset link) and the temp
installer filename/folder — it must match the installer's actual output
filename (see scripts/build_release.ps1 / build_calculator_release.ps1's
$ExeBaseName / -App exe name).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
from contextlib import suppress
from urllib.parse import urlparse

import flet as ft
import httpx
from packaging.version import Version

import i18n
from config import Config
from logger import logger


def version_is_outdated(current: str, minimum: str) -> bool:
    try:
        return Version(current) < Version(minimum)
    except Exception:
        logger.exception("Version comparison failed in version_is_outdated")
        return False


def show_update_required_screen(
    page: ft.Page,
    min_required: str,
    download_url: str,
    *,
    current_version: str,
    exe_basename: str = "CatManPro",
) -> None:
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
                    f"https://github.com/{owner}/{repo}/releases/download/{tag}/{exe_basename}-Setup-{guessed_version}.exe",
                    f"https://github.com/{owner}/{repo}/releases/download/{tag}/{exe_basename}-Setup.exe",
                    f"https://github.com/{owner}/{repo}/releases/download/{tag}/{exe_basename}.exe",
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

    async def _start_installer_and_exit(installer_path: str) -> None:
        # Launch the installer and do NOT touch our own window/process at
        # all — proactively closing ourselves here (page.window.destroy(),
        # self-taskkill, any ordering between the two) was confirmed live,
        # for CatMan Pro, to sometimes interfere with the installer actually
        # launching and showing its wizard. It's fine if this window stays
        # open side by side with the newly-installed one for a bit; closing
        # the OLD app is handled entirely on the installer's own side (see
        # each app's .installer.iss: CloseApplications/RestartApplications
        # during file replacement, plus an explicit postinstall taskkill
        # paired with launching the new version) — nothing here needs to
        # race that or guess at it.
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
            target_dir = os.path.join(tempfile.gettempdir(), f"{exe_basename}Update")
            os.makedirs(target_dir, exist_ok=True)
            installer_path = os.path.join(target_dir, f"{exe_basename}-Setup-latest.exe")

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

            await _start_installer_and_exit(installer_path)
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
                                            current=current_version,
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


async def check_and_maybe_block(
    page: ft.Page,
    *,
    app_version: str,
    get_policy_fn,
    exe_basename: str = "CatManPro",
) -> bool:
    """Swaps in the blocking update screen if/when the policy check resolves
    and the installed version is below min_required. get_policy_fn: a
    zero-arg callable returning {"min_required": ..., "download_url": ...,
    "download_url_android": ...} — e.g. DataManager().get_app_update_policy
    / get_calculator_update_policy / get_supadm_update_policy /
    get_gsm_parser_update_policy.

    Returns True if the blocking update screen was shown (page.controls was
    replaced) — callers that would otherwise show a login/password screen
    right after this must check the return value first, or they'd overwrite
    the update screen. main.py calls this fire-and-forget via page.run_task
    AFTER its own login screen is already up (see its own comment for why);
    everything else (calculator_app.py, gsm_parser_app.py, superadmin_app.py)
    awaits it BEFORE showing their gate and checks the return value."""
    try:
        policy = await asyncio.to_thread(get_policy_fn)
        min_required = (policy.get("min_required") or "").strip()
        if page.platform.is_mobile():
            download_url = (policy.get("download_url_android") or "").strip() or (
                policy.get("download_url") or ""
            ).strip()
        else:
            download_url = (policy.get("download_url") or "").strip() or (
                Config.UPDATE_DOWNLOAD_URL or ""
            ).strip()
        if min_required and version_is_outdated(app_version, min_required):
            show_update_required_screen(
                page,
                min_required,
                download_url,
                current_version=app_version,
                exe_basename=exe_basename,
            )
            return True
    except Exception:
        logger.exception("Failed to check app update policy (%s)", exe_basename)
    return False
