# 04 — API Contracts (REST, `/api/v1`)

FastAPI serves the live OpenAPI at `/docs`; **that is the authoritative contract once code exists**. This file is the plan and the review checklist. Error shape and pagination: see `02-architecture.md`.

Auth column: **S** = student attempt token, **HR** = HR or Super Admin (HR limited to own drives), **SA** = Super Admin only, **—** = public.

## Auth & student entry
| Method | Path | Auth | Body → Response |
|---|---|---|---|
| POST | `/auth/login` | — | `{email, password}` → `{access_token, expires_in:28800, user:{id, email, full_name, role}}` · staff token: HS256 `typ:"staff"`, 8 h, sent as `Authorization: Bearer` · 401 `INVALID_CREDENTIALS` (same for unknown email, wrong password, inactive, password never set) · 429 `TOO_MANY_ATTEMPTS` (5 wrong passwords in a row → locked 15 min) |
| POST | `/auth/forgot-password` | — | `{email}` → 204 always · emails a 1 h set-password link in the background only to an active account, so neither status nor timing reveals accounts |
| POST | `/auth/set-password` | — | `{token, password}` → 204 · token from an invite (72 h) or reset (1 h) link `FRONTEND_URL/set-password?token=…`; works once (it carries the password version) · 400 `LINK_INVALID` (expired, used, wrong type, inactive user) · 422 `WEAK_PASSWORD` (< 10 chars; link stays valid) |
| GET  | `/me` | HR | → `{id, email, full_name, role}` |
| POST | `/entry/verify` | — | `{student_id, dob}` → `{entry_token, candidate:{full_name}, rounds:[{round_id, type, test_id}]}` · 401 `ENTRY_MISMATCH` (logged) |
| POST | `/entry/steps` | entry | `{round_id, step:"location"\|"terms"\|"fullscreen", ok, meta}` → 204 |
| POST | `/entry/start` | entry | `{round_id}` → `{attempt_token, attempt_id}` · creates the attempt on the first call (`status='in_progress'`, `started_at=now()`, `deadline_at=started_at+test duration`); a repeat call on a live attempt returns the same attempt and never moves the deadline · attempt token: `typ:"attempt"`, claims `cid` + `aid`, 2 h; an entry token is not accepted as an attempt token and the reverse · 403 `NOT_INVITED` (not in that round) · 409 `ROUND_NOT_OPEN` (invited, but the round can't be sat now) · 409 `STEPS_MISSING` (location/terms not logged ok; `details.missing`) · 409 `PAPER_NOT_READY` (aptitude/technical: no accepted paper was sent to this student; coding: the round's test has no coding questions) — the attempt is rolled back · 409 `ATTEMPT_LOCKED` (attempt submitted or locked) · 409 `ATTEMPT_EXPIRED` (expired, or past `deadline_at`); no token is issued for these |

## Drives & pipeline
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET/POST | `/drives` | HR | list own / create `{company, campus, role_title, jd_text, rounds?, owner_id?}`. `owner_id` is Super Admin only: the active HR who runs the drive (`422 INVALID_OWNER` otherwise; HR sending someone else's id gets `403`). Omitted = the caller owns it. Each drive returns `owner_id`, `owner_name`. |
| GET/PATCH | `/drives/{id}` | HR | |
| PATCH | `/drives/{id}/owner` | SA | `{owner_id}` → hand the drive to another active HR (`422 INVALID_OWNER`, `404 DRIVE_NOT_FOUND`); audited `drive.reassign {from, to}`; returns the drive |
| GET | `/drives/{id}/pipeline` | HR | rounds with counts per `rc_status` |
| POST | `/drives/{id}/rounds` | HR | `{type, mode, position}` — add technical round |
| PATCH | `/rounds/{id}` | HR | `{status:"skipped"}`, `{mode}`, `{threshold}` |
| GET  | `/rounds/{id}/ranking?threshold=` | HR | preview for the cut-off slider (`threshold` = percent 0-100; omitted → the round's saved `threshold`, else none) → `{round_id, round_status, threshold\|null, counts:{pass\|null, fail\|null, absent, still_writing, locked}, candidates:[{candidate_id, student_id, name, attempt_status\|null, marks\|null, max_marks\|null, percent\|null, rank\|null, time_taken_sec\|null, would_pass\|null}]}` · `percent` = marks ÷ that student's own max marks × 100 (2 decimals; max 0 → 0); `rank` 1..n over finished attempts by percent desc, then `time_taken_sec` asc; `would_pass` = `percent >= threshold` (equal passes), `false` for absent, `null` while still writing / locked or with no threshold; `pass`/`fail` are `null` with no threshold · finished attempts first by rank, then still writing, locked, absent · on an active round, attempts past their deadline are auto-submitted first (same as `GET /attempt`), so the preview is exact · 404 `ROUND_NOT_FOUND` (also other HR's rounds) · 409 `ROUND_HAS_NO_TEST` (not aptitude/coding/technical) |
| POST | `/rounds/{id}/advance` | HR | `{threshold (0-100, percent), finalize_locked?=false, mark_absent?=false}` → `{round_id, threshold, passed, failed, absent, next_round_id\|null, invited:[{candidate_id, student_id, name}]}` · one transaction: auto-submits attempts past their deadline; saves `rounds.threshold`; sets each `round_candidates` `score` (percent), `rank` and `status` `passed` (percent ≥ threshold) / `failed` / `absent` (never attempted); this round → `completed`; the next round by position that isn't `skipped` → `active`, with the passed students inserted as `invited` (existing rows untouched; `invited` lists the new rows); no next round → just finishes · after commit, each newly invited student gets a background email (sign-in link only; skipped when SMTP isn't configured; failures logged, never undo the action) · audited `round.advance` · `finalize_locked:true` submits tab-switch-locked attempts with the marks they have · 404 `ROUND_NOT_FOUND` (also other HR's rounds) · 409 `ROUND_ALREADY_ADVANCED` (completed) / `ROUND_NOT_ACTIVE` (pending/skipped) / `ROUND_HAS_NO_TEST` · 409 `ROUND_STILL_RUNNING` (an attempt is in progress before its deadline; `details.counts`) · 409 `LOCKED_ATTEMPTS` (locked attempts and no `finalize_locked`; `details.attempts:[{attempt_id, candidate_id, student_id, name}]`) · 409 `NEXT_ROUND_NOT_READY` (the next round is a coding round with no coding questions on its test; `details.next_round_id`) · 409 `NOT_STARTED` (students who never started and no `mark_absent`; `details.counts`) — so a round advanced right after the paper is sent never fails everyone by accident · nothing is saved on any refusal |
| GET  | `/rounds/{id}/decisions` | HR | interview / final round list for HR's decision → `{round_id, round_type, round_status, candidates:[{candidate_id, student_id, name, status, interview_id\|null, interview_status\|null, scoring (pending\|done\|error\|null), ai_average\|null}]}` · `ai_average` = mean of the four AI interview scores (1-5), a recommendation only · sorted by name · 404 `ROUND_NOT_FOUND` (also other HR's rounds) · 409 `ROUND_NOT_HR_DECIDED` (test rounds use `/advance`) |
| POST | `/rounds/{id}/decide` | HR | `{decisions:[{candidate_id, decision: pass\|fail}]}` (exactly one per student in the round) → `{round_id, passed, failed, next_round_id\|null, invited:[{candidate_id, student_id, name}], drive_closed}` · HR is the finalizer: the AI score never decides · one transaction: each `round_candidates.status` → `passed`/`failed` (`ai_interview`) or `selected`/`rejected` (`final`), `score` = the AI average (record only); round → `completed`; next non-skipped round → `active` with the passed inserted as `invited`; no next round → drive `status = closed` · after commit, each newly invited student gets a background email (sign-in link only; skipped when SMTP isn't configured; failures logged, never undo the action) · audited `round.decide` · 404 `ROUND_NOT_FOUND` · 409 `ROUND_NOT_HR_DECIDED` / `ROUND_ALREADY_DECIDED` / `ROUND_NOT_ACTIVE` / `INTERVIEW_LIVE` (`details.candidates`) · 422 `DECISIONS_INCOMPLETE` (`details.missing`, `details.unknown`) / `DUPLICATE_DECISION` · nothing is saved on any refusal |
| POST | `/rounds/{id}/emails` | HR | `{template, candidate_ids?}` → `{queued}` |
| PATCH | `/rounds/{id}/candidates/{cid}` | HR | `{status, hr_note}` — manual rounds & final decision |

## Candidates & resumes
| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/drives/{id}/candidates/import` | HR | multipart `sheet` (.xlsx/.csv) → `{created, updated, errors:[{row, message}]}` |
| POST | `/drives/{id}/resumes` | HR | multipart PDFs or zip; matched by student_id in filename → parse in background |
| GET  | `/drives/{id}/candidates` | HR | `?skills=java,sql&min_cgpa=7&min_match=60&shortlisted=` paginated |
| POST | `/drives/{id}/shortlist` | HR | `{candidate_ids, shortlisted}` → `{updated, moved}` · shortlisting: after commit, each student newly invited to Round 1 gets a background email (sign-in link only; skipped when SMTP isn't configured; failures logged, never undo the action) |
| GET  | `/candidates/{id}` | HR | profile + resume fields + per-round history |

## Tests & questions
| Method | Path | Auth | Notes |
|---|---|---|---|
| PUT  | `/rounds/{id}/test` | HR | `{difficulty?, duration_sec? (60-36000), opens_at?, closes_at?}` (only sent fields change; `{}` is fine) → `{id, round_id, difficulty\|null, duration_sec, question_count, settings, opens_at\|null, closes_at\|null}` · creates the round's test row if it has none (`duration_sec` defaults to the round's `"Duration"` minutes × 60, else the standard default); the question list and `question_count` are set by `PUT /tests/{id}/questions` · audited `test.configure` · 404 `ROUND_NOT_FOUND` (also other HR's rounds) · 409 `ROUND_HAS_NO_TEST` (shortlist/interview/final rounds) · 422 `VALIDATION_ERROR` (`closes_at` not after `opens_at`, unknown field) |
| GET  | `/rounds/{id}/test` | HR | → `{id, round_id, difficulty\|null, duration_sec, question_count, settings, opens_at\|null, closes_at\|null, question_ids:[uuid], locked}` (same shape as the PUT plus two fields; `id` is the test id for `/tests/{id}/results` and `/tests/{id}/questions`) · `question_ids` in the order students see · `locked` = a student has started (then `PUT /tests/{id}/questions` answers `TEST_IN_USE`) · read only: never creates the test row, not audited · 404 `ROUND_NOT_FOUND` (also other HR's rounds) · 404 `TEST_NOT_FOUND` (no test set up yet: call the PUT) · 409 `ROUND_HAS_NO_TEST` (shortlist/interview/final rounds) |
| GET  | `/questions` | HR | `?kind&tags&difficulty&approved` · coding (the built part): `?kind=coding&difficulty&tags=a&tags=b&approved&limit=50 (1-200)` → `[{id, kind:"coding", body, difficulty, tags, marks, approved, created_at}]`, newest first, no test cases; HR sees own, Super Admin all · 422 `UNSUPPORTED_KIND` (any other `kind`) |
| POST | `/questions` | HR | manual create (coding incl. `test_cases[]`, must be 3 sample + 15 hidden) · coding: `{kind:"coding", body (title = first line), difficulty (easy\|medium\|advanced), tags[] (≤ 10, ≤ 40 chars), marks (0-999, default 1), test_cases:[{input, expected (not blank), is_sample}]}` → **201** `CodingQuestion` = `{id, kind, body, difficulty, tags, marks, approved:true, created_by, created_at, reference_solution\|null, reference_language\|null, test_cases:[{id, input, expected, is_sample, position}]}` (HR/SA only: it carries the hidden cases and, for AI questions, the reference solution; both `reference_*` are null for manual questions and are not editable through PATCH) · audited `question.create` (counts only) · 422 `VALIDATION_ERROR` (not 3 + 15 cases, blank body/expected, > 20000 chars) |
| GET  | `/questions/{id}` | HR | coding → `CodingQuestion` · 404 `QUESTION_NOT_FOUND` (also other HR's questions) |
| POST | `/questions/generate` | HR | coding AI drafts (synchronous, about 11 s per question measured; 150 s budget for the whole request): `{difficulty (easy\|medium\|advanced), topic? (≤ 200 chars), tags[]? (≤ 10, ≤ 40 chars), marks? (0-999, default 1), count? (1-3, default 1)}` → **201** `{created:[CodingQuestion], failed:[{index, reason}]}`. The LLM (NVIDIA, as Round 1) writes title, statement, a Python 3.8 reference solution and 3 + 15 inputs (each ≤ 2000 chars); the reply is schema-validated, then the reference solution runs on Judge0 for all 18 inputs and **its stdout is `expected`** (never the LLM's). A question whose reply is invalid or whose solution fails (compile/runtime error, > 2.5 s, blank output, same output for every input) is retried once with the error fed back, then listed in `failed`. Saved `approved:false`, `ai_generated:true`, `body = title + "\n" + statement`, with `reference_solution` + `reference_language:"python"`; HR reviews, edits and approves with `PATCH approved:true`. The prompt lists the caller's 30 newest coding titles as "do not repeat"; a repeated title is a failure · each saved question audited `question.generate` (counts only) in its own transaction · 503 `AI_PROVIDER_NOT_CONFIGURED` (no `NVIDIA_API_KEY`) · 503 `EXECUTION_UNAVAILABLE` (Judge0 down; nothing saved) · 502 `AI_BAD_OUTPUT` (every question failed; `details.failed`) · 502 `AI_PROVIDER_ERROR` / 429 `AI_PROVIDER_RATE_LIMITED` (the provider failed for every question) · 422 `VALIDATION_ERROR` |
| PATCH | `/questions/{id}` | HR | edit / approve / change `answer` → re-score affected attempts · coding: any of `{body, difficulty, tags, marks, approved, test_cases}` (sent fields may not be null; `test_cases` replaces the whole 3 + 15 set) → `CodingQuestion` · audited `question.update` · 404 `QUESTION_NOT_FOUND` · 409 `QUESTION_IN_USE` (new `test_cases` once any student has an answer row for it) |
| PUT  | `/tests/{id}/questions` | HR | `{question_ids}` · coding: `{question_ids:[uuid]}` (≤ 50, no repeats; order = order students see; `[]` clears) → `{test_id, question_count, levels:{easy, medium, advanced}, questions:[{question_id, position, difficulty, marks}]}`; a question's level is its own `difficulty` · audited `test.set_questions` (sending the current list again changes nothing) · 404 `TEST_NOT_FOUND` (also other HR's tests) · 409 `TEST_IN_USE` (a student has started) · 422 `QUESTIONS_NOT_FOUND` (not coding, not yours, or missing) / `QUESTIONS_NOT_APPROVED` / `QUESTIONS_INCOMPLETE` (not 3 + 15 cases); each lists `details.question_ids` |
| GET  | `/tests/{id}/results` | HR | coding results, read-only: `?limit=500 (1-1000)` → `{test_id, max_score, questions:[{question_id, position, difficulty, marks}], candidates:[{attempt_id, candidate_id, student_id, full_name, status, started_at, submitted_at, score, answers:[{question_id, submitted, language, passed, total, marks_earned}]}]}` · `status` reads `expired` once an `in_progress` attempt is past its deadline · `marks_earned` = `answers.marks` (= question marks × passed ÷ total, 2 places, set by submit-code), `score` = their sum (equals `attempts.marks` after the final submit when the test has only coding questions); decimals are strings · never code or case text · 404 `TEST_NOT_FOUND` |
| GET  | `/tests/{id}/results/{attempt_id}` | HR | → `{attempt_id, candidate_id, student_id, full_name, status, score, max_score, submissions:[ResultAnswer + {code, updated_at}]}`: the last SUBMITTED code per coding question (drafts are not shown) · 404 `TEST_NOT_FOUND` / `ATTEMPT_NOT_FOUND` (attempt of another test) |
| GET  | `/tests/{id}/dashboard` | HR | `{counts, verification_stats, ranking[], score_histogram}` |
| GET  | `/tests/{id}/attempts/{aid}` | HR | full answers with correctness |
| GET  | `/tests/{id}/alerts` | HR | locked attempts + violations |
| POST | `/attempts/{id}/resume` | HR | `{reason}` → reopens with remaining time, switches reset |

## Student test runtime
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET  | `/attempt` | S | `{status, deadline_at, server_now, questions:[no answers, sample cases only], saved_answers}` · coding questions: `{kind:"coding", id, title (first line of the body), statement, marks, languages:[{key, name}], samples:[{input, expected}] (≤ 3)}`; hidden cases never appear · `saved_answers[qid]` for coding = `{code, language}`, the last SUBMITTED code (a draft-only question is absent; drafts come from `GET /attempt/code-drafts`) |
| PUT  | `/attempt/answers/{qid}` | S | `{response}` or `{code, language}` — autosave, idempotent |
| POST | `/attempt/run` | S | `{question_id, code (1-64000 chars), language}` → `{results:[{case (1-based), passed, status, stdout (≤ 4000 chars), stderr, time\|null}]}` for the sample cases only; nothing is saved · `status`: `passed` \| `wrong_answer` \| `compile_error` \| `runtime_error` \| `time_limit`; `stderr` explains a failure (compiler output, program stderr, "Memory limit exceeded") · `language`: `python`, `javascript`, `c`, `cpp`, `java`, `go`, `sql` · 404 `QUESTION_NOT_FOUND` (not a coding question of this test) · 429 `RUN_IN_PROGRESS` (one run per candidate at a time) · 503 `EXECUTION_UNAVAILABLE` · 504 `EXECUTION_TIMEOUT` · 422 `VALIDATION_ERROR` |
| POST | `/attempt/submit-code` | S | `{question_id, code, language}` → `{passed, total:18}` (counts only, nothing about hidden cases) · saves `code`, `language`, the pass count and `answers.marks` = question marks × passed ÷ total (2 places, no level weighting); the latest submit wins, and the final submit adds it to `attempts.marks` · a Judge0 failure saves nothing · 409 `ATTEMPT_LOCKED`/`ATTEMPT_EXPIRED` also when the attempt was submitted while the code ran (nothing saved) · 409 `NO_TEST_CASES` · same 404/429/503/504/422 as run |
| PUT  | `/attempt/code-drafts/{qid}` | S | `{code (0-64000 chars), language}` → 204 · coding editor autosave (FR-17): sets only the draft; the submitted code, pass count and marks stay · 404 `QUESTION_NOT_FOUND` (not a coding question of this test) · 422 `VALIDATION_ERROR` |
| GET  | `/attempt/code-drafts` | S | → `{drafts:{qid:{code, language, saved_at}}}`: only drafts saved at or after that question's last submit, so a refresh restores the newest code (otherwise use `saved_answers`); readable in any attempt status |
| POST | `/attempt/violations` | S | `{kind}` → `{tab_switches, locked}` |
| POST | `/attempt/submit` | S | → `{status:"submitted"}`; also auto-run when `deadline_at` passes |
| GET  | `/me/status` | S | candidate's own round statuses |

All S endpoints return 409 `ATTEMPT_LOCKED` / `ATTEMPT_EXPIRED` when not `in_progress`.

## AI interview
Standalone entry: the student opens `/interview`, signs in with Student ID + DOB and gets an **interview token** (I) for that one interview. Audio goes browser ↔ Gemini Live directly with a one-use ephemeral token; the Gemini key and both prompts never leave the server (docs/09).

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/interview/ping` | public | 204, no DB. Timed by the page for the network check. |
| POST | `/interview/start` | public | `{student_id, dob, round_id?}` → `{interview_token, interview_id, status, candidate_name, role_title, company, duration_min, seconds_left}`. Only students with an open `round_candidates` row in an active `ai_interview` round; anyone else gets the same 401 as an unknown ID or wrong DOB. 5 misses lock 15 min. A successful sign-in sets the student's `round_candidates.status` `invited` → `in_progress`. |
| POST | `/interview/session` | I | Starts the clock on first call; mints a token (`uses: 1`, model + config incl. system instruction locked). → `{ephemeral_token, model, expires_at, deadline_at, server_now, seconds_left, resumed, role_title, candidate_name, client_config, protocol:{system_tag, typed_prefix, closing_phrase}}`. Call again to rejoin after a reload or when a reconnect needs a fresh token. 409 when completed; 503 `AI_NOT_CONFIGURED` without `GEMINI_API_KEY`. |
| POST | `/interview/transcript` | I | `{turns:[{role:"ai"\|"candidate", kind:"spoken"\|"typed"\|"question", text, language?, ts}], ended}`. Appends. `ended: true` completes the interview and queues scoring. |
| POST | `/interview/frame` | I | Body: one `image/jpeg`, max 100 KB (413 `FRAME_TOO_LARGE`). Held in memory only, never stored. |
| GET | `/interviews?round_id=&drive_id=` | HR | Rows: candidate, status, started/ended, turns, `scoring` (pending\|done\|error\|null), `average`, `has_notes`. Own drives only. |
| GET | `/interviews/{id}` | HR | Row + `transcript`, `ai_scores`, `hr_notes`, `skills`, `deadline_at`. |
| GET | `/interviews/{id}/frame` | HR | Latest camera still as `image/jpeg` if under 10 s old, else 404 `NO_FRAME`. `Cache-Control: no-store`. |
| POST | `/interviews/{id}/evaluate` | HR | 202 → `{scoring}`; re-runs scoring in the background. 409 if no transcript. Audited. |
| PATCH | `/interviews/{id}` | HR | `{hr_notes}` → detail. Audited. |

`ai_scores` (validated by `AiScores` before storing): `{communication, technical_depth, problem_solving, culture: 1-5 integers, sentiment:{overall: positive|neutral|negative, confidence: low|medium|high, note ≤300}, summary ≤1200, model, evaluated_at}`; `{pending: true}` while scoring; `{error: "<code>"}` when it failed twice. Advice for HR, not a decision.

## Super Admin
| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/admin/users` | SA | → `{items:[{id, email, full_name, role, is_active, created_at}], next_cursor:null}` |
| POST | `/admin/users` | SA | `{email, full_name, role:"hr"\|"super_admin"=hr, send_email=true}` → 201 `{...user, invite_link}` · creates the `users` row with no password; `send_email:true` emails a 72 h set-password link over SMTP (`invite_link:null`), `false` sends nothing and returns that one-time `invite_link` to share manually · 409 `USER_EXISTS` · 503 `EMAIL_NOT_CONFIGURED` / 502 `EMAIL_FAILED` (nothing created; retry or use `send_email:false`) |
| PATCH | `/admin/users/{id}` | SA | `{full_name?, role?, is_active?}` → user · 404 `USER_NOT_FOUND` · 409 `CANNOT_CHANGE_SELF` (own role/active) |
| DELETE | `/admin/users/{id}` | SA | → 204 · removes the `users` row · 409 `SUPER_ADMIN_PROTECTED` (Super Admins are deactivate-only) · 409 `USER_IN_USE` when drives/audit history/questions reference the user (deactivate instead) · 409 `CANNOT_CHANGE_SELF` · 404 |
| GET/PUT | `/admin/ai-prompts/{key}` | SA |
| GET/PUT | `/admin/settings` | SA |
| GET | `/admin/audit-logs` | SA |

## Papers (Round 1)
Prefix `/api/v1/papers`. All routes need **HR** (Super Admin passes). HR sees only drafts they own; Super Admin sees all. A draft owned by someone else returns the same `404 NOT_FOUND` as a missing one. Every mutation is audited. JSON is snake_case. Plan of record: `07-round1-paper-builder-implementation-plan.md`.

Enums: `track` = `it`\|`non_it`; level = `easy`\|`medium`\|`advanced`; section = `aptitude`\|`reasoning`\|`technical`; `status` = `generating`\|`draft`\|`accepted`\|`failed`.

`settings` (PaperSettings): `{track, counts:{easy,medium,advanced}, mix:{aptitude,reasoning,technical}, it_syntax_percent=50, pool_factor=2 (1-3), duration_min=60 (5-240), different_per_student=true, shuffle_questions=true, shuffle_options=true, role=null}`. `counts` needs all three keys, each 0-100, total ≥ 1; `mix` needs all three keys, each 0-100, summing to 100. `role` (≤ 120 chars, blank = null): the job the drive hires for, e.g. "Java Backend Developer"; the Technical questions are written for it. The UI requires it; it is optional in the API so older drafts still load. The former `subjects` and `focus` keys are ignored.

Pool pipeline: Aptitude and Reasoning are generated by code (computed answers). Technical (IT and Non-IT) is written for `role` by an NVIDIA model (`NVIDIA_API_KEY`), one call per batch of ≤ 8 questions; the question banks are not used and no model checks the answers — HR verifies and edits them in review. Every request waits for a shared slot (`NVIDIA_RPM`, default 30 a minute) so a paper stays under the free-tier limit. Aptitude and Reasoning cells hold `needed × (pool_factor+1)` candidates, of which one composer call marks `needed × pool_factor` `selected` (the rest stay in `pool` unselected for HR's picker); Technical cells hold `needed × pool_factor`, all selected.

| Method | Path | Auth | Body → Response |
|---|---|---|---|
| GET | `/papers/ai-status` | HR | → `{nvidia: bool}` (is the API key configured; never returns the key) |
| POST | `/papers/drafts` | HR | `{drive_ref, title, settings}` → **201** `DraftOut` with `status:"generating"`; starts background generation · 503 `AI_PROVIDER_NOT_CONFIGURED` (`details.providers` = `["nvidia"]`) · 422 `VALIDATION_ERROR` (bad mix/counts, blank `drive_ref`/`title`) |
| GET | `/papers/drafts` | HR | → `DraftOut[]` (HR: own, SA: all) |
| GET | `/papers/drafts/{id}` | HR | → `DraftDetail` · 404 `NOT_FOUND` |
| PATCH | `/papers/drafts/{id}/questions/{question_id}` | HR | `{text, options[4], answer (0-3), why}` → `DraftDetail` (checks re-run) · 409 `NOT_EDITABLE` (status is not `draft`) · 404 `NOT_FOUND` · 404 `QUESTION_NOT_FOUND` · 422 `VALIDATION_ERROR` (blank text/why, not exactly 4 options, blank or duplicate options (case-insensitive), answer outside 0-3) |
| PUT | `/papers/drafts/{id}/selection` | HR | `{selected_ids: [question id]}` (max 5000; duplicates ignored) → `DraftDetail`. HR picks which pool questions may reach students; every unlisted question is deselected. Papers, checks and send then use only selected questions · 409 `NOT_EDITABLE` · 404 `NOT_FOUND` · 422 `UNKNOWN_QUESTIONS` (`details.unknown`) |
| POST | `/papers/drafts/{id}/accept` | HR | → `DraftOut` (`status:"accepted"`, `accepted_at` set, pool frozen) · 409 `ALREADY_ACCEPTED` · 409 `NOT_READY` (still `generating` or `failed`) · 409 `CHECKS_FAILED` (`details.failed` = failed check ids) · 404 |
| POST | `/papers/drafts/{id}/send` | HR | `{candidate_refs: string[]}` (max 5000, each ≤ 200 chars) → `{sent, already_sent}` · newly sent candidates (UUID refs) get a background "Your Aptitude test is ready" email with the sign-in link for the drive's first test round (skipped without SMTP; failures logged, never undo the send) — a student can only start Round 1 once sent (`PAPER_NOT_READY` before) · 422 `NO_CANDIDATES` (empty after trimming and de-duplicating) · 409 `NOT_ACCEPTED` · 404 |

`DraftOut`: `{id, drive_ref, owner_id, title, track, status, settings, progress:{done,total}, error|null, pool_size, created_at, accepted_at|null, persisted_at|null}`. It never carries the question pool.

`DraftDetail` = `DraftOut` + `checks:[{id,label,ok,detail}]`, `sample:[KeyItem]` (the one review paper, built for candidate `"sample"`; empty while `generating`), `overlap:{shared,same_position,total}|null` (two sample students compared; null while generating or with no pool), `issues:[{candidate_ref, issued_at}]`, `edited_count`, `pool:[PoolItem]` (every generated question, original option order), `selected_count`. `PoolItem` = `KeyItem` + `selected` (default true). `KeyItem` = `{id, level, section, topic, text, options[], answer (index), why, fixed_options, edited}`. Answers appear only in these HR/SA responses, never in student-facing schemas.

Check ids: `settings_valid`, `pool_covers`, `no_repeats`, `answers_present`. (No "students get different papers" check: overlap between candidates is shown in `overlap`, never blocks accept.)

Behaviour notes
- **Progress polling.** `POST /drafts` returns immediately. Poll `GET /drafts/{id}` (about every 2-3 s) and read `progress.done / progress.total` while `status` is `generating`. Both are question counts (code-generated Aptitude/Reasoning plus Technical asked of the AI, its top-up reserve included), not a percentage; `total` is fixed from the first update and `done == total` while the final selection is composed. Stop on `draft` (ready to review), `failed` (show `error`; start a new draft) or `accepted`. A draft still `generating` 15 minutes after creation is reported as `failed` with `error:"Generation timed out"` (background jobs are lost on restart).
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
| 503 | `AI_PROVIDER_NOT_CONFIGURED` | `POST /drafts` when `NVIDIA_API_KEY` is missing |

Generation failures are not HTTP errors: the background job sets `status:"failed"` and stores a user-safe message in `error`. Those messages come from `AI_PROVIDER_NOT_CONFIGURED` (a key removed mid-run), `AI_PROVIDER_ERROR` (502 equivalent: provider outage after 3 short retries), `AI_PROVIDER_RATE_LIMITED` (429 equivalent: still rate-limited after 6 tries that wait as long as the provider asks — `Retry-After`, max 60 s each), `POOL_TOO_SMALL` (422 equivalent: the model returned too few usable questions), or `AI_COMPOSE_INVALID` (502 equivalent: the composer reply failed validation twice), or a generic "Generation failed. Try again." for anything else.

## Frontend screen → endpoint map
| Stitch screen | Endpoints |
|---|---|
| login | `POST /auth/login`, `/me`, `/auth/forgot-password`, `/auth/set-password` |
| hr_dashboard, drive_pipeline | `/drives`, `/drives/{id}/pipeline` |
| create_campus_drive_wizard | `POST /drives`, `/drives/{id}/rounds`, `PUT /rounds/{id}/test` |
| drive_registration | `/candidates/import`, `/resumes`, `/candidates`, `/shortlist` |
| question_bank_ai_generator | `/questions*` |
| round1_paper_builder (`/hr/papers`: Setup → Generate → Review → Send) | `/papers/ai-status`, `POST /papers/drafts`, `/papers/drafts`, `/papers/drafts/{id}`, `PATCH /papers/drafts/{id}/questions/{qid}`, `PUT /papers/drafts/{id}/selection`, `/papers/drafts/{id}/accept`, `/papers/drafts/{id}/send` |
| system_check_proctor_diagnostic | `/entry/*` |
| student_aptitude_test, student_coding_test | `/attempt*` |
| candidate_submission_result, offer_results_student_portal, candidate_dashboard | `/me/status` |
| candidate_evaluation_coding_result, proctoring_report | `/tests/{id}/attempts/{aid}`, `/tests/{id}/alerts` |
| interviewer_console, candidate_round_3_interview_details | `/interviews`, `/interviews/{id}`, `/interviews/{id}/frame` |
| final_ranking_offers | `/tests/{id}/dashboard`, `GET /rounds/{id}/decisions`, `POST /rounds/{id}/decide` |
| super_admin_dashboard, users_roles, ai_settings, audit_logs, partner_colleges | `/admin/*` (partner colleges = distinct `drives.campus`) |
