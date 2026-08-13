import random
import re
import socket
import sys
import time
import unicodedata
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import cloudscraper  # type: ignore[reportMissingImports]
except ImportError:
    cloudscraper = None

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

EKATALOG_HOME = "https://ek.ua/ua/"


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


def get_headers(referer=EKATALOG_HOME):
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Upgrade-Insecure-Requests": "1",
        "Referer": referer or EKATALOG_HOME,
        "Connection": "keep-alive",
    }


class EkatalogHttpClient:
    def __init__(self):
        self.session = self._new_requests_session()
        self.scraper = self._new_scraper()
        self.last_warmup_ts = 0

    def _new_requests_session(self):
        session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.6,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "HEAD"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _new_scraper(self):
        if cloudscraper is None:
            return None
        try:
            return cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
        except Exception:
            return None

    def _ensure_warmup(self):
        now = time.time()
        if now - self.last_warmup_ts < 120:
            return

        warmup_urls = [
            EKATALOG_HOME,
            # Прогрів для основних категорій, щоб пошук працював стабільніше
            "https://ek.ua/ua/list/122/",  # смартфони
            "https://ek.ua/ua/list/30/",  # планшети
            "https://ek.ua/ua/list/745/",  # годинники
        ]
        for warmup_url in warmup_urls:
            try:
                self.session.get(
                    warmup_url, headers=get_headers(EKATALOG_HOME), timeout=12
                )
            except Exception:
                continue
        self.last_warmup_ts = now

    def _looks_blocked(self, response):
        if response.status_code in (403, 429):
            return True
        body_l = response.text.lower() if response.text else ""
        markers = [
            "access denied",
            "forbidden",
            "attention required",
            "captcha",
        ]
        return any(m in body_l for m in markers)

    def _do_get(self, client, url, timeout, referer):
        return client.get(
            url, headers=get_headers(referer), timeout=timeout, allow_redirects=True
        )

    def get(
        self,
        url,
        timeout=30,
        max_retries=4,
        referer=EKATALOG_HOME,
        allow_scraper_fallback=True,
    ):
        _conn_errs = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
        while True:
            last_error = None
            is_conn_err = False

            for attempt in range(max_retries):
                self._ensure_warmup()
                try:
                    response = self._do_get(self.session, url, timeout, referer)

                    if self._looks_blocked(response):
                        if allow_scraper_fallback and self.scraper is not None:
                            instant_print(
                                "      ⚠️ 403/блок. Перехід на Cloudscraper..."
                            )
                            response = self._do_get(self.scraper, url, timeout, referer)
                        if self._looks_blocked(response):
                            raise requests.HTTPError(
                                f"Blocked by E-katalog (status={response.status_code}) for {url}"
                            )

                    response.raise_for_status()
                    return response
                except _conn_errs as e:
                    last_error = e
                    is_conn_err = True
                    sleep_s = min(12, 2 + attempt * 2) + random.uniform(0.5, 1.4)
                    instant_print(
                        f"      ⚠️ Спроба {attempt + 1}/{max_retries} не вдалась: {e}"
                    )
                    if attempt == 1:
                        self.session = self._new_requests_session()
                    time.sleep(sleep_s)
                except Exception as e:
                    last_error = e
                    sleep_s = min(12, 2 + attempt * 2) + random.uniform(0.5, 1.4)
                    instant_print(
                        f"      ⚠️ Спроба {attempt + 1}/{max_retries} не вдалась: {e}"
                    )
                    if attempt == 1:
                        self.session = self._new_requests_session()
                    time.sleep(sleep_s)

            if is_conn_err:
                wait_for_internet()
            else:
                if last_error:
                    raise last_error
                raise RuntimeError(f"HTTP GET failed for {url}")


EK_HTTP = EkatalogHttpClient()


def clean_name_final(name, keep_year=False):
    if not name:
        return ""
    # Видаляємо HTML сутності
    name = name.replace("&#160;", " ").replace("&nbsp;", " ")
    # Normalization
    name = unicodedata.normalize("NFKC", name)
    name = name.replace("\xa0", " ").replace("\u200b", "")
    name = name.lower()

    # --- ЛОГІКА: Poco та Black Shark (прибираємо Xiaomi) ---
    if "black shark" in name or "poco" in name:
        name = name.replace("xiaomi", "").strip()

    replacements = {
        "zflip": "flip",
        "z flip": "flip",
        "zfold": "fold",
        "z fold": "fold",
        "ztrifold": "trifold",
        "z trifold": "trifold",
    }
    for old, new in replacements.items():
        name = name.replace(old, new)

    # ВИДАЛЯЄМО РОЗМІР КОРПУСУ ГОДИННИКА (мм) — це те саме, що об'єм пам'яті
    # для телефону: просто варіант однієї й тієї ж моделі (40mm/44mm/47mm
    # Watch — одна карта), а не окрема модель. Раніше рядок нижче лише
    # уніфіковував запис ("40 mm"/"40мм" -> "40mm"), не видаляючи токен —
    # він лишався в site_words з цифрою всередині, і правило anti-false-
    # positive в _ek_name_matches (crash guard проти "сайт додає зайвий
    # номер моделі") сприймало "40mm" як НОВЕ покоління й відхиляло
    # збіг — Galaxy Watch9/Watch8/Watch7/Watch6 Classic з розміром у назві
    # ніколи не матчились із уже відстеженим GSM Arena пристроєм і щоразу
    # додавались як дублікат "новий без GSM".
    name = re.sub(r"\d{2}\s?(mm|мм)\b", " ", name)

    # --- ДОДАНО: РОЗДІЛЯЄМО ЗЛИТІ БУКВИ ТА ЦИФРИ (Pad60 -> Pad 60) ---
    # Змінено на розумне розділення (3+ букви), щоб не чіпати F56
    name = re.sub(r"([a-zа-яієґ]{3,})(\d)", r"\1 \2", name)
    name = re.sub(r"(\d)([a-zа-яієґ]{3,})", r"\1 \2", name)

    # Видаляємо мережеві стандарти
    name = re.sub(r"\b(4g|5g|lte|wi-fi|wifi|cellular)\b", " ", name)

    # ВИДАЛЯЄМО ОБ'ЄМ ПАМ'ЯТІ
    name = re.sub(r"\d+\s?([гг][бб]|gb|[тт][бб]|tb)", " ", name)

    # Видаляємо частоту оновлення екрана (наприклад "90Hz", "120Hz") —
    # раніше видаляло БУДЬ-ЯКЕ окреме число "60" без вимоги "Hz" поруч,
    # через що губився номер моделі на кшталт "Motorola Edge 60 Fusion"
    # (ставало "edge fusion"), і пристрій переставав відрізнятись за
    # назвою від інших моделей тієї ж лінійки (напр. "Edge 70 Fusion").
    name = re.sub(r"\b(60|90|120|144|165)\s?(hz|гц)\b", " ", name)

    # Рік, вказаний як єдиний вміст дужок ("(2022)"), — це той самий рік,
    # що й голий "2022": розкриваємо дужки в голий токен ДО видалення тексту
    # в дужках нижче, інакше keep_year=True не захищає рік у цьому випадку.
    name = re.sub(r"\((20\d{2})\)", r" \1 ", name)

    # "(China)" рятуємо так само, як рік вище — це не маркетинговий шум у
    # дужках (кольори тощо), а окрема регіональна версія пристрою, яку в нас
    # на ринку не продають. Без цього рятування generic-видалення тексту в
    # дужках нижче стирало "china" повністю, і China-версія з GSM Arena
    # починала збігатись з тим самим E-Katalog оголошенням, що й звичайна —
    # обидві версії підтягували одні й ті самі дані ЄК.
    if re.search(r"\bchina\b", name):
        name = re.sub(r"\bchina\b", " ", name) + " china"

    # ВИДАЛЯЄМО РОКИ (тільки якщо keep_year=False)
    if not keep_year:
        name = re.sub(r"\b(20\d{2})\b", " ", name)

    name = re.sub(r"\b(m1|m2|m3|m4|m5)\b", " ", name)
    name = re.sub(r"\bseries\b", " ", name)
    name = re.sub(r"\btitanium\b", " ", name)
    name = re.sub(r"\bsteel\b", " ", name)

    # Видаляємо текст в дужках
    name = re.sub(r"\(.*?\)", " ", name)

    # --- НОВА ЛОГІКА: + = plus ---
    # Замінюємо символ '+' на слово 'plus', щоб назви з таблиці та сайту збігалися
    name = name.replace("+", " plus ")

    # Очищуємо спеціальні символи (тепер '+' вже замінено на текст, тому не зникне)
    name = name.replace("/", " ")

    # Видаляємо всі інші спецсимволи, крім букв і цифр
    name = re.sub(r"[^\w\s]", " ", name)

    return " ".join(name.split()).strip()


def clean_name_for_search(name):
    if not name:
        return ""
    # Очищуємо нерозривні пробіли та переводимо в нижній регістр одразу для всієї обробки
    name = name.replace("\xa0", " ").replace("\u200b", "").lower()

    # --- ЛОГІКА "В ЛОБ" ДЛЯ ПОШУКУ ---
    replacements = {
        "zflip": "flip",
        "z flip": "flip",
        "zfold": "fold",
        "z fold": "fold",
        "ztrifold": "trifold",
        "z trifold": "trifold",
    }
    for old, new in replacements.items():
        name = name.replace(old, new)

    # --- ГОЛОВНЕ: РОЗДІЛЯЄМО ЗЛИТІ БУКВИ ТА ЦИФРИ (Flip7 -> flip 7) ---
    # Синхронізовано з clean_name_final (3+ букви)
    name = re.sub(r"([a-zа-яієґ]{3,})(\d)", r"\1 \2", name)
    name = re.sub(r"(\d)([a-zа-яієґ]{3,})", r"\1 \2", name)

    # Прибираємо Xiaomi для суббрендів
    if "poco" in name or "black shark" in name:
        name = re.sub(r"\bxiaomi\b", "", name, flags=re.I).strip()

    # Видаляємо службові слова
    name = re.sub(r"\b(series|titanium|steel)\b", "", name, flags=re.I)

    # Видаляємо мережеві стандарти (4G/5G/LTE)
    name = re.sub(r"\b(4G|5G|LTE)\b", "", name, flags=re.I)

    # Видаляємо пам'ять (ГБ/GB), але НЕ чіпаємо роки (2024, 2025 тощо)
    name = re.sub(r"\d+\s?(ГБ|GB)(?:\s?/\s?\d+\s?(ГБ|GB))?", "", name, flags=re.I)

    # Додано для кращого пошуку Flip/Fold (plus)
    name = name.replace("+", " plus ")

    # Видаляємо зайві пробіли
    return " ".join(name.split()).strip()


def clean_price_for_compare(price_str):
    if not price_str:
        return ""
    price_str = str(price_str).split(",")[0]
    return re.sub(r"[^\d]", "", price_str)


def parse_ekatalog_item(url, target_net, dev_type, skip_net_check=False):
    max_retries = 4  # Кількість спроб
    retry_delay = 45  # Пауза в секундах при технічних роботах

    for attempt in range(max_retries):
        try:
            table_url = url + ("&" if "?" in url else "?") + "view_=tbl"
            time.sleep(random.uniform(0.8, 2.5))

            try:
                res = EK_HTTP.get(table_url, timeout=35, max_retries=3, referer=url)

                # Перевірка на технічні роботи/блокування — a bad page (site
                # maintenance, CDN/anti-bot block page, error page) must
                # NEVER be treated as a valid product page. This check used
                # to only catch E-Katalog's own Ukrainian maintenance text —
                # an English "Under Maintenance" page (served by the
                # host/CDN layer, not the site itself) slipped straight
                # through to the normal parsing path below, which grabbed
                # the bad page's own <h1>/data-txt-title as if it were the
                # real product name — confirmed live, corrupted 478 already-
                # tracked devices' names/history in one run before this was
                # caught. Broadened to a general blocklist (not just this
                # one phrase) so the SAME class of bug can't recur under a
                # different page signature — on ANY match: retry, and if
                # every retry still sees it, parse_ekatalog_item returns
                # None (see bottom of function) — callers already treat
                # None as "skip this item", never as "clear the existing
                # name/data". A blocked/broken fetch must always be a
                # no-op, never an overwrite.
                _bad_page_markers = (
                    "сайт на обслуговуванні",
                    "технічні роботи",
                    "under maintenance",
                    "access denied",
                    "403 forbidden",
                    "attention required",  # Cloudflare block/challenge page
                    "just a moment",  # Cloudflare "checking your browser"
                    "service unavailable",
                    "502 bad gateway",
                    "503 service",
                )
                _res_text_lower = res.text.lower()
                if any(marker in _res_text_lower for marker in _bad_page_markers):
                    instant_print(
                        f"      ⏳ Спроба {attempt+1}: Сайт на обслуговуванні. Пауза {retry_delay}с..."
                    )
                    time.sleep(retry_delay)
                    continue
            except Exception as e:
                instant_print(f"      ❌ Помилка мережі (timeout/connection): {e}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return None

            soup = BeautifulSoup(res.text, "html.parser")

            def get_val_by_label(label_text):
                target_td = None
                search_label = label_text.strip().lower()
                for td in soup.find_all("td", class_="op1"):
                    full_text = td.get_text().strip().lower()
                    if search_label == full_text or full_text.startswith(search_label):
                        target_td = td
                        break
                if target_td:
                    next_td = target_td.find_next_sibling("td", class_="op3")
                    if next_td:
                        return next_td.get_text(" ", strip=True)
                return ""

            # --- БАЗОВІ ЦІНИ ТА ЗБІР ДАНИХ ДЛЯ СТАТУСУ ---
            price_block = soup.find("div", class_="desc-big-price")
            price_low, price_high, status = "-", "-", "Очікується"
            shop_count = "0"

            date_raw = ""
            date_label = soup.find(string=re.compile(r"Дата додавання", re.I))
            if date_label:
                try:
                    date_raw = (
                        date_label.find_parent("td")
                        .find_next_sibling("td")
                        .get_text(strip=True)
                    )
                except Exception:
                    pass

            # 1. Пошук кількості магазинів для фолбеку
            shop_sub_el = soup.find("sub", id=re.compile(r"num_prices"))
            if shop_sub_el:
                shop_count = re.sub(r"[^\d]", "", shop_sub_el.get_text())
            else:
                shop_link = soup.find("a", href=re.compile(r"/ua/shop/|/ua/prices/"))
                if shop_link:
                    sh_match = re.search(r"(\d+)", shop_link.get_text())
                    if sh_match:
                        shop_count = sh_match.group(1)

            # 2. Збір базових цін з головної сторінки (для використання у AR-AT)
            if price_block:
                prices = re.findall(r"([\d\s]+)", price_block.text.replace("\xa0", ""))
                prices = [p.replace(" ", "") for p in prices if p.strip().isdigit()]
                if len(prices) >= 2:
                    price_low, price_high = prices[0], prices[1]
                elif len(prices) == 1:
                    price_low, price_high = prices[0], prices[0]

            # 3. Попереднє визначення статусу на основі дати додавання (стовпчик Y)
            date_status = "Очікується"
            if date_raw:
                try:
                    months_ukr = {
                        "січень": 1,
                        "лютий": 2,
                        "березень": 3,
                        "квітень": 4,
                        "травень": 5,
                        "червень": 6,
                        "липень": 7,
                        "серпень": 8,
                        "вересень": 9,
                        "жовтень": 10,
                        "листопад": 11,
                        "грудень": 12,
                    }
                    d_parts = date_raw.lower().split()
                    m_idx, y_val = 1, 2024
                    for p in d_parts:
                        if p in months_ukr:
                            m_idx = months_ukr[p]
                        if p.isdigit() and len(p) == 4:
                            y_val = int(p)

                    date_added = datetime(y_val, m_idx, 1)
                    now = datetime.now()
                    diff_months = (
                        (now.year - date_added.year) * 12 + now.month - date_added.month
                    )

                    if diff_months < 4:
                        date_status = "Новинка! Очікується"
                    elif diff_months > 18:
                        date_status = "Застарів"
                    elif diff_months > 4:
                        date_status = "Очікується"
                except Exception:
                    pass

            # --- ЛОГІКА ПЕРЕХОДУ ПО КАРТКАХ ТОВАРУ (ВАРІАЦІЇ) ---
            full_variations = []

            # 1. Шукаємо кнопки (пам'ять, розмір) - РОЗШИРЕНО REGEX для ic02, ic03 і т.д.
            size_mem_block = soup.find("div", class_="m-c-f1-pl--inline") or soup.find(
                "div", id=re.compile(r"^(ibzz|ic\d{1,2}|ic0\d)")
            )

            # 2. Шукаємо кольори (плашки)
            color_block = soup.find(class_=re.compile(r"color-plate"))
            color_elements = (
                color_block.find_all("div", title=True) if color_block else []
            )

            def get_variant_data(v_url):
                if not v_url or not v_url.startswith("http"):
                    return price_low, price_high, shop_count
                try:
                    time.sleep(random.uniform(1.8, 3.5))
                    v_res = EK_HTTP.get(v_url, timeout=20, max_retries=3, referer=url)
                    v_soup = BeautifulSoup(v_res.text, "html.parser")

                    # Пошук ціни
                    v_low, v_high = price_low, price_high
                    p_block = v_soup.find("div", class_="desc-big-price")
                    if p_block:
                        p_found = re.findall(
                            r"([\d\s]+)", p_block.text.replace("\xa0", "")
                        )
                        p_found = [
                            p.replace(" ", "") for p in p_found if p.strip().isdigit()
                        ]
                        if len(p_found) >= 2:
                            v_low, v_high = p_found[0], p_found[1]
                        elif len(p_found) == 1:
                            v_low, v_high = p_found[0], p_found[0]

                    # Пошук кількості магазинів саме для цієї варіації
                    v_shops = "0"
                    s_sub = v_soup.find("sub", id=re.compile(r"num_prices"))
                    if s_sub:
                        v_shops = re.sub(r"[^\d]", "", s_sub.get_text())
                    else:
                        # Якщо тег sub не знайдено, спробуємо витягнути з тексту поруч із ціною
                        shop_link = v_soup.find("a", href=re.compile(r"/ua/shop/"))
                        if shop_link:
                            sh_match = re.search(r"(\d+)", shop_link.get_text())
                            if sh_match:
                                v_shops = sh_match.group(1)

                    return v_low, v_high, v_shops
                except Exception:
                    return price_low, price_high, shop_count

            # --- 1. ПЕРЕВІРКА ТА ЗБІР ВАРІАЦІЙ ---
            full_variations = []
            found_any_button = False

            if size_mem_block:
                spans = size_mem_block.find_all(
                    ["span", "a"], class_=re.compile(r"ib|m-c-f1-pl-lnk")
                )
                for s in spans:
                    for hidden in s.find_all(
                        string=re.compile("Тільки|Только|розділах", re.I)
                    ):
                        hidden.extract()

                    v_name = s.get_text(" ", strip=True).replace("\xa0", " ").strip()
                    if not v_name or len(v_name) < 2:
                        continue

                    found_any_button = True
                    a_tag = s if s.name == "a" else s.find("a")
                    v_url = ""
                    if a_tag and a_tag.get("href"):
                        href = a_tag.get("href")
                        v_url = (
                            "https://ek.ua" + href
                            if not href.startswith("http")
                            else href
                        )

                    # Отримуємо ціни та магазини для конкретної кнопки
                    v_low, v_high, v_sh = (
                        get_variant_data(v_url)
                        if v_url
                        else (price_low, price_high, shop_count)
                    )

                    if color_elements:
                        for ce in color_elements:
                            c_name = ce.get("title", "").strip()
                            if not c_name or "розділах" in c_name.lower():
                                continue
                            full_variations.append(
                                (f"{v_name}, {c_name}", v_low, v_high, v_sh)
                            )
                    else:
                        full_variations.append((v_name, v_low, v_high, v_sh))

            # Варіант 3: Тільки кольори
            if not found_any_button and color_elements:
                for ce in color_elements:
                    c_name = ce.get("title", "").strip()
                    if not c_name or "розділах" in c_name.lower():
                        continue

                    jsource = ce.get("jsource", "")
                    c_url = ""
                    if "idg_=" in jsource:
                        params = dict(re.findall(r"(\w+)=([^&]+)", jsource))
                        if "idg_" in params:
                            c_url = (
                                f"https://ek.ua/ua/ek-item.php?idg_={params['idg_']}"
                            )
                            if "colors_" in params:
                                c_url += f"&colors_={params['colors_']}"

                    cl_low, cl_high, cl_sh = (
                        get_variant_data(c_url)
                        if c_url
                        else (price_low, price_high, shop_count)
                    )
                    full_variations.append((c_name, cl_low, cl_high, cl_sh))

            # Варіант 4: Базова модель
            if not found_any_button and not color_elements:
                full_variations.append(("", price_low, price_high, shop_count))

            # --- 2. ФОРМУВАННЯ КОНТЕНТУ ДЛЯ ТАБЛИЦІ (AP, AQ, AR, AS, AT) ---
            if full_variations:
                # AP: Унікальні розміри/пам'ять (тільки ті, де Є цифри)
                all_sizes_raw = list(
                    dict.fromkeys([v[0].split(",")[0].strip() for v in full_variations])
                )
                unique_sizes = [
                    s for s in all_sizes_raw if any(char.isdigit() for char in s)
                ]

                # AQ: Унікальні кольори (тільки ті, де НЕМАЄ цифр)
                all_colors_raw = list(
                    dict.fromkeys(
                        [v[0].split(",")[-1].strip() for v in full_variations]
                    )
                )
                unique_colors = [
                    c for c in all_colors_raw if not any(char.isdigit() for char in c)
                ]
                aq_content = "\n".join(unique_colors)

                p_low_list = []
                p_high_list = []
                p_shops_list = []

                if unique_sizes:
                    ap_content = "\n".join(unique_sizes)
                    for size in unique_sizes:
                        match = next(
                            (v for v in full_variations if v[0].startswith(size)), None
                        )
                        if match:
                            p_low_list.append(match[1])
                            p_high_list.append(match[2])
                            p_shops_list.append(match[3])
                        else:
                            p_low_list.append(price_low)
                            p_high_list.append(price_high)
                            p_shops_list.append(shop_count)
                else:
                    ap_content = ""
                    p_low_list.append(price_low)
                    p_high_list.append(price_high)
                    p_shops_list.append(shop_count)

                ar_content = "\n".join(p_low_list)
                as_content = "\n".join(p_high_list)
                at_content = "\n".join(p_shops_list)

                # --- ЛОГІКА РОЗРАХУНКУ AL, AM ТА AK ---
                all_possible_lows = [
                    int(v[1]) for v in full_variations if str(v[1]).isdigit()
                ]
                if str(price_low).isdigit():
                    all_possible_lows.append(int(price_low))

                all_possible_highs = [
                    int(v[2]) for v in full_variations if str(v[2]).isdigit()
                ]
                if str(price_high).isdigit():
                    all_possible_highs.append(int(price_high))

                all_possible_shops = [
                    int(v[3]) for v in full_variations if str(v[3]).isdigit()
                ]
                if str(shop_count).isdigit():
                    all_possible_shops.append(int(shop_count))

                final_al = (
                    str(min(all_possible_lows)) if all_possible_lows else price_low
                )
                final_am = (
                    str(max(all_possible_highs)) if all_possible_highs else price_high
                )
                final_ak = (
                    str(max(all_possible_shops)) if all_possible_shops else shop_count
                )
            else:
                ap_content = aq_content = ar_content = as_content = at_content = ""
                final_al, final_am = price_low, price_high
                final_ak = shop_count

            # --- ФІНАЛЬНЕ ВИЗНАЧЕННЯ СТАТУСУ (AJ) ---
            try:
                val_ak = int(final_ak) if str(final_ak).isdigit() else 0
                val_al = int(final_al) if str(final_al).isdigit() else 0
            except Exception:
                val_ak = 0
                val_al = 0

            # Розраховуємо вік моделі заново для внутрішньої логіки
            current_diff_months = 999
            if date_raw:
                try:
                    months_ukr = {
                        "січень": 1,
                        "лютий": 2,
                        "березень": 3,
                        "квітень": 4,
                        "травень": 5,
                        "червень": 6,
                        "липень": 7,
                        "серпень": 8,
                        "вересень": 9,
                        "жовтень": 10,
                        "листопад": 11,
                        "грудень": 12,
                    }
                    d_parts = date_raw.lower().split()
                    m_idx, y_val = 1, 2024
                    for p in d_parts:
                        if p in months_ukr:
                            m_idx = months_ukr[p]
                        if p.isdigit() and len(p) == 4:
                            y_val = int(p)

                    date_added = datetime(y_val, m_idx, 1)
                    now = datetime.now()
                    current_diff_months = (
                        (now.year - date_added.year) * 12 + now.month - date_added.month
                    )
                except Exception:
                    pass

            # Визначаємо фінальний статус
            if val_ak > 0 and val_al > 0:
                if current_diff_months < 6:
                    status = "Продаж! Новинка"
                else:
                    status = "Продаж"
            else:
                status = date_status

            # --- ПЕРЕВІРКА МЕРЕЖІ ---
            match_net = True
            found_net_desc = "Checked"
            if not skip_net_check and dev_type not in ["Tablet", "Watch"]:
                net_info_main = get_val_by_label("Зв'язок")
                tech_block = soup.find("div", class_="m-c-f1")
                net_info_extra = (
                    tech_block.get_text(" ", strip=True) if tech_block else ""
                )
                full_net_text = (net_info_main + " " + net_info_extra).upper()
                site_has_5g = "5G" in full_net_text
                site_has_4g = any(
                    x in full_net_text for x in ["4G", "LTE", "SIM-CARD", "CELLULAR"]
                )
                t_net = str(target_net).strip().upper()
                if t_net == "5G":
                    match_net = site_has_5g
                elif t_net == "4G":
                    match_net = site_has_4g and not site_has_5g
                elif t_net == "NO":
                    match_net = not site_has_4g and not site_has_5g
                if not match_net:
                    found_net_desc = (
                        "5G"
                        if site_has_5g
                        else ("4G" if site_has_4g else "No (Wi-Fi only)")
                    )
                    return {"fail_net": True, "net_found": found_net_desc}

            # --- ХАРАКТЕРИСТИКИ ---
            title_div = soup.find("div", id="top-page-title")
            name = (
                title_div.get("data-txt-title", "").replace("&nbsp;", " ").strip()
                if title_div
                else ""
            )
            if not name:
                name = (
                    soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else ""
                )

            # Hard guard, independent of the page-level check above: the
            # extracted "product name" itself must never BE one of the known
            # bad-page titles, whatever produced it. This is the second of
            # two layers on purpose — the check above can only catch phrases
            # already in _bad_page_markers; if a differently-worded bad page
            # (a new CDN provider, a translated error page, etc.) slips past
            # it, this still refuses to hand back a name that IS the error
            # itself, forcing a retry/None instead of silently overwriting
            # an existing device's real name.
            if name.strip().lower() in _bad_page_markers:
                instant_print(
                    f"      ⏳ Спроба {attempt+1}: назва товару виявилась текстом"
                    f" помилки сторінки ({name!r}). Пауза {retry_delay}с..."
                )
                time.sleep(retry_delay)
                continue

            dims_raw = get_val_by_label("Розміри")
            length, w, t, dims_standard = "", "", "", ""
            if dims_raw:
                d_match = re.findall(r"(\d+\.?\d*)", dims_raw)
                if len(d_match) >= 3:
                    length, w, t = d_match[0], d_match[1], d_match[2]
                    dims_standard = f"{length}*{w}*{t}"

            weight = re.sub(r"[^\d]", "", get_val_by_label("Вага"))

            display_raw = ""
            if dev_type == "Watch":
                display_raw = get_val_by_label("Діагональ") or get_val_by_label(
                    "Основний дисплей"
                )
            elif dev_type == "Tablet":
                for td in soup.find_all("td", class_="op1"):
                    if "дисплей" in td.get_text().lower() and not td.find(
                        "span", class_="op1-title"
                    ):
                        val_td = td.find_next_sibling("td", class_="op3")
                        if val_td:
                            display_raw = val_td.get_text("|").split("|")[0].strip()
                            break
            else:
                display_raw = get_val_by_label("Основний дисплей")

            display = ""
            if display_raw:
                diag_match = re.search(r"(\d+\.?\d*)", display_raw)
                if diag_match:
                    display = diag_match.group(1).strip()

            os_info = get_val_by_label("Операційна система")
            cpu_info = get_val_by_label("Процесор")
            ram = get_val_by_label("Оперативна пам'ять")
            rom = get_val_by_label("Вбудована пам'ять")
            mem_info = f"{ram} / {rom}" if ram and rom else (ram or rom)

            battery_raw = get_val_by_label("Ємність батареї")
            battery_mah = ""
            if battery_raw:
                if "Вт*год" in battery_raw:
                    wh_match = re.search(r"(\d+\.?\d*)", battery_raw)
                    if wh_match:
                        wh_val = float(wh_match.group(1))
                        battery_mah = str(int(wh_val * 1000 / 3.7))
                else:
                    battery_mah = re.sub(r"[^\d]", "", battery_raw)

            # --- ФОТО (для пристроїв без GSM Arena — E-Katalog як єдине джерело) ---
            # Extracted from the already-fetched soup, no extra request. Prefer the
            # full-size "zoom" image over the small listing thumbnail.
            photo_main = ""
            zoom_div = soup.find("div", class_="list-zoomer")
            if zoom_div:
                zm = re.search(
                    r"open_pg\('(https?://[^']+)'", str(zoom_div.get("onclick") or "")
                )
                if zm:
                    photo_main = zm.group(1)
            if not photo_main:
                img_tag = soup.find("img", id=re.compile(r"^img_200_"))
                if img_tag and img_tag.get("src"):
                    photo_main = str(img_tag.get("src"))

            photo_gallery = []
            for gitem in soup.find_all("div", class_="i15-item"):
                gm = re.search(
                    r"open_pg\('(https?://[^']+\.(?:jpe?g|png|webp))'",
                    str(gitem.get("onclick") or ""),
                    re.I,
                )
                if gm and gm.group(1) not in photo_gallery:
                    photo_gallery.append(gm.group(1))
                if len(photo_gallery) >= 7:
                    break

            return {
                "W": name,
                "X": target_net,
                "Y": date_raw,
                "Z": dims_standard,
                "AA": length,
                "AB": w,
                "AC": t,
                "AD": weight,
                "AE": display,
                "AF": os_info,
                "AG": cpu_info,
                "AH": mem_info,
                "AI": battery_mah,
                "AJ": status,
                "AK": final_ak,
                "AL": final_al,
                "AM": final_am,
                "AN": url,
                "AP": ap_content,
                "AQ": aq_content,
                "AR": ar_content,
                "AS": as_content,
                "AT": at_content,
                "PHOTO_MAIN": photo_main,
                "PHOTO_GALLERY": photo_gallery,
            }

        except Exception as e:
            instant_print(
                f"      ❌ Помилка парсингу сторінки (спроба {attempt+1}): {e}"
            )
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return None

    return None
