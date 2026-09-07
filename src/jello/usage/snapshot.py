"""The Usage HUD's row set, arbitrated from local cache files.

`usage-hud-data` rewritten in Python function by function: the bash+jq original cannot be
ported as text, so every judgment below keeps the reference's name and the reference's
rule, and `tests/golden/usage-show.json` is rendered from the reference itself so the two
are compared rather than trusted.

Reads cache files and codex rollouts only. No network, no Keychain, no subprocess, and no
write of any kind (law L4). Accounts come from `jello.profile.core` (law L2); the only
directory this module lists is one codex home's own `sessions/` tree.

USAGE_HUD_STALE_AFTER (default 900) is the reference's freshness gate, kept by name.
"""

from __future__ import annotations

import glob
import json
import math
import os
import time

from ..profile import core
from . import fetch

STALE_AFTER_DEFAULT = 900
# Two reset epochs name the same window instance. A real window is at least 5h, so this
# slack can only absorb rounding between two caches quoting the same server value.
SEGMENT_SLACK = 120
CODEX_SCAN_MAX = 5
BASE_CACHE = ".usage-cache.json"
FABLE_CACHE = ".usage-cache-fable.json"
API_CACHE = ".usage-api-cache.json"
API_FABLE_CACHE = ".usage-api-cache-fable.json"
CODEX_CACHE = ".usage-hud-api-cache.json"
CLAUDE_CACHE_NAMES = (BASE_CACHE, FABLE_CACHE, API_CACHE, API_FABLE_CACHE)


class SnapshotError(Exception):
    """The snapshot could not be taken at all; `path` is the target that refused."""

    def __init__(self, message, path=None):
        super().__init__(message)
        self.path = path


def stale_after():
    try:
        return int(os.environ.get("USAGE_HUD_STALE_AFTER") or STALE_AFTER_DEFAULT)
    except ValueError:
        return STALE_AFTER_DEFAULT


def is_number(value):
    """jq's `type == "number"`: a JSON boolean is not one, and jq rejects a non-finite
    literal outright rather than reading a window from it."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def alternative(value, default):
    """jq's `//`: only null and false fall through to the alternative."""
    return default if value is None or value is False else value


def to_pct(value):
    """A raw percentage clamped to 0-100 and rounded half away from zero, 0 when unreadable."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number):
        return 0
    number = min(max(number, 0.0), 100.0)
    return math.floor(number + 0.5)


def humanize_until(target, now):
    """The reference's reset text: `Nd Nh`, `Nh Nm`, `Nm`, `now`, or `?`."""
    if target is None:
        return "?"
    difference = int(target) - int(now)
    if difference <= 0:
        return "now"
    days, hours = difference // 86400, (difference % 86400) // 3600
    minutes = (difference % 3600) // 60
    if days > 0:
        return f"{days}d{hours}h"
    if hours > 0:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def window_label(minutes, fallback):
    """window_minutes to the HUD's window key; `fallback` serves rollouts that predate it."""
    try:
        count = int(minutes)
    except (TypeError, ValueError):
        return fallback
    if count == 300:
        return "5h"
    if count == 10080:
        return "7d"
    if count == 0:
        return fallback
    if count >= 1440:
        return f"{count // 1440}d"
    return f"{(count + 59) // 60}h"


# --- one cache file --------------------------------------------------------------------

def cache_window(entry):
    """A window counts only with a numeric percentage AND a numeric reset epoch: a reading
    that cannot be placed in a window instance cannot be compared against another one, and
    there is no telling whether its window already reset."""
    if not isinstance(entry, dict):
        return None
    percentage, reset = entry.get("used_percentage"), entry.get("resets_at")
    if not is_number(percentage) or not is_number(reset):
        return None
    # The fraction is dropped so two caches quoting the same server value compare equal.
    return to_pct(percentage), int(reset)


def stamp(document, keys):
    for key in keys:
        value = document.get(key)
        if value is not None and value is not False:
            return math.floor(value) if is_number(value) else None
    return None


def parse_cache(path):
    """One usage cache with PER-WINDOW validity: a cache can carry a valid five_hour beside
    a null seven_day. `as_of` is when the data last MOVED (what arbitration and asOf need);
    `seen` is when it was last CONFIRMED, the only clock that can answer "is this current?"
    An unreadable or malformed file reads as no readings at all, exactly as the reference's
    jq failures yield empty fields."""
    document = core.read_json(path)
    if not isinstance(document, dict):
        return None
    source = document.get("source")
    return {
        "five": cache_window(document.get("five_hour")),
        "seven": cache_window(document.get("seven_day")),
        "as_of": stamp(document, ("fetched_at", "ts")),
        "seen": stamp(document, ("fetched_at", "activity_at", "ts")),
        "source": "" if source is None or source is False else str(source),
    }


# --- arbitration -----------------------------------------------------------------------

class Pick:
    """The winning reading for one window, and the keys that decided it."""

    def __init__(self):
        self.ok = False
        self.pct = None
        self.as_of = None
        self.seen = None
        self.epoch = None
        self.source = ""
        self.fresh = 0


def number(value):
    return 0 if value is None else value


def same_window(left, right):
    return left is not None and right is not None and abs(left - right) <= SEGMENT_SLACK


def window_beats(pick, pct, seen, as_of, fresh, epoch):
    """Usage only rises until the window resets, so the ranking is monotonic first and
    chronological second: a LATER reset epoch wins outright, because the other reading
    belongs to a spent window; inside one window the HIGHER percentage is the reading
    closest to truth, whichever cache saw it; equal percentages go to the more recently
    CONFIRMED cache, so citing one of two identical readings cannot drag a still-confirmed
    row into `stale`; equal confirmation goes to the newer data, which keeps the API cache
    (considered first) on exact ties."""
    if epoch is None:
        return False
    if pick.epoch is None:
        return True
    if not same_window(epoch, pick.epoch):
        return epoch > pick.epoch
    if number(pct) != number(pick.pct):
        return number(pct) > number(pick.pct)
    if fresh != pick.fresh:
        return fresh > pick.fresh
    if number(seen) != number(pick.seen):
        return number(seen) > number(pick.seen)
    return number(as_of) > number(pick.as_of)


def consider_window(pick, window, reading, now, limit):
    """`pick.seen` follows the winner, so the freshness gate judges the source actually
    used: freshness decides whether the row may be SHOWN, monotonicity decides WHICH
    number is least wrong."""
    if reading is None or window is None:
        return
    pct, epoch = window
    seen, as_of = reading["seen"], reading["as_of"]
    fresh = 1 if seen is not None and now - seen <= limit else 0
    if pick.ok and not window_beats(pick, pct, seen, as_of, fresh, epoch):
        return
    pick.ok = True
    pick.pct, pick.epoch, pick.seen, pick.as_of = pct, epoch, seen, as_of
    pick.source, pick.fresh = reading["source"], fresh


def pick_window(readings, key, now, limit):
    pick = Pick()
    for reading in readings:
        consider_window(pick, reading[key] if reading else None, reading, now, limit)
    return pick


def row_state(has_data, seen, epoch, now, limit):
    """The per-row freshness gate, applied to a RESOLVED row: `ok` when the data was
    confirmed within the limit, `stale` when nothing has confirmed it recently or its
    window has already reset, `missing` for a window this account has no data for. An
    undated row cannot justify its number either, so it gates as stale."""
    if not has_data:
        return "missing"
    if epoch is not None and epoch <= now:
        return "stale"
    if seen is not None and now - seen <= limit:
        return "ok"
    return "stale"


# --- rows ------------------------------------------------------------------------------

def emit_row(label, provider, window, state, pick, active, can_fetch, now):
    """`pct` rides `ok` and `stale` rows so the HUD can retain the last confirmed value;
    consumers read `state`, not the presence of `pct`. `asOf` is when the value last MOVED
    (history dedup keys on it), `seenAt` when it was last CONFIRMED -- never interchangeable.
    A row with no reading carries no reset, timestamp, or source either."""
    row = {"label": label, "provider": provider, "window": window, "state": state}
    if state in ("ok", "stale") and pick.pct is not None:
        row["pct"] = pick.pct
    if pick.ok:
        row["reset"] = humanize_until(pick.epoch, now)
    if pick.as_of is not None:
        row["asOf"] = pick.as_of
    if pick.seen is not None:
        row["seenAt"] = pick.seen
    if active is not None:
        row["active"] = active
    if pick.source:
        row["source"] = pick.source
    if can_fetch is not None:
        row["canFetch"] = can_fetch
    return row


def status_row(label, provider, state, reason, can_fetch):
    """The reason-only row: no window, percentage, or timestamp fields at all."""
    row = {"label": label, "provider": provider, "state": state, "reason": reason}
    if can_fetch is not None:
        row["canFetch"] = can_fetch
    return row


def claude_group(readings, now, limit):
    """One profile's three rows from its four caches:
      5h -- the session limit, SHARED across model families, so all four caches are
            candidates and consider_window reconciles them;
      7d -- weekly all-models, base caches only;
      fb -- Fable weekly, the Fable API cache's seven_day only. The statusline exposes
            account-wide seven_day even during a Fable session, so only the API cache is
            authoritative for this row, while the statusline Fable cache stays a valid
            shared-5h candidate."""
    api, api_fable, base, fable = readings
    return (
        pick_window((api, api_fable, base, fable), "five", now, limit),
        pick_window((api, base), "seven", now, limit),
        pick_window((api_fable,), "seven", now, limit),
    )


def claude_rows(row, label, signed_in, active, can_fetch, now, limit):
    if signed_in is False:
        return [status_row(label, "claude", "logged_out", "logged out", can_fetch)]
    paths = [os.path.join(row["dir"], name)
             for name in (API_CACHE, API_FABLE_CACHE, BASE_CACHE, FABLE_CACHE)]
    readings = [parse_cache(path) for path in paths]
    five, seven, fable = claude_group(readings, now, limit)
    if not (five.ok or seven.ok or fable.ok):
        return [status_row(label, "claude", "offline", "no data", can_fetch)]
    rows = [
        emit_row(label, "claude", "5h", row_state(five.ok, five.seen, five.epoch, now, limit),
                 five, active, can_fetch, now),
        emit_row(label, "claude", "7d", row_state(seven.ok, seven.seen, seven.epoch, now, limit),
                 seven, active, can_fetch, now),
    ]
    # An account with no Fable cache at all has no Fable limit, so that row is absent by
    # design, not missing.
    if fable.ok or os.path.isfile(paths[1]):
        rows.append(emit_row(label, "claude", "fb",
                             row_state(fable.ok, fable.seen, fable.epoch, now, limit),
                             fable, active, can_fetch, now))
    return rows


def rollout_snapshot(sessions):
    """The newest rollout file carrying rate limits, of the five most recently written.
    Scanning further costs time on every poll; scanning fewer can miss the only file that
    carries them."""
    pattern = os.path.join(sessions, "*", "*", "*", "rollout-*.jsonl")
    candidates = []
    for path in glob.glob(pattern):
        try:
            candidates.append((-os.stat(path).st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(candidates)[:CODEX_SCAN_MAX]:
        found = None
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    payload = record.get("payload")
                    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
                    if limits is None:
                        limits = record.get("rate_limits")
                    primary = limits.get("primary") if isinstance(limits, dict) else None
                    if isinstance(primary, dict) and is_number(primary.get("used_percent")):
                        found = limits
        except OSError:
            continue
        if found is not None:
            try:
                return found, math.floor(os.stat(path).st_mtime)
            except OSError:
                return found, 0
    return None, None


def parse_codex_snapshot(limits, ts, source):
    """The API cache and recent rollouts use the same rate-limit shape."""
    primary = limits.get("primary") if isinstance(limits, dict) else None
    if not isinstance(primary, dict):
        return None
    used = alternative(primary.get("used_percent"), None)
    reset = alternative(primary.get("resets_at"), 0)
    if used is None or not is_number(reset):
        return None
    parsed = {
        "w1": window_label(alternative(primary.get("window_minutes"), 0), "5h"),
        "p1": to_pct(used),
        "e1": math.floor(reset),
        "ts": ts,
        "source": source,
        "has2": False,
    }
    secondary = limits.get("secondary")
    used2 = secondary.get("used_percent") if isinstance(secondary, dict) else None
    reset2 = alternative(secondary.get("resets_at"), 0) if isinstance(secondary, dict) else None
    if is_number(used2) and is_number(reset2):
        parsed.update(has2=True, p2=to_pct(used2), e2=math.floor(reset2),
                      w2=window_label(alternative(secondary.get("window_minutes"), 0), "7d"))
    return parsed


def codex_reading(parsed):
    """A codex snapshot as one reading: the fetch time or rollout mtime is both clocks,
    because the value was observed and confirmed at that same moment."""
    return {"seen": parsed["ts"], "as_of": parsed["ts"], "source": parsed["source"]}


def codex_rows(row, label, signed_in, can_fetch, now, limit):
    if signed_in is False:
        return [status_row(label, "codex", "logged_out", "logged out", can_fetch)]
    sessions = os.path.join(row["dir"], "sessions")
    cache_path = os.path.join(row["dir"], CODEX_CACHE)
    document = core.read_json(cache_path)
    api_limits, api_ts = None, None
    if isinstance(document, dict):
        limits = document.get("rate_limits")
        primary = limits.get("primary") if isinstance(limits, dict) else None
        if isinstance(primary, dict) and is_number(primary.get("used_percent")):
            api_limits = limits
            fetched = document.get("fetched_at")
            api_ts = math.floor(fetched) if is_number(fetched) else 0
    rollout_limits, rollout_ts = (None, None)
    if os.path.isdir(sessions):
        rollout_limits, rollout_ts = rollout_snapshot(sessions)

    pick, chosen = Pick(), {}
    for limits, ts, source in ((api_limits, api_ts, "api"), (rollout_limits, rollout_ts, "rollout")):
        if limits is None:
            continue
        parsed = parse_codex_snapshot(limits, ts, source)
        if parsed is None:
            continue
        chosen[source] = parsed
        consider_window(pick, (parsed["p1"], parsed["e1"]), codex_reading(parsed), now, limit)
    if not pick.ok:
        reason = "no data"
        if not os.path.isdir(sessions) and not os.path.isfile(cache_path):
            reason = "not set up"
        return [status_row(label, "codex", "offline", reason, can_fetch)]

    parsed = chosen[pick.source]
    first = Pick()
    first.ok, first.pct, first.epoch = True, parsed["p1"], parsed["e1"]
    first.seen, first.as_of, first.source = pick.seen, pick.as_of, pick.source
    rows = [emit_row(label, "codex", parsed["w1"],
                     row_state(True, pick.seen, parsed["e1"], now, limit),
                     first, None, can_fetch, now)]
    # No `missing` row for an absent second window: Codex retired its 5h limit in 2026-07,
    # so a new rollout carries the weekly window alone. That row is gone upstream.
    if parsed["has2"]:
        second = Pick()
        second.ok, second.pct, second.epoch = True, parsed["p2"], parsed["e2"]
        second.seen, second.as_of, second.source = pick.seen, pick.as_of, pick.source
        rows.append(emit_row(label, "codex", parsed["w2"],
                             row_state(True, pick.seen, parsed["e2"], now, limit),
                             second, None, can_fetch, now))
    return rows


def active_index(rows):
    """The most-recently-used profile comes from statusline activity alone: API fetches
    refresh every profile at once and must not make whichever write finishes last look
    active. -1 when no cache carries a timestamp yet."""
    best, best_stamp = -1, 0
    for index, row in enumerate(rows):
        for name in (BASE_CACHE, FABLE_CACHE):
            document = core.read_json(os.path.join(row["dir"], name))
            if not isinstance(document, dict):
                continue
            value = alternative(alternative(document.get("activity_at"), document.get("ts")), 0)
            if is_number(value) and math.floor(value) > best_stamp:
                best, best_stamp = index, math.floor(value)
    return best


def snapshot_rows(home, now=None):
    """Every account's rows, in census order: claude profiles first, then codex homes.

    `home` names the HOME the census is taken under; discovery itself is core's (law L2),
    which resolves the same HOME, so this argument documents the caller's intent rather
    than re-deriving the layout.
    """
    del home
    if now is None:
        now = time.time()
    root = core.claude_root()
    if os.path.isdir(root) and not os.access(root, os.R_OK | os.X_OK):
        raise SnapshotError("cannot read the claude profile root", root)
    limit = stale_after()
    providers = fetch.capabilities()
    claude_fetch, codex_fetch = "claude" in providers, "codex" in providers

    claude = core.claude_rows(include_identity=False)
    codex = core.codex_rows(include_identity=False)
    active = active_index(claude)
    rows = []
    for index, row in enumerate(claude):
        # No timestamp anywhere: omit the flag entirely so the HUD keeps its
        # include-everything fallback instead of excluding every profile as inactive.
        flag = None if active < 0 else index == active
        rows.extend(claude_rows(row, core.hud_label("claude", row),
                                None, flag, claude_fetch,
                                now, limit))
    for row in codex:
        rows.extend(codex_rows(row, core.hud_label("codex", row), row.get("signed_in"),
                               codex_fetch, now, limit))
    return rows
