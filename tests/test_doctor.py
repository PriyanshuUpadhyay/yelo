"""C19 and law L4: `yelo doctor` reports every verdict and writes nothing.

Two tables live here. The first is one case per setup-step state; the second is matrix M4,
the `usage` and `hud` rows the cutover moves through. Two properties are asserted on every
case of both: the HOME tree is byte-identical before and after (doctor may never repair,
create, or touch anything), and the exit code is 1 only when some row is `missing` --
`owned-by-dotfiles` is the correct answer during the cutover, not a fault.

The HUD rows are read off the filesystem like every other row. Doctor never asks launchd
anything, so no test here needs a fake `launchctl`: whether the job is up right now is a
different question from what is installed.
"""

import json
import os
import plistlib
import stat

import pytest

from conftest import SEEDED_SETTINGS, tree_digest
from yelo import doctor as yelo_doctor, hud, setup

HUD_LABEL = "io.github.priyanshuupadhyay.yelo-hud"
LEGACY_PLIST = "work.foyer.usage-hud.plist"
USAGE_LINKS = ("usage-hud-data", "usage-hud-fetch")


def verdicts(result):
    return {line.split("\t")[0]: line.split("\t")[1]
            for line in result.stdout.splitlines() if "\t" in line}


def install_hud_targets(bench):
    """The two HUD targets in their `ok` shape. The setup-step table below is about the
    setup steps, so the HUD rows are held constant across it; matrix M4 varies them."""
    agents = bench.home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / (HUD_LABEL + ".plist")).write_bytes(plistlib.dumps({"Label": HUD_LABEL}))
    macos = bench.home / "Applications" / "UsageHUD.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (macos / "UsageHUD").write_text("binary\n")


# --- the states of matrix M4, one arranger each ------------------------------------------

def seed_accounts(bench):
    from conftest import build_fixture_home

    build_fixture_home(bench.home)


def state_absent(bench):
    # The selector and shell integration are required even before accounts exist.
    return {"launchers": "missing", "profiles": "missing"}


def state_no_accounts(bench):
    """A HOME with no account at all: there is nothing to write a launcher for, and that is
    a complete answer rather than a fault."""
    assert bench.run("setup").returncode == 0
    return {"launchers": "ok", "profiles": "ok"}


def state_installed(bench):
    seed_accounts(bench)
    assert bench.run("setup").returncode == 0
    return {"launchers": "ok", "profiles": "ok"}


def state_stale_launcher(bench):
    seed_accounts(bench)
    assert bench.run("setup").returncode == 0
    path = bench.launchers / "claude-pri"
    path.write_text(path.read_text().replace(".profiles/pri", ".profiles/pri-old"))
    return {"launchers": "missing", "profiles": "ok"}


def state_host_context_wired(bench):
    """SEEDED_SETTINGS carries agent-host-context.py under SessionStart, which is where that
    hook lives for good, so the `agents` row is answered while nothing else is installed."""
    bench.seed_settings(SEEDED_SETTINGS)
    return {"launchers": "missing", "profiles": "missing"}


def state_settings_not_json(bench):
    bench.settings.parent.mkdir(parents=True, exist_ok=True)
    bench.settings.write_text("{ not json")
    return {"launchers": "missing", "profiles": "missing"}


def state_profiles_owned_by_dotfiles(bench):
    real = bench.dotfiles / "home" / ".claude" / ".profiles"
    real.mkdir(parents=True)
    bench.profiles.parent.mkdir(parents=True, exist_ok=True)
    bench.profiles.symlink_to(real)
    return {"launchers": "missing", "profiles": "owned-by-dotfiles"}


def state_profiles_symlinked_elsewhere(bench):
    elsewhere = bench.home / "elsewhere"
    elsewhere.mkdir()
    bench.profiles.parent.mkdir(parents=True, exist_ok=True)
    bench.profiles.symlink_to(elsewhere)
    return {"launchers": "missing", "profiles": "missing"}


def state_profiles_is_a_file(bench):
    bench.profiles.parent.mkdir(parents=True, exist_ok=True)
    bench.profiles.write_text("not a directory\n")
    return {"launchers": "missing", "profiles": "missing"}


def state_settings_symlinked_into_dotfiles(bench):
    """Review finding F2: the target itself is the link, not a command inside it. It settles
    the `agents` row before a byte is read."""
    target = bench.dotfiles / "home" / ".claude" / "settings.json"
    target.write_text(json.dumps(SEEDED_SETTINGS, indent=2) + "\n")
    bench.settings.parent.mkdir(parents=True, exist_ok=True)
    bench.settings.symlink_to(target)
    return {"launchers": "missing", "profiles": "missing"}


def state_settings_symlinked_elsewhere(bench):
    stranger = bench.home / "stranger.json"
    stranger.write_text('{"model": "opus"}\n')
    bench.settings.parent.mkdir(parents=True, exist_ok=True)
    bench.settings.symlink_to(stranger)
    return {"launchers": "missing", "profiles": "missing"}


STATES = (
    state_absent, state_no_accounts, state_installed, state_stale_launcher,
    state_host_context_wired, state_settings_not_json, state_profiles_owned_by_dotfiles,
    state_profiles_symlinked_elsewhere, state_profiles_is_a_file,
    state_settings_symlinked_into_dotfiles, state_settings_symlinked_elsewhere,
)


# Nothing in the table below installs a Herdr or Prime target, so those three rows are
# `missing` throughout; a state that wires a host-context command overrides `agents`.


@pytest.mark.parametrize("arrange", STATES, ids=[fn.__name__[6:] for fn in STATES])
def test_doctor_matrix(bench, arrange):
    install_hud_targets(bench)
    expected = {**arrange(bench), "profile-mirrors": "ok", "usage": "ok", "hud": "ok"}
    before = bench.digest()

    result = bench.run("doctor")
    assert verdicts(result) == expected
    assert result.returncode == (1 if "missing" in expected.values() else 0)
    # L4: the verdict is read off the filesystem, so the filesystem cannot move.
    assert bench.digest() == before

    # Every row names its target, which is what makes the table actionable.
    for line in result.stdout.splitlines():
        assert len(line.split("\t")) == 3 and line.split("\t")[2]


def test_doctor_json_matches_the_table(bench):
    """M4 column 2 end to end: with every target installed by yelo, every row is ok."""
    assert bench.run("setup").returncode == 0
    install_hud_targets(bench)
    before = bench.digest()
    result = bench.run("doctor", "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert [row["step"] for row in report] == [step.name for step in yelo_doctor.CHECKS]
    assert all(row["state"] == "ok" for row in report), report
    assert bench.digest() == before


def test_doctor_never_creates_the_launcher_directory(bench):
    """The launchers step's target is ~/.local/bin; a check that created it would make the
    next check say ok without anything having been installed."""
    seed_accounts(bench)
    result = bench.run("doctor")
    assert result.returncode == 1
    assert not bench.launchers.exists()


def test_doctor_reports_profile_mirror_drift_without_repairing_it(bench):
    seed_accounts(bench)
    (bench.home / ".claude" / "projects").mkdir()
    assert bench.run("setup").returncode == 0
    drift = bench.profiles / "pri" / "projects"
    drift.unlink()
    drift.mkdir()
    config = bench.profiles / "work" / ".claude.json"
    config.unlink()
    before = bench.digest()

    result = bench.run("doctor")

    assert verdicts(result)["profile-mirrors"] == "missing"
    assert f"drift: {drift}" in result.stdout
    assert f"missing: {config}" in result.stdout
    assert bench.digest() == before


# --- matrix M4: the usage and hud rows through the cutover --------------------------------

def hud_dotfiles(bench):
    """A dotfiles tree under the temporary HOME, holding the two targets the cutover has
    not retired yet: the old `usage-hud-*` commands and the old LaunchAgent."""
    root = bench.home / "dotfiles"
    binaries = root / "home" / ".local" / "bin"
    agents = root / "home" / "Library" / "LaunchAgents"
    binaries.mkdir(parents=True, exist_ok=True)
    agents.mkdir(parents=True, exist_ok=True)
    for name in USAGE_LINKS:
        (binaries / name).write_text("#!/bin/sh\n")
    (agents / LEGACY_PLIST).write_bytes(plistlib.dumps({"Label": "work.foyer.usage-hud"}))
    return root


def link_usage_commands(bench, root):
    binaries = bench.home / ".local" / "bin"
    binaries.mkdir(parents=True, exist_ok=True)
    for name in USAGE_LINKS:
        (binaries / name).symlink_to(root / "home" / ".local" / "bin" / name)


def link_legacy_agent(bench, root):
    agents = bench.home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / LEGACY_PLIST).symlink_to(root / "home" / "Library" / "LaunchAgents" / LEGACY_PLIST)


# (usage links, legacy agent link, yelo plist and bundle) -> (usage, hud), exit code.
M4 = (
    ("before-cutover", True, True, False, ("owned-by-dotfiles", "owned-by-dotfiles"), 0),
    ("both-installed", True, True, True, ("owned-by-dotfiles", "owned-by-dotfiles"), 0),
    ("after-cutover", False, False, True, ("ok", "ok"), 0),
    ("nothing-installed", False, False, False, ("ok", "missing"), 1),
    ("agent-only", False, True, False, ("ok", "owned-by-dotfiles"), 0),
)


@pytest.mark.parametrize("name,links,legacy,yelo_pair,expected,code", M4,
                         ids=[row[0] for row in M4])
def test_doctor_hud_matrix(bench, name, links, legacy, yelo_pair, expected, code):
    """C19 and matrix M4, row by row. The setup steps are installed first so the exit code
    reads off the two HUD rows alone."""
    assert hud.DEFAULT_LABEL == HUD_LABEL, "the label the owner signed"
    root = hud_dotfiles(bench)
    assert bench.run("setup", YELO_DOTFILES_ROOT=str(root)).returncode == 0
    if links:
        link_usage_commands(bench, root)
    if legacy:
        link_legacy_agent(bench, root)
    if yelo_pair:
        install_hud_targets(bench)
    # The Herdr, Prime, and agents rows are held at `ok` so the exit code below reads off
    # the two HUD rows alone, which is what this matrix is about.
    before = bench.digest()

    result = bench.run("doctor", YELO_DOTFILES_ROOT=str(root))
    rows = verdicts(result)

    assert (rows["usage"], rows["hud"]) == expected
    assert result.returncode == code
    # L4: the verdict is read off the filesystem, so the filesystem cannot move -- and the
    # dotfiles tree under this HOME is inside the digest, so doctor cannot touch it either.
    assert bench.digest() == before

    # Every row names the target it judged, which is what makes the table actionable.
    details = {line.split("\t")[0]: line.split("\t")[2] for line in result.stdout.splitlines()}
    assert (str(root) in details["usage"]) == links
    assert (LEGACY_PLIST in details["hud"]) == legacy
    if not legacy:
        assert HUD_LABEL in details["hud"]


def test_doctor_hud_row_never_asks_launchd(bench):
    """The verdict is about what is installed, not about what is running: a HOME with the
    pair present reads `ok` with no launchctl on PATH at all."""
    install_hud_targets(bench)
    result = bench.run("doctor", PATH="")
    assert verdicts(result)["hud"] == "ok"
