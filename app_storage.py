"""
Per-device cache/storage location — lets a user move where CatMan Pro keeps
its local cache files (order snapshots, table layouts, outbox queues, etc.)
from one drive to another (e.g. C: -> D:) instead of always using
%APPDATA%\\CatManPro.

Every view that caches local files already reads the FLET_APP_STORAGE_DATA
environment variable as its first choice (see the many _app_data_dir()-style
methods across views/*.py) before falling back to %APPDATA%\\CatManPro. So
the only thing this module needs to do is: at app startup, read where the
user last pointed their storage at (a tiny pointer file that itself ALWAYS
stays at the fixed OS-standard location, since that's the one thing we must
always be able to find), and set that env var before any view is built.

Changing the location (set_storage_dir) physically MOVES every existing file
out of the old location into the new one — never leaves stale data behind
on the old drive.
"""

from __future__ import annotations

import contextlib
import os
import shutil

_POINTER_FILE = os.path.join(
    os.getenv("APPDATA") or os.path.expanduser("~"), "CatManPro", "_storage_pointer.txt"
)

_DEFAULT_DIR = os.path.join(
    os.getenv("APPDATA") or os.path.expanduser("~"), "CatManPro"
)


def get_current_storage_dir() -> str:
    """Where local cache files live right now. Checks the pointer file first
    (set via set_storage_dir), then FLET_APP_STORAGE_DATA (advanced/manual
    override), then the standard default."""
    with contextlib.suppress(Exception):
        if os.path.exists(_POINTER_FILE):
            with open(_POINTER_FILE, "r", encoding="utf-8") as f:
                saved = f.read().strip()
            if saved and os.path.isdir(saved):
                return saved
    env_override = os.getenv("FLET_APP_STORAGE_DATA")
    if env_override:
        return env_override
    return _DEFAULT_DIR


def apply_storage_dir_override() -> None:
    """Call once, as early as possible at app startup (before any view opens
    and computes its own cache path) — primes FLET_APP_STORAGE_DATA from the
    saved pointer, if the user has moved their storage before."""
    current = get_current_storage_dir()
    os.makedirs(current, exist_ok=True)
    os.environ["FLET_APP_STORAGE_DATA"] = current


def set_storage_dir(new_dir: str) -> tuple[bool, str]:
    """Move every existing cache file from the current location into
    new_dir, then point future sessions there. Returns (ok, message)."""
    new_dir = str(new_dir or "").strip()
    if not new_dir:
        return False, "Не вказано папку"
    old_dir = get_current_storage_dir()
    new_dir = os.path.abspath(new_dir)
    old_dir_abs = os.path.abspath(old_dir)
    if new_dir == old_dir_abs:
        return True, "Це вже поточна папка"

    try:
        os.makedirs(new_dir, exist_ok=True)
    except Exception as exc:
        return False, f"Не вдалося створити папку: {exc}"

    moved = 0
    errors: list[str] = []
    if os.path.isdir(old_dir_abs):
        for name in os.listdir(old_dir_abs):
            if name == os.path.basename(_POINTER_FILE):
                # Pointer file always stays at the fixed bootstrap location,
                # never inside the movable cache dir itself.
                continue
            src = os.path.join(old_dir_abs, name)
            dst = os.path.join(new_dir, name)
            try:
                if os.path.exists(dst):
                    if os.path.isdir(dst):
                        shutil.rmtree(dst, ignore_errors=True)
                    else:
                        with contextlib.suppress(Exception):
                            os.remove(dst)
                shutil.move(src, dst)
                moved += 1
            except Exception as exc:
                errors.append(f"{name}: {exc}")

    with contextlib.suppress(Exception):
        os.makedirs(os.path.dirname(_POINTER_FILE), exist_ok=True)
        with open(_POINTER_FILE, "w", encoding="utf-8") as f:
            f.write(new_dir)

    os.environ["FLET_APP_STORAGE_DATA"] = new_dir

    if errors:
        return False, f"Перенесено {moved} файл(ів), помилки: {'; '.join(errors[:3])}"
    return True, f"Перенесено {moved} файл(ів) у нову папку. Перезапустіть застосунок."
