"""
views/web_site_view.py
Суперадмінська вкладка "Web сайт" — редагування маркетингового сайту
ПРЯМО в репозиторії на GitHub (Contents API, без local git і без залежності
від локальної папки "Web Сайт" — та існує лише в основному репозиторії
розробника і не входить у зібраний CatManSupAdm.exe/.apk). Кожне читання й
кожен запис іде через GitHubPublisher (get_file/put_file/delete_file/list_dir),
результат кешується в self._file_cache на час життя WebSiteView.

Секції: Тексти (структурований редактор js/i18n.js + синк data-i18n у HTML),
Зображення (менеджер img/, мініатюри вантажаться напряму з
raw.githubusercontent.com), Файли сайту (сирий редактор — запасний варіант
для всього іншого), Білд і публікація (заливання exe/apk у GitHub Releases —
це єдина частина, що лишається локальною: збірки існують тільки на машині
розробника після реального білда), Налаштування (токен/repo).
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import html
import json
import os
import re

import flet as ft

import i18n
from github_publisher import GitHubPublisher, load_publish_config, save_publish_config
from logger import logger

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMG_REPO_DIR = "img"

SITE_HTML_FILES = [
    "index.html",
    "pages/catalog.html",
    "pages/company-individual.html",
    "pages/gsm-extension.html",
    "pages/orders-money.html",
    "pages/personal-tools.html",
    "pages/pricing.html",
    "pages/stock-planning.html",
]
SITE_ALL_FILES = SITE_HTML_FILES + [
    "css/style.css",
    "js/i18n.js",
    "js/main.js",
    "js/pricing-calc.js",
]

LANGS = ["uk", "en", "pl", "zh", "de"]
LANG_LABELS = {"uk": "UA", "en": "EN", "pl": "PL", "zh": "中文", "de": "DE"}

I18N_JS_REL = "js/i18n.js"
PRICE_HTML_FILE = "pages/pricing.html"

PAGE_TITLES = {
    "index.html": "Головна",
    "pages/catalog.html": "Каталог товарів",
    "pages/company-individual.html": "Компанія чи ви особисто",
    "pages/gsm-extension.html": "GSM Parser — розширення",
    "pages/orders-money.html": "Замовлення і гроші",
    "pages/personal-tools.html": "Особисті інструменти",
    "pages/pricing.html": "Тарифи",
    "pages/stock-planning.html": "Склад і план закупівель",
}

_SHOT_TAG_RE = re.compile(r'data-shot="([^"]+)"')
# Every page's screenshot groups sit right after their own intro paragraph —
# class="tab-hook" (catalog/orders-money/personal-tools/stock-planning),
# class="lede" (index.html hero) or class="section-lede" (gsm-extension.html).
# Matching on the "hook"/"lede" substring instead of one fixed class name
# covers all of them without hardcoding every page's markup.
_DESC_RE = re.compile(r'<p\s+class="[^"]*(?:hook|lede)[^"]*"[^>]*>(.*?)</p>', re.DOTALL)


def _strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def _shorten(text: str, limit: int = 140) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


# ==================================================================
# Читання сайту напряму з GitHub (Contents API) — жодної залежності від
# локальної папки "Web Сайт", яка існує лише в основному репозиторії
# розробника й не входить у зібраний CatManSupAdm.exe/.apk. Запис так само
# йде напряму на GitHub через GitHubPublisher.put_file — див. WebSiteView
# нижче (self._read_site_file/self._push_site_file), ці дві функції лишились
# лише як приватні helper'и без стану.
# ==================================================================


# ==================================================================
# js/i18n.js — парсинг/точкове редагування "const translations = {...}"
# ==================================================================

_LANG_BLOCK_START_RE = re.compile(r"^(\w+):\s*\{\s*$")
_LANG_BLOCK_END_RE = re.compile(r"^\}[,;]?\s*$")
_ENTRY_RE = re.compile(r'^\s*"((?:[^"\\]|\\.)+)":\s*"((?:[^"\\]|\\.)*)",?\s*$')


def _decode_js_string(raw: str) -> str:
    try:
        return json.loads('"' + raw + '"')
    except Exception:
        return raw


def _encode_js_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _find_lang_blocks(lines: list[str]) -> dict[str, tuple[int, int]]:
    """{lang: (start_line_idx, end_line_idx)} — start is the 'lang: {' line,
    end is the matching bare '}'/'},'/'};' line. Entries inside a block are
    flat key:value pairs (no nesting), so a single-level scan is enough."""
    blocks: dict[str, tuple[int, int]] = {}
    i, n = 0, len(lines)
    while i < n:
        m = _LANG_BLOCK_START_RE.match(lines[i])
        if m:
            lang = m.group(1)
            j = i + 1
            while j < n and not _LANG_BLOCK_END_RE.match(lines[j]):
                j += 1
            blocks[lang] = (i, j)
            i = j + 1
        else:
            i += 1
    return blocks


def _find_entries(lines: list[str], start: int, end: int) -> dict[str, tuple[int, str]]:
    """{key: (line_idx, decoded_value)} for entries strictly between start and end."""
    entries: dict[str, tuple[int, str]] = {}
    for idx in range(start + 1, end):
        m = _ENTRY_RE.match(lines[idx])
        if m:
            entries[m.group(1)] = (idx, _decode_js_string(m.group(2)))
    return entries


def load_i18n_index(text: str) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Returns (ordered_keys, {key: {lang: value}}), ordered off the 'uk'
    block (the source/base language — every key is expected to exist there)."""
    lines = text.split("\n")
    blocks = _find_lang_blocks(lines)
    per_lang = {lang: _find_entries(lines, s, e) for lang, (s, e) in blocks.items()}
    uk_entries = per_lang.get("uk", {})
    ordered_keys = list(uk_entries.keys())
    values: dict[str, dict[str, str]] = {}
    for key in ordered_keys:
        values[key] = {
            lang: (entries[key][1] if key in entries else "")
            for lang, entries in per_lang.items()
        }
    return ordered_keys, values


def set_i18n_entry(text: str, lang: str, key: str, value: str) -> tuple[str, bool]:
    """Updates (or inserts) `key` inside the `lang` block of i18n.js source
    text, touching only that one line — the rest of the file is untouched.
    Returns (new_text, changed)."""
    lines = text.split("\n")
    blocks = _find_lang_blocks(lines)
    if lang not in blocks:
        return text, False
    start, end = blocks[lang]
    entries = _find_entries(lines, start, end)
    new_line = f'  "{key}": {_encode_js_string(value)},'
    if key in entries:
        idx, old_value = entries[key]
        if old_value == value:
            return text, False
        lines[idx] = new_line
    else:
        insert_at = end
        if insert_at - 1 > start:
            prev = lines[insert_at - 1].rstrip()
            if prev and not prev.endswith(",") and not prev.endswith("{"):
                lines[insert_at - 1] = prev + ","
        lines.insert(insert_at, new_line)
    return "\n".join(lines), True


# ==================================================================
# pages/pricing.html — generic price-token scanner/editor. One regex finds
# every "NNN&nbsp;₴[<span class="price-usd">...$MM...</span>]" occurrence
# on the page (price tables, GSM card, one-time services list, bundle
# cards) instead of one hand-written pattern per markup shape. Each token's
# label is the nearest preceding HTML text node that isn't itself a price
# ("₴" or a bare number) — for the two 4-column tables this naturally
# reuses the row's own label cell for every column, since the price cells
# in between get filtered out as label candidates.
# ==================================================================

_PRICE_TOKEN_RE = re.compile(
    r'(?P<uah>\d+(?:[.,]\d+)?)&nbsp;₴'
    r'(?:\s*<span class="price-usd">(?P<usd_inner>[^<]*)</span>)?'
)
_USD_NUM_RE = re.compile(r'\$(\d+(?:[.,]\d+)?)')
_TEXT_NODE_RE = re.compile(r'>([^<>]+)<')
_PRICE_COLUMN_LABELS = ["1 людина", "5 людей", "10 людей", "15 людей"]
# UI chrome that sits right before some price occurrences but is never a
# useful label on its own (a mandatory-tab badge, a repeated CTA button,
# a bare "/міс" suffix, ...) — skipped so the nearest REAL label (row name,
# card title, sub-row heading) is used instead.
_LABEL_EXCLUDE = {"обов'язково", "одноразово", "написати менеджеру", "/міс", "міс"}


def parse_pricing_tokens(text: str) -> list[dict]:
    """Returns every editable price on the page, in document order, each as
    {start, end, label, uah, has_usd, usd_value, usd_prefix, usd_suffix}.
    start/end are exact offsets into `text` for the whole "NNN&nbsp;₴[...]"
    match — apply_price_tokens() splices replacements at those offsets."""
    text_nodes: list[tuple[int, str]] = []
    for m in _TEXT_NODE_RE.finditer(text):
        content = m.group(1).strip()
        if not content or "₴" in content or "$" in content:
            continue
        if re.fullmatch(r"\d+(?:[.,]\d+)?", content):
            continue
        if content.lower() in _LABEL_EXCLUDE:
            continue
        text_nodes.append((m.start(1), content))

    tokens: list[dict] = []
    last_label = ""
    repeat_count = 0
    ti = 0
    for m in _PRICE_TOKEN_RE.finditer(text):
        label = last_label
        while ti < len(text_nodes) and text_nodes[ti][0] < m.start():
            label = text_nodes[ti][1]
            ti += 1

        if label == last_label:
            repeat_count += 1
        else:
            repeat_count = 0
            last_label = label

        short_label = label[:44]
        if repeat_count:
            suffix = (
                _PRICE_COLUMN_LABELS[repeat_count]
                if repeat_count < len(_PRICE_COLUMN_LABELS)
                else f"#{repeat_count + 1}"
            )
            display_label = f"{short_label} · {suffix}"
        else:
            display_label = short_label or "(без назви)"

        usd_inner = m.group("usd_inner")
        usd_value, usd_prefix, usd_suffix = "", "", ""
        if usd_inner is not None:
            um = _USD_NUM_RE.search(usd_inner)
            if um:
                usd_value = um.group(1)
                usd_prefix = usd_inner[: um.start()]
                usd_suffix = usd_inner[um.end():]
            else:
                usd_prefix = usd_inner

        tokens.append(
            {
                "start": m.start(),
                "end": m.end(),
                "label": display_label,
                "group_key": last_label,
                "col_index": repeat_count,
                "uah": m.group("uah"),
                "has_usd": usd_inner is not None,
                "usd_value": usd_value,
                "usd_prefix": usd_prefix,
                "usd_suffix": usd_suffix,
            }
        )
    return tokens


def group_pricing_tokens(tokens: list[dict]) -> tuple[list[dict], list[dict]]:
    """Splits tokens into (grouped_rows, flat_tokens):
    grouped_rows are runs of exactly 4 consecutive tokens sharing the same
    group_key at col_index 0,1,2,3 — the "1/5/10/15 people" price tables —
    collapsed into one {"label", "cols": [tok0..tok3]} row each. Everything
    else (GSM, one-time services, bundle cards) stays as single-column
    flat_tokens, since it has no such fixed 4-wide structure."""
    grouped: list[dict] = []
    flat: list[dict] = []
    i, n = 0, len(tokens)
    while i < n:
        if (
            i + 3 < n
            and tokens[i]["col_index"] == 0
            and all(
                tokens[i + k]["group_key"] == tokens[i]["group_key"]
                and tokens[i + k]["col_index"] == k
                for k in range(1, 4)
            )
        ):
            grouped.append({"label": tokens[i]["group_key"], "cols": tokens[i : i + 4]})
            i += 4
        else:
            flat.append(tokens[i])
            i += 1
    return grouped, flat


def apply_price_token_edits(text: str, edits: list[dict]) -> str:
    """Batch version of apply_price_token_edit — each item in `edits` is a
    token dict merged with its edit fields (discount/new_uah/new_usd_value,
    plus old_uah/old_usd_value when discount=True). Spliced back-to-front by
    position so earlier offsets stay valid while later ones get replaced."""
    for edit in sorted(edits, key=lambda e: e["start"], reverse=True):
        text = apply_price_token_edit(text, edit, edit)
    return text


def _render_price_html(uah: str, has_usd: bool, usd_prefix: str, usd_value: str, usd_suffix: str) -> str:
    out = f"{uah}&nbsp;₴"
    if has_usd:
        out += f' <span class="price-usd">{usd_prefix}{usd_value}{usd_suffix}</span>'
    return out


def apply_price_token_edit(text: str, token: dict, edit: dict) -> str:
    """Splices a single token's replacement into `text` at token['start':'end'].
    edit either sets a plain new price ({"discount": False, "new_uah",
    "new_usd_value"}) or a discount pair — old price struck through, new
    price in red right after it ({"discount": True, "old_uah",
    "old_usd_value", "new_uah", "new_usd_value"})."""
    has_usd = token["has_usd"]
    usd_prefix, usd_suffix = token["usd_prefix"], token["usd_suffix"]
    if edit.get("discount"):
        old_html = _render_price_html(edit["old_uah"], has_usd, usd_prefix, edit["old_usd_value"], usd_suffix)
        new_html = _render_price_html(edit["new_uah"], has_usd, usd_prefix, edit["new_usd_value"], usd_suffix)
        replacement = (
            f'<del style="text-decoration:line-through;color:#94a3b8;">{old_html}</del> '
            f'<strong style="color:#DC2626;">{new_html}</strong>'
        )
    else:
        replacement = _render_price_html(edit["new_uah"], has_usd, usd_prefix, edit["new_usd_value"], usd_suffix)
    return text[: token["start"]] + replacement + text[token["end"] :]


# ==================================================================
# HTML data-i18n / data-i18n-html / data-i18n-attr синхронізація
# ==================================================================

_OPEN_TAG_RE = re.compile(r"<([a-zA-Z][\w-]*)((?:\s+[\w:-]+(?:=\"[^\"]*\")?)*)\s*/?>")


def _html_text_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _html_attr_escape(s: str) -> str:
    return _html_text_escape(s).replace('"', "&quot;")


def sync_uk_key_into_html(html_text: str, key: str, new_value: str) -> tuple[str, bool]:
    """Patches every element carrying data-i18n="key" (text or, with
    data-i18n-attr, an attribute) or data-i18n-html="key" (innerHTML) with
    new_value. This is a regex-based sync, not a full HTML parser — it's a
    convenience mirror of js/i18n.js's 'uk' entries, not the source of
    truth; the raw "Файли сайту" editor is always available as a fallback
    for anything it doesn't recognize. Returns (new_text, changed)."""
    edits: list[tuple[int, int, str]] = []
    for m in _OPEN_TAG_RE.finditer(html_text):
        tag, attrs_blob = m.group(1), m.group(2)
        text_key_m = re.search(r'data-i18n="([^"]*)"', attrs_blob)
        html_key_m = re.search(r'data-i18n-html="([^"]*)"', attrs_blob)
        attr_name_m = re.search(r'data-i18n-attr="([^"]*)"', attrs_blob)

        if html_key_m and html_key_m.group(1) == key:
            mode = "html"
        elif text_key_m and text_key_m.group(1) == key:
            mode = "attr" if attr_name_m else "text"
        else:
            continue

        if mode == "attr":
            attr_name = attr_name_m.group(1)
            attr_val_re = re.compile(re.escape(attr_name) + r'="[^"]*"')
            new_attr = f'{attr_name}="{_html_attr_escape(new_value)}"'
            new_attrs_blob, n = attr_val_re.subn(new_attr, attrs_blob, count=1)
            if n:
                edits.append((m.start(2), m.end(2), new_attrs_blob))
        else:
            close_re = re.compile(r"</" + re.escape(tag) + r">")
            close_m = close_re.search(html_text, m.end())
            if not close_m:
                continue
            inner = new_value if mode == "html" else _html_text_escape(new_value)
            edits.append((m.end(), close_m.start(), inner))

    if not edits:
        return html_text, False

    edits.sort(key=lambda e: e[0])
    out: list[str] = []
    last = 0
    for start, end, repl in edits:
        out.append(html_text[last:start])
        out.append(repl)
        last = end
    out.append(html_text[last:])
    return "".join(out), True


class WebSiteView:
    def __init__(self, content_area, user_data, db):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.manager_id = str(self.user_data.get("id") or "")
        self.t = i18n.bind(self.manager_id)

        self.sub_tab = "texts"  # texts | images | price | files | build | settings
        self._publisher = GitHubPublisher()

        self._i18n_keys: list[str] = []
        self._i18n_values: dict[str, dict[str, str]] = {}
        self._text_filter = ""
        self._text_page = 0
        self._file_cache: dict[str, bytes] = {}
        self._img_listing_cache: list[dict] | None = None
        self._shot_containers_cache: list[dict] | None = None
        self._texts_loading = False
        self._images_loading = False
        self._price_html_loaded = False
        self._prices_loading = False
        self._price_filter = ""

        self._raw_selected_file = SITE_ALL_FILES[0]

    # -------------------------------------------------------------- infra
    def _page(self):
        return self.content_area.page if self.content_area else None

    def _read_site_file(self, rel_path: str) -> str:
        """Reads rel_path's current content straight from GitHub (never
        local disk — see module docstring). Cached per WebSiteView instance
        so repeated reads within one open tab don't refetch; a successful
        publish updates the cache directly (see _save_and_publish)."""
        cached = self._file_cache.get(rel_path)
        if cached is not None:
            return cached.decode("utf-8")
        content, err = self._publisher.get_file(rel_path)
        if content is None:
            raise OSError(err or f"Файл не знайдено на GitHub: {rel_path}")
        self._file_cache[rel_path] = content
        return content.decode("utf-8")

    def _snack(self, text: str, color: str):
        page = self._page()
        if not page:
            return
        page.snack_bar = ft.SnackBar(
            ft.Text(text, color=ft.Colors.WHITE, weight="bold"),
            bgcolor=color,
            duration=3500,
        )
        page.snack_bar.open = True
        page.update()

    def _publish_items_sync(self, items: list[tuple[str, bytes]], message: str) -> tuple[int, list[str]]:
        """Runs inside asyncio.to_thread. Pushes already-computed content
        straight to GitHub, one commit per file (Contents API has no
        multi-file atomic commit without the heavier Git Data API) — no
        local disk read/write involved anywhere in this path."""
        ok_count = 0
        errors: list[str] = []
        for rel, content in items:
            ok, err = self._publisher.put_file(rel, content, message)
            if ok:
                self._file_cache[rel] = content
                if rel in SITE_HTML_FILES:
                    # Screenshot slots/captions are parsed from HTML text —
                    # a changed page invalidates the cached scan.
                    self._shot_containers_cache = None
                ok_count += 1
            else:
                errors.append(f"{rel}: {err}")
        return ok_count, errors

    async def _save_and_publish(self, items: list[tuple[str, bytes]], message: str, on_done=None):
        if not items:
            return
        if not self._publisher.configured:
            self._snack(
                "GitHub публікація не налаштована — відкрийте «Налаштування» і додайте токен.",
                "orange",
            )
            if on_done:
                on_done()
            return
        ok_count, errors = await asyncio.to_thread(self._publish_items_sync, items, message)
        if errors:
            self._snack(
                f"Опубліковано {ok_count}/{len(items)} файл(ів). Помилки: " + "; ".join(errors[:3]),
                "red",
            )
        else:
            self._snack(f"Опубліковано на GitHub: {ok_count} файл(ів)", "green")
        if on_done:
            on_done()

    async def _delete_and_publish(self, repo_path: str, message: str):
        if not self._publisher.configured:
            self._snack("GitHub публікація не налаштована — відкрийте «Налаштування».", "orange")
            return
        ok, err = await asyncio.to_thread(self._publisher.delete_file, repo_path, message)
        if ok:
            self._snack(f"Видалено на GitHub: {repo_path}", "green")
        else:
            self._snack(f"Помилка видалення на GitHub: {err}", "red")

    # -------------------------------------------------------------- build
    def build(self):
        self.content_area.controls.clear()
        with contextlib.suppress(Exception):
            self.content_area.scroll = ft.ScrollMode.ADAPTIVE

        header = ft.Text("Web сайт", size=22, weight="bold")
        nav = ft.Row(
            [
                self._nav_btn("texts", "Тексти", ft.Icons.TRANSLATE),
                self._nav_btn("images", "Зображення", ft.Icons.IMAGE_OUTLINED),
                self._nav_btn("price", "Прайс", ft.Icons.SELL_OUTLINED),
                self._nav_btn("files", "Файли сайту", ft.Icons.CODE),
                self._nav_btn("build", "Білд і публікація", ft.Icons.ROCKET_LAUNCH_OUTLINED),
                self._nav_btn("settings", "Налаштування", ft.Icons.SETTINGS_OUTLINED),
            ],
            wrap=True,
            spacing=8,
        )

        controls = [header, nav]
        if not self._publisher.configured and self.sub_tab != "settings":
            controls.append(
                ft.Container(
                    padding=10,
                    bgcolor=ft.Colors.ORANGE_50,
                    border_radius=8,
                    content=ft.Text(
                        "GitHub публікація не налаштована — ця вкладка читає й пише сайт "
                        "напряму через GitHub, без токена нічого не завантажиться. "
                        "Відкрийте «Налаштування» і додайте токен.",
                        size=12,
                        color=ft.Colors.ORANGE_900,
                    ),
                )
            )
        controls.append(ft.Divider())

        needs_texts_prefetch = (
            self.sub_tab == "texts" and not self._i18n_keys and self._publisher.configured
        )
        needs_images_prefetch = (
            self.sub_tab == "images" and self._img_listing_cache is None and self._publisher.configured
        )
        needs_price_prefetch = (
            self.sub_tab == "price" and not self._price_html_loaded and self._publisher.configured
        )

        if needs_texts_prefetch or needs_images_prefetch or needs_price_prefetch:
            controls.append(
                ft.Row(
                    [ft.ProgressRing(width=20, height=20, stroke_width=2), ft.Text("Завантаження з GitHub…")],
                    spacing=10,
                )
            )
            page = self._page()
            if page:
                if needs_texts_prefetch and not self._texts_loading:
                    self._texts_loading = True
                    page.run_task(self._prefetch_texts)
                elif needs_images_prefetch and not self._images_loading:
                    self._images_loading = True
                    page.run_task(self._prefetch_images)
                elif needs_price_prefetch and not self._prices_loading:
                    self._prices_loading = True
                    page.run_task(self._prefetch_prices)
        elif not self._publisher.configured and self.sub_tab != "settings":
            # Orange banner above already explains why — building texts/images/
            # files/build here would just hit GitHub and raise (no token), which
            # used to also show a scary "Сталася помилка" under the banner for
            # what is really just "not configured yet", not a crash.
            pass
        else:
            try:
                if self.sub_tab == "texts":
                    controls.append(self._build_texts_section())
                elif self.sub_tab == "images":
                    controls.append(self._build_images_section())
                elif self.sub_tab == "price":
                    controls.append(self._build_price_section())
                elif self.sub_tab == "files":
                    controls.append(self._build_files_section())
                elif self.sub_tab == "build":
                    controls.append(self._build_build_section())
                else:
                    controls.append(self._build_settings_section())
            except Exception:
                logger.exception("WebSiteView: failed to build section '%s'", self.sub_tab)
                controls.append(ft.Text("Сталася помилка під час побудови вкладки.", color="red"))

        self.content_area.controls.extend(controls)
        page = self._page()
        if page:
            page.update()

    async def _prefetch_texts(self):
        try:
            text = await asyncio.to_thread(self._read_site_file, I18N_JS_REL)
            self._i18n_keys, self._i18n_values = load_i18n_index(text)
        except Exception:
            logger.exception("WebSiteView: prefetch texts failed")
        finally:
            self._texts_loading = False
            self.build()

    async def _prefetch_images(self):
        try:
            entries, err = await asyncio.to_thread(self._publisher.list_dir, IMG_REPO_DIR)
            if err:
                logger.error("WebSiteView: list_dir img/ failed: %s", err)
            self._img_listing_cache = entries
            if self._shot_containers_cache is None:
                self._shot_containers_cache = await asyncio.to_thread(self._compute_shot_containers)
        except Exception:
            logger.exception("WebSiteView: prefetch images failed")
        finally:
            self._images_loading = False
            self.build()

    async def _prefetch_prices(self):
        try:
            await asyncio.to_thread(self._read_site_file, PRICE_HTML_FILE)
            self._price_html_loaded = True
        except Exception:
            logger.exception("WebSiteView: prefetch prices failed")
        finally:
            self._prices_loading = False
            self.build()

    def _nav_btn(self, key: str, label: str, icon):
        active = self.sub_tab == key

        def _click(_e, k=key):
            self.sub_tab = k
            self.build()

        return ft.FilledButton(
            label,
            icon=icon,
            on_click=_click,
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.BLUE_700 if active else ft.Colors.BLUE_GREY_100,
                color=ft.Colors.WHITE if active else ft.Colors.BLACK87,
            ),
        )

    # -------------------------------------------------------------- 1. Тексти
    def _build_texts_section(self):
        if not self._i18n_keys:
            text = self._read_site_file(I18N_JS_REL)
            self._i18n_keys, self._i18n_values = load_i18n_index(text)

        filter_field = ft.TextField(
            label="Пошук за ключем або текстом",
            value=self._text_filter,
            on_change=self._on_text_filter_change,
            border_radius=10,
            prefix_icon=ft.Icons.SEARCH,
        )

        needle = (self._text_filter or "").strip().lower()
        if needle:
            keys = [
                k
                for k in self._i18n_keys
                if needle in k.lower()
                or any(needle in (v or "").lower() for v in self._i18n_values.get(k, {}).values())
            ]
        else:
            keys = self._i18n_keys

        page_size = 60
        total_pages = max(1, (len(keys) + page_size - 1) // page_size)
        self._text_page = max(0, min(self._text_page, total_pages - 1))
        start = self._text_page * page_size
        shown = keys[start : start + page_size]
        rows = [self._build_text_key_row(k) for k in shown]

        info: list[ft.Control] = [
            ft.Text(f"Ключів перекладу: {len(self._i18n_keys)}", size=12, color="grey600")
        ]

        pager: list[ft.Control] = []
        if total_pages > 1:
            pager.append(
                ft.Row(
                    [
                        ft.IconButton(
                            ft.Icons.CHEVRON_LEFT,
                            on_click=self._prev_text_page,
                            disabled=self._text_page == 0,
                        ),
                        ft.Text(
                            f"{start + 1}–{min(start + page_size, len(keys))} з {len(keys)}"
                            f" (сторінка {self._text_page + 1}/{total_pages})",
                            size=12,
                        ),
                        ft.IconButton(
                            ft.Icons.CHEVRON_RIGHT,
                            on_click=self._next_text_page,
                            disabled=self._text_page >= total_pages - 1,
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                    spacing=6,
                )
            )

        return ft.Column(
            [*info, filter_field, *pager, ft.Column(rows, spacing=6), *pager],
            spacing=10,
        )

    def _on_text_filter_change(self, e):
        self._text_filter = e.control.value or ""
        self._text_page = 0
        self.build()

    def _prev_text_page(self, e):
        self._text_page = max(0, self._text_page - 1)
        self.build()

    def _next_text_page(self, e):
        self._text_page += 1
        self.build()

    def _build_text_key_row(self, key: str):
        original_values = dict(self._i18n_values.get(key, {}))
        fields: dict[str, ft.TextField] = {}
        for lang in LANGS:
            fields[lang] = ft.TextField(
                label=LANG_LABELS.get(lang, lang),
                value=original_values.get(lang, ""),
                multiline=True,
                min_lines=1,
                max_lines=4,
                dense=True,
                border_radius=8,
            )

        async def _do_save(_key=key, _fields=fields, _orig=original_values):
            changed_langs = {
                lang: (field.value or "")
                for lang, field in _fields.items()
                if (field.value or "") != (_orig.get(lang, "") or "")
            }
            if not changed_langs:
                self._snack("Немає змін", "grey")
                return

            def _apply():
                text = self._read_site_file(I18N_JS_REL)
                touched_i18n = False
                for lang, new_val in changed_langs.items():
                    text, ch = set_i18n_entry(text, lang, _key, new_val)
                    touched_i18n = touched_i18n or ch

                items: list[tuple[str, bytes]] = []
                if touched_i18n:
                    items.append((I18N_JS_REL, text.encode("utf-8")))
                if "uk" in changed_langs:
                    for rel in SITE_HTML_FILES:
                        html_text = self._read_site_file(rel)
                        new_html, ch = sync_uk_key_into_html(html_text, _key, changed_langs["uk"])
                        if ch:
                            items.append((rel, new_html.encode("utf-8")))
                return items

            items = await asyncio.to_thread(_apply)
            if not items:
                self._snack("Ключ не знайдено у файлі — змін не застосовано", "orange")
                return
            for lang, new_val in changed_langs.items():
                original_values[lang] = new_val
                self._i18n_values.setdefault(_key, {})[lang] = new_val
            await self._save_and_publish(items, f"Web сайт: оновлено текст «{_key}»")

        def _on_click(_e, _s=_do_save):
            page = self._page()
            if page:
                page.run_task(_s)

        return ft.Card(
            ft.Container(
                padding=12,
                content=ft.Column(
                    [
                        ft.Text(key, size=12, weight="bold", color="grey800", selectable=True),
                        *fields.values(),
                        ft.Row(
                            [ft.FilledButton("Зберегти", icon=ft.Icons.SAVE, on_click=_on_click)],
                            alignment=ft.MainAxisAlignment.END,
                        ),
                    ],
                    spacing=6,
                ),
            )
        )

    # -------------------------------------------------------------- 1b. Прайс
    def _build_price_section(self):
        text = self._read_site_file(PRICE_HTML_FILE)
        tokens = parse_pricing_tokens(text)
        grouped_rows, flat_tokens = group_pricing_tokens(tokens)

        filter_field = ft.TextField(
            label="Пошук за назвою",
            value=self._price_filter,
            on_change=self._on_price_filter_change,
            border_radius=10,
            prefix_icon=ft.Icons.SEARCH,
        )

        needle = (self._price_filter or "").strip().lower()
        if needle:
            grouped_rows = [r for r in grouped_rows if needle in r["label"].lower()]
            flat_tokens = [t for t in flat_tokens if needle in t["label"].lower()]

        content: list[ft.Control] = [
            ft.Text(
                f"{len(tokens)} цін на сторінці: {len(grouped_rows)} рядків у таблиці "
                f"«Вкладки/люди», {len(flat_tokens)} — окремих позицій нижче.",
                size=12,
                color="grey600",
            ),
            filter_field,
        ]

        if grouped_rows:
            content.append(ft.Text("Вкладки / кількість людей", size=13, weight="bold"))
            content.append(self._build_price_table_grouped(grouped_rows))
        if flat_tokens:
            content.append(ft.Text("GSM, одноразові послуги, тарифні пакети", size=13, weight="bold"))
            content.append(self._build_price_table_flat(flat_tokens))
        if not grouped_rows and not flat_tokens:
            content.append(ft.Text("Нічого не знайдено", size=12, color="grey600"))

        return ft.Column(content, spacing=10)

    def _on_price_filter_change(self, e):
        self._price_filter = e.control.value or ""
        self.build()

    @staticmethod
    def _price_money_field(value: str, width: int, visible: bool = True) -> ft.TextField:
        return ft.TextField(
            value=value,
            width=width,
            dense=True,
            content_padding=ft.padding.symmetric(horizontal=6, vertical=6),
            text_size=12,
            border_radius=6,
            visible=visible,
        )

    def _parse_price_money(self, field: ft.TextField, label: str) -> str | None:
        raw = (field.value or "").strip().replace(",", ".")
        try:
            float(raw)
        except ValueError:
            self._snack(f"«{label}»: ціна має бути числом", "red")
            return None
        return raw

    async def _publish_price_edits(self, edits: list[dict], summary_label: str):
        if not edits:
            self._snack("Немає змін", "grey")
            return

        def _apply():
            current_text = self._read_site_file(PRICE_HTML_FILE)
            return apply_price_token_edits(current_text, edits)

        new_text = await asyncio.to_thread(_apply)
        items = [(PRICE_HTML_FILE, new_text.encode("utf-8"))]
        await self._save_and_publish(
            items, f"Web сайт: оновлено {len(edits)} цін ({summary_label})", on_done=self.build
        )

    # Grouped table: one row per item, 1/5/10/15-people prices as 4 column pairs.
    def _build_price_table_grouped(self, rows: list[dict]) -> ft.Column:
        columns = [ft.DataColumn(ft.Text("Назва", size=11, weight="bold"))]
        for col_label in _PRICE_COLUMN_LABELS:
            columns.append(ft.DataColumn(ft.Text(f"{col_label}, ₴", size=10, weight="bold")))
            columns.append(ft.DataColumn(ft.Text("$", size=10, weight="bold")))

        field_map: list[tuple[dict, ft.TextField, ft.TextField]] = []
        data_rows = []
        for row in rows:
            cells = [ft.DataCell(ft.Text(row["label"], size=11, no_wrap=False, width=140))]
            for tok in row["cols"]:
                uah_f = self._price_money_field(tok["uah"], 60)
                usd_f = self._price_money_field(tok["usd_value"], 46, visible=tok["has_usd"])
                field_map.append((tok, uah_f, usd_f))
                cells.append(ft.DataCell(uah_f))
                cells.append(ft.DataCell(usd_f))
            data_rows.append(ft.DataRow(cells=cells))

        async def _save_all(_e=None):
            edits: list[dict] = []
            for tok, uah_f, usd_f in field_map:
                new_uah = self._parse_price_money(uah_f, tok["label"])
                if new_uah is None:
                    return
                new_usd = tok["usd_value"]
                if tok["has_usd"]:
                    new_usd = self._parse_price_money(usd_f, tok["label"])
                    if new_usd is None:
                        return
                if new_uah == tok["uah"] and new_usd == tok["usd_value"]:
                    continue
                edits.append({**tok, "discount": False, "new_uah": new_uah, "new_usd_value": new_usd})
            await self._publish_price_edits(edits, "таблиця вкладок/людей")

        def _on_click(_e):
            page = self._page()
            if page:
                page.run_task(_save_all)

        return ft.Column(
            [
                ft.Row(
                    [ft.DataTable(columns=columns, rows=data_rows, column_spacing=6,
                                   data_row_min_height=34, heading_row_height=34)],
                    scroll=ft.ScrollMode.ALWAYS,
                ),
                ft.Row(
                    [ft.FilledButton("Зберегти зміни в таблиці", icon=ft.Icons.SAVE, on_click=_on_click)],
                    alignment=ft.MainAxisAlignment.END,
                ),
            ],
            spacing=6,
        )

    # Flat table: everything without the 4-column shape — one row per price,
    # with an inline discount checkbox + a "new price" pair of fields.
    def _build_price_table_flat(self, tokens: list[dict]) -> ft.Column:
        columns = [
            ft.DataColumn(ft.Text("Назва", size=11, weight="bold")),
            ft.DataColumn(ft.Text("Ціна ₴", size=10, weight="bold")),
            ft.DataColumn(ft.Text("$", size=10, weight="bold")),
            ft.DataColumn(ft.Text("Знижка", size=10, weight="bold")),
            ft.DataColumn(ft.Text("Нова ₴", size=10, weight="bold")),
            ft.DataColumn(ft.Text("Нова $", size=10, weight="bold")),
        ]

        row_state: list[tuple[dict, ft.TextField, ft.TextField, ft.Checkbox, ft.TextField, ft.TextField]] = []
        data_rows = []
        for tok in tokens:
            uah_f = self._price_money_field(tok["uah"], 60)
            usd_f = self._price_money_field(tok["usd_value"], 46, visible=tok["has_usd"])
            discount_cb = ft.Checkbox(value=False)
            new_uah_f = self._price_money_field("", 60)
            new_usd_f = self._price_money_field("", 46, visible=tok["has_usd"])
            row_state.append((tok, uah_f, usd_f, discount_cb, new_uah_f, new_usd_f))
            data_rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(ft.Text(tok["label"], size=11, width=160)),
                        ft.DataCell(uah_f),
                        ft.DataCell(usd_f),
                        ft.DataCell(discount_cb),
                        ft.DataCell(new_uah_f),
                        ft.DataCell(new_usd_f),
                    ]
                )
            )

        async def _save_all(_e=None):
            edits: list[dict] = []
            for tok, uah_f, usd_f, discount_cb, new_uah_f, new_usd_f in row_state:
                is_discount = bool(discount_cb.value)
                cur_uah = self._parse_price_money(uah_f, tok["label"])
                if cur_uah is None:
                    return
                cur_usd = tok["usd_value"]
                if tok["has_usd"]:
                    cur_usd = self._parse_price_money(usd_f, tok["label"])
                    if cur_usd is None:
                        return

                if is_discount:
                    if not (new_uah_f.value or "").strip():
                        continue
                    disc_uah = self._parse_price_money(new_uah_f, tok["label"])
                    if disc_uah is None:
                        return
                    disc_usd = cur_usd
                    if tok["has_usd"]:
                        disc_usd = self._parse_price_money(new_usd_f, tok["label"])
                        if disc_usd is None:
                            return
                    edits.append(
                        {
                            **tok,
                            "discount": True,
                            "old_uah": cur_uah,
                            "old_usd_value": cur_usd,
                            "new_uah": disc_uah,
                            "new_usd_value": disc_usd,
                        }
                    )
                elif cur_uah != tok["uah"] or cur_usd != tok["usd_value"]:
                    edits.append({**tok, "discount": False, "new_uah": cur_uah, "new_usd_value": cur_usd})
            await self._publish_price_edits(edits, "решта цін")

        def _on_click(_e):
            page = self._page()
            if page:
                page.run_task(_save_all)

        return ft.Column(
            [
                ft.Row(
                    [ft.DataTable(columns=columns, rows=data_rows, column_spacing=6,
                                   data_row_min_height=34, heading_row_height=34)],
                    scroll=ft.ScrollMode.ALWAYS,
                ),
                ft.Row(
                    [ft.FilledButton("Зберегти зміни в таблиці", icon=ft.Icons.SAVE, on_click=_on_click)],
                    alignment=ft.MainAxisAlignment.END,
                ),
            ],
            spacing=6,
        )

    # -------------------------------------------------------------- 2. Зображення
    def _find_shot_containers(self) -> list[dict]:
        """Cache-aware wrapper — see _compute_shot_containers for the real
        scan. Cache is warmed by _prefetch_images and invalidated whenever a
        SITE_HTML_FILES page gets re-published (_publish_items_sync)."""
        if self._shot_containers_cache is not None:
            return self._shot_containers_cache
        return self._compute_shot_containers()

    def _compute_shot_containers(self) -> list[dict]:
        """Scans every HTML page for [data-shot] screenshot slots — the
        site's actual "these are the photos we need" list (see
        js/main.js openLightbox/probe: it tries img/<data-shot> and falls
        back to a placeholder if missing). This is deliberately NOT just
        "list whatever's in img/" — most slots aren't filled yet, and the
        admin needs to see and fill the empty ones too, from every page."""
        containers = []
        for rel in SITE_HTML_FILES:
            try:
                text = self._read_site_file(rel)
            except OSError:
                continue
            # Ordered (position, text) list of this page's intro/section
            # paragraphs, used below to find each shot's nearest preceding
            # one — i.e. which feature section it illustrates.
            descs = [(dm.start(), _strip_html(dm.group(1))) for dm in _DESC_RE.finditer(text)]
            desc_idx = -1
            for m in _SHOT_TAG_RE.finditer(text):
                filename = m.group(1)
                window = text[m.end() : m.end() + 500]
                next_m = _SHOT_TAG_RE.search(window)
                if next_m:
                    window = window[: next_m.start()]
                cap_m = re.search(r'<strong(?:\s+data-i18n="[^"]*")?[^>]*>([^<]*)</strong>', window)
                caption = (cap_m.group(1).strip() if cap_m else "") or filename

                while desc_idx + 1 < len(descs) and descs[desc_idx + 1][0] <= m.start():
                    desc_idx += 1
                description = _shorten(descs[desc_idx][1]) if desc_idx >= 0 else ""

                containers.append(
                    {"file": rel, "filename": filename, "caption": caption, "description": description}
                )
        return containers

    def _build_images_section(self):
        entries = self._img_listing_cache or []
        disk_names = {e["name"] for e in entries}
        sizes_by_name = {e["name"]: int(e.get("size") or 0) for e in entries}

        containers = self._find_shot_containers()
        filled = sum(1 for c in containers if c["filename"] in disk_names)

        sections: list[ft.Control] = [
            ft.Text(
                f"Слотів для скріншотів на сайті: {len(containers)} · заповнено: {filled} · порожньо: {len(containers) - filled}",
                size=12,
                color="grey600",
            )
        ]

        by_file: dict[str, list[dict]] = {}
        for c in containers:
            by_file.setdefault(c["file"], []).append(c)

        # Shared, filled-in-render-order list of {path, caption} for every
        # image that actually has a file — backs the click-to-enlarge
        # lightbox's prev/next navigation. Each tile builder below appends
        # to it (in the same order it renders), so by the time a user
        # clicks any thumbnail the list already covers every image on the
        # page in left-to-right, top-to-bottom order.
        lightbox_images: list[dict] = []

        for rel in SITE_HTML_FILES:
            page_containers = by_file.get(rel)
            if not page_containers:
                continue
            tiles = [
                self._build_shot_tile(
                    c, c["filename"] in disk_names, sizes_by_name.get(c["filename"], 0), lightbox_images
                )
                for c in page_containers
            ]
            sections.append(
                ft.Column(
                    [
                        ft.Text(PAGE_TITLES.get(rel, rel), size=14, weight="bold"),
                        ft.Row(tiles, wrap=True, spacing=10, run_spacing=10),
                    ],
                    spacing=6,
                )
            )

        container_names = {c["filename"] for c in containers}
        orphans = sorted(disk_names - container_names)
        if orphans:
            sections.append(
                ft.Column(
                    [
                        ft.Text(
                            "Інші файли в img/ (без прив'язки до жодної сторінки)",
                            size=14,
                            weight="bold",
                        ),
                        ft.Row(
                            [
                                self._build_orphan_tile(name, sizes_by_name.get(name, 0), lightbox_images)
                                for name in orphans
                            ],
                            wrap=True,
                            spacing=10,
                            run_spacing=10,
                        ),
                    ],
                    spacing=6,
                )
            )

        return ft.Column(sections, spacing=18)

    def _open_lightbox(self, images: list[dict], start_index: int):
        """Full-size click-to-enlarge viewer with close + prev/next
        (mouse and ←/→/Esc keys) — same proven pattern as the packaging
        photo gallery in views/packaging_view.py's open_image_view()."""
        page = self._page()
        if not page or not images:
            return
        dw = (getattr(page, "width", None) or 1200) * 0.8
        dh = (getattr(page, "height", None) or 900) * 0.8
        img_w = dw - 120
        img_h = dh - 70
        idx = [start_index]

        gal_img = ft.Image(src=images[idx[0]]["path"], width=img_w, height=img_h, fit=ft.BoxFit.CONTAIN)
        caption_text = ft.Text(images[idx[0]]["caption"], color=ft.Colors.WHITE, size=13)
        counter = ft.Text(
            f"{idx[0] + 1} / {len(images)}", color=ft.Colors.GREY_400, size=11, visible=len(images) > 1
        )

        def _go(delta, _e=None):
            idx[0] = (idx[0] + delta) % len(images)
            gal_img.src = images[idx[0]]["path"]
            caption_text.value = images[idx[0]]["caption"]
            counter.value = f"{idx[0] + 1} / {len(images)}"
            with contextlib.suppress(Exception):
                gal_img.update()
                caption_text.update()
                counter.update()

        arrow_style = ft.ButtonStyle(
            bgcolor=ft.Colors.with_opacity(0.22, ft.Colors.WHITE), shape=ft.CircleBorder()
        )
        prev_btn = ft.IconButton(
            icon=ft.Icons.CHEVRON_LEFT,
            icon_color=ft.Colors.WHITE,
            icon_size=34,
            on_click=lambda _e: _go(-1),
            visible=len(images) > 1,
            style=arrow_style,
        )
        next_btn = ft.IconButton(
            icon=ft.Icons.CHEVRON_RIGHT,
            icon_color=ft.Colors.WHITE,
            icon_size=34,
            on_click=lambda _e: _go(1),
            visible=len(images) > 1,
            style=arrow_style,
        )

        dlg_ref: list = [None]
        prev_kb = [getattr(page, "on_keyboard_event", None)]

        def _close(_e=None):
            page.on_keyboard_event = prev_kb[0]
            with contextlib.suppress(Exception):
                page.pop_dialog()

        def _on_kb(e):
            if e.key == "Arrow Left":
                _go(-1)
            elif e.key == "Arrow Right":
                _go(1)
            elif e.key == "Escape":
                _close()

        page.on_keyboard_event = _on_kb

        dlg_ref[0] = ft.AlertDialog(
            content=ft.Stack(
                [
                    ft.Container(
                        width=dw,
                        height=dh,
                        bgcolor="#111111",
                        content=ft.Column(
                            [
                                ft.Row(
                                    [
                                        prev_btn,
                                        ft.Container(
                                            content=gal_img, expand=True, alignment=ft.Alignment(0, 0)
                                        ),
                                        next_btn,
                                    ],
                                    expand=True,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                    spacing=4,
                                ),
                                ft.Container(content=caption_text, alignment=ft.Alignment(0, 0)),
                                ft.Container(
                                    content=counter,
                                    alignment=ft.Alignment(0, 0),
                                    padding=ft.padding.only(bottom=4),
                                ),
                            ],
                            spacing=0,
                            expand=True,
                        ),
                    ),
                    ft.Container(
                        content=ft.IconButton(
                            icon=ft.Icons.CLOSE,
                            icon_color=ft.Colors.WHITE,
                            icon_size=20,
                            on_click=_close,
                            style=ft.ButtonStyle(bgcolor=ft.Colors.with_opacity(0.55, ft.Colors.BLACK)),
                        ),
                        right=6,
                        top=6,
                    ),
                ],
                width=dw,
                height=dh,
            ),
            bgcolor="#111111",
            content_padding=ft.padding.all(0),
            on_dismiss=lambda _e: setattr(page, "on_keyboard_event", prev_kb[0]),
        )
        page.open(dlg_ref[0])

    async def _pick_single_file(self, dialog_title: str, extensions: list[str]) -> str:
        page = self._page()
        if not page:
            return ""
        fp = ft.FilePicker()
        page.services.append(fp)
        page.update()
        await asyncio.sleep(0.2)
        result = await fp.pick_files(
            dialog_title=dialog_title, allow_multiple=False, allowed_extensions=extensions
        )
        with contextlib.suppress(Exception):
            page.services.remove(fp)
            page.update()
        if not result:
            return ""
        return str(getattr(result[0], "path", "") or "")

    def _build_shot_tile(self, container: dict, exists: bool, size_bytes: int, lightbox_images: list[dict]):
        filename = container["filename"]
        caption = container["caption"]
        description = container.get("description") or ""
        img_url = self._publisher.raw_url(f"{IMG_REPO_DIR}/{filename}")
        size_kb = (size_bytes / 1024) if exists else 0

        async def _do_set(_filename=filename):
            src_path = await self._pick_single_file(
                f"Оберіть зображення для «{caption}»", ["png", "jpg", "jpeg", "webp", "svg"]
            )
            if not src_path:
                return

            def _apply() -> bytes:
                with open(src_path, "rb") as f:
                    return f.read()

            def _on_done():
                self._img_listing_cache = None
                self.build()

            data = await asyncio.to_thread(_apply)
            await self._save_and_publish(
                [(f"{IMG_REPO_DIR}/{_filename}", data)],
                f"Web сайт: оновлено зображення {_filename}",
                on_done=_on_done,
            )

        def _set_click(_e, _s=_do_set):
            page = self._page()
            if page:
                page.run_task(_s)

        def _delete_click(_e, _filename=filename, _caption=caption):
            page = self._page()
            if not page:
                return
            dlg_ref: list = [None]

            async def _do_delete():
                await self._delete_and_publish(
                    f"{IMG_REPO_DIR}/{_filename}", f"Web сайт: видалено зображення {_filename}"
                )
                self._img_listing_cache = None
                self.build()

            def _confirm(_e2):
                with contextlib.suppress(Exception):
                    page.pop_dialog()
                page.run_task(_do_delete)

            dlg_ref[0] = ft.AlertDialog(
                modal=True,
                title=ft.Text(f"Видалити «{_caption}»?"),
                content=ft.Text(
                    f"Слот img/{_filename} стане порожнім — на сайті знову покаже плейсхолдер."
                ),
                actions=[
                    ft.TextButton("Скасувати", on_click=lambda _e3: page.pop_dialog()),
                    ft.FilledButton(
                        "Видалити", icon=ft.Icons.DELETE, on_click=_confirm, bgcolor="red"
                    ),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
            page.show_dialog(dlg_ref[0])

        if exists:
            lb_index = len(lightbox_images)
            lightbox_images.append({"path": img_url, "caption": caption})

            def _open_click(_e, _imgs=lightbox_images, _idx=lb_index):
                self._open_lightbox(_imgs, _idx)

            thumb = ft.Container(
                content=ft.Image(src=img_url, width=180, height=100, fit=ft.BoxFit.CONTAIN),
                on_click=_open_click,
                ink=True,
                border_radius=6,
                tooltip="Переглянути на весь екран",
            )
            status = ft.Text(f"{size_kb:.0f} КБ", size=10, color="grey600")
            actions = ft.Row(
                [
                    ft.IconButton(
                        icon=ft.Icons.UPLOAD_FILE, tooltip="Замінити", on_click=_set_click, icon_size=18
                    ),
                    ft.IconButton(
                        icon=ft.Icons.DELETE_OUTLINE,
                        tooltip="Видалити",
                        on_click=_delete_click,
                        icon_size=18,
                    ),
                ],
                alignment=ft.MainAxisAlignment.END,
            )
        else:
            thumb = ft.Container(
                width=180,
                height=100,
                bgcolor=ft.Colors.BLUE_GREY_50,
                border_radius=6,
                alignment=ft.Alignment(0, 0),
                content=ft.Icon(
                    ft.Icons.IMAGE_NOT_SUPPORTED_OUTLINED, color=ft.Colors.BLUE_GREY_300, size=32
                ),
            )
            status = ft.Text("Немає зображення", size=10, color=ft.Colors.ORANGE_700)
            actions = ft.Row(
                [
                    ft.FilledButton(
                        "Завантажити", icon=ft.Icons.UPLOAD_FILE, on_click=_set_click, height=32
                    )
                ],
                alignment=ft.MainAxisAlignment.END,
            )

        return ft.Container(
            width=230,
            padding=8,
            border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
            border_radius=10,
            content=ft.Column(
                [
                    thumb,
                    ft.Text(caption, size=11, weight="bold", max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                    *(
                        [ft.Text(description, size=10, color="grey600", max_lines=3, overflow=ft.TextOverflow.ELLIPSIS)]
                        if description
                        else []
                    ),
                    ft.Text(f"img/{filename}", size=9, color="grey500", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    status,
                    actions,
                ],
                spacing=4,
            ),
        )

    def _build_orphan_tile(self, name: str, size_bytes: int, lightbox_images: list[dict]):
        img_url = self._publisher.raw_url(f"{IMG_REPO_DIR}/{name}")
        size_kb = size_bytes / 1024

        lb_index = len(lightbox_images)
        lightbox_images.append({"path": img_url, "caption": name})

        def _open_click(_e, _imgs=lightbox_images, _idx=lb_index):
            self._open_lightbox(_imgs, _idx)

        def _delete_click(_e, _name=name):
            page = self._page()
            if not page:
                return
            dlg_ref: list = [None]

            async def _do_delete():
                await self._delete_and_publish(f"{IMG_REPO_DIR}/{_name}", f"Web сайт: видалено зображення {_name}")
                self._img_listing_cache = None
                self.build()

            def _confirm(_e2):
                with contextlib.suppress(Exception):
                    page.pop_dialog()
                page.run_task(_do_delete)

            dlg_ref[0] = ft.AlertDialog(
                modal=True,
                title=ft.Text(f"Видалити {_name}?"),
                content=ft.Text("Цей файл не прив'язаний до жодного слота на сторінках сайту."),
                actions=[
                    ft.TextButton("Скасувати", on_click=lambda _e3: page.pop_dialog()),
                    ft.FilledButton(
                        "Видалити", icon=ft.Icons.DELETE, on_click=_confirm, bgcolor="red"
                    ),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
            )
            page.show_dialog(dlg_ref[0])

        return ft.Container(
            width=200,
            padding=8,
            border=ft.border.all(1, ft.Colors.BLUE_GREY_100),
            border_radius=10,
            content=ft.Column(
                [
                    ft.Container(
                        content=ft.Image(src=img_url, width=180, height=100, fit=ft.BoxFit.CONTAIN),
                        on_click=_open_click,
                        ink=True,
                        border_radius=6,
                        tooltip="Переглянути на весь екран",
                    ),
                    ft.Text(name, size=11, weight="bold", max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                    ft.Text(f"{size_kb:.0f} КБ", size=10, color="grey600"),
                    ft.Row(
                        [
                            ft.IconButton(
                                icon=ft.Icons.DELETE_OUTLINE,
                                tooltip="Видалити",
                                on_click=_delete_click,
                                icon_size=18,
                            )
                        ],
                        alignment=ft.MainAxisAlignment.END,
                    ),
                ],
                spacing=4,
            ),
        )

    # -------------------------------------------------------------- 3. Файли сайту
    def _build_files_section(self):
        dropdown = ft.Dropdown(
            label="Файл",
            value=self._raw_selected_file,
            options=[ft.dropdown.Option(f) for f in SITE_ALL_FILES],
            on_select=self._on_raw_file_change,
            border_radius=10,
            width=340,
        )
        try:
            content = self._read_site_file(self._raw_selected_file)
        except OSError as e:
            logger.exception("WebSiteView: cannot read %s", self._raw_selected_file)
            content = ""
            self._snack(f"Не вдалося прочитати файл з GitHub: {e}", "red")

        editor = ft.TextField(
            value=content,
            multiline=True,
            min_lines=28,
            max_lines=28,
            border_radius=10,
            text_size=12,
            text_style=ft.TextStyle(font_family="Consolas"),
        )

        async def _do_save(_rel=self._raw_selected_file, _editor=editor):
            new_content = _editor.value or ""
            await self._save_and_publish(
                [(_rel, new_content.encode("utf-8"))], f"Web сайт: правка файлу {_rel}"
            )

        def _on_click(_e, _s=_do_save):
            page = self._page()
            if page:
                page.run_task(_s)

        return ft.Column(
            [
                ft.Text(
                    "Довільна правка HTML/CSS/JS. Правки тут НЕ синхронізуються назад у "
                    "вкладку «Тексти» — окремий шлях для верстки/стилів і всього, чого не "
                    "покривають структуровані редактори.",
                    size=12,
                    color="grey600",
                ),
                ft.Row([dropdown, ft.FilledButton("Зберегти", icon=ft.Icons.SAVE, on_click=_on_click)]),
                editor,
            ],
            spacing=10,
            expand=True,
        )

    def _on_raw_file_change(self, e):
        self._raw_selected_file = e.control.value
        self.build()

    # -------------------------------------------------------------- 4. Білд і публікація
    def _scan_program_versions(self) -> list[dict]:
        """Scans .program\\CatManPro-<version>\\ for built .setup\\*.exe /
        .apk\\*.apk, checking both the flat legacy layout and the newer
        per-app-profile ".../CatManPro/.setup" layout (build_release.ps1
        supports both across different builds on disk)."""
        program_dir = os.path.join(ROOT, ".program")
        versions: list[dict] = []
        if not os.path.isdir(program_dir):
            return versions
        for entry in sorted(os.listdir(program_dir), reverse=True):
            if not entry.startswith("CatManPro-"):
                continue
            version = entry[len("CatManPro-"):]
            version_root = os.path.join(program_dir, entry)
            exe_path = apk_path = ""
            for base in (os.path.join(version_root, "CatManPro"), version_root):
                if not exe_path:
                    found = glob.glob(os.path.join(base, ".setup", "*.exe"))
                    if found:
                        exe_path = found[0]
                if not apk_path:
                    found = glob.glob(os.path.join(base, ".apk", "*.apk"))
                    if found:
                        apk_path = found[0]
            if exe_path or apk_path:
                versions.append({"version": version, "exe": exe_path, "apk": apk_path})
        return versions

    def _build_build_section(self):
        versions = self._scan_program_versions()
        version_dd = ft.Dropdown(
            label="Знайдена збірка (.program\\)",
            options=[
                ft.dropdown.Option(
                    v["version"],
                    text=f'{v["version"]} ({"exe" if v["exe"] else "—"} / {"apk" if v["apk"] else "—"})',
                )
                for v in versions
            ],
            value=versions[0]["version"] if versions else None,
            border_radius=10,
            width=320,
        )
        tag_field = ft.TextField(
            label="Тег релізу",
            hint_text="v1.3.0",
            border_radius=10,
            width=200,
            value=("v" + versions[0]["version"]) if versions else "",
        )
        name_field = ft.TextField(
            label="Назва релізу",
            border_radius=10,
            width=320,
            value=(f'CatMan-Pro {versions[0]["version"]}') if versions else "",
        )
        status_text = ft.Text("", size=12, color="grey700")

        def _selected_version():
            v = version_dd.value
            return next((x for x in versions if x["version"] == v), None)

        async def _do_publish_build():
            entry = _selected_version()
            if not entry:
                self._snack("Оберіть збірку", "orange")
                return
            assets = []
            if entry["exe"]:
                assets.append((entry["exe"], "CatManPro-Setup.exe"))
            if entry["apk"]:
                assets.append((entry["apk"], "CatManPro-android.apk"))
            if not assets:
                self._snack("У цій збірці немає ні exe, ні apk", "orange")
                return
            tag = (tag_field.value or "").strip()
            if not tag:
                self._snack("Вкажіть тег релізу", "orange")
                return
            name = (name_field.value or "").strip() or tag

            status_text.value = "Публікація релізу…"
            with contextlib.suppress(Exception):
                status_text.update()

            ok, err = await asyncio.to_thread(self._publisher.publish_build, tag, name, assets)
            status_text.value = "Готово ✓" if ok else f"Помилка: {err}"
            with contextlib.suppress(Exception):
                status_text.update()
            self._snack(
                "Реліз опубліковано на GitHub" if ok else f"Помилка публікації релізу: {err}",
                "green" if ok else "red",
            )

        def _on_publish_click(_e):
            page = self._page()
            if page:
                page.run_task(_do_publish_build)

        return ft.Column(
            [
                ft.Text("Публікація білда (.exe / .apk)", size=16, weight="bold"),
                version_dd,
                ft.Row([tag_field, name_field]),
                ft.FilledButton(
                    "Опублікувати реліз на GitHub",
                    icon=ft.Icons.ROCKET_LAUNCH_OUTLINED,
                    on_click=_on_publish_click,
                ),
                status_text,
            ],
            spacing=10,
        )

    # -------------------------------------------------------------- 5. Налаштування
    def _build_settings_section(self):
        cfg = load_publish_config()
        token_field = ft.TextField(
            label="GitHub Personal Access Token",
            password=True,
            can_reveal_password=True,
            hint_text="github_pat_… (права repo)",
            border_radius=10,
        )
        owner_field = ft.TextField(
            label="Власник репозиторію", value=cfg.get("owner", "vitalik19970527-prog"), border_radius=10
        )
        repo_field = ft.TextField(label="Репозиторій", value=cfg.get("repo", "CatMan-Pro"), border_radius=10)
        branch_field = ft.TextField(label="Гілка", value=cfg.get("branch", "main"), border_radius=10)
        status_text = ft.Text(
            "Токен налаштовано." if self._publisher.configured else "Токен ще не налаштовано.",
            size=12,
            color="green" if self._publisher.configured else "orange",
        )

        def _save(_e):
            new_cfg = dict(cfg)
            if (token_field.value or "").strip():
                new_cfg["token"] = token_field.value.strip()
            new_cfg["owner"] = (owner_field.value or "").strip() or "vitalik19970527-prog"
            new_cfg["repo"] = (repo_field.value or "").strip() or "CatMan-Pro"
            new_cfg["branch"] = (branch_field.value or "").strip() or "main"
            ok = save_publish_config(new_cfg)
            if ok:
                self._publisher = GitHubPublisher()
                self._snack("Налаштування публікації збережено", "green")
                self.build()
            else:
                self._snack("Не вдалося зберегти налаштування", "red")

        return ft.Column(
            [
                ft.Text("Налаштування публікації на GitHub", size=16, weight="bold"),
                status_text,
                owner_field,
                repo_field,
                branch_field,
                token_field,
                ft.Text(
                    "Токен зберігається лише локально (secrets/github_publish.json) — "
                    "ніколи не потрапляє у зібраний exe/apk.",
                    size=11,
                    color="grey600",
                ),
                ft.FilledButton("Зберегти", icon=ft.Icons.SAVE, on_click=_save),
            ],
            spacing=10,
        )
