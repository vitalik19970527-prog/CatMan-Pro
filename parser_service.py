#!/usr/bin/env python3
"""
GSM Parser VPS Service
======================
Розгортання на VPS (Ubuntu/Debian):

    pip install fastapi uvicorn apscheduler httpx

    # Запуск:
    python parser_service.py

    # Або через systemd / screen / tmux:
    uvicorn parser_service:app --host 0.0.0.0 --port 8765

    # Автозапуск через systemd:
    # /etc/systemd/system/gsm-parser.service
    # [Unit] Description=GSM Parser Service
    # [Service] WorkingDirectory=/opt/catman ExecStart=/usr/bin/python3 /opt/catman/parser_service.py
    # [Install] WantedBy=multi-user.target

Налаштування надсилаються з Flet-додатку через POST /configure.
Конфіг зберігається у parser_service_config.json поруч з цим файлом.
Всі endpoint-и (крім GET /status) потребують заголовку x-api-key.
"""
import contextlib
import json
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _DIR / "parser_service_config.json"
_LOG_PATH = _DIR / "gsm_last_parse.log"

_DEFAULT_CONFIG: dict = {
    "api_key": "",        # empty = no auth check
    "schedule_time": "",  # "HH:MM" or "" = disabled
    "database_url": "",   # PostgreSQL DSN
}


def _load_cfg() -> dict:
    if _CONFIG_PATH.exists():
        with contextlib.suppress(Exception):
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    return dict(_DEFAULT_CONFIG)


def _save_cfg(cfg: dict) -> None:
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


_cfg = _load_cfg()

# ── FastAPI ───────────────────────────────────────────────────────────────────

try:
    from fastapi import FastAPI, HTTPException, Header
    from pydantic import BaseModel
except ImportError:
    sys.exit("Install deps:  pip install fastapi uvicorn pydantic apscheduler")

app = FastAPI(title="GSM Parser Service", version="1.0.0")


def _require_auth(x_api_key: str = "") -> None:
    key = str(_cfg.get("api_key") or "").strip()
    if key and str(x_api_key or "").strip() != key:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Scheduler ─────────────────────────────────────────────────────────────────

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler(timezone="Europe/Kiev")
    _scheduler.start()
except ImportError:
    print("[WARNING] apscheduler not installed — auto-schedule disabled", file=sys.stderr)
    _scheduler = None

_parse_lock = threading.Lock()


def _run_parse(mode: str = "full") -> None:
    """Execute the parser. Called by scheduler or /trigger endpoint.

    _parse_lock only guards against two /trigger calls racing each other
    IN THIS PROCESS. gsm_parser_scheduler.py's --daemon is a separate
    process with its own scheduled run_times check — a manual trigger
    landing close to that schedule used to run fully in parallel with it
    (confirmed live: two overlapping ~5h scrapes, one shared corrupted log,
    two Telegram digests). The DB-backed lock below is the cross-process
    guard; whichever side asks first wins, the other skips."""
    if not _parse_lock.acquire(blocking=False):
        print(f"[{datetime.now():%H:%M:%S}] Parser already running — skipped", flush=True)
        return
    db_lock_owner = "trigger-service"
    db_lock_acquired = False
    _lock_db = None
    try:
        db_url = str(_cfg.get("database_url") or "").strip()
        if db_url:
            os.environ["DATABASE_URL"] = db_url
        from database import DataManager
        from gsm_parser_runner import run_gsm_parser

        _lock_db = DataManager()
        db_lock_acquired, lock_msg = _lock_db.try_acquire_gsm_parser_run_lock(db_lock_owner)
        if not db_lock_acquired:
            print(f"[{datetime.now():%H:%M:%S}] Пропущено — {lock_msg}", flush=True)
            return
        run_gsm_parser("__global__", "", "", mode or "full", True)
    except Exception as exc:
        msg = f"[{datetime.now():%H:%M:%S}] ПОМИЛКА: {exc}\n"
        print(msg, flush=True)
        with contextlib.suppress(Exception):
            _LOG_PATH.write_text(msg, encoding="utf-8")
    finally:
        if db_lock_acquired and _lock_db is not None:
            with contextlib.suppress(Exception):
                _lock_db.release_gsm_parser_run_lock(db_lock_owner)
        _parse_lock.release()


def _reschedule(time_str: str) -> None:
    if _scheduler is None:
        return
    _scheduler.remove_all_jobs()
    if not time_str or ":" not in time_str:
        return
    try:
        h, m = map(int, time_str.split(":"))
        _scheduler.add_job(_run_parse, "cron", hour=h, minute=m, id="parse_job")
        print(f"[Scheduler] Scheduled parse at {time_str:>5} (Kyiv time)", flush=True)
    except Exception as exc:
        print(f"[Scheduler] Failed to set schedule: {exc}", flush=True)


# Restore schedule from saved config on startup
with contextlib.suppress(Exception):
    if _cfg.get("schedule_time"):
        _reschedule(_cfg["schedule_time"])

# ── Models ────────────────────────────────────────────────────────────────────


class ConfigPayload(BaseModel):
    api_key: str = ""
    schedule_time: str = ""
    database_url: str = ""


class SchedulePayload(BaseModel):
    schedule_time: str


class TriggerPayload(BaseModel):
    mode: str = "full"


class ManualLinkPayload(BaseModel):
    brand: str
    device_type: str
    url: str


class ManualGsmLinkPayload(BaseModel):
    device_id: str
    url: str


class ManualGsmDevicePayload(BaseModel):
    brand: str
    device_type: str
    url: str


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/status")
def status():
    """Public — returns service health and current schedule."""
    return {
        "ok": True,
        "schedule": _cfg.get("schedule_time") or "вимкнено",
        "has_db": bool(_cfg.get("database_url")),
        "running": _parse_lock.locked(),
        "version": "1.0.0",
    }


@app.post("/configure")
def configure(data: ConfigPayload, x_api_key: str = Header("")):
    """Set full config: api_key, schedule_time, database_url.
    First call from Flet app after deploying on VPS."""
    _require_auth(x_api_key)
    for k, v in data.model_dump().items():
        if v:  # only update non-empty fields
            _cfg[k] = v
    _save_cfg(_cfg)
    _reschedule(_cfg.get("schedule_time", ""))
    return {"ok": True, "schedule": _cfg.get("schedule_time") or "вимкнено"}


@app.post("/schedule")
def set_schedule(data: SchedulePayload, x_api_key: str = Header("")):
    """Update only the schedule time (HH:MM or '' to disable)."""
    _require_auth(x_api_key)
    _cfg["schedule_time"] = data.schedule_time
    _save_cfg(_cfg)
    _reschedule(data.schedule_time)
    return {"ok": True, "schedule": data.schedule_time or "вимкнено"}


@app.post("/trigger")
def trigger(data: TriggerPayload = TriggerPayload(), x_api_key: str = Header("")):
    """Manually trigger a parse run (runs in background thread)."""
    _require_auth(x_api_key)
    if _parse_lock.locked():
        return {"ok": False, "message": "Парсинг вже виконується"}
    mode = str(data.mode or "full").strip() or "full"
    threading.Thread(target=_run_parse, args=(mode,), daemon=True).start()
    return {"ok": True, "message": f"Парсинг запущено у фоні (mode={mode})"}


@app.post("/fetch-manual-link")
def fetch_manual_link(data: ManualLinkPayload, x_api_key: str = Header("")):
    """Immediately fetch a single manually-pinned E-Katalog URL and store it
    as a standalone device — instant feedback for the "Посилання ЄК" dialog
    instead of waiting for the next scheduled/triggered full parse run.
    Fast (one page fetch), safe to call synchronously — unlike /trigger,
    which only ever starts the multi-hour full scrape in the background."""
    _require_auth(x_api_key)
    db_url = str(_cfg.get("database_url") or "").strip()
    if db_url:
        os.environ["DATABASE_URL"] = db_url
    try:
        from database import DataManager
        from gsm_parser_runner import fetch_and_add_manual_ek_device

        db = DataManager()
        ok, msg_or_name, dev_id, _ch, _ev = fetch_and_add_manual_ek_device(
            db, data.brand, data.device_type, data.url
        )
        return {"ok": ok, "message": msg_or_name, "device_id": dev_id}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


@app.post("/fetch-manual-gsm-link")
def fetch_manual_gsm_link(data: ManualGsmLinkPayload, x_api_key: str = Header("")):
    """Immediately fetch a single manually-pinned GSM Arena URL and attach it
    to an existing standalone E-Katalog device — instant feedback for the
    "Посилання GSM Arena" dialog instead of waiting for the next scheduled/
    triggered full parse run to discover the match on its own. Synchronous
    like /fetch-manual-link, but this one page fetch has its own built-in
    anti-bot pacing (~30-55s, see GSM_Parser/main.py's parse_device_details)
    so it runs noticeably slower than the E-Katalog equivalent."""
    _require_auth(x_api_key)
    db_url = str(_cfg.get("database_url") or "").strip()
    if db_url:
        os.environ["DATABASE_URL"] = db_url
    try:
        from database import DataManager
        from gsm_parser_runner import fetch_and_attach_manual_gsm_url

        db = DataManager()
        ok, msg_or_name, gsm_dev_id = fetch_and_attach_manual_gsm_url(
            db, data.device_id, data.url
        )
        return {"ok": ok, "message": msg_or_name, "device_id": gsm_dev_id}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


@app.post("/fetch-manual-gsm-device")
def fetch_manual_gsm_device(data: ManualGsmDevicePayload, x_api_key: str = Header("")):
    """Immediately fetch a manually-pinned GSM Arena URL for a device that
    has NO row on either side yet (unlike /fetch-manual-gsm-link, which
    attaches to an EXISTING standalone E-Katalog device) — instant feedback
    for the toolbar "Посилання GSM Arena" dialog, mirroring /fetch-manual-
    link's E-Katalog equivalent. Same anti-bot-pacing note as /fetch-manual-
    gsm-link — skipped for this one-off manual fetch."""
    _require_auth(x_api_key)
    db_url = str(_cfg.get("database_url") or "").strip()
    if db_url:
        os.environ["DATABASE_URL"] = db_url
    try:
        from database import DataManager
        from gsm_parser_runner import fetch_and_add_manual_gsm_device

        db = DataManager()
        ok, msg_or_name, gsm_dev_id = fetch_and_add_manual_gsm_device(
            db, data.brand, data.device_type, data.url
        )
        return {"ok": ok, "message": msg_or_name, "device_id": gsm_dev_id}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


@app.get("/logs")
def get_logs(x_api_key: str = Header("")):
    """Return the last parse log file content."""
    _require_auth(x_api_key)
    if not _LOG_PATH.exists():
        return {"ok": True, "content": "Лог відсутній. Запустіть парсинг."}
    return {"ok": True, "content": _LOG_PATH.read_text(encoding="utf-8")}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PARSER_SERVICE_PORT", "8765"))
    print(f"GSM Parser Service → http://0.0.0.0:{port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
