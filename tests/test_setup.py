"""C15-C17 and laws L2, L5: `jello setup` installs three targets and then changes nothing.

Every test runs the real command against a temporary HOME with `XDG_CACHE_HOME` pinned
inside it, so nothing under the real `$HOME` is read or written (R8), and with
`JELLO_DOTFILES_ROOT` pointed at a fake dotfiles tree, so the ownership verdict is decided
by a symlink the test made rather than by whatever this machine happens to have.
"""

import hashlib
import json
import os
import plistlib
import stat
import subprocess
import sys

import pytest

from jello import setup, shell

ZSHRC_LINE = shell.ZSHRC_LINE
# A settings.json shaped like a live one: other top-level keys, other events, and other
# groups inside the two events setup touches.
SEEDED_SETTINGS = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "model": "opus",
    "permissions": {"allow": ["Bash"]},
    "hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "$HOME/.claude/hooks/bash-guard.sh"}]}],
        "SessionStart": [{"hooks": [
            {"type": "command", "command": "$HOME/.claude/hooks/session-model-cache.sh"},
            {"type": "command", "command": "~/.claude/scripts/agent-host-context.py"}]}],
        "UserPromptSubmit": [{"hooks": [
            {"type": "command", "command": "$HOME/.claude/scripts/context-nudge.sh"}]}],
    },
    "statusLine": {"type": "command", "command": "statusline.sh"},
}


HUD_LABEL = "io.github.priyanshuupadhyay.jello-hud"


def install_hud_targets(home):
    """The two targets `jello doctor` reads for its `hud` row. A HOME without them is
    matrix M4 row 4 -- `hud missing`, exit 1 -- which would drown out what the tests below
    are actually about: the setup steps."""
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / (HUD_LABEL + ".plist")).write_bytes(plistlib.dumps({"Label": HUD_LABEL}))
    macos = home / "Applications" / "UsageHUD.app" / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (macos / "UsageHUD").write_text("binary\n")


def env_for(home, dotfiles, **overrides):
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        "XDG_CACHE_HOME": os.path.join(str(home), ".cache"),
        "JELLO_DOTFILES_ROOT": str(dotfiles),
    }
    env.update(overrides)
    return env


def run_jello(argv, env, stdin=""):
    return subprocess.run(
        [sys.executable, "-m", "jello.cli", *argv],
        capture_output=True, text=True, env=env, input=stdin,
    )


def tree_digest(root):
    """Path, kind, mode, and content of everything under root -- but never an mtime, so a
    pure touch would show up and a re-read would not."""
    digest = hashlib.sha256()
    for base, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        for name in sorted(directories + files):
            path = os.path.join(base, name)
            relative = os.path.relpath(path, root)
            mode = stat.S_IMODE(os.lstat(path).st_mode)
            if os.path.islink(path):
                digest.update(f"L {relative} {mode} {os.readlink(path)}\n".encode())
            elif os.path.isdir(path):
                digest.update(f"D {relative} {mode}\n".encode())
            else:
                digest.update(f"F {relative} {mode} ".encode())
                digest.update(hashlib.sha256(open(path, "rb").read()).hexdigest().encode())
                digest.update(b"\n")
    return digest.hexdigest()


@pytest.fixture
def bench(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    dotfiles = tmp_path / "dotfiles"
    (dotfiles / "home" / ".claude" / "hooks").mkdir(parents=True)
    (dotfiles / "home" / ".claude" / "hooks" / "session-profile-map.sh").write_text("#!/bin/sh\n")

    class Bench:
        def __init__(self):
            self.home = home
            self.dotfiles = dotfiles
            self.settings = home / ".claude" / "settings.json"
            self.profiles = home / ".claude" / ".profiles"
            self.cache = home / ".cache" / "jello" / "shell-init.zsh"

        def run(self, *argv, **overrides):
            return run_jello(list(argv), env_for(home, dotfiles, **overrides))

        def rows(self, result):
            return {line.split("\t")[0]: line.split("\t")[1]
                    for line in result.stdout.splitlines() if "\t" in line}

        def seed_settings(self, data):
            self.settings.parent.mkdir(parents=True, exist_ok=True)
            self.settings.write_text(json.dumps(data, indent=2) + "\n")

        def parsed(self):
            return json.loads(self.settings.read_text())

        def digest(self):
            return tree_digest(home)

    return Bench()


def jello_groups(data, event):
    groups = (data.get("hooks") or {}).get(event) or []
    return [group for group in groups if setup.is_jello_group(group)]


def without_jello(data):
    """The document L2 says must be equal before and after: everything but our own groups."""
    stripped = json.loads(json.dumps(data))
    for event, groups in (stripped.get("hooks") or {}).items():
        stripped["hooks"][event] = [g for g in groups if not setup.is_jello_group(g)]
    return stripped


def test_setup_idempotent(bench):
    """C15 and L5: the first run applies all three steps, the second changes nothing."""
    first = bench.run("setup")
    assert first.returncode == 0, first.stderr
    assert bench.rows(first) == {"shell": "changed", "claude-hooks": "changed",
                                 "profiles": "changed"}
    assert ZSHRC_LINE in first.stdout

    assert bench.cache.read_text().splitlines()[0].startswith("# jello ")
    assert stat.S_IMODE(bench.profiles.stat().st_mode) == 0o700
    # A settings.json that did not exist is created carrying the hooks key and nothing else.
    assert list(bench.parsed()) == ["hooks"]
    assert len(jello_groups(bench.parsed(), "SessionStart")) == 1

    before = bench.digest()
    second = bench.run("setup")
    assert second.returncode == 0, second.stderr
    assert bench.rows(second) == {"shell": "unchanged", "claude-hooks": "unchanged",
                                 "profiles": "unchanged"}
    assert bench.digest() == before

    # D1: setup prints the .zshrc line, it never writes it.
    assert not (bench.home / ".zshrc").exists()
    (bench.home / ".zshrc").write_text(f"# my rc\n{ZSHRC_LINE}\n")
    third = bench.run("setup", "shell")
    assert "~/.zshrc already has" in third.stdout
    assert (bench.home / ".zshrc").read_text() == f"# my rc\n{ZSHRC_LINE}\n"


def test_setup_runs_one_named_step(bench):
    result = bench.run("setup", "profiles")
    assert result.returncode == 0, result.stderr
    assert list(bench.rows(result)) == ["profiles"]
    assert not bench.settings.exists() and not bench.cache.exists()

    unknown = bench.run("setup", "nope")
    assert unknown.returncode == 1
    assert unknown.stderr == "jello: setup: unknown step: nope\n"


def test_settings_merge_preserves(bench):
    """C16 and L2: every other key and group survives, and each event ends with exactly
    one jello group however many runs or stale groups it started with."""
    bench.seed_settings(SEEDED_SETTINGS)
    before = without_jello(bench.parsed())

    for _ in range(2):
        result = bench.run("setup", "claude-hooks")
        assert result.returncode == 0, result.stderr

    after = bench.parsed()
    assert without_jello(after) == before
    assert list(after) == list(SEEDED_SETTINGS), "top-level key order must not move"
    for event in setup.HOOK_EVENTS:
        assert jello_groups(after, event) == [setup.hook_group()]
        # The chair's hook-matcher ruling: no matcher key, like every neighbouring group.
        assert "matcher" not in jello_groups(after, event)[0]
        assert len(after["hooks"][event]) == len(SEEDED_SETTINGS["hooks"][event]) + 1
        assert after["hooks"][event][0] == SEEDED_SETTINGS["hooks"][event][0]

    # A stale jello group is replaced where it stands, and a duplicate is dropped.
    stale = {"matcher": "", "hooks": [{"type": "command", "command": "jello resume old-name"}]}
    data = bench.parsed()
    data["hooks"]["SessionStart"] = [stale, data["hooks"]["SessionStart"][0], dict(stale)]
    bench.seed_settings(data)
    replaced = bench.run("setup", "claude-hooks")
    assert bench.rows(replaced)["claude-hooks"] == "changed"
    after = bench.parsed()
    assert jello_groups(after, "SessionStart") == [setup.hook_group()]
    assert after["hooks"]["SessionStart"][0] == setup.hook_group(), "replaced in place"
    assert len(after["hooks"]["SessionStart"]) == 2

    assert bench.rows(bench.run("setup", "claude-hooks"))["claude-hooks"] == "unchanged"


def test_settings_that_is_not_json_is_refused(bench):
    """M4 row 8: an unparseable settings.json is a refusal, never an overwrite -- writing
    over it would drop the user's whole configuration."""
    bench.settings.parent.mkdir(parents=True, exist_ok=True)
    bench.settings.write_text("{not json,,,")
    result = bench.run("setup", "claude-hooks")
    assert result.returncode == 1
    assert result.stderr.startswith("jello: setup: ")
    assert str(bench.settings) in result.stderr
    assert bench.settings.read_text() == "{not json,,,"

    doctor = bench.run("doctor")
    assert doctor.returncode == 1
    assert "claude-hooks\tmissing" in doctor.stdout


def test_setup_respects_dotfiles_ownership(bench):
    """C17 and M4 rows 6 and 11: a target dotfiles still owns is reported, not replaced."""
    hooks_dir = bench.home / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    predecessor = hooks_dir / "session-profile-map.sh"
    predecessor.symlink_to(bench.dotfiles / "home" / ".claude" / "hooks" / "session-profile-map.sh")
    data = json.loads(json.dumps(SEEDED_SETTINGS))
    for event in setup.HOOK_EVENTS:
        data["hooks"][event][0]["hooks"].append(
            {"type": "command", "command": "$HOME/.claude/hooks/session-profile-map.sh"}
        )
    bench.seed_settings(data)

    real_profiles = bench.dotfiles / "home" / ".claude" / ".profiles"
    real_profiles.mkdir(parents=True)
    bench.profiles.symlink_to(real_profiles)
    # The HUD is jello's here, so the two HUD rows stay out of the exit code below.
    install_hud_targets(bench.home)

    before = bench.digest()
    result = bench.run("setup")
    assert result.returncode == 0, result.stderr
    rows = bench.rows(result)
    assert rows["claude-hooks"] == "owned-by-dotfiles"
    assert rows["profiles"] == "owned-by-dotfiles"
    assert bench.parsed() == data, "an owned settings.json is left byte-identical"
    assert bench.profiles.is_symlink()

    doctor = bench.run("doctor")
    assert doctor.returncode == 0, "owned-by-dotfiles is not a fault"
    assert "claude-hooks\towned-by-dotfiles" in doctor.stdout
    assert "profiles\towned-by-dotfiles" in doctor.stdout
    # What earns the exit 0: nothing is `missing`, so the two HUD rows must read `ok`.
    assert bench.rows(doctor)["usage"] == "ok"
    assert bench.rows(doctor)["hud"] == "ok"

    # Only the shell step, which dotfiles does not own, may have changed anything.
    assert bench.digest() != before
    assert bench.rows(bench.run("setup"))["shell"] == "unchanged"


def test_other_dotfiles_hooks_do_not_own_the_events(bench):
    """Only the predecessor hook makes an event dotfiles-owned. The housekeeping and guard
    hooks stay in dotfiles after the cutover, and every one of them is a stow symlink into it
    on this machine; they must not keep `jello setup claude-hooks` from applying."""
    scripts_dir = bench.home / ".claude" / "scripts"
    scripts_dir.mkdir(parents=True)
    real = bench.dotfiles / "home" / ".claude" / "scripts" / "claude-housekeeping.sh"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("#!/bin/sh\n")
    (scripts_dir / "claude-housekeeping.sh").symlink_to(real)
    data = json.loads(json.dumps(SEEDED_SETTINGS))
    for event in setup.HOOK_EVENTS:
        data["hooks"][event][0]["hooks"].append(
            {"type": "command", "command": "$HOME/.claude/scripts/claude-housekeeping.sh"}
        )
    bench.seed_settings(data)
    install_hud_targets(bench.home)

    result = bench.run("setup", "claude-hooks")
    assert result.returncode == 0, result.stderr
    assert bench.rows(result)["claude-hooks"] == "changed"
    for event in setup.HOOK_EVENTS:
        assert any(setup.is_jello_group(group) for group in bench.parsed()["hooks"][event])


def test_settings_symlinked_into_dotfiles_is_owned(bench):
    """Review finding F2: the settings target itself can be dotfiles', not only the hook
    commands inside it. An atomic write would replace the link with a regular file and
    orphan what it pointed at, so the step reports and stops."""
    target = bench.dotfiles / "home" / ".claude" / "settings.json"
    target.write_text(json.dumps(SEEDED_SETTINGS, indent=2) + "\n")
    before = target.read_text()
    bench.settings.parent.mkdir(parents=True, exist_ok=True)
    bench.settings.symlink_to(target)

    result = bench.run("setup")
    assert result.returncode == 0, result.stderr
    assert bench.rows(result)["claude-hooks"] == "owned-by-dotfiles"
    assert bench.settings.is_symlink(), "an owned symlink must survive the run"
    assert target.read_text() == before, "the dotfiles file itself must not be written"

    doctor = bench.run("doctor")
    assert "claude-hooks\towned-by-dotfiles" in doctor.stdout
    assert str(bench.dotfiles) in doctor.stdout


def test_settings_symlinked_elsewhere_is_an_error(bench):
    """A symlink that is not dotfiles' is nobody's to replace, so setup refuses rather
    than turning it into a regular file."""
    stranger = bench.home / "stranger.json"
    stranger.write_text('{"model": "opus"}\n')
    bench.settings.parent.mkdir(parents=True, exist_ok=True)
    bench.settings.symlink_to(stranger)

    result = bench.run("setup", "claude-hooks")
    assert result.returncode == 1
    assert result.stderr == (
        f"jello: setup: settings.json is a symlink outside ~/dotfiles ({bench.settings})\n"
    )
    assert bench.settings.is_symlink()
    assert stranger.read_text() == '{"model": "opus"}\n'

    doctor = bench.run("doctor")
    assert doctor.returncode == 1
    assert "claude-hooks\tmissing" in doctor.stdout
    assert str(bench.settings) in doctor.stdout


def test_profile_root_symlinked_elsewhere_is_an_error(bench):
    """M4 row 12: a symlink that is not dotfiles' is nobody's to follow."""
    elsewhere = bench.home / "elsewhere"
    elsewhere.mkdir()
    bench.profiles.parent.mkdir(parents=True, exist_ok=True)
    bench.profiles.symlink_to(elsewhere)

    result = bench.run("setup", "profiles")
    assert result.returncode == 1
    assert result.stderr == (
        f"jello: setup: profile root is a symlink outside ~/dotfiles ({bench.profiles})\n"
    )
    assert bench.profiles.is_symlink()
    assert list(elsewhere.iterdir()) == []


def test_setup_json_output(bench):
    result = bench.run("setup", "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert [row["step"] for row in report] == list(setup.STEP_NAMES)
    assert all(row["result"] == "changed" for row in report)
