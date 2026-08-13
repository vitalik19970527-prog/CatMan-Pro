import logging
import logging.handlers
import os

# Рівень застосунку: ні на консоль, ні у файл не виводимо INFO/DEBUG —
# лише попередження та аварійні помилки (logger.warning/error/exception/
# critical). Застосунок раніше писав сотні logger.info/debug викликів
# (тимчасове діагностичне логування, додане під час пошуку фрізів) — тепер,
# коли всі знайдені причини фрізів підтверджено виправлено, це лише зайве
# I/O на кожен виклик у гарячих шляхах (введення в полях, сортування,
# перемикання вкладок) та необмежене зростання лог-файлу. Підняти рівень
# тимчасово назад до INFO/DEBUG через CATMAN_LOG_FILE/CATMAN_LOG_CONSOLE,
# якщо знадобиться діагностика — сам виклик logger.info(...) лишається
# в коді, просто відфільтровується тут.
_LOG_LEVEL_CONSOLE = getattr(
    logging, (os.getenv("CATMAN_LOG_CONSOLE") or "WARNING").upper(), logging.WARNING
)
_LOG_LEVEL_FILE = getattr(
    logging, (os.getenv("CATMAN_LOG_FILE") or "WARNING").upper(), logging.WARNING
)

logger = logging.getLogger("catman")
logger.handlers.clear()
# Повідомлення формуються; фільтрація для консолі/файлу — рівнями handlers нижче.
logger.setLevel(logging.DEBUG)
logger.propagate = False
logger.disabled = False

# Консоль: лише попередження та помилки
_console_handler = logging.StreamHandler()
_console_handler.setLevel(_LOG_LEVEL_CONSOLE)
_console_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)
logger.addHandler(_console_handler)

# Файл: детальніший журнал для діагностики. %APPDATA%\CatManPro\logs (same
# base app_storage.py uses for cache files) instead of a path relative to
# the installed script — the installer puts the app in Program Files, which
# a non-admin user can't write to, so a script-relative "logs" folder here
# silently failed to attach (bare except below) and the file handler simply
# never existed for a normal installed run. This module loads before
# app_storage.apply_storage_dir_override() runs (main.py imports
# admin_dashboard, which imports this, before it calls that), so it can't
# rely on FLET_APP_STORAGE_DATA being set yet — %APPDATA% directly is always
# available and user-writable regardless of install location or import order.
try:
    _log_dir = os.path.join(
        os.getenv("APPDATA") or os.path.expanduser("~"), "CatManPro", "logs"
    )
    os.makedirs(_log_dir, exist_ok=True)
    # Rotating, not plain FileHandler — the old unbounded file grew to ~39MB
    # over time. 5MB x 3 backups keeps recent history without unbounded growth.
    _file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(_log_dir, "catman.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _file_handler.setLevel(_LOG_LEVEL_FILE)
    _file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(_file_handler)
except Exception:
    pass
