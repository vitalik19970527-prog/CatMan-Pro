"""Local JSON storage for the 'Загальні' manager selection.

Stored in %APPDATA%/CM_WorkSpace/general_selection.json  (Windows)
or ~/.cm_workspace/general_selection.json  (other platforms).

Structure:
{
  "<supervisor_id>": ["<mgr_id1>", "<mgr_id2>", ...]
}
"""

import json
import os
import pathlib


def _store_path() -> pathlib.Path:
    flet_storage = os.getenv("FLET_APP_STORAGE_DATA")
    if flet_storage:
        base = pathlib.Path(flet_storage)
    elif os.name == "nt":
        base = pathlib.Path(os.environ.get("APPDATA", "~")).expanduser()
    else:
        base = pathlib.Path("~").expanduser()
    folder = base / "CM_WorkSpace"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "general_selection.json"


def _load_all() -> dict:
    p = _store_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _save_all(data: dict) -> None:
    try:
        _store_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def load_general_selection(supervisor_id: str) -> list[str]:
    """Return saved list of selected manager IDs for this supervisor."""
    data = _load_all()
    return list(data.get(str(supervisor_id or ""), []))


def save_general_selection(supervisor_id: str, manager_ids: list[str]) -> None:
    """Persist the selected manager IDs for this supervisor."""
    data = _load_all()
    data[str(supervisor_id or "")] = [str(m) for m in (manager_ids or [])]
    _save_all(data)
