# migrations — danger zone
- Applied migrations are immutable (hook blocks edits to existing files in migrations/versions/). Fix forward with a new revision.
- Every migration: has a working downgrade, enables RLS on new tables, and is mirrored in ../docs/03-database-schema.md in the same change.
- Never run `alembic downgrade` or destructive SQL against the shared Supabase DB without the user's explicit OK.
- Use the `new-migration` skill.
