#!/usr/bin/env bash
# PostToolUse(Edit|Write|MultiEdit): format the touched file. Never blocks; silent if the formatter isn't installed yet.
f=$(jq -r '.tool_input.file_path // empty')
[ -f "$f" ] || exit 0
dir=$(cd "$(dirname "$f")" && git rev-parse --show-toplevel 2>/dev/null) || exit 0
case "$f" in
  *.py) [ -f "$dir/pyproject.toml" ] && (cd "$dir" && uv run --quiet ruff format "$f" && uv run --quiet ruff check --fix --quiet "$f") >&2 ;;
  *.js|*.jsx|*.css|*.json|*.html) [ -x "$dir/node_modules/.bin/prettier" ] && "$dir/node_modules/.bin/prettier" --write --log-level warn "$f" >&2 ;;
esac
exit 0
