# Porting a change into this repository

This repository is the public release of a private working copy that runs on its author's notes. The code files are
the same in both: everything personal lives in the private copy's `config.json`, its `schema/registry-extra.json`
and its documents, never in the code. So a change is ported by copying the shared files, not by hand.

## Shared files

`brain.py`, `hook.py`, `runner.py`, `tests/run.py`, `tests/stub_claude.py`, `tests/mutate.py`, `docs/DESIGN.md`.

Everything else here (README, skill, config example, registry vocabulary, examples, license) is written for this
release.

## Steps

1. Commit the change in the private copy, with its tests green.
2. Copy the shared files here (the private copy's `scripts/publish_public.sh` does it).
3. Scrub: a private term list (people, machines, project and session names, paths) must find nothing. The list lives
   only in the private copy, since it names what it removes. A hit means the shared code holds something personal:
   fix it in the private copy first, so the two stay identical, then copy again.
4. Run `python3 tests/run.py` here. It must pass with no `config.json`.
5. Commit with dates in UTC.
