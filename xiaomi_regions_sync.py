"""
Syncs Xiaomi regional model numbers from GitHub MobileModels_Fork.
Fetches raw markdown, extracts model number → device/region mappings,
matches to gsm_parser_devices (brand=Xiaomi), updates gsm_parser_regional_variants.

Regions tracked: Global, Europe, Latin America, India.
China is intentionally excluded: its device sizes are hard to source and
not needed for this app.
Rules:
- If region exists and models unchanged → keep existing L/W/T/diagonal.
- If models changed → update models, clear L/W/T/diagonal.
- New regions are appended; existing regions that disappeared are NOT removed.
"""

import contextlib
import re
import uuid
from datetime import datetime
from difflib import SequenceMatcher
from typing import Callable, Optional

import requests

from logger import logger

SYNC_URL = (
    "https://raw.githubusercontent.com/luckyzyx/MobileModels_Fork/"
    "master/brands/xiaomi_en.md"
)

TARGET_REGIONS = {"Global", "Europe", "Latin America", "India"}

# Canonical display order inside variants list
_REGION_ORDER = ["Global", "Europe", "Latin America", "India"]


# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------

def _extract_segment(segment: str) -> tuple[str, str, str]:
    """Parse one '/'-separated segment → (device_name, region, annotation).

    E.g. "Redmi Note 14 Global (NFC)" → ("Redmi Note 14", "Global", "(NFC)")
    Returns ("", "", "") when no target region found.
    """
    segment = segment.strip()
    # Multi-word region must come first to avoid partial match of "India" inside "Latin America"
    for region in ("Latin America", "Global", "Europe", "India"):
        idx = segment.find(region)
        if idx == -1:
            continue
        device_name = segment[:idx].strip()
        if not device_name:
            continue
        suffix = segment[idx + len(region):].strip()
        return device_name, region, suffix  # suffix is annotation e.g. "(NFC)"
    return "", "", ""


def _parse_markdown(markdown: str) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """Return {device_name: {region: [(model_num, annotation), ...]}}."""
    result: dict[str, dict[str, list]] = {}

    # Lines like:  `24117RN76G`: Redmi Note 14 Global
    # Some devices share one description across several SKUs on the same
    # line, e.g.  `25028RN03G` `25028RN03A` `25028RN03Y`: REDMI A5 Global
    # — the old single-code regex didn't match that line at all (no colon
    # immediately after the first backtick group), silently dropping every
    # model number on it, e.g. all three "Global" codes for Redmi A5 while
    # its single-code India/Latin America lines parsed fine.
    # (model numbers are uppercase alphanumeric, 5–14 chars)
    line_pattern = re.compile(r"^((?:`[A-Z0-9]{4,14}`\s*)+):\s*(.+)$", re.MULTILINE)
    code_pattern = re.compile(r"`([A-Z0-9]{4,14})`")

    for m in line_pattern.finditer(markdown):
        model_nums = code_pattern.findall(m.group(1))
        description = m.group(2).strip()

        for model_num in model_nums:
            # Each segment (split by " / ") may reference a different device
            for seg in re.split(r"\s*/\s*", description):
                device_name, region, annotation = _extract_segment(seg)
                if not device_name or not region:
                    continue
                if device_name not in result:
                    result[device_name] = {}
                if region not in result[device_name]:
                    result[device_name][region] = []
                result[device_name][region].append((model_num, annotation))

    return result


# ---------------------------------------------------------------------------
# DB name normalisation & matching
# ---------------------------------------------------------------------------

def _norm_github(name: str) -> str:
    """Normalise a GitHub device name for comparison.

    Strips:
    - Leading/trailing whitespace
    - Trailing annotations like "(NFC)"
    - "Xiaomi " prefix (GitHub sometimes includes it for Xiaomi-branded flagships)
    - Trailing "5G" / "4G" connectivity suffix
    """
    name = name.strip()
    name = re.sub(r"\s*\([^)]+\)\s*$", "", name).strip()
    name = re.sub(r"^Xiaomi\s+", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+5G\s*$", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+4G\s*$", "", name, flags=re.IGNORECASE).strip()
    return name.lower()


def _norm_db(model_name: str) -> str:
    """Normalise a DB model_name for comparison."""
    name = model_name.strip()
    name = re.sub(r"^Xiaomi\s+", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+5G\s*$", "", name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+4G\s*$", "", name, flags=re.IGNORECASE).strip()
    return name.lower()


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _last_number_token(name: str) -> str:
    """The last whitespace-separated token containing a digit, e.g.
    "redmi note 12 pro" -> "12", "redmi a2" -> "a2", "redmi 2" -> "2".

    Character-similarity alone can't tell "Redmi 2" from "Redmi 12" (one
    inserted digit, ~93% SequenceMatcher ratio) or "Redmi 2" from "Redmi A2"
    (one inserted letter) — both are unrelated phones. A false match here
    isn't cosmetic: it overwrites the correct device's regional variants
    with the wrong model numbers, and can even DELETE rows for regions the
    false match's GitHub entry doesn't mention (via the "GitHub region
    absent -> delete" rule) — this is exactly what silently wiped Redmi
    12's Latin America row and Redmi A5's Global row (both fuzzy-collided
    with the unrelated "Redmi 2" / "Redmi 5"). Requiring this token to
    match exactly is a strict, cheap guard against that whole class of
    corruption, without weakening genuine fuzzy matches (typos, spacing)
    that share the same model-number token.
    """
    for tok in reversed(name.split()):
        if any(ch.isdigit() for ch in tok):
            return tok
    return ""


def _find_db_device(
    device_name: str,
    db_devices: list[dict],
) -> Optional[dict]:
    """Return best matching DB device or None.

    Two-pass strategy:
    1. Exact base-name match + correct 5G flag.
    2. If no result: exact base-name match ignoring 5G (handles flagship devices
       like "Xiaomi 17" where GitHub omits "5G" but DB has it).
    3. Fuzzy match (≥0.90 similarity, same last model-number token, same
       product-line word) with 5G filter.
    """
    is_5g = bool(re.search(r"\b5G\b", device_name, re.IGNORECASE))
    base = _norm_github(device_name)
    base_num = _last_number_token(base)
    base_line = base.split()[0] if base.split() else ""

    # Pass 1 — exact base name + matching connectivity
    exact_5g: list[dict] = []
    # Pass 2 — exact base name, any connectivity (fallback for flags with no "5G" label)
    exact_any: list[dict] = []
    # Pass 3 — fuzzy
    fuzzy: list[tuple[float, dict]] = []

    for dev in db_devices:
        model_name = str(dev.get("model_name") or "")
        network_label = str(dev.get("network_label") or "").strip().upper()
        dev_is_5g = network_label == "5G"
        db_base = _norm_db(model_name)

        if db_base == base:
            if is_5g == dev_is_5g:
                exact_5g.append(dev)
            else:
                exact_any.append(dev)
        else:
            db_line = db_base.split()[0] if db_base.split() else ""
            # First word distinguishes product lines ("Mi" vs "Redmi" vs
            # "Poco") that SequenceMatcher alone treats as a near-match
            # since "redmi" contains "mi" as a substring — e.g. "Mi Note 10
            # Lite" and "Redmi Note 10 Lite" are unrelated phones that share
            # every word except this one.
            if (
                is_5g == dev_is_5g
                and base_num == _last_number_token(db_base)
                and base_line == db_line
            ):
                sim = _similarity(base, db_base)
                if sim >= 0.90:
                    fuzzy.append((sim, dev))

    if exact_5g:
        return exact_5g[0]

    # Fallback: only use any-connectivity match if there is exactly one candidate
    # to avoid mixing up 4G/5G variants of the same model.
    if exact_any and len(exact_any) == 1:
        return exact_any[0]

    if fuzzy:
        fuzzy.sort(key=lambda x: x[0], reverse=True)
        return fuzzy[0][1]

    return None


# ---------------------------------------------------------------------------
# Variant merging
# ---------------------------------------------------------------------------

def _build_models_str(entries: list[tuple[str, str]]) -> str:
    """Build comma-separated models string, sorted by model number."""
    parts = []
    for model_num, annotation in sorted(entries, key=lambda x: x[0]):
        parts.append(f"{model_num} {annotation}".strip())
    return ", ".join(parts)


def _extract_model_nums(models_str) -> set[str]:
    """Reverse of _build_models_str: pull just the model numbers back out
    of a saved 'models' field (each entry is 'MODELNUM' or 'MODELNUM
    annotation')."""
    nums: set[str] = set()
    for part in str(models_str or "").split(","):
        part = part.strip()
        if part:
            nums.add(part.split(" ", 1)[0].strip())
    return nums


def _merge_variants(existing: list[dict], github_regions: dict[str, list]) -> list[dict]:
    """Merge GitHub region data into existing variants list.

    Source tracking via variant field 'source':
    - 'github' → added/confirmed by GitHub sync; may be auto-deleted.
    - ''/'manual'/absent → manually added by SA; NEVER auto-deleted.

    Rules:
    - GitHub region present + variant exists → update models if changed (clear dims),
      stamp source='github'.
    - GitHub region present + variant missing → add new, source='github'.
    - GitHub region absent + existing variant source='github' → DELETE,
      UNLESS its model numbers still appear somewhere in GitHub's data for
      this device (see manual-split protection below) — then it's kept and
      permanently re-labeled source='manual'.
    - GitHub region absent + existing variant has no source / source!='github' → keep.

    Manual-split protection: GitHub sometimes reports several model numbers
    under one region with one shared description (e.g. Redmi A5 "Global" =
    three SKUs), but they can be physically different sizes. A superadmin
    can split such a region into multiple rows via the edit dialog (moving
    some model numbers into a second row with its own dimensions, e.g.
    renamed to "Europe"). Two protections work together:
    1. Once a model number appears in ANY existing row's `models` field,
       it's treated as claimed by that row — the sync never re-merges it
       back into a different row for the same GitHub region name.
    2. A split-off row is often given a region name GitHub doesn't report
       at all for this device (like "Europe" above) — the plain "region
       absent → delete" rule would erase it outright regardless of #1,
       since deletion is decided by region NAME before any codes are
       considered. So: only delete a github-sourced row whose region name
       vanished from GitHub if its model numbers ALSO don't appear
       anywhere else in GitHub's current data for this device. If they do
       (they're just filed under a different region name now), keep the
       row and flip it to source='manual' so it's never touched again —
       without that, module-level codes that were moved to a new region
       label got deleted here, then re-absorbed into the original region
       on the very next sync since nothing "claimed" them anymore.
    """
    github_region_keys = {r.lower() for r in github_regions}
    github_all_nums: set[str] = set()
    for entries in github_regions.values():
        for num, _ann in entries:
            github_all_nums.add(num)

    claimed: set[str] = set()
    for v in existing:
        claimed |= _extract_model_nums(v.get("models"))

    # Build updated list, dropping github-sourced regions no longer on GitHub
    by_region: dict[str, dict] = {}
    updated: list[dict] = []
    for v in existing:
        region = str(v.get("region") or "").strip()
        source = str(v.get("source") or "").strip()
        rk = region.lower() if region else ""
        row_nums = _extract_model_nums(v.get("models"))
        if source == "github" and rk and rk not in github_region_keys:
            if row_nums and (row_nums & github_all_nums):
                # Codes still exist on GitHub, just under a different region
                # label the admin chose — a manual split, not a real removal.
                v = dict(v)
                v["source"] = "manual"
            else:
                continue  # truly gone from GitHub → delete
        v_copy = dict(v)
        updated.append(v_copy)
        if rk:
            by_region[rk] = v_copy

    # Apply GitHub data
    for region, model_entries in github_regions.items():
        rk = region.lower()
        target = by_region.get(rk)
        target_claimed = _extract_model_nums(target.get("models")) if target else set()
        # Codes claimed by some OTHER row (a manual split) are excluded —
        # GitHub only manages what's still assigned to this row.
        other_claimed = claimed - target_claimed
        fresh_entries = [
            (num, ann) for num, ann in model_entries if num not in other_claimed
        ]
        if not fresh_entries:
            # Every code GitHub reports for this region has been manually
            # moved to another row — nothing left for GitHub to manage here.
            continue
        new_models = _build_models_str(fresh_entries)

        if target is not None:
            current_models = str(target.get("models") or "").strip()
            if current_models != new_models:
                target["models"] = new_models
                target["L"] = ""
                target["W"] = ""
                target["T"] = ""
                target["diagonal"] = ""
            target["source"] = "github"
        else:
            new_v = {
                "region": region,
                "models": new_models,
                "L": "",
                "W": "",
                "T": "",
                "diagonal": "",
                "source": "github",
            }
            updated.append(new_v)
            by_region[rk] = new_v

    # Sort: github regions first in canonical order, then manual regions
    order = {r: i for i, r in enumerate(_REGION_ORDER)}
    updated.sort(
        key=lambda v: (
            0 if str(v.get("source") or "") == "github" else 1,
            order.get(str(v.get("region") or ""), 999),
        )
    )

    return updated


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def sync_xiaomi_regions(
    db,
    log_fn: Optional[Callable[[str], None]] = None,
    timeout: int = 30,
) -> dict:
    """Fetch GitHub data, parse, match, update DB.

    Returns stats dict with keys:
      fetched, matched, updated, not_found, errors,
      newly_appeared (list of dicts — devices that had ZERO region variants
      before this run and now have at least one; used by gsm_parser_runner.py
      to build the "regions appeared" Telegram event/card).
    """
    stats = {
        "fetched": 0,
        "matched": 0,
        "updated": 0,
        "not_found": 0,
        "errors": 0,
    }

    def _log(msg: str) -> None:
        logger.info("[XiaomiSync] %s", msg)
        with contextlib.suppress(Exception):
            if log_fn:
                log_fn(msg)

    _log("Синхронізація регіонів Xiaomi: завантажуємо дані з GitHub…")

    # 1. Fetch
    try:
        resp = requests.get(SYNC_URL, timeout=timeout)
        resp.raise_for_status()
        markdown = resp.text
    except Exception as exc:
        _log(f"Помилка завантаження GitHub: {exc}")
        stats["errors"] += 1
        return stats

    # 2. Parse
    github_data = _parse_markdown(markdown)
    stats["fetched"] = len(github_data)
    _log(f"Знайдено {stats['fetched']} пристроїв на GitHub")

    # 3. Load all Xiaomi devices from DB
    try:
        db_devices = db.get_all_gsm_xiaomi_devices()
    except Exception as exc:
        _log(f"Помилка читання БД: {exc}")
        stats["errors"] += 1
        return stats

    # 4. Match each GitHub heading to a DB device, but MERGE all headings
    # that resolve to the SAME device before touching the DB. GitHub often
    # lists one physical device under two headings that both correctly
    # resolve to one DB row — e.g. "REDMI Pad 2" (WiFi SKUs) and "REDMI Pad
    # 2 4G" (LTE SKUs): _norm_github only strips a trailing "5G" suffix, not
    # "4G", so both normalize to "redmi pad 2" and correctly match the same
    # device. Calling _merge_variants once per heading used to process
    # these as two separate passes, each with only ITS OWN narrow slice of
    # regions/models — so the first pass's Europe/India rows (absent from
    # the second heading's data) got deleted by the "region absent ->
    # delete" rule, then a later pass restored them from scratch with blank
    # dimensions. Merging first means every region/model GitHub reports for
    # a device is visible in one _merge_variants call, so deletion and the
    # "still exists, just elsewhere" protection both see the full picture.
    device_regions: dict[str, dict[str, list]] = {}
    device_by_id: dict[str, dict] = {}
    for device_name, regions in github_data.items():
        try:
            db_dev = _find_db_device(device_name, db_devices)
            if db_dev is None:
                logger.debug("[XiaomiSync] Not found in DB: %s", device_name)
                stats["not_found"] += 1
                continue

            stats["matched"] += 1
            device_id = str(db_dev.get("id") or "")
            device_by_id[device_id] = db_dev
            combined = device_regions.setdefault(device_id, {})
            for region, entries in regions.items():
                combined.setdefault(region, []).extend(entries)

        except Exception as exc:
            logger.exception("[XiaomiSync] Error for %r: %s", device_name, exc)
            stats["errors"] += 1

    newly_appeared: list[dict] = []
    for device_id, regions in device_regions.items():
        try:
            existing = db.get_gsm_regional_variants(device_id)
            updated = _merge_variants(existing, regions)

            # Save only when something changed
            if updated != existing:
                db.save_gsm_regional_variants(device_id, updated)
                stats["updated"] += 1
                # First regions EVER recorded for this device (not just this
                # sync) — the caller uses this to notify "regions appeared"
                # for devices that aren't already covered by a "new device"
                # or "shops appeared" Telegram card this same run.
                if not existing and updated:
                    dev = device_by_id.get(device_id) or {}
                    regions_str = ", ".join(
                        sorted(
                            {
                                str(v.get("region") or "").strip()
                                for v in updated
                                if str(v.get("region") or "").strip()
                            }
                        )
                    )
                    newly_appeared.append(
                        {
                            "device_id": device_id,
                            "brand": "Xiaomi",
                            "model_name": str(dev.get("model_name") or ""),
                            "device_type": str(dev.get("device_type") or ""),
                            "source": str(dev.get("source") or ""),
                            "source_url": str(dev.get("source_url") or ""),
                            "image_url": str(dev.get("image_url") or ""),
                            "network_full": str(
                                dev.get("network_full")
                                or dev.get("target_net")
                                or dev.get("network_label")
                                or ""
                            ),
                            "regions": regions_str,
                        }
                    )
                    # Sidebar's "З'явились регіони" panel reads this exactly
                    # like get_gsm_parser_sidebar_data reads shops_count
                    # transitions — a dedicated change_history marker, since
                    # gsm_parser_regional_variants itself carries no history.
                    with contextlib.suppress(Exception):
                        with db._general_pg().cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO public.gsm_parser_change_history
                                  (id, device_id, source, field_key, old_value, new_value, changed_at, run_id, meta)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                                """,
                                (
                                    f"chg_{uuid.uuid4().hex}",
                                    device_id,
                                    str(dev.get("source") or "GSM Arena"),
                                    "__regions_appeared__",
                                    "",
                                    regions_str,
                                    datetime.now(),
                                    "xiaomi_regions_sync",
                                    "{}",
                                ),
                            )
                        db._general_pg().commit()

        except Exception as exc:
            logger.exception(
                "[XiaomiSync] Error for device %r: %s", device_id, exc
            )
            stats["errors"] += 1

    _log(
        f"Готово: GitHub {stats['fetched']}, "
        f"збіглось {stats['matched']}, "
        f"оновлено {stats['updated']}, "
        f"не знайдено {stats['not_found']}, "
        f"помилок {stats['errors']}"
    )

    # Bump the same singleton trigger a full parser run bumps after finishing
    # (see gsm_parser_runner.py). _needs_reload() in gsm_parser_view.py only
    # forces a running app to re-pull from DB on the 9:00 daily boundary or
    # when this trigger is newer than its last sync — without bumping it
    # here too, a region-only run (e.g. this script invoked standalone,
    # outside the full parser flow) silently changes the DB but every
    # already-open app keeps showing its stale disk cache indefinitely.
    if stats["updated"] > 0:
        with contextlib.suppress(Exception):
            db.set_gsm_parse_trigger(stats["updated"])

    stats["newly_appeared"] = newly_appeared
    return stats
