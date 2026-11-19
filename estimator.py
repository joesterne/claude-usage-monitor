"""
Usage estimator: projects upcoming usage from the pace of the last 7 and last
30 days in the local log.

For each basis it works out a daily rate (requests, tokens, estimated cost)
and projects the next 7 days, the next 30 days, and the rest of this calendar
month. When the log covers less than the full window (a new install), the rate
is taken over the days actually covered, and the result says so.
"""

from datetime import datetime, timedelta, timezone

FIELDS = ("requests", "input_tokens", "output_tokens",
          "cache_creation_input_tokens", "cache_read_input_tokens", "cost_usd")
MIN_DAYS = 1.0   # below this the pace is too noisy to project


def _parse(ts):
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _rows(store, since_utc):
    with store.lock:
        return store.conn.execute(
            """SELECT COALESCE(event_time, received_at) AS t, input_tokens, output_tokens,
                      cache_creation_input_tokens, cache_read_input_tokens, cost_usd
               FROM usage_events WHERE COALESCE(event_time, received_at) >= ?""",
            (since_utc.strftime("%Y-%m-%dT%H:%M:%S"),)).fetchall()


def _first(store):
    with store.lock:
        r = store.conn.execute(
            "SELECT MIN(COALESCE(event_time, received_at)) FROM usage_events").fetchone()
    return _parse(r[0]) if r and r[0] else None


def _sum(rows, since):
    tot = dict.fromkeys(FIELDS, 0)
    for r in rows:
        t = _parse(r["t"])
        if t is None or t < since:
            continue
        tot["requests"] += 1
        for k in FIELDS[1:]:
            tot[k] += r[k] or 0
    return tot


def estimate(store, now=None):
    now = now or datetime.now(timezone.utc)
    local_now = now.astimezone()
    rows = _rows(store, now - timedelta(days=31))
    first = _first(store)
    history_days = (now - first).total_seconds() / 86400 if first else 0

    # Calendar month in local time.
    month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    days_left = (next_month - local_now).total_seconds() / 86400
    month_rows = _rows(store, month_start.astimezone(timezone.utc))
    month_to_date = _sum(month_rows, month_start.astimezone(timezone.utc))

    bases = []
    for window, label in ((7, "Last 7 days"), (30, "Last 30 days")):
        covered = min(float(window), history_days)
        tot = _sum(rows, now - timedelta(days=window))
        enough = covered >= MIN_DAYS
        daily = {k: (tot[k] / covered if enough else None) for k in FIELDS}
        proj = lambda days: {k: (daily[k] * days if enough else None) for k in FIELDS}
        month_proj = {k: (month_to_date[k] + daily[k] * days_left if enough else None) for k in FIELDS}
        bases.append({
            "window_days": window, "label": label, "covered_days": round(covered, 2),
            "enough_history": enough, "totals": tot, "per_day": daily,
            "next_7_days": proj(7), "next_30_days": proj(30), "this_month": month_proj,
        })

    # Daily estimated cost for the last 30 local days, for the small bar chart.
    days = []
    for i in range(29, -1, -1):
        d = (local_now - timedelta(days=i)).date()
        days.append({"date": d.isoformat(), "cost_usd": 0.0, "output_tokens": 0, "requests": 0})
    index = {d["date"]: d for d in days}
    for r in rows:
        t = _parse(r["t"])
        if t is None:
            continue
        d = index.get(t.astimezone().date().isoformat())
        if d:
            d["cost_usd"] += r["cost_usd"] or 0
            d["output_tokens"] += r["output_tokens"] or 0
            d["requests"] += 1

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "history_days": round(history_days, 2),
        "month": {"label": local_now.strftime("%B"), "days_left": round(days_left, 2),
                  "to_date": month_to_date},
        "bases": bases,
        "daily": days,
    }
