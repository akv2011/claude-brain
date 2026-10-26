---
name: brain
description: Search every past note, memory file, commit message and script header in the projects folder before re-solving something. Use before debugging an error, before starting a kind of task that may have been done before (a deploy, a migration, a crawl, a database or CI problem), when asked "how did we", "did we already" or "where is the note about", and before writing a new note (to update the existing entry instead).
---

# Brain

`~/.claude/skills/brain/brain search` searches the index of every note in the projects folder: repo docs, the
Claude Code memory folder, commit messages (why each change was made), script headers, `ponytail:` comments, and
text later deleted from notes. The index refreshes itself when it is over 10 minutes old.

## Steps

1. **Search with two or three wordings.** Search matches words, not meaning, so one wording can miss.
   - the exact error text: `~/.claude/skills/brain/brain search "Durable Object class is not exported"`
   - the tool plus the symptom: `brain search "replica reads hang"`
   - the project name plus the topic: `brain search "shop checkout redirect"`. Nicknames and session prefixes work.

   Narrow when there are too many hits: `--project <name>`, `--kind bug|method|decision|lesson|log|commit|script|memory`,
   `--limit 10`, `--json`.
   Done when: you have run at least two wordings, or the first one returned the note you needed.
2. **Read the whole list, then open the relevant hits.** Search lists up to 30 hits: the first 5 in detail, the rest
   one line each. The order is word matching, not meaning, so the answer is often below #1 (in tests the right note
   was in the top 30 for 27 of 30 queries but in the top 5 for 24). Open every hit that looks relevant. Each hit is a
   pointer (`/abs/path:line`), not the answer: read the section; in a long section the line points at the passage
   that matched.
   Check its date and any `[corrected]` flag (a later line says the claim changed). A `script: <path> (MISSING)`
   line means the script moved or was deleted.
   - `cause` and `fix` lines come from quotes checked against the note.
   - `reuse (suggestion)` is the reading model's advice, written from those quotes.

   Done when: you have read the section you will rely on, not only the hit line.
3. **Say what you used.** Name the note path in your reply, so the user can see which past fix you reused.
4. **Nothing found.** Say which wordings you tried. After solving it, write the finding as its own section in a topic
   doc (a dated heading with the words a search would type, then Symptom, Cause, Fix, Reuse lines), so the next
   search finds it.

## Maps

`views/` in the brain folder:
- `projects/<project>.md` has every bug, decision, method and lesson about one project, including those inside
  session and crawl logs (marked `[log]`). Open it before working on a project.
- `bugs.md` has symptom, cause and fix by domain.
- `methods.md` has reusable methods, lessons and decisions.
- `timeline.md` has every dated line, newest first.
- `by-project.md`, `by-tech.md` and `at-risk.md`.
