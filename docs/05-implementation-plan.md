# 05 — Implementation Plan

Phases follow the PDF timeline. Each task lists the FR/AC it closes; a task is done when its AC passes in CI, not when the code compiles. Owner column is filled in by the team (6 members, split TBD).

Suggested split once decided: **A** Auth+Admin, **B** Intake+Parser, **C** Round 1, **D** Coding+Judge0, **E** AI interview, **F** Frontend shell + HR dashboards. Frontend work for a feature belongs to the same owner as its backend.

## Phase 0 — Foundation (week 1)
| # | Task | Repo | Closes | Owner |
|---|---|---|---|---|
| 0.1 | `uv init`, FastAPI skeleton, `/healthz`, settings from env, error handler, pytest + ruff | backend | — | |
| 0.2 | Alembic + migration `0001_init` from `03-database-schema.md` | backend | — | |
| 0.3 | Next.js + TypeScript app, SCSS with Stitch tokens, role-guarded layouts, API client | frontend | — | |
| 0.4 | `.env.example` both repos, README setup, CI (lint + test) on push | both | — | |

## Phase 1 — Auth & roles
| 1.1 | Supabase JWT verify, `users` lookup, `require_role()` dependency, `/me` | backend | AC-1 | |
| 1.2 | Login screen, session handling, HR/Admin layouts | frontend | AC-1 | |
| 1.3 | Invite token (signed, per candidate+round), `/entry/*`, attempt token | backend | FR-5,6 AC-3 | |
| 1.4 | System-check screen: ID/DOB, location, terms, full screen | frontend | AC-3 | |
| 1.5 | `audit_logs` helper used by every HR mutation | backend | AC-10 | |

## Phase 2 — Drives, intake & shortlisting
| 2.1 | Drives + rounds CRUD, pipeline endpoint | backend | FR-1,21 | |
| 2.2 | Wizard + pipeline screens | frontend | FR-1 | |
| 2.3 | Excel/CSV import with per-row errors (openpyxl/csv) | backend | FR-2 | |
| 2.4 | Resume upload to Storage; parser (pdfplumber → Tesseract fallback), skills dictionary, CGPA regex, JD-match = skill overlap % | backend | FR-3 AC-2 | |
| 2.5 | Collect 20 sample resumes + expected values as test fixtures | backend | AC-2 | |
| 2.6 | Candidate table with filters + shortlist | frontend | FR-4 | |

## Phase 3 — Round 1
| 3.1 | Question CRUD, tags, approval; Gemini generation with schema validation | backend | FR-9,10 | |
| 3.2 | Question bank + AI generator screen | frontend | FR-9 | |
| 3.3 | Test config, attempt start/autosave/submit, server deadline, auto-expire | backend | FR-8 AC-4 | |
| 3.4 | Scoring, ranking, re-score on key change | backend | FR-10,11 AC-4 | |
| 3.5 | Aptitude test screen with local draft + server sync | frontend | FR-17 AC-6 | |
| 3.6 | `/rounds/{id}/advance` + EmailSender + email templates | backend | FR-12,26 | |
| 3.7 | Round 1 paper builder (`/papers/*`: LLM-generated sample paper, HR edit, accept, send; plan of record `07-round1-paper-builder-implementation-plan.md`) | both | FR-9,10 | |

## Phase 4 — Coding round
| 4.1 | Judge0 self-host (docker compose) + client with timeouts | backend | FR-15 | |
| 4.2 | Run (samples) / submit (18) endpoints; hidden cases never serialized | backend | FR-16 AC-5 | |
| 4.3 | Reference solutions in 6 languages as integration test | backend | AC-5 | |
| 4.4 | Coding screen: Monaco, language switch, per-question drafts | frontend | FR-17 AC-6 | |

## Phase 5 — AI interview
| 5.1 | Ephemeral Gemini Live token endpoint; prompts from `ai_prompts` | backend | FR-19 | |
| 5.2 | Interview screen: mic, live audio, transcript capture, thank-you end | frontend | FR-19 | |
| 5.3 | Transcript storage, evaluation with rubric schema, HR notes | backend | FR-20 AC-8 | |
| 5.4 | Interviewer console / interview details screens | frontend | FR-20 | |

## Phase 6 — Integrity & dashboards
| 6.1 | Violations endpoint, lock at 3, alerts, HR resume | backend | FR-24,25 AC-7 | |
| 6.2 | `useIntegrity` hook: fullscreen, visibilitychange/blur → API, lock screen | frontend | AC-7 | |
| 6.3 | Per-test dashboard endpoint + screen, proctoring report | both | FR-23 | |
| 6.4 | Final ranking & decision screen; add/skip round UI | both | FR-21,22 AC-9 | |
| 6.5 | Super Admin: users, prompts, settings, audit log screens | both | FR-27 | |

## Phase 7 — Hardening & demo
| 7.1 | Load test: 300 students autosaving every 10 s + 50 concurrent submits (locust) | backend | NFR perf | |
| 7.2 | Playwright E2E: student happy path, tab-switch lock + resume, HR advance | frontend | AC-3..9 | |
| 7.3 | Deploy (hosting per open decision), seed demo drive, final report & demo | both | — | |

## Dependencies
0 → 1 → 2 → 3 → (4, 5 in parallel) → 6 → 7. Frontend tasks need the matching backend contract merged (or mocked from `04-api-contracts.md`).

## Biggest risks & mitigation
| Risk | Mitigation |
|---|---|
| Gemini free-tier limits during a batch | Schedule interviews in slots; show retry; manual fallback mode per round |
| Judge0 capacity | Self-host, queue submissions server-side, cap concurrent per candidate to 1 |
| Network loss looks like tab switch | Lock is reversible by HR; violations show timestamps |
| OCR accuracy | HR can edit extracted CGPA/skills; fixtures measure accuracy |
