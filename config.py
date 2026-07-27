import os
import sys


def _load_dotenv_file():
    """Minimal .env loader to avoid extra dependencies."""
    candidates = []

    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(script_dir, ".env"))

    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        candidates.append(os.path.join(meipass, ".env"))

    exe_dir = (
        os.path.dirname(os.path.abspath(sys.executable or ""))
        if getattr(sys, "executable", "")
        else ""
    )
    if exe_dir:
        candidates.append(os.path.join(exe_dir, ".env"))

    candidates.append(os.path.join(os.getcwd(), ".env"))

    seen = set()
    for env_path in candidates:
        normalized = os.path.normcase(os.path.normpath(env_path))
        if normalized in seen or not os.path.exists(env_path):
            continue
        seen.add(normalized)

        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
            return
        except Exception:
            # Keep app startup resilient even if .env is malformed.
            pass


_load_dotenv_file()


class Config:
    # API base URL (local PC now, VPS later)
    CATMAN_API_BASE_URL = (
        os.getenv("CATMAN_API_BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/")
    )

    # Optional account-related settings (keep empty by default to avoid personal data in code)
    SUPERADMIN_BOOTSTRAP_EMAILS = os.getenv("SUPERADMIN_BOOTSTRAP_EMAILS", "")
    SUPERADMIN_EMAIL = os.getenv("SUPERADMIN_EMAIL", "")
    WORK_EMAIL = os.getenv("WORK_EMAIL", "")

    # в”Ђв”Ђв”Ђ Р’РµСЂСЃС–СЏ РїСЂРѕРіСЂР°РјРё в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
    # Р—РјС–РЅСЋР№С‚Рµ С†РµР№ СЂСЏРґРѕРє РїСЂРё РєРѕР¶РЅРѕРјСѓ СЂРµР»С–Р·С–.
    # Р¤РѕСЂРјР°С‚: MAJOR.MINOR.PATCH  (РЅР°РїСЂРёРєР»Р°Рґ "1.2.0")
    APP_VERSION = "1.2.8"

    # в”Ђв”Ђв”Ђ РџРѕСЃРёР»Р°РЅРЅСЏ РЅР° СЃРєР°С‡СѓРІР°РЅРЅСЏ РѕРЅРѕРІР»РµРЅРЅСЏ в”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђв”Ђ
    # Р’СЃС‚Р°РІС‚Рµ СЃСЋРґРё РїСЂСЏРјРµ РїРѕСЃРёР»Р°РЅРЅСЏ РЅР° .exe РїС–СЃР»СЏ С‚РѕРіРѕ СЏРє Р±СѓРґРµРјРѕ РіРѕС‚РѕРІС– РґРѕ СЂРµР»С–Р·Сѓ.
    # РџСЂРёРєР»Р°Рґ GitHub Releases: "https://github.com/YOUR_USER/catman-pro/releases/latest/download/CatManPro.exe"
    UPDATE_DOWNLOAD_URL = os.getenv("UPDATE_DOWNLOAD_URL", "")

    # Online-fast profile: keep hot data in memory briefly for instant UI while syncing aggressively.
    DB_READ_CACHE_ENABLED = str(
        os.getenv("DB_READ_CACHE_ENABLED", "1")
    ).strip().lower() in {"1", "true", "yes", "on"}

    # Generic cache TTL (seconds) for non-hot reads.
    DB_DEFAULT_CACHE_TTL_SECONDS = int(
        os.getenv("DB_DEFAULT_CACHE_TTL_SECONDS", "8") or "8"
    )

    # TTL for frequently changing lists (orders/products). Larger = fewer DB/API hits + cooler CPU.
    HOT_COLLECTIONS_TTL_SECONDS = int(
        os.getenv("HOT_COLLECTIONS_TTL_SECONDS", "6") or "6"
    )

    # Enable cross-client sync pulses.
    # Keep enabled for real-time updates between users.
    SYNC_PULSES_ENABLED = str(
        os.getenv("SYNC_PULSES_ENABLED", "1")
    ).strip().lower() in {"1", "true", "yes", "on"}

    # Sync profile: normal | eco | shared_live | cache_only
    # Default eco: gentler polling on laptops (set shared_live via .env for aggressive multi-user sync).
    SYNC_POLL_MODE = os.getenv("SYNC_POLL_MODE", "eco").strip().lower()

    # Poll interval for shared_live mode (seconds).
    SHARED_LIVE_SYNC_INTERVAL_SECONDS = float(
        os.getenv("SHARED_LIVE_SYNC_INTERVAL_SECONDS", "8") or "8"
    )

    # Floor interval for shared_live (seconds); avoids sub-second/timer storms when SHARED_LIVE is set too low.
    SHARED_LIVE_SYNC_MIN_INTERVAL_SECONDS = float(
        os.getenv("SHARED_LIVE_SYNC_MIN_INTERVAL_SECONDS", "2") or "2"
    )

    # Seconds between automatic full-screen rebuilds from sync pulses.
    SYNC_VIEW_RELOAD_MIN_INTERVAL_SECONDS = float(
        os.getenv("SYNC_VIEW_RELOAD_MIN_INTERVAL_SECONDS", "12") or "12"
    )

    # eco mode poll cadence (seconds). Longer stretches save battery and keep UI idle.
    SYNC_ECO_FAST_SECONDS = float(os.getenv("SYNC_ECO_FAST_SECONDS", "5") or "5")
    SYNC_ECO_IDLE1_SECONDS = float(os.getenv("SYNC_ECO_IDLE1_SECONDS", "8") or "8")
    SYNC_ECO_IDLE2_SECONDS = float(os.getenv("SYNC_ECO_IDLE2_SECONDS", "18") or "18")
    SYNC_ECO_IDLE3_SECONDS = float(os.getenv("SYNC_ECO_IDLE3_SECONDS", "35") or "35")

    # Brief faster polling after external change detection (eco/normal); kept >1s so the loop stays cheap.
    SYNC_POST_CHANGE_POLL_SECONDS = float(
        os.getenv("SYNC_POST_CHANGE_POLL_SECONDS", "3") or "3"
    )

    # В«Р’Р·Р°С”РјРѕСЂРѕР·СЂР°С…СѓРЅРєРёВ»: С„РѕРЅРѕРІС– РїРµСЂРµРІС–СЂРєРё Р·Р°РјРѕРІР»РµРЅСЊ Р±РµР· РїРѕРІРЅРѕС— РїРµСЂРµР±СѓРґРѕРІРё РІРєР»Р°РґРєРё.
    PAYMENTS_AUTO_REFRESH_INTERVAL_SECONDS = float(
        os.getenv("PAYMENTS_AUTO_REFRESH_INTERVAL_SECONDS", "15") or "15"
    )

    # В«Р—Р°РјРѕРІР»РµРЅРЅСЏВ»: РјС–РЅС–РјСѓРј РјС–Р¶ С…РІРёР»СЏРјРё live-row sync (get_order_by_id РїРѕ РІС–РґРєСЂРёС‚РёС… СЂСЏРґРєР°С…).
    ORDERS_LIVE_SYNC_DEBOUNCE_SECONDS = float(
        os.getenv("ORDERS_LIVE_SYNC_DEBOUNCE_SECONDS", "2.0") or "2.0"
    )

    # Building virtual "series as product" rows can be expensive on large catalogs.
    PRODUCTS_ENABLE_VIRTUAL_ROWS = str(
        os.getenv("PRODUCTS_ENABLE_VIRTUAL_ROWS", "0")
    ).strip().lower() in {"1", "true", "yes", "on"}

    # Remote mode: when app is used over remote connection / hosted desktop.
    # In this mode only SuperAdmin should be able to edit; others are view-only.
    REMOTE_MODE = str(os.getenv("REMOTE_MODE", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    # Cache TTL for sync pulse reads (seconds).
    SYNC_CACHE_TTL_SECONDS = int(os.getenv("SYNC_CACHE_TTL_SECONDS", "1") or "1")

    # Runtime cache only; freshness is prioritized over long-lived persistence.
    PERSISTENT_CACHE_FOREVER = str(
        os.getenv("PERSISTENT_CACHE_FOREVER", "0")
    ).strip().lower() in {"1", "true", "yes", "on"}










































































