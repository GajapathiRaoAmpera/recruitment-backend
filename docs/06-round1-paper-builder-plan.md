# 06 — Round 1 Paper Builder: summary

Status: **backend implemented, migration `0002_paper_builder` written but not applied**. Branch: `feature/round1-paper-builder` (both repos, uncommitted).
**Plan of record: `07-round1-paper-builder-implementation-plan.md`.** It supersedes the earlier draft of this file. API: `04-api-contracts.md` (Papers). Tables: `03-database-schema.md`.

## Goal

HR and Super Admin generate a Round 1 question paper (IT or Non-IT track) entirely with LLMs, review **one sample paper**, **edit any of its questions**, accept it, and send it to candidates. Existing screens and mock data keep working; the feature is additive (new module, new page, new tables).

## Decisions

1. **LLM-only generation.** Gemini generates Aptitude, Reasoning and Technical questions into a pool (about `pool_factor` × paper size); Groq, a different model family, independently solves each one blind and rejects mismatches. No code generators, no seed pool and no Judge0 verification yet. Accuracy is protected by the verifier, a required worked solution (`why`) and HR editing; it cannot be eliminated.
2. **One sample paper** is shown in review (built for the reserved candidate `"sample"`). HR can edit any question; an edit changes the shared pool item for every candidate whose subset contains it. Edits are allowed only while the draft is `draft`; accept freezes the pool.
3. **Questions are stored in `questions` only when the paper is sent.** Until then they live in `paper_drafts.pool` (JSON). Sending writes the pool once (idempotent) and records one `paper_issues(draft_id, candidate_ref, seed)` per candidate. Each candidate's paper is rebuilt from the seed `drive_ref|track|candidate_ref`.
4. **Background generation** (FastAPI `BackgroundTasks`): `generating → draft | failed`, with progress counters. A draft stuck in `generating` for over 15 minutes is reported as `failed`.
5. **No marking or results, no student exam flow** yet. Sending only records the issue; students cannot open it until the exam module exists.
6. `drive_ref` and `candidate_ref` are opaque text (no FK) because the drives and candidates modules are not built.
7. Migration `0002` adds `paper_drafts`, `paper_issues` and `questions.explanation`. It is written, not applied; applying to the shared database needs explicit approval.

## Checks and generation rules

Accept is blocked unless all checks pass: `settings_valid`, `pool_covers` (enough questions per level and section), `no_repeats`, `answers_present`, `students_differ`. Every generated question is validated with Pydantic (exactly 4 distinct options, one correct index, `why`), verified by Groq, and dropped if it disagrees. If too few survive, the draft becomes `failed`. A missing API key gives `AI_PROVIDER_NOT_CONFIGURED`; provider outages and rate limits are retried with backoff, then `AI_PROVIDER_ERROR`. Verification is never silently skipped. Keys (`GEMINI_API_KEY`, `GROQ_API_KEY`) come from `.env` and never reach the browser. Answers never appear in student-facing schemas.

## Frontend

A 4-step flow under `/hr/papers`: **Setup → Generate → Review → Send**. The builder needs a real sign-in; in prototype mode the page shows a permission state. No mock fallback and no client-side builder. The send step lists existing mock candidates through one data hook, so it can switch to the real candidates endpoint later.

## Out of scope / later

Student exam-taking flow, marking and results, code generators and a seeded technical pool, Judge0 verification of syntax snippets, verbal-ability section, per-question statistics, applying migrations to the shared database.

## Risks

- LLM answers can be wrong; the verifier and HR editing mitigate this, not eliminate it. Report the verifier rejection rate after the first real run.
- `persist_questions` SQL has not run against Postgres yet (migration unapplied); smoke-test on a dev database.
- Soft-skill "best answer" is judgement-based; the rationale is shown and HR can edit it.
