# recruitment-backend

**Purpose:** The single Python REST API behind Ampera Campus Hire: RBAC, drives/rounds, resume parsing (no AI), tests & scoring, Judge0 code runs, Gemini interviews, emails, audit. Specs live in `../docs/` (requirements, architecture, schema, API contracts, plan).

## Map
```
app/main.py            FastAPI app, routers under /api/v1
app/core/              settings, db, security, error handler
app/<feature>/         router.py → service.py → repo.py, schemas.py
  auth/ drives/ candidates/ tests/ execution/ interviews/ integrity/ notifications/ admin/
migrations/            Alembic
tests/                 pytest (mirrors app/)
```
Danger zones have their own CLAUDE.md: `app/auth/`, `app/execution/`, `app/integrity/`, `migrations/`.

## Commands
```bash
uv sync                                  # install
uv run fastapi dev app/main.py           # run on :8000, docs at /docs
uv run pytest -q                         # tests
uv run ruff check . && uv run ruff format --check .
uv run alembic revision -m "msg"         # new migration (never edit applied ones)
uv run alembic upgrade head
```

## Rules
- Stack is fixed: FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, Python 3.12. Ask before adding a dependency.
- Every route declares a role dependency (`require_role(...)`); HR queries filter by drive ownership.
- Every HR/Admin mutation writes `audit_logs` in the same transaction.
- Validate all AI output with a Pydantic schema before saving; mock Gemini/Judge0/email in unit tests.
- Error shape: `{"error": {"code", "message", "details"}}`.
- Skills: `add-endpoint`, `new-migration` (this repo); `postgres` for read-only DB inspection; `openai-docs` only if an OpenAI provider is added.
- `.env` is blocked by hook; add new keys to `.env.example`.
