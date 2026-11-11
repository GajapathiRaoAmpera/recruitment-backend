import os

# Dummy config so tests never reach the real Supabase; real env vars still win if set.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("SUPABASE_URL", "https://test-project.supabase.co")
