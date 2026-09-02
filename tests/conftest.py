"""One fixture account layout, and one way to run jello against it.

Every test that touches the filesystem builds this layout under a temporary HOME and runs
jello in a subprocess: `core.HOME` is resolved once at import, the way the reference script
resolves it, so a test cannot move HOME inside its own process. No test reads the real
~/.claude, ~/.codex*, ~/.prime, the real Keychain, or the real usage feed.
"""

import base64
import json
import os
import subprocess
import sys

import pytest

# The Keychain probe and the usage feed are the only two things core shells out to.
# /usr/bin/false makes "signed in" a fixed no; a path that cannot be executed makes the
# usage feed absent, so `list --usage` falls back to each account's own cache.
FALSE_BIN = "/usr/bin/false"
TRUE_BIN = "/usr/bin/true"

FIXTURE_ACCOUNTS = {
    "claude": [("pri", "pri@example.test"), ("work", "work@example.test")],
    "codex": [("base", "base@example.test", "pro", "acc-base"),
              ("alt", "alt@example.test", "plus", "acc-alt")],
    "prime": [("solo", "acc-alt")],
}


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


def fixture_env(home, security_bin=FALSE_BIN, usage_data=None):
    """A clean environment: HOME moved, the Keychain probe pinned, the usage feed absent
    unless a test supplies its own stub."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("AGENT_PROFILES_")}
    env["HOME"] = str(home)
    env["AGENT_PROFILES_SECURITY_BIN"] = security_bin
    env["AGENT_PROFILES_USAGE_DATA"] = str(
        usage_data if usage_data is not None else os.path.join(str(home), "no-usage-feed")
    )
    return env


def run_jello(argv, env, cwd=None, stdin=""):
    return subprocess.run(
        [sys.executable, "-m", "jello.cli", *argv],
        capture_output=True, text=True, env=env, cwd=cwd, input=stdin,
    )


@pytest.fixture
def fixture_home(tmp_path):
    return build_fixture_home(tmp_path / "home")


@pytest.fixture
def jello(fixture_home):
    """Run jello against the fixture HOME; extra environment keys override the defaults."""
    def call(*argv, stdin="", cwd=None, **overrides):
        env = fixture_env(fixture_home)
        env.update(overrides)
        return run_jello(list(argv), env, cwd=cwd, stdin=stdin)

    call.home = fixture_home
    return call
