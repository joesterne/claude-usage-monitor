#!/usr/bin/env python3
"""
Claude Code hook -> Claude Usage Monitor.

Claude Code runs this script on hook events (see README). It receives the hook
JSON on stdin, which includes `transcript_path`. The script reads any new lines
in that transcript, pulls out the `usage` block from each assistant message
(input_tokens, output_tokens, cache_creation_input_tokens,
cache_read_input_tokens) plus the model and message id, and POSTs them to the
local monitor.

It remembers how far it has read in each transcript (in
~/.claude-usage-monitor/offsets.json), so every run only sends what is new.
It never blocks Claude Code: on any error it exits 0 quietly.

Env vars:
  USAGE_MONITOR_URL   default http://127.0.0.1:8765/usage
"""

import json
import os
import sys
import urllib.request

URL = os.environ.get("USAGE_MONITOR_URL", "http://127.0.0.1:8765/usage")
STATE_DIR = os.path.join(os.path.expanduser("~"), ".claude-usage-monitor")
STATE_FILE = os.path.join(STATE_DIR, "offsets.json")


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def read_new_events(path, offset, session_id):
    events = []
    size = os.path.getsize(path)
    if offset > size:          # transcript was rewritten; start over
        offset = 0
    with open(path, "rb") as f:
        f.seek(offset)
        for raw in f:
            if not raw.endswith(b"\n"):   # partial line still being written
                break
            offset += len(raw)
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            msg = entry.get("message") if isinstance(entry, dict) else None
            if not isinstance(msg, dict) or entry.get("type") != "assistant":
                continue
            usage = msg.get("usage")
            model = msg.get("model")
            if not isinstance(usage, dict) or not model or model == "<synthetic>":
                continue
            events.append({
                "source": "claude-code",
                "model": model,
                "message_id": msg.get("id"),
                "session_id": entry.get("sessionId") or session_id,
                "timestamp": entry.get("timestamp"),
                "usage": {k: usage.get(k, 0) for k in (
                    "input_tokens", "output_tokens",
                    "cache_creation_input_tokens", "cache_read_input_tokens")},
            })
    return events, offset


def post(batch, url=None):
    # If the monitor isn't running this raises, offsets aren't saved, and the
    # same lines are sent next time.
    for i in range(0, len(batch), 500):
        req = urllib.request.Request(url or URL, data=json.dumps(batch[i:i + 500]).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()


def sync(paths, session_id=None, url=None):
    """Send unseen usage from the given transcripts. Returns events sent."""
    state = load_state()
    total = 0
    for p in paths:
        if not p or not os.path.exists(p):
            continue
        evs, new_off = read_new_events(p, state.get(p, 0), session_id)
        if evs:
            post(evs, url)
            total += len(evs)
        state[p] = new_off
        save_state(state)
    return total


def backfill(url=None, projects_dir=None):
    """Catch up on every Claude Code transcript on this machine, e.g. sessions
    that ran while the monitor was closed. Safe to repeat: offsets skip what was
    already sent and the server de-duplicates by message id."""
    root = projects_dir or os.path.join(os.path.expanduser("~"), ".claude", "projects")
    paths = []
    for dirpath, _, files in os.walk(root):
        paths += [os.path.join(dirpath, f) for f in files if f.endswith(".jsonl")]
    return sync(sorted(paths), url=url)


def main(stdin=None):
    try:
        hook = json.load(stdin or sys.stdin)
    except Exception:
        return
    sync([hook.get("transcript_path"), hook.get("agent_transcript_path")], hook.get("session_id"))


def run_safely(stdin=None):
    """Entry point that never fails, so Claude Code is never interrupted."""
    try:
        main(stdin)
    except Exception:
        pass


if __name__ == "__main__":
    run_safely()
    sys.exit(0)
