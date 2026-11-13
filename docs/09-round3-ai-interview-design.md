# 09 — Round 3 (AI interview) design

Status: **built** (2026-11-13), not yet run against a real Gemini key. The build differs from this draft in two places: students enter through a standalone `/interview` page (Student ID + DOB → interview token) instead of the exam entry flow, and the interviewer can open a typed answer box (`show_answer_box` tool) for code and written answers. `docs/04` has the final routes. Covers FR-19, FR-20, AC-8 and plan tasks 5.1–5.4, plus live camera monitoring (new scope).

## Intent
A shortlisted student talks to an AI interviewer by voice for 35–45 minutes (the round's duration, set by HR). The conversation is saved as text, scored against the rubric with a sentiment reading, and HR reads the transcript and scores, adds notes, and can watch the student's camera while the interview is live.

## Goals
- Student: mic in, AI voice out, live captions, a spoken thank-you at the end.
- The interviewer appears as a 3D character in the site's own style that reacts to the conversation.
- The student's network is checked before the interview and shown throughout it.
- Transcript of both sides stored on the `interviews` row.
- Scoring runs automatically when the interview ends; HR can re-run it.
- Scores are valid against a schema before they are stored (D10, AC-8).
- HR sees a near-live camera picture of each student in a live interview.
- HR list and details screens read the real API instead of mock data.

## Non-goals
- Storing audio or video. Camera frames live in server memory only and expire in seconds.
- Tone-of-voice analysis. Sentiment is read from the transcript text.
- Face matching, multiple-face detection, or any automatic judgement on the camera picture.
- FR-18 interview emails. There is no email sender in the backend yet; students reach the interview through the existing invite link and entry checks.
- Smooth real-time video (WebRTC). See Approaches.
- A human-looking avatar or accurate lip sync (mouth shapes per sound). The mouth follows loudness only.
- A bandwidth speed test, or reporting network quality to HR.

## Anti-goals
- The Gemini API key, the interviewer prompt as an editable client value, or the scoring prompt reaching the browser.
- Our API carrying 40 minutes of audio per student.
- A model's score being stored without schema validation, or being shown as a hiring decision. It is advice for HR.

## Constraints (checked 2026-11-13)
- Interviews run 35–45 minutes (user decision). Gemini Live does not do that by default: an audio-only session ends at **15 minutes** and each connection at about **10 minutes**. Both are lifted with two config switches:
  - `contextWindowCompression: { slidingWindow: {} }` removes the 15-minute cap. The model then forgets the oldest part of the conversation as it goes.
  - `sessionResumption: { handle }`: the server sends `sessionResumptionUpdate.newHandle`; before a connection ends it sends `goAway.timeLeft`; the browser reconnects with the latest handle. Handles last 2 hours. The same token works for these reconnects even with `uses: 1`.
- Ephemeral tokens: `POST https://generativelanguage.googleapis.com/v1beta/auth_tokens`, header `x-goog-api-key`, body `uses`, `expireTime`, `newSessionExpireTime`, `bidiGenerateContentSetup {model, generationConfig{responseModalities, speechConfig}, systemInstruction, ...}` (the docs page shows the SDK name `liveConnectConstraints`, which REST rejects; checked with a real call 2026-11-14). v1beta only, Live API only.
- Transcription is switched on in the session config (`inputAudioTranscription`, `outputAudioTranscription`) and arrives as `inputTranscription.text` / `outputTranscription.text` in server messages.
- Audio in: 16-bit PCM, 16 kHz, `audio/pcm;rate=16000`. Audio out: 16-bit PCM, 24 kHz.
- Model id used in Google's examples: `gemini-3.8-live` (config value, not hard-coded).
- The backend has no Gemini key today (question generation moved to NVIDIA). This adds `GEMINI_API_KEY`.
- One API process (D2), so in-memory camera frames work. They would not survive a move to several workers.

## Approaches

**Audio**
| | A. Browser ↔ Gemini direct, ephemeral token (**chosen**) | B. Proxy through our WebSocket |
|---|---|---|
| We build | one token endpoint | a relay: browser ↔ FastAPI ↔ Gemini |
| Load on our API | none | every audio stream |
| Transcript trust | browser-reported, a skilled student could alter it | server-captured |
| Latency | lowest | one extra hop |

A was chosen in the conversation: it is what `02-architecture.md` already describes, and HR watching live plus reading the transcript limits the tampering risk. B stays the upgrade path if transcript trust becomes a problem.

**Camera monitoring**
| | A. Snapshot relay (**chosen**) | B. WebRTC |
|---|---|---|
| HR sees | a still every 3 s | smooth video |
| We build | two endpoints, a polling `<img>` | signalling socket, peer connections, a TURN server |
| Campus networks | plain HTTPS, always works | often blocked without TURN |

A was chosen in the conversation. B can replace it later behind the same HR screen.

**Interviewer character**
| | A. Built in code from simple shapes (**chosen**) | B. Ready-made rigged model (`.glb`) |
|---|---|---|
| Looks | stylised, in the site's colours | human-like |
| Needs | nothing | a licensed model file, several MB |
| Weak PCs | fine | heavier |

A was chosen in the conversation ("build it, adapt it to the website theme and the platform"). B can replace it later inside the same component, since the audio hookup is identical.

**Scoring model** (my call): the existing `NvidiaClient.complete_json` in `app/papers/agents.py`. It is already configured, rate-limited and tested; using Gemini for this too would add a second client for one call.

## Design

### Entry and timing
- `attempts.repo.get_startable` accepts `ai_interview`. The student goes through the same invite → ID/DOB → system check → `POST /entry/start` and gets the same attempt token. This reuses the server deadline (D5) and tab-switch locking (D6) unchanged.
- `ensure_questions` returns true for `ai_interview` (no paper).
- The interview lasts the round's duration, which is the attempt deadline (D5). The default for a new `ai_interview` round changes from 20 to 40 minutes in `app/drives/rounds.py`; HR can set 35–45.
- The prompt gives the interviewer the total time and a section plan (introduction, projects, technical, problem solving, closing) with minutes per section, so it paces itself and does not rely on remembering the start of the conversation. The page tells the model when 3 minutes are left and ends the session at the deadline.
- The system check for an interview round adds three steps: microphone permission, camera permission and a network check (frontend only; no new verification step in the DB). See Network check.

### Backend: `app/interviews/` (router → service → repo, schemas)
Student routes use the existing attempt-token dependency; HR routes use `require_role` and drive ownership. HR mutations are audited.

| Method | Path | Auth | Behaviour |
|---|---|---|---|
| POST | `/interview/session` | S | Upserts the `interviews` row (`live`, `started_at`). Builds the system instruction from `ai_prompts.interview_system` (code default if absent) with role title, candidate skills and the time cap. Mints a token with `uses: 1`, `expireTime` = attempt deadline + 5 minutes, the model, and a locked config: audio response, both transcriptions, sliding-window compression, session resumption, system instruction. → `{ephemeral_token, model, expires_at, seconds_left}`. 409 if the interview is already `completed`. 503 `AI_NOT_CONFIGURED` without a key. |
| POST | `/interview/transcript` | S | `{turns:[{role: "ai"\|"candidate", text, ts}], ended}`. Appends to `transcript` (jsonb concat in one statement). Limits: 50 turns per call, 2 000 chars per turn, 1 500 turns total. `ended: true` sets `completed`, `ended_at`, submits the attempt, and queues scoring as a background task. |
| POST | `/interview/frame` | S | Body: one JPEG, max 100 KB. Kept in a module-level dict keyed by interview id with a timestamp. Nothing is written to disk or DB. |
| GET | `/interviews/{id}/frame` | HR | Latest frame as `image/jpeg` if it is under 10 s old, else 404. `Cache-Control: no-store`. |
| GET | `/rounds/{id}/interviews` | HR | List: candidate, status, started/ended, scores summary. |
| GET | `/interviews/{id}` | HR | Full row: transcript, `ai_scores`, `hr_notes`. |
| POST | `/interviews/{id}/evaluate` | HR | Re-runs scoring. 409 if no transcript. |
| PATCH | `/interviews/{id}` | HR | `{hr_notes}`. |

If a session drops, the student may call `/interview/session` again while the attempt is in progress and the interview is not completed; the transcript keeps appending. An attempt that expires or locks with the interview still `live` leaves it `live` with a partial transcript; HR can still run evaluate on it.

### Scoring
One `complete_json` call with `ai_prompts.interview_eval` (code default if absent) and the transcript. Output validated by a Pydantic model and stored in `ai_scores`:

```json
{
  "communication": 1-5, "technical_depth": 1-5, "problem_solving": 1-5, "culture": 1-5,
  "sentiment": {"overall": "positive|neutral|negative", "confidence": "low|medium|high", "note": "<= 300 chars"},
  "summary": "<= 1200 chars",
  "model": "...", "evaluated_at": "..."
}
```
Invalid output is retried once, then the interview gets `ai_scores = {"error": "<code>"}` so HR sees that scoring failed and can re-run. No schema migration: `ai_scores` is already jsonb, and `status` is already free text.

### Student screen: `src/app/exam/[roundId]/interview/page.tsx`
Replaces the scripted chat. New dependency: `@google/genai` (browser SDK for `live.connect` with the token).

- Start button (a click is needed to unlock audio) → `POST /interview/session` → `ai.live.connect`.
- Mic: `getUserMedia` → AudioWorklet → 16 kHz PCM chunks → `sendRealtimeInput`.
- Playback: queue of 24 kHz PCM buffers on an `AudioContext`; cleared when the model is interrupted.
- Captions from the two transcription streams; completed turns are posted to `/interview/transcript` as they finish (so a crash loses at most one turn).
- Camera: `getUserMedia` video → small self-preview; every 3 s a 320 px JPEG from a canvas → `/interview/frame`.
- Reconnects by itself: stores each `newHandle`, and on `goAway` or a dropped socket reopens the connection with that handle and the same token. Mic chunks captured during the gap (normally under a second) are dropped; a small "reconnecting" note shows.
- Interviewer character and network indicator: see the next two sections.
- Ends on the model's closing turn, the deadline, or the End button → final post with `ended: true` → `/exam/{roundId}/done`.
- States: permission denied (mic or camera), token error, connection lost with a Reconnect button, already completed.
- Existing integrity hook stays on.

Layout:
```
[logo]  AI interview — <role>    ▮▮▮▯ Good    32:41 left   [End interview]
+----------------------------------------------+  +-----------+
|            ( 3D interviewer )                |  | your      |
|          "Interviewer is speaking"           |  | camera    |
|----------------------------------------------|  +-----------+
|  AI: Tell me about a project you're ...      |  mic level ▮▮▮▯
|  You: I built an attendance app that ...     |
+----------------------------------------------+
```

### Interviewer character: `src/features/interview/interviewer-character.tsx`
New dependency: `three` (plain three.js in one client component, loaded with `next/dynamic` so only the interview page downloads it; no react-three-fiber).

- **Built in code:** head, shoulders, eyes and mouth from basic shapes. No model or texture files.
- **Theme:** colours are read from the site's CSS tokens at mount (`getComputedStyle`), so it matches the Stitch palette and follows light/dark without hard-coded colours.
- **States**, driven by two props (`state`, `level`):
  - `idle`: slow breathing, blinking.
  - `speaking`: mouth opens with the loudness of the AI audio (an `AnalyserNode` on the playback path), slight head movement.
  - `listening`: head tilts toward the student, a soft ring pulses with the mic level.
  - `thinking` (student finished, AI not yet answering) and `reconnecting`: a calm waiting pose.
- **Platform adaptation:**
  - Sizes to its container; on narrow screens it sits above the captions instead of beside them.
  - Pixel ratio capped at 2; rendering pauses when the tab is hidden.
  - `prefers-reduced-motion`: no sway or breathing, only the mouth and state colour change.
  - No WebGL (old or locked-down lab PCs): falls back to a flat SVG face with the same states. The interview never depends on the 3D view.
- The canvas is decorative for screen readers (`aria-hidden`); the state is also shown as text ("Interviewer is speaking").

### Network check: `src/features/interview/use-network-quality.ts`
One hook, used in two places. No new endpoint: it times small requests to the existing `GET /healthz`.

- **Measure:** 5 timed requests → median round trip and jitter (spread between them); `online` / `offline` browser events. Verdict: **Good** (median under 300 ms, jitter under 100 ms), **Weak** (anything slower, or a failed request), **Offline**. The thresholds are constants in the hook.
- **Before the interview** (system check step "Check your network"): runs once with a Re-check button. Good → pass. Weak → pass with a warning ("Your connection is slow. Move closer to the router or switch network if you can."). Offline → Start stays disabled. Weak does not block (my call): a hard block could strand a student on campus Wi-Fi that is slow but usable.
- **During the interview:** re-measures with one request every 10 s and shows signal bars with a text label in the header. It also drops to Weak while the Gemini connection is reconnecting, because our API being reachable does not prove the path to Google is. On Offline the page shows "Connection lost, reconnecting…", pauses the mic, and resumes the same session with the stored handle when the network returns. The timer keeps running (D5).
- Limit: this measures the path to our API, not to Google, and measures delay, not bandwidth. It catches the common cases (no network, very slow Wi-Fi) and nothing subtler.

### HR screens
- `hr/interviews/page.tsx`: list from `GET /rounds/{id}/interviews`; rows that are `live` show the camera still, refreshed every 3 s through `apiBlob`.
- `hr/interviews/[interviewId]/page.tsx`: transcript, four rubric scores, sentiment, summary, notes editor, Re-run scoring, and the live still while `live`. Built from the two Stitch mockups already in the repo.
- New `src/api/interviews.ts` using the existing `api` / `apiBlob` / `wire` helpers.

### Docs to update in the same change
- `01-requirements.md`: move camera out of "Out of scope" as "live camera stills during the AI interview, not stored"; note the 35–45 minute length.
- `04-api-contracts.md`: the table above.
- `03-database-schema.md`: `ai_scores` shape. `.env.example`: `GEMINI_API_KEY`, model.
- Terms text on the entry screen: one line saying the camera is viewed live by HR during the interview and not recorded.

## Tests
- Backend: token request body (mocked httpx: key in header only, config locked, never in the response); transcript append and limits; `ended` completes the interview and submits the attempt; score schema accepts valid and rejects invalid output; frame size limit, expiry, and HR ownership; every HR route's role check.
- Frontend: PCM conversion helpers; transcript turn batching; reconnect on `goAway` with the stored handle; page states with the SDK mocked; the character falls back to SVG when WebGL is missing and maps each state to the right label; network verdicts from fake timings (good, weak, failed request, offline event) and the Start button staying disabled when offline. The 3D look itself is checked by eye and with the three.js scene tools (screenshot per state, memory check on unmount).
- Not automatable: a real spoken interview. Needs one manual run with a real key before it is called done.

## Known limits (accepted)
- Transcript is browser-reported (see Approaches).
- Camera frames are lost on API restart and do not work across multiple workers.
- Over a 40-minute interview the model forgets the earliest answers (sliding window). The stored transcript is complete, because the browser collects it, so scoring sees everything; only the interviewer's own follow-ups are affected.
- A reconnect roughly every 10 minutes is a short audible gap, four times per interview.
- A 40-minute audio session uses a lot of Gemini quota. On the free tier, expect only a few interviews at a time; a batch needs a paid key or time slots.
- Whether the token can lock the full system instruction is stated but not shown in Google's examples. If it cannot, the instruction is sent from the browser at connect, which exposes the interviewer prompt (not the scoring prompt) to a student who opens dev tools. To be confirmed with the first real call.

## Left to the builder
Module internals, exact SCSS, worklet buffer sizes, default prompt wording, test file layout.
