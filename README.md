# Claude Brain

Search everything your Claude Code sessions have written down: the Markdown notes in every repo, Claude Code's
memory files, commit messages, script header comments and `ponytail:` comments. A session that hits an error finds
the past fix in a second instead of working it out again.

It is for people who keep many repos in one folder, run many Claude Code sessions there, and have them write notes.
The notes stay where they are. The brain reads them in place and never edits or moves them.

## What it does

- **Search.** `brain search "<error text or task>"` ranks every section of every note (SQLite FTS5 with bm25). A hit
  gives the project, `/path:line`, the heading path, the date, the cause and fix when known, the scripts the note
  names and whether they still exist, and other copies of the same text. Filters: `--project`, `--kind`, `--limit`,
  `--json`.
- **Sessions get it without asking.** A Claude Code hook puts the matching past fix beside a failed command or a
  pasted error, and tells each session once how to search and where its project's findings are. It stays silent
  unless a strong match exists, hints each note once per session, and never blocks a session.
- **A reading pass (optional).** The `claude` CLI reads every note section and records what it is (bug, method,
  decision or lesson), the symptom, cause and fix as quotes, and a one-line reuse tip. Every quote is checked against
  the note, and a quote that is not there is dropped, so the catalog cannot invent a fix. Tags survive only when the
  text names them. After the first read, only changed sections are read again.
- **Maps.** `views/` holds bugs by domain, methods and decisions, a timeline of dated lines, notes by project and by
  technology, one findings page per project, and the notes that exist in one place only. A project page marks
  findings that live only inside a session log, where a reader of the project's docs would never see them.
- **A daily job.** It rebuilds the index, imports finished readings, reads what changed, checks the catalog, writes
  the maps, and can copy notes that git does not hold to another drive.
- **A monthly audit.** A stronger model judges a random sample of what the reader extracted and reports each field's
  error rate against a 10% bar.

## What it indexes

| Source | How |
|---|---|
| Markdown under the projects folder | Split at H1 to H3 headings and dated lines, outside code fences. Joining a file's entries rebuilds it byte for byte (`brain.py check` proves it). Git worktree copies fold onto one path; identical copies show once. |
| Claude Code memory folder for that projects folder | One entry per memory file, with its `name` and `description` weighted high. |
| `~/.claude/CLAUDE.md` and any files you list | As notes. |
| Commit messages | One entry per commit, subject and body. |
| Script header comments | The first 40 lines of scripts in `scripts/`, `bin/`, `tools/` or `crawl/` folders, or named by a note. A hit on a note shows the scripts it names and whether they still exist. |
| `ponytail:` comments | One entry each, `file:line`. |
| Text deleted from notes | Deleted lines from repos active in the last 90 days, at low weight, so a finding removed with a finished task is still found. |

Left out: `node_modules`, virtualenvs, build and test output, script folders with more than 200 files (vendored),
the folders you list in `exclude`, and the brain's own folder (its plans would outrank the notes they describe).

## Requirements

- Python 3.9 or newer, standard library only, and git. macOS or Linux.
- ripgrep (`rg`) to index `ponytail:` comments. Without it, everything else works.
- For the reading pass and the audit: Claude Code, logged in (`claude -p` must work). On a remote, also tmux.

## Install

```sh
cd ~/code                                   # the folder that holds your repos
git clone https://github.com/akv2011/claude-brain
python3 claude-brain/brain.py build         # about 10 to 30 s for a thousand notes
python3 claude-brain/brain.py search "connection reset by peer"
```

The projects folder defaults to the folder the clone sits in, and the memory folder to the one Claude Code keeps
for that path. Set either in `config.json` (next to `brain.py`) when yours differ. Search rebuilds the index by
itself when it is over 10 minutes old.

### Let sessions search

```sh
mkdir -p ~/.claude/skills/brain
cp claude-brain/skill/SKILL.md ~/.claude/skills/brain/
ln -s "$PWD/claude-brain/brain.py" ~/.claude/skills/brain/brain
```

The skill tells a session when to search and how: two or three wordings, then open the hit before acting. A line in
`~/.claude/CLAUDE.md` helps sessions that do not load the skill:

```markdown
Before debugging an error or redoing a kind of task, run `~/.claude/skills/brain/brain search "<error or task>"`
with two or three wordings and open the hit.
```

### Hand sessions past fixes (the hook)

Add to `~/.claude/settings.json`, with the absolute path to your clone:

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "python3 /ABS/PATH/claude-brain/hook.py", "timeout": 10}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 /ABS/PATH/claude-brain/hook.py", "timeout": 10}]}],
    "PostToolUseFailure": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 /ABS/PATH/claude-brain/hook.py", "timeout": 10}]}]
  }
}
```

Running sessions pick it up without a restart. The hook acts only for sessions working under the projects folder,
takes about 10 ms per failure, never rebuilds the index, and on any error prints nothing and logs to
`~/.local/state/brain-hook/errors.log` (the daily job reports those).

### Read the notes (optional)

```sh
python3 brain.py classify pack --wave all            # prints the run name
python3 brain.py classify start --run <run>          # runs in the background, 2 calls at a time
python3 brain.py classify status --run <run>
python3 brain.py classify import --run <run>         # when it says finished
```

Each batch is about 40,000 characters and one `claude -p` call. On the author's notes (about 1,400 files) a full read
was about 300 calls, drawn from the Claude plan the CLI is logged in to. A usage-limit error pauses the run until the
reset time and then resumes. Set `remote` to run the calls on another machine over ssh.

### Run it daily

The job does nothing when it ran recently, so trigger it hourly:

```sh
# crontab -e
0 * * * * /usr/bin/python3 /ABS/PATH/claude-brain/brain.py daily --if-due 20
```

A systemd user timer is in `examples/`. On macOS, launchd and cron cannot read `~/Desktop` or `~/Documents` without
Full Disk Access for the interpreter; keep the projects folder elsewhere or trigger the job from another machine
over ssh.

## Configuration

`config.json` next to `brain.py`; every key is optional. `config.example.json` shows them all.

| Key | Default | What it sets |
|---|---|---|
| `projects_root` | the folder holding the clone | The folder of repos to index. |
| `memory_dir` | `~/.claude/projects/<projects_root, each non-alphanumeric as ->/memory` | Claude Code's memory folder for that path. |
| `claude_files` | `["~/.claude/CLAUDE.md"]` | Extra files or folders to index as notes, such as skills. |
| `exclude` | `[]` | Folders under `projects_root` to leave out, such as third-party clones. |
| `remote` | `"local"` | An ssh host that runs the reading calls in tmux. It needs python3 and Claude Code logged in. |
| `archive_dir` | none | A folder, such as another drive, for the daily copy of notes git does not hold and a monthly copy of every note. |
| `notify` | none | A command, as a list, that the daily job runs with one message line on failures and results. |
| `note_conventions` | none | A doc the session note points to for how to write a finding. |
| `push_catalog` | `false` | The daily job commits and pushes `catalog/notes.jsonl`. Only for a private repo: it quotes your notes. |
| `index_host` | none | `{"ssh": "<host>", "not_on": "linux"}`: a second machine that syncs this folder searches through a wrapper that forwards to the host, and never builds its own index. |
| `queries` | `review/queries.jsonl` | The search test set for `eval` and the daily job. |
| `registry_extra` | `schema/registry-extra.json` | Project nicknames, session prefixes and technology words. |

Environment variables override the file: `BRAIN_PROJECTS`, `BRAIN_MEMORY`, `BRAIN_CLAUDE_FILES`, `BRAIN_EXCLUDE`,
`BRAIN_REMOTE`, `BRAIN_ARCHIVE`, `BRAIN_QUERIES`, `BRAIN_REGISTRY_EXTRA`, and `BRAIN_CONFIG` (another config file).
`BRAIN_HOME` moves the index, catalog and maps; `BRAIN_CLAUDE` names the `claude` binary; `BRAIN_CLASSIFY_MODEL`,
`BRAIN_CLASSIFY_EFFORT` and `BRAIN_CRITIC_MODEL` set the models.

### Project names

Notes name a project many ways. `schema/registry.json` is built from the top-level folder names, the rows of a
`docs/repos.md` table in the projects folder (`` | `folder` | Product Name, site.example | ``), and
`project_aliases` in `schema/registry-extra.json`, which also takes the prefixes of chat session names that work on a
project (`"sessions": ["Shop_"]`). `--project` accepts any of these names.

### A search test set

`eval` scores search on a file of queries, one JSON object per line:

```json
{"id": "q1", "kind": "commit", "split": "score", "query": "Read D1 from the primary only", "expected": ["api/docs/database.md"]}
```

A good source is your own history: a commit subject, and the note that same commit changed. `kind: "commit"`
searches notes only, so the commit cannot find itself. The daily job pings when the score falls by more than 2.

## Writing notes it finds

It works on any Markdown. It works best when a finding is its own section with the words a search would type:

```markdown
### 2026-03-14 Worker hangs on D1 read replicas
- Symptom: "Error: The operation was aborted due to timeout"
- Cause: replica reads hang for 6 s on a cold region.
- Fix: read from the primary (`withSession("first-primary")`), commit 1a2b3c4.
- Reuse: any Worker that reads D1 right after a write.
```

Put findings in topic docs, and give a session log one line pointing to them.

## Measured

On the author's notes (about 1,400 note files, 560 memory files and 5,500 commits), 2026-10:

- Search: the right note in the top 5 for 24 of 30 queries mined from commit history, and 26 of 30 when a session
  tries three wordings, which is what the skill asks for.
- Hook: on 837 real failed commands it had not seen during tuning, it gave 8 hints, all pointing to the right note.
- Audit: causes, quotes and reuse tips are under the 10% error bar. Technology tags are 14 to 29% wrong and are the
  open item.
- Tests: 37 integration tests on a fixture tree of real git repos, and 62 deliberate breakages of the code, each
  turning at least one test red.

## Privacy

- Everything runs on your machines. Only the reading pass and the audit send note text out, to Claude, through your
  `claude` login. Those calls run with `--safe-mode`, no tools, no MCP servers and no saved session, and the runner
  removes `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` from their environment, so a stray key is never billed.
- `catalog/notes.jsonl` quotes your notes. It is gitignored here, and `push_catalog` is off by default.
- A note that git ignores is local-only: its rows go to `catalog/local.jsonl`, and the hook shows its location, never
  its text.
- A quote with the shape of a secret (keys, tokens, PEM headers) is dropped, and `lint` blocks a push that holds one.

## Commands

```
brain.py search <words> [--project P] [--kind K] [--limit N] [--json]
brain.py build | check | views | lint | health [--remote] | registry | eval [file]
brain.py classify pack --wave 1|2|3|all [--reread] [--only a.md,b.md] | start --run R | status --run R | import --run R
brain.py critic sample | pack --run R | import --run R | score --file verdicts.jsonl
brain.py daily [--if-due HOURS] [--read-every HOURS] [--no-push]
```

`--kind` takes `bug`, `method`, `decision`, `lesson`, `log`, `commit`, `script`, `ponytail`, `removed`, `note` or
`memory`.

## Tests

```sh
python3 tests/run.py          # integration tests: a fixture tree of real git repos, the real CLI, a stub claude
python3 tests/mutate.py       # breaks one rule at a time in a copy of the code; every break must turn a test red
```

## Layout

| Path | What |
|---|---|
| `brain.py` | Index, search, reading pass, maps, audit, daily job. |
| `hook.py` | The Claude Code hook. |
| `runner.py` | Runs a reading run's `claude -p` calls, resumable. |
| `schema/registry-extra.json` | Hand-kept names: projects, sessions, technologies, domains. |
| `skill/SKILL.md` | The skill sessions load. |
| `docs/DESIGN.md` | How it works and why, with the measurements behind each choice. |
| `index.sqlite`, `state/`, `views/`, `catalog/`, `review/` | Generated, gitignored. |

## License

MIT. See `LICENSE`.
