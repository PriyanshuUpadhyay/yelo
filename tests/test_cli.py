"""CLI lists, resolution, help, version, and usage rows use the Rust binary."""

import glob
import json
import os
import pathlib
import re
import time
import tomllib

import pytest

from conftest import (build_census_home, build_usage_home, fixture_env, repo_root, run_yelo,
                      usage_env)

ROOT = pathlib.Path(__file__).parent.parent
GOLDEN = ROOT / "tests" / "golden"
CLIS = ("claude", "codex")
# The signed label rule (R9), written out here rather than imported, so the census counts
# by the rule instead of by the same function the code under test uses.
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
# Every entry the census layout must leave out, whatever the reason.
CENSUS_EXCLUDED = {"bad name", "-lead", ".hidden", ".session-map", "notes.txt",
                   ".aliases", "", "file"}


@pytest.mark.parametrize("cli", CLIS)
def test_list_matches_golden(yelo, cli):
    """The golden bytes are the contract for this fixture HOME."""
    result = yelo("profile", "list", "--cli", cli)
    assert result.returncode == 0, result.stderr
    assert result.stdout == (GOLDEN / f"list-{cli}.txt").read_text()


def census(home, cli):
    """Law L3 counted straight off the filesystem: one row per layout directory whose
    label passes the rule. The codex `sessions/` marker is not part of discovery — it is
    the usage HUD's marker — and the reference script counts a codex home without it, so
    the census does too."""
    if cli == "claude":
        root = os.path.join(home, ".claude", ".profiles")
        return sum(1 for name in os.listdir(root)
                   if os.path.isdir(os.path.join(root, name)) and NAME_RE.match(name))
    if cli == "codex":
        prefix, root = ".codex-", home
    else:
        prefix, root = "agent-", os.path.join(home, ".prime")
    base = os.path.join(root, ".codex" if cli == "codex" else "agent")
    total = 1 if os.path.isdir(base) else 0
    for directory in glob.glob(os.path.join(root, prefix + "*")):
        name = os.path.basename(directory)[len(prefix):]
        if os.path.isdir(directory) and NAME_RE.match(name):
            total += 1
    return total


@pytest.mark.parametrize("cli", CLIS)
def test_profile_census_matches_layout(tmp_path, cli):
    """L3: no phantom row and no missed home. The layout carries a valid name, an invalid
    one, a hidden dot directory, a plain file, and a codex home with no sessions/ marker."""
    home = build_census_home(tmp_path / "census")
    result = run_yelo(["profile", "list", "--cli", cli, "--json"], fixture_env(home))
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    expected = census(home, cli)
    assert expected > 1, "the layout must hold more than the base home"
    assert len(rows) == expected
    assert [row["name"] for row in rows if row["name"] in CENSUS_EXCLUDED] == []


def test_resolve_exit_codes(yelo):
    home = yelo.home
    exact = yelo("profile", "resolve", "--cli", "claude", "--", "pri")
    assert exact.returncode == 0
    assert exact.stdout == f"pri\t{os.path.join(home, '.claude', '.profiles', 'pri')}\n"

    substring = yelo("profile", "resolve", "--cli", "claude", "--", "wor")
    assert substring.returncode == 0
    assert substring.stdout.split("\t")[0] == "work"

    email = yelo("profile", "resolve", "--cli", "codex", "--", "alt@example.test")
    assert email.returncode == 0
    assert email.stdout.split("\t")[0] == "alt"

    ambiguous = yelo("profile", "resolve", "--cli", "claude", "--", "e")
    assert ambiguous.returncode == 2
    assert ambiguous.stderr.startswith("claude: 'e' matches several profiles\n")
    assert "pri" in ambiguous.stderr and "work" in ambiguous.stderr

    missing = yelo("profile", "resolve", "--cli", "claude", "--", "zzz")
    assert missing.returncode == 1
    assert missing.stderr == "claude: no profile matches 'zzz'\n"
    assert missing.stdout == ""












# Public commands in help order. Vendor launches need none of these at runtime.
GROUPS = ("profile", "usage", "setup", "doctor", "hud")
# What each group offers, and nothing else.
SUBCOMMANDS = {
    "usage": ("show", "fetch", "doctor"),
    "hud": ("install", "assemble", "start", "stop"),
}


def test_help_lists_every_group(yelo):
    result = yelo("--help")
    assert result.returncode == 0
    for group in GROUPS:
        assert group in result.stdout


def test_supported_groups_registered(yelo):
    """C01: the groups are on the root parser, in order, and each one lists exactly the
    subcommands R1 names."""
    root = yelo("--help")
    assert root.returncode == 0
    assert choice_list(root.stdout) == list(GROUPS), root.stdout

    for group, expected in SUBCOMMANDS.items():
        result = yelo(group, "--help")
        assert result.returncode == 0, result.stderr
        listed = subcommands_of(result.stdout)
        assert listed == set(expected), (group, listed)


def choice_list(text):
    """The names argparse prints inside the `{a,b,c}` choice line of a subparser, in the
    order it prints them -- which is the order the groups were registered."""
    match = re.search(r"\{([a-z0-9,\-]+)\}", text)
    assert match, text
    return match.group(1).split(",")


def subcommands_of(text):
    return set(choice_list(text))


def test_help_needs_no_host(tmp_path):
    """C32: `--help` works on a machine with no Herdr at all -- no binary on PATH, no
    HERDR_* in the environment, and no socket. Importing a group may not need a host."""
    env = {"HOME": str(tmp_path), "PATH": str(tmp_path)}
    for argv in (["--help"], ["profile", "--help"], ["usage", "--help"],
                 ["setup", "--help"], ["doctor", "--help"], ["hud", "--help"]):
        result = run_yelo(argv, env)
        assert result.returncode == 0, (argv, result.stderr)
        assert result.stdout


def test_version(yelo):
    """C01: the version R1 names."""
    result = yelo("--version")
    assert result.returncode == 0
    assert result.stdout.strip() == "yelo 0.5.11"


# --- the trailing-argv hook (board A3, chair ruling cli-extras-hook) -----------------------


def test_a_subcommand_without_the_hook_gets_argparses_own_error(tmp_path):
    """Every other subcommand keeps argparse's message and its exit 2, unchanged."""
    env = {"HOME": str(tmp_path), "PATH": str(tmp_path)}
    result = run_yelo(["doctor", "--nope"], env)
    assert result.returncode == 2
    assert "unrecognized arguments: --nope" in result.stderr












def test_list_usage_in_process(tmp_path):
    """C13: the usage column and the picker read the cache files themselves, with no
    usage-hud-data anywhere on PATH and no override environment variable."""
    now = int(time.time())
    home = build_usage_home(tmp_path / "home", now)
    env = usage_env(home)
    env["PATH"] = "/nonexistent"
    listing = run_yelo(["profile", "list", "--cli", "claude", "--usage"], env)
    assert listing.returncode == 0, listing.stderr
    assert "5h 57% left" in listing.stdout, listing.stdout
    assert "no data" in listing.stdout, "the profile with no cache has no usage rows"

    picked = run_yelo(["profile", "pick", "--cli", "claude", "--json"], env)
    assert picked.returncode == 0, picked.stderr
    assert json.loads(picked.stdout)["name"] == "pri", (
        "pri is 21 minutes from wasting 57% of its 5h window; work has a whole one ahead")


def test_version_is_the_package_version(yelo):
    result = yelo("--version")
    assert result.returncode == 0
    version = tomllib.loads((repo_root() / "Cargo.toml").read_text())["package"]["version"]
    assert result.stdout.strip() == f"yelo {version}"
