"""
Application theme definitions — 11 themes.
Nav icon packs — 6 packs for sidebar navigation symbols.
"""
from __future__ import annotations

import flet as ft
import i18n
from typing import Any

# ─── Per-user active state ────────────────────────────────────────────────────

_active_themes: dict[str, str] = {}
_active_nav_packs: dict[str, str] = {}


def set_active_theme(manager_id: str, theme_key: str | None) -> None:
    if manager_id:
        _active_themes[manager_id] = theme_key or DEFAULT_THEME


def get_active_theme_key(manager_id: str) -> str:
    return _active_themes.get(manager_id) or DEFAULT_THEME


def get_active_theme(manager_id: str) -> dict:
    return get_theme(_active_themes.get(manager_id))


def set_active_nav_pack(manager_id: str, pack_key: str | None) -> None:
    if manager_id:
        _active_nav_packs[manager_id] = pack_key or DEFAULT_NAV_PACK


def get_active_nav_pack_key(manager_id: str) -> str:
    return _active_nav_packs.get(manager_id) or DEFAULT_NAV_PACK


def get_active_nav_pack(manager_id: str) -> dict:
    key = _active_nav_packs.get(manager_id) or DEFAULT_NAV_PACK
    return NAV_ICON_PACKS.get(key) or NAV_ICON_PACKS[DEFAULT_NAV_PACK]


# ─── Internal status symbols (embedded per theme) ─────────────────────────────

_SYM_STANDARD = {"ok": "✓", "err": "✗", "warn": "!", "inactive": "○", "active": "●", "add": "+", "star": "★", "bullet": "·", "arrow": "→", "sep": "│"}
_SYM_MINIMAL  = {"ok": "—", "err": "×", "warn": "·", "inactive": "◌", "active": "◉", "add": "╋", "star": "◆", "bullet": "·", "arrow": "›", "sep": "|"}
_SYM_GEOMETRIC= {"ok": "▲", "err": "▼", "warn": "◆", "inactive": "□", "active": "■", "add": "⊕", "star": "✦", "bullet": "▸", "arrow": "▶", "sep": "│"}
_SYM_CUTE     = {"ok": "💕", "err": "💔", "warn": "⚡", "inactive": "🌙", "active": "🌸", "add": "✨", "star": "🌟", "bullet": "🎀", "arrow": "→", "sep": "·"}
_SYM_NATURE   = {"ok": "🌿", "err": "🍂", "warn": "⚡", "inactive": "○", "active": "●", "add": "+", "star": "✿", "bullet": "·", "arrow": "→", "sep": "│"}
_SYM_CLASSIC  = {"ok": "✦", "err": "✘", "warn": "⚠", "inactive": "◇", "active": "◈", "add": "⊞", "star": "❋", "bullet": "▸", "arrow": "►", "sep": "║"}
_SYM_NEON     = {"ok": "▶", "err": "■", "warn": "◆", "inactive": "○", "active": "●", "add": "+", "star": "★", "bullet": "›", "arrow": "»", "sep": "|"}

# ─── Navigation icon packs ────────────────────────────────────────────────────
# "type": "material" → values are ft.Icons.* constants
# "type": "text"     → values are unicode/emoji strings

NAV_ICON_PACKS: dict[str, dict[str, Any]] = {
    "material": {
        "name": "Material", "desc": "Стандартні Material іконки", "type": "material",
        "categories": ft.Icons.CATEGORY, "products": ft.Icons.INVENTORY,
        "suppliers": ft.Icons.LOCAL_SHIPPING, "packaging": ft.Icons.ALL_INBOX,
        "copy": ft.Icons.CONTENT_COPY, "orders": ft.Icons.SHOPPING_CART,
        "payments": ft.Icons.PAYMENTS, "cost_orders": ft.Icons.CALCULATE,
        "cost_stock": ft.Icons.WAREHOUSE, "pack_stock": ft.Icons.INVENTORY_2,
        "media": ft.Icons.NEWSPAPER, "schedule": ft.Icons.EVENT_NOTE,
        "home": ft.Icons.HOME, "settings": ft.Icons.SETTINGS,
        "extensions": ft.Icons.EXTENSION, "gsm": ft.Icons.SATELLITE_ALT,
        "calculator": ft.Icons.CALCULATE, "notebook": ft.Icons.MENU_BOOK,
    },
    "outlined": {
        "name": "Контурні", "desc": "Material Outlined іконки", "type": "material",
        "categories": ft.Icons.CATEGORY_OUTLINED, "products": ft.Icons.INVENTORY_2_OUTLINED,
        "suppliers": ft.Icons.LOCAL_SHIPPING_OUTLINED, "packaging": ft.Icons.ALL_INBOX_OUTLINED,
        "copy": ft.Icons.CONTENT_COPY_OUTLINED, "orders": ft.Icons.SHOPPING_CART_OUTLINED,
        "payments": ft.Icons.PAYMENTS_OUTLINED, "cost_orders": ft.Icons.CALCULATE_OUTLINED,
        "cost_stock": ft.Icons.WAREHOUSE_OUTLINED, "pack_stock": ft.Icons.INVENTORY_OUTLINED,
        "media": ft.Icons.ARTICLE_OUTLINED, "schedule": ft.Icons.CALENDAR_TODAY_OUTLINED,
        "home": ft.Icons.HOME_OUTLINED, "settings": ft.Icons.SETTINGS_OUTLINED,
        "extensions": ft.Icons.EXTENSION_OUTLINED, "gsm": ft.Icons.SATELLITE_ALT_OUTLINED,
        "calculator": ft.Icons.CALCULATE_OUTLINED, "notebook": ft.Icons.MENU_BOOK_OUTLINED,
    },
    "business": {
        "name": "Бізнес", "desc": "Бізнес-стиль іконки", "type": "material",
        "categories": ft.Icons.GRID_VIEW, "products": ft.Icons.VIEW_LIST,
        "suppliers": ft.Icons.DELIVERY_DINING, "packaging": ft.Icons.INBOX,
        "copy": ft.Icons.COPY_ALL, "orders": ft.Icons.RECEIPT_LONG,
        "payments": ft.Icons.ACCOUNT_BALANCE, "cost_orders": ft.Icons.TRENDING_DOWN,
        "cost_stock": ft.Icons.STORAGE, "pack_stock": ft.Icons.BUSINESS_CENTER,
        "media": ft.Icons.ARTICLE, "schedule": ft.Icons.CALENDAR_MONTH,
        "home": ft.Icons.DASHBOARD, "settings": ft.Icons.TUNE,
        "extensions": ft.Icons.WIDGETS, "gsm": ft.Icons.CELL_TOWER,
        "calculator": ft.Icons.REQUEST_QUOTE, "notebook": ft.Icons.LIBRARY_BOOKS,
    },
    "emoji": {
        "name": "Емодзі", "desc": "🗂 📦 🚚 💳 📰 📅", "type": "text",
        "categories": "🗂", "products": "📦", "suppliers": "🚚", "packaging": "📥",
        "copy": "📋", "orders": "🛒", "payments": "💳", "cost_orders": "💰",
        "cost_stock": "🏭", "pack_stock": "📫", "media": "📰", "schedule": "📅",
        "home": "🏠", "settings": "⚙", "extensions": "🔧", "gsm": "📡",
        "calculator": "🧮", "notebook": "📓", "company": "🏢",
    },
    "emoji2": {
        "name": "Емодзі 2", "desc": "🌿 🎯 💎 🌅 🗓 🛰", "type": "text",
        "categories": "🌿", "products": "🌸", "suppliers": "🛻", "packaging": "🎁",
        "copy": "📑", "orders": "🎯", "payments": "💎", "cost_orders": "📊",
        "cost_stock": "🏔", "pack_stock": "🪣", "media": "🗞", "schedule": "🗓",
        "home": "🌅", "settings": "🔮", "extensions": "⚡", "gsm": "🛰",
        "calculator": "📊", "notebook": "📔", "company": "🏙",
    },
    "emoji3": {
        "name": "Емодзі 3", "desc": "📌 🛍 🏢 📈 💼 📆", "type": "text",
        "categories": "📌", "products": "🛍", "suppliers": "🏢", "packaging": "🎀",
        "copy": "🖨", "orders": "📝", "payments": "🏦", "cost_orders": "📈",
        "cost_stock": "🏗", "pack_stock": "🗃", "media": "📺", "schedule": "📆",
        "home": "💼", "settings": "🖥", "extensions": "🔌", "gsm": "📻",
        "calculator": "📈", "notebook": "🗒", "company": "🏛",
    },
}

NAV_ICON_PACK_GROUPS: list[tuple[str, list[str]]] = [
    ("Material іконки", ["material", "outlined", "business"]),
    ("Емодзі", ["emoji", "emoji2", "emoji3"]),
]

DEFAULT_NAV_PACK = "material"


def get_nav_icon(pack: dict, nav_key: str, fallback=None):
    """Return the icon/char for a nav item; fallback is used if key missing."""
    return pack.get(nav_key, fallback)


# ─── 11 Theme definitions ─────────────────────────────────────────────────────

THEMES: dict[str, dict[str, Any]] = {

    # ── Світлі ─────────────────────────────────────────────────────────────────
    "standard": {
        "name": "Стандарт", "group": "Світлі",
        "bg": "#F0F4F8", "panel": "#FFFFFF", "hdr": "#E2E8F0",
        "border": "#CBD5E1", "text": "#1E293B", "muted": "#475569",
        "accent": "#3B82F6", "accent_text": "#FFFFFF",
        "ok": "#16A34A", "ok_bg": "#DCFCE7",
        "warn": "#B45309", "warn_bg": "#FED7AA",
        "err": "#DC2626", "err_bg": "#FEE2E2",
        "soft": "#EFF6FF", "alt": "#F8FAFC",
        "chip_bg": "#DBEAFE", "chip_text": "#1E3A8A",
        "icon_color": "#1D4ED8",
        "bar": "#FFF6CC",
        "radius": 8, "font_scale": 1.0, "symbols": _SYM_STANDARD,
    },
    "arctic": {
        "name": "Арктика", "group": "Світлі",
        "bg": "#EFF6FF", "panel": "#FFFFFF", "hdr": "#DBEAFE",
        "border": "#93C5FD", "text": "#1E3A5F", "muted": "#1D4ED8",
        "accent": "#2563EB", "accent_text": "#FFFFFF",
        "ok": "#059669", "ok_bg": "#D1FAE5",
        "warn": "#D97706", "warn_bg": "#FEF3C7",
        "err": "#DC2626", "err_bg": "#FEE2E2",
        "soft": "#E0EEFF", "alt": "#F0F7FF",
        "chip_bg": "#DBEAFE", "chip_text": "#1E3A8A",
        "icon_color": "#1D4ED8",
        "bar": "#BFDBFE",
        "radius": 12, "font_scale": 1.0, "symbols": _SYM_STANDARD,
    },
    "sunny": {
        "name": "Сонячний", "group": "Світлі",
        "bg": "#FEFCE8", "panel": "#FFFFFF", "hdr": "#FEF9C3",
        "border": "#FDE047", "text": "#422006", "muted": "#713F12",
        "accent": "#CA8A04", "accent_text": "#FFFFFF",
        "ok": "#15803D", "ok_bg": "#DCFCE7",
        "warn": "#B45309", "warn_bg": "#FEF3C7",
        "err": "#B91C1C", "err_bg": "#FEE2E2",
        "soft": "#FEF9C3", "alt": "#FEFCE8",
        "chip_bg": "#FEF08A", "chip_text": "#713F12",
        "icon_color": "#854D0E",
        "bar": "#FEF08A",
        "radius": 8, "font_scale": 1.0, "symbols": _SYM_STANDARD,
    },

    # ── Темні ──────────────────────────────────────────────────────────────────
    "dark_night": {
        "name": "Темна ніч", "group": "Темні",
        "bg": "#0F172A", "panel": "#1E293B", "hdr": "#1E293B",
        "border": "#334155", "text": "#F1F5F9", "muted": "#94A3B8",
        "accent": "#60A5FA", "accent_text": "#0F172A",
        "ok": "#4ADE80", "ok_bg": "#14532D",
        "warn": "#FCD34D", "warn_bg": "#78350F",
        "err": "#F87171", "err_bg": "#7F1D1D",
        "soft": "#1E3A5F", "alt": "#172032",
        "chip_bg": "#1E3A5F", "chip_text": "#93C5FD",
        "icon_color": "#60A5FA",
        "bar": "#0F172A",
        "radius": 8, "font_scale": 1.0, "symbols": _SYM_STANDARD,
    },
    "charcoal": {
        "name": "Вугілля", "group": "Темні",
        "bg": "#18181B", "panel": "#27272A", "hdr": "#27272A",
        "border": "#3F3F46", "text": "#FAFAFA", "muted": "#A1A1AA",
        "accent": "#A78BFA", "accent_text": "#18181B",
        "ok": "#4ADE80", "ok_bg": "#14532D",
        "warn": "#FCD34D", "warn_bg": "#78350F",
        "err": "#F87171", "err_bg": "#7F1D1D",
        "soft": "#2E2740", "alt": "#1F1F22",
        "chip_bg": "#2E2740", "chip_text": "#C4B5FD",
        "icon_color": "#A78BFA",
        "bar": "#18181B",
        "radius": 6, "font_scale": 1.0, "symbols": _SYM_CLASSIC,
    },
    "ocean": {
        "name": "Океан", "group": "Темні",
        "bg": "#042F2E", "panel": "#0F4C4C", "hdr": "#0F4C4C",
        "border": "#0E7490", "text": "#ECFEFF", "muted": "#67E8F9",
        "accent": "#2DD4BF", "accent_text": "#042F2E",
        "ok": "#34D399", "ok_bg": "#064E3B",
        "warn": "#FCD34D", "warn_bg": "#78350F",
        "err": "#F87171", "err_bg": "#7F1D1D",
        "soft": "#1A5E5C", "alt": "#073535",
        "chip_bg": "#115E5C", "chip_text": "#67E8F9",
        "icon_color": "#2DD4BF",
        "bar": "#042F2E",
        "radius": 8, "font_scale": 1.0, "symbols": _SYM_NEON,
    },

    # ── Природа ────────────────────────────────────────────────────────────────
    "forest": {
        "name": "Ліс", "group": "Природа",
        "bg": "#F0FDF4", "panel": "#FFFFFF", "hdr": "#DCFCE7",
        "border": "#86EFAC", "text": "#14532D", "muted": "#166534",
        "accent": "#16A34A", "accent_text": "#FFFFFF",
        "ok": "#15803D", "ok_bg": "#DCFCE7",
        "warn": "#D97706", "warn_bg": "#FEF3C7",
        "err": "#DC2626", "err_bg": "#FEE2E2",
        "soft": "#DCFCE7", "alt": "#F0FDF4",
        "chip_bg": "#BBF7D0", "chip_text": "#14532D",
        "icon_color": "#15803D",
        "bar": "#BBF7D0",
        "radius": 10, "font_scale": 1.0, "symbols": _SYM_NATURE,
    },

    # ── Теплі ─────────────────────────────────────────────────────────────────
    "sunset": {
        "name": "Захід сонця", "group": "Теплі",
        "bg": "#FFF7ED", "panel": "#FFFFFF", "hdr": "#FFEDD5",
        "border": "#FDBA74", "text": "#431407", "muted": "#7C2D12",
        "accent": "#EA580C", "accent_text": "#FFFFFF",
        "ok": "#15803D", "ok_bg": "#DCFCE7",
        "warn": "#B45309", "warn_bg": "#FEF3C7",
        "err": "#B91C1C", "err_bg": "#FEE2E2",
        "soft": "#FED7AA", "alt": "#FFF7ED",
        "chip_bg": "#FED7AA", "chip_text": "#7C2D12",
        "icon_color": "#EA580C",
        "bar": "#FED7AA",
        "radius": 8, "font_scale": 1.0, "symbols": _SYM_STANDARD,
    },
    "coffee": {
        "name": "Кава", "group": "Теплі",
        "bg": "#FDFAF6", "panel": "#FFFFFF", "hdr": "#F5EFE6",
        "border": "#D4B896", "text": "#2D1B0A", "muted": "#78350F",
        "accent": "#92400E", "accent_text": "#FFFFFF",
        "ok": "#15803D", "ok_bg": "#DCFCE7",
        "warn": "#B45309", "warn_bg": "#FEF3C7",
        "err": "#B91C1C", "err_bg": "#FEE2E2",
        "soft": "#EAD9C6", "alt": "#F8F3EC",
        "chip_bg": "#EAD9C6", "chip_text": "#78350F",
        "icon_color": "#92400E",
        "bar": "#EAD9C6",
        "radius": 8, "font_scale": 1.0, "symbols": _SYM_STANDARD,
    },

    # ── Фіолетові ─────────────────────────────────────────────────────────────
    "lavender": {
        "name": "Лаванда", "group": "Фіолетові",
        "bg": "#FAF5FF", "panel": "#FFFFFF", "hdr": "#F3E8FF",
        "border": "#D8B4FE", "text": "#3B0764", "muted": "#6B21A8",
        "accent": "#9333EA", "accent_text": "#FFFFFF",
        "ok": "#15803D", "ok_bg": "#DCFCE7",
        "warn": "#B45309", "warn_bg": "#FEF3C7",
        "err": "#B91C1C", "err_bg": "#FEE2E2",
        "soft": "#E9D5FF", "alt": "#FAF5FF",
        "chip_bg": "#E9D5FF", "chip_text": "#6B21A8",
        "icon_color": "#9333EA",
        "bar": "#E9D5FF",
        "radius": 12, "font_scale": 1.0, "symbols": _SYM_STANDARD,
    },

    # ── Рожеві ────────────────────────────────────────────────────────────────
    "candy": {
        "name": "Цукерка", "group": "Рожеві",
        "bg": "#FFF0F6", "panel": "#FFFFFF", "hdr": "#FFE4EF",
        "border": "#F9A8D4", "text": "#500724", "muted": "#9D174D",
        "accent": "#EC4899", "accent_text": "#FFFFFF",
        "ok": "#15803D", "ok_bg": "#DCFCE7",
        "warn": "#B45309", "warn_bg": "#FEF3C7",
        "err": "#B91C1C", "err_bg": "#FEE2E2",
        "soft": "#FFD6E7", "alt": "#FFF5F9",
        "chip_bg": "#FFAECE", "chip_text": "#9D174D",
        "icon_color": "#EC4899",
        "bar": "#FFAECE",
        "radius": 14, "font_scale": 1.0, "symbols": _SYM_CUTE,
    },
}

DEFAULT_THEME = "standard"

THEME_GROUPS: list[tuple[str, list[str]]] = [
    ("Світлі",     ["standard", "arctic", "sunny"]),
    ("Темні",      ["dark_night", "charcoal", "ocean"]),
    ("Природа",    ["forest"]),
    ("Теплі",      ["sunset", "coffee"]),
    ("Фіолетові",  ["lavender"]),
    ("Рожеві",     ["candy"]),
]


def get_theme(name: str | None) -> dict:
    """Return the theme dict for *name*, falling back to standard."""
    return THEMES.get(name or DEFAULT_THEME) or THEMES[DEFAULT_THEME]


def get_bar_color(theme: dict) -> str:
    """Return the dashboard top/bottom bar color for the given theme."""
    return theme.get("bar") or theme.get("hdr", "#FFF6CC")


def _is_dark(hex_color: str) -> bool:
    try:
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return (0.299 * r + 0.587 * g + 0.114 * b) / 255 < 0.4
    except Exception:
        return False


# ─── Theme preview & card ─────────────────────────────────────────────────────

def build_theme_preview(theme: dict, width: int = 160, height: int = 100) -> ft.Container:
    """Build a mini visual preview of a theme."""
    bg      = theme["bg"]
    panel   = theme["panel"]
    hdr     = theme["hdr"]
    border  = theme["border"]
    text    = theme["text"]
    muted   = theme["muted"]
    accent  = theme["accent"]
    a_text  = theme.get("accent_text", "#FFFFFF")
    soft    = theme["soft"]
    alt     = theme["alt"]
    chip_bg = theme["chip_bg"]
    chip_tx = theme["chip_text"]
    err     = theme["err"]
    bar     = theme.get("bar", hdr)
    radius  = min(theme.get("radius", 8), 10)

    def _r(w, h, color, opacity=1.0):
        return ft.Container(width=w, height=h, bgcolor=color, border_radius=2, opacity=opacity)

    return ft.Container(
        width=width, height=height,
        border_radius=radius,
        bgcolor=bg,
        border=ft.border.all(1, border),
        clip_behavior=ft.ClipBehavior.HARD_EDGE,
        content=ft.Column(
            spacing=0, tight=True,
            controls=[
                ft.Container(
                    bgcolor=bar, height=20,
                    padding=ft.padding.symmetric(horizontal=6, vertical=4),
                    content=ft.Row(
                        [
                            ft.Container(width=8, height=8, bgcolor=accent, border_radius=4),
                            _r(36, 5, text, 0.65),
                            ft.Row(expand=True),
                            ft.Container(
                                width=28, height=12, bgcolor=chip_bg, border_radius=3,
                                content=ft.Text("ЮІ", size=7, color=chip_tx, text_align=ft.TextAlign.CENTER),
                                alignment=ft.Alignment(0, 0),
                            ),
                        ],
                        spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ),
                ft.Container(
                    bgcolor=panel, height=18,
                    padding=ft.padding.symmetric(horizontal=6, vertical=5),
                    content=ft.Row(
                        [_r(44, 5, text, 0.7), ft.Row(expand=True), _r(20, 5, muted, 0.5)],
                        spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ),
                ft.Container(
                    bgcolor=alt, height=18,
                    padding=ft.padding.symmetric(horizontal=6, vertical=5),
                    content=ft.Row(
                        [_r(32, 5, muted, 0.6), ft.Row(expand=True), _r(24, 5, muted, 0.35)],
                        spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ),
                ft.Container(
                    bgcolor=soft, height=18,
                    padding=ft.padding.symmetric(horizontal=6, vertical=5),
                    content=ft.Row(
                        [_r(38, 5, text, 0.6), ft.Row(expand=True),
                         ft.Container(width=7, height=7, bgcolor=err, border_radius=4)],
                        spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ),
                ft.Container(
                    bgcolor=bg, height=20,
                    padding=ft.padding.symmetric(horizontal=6, vertical=3),
                    content=ft.Row(
                        [
                            ft.Container(
                                width=46, height=14, bgcolor=accent, border_radius=4,
                                content=ft.Text("Зберегти", size=7, color=a_text,
                                                text_align=ft.TextAlign.CENTER, no_wrap=True),
                                alignment=ft.Alignment(0, 0),
                            ),
                            ft.Row(expand=True),
                            _r(28, 5, muted, 0.35),
                        ],
                        spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ),
            ],
        ),
    )


def build_theme_card(
    theme_key: str,
    theme: dict,
    is_selected: bool,
    on_select,
    current_theme: dict | None = None,
) -> ft.Container:
    _ct = current_theme or {}
    _label_color = _ct.get("text", "#111827")
    _muted_color = _ct.get("muted", "#6B7280")
    _card_bg = _ct.get("soft", "#EFF6FF") if is_selected else _ct.get("panel", "#F8FAFC")
    selected_border = ft.border.all(2, _ct.get("accent", "#2563EB")) if is_selected else ft.border.all(1, _ct.get("border", "#CBD5E1"))
    check = ft.Container(
        visible=is_selected,
        width=20, height=20,
        bgcolor=_ct.get("accent", "#2563EB"), border_radius=10, alignment=ft.Alignment(0, 0),
        content=ft.Text("✓", size=11, color=_ct.get("accent_text", "#FFFFFF"), weight=ft.FontWeight.BOLD),
    )
    return ft.Container(
        width=170, border_radius=10, border=selected_border,
        bgcolor=_card_bg,
        shadow=ft.BoxShadow(blur_radius=6, color="#1E293B18") if is_selected else None,
        on_click=lambda _e, k=theme_key: on_select(k),
        content=ft.Column(
            spacing=6,
            controls=[
                ft.Stack(
                    [
                        build_theme_preview(theme, width=166, height=96),
                        ft.Container(right=6, top=6, content=check),
                    ],
                    width=166, height=96,
                ),
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=6),
                    content=ft.Column(
                        spacing=2,
                        controls=[
                            ft.Text(theme["name"], size=12, weight=ft.FontWeight.W_600,
                                    color=_label_color, no_wrap=True),
                            ft.Text(theme["group"], size=10, color=_muted_color),
                        ],
                    ),
                ),
            ],
        ),
        padding=ft.padding.only(left=2, right=2, top=2, bottom=8),
    )


# ─── Nav icon pack preview & card ─────────────────────────────────────────────

def build_nav_pack_preview(
    pack: dict,
    width: int = 160,
    height: int = 96,
    current_theme: dict | None = None,
) -> ft.Container:
    """Mini preview showing 6 navigation icons/symbols from the pack."""
    _ct = current_theme or {}
    _icon_clr = _ct.get("icon_color", "#374151")
    _panel_clr = _ct.get("panel", "#F9FAFB")
    _border_clr = _ct.get("border", "#E5E7EB")
    _muted_clr = _ct.get("muted", "#9CA3AF")
    pack_type = pack.get("type", "text")
    preview_keys = ["home", "orders", "payments", "media", "schedule", "products"]
    items = []
    for key in preview_keys:
        val = pack.get(key)
        if val is None:
            continue
        if pack_type == "material":
            items.append(ft.Icon(val, size=18, color=_icon_clr))
        else:
            items.append(ft.Text(str(val), size=18, color=_icon_clr))

    rows = [items[:3], items[3:6]]
    row_widgets = [
        ft.Row(row, spacing=10, alignment=ft.MainAxisAlignment.CENTER)
        for row in rows if row
    ]

    return ft.Container(
        width=width, height=height,
        border_radius=8, bgcolor=_panel_clr,
        border=ft.border.all(1, _border_clr),
        clip_behavior=ft.ClipBehavior.HARD_EDGE,
        alignment=ft.Alignment(0, 0),
        content=ft.Column(
            row_widgets + [
                ft.Text(pack.get("desc", ""), size=9, color=_muted_clr,
                        text_align=ft.TextAlign.CENTER, no_wrap=True),
            ],
            spacing=6,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
            tight=True,
        ),
    )


def build_nav_pack_card(
    pack_key: str,
    pack: dict,
    is_selected: bool,
    on_select,
    current_theme: dict | None = None,
) -> ft.Container:
    _ct = current_theme or {}
    _label_color = _ct.get("text", "#111827")
    _muted_color = _ct.get("muted", "#6B7280")
    _card_bg = _ct.get("soft", "#EFF6FF") if is_selected else _ct.get("panel", "#F8FAFC")
    selected_border = ft.border.all(2, _ct.get("accent", "#2563EB")) if is_selected else ft.border.all(1, _ct.get("border", "#CBD5E1"))
    check = ft.Container(
        visible=is_selected, width=20, height=20,
        bgcolor=_ct.get("accent", "#2563EB"), border_radius=10, alignment=ft.Alignment(0, 0),
        content=ft.Text("✓", size=11, color=_ct.get("accent_text", "#FFFFFF"), weight=ft.FontWeight.BOLD),
    )
    return ft.Container(
        width=170, border_radius=10, border=selected_border,
        bgcolor=_card_bg,
        shadow=ft.BoxShadow(blur_radius=6, color="#1E293B18") if is_selected else None,
        on_click=lambda _e, k=pack_key: on_select(k),
        content=ft.Column(
            spacing=6,
            controls=[
                ft.Stack(
                    [
                        build_nav_pack_preview(pack, width=166, height=96, current_theme=_ct),
                        ft.Container(right=6, top=6, content=check),
                    ],
                    width=166, height=96,
                ),
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=6),
                    content=ft.Column(
                        spacing=2,
                        controls=[
                            ft.Text(pack["name"], size=12, weight=ft.FontWeight.W_600,
                                    color=_label_color, no_wrap=True),
                            ft.Text(pack.get("desc", ""), size=10, color=_muted_color),
                        ],
                    ),
                ),
            ],
        ),
        padding=ft.padding.only(left=2, right=2, top=2, bottom=8),
    )


# ─── Shared appearance section (admin / superadmin settings) ─────────────────

def build_admin_appearance_section(
    db,
    manager_id: str,
    on_theme_changed=None,
    on_nav_changed=None,
) -> ft.Container:
    """Reusable theme + nav-pack picker for admin / superadmin settings panels."""
    _t = i18n.bind(str(manager_id or ""))

    def _load() -> tuple:
        try:
            payload = db.get_manager_workspace_state(manager_id) or {}
            return (
                str(payload.get("ui_theme") or DEFAULT_THEME),
                str(payload.get("ui_nav_pack") or DEFAULT_NAV_PACK),
            )
        except Exception:
            return DEFAULT_THEME, DEFAULT_NAV_PACK

    def _save_theme(key: str) -> None:
        try:
            payload = db.get_manager_workspace_state(manager_id) or {}
            if not isinstance(payload, dict):
                payload = {}
            payload["ui_theme"] = key
            db.save_manager_workspace_state(manager_id, payload)
        except Exception:
            pass

    def _save_nav(key: str) -> None:
        try:
            payload = db.get_manager_workspace_state(manager_id) or {}
            if not isinstance(payload, dict):
                payload = {}
            payload["ui_nav_pack"] = key
            db.save_manager_workspace_state(manager_id, payload)
        except Exception:
            pass

    cur_theme, cur_nav = _load()
    theme_col = ft.Column(spacing=0)
    nav_col = ft.Column(spacing=0)

    def _get_cur_theme_dict():
        return get_theme(_active_themes.get(manager_id) or cur_theme)

    def _refresh_theme(selected_key: str):
        _save_theme(selected_key)
        set_active_theme(manager_id, selected_key)
        _ct = _get_cur_theme_dict()
        theme_col.controls.clear()
        for group_name, keys in THEME_GROUPS:
            theme_col.controls.append(
                ft.Container(
                    padding=ft.padding.only(top=10, bottom=4),
                    content=ft.Text(group_name, size=11, weight=ft.FontWeight.W_600,
                                    color=_ct.get("muted", "#6B7280")),
                )
            )
            row_items = []
            for k in keys:
                t = THEMES.get(k)
                if t:
                    row_items.append(build_theme_card(k, t, k == selected_key, _refresh_theme, current_theme=_ct))
            theme_col.controls.append(ft.Row(row_items, spacing=8, wrap=True))
        import contextlib as _cl
        with _cl.suppress(Exception):
            theme_col.update()
        if callable(on_theme_changed):
            try:
                on_theme_changed(selected_key)
            except Exception:
                pass

    def _refresh_nav(selected_key: str):
        _save_nav(selected_key)
        set_active_nav_pack(manager_id, selected_key)
        _ct = _get_cur_theme_dict()
        nav_col.controls.clear()
        for group_name, keys in NAV_ICON_PACK_GROUPS:
            nav_col.controls.append(
                ft.Container(
                    padding=ft.padding.only(top=10, bottom=4),
                    content=ft.Text(group_name, size=11, weight=ft.FontWeight.W_600,
                                    color=_ct.get("muted", "#6B7280")),
                )
            )
            row_items = []
            for k in keys:
                p = NAV_ICON_PACKS.get(k)
                if p:
                    row_items.append(build_nav_pack_card(k, p, k == selected_key, _refresh_nav, current_theme=_ct))
            nav_col.controls.append(ft.Row(row_items, spacing=8, wrap=True))
        import contextlib as _cl
        with _cl.suppress(Exception):
            nav_col.update()
        if callable(on_nav_changed):
            try:
                on_nav_changed(selected_key)
            except Exception:
                pass

    _refresh_theme(cur_theme)
    _refresh_nav(cur_nav)

    _ct_init = _get_cur_theme_dict()
    return ft.Container(
        content=ft.Column(
            [
                ft.Text(_t("themes.appearance_section", "Зовнішній вигляд"), size=15, weight=ft.FontWeight.W_700),
                ft.Divider(height=1, color=_ct_init.get("border", "#E5E7EB")),
                ft.Text(_t("themes.theme_section", "Тема інтерфейсу"), size=12, weight=ft.FontWeight.W_600,
                        color=_ct_init.get("text", "#374151")),
                ft.Text(
                    _t("themes.theme_hint", "Застосовується одразу до всіх вкладок і кнопок."),
                    size=11, color=_ct_init.get("muted", "#6B7280"),
                ),
                theme_col,
                ft.Divider(height=1, color=_ct_init.get("border", "#E5E7EB")),
                ft.Text(_t("themes.nav_pack_section", "Пакет іконок навігації"), size=12, weight=ft.FontWeight.W_600,
                        color=_ct_init.get("text", "#374151")),
                ft.Text(
                    _t("themes.nav_pack_hint", "Замінює значки біля назв сторінок в меню."),
                    size=11, color=_ct_init.get("muted", "#6B7280"),
                ),
                nav_col,
            ],
            spacing=10,
        ),
        padding=ft.padding.symmetric(vertical=4),
    )
