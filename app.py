#!/usr/bin/env python3
"""
Claude Usage Monitor - standalone app entry point.

Double-click the built app and it:
  1. starts the local usage server (127.0.0.1:8765),
  2. imports any Claude Code usage it hasn't seen yet (past sessions included),
  3. opens the dashboard in its own window.
Closing the window quits the app.

The same binary is also the Claude Code hook:
  ".../Claude Usage Monitor.app/Contents/MacOS/Claude Usage Monitor" --hook
so Claude Code doesn't need Python installed. The dashboard's
"Connect Claude Code" button writes that into ~/.claude/settings.json for you.

Data lives in ~/Library/Application Support/Claude Usage Monitor/usage.db on
macOS (~/.claude-usage-monitor/usage.db elsewhere).
"""

import os
import sys
import threading
import urllib.request
import webbrowser

import env  # noqa: F401  (loads .env settings into os.environ)
import claude_code_hook
import usage_monitor

APP_NAME = "Claude Usage Monitor"
HOST = os.environ.get("USAGE_MONITOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("USAGE_MONITOR_PORT", usage_monitor.DEFAULT_PORT))
URL = f"http://{HOST}:{PORT}"


def data_dir():
    if sys.platform == "darwin":
        d = os.path.expanduser(f"~/Library/Application Support/{APP_NAME}")
    else:
        d = os.path.expanduser("~/.claude-usage-monitor")
    os.makedirs(d, exist_ok=True)
    return d


def hook_command():
    if getattr(sys, "frozen", False):          # running as the built app
        return f'"{sys.executable}" --hook'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" --hook'


def already_running():
    try:
        with urllib.request.urlopen(URL + "/health", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def open_window():
    """Native window via pywebview if available, else the default browser.
    Returns when the user closes the window (browser fallback: never)."""
    try:
        import webview
    except ImportError:
        webbrowser.open(URL)
        threading.Event().wait()
        return
    webview.create_window(APP_NAME, URL, width=1180, height=900, min_size=(420, 500))
    webview.start()


def main():
    if "--hook" in sys.argv:
        claude_code_hook.run_safely()
        return

    if already_running():                      # second launch: just show it
        open_window()
        return

    db = os.environ.get("USAGE_MONITOR_DB") or os.path.join(data_dir(), "usage.db")
    if not os.path.exists(db):
        # fresh database: forget what the hook already sent elsewhere, so the
        # backfill below imports full history into this one
        try:
            os.remove(claude_code_hook.STATE_FILE)
        except OSError:
            pass
    httpd = usage_monitor.make_server(HOST, PORT, db,
                                      hook_command=hook_command())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def catch_up():
        try:
            n = claude_code_hook.backfill(url=URL + "/usage")
            print(f"Imported {n} usage records from Claude Code transcripts")
        except Exception as ex:
            print("Backfill skipped:", ex)
    threading.Thread(target=catch_up, daemon=True).start()

    try:
        open_window()
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
