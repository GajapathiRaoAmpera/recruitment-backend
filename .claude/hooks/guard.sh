#!/usr/bin/env bash
# PreToolUse(Edit|Write|MultiEdit): block secrets and applied migrations. Exit 2 = block, stderr goes to Claude.
f=$(jq -r '.tool_input.file_path // empty')
[ -z "$f" ] && exit 0
case "$(basename "$f")" in
  .env.example) ;;
  .env|.env.*) echo "Blocked: $f holds secrets. Edit .env.example and ask the user to update .env." >&2; exit 2 ;;
  connections.json) echo "Blocked: $f holds DB credentials." >&2; exit 2 ;;
esac
# Applied migrations are immutable: allow creating new files, block editing existing ones.
if [[ "$f" == */migrations/versions/* && -e "$f" ]]; then
  echo "Blocked: $f is an existing migration. Create a new migration instead (see migrations/CLAUDE.md)." >&2; exit 2
fi
exit 0
