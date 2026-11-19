"""
Plan limits for the dashboard: how much of the Claude subscription's usage
windows (the 5-hour session and the weekly limits) are used, the same numbers
Claude Code shows under /usage.

Source, in order:
  1. Claude's own usage endpoint, called with the OAuth sign-in Claude Code
     already stored on this machine (macOS Keychain item "Claude Code-credentials",
     or ~/.claude/.credentials.json). Only that token and this request go to
     api.anthropic.com; nothing from the local log is sent.
  2. Manual budgets from .env (USAGE_MONITOR_LIMIT_5H / USAGE_MONITOR_LIMIT_WEEK,
     in estimated API dollars), measured against the local log.

Set USAGE_MONITOR_LIMITS=manual to never call the endpoint, or off to hide the
limits panel.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
LABELS = {
    "five_hour": "Current session (5 hours)",
    "seven_day": "This week, all models",
    "seven_day_opus": "This week, Opus",
    "seven_day_sonnet": "This week, Sonnet",
    "seven_day_oauth_apps": "This week, apps",
}
_cache = {"at": 0.0, "data": None}
_lock = threading.Lock()


def _mode():
    return (os.environ.get("USAGE_MONITOR_LIMITS") or "auto").strip().lower()


def _ttl():
    try:
        return max(15, int(os.environ.get("USAGE_MONITOR_LIMITS_TTL") or 60))
    except ValueError:
        return 60


def _token_from(blob):
    try:
        d = json.loads(blob)
    except (TypeError, ValueError):
        return None
    o = d.get("claudeAiOauth") if isinstance(d, dict) else None
    return (o or {}).get("accessToken") or None


def oauth_token():
    """Claude Code's stored sign-in token, or None. Never logged."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=20)
            if out.returncode == 0:
                tok = _token_from(out.stdout.strip())
                if tok:
                    return tok
        except Exception:
            pass
    path = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
                        ".credentials.json")
    try:
        with open(path) as f:
            return _token_from(f.read())
    except OSError:
        return None


def parse(data):
    """Turn the endpoint's JSON into a list of bars. Any window that has a
    utilization number is shown, so new windows appear without a code change."""
    bars = []
    if not isinstance(data, dict):
        return bars
    for key, v in data.items():
        if not isinstance(v, dict) or v.get("utilization") is None:
            continue
        try:
            pct = float(v["utilization"])
        except (TypeError, ValueError):
            continue
        bars.append({"key": key, "label": LABELS.get(key) or key.replace("_", " ").capitalize(),
                     "percent": round(pct, 1), "resets_at": v.get("resets_at")})
    order = list(LABELS)
    bars.sort(key=lambda b: order.index(b["key"]) if b["key"] in order else len(order))
    return bars


def _from_claude():
    tok = oauth_token()
    if not tok:
        return {"source": "claude", "limits": [],
                "error": "Couldn't find a Claude Code sign-in on this Mac. Sign in to Claude Code, then refresh."}
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + tok,
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": "claude-usage-monitor",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        msg = ("Your Claude Code sign-in has expired. Run Claude Code once to renew it."
               if e.code in (401, 403) else f"Claude's usage service returned HTTP {e.code}.")
        return {"source": "claude", "limits": [], "error": msg}
    except Exception as e:
        return {"source": "claude", "limits": [], "error": f"Couldn't reach Claude's usage service ({e.__class__.__name__})."}
    bars = parse(data)
    return {"source": "claude", "limits": bars,
            "error": None if bars else "Claude didn't report any usage limits for this account."}


def _budget(name):
    try:
        v = float(os.environ.get(name) or 0)
        return v if v > 0 else None
    except ValueError:
        return None


def _from_manual(store):
    bars = []
    for key, label, hours, env in (("five_hour", "Last 5 hours vs budget", 5, "USAGE_MONITOR_LIMIT_5H"),
                                   ("seven_day", "Last 7 days vs budget", 168, "USAGE_MONITOR_LIMIT_WEEK")):
        budget = _budget(env)
        if budget is None:
            continue
        since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - hours * 3600))
        spent = store.summary(since)["totals"]["cost_usd"] or 0
        bars.append({"key": key, "label": label, "percent": round(100 * spent / budget, 1),
                     "resets_at": None, "detail": f"${spent:.2f} of ${budget:.2f} (est. API prices)"})
    return bars


def get(store, force=False):
    """Limits for the dashboard, cached for USAGE_MONITOR_LIMITS_TTL seconds."""
    mode = _mode()
    if mode == "off":
        return {"source": "off", "limits": [], "error": None}
    with _lock:
        if not force and _cache["data"] and time.time() - _cache["at"] < _ttl():
            out = dict(_cache["data"])
        else:
            out = _from_claude() if mode != "manual" else {"source": "manual", "limits": [], "error": None}
            _cache.update(at=time.time(), data=out)
            out = dict(out)
    if not out["limits"]:
        manual = _from_manual(store)
        if manual:
            out = {"source": "manual", "limits": manual,
                   "error": out.get("error") if mode != "manual" else None}
    out["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_cache["at"] or time.time()))
    return out
