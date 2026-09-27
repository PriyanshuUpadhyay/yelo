"""The CLI contract of yelo, as cases run against a fixture HOME.

`golden/record-cli.py` runs every case once against the Python yelo and writes
`golden/cli/<case>.json`. `test_cli_golden.py` runs the same cases against the yelo under test
(`YELO_CMD`, default the Python package) and compares. The Rust port has to pass the same
files, so it is judged against the Python yelo rather than against itself.
"""

import hashlib
import json
import os
import shlex
import subprocess
import sys
import time

from conftest import (build_census_home, build_fixture_home, build_usage_home,
                      fixture_env, usage_env)

OWNED_SESSION = "019e08eb-508e-7e73-8bc3-1e9c69b5dfd3"
# Fields that carry the wall clock. The fixtures place every cache at a fixed offset from
# `now`, so everything derived from those offsets stays, and only the raw stamps go.
CLOCK_KEYS = {"asOf", "seenAt"}

LIST = ["profile", "list", "--cli"]
RESOLVE = ["profile", "resolve", "--cli"]
PICK = ["profile", "pick", "--cli"]

# (name, home, argv, writes). `writes` adds a listing of HOME after the run. Left out, each
# for a reason its own phase covers: `usage fetch` (network), `setup` (launchers change on
# purpose), `hud` (launchd and swift), `update` and `release` (git), and `--help` (argparse text).
CASES = [
    ("version", "fixture", ["--version"], False),
    ("list-claude", "fixture", LIST + ["claude"], False),
    ("list-codex", "fixture", LIST + ["codex"], False),
    ("list-claude-json", "fixture", LIST + ["claude", "--json"], False),
    ("list-codex-json", "fixture", LIST + ["codex", "--json"], False),
    ("resolve-exact", "fixture", RESOLVE + ["claude", "--", "pri"], False),
    ("resolve-exact-json", "fixture", RESOLVE + ["claude", "--json", "--", "pri"], False),
    ("resolve-substring", "fixture", RESOLVE + ["claude", "--", "wor"], False),
    ("resolve-email", "fixture", RESOLVE + ["codex", "--", "alt@example.test"], False),
    ("resolve-ambiguous", "fixture", RESOLVE + ["claude", "--", "e"], False),
    ("resolve-missing", "fixture", RESOLVE + ["claude", "--", "zzz"], False),
    ("menu-no-terminal", "fixture", ["profile", "menu", "--cli", "claude"], False),
    ("pick-signed-out", "fixture", PICK + ["claude", "--json"], False),
    ("doctor-json", "fixture", ["doctor", "--json"], False),
    ("census-list-claude", "census", LIST + ["claude"], False),
    ("census-list-codex", "census", LIST + ["codex"], False),
    ("usage-show", "usage", ["usage", "show"], False),
    ("usage-show-json", "usage", ["usage", "show", "--json"], False),
    ("usage-list-claude", "usage", LIST + ["claude", "--usage"], False),
    ("usage-list-codex-json", "usage", LIST + ["codex", "--usage", "--json"], False),
    ("usage-pick-claude", "usage", PICK + ["claude", "--json"], False),
    ("usage-pick-codex", "usage", PICK + ["codex", "--json"], False),
    ("usage-sessions", "usage", ["profile", "sessions", "--cli", "codex", "--all", "--json"], False),
    ("usage-owner", "usage", ["profile", "owner", "--cli", "codex", "--json", OWNED_SESSION], False),
    ("usage-doctor", "usage", ["usage", "doctor"], False),
    ("create-claude", "fixture", ["profile", "create", "--cli", "claude", "--yes", "newone"], True),
    ("create-codex", "fixture", ["profile", "create", "--cli", "codex", "--yes", "newone"], True),
    ("sync-claude", "fixture", ["profile", "sync", "--cli", "claude"], True),
]


def command():
    return shlex.split(os.environ["YELO_CMD"]) if os.environ.get("YELO_CMD") else [
        sys.executable, "-m", "yelo.cli"]


def build(kind, root):
    home = os.path.join(root, "home")
    if kind == "usage":
        return build_usage_home(home, int(time.time())), usage_env
    builder = build_census_home if kind == "census" else build_fixture_home
    return builder(home), fixture_env


def environment(home, make_env):
    # Only the fixture decides: no XDG_* or YELO_* from the caller, and no real binary on PATH.
    env = {key: value for key, value in make_env(home).items()
           if not key.startswith(("XDG_", "YELO_"))}
    env["PATH"] = "/usr/bin:/bin"
    return env


def scrub(text, home):
    return text.replace(os.path.realpath(home), "$HOME").replace(home, "$HOME")


def without_clocks(value):
    if isinstance(value, dict):
        return {key: without_clocks(item) for key, item in value.items() if key not in CLOCK_KEYS}
    if isinstance(value, list):
        return [without_clocks(item) for item in value]
    return value


def listing(home):
    """Every entry under HOME as `kind path mode digest`, contents scrubbed of HOME."""
    lines = []
    for base, directories, files in os.walk(home):
        directories.sort()
        for name in sorted(directories + files):
            path = os.path.join(base, name)
            relative = os.path.relpath(path, home)
            mode = oct(os.lstat(path).st_mode & 0o777)
            if os.path.islink(path):
                lines.append(f"L {relative} {mode} {scrub(os.readlink(path), home)}")
            elif os.path.isdir(path):
                lines.append(f"D {relative} {mode}")
            else:
                content = scrub(open(path, encoding="utf-8", errors="replace").read(), home)
                lines.append(f"F {relative} {mode} {hashlib.sha256(content.encode()).hexdigest()[:16]}")
    return lines


def run(case, root):
    name, kind, argv, writes = case
    home, make_env = build(kind, root)
    result = subprocess.run(command() + argv, capture_output=True, text=True, input="",
                            env=environment(home, make_env), cwd=home)
    stdout = scrub(result.stdout, home)
    try:
        stdout = without_clocks(json.loads(stdout))
    except ValueError:
        pass
    outcome = {"argv": argv, "home": kind, "exit": result.returncode,
               "stdout": stdout, "stderr": scrub(result.stderr, home)}
    if writes:
        outcome["home_after"] = listing(home)
    return outcome
