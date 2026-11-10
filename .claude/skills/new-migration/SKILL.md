---
name: new-migration
description: Create a database schema change for recruitment-backend (Alembic on Supabase Postgres). Use for any new table, column, index, enum value or constraint.
---

# New migration

1. Never edit an existing file in `migrations/versions/` (hook blocks it). Fix forward.
2. `uv run alembic revision -m "<verb_noun>"`, then write `op.execute("""SQL""")` for upgrade and a working downgrade.
3. New table → `alter table x enable row level security;` (no policies; API uses service connection).
4. Enum value add → `alter type ... add value` (cannot run in a transaction block on old PG; note it).
5. Update `../docs/03-database-schema.md` in the same change, and the invariants list if behaviour changes.
6. Apply only to a local/dev DB: `uv run alembic upgrade head`. Ask the user before touching the shared Supabase DB.
7. Verify with the `postgres` skill (read-only): check the table/columns exist.
