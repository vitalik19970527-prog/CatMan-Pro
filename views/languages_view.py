"""
views/languages_view.py
Суперадмінський екран "Мови" — керування перекладами інтерфейсу:
  - таблиця ключ(код) + база (українська) -> значення по кожній активній мові,
    редагування клітинок напряму
  - масова вставка з буфера обміну по стовпцю (одній мові)
  - експорт/імпорт перекладів через Excel
  - додавання нової мови (назва, emoji-прапор, код)
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re

import flet as ft

import i18n
from logger import logger

try:
    from tkinter import Tk, filedialog

    TK_AVAILABLE = True
except Exception:
    TK_AVAILABLE = False


_COMMON_FLAGS = [
    "🇺🇦",
    "🇬🇧",
    "🇺🇸",
    "🇵🇱",
    "🇩🇪",
    "🇫🇷",
    "🇪🇸",
    "🇮🇹",
    "🇵🇹",
    "🇳🇱",
    "🇨🇿",
    "🇸🇰",
    "🇭🇺",
    "🇷🇴",
    "🇧🇬",
    "🇬🇷",
    "🇹🇷",
    "🇷🇺",
    "🇨🇳",
    "🇯🇵",
    "🇰🇷",
    "🇮🇳",
    "🇸🇦",
    "🇮🇱",
    "🇧🇷",
    "🇲🇽",
    "🇨🇦",
    "🇸🇪",
    "🇳🇴",
    "🇫🇮",
    "🇩🇰",
    "🇮🇪",
]

_KEY_COL_W = 280
_UK_COL_W = 300
_LANG_COL_W = 220


def _preferred_user_folder() -> str:
    for cand in (os.path.expanduser("~/Desktop"), os.path.expanduser("~")):
        if os.path.isdir(cand):
            return cand
    return os.getcwd()


def _pick_save_path(title: str, initialfile: str) -> str | None:
    if not TK_AVAILABLE:
        return None
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.asksaveasfilename(
        title=title,
        initialdir=_preferred_user_folder(),
        initialfile=initialfile,
        defaultextension=".xlsx",
        filetypes=[("Excel", "*.xlsx"), ("Усі файли", "*.*")],
    )
    root.destroy()
    return path or None


def _pick_open_path(title: str) -> str | None:
    if not TK_AVAILABLE:
        return None
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title=title,
        initialdir=_preferred_user_folder(),
        filetypes=[("Excel", "*.xlsx"), ("Усі файли", "*.*")],
    )
    root.destroy()
    return path or None


class LanguagesView:
    def __init__(self, content_area, user_data, db):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.manager_id = str(self.user_data.get("id") or "")
        self.t = i18n.bind(self.manager_id)

        self.languages: list[dict] = (
            []
        )  # [{code, name, flag_emoji, sort_order, enabled}]
        self.keys: list[dict] = []  # [{key, uk_text}]
        self.values: dict = {}  # {key: {lang_code: value}}
        self._field_refs: dict = {}  # (key, lang_code) -> ft.TextField
        self._dirty_langs: set = set()
        self._new_flag_pick = "🇺🇦"
        self._filter_text = ""
        self._only_empty = False
        self._page = 0
        self._page_size = 50
        self._body_holder: ft.Container | None = None
        self._page_label: ft.Text | None = None
        self._prev_page_btn: ft.IconButton | None = None
        self._next_page_btn: ft.IconButton | None = None

    # ------------------------------------------------------------ helpers
    def _show_help(self) -> None:
        page = self.content_area.page if self.content_area else None
        if not page:
            return
        from views.help_utils import show_modal_help
        show_modal_help(page, self.t("languages.help.title", "Довідка — Мови"), [
            self.t("languages.help.desc", "Таблиця перекладів інтерфейсу: заповніть переклади для кожної мови."),
            (self.t("languages.help.steps_title", "Як користуватись"), None),
            self.t("languages.help.step1", "1. Базові тексти — українською; заповніть клітинки перекладів для інших мов."),
            self.t("languages.help.step2", "2. Іконка буфера обміну біля назви мови — вставити колонку перекладів з буфера."),
            self.t("languages.help.step3", "3. Можна також вивантажити в Excel, заповнити та завантажити назад."),
            self.t("languages.help.step4", "4. Натисніть «Зберегти» щоб застосувати зміни."),
        ])

    def _show_snack(self, text, color):
        page = self.content_area.page if self.content_area else None
        if not page:
            return
        page.snack_bar = ft.SnackBar(
            ft.Text(text, color=ft.Colors.WHITE, weight="bold"),
            bgcolor=color,
            duration=2500,
        )
        page.snack_bar.open = True
        page.update()

    def _show_saved_dialog(self, message: str = "Збережено"):
        page = self.content_area.page if self.content_area else None
        if not page:
            return
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(self.t("orders.sync.synced", "Збережено")),
            content=ft.Text(str(message or self.t("orders.sync.synced", "Збережено"))),
            actions=[ft.FilledButton("OK", on_click=lambda _e: page.pop_dialog())],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    def _reload_data(self):
        self.languages = self.db.get_translation_languages() or []
        data = self.db.get_all_translations() or {"keys": [], "values": {}}
        self.keys = data.get("keys") or []
        self.values = data.get("values") or {}
        self._dirty_langs = set()
        self._field_refs = {}

    # ------------------------------------------------------------ filter/paging
    def _key_has_empty_translation(self, key_row: dict) -> bool:
        """True if this key is missing (or has whitespace-only) a value for
        at least one editable (non-UA) language."""
        key = str(key_row.get("key") or "")
        vals = self.values.get(key) or {}
        return any(
            not str(vals.get(str(lang.get("code") or "")) or "").strip()
            for lang in self._editable_langs
        )

    def _filtered_keys(self) -> list[dict]:
        keys = self.keys
        if self._only_empty:
            keys = [k for k in keys if self._key_has_empty_translation(k)]
        needle = (self._filter_text or "").strip().lower()
        if not needle:
            return keys
        return [
            k
            for k in keys
            if needle in str(k.get("key") or "").lower()
            or needle in str(k.get("uk_text") or "").lower()
        ]

    def _on_filter_changed(self, e):
        self._filter_text = e.control.value or ""
        self._page = 0
        self._refresh_body()

    def _change_page(self, delta: int):
        self._page += delta
        self._refresh_body()

    def _clear_empty_filter(self, _e=None):
        self._only_empty = False
        self._page = 0
        self.build()

    def _compute_body(self, page_keys: list[dict], total_filtered: int):
        if not self.keys:
            return ft.Container(
                padding=20,
                content=ft.Text(
                    self.t(
                        "languages.empty_state",
                        "Поки немає жодного зареєстрованого напису для перекладу."
                        " Ключі з'являються автоматично, щойно відповідна вкладка"
                        " програми відкривається хоч раз.",
                    ),
                    size=13,
                    color="grey600",
                ),
            )
        if not total_filtered:
            if self._only_empty and not (self._filter_text or "").strip():
                msg = self.t(
                    "languages.no_empty", "Усі переклади заповнені для всіх мов \U0001f389"
                )
            else:
                msg = self.t("languages.no_matches", "Нічого не знайдено за цим запитом.")
            return ft.Container(
                padding=20,
                content=ft.Text(msg, size=13, color="grey600"),
            )
        # ── Sticky table header + scrollable data rows ─────────────────────
        return self._build_table(page_keys)

    def _refresh_body(self):
        """Recompute only the current page of rows and swap it in with a
        targeted update — never rebuild/re-send the whole tree."""
        filtered = self._filtered_keys()
        total = len(filtered)
        page_count = max(1, (total + self._page_size - 1) // self._page_size)
        self._page = max(0, min(self._page, page_count - 1))
        start = self._page * self._page_size
        page_keys = filtered[start : start + self._page_size]

        if self._body_holder is not None:
            self._body_holder.content = self._compute_body(page_keys, total)
            with contextlib.suppress(Exception):
                self._body_holder.update()

        if self._page_label is not None:
            if total:
                self._page_label.value = f"{start + 1}-{min(start + self._page_size, total)} з {total}"
            else:
                self._page_label.value = "0 з 0"
            with contextlib.suppress(Exception):
                self._page_label.update()
        if self._prev_page_btn is not None:
            self._prev_page_btn.disabled = self._page <= 0
            with contextlib.suppress(Exception):
                self._prev_page_btn.update()
        if self._next_page_btn is not None:
            self._next_page_btn.disabled = self._page >= page_count - 1
            with contextlib.suppress(Exception):
                self._next_page_btn.update()

    # ------------------------------------------------------------ build
    def build(self, only_empty: bool = False):
        """Loading `_reload_data()` (2 full-table selects, potentially many
        thousands of rows once translations pile up) directly on the UI
        thread used to freeze the whole app on tab open. Show a spinner
        immediately and fetch off-thread instead — same pattern already
        used elsewhere in this codebase (see gsm_parser_view.py)."""
        self._filter_text = ""
        self._only_empty = only_empty
        self._page = 0
        self.content_area.controls.clear()
        with contextlib.suppress(Exception):
            self.content_area.scroll = None   # outer must NOT scroll — only inner data rows scroll

        page = self.content_area.page if self.content_area else None
        self.content_area.controls.append(
            ft.Container(
                padding=60,
                alignment=ft.Alignment(0, 0),
                content=ft.Column(
                    [
                        ft.ProgressRing(),
                        ft.Text(
                            self.t("common.loading", "Завантаження…"),
                            size=13,
                            color="grey600",
                        ),
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=12,
                    tight=True,
                ),
            )
        )
        with contextlib.suppress(Exception):
            if page:
                self.content_area.update()

        if not page:
            self._reload_data()
            self._build_loaded()
            return

        async def _load(_only_empty=only_empty):
            await asyncio.to_thread(self._reload_data)
            with contextlib.suppress(Exception):
                self._build_loaded()

        page.run_task(_load)

    def _build_loaded(self) -> None:
        self.content_area.controls.clear()

        # ── Fixed page header: title + buttons + subtitle ─────────────────────
        toolbar = ft.Row(
            [
                ft.Text(self.t("nav.languages", "Мови"), size=22, weight="bold"),
                ft.IconButton(
                    icon=ft.Icons.HELP_OUTLINE,
                    icon_size=18,
                    tooltip=self.t("common.help.tooltip", "Довідка"),
                    on_click=lambda _e: self._show_help(),
                ),
                ft.Row(expand=True),
                ft.OutlinedButton(
                    self.t("languages.add_btn", "Додати мову"),
                    icon=ft.Icons.ADD,
                    on_click=self._open_add_language_dialog,
                ),
                ft.OutlinedButton(
                    self.t("languages.export_excel", "Завантажити Excel"),
                    icon=ft.Icons.DOWNLOAD,
                    on_click=self._export_excel,
                ),
                ft.OutlinedButton(
                    self.t("languages.import_excel", "Завантажити з Excel"),
                    icon=ft.Icons.UPLOAD,
                    on_click=self._import_excel,
                ),
                ft.FilledButton(
                    self.t("common.save", "Зберегти"),
                    icon=ft.Icons.SAVE,
                    on_click=self._save_all,
                ),
            ],
            spacing=8,
        )
        subtitle = ft.Text(
            "Базові написи беруться з коду (українською). Заповніть переклад для"
            " кожної мови — клітинки редагуються напряму. Біля назви мови є кнопка"
            " вставки з буфера обміну (вставляє рядок за рядком на весь стовпець)."
            " Можна також вивантажити таблицю в Excel, заповнити і завантажити назад.",
            size=12,
            color="grey700",
        )

        self._page_label = ft.Text("", size=12, color="grey700")
        self._prev_page_btn = ft.IconButton(
            icon=ft.Icons.CHEVRON_LEFT,
            icon_size=18,
            tooltip=self.t("common.prev_page", "Попередня сторінка"),
            on_click=lambda _e: self._change_page(-1),
        )
        self._next_page_btn = ft.IconButton(
            icon=ft.Icons.CHEVRON_RIGHT,
            icon_size=18,
            tooltip=self.t("common.next_page", "Наступна сторінка"),
            on_click=lambda _e: self._change_page(1),
        )
        filter_row = ft.Row(
            [
                ft.TextField(
                    hint_text=self.t("languages.search_hint", "Пошук за ключем або українським текстом"),
                    prefix_icon=ft.Icons.SEARCH,
                    dense=True,
                    border_radius=8,
                    width=360,
                    content_padding=ft.padding.symmetric(horizontal=10, vertical=8),
                    on_change=self._on_filter_changed,
                ),
                ft.Row(expand=True),
                self._prev_page_btn,
                self._page_label,
                self._next_page_btn,
            ],
            spacing=8,
        )

        # Fixed top: toolbar + (optional empty-filter banner) + filter/pager + subtitle never scroll
        fixed_top_children = [toolbar]
        if self._only_empty:
            fixed_top_children.append(
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=10, vertical=6),
                    bgcolor="#FEF2F2",
                    border_radius=8,
                    border=ft.border.all(1, "#FCA5A5"),
                    content=ft.Row(
                        [
                            ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, size=16, color="#DC2626"),
                            ft.Text(
                                self.t(
                                    "languages.empty_filter_active",
                                    "Показані лише написи з порожнім перекладом хоча б однією мовою",
                                ),
                                size=12,
                                color="#991B1B",
                            ),
                            ft.Row(expand=True),
                            ft.TextButton(
                                self.t("languages.clear_filter", "Показати всі"),
                                on_click=self._clear_empty_filter,
                            ),
                        ],
                        spacing=8,
                    ),
                )
            )
        fixed_top_children.extend([filter_row, subtitle])
        fixed_top = ft.Column(fixed_top_children, spacing=4, tight=True)

        filtered = self._filtered_keys()
        total = len(filtered)
        page_count = max(1, (total + self._page_size - 1) // self._page_size)
        self._page = max(0, min(self._page, page_count - 1))
        start = self._page * self._page_size
        page_keys = filtered[start : start + self._page_size]
        if total:
            self._page_label.value = f"{start + 1}-{min(start + self._page_size, total)} з {total}"
        else:
            self._page_label.value = "0 з 0"
        self._prev_page_btn.disabled = self._page <= 0
        self._next_page_btn.disabled = self._page >= page_count - 1

        self._body_holder = ft.Container(expand=True, content=self._compute_body(page_keys, total))

        # Single root: fixed_top pinned, body fills remaining height with its own scroll
        root = ft.Column(
            [fixed_top, self._body_holder],
            spacing=0,
            expand=True,
        )
        self.content_area.controls.append(root)

        with contextlib.suppress(Exception):
            if self.content_area.page:
                self.content_area.update()

    @property
    def _editable_langs(self) -> list[dict]:
        """All languages except UA — UA is always the base shown in the first column."""
        return [
            lang
            for lang in self.languages
            if str(lang.get("code") or "").upper() != "UA"
        ]

    # ------------------------------------------------------------ table
    def _build_table(self, page_keys: list[dict]) -> ft.Container:
        """
        Layout:
          ft.Row (h-scroll) ← shared horizontal scroll for header AND data
            ft.Column (expand=True)
              ├── column_header_row (fixed, not vertically scrollable)
              └── data_rows_col (expand=True, scroll=AUTO) ← only this scrolls vertically
        Scroll ← / → buttons float over the whole block.

        `page_keys` is only the current page slice — rendering the full key
        list at once (potentially thousands of TextField cells) is what used
        to freeze the tab on open.
        """
        # ── Build column header cells (NO data rows here) ─────────────────────
        col_hdr = self._build_header_row()

        # ── Build data rows in a vertically-scrollable column ─────────────────
        data_rows = [self._build_data_row(k) for k in page_keys]
        data_col = ft.Column(
            data_rows,
            spacing=0,
            tight=True,
            scroll=ft.ScrollMode.AUTO,   # vertical scroll lives here
            expand=True,                  # fills remaining vertical space
        )

        # ── Inner column: header (pinned) + scrollable data ───────────────────
        inner_col = ft.Column(
            [col_hdr, data_col],
            spacing=0,
            expand=True,
        )

        # ── Shared horizontal scroll ───────────────────────────────────────────
        h_scroll_ref = ft.Ref[ft.Row]()

        def _h_scroll(delta: int) -> None:
            async def _do(_d: int = delta) -> None:
                with contextlib.suppress(Exception):
                    if h_scroll_ref.current:
                        await h_scroll_ref.current.scroll_to(delta=_d, duration=180)
            page = self.content_area.page if self.content_area else None
            if page:
                with contextlib.suppress(Exception):
                    page.run_task(_do)

        table_h_row = ft.Row(
            ref=h_scroll_ref,
            controls=[inner_col],
            scroll=ft.ScrollMode.HIDDEN,
            expand=True,
        )

        # ── Scroll ← / → overlay buttons ──────────────────────────────────────
        _shadow = ft.BoxShadow(
            spread_radius=1, blur_radius=6,
            color="#9CA3AF", offset=ft.Offset(0, 2),
        )
        _btn_style = ft.ButtonStyle(padding=ft.padding.all(4))

        scroll_left_btn = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_LEFT, icon_size=20, icon_color="#1D4ED8",
                on_click=lambda _: _h_scroll(-400), style=_btn_style,
            ),
            left=4, top=16,
            bgcolor="#FFFFFF", border_radius=16,
            width=36, height=36, shadow=_shadow, opacity=0.90,
        )
        scroll_right_btn = ft.Container(
            content=ft.IconButton(
                ft.Icons.CHEVRON_RIGHT, icon_size=20, icon_color="#1D4ED8",
                on_click=lambda _: _h_scroll(400), style=_btn_style,
            ),
            right=4, top=16,
            bgcolor="#FFFFFF", border_radius=16,
            width=36, height=36, shadow=_shadow, opacity=0.90,
        )

        return ft.Container(
            expand=True,       # fill remaining content_area space so outer scroll doesn't activate
            content=ft.Stack(
                [table_h_row, scroll_left_btn, scroll_right_btn],
                expand=True,
            ),
        )

    def _build_header_row(self) -> ft.Container:
        cells = [
            ft.Container(
                width=_KEY_COL_W,
                padding=8,
                content=ft.Text(
                    self.t("languages.col_key", "Ключ"),
                    size=12,
                    weight=ft.FontWeight.W_600,
                ),
            ),
            ft.Container(
                width=_UK_COL_W,
                padding=8,
                content=ft.Text(
                    self.t("languages.col_uk_base", "Українська (база)"),
                    size=12,
                    weight=ft.FontWeight.W_600,
                ),
            ),
        ]
        for lang in self._editable_langs:
            code = str(lang.get("code") or "")
            cells.append(
                ft.Container(
                    width=_LANG_COL_W,
                    padding=8,
                    content=ft.Row(
                        [
                            ft.Text(
                                f"{lang.get('name') or code} ({code})",
                                size=12,
                                weight=ft.FontWeight.W_600,
                                no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                                expand=True,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.CONTENT_PASTE,
                                icon_size=16,
                                tooltip=f"Вставити з буфера для {code}",
                                on_click=lambda _e, c=code: self._open_paste_dialog(c),
                            ),
                        ],
                        spacing=2,
                        tight=True,
                    ),
                )
            )
        return ft.Container(
            content=ft.Row(cells, spacing=0),
            bgcolor="#F1F5F9",
            border=ft.border.only(bottom=ft.border.BorderSide(1, "#CBD5E1")),
        )

    def _build_data_row(self, key_row: dict) -> ft.Container:
        key = str(key_row.get("key") or "")
        uk_text = str(key_row.get("uk_text") or "")
        cells = [
            ft.Container(
                width=_KEY_COL_W,
                padding=8,
                content=ft.Text(
                    key,
                    size=11,
                    color="grey600",
                    no_wrap=True,
                    overflow=ft.TextOverflow.ELLIPSIS,
                    tooltip=key,
                ),
            ),
            ft.Container(
                width=_UK_COL_W,
                padding=4,
                content=ft.TextField(
                    value=uk_text,
                    dense=True,
                    border_radius=6,
                    content_padding=ft.padding.symmetric(horizontal=8, vertical=6),
                    text_size=12,
                    width=_UK_COL_W - 8,
                    tooltip=uk_text,
                    on_change=lambda e, k=key: self._on_cell_changed(k, "UA", e.control.value),
                ),
            ),
        ]
        for lang in self._editable_langs:
            code = str(lang.get("code") or "")
            current = str((self.values.get(key) or {}).get(code) or "")
            is_empty = not current.strip()
            tf = ft.TextField(
                value=current,
                dense=True,
                border_radius=6,
                border_color="#DC2626" if is_empty else None,
                bgcolor="#FEF2F2" if is_empty else None,
                content_padding=ft.padding.symmetric(horizontal=8, vertical=6),
                text_size=12,
                width=_LANG_COL_W - 8,
                on_change=lambda e, k=key, c=code: self._on_cell_changed(
                    k, c, e.control.value, e.control
                ),
            )
            self._field_refs[(key, code)] = tf
            cells.append(ft.Container(width=_LANG_COL_W, padding=4, content=tf))
        return ft.Container(
            content=ft.Row(cells, spacing=0),
            border=ft.border.only(bottom=ft.border.BorderSide(1, "#E2E8F0")),
        )

    def _on_cell_changed(self, key: str, lang_code: str, value: str, control=None):
        self.values.setdefault(key, {})[lang_code] = value
        self._dirty_langs.add(lang_code)
        if control is not None:
            is_empty = not str(value or "").strip()
            control.border_color = "#DC2626" if is_empty else None
            control.bgcolor = "#FEF2F2" if is_empty else None
            with contextlib.suppress(Exception):
                control.update()

    # ------------------------------------------------------------ paste
    def _open_paste_dialog(self, lang_code: str):
        page = self.content_area.page if self.content_area else None
        if not page:
            return
        info = ft.Text(
            f"Вставте {len(self.keys)} рядків (по одному на ключ, у тому ж порядку,"
            " що й у таблиці) — кожен рядок піде у відповідну клітинку зверху вниз.",
            size=12,
            color="grey700",
        )
        paste_field = ft.TextField(
            multiline=True,
            min_lines=10,
            max_lines=16,
            autofocus=True,
            hint_text="Встав сюди (Ctrl+V) скопійований з Excel/іншого джерела стовпець…",
        )

        def _apply(_e):
            lines = (paste_field.value or "").splitlines()
            applied = 0
            for i, k_row in enumerate(self.keys):
                if i >= len(lines):
                    break
                key = str(k_row.get("key") or "")
                val = lines[i].rstrip("\r")
                tf = self._field_refs.get((key, lang_code))
                if tf is not None:
                    tf.value = val
                self._on_cell_changed(key, lang_code, val, tf)
                applied += 1
            page.pop_dialog()
            self._show_snack(f"Вставлено {applied} значень для {lang_code}", "green")

        def _paste_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, self.t("languages.paste_dialog_title", "Вставка з буфера — {code}").format(code=lang_code), [
                self.t("languages.paste_help.step1", "1. Скопіюйте колонку перекладів з Excel або іншого джерела (по одному значенню на рядок)."),
                self.t("languages.paste_help.step2", "2. Вставте в поле тексту та натисніть «Застосувати» — значення заповнюються по порядку рядків."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Text(
                    self.t(
                        "languages.paste_dialog_title", "Вставка з буфера — {code}"
                    ).format(code=lang_code),
                    expand=True,
                ),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_paste_help),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
            content=ft.Container(
                width=520,
                content=ft.Column([info, paste_field], spacing=10, tight=True),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _e: page.pop_dialog()),
                ft.FilledButton("Застосувати", on_click=_apply),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    # ------------------------------------------------------------ save
    def _save_all(self, _e=None):
        if not self._dirty_langs:
            self._show_snack(
                self.t("languages.no_unsaved_changes", "Немає незбережених змін"),
                "blue",
            )
            return
        ok_all = True
        for lang_code in list(self._dirty_langs):
            if lang_code.upper() == "UA":
                uk_values = {
                    key: (self.values.get(key) or {}).get("UA", "")
                    for key in (str(k.get("key") or "") for k in self.keys)
                }
                uk_values = {k: v for k, v in uk_values.items() if k}
                if uk_values:
                    ok = self.db.save_translation_keys_uk_bulk(uk_values)
                    ok_all = ok_all and bool(ok)
                continue
            values_for_lang = {
                key: (self.values.get(key) or {}).get(lang_code, "")
                for key in (str(k.get("key") or "") for k in self.keys)
            }
            values_for_lang = {k: v for k, v in values_for_lang.items() if k}
            if values_for_lang:
                ok = self.db.save_translation_values_bulk(lang_code, values_for_lang)
                ok_all = ok_all and bool(ok)
            i18n.invalidate_translations_cache(lang_code)
        if ok_all:
            self._dirty_langs = set()
            self._show_saved_dialog(
                self.t("languages.translations_saved", "Переклади збережено.")
            )
        else:
            self._show_snack(
                self.t(
                    "languages.err.save_some_failed",
                    "Не вдалося зберегти деякі переклади",
                ),
                "red",
            )

    # ------------------------------------------------------------ add language
    def _open_add_language_dialog(self, _e=None):
        page = self.content_area.page if self.content_area else None
        if not page:
            return
        self._new_flag_pick = "🇺🇦"
        name_f = ft.TextField(
            label="Назва мови", hint_text="напр. Французька", border_radius=8
        )
        code_f = ft.TextField(
            label="Код (2-4 літери)",
            hint_text="напр. FR",
            border_radius=8,
            max_length=4,
        )
        selected_text = ft.Text(
            f"Обрано: {self._new_flag_pick}", size=12, color="grey700"
        )

        flag_buttons = []
        for flag in _COMMON_FLAGS:

            def _pick(_e, f=flag):
                self._new_flag_pick = f
                selected_text.value = f"Обрано: {f}"
                with contextlib.suppress(Exception):
                    selected_text.update()

            flag_buttons.append(
                ft.Container(
                    content=ft.Text(flag, size=18),
                    padding=6,
                    border_radius=6,
                    ink=True,
                    on_click=_pick,
                )
            )
        flag_grid = ft.Row(flag_buttons, wrap=True, spacing=2, run_spacing=2)

        existing_codes = {
            str(lang.get("code") or "").upper() for lang in self.languages
        }

        def _submit(_e):
            name = (name_f.value or "").strip()
            code = (code_f.value or "").strip().upper()
            if not name or not code:
                name_f.error_text = None if name else "Обов'язково"
                code_f.error_text = None if code else "Обов'язково"
                with contextlib.suppress(Exception):
                    name_f.update()
                    code_f.update()
                return
            if code in existing_codes:
                code_f.error_text = "Така мова вже є"
                with contextlib.suppress(Exception):
                    code_f.update()
                return
            new_rows = list(self.languages) + [
                {
                    "code": code,
                    "name": name,
                    "flag_emoji": self._new_flag_pick,
                    "sort_order": len(self.languages),
                    "enabled": True,
                }
            ]
            ok = self.db.save_translation_languages_bulk(new_rows)
            if not ok:
                self._show_snack("Не вдалося додати мову", "red")
                return
            i18n.invalidate_languages_cache()
            page.pop_dialog()
            self._show_snack(f"Мову {name} ({code}) додано", "green")
            self.build()

        def _add_lang_help(_):
            from views.help_utils import show_modal_help
            show_modal_help(page, self.t("languages.add_btn", "Додати мову"), [
                self.t("languages.add_lang_help.step1", "1. Введіть назву мови та її код (наприклад FR, ES)."),
                self.t("languages.add_lang_help.step2", "2. Оберіть емодзі прапора та натисніть «Додати»."),
            ])

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Row([
                ft.Text(self.t("languages.add_btn", "Додати мову"), expand=True),
                ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_add_lang_help),
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
            content=ft.Container(
                width=420,
                content=ft.Column(
                    [
                        name_f,
                        code_f,
                        ft.Text("Прапор:", size=12),
                        flag_grid,
                        selected_text,
                    ],
                    spacing=10,
                    tight=True,
                ),
            ),
            actions=[
                ft.TextButton("Скасувати", on_click=lambda _e: page.pop_dialog()),
                ft.FilledButton("Додати", on_click=_submit),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    # ------------------------------------------------------------ excel export
    def _export_excel(self, _e=None):
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill
        except ImportError as exc:
            self._show_snack(f"openpyxl не встановлено: {exc}", "red")
            return
        path = _pick_save_path("Зберегти переклади Excel", "CatMan_translations.xlsx")
        if not path:
            return
        try:
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Переклади"
            hdr_font = Font(bold=True, color="FFFFFF")
            hdr_fill = PatternFill("solid", fgColor="2D6A9F")
            headers = ["Ключ", "Українська (база)"] + [
                f"{lang.get('name') or lang.get('code')} [{lang.get('code')}]"
                for lang in self._editable_langs
            ]
            for c, h in enumerate(headers, start=1):
                cell = ws.cell(row=1, column=c, value=h)
                cell.font = hdr_font
                cell.fill = hdr_fill
            for r, k_row in enumerate(self.keys, start=2):
                key = str(k_row.get("key") or "")
                ws.cell(row=r, column=1, value=key)
                ws.cell(row=r, column=2, value=str(k_row.get("uk_text") or ""))
                for c, lang in enumerate(self._editable_langs, start=3):
                    code = str(lang.get("code") or "")
                    ws.cell(
                        row=r,
                        column=c,
                        value=str((self.values.get(key) or {}).get(code) or ""),
                    )
            ws.column_dimensions["A"].width = 38
            ws.column_dimensions["B"].width = 40
            for i in range(len(self._editable_langs)):
                from openpyxl.utils import get_column_letter

                ws.column_dimensions[get_column_letter(3 + i)].width = 32
            wb.save(path)
            self._show_snack(f"Збережено: {path}", "green")
        except Exception as exc:
            logger.exception("languages export failed")
            self._show_snack(f"Не вдалося зберегти файл: {exc}", "red")

    # ------------------------------------------------------------ excel import
    def _import_excel(self, _e=None):
        try:
            import openpyxl
        except ImportError as exc:
            self._show_snack(f"openpyxl не встановлено: {exc}", "red")
            return
        path = _pick_open_path("Оберіть файл перекладів Excel")
        if not path or not os.path.isfile(path):
            return
        try:
            wb = openpyxl.load_workbook(path, data_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                self._show_snack("Файл порожній", "orange")
                return
            header = [str(c or "") for c in rows[0]]
            known_codes = {
                str(lang.get("code") or "").upper() for lang in self._editable_langs
            }
            col_lang: dict[int, str] = {}
            for idx, h in enumerate(header):
                m = re.search(r"\[([A-Za-z]{2,4})\]\s*$", h)
                code = m.group(1).upper() if m else h.strip().upper()
                if code in known_codes:
                    col_lang[idx] = code
            if not col_lang:
                self._show_snack(
                    "Не знайдено жодного відомого коду мови в заголовках файлу",
                    "orange",
                )
                return
            per_lang_values: dict[str, dict[str, str]] = {
                c: {} for c in col_lang.values()
            }
            updated_rows = 0
            for row in rows[1:]:
                if not row:
                    continue
                key = str(row[0] or "").strip()
                if not key:
                    continue
                for idx, code in col_lang.items():
                    if idx < len(row) and row[idx] not in (None, ""):
                        per_lang_values[code][key] = str(row[idx])
                updated_rows += 1
            saved_langs = 0
            for code, values in per_lang_values.items():
                if values:
                    self.db.save_translation_values_bulk(code, values)
                    i18n.invalidate_translations_cache(code)
                    saved_langs += 1
            self._show_snack(
                f"Імпортовано {updated_rows} рядків для {saved_langs} мов", "green"
            )
            self.build()
        except Exception as exc:
            logger.exception("languages import failed")
            self._show_snack(f"Не вдалося прочитати файл: {exc}", "red")
