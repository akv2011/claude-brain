#!/usr/bin/python3
"""Brain: search every note under a projects folder and that folder's Claude Code memory.

Notes stay where they are. `build` walks them, folds git worktree copies onto one logical path,
splits every file into entries at headings and dated lines (losslessly), adds commit messages,
script headers, `ponytail:` comments and text deleted from notes, merges the classified fields
from catalog/, and writes index.sqlite. `search` answers from that index. Design: docs/DESIGN.md.
Stdlib only, Python 3.9+, macOS or Linux. Settings: config.json beside this file (config.example.json lists them).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
HOME = os.path.expanduser("~")
CONFIG_PATH = os.environ.get("BRAIN_CONFIG") or os.path.join(HERE, "config.json")
CONFIG = json.load(open(CONFIG_PATH)) if os.path.exists(CONFIG_PATH) else {}


def setting(key, env, default=None):
    """An environment variable, else config.json, else the default."""
    return os.environ[env] if env and env in os.environ else CONFIG.get(key, default)


def setting_list(key, env, default):
    v = setting(key, env, default)
    return [os.path.expanduser(p) for p in (v.split(os.pathsep) if isinstance(v, str) else v) if p]


# The projects folder defaults to the one this code sits in; its Claude Code memory folder is the one Claude Code
# keeps for that path (every character but a letter or digit becomes "-").
PROJECTS = os.path.abspath(os.path.expanduser(setting("projects_root", "BRAIN_PROJECTS", os.path.dirname(HERE))))
MEMORY = os.path.abspath(os.path.expanduser(setting("memory_dir", "BRAIN_MEMORY", os.path.join(
    HOME, ".claude/projects", re.sub(r"[^A-Za-z0-9]", "-", PROJECTS), "memory"))))
CLAUDE_DIR = os.path.join(HOME, ".claude")
CLAUDE_FILES = setting_list("claude_files", "BRAIN_CLAUDE_FILES", [os.path.join(CLAUDE_DIR, "CLAUDE.md")])
BRAIN = os.path.abspath(os.environ.get("BRAIN_HOME", HERE))
STATE = os.path.join(BRAIN, "state")
INDEX = os.path.join(BRAIN, "index.sqlite")
CATALOG = os.path.join(BRAIN, "catalog")
REGISTRY = os.path.join(BRAIN, "schema/registry.json")
REGISTRY_EXTRA = os.path.expanduser(setting("registry_extra", "BRAIN_REGISTRY_EXTRA", os.path.join(HERE, "schema/registry-extra.json")))
STALE_SECONDS = int(os.environ.get("BRAIN_STALE_SECONDS", "600"))
# ponytail comments and script files change rarely and cost 13 s of a 27 s build to scan; a search-time rebuild reuses
# the last scan for this long, and `brain.py build` and the daily job always rescan.
SLOW_SCAN_SECONDS = int(os.environ.get("BRAIN_SLOW_SCAN_SECONDS", "3600"))
GIT = "/usr/bin/git" if os.path.exists("/usr/bin/git") else "git"
RG = next((p for p in ("/opt/homebrew/bin/rg", "/usr/local/bin/rg", "/usr/bin/rg") if os.path.exists(p)), None)

PRUNE_NAMES = {"node_modules", ".git", ".venv", "venv", "site-packages", ".next", ".open-next", ".wrangler",
               "dist", "__pycache__", ".pytest_cache", ".cache", ".turbo", ".vercel",
               "test-results", "playwright-report", "blob-report", "coverage", ".playwright-mcp", "htmlcov"}
# Folders left out by path: third-party clones (config "exclude"), and the brain's own code and data, whose plans and
# research would outrank the notes they describe.
_REAL_PROJECTS = os.path.realpath(PROJECTS)  # a projects_root reached through a symlink still contains the brain
PRUNE_PATHS = set(setting_list("exclude", "BRAIN_EXCLUDE", [])) | {
    os.path.relpath(os.path.realpath(p), _REAL_PROJECTS) for p in (HERE, BRAIN)
    if os.path.realpath(p).startswith(_REAL_PROJECTS + os.sep)}
NOTE_EXT = (".md", ".mdx")
SCRIPT_EXT = (".py", ".sh", ".ts", ".mjs", ".js", ".cjs")
SCRIPT_DIRS = {"scripts", "bin", "tools", "crawl"}
SOURCE_WEIGHT = {"note": 1.0, "memory": 1.0, "claude": 1.0, "commit": 0.85, "script": 0.85,
                 "ponytail": 0.85, "removed": 0.4}
BIG_ENTRY = 20000
VENDORED_DIR_FILES = 200
STOPWORDS = set("""a an and are as at be but by can could did do does doing for from had has have how i if in
into is it its me my no not of on or our so that the their them then there these this those to too u ur us
was we were what when where which who why will with would you your why whats hows ok okay pls please""".split())

FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEAD_RE = re.compile(r"^(#{1,4})[ \t]+(\S.*?)[ \t#]*$")
DATE_RE = re.compile(r"(?<!\d)(20\d\d)-([01]\d)-([0-3]\d)(?!\d)")
DATED_LINE_RE = re.compile(r"^(?:[-*+][ \t]+|>[ \t]*)?(?:\*\*)?\[?(20\d\d-[01]\d-[0-3]\d)(?!\d)")
# A prose line that stops mid-sentence: a bare dated line after it is the sentence wrapping, not a log row.
# Measured 2026-10-26: all 115 such cases in the notes were wrapped prose; log rows follow a dated, blank or ended line.
WRAPPED_RE = re.compile(r"^(?![ \t]*(?:#|[-*+>|][ \t]|\d+\.[ \t]|\[?20\d\d-\d\d-\d\d)).*[^\s.!?:;)\]|`\"'*]$")
FRONTMATTER_RE = re.compile(r"\A(?:﻿)?---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
CORRECTION_RE = re.compile(r"(?i)\b(corrected[ :]+20\d\d-\d\d-\d\d|was wrong|my wrong|turned out|superseded"
                           r"|retract(?:ed|ion)?|not true|was mistaken|correction:|wrong cause)")
SCRIPT_REF_RE = re.compile(r"(?<![\w/.~-])((?:[\w.-]+/)*(?:scripts|bin|tools|crawl|src|lib)/[\w./-]*?\w\.(?:py|sh|ts|mjs|js|cjs))\b")
SECRET_RE = re.compile(r"\bghp_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\bsk-[A-Za-z0-9_-]{20,}|\bAKIA[0-9A-Z]{16}"
                       r"|-----BEGIN [A-Z ]*PRIVATE KEY|\bxox[bap]-[A-Za-z0-9-]{10,}|\beyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{20,}"
                       r"|(?i:\b(?:password|passwd|secret|api[_-]?key|token|bearer)\s*[=:]\s*['\"]?(?![<${])"
                       r"(?=[A-Za-z0-9._~+/-]*\d)(?=[A-Za-z0-9._~+/-]*[A-Za-z])[A-Za-z0-9._~+/-]{16,})|\bcfat_[A-Za-z0-9_-]{20,}"
                       r"|\b[0-9a-fA-F]{64}\b")
IPV4_RE = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
WIKILINK_RE = re.compile(r"\[\[([^\]|#\n]+)")
MDLINK_RE = re.compile(r"\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")


def log(msg):
    print(msg, file=sys.stderr)


def read_text(path):
    """Decode with surrogateescape so every byte survives a round trip (lossless split check)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    return raw.decode("utf-8", errors="surrogateescape")


def clean(text):
    """Text for SQLite and output: undecodable bytes become U+FFFD."""
    return text.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")


def sha(text):
    return hashlib.sha256(text.encode("utf-8", errors="surrogateescape")).hexdigest()


def valid_date(y, m, d):
    try:
        return date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def first_date(text):
    for m in DATE_RE.finditer(text):
        iso = valid_date(*m.groups())
        if iso:
            return iso
    return None


def run(args, cwd=None, timeout=120, ok=(0,)):
    """stdout of a command, or None when it fails. rg exits 2 on a partial error (a symlink loop, an unreadable
    file) after printing every match it found, so its callers pass ok=(0, 1, 2)."""
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode not in ok:
        return None
    return r.stdout.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- registry

def build_registry():
    """registry.json = project names parsed from docs/repos.md + the hand-kept registry-extra.json."""
    extra = json.load(open(REGISTRY_EXTRA))
    tech_names = {a.lower() for t, al in extra["tech"].items() for a in [t] + al} | {"anilist", "claude"}
    projects = {}
    repos_md = os.path.join(PROJECTS, "docs/repos.md")
    if os.path.exists(repos_md):
        for line in open(repos_md, encoding="utf-8"):
            m = re.match(r"^\|\s*`([^`]+)`\s*\|([^|]*)\|", line)
            if not m:
                continue
            folder, names = m.group(1).strip(), m.group(2)
            pid = folder.lower()
            if pid in projects or folder in PRUNE_PATHS:
                continue
            entry = projects.setdefault(pid, {"folder": folder, "aliases": [], "source": "docs/repos.md"})
            entry["about"] = re.split(r"(?<=\.)\s", re.sub(r"\s+", " ", re.sub(r"\*\*|`", "", names)).strip())[0][:120]
            for part in re.split(r"[,;:]", names)[:2]:
                part = re.sub(r"\*\*|\(.*?\)", "", part).strip()
                code = re.match(r"^`([^`]+)`$", part)
                name = code.group(1) if code else part
                if (code or re.fullmatch(r"[A-Z][A-Za-z0-9]+(?: [A-Z][A-Za-z0-9]+){0,2}|[a-z0-9-]+\.(?:com|me|net|org|io|app)", name)) \
                        and name.lower() not in tech_names and name not in entry["aliases"]:
                    entry["aliases"].append(name)
    for parent in (PROJECTS, os.path.join(PROJECTS, "Projects")):
        if not os.path.isdir(parent):
            continue
        for name in sorted(os.listdir(parent)):
            path = os.path.join(parent, name)
            rel = os.path.relpath(path, PROJECTS)
            if name.startswith(".") or name in PRUNE_NAMES or rel in PRUNE_PATHS or not os.path.isdir(path) \
                    or os.path.isfile(os.path.join(path, ".git")) or (parent == PROJECTS and name == "Projects"):
                continue
            projects.setdefault(name.lower(), {"folder": name, "aliases": [], "source": "folder"})
    for pid, extra_p in extra.get("project_aliases", {}).items():
        entry = projects.setdefault(pid, {"folder": pid, "aliases": [], "source": ""})
        for a in extra_p["aliases"]:
            if a not in entry["aliases"]:
                entry["aliases"].append(a)
        entry["source"] = "; ".join(s for s in (entry["source"], extra_p.get("source", "")) if s)
        if extra_p.get("sessions"):
            entry["sessions"] = extra_p["sessions"]
    os.makedirs(os.path.dirname(REGISTRY), exist_ok=True)
    reg = {"_about": "Generated by brain.py registry from docs/repos.md and schema/registry-extra.json. Do not edit; edit those.",
           "projects": dict(sorted(projects.items())), "tech": extra["tech"], "domains": extra["domains"]}
    with open(REGISTRY + ".tmp", "w") as fh:
        json.dump(reg, fh, indent=1, ensure_ascii=False)
    os.replace(REGISTRY + ".tmp", REGISTRY)
    return reg


def load_registry():
    if not os.path.exists(REGISTRY) or os.path.getmtime(REGISTRY) < os.path.getmtime(REGISTRY_EXTRA):
        return build_registry()
    return json.load(open(REGISTRY))


class Vocab:
    """Every name for every project and technology, compiled once."""

    def __init__(self, reg):
        self.reg = reg
        self.canon = {}      # lowercase alias -> (facet, canonical)
        self.keyed = {}      # normalized alias -> {(facet, canonical)}
        exact, loose, dotted = [], [], []
        names = [("project", pid, a) for pid, p in reg["projects"].items() for a in [p.get("folder", pid)] + p["aliases"]]
        names += [("tech", tid, a) for tid, aliases in reg["tech"].items() for a in [tid] + aliases]
        # A session name (Shop_Sync) names its project; matched with its case, so shop_tags.py is not one.
        sessions = {s for p in reg["projects"].values() for s in p.get("sessions", [])}
        names += [("project", pid, s) for pid, p in reg["projects"].items() for s in p.get("sessions", [])]
        for facet, cid, a in names:
            self.canon.setdefault(a.lower(), (facet, cid))
            self.keyed.setdefault(self.norm(a), set()).add((facet, cid))
            esc = re.escape(a).replace(r"\ ", r"[\s_-]+")
            (dotted if a.startswith(".") else exact if (len(a) <= 2 or a.isupper() or a in sessions) else loose).append(esc)
        # Word edges; an alias that ends in punctuation (flex:) ends its own word, so "flex:1" still matches.
        edge = r"(?<![A-Za-z0-9])(%s)(?:(?<![A-Za-z0-9])|(?![A-Za-z0-9]))"
        by_len = lambda xs: "|".join(sorted(set(xs), key=lambda x: (-len(x), x)))  # stable: the mentions cache keys on it
        self.rx_exact = re.compile(edge % by_len(exact)) if exact else None
        self.rx_loose = re.compile(edge % by_len(loose), re.I) if loose else None
        self.rx_dotted = re.compile(r"(%s)(?![A-Za-z0-9])" % by_len(dotted), re.I) if dotted else None
        self.folders = {p.get("folder", pid).lower(): pid for pid, p in reg["projects"].items()}

    @staticmethod
    def norm(a):
        return re.sub(r"[\s_-]+", " ", a.strip().lower())

    def mentions(self, text):
        found = set()
        for rx in (self.rx_exact, self.rx_loose, self.rx_dotted):
            if rx is None:
                continue
            for m in rx.finditer(text):
                found |= self.keyed.get(self.norm(m.group(1)), set())
        return found

    def project_for_path(self, logical):
        """Projects/<folder>/... -> project id; memory and shared docs get their own buckets."""
        parts = logical.split("/")
        if parts[0] == "memory":
            return "memory"
        if parts[0] == "claude":
            return "claude-config"
        if parts[0] == "Projects" and len(parts) > 2:
            return parts[1].lower()
        if len(parts) == 1 or parts[0] == "docs":
            return "projects-shared"
        top = base = parts[0]
        while base.lower() not in self.folders and "-" in base:  # a worktree folder named <repo>-<branch>
            base = base.rsplit("-", 1)[0]
        return self.folders.get(base.lower()) or top.lower()

    def copied_from(self, logical):
        """The project a copied note was written in: its innermost `from-<project>/` folder. A note under
        reader/docs/from-shop/from-blog/ is a blog note that shop passed on."""
        segs = [seg[5:] for seg in logical.split("/")[:-1] if seg.lower().startswith("from-")]
        p = self.fold(segs[-1], "project") if segs else None
        return {p} if p and not p.startswith("other:") else set()

    def fold(self, value, facet):
        """A model-written tech or project value -> canonical id, or other:<value>."""
        v = value.strip().lower()
        if v.startswith("other:"):
            v = v[6:].strip()
        if not v:
            return None
        known = self.reg["tech"] if facet == "tech" else self.reg["projects"]
        for cand in (v, v.replace("-", " "), v.replace(" ", "-"), v.replace("_", "-"), v.replace("-", "_"),
                     v.replace(" ", "_")):
            hit = self.canon.get(cand)
            if hit and hit[0] == facet:
                return hit[1]
            if cand in known:
                return cand
        return "other:" + re.sub(r"[^a-z0-9.+-]+", "-", v).strip("-")


# ---------------------------------------------------------------- parsing

def frontmatter(text):
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, 0
    meta = {}
    for line in m.group(1).splitlines():
        mm = re.match(r"^\s*(name|description|type|modified)\s*:\s*(.*?)\s*$", line)
        if mm and mm.group(1) not in meta:
            meta[mm.group(1)] = mm.group(2).strip().strip("'\"")
    return meta, m.end()


def split_entries(text):
    """Split a note into entries at H1-H3 headings and dated lines, outside code fences.

    Lossless: every line lands in exactly one entry, so "".join(e["text"]) == text.
    H4 headings start a new entry only inside an entry that has grown past BIG_ENTRY chars.
    """
    entries = []
    cur = None
    fence = None
    stack = {}
    lineno = 0
    prev = ""
    for line in text.splitlines(keepends=True):
        lineno += 1
        body = line.rstrip("\r\n")
        boundary = None
        fm = FENCE_RE.match(body)
        if fence:
            if fm and fm.group(1)[0] == fence[0] and len(fm.group(1)) >= len(fence) and body.strip() == fm.group(1):
                fence = None
        elif fm:
            fence = fm.group(1)
        else:
            hm = HEAD_RE.match(body)
            if hm:
                level = len(hm.group(1))
                if level <= 3 or (cur is not None and sum(len(l) for l in cur["lines"]) > BIG_ENTRY):
                    boundary = ("heading", level, hm.group(2))
            elif DATED_LINE_RE.match(body) and not (body[:1] in "[2" and WRAPPED_RE.match(prev)):
                boundary = ("dated", 0, body.strip())
        if boundary or cur is None:
            if cur is not None:
                entries.append(cur)
            if boundary and boundary[0] == "heading":
                level = boundary[1]
                stack = {k: v for k, v in stack.items() if k < level}
                path = [stack[k] for k in sorted(stack)]
                stack[level] = boundary[2]
                anchor = body.strip()
            elif boundary:
                path = [stack[k] for k in sorted(stack)]
                anchor = boundary[2][:160]
            else:
                path, anchor = [], "(top)"
            cur = {"anchor": anchor, "heading_path": path, "line": lineno, "lines": [],
                   "dated": bool(boundary and boundary[0] == "dated")}
        cur["lines"].append(line)
        prev = body
    if cur is not None:
        entries.append(cur)
    for i, e in enumerate(entries, 1):
        e["n"] = i
        e["text"] = "".join(e.pop("lines"))
        e["ehash"] = sha(e["text"])
        e["date"] = first_date(e["anchor"]) if e["anchor"] != "(top)" else None
    return entries


def title_of(text, fallback):
    """The first level-1 heading outside a code fence, at column 0."""
    fence = None
    for line in text.splitlines():
        fm = FENCE_RE.match(line)
        if fence:
            if fm and fm.group(1)[0] == fence[0] and len(fm.group(1)) >= len(fence):
                fence = None
            continue
        if fm:
            fence = fm.group(1)
            continue
        m = HEAD_RE.match(line)
        if m and len(m.group(1)) == 1:
            return m.group(2).strip()
    return fallback


# ---------------------------------------------------------------- discovery

class Repo:
    """One git worktree (or main checkout) the walk found notes in."""

    def __init__(self, top_real, common_real, walk_top):
        self.top_real, self.common_real, self.walk_top = top_real, common_real, walk_top
        main = os.path.dirname(common_real) if os.path.basename(common_real) == ".git" else common_real
        proj_real = os.path.realpath(PROJECTS)
        if main == top_real:
            self.main_walk = walk_top
        elif (main + "/").startswith(proj_real + "/") or main == proj_real:
            self.main_walk = os.path.relpath(main, proj_real)
            if self.main_walk == ".":
                self.main_walk = ""
        else:
            self.main_walk = walk_top
        self._status = None
        self._failed = False

    def state(self, in_repo):
        """'tracked' | 'untracked' | 'modified' | 'ignored' (by name or by an ignored parent folder)."""
        st = self.status()
        if in_repo in st:
            return st[in_repo]
        if self._failed:
            return "unknown"
        for d in self._ignored_dirs:
            if in_repo.startswith(d):
                return "ignored"
        return "tracked"

    def status(self):
        """path relative to this worktree -> 'untracked' | 'modified' | 'ignored'. Git lists a wholly ignored
        folder as one `dir/` line; those prefixes go to _ignored_dirs."""
        if self._status is None:
            self._status = {}
            self._ignored_dirs = []
            out = run([GIT, "-C", self.top_real, "status", "--porcelain=v1", "-z", "--ignored=matching", "-uall"],
                      timeout=120)
            self._failed = out is None
            out = out or ""
            items = out.split("\0")
            i = 0
            while i < len(items):
                it = items[i]
                i += 1
                if len(it) < 4:
                    continue
                code, path = it[:2], it[3:]
                if code[0] in "RC":
                    i += 1
                state = {"??": "untracked", "!!": "ignored"}.get(code, "modified")
                if path.endswith("/"):
                    if state == "ignored":
                        self._ignored_dirs.append(path)
                    continue
                self._status[path] = state
        return self._status


class Corpus:
    def __init__(self):
        self.repos = {}        # realpath dir -> Repo or None
        self.files = []        # physical note files
        self.walk_errors = []

    def repo_for_dir(self, real_dir, walk_dir):
        """The worktree holding real_dir: nearest ancestor with a .git entry, one rev-parse per worktree."""
        if real_dir in self.repos:
            return self.repos[real_dir]
        top = real_dir
        while not os.path.exists(os.path.join(top, ".git")):
            parent = os.path.dirname(top)
            if parent == top:
                top = None
                break
            top = parent
        repo = None
        if top is not None:
            repo = self.repos.get(("top", top))
            if repo is None and ("top", top) not in self.repos:
                out = run([GIT, "-C", top, "rev-parse", "--path-format=absolute", "--git-common-dir"])
                if out and out.strip():
                    rel_inside = os.path.relpath(real_dir, top)
                    walk_top = walk_dir if rel_inside == "." else walk_dir[: -len(rel_inside)].rstrip("/")
                    repo = Repo(top, os.path.realpath(out.strip()), walk_top)
                self.repos[("top", top)] = repo
        self.repos[real_dir] = repo
        return repo

    def walk(self):
        seen = set()
        for root, dirs, files in os.walk(PROJECTS, followlinks=True):
            real = os.path.realpath(root)
            if real in seen:
                dirs[:] = []
                continue
            seen.add(real)
            rel = os.path.relpath(root, PROJECTS)
            rel = "" if rel == "." else rel
            dirs[:] = sorted(d for d in dirs if d not in PRUNE_NAMES
                             and (os.path.join(rel, d) if rel else d) not in PRUNE_PATHS)
            for f in sorted(files):
                if f.lower().endswith(NOTE_EXT):
                    self.files.append(self.describe(os.path.join(root, f), rel, real, "note"))
        if os.path.isdir(MEMORY):
            for f in sorted(os.listdir(MEMORY)):
                if f.lower().endswith(".md"):
                    p = os.path.join(MEMORY, f)
                    self.files.append({"abspath": p, "phys": "memory/" + f, "logical": "memory/" + f,
                                       "source": "memory", "repo": None, "git": "norepo"})
        for item in CLAUDE_FILES:
            paths = [item] if os.path.isfile(item) else [
                os.path.join(r, f) for r, _, fs in os.walk(item) for f in sorted(fs) if f.lower().endswith(".md")]
            for p in sorted(paths):
                if (p + "/").startswith(CLAUDE_DIR + "/"):
                    rel = "claude/" + os.path.relpath(p, CLAUDE_DIR)
                elif os.path.isfile(item):
                    rel = "claude/" + os.path.basename(p)
                else:
                    rel = "claude/" + os.path.join(os.path.basename(item), os.path.relpath(p, item))
                self.files.append({"abspath": p, "phys": rel, "logical": rel, "source": "claude", "repo": None,
                                   "git": "norepo"})

    def describe(self, path, rel_dir, real_dir, source):
        phys = os.path.join(rel_dir, os.path.basename(path)) if rel_dir else os.path.basename(path)
        repo = self.repo_for_dir(real_dir, rel_dir)
        info = {"abspath": path, "phys": phys, "source": source, "repo": repo}
        if repo is None:
            info["logical"], info["git"] = phys, "norepo"
            return info
        in_repo = os.path.relpath(os.path.join(real_dir, os.path.basename(path)), repo.top_real)
        info["logical"] = os.path.join(repo.main_walk, in_repo) if repo.main_walk else in_repo
        info["git"] = repo.state(in_repo)
        return info


# ---------------------------------------------------------------- git-derived sources (cached per repo state)

def git_cache():
    path = os.path.join(STATE, "gitcache.json")
    try:
        return path, json.load(open(path))
    except (OSError, ValueError):
        return path, {}


def refs_key(top):
    out = run([GIT, "-C", top, "for-each-ref", "--format=%(objectname)", "refs/heads", "refs/remotes", "refs/tags"]) or ""
    return sha(out)


def repo_dates(top):
    """file path in repo -> date of its latest commit."""
    out = run([GIT, "-C", top, "log", "--format=%x1e%cs", "--name-only", "--", "*.md", "*.MD", "*.mdx"],
              timeout=300) or ""
    dates = {}
    for block in out.split("\x1e")[1:]:
        lines = block.strip("\n").split("\n")
        for f in lines[1:]:
            if f and f not in dates:
                dates[f] = lines[0]
    return dates


def repo_commits(top):
    """Commits on branches, remotes and tags; None when git fails, so a failure is never cached as empty."""
    out = run([GIT, "-C", top, "log", "--branches", "--remotes", "--tags",
               "--format=%x1e%H%x1f%cs%x1f%s%x1f%b"], timeout=300)
    if out is None:
        return None
    commits = []
    for block in out.split("\x1e")[1:]:
        parts = block.split("\x1f")
        if len(parts) >= 4:
            commits.append({"sha": parts[0], "date": parts[1], "subject": parts[2].strip(), "body": parts[3].strip()})
    return commits


def repo_removed(top, current_text):
    """Lines deleted from notes over history that no longer exist in the note, grouped per commit and file."""
    out = run([GIT, "-C", top, "log", "--branches", "-p", "-U0", "--no-color", "--no-renames",
               "--format=%x1e%H%x1f%cs", "--", "*.md", "*.MD"], timeout=600)
    if out is None:
        return None
    removed = []
    for block in out.split("\x1e")[1:]:
        head, _, diff = block.partition("\n")
        csha, _, cdate = head.partition("\x1f")
        for fblock in re.split(r"^diff --git ", diff, flags=re.M)[1:]:
            m = re.match(r"a/(.+?) b/", fblock)
            if not m:
                continue
            fpath = m.group(1)
            now = current_text(fpath)
            lines = [l[1:].rstrip() for l in fblock.split("\n") if l.startswith("-") and not l.startswith("---")]
            gone = [l for l in lines if len(l.split()) >= 4 and l.strip() not in now]
            if gone:
                removed.append({"sha": csha, "date": cdate, "path": fpath, "text": "\n".join(gone)[:20000]})
    return removed


def script_header(path):
    try:
        if os.path.getsize(path) > 500_000:
            return None
        text = clean(read_text(path))
    except OSError:
        return None
    lines = text.splitlines()
    i = 1 if lines and lines[0].startswith("#!") else 0
    out = []
    if path.endswith(".py"):
        m = re.match(r'\s*(?:#[^\n]*\n\s*)*(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', "\n".join(lines[i:]), re.S)
        if m:
            out = m.group(1).strip().splitlines()
    if not out:
        for line in lines[i:i + 60]:
            s = line.strip()
            if s.startswith(("#", "//", "/*", "*")) and not s.startswith("#!"):
                out.append(re.sub(r"^(#+|//+|/\*+|\*+/?)\s?", "", s))
            elif s and out:
                break
            elif s and not out:
                if not s.startswith(("import ", "from ", "set -", "'use", '"use', "require(")):
                    break
    if not out:
        out = lines[i:i + 12]
    return "\n".join(out[:40]).strip()


# ---------------------------------------------------------------- catalog

def load_catalog():
    rows = {}
    for name in ("notes.jsonl", "local.jsonl"):
        p = os.path.join(CATALOG, name)
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line:
                    row = json.loads(line)
                    rows[row["logical"]] = row
    return rows


# ---------------------------------------------------------------- build

SCHEMA_SQL = """
CREATE TABLE docs(id INTEGER PRIMARY KEY, logical TEXT UNIQUE, path TEXT, abspath TEXT, source TEXT,
  project TEXT, title TEXT, description TEXT, date TEXT, kind TEXT, use_ TEXT, git_state TEXT,
  sensitive INT, copies TEXT, body_sha TEXT, classified INT);
CREATE TABLE entries(id INTEGER PRIMARY KEY, doc INT, n INT, anchor TEXT, heading_path TEXT, line INT,
  date TEXT, ehash TEXT, text TEXT, source TEXT, type TEXT, cause TEXT, reuse TEXT, symptom TEXT, fix TEXT,
  corrected INT, copy_path TEXT, weight REAL);
CREATE TABLE tags(entry INT, facet TEXT, value TEXT);
CREATE TABLE items(entry INT, item INT, type TEXT, symptom TEXT, cause TEXT, fix TEXT, reuse TEXT);
CREATE TABLE links(doc INT, kind TEXT, target TEXT, resolved TEXT, exists_ INT);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE fts USING fts5(title, description, anchor, tags, body, tokenize='porter unicode61');
CREATE INDEX entries_doc ON entries(doc);
CREATE INDEX tags_entry ON tags(entry);
CREATE INDEX tags_value ON tags(facet, value);
CREATE INDEX items_entry ON items(entry);
"""


class Builder:
    def __init__(self, vocab, full=True):
        self.vocab = vocab
        self.full = full
        self.corpus = Corpus()
        self.catalog = load_catalog()
        self.db = None
        self.counts = {}
        self.refs = {}
        reg_version = sha(json.dumps(vocab.reg, sort_keys=True) +  # the patterns too: a matching fix must reach cached rows
                          "".join(rx.pattern for rx in (vocab.rx_exact, vocab.rx_loose, vocab.rx_dotted) if rx))
        self.mention_path = os.path.join(STATE, "mentions-cache.json")
        try:
            cached = json.load(open(self.mention_path))
        except (OSError, ValueError):
            cached = {}
        self.mention_cache = {k: [tuple(x) for x in v] for k, v in cached.get("tags", {}).items()} \
            if cached.get("registry") == reg_version else {}
        self.mention_used = {}
        self.reg_version = reg_version

    def build(self):
        os.makedirs(STATE, exist_ok=True)
        t0 = time.time()
        self.corpus.walk()
        tmp = INDEX + ".tmp-%d" % os.getpid()
        if os.path.exists(tmp):
            os.remove(tmp)
        self.db = sqlite3.connect(tmp)
        self.db.executescript(SCHEMA_SQL)
        manifest = self.index_notes()
        self.index_git_sources()
        self.index_scripts()
        self.index_ponytail()
        self.db.execute("INSERT INTO meta VALUES('built_at', ?)", (datetime.now(timezone.utc).isoformat(),))
        self.db.execute("INSERT INTO meta VALUES('schema', ?)", (SCHEMA_VERSION,))
        self.db.execute("INSERT INTO meta VALUES('counts', ?)", (json.dumps(self.counts),))
        self.db.commit()
        self.db.close()
        os.replace(tmp, INDEX)
        with open(self.mention_path + ".tmp", "w") as fh:
            json.dump({"registry": self.reg_version, "tags": self.mention_used}, fh)
        os.replace(self.mention_path + ".tmp", self.mention_path)
        with open(os.path.join(STATE, "manifest.json.tmp"), "w") as fh:
            json.dump(manifest, fh)
        os.replace(os.path.join(STATE, "manifest.json.tmp"), os.path.join(STATE, "manifest.json"))
        self.counts["seconds"] = round(time.time() - t0, 2)
        return self.counts

    # notes ----------------------------------------------------------------
    def index_notes(self):
        groups = {}
        for f in self.corpus.files:
            groups.setdefault(f["logical"], []).append(f)
        dates_by_repo = {}
        cache_path, cache = git_cache()
        manifest = {"built_at": datetime.now(timezone.utc).isoformat(), "docs": {}}
        self.texts = {}
        for logical in sorted(groups):
            copies = sorted(groups[logical], key=lambda f: (f["phys"] != logical, f["phys"]))
            main = copies[0]
            try:
                text = read_text(main["abspath"])
            except OSError as exc:
                self.corpus.walk_errors.append("%s: %s" % (main["abspath"], exc))
                continue
            self.texts[logical] = text
            meta, fm_end = frontmatter(text)
            entries = split_entries(text)
            repo = main["repo"]
            doc_date = (meta.get("modified") or "")[:10] or None
            if repo is not None and not doc_date:
                key = repo.top_real
                if key not in dates_by_repo:
                    ck = "dates:" + key
                    rk = self.refs_key(key)
                    if cache.get(ck, {}).get("refs") != rk:
                        cache[ck] = {"refs": rk, "dates": repo_dates(key)}
                    dates_by_repo[key] = cache[ck]["dates"]
                doc_date = dates_by_repo[key].get(os.path.relpath(os.path.realpath(main["abspath"]), repo.top_real))
            if not doc_date:
                doc_date = datetime.fromtimestamp(os.path.getmtime(main["abspath"])).date().isoformat()
            title = meta.get("name") or title_of(text, os.path.basename(logical))
            cat = self.catalog.get(logical, {})
            sensitive = any(c["git"] in ("ignored", "unknown") for c in copies)
            if any(c["git"] == "unknown" for c in copies):
                self.counts["git_state_unknown"] = self.counts.get("git_state_unknown", 0) + 1
            states = sorted({c["git"] for c in copies})
            project = self.vocab.project_for_path(logical)
            description = meta.get("description") or cat.get("description") or ""
            cur = self.db.execute(
                "INSERT INTO docs(logical,path,abspath,source,project,title,description,date,kind,use_,git_state,"
                "sensitive,copies,body_sha,classified) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (logical, main["phys"], main["abspath"], main["source"], project, clean(title), clean(description),
                 doc_date, cat.get("kind") or self.kind_hint(logical, entries), cat.get("use"), ",".join(states),
                 int(sensitive), json.dumps([c["phys"] for c in copies[1:]]), sha(text), int(bool(cat))))
            doc_id = cur.lastrowid
            cat_entries = {}  # section hash -> its items, in order
            for x in sorted(cat.get("entries", []), key=lambda x: x.get("item", 1)):
                cat_entries.setdefault(x["ehash"], []).append(x)
            for e in entries:
                body = e["text"][fm_end:] if e["n"] == 1 and fm_end else e["text"]
                self.add_entry(doc_id, e, body, main["source"], cat_entries.get(e["ehash"]), cat, title, description,
                               project, doc_date)
            main_hashes = {e["ehash"] for e in entries}
            for c in copies[1:]:
                try:
                    ctext = read_text(c["abspath"])
                except OSError:
                    continue
                for e in split_entries(ctext):
                    if e["ehash"] not in main_hashes:
                        main_hashes.add(e["ehash"])
                        self.add_entry(doc_id, e, e["text"], main["source"], cat_entries.get(e["ehash"]), cat, title,
                                       description, project, doc_date, copy_path=c["phys"])
                        self.counts["copy_only_entries"] = self.counts.get("copy_only_entries", 0) + 1
            self.add_links(doc_id, logical, text, main, repo)
            manifest["docs"][logical] = {"path": main["phys"], "abspath": main["abspath"], "source": main["source"],
                                         "copies": [c["phys"] for c in copies[1:]], "git": states,
                                         "sensitive": sensitive, "body_sha": sha(text), "project": project,
                                         "title": clean(title), "chars": len(text),
                                         "entries": [{"n": e["n"], "ehash": e["ehash"], "anchor": clean(e["anchor"]),
                                                      "line": e["line"], "chars": len(e["text"])} for e in entries]}
            self.counts[main["source"] + "_docs"] = self.counts.get(main["source"] + "_docs", 0) + 1
        self.counts["files"] = len(self.corpus.files)
        with open(cache_path + ".tmp", "w") as fh:
            json.dump(cache, fh)
        os.replace(cache_path + ".tmp", cache_path)
        return manifest

    def refs_key(self, top):
        if top not in self.refs:
            self.refs[top] = refs_key(top)
        return self.refs[top]

    def kind_hint(self, logical, entries):
        name = os.path.basename(logical).lower()
        if name in ("readme.md", "memory.md", "index.md"):
            return "index"
        if "session-log" in name or name.endswith("-log.md") or name in ("log.md", "daily-log.md"):
            return "log"
        if name in ("tasks.md", "todo.md") or name.startswith("plan"):
            return "plan"
        if "data-map" in name:
            return "reference"
        if re.search(r"bug|lesson|learning|incident|postmortem", name):
            return "bug-writeup"
        if sum(1 for e in entries if e["dated"]) > len(entries) / 2 > 2:
            return "log"
        return None

    def add_entry(self, doc_id, e, body, source, cat_items, cat, title, description, project, doc_date, copy_path=None):
        corrected = int(bool(CORRECTION_RE.search(e["text"])))
        cat_items = cat_items or []
        cat_e = cat_items[0] if cat_items else {}
        etype = cat_e.get("type") or ("log" if e["dated"] else None)
        cur = self.db.execute(
            "INSERT INTO entries(doc,n,anchor,heading_path,line,date,ehash,text,source,type,cause,reuse,symptom,fix,"
            "corrected,copy_path,weight) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (doc_id, e["n"], clean(e["anchor"]), clean(" > ".join(e["heading_path"])), e["line"],
             e["date"] or doc_date, e["ehash"], clean(e["text"]), source, etype, cat_e.get("root_cause"),
             cat_e.get("reuse"), cat_e.get("symptom_quote"), cat_e.get("fix_quote"),
             int(corrected or any(x.get("corrects_quote") for x in cat_items)), copy_path, SOURCE_WEIGHT[source]))
        eid = cur.lastrowid
        self.db.executemany("INSERT INTO items VALUES(?,?,?,?,?,?,?)",
                            [(eid, x.get("item", 1), x.get("type"), x.get("symptom_quote"), x.get("root_cause"),
                              x.get("fix_quote"), x.get("reuse")) for x in cat_items])
        tags = set()
        key = e["ehash"]
        if key not in self.mention_cache:
            self.mention_cache[key] = sorted(self.vocab.mentions(e["text"]))
            self.counts["mentions_computed"] = self.counts.get("mentions_computed", 0) + 1
        self.mention_used[key] = self.mention_cache[key]
        for facet, value in self.mention_cache[key]:
            tags.add(("mention-" + facet, value))
        domains = [d for x in cat_items for d in x.get("domain", [])] or (cat.get("domain", []) if e["n"] == 1 else [])
        tags |= {("domain", d) for d in domains}
        tags |= {("tech", t) for x in cat_items for t in x.get("tech", [])}
        tags |= {("project", p) for x in cat_items for p in x.get("projects", [])}
        tags.add(("project", project))
        self.db.executemany("INSERT INTO tags VALUES(?,?,?)", [(eid, f, v) for f, v in sorted(tags)])
        tag_text = " ".join(sorted({v for _, v in tags}))
        if cat_items:
            body = body + "\n" + " ".join(v for x in cat_items for v in (x.get("root_cause"), x.get("reuse")) if v)
        self.db.execute("INSERT INTO fts(rowid,title,description,anchor,tags,body) VALUES(?,?,?,?,?,?)",
                        (eid, clean(title), clean(description), clean(" > ".join(e["heading_path"] + [e["anchor"]])),
                         tag_text, clean(body)))
        self.counts["entries"] = self.counts.get("entries", 0) + 1
        return eid

    def add_links(self, doc_id, logical, text, main, repo):
        rows = []
        note_dir = os.path.dirname(main["abspath"])
        repo_top = repo.top_real if repo else None
        for m in WIKILINK_RE.finditer(text):
            target = m.group(1).strip()
            p = os.path.join(MEMORY, target + ".md")
            rows.append((doc_id, "wikilink", target, p, int(os.path.exists(p))))
        for m in MDLINK_RE.finditer(text):
            target = m.group(1)
            p = os.path.normpath(os.path.join(note_dir, target)) if not target.startswith("/") else target
            rows.append((doc_id, "mdlink", target, p, int(os.path.exists(p))))
        for m in set(SCRIPT_REF_RE.findall(text)):
            cands = [os.path.join(b, m) for b in (repo_top, note_dir, PROJECTS) if b]
            hit = next((c for c in cands if os.path.isfile(c)), None)
            rows.append((doc_id, "script", m, hit or cands[0], int(bool(hit))))
        self.db.executemany("INSERT INTO links VALUES(?,?,?,?,?)", rows)
        self.counts["links"] = self.counts.get("links", 0) + len(rows)

    # other sources -----------------------------------------------------------
    def main_repos(self):
        """One Repo per git common dir, the main checkout when the walk found it."""
        by_common = {}
        for key, repo in self.corpus.repos.items():
            if isinstance(key, tuple) and repo is not None:
                cur = by_common.get(repo.common_real)
                if cur is None or repo.walk_top == repo.main_walk:
                    by_common[repo.common_real] = repo
        return sorted(by_common.values(), key=lambda r: r.main_walk)

    def synthetic_doc(self, logical, path, abspath, source, project, title, date_):
        cur = self.db.execute(
            "INSERT OR IGNORE INTO docs(logical,path,abspath,source,project,title,description,date,kind,git_state,"
            "sensitive,copies,body_sha,classified) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (logical, path, abspath, source, project, clean(title), "", date_, source, "", 0, "[]", "", 0))
        if cur.lastrowid and cur.rowcount:
            return cur.lastrowid
        return self.db.execute("SELECT id FROM docs WHERE logical=?", (logical,)).fetchone()[0]

    def index_git_sources(self):
        cache_path, cache = git_cache()
        cutoff = date.today().toordinal() - 90
        for repo in self.main_repos():
            rk = self.refs_key(repo.top_real)
            name = repo.main_walk or "(projects root)"
            project = self.vocab.project_for_path(repo.main_walk + "/x" if repo.main_walk else "docs")
            ck = "commits:" + repo.common_real
            if cache.get(ck, {}).get("refs") != rk:
                fresh = repo_commits(repo.top_real)
                if fresh is None:
                    self.counts.setdefault("git_log_failed", []).append(name)
                    fresh = cache.get(ck, {}).get("commits", [])
                else:
                    cache[ck] = {"refs": rk, "commits": fresh}
                commits = fresh
            else:
                commits = cache[ck]["commits"]
            for c in commits:
                logical = "%s@%s" % (name, c["sha"][:10])
                text = c["subject"] + ("\n\n" + c["body"] if c["body"] else "")
                doc = self.synthetic_doc(logical, logical, repo.top_real, "commit", project, c["subject"], c["date"])
                e = {"n": 1, "anchor": "commit " + c["sha"][:10], "heading_path": [], "line": 0, "date": c["date"],
                     "ehash": sha(text), "text": text, "dated": False}
                self.add_entry(doc, e, text, "commit", None, {}, c["subject"], "", project, c["date"])
                self.counts["commits"] = self.counts.get("commits", 0) + 1
            recent = [c for c in commits if c["date"] and date.fromisoformat(c["date"]).toordinal() >= cutoff]
            if not recent:
                continue
            rkey = "removed:" + repo.common_real
            if cache.get(rkey, {}).get("refs") != rk:
                def current_text(fpath, top=repo.top_real):
                    try:
                        return read_text(os.path.join(top, fpath))
                    except OSError:
                        return ""
                fresh = repo_removed(repo.top_real, current_text)
                if fresh is not None:
                    cache[rkey] = {"refs": rk, "removed": fresh}
            for r in cache.get(rkey, {}).get("removed", []):
                note_logical = os.path.join(repo.main_walk, r["path"]) if repo.main_walk else r["path"]
                logical = "%s (removed %s)" % (note_logical, r["sha"][:10])
                doc = self.synthetic_doc(logical, note_logical, os.path.join(repo.top_real, r["path"]), "removed",
                                         project, os.path.basename(r["path"]) + " (removed text)", r["date"])
                e = {"n": 1, "anchor": "removed in " + r["sha"][:10], "heading_path": [], "line": 0, "date": r["date"],
                     "ehash": sha(r["text"]), "text": r["text"], "dated": False}
                self.add_entry(doc, e, r["text"], "removed", None, {}, os.path.basename(r["path"]), "", project, r["date"])
                self.counts["removed"] = self.counts.get("removed", 0) + 1
        with open(cache_path + ".tmp", "w") as fh:
            json.dump(cache, fh)
        os.replace(cache_path + ".tmp", cache_path)

    def fold(self, abspath):
        """A file under PROJECTS -> (logical path, Repo, path inside the repo), worktrees folded to the main checkout."""
        rel_walk = os.path.relpath(abspath, PROJECTS)
        real_dir = os.path.realpath(os.path.dirname(abspath))
        repo = self.corpus.repo_for_dir(real_dir, os.path.dirname(rel_walk))
        if repo is None:
            return rel_walk, None, rel_walk
        in_repo = os.path.relpath(os.path.join(real_dir, os.path.basename(abspath)), repo.top_real)
        return (os.path.join(repo.main_walk, in_repo) if repo.main_walk else in_repo), repo, in_repo

    def scan(self, name, fn):
        """fn()'s result, reused from the last scan for SLOW_SCAN_SECONDS unless this is a full build."""
        path = os.path.join(STATE, "scan-%s.json" % name)
        if not self.full:
            try:
                cached = json.load(open(path))
                if time.time() - cached["at"] < SLOW_SCAN_SECONDS:
                    return cached["value"]
            except (OSError, ValueError, KeyError):
                pass
        value = fn()
        with open(path + ".tmp", "w") as fh:
            json.dump({"at": time.time(), "value": value}, fh)
        os.replace(path + ".tmp", path)
        return value

    def code_walk(self):
        """Top-level script files (a scripts/bin/tools/crawl folder at most one level below the repo top)."""
        found = dict(self.scan("scripts", self.script_files))
        for (resolved,) in self.db.execute("SELECT DISTINCT resolved FROM links WHERE kind='script' AND exists_=1"):
            if (resolved + "/").startswith(PROJECTS + "/") and set(resolved.split(os.sep)) & SCRIPT_DIRS:
                logical, _, _ = self.fold(resolved)
                found.setdefault(logical, resolved)
        return found

    def script_files(self):
        found = {}
        seen = set()
        for root, dirs, files in os.walk(PROJECTS, followlinks=True):
            real = os.path.realpath(root)
            if real in seen:
                dirs[:] = []
                continue
            seen.add(real)
            rel = os.path.relpath(root, PROJECTS)
            rel = "" if rel == "." else rel
            dirs[:] = sorted(d for d in dirs if d not in PRUNE_NAMES
                             and (os.path.join(rel, d) if rel else d) not in PRUNE_PATHS)
            for f in files:
                if not f.endswith(SCRIPT_EXT):
                    continue
                path = os.path.join(root, f)
                logical, repo, in_repo = self.fold(path)
                parts = in_repo.split(os.sep)
                if any(x in SCRIPT_DIRS for x in parts[:2]) and len(parts) <= 4:
                    if logical not in found or path == os.path.join(PROJECTS, logical):
                        found[logical] = path
        per_dir = {}
        for logical in found:
            per_dir.setdefault(os.path.dirname(logical), []).append(logical)
        for d, items in per_dir.items():
            if len(items) > VENDORED_DIR_FILES:
                for logical in items:
                    del found[logical]
                self.counts.setdefault("vendored_dirs_skipped", []).append("%s (%d files)" % (d, len(items)))
        return found

    def index_scripts(self):
        for logical, path in sorted(self.code_walk().items()):
            header = script_header(path)
            if header is None:
                continue
            project = self.vocab.project_for_path(logical)
            doc = self.synthetic_doc(logical, logical, path, "script", project, os.path.basename(logical),
                                     datetime.fromtimestamp(os.path.getmtime(path)).date().isoformat())
            text = logical + "\n" + header
            e = {"n": 1, "anchor": "script header", "heading_path": [], "line": 1, "date": None, "ehash": sha(text),
                 "text": text, "dated": False}
            self.add_entry(doc, e, text, "script", None, {}, os.path.basename(logical), "", project, None)
            self.counts["scripts"] = self.counts.get("scripts", 0) + 1

    def index_ponytail(self):
        if not RG:
            print("brain: rg not found, ponytail comments not indexed", file=sys.stderr)
            return
        args = [RG, "-n", "--no-heading", "--follow", "--no-ignore", "-g", "!*.md", "-g", "!*.json", "-g", "!*.jsonl",
                "-g", "!*.min.js"]
        for name in PRUNE_NAMES:
            args += ["-g", "!" + name]
        for path in PRUNE_PATHS:
            args += ["-g", "!/" + path]
        # rg anchors a "!/path" glob at its working directory, not at the search root: run from PROJECTS or the
        # pruned trees leak (brain/state batch prompts and third-party clones did, 2026-10-26)
        out = self.scan("ponytail", lambda: run(args + ["ponytail:", PROJECTS], cwd=PROJECTS, timeout=300,
                                                ok=(0, 1, 2)) or "")
        seen = set()
        for line in out.splitlines():
            m = re.match(r"^(.*?):(\d+):(.*)$", line)
            if not m:
                continue
            path, lineno, content = m.group(1), int(m.group(2)), m.group(3).strip()
            logical, _, _ = self.fold(path)
            if (logical, content) in seen:
                continue
            seen.add((logical, content))
            try:
                lines = clean(read_text(path)).splitlines()
            except OSError:
                continue
            text = "\n".join(lines[max(0, lineno - 3): lineno + 2])
            key = "%s:%d" % (logical, lineno)
            project = self.vocab.project_for_path(logical)
            doc = self.synthetic_doc(key, key, path, "ponytail", project, os.path.basename(logical) + " ponytail", None)
            e = {"n": 1, "anchor": "ponytail: line %d" % lineno, "heading_path": [], "line": lineno, "date": None,
                 "ehash": sha(text), "text": text, "dated": False}
            self.add_entry(doc, e, text, "ponytail", None, {}, os.path.basename(logical), "", project, None)
            self.counts["ponytail"] = self.counts.get("ponytail", 0) + 1


def locked(name, blocking=True):
    os.makedirs(STATE, exist_ok=True)
    fh = open(os.path.join(STATE, name + ".lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        fh.close()
        return None
    return fh


def build(force=False):
    lock = locked("build", blocking=True)
    try:
        if not force and os.path.exists(INDEX) and time.time() - os.path.getmtime(INDEX) < 2:
            return {}
        return Builder(Vocab(load_registry()), full=True).build()
    finally:
        lock.close()


SCHEMA_VERSION = sha(SCHEMA_SQL)[:12]


def index_state():
    """ok, stale (older than STALE_SECONDS: still searchable) or unusable (missing, or built by another schema)."""
    try:
        db = sqlite3.connect("file:%s?mode=ro" % INDEX, uri=True)
        row = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        db.close()
    except sqlite3.Error:
        return "unusable"
    if not row or row[0] != SCHEMA_VERSION:
        return "unusable"
    return "ok" if time.time() - os.path.getmtime(INDEX) < STALE_SECONDS else "stale"


def ensure_fresh():
    """Rebuild when the index is stale or unusable; a stale index never blocks a search on a running build."""
    state = index_state()
    if state == "ok":
        return
    lock = locked("build", blocking=state == "unusable")
    if lock is None:
        return
    try:
        if index_state() != "ok":
            Builder(Vocab(load_registry()), full=False).build()
    finally:
        lock.close()


# ---------------------------------------------------------------- search

# Ranking factors, tuned on the tune100 split of review/queries.jsonl (never on the score split).
RANK = {"bm25": (8.0, 4.0, 3.0, 2.0, 1.0), "cover": (0.4, 1), "log": 1.0, "log_doc": 1.0, "doc_sum": 0.0}

ITEM_OF = "EXISTS (SELECT 1 FROM items i WHERE i.entry = e.id AND i.type = '%s')"
KIND_FILTERS = {
    "bug": ITEM_OF % "bug", "method": ITEM_OF % "method", "decision": ITEM_OF % "decision",
    "lesson": ITEM_OF % "lesson", "log": "e.type = 'log'", "commit": "e.source = 'commit'",
    "script": "e.source = 'script'", "ponytail": "e.source = 'ponytail'", "removed": "e.source = 'removed'",
    "note": "e.source IN ('note','memory','claude')", "memory": "e.source = 'memory'",
}


def query_terms(q, vocab):
    words = []
    for w in re.findall(r"\w+", q.lower(), re.UNICODE):
        if w not in STOPWORDS and (len(w) > 1 or w.isdigit()) and w not in words:
            words.append(w)
    words = words[:32]
    phrases = list(words)
    expansions = set()
    low = " " + re.sub(r"[\s_-]+", " ", q.lower()) + " "
    for alias, (facet, cid) in vocab.canon.items():
        a = " " + re.sub(r"[\s_-]+", " ", alias) + " "
        if len(alias) >= 2 and a in low:
            expansions.add((facet, cid))
    extra = []
    for facet, cid in sorted(expansions):
        names = [cid]
        if facet == "project":
            p = vocab.reg["projects"][cid]
            names += [p.get("folder", cid)] + p["aliases"]
        else:
            names += vocab.reg["tech"].get(cid, [])
        for n in names:
            n = n.lower()
            if n not in phrases and n not in extra:
                extra.append(n)
    return words, phrases + extra


def fts_query(phrases):
    out = []
    for p in phrases:
        toks = re.findall(r"\w+", p, re.UNICODE)
        if toks:
            out.append('"' + " ".join(toks) + '"')
    return " OR ".join(out)


def search(q, project=None, kind=None, limit=5, per_doc=2, db=None, vocab=None):
    vocab = vocab or Vocab(load_registry())
    words, phrases = query_terms(q, vocab)
    match = fts_query(phrases)
    if not match:
        return words, []
    own = db is None
    db = db or sqlite3.connect("file:%s?mode=ro" % INDEX, uri=True)
    where, args = ["fts MATCH ?"], [match]
    if kind:
        where.append(KIND_FILTERS[kind])
    if project:
        pid = vocab.fold(project, "project")
        pid = pid if not pid.startswith("other:") else project.lower()
        where.append("(d.project = ? OR e.id IN (SELECT entry FROM tags WHERE value = ? AND facet IN ('project','mention-project')))")
        args += [pid, pid]
    sql = ("SELECT e.id, bm25(fts, %s) AS score, e.weight, e.doc, e.type, d.kind FROM fts "
           "JOIN entries e ON e.id = fts.rowid JOIN docs d ON d.id = e.doc WHERE " % ", ".join(map(str, RANK["bm25"])) +
           " AND ".join(where) + " ORDER BY score LIMIT 300")
    cands = db.execute(sql, args).fetchall()
    ranked, doc_total = [], {}
    for eid, score, weight, doc, etype, dkind in cands:
        text = db.execute("SELECT lower(text || ' ' || anchor) FROM entries WHERE id=?", (eid,)).fetchone()[0]
        title = db.execute("SELECT lower(title || ' ' || coalesce(description,'')) FROM docs WHERE id=?", (doc,)).fetchone()[0]
        hay = text + " " + title
        cover = sum(1 for w in words if re.search(r"\b" + re.escape(w[:max(4, len(w) - 2)]), hay)) / max(1, len(words))
        s_ = score * weight * (RANK["cover"][0] + cover) ** RANK["cover"][1]
        if etype == "log":
            s_ *= RANK["log"]
        if dkind in ("log", "plan"):
            s_ *= RANK["log_doc"]
        ranked.append((s_, eid, doc))
        doc_total[doc] = doc_total.get(doc, 0.0) + s_
    if RANK["doc_sum"]:
        ranked = [(s_ + RANK["doc_sum"] * (doc_total[doc] - s_), eid, doc) for s_, eid, doc in ranked]
    ranked.sort()
    out, per, seen_text = [], {}, {}
    for score, eid, doc in ranked:
        if per.get(doc, 0) >= per_doc:
            continue
        ehash = db.execute("SELECT ehash FROM entries WHERE id=?", (eid,)).fetchone()[0]
        row = hit_row(db, eid, words, score)
        if ehash in seen_text:
            prev = seen_text[ehash]
            prev.setdefault("also_in", []).append(row["path"])
            missing = lambda h: sum(1 for x in h["scripts"] if not x["exists"])
            if missing(row) < missing(prev):
                row["also_in"] = [prev["path"]] + [x for x in prev["also_in"] if x != row["path"]]
                out[out.index(prev)] = row
                seen_text[ehash] = row
            continue
        seen_text[ehash] = row
        per[doc] = per.get(doc, 0) + 1
        out.append(row)
        if len(out) >= limit:
            break
    if own:
        db.close()
    return words, out


def hit_row(db, eid, words, score):
    r = db.execute(
        "SELECT e.anchor, e.heading_path, e.line, e.date, e.text, e.source, e.type, e.cause, e.reuse, e.corrected, "
        "e.copy_path, d.logical, d.path, d.abspath, d.project, d.title, d.description, d.sensitive, d.id, e.symptom, e.fix "
        "FROM entries e JOIN docs d ON d.id = e.doc WHERE e.id=?", (eid,)).fetchone()
    (anchor, hpath, line, date_, text, source, etype, cause, reuse, corrected, copy_path, logical, path, abspath,
     project, title, description, sensitive, doc_id, symptom, fix) = r
    if text.startswith(("---", "\ufeff---")):
        meta, end = frontmatter(text)
        text = text[end:] if end else text
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    matched = [l for l in lines if any(re.search(r"\b" + re.escape(w[:max(4, len(w) - 2)]), l.lower()) for w in words)]
    scripts = db.execute("SELECT target, exists_ FROM links WHERE doc=? AND kind='script'", (doc_id,)).fetchall()
    in_entry = [(t, x) for t, x in scripts if t in text]
    items = [{"item": i, "type": t, "cause": c, "reuse": u} for i, t, c, u in
             db.execute("SELECT item, type, cause, reuse FROM items WHERE entry=? ORDER BY item", (eid,))]
    return {"items": items,"score": round(score, 3), "project": project, "source": source, "type": etype, "logical": logical,
            "path": copy_path or path, "abspath": abspath if not copy_path else os.path.join(PROJECTS, copy_path),
            "line": line, "anchor": anchor, "heading_path": hpath, "title": title, "date": date_,
            "corrected": bool(corrected), "sensitive": bool(sensitive), "cause": cause, "reuse": reuse,
            "symptom": symptom, "fix": fix, "description": description,
            "matched": [m[:220] for m in matched[:3]] or [l[:220] for l in lines[:2]],
            "scripts": [{"path": t, "exists": bool(x)} for t, x in in_entry[:5]]}


def format_hits(q, hits):
    if not hits:
        return "No hits for %r. Try other words: the error text, the tool, or the project name." % q
    out = []
    for i, h in enumerate(hits, 1):
        flags = [x for x in (h["source"] if h["source"] not in ("note", "memory") else None, h["type"],
                             "corrected" if h["corrected"] else None, "local-only" if h["sensitive"] else None) if x]
        where = h["abspath"] + (":%d" % h["line"] if h["line"] else "")
        out.append("%d. [%s] %s" % (i, h["project"], where))
        path_parts = [x for x in (h["heading_path"] or "").split(" > ") if x and x != h["title"]]
        ctx = " > ".join([h["title"]] + path_parts) if h["title"] else " > ".join(path_parts)
        out.append("   %s | %s%s" % (h["anchor"][:120], ctx[:120], (" | " + h["date"]) if h["date"] else ""))
        if flags:
            out.append("   [" + ", ".join(flags) + "]")
        if len(h.get("items") or []) > 1:
            for it in h["items"]:
                out.append("   %d. [%s] cause: %s | reuse (suggestion): %s" % (it["item"], it["type"], it["cause"] or "-",
                                                                            it["reuse"] or "-"))
        elif h["cause"] or h["reuse"]:
            out.append("   cause: %s | reuse (suggestion): %s" % (h["cause"] or "-", h["reuse"] or "-"))
        elif h["description"] and h["anchor"] == "(top)":
            out.append("   " + h["description"][:200])
        for m in h["matched"]:
            out.append("   > " + m)
        for s in h["scripts"]:
            out.append("   script: %s (%s)" % (s["path"], "exists" if s["exists"] else "MISSING"))
        if h.get("also_in"):
            out.append("   same text also in: " + ", ".join(h["also_in"][:3]))
    return "\n".join(out)


def log_usage(q, args, hits, ms):
    os.makedirs(STATE, exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "host": os.uname().nodename,
           "cwd": os.environ.get("BRAIN_CALLER_CWD") or os.getcwd(), "query": q, "project": args.project,
           "kind": args.kind, "ms": ms, "hits": [h["logical"] + "#" + str(h["line"]) for h in hits]}
    with open(os.path.join(STATE, "usage.jsonl"), "a") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- check

def check():
    """Every in-scope file is indexed, and every file's entries rebuild it byte for byte."""
    corpus = Corpus()
    corpus.walk()
    bad, n_entries = [], 0
    for f in corpus.files:
        with open(f["abspath"], "rb") as fh:
            raw = fh.read()
        es = split_entries(read_text(f["abspath"]))
        n_entries += len(es)
        if "".join(e["text"] for e in es).encode("utf-8", errors="surrogateescape") != raw:
            bad.append(f["phys"])
    built = os.path.getmtime(INDEX)
    logical = {f["logical"] for f in corpus.files}
    newer = {f["logical"] for f in corpus.files if os.path.getmtime(f["abspath"]) > built}
    db = sqlite3.connect("file:%s?mode=ro" % INDEX, uri=True)
    indexed = {r[0] for r in db.execute("SELECT logical FROM docs WHERE source IN ('note','memory','claude')")}
    db.close()
    return {"files": len(corpus.files), "logical": len(logical), "entries": n_entries,
            "lossless_mismatches": bad, "missing_from_index": sorted(logical - indexed - newer),
            "new_since_build": sorted((logical - indexed) & newer), "extra_in_index": sorted(indexed - logical),
            "walk_errors": corpus.walk_errors}


# ---------------------------------------------------------------- classify

CLASSIFY_MODEL = os.environ.get("BRAIN_CLASSIFY_MODEL", "claude-sonnet-5-5")
CLASSIFY_EFFORT = os.environ.get("BRAIN_CLASSIFY_EFFORT", "high")
REMOTE = setting("remote", "BRAIN_REMOTE", "local")  # an ssh host that runs the reading runs, or "local" for this machine
REMOTE_RUNS = "~/.local/state/brain-run"
BATCH_CHARS, MEMORY_BATCH_CHARS, PART_CHARS = 40000, 60000, 45000
MAX_ITEMS, MEMORY_MAX_ITEMS = 12, 40
KINDS = ["log", "incident", "bug-writeup", "decision", "lesson", "checklist", "runbook", "plan", "research",
         "reference", "explanation", "index"]
USES = ["follow", "consult", "avoid", "decide", "history"]
ENTRY_TYPES = ["bug", "method", "decision", "lesson"]
WAVE1_RE = re.compile(r"(?i)(bug|lesson|learning|incident|postmortem|runbook|playbook|how-?to|troubleshoot|fix|trap"
                      r"|gotcha|checklist|deploy|recon|failure|traps|pitfall)")
QUOTE_FIELDS = ("symptom_quote", "root_cause_quote", "fix_quote", "corrects_quote")

PROMPT_HEAD = """You classify engineering notes for a search index that coding sessions use to reuse past fixes.
For each numbered FILE below return one object in `files`, with `n` equal to the FILE number.

File fields:
- kind: what the file is. First match wins, in this order: log (dated entries appended over time), incident,
  bug-writeup, decision, lesson, checklist, runbook, plan, research, reference, explanation, index.
- use: follow (steps or a rule), consult (facts to look up), avoid (a trap), decide (precedent), history (record only).
  A note written so a later session does not redo the work is follow or consult, not history.
- domain: up to 3 of: {domains}. If none fits, other:<word>.
- tech: tools and platforms the file is about, from this list when one fits: {tech}. Otherwise other:<word>.
- projects: the projects the file is about, from the list below when one fits. The folder a file sits in is a hint,
  not the answer: a shared doc can be about several projects, and a note that names only a session (Shop_Sync) or a
  site name belongs to that session's or site's project.
- description: one line, under 240 characters, saying what the note gives or what happened, with a number or
  outcome when the note has one.

entries: the text is split into sections, and each section starts with a marker [E<k>]. Return an entry for a
section that holds a bug, a reusable method, a decision, or a lesson. That includes a section with two or more shell
commands to run (a method), a list of named settings with their values (a method), and a measured number that
changes what to do next (a decision). A section with labelled lines (Symptom:, Cause:, Root cause:, Fix:) is always an
entry. Skip plain status lines, log events with no lesson, tables of contents, and
anything a later reader would not reuse. When one section holds several separate bugs, methods or decisions
(numbered items, table rows or bold-led paragraphs, each with its own cause or fix), return one entry per item, all
with the same e and item 1, 2, 3 in order; otherwise item is 1. For each returned entry:
- e: the marker number k. item: its number within the section. type: bug | method | decision | lesson.
- tech, domain, projects: for that section alone (sections in one file often cover different tools and projects).
  Name a tech only if the section names it, or a command or file that belongs only to it. Never add a tool because
  the project usually uses it. Tag the tool the problem or fix is in, not one the section mentions in passing or one
  from another item in the same section. List every project the section names, not only the folder's.
  other:<name> is only for a library, language, runtime or hosted service, never a site a crawl targets, a project,
  a script, a file format or a concept.
- symptom_quote, root_cause_quote, fix_quote, corrects_quote: text copied EXACTLY from that same section, same
  characters and punctuation, at most 200 characters each: a whole clause that states the symptom, cause or fix, never
  a fragment that starts or ends on a connector word. corrects_quote only when the section says an earlier claim was
  wrong.
- root_cause: the cause in at most 15 of your own words, only when root_cause_quote supports it. If the section
  says why (because, so, the cause is, X does not do Y), fill it; null only when no reason is given.
- fix_quote: the change that removed the root cause. Not a workaround the text says failed, not a guard ("do not
  delete"), not a finding. If the text only proposes a fix, quote the proposal.
- reuse: at most 25 of your own words that restate root_cause and the fix as an instruction for next time. Write it
  from this section only: add no step, tool, number or check it does not state, and never use the sections before
  or after it. If the section has no fix, reuse names the trap only. A decision
  or lesson entry must carry reuse; if you cannot write one, do not return the entry.
Set anything the section does not state to null. Never guess a cause. Never copy secret values (keys, tokens,
passwords) into any field. Memory notes marked FACETS ONLY get file fields and an empty entries list.
"""


def classify_schema():
    q = {"type": ["string", "null"], "maxLength": 300}
    lst = lambda n: {"type": "array", "items": {"type": "string", "maxLength": 60}, "maxItems": n}
    entry = {"type": "object", "required": ["e", "type"], "properties": {
        "e": {"type": "integer"}, "item": {"type": "integer"}, "type": {"enum": ENTRY_TYPES}, "tech": lst(8), "domain": lst(3), "projects": lst(6),
        "symptom_quote": q, "root_cause": {"type": ["string", "null"], "maxLength": 160}, "root_cause_quote": q,
        "fix_quote": q, "reuse": {"type": ["string", "null"], "maxLength": 220}, "corrects_quote": q}}
    f = {"type": "object", "required": ["n", "kind", "use", "domain", "tech", "projects", "description", "entries"],
         "properties": {"n": {"type": "integer"}, "kind": {"enum": KINDS}, "use": {"enum": USES}, "domain": lst(3),
                        "tech": lst(10), "projects": lst(6), "description": {"type": "string", "maxLength": 300},
                        "entries": {"type": "array", "items": entry}}}
    return {"type": "object", "required": ["files"], "properties": {"files": {"type": "array", "items": f}}}


def qnorm(text):
    import unicodedata
    t = unicodedata.normalize("NFKC", text or "")
    t = re.sub(r"[‐-―−]", "-", t)
    t = t.replace("‘", "'").replace("’", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"[`*]", "", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def wave_of(logical, source):
    if source == "memory":
        return 3
    return 1 if (WAVE1_RE.search(os.path.basename(logical)) or logical.startswith("docs/")) else 2


def work_items(wave, reread=False, only=None):
    """What the next run must read: new files whole, changed files whole or only their changed sections."""
    manifest = json.load(open(os.path.join(STATE, "manifest.json")))["docs"]
    catalog = load_catalog()
    items = []
    for logical in sorted(manifest):
        d = manifest[logical]
        if d["source"] not in ("note", "memory", "claude") or (wave != "all" and wave_of(logical, d["source"]) != int(wave)):
            continue
        if only is not None and logical not in only:
            continue
        try:
            text = read_text(d["abspath"])
        except OSError:
            continue
        entries = split_entries(text)
        row = catalog.get(logical)
        fresh = reread or only is not None or bool(row and row.get("reread"))
        if fresh:
            row = None
        facets = d["source"] == "memory"
        if row is None:
            mode, todo = ("facets" if facets else "full"), entries
        else:
            seen = set(row.get("seen", []))
            todo = [e for e in entries if e["ehash"] not in seen]
            if not todo:
                continue
            changed = sum(len(e["text"]) for e in todo)
            mode = "facets" if facets else ("full" if changed > 0.5 * len(text) else "partial")
            if mode == "full":
                todo = entries
        items.append({"logical": logical, "mode": mode, "reread": fresh, "source": d["source"], "project": d["project"],
                      "sensitive": d["sensitive"], "body_sha": sha(text), "title": d["title"], "all": entries,
                      "todo": todo})
    return items


def render_item(n, item, part=None):
    head = "=== FILE %d: %s (folder project: %s)" % (n, item["logical"], item["project"])
    if item["mode"] == "facets":
        return head + " FACETS ONLY ===\n" + "".join(e["text"] for e in item["all"])
    if part:
        head += " PART %d of %d" % part
    if item["mode"] == "partial":
        head += " ONLY CHANGED SECTIONS of the note titled %r" % item["title"]
    body = []
    for e in item["todo"]:
        prefix = "[E%d] " % e["n"]
        if item["mode"] == "partial":
            prefix += "(section path: %s) " % " > ".join(e["heading_path"]) if e["heading_path"] else ""
        body.append(prefix + e["text"])
    return head + " ===\n" + "".join(body)


def pack(wave, limit=None, run_name=None, reread=False, only=None):
    ensure_fresh()
    reg = load_registry()
    items = work_items(wave, reread, only)
    items.sort(key=lambda it: (wave_of(it["logical"], it["source"]), it["logical"]))
    if limit:
        items = items[:limit]
    base = run_name or "w%s-%s" % (wave, datetime.now().strftime("%Y%m%d-%H%M%S"))
    run_name, n = base, 1
    while os.path.exists(os.path.join(STATE, "classify", run_name)):
        n += 1
        run_name = "%s-%d" % (base, n)
    run_dir = os.path.join(STATE, "classify", run_name)
    os.makedirs(os.path.join(run_dir, "batches"))
    head = PROMPT_HEAD.format(domains=", ".join(reg["domains"]), tech=", ".join(sorted(reg["tech"]))) + project_list(reg)
    units = []
    for it in items:
        size = sum(len(e["text"]) for e in (it["all"] if it["mode"] == "facets" else it["todo"]))
        if it["mode"] != "facets" and size > PART_CHARS:
            parts, cur, cur_len = [], [], 0
            for e in it["todo"]:
                if cur and cur_len + len(e["text"]) > PART_CHARS:
                    parts.append(cur)
                    cur, cur_len = [], 0
                cur.append(e)
                cur_len += len(e["text"])
            parts.append(cur)
            for i, chunk in enumerate(parts, 1):
                units.append((dict(it, todo=chunk), (i, len(parts)), sum(len(e["text"]) for e in chunk)))
        else:
            units.append((it, None, size))
    batches, cur, cur_len = [], [], 0
    for unit in units:
        memory = unit[0]["mode"] == "facets"
        cap, max_items = (MEMORY_BATCH_CHARS, MEMORY_MAX_ITEMS) if memory else (BATCH_CHARS, MAX_ITEMS)
        if cur and (cur_len + unit[2] > cap or len(cur) >= max_items or (cur[0][0]["mode"] == "facets") != memory):
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(unit)
        cur_len += unit[2]
    if cur:
        batches.append(cur)
    for bi, batch in enumerate(batches, 1):
        bid = "b%04d" % bi
        texts, mapping = [], []
        for n, (it, part, _) in enumerate(batch, 1):
            texts.append(render_item(n, it, part))
            mapping.append({"n": n, "logical": it["logical"], "mode": it["mode"], "reread": it["reread"], "part": part,
                            "source": it["source"],
                            "sensitive": it["sensitive"], "body_sha": it["body_sha"],
                            "all_ehashes": [e["ehash"] for e in it["all"]],
                            "entries": {str(e["n"]): {"ehash": e["ehash"], "anchor": clean(e["anchor"]),
                                                      "text": clean(e["text"])} for e in it["todo"]}})
        with open(os.path.join(run_dir, "batches", bid + ".prompt"), "w") as fh:
            fh.write(head + "\n" + "\n\n".join(clean(t) for t in texts) + "\n")
        with open(os.path.join(run_dir, "batches", bid + ".json"), "w") as fh:
            json.dump({"items": mapping}, fh)
    with open(os.path.join(run_dir, "schema.json"), "w") as fh:
        json.dump(classify_schema(), fh)
    meta = {"model": CLASSIFY_MODEL, "effort": CLASSIFY_EFFORT, "wave": wave, "reread": reread,
            "prompt_sha": sha(head)[:12], "created": datetime.now().isoformat(),
            "batches": len(batches), "items": len(items), "chars": sum(u[2] for u in units)}
    with open(os.path.join(run_dir, "meta.json"), "w") as fh:
        json.dump(meta, fh, indent=1)
    if not batches:
        with open(os.path.join(run_dir, "imported"), "w") as fh:
            fh.write("empty")
    return run_name, meta


def project_list(reg):
    """The project list the reader tags from; the critic gets the same one."""
    return "\nProjects (id: what it is; sessions that work on it):\n" + "".join(
        "- %s%s%s\n" % (pid, ": " + p["about"] if p.get("about") else "",
                        "; sessions " + ", ".join(x + "*" for x in p["sessions"]) if p.get("sessions") else "")
        for pid, p in sorted(reg["projects"].items()))


def remote(cmd, input_bytes=None, timeout=300):
    return subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", REMOTE, cmd], input=input_bytes,
                          capture_output=True, timeout=timeout)


def start(run_name, parallel=2, wait=False):
    run_dir = os.path.join(STATE, "classify", run_name)
    if REMOTE == "local":
        args = [sys.executable, os.path.join(HERE, "runner.py"), run_dir, str(parallel)]
        if wait:
            return subprocess.run(args).returncode
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=open(os.path.join(run_dir, "runner.log"), "w"))
        with open(os.path.join(run_dir, "runner.pid"), "w") as fh:
            fh.write(str(proc.pid))
        return 0
    tar = subprocess.run(["tar"] + (["--no-mac-metadata"] if sys.platform == "darwin" else []) +
                         ["-C", os.path.join(STATE, "classify"), "-cf", "-", run_name],
                         capture_output=True, env=dict(os.environ, COPYFILE_DISABLE="1")).stdout
    target = "%s/%s" % (REMOTE_RUNS, run_name)
    r = remote("mkdir -p %s && tar -C %s -xf -" % (REMOTE_RUNS, REMOTE_RUNS), input_bytes=tar, timeout=300)
    if r.returncode:
        raise RuntimeError("ship failed: %s" % r.stderr.decode()[-500:])
    r = remote("cat > %s/runner.py" % target, input_bytes=open(os.path.join(HERE, "runner.py"), "rb").read())
    if r.returncode:
        raise RuntimeError("runner copy failed: %s" % r.stderr.decode()[-500:])
    session = "brain-" + run_name
    r = remote("tmux has-session -t %s 2>/dev/null || tmux new-session -d -s %s "
               "'python3 %s/runner.py %s %d > %s/runner.log 2>&1'" % (session, session, target, target, parallel, target))
    if r.returncode:
        raise RuntimeError("tmux start failed: %s" % r.stderr.decode()[-500:])
    return 0


def runner_alive(pid_file):
    """A local runner is alive while its pid runs. waitpid reaps it when this process started it, so a finished
    child never looks alive as a zombie."""
    if not os.path.exists(pid_file):
        return False
    pid = int(open(pid_file).read())
    try:
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def status(run_name):
    run_dir = os.path.join(STATE, "classify", run_name)
    if REMOTE == "local":
        p = os.path.join(run_dir, "progress.json")
        prog = json.load(open(p)) if os.path.exists(p) else {"state": "not started"}
        prog["finished"] = os.path.exists(os.path.join(run_dir, "finished"))
        prog["alive"] = runner_alive(os.path.join(run_dir, "runner.pid"))
        return prog
    r = remote("cat %s/%s/progress.json 2>/dev/null; echo; tmux has-session -t brain-%s 2>/dev/null && echo ALIVE; "
               "test -f %s/%s/finished && echo FINISHED" % (REMOTE_RUNS, run_name, run_name, REMOTE_RUNS, run_name))
    if r.returncode == 255:
        raise RuntimeError("cannot reach %s over ssh" % REMOTE)
    out = r.stdout.decode()
    body = out[:out.rfind("}") + 1]
    prog = json.loads(body) if body.strip().startswith("{") else {"state": "no progress yet"}
    prog["finished"] = "FINISHED" in out.splitlines()
    prog["alive"] = "ALIVE" in out.splitlines()
    return prog


def pull(run_name):
    run_dir = os.path.join(STATE, "classify", run_name)
    if REMOTE == "local":
        return
    r = remote("cd %s/%s && tar -cf - out err progress.json 2>/dev/null" % (REMOTE_RUNS, run_name), timeout=600)
    if r.stdout:
        subprocess.run(["tar", "-C", run_dir, "-xf", "-"], input=r.stdout, check=True)


def import_run(run_name):
    """Validate every model answer against what the model was shown, then merge it into the catalog.

    This is the catalog's only writer. A quote that is not in its own section nulls its field, and the null rate
    per field is reported, so a check that nulls everything shows up as a failure instead of passing quietly."""
    lock = locked("catalog")
    try:
        pull(run_name)
        run_dir = os.path.join(STATE, "classify", run_name)
        meta = json.load(open(os.path.join(run_dir, "meta.json")))
        vocab = Vocab(load_registry())
        domains = set(vocab.reg["domains"])
        catalog = load_catalog()
        stats = {"batches": 0, "missing_batches": [], "items": 0, "items_missing": [], "entries": 0,
                 "bad_entry_numbers": 0, "duplicate_n": 0, "fields": {}}
        merged, missing = {}, {}
        for f in sorted(os.listdir(os.path.join(run_dir, "batches"))):
            if not f.endswith(".json") or f.startswith("."):
                continue
            bid = f[:-5]
            mapping = json.load(open(os.path.join(run_dir, "batches", f)))["items"]
            out_path = os.path.join(run_dir, "out", bid + ".json")
            if not os.path.exists(out_path):
                stats["missing_batches"].append(bid)
                continue
            stats["batches"] += 1
            answer = json.load(open(out_path))
            files = {}
            for x in (answer.get("structured_output") or {}).get("files", []):
                if isinstance(x, dict):
                    stats["duplicate_n"] += x.get("n") in files
                    files.setdefault(x.get("n"), x)  # a repeated number is a later file mislabelled; it reads as missing
            for item in mapping:
                ans = files.get(item["n"])
                if ans is None:
                    stats["items_missing"].append(item["logical"])
                    missing[item["logical"]] = item
                    continue
                stats["items"] += 1
                acc = merged.setdefault(item["logical"], {"item": item, "file": None, "entries": {}, "seen": set(), "text": ""})
                acc["text"] += "".join(x["text"] for x in item["entries"].values())
                if acc["file"] is None and item["mode"] != "partial":
                    acc["file"] = ans
                acc["seen"].update(e["ehash"] for e in item["entries"].values())
                if item["mode"] == "facets":
                    acc["seen"].update(item["all_ehashes"])
                scopes = section_scopes(item["entries"])
                shown = "".join(x["text"] for x in item["entries"].values())
                read_tech = {cid for f, cid in vocab.mentions(shown) if f == "tech"}
                for e in ans.get("entries") or []:
                    src = item["entries"].get(str(e.get("e")))
                    if src is None:
                        stats["bad_entry_numbers"] += 1
                        continue
                    src = dict(src, scope_text=scopes.get(str(e.get("e")), src["text"]), read_tech=read_tech,
                               home_project=vocab.project_for_path(item["logical"]),
                               copied_from=vocab.copied_from(item["logical"]))
                    cleaned = clean_entry(e, src, vocab, domains, stats)
                    if cleaned is not None:
                        acc["entries"][(src["ehash"], cleaned["item"])] = cleaned
                        stats["entries"] += 1
        for logical, acc in merged.items():
            item, f = acc["item"], acc["file"]
            row = catalog.get(logical) or {"logical": logical, "entries": []}
            if f is not None:
                row.update({"kind": f.get("kind"), "use": f.get("use"),
                            "domain": [d if d in domains else "other:" + d.split(":")[-1] for d in f.get("domain", [])][:3],
                            "tech": grounded(sorted({t for t in (vocab.fold(x, "tech") for x in f.get("tech", [])) if t}),
                                             "tech", acc["text"], vocab),
                            "projects": sorted({p for p in (vocab.fold(x, "project") for x in f.get("projects", [])) if p}
                                               | vocab.copied_from(logical)),
                            "description": "" if SECRET_RE.search(f.get("description") or "") else (f.get("description") or "")[:300]})
            current = set(item["all_ehashes"])
            # A section the model was shown this run gets exactly the items it returned now; the rest keep theirs (a reread
            # shows every section, so it replaces them all).
            keep = {(e["ehash"], e.get("item", 1)): e for e in row.get("entries", [])
                    if e["ehash"] in current and e["ehash"] not in acc["seen"]}
            keep.update(acc["entries"])
            row["entries"] = sorted(keep.values(), key=lambda e: (e["n"], e.get("item", 1)))
            row["seen"] = sorted((set(row.get("seen", [])) & current) | acc["seen"])
            row.pop("reread", None)
            row.update({"body_sha": item["body_sha"], "source": item["source"], "sensitive": item["sensitive"],
                        "model": meta["model"], "effort": meta["effort"], "run": run_name,
                        "classified": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            catalog[logical] = row
        for logical, item in missing.items():
            if catalog.get(logical) and (meta.get("reread") or item.get("reread")):
                catalog[logical]["reread"] = True  # its old seen list would hide it from the next pack
        write_catalog(catalog)
        stats["docs_updated"] = len(merged)
        stats["null_rate"] = {k: round(v["nulled"] / v["returned"], 3) for k, v in stats["fields"].items() if v["returned"]}
        # A rule or the model broke when one of these jumps; normal runs sit at 0 to 3%.
        alarms, returned = [], stats["entries"] + stats.get("entries_dropped_empty", 0)
        if returned >= 10 and stats.get("entries_dropped_empty", 0) > 0.1 * returned:
            alarms.append("dropped %d of %d entries as empty" % (stats["entries_dropped_empty"], returned))
        for k, v in stats["fields"].items():
            if v["returned"] >= 10 and v["nulled"] > 0.2 * v["returned"]:
                alarms.append("quote check emptied %d of %d %s" % (v["nulled"], v["returned"], k))
        asked = stats["items"] + len(stats["items_missing"])
        if asked >= 20 and len(stats["items_missing"]) > 0.05 * asked:
            alarms.append("model skipped %d of %d notes" % (len(stats["items_missing"]), asked))
        stats["alarms"] = alarms
        os.makedirs(os.path.join(BRAIN, "review"), exist_ok=True)
        with open(os.path.join(BRAIN, "review", "classify-%s.json" % run_name), "w") as fh:
            json.dump(dict(stats, missing_batches=stats["missing_batches"][:50], items_missing=stats["items_missing"][:50]),
                      fh, indent=1)
        return stats
    finally:
        lock.close()


def repair_quote(quote, scope_text):
    """The exact sentence or line in scope closest to a near-miss quote (difflib ratio 0.9 or more), else None."""
    import difflib
    q = quote.strip().lower()
    best, best_r = None, 0.0
    for piece in re.split(r"(?<=[.!?])\s+|\n+", scope_text):
        piece = piece.strip()
        if not piece:
            continue
        low = piece.lower()
        blocks = [b for b in difflib.SequenceMatcher(None, q, low, autojunk=False).get_matching_blocks() if b.size]
        if not blocks:
            continue
        span = piece[blocks[0].b: blocks[-1].b + blocks[-1].size]
        r = difflib.SequenceMatcher(None, q, span.lower(), autojunk=False).ratio()
        if r > best_r:
            best, best_r = span, r
    return best if best_r >= 0.9 else None


def grounded(values, facet, scope_text, vocab, keep=()):
    """Keep a tech or project value only when its name or an alias appears in the text it was read from."""
    present = {cid for f, cid in vocab.mentions(scope_text) if f == facet}
    low = scope_text.lower()
    out = []
    for v in values:
        if v in keep or v in present or (v.startswith("other:") and v[6:].replace("-", " ") in low.replace("-", " ")):
            out.append(v)
    return sorted(set(out))


def section_scopes(entries):
    """Quote-check scope per entry number: the entry's own text plus the deeper sections right after it, so a heading
    with no body of its own is checked against its subsections (the model reads them as one).
    entries: {number: {"anchor": ..., "text": ...}} for consecutive or partial sections."""
    order = sorted(entries, key=int)
    level = lambda a: len(re.match(r"^(#*)", a).group(1)) or 9
    scopes = {}
    for i, k in enumerate(order):
        lv = level(entries[k]["anchor"])
        parts, prev = [entries[k]["text"]], int(k)
        for k2 in order[i + 1:]:
            if int(k2) != prev + 1 or level(entries[k2]["anchor"]) <= lv:
                break
            parts.append(entries[k2]["text"])
            prev = int(k2)
        scopes[k] = "".join(parts)
    return scopes


def clean_entry(e, src, vocab, domains, stats):
    scope_raw = src.get("scope_text") or src["text"]
    text = qnorm(scope_raw)
    out = {"ehash": src["ehash"], "n": int(e["e"]), "item": max(1, int(e.get("item") or 1)), "anchor": src["anchor"],
           "type": e.get("type")}
    for k in QUOTE_FIELDS:
        v = e.get(k)
        st = stats["fields"].setdefault(k, {"returned": 0, "nulled": 0})
        if v:
            st["returned"] += 1
            if qnorm(v) not in text:
                fixed = repair_quote(v, scope_raw)
                if fixed:
                    st["repaired"] = st.get("repaired", 0) + 1
                    out.setdefault("repaired", []).append(k)
                    v = fixed
                else:
                    st["nulled"] += 1
                    out.setdefault("nulled", []).append(k)
                    v = None
            if v and SECRET_RE.search(v):
                st["secret"] = st.get("secret", 0) + 1
                out.setdefault("nulled", []).append(k + ":secret")
                v = None
        out[k] = v
    out["root_cause"] = (e.get("root_cause") or None) if out["root_cause_quote"] else None
    out["reuse"] = e.get("reuse") or None
    if out["reuse"]:
        named = {cid for f, cid in vocab.mentions(out["reuse"]) if f == "tech"}
        if named - src["read_tech"]:
            out["reuse"] = None
            out.setdefault("nulled", []).append("reuse:names-tech-not-in-note")
    for k in ("root_cause", "reuse"):
        if out[k] and SECRET_RE.search(out[k]):
            out[k] = None
            out.setdefault("nulled", []).append(k + ":secret")
    out["anchor"] = SECRET_RE.sub("[redacted]", out["anchor"])
    tech = sorted({t for t in (vocab.fold(x, "tech") for x in e.get("tech") or []) if t})
    out["tech"] = grounded(tech, "tech", scope_raw, vocab, keep=src["read_tech"])  # named anywhere in what the model read
    stats["tags_dropped"] = stats.get("tags_dropped", 0) + len(tech) - len(out["tech"])
    out["domain"] = [d if d in domains else "other:" + d.split(":")[-1] for d in (e.get("domain") or [])][:3]
    projects = sorted({p for p in (vocab.fold(x, "project") for x in e.get("projects") or []) if p})
    out["projects"] = sorted(set(grounded(projects, "project", scope_raw, vocab, keep=(src["home_project"],)))
                             | src["copied_from"]) or ([src["home_project"]] if src["home_project"] in vocab.reg["projects"] else [])
    if not (out["root_cause"] or out["fix_quote"] or out["reuse"]):
        stats["entries_dropped_empty"] = stats.get("entries_dropped_empty", 0) + 1
        return None
    return out


def write_catalog(catalog):
    os.makedirs(CATALOG, exist_ok=True)
    for name, rows in (("notes.jsonl", [r for r in catalog.values() if not r.get("sensitive")]),
                       ("local.jsonl", [r for r in catalog.values() if r.get("sensitive")])):
        path = os.path.join(CATALOG, name)
        with open(path + ".tmp", "w") as fh:
            for r in sorted(rows, key=lambda r: r["logical"]):
                fh.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        os.replace(path + ".tmp", path)


# ---------------------------------------------------------------- critic (monthly quality audit)

# An independent model judges a fresh sample of the catalog against the note text every month, so quality is measured
# without anyone asking. Strata and the 10% gate as in review/critic-classify.md and review/critic2-classify.md.
CRITIC_MODEL = os.environ.get("BRAIN_CRITIC_MODEL", "claude-opus-5-5")
CRITIC_ROWS_PER_BATCH = 20
CRITIC_BATCH_CHARS = 150000
CRITIC_SECTION_CHARS = 40000  # the largest sampled scopes run 38k to 46k; at 12k the critic could not see their quotes
CRITIC_GATE = 0.10
CRITIC_GATED = ("bug", "reuse", "file", "memory")
CRITIC_FILE_FIELDS = ["kind", "use", "domain", "tech", "projects", "description"]
CRITIC_BRIEF = """You are the critic of an LLM reading pass over engineering notes. Judge each sampled field strictly against
the source text in the same row. Do not trust the fields; look for an invented cause, a wrong quote, a wrong tag, and a
finding the reader missed.

Each row below is JSON with sid, stratum, logical and:
- stratum bug, reuse or nulled: entry (the fields to judge) and section_text (the section they must come from, with
  its subsections, as the reader saw it). When one section holds several separate findings (numbered items, table
  rows, bold-led paragraphs), the reader returns one entry per finding, numbered by entry.item: judge an entry against
  its own finding, not the others in the section.
  reuse rows are method, decision or lesson entries with no quote: judge whether reuse states what the section says.
  nulled rows are entries where an automatic check emptied a field: check the fields that remain.
- stratum file: file (kind, use, domain, tech, projects, description), entries (what the reader extracted), file_text.
- stratum memory: file (file-level fields) and file_text.
- stratum skipped: a section_text the reader chose not to extract.

The reader's rules: kind is first-match-wins from log, incident, bug-writeup, decision, lesson, checklist, runbook,
plan, research, reference, explanation, index; use is follow, consult, avoid, decide or history; quotes are copied
exactly from the same section; root_cause is supported by root_cause_quote; reuse is written only from the section;
tech names the tool the problem or fix is in, named in the section or by a command or file that belongs only to it (a
.py script is python), never one mentioned in passing or one from another finding; projects names every project the
section is about, and a session name or site name counts as naming its project (the project list below).
Word limits are style limits: never mark a field wrong for length alone.

Verdicts per applicable field: right (correct and supported), wrong (contradicts the text, misclassifies, or is null
although the text clearly states it), unsupported (claims something the text does not state), n/a (null, nothing to fill).
- bug, reuse and nulled rows: type, symptom_quote, root_cause, fix_quote, reuse, tech, projects.
- file and memory rows: kind, use, domain, tech, projects, description; file rows also extracted_entries (right if the
  extracted sections are the ones worth extracting, wrong if an important bug or method was missed or junk extracted).
- skipped rows: missed_entry (right if skipping was correct, wrong if it should have been extracted).
Return one verdict object per row with a one-line note naming what is wrong, empty when all is right.
"""


def critic_sample(seed):
    """A reproducible sample of the catalog for the critic; local-only notes are never sampled."""
    rnd = random.Random(seed)
    manifest = json.load(open(os.path.join(STATE, "manifest.json")))["docs"]
    catalog = load_catalog()
    rows = []
    sections = lambda abspath: {e["ehash"]: e for e in split_entries(read_text(abspath))}

    def scopes(abspath):  # the section plus its subsections: what import checked the quotes against
        numbered = {str(i): e for i, e in enumerate(split_entries(read_text(abspath)), 1)}
        scoped = section_scopes(numbered)
        return {e["ehash"]: scoped[k] for k, e in numbered.items()}

    def add(stratum, logical, extra):
        d = manifest[logical]
        if not d.get("sensitive"):
            rows.append(dict({"sid": "s%03d" % (len(rows) + 1), "stratum": stratum, "logical": logical}, **extra))

    bugs, quoteless, nulled = [], [], []
    for logical, row in sorted(catalog.items()):
        if logical not in manifest:
            continue
        for e in row.get("entries", []):
            (bugs if e.get("type") == "bug" else []).append((logical, e))
            if e.get("type") != "bug" and e.get("reuse") and not any(e.get(k) for k in QUOTE_FIELDS):
                quoteless.append((logical, e))
            if e.get("nulled"):
                nulled.append((logical, e))
    for stratum, pool, n in (("bug", bugs, 50), ("reuse", quoteless, 40), ("nulled", nulled, 25)):
        for logical, e in rnd.sample(pool, min(n, len(pool))):
            scope = scopes(manifest[logical]["abspath"]).get(e["ehash"])
            if scope:
                add(stratum, logical, {"entry": e, "section_text": clean(scope)[:CRITIC_SECTION_CHARS]})
    notes = sorted(l for l, r in catalog.items() if l in manifest and r.get("source") == "note")
    for logical in rnd.sample(notes, min(40, len(notes))):
        r = catalog[logical]
        add("file", logical, {"file": {k: r.get(k) for k in CRITIC_FILE_FIELDS}, "entries": r.get("entries", []),
                              "file_text": clean(read_text(manifest[logical]["abspath"]))[:20000]})
    skipped = []
    for logical, r in sorted(catalog.items()):
        if logical in manifest and r.get("source") == "note" and wave_of(logical, "note") == 1:
            taken, seen = {e["ehash"] for e in r.get("entries", [])}, set(r.get("seen", []))
            skipped += [(logical, sec) for h, sec in sections(manifest[logical]["abspath"]).items()
                        if h not in taken and h in seen and len(sec["text"]) > 200]
    for logical, sec in rnd.sample(skipped, min(20, len(skipped))):
        add("skipped", logical, {"section_text": clean(sec["text"])[:CRITIC_SECTION_CHARS]})
    mem = sorted(l for l, r in catalog.items() if l in manifest and r.get("source") == "memory")
    for logical in rnd.sample(mem, min(15, len(mem))):
        r = catalog[logical]
        add("memory", logical, {"file": {k: r.get(k) for k in CRITIC_FILE_FIELDS},
                                "file_text": clean(read_text(manifest[logical]["abspath"]))})
    return rows


def critic_score(verdicts):
    """{(stratum, field): {right, wrong, unsupported}} and the cells over the gate."""
    cells = {}
    for v in verdicts:
        for field, verdict in (v.get("fields") or {}).items():
            c = cells.setdefault((v.get("stratum"), field), {"right": 0, "wrong": 0, "unsupported": 0})
            if verdict in c:
                c[verdict] += 1
    over = []
    for (stratum, field), c in sorted(cells.items()):
        judged = sum(c.values())
        bad = (c["wrong"] + c["unsupported"]) / judged if judged else 0
        c["bad"] = round(bad, 3)
        if stratum in CRITIC_GATED and judged >= 10 and bad > CRITIC_GATE:
            over.append("%s %s %.0f%%" % (stratum, field, 100 * bad))
    return cells, over


def critic_pack(run_name, seed):
    rows = critic_sample(seed)
    run_dir = os.path.join(STATE, "classify", run_name)
    os.makedirs(os.path.join(run_dir, "batches"))
    batches, size = [[]], 0
    for r in rows:  # by size too: a few rows carry 40k of section text
        n = len(json.dumps(r, ensure_ascii=False))
        if batches[-1] and (len(batches[-1]) == CRITIC_ROWS_PER_BATCH or size + n > CRITIC_BATCH_CHARS):
            batches.append([])
            size = 0
        batches[-1].append(r)
        size += n
    batches = [b for b in batches if b]
    brief = CRITIC_BRIEF + project_list(load_registry()) + "\nCRITIC ROWS\n"
    for bi, batch in enumerate(batches, 1):
        with open(os.path.join(run_dir, "batches", "b%04d.prompt" % bi), "w") as fh:
            fh.write(brief + "\n".join(json.dumps(r, ensure_ascii=False) for r in batch) + "\n")
        with open(os.path.join(run_dir, "batches", "b%04d.json" % bi), "w") as fh:
            json.dump({"rows": [{"sid": r["sid"], "stratum": r["stratum"], "logical": r["logical"]} for r in batch]}, fh)
    verdict = {"type": "object", "required": ["sid", "stratum", "fields", "note"], "properties": {
        "sid": {"type": "string"}, "stratum": {"type": "string"}, "note": {"type": "string", "maxLength": 400},
        "fields": {"type": "object", "additionalProperties": {"enum": ["right", "wrong", "unsupported", "n/a"]}}}}
    with open(os.path.join(run_dir, "schema.json"), "w") as fh:
        json.dump({"type": "object", "required": ["verdicts"],
                   "properties": {"verdicts": {"type": "array", "items": verdict}}}, fh)
    meta = {"kind": "critic", "model": CRITIC_MODEL, "effort": "high", "seed": seed, "rows": len(rows),
            "batches": len(batches), "created": datetime.now().isoformat()}
    with open(os.path.join(run_dir, "meta.json"), "w") as fh:
        json.dump(meta, fh, indent=1)
    return meta


def critic_import(run_name):
    """Score a finished critic run. The verdicts, which quote note text, stay in state/; review/ gets counts only."""
    pull(run_name)
    run_dir = os.path.join(STATE, "classify", run_name)
    verdicts, missing = [], 0
    for f in sorted(os.listdir(os.path.join(run_dir, "batches"))):
        if not f.endswith(".json") or f.startswith("."):
            continue
        out = os.path.join(run_dir, "out", f)
        sids = {r["sid"] for r in json.load(open(os.path.join(run_dir, "batches", f)))["rows"]}
        got = (json.load(open(out)).get("structured_output") or {}).get("verdicts", []) if os.path.exists(out) else []
        got = [v for v in got if isinstance(v, dict) and v.get("sid") in sids]
        missing += len(sids) - len({v["sid"] for v in got})
        verdicts += got
    with open(os.path.join(run_dir, "verdicts.jsonl"), "w") as fh:
        for v in verdicts:
            fh.write(json.dumps(v, ensure_ascii=False) + "\n")
    cells, over = critic_score(verdicts)
    lines = ["# Critic %s" % run_name, "",
             "Written by `brain.py critic import` on %s. %s judged %d sampled rows (seed %s) against the note text; "
             "%d rows got no verdict. Verdict notes: `state/classify/%s/verdicts.jsonl` (local, they quote notes)."
             % (datetime.now().strftime("%Y-%m-%d"), CRITIC_MODEL, len(verdicts), json.load(open(os.path.join(run_dir, "meta.json")))["seed"],
                missing, run_name), "",
             "Over the %d%% gate: %s" % (100 * CRITIC_GATE, ", ".join(over) or "nothing"), "",
             "| stratum | field | right | wrong | unsupported | bad |", "|---|---|---|---|---|---|"]
    lines += ["| %s | %s | %d | %d | %d | %.0f%% |" % (st, fd, c["right"], c["wrong"], c["unsupported"], 100 * c["bad"])
              for (st, fd), c in sorted(cells.items())]
    bad_rows = ["%s (%s): %s" % (v["sid"], v.get("stratum"), ", ".join(k for k, x in v["fields"].items() if x in ("wrong", "unsupported")))
                for v in verdicts if any(x in ("wrong", "unsupported") for x in (v.get("fields") or {}).values())]
    lines += ["", "Rows with a wrong or unsupported field:", ""] + ["- " + b for b in bad_rows]
    os.makedirs(os.path.join(BRAIN, "review"), exist_ok=True)
    with open(os.path.join(BRAIN, "review", run_name + ".md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return {"rows": len(verdicts), "missing": missing, "over_gate": over}


# ---------------------------------------------------------------- hints (for the Claude Code hook)

# Rules measured on 1,400 real Bash failures from the Projects transcripts (2026-10-22 to 10-26): most failures carry
# no specific error (a grep that matched nothing) or print note text, so a hint needs a tool-shaped error line with an
# error word, and a past finding that shares three of its informative words, one of them in under 1% of entries (shared
# exception-template words like "module named" or "expecting value" matched unrelated notes).
HINT_ERR_RE = re.compile(r"^(\S+:\s|\s*[\w.]*(Error|Exception)\b)|^.{0,40}\b(Error|ERROR|error|fatal|FATAL|Fatal):\s")
HINT_ERRWORD_RE = re.compile(r"(?i)\b(error|exception|failed|failure|fatal|denied|not found|cannot|can't|could not|unable|"
                             r"refused|timed? ?out|traceback|undefined|invalid|missing|no such|forbidden|unauthori[sz]ed|"
                             r"conflict|rejected|exceeded)\b")
HINT_TEXTY_RE = re.compile(r"^\s*([|>#*+-]|\d+[:-]|\S+:\d+:|[0-9a-f]{7,}\s|20\d\d-\d\d-\d\d|(name|description|type|metadata|"
                           r"title|related):|no error)|\[\[", re.I)
HINT_READONLY_RE = re.compile(r"^\s*(cd [^;&|\n]+(&&|;)\s*)?(grep|rg|cat|sed|head|tail|ls|find|test|\[|wc|diff|awk|jq|stat|file|"
                              r"echo|printf|which|type|git (log|show|diff|status|grep))\b")
HINT_GENERIC = set("error errors exception failed failure fatal denied found cannot unable refused timeout timed invalid "
                   "missing such forbidden code exit line file files command commands warning eval zsh bash shell usage "
                   "none true false null undefined traceback most recent call last raise raised directory already page "
                   "exceeded running anything neither still".split())
HINT_NOISE_RE = re.compile(r"(/[\w.@~-]+)+|\b[0-9a-f]{7,}\b|\b\d+(\.\d+)*\b|https?://\S+")


def hint_words(text):
    return {w for w in re.findall(r"[a-z][a-z0-9_]{3,}", HINT_NOISE_RE.sub(" ", (text or "").lower()))
            if w not in STOPWORDS and w not in HINT_GENERIC}


def error_line(text):
    """The last line of a command's output that looks like a tool's error, or None. Bash only: on 528 MCP tool failures
    (browser tools) the same matching gave 7 hints, all wrong, so the hook does not hint on them."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip() and not l.startswith("Exit code")]
    found = [l for l in lines if HINT_ERR_RE.search(l) and HINT_ERRWORD_RE.search(l) and not HINT_TEXTY_RE.search(l)
             and len(hint_words(l)) >= 4]
    return found[-1][:300] if found else None


def error_hint(text, command="", db=None, vocab=None):
    """A past finding that matches this failure, as {key, text}, or None. Never rebuilds the index."""
    if command and HINT_READONLY_RE.search(command):
        return None
    line = error_line(text)
    if not line or index_state() == "unusable":
        return None
    own = db is None
    db = db or sqlite3.connect("file:%s?mode=ro" % INDEX, uri=True)
    try:
        want = hint_words(line)
        n = db.execute("SELECT count(*) FROM fts").fetchone()[0]
        rare = lambda w: db.execute("SELECT count(*) FROM fts WHERE fts MATCH ?", ('"%s"' % w,)).fetchone()[0] <= max(2, 0.01 * n)
        for h in search(line, kind="note", limit=5, per_doc=1, db=db, vocab=vocab or Vocab(load_registry()))[1]:
            if not (h["type"] in ("bug", "lesson", "method", "decision") or h["source"] == "memory"):
                continue
            seen = hint_words(" ".join(h["matched"] + [h["anchor"]] + [x for x in (h["symptom"], h["cause"], h["fix"]) if x]))
            shared = want & seen
            if len(shared) >= 3 and len(shared) >= 0.5 * len(want) and any(rare(w) for w in shared):
                parts = ["Brain: a past note matches this error. Read it before debugging: %s:%s (%s)"
                         % (h["abspath"], h["line"], re.sub(r"^#+\s*", "", h["anchor"])[:120])]
                if h["sensitive"]:  # a local-only note's text never travels into a session that might commit it
                    parts.append("(a local-only note: open it to read the cause and fix)")
                else:
                    parts += ["%s: %s" % (k, h[v]) for k, v in (("cause", "cause"), ("fix", "fix"), ("reuse (suggestion)", "reuse")) if h[v]]
                return {"key": "%s:%s" % (h["logical"], h["line"]), "text": "\n".join(parts), "line": line}
        return None
    finally:
        if own:
            db.close()


def session_intro(cwd, vocab=None):
    """One note for a session working under the projects folder: the brain exists, and where its project's findings are."""
    root, cwd = os.path.realpath(PROJECTS), os.path.realpath(cwd)
    if not (cwd + "/").startswith(root + "/"):
        return None
    rel = os.path.relpath(cwd, root)
    project = (vocab or Vocab(load_registry())).project_for_path(rel + "/x") if rel != "." else None
    findings = os.path.join(VIEWS, "projects", re.sub(r"[^a-z0-9._-]+", "-", (project or "").lower()) + ".md")
    out = ["Brain: every past note, memory file and commit in %s is searchable. Before debugging an error or "
           "redoing a kind of task, run `~/.claude/skills/brain/brain search \"<error text or task>\"` with two or three "
           "wordings and open the hit. When a command fails, a matching past fix is added here automatically."
           % os.path.basename(root)]
    if project and os.path.exists(findings):
        out.append("Findings for %s (bugs, decisions, methods, including ones buried in session logs): %s" % (project, findings))
    if NOTE_CONVENTIONS:
        out.append("Write a finding (non-obvious bug cause, decision, method) as an entry in a topic doc per "
                   "%s; the session log gets one line pointing to it." % NOTE_CONVENTIONS)
    return "\n".join(out)


# ---------------------------------------------------------------- views, health, lint

VIEWS = os.path.join(BRAIN, "views")
ARCHIVE = setting("archive_dir", "BRAIN_ARCHIVE")  # a folder (another drive) for daily restore copies; unset skips them
NOTIFY = setting("notify", None)  # a command (argv list) that takes one message line, run on failures and results
NOTE_CONVENTIONS = setting("note_conventions", None)  # where sessions are told to read how to write a finding
QUERIES = os.path.expanduser(setting("queries", "BRAIN_QUERIES", os.path.join(HERE, "review/queries.jsonl")))
PUSH_CATALOG = setting("push_catalog", None, False)  # the daily job commits and pushes catalog/notes.jsonl
HOOK_STATE = os.environ.get("BRAIN_HOOK_STATE") or os.path.join(HOME, ".local/state/brain-hook")

def md_link(abspath, line):
    return "`%s:%s`" % (abspath, line) if line else "`%s`" % abspath


LOG_NOTE_RE = re.compile(r"session-log|-log\.md$|/log\.md$|crawl-log|loop-log")


def hook_report():
    """Hints given and hook errors in the last 24 h, per machine. The hook fails open, so this is how a broken hook
    gets noticed."""
    since = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    def summarize(hints, errors):
        recent = lambda text: [json.loads(l) for l in text.splitlines() if l.strip().startswith("{") and json.loads(l)["ts"] >= since]
        errs = recent(errors)
        return {"hints_24h": len(recent(hints)), "errors_24h": len(errs), "last_error": errs[-1]["error"] if errs else None}
    read = lambda name: open(os.path.join(HOOK_STATE, name)).read() if os.path.exists(os.path.join(HOOK_STATE, name)) else ""
    out = {"local": summarize(read("hints.jsonl"), read("errors.log"))}
    if REMOTE != "local":
        # The remote shell may be zsh, which reads an unquoted =word as a command lookup; no error log yet is not a failure.
        r = remote("cat ~/.local/state/brain-hook/hints.jsonl 2>/dev/null; echo '===SPLIT==='; "
                   "cat ~/.local/state/brain-hook/errors.log 2>/dev/null; true", timeout=60)
        if r.returncode == 0:
            hints, _, errors = r.stdout.decode(errors="replace").partition("===SPLIT===")
            out[REMOTE] = summarize(hints, errors)
        else:
            out[REMOTE] = {"unreachable": True}
    return out


def project_findings(db, stamp):
    """views/projects/<project>.md: every finding about a project, wherever it was written. Findings in session and
    crawl logs are marked, since a reader of the project's docs would not see them otherwise."""
    rows = db.execute(
        "SELECT g.value, i.type, e.anchor, e.line, e.date, i.symptom, i.cause, i.fix, i.reuse, e.corrected, d.abspath, "
        "d.logical, d.kind FROM items i JOIN entries e ON e.id = i.entry JOIN docs d ON d.id = e.doc "
        "JOIN tags g ON g.entry = e.id AND g.facet = 'project' WHERE e.copy_path IS NULL "
        "ORDER BY g.value, e.date DESC, d.logical, e.line, i.item").fetchall()
    by, memos, catalog = {}, {}, load_catalog()
    for r in rows:
        by.setdefault(r[0], []).append(r)
    for logical, abspath, title, desc in db.execute("SELECT logical, abspath, title, description FROM docs WHERE source='memory'"):
        for p in (catalog.get(logical) or {}).get("projects", []):  # memory notes are read for file fields only
            memos.setdefault(p, []).append((title, desc, abspath))
    folder = os.path.join(VIEWS, "projects")
    os.makedirs(folder, exist_ok=True)
    for f in os.listdir(folder):  # generated only; a project with no findings left loses its file
        os.remove(os.path.join(folder, f))
    made, index = [], ["# Findings by project", "", stamp, "",
                       "One file per project: every bug, decision, method and lesson the reading pass found about it, "
                       "in any note, newest first. `[log]` marks a finding that lives only inside a session or crawl log.",
                       "", "| Project | Findings | Bugs | In logs | Memory notes |", "|---|---|---|---|---|"]
    order = ("bug", "decision", "method", "lesson")
    for project in sorted(set(by) | set(memos)):
        items, memo = by.get(project, []), sorted(memos.get(project, []))
        logged = sum(1 for r in items if LOG_NOTE_RE.search(r[11].lower()) or r[12] == "log")
        lines = ["# Findings: %s" % project, "", stamp, "",
                 "%d findings, %d of them inside session or crawl logs; %d memory notes." % (len(items), logged, len(memo)), ""]
        if memo:
            lines += ["## Memory notes (%d)" % len(memo), "", "Rules and lessons kept in Claude memory about this project.", ""]
            lines += ["- %s: %s `%s`" % (title, desc[:200], abspath) for title, desc, abspath in memo] + [""]
        for t in order:
            these = [r for r in items if r[1] == t]
            if not these:
                continue
            lines += ["## %ss (%d)" % (t.capitalize(), len(these)), ""]
            for (_, _, anchor, line, date_, symptom, cause, fix, reuse, corrected, abspath, logical, kind) in these:
                tag = " [log]" if LOG_NOTE_RE.search(logical.lower()) or kind == "log" else ""
                lines.append("### %s %s%s" % (date_ or "", re.sub(r"^[-#*\s]+", "", anchor)[:140], tag))
                lines.append("- %s%s" % (md_link(abspath, line), ", corrected later" if corrected else ""))
                for label, v in (("Symptom", symptom), ("Cause", cause), ("Fix", fix), ("Reuse (suggestion)", reuse)):
                    if v:
                        lines.append("- %s: %s" % (label, v))
                lines.append("")
        name = re.sub(r"[^a-z0-9._-]+", "-", project.lower()) + ".md"
        path = os.path.join(folder, name)
        with open(path + ".tmp", "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.replace(path + ".tmp", path)
        made.append(path)
        index.append("| [%s](%s) | %d | %d | %d | %d |" % (project, name, len(items), sum(1 for r in items if r[1] == "bug"),
                                                         logged, len(memo)))
    with open(os.path.join(folder, "README.md"), "w") as fh:
        fh.write("\n".join(index) + "\n")
    return made


def write_view(name, lines):
    os.makedirs(VIEWS, exist_ok=True)
    path = os.path.join(VIEWS, name)
    with open(path + ".tmp", "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(path + ".tmp", path)
    return path


def views():
    """The six maps plus one findings file per project. Every line points at a real file and line."""
    ensure_fresh()
    db = sqlite3.connect("file:%s?mode=ro" % INDEX, uri=True)
    stamp = "Generated %s by `brain.py views` from index.sqlite; do not edit." % datetime.now().strftime("%Y-%m-%d %H:%M")
    q = ("SELECT e.anchor, e.line, e.date, i.type, i.cause, i.reuse, i.symptom, i.fix, e.corrected, d.abspath, d.project, "
         "d.logical, e.id FROM items i JOIN entries e ON e.id = i.entry JOIN docs d ON d.id = e.doc WHERE i.type IN (%s) "
         "AND e.copy_path IS NULL ORDER BY e.date DESC, d.logical, e.line, i.item")
    def domains_of(eid):
        return [v for (v,) in db.execute("SELECT value FROM tags WHERE entry=? AND facet='domain'", (eid,))] or ["unsorted"]
    made = []
    for name, types, title in (("bugs.md", "'bug'", "Bugs: what was seen, why, and the fix"),
                               ("methods.md", "'method','lesson','decision'", "Methods, lessons and decisions to reuse")):
        groups = {}
        for row in db.execute(q % types):
            for d in domains_of(row[12])[:1]:
                groups.setdefault(d, []).append(row)
        lines = ["# " + title, "", stamp, "", "Sections come from the Sonnet reading pass; every quote was checked "
                 "against its section. `reuse` is the model's suggestion, written from the quoted fix and cause.", ""]
        total = 0
        for d in sorted(groups):
            lines += ["## " + d, ""]
            for (anchor, line, date_, etype, cause, reuse, symptom, fix, corrected, abspath, project, logical, eid) in groups[d]:
                total += 1
                lines.append("### %s %s" % (date_ or "", re.sub(r"^#+\s*", "", anchor)[:140]))
                lines.append("- %s, %s, %s%s" % (project, etype, md_link(abspath, line), ", corrected later" if corrected else ""))
                if symptom:
                    lines.append("- Symptom: %s" % symptom)
                if cause:
                    lines.append("- Cause: %s" % cause)
                if fix:
                    lines.append("- Fix: %s" % fix)
                if reuse:
                    lines.append("- Reuse (suggestion): %s" % reuse)
                lines.append("")
        lines.insert(4, "%d entries in %d domains." % (total, len(groups)))
        made.append(write_view(name, lines))
    lines = ["# Timeline", "", stamp, "", "Every dated line and dated heading across the notes, newest first.", ""]
    month = None
    for anchor, line, date_, abspath, project in db.execute(
            "SELECT e.anchor, e.line, e.date, d.abspath, d.project FROM entries e JOIN docs d ON d.id=e.doc "
            "WHERE e.type='log' OR (e.date IS NOT NULL AND e.anchor != '(top)' AND e.source IN ('note','memory')) "
            "ORDER BY e.date DESC, d.logical, e.line"):
        if not date_:
            continue
        if date_[:7] != month:
            month = date_[:7]
            lines += ["", "## " + month, ""]
        lines.append("- %s [%s] %s %s" % (date_, project, re.sub(r"^[-#*\s]+", "", anchor)[:150], md_link(abspath, line)))
    made.append(write_view("timeline.md", lines))
    lines = ["# By project", "", stamp, ""]
    for project, n_docs in db.execute("SELECT project, count(*) FROM docs WHERE source IN ('note','memory','claude') "
                                      "GROUP BY project ORDER BY count(*) DESC"):
        n_bugs = db.execute("SELECT count(*) FROM items i JOIN entries e ON e.id=i.entry JOIN docs d ON d.id=e.doc "
                            "WHERE d.project=? AND i.type='bug' AND e.copy_path IS NULL", (project,)).fetchone()[0]
        lines += ["## %s" % project, "", "%d notes, %d bug entries." % (n_docs, n_bugs), ""]
        for logical, abspath, title, desc, kind in db.execute(
                "SELECT logical, abspath, title, description, kind FROM docs WHERE project=? AND source IN "
                "('note','memory','claude') ORDER BY logical", (project,)):
            lines.append("- `%s` %s%s%s" % (logical, "(%s) " % kind if kind else "", title[:80],
                                             (": " + desc[:160]) if desc else ""))
        lines.append("")
    made.append(write_view("by-project.md", lines))
    lines = ["# By technology", "", stamp, "", "\"About\" comes from the reading pass; \"mentions\" from the registry "
             "aliases matched in the text.", ""]
    for tech in sorted(load_registry()["tech"]):
        about = db.execute("SELECT DISTINCT d.logical, d.abspath, e.anchor, e.line FROM tags g JOIN entries e ON e.id=g.entry "
                           "JOIN docs d ON d.id=e.doc WHERE g.facet='tech' AND g.value=? ORDER BY d.logical LIMIT 60",
                           (tech,)).fetchall()
        mentions = db.execute("SELECT count(DISTINCT e.doc) FROM tags g JOIN entries e ON e.id=g.entry WHERE "
                              "g.facet='mention-tech' AND g.value=?", (tech,)).fetchone()[0]
        if not about and not mentions:
            continue
        lines += ["## " + tech, "", "%d sections about it; mentioned in %d documents." % (len(about), mentions), ""]
        for logical, abspath, anchor, line in about:
            lines.append("- %s %s" % (re.sub(r"^#+\s*", "", anchor)[:100], md_link(abspath, line)))
        lines.append("")
    made.append(write_view("by-tech.md", lines))
    made += project_findings(db, stamp)
    db.close()
    h = health(with_remote=True)
    made.append(write_view("at-risk.md", at_risk_lines(h, stamp)))
    return made


def health(with_remote=False):
    """Notes and commits that exist in one place only, plus index, catalog and usage state."""
    manifest = json.load(open(os.path.join(STATE, "manifest.json")))["docs"]
    out = {"untracked": [], "modified": [], "norepo": [], "repos": [], "missing_on_remote": None, "memory_notes": 0}
    for logical, d in sorted(manifest.items()):
        if d["source"] == "memory":
            out["memory_notes"] += 1
        elif d["source"] == "note":
            for state_ in d["git"]:
                if state_ in ("untracked", "modified", "norepo"):
                    out[state_].append(logical)
    corpus = Corpus()
    corpus.walk()
    seen = set()
    for key, repo in corpus.repos.items():
        if not isinstance(key, tuple) or repo is None:
            continue
        r = {"repo": repo.walk_top or "(projects root)", "remote": bool((run([GIT, "-C", repo.top_real, "remote"]) or "").strip())}
        if repo.common_real not in seen:
            seen.add(repo.common_real)
            r["local_only_commits"] = int((run([GIT, "-C", repo.top_real, "rev-list", "--branches", "--not", "--remotes",
                                                "--count"]) or "0").strip() or 0)
        head = (run([GIT, "-C", repo.top_real, "symbolic-ref", "-q", "HEAD"]) or "").strip()
        if not head:
            r["detached_commits"] = int((run([GIT, "-C", repo.top_real, "rev-list", "HEAD", "--not", "--remotes",
                                              "--branches", "--count"]) or "0").strip() or 0)
        if not r["remote"] or r.get("local_only_commits") or r.get("detached_commits"):
            out["repos"].append(r)
    if with_remote and REMOTE != "local":
        try:  # the remote keeps the projects folder at the same path under its home
            res = remote("ls -1 %s" % ("~/" + os.path.relpath(PROJECTS, HOME) if PROJECTS.startswith(HOME + os.sep)
                                       else PROJECTS), timeout=20)
            if res.returncode == 0:
                there = set(res.stdout.decode().split())
                tops = sorted({l.split("/")[0] for l in manifest if "/" in l and manifest[l]["source"] == "note"})
                out["missing_on_remote"] = [t for t in tops if t not in there]
        except subprocess.TimeoutExpired:
            pass
    usage = os.path.join(STATE, "usage.jsonl")
    days = {}
    if os.path.exists(usage):
        for line in open(usage):
            try:
                days[json.loads(line)["ts"][:10]] = days.get(json.loads(line)["ts"][:10], 0) + 1
            except (ValueError, KeyError):
                pass
    out["searches_per_day"] = dict(sorted(days.items())[-14:])
    out["index_age_min"] = round((time.time() - os.path.getmtime(INDEX)) / 60, 1) if os.path.exists(INDEX) else None
    catalog = load_catalog()
    out["catalog_docs"] = len(catalog)
    out["deleted_but_cataloged"] = sorted(l for l in catalog if l not in manifest)
    runs_dir = os.path.join(STATE, "classify")
    waiting = [r for r in (sorted(os.listdir(runs_dir)) if os.path.isdir(runs_dir) else [])
               if not os.path.exists(os.path.join(runs_dir, r, "imported"))]
    out["runs_waiting"] = waiting
    out["unclassified_docs"] = sum(1 for l, d in manifest.items() if d["source"] in ("note", "memory", "claude") and l not in catalog)
    last = os.path.join(STATE, "daily-last-success")
    out["daily_last_success"] = open(last).read().strip() if os.path.exists(last) else None
    return out


def at_risk_lines(h, stamp):
    lines = ["# At risk", "", stamp, "", "Notes and commits that exist in one place only. The brain lists them; it never "
             "commits or pushes inside another session's repo.", ""]
    lines += ["## Notes git does not hold", "", "%d untracked, %d modified, %d outside any repo." %
              (len(h["untracked"]), len(h["modified"]), len(h["norepo"])), ""]
    for k in ("untracked", "modified", "norepo"):
        for l in h[k]:
            lines.append("- %s `%s`" % (k, l))
    lines += ["", "## Repos with commits only on this machine, or no remote", ""]
    for r in h["repos"]:
        lines.append("- `%s`: remote %s, local-only commits %s, detached commits %s" % (
            r["repo"], "yes" if r["remote"] else "NONE", r.get("local_only_commits", "-"), r.get("detached_commits", "-")))
    if REMOTE != "local":
        lines += ["", "## Folders not on %s" % REMOTE, ""]
        lines.append("%s unreachable when this was written." % REMOTE if h["missing_on_remote"] is None else
                     (", ".join("`%s`" % x for x in h["missing_on_remote"]) or "none"))
    lines += ["", "## Memory folder", "", "%d notes with no git; %s." % (h["memory_notes"], "the daily archive copy holds them"
                                                                        if ARCHIVE else "no archive_dir is set, so nothing else does")]
    lines += ["", "## Notes the catalog remembers but disk no longer has", "",
              "Restore the text from the archive copies (`<archive_dir>/brain/`) if one was deleted by mistake.", ""]
    lines += ["- `%s`" % l for l in h.get("deleted_but_cataloged", [])] or ["none"]
    return lines


def lint():
    """Errors block the daily commit: a secret shape in the pushed catalog, or a quote missing from its source.
    Warnings are listed: IPv4 addresses and absolute home paths in the pushed catalog."""
    errors, warnings = [], []
    path = os.path.join(CATALOG, "notes.jsonl")
    if os.path.exists(path):
        text = open(path, encoding="utf-8").read()
        for line in text.splitlines():
            row = json.loads(line) if line.strip() else {}
            fields = [row.get("description") or ""] + [str(e.get(k) or "") for e in row.get("entries", [])
                                                       for k in ("anchor", "root_cause", "reuse") + QUOTE_FIELDS]
            for f in fields:
                m = SECRET_RE.search(f)
                if m:
                    errors.append("secret shape in catalog/notes.jsonl row %s near %r" % (row.get("logical"), f[:m.start()][-40:]))
        ips = sorted(set(IPV4_RE.findall(text)) - {"0.0.0.0", "127.0.0.1"})
        if ips:
            warnings.append("%d IPv4 addresses in catalog/notes.jsonl (private repo): %s" % (len(ips), ", ".join(ips[:8])))
        if os.path.expanduser("~/") in text:  # not any home: CI notes quote the CI runner's home
            warnings.append("absolute home paths appear in catalog/notes.jsonl")
    manifest = json.load(open(os.path.join(STATE, "manifest.json")))["docs"]
    stale = 0
    for logical, row in load_catalog().items():
        d = manifest.get(logical)
        if d is None:
            warnings.append("catalog row for a note no longer on disk: %s" % logical)
            continue
        try:
            split = split_entries(read_text(d["abspath"]))
        except OSError:
            continue
        entries = {e["ehash"]: e for e in split}
        scopes = section_scopes({str(e["n"]): {"anchor": e["anchor"], "text": e["text"]} for e in split})
        for e in row.get("entries", []):
            src = entries.get(e["ehash"])
            if src is None:
                stale += 1
                continue
            body = qnorm(clean(scopes[str(src["n"])]))
            for k in QUOTE_FIELDS:
                if e.get(k) and qnorm(e[k]) not in body:
                    errors.append("quote not in its section: %s#%s %s" % (logical, e.get("anchor", "")[:40], k))
    if stale:
        warnings.append("%d catalog entries point at sections that changed since reading (re-read on the next run)" % stale)
    return errors, warnings


def notify(msg):
    if NOTIFY and not os.environ.get("BRAIN_NO_NOTIFY"):
        try:
            subprocess.run([os.path.expanduser(a) for a in NOTIFY] + [msg[:95]], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass


# ---------------------------------------------------------------- daily

def archive_copy(kind, paths, h):
    """One tar per day (daily) or per month (snapshot) under <archive_dir>/brain/, with README.md, SHA256SUMS, and one
    line per folder in the archive's INDEX.md when it has one. Never deletes."""
    import tarfile
    if not ARCHIVE:
        return "skipped: no archive_dir set"
    if not os.path.isdir(ARCHIVE):
        return "skipped: %s not mounted" % ARCHIVE
    month = datetime.now().strftime("%Y-%m")
    folder = os.path.join(ARCHIVE, "brain", "%s-%s" % (kind, month))
    name = ("brain-daily-%s.tar.gz" % datetime.now().strftime("%Y-%m-%d")) if kind == "daily" else \
        ("notes-%s.tar.gz" % datetime.now().strftime("%Y-%m-%d"))
    target = os.path.join(folder, name)
    if os.path.exists(target):
        return "exists: " + target
    os.makedirs(folder, exist_ok=True)
    tmp = target + ".partial"
    with tarfile.open(tmp, "w:gz") as tar:
        for arc, path in paths:
            if os.path.exists(path):
                tar.add(path, arcname=arc)
    digest = hashlib.sha256(open(tmp, "rb").read()).hexdigest()
    with tarfile.open(tmp, "r:gz") as tar:
        members = len(tar.getmembers())
    os.replace(tmp, target)
    with open(os.path.join(folder, "SHA256SUMS"), "a") as fh:
        fh.write("%s  %s\n" % (digest, name))
    readme = os.path.join(folder, "README.md")
    if not os.path.exists(readme):
        what = ("the Claude Code memory folder plus every note git does not hold (untracked, modified, outside any "
                "repo), one tar per day" if kind == "daily" else "every note the brain indexes plus the catalog, once a month")
        with open(readme, "w") as fh:
            fh.write("# brain %s %s\n\nWhat: %s.\nMade by: `brain.py daily`.\nLoad: `tar -xzf <file>`; "
                     "paths inside are logical paths (memory/<note>.md, <repo>/<path>).\nVerify: `shasum -a 256 -c SHA256SUMS`.\n"
                     "Read by: nothing automatic; this is a restore copy.\n" % (kind, month, what))
        index = os.path.join(ARCHIVE, "INDEX.md")
        if os.path.exists(index):
            with open(index, "a") as fh:
                fh.write("| brain | brain/%s-%s/ | %s | %.1f MB first file | %s | Brain |\n" % (
                    kind, month, datetime.now().strftime("%Y-%m-%d"), os.path.getsize(target) / 1e6, what))
    for root, _, files in os.walk(folder):
        for f in files:
            if f.startswith("._"):
                os.remove(os.path.join(root, f))
    return "%s (%d members, sha256 %s)" % (target, members, digest[:12])


def daily(if_due_hours=20, push=True, read_every_hours=6):
    """The scheduled job, triggered hourly. A reading cycle (build, import finished runs, read what changed, rewrite the
    maps) runs every read_every_hours, so findings reach the maps within hours; the full job (backups, eval, lint, push)
    every if_due_hours. Each step logs; a failure notifies (config "notify") and stops before anything is pushed."""
    os.makedirs(STATE, exist_ok=True)
    last_path, read_path = os.path.join(STATE, "daily-last-success"), os.path.join(STATE, "read-last-success")
    age = lambda p: time.time() - os.path.getmtime(p) if os.path.exists(p) else float("inf")
    full = not if_due_hours or age(last_path) >= if_due_hours * 3600
    if not full and age(read_path) < read_every_hours * 3600:
        return 0
    lock = locked("daily", blocking=False)
    if lock is None:
        return 0
    report = {"started": datetime.now().isoformat(timespec="seconds"), "full": full}
    try:
        if full and os.path.exists(last_path) and time.time() - os.path.getmtime(last_path) > 36 * 3600:
            notify("Error: brain-daily: last success %.0f h ago" % ((time.time() - os.path.getmtime(last_path)) / 3600))
        report["build"] = build(force=True)
        runs_dir = os.path.join(STATE, "classify")
        for run_name in sorted(os.listdir(runs_dir)) if os.path.isdir(runs_dir) else []:
            rdir = os.path.join(runs_dir, run_name)
            done_mark = os.path.join(rdir, "imported")
            if os.path.exists(done_mark) or not os.path.exists(os.path.join(rdir, "meta.json")):
                continue
            if json.load(open(os.path.join(rdir, "meta.json"))).get("batches", 0) == 0:
                open(done_mark, "w").write("empty")
                continue
            st = status(run_name)
            restarts_path = os.path.join(rdir, "restarts")
            restarts = int(open(restarts_path).read()) if os.path.exists(restarts_path) else 0
            if st.get("finished") or (not st.get("alive") and restarts >= 3):
                if json.load(open(os.path.join(rdir, "meta.json"))).get("kind") == "critic":
                    crit = critic_import(run_name)
                    report.setdefault("critic", {})[run_name] = crit
                    notify("Done: brain %s: %s" % (run_name, "over the 10%% gate: " + ", ".join(crit["over_gate"])
                                                    if crit["over_gate"] else "every field under the 10% gate"))
                    open(done_mark, "w").write(datetime.now().isoformat())
                    continue
                imp = import_run(run_name)
                report.setdefault("imported", {})[run_name] = {k: v for k, v in imp.items() if k != "fields"}
                for alarm in imp["alarms"]:
                    notify("Error: brain import %s: %s" % (run_name, alarm))
                open(done_mark, "w").write(datetime.now().isoformat() + (" partial after %d restarts" % restarts
                                                                         if not st.get("finished") else ""))
            elif not st.get("alive"):
                open(restarts_path, "w").write(str(restarts + 1))
                start(run_name, parallel=2)
                report.setdefault("restarted", []).append(run_name)
        if report.get("imported"):
            build(force=True)
        running = [r for r in (os.listdir(runs_dir) if os.path.isdir(runs_dir) else [])
                   if not os.path.exists(os.path.join(runs_dir, r, "imported")) and not r.startswith("critic-")]
        critic_run = "critic-" + datetime.now().strftime("%Y-%m")
        if not running and load_catalog() and not os.path.exists(os.path.join(runs_dir, critic_run)):  # audit read notes
            report["critic_packed"] = critic_pack(critic_run, int(datetime.now().strftime("%Y%m")))
            start(critic_run, parallel=2)
        if not running:
            name, meta = pack("all", None, "daily-" + datetime.now().strftime("%Y%m%d-%H%M"))
            report["packed"] = meta
            if meta["batches"]:
                start(name, parallel=2)
            else:
                open(os.path.join(runs_dir, name, "imported"), "w").write("empty")
        if not full:
            report["views"] = views()
            open(read_path, "w").write(datetime.now().isoformat(timespec="seconds"))
            return 0
        report["hook"] = hook_report()
        for machine, r in report["hook"].items():
            if r.get("errors_24h"):
                notify("Error: brain hook on %s failed %d times in 24 h: %s" % (machine, r["errors_24h"], r["last_error"][:40]))
        h = health()
        at_risk = [("memory/" + os.path.basename(p), p) for p in
                   sorted(os.path.join(MEMORY, f) for f in os.listdir(MEMORY))] if os.path.isdir(MEMORY) else []
        at_risk += [(l, os.path.join(PROJECTS, l)) for l in h["untracked"] + h["modified"] + h["norepo"]]
        report["archive_daily"] = archive_copy("daily", at_risk, h)
        month_folder = os.path.join(ARCHIVE or "", "brain", "notes-snapshot-" + datetime.now().strftime("%Y-%m"))
        if ARCHIVE and os.path.isdir(ARCHIVE) and not os.path.isdir(month_folder):
            manifest = json.load(open(os.path.join(STATE, "manifest.json")))["docs"]
            everything = [(l, d["abspath"]) for l, d in manifest.items() if d["source"] in ("note", "memory", "claude")]
            everything += [("brain-catalog/" + f, os.path.join(CATALOG, f)) for f in ("notes.jsonl", "local.jsonl")]
            report["archive_snapshot"] = archive_copy("notes-snapshot", everything, h)
        if os.path.exists(QUERIES):
            report["eval"] = evaluate(QUERIES, "score", 5, quiet=True)
            prev = os.path.join(STATE, "eval-last.json")
            last = json.load(open(prev)) if os.path.exists(prev) else None
            if last and report["eval"]["hits"] < last["hits"] - 2:
                notify("Error: brain-daily: retrieval fell from %d to %d of %d" % (last["hits"], report["eval"]["hits"], report["eval"]["n"]))
            json.dump(report["eval"], open(prev, "w"))
        errors, warnings = lint()
        report["lint"] = {"errors": errors[:20], "warnings": warnings[:20]}
        if errors:
            notify("Error: brain-daily: lint found %d errors, nothing pushed" % len(errors))
            return 1
        report["views"] = views()
        if push and PUSH_CATALOG and os.path.isdir(os.path.join(HERE, ".git")):
            stats = [os.path.join("review", f) for f in os.listdir(os.path.join(HERE, "review"))
                     if (f.startswith("classify-") and f.endswith(".json")) or re.fullmatch(r"critic-\d{4}-\d\d\.md", f)] if os.path.isdir(os.path.join(HERE, "review")) else []
            run([GIT, "-C", HERE, "add", "catalog/notes.jsonl", "schema/registry.json"] + stats)
            if run([GIT, "-C", HERE, "diff", "--cached", "--quiet"]) is None:
                run([GIT, "-C", HERE, "commit", "-q", "-m", "Daily catalog update: notes read and quotes checked"])
                report["push"] = "pushed" if run([GIT, "-C", HERE, "push", "-q"], timeout=120) is not None else "push failed"
                if report["push"] == "push failed":
                    notify("Error: brain-daily: git push failed")
        for path in (last_path, read_path):
            with open(path, "w") as fh:
                fh.write(datetime.now().isoformat(timespec="seconds"))
        return 0
    except Exception as exc:
        report["error"] = repr(exc)
        notify("Error: brain-daily: %s" % repr(exc)[:70])
        return 1
    finally:
        report["finished"] = datetime.now().isoformat(timespec="seconds")
        with open(os.path.join(STATE, "daily-report.json"), "w") as fh:
            json.dump(report, fh, indent=1, default=str)
        lock.close()


# ---------------------------------------------------------------- eval

def evaluate(path, split, limit, quiet=False):
    """Hit = an expected note among the top `limit` note hits (commit-subject queries search notes only, so a
    commit cannot find itself). Prints every miss so a reviewer sees what failed, not just the rate."""
    ensure_fresh()
    vocab = Vocab(load_registry())
    db = sqlite3.connect("file:%s?mode=ro" % INDEX, uri=True)
    rows = [json.loads(l) for l in open(path) if l.strip()]
    rows = [r for r in rows if r["split"] == split]
    hits, out = 0, []
    for r in rows:
        kind = "note" if r["kind"] == "commit" else None
        _, found = search(r["query"], kind=kind, limit=limit, per_doc=1, db=db, vocab=vocab)
        got = [h["logical"] for h in found]
        rank = next((i + 1 for i, g in enumerate(got) if g in r["expected"]), None)
        hits += bool(rank)
        out.append("%s %s %-4s %s" % (r["id"], r["kind"][:6], rank or "MISS", r["query"][:90]))
        if not rank:
            out.append("      want %s" % ", ".join(r["expected"]))
            out.append("      got  %s" % ", ".join(got[:5]))
    db.close()
    if quiet:
        return {"split": split, "hits": hits, "n": len(rows), "limit": limit}
    print("\n".join(out))
    print("split %s: %d of %d in the top %d" % (split, hits, len(rows), limit))
    return 0


# ---------------------------------------------------------------- cli

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--args0"]:
        parts = sys.stdin.buffer.read().decode().split("\0")
        os.environ["BRAIN_CALLER_CWD"] = parts[0]
        argv = [a for a in parts[1:] if a != ""]
        if argv[:1] != ["search"]:
            print("brain: a forwarded call runs `search` only; run everything else where the index lives.", file=sys.stderr)
            return 2
    # A second machine that syncs this folder searches through a wrapper that forwards to the machine holding the
    # index (config "index_host": {"ssh": <host>, "not_on": <sys.platform prefix>}); it never builds its own copy.
    copy = bool(os.environ.get("BRAIN_READ_ONLY_COPY")) and argv[:1] == ["search"]  # the synced copy, index host away
    host = setting("index_host", None) or {}
    if host and sys.platform.startswith(host["not_on"]) and "BRAIN_HOME" not in os.environ and not copy:  # tests' own home
        print("brain: run ~/.claude/skills/brain/brain here; it forwards to %s, where the index lives." % host["ssh"],
              file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(prog="brain", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search", help="search notes, commits, scripts")
    s.add_argument("query", nargs="+")
    s.add_argument("--project")
    s.add_argument("--kind", choices=sorted(KIND_FILTERS))
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-refresh", action="store_true")
    sub.add_parser("build", help="scan everything and rebuild index.sqlite")
    sub.add_parser("check", help="lossless split and coverage check")
    sub.add_parser("registry", help="regenerate schema/registry.json")
    c = sub.add_parser("classify", help="pack | start | status | import a reading run")
    c.add_argument("action", choices=["pack", "start", "status", "import"])
    c.add_argument("--wave", default="1", choices=["1", "2", "3", "all"])
    c.add_argument("--limit", type=int)
    c.add_argument("--run")
    c.add_argument("--parallel", type=int, default=2)
    c.add_argument("--wait", action="store_true")
    c.add_argument("--reread", action="store_true", help="read every note again and replace its entries")
    c.add_argument("--only", help="comma-separated logical paths: read just these again and replace their entries")
    cr = sub.add_parser("critic", help="sample | pack | import | score: the monthly quality audit (the daily job runs it)")
    cr.add_argument("action", choices=["sample", "pack", "import", "score"])
    cr.add_argument("--run")
    cr.add_argument("--seed", type=int, default=int(datetime.now().strftime("%Y%m")))
    cr.add_argument("--file", help="score: a verdicts .jsonl written by a critic")
    dp = sub.add_parser("daily", help="the scheduled job: build, import, read changes, lint, views, push, back up")
    dp.add_argument("--if-due", type=float, default=20, help="skip if the last success is newer than this many hours")
    dp.add_argument("--no-push", action="store_true")
    dp.add_argument("--read-every", type=float, default=6, help="run a reading cycle when the last one is older than this many hours")
    sub.add_parser("views", help="write the six maps into views/")
    hp = sub.add_parser("health", help="at-risk notes and commits, index, catalog and usage state")
    hp.add_argument("--remote", action="store_true", help="also list folders the remote does not have")
    sub.add_parser("lint", help="check the catalog: secrets, quotes, IPs")
    e = sub.add_parser("eval", help="score search on a mined test set (review/queries.jsonl)")
    e.add_argument("file", nargs="?", default=QUERIES)
    e.add_argument("--split", default="score")
    e.add_argument("--limit", type=int, default=5)
    args = ap.parse_args(argv)
    if args.cmd == "search":
        q = " ".join(args.query)
        if copy:
            if index_state() == "unusable":
                print("brain: the index host is unreachable and this copy of the index is missing or from other code; "
                      "fall back to rg.", file=sys.stderr)
                return 2
            print("(The index host is unreachable: searched this machine's copy of the index, built %s.)" % datetime.fromtimestamp(
                os.path.getmtime(INDEX)).strftime("%Y-%m-%d %H:%M"), file=sys.stderr)
        elif not args.no_refresh or index_state() == "unusable":
            ensure_fresh()
        t = time.time()
        _, hits = search(q, project=args.project, kind=args.kind, limit=args.limit)
        ms = round((time.time() - t) * 1000, 1)
        if not copy:  # a usage line written on both machines would conflict in the sync
            log_usage(q, args, hits, ms)
        print(json.dumps(hits, ensure_ascii=False, indent=1) if args.json else format_hits(q, hits))
        return 0 if hits else 1
    if args.cmd == "build":
        counts = build(force=True)
        print(json.dumps(counts, indent=1))
        return 0
    if args.cmd == "check":
        if not os.path.exists(INDEX):
            build(force=True)
        res = check()
        print(json.dumps({k: (v if not isinstance(v, list) else {"count": len(v), "first": v[:10]})
                          for k, v in res.items()}, indent=1))
        return 0 if not (res["lossless_mismatches"] or res["missing_from_index"] or res["extra_in_index"]) else 1
    if args.cmd == "classify":
        if args.action == "pack":
            name, meta = pack(args.wave, args.limit, args.run, args.reread, set(args.only.split(",")) if args.only else None)
            print(json.dumps(dict(meta, run=name), indent=1))
        elif args.action == "start":
            return start(args.run, args.parallel, args.wait)
        elif args.action == "status":
            print(json.dumps(status(args.run), indent=1))
        elif args.action == "import":
            st = import_run(args.run)
            print(json.dumps({k: v for k, v in st.items() if k != "fields"}, indent=1))
        return 0
    if args.cmd == "critic":
        if args.action == "sample":
            path = os.path.join(STATE, "critic-sample.jsonl")
            with open(path, "w") as fh:
                for r in critic_sample(args.seed):
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(path)
        elif args.action == "pack":
            print(json.dumps(dict(critic_pack(args.run, args.seed), run=args.run), indent=1))
        elif args.action == "import":
            print(json.dumps(critic_import(args.run), indent=1))
        else:
            cells, over = critic_score([json.loads(l) for l in open(args.file) if l.strip()])
            for (st, fd), c in sorted(cells.items()):
                print("%-9s %-18s right %3d wrong %3d unsupported %3d  bad %4.0f%%" % (st, fd, c["right"], c["wrong"],
                                                                                    c["unsupported"], 100 * c["bad"]))
            print("over the gate:", ", ".join(over) or "nothing")
            return 1 if over else 0
        return 0
    if args.cmd == "daily":
        return daily(args.if_due, push=not args.no_push, read_every_hours=args.read_every)
    if args.cmd == "views":
        for p in views():
            print(p)
        return 0
    if args.cmd == "health":
        h = health(with_remote=args.remote)
        print(json.dumps({k: (v if not isinstance(v, list) or len(v) <= 20 else {"count": len(v), "first": v[:20]})
                          for k, v in h.items()}, indent=1))
        return 0
    if args.cmd == "lint":
        errors, warnings = lint()
        for w in warnings:
            print("WARN  " + w)
        for e in errors:
            print("ERROR " + e)
        print("%d errors, %d warnings" % (len(errors), len(warnings)))
        return 1 if errors else 0
    if args.cmd == "eval":
        return evaluate(args.file, args.split, args.limit)
    if args.cmd == "registry":
        reg = build_registry()
        print("projects %d, tech %d" % (len(reg["projects"]), len(reg["tech"])))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
