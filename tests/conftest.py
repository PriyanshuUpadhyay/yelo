"""One fixture account layout, and one way to run jello against it.

Every test that touches the filesystem builds this layout under a temporary HOME and runs
jello in a subprocess: `core.HOME` is resolved once at import, the way the reference script
resolves it, so a test cannot move HOME inside its own process. No test reads the real
~/.claude, ~/.codex*, ~/.prime, the real Keychain, or the real usage caches.

`build_usage_home` is the W1 worked example: the layout the usage goldens were rendered
from. Every epoch it writes is `now` plus a fixed offset that sits thirty seconds inside
its minute and well away from an hour or day boundary, so the humanized `reset` strings
are the same whichever second the fixture is built in.
"""

import base64
import json
import os
import subprocess
import sys
import time

import pytest

# The Keychain probe is the only thing core shells out to. /usr/bin/false makes "signed in"
# a fixed no, /usr/bin/true a fixed yes.
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
# the reset strings the reference renders and the ones jello renders later agree.
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
    """What `jello usage fetch` writes: one fetch time for both clocks."""
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
