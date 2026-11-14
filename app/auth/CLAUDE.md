# app/auth — danger zone
- Two token types: Supabase JWT (HR/Admin; verify ES256/RS256 signature + exp + aud + iss against the project JWKS at `SUPABASE_URL` (never HS256), then load role from `users`, never from the token claims alone) and our attempt token (student; scoped to one candidate + one attempt, 2 h).
- `/entry/verify`: no invite email; student types student_id + dob, matched against `candidates` that have an active round. Log wrong-DOB tries to `verification_logs` (unknown IDs cannot be logged, FK); same 401 for "no such student" and "wrong DOB"; lock after 5 failures/15 min.
- Entry token: signed (HS256, `STUDENT_TOKEN_SECRET`), candidate-scoped, 2 h.
- Never log tokens, DOBs or passwords. Tests required for every role × endpoint group (AC-1).
