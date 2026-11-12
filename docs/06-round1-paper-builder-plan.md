# 06 — Round 1 Paper Builder: plan

Status: **draft for review** (nothing implemented). Branch: `feature/round1-paper-builder` (both repos, uncommitted).
Inspiration: `smart-recruitment-hr-portal/question-engine` (Python engine + React builder).

## 1. Goal

HR and Super Admin build a Round 1 question paper for a drive by choosing a **role track — IT or Non-IT** — then issue a
different paper to each existing (mock) candidate, and see marked results. Existing screens and mock data must keep working.

Success criteria
- A paper is built from settings: counts per level (easy / medium / advanced) and a mix of Aptitude / Reasoning / Technical.
- Same student ⇒ same paper; different students ⇒ different questions, question order and option order.
- No repeated question in a paper; every question has a correct answer and a reason; answers never reach the student.
- A draft that fails its checks cannot be accepted; once accepted, settings are locked; issuing twice never changes a paper;
  a student submits once.
- Nothing existing breaks: the feature is additive (new module, new page, new tables); existing routes, screens and mock data are untouched.
- Production-grade only: the API is the single source of truth. No client-side fallback builders, no JSON-file or in-memory
  storage in runtime code, no simulated AI calls. If the API or an AI provider is unavailable the UI shows a clear error and retry.

Out of scope: the live student exam-taking flow, verbal-ability section, proctoring changes, applying migrations to the shared DB.

## 2. Where questions come from

| Part | Source | Notes |
|---|---|---|
| Aptitude | **Recommended: code generators** (random numbers per student, answer computed) | Pure-AI alternative is possible (§5) but LLM arithmetic can be wrong. Decision pending. |
| Reasoning | **Recommended: code generators + solvers** (series, coding-decoding, syllogism, ordering, calendar, blood relations) | Same as above. |
| Technical — IT | Stored in `questions`: ~50% concept, ~50% **syntax** (output prediction, find the bug, fill the keyword, correct syntax) | Python, JS, Java, C/C++, SQL, HTML/CSS. Snippet outputs verified in the Judge0 sandbox before acceptance. |
| Technical — Non-IT | Stored in `questions`: **soft skills** (situational judgement, best/worst response, concepts, message tone) | Communication, teamwork, conflict, time mgmt, leadership, adaptability, ethics, customer handling, EQ. Each has a rationale. |

Seed pool: derived from the inspiration repo's 38 roles (≈1,368 facts + hand-written questions), merged into the two tracks,
de-duplicated, and extended with the syntax and soft-skill sets. Seeded rows are flagged "needs review" until a subject expert
approves them (the inspiration data was never expert-reviewed).

AI generation fills gaps and verbal/scenario content through the existing question-bank flow: drafts are saved
`approved=false`; only approved questions can enter a paper.

## 3. Generation best practice (applies to every AI-made question)

1. Structured output validated by a Pydantic schema: exactly 4 options, one correct index, reason, topic tag, level.
2. **Two agents on two providers**: a *generator* (**Gemini**) and an independent *verifier* (**Groq**, different model family so errors
   are uncorrelated) that solves the question blind; reject on mismatch, one retry, then drop. Keys come from `.env`
   (`GEMINI_API_KEY`, `GROQ_API_KEY`, optional model names) via `app/core/config.py`, documented in `.env.example`, never logged or
   sent to the browser. Missing key ⇒ explicit "AI provider not configured" error. On a provider outage/rate limit, retry with backoff;
   no silent degradation of verification. Groq is called over its OpenAI-compatible HTTP API with the existing HTTP client (ask before adding a dependency).
3. Distractors must be plausible (common mistakes), no "all of the above", no length giveaways.
4. Duplicate check against the existing pool (normalised text / embeddings).
5. Difficulty by rubric (steps, concept depth, expected time), not by feel.
6. Generate **ahead of time** (at draft creation), never during an exam; generate ~3× the paper size so each student draws a different subset.
7. Store prompt key + version, model and `ai_generated=true` on every generated row; prompts live in `ai_prompts` (editable in AI settings).
8. HR approval is the gate. Track "reported wrong" and per-question stats (everyone right / everyone wrong / top scorers miss it); re-score on key correction.

## 4. Backend design (`app/papers/`)

Follows the repo layout: `router.py → service.py → repo.py`, `schemas.py`.

```
app/papers/
  router.py        /api/v1/papers endpoints (require_role HR, SUPER_ADMIN; student endpoints for own paper)
  service.py       draft lifecycle: create+check → accept → issue → submit/mark
  repo.py          DB access (interface, so tests use an in-memory fake)
  schemas.py       Pydantic models (settings, draft, paper, result)
  builder.py       counts per level, largest-remainder mix split, seeded shuffle (drive|track|student), no-duplicate guard
  generators/      aptitude.py, reasoning.py (computed answers)
  agents.py        Gemini generator + verifier (schema-validated, mocked in tests)
  seed/            IT and Non-IT technical pools + loader
```

### Endpoints (all errors use `{"error": {"code","message","details"}}`)

| Method | Path | Who | Purpose |
|---|---|---|---|
| POST | `/papers/drafts` | HR, SA | Build + check a draft from settings |
| GET | `/papers/drafts`, `/papers/drafts/{id}` | HR, SA | List (HR: own, SA: all) / detail with 2 sample papers, issued, results |
| POST | `/papers/drafts/{id}/accept` | HR, SA | Accept (only if all checks pass); locks settings |
| POST | `/papers/drafts/{id}/issue` | HR, SA | `{candidate_ids}`; idempotent |
| GET | `/papers/drafts/{id}/key/{candidate_id}` | HR, SA | Paper with answers |
| GET | `/papers/mine` | Student | Own paper, **no answers** |
| POST | `/papers/mine/submit` | Student | One submission; marked server-side |

Every HR/SA mutation writes `audit_logs` in the same transaction. HR queries filter by drive ownership.

### Settings

`track: "it" | "non_it"`, `counts {easy, medium, advanced}`, `mix {aptitude, reasoning, technical}` (percent, sums to 100),
`technical_split` (IT: concept/syntax %), `duration_min`, `different_per_student`, `shuffle_questions`, `shuffle_options`.

### Determinism and keys

Shuffle seed = `drive | track | candidate`. A candidate's paper and answer key are **rebuilt from the seed** at marking time;
answer keys are not stored. Options with a conventional order ("Only I follows") are never shuffled.

### Data (DB)

- Technical pool lives in the existing `questions` table (tags: `it` / `non_it`, `section:technical`, `kind:concept|syntax|soft_skill`;
  `difficulty`, `approved`, `ai_generated`). No schema change.
- New migration `0002_paper_builder` adds `paper_drafts` (drive, owner, track, settings jsonb, status, checks jsonb, accepted_at)
  and `paper_issues` (draft, candidate, seed, answers jsonb, score, breakdown jsonb, submitted_at; unique draft+candidate).
  RLS on, working downgrade, mirrored in `03-database-schema.md` in the same change.
- A second migration (data) seeds the technical pool. **Migrations are written, not applied** — applying to the shared Supabase DB needs explicit OK.

## 5. Open decision

Aptitude and Reasoning: **hybrid (recommended)** — code generators with built-in answer checks — vs **pure AI agents** (generator +
verifier, pool generated at draft time). Both fit this design; only `generators/` vs `agents.py` usage changes.

## 6. Frontend design (`recruitment-frontend`)

- New page `/hr/papers` (nav: "Paper builder", HR + Super Admin) plus a link from the Round 1 aptitude round page.
- Code in `src/features/papers/` + a store slice; existing screens untouched. Built from Stitch tokens/SCSS, existing `ui/` components.
- 4-step stepper: **Setup → Preview → Issue → Results**.
  - Setup: IT / Non-IT segmented toggle; easy/medium/advanced steppers; mix slider with live percentages; IT syntax-vs-concept split;
    live pool-coverage meter and shortfall warning.
  - Preview: integrity checklist (valid settings, pool covers paper, no repeats, answers present, students differ), two sample
    papers side by side with a "how different" score; Accept disabled until checks pass. Code questions render in monospace blocks.
  - Issue: existing mock candidates as a checkable list (search, select all), status chips (issued / submitted).
  - Results: scores with split by level and by part; per-candidate paper/key view.
- Quality bar: loading / empty / error / permission-denied states, keyboard + aria, reduced-motion respected, 360px-wide layout,
  no hard-coded colours.
- API client in `src/api/` talks to the backend only. No local builder and no mock paper logic; failures surface as error states with retry.
- Candidates: the issue step lists the existing mock candidates (as requested) through a single data hook, so it switches to the real
  candidates endpoint without UI changes once that module exists.

## 7. Testing and verification

- Backend `pytest`: determinism, student-to-student difference, no duplicates, mix sums to total, shortfall reporting, accept gate,
  locked settings, idempotent issue, single submit, answers absent from student payload, role guards, audit rows, schema
  validation of AI output (Gemini/Judge0 mocked).
- Frontend `vitest`: stepper gating, API client error handling, key components (API mocked at the network boundary in tests only).
- `ruff check` / `ruff format --check`, `npm run lint`, `typecheck`, `build` — report any failure honestly.

## 8. Delivery order

1. Backend: schemas + builder + generators + tests (no DB).
2. Backend: service + Postgres repo + router + role/audit tests (an in-memory fake repo exists in tests only).
3. Backend: migrations (written, not applied) + seed pools + doc updates (`03`, `04`).
4. Backend: Gemini generator/verifier agents (mocked) + soft-skill / syntax authoring.
5. Frontend: types, API client, candidates hook.
6. Frontend: builder UI (four steps) → nav + round-page link → polish and responsive pass.
7. Full verification run; summary of changes. **No commits** — left for the user.

## 9. Risks

- Seeded technical content is unreviewed — expert review needed before real candidates.
- LLM answers can be wrong — verifier + HR approval mitigate, not eliminate.
- Soft-skill "best answer" is judgement-based — rationale shown, HR can edit keys.
- Drives/tests/candidates modules are not built yet — the module keeps its own tables and talks to candidates by id to avoid blocking on them.
