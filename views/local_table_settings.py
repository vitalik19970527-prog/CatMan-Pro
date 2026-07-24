"""
Shared utility for persisting per-user per-region table settings
(column visibility, widths, order) to a local JSON file on the device.

Files are stored at ~/.catmanpro/tbl_{table}_{company}_{user}_{region}.json
Settings survive DB outages and load instantly on open.
Multiple users on one device each get their own file.
"""
from __future__ import annotations

import json
import os
from typing import Any

_APP_DIR_CACHE: str | None = None


def _app_data_dir() -> str:
    global _APP_DIR_CACHE
    if _APP_DIR_CACHE:
        return _APP_DIR_CACHE
    path = os.getenv("FLET_APP_STORAGE_DATA") or os.path.join(
        os.path.expanduser("~"), ".catmanpro"
    )
    os.makedirs(path, exist_ok=True)
    _APP_DIR_CACHE = path
    return path


def _safe_seg(s: Any) -> str:
    return str(s or "").replace("/", "_").replace("\\", "_").strip() or "x"


def _file_path(table: str, company_id: str, user_id: str, region_id: str) -> str:
    return os.path.join(
        _app_data_dir(),
        f"tbl_{_safe_seg(table)}_{_safe_seg(company_id)}"
        f"_{_safe_seg(user_id)}_{_safe_seg(region_id)}.json",
    )


def load_table_settings(
    table: str,
    company_id: str,
    user_id: str,
    region_id: str,
    defaults: dict,
) -> dict:
    """Load saved table settings, merging over defaults.

    Saved keys override defaults; keys present only in defaults are kept
    so newly added columns appear with their default widths.
    Returns a copy — caller may mutate freely.
    """
    path = _file_path(table, company_id, user_id, region_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                merged = dict(defaults)
                merged.update(saved)
                return merged
        except Exception:
            pass
    return dict(defaults)


def save_table_settings(
    table: str,
    company_id: str,
    user_id: str,
    region_id: str,
    settings: dict,
) -> None:
    """Persist table settings to local file immediately (synchronous)."""
    path = _file_path(table, company_id, user_id, region_id)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, default=str)
    except Exception:
        pass
