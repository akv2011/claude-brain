#!/usr/bin/python3
"""Stands in for `claude -p --output-format json` at the network edge in the classify tests.

Reads the prompt on stdin and answers per FILE and [E<k>] marker, copying quotes from the section text the
way the real model is told to. STUB_MODE (comma list) adds the failures the runner and import must survive:
badquote, ratelimit-once, garbage-once, wrong-n, dup-n, quoteless-reuse, file-tech, multi-item, critic-wrong."""
import json
import os
import re
import sys

mode = set(filter(None, os.environ.get("STUB_MODE", "").split(",")))
prompt = sys.stdin.read()
calls_file = os.path.join(os.getcwd(), ".stub-calls")
calls = int(open(calls_file).read()) if os.path.exists(calls_file) else 0
open(calls_file, "w").write(str(calls + 1))

if "ratelimit-once" in mode and calls == 0:
    print(json.dumps({"type": "result", "is_error": True, "api_error_status": 429,
                      "result": "You've hit your session limit · resets 6:50am (Europe/Berlin)"}))
    sys.exit(1)
if "garbage-once" in mode and calls == 0:
    print("<html>upstream error</html>")
    sys.exit(1)

if "CRITIC ROWS" in prompt:
    verdicts = []
    for line in prompt.split("CRITIC ROWS", 1)[1].splitlines():
        if line.startswith("{"):
            row = json.loads(line)
            fields = {"tech": "wrong" if ("critic-wrong" in mode and row["stratum"] == "bug") else "right", "type": "right"}
            verdicts.append({"sid": row["sid"], "stratum": row["stratum"], "fields": fields, "note": ""})
    print(json.dumps({"type": "result", "is_error": False, "result": "", "structured_output": {"verdicts": verdicts}}))
    sys.exit(0)

files = []
for m in re.finditer(r"=== FILE (\d+): (\S+).*?===\n(.*?)(?=\n=== FILE \d+:|\Z)", prompt, re.S):
    n, logical, body = int(m.group(1)), m.group(2), m.group(3)
    entries = []
    for em in re.finditer(r"\[E(\d+)\] (.*?)(?=\[E\d+\] |\Z)", body, re.S):
        k, text = int(em.group(1)), em.group(2)
        sym = re.search(r"Symptom: (.+)", text)
        cause = re.search(r"Cause: (.+)", text)
        if sym or cause:
            e = {"e": k, "type": "bug", "tech": ["wrangler"], "domain": ["database"], "projects": ["AlphaSite"],
                 "symptom_quote": sym.group(1).strip() if sym else None,
                 "root_cause_quote": cause.group(1).strip() if cause else None, "root_cause": "stub cause",
                 "fix_quote": None, "reuse": "stub reuse", "corrects_quote": None}
            if "nearquote" in mode and cause:
                e["root_cause_quote"] = cause.group(1).strip().replace("routed", "routes")
            if "empty-entry" in mode:
                e.update(root_cause_quote=None, root_cause=None, fix_quote=None, reuse=None)
            if "reuse-foreign-tech" in mode:
                e["reuse"] = "next time run it under puppeteer instead"
            if "leaky-reuse" in mode:
                e["reuse"] = "rotate it, then send token= abc123def456ghi789jkl again"
            if "quoteless-reuse" in mode:
                e.update(type="method", symptom_quote=None, root_cause_quote=None, root_cause=None)
            if "file-tech" in mode:
                e.update(reuse="redeploy it with wrangler", projects=[])
            if "badquote" in mode:
                e["root_cause_quote"] = "this sentence is not in the note"
            entries.append(e)
            causes = re.findall(r"Cause: (.+)", text)
            if "multi-item" in mode and len(causes) > 1:
                entries[-1:] = [dict(e, item=i, type="bug" if i == 1 else "method", root_cause_quote=c.strip(), root_cause="cause %d" % i, reuse="reuse %d" % i,
                                     symptom_quote=None, tech=[]) for i, c in enumerate(causes, 1)]
    if "parent-attribution" in mode:
        markers = list(re.finditer(r"\[E(\d+)\] (?:\(section path: [^)]*\) )?(#+) [^\n]*\n(.*?)(?=\[E\d+\] |\Z)", body, re.S))
        for i, mm in enumerate(markers[:-1]):
            nxt = markers[i + 1]
            child_cause = re.search(r"Cause: (.+)", nxt.group(3))
            if not mm.group(3).strip() and len(nxt.group(2)) > len(mm.group(2)) and child_cause:
                entries = [x for x in entries if x["e"] != int(nxt.group(1))]
                entries.append({"e": int(mm.group(1)), "type": "bug", "tech": [], "domain": [], "projects": [],
                                "symptom_quote": None, "root_cause_quote": child_cause.group(1).strip(),
                                "root_cause": "child cause", "fix_quote": None, "reuse": "watch for it",
                                "corrects_quote": None})
    files.append({"n": 99 if ("wrong-n" in mode and n == 1) else n, "kind": "bug-writeup" if "bug" in logical else "reference",
                  "use": "avoid", "domain": ["database"], "tech": ["Wrangler", "frobnicator"], "projects": [] if "file-tech" in mode else ["alphasite.com"],
                  "description": "stub description for %s" % logical,
                  "entries": [] if "FACETS ONLY" in m.group(0)[:300] else entries})
if "dup-n" in mode and files:
    files = [files[0], dict(files[0], description="placeholder", entries=[])]
print(json.dumps({"type": "result", "is_error": False, "num_turns": 1, "result": "",
                  "structured_output": {"files": files}, "usage": {"output_tokens": 10}}))
