"""One fixture account layout, and one way to run yelo against it.

Every test that touches the filesystem builds this layout under a temporary HOME and runs
yelo in a subprocess: `core.HOME` is resolved once at import, the way the reference script
resolves it, so a test cannot move HOME inside its own process. No test reads the real
~/.claude, ~/.codex*, ~/.prime, the real Keychain, or the real usage caches.

`build_usage_home` is the W1 worked example: the layout the usage goldens were rendered
from. Every epoch it writes is `now` plus a fixed offset that sits thirty seconds inside
its minute and well away from an hour or day boundary, so the humanized `reset` strings
are the same whichever second the fixture is built in.

`bench` is the install-side twin: a temporary HOME, a fake ~/dotfiles to own things, and a
`bin` directory first on a PATH that carries nothing else of ours. A test that needs a
`herdr`, `swift`, or `git` writes a fake with `bench.fake(...)` and reads back the argv it
was called with.

Law L7 does not depend on a test remembering any of that: `sealed_home` is autouse, so
every test in the suite -- including one that builds its own layout -- starts with HOME,
`XDG_CACHE_HOME`, `XDG_STATE_HOME`, and `YELO_DOTFILES_ROOT` inside a temporary tree and
with a PATH that carries no `herdr`, `swift`, `claude`, `codex`, `agy`, or `node`.
"""

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time

import pytest

# The Keychain probe is the only thing core shells out to. /usr/bin/false makes "signed in"
# a fixed no, /usr/bin/true a fixed yes.
FALSE_BIN = "/usr/bin/false"
TRUE_BIN = "/usr/bin/true"

# The binaries yelo shells out to. None of them may be reachable by name from a test
# process: `herdr` would answer from the live server, `swift` would start a real build, and
# the three providers would be launched for real. A test that needs one writes a fake and
# puts it on PATH itself (`bench.fake`, the `hud` fixture, WatcherFixture).
BLOCKED_BINARIES = ("herdr", "swift", "claude", "codex", "agy", "node")
# The only directories a test's PATH keeps: the shell stubs need coreutils, `zsh`, and
# `security`, and nothing the suite reaches for is installed anywhere else.
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")

FIXTURE_ACCOUNTS = {
    "claude": [("pri", "pri@example.test"), ("work", "work@example.test")],
    "codex": [("base", "base@example.test", "pro", "acc-base"),
              ("alt", "alt@example.test", "plus", "acc-alt")],
    "prime": [("solo", "acc-alt")],
}


@pytest.fixture(scope="session")
def sealed_path(tmp_path_factory):
    """The system PATH with the six blocked binaries removed, wherever they live.

    PATH cannot hide one name out of a directory, and /usr/bin holds `swift` next to the
    coreutils the fixture shell stubs call, so a directory that holds a blocked name is
    replaced by a symlink farm of everything else it holds. Everything outside the system
    directories is dropped outright: that is where `herdr`, the three providers, and `node`
    are installed on this machine. Built once per session.
    """
    farm = tmp_path_factory.mktemp("path")
    blocked = set(BLOCKED_BINARIES)
    entries = []
    for directory in SYSTEM_PATH:
        names = set(os.listdir(directory)) if os.path.isdir(directory) else set()
        if not names & blocked:
            entries.append(directory)
            continue
        mirror = farm / directory.strip("/").replace("/", "-")
        mirror.mkdir()
        for name in sorted(names - blocked):
            (mirror / name).symlink_to(os.path.join(directory, name))
        entries.append(str(mirror))
    return os.pathsep.join(entries)


@pytest.fixture(autouse=True)
def sealed_home(tmp_path_factory, monkeypatch, sealed_path):
    """Law L7 for the whole suite, rather than one test at a time (F29).

    Every test starts with HOME, the three XDG roots, and `YELO_DOTFILES_ROOT` inside a
    temporary tree, and with a PATH that carries none of the six binaries yelo shells out
    to: a test that forgets to move HOME still cannot read the real ~/.claude or ~/.codex,
    and no test can reach the live Herdr, start a Swift build, or launch a provider. The
    fixtures below still set the same variables explicitly for the subprocess they run --
    this is the floor, not their replacement, and a test that wants one of the six writes a
    fake and puts it on PATH itself.
    """
    sealed = tmp_path_factory.mktemp("sealed")
    home = sealed / "home"
    home.mkdir()
    (sealed / "dotfiles" / "home").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("YELO_DOTFILES_ROOT", str(sealed / "dotfiles"))
    monkeypatch.setenv("PATH", sealed_path)
    # /usr/bin/python3 is Apple's, and it caches its bytecode under
    # $HOME/Library/Caches/com.apple.python: a write into the HOME under test that no test
    # asked for. The stubs run whatever `env python3` finds, so the switch goes here.
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    for name in BLOCKED_BINARIES:
        assert shutil.which(name) is None, f"{name} is still reachable from a test"
    return home


def jwt(payload):
    """An id_token is read for its payload only — no signature is checked — so a header
    and a payload segment are enough to stand in for one."""
    def segment(data):
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{segment({'alg': 'none'})}.{segment(payload)}.signature"


def codex_auth(email, plan, account_id):
    return {
        "tokens": {
            "id_token": jwt({
                "email": email,
                "https://api.openai.com/auth": {
                    "chatgpt_plan_type": plan,
                    "chatgpt_account_id": account_id,
                },
            }),
            "access_token": "access-" + account_id,
            "account_id": account_id,
        }
    }


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def build_fixture_home(home):
    """Two claude profiles, two codex homes, one prime home, with fixed bytes so the
    rendered tables are the same on every machine."""
    home = str(home)
    for name, email in FIXTURE_ACCOUNTS["claude"]:
        write(os.path.join(home, ".claude", ".profiles", name, "email"), email + "\n")
    for name, email, plan, account_id in FIXTURE_ACCOUNTS["codex"]:
        directory = os.path.join(home, ".codex" if name == "base" else ".codex-" + name)
        os.makedirs(os.path.join(directory, "sessions"), exist_ok=True)
        write(os.path.join(directory, "auth.json"),
              json.dumps(codex_auth(email, plan, account_id)) + "\n")
        write(os.path.join(directory, "config.toml"), 'model = "gpt-5-codex"\n')
    write(os.path.join(home, ".codex", "profile-label"), "base\n")
    for name, account_id in FIXTURE_ACCOUNTS["prime"]:
        write(os.path.join(home, ".prime", "agent-" + name, "auth.json"),
              json.dumps({"openai-codex": {"type": "oauth",
                                           "access": jwt({"sub": account_id}),
                                           "accountId": account_id}}) + "\n")
    return home


# One entry of every kind law L3 has to rule on, per layout: names that pass and names
# that fail the label rule, a hidden dot directory, a plain file where a home would sit,
# and a codex home without the sessions/ marker.
CENSUS_DIRS = [
    ".claude/.profiles/pri",
    ".claude/.profiles/work-2",
    ".claude/.profiles/bad name",
    ".claude/.profiles/-lead",
    ".claude/.profiles/.hidden",
    ".claude/.profiles/.session-map",
    ".codex/sessions",
    ".codex-alt/sessions",
    ".codex-nomarker",
    ".codex-bad name",
    ".codex-",
    ".prime/agent",
    ".prime/agent-solo",
    ".prime/agent-x2",
    ".prime/agent-bad name",
    ".prime/.hidden",
]
CENSUS_FILES = [
    ".claude/.profiles/notes.txt",
    ".claude/.profiles/.aliases",
    ".codex-file",
    ".prime/agent-file",
]


def build_census_home(home):
    """A layout for the L3 census: every entry kind, and nothing that needs credentials."""
    home = str(home)
    for relative in CENSUS_DIRS:
        os.makedirs(os.path.join(home, *relative.split("/")), exist_ok=True)
    for relative in CENSUS_FILES:
        write(os.path.join(home, *relative.split("/")), "not a profile home\n")
    write(os.path.join(home, ".codex", "profile-label"), "base\n")
    return home


def fixture_env(home, security_bin=FALSE_BIN):
    """A clean environment: HOME moved and the Keychain probe pinned. There is no usage
    feed to point anywhere: the rows are read in process from the cache files under HOME."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("AGENT_PROFILES_")}
    env["HOME"] = str(home)
    env["AGENT_PROFILES_SECURITY_BIN"] = security_bin
    # No codex binary, so `canFetch` is false for codex on every fixture row.
    env["CODEX_BIN"] = os.path.join(str(home), "no-codex-binary")
    return env


# --- the W1 usage layout ----------------------------------------------------------------
# Offsets from `now`, all 30 seconds inside their minute and away from an hour boundary, so
# the reset strings the reference renders and the ones yelo renders later agree.
FIVE_HOUR_RESET = 1290          # 21m30s  -> "21m"
SEVEN_DAY_RESET = 264630        # 3d1h30m30s -> "3d1h"
CODEX_RESET = 95430             # 1d2h30m30s -> "1d2h"
USAGE_ACCOUNTS = {
    "claude": [("pri", "pri@example.test"), ("work", "work@example.test")],
    "codex": [("", "base@example.test", "pro", "acc-base"),
              ("alt", "alt@example.test", "plus", "acc-alt")],
}


def statusline_cache(now, five, seven):
    """What statusline-command.sh writes: no fetched_at while the value has advanced, and
    activity_at restamped on every render."""
    return {
        "five_hour": {"used_percentage": five, "resets_at": now + FIVE_HOUR_RESET},
        "seven_day": {"used_percentage": seven, "resets_at": now + SEVEN_DAY_RESET},
        "ts": now - 90, "activity_at": now - 30, "source": "statusline",
    }


def api_cache(now, five, seven, age):
    """What `yelo usage fetch` writes: one fetch time for both clocks."""
    return {
        "five_hour": {"used_percentage": five, "resets_at": now + FIVE_HOUR_RESET},
        "seven_day": {"used_percentage": seven, "resets_at": now + SEVEN_DAY_RESET},
        "ts": now - age, "fetched_at": now - age, "source": "api",
    }


def codex_cache(now, percent, age):
    return {
        "rate_limits": {
            "primary": {"used_percent": percent, "window_minutes": 10080,
                        "resets_at": now + CODEX_RESET},
            "secondary": None,
        },
        "fetched_at": now - age, "source": "api",
    }


def rollout(directory, now, percent):
    """One codex rollout line carrying rate limits, laid out the way Codex writes it."""
    path = os.path.join(directory, "sessions", "2026", "09", "01",
                        "rollout-2026-09-01T09-00-00-019e08eb-508e-7e73-8bc3-1e9c69b5dfd3.jsonl")
    record = {"timestamp": "2026-09-01T09:00:00", "type": "event_msg", "payload": {
        "rate_limits": {
            "primary": {"used_percent": percent, "window_minutes": 10080,
                        "resets_at": now + CODEX_RESET},
            "secondary": None,
        }}}
    write(path, json.dumps(record) + "\n")


def build_usage_home(home, now):
    """The W1 worked example: `pri` with three caches, `work` with none, the codex base
    with an API cache, and `alt` with a rollout only. No prime home, and no account whose
    label the census and the reference script would disagree about."""
    home = str(home)
    for name, email in USAGE_ACCOUNTS["claude"]:
        write(os.path.join(home, ".claude", ".profiles", name, "email"), email + "\n")
    profile = os.path.join(home, ".claude", ".profiles", "pri")
    write(os.path.join(profile, ".usage-cache.json"),
          json.dumps(statusline_cache(now, 43, 11)) + "\n")
    write(os.path.join(profile, ".usage-api-cache.json"),
          json.dumps(api_cache(now, 41, 12, 120)) + "\n")
    write(os.path.join(profile, ".usage-api-cache-fable.json"),
          json.dumps(api_cache(now, 40, 5, 150)) + "\n")
    for name, email, plan, account_id in USAGE_ACCOUNTS["codex"]:
        directory = os.path.join(home, ".codex" if not name else ".codex-" + name)
        os.makedirs(os.path.join(directory, "sessions"), exist_ok=True)
        write(os.path.join(directory, "auth.json"),
              json.dumps(codex_auth(email, plan, account_id)) + "\n")
    write(os.path.join(home, ".codex", "profile-label"), "\n")
    write(os.path.join(home, ".codex", ".usage-hud-api-cache.json"),
          json.dumps(codex_cache(now, 30, 60)) + "\n")
    rollout(os.path.join(home, ".codex-alt"), now, 18)
    return home


def usage_env(home):
    """The W1 environment: both claude profiles signed in, and no codex binary."""
    return fixture_env(home, security_bin=TRUE_BIN)


@pytest.fixture
def usage_home(tmp_path):
    now = int(time.time())
    return build_usage_home(tmp_path / "usage-home", now), now


def run_yelo(argv, env, cwd=None, stdin=""):
    return subprocess.run(
        [sys.executable, "-m", "yelo.cli", *argv],
        capture_output=True, text=True, env=env, cwd=cwd, input=stdin,
    )


@pytest.fixture
def fixture_home(tmp_path):
    return build_fixture_home(tmp_path / "home")


@pytest.fixture
def yelo(fixture_home):
    """Run yelo against the fixture HOME; extra environment keys override the defaults."""
    def call(*argv, stdin="", cwd=None, **overrides):
        env = fixture_env(fixture_home)
        env.update(overrides)
        return run_yelo(list(argv), env, cwd=cwd, stdin=stdin)

    call.home = fixture_home
    return call


# --- the install bench --------------------------------------------------------------------
# The bench PATH is the fake-binary directory and nothing else. macOS ships `swift` in
# /usr/bin, so a PATH with the system directories on it would let a test start a real build
# (and a machine with `herdr` installed would let one reach the live server). Tests run
# yelo through `sys.executable`, which is absolute, so nothing here needs more; a test that
# wants `git`, `herdr`, or `swift` writes its own with `bench.fake(...)`.
# A settings.json shaped like a live one. yelo writes none of it: the only row read out of
# it is `agents`, which asks whether some SessionStart group runs agent-host-context.py.
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


def env_for(home, dotfiles, binaries=None, **overrides):
    """The bench environment. `binaries` is the fake-binary directory that becomes the whole
    PATH; the HUD suite passes none and keeps the machine's PATH, because its targets are a
    real `swift` and a real `codesign`."""
    env = {
        "HOME": str(home),
        "PATH": str(binaries) if binaries is not None else os.environ.get("PATH", ""),
        "XDG_CACHE_HOME": os.path.join(str(home), ".cache"),
        "XDG_CONFIG_HOME": os.path.join(str(home), ".config"),
        "XDG_STATE_HOME": os.path.join(str(home), ".local", "state"),
        "YELO_DOTFILES_ROOT": str(dotfiles),
    }
    env.update(overrides)
    return env


@pytest.fixture
def bench(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    dotfiles = tmp_path / "dotfiles"
    (dotfiles / "home" / ".claude" / "hooks").mkdir(parents=True)
    binaries = tmp_path / "bin"
    binaries.mkdir()

    class Bench:
        def __init__(self):
            self.home = home
            self.dotfiles = dotfiles
            self.bin = binaries
            self.settings = home / ".claude" / "settings.json"
            self.profiles = home / ".claude" / ".profiles"
            self.launchers = home / ".local" / "bin"
            self.state = home / ".local" / "state"

        def run(self, *argv, **overrides):
            return run_yelo(list(argv), env_for(home, dotfiles, binaries, **overrides))

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

        def fake(self, name, body="exit 0"):
            """An executable that records its argv, one shell-quoted line per call, so a
            test can assert what yelo asked for without a real binary existing."""
            path = binaries / name
            log = binaries / f"{name}.log"
            path.write_text(
                "#!/bin/sh\n"
                f'printf "%s\\n" "$*" >> {log}\n'
                f"{body}\n"
            )
            path.chmod(0o755)
            return path

        def calls(self, name):
            log = binaries / f"{name}.log"
            return log.read_text().splitlines() if log.exists() else []

    return Bench()
