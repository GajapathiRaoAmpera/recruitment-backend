# 04 — API Contracts (REST, `/api/v1`)

FastAPI serves the live OpenAPI at `/docs`; **that is the authoritative contract once code exists**. This file is the plan and the review checklist. Error shape and pagination: see `02-architecture.md`.

Auth column: **S** = student attempt token, **HR** = HR or Super Admin (HR limited to own drives), **SA** = Super Admin only, **—** = public.

## Auth & student entry
| Method | Path | Auth | Body → Response |
|---|---|---|---|
| GET  | `/me` | HR | → `{id, email, full_name, role}` |
| POST | `/entry/verify` | — | `{student_id, dob}` → `{entry_token, candidate:{full_name}, rounds:[{round_id, type, test_id}]}` · 401 `ENTRY_MISMATCH` (logged) |
| POST | `/entry/steps` | entry | `{round_id, step:"location"\|"terms"\|"fullscreen", ok, meta}` → 204 |
| POST | `/entry/start` | entry | `{round_id}` → `{attempt_token, attempt_id}` · creates the attempt on the first call (`status='in_progress'`, `started_at=now()`, `deadline_at=started_at+test duration`); a repeat call on a live attempt returns the same attempt and never moves the deadline · attempt token: `typ:"attempt"`, claims `cid` + `aid`, 2 h; an entry token is not accepted as an attempt token and the reverse · 403 `NOT_INVITED` (not in that round) · 409 `ROUND_NOT_OPEN` (invited, but the round can't be sat now) · 409 `STEPS_MISSING` (location/terms not logged ok; `details.missing`) · 409 `ATTEMPT_LOCKED` (attempt submitted or locked) · 409 `ATTEMPT_EXPIRED` (expired, or past `deadline_at`); no token is issued for these |

## Drives & pipeline
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET/POST | `/drives` | HR | list own / create `{company, campus, role_title, jd_text}` |
| GET/PATCH | `/drives/{id}` | HR | |
| GET | `/drives/{id}/pipeline` | HR | rounds with counts per `rc_status` |
| POST | `/drives/{id}/rounds` | HR | `{type, mode, position}` — add technical round |
| PATCH | `/rounds/{id}` | HR | `{status:"skipped"}`, `{mode}`, `{threshold}` |
| POST | `/rounds/{id}/advance` | HR | `{threshold}` → marks passed/failed, creates next round_candidates `{advanced, rejected}` |
| POST | `/rounds/{id}/emails` | HR | `{template, candidate_ids?}` → `{queued}` |
| PATCH | `/rounds/{id}/candidates/{cid}` | HR | `{status, hr_note}` — manual rounds & final decision |

## Candidates & resumes
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/drives/{id}/candidates/import` | HR | multipart `sheet` (.xlsx/.csv) → `{created, updated, errors:[{row, message}]}` |
| POST | `/drives/{id}/resumes` | HR | multipart PDFs or zip; matched by student_id in filename → parse in background |
| GET  | `/drives/{id}/candidates` | HR | `?skills=java,sql&min_cgpa=7&min_match=60&shortlisted=` paginated |
| POST | `/drives/{id}/shortlist` | HR | `{candidate_ids, shortlisted}` |
| GET  | `/candidates/{id}` | HR | profile + resume fields + per-round history |

## Tests & questions
| Method | Path | Auth | Notes |
|---|---|---|---|
| PUT  | `/rounds/{id}/test` | HR | `{difficulty, duration_sec, question_count, settings, opens_at, closes_at}` |
| GET  | `/questions` | HR | `?kind&tags&difficulty&approved` |
| POST | `/questions` | HR | manual create (coding incl. `test_cases[]`, must be 3 sample + 15 hidden) |
| POST | `/questions/generate` | HR | `{kind, tags, difficulty, count}` → draft questions `approved=false` |
| PATCH | `/questions/{id}` | HR | edit / approve / change `answer` → re-score affected attempts |
| PUT  | `/tests/{id}/questions` | HR | `{question_ids}` |
| GET  | `/tests/{id}/dashboard` | HR | `{counts, verification_stats, ranking[], score_histogram}` |
| GET  | `/tests/{id}/attempts/{aid}` | HR | full answers with correctness |
| GET  | `/tests/{id}/alerts` | HR | locked attempts + violations |
| POST | `/attempts/{id}/resume` | HR | `{reason}` → reopens with remaining time, switches reset |

## Student test runtime
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET  | `/attempt` | S | `{status, deadline_at, server_now, questions:[no answers, sample cases only], saved_answers}` |
| PUT  | `/attempt/answers/{qid}` | S | `{response}` or `{code, language}` — autosave, idempotent |
| POST | `/attempt/run` | S | `{question_id, code, language}` → `{results:[{case, passed, stdout, stderr, time}]}` sample only |
| POST | `/attempt/submit-code` | S | `{question_id, code, language}` → `{passed, total:18}` |
| POST | `/attempt/violations` | S | `{kind}` → `{tab_switches, locked}` |
| POST | `/attempt/submit` | S | → `{status:"submitted"}`; also auto-run when `deadline_at` passes |
| GET  | `/me/status` | S | candidate's own round statuses |

All S endpoints return 409 `ATTEMPT_LOCKED` / `ATTEMPT_EXPIRED` when not `in_progress`.

## AI interview
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/interview/session` | S | → `{ephemeral_token, model, expires_at}` (prompt from `ai_prompts`) |
| POST | `/interview/transcript` | S | `{turns:[{role, text, ts}], ended}` (append) |
| POST | `/interviews/{id}/evaluate` | HR | (re)run AI scoring → `ai_scores` |
| PATCH | `/interviews/{id}` | HR | `{hr_notes}` |
| GET  | `/rounds/{id}/interviews` | HR | list with scores |

## Super Admin
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/admin/users` | SA | → `{items:[{id, email, full_name, role, is_active, created_at}], next_cursor:null}` |
| POST | `/admin/users` | SA | `{email, full_name, role:"hr"\|"super_admin"=hr, send_email=true}` → 201 `{...user, invite_link}` · creates the Supabase Auth user; `send_email:true` emails the invite to `FRONTEND_URL/set-password` (`invite_link:null`), `false` sends nothing and returns the one-time `invite_link` to share manually · 409 `USER_EXISTS` · 429 `EMAIL_RATE_LIMIT` · 502 `AUTH_PROVIDER_ERROR` |
| PATCH | `/admin/users/{id}` | SA | `{full_name?, role?, is_active?}` → user · 404 `USER_NOT_FOUND` · 409 `CANNOT_CHANGE_SELF` (own role/active) |
| DELETE | `/admin/users/{id}` | SA | → 204 · removes the `users` row and the Supabase Auth account · 409 `SUPER_ADMIN_PROTECTED` (Super Admins are deactivate-only) · 409 `USER_IN_USE` when drives/audit history/questions reference the user (deactivate instead) · 409 `CANNOT_CHANGE_SELF` · 404 · 502 `AUTH_PROVIDER_ERROR` (nothing changed) |
| GET/PUT | `/admin/ai-prompts/{key}` | SA |
| GET/PUT | `/admin/settings` | SA |
| GET | `/admin/audit-logs` | SA |

## Papers (Round 1)
Prefix `/api/v1/papers`. All routes need **HR** (Super Admin passes). HR sees only drafts they own; Super Admin sees all. A draft owned by someone else returns the same `404 NOT_FOUND` as a missing one. Every mutation is audited. JSON is snake_case. Plan of record: `07-round1-paper-builder-implementation-plan.md`.

Enums: `track` = `it`\|`non_it`; level = `easy`\|`medium`\|`advanced`; section = `aptitude`\|`reasoning`\|`technical`; `status` = `generating`\|`draft`\|`accepted`\|`failed`.

`settings` (PaperSettings): `{track, counts:{easy,medium,advanced}, mix:{aptitude,reasoning,technical}, it_syntax_percent=50, pool_factor=2 (1-3), duration_min=60 (5-240), different_per_student=true, shuffle_questions=true, shuffle_options=true, subjects=null, focus=null}`. `counts` needs all three keys, each 0-100, total ≥ 1; `mix` needs all three keys, each 0-100, summing to 100. `subjects` (IT only): `{subject: weight}` for the Technical section from the PDF banks, each 0-100, summing to 100; `null` = even split over the subjects the banks have. `focus` (≤ 200 chars, blank = null): steers bank retrieval when embeddings exist.

Pool pipeline: Aptitude and Reasoning are generated by code (computed answers); IT Technical is copied from approved questions of active PDF banks (new id per draft, `source_id` = bank question, persisted with a `source:<id>` tag); Non-IT Technical is still generated by Gemini and verified by Groq. Each level/section cell holds `needed × (pool_factor+1)` candidates; one Gemini composer call marks `needed × pool_factor` per cell `selected`, the rest stay in `pool` unselected for HR's picker.

| Method | Path | Auth | Body → Response |
|---|---|---|---|
| GET | `/papers/ai-status` | HR | → `{gemini: bool, groq: bool}` (is each API key configured; never returns keys) |
| POST | `/papers/drafts` | HR | `{drive_ref, title, settings}` → **201** `DraftOut` with `status:"generating"`; starts background generation · 503 `AI_PROVIDER_NOT_CONFIGURED` (`details.providers` = missing; IT needs Gemini, Non-IT needs Gemini and Groq) · 422 `VALIDATION_ERROR` (bad mix/counts, blank `drive_ref`/`title`) |
| GET | `/papers/drafts` | HR | → `DraftOut[]` (HR: own, SA: all) |
| GET | `/papers/drafts/{id}` | HR | → `DraftDetail` · 404 `NOT_FOUND` |
| PATCH | `/papers/drafts/{id}/questions/{question_id}` | HR | `{text, options[4], answer (0-3), why}` → `DraftDetail` (checks re-run) · 409 `NOT_EDITABLE` (status is not `draft`) · 404 `NOT_FOUND` · 404 `QUESTION_NOT_FOUND` · 422 `VALIDATION_ERROR` (blank text/why, not exactly 4 options, blank or duplicate options (case-insensitive), answer outside 0-3) |
| PUT | `/papers/drafts/{id}/selection` | HR | `{selected_ids: [question id]}` (max 5000; duplicates ignored) → `DraftDetail`. HR picks which pool questions may reach students; every unlisted question is deselected. Papers, checks and send then use only selected questions · 409 `NOT_EDITABLE` · 404 `NOT_FOUND` · 422 `UNKNOWN_QUESTIONS` (`details.unknown`) |
| POST | `/papers/drafts/{id}/accept` | HR | → `DraftOut` (`status:"accepted"`, `accepted_at` set, pool frozen) · 409 `ALREADY_ACCEPTED` · 409 `NOT_READY` (still `generating` or `failed`) · 409 `CHECKS_FAILED` (`details.failed` = failed check ids) · 404 |
| POST | `/papers/drafts/{id}/send` | HR | `{candidate_refs: string[]}` (max 5000, each ≤ 200 chars) → `{sent, already_sent}` · 422 `NO_CANDIDATES` (empty after trimming and de-duplicating) · 409 `NOT_ACCEPTED` · 404 |

`DraftOut`: `{id, drive_ref, owner_id, title, track, status, settings, progress:{done,total}, error|null, pool_size, created_at, accepted_at|null, persisted_at|null}`. It never carries the question pool.

`DraftDetail` = `DraftOut` + `checks:[{id,label,ok,detail}]`, `sample:[KeyItem]` (the one review paper, built for candidate `"sample"`; empty while `generating`), `overlap:{shared,same_position,total}|null` (two sample students compared; null while generating or with no pool), `issues:[{candidate_ref, issued_at}]`, `edited_count`, `pool:[PoolItem]` (every generated question, original option order), `selected_count`. `PoolItem` = `KeyItem` + `selected` (default true). `KeyItem` = `{id, level, section, topic, text, options[], answer (index), why, fixed_options, edited}`. Answers appear only in these HR/SA responses, never in student-facing schemas.

Check ids: `settings_valid`, `pool_covers`, `no_repeats`, `answers_present`. (No "students get different papers" check: overlap between candidates is shown in `overlap`, never blocks accept.)

Behaviour notes
- **Progress polling.** `POST /drafts` returns immediately. Poll `GET /drafts/{id}` (about every 2-3 s) and read `progress.done / progress.total` while `status` is `generating`. Stop on `draft` (ready to review), `failed` (show `error`; start a new draft) or `accepted`. A draft still `generating` 15 minutes after creation is reported as `failed` with `error:"Generation timed out"` (background jobs are lost on restart).
- **One sample paper, editable.** Edits change the shared pool item, so they apply to every candidate whose subset contains it. Edits are allowed only while `status` is `draft`; the edited question is not re-verified by Groq.
- **Questions reach the `questions` table only on first send** (`persisted_at` is set then); sending also stores one `paper_issues` row per candidate (`candidate_ref`, seed). Re-sending the same `candidate_ref` is counted in `already_sent`, and a question is never written twice. There is no student exam flow yet, so students cannot open a sent paper; marking and results are not built.
- `drive_ref` and `candidate_ref` are opaque text, not foreign keys.

Errors (all `{"error":{code,message,details}}`)
| HTTP | Code | When |
|---|---|---|
| 401 / 403 | (auth errors) | missing or invalid token / role below HR |
| 404 | `NOT_FOUND` | draft missing or owned by another HR |
| 404 | `QUESTION_NOT_FOUND` | `question_id` not in this draft's pool |
| 409 | `NOT_EDITABLE` | edit when status is not `draft` |
| 409 | `ALREADY_ACCEPTED` | accept on an accepted draft |
| 409 | `NOT_READY` | accept while `generating` or `failed` |
| 409 | `CHECKS_FAILED` | accept with failed checks (`details.failed`) |
| 409 | `NOT_ACCEPTED` | send before accept |
| 422 | `UNKNOWN_QUESTIONS` | selection lists ids not in the pool (`details.unknown`) |
| 422 | `NO_CANDIDATES` | send with no usable `candidate_refs` |
| 422 | `VALIDATION_ERROR` | invalid body (settings, edit rules, missing `candidate_refs`) |
| 503 | `AI_PROVIDER_NOT_CONFIGURED` | `POST /drafts` when a required key is missing (IT: Gemini; Non-IT: Gemini + Groq) |

Generation failures are not HTTP errors: the background job sets `status:"failed"` and stores a user-safe message in `error`. Those messages come from `AI_PROVIDER_NOT_CONFIGURED` (a key removed mid-run), `AI_PROVIDER_ERROR` (502 equivalent: provider outage after 3 short retries), `AI_PROVIDER_RATE_LIMITED` (429 equivalent: still rate-limited after 6 tries that wait as long as the provider asks — `Retry-After` or Gemini's body `retryDelay`, max 60 s each), `AI_PROVIDER_QUOTA_EXHAUSTED` (429 equivalent: per-day quota used up; fails at once) `POOL_TOO_SMALL` (422 equivalent: too few generated questions passed Groq verification), `BANK_TOO_SMALL` (422 equivalent, IT: the banks lack approved Technical questions; `details.missing` = `{"<level>/technical": n}` — upload or approve more), or `AI_COMPOSE_INVALID` (502 equivalent: the composer reply failed validation twice), or a generic "Generation failed. Try again." for anything else.

## Question banks (PDF MCQ banks for IT Technical)
Prefix `/api/v1/question-banks`. All routes need **HR** (Super Admin passes). Every HR can list, view and download every bank and use its approved questions in papers; only the **uploader or a Super Admin** may change one (else `403 FORBIDDEN`; the `can_manage` flag on each bank tells the UI). Every mutation is audited (`question_bank.created|updated|replaced|deleted|question_edited|approval_changed`). Plan of record: `08-question-bank-rag-plan.md`. Migration `0004_question_bank` (needs the `vector` extension).

| Method | Path | Body → Response |
|---|---|---|
| POST | `/question-banks` | multipart `file` (PDF), `name` (1-200), `track` (`it`\|`non_it`), `notes?` (≤ 2000) → **201** `SourceOut`. Parsed at once; questions stored **unapproved**; embeddings follow in the background with the free local model all-MiniLM-L6-v2 (fastembed, no API key; `embedding_status` `pending` → `done`\|`failed` (e.g. the model could not be downloaded; retrieval then uses its deterministic order); `skipped` is not produced by the current server) · 413 `FILE_TOO_LARGE` (`details.max_mb`, default 20) · 422 `NOT_A_PDF` · 422 `PDF_UNREADABLE` (encrypted/corrupt/no pages, or parse > 60 s) · 422 `PDF_TOO_MANY_PAGES` (> 400 pages; `details.pages`, `details.max_pages`) · 422 `NO_QUESTIONS_FOUND` (nothing saved; `details.warnings`, `details.skipped`) · 409 `DUPLICATE_FILE` (same sha256; `details.existing_id`, `details.existing_name`) · 503 `PARSER_BUSY` (two PDFs already being read; retry) |
| GET | `/question-banks?track=&is_active=&page=1&page_size=50` (≤ 100) | → `{items: SourceOut[], total, page, page_size}` |
| GET | `/question-banks/availability?track=` | → `{track, subjects: {subject: {easy, medium, advanced}}, by_level: {easy, medium, advanced}, total}` — approved questions in active banks of that track (what a paper can draw on) |
| GET | `/question-banks/{id}` | → `SourceDetail` · 404 `NOT_FOUND` |
| GET | `/question-banks/{id}/questions?subject=&level=&approved=&page=1&page_size=50` (≤ 100) | → `{items: BankQuestionOut[], total, page, page_size}` |
| GET | `/question-banks/{id}/file` | → the original PDF (`application/pdf`, attachment; fetch with the bearer token) · 404 `FILE_NOT_FOUND` |
| PATCH | `/question-banks/{id}` | `{name?, track?, notes?, is_active?}` (at least one; only `notes` may be null) → `SourceDetail`. `is_active:false` hides the bank from new papers |
| PUT | `/question-banks/{id}/file` | multipart `file` → `SourceDetail`. Re-parses and replaces **all** its questions; approval and embeddings reset · same upload errors · 409 `BANK_IN_USE` (questions already used by issued tests: upload as a new bank instead) |
| DELETE | `/question-banks/{id}` | → 204 (file and questions removed; papers already sent keep their own copies) · 409 `BANK_IN_USE` (deactivate instead) |
| PATCH | `/question-banks/{id}/questions/{qid}` | `{text, options[4], answer (0-3), why}` (same rules as the paper edit) → `BankQuestionOut`. Clears the question's parse warnings; re-embedded at once, best effort (on failure its old vector is dropped and it ranks after embedded ones) · 404 `QUESTION_NOT_FOUND` |
| PUT | `/question-banks/{id}/approval` | `{approved, scope: "all_clean"\|"subject"\|"ids", subject?, question_ids?}` → `{changed, question_count, approved_count}`. `all_clean`: approving takes every question **without** parse warnings, unapproving takes all; `subject` needs `subject`; `ids` needs `question_ids` (≤ 5000) · 422 `UNKNOWN_SUBJECT` · 422 `UNKNOWN_QUESTIONS` (nothing changed; `details.requested`, `details.found`) |

`SourceOut`: `{id, name, filename, sha256, track, notes|null, status ("ready"|"failed"), is_active, uploaded_by, embedding_status, created_at, updated_at, question_count, approved_count, can_manage}` (the storage key never leaves the server). `SourceDetail` = `SourceOut` + `report: {total, by_subject, by_level, warnings[], skipped[]}` + `counts: {subject: {level: {total, approved}}}`. `BankQuestionOut`: `{id, source_id, subject|null, topic|null, level, text, options[], answer, explanation|null, approved, warnings[], source_number|null, created_at}` — answers appear only here (HR/SA), never to students.

Errors (all `{"error":{code,message,details}}`)
| HTTP | Code | When |
|---|---|---|
| 400 | `BAD_REQUEST` | invalid `Content-Length` header |
| 403 | `FORBIDDEN` | mutation by an HR who is not the uploader |
| 404 | `NOT_FOUND` / `QUESTION_NOT_FOUND` / `FILE_NOT_FOUND` | bank, question in that bank, or the stored PDF missing |
| 409 | `DUPLICATE_FILE` | the same file (sha256) is already a bank |
| 409 | `BANK_IN_USE` | delete/replace a bank whose questions issued tests use |
| 413 | `FILE_TOO_LARGE` | upload over `QBANK_MAX_MB` (checked on `Content-Length` before the body is read, and while reading) |
| 422 | `NOT_A_PDF` / `PDF_UNREADABLE` / `PDF_TOO_MANY_PAGES` / `NO_QUESTIONS_FOUND` | file is not a PDF / cannot be read in time or at all / over 400 pages / no question found |
| 422 | `UNKNOWN_SUBJECT` / `UNKNOWN_QUESTIONS` / `VALIDATION_ERROR` | approval scope does not match the bank / invalid body |
| 500 | `STORAGE_ERROR` | the file could not be saved or read on the server |
| 503 | `PARSER_BUSY` | both PDF parser slots are busy |

Deployment: the size check reads `Content-Length`; also cap the request body at the reverse proxy (e.g. `client_max_body_size 21m`), since chunked uploads are only stopped while being read.

## Frontend screen → endpoint map
| Stitch screen | Endpoints |
|---|---|
| login | Supabase Auth, `/me` |
| hr_dashboard, drive_pipeline | `/drives`, `/drives/{id}/pipeline` |
| create_campus_drive_wizard | `POST /drives`, `/drives/{id}/rounds`, `PUT /rounds/{id}/test` |
| drive_registration | `/candidates/import`, `/resumes`, `/candidates`, `/shortlist` |
| question_bank_ai_generator | `/questions*` |
| question_banks (`/hr/question-banks`, `/hr/question-banks/{id}`) | `/question-banks*` |
| round1_paper_builder (`/hr/papers`: Setup → Generate → Review → Send) | `/papers/ai-status`, `POST /papers/drafts`, `/papers/drafts`, `/papers/drafts/{id}`, `PATCH /papers/drafts/{id}/questions/{qid}`, `PUT /papers/drafts/{id}/selection`, `/papers/drafts/{id}/accept`, `/papers/drafts/{id}/send` |
| system_check_proctor_diagnostic | `/entry/*` |
| student_aptitude_test, student_coding_test | `/attempt*` |
| candidate_submission_result, offer_results_student_portal, candidate_dashboard | `/me/status` |
| candidate_evaluation_coding_result, proctoring_report | `/tests/{id}/attempts/{aid}`, `/tests/{id}/alerts` |
| interviewer_console, candidate_round_3_interview_details | `/rounds/{id}/interviews`, `/interviews/{id}` |
| final_ranking_offers | `/tests/{id}/dashboard`, `PATCH /rounds/{id}/candidates/{cid}` |
| super_admin_dashboard, users_roles, ai_settings, audit_logs, partner_colleges | `/admin/*` (partner colleges = distinct `drives.campus`) |
