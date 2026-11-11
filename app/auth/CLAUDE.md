# app/auth — danger zone
- Two token types: Supabase JWT (HR/Admin; verify ES256/RS256 signature + exp + aud + iss against the project JWKS at `SUPABASE_URL` (never HS256), then load role from `users`, never from the token claims alone) and our attempt token (student; scoped to one candidate + one attempt, 2 h).
- `/entry/verify`: compare student_id + dob against `candidates` for the invite's drive; log every try to `verification_logs`; same 401 message for "no such student" and "wrong DOB".
- Invite tokens are signed (itsdangerous/JWT), bound to candidate_id + round_id, expire at test close.
- Never log tokens, DOBs or passwords. Tests required for every role × endpoint group (AC-1).
