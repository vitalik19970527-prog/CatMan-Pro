"""Mixin that adds "Загальні" (general/combined) mode support to data-views.

Usage in a view's __init__:
    self._general_mode: bool = bool(user_data.get("general_mode", False))
    self._general_manager_ids: list[str] = list(user_data.get("general_manager_ids") or [])
    self._general_manager_names: dict[str, str] = _build_mgr_names(
        user_data.get("all_subordinates_flat") or [], user_data
    )

Usage in a view's data-load method:
    if self._general_mode:
        items = self._gm_load_for_all_managers(self._load_single_manager_data)
    else:
        items = self._load_single_manager_data(self.manager_id)

Each item returned from _load_single_manager_data must be a list[dict].
The mixin automatically stamps each dict with:
    _gm_manager_id   – the manager_id that produced this record
    _gm_manager_name – the display name for grouping headers
"""

from __future__ import annotations

import flet as ft


def build_general_manager_names(
    all_subordinates_flat: list[dict], user_data: dict
) -> dict[str, str]:
    """Build {manager_id: full_name} map for all subordinates + self."""
    names: dict[str, str] = {}
    for m in all_subordinates_flat or []:
        if isinstance(m, dict):
            mid = str(m.get("id") or "").strip()
            if mid:
                names[mid] = str(m.get("full_name") or mid)
    own_id = str((user_data or {}).get("id") or "").strip()
    own_name = str((user_data or {}).get("full_name") or "Я")
    if own_id:
        names[own_id] = own_name
    return names


class GeneralModeMixin:
    """Add to any data-view class.
    The host class must have:
        self._general_mode: bool
        self._general_manager_ids: list[str]
        self._general_manager_names: dict[str, str]
    """

    def _gm_load_for_all_managers(
        self,
        single_load_fn,
        *args,
        **kwargs,
    ) -> list[dict]:
        """Call single_load_fn(manager_id, *args, **kwargs) for each selected manager.
        Returns combined list with _gm_manager_id / _gm_manager_name stamped on each row."""
        combined: list[dict] = []
        for mid in (self._general_manager_ids or []):
            try:
                rows = single_load_fn(mid, *args, **kwargs) or []
                name = (self._general_manager_names or {}).get(mid, mid)
                for row in rows:
                    if isinstance(row, dict):
                        row["_gm_manager_id"] = mid
                        row["_gm_manager_name"] = name
                combined.extend(r for r in rows if isinstance(r, dict))
            except Exception:
                pass
        return combined

    # ─────────── UI helpers ───────────

    @staticmethod
    def gm_section_header(manager_name: str, theme: dict | None = None) -> ft.Container:
        """A visual section separator row with the manager's name."""
        _th = theme or {}
        _bg = _th.get("soft", "#EEF2FF")
        _border_clr = _th.get("border", "#C7D2FE")
        _color = _th.get("chip_text", "#4338CA")
        return ft.Container(
            bgcolor=_bg,
            border=ft.border.only(
                bottom=ft.BorderSide(1, _border_clr),
                top=ft.BorderSide(1, _border_clr),
            ),
            padding=ft.padding.symmetric(horizontal=10, vertical=4),
            content=ft.Text(
                manager_name,
                size=10,
                weight=ft.FontWeight.W_700,
                color=_color,
            ),
        )

    @staticmethod
    def gm_group_by_manager(rows: list[dict]) -> list[tuple[str, str, list[dict]]]:
        """Group rows by _gm_manager_id.
        Returns list of (manager_id, manager_name, rows) preserving insertion order."""
        order: list[str] = []
        groups: dict[str, tuple[str, list[dict]]] = {}
        for row in rows:
            mid = str(row.get("_gm_manager_id") or "")
            name = str(row.get("_gm_manager_name") or mid)
            if mid not in groups:
                order.append(mid)
                groups[mid] = (name, [])
            groups[mid][1].append(row)
        return [(mid, groups[mid][0], groups[mid][1]) for mid in order]
