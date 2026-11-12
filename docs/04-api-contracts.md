# 04 — API Contracts (REST, `/api/v1`)

FastAPI serves the live OpenAPI at `/docs`; **that is the authoritative contract once code exists**. This file is the plan and the review checklist. Error shape and pagination: see `02-architecture.md`.

Auth column: **S** = student attempt token, **HR** = HR or Super Admin (HR limited to own drives), **SA** = Super Admin only, **—** = public.

## Auth & student entry
| Method | Path | Auth | Body → Response |
|---|---|---|---|
| GET  | `/me` | HR | → `{id, email, full_name, role}` |
| POST | `/entry/verify` | — | `{student_id, dob}` → `{entry_token, candidate:{full_name}, rounds:[{round_id, type, test_id}]}` · 401 `ENTRY_MISMATCH` (logged) |
| POST | `/entry/steps` | entry | `{round_id, step:"location"\|"terms"\|"fullscreen", ok, meta}` → 204 |
| POST | `/entry/start` | entry | → `{attempt_token, attempt_id}` · 409 if location/terms missing |

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

`settings` (PaperSettings): `{track, counts:{easy,medium,advanced}, mix:{aptitude,reasoning,technical}, it_syntax_percent=50, pool_factor=2 (1-3), duration_min=60 (5-240), different_per_student=true, shuffle_questions=true, shuffle_options=true}`. `counts` needs all three keys, each 0-100, total ≥ 1; `mix` needs all three keys, each 0-100, summing to 100.

| Method | Path | Auth | Body → Response |
|---|---|---|---|
| GET | `/papers/ai-status` | HR | → `{gemini: bool, groq: bool}` (is each API key configured; never returns keys) |
| POST | `/papers/drafts` | HR | `{drive_ref, title, settings}` → **201** `DraftOut` with `status:"generating"`; starts background generation (Gemini generates, Groq verifies) · 503 `AI_PROVIDER_NOT_CONFIGURED` (`details.providers` = missing) · 422 `VALIDATION_ERROR` (bad mix/counts, blank `drive_ref`/`title`) |
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
| 503 | `AI_PROVIDER_NOT_CONFIGURED` | `POST /drafts` when a Gemini or Groq key is missing |

Generation failures are not HTTP errors: the background job sets `status:"failed"` and stores a user-safe message in `error`. Those messages come from `AI_PROVIDER_NOT_CONFIGURED` (a key removed mid-run), `AI_PROVIDER_ERROR` (502 equivalent: provider outage after 3 short retries), `AI_PROVIDER_RATE_LIMITED` (429 equivalent: still rate-limited after 6 tries that wait as long as the provider asks — `Retry-After` or Gemini's body `retryDelay`, max 60 s each), `AI_PROVIDER_QUOTA_EXHAUSTED` (429 equivalent: per-day quota used up; fails at once) or `POOL_TOO_SMALL` (422 equivalent: too few generated questions passed Groq verification), or a generic "Generation failed. Try again." for anything else.

## Frontend screen → endpoint map
| Stitch screen | Endpoints |
|---|---|
| login | Supabase Auth, `/me` |
| hr_dashboard, drive_pipeline | `/drives`, `/drives/{id}/pipeline` |
| create_campus_drive_wizard | `POST /drives`, `/drives/{id}/rounds`, `PUT /rounds/{id}/test` |
| drive_registration | `/candidates/import`, `/resumes`, `/candidates`, `/shortlist` |
| question_bank_ai_generator | `/questions*` |
| round1_paper_builder (`/hr/papers`: Setup → Generate → Review → Send) | `/papers/ai-status`, `POST /papers/drafts`, `/papers/drafts`, `/papers/drafts/{id}`, `PATCH /papers/drafts/{id}/questions/{qid}`, `PUT /papers/drafts/{id}/selection`, `/papers/drafts/{id}/accept`, `/papers/drafts/{id}/send` |
| system_check_proctor_diagnostic | `/entry/*` |
| student_aptitude_test, student_coding_test | `/attempt*` |
| candidate_submission_result, offer_results_student_portal, candidate_dashboard | `/me/status` |
| candidate_evaluation_coding_result, proctoring_report | `/tests/{id}/attempts/{aid}`, `/tests/{id}/alerts` |
| interviewer_console, candidate_round_3_interview_details | `/rounds/{id}/interviews`, `/interviews/{id}` |
| final_ranking_offers | `/tests/{id}/dashboard`, `PATCH /rounds/{id}/candidates/{cid}` |
| super_admin_dashboard, users_roles, ai_settings, audit_logs, partner_colleges | `/admin/*` (partner colleges = distinct `drives.campus`) |
