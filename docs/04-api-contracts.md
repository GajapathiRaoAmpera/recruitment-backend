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

## Frontend screen → endpoint map
| Stitch screen | Endpoints |
|---|---|
| login | Supabase Auth, `/me` |
| hr_dashboard, drive_pipeline | `/drives`, `/drives/{id}/pipeline` |
| create_campus_drive_wizard | `POST /drives`, `/drives/{id}/rounds`, `PUT /rounds/{id}/test` |
| drive_registration | `/candidates/import`, `/resumes`, `/candidates`, `/shortlist` |
| question_bank_ai_generator | `/questions*` |
| system_check_proctor_diagnostic | `/entry/*` |
| student_aptitude_test, student_coding_test | `/attempt*` |
| candidate_submission_result, offer_results_student_portal, candidate_dashboard | `/me/status` |
| candidate_evaluation_coding_result, proctoring_report | `/tests/{id}/attempts/{aid}`, `/tests/{id}/alerts` |
| interviewer_console, candidate_round_3_interview_details | `/rounds/{id}/interviews`, `/interviews/{id}` |
| final_ranking_offers | `/tests/{id}/dashboard`, `PATCH /rounds/{id}/candidates/{cid}` |
| super_admin_dashboard, users_roles, ai_settings, audit_logs, partner_colleges | `/admin/*` (partner colleges = distinct `drives.campus`) |
