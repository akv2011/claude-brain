#!/usr/bin/env python3
"""Brain classify runner. Runs in tmux on the remote (config "remote"), or here as a background process,
started by `brain.py classify start`.

One isolated `claude -p` call per batch file in <run>/batches/, written to <run>/out/<batch>.json.
Resumable: a batch with an out file is done, so a restart only runs the rest. A usage-limit error pauses
every worker until the reset time the CLI names, then retries the same batch; other errors retry up to
3 times. Progress goes to <run>/progress.json. Usage: runner.py <run-dir> [parallel]
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

RUN = os.path.abspath(sys.argv[1])
PARALLEL = int(sys.argv[2]) if len(sys.argv) > 2 else 2
# The native installer's path first: a non-interactive ssh shell often has no claude on its PATH.
CLAUDE = os.environ.get("BRAIN_CLAUDE") or next((p for p in (os.path.expanduser("~/.local/bin/claude"),
                                                          shutil.which("claude")) if p and os.path.exists(p)), "claude")
MAX_SLEEP = int(os.environ.get("BRAIN_RUNNER_MAX_SLEEP", str(6 * 3600)))
TIMEOUT = int(os.environ.get("BRAIN_RUNNER_TIMEOUT", "1800"))
META = json.load(open(os.path.join(RUN, "meta.json")))
SCHEMA = open(os.path.join(RUN, "schema.json")).read()
OUT = os.path.join(RUN, "out")
ERR = os.path.join(RUN, "err")
LIMIT_RE = re.compile(r"(?i)(session limit|usage limit|rate limit|hit your .*limit|too many requests|overloaded)")
lock = threading.Lock()
state = {"paused_until": 0.0, "attempts": {}, "failed": [], "last_error": None, "calls": 0, "inflight": 0}


def reset_time(msg):
    """'resets 6:50am (Europe/Berlin)' -> epoch seconds of the next such time; else 30 minutes from now."""
    m = re.search(r"resets\s+(\d{1,2})(?::(\d\d))?\s*([ap]m)?(?:\s*\(([^)]+)\))?", msg, re.I)
    if not m:
        return time.time() + 1800
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        hour = hour % 12 + (12 if m.group(3).lower() == "pm" else 0)
    tz = None
    if m.group(4):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(m.group(4).strip())
        except Exception:
            tz = None
    now = datetime.now(tz) if tz else datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp() + 120


def write_progress(pending):
    done = len([f for f in os.listdir(OUT) if f.endswith(".json") and f[:-5] in BATCHES])
    prog = {"total": len(BATCHES), "done": done, "pending": pending, "failed": state["failed"],
            "paused_until": datetime.fromtimestamp(state["paused_until"]).isoformat() if state["paused_until"] > time.time() else None,
            "last_error": state["last_error"], "calls": state["calls"], "updated": datetime.now().isoformat(timespec="seconds")}
    tmp = os.path.join(RUN, "progress.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(prog, fh, indent=1)
    os.replace(tmp, os.path.join(RUN, "progress.json"))


def call(bid):
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL")}
    args = [CLAUDE, "-p", "--safe-mode", "--model", META["model"], "--effort", META["effort"], "--strict-mcp-config",
            "--tools", "", "--no-session-persistence", "--output-format", "json", "--json-schema", SCHEMA]
    with open(os.path.join(RUN, "batches", bid + ".prompt")) as fh:
        try:
            r = subprocess.run(args, stdin=fh, capture_output=True, text=True, cwd=RUN, env=env, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            return None, "timeout after %d s" % TIMEOUT
    try:
        j = json.loads(r.stdout)
    except ValueError:
        return None, "not JSON (exit %d): %s %s" % (r.returncode, r.stdout[:300], r.stderr[:300])
    if j.get("is_error"):
        return None, "is_error %s: %s" % (j.get("api_error_status"), str(j.get("result"))[:400])
    if not isinstance(j.get("structured_output"), dict):
        return None, "no structured_output: %s" % str(j.get("result"))[:300]
    return j, None


def worker(queue):
    while True:
        with lock:
            if not queue:
                if state["inflight"] == 0:
                    return
                bid = None
            else:
                bid = queue.pop(0)
                state["inflight"] += 1
        if bid is None:
            time.sleep(1)
            continue
        try:
            run_one(bid, queue)
        finally:
            with lock:
                state["inflight"] -= 1


def run_one(bid, queue):
    while time.time() < state["paused_until"]:
        time.sleep(min(30, max(1, state["paused_until"] - time.time())))
    if os.path.exists(os.path.join(OUT, bid + ".json")):
        return
    j, err = call(bid)
    with lock:
        state["calls"] += 1
        if j is not None:
            tmp = os.path.join(OUT, bid + ".json.tmp")
            with open(tmp, "w") as fh:
                json.dump(j, fh)
            os.replace(tmp, os.path.join(OUT, bid + ".json"))
        elif LIMIT_RE.search(err) or "429" in err:
            state["paused_until"] = min(reset_time(err), time.time() + MAX_SLEEP)
            state["last_error"] = err
            queue.insert(0, bid)
        else:
            n = state["attempts"].get(bid, 0) + 1
            state["attempts"][bid] = n
            state["last_error"] = err
            with open(os.path.join(ERR, "%s.%d.txt" % (bid, n)), "w") as fh:
                fh.write(err)
            if n < 3:
                queue.append(bid)
            else:
                state["failed"].append(bid)
        write_progress(len(queue))


BATCHES = sorted(f[:-7] for f in os.listdir(os.path.join(RUN, "batches")) if f.endswith(".prompt") and not f.startswith("."))


def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(ERR, exist_ok=True)
    queue = [b for b in BATCHES if not os.path.exists(os.path.join(OUT, b + ".json"))]
    write_progress(len(queue))
    threads = [threading.Thread(target=worker, args=(queue,)) for _ in range(PARALLEL)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    write_progress(0)
    with open(os.path.join(RUN, "finished"), "w") as fh:
        fh.write(datetime.now().isoformat())
    return 1 if state["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
