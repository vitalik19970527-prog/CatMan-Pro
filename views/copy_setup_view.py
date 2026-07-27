import contextlib

import flet as ft

import i18n

# ---------------------------------------------------------------------------
# Theme colour shims
# ---------------------------------------------------------------------------
_BORDER  = '#D8DEE9'
_BORDER2 = '#E5E7EB'


def _th_from_view(view) -> dict:
    from views.themes import get_active_theme
    return get_active_theme(str(getattr(view, 'manager_id', '') or ''))


from logger import logger


def _normalize_role(role):
    return (role or "").strip().lower().replace(" ", "")


def _can_copy(role):
    return _normalize_role(role) in {
        "адмін",
        "админ",
        "admin",
        "суперадмін",
        "суперадмин",
        "superadmin",
    }


class CopySetupView:
    HEADER_SIZE = 28
    META_TEXT_SIZE = 12
    COPY_PANEL_HEIGHT = 520

    # Persists which sections are expanded across CopySetupView re-instantiation
    # (a fresh instance is created on every tab load and every background
    # sync-pulse reload, so a plain `self._section_state` would reset to
    # `default_open` every time — see _make_section below). Keyed by
    # (company_id, section_key); resets on app restart, which is fine.
    _section_state_by_company: dict = {}

    def __init__(
        self, content_area, user_data, db, on_open_module=None, consume_dirty_scope=None
    ):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.t = i18n.bind(str(self.user_data.get("id") or self.user_data.get("active_manager_id") or ""))
        self.company_id = self.user_data.get("company_id")
        self.on_open_module = on_open_module
        self.consume_dirty_scope = consume_dirty_scope or (lambda x: False)
        self._section_state = CopySetupView._section_state_by_company.setdefault(
            str(self.company_id or ""), {}
        )

    def _show_help(self) -> None:
        page = self.content_area.page if self.content_area else None
        if not page:
            return
        from views.help_utils import show_modal_help
        show_modal_help(page, self.t("copy.help.title", "Довідка — Копіювання"), [
            self.t("copy.help.desc", "Копіювання налаштувань товарів між менеджерами та регіонами: пакування, постачальники."),
            (self.t("copy.help.steps_title", "Як користуватись"), None),
            self.t("copy.help.step1", "1. Оберіть менеджера-джерело та регіон, потім товар."),
            self.t("copy.help.step2", "2. Оберіть менеджера-призначення та регіон."),
            self.t("copy.help.step3", "3. «Копіювати» — копіює з поточними даними; «Копіювати з правками» — дозволяє змінити дані перед копіюванням."),
            self.t("copy.help.step4", "4. Копія з'являється у списку менеджера-призначення."),
        ])

    def _show_snack(self, text, color):
        page = self.content_area.page
        if not page:
            return
        page.snack_bar = ft.SnackBar(
            ft.Text(text, color=ft.Colors.WHITE, weight="bold"),
            bgcolor=color,
            duration=3000,
        )
        page.snack_bar.open = True
        page.update()

    def _clear_field_errors(self, *controls):
        for control in controls:
            if control is not None and hasattr(control, "error_text"):
                control.error_text = None

    def _mark_required(self, control, message="Поле обов'язкове"):
        if control is not None and hasattr(control, "error_text"):
            control.error_text = message

    def _open_module(self, module_key):
        if callable(self.on_open_module):
            self.on_open_module(module_key)

    def _make_section(self, key, title, controls, default_open=False):
        is_open = bool(self._section_state.get(key, default_open))
        body = ft.Column(controls, spacing=8, visible=is_open)
        toggle_btn = ft.TextButton(
            title,
            icon=(
                ft.Icons.KEYBOARD_ARROW_DOWN
                if is_open
                else ft.Icons.KEYBOARD_ARROW_RIGHT
            ),
            style=ft.ButtonStyle(
                padding=ft.padding.symmetric(horizontal=0, vertical=0)
            ),
        )

        def _toggle(_):
            body.visible = not body.visible
            self._section_state[key] = body.visible
            toggle_btn.icon = (
                ft.Icons.KEYBOARD_ARROW_DOWN
                if body.visible
                else ft.Icons.KEYBOARD_ARROW_RIGHT
            )
            with contextlib.suppress(Exception):
                body.update()
            with contextlib.suppress(Exception):
                toggle_btn.update()

        toggle_btn.on_click = _toggle
        return ft.Container(
            border=ft.border.all(1, _BORDER2),
            border_radius=10,
            padding=10,
            content=ft.Column([toggle_btn, body], spacing=6),
        )

    def _source_target_split(self, source_controls, target_controls):
        return ft.ResponsiveRow(
            [
                ft.Container(
                    col={"xs": 12, "md": 6},
                    border=ft.border.all(1, _BORDER),
                    border_radius=10,
                    padding=10,
                    height=self.COPY_PANEL_HEIGHT,
                    content=ft.Column(
                        [
                            ft.Text(self.t("copy.from_label", "Звідки"), weight="bold", color="grey800"),
                            ft.Column(
                                source_controls,
                                spacing=8,
                                scroll=ft.ScrollMode.ADAPTIVE,
                                expand=True,
                            ),
                        ],
                        spacing=8,
                        expand=True,
                    ),
                ),
                ft.Container(
                    col={"xs": 12, "md": 6},
                    border=ft.border.all(1, _BORDER),
                    border_radius=10,
                    padding=10,
                    height=self.COPY_PANEL_HEIGHT,
                    content=ft.Column(
                        [
                            ft.Text(self.t("copy.to_label", "Копіюємо в"), weight="bold", color="grey800"),
                            ft.Column(
                                target_controls,
                                spacing=8,
                                scroll=ft.ScrollMode.ADAPTIVE,
                                expand=True,
                            ),
                        ],
                        spacing=8,
                        expand=True,
                    ),
                ),
            ],
            run_spacing=10,
        )

    def _list_button(self, title, subtitle, on_click):
        return ft.OutlinedButton(
            on_click=on_click,
            style=ft.ButtonStyle(
                padding=ft.padding.symmetric(horizontal=12, vertical=10),
                shape=ft.RoundedRectangleBorder(radius=8),
                side=ft.BorderSide(1, _BORDER2),
            ),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Icon(ft.Icons.EDIT, size=16, color=ft.Colors.BLUE_700),
                            ft.Text(title or "Елемент", weight="bold", color="grey900"),
                        ],
                        spacing=6,
                    ),
                    ft.Text(subtitle or "", size=12, color="grey700"),
                ],
                spacing=4,
            ),
        )

    def _find_row(self, rows, row_id):
        for row in rows or []:
            if isinstance(row, dict) and row.get("id") == row_id:
                return row
        return None

    def _find_by_name(self, rows, name):
        target = (name or "").strip().lower()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if (row.get("name") or "").strip().lower() == target:
                return row
        return None

    def build(self):
        _th = _th_from_view(self)
        global _BORDER, _BORDER2
        _BORDER = _BORDER2 = _th["border"]
        if not self.content_area.page:
            return

        self.content_area.controls.clear()

        role = self.user_data.get("role")
        if not _can_copy(role):
            self.content_area.controls.append(
                ft.Text(
                    self.t("copy.access_denied", "Доступ до вкладки копіювання має лише Адмін або СуперАдмін"),
                    color="red",
                )
            )
            self.content_area.update()
            return

        if not self.company_id:
            self.content_area.controls.extend(
                [
                    ft.Text(self.t("copy.title", "Копіювання"), size=self.HEADER_SIZE, weight="bold"),
                    ft.Text(
                        self.t("copy.select_company_hint", "Оберіть конкретну компанію зверху. Копіювання між компаніями заборонено."),
                        size=self.META_TEXT_SIZE,
                        color="grey700",
                    ),
                ]
            )
            self.content_area.update()
            return

        managers = [
            m
            for m in self.db.get_company_managers(
                self.company_id, force_refresh=self.consume_dirty_scope("users")
            )
            if isinstance(m, dict) and m.get("id")
        ]
        regions = [
            r
            for r in self.db.get_company_regions(
                self.company_id, force_refresh=self.consume_dirty_scope("company")
            )
            if isinstance(r, dict) and r.get("id")
        ]
        if not managers:
            self.content_area.controls.extend(
                [
                    ft.Text(self.t("copy.title", "Копіювання"), size=self.HEADER_SIZE, weight="bold"),
                    ft.Text(
                        self.t("copy.need_users", "Для копіювання потрібні користувачі у компанії"),
                        size=self.META_TEXT_SIZE,
                        color="grey700",
                    ),
                ]
            )
            self.content_area.update()
            return
        if not regions:
            self.content_area.controls.extend(
                [
                    ft.Text(self.t("copy.title", "Копіювання"), size=self.HEADER_SIZE, weight="bold"),
                    ft.Text(
                        self.t("copy.need_regions", "Для копіювання потрібні щонайменше 1 регіон у компанії"),
                        size=self.META_TEXT_SIZE,
                        color="grey700",
                    ),
                ]
            )
            self.content_area.update()
            return

        region_options = [
            ft.dropdown.Option(key=r.get("id"), text=r.get("name", "Регіон"))
            for r in regions
        ]
        manager_options = [
            ft.dropdown.Option(key=m.get("id"), text=m.get("full_name", "Користувач"))
            for m in managers
        ]
        managers_by_id = {
            m.get("id"): m for m in managers if isinstance(m, dict) and m.get("id")
        }

        def _build_levels_rows(levels_source=None):
            levels_source = (
                levels_source
                if isinstance(levels_source, list)
                else (
                    self.db.get_company_catalog_config(self.company_id).get("levels")
                    or []
                )
            )
            levels_rows = []
            for lvl in levels_source:
                pos = int(lvl.get("position", 0))
                if pos < 1 or pos > 6:
                    continue
                levels_rows.append(
                    {
                        "title": (lvl.get("title") or f"Рівень {pos}"),
                        "enabled": bool(lvl.get("enabled")),
                        "source_pos": lvl.get("source_pos"),
                    }
                )
            return levels_rows

        def _regions_for_manager(manager_id):
            manager = managers_by_id.get(manager_id) or {}
            role = manager.get("role") if isinstance(manager, dict) else None
            rows = self.db.get_visible_regions_for_manager(
                self.company_id, manager_id, role
            )
            return [r for r in rows if isinstance(r, dict) and r.get("id")]

        # ---------------------------
        # КОПІЮВАННЯ КАТЕГОРІЙ (НОВА МОДЕЛЬ РІВНІВ)
        # ---------------------------
        cat_copy_result = ft.Text("", size=12, color="grey700")
        cat_copy_src_region_f = ft.Dropdown(
            label="Джерело: регіон", options=region_options, border_radius=10
        )
        cat_copy_src_category_f = ft.Dropdown(
            label="Джерело: категорія", options=[], border_radius=10
        )
        cat_copy_dst_region_f = ft.Dropdown(
            label="Призначення: регіон", options=region_options, border_radius=10
        )
        cat_copy_name_f = ft.TextField(
            label="Нова назва категорії (опційно)", border_radius=10
        )
        cat_copy_mode_f = ft.RadioGroup(
            value="full",
            content=ft.Row(
                [
                    ft.Radio(value="full", label=self.t("copy.mode.full", "Копіювати повністю")),
                    ft.Radio(value="partial", label=self.t("copy.mode.partial", "Копіювати частково")),
                ],
                spacing=14,
                wrap=True,
            ),
        )

        cat_copy_partial_levels_container = ft.Column(spacing=8)
        cat_copy_partial_block = ft.Container(
            content=ft.Column(
                [
                    ft.Text(
                        self.t("copy.select_levels_hint", "Оберіть, що переносимо по кожному рівню:"),
                        size=12,
                        color="grey700",
                    ),
                    cat_copy_partial_levels_container,
                ],
                spacing=6,
            ),
            visible=False,
        )
        cat_copy_levels_container = ft.Column(
            spacing=8, tight=True, scroll=ft.ScrollMode.ADAPTIVE
        )

        cat_copy_state = {
            "paths": [],
            "active_positions": [],
            "level_checks_by_pos": {},
            "level_rows": [],
            "level_titles": {},
        }

        def _selected_source_positions_for_target():
            if cat_copy_mode_f.value != "partial":
                return list(cat_copy_state.get("active_positions") or [])
            selected = []
            for pos in cat_copy_state.get("active_positions") or []:
                checks = (cat_copy_state.get("level_checks_by_pos") or {}).get(
                    pos
                ) or []
                if any(cb.value for _nid, cb in checks):
                    selected.append(pos)
            return selected

        def _sync_target_levels_with_source_selection():
            wanted_source_positions = _selected_source_positions_for_target()
            level_titles = cat_copy_state.get("level_titles") or {}
            current_rows = list(cat_copy_state.get("level_rows") or [])

            # Preserve user edits for existing source-driven rows.
            current_source_rows = {
                int(r.get("source_pos")): r
                for r in current_rows
                if r.get("source_pos") is not None
            }

            synced_rows = []
            for pos in wanted_source_positions:
                if pos in current_source_rows:
                    row = current_source_rows[pos]
                    row["source_pos"] = pos
                    synced_rows.append(row)
                else:
                    synced_rows.append(
                        {
                            "title": level_titles.get(pos, f"Рівень {pos}"),
                            "enabled": True,
                            "source_pos": pos,
                        }
                    )

            cat_copy_state["level_rows"] = synced_rows
            _render_cat_copy_levels_editor()

        def _render_cat_copy_levels_editor():
            rows = cat_copy_state.get("level_rows") or []

            def _move_level_row(index, shift):
                src = index
                dst = index + shift
                if src < 0 or src >= len(rows) or dst < 0 or dst >= len(rows):
                    return
                row = rows.pop(src)
                rows.insert(dst, row)
                _render_cat_copy_levels_editor()
                with contextlib.suppress(Exception):
                    cat_copy_levels_container.update()

            def _remove_level_row(index):
                if len(rows) <= 1:
                    return
                if 0 <= index < len(rows):
                    rows.pop(index)
                    _render_cat_copy_levels_editor()
                    with contextlib.suppress(Exception):
                        cat_copy_levels_container.update()

            def _add_level_row(_):
                if len(rows) >= 6:
                    return
                next_pos = len(rows) + 1
                rows.append(
                    {
                        "title": f"Рівень {next_pos}",
                        "enabled": True,
                        "source_pos": None,
                    }
                )
                _render_cat_copy_levels_editor()
                with contextlib.suppress(Exception):
                    cat_copy_levels_container.update()

            editor_controls = [
                ft.Row(
                    [
                        ft.Text(
                            "Порядок рівнів нової категорії:",
                            size=12,
                            color="grey700",
                            expand=True,
                        ),
                        ft.TextButton(
                            "Додати рівень",
                            icon=ft.Icons.ADD,
                            disabled=len(rows) >= 6,
                            on_click=_add_level_row,
                        ),
                    ],
                    spacing=6,
                )
            ]

            for i, row in enumerate(rows):

                def _on_title_change(e, _i=i):
                    rows[_i]["title"] = (
                        getattr(getattr(e, "control", None), "value", "") or ""
                    ).strip()

                def _on_enabled_change(e, _i=i):
                    rows[_i]["enabled"] = bool(
                        getattr(getattr(e, "control", None), "value", False)
                    )

                title_f = ft.TextField(
                    label=f"Назва рівня {i + 1}",
                    value=(row.get("title") or f"Рівень {i + 1}"),
                    border_radius=10,
                    on_change=_on_title_change,
                )
                enabled_cb = ft.Checkbox(
                    label="Увімкнено",
                    value=bool(row.get("enabled")),
                    on_change=_on_enabled_change,
                )
                editor_controls.append(
                    ft.Container(
                        border=ft.border.all(1, _BORDER2),
                        border_radius=8,
                        padding=8,
                        bgcolor=ft.Colors.WHITE,
                        content=ft.Column(
                            [
                                ft.Text(
                                    f"Джерело: {cat_copy_state.get('level_titles', {}).get(int(row.get('source_pos')), f'Рівень {int(row.get('source_pos'))}') if row.get('source_pos') is not None else 'Кастомний рівень'}",
                                    size=11,
                                    color="grey700",
                                ),
                                title_f,
                                ft.Row(
                                    [
                                        enabled_cb,
                                        ft.IconButton(
                                            icon=ft.Icons.KEYBOARD_ARROW_UP,
                                            tooltip="Підняти",
                                            disabled=i == 0,
                                            on_click=lambda _e, _i=i: _move_level_row(
                                                _i, -1
                                            ),
                                        ),
                                        ft.IconButton(
                                            icon=ft.Icons.KEYBOARD_ARROW_DOWN,
                                            tooltip="Опустити",
                                            disabled=i == len(rows) - 1,
                                            on_click=lambda _e, _i=i: _move_level_row(
                                                _i, 1
                                            ),
                                        ),
                                        ft.IconButton(
                                            icon=ft.Icons.DELETE_OUTLINE,
                                            tooltip="Видалити рівень",
                                            disabled=len(rows) <= 1,
                                            on_click=lambda _e, _i=i: _remove_level_row(
                                                _i
                                            ),
                                        ),
                                    ],
                                    spacing=6,
                                    wrap=True,
                                ),
                            ],
                            spacing=6,
                        ),
                    )
                )

            if len(editor_controls) == 1:
                editor_controls.append(
                    ft.Text("Немає рівнів для редагування", size=12, color="grey700")
                )
            cat_copy_levels_container.controls = editor_controls

        def _collect_level_paths(category_id, level_positions):
            out = []
            if not category_id or not level_positions:
                return out

            def _walk(index, parent_node_id, acc):
                pos = level_positions[index]
                nodes = [
                    n
                    for n in self.db.get_category_level_nodes(
                        category_id, pos, parent_node_id
                    )
                    if isinstance(n, dict) and n.get("id")
                ]

                if not nodes:
                    if acc:
                        out.append(list(acc))
                    return

                for node in nodes:
                    next_acc = acc + [
                        {
                            "position": pos,
                            "name": node.get("name") or "",
                            "node_id": node.get("id"),
                        }
                    ]
                    if index == len(level_positions) - 1:
                        out.append(next_acc)
                    else:
                        _walk(index + 1, node.get("id"), next_acc)

            _walk(0, None, [])
            return out

        def _reload_cat_copy_source_categories(_=None):
            rid = cat_copy_src_region_f.value
            cats = [
                c
                for c in self.db.get_company_categories(self.company_id, rid)
                if isinstance(c, dict) and c.get("id")
            ]
            cat_copy_src_category_f.options = [
                ft.dropdown.Option(key=c.get("id"), text=c.get("name", "Категорія"))
                for c in cats
            ]
            cat_copy_src_category_f.value = (
                cat_copy_src_category_f.options[0].key
                if cat_copy_src_category_f.options
                else None
            )
            _reload_cat_copy_metadata()

        def _reload_cat_copy_metadata(_=None):
            src_cat_id = cat_copy_src_category_f.value
            src_rid = cat_copy_src_region_f.value
            cat_copy_partial_levels_container.controls = []
            cat_copy_state["paths"] = []
            cat_copy_state["active_positions"] = []
            cat_copy_state["level_checks_by_pos"] = {}
            cat_copy_state["level_titles"] = {}

            if not src_cat_id:
                cat_copy_levels_container.controls = [
                    ft.Text("Оберіть категорію-джерело", size=12, color="grey700")
                ]
                with contextlib.suppress(Exception):
                    cat_copy_levels_container.update()
                return

            src_cat = self._find_row(
                self.db.get_company_categories(self.company_id, src_rid), src_cat_id
            )
            src_cfg = self.db.get_category_levels_config(src_cat or src_cat_id)
            src_levels = src_cfg.get("levels") or []

            level_titles = {
                int(level_def.get("position", 0)): (
                    level_def.get("title")
                    or f"Рівень {int(level_def.get('position', 0))}"
                )
                for level_def in src_levels
                if isinstance(level_def, dict)
                and int(level_def.get("position", 0)) in [1, 2, 3, 4, 5, 6]
            }
            cat_copy_state["level_titles"] = level_titles
            active_positions = [
                int(level_def.get("position"))
                for level_def in src_levels
                if isinstance(level_def, dict)
                and bool(level_def.get("enabled"))
                and int(level_def.get("position", 0)) in [1, 2, 3, 4, 5, 6]
            ]
            if not active_positions:
                active_positions = [1, 2]
            cat_copy_state["active_positions"] = active_positions

            # Build destination levels editor from the active positions discovered in the source,
            # pre-filling names from source titles so user can rename for the destination.
            levels_for_editor = [
                {
                    "position": pos,
                    "title": level_titles.get(pos, f"Рівень {pos}"),
                    "enabled": True,
                    "source_pos": pos,
                }
                for pos in active_positions
            ]
            cat_copy_state["level_rows"] = _build_levels_rows(levels_for_editor)
            if not cat_copy_state["level_rows"]:
                cat_copy_state["level_rows"] = _build_levels_rows(
                    [
                        {
                            "position": 1,
                            "title": "Рівень 1",
                            "enabled": True,
                            "source_pos": 1,
                        },
                        {
                            "position": 2,
                            "title": "Рівень 2",
                            "enabled": True,
                            "source_pos": 2,
                        },
                    ]
                )
            _sync_target_levels_with_source_selection()

            all_paths = _collect_level_paths(src_cat_id, active_positions)
            cat_copy_state["paths"] = all_paths

            if not all_paths:
                cat_copy_partial_levels_container.controls.append(
                    ft.Text(
                        "У вибраній категорії поки немає вузлів рівнів для копіювання",
                        size=12,
                        color="grey700",
                    )
                )
            else:
                items_by_pos = {}
                for path in all_paths:
                    for item in path:
                        pos = int(item.get("position", 0))
                        nid = item.get("node_id")
                        nname = item.get("name") or "-"
                        if pos < 1 or pos > 6 or not nid:
                            continue
                        items_by_pos.setdefault(pos, {})[nid] = nname

                for pos in active_positions:
                    level_name = level_titles.get(pos, f"Рівень {pos}")
                    pairs = sorted(
                        (items_by_pos.get(pos) or {}).items(),
                        key=lambda kv: (kv[1] or "").lower(),
                    )
                    checks = []
                    checks_col = ft.Column(
                        spacing=4, height=130, scroll=ft.ScrollMode.ADAPTIVE
                    )
                    for nid, nname in pairs:
                        cb = ft.Checkbox(label=nname or "-", value=True)
                        def _on_cb_change(_e, _container=cat_copy_levels_container):
                            _sync_target_levels_with_source_selection()
                            with contextlib.suppress(Exception):
                                _container.update()
                        cb.on_change = _on_cb_change
                        checks.append((nid, cb))
                        checks_col.controls.append(cb)

                    def _select_all(_e, _checks=checks):
                        for _nid, _cb in _checks:
                            _cb.value = True
                        _sync_target_levels_with_source_selection()
                        with contextlib.suppress(Exception):
                            cat_copy_levels_container.update()

                    def _clear_all(_e, _checks=checks):
                        for _nid, _cb in _checks:
                            _cb.value = False
                        _sync_target_levels_with_source_selection()
                        with contextlib.suppress(Exception):
                            cat_copy_levels_container.update()

                    cat_copy_state["level_checks_by_pos"][pos] = checks
                    cat_copy_partial_levels_container.controls.append(
                        ft.Container(
                            border=ft.border.all(1, _BORDER),
                            border_radius=8,
                            padding=8,
                            content=ft.Column(
                                [
                                    ft.Row(
                                        [
                                            ft.Text(
                                                f"{level_name}",
                                                weight="bold",
                                                color="grey800",
                                                expand=True,
                                            ),
                                            ft.TextButton(self.t("copy.select_all", "Все"), on_click=_select_all),
                                            ft.TextButton(
                                                self.t("copy.clear_all", "Очистити"), on_click=_clear_all
                                            ),
                                        ],
                                        spacing=6,
                                    ),
                                    (
                                        checks_col
                                        if checks_col.controls
                                        else ft.Text(
                                            self.t("copy.no_items", "Немає елементів"), size=12, color="grey700"
                                        )
                                    ),
                                ],
                                spacing=6,
                            ),
                        )
                    )

            default_name = (src_cat or {}).get("name") or ""
            if not (cat_copy_name_f.value or "").strip():
                cat_copy_name_f.value = default_name
            with contextlib.suppress(Exception):
                cat_copy_levels_container.update()
            with contextlib.suppress(Exception):
                cat_copy_name_f.update()

        def _copy_categories(_):
            src_rid = cat_copy_src_region_f.value
            src_cat_id = cat_copy_src_category_f.value
            dst_rid = cat_copy_dst_region_f.value
            if not src_rid or not src_cat_id or not dst_rid:
                self._show_snack(self.t("copy.select_source_dest", "Оберіть джерело і призначення"), "orange")
                return

            src_cat = self._find_row(
                self.db.get_company_categories(self.company_id, src_rid), src_cat_id
            )
            if not src_cat:
                self._show_snack(self.t("copy.source_category_not_found", "Категорію-джерело не знайдено"), "red")
                return

            level_rows = cat_copy_state.get("level_rows") or []
            levels_payload = []
            for i, row in enumerate(level_rows, start=1):
                if i > 6:
                    continue
                levels_payload.append(
                    {
                        "position": i,
                        "title": ((row.get("title") or "") or f"Рівень {i}").strip(),
                        "enabled": bool(row.get("enabled")),
                    }
                )

            source_pos_to_dst_pos = {}
            for i, row in enumerate(level_rows, start=1):
                src_pos = row.get("source_pos")
                if src_pos is None:
                    continue
                try:
                    src_pos = int(src_pos)
                except Exception:
                    continue
                if bool(row.get("enabled")):
                    source_pos_to_dst_pos[src_pos] = i

            target_name = (
                (cat_copy_name_f.value or "").strip()
                or src_cat.get("name")
                or "Категорія"
            )
            ok = self.db.create_category_with_access(
                self.company_id,
                dst_rid,
                target_name,
                hierarchy_config={
                    "target_margin_percent": src_cat.get("target_margin_percent"),
                    "use_products": False,
                    "levels": levels_payload,
                },
            )
            if not ok:
                self._show_snack("Не вдалося створити категорію призначення", "red")
                return

            dst_cat = self._find_by_name(
                self.db.get_company_categories(self.company_id, dst_rid), target_name
            )
            if not dst_cat or not dst_cat.get("id"):
                self._show_snack(
                    "Категорію створено, але не знайдено для копіювання рівнів",
                    "orange",
                )
                return

            selected_paths = []
            if cat_copy_mode_f.value == "partial":
                selected_by_pos = {
                    int(pos): {nid for nid, cb in checks if cb.value}
                    for pos, checks in (
                        cat_copy_state.get("level_checks_by_pos") or {}
                    ).items()
                }
                selected_paths = [
                    path
                    for path in (cat_copy_state.get("paths") or [])
                    if all(
                        int(item.get("position", 0)) not in source_pos_to_dst_pos
                        or item.get("node_id")
                        in selected_by_pos.get(int(item.get("position", 0)), set())
                        for item in path
                    )
                ]
            else:
                selected_paths = list(cat_copy_state.get("paths") or [])

            copied_paths = 0
            failed_paths = 0
            for path in selected_paths:
                transformed_path = []
                for level_item in path:
                    src_pos = int(level_item.get("position", 0))
                    if src_pos not in source_pos_to_dst_pos:
                        continue
                    transformed_path.append(
                        {
                            "position": source_pos_to_dst_pos[src_pos],
                            "name": level_item.get("name") or "",
                        }
                    )

                if not transformed_path:
                    continue

                parent_node_id = None
                path_ok = True
                for level_item in transformed_path:
                    created = self.db.find_or_create_level_node(
                        dst_cat.get("id"),
                        int(level_item.get("position", 0)),
                        level_item.get("name") or "",
                        parent_node_id=parent_node_id,
                    )
                    if not isinstance(created, dict) or not created.get("id"):
                        path_ok = False
                        break
                    parent_node_id = created.get("id")
                if path_ok:
                    copied_paths += 1
                else:
                    failed_paths += 1

            cat_copy_result.value = f"Готово: категорія=1, гілок скопійовано={copied_paths}, помилок={failed_paths}, режим={'частковий' if cat_copy_mode_f.value == 'partial' else 'повний'}"
            self._show_snack(
                self.t("copy.category_copy_done", "Копіювання категорії завершено"),
                "green" if failed_paths == 0 else "orange",
            )
            with contextlib.suppress(Exception):
                cat_copy_result.update()

        cat_copy_src_region_f.on_select = _reload_cat_copy_source_categories
        cat_copy_src_category_f.on_select = _reload_cat_copy_metadata

        def _on_cat_copy_mode_change(_):
            cat_copy_partial_block.visible = cat_copy_mode_f.value == "partial"
            _sync_target_levels_with_source_selection()
            with contextlib.suppress(Exception):
                cat_copy_partial_block.update()
            with contextlib.suppress(Exception):
                cat_copy_levels_container.update()

        cat_copy_mode_f.on_change = _on_cat_copy_mode_change

        # ---------------------------
        # ПАКУВАННЯ
        # ---------------------------
        pack_result = ft.Text("", size=12, color="grey700")
        pack_src_manager_f = ft.Dropdown(
            label="Джерело: користувач", options=manager_options, border_radius=10
        )
        pack_dst_manager_f = ft.Dropdown(
            label="Призначення: користувач", options=manager_options, border_radius=10
        )
        pack_src_region_f = ft.Dropdown(
            label="Джерело: регіон", options=[], border_radius=10
        )
        pack_src_item_f = ft.Dropdown(
            label="Джерело: пакування", options=[], border_radius=10
        )
        pack_dst_region_f = ft.Dropdown(
            label="Призначення: регіон", options=[], border_radius=10
        )

        def _reload_pack_source_regions(_=None):
            src_mid = pack_src_manager_f.value
            rows = _regions_for_manager(src_mid)
            pack_src_region_f.options = [
                ft.dropdown.Option(key=r.get("id"), text=r.get("name", "Регіон"))
                for r in rows
            ]
            pack_src_region_f.value = (
                pack_src_region_f.options[0].key if pack_src_region_f.options else None
            )
            _reload_pack_source_items()

        def _reload_pack_target_regions(_=None):
            dst_mid = pack_dst_manager_f.value
            rows = _regions_for_manager(dst_mid)
            pack_dst_region_f.options = [
                ft.dropdown.Option(key=r.get("id"), text=r.get("name", "Регіон"))
                for r in rows
            ]
            pack_dst_region_f.value = (
                pack_dst_region_f.options[0].key if pack_dst_region_f.options else None
            )
            with contextlib.suppress(Exception):
                pack_dst_region_f.update()

        def _reload_pack_source_items(_=None):
            rid = pack_src_region_f.value
            src_mid = pack_src_manager_f.value
            items = [
                p
                for p in self.db.get_packaging(
                    self.company_id, rid, manager_id=src_mid or None
                )
                if isinstance(p, dict) and p.get("id")
            ]
            pack_src_item_f.options = [
                ft.dropdown.Option(key=p.get("id"), text=p.get("name", "Пакування"))
                for p in items
            ]
            pack_src_item_f.value = (
                pack_src_item_f.options[0].key if pack_src_item_f.options else None
            )
            with contextlib.suppress(Exception):
                pack_src_item_f.update()

        pack_src_region_f.on_select = _reload_pack_source_items
        pack_src_manager_f.on_select = _reload_pack_source_regions
        pack_dst_manager_f.on_select = _reload_pack_target_regions

        def _resolve_supplier_id_for_target(
            src_supplier_id, target_region_id, target_manager_id=None
        ):
            if not src_supplier_id:
                return None
            src_sup = self._find_row(
                self.db.get_suppliers(self.company_id), src_supplier_id
            )
            if not src_sup:
                return None
            src_name = (src_sup.get("name") or "").strip().lower()
            target_sups = [
                s
                for s in self.db.get_suppliers(
                    self.company_id,
                    target_region_id,
                    manager_id=target_manager_id or None,
                )
                if isinstance(s, dict)
            ]
            for row in target_sups:
                if (row.get("name") or "").strip().lower() == src_name:
                    return row.get("id")
            return None

        def _copy_packaging_with_payload(src, dst_region_id, payload_override=None):
            src = src if isinstance(src, dict) else {}
            if not dst_region_id:
                self._show_snack("Оберіть регіон призначення", "orange")
                return False
            dst_mid = pack_dst_manager_f.value or None
            payload = {
                "id": src.get("id") and None,
                "company_id": self.company_id,
                "region_id": dst_region_id,
                "manager_id": dst_mid or "",
                "supplier_id": _resolve_supplier_id_for_target(
                    src.get("supplier_id"), dst_region_id, target_manager_id=dst_mid
                ),
                "name": src.get("name") or "Пакування",
                "size": src.get("size") or "",
                "price": src.get("price") or "",
                "image": src.get("image") or "",
                "notes": src.get("notes") or "",
            }
            if isinstance(payload_override, dict):
                payload.update(payload_override)
            payload.pop("id", None)
            return self.db.add_packaging(payload)

        def _copy_packaging_direct(_):
            if not pack_src_manager_f.value or not pack_dst_manager_f.value:
                self._show_snack("Оберіть користувача джерела і призначення", "orange")
                return
            src_id = pack_src_item_f.value
            dst_rid = pack_dst_region_f.value
            src_rows = [
                p
                for p in self.db.get_packaging(
                    self.company_id,
                    pack_src_region_f.value,
                    manager_id=pack_src_manager_f.value or None,
                )
                if isinstance(p, dict)
            ]
            src = self._find_row(src_rows, src_id)
            if not src or not dst_rid:
                self._show_snack("Оберіть елемент і регіон", "orange")
                return
            if _copy_packaging_with_payload(src, dst_rid):
                pack_result.value = "Готово: пакування скопійовано"
                self._show_snack("Пакування скопійовано", "green")
                with contextlib.suppress(Exception):
                    pack_result.update()
            else:
                self._show_snack("Не вдалося скопіювати пакування", "red")

        def _open_packaging_edit_modal(_):
            if not pack_src_manager_f.value or not pack_dst_manager_f.value:
                self._show_snack("Оберіть користувача джерела і призначення", "orange")
                return
            src_id = pack_src_item_f.value
            dst_rid = pack_dst_region_f.value
            src_rows = [
                p
                for p in self.db.get_packaging(
                    self.company_id,
                    pack_src_region_f.value,
                    manager_id=pack_src_manager_f.value or None,
                )
                if isinstance(p, dict)
            ]
            src = self._find_row(src_rows, src_id)
            if not src or not dst_rid:
                self._show_snack("Оберіть елемент і регіон призначення", "orange")
                return

            name_f = ft.TextField(
                label="Назва", value=src.get("name") or "", border_radius=10
            )
            size_f = ft.TextField(
                label="Розмір", value=src.get("size") or "", border_radius=10
            )
            price_f = ft.TextField(
                label="Ціна", value=str(src.get("price") or ""), border_radius=10
            )
            image_f = ft.TextField(
                label="Зображення URL", value=src.get("image") or "", border_radius=10
            )
            notes_f = ft.TextField(
                label="Нотатки",
                value=src.get("notes") or "",
                border_radius=10,
                multiline=True,
                min_lines=2,
                max_lines=4,
            )

            target_suppliers = [
                s
                for s in self.db.get_suppliers(
                    self.company_id,
                    dst_rid,
                    manager_id=pack_dst_manager_f.value or None,
                )
                if isinstance(s, dict) and s.get("id")
            ]
            sup_options = [ft.dropdown.Option(key="", text="(без постачальника)")] + [
                ft.dropdown.Option(key=s.get("id"), text=s.get("name", "Постачальник"))
                for s in target_suppliers
            ]
            default_sup = (
                _resolve_supplier_id_for_target(
                    src.get("supplier_id"),
                    dst_rid,
                    target_manager_id=pack_dst_manager_f.value or None,
                )
                or ""
            )
            supplier_f = ft.Dropdown(
                label="Постачальник",
                options=sup_options,
                value=default_sup,
                border_radius=10,
            )

            def _save(_):
                self._clear_field_errors(name_f, supplier_f)
                target_name = (name_f.value or "").strip() or "Пакування"
                target_supplier_id = supplier_f.value or None
                if self.db.packaging_name_exists(
                    self.company_id, target_name, dst_rid, target_supplier_id
                ):
                    self._mark_required(name_f, "Таке пакування вже існує")
                    self._show_snack(
                        "Таке пакування вже існує у регіоні призначення", "orange"
                    )
                    with contextlib.suppress(Exception):
                        name_f.update()
                    return
                ok = _copy_packaging_with_payload(
                    src,
                    dst_rid,
                    payload_override={
                        "name": target_name,
                        "size": (size_f.value or "").strip(),
                        "price": (price_f.value or "").strip(),
                        "image": (image_f.value or "").strip(),
                        "notes": (notes_f.value or "").strip(),
                        "supplier_id": target_supplier_id,
                    },
                )
                if not ok:
                    self._show_snack("Не вдалося скопіювати пакування", "red")
                    return
                self._close_dialog(dlg)
                pack_result.value = "Готово: пакування скопійовано з правками"
                self._show_snack("Пакування скопійовано з правками", "green")
                with contextlib.suppress(Exception):
                    pack_result.update()

            def _pack_edit_help(_):
                from views.help_utils import show_modal_help
                show_modal_help(self.content_area.page, "Редагування перед копіюванням: Пакування", [
                    self.t("copy.pack_edit_help.step1", "1. Відредагуйте поля пакування перед копіюванням: назва, розмір, ціна, постачальник, зображення, нотатки."),
                    self.t("copy.pack_edit_help.step2", "2. Натисніть «Копіювати з правками» щоб зберегти змінену копію."),
                ])

            dlg = ft.AlertDialog(
                title=ft.Row([
                    ft.Text("Редагування перед копіюванням: Пакування", expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_pack_edit_help),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
                content=ft.Column(
                    [
                        name_f,
                        ft.Row([size_f, price_f], spacing=8, wrap=True),
                        supplier_f,
                        image_f,
                        notes_f,
                    ],
                    spacing=8,
                    tight=True,
                    width=620,
                    scroll=ft.ScrollMode.ADAPTIVE,
                ),
                actions=[
                    ft.TextButton(
                        "Скасувати", on_click=lambda _: self._close_dialog(dlg)
                    ),
                    ft.FilledButton(self.t("copy.copy_with_edits", "Копіювати з правками"), on_click=_save),
                ],
            )
            self.content_area.page.show_dialog(dlg)

        # ---------------------------
        # ПОСТАЧАЛЬНИКИ
        # ---------------------------
        sup_result = ft.Text("", size=12, color="grey700")
        sup_src_manager_f = ft.Dropdown(
            label="Джерело: користувач", options=manager_options, border_radius=10
        )
        sup_dst_manager_f = ft.Dropdown(
            label="Призначення: користувач", options=manager_options, border_radius=10
        )
        sup_src_region_f = ft.Dropdown(
            label="Джерело: регіон", options=[], border_radius=10
        )
        sup_src_item_f = ft.Dropdown(
            label="Джерело: постачальник", options=[], border_radius=10
        )
        sup_dst_region_f = ft.Dropdown(
            label="Призначення: регіон", options=[], border_radius=10
        )

        def _reload_sup_source_regions(_=None):
            src_mid = sup_src_manager_f.value
            rows = _regions_for_manager(src_mid)
            sup_src_region_f.options = [
                ft.dropdown.Option(key=r.get("id"), text=r.get("name", "Регіон"))
                for r in rows
            ]
            sup_src_region_f.value = (
                sup_src_region_f.options[0].key if sup_src_region_f.options else None
            )
            _reload_sup_source_items()

        def _reload_sup_target_regions(_=None):
            dst_mid = sup_dst_manager_f.value
            rows = _regions_for_manager(dst_mid)
            sup_dst_region_f.options = [
                ft.dropdown.Option(key=r.get("id"), text=r.get("name", "Регіон"))
                for r in rows
            ]
            sup_dst_region_f.value = (
                sup_dst_region_f.options[0].key if sup_dst_region_f.options else None
            )
            with contextlib.suppress(Exception):
                sup_dst_region_f.update()

        def _reload_sup_source_items(_=None):
            rid = sup_src_region_f.value
            src_mid = sup_src_manager_f.value
            items = [
                s
                for s in self.db.get_suppliers(
                    self.company_id, rid, manager_id=src_mid or None
                )
                if isinstance(s, dict) and s.get("id")
            ]
            sup_src_item_f.options = [
                ft.dropdown.Option(key=s.get("id"), text=s.get("name", "Постачальник"))
                for s in items
            ]
            sup_src_item_f.value = (
                sup_src_item_f.options[0].key if sup_src_item_f.options else None
            )
            with contextlib.suppress(Exception):
                sup_src_item_f.update()

        sup_src_region_f.on_select = _reload_sup_source_items
        sup_src_manager_f.on_select = _reload_sup_source_regions
        sup_dst_manager_f.on_select = _reload_sup_target_regions

        def _copy_supplier_with_payload(src, dst_region_id, payload_override=None):
            src = src if isinstance(src, dict) else {}
            if not dst_region_id:
                self._show_snack("Оберіть регіон призначення", "orange")
                return False
            payload = {
                "company_id": self.company_id,
                "region_id": dst_region_id,
                "manager_id": (sup_dst_manager_f.value or ""),
                "name": src.get("name") or "Постачальник",
                "phone": src.get("phone") or "",
                "currency": src.get("currency") or "USD",
                "swift": src.get("swift") or "",
                "alipay_id": src.get("alipay_id") or "",
                "wechat_id": src.get("wechat_id") or "",
                "alipay_qr": src.get("alipay_qr") or "",
                "wechat_qr": src.get("wechat_qr") or "",
                "notes": src.get("notes") or "",
            }
            if isinstance(payload_override, dict):
                payload.update(payload_override)
            return self.db.add_supplier(payload)

        def _copy_supplier_direct(_):
            if not sup_src_manager_f.value or not sup_dst_manager_f.value:
                self._show_snack("Оберіть користувача джерела і призначення", "orange")
                return
            src_id = sup_src_item_f.value
            dst_rid = sup_dst_region_f.value
            src_rows = [
                s
                for s in self.db.get_suppliers(
                    self.company_id,
                    sup_src_region_f.value,
                    manager_id=sup_src_manager_f.value or None,
                )
                if isinstance(s, dict)
            ]
            src = self._find_row(src_rows, src_id)
            if not src or not dst_rid:
                self._show_snack("Оберіть елемент і регіон", "orange")
                return
            if _copy_supplier_with_payload(src, dst_rid):
                sup_result.value = "Готово: постачальника скопійовано"
                self._show_snack("Постачальника скопійовано", "green")
                with contextlib.suppress(Exception):
                    sup_result.update()
            else:
                self._show_snack("Не вдалося скопіювати постачальника", "red")

        def _open_supplier_edit_modal(_):
            if not sup_src_manager_f.value or not sup_dst_manager_f.value:
                self._show_snack("Оберіть користувача джерела і призначення", "orange")
                return
            src_id = sup_src_item_f.value
            dst_rid = sup_dst_region_f.value
            src_rows = [
                s
                for s in self.db.get_suppliers(
                    self.company_id,
                    sup_src_region_f.value,
                    manager_id=sup_src_manager_f.value or None,
                )
                if isinstance(s, dict)
            ]
            src = self._find_row(src_rows, src_id)
            if not src or not dst_rid:
                self._show_snack("Оберіть елемент і регіон призначення", "orange")
                return

            name_f = ft.TextField(
                label="Назва", value=src.get("name") or "", border_radius=10
            )
            phone_f = ft.TextField(
                label="Телефон", value=src.get("phone") or "", border_radius=10
            )
            currency_f = ft.TextField(
                label="Валюта", value=src.get("currency") or "USD", border_radius=10
            )
            swift_f = ft.TextField(
                label="SWIFT", value=src.get("swift") or "", border_radius=10
            )
            alipay_id_f = ft.TextField(
                label="Alipay ID", value=src.get("alipay_id") or "", border_radius=10
            )
            wechat_id_f = ft.TextField(
                label="Wechat ID", value=src.get("wechat_id") or "", border_radius=10
            )
            alipay_qr_f = ft.TextField(
                label="Alipay QR", value=src.get("alipay_qr") or "", border_radius=10
            )
            wechat_qr_f = ft.TextField(
                label="Wechat QR", value=src.get("wechat_qr") or "", border_radius=10
            )
            notes_f = ft.TextField(
                label="Нотатки",
                value=src.get("notes") or "",
                border_radius=10,
                multiline=True,
                min_lines=2,
                max_lines=4,
            )

            def _save(_):
                self._clear_field_errors(name_f)
                target_name = (name_f.value or "").strip() or "Постачальник"
                if self.db.supplier_name_exists(self.company_id, target_name, dst_rid):
                    self._mark_required(name_f, "Такий постачальник вже існує")
                    self._show_snack(
                        "Такий постачальник вже існує у регіоні призначення", "orange"
                    )
                    with contextlib.suppress(Exception):
                        name_f.update()
                    return
                ok = _copy_supplier_with_payload(
                    src,
                    dst_rid,
                    payload_override={
                        "name": target_name,
                        "phone": (phone_f.value or "").strip(),
                        "currency": (currency_f.value or "").strip() or "USD",
                        "swift": (swift_f.value or "").strip(),
                        "alipay_id": (alipay_id_f.value or "").strip(),
                        "wechat_id": (wechat_id_f.value or "").strip(),
                        "alipay_qr": (alipay_qr_f.value or "").strip(),
                        "wechat_qr": (wechat_qr_f.value or "").strip(),
                        "notes": (notes_f.value or "").strip(),
                    },
                )
                if not ok:
                    self._show_snack("Не вдалося скопіювати постачальника", "red")
                    return
                self._close_dialog(dlg)
                sup_result.value = "Готово: постачальника скопійовано з правками"
                self._show_snack("Постачальника скопійовано з правками", "green")
                with contextlib.suppress(Exception):
                    sup_result.update()

            def _sup_edit_help(_):
                from views.help_utils import show_modal_help
                show_modal_help(self.content_area.page, "Редагування перед копіюванням: Постачальник", [
                    self.t("copy.sup_edit_help.step1", "1. Відредагуйте поля постачальника перед копіюванням: назва, контакт, регіон, валюта, платіжні дані."),
                    self.t("copy.sup_edit_help.step2", "2. Натисніть «Копіювати з правками» щоб зберегти змінену копію."),
                ])

            dlg = ft.AlertDialog(
                title=ft.Row([
                    ft.Text("Редагування перед копіюванням: Постачальник", expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_sup_edit_help),
                ], vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
                content=ft.Column(
                    [
                        name_f,
                        ft.Row([phone_f, currency_f], spacing=8, wrap=True),
                        swift_f,
                        ft.Row([alipay_id_f, wechat_id_f], spacing=8, wrap=True),
                        ft.Row([alipay_qr_f, wechat_qr_f], spacing=8, wrap=True),
                        notes_f,
                    ],
                    spacing=8,
                    tight=True,
                    width=620,
                    scroll=ft.ScrollMode.ADAPTIVE,
                ),
                actions=[
                    ft.TextButton(
                        "Скасувати", on_click=lambda _: self._close_dialog(dlg)
                    ),
                    ft.FilledButton(self.t("copy.copy_with_edits", "Копіювати з правками"), on_click=_save),
                ],
            )
            self.content_area.page.show_dialog(dlg)

        # Початкові значення дропдаунів
        cat_copy_src_region_f.value = regions[0].get("id")
        cat_copy_dst_region_f.value = regions[0].get("id")
        _reload_cat_copy_source_categories()
        _on_cat_copy_mode_change(None)

        default_mid = (
            self.user_data.get("active_manager_id")
            or self.user_data.get("id")
            or managers[0].get("id")
        )
        if default_mid not in managers_by_id:
            default_mid = managers[0].get("id")

        pack_src_manager_f.value = default_mid
        pack_dst_manager_f.value = default_mid
        _reload_pack_source_regions()
        _reload_pack_target_regions()

        sup_src_manager_f.value = default_mid
        sup_dst_manager_f.value = default_mid
        _reload_sup_source_regions()
        _reload_sup_target_regions()

        self.content_area.controls.extend(
            [
                ft.Row([
                    ft.Text(self.t("copy.title", "Копіювання"), size=self.HEADER_SIZE, weight="bold"),
                    ft.IconButton(
                        icon=ft.Icons.HELP_OUTLINE,
                        icon_size=18,
                        tooltip=self.t("common.help.tooltip", "Довідка"),
                        on_click=lambda _e: self._show_help(),
                    ),
                ], spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
                ft.Text(
                    "Всі блоки згорнуті за замовчуванням. Відкритий блок залишається відкритим, поки ви з ним працюєте.",
                    size=self.META_TEXT_SIZE,
                    color="grey700",
                ),
                self._make_section(
                    "copy_categories_new",
                    "Копіювання Категорій",
                    [
                        ft.Text(
                            "Копіювання працює тільки з новою ієрархією рівнів. Доступні режими: повністю або частково (мультивибір по кожному рівню).",
                            size=12,
                            color="grey700",
                        ),
                        self._source_target_split(
                            source_controls=[
                                cat_copy_src_region_f,
                                cat_copy_src_category_f,
                                ft.Text("Режим копіювання:", size=12, color="grey700"),
                                cat_copy_mode_f,
                                cat_copy_partial_block,
                            ],
                            target_controls=[
                                cat_copy_dst_region_f,
                                cat_copy_name_f,
                                ft.Text(
                                    "Конфігурація рівнів (для нової категорії):",
                                    size=12,
                                    color="grey700",
                                ),
                                ft.Container(
                                    border=ft.border.all(1, _BORDER),
                                    border_radius=8,
                                    padding=8,
                                    bgcolor=ft.Colors.WHITE,
                                    height=260,
                                    content=cat_copy_levels_container,
                                ),
                            ],
                        ),
                        ft.Row(
                            [
                                ft.FilledButton(
                                    self.t("copy.copy_category_btn", "Копіювати категорію"),
                                    icon=ft.Icons.CONTENT_COPY,
                                    on_click=_copy_categories,
                                ),
                                ft.TextButton(
                                    self.t("copy.open_categories_btn", "Відкрити Категорії"),
                                    on_click=lambda _: self._open_module("categories"),
                                ),
                            ],
                            wrap=True,
                            spacing=8,
                        ),
                        cat_copy_result,
                    ],
                    default_open=False,
                ),
                self._make_section(
                    "copy_packaging",
                    "Копіювання Пакування",
                    [
                        ft.Text(
                            "Спочатку оберіть користувача-джерело і користувача-призначення, потім регіони.",
                            size=12,
                            color="grey700",
                        ),
                        self._source_target_split(
                            source_controls=[
                                pack_src_manager_f,
                                pack_src_region_f,
                                pack_src_item_f,
                            ],
                            target_controls=[
                                pack_dst_manager_f,
                                pack_dst_region_f,
                            ],
                        ),
                        ft.Row(
                            [
                                ft.FilledButton(
                                    "Копіювати",
                                    icon=ft.Icons.CONTENT_COPY,
                                    on_click=_copy_packaging_direct,
                                ),
                                ft.OutlinedButton(
                                    "Правити і копіювати",
                                    icon=ft.Icons.EDIT,
                                    on_click=_open_packaging_edit_modal,
                                ),
                                ft.TextButton(
                                    "Відкрити Пакування",
                                    on_click=lambda _: self._open_module("packaging"),
                                ),
                            ],
                            wrap=True,
                            spacing=8,
                        ),
                        pack_result,
                    ],
                    default_open=False,
                ),
                self._make_section(
                    "copy_suppliers",
                    "Копіювання Постачальників",
                    [
                        ft.Text(
                            "Спочатку оберіть користувача-джерело і користувача-призначення, потім регіони.",
                            size=12,
                            color="grey700",
                        ),
                        self._source_target_split(
                            source_controls=[
                                sup_src_manager_f,
                                sup_src_region_f,
                                sup_src_item_f,
                            ],
                            target_controls=[
                                sup_dst_manager_f,
                                sup_dst_region_f,
                            ],
                        ),
                        ft.Row(
                            [
                                ft.FilledButton(
                                    "Копіювати",
                                    icon=ft.Icons.CONTENT_COPY,
                                    on_click=_copy_supplier_direct,
                                ),
                                ft.OutlinedButton(
                                    "Правити і копіювати",
                                    icon=ft.Icons.EDIT,
                                    on_click=_open_supplier_edit_modal,
                                ),
                                ft.TextButton(
                                    "Відкрити Постачальники",
                                    on_click=lambda _: self._open_module("suppliers"),
                                ),
                            ],
                            wrap=True,
                            spacing=8,
                        ),
                        sup_result,
                    ],
                    default_open=False,
                ),
            ]
        )

        self.content_area.update()

    def _close_dialog(self, dlg):
        page = self.content_area.page if self.content_area else None
        if not page:
            logger.warning("[modal:copy-setup] close skipped: page is None")
            return
        logger.info(
            "[modal:copy-setup] close:start dlg=%s@%s open=%s overlay_before=%s",
            type(dlg).__name__,
            id(dlg),
            getattr(dlg, "open", None),
            len(getattr(page, "overlay", []) or []),
        )
        page.pop_dialog()
        logger.info(
            "[modal:copy-setup] close:finish dlg=%s@%s open=%s overlay_after=%s",
            type(dlg).__name__,
            id(dlg),
            getattr(dlg, "open", None),
            len(getattr(page, "overlay", []) or []),
        )
