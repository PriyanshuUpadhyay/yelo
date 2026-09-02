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
    # The prime creator links these from the base agent home; the absent ones are skipped.
    base = home / ".prime" / "agent"
    base.mkdir(parents=True)
    (base / "settings.json").write_text("{}\n")
    (base / "bin").mkdir()

    result = jello("profile", "create", "--cli", "claude", "zed",
                   "--email", "zed@example.test", "--yes")
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "Created profile 'zed'. Sign in with: claude --profile zed auth login\n"
    )
    directory = home / ".claude" / ".profiles" / "zed"
    assert mode(directory) == 0o700
    assert (directory / "email").read_text() == "zed@example.test\n"

    result = jello("profile", "create", "--cli", "codex", "zeta", "--yes")
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "Created profile 'zeta'. Sign in with: codex --profile zeta login\n"
    )
    directory = home / ".codex-zeta"
    assert mode(directory) == 0o700
    assert mode(directory / "sessions") == 0o700
    assert (directory / "config.toml").read_text() == 'model = "gpt-5-codex"\n'
    assert mode(directory / "config.toml") == 0o600
    assert link_to(directory / "hooks.json") == os.path.realpath(home / ".codex" / "hooks.json")
    assert link_to(directory / "AGENTS.md") == os.path.realpath(real_agents)

    result = jello("profile", "create", "--cli", "prime", "zeta", "--yes")
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "Created profile 'zeta'. Start it with 'prime-agent --profile zeta', "
        "then run /login.\n"
    )
    directory = home / ".prime" / "agent-zeta"
    assert mode(directory) == 0o700
    assert (directory / "auth.json").read_text() == "{}\n"
    assert mode(directory / "auth.json") == 0o600
    assert link_to(directory / "settings.json") == os.path.realpath(base / "settings.json")
    assert link_to(directory / "bin") == os.path.realpath(base / "bin")
    for absent in ("extensions", "skills", "harness"):
        assert not (directory / absent).exists()

    # The new homes are accounts now, so the resolver counts them.
    listed = jello("profile", "list", "--cli", "codex")
    assert "zeta" in listed.stdout


REFUSALS = [
    (["--cli", "claude", "bad name"], 2, "claude: invalid profile name: bad name"),
    (["--cli", "prime", "bad name"], 2, "prime-agent: invalid profile name: bad name"),
    # R9: the label rule is ASCII, whatever the locale calls alphanumeric.
    (["--cli", "claude", "é"], 2, "claude: invalid profile name: é"),
    (["--cli", "codex", "é"], 2, "codex: invalid profile name: é"),
    (["--cli", "prime", "é"], 2, "prime-agent: invalid profile name: é"),
    (["--cli", "claude", "ok", "--email", "--yes"], 2,
     "claude: --email requires an address"),
    (["--cli", "codex", "ok", "--email", "a@b.test", "--yes"], 2,
     "usage: codex profile create NAME [--yes]"),
    (["--cli", "claude", "pri", "--yes"], 1, "claude: profile already exists: pri"),
    (["--cli", "claude", "wor", "--yes"], 1,
     "claude: 'wor' already identifies an existing account"),
    (["--cli", "claude", "e", "--yes"], 1,
     "claude: 'e' matches several existing accounts"),
    (["--cli", "codex", "fresh"], 2,
     "codex: confirmation required; rerun with --yes in a non-interactive shell"),
]


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
    ("prime", "prime-agent: invalid profile name: -bad"),
]
ABSENT_NAMES = [
    ("claude", "usage: claude profile create NAME [--email ADDR] [--yes]"),
    ("codex", "usage: codex profile create NAME [--yes]"),
    ("prime", "usage: prime-agent profile create NAME [--yes]"),
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
