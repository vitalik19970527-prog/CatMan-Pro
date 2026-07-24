"""
Shared currency-conversion helpers for cost-price calculations.

Consolidates what used to be four near-identical live-NBU-rate converters
(cost_price_stock_page.py, cost_price_orders_table.py, orders_view.py) plus
a static-table outlier (products_view.py) into one implementation, and adds
the pivot-currency rule used to decide when a cost price can be computed in
a product's native currency instead of always going through USD:

- if every currency involved for a product (purchase + sale tiers) is the
  same single currency, use that currency directly (no conversion, no FX
  rate dependency);
- if two or more distinct currencies are involved, fall back to USD.

Rates are fixed for the calendar day: fetched once (on the first call of the
day, from any process on this machine — the result is persisted to a local
file so later app launches the same day reuse it instantly instead of
hitting the network again) and never refreshed again until the date changes.
This app only ever prices in USD/EUR/PLN/UAH/CNY, so one settled rate per
currency per day is what "today's course" means to the business anyway —
there is no benefit to re-polling NBU mid-day, only latency and rate-limit
risk.

Locking rule (do not break this): _state_lock guards ONLY the in-memory
_uah_rates_cache/_uah_rates_date/_refresh_in_flight variables and is held
for microseconds at a time — never across network I/O or disk I/O. convert()
is called from nearly every view's synchronous build path (often inside
asyncio.to_thread), so anything it can reach must never block on a lock that
a slow/hanging network fetch elsewhere might be holding. A prior version of
this module held _refresh_lock across the whole fetch_uah_rates() call,
which meant one hung NBU request (e.g. DNS resolution stuck on a broken
network — requests' timeout= does not reliably bound DNS lookups) froze
every other screen in the app that touched currency conversion.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date
from typing import Iterable, Optional

_uah_rates_cache: dict = {}
_uah_rates_date: str = ""  # ISO date (YYYY-MM-DD) the cache was fetched for
_state_lock = threading.Lock()
_refresh_in_flight = False
_CACHE_FILE_NAME = "fx_rates_cache.json"


def _safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def today_str() -> str:
    return date.today().isoformat()


def set_rates(rates: dict, day: str | None = None) -> None:
    """Inject externally-sourced rates directly into the module cache —
    e.g. rates already fetched today by another user of this VPS company
    and read back from the shared company DB. Skips both the network fetch
    and the local same-day-file lookup that ensure_rates_loaded() would
    otherwise do; still writes through to the local file so this machine
    also has an offline fallback for the rest of the day."""
    global _uah_rates_cache, _uah_rates_date
    if not isinstance(rates, dict) or not rates:
        return
    day = day or today_str()
    parsed = {
        str(k).upper().strip(): v
        for k, v in rates.items()
        if _safe_float(v, None) is not None
    }
    if not parsed:
        return
    with _state_lock:
        _uah_rates_cache = parsed
        _uah_rates_date = day
    _save_cache_to_disk(parsed, day)


def _cache_file_path() -> str:
    base = os.getenv("FLET_APP_STORAGE_DATA") or os.path.join(
        os.path.expanduser("~"), ".catmanpro"
    )
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        pass
    return os.path.join(base, _CACHE_FILE_NAME)


def _load_cache_from_disk() -> None:
    """Picks up a same-day fetch already done by an earlier launch of the
    app today (this process's in-memory cache always starts empty). The
    file read itself needs no lock (nothing else in this module writes to
    the file); only the resulting module-state update briefly takes one."""
    global _uah_rates_cache, _uah_rates_date
    try:
        with open(_cache_file_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        rates = data.get("rates") if isinstance(data, dict) else None
        day = str((data or {}).get("date") or "") if isinstance(data, dict) else ""
        if isinstance(rates, dict) and day:
            parsed = {
                str(k).upper().strip(): v
                for k, v in rates.items()
                if _safe_float(v, None) is not None
            }
            if parsed:
                with _state_lock:
                    _uah_rates_cache = parsed
                    _uah_rates_date = day
    except Exception:
        pass


def _save_cache_to_disk(rates: dict, day: str) -> None:
    try:
        with open(_cache_file_path(), "w", encoding="utf-8") as f:
            json.dump({"date": day, "rates": rates}, f)
    except Exception:
        pass


def fetch_uah_rates() -> dict:
    """Live 'X per 1 UAH' rates from NBU, falling back to open.er-api, then
    to a third keyless CDN-hosted source in case both are unreachable
    (corporate firewall, ISP block, etc). Never call this while holding
    _state_lock — it can take a long time (or, on a broken network, far
    longer than its nominal per-request timeout) and must never block any
    other caller."""
    try:
        import requests as _requests
    except Exception:
        return {}

    urls = [
        "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json",
        "https://open.er-api.com/v6/latest/UAH",
        "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/uah.json",
    ]

    for idx, url in enumerate(urls):
        try:
            resp = _requests.get(url, timeout=6)
            resp.raise_for_status()
            data = resp.json()
            out = {"UAH": 1.0}
            if idx == 0 and isinstance(data, list):
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    cc = str(row.get("cc") or "").upper().strip()
                    rate = _safe_float(row.get("rate"), None)
                    unit = _safe_float(row.get("unit"), 1.0) or 1.0
                    if cc and rate and rate > 0 and unit > 0:
                        out[cc] = float(rate) / float(unit)
                if len(out) > 1:
                    return out
            if idx == 1 and isinstance(data, dict):
                rates = data.get("rates") if isinstance(data.get("rates"), dict) else {}
                for cc, v in rates.items():
                    val = _safe_float(v, None)
                    cc_u = str(cc or "").upper().strip()
                    if cc_u and cc_u != "UAH" and val and val > 0:
                        out[cc_u] = 1.0 / float(val)
                if len(out) > 1:
                    return out
            if idx == 2 and isinstance(data, dict):
                rates = data.get("uah") if isinstance(data.get("uah"), dict) else {}
                for cc, v in rates.items():
                    val = _safe_float(v, None)
                    cc_u = str(cc or "").upper().strip()
                    if cc_u and cc_u != "UAH" and val and val > 0:
                        out[cc_u] = 1.0 / float(val)
                if len(out) > 1:
                    return out
        except Exception:
            continue
    return {}


def has_today() -> bool:
    """Cheap, local-only check (memory, then the same-day disk file — never
    the network or a company DB) for whether today's rates are already
    available. Lets a caller decide whether it's worth checking a shared
    company-DB copy before falling back to ensure_rates_loaded()'s network
    fetch."""
    with _state_lock:
        if _uah_rates_cache and _uah_rates_date == today_str():
            return True
    _load_cache_from_disk()
    with _state_lock:
        return bool(_uah_rates_cache) and _uah_rates_date == today_str()


def ensure_rates_loaded() -> dict:
    """Blocking call that guarantees today's rates are in the cache before
    returning — call this once, off the UI thread, at app/view startup.

    No-op (instant) if today's rates are already in memory. Otherwise tries
    the local same-day file first (another launch may have fetched already
    today), and only hits the network as a last resort — at most once per
    calendar day, across every process on this machine. The network fetch
    itself is never done while holding _state_lock (see module docstring):
    a rare concurrent double-fetch (two threads both missing the cache at
    once) is a harmless, self-correcting waste of one extra HTTP round
    trip — infinitely better than every other screen's currency conversion
    blocking on a lock held by a hung request."""
    global _uah_rates_cache, _uah_rates_date
    today = today_str()
    with _state_lock:
        if _uah_rates_cache and _uah_rates_date == today:
            return dict(_uah_rates_cache)
    _load_cache_from_disk()
    with _state_lock:
        if _uah_rates_cache and _uah_rates_date == today:
            return dict(_uah_rates_cache)
    fresh = fetch_uah_rates()
    if fresh:
        with _state_lock:
            _uah_rates_cache = fresh
            _uah_rates_date = today
        _save_cache_to_disk(fresh, today)
    with _state_lock:
        return dict(_uah_rates_cache)


def _kick_background_refresh() -> None:
    global _refresh_in_flight
    with _state_lock:
        if _refresh_in_flight:
            return
        _refresh_in_flight = True

    def _run():
        global _refresh_in_flight
        try:
            ensure_rates_loaded()
        finally:
            with _state_lock:
                _refresh_in_flight = False

    threading.Thread(target=_run, daemon=True).start()


def get_uah_rates(force_refresh: bool = False) -> dict:
    """Cache-only read — NEVER performs blocking network I/O on the calling
    thread, and never waits on a lock that a network fetch could be holding
    (see module docstring). Safety net for callers that run before any
    view's startup code has called `ensure_rates_loaded()`: if today's
    rates aren't in memory yet, this returns whatever's cached right now
    (possibly `{}`, or yesterday's rates until refreshed) and kicks a
    background fetch. Once a rate for today is cached, it is fixed for the
    rest of the day; `force_refresh=True` bypasses that to force an
    immediate re-fetch (background, still non-blocking here).
    """
    global _uah_rates_date
    today = today_str()
    with _state_lock:
        cache_empty = not _uah_rates_cache
    if force_refresh:
        with _state_lock:
            _uah_rates_date = ""
    elif cache_empty:
        _load_cache_from_disk()
    with _state_lock:
        is_stale = force_refresh or _uah_rates_date != today
        snapshot = dict(_uah_rates_cache)
    if is_stale:
        _kick_background_refresh()
    return snapshot


def convert(amount: Optional[float], from_ccy: str, to_ccy: str) -> Optional[float]:
    if amount is None:
        return None
    src = str(from_ccy or "USD").upper().strip() or "USD"
    dst = str(to_ccy or "USD").upper().strip() or "USD"
    if src == dst:
        return float(amount)
    rates = get_uah_rates() or {}

    def _uah_per_unit(ccy: str) -> Optional[float]:
        if ccy == "UAH":
            return 1.0
        return _safe_float(rates.get(ccy), None)

    src_uah = _uah_per_unit(src)
    dst_uah = _uah_per_unit(dst)
    if not src_uah or not dst_uah:
        return None
    return float(amount) * (float(src_uah) / float(dst_uah))


def resolve_pivot_currency(currencies: Iterable[Optional[str]]) -> str:
    """Cost-price pivot-currency rule: if every given currency is the same
    single currency, use it as-is; otherwise (mixed, or empty) fall back to
    USD. Re-run this fresh on every recalculation — it is not meant to be
    cached, so a later currency change is picked up automatically."""
    normalized = {
        str(c).upper().strip()
        for c in currencies
        if c is not None and str(c).strip()
    }
    if len(normalized) == 1:
        return next(iter(normalized))
    return "USD"
