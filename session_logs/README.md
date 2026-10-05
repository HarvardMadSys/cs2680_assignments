# Session logs

Coding-agent session logs from work on this repository, one `.jsonl` file per session.

Claude Code keeps each session's transcript at `~/.claude/projects/<project>/<session-id>.jsonl`,
where `<project>` is the working directory's path with `/` and other punctuation turned into `-`
(e.g. `-home-user-cs2680-assignments`). For other coding agents, see the table in
[Assignment 2, §0.2](../assignment2/project_description_assignment.md#02-developer-logs-developer_logs).

Name each file `<assignment>_<YYYY-MM-DD>_<topic>.jsonl`, e.g.
`assignment3_2026-10-04_starter-code.jsonl`, so the logs sort by assignment and date.

A transcript contains every command the agent ran and its output, so check it for API keys
(e.g. `CS2680_API_KEY`) and other secrets before committing it.
