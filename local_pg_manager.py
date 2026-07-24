"""Local (per-company) Postgres cluster lifecycle management for "Локал" storage mode.

Each company/individual account with storage_mode='local' gets its OWN
isolated Postgres cluster: a dedicated data directory, its own randomly
picked 127.0.0.1-only port, started/stopped as a background process by this
module. This mirrors the "own dedicated database" isolation already used for
VPS companies (see database.py's create_company/_ensure_company_schema) —
here it's a whole own cluster instead of a database on a shared server,
because there IS no shared local server to provision a database on.

The bundled Postgres binaries ship with the installer (see
scripts/CatManPro.installer.iss + the PyInstaller spec, which stage a
`pgsql/` directory containing bin/lib/share next to the app). At runtime we
resolve their location via pg_bin_dir(); a dev checkout without a bundled
copy falls back to CATMAN_LOCAL_PG_BIN or a full local Postgres install for
local testing only — production builds must resolve via the bundled path.

database.py never calls postgres.exe directly for local mode; it only reads
back (host, port, db_name, user) from ensure_started() and stores them in
companies.db_host/db_port/db_name/db_user exactly like the VPS path — so
_resolve_company_dsn()/_pg()/_ensure_company_schema() need zero changes.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

try:
    import psycopg
except ImportError:
    # psycopg is a C extension with no Android wheel; mobile has no bundled
    # local Postgres cluster to manage anyway (see module docstring), but
    # main.py imports this module unconditionally, so it still needs to
    # import cleanly there. See psycopg_android_shim.py / database.py.
    import psycopg_android_shim as psycopg

logger = logging.getLogger(__name__)


class LocalPgError(RuntimeError):
    pass


def _app_data_root() -> Path:
    base = os.getenv("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "CatManPro"


def _pgdata_dir(company_id: str) -> Path:
    return _app_data_root() / "pgdata" / company_id


def pg_bin_dir() -> Path:
    """Resolve the directory containing postgres.exe/initdb.exe/pg_ctl.exe/
    pg_dump.exe/pg_restore.exe.

    Resolution order: packaged app (PyInstaller `_MEIPASS/pgsql/bin`, staged
    by CatManPro.spec from vendor/pgsql) -> CATMAN_LOCAL_PG_BIN override ->
    the repo's own `vendor/pgsql/bin` (dev checkout — same trimmed
    PostgreSQL 18.3 x64 bin/lib/share bundle that gets shipped in real
    builds; postgres.exe/initdb.exe/pg_ctl.exe/pg_dump.exe/pg_restore.exe +
    their runtime DLLs, with the pgAdmin/StackBuilder-only wx*/stackbuilder
    files stripped out — ICU DLLs are a hard load-time dependency of
    postgres.exe even when --locale=C is used, so they stay in).
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "pgsql" / "bin"
        if (candidate / "postgres.exe").exists():
            return candidate
    env_override = os.getenv("CATMAN_LOCAL_PG_BIN")
    if env_override and (Path(env_override) / "postgres.exe").exists():
        return Path(env_override)
    repo_vendor = Path(__file__).resolve().parent / "vendor" / "pgsql" / "bin"
    if (repo_vendor / "postgres.exe").exists():
        return repo_vendor
    raise LocalPgError(
        "Bundled Postgres binaries not found. Set CATMAN_LOCAL_PG_BIN to a "
        "directory containing postgres.exe/initdb.exe/pg_ctl.exe, or bundle "
        "pgsql/bin with the installer."
    )


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _run(
    bin_dir: Path, exe: str, args: list[str], capture: bool = True
) -> subprocess.CompletedProcess:
    """Run a Postgres binary and wait for it to exit.

    capture=False MUST be used for `pg_ctl ... start`: pg_ctl spawns postgres.exe
    as a long-running grandchild that on Windows inherits our pipe handles, so
    a captured PIPE never sees EOF (postgres keeps the write end open forever)
    and subprocess.run() hangs indefinitely even though the server started
    fine. DEVNULL sidesteps this entirely; postgres.exe's own output already
    goes to the `-l <logfile>` we pass, so nothing is lost.
    """
    stdio = subprocess.PIPE if capture else subprocess.DEVNULL
    return subprocess.run(
        [str(bin_dir / exe), *args],
        stdout=stdio,
        stderr=stdio,
        text=True,
        creationflags=creation_flags(),
    )


def _is_initialized(data_dir: Path) -> bool:
    return (data_dir / "PG_VERSION").exists()


def _port_file(data_dir: Path) -> Path:
    return data_dir / "catman_port.txt"


def _read_port(data_dir: Path) -> int | None:
    pf = _port_file(data_dir)
    if pf.exists():
        with contextlib.suppress(ValueError):
            return int(pf.read_text().strip())
    return None


def _probe_alive(
    host: str, port: int, user: str, password: str, dbname: str, timeout: float = 2.0
) -> bool:
    try:
        conn = psycopg.connect(
            f"postgresql://{user}:{password}@{host}:{port}/{dbname}",
            connect_timeout=timeout,
        )
        conn.close()
        return True
    except Exception:
        return False


def ensure_started(company_id: str, password: str) -> tuple[str, int, str, str]:
    """Ensure an isolated local Postgres cluster is initialized and running
    for this company_id. Returns (host, port, db_name, user).

    `password` is the plaintext superuser password to set on first
    initialization. The caller (database.py: create_company) is responsible
    for Fernet-encrypting it into companies.db_password_enc — this module
    never persists the plaintext password itself.
    """
    company_id = str(company_id or "").strip()
    if not company_id:
        raise LocalPgError("company_id is required")
    bin_dir = pg_bin_dir()
    data_dir = _pgdata_dir(company_id)
    user = "postgres"
    db_name = "postgres"

    if not _is_initialized(data_dir):
        data_dir.parent.mkdir(parents=True, exist_ok=True)
        pwfile = data_dir.parent / f".initpw_{company_id}.tmp"
        pwfile.write_text(password, encoding="utf-8")
        try:
            res = _run(
                bin_dir,
                "initdb.exe",
                [
                    "-D",
                    str(data_dir),
                    "-U",
                    user,
                    "--pwfile",
                    str(pwfile),
                    "-E",
                    "UTF8",
                    "--locale=C",
                    # Without an explicit --auth, initdb defaults pg_hba.conf's
                    # 127.0.0.1/::1 records to "trust" (no password needed at
                    # all) — any local process on the machine could then
                    # connect to this company's data. Must be explicit.
                    "--auth-host=scram-sha-256",
                    "--auth-local=scram-sha-256",
                ],
            )
            if res.returncode != 0:
                raise LocalPgError(f"initdb failed: {res.stderr or res.stdout}")
        finally:
            with contextlib.suppress(OSError):
                pwfile.unlink()
        port = _free_local_port()
        _port_file(data_dir).write_text(str(port), encoding="utf-8")
    else:
        port = _read_port(data_dir) or _free_local_port()
        _port_file(data_dir).write_text(str(port), encoding="utf-8")

    if not _probe_alive("127.0.0.1", port, user, password, db_name):
        res = _run(
            bin_dir,
            "pg_ctl.exe",
            [
                "-D",
                str(data_dir),
                "-l",
                str(data_dir / "server.log"),
                "-o",
                f"-p {port} -h 127.0.0.1",
                "start",
            ],
            capture=False,
        )
        if res.returncode != 0:
            log_tail = ""
            with contextlib.suppress(OSError):
                log_tail = (data_dir / "server.log").read_text(encoding="utf-8", errors="replace")[-2000:]
            raise LocalPgError(f"pg_ctl start failed (exit {res.returncode}): {log_tail}")
        for _ in range(30):
            if _probe_alive("127.0.0.1", port, user, password, db_name):
                break
            time.sleep(0.3)
        else:
            raise LocalPgError("local Postgres did not become ready in time")

    return "127.0.0.1", port, db_name, user


def is_running(company_id: str, port: int, user: str, password: str) -> bool:
    return _probe_alive("127.0.0.1", port, user, password, "postgres")


def stop(company_id: str) -> None:
    """Stop the local cluster for one company (no-op if not initialized/running)."""
    company_id = str(company_id or "").strip()
    if not company_id:
        return
    data_dir = _pgdata_dir(company_id)
    if not _is_initialized(data_dir):
        return
    try:
        bin_dir = pg_bin_dir()
    except LocalPgError:
        return
    with contextlib.suppress(Exception):
        _run(bin_dir, "pg_ctl.exe", ["-D", str(data_dir), "-m", "fast", "stop"])


def stop_all() -> None:
    """Stop every local cluster on this machine — called on clean app exit so
    a non-technical user never ends up with an orphaned Postgres process."""
    root = _app_data_root() / "pgdata"
    if not root.exists():
        return
    for entry in root.iterdir():
        if entry.is_dir():
            with contextlib.suppress(Exception):
                stop(entry.name)
