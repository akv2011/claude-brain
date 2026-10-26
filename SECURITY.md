# Security

Report a vulnerability through GitHub's private vulnerability reporting on this repository (Security tab, "Report a
vulnerability"). Please do not open a public issue for it.

What the brain touches, so you can judge a report:
- It reads files under the projects folder, the Claude Code memory folder and the files you list. It never writes
  there, never commits in a project's repo, and never deletes a note.
- It writes only inside its own folder (`index.sqlite`, `state/`, `views/`, `catalog/`, `review/`) and, when set,
  `archive_dir` and `~/.local/state/brain-hook/`.
- The reading pass and the audit send note text to Claude through the logged-in `claude` CLI, isolated with
  `--safe-mode`, no tools, no MCP servers and no saved session. The runner removes `ANTHROPIC_API_KEY` and
  `ANTHROPIC_AUTH_TOKEN` from those calls.
- With `remote` set, it runs `ssh <remote>` with BatchMode and ships each reading run's prompts there.
- The catalog quotes your notes. Quotes shaped like secrets are dropped, and `lint` blocks a commit that holds one.
