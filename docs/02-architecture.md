# 02 — Architecture

## Shape
```
Next.js app (React, TypeScript, SCSS)     ── Student / HR / Super Admin portals, one app, role-routed
        │  HTTPS JSON (Bearer JWT)
        ▼
Python API (FastAPI)                      ── all business logic, RBAC, scoring, emails
        │
  ┌─────┼──────────┬──────────────┬─────────────┬───────────┐
Supabase        Judge0        Gemini API      Resume parser   Email
Postgres/Auth/  code exec     (questions,     pdfplumber +    console | Resend/SMTP
Storage                       Live interview) Tesseract fallback
```
The browser never talks to Supabase DB or Judge0 directly. Exception: the Gemini Live audio stream, which uses a **short-lived ephemeral token** minted by the API (API key stays server-side).

## Backend layout (`recruitment-backend/`)
```
app/
  main.py            FastAPI app, routers mounted under /api/v1
  core/              config (pydantic-settings), db session, security (JWT), errors
  auth/              HR/Admin Supabase JWT verify; student entry token issue/verify   ← danger zone
  drives/            drives, rounds (pipeline), round_candidates
  candidates/        import (Excel/CSV), resumes, parser, shortlist filters
  tests/             tests, questions, AI generation, attempts, answers, scoring, ranking
  execution/         Judge0 client, run/submit                                      ← danger zone
  interviews/        Gemini Live token, transcript, evaluation
  integrity/         verification_logs, violations, resume access                  ← danger zone
  notifications/     EmailSender, templates, email_logs
  admin/             users, ai_prompts, ai settings, audit_logs
migrations/          Alembic (SQL lives here; never edit an applied migration)      ← danger zone
tests/               pytest
```
Each feature folder = `router.py` (HTTP) → `service.py` (logic) → `repo.py` (SQL) + `schemas.py` (Pydantic).

## Frontend layout (`recruitment-frontend/`)
```
src/
  app/          Next.js App Router: (auth)/{student,hr,admin}/login, (staff)/hr, (staff)/admin, (student)/student, exam/[roundId]
  components/   shared UI built from Stitch tokens (ui/, shell/, hire/, student/, brand/, auth/)
  features/     test-runner/ (danger zone: full screen, tab-switch reporting, local drafts)
  lib/          supabase client, api fetch client, helpers
  mock/         in-memory prototype data; replaced screen by screen as endpoints land
  styles/       SCSS tokens + mixins copied from Stitch DESIGN.md
stitch_.../     design mockups — reference only, not shipped
```
The browser calls `/api/v1/*` on its own origin; Next rewrites it to FastAPI (no CORS needed).

## Key decisions (ADR-lite)
| # | Decision | Why |
|---|---|---|
| D1 | FastAPI + Pydantic v2 + SQLAlchemy 2 (async, asyncpg) + Alembic, Python 3.12, `uv` | Typed contracts → OpenAPI for free; team knows Python |
| D2 | Modular monolith, one API process; background work via FastAPI `BackgroundTasks` | Smallest thing that works for 300 users. Upgrade to a queue (arq/RQ) if email/OCR batches exceed ~30 s |
| D3 | HR/Admin auth = Supabase Auth; API verifies Supabase JWT and loads role from `users` | No password handling in our code |
| D4 | Student auth = signed invite token in email link + ID/DOB → API issues attempt-scoped JWT (2 h) | Students have no accounts per spec |
| D5 | Server owns the deadline: `deadline_at = started_at + duration`; client timer is display only | Refresh/offline cannot add time |
| D6 | Tab switches reported to API; server counts and closes at 3 | Client cannot be trusted to self-close |
| D7 | Hidden test cases and answer keys are never serialized to student endpoints | Integrity |
| D8 | RLS enabled on every table with no anon/authenticated policies; API uses service connection | Defence in depth if the anon key leaks |
| D9 | Judge0 self-hosted (Judge0 CE). SQL runs via its SQLite language | Free tier of hosted Judge0 too small for a batch |
| D10 | All AI output (questions, scores) validated against a Pydantic schema before storage; questions need HR approval | Model output is untrusted |
| D11 | Frontend: Next.js (App Router) + React 19 + TypeScript, SCSS modules with Stitch tokens, Monaco editor. HR/Admin sign in with supabase-js in the browser (publishable key only) | Built this way already; TypeScript compiles to JavaScript, so the spec's "React + JS" constraint holds — confirm with the mentor |

## Cross-cutting rules
- Errors: `{"error": {"code": "STRING_CODE", "message": "human text", "details": {}}}`.
- Every HR/Admin mutation writes `audit_logs` in the same transaction.
- Times stored UTC (`timestamptz`); UI shows local time.
- Pagination: `?limit=50&cursor=<opaque>` → `{items, next_cursor}`.
