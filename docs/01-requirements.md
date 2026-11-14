# 01 — Requirements, Constraints, Acceptance Criteria

Source: *Smart Recruitment Platform: Project Documentation (Working Title)*, Nov 10 2026. UI source of truth: `recruitment-frontend/stitch_ampera_campus_recruitment_platform/` (product name in designs: **Ampera Campus Hire**).

## Goal
One web platform that runs a campus recruitment drive end to end (shortlist → Round 1 → Round 2 → AI interview → final decision) with HR in control of every stage.

## Roles
| Role | Login | Can |
|---|---|---|
| Student | No account. Emailed link + Student ID + DOB per round | Take rounds they are invited to, see own status |
| HR | Email + password (Supabase Auth) | Everything about drives they own: candidates, rounds, questions, thresholds, emails, alerts, resume access, final decision |
| Super Admin | Email + password, created directly in DB | Everything HR can + users/roles, AI prompts, AI settings, audit logs |

## Functional requirements
IDs are referenced from the plan, API contracts and tests.

### Intake & shortlisting
- **FR-1** HR creates a drive (company, campus, JD, role) via the wizard.
- **FR-2** HR uploads the Google Form Excel/CSV export and resumes (PDF, zip or per-row Drive links).
- **FR-3** Local resume analyser (no AI) extracts text, skills, CGPA.
- **FR-4** HR filters by skills, CGPA, JD-match score and shortlists.

### Round entry (every round)
- **FR-5** Student opens round link, enters Student ID + DOB; must match the form data.
- **FR-6** Student grants location and accepts terms; each step is logged to `verification_logs`.
- **FR-7** Test starts in full screen.

### Round 1 — aptitude/reasoning/technical MCQ
- **FR-8** HR sets difficulty, timer, question count, tags.
- **FR-9** Questions are AI-generated (Gemini), stored with tags, `ai_generated=true`, and need HR approval before use.
- **FR-10** HR views questions, each student's answers, results; filters by tag; corrects answer keys (re-scores affected attempts).
- **FR-11** Ranking by marks desc, then time taken asc.
- **FR-12** HR sets a cut-off and triggers advance emails.

### Round 2 — coding
- **FR-13** HR picks levels (easy/medium/advanced), count, complexity.
- **FR-14** Each question has 3 sample + 15 hidden test cases.
- **FR-15** Languages: SQL, Python, C, C++, Java, Go via Judge0.
- **FR-16** *Run* executes sample cases only; *Submit* runs all 18 and shows passed count (hidden case contents never sent to the browser).
- **FR-17** Timer and code drafts persist locally; students can switch questions and survive refresh/network loss.

### AI interview
- **FR-18** HR triggers interview emails.
- **FR-19** Interview runs on Gemini Live API, ends with a thank-you.
- **FR-20** Transcript saved; AI scores generated; HR reads both and adds notes.

### Further rounds & decision
- **FR-21** HR can add one extra technical round, skip any round, set each later round AI or manual.
- **FR-22** HR confirms final-interview list and records final decision per candidate.

### Dashboards, integrity, notifications
- **FR-23** Per-test HR dashboard: invited/started/submitted, scores, ranking, verification stats.
- **FR-24** Tab switch / focus loss is counted server-side; 3rd switch closes the attempt.
- **FR-25** Closed attempts appear in that test's Alerts tab; HR can grant resume.
- **FR-26** Emails sent at each stage, logged in `email_logs`.
- **FR-27** Super Admin manages AI prompts, AI settings, users/roles; views audit log.

## Non-functional requirements & constraints
| Area | Requirement |
|---|---|
| Security | RBAC enforced in the API (never only in UI). Hidden test cases, answer keys, AI prompts never reach students. Secrets only on the server. |
| Reliability | Answers/code/timer survive refresh and network loss; server is source of truth for the deadline. |
| Integrity | Full screen, location, terms; attempt closes at 3 tab switches. |
| Performance | Run result p95 < 5 s; dashboard p95 < 2 s for 500 candidates. |
| Scalability | One campus batch concurrently (assume **300** until decided). |
| Auditability | Every verification step, violation and HR action is logged with actor + timestamp. |
| Constraints | React + JavaScript frontend; Python REST API; Supabase (Postgres, Auth, Storage); Judge0; Gemini free tier; resume parsing must not use AI; web only. |
| Out of scope | Payments, offer letters, onboarding, mobile app, camera/face proctoring. |

## Acceptance criteria (definition of done per area)
| ID | Given / When / Then |
|---|---|
| AC-1 Roles | A student token calling any `/hr/*` endpoint gets 403; an HR user cannot read another HR's drive; only Super Admin can read `ai_prompts`. |
| AC-2 Resume analyser | On the 20-resume sample set, CGPA is correct for ≥ 90% and listed skills recall ≥ 80%. |
| AC-3 Entry | Wrong ID/DOB → rejected and logged; correct ID/DOB without location or terms → cannot start; all steps visible in HR stats. |
| AC-4 Round 1 | Attempt auto-submits at server deadline; ranking matches marks desc / time asc; changing an answer key re-scores; cut-off emails only those ≥ threshold. |
| AC-5 Coding | Run returns only 3 sample results; Submit returns `passed/18`; each of the 6 languages passes a reference solution. |
| AC-6 Draft recovery | Refresh or 60 s offline mid-test → code, answers and remaining time restored; no time gained. |
| AC-7 Integrity | 3rd tab switch closes attempt and creates an alert; HR "resume" reopens it with remaining time and switch count reset. |
| AC-8 AI interview | Interview completes, transcript stored, score JSON valid against schema, HR can add notes. |
| AC-9 HR flow | Skip a round → candidates move to the next; add technical round → appears in pipeline; per-test dashboard loads. |
| AC-10 Audit | Every HR mutation and verification step has an `audit_logs` / `verification_logs` row. |

## Open decisions (defaults used until decided)
| Decision | Default we build against |
|---|---|
| Project name | "Ampera Campus Hire" (from designs) |
| Login method | Supabase Auth email+password for HR/Admin; signed link + ID/DOB for students |
| Hosting | Frontend: Vercel/Netlify; API: Render/Railway; Judge0: self-hosted VM (Docker) |
| Email service | Provider-agnostic `EmailSender`; dev = console, prod = Resend or SMTP |
| AI interview scoring | Rubric JSON (communication, technical depth, problem solving, culture; 1–5 each) + summary |
| Candidate count | 300 concurrent |
| RAG for resumes | Not now |
