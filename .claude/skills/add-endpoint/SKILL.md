---
name: add-endpoint
description: Add or change a REST endpoint in recruitment-backend. Use when creating a route, changing a request/response shape, or wiring a new feature into the API.
---

# Add an endpoint

1. Find it in `../docs/04-api-contracts.md`. Not there → add the row first (method, path, auth, body → response) and tell the user.
2. In `app/<feature>/`:
   - `schemas.py`: Pydantic request/response models. Student-facing models must not contain `answer`, hidden test cases, or prompts.
   - `repo.py`: SQL only. HR queries filter by `drives.owner_id` (super_admin bypasses).
   - `service.py`: logic; raise `AppError(code, message, status)`; mutations call `audit(actor, action, entity, id, diff)` in the same transaction.
   - `router.py`: route with `Depends(require_role(...))`, `response_model=`.
3. Test in `tests/<feature>/`: happy path, 401 no token, 403 wrong role / other HR's drive, 404/409 domain errors. Mock Gemini/Judge0/email.
4. Run `uv run pytest -q tests/<feature>` and `uv run ruff check .`; report actual output.
5. Reference the FR/AC id in the commit message.
