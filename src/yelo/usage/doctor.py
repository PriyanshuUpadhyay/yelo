"""`yelo usage doctor`: a read-only health check of the usage pipeline.

`usage-hud-doctor` ported, keeping its five sections and its `PASS: `, `WARN: `, `FAIL: `
line shape. Two things change. The snapshot, the profile list, the credential probe, and
the job state are read in process instead of through four subprocesses, which retires the
reference's cross-check of two enumerations -- one census remains, so there is nothing to
cross-check (board C3). And it exits 1 when it printed any FAIL (R4) where the reference
always exited 0.

It reads; it never writes, fetches, or prints a token (law L4).
"""

from __future__ import annotations

import os
import subprocess
import time

from .. import hud
from ..profile import core
from . import fetch, snapshot

CACHE_NAMES = (snapshot.BASE_CACHE, snapshot.FABLE_CACHE,
               snapshot.API_CACHE, snapshot.API_FABLE_CACHE)
LOG_NAMES = ("yelo-hud.out.log", "yelo-hud.err.log",
             "usage-hud.out.log", "usage-hud.err.log")
TOKEN_MARKERS = ("Bearer ", "accessToken", "eyJ")
# A window age beyond ten years is a clock or a schema fault, not an age.
MAX_AGE_SECONDS = 315360000


class Report:
    """The printed lines, and whether any of them was a FAIL."""

    def __init__(self):
        self.failed = False

    def header(self, text):
        print(f"\n— {text} —")

    def passed(self, text):
        print(f"PASS: {text}")

    def warn(self, text):
        print(f"WARN: {text}")

    def fail(self, text):
        self.failed = True
        print(f"FAIL: {text}")


def log_paths(home):
    return [os.path.join(home, "Library", "Logs", name) for name in LOG_NAMES]


# --- sections --------------------------------------------------------------------------

def check_caches(report, accounts):
    """A bad cache is a WARN, never a FAIL: the four caches back each other up, so one
    unreadable file is degraded, not dead. Whether it actually cost a row is the freshness
    watchdog's verdict."""
    for name, directory, _ in accounts:
        files = windows = 0
        bad = False
        for cache in CACHE_NAMES:
            path = os.path.join(directory, cache)
            if not os.path.isfile(path):
                continue
            files += 1
            document = core.read_json(path)
            if not isinstance(document, dict):
                report.warn(f"{name}/{cache} is not parseable JSON — "
                            "the data feed reads nothing from it")
                bad = True
                continue
            present = [document.get(key) for key in ("five_hour", "seven_day")]
            present = [entry for entry in present if isinstance(entry, dict)]
            windows += len(present)
            broken = sum(1 for entry in present
                         if not snapshot.is_number(entry.get("used_percentage"))
                         or not snapshot.is_number(entry.get("resets_at")))
            if broken:
                report.warn(f"{name}/{cache}: {broken} window(s) with a non-numeric "
                            "used_percentage/resets_at — the data feed drops them silently")
                bad = True
        if files == 0:
            report.warn(f"{name}: no usage cache files — nothing feeds the HUD for this profile")
        elif not bad:
            report.passed(f"{name} caches well-formed ({files} file(s), {windows} window(s))")


def seen_text(row, now):
    stamp = row.get("seenAt")
    if not snapshot.is_number(stamp):
        return "never confirmed"
    age = int(now - stamp)
    if age < 0 or age > MAX_AGE_SECONDS:
        return "seenAt unusable"
    return f"seen {age}s ago" if age < 120 else f"seen {age // 60}m ago"


def check_freshness(report, rows, now):
    """A row the feed still shows but nothing has confirmed recently is the failure mode
    that let a 69%-vs-100% meter sit unnoticed. One quiet window is degraded; every window
    quiet means the writers have stopped."""
    states = [row.get("state") for row in rows]
    for row in rows:
        # No column padding: the labels carry a multibyte separator, so any aligned table
        # is one locale away from being wrong.
        print(f"  row: {row['label']} {row.get('window') or '-'} — "
              f"{row.get('state')}, {seen_text(row, now)}")
    total = len(states)
    stale = states.count("stale")
    current = states.count("ok")
    other = total - stale - current
    if total == 0:
        report.fail("the usage snapshot emitted no rows — nothing feeds the HUD")
    elif stale == total:
        report.fail(f"every usage row is stale ({total} of {total}) — "
                    "the usage pipeline has stopped")
    elif stale > 0:
        report.warn(f"{stale} of {total} row(s) stale — those writers have gone quiet, "
                    "the HUD shows no number for them")
    elif current == 0:
        report.warn(f"no usage row carries data ({other} without data) — "
                    "nothing has been configured or written yet")
    elif other > 0:
        report.passed(f"{current} of {total} row(s) current ({other} without data)")
    else:
        report.passed(f"all {total} row(s) current")

    # Only current and stale rows carry the last confirmed percentage; every other state
    # has no usable value and must keep pct out of every downstream consumer.
    carried = [row for row in rows if row.get("state") not in ("ok", "stale") and "pct" in row]
    if carried:
        report.fail(f"{len(carried)} row(s) without usable data carry a pct — "
                    "the row gate has regressed")
    else:
        report.passed("no row without usable data carries a pct")


def legacy_loaded():
    """The dotfiles job, read but never addressed: before the cutover it is what actually
    draws the HUD, so the yelo job being absent is expected, not a fault."""
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{hud.LEGACY_LABEL}"],
            capture_output=True, text=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def check_liveness(report, home):
    loaded, pid = hud.job_state(home)
    if loaded and pid is not None:
        report.passed(f"{hud.LABEL} running (pid {pid})")
    elif loaded:
        report.fail(f"{hud.LABEL} loaded but not running — "
                    f"check {os.path.join(home, 'Library', 'Logs', 'yelo-hud.err.log')}")
    elif legacy_loaded():
        report.warn(f"{hud.LABEL} not loaded; the dotfiles job {hud.LEGACY_LABEL} still "
                    "draws the HUD — cut over with yelo hud install, then yelo hud start")
    else:
        report.fail(f"{hud.LABEL} not loaded — the usage HUD is not running "
                    "(yelo hud install, then yelo hud start)")
    bundle = hud.binary_path(home)
    plist = hud.plist_path(home)
    for label, path in (("bundle", bundle), ("LaunchAgent", plist)):
        if os.path.isfile(path):
            report.passed(f"HUD {label} present: {path}")
        else:
            report.warn(f"HUD {label} absent: {path} (yelo hud install writes it)")


def check_credentials(report, accounts):
    """Profiles key onto separate Keychain services, so this asks per profile — boolean
    only, the token is never emitted."""
    if not accounts:
        report.warn("credential check skipped — no claude profile found")
        return
    for name, directory, state in fetch.check_credentials():
        if state == "ok":
            report.passed(f"{name}: OAuth token readable from Keychain")
        elif os.path.isfile(os.path.join(directory, ".credentials.json")):
            report.warn(f"{name}: .credentials.json present but no Keychain item — "
                        "yelo usage fetch reads the Keychain only, so this profile never "
                        "refreshes from the API")
        else:
            report.warn(f"{name}: no OAuth credential source — API refresh reports "
                        "fetch-failed (statusline caches still feed the HUD)")


def check_codex(report, rows):
    seen = False
    for row in rows:
        if row.get("provider") != "codex":
            continue
        seen = True
        label = f"{row['label']} {row.get('window') or ''}"
        source = row.get("source") or "unknown source"
        state = row.get("state")
        if state == "ok":
            report.passed(f"{label}: current from {source}")
        elif state == "stale":
            report.warn(f"{label}: stale data from {source}")
        else:
            report.warn(f"{label}: {state}")
    if not seen:
        report.warn("no Codex rows — no profile currently feeds the HUD")


def scan_paths(home, accounts):
    paths = [path for path in log_paths(home) if os.path.isfile(path)]
    for _, directory, _ in accounts:
        paths += [os.path.join(directory, name) for name in CACHE_NAMES
                  if os.path.isfile(os.path.join(directory, name))]
    for row in core.codex_rows():
        path = os.path.join(row["dir"], snapshot.CODEX_CACHE)
        if os.path.isfile(path):
            paths.append(path)
    return paths


def check_leaks(report, home, accounts):
    """fetch is the only piece that handles a bearer token, and what the pipeline writes to
    disk is the HUD's launchd logs and the per-profile caches. Counts only, never contents."""
    paths = scan_paths(home, accounts)
    if not paths:
        report.warn("no pipeline-written file exists to scan — the HUD has never run and "
                    "no cache has been written")
        return
    hits = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        if any(marker in text for marker in TOKEN_MARKERS):
            hits += 1
    if hits:
        report.fail(f"{hits} pipeline-written file(s) hold possible token material — "
                    "LEAK, inspect + rotate")
    else:
        report.passed(f"no token material in {len(paths)} pipeline-written file(s)")


def run(args):
    del args
    home = core.HOME
    now = time.time()
    report = Report()
    accounts = fetch.profiles()
    rows = None

    report.header("usage meter")
    try:
        rows = snapshot.snapshot_rows(home, now)
    except snapshot.SnapshotError as error:
        report.fail(f"the usage snapshot failed: {error} ({error.path}) — "
                    "the HUD polls this exact command")
    check_caches(report, accounts)
    if rows is not None:
        check_freshness(report, rows, now)
    check_liveness(report, home)
    check_credentials(report, accounts)

    report.header("codex usage meter")
    if rows is None:
        report.warn("Codex row check skipped — the usage snapshot is unavailable")
    else:
        check_codex(report, rows)

    report.header("token-leak scan")
    check_leaks(report, home, accounts)
    return 1 if report.failed else 0
