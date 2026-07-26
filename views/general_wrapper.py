"""Utility that wraps any view in collapsible per-manager sections for 'Загальні' mode.

Usage in a view's build():
    from views.general_wrapper import build_general_sections
    if self._general_mode and self._general_manager_ids:
        build_general_sections(
            self.content_area,
            self.user_data,
            self.db,
            self.__class__,          # same view class
            view_extra_kwargs={...},  # extra __init__ args besides content_area/user_data/db
        )
        return
    # ... normal single-manager build below ...
"""

from __future__ import annotations

import contextlib

import flet as ft

import i18n
from views import themes as _gw_themes
from views.general_mode_mixin import build_general_manager_names
from views.mobile_layout_helpers import is_mobile, make_scrollable_chip_row


def _gw_get_theme(user_data: dict) -> dict:
    mid = str(user_data.get("active_manager_id") or user_data.get("id") or "").strip()
    return _gw_themes.get_active_theme(mid)


def build_general_sections(
    content_area: ft.Column,
    user_data: dict,
    db,
    view_class,
    *,
    view_extra_kwargs: dict | None = None,
    section_header_text: str | None = None,
    count_getter=None,
    unpaid_count_getter=None,
    chips_getter=None,
    count_label: str = "Замовлень:",
    top_controls: list | None = None,
) -> None:
    _mid = str(user_data.get("active_manager_id") or user_data.get("id") or "")
    _t = i18n.bind(str(user_data.get("id") or ""))
    """Render collapsible manager sections (collapsed by default) inside content_area.

    Each section lazy-instantiates view_class for that manager when expanded.
    view_class.__init__ must accept (content_area, user_data, db, **view_extra_kwargs).
    chips_getter(manager_id) -> list[ft.Control]: optional extra chips for the header row.
    """
    general_manager_ids: list[str] = list(user_data.get("general_manager_ids") or [])
    all_subs: list[dict] = list(user_data.get("all_subordinates_flat") or [])
    mgr_names: dict[str, str] = build_general_manager_names(all_subs, user_data)
    extra_kw: dict = dict(view_extra_kwargs or {})
    _th = _gw_get_theme(user_data)
    _gw_icon = _th["icon_color"]
    _gw_muted = _th["muted"]
    _gw_text = _th["text"]
    _gw_panel = _th["panel"]
    _gw_border = _th["border"]
    _gw_chip_bg = _th.get("chip_bg", _th["panel"])

    sections: list[ft.Control] = []

    if section_header_text:
        sections.append(
            ft.Container(
                content=ft.Text(
                    section_header_text, size=11, color=_gw_muted, italic=True
                ),
                padding=ft.padding.only(bottom=6),
            )
        )

    for mid in general_manager_ids:
        mgr_name = mgr_names.get(mid, mid)
        arrow = ft.Icon(ft.Icons.EXPAND_MORE, size=18, color=_gw_icon)

        sub_area = ft.Column(
            expand=True,
            spacing=0,
            scroll=ft.ScrollMode.HIDDEN,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        body = ft.Column([sub_area], spacing=0, visible=False)
        _loaded = [False]

        def _expand(e, _mid=mid, _body=body, _arrow=arrow, _sub=sub_area, _lf=_loaded):
            _body.visible = not _body.visible
            _arrow.name = (
                ft.Icons.EXPAND_LESS if _body.visible else ft.Icons.EXPAND_MORE
            )
            if _body.visible and not _lf[0]:
                _lf[0] = True
                mgr_ctx = dict(user_data)
                mgr_ctx["active_manager_id"] = _mid
                mgr_ctx["active_manager_name"] = mgr_names.get(_mid, _mid)
                mgr_ctx["subordinate_view_readonly"] = True
                mgr_ctx["general_mode"] = False
                mgr_ctx["general_manager_ids"] = []
                mgr_ctx["_is_sub_section_view"] = True
                try:
                    inst = view_class(_sub, mgr_ctx, db, **extra_kw)
                    inst.build()
                except Exception as _ex:
                    _sub.controls.append(
                        ft.Text(
                            _t(
                                "async.error_title", "Помилка завантаження: {ex}"
                            ).format(ex=_ex),
                            size=11,
                            color="red",
                        )
                    )
            with contextlib.suppress(Exception):
                e.control.parent.update()

        # Left side: arrow + name
        left_row = ft.Row(
            [
                arrow,
                ft.Text(mgr_name, size=12, weight=ft.FontWeight.W_700, color=_gw_icon),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            tight=True,
        )

        # Right side: financial chips + order count
        right_children = []
        if chips_getter is not None:
            try:
                extra_chips = chips_getter(mid) or []
                right_children.extend(extra_chips)
            except Exception:
                pass
        if unpaid_count_getter is not None:
            try:
                ucnt = unpaid_count_getter(mid)
                _u_bg = "#DCFCE7" if ucnt == 0 else "#FEE2E2"
                _u_fg = "#166534" if ucnt == 0 else "#DC2626"
                right_children.append(
                    ft.Container(
                        width=132,
                        padding=ft.padding.symmetric(horizontal=10, vertical=4),
                        bgcolor=_u_bg,
                        border_radius=8,
                        alignment=ft.alignment.Alignment(0, -1),
                        content=ft.Column(
                            [
                                ft.Text(
                                    _t("payments.chip.unpaid_label", "Не сплачених:"),
                                    size=9,
                                    weight=ft.FontWeight.W_700,
                                    color=_gw_muted,
                                    text_align=ft.TextAlign.CENTER,
                                ),
                                ft.Text(
                                    str(ucnt),
                                    size=10,
                                    weight=ft.FontWeight.W_700,
                                    color=_u_fg,
                                    text_align=ft.TextAlign.CENTER,
                                ),
                            ],
                            spacing=2,
                            tight=True,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    )
                )
            except Exception:
                pass
        if count_getter is not None:
            try:
                cnt = count_getter(mid)
                right_children.append(
                    ft.Container(
                        width=132,
                        padding=ft.padding.symmetric(horizontal=10, vertical=4),
                        bgcolor=_gw_chip_bg,
                        border_radius=8,
                        alignment=ft.alignment.Alignment(0, -1),
                        content=ft.Column(
                            [
                                ft.Text(
                                    count_label,
                                    size=9,
                                    weight=ft.FontWeight.W_700,
                                    color=_gw_muted,
                                    text_align=ft.TextAlign.CENTER,
                                ),
                                ft.Text(
                                    str(cnt),
                                    size=10,
                                    weight=ft.FontWeight.W_700,
                                    color=_gw_icon,
                                    text_align=ft.TextAlign.CENTER,
                                ),
                            ],
                            spacing=2,
                            tight=True,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    )
                )
            except Exception:
                pass

        # On mobile a wrap=True chip row breaks onto 2+ lines the moment the
        # combined chip width exceeds the narrow screen, making every
        # collapsed section header taller than necessary. Keep it a single
        # horizontally-scrollable line on mobile instead; desktop keeps the
        # original wrapping Row untouched.
        _right_row = (
            make_scrollable_chip_row(
                right_children,
                spacing=6,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            )
            if is_mobile(content_area.page)
            else ft.Row(
                right_children,
                spacing=6,
                tight=True,
                wrap=True,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            )
        )
        header_content = (
            ft.Row(
                [
                    ft.Container(content=left_row, expand=True),
                    _right_row,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            )
            if right_children
            else ft.Row(
                [left_row],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )

        header = ft.Container(
            bgcolor=_gw_chip_bg,
            border=ft.border.all(1, _gw_border),
            border_radius=ft.border_radius.only(top_left=6, top_right=6),
            padding=ft.padding.symmetric(horizontal=10, vertical=7),
            on_click=_expand,
            content=header_content,
        )

        sections.append(
            ft.Container(
                content=ft.Column([header, body], spacing=0),
                border=ft.border.all(1, _gw_border),
                border_radius=6,
                margin=ft.margin.only(bottom=6),
            )
        )

    content_area.controls.clear()
    if top_controls:
        content_area.controls.extend(top_controls)
    content_area.controls.append(
        ft.Column(
            sections
            or [
                ft.Text(
                    _t("general.no_manager_selected", "Не вибрано жодного менеджера"),
                    size=12,
                    color=_gw_muted,
                )
            ],
            spacing=0,
        )
    )
    with contextlib.suppress(Exception):
        content_area.update()


def build_region_sections(
    content_area: ft.Column,
    user_data: dict,
    db,
    view_class,
    *,
    view_extra_kwargs: dict | None = None,
    count_getter=None,
    unpaid_count_getter=None,
    chips_getter=None,
    count_label: str = "Замовлень:",
    region_kwarg_name: str = "active_region",
    top_controls: list | None = None,
) -> None:
    """Render collapsible region sections when 'All regions' is selected.

    chips_getter(region_id) -> list[ft.Control]: optional extra chips for the header row.
    """
    _t = i18n.bind(str(user_data.get("id") or ""))
    company_id = str(user_data.get("company_id") or "").strip()
    regions = [
        r for r in (db.get_company_regions(company_id) or []) if isinstance(r, dict)
    ]
    extra_kw: dict = dict(view_extra_kwargs or {})
    _th = _gw_get_theme(user_data)
    _gw_icon = _th["icon_color"]
    _gw_muted = _th["muted"]
    _gw_panel = _th["panel"]
    _gw_border = _th["border"]
    _gw_chip_bg = _th.get("chip_bg", _th["panel"])

    sections: list[ft.Control] = []

    for region in regions:
        rid = str(region.get("id") or "").strip()
        rname = str(region.get("name") or rid).strip()
        arrow = ft.Icon(ft.Icons.EXPAND_MORE, size=18, color=_gw_icon)

        sub_area = ft.Column(
            expand=True,
            spacing=0,
            scroll=ft.ScrollMode.HIDDEN,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        body = ft.Column([sub_area], spacing=0, visible=False)
        _loaded = [False]

        def _expand(
            e, _rname=rname, _body=body, _arrow=arrow, _sub=sub_area, _lf=_loaded
        ):
            _body.visible = not _body.visible
            _arrow.name = (
                ft.Icons.EXPAND_LESS if _body.visible else ft.Icons.EXPAND_MORE
            )
            if _body.visible and not _lf[0]:
                _lf[0] = True
                region_kw = dict(extra_kw)
                region_kw[region_kwarg_name] = _rname
                sub_ud = dict(user_data)
                sub_ud["_is_sub_section_view"] = True
                try:
                    inst = view_class(_sub, sub_ud, db, **region_kw)
                    inst.build()
                except Exception as _ex:
                    _sub.controls.append(
                        ft.Text(
                            _t(
                                "async.error_title", "Помилка завантаження: {ex}"
                            ).format(ex=_ex),
                            size=11,
                            color="red",
                        )
                    )
            with contextlib.suppress(Exception):
                e.control.parent.update()

        # Left side: arrow + name
        left_row = ft.Row(
            [
                arrow,
                ft.Text(rname, size=12, weight=ft.FontWeight.W_700, color=_gw_icon),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            tight=True,
        )

        # Right side: financial chips + order count
        right_children = []
        if chips_getter is not None:
            try:
                extra_chips = chips_getter(rid) or []
                right_children.extend(extra_chips)
            except Exception:
                pass
        if unpaid_count_getter is not None:
            try:
                ucnt = unpaid_count_getter(rid)
                _u_bg = "#DCFCE7" if ucnt == 0 else "#FEE2E2"
                _u_fg = "#166534" if ucnt == 0 else "#DC2626"
                right_children.append(
                    ft.Container(
                        width=132,
                        padding=ft.padding.symmetric(horizontal=10, vertical=4),
                        bgcolor=_u_bg,
                        border_radius=8,
                        alignment=ft.alignment.Alignment(0, -1),
                        content=ft.Column(
                            [
                                ft.Text(
                                    _t("payments.chip.unpaid_label", "Не сплачених:"),
                                    size=9,
                                    weight=ft.FontWeight.W_700,
                                    color=_gw_muted,
                                    text_align=ft.TextAlign.CENTER,
                                ),
                                ft.Text(
                                    str(ucnt),
                                    size=10,
                                    weight=ft.FontWeight.W_700,
                                    color=_u_fg,
                                    text_align=ft.TextAlign.CENTER,
                                ),
                            ],
                            spacing=2,
                            tight=True,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    )
                )
            except Exception:
                pass
        if count_getter is not None:
            try:
                cnt = count_getter(rid)
                right_children.append(
                    ft.Container(
                        width=132,
                        padding=ft.padding.symmetric(horizontal=10, vertical=4),
                        bgcolor=_gw_chip_bg,
                        border_radius=8,
                        alignment=ft.alignment.Alignment(0, -1),
                        content=ft.Column(
                            [
                                ft.Text(
                                    count_label,
                                    size=9,
                                    weight=ft.FontWeight.W_700,
                                    color=_gw_muted,
                                    text_align=ft.TextAlign.CENTER,
                                ),
                                ft.Text(
                                    str(cnt),
                                    size=10,
                                    weight=ft.FontWeight.W_700,
                                    color=_gw_icon,
                                    text_align=ft.TextAlign.CENTER,
                                ),
                            ],
                            spacing=2,
                            tight=True,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    )
                )
            except Exception:
                pass

        # On mobile a wrap=True chip row breaks onto 2+ lines the moment the
        # combined chip width exceeds the narrow screen, making every
        # collapsed section header taller than necessary. Keep it a single
        # horizontally-scrollable line on mobile instead; desktop keeps the
        # original wrapping Row untouched.
        _right_row = (
            make_scrollable_chip_row(
                right_children,
                spacing=6,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            )
            if is_mobile(content_area.page)
            else ft.Row(
                right_children,
                spacing=6,
                tight=True,
                wrap=True,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            )
        )
        header_content = (
            ft.Row(
                [
                    ft.Container(content=left_row, expand=True),
                    _right_row,
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            )
            if right_children
            else ft.Row(
                [left_row],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )
        )

        header = ft.Container(
            bgcolor=_gw_chip_bg,
            border=ft.border.all(1, _gw_border),
            border_radius=ft.border_radius.only(top_left=6, top_right=6),
            padding=ft.padding.symmetric(horizontal=10, vertical=6),
            on_click=_expand,
            content=header_content,
        )

        sections.append(
            ft.Container(
                content=ft.Column([header, body], spacing=0),
                border=ft.border.all(1, _gw_border),
                border_radius=6,
                margin=ft.margin.only(bottom=4),
            )
        )

    content_area.controls.clear()
    if top_controls:
        content_area.controls.extend(top_controls)
    content_area.controls.append(
        ft.Column(
            sections
            or [
                ft.Text(
                    _t("general.no_regions", "Немає регіонів"), size=12, color=_gw_muted
                )
            ],
            spacing=0,
        )
    )
    with contextlib.suppress(Exception):
        content_area.update()
