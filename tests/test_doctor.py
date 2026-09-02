"""C18 and law L4: `jello doctor` reports matrix M4's verdict and writes nothing.

The doctor column of M4 is the whole table here, one case per target state. Two properties
are asserted on every case: the HOME tree is byte-identical before and after (doctor may
never repair, create, or touch anything), and the exit code is 1 only when some step is
`missing` -- `owned-by-dotfiles` is the correct answer during the cutover, not a fault.
"""

import json
import os
import stat

import pytest

from jello import setup
from test_setup import SEEDED_SETTINGS, bench, tree_digest  # noqa: F401 - shared fixture


def verdicts(result):
    return {line.split("\t")[0]: line.split("\t")[1]
            for line in result.stdout.splitlines() if "\t" in line}


# --- the states of matrix M4, one arranger each ------------------------------------------

def state_absent(bench):
    return {"shell": "missing", "claude-hooks": "missing", "profiles": "missing"}


def state_installed(bench):
    assert bench.run("setup").returncode == 0
    return {"shell": "ok", "claude-hooks": "ok", "profiles": "ok"}


def state_stale_cache(bench):
    assert bench.run("setup").returncode == 0
    text = bench.cache.read_text().split("\n", 1)[1]
    bench.cache.write_text("# jello 0.0.0\n" + text)
    return {"shell": "missing", "claude-hooks": "ok", "profiles": "ok"}


def state_other_hook_groups_only(bench):
    bench.seed_settings(SEEDED_SETTINGS)
    return {"shell": "missing", "claude-hooks": "missing", "profiles": "missing"}


def state_one_event_wired(bench):
    assert bench.run("setup").returncode == 0
    data = bench.parsed()
    data["hooks"]["UserPromptSubmit"] = []
    bench.seed_settings(data)
    return {"shell": "ok", "claude-hooks": "missing", "profiles": "ok"}


def state_settings_not_json(bench):
    bench.settings.parent.mkdir(parents=True, exist_ok=True)
    bench.settings.write_text("{ not json")
    return {"shell": "missing", "claude-hooks": "missing", "profiles": "missing"}


def state_owned_by_dotfiles(bench):
    hooks = bench.home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "session-profile-map.sh").symlink_to(
        bench.dotfiles / "home" / ".claude" / "hooks" / "session-profile-map.sh"
    )
    data = json.loads(json.dumps(SEEDED_SETTINGS))
    for event in setup.HOOK_EVENTS:
        data["hooks"][event][0]["hooks"].append(
            {"type": "command", "command": "$HOME/.claude/hooks/session-profile-map.sh"}
        )
    bench.seed_settings(data)
    real = bench.dotfiles / "home" / ".claude" / ".profiles"
    real.mkdir(parents=True)
    bench.profiles.symlink_to(real)
    return {"shell": "missing", "claude-hooks": "owned-by-dotfiles",
            "profiles": "owned-by-dotfiles"}


def state_profiles_symlinked_elsewhere(bench):
    elsewhere = bench.home / "elsewhere"
    elsewhere.mkdir()
    bench.profiles.parent.mkdir(parents=True, exist_ok=True)
    bench.profiles.symlink_to(elsewhere)
    return {"shell": "missing", "claude-hooks": "missing", "profiles": "missing"}


def state_profiles_is_a_file(bench):
    bench.profiles.parent.mkdir(parents=True, exist_ok=True)
    bench.profiles.write_text("not a directory\n")
    return {"shell": "missing", "claude-hooks": "missing", "profiles": "missing"}


def state_settings_symlinked_into_dotfiles(bench):
    """Review finding F2: the target itself is the link, not a command inside it."""
    target = bench.dotfiles / "home" / ".claude" / "settings.json"
    target.write_text(json.dumps(SEEDED_SETTINGS, indent=2) + "\n")
    bench.settings.parent.mkdir(parents=True, exist_ok=True)
    bench.settings.symlink_to(target)
    return {"shell": "missing", "claude-hooks": "owned-by-dotfiles", "profiles": "missing"}


def state_settings_symlinked_elsewhere(bench):
    stranger = bench.home / "stranger.json"
    stranger.write_text('{"model": "opus"}\n')
    bench.settings.parent.mkdir(parents=True, exist_ok=True)
    bench.settings.symlink_to(stranger)
    return {"shell": "missing", "claude-hooks": "missing", "profiles": "missing"}


STATES = (
    state_absent, state_installed, state_stale_cache, state_other_hook_groups_only,
    state_one_event_wired, state_settings_not_json, state_owned_by_dotfiles,
    state_profiles_symlinked_elsewhere, state_profiles_is_a_file,
    state_settings_symlinked_into_dotfiles, state_settings_symlinked_elsewhere,
)


@pytest.mark.parametrize("arrange", STATES, ids=[fn.__name__[6:] for fn in STATES])
def test_doctor_matrix(bench, arrange):
    expected = arrange(bench)
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
    assert bench.run("setup").returncode == 0
    before = bench.digest()
    result = bench.run("doctor", "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert [row["step"] for row in report] == list(setup.STEP_NAMES)
    assert all(row["state"] == "ok" for row in report)
    assert bench.digest() == before


def test_doctor_never_creates_the_cache_directory(bench):
    """The shell step's target lives under ~/.cache/jello; a check that created it would
    make the next check say ok without anything having been installed."""
    result = bench.run("doctor")
    assert result.returncode == 1
    assert not (bench.home / ".cache").exists()
