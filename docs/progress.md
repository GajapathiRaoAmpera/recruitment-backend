# SDD ledger — plan: docs/07-round1-paper-builder-implementation-plan.md

Execution: subagent-driven. **No commits** (user instruction) — implementers leave changes in the working tree; reviewers read the listed files. Branch `feature/round1-paper-builder` in both repos.

## Baseline (before any change)
- Backend: `.venv\Scripts\python.exe -m pytest -q` → 53 passed; `ruff check .` → all checks passed. (`uv` is not installed; the repo `.venv` is used.)
- Frontend: `npm run typecheck` clean; `npm test` → 8 files, 32 tests passed.

## Pre-flight scan

| Pair / task | Produces → consumes | Finding |
|---|---|---|
| T1 → T2 | `PaperSettings`, `PoolQuestion`, `KeyItem`, `Check` → builder | consistent |
| T2 → T3 | `needed(settings)` → `generate_pool` batch planning and `POOL_TOO_SMALL` | consistent; T3 imports from `builder` |
| T1/T2/T3 → T4 | schemas, builder, agents → service | `DraftOut`, `DraftDetail`, `SendResult` are used by T4 but defined nowhere |
| T4 ↔ T6 | API JSON → TS types | field names must match (snake_case on the wire) |
| T3 config ↔ tests/conftest.py | new optional env fields | no clash; fields are optional |
| T5 docs ↔ T4 routes | routes documented | consistent once T4 done |
| T6–T10 frontend | each consumes `src/api/papers.ts` + `types.ts` | consistent |
| T0..T11 each | own text agrees with itself | T0 says "uv"; uv absent |

Rulings
- Ruling: use the repo `.venv` (`.venv\Scripts\python.exe -m pytest|ruff`) wherever the plan says `uv run` — uv is not installed — costs nothing if wrong.
- Ruling: Task 4 defines `DraftOut`, `DraftDetail`, `SendResult` in `app/papers/schemas.py` (plan omitted them); wire names snake_case — costs a small rename if wrong.
- Ruling: Task 0 is done by the controller (branches + baseline above), not a subagent — trivial.
- Ruling: no commits anywhere; review package = the file list in the implementer report plus `git status`/`git diff` — user instruction overrides the skill's commit steps.

## Tasks
- Task 0: complete (frontend branch created; baseline recorded; httpx 0.28.1 present)
- Task 1: complete (no commits by design; review clean; 10 tests pass). minor (deferred): tests check ValidationError only, no field matching; QuestionEdit mutates in after-validator (style)
- Task 2: fix round 1/5 dispatched (ruling: students_differ = shared<total per brief, not order-only; add id dedupe; drop no-op comprehension). minor (deferred): settings_valid check is vacuous for validated settings; tests never hit failing settings_valid
- Ruling: students_differ requires a different question SET (shared<total) — otherwise HR is not warned that a small pool gives every candidate the same questions — costs: pool_factor=1 with different_per_student blocks accept until HR raises the factor or turns it off
- Task 2: fix round 2/5 (id-dedupe test strengthened; mutation-checked). Task 2: complete (no commits; review clean after 2 rounds; 36 papers tests pass)
- Task 3: implementer DONE_WITH_CONCERNS (55 papers tests, 108 full suite); reviewing. Note: ruff 0.17 formats python blocks inside docs/*.md - plan doc 07 reformatted so make lint stays green
- Task 3: review ✅ spec; 1 Important (top-up makes progress fraction regress) -> fix round 1/5 dispatched. minor (deferred): default model ids unverified until real keys (gemini-3.6-flash, openai/gpt-oss-120b); verification inside a batch is serial (slow, ~8+ calls/batch); pydantic lax coercion of answer; Gemini uses generateContent+JSON mode (docs now lead with newer Interactions API)
- Ruling: keep the implementer's one top-up round in generate_pool (bounded, reduces spurious POOL_TOO_SMALL) - costs extra LLM calls/time in worst case
- Task 3: fix round 1/5 (progress fixed-total w/ reserve; failing-first test). Task 3: complete (no commits; 109 backend tests pass)
- Controller fix (user-reported Pylance error): builder.split_by_percent made generic over key type (no behaviour change); papers tests re-run
- Task 4: review spec ✅; 1 Important (concurrent edit/accept lost-update -> add row lock FOR UPDATE) + minors -> fix round 1/5 dispatched. minor (deferred): created_by = draft owner not sender; repo SQL only string-asserted - needs dev-DB smoke test after migration is applied (user must apply 0002)
- Ruling: accept Task 4 extras: NO_CANDIDATES 422, NOT_READY 409, QUESTION_NOT_FOUND; ai_prompts rows with prefix paper_ passed as prompt overrides - costs nothing; docs/04 left to Task 5
- Task 4: fix round 1/5 (row lock FOR UPDATE on edit/accept/send; late-job guard; cast kind). Task 4: complete (no commits; 159 backend tests; migration 0002 written NOT applied)
- Task 5: complete (docs 04/05/06 updated from the code; controller spot-checked routes vs docs). Backend tasks 0-5 done; frontend tasks 6-11 next
- Task 6: complete (no commits; review clean first pass; frontend 38 tests). minor (deferred): ApiError drops details (UI cannot read details.failed / details.providers); mutations throw raw TypeError on network failure (only useAsync wraps NETWORK_ERROR) - UI components must handle; useAsync keeps old error/data while reloading (check loading first); useDriveCandidates returns new array each render
