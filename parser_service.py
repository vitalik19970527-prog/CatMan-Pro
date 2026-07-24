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


def _run_parse() -> None:
    """Execute the parser. Called by scheduler or /trigger endpoint."""
    if not _parse_lock.acquire(blocking=False):
        print(f"[{datetime.now():%H:%M:%S}] Parser already running — skipped", flush=True)
        return
    try:
        db_url = str(_cfg.get("database_url") or "").strip()
        if db_url:
            os.environ["DATABASE_URL"] = db_url
        from gsm_parser_runner import run_gsm_parser
        run_gsm_parser("__global__", "", "", "full", True)
    except Exception as exc:
        msg = f"[{datetime.now():%H:%M:%S}] ПОМИЛКА: {exc}\n"
        print(msg, flush=True)
        with contextlib.suppress(Exception):
            _LOG_PATH.write_text(msg, encoding="utf-8")
    finally:
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
def trigger(x_api_key: str = Header("")):
    """Manually trigger a parse run (runs in background thread)."""
    _require_auth(x_api_key)
    if _parse_lock.locked():
        return {"ok": False, "message": "Парсинг вже виконується"}
    threading.Thread(target=_run_parse, daemon=True).start()
    return {"ok": True, "message": "Парсинг запущено у фоні"}


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
