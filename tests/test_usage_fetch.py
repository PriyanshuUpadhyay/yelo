"""C07-C10, C22: the refresh path, with a fake Keychain, a local endpoint, and no network.

Every binary the fetch shells out to is a stub on PATH or pinned by an environment
variable, and the usage endpoint is an http.server bound to 127.0.0.1 on an ephemeral
port, so nothing here reaches the real Keychain, the real API, or the real HOME. The
stand-ins are owed one real `jello usage fetch` in the stage 11 member walk.
"""

import hashlib
import http.server
import json
import os
import pathlib
import threading
import time

import pytest

from conftest import codex_auth, fixture_env, run_jello, write
from jello.usage import fetch as fetch_module

TOKEN = "sk-ant-oat-FIXTURE-TOKEN-3f9c1d"
FIVE_HOUR_RESET = 1290
SEVEN_DAY_ISO = "2026-09-05T10:00:00.123456+00:00"
SEVEN_DAY_EPOCH = 1788602400

SECURITY_STUB = '''#!/usr/bin/env python3
"""Stand-in for `security`: an existence probe without -w, the blob with it. Every argv is
recorded, so a test can ask which of the two forms the command under test used."""
import json, os, sys

argv = sys.argv[1:]
log = os.environ.get("FAKE_SECURITY_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("\\t".join(argv) + "\\n")
service = None
for index, item in enumerate(argv):
    if item == "-s" and index + 1 < len(argv):
        service = argv[index + 1]
with open(os.environ["FAKE_KEYCHAIN"], encoding="utf-8") as handle:
    items = json.load(handle)
blob = items.get(service)
if blob is None:
    sys.exit(1)
if "-w" in argv:
    sys.stdout.write(blob)
sys.exit(0)
'''

CLAUDE_STUB = '''#!/usr/bin/env python3
"""Stand-in for `claude -p`: records its argv, then exits with FAKE_CLAUDE_EXIT."""
import os, sys

with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")
    handle.write(os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR", "") + "\\n")
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
'''

CODEX_STUB = '''#!/usr/bin/env python3
"""Stand-in for `codex app-server`: answers initialize, then the rate-limit read."""
import json, sys

for line in sys.stdin:
    message = json.loads(line)
    if message.get("id") == 1:
        print(json.dumps({"id": 1, "result": {}}), flush=True)
    elif message.get("id") == 2:
        print(json.dumps({"id": 2, "result": {"rateLimits": {
            "primary": {"usedPercent": 30.4, "windowDurationMins": 10080,
                        "resetsAt": 4102444800},
            "secondary": None}}}), flush=True)
'''


def executable(path, text):
    write(str(path), text)
    os.chmod(str(path), 0o755)
    return str(path)


def keychain_service(home, name):
    """The signed rule written out here rather than imported, so the test keys the item by
    the rule and the code has to agree with it."""
    identity = os.path.join(str(home), f".claude-{name}")
    return "Claude Code-credentials-" + hashlib.sha256(identity.encode()).hexdigest()[:8]


def usage_payload(now, fable=True):
    payload = {
        "five_hour": {"used_percentage": 43, "resets_at": now + FIVE_HOUR_RESET},
        "seven_day": {"utilization": 12, "resets_at": SEVEN_DAY_ISO},
    }
    if fable:
        # Fable's weekly limit is not a top-level window: it is the limits[] entry scoped
        # to the Fable model.
        payload["limits"] = [
            {"kind": "weekly", "used_percentage": 99},
            {"kind": "weekly_scoped", "scope": {"model": {"display_name": "Fable"}},
             "used_percentage": 5, "resets_at": SEVEN_DAY_EPOCH},
        ]
    return payload


class Endpoint:
    """The usage endpoint, scripted per test: one (status, body) or (status, body, headers)
    per request, the last one repeating once the script runs out. Every request it received
    is kept, so a server that should never have been called can prove it never was."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.requests = []
        endpoint = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                entry = endpoint.script[min(endpoint.calls, len(endpoint.script) - 1)]
                status, body = entry[0], entry[1]
                extra = entry[2] if len(entry) > 2 else {}
                endpoint.calls += 1
                endpoint.requests.append(dict(self.headers))
                raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
                self.send_response(status)
                for key, value in extra.items():
                    self.send_header(key, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format, *args):
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/api/oauth/usage"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def bench(tmp_path):
    """One signed-in claude profile, a fake Keychain holding its token, and stubs for
    `security` and `claude`."""
    home = tmp_path / "home"
    write(os.path.join(str(home), ".claude", ".profiles", "pri", "email"), "p@example.test\n")
    stubs = tmp_path / "stubs"
    keychain = tmp_path / "keychain.json"
    write(str(keychain), json.dumps({
        keychain_service(home, "pri"): json.dumps({"claudeAiOauth": {"accessToken": TOKEN}}),
    }))
    security = executable(stubs / "security", SECURITY_STUB)
    claude = executable(stubs / "claude", CLAUDE_STUB)
    environment = fixture_env(home, security_bin=security)
    environment.update({
        "FAKE_KEYCHAIN": str(keychain),
        "FAKE_CLAUDE_LOG": str(tmp_path / "claude.log"),
        "FAKE_SECURITY_LOG": str(tmp_path / "security.log"),
        "CLAUDE_BIN": claude,
    })
    return {"home": home, "env": environment, "keychain": keychain,
            "log": tmp_path / "claude.log", "security_log": tmp_path / "security.log",
            "tmp": tmp_path}


def fetch(bench, endpoint=None, **overrides):
    environment = dict(bench["env"])
    if endpoint is not None:
        environment["JELLO_USAGE_API_URL"] = endpoint.url
    environment.update(overrides)
    return run_jello(["usage", "fetch"], environment)


def cache_path(bench, name):
    return os.path.join(str(bench["home"]), ".claude", ".profiles", "pri", name)


# --- C07 -------------------------------------------------------------------------------

def test_fetch_claude_writes_caches(bench):
    now = int(time.time())
    endpoint = Endpoint([(200, usage_payload(now))])
    try:
        result = fetch(bench, endpoint)
    finally:
        endpoint.close()
    assert result.returncode == 0, result.stderr
    assert result.stdout == "pri: ok\n"

    base = json.loads(pathlib.Path(cache_path(bench, ".usage-api-cache.json")).read_text())
    assert base["five_hour"] == {"used_percentage": 43, "resets_at": now + FIVE_HOUR_RESET}
    # `utilization` is one of the field names the reference probes for, and the ISO reset
    # with a fraction and a +00:00 offset normalizes to epoch seconds.
    assert base["seven_day"] == {"used_percentage": 12, "resets_at": SEVEN_DAY_EPOCH}
    assert base["source"] == "api" and base["ts"] == base["fetched_at"]

    fable = json.loads(pathlib.Path(cache_path(bench, ".usage-api-cache-fable.json")).read_text())
    assert fable["five_hour"] == base["five_hour"], "the fable cache carries the shared 5h"
    assert fable["seven_day"] == {"used_percentage": 5, "resets_at": SEVEN_DAY_EPOCH}

    # Law L5: only the two cache names, written by replace, no temp file left behind.
    assert set(os.listdir(os.path.dirname(cache_path(bench, "")))) == {
        "email", ".usage-api-cache.json", ".usage-api-cache-fable.json"}


def test_fetch_without_a_fable_window_leaves_that_cache_alone(bench):
    now = int(time.time())
    endpoint = Endpoint([(200, usage_payload(now, fable=False))])
    try:
        assert fetch(bench, endpoint).stdout == "pri: ok\n"
    finally:
        endpoint.close()
    assert not os.path.exists(cache_path(bench, ".usage-api-cache-fable.json"))


def test_fable_write_failure_still_counts_as_a_run(bench):
    """The base cache landed, so the run is not a failure (the reference returns 0 here);
    the status line says the fable cache stayed stale rather than reporting a clean ok."""
    now = int(time.time())
    os.makedirs(cache_path(bench, ".usage-api-cache-fable.json"))
    endpoint = Endpoint([(200, usage_payload(now))])
    try:
        result = fetch(bench, endpoint)
    finally:
        endpoint.close()
    assert result.stdout == "pri: fable-write-failed\n"
    assert result.returncode == 0


def tree(home):
    """Every file under HOME with the bytes that say whether it moved."""
    found = {}
    for base, _, names in os.walk(str(home)):
        for name in names:
            path = os.path.join(base, name)
            stat = os.stat(path)
            found[path] = (stat.st_mtime_ns, stat.st_size)
    return found


def with_codex(bench, tmp_path):
    """The fixture HOME plus one codex home and a stub app-server, so a run writes all
    three caches and prints a second outcome line."""
    directory = os.path.join(str(bench["home"]), ".codex")
    os.makedirs(os.path.join(directory, "sessions"), exist_ok=True)
    write(os.path.join(directory, "auth.json"),
          json.dumps(codex_auth("c@example.test", "pro", "acc-1")) + "\n")
    write(os.path.join(directory, "profile-label"), "\n")
    return executable(tmp_path / "stubs" / "codex", CODEX_STUB)


def test_fetch_writes_only_api_caches(bench, tmp_path):
    """Law L5: a successful run creates or rewrites the three API caches and nothing else
    under HOME, and os.replace leaves no temp file behind."""
    codex = with_codex(bench, tmp_path)
    before = tree(bench["home"])
    endpoint = Endpoint([(200, usage_payload(int(time.time())))])
    try:
        result = fetch(bench, endpoint, CODEX_BIN=codex)
    finally:
        endpoint.close()
    assert result.stdout == "pri: ok\ncx: ok\n" and result.returncode == 0
    after = tree(bench["home"])
    moved = {os.path.basename(path) for path in after
             if path not in before or after[path] != before[path]}
    assert moved == {".usage-api-cache.json", ".usage-api-cache-fable.json",
                     ".usage-hud-api-cache.json"}
    assert set(before) - set(after) == set(), "nothing under HOME was removed"
    assert [path for path in after if ".tmp." in path] == []


# --- C08 -------------------------------------------------------------------------------

def test_fetch_auth_paths(bench):
    now = int(time.time())
    endpoint = Endpoint([(401, {}), (200, usage_payload(now))])
    try:
        result = fetch(bench, endpoint)
    finally:
        endpoint.close()
    assert result.stdout == "pri: ok\n" and result.returncode == 0
    woken = bench["log"].read_text().splitlines()
    assert woken[0] == "-p ok --model haiku"
    assert woken[1] == os.path.join(str(bench["home"]), ".claude-pri")

    # A second 401 after the one wake is a real re-login, and there is never a second wake.
    bench["log"].write_text("")
    endpoint = Endpoint([(401, {})])
    try:
        result = fetch(bench, endpoint)
    finally:
        endpoint.close()
    assert result.stdout == "pri: auth-stale\n" and result.returncode == 1
    assert len(bench["log"].read_text().splitlines()) == 2, "exactly one wake"

    # No Keychain item: the reference never attempts the request and never wakes.
    bench["log"].write_text("")
    write(str(bench["keychain"]), json.dumps({}))
    endpoint = Endpoint([(200, usage_payload(now))])
    try:
        result = fetch(bench, endpoint)
    finally:
        endpoint.close()
    assert result.stdout == "pri: fetch-failed\n" and result.returncode == 1
    assert endpoint.calls == 0 and bench["log"].read_text() == ""


def test_server_error_is_fetch_failed(bench):
    endpoint = Endpoint([(500, {"error": "nope"})])
    try:
        result = fetch(bench, endpoint)
    finally:
        endpoint.close()
    assert result.stdout == "pri: fetch-failed\n" and result.returncode == 1


def test_unreachable_endpoint_is_fetch_failed(bench):
    # Port 1 on the loopback refuses at once: a network failure, not an HTTP answer.
    result = fetch(bench, JELLO_USAGE_API_URL="http://127.0.0.1:1/usage")
    assert result.stdout == "pri: fetch-failed\n" and result.returncode == 1


@pytest.mark.parametrize("literal", ["1e999", "NaN"])
def test_non_finite_numbers_are_not_a_window(bench, tmp_path, literal):
    """json.loads reads `1e999` as infinity and `NaN` as a float, where the reference's jq
    rejects both tokens outright. The window is absent, so the profile prints fetch-failed;
    every other account still gets its own line, and nothing raises."""
    codex = with_codex(bench, tmp_path)
    body = ('{"five_hour": {"used_percentage": %s, "resets_at": %s}}'
            % (literal, literal)).encode()
    endpoint = Endpoint([(200, body)])
    try:
        result = fetch(bench, endpoint, CODEX_BIN=codex)
    finally:
        endpoint.close()
    assert result.stdout == "pri: fetch-failed\ncx: ok\n", "one line per account, census order"
    # The reference exits 0 while any account succeeded, and a traceback would have taken
    # the codex line with it.
    assert result.returncode == 0
    assert "Traceback" not in result.stderr
    assert not os.path.exists(cache_path(bench, ".usage-api-cache.json"))


def test_an_unexpected_exception_stays_inside_its_job(monkeypatch, capsys):
    """Whatever a job raises, its account still gets exactly one outcome line, and the
    trace names the exception class only -- a message can quote a body or a header."""
    monkeypatch.setenv("USAGE_HUD_FETCH_DEBUG", "1")

    def boom(secret):
        raise OverflowError(f"cannot convert {secret}")

    assert fetch_module.contained("pri", boom, (TOKEN,)) == ("fetch-failed", False)
    trace = capsys.readouterr().err
    assert "pri: unexpected OverflowError" in trace
    assert TOKEN not in trace


# --- C09 -------------------------------------------------------------------------------

def test_token_never_leaks(bench):
    """Law L1: with both trace switches on, the token string appears in no stream and in
    no file the run wrote."""
    now = int(time.time())
    endpoint = Endpoint([(401, {}), (200, usage_payload(now))])
    try:
        result = fetch(bench, endpoint, USAGE_HUD_FETCH_DEBUG="1", USAGE_HUD_FETCH_DUMP="1")
    finally:
        endpoint.close()
    assert result.returncode == 0
    assert "[dbg]" in result.stderr and "[dump]" in result.stderr, "the traces did run"
    assert TOKEN not in result.stdout
    assert TOKEN not in result.stderr
    for base, _, names in os.walk(str(bench["home"])):
        for name in names:
            path = os.path.join(base, name)
            assert TOKEN not in pathlib.Path(path).read_text(errors="replace"), path
    # R12's other side: fetch is the one command that MAY read the token bytes, so its
    # Keychain calls do pass -w. `usage show` never does -- see
    # test_usage_snapshot.py::test_show_never_reads_token_bytes.
    calls = [line.split("\t") for line in
             bench["security_log"].read_text().splitlines() if line]
    assert calls and all(argv[0] == "find-generic-password" for argv in calls)
    assert any("-w" in argv for argv in calls)


def test_a_redirect_is_never_followed(bench):
    """urllib's default handler replays the whole header set -- `Authorization` with it --
    at whatever origin a 30x names, and the reference's curl followed no redirect at all.
    So the second origin is never contacted, and the 30x is read as an ordinary failure."""
    target = Endpoint([(200, usage_payload(int(time.time())))])
    hop = Endpoint([(302, {}, {"Location": target.url})])
    try:
        result = fetch(bench, hop, USAGE_HUD_FETCH_DEBUG="1")
    finally:
        hop.close()
        target.close()
    assert hop.calls == 1
    assert target.calls == 0 and target.requests == [], "no request reached the second origin"
    assert result.stdout == "pri: fetch-failed\n" and result.returncode == 1
    assert TOKEN not in result.stdout and TOKEN not in result.stderr
    assert not os.path.exists(cache_path(bench, ".usage-api-cache.json"))


def test_a_body_that_quotes_the_token_never_prints_it(bench):
    """The dump prints a parsed 200 body, and a body can quote the bearer value back at us.
    Redaction is what keeps law L1 true for a response we do not control."""
    payload = usage_payload(int(time.time()))
    payload["echo"] = f"Authorization: Bearer {TOKEN}"
    endpoint = Endpoint([(200, payload)])
    try:
        result = fetch(bench, endpoint, USAGE_HUD_FETCH_DEBUG="1", USAGE_HUD_FETCH_DUMP="1")
    finally:
        endpoint.close()
    assert result.stdout == "pri: ok\n" and result.returncode == 0
    assert "<token>" in result.stderr, "the echo was printed, redacted"
    assert TOKEN not in result.stdout and TOKEN not in result.stderr
    for base, _, names in os.walk(str(bench["home"])):
        for name in names:
            path = os.path.join(base, name)
            assert TOKEN not in pathlib.Path(path).read_text(errors="replace"), path


def test_a_failed_request_raises_nothing(monkeypatch):
    """The in-process half of law L1: the request fails while the token is in hand, and the
    failure comes back as an answer rather than as an exception -- so there is no message
    for the token to ride out on. The endpoint is closed under the request to force it."""
    endpoint = Endpoint([(200, {})])
    endpoint.close()
    monkeypatch.setenv("JELLO_USAGE_API_URL", endpoint.url)
    assert fetch_module.request_usage(TOKEN) == (None, None)


# --- C10 -------------------------------------------------------------------------------

def test_fetch_codex(bench, tmp_path):
    home = bench["home"]
    directory = os.path.join(str(home), ".codex")
    os.makedirs(os.path.join(directory, "sessions"), exist_ok=True)
    write(os.path.join(directory, "auth.json"),
          json.dumps(codex_auth("c@example.test", "pro", "acc-1")) + "\n")
    write(os.path.join(directory, "profile-label"), "\n")
    codex = executable(tmp_path / "stubs" / "codex", CODEX_STUB)
    now = int(time.time())
    endpoint = Endpoint([(200, usage_payload(now))])
    try:
        result = fetch(bench, endpoint, CODEX_BIN=codex)
    finally:
        endpoint.close()
    assert result.returncode == 0
    assert result.stdout == "pri: ok\ncx: ok\n", "census order: claude first, then codex"
    written = json.loads(
        pathlib.Path(os.path.join(directory, ".usage-hud-api-cache.json")).read_text())
    assert written["rate_limits"]["primary"] == {"used_percent": 30, "window_minutes": 10080,
                                                 "resets_at": 4102444800}
    assert written["rate_limits"]["secondary"] is None and written["source"] == "api"


def test_fetch_codex_without_a_binary(bench):
    directory = os.path.join(str(bench["home"]), ".codex")
    os.makedirs(os.path.join(directory, "sessions"), exist_ok=True)
    write(os.path.join(directory, "auth.json"),
          json.dumps(codex_auth("c@example.test", "pro", "acc-1")) + "\n")
    endpoint = Endpoint([(500, {})])
    try:
        result = fetch(bench, endpoint)
    finally:
        endpoint.close()
    assert result.stdout == "pri: fetch-failed\ncx: fetch-failed\n"
    assert result.returncode == 1


# --- C22 -------------------------------------------------------------------------------

def test_fetch_no_profiles(tmp_path):
    home = tmp_path / "empty"
    os.makedirs(str(home))
    result = run_jello(["usage", "fetch"], fixture_env(home))
    assert result.stdout == "" and result.returncode == 1
