# app/execution — danger zone (Judge0)
- Hidden test cases (`is_sample = false`) are loaded only here and in scoring; responses to students contain counts only, never input/expected/stdout of hidden cases.
- Always set Judge0 `cpu_time_limit`, `memory_limit`, `wall_time_limit`; client timeout 15 s; one in-flight run per candidate.
- Compare output after normalising trailing whitespace/newlines only.
- Languages map in one dict (SQL→SQLite, Python, C, C++, Java, Go); AC-5 integration test runs a reference solution per language.
- Unit tests mock Judge0; never call it from unit tests.
