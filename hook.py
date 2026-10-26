#!/usr/bin/python3
"""Claude Code hook: hands sessions working under the projects folder what the brain knows, when it helps.

- SessionStart (new, resumed or compacted context) and the first prompt of a session that was already running: one
  note on how to search and where the project's findings file is.
- PostToolUseFailure on Bash, and a prompt with a pasted error: a past finding that matches the error, when a strong
  one exists (rules and their measurement in brain.py, "hints"). Each note is hinted once per session.

Fails open: any problem prints nothing and exits 0, so a session is never blocked. Never rebuilds the index.
State and the hint log are per machine (BRAIN_HOOK_STATE, default ~/.local/state/brain-hook), outside any sync.
"""
import json
import os
import sys
import time

STATE = os.environ.get("BRAIN_HOOK_STATE") or os.path.expanduser("~/.local/state/brain-hook")


def respond(d):
    import brain
    event, sid = d.get("hook_event_name") or "", d.get("session_id") or "no-session"
    cwd = os.path.realpath(d.get("cwd") or "")
    if not (cwd + "/").startswith(os.path.realpath(brain.PROJECTS) + "/"):
        return None
    os.makedirs(STATE, exist_ok=True)
    path = os.path.join(STATE, "%s.json" % "".join(c for c in sid if c.isalnum() or c == "-"))
    try:
        state = json.load(open(path))
    except (OSError, ValueError):
        state = {"intro": False, "hinted": []}
    out = []
    if event == "SessionStart" or (event == "UserPromptSubmit" and not state["intro"]):
        intro = brain.session_intro(cwd)
        if intro:
            out.append(intro)
            state["intro"] = True
    hint = None
    if event == "PostToolUseFailure" and d.get("tool_name") == "Bash":
        hint = brain.error_hint(d.get("error") or "", (d.get("tool_input") or {}).get("command", ""))
    elif event == "UserPromptSubmit":
        hint = brain.error_hint(d.get("prompt") or "")
    if hint and hint["key"] not in state["hinted"]:
        out.append(hint["text"])
        state["hinted"].append(hint["key"])
        with open(os.path.join(STATE, "hints.jsonl"), "a") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": sid, "event": event, "cwd": cwd,
                                 "error": hint["line"], "note": hint["key"]}) + "\n")
    if not out:
        return None
    with open(path + ".tmp", "w") as fh:
        json.dump(state, fh)
    os.replace(path + ".tmp", path)
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": "\n\n".join(out)}}


def main():
    try:
        d = json.load(sys.stdin)
        target = os.environ.get("BRAIN_TEST_TARGET")  # the tests and mutation runs point this at their brain.py
        if target:
            import importlib.util
            spec = importlib.util.spec_from_file_location("brain", target)
            sys.modules["brain"] = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(sys.modules["brain"])
        sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
        out = respond(d)
        if out:
            print(json.dumps(out))
    except Exception as exc:  # a hook must never break or block the session it serves; the daily job reads this log
        try:
            os.makedirs(STATE, exist_ok=True)
            with open(os.path.join(STATE, "errors.log"), "a") as fh:
                fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "error": repr(exc)[:300]}) + "\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
