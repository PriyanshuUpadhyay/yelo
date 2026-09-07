"""C05 and C06: `profile create` reproduces the three shell creators, effect for effect.

Every case runs against a temporary HOME, so the real ~/.claude, ~/.codex*, and ~/.prime
are never read or written.
"""

import os
import pathlib
import stat

from conftest import build_fixture_home, fixture_env, run_jello


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def link_to(path):
    return os.path.realpath(os.readlink(path))


def test_create_each_cli(jello):
    home = pathlib.Path(jello.home)
    # The codex creator links these two from the base home, following a symlinked source.
    (home / ".codex" / "hooks.json").write_text("{}\n")
    real_agents = home / "real-AGENTS.md"
    real_agents.write_text("house rules\n")
    (home / ".codex" / "AGENTS.md").symlink_to(real_agents)
    result = jello("profile", "create", "--cli", "claude", "zed",
                   "--email", "zed@example.test", "--yes")
    assert result.returncode == 0, result.stderr
    # The launcher is written last and named in the line, so `claude-zed` is a command the
    # moment the folder exists (ADR 0005).
    assert result.stdout.splitlines()[0] == (
        "Created profile 'zed'. Sign in with: claude-zed auth login")
    launcher = home / ".local" / "bin" / "claude-zed"
    assert result.stdout.splitlines()[1] == f"Wrote {launcher}."
    assert launcher.read_text().splitlines()[1].endswith("account zed")
    directory = home / ".claude" / ".profiles" / "zed"
    assert mode(directory) == 0o700
    assert (directory / "email").read_text() == "zed@example.test\n"

    result = jello("profile", "create", "--cli", "codex", "zeta", "--yes")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == (
        "Created profile 'zeta'. Sign in with: codex-zeta login")
    assert result.stdout.splitlines()[1] == f"Wrote {home / '.local' / 'bin' / 'codex-zeta'}."
    directory = home / ".codex-zeta"
    assert mode(directory) == 0o700
    assert mode(directory / "sessions") == 0o700
    assert (directory / "config.toml").read_text() == 'model = "gpt-5-codex"\n'
    assert mode(directory / "config.toml") == 0o600
    assert link_to(directory / "hooks.json") == os.path.realpath(home / ".codex" / "hooks.json")
    assert link_to(directory / "AGENTS.md") == os.path.realpath(real_agents)

    # The new homes are accounts now, so the resolver counts them.
    listed = jello("profile", "list", "--cli", "codex")
    assert "zeta" in listed.stdout


REFUSALS = [
    (["--cli", "claude", "bad name"], 2, "claude: invalid profile name: bad name"),
    # R9: the label rule is ASCII, whatever the locale calls alphanumeric.
    (["--cli", "claude", "é"], 2, "claude: invalid profile name: é"),
    (["--cli", "codex", "é"], 2, "codex: invalid profile name: é"),
    (["--cli", "claude", "ok", "--email", "--yes"], 2,
     "claude: --email requires an address"),
    (["--cli", "codex", "ok", "--email", "a@b.test", "--yes"], 2,
     "usage: jello profile create --cli codex NAME [--yes]"),
    (["--cli", "claude", "pri", "--yes"], 1, "claude: profile already exists: pri"),
    (["--cli", "claude", "wor", "--yes"], 1,
     "claude: 'wor' already identifies an existing account"),
    (["--cli", "claude", "e", "--yes"], 1,
     "claude: 'e' matches several existing accounts"),
    (["--cli", "codex", "fresh"], 2,
     "codex: confirmation required; rerun with --yes in a non-interactive shell"),
]


def test_create_refuses_a_command_somebody_else_owns(jello):
    """R8-F2: the success line names `claude-zed`, so a foreign file at that path makes the
    line a lie. The refusal comes before anything is made, and the home stays absent."""
    home = pathlib.Path(jello.home)
    launcher = home / ".local" / "bin" / "claude-zed"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("#!/bin/sh\necho mine\n")

    result = jello("profile", "create", "--cli", "claude", "zed", "--yes")
    assert result.returncode == 1
    assert result.stderr == (
        f"claude: 'zed' would need a command somebody else owns: {launcher}\n")
    assert result.stdout == ""
    assert not (home / ".claude" / ".profiles" / "zed").exists()
    assert launcher.read_text() == "#!/bin/sh\necho mine\n"


def test_create_refuses_a_symlink_even_to_one_of_our_own_launchers(jello):
    """R8-F2's remaining case: a symlink whose target carries the jello header read as
    `ours` through the link, so create replaced the link with a regular file and orphaned
    whatever it pointed at. A symlink at a launcher path is always somebody else's."""
    home = pathlib.Path(jello.home)
    binaries = home / ".local" / "bin"
    binaries.mkdir(parents=True, exist_ok=True)
    real = binaries / "claude-pri"
    real.write_text("#!/bin/sh\n# written by jello setup launchers: account pri\n"
                    "exec env AGENT_PROFILE_LABEL=pri claude \"$@\"\n")
    real.chmod(0o755)
    link = binaries / "claude-zed"
    link.symlink_to(real)

    result = jello("profile", "create", "--cli", "claude", "zed", "--yes")
    assert result.returncode == 1
    assert result.stderr == (
        f"claude: 'zed' would need a command somebody else owns: {link}\n")
    assert result.stdout == ""
    assert not (home / ".claude" / ".profiles" / "zed").exists()
    assert link.is_symlink(), "the link itself must survive"
    assert link.resolve() == real.resolve(), "and still point where it pointed"


def test_a_launcher_that_cannot_be_written_is_reported(jello):
    """R8-F2, the other half: the home exists by then, so the failure is named rather than
    swallowed, and it says what finishes the job."""
    home = pathlib.Path(jello.home)
    binaries = home / ".local" / "bin"
    binaries.mkdir(parents=True, exist_ok=True)
    binaries.chmod(0o500)
    try:
        result = jello("profile", "create", "--cli", "claude", "zed", "--yes")
    finally:
        binaries.chmod(0o700)

    assert result.returncode == 1
    assert result.stderr.startswith("claude: created ")
    assert "its launcher could not be written" in result.stderr
    assert "jello setup launchers" in result.stderr
    # The account itself was made, which is exactly what the message says.
    assert (home / ".claude" / ".profiles" / "zed").is_dir()


def test_create_refusals(jello):
    home = pathlib.Path(jello.home)
    before = sorted(os.listdir(home))
    for argv, code, message in REFUSALS:
        result = jello("profile", "create", *argv)
        assert result.returncode == code, (argv, result.stderr)
        assert message in result.stderr, (argv, result.stderr)
        assert result.stdout == ""
    assert not (home / ".codex-fresh").exists()
    assert not (home / ".claude" / ".profiles" / "wor").exists()
    assert sorted(os.listdir(home)) == before


DASH_NAMES = [
    ("claude", "claude: invalid profile name: -bad"),
    ("codex", "codex: invalid profile name: -bad"),
]
ABSENT_NAMES = [
    ("claude", "usage: jello profile create --cli claude NAME [--email ADDR] [--yes]"),
    ("codex", "usage: jello profile create --cli codex NAME [--yes]"),
]


def test_create_dash_name(jello):
    """After `--`, a dash-prefixed name is a name: the refusal is the shell's
    `codex: invalid profile name: -bad`, not argparse's. The wrappers always send the
    separator. An absent name answers with that CLI's usage line."""
    home = pathlib.Path(jello.home)
    before = sorted(os.listdir(home))
    for cli, message in DASH_NAMES:
        result = jello("profile", "create", "--cli", cli, "--yes", "--", "-bad")
        assert result.returncode == 2, (cli, result.stderr)
        assert result.stderr == message + "\n", (cli, result.stderr)
        assert result.stdout == ""
    for cli, usage in ABSENT_NAMES:
        result = jello("profile", "create", "--cli", cli)
        assert result.returncode == 2, (cli, result.stderr)
        assert result.stderr == usage + "\n", (cli, result.stderr)
        assert result.stdout == ""
    # Without the separator argparse refuses the token itself. The exit code is what the
    # caller sees either way, so only that is asserted.
    bare = jello("profile", "create", "--cli", "codex", "--yes", "-bad")
    assert bare.returncode == 2
    assert bare.stdout == ""
    assert sorted(os.listdir(home)) == before


def test_create_refuses_symlinked_claude_root(tmp_path):
    """Only the claude creator has this check today, so only claude is asserted."""
    home = build_fixture_home(tmp_path / "linked")
    root = os.path.join(home, ".claude", ".profiles")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.rename(root, str(tmp_path / "moved"))
    os.symlink(str(elsewhere), root)
    result = run_jello(["profile", "create", "--cli", "claude", "zed", "--yes"],
                       fixture_env(home))
    assert result.returncode == 1
    assert result.stderr == f"claude: profile root must not be a symlink: {root}\n"
    assert sorted(os.listdir(elsewhere)) == []
