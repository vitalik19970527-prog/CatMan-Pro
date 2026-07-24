"""Shared helper for disambiguating products that share the same name.

Two products can never have identical name + category + supplier (that would
be an exact duplicate row) — so whenever a name repeats in a list, category
and/or supplier must differ between the repeats. Every place in the UI that
shows a product name should call `compute_name_suffixes()` once for the
currently-loaded product list and append the returned per-id suffix, so
duplicate names are never shown as bare, indistinguishable text.
"""

from __future__ import annotations

from typing import Callable

import flet as ft


def _default_name(p: dict) -> str:
    return str((p or {}).get("name") or "").strip()


def _default_category(p: dict) -> str:
    return str((p or {}).get("category_name") or "").strip()


def _default_supplier(p: dict) -> str:
    return str((p or {}).get("supplier_name") or "").strip()


def _default_id(p: dict) -> str:
    return str((p or {}).get("id") or "")


def compute_name_suffixes(
    products: list[dict],
    *,
    name_of: Callable[[dict], str] = _default_name,
    category_of: Callable[[dict], str] = _default_category,
    supplier_of: Callable[[dict], str] = _default_supplier,
    id_of: Callable[[dict], str] = _default_id,
) -> dict[str, str]:
    """Return {product_id: suffix}.

    suffix is "" for names that are unique in `products`. For a name that
    repeats, the suffix shows exactly the attribute(s) that actually vary
    within that name-group:
      - only category differs  -> " | Кат: {category}"
      - only supplier differs  -> " | Постач: {supplier}"
      - both differ            -> " | Кат: {category} | Постач: {supplier}"
      - neither differs (should be rare — e.g. differ only by a deeper
        catalog hierarchy level) -> show both anyway as a fallback so the
        entries aren't left looking identical.
    """
    groups: dict[str, list[dict]] = {}
    for p in products or []:
        if not isinstance(p, dict):
            continue
        nm = name_of(p)
        if not nm:
            continue
        groups.setdefault(nm.lower(), []).append(p)

    suffixes: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        cats = {category_of(p) for p in members}
        cats.discard("")
        sups = {supplier_of(p) for p in members}
        sups.discard("")
        show_cat = len(cats) > 1
        show_sup = len(sups) > 1
        if not show_cat and not show_sup:
            show_cat = bool(cats)
            show_sup = bool(sups)
        for p in members:
            parts = []
            if show_cat:
                parts.append(f"Кат: {category_of(p) or '-'}")
            if show_sup:
                parts.append(f"Постач: {supplier_of(p) or '-'}")
            if parts:
                suffixes[id_of(p)] = " | " + " | ".join(parts)
    return suffixes


def resolve_category_label(
    product: dict,
    *,
    node_path_of: Callable[[str], list[dict]] | None = None,
    category_by_id: dict | None = None,
) -> str:
    """Best-effort category name for disambiguation.

    Prefers the ROOT node of the product's catalog-tree path (via
    `catalog_node_id` -> `node_path_of`) — that's the actual category, and is
    always correct because it's the same data the "Ієрархія:" line renders.
    `category_id`/`category_name` are NOT reliable alone: some products are
    classified only through the catalog tree and never get a flat
    `category_id` written, which used to show as a bare "-" (or made two
    same-named products with different categories look identical because
    both resolved to "").
    """
    node_id = str((product or {}).get("catalog_node_id") or "").strip()
    if node_path_of is not None and node_id:
        try:
            path_rows = node_path_of(node_id) or []
        except Exception:
            path_rows = []
        if path_rows:
            root_name = str((path_rows[0] or {}).get("name") or "").strip()
            if root_name:
                return root_name

    cat_nested = (product or {}).get("category")
    if isinstance(cat_nested, dict):
        nm = str(cat_nested.get("name") or "").strip()
        if nm:
            return nm

    flat = str((product or {}).get("category_name") or "").strip()
    if flat:
        return flat

    if category_by_id:
        cid = str((product or {}).get("category_id") or "").strip()
        if cid:
            return str((category_by_id.get(cid) or {}).get("name") or "").strip()

    return ""


def resolve_supplier_label(
    product: dict,
    *,
    supplier_by_id: dict | None = None,
) -> str:
    """Best-effort supplier name for disambiguation — nested `supplier` dict
    (already joined for display) first, then the flat `supplier_name` cache
    field, then a `supplier_id` -> `supplier_by_id` lookup."""
    nested = (product or {}).get("supplier")
    if isinstance(nested, dict):
        nm = str(nested.get("name") or "").strip()
        if nm:
            return nm

    flat = str((product or {}).get("supplier_name") or "").strip()
    if flat:
        return flat

    if supplier_by_id:
        sid = str((product or {}).get("supplier_id") or "").strip()
        if sid:
            return str((supplier_by_id.get(sid) or {}).get("name") or "").strip()

    return ""


def make_node_path_resolver(db, cache: dict | None = None) -> Callable[[str], list]:
    """Memoized wrapper around `db.get_node_path(node_id)` — many products
    share the same catalog leaf, so this avoids re-querying per row. Pass an
    existing dict via `cache` to share it with other code (e.g. a view's own
    `_product_path_cache` used for the hierarchy breadcrumb) — otherwise a
    fresh one is created and kept alive only as long as the returned
    closure is referenced."""
    _cache = cache if isinstance(cache, dict) else {}

    def _resolve(node_id: str) -> list:
        nid = str(node_id or "").strip()
        if not nid:
            return []
        if nid not in _cache:
            try:
                _cache[nid] = db.get_node_path(nid) or []
            except Exception:
                _cache[nid] = []
        return _cache[nid]

    return _resolve


def display_name(
    product: dict,
    suffixes: dict[str, str],
    *,
    name_of: Callable[[dict], str] = _default_name,
    id_of: Callable[[dict], str] = _default_id,
) -> str:
    """name + disambiguation suffix (empty string if the name is unique)."""
    return name_of(product) + suffixes.get(id_of(product), "")


def with_instant_tooltip(control: ft.Control, full_text: str) -> ft.Control:
    """Wrap `control` so hovering shows `full_text` almost instantly — for
    spots (dropdown rows, narrow table cells) where the disambiguated name
    is too long to display in full."""
    control.tooltip = ft.Tooltip(message=full_text, wait_duration=0)
    return control
