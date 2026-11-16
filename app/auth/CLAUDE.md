# app/auth — danger zone
- Staff auth is ours (D3): scrypt hashes in `users.password_hash` (`passwords.py`), HS256 tokens signed with `JWT_SECRET` and pinned to HS256. Every token has a `typ` (`staff` 8 h, `set_password` one-time via `pv` = `password_set_at`) and decode checks it. Role is loaded from `users`, never from claims. Login: one 401 for every failure; lock after 5 wrong passwords/15 min. forgot-password is always 204.
- Student tokens: entry (`typ:"entry"`) and attempt (scoped to one candidate + one attempt, 2 h), signed with a key derived from `JWT_SECRET`.
- `/entry/verify`: no invite email; student types student_id + dob, matched against `candidates` that have an active round. Log wrong-DOB tries to `verification_logs` (unknown IDs cannot be logged, FK); same 401 for "no such student" and "wrong DOB"; lock after 5 failures/15 min.
- Entry token: signed (HS256, `STUDENT_TOKEN_SECRET`), candidate-scoped, 2 h.
- Never log tokens, DOBs or passwords. Tests required for every role × endpoint group (AC-1).
