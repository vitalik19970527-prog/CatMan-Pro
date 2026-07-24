"""pg_dump/pg_restore subprocess wrappers for moving a company's business-data
database between Postgres instances. Used by:
- database.py: migrate_company_local_to_vps() — automatic Локал -> VPS upgrade.
- The manual Export/Import device-transfer feature (settings UI).

The bundled pg_dump.exe/pg_restore.exe ship in the same vendor/pgsql
distribution as the local Postgres server itself (see
local_pg_manager.py's pg_bin_dir()) — no separate download/tooling needed.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile

from local_pg_manager import LocalPgError, creation_flags, pg_bin_dir


class PgMigrationError(RuntimeError):
    pass


def _run_with_password(bin_dir, exe: str, args: list[str], password: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PGPASSWORD=password)
    return subprocess.run(
        [str(bin_dir / exe), *args],
        capture_output=True,
        text=True,
        env=env,
        creationflags=creation_flags(),
    )


def dump_database(
    *, host: str, port: int, user: str, password: str, dbname: str, output_file: str
) -> None:
    """pg_dump the given database in custom format (-Fc, required by
    pg_restore) to output_file."""
    try:
        bin_dir = pg_bin_dir()
    except LocalPgError as exc:
        raise PgMigrationError(str(exc)) from exc
    res = _run_with_password(
        bin_dir,
        "pg_dump.exe",
        ["-h", host, "-p", str(port), "-U", user, "-d", dbname, "-Fc", "-f", output_file],
        password,
    )
    if res.returncode != 0:
        raise PgMigrationError(f"pg_dump failed: {res.stderr or res.stdout}")


def dump_database_tables(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    dbname: str,
    tables: list[str],
    output_file: str,
) -> None:
    """Like dump_database, but restricted to the given table name/pattern
    list (-t, repeatable, supports psql-style wildcards e.g. 'gsm_parser_*').
    Used for superadmin filtered backups of the shared general database,
    where the full database is too large/mixed-purpose to dump whole."""
    try:
        bin_dir = pg_bin_dir()
    except LocalPgError as exc:
        raise PgMigrationError(str(exc)) from exc
    args = ["-h", host, "-p", str(port), "-U", user, "-d", dbname, "-Fc"]
    for t in tables:
        args += ["-t", t]
    args += ["-f", output_file]
    res = _run_with_password(bin_dir, "pg_dump.exe", args, password)
    if res.returncode != 0:
        raise PgMigrationError(f"pg_dump failed: {res.stderr or res.stdout}")


def restore_database(
    *, host: str, port: int, user: str, password: str, dbname: str, input_file: str
) -> None:
    """pg_restore a custom-format dump into dbname. --clean --if-exists drops
    existing objects (that the dump also contains) first, so this works both
    against a freshly-provisioned empty database (Локал->VPS upgrade) and an
    existing, already-populated one (restoring a backup over a broken
    account). --no-owner/--no-privileges avoid failures from role-ownership
    mismatches between the source (local, single 'postgres' role) and target
    (VPS, its own admin role) servers. --single-transaction makes a failed
    restore a clean no-op instead of a half-populated database."""
    try:
        bin_dir = pg_bin_dir()
    except LocalPgError as exc:
        raise PgMigrationError(str(exc)) from exc
    res = _run_with_password(
        bin_dir,
        "pg_restore.exe",
        [
            "-h", host, "-p", str(port), "-U", user, "-d", dbname,
            "--no-owner", "--no-privileges",
            "--clean", "--if-exists",
            "--exit-on-error", "--single-transaction",
            input_file,
        ],
        password,
    )
    if res.returncode != 0:
        raise PgMigrationError(f"pg_restore failed: {res.stderr or res.stdout}")


def dump_to_temp_file(*, host: str, port: int, user: str, password: str, dbname: str) -> str:
    """Convenience: dump into a fresh temp file, return its path. Caller owns
    cleanup (os.remove) once the migration/export is done."""
    fd, path = tempfile.mkstemp(suffix=".dump", prefix="catman_migrate_")
    os.close(fd)
    try:
        dump_database(host=host, port=port, user=user, password=password, dbname=dbname, output_file=path)
    except Exception:
        with contextlib.suppress(OSError):
            os.remove(path)
        raise
    return path
