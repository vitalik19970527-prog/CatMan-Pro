"""
One-time migration: move per-company tables from public schema
to a dedicated company_<id> schema.

Run ONCE with:
    python migrate_company_schemas.py [--dry-run]

After running:
  - public.orders           -> company_<id>.orders
  - public.managers stays   -> public.managers  (global)
  - public.companies stays  -> public.companies (global)
  - Views in public.* are created for backward-compat (read+write)
    until database.py is updated to use search_path.

Safety:
  - Creates schema only if not exists.
  - ALTER TABLE ... SET SCHEMA is atomic.
  - Dry-run flag shows what would happen without changing anything.
"""

import sys

import psycopg
from psycopg.rows import dict_row

DSN = "postgresql://postgres:2705@127.0.0.1:5432/catman"

# Tables that STAY in public (global / cross-company)
GLOBAL_TABLES = {
    "companies",
    "managers",
    "sync_pulses",
    "app_update_policy",
    "gsm_parser_devices",
    "gsm_parser_change_history",
}


def schema_name(company_id: str) -> str:
    return f"company_{company_id}"


def get_existing_tables(cur, schema: str) -> set[str]:
    cur.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        """,
        (schema,),
    )
    return {r["table_name"] for r in cur.fetchall()}


def get_existing_views(cur, schema: str) -> set[str]:
    cur.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'VIEW'
        """,
        (schema,),
    )
    return {r["table_name"] for r in cur.fetchall()}


def migrate_company(cur, company_id: str, dry_run: bool) -> None:
    sc = schema_name(company_id)
    print(f"\n{'[DRY RUN] ' if dry_run else ''}Migrating company {company_id} -> schema {sc}")

    # 1. Create schema
    if dry_run:
        print(f"  WOULD: CREATE SCHEMA IF NOT EXISTS {sc}")
    else:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {sc}")
        print(f"  OK: schema {sc} ensured")

    public_tables = get_existing_tables(cur, "public")
    company_tables_already_in_schema = get_existing_tables(cur, sc)

    tables_to_move = sorted(public_tables - GLOBAL_TABLES)

    print(f"\n  Tables to move ({len(tables_to_move)}):")
    for tbl in tables_to_move:
        already_there = tbl in company_tables_already_in_schema
        status = "ALREADY IN SCHEMA" if already_there else "will move"
        print(f"    {tbl}: {status}")

    if dry_run:
        return

    # 2. Move tables
    for tbl in tables_to_move:
        if tbl in company_tables_already_in_schema:
            print(f"  SKIP: {tbl} already in {sc}")
            continue
        try:
            cur.execute(f"ALTER TABLE public.{tbl} SET SCHEMA {sc}")
            print(f"  MOVED: public.{tbl} -> {sc}.{tbl}")
        except Exception as e:
            print(f"  ERROR moving {tbl}: {e}")

    # 3. Create backward-compat views in public (so old queries still work)
    # Simple views on single tables support INSERT/UPDATE/DELETE automatically.
    company_tables_now = get_existing_tables(cur, sc)
    existing_views = get_existing_views(cur, "public")

    for tbl in sorted(company_tables_now):
        if tbl in GLOBAL_TABLES:
            continue
        if tbl in existing_views:
            print(f"    SKIP view: public.{tbl} already exists")
            continue
        try:
            cur.execute(
                f"CREATE OR REPLACE VIEW public.{tbl} AS SELECT * FROM {sc}.{tbl}"
            )
            print(f"    VIEW: public.{tbl} -> {sc}.{tbl}")
        except Exception as e:
            print(f"    ERROR creating view for {tbl}: {e}")

    print(f"\n  Migration complete for company {company_id}")


def verify(cur) -> None:
    print("\n=== VERIFICATION ===")
    cur.execute(
        """
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast', 'public')
        ORDER BY schema_name
        """
    )
    schemas = [r["schema_name"] for r in cur.fetchall()]
    print(f"Company schemas: {schemas}")

    for sc in schemas:
        tables = get_existing_tables(cur, sc)
        print(f"\n  {sc}: {len(tables)} tables")
        for tbl in sorted(tables):
            cur.execute(f"SELECT COUNT(*) as cnt FROM {sc}.{tbl}")
            cnt = cur.fetchone()["cnt"]
            print(f"    {tbl}: {cnt} rows")

    print("\n  Public views (backward-compat):")
    views = get_existing_views(cur, "public")
    for v in sorted(views):
        print(f"    public.{v}")

    print("\n  Public tables (global):")
    ptables = get_existing_tables(cur, "public")
    for t in sorted(ptables):
        print(f"    public.{t}")


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("DRY RUN MODE - no changes will be made")

    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    cur = conn.cursor()

    # Get all companies
    cur.execute("SELECT id, name FROM public.companies ORDER BY created_at")
    companies = cur.fetchall()
    print(f"Found {len(companies)} company(ies):")
    for c in companies:
        print(f"  {c['id']} - {c['name']}")

    if not companies:
        print("No companies found. Exiting.")
        conn.close()
        return

    for company in companies:
        migrate_company(cur, company["id"], dry_run)

    if not dry_run:
        verify(cur)

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
