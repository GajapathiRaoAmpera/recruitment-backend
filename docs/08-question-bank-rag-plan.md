> **Superseded (2026-11-13):** the PDF question bank and RAG retrieval were removed from the code. Technical questions are now written for the drive's role by an NVIDIA model and checked by HR. The database tables from migration 0004 remain, unused. Kept for history only.

# PDF Question Bank + RAG + Generators Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax. **No commits** (user commits).

**Goal:** Technical questions for the IT track come from an uploaded PDF question bank (HR / Super Admin can add, view, edit, replace, deactivate and delete bank files); Aptitude and Reasoning come from quantitative code generators; one LLM call composes the sample paper from retrieved candidates; HR reviews and edits; questions are stored in the DB when the paper is sent; every student's paper differs (questions, order and options jumbled).

**Architecture:** New module `app/qbank/` (parser, storage, repo, service, router, embeddings, retrieval). New `app/papers/generators/` (aptitude, reasoning). `papers.service.build_pool` is replaced by a pool builder that merges generators + bank retrieval (IT) and asks Gemini to choose which candidates are `selected` (ids only, validated). Non-IT keeps the existing LLM generation for its Technical part (no Non-IT PDF yet), with Aptitude/Reasoning from generators.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy 2 raw SQL, Alembic, pdfplumber (already a dependency), pgvector via hand-written SQL (no new Python dependency), Gemini embeddings over httpx. Frontend: Next.js 16, React 19, SCSS tokens, Vitest.

**Spec:** the user's messages in this session + `docs/07-round1-paper-builder-implementation-plan.md` (built and merged). This plan supersedes 07 where they differ.

## Rulings (made without waiting; each cheap to reverse)

1. **PDF reader = pdfplumber** (user decision; already in `pyproject.toml`). No OCR for this bank (it has a text layer); `pytesseract`/`pdf2image` stay for resumes.
2. **RAG = pgvector + Gemini embeddings** (recommended option; user said "use RAG"). Migration writes `create extension if not exists vector`; **written, not applied**.
3. **Permissions:** every HR/Super Admin can view and use every bank. **Uploader or Super Admin** can edit, approve, replace, deactivate or delete a bank (else 403 `FORBIDDEN`).
4. **Delete = hard delete** of the file, its `questions` rows and embeddings. Safe because paper pools hold independent copies of questions (new ids per draft); sent papers are unaffected. **Deactivate** (`is_active=false`) hides a bank from new papers without deleting.
5. **Non-IT Technical** stays LLM-generated (existing agents, Technical section only) until a Non-IT PDF is uploaded; a future Non-IT bank uses the same flow.
6. **File storage** follows the candidates module: local disk behind a small interface (`app/qbank/storage.py`), flagged as a shortcut (lost without a mounted volume; swap for object storage before prod).
7. **No silent fallbacks:** missing Gemini key → clear `AI_PROVIDER_NOT_CONFIGURED`; composer failure → draft `failed` with a message. Embeddings failing at upload does not fail the upload: the bank gets `embedding_status="failed"|"skipped"` (visible in UI) and retrieval then ranks by subject/level only — shown to HR, not hidden.
8. **LLM never writes or edits a question** in the new pipeline: it returns ids only; code validates counts/levels/uniqueness.

## Global Constraints

- Backend stack fixed (no new dependencies). Use `.\.venv\Scripts\python.exe -m pytest|ruff|alembic` (uv absent). Baseline: 229 tests pass.
- Every route `require_role("hr")` (Super Admin passes); every mutation `audit(...)` before commit; errors via `AppError`.
- Migrations: write, never apply; RLS on new tables; working downgrade; mirror in `docs/03-database-schema.md`; one statement per `op.execute`; **avoid `:name` in literal SQL strings** (text() binds them — use `jsonb_build_object`).
- Answers/why never in student-facing models. Uploaded PDF = untrusted: check `%PDF-` magic, max 20 MB, max 400 pages, sha256 de-dupe, never execute, never pass raw PDF text to an LLM.
- Level mapping: Easy→`easy`, Medium→`medium`, Hard→`advanced`.
- Frontend: tokens only, existing `ui/` + `hire/` components, loading/empty/error/permission states, 360 px, reduced motion, no new npm deps, do not modify existing screens except where a task says so. Use the `ui-ux-pro-max` skill for UI tasks.
- Tests never touch network or a real DB. Report exact commands/outputs; no commits.

## PDF layout facts (from `core_cs_500_mcq.pdf`; the parser must handle all of these)

- 500 questions, 6 subjects, one heading line per subject: `Programming and Object-Oriented Design` (84), `Data Structures and Algorithms` (84), `Operating Systems` (83), `Database Management Systems` (83), `Computer Networks` (83), `Software Engineering` (83). Levels: Easy 134, Medium 198, Hard 168.
- Page 1 is a summary table; `Page N` footers; questions start page 2. Question block text (per page extraction):
  ```
  1. Easy - Libraries
  What is the difference between a library and a framework?
  A. A framework is only used for testing
  B. Your code calls a library; a framework calls your code
  C. ...
  D. There is no difference
  ```
  Number restarts per subject. Levels are NOT ordered within a subject (e.g. DSA 74–80 are Medium after Hard 46–73). Stems may wrap to 2 lines; options may wrap; options can contain `/`, `*`, `^`, `<`, quotes, code-like text (`7 // 2`, `x & (x - 1)`, `O(n^2)`).
- Subject headings can appear mid-page right after the previous subject's last question (e.g. `Data Structures and Algorithms` directly after question 84).
- Answer key starts at heading `Answer Key`, one table per subject, header `# Level Answer`. Extraction variants seen (all must parse): `1 Easy B` (spaces), `36 MediumB` (level glued to answer, no space), and a run-on single line `Operating Systems # Level Answer 1 Easy A 2 Easy C ... 9 Easy B` followed by normal lines.
- Every question has exactly 4 options A–D and exactly one answer.

## File Structure

| Path | Responsibility |
|---|---|
| `app/papers/generators/{__init__,aptitude,reasoning}.py` | computed-answer generators → `GeneratedQuestion` |
| `app/qbank/parser.py` | pure: pages(text) → `ParsedBank` + `ParseReport`; `extract_pages(bytes)` via pdfplumber |
| `app/qbank/storage.py` | save/read/delete bank files (local disk shortcut) |
| `app/qbank/repo.py` | SQL: sources, questions, counts, embeddings, retrieval |
| `app/qbank/service.py`, `router.py`, `schemas.py` | CRUD, approve, edit, replace, delete, download |
| `app/qbank/embeddings.py`, `retrieval.py` | Gemini embedder; candidate retrieval |
| `app/papers/pool.py` | new `build_pool` (generators + bank + composer) |
| `app/papers/agents.py` | add `compose_selection`, allow `sections=` filter on generation |
| `migrations/versions/0003_question_bank.py` | `question_sources`, `question_embeddings`, `questions.source_id/subject` |
| `tests/qbank/…`, `tests/papers/test_generators_*.py`, `test_pool.py` | mirrors |
| Frontend | `api/qbank.ts`, `features/qbank/*`, pages under `app/(staff)/hr/question-banks/`, nav entry, setup additions |

---

### Task 1: Quantitative generators (Aptitude + Reasoning)

**Files:** Create `app/papers/generators/__init__.py`, `aptitude.py`, `reasoning.py`; Test `tests/papers/test_generators_aptitude.py`, `test_generators_reasoning.py`.

**Produces:**
```python
@dataclass(frozen=True)
class GeneratedQuestion: topic: str; text: str; options: list[str]; answer: int; why: str; fixed_options: bool = False
Generator = Callable[[random.Random, Level], GeneratedQuestion]
APTITUDE: dict[str, Generator]   # >= 8: percentage, profit_loss, ratio, time_work, speed_distance, simple_interest, average, mixture
REASONING: dict[str, Generator]  # >= 7: number_series, letter_series, coding_decoding, syllogism, ordering, calendar, blood_relation
def generate_questions(section: Literal["aptitude","reasoning"], level: Level, count: int, seed: str) -> list[PoolQuestion]
```
`generate_questions` rotates topics, uses `random.Random(seed)`, dedupes by normalised text (bounded retries, never fewer than `count` unless impossible → raises), returns `PoolQuestion` with `id=uuid5(NAMESPACE_URL, f"{seed}|{topic}|{text}")`, `style=section`, `selected=True`, `verified=True`. Reference ideas only: `C:\Users\samin\OneDrive\Desktop\smart-recruitment-hr-portal\question-engine\generators.py` and `reasoning.py` (re-write cleanly).

- [ ] **Step 1: Failing tests:** every generator × level yields 4 distinct options, `0<=answer<4`, non-empty `why`; same seed ⇒ same questions; different seeds ⇒ ≥ 10 distinct texts per topic over 20 seeds; **independent solver per topic** recomputes the answer from the generator's own numbers (no parsing of prose: generators expose structured facts via a `_facts` helper used only by tests, or tests re-derive from a seeded `rng` replay); syllogism options `fixed_options` and in conventional order; ordering puzzles have exactly one solution (brute force); calendar answers match `datetime.date.weekday`; `generate_questions` returns exactly `count` unique questions; ids stable across runs.
- [ ] **Step 2:** `.\.venv\Scripts\python.exe -m pytest tests/papers -q` → new tests FAIL. **Step 3:** Implement. Difficulty scales steps/numbers (easy one step; advanced two-step or unit conversion); distractors are plausible slips. **Step 4:** PASS + ruff.

### Task 2: PDF question-bank parser (pure) + extraction

**Files:** Create `app/qbank/__init__.py`, `app/qbank/parser.py`; Test `tests/qbank/__init__.py`, `tests/qbank/test_parser.py`.

**Produces:**
```python
SUBJECTS: tuple[str, ...]  # the six headings above (exact strings); unknown headings are accepted and slugged
@dataclass(frozen=True)
class ParsedQuestion: subject: str; number: int; level: Level; topic: str; text: str; options: list[str]; answer: int; warnings: tuple[str, ...]
@dataclass(frozen=True)
class ParseReport: total: int; by_subject: dict[str,int]; by_level: dict[str,int]; warnings: list[str]; skipped: list[str]
@dataclass(frozen=True)
class ParsedBank: questions: list[ParsedQuestion]; report: ParseReport
def parse_pages(pages: Sequence[str]) -> ParsedBank          # pure, no I/O
def extract_pages(data: bytes) -> list[str]                  # pdfplumber; raises AppError(422,"PDF_UNREADABLE")
```
Rules: join each question to its key entry by `(subject, number)`; a question with no key entry, a key entry with no question, a level mismatch between block and key, or ≠ 4 options → recorded in `warnings` (question kept only when it has 4 options and an answer; otherwise listed in `report.skipped`). Handle the three key-table variants and wrapped stems/options in "Layout facts". Never raises on odd content; `parse_pages([])` → empty bank with a warning.

- [ ] **Step 1: Failing tests** with inline text fixtures copied from the layout facts: `test_parses_basic_block`, `test_wrapped_stem_and_option`, `test_subject_heading_after_last_question`, `test_levels_not_monotonic`, `test_key_spaced`, `test_key_glued_level_answer` (`36 MediumB`), `test_key_run_on_single_line`, `test_level_mismatch_warns`, `test_missing_key_entry_skips_question`, `test_five_options_skipped`, `test_hard_maps_to_advanced`, `test_options_with_symbols` (`7 // 2`, `x & (x - 1)`, `O(n^2)`), `test_numbering_restarts_per_subject`, `test_empty_input`, `test_report_counts`. Add `test_real_pdf_if_present` that is skipped unless env `QBANK_SAMPLE_PDF` points to a file; when present it asserts 500 questions, 6 subjects, 134/198/168 levels, no skipped.
- [ ] **Step 2:** FAIL. **Step 3:** Implement with compiled regexes, line state machine. **Step 4:** PASS + ruff.

### Task 3: Migration, storage and repo

**Files:** Create `migrations/versions/0003_question_bank.py`, `app/qbank/storage.py`, `app/qbank/repo.py`; Modify `app/core/config.py` (`qbank_dir`, `qbank_max_mb=20`), `.env.example`, `docs/03-database-schema.md`; Test `tests/qbank/test_repo_sql.py`, `test_storage.py`.

Use the `new-migration` skill. `0003` (down_revision `"0002"`): `create extension if not exists vector`; `question_sources (id uuid pk default gen_random_uuid(), name text not null, filename text not null, storage_key text not null, sha256 text not null unique, track text not null check in ('it','non_it'), notes text, status text not null check in ('ready','failed') default 'ready', is_active boolean not null default true, uploaded_by uuid not null references users(id), report jsonb not null default '{}', embedding_status text not null default 'pending' check in ('pending','done','failed','skipped'), created_at timestamptz not null default now(), updated_at timestamptz not null default now())`; `alter table questions add column source_id uuid references question_sources(id) on delete cascade, add column subject text, add column topic text` (one statement per op.execute); `question_embeddings (question_id uuid primary key references questions(id) on delete cascade, embedding vector(768) not null, model text not null)` (dimension from config `embedding_dim`; if Gemini's current model differs, use that dimension in the migration and document); indexes on `questions(source_id)`, `questions(subject)`; RLS on the two new tables; full downgrade (drop in reverse; do NOT drop the extension). **Write only; never apply.**

**Storage (`storage.py`):** `save(key: str, data: bytes) -> None`, `read(key) -> bytes | None`, `delete(key) -> None`, path-traversal guard like `app/candidates/storage.py` (keys are UUID-only), runs file I/O in `asyncio.to_thread`.

**Repo (async, first arg `session`):** `insert_source(...)`, `get_source(id)`, `get_source_by_hash(sha256)`, `list_sources()` (with counts), `update_source(id, fields)`, `delete_source(id)` (cascades), `insert_questions(source_id, rows)`, `delete_questions(source_id)`, `list_questions(source_id, subject, level, approved, limit, offset) -> (rows, total)`, `counts(source_id) -> {subject: {level: {total, approved}}}`, `get_question(id)`, `update_question(id, fields)`, `set_approval(source_id, ids|None, subject|None, approved, only_clean)`, `set_embedding_status(id, status)`, `upsert_embeddings(rows)`, `search_questions(track, levels, subjects, query_vec|None, per_cell, ...)`.

- [ ] **Step 1: Failing tests:** SQL-shape assertions (recording fake session): `test_insert_questions_casts_enum_and_jsonb`, `test_list_questions_filters_and_paginates`, `test_delete_source_cascades_only_via_fk`, `test_search_uses_vector_operator_only_with_query_vec`, `test_search_filters_active_approved_track`; storage: `test_save_read_delete_roundtrip(tmp_path)`, `test_rejects_traversal_keys`, `test_read_missing_returns_none`. `alembic heads` shows single head `0003` without applying.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.

### Task 4: Question-bank service and router (CRUD)

**Files:** Create `app/qbank/schemas.py`, `service.py`, `router.py`; Modify `app/main.py`; Test `tests/qbank/test_api.py` (pattern of `tests/papers/test_api.py`).

Routes under `/api/v1/question-banks` (all `require_role("hr")`): `POST ""` (multipart `file`, `name`, `track`, `notes`; **201** `SourceOut`), `GET ""`, `GET "/{id}"` (detail: counts by subject/level incl. approved, report, embedding_status), `GET "/{id}/questions"` (`subject, level, approved, page, page_size≤100`), `PATCH "/{id}"` (`name, track, notes, is_active`), `PUT "/{id}/file"` (replace: re-parse, replaces all its questions, resets approval and embeddings), `DELETE "/{id}"` (204), `GET "/{id}/file"` (download original), `PATCH "/{id}/questions/{qid}"` (`QuestionEdit` from papers.schemas), `PUT "/{id}/approval"` (`{approved: bool, scope: "all_clean"|"subject"|"ids", subject?, question_ids?}`). Upload flow: validate size/magic/pages → sha256 duplicate → **409 `DUPLICATE_FILE`** (details name the existing bank) → `to_thread(extract_pages)` + `parse_pages` → zero questions ⇒ **422 `NO_QUESTIONS_FOUND`** (nothing saved) → save file → one transaction inserts source + questions (`approved=false`) + audit → commit → schedule embedding as a BackgroundTask (Task 5). Mutations other than create require `uploader == actor or actor.role == "super_admin"` else **403 `FORBIDDEN`**; missing/other → 404 `NOT_FOUND`. Row lock (`for update`) on replace/delete/approve.

- [ ] **Step 1: Failing tests:** `test_upload_parses_and_stores_unapproved`, `test_upload_rejects_non_pdf_oversize_and_too_many_pages`, `test_duplicate_hash_conflicts`, `test_no_questions_found_saves_nothing`, `test_list_and_detail_counts`, `test_questions_pagination_and_filters`, `test_patch_metadata_and_deactivate`, `test_replace_reparses_and_resets_approval`, `test_delete_removes_file_and_rows`, `test_only_uploader_or_super_admin_mutates`, `test_other_hr_can_view_and_download`, `test_edit_question_validates_and_audits`, `test_approval_scopes_all_clean_subject_ids`, `test_every_mutation_audited`, `test_unauthenticated_401_and_non_staff_403`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** full suite + ruff green.

### Task 5: Embeddings and retrieval (RAG)

**Files:** Create `app/qbank/embeddings.py`, `app/qbank/retrieval.py`; Modify `app/core/config.py` (`gemini_embedding_model`, `embedding_dim=768`), `.env.example`; Test `tests/qbank/test_embeddings.py`, `test_retrieval.py`.

Look up the current Gemini embedding model id and batch endpoint from Google's docs at implementation time (do not guess). **Produces:**
```python
class Embedder(Protocol): async def embed(self, texts: Sequence[str], *, task: Literal["document","query"]) -> list[list[float]]
class GeminiEmbedder(_HttpJsonClient-based)     # batches of <= 100, retry/backoff like agents, key via header, never logged
async def embed_source(source_id: UUID, embedder: Embedder) -> None     # own session; sets embedding_status done|failed; skipped when no key
@dataclass(frozen=True) class BankQuestion: id: str; subject: str; topic: str; level: Level; text: str; options: list[str]; answer: int; why: str
async def retrieve_technical(session, *, track: Track, per_level: Mapping[Level,int], subjects: Mapping[str,int] | None, focus: str | None, embedder: Embedder | None) -> list[BankQuestion]
```
`retrieve_technical`: approved + active sources + track only; per level take `per_level[level]` questions split by `subjects` weights (largest remainder; equal split when None); when `focus` is given **and** embeddings exist, rank candidates within each cell by cosine distance to the embedded focus text, else deterministic order; never invent, report shortfall as `{level/subject: missing}`.

- [ ] **Step 1: Failing tests (fake Embedder, no network):** `test_batches_of_100`, `test_key_never_in_errors_or_logs`, `test_embed_source_marks_done`, `test_embed_source_failure_marks_failed_not_raised`, `test_no_key_marks_skipped`, `test_retrieve_respects_level_subject_counts`, `test_retrieve_excludes_unapproved_inactive_other_track`, `test_focus_ranks_by_similarity_when_embeddings_exist`, `test_focus_ignored_with_explicit_flag_when_no_embeddings`, `test_shortfall_reported_not_invented`.
- [ ] **Step 2:** FAIL. **Step 3:** Implement. **Step 4:** PASS.

### Task 6: Paper pipeline integration + LLM composer

**Files:** Create `app/papers/pool.py`; Modify `app/papers/schemas.py` (`PaperSettings.subjects: dict[str,int] | None = None`, `PaperSettings.focus: str | None = Field(default=None, max_length=200)`; `PoolQuestion.source_id: str | None = None`), `app/papers/agents.py` (`compose_selection`, optional `sections` filter), `app/papers/service.py` (`build_pool` delegates to `pool.build_pool`, passes the session), `app/papers/router.py`/`service.ai_status` (only Gemini required for IT), `docs/04`; Test `tests/papers/test_pool.py`, `test_compose.py`, update affected existing tests.

**Produces:**
```python
async def build_pool(session, settings, *, on_progress, overrides, embedder=None, composer_client=None) -> list[PoolQuestion]
async def compose_selection(client: LlmClient, candidates: Sequence[PoolQuestion], target: Mapping[tuple[Level, Section], int]) -> set[str]
```
Pipeline (IT): (1) `generate_questions` for aptitude+reasoning at `needed(settings) × (pool_factor+1)` per cell (instant, seeded by draft id); (2) `retrieve_technical` for the Technical cells at the same oversampling, subjects/focus from settings, converting to `PoolQuestion` with **new uuid5 ids** (`uuid5(draft, bank_id)`), `source_id=bank_id`, `style="concept"`; a retrieval shortfall ⇒ `AppError(422,"BANK_TOO_SMALL", details missing)` telling HR to upload/approve more; (3) **one Gemini call** sends compact candidates (id, level, section, topic, text ≤ 200 chars, no answers) and asks for the ids to keep, `needed × pool_factor` per cell, covering topics evenly and avoiding near-duplicates; reply validated by code: ids ⊆ candidates, exact per-cell counts, unique — else one retry, then `AppError(502,"AI_COMPOSE_INVALID")`; unselected candidates stay in the pool with `selected=False` (HR can add them back in the existing picker). Non-IT: aptitude/reasoning via generators, Technical via existing `generate_pool(..., sections=("technical",))`, then the same composer. Progress reported in coarse steps (generators → retrieval → compose → done) monotonic. Existing per-student builder is unchanged (seeded draw from `selected`, question + option shuffle).

- [ ] **Step 1: Failing tests (fakes only):** `test_it_pool_has_expected_cells_and_sections`, `test_technical_ids_are_new_per_draft_and_keep_source_id`, `test_bank_too_small_raises_with_details`, `test_composer_receives_no_answers_or_why` (inspect the prompt), `test_composer_invalid_ids_retry_then_error`, `test_composer_wrong_counts_rejected`, `test_unselected_candidates_remain_in_pool`, `test_progress_monotonic`, `test_non_it_uses_llm_for_technical_only`, `test_missing_gemini_key_not_configured`, `test_edit_in_draft_does_not_touch_bank_row`, `test_send_persists_bank_derived_questions_with_source_tag` (existing persist path), `test_students_get_different_papers_from_selected_pool` (builder with 2 candidate refs).
- [ ] **Step 2:** FAIL. **Step 3:** Implement; keep the old LLM generation code for Non-IT; update docs/04 (new settings fields, errors `BANK_TOO_SMALL`, `AI_COMPOSE_INVALID`). **Step 4:** full suite + ruff green.

### Task 7: Backend docs

- [ ] **Step 1:** `docs/04-api-contracts.md`: new "Question banks" section (every route, body, response, errors `DUPLICATE_FILE`, `NO_QUESTIONS_FOUND`, `PDF_UNREADABLE`, `FORBIDDEN`, `NOT_FOUND`, `FILE_TOO_LARGE`, `NOT_A_PDF`, `TOO_MANY_PAGES`) + screen map row. **Step 2:** `docs/03` mirrors 0003. **Step 3:** add this plan to `docs/05` one line. **Step 4:** full backend suite, ruff check + format green.

### Task 8: Frontend API layer, nav, hooks

**Files:** `src/api/qbank.ts`, `src/features/qbank/types.ts`, `src/lib/nav.ts` (add "PDF question banks" `/hr/question-banks`, icon `Library`), tests. Multipart upload helper that reuses `api()` auth (do not set Content-Type; let the browser add the boundary). camelCase mapping in one place like `src/api/papers.ts`. Tests: `maps snake_case`, `upload sends FormData without JSON content-type`, `surfaces ApiError code/message`, `nav shows entry for HR and Super Admin`.

### Task 9: Bank list, upload, replace, deactivate, delete (`/hr/question-banks`)

Use `ui-ux-pro-max`. Page: header with "Upload PDF"; stat strip (banks, approved questions, subjects); cards or table per bank: name, track chip, status, `is_active` toggle, questions (approved/total), levels mini-bars, embedding status chip with plain explanation, uploader, date, actions (Open, Download, Replace file, Delete with confirm dialog that states questions inside already-sent papers are unaffected). Upload dialog: drag-and-drop + file picker, name, track (IT / Non-IT), notes, client-side checks (PDF type, ≤ 20 MB), progress and error states mapped from API codes (`DUPLICATE_FILE` links to the existing bank). Actions hidden/disabled for non-owners (UI convenience only). States: loading skeleton, empty ("Upload your first question bank"), error + retry, prototype-mode permission state. Tests for each state, owner-only actions, delete confirm, upload validation.

### Task 10: Bank detail — parse report, questions, approve, edit (`/hr/question-banks/[id]`)

Breadcrumbs; summary (counts by subject × level with approved ratio), **parse report** panel (warnings and skipped items readable, not raw JSON), filters (subject, level, approved), paginated question list reusing the paper question card styling with correct-answer marker, bulk actions "Approve all clean", "Approve subject", "Unapprove", per-question Edit dialog (same fields/validation as the paper edit dialog), embedding-status banner. Tests: filters, approve scopes call the right payloads, edit validation, pagination, non-owner read-only.

### Task 11: Paper setup additions

Modify only the papers setup components: for IT show **bank availability** (approved questions by level and subject from a new lightweight call `GET /question-banks/availability?track=` — add to Task 4 if missing, documented), **subject mix** sliders for the six subjects (sum 100, "Even split" default), optional **focus** text (≤ 200 chars) with hint that it steers retrieval when embeddings exist; block Create with a clear message when approved questions are insufficient; Non-IT keeps the current setup. Update copy: "Aptitude and Reasoning are generated by code; Technical comes from your PDF bank; AI picks the sample paper." Tests for the new controls and the insufficient-bank message.

### Task 12: Whole-feature verification

- [ ] Backend `.\.venv\Scripts\python.exe -m pytest -q`, `ruff check .`, `ruff format --check .`, `alembic heads` (single head `0003`, not applied). Frontend `npm run lint`, `npm run typecheck`, `npm test`, `npm run build`. Paste summary lines.
- [ ] If the user provides the real PDF path (env `QBANK_SAMPLE_PDF`), run the real-PDF test and report exact counts; otherwise state it was verified only on text fixtures.
- [ ] `git status`/`git diff --stat` in both repos: list changed files; **nothing committed or migrated**; the user must apply `0003` (enables `vector`) and add `GEMINI_API_KEY`; Gemini embedding model id and dimension to be confirmed against a real call.
