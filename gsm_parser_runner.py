import asyncio
import contextlib
import importlib.util
import json
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from database import DataManager
from logger import logger

SOURCE_GSM = "GSM Arena"
SOURCE_EK = "E-Katalog"
SOURCE_XIAOMI_REGIONS = "Xiaomi Regions"
_GLOBAL_COMPANY_ID = "__global__"

# Path where the last parse run log is stored (read by the UI "Лог" tab)
LAST_PARSE_LOG_PATH = Path(__file__).resolve().parent / "gsm_last_parse.log"

_PARSE_SESSION_ID = str(uuid.uuid4())[:8]
_LEGACY_GSM_MODULE = None
_LEGACY_EK_MODULE = None


def _legacy_root() -> Path:
    return Path(__file__).resolve().parent / "GSM_Parser"


def _load_module_from_file(name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(name, str(file_path))
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot load module from {file_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _legacy_gsm():
    global _LEGACY_GSM_MODULE
    if _LEGACY_GSM_MODULE is None:
        _LEGACY_GSM_MODULE = _load_module_from_file(
            "legacy_gsm_main", _legacy_root() / "main.py"
        )
    return _LEGACY_GSM_MODULE


def _legacy_ek():
    global _LEGACY_EK_MODULE
    if _LEGACY_EK_MODULE is None:
        _LEGACY_EK_MODULE = _load_module_from_file(
            "legacy_ekatalog", _legacy_root() / "ekatalog.py"
        )
    return _LEGACY_EK_MODULE


def _normalize_model_name(name: str, rules: dict) -> str:
    txt = str(name or "").strip()
    if not txt:
        return ""

    if bool(rules.get("remove_4g5g", False)):
        txt = re.sub(r"\b(4G|5G|LTE)\b", " ", txt, flags=re.IGNORECASE)

    if bool(rules.get("remove_punctuation", False)):
        # Keep "+" — it's a meaningful suffix in device names (e.g. "Pro+" vs "Pro").
        txt = re.sub(r"[^\w\s+]", " ", txt)

    for word in rules.get("exclude_words") or []:
        word = str(word or "").strip()
        if not word:
            continue
        txt = re.sub(rf"\b{re.escape(word)}\b", " ", txt, flags=re.IGNORECASE)

    for rule in rules.get("custom_rules") or []:
        if not isinstance(rule, dict) or not bool(rule.get("enabled", True)):
            continue
        pattern = str(rule.get("pattern") or "")
        repl = str(rule.get("replacement") or "")
        if not pattern:
            continue
        with contextlib.suppress(Exception):
            txt = re.sub(pattern, repl, txt, flags=re.IGNORECASE)

    txt = re.sub(r"\s+", " ", txt).strip()
    if bool(rules.get("case_insensitive", True)):
        txt = txt.lower()
    return txt


def _name_similarity(a: str, b: str, rules: dict) -> float:
    na = _normalize_model_name(a, rules)
    nb = _normalize_model_name(b, rules)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    if na in nb or nb in na:
        return 86.0

    wa = set(na.split())
    wb = set(nb.split())
    if not wa or not wb:
        return 0.0
    return (len(wa & wb) / len(wa | wb)) * 100.0


def _ensure_db() -> DataManager:
    db = DataManager()
    with db._general_pg().cursor() as cur:
        cur.execute("SELECT 1")
    return db


def _sleep_between(
    delays: dict, min_key: str, max_key: str, fallback_min: float, fallback_max: float
):
    try:
        low = float(delays.get(min_key, fallback_min))
        high = float(delays.get(max_key, fallback_max))
        if high < low:
            low, high = high, low
        time.sleep(random.uniform(max(0.0, low), max(0.0, high)))
    except Exception:
        time.sleep(random.uniform(fallback_min, fallback_max))


def _fetch_one(
    cur,
    source: str,
    model_key: str,
) -> Optional[dict]:
    cur.execute(
        """
        SELECT id, source, brand, device_type, model_name, model_key, source_url, image_url, specs, created_at
        FROM public.gsm_parser_devices
        WHERE source=%s AND model_key=%s
        LIMIT 1
        """,
        (source, model_key),
    )
    row = cur.fetchone()
    return row if isinstance(row, dict) else None


def _fetch_one_ek_by_url(cur, source_url: str) -> Optional[dict]:
    """Same E-Katalog product page must always map to the same DB row,
    regardless of which GSM Arena device it's currently linked to. Matching
    by model_key alone let a re-match (auto re-match after GSM Arena
    reshuffles its own ids, OR a manual "Посилання ЄК" pin) create a brand
    new row instead of updating the existing one — leaving the old row
    behind with a now-dangling gsm_ref_device_id, which then shows up as a
    duplicate "standalone, no GSM data" entry in the results table."""
    cur.execute(
        """
        SELECT id, source, brand, device_type, model_name, model_key, source_url, image_url, specs, created_at
        FROM public.gsm_parser_devices
        WHERE source=%s AND source_url=%s
        LIMIT 1
        """,
        (SOURCE_EK, source_url),
    )
    row = cur.fetchone()
    return row if isinstance(row, dict) else None


def _link_new_gsm_device_to_standalone_ek(
    db: DataManager,
    gsm_device_id: str,
    brand: str,
    device_type: str,
    model_name: str,
    rules: Any,
) -> None:
    """A brand-new GSM Arena device may be the very same real-world phone as
    an existing STANDALONE E-Katalog row — one stored with no GSM match at
    all because GSM Arena hadn't listed it yet (see the leftover-candidates
    pass in run_gsm_parser). If a name match is found here, link them via
    the same specs["gsm_ref_device_id"]/["gsm_ref_model_key"] fields every
    other GSM<->EK link already uses (get_gsm_merged_results, comparisons,
    the results table's GSM/EK column pairing — all of it keys off this
    field), instead of leaving two permanently-separate rows for one
    device. Best-effort: any failure here must never break the GSM Arena
    insert it's called right after."""
    try:
        legacy_ek = _legacy_ek()
        with db._general_pg().cursor() as cur:
            cur.execute(
                """
                SELECT id, model_name, specs
                FROM public.gsm_parser_devices
                WHERE source=%s AND brand=%s AND device_type=%s
                  AND (specs->>'gsm_ref_device_id' IS NULL OR specs->>'gsm_ref_device_id' = '')
                """,
                (SOURCE_EK, brand, device_type),
            )
            candidates = cur.fetchall() or []
            for row in candidates:
                ek_name = str(row.get("model_name") or "")
                if not ek_name or not _ek_name_matches(legacy_ek, model_name, ek_name):
                    continue
                specs = row.get("specs") if isinstance(row.get("specs"), dict) else {}
                specs = dict(specs)
                specs["gsm_ref_device_id"] = gsm_device_id
                specs["gsm_ref_model_key"] = _normalize_model_name(model_name, rules)
                cur.execute(
                    "UPDATE public.gsm_parser_devices SET specs=%s::jsonb, updated_at=%s WHERE id=%s",
                    (json.dumps(specs, ensure_ascii=False), datetime.now(), row.get("id")),
                )
                db._general_pg().commit()
                logger.info(
                    "[gsm-ek-link] linked new GSM device %s (%s) to standalone EK row %s",
                    gsm_device_id, model_name, row.get("id"),
                )
                return  # link to the first/best match only
    except Exception:
        logger.exception(
            "[gsm-ek-link] failed to link new GSM device %s (%s) to a standalone EK row",
            gsm_device_id, model_name,
        )


def _log_change(
    cur,
    device_id: str,
    source: str,
    field_key: str,
    old_value: Any,
    new_value: Any,
    meta: Optional[dict] = None,
):
    cur.execute(
        """
        INSERT INTO public.gsm_parser_change_history
          (id, device_id, source, field_key, old_value, new_value, changed_at, run_id, meta)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            f"chg_{uuid.uuid4().hex}",
            device_id,
            source,
            field_key,
            None if old_value is None else str(old_value),
            None if new_value is None else str(new_value),
            datetime.now(),
            _PARSE_SESSION_ID,
            json.dumps(meta or {}, ensure_ascii=False),
        ),
    )


def _record_daily_history(db: DataManager, device_id: str, source: str, specs: dict):
    """Record daily E-Katalog price/shops snapshot (feeds the price+shops chart).
    GSM Arena prices are no longer charted, so nothing is recorded for that source."""
    if source != SOURCE_EK:
        return
    fields = {
        "price_ek_min": str(specs.get("price_min") or "").strip(),
        "price_ek_max": str(specs.get("price_max") or "").strip(),
        "shops_count": specs.get("shops_count"),
    }
    with contextlib.suppress(Exception):
        db.record_gsm_daily_history(device_id, fields)


def fetch_and_apply_manual_ek_url(
    db: DataManager, gsm_id: str, url: str
) -> tuple[bool, str, str]:
    """Immediately fetch a superadmin-pinned E-Katalog URL for one GSM Arena
    device and save it, instead of waiting for the next scheduled parser
    run to pick it up. Trusts the URL completely — no name/type matching
    or comparison against the GSM Arena device; the whole point of pinning
    a URL by hand is to bypass that, same as the parser's own "UPDATE MODE"
    re-fetch of an existing/pinned URL (see the main run loop above).

    Returns (ok, message_or_ek_name, ek_device_id)."""
    url = str(url or "").strip()
    if not url:
        return False, "Порожнє посилання", ""

    row = db.get_gsm_device_by_id(gsm_id)
    if not row or str(row.get("source") or "") != SOURCE_GSM:
        return False, "Пристрій GSM Arena не знайдено", ""

    brand = str(row.get("brand") or "").strip()
    model_name = str(row.get("model_name") or "").strip()
    gsm_specs = row.get("specs") if isinstance(row.get("specs"), dict) else {}
    dev_type_label, _ = _type_to_cat(str(row.get("device_type") or ""))
    target_net = _extract_target_net(gsm_specs)

    legacy_ek = _legacy_ek()
    try:
        parsed = legacy_ek.parse_ekatalog_item(
            url, target_net, dev_type_label, skip_net_check=True
        )
    except Exception as exc:
        return False, f"Помилка завантаження сторінки: {exc}", ""
    if not parsed or "fail_net" in parsed:
        return False, "Не вдалося завантажити сторінку ЄК (мережа/недоступно)", ""

    ek_specs = {
        "target_net": target_net,
        "date_added": str(parsed.get("Y") or ""),
        "dimensions": str(parsed.get("Z") or ""),
        "length": str(parsed.get("AA") or ""),
        "width": str(parsed.get("AB") or ""),
        "thickness": str(parsed.get("AC") or ""),
        "weight": str(parsed.get("AD") or ""),
        "display": str(parsed.get("AE") or ""),
        "os": str(parsed.get("AF") or ""),
        "cpu": str(parsed.get("AG") or ""),
        "memory": str(parsed.get("AH") or ""),
        "battery": str(parsed.get("AI") or ""),
        "status": str(parsed.get("AJ") or ""),
        "price_low": str(parsed.get("AK") or ""),
        "price_min": str(parsed.get("AL") or ""),
        "price_max": str(parsed.get("AM") or ""),
        "ek_url": url,
        "shops_count": str(parsed.get("AK") or ""),
        "variations": {
            "AP": str(parsed.get("AP") or ""),
            "AQ": str(parsed.get("AQ") or ""),
            "AR": str(parsed.get("AR") or ""),
            "AS": str(parsed.get("AS") or ""),
            "AT": str(parsed.get("AT") or ""),
        },
        "gsm_ref_model_key": str(row.get("model_key") or ""),
        "gsm_ref_device_id": gsm_id,
    }
    ek_model_name = str(parsed.get("W") or model_name).strip()
    ek_payload = {
        "source": SOURCE_EK,
        "brand": brand,
        "device_type": dev_type_label,
        "model_name": ek_model_name,
        "model_key": str(row.get("model_key") or "").strip()
        or _normalize_model_name(ek_model_name, {}),
        "source_url": url,
        "image_url": str(row.get("image_url") or ""),
        "specs": ek_specs,
    }

    ok, dev_id, ch, ev_status, _extra_status = _upsert_device(db, ek_payload)
    if not ok:
        return False, "Помилка запису в БД", ""
    _record_daily_history(db, gsm_id, SOURCE_EK, ek_specs)
    return True, ek_model_name, dev_id


def _download_ek_bytes(legacy_ek, url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        res = legacy_ek.EK_HTTP.get(url, timeout=20, max_retries=2)
        content = getattr(res, "content", None)
        return bytes(content) if content else None
    except Exception:
        return None


def _save_ek_primary_photos(legacy_ek, db: DataManager, device_id: str, parsed: dict):
    """Devices with no GSM Arena counterpart (EK-primary mode) have no other
    photo source, so download the main + gallery images straight from the
    E-Katalog product page (already extracted by parse_ekatalog_item) and
    store them the same way GSM Arena photos are stored."""
    main_url = str(parsed.get("PHOTO_MAIN") or "")
    gallery_urls = parsed.get("PHOTO_GALLERY") or []
    if not main_url and not gallery_urls:
        return
    main_bytes = _download_ek_bytes(legacy_ek, main_url) if main_url else None
    gallery_bytes = []
    for g_url in gallery_urls[:7]:
        time.sleep(random.uniform(0.4, 0.9))
        g_bytes = _download_ek_bytes(legacy_ek, g_url)
        if g_bytes:
            gallery_bytes.append(g_bytes)
    if main_bytes or gallery_bytes:
        db.save_gsm_device_photos(device_id, main_bytes, gallery_bytes)


# ---------------------------------------------------------------------------
# E-Katalog "shops appeared" detection — status-based, not shops_count-based
# ---------------------------------------------------------------------------
_MONTHS_UKR = {
    "січень": 1, "лютий": 2, "березень": 3, "квітень": 4, "травень": 5, "червень": 6,
    "липень": 7, "серпень": 8, "вересень": 9, "жовтень": 10, "листопад": 11, "грудень": 12,
}
_DEFAULT_ON_SALE_STATUS_PREFIXES = ["продаж"]


def _parse_status_list(raw, default: list) -> list:
    """Comma-separated status-prefix list from GSM Parser settings (UI-
    editable — see extensions_view.py) -> lowercased list. Falls back to
    `default` when unset, so existing configs keep working unchanged."""
    if not raw or not str(raw).strip():
        return list(default)
    out = [p.strip().lower() for p in str(raw).split(",") if p.strip()]
    return out or list(default)


def _ekat_months_since(date_str) -> Optional[int]:
    """Months between now and E-Katalog's own 'Дата додавання' (specs
    key 'date_added', Ukrainian month-name text, e.g. '15 липня 2024') —
    the listing's real age, as opposed to gsm_parser_devices.created_at
    which only measures when OUR scraper first discovered the row (can lag
    the real listing by months if a brand/category was added to tracking
    late). Returns None when the date can't be parsed — callers treat that
    as "age unknown, don't filter it out"."""
    s = str(date_str or "").strip()
    if not s or s == "-":
        return None
    try:
        m_idx = None
        y_val = None
        for p in s.lower().split():
            if p in _MONTHS_UKR:
                m_idx = _MONTHS_UKR[p]
            if p.isdigit() and len(p) == 4:
                y_val = int(p)
        if m_idx is None or y_val is None:
            return None
        added = datetime(y_val, m_idx, 1)
        now = datetime.now()
        return (now.year - added.year) * 12 + now.month - added.month
    except Exception:
        return None


def _status_is_on_sale(status, prefixes: Optional[list]) -> bool:
    s = str(status or "").strip().lower()
    if not s:
        return False
    return any(s.startswith(p) for p in (prefixes or _DEFAULT_ON_SALE_STATUS_PREFIXES) if p)


def _upsert_device(
    db: DataManager,
    payload: dict,
    max_age_months: Optional[int] = None,
    on_sale_status_prefixes: Optional[list] = None,
) -> tuple[bool, str, int, str, Optional[str]]:
    """Returns: (ok, device_id, changes_count, status, extra_status).

    "shops_appeared" fires once per device, ever: the first time its EK
    `status` spec transitions into an on-sale state (on_sale_status_prefixes,
    default ["продаж"] — matches both "Продаж" and "Продаж! Новинка").
    A persistent specs["ek_ever_on_sale"] flag prevents re-firing later if
    the listing drops out of stock and comes back — shops_count/status can
    flicker between scrapes (temporary stock-outs, flaky E-Katalog scrape),
    and without this guard a long-established device would re-trigger "just
    appeared" every time it flickered back to available. This replaced an
    earlier shops_count-based check (old_shops==0 and new_shops>0) that had
    exactly that flicker problem.

    max_age_months: if set, "shops_appeared" additionally requires the
    listing's own E-Katalog "Дата додавання" (specs["date_added"], parsed by
    _ekat_months_since) to be within this many months — the age of the
    LISTING, not of our gsm_parser_devices row (created_at only measures
    when OUR scraper first discovered it, which can lag the real listing by
    months if a brand/category was added to tracking late). None means no
    age limit (legacy default, used by the manual single-URL pin path which
    isn't part of a scheduled run).

    extra_status: normally None. When a device is inserted for the very
    first time ("new") and its EK status is ALREADY on-sale on this first
    sighting (it skipped the "waiting" stage entirely, or we simply never
    saw it before that point), extra_status is "shops_appeared" so the
    caller can ALSO emit that event alongside "new" — the row otherwise
    would never end up in the "just appeared for sale" report at all,
    since after this call it's already flagged ek_ever_on_sale."""
    source = str(payload.get("source") or "").strip()
    model_key = str(payload.get("model_key") or "").strip()
    if not source or not model_key:
        return False, "", 0, "error", None

    now = datetime.now()
    device_order = int(payload.get("device_order") or 0)
    source_url = str(payload.get("source_url") or "").strip()
    try:
        with db._general_pg().cursor() as cur:
            # Look up by (source, model_key) FIRST — each model_key always
            # owns its own row. This matters when two DIFFERENT GSM Arena
            # devices are (auto-matched or manually pinned) to the very same
            # E-Katalog listing — e.g. a Wi-Fi-only tablet and its 5G sibling
            # sharing one ambiguous EK page — each must keep its own
            # persistent row and get refreshed independently, not fight over
            # a single row via the constraint.
            existing = _fetch_one(cur, source, model_key)
            if not existing and source == SOURCE_EK and source_url:
                url_match = _fetch_one_ek_by_url(cur, source_url)
                if url_match is not None:
                    _url_specs = (
                        url_match.get("specs")
                        if isinstance(url_match.get("specs"), dict)
                        else {}
                    )
                    _incoming_specs_ref = (
                        payload.get("specs")
                        if isinstance(payload.get("specs"), dict)
                        else {}
                    )
                    _existing_gsm_ref = str(
                        _url_specs.get("gsm_ref_device_id") or ""
                    ).strip()
                    _incoming_gsm_ref = str(
                        _incoming_specs_ref.get("gsm_ref_device_id") or ""
                    ).strip()
                    # Only adopt the URL-matched row (recovering it under our
                    # model_key) if it isn't already claimed by a DIFFERENT
                    # GSM Arena device. This preserves the original purpose of
                    # the URL lookup — recovering an EK-primary row whose own
                    # drifting title text changed its model_key between runs
                    # (those rows have no gsm_ref_device_id) — without letting
                    # two distinct devices steal each other's row every run.
                    if not _existing_gsm_ref or _existing_gsm_ref == _incoming_gsm_ref:
                        existing = url_match
            if not existing:
                device_id = f"dev_{uuid.uuid4().hex}"
                insert_specs = dict(payload.get("specs") or {})
                extra_status = None
                if source == SOURCE_EK and _status_is_on_sale(
                    insert_specs.get("status"), on_sale_status_prefixes
                ):
                    insert_specs["ek_ever_on_sale"] = True
                    age_ok = True
                    if max_age_months is not None:
                        months = _ekat_months_since(insert_specs.get("date_added"))
                        if months is not None and months > max_age_months:
                            age_ok = False
                    if age_ok:
                        extra_status = "shops_appeared"
                cur.execute(
                    """
                    INSERT INTO public.gsm_parser_devices
                      (id, source, brand, device_type, model_name, model_key,
                       source_url, image_url, specs, last_seen_at, created_at, updated_at,
                       device_order)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                    """,
                    (
                        device_id,
                        source,
                        str(payload.get("brand") or ""),
                        str(payload.get("device_type") or ""),
                        str(payload.get("model_name") or ""),
                        model_key,
                        str(payload.get("source_url") or ""),
                        str(payload.get("image_url") or ""),
                        json.dumps(insert_specs, ensure_ascii=False),
                        now,
                        now,
                        now,
                        device_order,
                    ),
                )
                _log_change(
                    cur,
                    device_id,
                    source,
                    "__created__",
                    "",
                    str(payload.get("model_name") or ""),
                )
                if extra_status == "shops_appeared":
                    # Dedicated marker the sidebar's "З'явились в продажу"
                    # panel reads directly (see get_gsm_parser_sidebar_data),
                    # instead of re-deriving from raw shops_count history —
                    # that reconstruction has no way to know this is the
                    # device's first-ever on-sale sighting vs. a later
                    # 0->positive flicker, so it kept resurfacing
                    # long-established devices as "just appeared".
                    _log_change(
                        cur,
                        device_id,
                        source,
                        "__shops_appeared__",
                        "",
                        str(insert_specs.get("status") or ""),
                    )
                db._general_pg().commit()
                return True, device_id, 1, "new", extra_status

            device_id = str(existing.get("id") or "")
            changes = 0
            existing_specs = (
                existing.get("specs") if isinstance(existing.get("specs"), dict) else {}
            )
            incoming_specs = (
                payload.get("specs") if isinstance(payload.get("specs"), dict) else {}
            )

            # Change tracking (gsm_parser_change_history) is kept only for the fields
            # that actually drive the E-Katalog charts/sidebar: min/max price and
            # shop count. GSM Arena updates are applied straight to the row below
            # without a history entry — we just detect whether anything changed at
            # all, for the "new/updated/seen" status returned to the caller.
            if source == SOURCE_EK:
                for field_key in ("price_min", "price_max", "shops_count"):
                    old_v = str(existing_specs.get(field_key) or "").strip()
                    new_v = str(incoming_specs.get(field_key) or "").strip()
                    if old_v != new_v:
                        _log_change(cur, device_id, source, field_key, old_v, new_v)
                        changes += 1
            else:
                if str(existing.get("model_name") or "") != str(
                    payload.get("model_name") or ""
                ) or json.dumps(
                    existing_specs, sort_keys=True, ensure_ascii=False
                ) != json.dumps(incoming_specs, sort_keys=True, ensure_ascii=False):
                    changes = 1

            shops_appeared = False
            if source == SOURCE_EK:
                old_status = str(existing_specs.get("status") or "")
                new_status = str(incoming_specs.get("status") or "")
                is_now_on_sale = _status_is_on_sale(new_status, on_sale_status_prefixes)
                ever_on_sale = bool(existing_specs.get("ek_ever_on_sale"))
                _shops_appeared_suppress_reason = None
                if is_now_on_sale and old_status.strip().lower() != new_status.strip().lower():
                    changes = max(changes, 1)
                if is_now_on_sale:
                    # Persist the flag onto incoming_specs so it survives the
                    # merge below and is never lost on a later scrape, even
                    # after this device eventually goes back out of stock.
                    incoming_specs["ek_ever_on_sale"] = True
                    if not ever_on_sale:
                        shops_appeared = True
                        if max_age_months is not None:
                            date_added = (
                                incoming_specs.get("date_added")
                                or existing_specs.get("date_added")
                            )
                            months = _ekat_months_since(date_added)
                            if months is not None and months > max_age_months:
                                shops_appeared = False
                                _shops_appeared_suppress_reason = (
                                    f"listing too old: {months} months > {max_age_months}"
                                )
                        if shops_appeared:
                            logger.info(
                                "shops_appeared TRIGGER device_id=%s model=%s"
                                " old_status=%r new_status=%r date_added=%r",
                                device_id,
                                str(existing.get("model_name") or payload.get("model_name") or ""),
                                old_status,
                                new_status,
                                incoming_specs.get("date_added") or existing_specs.get("date_added"),
                            )
                            # See the matching marker in the INSERT branch
                            # above — same reasoning, same field_key, so the
                            # sidebar query has a single source of truth for
                            # "this device's first-ever on-sale sighting"
                            # regardless of whether it happened on creation
                            # or on a later scrape of an existing row.
                            _log_change(
                                cur,
                                device_id,
                                source,
                                "__shops_appeared__",
                                "",
                                new_status,
                            )
                    else:
                        _shops_appeared_suppress_reason = "ek_ever_on_sale already true"
                elif old_status.strip().lower() != new_status.strip().lower():
                    _shops_appeared_suppress_reason = (
                        f"status not recognized as on-sale (prefixes={on_sale_status_prefixes!r})"
                    )
                if _shops_appeared_suppress_reason and (
                    old_status.strip().lower() != new_status.strip().lower()
                    or bool(incoming_specs.get("shops_count"))
                    != bool(existing_specs.get("shops_count"))
                ):
                    # Only every device whose status/shops_count actually
                    # moved this run — cheap enough to always log, and turns
                    # the next "TG fired but sidebar panel didn't show it"
                    # report into an exact, traceable reason instead of
                    # another dead end requiring a live DB investigation.
                    logger.info(
                        "shops_appeared SUPPRESSED device_id=%s model=%s reason=%s"
                        " old_status=%r new_status=%r ever_on_sale=%s",
                        device_id,
                        str(existing.get("model_name") or payload.get("model_name") or ""),
                        _shops_appeared_suppress_reason,
                        old_status,
                        new_status,
                        ever_on_sale,
                    )

            # Merge onto existing_specs instead of replacing wholesale — this
            # row's specs blob is shared across concerns: a GSM Arena rescrape
            # only knows about GSM Arena fields, but the SAME row can also carry
            # manual_ek_url (superadmin's pinned E-Katalog override, written by
            # save_gsm_device_manual_ek_url) and other cross-domain keys added
            # elsewhere. A raw overwrite here silently dropped manual_ek_url on
            # the very next GSM Arena scrape after it was set — the pin would
            # vanish before the EK phase of the SAME run ever got a chance to
            # read it, forcing a needless (and sometimes wrong) re-match.
            merged_specs = {**existing_specs, **incoming_specs}

            cur.execute(
                """
                UPDATE public.gsm_parser_devices
                SET brand=%s,
                    device_type=%s,
                    model_name=%s,
                    model_key=%s,
                    source_url=%s,
                    image_url=%s,
                    specs=%s::jsonb,
                    last_seen_at=%s,
                    updated_at=%s,
                    device_order=%s
                WHERE id=%s
                """,
                (
                    str(payload.get("brand") or ""),
                    str(payload.get("device_type") or ""),
                    str(payload.get("model_name") or ""),
                    model_key,
                    str(payload.get("source_url") or ""),
                    str(payload.get("image_url") or ""),
                    json.dumps(merged_specs, ensure_ascii=False),
                    now,
                    now,
                    device_order,
                    device_id,
                ),
            )

            db._general_pg().commit()
            if shops_appeared:
                return True, device_id, changes, "shops_appeared", None
            if changes > 0:
                return True, device_id, changes, "updated", None
            return True, device_id, 0, "seen", None
    except Exception:
        logger.exception(
            "_upsert_device failed for source=%s model_key=%s — rolling back "
            "and skipping this device so the rest of the run can continue",
            source,
            model_key,
        )
        with contextlib.suppress(Exception):
            db._general_pg().rollback()
        return False, "", 0, "error", None


def _message_map(config: dict) -> dict:
    telegram = (
        config.get("telegram") if isinstance(config.get("telegram"), dict) else {}
    )
    rows = (
        telegram.get("messages") if isinstance(telegram.get("messages"), list) else []
    )
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("id") or "").strip()
        if key:
            out[key] = row
    return out


def _render_message_template(template: str, context: dict) -> str:
    class SafeDict(dict):
        def __missing__(self, key):
            return ""

    return str(template or "").format_map(SafeDict(context))


def _tg_post(url: str, data: dict, files: Optional[dict] = None, timeout: int = 20) -> bool:
    """POST to the Telegram Bot API with 429 (flood-control) retry and a
    pacing delay after every call. Telegram allows roughly one message per
    second per chat — firing device cards back-to-back with no delay and no
    status check (the previous behavior) made 429 responses invisible: the
    request "succeeded" from requests' point of view (no exception raised),
    so whichever message got rate-limited — often a section header, since
    headers are sent right after a burst of photos — silently vanished with
    no trace, making entire report sections (e.g. GSM Arena new devices)
    appear to never have been sent. Returns True only on a genuine 2xx —
    callers use this to fall back to a plain text message when a photo URL
    is rejected (e.g. Telegram's "wrong type of the web page content" for a
    scraped image link that isn't actually a direct image), instead of
    losing the card entirely."""
    file_streams = list(files.values()) if files else []
    for attempt in range(5):
        for f in file_streams:
            with contextlib.suppress(Exception):
                f.seek(0)
        try:
            resp = requests.post(url, data=data, files=files, timeout=timeout)
        except Exception as e:
            logger.warning("Telegram request failed (attempt %s): %s", attempt + 1, e)
            time.sleep(2.0)
            continue
        if resp.status_code == 429:
            try:
                retry_after = int(resp.json().get("parameters", {}).get("retry_after", 3))
            except Exception:
                retry_after = 3
            logger.info("Telegram rate-limited (429) — retrying in %ss", retry_after)
            time.sleep(retry_after + 1)
            continue
        if not resp.ok:
            logger.warning("Telegram send failed: %s %s", resp.status_code, resp.text[:300])
        time.sleep(1.2)
        return bool(resp.ok)
    logger.warning("Telegram send gave up after repeated 429/errors: %s", url)
    return False


def _send_tg_photo(token: str, chat_id: str, photo: str, caption: str):
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    if str(photo).startswith("http://") or str(photo).startswith("https://"):
        ok = _tg_post(
            url,
            data={
                "chat_id": chat_id,
                "photo": photo,
                "caption": caption,
                "parse_mode": "HTML",
            },
            timeout=20,
        )
        if not ok:
            # Photo URL rejected (dead link, redirect, non-image content —
            # Telegram's "wrong type of the web page content" etc.) — send
            # the card as plain text instead of silently dropping it.
            _send_tg_message(token, chat_id, caption)
        return

    path = Path(str(photo or ""))
    if path.exists() and path.is_file():
        with path.open("rb") as f:
            ok = _tg_post(
                url,
                data={
                    "chat_id": chat_id,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                files={"photo": f},
                timeout=20,
            )
        if not ok:
            _send_tg_message(token, chat_id, caption)
        return

    _send_tg_message(token, chat_id, caption)


def _send_logo_header(token: str, chat_id: str, logo_path: str, header_text: str):
    text = str(header_text or "").strip()
    if not text:
        return
    logo = str(logo_path or "").strip()
    if logo:
        with contextlib.suppress(Exception):
            _send_tg_photo(token, chat_id, logo, text)
            return
    _send_tg_message(token, chat_id, text)


def _xiaomi_regions_str(db: DataManager, device_id: str) -> str:
    """Comma-separated region names currently on file for this device (e.g.
    'Europe, Global, India') — used to append regions straight into a "new
    device" / "shops appeared" Telegram card for Xiaomi devices that already
    have region data, regardless of whether that data was captured this run
    or an earlier one."""
    try:
        variants = db.get_gsm_regional_variants(device_id) or []
    except Exception:
        return ""
    names = sorted(
        {str(v.get("region") or "").strip() for v in variants if str(v.get("region") or "").strip()}
    )
    return ", ".join(names)


def _event_context(event: dict) -> dict:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    specs = payload.get("specs") if isinstance(payload.get("specs"), dict) else {}
    vars_ = specs.get("variations") if isinstance(specs.get("variations"), dict) else {}
    return {
        "source": str(payload.get("source") or ""),
        "brand": str(payload.get("brand") or ""),
        "device_type": str(payload.get("device_type") or ""),
        "model_name": str(payload.get("model_name") or ""),
        "source_url": str(payload.get("source_url") or ""),
        "image_url": str(payload.get("image_url") or ""),
        "changes_count": int(event.get("changes") or 0),
        "network_full": str(specs.get("network_full") or specs.get("target_net") or ""),
        "dimensions": str(specs.get("dimensions") or ""),
        # GSM Arena stores this under "display_size", E-Katalog under
        # "display" — one unified field so a single checkbox covers both
        # sources instead of needing two near-duplicate list entries.
        "diagonal": str(specs.get("display_size") or specs.get("display") or ""),
        "status": str(specs.get("status") or ""),
        "price_uah": str(specs.get("price_uah") or ""),
        "price_min": str(specs.get("price_min") or ""),
        "price_max": str(specs.get("price_max") or ""),
        "shops_count": str(specs.get("shops_count") or ""),
        "regions": str(specs.get("regions") or ""),
        "ap": str(vars_.get("AP") or ""),
        "aq": str(vars_.get("AQ") or ""),
        "ar": str(vars_.get("AR") or ""),
        "as": str(vars_.get("AS") or ""),
        "at": str(vars_.get("AT") or ""),
    }


# Fields a "device card" block can be composed from — the settings UI shows
# these as plain checkboxes (label only) so configuring a report never
# requires knowing/typing a {placeholder} name. Some fields are naturally
# blank for the "other" source (e.g. price_uah is GSM Arena-only, price_min_max
# is E-Katalog-only) — picking an irrelevant field for a category just renders
# an empty line, so the per-category defaults below only pick relevant ones.
FIELD_CATALOG: list[tuple[str, str, str]] = [
    ("brand", "Бренд", "🏷 Бренд: <b>{brand}</b>"),
    ("model_name", "Модель", "📱 Модель: <b>{model_name}</b>"),
    ("device_type", "Тип пристрою", "🛠 Тип: {device_type}"),
    ("network_full", "Мережа", "📶 Мережа: {network_full}"),
    ("dimensions", "Розмір", "📐 Розмір: {dimensions}"),
    ("diagonal", "Діагональ", "🖥 Діагональ: {diagonal}"),
    ("price_uah", "Ціна, грн (GSM Arena)", "💵 Ціна (грн): <b>{price_uah}</b>"),
    ("status", "Статус (E-Katalog)", "📊 Статус: {status}"),
    (
        "price_min_max",
        "Мін/Макс ціна (E-Katalog)",
        "💰 Мін/Макс: <b>{price_min}</b> / <b>{price_max}</b> грн",
    ),
    ("shops_count", "Кількість магазинів (E-Katalog)", "🛒 Магазинів: <b>{shops_count}</b>"),
    ("regions", "Регіони (Xiaomi)", "🌍 Регіони: {regions}"),
    ("link", "Посилання «Відкрити»", "🔗 <a href='{source_url}'>Відкрити</a>"),
]

# Category keys the runner recognizes — each maps to one of the four event
# groups a parse run can produce. Every user-added/removed message block
# lives inside one of these lists, in display order.
CATEGORY_KEYS = ("gsm_new", "ekatalog_new", "ekatalog_shops", "regions_appeared")


def _default_category_blocks(assets: dict) -> dict:
    """Fallback block layout used when the config predates the flexible
    per-category editor (no telegram.categories key saved yet) — matches the
    previous fixed header+card templates 1:1, and carries over any
    previously-uploaded global logo as that category's header photo so nothing
    already configured gets silently dropped the first time this runs."""
    gsm_logo = str((assets or {}).get("gsm_logo_path") or (assets or {}).get("gsm_logo_url") or "")
    ek_logo = str((assets or {}).get("ekatalog_logo_path") or (assets or {}).get("ekatalog_logo_url") or "")
    return {
        "gsm_new": [
            {
                "kind": "header",
                "enabled": True,
                "text": "<b>🌐 GSM Arena: Нові пристрої:</b>",
                "photo_path": gsm_logo,
            },
            {
                "kind": "device_card",
                "enabled": True,
                "fields": ["brand", "model_name", "device_type", "network_full", "dimensions", "diagonal", "price_uah", "regions", "link"],
            },
        ],
        "ekatalog_new": [
            {
                "kind": "header",
                "enabled": True,
                "text": "<b>🛒 E-Katalog: Нові пристрої:</b>",
                "photo_path": ek_logo,
            },
            {
                "kind": "device_card",
                "enabled": True,
                "fields": ["brand", "model_name", "dimensions", "diagonal", "status", "price_min_max", "regions", "link"],
            },
        ],
        "ekatalog_shops": [
            {
                "kind": "header",
                "enabled": True,
                "text": "<b>🛍️ Почали продавати на E-Katalog:</b>",
                "photo_path": ek_logo,
            },
            {
                "kind": "device_card",
                "enabled": True,
                "fields": ["brand", "model_name", "dimensions", "diagonal", "status", "price_min_max", "shops_count", "regions", "link"],
            },
        ],
        "regions_appeared": [
            {
                "kind": "header",
                "enabled": True,
                "text": "<b>🌍 З'явились регіони:</b>",
                "photo_path": "",
            },
            {
                "kind": "device_card",
                "enabled": True,
                "fields": ["model_name", "device_type", "network_full", "regions"],
            },
        ],
    }


def _compose_device_card(fields: list, ctx: dict) -> str:
    line_by_key = {k: tpl for k, _label, tpl in FIELD_CATALOG}
    lines = []
    for key in fields or []:
        tpl = line_by_key.get(str(key))
        if not tpl:
            continue
        rendered = _render_message_template(tpl, ctx)
        if rendered.strip():
            lines.append(rendered)
    return "\n".join(lines)


def _send_category_blocks(
    db,
    token: str,
    chat_id: str,
    blocks: list,
    events_for_cat: list[dict],
) -> None:
    """Send every enabled block for one category, in the configured order —
    any number of headers (each with its own optional photo) and device-card
    blocks (each rendered once per device) can be mixed freely."""
    if not events_for_cat:
        return
    for block in blocks or []:
        if not isinstance(block, dict) or not bool(block.get("enabled", True)):
            continue
        kind = str(block.get("kind") or "").strip()
        if kind == "header":
            txt = str(block.get("text") or "").strip()
            if not txt:
                continue
            photo = str(block.get("photo_path") or block.get("photo_url") or "").strip()
            if photo.lower().startswith("pgfile://"):
                # Uploaded via the message editor — stored as bytes in Postgres
                # (`files` table), not a real URL/local path, so it needs to be
                # materialized to a temp file before Telegram can send it.
                with contextlib.suppress(Exception):
                    photo = db.resolve_image_file_path(photo) or ""
            with contextlib.suppress(Exception):
                _send_logo_header(token, chat_id, photo, txt)
        elif kind == "device_card":
            flds = block.get("fields") if isinstance(block.get("fields"), list) else []
            for event in events_for_cat:
                ctx = _event_context(event)
                body = _compose_device_card(flds, ctx)
                if not body.strip():
                    continue
                image_url = str(ctx.get("image_url") or "").strip()
                with contextlib.suppress(Exception):
                    if image_url:
                        _send_tg_photo(token, chat_id, image_url, body)
                    else:
                        _send_tg_message(token, chat_id, body)


def _send_telegram_detailed(db, config: dict, events: list[dict], stats: dict):
    telegram = (
        config.get("telegram") if isinstance(config.get("telegram"), dict) else {}
    )
    token = str(telegram.get("bot_token") or "").strip()
    chat_id = str(telegram.get("chat_id") or "").strip()
    if not token or not chat_id:
        return

    assets = telegram.get("assets") if isinstance(telegram.get("assets"), dict) else {}
    msg_map = _message_map(config)

    def _msg(row_id: str, fallback_text: str, fallback_type: str = "text"):
        row = msg_map.get(row_id) or {}
        enabled = bool(row.get("enabled", True))
        m_type = str(row.get("message_type") or fallback_type).strip() or fallback_type
        text = str(row.get("text") or fallback_text)
        return enabled, m_type, text

    enabled_intro, _, intro_text = _msg("intro", "")
    if enabled_intro and intro_text.strip():
        with contextlib.suppress(Exception):
            _send_tg_message(token, chat_id, intro_text)

    gsm_new_events = [
        e
        for e in events
        if str(e.get("source")) == SOURCE_GSM and str(e.get("status")) == "new"
    ]
    ek_new_events = [
        e
        for e in events
        if str(e.get("source")) == SOURCE_EK and str(e.get("status")) == "new"
    ]
    ek_shops_events = [
        e
        for e in events
        if str(e.get("source")) == SOURCE_EK
        and str(e.get("status")) == "shops_appeared"
    ]
    regions_events = [
        e
        for e in events
        if str(e.get("source")) == SOURCE_XIAOMI_REGIONS
        and str(e.get("status")) == "regions_appeared"
    ]

    categories_cfg = telegram.get("categories")
    if not isinstance(categories_cfg, dict) or not categories_cfg:
        categories_cfg = _default_category_blocks(assets)

    _send_category_blocks(
        db, token, chat_id,
        categories_cfg.get("gsm_new") if isinstance(categories_cfg.get("gsm_new"), list) else [],
        gsm_new_events,
    )
    _send_category_blocks(
        db, token, chat_id,
        categories_cfg.get("ekatalog_new") if isinstance(categories_cfg.get("ekatalog_new"), list) else [],
        ek_new_events,
    )
    _send_category_blocks(
        db, token, chat_id,
        categories_cfg.get("ekatalog_shops") if isinstance(categories_cfg.get("ekatalog_shops"), list) else [],
        ek_shops_events,
    )
    _send_category_blocks(
        db, token, chat_id,
        categories_cfg.get("regions_appeared") if isinstance(categories_cfg.get("regions_appeared"), list) else [],
        regions_events,
    )

    if not gsm_new_events and not ek_new_events and not ek_shops_events and not regions_events:
        en, _, txt = _msg(
            "no_new_devices", "<b>Нових пристрої за добу не з'явилось</b>"
        )
        if en and txt.strip():
            with contextlib.suppress(Exception):
                _send_tg_message(token, chat_id, txt)


def _existing_brand_url_map(db: DataManager, brand: str) -> dict:
    out = {}
    with db._general_pg().cursor() as cur:
        cur.execute(
            """
            SELECT source_url FROM public.gsm_parser_devices
            WHERE source=%s AND brand=%s
            ORDER BY CASE WHEN device_order = 0 THEN 9999 ELSE device_order END ASC
            """,
            (SOURCE_GSM, brand),
        )
        rows = cur.fetchall() or []
    for row in rows:
        url = str((row or {}).get("source_url") or "").strip().lower()
        if not url:
            continue
        if url.endswith("/"):
            url = url[:-1]
        out[url] = {"row": 0, "data": []}
    return out


def _count_brand_rows(db: DataManager, brand: str) -> int:
    with db._general_pg().cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM public.gsm_parser_devices WHERE source=%s AND brand=%s",
            (SOURCE_GSM, brand),
        )
        row = cur.fetchone() or {}
    return int(row.get("c") or 0)


def _map_gsm_info_to_payload(info: list, rules: dict) -> dict:
    model_name = str(info[1] if len(info) > 1 else "").strip()
    # model_key is the DB unique identifier — must preserve ALL distinguishing
    # words (Ultra, Pro, Max, Lite, Pro+, 4G, 5G, etc.) so that variants like
    # "Realme 15 Ultra" vs "Realme 15" or "16 Pro" vs "16 Pro+" never collide.
    # exclude_words / custom_rules are for COMPARISON only, not for keying.
    _id_rules = {
        "remove_4g5g": False,
        "remove_punctuation": False,
        "exclude_words": [],
        "custom_rules": [],
        "case_insensitive": rules.get("case_insensitive", True),
    }
    model_key = _normalize_model_name(model_name, _id_rules)
    specs = {
        "network_label": str(info[2] if len(info) > 2 else ""),
        "network_full": str(info[3] if len(info) > 3 else ""),
        "announced": str(info[6] if len(info) > 6 else ""),
        "released": str(info[7] if len(info) > 7 else ""),
        "dimensions": str(info[8] if len(info) > 8 else ""),
        "length": str(info[9] if len(info) > 9 else ""),
        "width": str(info[10] if len(info) > 10 else ""),
        "thickness": str(info[11] if len(info) > 11 else ""),
        "weight": str(info[12] if len(info) > 12 else ""),
        "display_size": str(info[13] if len(info) > 13 else ""),
        "os": str(info[14] if len(info) > 14 else ""),
        "chipset": str(info[15] if len(info) > 15 else ""),
        "memory": str(info[16] if len(info) > 16 else ""),
        "battery": str(info[17] if len(info) > 17 else ""),
        "price_gsm": str(info[18] if len(info) > 18 else ""),
        "price_uah": str(info[19] if len(info) > 19 else ""),
        "year": str(info[22] if len(info) > 22 else ""),
    }
    return {
        "source": SOURCE_GSM,
        "brand": str(info[0] if len(info) > 0 else "").strip(),
        "device_type": str(info[4] if len(info) > 4 else "").strip(),
        "model_name": model_name,
        "model_key": model_key,
        "source_url": str(info[21] if len(info) > 21 else "").strip(),
        "image_url": str(info[20] if len(info) > 20 else "").strip(),
        "specs": specs,
    }


def _extract_target_net(gsm_specs: dict) -> str:
    if not isinstance(gsm_specs, dict):
        return ""
    return str(
        gsm_specs.get("network_label") or gsm_specs.get("network_full") or ""
    ).strip()


_EK_MONTHS_UKR: dict[str, int] = {
    "січень": 1, "лютий": 2, "березень": 3, "квітень": 4, "травень": 5, "червень": 6,
    "липень": 7, "серпень": 8, "вересень": 9, "жовтень": 10, "листопад": 11, "грудень": 12,
    # genitive forms E-Katalog also uses when a day-of-month precedes the month name
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}


def _parse_ek_date_added(raw: str) -> tuple[str, str]:
    """Parse E-Katalog's "Дата додавання" free-text cell (e.g. "12 листопада
    2021" or "Травень 2025") into (year, month) as zero-padded strings, or
    ("", "") if no recognizable month/year was found. Used for EK-primary
    brands (e.g. Amazfit/MiBand) so their listing can be sorted by real date
    and their "Рік" column filled in — those brands have no GSM Arena data
    to source a year from otherwise."""
    year = ""
    month = ""
    for part in str(raw or "").lower().split():
        if part in _EK_MONTHS_UKR:
            month = f"{_EK_MONTHS_UKR[part]:02d}"
        elif part.isdigit() and len(part) == 4:
            year = part
    return year, month


def _type_to_cat(device_type: str) -> tuple[str, str]:
    dt = str(device_type or "").strip().lower()
    if dt in {"смартфон", "smartphone", "phone", "телефон"}:
        return "Smartphone", "122"
    if dt in {"планшет", "tablet"}:
        return "Tablet", "30"
    if dt in {"годинник", "watch", "смартгодинник", "smartwatch"}:
        return "Watch", "745"
    return "Smartphone", "122"


_EK_IMPORTANT_TAGS: frozenset[str] = frozenset(
    [
        "pro",
        "india",
        "sports",
        "aluminum",
        "nike",
        "cn",
        "curve",
        "active",
        "premier",
        "smart",
        "note",
        "xpad",
        "hot",
        "gt",
        "hd",
        "zero",
        "vip",
        "plus",
        "+",
        "max",
        "ultra",
        "mini",
        "se",
        "xl",
        "lite",
        "power",
        "play",
        "neo",
        "stylus",
        "fusion",
        "edge",
        "fe",
        "go",
        "slim",
        "flip",
        "fold",
        "trifold",
        "classic",
        "z",
    ]
)

_EK_NETWORK_RE = re.compile(r"\b(4g|5g|lte|wi-?fi|cellular)\b", re.IGNORECASE)


def _ek_network_tokens(text: str) -> frozenset:
    """Network-generation markers (4G/5G/LTE/Wi-Fi/Cellular) read from the
    ORIGINAL, pre-clean_name_final text. clean_name_final strips these tokens
    entirely before the main word-set comparison in _ek_name_matches — normally
    harmless (naming-convention noise between the two sites) but wrong when a
    network marker is the ONLY thing separating two genuinely different SKUs,
    e.g. tablets frequently sold as separate products: "Redmi Pad Pro" (Wi-Fi)
    vs "Redmi Pad Pro 5G" — same base name, different price/specs/stock."""
    return frozenset(
        m.group(1).lower().replace("-", "")
        for m in _EK_NETWORK_RE.finditer(str(text or ""))
    )


def _ek_is_block(exc: Exception) -> bool:
    """Return True if the exception indicates an anti-bot block from ek.ua."""
    s = str(exc).lower()
    return any(m in s for m in ("blocked", "403", "429", "captcha", "access denied", "forbidden"))


def _ek_reset_session(legacy_ek) -> None:
    """Discard EK HTTP session and cloudscraper — new cookies on next request."""
    try:
        legacy_ek.EK_HTTP.session = legacy_ek.EK_HTTP._new_requests_session()
        legacy_ek.EK_HTTP.scraper = legacy_ek.EK_HTTP._new_scraper()
        legacy_ek.EK_HTTP.last_warmup_ts = 0
    except Exception:
        pass


def _get_all_brand_links(legacy_ek, list_url: str, sleep_fn=None):
    """Collect all product (title, url) candidates from an ek.ua brand listing
    URL (paginated). Uses class='model-short-title' exactly as the old
    ekatalog.py did, but — unlike the old collector — also keeps the anchor's
    own text. The listing page already renders the full product title right
    there in the link (e.g. "Apple Watch Ultra 3"), so callers can filter
    candidates by name locally before paying for a full page fetch.
    Returns list[dict] with 'url'/'title' keys on success, or the string
    "STOP" if an anti-bot block is hit.
    sleep_fn: optional callable(secs) for stop/pause support."""
    all_links: list[dict] = []
    seen: set[str] = set()
    _sleep = sleep_fn if sleep_fn is not None else time.sleep
    page = 1
    while page <= 40:
        page_url = list_url + (f"&page_={page}" if page > 1 else "")
        try:
            res = legacy_ek.EK_HTTP.get(
                page_url,
                timeout=25,
                max_retries=3,
                referer=legacy_ek.EKATALOG_HOME,
            )
            soup = legacy_ek.BeautifulSoup(res.text, "html.parser")
            found_on_page = 0
            for a in soup.find_all("a", class_="model-short-title"):
                href = str(a.get("href") or "").strip()
                if not href:
                    continue
                full_url = (
                    "https://ek.ua" + href if not href.startswith("http") else href
                )
                if full_url not in seen:
                    seen.add(full_url)
                    title = a.get_text(" ", strip=True) or str(
                        a.get("title") or ""
                    ).strip()
                    all_links.append({"url": full_url, "title": title})
                    found_on_page += 1
            if not found_on_page:
                break
            if not soup.find("a", id=re.compile(r"pager_"), string=str(page + 1)):
                break
            page += 1
            _sleep(random.uniform(1.5, 3.0))
        except Exception as exc:
            if _ek_is_block(exc):
                return "STOP"
            break
    return all_links


def _ek_year_tokens(text: str) -> set:
    """Extract 4-digit years, including ones written as "(2022)" — a bare
    year wrapped in parens is still a year marker for matching purposes, but
    clean_name_final's parenthesis-stripping step would otherwise erase it
    before it's ever compared."""
    txt = str(text or "")
    txt = re.sub(r"\((20\d{2})\)", r" \1 ", txt)
    return set(re.findall(r"\b(20\d{2})\b", txt))


def _ek_name_matches(
    legacy_ek, model_name: str, site_name: str, has_year: bool = False
) -> bool:
    """Name-matching 1:1 with old ekatalog.py run_ekatalog_sync matching block.

    Year handling: the base word-match always ignores year (a search term
    with a year must still be able to match a site listing that never states
    one — e.g. an older/unlabeled variant). Year is only enforced as a hard
    requirement when BOTH sides actually state one — otherwise a device like
    "Galaxy Tab S6 Lite (2022)" would happily match an unrelated "...2024"
    reissue of the same base name just because 2022 got silently stripped."""
    _ = has_year  # kept for call-site compatibility; no longer gates behavior
    orig_clean = legacy_ek.clean_name_final(model_name, keep_year=False)
    site_clean = legacy_ek.clean_name_final(site_name, keep_year=False)
    orig_words = set(orig_clean.split())
    site_words = set(site_clean.split())
    # 1. original words must be subset of site words (same as: if not issubset: continue)
    if not orig_words.issubset(site_words):
        return False
    # 2. important tags must not differ between original and site
    if any((tag in site_words) != (tag in orig_words) for tag in _EK_IMPORTANT_TAGS):
        return False
    # 2b. network-generation markers (4G/5G/LTE/Wi-Fi) — checked on the
    # ORIGINAL strings since clean_name_final already stripped them from
    # orig_words/site_words above. Must match exactly, not just "both present":
    # a search for the plain Wi-Fi variant must never match a listing that
    # only exists as the 5G variant (or vice versa) — see _ek_network_tokens.
    if _ek_network_tokens(model_name) != _ek_network_tokens(site_name):
        return False
    # 3. single-letter markers from old code
    if ("s" in orig_words) != ("s" in site_words):
        return False
    if ("i" in orig_words) != ("i" in site_words):
        return False
    # 4. year: only a hard constraint when both sides state one
    orig_years = _ek_year_tokens(model_name)
    site_years = _ek_year_tokens(site_name)
    if orig_years and site_years and orig_years.isdisjoint(site_years):
        return False
    # 5. site must not introduce an extra generation/model number the search
    # term doesn't have. issubset() alone lets the site side carry extra
    # words, which is normally fine (colour, storage — already stripped
    # above) but a bare or suffixed digit token ("4", "4a", "11i"...) almost
    # always marks a distinct generation: searching the original unnumbered
    # "Pixel XL" must never match "Pixel 4 XL", and unnumbered "Pixel" must
    # never match "Pixel 4a", just because both are otherwise word-subsets.
    orig_numbered = {w for w in orig_words if any(ch.isdigit() for ch in w)}
    site_numbered = {w for w in site_words if any(ch.isdigit() for ch in w)}
    if site_numbered - orig_numbered:
        return False
    return True


def _ek_year_match_tier(model_name: str, site_name: str) -> int:
    """Rank an already-accepted candidate (see _ek_name_matches — a candidate
    with a genuinely different year never reaches this, it's hard-rejected)
    so the search tries an exact year match FIRST and only falls back to a
    candidate that states no year at all: 0 = search has no year, or the
    candidate states the same year (best); 1 = search has a year but the
    candidate states none (fallback — accepted, but only if nothing better
    exists)."""
    orig_years = _ek_year_tokens(model_name)
    if not orig_years:
        return 0
    return 0 if _ek_year_tokens(site_name) else 1


def _get_manual_ek_url(db: "DataManager", gsm_device_id: str) -> str:
    """Fresh per-device read of the manual EK pin straight from the DB.

    The GSM device row this phase iterates over (`row`) comes from a single
    upfront snapshot query taken before the whole EK phase started (see
    `all_gsm_rows = _load_all_gsm_rows_for_ek(db)`), which can run for a long
    time across hundreds of devices. If the user pins a manual EK URL from a
    device card WHILE this run is already in progress, that edit lands in the
    DB immediately but is invisible in the stale in-memory snapshot — the pin
    would silently be ignored until the next full run. This one-row lookup by
    primary key is cheap enough to do per device and keeps it live."""
    with contextlib.suppress(Exception):
        with db._general_pg().cursor() as cur:
            cur.execute(
                "SELECT specs FROM public.gsm_parser_devices WHERE id=%s LIMIT 1",
                (str(gsm_device_id),),
            )
            row = cur.fetchone()
            specs = (
                (dict(row) if isinstance(row, dict) else {}).get("specs")
                if row
                else None
            )
            if isinstance(specs, dict):
                return str(specs.get("manual_ek_url") or "").strip()
    return ""


def _get_ek_existing_url(db: "DataManager", gsm_device_id: str) -> str:
    """Return source_url of existing EK DB record for this GSM device, or empty."""
    with contextlib.suppress(Exception):
        with db._general_pg().cursor() as cur:
            cur.execute(
                "SELECT source_url FROM public.gsm_parser_devices "
                "WHERE source=%s AND specs->>'gsm_ref_device_id'=%s LIMIT 1",
                (SOURCE_EK, gsm_device_id),
            )
            row = cur.fetchone()
            if row:
                return str(
                    (dict(row) if isinstance(row, dict) else {}).get("source_url") or ""
                )
    return ""


def _load_gsm_rows_for_ek(db: DataManager, brand: str, device_type: str) -> list[dict]:
    where = ["source=%s"]
    params: list[Any] = [SOURCE_GSM]

    if brand:
        where.append("brand=%s")
        params.append(brand)

    query = (
        "SELECT id, brand, device_type, model_name, model_key, source_url, image_url, specs "
        "FROM public.gsm_parser_devices WHERE "
        + " AND ".join(where)
        + " ORDER BY CASE WHEN device_order = 0 THEN 9999 ELSE device_order END ASC"
    )

    rows: list[dict] = []
    with db._general_pg().cursor() as cur:
        cur.execute(query, tuple(params))
        rows = cur.fetchall() or []

    if not device_type:
        return rows

    filtered = []
    requested, _ = _type_to_cat(device_type)
    for row in rows:
        got, _ = _type_to_cat(str(row.get("device_type") or ""))
        if got == requested:
            filtered.append(row)
    return filtered


def _load_all_gsm_rows_for_ek(db: DataManager) -> list[dict]:
    """Return all GSM Arena devices ordered by brand then device_order for flat EK iteration."""
    rows: list[dict] = []
    with db._general_pg().cursor() as cur:
        cur.execute(
            """
            SELECT id, brand, device_type, model_name, model_key, source_url, image_url, specs
            FROM public.gsm_parser_devices
            WHERE source=%s
            ORDER BY brand, CASE WHEN device_order = 0 THEN 9999 ELSE device_order END ASC
            """,
            (SOURCE_GSM,),
        )
        rows = cur.fetchall() or []
    return rows


def _send_tg_message(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    _tg_post(
        url,
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
        timeout=15,
    )


def _send_telegram_summary(config: dict, stats: dict):
    telegram = (
        config.get("telegram") if isinstance(config.get("telegram"), dict) else {}
    )
    token = str(telegram.get("bot_token") or "").strip()
    chat_id = str(telegram.get("chat_id") or "").strip()
    if not token or not chat_id:
        return

    changed_models = int(stats.get("changed", 0))
    exec_seconds = float(stats.get("elapsed_sec", 0.0))

    messages = _message_map(config)
    stats_tpl = messages.get("stats") if isinstance(messages.get("stats"), dict) else {}
    final_tpl = messages.get("final") if isinstance(messages.get("final"), dict) else {}
    stats_text = str(
        stats_tpl.get("text")
        or "📊 <b>Статистика оновлення:</b>\n⏱ <b>Час парсингу:</b> {execution_time}\n🔄 <b>Кількість моделей зі змінами:</b> {changed_models}"
    )
    final_text = str(final_tpl.get("text") or "🏁 <b>Всі дані оновлено.</b>")
    spreadsheet_url = str(
        (config.get("telegram") or {}).get("spreadsheet_url") or ""
    ).strip()

    context = {
        "execution_time": f"{exec_seconds:.1f}s",
        "changed_models": changed_models,
        "spreadsheet_url": spreadsheet_url,
        "errors": int(stats.get("errors", 0)),
        "gsm_written": int(stats.get("gsm_written", 0)),
        "ek_written": int(stats.get("ek_written", 0)),
    }

    text = _render_message_template(stats_text, context)
    text2 = _render_message_template(final_text, context)

    try:
        _send_tg_message(token, chat_id, text)
        _send_tg_message(token, chat_id, text2)
    except Exception as exc:
        logger.warning("Telegram summary failed: %s", exc)


def _in_quiet_window(now: datetime, start_hm: str, end_hm: str) -> bool:
    """True if `now` falls in [start_hm, end_hm) — the window may wrap past
    midnight (e.g. "21:00" -> "08:00" covers both 23:xx and 03:xx)."""
    try:
        sh, sm = (int(x) for x in start_hm.split(":"))
        eh, em = (int(x) for x in end_hm.split(":"))
    except Exception:
        return False
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    now_min = now.hour * 60 + now.minute
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min


def _next_time_at_or_after(now: datetime, hm: str) -> datetime:
    """Next occurrence of clock time `hm` ("HH:MM") strictly after `now`."""
    try:
        h, m = (int(x) for x in hm.split(":"))
    except Exception:
        h, m = 8, 0
    candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _flush_due_telegram_notifications(db, log_fn: Optional[Callable[[str], None]] = None) -> None:
    """Send any queued report whose scheduled time has passed. Called before
    deciding what to do with the CURRENT run's report, so delivery stays
    timely even when several runs in a row land inside quiet hours."""
    try:
        pending = db.get_gsm_pending_telegram_notifications() or []
    except Exception:
        return
    if not pending:
        return

    now = datetime.now()
    remaining = []
    sent_any = False
    for item in pending:
        try:
            send_at = datetime.fromisoformat(str(item.get("send_at") or ""))
        except Exception:
            send_at = now  # malformed timestamp — send now rather than lose it
        if send_at > now:
            remaining.append(item)
            continue
        item_cfg = item.get("cfg") if isinstance(item.get("cfg"), dict) else {}
        item_events = item.get("events") if isinstance(item.get("events"), list) else []
        item_stats = item.get("stats") if isinstance(item.get("stats"), dict) else {}
        with contextlib.suppress(Exception):
            _send_telegram_detailed(db, item_cfg, item_events, item_stats)
        with contextlib.suppress(Exception):
            _send_telegram_summary(item_cfg, item_stats)
        sent_any = True
        if log_fn:
            with contextlib.suppress(Exception):
                log_fn(
                    f"Надіслано відкладене сповіщення (заплановано на {send_at.strftime('%d.%m %H:%M')})"
                )

    if sent_any:
        with contextlib.suppress(Exception):
            db.save_gsm_pending_telegram_notifications(remaining)


def _maybe_queue_or_send_telegram(
    db, cfg: dict, events: list, stats: dict, log_fn: Optional[Callable[[str], None]] = None
) -> None:
    """Send the Telegram report now, unless the run finished inside the
    configured "Не турбувати" quiet-hours window — in that case it's queued
    (see get/save_gsm_pending_telegram_notifications) and sent the next time
    the parser runs at or after the configured end time."""
    _flush_due_telegram_notifications(db, log_fn)

    telegram = cfg.get("telegram") if isinstance(cfg.get("telegram"), dict) else {}
    window_start = str(telegram.get("report_window_start") or "").strip()
    window_end = str(telegram.get("report_window_end") or "").strip()
    send_time = str(telegram.get("report_send_time") or "").strip()
    now = datetime.now()

    if (
        window_start
        and window_end
        and send_time
        and _in_quiet_window(now, window_start, window_end)
    ):
        send_at = _next_time_at_or_after(now, send_time)
        pending = []
        with contextlib.suppress(Exception):
            pending = db.get_gsm_pending_telegram_notifications() or []
        pending.append(
            {
                "send_at": send_at.isoformat(),
                "cfg": cfg,
                "events": events,
                "stats": stats,
            }
        )
        with contextlib.suppress(Exception):
            db.save_gsm_pending_telegram_notifications(pending)
        if log_fn:
            with contextlib.suppress(Exception):
                log_fn(
                    f"Сповіщення відкладено до {send_at.strftime('%d.%m %H:%M')}"
                    f" (вікно \"не турбувати\" {window_start}–{window_end})"
                )
        return

    with contextlib.suppress(Exception):
        _send_telegram_detailed(db, cfg, events, stats)
    _send_telegram_summary(cfg, stats)


async def gsm_local_tg_flush_loop(db, is_permitted) -> None:
    """Periodically flush due queued Telegram notifications while the LOCAL
    desktop app is open — this is what the VPS daemon's own tick already
    does (see gsm_parser_scheduler.py run_daemon), so a report queued during
    quiet hours goes out on time even when no new parser run happens to
    trigger `_maybe_queue_or_send_telegram`'s own flush-on-entry.

    `is_permitted` is a zero-arg callable re-checked every tick — pass one
    that returns True only for a superadmin session or a manager granted the
    "Запуск парсингу" GSM permission, so this loop doesn't send reports on
    behalf of a session that couldn't launch the parser itself.

    Both `is_permitted()` and `_flush_due_telegram_notifications` do
    synchronous DB calls (psycopg, blocking network I/O) — previously called
    directly here on the main asyncio event loop thread. If the shared
    connection ever needs a fresh TCP handshake/retry (idle timeout, VPN
    blip, laptop sleep/resume), that blocks the ENTIRE app's event loop —
    every click, every chart hover, every dialog close — for however long
    the OS takes to notice the connection is stuck, observed as 1.5-2 minute
    app-wide freezes every 3 minutes. Both now run via asyncio.to_thread so a
    slow/stuck DB round trip only stalls this background tick, never the UI."""
    await asyncio.sleep(30)
    while True:
        try:
            if await asyncio.to_thread(is_permitted):
                await asyncio.to_thread(_flush_due_telegram_notifications, db, None)
        except Exception:
            pass
        await asyncio.sleep(180)


def _normalize_parse_mode(parse_mode: str) -> str:
    mode = str(parse_mode or "full").strip().lower()
    if mode in {"gsm", "gsm_arena"}:
        return "gsm"
    if mode in {"ek", "ekatalog", "e-katalog"}:
        return "ekatalog"
    return "full"


class _UserStop(BaseException):
    """Raised inside run_gsm_parser when the user requests a stop (not caught by bare except Exception)."""


def run_gsm_parser(
    company_id: str,
    manager_id: str,
    role: str,
    parse_mode: str = "full",
    send_telegram: bool = True,
    progress_cb: Optional[Callable[[str], None]] = None,
    stop_event: Optional["threading.Event"] = None,
    pause_event: Optional["threading.Event"] = None,
):
    """
    Main entry point.
    - Reads parser config from DB.
    - Uses legacy GSM_Parser logic for parsing rules.
    - Saves results and change history in PostgreSQL.
    """
    _ = (manager_id, role)
    company_id = str(company_id or _GLOBAL_COMPANY_ID).strip() or _GLOBAL_COMPANY_ID
    mode = _normalize_parse_mode(parse_mode)

    start_ts = time.time()
    stats = {
        "gsm_written": 0,
        "ek_written": 0,
        "changed": 0,
        "errors": 0,
        "elapsed_sec": 0.0,
    }
    events: list[dict] = []

    # ── Per-run log capture — clear file at start, append each line immediately ──
    _ctx: dict = {"gsm": None, "ek": None, "gsm_ip": None, "ek_ip": None}
    _log_header = f"=== GSM Parser: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | mode={parse_mode} ==="
    with contextlib.suppress(Exception):
        LAST_PARSE_LOG_PATH.write_text(_log_header + "\n", encoding="utf-8")

    def _log(msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        with contextlib.suppress(Exception):
            with LAST_PARSE_LOG_PATH.open("a", encoding="utf-8") as _lf:
                _lf.write(line + "\n")
        # Also mirror to the DB so the desktop UI's "Логи" tab shows live
        # progress even when the parser is actually running on a different
        # machine (e.g. the VPS scheduler) — a local file written there
        # isn't visible from here. `db` is bound by the time any real
        # parsing (and therefore _log) runs; suppress covers the unlikely
        # case it isn't yet.
        with contextlib.suppress(Exception):
            db.append_gsm_parser_run_log(line)
        if progress_cb:
            with contextlib.suppress(Exception):
                progress_cb(msg)

    logger.info(
        "Parser start company=%s session=%s mode=%s",
        company_id,
        _PARSE_SESSION_ID,
        mode,
    )

    def _check_control():
        """Raise _UserStop if stop requested; block while paused."""
        if stop_event and stop_event.is_set():
            raise _UserStop()
        if pause_event:
            while not pause_event.is_set():
                if stop_event and stop_event.is_set():
                    raise _UserStop()
                time.sleep(0.3)

    def _smart_sleep(secs: float) -> None:
        """Sleep for `secs` seconds polling stop/pause every 2 s."""
        end = time.time() + max(0.0, secs)
        while time.time() < end:
            _check_control()
            remaining = end - time.time()
            time.sleep(min(2.0, max(0.0, remaining)))

    try:
        db = _ensure_db()
        with contextlib.suppress(Exception):
            db.reset_gsm_parser_run_log(_log_header)
        cfg = db.get_gsm_parser_global_config()
        rules = cfg.get("comparison") if isinstance(cfg.get("comparison"), dict) else {}
        settings = cfg.get("settings") if isinstance(cfg.get("settings"), dict) else {}
        devices_cfg = (
            settings.get("devices") if isinstance(settings.get("devices"), dict) else {}
        )
        delays = (
            settings.get("delays") if isinstance(settings.get("delays"), dict) else {}
        )
        try:
            gsm_recent_count = max(1, int(settings.get("gsm_recent_count") or 15))
        except Exception:
            gsm_recent_count = 15
        try:
            shops_appeared_max_age_months = max(
                1, min(120, int(settings.get("shops_appeared_max_age_months") or 8))
            )
        except Exception:
            shops_appeared_max_age_months = 8

        ek_on_sale_status_prefixes = _parse_status_list(
            settings.get("ek_on_sale_statuses"), _DEFAULT_ON_SALE_STATUS_PREFIXES
        )

        legacy_gsm = _legacy_gsm()
        legacy_ek = _legacy_ek()

        # Patch instant_print so legacy modules' output goes into the log
        _ctx["gsm"] = legacy_gsm
        _ctx["ek"] = legacy_ek
        _ctx["gsm_ip"] = getattr(legacy_gsm, "instant_print", None)
        _ctx["ek_ip"] = getattr(legacy_ek, "instant_print", None)
        legacy_gsm.instant_print = lambda t: _log(str(t))
        legacy_ek.instant_print = lambda t: _log(str(t))

        # Monkey-patch time.sleep inside the legacy GSM module so that long
        # per-device sleeps (12-22 s in parse_device_details) respond to
        # stop/pause events.  We replace only the `time` reference in that
        # module's namespace; the rest of the `time` module API is proxied.
        class _StopAwareTime:
            """Proxy for the `time` module used inside the legacy GSM module."""

            def sleep(self, secs: float) -> None:  # noqa: D102
                _smart_sleep(secs)

            def __getattr__(self, name: str):  # noqa: D105
                return getattr(time, name)

        legacy_gsm.time = _StopAwareTime()
        legacy_ek.time = _StopAwareTime()

        # 1) GSM Arena phase
        if mode in {"full", "gsm"}:
            # Primary source: gsm_parser_brands table.
            # Fallback: legacy config-stored list (for backward compat).
            db_brands = db.get_gsm_parser_brands()
            if db_brands:
                gsm_brands = [
                    {
                        "brand": r.get("brand") or "",
                        "url": r.get("gsm_url") or "",
                        "last_device": r.get("last_item") or "",
                        "enabled": r.get("enabled", True),
                    }
                    for r in db_brands
                ]
            else:
                gsm_brands = (
                    devices_cfg.get("gsm_arena")
                    if isinstance(devices_cfg.get("gsm_arena"), list)
                    else []
                )
            gsm_total = len(
                [
                    b
                    for b in gsm_brands
                    if isinstance(b, dict)
                    and b.get("brand")
                    and b.get("url")
                    and b.get("enabled", True)
                ]
            )

            # Reset cloudscraper session at the start of every parse run so stale
            # Cloudflare cookies from any previous run don't trigger a block.
            if gsm_total > 0:
                try:
                    cs_mod = getattr(legacy_gsm, "_cloudscraper", None)
                    if cs_mod:
                        legacy_gsm._scraper = cs_mod.create_scraper(
                            browser={"browser": "chrome", "platform": "windows", "mobile": False}
                        )
                        logger.info("GSM: scraper session reset at parse start")
                except Exception as reset_err:
                    logger.warning("GSM: could not reset scraper at start: %s", reset_err)

            # Warm up the cloudscraper session against GSM Arena's homepage
            # before hitting brand-specific pages.  This lets Cloudflare issue
            # a cf_clearance cookie once so subsequent brand requests are not
            # challenged and do not trigger a 429.
            if gsm_total > 0:
                _log("[1/2] GSM Arena: підключення до сайту…")
                try:
                    _check_control()
                    warmup_res = legacy_gsm.http_get(
                        "https://www.gsmarena.com/", timeout=30
                    )
                    if getattr(warmup_res, "status_code", 0) == 429:
                        _log(
                            "[1/2] GSM Arena: сайт тимчасово заблокований, пауза 5 хв…"
                        )
                        _smart_sleep(300)
                    else:
                        # Human-like pause between homepage and first brand page.
                        _smart_sleep(random.uniform(6.5, 12.5))
                except _UserStop:
                    raise
                except Exception:
                    pass

            # Shuffle brand order like the legacy script did — avoids always hitting
            # the same brand first with a cold Cloudflare session and prevents
            # Cloudflare from detecting a fixed request pattern.
            gsm_brands_shuffled = [
                b
                for b in gsm_brands
                if isinstance(b, dict)
                and b.get("brand")
                and b.get("url")
                and b.get("enabled", True)
            ]
            random.shuffle(gsm_brands_shuffled)

            _log("[1/2] GSM Arena ► Завантажуємо статус фото з БД…")
            _gsm_photo_status = db.get_gsm_devices_photo_status()
            _log(f"[1/2] GSM Arena ► Статус фото: {len(_gsm_photo_status)} пристроїв у БД")

            gsm_idx = 0
            _consecutive_blocks = 0
            for brand_row in gsm_brands_shuffled:
                brand = str(brand_row.get("brand") or "").strip()
                brand_url = str(brand_row.get("url") or "").strip()
                last_device = str(brand_row.get("last_device") or "").strip()

                gsm_idx += 1
                existing_count = _count_brand_rows(db, brand)
                existing_map = _existing_brand_url_map(db, brand)
                mode_label = "оновлення" if existing_count > 0 else "повний збір"
                _log(
                    f"[1/2] GSM Arena ► Бренд {gsm_idx}/{gsm_total}: {brand} "
                    f"({mode_label}, у БД: {existing_count}) — отримуємо список…"
                )

                logger.info("GSM brand start: %s (existing=%d)", brand, existing_count)

                # Between-brand navigation: visit a random GSM Arena catalogue page
                # before the next brand — varies the request pattern so Cloudflare
                # doesn't see the same makers.php3 hit before every single brand.
                nav_pages = [
                    "https://www.gsmarena.com/makers.php3",
                    "https://www.gsmarena.com/search.php3",
                    "https://www.gsmarena.com/network-bands.php3",
                    brand_url,  # revisit current brand listing (natural browsing)
                ]
                nav_url = random.choice(nav_pages)
                _log(f"[1/2] GSM Arena ► Навігаційна сторінка: {nav_url}")
                try:
                    _check_control()
                    legacy_gsm.http_get(nav_url, timeout=30)
                    nav_w = random.uniform(4.0, 9.6)
                    _log(f"[1/2] GSM Arena ► Пауза після навігації: {int(nav_w)}с")
                    _smart_sleep(nav_w)
                except _UserStop:
                    raise
                except Exception:
                    pass

                # Retry up to 2 times on anti-bot block with a long cooldown between attempts.
                links = "STOP"
                for _gsm_attempt in range(2):
                    if _gsm_attempt > 0:
                        wait_s = random.uniform(
                            float(delays.get("gsm_antibot_retry_min_sec", 180)),
                            float(delays.get("gsm_antibot_retry_max_sec", 360)),
                        )
                        _log(
                            f"[1/2] GSM Arena ► БЛОК на лістингу [{brand}] спроба {_gsm_attempt+1}"
                            f" — пауза {int(wait_s)}с, скидаємо сесію…"
                        )
                        _smart_sleep(wait_s)
                        # Reset cloudscraper session — old cookies may be flagged by Cloudflare.
                        try:
                            _cs = getattr(legacy_gsm, "_cloudscraper", None)
                            if _cs:
                                legacy_gsm._scraper = _cs.create_scraper(
                                    browser={
                                        "browser": "chrome",
                                        "platform": "windows",
                                        "mobile": False,
                                    }
                                )
                                _log("[1/2] GSM Arena ► Сесія скинута, повторний warmup…")
                        except Exception as _se:
                            logger.warning("GSM: could not reset scraper: %s", _se)
                        # Re-warmup with fresh session before hitting brand page.
                        try:
                            _check_control()
                            legacy_gsm.http_get("https://www.gsmarena.com/", timeout=30)
                            warmup_w = random.uniform(6.5, 12.5)
                            _log(f"[1/2] GSM Arena ► Warmup пауза: {int(warmup_w)}с")
                            _smart_sleep(warmup_w)
                        except _UserStop:
                            raise
                        except Exception:
                            pass

                    if existing_count > 0:
                        _log(f"[1/2] GSM Arena ► [{brand}] Режим: оновлення (бренд вже є, {existing_count} у БД) — перевіряємо перші {gsm_recent_count}…")
                        links = legacy_gsm.get_recent_device_links_for_brand(
                            brand_url, existing_map
                        )
                        # Known brand: only track the newest N listing entries each
                        # run (new releases + recent price refresh), N configured in
                        # "Налаштування" → "Кількість товару GSM Arena". Older devices
                        # in the brand's catalog are left as last recorded — no need
                        # to keep re-scraping the whole history every run.
                        if links != "STOP":
                            links = list(links)[:gsm_recent_count]
                    else:
                        _log(f"[1/2] GSM Arena ► [{brand}] Режим: повний збір — завантажуємо всі сторінки лістингу…")
                        links = legacy_gsm.get_all_device_links_for_brand(brand_url)

                    if links != "STOP":
                        _log(f"[1/2] GSM Arena ► [{brand}] Лістинг отримано: {len(links)} пристроїв")
                        break
                    _log(f"[1/2] GSM Arena ► БЛОК [{brand}] спроба {_gsm_attempt+1} — лістинг повернув STOP")
                    stats["errors"] += 1
                    logger.warning(
                        "GSM brand stopped (attempt %d): %s", _gsm_attempt + 1, brand
                    )

                if links == "STOP":
                    _consecutive_blocks += 1
                    if _consecutive_blocks >= 2:
                        wait_s = random.uniform(300.0, 600.0)
                        _log(
                            f"[1/2] GSM Arena ► {_consecutive_blocks} брендів поспіль заблоковано"
                            f" — IP заблоковано глобально, пауза {int(wait_s // 60)} хв…"
                        )
                        # Reset scraper session during the extended cooldown.
                        try:
                            _cs = getattr(legacy_gsm, "_cloudscraper", None)
                            if _cs:
                                legacy_gsm._scraper = _cs.create_scraper(
                                    browser={
                                        "browser": "chrome",
                                        "platform": "windows",
                                        "mobile": False,
                                    }
                                )
                        except Exception:
                            pass
                    else:
                        wait_s = random.uniform(
                            float(delays.get("gsm_brand_stop_min_sec", 120)),
                            float(delays.get("gsm_brand_stop_max_sec", 240)),
                        )
                        _log(
                            f"[1/2] GSM Arena: {brand} — пропускаємо. Пауза {int(wait_s)}с…"
                        )
                    _smart_sleep(wait_s)
                    continue

                _consecutive_blocks = 0  # reset on successful brand

                links_list = list(links or [])
                links_total = len(links_list)

                # If last_device is set, find its position in the URL list so we
                # can report the actual number of devices that will be processed.
                effective_total = links_total
                if last_device:
                    _last_slug = re.sub(r"[^a-z0-9]+", "_", last_device.lower().strip()).strip("_")
                    # Match slug as a complete segment: in GSM Arena URLs the slug is
                    # always followed by "-{id}.php", so "hot_10-" won't match "hot_10i-".
                    _last_slug_pat = re.compile(re.escape(_last_slug) + r"[-/.]")
                    for _ei, _el in enumerate(links_list, 1):
                        if _last_slug_pat.search(str(_el).lower()):
                            effective_total = _ei
                            break

                # Estimate: parse_device_details/parse_device_price_dates each start
                # with a hardcoded time.sleep(random.uniform(32.0, 56.0)) anti-bot
                # pause (avg ~44s, see GSM_Parser/main.py) — that dominates the real
                # per-device time, not the page fetch/parse itself. Add the configured
                # inter-device pause on top (default 3.2-6.4s).
                _page_min = float(delays.get("gsm_device_page_min_sec", 3.2))
                _page_max = float(delays.get("gsm_device_page_max_sec", 6.4))
                _avg_sec = int(44 + (_page_min + _page_max) / 2)
                _est_min = effective_total * _avg_sec // 60
                _est_h = _est_min // 60
                _est_m = _est_min % 60
                _time_str = f"{_est_h}год {_est_m}хв" if _est_h else f"{_est_m}хв"
                _log(
                    f"[1/2] GSM Arena: {brand} — знайдено {effective_total} пристроїв"
                    + (f" (з {links_total})" if effective_total != links_total else "")
                    + f" | орієнтовний час: ~{_time_str} (~{_avg_sec}с/пристрій)"
                )
                if last_device:
                    _log(f"[1/2] GSM Arena: {brand} — зупинка після: «{last_device}»")

                _device_stop = False
                # Counter-based human breaks: after a random 8-12 devices, take a
                # 5-12 s extra pause. Each time the break fires, the NEXT interval
                # is re-randomised — gives true stochastic spacing (8, 19, 27, 38…).
                _gsm_break_counter = 0
                _gsm_next_break = random.randint(8, 12)
                for link_idx, link in enumerate(links_list, 1):
                    _gsm_break_counter += 1
                    if _gsm_break_counter >= _gsm_next_break:
                        _wb = random.uniform(4.0, 9.6)
                        _log(f"[1/2] GSM Arena: {brand} — людська пауза {int(_wb)}с після {_gsm_break_counter} пристроїв…")
                        _smart_sleep(_wb)
                        _gsm_break_counter = 0
                        _gsm_next_break = random.randint(8, 12)

                    link_norm = link.strip().lower().rstrip("/")
                    _pstat = _gsm_photo_status.get(link_norm)
                    model_name = ""

                    if _pstat and _pstat["has_main"]:
                        # ── Lightweight path: device already in DB with main photo ──
                        # Only fetch price + dates (1 request). Gallery topped up if needed.
                        pdata = legacy_gsm.parse_device_price_dates(link)
                        if pdata == "STOP":
                            stats["errors"] += 1
                            _consecutive_blocks += 1
                            if _consecutive_blocks >= 2:
                                wait_s = random.uniform(300.0, 600.0)
                                _log(
                                    f"[1/2] GSM Arena ► КРИТИЧНИЙ БАН (легкий режим) [{brand}]"
                                    f" пристрій {link_idx}/{effective_total}, {_consecutive_blocks} блоків поспіль"
                                    f" — IP заблоковано глобально, пауза {int(wait_s // 60)} хв…"
                                )
                            else:
                                wait_s = random.uniform(
                                    float(delays.get("gsm_antibot_retry_min_sec", 180)),
                                    float(delays.get("gsm_antibot_retry_max_sec", 360)),
                                )
                                _log(
                                    f"[1/2] GSM Arena ► БЛОК (легкий режим) [{brand}]"
                                    f" пристрій {link_idx}/{effective_total} — пауза {int(wait_s)}с…"
                                )
                            _smart_sleep(wait_s)
                            try:
                                _cs = getattr(legacy_gsm, "_cloudscraper", None)
                                if _cs:
                                    legacy_gsm._scraper = _cs.create_scraper(
                                        browser={"browser": "chrome", "platform": "windows", "mobile": False}
                                    )
                            except Exception:
                                pass
                            _device_stop = True
                            break

                        model_name = _pstat["model_name"]
                        if pdata:
                            dev_id = _pstat["device_id"]
                            changed = db.update_gsm_device_price_and_dates(
                                dev_id,
                                pdata["price_raw"],
                                pdata["price_uah"],
                                pdata["announced"],
                                pdata["released"],
                                device_order=link_idx,
                            )
                            stats["gsm_written"] += 1
                            stats["changed"] += int(bool(changed))
                            # Top up gallery if GSM Arena now has more photos than we stored
                            g_count = _pstat["gallery_count"]
                            if g_count < 7:
                                new_photos = legacy_gsm.fetch_incremental_gallery_photos(link, g_count)
                                if new_photos:
                                    added = db.add_gsm_device_gallery_photos(dev_id, new_photos, g_count)
                                    if added:
                                        _gsm_photo_status[link_norm]["gallery_count"] = g_count + added
                            _status_lbl = ("оновлено (" + ", ".join(changed.keys()) + ")") if changed else "без змін"
                            _log(
                                f"[1/2] GSM Arena ► [{brand}] {model_name}"
                                f" ({link_idx}/{effective_total}) — {_status_lbl}"
                                f" | всього записано: {stats['gsm_written']}"
                            )
                    else:
                        # ── Full path: new device or device without main photo ──
                        info = legacy_gsm.parse_device_details(link, brand)
                        if info == "STOP":
                            stats["errors"] += 1
                            _consecutive_blocks += 1
                            if _consecutive_blocks >= 2:
                                wait_s = random.uniform(300.0, 600.0)
                                _log(
                                    f"[1/2] GSM Arena ► КРИТИЧНИЙ БАН на сторінці пристрою [{brand}]"
                                    f" пристрій {link_idx}/{effective_total}, {_consecutive_blocks} блоків поспіль"
                                    f" — IP заблоковано глобально, пауза {int(wait_s // 60)} хв…"
                                )
                            else:
                                wait_s = random.uniform(
                                    float(delays.get("gsm_antibot_retry_min_sec", 180)),
                                    float(delays.get("gsm_antibot_retry_max_sec", 360)),
                                )
                                _log(
                                    f"[1/2] GSM Arena ► БЛОК на сторінці пристрою [{brand}]"
                                    f" пристрій {link_idx}/{effective_total} — пауза {int(wait_s)}с…"
                                )
                            _smart_sleep(wait_s)
                            try:
                                _cs = getattr(legacy_gsm, "_cloudscraper", None)
                                if _cs:
                                    legacy_gsm._scraper = _cs.create_scraper(
                                        browser={"browser": "chrome", "platform": "windows", "mobile": False}
                                    )
                                    _log("[1/2] GSM Arena ► Сесія скинута після блоку на сторінці пристрою…")
                            except Exception as _se:
                                logger.warning("GSM: could not reset scraper after device-page block: %s", _se)
                            _device_stop = True
                            break
                        if not info or not isinstance(info, list):
                            continue

                        model_name = str(info[1] if len(info) > 1 else "").strip()
                        payload = _map_gsm_info_to_payload(info, rules)
                        payload["device_order"] = link_idx
                        ok, dev_id, ch, status, _extra_status = _upsert_device(
                            db, payload, shops_appeared_max_age_months
                        )
                        if ok:
                            stats["gsm_written"] += 1
                            stats["changed"] += int(ch)
                            main_photo = info[23] if len(info) > 23 else None
                            gallery_photos = info[24] if len(info) > 24 else []
                            if main_photo or gallery_photos:
                                with contextlib.suppress(Exception):
                                    db.save_gsm_device_photos(dev_id, main_photo, gallery_photos)
                            # Register in photo_status so subsequent runs know this device has photos
                            _gsm_photo_status[link_norm] = {
                                "device_id": dev_id,
                                "model_name": model_name,
                                "has_main": bool(main_photo),
                                "gallery_count": len(gallery_photos) if gallery_photos else 0,
                                "announced_empty": not str((payload.get("specs") or {}).get("announced") or "").strip(),
                                "released_empty":  not str((payload.get("specs") or {}).get("released") or "").strip(),
                            }
                            status_label = {"new": "НОВИЙ", "updated": "оновлено", "seen": "без змін"}.get(status, status)
                            _log(
                                f"[1/2] GSM Arena ► [{brand}] {model_name}"
                                f" ({link_idx}/{effective_total}) — {status_label}"
                                f" | всього записано: {stats['gsm_written']}"
                            )
                            if status == "new":
                                # This device may already exist as a
                                # standalone E-Katalog row (found on EK
                                # before GSM Arena ever listed it) — link
                                # them instead of leaving two rows for the
                                # same phone. Best-effort, never fatal.
                                _link_new_gsm_device_to_standalone_ek(
                                    db,
                                    dev_id,
                                    str(payload.get("brand") or ""),
                                    str(payload.get("device_type") or ""),
                                    model_name,
                                    rules,
                                )
                            if status in {"new", "updated"}:
                                if status == "new" and str(payload.get("brand") or "").strip().lower() == "xiaomi":
                                    _r = _xiaomi_regions_str(db, dev_id)
                                    if _r:
                                        payload.setdefault("specs", {})["regions"] = _r
                                events.append({
                                    "source": SOURCE_GSM,
                                    "status": status,
                                    "changes": int(ch),
                                    "device_id": dev_id,
                                    "payload": payload,
                                })
                        else:
                            stats["errors"] += 1
                            _log(f"[1/2] GSM Arena ► [{brand}] {model_name} — помилка запису в БД")

                    if last_device and model_name and " ".join(model_name.lower().split()) == " ".join(
                        last_device.lower().split()
                    ):
                        _log(f"[1/2] GSM Arena ► [{brand}] Досягнуто last_device: {last_device}, зупиняємо бренд")
                        break

                    _sleep_between(
                        delays,
                        "gsm_device_page_min_sec",
                        "gsm_device_page_max_sec",
                        3.2,
                        6.4,
                    )
                    _check_control()

                # Between-brand delay — let the IP cool down after many device-page
                # requests before hitting the next brand's listing.
                if not _device_stop:
                    bb_min = int(delays.get("gsm_brand_min_sec", 36))
                    bb_max = int(delays.get("gsm_brand_max_sec", 72))
                    _log(f"[1/2] GSM Arena ► [{brand}] Бренд завершено. Пауза між брендами {bb_min}-{bb_max}с…")
                    _sleep_between(
                        delays,
                        "gsm_brand_min_sec",
                        "gsm_brand_max_sec",
                        36.0,
                        72.0,
                    )
                _check_control()

        # 2) E-Katalog phase — flat iteration like old 111ekatalog.py run_ekatalog_sync()
        if mode in {"full", "ekatalog"}:
            db_brands = db.get_gsm_parser_brands() or []

            # Build map: (BRAND_UPPER, dtype) → list_url, and original-case name map
            ek_url_by_key: dict[tuple[str, str], str] = {}
            _brand_name_by_upper: dict[str, str] = {}
            # Brands with no GSM Arena URL at all (e.g. Xiaomi Mi Band — GSM
            # Arena doesn't list fitness trackers) are scraped E-Katalog-only:
            # every listing item becomes its own device instead of being
            # matched against a GSM Arena row.
            _brand_gsm_url_by_upper: dict[str, str] = {}
            for br in db_brands:
                bname = str(br.get("brand") or "").strip()
                if not bname or not bool(br.get("enabled", True)):
                    continue
                _brand_name_by_upper[bname.upper()] = bname
                _brand_gsm_url_by_upper[bname.upper()] = str(
                    br.get("gsm_url") or ""
                ).strip()
                for field, dtype in (
                    ("ek_url_phones", "Smartphone"),
                    ("ek_url_tablets", "Tablet"),
                    ("ek_url_watches", "Watch"),
                ):
                    url_val = str(br.get(field) or "").strip()
                    if url_val and url_val.startswith("http"):
                        ek_url_by_key[(bname.upper(), dtype)] = url_val

            if not ek_url_by_key:
                logger.warning(
                    "[EK] No brand list URLs configured — skipping EK phase. "
                    "Add URLs in the Brands settings tab."
                )
            else:
                # Warmup on EK homepage before hitting brand listing pages.
                try:
                    _check_control()
                    _log("[2/2] ЄК: відкриваємо головну сторінку (warmup)…")
                    legacy_ek.EK_HTTP.get(
                        legacy_ek.EKATALOG_HOME, timeout=25, max_retries=2
                    )
                    _smart_sleep(random.uniform(5.0, 12.0))
                except _UserStop:
                    raise
                except Exception:
                    pass

                # ── Inline: load listing → immediately process devices of that type ──
                all_gsm_rows = _load_all_gsm_rows_for_ek(db)
                _log(f"[2/2] ЄК ► Знайдено {len(all_gsm_rows)} пристроїв GSM для обробки")

                # Group GSM devices by (BRAND_UPPER, dev_type_label)
                gsm_by_key: dict[tuple[str, str], list] = {}
                for _r in all_gsm_rows:
                    _b = str(_r.get("brand") or "").upper()
                    _dt, _ = _type_to_cat(str(_r.get("device_type") or ""))
                    gsm_by_key.setdefault((_b, _dt), []).append(_r)

                _ek_list_idx = 0
                for (brand_upper, dtype), list_url in ek_url_by_key.items():
                    _check_control()
                    bname = _brand_name_by_upper.get(brand_upper, brand_upper)
                    brand_has_gsm = bool(_brand_gsm_url_by_upper.get(brand_upper, ""))

                    # Skip if no GSM devices of this brand/type in DB — unless the
                    # brand has no GSM Arena URL at all, in which case E-Katalog
                    # is the only source and we always process its listing.
                    devices_for_type = gsm_by_key.get((brand_upper, dtype), [])
                    if brand_has_gsm and not devices_for_type:
                        _log(
                            f"[2/2] ЄК ► Пропускаємо {bname}/{dtype}:"
                            f" немає пристроїв GSM в БД"
                        )
                        continue

                    # Pause between brand/type combos
                    if _ek_list_idx > 0:
                        ek_list_w = random.uniform(5.0, 10.0)
                        _log(
                            f"[2/2] ЄК ► Пауза між обробками: {int(ek_list_w)}с…"
                        )
                        _smart_sleep(ek_list_w)
                    _ek_list_idx += 1

                    # Load listing for this brand/type
                    _log(
                        f"[2/2] ЄК ► Завантаження лістингу: {bname} / {dtype} ({list_url})"
                    )
                    candidate_links: list[dict] = []
                    for _ek_list_attempt in range(2):
                        if _ek_list_attempt > 0:
                            ek_block_wait = random.uniform(
                                float(delays.get("ek_antibot_retry_min_sec", 120)),
                                float(delays.get("ek_antibot_retry_max_sec", 240)),
                            )
                            _log(
                                f"[2/2] ЄК ► БЛОК лістингу [{bname}/{dtype}] спроба {_ek_list_attempt+1}"
                                f" — пауза {int(ek_block_wait)}с, скидаємо сесію…"
                            )
                            _smart_sleep(ek_block_wait)
                            _ek_reset_session(legacy_ek)
                            _log("[2/2] ЄК ► Сесія скинута, повторюємо лістинг…")
                        result = _get_all_brand_links(
                            legacy_ek, list_url, sleep_fn=_smart_sleep
                        )
                        if result == "STOP":
                            _log(
                                f"[2/2] ЄК ► [{bname}/{dtype}] Лістинг повернув STOP"
                                f" (спроба {_ek_list_attempt+1})"
                            )
                            stats["errors"] += 1
                        else:
                            candidate_links = result
                            break

                    if not brand_has_gsm:
                        # EK-PRIMARY MODE: brand has no GSM Arena URL configured at
                        # all (e.g. Xiaomi Mi Band — GSM Arena doesn't list fitness
                        # trackers). E-Katalog is the only source: every listing
                        # item becomes its own device, matched to nothing.

                        # Superadmin-added manual links (items missing from the
                        # auto-crawled listing page) are merged in here so they get
                        # parsed/refreshed on every run just like a normal listing
                        # item, instead of needing separate one-off handling.
                        with contextlib.suppress(Exception):
                            _existing_urls = {
                                str(c.get("url") or "").strip().lower().rstrip("/")
                                for c in candidate_links
                            }
                            for _ml in db.get_gsm_manual_ek_links(bname, dtype) or []:
                                _ml_url = str(_ml.get("url") or "").strip()
                                if _ml_url and _ml_url.lower().rstrip("/") not in _existing_urls:
                                    candidate_links.append({"url": _ml_url})
                                    _existing_urls.add(_ml_url.lower().rstrip("/"))

                        _log(
                            f"[2/2] ЄК ► [{bname}/{dtype}] Бренд без GSM Arena —"
                            f" збираємо напряму {len(candidate_links)} позицій з лістингу…"
                        )
                        for ek_idx, cand in enumerate(candidate_links, 1):
                            _check_control()
                            link = str(cand.get("url") or "")
                            cand_title = str(cand.get("title") or link).strip()
                            if not link:
                                continue
                            try:
                                tmp = legacy_ek.parse_ekatalog_item(
                                    link, "", dtype, skip_net_check=True
                                )
                            except Exception as exc:
                                _log(
                                    f"[2/2] ЄК ► [{bname}] {cand_title}"
                                    f" ({ek_idx}/{len(candidate_links)}) — помилка: {exc}"
                                )
                                _smart_sleep(random.uniform(2.0, 5.0))
                                continue
                            if not tmp or "fail_net" in tmp:
                                _smart_sleep(random.uniform(2.0, 5.0))
                                continue

                            ek_model_name = str(tmp.get("W") or cand_title).strip()
                            if not ek_model_name:
                                _smart_sleep(random.uniform(2.0, 5.0))
                                continue

                            _ek_date_raw = str(tmp.get("Y") or "")
                            _ek_year, _ek_month = _parse_ek_date_added(_ek_date_raw)
                            ek_specs = {
                                "date_added": _ek_date_raw,
                                "date_added_year": _ek_year,
                                "date_added_month": _ek_month,
                                "dimensions": str(tmp.get("Z") or ""),
                                "length": str(tmp.get("AA") or ""),
                                "width": str(tmp.get("AB") or ""),
                                "thickness": str(tmp.get("AC") or ""),
                                "weight": str(tmp.get("AD") or ""),
                                "display": str(tmp.get("AE") or ""),
                                "os": str(tmp.get("AF") or ""),
                                "cpu": str(tmp.get("AG") or ""),
                                "memory": str(tmp.get("AH") or ""),
                                "battery": str(tmp.get("AI") or ""),
                                "status": str(tmp.get("AJ") or ""),
                                "price_low": str(tmp.get("AK") or ""),
                                "price_min": str(tmp.get("AL") or ""),
                                "price_max": str(tmp.get("AM") or ""),
                                "ek_url": link,
                                "shops_count": str(tmp.get("AK") or ""),
                                "variations": {
                                    "AP": str(tmp.get("AP") or ""),
                                    "AQ": str(tmp.get("AQ") or ""),
                                    "AR": str(tmp.get("AR") or ""),
                                    "AS": str(tmp.get("AS") or ""),
                                    "AT": str(tmp.get("AT") or ""),
                                },
                            }
                            ek_payload = {
                                "source": SOURCE_EK,
                                "brand": bname,
                                "device_type": dtype,
                                "model_name": ek_model_name,
                                "model_key": _normalize_model_name(ek_model_name, rules),
                                "source_url": link,
                                "image_url": str(tmp.get("PHOTO_MAIN") or ""),
                                "specs": ek_specs,
                            }

                            ok, dev_id, ch, ev_status, ev_extra_status = _upsert_device(
                                db, ek_payload, shops_appeared_max_age_months,
                                ek_on_sale_status_prefixes,
                            )
                            if ok:
                                stats["ek_written"] += 1
                                stats["changed"] += int(ch)
                                _record_daily_history(db, dev_id, SOURCE_EK, ek_specs)
                                if ev_status in {"new", "updated"}:
                                    with contextlib.suppress(Exception):
                                        _save_ek_primary_photos(
                                            legacy_ek, db, dev_id, tmp
                                        )
                                ek_st_label = {
                                    "new": "НОВИЙ",
                                    "updated": "оновлено",
                                    "shops_appeared": "з'явились магазини",
                                    "seen": "без змін",
                                }.get(ev_status, ev_status)
                                if ev_extra_status:
                                    ek_st_label += " + з'явились магазини"
                                _log(
                                    f"[2/2] ЄК ► [{bname}] {ek_model_name}"
                                    f" ({ek_idx}/{len(candidate_links)}) — {ek_st_label}"
                                    f" | всього ЄК: {stats['ek_written']}"
                                )
                                if ev_status in {"new", "updated", "shops_appeared"}:
                                    if ev_status in {"new", "shops_appeared"} and str(ek_payload.get("brand") or "").strip().lower() == "xiaomi":
                                        _r = _xiaomi_regions_str(db, dev_id)
                                        if _r:
                                            ek_payload.setdefault("specs", {})["regions"] = _r
                                    events.append(
                                        {
                                            "source": SOURCE_EK,
                                            "status": ev_status,
                                            "changes": int(ch),
                                            "device_id": dev_id,
                                            "payload": ek_payload,
                                        }
                                    )
                                if ev_extra_status == "shops_appeared" and ev_status != "shops_appeared":
                                    events.append(
                                        {
                                            "source": SOURCE_EK,
                                            "status": "shops_appeared",
                                            "changes": int(ch),
                                            "device_id": dev_id,
                                            "payload": ek_payload,
                                        }
                                    )
                            else:
                                stats["errors"] += 1
                                _log(
                                    f"[2/2] ЄК ► [{bname}] {ek_model_name}"
                                    f" — помилка запису в БД"
                                )

                            _smart_sleep(random.uniform(2.0, 5.0))
                        continue

                    _log(
                        f"[2/2] ЄК ► [{bname}/{dtype}] Лістинг: {len(candidate_links)} посилань."
                        f" Обробляємо {len(devices_for_type)} пристроїв…"
                    )

                    # Process all GSM devices of this brand/type inline
                    # URLs matched to a GSM device below are tracked here so
                    # the leftover-candidates pass after this loop (see
                    # "EK devices with no GSM Arena match yet" below) only
                    # ever picks up genuinely unmatched listing items.
                    matched_ek_urls: set[str] = set()
                    for ek_idx, row in enumerate(devices_for_type, 1):
                        _check_control()

                        brand = str(row.get("brand") or "").strip()
                        model_name = str(row.get("model_name") or "").strip()
                        if not model_name:
                            continue
                        gsm_id = str(row.get("id") or "")
                        gsm_specs = (
                            row.get("specs")
                            if isinstance(row.get("specs"), dict)
                            else {}
                        )
                        dev_type_label, _ = _type_to_cat(
                            str(row.get("device_type") or "")
                        )
                        target_net = _extract_target_net(gsm_specs)

                        # A superadmin-pinned URL (set from the device card when
                        # auto-matching found nothing or the wrong page) always
                        # wins over whatever the previous run auto-matched.
                        # Read fresh from the DB, not from gsm_specs — `row` is
                        # part of a snapshot taken once before this whole phase
                        # started, so a pin set while THIS run is already in
                        # progress must not be missed (see _get_manual_ek_url).
                        manual_ek_url = _get_manual_ek_url(db, gsm_id)
                        existing_url = manual_ek_url or _get_ek_existing_url(db, gsm_id)
                        is_manual = bool(manual_ek_url) and existing_url == manual_ek_url
                        parsed: Optional[dict] = None
                        item_url = ""
                        is_update = bool(
                            existing_url and existing_url.startswith("http")
                        )

                        # UPDATE MODE: re-parse existing (or manually pinned) EK page
                        if is_update:
                            _log(
                                f"[2/2] ЄК ► [{brand}] {model_name}"
                                f" ({ek_idx}/{len(devices_for_type)})"
                                f" — {'РУЧНЕ ПОСИЛАННЯ' if is_manual else 'ОНОВЛЕННЯ'}"
                            )
                            try:
                                _check_control()
                                tmp = legacy_ek.parse_ekatalog_item(
                                    existing_url,
                                    target_net,
                                    dev_type_label,
                                    skip_net_check=True,
                                )
                                if tmp and "fail_net" not in tmp:
                                    # A manual pin is trusted completely, no
                                    # comparison at all — that's the whole
                                    # point of pinning by hand. An AUTO-matched
                                    # existing link, though, gets re-validated
                                    # every run: without this, one wrong match
                                    # (a name-matching bug, or the site
                                    # reshuffling its own catalog) would keep
                                    # silently re-confirming itself forever,
                                    # since UPDATE MODE never used to check
                                    # identity again after the first match.
                                    site_title = str(tmp.get("W") or "")
                                    if is_manual or _ek_name_matches(
                                        legacy_ek, model_name, site_title
                                    ):
                                        parsed = tmp
                                        item_url = existing_url
                                        matched_ek_urls.add(
                                            item_url.strip().lower().rstrip("/")
                                        )
                                    else:
                                        _log(
                                            f"[2/2] ЄК ► [{brand}] {model_name}"
                                            f" — існуюче посилання веде на інший"
                                            f" товар ({site_title!r}), шукаємо заново"
                                        )
                                elif tmp and "fail_net" in tmp:
                                    _log(
                                        f"[2/2] ЄК ► [{brand}] {model_name}"
                                        f" — fail_net при оновленні, йдемо в пошук"
                                    )
                            except Exception as exc:
                                _log(
                                    f"[2/2] ЄК ► [{brand}] {model_name}"
                                    f" — помилка оновлення: {exc}"
                                )

                        # SEARCH MODE: walk listing links and match by name.
                        # NEVER for a manually-pinned device — is_manual means the
                        # superadmin explicitly chose this exact EK page, overriding
                        # whatever the name-matcher would otherwise pick. If the
                        # pinned URL's fetch failed this run (network hiccup, page
                        # temporarily down), falling through to name-matching here
                        # would silently replace it with an auto-matched candidate
                        # (possibly the very wrong page the pin was set to correct),
                        # discarding the override until someone notices. Skip and
                        # retry the pinned URL again next run instead.
                        if not parsed and is_manual:
                            _log(
                                f"[2/2] ЄК ► [{brand}] {model_name}"
                                f" — РУЧНЕ ПОСИЛАННЯ недоступне цього разу,"
                                f" пропускаємо (без пошуку за назвою)"
                            )
                        if not parsed and not is_manual:
                            if not candidate_links:
                                _log(
                                    f"[2/2] ЄК ► [{brand}] {model_name}"
                                    f" ({ek_idx}/{len(devices_for_type)})"
                                    f" — НЕ ЗНАЙДЕНО (немає посилань для цього типу)"
                                )
                                if not is_update:
                                    _smart_sleep(random.uniform(2.0, 5.0))
                                continue

                            # Cheap pre-filter: the listing page already renders each
                            # candidate's full title in the link text/`title` attr, so
                            # we can rule out non-matches without fetching (and, for
                            # matches, chasing every colour/size sub-page of) their
                            # full product page. Only candidates whose listing title
                            # already matches get a real HTTP request.
                            name_candidates = [
                                c
                                for c in candidate_links
                                if _ek_name_matches(
                                    legacy_ek,
                                    model_name,
                                    str(c.get("title") or ""),
                                )
                            ]
                            # Try an exact-year candidate before a no-year fallback
                            # (stable sort keeps listing order within each tier —
                            # a genuinely different year never reaches this list,
                            # it's already hard-rejected by _ek_name_matches above).
                            name_candidates.sort(
                                key=lambda c: _ek_year_match_tier(
                                    model_name, str(c.get("title") or "")
                                )
                            )
                            _log(
                                f"[2/2] ЄК ► [{brand}] {model_name}"
                                f" ({ek_idx}/{len(devices_for_type)})"
                                f" — ПОШУК: {len(name_candidates)} за назвою"
                                f" з {len(candidate_links)} кандидатів"
                            )
                            found = False
                            for cand in name_candidates:
                                link = str(cand.get("url") or "")
                                if not link:
                                    continue
                                try:
                                    _check_control()
                                    tmp = legacy_ek.parse_ekatalog_item(
                                        link, target_net, dev_type_label
                                    )
                                    if not tmp or "fail_net" in tmp:
                                        continue
                                    site_name = str(tmp.get("W") or "")
                                    if _ek_name_matches(
                                        legacy_ek, model_name, site_name
                                    ):
                                        parsed = tmp
                                        item_url = link
                                        found = True
                                        matched_ek_urls.add(
                                            item_url.strip().lower().rstrip("/")
                                        )
                                        _log(
                                            f"[2/2] ЄК ► [{brand}] {model_name}"
                                            f" — ЗНАЙДЕНО: {link}"
                                        )
                                        break
                                except Exception as exc:
                                    _log(
                                        f"[2/2] ЄК ► [{brand}] {model_name}"
                                        f" — помилка кандидата {link}: {exc}"
                                    )

                            if not found:
                                _log(
                                    f"[2/2] ЄК ► [{brand}] {model_name}"
                                    f" — НЕ ЗНАЙДЕНО в лістингу"
                                )

                        if not parsed or "fail_net" in parsed:
                            _smart_sleep(random.uniform(2.0, 5.0))
                            continue

                        # Build EK payload and save to DB.
                        # No photo scraping for E-Katalog — device photos come from GSM Arena
                        # only (see main_photo/gallery_photos in the GSM Arena phase above).
                        ek_specs = {
                            "target_net": target_net,
                            "date_added": str(parsed.get("Y") or ""),
                            "dimensions": str(parsed.get("Z") or ""),
                            "length": str(parsed.get("AA") or ""),
                            "width": str(parsed.get("AB") or ""),
                            "thickness": str(parsed.get("AC") or ""),
                            "weight": str(parsed.get("AD") or ""),
                            "display": str(parsed.get("AE") or ""),
                            "os": str(parsed.get("AF") or ""),
                            "cpu": str(parsed.get("AG") or ""),
                            "memory": str(parsed.get("AH") or ""),
                            "battery": str(parsed.get("AI") or ""),
                            "status": str(parsed.get("AJ") or ""),
                            "price_low": str(parsed.get("AK") or ""),
                            "price_min": str(parsed.get("AL") or ""),
                            "price_max": str(parsed.get("AM") or ""),
                            "ek_url": item_url,
                            "shops_count": str(parsed.get("AK") or ""),
                            "variations": {
                                "AP": str(parsed.get("AP") or ""),
                                "AQ": str(parsed.get("AQ") or ""),
                                "AR": str(parsed.get("AR") or ""),
                                "AS": str(parsed.get("AS") or ""),
                                "AT": str(parsed.get("AT") or ""),
                            },
                            "gsm_ref_model_key": str(row.get("model_key") or ""),
                            "gsm_ref_device_id": gsm_id,
                        }
                        ek_model_name = str(parsed.get("W") or model_name).strip()
                        ek_payload = {
                            "source": SOURCE_EK,
                            "brand": brand,
                            "device_type": dev_type_label,
                            "model_name": ek_model_name,
                            # Key EK rows by the matched GSM device's model_key, not by
                            # freshly scraped EK title text. The real identity link is
                            # gsm_ref_device_id (see _get_ek_existing_url); if the EK
                            # site title text drifts slightly between runs (spacing,
                            # capitalization, storage suffix order), re-deriving the key
                            # from that text here would miss the existing row and create
                            # a duplicate "new" device every run.
                            "model_key": str(row.get("model_key") or "").strip()
                            or _normalize_model_name(ek_model_name, rules),
                            "source_url": item_url,
                            "image_url": str(row.get("image_url") or ""),
                            "specs": ek_specs,
                        }

                        ok, dev_id, ch, ev_status, ev_extra_status = _upsert_device(
                            db, ek_payload, shops_appeared_max_age_months,
                            ek_on_sale_status_prefixes,
                        )
                        if ok:
                            stats["ek_written"] += 1
                            stats["changed"] += int(ch)
                            _record_daily_history(db, gsm_id, SOURCE_EK, ek_specs)
                            ek_st_label = {
                                "new": "НОВИЙ",
                                "updated": "оновлено",
                                "shops_appeared": "з'явились магазини",
                                "seen": "без змін",
                            }.get(ev_status, ev_status)
                            if ev_extra_status:
                                ek_st_label += " + з'явились магазини"
                            _log(
                                f"[2/2] ЄК ► [{brand}] {model_name}"
                                f" — {ek_st_label} | всього ЄК: {stats['ek_written']}"
                            )
                            if ev_status in {"new", "updated", "shops_appeared"}:
                                if ev_status in {"new", "shops_appeared"} and str(ek_payload.get("brand") or "").strip().lower() == "xiaomi":
                                    _r = _xiaomi_regions_str(db, dev_id)
                                    if _r:
                                        ek_payload.setdefault("specs", {})["regions"] = _r
                                events.append(
                                    {
                                        "source": SOURCE_EK,
                                        "status": ev_status,
                                        "changes": int(ch),
                                        "device_id": dev_id,
                                        "payload": ek_payload,
                                    }
                                )
                            if ev_extra_status == "shops_appeared" and ev_status != "shops_appeared":
                                events.append(
                                    {
                                        "source": SOURCE_EK,
                                        "status": "shops_appeared",
                                        "changes": int(ch),
                                        "device_id": dev_id,
                                        "payload": ek_payload,
                                    }
                                )
                        else:
                            stats["errors"] += 1
                            _log(
                                f"[2/2] ЄК ► [{brand}] {model_name}"
                                f" — помилка запису в БД"
                            )

                        _smart_sleep(random.uniform(2.0, 5.0))

                    # EK devices with no GSM Arena match yet: a brand can be
                    # tracked on GSM Arena overall while still missing a
                    # specific model that IS on E-Katalog (GSM Arena simply
                    # hasn't listed it, or listed it under a name the
                    # matcher above didn't accept). Previously every listing
                    # candidate that didn't match one of THIS brand/type's
                    # GSM devices was silently dropped every run — never
                    # stored, never shown. Store it as a standalone EK row
                    # instead (same shape as the EK-primary-brand path
                    # above, just for leftover candidates within an
                    # otherwise GSM-tracked brand) so it at least shows up
                    # with EK data and an empty GSM Arena side. If the
                    # device later appears on GSM Arena, the GSM Arena
                    # insert path (_link_new_gsm_device_to_standalone_ek)
                    # links it into this same row instead of leaving a
                    # separate duplicate.
                    leftover_candidates = [
                        c
                        for c in candidate_links
                        if str(c.get("url") or "").strip().lower().rstrip("/")
                        not in matched_ek_urls
                        and str(c.get("url") or "").strip()
                    ]
                    for ek_idx, cand in enumerate(leftover_candidates, 1):
                        _check_control()
                        link = str(cand.get("url") or "")
                        cand_title = str(cand.get("title") or link).strip()
                        try:
                            tmp = legacy_ek.parse_ekatalog_item(
                                link, "", dtype, skip_net_check=True
                            )
                        except Exception as exc:
                            _log(
                                f"[2/2] ЄК ► [{bname}] {cand_title}"
                                f" (без GSM, {ek_idx}/{len(leftover_candidates)})"
                                f" — помилка: {exc}"
                            )
                            _smart_sleep(random.uniform(2.0, 5.0))
                            continue
                        if not tmp or "fail_net" in tmp:
                            _smart_sleep(random.uniform(2.0, 5.0))
                            continue

                        ek_model_name = str(tmp.get("W") or cand_title).strip()
                        if not ek_model_name:
                            _smart_sleep(random.uniform(2.0, 5.0))
                            continue

                        _ek_date_raw = str(tmp.get("Y") or "")
                        _ek_year, _ek_month = _parse_ek_date_added(_ek_date_raw)
                        ek_specs = {
                            "date_added": _ek_date_raw,
                            "date_added_year": _ek_year,
                            "date_added_month": _ek_month,
                            "dimensions": str(tmp.get("Z") or ""),
                            "length": str(tmp.get("AA") or ""),
                            "width": str(tmp.get("AB") or ""),
                            "thickness": str(tmp.get("AC") or ""),
                            "weight": str(tmp.get("AD") or ""),
                            "display": str(tmp.get("AE") or ""),
                            "os": str(tmp.get("AF") or ""),
                            "cpu": str(tmp.get("AG") or ""),
                            "memory": str(tmp.get("AH") or ""),
                            "battery": str(tmp.get("AI") or ""),
                            "status": str(tmp.get("AJ") or ""),
                            "price_low": str(tmp.get("AK") or ""),
                            "price_min": str(tmp.get("AL") or ""),
                            "price_max": str(tmp.get("AM") or ""),
                            "ek_url": link,
                            "shops_count": str(tmp.get("AK") or ""),
                            "variations": {
                                "AP": str(tmp.get("AP") or ""),
                                "AQ": str(tmp.get("AQ") or ""),
                                "AR": str(tmp.get("AR") or ""),
                                "AS": str(tmp.get("AS") or ""),
                                "AT": str(tmp.get("AT") or ""),
                            },
                        }
                        ek_payload = {
                            "source": SOURCE_EK,
                            "brand": bname,
                            "device_type": dtype,
                            "model_name": ek_model_name,
                            "model_key": _normalize_model_name(ek_model_name, rules),
                            "source_url": link,
                            "image_url": str(tmp.get("PHOTO_MAIN") or ""),
                            "specs": ek_specs,
                        }

                        ok, dev_id, ch, ev_status, ev_extra_status = _upsert_device(
                            db, ek_payload, shops_appeared_max_age_months,
                            ek_on_sale_status_prefixes,
                        )
                        if ok:
                            stats["ek_written"] += 1
                            stats["changed"] += int(ch)
                            _record_daily_history(db, dev_id, SOURCE_EK, ek_specs)
                            if ev_status in {"new", "updated"}:
                                with contextlib.suppress(Exception):
                                    _save_ek_primary_photos(
                                        legacy_ek, db, dev_id, tmp
                                    )
                            ek_st_label = {
                                "new": "НОВИЙ (без GSM)",
                                "updated": "оновлено (без GSM)",
                                "shops_appeared": "з'явились магазини",
                                "seen": "без змін",
                            }.get(ev_status, ev_status)
                            _log(
                                f"[2/2] ЄК ► [{bname}] {ek_model_name}"
                                f" (без GSM, {ek_idx}/{len(leftover_candidates)})"
                                f" — {ek_st_label} | всього ЄК: {stats['ek_written']}"
                            )
                            if ev_status in {"new", "updated", "shops_appeared"}:
                                events.append(
                                    {
                                        "source": SOURCE_EK,
                                        "status": ev_status,
                                        "changes": int(ch),
                                        "device_id": dev_id,
                                        "payload": ek_payload,
                                    }
                                )
                        else:
                            stats["errors"] += 1
                            _log(
                                f"[2/2] ЄК ► [{bname}] {ek_model_name}"
                                f" (без GSM) — помилка запису в БД"
                            )

                        _smart_sleep(random.uniform(2.0, 5.0))

        with contextlib.suppress(Exception):
            db.update_gsm_parser_run_timestamp(datetime.now().isoformat())

        # Record the trigger after every run (not just when something
        # changed) — every user/admin/superadmin session polls this via
        # _needs_reload() and force-refreshes its local cache from the DB
        # the next time it's looked at, so nobody is left staring at a
        # stale pre-parse snapshot until the next 9:00 boundary.
        with contextlib.suppress(Exception):
            db.set_gsm_parse_trigger(stats["changed"])

        # Sync Xiaomi regional variants from GitHub after every successful
        # run — BEFORE the Telegram send below, so a device that just got
        # regions for the first time can be folded into this run's report:
        # merged into its "new device" / "shops appeared" card if it's
        # already getting one this run, otherwise sent as its own
        # standalone "regions appeared" card.
        try:
            from xiaomi_regions_sync import sync_xiaomi_regions
            _log("Синхронізація регіонів Xiaomi…")
            _xsync = sync_xiaomi_regions(db, log_fn=_log)
            if _xsync.get("not_found"):
                logger.info(
                    "[XiaomiSync] %d devices not matched in DB",
                    _xsync["not_found"],
                )
            _covered_device_ids = {
                str(e.get("device_id") or "")
                for e in events
                if str(e.get("status")) in {"new", "shops_appeared"}
            }
            for _entry in _xsync.get("newly_appeared") or []:
                _did = str(_entry.get("device_id") or "")
                if not _did:
                    continue
                if _did in _covered_device_ids:
                    for _e in events:
                        if (
                            str(_e.get("device_id") or "") == _did
                            and str(_e.get("status")) in {"new", "shops_appeared"}
                        ):
                            _p = _e.get("payload") if isinstance(_e.get("payload"), dict) else {}
                            _p.setdefault("specs", {})["regions"] = _entry.get("regions") or ""
                            _e["payload"] = _p
                    continue
                events.append(
                    {
                        "source": SOURCE_XIAOMI_REGIONS,
                        "status": "regions_appeared",
                        "changes": 1,
                        "device_id": _did,
                        "payload": {
                            "source": _entry.get("source") or SOURCE_GSM,
                            "brand": "Xiaomi",
                            "device_type": _entry.get("device_type") or "",
                            "model_name": _entry.get("model_name") or "",
                            "source_url": _entry.get("source_url") or "",
                            "image_url": _entry.get("image_url") or "",
                            "specs": {
                                "network_full": _entry.get("network_full") or "",
                                "regions": _entry.get("regions") or "",
                            },
                        },
                    }
                )
        except Exception as _xe:
            logger.warning("[XiaomiSync] sync failed: %s", _xe)

        stats["elapsed_sec"] = time.time() - start_ts
        tg_enabled = bool((cfg.get("telegram") or {}).get("tg_enabled", True))
        if bool(send_telegram) and tg_enabled:
            _maybe_queue_or_send_telegram(db, cfg, events, stats, _log)

        msg = (
            "GSM Parser: complete | "
            f"mode={mode} "
            f"gsm={stats['gsm_written']} ek={stats['ek_written']} "
            f"changed={stats['changed']} errors={stats['errors']} "
            f"time={stats['elapsed_sec']:.1f}s"
        )
        _log(
            f"Готово: {stats['gsm_written']} GSM + {stats['ek_written']} ЄК, "
            f"змін: {stats['changed']}, час: {stats['elapsed_sec']:.0f}с"
        )
        logger.info(msg)

        return True, msg

    except _UserStop:
        _log("Парсинг зупинено користувачем")
        logger.info("Parser stopped by user request")
        return False, "Зупинено"

    except Exception as exc:
        _log(f"ПОМИЛКА: {exc}")
        logger.exception("Parser failed: %s", exc)
        # Best-effort: still deliver/queue whatever partial report we have.
        # An unexpected crash mid-run must never mean total silence — this is
        # exactly what happened when a single bad device used to abort the
        # whole run before the Telegram step (below) was ever reached.
        with contextlib.suppress(Exception):
            _db = locals().get("db")
            _cfg = locals().get("cfg")
            if _db is not None and _cfg is not None:
                stats["elapsed_sec"] = time.time() - start_ts
                stats["errors"] = int(stats.get("errors") or 0) + 1
                tg_enabled = bool((_cfg.get("telegram") or {}).get("tg_enabled", True))
                if bool(send_telegram) and tg_enabled:
                    _maybe_queue_or_send_telegram(_db, _cfg, events, stats, _log)
        return False, f"GSM Parser failure: {exc}"

    finally:
        # Restore instant_print on both legacy modules
        with contextlib.suppress(Exception):
            if _ctx["gsm"] and _ctx["gsm_ip"]:
                _ctx["gsm"].instant_print = _ctx["gsm_ip"]
        with contextlib.suppress(Exception):
            if _ctx["ek"] and _ctx["ek_ip"]:
                _ctx["ek"].instant_print = _ctx["ek_ip"]
        # Append terminator line
        with contextlib.suppress(Exception):
            with LAST_PARSE_LOG_PATH.open("a", encoding="utf-8") as lf:
                lf.write(f"[{datetime.now().strftime('%H:%M:%S')}] === Кінець ===\n")


# ---------------------------------------------------------------------------
# Legacy UI compatibility for the parser tab.
# ---------------------------------------------------------------------------


def _direct_gsm_sheet_url() -> str:
    env_url = (os.getenv("GSM_SHEET_URL") or "").strip()
    if not env_url:
        env_url = "https://docs.google.com/spreadsheets/d/1WpkunksJZnJbaT9W62o5uWgR5tTD9AUq8PG23lys3Ok/edit?gid=0#gid=0"

    url = (
        env_url.replace("/edit?", "/view?")
        .replace("/edit#", "/view#")
        .replace("/edit", "/view")
    )
    if "rm=minimal" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}rm=minimal&single=true&widget=true&headers=false"
    return url


def get_gsm_sheet_view_url(
    worksheet_name: str = "Лист1",
    spreadsheet_name: str = "GSM PARSER",
    filter_view_id: Optional[int] = None,
) -> str:
    _ = (worksheet_name, spreadsheet_name, filter_view_id)
    return _direct_gsm_sheet_url()


def prepare_gsm_sheet_bot_view(
    worksheet_name: str = "Лист1",
    spreadsheet_name: str = "GSM PARSER",
):
    _ = (worksheet_name, spreadsheet_name)
    return {
        "filter_ok": True,
        "filter_msg": "ok",
        "filter_view_id": int(filter_view_id) if (filter_view_id := 1) else 1,
        "lock_ok": True,
        "lock_msg": "ok",
    }
