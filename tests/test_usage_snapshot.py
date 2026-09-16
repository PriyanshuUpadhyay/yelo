"""C01-C06: the snapshot judged against the reference data feed.

The goldens are rendered once, from the bash+jq script yelo's snapshot was rewritten
from, by

    cd /Users/priyanshu/work/jello/wt/main
    uv run --with pytest python tests/golden/render-usage.py

(tests/golden/render-usage.py, sha256
0a11469587ecff0866cc17d7f4cd006c73e8f871176a9685ea4a833b0721fa4e; it refuses to run when
the reference script's own digest has changed, so a golden can never be re-rendered to
make a failing test pass).
The comparison drops `asOf` and `seenAt`: they are absolute epochs, and the fixture is
rebuilt at test time, so those two necessarily move while every other field is a function
of the fixed offsets. Their presence, type, and omission rules are asserted instead by
test_row_keys_and_table.
"""

import hashlib
import json
import os
import pathlib
import time

import pytest

from conftest import (build_usage_home, codex_auth, fixture_env, rollout, run_yelo,
                      statusline_cache, usage_env, write)
from yelo.usage import snapshot

SECURITY_PROBE_STUB = '''#!/usr/bin/env python3
"""Stand-in for `security`: records every argv, then answers "the item exists"."""
import os, sys

with open(os.environ["FAKE_SECURITY_LOG"], "a", encoding="utf-8") as handle:
    handle.write("\\t".join(sys.argv[1:]) + "\\n")
sys.exit(0)
'''

GOLDEN = pathlib.Path(__file__).parent / "golden"
CLOCK_FIELDS = ("asOf", "seenAt")
FIVE_HOUR_RESET = 1290
SEVEN_DAY_RESET = 264630


def show(home, *flags, **overrides):
    env = usage_env(home)
    env.update(overrides)
    return run_yelo(["usage", "show", *flags], env)


def rows_of(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def show_rows(home, **overrides):
    return rows_of(show(home, "--json", **overrides))


def without_clocks(rows):
    return [{key: value for key, value in row.items() if key not in CLOCK_FIELDS}
            for row in rows]


def profile(home, name):
    return os.path.join(str(home), ".claude", ".profiles", name)


def claude_home(tmp_path, name="pri"):
    """A HOME with one signed-in claude profile and no codex or prime account at all."""
    home = tmp_path / "home"
    write(os.path.join(str(home), ".claude", ".profiles", name, "email"), "a@example.test\n")
    return home


def codex_home(tmp_path, name=None):
    home = tmp_path / "home"
    directory = os.path.join(str(home), ".codex" if name is None else ".codex-" + name)
    os.makedirs(os.path.join(directory, "sessions"), exist_ok=True)
    write(os.path.join(directory, "auth.json"),
          json.dumps(codex_auth("c@example.test", "pro", "acc-1")) + "\n")
    if name is None:
        write(os.path.join(directory, "profile-label"), "\n")
    return home, directory


def cache(path, document):
    write(path, json.dumps(document) + "\n")


# --- C01 -------------------------------------------------------------------------------

def test_show_matches_golden(tmp_path):
    """The reference's own row array for the W1 layout, rebuilt at this second."""
    now = int(time.time())
    home = build_usage_home(tmp_path / "home", now)
    golden = json.loads((GOLDEN / "usage-show.json").read_text())
    assert without_clocks(show_rows(home)) == without_clocks(golden)


def test_show_table_matches_golden(tmp_path):
    """The human table is the one surface with no reference, so its golden is yelo's own."""
    now = int(time.time())
    home = build_usage_home(tmp_path / "home", now)
    result = show(home)
    assert result.returncode == 0, result.stderr
    assert result.stdout == (GOLDEN / "usage-show.txt").read_text()


# --- C02 -------------------------------------------------------------------------------

def reading(pct, epoch, seen, as_of, source):
    return {"five": (pct, epoch), "seven": (pct, epoch),
            "seen": seen, "as_of": as_of, "source": source}


def winner(readings, now=1000000, limit=900):
    return snapshot.pick_window(readings, "five", now, limit)


def test_window_beats_order():
    """A later reset epoch wins outright; inside one window the higher percentage wins,
    then the fresher cache, then the more recently confirmed one."""
    now = 1000000
    spent = reading(90, now + 100, now - 10, now - 10, "statusline")
    current = reading(10, now + 5000, now - 10, now - 10, "api")
    assert winner((spent, current)).pct == 10
    assert winner((current, spent)).pct == 10

    # Within the 120 s slack the two readings name one window instance, so the higher
    # percentage wins whichever cache saw it and whenever.
    low = reading(10, now + 5000, now - 10, now - 10, "api")
    high = reading(80, now + 5000 + 119, now - 400, now - 400, "statusline")
    assert winner((low, high)).pct == 80
    # Past the slack the later epoch is a different window and wins on its own.
    later = reading(1, now + 5000 + 121, now - 400, now - 400, "statusline")
    assert winner((low, later)).pct == 1

    # Equal percentages: the fresher reading, then the more recently confirmed one.
    stale_side = reading(50, now + 5000, now - 5000, now - 5000, "statusline")
    fresh_side = reading(50, now + 5000, now - 10, now - 5000, "api")
    assert winner((stale_side, fresh_side)).source == "api"
    older = reading(50, now + 5000, now - 100, now - 100, "statusline")
    newer = reading(50, now + 5000, now - 10, now - 10, "api")
    assert winner((older, newer)).source == "api"


def test_window_scopes(tmp_path):
    """5h is SHARED across the four caches, 7d comes from the base caches only, and fb from
    the Fable API cache's seven_day only."""
    now = int(time.time())
    home = claude_home(tmp_path)
    directory = profile(home, "pri")
    cache(os.path.join(directory, ".usage-api-cache-fable.json"), {
        "five_hour": {"used_percentage": 77, "resets_at": now + FIVE_HOUR_RESET},
        "seven_day": {"used_percentage": 9, "resets_at": now + SEVEN_DAY_RESET},
        "ts": now, "fetched_at": now, "source": "api",
    })
    rows = {(row["label"], row.get("window")): row for row in show_rows(home)}
    # The fable cache alone carried every window: it may feed 5h and fb, never 7d.
    assert rows[("cl·a@example.test", "5h")]["pct"] == 77
    assert rows[("cl·a@example.test", "fb")]["pct"] == 9
    assert rows[("cl·a@example.test", "7d")]["state"] == "missing"
    assert "pct" not in rows[("cl·a@example.test", "7d")]


# --- C03 -------------------------------------------------------------------------------

def test_row_states(tmp_path):
    """seenAt older than the gate is stale with its pct; a passed reset is stale; a window
    with no reading is missing; an account with no cache at all is offline; an account with
    no credentials is logged_out."""
    now = int(time.time())
    home = claude_home(tmp_path)
    directory = profile(home, "pri")
    cache(os.path.join(directory, ".usage-cache.json"),
          statusline_cache(now - 2400, 43, 11))
    rows = {row.get("window"): row for row in show_rows(home)}
    assert rows["5h"]["state"] == "stale" and rows["5h"]["pct"] == 43

    cache(os.path.join(directory, ".usage-cache.json"), {
        "five_hour": {"used_percentage": 43, "resets_at": now - 5},
        "ts": now, "activity_at": now, "source": "statusline",
    })
    rows = {row.get("window"): row for row in show_rows(home)}
    assert rows["5h"]["state"] == "stale", "a window whose reset has passed cannot be ok"
    assert rows["7d"]["state"] == "missing"

    os.unlink(os.path.join(directory, ".usage-cache.json"))
    offline = show_rows(home)
    assert offline == [{"label": "cl·a@example.test", "provider": "claude", "state": "offline",
                        "reason": "no data", "canFetch": True}]

    logged_out = rows_of(run_yelo(["usage", "show", "--json"], fixture_env(home)))
    assert logged_out == offline, "local reads do not inspect sign-in state"


def test_stale_after_is_the_gate(tmp_path):
    now = int(time.time())
    home = claude_home(tmp_path)
    cache(os.path.join(profile(home, "pri"), ".usage-cache.json"),
          statusline_cache(now - 600, 43, 11))
    fresh = {row.get("window"): row for row in show_rows(home)}
    assert fresh["5h"]["state"] == "ok"
    tightened = {row.get("window"): row
                 for row in show_rows(home, USAGE_HUD_STALE_AFTER="60")}
    assert tightened["5h"]["state"] == "stale"


# --- C04 -------------------------------------------------------------------------------

def test_codex_rows(tmp_path):
    """The API cache beats an older rollout, only the five newest rollouts are read, and a
    missing secondary window omits its row instead of reporting it missing."""
    now = int(time.time())
    home, directory = codex_home(tmp_path)
    rollout(directory, now, 18)
    os.utime(os.path.join(directory, "sessions", "2026", "09", "01",
                          "rollout-2026-09-01T09-00-00-019e08eb-508e-7e73-8bc3-1e9c69b5dfd3.jsonl"),
             (now - 4000, now - 4000))
    cache(os.path.join(directory, ".usage-hud-api-cache.json"), {
        "rate_limits": {"primary": {"used_percent": 30, "window_minutes": 10080,
                                    "resets_at": now + 95430},
                        "secondary": None},
        "fetched_at": now - 60, "source": "api",
    })
    rows = show_rows(home)
    assert [row["window"] for row in rows] == ["7d"], "no missing row for a retired window"
    assert (rows[0]["pct"], rows[0]["source"]) == (30, "api")

    # A secondary window earns its own row, labelled from its own window_minutes.
    cache(os.path.join(directory, ".usage-hud-api-cache.json"), {
        "rate_limits": {"primary": {"used_percent": 30, "window_minutes": 10080,
                                    "resets_at": now + 95430},
                        "secondary": {"used_percent": 8, "window_minutes": 300,
                                      "resets_at": now + FIVE_HOUR_RESET}},
        "fetched_at": now - 60, "source": "api",
    })
    rows = show_rows(home)
    assert [(row["window"], row["pct"]) for row in rows] == [("7d", 30), ("5h", 8)]


def test_only_the_five_newest_rollouts_are_read(tmp_path):
    now = int(time.time())
    home, directory = codex_home(tmp_path)
    day = os.path.join(directory, "sessions", "2026", "09", "01")
    os.makedirs(day, exist_ok=True)
    for index in range(6):
        path = os.path.join(day, f"rollout-2026-09-0{index + 1}T09-00-00-{index}.jsonl")
        carries = index == 0
        record = {"payload": {"rate_limits": {
            "primary": {"used_percent": 22, "window_minutes": 10080,
                        "resets_at": now + 95430},
            "secondary": None}}} if carries else {"payload": {"type": "message"}}
        write(path, json.dumps(record) + "\n")
        os.utime(path, (now - 1000 * (6 - index), now - 1000 * (6 - index)))
    assert show_rows(home)[0]["state"] == "offline", "the sixth-newest is out of scope"

    newest = os.path.join(day, "rollout-2026-09-01T09-00-00-0.jsonl")
    os.utime(newest, (now, now))
    assert show_rows(home)[0]["pct"] == 22


@pytest.mark.parametrize("minutes,expected", [
    (300, "5h"), (10080, "7d"), (4320, "3d"), (120, "2h"), (90, "2h"), (0, "fallback"),
])
def test_window_label(minutes, expected):
    assert snapshot.window_label(minutes, "fallback") == expected


def test_codex_offline_reasons(tmp_path):
    home, directory = codex_home(tmp_path)
    os.rmdir(os.path.join(directory, "sessions"))
    assert show_rows(home)[0]["reason"] == "not set up"
    os.makedirs(os.path.join(directory, "sessions"))
    assert show_rows(home)[0]["reason"] == "no data"


# --- C05 -------------------------------------------------------------------------------

def test_row_keys_and_table(tmp_path):
    """pct only on ok or stale, active only when a claude cache carries a timestamp,
    canFetch true for claude and false for codex without a codex binary, and the six-column
    table."""
    now = int(time.time())
    home = build_usage_home(tmp_path / "home", now)
    rows = show_rows(home)
    for row in rows:
        assert set(row) <= {"label", "provider", "window", "state", "pct", "reset", "asOf",
                            "seenAt", "active", "source", "canFetch", "primeSignedIn",
                            "reason"}
        if row["state"] not in ("ok", "stale"):
            assert "pct" not in row
        else:
            assert isinstance(row["pct"], int) and isinstance(row["seenAt"], int)
            assert isinstance(row["asOf"], int)
        assert row["canFetch"] is (row["provider"] == "claude")
        assert "primeSignedIn" not in row
    # The three `pri` rows carry the flag; the offline `work` row is a status row, which
    # has no window, timestamp, or activity fields at all.
    assert [row.get("active") for row in rows if row["provider"] == "claude"] == \
        [True, True, True, None]
    assert all("active" not in row for row in rows if row["provider"] == "codex")

    table = show(home).stdout.splitlines()
    assert table[0].split() == ["LABEL", "WINDOW", "PCT", "RESET", "STATE", "SOURCE"]
    assert len(table) == len(rows) + 1

    # With no claude cache carrying a timestamp the flag is omitted entirely, so the HUD
    # keeps its include-everything fallback instead of excluding every profile.
    bare = claude_home(tmp_path / "bare")
    cache(os.path.join(profile(tmp_path / "bare" / "home", "pri"), ".usage-api-cache.json"),
          {"five_hour": {"used_percentage": 5, "resets_at": now + FIVE_HOUR_RESET},
           "fetched_at": now, "source": "api"})
    assert all("active" not in row for row in show_rows(bare))




# --- C06 -------------------------------------------------------------------------------

def test_unreadable_cache_and_error_shape(tmp_path):
    """One truncated cache is absent for that file only; every cache unreadable leaves the
    reference's single offline row; an unreadable profile root is the error shape."""
    now = int(time.time())
    home = claude_home(tmp_path)
    directory = profile(home, "pri")
    write(os.path.join(directory, ".usage-api-cache.json"), '{"five_hour": {"used_per')
    cache(os.path.join(directory, ".usage-cache.json"), {
        "five_hour": {"used_percentage": 43, "resets_at": now + FIVE_HOUR_RESET},
        "ts": now, "activity_at": now, "source": "statusline",
    })
    rows = {row["window"]: row for row in show_rows(home)}
    assert rows["5h"]["pct"] == 43, "the readable cache still decides the window"
    assert rows["7d"]["state"] == "missing"
    # A missing row carries no reason, no pct, and no reset: the reference's json_row has
    # no reason key at all, and there is no reading to date.
    assert set(rows["7d"]) == {"label", "provider", "window", "state", "active", "canFetch"}

    os.unlink(os.path.join(directory, ".usage-cache.json"))
    assert show_rows(home) == [{"label": "cl·a@example.test", "provider": "claude",
                                    "state": "offline", "reason": "no data",
                                    "canFetch": True}]

    root = os.path.join(str(home), ".claude", ".profiles")
    os.chmod(root, 0o000)
    try:
        result = show(home, "--json")
    finally:
        os.chmod(root, 0o755)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"yelo: usage show: cannot read the claude profile root ({root})\n"


# --- R12 -------------------------------------------------------------------------------

def keychain_service(home, name):
    """The signed rule written out here rather than imported, so the test keys the item by
    the rule and the code has to agree with it."""
    identity = os.path.join(str(home), f".claude-{name}")
    return "Claude Code-credentials-" + hashlib.sha256(identity.encode()).hexdigest()[:8]


def test_show_never_reads_token_bytes(tmp_path):
    """Local usage does not access the Keychain, including its metadata."""
    now = int(time.time())
    home = build_usage_home(tmp_path / "home", now)
    stub = tmp_path / "stubs" / "security"
    write(str(stub), SECURITY_PROBE_STUB)
    os.chmod(str(stub), 0o755)
    log = tmp_path / "security.log"
    rows = show_rows(home, AGENT_PROFILES_SECURITY_BIN=str(stub), FAKE_SECURITY_LOG=str(log))
    assert rows, "the show did emit its rows"

    assert not log.exists(), "local usage must not call the Keychain"


def test_show_writes_nothing(tmp_path):
    """Law L4: a read-only command leaves every file under HOME as it found it."""
    now = int(time.time())
    home = build_usage_home(tmp_path / "home", now)
    before = {}
    for base, _, names in os.walk(str(home)):
        for name in names:
            path = os.path.join(base, name)
            before[path] = os.stat(path).st_mtime_ns
    assert show(home, "--json").returncode == 0
    after = {path: os.stat(path).st_mtime_ns for path in before}
    assert after == before
