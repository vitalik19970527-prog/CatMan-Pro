import re
from typing import Any

_CURRENCY_PREFIXES = ("$", "€", "¥", "₴")
_CURRENCY_SUFFIX_RE = re.compile(r"^\s*([+-]?[\d\s'.,]+)\s*([A-Za-z$€¥₴%]{1,8})\s*$")
_DATE_LIKE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CURRENCY_NON_ALPHA = frozenset("$€¥₴")


def normalize_numeric_input(raw: Any) -> str:
    if raw is None:
        return ""
    s = str(raw).strip()
    if s == "":
        return ""
    for token in ("USD", "UAH", "EUR", "PLN", "CNY", "zł"):
        s = s.replace(token, "")
    for sym in _CURRENCY_PREFIXES:
        s = s.replace(sym, "")
    s = s.replace("\u00a0", " ")
    s = s.replace("'", "")
    s = s.replace(" ", "")
    s = s.replace(",", ".")
    return s.strip()


def safe_float(raw: Any, default: float | None = None) -> float | None:
    try:
        s = normalize_numeric_input(raw)
        if s == "":
            return default
        return float(s)
    except Exception:
        return default


def format_grouped(
    value: Any, decimals: int | None = None, trim: bool = True
) -> str:
    n = safe_float(value, None)
    if n is None:
        return ""

    if decimals is None:
        src = "" if value is None else str(value)
        if "." in src:
            decimals = len(src.rsplit(".", 1)[1])
        elif "," in src:
            decimals = len(src.rsplit(",", 1)[1])
        else:
            decimals = 0 if abs(n - int(n)) < 1e-9 else 2
        decimals = max(0, min(6, int(decimals)))

    # Format with comma as thousands separator, then replace with apostrophe
    out = f"{n:,.{decimals}f}".replace(",", "'")
    # Replace decimal point dot with comma for European format (6'000,00)
    if "." in out:
        integer_part, decimal_part = out.rsplit(".", 1)
        out = f"{integer_part},{decimal_part}"
    if trim and "," in out:
        out = out.rstrip("0").rstrip(",")
    return out


def row_text_matches(query: Any, *values: Any) -> bool:
    """Case-insensitive substring match against the display form of each value.
    Numeric values are matched against their grouped/formatted text (e.g. '42'
    matches 4'020) so partial-digit search behaves like matching rendered cells.
    An empty query matches everything (used to no-op a cleared filter)."""
    q = str(query or "").strip().lower()
    if not q:
        return True
    for v in values:
        if v is None or v == "":
            continue
        s = format_grouped(v) if isinstance(v, (int, float)) else str(v)
        if q in s.lower():
            return True
    return False


def format_maybe_numeric_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return format_grouped(value)

    s = str(value)
    trimmed = s.strip()
    if trimmed == "":
        return ""
    if _DATE_LIKE_RE.match(trimmed):
        return s

    if any(ch.isalpha() or ch in _CURRENCY_NON_ALPHA for ch in trimmed):
        m = _CURRENCY_SUFFIX_RE.match(trimmed)
        if m:
            num_part = m.group(1)
            suffix = m.group(2)
            num_fmt = format_grouped(num_part)
            return f"{num_fmt} {suffix}" if num_fmt else s
        return s

    if trimmed.startswith("#"):
        return s

    num_fmt = format_grouped(trimmed)
    return num_fmt if num_fmt else s
