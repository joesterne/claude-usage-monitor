"""
Tiny .env loader (standard library only).

Settings are read from the first of these that exist, without overriding
variables already set in the real environment:
  1. the path in USAGE_MONITOR_ENV, if set
  2. .env next to these scripts
  3. ~/Library/Application Support/Claude Usage Monitor/.env  (macOS app)
  4. ~/.claude-usage-monitor/.env

Copy .env.example to .env and edit. .env is git-ignored.
"""

import os
import sys

APP_NAME = "Claude Usage Monitor"


def _candidates():
    if os.environ.get("USAGE_MONITOR_ENV"):
        yield os.environ["USAGE_MONITOR_ENV"]
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    yield os.path.join(here, ".env")
    yield os.path.expanduser(f"~/Library/Application Support/{APP_NAME}/.env")
    yield os.path.expanduser("~/.claude-usage-monitor/.env")


def load_env():
    """Load KEY=VALUE lines from the .env files; returns the paths read."""
    loaded = []
    for path in _candidates():
        if not os.path.isfile(path):
            continue
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip().removeprefix("export ").strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                os.environ.setdefault(key, value)
        loaded.append(path)
    return loaded


load_env()
