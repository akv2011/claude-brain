# Design

How Claude Brain works and why each choice was made. The numbers come from building it on one person's notes in
October 2026: about 1,400 note files across 60 repos, 560 memory files and 5,500 commits.

## The problem

Claude Code sessions write useful notes: the cause of a bug that took an hour, the command that fixed a deploy, a
decision and its reason. The next session rarely finds them. Notes sit in hundreds of files across dozens of repos,
name the same project several ways, and bury findings inside session logs. So fixes get worked out again.

## Shape

```
notes on disk ──build──> index.sqlite (FTS5) ──search──> hits
      │                        ▲
      └──classify (claude -p)──┴── catalog/*.jsonl (typed findings, quotes checked)
```

- `build` walks the projects folder and needs no model. Search works on day one.
- The reading pass adds fields on top: entry type, symptom, cause, fix, reuse, tags. Its output is a catalog keyed by
  section, merged into the index at build time. Deleting the catalog loses the fields, never a note.
- The daily job runs both and writes the maps.

## Decisions

### 1. Notes stay where they are

Moving notes into one folder would break the links between them and the code that names their paths, and would
collide with sessions editing them. The brain indexes in place and never writes into a project.

### 2. Logical paths, and git worktrees fold onto one

A repo with five worktrees shows five copies of every note. Copies fold by `git rev-parse --git-common-dir`, never by
folder name, because a folder named `<repo>-migrate` can be a separate repo. A section whose text differs between
copies stays its own entry; identical text shows once with "other copies" listed.

### 3. Split losslessly at headings and dated lines

Every note splits into entries at H1 to H3 headings and at lines that start with a date, outside code fences. A line
that starts with a date but continues the sentence before it stays in its paragraph. Every line belongs to exactly
one entry, and joining a file's entries rebuilds the file byte for byte. `brain.py check` proves that on every file,
so a splitting bug cannot hide text.

### 4. Search is lexical, and sessions reword

SQLite FTS5 with bm25, with field weights (a memory file's `name` and `description` weigh more), an OR query built
from the words so raw error text cannot break the syntax, and per-document caps so one long file cannot fill the top
5. Tried and rejected: ranking-weight changes, static embeddings and pseudo-relevance feedback (no gain on an 83-query
tuning set), and a local vector index (16 of 30 alone, 23 of 30 fused with ours, no better than ours alone). What did
help: telling the session to search with two or three wordings. A session is a free query expander, and three
wordings moved the gating set from 24 to 26 of 30.

The session is also the reranker. BM25 orders by words, not meaning, so search lists 30 hits: the first 5 in detail,
the rest one line each, and the skill tells the session to read the whole list and open every relevant hit. The right
note is in the top 30 for 27 of 30 gating queries and 77 of 83 tuning queries, against 24 and 71 in the top 5.

Long sections are searched in passages. BM25 scores a match by the length of the text it sits in, so a task list of
15,000 characters under one heading buried its bullets: a session's four wordings for one decision ranked the right
section 26th to 37th. Every section over 2,000 characters is also indexed as passages of paragraphs and top-level
list items; a hit points at the passage's line and shows the findings whose quotes sit in it. Sections themselves
stay whole, so the catalog and the lossless check do not change. Worktree copies of one section count as one hit,
the main checkout's, with the copies listed. With passages, that decision ranked 3rd, and the tuning set's top 5 rose
from 69 to 71 with no loss on the gating set. The hook keeps its tuned rules on top of this: it still judges a note
by its first matching lines and counts a word's rarity by sections, because judging by the best lines added hints on
real failures and the new ones were wrong.

### 5. Every extracted claim carries a quote, checked by code

The reader returns the symptom, cause and fix as quotes from the section. Import normalizes whitespace, dashes and
quote marks and checks each quote is in the section (with its subsections). A near-miss quote is repaired to the
closest real span when it matches 90% or better; otherwise the field is emptied. Import counts how often each field
is emptied and raises an alarm when a field is emptied over 20% of the time, when over 10% of entries end up empty,
or when the model skips over 5% of notes, so the check cannot pass by doing nothing.

### 6. Tags are grounded in what the model read

The reader tags each section with technologies and projects. A tag survives only if the text the model read names
it (registry aliases, case rules for short names). Project tags must be named in the section or be the folder's own
project. A note copied in from another project, under a `from-<project>/` folder, keeps the innermost one. A chat
session name (`Shop_Sync`) names its project, matched case-sensitively so `shop_tags.py` does not.

Measured and rejected: grounding tech in the section alone (fixed half the bad tags, removed more good ones);
grounding file-level projects (dropped mostly right values).

### 7. Several findings per section, cited by number

A section can hold a table of twelve bugs. The catalog key is (section hash, item), so all twelve survive. The
model cites sections by number and never copies anchors or text it was not asked to quote; that cut output tokens
about 8 times against a pilot that echoed sections.

### 8. Re-read only what changed

Each section is keyed by its own hash, so an edit in the middle of a long log re-reads that section only. A changed
import rule re-applies to the saved raw answers (`state/classify/<run>/out/`) with no model call, because import is
the only catalog writer and is idempotent.

### 9. The reader runs isolated

`claude -p --safe-mode --strict-mcp-config --tools '' --no-session-persistence --output-format json --json-schema`,
from the run folder, with `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_MODEL` removed. Without
`--safe-mode`, CLAUDE.md files (even the managed one, with `--setting-sources ''`) still loaded and steered the answers; without `--strict-mcp-config`
every call started every MCP server. Without `--json-schema` the keys drifted. Calls run two at a time, resumable: a
batch with an answer file is done, a usage-limit error pauses until the reset time the CLI prints.

### 10. A monthly audit by a stronger model, with the reader's view

Each month the daily job samples bug entries, reuse rows, files, memory files, entries whose quotes were emptied, and
sections the reader skipped. A stronger model marks each field right, wrong or unsupported; a field over 10% bad is
reported. The audit must see exactly what the reader saw (the same section scope, the same project list, the same
rules). The first audit gave it less, and nine fields looked bad because of the harness, not the reader. A test now
checks every judged quote is present in the text the audit sees.

### 11. Sessions do not search unless the brain comes to them

A skill and a CLAUDE.md line were not enough: in two days other sessions searched about 13 times. The hook reaches
every session, including ones already running, at the moment it helps:
- a failed Bash command, or a prompt with a pasted error, gets the matching past finding. The rules were tuned on
  1,400 real failures: a hint needs a rare word shared with the finding, not just common error words, and read-only
  commands (`cat`, `grep`, `rg`) never trigger one. On 837 failures it had not seen, it hinted 8 times, all correct.
- session start, and the first prompt of a session that was already running, get one note: how to search, and the
  path of the project's findings page.

The hook never rebuilds the index (it reads the last one), answers in 10 to 260 ms, and fails open: any error prints
nothing and is logged for the daily job to report.

### 12. Findings in logs are surfaced, not moved

About 30% of findings sat only inside session or crawl logs. `views/projects/<project>.md` lists every finding about
a project wherever it was written and marks the ones in logs. Writing conventions ask sessions to put a finding in a
topic doc and give the log one line pointing to it.

### 13. The brain leaves itself out

In the pilot, the brain's own research files outranked the real notes on 2 of 6 queries. The brain's folder, and its
data folder if separate, are never indexed.

### 14. Tests are integration tests, and mutations prove they can fail

`tests/run.py` builds a fixture tree of real git repos (worktrees, ignored folders, symlink loops, invalid UTF-8,
copied notes, a memory folder) and drives the real CLI, stubbing only the `claude` binary. `tests/mutate.py` breaks
one rule at a time in a private copy of the code and requires a test to fail for each. Early runs found mutations
that survived because no test reached the branch; each got a test.

## What it does not do

- Session transcripts are not indexed. They are long, mostly tool output, and Claude Code deletes them after 30 days.
- No embeddings and no knowledge graph. The catalog is already a typed graph (notes, sections, projects, tools,
  causes) in SQLite, and the measured gain from vectors was nil on these notes.
- It never edits, moves or deletes a note, and never commits in a project's repo.

## Open

Technology tags are the reader's judgment and run 14 to 29% wrong in the audit: the reader tags a tool mentioned in
passing, or one a project usually uses. They feed `views/by-tech.md` and a low-weight search column; search did not
move. The next step is a reader-prompt change tried on a fresh sample before any re-read.
