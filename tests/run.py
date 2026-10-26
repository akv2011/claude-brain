#!/usr/bin/python3
"""Integration tests: build a fixture Projects tree with real git repos on disk, then drive the real
brain.py CLI against it. Nothing inside brain.py is stubbed; the model and ssh are the only edges, and
the classify tests stub only the `claude` executable. Run: /usr/bin/python3 tests/run.py [-k name]."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.realpath(__file__))
BRAIN_PY = os.environ.get("BRAIN_TEST_TARGET") or os.path.join(os.path.dirname(HERE), "brain.py")
HOOK_PY = os.environ.get("BRAIN_TEST_HOOK") or os.path.join(os.path.dirname(HERE), "hook.py")
GIT = "/usr/bin/git"
RESULTS = []


def sh(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                            GIT_COMMITTER_EMAIL="t@t"))


def write(path, text, mode="w"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as fh:
        fh.write(text)


def commit(repo, msg, body=""):
    sh(GIT, "add", "-A", cwd=repo)
    sh(GIT, "commit", "-q", "-m", msg, *(["-m", body] if body else []), cwd=repo)


BIG_SECTION = "\n".join("Filler line %d about nothing in particular for size." % i for i in range(500))

BUGS_MD = """# Alpha bugs

## B1 \u2014 Worker hung on D1 read replica
- Symptom: "replica reads hung 6 s in CDG"
- Cause: withSession first-unconstrained routed reads to cold replicas behind the wrangler deploy.
```bash
## not a heading inside fence
2026-01-01 not a dated line inside fence
```
~~~
### also not a heading in a tilde fence
~~~

### B2 Setext check
Setext Heading Should Not Split
===============================
plain text under setext.
The ibex fee was confirmed on
2026-10-01 after one more check.

- 2026-10-20: dated bullet about the zebra migration
2026-10-21 14:00 UTC | sess_A | event about quokka rollout | abc123 | verified
## 2026-10-22 14:40 IST: dated heading about wombat deploy
#### H4 small stays inside
small h4 body text

## Big section
""" + BIG_SECTION + """
#### H4 inside big section splits here
tail of the big section mentions platypus
"""


def make_fixture(root):
    P = os.path.join(root, "Projects")
    mem = os.path.join(root, "memory")
    claude = os.path.join(root, "claude")
    os.makedirs(P)
    # root repo: shared docs, a file ignored by name, an ignored folder, an untracked note
    write(os.path.join(P, ".gitignore"), "docs/private.md\nscratch-out/\nalpha/\nalpha-wt/\nalpha-migrate/\n")
    write(os.path.join(P, "docs/repos.md"), "| Folder | Product |\n|---|---|\n| `alpha` | AlphaSite, alphasite.com |\n"
                                              "| `beta` | `betatool`: a helper |\n")
    write(os.path.join(P, "docs/shared.md"), "# Shared rules\n\n## Accounts\nThe yak account owns hosting.\n")
    sh(GIT, "init", "-q", "-b", "main", cwd=P)
    commit(P, "root docs")
    write(os.path.join(P, "docs/private.md"), "# Private\nFriend home address lives here, nightjar.\n")
    write(os.path.join(P, "untracked-note.md"), "# Untracked\nkestrel untracked note body.\n")
    write(os.path.join(P, "scratch-out/report.md"), "# Report\ngannet report in an ignored folder.\n")
    # alpha: own repo with history, scripts, vendored code, ponytail comment, test output
    A = os.path.join(P, "alpha")
    write(os.path.join(A, "docs/bugs.md"), BUGS_MD)
    write(os.path.join(A, "docs/notes.md"), "# Notes\n\nThe gizmo needs a restart after every resize event.\nkeep line\n")
    write(os.path.join(A, "docs/layout.md"), "# Layout\n\nThe caracal box wrapped until `.box{flex:1 1 auto}` became basis 0.\n")
    write(os.path.join(A, "docs/howto.md"), "# How to\n\n## Purge\nRun `scripts/fix_cache.py --all` then `scripts/gone.py`.\n")
    write(os.path.join(A, "scripts/fix_cache.py"), '#!/usr/bin/env python3\n"""Purge the albatross edge cache for every zone."""\nprint(1)\n')
    for i in range(201):
        write(os.path.join(A, "scripts/vendored/lib%03d.js" % i), "// vendored library file\nmodule.exports=%d\n" % i)
    write(os.path.join(A, "src/lock.py"), "import x\n\n# ponytail: global lock, per-key locks if throughput matters\nLOCK = 1\n")
    write(os.path.join(A, "test-results/case/error-context.md"), "# generated\nerror context pelican\n")
    write(os.path.join(A, ".gitignore"), "out/\ntest-results/\n")
    write(os.path.join(A, "out/rollout.md"), "# Rollout\nheron rollout report in an ignored folder.\n")
    sh(GIT, "init", "-q", "-b", "main", cwd=A)
    commit(A, "Read D1 from the primary only", "Because replica reads from CDG hung for six seconds (ocelot).")
    write(os.path.join(A, "docs/notes.md"), "# Notes\n\nkeep line\n")
    commit(A, "Drop the gizmo note")
    sh(GIT, "worktree", "add", "-q", "-b", "wt", os.path.join(P, "alpha-wt"), cwd=A)
    with open(os.path.join(P, "alpha-wt/docs/bugs.md"), "a") as fh:
        fh.write("\n## B3 Only in the worktree\ncapybara entry that exists only in the worktree copy.\n")
    # a separate repo with a similar name must not fold into alpha
    M = os.path.join(P, "alpha-migrate")
    write(os.path.join(M, "README.md"), "# Alpha migrate\nseparate repo about the tapir upload.\n")
    sh(GIT, "init", "-q", "-b", "main", cwd=M)
    commit(M, "init migrate")
    # beta: plain folder inside the root repo, awkward encodings
    B = os.path.join(P, "beta")
    write(os.path.join(B, "crlf.md"), "\ufeff# CRLF note\r\n\r\n## Windows section\r\nlemur line with CRLF\r\n", mode="w")
    write(os.path.join(B, "empty.md"), "")
    write(os.path.join(B, "bad.md"), b"# Bad bytes\n\nmarmot \xff\xfe invalid utf8\n## Next\nafter\n", mode="wb")
    write(os.path.join(B, "nohead.md"), "just a paragraph about the narwhal with no headings\nsecond line\n")
    write(os.path.join(B, "copied-howto.md"), open(os.path.join(A, "docs/howto.md")).read())
    write(os.path.join(B, "fenced-title.md"), "```bash\n# or this\n```\n# Real Title\nbody about the tamarin\n")
    # excluded trees
    write(os.path.join(P, "vendor/repos/x/README.md"), "# third party\nexcluded quetzal\n")
    write(os.path.join(P, "brain/PLAN.md"), "# brain\nexcluded brain plan raccoon\n")
    # the ponytail scan walks code too, so the excluded trees need code that carries one
    write(os.path.join(P, "vendor/repos/x/lib.py"), "# ponytail: excluded quetzal shortcut\n")
    write(os.path.join(P, "brain/research/b0001.prompt"), "ponytail: raccoon from a quoted note\n")
    # symlinked project outside the tree, and a symlink loop
    O = os.path.join(root, "outside/linkedproj")
    write(os.path.join(O, "docs/a.md"), "# Linked\nlinked project mentions the axolotl.\n")
    sh(GIT, "init", "-q", "-b", "main", cwd=O)
    commit(O, "linked init")
    os.symlink(O, os.path.join(P, "linked"))
    os.symlink("..", os.path.join(B, "loop"))
    # memory
    write(os.path.join(mem, "note-a.md"), "---\nname: note-a\ndescription: \"D1 read replicas hung on AlphaSite, rolled back\"\n"
                                          "metadata:\n  type: feedback\n---\n\nBody of note a links [[note-b]] and [[missing-note]].\n")
    write(os.path.join(mem, "note-b.md"), "---\nname: other-name\ndescription: name differs from the file name\n---\n\nnote b body ibex\n")
    write(os.path.join(mem, "corrected.md"), "---\nname: corrected\ndescription: a fact that changed\n---\n\n"
                                             "The cache lives 5 min.\n- Corrected 2026-10-01: it lives 30 min (jackal).\n")
    write(os.path.join(mem, "MEMORY.md"), "- [Note A](note-a.md)\n")
    write(os.path.join(claude, "CLAUDE.md"), "# Global rules\nAlways mention the okapi rule.\n")
    return P, mem, claude


class Env:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="brain-test-")
        self.P, self.mem, self.claude = make_fixture(self.root)
        self.home = os.path.join(self.P, "brain")  # inside the projects folder, as installed: it must leave itself out
        os.makedirs(self.home, exist_ok=True)
        self.archive = os.path.join(self.root, "drive/archive")
        os.makedirs(self.archive)
        write(os.path.join(self.archive, "INDEX.md"), "# archive index\n\n| project | dataset folder |\n|---|---|\n")
        extra = json.load(open(os.path.join(os.path.dirname(HERE), "schema/registry-extra.json")))
        extra["project_aliases"] = {"gamma": {"aliases": [], "sessions": ["Lark_"], "source": "fixture"},
                                    "delta": {"aliases": [], "sessions": ["Wren_"], "source": "fixture"}}
        write(os.path.join(self.root, "registry-extra.json"), json.dumps(extra))
        write(os.path.join(self.root, "queries.jsonl"), json.dumps(
            {"id": "q1", "kind": "commit", "split": "score", "query": "Read D1 from the primary only",
             "expected": ["alpha/docs/bugs.md"]}) + "\n")
        write(os.path.join(self.root, "config.json"), json.dumps({"exclude": ["vendor/repos"]}))
        self.env = dict(os.environ, BRAIN_PROJECTS=self.P, BRAIN_MEMORY=self.mem, BRAIN_HOME=self.home,
                        BRAIN_CLAUDE_FILES=os.path.join(self.claude, "CLAUDE.md"), BRAIN_ARCHIVE=self.archive,
                        BRAIN_CONFIG=os.path.join(self.root, "config.json"),
                        BRAIN_REGISTRY_EXTRA=os.path.join(self.root, "registry-extra.json"),
                        BRAIN_QUERIES=os.path.join(self.root, "queries.jsonl"),
                        BRAIN_NO_NOTIFY="1", BRAIN_REMOTE="local", BRAIN_HOOK_STATE=os.path.join(self.root, "hook-state"))

    def cli(self, *args, check=True, extra_env=None):
        r = subprocess.run(["/usr/bin/python3", BRAIN_PY, *args], capture_output=True, text=True,
                           env=dict(self.env, **(extra_env or {})), timeout=600)
        assert "Traceback" not in r.stderr, "traceback from %s:\n%s" % (args, r.stderr[-2000:])
        if check:
            assert r.returncode in (0, 1), "exit %d for %s: %s" % (r.returncode, args, r.stderr[-800:])
        return r

    def search(self, *args):
        r = self.cli("search", "--no-refresh", "--json", *args)
        return json.loads(r.stdout) if r.stdout.strip().startswith("[") else []

    def db(self):
        return sqlite3.connect(os.path.join(self.home, "index.sqlite"))


def test(fn):
    RESULTS.append(fn)
    return fn


@test
def build_and_check(t):
    r = t.cli("build")
    counts = json.loads(r.stdout)
    assert counts["note_docs"] >= 10, counts
    r = t.cli("check")
    res = json.loads(r.stdout)
    assert res["lossless_mismatches"]["count"] == 0, res
    assert res["missing_from_index"]["count"] == 0, res
    assert r.returncode == 0, res


@test
def exclusions(t):
    logical = {x[0] for x in t.db().execute("SELECT logical FROM docs")}
    for bad in ("alpha/test-results/case/error-context.md", "vendor/repos/x/README.md", "brain/PLAN.md"):
        assert bad not in logical, bad
    assert not [l for l in logical if "scripts/vendored" in l], "vendored scripts indexed"
    assert not t.search("pelican") and not t.search("quetzal") and not t.search("raccoon")
    link = os.path.join(t.root, "projects-link")
    os.symlink(t.P, link)
    t.cli("build", extra_env={"BRAIN_PROJECTS": link})
    linked = {x[0] for x in t.db().execute("SELECT logical FROM docs")}
    assert "brain/PLAN.md" not in linked, "a projects folder reached through a symlink still leaves the brain out"
    t.cli("build")


@test
def worktree_folding(t):
    db = t.db()
    rows = db.execute("SELECT logical, copies FROM docs WHERE logical LIKE '%docs/bugs.md'").fetchall()
    assert rows == [("alpha/docs/bugs.md", json.dumps(["alpha-wt/docs/bugs.md"]))], rows
    hits = t.search("capybara")
    assert hits and hits[0]["logical"] == "alpha/docs/bugs.md" and hits[0]["path"] == "alpha-wt/docs/bugs.md", hits
    sep = db.execute("SELECT logical FROM docs WHERE logical LIKE 'alpha-migrate/%'").fetchall()
    assert sep == [("alpha-migrate/README.md",)], sep


@test
def splitting(t):
    db = t.db()
    anchors = [a for (a,) in db.execute("SELECT e.anchor FROM entries e JOIN docs d ON d.id=e.doc "
                                        "WHERE d.logical='alpha/docs/bugs.md' AND e.copy_path IS NULL ORDER BY e.n")]
    assert "## B1 \u2014 Worker hung on D1 read replica" in anchors, anchors
    assert not any("not a heading inside fence" in a or "tilde fence" in a or "not a dated line" in a for a in anchors), anchors
    assert not any("Setext Heading" in a for a in anchors), anchors
    assert any(a.startswith("- 2026-10-20") for a in anchors), anchors
    assert any(a.startswith("2026-10-21 14:00 UTC") for a in anchors), anchors
    assert not any(a.startswith("2026-10-01 after") for a in anchors), "a wrapped line starting with a date stays put: %s" % anchors
    assert sum(1 for a in anchors if "wombat" in a) == 1, anchors
    assert "#### H4 small stays inside" not in anchors, anchors
    assert "#### H4 inside big section splits here" in anchors, anchors
    hit = t.search("not a heading inside fence")[0]
    assert hit["anchor"].startswith("## B1"), hit
    dated = [h for h in t.search("quokka rollout") if h["type"] == "log"]
    assert dated and dated[0]["date"] == "2026-10-21", dated


@test
def encodings(t):
    assert t.search("lemur")[0]["logical"] == "beta/crlf.md"
    assert t.search("marmot")[0]["logical"] == "beta/bad.md"
    assert t.search("narwhal")[0]["anchor"] == "(top)"
    assert "beta/empty.md" in {x[0] for x in t.db().execute("SELECT logical FROM docs")}


@test
def ignored_and_untracked(t):
    for word, logical in (("nightjar", "docs/private.md"), ("gannet", "scratch-out/report.md"),
                          ("heron", "alpha/out/rollout.md")):
        h = t.search(word)
        assert h and h[0]["logical"] == logical and h[0]["sensitive"], (word, h)
    states = dict(t.db().execute("SELECT logical, git_state FROM docs"))
    assert states["untracked-note.md"] == "untracked", states["untracked-note.md"]
    assert states["docs/shared.md"] == "tracked"


@test
def other_sources(t):
    c = t.search("--kind", "commit", "ocelot")
    assert c and c[0]["source"] == "commit" and "primary only" in c[0]["title"], c
    r = t.search("gizmo restart resize")
    assert r and r[0]["source"] == "removed", r
    s = t.search("albatross edge cache")
    assert s and s[0]["source"] == "script" and s[0]["logical"] == "alpha/scripts/fix_cache.py", s
    p = t.search("global lock per-key throughput")
    # src/lock.py's header is that same comment, so the module header and the ponytail entry both match
    assert any(x["source"] == "ponytail" and x["logical"].startswith("alpha/src/lock.py") for x in p[:2]), p
    how = [h for h in t.search("purge fix_cache") if h["logical"] == "alpha/docs/howto.md"][0]
    assert {"path": "scripts/fix_cache.py", "exists": True} in how["scripts"], how
    assert {"path": "scripts/gone.py", "exists": False} in how["scripts"], how


@test
def code_headers_are_indexed(t):
    write(os.path.join(t.P, "alpha/src/engine.ts"), "/**\n * The engine routes jaguar frames to the okapi relay.\n */\nexport const x = 1;\n")
    write(os.path.join(t.P, "alpha/src/plain.ts"), "export const kinkajou = 1;\n")
    write(os.path.join(t.P, "alpha/bin/runthing"), "#!/usr/bin/env node\n// Run the tamandua sweep across every host.\nconsole.log(1);\n")
    t.cli("build")
    src = t.search("--kind", "script", "jaguar okapi relay")
    assert src and src[0]["logical"] == "alpha/src/engine.ts", "a src/ module's header comment is indexed: %s" % src
    assert not t.search("kinkajou"), "a src/ file with no header comment is not indexed as code lines"
    run = t.search("--kind", "script", "tamandua sweep")
    assert run and run[0]["logical"] == "alpha/bin/runthing", "an extensionless #! script is indexed: %s" % run


@test
def copies_across_repos(t):
    hits = [h for h in t.search("purge fix_cache") if "howto" in h["path"]]
    assert len(hits) == 1, hits
    h = hits[0]
    assert h["logical"] == "alpha/docs/howto.md", "prefers the copy whose script links resolve: %s" % h
    assert h.get("also_in") == ["beta/copied-howto.md"], h


@test
def memory_and_links(t):
    h = t.search("replicas hung rolled back")
    assert h[0]["logical"] == "memory/note-a.md", h
    b = t.search("ibex")
    assert b and b[0]["logical"] == "memory/note-b.md", b
    links = dict(t.db().execute("SELECT target, exists_ FROM links WHERE kind='wikilink'"))
    assert links == {"note-b": 1, "missing-note": 0}, links
    c = t.search("jackal")
    assert c and c[0]["corrected"], c
    g = t.search("okapi")
    assert g and g[0]["source"] == "claude", g


@test
def aliases_and_filters(t):
    h = t.search("AlphaSite replica")
    assert any(x["logical"].startswith("alpha/") for x in h), h
    only = t.search("--project", "alphasite.com", "replica")
    tagged = {l for (l,) in t.db().execute(
        "SELECT DISTINCT d.logical FROM tags g JOIN entries e ON e.id=g.entry JOIN docs d ON d.id=e.doc "
        "WHERE g.value='alpha' AND g.facet IN ('project','mention-project')")}
    assert only and all(x["project"] == "alpha" or x["logical"] in tagged for x in only), only
    assert "memory/note-a.md" in tagged, "a memory note naming AlphaSite carries the alpha mention tag"
    css = {l for (l,) in t.db().execute("SELECT DISTINCT d.logical FROM tags g JOIN entries e ON e.id=g.entry JOIN docs d "
                                        "ON d.id=e.doc WHERE g.value='css' AND g.facet='mention-tech'")}
    assert "alpha/docs/layout.md" in css, "an alias ending in punctuation (flex:) matches before a digit: %s" % css
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Queue note\nLark_Sync saw the narwhal queue stall; wren_ids.py is unrelated.\n")
    t.cli("build")
    by_session = t.search("--project", "gamma", "narwhal")
    assert any(x["logical"] == "docs/shared.md" for x in by_session), "a session name (Lark_Sync) names its project: %s" % by_session
    tags = {v for (v,) in t.db().execute("SELECT g.value FROM tags g JOIN entries e ON e.id=g.entry WHERE e.anchor LIKE '%Queue note%' "
                                         "AND g.facet='mention-project'")}
    assert tags == {"gamma"}, "only the session's project; wren_ids.py is not the Wren_ session: %s" % tags
    logs = t.search("--kind", "log", "zebra quokka")
    assert logs and all(x["type"] == "log" for x in logs), logs


@test
def long_sections_and_worktree_copies(t):
    filler = "".join("- [ ] Q%d routine upkeep task number %d, nothing to see in this line at all.\n" % (i, i) for i in range(60))
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Lane Q tasks\n" + filler + "- [~] Q99 the quokka console belongs to the heron account, owners = heron.\n"
                 + filler)
    for repo, words in (("alpha", "main copy"), ("alpha-wt", "branch copy")):
        with open(os.path.join(t.P, repo, "docs/bugs.md"), "a") as fh:
            fh.write("\n## B4 Pangolin section\nthe pangolin queue stalls, %s.\n" % words)
    t.cli("build")
    lines = open(os.path.join(t.P, "docs/shared.md")).read().split("\n")
    head = lines.index("## Lane Q tasks") + 1
    buried = next(i for i, l in enumerate(lines, 1) if "Q99" in l)
    h = [x for x in t.search("quokka console heron account") if x["logical"] == "docs/shared.md"][0]
    assert head < h["line"] <= buried and any("Q99" in m for m in h["matched"]), \
        "a hit in a long section points at the passage that matched: %s" % h
    p = [x for x in t.search("pangolin queue stalls") if x["logical"] == "alpha/docs/bugs.md"]
    assert len(p) == 1 and p[0]["path"] == "alpha/docs/bugs.md" and p[0].get("also_in") == ["alpha-wt/docs/bugs.md"], \
        "worktree copies of one section are one hit, the main checkout's: %s" % p
    many = t.search("routine upkeep task alpha replica gizmo")
    assert len(many) >= 7, many
    out = t.cli("search", "--no-refresh", "routine upkeep task alpha replica gizmo").stdout
    assert "More hits, one line each" in out and [l for l in out.splitlines() if l.startswith("7. ")], out[-1500:]
    assert not [l for l in out.split("More hits")[1].splitlines() if l.startswith("   ")], "hits past the fifth take one line"


@test
def symlinks(t):
    h = t.search("axolotl")
    assert h and h[0]["logical"] == "linked/docs/a.md", h


@test
def hostile_queries(t):
    for q in ['error: "NOT" (foo) AND -bar: *', "NEAR(a b)", "a OR", '"', "\u2014", "\u00e9t\u00e9 caf\u00e9",
              "x" * 5000, "the and of", "  ", "col:umn", "^start", "{}[]"]:
        r = t.cli("search", "--no-refresh", q)
        assert r.returncode in (0, 1), (q, r.stderr)
    r = t.cli("search", "--no-refresh", "--json", "\u2014 replica \u2014")
    assert json.loads(r.stdout), "em dash query found nothing"


@test
def freshness_and_concurrency(t):
    note = os.path.join(t.P, "docs/shared.md")
    with open(note, "a") as fh:
        fh.write("\n## New entry\nThe dingo fact was just written.\n")
    old = time.time() - 3600
    os.utime(os.path.join(t.home, "index.sqlite"), (old, old))
    outs = []

    def go():
        r = subprocess.run(["/usr/bin/python3", BRAIN_PY, "search", "--json", "dingo"], capture_output=True,
                           text=True, env=t.env, timeout=600)
        outs.append(r)
    threads = [threading.Thread(target=go) for _ in range(3)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    for r in outs:
        assert "Traceback" not in r.stderr, r.stderr[-1500:]
    found = [r for r in outs if r.stdout.strip().startswith("[") and json.loads(r.stdout)]
    assert found, [r.stdout[:200] for r in outs]
    lines = open(os.path.join(t.home, "state/usage.jsonl")).read().splitlines()
    assert any(json.loads(l)["query"] == "dingo" for l in lines)


@test
def missing_index_builds(t):
    os.remove(os.path.join(t.home, "index.sqlite"))
    r = t.cli("search", "--json", "yak account")
    assert json.loads(r.stdout)[0]["logical"] == "docs/shared.md", r.stdout[:300]


def classify(t, mode="", wave="all", limit=None, reread=False):
    env = {"BRAIN_REMOTE": "local", "BRAIN_CLAUDE": os.path.join(HERE, "stub_claude.py"), "STUB_MODE": mode,
           "BRAIN_RUNNER_MAX_SLEEP": "1"}
    r = t.cli("classify", "pack", "--wave", wave, *(["--limit", str(limit)] if limit else []),
              *(["--reread"] if reread else []), extra_env=env)
    run = json.loads(r.stdout)
    if run["batches"]:
        t.cli("classify", "start", "--run", run["run"], "--wait", extra_env=env, check=False)
    imp = json.loads(t.cli("classify", "import", "--run", run["run"], extra_env=env).stdout)
    t.cli("build")
    return run, imp


def catalog_rows(t, name="notes.jsonl"):
    p = os.path.join(t.home, "catalog", name)
    return {json.loads(l)["logical"]: json.loads(l) for l in open(p)} if os.path.exists(p) else {}


@test
def classify_round_trip(t):
    run, imp = classify(t)
    assert run["batches"] >= 2 and not imp["missing_batches"] and not imp["items_missing"], (run, imp)
    rows = catalog_rows(t)
    bugs = rows["alpha/docs/bugs.md"]
    b1 = [e for e in bugs["entries"] if e["anchor"].startswith("## B1")]
    assert b1 and b1[0]["type"] == "bug" and b1[0]["root_cause_quote"].startswith("withSession"), bugs["entries"]
    assert b1[0]["tech"] == ["cloudflare-workers"] and b1[0]["projects"] == ["alpha"], b1
    assert "cloudflare-workers" in bugs["tech"] and "other:frobnicator" not in bugs["tech"], \
        "a tag the text never names is dropped: %s" % bugs["tech"]
    assert "docs/private.md" not in rows and "docs/private.md" in catalog_rows(t, "local.jsonl"), "sensitive row leaked"
    mem = rows["memory/note-a.md"]
    assert mem["entries"] == [] and mem["description"].startswith("stub"), mem
    h = t.search("--kind", "bug", "replica hung")
    assert h and h[0]["cause"] == "stub cause" and h[0]["type"] == "bug", h
    assert imp["null_rate"].get("root_cause_quote", 0) == 0, imp


@test
def classify_is_idempotent_and_partial(t):
    run, _ = classify(t)
    assert run["items"] == 0, "nothing changed, nothing to read: %s" % run
    again = json.loads(t.cli("classify", "pack", "--wave", "all", "--run", run["run"], extra_env={"BRAIN_REMOTE": "local"}).stdout)
    assert again["run"] == run["run"] + "-2", "a reused run name gets a suffix, never a crash: %s" % again
    one = json.loads(t.cli("classify", "pack", "--wave", "all", "--only", "alpha/docs/bugs.md", extra_env={"BRAIN_REMOTE": "local"}).stdout)
    batch = json.load(open(os.path.join(t.home, "state/classify", one["run"], "batches/b0001.json")))["items"]
    assert [(b["logical"], b["mode"], b["reread"]) for b in batch] == [("alpha/docs/bugs.md", "full", True)], batch
    open(os.path.join(t.home, "state/classify", one["run"], "imported"), "w").write("test")
    with open(os.path.join(t.P, "alpha/docs/bugs.md"), "a") as fh:
        fh.write("\n## B9 New bug\n- Symptom: \"the koala job stalls\"\n- Cause: a lock never released.\n")
    t.cli("build")
    run, imp = classify(t)
    assert run["items"] == 1, run
    batch = json.load(open(os.path.join(t.home, "state/classify", run["run"], "batches/b0001.json")))
    sent = [e["anchor"] for e in batch["items"][0]["entries"].values()]
    assert batch["items"][0]["mode"] == "partial" and "## B9 New bug" in sent, batch
    assert len(sent) <= 2, "only the new section and the old last one (it gained a newline) are re-read: %s" % sent
    rows = catalog_rows(t)
    anchors = [e["anchor"] for e in rows["alpha/docs/bugs.md"]["entries"]]
    assert any(a.startswith("## B1") for a in anchors) and any(a.startswith("## B9") for a in anchors), anchors


@test
def classify_nulls_bad_quotes(t):
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Wallaby outage\n- Symptom: \"wallaby 502s\"\n- Cause: the pool was exhausted.\n")
    t.cli("build")
    run, imp = classify(t, mode="badquote")
    e = [x for x in catalog_rows(t)["docs/shared.md"]["entries"] if "Wallaby" in x["anchor"]][0]
    assert e["root_cause_quote"] is None and e["root_cause"] is None and e["symptom_quote"] == '"wallaby 502s"', e
    assert imp["null_rate"]["root_cause_quote"] == 1.0, imp


@test
def classify_survives_limits_and_garbage(t):
    for i, mode in enumerate(("ratelimit-once", "garbage-once")):
        with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
            fh.write("\n## Outage %d\n- Symptom: \"emu %d failed\"\n" % (i, i))
        t.cli("build")
        run, imp = classify(t, mode=mode)
        prog = json.load(open(os.path.join(t.home, "state/classify", run["run"], "progress.json")))
        assert prog["calls"] == 2 and not prog["failed"] and not imp["missing_batches"], (mode, prog, imp)
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Outage 9\n- Symptom: \"dingo stuck\"\n")
    t.cli("build")
    run, imp = classify(t, mode="wrong-n")
    assert imp["items_missing"] == ["docs/shared.md"], imp
    run2, _ = classify(t)
    assert run2["items"] == 1, "a file the model skipped is read again next run: %s" % run2


@test
def import_repairs_grounds_and_drops(t):
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Parent with no body\n### Child detail\n- Cause: the shrew cache was never warmed.\n")
    t.cli("build")
    classify(t, mode="parent-attribution")
    e = [x for x in catalog_rows(t)["docs/shared.md"]["entries"] if "Parent with no body" in x["anchor"]]
    assert e and e[0]["root_cause_quote"] == "the shrew cache was never warmed.", e
    r = t.cli("lint", check=False)
    assert r.returncode == 0, "lint uses the same subsection scope as import: %s" % r.stdout[-400:]
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Mole outage\n- Symptom: \"mole stalls\"\n- Cause: the mole pool routed every write to one shard.\n")
    t.cli("build")
    run, imp = classify(t, mode="nearquote")
    e = [x for x in catalog_rows(t)["docs/shared.md"]["entries"] if "Mole" in x["anchor"]][0]
    assert e["root_cause_quote"] == "the mole pool routed every write to one shard.", e
    assert "root_cause_quote" in e.get("repaired", []), e
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Vole note\n- Symptom: \"vole slow\"\n")
    t.cli("build")
    run, imp = classify(t, mode="empty-entry")
    assert not [x for x in catalog_rows(t)["docs/shared.md"]["entries"] if "Vole" in x["anchor"]], "empty entry kept"
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Stoat crash\n- Symptom: \"stoat crashed\"\n- Cause: a stale lock file.\n")
    t.cli("build")
    classify(t, mode="reuse-foreign-tech")
    e = [x for x in catalog_rows(t)["docs/shared.md"]["entries"] if "Stoat" in x["anchor"]][0]
    assert e["reuse"] is None and "reuse:names-tech-not-in-note" in e["nulled"], e


@test
def reread_replaces_entries(t):
    env = {"BRAIN_REMOTE": "local", "BRAIN_CLAUDE": os.path.join(HERE, "stub_claude.py"), "STUB_MODE": "empty-entry",
           "BRAIN_RUNNER_MAX_SLEEP": "1"}
    before = len(catalog_rows(t)["alpha/docs/bugs.md"]["entries"])
    run = json.loads(t.cli("classify", "pack", "--wave", "all", "--reread", extra_env=env).stdout)
    assert run["items"] >= 10 and run["reread"], run
    t.cli("classify", "start", "--run", run["run"], "--wait", extra_env=env, check=False)
    imp = json.loads(t.cli("classify", "import", "--run", run["run"], extra_env=env).stdout)
    after = catalog_rows(t)["alpha/docs/bugs.md"]["entries"]
    assert before > 0 and after == [], "a reread replaces old entries (empty-entry mode drops every new one)"
    assert any("as empty" in a for a in imp["alarms"]), "dropping every entry raises an alarm: %s" % imp


@test
def import_keeps_reuse_and_first_answer(t):
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Ferret sync runbook\n- Cause: run the ferret sync twice before the deploy.\n")
    t.cli("build")
    classify(t, mode="quoteless-reuse")
    e = [x for x in catalog_rows(t)["docs/shared.md"]["entries"] if "Ferret" in x["anchor"]]
    assert e and e[0]["type"] == "method" and e[0]["reuse"] == "stub reuse" and not e[0]["root_cause_quote"], \
        "a method's reuse needs no quote, only a tech the section names: %s" % e
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Gecko outage\n- Symptom: \"gecko 500s\"\n- Cause: the gecko queue filled.\n")
    t.cli("build")
    run, imp = classify(t, mode="dup-n")
    e = [x for x in catalog_rows(t)["docs/shared.md"]["entries"] if "Gecko" in x["anchor"]]
    assert e and e[0]["root_cause_quote"] == "the gecko queue filled.", \
        "a second answer numbered 1 must not replace the first: %s" % e
    assert imp.get("duplicate_n") == 1, imp


@test
def import_grounds_in_what_the_model_read(t):
    write(os.path.join(t.P, "beta/docs/from-alpha/otter.md"), "# Otter\n\n## Setup\nWe deploy with wrangler.\n\n"
          "## Otter outage\n- Symptom: \"otter stalls\"\n- Cause: the otter queue filled.\n")
    t.cli("build")
    classify(t, mode="file-tech")
    rows = dict(catalog_rows(t), **catalog_rows(t, "local.jsonl"))
    e = [x for x in rows["beta/docs/from-alpha/otter.md"]["entries"] if "Otter" in x["anchor"]][0]
    assert e["tech"] == ["cloudflare-workers"], "a tag the note names in another section survives: %s" % e
    assert e["reuse"] == "redeploy it with wrangler", "so does a reuse line naming that tool: %s" % e
    assert e["projects"] == ["alpha"] and "alpha" in rows["beta/docs/from-alpha/otter.md"]["projects"], \
        "a note under from-alpha/ belongs to alpha: %s" % e
    write(os.path.join(t.P, "beta/docs/from-alpha/from-beta/otter-back.md"), "# Otter back\n\n"
          "## Otter relapse\n- Symptom: \"otter stalls again\"\n- Cause: the otter queue filled twice.\n")
    t.cli("build")
    classify(t, mode="file-tech")
    rows = dict(catalog_rows(t), **catalog_rows(t, "local.jsonl"))
    e = [x for x in rows["beta/docs/from-alpha/from-beta/otter-back.md"]["entries"] if "relapse" in x["anchor"]][0]
    assert e["projects"] == ["beta"], "a copy of a copy belongs to where it was written, the innermost from-: %s" % e


@test
def sections_hold_several_items(t):
    path = os.path.join(t.P, "alpha/docs/pairs.md")
    write(path, "# Pairs\n\n## Two outages\n1. Symptom: \"weasel stalls\"\n   Cause: the weasel pool leaked.\n"
                "2. Symptom: \"marten stalls\"\n   Cause: the marten cache expired.\n")
    t.cli("build")
    classify(t, mode="multi-item")
    rows = dict(catalog_rows(t), **catalog_rows(t, "local.jsonl"))
    es = [e for e in rows["alpha/docs/pairs.md"]["entries"] if "Two outages" in e["anchor"]]
    assert [(e["item"], e["root_cause_quote"]) for e in es] == [(1, "the weasel pool leaked."), (2, "the marten cache expired.")], es
    h = t.search("--kind", "method", "marten cache expired")
    assert h and h[0]["logical"] == "alpha/docs/pairs.md" and [i["cause"] for i in h[0]["items"]] == ["cause 1", "cause 2"], \
        "--kind matches any item, not only the section's first: %s" % h
    t.cli("views")
    bugs, methods = (open(os.path.join(t.home, "views", n)).read() for n in ("bugs.md", "methods.md"))
    assert "Cause: cause 1" in bugs and "Cause: cause 2" in methods, "the maps list every item of a section"
    pairs = open(path).read()  # the new section goes first, so "Two outages" is shown again unchanged
    write(path, pairs.replace("# Pairs\n\n", "# Pairs\n\n## Long tail\n" + "Plain filler about nothing in particular.\n" * 40 + "\n", 1))
    t.cli("build")
    classify(t)
    rows = dict(catalog_rows(t), **catalog_rows(t, "local.jsonl"))
    es = [e for e in rows["alpha/docs/pairs.md"]["entries"] if "Two outages" in e["anchor"]]
    assert [e["item"] for e in es] == [1], "a section read again keeps only the items read now: %s" % es


@test
def reread_misses_are_read_again(t):
    classify(t, reread=True)
    run, imp = classify(t, mode="wrong-n", reread=True)
    missed = sorted(set(imp["items_missing"]))
    had = [l for l in missed if catalog_rows(t).get(l, {}).get("entries")]
    assert had, "the fixture needs a skipped note with entries: %s" % missed
    run2, _ = classify(t, mode="empty-entry")
    assert run2["items"] == len(missed), "every note a reread skipped is read again: %s, skipped %s" % (run2, missed)
    rows = catalog_rows(t)
    assert all(rows[l]["entries"] == [] and "reread" not in rows[l] for l in had), \
        "and that read replaces its old entries: %s" % {l: rows[l] for l in had}
    classify(t, reread=True)


@test
def views_health_lint(t):
    t.cli("views")
    bugs = open(os.path.join(t.home, "views/bugs.md")).read()
    assert "B1" in bugs and os.path.join(t.P, "alpha/docs/bugs.md") in bugs and "Cause: stub cause" in bugs, bugs[:800]
    for name in ("methods.md", "timeline.md", "by-project.md", "by-tech.md", "at-risk.md"):
        assert os.path.getsize(os.path.join(t.home, "views", name)) > 50, name
    alpha = open(os.path.join(t.home, "views/projects/alpha.md")).read()
    assert "B1" in alpha and "Cause: stub cause" in alpha, "a project's findings file lists its bugs: %s" % alpha[:400]
    assert "## Memory notes" in alpha and "note-a.md" in alpha, "and the memory notes about the project: %s" % alpha[:600]
    assert "[alpha](alpha.md)" in open(os.path.join(t.home, "views/projects/README.md")).read()
    timeline = open(os.path.join(t.home, "views/timeline.md")).read()
    assert "quokka" in timeline and timeline.index("2026-10-22") < timeline.index("2026-10-21"), "newest first"
    risk = open(os.path.join(t.home, "views/at-risk.md")).read()
    assert "untracked-note.md" in risk and "`alpha`: remote NONE" in risk, risk
    assert t.cli("lint").returncode == 0
    path = os.path.join(t.home, "catalog/notes.jsonl")
    good = open(path).read()
    with open(path, "a") as fh:
        fh.write(json.dumps({"logical": "x", "description": "key ghp_" + "A" * 36}) + "\n")
    r = t.cli("lint", check=False)
    assert r.returncode == 1 and "secret shape" in r.stdout, r.stdout
    with open(path, "w") as fh:
        fh.write(good)
    os.remove(os.path.join(t.mem, "note-b.md"))
    t.cli("build")
    r = t.cli("lint")
    assert "no longer on disk: memory/note-b.md" in r.stdout, r.stdout


@test
def daily_job(t):
    r = t.cli("daily", "--if-due", "0", "--no-push", extra_env={"BRAIN_CLAUDE": os.path.join(HERE, "stub_claude.py")})
    assert r.returncode == 0, r.stderr[-1500:]
    rep = json.load(open(os.path.join(t.home, "state/daily-report.json")))
    assert "error" not in rep and rep["lint"]["errors"] == [], rep
    month = time.strftime("%Y-%m")
    d = os.path.join(t.archive, "brain", "daily-" + month)
    tars = [f for f in os.listdir(d) if f.endswith(".tar.gz")]
    assert len(tars) == 1, os.listdir(d)
    chk = subprocess.run(["shasum", "-a", "256", "-c", "SHA256SUMS"], cwd=d, capture_output=True, text=True)
    assert chk.returncode == 0, chk.stdout + chk.stderr
    names = subprocess.run(["tar", "-tzf", os.path.join(d, tars[0])], capture_output=True, text=True).stdout.split()
    assert "memory/note-a.md" in names and "untracked-note.md" in names, names
    snap = os.path.join(t.archive, "brain", "notes-snapshot-" + month)
    assert os.path.exists(os.path.join(snap, "README.md")) and os.path.exists(os.path.join(snap, "SHA256SUMS"))
    index = open(os.path.join(t.archive, "INDEX.md")).read()
    assert index.count("brain/daily-" + month) == 1 and index.count("brain/notes-snapshot-" + month) == 1, index
    t.cli("daily", "--if-due", "0", "--no-push")
    assert len([f for f in os.listdir(d) if f.endswith(".tar.gz")]) == 1, "one daily tar per day"
    assert open(os.path.join(t.archive, "INDEX.md")).read().count("brain/daily-" + month) == 1
    before = os.path.getmtime(os.path.join(t.home, "state/daily-report.json"))
    time.sleep(1.1)
    t.cli("daily", "--if-due", "20", "--no-push")
    assert os.path.getmtime(os.path.join(t.home, "state/daily-report.json")) == before, "not due, so nothing ran"


@test
def titles_skip_fences(t):
    title = dict(t.db().execute("SELECT logical, title FROM docs"))["beta/fenced-title.md"]
    assert title == "Real Title", title


@test
def secrets_never_reach_the_catalog(t):
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Leaky config\n- Symptom: password = " + "hunter2" * 3 + "\n- Cause: a key pasted in a note.\n")  # planted fakes, split for scanners
    t.cli("build")
    run, imp = classify(t)
    rows = catalog_rows(t)
    e = [x for x in rows["docs/shared.md"]["entries"] if "Leaky" in x["anchor"]][0]
    assert e["symptom_quote"] is None and "symptom_quote:secret" in e["nulled"], e
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Token reuse\n- Symptom: \"stale session\"\n- Cause: the session token expired.\n")
    t.cli("build")
    classify(t, mode="leaky-reuse")
    e2 = [x for x in catalog_rows(t)["docs/shared.md"]["entries"] if "Token reuse" in x["anchor"]][0]
    assert e2["reuse"] is None and "reuse:secret" in e2["nulled"], e2
    assert "hunter2hunter2" not in open(os.path.join(t.home, "catalog/notes.jsonl")).read()
    assert t.cli("lint").returncode == 0
    path = os.path.join(t.home, "catalog/notes.jsonl")
    good = open(path).read()
    for planted in ("token: " + "abcdefghijklmnop" + "1234", "key " + "ab12" * 16):
        with open(path, "a") as fh:
            fh.write(json.dumps({"logical": "x", "description": planted, "entries": []}) + "\n")
        r = t.cli("lint", check=False)
        assert r.returncode == 1 and "secret shape" in r.stdout, (planted, r.stdout)
        with open(path, "w") as fh:
            fh.write(good)


@test
def daily_packs_starts_imports_and_recovers(t):
    env = {"BRAIN_CLAUDE": os.path.join(HERE, "stub_claude.py")}
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Gecko outage\n- Symptom: \"gecko pool drained\"\n- Cause: no retry budget.\n")
    r = t.cli("daily", "--if-due", "0", "--no-push", extra_env=env)
    rep = json.load(open(os.path.join(t.home, "state/daily-report.json")))
    assert r.returncode == 0 and rep.get("packed", {}).get("batches", 0) >= 1, rep
    assert "eval" in rep and rep["archive_daily"], rep
    month = "critic-" + time.strftime("%Y-%m")
    assert os.path.isdir(os.path.join(t.home, "state/classify", month)), "a daily packs the month's critic: %s" % rep
    run = [d for d in os.listdir(os.path.join(t.home, "state/classify"))
           if not os.path.exists(os.path.join(t.home, "state/classify", d, "imported")) and not d.startswith("critic-")]
    assert len(run) == 1, run
    deadline = time.time() + 60
    while not os.path.exists(os.path.join(t.home, "state/classify", run[0], "finished")) and time.time() < deadline:
        time.sleep(0.5)
    while not os.path.exists(os.path.join(t.home, "state/classify", month, "finished")) and time.time() < deadline:
        time.sleep(0.5)
    t.cli("daily", "--if-due", "0", "--no-push", extra_env=env)
    rep = json.load(open(os.path.join(t.home, "state/daily-report.json")))
    assert run[0] in rep.get("imported", {}), rep
    assert os.path.exists(os.path.join(t.home, "review", month + ".md")) and \
        os.path.exists(os.path.join(t.home, "state/classify", month, "imported")), "and a later daily scores it: %s" % rep
    assert any("Gecko" in e["anchor"] for e in catalog_rows(t)["docs/shared.md"]["entries"])
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Ibis outage\n- Symptom: \"ibis stuck\"\n")
    t.cli("build")
    dead = json.loads(t.cli("classify", "pack", "--wave", "all", extra_env={"BRAIN_REMOTE": "local"}).stdout)["run"]
    t.cli("daily", "--if-due", "0", "--no-push", extra_env=env)
    rep = json.load(open(os.path.join(t.home, "state/daily-report.json")))
    assert dead in rep.get("restarted", []), "a run with no live runner is restarted: %s" % rep
    deadline = time.time() + 60
    while not os.path.exists(os.path.join(t.home, "state/classify", dead, "finished")) and time.time() < deadline:
        time.sleep(0.5)
    t.cli("daily", "--if-due", "0", "--no-push", extra_env=env)
    assert os.path.exists(os.path.join(t.home, "state/classify", dead, "imported"))


@test
def reading_cycle_runs_between_dailies(t):
    env = {"BRAIN_CLAUDE": os.path.join(HERE, "stub_claude.py")}
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Heron outage\n- Symptom: \"heron queue stuck\"\n- Cause: a lease never expired.\n")
    t.cli("daily", "--if-due", "999", "--read-every", "0", "--no-push", extra_env=env)
    rep = json.load(open(os.path.join(t.home, "state/daily-report.json")))
    assert rep["full"] is False and rep.get("packed", {}).get("items", 0) >= 1 and rep.get("views"), rep
    assert "archive_daily" not in rep and "eval" not in rep, "a reading cycle skips backups, eval and push: %s" % rep
    t.cli("daily", "--if-due", "999", "--no-push", extra_env=env)
    assert json.load(open(os.path.join(t.home, "state/daily-report.json")))["started"] == rep["started"], \
        "nothing runs again within the reading interval"


@test
def critic_audits_a_sample(t):
    with open(os.path.join(t.P, "docs/shared.md"), "a") as fh:
        fh.write("\n## Weasel queue\n" + "Plain filler about the weasel queue and nothing else.\n" * 300 +
                 "- Cause: the weasel queue overflowed at the tail.\n"
                 "\n## Ferret parent\n### Ferret child\n- Cause: the ferret index was built twice.\n")
    t.cli("build")
    classify(t, mode="parent-attribution")
    env = {"BRAIN_REMOTE": "local", "BRAIN_CLAUDE": os.path.join(HERE, "stub_claude.py"), "STUB_MODE": "critic-wrong",
           "BRAIN_RUNNER_MAX_SLEEP": "1"}
    meta = json.loads(t.cli("critic", "pack", "--run", "critic-test", "--seed", "7", extra_env=env).stdout)
    assert meta["kind"] == "critic" and meta["rows"] > 0, meta
    prompts = [open(os.path.join(t.home, "state/classify/critic-test/batches", f)).read()
               for f in sorted(os.listdir(os.path.join(t.home, "state/classify/critic-test/batches"))) if f.endswith(".prompt")]
    flat = lambda s: " ".join(s.split()).lower()
    rows = [json.loads(l) for p in prompts for l in p.split("CRITIC ROWS", 1)[1].splitlines() if l.startswith("{")]
    judged = [r for r in rows if "entry" in r]
    assert any("weasel" in r["entry"]["anchor"].lower() for r in judged) and any("ferret" in r["entry"]["anchor"].lower() for r in judged), \
        "both new bugs are sampled: %s" % [r["entry"]["anchor"] for r in judged]
    for r in judged:
        for k in ("symptom_quote", "root_cause_quote", "fix_quote", "corrects_quote"):
            q = r["entry"].get(k)
            assert not q or flat(q) in flat(r["section_text"]), "the critic sees the text %s came from (%s)" % (k, r["entry"]["anchor"])
    assert all("- alpha" in p.split("CRITIC ROWS", 1)[0] for p in prompts), "the critic gets the reader's project list"
    t.cli("classify", "start", "--run", "critic-test", "--wait", extra_env=env, check=False)
    out = json.loads(t.cli("critic", "import", "--run", "critic-test", extra_env=env).stdout)
    open(os.path.join(t.home, "state/classify/critic-test/imported"), "w").write("test")
    assert out["rows"] == meta["rows"] and out["missing"] == 0, out
    md = open(os.path.join(t.home, "review/critic-test.md")).read()
    bugs = len([1 for l in open(os.path.join(t.home, "state/classify/critic-test/verdicts.jsonl")) if '"bug"' in l])
    assert "| bug | tech | 0 | %d | 0 | 100%% |" % bugs in md, md[:900]
    assert (bugs >= 10) == bool(out["over_gate"]), "the gate needs ten judged rows: %s %s" % (bugs, out)
    assert "withSession" not in md and "replica reads hung" not in md, "review files carry counts, never note text"


@test
def archive_unmounted_still_succeeds(t):
    r = t.cli("daily", "--if-due", "0", "--no-push", extra_env={"BRAIN_ARCHIVE": os.path.join(t.root, "no-such-drive")})
    rep = json.load(open(os.path.join(t.home, "state/daily-report.json")))
    assert r.returncode == 0 and rep["archive_daily"].startswith("skipped"), rep


@test
def no_index_with_no_refresh(t):
    os.remove(os.path.join(t.home, "index.sqlite"))
    r = t.cli("search", "--no-refresh", "--json", "yak account")
    assert json.loads(r.stdout)[0]["logical"] == "docs/shared.md"


def hook(t, payload):
    env = dict(t.env, BRAIN_TEST_TARGET=BRAIN_PY)
    r = subprocess.run(["/usr/bin/python3", HOOK_PY], input=json.dumps(payload), capture_output=True, text=True,
                       env=env, timeout=120)
    assert r.returncode == 0 and not r.stderr, r.stderr[-800:]
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout.strip() else None


@test
def hook_hands_sessions_past_fixes(t):
    cwd = os.path.join(t.P, "alpha")
    fail = {"session_id": "s1", "cwd": cwd, "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
            "tool_input": {"command": "node worker.js"}, "error": "Exit code 1\nError: withSession reads hung on cold replicas in CDG"}
    got = hook(t, fail)
    assert got and "alpha/docs/bugs.md" in got and "cause:" in got, "a failure gets the past fix: %s" % got
    assert hook(t, fail) is None, "each note is hinted once per session"
    for cmd, err in (("grep -r foo .", "Exit code 1\n"), ("python x.py", "Exit code 1\nModuleNotFoundError: No module named 'yaml'"),
                     ("node a.js", "Exit code 2\nsrc/a.js:12: error replica reads hung withSession cold CDG"),
                     ("node b.js", "Exit code 1\nError: replicas reads hung again"),  # shares only common words with B1
                     ("cat docs/bugs.md", "Exit code 1\nError: withSession reads hung on cold replicas in CDG")):  # read, not run
        assert hook(t, dict(fail, session_id="s2", tool_input={"command": cmd}, error=err)) is None, (cmd, err)
    prompt = {"session_id": "s3", "cwd": cwd, "hook_event_name": "UserPromptSubmit", "prompt": "carry on"}
    intro = hook(t, prompt)
    assert intro and "brain search" in intro and "views/projects/alpha.md" in intro, intro
    assert hook(t, prompt) is None, "a running session gets the note once"
    wt = hook(t, {"session_id": "s7", "cwd": os.path.join(t.P, "alpha-wt"), "hook_event_name": "SessionStart"})
    assert wt and "views/projects/alpha.md" in wt, "a session in a worktree folder gets its repo's findings: %s" % wt
    assert hook(t, {"session_id": "s4", "cwd": t.root, "hook_event_name": "SessionStart"}) is None, "outside Projects: nothing"
    assert hook(t, dict(fail, session_id="s6", cwd=t.root)) is None, "not even a hint outside Projects"
    write(os.path.join(t.P, "scratch-out/kestrel.md"), "# Kestrel\n\n## Kestrel crash\n- Symptom: \"kestrel daemon crashed\"\n"
                                                        "- Cause: the kestrel socket file was stale after a reboot.\n")
    t.cli("build")
    classify(t)
    local = hook(t, dict(fail, session_id="s5", error="Exit code 1\nError: kestrel daemon crashed, socket file stale"))
    assert local and "scratch-out/kestrel.md" in local and "cause:" not in local and "reuse" not in local, \
        "a local-only note gives its location, never its text: %s" % local
    broken = os.path.join(t.root, "broken_brain.py")
    write(broken, "raise RuntimeError('brain broke on import')\n")
    r = subprocess.run(["/usr/bin/python3", HOOK_PY], input=json.dumps(fail), capture_output=True, text=True,
                       env=dict(t.env, BRAIN_TEST_TARGET=broken), timeout=60)
    assert r.returncode == 0 and not r.stdout.strip(), "a broken brain never breaks the session: %s" % r.stderr
    r = t.cli("daily", "--if-due", "0", "--no-push")
    rep = json.load(open(os.path.join(t.home, "state/daily-report.json")))
    assert rep["hook"]["local"]["errors_24h"] >= 1 and "brain broke" in rep["hook"]["local"]["last_error"], rep.get("hook")
    assert rep["hook"]["local"]["hints_24h"] >= 1, rep.get("hook")


@test
def hook_keeps_its_tuned_match_rules(t):
    # The hook judges a note by its first matching lines: on 1,000 real failures, judging by the best lines added 8
    # hints and the new ones were wrong. Here only a late line repeats the error's words, so the hook stays silent.
    write(os.path.join(t.mem, "ibis-fleet.md"), "---\nname: ibis-fleet\ndescription: notes on the ibis fleet\n---\n\n"
          "The ibis fleet runs nightly.\nEach ibis node keeps a cache.\nAn ibis log lands in the bucket.\n"
          "Ask before touching an ibis box.\nOld trace: ibis fleet refreshing tokens reauthentication prompt "
          "noninteractive execution problem current list\n")
    # Rarity counts sections, not passages: a word found in one long note is rare however many passages hold it.
    para = lambda k: ("The zorilla socket handshake fails at warmup when the port file is stale. " if k == 0 else
                      "Paragraph %d still mentions the zorilla. " % k) + "Background filler sentence. " * 45 + "\n\n"
    write(os.path.join(t.mem, "zorilla-daemon.md"), "---\nname: zorilla-daemon\ndescription: the zorilla daemon\n---\n\n"
          + "".join(para(k) for k in range(4)))
    for k in range(3):
        write(os.path.join(t.mem, "socket-filler-%d.md" % k), "---\nname: socket-filler-%d\ndescription: filler\n---\n\n"
              "a socket handshake at warmup, note %d.\n" % (k, k))
    t.cli("build")
    zid = t.db().execute("SELECT count(*) FROM passages p JOIN entries e ON e.id = p.entry JOIN docs d ON d.id = e.doc "
                         "WHERE d.logical = 'memory/zorilla-daemon.md'").fetchone()[0]
    assert zid >= 3, "the long note is searched in passages: %d" % zid
    late = {"session_id": "s8", "cwd": os.path.join(t.P, "alpha"), "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
            "tool_input": {"command": "ibisctl fleet list"}, "error": "Exit code 1\nERROR: (ibis.fleet.list) There was a "
            "problem refreshing your current tokens: Reauthentication failed. cannot prompt during noninteractive execution."}
    assert hook(t, late) is None, "a note matching only on a late line gets no hint"
    rare = dict(late, session_id="s9", tool_input={"command": "zorillactl start"},
                error="Exit code 1\nError: zorilla socket handshake refused during warmup")
    got = hook(t, rare)
    assert got and "zorilla-daemon.md" in got, "a word in one note's several passages is still rare: %s" % got


@test
def rebuild_reuses_its_caches(t):
    t.cli("build")
    again = json.loads(t.cli("build").stdout)
    assert again.get("mentions_computed", 0) == 0, "a build with nothing changed recomputes no mentions: %s" % again


@test
def index_from_an_older_schema_is_rebuilt(t):
    t.cli("build")
    db = sqlite3.connect(os.path.join(t.home, "index.sqlite"))
    db.execute("DROP TABLE items")
    db.execute("UPDATE meta SET value='old' WHERE key='schema'")
    db.commit()
    db.close()
    r = t.cli("search", "--no-refresh", "--json", "yak account", check=False)
    assert r.returncode == 0 and json.loads(r.stdout)[0]["logical"] == "docs/shared.md", \
        "a search never runs on an index built by other code: %s %s" % (r.stdout[:200], r.stderr[-300:])


@test
def git_failure_fails_closed(t):
    with open(os.path.join(t.P, ".git/index"), "wb") as fh:
        fh.write(b"garbage, not an index")
    r = json.loads(t.cli("build").stdout)
    assert r.get("git_state_unknown", 0) >= 1, r
    docs = {l: (st, sens) for l, st, sens in t.db().execute("SELECT logical, git_state, sensitive FROM docs")}
    assert docs["docs/private.md"] == ("unknown", 1) and docs["untracked-note.md"] == ("unknown", 1), docs


def main():
    only = sys.argv[sys.argv.index("-k") + 1] if "-k" in sys.argv else None
    t = Env()
    failed = 0
    try:
        for fn in RESULTS:
            if only and only not in fn.__name__:
                continue
            start = time.time()
            try:
                fn(t)
                print("PASS %-26s %.1fs" % (fn.__name__, time.time() - start))
            except AssertionError as exc:
                failed += 1
                print("FAIL %-26s %s" % (fn.__name__, str(exc)[:1500]))
    finally:
        if "--keep" in sys.argv:
            print("fixture kept at", t.root)
        else:
            shutil.rmtree(t.root, ignore_errors=True)
    ran = len([f for f in RESULTS if not only or only in f.__name__])
    print("%d passed, %d failed, 0 skipped" % (ran - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
