"""User file storage — per-user per-region, nested folder tree, fast region switch."""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from datetime import datetime

import flet as ft

import i18n
from views.async_view_build_helper import safe_update_with_retry
from views.mobile_layout_helpers import is_mobile

_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

_ACCENT = "#1D4ED8"
_MUTED = "#64748B"
_BORDER = "#CBD5E1"
_SOFT = "#EFF6FF"
_TEXT = "#0F172A"
_OK_DK = "#0F7B53"
_ALT = "#F7FAFF"
_WARN_RED = "#DC2626"


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} КБ"
    return f"{n / (1024 * 1024):.1f} МБ"


def _file_icon(filename: str) -> tuple[str, str]:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "xlsx": (ft.Icons.TABLE_CHART, "#217346"),
        "xls": (ft.Icons.TABLE_CHART, "#217346"),
        "xlsm": (ft.Icons.TABLE_CHART, "#217346"),
        "docx": (ft.Icons.DESCRIPTION, "#2B579A"),
        "doc": (ft.Icons.DESCRIPTION, "#2B579A"),
        "pdf": (ft.Icons.PICTURE_AS_PDF, "#E53935"),
        "csv": (ft.Icons.TABLE_VIEW, _MUTED),
        "zip": (ft.Icons.FOLDER_ZIP_OUTLINED, _MUTED),
        "png": (ft.Icons.IMAGE_OUTLINED, _MUTED),
        "jpg": (ft.Icons.IMAGE_OUTLINED, _MUTED),
        "jpeg": (ft.Icons.IMAGE_OUTLINED, _MUTED),
    }.get(ext, (ft.Icons.ATTACH_FILE, _MUTED))


class FilesView:
    def __init__(self, content_area, user_data, db, current_region_name: str = ""):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.company_id = str(self.user_data.get("company_id") or "")

        own_id = str(self.user_data.get("id") or "")
        active_id = str(self.user_data.get("active_manager_id") or own_id)
        self.manager_id = active_id
        self._own_id = own_id
        self.t = i18n.bind(own_id)

        self.region_id = str(self.user_data.get("active_region_id") or "")
        self.region_label = str(
            current_region_name or self.user_data.get("active_region_label") or ""
        )

        # Read-only: viewing other user's files OR all-regions mode
        self._is_other_user = own_id != active_id and active_id != ""
        self._is_all_regions = self.region_id == ""
        self._readonly = self._is_other_user or self._is_all_regions

        # Tree state
        self._folders: list[dict] = []
        self._expanded_ids: set[str] = set()
        self._sel_folder_id: str | None = None
        self._files: list[dict] = []

        # File-list sort: col is "name"/"date"/None (None = original/upload order);
        # dir is 1 (asc) or -1 (desc). Clicking a header cycles asc -> desc -> none.
        self._file_sort_col: str | None = None
        self._file_sort_dir: int = 1

        # Persistent UI refs (set once in _build_layout, reused on reload)
        self._tree_col: ft.Column | None = None
        self._files_col: ft.Column | None = None
        self._header_text: ft.Text | None = None
        self._upload_btn: ft.FilledButton | None = None
        self._create_folder_row: ft.Control | None = None
        self._region_badge_text: ft.Text | None = None

        self._layout_built = False
        self._page_ref = None

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    def build(self):
        """Build layout once; subsequent calls just refresh data."""
        if self._layout_built:
            self._schedule_reload_tree()
            return
        self._layout_built = True
        self._build_layout()
        self._schedule_reload_tree()

    def resume(self):
        """Called when tab becomes active — only refresh data, no layout rebuild."""
        if self._layout_built:
            self._schedule_reload_tree()
        else:
            self.build()

    def _schedule_reload_tree(self) -> None:
        """Run the folder-tree fetch off-thread instead of blocking the event
        loop — _reload_tree_async (already used by set_region) already does
        this correctly with a loading stub; build()/resume() just called the
        blocking self._reload_tree() directly instead of reusing it."""
        page = self._page()
        if page and hasattr(page, "run_task"):
            with contextlib.suppress(Exception):
                page.run_task(self._reload_tree_async)
                return
        # No page yet (shouldn't normally happen) — fall back to the
        # blocking fetch rather than silently loading nothing.
        self._reload_tree()

    async def set_region(self, region_id: str, region_label: str) -> None:
        """Called by dashboard on region change. No layout rebuild — only the
        folder tree is re-fetched, and that DB call runs off-thread so the
        UI never blocks while switching regions."""
        self.region_id = region_id
        self.region_label = region_label
        self._is_all_regions = region_id == ""
        self._readonly = self._is_other_user or self._is_all_regions
        self._sel_folder_id = None
        self._files = []

        if not self._layout_built:
            return

        # Update region badge
        if self._region_badge_text is not None:
            self._region_badge_text.value = region_label or self.t(
                "files.all_regions", "Всі регіони"
            )
            with contextlib.suppress(Exception):
                self._region_badge_text.update()

        # Toggle create-folder row + upload button
        self._sync_readonly_ui()

        # Clear file panel
        self._rebuild_files_col()

        # Reload tree data without blocking the event loop
        await self._reload_tree_async()

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def _page(self):
        try:
            pg = self.content_area.page if self.content_area else None
        except RuntimeError:
            pg = None
        if pg is not None:
            self._page_ref = pg
        return self._page_ref or pg

    def _snack(self, msg: str, color=ft.Colors.GREEN):
        pg = self._page()
        if not pg:
            return
        with contextlib.suppress(Exception):
            pg.snack_bar = ft.SnackBar(
                ft.Text(msg, color="white"), bgcolor=color, duration=2500
            )
            pg.snack_bar.open = True
            pg.update()

    def _sync_readonly_ui(self):
        if self._create_folder_row is not None:
            self._create_folder_row.visible = not self._readonly
            with contextlib.suppress(Exception):
                self._create_folder_row.update()
        if self._upload_btn is not None and self._readonly:
            self._upload_btn.visible = False
            with contextlib.suppress(Exception):
                self._upload_btn.update()

    # ------------------------------------------------------------------ #
    #  Layout (built once)                                                 #
    # ------------------------------------------------------------------ #

    def _build_layout(self):
        ca = self.content_area

        # ---- Tree column (left) ----
        tree_col = ft.Column(
            [], spacing=0, tight=True, scroll=ft.ScrollMode.AUTO, expand=True
        )
        self._tree_col = tree_col

        # Create root folder row
        create_tf = ft.TextField(
            hint_text=self.t("files.new_folder_hint", "Нова папка…"),
            dense=True,
            text_size=12,
            expand=True,
            border_radius=6,
            content_padding=ft.padding.symmetric(horizontal=8, vertical=4),
        )

        def _create_root(_):
            name = (create_tf.value or "").strip()
            if not name:
                return
            gid = self.db.create_user_file_group(
                self.company_id, self.manager_id, self.region_id, name, parent_id=None
            )
            if gid:
                create_tf.value = ""
                with contextlib.suppress(Exception):
                    create_tf.update()
                self._reload_tree()
            else:
                self._snack(
                    self.t("files.err.create_folder", "Помилка створення папки"),
                    ft.Colors.RED,
                )

        create_tf.on_submit = _create_root

        create_folder_row = ft.Row(
            [
                ft.Icon(ft.Icons.CREATE_NEW_FOLDER_OUTLINED, size=14, color=_OK_DK),
                create_tf,
                ft.IconButton(
                    ft.Icons.ADD_CIRCLE_OUTLINE,
                    icon_color=_OK_DK,
                    icon_size=16,
                    tooltip=self.t("files.tooltip.create_folder", "Створити папку"),
                    on_click=_create_root,
                    style=ft.ButtonStyle(padding=ft.padding.all(2)),
                ),
            ],
            spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            visible=not self._readonly,
        )
        self._create_folder_row = create_folder_row

        # Region badge
        region_badge_text = ft.Text(
            self.region_label or self.t("files.all_regions", "Всі регіони"),
            size=10,
            color=_ACCENT,
        )
        self._region_badge_text = region_badge_text

        region_badge = ft.Container(
            bgcolor=_SOFT,
            border=ft.border.all(1, _BORDER),
            border_radius=6,
            padding=ft.padding.symmetric(horizontal=8, vertical=3),
            content=region_badge_text,
        )

        left_panel = ft.Column(
            [
                ft.Row(
                    [
                        ft.Icon(ft.Icons.FOLDER_OUTLINED, size=15, color=_ACCENT),
                        ft.Text(
                            self.t("files.folders_label", "Папки"),
                            size=12,
                            weight=ft.FontWeight.W_700,
                            color=_ACCENT,
                        ),
                        ft.Container(expand=True),
                        region_badge,
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=4,
                ),
                ft.Divider(height=1, thickness=1, color=_BORDER),
                ft.Container(expand=True, content=tree_col),
                ft.Divider(height=1, thickness=1, color=_BORDER),
                create_folder_row,
            ],
            spacing=6,
            expand=True,
        )

        # ---- Files column (right) ----
        header_text = ft.Text(
            self.t("files.select_folder", "Оберіть папку"),
            size=13,
            color=_MUTED,
            italic=True,
            expand=True,
        )
        self._header_text = header_text

        upload_btn = ft.FilledButton(
            self.t("files.upload_btn", "Завантажити файл"),
            icon=ft.Icons.UPLOAD_FILE,
            visible=False,
            on_click=lambda _: self._page()
            and self._page().run_task(self._pick_and_upload),
            style=ft.ButtonStyle(
                bgcolor=_ACCENT,
                padding=ft.padding.symmetric(horizontal=14, vertical=7),
                text_style=ft.TextStyle(size=12),
            ),
        )
        self._upload_btn = upload_btn

        files_col = ft.Column(
            [],
            spacing=0,
            tight=True,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        self._files_col = files_col

        right_panel = ft.Column(
            [
                ft.Row(
                    [header_text, upload_btn],
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Divider(height=1, thickness=1, color=_BORDER),
                ft.Container(expand=True, content=files_col),
            ],
            spacing=8,
            expand=True,
        )

        # ---- Title row ----
        title_row = ft.Row(
            [
                ft.Icon(ft.Icons.FOLDER_COPY_OUTLINED, size=20, color=_ACCENT),
                ft.Text(
                    self.t("files.title", "Мої файли"),
                    size=17,
                    weight=ft.FontWeight.W_700,
                    color=_TEXT,
                ),
                ft.IconButton(
                    icon=ft.Icons.HELP_OUTLINE,
                    icon_size=18,
                    icon_color=_ACCENT,
                    tooltip=self.t("common.help.tooltip", "Довідка"),
                    on_click=lambda _: self._show_help(),
                ),
            ],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # ---- Main layout ----
        # A fixed 240px folder-tree column next to an expand=True files column
        # works on desktop's wide content area, but on a narrow phone screen
        # 240px alone can eat most/all of the available width, squeezing (or
        # overflowing) the files panel down to nothing. On mobile, stack the
        # tree above the files list instead of side-by-side.
        if is_mobile(getattr(ca, "page", None)):
            layout = ft.Column(
                [
                    ft.Container(
                        height=260,
                        content=left_panel,
                        border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
                        padding=ft.padding.only(bottom=8),
                    ),
                    ft.Container(
                        expand=True,
                        content=right_panel,
                        padding=ft.padding.only(top=8),
                    ),
                ],
                spacing=0,
                expand=True,
            )
        else:
            layout = ft.Row(
                [
                    ft.Container(
                        width=240,
                        content=left_panel,
                        border=ft.border.only(right=ft.BorderSide(1, _BORDER)),
                        padding=ft.padding.only(right=12, top=4, bottom=4),
                    ),
                    ft.Container(
                        expand=True,
                        content=right_panel,
                        padding=ft.padding.only(left=16, top=4, bottom=4, right=4),
                    ),
                ],
                spacing=0,
                expand=True,
                vertical_alignment=ft.CrossAxisAlignment.START,
            )

        ca.controls.clear()
        ca.controls.append(
            ft.Column(
                [
                    ft.Container(
                        content=title_row,
                        padding=ft.padding.symmetric(horizontal=16, vertical=8),
                    ),
                    ft.Container(
                        expand=True,
                        content=layout,
                        padding=ft.padding.symmetric(horizontal=16),
                    ),
                ],
                spacing=0,
                expand=True,
            )
        )
        with contextlib.suppress(Exception):
            ca.update()

    # ------------------------------------------------------------------ #
    #  Tree reload                                                         #
    # ------------------------------------------------------------------ #

    def _reload_tree(self):
        self._folders = (
            self.db.get_user_file_groups(
                self.company_id, self.manager_id, self.region_id
            )
            or []
        )
        self._rebuild_tree_col()

    def _render_tree_loading_stub(self):
        col = self._tree_col
        if col is None:
            return
        col.controls.clear()
        col.controls.append(
            ft.Container(
                padding=ft.padding.symmetric(vertical=20, horizontal=4),
                content=ft.Row(
                    [
                        ft.ProgressRing(
                            width=14, height=14, stroke_width=2, color=_ACCENT
                        ),
                        ft.Text(
                            self.t("files.loading", "Завантаження…"),
                            size=11,
                            color=_MUTED,
                            italic=True,
                        ),
                    ],
                    spacing=8,
                ),
            )
        )
        with contextlib.suppress(Exception):
            col.update()

    async def _reload_tree_async(self):
        self._render_tree_loading_stub()
        # Capture the region this load was started for — if the user clicks
        # another region again before this DB call returns, the stale result
        # must not overwrite the newer one.
        _region_at_start = self.region_id
        folders = (
            await asyncio.to_thread(
                self.db.get_user_file_groups,
                self.company_id,
                self.manager_id,
                self.region_id,
            )
            or []
        )
        if _region_at_start != self.region_id:
            return
        self._folders = folders
        self._rebuild_tree_col()

    def _get_children(self, parent_id: str) -> list[dict]:
        return [f for f in self._folders if str(f.get("parent_id") or "") == parent_id]

    def _rebuild_tree_col(self):
        col = self._tree_col
        if col is None:
            return
        col.controls.clear()

        roots = [f for f in self._folders if not f.get("parent_id")]
        if not roots:
            col.controls.append(
                ft.Container(
                    padding=ft.padding.symmetric(vertical=20, horizontal=4),
                    content=ft.Text(
                        self.t(
                            "files.no_folders",
                            "Немає папок."
                            + ("" if self._readonly else " Створіть першу."),
                        ),
                        size=11,
                        color=_MUTED,
                        italic=True,
                    ),
                )
            )
        else:
            col.controls.extend(self._build_tree_items(roots, depth=0))

        safe_update_with_retry(col, self._page())

    def _build_tree_items(self, folders: list[dict], depth: int) -> list[ft.Control]:
        items = []
        for f in sorted(folders, key=lambda x: (x.get("name") or "").lower()):
            fid = str(f.get("id") or "")
            children = self._get_children(fid)
            is_exp = fid in self._expanded_ids
            is_sel = fid == self._sel_folder_id
            items.append(
                self._build_folder_item(f, depth, bool(children), is_exp, is_sel)
            )
            if is_exp:
                items.extend(self._build_tree_items(children, depth + 1))
        return items

    def _build_folder_item(
        self,
        folder: dict,
        depth: int,
        has_children: bool,
        is_expanded: bool,
        is_sel: bool,
    ) -> ft.Control:
        fid = str(folder.get("id") or "")
        name = str(folder.get("name") or "")
        region_id = str(folder.get("region_id") or "")

        def _toggle(_):
            if fid in self._expanded_ids:
                self._expanded_ids.discard(fid)
            else:
                self._expanded_ids.add(fid)
            self._rebuild_tree_col()

        def _select(_):
            self._sel_folder_id = fid
            if self._header_text:
                self._header_text.value = name
                self._header_text.italic = False
                self._header_text.color = _TEXT
                with contextlib.suppress(Exception):
                    self._header_text.update()
            if self._upload_btn:
                self._upload_btn.visible = not self._readonly
                with contextlib.suppress(Exception):
                    self._upload_btn.update()
            self._reload_files()
            self._rebuild_tree_col()

        # Expand arrow (or spacer)
        if has_children:
            expand_ctl = ft.IconButton(
                icon=ft.Icons.EXPAND_LESS if is_expanded else ft.Icons.EXPAND_MORE,
                icon_size=13,
                icon_color=_MUTED,
                on_click=_toggle,
                style=ft.ButtonStyle(padding=ft.padding.all(0)),
                width=18,
                height=20,
            )
        else:
            expand_ctl = ft.Container(width=18)

        folder_icon = ft.Icon(
            (
                ft.Icons.FOLDER_OPEN
                if (is_expanded or is_sel)
                else ft.Icons.FOLDER_OUTLINED
            ),
            size=14,
            color=_ACCENT if is_sel else _MUTED,
        )

        # Action buttons (hidden when not selected OR readonly)
        action_btns = []
        if is_sel and not self._readonly:

            def _add_sub(_, _fid=fid):
                self._show_subfolder_dialog(_fid)

            def _rename(_, _fid=fid, _n=name):
                self._show_rename_folder_dialog(_fid, _n)

            def _del(_, _fid=fid, _n=name):
                self._confirm_delete_folder(_fid, _n)

            action_btns = [
                ft.IconButton(
                    ft.Icons.CREATE_NEW_FOLDER_OUTLINED,
                    icon_size=12,
                    icon_color=_MUTED,
                    tooltip=self.t("files.tooltip.add_subfolder", "Підпапка"),
                    on_click=_add_sub,
                    style=ft.ButtonStyle(padding=ft.padding.all(1)),
                    width=22,
                    height=22,
                ),
                ft.IconButton(
                    ft.Icons.EDIT_OUTLINED,
                    icon_size=12,
                    icon_color=_MUTED,
                    tooltip=self.t("files.tooltip.rename", "Перейменувати"),
                    on_click=_rename,
                    style=ft.ButtonStyle(padding=ft.padding.all(1)),
                    width=22,
                    height=22,
                ),
                ft.IconButton(
                    ft.Icons.DELETE_OUTLINE,
                    icon_size=12,
                    icon_color=ft.Colors.RED_400,
                    tooltip=self.t("files.tooltip.delete_folder", "Видалити"),
                    on_click=_del,
                    style=ft.ButtonStyle(padding=ft.padding.all(1)),
                    width=22,
                    height=22,
                ),
            ]

        # Region badge (all-regions mode, root folders only)
        region_badge = []
        if self._is_all_regions and region_id and not folder.get("parent_id"):
            region_badge = [
                ft.Container(
                    bgcolor=_SOFT,
                    border=ft.border.all(1, _BORDER),
                    border_radius=4,
                    padding=ft.padding.symmetric(horizontal=4, vertical=1),
                    content=ft.Text(
                        (region_id[:6] + "…") if len(region_id) > 6 else region_id,
                        size=9,
                        color=_ACCENT,
                    ),
                )
            ]

        row = ft.Row(
            [
                ft.Container(width=depth * 14),
                expand_ctl,
                folder_icon,
                ft.Container(
                    expand=True,
                    content=ft.Text(
                        name,
                        size=12,
                        weight=ft.FontWeight.W_600 if is_sel else ft.FontWeight.W_400,
                        color=_ACCENT if is_sel else _TEXT,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    on_click=_select,
                    padding=ft.padding.only(left=3),
                ),
                *region_badge,
                *action_btns,
            ],
            spacing=2,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        return ft.Container(
            bgcolor=_SOFT if is_sel else "transparent",
            border_radius=5,
            border=ft.border.all(1, _ACCENT) if is_sel else None,
            padding=ft.padding.symmetric(horizontal=4, vertical=3),
            margin=ft.margin.only(bottom=1),
            content=row,
        )

    # ------------------------------------------------------------------ #
    #  Folder dialogs                                                      #
    # ------------------------------------------------------------------ #

    def _show_subfolder_dialog(self, parent_id: str):
        pg = self._page()
        if not pg:
            return
        tf = ft.TextField(
            hint_text=self.t("files.new_folder_hint", "Нова папка…"),
            autofocus=True,
            dense=True,
            text_size=13,
            border_radius=6,
            content_padding=ft.padding.symmetric(horizontal=10, vertical=6),
        )
        dlg: list = [None]

        def _save(_):
            name = (tf.value or "").strip()
            if not name:
                return
            with contextlib.suppress(Exception):
                pg.pop_dialog()
            gid = self.db.create_user_file_group(
                self.company_id,
                self.manager_id,
                self.region_id,
                name,
                parent_id=parent_id,
            )
            if gid:
                self._expanded_ids.add(parent_id)
                self._reload_tree()
            else:
                self._snack(
                    self.t("files.err.create_folder", "Помилка створення папки"),
                    ft.Colors.RED,
                )

        tf.on_submit = _save
        dlg[0] = ft.AlertDialog(
            modal=True,
            title=ft.Text(
                self.t("files.dlg.new_subfolder", "Нова підпапка"),
                size=14,
                weight="bold",
            ),
            content=ft.Container(width=320, content=tf),
            actions=[
                ft.TextButton(
                    self.t("common.cancel", "Скасувати"),
                    on_click=lambda _: pg.pop_dialog(),
                ),
                ft.FilledButton(self.t("common.create", "Створити"), on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        pg.open(dlg[0])

    def _show_rename_folder_dialog(self, folder_id: str, current_name: str):
        pg = self._page()
        if not pg:
            return
        tf = ft.TextField(
            value=current_name,
            autofocus=True,
            dense=True,
            text_size=13,
            border_radius=6,
            content_padding=ft.padding.symmetric(horizontal=10, vertical=6),
        )
        dlg: list = [None]

        def _save(_):
            name = (tf.value or "").strip()
            if not name or name == current_name:
                with contextlib.suppress(Exception):
                    pg.pop_dialog()
                return
            with contextlib.suppress(Exception):
                pg.pop_dialog()
            ok = self.db.rename_user_file_group(folder_id, name)
            if ok:
                for f in self._folders:
                    if str(f.get("id") or "") == folder_id:
                        f["name"] = name
                        break
                if self._sel_folder_id == folder_id and self._header_text:
                    self._header_text.value = name
                    with contextlib.suppress(Exception):
                        self._header_text.update()
                self._rebuild_tree_col()
            else:
                self._snack(
                    self.t("files.err.rename", "Помилка перейменування"), ft.Colors.RED
                )

        tf.on_submit = _save
        dlg[0] = ft.AlertDialog(
            modal=True,
            title=ft.Text(
                self.t("files.dlg.rename_folder", "Перейменувати папку"),
                size=14,
                weight="bold",
            ),
            content=ft.Container(width=320, content=tf),
            actions=[
                ft.TextButton(
                    self.t("common.cancel", "Скасувати"),
                    on_click=lambda _: pg.pop_dialog(),
                ),
                ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        pg.open(dlg[0])

    def _confirm_delete_folder(self, folder_id: str, name: str):
        pg = self._page()
        if not pg:
            return
        dlg: list = [None]

        def _do(_):
            with contextlib.suppress(Exception):
                pg.pop_dialog()
            ok = self.db.delete_user_file_group(folder_id)
            if ok:
                if self._sel_folder_id == folder_id:
                    self._sel_folder_id = None
                    self._files = []
                    self._rebuild_files_col()
                    if self._header_text:
                        self._header_text.value = self.t(
                            "files.select_folder", "Оберіть папку"
                        )
                        self._header_text.italic = True
                        self._header_text.color = _MUTED
                        with contextlib.suppress(Exception):
                            self._header_text.update()
                    if self._upload_btn:
                        self._upload_btn.visible = False
                        with contextlib.suppress(Exception):
                            self._upload_btn.update()
                self._expanded_ids.discard(folder_id)
                self._snack(
                    self.t("files.folder_deleted", 'Папку "{name}" видалено').format(
                        name=name
                    ),
                    ft.Colors.ORANGE,
                )
                self._reload_tree()
            else:
                self._snack(
                    self.t("files.err.delete", "Помилка видалення"), ft.Colors.RED
                )

        dlg[0] = ft.AlertDialog(
            modal=True,
            title=ft.Text(
                self.t("files.delete_folder_confirm", 'Видалити "{name}"?').format(
                    name=name
                ),
                size=14,
                weight="bold",
            ),
            content=ft.Text(
                self.t(
                    "files.delete_folder_body",
                    "Всі файли та підпапки будуть видалені безповоротно.",
                ),
                size=12,
                color=_MUTED,
            ),
            actions=[
                ft.TextButton(
                    self.t("common.cancel", "Скасувати"),
                    on_click=lambda _: pg.pop_dialog(),
                ),
                ft.FilledButton(
                    self.t("common.delete", "Видалити"),
                    icon=ft.Icons.DELETE_OUTLINE,
                    on_click=_do,
                    style=ft.ButtonStyle(bgcolor=ft.Colors.RED_600),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        pg.open(dlg[0])

    # ------------------------------------------------------------------ #
    #  Files                                                               #
    # ------------------------------------------------------------------ #

    def _reload_files(self):
        if not self._sel_folder_id:
            return
        self._files = self.db.get_user_files_meta(self._sel_folder_id) or []
        self._rebuild_files_col()

    def _parse_file_date(self, f: dict):
        created_at = f.get("created_at")
        try:
            if isinstance(created_at, str):
                return datetime.fromisoformat(created_at)
            return created_at
        except Exception:
            return None

    def _sorted_files(self) -> list[dict]:
        """Return self._files in the order the current header sort dictates.
        col=None means original (upload/created_at ASC) order — no re-sort needed."""
        col = self._file_sort_col
        if not col:
            return self._files
        reverse = self._file_sort_dir < 0

        def _name_key(f):
            return str(f.get("original_filename") or "").lower()

        def _date_key(f):
            return self._parse_file_date(f) or datetime.min

        key = _name_key if col == "name" else _date_key
        return sorted(self._files, key=key, reverse=reverse)

    def _toggle_file_sort(self, col_key: str, _=None):
        # 3-state cycle per column: asc -> desc -> none (back to upload order).
        if self._file_sort_col != col_key:
            self._file_sort_col = col_key
            self._file_sort_dir = 1
        elif self._file_sort_dir == 1:
            self._file_sort_dir = -1
        else:
            self._file_sort_col = None
            self._file_sort_dir = 1
        self._rebuild_files_col()

    def _sort_header_cell(
        self, label: str, col_key: str, *, expand=None, width=None
    ) -> ft.Control:
        active = self._file_sort_col == col_key
        if active and self._file_sort_dir == 1:
            arrow = ft.Icons.ARROW_UPWARD
        elif active and self._file_sort_dir == -1:
            arrow = ft.Icons.ARROW_DOWNWARD
        else:
            arrow = None
        content = ft.Row(
            [
                ft.Text(
                    label,
                    size=10,
                    color=_ACCENT if active else _MUTED,
                    weight="bold",
                ),
                (
                    ft.Icon(arrow, size=11, color=_ACCENT)
                    if arrow
                    else ft.Container(width=11)
                ),
            ],
            spacing=2,
            tight=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        return ft.Container(
            content=content,
            expand=expand,
            width=width,
            ink=True,
            on_click=lambda _, _ck=col_key: self._toggle_file_sort(_ck),
        )

    def _rebuild_files_col(self):
        col = self._files_col
        if col is None:
            return
        col.controls.clear()

        if not self._sel_folder_id:
            safe_update_with_retry(col, self._page())
            return

        if not self._files:
            col.controls.append(
                ft.Container(
                    padding=ft.padding.symmetric(vertical=40),
                    alignment=ft.Alignment(0, 0),
                    content=ft.Column(
                        [
                            ft.Icon(
                                ft.Icons.FOLDER_OPEN_OUTLINED, size=44, color="#CBD5E1"
                            ),
                            ft.Text(
                                self.t("files.empty_folder", "Папка порожня."),
                                size=12,
                                color=_MUTED,
                                italic=True,
                            ),
                            *(
                                [
                                    ft.Text(
                                        self.t(
                                            "files.upload_hint",
                                            "Натисніть «Завантажити файл» щоб додати.",
                                        ),
                                        size=11,
                                        color="#94A3B8",
                                    )
                                ]
                                if not self._readonly
                                else []
                            ),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=6,
                    ),
                )
            )
        else:
            col.controls.append(
                ft.Container(
                    bgcolor=_ALT,
                    border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
                    padding=ft.padding.symmetric(horizontal=10, vertical=5),
                    content=ft.Row(
                        [
                            ft.Container(width=22),
                            self._sort_header_cell(
                                self.t("files.col.file", "Файл"), "name", expand=True
                            ),
                            ft.Text(
                                self.t("files.col.size", "Розмір"),
                                size=10,
                                color=_MUTED,
                                weight="bold",
                                width=72,
                            ),
                            self._sort_header_cell(
                                self.t("files.col.date", "Дата"), "date", width=86
                            ),
                            ft.Container(width=self._actions_width()),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )
            for f in self._sorted_files():
                col.controls.append(self._build_file_row(f))

        safe_update_with_retry(col, self._page())

    def _actions_width(self) -> int:
        # Open + Save = 2 buttons; + Rename + Move + Delete for own user
        if self._readonly:
            return 76  # open + save
        return 154  # open + save + rename + move + delete

    def _build_file_row(self, f: dict) -> ft.Control:
        fid = str(f.get("id") or "")
        fname = str(f.get("original_filename") or "")
        fsize = int(f.get("file_size") or 0)
        created_at = f.get("created_at")
        try:
            dt = (
                datetime.fromisoformat(str(created_at))
                if isinstance(created_at, str)
                else created_at
            )
            date_str = (
                dt.strftime("%d.%m.%Y")
                if hasattr(dt, "strftime")
                else str(created_at)[:10]
            )
        except Exception:
            date_str = str(created_at or "")[:10]

        icon_name, icon_color = _file_icon(fname)

        async def _open(_fid=fid, _fname=fname):
            data = await asyncio.to_thread(self.db.get_user_file_data, _fid)
            if not data:
                self._snack(
                    self.t("files.err.no_data", "Не вдалося отримати файл"),
                    ft.Colors.RED,
                )
                return
            try:
                tmp_path = os.path.join(tempfile.gettempdir(), _fname)
                with open(tmp_path, "wb") as fh:
                    fh.write(data)
                os.startfile(tmp_path)
            except Exception as ex:
                self._snack(f"Помилка відкриття: {ex}", ft.Colors.RED)

        async def _save(_fid=fid, _fname=fname):
            pg = self._page()
            if not pg:
                return
            fp = ft.FilePicker()
            pg.services.append(fp)
            pg.update()
            await asyncio.sleep(0.2)
            save_path = await fp.save_file(
                dialog_title=self.t("files.save_dialog", 'Зберегти "{fname}"').format(
                    fname=_fname
                ),
                file_name=_fname,
                allowed_extensions=(
                    [_fname.rsplit(".", 1)[-1]] if "." in _fname else None
                ),
            )
            with contextlib.suppress(Exception):
                pg.services.remove(fp)
                pg.update()
            if not save_path:
                return
            data = await asyncio.to_thread(self.db.get_user_file_data, _fid)
            if not data:
                self._snack(
                    self.t("files.err.no_data", "Не вдалося отримати файл"),
                    ft.Colors.RED,
                )
                return
            try:
                with open(save_path, "wb") as fh:
                    fh.write(data)
                self._snack(
                    self.t("files.saved", "Збережено: {name}").format(
                        name=os.path.basename(save_path)
                    ),
                    ft.Colors.GREEN,
                )
            except Exception as ex:
                self._snack(f"Помилка збереження: {ex}", ft.Colors.RED)

        def _move(_e=None, _fid=fid, _fname=fname):
            self._show_move_dialog(_fid, _fname)

        def _rename(_e=None, _fid=fid, _fname=fname):
            self._show_rename_file_dialog(_fid, _fname)

        def _confirm_delete(_e=None, _fid=fid, _fname=fname):
            pg = self._page()
            if not pg:
                return
            dlg: list = [None]

            def _do(_):
                with contextlib.suppress(Exception):
                    pg.pop_dialog()
                ok = self.db.delete_user_file(_fid)
                if ok:
                    self._files = [
                        x for x in self._files if str(x.get("id") or "") != _fid
                    ]
                    self._rebuild_files_col()
                    self._snack(f'"{_fname}" видалено', ft.Colors.ORANGE)
                else:
                    self._snack(
                        self.t("files.err.delete_file", "Помилка видалення файлу"),
                        ft.Colors.RED,
                    )

            dlg[0] = ft.AlertDialog(
                modal=True,
                title=ft.Text(
                    self.t("files.delete_file_confirm", 'Видалити "{fname}"?').format(
                        fname=_fname
                    ),
                    size=14,
                    weight="bold",
                ),
                actions=[
                    ft.TextButton(
                        self.t("common.cancel", "Скасувати"),
                        on_click=lambda _: pg.pop_dialog(),
                    ),
                    ft.FilledButton(
                        self.t("common.delete", "Видалити"),
                        icon=ft.Icons.DELETE_OUTLINE,
                        on_click=_do,
                        style=ft.ButtonStyle(bgcolor=ft.Colors.RED_600),
                    ),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
            pg.open(dlg[0])

        def _run_open(_):
            pg = self._page()
            if pg:
                pg.run_task(_open)

        def _run_save(_):
            pg = self._page()
            if pg:
                pg.run_task(_save)

        # Explicit width/height keeps each button at 24px (Flet's default
        # IconButton touch target is ~40-48px otherwise) — without it, 5
        # buttons overflowed past the _actions_width() budget and pushed
        # Delete outside the row's visible/clickable area.
        action_btns = [
            ft.IconButton(
                ft.Icons.OPEN_IN_NEW,
                icon_size=16,
                icon_color=_ACCENT,
                tooltip=self.t("files.tooltip.open", "Відкрити"),
                on_click=_run_open,
                style=ft.ButtonStyle(padding=ft.padding.all(4)),
                width=24,
                height=24,
            ),
            ft.IconButton(
                ft.Icons.SAVE_ALT,
                icon_size=16,
                icon_color=_OK_DK,
                tooltip=self.t("files.tooltip.save_pc", "Зберегти на ПК"),
                on_click=_run_save,
                style=ft.ButtonStyle(padding=ft.padding.all(4)),
                width=24,
                height=24,
            ),
        ]
        if not self._readonly:
            action_btns += [
                ft.IconButton(
                    ft.Icons.EDIT_OUTLINED,
                    icon_size=16,
                    icon_color=_MUTED,
                    tooltip=self.t("files.tooltip.rename", "Перейменувати"),
                    on_click=lambda _, _rn=_rename: _rn(),
                    style=ft.ButtonStyle(padding=ft.padding.all(4)),
                    width=24,
                    height=24,
                ),
                ft.IconButton(
                    ft.Icons.DRIVE_FILE_MOVE_OUTLINED,
                    icon_size=16,
                    icon_color=_MUTED,
                    tooltip=self.t("files.tooltip.move", "Перемістити"),
                    on_click=lambda _, _mv=_move: _mv(),
                    style=ft.ButtonStyle(padding=ft.padding.all(4)),
                    width=24,
                    height=24,
                ),
                ft.IconButton(
                    ft.Icons.DELETE_OUTLINE,
                    icon_size=16,
                    icon_color=ft.Colors.RED_400,
                    tooltip=self.t("common.delete", "Видалити"),
                    on_click=lambda _, _cd=_confirm_delete: _cd(),
                    style=ft.ButtonStyle(padding=ft.padding.all(4)),
                    width=24,
                    height=24,
                ),
            ]

        return ft.Container(
            border=ft.border.only(bottom=ft.BorderSide(1, _BORDER)),
            padding=ft.padding.symmetric(horizontal=10, vertical=6),
            content=ft.Row(
                [
                    ft.Icon(icon_name, size=18, color=icon_color),
                    ft.Text(
                        fname,
                        size=12,
                        expand=True,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                        color=_TEXT,
                    ),
                    ft.Text(_fmt_size(fsize), size=11, color=_MUTED, width=72),
                    ft.Text(date_str, size=11, color=_MUTED, width=86),
                    ft.Row(action_btns, spacing=0, width=self._actions_width()),
                ],
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    # ------------------------------------------------------------------ #
    #  Rename file dialog                                                  #
    # ------------------------------------------------------------------ #

    def _show_rename_file_dialog(self, file_id: str, current_name: str):
        pg = self._page()
        if not pg:
            return
        if "." in current_name:
            base, ext = current_name.rsplit(".", 1)
            ext = "." + ext
        else:
            base, ext = current_name, ""
        tf = ft.TextField(
            value=base,
            autofocus=True,
            dense=True,
            text_size=13,
            border_radius=6,
            suffix=ft.Text(ext) if ext else None,
            content_padding=ft.padding.symmetric(horizontal=10, vertical=6),
        )
        dlg: list = [None]

        def _save(_):
            new_base = (tf.value or "").strip()
            new_name = f"{new_base}{ext}"
            if not new_base or new_name == current_name:
                with contextlib.suppress(Exception):
                    pg.pop_dialog()
                return
            with contextlib.suppress(Exception):
                pg.pop_dialog()
            ok = self.db.rename_user_file(file_id, new_name)
            if ok:
                for f in self._files:
                    if str(f.get("id") or "") == file_id:
                        f["original_filename"] = new_name
                        break
                self._rebuild_files_col()
                self._snack(self.t("files.renamed", "Перейменовано"), ft.Colors.GREEN)
            else:
                self._snack(
                    self.t("files.err.rename", "Помилка перейменування"), ft.Colors.RED
                )

        tf.on_submit = _save
        dlg[0] = ft.AlertDialog(
            modal=True,
            title=ft.Text(
                self.t("files.dlg.rename_file", "Перейменувати файл"),
                size=14,
                weight="bold",
            ),
            content=ft.Container(width=320, content=tf),
            actions=[
                ft.TextButton(
                    self.t("common.cancel", "Скасувати"),
                    on_click=lambda _: pg.pop_dialog(),
                ),
                ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        pg.open(dlg[0])

    # ------------------------------------------------------------------ #
    #  Move dialog                                                         #
    # ------------------------------------------------------------------ #

    def _show_move_dialog(self, file_id: str, fname: str):
        pg = self._page()
        if not pg:
            return

        # Build flat folder list with indent to show nesting
        def _flat(folders: list[dict], parent_id: str | None, depth: int) -> list[dict]:
            result = []
            for f in sorted(
                [x for x in folders if (x.get("parent_id") or None) == parent_id],
                key=lambda x: (x.get("name") or "").lower(),
            ):
                result.append({**f, "_depth": depth})
                result.extend(_flat(folders, f["id"], depth + 1))
            return result

        flat = _flat(self._folders, None, 0)
        # exclude current folder
        flat = [f for f in flat if f["id"] != self._sel_folder_id]

        if not flat:
            self._snack(
                self.t(
                    "files.err.no_other_folder", "Немає інших папок для переміщення"
                ),
                _MUTED,
            )
            return

        options = [
            ft.dropdown.Option(
                key=str(f["id"]),
                text=("  " * f["_depth"]) + "📁 " + str(f.get("name") or ""),
            )
            for f in flat
        ]
        dd = ft.Dropdown(
            options=options,
            hint_text=self.t("files.move_select_hint", "Оберіть папку…"),
            dense=True,
            text_size=13,
        )
        dlg: list = [None]

        def _do(_):
            if not dd.value:
                return
            with contextlib.suppress(Exception):
                pg.pop_dialog()
            ok = self.db.move_user_file(file_id, dd.value)
            if ok:
                self._snack(
                    self.t("files.moved", '"{fname}" переміщено').format(fname=fname),
                    ft.Colors.GREEN,
                )
                self._reload_files()
            else:
                self._snack(
                    self.t("files.err.move", "Помилка переміщення"), ft.Colors.RED
                )

        dlg[0] = ft.AlertDialog(
            modal=True,
            title=ft.Text(
                self.t("files.dlg.move", 'Перемістити "{fname}"').format(fname=fname),
                size=14,
                weight="bold",
            ),
            content=ft.Container(width=340, content=dd),
            actions=[
                ft.TextButton(
                    self.t("common.cancel", "Скасувати"),
                    on_click=lambda _: pg.pop_dialog(),
                ),
                ft.FilledButton(self.t("files.move_btn", "Перемістити"), on_click=_do),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        pg.open(dlg[0])

    # ------------------------------------------------------------------ #
    #  Upload                                                              #
    # ------------------------------------------------------------------ #

    async def _pick_and_upload(self):
        if not self._sel_folder_id or self._readonly:
            return
        pg = self._page()
        if not pg:
            return

        fp = ft.FilePicker()
        pg.services.append(fp)
        pg.update()
        await asyncio.sleep(0.2)

        result = await fp.pick_files(
            dialog_title=self.t("files.pick_dialog", "Оберіть файл для завантаження"),
            allow_multiple=False,
        )
        with contextlib.suppress(Exception):
            pg.services.remove(fp)
            pg.update()

        if not result:
            return
        f = result[0]
        path = str(f.path or "").strip()
        fname = str(f.name or os.path.basename(path) or "file").strip()
        if not path:
            return

        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except Exception as ex:
            self._snack(f"Помилка читання файлу: {ex}", ft.Colors.RED)
            return

        if len(data) > _MAX_FILE_SIZE:
            self._snack(
                self.t(
                    "files.err.too_large", "Файл занадто великий (макс. {sz})"
                ).format(sz=_fmt_size(_MAX_FILE_SIZE)),
                ft.Colors.RED,
            )
            return

        import mimetypes

        mime = mimetypes.guess_type(fname)[0] or "application/octet-stream"

        btn = self._upload_btn
        if btn:
            btn.text = self.t("files.uploading", "Завантаження…")
            btn.disabled = True
            with contextlib.suppress(Exception):
                btn.update()

        fid = await asyncio.to_thread(
            self.db.upload_user_file,
            self._sel_folder_id,
            self.company_id,
            self.manager_id,
            self.region_id,
            fname,
            data,
            mime,
        )

        if btn:
            btn.text = self.t("files.upload_btn", "Завантажити файл")
            btn.disabled = False
            with contextlib.suppress(Exception):
                btn.update()

        if fid:
            self._snack(
                self.t("files.uploaded", '"{fname}" завантажено').format(fname=fname),
                ft.Colors.GREEN,
            )
            self._reload_files()
        else:
            self._snack(
                self.t("files.err.upload", "Помилка завантаження файлу"), ft.Colors.RED
            )

    # ------------------------------------------------------------------ #
    #  Help                                                                #
    # ------------------------------------------------------------------ #

    def _show_help(self) -> None:
        page = self._page()
        if not page:
            return
        from views.help_utils import show_modal_help

        show_modal_help(
            page,
            self.t("files.help.title", "Довідка — Файли"),
            [
                self.t(
                    "files.help.desc",
                    "Зберігання файлів, організованих у папки по регіонах.",
                ),
                (self.t("files.help.steps_title", "Як користуватись"), None),
                self.t(
                    "files.help.step1",
                    "1. Ліворуч — дерево папок. Натисніть «Нова папка» щоб створити кореневу папку.",
                ),
                self.t(
                    "files.help.step2",
                    "2. Щоб створити підпапку — виберіть папку і натисніть іконку 📁+ біля неї.",
                ),
                self.t(
                    "files.help.step3",
                    "3. Клік на папку → праворуч з'являться файли. «Завантажити файл» додає файл (макс. 50 МБ).",
                ),
                self.t(
                    "files.help.step4",
                    "4. Для кожного файлу: відкрити, зберегти, перемістити, видалити. Чужі файли — тільки читання.",
                ),
            ],
        )
