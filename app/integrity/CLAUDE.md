# app/integrity — danger zone (test fairness)
- Server owns time: `deadline_at` set once at start; on lock store `remaining_sec_on_lock`; HR resume sets `deadline_at = now() + remaining`, resets `tab_switches`, increments `resume_count`.
- Increment and lock in ONE statement: `update attempts set tab_switches = tab_switches + 1, status = case when tab_switches + 1 >= 3 then 'locked' else status end ... returning`.
- Every violation and every HR resume is logged (violations + audit_logs).
- A network loss can look like a tab switch: never auto-fail, only lock.
