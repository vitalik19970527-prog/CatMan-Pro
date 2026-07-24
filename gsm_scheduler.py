"""
GSM Parser local scheduler.
Runs as an asyncio background task inside the Flet app.
Every 30 s checks if a scheduled parse time has arrived.
If VPS service is configured → sends HTTP trigger; otherwise → runs locally in a thread.
"""
import asyncio
import contextlib
import threading
from datetime import datetime
from typing import Callable

_TASK: "asyncio.Task | None" = None
_LAST_RUN_DATE: str = ""  # "YYYY-MM-DD" of last triggered run


async def _check_loop(get_cfg: Callable) -> None:
    global _LAST_RUN_DATE
    while True:
        await asyncio.sleep(30)
        try:
            cfg = get_cfg()
            if not cfg.get("enabled"):
                continue
            run_times: list = cfg.get("run_times") or []
            if not run_times:
                continue
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            for ts in run_times:
                parts = str(ts or "").strip().split(":")
                if len(parts) < 2:
                    continue
                try:
                    h, m = int(parts[0]), int(parts[1])
                except ValueError:
                    continue
                if now.hour == h and now.minute == m and _LAST_RUN_DATE != today:
                    _LAST_RUN_DATE = today
                    _fire(cfg)
                    break
        except Exception:
            pass


def _fire(cfg: dict) -> None:
    """Trigger a parse run — VPS or local."""
    vps_url = str(cfg.get("vps_service_url") or "").strip().rstrip("/")
    api_key = str(cfg.get("vps_api_key") or "").strip()

    if vps_url:
        def _vps_trigger() -> None:
            try:
                import httpx
                from logger import logger
                headers = {"x-api-key": api_key} if api_key else {}
                r = httpx.post(f"{vps_url}/trigger", headers=headers, timeout=30)
                logger.info("[Scheduler] VPS trigger response: %s", r.json())
            except Exception as exc:
                from logger import logger
                logger.warning("[Scheduler] VPS trigger failed: %s", exc)

        threading.Thread(target=_vps_trigger, daemon=True).start()
    else:
        def _local_parse() -> None:
            try:
                from gsm_parser_runner import run_gsm_parser
                run_gsm_parser("__global__", "", "", "full", True)
            except Exception as exc:
                from logger import logger
                logger.exception("[Scheduler] Local parse failed: %s", exc)

        threading.Thread(target=_local_parse, daemon=True).start()


def start(get_cfg: Callable) -> None:
    """Start scheduler background task. Safe to call multiple times (no-op if already running)."""
    global _TASK
    if _TASK and not _TASK.done():
        return
    with contextlib.suppress(Exception):
        _TASK = asyncio.get_event_loop().create_task(_check_loop(get_cfg))


def stop() -> None:
    """Cancel the scheduler task."""
    global _TASK
    with contextlib.suppress(Exception):
        if _TASK:
            _TASK.cancel()
    _TASK = None
