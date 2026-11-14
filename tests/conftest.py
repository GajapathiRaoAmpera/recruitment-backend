import os

# Dummy config for tests; real env vars still win if set.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-" + "0123456789abcdef" * 4)
