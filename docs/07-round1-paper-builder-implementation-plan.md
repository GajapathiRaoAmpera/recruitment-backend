# Round 1 Paper Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** HR and Super Admin generate a Round 1 question paper (IT or Non-IT track) entirely with LLMs, review **one sample paper** and **edit any of its questions**, accept it, and send it to candidates — at which point the questions are saved to the DB.

**Architecture:** A new backend module `app/papers/` (router → service → repo, a pure `builder`, and LLM `agents`). Creating a paper starts a background job: **Gemini generates** Aptitude, Reasoning and Technical questions into a pool (about `pool_factor` × paper size) and **Groq independently verifies** each one. The pool lives inside the draft row (`paper_drafts.pool`, JSON) while HR reviews and edits; on **send**, the pool is written to the existing `questions` table and each candidate gets their own seeded subset. The Next.js app gets a 4-step flow under `/hr/papers`: Setup → Generate → Review → Send.

**Tech Stack:** Backend — FastAPI (+ BackgroundTasks), Pydantic v2, SQLAlchemy 2 async raw `text()` SQL (like `app/admin/repo.py`), Alembic, pytest, ruff, httpx (ships with `fastapi[standard]`). Frontend — Next.js 16, React 19, TypeScript, SCSS modules + Ampera tokens, Radix, lucide, Vitest + Testing Library.

**Spec:** `docs/06-round1-paper-builder-plan.md` (this plan supersedes it where they differ; Task 6 updates 06).

## Decisions and deviations (read first)

1. **LLM-only question generation** (user decision). No code generators, no seeded pool. Accuracy risk for Aptitude/Reasoning is handled by the blind verifier, a required worked solution, and HR editing — it cannot be eliminated; report the verifier rejection rate after the first real run.
2. **Questions are stored in `questions` only when the paper is sent** (user decision). Until then they live in `paper_drafts.pool` so HR previews exactly what students will get. After accept the pool is frozen; edits are allowed only while status is `draft`.
3. **One sample paper** in review (user decision), built from the pool for the reserved candidate ref `"sample"`. Editing a question edits the shared pool item, so it changes for every candidate whose subset contains it.
4. **"Send" = record an issue.** There is no student login or exam flow in the backend yet, so sending stores `paper_issues(draft_id, candidate_ref, seed)` and persists the questions; students cannot open it until the exam module exists. Marking and results are out of scope.
5. **`drive_ref` / `candidate_ref` are opaque `text`, no FK** (mock ids like `d1`, `d1-c0`; drives/candidates modules do not exist yet).
6. **Generation runs in the background** (FastAPI `BackgroundTasks`): status `generating → draft | failed`, progress counters on the row. A draft stuck in `generating` for more than 15 minutes is reported as `failed` on read (process restarts lose in-flight jobs).
7. `questions.explanation text` is added (schema has no place for the "why"). Judge0 verification of syntax snippets is deferred (execution module not built).
8. The paper builder needs a real sign-in; in prototype mode (no Supabase env) the page shows a permission state. No mock fallback, no client-side builder.

## Global Constraints

- **No commits.** The user commits. Every "checkpoint" is `git status` / `git diff --stat` only. Never `git commit`, `push` or `stash`.
- Branch `feature/round1-paper-builder` in **both** repos (backend exists; frontend created in Task 0).
- Backend stack is fixed. **No new dependencies**; ask first.
- Every route declares `require_role("hr")` (Super Admin passes automatically). HR sees only drafts they own; Super Admin sees all. Cross-owner access returns **404**.
- Every HR/SA mutation calls `app.core.audit.audit(...)` in the same transaction before commit.
- Errors only via `AppError(status, code, message, details)`.
- Validate all LLM output with Pydantic before use; Gemini/Groq mocked in tests; API keys never logged, returned, or sent to the browser.
- Applied migrations are immutable. **Write migrations, do not apply them.** Use the `new-migration` skill; RLS on new tables; working `downgrade`; mirror in `docs/03-database-schema.md`.
- Levels `easy | medium | advanced`, tracks `it | non_it`, sections `aptitude | reasoning | technical` everywhere.
- Answers (`answer`, `why`) never appear in any student-facing schema.
- Frontend: tokens only (no hard-coded colours), reuse `components/ui` and `components/hire`, every view handles loading / empty / error / permission-denied, reduced-motion safe, works at 360 px, **no new npm dependencies** (small `useAsync` hook instead of TanStack Query). Do **not** modify the existing question-bank page; build a new edit dialog in `features/papers` modelled on its `EditDialog`.
- Do not invent endpoints silently: add every new endpoint to `docs/04-api-contracts.md` (Task 6).
- Commands: backend `uv run pytest -q`, `uv run ruff check . && uv run ruff format --check .`; frontend `npm run lint`, `npm run typecheck`, `npm test`, `npm run build`. If `uv` is missing, say so and install per the Makefile before starting.

## Review Focus

1. Mix not summing to 100, zero total, negative values → 422 `VALIDATION_ERROR` (Task 1).
2. LLM returns malformed JSON, 3 options, duplicate options, an out-of-range answer, or a verifier disagreement → question rejected, never saved; if too few survive for the paper, the draft becomes `failed` with a clear message (Task 3).
3. Missing API key / provider 429 / provider outage → explicit `AI_PROVIDER_NOT_CONFIGURED` / retry with backoff then `AI_PROVIDER_ERROR`; verification is never silently skipped (Tasks 3, 4).
4. Pool smaller than the paper, or too few questions at one level+section → `pool_covers` check fails, accept blocked, no repeats (Tasks 2, 4).
5. Editing after accept, or editing to an invalid question (blank text, duplicate options, bad answer index), or another HR editing someone's draft → 409 / 422 / 404 (Task 4).
6. Sending twice, to an empty list, or before accept → idempotent / 422 / 409; a question is never written twice to `questions` (Task 4).

## File Structure

Backend (`recruitment-backend/`):

| Path | Responsibility |
|---|---|
| `app/papers/schemas.py` | settings, pool question, paper items, checks, draft DTOs |
| `app/papers/builder.py` | pure: percent split, seeded paper from a pool, checks, overlap |
| `app/papers/agents.py` | Gemini generator, Groq verifier, prompts, batch generate+verify, pool orchestration with progress |
| `app/papers/repo.py` | all SQL (drafts, issues, persisting questions) |
| `app/papers/service.py` | create/background-generate/get/list/edit/accept/send + audit + AppErrors |
| `app/papers/router.py` | `/api/v1/papers/*` |
| `app/core/config.py`, `.env.example` | provider keys and model names |
| `migrations/versions/0002_paper_builder.py` | `paper_drafts`, `paper_issues`, `questions.explanation` |
| `tests/papers/…` | mirrors the above |

Frontend (`recruitment-frontend/src/`):

| Path | Responsibility |
|---|---|
| `api/papers.ts`, `features/papers/types.ts` | typed API calls and DTOs |
| `features/papers/use-async.ts`, `use-drive-candidates.ts` | fetch hook; mock-store candidates (single swap point) |
| `features/papers/components/*.tsx`, `papers.module.scss` | stepper, track toggle, mix editor, setup, generation progress, checks, paper view, question edit dialog, review, send, draft list |
| `app/(staff)/hr/papers/page.tsx`, `new/page.tsx`, `[draftId]/page.tsx` | list, setup, generate/review/send |
| `lib/nav.ts`; aptitude round page | nav entry; "Build Round 1 paper" link |

---

### Task 0: Branches and baseline

- [ ] **Step 1:** In `recruitment-frontend`: `git checkout -b feature/round1-paper-builder` (backend already on it).
- [ ] **Step 2:** Baseline: backend `uv run pytest -q`, `uv run ruff check .`; frontend `npm run typecheck`, `npm test`. Record pre-existing failures.
- [ ] **Step 3:** `uv run python -c "import httpx"` succeeds (no new dependency needed).
- [ ] **Step 4:** Checkpoint: `git status --short` in both repos.

### Task 1: Schemas and settings validation

**Files:** Create `app/papers/__init__.py`, `app/papers/schemas.py`, `tests/papers/__init__.py`, `tests/papers/test_schemas.py`.

**Produces:**
```python
Track = Literal["it", "non_it"]; Level = Literal["easy", "medium", "advanced"]
Section = Literal["aptitude", "reasoning", "technical"]
DraftStatus = Literal["generating", "draft", "accepted", "failed"]
LEVELS: tuple[Level, ...]; SECTIONS: tuple[Section, ...]

class PaperSettings(BaseModel):
    track: Track
    counts: dict[Level, int]          # all three keys required, each 0..100, sum >= 1
    mix: dict[Section, int]           # all three keys, each 0..100, sum == 100
    it_syntax_percent: int = 50       # 0..100; IT technical only; guides the prompt (rest are concept questions)
    pool_factor: int = 2              # 1..3; pool size = factor x paper size
    duration_min: int = 60            # 5..240
    different_per_student: bool = True
    shuffle_questions: bool = True
    shuffle_options: bool = True

class PoolQuestion(BaseModel):        # stored in paper_drafts.pool
    id: str                           # uuid4 string, becomes questions.id on send
    level: Level; section: Section; topic: str; style: str   # style: concept | syntax | soft_skill | aptitude | reasoning
    text: str; options: list[str]; answer: int; why: str
    fixed_options: bool = False       # conventional-order options are never shuffled
    edited: bool = False; verified: bool = True
class PaperItem(BaseModel): id: str; level: Level; section: Section; topic: str; text: str; options: list[str]   # student-safe
class KeyItem(PaperItem):   answer: int; why: str; fixed_options: bool = False; edited: bool = False
class QuestionEdit(BaseModel): text: str; options: list[str]; answer: int; why: str    # 4 distinct non-blank options, 0<=answer<4, non-blank text and why
class Check(BaseModel): id: str; label: str; ok: bool; detail: str
```

- [ ] **Step 1: Failing tests:** `test_mix_must_sum_to_100`, `test_counts_total_must_be_positive`, `test_negative_count_rejected`, `test_all_levels_and_sections_required`, `test_pool_factor_and_duration_bounds`, `test_question_edit_rejects_duplicate_options`, `test_question_edit_rejects_bad_answer_index`, `test_question_edit_rejects_blank_text`, `test_paper_item_has_no_answer_field`.
- [ ] **Step 2:** `uv run pytest tests/papers/test_schemas.py -q` → FAIL. **Step 3:** Implement (`PaperItem` must not inherit from `KeyItem`). **Step 4:** PASS; `uv run ruff check app/papers tests/papers`.

### Task 2: Builder and checks (pure)

**Files:** Create `app/papers/builder.py`; Test `tests/papers/test_builder.py`.

**Produces:**
```python
def split_by_percent(total: int, weights: Mapping[str, int]) -> dict[str, int]          # largest remainder; sums to total
def needed(settings: PaperSettings) -> dict[Level, dict[Section, int]]                   # per level, per section counts
def build_paper(settings: PaperSettings, drive_ref: str, candidate_ref: str,
                pool: Sequence[PoolQuestion]) -> BuiltPaper                              # BuiltPaper(items: list[KeyItem], missing: dict[str, int])
def overlap(a: Sequence[KeyItem], b: Sequence[KeyItem]) -> Overlap                       # Overlap(shared:int, same_position:int, total:int)
def run_checks(settings: PaperSettings, drive_ref: str, pool: Sequence[PoolQuestion]) -> list[Check]
```
Seed string `f"{drive_ref}|{settings.track}|{candidate_ref}"` with `random.Random(seed)`; `different_per_student=False` → candidate_ref `"fixed"`. Per level+section draw `needed` questions from the pool without repeats; never invent; shuffle order/options per settings but never options of `fixed_options` questions; the correct `answer` index follows the option shuffle. Checks (ids): `settings_valid`, `pool_covers`, `no_repeats` (also no duplicate text), `answers_present` (4 distinct options, valid index, non-blank why), `students_differ` (builds `"sample-a"` vs `"sample-b"`; passes when `different_per_student` is false or overlap < 100%).

- [ ] **Step 1: Failing tests:** `test_split_sums_to_total` (parametrised incl. 1 question / 3 parts), `test_same_candidate_same_paper`, `test_different_candidates_differ`, `test_fixed_mode_same_questions_for_all`, `test_no_repeated_question`, `test_counts_per_level_and_section_match_settings`, `test_shortfall_reported_not_invented`, `test_fixed_options_never_shuffled`, `test_answer_index_follows_option_shuffle`, `test_edited_question_appears_edited_in_paper`, `test_pool_covers_check_fails_on_shortfall`, `test_answers_present_check_flags_bad_question`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.

### Task 3: LLM agents (Gemini generates, Groq verifies)

**Files:** Modify `app/core/config.py`, `.env.example`; Create `app/papers/agents.py`; Test `tests/papers/test_agents.py`.

Config adds `gemini_api_key: str | None`, `groq_api_key: str | None`, `gemini_model: str`, `groq_model: str` (no key defaults). Look up current model ids and request formats (Gemini JSON mode; Groq OpenAI-compatible chat) in the providers' docs at implementation time — do not guess. Default prompts are constants in code; an `ai_prompts` row with key `paper_gen_<section>` / `paper_verify` (check columns in `docs/03-database-schema.md`) overrides them so Super Admin can tune them later.

**Produces:**
```python
class GeneratedMcq(BaseModel): text: str; options: list[str]; answer: int; why: str; topic: str; fixed_options: bool = False   # 4 distinct options, 0<=answer<4
class LlmClient(Protocol): async def complete_json(self, *, system: str, user: str) -> dict
class GeminiClient(LlmClient); class GroqClient(LlmClient)      # httpx, timeouts, retry 429/5xx with backoff (max 3), never echo keys
def providers_status() -> dict[str, bool]                        # {"gemini": bool, "groq": bool}
async def generate_batch(generator, verifier, *, track, level, section, style, topic, count, avoid: Sequence[str]) -> BatchResult   # accepted: list[PoolQuestion], rejected: int
async def generate_pool(generator, verifier, settings: PaperSettings, *, on_progress: Callable[[int, int], Awaitable[None]]) -> list[PoolQuestion]
```
Prompt rules: level rubric (easy 1 step / medium 2 steps / advanced multi-step or edge case); Aptitude and Reasoning require a worked solution in `why` and plausible distractors (common slips); IT technical mixes concept and **syntax** (output prediction, find the bug, fill the keyword, correct syntax; code in fenced blocks) per `it_syntax_percent`; Non-IT technical is **soft skills** (situational judgement, best response, concept, message tone) with a rationale; no "all of the above"; syllogism-style questions set `fixed_options`. Flow per batch (≤ 8 questions): generate → schema-validate → drop duplicates of already-accepted text → verifier solves **blind** (no answer given) → keep only on agreement; one regeneration per mismatching question, then reject. `generate_pool` plans batches from `needed(settings) × pool_factor`, runs at most 3 concurrently (`asyncio.Semaphore`), reports progress, and raises `AppError(503, "AI_PROVIDER_NOT_CONFIGURED")` / `AppError(502, "AI_PROVIDER_ERROR")` / `AppError(422, "POOL_TOO_SMALL")` when survivors cannot cover the paper (use `needed`).

- [ ] **Step 1: Failing tests (fake `LlmClient`s, no network):** `test_valid_json_accepted`, `test_malformed_json_rejected`, `test_wrong_option_count_rejected`, `test_duplicate_options_rejected`, `test_verifier_disagreement_rejected_after_one_retry`, `test_duplicate_text_dropped`, `test_missing_key_raises_not_configured`, `test_429_retries_then_provider_error`, `test_keys_never_in_errors_or_logs` (caplog), `test_generate_pool_covers_needed_counts_times_factor`, `test_generate_pool_raises_pool_too_small`, `test_progress_reported_monotonically`, `test_it_prompt_mentions_syntax_and_non_it_mentions_soft_skills`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS. `.env.example` documents `GEMINI_API_KEY`, `GROQ_API_KEY`, `GEMINI_MODEL`, `GROQ_MODEL` (empty, "server only"). Do **not** touch `.env` (hook-blocked); tell the user to add keys.

### Task 4: Migration, repo, service, router

**Files:** Create `migrations/versions/0002_paper_builder.py`, `app/papers/repo.py`, `app/papers/service.py`, `app/papers/router.py`; Modify `app/main.py` (include router), `docs/03-database-schema.md`; Test `tests/papers/test_repo_sql.py`, `tests/papers/test_api.py` (follow the `FakeSession` + monkeypatched repo + dependency-override pattern of `tests/admin/test_users.py`).

Use the `new-migration` skill. `0002` (down_revision `"0001"`), one statement per `op.execute`: `alter table questions add column explanation text`; `paper_drafts (id uuid pk default gen_random_uuid(), drive_ref text not null, owner_id uuid not null references users(id), title text not null, track text not null check in ('it','non_it'), settings jsonb not null, pool jsonb not null default '[]', checks jsonb not null default '[]', status text not null check in ('generating','draft','accepted','failed'), progress jsonb not null default '{"done":0,"total":0}', error text, created_at timestamptz not null default now(), accepted_at timestamptz, persisted_at timestamptz)`; `paper_issues (draft_id uuid not null references paper_drafts(id) on delete cascade, candidate_ref text not null, seed text not null, issued_at timestamptz not null default now(), primary key (draft_id, candidate_ref))`; indexes on `paper_drafts(owner_id)`, `paper_drafts(drive_ref)`; RLS on both; full `downgrade`. **Write only; do not apply.**

**Repo (async, first arg `session`):** `insert_draft(...)`, `get_draft(draft_id)`, `list_drafts(owner_id | None)`, `update_progress(draft_id, done, total)`, `finish_generation(draft_id, pool, checks)`, `fail_generation(draft_id, error)`, `save_pool_and_checks(draft_id, pool, checks) -> bool` (only when `status='draft'`), `mark_accepted(draft_id)` (only from `draft`), `persist_questions(draft_id, pool) -> int` (insert into `questions` with the pool ids, `kind='mcq'`, `difficulty` mapped, `answer` = index, `tags = {paper:<id>, track:<t>, section:<s>, style:<x>}`, `ai_generated=true`, `approved=true`; `on conflict (id) do nothing`; stamps `persisted_at`), `insert_issues(draft_id, rows) -> int` (on conflict do nothing), `list_issues(draft_id)`.

**Service:**
```python
create_draft(session, actor, background, drive_ref: str, title: str, settings: PaperSettings) -> DraftOut   # 503 if keys missing; status 'generating'
run_generation(draft_id: UUID, actor_id: UUID) -> None            # background; opens its own session; progress, then draft|failed
get_draft(session, actor, draft_id) -> DraftDetail                # + sample (KeyItem list for candidate "sample"), overlap, progress, issues; stale 'generating' (>15 min) reported failed
list_drafts(session, actor) -> list[DraftOut]
edit_question(session, actor, draft_id, question_id: str, body: QuestionEdit) -> DraftDetail   # 409 NOT_EDITABLE unless draft; sets edited=True; re-runs checks
accept_draft(session, actor, draft_id) -> DraftOut                # 409 CHECKS_FAILED / ALREADY_ACCEPTED
send_papers(session, actor, draft_id, candidate_refs: list[str]) -> SendResult   # {sent, already_sent}; 409 NOT_ACCEPTED; 422 empty; persists questions once
ai_status() -> dict[str, bool]
```
Routes (prefix `/papers`, `require_role("hr")`): `GET /ai-status`, `POST /drafts`, `GET /drafts`, `GET /drafts/{id}`, `PATCH /drafts/{id}/questions/{question_id}`, `POST /drafts/{id}/accept`, `POST /drafts/{id}/send`. `owner_id` filter for HR, none for Super Admin; other owner → `AppError(404, "NOT_FOUND", …)`. All mutations `audit(...)` before commit.

- [ ] **Step 1: Failing tests:** repo — `test_persist_questions_uses_on_conflict_do_nothing`, `test_mark_accepted_only_from_draft`, `test_save_pool_only_when_draft`, `test_insert_issues_on_conflict_do_nothing`; API — `test_create_starts_generation_and_audits`, `test_create_503_when_keys_missing`, `test_generation_success_sets_draft_with_checks`, `test_generation_failure_sets_failed_with_message`, `test_stale_generating_reported_failed`, `test_detail_has_one_sample_paper`, `test_edit_question_updates_pool_and_flags_edited`, `test_edit_rejects_invalid_and_is_audited`, `test_edit_after_accept_conflicts`, `test_failed_checks_block_accept`, `test_accept_locks_and_audits`, `test_accept_twice_conflicts`, `test_send_requires_accepted`, `test_send_persists_questions_once_and_is_idempotent`, `test_send_dedupes_refs_and_rejects_empty`, `test_other_hr_gets_404`, `test_super_admin_sees_all`, `test_unauthenticated_401_non_staff_403`, `test_validation_error_shape`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement; write the migration; `uv run alembic heads` shows single head `0002` **without applying**. **Step 4:** `uv run pytest -q` (full suite) PASS; ruff clean.

### Task 5: Backend docs

- [ ] **Step 1:** Add a "Papers (Round 1)" section to `docs/04-api-contracts.md` for every route in Task 4 (method, path, auth, body, response, errors `CHECKS_FAILED`, `ALREADY_ACCEPTED`, `NOT_EDITABLE`, `NOT_ACCEPTED`, `NOT_FOUND`, `POOL_TOO_SMALL`, `AI_PROVIDER_NOT_CONFIGURED`, `AI_PROVIDER_ERROR`) and a screen→endpoint row.
- [ ] **Step 2:** Update `docs/06-round1-paper-builder-plan.md` to match this plan's decisions (LLM-only, one sample paper, edit, store-at-send) and point to this file; add one line to `05-implementation-plan.md`.
- [ ] **Step 3:** `uv run pytest -q && uv run ruff check . && uv run ruff format --check .` green. Checkpoint `git status --short`.

### Task 6: Frontend API layer, nav and guards

**Files:** Create `src/features/papers/types.ts`, `use-async.ts`, `use-drive-candidates.ts`, `src/api/papers.ts`; Modify `src/lib/nav.ts`; Test `src/api/papers.test.ts`, `src/features/papers/use-async.test.tsx`.

**Produces (`src/api/papers.ts`; one place maps snake_case ↔ camelCase):**
```ts
aiStatus(): Promise<{gemini: boolean; groq: boolean}>
createDraft(body: {driveRef: string; title: string; settings: PaperSettings}): Promise<Draft>
listDrafts(): Promise<Draft[]>
getDraft(id: string): Promise<DraftDetail>                 // status, progress, checks, sample: KeyItem[], overlap, issues
editQuestion(id: string, questionId: string, body: QuestionEdit): Promise<DraftDetail>
acceptDraft(id: string): Promise<Draft>
sendPapers(id: string, candidateRefs: string[]): Promise<{sent: number; alreadySent: number}>
```
`useAsync<T>(fn, deps): {data?, error?: ApiError, loading, reload()}` ignores stale responses. `useDriveCandidates(driveId): Candidate[]` reads `state.candidates` from the mock store. Nav: `{label: "Paper builder", href: "/hr/papers", icon: FileText, section: "main"}` after "Question bank".

- [ ] **Step 1: Failing tests:** `maps snake_case and surfaces ApiError code/message`, `useAsync ignores stale response`, `useAsync reload refetches`, `nav shows Paper builder for HR and Super Admin`.
- [ ] **Step 2:** `npm test -- papers use-async` FAIL. **Step 3:** Implement (mock `fetch` at the boundary). **Step 4:** PASS; `npm run typecheck`.

### Task 7: Setup and generation progress (`/hr/papers/new`, `/hr/papers/[draftId]` while generating)

**Files:** Create `new/page.tsx`, `[draftId]/page.tsx` (shell + generating view), `components/{stepper,track-toggle,mix-editor,count-steppers,setup-step,generation-progress}.tsx`, `papers.module.scss`; Test `setup-step.test.tsx`, `generation-progress.test.tsx`.

Use the `ui-ux-pro-max` skill for the visual pass; Ampera tokens only. Setup: drive select (mock drives, pre-selected from `?drive=`), title, **IT / Non-IT segmented control** (radiogroup, arrow keys), easy/medium/advanced steppers, mix editor for Aptitude/Reasoning/Technical that always totals 100 (visible running total, "Balance to 100"), IT-only "Syntax vs concept" slider, pool factor (1–3, with a plain hint on per-student variety), duration, shuffle toggles, an **AI provider status banner** from `aiStatus` (Create disabled with an actionable message when a key is missing), and a sticky summary ("60 questions · will generate ~120 · 60 min"). Create → navigate to the draft. Generation view: determinate progress from `progress.done/total` (poll `getDraft` every 2 s while `generating`, stop on unmount), plain-language stage text, `aria-live="polite"`, failed state shows the server message with "Start over". Prototype mode → permission state.

- [ ] **Step 1: Failing tests:** `track toggle is a radiogroup operable by arrow keys`, `mix editor blocks submit unless total is 100`, `create disabled while counts total 0`, `create disabled with message when a provider key is missing`, `shows permission state in prototype mode`, `server 422 shown without losing form values`, `progress bar reflects done/total and stops polling on unmount`, `failed generation shows message and start over`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS; lint + typecheck.

### Task 8: Review — one sample paper with editable questions

**Files:** Create `components/{checks-list,paper-view,question-card,question-edit-dialog,review-step}.tsx`; Test `review-step.test.tsx`, `question-edit-dialog.test.tsx`.

Review step shows the integrity checklist (icon + text + detail, never colour alone), pool summary ("120 generated · 60 in this sample · verified by 2 models"), and **one** sample paper as question cards (level and section chips, "Verified" and "Edited" badges, options A–D with the correct one marked, collapsible "Why", fenced code rendered in monospace). Each card has an **Edit** button opening `question-edit-dialog` (side dialog, same fields and tone as the question-bank `EditDialog`: question text, four options, correct-answer select, explanation; inline validation mirroring `QuestionEdit`; saving calls `editQuestion`, refreshes checks, toasts, warns that the change applies to every candidate who gets this question). "Accept paper" is disabled until all checks pass, opens a confirm dialog that settings and questions lock; accepted drafts show a locked banner and hide Edit.

- [ ] **Step 1: Failing tests:** `shows exactly one sample paper`, `every card has an Edit button while draft`, `edit dialog blocks duplicate options and blank text`, `saving calls editQuestion and refreshes`, `edited badge appears after save`, `accept disabled until all checks pass`, `confirm dialog before accept`, `accepted draft hides Edit and shows locked banner`, `code fences render as code blocks`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.

### Task 9: Send and draft list (`/hr/papers`)

**Files:** Create `app/(staff)/hr/papers/page.tsx`, `components/{send-step,draft-list}.tsx`; Test `send-step.test.tsx`, `draft-list.test.tsx`.

Send: mock candidates for the draft's `driveRef` as a searchable checkable table (select all / none apply to the filtered rows, branch + CGPA columns, "sent" chips from `issues`), confirm → `sendPapers`; toast reports `sent` vs `alreadySent`; copy states that candidates cannot open it until the exam module ships. Draft list: cards with title, track, status (Generating / Draft / Accepted / Sent / Failed), counts, mix, created date, owner column for Super Admin only; loading skeletons, empty state with "New paper", error + retry.

- [ ] **Step 1: Failing tests:** `select all selects only filtered rows`, `send disabled until accepted and a candidate is selected`, `already-sent candidates labelled and not an error`, `draft list empty/loading/error`, `super admin sees owner column, hr does not`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.

### Task 10: Round link, polish and accessibility

**Files:** Modify `src/app/(staff)/hr/drives/[driveId]/rounds/[roundId]/page.tsx` (add "Build Round 1 paper" for `type === "aptitude"` rounds → `/hr/papers/new?drive=<driveId>`); polish `papers.module.scss`.

- [ ] **Step 1: Failing test:** `aptitude round shows the Build paper link with the drive id; coding round does not`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement, then the quality pass: reduced-motion respected, focus rings from tokens, labelled controls, `aria-live` for generation and check results, 360 px layout (stepper collapses, cards stack, side dialog becomes full-screen sheet), no hard-coded colours (`grep -rn "#[0-9a-fA-F]\{3,6\}" src/features/papers "src/app/(staff)/hr/papers"` returns nothing).
- [ ] **Step 4:** PASS.

### Task 11: Whole-feature verification

- [ ] **Step 1:** Backend `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .` — paste the summary lines.
- [ ] **Step 2:** Frontend `npm run lint`, `npm run typecheck`, `npm test`, `npm run build` — paste the summary lines.
- [ ] **Step 3:** `git diff --stat` in both repos: only expected pre-existing files touched (`app/main.py`, `app/core/config.py`, `.env.example`, `docs/03|04|05|06`, `src/lib/nav.ts`, the aptitude round page); existing tests unchanged and green.
- [ ] **Step 4:** If the user supplies keys and approves a dev database, run the app (skill `run`), create a small paper (e.g. 3/3/3), watch generation, edit a question, accept, send, and report the verifier rejection rate and timing. Otherwise state plainly that LLM behaviour was verified only with mocks.
- [ ] **Step 5:** Final checkpoint `git status --short` in both repos: list changed files, skipped steps, "add keys to `.env` yourself", "migration `0002` written but not applied", and that **nothing was committed**.
