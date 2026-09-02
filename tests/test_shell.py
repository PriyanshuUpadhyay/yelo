"""C07-C12: the rendered zsh init, and the four wrappers it defines. R4.

Every wrapper test runs real zsh against a temporary HOME with fake `claude`, `codex`, and
`prime-agent` executables on PATH that record their argv and the profile environment they
were handed. That is the only way to prove R4's real claim -- that the ported text still
exports the same variables to the same binary -- because the behavior lives in the shell,
not in Python.

`zsh -f` keeps the user's own rc files out of it, and stdin is a pipe, so `[ -t 0 ]` is
false exactly as it is for a hook or a script.
"""

import os
import shutil
import subprocess
import sys

import pytest

import jello as jello_package
from jello import shell

VERSION_LINE = f"# jello {jello_package.__version__}"
DUMPED_VARS = (
    "AGENT_PROFILE_LABEL", "CLAUDE_PROFILE_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    "CODEX_HOME", "CODEX_CONFIG_PATH",
    "PRIME_AGENT_PROFILE_LABEL", "PRIME_AGENT_CODING_AGENT_DIR",
)
FAKE_BINARY = """#!/bin/sh
: > "$JELLO_TEST_DUMP"
for value in "$@"; do printf 'argv\t%s\n' "$value" >> "$JELLO_TEST_DUMP"; done
for name in {names}; do
  eval "value=\\${{$name-}}"
  printf 'env\t%s\t%s\n' "$name" "$value" >> "$JELLO_TEST_DUMP"
done
"""

# Truncates on every call, so the dump always holds the last invocation -- which is the
# `profile create` the test is about, not the `shell-init` that set the wrappers up.
RECORDING_JELLO = """#!/bin/sh
: > "$JELLO_ARGV_DUMP"
for value in "$@"; do printf '%s\\n' "$value" >> "$JELLO_ARGV_DUMP"; done
case "$1" in
  shell-init) exec "$JELLO_REAL" "$@" ;;
esac
exit 0
"""

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed")


def executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def bench(fixture_home, tmp_path):
    """A temp PATH holding a `jello` shim and the three fake CLI binaries, plus the
    environment that pins HOME, the cache, the Keychain probe, and the usage feed."""
    home = str(fixture_home)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dump = tmp_path / "dump.txt"
    executable(bin_dir / "jello", f'#!/bin/sh\nexec "{sys.executable}" -m jello.cli "$@"\n')
    for name in ("claude", "codex", "prime-agent"):
        executable(bin_dir / name, FAKE_BINARY.format(names=" ".join(DUMPED_VARS)))

    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AGENT_PROFILE", "CLAUDE_", "CODEX_", "PRIME_AGENT_"))}
    env.update({
        "HOME": home,
        "PATH": f"{bin_dir}:{env.get('PATH', '')}",
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "JELLO_TEST_DUMP": str(dump),
        "AGENT_PROFILES_SECURITY_BIN": "/usr/bin/false",
        "AGENT_PROFILES_USAGE_DATA": os.path.join(home, "no-usage-feed"),
    })

    class Bench:
        def __init__(self):
            self.home = home
            self.env = env
            self.dump = dump

        def zsh(self, script, **overrides):
            """Evaluate the init, then run `script`, with stdin a pipe (never a TTY)."""
            merged = dict(env, **overrides)
            return subprocess.run(
                ["zsh", "-f", "-c", 'eval "$(jello shell-init zsh)"\n' + script],
                capture_output=True, text=True, env=merged, input="",
            )

        def jello(self, *argv, **overrides):
            return subprocess.run(
                [sys.executable, "-m", "jello.cli", *argv],
                capture_output=True, text=True, env=dict(env, **overrides), input="",
            )

        def record_jello(self, tmp_path):
            """Swap the real jello shim for one that only records its argv, and return a
            reader for it. Proving the wrapper's argv needs the argv, not its effect."""
            fake_bin = tmp_path / "fake-jello-bin"
            fake_bin.mkdir(exist_ok=True)
            dump = tmp_path / "jello-argv.txt"
            # `shell-init` is delegated to the real jello, because the wrapper text under
            # test has to be the real one; every other call is recorded and stops there.
            executable(fake_bin / "jello", RECORDING_JELLO)
            self.recording_env = {
                "PATH": f"{fake_bin}:{env['PATH']}",
                "JELLO_ARGV_DUMP": str(dump),
                "JELLO_REAL": str(bin_dir / "jello"),
            }
            return lambda: dump.read_text().splitlines() if dump.exists() else None

        def recorded(self):
            """{"argv": [...], "env": {...}} from the fake binary, or None if none ran."""
            if not self.dump.exists():
                return None
            argv, seen = [], {}
            for line in self.dump.read_text().splitlines():
                kind, _, rest = line.partition("\t")
                if kind == "argv":
                    argv.append(rest)
                elif kind == "env":
                    name, _, value = rest.partition("\t")
                    seen[name] = value
            return {"argv": argv, "env": seen}

    return Bench()


def test_cache_round_trip(bench):
    """C07: the printed script is the cache when the cache carries this version, and a
    cache from another version is re-rendered over. That is the only invalidation."""
    first = bench.jello("shell-init", "zsh")
    assert first.returncode == 0, first.stderr
    assert first.stdout.splitlines()[0] == VERSION_LINE

    cache = os.path.join(bench.env["XDG_CACHE_HOME"], "jello", "shell-init.zsh")
    assert open(cache, encoding="utf-8").read() == first.stdout

    # L6: rendering is pure, so a second call at one version is byte-identical.
    second = bench.jello("shell-init", "zsh")
    assert second.stdout == first.stdout

    # The cache is read, not re-rendered: a marker planted in it comes back out.
    marked = first.stdout + "# marker\n"
    open(cache, "w", encoding="utf-8").write(marked)
    assert bench.jello("shell-init", "zsh").stdout == marked

    # A cache stamped with another version is replaced whole, marker and all.
    open(cache, "w", encoding="utf-8").write("# jello 0.0.0\n# marker\n")
    third = bench.jello("shell-init", "zsh")
    assert third.stdout == first.stdout
    assert "# marker" not in open(cache, encoding="utf-8").read()

    leftovers = [name for name in os.listdir(os.path.dirname(cache))
                 if name.startswith(".shell-init.zsh.")]
    assert leftovers == [], "the atomic write left a temp file behind"


def test_zsh_parses_and_defines(bench, tmp_path):
    """C08: the text is valid zsh, and evaluating it defines all four wrappers."""
    rendered = tmp_path / "shell-init.zsh"
    rendered.write_text(shell.render_zsh(jello_package.__version__))
    parsed = subprocess.run(["zsh", "-n", str(rendered)], capture_output=True, text=True)
    assert parsed.returncode == 0, parsed.stderr

    result = bench.zsh("whence -w claude cc CC codex prime-agent")
    assert result.returncode == 0, result.stderr
    for name in ("claude", "cc", "CC", "codex", "prime-agent"):
        assert f"{name}: function" in result.stdout


@pytest.mark.parametrize("flag", ["--profile pri", "--profile=pri"])
def test_claude_wrapper_exports(bench, flag):
    """C09: both flag forms of M1 reach the binary with the same three exports."""
    result = bench.zsh(f"claude {flag} hello")
    assert result.returncode == 0, result.stderr
    recorded = bench.recorded()
    assert recorded["argv"] == ["hello"]
    assert recorded["env"]["AGENT_PROFILE_LABEL"] == "pri"
    assert recorded["env"]["CLAUDE_PROFILE_DIR"] == os.path.join(
        bench.home, ".claude", ".profiles", "pri"
    )
    assert recorded["env"]["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == os.path.join(
        bench.home, ".claude-pri"
    )
    # A profile flag never reaches the binary.
    assert "--profile" not in recorded["argv"]


def test_claude_wrapper_refuses_a_repeated_flag(bench):
    result = bench.zsh("claude --profile pri --profile work hello")
    assert result.returncode == 2
    assert "claude: --profile specified more than once" in result.stderr
    assert bench.recorded() is None


def test_codex_host_guard(bench):
    """C10: the codex exports, and the host guard that replaced the Herdr checks --
    called when it is defined, skipped when it is not, and able to stop the launch."""
    codex_home = os.path.join(bench.home, ".codex-alt")

    without_guard = bench.zsh("codex --profile alt resume")
    assert without_guard.returncode == 0, without_guard.stderr
    recorded = bench.recorded()
    assert recorded["argv"] == ["resume"]
    assert recorded["env"]["CODEX_HOME"] == codex_home
    assert recorded["env"]["CODEX_CONFIG_PATH"] == os.path.join(codex_home, "config.toml")

    # Defined and passing: the guard sees the scanned subcommand, then the binary runs.
    passing = bench.zsh(
        '_codex_host_guard() { print -u2 "guard:$1:$2"; return 0 }\n'
        "codex -C /tmp --profile alt resume"
    )
    assert passing.returncode == 0, passing.stderr
    assert "guard:resume:-C" in passing.stderr, "the guard classifies by subcommand, not argv[1]"
    assert bench.recorded()["argv"] == ["-C", "/tmp", "resume"]

    # Defined and failing: the launch stops and the wrapper carries the guard's status.
    bench.dump.unlink()
    failing = bench.zsh(
        "_codex_host_guard() { return 3 }\ncodex --profile alt resume"
    )
    assert failing.returncode == 3
    assert bench.recorded() is None, "a failed guard must stop the binary"


def test_codex_plumbing_keeps_the_callers_home(bench):
    """M2: a plumbing subcommand with no --profile exports nothing."""
    result = bench.zsh("codex --version")
    assert result.returncode == 0, result.stderr
    recorded = bench.recorded()
    assert recorded["argv"] == ["--version"]
    assert recorded["env"]["CODEX_HOME"] == ""


@pytest.mark.parametrize(
    "command,message",
    [
        ("claude --profile", "claude: --profile requires a name"),
        ("codex --profile", "codex: --profile requires a name"),
        ("prime-agent --profile", "prime-agent: --profile requires a name"),
    ],
)
def test_valueless_profile_non_tty(bench, command, message):
    """C11: outside a TTY the picker is unreadable and its read would hang, so the
    valueless flag is a refusal for all three wrappers."""
    result = bench.zsh(f"{command}\nprint -r -- \"status=$?\"")
    assert message in result.stderr
    assert "status=2" in result.stdout
    assert bench.recorded() is None


def test_resume_uses_session_map(bench):
    """C12: herdr replays a bare `claude --resume <sid>` with neither the flag nor the
    pane env, so the map written by `jello resume map-session` is the only source left."""
    sid = "1234-abcd"
    map_dir = os.path.join(bench.home, ".claude", ".profiles", ".session-map")
    os.makedirs(map_dir, exist_ok=True)
    entry = os.path.join(map_dir, sid)
    open(entry, "w", encoding="utf-8").write("pri\n")
    os.utime(entry, (0, 0))

    result = bench.zsh(f"claude --resume {sid}")
    assert result.returncode == 0, result.stderr
    recorded = bench.recorded()
    assert recorded["argv"] == ["--resume", sid]
    assert recorded["env"]["AGENT_PROFILE_LABEL"] == "pri"
    assert os.stat(entry).st_mtime > 0, "a mapped launch keeps the prune off a live session"

    # A stale entry names no account, so it falls through to the ambient env.
    open(entry, "w", encoding="utf-8").write("retired-name\n")
    fallen = bench.zsh(f"claude --resume {sid}", AGENT_PROFILE_LABEL="work")
    assert fallen.returncode == 0, fallen.stderr
    assert bench.recorded()["env"]["AGENT_PROFILE_LABEL"] == "work"


CREATE_FORWARDS = (
    ("claude profile create -bad", ["profile", "create", "--cli", "claude", "--", "-bad"]),
    ("codex profile create work --yes",
     ["profile", "create", "--cli", "codex", "--yes", "--", "work"]),
    ("prime-agent profile create", ["profile", "create", "--cli", "prime"]),
    ("claude profile create pri --email a@b.test --yes",
     ["profile", "create", "--cli", "claude", "--email", "a@b.test", "--yes", "--", "pri"]),
)


@pytest.mark.parametrize("command,argv", CREATE_FORWARDS, ids=[c[0] for c in CREATE_FORWARDS])
def test_profile_create_argv(bench, tmp_path, command, argv):
    """NAME is the first token whatever it looks like, so it travels after `--` while the
    options stay in front of it. A `create` with no token at all forwards no `--` either,
    which is how jello tells "no name" apart from "an empty name"."""
    recorded = bench.record_jello(tmp_path)
    result = bench.zsh(command, **bench.recording_env)
    assert result.returncode == 0, result.stderr
    assert recorded() == argv


def test_profile_create_forwards_to_jello(bench):
    """Record decisions create-order and create-argv: the wrapper keeps no prompt and no
    refusal of its own; it forwards argv and jello answers."""
    result = bench.zsh("claude profile create pri --yes")
    assert result.returncode == 1
    assert result.stderr == "claude: profile already exists: pri\n"

    taken = bench.zsh("claude profile create wor --yes")
    assert taken.returncode == 1
    assert taken.stderr == "claude: 'wor' already identifies an existing account\n"

    invalid = bench.zsh("codex profile create 'bad name' --yes")
    assert invalid.returncode == 2
    assert invalid.stderr == "codex: invalid profile name: bad name\n"

    # The whole point of the `--`: a dash-prefixed name reaches the module that owns the
    # refusal instead of being claimed as an unknown option on the way.
    dashed = bench.zsh("claude profile create -bad --yes")
    assert dashed.returncode == 2
    assert dashed.stderr == "claude: invalid profile name: -bad\n"

    nameless = bench.zsh("prime-agent profile create")
    assert nameless.returncode == 2
    assert nameless.stderr == "usage: prime-agent profile create NAME [--yes]\n"
