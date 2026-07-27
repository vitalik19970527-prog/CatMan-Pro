import logging
import os

# Рівень застосунку: на консоль не виводимо INFO/DEBUG (не засмічувати термінал).
_LOG_LEVEL_CONSOLE = getattr(
    logging, (os.getenv("CATMAN_LOG_CONSOLE") or "WARNING").upper(), logging.WARNING
)
_LOG_LEVEL_FILE = getattr(
    logging, (os.getenv("CATMAN_LOG_FILE") or "INFO").upper(), logging.INFO
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

# Файл: детальніший журнал для діагностики
try:
    _log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _file_handler = logging.FileHandler(
        os.path.join(_log_dir, "catman.log"), encoding="utf-8"
    )
    _file_handler.setLevel(_LOG_LEVEL_FILE)
    _file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    logger.addHandler(_file_handler)
except Exception:
    pass
