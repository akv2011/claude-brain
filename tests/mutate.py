#!/usr/bin/python3
"""Prove the integration tests can fail: break one rule at a time in a copy of brain.py, run the suite against
the copy, and require at least one FAIL per mutation. Run: /usr/bin/python3 tests/mutate.py [-k <name part>]"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
SRC = open(os.path.join(os.path.dirname(HERE), "brain.py")).read()
HOOK_SRC = open(os.path.join(os.path.dirname(HERE), "hook.py")).read()
# Mutations of hook.py; the rest mutate brain.py.
HOOK_MUTATIONS = {
    "intro on every prompt": ('(event == "UserPromptSubmit" and not state["intro"])', '(event == "UserPromptSubmit")'),
    "hint repeated in a session": ('    if hint and hint["key"] not in state["hinted"]:', '    if hint:'),
    "hook outside Projects": ('    if not (cwd + "/").startswith(os.path.realpath(brain.PROJECTS) + "/"):\n        return None', '    if False:\n        return None'),
}
MUTATIONS = {
    "no fence tracking": ('FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")', 'FENCE_RE = re.compile(r"(?!x)x")'),
    "no worktree folding": ("        if main == top_real:\n            self.main_walk = walk_top",
                            "        if True:\n            self.main_walk = walk_top"),
    "ignored folder not inherited": ('                return "ignored"\n        return "tracked"', '                pass\n        return "tracked"'),
    "rg partial exit dropped": ("ok=(0, 1, 2)) or \"\"", "ok=(0,)) or \"\""),
    "fts tokens unquoted": ("out.append('\"' + \" \".join(toks) + '\"')", "out.append(p)"),
    "test output not pruned": ('"test-results", ', ''),
    "never refreshes": ('    return "ok" if time.time() - os.path.getmtime(INDEX) < STALE_SECONDS else "stale"', '    return "ok"'),
    "bytes replaced on read": ('return raw.decode("utf-8", errors="surrogateescape")', 'return raw.decode("utf-8", errors="replace")'),
    "copies not deduped": ("        if ehash in seen_text:", "        if False:"),
    "quote check off": ("            if qnorm(v) not in text:", "            if False:"),
    "partial re-read off": ('            mode = "facets" if facets else ("full" if changed > 0.5 * len(text) else "partial")',
                            '            mode = "facets" if facets else "full"'),
    "local rows pushed": ('    for name, rows in (("notes.jsonl", [r for r in catalog.values() if not r.get("sensitive")]),',
                          '    for name, rows in (("notes.jsonl", list(catalog.values())),'),
    "git failure fails open": ('        if self._failed:\n            return "unknown"', '        if False:\n            return "unknown"'),
    "secret quotes kept": ("            if v and SECRET_RE.search(v):", "            if False:"),
    "dead runs never restarted": ("            elif not st.get(\"alive\"):\n                open(restarts_path", "            elif False:\n                open(restarts_path"),
    "titles read fences": ("        if fm:\n            fence = fm.group(1)\n            continue\n        m = HEAD_RE.match(line)",
                           "        if False:\n            fence = fm.group(1)\n            continue\n        m = HEAD_RE.match(line)"),
    "secret model text kept": ("        if out[k] and SECRET_RE.search(out[k]):", "        if False:"),
    "tags not grounded": ("        if v in keep or v in present or", "        if True or"),
    "no quote repair": ("    return best if best_r >= 0.9 else None", "    return None"),
    "no child scope": ('        scopes[k] = "".join(parts)', '        scopes[k] = parts[0]'),
    "empty entries kept": ('    if not (out["root_cause"] or out["fix_quote"] or out["reuse"]):', '    if False:'),
    "foreign tech in reuse kept": ('        if named - src["read_tech"]:', "        if False:"),
    "repeated number replaces first": ('files.setdefault(x.get("n"), x)', 'files.__setitem__(x.get("n"), x)'),
    "reread misses not requeued": ('                catalog[logical]["reread"] = True', '                pass'),
    "requeue flag ignored by pack": ('        fresh = reread or only is not None or bool(row and row.get("reread"))',
                                     '        fresh = reread or only is not None'),
    "pack --only reads everything": ("        if only is not None and logical not in only:\n            continue", ""),
    "tech grounded in section only": ('keep=src["read_tech"])  # named anywhere', 'keep=())  # named anywhere'),
    "reuse checked against section only": ('        if named - src["read_tech"]:',
                                           '        if named - {cid for f, cid in vocab.mentions(scope_raw) if f == "tech"}:'),
    "copied-from projects ignored": ('                             | src["copied_from"])', '                             )'),
    "copied-from projects ignored by files": ('                                               | vocab.copied_from(logical)),',
                                              '                                               ),'),
    "wrapped dated line splits": (' and not (body[:1] in "[2" and WRAPPED_RE.match(prev))', ''),
    "punctuation aliases need a word edge": ('(?:(?<![A-Za-z0-9])|(?![A-Za-z0-9]))"', '(?![A-Za-z0-9])"'),
    "one item per section": ('acc["entries"][(src["ehash"], cleaned["item"])] = cleaned', 'acc["entries"][(src["ehash"], 1)] = cleaned'),
    "stale items kept": (' and e["ehash"] not in acc["seen"]}', '}'),
    "kind filter reads the first item": ("ITEM_OF = \"EXISTS (SELECT 1 FROM items i WHERE i.entry = e.id AND i.type = '%s')\"",
                                         "ITEM_OF = \"e.type = '%s'\""),
    "maps list the first item only": ('"AND e.copy_path IS NULL ORDER BY e.date DESC, d.logical, e.line, i.item")',
                                      '"AND e.copy_path IS NULL AND i.item = 1 ORDER BY e.date DESC, d.logical, e.line, i.item")'),
    "index schema not checked": ('    if not row or row[0] != SCHEMA_VERSION:', '    if not row:'),
    "alias order unstable": ('key=lambda x: (-len(x), x)))', 'key=len, reverse=True))'),
    "hints without a rare shared word": (" and any(rare(w) for w in shared)", ""),
    "hints on read-only commands": ("    if command and HINT_READONLY_RE.search(command):\n        return None", "    if False:\n        return None"),
    "monthly critic never packed": ("        if not running and load_catalog() and not os.path.exists(os.path.join(runs_dir, critic_run)):",
                                    "        if False:"),
    "reading cycle never runs": ("    if not full and age(read_path) < read_every_hours * 3600:\n        return 0", "    if not full:\n        return 0"),
    "reading cycle runs the full job": ('        if not full:\n            report["views"] = views()', '        if False:\n            report["views"] = views()'),
    "memory notes left out of findings": ('        for p in (catalog.get(logical) or {}).get("projects", []):', '        for p in []:'),
    "local-only hint text shown": ('                if h["sensitive"]:  # a local-only', '                if False:  # a local-only'),
    "drop alarm silenced": ('        if returned >= 10 and stats.get("entries_dropped_empty", 0) > 0.1 * returned:', '        if False:'),
    "reuse needs a quote": ('    out["reuse"] = e.get("reuse") or None',
                            '    out["reuse"] = (e.get("reuse") or None) if any(out[k] for k in QUOTE_FIELDS) else None'),
    "lint ignores subsection scope": ('            body = qnorm(clean(scopes[str(src["n"])]))', '            body = qnorm(clean(src["text"]))'),
    "copy of a copy takes the outer from-": ('p = self.fold(segs[-1], "project") if segs else None', 'p = self.fold(segs[0], "project") if segs else None'),
    "session names are not names": ('        names += [("project", pid, s) for pid, p in reg["projects"].items() for s in p.get("sessions", [])]\n', ''),
    "session names ignore case": (' or a.isupper() or a in sessions) else loose)', ' or a.isupper()) else loose)'),
    "critic sees 12k of a section": ('CRITIC_SECTION_CHARS = 40000', 'CRITIC_SECTION_CHARS = 12000'),
    "critic sees the section without subsections": ('        return {e["ehash"]: scoped[k] for k, e in numbered.items()}',
                                                    '        return {e["ehash"]: e["text"] for k, e in numbered.items()}'),
    "critic gets no project list": ('    brief = CRITIC_BRIEF + project_list(load_registry()) + "\\nCRITIC ROWS\\n"',
                                    '    brief = CRITIC_BRIEF + "\\nCRITIC ROWS\\n"'),
    "ponytail scan from any cwd": ("PROJECTS], cwd=PROJECTS, timeout=300,", "PROJECTS], timeout=300,"),
    "brain indexes itself": ("    if os.path.realpath(p).startswith(_REAL_PROJECTS + os.sep)}", "    if False}"),
    "exclude setting ignored": ('PRUNE_PATHS = set(setting_list("exclude", "BRAIN_EXCLUDE", []))', "PRUNE_PATHS = set()"),
    "worktree folder not folded": ('        while base.lower() not in self.folders and "-" in base:', "        while False:"),
    "passage: none": ("PASSAGE_CHARS = 2000", "PASSAGE_CHARS = 10 ** 9"),
    "passage: line ignored": ("        text, line = ptext, pline", "        text = ptext"),
    "passage: copies each a hit": ("        if eid not in winner:  # another worktree copy of a section that has a hit\n            continue",
                                   "        if False:\n            continue"),
    "passage: no compact list": ("        if i > FULL_HITS:", "        if False:"),
    "passage: hook matches best lines": ("                        best_lines=False)[1]:", "                        best_lines=True)[1]:"),
    "passage: rarity counts passages": ('"SELECT count(DISTINCT p.entry) FROM fts', '"SELECT count(p.entry) FROM fts'),
    "code: src headers skipped": ('SCRIPT_DIRS = {"scripts", "bin", "tools", "crawl", "src"}', 'SCRIPT_DIRS = {"scripts", "bin", "tools", "crawl"}'),
    "code: extensionless scripts skipped": ('and not (in_script_dir and "." not in f and shebang(path)):', 'and not False:'),
    "code: src code lines indexed": ("header = script_header(path, code_fallback=not in_src)", "header = script_header(path)"),
    "no frontmatter description": ('        description = meta.get("description") or cat.get("description") or ""', '        description = ""'),
}


def main():
    # Mutate a private copy of the code: a brain folder synced to a second machine would race a mutant written in it,
    # and tests copied away from their folder lose the paths they need.
    root = tempfile.mkdtemp(prefix="brain-mutate-")
    code = os.path.join(root, "brain")
    for rel in ("brain.py", "hook.py", "runner.py", "schema/registry-extra.json", "tests/run.py", "tests/stub_claude.py"):
        os.makedirs(os.path.dirname(os.path.join(code, rel)), exist_ok=True)
        shutil.copy2(os.path.join(os.path.dirname(HERE), rel), os.path.join(code, rel))
    env = {k: v for k, v in os.environ.items() if not k.startswith("BRAIN_TEST_")}
    suite = lambda: subprocess.run(["/usr/bin/python3", os.path.join(code, "tests/run.py")], capture_output=True,
                                   text=True, env=env, timeout=900).stdout
    base = [l for l in suite().splitlines() if l.startswith("FAIL")]
    if base:
        print("the unmutated copy fails, so no mutation result would mean anything:\n" + "\n".join(base))
        return 2
    survived = []
    jobs = [(n, o, w, "brain.py", SRC) for n, (o, w) in MUTATIONS.items()] + \
           [(n, o, w, "hook.py", HOOK_SRC) for n, (o, w) in HOOK_MUTATIONS.items()]
    only = sys.argv[sys.argv.index("-k") + 1] if "-k" in sys.argv else None  # -k <text>: only mutations named with it
    jobs = [j for j in jobs if not only or only in j[0]]
    for name, old, new, target, src in jobs:
        assert src.count(old) == 1, "mutation anchor not found once: " + name
        with open(os.path.join(code, target), "w") as fh:
            fh.write(src.replace(old, new))
        fails = [l.split()[1] for l in suite().splitlines() if l.startswith("FAIL")]
        with open(os.path.join(code, target), "w") as fh:
            fh.write(src)
        print("%-32s %s" % (name, ("caught by " + ", ".join(fails)) if fails else "SURVIVED"))
        if not fails:
            survived.append(name)
    shutil.rmtree(root, ignore_errors=True)
    print("%d mutations, %d caught, %d survived" % (len(jobs), len(jobs) - len(survived), len(survived)))
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
