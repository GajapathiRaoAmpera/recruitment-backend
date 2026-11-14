# app/execution — danger zone (Judge0)
- Hidden test cases (`is_sample = false`) are loaded only here and in scoring; responses to students contain counts only, never input/expected/stdout of hidden cases.
- Always set Judge0 `cpu_time_limit`, `memory_limit`, `wall_time_limit`; client timeout 15 s; one in-flight run per candidate.
- Compare output after normalising trailing whitespace/newlines only.
- Languages map in one dict (SQL→SQLite, Python, C, C++, Java, Go); AC-5 integration test runs a reference solution per language.
- Unit tests mock Judge0; never call it from unit tests.
- Function-mode drivers (`function_mode/templates/*.tmpl`) are fixed repo code, never LLM-written; `build_source()` output goes to Judge0 only, never into a response, DB row or log. Student code stays first so error line numbers match. Change a template → run `test_function_drivers_judge0.py`.
