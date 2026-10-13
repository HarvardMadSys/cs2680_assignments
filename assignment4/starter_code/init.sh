#!/usr/bin/env bash
# Copy the session packager and write CLAUDE.local.md at the git repository root.
# Claude Code loads that filename from the working directory and its parents.

set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(git -C "$script_dir" rev-parse --show-toplevel)
target="$repo_root/CLAUDE.local.md"

cp "$script_dir/package_claude_sessions.py" "$repo_root/package_claude_sessions.py"

cat > "$target" << 'EOF'
# Repository workflow

At the end of every user turn, before you stop, commit the work from that turn.

- If this turn created or edited files, stage only those files and make one git commit.
- Before that commit, package the Claude Code, Cursor, and Codex session logs for the whole repository and include the archive in the same commit. From the repository root, run `python3 package_claude_sessions.py --path . --output claude-sessions.tar.gz`, then stage `claude-sessions.tar.gz`.
- Write a short commit message that says what the turn changed.
- If this turn left the tree clean and the session archive is unchanged, do not make an empty commit.
- Do not stage unrelated dirty files, secrets, or gitignored files.
- Do not amend, rebase, or force-push unless the user asks.
EOF

echo "Wrote $target"
echo "Copied session packager to $repo_root/package_claude_sessions.py"
