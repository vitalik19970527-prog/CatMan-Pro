"""
One-time script: rename company schema from ID-only to name+ID format.

Old: company_d39953647c894662bef4a441b5851bb3
New: company_itsellopt_d39953647c894662bef4a441b5851bb3

Also drops old public views and recreates them pointing to the new schema name.

Run: python rename_company_schema.py [--dry-run]
"""

import re
import sys

import psycopg
from psycopg.rows import dict_row

DSN = "postgresql://postgres:2705@127.0.0.1:5432/catman"

GLOBAL_TABLES = {
    "companies",
    "managers",
    "sync_pulses",
    "app_update_policy",
    "gsm_parser_devices",
    "gsm_parser_change_history",
}


def slugify(name: str) -> str:
    ascii_name = re.sub(r"[^\x00-\x7F]", "", name)
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")
    return slug


def company_schema_name(company_id: str, company_name: str) -> str:
    cid = company_id.replace("-", "_")
    if company_name:
        slug = slugify(company_name)
        max_slug = 63 - 8 - 1 - len(cid)
        slug = slug[: max(max_slug, 0)]
        if slug:
            return f"company_{slug}_{cid}"
    return f"company_{cid}"


def get_schemas(cur) -> list[str]:
    cur.execute("""
        SELECT schema_name FROM information_schema.schemata
        WHERE schema_name NOT IN ('pg_catalog','information_schema','pg_toast','public')
        ORDER BY schema_name
    """)
    return [r["schema_name"] for r in cur.fetchall()]


def get_tables(cur, schema: str) -> list[str]:
    cur.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """,
        (schema,),
    )
    return [r["table_name"] for r in cur.fetchall()]


def get_views(cur, schema: str) -> list[str]:
    cur.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = %s AND table_type = 'VIEW'
        ORDER BY table_name
    """,
        (schema,),
    )
    return [r["table_name"] for r in cur.fetchall()]


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("=== DRY RUN — no changes ===\n")

    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    cur = conn.cursor()

    cur.execute("SELECT id, name FROM public.companies ORDER BY created_at")
    companies = cur.fetchall()

    existing_schemas = get_schemas(cur)
    print(f"Existing company schemas: {existing_schemas}\n")

    for company in companies:
        cid = company["id"]
        cname = company["name"] or ""
        old_schema = f"company_{cid.replace('-','_')}"
        new_schema = company_schema_name(cid, cname)

        print(f"Company: {cname!r} ({cid[:12]}...)")
        print(f"  Old schema: {old_schema}")
        print(f"  New schema: {new_schema}")

        if old_schema == new_schema:
            print("  Already correct name — skipping.\n")
            continue

        if old_schema not in existing_schemas:
            if new_schema in existing_schemas:
                print("  New schema already exists — skipping rename.\n")
            else:
                print(f"  WARNING: old schema {old_schema!r} not found!\n")
            continue

        if dry_run:
            print(f"  WOULD: ALTER SCHEMA {old_schema} RENAME TO {new_schema}")
            print(
                f"  WOULD: drop {len(get_views(cur,'public'))} public views and recreate\n"
            )
            continue

        # Step 1: drop old public views (they reference old schema name)
        old_views = get_views(cur, "public")
        print(f"  Dropping {len(old_views)} old public views...")
        for v in old_views:
            try:
                cur.execute(f"DROP VIEW IF EXISTS public.{v} CASCADE")
            except Exception as e:
                print(f"    WARN: drop view public.{v}: {e}")

        # Step 2: rename schema
        print(f"  Renaming schema: {old_schema} -> {new_schema}")
        cur.execute(f"ALTER SCHEMA {old_schema} RENAME TO {new_schema}")

        # Step 3: recreate public views pointing to new schema
        tables = get_tables(cur, new_schema)
        print(f"  Recreating {len(tables)} public views -> {new_schema}.*")
        for tbl in tables:
            if tbl in GLOBAL_TABLES:
                continue
            try:
                cur.execute(
                    f"CREATE OR REPLACE VIEW public.{tbl} AS SELECT * FROM {new_schema}.{tbl}"
                )
            except Exception as e:
                print(f"    WARN: create view public.{tbl}: {e}")

        print(f"  Done: {new_schema}\n")

    # Verify
    print("=== Final schemas ===")
    for sc in get_schemas(cur):
        tbls = get_tables(cur, sc)
        print(f"  {sc}: {len(tbls)} tables")

    print("\n=== Public views ===")
    for v in get_views(cur, "public"):
        print(f"  public.{v}")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
