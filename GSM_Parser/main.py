from pathlib import Path

import requests

try:
    import cloudscraper as _cloudscraper

    _scraper = _cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
except ImportError:
    _scraper = None
import random
import re
import socket
import sys
import time
import unicodedata
from datetime import datetime

import gspread
from bs4 import BeautifulSoup
from gspread_formatting import (
    cellFormat,
    color,
    format_cell_range,
    format_cell_ranges,
    textFormat,
)
from oauth2client.service_account import ServiceAccountCredentials

# --- 1. ДОПОМІЖНІ ФУНКЦІЇ (МАЮТЬ БУТИ ПЕРШИМИ) ---


def instant_print(text):
    print(text)
    sys.stdout.flush()


def wait_for_internet(interval=30):
    instant_print("📡 Інтернет недоступний. Чекаємо відновлення з'єднання...")
    _CHECKS = [
        ("8.8.8.8", 53),
        ("1.1.1.1", 53),
        ("google.com", 80),
        ("google.com", 443),
    ]
    while True:
        for host, port in _CHECKS:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(5)
                s.connect((host, port))
                s.close()
                instant_print("✅ Інтернет відновлено! Продовжуємо...")
                return
            except Exception:
                continue
        time.sleep(interval)


def ensure_rows(sheet, required_rows):
    current_grid_rows = sheet.row_count
    if required_rows > current_grid_rows:
        add_count = max(required_rows - current_grid_rows, 50)
        sheet.add_rows(add_count)
        instant_print(f"➕ Розширення таблиці: додано {add_count} нових рядків.")


def parse_gsm_date(text):
    if not text:
        return ""
    clean_text = re.sub(r"(Released|Announced|Status|\.|\,)", " ", text).strip()

    months = {
        "january": "01",
        "february": "02",
        "march": "03",
        "april": "04",
        "may": "05",
        "june": "06",
        "july": "07",
        "august": "08",
        "september": "09",
        "october": "10",
        "november": "11",
        "december": "12",
        "jan": "01",
        "feb": "02",
        "mar": "03",
        "apr": "04",
        "jun": "06",
        "jul": "07",
        "aug": "08",
        "sep": "09",
        "oct": "10",
        "nov": "11",
        "dec": "12",
    }

    parts = clean_text.lower().split()
    year = next((p for p in parts if len(p) == 4 and p.isdigit()), "")
    month = "01"
    day = "01"

    for p in parts:
        if p in months:
            month = months[p]
        elif p.isdigit() and len(p) <= 2:
            day = p.zfill(2)

    if not year:
        return text
    return f"{day}.{month}.{year}"


# --- 2. ФУНКЦІЯ ОТРИМАННЯ КУРСІВ ВАЛЮТ ---


def get_live_rates():
    rates = {"USD": 44.0, "EUR": 51.0, "INR": 0.52}
    try:
        response = requests.get(
            "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json",
            timeout=10,
        )
        if response.status_code == 200:
            data = response.json()
            for item in data:
                if item["cc"] == "USD":
                    rates["USD"] = item["rate"]
                elif item["cc"] == "EUR":
                    rates["EUR"] = item["rate"]
                elif item["cc"] == "INR":
                    rates["INR"] = item["rate"]
            instant_print(
                f"📊 Курси оновлено з НБУ: USD: {rates['USD']}, EUR: {rates['EUR']}, INR: {rates['INR']}"
            )
    except Exception as e:
        instant_print(f"⚠️ Не вдалося отримати курси, використовуються стандартні: {e}")
    return rates


CURRENT_RATES = get_live_rates()

# --- 3. НАЛАШТУВАННЯ ---

GOOGLE_JSON = "inlaid-rig-463210-f7-ef379b167988.json"
SHEET_NAME = "GSM PARSER"
TEMP_SHEET_NAME = "Temp_Changes"

GSM_ARENA_BASE = "https://www.gsmarena.com/"

BRANDS = {
    "Tecno": "https://www.gsmarena.com/tecno-phones-120.php",
    "Samsung": "https://www.gsmarena.com/samsung-phones-9.php",
    "Apple": "https://www.gsmarena.com/apple-phones-48.php",
    "Motorola": "https://www.gsmarena.com/motorola-phones-4.php",
    "Xiaomi": "https://www.gsmarena.com/xiaomi-phones-80.php",
    "Google": "https://www.gsmarena.com/google-phones-107.php",
    "Infinix": "https://www.gsmarena.com/infinix-phones-119.php",
}

MAX_PAGES_PER_BRAND_SAFETY = 25
MAX_MODELS_PER_BRAND = 15

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
]

_soft_ban_until: float = 0.0  # timestamp; while time.time() < this, apply cautious delays


def _mark_soft_ban(duration: float = 420.0) -> None:
    """Call when any 429 is detected — extends cautious-mode window."""
    global _soft_ban_until
    _soft_ban_until = max(_soft_ban_until, time.time() + duration)


def _soft_ban_extra_sleep() -> None:
    """If we're in cautious mode, sleep an extra 60-90s before the next request."""
    if time.time() < _soft_ban_until:
        extra = random.uniform(60.0, 90.0)
        instant_print(f"    ⚠️ Обережний режим — додаткова пауза {int(extra)}с...")
        time.sleep(extra)


_CAPTCHA_MARKERS = [
    "just a moment",
    "cf-browser-verification",
    "verify you are human",
    "enable javascript and cookies",
    "checking your browser",
    "ddos-guard",
    "challenge-form",
    "captcha",
]


class _BlockedResponse:
    status_code = 429
    text = ""


def is_blocked_page(html_text):
    lower = html_text.lower()
    return any(marker in lower for marker in _CAPTCHA_MARKERS)


def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://www.google.com/",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


_NET_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)


def _load_proxy() -> dict | None:
    """Read proxy from GSM_Parser/proxy.txt (one line: http://user:pass@host:port or http://host:port)."""
    try:
        p = Path(__file__).resolve().parent / "proxy.txt"
        if p.exists():
            line = p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
            if line and not line.startswith("#"):
                return {"http": line, "https": line}
    except Exception:
        pass
    return None


_PROXY = _load_proxy()
if _PROXY:
    instant_print(f"🔀 Проксі увімкнено: {list(_PROXY.values())[0]}")


def http_get(url, *, timeout=30, max_retries=3, backoff_base=2.0):
    proxy = _load_proxy() or _PROXY
    while True:
        last_exc = None
        is_conn_err = False
        for attempt in range(max_retries):
            try:
                if _scraper:
                    res = _scraper.get(
                        url,
                        headers={"Referer": "https://www.google.com/"},
                        timeout=timeout,
                        proxies=proxy,
                    )
                else:
                    res = requests.get(
                        url, headers=get_headers(), timeout=timeout, proxies=proxy
                    )
                if res.status_code == 429:
                    return res
                if res.status_code >= 500:
                    wait = (backoff_base**attempt) + random.uniform(0.2, 0.9)
                    time.sleep(wait)
                    continue
                if res.status_code == 200 and is_blocked_page(res.text):
                    instant_print("🤖 [BLOCK] Виявлено CAPTCHA/bot-check сторінку!")
                    return _BlockedResponse()
                return res
            except _NET_ERRORS as e:
                last_exc = e
                is_conn_err = True
                wait = (backoff_base**attempt) + random.uniform(0.2, 0.9)
                time.sleep(wait)
            except Exception as e:
                last_exc = e
                wait = (backoff_base**attempt) + random.uniform(0.2, 0.9)
                time.sleep(wait)
        if is_conn_err:
            wait_for_internet()
        else:
            if last_exc:
                raise last_exc
            raise Exception("HTTP GET failed without exception")


def _derive_gsm_photos_url(dev_url: str) -> str:
    """Same pattern as the app-side helper (views/gsm_parser_view.py) — derive
    the GSM Arena gallery-pictures URL from a device page URL."""
    m = re.match(r"(https://www\.gsmarena\.com/)(.+?)-(\d+)\.php", str(dev_url or ""))
    return f"{m.group(1)}{m.group(2)}-pictures-{m.group(3)}.php" if m else ""


def _download_image_bytes(url: str) -> tuple:
    """Returns (bytes_or_None, blocked: bool). blocked=True means a 429 was
    hit — the caller should stop fetching further images for this device
    rather than keep hammering a blocked endpoint."""
    if not url:
        return None, False
    try:
        res = http_get(url, timeout=20, max_retries=2)
        status = getattr(res, "status_code", 0)
        if status == 429:
            return None, True
        if status != 200:
            return None, False
        content = getattr(res, "content", None)
        return (bytes(content) if content else None), False
    except Exception:
        return None, False


def _fetch_gallery_photo_bytes(dev_url: str, max_photos: int = 4) -> list:
    """Download up to `max_photos` gallery images for a device. Downloaded
    once here (at parse time) and stored in the DB — the app never hits
    GSM Arena directly for these, to avoid re-triggering anti-bot blocking
    across every end-user's machine.

    Paced like every other sub-request in this parser (small pause before
    each request, not just between gallery images) — a device-page fetch
    immediately followed by a photo fetch and a gallery-page fetch with zero
    gap is exactly the kind of burst pattern that gets flagged. Stops early
    on a 429 instead of continuing to hammer a blocked endpoint."""
    photos_url = _derive_gsm_photos_url(dev_url)
    if not photos_url:
        return []
    try:
        time.sleep(random.uniform(2.4, 4.8))
        res = http_get(photos_url, timeout=20, max_retries=2)
        status = getattr(res, "status_code", 0)
        if status == 429:
            instant_print("      ⚠️ 429 на сторінці галереї — пропускаємо фото для цього пристрою")
            _mark_soft_ban(420.0)
            pause = random.uniform(300.0, 480.0)
            instant_print(f"      💤 Пауза {int(pause)}с після 429 на галереї...")
            time.sleep(pause)
            return []
        if status != 200:
            return []
        soup = BeautifulSoup(res.text, "html.parser")
        urls = [
            img["src"]
            for img in soup.find_all("img")
            if "gsmarena.com/vv/pics/" in str(img.get("src", ""))
        ][:max_photos]
        out = []
        for u in urls:
            time.sleep(random.uniform(2.0, 4.0))
            b, blocked = _download_image_bytes(u)
            if blocked:
                instant_print("      ⚠️ 429 на фото — зупиняємо галерею для цього пристрою")
                _mark_soft_ban(420.0)
                pause = random.uniform(300.0, 480.0)
                instant_print(f"      💤 Пауза {int(pause)}с після 429 на фото...")
                time.sleep(pause)
                break
            if b:
                out.append(b)
        return out
    except Exception:
        return []


def get_spec(soup, spec_name):
    tag = soup.find("td", {"data-spec": spec_name})
    return tag.text.strip() if tag else ""


def find_row_by_url(sheet, url):
    try:
        col_v = sheet.col_values(22)
        search_url = url.strip().lower()
        if search_url.endswith("/"):
            search_url = search_url[:-1]
        for idx, val in enumerate(col_v):
            clean_val = val.strip().lower()
            if clean_val.endswith("/"):
                clean_val = clean_val[:-1]
            if clean_val == search_url:
                return idx + 1
    except Exception as e:
        instant_print(f"⚠️ Помилка пошуку рядка: {e}")
    return None


def prepare_sheet(sheet):
    instant_print("🧹 Очищення старого маркування (A:V)...")
    try:
        all_vals = sheet.get_all_values()
        total_rows = len(all_vals)
        if total_rows < 2:
            return
        white_fmt = cellFormat(backgroundColor=color(1, 1, 1))
        format_cell_range(sheet, f"A2:V{total_rows}", white_fmt)
    except Exception as e:
        instant_print(f"⚠️ Не вдалося підготувати аркуш: {e}")


def get_existing_data_for_brand(sheet, brand_name):
    try:
        all_vals = sheet.get_all_values()
        brand_data = {}
        for i, row in enumerate(all_vals):
            if len(row) > 21 and row[0] == brand_name:
                url = row[21].strip().lower()
                if url.endswith("/"):
                    url = url[:-1]
                brand_data[url] = {"row": i + 1, "data": row}
        return brand_data
    except Exception:
        return {}


def get_existing_name_rows_for_brand(sheet, brand_name):
    try:
        all_vals = sheet.get_all_values()
        name_map = {}
        for i, row in enumerate(all_vals):
            if len(row) > 1 and row[0] == brand_name:
                nm = str(row[1]).strip().lower()
                if nm:
                    name_map[nm] = {"row": i + 1, "data": row}
        return name_map
    except Exception as e:
        instant_print(f"⚠️ Помилка побудови мапи імен: {e}")
        return {}


def update_existing_row(sheet, row_idx, new_data, old_data, temp_sheet=None):
    try:
        from datetime import timedelta

        orange_fmt = cellFormat(backgroundColor=color(1.0, 0.8, 0.4))

        new_data_clean = [
            unicodedata.normalize("NFKD", str(x)).strip() if x else "" for x in new_data
        ]
        new_data_clean[5] = f"=IMAGE(U{row_idx})"

        headers = sheet.row_values(1)
        highlight_formats = []
        changed_any = False
        today_str = datetime.now().strftime("%d.%m.%Y")
        date_limit = datetime.now() - timedelta(days=365)
        alphabet = "ABCDEFGHIJKLMNOPQRSTUV"

        try:
            all_notes_matrix = sheet.get_notes()
            if row_idx <= len(all_notes_matrix):
                row_notes_data = all_notes_matrix[row_idx - 1]
                existing_notes = [
                    (
                        str(row_notes_data[col])
                        if col < len(row_notes_data) and row_notes_data[col]
                        else ""
                    )
                    for col in range(22)
                ]
            else:
                existing_notes = [""] * 22
        except Exception as e:
            instant_print(f"    ⚠️ Помилка зчитування приміток: {e}")
            existing_notes = [""] * 22

        def to_clean_num(val):
            if not val:
                return ""
            cleaned = re.sub(r"[^\d.,]", "", val).replace(",", ".")
            return cleaned if any(char.isdigit() for char in cleaned) else val.strip()

        final_notes_to_send = {}

        for i in range(2, 22):
            if i in [5, 20, 21]:
                continue

            nv = str(new_data_clean[i]).strip()
            ov = str(old_data[i]).strip() if i < len(old_data) else ""

            is_changed = False

            if i in [6, 7]:

                def normalize_date_str(d):
                    if not d:
                        return ""
                    parts = [p.replace(".0", "") for p in d.split(".") if p]
                    return ".".join([str(int(p)) if p.isdigit() else p for p in parts])

                try:
                    if normalize_date_str(nv) != normalize_date_str(ov):
                        is_changed = True
                except Exception:
                    if nv != ov:
                        is_changed = True
            elif i in [8, 13, 18, 19]:
                if to_clean_num(nv) != to_clean_num(ov) and nv != "":
                    is_changed = True
            else:
                if nv != ov and nv != "":
                    is_changed = True

            if is_changed:
                changed_any = True
                col_name = headers[i] if i < len(headers) else f"Колонка {alphabet[i]}"
                cell_address = f"{alphabet[i]}{row_idx}"

                instant_print(
                    f"    🔍 Зміна в {new_data_clean[1]} [{col_name}]: '{ov}' -> '{nv}'"
                )

                new_history_line = f"{today_str}: {ov}"
                current_note_content = (
                    existing_notes[i] if i < len(existing_notes) else ""
                )

                lines = [
                    ln.strip() for ln in current_note_content.split("\n") if ln.strip()
                ]
                valid_old_lines = []

                for line in lines:
                    date_match = re.match(r"^(\d{2}\.\d{2}\.\d{4})", line)
                    if date_match:
                        try:
                            line_date = datetime.strptime(
                                date_match.group(1), "%d.%m.%Y"
                            )
                            if line_date >= date_limit:
                                valid_old_lines.append(line)
                        except Exception:
                            valid_old_lines.append(line)
                    else:
                        valid_old_lines.append(line)

                if not valid_old_lines or valid_old_lines[0] != new_history_line:
                    final_note_text = "\n".join([new_history_line] + valid_old_lines)
                else:
                    final_note_text = "\n".join(valid_old_lines)

                final_notes_to_send[cell_address] = final_note_text
                highlight_formats.append((cell_address, orange_fmt))

                if temp_sheet:
                    try:
                        temp_sheet.append_row(
                            [
                                today_str,
                                new_data_clean[1],
                                col_name,
                                ov if ov else "-",
                                nv,
                                new_data_clean[21],
                            ]
                        )
                    except Exception:
                        pass

        if changed_any:
            if final_notes_to_send:
                try:
                    sheet.insert_notes(final_notes_to_send)
                except Exception as e:
                    instant_print(f"    ⚠️ Помилка запису приміток: {e}")

            sheet.update(
                range_name=f"A{row_idx}:V{row_idx}",
                values=[new_data_clean[:22]],
                value_input_option="USER_ENTERED",
            )

            if len(new_data_clean) > 22:
                sheet.update(
                    range_name=f"AO{row_idx}",
                    values=[[new_data_clean[22]]],
                    value_input_option="USER_ENTERED",
                )

            if highlight_formats:
                format_cell_ranges(sheet, highlight_formats)

            return True
        return False

    except Exception as e:
        instant_print(f"❌ Критична помилка в update_existing_row: {e}")
        return False


def ensure_url_fields(sheet, row_idx, info):
    try:
        img_url = info[20] if len(info) > 20 else ""
        url = info[21] if len(info) > 21 else ""
        if url:
            sheet.update(
                range_name=f"V{row_idx}",
                values=[[url]],
                value_input_option="USER_ENTERED",
            )
        if img_url:
            sheet.update(
                range_name=f"U{row_idx}",
                values=[[img_url]],
                value_input_option="USER_ENTERED",
            )
    except Exception as e:
        instant_print(f"⚠️ Не вдалося оновити URL поля: {e}")


def parse_device_details(url, brand_name, skip_pacing: bool = False, fetch_photos: bool = True):
    """skip_pacing=True skips the ~32-56s anti-bot delay before the request —
    that pacing exists to space out a BULK crawl's many sequential requests,
    which doesn't apply to a single one-off manual-link fetch (see the
    "Ручне посилання" dialogs' callers, which pass skip_pacing=True).

    fetch_photos=False skips the main-photo/gallery download (2 extra
    requests) — used for the "recent N devices" re-check of already-known
    devices, where the goal is to catch every spec/name/date change from
    the page we're already fetching anyway, without paying for photos that
    essentially never change once a device has them."""
    try:
        if not skip_pacing:
            _soft_ban_extra_sleep()
            wait = random.uniform(32.0, 56.0)
            time.sleep(wait)
        res = http_get(url, timeout=30, max_retries=3)

        if res.status_code == 429:
            instant_print("🛑 КРИТИЧНИЙ БАН (429) — GSM Arena блокує на 24 години. Зупиняємо.")
            return "STOP"

        if res.status_code != 200:
            return None

        # A maintenance/anti-bot-block/CDN challenge page can still return
        # HTTP 200 with real HTML content — status code alone doesn't catch
        # it. Same class of bug as E-Katalog's parse_ekatalog_item (see its
        # comment): a bad page slipping through here would have its own
        # <title>/og:title picked up as the device's real name via the
        # fallback below. Two layers, same as the E-Katalog fix: check the
        # raw page text first, then refuse to accept an extracted name that
        # IS one of these phrases regardless of where it slipped through.
        _bad_page_markers = (
            "сайт на обслуговуванні",
            "технічні роботи",
            "under maintenance",
            "access denied",
            "403 forbidden",
            "attention required",
            "just a moment",
            "service unavailable",
            "502 bad gateway",
            "503 service",
        )
        _res_text_lower = res.text.lower()
        if any(marker in _res_text_lower for marker in _bad_page_markers):
            instant_print(f"      ⏳ Сторінка виглядає як блок/технічні роботи: {url}")
            return None

        soup = BeautifulSoup(res.text, "html.parser")

        name_tag = soup.find("h1", class_="specs-phone-name-title")
        if name_tag:
            name = name_tag.text
        else:
            og_title = soup.find("meta", property="og:title")
            if og_title and og_title.get("content"):
                name = og_title.get("content").strip()
            else:
                title = soup.find("title")
                name = title.get_text().strip() if title else ""
            if not name:
                return None
        if name.strip().lower() in _bad_page_markers:
            instant_print(f"      ⏳ Назва виявилась текстом помилки сторінки: {url}")
            return None
        name = re.sub(r"Xiaomi\s+Poco", "Poco", name, flags=re.IGNORECASE).strip()

        net_tech = get_spec(soup, "nettech")
        if not net_tech:
            net_row = soup.find("th", string=re.compile(r"Network", re.I))
            if net_row:
                tech_cell = net_row.find_next("td", class_="nfo")
                if tech_cell:
                    net_tech = tech_cell.text.strip()

        net_tech_upper = net_tech.upper()
        connection_module = (
            "5G"
            if "5G" in net_tech_upper
            else (
                "4G"
                if any(x in net_tech_upper for x in ["LTE", "GSM", "HSPA", "CDMA"])
                else "No"
            )
        )

        photo_div = soup.find("div", class_="specs-photo-main")
        img_url = (
            photo_div.find("img")["src"] if photo_div and photo_div.find("img") else ""
        )

        # Download photos now (once, at parse time) so the app/DB never needs
        # to hit GSM Arena live for them. parse_device_details is only called
        # for devices not yet known to the DB (new devices, or first-ever
        # collect for a brand) — so this does not multiply request volume for
        # already-known/unchanged devices on routine incremental runs. A full
        # from-scratch reparse treats every device as new, though, so this
        # whole sequence is paced exactly like the rest of the parser — never
        # fire the device page, the main photo and the gallery page back to
        # back with no gap.
        main_photo_bytes = None
        gallery_photo_bytes = []
        if fetch_photos:
            if img_url:
                time.sleep(random.uniform(1.6, 3.2))
                main_photo_bytes, _main_photo_blocked = _download_image_bytes(img_url)
                if _main_photo_blocked:
                    instant_print("      ⚠️ 429 на головному фото — пропускаємо фото для цього пристрою")
                    _mark_soft_ban(420.0)
                    pause = random.uniform(300.0, 480.0)
                    instant_print(f"      💤 Пауза {int(pause)}с після 429 на фото...")
                    time.sleep(pause)
                    gallery_photo_bytes = []
                else:
                    gallery_photo_bytes = _fetch_gallery_photo_bytes(url)
            else:
                gallery_photo_bytes = _fetch_gallery_photo_bytes(url)

        raw_announced = get_spec(soup, "year") or get_spec(soup, "announced")
        announced_date = parse_gsm_date(raw_announced)

        device_year = ""
        if raw_announced:
            year_match = re.search(r"\b(19\d{2}|20\d{2})\b", raw_announced)
            device_year = year_match.group(1) if year_match else ""

        status_raw = get_spec(soup, "status")
        if "Released" in status_raw:
            released_date = parse_gsm_date(status_raw)
        else:
            snap_bar = soup.find("div", id="specs-cp-btns")
            snap_text = snap_bar.get_text() if snap_bar else ""
            rel_match = re.search(r"Released\s+(\d{4},?\s+\w+\s?\d*)", snap_text)
            released_date = (
                parse_gsm_date(rel_match.group(1)) if rel_match else status_raw
            )

        display_raw = get_spec(soup, "displaysize")
        display_match = re.search(r"(\d+\.?\d*)", display_raw)
        display_size_text = display_match.group(1) if display_match else ""

        os_text = get_spec(soup, "os")
        dims = get_spec(soup, "dimensions")

        diag_val = 0.0
        try:
            diag_val = float(display_size_text)
        except Exception:
            pass

        if diag_val > 0 and diag_val < 3.0:
            device_type = "Watch"
        elif any(
            x in name.lower() or x in dims.lower() for x in ["fold", "flip", "foldable"]
        ):
            device_type = "Smartphone"
        elif diag_val > 7.0:
            device_type = "Tablet"
        else:
            device_type = "Smartphone"

        d_list = re.findall(r"(\d+\.?\d?)", dims)
        dim_l, w, t = (
            (d_list[0], d_list[1], d_list[2]) if len(d_list) >= 3 else ("", "", "")
        )

        dims_standard = f"{dim_l}*{w}*{t}" if dim_l and w and t else ""

        weight_raw = get_spec(soup, "weight")
        weight_cleaned = (
            re.search(r"(\d+)", weight_raw).group(1)
            if re.search(r"(\d+)", weight_raw)
            else ""
        )

        price_raw = get_spec(soup, "price")
        price_uah = ""
        price_clean = price_raw.replace(",", "")

        eur_match = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:€|EUR)", price_clean, re.IGNORECASE
        ) or re.search(r"(?:€|EUR)\s*(\d+(?:\.\d+)?)", price_clean, re.IGNORECASE)
        usd_match = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:\$|USD)", price_clean, re.IGNORECASE
        ) or re.search(r"(?:\$|USD)\s*(\d+(?:\.\d+)?)", price_clean, re.IGNORECASE)
        inr_match = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:₹|INR)", price_clean, re.IGNORECASE
        ) or re.search(r"(?:₹|INR)\s*(\d+(?:\.\d+)?)", price_clean, re.IGNORECASE)

        if eur_match:
            price_uah = str(int(float(eur_match.group(1)) * CURRENT_RATES["EUR"]))
        elif usd_match:
            price_uah = str(int(float(usd_match.group(1)) * CURRENT_RATES["USD"]))
        elif inr_match:
            price_uah = str(int(float(inr_match.group(1)) * CURRENT_RATES["INR"]))

        if price_uah:
            price_uah = re.sub(r"[^\d]", "", price_uah)

        battery_raw = get_spec(soup, "batdescription1")
        battery_cleaned = (
            re.search(r"(\d+)", battery_raw).group(1)
            if re.search(r"(\d+)", battery_raw)
            else ""
        )

        return [
            brand_name,
            name,
            connection_module,
            net_tech,
            device_type,
            "",
            announced_date,
            released_date,
            dims_standard,
            dim_l,
            w,
            t,
            weight_cleaned,
            display_size_text,
            os_text,
            get_spec(soup, "chipset"),
            get_spec(soup, "internalmemory"),
            battery_cleaned,
            price_raw,
            price_uah,
            img_url,
            url,
            device_year,
            main_photo_bytes,
            gallery_photo_bytes,
        ]
    except Exception as e:
        instant_print(f"❌ Помилка на {url}: {e}")
        return None


def parse_device_price_dates(url: str):
    """Lightweight fetch for existing devices that already have photos in the DB.
    Downloads the device page once and extracts only price + announced/released dates.
    Returns a dict, None on error, or 'STOP' on 429."""
    try:
        _soft_ban_extra_sleep()
        time.sleep(random.uniform(32.0, 56.0))
        res = http_get(url, timeout=30, max_retries=3)
        if res.status_code == 429:
            instant_print("🛑 КРИТИЧНИЙ БАН (429) — GSM Arena блокує на 24 години. Зупиняємо.")
            return "STOP"
        if res.status_code != 200:
            return None
        soup = BeautifulSoup(res.text, "html.parser")

        raw_announced = get_spec(soup, "year") or get_spec(soup, "announced")
        announced_date = parse_gsm_date(raw_announced)

        status_raw = get_spec(soup, "status")
        if "Released" in status_raw:
            released_date = parse_gsm_date(status_raw)
        else:
            snap_bar = soup.find("div", id="specs-cp-btns")
            snap_text = snap_bar.get_text() if snap_bar else ""
            rel_match = re.search(r"Released\s+(\d{4},?\s+\w+\s?\d*)", snap_text)
            released_date = parse_gsm_date(rel_match.group(1)) if rel_match else status_raw

        price_raw = get_spec(soup, "price")
        price_uah = ""
        price_clean = price_raw.replace(",", "")
        eur_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:€|EUR)", price_clean, re.IGNORECASE) or re.search(r"(?:€|EUR)\s*(\d+(?:\.\d+)?)", price_clean, re.IGNORECASE)
        usd_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:\$|USD)", price_clean, re.IGNORECASE) or re.search(r"(?:\$|USD)\s*(\d+(?:\.\d+)?)", price_clean, re.IGNORECASE)
        inr_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:₹|INR)", price_clean, re.IGNORECASE) or re.search(r"(?:₹|INR)\s*(\d+(?:\.\d+)?)", price_clean, re.IGNORECASE)
        if eur_match:
            price_uah = str(int(float(eur_match.group(1)) * CURRENT_RATES["EUR"]))
        elif usd_match:
            price_uah = str(int(float(usd_match.group(1)) * CURRENT_RATES["USD"]))
        elif inr_match:
            price_uah = str(int(float(inr_match.group(1)) * CURRENT_RATES["INR"]))
        if price_uah:
            price_uah = re.sub(r"[^\d]", "", price_uah)

        return {
            "price_raw": price_raw,
            "price_uah": price_uah,
            "announced": announced_date,
            "released": released_date,
        }
    except Exception as e:
        instant_print(f"❌ Помилка (легкий режим) на {url}: {e}")
        return None


def fetch_incremental_gallery_photos(dev_url: str, existing_count: int, max_total: int = 7) -> list:
    """Download gallery photos at positions [existing_count..max_total-1] from GSM Arena.
    Used to top-up the gallery without touching already-stored photos."""
    if existing_count >= max_total:
        return []
    photos_url = _derive_gsm_photos_url(dev_url)
    if not photos_url:
        return []
    try:
        time.sleep(random.uniform(2.4, 4.8))
        res = http_get(photos_url, timeout=20, max_retries=2)
        status = getattr(res, "status_code", 0)
        if status == 429:
            instant_print("      ⚠️ 429 на галереї (top-up) — пропускаємо")
            _mark_soft_ban(420.0)
            pause = random.uniform(300.0, 480.0)
            instant_print(f"      💤 Пауза {int(pause)}с...")
            time.sleep(pause)
            return []
        if status != 200:
            return []
        soup = BeautifulSoup(res.text, "html.parser")
        all_urls = [
            img["src"]
            for img in soup.find_all("img")
            if "gsmarena.com/vv/pics/" in str(img.get("src", ""))
        ][:max_total]
        new_urls = all_urls[existing_count:]
        if not new_urls:
            return []
        out = []
        for u in new_urls:
            time.sleep(random.uniform(2.0, 4.0))
            b, blocked = _download_image_bytes(u)
            if blocked:
                instant_print("      ⚠️ 429 на фото (top-up) — зупиняємо")
                _mark_soft_ban(420.0)
                pause = random.uniform(300.0, 480.0)
                instant_print(f"      💤 Пауза {int(pause)}с...")
                time.sleep(pause)
                break
            if b:
                out.append(b)
        return out
    except Exception:
        return []


def sync_to_google(data_batch, brand_name, temp_sheet=None):
    global total_added
    if not data_batch:
        return
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_JSON, scope)
        client = gspread.authorize(creds)
        ss = client.open(SHEET_NAME)
        sheet = ss.sheet1

        all_vals = sheet.get_all_values()
        current_rows_count = len(all_vals)

        last_brand_row = -1
        for i, row in enumerate(all_vals):
            if row and row[0] == brand_name:
                last_brand_row = i + 1

        target_idx = (
            last_brand_row + 1 if last_brand_row != -1 else current_rows_count + 1
        )

        ensure_rows(sheet, target_idx + len(data_batch))

        if target_idx <= current_rows_count:
            sheet.insert_rows([[""] * 41] * len(data_batch), row=target_idx)

        av_data = []
        ao_data = []

        for idx, item in enumerate(data_batch):
            row_num = target_idx + idx
            clean_item = [
                unicodedata.normalize("NFKD", str(x)).strip() if x else "" for x in item
            ]
            clean_item[5] = f"=IMAGE(U{row_num})"
            av_data.append(clean_item[:22])
            year_val = clean_item[22] if len(clean_item) > 22 else ""
            ao_data.append([year_val])

        range_av = f"A{target_idx}:V{target_idx + len(av_data) - 1}"
        sheet.update(
            range_name=range_av, values=av_data, value_input_option="USER_ENTERED"
        )

        range_ao = f"AO{target_idx}:AO{target_idx + len(ao_data) - 1}"
        sheet.update(
            range_name=range_ao, values=ao_data, value_input_option="USER_ENTERED"
        )

        total_added += len(data_batch)

        format_cell_range(
            sheet,
            f"A{target_idx}:V{target_idx + len(av_data) - 1}",
            cellFormat(backgroundColor=color(0.85, 0.92, 1.0)),
        )

        if temp_sheet:
            today_str = datetime.now().strftime("%d.%m.%Y")
            for item in data_batch:
                try:
                    temp_sheet.append_row(
                        [today_str, item[1], "Новий", "-", "Додано", item[21]]
                    )
                except Exception:
                    pass

    except Exception as e:
        instant_print(f"    ❌ Помилка вставки: {e}")


def sort_sheet_by_brand_and_date():
    instant_print("    🔃 Сортування: Бренди + Дати...")
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_JSON, scope)
        client = gspread.authorize(creds)
        sheet = client.open(SHEET_NAME).sheet1
        sheet.sort((1, "asc"), (7, "des"))
        time.sleep(1)
    except Exception as e:
        instant_print(f"    ❌ Помилка сортування: {e}")


def format_dimensions_by_brand(brand_name):
    instant_print(f"    🎨 Кольори розмірів та діагоналей для {brand_name}...")
    try:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_JSON, scope)
        client = gspread.authorize(creds)
        sheet = client.open(SHEET_NAME).sheet1

        time.sleep(1.5)

        data = sheet.get_all_values()
        if len(data) < 2:
            return

        brand_rows = []
        for i, row in enumerate(data[1:], 2):
            if len(row) > 13 and row[0] == brand_name:
                dim_l = str(row[9]).strip()
                w = str(row[10]).strip()
                t = str(row[11]).strip()
                diag = str(row[13]).strip()
                size_str = (
                    f"{dim_l}|{w}|{t}" if (dim_l and w and t and dim_l != "0") else None
                )
                brand_rows.append(
                    {
                        "idx": i,
                        "size": size_str,
                        "diag": diag if (diag and diag != "0") else None,
                    }
                )

        size_groups = {}
        diag_groups = {}

        for item in brand_rows:
            if item["size"]:
                size_groups.setdefault(item["size"], []).append(item["idx"])
            if item["diag"]:
                diag_groups.setdefault(item["diag"], []).append(item["idx"])

        formats_to_apply = []

        for val, rows in size_groups.items():
            if len(rows) > 1:
                random.seed(val)
                bg_color = color(
                    random.uniform(0.8, 0.98),
                    random.uniform(0.8, 0.98),
                    random.uniform(0.8, 0.98),
                )
                for r_idx in rows:
                    formats_to_apply.append(
                        (
                            f"I{r_idx}:L{r_idx}",
                            cellFormat(
                                backgroundColor=bg_color,
                                textFormat=textFormat(bold=True),
                            ),
                        )
                    )

        for val, rows in diag_groups.items():
            if len(rows) > 1:
                random.seed(val + "diag")
                bg_color = color(
                    random.uniform(0.8, 0.98),
                    random.uniform(0.8, 0.98),
                    random.uniform(0.8, 0.98),
                )
                for r_idx in rows:
                    formats_to_apply.append(
                        (
                            f"N{r_idx}",
                            cellFormat(
                                backgroundColor=bg_color,
                                textFormat=textFormat(bold=True),
                            ),
                        )
                    )

        if formats_to_apply:
            format_cell_ranges(sheet, formats_to_apply)

    except Exception as e:
        instant_print(f"    ❌ Помилка форматування: {e}")


def get_all_pages_for_brand(brand_url):
    try:
        res = http_get(brand_url, timeout=30, max_retries=3)
        if res.status_code == 429:
            instant_print("🛑 429 при визначенні сторінок бренду.")
            return "STOP"
        soup = BeautifulSoup(res.text, "html.parser")
        nav = soup.find("div", class_="nav-pages")

        if not nav:
            return [brand_url]

        href_by_num = {}
        for a in nav.find_all("a"):
            text = a.text.strip()
            href = a.get("href", "")
            if text.isdigit() and href:
                href_by_num[int(text)] = href

        if not href_by_num:
            return [brand_url]

        last_page = max(href_by_num)
        instant_print(
            f"📑 Визначено останню сторінку: {last_page}"
            f" (~{last_page * 50} пристроїв в лістингу)"
        )

        # The current page is never rendered as a link in the pager — only
        # OTHER pages get an <a>. So if page 1 shows up as a link here, the
        # configured brand_url is not actually page 1 (e.g. someone saved a
        # later page's URL by mistake). New devices always land on page 1,
        # so we must resolve and use the real page-1 URL, not just trust
        # whatever URL was configured for the brand.
        if 1 in href_by_num:
            page_one_url = GSM_ARENA_BASE + href_by_num[1].lstrip("/")
            if page_one_url != brand_url:
                instant_print(
                    "⚠️ Посилання бренду веде не на 1-шу сторінку. "
                    f"Використовую справжню 1-шу сторінку: {page_one_url}"
                )
        else:
            page_one_url = brand_url

        sample_href = next(
            (h for h in href_by_num.values() if "-p" in h), ""
        )
        if not sample_href:
            return [page_one_url]

        pages = [page_one_url]
        for i in range(2, last_page + 1):
            new_href = re.sub(r"-p\d+\.php", f"-p{i}.php", sample_href)
            pages.append(GSM_ARENA_BASE + new_href.lstrip("/"))

        return pages
    except Exception as e:
        instant_print(f"⚠️ Помилка визначення сторінок: {e}")
        return [brand_url]


def get_device_links_from_page(page_url):
    try:
        res = http_get(page_url, timeout=30, max_retries=3)
        if res.status_code == 429:
            return "STOP"
        soup = BeautifulSoup(res.text, "html.parser")
        makers = soup.find("div", class_="makers")
        return (
            [GSM_ARENA_BASE + a.get("href").lstrip("/") for a in makers.find_all("a")]
            if makers
            else []
        )
    except Exception:
        return []


def get_recent_device_links_for_brand(brand_url, existing_url_map):
    pages = get_all_pages_for_brand(brand_url)
    if pages == "STOP":
        return "STOP"

    collected = []
    for page in pages[:MAX_PAGES_PER_BRAND_SAFETY]:
        links = get_device_links_from_page(page)
        if links == "STOP":
            return "STOP"
        if not links:
            continue
        for link in links:
            collected.append(link)
            if len(collected) >= MAX_MODELS_PER_BRAND:
                return collected[:MAX_MODELS_PER_BRAND]
        time.sleep(random.uniform(3.2, 5.6))

    return collected[:MAX_MODELS_PER_BRAND]


def get_all_device_links_for_brand(brand_url):
    pages = get_all_pages_for_brand(brand_url)
    if pages == "STOP":
        return "STOP"

    # Visit listing pages in random order (human-like, harder to fingerprint).
    # Each link is tagged with (page_idx, pos_on_page) so we can restore
    # the canonical GSM Arena order after collecting from all pages.
    page_indices = list(range(len(pages)))
    random.shuffle(page_indices)
    instant_print(f"🔀 Порядок обходу сторінок: {[i + 1 for i in page_indices]}")

    links_with_pos: list[tuple[int, int, str]] = []
    visited: set[int] = set()
    for page_idx in page_indices:
        if page_idx in visited:
            continue
        visited.add(page_idx)
        links = get_device_links_from_page(pages[page_idx])
        if links == "STOP":
            return "STOP"
        if links:
            for pos, link in enumerate(links):
                links_with_pos.append((page_idx, pos, link))
        time.sleep(random.uniform(4.0, 7.2))

    # Restore GSM Arena order (page 1 first, then page 2, …)
    links_with_pos.sort(key=lambda x: (x[0], x[1]))
    return [link for _, _, link in links_with_pos]


# --- ОСНОВНИЙ БЛОК ---
if __name__ == "__main__":
    start_time = time.time()
    total_added = 0
    total_updated = 0

    try:
        instant_print("🚀 СКРИПТ ЗАПУЩЕНО!")
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_JSON, scope)
        client = gspread.authorize(creds)
        ss = client.open(SHEET_NAME)
        main_sheet = ss.sheet1
        prepare_sheet(main_sheet)

        try:
            temp_sheet = ss.worksheet(TEMP_SHEET_NAME)
        except Exception:
            temp_sheet = None

        brand_items = list(BRANDS.items())
        random.shuffle(brand_items)

        gsm_banned = False
        for brand_name, main_url in brand_items:
            if gsm_banned:
                break
            instant_print(f"\n🔎 БРЕНД: {brand_name.upper()}")
            existing_brand_map = get_existing_data_for_brand(main_sheet, brand_name)
            existing_name_map = get_existing_name_rows_for_brand(main_sheet, brand_name)

            processed_urls_in_session = set()
            _devices_since_long_break = 0
            _next_break_at = random.randint(8, 12)

            if not existing_brand_map:
                instant_print(
                    "  📌 Бренду в таблиці немає: парсимо всі сторінки бренду."
                )
                target_links = get_all_device_links_for_brand(main_url)
            else:
                recent_links = get_recent_device_links_for_brand(
                    main_url, existing_brand_map
                )
                if recent_links == "STOP":
                    target_links = "STOP"
                else:
                    recent_set = {
                        link.strip().lower().rstrip("/") for link in recent_links
                    }
                    extra_links = [
                        url for url in existing_brand_map if url not in recent_set
                    ]
                    target_links = recent_links + extra_links
                    instant_print(
                        f"  📋 {len(recent_links)} нових з GSMarena + {len(extra_links)} наявних з таблиці = {len(target_links)} товарів"
                    )

            if target_links == "STOP":
                instant_print("🛑 БАН 429 на сторінках бренду — GSM Arena блокує на 24 години. Зупиняємо.")
                gsm_banned = True
                break

            for link in target_links:
                link_clean = link.strip().lower()
                if link_clean.endswith("/"):
                    link_clean = link_clean[:-1]

                if link_clean in processed_urls_in_session:
                    continue

                _devices_since_long_break += 1
                if _devices_since_long_break >= _next_break_at:
                    human_pause = random.uniform(180.0, 360.0)
                    instant_print(f"  ☕ Людська пауза {int(human_pause)}с після {_devices_since_long_break} пристроїв...")
                    time.sleep(human_pause)
                    _devices_since_long_break = 0
                    _next_break_at = random.randint(8, 12)

                instant_print(f"  ⏳ Перевірка: {link_clean.split('/')[-1]}")
                info = parse_device_details(link, brand_name)
                if info == "STOP":
                    instant_print("🛑 БАН 429 — GSM Arena блокує на 24 години. Зупиняємо весь парсинг.")
                    gsm_banned = True
                    break
                if not info:
                    continue

                processed_urls_in_session.add(link_clean)
                device_name_clean = str(info[1]).strip().lower()

                # Спочатку шукаємо в снапшоті (без зайвого API-виклику).
                # find_row_by_url як запасний варіант для пристроїв доданих у поточній сесії.
                entry = existing_brand_map.get(link_clean)
                actual_row_idx = (
                    entry["row"] if entry else find_row_by_url(main_sheet, link_clean)
                )

                if actual_row_idx:
                    old_data = []
                    while True:
                        try:
                            old_data = main_sheet.row_values(actual_row_idx)
                            break
                        except _NET_ERRORS as e:
                            instant_print(
                                f"    ⚠️ Помилка з'єднання з Google Sheets: {e}"
                            )
                            wait_for_internet()
                    if update_existing_row(
                        main_sheet, actual_row_idx, info, old_data, temp_sheet
                    ):
                        total_updated += 1
                        instant_print(f"    🟠 ОНОВЛЕНО: {info[1]}")
                    ensure_url_fields(main_sheet, actual_row_idx, info)
                else:
                    if device_name_clean in existing_name_map:
                        name_row_idx = existing_name_map[device_name_clean]["row"]
                        old_data = main_sheet.row_values(name_row_idx)
                        if update_existing_row(
                            main_sheet, name_row_idx, info, old_data, temp_sheet
                        ):
                            total_updated += 1
                            instant_print(f"    🟠 ОНОВЛЕНО (по назві): {info[1]}")
                        ensure_url_fields(main_sheet, name_row_idx, info)
                    else:
                        sync_to_google([info], brand_name, temp_sheet)
                        instant_print(f"    ✨ ДОДАНО: {info[1]}")
                        existing_name_map[device_name_clean] = {"row": 0, "data": info}
                        existing_brand_map[link_clean] = {"row": 0, "data": info}

            sort_sheet_by_brand_and_date()
            format_dimensions_by_brand(brand_name)
            instant_print(f"🏁 {brand_name} оброблено.")
            if not gsm_banned:
                brand_pause = random.uniform(300.0, 600.0)
                instant_print(f"⏸ Пауза між брендами: {int(brand_pause)}с ({int(brand_pause)//60}хв {int(brand_pause)%60}с)...")
                time.sleep(brand_pause)

    except Exception as e:
        instant_print(f"🛑 ВИНИКЛА ПОМИЛКА: {e}")
    finally:
        end_time = time.time()
        elapsed = end_time - start_time
        mins, secs = divmod(int(elapsed), 60)
        instant_print(f"\n🏆 ЗАВЕРШЕНО ЗА {mins}хв {secs}сек!")
        instant_print(f"📊 ПІДСУМОК: Додано: {total_added} | Оновлено: {total_updated}")
