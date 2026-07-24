"""Generate 001_schema_from_models.sql from backend.app.models (loaded from .pyc)."""
from __future__ import annotations

import importlib.util
import sys
import types as types_t
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

ROOT = Path(__file__).resolve().parents[2]
BACKEND_APP = ROOT / "backend" / "app"


def _load_models():
    sys.path.insert(0, str(ROOT))
    for name in ("backend", "backend.app"):
        m = types_t.ModuleType(name)
        m.__path__ = [str(BACKEND_APP) if "app" in name else str(ROOT / "backend")]
        sys.modules[name] = m
    for mod, relpy in [
        ("backend.app.settings", "settings.cpython-314.pyc"),
        ("backend.app.db", "db.cpython-314.pyc"),
        ("backend.app.models", "models.cpython-314.pyc"),
    ]:
        path = BACKEND_APP / "__pycache__" / relpy
        spec = importlib.util.spec_from_file_location(mod, str(path))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Cannot load {path}")
        m = importlib.util.module_from_spec(spec)
        sys.modules[mod] = m
        spec.loader.exec_module(m)  # type: ignore[union-attr]
    import backend.app.models as M  # noqa: WPS433

    return M


def main() -> None:
    M = _load_models()
    dialect = postgresql.dialect()
    table_sql: list[str] = []
    for table in M.Base.metadata.sorted_tables:
        table_sql.append(str(CreateTable(table).compile(dialect=dialect)) + ";")
    index_sql: list[str] = []
    for table in M.Base.metadata.sorted_tables:
        for ix in table.indexes:
            index_sql.append(str(CreateIndex(ix).compile(dialect=dialect)) + ";")

    optional = """
-- Опційно (після очищення '' у посиланнях): зв'язок workspace з managers
-- ALTER TABLE manager_workspace_state
--   ADD CONSTRAINT fk_manager_workspace_state_manager
--   FOREIGN KEY (manager_id) REFERENCES managers(id) ON DELETE CASCADE;
""".rstrip()

    out = f"""-- CatMan Pro — схема PostgreSQL, згенерована з SQLAlchemy (backend.app.models).
--   python scripts/postgres/generate_schema.py
--
-- Приклад:
--   createdb -U postgres catman
--   psql -U postgres -d catman -f scripts/postgres/001_schema_from_models.sql

BEGIN;

{chr(10).join(table_sql)}

{chr(10).join(index_sql)}

{optional}

COMMIT;
"""
    out_path = Path(__file__).with_name("001_schema_from_models.sql")
    out_path.write_text(out, encoding="utf-8")
    print("Wrote", out_path, "bytes", len(out.encode("utf-8")))


if __name__ == "__main__":
    main()
