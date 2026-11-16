"""SQL questions (SQLite): the student writes a query; it runs on Judge0 (language "sql",
SQLite 3.27) against a hidden setup script, once per dataset, and its rows are compared with
the rows of the reference query. No database is hosted and nothing is graded by an LLM.

- script.py:   a small SQL lexer; strict checks of the generated schema (CREATE TABLE only),
               base dataset (one-row literal INSERTs) and reference query (one SELECT / WITH).
- datasets.py: the 18 datasets derived from the base dataset by fixed-seed code (sample 1 =
               the base rows; the rest are row-order shuffles and subsets), and the table form
               of a dataset shown to students for the 3 samples.
- runner.py:   build_source() = fixed settings + schema + dataset + the student's query, the
               stderr line remap, and the row comparison. Never sent to the browser.
- student.py:  what GET /attempt shows for a SQL question.

Each Judge0 run starts with `.open :memory:`: the sandbox cannot write the database file
Judge0 opens (every statement fails with "access permission denied"), so the run uses a fresh
in-memory database that disappears with it. A student's DROP / DELETE only affects that run.
"""
